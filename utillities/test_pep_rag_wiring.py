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


def test_identity_delegates_observations_and_fails_on_problems(monkeypatch):
    req = _int_req(PEP_EXPECTED_DEB="1.0.0~beta1-1.trixie")
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", req)
    monkeypatch.setattr(rag, "client", _Client())
    monkeypatch.setattr(rag.package_management, "query_installed_version",
                        lambda c, pkg: "1.0.0~beta1-1.trixie")
    monkeypatch.setattr(rag.package_management, "query_binary_version",
                        lambda c, path: "Version: 1.0.0")

    captured = {}

    def spy_record(observed, request, out_path, *, binary_missing=False):
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


def test_identity_passes_when_helper_returns_no_problems(monkeypatch):
    req = _int_req()
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", req)
    monkeypatch.setattr(rag, "client", _Client())
    monkeypatch.setattr(rag.package_management, "query_installed_version", lambda c, pkg: "1.0.0")
    monkeypatch.setattr(rag.package_management, "query_binary_version", lambda c, path: "Version: 1.0.0")

    seen = {"called": 0}

    def ok_record(observed, request, out_path, *, binary_missing=False):
        seen["called"] += 1
        return ({"l2a": "not_attempted", "l2b": "not_attempted", "l1": "proven"}, [])

    monkeypatch.setattr(rag.pep_evidence, "record_identity_verdict", ok_record)

    rag.test_rag_identity("c1", "deb", "pgedge-rag-server")   # must NOT raise
    assert seen["called"] == 1                                 # delegation happened


def test_identity_skips_in_standalone(monkeypatch):
    monkeypatch.setattr(rag, "INTEGRATION_REQUEST", None)
    with pytest.raises(pytest.skip.Exception):
        rag.test_rag_identity("c1", "deb", "pgedge-rag-server")
