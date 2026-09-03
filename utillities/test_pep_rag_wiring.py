"""Docker-free tests that exercise the REAL functions in
component-test/test_pep_rag.py (not reimplementations). We load that module with
docker.from_env neutralized, then drive its functions with a fake docker client
and monkeypatched package helpers, controlling INTEGRATION_REQUEST to select the
standalone vs integration branches. This covers the branch/skip/delegation logic
that unit-testing pep_evidence alone cannot reach."""
import importlib.util
import os
import sys

import pytest
import docker


# --- load the real component-test module by path, with the daemon call neutralized ---
_RAG_PATH = os.path.join(os.path.dirname(__file__), "..", "component-test", "test_pep_rag.py")


def _load_rag():
    _orig = docker.from_env
    docker.from_env = lambda *a, **k: None      # module-level `client = docker.from_env()`
    try:
        spec = importlib.util.spec_from_file_location("rag_under_test", _RAG_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["rag_under_test"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        docker.from_env = _orig


rag = _load_rag()


# --- fakes ---
class _FakeContainer:
    status = "running"


class _Client:
    """Minimal docker client: .containers.get(name) -> running container."""
    def __init__(self):
        self._c = _FakeContainer()

        class _Containers:
            def get(_self, name):
                return self._c
        self.containers = _Containers()


def _int_req(**over):
    """A REAL normalized integration request via the production adapter."""
    env = {
        "PEP_INTEGRATION_MODE": "1",
        "PEP_COMPONENT": "rag",
        "PEP_PACKAGE_NAME": "pgedge-rag-server",
        "PEP_CHANNEL": "daily",
        "PEP_EXPECTED_VERSION": "1.0.0",
        "PEP_FAMILY": "deb",
        "PEP_ARCH_FILTER": "amd64",
        "PEP_CONTAINER_ALIAS": "debian13-amd64",
        "PG_MAJOR_VERSION": "17",
    }
    env.update(over)
    return rag.pep_request_env.build_request_from_env(env)


class _Spy:
    def __init__(self, ret):
        self.calls = []
        self._ret = ret

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return self._ret


# --------------------------------------------------------------------------- install


def test_standalone_install_uses_install_package_not_pinned(monkeypatch):
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", None)
    monkeypatch.setattr(rag, "client", _Client())
    ip = _Spy((True, "rhel", "installed"))
    pinned = _Spy((True, "out"))
    monkeypatch.setattr(rag.package_management, "install_package", ip)
    monkeypatch.setattr(rag.package_management, "install_pinned", pinned)

    rag.test_rag_component_install("c1", "rhel", "pgedge-rag-server")

    assert len(ip.calls) == 1                      # legacy path used
    assert pinned.calls == []                      # pinned NOT used in standalone


def test_integration_pinned_uses_install_pinned(monkeypatch):
    req = _int_req(PEP_EXPECTED_DEB="1.0.0~beta1-1.trixie")   # explicit L2a -> pinned
    assert rag.pep_verify.choose_install(req)[0] == "pinned"  # (real decision)
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", req)
    monkeypatch.setattr(rag, "client", _Client())
    pinned = _Spy((True, "ok"))
    ip = _Spy((True, "deb", "installed"))
    monkeypatch.setattr(rag.package_management, "install_pinned", pinned)
    monkeypatch.setattr(rag.package_management, "install_package", ip)

    rag.test_rag_component_install("c1", "deb", "pgedge-rag-server")

    assert len(pinned.calls) == 1
    (a, _k) = pinned.calls[0]
    assert a[1] == "pgedge-rag-server" and a[2] == "1.0.0~beta1-1.trixie"
    assert ip.calls == []                          # latest path NOT used when pinned


def test_integration_pinned_surfaces_install_failure(monkeypatch):
    req = _int_req(PEP_EXPECTED_DEB="1.0.0~beta1-1.trixie")
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", req)
    monkeypatch.setattr(rag, "client", _Client())
    monkeypatch.setattr(rag.package_management, "install_pinned", _Spy((False, "no candidate")))

    with pytest.raises(pytest.fail.Exception) as ei:   # pytest.fail wraps the AssertionError
        rag.test_rag_component_install("c1", "deb", "pgedge-rag-server")
    assert "no candidate" in str(ei.value)             # failure detail surfaced


def test_integration_latest_uses_install_package(monkeypatch):
    req = _int_req()                                   # no expected_deb -> l2a False -> latest
    assert rag.pep_verify.choose_install(req) == ("latest", None)
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", req)
    monkeypatch.setattr(rag, "client", _Client())
    ip = _Spy((True, "deb", "installed"))
    pinned = _Spy((True, "out"))
    monkeypatch.setattr(rag.package_management, "install_package", ip)
    monkeypatch.setattr(rag.package_management, "install_pinned", pinned)

    rag.test_rag_component_install("c1", "deb", "pgedge-rag-server")

    assert len(ip.calls) == 1                          # L1/latest path used
    assert pinned.calls == []


# ----------------------------------------------------------------- legacy skip guards


def test_integration_skips_legacy_version_check(monkeypatch):
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", _int_req())
    with pytest.raises(pytest.skip.Exception):
        rag.test_rag_component_version("c1", "deb", "pgedge-rag-server")


def test_integration_skips_legacy_binary_check(monkeypatch):
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", _int_req())
    with pytest.raises(pytest.skip.Exception):
        rag.test_rag_binary_version("c1", "deb", "pgedge-rag-server")


def test_standalone_does_not_skip_version_check(monkeypatch):
    # In standalone mode the legacy check is NOT skipped by the integration guard;
    # it proceeds and skips only for its own reason (no expected version in env).
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", None)
    monkeypatch.setattr(rag, "rag_version_map", {"pgedge-rag-server": ""})
    with pytest.raises(pytest.skip.Exception) as ei:
        rag.test_rag_component_version("c1", "deb", "pgedge-rag-server")
    assert "No version defined" in str(ei.value)       # legacy reason, not the integration guard


# ------------------------------------------------------------------- identity delegation


def test_identity_delegates_observations_and_fails_on_problems(monkeypatch, tmp_path):
    req = _int_req(PEP_EXPECTED_DEB="1.0.0~beta1-1.trixie")
    inst, _ident = _wire_integration(monkeypatch, tmp_path, req)
    rag.pep_evidence.write_install_evidence(req, "run-A", "pinned", "1.0.0~beta1-1.trixie", inst)
    monkeypatch.setattr(rag.package_management, "query_installed_version",
                        lambda c, pkg: "1.0.0~beta1-1.trixie")
    monkeypatch.setattr(rag.package_management, "query_binary_version",
                        lambda c, path: "Version: 1.0.0")

    captured = {}

    def spy_record(observed, request, out_path, *, binary_missing=False,
                   run_token=None, observed_out=None):
        captured["observed"] = observed
        captured["request"] = request
        captured["binary_missing"] = binary_missing
        return ({"l2a": "not_proven", "l2b": "not_attempted", "l1": "proven"},
                ["identity not proven for attemptable rung(s): ['l2a']"])

    monkeypatch.setattr(rag.pep_evidence, "record_identity_verdict", spy_record)

    with pytest.raises(AssertionError) as ei:
        rag.test_rag_identity("c1", "deb", "pgedge-rag-server")

    # Delegation: the gathered observations were handed to pep_evidence verbatim.
    assert captured["request"] is req
    assert captured["observed"]["deb"] == "1.0.0~beta1-1.trixie"
    assert captured["observed"]["component_version"] == "1.0.0~beta1-1.trixie"
    assert captured["observed"]["binary"] == "Version: 1.0.0"
    assert captured["binary_missing"] is False
    # Failure comes from the helper's problems, surfaced by the test.
    assert "l2a" in str(ei.value)


def test_identity_passes_when_helper_returns_no_problems(monkeypatch, tmp_path):
    req = _int_req()
    inst, _ident = _wire_integration(monkeypatch, tmp_path, req)
    rag.pep_evidence.write_install_evidence(req, "run-A", "latest", None, inst)
    monkeypatch.setattr(rag.package_management, "query_installed_version", lambda c, pkg: "1.0.0")
    monkeypatch.setattr(rag.package_management, "query_binary_version", lambda c, path: "Version: 1.0.0")

    seen = {"called": 0}

    def ok_record(observed, request, out_path, *, binary_missing=False,
                  run_token=None, observed_out=None):
        seen["called"] += 1
        return ({"l2a": "not_attempted", "l2b": "not_attempted", "l1": "proven"}, [])

    monkeypatch.setattr(rag.pep_evidence, "record_identity_verdict", ok_record)

    rag.test_rag_identity("c1", "deb", "pgedge-rag-server")   # must NOT raise
    assert seen["called"] == 1                                 # delegation happened


def test_identity_skips_in_standalone(monkeypatch):
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", None)
    with pytest.raises(pytest.skip.Exception):
        rag.test_rag_identity("c1", "deb", "pgedge-rag-server")


# --------------------------------------------------- Task 9: install-before-identity

import json


def _wire_integration(monkeypatch, tmp_path, req, run_token="run-A"):
    """Common setup: integration request, fake client, tmp evidence paths, token."""
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", req)
    monkeypatch.setattr(rag, "client", _Client())
    inst = str(tmp_path / "install-evidence.json")
    ident = str(tmp_path / "identity-evidence.json")
    monkeypatch.setenv("PEP_INSTALL_OUT", inst)
    monkeypatch.setenv("PEP_IDENTITY_OUT", ident)
    # Route the audit-only observed-identity file to tmp too, so no wiring test
    # writes into the repo's test-logs/.
    monkeypatch.setenv("PEP_OBSERVED_OUT", str(tmp_path / "observed-identity.json"))
    monkeypatch.setenv("PEP_RUN_TOKEN", run_token)
    return inst, ident


def test_install_writes_scope_marker_then_identity_passes(monkeypatch, tmp_path):
    # Full happy path through the REAL functions + REAL pep_evidence/pep_verify:
    # a successful pinned install records the scope marker, and identity then
    # passes the precondition, gathers observations, and proves identity.
    req = _int_req(PEP_EXPECTED_DEB="1.0.0~beta1-1.trixie")
    inst, ident = _wire_integration(monkeypatch, tmp_path, req)
    monkeypatch.setattr(rag.package_management, "install_pinned", _Spy((True, "ok")))

    rag.test_rag_component_install("c1", "deb", "pgedge-rag-server")

    marker = json.loads(open(inst).read())
    assert marker["run_token"] == "run-A" and marker["install_kind"] == "pinned"
    assert marker["install_token"] == "1.0.0~beta1-1.trixie"

    monkeypatch.setattr(rag.package_management, "query_installed_version",
                        lambda c, pkg: "1.0.0~beta1-1.trixie")
    monkeypatch.setattr(rag.package_management, "query_binary_version", lambda c, path: "Version: 1.0.0")

    rag.test_rag_identity("c1", "deb", "pgedge-rag-server")   # must NOT raise
    ev = json.loads(open(ident).read())
    assert set(ev) == {"l2a", "l2b", "l1"} and ev["l2a"] == "proven" and ev["l1"] == "proven"

    # The REAL path passes PEP_OBSERVED_OUT through to record_identity_verdict, so
    # the audit-only observed-identity.json is written with the actual observations
    # (binary as the PARSED token), while identity-evidence.json stays strict above.
    observed = json.loads(open(str(tmp_path / "observed-identity.json")).read())
    assert observed["run_token"] == "run-A"
    assert observed["target"]["package_name"] == "pgedge-rag-server"
    assert observed["observed"] == {
        "package_manager_version": "1.0.0~beta1-1.trixie",
        "binary_version": "1.0.0",                 # parsed from "Version: 1.0.0"
        "component_version": "1.0.0~beta1-1.trixie",
    }


def _assert_precondition_failure(ident_path, excinfo, needle):
    # The failure must persist a STRICT identity file (so the run reads completed/
    # fail, not a masked infra_failure) and name the reason. L1 is not_attempted
    # (identity was never queried), consistent with assert_identity.
    ev = json.loads(open(ident_path).read())
    assert set(ev) == {"l2a", "l2b", "l1"}                 # strict schema preserved
    assert ev["l1"] == "not_attempted"
    assert needle in str(excinfo.value)


def test_identity_fails_when_install_marker_absent(monkeypatch, tmp_path):
    req = _int_req(PEP_EXPECTED_DEB="1.0.0~beta1-1.trixie")
    _inst, ident = _wire_integration(monkeypatch, tmp_path, req)   # no install run -> no marker
    with pytest.raises(pytest.fail.Exception) as ei:
        rag.test_rag_identity("c1", "deb", "pgedge-rag-server")
    _assert_precondition_failure(ident, ei, "absent")


def test_identity_fails_on_stale_run_token(monkeypatch, tmp_path):
    req = _int_req(PEP_EXPECTED_DEB="1.0.0~beta1-1.trixie")
    inst, ident = _wire_integration(monkeypatch, tmp_path, req, run_token="run-NEW")
    # Marker written by a PRIOR run (different token) still sitting in test-logs/.
    rag.pep_evidence.write_install_evidence(req, "run-OLD", "pinned", "1.0.0~beta1-1.trixie", inst)
    with pytest.raises(pytest.fail.Exception) as ei:
        rag.test_rag_identity("c1", "deb", "pgedge-rag-server")
    _assert_precondition_failure(ident, ei, "run_token")


def test_identity_fails_on_target_mismatch(monkeypatch, tmp_path):
    req = _int_req(PEP_EXPECTED_DEB="1.0.0~beta1-1.trixie")           # deb / debian13-amd64
    inst, ident = _wire_integration(monkeypatch, tmp_path, req, run_token="run-A")
    other = _int_req(PEP_FAMILY="rpm", PEP_CONTAINER_ALIAS="rocky9-amd64",
                     PEP_EXPECTED_RPM="1.0.0-1.el9")                  # a DIFFERENT target
    rag.pep_evidence.write_install_evidence(other, "run-A", "pinned", "1.0.0-1.el9", inst)
    with pytest.raises(pytest.fail.Exception) as ei:
        rag.test_rag_identity("c1", "deb", "pgedge-rag-server")
    _assert_precondition_failure(ident, ei, "target")


def test_identity_fails_when_run_token_empty(monkeypatch, tmp_path):
    # A bypassed bridge (no PEP_RUN_TOKEN) must fail safe even if a marker exists
    # with an empty token -- empty-vs-empty must not pass the precondition.
    req = _int_req(PEP_EXPECTED_DEB="1.0.0~beta1-1.trixie")
    inst, ident = _wire_integration(monkeypatch, tmp_path, req, run_token="")
    rag.pep_evidence.write_install_evidence(req, "", "pinned", "1.0.0~beta1-1.trixie", inst)
    with pytest.raises(pytest.fail.Exception) as ei:
        rag.test_rag_identity("c1", "deb", "pgedge-rag-server")
    _assert_precondition_failure(ident, ei, "PEP_RUN_TOKEN")


def test_certification_upgrade_guard_skips_when_upgrade_off(monkeypatch):
    # The bridge forces UPGRADE=false for certification; this is the component-level
    # guard that turns that into an actual skip (no replacement before identity).
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", _int_req())
    monkeypatch.delenv("UPGRADE", raising=False)                     # == "false"
    with pytest.raises(pytest.skip.Exception):
        rag.test_rag_component_upgrade("c1", "deb", "pgedge-rag-server")
