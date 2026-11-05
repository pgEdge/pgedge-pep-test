"""Docker-free tests that exercise the REAL functions in component-test/test_pep_ace.py.

The module is loaded by path with docker.from_env neutralized and with the environment of the case
under test (the integration request, platform filter and Spock selection are decided at import
time), then driven with fake containers and spied package helpers -- the approach of
test_pep_rag_wiring.py and test_pep_spock_wiring.py. Covers:
  * standalone behaviour kept (configured package, PLATFORM_FILTER, SPOCK_MAJOR, unpinned install,
    the documented standalone skips);
  * integration mode: the request selects the package and platform, the PostgreSQL/Spock
    prerequisites install before the pinned ACE package and their versions are recorded, the shared
    identity step, ownership of /usr/bin/ace, and an exact binary version derived from the request's
    effective tag (prerelease punctuation kept, cross-checked with the other release facts);
  * required checks fail instead of skipping in integration mode (missing reference, SBOM
    configuration, container or effective tag);
  * the functional steps: ACE's init commands must succeed, the seeded rows are checked exactly,
    and table-diff must leave exactly one fresh report whose counts match the seed.
"""
import importlib.util
import itertools
import json
import os
import shutil
import subprocess
import sys

import docker
import pytest

_ACE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "component-test", "test_pep_ace.py")
_counter = itertools.count()

# ACE 3.0.0 as published in the release channel (tag v3.0.0, build number 1).
_GA_RPM = {"PEP_INTEGRATION_MODE": "1", "PEP_COMPONENT": "ace", "PEP_PACKAGE_NAME": "pgedge-ace",
           "PEP_CHANNEL": "release", "PEP_EXPECTED_VERSION": "3.0.0", "PEP_FAMILY": "rpm",
           "PEP_ARCH_FILTER": "arm64", "PEP_CONTAINER_ALIAS": "rocky9-arm64", "PG_MAJOR_VERSION": "17",
           "PEP_EXPECTED_BUILDNUM": "1", "PEP_EFFECTIVE_TAG": "v3.0.0", "PEP_EXPECTED_RPM": "3.0.0-1.el9",
           "REPO": "release", "CONTAINERS": "rocky9-arm64", "DEB_CONTAINERS": "debian12-arm64"}
_GA_DEB = dict(_GA_RPM, PEP_FAMILY="deb", PEP_CONTAINER_ALIAS="debian12-arm64", PEP_EXPECTED_RPM=None,
               PEP_EXPECTED_DEB="3.0.0-1.bookworm")
# A future release candidate (tag v3.0.1-rc.1 -> build number rc1_1; the binary keeps the dot).
_RC_RPM = dict(_GA_RPM, PEP_CHANNEL="staging", REPO="staging", PEP_EXPECTED_VERSION="3.0.1",
               PEP_EXPECTED_BUILDNUM="rc1_1", PEP_EFFECTIVE_TAG="v3.0.1-rc.1",
               PEP_EXPECTED_RPM="3.0.1-rc1_1.el9")
_STANDALONE = {"PG_MAJOR_VERSION": "17", "PGEDGE_ACE_VERSION": "3.0.0",
               "CONTAINERS": "rocky9-arm64", "DEB_CONTAINERS": "debian12-arm64"}
_KEYS = sorted(set(_GA_RPM) | set(_GA_DEB) | {
    "PEP_EXPECTED_DEB", "PEP_EXPECTED_BINARY", "PEP_MODE", "PEP_SCENARIO", "PLATFORM_FILTER",
    "SPOCK_MAJOR", "PGEDGE_ACE_VERSION", "ACE_PACKAGE", "DEB_ACE_PACKAGE", "DECOUPLED_COMPONENTS_SBOM",
    "UPGRADE", "ACE_WORK_DIR"})


def load(monkeypatch, env):
    """A fresh copy of the ACE component test under exactly ``env`` (None values unset)."""
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(docker, "from_env", lambda *a, **k: None)
    name = "ace_wiring_under_test_%d" % next(_counter)
    spec = importlib.util.spec_from_file_location(name, _ACE_PATH)
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


