"""Docker-free tests that exercise the REAL functions in component-test/test_pep_radar.py.

The module is loaded by path with docker.from_env and load_dotenv neutralized and with the environment
of the case under test (the integration request, platform filter and binary settings are decided at
import time), then driven with fake containers and spied package helpers -- the approach of
test_pep_ace_wiring.py. Versions are synthetic (9.8.7), never a real radar release. Covers:
  * standalone behaviour kept (configured package, PLATFORM_FILTER, server package, unpinned install,
    the documented standalone skips);
  * integration mode: the request selects the package, platform and binary; a request the test cannot
    serve is refused before anything is installed; the PostgreSQL server installs before the pinned
    radar package; only the requested radar package may be installed; the shared identity step;
    ownership of /usr/bin/radar and an exact binary version derived from the request's effective tag;
  * required checks fail instead of skipping in integration mode (missing container or reference);
  * the functional collections: radar.out and postgresql/version.tsv are checked against the expected
    radar version and the running server, the skip flags are honoured, and the bundle is never copied
    out or printed and is removed afterwards;
  * the stripped-ELF check and the expected-output references derived from radar's packaging.
"""
import importlib.util
import itertools
import json
import sys
from pathlib import Path

import docker
import dotenv
import pytest

_REPO = Path(__file__).resolve().parents[3]
_RADAR_PATH = _REPO / "component-test" / "test_pep_radar.py"
_counter = itertools.count()

# radar 9.8.7 (synthetic) as published in the staging channel (tag v9.8.7, build number 1).
_GA_RPM = {"PEP_INTEGRATION_MODE": "1", "PEP_COMPONENT": "radar", "PEP_PACKAGE_NAME": "pgedge-radar",
           "PEP_CHANNEL": "staging", "PEP_EXPECTED_VERSION": "9.8.7", "PEP_FAMILY": "rpm",
           "PEP_ARCH_FILTER": "amd64", "PEP_CONTAINER_ALIAS": "oel9-amd64", "PG_MAJOR_VERSION": "17",
           "PEP_EXPECTED_BUILDNUM": "1", "PEP_EFFECTIVE_TAG": "v9.8.7", "PEP_EXPECTED_RPM": "9.8.7-1.el9",
           "REPO": "staging", "CONTAINERS": "oel9-amd64", "DEB_CONTAINERS": "debian13-amd64"}
_GA_DEB = dict(_GA_RPM, PEP_FAMILY="deb", PEP_CONTAINER_ALIAS="debian13-amd64", PEP_EXPECTED_RPM=None,
               PEP_EXPECTED_DEB="9.8.7-1.trixie")
# A daily test build (tag v9.8.7-test1 -> build number test1_1; the binary keeps the tag's suffix).
_TEST_RPM = dict(_GA_RPM, PEP_CHANNEL="daily", REPO="daily", PEP_EXPECTED_BUILDNUM="test1_1",
                 PEP_EFFECTIVE_TAG="v9.8.7-test1", PEP_EXPECTED_RPM="9.8.7-test1_1.el9")
_TEST_DEB = dict(_GA_DEB, PEP_CHANNEL="daily", REPO="daily", PEP_EXPECTED_BUILDNUM="test1_1",
                 PEP_EFFECTIVE_TAG="v9.8.7-test1", PEP_EXPECTED_DEB="9.8.7~test1-1.trixie")
# A release candidate (tag v9.8.7-rc.1 -> build number rc1_1; the binary keeps the dot).
_RC_RPM = dict(_GA_RPM, PEP_EXPECTED_BUILDNUM="rc1_1", PEP_EFFECTIVE_TAG="v9.8.7-rc.1",
               PEP_EXPECTED_RPM="9.8.7-rc1_1.el9")
_STANDALONE = {"PG_MAJOR_VERSION": "17", "PGEDGE_RADAR_VERSION": "9.8.7",
               "CONTAINERS": "oel9-amd64", "DEB_CONTAINERS": "debian13-amd64"}
_KEYS = sorted(set(_GA_RPM) | set(_GA_DEB) | {
    "PEP_EXPECTED_DEB", "PEP_EXPECTED_BINARY", "PEP_MODE", "PEP_SCENARIO", "PLATFORM_FILTER",
    "PGEDGE_RADAR_VERSION", "RADAR_PACKAGE", "DEB_RADAR_PACKAGE", "COMPONENT_BINARY",
    "COMPONENT_BINARY_VERSION", "SERVER_PACKAGE", "DEB_SERVER_PACKAGE", "UPGRADE", "UPGRADE_REPO",
    "PG_PORT", "PG_DATA_DIR", "PG_BIN_PATH", "DEB_PG_BIN_PATH", "PG_USER", "DEB_PG_USER",
    "RADAR_TEST_DATABASE", "SKIP_CLEANUP", "COMPONENTS"})


def load(monkeypatch, env):
    """A fresh copy of the radar component test under exactly ``env`` (None values unset)."""
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(docker, "from_env", lambda *a, **k: None)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)   # only ``env`` decides
    name = "radar_wiring_under_test_%d" % next(_counter)
    spec = importlib.util.spec_from_file_location(name, str(_RADAR_PATH))
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


class _Container:
    """A running container whose exec_run answers from ``respond(cmd) -> (exit_code, text)``. Copying
    files out of it (or into it) is not part of any check, so those calls fail the test."""
    status = "running"

    def __init__(self, respond):
        self.respond, self.cmds = respond, []

    def exec_run(self, cmd, user=None, demux=False, **kwargs):
        self.cmds.append(cmd)
        ec, out = self.respond(cmd)
        if demux:
            return ec, ((out or "").encode() or None, None)
        return ec, (out or "").encode()

    def get_archive(self, *a, **k):
        raise AssertionError("a bundle or file was copied out of the container")

    def put_archive(self, *a, **k):
        raise AssertionError("a file was copied into the container")

    def export(self, *a, **k):
        raise AssertionError("the container filesystem was exported")


