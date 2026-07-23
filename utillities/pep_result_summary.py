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
_EVIDENCE_RUNGS = ("l2a", "l2b", "l1")
_EVIDENCE_VALUES = {"proven", "not_proven", "not_attempted"}


class ManifestSchemaError(ValueError):
    """The report_manifest JSON parsed but has the wrong shape (not a plumbing
    read error, but still an unusable manifest -> treated as infra_failure)."""


class SideFileError(Exception):
    """An optional CLI side file (identity/provenance JSON) is missing, unreadable,
    malformed, or the wrong shape -> treated as infra_failure by main()."""


def _load_optional_json_object(path, label):
    """Load an optional JSON-object side file. Returns None if `path` is falsy.

    Raises SideFileError (never a raw OSError/JSONDecodeError/shape crash) on a
    missing/unreadable/malformed file or a top level that is not a JSON object.
    """
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SideFileError(f"{label} file unreadable or invalid JSON ({e.__class__.__name__})")
    if not isinstance(data, dict):
        raise SideFileError(f"{label} file must contain a JSON object, got {type(data).__name__}")
    return data


def _validate_identity_evidence(data):
    """Semantically validate a supplied identity-evidence object before it can be
    emitted in a result. Requires EXACTLY the rungs l2a/l2b/l1, each holding one of
    proven|not_proven|not_attempted. Extra keys are rejected (STRICT) so no
    unexpected structure leaks into a 'completed' summary. Raises SideFileError on
    any violation -> main() classifies it as infra_failure.
    """
    missing = [k for k in _EVIDENCE_RUNGS if k not in data]
    if missing:
        raise SideFileError(
            f"identity-evidence missing required rung(s): {', '.join(missing)}")
    extra = sorted(k for k in data if k not in _EVIDENCE_RUNGS)
    if extra:
        raise SideFileError(
            f"identity-evidence has unexpected key(s): {', '.join(extra)}")
    for rung in _EVIDENCE_RUNGS:
        val = data[rung]
        if val not in _EVIDENCE_VALUES:
            raise SideFileError(
                f"identity-evidence rung {rung!r} must be one of "
                f"{sorted(_EVIDENCE_VALUES)}, got {val!r}")
    return data


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
        # An explicitly-supplied manifest is authoritative: validate its schema and,
        # on any violation, raise (caller classifies as infra_failure). NEVER fall
        # back to glob discovery here. The `reports` key is REQUIRED ({} is invalid),
        # but an empty `reports` list is valid (-> []).
        data = json.loads(Path(report_manifest).read_text())   # JSONDecodeError -> caller
        if not isinstance(data, dict):
            raise ManifestSchemaError(
                f"manifest top level must be a JSON object, got {type(data).__name__}")
        if "reports" not in data:
            raise ManifestSchemaError("manifest must contain a 'reports' key")
        raw = data["reports"]
        if not isinstance(raw, list):
            raise ManifestSchemaError(
                f"manifest 'reports' must be a list, got {type(raw).__name__}")
        out = []
        for entry in raw:
            if not isinstance(entry, str):
                raise ManifestSchemaError(
                    f"manifest 'reports' entries must be strings, got {type(entry).__name__}")
            if not entry.strip():
                raise ManifestSchemaError("manifest 'reports' entries must be non-empty strings")
            out.append(Path(entry))
        return out
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
    except (OSError, json.JSONDecodeError, ManifestSchemaError) as e:
        return _finish("infra_failure", "not_run", zero,
                       reason=f"report manifest unreadable or invalid ({e.__class__.__name__})")
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

    # Guard the optional side files: a missing/unreadable/malformed/wrong-shape
    # identity or provenance file is a plumbing fault -> infra_failure, never a
    # crash. The summary JSON is still written below so the outcome is recorded.
    try:
        identity = _load_optional_json_object(args.identity_json, "identity-evidence")
        if identity is not None:
            _validate_identity_evidence(identity)
        provenance = _load_optional_json_object(args.provenance_json, "provenance")
        summary, exit_code = build_summary(
            args.xml_dir, mode=args.mode, preview=args.preview,
            identity_evidence=identity, provenance=provenance,
            validation_error=args.validation_error, infra_error=args.infra_error,
            reports=args.reports, report_manifest=args.reports_manifest,
        )
    except SideFileError as e:
        summary, exit_code = build_summary(mode=args.mode, infra_error=str(e))

    Path(args.out).write_text(json.dumps(summary, indent=2))
    print(f"[pep-summary] execution={summary['execution_status']} "
          f"verdict={summary['test_verdict']} mode={args.mode} exit={exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