# --------------------------------------------------------------------------- standalone kept
def test_standalone_keeps_configured_package_platform_filter_and_spock_selection(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, PLATFORM_FILTER="deb", SPOCK_MAJOR="60",
                               DEB_ACE_PACKAGE="pgedge-ace-custom"))
    assert m.INTEGRATION_REQUEST is None
    assert m.all_containers == [("debian12-arm64", "deb")]
    assert m.get_container_config("deb")["ace_package"] == "pgedge-ace-custom"
    assert m._ace_spock_packages("rhel") == ["pgedge-spock60_17", "pgedge-postgresql17-contrib"]
    assert m._ace_spock_packages("deb") == ["pgedge-postgresql-17-spock60", "pgedge-postgresql-17"]
    m = load(monkeypatch, _STANDALONE)
    assert m.all_containers == [("rocky9-arm64", "rhel"), ("debian12-arm64", "deb")]
    assert m._ace_spock_packages("rhel")[0] == "pgedge-spock50_17"


def test_standalone_install_is_unpinned_and_unchanged(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    m.test_component_install("rocky9-arm64", "rhel")
    assert pinned.calls == [] and ip.calls == [((m.client.containers.get("x"), "pgedge-ace"), {})]


@pytest.mark.parametrize("test,env", [
    ("test_ace_binary_version", dict(_STANDALONE, PGEDGE_ACE_VERSION=None)),
    ("test_component_package_version", dict(_STANDALONE, PGEDGE_ACE_VERSION=None)),
    ("test_verify_sbom", _STANDALONE),
    ("test_ace_identity", _STANDALONE),
], ids=["binary-version-unset", "package-version-unset", "sbom-path-unset", "identity-integration-only"])
def test_standalone_keeps_its_documented_skips(monkeypatch, test, env):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    assert outcome(getattr(m, test), "rocky9-arm64", "rhel")[0] == "skipped"


def test_standalone_still_skips_a_missing_container_or_reference(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(missing=True))
    assert outcome(m.test_ace_help, "rocky9-arm64", "rhel")[0] == "skipped"
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for pgedge-ace")))
    assert outcome(m.test_verify_bundled_files, "rocky9-arm64", "rhel")[0] == "skipped"


# --------------------------------------------------------------------------- integration selection
@pytest.mark.parametrize("env,containers", [
    (dict(_GA_RPM, PLATFORM_FILTER="deb"), [("rocky9-arm64", "rhel")]),
    (_GA_DEB, [("debian12-arm64", "deb")]),
], ids=["rpm-overrides-filter", "deb"])
def test_integration_request_selects_the_package_platform_and_spock_5(monkeypatch, env, containers):
    m = load(monkeypatch, dict(env, SPOCK_MAJOR="60", ACE_PACKAGE="pgedge-ace-custom",
                               DEB_ACE_PACKAGE="pgedge-ace-custom"))
    assert m.INTEGRATION_REQUEST["component"] == "ace"
    assert m.all_containers == containers
    assert m.get_container_config("rhel")["ace_package"] == "pgedge-ace"
    assert m.get_container_config("deb")["ace_package"] == "pgedge-ace"
    assert m.ace_spock_major == "50"                     # never SPOCK_MAJOR in integration mode


def test_a_request_for_another_package_is_rejected_at_load(monkeypatch):
    with pytest.raises(Exception, match="does not match component 'ace'"):
        load(monkeypatch, dict(_GA_RPM, PEP_PACKAGE_NAME="pgedge-rag-server2"))


# --------------------------------------------------------------------------- install
def test_prerequisites_install_first_then_the_pinned_request_and_versions_are_recorded(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    order = []
    ip = _Spy((True, "rhel", "installed"), order, "prerequisites")
    pinned = _Spy((True, "ok", "a" * 64), order, "pinned")
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    versions = {"pgedge-ace": "3.0.0-1.el9", "pgedge-spock50_17": "5.0.13-1.el9"}
    monkeypatch.setattr(m.package_management, "query_installed_version",
                        lambda c, n: versions.get(n, "17.11-1PGEDGE.el9"))

    m.test_component_install("rocky9-arm64", "rhel")

    assert order == ["prerequisites", "pinned"]
    assert ip.calls[0][1]["package_name"] == [
        "pgedge-postgresql17-server", "pgedge-postgresql17-contrib", "pgedge-spock50_17"]
    assert pinned.calls[0][0][1:] == ("pgedge-ace", "3.0.0-1.el9")
    marker = json.loads((tmp_path / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "a" * 64
    prereq = json.loads((tmp_path / "prerequisite-versions.json").read_text())
    assert prereq == {"channel": "release", "pg_major": "17", "spock_series": "50",
                      "requested_package": "pgedge-ace",
                      "installed_versions": {"pgedge-postgresql17-server": "17.11-1PGEDGE.el9",
                                             "pgedge-postgresql17-contrib": "17.11-1PGEDGE.el9",
                                             "pgedge-spock50_17": "5.0.13-1.el9",
                                             "pgedge-ace": "3.0.0-1.el9"}}


def test_deb_prerequisites_are_the_server_and_spock_packages(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    ip, pinned = _Spy((True, "debian", "installed")), _Spy((True, "ok", "b" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    m.test_component_install("debian12-arm64", "deb")
    assert ip.calls[0][1]["package_name"] == ["pgedge-postgresql-17", "pgedge-postgresql-17-spock50"]
    assert pinned.calls[0][0][1:] == ("pgedge-ace", "3.0.0-1.bookworm")


def test_a_missing_prerequisite_is_reported_as_such_and_ace_is_not_installed(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    pinned = _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package",
                        _Spy(Exception("Failed to install pgedge-spock50_17: No match")))
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    status, msg = outcome(m.test_component_install, "rocky9-arm64", "rhel")
    assert status == "failed" and "prerequisites unavailable from the 'release' channel" in msg
    assert pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_a_failed_pinned_install_fails_without_install_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((False, "checksum mismatch", None)))
    status, msg = outcome(m.test_component_install, "rocky9-arm64", "rhel")
    assert status == "failed" and "pinned install of pgedge-ace=3.0.0-1.el9 failed: checksum mismatch" in msg
    assert not (tmp_path / "install-evidence.json").exists()


def test_the_cluster_step_does_not_reinstall_prerequisites_in_integration_mode(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    assert m._cluster_prerequisites_to_install("rhel") == []
    m = load(monkeypatch, dict(_STANDALONE, SPOCK_MAJOR="60"))
    assert m._cluster_prerequisites_to_install("rhel") == ["pgedge-spock60_17", "pgedge-postgresql17-contrib"]


# --------------------------------------------------------------------------- identity
@pytest.mark.parametrize("observed,ok", [("3.0.0-1.el9", True), ("2.1.1-1.el9", False),
                                         ("3.0.0-rc3_1.el9", False)], ids=["exact", "older", "rc"])
def test_identity_proves_the_exact_requested_package_version(monkeypatch, tmp_path, observed, ok):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    m.pep_integration_mode.pep_evidence.write_install_evidence(
        m.INTEGRATION_REQUEST, "run-A", "pinned", "3.0.0-1.el9", str(tmp_path / "install-evidence.json"),
        installed_sha256="a" * 64)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: observed)
    status, msg = outcome(m.test_ace_identity, "rocky9-arm64", "rhel")
    assert status == ("passed" if ok else "failed")
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")


def test_identity_without_install_evidence_fails_with_persisted_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    status, msg = outcome(m.test_ace_identity, "rocky9-arm64", "rhel")
    assert status == "failed" and "install-before-identity precondition failed" in msg
    assert (tmp_path / "identity-evidence.json").exists()


@pytest.mark.parametrize("test,why", [
    ("test_component_package_version", "superseded by test_ace_identity"),
    ("test_component_upgrade", "certification never upgrades"),
], ids=["package-version", "upgrade"])
def test_integration_mode_only_skips_documented_superseded_or_disabled_steps(monkeypatch, test, why):
    m = load(monkeypatch, dict(_GA_RPM, UPGRADE="true", PGEDGE_ACE_VERSION="3.0.0"))
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    status, msg = outcome(getattr(m, test), "rocky9-arm64", "rhel")
    assert status == "skipped" and why in msg


# --------------------------------------------------------------------------- expected binary version
@pytest.mark.parametrize("env,expected", [
    (_GA_RPM, "3.0.0"),
    (_GA_DEB, "3.0.0"),
    (_RC_RPM, "3.0.1-rc.1"),
    (dict(_RC_RPM, PEP_FAMILY="deb", PEP_CONTAINER_ALIAS="ubuntu2404-arm64", PEP_EXPECTED_RPM=None,
          PEP_EXPECTED_DEB="3.0.1~rc1-1.noble"), "3.0.1-rc.1"),
    (dict(_GA_DEB, PEP_CHANNEL="daily", REPO="daily", PEP_EXPECTED_VERSION="3.0.1", PEP_EXPECTED_BUILDNUM="test1_1",
          PEP_EFFECTIVE_TAG="v3.0.1-test1", PEP_EXPECTED_DEB="3.0.1~test1-1.bookworm"), "3.0.1-test1"),
    (dict(_GA_RPM, PEP_EXPECTED_BUILDNUM=None, PEP_EXPECTED_RPM=None), "3.0.0"),
], ids=["ga-rpm", "ga-deb", "rc-rpm", "rc-deb", "test-deb", "tag-only"])
def test_the_expected_binary_version_is_the_effective_tag_without_v(monkeypatch, env, expected):
    m = load(monkeypatch, env)
    assert m.expected_ace_binary_version(m.INTEGRATION_REQUEST) == (expected, [])


@pytest.mark.parametrize("override,problem", [
    ({"effective_tag": None}, "effective_tag is required"),
    ({"effective_tag": "v3.0"}, "is not v<major>.<minor>.<patch>"),
    ({"effective_tag": "v3.0.1"}, "does not match expected_version '3.0.0'"),
    ({"effective_tag": "v3.0.0-rc.3"}, "expected_buildnum '1' does not match"),
    ({"expected_buildnum": "2"}, "expected_buildnum '2' does not match"),
    ({"expected_rpm": "3.0.0-rc3_1.el9"}, "expected_rpm '3.0.0-rc3_1.el9' does not match"),
    ({"expected_rpm": "3.0.0-1"}, "expected_rpm '3.0.0-1' does not match"),
], ids=["no-tag", "malformed-tag", "tag-vs-version", "tag-vs-buildnum", "buildnum", "rpm-string",
        "rpm-no-dist"])
def test_inconsistent_release_facts_are_reported(monkeypatch, override, problem):
    m = load(monkeypatch, _GA_RPM)
    expected, problems = m.expected_ace_binary_version(dict(m.INTEGRATION_REQUEST, **override))
    assert expected is None and any(problem in p for p in problems), problems


def test_an_inconsistent_deb_string_is_reported(monkeypatch):
    m = load(monkeypatch, _GA_DEB)
    for wrong in ("3.0.0~rc3-1.bookworm", "3.0.0-2.bookworm"):
        expected, problems = m.expected_ace_binary_version(dict(m.INTEGRATION_REQUEST, expected_deb=wrong))
        assert expected is None and any("expected_deb %r does not match" % wrong in p for p in problems)


# --------------------------------------------------------------------------- binary version + ownership
def _binary(version_line, owner="pgedge-ace", owner_rc=0):
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qf"]:
            return owner_rc, owner + "\n"
        if cmd[:2] == ["dpkg-query", "-S"]:
            return owner_rc, "%s: /usr/bin/ace\n" % owner
        if cmd == ["/usr/bin/ace", "--version"]:
            return 0, version_line
        return 1, "unexpected %r" % (cmd,)
    return respond


@pytest.mark.parametrize("env,ctype,line", [
    (_GA_RPM, "rhel", "ace version 3.0.0\n"),
    (_GA_DEB, "deb", "ace version 3.0.0\n"),
    (_RC_RPM, "rhel", "ace version 3.0.1-rc.1\n"),
], ids=["ga-rpm", "ga-deb", "rc-rpm"])
def test_the_owned_binary_reporting_the_tag_version_passes(monkeypatch, env, ctype, line):
    m = load(monkeypatch, dict(env, PGEDGE_ACE_VERSION="9.9.9"))    # the config value is ignored
    c = _Container(_binary(line))
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_ace_binary_version, "x", ctype) == ("passed", "")
    assert c.cmds[0][:2] in (["rpm", "-qf"], ["dpkg-query", "-S"])          # ownership first


@pytest.mark.parametrize("env,line", [
    (_GA_RPM, "ace version 3.0.0-rc.3\n"),
    (_GA_RPM, "ace version 3.0.0-dirty\n"),
    (_RC_RPM, "ace version 3.0.1-rc1\n"),
    (_RC_RPM, "ace version 3.0.1\n"),
    (_GA_RPM, "ace version unknown\n"),
], ids=["ga-vs-rc", "dirty", "rc-without-dot", "rc-vs-ga", "unparsable"])
def test_a_binary_reporting_any_other_version_fails(monkeypatch, env, line):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary(line))))
    assert outcome(m.test_ace_binary_version, "x", "rhel")[0] == "failed"


@pytest.mark.parametrize("ctype,owner,rc", [
    ("rhel", "pgedge-ace-legacy", 0), ("deb", "pgedge-ace-legacy", 0),
    ("rhel", "file /usr/bin/ace is not owned by any package", 1),
], ids=["rpm-other-owner", "deb-other-owner", "unowned"])
def test_a_binary_not_owned_by_the_requested_package_fails(monkeypatch, ctype, owner, rc):
    env = _GA_RPM if ctype == "rhel" else _GA_DEB
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_binary("ace version 3.0.0\n", owner, rc))))
    status, msg = outcome(m.test_ace_binary_version, "x", ctype)
    assert status == "failed" and "/usr/bin/ace" in msg


