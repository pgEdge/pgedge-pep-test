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
