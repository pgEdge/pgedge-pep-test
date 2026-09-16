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
# The five release-run provenance fields that MUST bind a matched atomic summary
# (caller_* on the summary) to the invocation plan (plan.provenance.*).
_PROVENANCE_BINDING = (
    ("caller_repo", "repository"),
    ("caller_run_id", "run_id"),
    ("caller_run_attempt", "run_attempt"),
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
def _bind_field(iid, label, summary_val, plan_val, errors):
    pv, sv = _coerce(plan_val), _coerce(summary_val)
    if pv == "":
        errors.append("leg %s: cannot bind %s: plan provenance is missing this value" % (iid, label))
        return
    if sv == "":
        errors.append("leg %s: cannot bind %s: summary provenance is missing this value" % (iid, label))
        return
    if sv != pv:
        errors.append("leg %s: %s %r does not match plan provenance %r" % (iid, label, summary_val, plan_val))


def _validate_matched_provenance(matched, plan_prov):
    """Bind every matched summary's caller_* provenance to the plan's release-run
    provenance, and enforce PEP-ref + enforcement-mode consistency across matched
    legs. Returns a deterministic list of structural error strings (empty == valid).

    caller_repo/run_id/run_attempt/sha/ref bind DIRECTLY to plan.provenance (a
    mismatch is validation_failure). pep_requested_ref/pep_resolved_sha have no
    absolute plan value, so they are required nonblank, required to agree across
    matched legs, and — only when requested_ref is a full 40-hex SHA — required to
    equal resolved_sha; proving how an arbitrary ref resolves stays workflow work."""
    errors = []
    if not matched:
        return errors
    # Each matched record is already complete/consistent (malformed classification
    # guarantees seven nonblank provenance fields + a full pep_resolved_sha + a
    # full-SHA pep_requested_ref that equals it), so only CROSS-record concerns remain:
    # binding caller_* to the plan and agreement across matched legs.
    requested, resolved, modes = {}, {}, {}
    for iid in sorted(matched):
        prov = matched[iid].get("provenance") or {}
        for summary_key, plan_key in _PROVENANCE_BINDING:
            _bind_field(iid, summary_key, prov.get(summary_key), plan_prov.get(plan_key), errors)
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
def _matched_leg(iid, planned, s):
    return {
        "invocation_id": iid,
        "reconciliation": "matched",
        "execution_status": s.get("execution_status"),
        "test_verdict": s.get("test_verdict"),
        "enforcement_mode": s.get("enforcement_mode"),
        # Complete evidence is guaranteed for a matched record (see malformed
        # classification): copy it verbatim, never substitute {} for it.
        "identity_evidence": copy.deepcopy(s.get("identity_evidence")),
        "counts": copy.deepcopy(s.get("counts")),
        "reason": s.get("reason"),
        "reason_code": None,
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
        "provenance": None,
        "planned_invocation": planned,
    }


# --------------------------------------------------------------------------- #
# aggregate axes (kept independent)
# --------------------------------------------------------------------------- #
def _aggregate_execution(legs, n_expected):
    """(execution_status, reason_code). Precedence: missing/infra -> mixed preview ->
    leg incomplete -> all preview -> all completed; zero eligible is its own case."""
    if n_expected == 0:
        return "incomplete", "zero_eligible"
    has_missing = any(l["reconciliation"] == "missing" for l in legs)
    has_infra_matched = any(
        l["reconciliation"] == "matched" and l["execution_status"] == "infra_failure" for l in legs)
    if has_missing or has_infra_matched:
        return "infra_failure", ("missing_result" if has_missing else "infra_leg")
    statuses = [l["execution_status"] for l in legs]         # matched legs only now
    has_preview = any(st == "preview" for st in statuses)
    has_non_preview = any(st != "preview" for st in statuses)
    if has_preview and has_non_preview:
        return "incomplete", "mixed_mode"
    if any(st == "incomplete" for st in statuses):
        return "incomplete", "leg_incomplete"
    if has_preview:                                          # all preview
        return "preview", None
    return "completed", None                                 # >=1 leg, all completed


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


def _fail_closed(errors, plan=None, unexpected=None):
    """Empty/failed-safe cert-result for a STRUCTURAL validation failure. Echoes the
    plan's provenance/release for traceability; carries the offending records."""
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
        "release": copy.deepcopy(rel) if isinstance(rel, dict) else {},
        "execution_status": "infra_failure",
        "test_verdict": "not_run",
        "coverage_status": "none",
        "counts": _zero_counts(len(unexpected)),
        "legs": [],
        "coverage_gaps": [],
        "unexpected_results": _sorted_unexpected(unexpected),
    }


