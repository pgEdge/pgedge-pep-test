#!/usr/bin/env python3
"""Certification enforcement policy: turn a cert-result/1 + requested mode into a
deterministic pep-cert-decision/1 that SEPARATES certification truth from workflow
enforcement.

PURE and DETERMINISTIC: no filesystem/network/clock in the core (``decide``). This
module owns ONLY the observe/gate colour policy. It consumes the THREE independent
axes the reducer already produced (execution_status, test_verdict, coverage_status)
plus ``result_resolved`` and the matched legs' enforcement modes. It re-derives NO
planning or reduction — every axis is read, never recomputed.

The decision keeps four things distinct so a workflow (and a human) can read them
independently:
  * ``certification_state`` -- the PRODUCT truth: ``pass | fail | incomplete | preview``.
    A product failure stays ``fail`` even when observe mode lets the workflow go green;
    a preview is NEVER reported as a pass.
  * ``policy_decision``     -- what the policy does about it: ``allow | report | block``.
  * ``workflow_conclusion`` -- the resulting job colour: ``success | failure``.
  * ``requested_mode``      -- the enforcement mode asked for (``observe | gate``).
Plus a stable ``reason_code`` and the copied aggregate ``axes`` a workflow emits.

Policy precedence (first match wins):
  1. Invalid mode / malformed or wrong-schema result / ``result_resolved != true`` /
     a matched leg whose enforcement_mode differs from the requested mode
     -> incomplete / block / failure.
  2. execution_status ``infra_failure`` or ``incomplete`` (missing results, infra legs,
     zero eligible) -> incomplete / block / failure in BOTH modes.
  3. execution_status ``preview`` -> observe: preview/report/success; gate:
     preview/block/failure. (Never a pass.)
  4. ``completed`` + ``not_run`` -> incomplete / block / failure in both modes.
  5. ``completed`` product failure -> observe: fail/report/success; gate: fail/block/failure.
  6. ``completed`` pass + partial coverage -> observe: incomplete/report/success; gate:
     incomplete/block/failure.
  7. ``completed`` pass + complete coverage -> pass / allow / success in both modes.
Any unrecognised or contradictory top-level axis combination blocks fail-closed.

Exit code (CLI): 0 when the policy allows the workflow to stay green (success), 1 when
it blocks (failure), 2 ONLY for CLI/configuration misuse where no trustworthy decision
can be produced (missing args, an unwritable output). An invalid mode or a malformed
result is NOT misuse -- it yields a fail-closed BLOCK decision (exit 1).

Stdlib only. Offline-testable via ``pytest utillities/test_pep_cert_gate.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

SCHEMA = "pep-cert-decision/1"
RESULT_SCHEMA = "cert-result/1"

# The cert-result/1 vocab (contract mirror; NOT reducer logic — no reduction happens
# here, these only gate which axis values are recognised as well-formed).
_MODES = ("observe", "gate")
_EXECUTION_STATUSES = ("completed", "incomplete", "infra_failure", "preview")
_TEST_VERDICTS = ("pass", "fail", "not_run")
_COVERAGE_STATUSES = ("complete", "partial", "none")

# certification_state vocabulary
ST_PASS, ST_FAIL, ST_INCOMPLETE, ST_PREVIEW = "pass", "fail", "incomplete", "preview"
# policy_decision vocabulary
PD_ALLOW, PD_REPORT, PD_BLOCK = "allow", "report", "block"
# workflow_conclusion vocabulary
WC_SUCCESS, WC_FAILURE = "success", "failure"


def _decision(state, policy, conclusion, reason, mode, axes):
    """Assemble the stable pep-cert-decision/1 document. workflow_conclusion is always
    consistent with policy_decision (allow/report -> success, block -> failure)."""
    return {
        "schema": SCHEMA,
        "certification_state": state,
        "policy_decision": policy,
        "workflow_conclusion": conclusion,
        "requested_mode": mode,
        "reason_code": reason,
        "axes": axes,
    }


def _axes(cert_result):
    """Copy the aggregate axes a workflow surfaces, each null when absent/out-of-vocab.
    Never recomputed — a straight, type-checked read of the reducer's output."""
    d = cert_result if isinstance(cert_result, dict) else {}
    rr = d.get("result_resolved")
    es = d.get("execution_status")
    tv = d.get("test_verdict")
    cov = d.get("coverage_status")
    rc = d.get("reason_code")
    return {
        "result_resolved": rr if isinstance(rr, bool) else None,
        "execution_status": es if es in _EXECUTION_STATUSES else None,
        "test_verdict": tv if tv in _TEST_VERDICTS else None,
        "coverage_status": cov if cov in _COVERAGE_STATUSES else None,
        "reason_code": rc if (rc is None or isinstance(rc, str)) else None,
    }


