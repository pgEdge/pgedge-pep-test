#!/usr/bin/env python3
"""Aggregate self-identifying atomic PEP results into a deterministic cert-result/1.

PURE and DETERMINISTIC: no filesystem, network, environment or clock access in the
core (``build_cert_result``). This module consumes an ALREADY-RESOLVED
``pep-invocation-plan/1`` document (the planner's output, which enumerates every
EXPECTED invocation and carries the release-run provenance) plus a list of
ALREADY-PARSED atomic result summaries (each produced by ``pep_result_summary.py``
and self-identifying via a top-level ``invocation_id``). It emits a
``cert-result/1`` document that keeps three axes SEPARATE — execution status,
product verdict, and coverage status — and represents every leg truthfully.

Join model (no artifact-name parsing): each atomic summary self-identifies with a
top-level ``invocation_id``; the reducer joins it to the corresponding
``matrix.include`` entry from the plan (which owns the dimensions, package identity,
SHA and expected pins). The full planned entry is preserved verbatim per leg as
``planned_invocation`` alongside the atomic outcome and full validated provenance.

Attempt awareness (rerun-safe): a GitHub run keeps ONE ``run_id`` across every re-run
attempt (only ``run_attempt`` increments), so identity binds to the FOUR
attempt-stable provenance fields (repository/run_id/sha/ref) and the producing attempt
is judged separately — each summary's ``caller_run_attempt`` is classified against the
REQUIRED live aggregation attempt (``current_run_attempt``): equal is current, lower is
prior/historical, greater is a future violation (fail-closed). Only a current-attempt
result fills a leg; a prior-attempt record is retained in ``historical_results`` for
audit and is NEVER promoted (so an expected id with only carried-forward history is a
truthful ``missing_result`` leg). ``provenance.run_attempt`` stays the SOURCE
plan/capture attempt; ``attempt_context`` reports both the plan and aggregation
attempts without relabelling either. No timestamps and no newest-wins selection.

Package proof (every current, non-preview leg; preview legs are exempt): a PASS needs the
installed bytes AND the installed identity proven, so the reducer checks each such leg
against its planned invocation:
  * digest — the summary's ``installed_package_sha256`` (the SHA-256 of the file a
    verified pinned install used) must equal the plan's ``package.sha256``. Every plan
    entry must carry a valid 64-hex digest (structural). The leg records
    ``package_digest`` = match | mismatch | missing (null digest, or a summary that
    predates the field) | not_required (preview);
  * identity — the plan's rungs must be proven: l1 and l2a (exact package-manager
    version-release; every plan entry must carry its family's exact pin, equal to the
    planned package's version-release) always; l2b only when the component policy
    planned an expected binary version. ``unproven_identity_rungs`` lists the gaps.
    Proven identity also binds the digest: the identity test proves identity only
    after checking that the install evidence carrying the digest belongs to the current
    run and target (it otherwise records l1 not_attempted).
A completed leg with a problem becomes ``incomplete`` with the specific reason_code —
``package_digest_mismatch`` over ``package_digest_missing`` over ``identity_unproven`` —
while its verdict, counts and failures are kept as reported. A mismatch is also recorded
on an incomplete or infra leg. The aggregate reason_code carries these reasons (see
``_aggregate_execution``), so the gate can name them.

Structural boundary (``result_resolved`` + ``errors[]``):
  * A malformed, unknown (id not in the plan), or duplicate result record, or a
    matched result whose provenance does not bind to the plan, is a STRUCTURAL
    validation failure -> ``result_resolved == false``, an empty/failed-safe result
    (infra_failure / not_run / none), ``reason_code == "validation_failure"``, and
    the offending records preserved in ``unexpected_results`` (duplicates retained,
    never silently resolved to one).
  * A genuinely MISSING expected result is NOT structural: it yields a truthfully
    resolved cert-result with a synthesized infra_failure / not_run / missing_result
    leg and partial coverage (a leg can fail before emitting its summary, so a
    missing result affects execution as well as coverage).

Scope of this slice (nothing else): the coordinator workflow, GitHub artifact
download, consolidated-report rendering, and any aggregate observe/gate exit policy
are OUT of scope. cert-result/1 is pure data; a gate/report consumer derives the
final decision from the three axes later.

Stdlib only. Unit-testable via ``pytest utillities/test_pep_cert_result.py``.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

SCHEMA = "cert-result/1"
PLAN_SCHEMA = "pep-invocation-plan/1"

# Established atomic vocabularies (must match pep_result_summary.py exactly). A
# summary whose status/verdict/mode falls outside these is malformed (fail-closed).
EXECUTION_STATUSES = ("completed", "incomplete", "infra_failure", "preview")
TEST_VERDICTS = ("pass", "fail", "not_run")
ENFORCEMENT_MODES = ("observe", "gate")

# Same workflow-safe charset the preflight/planner enforce for an invocation id.
# Matched with fullmatch() (never match()+`$`) so a trailing/embedded newline can
# never slip through — `$` matches before a final "\n", fullmatch does not.
_INVOCATION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
# A full git object name; only then can the pure reducer prove requested==resolved.
_FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
# Package digests: the plan's (capture) digest is compared case-insensitively; the
# summarizer always emits a lowercase observed digest.
_PLANNED_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_OBSERVED_SHA256_RE = re.compile(r"[0-9a-f]{64}")

# Package-proof reason codes, most severe first (a leg carries the first that applies).
RC_DIGEST_MISMATCH = "package_digest_mismatch"
RC_DIGEST_MISSING = "package_digest_missing"
RC_IDENTITY_UNPROVEN = "identity_unproven"
# Family -> (its exact package-manager pin field, the opposite family's field).
_PIN_FIELDS = {"rpm": ("expected_rpm", "expected_deb"), "deb": ("expected_deb", "expected_rpm")}

# Atomic identity-evidence and counts contracts (must match pep_result_summary.py).
# A matched summary must carry COMPLETE, well-formed evidence — never {} substituted
# for a missing/malformed object.
_EVIDENCE_RUNGS = ("l2a", "l2b", "l1")
_EVIDENCE_VALUES = ("proven", "not_proven", "not_attempted")
_COUNT_KEYS = ("tests", "failures", "errors", "skipped")

# The five release-run provenance source fields on the invocation plan.
_PLAN_PROVENANCE_KEYS = ("repository", "run_id", "run_attempt", "sha", "ref")
# The stable seven-key shape of the top-level cert-result provenance block.
_TOP_PROVENANCE_KEYS = _PLAN_PROVENANCE_KEYS + ("pep_requested_ref", "pep_resolved_sha")
# The ATTEMPT-STABLE release-run provenance fields that bind an atomic summary
# (caller_* on the summary) to the invocation plan (plan.provenance.*). run_attempt
# is DELIBERATELY EXCLUDED: it is not an identity discriminator (one GitHub run keeps
# a single run_id across every re-run attempt, incrementing only run_attempt), so a
# carried-forward prior-attempt summary shares these four fields and must be
# classified by attempt (current/prior/future) rather than rejected as foreign.
_STABLE_BINDING = (
    ("caller_repo", "repository"),
    ("caller_run_id", "run_id"),
    ("caller_sha", "sha"),
    ("caller_ref", "ref"),
)
# Provenance fields carried as identifying evidence for an unexpected record.
_PROVENANCE_KEYS = (
    "caller_repo", "caller_sha", "caller_ref", "caller_run_id", "caller_run_attempt",
    "pep_requested_ref", "pep_resolved_sha",
)


def _nonblank_str(x):
    return isinstance(x, str) and x.strip() != ""


def _coerce(v):
    """Compare provenance values by their string form (env values are strings, but a
    source plan may carry an int run id/attempt); returns "" for a None/blank value."""
    if v is None:
        return ""
    return str(v).strip()


def _is_positive_decimal_str(v):
    """A positive decimal string ("1", "123") — the form the capture path emits for a
    run id/attempt. Rejects booleans, ints, objects, arrays, zero, negatives and blanks
    (isdigit() is false for "-1"/""/non-digits; "0" is rejected by the >0 guard)."""
    return isinstance(v, str) and v.isdigit() and int(v) > 0


def _is_nonneg_int(v):
    """A true nonnegative integer — booleans are explicitly rejected (bool is an int
    subclass, so a JSON true/false must not pass as a 0/1 count)."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _identity_evidence_reason(ev):
    """Reason string if identity_evidence is not a complete, well-formed object, else None."""
    if not isinstance(ev, dict):
        return "identity_evidence is missing or not an object"
    if set(ev.keys()) != set(_EVIDENCE_RUNGS):
        return "identity_evidence must have exactly rungs l2a/l2b/l1"
    for rung in _EVIDENCE_RUNGS:
        if ev.get(rung) not in _EVIDENCE_VALUES:
            return "identity_evidence rung %r is not a recognized value" % rung
    return None