# --------------------------------------------------------------------------- #
# pure core
# --------------------------------------------------------------------------- #
def build_cert_result(plan, summaries):
    """Reduce a resolved ``pep-invocation-plan/1`` + parsed atomic summaries into a
    deterministic ``cert-result/1`` dict. Never raises for JSON-compatible input."""
    # ---- Phase 0: input structural validity ----
    if not isinstance(plan, dict):
        return _fail_closed(["source invocation plan is not an object"])
    if plan.get("schema") != PLAN_SCHEMA:
        return _fail_closed(["source plan schema must be %r" % PLAN_SCHEMA], plan=plan)
    if plan.get("plan_resolved") is not True:
        return _fail_closed(["source invocation plan is not resolved"], plan=plan)
    if not isinstance(summaries, list):
        return _fail_closed(["summaries must be a list"], plan=plan)

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
        return _fail_closed(outer, plan=plan)

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
        expected[iid] = entry
    if dup_expected:
        plan_errors.append("plan has duplicate invocation_id(s): %s" % ", ".join(sorted(dup_expected)))
    if plan_errors:
        return _fail_closed(plan_errors, plan=plan)

    # ---- Phase 2: classify each atomic summary ----
    unexpected, candidates = [], {}
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
        candidates.setdefault(iid, []).append(s)

    # Duplicates: >1 candidate for one expected id. Retain EVERY candidate's evidence
    # in unexpected_results; never silently select one. They do not enter legs.
    for iid in sorted(candidates):
        group = candidates[iid]
        if len(group) > 1:
            for s in group:
                unexpected.append({"kind": "duplicate", "invocation_id": iid,
                                   "evidence": _evidence(s, "duplicate result for an expected invocation")})
    matched = {iid: group[0] for iid, group in candidates.items() if len(group) == 1}

    # ---- Phase 3: structural gate (fail closed on unexpected records or bad binding) ----
    # plan provenance is a validated dict with five nonblank fields (Phase 0b).
    plan_prov = plan["provenance"]
    struct_errors = _validate_matched_provenance(matched, plan_prov)
    kind_counts = {}
    for u in unexpected:
        kind_counts[u["kind"]] = kind_counts.get(u["kind"], 0) + 1
    for kind in ("malformed", "unknown", "duplicate"):
        if kind_counts.get(kind):
            struct_errors.append("%d %s result record(s) present" % (kind_counts[kind], kind))
    if struct_errors:
        return _fail_closed(struct_errors, plan=plan, unexpected=unexpected)

    # ---- Phase 4: build exactly one leg per expected invocation ----
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

    # Top-level provenance: five plan fields + the two PEP fields taken from the
    # already-validated common atomic provenance (all matched legs agree), or null when
    # no result matched. Any matched summary carries the same values by construction.
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
        "provenance": _provenance_block(plan_prov, pep_requested_ref, pep_resolved_sha),
        "release": copy.deepcopy(plan.get("release") or {}),
        "execution_status": execution_status,
        "test_verdict": test_verdict,
        "coverage_status": coverage_status,
        "counts": _counts(legs, n_expected, plan_gaps, 0),
        "legs": legs,
        "coverage_gaps": copy.deepcopy(plan_gaps),
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
    ap.add_argument("--out", default=None, help="write the cert-result here (default: stdout)")
    args = ap.parse_args(argv)

    plan = _load_json(args.plan)
    summaries = [_load_json(p) for p in args.summary]
    if args.summaries_dir:
        for f in sorted(Path(args.summaries_dir).glob("*.json")):
            summaries.append(_load_json(str(f)))

    result = build_cert_result(plan, summaries)
    text = to_json(result)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0 if result.get("result_resolved") else 1


if __name__ == "__main__":
    sys.exit(main())