class _Client:
    def __init__(self, container=None, missing=False):
        class _Containers:
            def get(_self, name):
                if missing:
                    raise docker.errors.NotFound("no such container")
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


def outcome(fn, *args):
    """("passed"|"skipped"|"failed", message) of a real component-test function."""
    try:
        fn(*args)
    except pytest.skip.Exception as e:
        return "skipped", str(e)
    except (AssertionError, pytest.fail.Exception) as e:
        return "failed", str(e)
    return "passed", ""


def _wire(monkeypatch, tmp_path, m, container):
    monkeypatch.setattr(m, "client", _Client(container))
    for k, f in (("PEP_INSTALL_OUT", "install-evidence.json"), ("PEP_IDENTITY_OUT", "identity-evidence.json"),
                 ("PEP_OBSERVED_OUT", "observed-identity.json"), ("PEP_PREREQ_OUT", "prerequisite-versions.json")):
        monkeypatch.setenv(k, str(tmp_path / f))
    monkeypatch.setenv("PEP_RUN_TOKEN", "run-A")


def _nothing(cmd):
    return 0, ""


def _pkg_list(*names):
    """The package-manager listing installed_radar_packages reads (rpm -qa / dpkg-query -W)."""
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qa"]:
            return 0, "".join(n + "\n" for n in names)
        if cmd[:2] == ["dpkg-query", "-W"]:
            return 0, "".join(n + " install ok installed\n" for n in names)
        return 0, ""
    return respond


# --------------------------------------------------------------------------- standalone kept
def test_standalone_keeps_configured_package_platform_filter_and_binary(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, PLATFORM_FILTER="deb", DEB_RADAR_PACKAGE="pgedge-radar-custom",
                               COMPONENT_BINARY="/opt/radar/bin/radar", SERVER_PACKAGE="my-server"))
    assert m.INTEGRATION_REQUEST is None and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == [("debian13-amd64", "deb")]
    assert m.get_container_config("deb")["radar_package"] == "pgedge-radar-custom"
    assert m.get_container_config("rhel")["server_package"] == "my-server"
    assert m.component_binary == "/opt/radar/bin/radar"
    m = load(monkeypatch, _STANDALONE)
    assert m.all_containers == [("oel9-amd64", "rhel"), ("debian13-amd64", "deb")]
    assert m.component_binary == "/usr/bin/radar"