def _counts_reason(c):
    """Reason string if counts is not a complete, consistent object, else None."""
    if not isinstance(c, dict):
        return "counts is missing or not an object"
    if set(c.keys()) != set(_COUNT_KEYS):
        return "counts must have exactly tests/failures/errors/skipped"
    for k in _COUNT_KEYS:
        if not _is_nonneg_int(c.get(k)):
            return "counts.%s must be a nonnegative integer (booleans rejected)" % k
    if c["failures"] + c["errors"] + c["skipped"] > c["tests"]:
        return "counts failures+errors+skipped exceed tests"
    return None


def _status_verdict_counts_reason(execution_status, verdict, c):
    """Reason string if the verdict/counts/status combination contradicts the semantics
    pep_result_summary.py actually produces, else None (counts is a validated object).

      * fail    <- failures+errors > 0
      * pass    <- failures==0, errors==0, executed(tests-skipped) > 0
      * not_run <- failures==0, errors==0, executed == 0
      * preview / infra_failure MUST be not_run with ALL counts zero.
    A contradiction (e.g. pass with failures, fail with none, not_run with executed,
    a preview/infra leg claiming pass/fail or nonzero counts) is malformed."""
    tests, failures, errors, skipped = c["tests"], c["failures"], c["errors"], c["skipped"]
    executed = tests - skipped
    if verdict == "fail":
        if failures + errors <= 0:
            return "test_verdict 'fail' requires failures+errors > 0"
    elif verdict == "pass":
        if failures != 0 or errors != 0 or executed <= 0:
            return "test_verdict 'pass' requires failures==0, errors==0 and executed>0"
    elif verdict == "not_run":
        if failures != 0 or errors != 0 or executed != 0:
            return "test_verdict 'not_run' requires failures==0, errors==0 and executed==0"
    if execution_status in ("preview", "infra_failure"):
        if verdict != "not_run" or any((tests, failures, errors, skipped)):
            return "execution_status %r requires not_run with all counts zero" % execution_status
    return None


