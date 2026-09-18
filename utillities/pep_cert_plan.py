"""Offline certification-plan reducer (cert-plan/1) for PEP build-integrated testing.

PURE and DETERMINISTIC: no GitHub API calls, no rpm/dpkg execution, no container
inspection, no project-specific job-name parsing. Those belong to later live
adapters. This module consumes ALREADY-STRUCTURED data and emits a deterministic
cert-plan/1 document describing, per planned build cell:

  * effective BUILD STATE   (available | incomplete | failed | never_ran | ambiguous)
  * PUBLICATION STATE       (publish_confirmed | publish_unconfirmed | publish_skipped)
  * inspected MEMBERS        (all retained; source/debug excluded from certification)
  * selected TARGETS         (runtime members allowed by policy, arch-agreeing, with the
                              evidence — identity + supported arch + checksum — needed to
                              justify eligibility)
  * package_identity_state   (pre-test consistency of the physical package vs the
                              intended version/build — DISTINCT from PEP's L1/L2a/L2b
                              result evidence, produced later)

Contract guarantees:
  - Seed every planned cell; the coverage denominator is the ORIGINAL planned count
    (malformed/duplicate entries are surfaced, never silently dropped).
  - `plan_resolved == false` is a GLOBAL certification stop: no target may be eligible.
  - Require ONE unambiguous latest job mapping. A latest non-success conclusion overrides
    every artifact; a latest success plus the EXACT current stable cell artifact is
    `available` (a carried-forward success counts); a latest success without its artifact
    is `incomplete`. The artifact's producing attempt is NEVER required to equal the
    jobs-API run_attempt.
  - Malformed structured input yields a deterministic unresolved/ambiguous result, never
    an uncaught exception and never an eligible target.
  - Require unique planned cell_id and artifact_name. Exact-duplicate job records are
    deduplicated; conflicting duplicates are ambiguous.

Stdlib only. Reuses PEP family/channel/arch vocabulary. Identity uses an EXACT native
comparison grounded in the shared pgEdge packaging convention (pkg/common.sh,
pkg/build-rpm.sh, pkg/build-deb.sh) — NOT the coarse L1 normalizer. Unit-testable via
`pytest utillities/test_pep_cert_plan.py`.
"""
from __future__ import annotations

import json
import re
from collections import Counter

SCHEMA = "cert-plan/1"

BUILD_STATES = ("available", "incomplete", "failed", "never_ran", "ambiguous")
PUBLICATION_STATES = ("publish_confirmed", "publish_unconfirmed", "publish_skipped")
TARGET_SELECTION_STATES = ("resolved", "target_unresolved", "target_ambiguous")
IDENTITY_STATES = ("confirmed", "mismatch", "unverified")

# Shared PEP vocabulary (mirrors utillities/container_resolver.py family/arch tokens and
# the pep-integration.yml channel comment).
FAMILIES = ("rpm", "deb")
SUPPORTED_ARCHES = ("amd64", "arm64")
CHANNELS = ("release", "staging", "daily")
# Execution intent, owned by the coordinator (execution_mode). "full" is the strict,
# certifiable path (published, non-simulated). "preview" is a DRY-RUN planning path that
# additionally admits a simulated, built-and-identity-confirmed target that was truthfully
# NOT published (see _strict_eligibility / _preview_eligibility). Absent == "full"
# (backward-compatible strict default).
EXECUTION_MODES = ("preview", "full")
_ARCH_INDEPENDENT = {"noarch", "all"}

# cert-result/1 boundary vocabulary — reused from PEP, emitted here ONLY as placeholders
# so downstream layers share one enum. No results are computed in Stage 1.
EXECUTION_STATUS_VOCAB = ("completed", "preview", "incomplete", "infra_failure")
TEST_VERDICT_VOCAB = ("pass", "fail", "not_run")

_ARCH_NORMALIZE = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_REQUIRED_CELL_KEYS = ("cell_id", "artifact_name", "family", "os", "normalized_arch")

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_PG_MAJOR_RE = re.compile(r"\A[0-9]+\Z")          # build_pg_major: nonblank, digits only


def _parse_simulated(value):
    """(value, valid). Accept ONLY a real bool or the GitHub-input strings 'true'/'false'
    (case-insensitive). Anything else — including a missing key — is invalid, which makes
    certification non-certifiable rather than being silently guessed."""
    if isinstance(value, bool):
        return value, True
    if isinstance(value, str):
        s = value.strip().lower()
        if s == "true":
            return True, True
        if s == "false":
            return False, True
    return None, False


def _nonempty(v):
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    return True


def _valid_sha256(v):
    return isinstance(v, str) and _SHA256_RE.match(v.strip()) is not None