def test_standalone_install_is_unpinned_and_unchanged(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    m.test_component_install("oel9-amd64", "rhel")
    assert pinned.calls == [] and ip.calls == [((c, "pgedge-radar"), {})]


def test_standalone_server_install_is_unchanged(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    ip = _Spy((True, "debian", "installed"))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    m.test_server_install("debian13-amd64", "deb")
    assert ip.calls == [((c, "pgedge-postgresql-17"), {"pg_major_version": "17", "install_pg_server": True})]


def test_standalone_package_version_check_uses_pgedge_radar_version(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    verify = _Spy((True, "rhel", "9.8.7-1.el9", "ok"))
    monkeypatch.setattr(m.package_management, "verify_package_version", verify)
    assert outcome(m.test_component_package_version, "oel9-amd64", "rhel") == ("passed", "")
    assert verify.calls[0][0][1:] == ("pgedge-radar", "9.8.7")


@pytest.mark.parametrize("test,env", [
    ("test_component_package_version", dict(_STANDALONE, PGEDGE_RADAR_VERSION=None)),
    ("test_binary_version", dict(_STANDALONE, PGEDGE_RADAR_VERSION=None)),
    ("test_radar_identity", _STANDALONE),
    ("test_component_upgrade", _STANDALONE),
], ids=["package-version-unset", "binary-version-unset", "identity-integration-only", "upgrade-off"])
def test_standalone_keeps_its_documented_skips(monkeypatch, test, env):
    m = load(monkeypatch, env)
    assert m.radar_version == ("" if env.get("PGEDGE_RADAR_VERSION") is None else "9.8.7")  # no made-up default
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    assert outcome(getattr(m, test), "oel9-amd64", "rhel")[0] == "skipped"


def test_standalone_still_skips_a_missing_container_or_reference(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(missing=True))
    for test in ("test_configure_repository", "test_component_install", "test_verify_sbom",
                 "test_radar_collect_system_only", "test_binary_version", "test_binary_stripped"):
        assert outcome(getattr(m, test), "oel9-amd64", "rhel")[0] == "skipped", test
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for radar")))
    assert outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel")[0] == "skipped"


# --------------------------------------------------------------------------- integration selection
@pytest.mark.parametrize("env,containers", [
    (dict(_GA_RPM, PLATFORM_FILTER="deb"), [("oel9-amd64", "rhel")]),
    (_GA_DEB, [("debian13-amd64", "deb")]),
], ids=["rpm-overrides-filter", "deb"])
def test_integration_request_selects_the_package_platform_and_binary(monkeypatch, env, containers):
    m = load(monkeypatch, dict(env, RADAR_PACKAGE="pgedge-radar-custom", DEB_RADAR_PACKAGE="pgedge-radar-custom",
                               COMPONENT_BINARY="/usr/bin/ace", COMPONENT_BINARY_VERSION="1.2.3",
                               PGEDGE_RADAR_VERSION="1.2.3"))
    assert m.INTEGRATION_REQUEST["component"] == "radar" and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == containers
    assert m.get_container_config("rhel")["radar_package"] == "pgedge-radar"
    assert m.get_container_config("deb")["radar_package"] == "pgedge-radar"
    assert m.component_binary == "/usr/bin/radar"           # never the shared COMPONENT_BINARY
    assert m.expected_radar_version() == ("9.8.7", None)    # never the config versions


def test_a_request_for_another_package_is_rejected_at_load(monkeypatch):
    with pytest.raises(Exception, match="does not match component 'radar'"):
        load(monkeypatch, dict(_GA_RPM, PEP_PACKAGE_NAME="pgedge-ace"))


def test_the_backstop_names_each_request_it_cannot_serve(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    req = dict(m.INTEGRATION_REQUEST)
    assert m.integration_problem(req, "17") is None
    assert "PG17 disagrees with PG_MAJOR_VERSION=16" in m.integration_problem(req, "16")
    assert "component 'ace' is not radar" in m.integration_problem(dict(req, component="ace"), "17")
    assert "is not a radar package" in m.integration_problem(dict(req, package_name="pgedge-ace"), "17")
    assert m.integration_problem(None, "17") is None
    # A valid request routed to the wrong test (here an ACE request) is refused, not installed.
    m = load(monkeypatch, dict(_GA_RPM, PEP_COMPONENT="ace", PEP_PACKAGE_NAME="pgedge-ace"))
    assert "component 'ace' is not radar" in m.INTEGRATION_PROBLEM


# --------------------------------------------------------------------------- install
def test_the_server_installs_first_then_the_pinned_request_and_versions_are_recorded(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-radar", "pgedge-postgresql17-server")))
    order = []
    ip = _Spy((True, "rhel", "installed"), order, "server")
    pinned = _Spy((True, "ok", "a" * 64), order, "pinned")
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    versions = {"pgedge-radar": "9.8.7-1.el9"}
    monkeypatch.setattr(m.package_management, "query_installed_version",
                        lambda c, n: versions.get(n, "17.11-1PGEDGE.el9"))

    m.test_component_install("oel9-amd64", "rhel")

    assert order == ["server", "pinned"]
    assert ip.calls[0][1]["package_name"] == ["pgedge-postgresql17-server"]
    assert pinned.calls[0][0][1:] == ("pgedge-radar", "9.8.7-1.el9")
    marker = json.loads((tmp_path / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "a" * 64
    prereq = json.loads((tmp_path / "prerequisite-versions.json").read_text())
    assert prereq == {"channel": "staging", "pg_major": "17", "requested_package": "pgedge-radar",
                      "installed_versions": {"pgedge-postgresql17-server": "17.11-1PGEDGE.el9",
                                             "pgedge-radar": "9.8.7-1.el9"}}


def test_the_deb_server_prerequisite_is_the_server_package_for_the_requested_major(monkeypatch, tmp_path):
    m = load(monkeypatch, _TEST_DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-radar", "pgedge-postgresql-17")))
    ip, pinned = _Spy((True, "debian", "installed")), _Spy((True, "ok", "b" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    m.test_component_install("debian13-amd64", "deb")
    assert ip.calls[0][1]["package_name"] == ["pgedge-postgresql-17"]
    assert pinned.calls[0][0][1:] == ("pgedge-radar", "9.8.7~test1-1.trixie")


def test_a_missing_server_is_a_prerequisite_failure_and_radar_is_not_installed(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    pinned = _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package",
                        _Spy(Exception("Failed to install pgedge-postgresql17-server: No match")))
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    status, msg = outcome(m.test_component_install, "oel9-amd64", "rhel")
    assert status == "failed" and "server prerequisite unavailable from the 'staging' channel" in msg
    assert pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_a_request_the_backstop_rejects_is_refused_before_any_install(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m, "INTEGRATION_PROBLEM", m.integration_problem(m.INTEGRATION_REQUEST, "16"))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    status, msg = outcome(m.test_component_install, "oel9-amd64", "rhel")
    assert status == "failed" and "cannot be served" in msg and "PG_MAJOR_VERSION=16" in msg
    assert ip.calls == [] and pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_a_failed_pinned_install_fails_without_install_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((False, "checksum mismatch", None)))
    status, msg = outcome(m.test_component_install, "oel9-amd64", "rhel")
    assert status == "failed" and "pinned install of pgedge-radar=9.8.7-1.el9 failed: checksum mismatch" in msg
    assert not (tmp_path / "install-evidence.json").exists()


@pytest.mark.parametrize("ctype,present", [
    ("rhel", ("pgedge-radar", "pgedge-radar-debuginfo")),   # a stray radar package beside the request
    ("deb", ("pgedge-radar", "pgedge-radar-dbgsym")),
    ("rhel", ()),                                          # the request is not installed at all
], ids=["rpm-stray", "deb-stray", "absent"])
def test_any_radar_package_other_than_the_request_fails_the_install_step(monkeypatch, tmp_path, ctype, present):
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(*present, "pgedge-postgresql17-server")))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((True, "ok", "a" * 64)))
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    status, msg = outcome(m.test_component_install, "x", ctype)
    assert status == "failed" and "expected exactly the requested radar package" in msg


def test_component_tests_share_the_integration_helper_instances(monkeypatch):
    # Patching package_management / pep_evidence must reach the shared install + identity steps.
    m = load(monkeypatch, _GA_RPM)
    assert m.pep_request_env is m.pep_integration_mode.pep_request_env
    assert m.package_management is m.pep_integration_mode.package_management


# --------------------------------------------------------------------------- identity
@pytest.mark.parametrize("observed,ok", [("9.8.7-1.el9", True), ("9.8.6-1.el9", False),
                                         ("9.8.7-rc1_1.el9", False)], ids=["exact", "older", "rc"])
def test_identity_proves_the_exact_requested_package_version(monkeypatch, tmp_path, observed, ok):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    m.pep_integration_mode.pep_evidence.write_install_evidence(
        m.INTEGRATION_REQUEST, "run-A", "pinned", "9.8.7-1.el9", str(tmp_path / "install-evidence.json"),
        installed_sha256="a" * 64)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: observed)
    status, msg = outcome(m.test_radar_identity, "oel9-amd64", "rhel")
    assert status == ("passed" if ok else "failed")
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")


def test_identity_without_install_evidence_fails_with_persisted_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    status, msg = outcome(m.test_radar_identity, "oel9-amd64", "rhel")
    assert status == "failed" and "install-before-identity precondition failed" in msg
    assert (tmp_path / "identity-evidence.json").exists()


@pytest.mark.parametrize("test,why", [
    ("test_component_package_version", "superseded by test_radar_identity"),
    ("test_component_upgrade", "certification never upgrades"),
    ("test_server_install", "installed the PostgreSQL server before the requested radar package"),
], ids=["package-version", "upgrade", "server-install"])
def test_integration_mode_only_skips_documented_superseded_or_disabled_steps(monkeypatch, test, why):
    m = load(monkeypatch, dict(_GA_RPM, UPGRADE="true", PGEDGE_RADAR_VERSION="9.8.7"))
    ip = _Spy((True, "rhel", "installed"))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    status, msg = outcome(getattr(m, test), "oel9-amd64", "rhel")
    assert status == "skipped" and why in msg and ip.calls == []


# --------------------------------------------------------------------------- expected binary version
@pytest.mark.parametrize("env,expected", [
    (_GA_RPM, "9.8.7"),
    (_GA_DEB, "9.8.7"),
    (_RC_RPM, "9.8.7-rc.1"),
    (dict(_RC_RPM, PEP_FAMILY="deb", PEP_CONTAINER_ALIAS="debian13-amd64", PEP_EXPECTED_RPM=None,
          PEP_EXPECTED_DEB="9.8.7~rc1-1.trixie"), "9.8.7-rc.1"),
    (_TEST_RPM, "9.8.7-test1"),
    (_TEST_DEB, "9.8.7-test1"),
    (dict(_GA_RPM, PEP_EXPECTED_BUILDNUM=None, PEP_EXPECTED_RPM=None), "9.8.7"),
], ids=["ga-rpm", "ga-deb", "rc-rpm", "rc-deb", "test-rpm", "test-deb", "tag-only"])
def test_the_expected_binary_version_is_the_effective_tag_without_v(monkeypatch, env, expected):
    m = load(monkeypatch, env)
    assert m.expected_radar_binary_version(m.INTEGRATION_REQUEST) == (expected, [])


@pytest.mark.parametrize("override,problem", [
    ({"effective_tag": None}, "effective_tag is required"),
    ({"effective_tag": "v9.8"}, "is not v<major>.<minor>.<patch>"),
    ({"effective_tag": "v9.8.8"}, "does not match expected_version '9.8.7'"),
    ({"effective_tag": "v9.8.7-rc.1"}, "expected_buildnum '1' does not match"),
    ({"expected_buildnum": "2"}, "expected_buildnum '2' does not match"),
    ({"expected_rpm": "9.8.7-rc1_1.el9"}, "expected_rpm '9.8.7-rc1_1.el9' does not match"),
    ({"expected_rpm": "9.8.7-1"}, "expected_rpm '9.8.7-1' does not match"),
], ids=["no-tag", "malformed-tag", "tag-vs-version", "tag-vs-buildnum", "buildnum", "rpm-string",
        "rpm-no-dist"])
def test_inconsistent_release_facts_are_reported(monkeypatch, override, problem):
    m = load(monkeypatch, _GA_RPM)
    expected, problems = m.expected_radar_binary_version(dict(m.INTEGRATION_REQUEST, **override))
    assert expected is None and any(problem in p for p in problems), problems


def test_an_inconsistent_deb_string_is_reported(monkeypatch):
    m = load(monkeypatch, _GA_DEB)
    for wrong in ("9.8.7~rc1-1.trixie", "9.8.7-2.trixie", "9.8.7-test1-1.trixie"):
        expected, problems = m.expected_radar_binary_version(dict(m.INTEGRATION_REQUEST, expected_deb=wrong))
        assert expected is None and any("expected_deb %r does not match" % wrong in p for p in problems)


# --------------------------------------------------------------------------- binary version + ownership
def _binary(version_out, owner="pgedge-radar", owner_rc=0, rc=0):
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qf"]:
            return owner_rc, owner + "\n"
        if cmd[:2] == ["dpkg-query", "-S"]:
            return owner_rc, "%s: /usr/bin/radar\n" % owner
        if cmd == ["/usr/bin/radar", "--version"]:
            return rc, version_out
        return 1, "unexpected %r" % (cmd,)
    return respond


@pytest.mark.parametrize("env,ctype,line", [
    (_GA_RPM, "rhel", "radar version 9.8.7\n"),
    (_GA_DEB, "deb", "radar version 9.8.7\n"),
    (_RC_RPM, "rhel", "radar version 9.8.7-rc.1\n"),
    (_TEST_DEB, "deb", "radar version 9.8.7-test1\n"),
], ids=["ga-rpm", "ga-deb", "rc-rpm", "test-deb"])
def test_the_owned_binary_reporting_the_tag_version_passes(monkeypatch, env, ctype, line):
    m = load(monkeypatch, dict(env, PGEDGE_RADAR_VERSION="1.2.3", COMPONENT_BINARY_VERSION="1.2.3"))
    c = _Container(_binary(line))
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_binary_version, "x", ctype) == ("passed", "")
    assert c.cmds[0][:2] in (["rpm", "-qf"], ["dpkg-query", "-S"])          # ownership first
    assert c.cmds[1] == ["/usr/bin/radar", "--version"]                    # never a collection run


@pytest.mark.parametrize("env,out", [
    (_GA_RPM, "radar version v9.8.7\n"),                   # prefix: the tag, not the stamped version
    (_TEST_RPM, "radar version 9.8.7\n"),                  # missing suffix
    (_GA_RPM, "radar version 9.8.7-test1\n"),              # extra suffix
    (_GA_RPM, "radar version 9.8.7-dirty\n"),              # extra suffix (local build)
    (_RC_RPM, "radar version 9.8.7-rc1\n"),                # suffix without its dot
    (_GA_RPM, "radar version 9.8.6\n"),                    # different version
    (_GA_RPM, "radar version 9.8.7.1\n"),                  # expected version is only a prefix
    (_GA_RPM, "radar version dev\n"),                      # unstamped build
    (_GA_RPM, "radar version 9.8.7 (abc1234)\n"),          # trailing text
    (_GA_RPM, "radar version 9.8.7\nradar version 9.8.6\n"),
    (_GA_RPM, "9.8.7\n"),
], ids=["v-prefix", "missing-suffix", "extra-suffix", "dirty", "rc-without-dot", "different",
        "longer-version", "dev", "trailing-text", "two-lines", "no-version-line"])
def test_a_binary_reporting_any_other_version_fails(monkeypatch, env, out):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary(out))))
    assert outcome(m.test_binary_version, "x", "rhel")[0] == "failed"


def test_a_failing_version_command_fails(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary("radar version 9.8.7\n", rc=1))))
    status, msg = outcome(m.test_binary_version, "x", "rhel")
    assert status == "failed" and "exit code 1" in msg