def _provenance_reason(prov):
    """Reason string if provenance is not complete/consistent evidence, else None.

    Requires all seven nonblank string fields, a full-40-hex pep_resolved_sha, and —
    when pep_requested_ref is ALSO a full SHA — case-insensitive equality with it."""
    if not isinstance(prov, dict):
        return "provenance is missing or not an object"
    for k in _PROVENANCE_KEYS:
        if not _nonblank_str(prov.get(k)):
            return "provenance field %r must be a nonblank string" % k
    # The summary must carry and validate its OWN producing attempt as a positive
    # decimal string so it can be classified (current/prior/future) against the live
    # aggregation attempt. A nonblank-but-non-numeric caller_run_attempt is malformed.
    if not _is_positive_decimal_str(prov.get("caller_run_attempt")):
        return "provenance caller_run_attempt must be a positive decimal string"
    rs = prov["pep_resolved_sha"]
    if not _FULL_SHA_RE.fullmatch(rs):
        return "pep_resolved_sha must be a full 40-hex SHA"
    rr = prov["pep_requested_ref"]
    if _FULL_SHA_RE.fullmatch(rr) and rr.lower() != rs.lower():
        return "pep_requested_ref is a full SHA but does not equal pep_resolved_sha"
    return None


# --------------------------------------------------------------------------- #
# record classification
# --------------------------------------------------------------------------- #
def _summary_malformed_reason(s):
    """Return a reason string if the atomic summary is structurally unusable, else None.

    A summary is malformed when it is not an object, cannot self-identify (missing or
    non-workflow-safe ``invocation_id`` — a rejected/absent id is "" and fails here),
    carries an out-of-vocabulary status/verdict/mode, OR is missing complete, well-formed
    evidence: a full identity_evidence rung set, a consistent counts object, and a
    complete provenance block (seven nonblank fields, a full pep_resolved_sha, and a
    full-SHA pep_requested_ref that equals it). Incomplete evidence is malformed — never
    silently accepted with {} substituted."""
    if not isinstance(s, dict):
        return "result record is not an object"
    iid = s.get("invocation_id")
    if not (isinstance(iid, str) and _INVOCATION_ID_RE.fullmatch(iid)):
        return "invocation_id missing or not workflow-safe"
    if s.get("execution_status") not in EXECUTION_STATUSES:
        return "execution_status is not a recognized value"
    if s.get("test_verdict") not in TEST_VERDICTS:
        return "test_verdict is not a recognized value"
    if s.get("enforcement_mode") not in ENFORCEMENT_MODES:
        return "enforcement_mode is not a recognized value"
    # Additive field: absent (an older summary) reads as no digest; present, it must be
    # null or exactly what the summarizer emits.
    digest = s.get("installed_package_sha256")
    if not (digest is None or (isinstance(digest, str) and _OBSERVED_SHA256_RE.fullmatch(digest))):
        return "installed_package_sha256 must be null or a lowercase 64-hex digest"
    reason = (_identity_evidence_reason(s.get("identity_evidence"))
              or _counts_reason(s.get("counts"))
              or _provenance_reason(s.get("provenance")))
    if reason:
        return reason
    # counts is now a validated, complete object -> enforce the status/verdict/counts
    # semantics so a record whose own counts contradict its verdict cannot be accepted.
    return _status_verdict_counts_reason(s["execution_status"], s["test_verdict"], s["counts"])


def _evidence(s, reason):
    """Deterministic identifying evidence for an unexpected/duplicate/malformed input.
    A dict record is preserved COMPLETE (every field: counts, identity_evidence, reason,
    provenance, ...) so a fail-closed result is diagnosable; a non-dict input keeps a
    bounded repr."""
    if isinstance(s, dict):
        return {"reason": reason, "record": copy.deepcopy(s)}
    return {"reason": reason, "repr": repr(s)[:200]}


def _unexpected_invocation_id(s):
    """The id to file an unexpected record under (None when it cannot be trusted)."""
    if isinstance(s, dict):
        iid = s.get("invocation_id")
        if isinstance(iid, str) and iid.strip():
            return iid
    return None


# --------------------------------------------------------------------------- #
# provenance binding (matched legs only)
# --------------------------------------------------------------------------- #
def _attempt_context(plan_prov, current_run_attempt):
    """The stable two-key attempt-context block. ``plan_run_attempt`` mirrors the
    top-level provenance run_attempt (the SOURCE plan/capture attempt, nulled when the
    plan value is untrustworthy — never relabelled as the aggregation attempt);
    ``aggregation_run_attempt`` is the validated live aggregation attempt, or null when
    it is not a positive decimal string. Shape is ALWAYS these two keys."""
    prov = plan_prov if isinstance(plan_prov, dict) else {}
    pa = prov.get("run_attempt")
    plan_ra = pa if _valid_plan_provenance_value("run_attempt", pa) else None
    agg_ra = current_run_attempt if _is_positive_decimal_str(current_run_attempt) else None
    return {"plan_run_attempt": plan_ra, "aggregation_run_attempt": agg_ra}


def _stable_binding_errors(iid, prov, plan_prov):
    """Deterministic error strings if a summary's ATTEMPT-STABLE provenance
    (repository/run_id/sha/ref) does not bind to the plan (empty == binds). A summary
    that does not bind is FOREIGN regardless of its attempt. The plan provenance is a
    validated dict (Phase 0b), so only the summary side can be missing/mismatched."""
    errors = []
    for summary_key, plan_key in _STABLE_BINDING:
        pv, sv = _coerce(plan_prov.get(plan_key)), _coerce(prov.get(summary_key))
        if sv == "":
            errors.append("leg %s: cannot bind %s: summary provenance is missing this value"
                          % (iid, summary_key))
        elif sv != pv:
            errors.append("leg %s: %s %r does not match plan provenance %r"
                          % (iid, summary_key, prov.get(summary_key), plan_prov.get(plan_key)))
    return errors


