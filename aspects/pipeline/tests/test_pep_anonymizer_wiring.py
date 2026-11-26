"""Docker-free tests that exercise the REAL functions in component-test/test_pep_anonymizer.py.

The module is loaded by path with docker.from_env and load_dotenv neutralized and with the environment
of the case under test (the integration request, platform filter and package settings are decided at
import time), then driven with fake containers and spied package helpers -- the approach of
test_pep_radar_wiring.py. Versions are synthetic (9.8.7), never a real anonymizer release. Covers:
  * standalone behaviour kept (configured package, PLATFORM_FILTER, unpinned install, the configured
    PGEDGE_ANONYMIZER_VERSION checks and the documented standalone skips);
  * integration mode: the request selects the package and platform; a request the test cannot serve
    is refused before anything is installed; the requested package is the only install (pinned, with
    checksum evidence) and the only anonymizer package present; the shared identity step; ownership
    of /usr/bin/pgedge-anonymizer and an exact `pgedge-anonymizer version` line derived from the
    request's effective tag (other stdout lines and stderr are ignored; a second version line fails;
    the build stamp must be a real RFC 3339 instant; stderr that is not UTF-8 is escaped, never fatal);
  * required checks fail instead of skipping in integration mode (missing container, expected-output
    reference or SBOM location);
  * the stripped-ELF check (both modes) and the expected-output references derived from the
    anonymizer packaging.
The anonymizer test never uses PostgreSQL, so nothing here starts or checks a server.
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
_ANON_PATH = _REPO / "component-test" / "test_pep_anonymizer.py"
_BIN = "/usr/bin/pgedge-anonymizer"
_counter = itertools.count()

# anonymizer 9.8.7 (synthetic) as published in the staging channel (tag v9.8.7, build number 1).
_GA_RPM = {"PEP_INTEGRATION_MODE": "1", "PEP_COMPONENT": "anonymizer", "PEP_PACKAGE_NAME": "pgedge-anonymizer",
           "PEP_CHANNEL": "staging", "PEP_EXPECTED_VERSION": "9.8.7", "PEP_FAMILY": "rpm",
           "PEP_ARCH_FILTER": "amd64", "PEP_CONTAINER_ALIAS": "oel9-amd64", "PG_MAJOR_VERSION": "17",
           "PEP_EXPECTED_BUILDNUM": "1", "PEP_EFFECTIVE_TAG": "v9.8.7", "PEP_EXPECTED_RPM": "9.8.7-1.el9",
           "REPO": "staging", "CONTAINERS": "oel9-amd64", "DEB_CONTAINERS": "debian13-amd64",
           "DECOUPLED_COMPONENTS_SBOM": "/usr/share"}
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
_STANDALONE = {"PG_MAJOR_VERSION": "17", "PGEDGE_ANONYMIZER_VERSION": "9.8.7", "REPO": "release",
               "CONTAINERS": "oel9-amd64", "DEB_CONTAINERS": "debian13-amd64",
               "DECOUPLED_COMPONENTS_SBOM": "/usr/share"}
_KEYS = sorted(set(_GA_RPM) | set(_GA_DEB) | {
    "PEP_EXPECTED_DEB", "PEP_EXPECTED_BINARY", "PEP_MODE", "PEP_SCENARIO", "PLATFORM_FILTER",
    "PGEDGE_ANONYMIZER_VERSION", "ANONYMIZER_PACKAGE", "DEB_ANONYMIZER_PACKAGE", "UPGRADE", "UPGRADE_REPO",
    "PG_PORT", "PG_DATA_DIR", "PG_BIN_PATH", "DEB_PG_BIN_PATH", "PG_USER", "DEB_PG_USER", "SKIP_CLEANUP",
    "COMPONENT_BINARY", "COMPONENT_BINARY_VERSION"})

_STAMP = "2026-11-09T10:11:12Z"     # GoReleaser {{.Date}}: the build's UTC time in RFC 3339


def load(monkeypatch, env):
    """A fresh copy of the anonymizer component test under exactly ``env`` (None values unset)."""
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(docker, "from_env", lambda *a, **k: None)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)   # only ``env`` decides
    name = "anonymizer_wiring_under_test_%d" % next(_counter)
    spec = importlib.util.spec_from_file_location(name, str(_ANON_PATH))
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
        raw = out if isinstance(out, bytes) else (out or "").encode()    # bytes are returned as given
        if demux:
            return ec, (raw or None, None)
        return ec, raw

    def get_archive(self, *a, **k):
        raise AssertionError("a file was copied out of the container")

    def put_archive(self, *a, **k):
        raise AssertionError("a file was copied into the container")


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
    """The package-manager listing installed_anonymizer_packages reads (rpm -qa / dpkg-query -W)."""
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qa"]:
            return 0, "".join(n + "\n" for n in names)
        if cmd[:2] == ["dpkg-query", "-W"]:
            return 0, "".join(n + " install ok installed\n" for n in names)
        return 0, ""
    return respond


# --------------------------------------------------------------------------- standalone kept
def test_standalone_keeps_configured_package_and_platform_filter(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, PLATFORM_FILTER="deb", ANONYMIZER_PACKAGE="pgedge-anonymizer-custom"))
    assert m.INTEGRATION_REQUEST is None and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == [("debian13-amd64", "deb")]
    assert m.get_container_config("deb")["anonymizer_package"] == "pgedge-anonymizer-custom"
    assert m.get_container_config("rhel")["anonymizer_package"] == "pgedge-anonymizer-custom"
    m = load(monkeypatch, _STANDALONE)
    assert m.all_containers == [("oel9-amd64", "rhel"), ("debian13-amd64", "deb")]
    assert m.anonymizer_version == "9.8.7"


def test_standalone_install_is_unpinned_and_unchanged(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    m.test_component_install("oel9-amd64", "rhel")
    assert pinned.calls == [] and ip.calls == [((c, "pgedge-anonymizer"), {})]


def test_standalone_package_version_check_uses_pgedge_anonymizer_version(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    verify = _Spy((True, "rhel", "9.8.7-1.el9", "ok"))
    monkeypatch.setattr(m.package_management, "verify_package_version", verify)
    assert outcome(m.test_component_package_version, "oel9-amd64", "rhel") == ("passed", "")
    assert verify.calls[0][0][1:] == ("pgedge-anonymizer", "9.8.7")


@pytest.mark.parametrize("test,env", [
    ("test_component_package_version", dict(_STANDALONE, PGEDGE_ANONYMIZER_VERSION=None)),
    ("test_binary_version", dict(_STANDALONE, PGEDGE_ANONYMIZER_VERSION=None)),
    ("test_verify_sbom", dict(_STANDALONE, DECOUPLED_COMPONENTS_SBOM=None)),
    ("test_anonymizer_identity", _STANDALONE),
    ("test_component_upgrade", _STANDALONE),
], ids=["package-version-unset", "binary-version-unset", "sbom-dir-unset", "identity-integration-only",
        "upgrade-off"])
def test_standalone_keeps_its_documented_skips(monkeypatch, test, env):
    m = load(monkeypatch, env)
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(getattr(m, test), "oel9-amd64", "rhel")[0] == "skipped"
    assert c.cmds == []


def test_standalone_still_skips_a_missing_container_or_reference(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(missing=True))
    for test in ("test_configure_repository", "test_component_install", "test_component_package_version",
                 "test_verify_bundled_files", "test_verify_sbom", "test_binary_version", "test_binary_stripped",
                 "test_package_uninstall", "test_pgedge_cleanup"):
        assert outcome(getattr(m, test), "oel9-amd64", "rhel")[0] == "skipped", test
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for anonymizer")))
    assert outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel")[0] == "skipped"


def _version_cmd(out, rc=0):
    """`<binary> version` as the standalone check runs it (stderr merged, through bash)."""
    def respond(cmd):
        if cmd == ["bash", "-c", "%s version 2>&1" % _BIN]:
            return rc, out
        return 1, "unexpected %r" % (cmd,)
    return respond


@pytest.mark.parametrize("configured,out,status", [
    ("9.8.7", "pgedge-anonymizer 9.8.7 (built %s)\n" % _STAMP, "passed"),
    ("9.8.7", "pgedge-anonymizer 9.8.6 (built %s)\n" % _STAMP, "failed"),
    # Kept as it was: a containment check of the configured value (reported, not tightened).
    ("9.8.7", "pgedge-anonymizer 9.8.7-test1 (built %s)\n" % _STAMP, "passed"),
], ids=["match", "mismatch", "containment-kept"])
def test_standalone_binary_version_keeps_the_configured_containment_check(monkeypatch, configured, out, status):
    m = load(monkeypatch, dict(_STANDALONE, PGEDGE_ANONYMIZER_VERSION=configured))
    c = _Container(_version_cmd(out))
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_binary_version, "x", "rhel")[0] == status
    assert c.cmds == [["bash", "-c", "%s version 2>&1" % _BIN]]           # no ownership check standalone


# --------------------------------------------------------------------------- integration selection
@pytest.mark.parametrize("env,containers", [
    (dict(_GA_RPM, PLATFORM_FILTER="deb"), [("oel9-amd64", "rhel")]),
    (_GA_DEB, [("debian13-amd64", "deb")]),
], ids=["rpm-overrides-filter", "deb"])
def test_integration_request_selects_the_package_and_platform(monkeypatch, env, containers):
    m = load(monkeypatch, dict(env, ANONYMIZER_PACKAGE="pgedge-anonymizer-custom", PGEDGE_ANONYMIZER_VERSION="1.2.3"))
    assert m.INTEGRATION_REQUEST["component"] == "anonymizer" and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == containers
    assert m.get_container_config("rhel")["anonymizer_package"] == "pgedge-anonymizer"
    assert m.get_container_config("deb")["anonymizer_package"] == "pgedge-anonymizer"
    assert m.expected_anonymizer_binary_version(m.INTEGRATION_REQUEST) == ("9.8.7", [])  # never the config


def test_a_request_for_another_package_is_rejected_at_load(monkeypatch):
    with pytest.raises(Exception, match="does not match component 'anonymizer'"):
        load(monkeypatch, dict(_GA_RPM, PEP_PACKAGE_NAME="pgedge-radar"))


def test_the_backstop_names_each_request_it_cannot_serve(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    req = dict(m.INTEGRATION_REQUEST)
    assert m.integration_problem(req) is None
    assert m.integration_problem(None) is None
    assert "component 'radar' is not anonymizer" in m.integration_problem(dict(req, component="radar"))
    assert "is not an anonymizer package" in m.integration_problem(dict(req, package_name="pgedge-radar"))
    # The test never uses PostgreSQL, so the request's PG major is not a reason to refuse it.
    assert m.integration_problem(dict(req, pg_major="18")) is None
    # A valid request routed to the wrong test (here a radar request) is refused, not installed.
    m = load(monkeypatch, dict(_GA_RPM, PEP_COMPONENT="radar", PEP_PACKAGE_NAME="pgedge-radar"))
    assert "component 'radar' is not anonymizer" in m.INTEGRATION_PROBLEM


# --------------------------------------------------------------------------- install
@pytest.mark.parametrize("env,ctype,exact", [
    (_GA_RPM, "rhel", "9.8.7-1.el9"),
    (_TEST_DEB, "deb", "9.8.7~test1-1.trixie"),
], ids=["rpm", "deb"])
def test_only_the_requested_package_is_installed_pinned_with_evidence(monkeypatch, tmp_path, env, ctype, exact):
    m = load(monkeypatch, env)
    c = _Container(_pkg_list("pgedge-anonymizer", "pgedge-radar"))
    _wire(monkeypatch, tmp_path, m, c)
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)

    m.test_component_install("x", ctype)

    # No prerequisite: the test never uses PostgreSQL or any other package.
    assert ip.calls == []
    # Exactly one pinned install, into this container, of the requested package at the exact pin.
    assert pinned.calls == [((c, "pgedge-anonymizer", exact), {})]
    marker = json.loads((tmp_path / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "a" * 64
    assert not (tmp_path / "prerequisite-versions.json").exists()


def test_a_request_the_backstop_rejects_is_refused_before_any_install(monkeypatch, tmp_path):
    m = load(monkeypatch, dict(_GA_RPM, PEP_COMPONENT="radar", PEP_PACKAGE_NAME="pgedge-radar"))
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    status, msg = outcome(m.test_component_install, "oel9-amd64", "rhel")
    assert status == "failed" and "cannot be served" in msg and "is not anonymizer" in msg
    assert ip.calls == [] and pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_a_failed_pinned_install_fails_without_install_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((False, "checksum mismatch", None)))
    status, msg = outcome(m.test_component_install, "oel9-amd64", "rhel")
    assert status == "failed" and "pinned install of pgedge-anonymizer=9.8.7-1.el9 failed: checksum mismatch" in msg
    assert not (tmp_path / "install-evidence.json").exists()


@pytest.mark.parametrize("ctype,present", [
    ("rhel", ("pgedge-anonymizer", "pgedge-anonymizer-debuginfo")),   # a stray package beside the request
    ("deb", ("pgedge-anonymizer", "pgedge-anonymizer-dbgsym")),
    ("rhel", ()),                                                    # the request is not installed at all
], ids=["rpm-stray", "deb-stray", "absent"])
def test_any_anonymizer_package_other_than_the_request_fails_the_install_step(monkeypatch, tmp_path, ctype,
                                                                             present):
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(*present, "pgedge-postgresql17-server")))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((True, "ok", "a" * 64)))
    status, msg = outcome(m.test_component_install, "x", ctype)
    assert status == "failed" and "expected exactly the requested anonymizer package" in msg


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
    status, msg = outcome(m.test_anonymizer_identity, "oel9-amd64", "rhel")
    assert status == ("passed" if ok else "failed")
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")


def test_identity_without_install_evidence_fails_with_persisted_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    status, msg = outcome(m.test_anonymizer_identity, "oel9-amd64", "rhel")
    assert status == "failed" and "install-before-identity precondition failed" in msg
    assert (tmp_path / "identity-evidence.json").exists()


@pytest.mark.parametrize("test,why", [
    ("test_component_package_version", "superseded by test_anonymizer_identity"),
    ("test_component_upgrade", "certification never upgrades"),
], ids=["package-version", "upgrade"])
def test_integration_mode_only_skips_documented_superseded_or_disabled_steps(monkeypatch, test, why):
    m = load(monkeypatch, dict(_GA_RPM, UPGRADE="true", PGEDGE_ANONYMIZER_VERSION="9.8.7"))
    ip = _Spy((True, "rhel", "installed"))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "upgrade_package", ip)
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
    assert m.expected_anonymizer_binary_version(m.INTEGRATION_REQUEST) == (expected, [])


@pytest.mark.parametrize("override,problem", [
    ({"effective_tag": None}, "effective_tag is required"),
    ({"effective_tag": "v9.8"}, "is not v<major>.<minor>.<patch>"),
    ({"effective_tag": "9.8.7"}, "is not v<major>.<minor>.<patch>"),
    ({"effective_tag": "v9.8.7-beta2-test"}, "is not v<major>.<minor>.<patch>"),
    ({"effective_tag": "v9.8.8"}, "does not match expected_version '9.8.7'"),
    ({"effective_tag": "v9.8.7-rc.1"}, "expected_buildnum '1' does not match"),
    ({"expected_buildnum": "2"}, "expected_buildnum '2' does not match"),
    ({"expected_rpm": "9.8.7-rc1_1.el9"}, "expected_rpm '9.8.7-rc1_1.el9' does not match"),
    ({"expected_rpm": "9.8.7-1"}, "expected_rpm '9.8.7-1' does not match"),
], ids=["no-tag", "malformed-tag", "no-v", "hyphenated-suffix", "tag-vs-version", "tag-vs-buildnum",
        "buildnum", "rpm-string", "rpm-no-dist"])
def test_inconsistent_release_facts_are_reported(monkeypatch, override, problem):
    m = load(monkeypatch, _GA_RPM)
    expected, problems = m.expected_anonymizer_binary_version(dict(m.INTEGRATION_REQUEST, **override))
    assert expected is None and any(problem in p for p in problems), problems


def test_an_inconsistent_deb_string_is_reported(monkeypatch):
    m = load(monkeypatch, _GA_DEB)
    for wrong in ("9.8.7~rc1-1.trixie", "9.8.7-2.trixie", "9.8.7-test1-1.trixie"):
        expected, problems = m.expected_anonymizer_binary_version(dict(m.INTEGRATION_REQUEST, expected_deb=wrong))
        assert expected is None and any("expected_deb %r does not match" % wrong in p for p in problems)


# --------------------------------------------------------------------------- binary version + ownership
def _binary(version_out, owner="pgedge-anonymizer", owner_rc=0, rc=0):
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qf"]:
            return owner_rc, owner + "\n"
        if cmd[:2] == ["dpkg-query", "-S"]:
            return owner_rc, "%s: %s\n" % (owner, _BIN)
        if cmd == [_BIN, "version"]:
            return rc, version_out
        return 1, "unexpected %r" % (cmd,)
    return respond


@pytest.mark.parametrize("env,ctype,line", [
    (_GA_RPM, "rhel", "pgedge-anonymizer 9.8.7 (built %s)\n" % _STAMP),
    (_GA_DEB, "deb", "pgedge-anonymizer 9.8.7 (built %s)\n" % _STAMP),
    (_RC_RPM, "rhel", "pgedge-anonymizer 9.8.7-rc.1 (built %s)\n" % _STAMP),
    (_TEST_DEB, "deb", "pgedge-anonymizer 9.8.7-test1 (built 2026-11-09T10:11:12.5+05:00)\n"),
], ids=["ga-rpm", "ga-deb", "rc-rpm", "test-deb"])
def test_the_owned_binary_reporting_the_tag_version_passes(monkeypatch, env, ctype, line):
    m = load(monkeypatch, dict(env, PGEDGE_ANONYMIZER_VERSION="1.2.3"))
    c = _Container(_binary(line))
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_binary_version, "x", ctype) == ("passed", "")
    assert c.cmds[0][:2] in (["rpm", "-qf"], ["dpkg-query", "-S"])          # ownership first
    assert c.cmds[1] == [_BIN, "version"]                                   # stdout only, no shell


@pytest.mark.parametrize("env,out", [
    (_GA_RPM, "pgedge-anonymizer v9.8.7 (built %s)\n" % _STAMP),          # the tag, not the stamped version
    (_TEST_RPM, "pgedge-anonymizer 9.8.7 (built %s)\n" % _STAMP),         # missing suffix
    (_GA_RPM, "pgedge-anonymizer 9.8.7-test1 (built %s)\n" % _STAMP),     # extra suffix
    (_GA_RPM, "pgedge-anonymizer 9.8.8-next (built %s)\n" % _STAMP),      # a --snapshot build
    (_RC_RPM, "pgedge-anonymizer 9.8.7-rc1 (built %s)\n" % _STAMP),       # suffix without its dot
    (_GA_RPM, "pgedge-anonymizer 9.8.6 (built %s)\n" % _STAMP),           # different version
    (_GA_RPM, "pgedge-anonymizer 9.8.7.1 (built %s)\n" % _STAMP),         # expected version is only a prefix
    (_GA_RPM, "pgedge-anonymizer 9.8.7 (built unknown)\n"),               # ldflags not applied: the source
    # default is bumped with each release, so it can equal the tag; only the missing stamp fails it
    (_GA_RPM, "pgedge-anonymizer 9.8.7 (built 2026-11-09)\n"),            # not a GoReleaser build stamp
    (_GA_RPM, "pgedge-anonymizer 9.8.7\n"),                               # no build stamp at all
    (_GA_RPM, "pgedge-anonymizer 9.8.7 (built %s) extra\n" % _STAMP),     # trailing text
    (_GA_RPM, "pgedge-anonymizer 9.8.7 (built %s)\npgedge-anonymizer 9.8.6 (built %s)\n" % (_STAMP, _STAMP)),
    (_GA_RPM, "9.8.7\n"),
    (_GA_RPM, ""),
], ids=["v-prefix", "missing-suffix", "extra-suffix", "snapshot", "rc-without-dot", "different",
        "longer-version", "unstamped", "date-only", "no-stamp", "trailing-text", "two-lines", "bare-version",
        "empty"])
def test_a_binary_reporting_any_other_version_line_fails(monkeypatch, env, out):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary(out))))
    assert outcome(m.test_binary_version, "x", "rhel")[0] == "failed"


# Only lines whose first word is "pgedge-anonymizer" are version lines; any other stdout line is ignored.
_LINE = "pgedge-anonymizer 9.8.7 (built %s)\n" % _STAMP
_FOUND = "expected one 'pgedge-anonymizer <version> (built ...)' line on stdout, found %d"


@pytest.mark.parametrize("env,ctype", [(_GA_RPM, "rhel"), (_GA_DEB, "deb")], ids=["rpm", "deb"])
@pytest.mark.parametrize("out", [
    "a notice\n" + _LINE,
    _LINE + "\nA newer release is available.\n",
    "pgedge-anonymizer: a note\n" + _LINE,
], ids=["leading-notice", "trailing-blank-and-banner", "colon-prefixed-note"])
def test_unrelated_stdout_lines_around_the_one_version_line_are_ignored(monkeypatch, env, ctype, out):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary(out))))
    assert outcome(m.test_binary_version, "x", ctype) == ("passed", "")


@pytest.mark.parametrize("out,needle", [
    ("a notice\n\nA newer release is available.\n", _FOUND % 0),
    ("pgedge-anonymizer: a note\n", _FOUND % 0),
    ("a notice\n" + _LINE + _LINE, _FOUND % 2),
    (_LINE + "pgedge-anonymizer 9.8.7 built later\n", _FOUND % 2),
    ("a notice\npgedge-anonymizer 9.8.7\n", "malformed version line 'pgedge-anonymizer 9.8.7'"),
    ("a notice\npgedge-anonymizer 9.8.7 (built unknown)\n",
     "build time 'unknown' is not a release build's RFC 3339 stamp"),
], ids=["noise-without-version-line", "colon-prefixed-note-only", "duplicate", "malformed-second-version-line",
        "noise-and-malformed", "noise-and-unstamped"])
def test_unrelated_stdout_lines_never_make_a_bad_version_line_pass(monkeypatch, out, needle):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary(out))))
    status, msg = outcome(m.test_binary_version, "x", "rhel")
    assert status == "failed" and needle in msg, msg


# The build stamp must be a real RFC 3339 instant, not just its shape.
@pytest.mark.parametrize("built", [
    _STAMP,
    "2024-02-29T10:11:12Z",                   # a leap day
    "2026-12-31T23:59:59Z",
    "2026-11-09T00:00:00Z",
    "2026-11-09T10:11:12.5+05:00",            # one-digit fraction, which Python 3.9's fromisoformat refuses
    "2026-11-09T10:11:12.123456789Z",         # nanoseconds (Go's RFC3339Nano)
    "2026-11-09T10:11:12-23:59",
    "2026-11-09T10:11:12+00:00",
    "2026-11-09T10:11:12-00:00",
], ids=["utc", "leap-day", "year-end", "midnight", "short-fraction", "nanoseconds", "max-offset",
        "zero-offset", "unknown-offset"])
def test_a_real_rfc3339_build_stamp_passes(monkeypatch, built):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary("pgedge-anonymizer 9.8.7 (built %s)\n" % built))))
    assert outcome(m.test_binary_version, "x", "rhel") == ("passed", "")
    assert m.is_release_build_stamp(built)


@pytest.mark.parametrize("built", [
    "2026-02-30T10:11:12Z",                   # February 30
    "2025-02-29T10:11:12Z",                   # 2025 is not a leap year
    "2026-11-31T10:11:12Z",                   # November has 30 days
    "2026-13-09T10:11:12Z",
    "2026-00-09T10:11:12Z",
    "2026-11-00T10:11:12Z",
    "2026-11-09T24:00:00Z",
    "2026-11-09T10:60:12Z",
    "2026-11-09T10:11:60Z",                   # a leap second, which Go never writes
    "2026-11-09T10:11:12+24:00",
    "2026-11-09T10:11:12+05:60",
    "\uff12\uff10\uff12\uff16-11-09T10:11:12Z",   # 2026 in full-width digits
], ids=["feb-30", "feb-29-common-year", "nov-31", "month-13", "month-0", "day-0", "hour-24", "minute-60",
        "second-60", "offset-hour-24", "offset-minute-60", "non-ascii-digits"])
def test_an_impossible_build_stamp_fails(monkeypatch, built):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary("pgedge-anonymizer 9.8.7 (built %s)\n" % built))))
    status, msg = outcome(m.test_binary_version, "x", "rhel")
    assert status == "failed" and "build time %r is not a release build's RFC 3339 stamp" % built in msg, msg
    with pytest.raises(ValueError, match="is not a release build's RFC 3339 stamp"):
        m.parse_anonymizer_version("pgedge-anonymizer 9.8.7 (built %s)\n" % built)
    assert not m.is_release_build_stamp(built)


class _StderrContainer(_Container):
    """A _Container whose demuxed exec_run also returns ``stderr`` (text, or bytes as given) on its own
    stream."""

    def __init__(self, respond, stderr):
        super().__init__(respond)
        self.stderr = stderr

    def exec_run(self, cmd, user=None, demux=False, **kwargs):
        ec, out = super().exec_run(cmd, user=user, demux=demux, **kwargs)
        err = self.stderr if isinstance(self.stderr, bytes) else self.stderr.encode()
        return (ec, (out[0], err)) if demux else (ec, out)


@pytest.mark.parametrize("out,err,needle", [
    (_LINE, "a warning\npgedge-anonymizer 9.8.6 (built %s)\n" % _STAMP, None),
    ("", _LINE, _FOUND % 0),
], ids=["stderr-version-line-ignored", "version-only-on-stderr"])
def test_stderr_is_never_read_for_the_version(monkeypatch, capsys, out, err, needle):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_StderrContainer(_binary(out), err)))
    status, msg = outcome(m.test_binary_version, "x", "rhel")
    assert "stderr: " + err.splitlines()[0] in capsys.readouterr().out      # the stderr did reach the check
    if needle is None:
        assert (status, msg) == ("passed", "")
    else:
        assert status == "failed" and needle in msg, msg


# stderr bytes that are not UTF-8 (a Latin-1 "e acute" and a lone 0xff).
_NOT_UTF8 = b"warning: caf\xe9 \xff\n"


@pytest.mark.parametrize("out,rc,status,needle", [
    (_LINE, 0, "passed", None),
    ("pgedge-anonymizer 9.8.6 (built %s)\n" % _STAMP, 0, "failed", "anonymizer version mismatch"),
    (_LINE, 1, "failed", "exit code 1"),
], ids=["right-version", "wrong-version", "failing-command"])
def test_stderr_that_is_not_utf8_is_escaped_and_never_decides_the_verdict(monkeypatch, capsys, out, rc, status,
                                                                          needle):
    # stderr is only printed and quoted, so its bytes are escaped instead of raising UnicodeDecodeError;
    # the exit-code check and the exact stdout version check still decide.
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_StderrContainer(_binary(out, rc=rc), _NOT_UTF8)))
    got, msg = outcome(m.test_binary_version, "x", "rhel")
    assert got == status and (needle is None or needle in msg), msg
    assert "stderr: warning: caf\\xe9 \\xff" in capsys.readouterr().out
    if rc:
        assert "stderr: 'warning: caf\\\\xe9 \\\\xff\\n'" in msg                 # quoted, escaped


def test_stdout_that_is_not_utf8_is_still_decoded_strictly(monkeypatch):
    # Only stderr is escaped: stdout is what the version is read from, and bytes that are not UTF-8 there
    # still stop the check instead of being read around.
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary(_LINE.encode() + b"caf\xe9\n"))))
    with pytest.raises(UnicodeDecodeError):
        m.test_binary_version("x", "rhel")


def test_a_failing_version_command_fails(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary("pgedge-anonymizer 9.8.7 (built %s)\n" % _STAMP,
                                                                rc=1))))
    status, msg = outcome(m.test_binary_version, "x", "rhel")
    assert status == "failed" and "exit code 1" in msg


@pytest.mark.parametrize("ctype,owner,rc", [
    ("rhel", "pgedge-anonymizer-legacy", 0), ("deb", "pgedge-anonymizer-legacy", 0),
    ("rhel", "file %s is not owned by any package" % _BIN, 1),
], ids=["rpm-other-owner", "deb-other-owner", "unowned"])
def test_a_binary_not_owned_by_the_requested_package_fails(monkeypatch, ctype, owner, rc):
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    c = _Container(_binary("pgedge-anonymizer 9.8.7 (built %s)\n" % _STAMP, owner, rc))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_binary_version, "x", ctype)
    assert status == "failed" and _BIN in msg
    assert [_BIN, "version"] not in c.cmds                                  # never probed


@pytest.mark.parametrize("override,problem", [
    ({"PEP_EFFECTIVE_TAG": None}, "effective_tag is required"),
    ({"PEP_EXPECTED_BUILDNUM": "test1_1"}, "expected_buildnum 'test1_1' does not match"),
], ids=["no-tag", "disagreeing-buildnum"])
def test_a_request_whose_version_cannot_be_derived_fails_before_probing(monkeypatch, override, problem):
    m = load(monkeypatch, dict(_GA_RPM, **override))
    c = _Container(_binary("pgedge-anonymizer 9.8.7 (built %s)\n" % _STAMP))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_binary_version, "x", "rhel")
    assert status == "failed" and problem in msg and c.cmds == []


# --------------------------------------------------------------------------- stripped ELF
_FILE = "ELF 64-bit LSB executable, x86-64, version 1 (SYSV), statically linked, Go BuildID=x, BuildID[sha1]=y, "


@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
@pytest.mark.parametrize("out,problem", [
    (_FILE + "stripped", None),
    (_FILE + "not stripped", "it is not stripped"),
    (_FILE + "with debug_info, not stripped", "it is not stripped"),
    (_FILE.rstrip(", "), "does not report it as stripped"),
    ("POSIX shell script, ASCII text executable", "not an ELF binary"),
    ("cannot open `%s' (No such file or directory)" % _BIN, "not an ELF binary"),
], ids=["stripped", "not-stripped", "debug-info", "unreported", "script", "missing"])
def test_the_strip_check_requires_a_stripped_elf_in_both_modes(monkeypatch, env, out, problem):
    m = load(monkeypatch, env)
    # The old check ran `file <binary>` through bash; answer both forms so only the verdict differs.
    monkeypatch.setattr(m, "client", _Client(_Container(
        lambda cmd: (0, out + "\n") if cmd in (["file", "-b", _BIN], ["bash", "-c", "file %s 2>&1" % _BIN])
        else (1, "unexpected"))))
    assert outcome(m.test_binary_stripped, "x", "rhel")[0] == ("passed" if problem is None else "failed")
    found = m.elf_strip_problem(out)
    assert (found is None) if problem is None else (problem in found)


# --------------------------------------------------------------------------- required checks
def test_integration_mode_fails_a_missing_container(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(missing=True))
    for test in ("test_configure_repository", "test_component_install", "test_anonymizer_identity",
                 "test_verify_bundled_files", "test_verify_sbom", "test_binary_version", "test_binary_stripped",
                 "test_package_uninstall", "test_pgedge_cleanup"):
        status, msg = outcome(getattr(m, test), "oel9-amd64", "rhel")
        assert status == "failed" and "not found" in msg, test


def test_integration_mode_fails_a_missing_bundled_file_reference(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for anonymizer")))
    status, msg = outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel")
    assert status == "failed" and "No expected file found" in msg


def test_integration_mode_fails_an_unset_sbom_location(monkeypatch):
    m = load(monkeypatch, dict(_GA_RPM, DECOUPLED_COMPONENTS_SBOM=None))
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_verify_sbom, "oel9-amd64", "rhel")
    assert status == "failed" and "DECOUPLED_COMPONENTS_SBOM" in msg and c.cmds == []


@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_a_missing_or_unverified_sbom_fails_in_both_modes(monkeypatch, env):
    m = load(monkeypatch, env)

    def respond(cmd):
        if isinstance(cmd, str) and "sq verify" in cmd:
            return 1, "Error: No such file or directory\n"
        return 0, ""
    c = _Container(respond)
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_verify_sbom, "oel9-amd64", "rhel")
    assert status == "failed" and "SBOM verification failed" in msg
    assert any("/usr/share/pgedge-anonymizer" in str(cmd) and "pgedge-anonymizer-sbom.json" in str(cmd)
               for cmd in c.cmds)


# --------------------------------------------------------------------------- expected-output references
# The payload the anonymizer packaging defines at pgEdge/pgedge-anonymizer 0ba7eef:
#   RPM pkg/rpm/anonymizer.spec %install (:36-42) and %files (:44-52): %license LICENCE.md, %doc README.md,
#     /usr/bin/pgedge-anonymizer, both /etc/pgedge yamls (%config(noreplace)), the SBOM pair;
#   DEB pkg/deb/debian/rules override_dh_auto_install (:19-23): the binary, both /etc/pgedge yamls, the SBOM
#     pair; pkg/deb/debian/docs: LICENCE.md (962 bytes, left uncompressed) and README.md (4291 bytes,
#     dh_compress -> README.md.gz) under /usr/share/doc/pgedge-anonymizer/.
_RPM_PAYLOAD = ["/etc/pgedge/pgedge-anonymizer.yaml", "/etc/pgedge/pgedge-anonymizer-patterns.yaml",
                "/usr/bin/pgedge-anonymizer", "/usr/share/doc/pgedge-anonymizer/README.md",
                "/usr/share/pgedge-anonymizer/pgedge-anonymizer-sbom.json",
                "/usr/share/pgedge-anonymizer/pgedge-anonymizer-sbom.json.asc",
                "/usr/share/licenses/pgedge-anonymizer/LICENCE.md"]
_DEB_PAYLOAD = ["/etc/pgedge/pgedge-anonymizer.yaml", "/etc/pgedge/pgedge-anonymizer-patterns.yaml",
                "/usr/bin/pgedge-anonymizer", "/usr/share/doc/pgedge-anonymizer/LICENCE.md",
                "/usr/share/doc/pgedge-anonymizer/README.md.gz",
                "/usr/share/pgedge-anonymizer/pgedge-anonymizer-sbom.json",
                "/usr/share/pgedge-anonymizer/pgedge-anonymizer-sbom.json.asc"]
_RPM_EXTRAS = ["/etc/pgedge", "/usr/share/doc/pgedge-anonymizer", "/usr/share/licenses/pgedge-anonymizer",
               "/usr/lib/.build-id", "/usr/lib/.build-id/28",
               "/usr/lib/.build-id/28/fb947599f6e6f95c9ae070fac5a48f93c08854"]
_DEB_EXTRAS = ["/.", "/etc", "/etc/pgedge", "/usr", "/usr/bin", "/usr/share", "/usr/share/doc",
               "/usr/share/doc/pgedge-anonymizer", "/usr/share/doc/pgedge-anonymizer/changelog.Debian.gz",
               "/usr/share/pgedge-anonymizer"]


def test_the_expected_output_references_list_the_packaged_payload():
    assert (_REPO / "expected-output" / "rpm" / "anonymizer").read_text().splitlines() == _RPM_PAYLOAD
    assert (_REPO / "expected-output" / "deb" / "anonymizer").read_text().splitlines() == _DEB_PAYLOAD


def _listing(cmd_text, files):
    return lambda cmd: (0, "".join(f + "\n" for f in files)) if cmd == cmd_text else (1, "unexpected")


@pytest.mark.parametrize("ctype,listing_cmd,payload,extras", [
    ("rhel", "rpm -ql pgedge-anonymizer", _RPM_PAYLOAD, _RPM_EXTRAS),
    ("deb", "dpkg -L pgedge-anonymizer", _DEB_PAYLOAD, _DEB_EXTRAS),
], ids=["rpm", "deb"])
@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_the_real_bundled_file_check_finds_the_anonymizer_reference(monkeypatch, env, ctype, listing_cmd, payload,
                                                                    extras):
    # The real verify_bundled_files derives "anonymizer" from pgedge-anonymizer and compares the package's
    # file list against expected-output/<family>/anonymizer: every listed file must be installed. Entries the
    # package manager adds around the payload (directories, RPM build-id links, the Debian changelog) are
    # reported as extra.
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_listing(listing_cmd, payload + extras))))
    assert outcome(m.test_verify_bundled_files, "x", ctype) == ("passed", "")
    for missing in (payload[1], payload[2], payload[-1]):     # patterns file, binary, last entry
        monkeypatch.setattr(m, "client", _Client(_Container(
            _listing(listing_cmd, [f for f in payload + extras if f != missing]))))
        status, msg = outcome(m.test_verify_bundled_files, "x", ctype)
        assert status == "failed" and "Missing files" in msg, missing


def test_a_deb_licence_under_usr_share_licenses_still_matches(monkeypatch):
    # The DEB reference now names /usr/share/doc/pgedge-anonymizer/LICENCE.md (debian/docs). A package that
    # installs LICENCE.md under /usr/share/licenses instead still passes: the shared Debian basename
    # heuristic accepts the single LICENCE.md, exactly as it accepted the old reference against the
    # /usr/share/doc layout. So that correction cannot change a standalone verdict either way.
    m = load(monkeypatch, _STANDALONE)
    moved = [("/usr/share/licenses/pgedge-anonymizer/LICENCE.md" if f.endswith("/LICENCE.md") else f)
             for f in _DEB_PAYLOAD]
    monkeypatch.setattr(m, "client", _Client(_Container(_listing("dpkg -L pgedge-anonymizer", moved))))
    assert outcome(m.test_verify_bundled_files, "x", "deb") == ("passed", "")