@pytest.mark.parametrize("ctype,owner,rc", [
    ("rhel", "pgedge-radar-legacy", 0), ("deb", "pgedge-radar-legacy", 0),
    ("rhel", "file /usr/bin/radar is not owned by any package", 1),
], ids=["rpm-other-owner", "deb-other-owner", "unowned"])
def test_a_binary_not_owned_by_the_requested_package_fails(monkeypatch, ctype, owner, rc):
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    c = _Container(_binary("radar version 9.8.7\n", owner, rc))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_binary_version, "x", ctype)
    assert status == "failed" and "/usr/bin/radar" in msg
    assert ["/usr/bin/radar", "--version"] not in c.cmds                   # never probed


def test_a_request_without_an_effective_tag_fails_the_binary_check(monkeypatch):
    m = load(monkeypatch, dict(_GA_RPM, PEP_EFFECTIVE_TAG=None))
    monkeypatch.setattr(m, "client", _Client(_Container(_binary("radar version 9.8.7\n"))))
    status, msg = outcome(m.test_binary_version, "x", "rhel")
    assert status == "failed" and "effective_tag is required" in msg


@pytest.mark.parametrize("env,line,status", [
    (dict(_STANDALONE, COMPONENT_BINARY_VERSION="9.8.7-test1"), "radar version 9.8.7-test1\n", "passed"),
    (dict(_STANDALONE, COMPONENT_BINARY_VERSION="9.8.7"), "radar version 9.8.7-test1\n", "failed"),
], ids=["binary-version", "standalone-mismatch"])
def test_standalone_binary_version_uses_the_configured_binary_version(monkeypatch, env, line, status):
    m = load(monkeypatch, env)
    c = _Container(_binary(line))
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_binary_version, "x", "rhel")[0] == status
    assert c.cmds == [["/usr/bin/radar", "--version"]]                    # no ownership check standalone