def _matched_cross_consistency(matched):
    """Enforce PEP-ref + enforcement-mode consistency across the CURRENT matched legs
    (never historical records). Returns a deterministic list of structural error
    strings (empty == consistent).

    pep_requested_ref/pep_resolved_sha have no absolute plan value, so they are
    required to agree across matched legs (each is already nonblank, and — when
    requested_ref is a full 40-hex SHA — already required to equal resolved_sha by the
    malformed classification). enforcement_mode must likewise agree across matched legs.
    Attempt-stable binding to the plan is enforced per-record upstream, so only these
    cross-record concerns remain here."""
    errors = []
    if not matched:
        return errors
    requested, resolved, modes = {}, {}, {}
    for iid in sorted(matched):
        prov = matched[iid].get("provenance") or {}
        requested[iid] = prov.get("pep_requested_ref")
        resolved[iid] = prov.get("pep_resolved_sha")
        modes[iid] = matched[iid].get("enforcement_mode")
    if len(set(requested.values())) > 1:
        errors.append("matched summaries disagree on pep_requested_ref: %s"
                      % ", ".join(sorted(set(requested.values()))))
    if len(set(resolved.values())) > 1:
        errors.append("matched summaries disagree on pep_resolved_sha: %s"
                      % ", ".join(sorted(set(resolved.values()))))
    if len(set(modes.values())) > 1:
        errors.append("matched summaries disagree on enforcement_mode: %s"
                      % ", ".join(sorted(str(m) for m in modes.values())))
    return errors


# --------------------------------------------------------------------------- #
# leg construction (one row per EXPECTED invocation)
# --------------------------------------------------------------------------- #
def _package_digest(planned, s):
    """match | mismatch | missing | not_required for one matched leg (module docstring)."""
    if s.get("execution_status") == "preview":
        return "not_required"
    observed = s.get("installed_package_sha256")
    if observed is None:
        return "missing"
    return "match" if observed == planned["package"]["sha256"].lower() else "mismatch"


def _planned_pin_error(entry):
    """Why a plan entry does not carry the planner's pin contract, else None. Every
    certification invocation pins its exact package (L2a): the pin field of its own
    family must equal the planned package's ``<version>-<release>``, and the opposite
    family's field must be "". expected_binary is "" unless the component policy plans
    one. A corrupt entry must never read as "L2a not planned"."""
    # Type first: a JSON array/object family is unhashable and must not reach the lookup.
    family = entry.get("family")
    if not (isinstance(family, str) and family in _PIN_FIELDS):
        return "family %r is not rpm or deb" % (family,)
    pin_key, other_key = _PIN_FIELDS[family]
    pkg = entry["package"]                                   # a dict (checked by the caller)
    version, release = pkg.get("version"), pkg.get("release")
    if not (_nonblank_str(version) and _nonblank_str(release)):
        return "package version and release must be nonblank strings, got %r and %r" % (version, release)
    exact = "%s-%s" % (version, release)
    if entry.get(pin_key) != exact:                          # plain equality: safe for any JSON value
        return "%s must be the planned package's exact pin %r, got %r" % (pin_key, exact, entry.get(pin_key))
    if entry.get(other_key) != "":
        return "%s must be empty for a %s package, got %r" % (other_key, family, entry.get(other_key))
    # The planner emits "" for an unplanned binary version (it blanks whitespace too), so
    # a whitespace-only value is corrupt and must not read as "L2b not planned".
    binary = entry.get("expected_binary")
    if not (isinstance(binary, str) and (binary == "" or binary.strip())):
        return "expected_binary must be \"\" or a nonblank string, got %r" % (binary,)
    return None


def _unproven_identity_rungs(planned, s):
    """The identity rungs this leg's plan requires but its summary did not prove, in rung
    order: l1 and l2a always (every certification invocation carries its exact pin; see
    _planned_pin_error); l2b only when an expected binary version is planned (component
    policy). [] for a preview leg."""
    if s.get("execution_status") == "preview":
        return []
    required = {"l1", "l2a"}
    if _nonblank_str(planned["expected_binary"]):
        required.add("l2b")
    ev = s["identity_evidence"]
    return [r for r in _EVIDENCE_RUNGS if r in required and ev[r] != "proven"]


def _matched_leg(iid, planned, s):
    execution_status = s.get("execution_status")
    digest = _package_digest(planned, s)
    unproven = _unproven_identity_rungs(planned, s)
    reason_code = None
    if digest == "mismatch":
        reason_code = RC_DIGEST_MISMATCH
    elif execution_status == "completed":
        if digest == "missing":
            reason_code = RC_DIGEST_MISSING
        elif unproven:
            reason_code = RC_IDENTITY_UNPROVEN
    if reason_code is not None and execution_status == "completed":
        # Unproven bytes or identity cannot certify, whatever the tests said: the leg is
        # incomplete, and its verdict, counts and failures stay exactly as reported.
        execution_status = "incomplete"
    return {
        "invocation_id": iid,
        "reconciliation": "matched",
        "execution_status": execution_status,
        "test_verdict": s.get("test_verdict"),
        "enforcement_mode": s.get("enforcement_mode"),
        # Complete evidence is guaranteed for a matched record (see malformed
        # classification): copy it verbatim, never substitute {} for it.
        "identity_evidence": copy.deepcopy(s.get("identity_evidence")),
        "counts": copy.deepcopy(s.get("counts")),
        "reason": s.get("reason"),
        "reason_code": reason_code,
        "installed_package_sha256": s.get("installed_package_sha256"),
        "package_digest": digest,
        "unproven_identity_rungs": unproven,
        "provenance": copy.deepcopy(s.get("provenance")),
        "planned_invocation": planned,
    }


def _missing_leg(iid, planned):
    # A missing result may be a leg that failed BEFORE emitting its summary, so it is
    # infra_failure (not merely coverage-only) with a not_run verdict.
    return {
        "invocation_id": iid,
        "reconciliation": "missing",
        "execution_status": "infra_failure",
        "test_verdict": "not_run",
        "enforcement_mode": None,
        "identity_evidence": None,
        "counts": None,
        "reason": "no atomic result was collected for this expected invocation",
        "reason_code": "missing_result",
        # Nothing was observed, so the package proof is not evaluated for this leg.
        "installed_package_sha256": None,
        "package_digest": None,
        "unproven_identity_rungs": None,
        "provenance": None,
        "planned_invocation": planned,
    }


