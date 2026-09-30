"""Unit tests for utillities.pep_result_summary."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

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


# --- manifest SCHEMA validation (valid JSON, wrong shape -> infra_failure) ---

def test_manifest_top_level_array_is_infra_failure(tmp_path):
    m = tmp_path / "current-run.json"; m.write_text("[]")   # valid JSON, not an object
    summary, code = prs.build_summary(mode="observe", report_manifest=str(m))
    assert summary["execution_status"] == "infra_failure"
    assert summary["test_verdict"] == "not_run"
    assert code == 0
    _, gcode = prs.build_summary(mode="gate", report_manifest=str(m))
    assert gcode == 1


def test_manifest_reports_null_is_infra_failure(tmp_path):
    m = tmp_path / "current-run.json"; m.write_text(json.dumps({"reports": None}))
    summary, code = prs.build_summary(mode="observe", report_manifest=str(m))
    assert summary["execution_status"] == "infra_failure"
    assert code == 0


def test_manifest_non_string_entry_is_infra_failure(tmp_path):
    m = tmp_path / "current-run.json"; m.write_text(json.dumps({"reports": [123]}))
    summary, code = prs.build_summary(mode="observe", report_manifest=str(m))
    assert summary["execution_status"] == "infra_failure"
    assert code == 0


# --- CLI (main) side-file guards: identity/provenance JSON -> infra_failure ---

def test_main_missing_identity_file_is_infra_failure(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    out = tmp_path / "summary.json"
    code = prs.main(["--xml-dir", str(tmp_path), "--out", str(out),
                     "--mode", "observe", "--identity-json", str(tmp_path / "nope.json")])
    data = json.loads(out.read_text())          # summary still written
    assert data["execution_status"] == "infra_failure"
    assert data["test_verdict"] == "not_run"
    assert code == 0
    gcode = prs.main(["--xml-dir", str(tmp_path), "--out", str(out),
                      "--mode", "gate", "--identity-json", str(tmp_path / "nope.json")])
    assert gcode == 1


def test_main_malformed_provenance_file_is_infra_failure(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    (tmp_path / "prov.json").write_text("{bad json")
    out = tmp_path / "summary.json"
    code = prs.main(["--xml-dir", str(tmp_path), "--out", str(out),
                     "--mode", "observe", "--provenance-json", str(tmp_path / "prov.json")])
    data = json.loads(out.read_text())
    assert data["execution_status"] == "infra_failure"
    assert code == 0


def test_main_invalid_shape_identity_is_infra_failure(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    (tmp_path / "id.json").write_text("[]")     # valid JSON, wrong shape (not an object)
    out = tmp_path / "summary.json"
    gcode = prs.main(["--xml-dir", str(tmp_path), "--out", str(out),
                      "--mode", "gate", "--identity-json", str(tmp_path / "id.json")])
    data = json.loads(out.read_text())
    assert data["execution_status"] == "infra_failure"
    assert gcode == 1


# --- manifest MUST contain a 'reports' key; empty-string entries invalid ---

def test_manifest_missing_reports_key_is_infra_failure(tmp_path):
    # {} is a valid JSON object but carries no run scope -> infra_failure, NOT an
    # empty (valid) manifest and NOT a fall-back to glob.
    m = tmp_path / "current-run.json"; m.write_text(json.dumps({}))
    summary, code = prs.build_summary(mode="observe", report_manifest=str(m))
    assert summary["execution_status"] == "infra_failure"
    assert summary["test_verdict"] == "not_run"
    assert code == 0
    _, gcode = prs.build_summary(mode="gate", report_manifest=str(m))
    assert gcode == 1


def test_manifest_empty_reports_list_remains_valid(tmp_path):
    # {"reports": []} is still a VALID empty manifest -> incomplete/not_run, not infra.
    m = tmp_path / "current-run.json"; m.write_text(json.dumps({"reports": []}))
    summary, code = prs.build_summary(mode="observe", report_manifest=str(m))
    assert summary["execution_status"] == "incomplete"
    assert summary["test_verdict"] == "not_run"
    assert code == 0


def test_manifest_empty_string_entry_is_infra_failure(tmp_path):
    m = tmp_path / "current-run.json"; m.write_text(json.dumps({"reports": [""]}))
    summary, code = prs.build_summary(mode="observe", report_manifest=str(m))
    assert summary["execution_status"] == "infra_failure"
    assert summary["test_verdict"] == "not_run"
    assert code == 0


# --- identity-evidence SEMANTIC schema (via main): rungs + values, strict keys ---

def _identity_main(tmp_path, id_body, mode="gate"):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    (tmp_path / "id.json").write_text(id_body)
    out = tmp_path / "summary.json"
    code = prs.main(["--xml-dir", str(tmp_path), "--out", str(out),
                     "--mode", mode, "--identity-json", str(tmp_path / "id.json")])
    return json.loads(out.read_text()), code


def test_main_identity_missing_rung_is_infra_failure(tmp_path):
    data, code = _identity_main(tmp_path, json.dumps({"l2a": "proven", "l1": "not_attempted"}))
    assert data["execution_status"] == "infra_failure"    # l2b missing
    assert data["test_verdict"] == "not_run"
    assert code == 1


def test_main_identity_unknown_value_is_infra_failure(tmp_path):
    data, code = _identity_main(
        tmp_path, json.dumps({"l2a": "maybe", "l2b": "not_proven", "l1": "proven"}))
    assert data["execution_status"] == "infra_failure"    # "maybe" not a valid value
    assert code == 1


def test_main_identity_extra_key_is_infra_failure(tmp_path):
    # STRICT extra-key handling: an unexpected key -> infra_failure (not emitted).
    data, code = _identity_main(tmp_path, json.dumps(
        {"l2a": "proven", "l2b": "proven", "l1": "proven", "l3": "proven"}))
    assert data["execution_status"] == "infra_failure"
    assert code == 1


def test_main_identity_array_value_is_infra_failure(tmp_path):
    # A JSON array value is unhashable; membership must be guarded, not crash.
    data, code = _identity_main(tmp_path, json.dumps(
        {"l2a": ["proven"], "l2b": "proven", "l1": "proven"}), mode="observe")
    assert data["execution_status"] == "infra_failure"
    assert data["test_verdict"] == "not_run"
    assert code == 0                                  # observe still exits 0
    gdata, gcode = _identity_main(tmp_path, json.dumps(
        {"l2a": ["proven"], "l2b": "proven", "l1": "proven"}), mode="gate")
    assert gdata["execution_status"] == "infra_failure"   # summary JSON written
    assert gcode == 1


def test_main_identity_object_value_is_infra_failure(tmp_path):
    # A JSON object value is likewise unhashable.
    data, code = _identity_main(tmp_path, json.dumps(
        {"l2a": {"nested": True}, "l2b": "proven", "l1": "proven"}), mode="observe")
    assert data["execution_status"] == "infra_failure"
    assert data["test_verdict"] == "not_run"
    assert code == 0
    gdata, gcode = _identity_main(tmp_path, json.dumps(
        {"l2a": {"nested": True}, "l2b": "proven", "l1": "proven"}), mode="gate")
    assert gdata["execution_status"] == "infra_failure"
    assert gcode == 1


def test_main_valid_identity_reaches_completed_result(tmp_path):
    # A well-formed identity object is accepted and emitted in a completed result.
    data, code = _identity_main(tmp_path, json.dumps(
        {"l2a": "proven", "l2b": "not_proven", "l1": "proven"}), mode="observe")
    assert data["execution_status"] == "completed"
    assert data["test_verdict"] == "pass"
    assert data["identity_evidence"] == {
        "l2a": "proven", "l2b": "not_proven", "l1": "proven"}
    assert code == 0


# --- self-identifying invocation_id: stamped top-level on EVERY branch ---

_VALID_ID = "rag-rocky9-amd64-pg17-0123abcd4567ef89"


def test_invocation_id_stamped_on_completed(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    summary, _ = prs.build_summary(tmp_path, mode="observe", invocation_id=_VALID_ID)
    assert summary["execution_status"] == "completed"
    assert summary["invocation_id"] == _VALID_ID


def test_invocation_id_stamped_on_preview(tmp_path):
    summary, _ = prs.build_summary(tmp_path, mode="observe", preview=True, invocation_id=_VALID_ID)
    assert summary["execution_status"] == "preview"
    assert summary["invocation_id"] == _VALID_ID


def test_invocation_id_stamped_on_validation_error(tmp_path):
    summary, _ = prs.build_summary(
        tmp_path, mode="observe", validation_error="bad input", invocation_id=_VALID_ID)
    assert summary["execution_status"] == "incomplete"
    assert summary["invocation_id"] == _VALID_ID


def test_invocation_id_stamped_on_infra_error(tmp_path):
    summary, _ = prs.build_summary(
        tmp_path, mode="observe", infra_error="runner lost", invocation_id=_VALID_ID)
    assert summary["execution_status"] == "infra_failure"
    assert summary["invocation_id"] == _VALID_ID


def test_invocation_id_stamped_on_incomplete_no_reports(tmp_path):
    summary, _ = prs.build_summary(tmp_path, mode="observe", invocation_id=_VALID_ID)
    assert summary["execution_status"] == "incomplete"
    assert summary["invocation_id"] == _VALID_ID


def test_invocation_id_default_absent_is_empty(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    summary, _ = prs.build_summary(tmp_path, mode="observe")   # default ""
    assert summary["invocation_id"] == ""


def test_unsafe_invocation_id_degrades_to_empty_not_restamped(tmp_path):
    # A value fed straight to the summarizer that is NOT workflow-safe must degrade
    # to "" (mirroring the preflight drop), never leak an unsafe id into a result.
    # Whole-string validation (fullmatch) rejects trailing/embedded newline and CR,
    # which a match()+`$` check would have wrongly accepted.
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    for bad in ["bad/id", "with space", "x" * 65, "tab\tid",
                "trailing\n", "embed\ned", "cr\r", "crlf\r\n"]:
        summary, _ = prs.build_summary(tmp_path, mode="observe", invocation_id=bad)
        assert summary["invocation_id"] == "", repr(bad)


def test_valid_planner_style_invocation_id_preserved(tmp_path):
    # A real planner-generated id (readable prefix + 16-hex digest, 64 chars max)
    # must remain unchanged through the summarizer validator.
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    for good in ["rag-rocky9-amd64-pg17-0123abcd4567ef89", "a", "A.Z_0-9", "x" * 64]:
        summary, _ = prs.build_summary(tmp_path, mode="observe", invocation_id=good)
        assert summary["invocation_id"] == good, repr(good)


def test_main_passes_invocation_id_through_to_summary(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    out = tmp_path / "summary.json"
    code = prs.main(["--xml-dir", str(tmp_path), "--out", str(out),
                     "--mode", "observe", "--invocation-id", _VALID_ID])
    data = json.loads(out.read_text())
    assert data["invocation_id"] == _VALID_ID
    assert code == 0


def test_main_stamps_invocation_id_even_on_sidefile_infra(tmp_path):
    # A side-file plumbing fault still yields a self-identifying result.
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    out = tmp_path / "summary.json"
    code = prs.main(["--xml-dir", str(tmp_path), "--out", str(out), "--mode", "observe",
                     "--identity-json", str(tmp_path / "missing.json"),
                     "--invocation-id", _VALID_ID])
    data = json.loads(out.read_text())
    assert data["execution_status"] == "infra_failure"
    assert data["invocation_id"] == _VALID_ID
    assert code == 0


# --- installed_package_sha256: the verified install's digest, via --install-json ---

_DIGEST = "ab" * 32


def _install_main(tmp_path, body, *, extra=(), mode="observe"):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    inst = tmp_path / "install-evidence.json"
    if body is not None:
        inst.write_text(body if isinstance(body, str) else json.dumps(body))
    out = tmp_path / "summary.json"
    code = prs.main(["--xml-dir", str(tmp_path), "--out", str(out), "--mode", mode,
                     "--install-json", str(inst), *extra])
    return json.loads(out.read_text()), code


def test_install_json_digest_is_carried_without_changing_the_result(tmp_path):
    data, code = _install_main(tmp_path, {"installed": True, "install_kind": "pinned",
                                          "installed_sha256": _DIGEST})
    assert data["installed_package_sha256"] == _DIGEST
    assert (data["execution_status"], data["test_verdict"], code) == ("completed", "pass", 0)


@pytest.mark.parametrize("body", [
    {"installed": True, "install_kind": "latest", "installed_sha256": None},
    {"installed": True, "install_kind": "pinned"},                  # no digest recorded
])
def test_install_json_without_a_digest_yields_null(tmp_path, body):
    data, _ = _install_main(tmp_path, body)
    assert data["installed_package_sha256"] is None and data["execution_status"] == "completed"


def test_no_install_json_yields_null_digest(tmp_path):
    _dir_with(tmp_path, "report-rpm-rag-17.xml", _PASS_XML)
    summary, _ = prs.build_summary(tmp_path, mode="observe")
    assert summary["installed_package_sha256"] is None


@pytest.mark.parametrize("bad", ["AB" * 32, "ab" * 31, 5, ["ab" * 32], ""])
def test_malformed_install_digest_is_infra_failure(tmp_path, bad):
    data, code = _install_main(tmp_path, {"installed_sha256": bad}, mode="gate")
    assert data["execution_status"] == "infra_failure" and code == 1
    assert "installed_sha256" in data["reason"] and data["installed_package_sha256"] is None


@pytest.mark.parametrize("body", [None, "{not json", "[1, 2]"])
def test_missing_or_unreadable_install_json_is_infra_failure(tmp_path, body):
    data, _ = _install_main(tmp_path, body)
    assert data["execution_status"] == "infra_failure"
    assert "install-evidence" in data["reason"]


@pytest.mark.parametrize("kw", [{"preview": True}, {"validation_error": "bad"}, {"infra_error": "boom"}])
def test_digest_is_stamped_on_every_branch(tmp_path, kw):
    summary, _ = prs.build_summary(tmp_path, mode="observe", installed_package_sha256=_DIGEST, **kw)
    assert summary["installed_package_sha256"] == _DIGEST