def _has_epoch(member):
    """True if the member declares a real (nonempty, nonzero) epoch. Stage 1 does not
    compare epochs, so a real epoch makes identity unverified (epoch support deferred)."""
    e = member.get("epoch")
    if e is None:
        return False
    if isinstance(e, str) and e.strip() in ("", "0"):
        return False
    if isinstance(e, int) and e == 0:
        return False
    return True


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _is_pos_int(x):
    return _is_int(x) and x >= 1


def _nonblank_str(x):
    return isinstance(x, str) and x.strip() != ""


# Member identity fields that must be explicit strings for a member to be selectable.
_MEMBER_STR_FIELDS = ("package_name", "version", "release", "native_arch", "package_class", "sha256")


def _allowed_set(policy):
    """Sanitize the policy allowlist ONCE into a hashable string-only set. A non-list value
    or non-string entries are dropped here (the envelope validator diagnoses them); the raw
    value is never sorted or iterated by callers."""
    raw = policy.get("allowed_runtime_package_names")
    if not isinstance(raw, list):
        return set()
    return {x for x in raw if isinstance(x, str)}


def _validate_envelope(inp):
    """Validate the complete input envelope BEFORE reduction. Returns a deterministic
    list of structural diagnostics (empty == structurally sound). Any diagnostic makes
    the plan non-certifiable; malformed records are diagnosed here rather than silently
    dropped. Read-only: never mutates, never raises."""
    if not isinstance(inp, dict):
        return ["input is not an object"]
    d = []
    for key, typ, label in (("release_intent", dict, "an object"),
                            ("component_policy", dict, "an object"),
                            ("provenance", dict, "an object"),
                            ("job_records", list, "a list"),
                            ("artifacts", list, "a list"),
                            ("publication_results", dict, "an object")):
        if key in inp and inp[key] is not None and not isinstance(inp[key], typ):
            d.append("%s must be %s" % (key, label))
    pol = inp.get("component_policy")
    if isinstance(pol, dict):
        arp = pol.get("allowed_runtime_package_names")
        if arp is not None:
            if not isinstance(arp, list):
                d.append("component_policy.allowed_runtime_package_names must be a list")
            elif not all(_nonblank_str(x) for x in arp):
                d.append("component_policy.allowed_runtime_package_names must be nonblank strings")
            elif len(set(arp)) != len(arp):
                d.append("component_policy.allowed_runtime_package_names must be unique")
        ebv = pol.get("expected_binary_version")
        if ebv is not None and not isinstance(ebv, str):
            d.append("component_policy.expected_binary_version must be a string")
    jr = inp.get("job_records")
    if isinstance(jr, list):
        for i, r in enumerate(jr):
            if not isinstance(r, dict):
                d.append("job_records[%d] is not an object" % i)
                continue
            if not _nonblank_str(r.get("cell_id")):
                d.append("job_records[%d].cell_id must be a nonblank string" % i)
            if not _is_int(r.get("job_id")):
                d.append("job_records[%d].job_id must be an integer" % i)
            if not _is_pos_int(r.get("run_attempt")):
                d.append("job_records[%d].run_attempt must be a positive integer" % i)
            if not isinstance(r.get("status"), str):
                d.append("job_records[%d].status must be a string" % i)
            if r.get("conclusion") is not None and not isinstance(r.get("conclusion"), str):
                d.append("job_records[%d].conclusion must be a string or null" % i)
    arts = inp.get("artifacts")
    if isinstance(arts, list):
        for i, a in enumerate(arts):
            if not isinstance(a, dict):
                d.append("artifacts[%d] is not an object" % i)
                continue
            if not _nonblank_str(a.get("name")):
                d.append("artifacts[%d].name must be a nonblank string" % i)
            mems = a.get("members")
            if mems is None:
                continue
            if not isinstance(mems, list):
                d.append("artifacts[%d].members must be a list" % i)
                continue
            for j, m in enumerate(mems):
                if not isinstance(m, dict):
                    d.append("artifacts[%d].members[%d] is not an object" % (i, j))
                    continue
                for f in _MEMBER_STR_FIELDS:
                    if f in m and m[f] is not None and not isinstance(m[f], str):
                        d.append("artifacts[%d].members[%d].%s must be a string" % (i, j, f))
                ep = m.get("epoch")
                if not (ep is None or isinstance(ep, str) or _is_int(ep)):
                    d.append("artifacts[%d].members[%d].epoch must be a string, integer, or null" % (i, j))
    return d


def normalize_arch(native_arch):
    """Native package arch -> normalized arch. Non-string values (incl None) return None;
    unknown/arch-independent string tokens (noarch, all, src, …) pass through unchanged.
    Total: never raises on unhashable (list/dict) input."""
    if not isinstance(native_arch, str):
        return None
    return _ARCH_NORMALIZE.get(native_arch, native_arch)