# --------------------------------------------------------------------------- #
# historical (prior-attempt) audit records — never promoted to a leg
# --------------------------------------------------------------------------- #
def _historical_entry(iid, s):
    """Audit record for a bound PRIOR-attempt summary. Preserves enough to audit the
    invocation, its producing attempt, its result and its full provenance. Never
    influences legs, coverage, verdict, or the top-level PEP refs."""
    return {
        "invocation_id": iid,
        "producing_attempt": s["provenance"]["caller_run_attempt"],
        "execution_status": s.get("execution_status"),
        "test_verdict": s.get("test_verdict"),
        "enforcement_mode": s.get("enforcement_mode"),
        "identity_evidence": copy.deepcopy(s.get("identity_evidence")),
        "counts": copy.deepcopy(s.get("counts")),
        "reason": s.get("reason"),
        # Audit only (null when absent: a prior attempt may predate the field).
        "installed_package_sha256": s.get("installed_package_sha256"),
        "provenance": copy.deepcopy(s.get("provenance")),
    }


def _sorted_historical(historical):
    """Byte-deterministic ordering. On the resolved path each (invocation_id,
    producing_attempt) pair is unique (a same-attempt duplicate fails closed), so the
    numeric-attempt key is total; the serialized tie-breaker keeps it total on every
    path regardless of input order."""
    return sorted(historical, key=lambda h: (
        h["invocation_id"], int(h["producing_attempt"]),
        json.dumps(h, sort_keys=True, ensure_ascii=False)))


# --------------------------------------------------------------------------- #
# aggregate axes (kept independent)
# --------------------------------------------------------------------------- #
def _aggregate_execution(legs, n_expected):
    """(execution_status, reason_code). The status is unchanged by package proof:
    missing/infra -> infra_failure; mixed preview or any incomplete leg -> incomplete;
    all preview -> preview; all completed -> completed; zero eligible is its own case.

    The reason is the first that applies, in a fixed order: package_digest_mismatch
    (positive evidence of wrong bytes, so it outranks every absence of evidence) ->
    missing_result -> infra_leg -> mixed_mode -> package_digest_missing ->
    identity_unproven -> leg_incomplete. Leg reason codes are read, never recomputed."""
    if n_expected == 0:
        return "incomplete", "zero_eligible"
    codes = {l["reason_code"] for l in legs}
    has_missing = any(l["reconciliation"] == "missing" for l in legs)
    has_infra_matched = any(
        l["reconciliation"] == "matched" and l["execution_status"] == "infra_failure" for l in legs)
    statuses = [l["execution_status"] for l in legs if l["reconciliation"] == "matched"]
    has_preview = any(st == "preview" for st in statuses)
    mixed = has_preview and any(st != "preview" for st in statuses)
    if has_missing or has_infra_matched:
        status = "infra_failure"
    elif mixed or any(st == "incomplete" for st in statuses):
        status = "incomplete"
    elif has_preview:                                        # all preview
        return "preview", None
    else:
        return "completed", None                             # >=1 leg, all completed
    if RC_DIGEST_MISMATCH in codes:
        return status, RC_DIGEST_MISMATCH
    if has_missing:
        return status, "missing_result"
    if has_infra_matched:
        return status, "infra_leg"
    if mixed:
        return status, "mixed_mode"
    for rc in (RC_DIGEST_MISSING, RC_IDENTITY_UNPROVEN):
        if rc in codes:
            return status, rc
    return status, "leg_incomplete"


def _aggregate_verdict(legs, n_expected):
    """Product verdict, independent of execution/coverage: any real fail -> fail;
    else pass only when every expected invocation matched and every verdict is pass;
    else not_run."""
    if any(l["test_verdict"] == "fail" for l in legs):
        return "fail"
    all_matched = n_expected > 0 and all(l["reconciliation"] == "matched" for l in legs)
    if all_matched and all(l["test_verdict"] == "pass" for l in legs):
        return "pass"
    return "not_run"


def _aggregate_coverage(legs, n_expected, plan_gaps):
    """complete only with >=1 expected invocation, no planner gaps, and every expected
    invocation matched with a COMPLETED result (a completed/fail leg still counts);
    partial when work was expected but a gap or any non-completed leg prevents that;
    none when nothing was planned."""
    if n_expected == 0:
        return "none"
    if plan_gaps:
        return "partial"
    if all(l["reconciliation"] == "matched" and l["execution_status"] == "completed" for l in legs):
        return "complete"
    return "partial"


def _counts(legs, n_expected, plan_gaps, n_unexpected):
    def _n(pred):
        return sum(1 for l in legs if pred(l))
    return {
        "expected_invocations": n_expected,
        "matched": _n(lambda l: l["reconciliation"] == "matched"),
        "missing": _n(lambda l: l["reconciliation"] == "missing"),
        "completed": _n(lambda l: l["execution_status"] == "completed"),
        "incomplete": _n(lambda l: l["execution_status"] == "incomplete"),
        "infra_failure": _n(lambda l: l["execution_status"] == "infra_failure"),
        "preview": _n(lambda l: l["execution_status"] == "preview"),
        "pass": _n(lambda l: l["test_verdict"] == "pass"),
        "fail": _n(lambda l: l["test_verdict"] == "fail"),
        "not_run": _n(lambda l: l["test_verdict"] == "not_run"),
        "planner_gaps": len(plan_gaps),
        "unexpected_results": n_unexpected,
    }


