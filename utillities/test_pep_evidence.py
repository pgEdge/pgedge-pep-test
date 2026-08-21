"""Docker-free unit tests for utillities.pep_evidence.record_identity_verdict:
the identity verdict + evidence persistence that drives the integration RAG
identity test. Proves evidence is persisted BEFORE the verdict (so a failing
identity never loses it) and that attemptable rungs drive pass/fail."""
import importlib.util
import json
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "pep_evidence", str(Path(__file__).parent / "pep_evidence.py"))
ev_mod = importlib.util.module_from_spec(_spec)
sys.modules["pep_evidence"] = ev_mod
_spec.loader.exec_module(ev_mod)


def _req(**o):
    base = {"family": "rpm", "expected_version": "1.0.0", "package_name": "pgedge-rag-server",
            "attemptable_now": {"l2a": False, "l2b": False, "l1": True},
            "expected_rpm": None, "expected_deb": None, "expected_binary": None}
    base.update(o)
    return base


def _observed(**o):
    base = {"rpm": None, "deb": None, "binary": None, "component_version": None}
    base.update(o)
    return base


def test_records_and_passes_when_all_proven(tmp_path):
    out = tmp_path / "identity.json"
    r = _req(attemptable_now={"l2a": True, "l2b": False, "l1": True}, expected_rpm="1.0.0-1.el9")
    obs = _observed(rpm="1.0.0-1.el9", component_version="1.0.0-1.el9")
    ev, problems = ev_mod.record_identity_verdict(obs, r, str(out))
    assert problems == []
    assert ev["l2a"] == "proven" and ev["l1"] == "proven"
    assert json.loads(out.read_text())["l2a"] == "proven"   # evidence persisted


def test_persists_evidence_even_on_identity_failure(tmp_path):
    # The key guarantee: a failed identity must NOT lose the evidence.
    out = tmp_path / "identity.json"
    r = _req(attemptable_now={"l2a": True, "l2b": False, "l1": True}, expected_rpm="1.0.0-1.el9")
    obs = _observed(rpm="9.9.9-9.el9", component_version="9.9.9-9.el9")   # mismatch
    ev, problems = ev_mod.record_identity_verdict(obs, r, str(out))
    assert problems, "a mismatch must produce a problem"
    assert out.exists()                                     # persisted despite failure
    assert json.loads(out.read_text())["l2a"] == "not_proven"


def test_binary_missing_with_l2b_attemptable_is_problem(tmp_path):
    out = tmp_path / "identity.json"
    r = _req(attemptable_now={"l2a": False, "l2b": True, "l1": True}, expected_binary="Version: 1.0.0")
    obs = _observed(component_version="1.0.0")
    ev, problems = ev_mod.record_identity_verdict(obs, r, str(out), binary_missing=True)
    assert any("misconfig" in p for p in problems)
    assert out.exists()                                     # evidence still persisted


def test_binary_missing_ok_when_l2b_not_attemptable(tmp_path):
    out = tmp_path / "identity.json"
    r = _req(attemptable_now={"l2a": True, "l2b": False, "l1": True}, expected_rpm="1.0.0-1.el9")
    obs = _observed(rpm="1.0.0-1.el9", component_version="1.0.0")
    ev, problems = ev_mod.record_identity_verdict(obs, r, str(out), binary_missing=True)
    assert problems == []                                   # missing binary is harmless here
    assert ev["l2b"] == "not_attempted"


def test_l1_not_proven_is_problem(tmp_path):
    out = tmp_path / "identity.json"
    r = _req(attemptable_now={"l2a": False, "l2b": False, "l1": True})
    obs = _observed(component_version="2.0.0")              # != expected 1.0.0
    ev, problems = ev_mod.record_identity_verdict(obs, r, str(out))
    assert any("component-version" in p for p in problems)


def test_l1_missing_observation_is_problem(tmp_path):
    # No component_version observed at all -> l1 stays not_attempted -> failure in
    # integration mode (we always expect to observe the component version).
    out = tmp_path / "identity.json"
    r = _req(attemptable_now={"l2a": False, "l2b": False, "l1": True})
    obs = _observed(component_version=None)
    ev, problems = ev_mod.record_identity_verdict(obs, r, str(out))
    assert problems and ev["l1"] == "not_attempted"


def test_non_attemptable_rungs_do_not_fail(tmp_path):
    out = tmp_path / "identity.json"
    r = _req(attemptable_now={"l2a": False, "l2b": False, "l1": True})
    obs = _observed(rpm="whatever", component_version="1.0.0")
    ev, problems = ev_mod.record_identity_verdict(obs, r, str(out))
    assert problems == [] and ev["l2a"] == "not_attempted"


# --------------------------------------------------------------------------- Task 9:
# install-before-identity scope marker + precondition + truthful precondition failure.


def _full_req(**o):
    base = {"component": "rag", "package_name": "pgedge-rag-server", "channel": "daily",
            "expected_version": "1.0.0", "family": "deb", "arch": "amd64", "pg_major": "17",
            "container_alias": "debian13-amd64",
            "attemptable_now": {"l2a": True, "l2b": False, "l1": True}}
    base.update(o)
    return base


def test_build_install_evidence_shape():
    r = _full_req()
    obj = ev_mod.build_install_evidence(r, "tok-1", "pinned", "1.0.0~beta1-1.trixie")
    assert obj["run_token"] == "tok-1" and obj["installed"] is True
    assert obj["install_kind"] == "pinned" and obj["install_token"] == "1.0.0~beta1-1.trixie"
    assert obj["target"]["package_name"] == "pgedge-rag-server" and obj["target"]["family"] == "deb"
    assert obj["expected_version"] == "1.0.0"


