"""Unit tests for utillities.pep_result_summary."""
import importlib.util
import json
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "pep_result_summary", str(Path(__file__).parent / "pep_result_summary.py")
)
prs = importlib.util.module_from_spec(_spec)
sys.modules["pep_result_summary"] = prs
_spec.loader.exec_module(prs)

_PASS_XML = (
    '<testsuite name="s" tests="3" failures="0" errors="0" skipped="0">'
    '<testcase name="a"/><testcase name="b"/><testcase name="c"/></testsuite>'
)
_FAIL_XML = (
    '<testsuite name="s" tests="3" failures="1" errors="0" skipped="0">'
    '<testcase name="a"/><testcase name="b"><failure/></testcase>'
    '<testcase name="c"/></testsuite>'
)
_SKIP_XML = (
    '<testsuite name="s" tests="2" failures="0" errors="0" skipped="2">'
    '<testcase name="a"><skipped/></testcase>'
    '<testcase name="b"><skipped/></testcase></testsuite>'
)
_ZERO_XML = '<testsuite name="s" tests="0" failures="0" errors="0" skipped="0"></testsuite>'
_SUITES_XML = (
    '<testsuites>'
    '<testsuite name="s1" tests="2" failures="0" errors="0" skipped="0">'
    '<testcase name="a"/><testcase name="b"/></testsuite>'
    '<testsuite name="s2" tests="1" failures="1" errors="0" skipped="0">'
    '<testcase name="c"><failure/></testcase></testsuite>'
    '</testsuites>'
)
_GARBAGE_XML = '<testsuite name="s" tests="oops" failures="0" errors="0" skipped="0"></testsuite>'


def _dir_with(tmp_path, name, body):
    (tmp_path / name).write_text(body)
    return tmp_path