def _zero_counts(n_unexpected=0):
    return {
        "expected_invocations": 0, "matched": 0, "missing": 0,
        "completed": 0, "incomplete": 0, "infra_failure": 0, "preview": 0,
        "pass": 0, "fail": 0, "not_run": 0,
        "planner_gaps": 0, "unexpected_results": n_unexpected,
    }


def _valid_plan_provenance_value(key, v):
    """Whether a plan-provenance value passes the SAME field-specific validation Phase 0b
    applies, so a malformed value is never echoed back as trustworthy (fail-closed output)."""
    if key in ("repository", "ref"):
        return _nonblank_str(v)
    if key == "sha":
        return isinstance(v, str) and _FULL_SHA_RE.fullmatch(v) is not None
    if key in ("run_id", "run_attempt"):
        return _is_positive_decimal_str(v)
    return False


def _provenance_block(plan_prov, pep_requested_ref, pep_resolved_sha):
    """The stable seven-key top-level provenance block. Each of the five source fields is
    retained verbatim ONLY when it passes the same field-specific validation as Phase 0b
    (repository/ref nonblank string, sha full-40-hex, run_id/run_attempt positive decimal
    string), otherwise null — so malformed plan provenance is not echoed back as though it
    were trustworthy, while valid fields are retained independently. The two PEP fields are
    the supplied already-validated common atomic values, or null. Shape is ALWAYS seven keys.
    On the resolved path plan_prov is fully validated, so every field is retained verbatim
    (unchanged behavior)."""
    prov = plan_prov if isinstance(plan_prov, dict) else {}
    block = {}
    for k in _PLAN_PROVENANCE_KEYS:
        v = prov.get(k)
        block[k] = v if _valid_plan_provenance_value(k, v) else None
    block["pep_requested_ref"] = pep_requested_ref
    block["pep_resolved_sha"] = pep_resolved_sha
    return block


def _sorted_unexpected(unexpected):
    # Canonical serialized-evidence tie-breaker: two records sharing kind + id (e.g.
    # conflicting duplicates) order by their full serialized evidence, so reversing
    # the input order yields byte-identical cert-result JSON.
    return sorted(unexpected, key=lambda u: (
        u.get("kind") or "", str(u.get("invocation_id") or ""),
        json.dumps(u.get("evidence"), sort_keys=True, ensure_ascii=False)))


def _fail_closed(errors, plan=None, unexpected=None, current_run_attempt=None):
    """Empty/failed-safe cert-result for a STRUCTURAL validation failure. Echoes the
    plan's provenance/release for traceability; carries the offending records. The
    stable-shape fields (seven-key provenance, two-key attempt_context, empty
    historical_results) are always present."""
    unexpected = list(unexpected or [])
    plan = plan if isinstance(plan, dict) else {}
    rel = plan.get("release")
    return {
        "schema": SCHEMA,
        "result_resolved": False,
        "errors": sorted(errors),
        "reason_code": "validation_failure",
        # Seven-key shape retained even when failing closed: trustworthy plan values are
        # kept, the two PEP fields (and any untrustworthy plan value) are null.
        "provenance": _provenance_block(plan.get("provenance"), None, None),
        "attempt_context": _attempt_context(plan.get("provenance"), current_run_attempt),
        "release": copy.deepcopy(rel) if isinstance(rel, dict) else {},
        "execution_status": "infra_failure",
        "test_verdict": "not_run",
        "coverage_status": "none",
        "counts": _zero_counts(len(unexpected)),
        "legs": [],
        "coverage_gaps": [],
        "historical_results": [],
        "unexpected_results": _sorted_unexpected(unexpected),
    }


