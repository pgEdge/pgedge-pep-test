"""Docker-free tests that exercise the REAL functions in component-test/test_pep_docloader.py.

The module is loaded by path with docker.from_env and load_dotenv neutralized and with the environment
of the case under test (the integration request, platform filter and binary settings are decided at
import time), then driven with fake containers and spied package helpers -- the approach of
test_pep_radar_wiring.py. Versions are synthetic (9.8.7), never a real docloader release. Covers:
  * standalone behaviour kept (configured package, PLATFORM_FILTER, unpinned install, the documented
    standalone skips, no binary check unless DOCLOADER_BINARY_VERSION is set);
  * integration mode: the request selects the package and platform; a request the test cannot serve is
    refused before anything is installed; the requested package is the only install (docloader needs
    no PostgreSQL server or other prerequisite), pinned, with checksum evidence; only the requested
    docloader package may be installed; the shared identity step; ownership of /usr/bin/pgedge-docloader
    and an exact `pgedge-docloader version` derived from the request's effective tag;
  * required checks fail instead of skipping in integration mode (missing container, reference or SBOM
    configuration);
  * the expected-output references and the SBOM directory derived from docloader's packaging.
  * the served enterprise-built RPMs (1.0.0-1, 1.0.0-beta1_1, old SBOM layout): a standalone run checks
    them against their own exact inventory; integration mode always demands the in-repo layout.
The existing test has no strip check and none is added, so there is nothing to cover for it here.
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
_DOCLOADER_PATH = _REPO / "component-test" / "test_pep_docloader.py"
_counter = itertools.count()
_BIN = "/usr/bin/pgedge-docloader"

# docloader 9.8.7 (synthetic) as published in the staging channel (tag v9.8.7, build number 1).
_GA_RPM = {"PEP_INTEGRATION_MODE": "1", "PEP_COMPONENT": "docloader", "PEP_PACKAGE_NAME": "pgedge-docloader",
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
# Release candidates: v9.8.7-rc1 (no dot) and v9.8.7-rc.1 both give build number rc1_1; the binary
# keeps the tag's suffix exactly as written.
_RC1_RPM = dict(_GA_RPM, PEP_EXPECTED_BUILDNUM="rc1_1", PEP_EFFECTIVE_TAG="v9.8.7-rc1",
                PEP_EXPECTED_RPM="9.8.7-rc1_1.el9")
_RC_DOT_RPM = dict(_RC1_RPM, PEP_EFFECTIVE_TAG="v9.8.7-rc.1")
_RC_DOT_DEB = dict(_GA_DEB, PEP_EXPECTED_BUILDNUM="rc1_1", PEP_EFFECTIVE_TAG="v9.8.7-rc.1",
                   PEP_EXPECTED_DEB="9.8.7~rc1-1.trixie")
_STANDALONE = {"PG_MAJOR_VERSION": "17", "PGEDGE_DOCLOADER_VERSION": "9.8.7", "REPO": "release",
               "CONTAINERS": "oel9-amd64", "DEB_CONTAINERS": "debian13-amd64",
               "DECOUPLED_COMPONENTS_SBOM": "/usr/share"}
_KEYS = sorted(set(_GA_RPM) | set(_GA_DEB) | set(_STANDALONE) | {
    "PEP_EXPECTED_DEB", "PEP_EXPECTED_BINARY", "PEP_MODE", "PEP_SCENARIO", "PLATFORM_FILTER",
    "PGEDGE_DOCLOADER_VERSION", "DOCLOADER_PACKAGE", "DEB_DOCLOADER_PACKAGE", "DOCLOADER_BINARY_VERSION",
    "COMPONENT_BINARY", "COMPONENT_BINARY_VERSION", "UPGRADE", "UPGRADE_REPO", "PG_PORT", "PG_DATA_DIR",
    "PG_BIN_PATH", "DEB_PG_BIN_PATH", "PG_USER", "DEB_PG_USER", "SKIP_CLEANUP", "COMPONENTS",
    "CHECK_EXTENSIONS", "BASE_EXTENSIONS"})


def load(monkeypatch, env):
    """A fresh copy of the docloader component test under exactly ``env`` (None values unset)."""
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(docker, "from_env", lambda *a, **k: None)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)   # only ``env`` decides
    name = "docloader_wiring_under_test_%d" % next(_counter)
    spec = importlib.util.spec_from_file_location(name, str(_DOCLOADER_PATH))
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


class _Container:
    """A running container whose exec_run answers from ``respond(cmd) -> (exit_code, text)``."""
    status = "running"

    def __init__(self, respond):
        self.respond, self.cmds = respond, []

    def exec_run(self, cmd, user=None, demux=False, **kwargs):
        self.cmds.append(cmd)
        ec, out = self.respond(cmd)
        if demux:
            return ec, ((out or "").encode() or None, None)
        return ec, (out or "").encode()


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
    """The package-manager listing installed_docloader_packages reads (rpm -qa / dpkg-query -W)."""
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qa"]:
            return 0, "".join(n + "\n" for n in names)
        if cmd[:2] == ["dpkg-query", "-W"]:
            return 0, "".join(n + " install ok installed\n" for n in names)
        return 0, ""
    return respond


# --------------------------------------------------------------------------- standalone kept
def test_standalone_keeps_configured_package_and_platform_filter(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, PLATFORM_FILTER="deb", DEB_DOCLOADER_PACKAGE="pgedge-docloader-custom"))
    assert m.INTEGRATION_REQUEST is None and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == [("debian13-amd64", "deb")]
    assert m.get_container_config("deb")["docloader_package"] == "pgedge-docloader-custom"
    m = load(monkeypatch, dict(_STANDALONE, DOCLOADER_PACKAGE="pgedge-docloader-custom"))
    assert m.all_containers == [("oel9-amd64", "rhel"), ("debian13-amd64", "deb")]
    assert m.get_container_config("rhel")["docloader_package"] == "pgedge-docloader-custom"


def test_standalone_install_is_unpinned_and_unchanged(monkeypatch, tmp_path):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(_nothing)
    _wire(monkeypatch, tmp_path, m, c)
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    assert outcome(m.test_component_install, "oel9-amd64", "rhel") == ("passed", "")
    assert pinned.calls == [] and ip.calls == [((c, "pgedge-docloader"), {})]
    assert not (tmp_path / "install-evidence.json").exists()


def test_standalone_package_version_check_uses_pgedge_docloader_version(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    verify = _Spy((True, "rhel", "9.8.7-1.el9", "ok"))
    monkeypatch.setattr(m.package_management, "verify_package_version", verify)
    assert outcome(m.test_component_package_version, "oel9-amd64", "rhel") == ("passed", "")
    assert verify.calls[0][0][1:] == ("pgedge-docloader", "9.8.7")


@pytest.mark.parametrize("test,env", [
    ("test_component_package_version", dict(_STANDALONE, PGEDGE_DOCLOADER_VERSION=None)),
    ("test_docloader_binary_version", _STANDALONE),
    ("test_docloader_identity", _STANDALONE),
    ("test_component_upgrade", _STANDALONE),
    ("test_verify_sbom", dict(_STANDALONE, DECOUPLED_COMPONENTS_SBOM=None)),
], ids=["package-version-unset", "binary-version-unset", "identity-integration-only", "upgrade-off",
        "sbom-path-unset"])
def test_standalone_keeps_its_documented_skips(monkeypatch, test, env):
    m = load(monkeypatch, env)
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(getattr(m, test), "oel9-amd64", "rhel")[0] == "skipped"
    assert c.cmds == []


def test_standalone_still_skips_a_missing_container_or_reference(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, DOCLOADER_BINARY_VERSION="9.8.7"))
    monkeypatch.setattr(m, "client", _Client(missing=True))
    for test in ("test_configure_repository", "test_component_install", "test_component_package_version",
                 "test_verify_bundled_files", "test_verify_sbom", "test_docloader_binary_version",
                 "test_package_uninstall", "test_pgedge_cleanup"):
        assert outcome(getattr(m, test), "oel9-amd64", "rhel")[0] == "skipped", test
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for docloader")))
    assert outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel")[0] == "skipped"


# --------------------------------------------------------------------------- integration selection
@pytest.mark.parametrize("env,containers", [
    (dict(_GA_RPM, PLATFORM_FILTER="deb"), [("oel9-amd64", "rhel")]),
    (_GA_DEB, [("debian13-amd64", "deb")]),
], ids=["rpm-overrides-filter", "deb"])
def test_integration_request_selects_the_package_and_platform(monkeypatch, env, containers):
    m = load(monkeypatch, dict(env, DOCLOADER_PACKAGE="pgedge-docloader-custom",
                               DEB_DOCLOADER_PACKAGE="pgedge-docloader-custom", PGEDGE_DOCLOADER_VERSION="1.2.3",
                               DOCLOADER_BINARY_VERSION="1.2.3"))
    assert m.INTEGRATION_REQUEST["component"] == "docloader" and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == containers
    assert m.get_container_config("rhel")["docloader_package"] == "pgedge-docloader"
    assert m.get_container_config("deb")["docloader_package"] == "pgedge-docloader"
    assert m.expected_docloader_version() == ("9.8.7", None)    # never the config versions


def test_a_request_for_another_package_is_rejected_at_load(monkeypatch):
    with pytest.raises(Exception, match="does not match component 'docloader'"):
        load(monkeypatch, dict(_GA_RPM, PEP_PACKAGE_NAME="pgedge-radar"))


def test_the_backstop_names_each_request_it_cannot_serve(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    req = dict(m.INTEGRATION_REQUEST)
    assert m.integration_problem(req) is None
    assert m.integration_problem(None) is None
    assert "component 'radar' is not docloader" in m.integration_problem(dict(req, component="radar"))
    assert "is not a docloader package" in m.integration_problem(dict(req, package_name="pgedge-radar"))
    # The test never uses PostgreSQL, so the leg's PG major does not change what it can serve.
    for pg in ("16", "17", "18"):
        assert m.integration_problem(dict(req, pg_major=pg)) is None
    # A valid request routed to the wrong test (here a radar request) is refused, not installed.
    m = load(monkeypatch, dict(_GA_RPM, PEP_COMPONENT="radar", PEP_PACKAGE_NAME="pgedge-radar"))
    assert "component 'radar' is not docloader" in m.INTEGRATION_PROBLEM


# --------------------------------------------------------------------------- install
def test_only_the_requested_package_is_installed_pinned_with_checksum_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-docloader")))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)

    assert outcome(m.test_component_install, "oel9-amd64", "rhel") == ("passed", "")

    # docloader needs no PostgreSQL server or other pgEdge prerequisite: nothing is installed but the
    # requested package, pinned to the request's exact version.
    assert ip.calls == []
    assert pinned.calls[0][0][1:] == ("pgedge-docloader", "9.8.7-1.el9")
    marker = json.loads((tmp_path / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "a" * 64
    assert not (tmp_path / "prerequisite-versions.json").exists()


def test_a_deb_test_build_is_pinned_to_its_tilde_version(monkeypatch, tmp_path):
    m = load(monkeypatch, _TEST_DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-docloader")))
    ip, pinned = _Spy((True, "debian", "installed")), _Spy((True, "ok", "b" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    assert outcome(m.test_component_install, "debian13-amd64", "deb") == ("passed", "")
    assert ip.calls == [] and pinned.calls[0][0][1:] == ("pgedge-docloader", "9.8.7~test1-1.trixie")


def test_a_request_the_backstop_rejects_is_refused_before_any_install(monkeypatch, tmp_path):
    m = load(monkeypatch, dict(_GA_RPM, PEP_COMPONENT="radar", PEP_PACKAGE_NAME="pgedge-radar"))
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    status, msg = outcome(m.test_component_install, "oel9-amd64", "rhel")
    assert status == "failed" and "cannot be served" in msg and "is not docloader" in msg
    assert ip.calls == [] and pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_a_failed_pinned_install_fails_without_install_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((False, "checksum mismatch", None)))
    status, msg = outcome(m.test_component_install, "oel9-amd64", "rhel")
    assert status == "failed" and "pinned install of pgedge-docloader=9.8.7-1.el9 failed: checksum mismatch" in msg
    assert not (tmp_path / "install-evidence.json").exists()


@pytest.mark.parametrize("ctype,present", [
    ("rhel", ("pgedge-docloader", "pgedge-docloader-debuginfo")),   # a stray docloader package beside it
    ("deb", ("pgedge-docloader", "pgedge-docloader-dbgsym")),
    ("rhel", ()),                                                  # the request is not installed at all
], ids=["rpm-stray", "deb-stray", "absent"])
def test_any_docloader_package_other_than_the_request_fails_the_install_step(monkeypatch, tmp_path, ctype,
                                                                             present):
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(*present, "pgedge-radar")))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((True, "ok", "a" * 64)))
    status, msg = outcome(m.test_component_install, "x", ctype)
    assert status == "failed" and "expected exactly the requested docloader package" in msg


def test_another_components_package_does_not_count_as_docloader(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-docloader", "pgedge-radar", "pgedge-anonymizer")))
    assert m.installed_docloader_packages(m.client.containers.get("x"), "rhel") == ["pgedge-docloader"]


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
    c = _Container(_nothing)
    _wire(monkeypatch, tmp_path, m, c)
    m.pep_integration_mode.pep_evidence.write_install_evidence(
        m.INTEGRATION_REQUEST, "run-A", "pinned", "9.8.7-1.el9", str(tmp_path / "install-evidence.json"),
        installed_sha256="a" * 64)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: observed)
    status, msg = outcome(m.test_docloader_identity, "oel9-amd64", "rhel")
    assert status == ("passed" if ok else "failed")
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")
    assert c.cmds == []        # the shared `<binary> -version` probe is not used (docloader has a subcommand)


def test_identity_without_install_evidence_fails_with_persisted_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    status, msg = outcome(m.test_docloader_identity, "oel9-amd64", "rhel")
    assert status == "failed" and "install-before-identity precondition failed" in msg
    assert (tmp_path / "identity-evidence.json").exists()


@pytest.mark.parametrize("test,why", [
    ("test_component_package_version", "superseded by test_docloader_identity"),
    ("test_component_upgrade", "certification never upgrades"),
], ids=["package-version", "upgrade"])
def test_integration_mode_only_skips_documented_superseded_or_disabled_steps(monkeypatch, test, why):
    m = load(monkeypatch, dict(_GA_RPM, UPGRADE="true", PGEDGE_DOCLOADER_VERSION="9.8.7"))
    ip, up = _Spy((True, "rhel", "installed")), _Spy((True, "rhel", "upgraded"))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "upgrade_package", up)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    status, msg = outcome(getattr(m, test), "oel9-amd64", "rhel")
    assert status == "skipped" and why in msg and ip.calls == [] and up.calls == []


# --------------------------------------------------------------------------- expected binary version
@pytest.mark.parametrize("env,expected", [
    (_GA_RPM, "9.8.7"),
    (_GA_DEB, "9.8.7"),
    (_RC1_RPM, "9.8.7-rc1"),
    (_RC_DOT_RPM, "9.8.7-rc.1"),
    (_RC_DOT_DEB, "9.8.7-rc.1"),
    (_TEST_RPM, "9.8.7-test1"),
    (_TEST_DEB, "9.8.7-test1"),
    (dict(_GA_RPM, PEP_EXPECTED_BUILDNUM=None, PEP_EXPECTED_RPM=None), "9.8.7"),
], ids=["ga-rpm", "ga-deb", "rc1-rpm", "rc.1-rpm", "rc.1-deb", "test-rpm", "test-deb", "tag-only"])
def test_the_expected_binary_version_is_the_effective_tag_without_v(monkeypatch, env, expected):
    m = load(monkeypatch, env)
    assert m.expected_docloader_binary_version(m.INTEGRATION_REQUEST) == (expected, [])


@pytest.mark.parametrize("override,problem", [
    ({"effective_tag": None}, "effective_tag is required"),
    ({"effective_tag": "v9.8"}, "is not v<major>.<minor>.<patch>"),
    ({"effective_tag": "v9.8.7-rc-1"}, "is not v<major>.<minor>.<patch>"),
    ({"effective_tag": "v9.8.7+build1"}, "is not v<major>.<minor>.<patch>"),
    ({"effective_tag": "v9.8.8"}, "does not match expected_version '9.8.7'"),
    ({"effective_tag": "v9.8.7-rc.1"}, "expected_buildnum '1' does not match"),
    ({"expected_buildnum": "2"}, "expected_buildnum '2' does not match"),
    ({"expected_rpm": "9.8.7-rc1_1.el9"}, "expected_rpm '9.8.7-rc1_1.el9' does not match"),
    ({"expected_rpm": "9.8.7-1"}, "expected_rpm '9.8.7-1' does not match"),
    ({"expected_rpm": "9.8.7-10.el9"}, "expected_rpm '9.8.7-10.el9' does not match"),
], ids=["no-tag", "malformed-tag", "hyphenated-suffix", "build-metadata", "tag-vs-version", "tag-vs-buildnum",
        "buildnum", "rpm-string", "rpm-no-dist", "rpm-other-release"])
def test_inconsistent_release_facts_are_reported(monkeypatch, override, problem):
    m = load(monkeypatch, _GA_RPM)
    expected, problems = m.expected_docloader_binary_version(dict(m.INTEGRATION_REQUEST, **override))
    assert expected is None and any(problem in p for p in problems), problems


def test_an_inconsistent_deb_string_is_reported(monkeypatch):
    m = load(monkeypatch, _GA_DEB)
    for wrong in ("9.8.7~rc1-1.trixie", "9.8.7-2.trixie", "9.8.7-test1-1.trixie", "9.8.7-1"):
        expected, problems = m.expected_docloader_binary_version(dict(m.INTEGRATION_REQUEST, expected_deb=wrong))
        assert expected is None and any("expected_deb %r does not match" % wrong in p for p in problems)


# --------------------------------------------------------------------------- binary version + ownership
_SHA = "0123456789abcdef0123456789abcdef01234567"
_DATE = "2026-11-09T10:11:12Z"


def _line(version, commit=_SHA, date=_DATE):
    """What `pgedge-docloader version` prints (cmd/pgedge-docloader/main.go)."""
    return "pgedge-docloader %s (commit: %s, built: %s)\n" % (version, commit, date)


def _binary(version_out, owner="pgedge-docloader", owner_rc=0, rc=0):
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
    (_GA_RPM, "rhel", _line("9.8.7")),
    (_GA_DEB, "deb", _line("9.8.7")),
    (_RC1_RPM, "rhel", _line("9.8.7-rc1")),
    (_RC_DOT_DEB, "deb", _line("9.8.7-rc.1")),
    (_TEST_DEB, "deb", _line("9.8.7-test1")),
], ids=["ga-rpm", "ga-deb", "rc1-rpm", "rc.1-deb", "test-deb"])
def test_the_owned_binary_reporting_the_tag_version_passes(monkeypatch, env, ctype, line):
    m = load(monkeypatch, dict(env, PGEDGE_DOCLOADER_VERSION="1.2.3", DOCLOADER_BINARY_VERSION="1.2.3"))
    c = _Container(_binary(line))
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_docloader_binary_version, "x", ctype) == ("passed", "")
    assert c.cmds[0][:2] in (["rpm", "-qf"], ["dpkg-query", "-S"])          # ownership first
    assert c.cmds[0][-1] == _BIN and c.cmds[1] == [_BIN, "version"]


@pytest.mark.parametrize("env,out", [
    (_GA_RPM, _line("v9.8.7")),                         # prefix: the tag, not the stamped version
    (_TEST_RPM, _line("9.8.7")),                        # missing suffix
    (_GA_RPM, _line("9.8.7-test1")),                    # extra suffix
    (_GA_RPM, _line("9.8.7-dirty")),                    # extra suffix (local build)
    (_RC_DOT_RPM, _line("9.8.7-rc1")),                  # suffix without its dot
    (_RC1_RPM, _line("9.8.7-rc.1")),                    # suffix with a dot the tag does not have
    (_GA_RPM, _line("9.8.6")),                          # different version
    (_GA_RPM, _line("9.8.7.1")),                        # expected version is only a prefix
    (_GA_RPM, _line("9.8.8-next")),                     # a --snapshot build
    (_GA_RPM, _line("9.8.7", commit="none", date="unknown")),   # unstamped (main.go defaults)
    (_GA_RPM, _line("9.8.7", commit="none")),
    (_GA_RPM, _line("9.8.7", date="unknown")),
    (_GA_RPM, "pgedge-docloader 9.8.7\n"),              # no commit/build stamp
    (_GA_RPM, _line("9.8.7").rstrip("\n") + " extra\n"),   # trailing text
    (_GA_RPM, _line("9.8.7") + _line("9.8.6")),
    (_GA_RPM, "9.8.7\n"),
    (_GA_RPM, ""),
], ids=["v-prefix", "missing-suffix", "extra-suffix", "dirty", "rc-without-dot", "rc-with-dot", "different",
        "longer-version", "snapshot", "unstamped", "no-commit", "no-date", "no-stamp", "trailing-text",
        "two-lines", "no-version-line", "empty"])
def test_a_binary_reporting_any_other_version_fails(monkeypatch, env, out):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary(out))))
    assert outcome(m.test_docloader_binary_version, "x", "rhel")[0] == "failed"


def test_a_failing_version_command_fails(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary(_line("9.8.7"), rc=1))))
    status, msg = outcome(m.test_docloader_binary_version, "x", "rhel")
    assert status == "failed" and "exit code 1" in msg


@pytest.mark.parametrize("ctype,owner,rc", [
    ("rhel", "pgedge-docloader-legacy", 0), ("deb", "pgedge-docloader-legacy", 0),
    ("rhel", "file /usr/bin/pgedge-docloader is not owned by any package", 1),
], ids=["rpm-other-owner", "deb-other-owner", "unowned"])
def test_a_binary_not_owned_by_the_requested_package_fails(monkeypatch, ctype, owner, rc):
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    c = _Container(_binary(_line("9.8.7"), owner, rc))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_docloader_binary_version, "x", ctype)
    assert status == "failed" and _BIN in msg
    assert [_BIN, "version"] not in c.cmds                                  # never probed


def test_a_request_whose_facts_disagree_fails_the_binary_check_before_probing(monkeypatch):
    for env, why in ((dict(_GA_RPM, PEP_EFFECTIVE_TAG=None), "effective_tag is required"),
                     (dict(_GA_RPM, PEP_EXPECTED_BUILDNUM="test1_1"), "expected_buildnum 'test1_1' does not match")):
        m = load(monkeypatch, env)
        c = _Container(_binary(_line("9.8.7")))
        monkeypatch.setattr(m, "client", _Client(c))
        status, msg = outcome(m.test_docloader_binary_version, "x", "rhel")
        assert status == "failed" and why in msg and c.cmds == []


@pytest.mark.parametrize("env,line,status", [
    (dict(_STANDALONE, DOCLOADER_BINARY_VERSION="9.8.7-test1"), _line("9.8.7-test1"), "passed"),
    (dict(_STANDALONE, DOCLOADER_BINARY_VERSION="9.8.7"), _line("9.8.7-test1"), "failed"),
], ids=["binary-version", "standalone-mismatch"])
def test_standalone_binary_version_uses_the_configured_binary_version(monkeypatch, env, line, status):
    m = load(monkeypatch, env)
    c = _Container(_binary(line))
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_docloader_binary_version, "x", "rhel")[0] == status
    assert c.cmds == [[_BIN, "version"]]                                   # no ownership check standalone


@pytest.mark.parametrize("env", [
    _STANDALONE,
    # The shared COMPONENT_BINARY / COMPONENT_BINARY_VERSION are last-wins across the config sections
    # (config*.env sets COMPONENT_BINARY=/usr/bin/radar), so they never configure docloader's check.
    dict(_STANDALONE, COMPONENT_BINARY="/usr/bin/radar", COMPONENT_BINARY_VERSION="9.8.7"),
], ids=["package-version-only", "shared-component-binary"])
def test_standalone_runs_no_binary_check_unless_docloader_binary_version_is_set(monkeypatch, env):
    # PGEDGE_DOCLOADER_VERSION is the PACKAGE version (9.8.7 in _STANDALONE); a prerelease binary reports
    # 9.8.7-test1 while its packages are 9.8.7-test1_1 / 9.8.7~test1-1, so it is no binary expectation.
    m = load(monkeypatch, env)
    assert m.docloader_version == "9.8.7" and m.expected_docloader_version() == (None, None)
    c = _Container(_binary(_line("9.8.7")))
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_docloader_binary_version, "x", "rhel")[0] == "skipped"
    assert c.cmds == []


# --------------------------------------------------------------------------- required checks
def test_integration_mode_fails_a_missing_container(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(missing=True))
    for test in ("test_configure_repository", "test_component_install", "test_docloader_identity",
                 "test_verify_bundled_files", "test_verify_sbom", "test_docloader_binary_version",
                 "test_package_uninstall", "test_pgedge_cleanup"):
        status, msg = outcome(getattr(m, test), "oel9-amd64", "rhel")
        assert status == "failed" and "not found" in msg, test


def test_integration_mode_fails_a_missing_bundled_file_reference(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for docloader")))
    status, msg = outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel")
    assert status == "failed" and "No expected file found" in msg


def test_integration_mode_fails_a_missing_sbom_configuration(monkeypatch):
    m = load(monkeypatch, dict(_GA_RPM, DECOUPLED_COMPONENTS_SBOM=None))
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_verify_sbom, "oel9-amd64", "rhel")
    assert status == "failed" and "DECOUPLED_COMPONENTS_SBOM" in msg and c.cmds == []


# --------------------------------------------------------------------------- SBOM
_SBOM_DIR = "/usr/share/pgedge-docloader"


def _sbom(missing=(), sq_rc=0, sq_out="1 authenticated signature.\n"):
    """Answers for the SBOM check: ``missing`` names files absent from the SBOM directory."""
    def respond(cmd):
        if isinstance(cmd, list) and cmd[:2] == ["test", "-f"]:
            return (1, "") if cmd[2].rsplit("/", 1)[1] in missing else (0, "")
        text = cmd if isinstance(cmd, str) else " ".join(cmd)
        if text.startswith("wget "):
            return 0, ""
        if text.startswith("sq verify --help"):
            return 0, "--signer-file --signature-file"
        if "sq verify" in text:
            return sq_rc, sq_out
        return 0, ""
    return respond


@pytest.mark.parametrize("env,ctype", [(_GA_RPM, "rhel"), (_GA_DEB, "deb"), (_STANDALONE, "rhel"),
                                        (_STANDALONE, "deb")],
                         ids=["integration-rpm", "integration-deb", "standalone-rpm", "standalone-deb"])
def test_the_sbom_is_verified_in_the_packaged_directory_on_both_families(monkeypatch, env, ctype):
    # docloader.spec %install/%files and debian/rules both install the SBOM and its signature under
    # /usr/share/pgedge-docloader/ (RPM and DEB alike).
    m = load(monkeypatch, env)
    c = _Container(_sbom())
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_verify_sbom, "x", ctype) == ("passed", "")
    verify = [cmd for cmd in c.cmds if isinstance(cmd, str) and "sq verify" in cmd and "--help" not in cmd]
    assert len(verify) == 1 and ("cd %s && sq verify" % _SBOM_DIR) in verify[0]
    assert "pgedge-docloader-sbom.json.asc" in verify[0] and verify[0].rstrip("'").endswith("pgedge-docloader-sbom.json")
    if ctype == "rhel":
        assert any(isinstance(cmd, str) and cmd.startswith("wget ") and ("-O %s/pgedge-rsa.pub" % _SBOM_DIR) in cmd
                   for cmd in c.cmds)


@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
@pytest.mark.parametrize("missing", ["pgedge-docloader-sbom.json", "pgedge-docloader-sbom.json.asc"])
def test_a_missing_sbom_file_fails_in_both_modes(monkeypatch, env, missing):
    # Both docloader packages ship the SBOM, so its absence is a package defect, never a skip. A package
    # outside the legacy set with the pre-2026-04-17 RPM layout (/usr/share/pgedge-docloader-sbom.json)
    # fails here.
    m = load(monkeypatch, env)
    c = _Container(_sbom(missing=(missing,)))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_verify_sbom, "oel9-amd64", "rhel")
    assert status == "failed" and ("%s/%s is missing" % (_SBOM_DIR, missing)) in msg
    assert not any(isinstance(cmd, str) and "sq verify" in cmd for cmd in c.cmds)


def test_a_bad_sbom_signature_fails(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_sbom(sq_rc=1, sq_out="Error: no good signature"))))
    status, msg = outcome(m.test_verify_sbom, "oel9-amd64", "rhel")
    assert status == "failed" and "SBOM verification failed" in msg


# --------------------------------------------------------------------------- expected-output references
# The payload docloader's packaging defines at pgEdge/pgedge-docloader 7ce99c8:
#   RPM pkg/rpm/docloader.spec %files (l.36-42): %license LICENCE.md, %doc README.md, %{_bindir}/%{sname},
#     %config(noreplace) %{_sysconfdir}/pgedge/docloader/config.yml, %{_datadir}/%{sname}/%{sname}-sbom.json{,.asc};
#   DEB pkg/deb/debian/rules override_dh_auto_install (l.19-22) for the binary, config and SBOM, debian/docs
#     (LICENCE.md, 962 bytes, under dh_compress's 4 KiB threshold) and the changelog build-deb.sh writes (l.67-73).
_RPM_PAYLOAD = ["/etc/pgedge/docloader/config.yml", "/usr/bin/pgedge-docloader",
                "/usr/share/doc/pgedge-docloader/README.md", "/usr/share/licenses/pgedge-docloader",
                "/usr/share/licenses/pgedge-docloader/LICENCE.md",
                "/usr/share/pgedge-docloader/pgedge-docloader-sbom.json",
                "/usr/share/pgedge-docloader/pgedge-docloader-sbom.json.asc"]
_DEB_PAYLOAD = ["/etc/pgedge/docloader/config.yml", "/usr/bin/pgedge-docloader",
                "/usr/share/doc/pgedge-docloader/LICENCE.md", "/usr/share/doc/pgedge-docloader/changelog.Debian.gz",
                "/usr/share/pgedge-docloader", "/usr/share/pgedge-docloader/pgedge-docloader-sbom.json",
                "/usr/share/pgedge-docloader/pgedge-docloader-sbom.json.asc"]


def test_the_expected_output_references_list_the_packaged_payload():
    assert (_REPO / "expected-output" / "rpm" / "docloader").read_text().splitlines() == _RPM_PAYLOAD
    assert (_REPO / "expected-output" / "deb" / "docloader").read_text().splitlines() == _DEB_PAYLOAD


@pytest.mark.parametrize("ctype,listing_cmd,payload,extras", [
    ("rhel", "rpm -ql pgedge-docloader", _RPM_PAYLOAD, ["/usr/share/doc/pgedge-docloader"]),
    ("deb", "dpkg -L pgedge-docloader", _DEB_PAYLOAD,
     ["/.", "/etc", "/etc/pgedge", "/etc/pgedge/docloader", "/usr", "/usr/bin", "/usr/share", "/usr/share/doc",
      "/usr/share/doc/pgedge-docloader"]),
], ids=["rpm", "deb"])
@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_the_real_bundled_file_check_finds_the_docloader_reference(monkeypatch, env, ctype, listing_cmd, payload,
                                                                   extras):
    # The real verify_bundled_files derives "docloader" from pgedge-docloader and compares the package's
    # file list against expected-output/<family>/docloader: every listed file must be installed. Entries
    # the package manager adds around the payload (directories) are reported as extra.
    m = load(monkeypatch, env)

    def installed(files):
        return lambda cmd: (0, "".join(f + "\n" for f in files)) if cmd == listing_cmd else (1, "unexpected")
    monkeypatch.setattr(m, "client", _Client(_Container(installed(payload + extras))))
    assert outcome(m.test_verify_bundled_files, "x", ctype) == ("passed", "")
    for missing in (payload[0], payload[-1]):
        monkeypatch.setattr(m, "client", _Client(_Container(installed([f for f in payload if f != missing]))))
        status, msg = outcome(m.test_verify_bundled_files, "x", ctype)
        assert status == "failed" and "Missing files" in msg, missing


def test_an_rpm_with_the_old_top_level_sbom_layout_fails_the_bundled_file_check(monkeypatch):
    # The layout the in-repo spec replaced (pgedge-enterprise-packages before d80f6b7, 2026-04-17), on a
    # package whose version-release is not one of the served legacy builds (unreadable here).
    m = load(monkeypatch, _STANDALONE)
    old = [f for f in _RPM_PAYLOAD if not f.startswith("/usr/share/pgedge-docloader/")] + [
        "/usr/share/pgedge-docloader-sbom.json", "/usr/share/pgedge-docloader-sbom.json.asc"]
    monkeypatch.setattr(m, "client", _Client(_Container(
        lambda cmd: (0, "".join(f + "\n" for f in old)) if cmd == "rpm -ql pgedge-docloader" else (1, ""))))
    status, msg = outcome(m.test_verify_bundled_files, "x", "rhel")
    assert status == "failed" and "/usr/share/pgedge-docloader/pgedge-docloader-sbom.json" in msg


# --------------------------------------------------------------------------- served enterprise-built RPMs
# The docloader RPMs the channels serve today were built by pgedge-enterprise-packages before its d80f6b7
# (2026-04-17) moved the SBOM into /usr/share/pgedge-docloader/. The saved dnf filelists (release, staging
# and daily, 2026-11-09) show exactly two docloader version-releases, 1.0.0-1 and 1.0.0-beta1_1, both with
# the SBOM directly under /usr/share; docloader's own spec has always used the subdirectory. A standalone
# run compares those two version-releases with their own (legacy) inventory and everything else with the
# in-repo one. Integration mode certifies docloader's own builds and never consults the legacy set.
_LEGACY_RPM_PAYLOAD = ["/etc/pgedge/docloader/config.yml", "/usr/bin/pgedge-docloader",
                       "/usr/share/doc/pgedge-docloader/README.md", "/usr/share/licenses/pgedge-docloader",
                       "/usr/share/licenses/pgedge-docloader/LICENCE.md",
                       "/usr/share/pgedge-docloader-sbom.json", "/usr/share/pgedge-docloader-sbom.json.asc"]
_VR_QUERY = ["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", "pgedge-docloader"]
# A v1.0.0 request (the only GA whose number matches a legacy build): docloader's own pipeline would build it.
_V100_RPM = dict(_GA_RPM, PEP_EXPECTED_VERSION="1.0.0", PEP_EFFECTIVE_TAG="v1.0.0", PEP_EXPECTED_RPM="1.0.0-1.el9")


def _rpm(version_release, files=(), missing=()):
    """An RPM container: the installed version-release (None = query fails), its file list and the SBOM
    answers (``missing`` names SBOM files absent from where the check looks)."""
    sbom = _sbom(missing=missing)

    def respond(cmd):
        if cmd == _VR_QUERY:
            return (0, version_release) if version_release else (1, "package pgedge-docloader is not installed")
        if cmd == "rpm -ql pgedge-docloader":
            return 0, "".join(f + "\n" for f in files)
        return sbom(cmd)
    return respond


def test_the_legacy_reference_lists_the_served_enterprise_built_payload():
    # Exactly the saved filelist of pgedge-docloader 1.0.0-1.el9 without the build-id links, and what
    # expected-output/rpm/docloader listed before the in-repo layout replaced it.
    assert (_REPO / "expected-output" / "rpm" / "docloader-enterprise-legacy").read_text().splitlines() \
        == _LEGACY_RPM_PAYLOAD


@pytest.mark.parametrize("integration,vr,layout", [
    (False, "1.0.0-1.el9", "enterprise-legacy"), (False, "1.0.0-1.el10", "enterprise-legacy"),
    (False, "1.0.0-beta1_1.el9", "enterprise-legacy"),
    (False, "1.0.0-2.el9", "in-repo"), (False, "1.0.0-test1_1.el9", "in-repo"), (False, "1.0.0-1", "in-repo"),
    (False, "11.0.0-1.el9", "in-repo"), (False, "9.8.7-1.el9", "in-repo"), (False, None, "in-repo"),
    (False, "", "in-repo"),
    (True, "1.0.0-1.el9", "in-repo"), (True, "1.0.0-beta1_1.el9", "in-repo"), (True, None, "in-repo")])
def test_the_rpm_reference_layout_rule(monkeypatch, integration, vr, layout):
    m = load(monkeypatch, _STANDALONE)
    assert m.rpm_reference_layout(integration, vr) == layout


@pytest.mark.parametrize("vr", ["1.0.0-1.el9", "1.0.0-1.el10", "1.0.0-beta1_1.el9"])
def test_a_standalone_run_checks_a_served_enterprise_built_rpm_against_its_own_inventory(monkeypatch, vr):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(_rpm(vr, _LEGACY_RPM_PAYLOAD + ["/usr/share/doc/pgedge-docloader"]))
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_verify_bundled_files, "x", "rhel") == ("passed", "")
    assert outcome(m.test_verify_sbom, "x", "rhel") == ("passed", "")
    verify = [cmd for cmd in c.cmds if isinstance(cmd, str) and "sq verify" in cmd]
    assert len(verify) == 1 and "cd /usr/share && sq verify" in verify[0]
    # Its own inventory is still exact: a missing legacy file fails.
    for missing in ("/usr/share/pgedge-docloader-sbom.json", "/usr/bin/pgedge-docloader"):
        monkeypatch.setattr(m, "client", _Client(_Container(
            _rpm(vr, [f for f in _LEGACY_RPM_PAYLOAD if f != missing]))))
        status, msg = outcome(m.test_verify_bundled_files, "x", "rhel")
        assert status == "failed" and missing in msg, missing
    monkeypatch.setattr(m, "client", _Client(_Container(
        _rpm(vr, _LEGACY_RPM_PAYLOAD, missing=("pgedge-docloader-sbom.json.asc",)))))
    status, msg = outcome(m.test_verify_sbom, "x", "rhel")
    assert status == "failed" and "/usr/share/pgedge-docloader-sbom.json.asc is missing" in msg


@pytest.mark.parametrize("vr", ["1.0.0-1.el9", "1.0.0-beta1_1.el9"])
def test_a_legacy_version_release_with_the_in_repo_layout_fails_standalone(monkeypatch, vr):
    # Fail closed: the legacy set names packages, not layouts. A rebuild reusing a legacy version-release
    # with the in-repo layout is reported, never accepted under either inventory.
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(
        _rpm(vr, _RPM_PAYLOAD, missing=("pgedge-docloader-sbom.json", "pgedge-docloader-sbom.json.asc")))))
    status, msg = outcome(m.test_verify_bundled_files, "x", "rhel")
    assert status == "failed" and "/usr/share/pgedge-docloader-sbom.json" in msg
    assert outcome(m.test_verify_sbom, "x", "rhel")[0] == "failed"


@pytest.mark.parametrize("vr", ["1.0.0-2.el9", "1.0.0-test1_1.el9", "9.8.7-1.el9", None])
def test_any_other_standalone_rpm_must_have_the_in_repo_layout(monkeypatch, vr):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_rpm(vr, _RPM_PAYLOAD))))
    assert outcome(m.test_verify_bundled_files, "x", "rhel") == ("passed", "")
    monkeypatch.setattr(m, "client", _Client(_Container(
        _rpm(vr, _LEGACY_RPM_PAYLOAD, missing=("pgedge-docloader-sbom.json", "pgedge-docloader-sbom.json.asc")))))
    status, msg = outcome(m.test_verify_bundled_files, "x", "rhel")
    assert status == "failed" and "/usr/share/pgedge-docloader/pgedge-docloader-sbom.json" in msg
    status, msg = outcome(m.test_verify_sbom, "x", "rhel")
    assert status == "failed" and "/usr/share/pgedge-docloader/pgedge-docloader-sbom.json is missing" in msg


def test_integration_mode_always_demands_the_in_repo_layout_and_never_asks_the_version(monkeypatch):
    # Even a v1.0.0 request whose installed version-release equals a legacy build's: integration mode
    # certifies docloader's own build, whose spec has always used /usr/share/pgedge-docloader/.
    m = load(monkeypatch, _V100_RPM)
    c = _Container(_rpm("1.0.0-1.el9", _LEGACY_RPM_PAYLOAD,
                        missing=("pgedge-docloader-sbom.json", "pgedge-docloader-sbom.json.asc")))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_verify_bundled_files, "x", "rhel")
    assert status == "failed" and "/usr/share/pgedge-docloader/pgedge-docloader-sbom.json" in msg
    status, msg = outcome(m.test_verify_sbom, "x", "rhel")
    assert status == "failed" and "/usr/share/pgedge-docloader/pgedge-docloader-sbom.json is missing" in msg
    assert _VR_QUERY not in c.cmds
    monkeypatch.setattr(m, "client", _Client(_Container(_rpm("1.0.0-1.el9", _RPM_PAYLOAD))))
    assert outcome(m.test_verify_bundled_files, "x", "rhel") == ("passed", "")
    assert outcome(m.test_verify_sbom, "x", "rhel") == ("passed", "")


@pytest.mark.parametrize("env", [_GA_DEB, _STANDALONE], ids=["integration", "standalone"])
def test_deb_runs_never_ask_the_rpm_version(monkeypatch, env):
    m = load(monkeypatch, env)
    c = _Container(_sbom())
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_verify_sbom, "x", "deb") == ("passed", "")
    assert _VR_QUERY not in c.cmds