def test_precondition_ok_when_matching():
    r = _full_req()
    ev = ev_mod.build_install_evidence(r, "tok-1", "pinned", "1.0.0~beta1-1.trixie")
    assert ev_mod.install_precondition_problems(ev, r, "tok-1") == []


def test_precondition_absent_marker():
    probs = ev_mod.install_precondition_problems(None, _full_req(), "tok-1")
    assert probs and "absent" in probs[0]


def test_precondition_empty_current_token_is_rejected():
    # A missing/empty PEP_RUN_TOKEN must fail safe, even with a matching marker,
    # so an empty-vs-empty token comparison can never pass.
    r = _full_req()
    marker = ev_mod.build_install_evidence(r, "", "pinned", "1.0.0~beta1-1.trixie")
    probs = ev_mod.install_precondition_problems(marker, r, "")
    assert probs and "PEP_RUN_TOKEN" in probs[0]


def test_precondition_empty_token_absent_marker_also_rejected():
    probs = ev_mod.install_precondition_problems(None, _full_req(), "")
    assert probs and "PEP_RUN_TOKEN" in probs[0]


def test_precondition_stale_token():
    r = _full_req()
    ev = ev_mod.build_install_evidence(r, "OLD-RUN", "pinned", "x")
    probs = ev_mod.install_precondition_problems(ev, r, "NEW-RUN")
    assert any("run_token" in p for p in probs)


def test_precondition_target_mismatch():
    r = _full_req()  # deb/debian13
    ev = ev_mod.build_install_evidence(
        _full_req(family="rpm", container_alias="rocky9-amd64"), "tok", "pinned", "x")
    probs = ev_mod.install_precondition_problems(ev, r, "tok")
    assert any("target" in p for p in probs)


def test_precondition_version_mismatch():
    r = _full_req()
    ev = ev_mod.build_install_evidence(_full_req(expected_version="9.9.9"), "tok", "pinned", "x")
    probs = ev_mod.install_precondition_problems(ev, r, "tok")
    assert any("expected_version" in p for p in probs)


def test_record_precondition_failure_matches_assert_identity_zero_obs(tmp_path):
    # Identity was never queried, so the evidence must equal what assert_identity
    # yields from ZERO observations: attemptable l2a -> not_proven, l2b (not
    # attemptable here) -> not_attempted, l1 -> not_attempted (no observation).
    out = tmp_path / "identity.json"
    r = _full_req(attemptable_now={"l2a": True, "l2b": False, "l1": True})
    ev = ev_mod.record_precondition_failure(r, str(out))
    assert set(ev.keys()) == {"l2a", "l2b", "l1"}                 # STRICT schema
    assert ev["l2a"] == "not_proven"
    assert ev["l2b"] == "not_attempted"
    assert ev["l1"] == "not_attempted"                           # reconciled with assert_identity
    on_disk = json.loads(out.read_text())
    assert on_disk == ev                                         # persisted strict, same object


def test_precondition_evidence_passes_strict_summary_validator(tmp_path):
    # The persisted evidence must satisfy pep_result_summary's STRICT {l2a,l2b,l1}
    # validator, so a failed precondition yields completed/fail, never a masked
    # infra_failure from the summarizer rejecting the file.
    prs = _load_summary()
    ev = ev_mod.record_precondition_failure(_full_req(), str(tmp_path / "id.json"))
    prs._validate_identity_evidence(ev)                          # must NOT raise


def _load_summary():
    _s = importlib.util.spec_from_file_location(
        "pep_result_summary", str(Path(__file__).parent / "pep_result_summary.py"))
    prs = importlib.util.module_from_spec(_s)
    _s.loader.exec_module(prs)
    return prs


def test_precondition_failure_reports_completed_fail_end_to_end(tmp_path):
    # End-to-end reporting decision (NOT just the schema validator): a failing JUnit
    # report + the strict precondition-failure identity evidence, run through the
    # REAL summarizer main(), must classify as completed/fail -- observe exits 0
    # (report-only), gate exits 1.
    prs = _load_summary()
    ident = tmp_path / "identity-evidence.json"
    ev = ev_mod.record_precondition_failure(_full_req(), str(ident))     # writes strict file
    junit = tmp_path / "report.xml"
    junit.write_text(
        '<testsuite tests="2" failures="1" errors="0" skipped="0">'
        '<testcase name="test_rag_component_install">'
        '<failure message="pinned install failed"/></testcase>'
        '<testcase name="test_rag_identity">'
        '<failure message="install-before-identity precondition failed"/></testcase>'
        '</testsuite>')
    out = tmp_path / "summary.json"

    rc_obs = prs.main(["--reports", str(junit), "--identity-json", str(ident),
                       "--out", str(out), "--mode", "observe"])
    summ = json.loads(out.read_text())
    assert summ["execution_status"] == "completed"
    assert summ["test_verdict"] == "fail"
    assert summ["identity_evidence"] == ev            # strict precondition evidence surfaced
    assert rc_obs == 0                                # observe: report-only

    rc_gate = prs.main(["--reports", str(junit), "--identity-json", str(ident),
                        "--out", str(out), "--mode", "gate"])
    assert rc_gate == 1                               # gate: fail -> nonzero


def test_write_install_evidence_roundtrip(tmp_path):
    out = tmp_path / "install.json"
    ev_mod.write_install_evidence(_full_req(), "tok", "latest", None, str(out))
    got = ev_mod.load_json_object(str(out))
    assert got["run_token"] == "tok" and got["install_kind"] == "latest" and got["install_token"] is None


def test_load_json_object_absent_returns_none(tmp_path):
    assert ev_mod.load_json_object(str(tmp_path / "nope.json")) is None