@pytest.mark.parametrize("line", ["radar version 9.8.7\n", "radar version 9.8.7-test1\n"])
def test_standalone_never_takes_the_package_version_for_the_binary_version(monkeypatch, line):
    # PGEDGE_RADAR_VERSION is the PACKAGE version (9.8.7 in _STANDALONE); a prerelease binary reports
    # 9.8.7-test1 while its packages are 9.8.7-test1_1 / 9.8.7~test1-1, so it is no binary expectation.
    m = load(monkeypatch, _STANDALONE)
    assert m.radar_version == "9.8.7" and m.expected_radar_version() == (None, None)
    c = _Container(_binary(line))
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_binary_version, "x", "rhel")[0] == "skipped"
    assert c.cmds == []


# --------------------------------------------------------------------------- stripped ELF
_FILE = "ELF 64-bit LSB executable, x86-64, version 1 (SYSV), statically linked, Go BuildID=x, BuildID[sha1]=y, "


@pytest.mark.parametrize("out,problem", [
    (_FILE + "stripped", None),
    (_FILE + "not stripped", "it is not stripped"),
    (_FILE + "with debug_info, not stripped", "it is not stripped"),
    (_FILE.rstrip(", "), "does not report it as stripped"),
    ("POSIX shell script, ASCII text executable", "not an ELF binary"),
    ("cannot open `/usr/bin/radar' (No such file or directory)", "not an ELF binary"),
], ids=["stripped", "not-stripped", "debug-info", "unreported", "script", "missing"])
def test_the_strip_check_requires_a_stripped_elf(monkeypatch, out, problem):
    m = load(monkeypatch, _GA_RPM)
    found = m.elf_strip_problem(out)
    assert (found is None) if problem is None else (problem in found)
    monkeypatch.setattr(m, "client", _Client(_Container(
        lambda cmd: (0, out + "\n") if cmd == ["file", "-b", "/usr/bin/radar"] else (1, "unexpected"))))
    assert outcome(m.test_binary_stripped, "x", "rhel")[0] == ("passed" if problem is None else "failed")