def _empty_evidence():
    return {"latest_job_id": None, "latest_run_attempt": None, "latest_status": None,
            "latest_conclusion": None, "artifact_present": False, "artifact_id": None,
            "artifact_duplicate": False}


# --- build state ------------------------------------------------------------
def _resolve_build(cell, job_records, artifacts):
    """Return (build_state, build_evidence, matched_artifact_or_None). Never raises."""
    recs = [r for r in job_records if isinstance(r, dict) and r.get("cell_id") == cell["cell_id"]]
    matching = [a for a in artifacts if isinstance(a, dict) and a.get("name") == cell["artifact_name"]]
    art_dup = len(matching) > 1
    art = matching[0] if len(matching) == 1 else None
    ev = _empty_evidence()
    ev.update(artifact_present=art is not None, artifact_id=(art.get("id") if art else None),
              artifact_duplicate=art_dup)

    if not recs:
        return "never_ran", ev, art

    # Fail closed on an invalid run_attempt (must be a POSITIVE integer; bool excluded) or a
    # non-integer job_id — this also avoids unhashable/type errors when grouping below.
    if any(not _is_pos_int(r.get("run_attempt")) for r in recs):
        ev["invalid_reason"] = "invalid_run_attempt"
        return "ambiguous", ev, art
    if any(not _is_int(r.get("job_id")) for r in recs):
        ev["invalid_reason"] = "invalid_job_id"
        return "ambiguous", ev, art
    # status/conclusion must be hashable scalars (string, or null conclusion) BEFORE they are
    # folded into dedup/grouping keys — otherwise a list/dict value would raise while hashing.
    if any(not isinstance(r.get("status"), str)
           or not (r.get("conclusion") is None or isinstance(r.get("conclusion"), str))
           for r in recs):
        ev["invalid_reason"] = "invalid_status_or_conclusion"
        return "ambiguous", ev, art

    # Collapse EXACT duplicates; a CONFLICTING duplicate (same job_id+run_attempt, different
    # status/conclusion) is ambiguous — and this is order-independent.
    seen, uniq = set(), []
    for r in recs:
        key = (r.get("job_id"), r.get("run_attempt"), r.get("status"), r.get("conclusion"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    by_jra = {}
    for r in uniq:
        by_jra.setdefault((r.get("job_id"), r.get("run_attempt")), set()).add(
            (r.get("status"), r.get("conclusion")))
    if any(len(v) > 1 for v in by_jra.values()):
        ev["invalid_reason"] = "conflicting_job_records"
        return "ambiguous", ev, art
    if art_dup:
        return "ambiguous", ev, art

    maxa = max(r.get("run_attempt") for r in uniq)
    at_max = [r for r in uniq if r.get("run_attempt") == maxa]
    if len({r.get("job_id") for r in at_max}) > 1:
        ev["latest_run_attempt"] = maxa
        return "ambiguous", ev, art

    latest = at_max[0]                              # single job, exact-dups collapsed -> deterministic
    ev.update(latest_job_id=latest.get("job_id"), latest_run_attempt=maxa,
              latest_status=latest.get("status"), latest_conclusion=latest.get("conclusion"))
    if latest.get("status") != "completed":
        return "incomplete", ev, art               # latest not completed -> unconfirmed
    if latest.get("conclusion") != "success":
        return "failed", ev, art                   # non-success overrides artifacts
    if art is not None:
        return "available", ev, art                # success + exact stable artifact
    return "incomplete", ev, art                   # success without its artifact


# --- publication state ------------------------------------------------------
def _resolve_publication(family, build_state, publication_results, simulated):
    """Resolve the TRUTHFUL publication state/reason for a family. A simulated run did NOT publish, so
    its push outcomes are read fail-closed and NEVER collapsed into a confirmed publication:
      * a genuinely absent/skipped family push is the simulated skip (``publish_skipped``/``simulated``)
        -- the sole preview-eligibility basis;
      * an explicit ``failure``/``cancelled`` stays ``publish_unconfirmed`` (fail-closed);
      * a contradictory ``success`` (a simulated run cannot have published) is preserved fail-closed as
        ``publish_unconfirmed``/``simulated_with_family_push_success`` -- never a confirmed publication
        and never a simulated skip.
    A non-simulated run keeps the ordinary mapping (success + available -> ``publish_confirmed``). The
    raw outcome is preserved in the emitted state/reason rather than hidden."""
    push = (publication_results or {}).get(family)
    if push in (None, "skipped"):
        # Truthful skip: a simulated run's push jobs deliberately did not run (reason 'simulated');
        # a normal missing/skipped family is 'family_push_skipped'. Only the former is preview-eligible.
        return "publish_skipped", ("simulated" if simulated else "family_push_skipped")
    if push == "success":
        if simulated:
            # Contradiction: a simulated run did not publish, yet a family push reports success. Fail
            # closed on the raw contradiction -- never a confirmed publication nor a simulated skip.
            return "publish_unconfirmed", "simulated_with_family_push_success"
        if build_state == "available":
            return "publish_confirmed", "family_push_success"
        return "publish_skipped", "not_built"      # nothing of this cell to publish
    if push in ("failure", "cancelled"):
        return "publish_unconfirmed", "family_push_" + push
    return "publish_unconfirmed", "family_push_" + str(push)   # unknown -> fail closed


# --- identity (pre-test) ----------------------------------------------------
# Cell OS token -> native dist/distro suffix, grounded in the supported build matrix
# (RPM almalinux:N -> 'el-N' cell -> 'elN' dist; DEB codename cells use the codename).
def _expected_dist(family, os_token):
    if not isinstance(os_token, str) or not os_token:
        return None
    if family == "rpm":
        return os_token.replace("-", "")           # el-9 -> el9, el-10 -> el10
    if family == "deb":
        return os_token                            # bookworm -> bookworm
    return None


def _expected_native(family, os_token, version, buildnum):
    """Fully reconstruct the EXPECTED native (version, release) for a family+cell per the
    shared pgEdge convention (pkg/common.sh, build-rpm.sh, build-deb.sh), or None when the
    family/os is unsupported. Release is compared EXACTLY, so a dotted buildnum keeps its
    distro suffix and a wrong dist/distro is a mismatch.
      RPM -> Version=<version>,             Release=<buildnum>.<dist>  (buildnum verbatim)
      DEB -> a '<pretag>_<n>' buildnum folds the pretag into the version with '~'
             (2.0.0~beta3) and uses <n> as the revision: Release=<n>.<distro>.
    """
    dist = _expected_dist(family, os_token)
    if not dist:
        return None
    if family == "deb" and "_" in (buildnum or ""):
        pretag, num = buildnum.split("_", 1)
        return version + "~" + pretag, num + "." + dist
    return version, str(buildnum) + "." + dist


def _package_identity_state(member, family, os_token, intended_version, intended_buildnum):
    """EXACT native comparison of the inspected package against the INTENDED version/build
    AND the planned cell's dist/distro. Never records an observed binary version (PEP L2b).
    A real epoch is unverified (epoch comparison is deferred past Stage 1)."""
    if family not in FAMILIES:
        return "unverified"
    if not _nonblank_str(intended_version) or not _nonblank_str(intended_buildnum):
        return "unverified"                        # non-string intent cannot be a native match
    if _has_epoch(member):
        return "unverified"
    exp = _expected_native(family, os_token, intended_version, intended_buildnum)
    if exp is None:
        return "unverified"                        # unsupported family/os
    exp_v, exp_r = exp
    ok = member.get("version") == exp_v and member.get("release") == exp_r
    return "confirmed" if ok else "mismatch"


# --- target selection -------------------------------------------------------
def _make_target(cell, member, policy, release_intent, provenance, family, pg_norm):
    physical = member.get("package_name")
    return {
        "target_id": "%s::%s" % (cell["cell_id"], physical),   # globally unique; incl noarch/all
        "logical_component": release_intent.get("logical_component"),
        "producer_repo": (provenance or {}).get("repository"),
        "physical_package": physical,
        "family": family, "os": cell["os"],
        # execution_arch is what the atomic PEP workflow consumes (always amd64|arm64,
        # from the cell); native_package_arch is the member's own arch (incl noarch/all).
        "execution_arch": cell["normalized_arch"],
        "native_package_arch": member.get("native_arch"),
        # Build-side PostgreSQL identity, echoed verbatim for Stage 3 (nullable). NO
        # eligibility is derived here: pg_coupled tells Stage 3 whether build_pg_major
        # constrains certification (coupled) or is a test-policy choice (independent).
        "build_pg_major": pg_norm["build_pg_major"],
        "build_pg_version": pg_norm["build_pg_version"],
        "pg_coupled": pg_norm["pg_coupled"],
        "package": {"name": physical, "epoch": member.get("epoch"),
                    "version": member.get("version"), "release": member.get("release"),
                    "sha256": member.get("sha256")},
        "expected": {"intended_version": release_intent.get("intended_version"),
                     "intended_buildnum": release_intent.get("intended_buildnum"),
                     "expected_binary_version": policy.get("expected_binary_version")},
        "package_identity_state": _package_identity_state(
            member, family, cell.get("os"), release_intent.get("intended_version"),
            release_intent.get("intended_buildnum")),
    }


def _select_targets(cell, art, policy, release_intent, provenance, family, allowed, pg_norm):
    """Return (targets, target_selection_state, members_out). `allowed` is the pre-sanitized
    string-only allowlist set. Every inspected member is retained with `selected` +
    exclusion_reasons; a candidate must carry the evidence required to justify eligibility
    (allowed runtime name, supported+agreeing arch, and a non-empty checksum and version)."""
    members_out, candidates = [], []
    cell_arch_supported = cell.get("normalized_arch") in SUPPORTED_ARCHES
    raw_members = art.get("members") if art else []
    if not isinstance(raw_members, list):          # malformed members: fail closed, no target
        raw_members = []
    for m in raw_members:
        if not isinstance(m, dict):
            continue
        rec = dict(m)
        norm = normalize_arch(m.get("native_arch"))   # total: None for non-string arch
        rec["normalized_arch"] = norm
        reasons = []
        str_ok = all(isinstance(m.get(f), str) for f in _MEMBER_STR_FIELDS)
        if not str_ok:
            # identity fields aren't all strings -> unselected; DON'T run any check below that
            # needs a string/hashable value (allowlist membership, arch compare, sha, version).
            reasons.append("non_string_identity_field")
        else:
            if m.get("package_class") != "runtime":
                reasons.append("non_runtime")
            if m.get("package_name") not in allowed:
                reasons.append("package_not_allowed")
            if not (norm in SUPPORTED_ARCHES or norm in _ARCH_INDEPENDENT):
                reasons.append("unsupported_arch")
            elif not (norm == cell.get("normalized_arch") or norm in _ARCH_INDEPENDENT):
                reasons.append("arch_mismatch")
            if not cell_arch_supported:
                reasons.append("cell_arch_unsupported")
            sha = m.get("sha256")
            if not sha:
                reasons.append("missing_checksum")
            elif not _valid_sha256(sha):
                reasons.append("invalid_checksum")
            if not _nonempty(m.get("version")):
                reasons.append("missing_version")
        rec["selected"] = not reasons
        if reasons:
            rec["exclusion_reasons"] = reasons
        members_out.append(rec)
        if not reasons:
            candidates.append(rec)
    members_out.sort(key=lambda r: (str(r.get("package_name") or ""), str(r.get("native_arch") or ""),
                                    str(r.get("sha256") or "")))
    if not candidates:
        return [], "target_unresolved", members_out
    dup = Counter((c.get("package_name"), c.get("normalized_arch")) for c in candidates)
    if any(v > 1 for v in dup.values()):
        return [], "target_ambiguous", members_out
    targets = [_make_target(cell, c, policy, release_intent, provenance, family, pg_norm) for c in candidates]
    targets.sort(key=lambda t: str(t["target_id"]))
    return targets, "resolved", members_out


def _strict_eligibility(build_state, pub_state, identity, simulated, family, channel,
                        plan_resolved, context_ok):
    """STRICT certification eligibility -- cert-plan/1's existing ``eligibility`` field, with its
    UNCHANGED vocabulary (``eligible`` | ``ineligible``). A target is ``eligible`` only when the
    build is available, the family push is CONFIRMED, the identity is confirmed and the release is
    NOT simulated. A simulated (hence unpublished) target is therefore always ``ineligible`` here.
    Existing consumers that know only this field keep treating a preview target as ineligible."""
    if not plan_resolved:
        return "ineligible", "plan_unresolved"
    if not context_ok:
        return "ineligible", "incomplete_release_context"
    if simulated:
        return "ineligible", "simulated_not_eligible"
    if family not in FAMILIES:
        return "ineligible", "unsupported_family"
    if channel not in CHANNELS:
        return "ineligible", "unsupported_channel"
    if build_state != "available":
        return "ineligible", "build_" + build_state
    if pub_state != "publish_confirmed":
        return "ineligible", "publication_" + pub_state
    if identity != "confirmed":
        return "ineligible", "identity_" + identity
    return "eligible", "built_published_identity_confirmed"


def _preview_eligibility(build_state, pub_state, pub_reason, identity, simulated, family, channel,
                         plan_resolved, context_ok, execution_mode):
    """ADDITIVE dry-run eligibility -- cert-plan/1's new, self-describing ``preview_eligibility``
    field (``eligible`` | ``ineligible``), SEPARATE from strict ``eligibility`` and mode-bound to the
    stamped ``execution_mode``. It is ``eligible`` ONLY when the plan is a preview plan AND the target
    is a truthfully-unpublished SIMULATED build: the family publication is the genuine simulated skip
    (``publish_skipped`` with reason ``simulated``), the build is available, the package identity is
    confirmed and the family/channel are supported -- "built and identity-confirmed, dry-run planning
    only; NOT published". In a full plan it is uniformly ``ineligible`` (``not_preview_mode``). A real
    publication failure/cancellation, a contradictory success, a non-simulated skip, an unavailable
    build, an identity mismatch or missing context all stay ``ineligible``. It never asserts the
    package was published; the planner additionally runs it only when the requested mode is preview."""
    if not plan_resolved:
        return "ineligible", "plan_unresolved"
    if not context_ok:
        return "ineligible", "incomplete_release_context"
    if execution_mode != "preview":
        return "ineligible", "not_preview_mode"
    if not simulated:
        return "ineligible", "not_simulated"
    if not (pub_state == "publish_skipped" and pub_reason == "simulated"):
        # failure / cancellation / contradictory success / non-simulated skip -> the raw pub truth,
        # never a preview bypass.
        return "ineligible", "publication_" + pub_state
    if family not in FAMILIES:
        return "ineligible", "unsupported_family"
    if channel not in CHANNELS:
        return "ineligible", "unsupported_channel"
    if build_state != "available":
        return "ineligible", "build_" + build_state
    if identity != "confirmed":
        return "ineligible", "identity_" + identity
    return "eligible", "simulated_built_identity_confirmed_dry_run"


def _cell_header(c):
    return {k: (c.get(k) if isinstance(c, dict) else None) for k in _REQUIRED_CELL_KEYS}


def _entry_errors(c):
    """Structural validation of one planned entry -> list of reasons (empty == valid)."""
    if not isinstance(c, dict):
        return ["not_an_object"]
    reasons = []
    for k in _REQUIRED_CELL_KEYS:
        v = c.get(k)
        if k not in c or not isinstance(v, str) or v == "":
            reasons.append("missing_or_blank:" + k)
    return reasons


# --- build-side PostgreSQL identity (optional, nullable) --------------------
def _pg_build_identity(cell):
    """Normalize + validate a planned cell's OPTIONAL build-side PostgreSQL identity.

    Returns (normalized, errors). `normalized` always carries the three keys
    (build_pg_major, build_pg_version, pg_coupled) in a JSON-safe canonical form;
    `errors` is a deterministic list (empty == valid). This parses NOTHING from
    cell_id and is TOTAL (never raises for JSON-compatible input).

    Legal states (anything else is contradictory/malformed -> a global stop):
      * PG-INDEPENDENT: pg_coupled false/absent AND no non-blank build PG field
        (blank/absent/null representative detector metadata is tolerated -> null).
      * PG-COUPLED:     pg_coupled true, build_pg_major a nonblank numeric string,
        build_pg_version a nonblank string whose major agrees with build_pg_major.

    This is pure passthrough for Stage 3: NO eligibility behavior is derived here.
    """
    normalized = {"build_pg_major": None, "build_pg_version": None, "pg_coupled": False}
    if not isinstance(cell, dict):
        return normalized, []
    raw_coupled = cell.get("pg_coupled", False)
    raw_major = cell.get("build_pg_major")
    raw_version = cell.get("build_pg_version")
    # Canonical JSON-safe echo: only a nonblank string survives as a value; a real
    # bool survives as pg_coupled; everything else collapses to null / false.
    normalized = {
        "build_pg_major": raw_major if _nonblank_str(raw_major) else None,
        "build_pg_version": raw_version if _nonblank_str(raw_version) else None,
        "pg_coupled": raw_coupled if isinstance(raw_coupled, bool) else False,
    }
    errors = []
    if not isinstance(raw_coupled, bool):
        errors.append("pg_coupled must be a boolean")
        return normalized, errors                # coupling unknown -> fail closed
    # "supplied" = a usable nonblank string OR a present-but-wrong-typed value
    # (a non-null non-string). Blank / null / absent counts as NOT supplied.
    major_supplied = _nonblank_str(raw_major) or (raw_major is not None and not isinstance(raw_major, str))
    version_supplied = _nonblank_str(raw_version) or (raw_version is not None and not isinstance(raw_version, str))
    if not raw_coupled:
        # PG-INDEPENDENT: representative/detector PG metadata must NOT leak in as coupling.
        if major_supplied or version_supplied:
            errors.append("pg_coupled=false cell must not carry build_pg_major/build_pg_version")
        return normalized, errors
    # PG-COUPLED.
    if not (_nonblank_str(raw_major) and _PG_MAJOR_RE.match(raw_major)):
        errors.append("pg_coupled=true requires build_pg_major as a nonblank numeric string")
    if not _nonblank_str(raw_version):
        errors.append("pg_coupled=true requires build_pg_version as a nonblank string")
    elif _nonblank_str(raw_major) and _PG_MAJOR_RE.match(raw_major):
        if raw_version.split(".", 1)[0] != raw_major:      # major agreement only when both usable
            errors.append("build_pg_version major must agree with build_pg_major")
    return normalized, errors


# --- top-level reducer ------------------------------------------------------
def reduce(inp):
    """Reduce structured build/publication inputs to a deterministic cert-plan/1 dict.
    Never raises for JSON-compatible input; malformed shapes fail closed."""
    # Validate the whole envelope up front; any structural diagnostic makes the plan
    # non-certifiable (a global stop) rather than raising or silently dropping records.
    errors = _validate_envelope(inp)
    plan_resolved = not errors
    if not isinstance(inp, dict):
        inp = {}

    def _dict(v):
        return v if isinstance(v, dict) else {}

    def _list(v):
        return v if isinstance(v, list) else []

    ri = _dict(inp.get("release_intent"))
    policy = _dict(inp.get("component_policy"))
    provenance = _dict(inp.get("provenance"))
    jobs = [r for r in _list(inp.get("job_records")) if isinstance(r, dict)]
    artifacts = [a for a in _list(inp.get("artifacts")) if isinstance(a, dict)]
    pubs = _dict(inp.get("publication_results"))
    channel = ri.get("channel")
    allowed_set = _allowed_set(policy)               # sanitized once; used for selection + output

    # simulated is REQUIRED and strict (bool or 'true'/'false'); missing/unknown makes
    # certification non-certifiable rather than silently defaulting to a live run.
    sim_value, sim_valid = _parse_simulated(ri.get("simulated"))
    simulated = sim_value if sim_valid else True     # unknown -> fail closed (publish_skipped)

    # execution_mode is the coordinator's single execution intent, bound into the plan so a preview
    # plan cannot later be consumed as full (the invocation planner re-checks the stamped value).
    # Absent -> "full" (backward-compatible strict default); a malformed value fails the plan closed.
    em_raw = inp.get("execution_mode")
    if em_raw is None:
        execution_mode = "full"
    elif em_raw in EXECUTION_MODES:
        execution_mode = em_raw
    else:
        execution_mode = None
        plan_resolved = False
        errors.append("execution_mode %r is not one of %s" % (em_raw, list(EXECUTION_MODES)))

    # Minimum release-intent + provenance required before ANY target can be eligible: an
    # eligible target must not carry a null logical component or producer repository.
    context_missing = []
    if not sim_valid:
        context_missing.append("simulated")
    for _key, _val in (("logical_component", ri.get("logical_component")),
                       ("effective_tag", ri.get("effective_tag")),
                       ("intended_version", ri.get("intended_version")),
                       ("intended_buildnum", ri.get("intended_buildnum")),
                       ("provenance.repository", provenance.get("repository"))):
        if not _nonblank_str(_val):               # must be a nonblank STRING (not numeric)
            context_missing.append(_key)
    context_ok = not context_missing
    for _m in context_missing:
        errors.append("release context missing/invalid: %s" % _m)

    planned = inp.get("planned_cells")
    is_list = isinstance(planned, list)
    planned_list = planned if is_list else []
    if not is_list or not planned_list:
        plan_resolved = False
        errors.append("planned_cells missing or empty")

    entry_reasons = []
    for i, c in enumerate(planned_list):
        rs = _entry_errors(c)
        entry_reasons.append(rs)
        if rs:
            plan_resolved = False
            errors.append("planned_cells[%d] invalid: %s" % (i, ",".join(rs)))

    cid_counts = Counter(c.get("cell_id") for c in planned_list
                         if isinstance(c, dict) and isinstance(c.get("cell_id"), str))
    an_counts = Counter(c.get("artifact_name") for c in planned_list
                        if isinstance(c, dict) and isinstance(c.get("artifact_name"), str))
    dup_cids = {k for k, v in cid_counts.items() if v > 1}
    dup_ans = {k for k, v in an_counts.items() if v > 1}
    if dup_cids:
        plan_resolved = False
        errors.append("duplicate cell_id: %s" % sorted(dup_cids))
    if dup_ans:
        plan_resolved = False
        errors.append("duplicate artifact_name: %s" % sorted(dup_ans))

    # Build-PG identity is validated in a PRE-PASS (like entry_reasons/dups) so the global
    # plan_resolved stop is fully settled BEFORE any target eligibility is computed. Doing it
    # inside the cell loop made the stop order-dependent (a valid cell processed before a
    # later invalid-PG cell could be marked eligible). Cache the normalized identity for reuse.
    pg_norms = []
    for i, c in enumerate(planned_list):
        pg_norm, pg_errs = _pg_build_identity(c)
        pg_norms.append(pg_norm)
        if pg_errs:                                 # contradictory/malformed PG => global stop
            plan_resolved = False
            for r in pg_errs:
                errors.append("planned_cells[%d] pg identity invalid: %s" % (i, r))

    cells_out = []
    for i, c in enumerate(planned_list):
        header = _cell_header(c)
        pg_norm = pg_norms[i]                        # reuse cached normalized PG identity
        base = {**header, **pg_norm, "_index": i, "planned": True}
        if entry_reasons[i]:                        # malformed entry: fail closed, never eligible
            cells_out.append({**base, "build_state": "ambiguous", "invalid_reasons": entry_reasons[i],
                              "build_evidence": _empty_evidence(), "members": [],
                              "publication_state": "publish_skipped", "publication_reason": "invalid_cell",
                              "targets": [], "target_selection_state": "target_unresolved"})
            continue
        if header["cell_id"] in dup_cids or header["artifact_name"] in dup_ans:
            reason = "duplicate_cell_id" if header["cell_id"] in dup_cids else "duplicate_artifact_name"
            pub_state, pub_reason = _resolve_publication(header["family"], "ambiguous", pubs, simulated)
            cells_out.append({**base, "build_state": "ambiguous", "ambiguity_reason": reason,
                              "build_evidence": _empty_evidence(), "members": [],
                              "publication_state": pub_state, "publication_reason": pub_reason,
                              "targets": [], "target_selection_state": "target_unresolved"})
            continue

        family = header["family"]
        build_state, ev, art = _resolve_build(c, jobs, artifacts)
        pub_state, pub_reason = _resolve_publication(family, build_state, pubs, simulated)
        targets, sel_state, members_out = _select_targets(c, art, policy, ri, provenance, family, allowed_set, pg_norm)
        for t in targets:
            ident = t["package_identity_state"]
            # STRICT (certifiable) eligibility — cert-plan/1's existing field, unchanged vocabulary.
            t["eligibility"], t["eligibility_reason"] = _strict_eligibility(
                build_state, pub_state, ident, simulated, family, channel, plan_resolved, context_ok)
            # ADDITIVE dry-run eligibility — a SEPARATE, mode-bound field: _preview_eligibility returns
            # "not_preview_mode" (ineligible) unless this plan's execution_mode is preview, so it is
            # eligible only in a preview-stamped plan; the planner then runs it only in preview.
            t["preview_eligibility"], t["preview_eligibility_reason"] = _preview_eligibility(
                build_state, pub_state, pub_reason, ident, simulated, family, channel,
                plan_resolved, context_ok, execution_mode)
        cells_out.append({**base, "build_state": build_state, "build_evidence": ev,
                          "members": members_out, "publication_state": pub_state,
                          "publication_reason": pub_reason, "targets": targets,
                          "target_selection_state": sel_state})

    cells_out.sort(key=lambda x: (str(x.get("cell_id") or ""), x["_index"]))
    for x in cells_out:
        x.pop("_index", None)

    available_n = sum(1 for x in cells_out if x["build_state"] == "available")
    selected_n = sum(len(x["targets"]) for x in cells_out)
    eligible_n = sum(1 for x in cells_out for t in x["targets"] if t.get("eligibility") == "eligible")
    preview_eligible_n = sum(1 for x in cells_out for t in x["targets"]
                             if t.get("preview_eligibility") == "eligible")

    return {
        "schema": SCHEMA,
        "plan_resolved": plan_resolved,
        "execution_mode": execution_mode,              # bound intent; null == malformed (fail closed)
        "errors": errors,
        "provenance": {k: provenance.get(k) for k in ("repository", "run_id", "run_attempt", "sha", "ref")},
        "release_intent": {                            # NO singular physical_package here
            "logical_component": ri.get("logical_component"),
            "intended_version": ri.get("intended_version"),
            "intended_buildnum": ri.get("intended_buildnum"),
            "effective_tag": ri.get("effective_tag"),
            "channel": channel,
            "simulated": (sim_value if sim_valid else None)},   # null == missing/invalid
        "component_policy": {
            "allowed_runtime_package_names": sorted(allowed_set),   # same sanitized value
            "expected_binary_version": policy.get("expected_binary_version")},
        "cells": cells_out,
        "coverage_denominators": {
            "planned_build_cells": len(planned_list),   # honest: original planned entries
            "available_build_cells": available_n,
            "selected_targets": selected_n,
            "eligible_targets": eligible_n,              # STRICT (certifiable) eligibility only
            "preview_eligible_targets": preview_eligible_n,  # dry-run-only (simulated, unpublished)
            "required_invocations": None},               # policy-derived; later stage
        "cert_result_boundary": {                        # placeholder only; no results in Stage 1
            "schema": "cert-result/1",
            "execution_status_vocab": list(EXECUTION_STATUS_VOCAB),
            "test_verdict_vocab": list(TEST_VERDICT_VOCAB)},
    }


def to_json(plan):
    """Canonical, deterministic serialization of a cert-plan/1 dict."""
    return json.dumps(plan, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main(argv=None):
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        sys.stderr.write("usage: pep_cert_plan.py <reducer-input.json>\n")
        return 2
    with open(argv[0]) as fh:
        sys.stdout.write(to_json(reduce(json.load(fh))))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