# --------------------------------------------------------------------------- #
# pure core
# --------------------------------------------------------------------------- #
def build_cert_result(plan, summaries, current_run_attempt):
    """Reduce a resolved ``pep-invocation-plan/1`` + parsed atomic summaries into a
    deterministic ``cert-result/1`` dict. Never raises for JSON-compatible input.

    ``current_run_attempt`` is the LIVE aggregation attempt (the coordinator's
    ``github.run_attempt`` at aggregate time). It is REQUIRED and validated as a
    positive decimal string — never silently defaulted from the plan — because a
    rerun-failed run carries the plan/capture attempt forward while re-executed legs
    stamp the newer attempt, so the producing attempt must be judged against the live
    value, not the (possibly stale) plan provenance."""
    # ---- Phase 0: input structural validity ----
    # The live aggregation attempt is a required, self-standing input: an invalid one
    # is a validation failure regardless of the plan, and is never inferred from it.
    if not _is_positive_decimal_str(current_run_attempt):
        return _fail_closed(["current_run_attempt must be a positive decimal string"],
                            plan=plan if isinstance(plan, dict) else None,
                            current_run_attempt=current_run_attempt)
    if not isinstance(plan, dict):
        return _fail_closed(["source invocation plan is not an object"],
                            current_run_attempt=current_run_attempt)
    if plan.get("schema") != PLAN_SCHEMA:
        return _fail_closed(["source plan schema must be %r" % PLAN_SCHEMA], plan=plan,
                            current_run_attempt=current_run_attempt)
    if plan.get("plan_resolved") is not True:
        return _fail_closed(["source invocation plan is not resolved"], plan=plan,
                            current_run_attempt=current_run_attempt)
    if not isinstance(summaries, list):
        return _fail_closed(["summaries must be a list"], plan=plan,
                            current_run_attempt=current_run_attempt)

    # ---- Phase 0b: validate every OUTER plan field the reducer directly trusts ----
    # A resolved plan is not re-planned here, but a malformed outer contract must fail
    # closed rather than be silently coerced (a corrupt coverage_gaps must never read
    # as complete coverage; a missing provenance must never bind blank).
    outer = []
    prov = plan.get("provenance")
    if not isinstance(prov, dict):
        outer.append("plan provenance must be an object")
    else:
        # The capture path emits strings: pin each field to its real type so a
        # bool/int/object/array/zero can never masquerade as a run identity.
        if not _nonblank_str(prov.get("repository")):
            outer.append("plan provenance repository must be a nonblank string")
        if not _nonblank_str(prov.get("ref")):
            outer.append("plan provenance ref must be a nonblank string")
        sha = prov.get("sha")
        if not (isinstance(sha, str) and _FULL_SHA_RE.fullmatch(sha)):
            outer.append("plan provenance sha must be a full 40-hex string")
        if not _is_positive_decimal_str(prov.get("run_id")):
            outer.append("plan provenance run_id must be a positive decimal string")
        if not _is_positive_decimal_str(prov.get("run_attempt")):
            outer.append("plan provenance run_attempt must be a positive decimal string")
        # attempt_context invariant: the source plan/capture attempt can never exceed
        # the live aggregation attempt (current_run_attempt was validated in Phase 0).
        pa = prov.get("run_attempt")
        if _is_positive_decimal_str(pa) and int(pa) > int(current_run_attempt):
            outer.append("plan provenance run_attempt %s is greater than current_run_attempt %s"
                         % (pa, current_run_attempt))
    if not isinstance(plan.get("release"), dict):
        outer.append("plan release must be an object")
    if not isinstance(plan.get("coverage_gaps"), list):
        outer.append("plan coverage_gaps must be a list")
    # A resolved plan must carry an empty errors list; a resolved plan bearing errors
    # is internally contradictory and must fail closed.
    plan_errs = plan.get("errors")
    if not isinstance(plan_errs, list):
        outer.append("plan errors must be a list")
    elif plan_errs:
        outer.append("resolved plan must not carry errors")
    matrix = plan.get("matrix")
    include = matrix.get("include") if isinstance(matrix, dict) else None
    if not isinstance(include, list):
        outer.append("plan matrix.include must be a list")
    if outer:
        return _fail_closed(outer, plan=plan, current_run_attempt=current_run_attempt)

    # ---- Phase 1: index EXPECTED invocations (defensive uniqueness/id checks) ----
    expected, dup_expected, plan_errors = {}, set(), []
    for entry in include:
        if not isinstance(entry, dict):
            plan_errors.append("plan matrix.include entry is not an object")
            continue
        iid = entry.get("invocation_id")
        if not (isinstance(iid, str) and _INVOCATION_ID_RE.fullmatch(iid)):
            plan_errors.append("plan invocation_id %r is missing or not workflow-safe" % (iid,))
            continue
        if iid in expected:
            dup_expected.add(iid)
            continue
        # The planned digest every non-preview leg is proven against (the planner always
        # emits one; a plan without it cannot certify any leg).
        pkg = entry.get("package")
        sha = pkg.get("sha256") if isinstance(pkg, dict) else None
        if not (isinstance(sha, str) and _PLANNED_SHA256_RE.fullmatch(sha)):
            plan_errors.append("plan invocation %s package.sha256 is not a 64-hex digest" % iid)
            continue
        pin_error = _planned_pin_error(entry)
        if pin_error:
            plan_errors.append("plan invocation %s %s" % (iid, pin_error))
            continue
        expected[iid] = entry
    if dup_expected:
        plan_errors.append("plan has duplicate invocation_id(s): %s" % ", ".join(sorted(dup_expected)))
    if plan_errors:
        return _fail_closed(plan_errors, plan=plan, current_run_attempt=current_run_attempt)

    # ---- Phase 2: classify each atomic summary (attempt-aware) ----
    # plan provenance is a validated dict with five nonblank fields (Phase 0b).
    plan_prov = plan["provenance"]
    cur_att = int(current_run_attempt)
    unexpected, binding_errors = [], []
    current_cand, prior_cand = {}, {}
    for s in summaries:
        reason = _summary_malformed_reason(s)
        if reason is not None:
            unexpected.append({"kind": "malformed",
                               "invocation_id": _unexpected_invocation_id(s),
                               "evidence": _evidence(s, reason)})
            continue
        iid = s["invocation_id"]
        if iid not in expected:
            unexpected.append({"kind": "unknown",
                               "invocation_id": iid,
                               "evidence": _evidence(s, "invocation_id is not in the plan")})
            continue
        # A well-formed summary carries a complete provenance object (malformed
        # classification guarantees seven nonblank fields + a positive-decimal
        # caller_run_attempt). Bind the ATTEMPT-STABLE identity first: a summary that
        # does not bind is foreign regardless of attempt.
        prov_s = s["provenance"]
        berrs = _stable_binding_errors(iid, prov_s, plan_prov)
        if berrs:
            binding_errors.extend(berrs)
            unexpected.append({"kind": "foreign", "invocation_id": iid,
                               "evidence": _evidence(s, "summary stable provenance does not bind to the plan")})
            continue
        att = int(prov_s["caller_run_attempt"])
        if att > cur_att:
            unexpected.append({"kind": "future", "invocation_id": iid,
                               "evidence": _evidence(
                                   s, "producing attempt %d is greater than the aggregation attempt %d"
                                   % (att, cur_att))})
        elif att == cur_att:
            current_cand.setdefault(iid, []).append(s)
        else:
            prior_cand.setdefault(iid, []).append(s)

    # Current duplicates: >1 CURRENT candidate for one expected id. Retain EVERY
    # candidate's evidence; never silently select one. They do not enter legs.
    for iid in sorted(current_cand):
        group = current_cand[iid]
        if len(group) > 1:
            for s in group:
                unexpected.append({"kind": "duplicate", "invocation_id": iid,
                                   "evidence": _evidence(s, "duplicate current result for an expected invocation")})
    matched = {iid: group[0] for iid, group in current_cand.items() if len(group) == 1}

    # Historical (prior-attempt) records: distinct producing attempts for one
    # invocation are allowed and audited; two records sharing the SAME invocation and
    # producing attempt are ambiguous -> validation failure (both retained as evidence).
    # Group by the NUMERIC attempt value (caller_run_attempt is a validated positive
    # decimal), so different spellings of the same number ("1" and "01") are the SAME
    # producing attempt and collide as a duplicate. Output spelling is left untouched:
    # _historical_entry preserves each record's original caller_run_attempt string.
    historical = []
    for iid in sorted(prior_cand):
        by_attempt = {}
        for s in prior_cand[iid]:
            by_attempt.setdefault(int(s["provenance"]["caller_run_attempt"]), []).append(s)
        for att_num in sorted(by_attempt):
            grp = by_attempt[att_num]
            if len(grp) > 1:
                for s in grp:
                    unexpected.append({"kind": "duplicate_historical", "invocation_id": iid,
                                       "evidence": _evidence(
                                           s, "duplicate historical result for an expected invocation and producing attempt")})
            else:
                historical.append(_historical_entry(iid, grp[0]))

    # ---- Phase 3: structural gate (fail closed on unexpected records or bad binding) ----
    # Foreign binding detail surfaces in errors; cross-consistency applies to CURRENT
    # matched legs only (never historical).
    struct_errors = list(binding_errors) + _matched_cross_consistency(matched)
    kind_counts = {}
    for u in unexpected:
        kind_counts[u["kind"]] = kind_counts.get(u["kind"], 0) + 1
    for kind in ("malformed", "unknown", "foreign", "future", "duplicate", "duplicate_historical"):
        if kind_counts.get(kind):
            struct_errors.append("%d %s result record(s) present" % (kind_counts[kind], kind))
    if struct_errors:
        return _fail_closed(struct_errors, plan=plan, unexpected=unexpected,
                            current_run_attempt=current_run_attempt)

    # ---- Phase 4: build exactly one leg per expected invocation ----
    # A leg is filled ONLY by a current-attempt result; a prior-attempt record is never
    # promoted (an expected id with only history is a missing_result leg).
    legs = []
    for iid in sorted(expected):
        planned = copy.deepcopy(expected[iid])
        s = matched.get(iid)
        legs.append(_matched_leg(iid, planned, s) if s is not None else _missing_leg(iid, planned))

    # ---- Phase 5: aggregate the three independent axes ----
    n_expected = len(expected)
    plan_gaps = plan["coverage_gaps"]          # validated list (Phase 0b)
    execution_status, reason_code = _aggregate_execution(legs, n_expected)
    test_verdict = _aggregate_verdict(legs, n_expected)
    coverage_status = _aggregate_coverage(legs, n_expected, plan_gaps)

    # Top-level PEP requested/resolved come ONLY from current matched summaries (never
    # historical); null when no current result matched. Any matched summary carries the
    # same values by construction (cross-consistency enforced above).
    if matched:
        common = matched[sorted(matched)[0]]["provenance"]
        pep_requested_ref = common.get("pep_requested_ref")
        pep_resolved_sha = common.get("pep_resolved_sha")
    else:
        pep_requested_ref = pep_resolved_sha = None

    return {
        "schema": SCHEMA,
        "result_resolved": True,
        "errors": [],
        "reason_code": reason_code,
        # provenance.run_attempt stays the SOURCE plan/capture attempt; the live
        # aggregation attempt lives only in attempt_context, never relabelling the plan.
        "provenance": _provenance_block(plan_prov, pep_requested_ref, pep_resolved_sha),
        "attempt_context": _attempt_context(plan_prov, current_run_attempt),
        "release": copy.deepcopy(plan.get("release") or {}),
        "execution_status": execution_status,
        "test_verdict": test_verdict,
        "coverage_status": coverage_status,
        "counts": _counts(legs, n_expected, plan_gaps, 0),
        "legs": legs,
        "coverage_gaps": copy.deepcopy(plan_gaps),
        "historical_results": _sorted_historical(historical),
        "unexpected_results": [],
    }