def test_completed_pass_observe_exit0(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    summary, code = prs.build_summary(tmp_path, mode="observe")
    assert summary["execution_status"] == "completed"
    assert summary["test_verdict"] == "pass"
    assert code == 0


def test_completed_fail_observe_exit0_but_verdict_fail(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _FAIL_XML)
    summary, code = prs.build_summary(tmp_path, mode="observe")
    assert summary["test_verdict"] == "fail"
    assert code == 0  # observe never fails the job on product failure


def test_completed_fail_gate_exit1(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _FAIL_XML)
    summary, code = prs.build_summary(tmp_path, mode="gate")
    assert summary["test_verdict"] == "fail"
    assert code == 1


def test_gate_pass_exit0(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    _, code = prs.build_summary(tmp_path, mode="gate")
    assert code == 0


def test_all_skipped_is_not_run_not_pass(tmp_path):
    _dir_with(tmp_path, "report.xml", _SKIP_XML)
    summary, code = prs.build_summary(tmp_path, mode="observe")
    assert summary["execution_status"] == "completed"
    assert summary["test_verdict"] == "not_run"  # must NOT be 'pass'
    assert code == 0
    _, gcode = prs.build_summary(tmp_path, mode="gate")
    assert gcode == 2  # gate: only completed+pass exits 0


def test_zero_tests_is_incomplete(tmp_path):
    _dir_with(tmp_path, "report.xml", _ZERO_XML)
    summary, code = prs.build_summary(tmp_path, mode="observe")
    assert summary["execution_status"] == "incomplete"
    assert summary["test_verdict"] == "not_run"
    assert summary["reason"] == "no tests collected"


def test_no_reports_is_incomplete(tmp_path):
    summary, code = prs.build_summary(tmp_path, mode="observe")
    assert summary["execution_status"] == "incomplete"
    assert summary["test_verdict"] == "not_run"
    assert code == 0


def test_validation_error_populates_all_dimensions(tmp_path):
    summary, code = prs.build_summary(
        tmp_path, mode="observe", validation_error="expected_version is required"
    )
    assert summary["execution_status"] == "incomplete"
    assert summary["test_verdict"] == "not_run"
    assert summary["identity_evidence"] == {
        "l2a": "not_attempted", "l2b": "not_attempted", "l1": "not_attempted"
    }
    assert summary["reason"] == "expected_version is required"
    assert code == 0


def test_infra_failure_observe_exit0(tmp_path):
    summary, code = prs.build_summary(
        tmp_path, mode="observe", infra_error="docker pull failed"
    )
    assert summary["execution_status"] == "infra_failure"
    assert summary["test_verdict"] == "not_run"
    assert code == 0


def test_infra_failure_gate_exit1(tmp_path):
    _, code = prs.build_summary(tmp_path, mode="gate", infra_error="runner lost")
    assert code == 1


def test_only_malformed_is_incomplete_and_flagged(tmp_path):
    _dir_with(tmp_path, "report-bad.xml", "<not-xml")
    summary, code = prs.build_summary(tmp_path, mode="observe")
    assert summary["execution_status"] == "incomplete"
    assert summary["reason"] == "reports listed but unreadable or unparseable"
    assert summary["malformed_reports"] == 1


def test_malformed_alongside_valid_is_incomplete_not_completed(tmp_path):
    # A malformed report means we could not read all results -> cannot claim
    # 'completed'. Verdict still reflects what parsed; malformed is flagged.
    _dir_with(tmp_path, "report-ok.xml", _PASS_XML)
    (tmp_path / "report-bad.xml").write_text("<nope")
    summary, code = prs.build_summary(tmp_path, mode="observe")
    assert summary["execution_status"] == "incomplete"
    assert summary["test_verdict"] == "pass"
    assert summary["malformed_reports"] == 1


def test_preview_is_not_pass_or_fail(tmp_path):
    summary, code = prs.build_summary(tmp_path, mode="observe", preview=True)
    assert summary["execution_status"] == "preview"
    assert summary["test_verdict"] == "not_run"
    assert code == 0


def test_duplicate_report_basename_counted_once(tmp_path):
    # Mirrors run_pep_tf.sh writing the SAME report to component + consolidated dirs.
    (tmp_path / "rag" / "17").mkdir(parents=True)
    (tmp_path / "consolidated-x").mkdir()
    (tmp_path / "rag" / "17" / "report-rpm-rag-17.xml").write_text(_FAIL_XML)
    (tmp_path / "consolidated-x" / "report-rpm-rag-17.xml").write_text(_FAIL_XML)
    summary, _ = prs.build_summary(reports=[
        str(tmp_path / "rag" / "17" / "report-rpm-rag-17.xml"),
        str(tmp_path / "consolidated-x" / "report-rpm-rag-17.xml"),
    ], mode="observe")
    assert summary["counts"]["tests"] == 3   # not 6 — deduped by basename
    assert summary["counts"]["failures"] == 1


def test_stale_reports_from_previous_run_not_counted(tmp_path):
    # test-logs accumulates: a PASS report for THIS run and a stale FAIL report
    # for a previous run share a basename. The manifest lists only this run's
    # report, so the stale FAIL must be ignored (verdict pass, 0 failures).
    cur = tmp_path / "consolidated-new"; cur.mkdir()
    old = tmp_path / "consolidated-old"; old.mkdir()
    (cur / "report-rpm-rag-17.xml").write_text(_PASS_XML)
    (old / "report-rpm-rag-17.xml").write_text(_FAIL_XML)
    manifest = tmp_path / "current-run.json"
    manifest.write_text(json.dumps({"reports": [str(cur / "report-rpm-rag-17.xml")]}))
    summary, _ = prs.build_summary(mode="observe", report_manifest=str(manifest))
    assert summary["test_verdict"] == "pass"
    assert summary["counts"]["failures"] == 0


def test_main_writes_json_and_returns_exit(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _FAIL_XML)
    out = tmp_path / "summary.json"
    code = prs.main([
        "--xml-dir", str(tmp_path), "--out", str(out), "--mode", "gate",
    ])
    data = json.loads(out.read_text())
    assert data["test_verdict"] == "fail"
    assert code == 1


def test_testsuites_wrapper_aggregated(tmp_path):
    _dir_with(tmp_path, "report.xml", _SUITES_XML)
    summary, _ = prs.build_summary(tmp_path, mode="observe")
    assert summary["counts"]["tests"] == 3
    assert summary["counts"]["failures"] == 1
    assert summary["test_verdict"] == "fail"


def test_garbage_numeric_attr_is_malformed_not_crash(tmp_path):
    # A valid-XML report with a non-numeric count must be treated as malformed,
    # never crash the summarizer (observe must still exit 0).
    _dir_with(tmp_path, "report.xml", _GARBAGE_XML)
    summary, code = prs.build_summary(tmp_path, mode="observe")
    assert summary["execution_status"] == "incomplete"
    assert summary["malformed_reports"] == 1
    assert code == 0


def test_missing_report_path_is_incomplete_not_crash(tmp_path):
    # run_pep_tf.sh can record an expected JUnit path even when pytest died before
    # writing it. An explicitly-listed missing path must be handled, not crash.
    missing = tmp_path / "never-written.xml"
    summary, code = prs.build_summary(reports=[str(missing)], mode="observe")
    assert summary["execution_status"] == "incomplete"
    assert summary["test_verdict"] == "not_run"
    assert summary["malformed_reports"] == 1
    assert code == 0                      # observe never fails the job
    # gate: incomplete (not completed&pass, not fail/infra) -> exit 2
    _, gcode = prs.build_summary(reports=[str(missing)], mode="gate")
    assert gcode == 2


def test_empty_manifest_is_incomplete_and_does_not_fall_back(tmp_path):
    # An empty current-run manifest is VALID and yields incomplete/not_run; it must
    # NOT fall back to a historical/stale report even if one is present via xml_dir.
    stale = tmp_path / "consolidated-old"; stale.mkdir()
    (stale / "report-rpm-rag-17.xml").write_text(_FAIL_XML)
    manifest = tmp_path / "current-run.json"
    manifest.write_text(json.dumps({"reports": []}))
    summary, code = prs.build_summary(
        tmp_path, mode="observe", report_manifest=str(manifest))
    assert summary["execution_status"] == "incomplete"
    assert summary["test_verdict"] == "not_run"
    assert summary["counts"]["tests"] == 0      # stale FAIL NOT counted
    assert code == 0


def test_missing_manifest_file_is_infra_failure(tmp_path):
    # A manifest explicitly requested but absent is a plumbing/handoff failure.
    missing_manifest = tmp_path / "nope.json"
    summary, code = prs.build_summary(mode="observe", report_manifest=str(missing_manifest))
    assert summary["execution_status"] == "infra_failure"
    assert summary["test_verdict"] == "not_run"
    assert code == 0                      # observe still exits 0
    _, gcode = prs.build_summary(mode="gate", report_manifest=str(missing_manifest))
    assert gcode == 1                     # gate: infra_failure -> exit 1


def test_malformed_manifest_json_is_infra_failure(tmp_path):
    manifest = tmp_path / "current-run.json"
    manifest.write_text("{ this is not valid json")
    summary, code = prs.build_summary(mode="observe", report_manifest=str(manifest))
    assert summary["execution_status"] == "infra_failure"
    assert summary["test_verdict"] == "not_run"
    assert code == 0