def _structural_reason(cert_result):
    """Stable reason string if the cert-result is not a trustworthy, resolved
    cert-result/1; else None. For a resolved result this REQUIRES: in-vocab
    execution/verdict/coverage axes; ``errors`` exactly an empty list; ``reason_code``
    null or a string; ``legs`` a list of objects each carrying a ``reconciliation`` of
    exactly ``matched`` or ``missing``. A malformed leg SHAPE is malformed_result; a
    matched leg with the wrong/absent mode is handled separately as mode_mismatch."""
    if not isinstance(cert_result, dict):
        return "malformed_result"
    if cert_result.get("schema") != RESULT_SCHEMA:
        return "wrong_schema"
    if cert_result.get("result_resolved") is not True:
        return "unresolved"
    if cert_result.get("execution_status") not in _EXECUTION_STATUSES:
        return "malformed_result"
    if cert_result.get("test_verdict") not in _TEST_VERDICTS:
        return "malformed_result"
    if cert_result.get("coverage_status") not in _COVERAGE_STATUSES:
        return "malformed_result"
    if cert_result.get("errors") != []:               # a resolved result carries no errors
        return "malformed_result"
    rc = cert_result.get("reason_code")
    if not (rc is None or isinstance(rc, str)):
        return "malformed_result"
    legs = cert_result.get("legs")
    if not isinstance(legs, list):
        return "malformed_result"
    for leg in legs:
        if not isinstance(leg, dict):
            return "malformed_result"
        if leg.get("reconciliation") not in ("matched", "missing"):
            return "malformed_result"
    return None


def _matched_mode_mismatch(cert_result, mode):
    """True if any MATCHED leg's enforcement_mode is not EXACTLY the requested mode --
    a different string, a non-string, null or an absent key all count as a mismatch. A
    missing (synthesized) leg carries a null mode and never triggers this on its own;
    its missing/infra status blocks via the execution axis instead. (Legs are already
    validated dicts with a matched/missing reconciliation by _structural_reason.)"""
    for leg in cert_result.get("legs") or []:
        if isinstance(leg, dict) and leg.get("reconciliation") == "matched":
            if leg.get("enforcement_mode") != mode:
                return True
    return False


def _all_matched_nonempty(legs):
    """True only when there is at least one leg and every leg is matched. Used to reject
    a completed/preview aggregate that carries no legs or any missing leg (the reducer
    never emits those shapes)."""
    return bool(legs) and all(leg.get("reconciliation") == "matched" for leg in legs)


def _incomplete_reason(cert_result):
    """Stable reason for a blocking infra_failure/incomplete result, distinguishing the
    common causes from the reducer's aggregate reason_code."""
    rc = cert_result.get("reason_code")
    if rc == "zero_eligible":
        return "zero_eligible"
    if rc == "missing_result":
        return "missing_result"
    if cert_result.get("execution_status") == "infra_failure":
        return "infra_failure"
    return "execution_incomplete"