# --------------------------------------------------------------------------- required checks
def test_integration_mode_fails_a_missing_container(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(missing=True))
    for test in ("test_configure_repository", "test_component_install", "test_radar_identity",
                 "test_verify_bundled_files", "test_verify_sbom", "test_init_cluster",
                 "test_radar_collect_system_and_postgres", "test_radar_collect_system_only",
                 "test_radar_collect_postgres_only", "test_binary_version", "test_binary_stripped",
                 "test_verify_license_file", "test_verify_readme_file", "test_package_uninstall"):
        status, msg = outcome(getattr(m, test), "oel9-amd64", "rhel")
        assert status == "failed" and "not found" in msg, test


def test_integration_mode_fails_a_missing_bundled_file_reference(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for radar")))
    status, msg = outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel")
    assert status == "failed" and "No expected file found" in msg


@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_a_missing_sbom_file_fails_in_both_modes(monkeypatch, env):
    # Both radar packages ship the SBOM, so its absence is a package defect, never a skip.
    m = load(monkeypatch, env)
    c = _Container(lambda cmd: (1, "") if cmd[:2] == ["test", "-f"] else (0, ""))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_verify_sbom, "oel9-amd64", "rhel")
    assert status == "failed" and "pgedge-radar-sbom.json is missing" in msg


# --------------------------------------------------------------------------- expected-output references
# The payload radar's packaging defines (pgEdge/radar packaging/rpm/radar.spec %files; DEB
# packaging/deb/debian/rules + debian/docs, with dh_installchangelogs and dh_compress).
_RPM_PAYLOAD = ["/usr/bin/radar", "/usr/share/doc/pgedge-radar/README.md", "/usr/share/licenses/pgedge-radar",
                "/usr/share/licenses/pgedge-radar/LICENCE.md", "/usr/share/pgedge-radar/pgedge-radar-sbom.json",
                "/usr/share/pgedge-radar/pgedge-radar-sbom.json.asc"]
_DEB_PAYLOAD = ["/usr/bin/radar", "/usr/share/doc/pgedge-radar/LICENCE.md",
                "/usr/share/doc/pgedge-radar/README.md.gz", "/usr/share/doc/pgedge-radar/changelog.Debian.gz",
                "/usr/share/pgedge-radar/pgedge-radar-sbom.json", "/usr/share/pgedge-radar/pgedge-radar-sbom.json.asc"]


def test_the_expected_output_references_list_the_packaged_payload():
    assert (_REPO / "expected-output" / "rpm" / "radar").read_text().splitlines() == _RPM_PAYLOAD
    assert (_REPO / "expected-output" / "deb" / "radar").read_text().splitlines() == _DEB_PAYLOAD


@pytest.mark.parametrize("ctype,listing_cmd,payload,extras", [
    ("rhel", "rpm -ql pgedge-radar", _RPM_PAYLOAD,
     ["/usr/lib/.build-id", "/usr/lib/.build-id/28", "/usr/lib/.build-id/28/fb947599f6e6f95c9ae070fac5a48f93c08854"]),
    ("deb", "dpkg -L pgedge-radar", _DEB_PAYLOAD,
     ["/.", "/usr", "/usr/bin", "/usr/share", "/usr/share/doc", "/usr/share/doc/pgedge-radar",
      "/usr/share/pgedge-radar"]),
], ids=["rpm", "deb"])
@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_the_real_bundled_file_check_finds_the_radar_reference(monkeypatch, env, ctype, listing_cmd, payload,
                                                               extras):
    # The real verify_bundled_files derives "radar" from pgedge-radar and compares the package's file
    # list against expected-output/<family>/radar: every listed file must be installed. Entries the
    # package manager adds around the payload (directories, RPM build-id links) are reported as extra.
    m = load(monkeypatch, env)

    def installed(files):
        return lambda cmd: (0, "".join(f + "\n" for f in files)) if cmd == listing_cmd else (1, "unexpected")
    monkeypatch.setattr(m, "client", _Client(_Container(installed(payload + extras))))
    assert outcome(m.test_verify_bundled_files, "x", ctype) == ("passed", "")
    for missing in (payload[0], payload[-1]):
        monkeypatch.setattr(m, "client", _Client(_Container(installed([f for f in payload if f != missing]))))
        status, msg = outcome(m.test_verify_bundled_files, "x", ctype)
        assert status == "failed" and "Missing files" in msg, missing


# --------------------------------------------------------------------------- functional collections
_SECRET = "SENTINEL-collected-value-0f3a"     # stands for configuration/environment data in a bundle
_PG17 = ("170006", "17.6", "PostgreSQL 17.6 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 11.5.0, 64-bit")
_ZIP = "radar-c0ffee-20261107-120000.zip"
_RADAR_OUTPUT = ("Collecting diagnostic data...\n"
                 "ERROR: ✗ system/dmesg: command 'dmesg []' failed: exit status 1 (output: %s\n"
                 "continued %s)\n"
                 "✓ Archive created: %s (12 KB)\n") % (_SECRET, _SECRET, _ZIP)


