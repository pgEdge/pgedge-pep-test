"""Docker-free tests that exercise the REAL functions in component-test/test_pep_spock.py.

The module is loaded by path with docker.from_env neutralized and with the environment of the
case under test (the spock series, platform filter and integration request are decided at import
time), then driven with a fake docker client/container and spied package helpers -- the same
approach as test_pep_rag_wiring.py. Covers: standalone SPOCK_MAJOR behaviour preserved; the
integration series coming from the validated request (v5 and v6), never from SPOCK_MAJOR or its
default; companions installed before the pinned spock package; only the requested spock package
installed; clear prerequisite and install failures; the identity step; and both nodes running the
requested PG major and spock release.
"""
import importlib.util
import itertools
import json
import os
import sys

import docker
import pytest

_SPOCK_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "component-test", "test_pep_spock.py")
_counter = itertools.count()

_V5_RPM = {"PEP_INTEGRATION_MODE": "1", "PEP_COMPONENT": "spock", "PEP_PACKAGE_NAME": "pgedge-spock50_17",
           "PEP_CHANNEL": "staging", "PEP_EXPECTED_VERSION": "5.0.13", "PEP_FAMILY": "rpm",
           "PEP_ARCH_FILTER": "amd64", "PEP_CONTAINER_ALIAS": "rocky9-amd64", "PG_MAJOR_VERSION": "17",
           "PEP_EXPECTED_RPM": "5.0.13-1.el9", "PEP_EXPECTED_BUILDNUM": "1",
           "REPO": "staging", "CONTAINERS": "rocky9-amd64", "DEB_CONTAINERS": "debian13-amd64"}
_V6_DEB = dict(_V5_RPM, PEP_PACKAGE_NAME="pgedge-postgresql-17-spock60", PEP_EXPECTED_VERSION="6.0.0",
               PEP_FAMILY="deb", PEP_CONTAINER_ALIAS="debian13-amd64", PEP_EXPECTED_RPM=None,
               PEP_EXPECTED_DEB="6.0.0-1.trixie")
_PEP_KEYS = sorted(set(_V5_RPM) | set(_V6_DEB) | {"SPOCK_MAJOR", "PLATFORM_FILTER", "PEP_EXPECTED_DEB"})