# --------------------------------------------------------------------------- #
# pure policy core
# --------------------------------------------------------------------------- #
def decide(cert_result, mode):
    """Reduce a cert-result/1 + requested mode to a deterministic pep-cert-decision/1.
    Never raises for JSON-compatible input; every branch returns a full decision."""
    axes = _axes(cert_result)

    # ---- 1. fail-closed pre-conditions (invalid mode / bad result / mode mismatch) ----
    if mode not in _MODES:
        return _decision(ST_INCOMPLETE, PD_BLOCK, WC_FAILURE, "invalid_mode", mode, axes)
    reason = _structural_reason(cert_result)
    if reason is not None:
        return _decision(ST_INCOMPLETE, PD_BLOCK, WC_FAILURE, reason, mode, axes)
    if _matched_mode_mismatch(cert_result, mode):
        return _decision(ST_INCOMPLETE, PD_BLOCK, WC_FAILURE, "mode_mismatch", mode, axes)

    es = cert_result["execution_status"]
    tv = cert_result["test_verdict"]
    cov = cert_result["coverage_status"]
    legs = cert_result["legs"]

    def _contradictory():
        return _decision(ST_INCOMPLETE, PD_BLOCK, WC_FAILURE, "contradictory_axes", mode, axes)

    # ---- 2. infra / incomplete execution -> block in BOTH modes (missing legs allowed) ----
    if es in ("infra_failure", "incomplete"):
        return _decision(ST_INCOMPLETE, PD_BLOCK, WC_FAILURE, _incomplete_reason(cert_result), mode, axes)

    # ---- contradictory-axis rejection BEFORE the observe/gate policy ----
    # A completed or preview aggregate is what the reducer emits with reason_code null;
    # a non-null reason_code on either is contradictory input.
    if cert_result.get("reason_code") is not None:
        return _contradictory()

    # ---- 3. preview: requires not_run + partial coverage + >=1 leg, all matched ----
    if es == "preview":
        if not (tv == "not_run" and cov == "partial" and _all_matched_nonempty(legs)):
            return _contradictory()                           # e.g. preview+pass, preview+complete/none, no legs
        if mode == "observe":
            return _decision(ST_PREVIEW, PD_REPORT, WC_SUCCESS, "preview", mode, axes)
        return _decision(ST_PREVIEW, PD_BLOCK, WC_FAILURE, "preview", mode, axes)

    # ---- 4-7: completed: requires >=1 leg, all matched, coverage complete|partial ----
    if es == "completed":
        if not _all_matched_nonempty(legs):                   # a missing/absent leg -> reducer would not call it completed
            return _contradictory()
        if tv == "not_run":                                   # 4. nothing executed (explicit red-both)
            return _decision(ST_INCOMPLETE, PD_BLOCK, WC_FAILURE, "not_run", mode, axes)
        if cov not in ("complete", "partial"):                # pass|fail with coverage none is contradictory
            return _contradictory()
        if tv == "fail":                                      # 5. product failure
            if mode == "observe":
                return _decision(ST_FAIL, PD_REPORT, WC_SUCCESS, "product_fail", mode, axes)
            return _decision(ST_FAIL, PD_BLOCK, WC_FAILURE, "product_fail", mode, axes)
        # tv == "pass"
        if cov == "complete":                                 # 7. clean pass
            return _decision(ST_PASS, PD_ALLOW, WC_SUCCESS, "clean_pass", mode, axes)
        # 6. supported pass, partial coverage
        if mode == "observe":
            return _decision(ST_INCOMPLETE, PD_REPORT, WC_SUCCESS, "partial_coverage", mode, axes)
        return _decision(ST_INCOMPLETE, PD_BLOCK, WC_FAILURE, "partial_coverage", mode, axes)

    # ---- fall-through: unrecognised -> block fail-closed ----
    return _contradictory()


def to_json(decision):
    """Canonical, deterministic serialization of a pep-cert-decision/1 dict."""
    return json.dumps(decision, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# impure edge
# --------------------------------------------------------------------------- #
def _load_json(path):
    """Parse a JSON file; return its value, or None on any read/parse fault (a None
    result is treated as malformed by ``decide`` -> a fail-closed BLOCK decision)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_atomic(path, text):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Decide the certification workflow colour from a cert-result/1 (pure policy)")
    ap.add_argument("--result", required=True, help="cert-result/1 JSON file")
    # NOTE: intentionally NOT choices=(...) -- an invalid mode is a fail-closed BLOCK
    # decision (exit 1), not argparse misuse (exit 2).
    ap.add_argument("--mode", required=True, help="requested enforcement mode (observe|gate)")
    ap.add_argument("--out", required=True, help="write the pep-cert-decision/1 here")
    args = ap.parse_args(argv)

    cert_result = _load_json(args.result)           # None -> malformed -> BLOCK
    decision = decide(cert_result, args.mode)
    try:
        _write_atomic(args.out, to_json(decision))
    except OSError:
        sys.stderr.write("failed to write certification decision output\n")
        return 2                                    # config misuse: no decision persisted
    return 0 if decision["workflow_conclusion"] == WC_SUCCESS else 1


if __name__ == "__main__":
    sys.exit(main())