def _bundle(version="9.8.7", collected=_PG17[2], dirs=("system", "postgresql", "databases/postgres"),
            nest=""):
    """Archive entries (name -> text) as radar writes them, payload optionally nested under ``nest``."""
    entries = {"radar.out": "version: %s\ncommit: abc1234\n" % version}
    for d in dirs:
        entries["%s/data.out" % d] = "%s\n" % _SECRET
    if "postgresql" in dirs:
        entries["postgresql/version.tsv"] = "version\n%s\n" % collected
        entries["postgresql/postgresql.conf"] = "password_hint = '%s'\n" % _SECRET
    return {nest + k: v for k, v in entries.items()}


def _functional(entries, server=_PG17, radar_rc=0, radar_output=_RADAR_OUTPUT, zips=(_ZIP,)):
    """Answers for one functional step: radar 'writes' a bundle holding ``entries``; the server
    reports ``server`` = (server_version_num, server_version, version())."""
    def respond(cmd):
        if cmd[:2] == ["/bin/sh", "-c"]:
            script = cmd[2]
            if script == "command -v unzip":
                return 0, "/usr/bin/unzip\n"
            if script.startswith("rm -rf -- /tmp/radar-functional/") and "mkdir -p" in script:
                return 0, ""
            if "PGPORT=" in script and "/usr/bin/radar" in script:
                return radar_rc, radar_output
            if "ls -1 -- *.zip" in script:
                return 0, "".join(z + "\n" for z in zips)
            if "FROM pg_database" in script:
                return 0, "1\n"
            return 1, "unexpected %r" % (cmd,)
        if cmd[0].endswith("/psql"):
            return 0, "".join(v + "\n" for v in server)
        if cmd[:2] == ["unzip", "-Z1"]:
            return 0, "".join(e + "\n" for e in entries)
        if cmd[:2] == ["unzip", "-p"]:
            return (0, entries[cmd[3]]) if cmd[3] in entries else (11, "")
        if cmd[:3] == ["rm", "-rf", "--"]:
            return 0, ""
        return 1, "unexpected %r" % (cmd,)
    return respond


_CASES = {
    "full": ("test_radar_collect_system_and_postgres", ("system", "postgresql", "databases/postgres")),
    "system-only": ("test_radar_collect_system_only", ("system",)),
    "postgres-only": ("test_radar_collect_postgres_only", ("postgresql", "databases/mydatabase")),
}


def _run_case(monkeypatch, env, case, **functional):
    m = load(monkeypatch, env)
    functional.setdefault("entries", _bundle(dirs=_CASES[case][1]))
    c = _Container(_functional(**functional))
    monkeypatch.setattr(m, "client", _Client(c))
    return outcome(getattr(m, _CASES[case][0]), "x", "rhel"), c


def _removed(c, case):
    return c.cmds[-1] == ["rm", "-rf", "--", "/tmp/radar-functional/" + case]


@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
@pytest.mark.parametrize("case", sorted(_CASES))
def test_a_collection_reports_the_expected_radar_and_server_and_is_removed(monkeypatch, capsys, env, case):
    (res, c) = _run_case(monkeypatch, env, case)
    printed = capsys.readouterr().out
    assert res == ("passed", ""), res
    assert _removed(c, case)
    # Read inside the container: only the two entries asserted on, nothing extracted or copied out.
    reads = sorted(cmd[3] for cmd in c.cmds if cmd[:2] == ["unzip", "-p"])
    assert reads == (["radar.out"] if case == "system-only" else ["postgresql/version.tsv", "radar.out"])
    assert not any(cmd[0] in ("cat", "cp", "tar") for cmd in c.cmds)
    assert not any("unzip -q" in str(cmd) or "ls -la" in str(cmd) or " -d /" in str(cmd) for cmd in c.cmds)
    # Printed: radar's status lines, the failed collector's name, versions and names; never the data.
    assert _SECRET not in printed
    assert "system/dmesg (details withheld)" in printed and "1 other line(s) withheld" in printed
    assert "radar.out records version 9.8.7" in printed


def test_the_payload_root_may_be_nested_one_level(monkeypatch):
    (res, c) = _run_case(monkeypatch, _GA_RPM, "full", entries=_bundle(nest="radar-c0ffee-20261107-120000/"))
    assert res == ("passed", "")
    assert sorted(cmd[3] for cmd in c.cmds if cmd[:2] == ["unzip", "-p"]) == [
        "radar-c0ffee-20261107-120000/postgresql/version.tsv", "radar-c0ffee-20261107-120000/radar.out"]