def test_a_request_without_an_effective_tag_fails_the_binary_check(monkeypatch):
    m = load(monkeypatch, dict(_GA_RPM, PEP_EFFECTIVE_TAG=None))
    monkeypatch.setattr(m, "client", _Client(_Container(_binary("ace version 3.0.0\n"))))
    status, msg = outcome(m.test_ace_binary_version, "x", "rhel")
    assert status == "failed" and "effective_tag is required" in msg


# --------------------------------------------------------------------------- required checks
def test_integration_mode_fails_a_missing_container(monkeypatch):
    m = load(monkeypatch, dict(_GA_RPM, DECOUPLED_COMPONENTS_SBOM="/usr/share"))
    monkeypatch.setattr(m, "client", _Client(missing=True))
    for test in ("test_configure_repository", "test_component_install", "test_verify_bundled_files",
                 "test_verify_sbom", "test_ace_help", "test_ace_binary_version", "test_ace_table_diff"):
        status, msg = outcome(getattr(m, test), "rocky9-arm64", "rhel")
        assert status == "failed" and "not found" in msg, test


def test_integration_mode_fails_a_missing_bundled_file_reference(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for pgedge-ace")))
    status, msg = outcome(m.test_verify_bundled_files, "rocky9-arm64", "rhel")
    assert status == "failed" and "No expected file found" in msg


def test_integration_mode_fails_a_missing_sbom_configuration(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    status, msg = outcome(m.test_verify_sbom, "rocky9-arm64", "rhel")
    assert status == "failed" and "DECOUPLED_COMPONENTS_SBOM" in msg


# --------------------------------------------------------------------------- functional: init
def _config_responder(cluster_rc=0, config_rc=0, created=True):
    def respond(cmd):
        if cmd[0] == "psql":
            return 0, "CREATE EXTENSION\n"
        script = cmd[-1]
        if " cluster init " in script:
            return cluster_rc, ("Wrote pg service file\n" if cluster_rc == 0
                                else "pg service file already exists (use --force to overwrite)\n")
        if " config init " in script:
            return config_rc, ("Wrote config file\n" if config_rc == 0
                               else "config file already exists (use --force to overwrite)\n")
        if script.startswith("test -s "):
            return (0 if created else 1), ""
        return 0, ""
    return respond


@pytest.mark.parametrize("env", [_STANDALONE, _GA_RPM], ids=["standalone", "integration"])
def test_ace_init_commands_must_succeed_and_create_their_files(monkeypatch, env):
    m = load(monkeypatch, env)
    c = _Container(_config_responder())
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_ace_functional_create_config, "x", "rhel") == ("passed", "")
    scripts = [cmd[-1] for cmd in c.cmds if cmd[0] == "bash"]
    reset = next(i for i, s in enumerate(scripts) if "rm -rf -- " in s)
    cluster = next(i for i, s in enumerate(scripts) if " cluster init " in s)
    config = next(i for i, s in enumerate(scripts) if " config init " in s)
    assert reset < cluster < config                       # a fresh work directory first
    assert "--force" not in scripts[cluster] and "--force" not in scripts[config]
    for name in ("cluster", "config"):
        m2 = load(monkeypatch, env)
        bad = {"cluster_rc": 1} if name == "cluster" else {"config_rc": 1}
        monkeypatch.setattr(m2, "client", _Client(_Container(_config_responder(**bad))))
        status, msg = outcome(m2.test_ace_functional_create_config, "x", "rhel")
        assert status == "failed" and "already exists" in msg, name
    m3 = load(monkeypatch, env)
    monkeypatch.setattr(m3, "client", _Client(_Container(_config_responder(created=False))))
    assert outcome(m3.test_ace_functional_create_config, "x", "rhel")[0] == "failed"


# --------------------------------------------------------------------------- functional: seed
def test_the_seed_constants_give_the_documented_counts(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    assert m.expected_seed_after_setup() == {n: {"initial_row": 10} for n in ("n1", "n2", "n3")}
    assert m.expected_seed_after_divergence() == {
        "n1": {"initial_row": 10, "n1_load": 100},
        "n2": {"initial_row": 10, "n2_divergent": 5},
        "n3": {"initial_row": 10, "n3_divergent": 5}}
    assert m.expected_table_diff_counts() == {"n1/n2": 100, "n1/n3": 100, "n2/n3": 5}


def _label_rows(counts):
    return "".join("%s|%d\n" % (label, n) for label, n in sorted(counts.items()))


def _seed_responder(by_port, disable_rc=0, enabled="0"):
    """psql answers for the divergence step: label counts per node port (after the inserts)."""
    def respond(cmd):
        if cmd[0] != "psql":
            return 0, ""
        port, sql = cmd[cmd.index("-p") + 1], cmd[-1]
        if "sub_disable" in sql:
            return disable_rc, ("DO\n" if disable_rc == 0 else "ERROR: permission denied\n")
        if "sub_enabled" in sql:
            return 0, enabled + "\n"
        if "regexp_replace" in sql:
            return 0, _label_rows(by_port[port])
        return 0, "INSERT 0 5\n"
    return respond


_DIVERGED = {"5431": {"initial_row": 10, "n1_load": 100}, "5432": {"initial_row": 10, "n2_divergent": 5},
             "5433": {"initial_row": 10, "n3_divergent": 5}}


@pytest.mark.parametrize("env", [_STANDALONE, _GA_RPM], ids=["standalone", "integration"])
def test_divergence_step_checks_the_exact_seeded_rows(monkeypatch, env):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "client", _Client(_Container(_seed_responder(_DIVERGED))))
    assert outcome(m.test_ace_functional_create_divergence, "x", "rhel") == ("passed", "")
    leaked = dict(_DIVERGED, **{"5432": {"initial_row": 10, "n1_load": 100, "n2_divergent": 5}})
    monkeypatch.setattr(m, "client", _Client(_Container(_seed_responder(leaked))))
    status, msg = outcome(m.test_ace_functional_create_divergence, "x", "rhel")
    assert status == "failed" and "n2" in msg and "n1_load" in msg


@pytest.mark.parametrize("over,msg", [({"disable_rc": 1}, "disable"), ({"enabled": "1"}, "still enabled")],
                         ids=["disable-fails", "still-enabled"])
def test_divergence_step_fails_when_subscriptions_stay_active(monkeypatch, over, msg):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "client", _Client(_Container(_seed_responder(_DIVERGED, **over))))
    status, text = outcome(m.test_ace_functional_create_divergence, "x", "rhel")
    assert status == "failed" and msg in text


