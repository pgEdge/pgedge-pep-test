"""Build the multidimensional PEP result from JUnit XML + emit an exit code.

Dimensions (design spec section 4): execution_status (completed | incomplete |
infra_failure | preview), test_verdict (pass | fail | not_run), enforcement_mode
(observe | gate), achieved identity_evidence, and provenance. Report-only policy:
in 'observe' mode every HANDLED outcome exits 0; in 'gate' mode a product failure
or a non-completed run exits non-zero.

Stdlib only -> unit-testable via `pytest utillities/test_pep_result_summary.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_NOT_ATTEMPTED = {"l2a": "not_attempted", "l2b": "not_attempted", "l1": "not_attempted"}


def _resolve_report_paths(xml_dir=None, reports=None, report_manifest=None):
    """Decide WHICH report files belong to THIS execution.

    `test-logs/` is never cleared and accumulates a new `consolidated-<ts>/` dir
    every run, so a recursive glob over it would read STALE reports from previous
    runs (and basename-dedup would then keep an arbitrary/stale copy). The caller
    therefore passes the current run's reports EXPLICITLY:
      * report_manifest -> path to JSON `{"reports": [<abs paths>], ...}` that
        run_pep_tf.sh writes for the run just executed (preferred);
      * reports         -> an explicit list of file paths;
      * xml_dir         -> LAST-RESORT recursive glob, only safe when the dir holds
        a single run (e.g. a fresh CI runner). Documented as ad-hoc.
    """
    if report_manifest is not None:
        data = json.loads(Path(report_manifest).read_text())
        return [Path(p) for p in data.get("reports", [])]
    if reports is not None:
        return [Path(p) for p in reports]
    return sorted(Path(xml_dir).glob("**/*.xml")) if xml_dir is not None else []


def _aggregate_junit(paths):
    """Sum testcase counts across the given report files (this run only).

    Returns (totals, parsed_any, malformed_count). A file that is missing/unreadable
    (OSError), fails to parse (ParseError), OR carries a non-numeric count attribute
    (ValueError) is counted as malformed (not partially summed and not silently
    dropped), so a single bad/absent report never crashes the run. A missing path is
    reachable in practice: run_pep_tf.sh can record an expected JUnit path even when
    pytest died before writing the file.
    """
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    parsed_any = False
    malformed = 0
    seen = set()   # dedupe by basename: run_pep_tf.sh writes each report to BOTH
                   # test-logs/{component}/{env}/ AND test-logs/consolidated-<ts>/,
                   # so the same run's report can appear twice in the manifest/list.
    for f in sorted(paths, key=lambda p: str(p)):
        if f.name in seen:
            continue
        seen.add(f.name)
        try:
            root = ET.parse(f).getroot()
            suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
            file_totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
            for s in suites:
                for k in file_totals:
                    file_totals[k] += int(s.get(k, 0))
        except (OSError, ET.ParseError, ValueError):   # missing/unreadable, bad XML, or bad count
            malformed += 1
            continue
        if suites:                       # valid XML with >=1 <testsuite>
            parsed_any = True
            for k in totals:
                totals[k] += file_totals[k]
    return totals, parsed_any, malformed


def build_summary(xml_dir=None, *, mode="observe", preview=False, identity_evidence=None,
                  provenance=None, validation_error=None, infra_error=None,
                  reports=None, report_manifest=None):
    """Return (summary_dict, exit_code). See module docstring for the policy.

    Outcome classification:
      * preview=True      -> preview / not_run (no install; not a pass or a failure)
      * validation_error  -> incomplete / not_run
      * infra_error       -> infra_failure / not_run
      * no parseable XML  -> incomplete / not_run (flags malformed_reports if any)
      * tests == 0        -> incomplete / not_run ("no tests collected")
      * ANY malformed report present -> incomplete (we cannot claim a complete,
        trustworthy result even if some reports parsed); verdict still reflects
        what parsed, and malformed_reports is flagged. "Malformed" here also covers
        an explicitly-listed path that is missing/unreadable (test run failed to
        emit it) -> incomplete, never a crash.
      * missing/malformed report_manifest FILE -> infra_failure (the reporting
        handoff/plumbing is broken, distinct from "tests produced no reports").
      * failures+errors>0 -> completed / fail
      * some executed     -> completed / pass   (executed = tests - skipped)
      * all skipped       -> completed / not_run ("all tests skipped") — NOT pass
    Gate exit: 0 iff completed & pass; 1 if fail or infra_failure; else 2.
    Observe exit: always 0 (report-only).
    """
    if mode not in ("observe", "gate"):
        raise ValueError(f"mode must be 'observe' or 'gate', got {mode!r}")
    identity = identity_evidence or dict(_NOT_ATTEMPTED)
    zero = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}

    def _finish(execution_status, verdict, counts, reason=None, malformed=0):
        summary = {
            "execution_status": execution_status,
            "test_verdict": verdict,
            "enforcement_mode": mode,
            "identity_evidence": identity,
            "counts": counts,
            "provenance": provenance or {},
        }
        if reason is not None:
            summary["reason"] = reason
        if malformed:
            summary["malformed_reports"] = malformed
        if mode == "observe":
            exit_code = 0
        elif execution_status == "completed" and verdict == "pass":
            exit_code = 0
        elif verdict == "fail" or execution_status == "infra_failure":
            exit_code = 1
        else:
            exit_code = 2
        return summary, exit_code

    if preview:
        return _finish("preview", "not_run", zero, reason="preview mode (no install)")
    if validation_error is not None:
        return _finish("incomplete", "not_run", zero, reason=validation_error)
    if infra_error is not None:
        return _finish("infra_failure", "not_run", zero, reason=infra_error)

    # A missing/malformed manifest FILE is a plumbing failure (we cannot even
    # learn which reports to read) -> infra_failure, not a partial test result.
    # An EMPTY manifest ({"reports": []}) is valid and yields [] (no fallback glob).
    try:
        paths = _resolve_report_paths(
            xml_dir=(Path(xml_dir) if xml_dir is not None else None),
            reports=reports, report_manifest=report_manifest)
    except (OSError, json.JSONDecodeError) as e:
        return _finish("infra_failure", "not_run", zero,
                       reason=f"report manifest unreadable ({e.__class__.__name__})")
    totals, parsed_any, malformed = _aggregate_junit(paths)
    if not parsed_any:
        reason = ("reports listed but unreadable or unparseable" if malformed
                  else "no JUnit reports found")
        return _finish("incomplete", "not_run", totals, reason=reason, malformed=malformed)
    if totals["tests"] == 0:
        return _finish("incomplete", "not_run", totals,
                       reason="no tests collected", malformed=malformed)

    failed = (totals["failures"] + totals["errors"]) > 0
    executed = totals["tests"] - totals["skipped"]
    if failed:
        verdict, reason = "fail", None
    elif executed > 0:
        verdict, reason = "pass", None
    else:
        verdict, reason = "not_run", "all tests skipped"
    # Any unparseable report means we cannot truthfully claim 'completed'.
    status = "completed" if malformed == 0 else "incomplete"
    if malformed and reason is None:
        reason = "some reports unreadable or unparseable; result may be partial"
    return _finish(status, verdict, totals, reason=reason, malformed=malformed)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build PEP multidimensional result summary")
    # Report scoping (prefer the manifest so only THIS run's reports are read;
    # see _resolve_report_paths). Exactly one source is normally supplied.
    ap.add_argument("--reports-manifest", default=None,
                    help="JSON {'reports':[...]} for the run just executed (preferred)")
    ap.add_argument("--reports", nargs="*", default=None, help="explicit report file list")
    ap.add_argument("--xml-dir", default=None,
                    help="LAST-RESORT recursive glob; safe only for a single-run dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="observe", choices=["observe", "gate"])
    ap.add_argument("--preview", action="store_true", help="preview run (no install)")
    ap.add_argument("--validation-error", default=None)
    ap.add_argument("--infra-error", default=None)
    ap.add_argument("--identity-json", default=None, help="path to identity-evidence JSON")
    ap.add_argument("--provenance-json", default=None, help="path to provenance JSON")
    args = ap.parse_args(argv)

    identity = json.loads(Path(args.identity_json).read_text()) if args.identity_json else None
    provenance = json.loads(Path(args.provenance_json).read_text()) if args.provenance_json else None

    summary, exit_code = build_summary(
        args.xml_dir, mode=args.mode, preview=args.preview,
        identity_evidence=identity, provenance=provenance,
        validation_error=args.validation_error, infra_error=args.infra_error,
        reports=args.reports, report_manifest=args.reports_manifest,
    )
    Path(args.out).write_text(json.dumps(summary, indent=2))
    print(f"[pep-summary] execution={summary['execution_status']} "
          f"verdict={summary['test_verdict']} mode={args.mode} exit={exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