@pytest.mark.parametrize("case,over,msg", [
    ("full", {"entries": _bundle(version="9.8.6")}, "radar.out records version '9.8.6', expected exactly '9.8.7'"),
    ("system-only", {"entries": _bundle(version="v9.8.7", dirs=("system",))}, "radar.out records version 'v9.8.7'"),
    ("full", {"entries": {k: v for k, v in _bundle().items() if k != "radar.out"}}, "expected one radar.out"),
    ("full", {"entries": dict(_bundle(), **{"radar.out": "commit: abc1234\n"})}, "0 'version:' line(s)"),
    # radar fell back to system-only collection (it could not connect): no PostgreSQL data at all
    ("full", {"entries": _bundle(dirs=("system",))}, "Expected directory 'databases' missing"),
    ("full", {"entries": _bundle(dirs=("system", "databases/postgres"))}, "Expected directory 'postgresql' missing"),
    ("postgres-only", {"entries": _bundle(dirs=("system", "databases/mydatabase"))},
     "Expected directory 'postgresql' missing"),
    ("postgres-only", {"entries": _bundle(dirs=("postgresql", "databases/postgres"))},
     "Expected directory 'databases/mydatabase' missing"),
    # a skip flag that was ignored
    ("system-only", {"entries": _bundle(dirs=("system", "postgresql"))}, "'postgresql' should have been skipped"),
    ("postgres-only", {"entries": _bundle(dirs=("system", "postgresql", "databases/mydatabase"))},
     "'system' should have been skipped"),
    # the collected server is not the running server, or not the leg's PG major
    ("full", {"entries": _bundle(collected="PostgreSQL 16.10 on x86_64-pc-linux-gnu, compiled by gcc, 64-bit")},
     "the running server's version() is"),
    ("full", {"server": ("160010", "16.10", "PostgreSQL 16.10 on x86_64-pc-linux-gnu, compiled by gcc, 64-bit"),
              "entries": _bundle(collected="PostgreSQL 16.10 on x86_64-pc-linux-gnu, compiled by gcc, 64-bit")},
     "the running server is PostgreSQL 16 (server_version_num 160010), not the leg's PG 17"),
    ("full", {"server": ("170006", "17.5", _PG17[2])}, "does not name server_version '17.5'"),
    ("postgres-only", {"entries": dict(_bundle(dirs=_CASES["postgres-only"][1]),
                                       **{"postgresql/version.tsv": "version\n%s\nextra\n" % _PG17[2]})},
     "is not a one-row 'version' table"),
    ("full", {"entries": {k: v for k, v in _bundle().items() if k != "postgresql/version.tsv"}},
     "postgresql/version.tsv missing"),
    # the run itself
    ("full", {"zips": ()}, "Expected exactly one .zip"),
    ("full", {"zips": (_ZIP, "radar-c0ffee-20261107-120001.zip")}, "Expected exactly one .zip"),
    ("full", {"zips": ("bundle.zip",)}, "does not match the expected"),
], ids=["radar-out-version", "radar-out-v-prefix", "no-radar-out", "radar-out-no-version",
        "fell-back-to-system-only", "no-postgresql", "postgres-only-without-postgresql", "postgres-only-without-database",
        "skip-postgres-ignored", "skip-system-ignored", "collected-other-server", "server-other-major",
        "version-not-naming-server-version", "malformed-version-tsv", "no-version-tsv",
        "no-zip", "two-zips", "bad-zip-name"])
def test_a_collection_that_does_not_show_the_expected_data_fails_and_is_removed(monkeypatch, capsys, case, over,
                                                                              msg):
    (status, text), c = _run_case(monkeypatch, _GA_RPM, case, **over)
    assert status == "failed" and msg in text, text
    assert _removed(c, case)
    assert _SECRET not in text and _SECRET not in capsys.readouterr().out


def test_a_failed_radar_run_reports_only_status_lines_and_is_removed(monkeypatch, capsys):
    output = ("Collecting diagnostic data...\n"
              "ERROR: Could not connect to PostgreSQL: failed to connect: connection refused\n"
              "ERROR: Continuing with system data collection only...\n"
              "ERROR: ✗ system/environ: read failed: %s\n"
              "ERROR: No data collected - this may indicate a problem\n") % _SECRET
    (status, text), c = _run_case(monkeypatch, _GA_RPM, "full", radar_rc=4, radar_output=output)
    assert status == "failed" and "exit 4" in text
    assert "Continuing with system data collection only" in text and "system/environ (details withheld)" in text
    assert _SECRET not in text and _SECRET not in capsys.readouterr().out
    assert _removed(c, "full") and not any(cmd[:2] == ["unzip", "-Z1"] for cmd in c.cmds)


def test_an_unreadable_server_fails_before_radar_runs(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    c = _Container(lambda cmd: (2, "") if cmd[0].endswith("/psql") else (0, ""))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_radar_collect_system_and_postgres, "x", "rhel")
    assert status == "failed" and "cannot read the running server's version" in msg
    assert not any("PGPORT=" in str(cmd) for cmd in c.cmds)


def test_a_collection_fails_when_the_expected_version_cannot_be_derived(monkeypatch):
    m = load(monkeypatch, dict(_GA_RPM, PEP_EXPECTED_BUILDNUM="test1_1"))
    c = _Container(_functional(_bundle(dirs=("system",))))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_radar_collect_system_only, "x", "rhel")
    assert status == "failed" and "expected_buildnum 'test1_1' does not match" in msg
    assert not any("PGPORT=" in str(cmd) for cmd in c.cmds)


def test_a_standalone_collection_without_an_expected_version_still_checks_radar_out(monkeypatch, capsys):
    env = dict(_STANDALONE, PGEDGE_RADAR_VERSION=None)
    (res, c) = _run_case(monkeypatch, env, "system-only", entries=_bundle(version="1.2.3", dirs=("system",)))
    assert res == ("passed", "") and "radar.out records version 1.2.3 (no expected version set)" in capsys.readouterr().out
    (res, c) = _run_case(monkeypatch, env, "system-only",
                         entries=dict(_bundle(dirs=("system",)), **{"radar.out": "commit: x\n"}))
    assert res[0] == "failed"


def test_the_console_summary_shows_only_radar_status_lines(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    output = ("Collecting diagnostic data...\n"
              "ERROR: ✗ postgres/stat_ssl: query failed: %s\n"
              "%s\n"
              "Warning: failed to close output file: %s\n"
              "✓ Archive created: %s (3 KB)\n") % (_SECRET, _SECRET, _SECRET, _ZIP)
    assert m.radar_console_summary(output) == [
        "Collecting diagnostic data...", "ERROR: ✗ postgres/stat_ssl (details withheld)",
        "✓ Archive created: %s (3 KB)" % _ZIP, "(2 other line(s) withheld)"]