def test_initial_replication_must_reach_exactly_the_seeded_rows(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    synced = {"5431": {"initial_row": 10}, "5432": {"initial_row": 10}, "5433": {"initial_row": 10}}
    expected = m.expected_seed_after_setup()
    assert m.wait_for_seed(_Container(_seed_responder(synced)), "postgres", expected, timeout=0) == expected
    partial = dict(synced, **{"5433": {"initial_row": 9}})
    with pytest.raises(AssertionError, match="n3"):
        m.wait_for_seed(_Container(_seed_responder(partial)), "postgres", expected, timeout=0)
    extra = dict(synced, **{"5432": {"initial_row": 11}})
    with pytest.raises(AssertionError, match="n2"):
        m.wait_for_seed(_Container(_seed_responder(extra)), "postgres", expected, timeout=0)


# --------------------------------------------------------------------------- functional: table-diff
def _report(counts=None, **summary):
    s = {"schema": "public", "table": "n1", "nodes": ["n1", "n2", "n3"], "diff_row_limit_reached": False,
         "diff_rows_count": counts if counts is not None else {"n1/n2": 100, "n1/n3": 100, "n2/n3": 5}}
    s.update(summary)
    return {"diffs": {}, "summary": s}


def _diff_responder(reports, run_rc=0, leftover="", reset_rc=0):
    """bash/cat answers for the table-diff step. ``reports`` maps file name -> JSON text written
    by the run; ``leftover`` is what the reset prints (the directory listing right after it)."""
    state = {"ran": False}

    def respond(cmd):
        if cmd[0] == "cat":
            name = os.path.basename(cmd[1])
            return (0, reports[name]) if state["ran"] and name in reports else (1, "No such file")
        script = cmd[-1]
        if "rm -rf -- " in script:
            return reset_rc, leftover
        if " table-diff " in script:
            state["ran"] = True
            return run_rc, "TABLES DO NOT MATCH\n"
        if "public_n1_diffs-" in script:
            return 0, "".join(n + "\n" for n in reports) if state["ran"] else ""
        return 1, "unexpected %r" % (cmd,)
    return respond


def _diff_outcome(monkeypatch, env, responder):
    m = load(monkeypatch, env)
    c = _Container(responder)
    monkeypatch.setattr(m, "client", _Client(c))
    return outcome(m.test_ace_table_diff, "x", "rhel"), c


@pytest.mark.parametrize("env", [_STANDALONE, _GA_RPM], ids=["standalone", "integration"])
def test_table_diff_reads_the_one_fresh_report_and_its_counts(monkeypatch, env):
    (res, c) = _diff_outcome(monkeypatch, env, _diff_responder(
        {"public_n1_diffs-20261105120000.json": json.dumps(_report())}))
    assert res == ("passed", "")
    scripts = [cmd[-1] for cmd in c.cmds if cmd[0] == "bash"]
    assert "rm -rf -- /tmp/ace-pep/table-diff " in scripts[0] and " table-diff " in scripts[1]
    assert "cd " in scripts[1] and "ACE_CONFIG=" in scripts[1] and "PGSERVICEFILE=" in scripts[1]


@pytest.mark.parametrize("reports,leftover,run_rc,msg", [
    ({"public_n1_diffs-1.json": json.dumps(_report({"n1/n2": 95, "n1/n3": 100, "n2/n3": 5}))}, "", 0,
     "diff_rows_count"),
    ({"public_n1_diffs-1.json": json.dumps(_report({}))}, "", 0, "diff_rows_count"),
    ({"public_n1_diffs-1.json": json.dumps(_report(incomplete_pairs=["n1/n3"]))}, "", 0, "incomplete"),
    ({"public_n1_diffs-1.json": json.dumps(_report(diff_row_limit_reached=True))}, "", 0, "limit"),
    ({"public_n1_diffs-1.json": json.dumps(_report(table="n2"))}, "", 0, "public.n2"),
    ({"public_n1_diffs-1.json": "{not json"}, "", 0, "not valid JSON"),
    ({}, "", 0, "found 0"),
    ({"public_n1_diffs-1.json": json.dumps(_report()), "public_n1_diffs-2.json": json.dumps(_report())}, "", 0,
     "found 2"),
    ({"public_n1_diffs-1.json": json.dumps(_report())}, "public_n1_diffs-0.json\n", 0, "not empty"),
    ({"public_n1_diffs-1.json": json.dumps(_report())}, "", 1, "exit 1"),
], ids=["wrong-count", "no-differences", "incomplete", "row-limit", "wrong-table", "bad-json", "no-report",
        "two-reports", "stale-directory", "command-fails"])
def test_table_diff_fails_a_wrong_missing_or_stale_report(monkeypatch, reports, leftover, run_rc, msg):
    (status, text), _ = _diff_outcome(monkeypatch, _GA_RPM, _diff_responder(reports, run_rc, leftover))
    assert status == "failed" and msg in text, text


def test_table_diff_stops_when_the_reset_refuses_a_symbolic_link(monkeypatch):
    (status, text), c = _diff_outcome(monkeypatch, _GA_RPM, _diff_responder(
        {"public_n1_diffs-1.json": json.dumps(_report())}, reset_rc=3,
        leftover="refusing to empty /tmp/ace-pep/table-diff: /tmp/ace-pep is a symbolic link\n"))
    assert status == "failed" and "symbolic link" in text
    assert not any(" table-diff " in cmd[-1] for cmd in c.cmds if cmd[0] == "bash")


# --------------------------------------------------------------------------- work-directory boundary
# The functional steps empty the work directory with rm -rf, so ACE_WORK_DIR must name a clearly
# test-owned directory: /tmp would also delete the nodes' data directories (/tmp/n1..n3).
@pytest.mark.parametrize("value,expected", [
    (None, "/tmp/ace-pep"), ("", "/tmp/ace-pep"), ("/tmp/ace-pep/", "/tmp/ace-pep"),
    ("/tmp/ace-pep-run1", "/tmp/ace-pep-run1"), ("/var/tmp/ace-pep.2", "/var/tmp/ace-pep.2"),
], ids=["default", "empty", "trailing-slash", "suffix", "other-parent"])
def test_a_dedicated_ace_pep_work_directory_is_accepted(monkeypatch, value, expected):
    m = load(monkeypatch, dict(_STANDALONE, ACE_WORK_DIR=value))
    assert (m.ace_work_dir, m.ACE_WORK_DIR_PROBLEM) == (expected, None)
    assert m.ace_table_diff_dir == expected + "/table-diff"


_UNSAFE_WORK_DIRS = [
    "/tmp", "/", "/var/lib/postgresql", "/home", "tmp/ace-pep", "ace-pep", "/tmp/n1", "/tmp/n1/ace-pep",
    "/tmp/ace-pep/..", "/tmp/../tmp/ace-pep", "/tmp/./ace-pep", "//tmp/ace-pep", "/tmp//ace-pep",
    "/tmp/my-ace-pep", "/tmp/ace-pep x", "/tmp/ace-pep;rm -rf /", "/tmp/$HOME/ace-pep", "/tmp/ace-pep\n",
    "/tmp/*/ace-pep",
]


@pytest.mark.parametrize("value", _UNSAFE_WORK_DIRS, ids=[repr(v) for v in _UNSAFE_WORK_DIRS])
def test_an_unsafe_work_directory_is_refused(monkeypatch, value):
    m = load(monkeypatch, dict(_STANDALONE, ACE_WORK_DIR=value))
    assert m.ACE_WORK_DIR_PROBLEM and "ACE_WORK_DIR" in m.ACE_WORK_DIR_PROBLEM


@pytest.mark.parametrize("value", ["/tmp", "/", "/tmp/n1"], ids=["tmp", "root", "node-data"])
@pytest.mark.parametrize("test", ["test_ace_functional_create_config", "test_ace_table_diff",
                                  "test_ace_mtree_init"], ids=["create-config", "table-diff", "mtree"])
@pytest.mark.parametrize("env", [_STANDALONE, _GA_RPM], ids=["standalone", "integration"])
def test_an_unsafe_work_directory_fails_before_any_container_command(monkeypatch, value, test, env):
    m = load(monkeypatch, dict(env, ACE_WORK_DIR=value))
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(getattr(m, test), "x", "rhel")
    assert status == "failed" and "ACE_WORK_DIR" in msg
    assert c.cmds == []                                   # nothing ran, so nothing was deleted


def test_the_reset_refuses_a_path_outside_the_work_directory(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(_nothing)
    for path in ("/tmp", "/tmp/ace-pep-other", "/tmp/ace-pep/../n1", "/tmp/n1"):
        with pytest.raises(AssertionError, match="outside the ACE work directory"):
            m._reset_test_dir(c, path)
    assert c.cmds == []


class _LocalBash:
    """A real seam: runs the reset's `bash -c` script on this machine (nothing else is allowed)."""
    status = "running"

    def __init__(self):
        self.cmds = []

    def exec_run(self, cmd, user=None, **kwargs):
        self.cmds.append(cmd)
        if cmd[:2] != ["bash", "-c"] or len(cmd) != 3:
            return 1, b"unexpected command"
        p = subprocess.run(cmd, capture_output=True, timeout=30)
        return p.returncode, p.stdout + p.stderr


def test_the_real_reset_empties_only_the_work_directory_and_never_follows_a_link(monkeypatch, tmp_path):
    work, sibling, target = tmp_path / "ace-pep", tmp_path / "n1", tmp_path / "elsewhere"
    for d in (work, sibling, target):
        d.mkdir()
        (d / "keep.txt").write_text("x")
    m = load(monkeypatch, dict(_STANDALONE, ACE_WORK_DIR=str(work)))
    assert m.ACE_WORK_DIR_PROBLEM is None
    sh = _LocalBash()

    ec, out = m._reset_test_dir(sh, m.ace_work_dir)             # emptied and recreated
    assert (ec, out) == (0, b"") and list(work.iterdir()) == []
    (work / "table-diff").mkdir()
    (work / "table-diff" / "public_n1_diffs-old.json").write_text("{}")
    (work / "pg_service.conf").write_text("x")
    ec, out = m._reset_test_dir(sh, m.ace_table_diff_dir)       # only the report directory
    assert (ec, out) == (0, b"") and sorted(p.name for p in work.iterdir()) == ["pg_service.conf", "table-diff"]
    assert list((work / "table-diff").iterdir()) == []
    assert (sibling / "keep.txt").exists() and (target / "keep.txt").exists()

    (work / "table-diff").rmdir()
    (work / "table-diff").symlink_to(target)                    # a link where the report directory goes
    ec, out = m._reset_test_dir(sh, m.ace_table_diff_dir)
    assert ec != 0 and b"symbolic link" in out and (target / "keep.txt").exists()
    shutil.rmtree(work)
    work.symlink_to(target)                                     # the work directory itself is a link
    for path in (m.ace_work_dir, m.ace_table_diff_dir):
        ec, out = m._reset_test_dir(sh, path)
        assert ec != 0 and b"symbolic link" in out and work.is_symlink() and (target / "keep.txt").exists()