def load(monkeypatch, env):
    """Load a fresh copy of the spock component test under exactly ``env`` (None values unset)."""
    for k in _PEP_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    orig = docker.from_env
    docker.from_env = lambda *a, **k: None
    try:
        name = "spock_under_test_%d" % next(_counter)
        spec = importlib.util.spec_from_file_location(name, _SPOCK_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        docker.from_env = orig


class _Container:
    """A running container whose exec_run answers from ``respond(cmd) -> (exit_code, text)``."""
    status = "running"

    def __init__(self, respond):
        self.respond, self.cmds = respond, []

    def exec_run(self, cmd, user=None):
        self.cmds.append(cmd)
        ec, text = self.respond(cmd)
        return ec, text.encode()


class _Client:
    def __init__(self, container):
        class _Containers:
            def get(_self, name):
                return container
        self.containers = _Containers()


class _Spy:
    def __init__(self, ret, log=None, label=None):
        self.calls, self._ret, self._log, self._label = [], ret, log, label

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        if self._log is not None:
            self._log.append(self._label)
        if isinstance(self._ret, Exception):
            raise self._ret
        return self._ret


def _pkg_list(*names):
    """exec_run responder for the package-manager listing used by installed_spock_packages."""
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qa"]:
            return 0, "\n".join(names) + "\n"
        if cmd[0] == "dpkg-query" and cmd[1] == "-W":
            return 0, "".join("%s install ok installed\n" % n for n in names)
        return 0, ""
    return respond


# --------------------------------------------------------------------------- series selection
def test_standalone_keeps_spock_major_and_its_default(monkeypatch):
    m = load(monkeypatch, {"PG_MAJOR_VERSION": "17", "SPOCK_MAJOR": "60"})
    assert m.INTEGRATION_REQUEST is None and m.spock_major == "60"
    assert m.get_spock_pkg_name("rhel") == "pgedge-spock60_17"
    assert m.get_spock_packages("deb") == ["pgedge-postgresql-17-spock60", "pgedge-postgresql-17"]
    m = load(monkeypatch, {"PG_MAJOR_VERSION": "17"})
    assert m.spock_major == "50" and m.get_spock_pkg_name("deb") == "pgedge-postgresql-17-spock50"


@pytest.mark.parametrize("env,series,family,containers", [
    (dict(_V5_RPM, SPOCK_MAJOR="60"), "50", "rpm", [("rocky9-amd64", "rhel")]),
    (dict(_V5_RPM, SPOCK_MAJOR=None), "50", "rpm", [("rocky9-amd64", "rhel")]),
    (dict(_V6_DEB, SPOCK_MAJOR="50"), "60", "deb", [("debian13-amd64", "deb")]),
    (dict(_V6_DEB, SPOCK_MAJOR=None, PLATFORM_FILTER="rpm"), "60", "deb", [("debian13-amd64", "deb")]),
], ids=["v5-rpm-env-60", "v5-rpm-no-env", "v6-deb-env-50", "v6-deb-default-and-wrong-filter"])
def test_integration_series_comes_from_the_request_never_spock_major(monkeypatch, env, series, family, containers):
    m = load(monkeypatch, env)
    assert m.INTEGRATION_PROBLEM is None
    assert m.INTEGRATION_SERIES == series and m.spock_major == series
    assert m.get_spock_pkg_name("rhel") == m.get_spock_pkg_name("deb") == env["PEP_PACKAGE_NAME"]
    assert m.all_containers == containers                   # the request family selects the platform


def test_integration_series_backstop_names_each_problem(monkeypatch):
    m = load(monkeypatch, _V5_RPM)
    req = dict(m.INTEGRATION_REQUEST)
    assert m._integration_series(req, "17") == ("50", None)
    assert "PG_MAJOR_VERSION=16" in m._integration_series(req, "16")[1]
    assert "pg_major_mismatch" in m._integration_series(dict(req, package_name="pgedge-spock50_16"), "17")[1]
    assert "series_mismatch" in m._integration_series(dict(req, expected_version="6.0.0"), "17")[1]
    assert "package_family_mismatch" in m._integration_series(dict(req, family="deb"), "17")[1]
    assert "not the PG-coupled spock" in m._integration_series(dict(req, component="rag"), "17")[1]
    assert m._integration_series(None, "17") == (None, None)


def test_a_mismatched_request_is_rejected_at_load_before_any_install(monkeypatch):
    with pytest.raises(Exception, match="series_mismatch"):
        load(monkeypatch, dict(_V5_RPM, PEP_PACKAGE_NAME="pgedge-spock60_17"))


# --------------------------------------------------------------------------- install
def _wire(monkeypatch, tmp_path, m, container):
    monkeypatch.setattr(m, "client", _Client(container))
    for k, f in (("PEP_INSTALL_OUT", "install-evidence.json"), ("PEP_IDENTITY_OUT", "identity-evidence.json"),
                 ("PEP_OBSERVED_OUT", "observed-identity.json"), ("PEP_PREREQ_OUT", "prerequisite-versions.json")):
        monkeypatch.setenv(k, str(tmp_path / f))
    monkeypatch.setenv("PEP_RUN_TOKEN", "run-A")


def test_companions_install_first_then_the_pinned_request_and_versions_are_recorded(monkeypatch, tmp_path):
    m = load(monkeypatch, _V5_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-spock50_17", "pgedge-postgresql17-server")))
    order = []
    ip = _Spy((True, "rhel", "installed"), order, "companions")
    pinned = _Spy((True, "ok", "f" * 64), order, "pinned")
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    monkeypatch.setattr(m.package_management, "query_installed_version",
                        lambda c, n: {"pgedge-spock50_17": "5.0.13-1.el9"}.get(n, "17.11-1PGEDGE.el9"))

    m.test_install_spock_packages("rocky9-amd64", "rhel")

    assert order == ["companions", "pinned"]
    assert ip.calls[0][1]["package_name"] == ["pgedge-postgresql17-server", "pgedge-postgresql17-contrib"]
    assert pinned.calls[0][0][1:] == ("pgedge-spock50_17", "5.0.13-1.el9")
    marker = json.loads((tmp_path / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "f" * 64
    prereq = json.loads((tmp_path / "prerequisite-versions.json").read_text())
    assert prereq == {"channel": "staging", "pg_major": "17", "requested_package": "pgedge-spock50_17",
                      "installed_versions": {"pgedge-postgresql17-server": "17.11-1PGEDGE.el9",
                                             "pgedge-postgresql17-contrib": "17.11-1PGEDGE.el9",
                                             "pgedge-spock50_17": "5.0.13-1.el9"}}


def test_deb_companion_is_the_server_package_for_the_requested_major(monkeypatch, tmp_path):
    m = load(monkeypatch, _V6_DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-postgresql-17-spock60", "pgedge-postgresql-17")))
    ip = _Spy((True, "debian", "installed"))
    pinned = _Spy((True, "ok", "e" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    m.test_install_spock_packages("debian13-amd64", "deb")
    assert ip.calls[0][1]["package_name"] == ["pgedge-postgresql-17"]
    assert pinned.calls[0][0][1:] == ("pgedge-postgresql-17-spock60", "6.0.0-1.trixie")


def test_missing_prerequisite_is_reported_as_such_and_spock_is_not_installed(monkeypatch, tmp_path):
    m = load(monkeypatch, _V5_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    pinned = _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package",
                        _Spy(Exception("Failed to install pgedge-postgresql17-server: No match")))
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    with pytest.raises(pytest.fail.Exception, match="PostgreSQL 17 prerequisite unavailable from the 'staging' channel"):
        m.test_install_spock_packages("rocky9-amd64", "rhel")
    assert pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_failed_pinned_install_fails_loudly_without_install_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _V5_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((False, "checksum mismatch", None)))
    with pytest.raises(AssertionError, match="pinned install of pgedge-spock50_17=5.0.13-1.el9 failed: checksum mismatch"):
        m.test_install_spock_packages("rocky9-amd64", "rhel")
    assert not (tmp_path / "install-evidence.json").exists()


@pytest.mark.parametrize("present", [
    ("pgedge-spock50_17", "pgedge-spock60_17"),           # another series beside the request
    ("pgedge-spock50_17", "pgedge-spock50_16"),           # another PG major beside the request
    ("pgedge-spock60_17",),                               # the request replaced
], ids=["other-series", "other-pg", "replaced"])
def test_any_spock_package_other_than_the_request_fails_the_install_step(monkeypatch, tmp_path, present):
    m = load(monkeypatch, _V5_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(*present, "pgedge-spock50_17-llvmjit")))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((True, "ok", "f" * 64)))
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    with pytest.raises(AssertionError, match="expected exactly the requested spock package"):
        m.test_install_spock_packages("rocky9-amd64", "rhel")


def test_install_step_refuses_a_request_the_backstop_rejects(monkeypatch, tmp_path):
    m = load(monkeypatch, _V5_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m, "INTEGRATION_PROBLEM", "request PG17 disagrees with PG_MAJOR_VERSION=16")
    ip = _Spy((True, "rhel", "installed"))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    with pytest.raises(AssertionError, match="cannot be served"):
        m.test_install_spock_packages("rocky9-amd64", "rhel")
    assert ip.calls == []


def test_standalone_install_is_unchanged(monkeypatch):
    m = load(monkeypatch, {"PG_MAJOR_VERSION": "17", "SPOCK_MAJOR": "50"})
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    ip = _Spy((True, "rhel", "installed"))
    pinned = _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    m.test_install_spock_packages("rocky9-amd64", "rhel")
    assert pinned.calls == []
    assert ip.calls[0][1]["package_name"] == ["pgedge-spock50_17", "pgedge-postgresql17-contrib"]


# --------------------------------------------------------------------------- identity
@pytest.mark.parametrize("observed,ok", [("5.0.13-1.el9", True), ("5.0.12-1.el9", False),
                                         ("5.0.13-rc1_1.el9", False)], ids=["exact", "older", "rc"])
def test_identity_proves_the_exact_requested_version(monkeypatch, tmp_path, observed, ok):
    m = load(monkeypatch, _V5_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    m.pep_integration_mode.pep_evidence.write_install_evidence(
        m.INTEGRATION_REQUEST, "run-A", "pinned", "5.0.13-1.el9", str(tmp_path / "install-evidence.json"),
        installed_sha256="f" * 64)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: observed)
    if ok:
        m.test_spock_identity("rocky9-amd64", "rhel")
    else:
        with pytest.raises(AssertionError, match="not proven"):
            m.test_spock_identity("rocky9-amd64", "rhel")
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")


def test_identity_without_install_evidence_fails_with_persisted_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _V5_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    with pytest.raises(pytest.fail.Exception, match="install-before-identity precondition failed"):
        m.test_spock_identity("rocky9-amd64", "rhel")
    assert (tmp_path / "identity-evidence.json").exists()


def test_identity_and_version_steps_by_mode(monkeypatch):
    m = load(monkeypatch, {"PG_MAJOR_VERSION": "17"})
    with pytest.raises(pytest.skip.Exception):
        m.test_spock_identity("rocky9-amd64", "rhel")
    m = load(monkeypatch, _V5_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    with pytest.raises(pytest.skip.Exception, match="test_spock_identity"):
        m.test_verify_spock_pkg_version("rocky9-amd64", "rhel")


# --------------------------------------------------------------------------- both nodes
def _nodes(owner="pgedge-spock50_17", versions=("170011", "170011"), ext=("5.0.13", "5.0.13"), deb=False):
    ports = {"5431": 0, "5432": 1}

    def respond(cmd):
        if cmd[0] == "psql":
            port, sql = cmd[cmd.index("-p") + 1], cmd[-1]
            if "PKGLIBDIR" in sql:
                return 0, "/usr/pgsql-17/lib\n"
            if "server_version_num" in sql:
                return 0, versions[ports[port]] + "\n"
            if "extversion" in sql:
                return 0, ext[ports[port]] + "\n"
        if cmd[:2] == ["rpm", "-qf"]:
            return 0, owner
        if cmd[:2] == ["dpkg-query", "-S"]:
            return 0, "%s: %s\n" % (owner, cmd[2])
        return 1, "unexpected %r" % (cmd,)
    return respond


def test_both_nodes_run_the_requested_pg_major_and_spock_release(monkeypatch):
    m = load(monkeypatch, _V5_RPM)
    c = _Container(_nodes())
    m.verify_nodes_use_requested_spock(c, "rhel", m.get_container_config("rhel"))
    assert sum(1 for cmd in c.cmds if cmd[0] == "psql" and "extversion" in cmd[-1]) == 2
    m6 = load(monkeypatch, _V6_DEB)
    m6.verify_nodes_use_requested_spock(_Container(_nodes(owner="pgedge-postgresql-17-spock60",
                                                          ext=("6.0.0", "6.0.0"))),
                                        "deb", m6.get_container_config("deb"))


@pytest.mark.parametrize("over,msg", [
    ({"owner": "pgedge-spock60_17"}, "not the requested"),
    ({"versions": ("170011", "160004")}, "n2 runs PostgreSQL 160004"),
    ({"ext": ("5.0.13", "5.0.12")}, "n2: spock extension version is '5.0.12'"),
    ({"ext": ("6.0.0", "5.0.13")}, "n1: spock extension version is '6.0.0'"),
], ids=["library-owner", "pg-major", "ext-version", "ext-series"])
def test_node_checks_reject_a_wrong_library_pg_major_or_extension(monkeypatch, over, msg):
    m = load(monkeypatch, _V5_RPM)
    with pytest.raises(AssertionError, match=msg):
        m.verify_nodes_use_requested_spock(_Container(_nodes(**over)), "rhel", m.get_container_config("rhel"))


def test_component_tests_share_the_integration_helper_instances(monkeypatch):
    # Patching package_management / pep_evidence must reach the shared install + identity steps.
    m = load(monkeypatch, _V5_RPM)
    assert m.pep_request_env is m.pep_integration_mode.pep_request_env
    assert m.package_management is m.pep_integration_mode.package_management
    assert m._pp is sys.modules["pep_package_policy"]


# --------------------------------------------------------------------------- bundled-file reference
_NO_REFERENCE = Exception("No expected file found for pgedge-spock50_17 at /x/expected-output/rpm (checked: /x/a)")
_STANDALONE = {"PG_MAJOR_VERSION": "17"}


def _bundled_outcome(monkeypatch, env, verify):
    """Run the real test_verify_bundled_files and report ("passed"|"skipped"|"failed", message).
    Recording the outcome keeps an unexpected skip from being reported as a skipped test."""
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    monkeypatch.setattr(m.file_management, "verify_bundled_files", verify)
    try:
        m.test_verify_bundled_files("rocky9-amd64", "rhel")
    except pytest.skip.Exception as e:
        return "skipped", str(e)
    except pytest.fail.Exception as e:
        return "failed", str(e)
    return "passed", ""


def _raises(exc):
    def verify(**kwargs):
        raise exc
    return verify


def test_integration_mode_fails_when_the_bundled_file_reference_is_missing(monkeypatch):
    # Skips are reported separately, but the overall verdict can still pass with this check skipped.
    outcome, msg = _bundled_outcome(monkeypatch, _V5_RPM, _raises(_NO_REFERENCE))
    assert outcome == "failed" and "No expected file found for pgedge-spock50_17" in msg


def test_standalone_still_skips_when_the_bundled_file_reference_is_missing(monkeypatch):
    outcome, msg = _bundled_outcome(monkeypatch, _STANDALONE, _raises(_NO_REFERENCE))
    assert outcome == "skipped" and "No expected file found" in msg


@pytest.mark.parametrize("env", [_V5_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_bundled_file_outcomes_are_unchanged_when_the_reference_exists(monkeypatch, env):
    assert _bundled_outcome(monkeypatch, env, lambda **k: (True, None, "match")) == ("passed", "")
    outcome, msg = _bundled_outcome(monkeypatch, env, lambda **k: (False, {"missing_files": ["/usr/x"]}, "Bundled files differ"))
    assert outcome == "failed" and "Missing files" in msg and "/usr/x" in msg
    outcome, msg = _bundled_outcome(monkeypatch, env, _raises(RuntimeError("rpm -ql failed")))
    assert outcome == "failed" and "rpm -ql failed" in msg


# --------------------------------------------------------------------------- prerelease versions
# Real pgEdge prereleases. Tag v5.0.13-rc.1 -> release intent 5.0.13 / rc1_1 -> staging RPM
# pgedge-spock50_17 5.0.13-rc1_1.el9; tag v6.0.0-beta.1 -> 6.0.0 / beta1_1 -> staging DEB
# pgedge-postgresql-17-spock60 6.0.0~beta1-1.bookworm. The prerelease tag lives only in the package
# version (checked by the identity step); the extension version is SPOCK_VERSION without it (the
# beta DEB ships default_version '6.0.0'), so it must still equal expected_version exactly.
_RC_RPM = dict(_V5_RPM, PEP_EXPECTED_BUILDNUM="rc1_1", PEP_EXPECTED_RPM="5.0.13-rc1_1.el9")
_BETA_DEB = dict(_V6_DEB, PEP_EXPECTED_BUILDNUM="beta1_1", PEP_EXPECTED_DEB="6.0.0~beta1-1.bookworm",
                 PEP_CONTAINER_ALIAS="debian12-amd64", DEB_CONTAINERS="debian12-amd64")


@pytest.mark.parametrize("env,ctype,owner,shipped,other", [
    (_RC_RPM, "rhel", "pgedge-spock50_17", "5.0.13", "5.0.12"),
    (_BETA_DEB, "deb", "pgedge-postgresql-17-spock60", "6.0.0", "6.0.1"),
], ids=["rc-rpm", "beta-deb"])
def test_a_prerelease_request_accepts_the_extension_version_its_package_ships(monkeypatch, env, ctype, owner,
                                                                              shipped, other):
    m = load(monkeypatch, env)
    assert m.INTEGRATION_PROBLEM is None
    cfg = m.get_container_config(ctype)
    m.verify_nodes_use_requested_spock(_Container(_nodes(owner=owner, ext=(shipped, shipped))), ctype, cfg)
    for wrong in (other, shipped + "~beta1", shipped + "-rc.1"):    # still exact: no prefix or substring match
        with pytest.raises(AssertionError, match="spock extension version is %r" % wrong):
            m.verify_nodes_use_requested_spock(_Container(_nodes(owner=owner, ext=(wrong, wrong))), ctype, cfg)


# --------------------------------------------------------------------------- spock 6 release identity
# Tag v6.0.0-beta.2 -> release intent 6.0.0 / beta2_1. Its exact spock60 packages are RPM
# pgedge-spock60_<pg> 6.0.0-beta2_1.el<N> and DEB pgedge-postgresql-<pg>-spock60 6.0.0~beta2-1.<codename>;
# the extension both ship is 6.0.0 (include/spock.h SPOCK_VERSION, spock.control default_version).
_BETA2_RPM = dict(_V5_RPM, PEP_PACKAGE_NAME="pgedge-spock60_17", PEP_EXPECTED_VERSION="6.0.0",
                  PEP_EXPECTED_BUILDNUM="beta2_1", PEP_EXPECTED_RPM="6.0.0-beta2_1.el9")
_BETA2_DEB = dict(_V6_DEB, PEP_EXPECTED_BUILDNUM="beta2_1", PEP_EXPECTED_DEB="6.0.0~beta2-1.noble",
                  PEP_CONTAINER_ALIAS="ubuntu2404-arm64", PEP_ARCH_FILTER="arm64", DEB_CONTAINERS="ubuntu2404-arm64")


@pytest.mark.parametrize("env,series,package", [
    (dict(_BETA2_RPM, SPOCK_MAJOR="50"), "60", "pgedge-spock60_17"),
    (dict(_BETA2_DEB, SPOCK_MAJOR=None), "60", "pgedge-postgresql-17-spock60"),
], ids=["rpm-env-50", "deb-default-50"])
def test_a_spock6_release_tests_its_own_spock60_package_whatever_spock_major_says(monkeypatch, env, series, package):
    m = load(monkeypatch, env)
    assert m.INTEGRATION_PROBLEM is None and m.spock_major == series
    assert m.get_spock_pkg_name("rhel") == m.get_spock_pkg_name("deb") == package


@pytest.mark.parametrize("env,ctype,observed,ok", [
    (_BETA2_RPM, "rhel", "6.0.0-beta2_1.el9", True),
    (_BETA2_RPM, "rhel", "6.0.0-beta1_1.el9", False),          # the previous beta
    (_BETA2_RPM, "rhel", "6.0.0-1.el9", False),                # the final release
    (_BETA2_DEB, "deb", "6.0.0~beta2-1.noble", True),
    (_BETA2_DEB, "deb", "6.0.0~beta2-1.jammy", False),         # another codename
    (_BETA2_DEB, "deb", "6.0.0-1.noble", False),
], ids=["rpm-exact", "rpm-beta1", "rpm-final", "deb-exact", "deb-codename", "deb-final"])
def test_spock6_beta_identity_is_the_exact_package_build(monkeypatch, tmp_path, env, ctype, observed, ok):
    m = load(monkeypatch, env)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    pin = env.get("PEP_EXPECTED_RPM") or env["PEP_EXPECTED_DEB"]
    m.pep_integration_mode.pep_evidence.write_install_evidence(
        m.INTEGRATION_REQUEST, "run-A", "pinned", pin, str(tmp_path / "install-evidence.json"),
        installed_sha256="f" * 64)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: observed)
    container = env["CONTAINERS"] if ctype == "rhel" else env["DEB_CONTAINERS"]
    if ok:
        m.test_spock_identity(container, ctype)
    else:
        with pytest.raises(AssertionError, match="not proven"):
            m.test_spock_identity(container, ctype)
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")


def test_spock6_beta_nodes_must_run_extension_6_0_0_exactly(monkeypatch):
    m = load(monkeypatch, _BETA2_RPM)
    cfg = m.get_container_config("rhel")
    m.verify_nodes_use_requested_spock(_Container(_nodes(owner="pgedge-spock60_17", ext=("6.0.0", "6.0.0"))),
                                       "rhel", cfg)
    for wrong in ("5.0.14", "6.0.0-beta2", "6.0.0beta2"):
        with pytest.raises(AssertionError, match="spock extension version is %r" % wrong):
            m.verify_nodes_use_requested_spock(_Container(_nodes(owner="pgedge-spock60_17", ext=(wrong, wrong))),
                                               "rhel", cfg)


# --------------------------------------------------------------------------- standalone extension version
@pytest.mark.parametrize("configured,extension", [
    ("5.0.14", "5.0.14"), ("5.0.4", "5.0.4"),                  # Spock 5 configuration: unchanged
    ("6.0.0beta2", "6.0.0"), ("6.0.0-beta2", "6.0.0"), ("6.0.0~beta2", "6.0.0"), ("6.0.0", "6.0.0"),
])
def test_extension_version_drops_only_a_pre_release_tag(monkeypatch, configured, extension):
    assert load(monkeypatch, {"PG_MAJOR_VERSION": "17"}).extension_version_for(configured) == extension


def _standalone_extension_check(monkeypatch, spock_major, configured, ext):
    m = load(monkeypatch, {"PG_MAJOR_VERSION": "17", "SPOCK_MAJOR": spock_major})
    monkeypatch.setenv("PGEDGE_SPOCK%s_17_VERSION" % spock_major, configured)
    c = _Container(_nodes(ext=(ext, ext)))
    monkeypatch.setattr(m, "client", _Client(c))
    m.test_verify_spock_extension_version("rocky9-amd64", "rhel")
    return c


def test_standalone_spock6_beta_accepts_the_extension_its_package_ships(monkeypatch):
    c = _standalone_extension_check(monkeypatch, "60", "6.0.0beta2", "6.0.0")
    assert sum(1 for cmd in c.cmds if cmd[0] == "psql" and "extversion" in cmd[-1]) == 2
    with pytest.raises(AssertionError, match="expected '6.0.0', got '5.0.14'"):
        _standalone_extension_check(monkeypatch, "60", "6.0.0beta2", "5.0.14")


@pytest.mark.parametrize("spock_major,configured,ext", [
    ("60", "6.0.0beta2", "6.0.0-beta2"), ("60", "6.0.0beta2", "6.0.0.1"), ("50", "5.0.1", "5.0.14")],
    ids=["tagged-extension", "longer-extension", "spock5-prefix"])
def test_standalone_extension_version_must_match_exactly(monkeypatch, spock_major, configured, ext):
    with pytest.raises(AssertionError, match="Spock version mismatch"):
        _standalone_extension_check(monkeypatch, spock_major, configured, ext)


def test_standalone_spock5_extension_check_is_unchanged(monkeypatch):
    _standalone_extension_check(monkeypatch, "50", "5.0.14", "5.0.14")
    with pytest.raises(AssertionError, match="expected '5.0.14', got '5.0.13'"):
        _standalone_extension_check(monkeypatch, "50", "5.0.14", "5.0.13")


# --------------------------------------------------------------------------- Spock 6 DEB file contract
# expected-output/deb/spock60 against Spock tag v6.0.0-beta.2 (d05e2e2b): the bitcode its Makefile
# builds (src/*.c and src/compat/<pg>/*.c, spock_output separately) and the upgrade scripts it ships
# (sql/spock*--*.sql), as the noble amd64 PG17 DEB build installed them (pgEdge/spock run 44045406576,
# job 133909938946). beta.2 renamed spock_monitoring.c to spock_monitor.c and added spock_change_log.c.
# Spock 5 (v5_STABLE) still builds spock_monitoring.c, so expected-output/deb/spock50 keeps that name.
_REPO = os.path.join(os.path.dirname(__file__), "..", "..", "..")
_BITCODE = "/usr/lib/postgresql/17/lib/bitcode/spock/src/"
_EXTENSION = "/usr/share/postgresql/17/extension/"
_SPOCK6_BETA2_BITCODE = (
    "compat/17/spock_compat", "spock", "spock_apply", "spock_apply_heap", "spock_autoddl", "spock_change_log",
    "spock_common", "spock_conflict", "spock_conflict_stat", "spock_dependency", "spock_exception_handler",
    "spock_executor", "spock_failover_slots", "spock_fe", "spock_functions", "spock_group", "spock_injection",
    "spock_jsonb_utils", "spock_manager", "spock_monitor", "spock_node", "spock_output_config",
    "spock_output_plugin", "spock_output_proto", "spock_progress_recovery", "spock_proto_json",
    "spock_proto_native", "spock_queue", "spock_readonly", "spock_relcache", "spock_repset", "spock_rmgr",
    "spock_rpc", "spock_sequences", "spock_shmem", "spock_sync", "spock_worker")
_SPOCK6_BETA2_SQL = frozenset(
    ["spock--5.0.%d--5.0.%d.sql" % (v, v + 1) for v in range(14)]
    + ["spock--5.0.14--6.0.0.sql", "spock--6.0.0--6.0.0-beta1-to-beta2.sql",
       "spock--6.0.0-beta1-to-beta2--6.0.0.sql", "spock--6.0.0.sql"])
_SPOCK6_DEB = "pgedge-postgresql-17-spock60"


def _reference(family, name):
    with open(os.path.join(_REPO, "expected-output", family, name)) as f:
        return f.read().splitlines()


def test_the_spock6_deb_reference_lists_exactly_the_beta2_bitcode():
    ref = _reference("deb", "spock60")
    listed = sorted(l[len(_BITCODE):-len(".bc")] for l in ref if l.startswith(_BITCODE) and l.endswith(".bc"))
    assert listed == sorted(_SPOCK6_BETA2_BITCODE)
    assert _BITCODE + "spock_monitoring.bc" not in ref


def test_the_spock6_deb_reference_names_only_upgrade_scripts_beta2_ships():
    sql = {l[len(_EXTENSION):] for l in _reference("deb", "spock60") if l.startswith(_EXTENSION) and l.endswith(".sql")}
    assert sql <= _SPOCK6_BETA2_SQL, sorted(sql - _SPOCK6_BETA2_SQL)
    assert {"spock--5.0.14--6.0.0.sql", "spock--6.0.0.sql"} <= sql


def test_the_spock5_deb_reference_keeps_spock_monitoring():
    ref = _reference("deb", "spock50")
    assert _BITCODE + "spock_monitoring.bc" in ref
    assert _BITCODE + "spock_monitor.bc" not in ref and _BITCODE + "spock_change_log.bc" not in ref


def _beta2_deb_listing(rename=None, drop=None):
    """``dpkg -L pgedge-postgresql-17-spock60`` for the beta.2 DEB: the recorded bitcode and upgrade
    scripts, the reference's other entries (libraries, bitcode indexes, SBOM, docs, control) and the
    directories dpkg lists. ``rename`` = (old, new) bitcode name; ``drop`` = a bitcode name to leave out."""
    bitcode = [(rename[1] if rename and b == rename[0] else b) for b in _SPOCK6_BETA2_BITCODE if b != drop]
    other = [l for l in _reference("deb", "spock60")
             if not (l.startswith(_BITCODE) or (l.startswith(_EXTENSION) and l.endswith(".sql")))]
    dirs = ["/.", "/usr", "/usr/lib", "/usr/lib/postgresql", "/usr/lib/postgresql/17", "/usr/lib/postgresql/17/lib",
            "/usr/lib/postgresql/17/lib/bitcode/spock/src", "/usr/share/postgresql/17/extension"]
    return dirs + other + [_BITCODE + b + ".bc" for b in bitcode] + [_EXTENSION + s for s in sorted(_SPOCK6_BETA2_SQL)]


def _real_bundled_outcome(monkeypatch, listing):
    """Run the real test_verify_bundled_files, with the real file_management check and reference,
    on a Spock 6 beta.2 DEB request whose package lists ``listing``."""
    m = load(monkeypatch, _BETA2_DEB)

    def respond(cmd):
        if cmd == "dpkg -L %s" % _SPOCK6_DEB:
            return 0, "".join(f + "\n" for f in listing)
        return _pkg_list(_SPOCK6_DEB)(cmd) if isinstance(cmd, list) else (1, "unexpected")
    monkeypatch.setattr(m, "client", _Client(_Container(respond)))
    try:
        m.test_verify_bundled_files("ubuntu2404-arm64", "deb")
    except pytest.skip.Exception as e:
        return "skipped", str(e)
    except pytest.fail.Exception as e:
        return "failed", str(e)
    return "passed", ""


def test_the_real_bundled_file_check_passes_the_beta2_deb_payload(monkeypatch):
    assert _real_bundled_outcome(monkeypatch, _beta2_deb_listing()) == ("passed", "")


@pytest.mark.parametrize("listing,missing", [
    (dict(rename=("spock_monitor", "spock_monitoring")), "spock_monitor.bc"),
    (dict(drop="spock_change_log"), "spock_change_log.bc"),
    (dict(drop="spock_monitor"), "spock_monitor.bc"),
], ids=["spock5-monitoring-name", "no-change-log", "no-monitor"])
def test_the_real_bundled_file_check_still_fails_a_missing_beta2_file(monkeypatch, listing, missing):
    status, msg = _real_bundled_outcome(monkeypatch, _beta2_deb_listing(**listing))
    assert status == "failed" and "Missing 1 expected files" in msg and missing in msg, msg