def to_json(result):
    """Canonical, deterministic serialization of a cert-result/1 dict."""
    return json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# impure edge: load already-extracted JSON, then delegate to the pure core
# --------------------------------------------------------------------------- #
def _load_json(path):
    """Parse a JSON file; return its object, or None on any read/parse fault (a None
    summary is classified as malformed by the core; a None plan fails closed)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Aggregate atomic PEP result summaries into a cert-result/1 (local dry-run)")
    ap.add_argument("--plan", required=True, help="resolved pep-invocation-plan/1 JSON file")
    ap.add_argument("--summary", action="append", default=[],
                    help="an already-extracted atomic summary.json (repeatable)")
    ap.add_argument("--summaries-dir", default=None,
                    help="directory of already-extracted *.json atomic summaries")
    ap.add_argument("--current-run-attempt", required=True,
                    help="the live aggregation attempt (the coordinator's github.run_attempt); "
                         "a positive decimal string. Never inferred from the plan.")
    ap.add_argument("--out", default=None, help="write the cert-result here (default: stdout)")
    args = ap.parse_args(argv)

    plan = _load_json(args.plan)
    summaries = [_load_json(p) for p in args.summary]
    if args.summaries_dir:
        for f in sorted(Path(args.summaries_dir).glob("*.json")):
            summaries.append(_load_json(str(f)))

    # The pure core validates current_run_attempt (positive decimal) and fails closed
    # with a truthful cert-result rather than an argparse crash on a bad value.
    result = build_cert_result(plan, summaries, args.current_run_attempt)
    text = to_json(result)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0 if result.get("result_resolved") else 1


if __name__ == "__main__":
    sys.exit(main())
