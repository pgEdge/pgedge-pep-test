"""Docker-free tests that exercise the REAL functions in component-test/test_pep_ai_db_workbench.py.

The module is loaded by path with docker.from_env and load_dotenv neutralized and with the environment
of the case under test (the integration request, platform filter and package settings are decided at
import time), then driven with fake containers and spied package helpers -- the approach of
test_pep_radar_wiring.py. Versions are synthetic (9.8.7), never a real ai-dba-workbench release.
Covers:
  * standalone behaviour kept (the four configured packages, PLATFORM_FILTER, unpinned installs, the
    documented standalone skips, the binary banner check);
  * integration mode: the request selects the package and platform, and only the requested server
    package is exercised; a request the test cannot serve is refused before anything is installed;
    the pinned install with checksum evidence; the server's upgrade-check dependency recorded, and no
    other workbench package installed; the shared identity step; the release facts that tie the
    package version to the tag;
  * required checks fail instead of skipping in integration mode (missing container, reference, SBOM
    location); the documented integration skips (package version, upgrade, binary version);
  * the stripped-ELF check and the expected-output references derived from the workbench packaging.
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
_MODULE_PATH = _REPO / "component-test" / "test_pep_ai_db_workbench.py"
_counter = itertools.count()

SERVER = "pgedge-ai-dba-server"
CHECK = "pgedge-ai-dba-upgrade-check"
COLLECTOR, ALERTER, CLIENT = "pgedge-ai-dba-collector", "pgedge-ai-dba-alerter", "pgedge-ai-dba-client"

# The workbench server 9.8.7 (synthetic) as published in the staging channel (tag v9.8.7, build 1).
_GA_RPM = {"PEP_INTEGRATION_MODE": "1", "PEP_COMPONENT": "ai_db_workbench", "PEP_PACKAGE_NAME": SERVER,
           "PEP_CHANNEL": "staging", "PEP_EXPECTED_VERSION": "9.8.7", "PEP_FAMILY": "rpm",
           "PEP_ARCH_FILTER": "amd64", "PEP_CONTAINER_ALIAS": "oel9-amd64", "PG_MAJOR_VERSION": "17",
           "PEP_EXPECTED_BUILDNUM": "1", "PEP_EFFECTIVE_TAG": "v9.8.7", "PEP_EXPECTED_RPM": "9.8.7-1.el9",
           "REPO": "staging", "CONTAINERS": "oel9-amd64", "DEB_CONTAINERS": "debian13-amd64"}
_GA_DEB = dict(_GA_RPM, PEP_FAMILY="deb", PEP_CONTAINER_ALIAS="debian13-amd64", PEP_EXPECTED_RPM=None,
               PEP_EXPECTED_DEB="9.8.7-1.trixie")
# A daily test build (tag v9.8.7-test1 -> build number test1_1; DEB moves the suffix after "~").
_TEST_RPM = dict(_GA_RPM, PEP_CHANNEL="daily", REPO="daily", PEP_EXPECTED_BUILDNUM="test1_1",
                 PEP_EFFECTIVE_TAG="v9.8.7-test1", PEP_EXPECTED_RPM="9.8.7-test1_1.el9")
_TEST_DEB = dict(_GA_DEB, PEP_CHANNEL="daily", REPO="daily", PEP_EXPECTED_BUILDNUM="test1_1",
                 PEP_EFFECTIVE_TAG="v9.8.7-test1", PEP_EXPECTED_DEB="9.8.7~test1-1.trixie")
# A release candidate whose tag has a dotted suffix (v9.8.7-rc.1 -> build number rc1_1).
_RC_RPM = dict(_GA_RPM, PEP_EXPECTED_BUILDNUM="rc1_1", PEP_EFFECTIVE_TAG="v9.8.7-rc.1",
               PEP_EXPECTED_RPM="9.8.7-rc1_1.el9")
_RC_DEB = dict(_GA_DEB, PEP_EXPECTED_BUILDNUM="rc1_1", PEP_EFFECTIVE_TAG="v9.8.7-rc.1",
               PEP_EXPECTED_DEB="9.8.7~rc1-1.trixie")
_STANDALONE = {"PGEDGE_AI_DBA_VERSION": "9.8.7", "CONTAINERS": "oel9-amd64", "DEB_CONTAINERS": "debian13-amd64"}
_KEYS = sorted(set(_GA_RPM) | set(_GA_DEB) | {
    "PEP_EXPECTED_DEB", "PEP_EXPECTED_BINARY", "PEP_MODE", "PEP_SCENARIO", "PLATFORM_FILTER",
    "PGEDGE_AI_DBA_VERSION", "AI_DBA_SERVER_PACKAGE", "AI_DBA_ALERTER_PACKAGE", "AI_DBA_COLLECTOR_PACKAGE",
    "AI_DBA_CLIENT_PACKAGE", "AI_DBA_SERVER_BINARY", "AI_DBA_ALERTER_BINARY", "AI_DBA_COLLECTOR_BINARY",
    "AI_DBA_COMPONENTS", "UPGRADE", "UPGRADE_REPO", "SKIP_CLEANUP", "COMPONENTS"})


def load(monkeypatch, env):
    """A fresh copy of the workbench component test under exactly ``env`` (None values unset)."""
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(docker, "from_env", lambda *a, **k: None)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)   # only ``env`` decides
    name = "ai_db_workbench_wiring_under_test_%d" % next(_counter)
    spec = importlib.util.spec_from_file_location(name, str(_MODULE_PATH))
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
    """The package-manager listing the install step reads (rpm -qa / dpkg-query -W)."""
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qa"]:
            return 0, "".join(n + "\n" for n in names)
        if cmd[:2] == ["dpkg-query", "-W"]:
            return 0, "".join(n + " install ok installed\n" for n in names)
        return 0, ""
    return respond


def _call(m, test, container_name="oel9-amd64", container_type="rhel"):
    """Call a real test with the arguments its parametrization gives it."""
    fn = getattr(m, test)
    names = fn.__code__.co_varnames[:fn.__code__.co_argcount]
    args = {"container_name": container_name, "container_type": container_type, "package": SERVER,
            "binary_path": "/usr/bin/ai-dba-server"}
    return outcome(fn, *[args[n] for n in names])


# --------------------------------------------------------------------------- standalone kept
def test_standalone_keeps_all_four_configured_packages_and_the_platform_filter(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, PLATFORM_FILTER="deb", AI_DBA_SERVER_PACKAGE="my-server",
                               AI_DBA_COLLECTOR_BINARY="/opt/bin/collector"))
    assert m.INTEGRATION_REQUEST is None and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == [("debian13-amd64", "deb")]
    assert m.ai_db_workbench_packages == ["my-server", ALERTER, COLLECTOR, CLIENT]
    assert [c[2:] for c in m.all_container_binary_combinations] == [
        (ALERTER, "/usr/bin/ai-dba-alerter"), (COLLECTOR, "/opt/bin/collector"), ("my-server", "/usr/bin/ai-dba-server")]
    m = load(monkeypatch, _STANDALONE)
    assert m.all_containers == [("oel9-amd64", "rhel"), ("debian13-amd64", "deb")]
    assert m.ai_db_workbench_packages == [SERVER, ALERTER, COLLECTOR, CLIENT]
    assert len(m.all_container_package_combinations) == 8 and len(m.all_container_binary_combinations) == 6
    # The standalone dependency expectations are unchanged: the served packages predate upgrade-check.
    assert m.ai_db_workbench_dependency_map == {ALERTER: [SERVER], CLIENT: [SERVER], COLLECTOR: [SERVER], SERVER: []}


def test_standalone_install_is_unpinned_and_unchanged(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    for pkg in (SERVER, ALERTER, COLLECTOR, CLIENT):
        m.test_ai_db_workbench_install("oel9-amd64", "rhel", pkg)
    assert pinned.calls == [] and [a for a, k in ip.calls] == [(c, p) for p in (SERVER, ALERTER, COLLECTOR, CLIENT)]


def test_standalone_package_version_check_uses_pgedge_ai_dba_version(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    verify = _Spy((True, "rhel", "9.8.7-1.el9", "ok"))
    monkeypatch.setattr(m.package_management, "verify_package_version", verify)
    assert outcome(m.test_ai_db_workbench_package_version, "oel9-amd64", "rhel", COLLECTOR) == ("passed", "")
    assert verify.calls[0][0][1:] == (COLLECTOR, "9.8.7")


@pytest.mark.parametrize("test,args", [
    ("test_ai_db_workbench_upgrade", ("oel9-amd64", "rhel", SERVER)),
    ("test_ai_db_workbench_identity", ("oel9-amd64", "rhel")),
    ("test_binary_version", ("oel9-amd64", "rhel", SERVER, "/usr/bin/ai-dba-server")),
    ("test_verify_package_dependencies", ("oel9-amd64", "rhel", SERVER)),
    ("test_verify_systemd_service", ("oel9-amd64", "rhel", CLIENT)),
], ids=["upgrade-off", "identity-integration-only", "server-binary-version", "server-dependencies",
        "client-service"])
def test_standalone_keeps_its_documented_skips(monkeypatch, test, args):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(getattr(m, test), *args)[0] == "skipped"
    assert c.cmds == []


def test_standalone_still_skips_a_missing_container_or_reference(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(missing=True))
    for test in ("test_configure_repository", "test_ai_db_workbench_install", "test_ai_db_workbench_package_version",
                 "test_verify_bundled_files", "test_verify_sbom", "test_verify_license_file", "test_verify_readme_file",
                 "test_verify_systemd_service", "test_binary_stripped", "test_ai_db_workbench_uninstall",
                 "test_pgedge_cleanup"):
        assert _call(m, test)[0] == "skipped", test
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for ai-dba-collector")))
    assert outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel", COLLECTOR)[0] == "skipped"
    monkeypatch.setitem(m.ai_db_workbench_sbom_info, CLIENT, None)
    assert outcome(m.test_verify_sbom, "oel9-amd64", "rhel", CLIENT)[0] == "skipped"


@pytest.mark.parametrize("banner,status", [
    ("pgEdge AI DBA Workbench Alerter v9.8.7 starting...\nfailed to connect\n", "passed"),
    ("pgEdge AI DBA Workbench Alerter v9.8.6 starting...\n", "failed"),
], ids=["banner", "other-version"])
def test_standalone_binary_version_keeps_the_banner_check(monkeypatch, banner, status):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(lambda cmd: (0, banner))
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_binary_version, "x", "rhel", ALERTER, "/usr/bin/ai-dba-alerter")[0] == status
    assert c.cmds == [["bash", "-c", "/usr/bin/ai-dba-alerter 2>&1; true"]]


# --------------------------------------------------------------------------- integration selection
@pytest.mark.parametrize("env,containers", [
    (dict(_GA_RPM, PLATFORM_FILTER="deb"), [("oel9-amd64", "rhel")]),
    (_GA_DEB, [("debian13-amd64", "deb")]),
], ids=["rpm-overrides-filter", "deb"])
def test_integration_request_selects_only_the_requested_server_package(monkeypatch, env, containers):
    m = load(monkeypatch, dict(env, AI_DBA_SERVER_PACKAGE="my-server", AI_DBA_SERVER_BINARY="/opt/bin/server",
                               PGEDGE_AI_DBA_VERSION="1.2.3"))
    assert m.INTEGRATION_REQUEST["component"] == "ai_db_workbench" and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == containers
    assert m.ai_db_workbench_packages == [SERVER]            # never the collector, alerter or client
    assert m.all_container_package_combinations == [c + (SERVER,) for c in containers]
    assert m.all_container_binary_combinations == [c + (SERVER, "/usr/bin/ai-dba-server") for c in containers]
    assert m.ai_db_workbench_sbom_info[SERVER]["dir"] == "/usr/share/pgedge-ai-dba-server"
    assert m.ai_db_workbench_service_map[SERVER] == "/lib/systemd/system/pgedge-ai-dba-server.service"
    assert m.ai_db_workbench_dependency_map[SERVER] == [CHECK]


@pytest.mark.parametrize("package,why", [
    (COLLECTOR, "runtime_not_certified"), (CLIENT, "runtime_not_certified"), (CHECK, "runtime_not_certified"),
    # the workbench entry names its companions, so any other name is unclassified (still refused)
    ("pgedge-radar", "package_unclassified"),
], ids=["collector", "client", "upgrade-check", "foreign"])
def test_a_request_for_any_other_package_is_rejected_at_load(monkeypatch, package, why):
    with pytest.raises(Exception, match="does not match component 'ai_db_workbench'.*%s" % why):
        load(monkeypatch, dict(_GA_RPM, PEP_PACKAGE_NAME=package))


def test_the_backstop_names_each_request_it_cannot_serve(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    req = dict(m.INTEGRATION_REQUEST)
    assert m.integration_problem(req) is None
    assert "component 'radar' is not ai_db_workbench" in m.integration_problem(dict(req, component="radar"))
    assert "is not an ai_db_workbench package" in m.integration_problem(dict(req, package_name=COLLECTOR))
    assert m.integration_problem(None) is None
    # A valid request routed to the wrong test (here a radar request) is refused, not installed.
    m = load(monkeypatch, dict(_GA_RPM, PEP_COMPONENT="radar", PEP_PACKAGE_NAME="pgedge-radar"))
    assert "component 'radar' is not ai_db_workbench" in m.INTEGRATION_PROBLEM


# --------------------------------------------------------------------------- install
@pytest.mark.parametrize("env,ctype,exact", [
    (_GA_RPM, "rhel", "9.8.7-1.el9"), (_TEST_DEB, "deb", "9.8.7~test1-1.trixie"),
], ids=["rpm", "deb"])
def test_only_the_requested_server_is_installed_pinned_and_its_dependency_recorded(monkeypatch, tmp_path, env,
                                                                                  ctype, exact):
    m = load(monkeypatch, env)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(SERVER, CHECK, "pgedge-postgresql-17")))
    ip, pinned = _Spy((True, "x", "installed")), _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    versions = {SERVER: exact, CHECK: exact}
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: versions.get(n))

    assert outcome(m.test_ai_db_workbench_install, "x", ctype, SERVER) == ("passed", "")

    assert ip.calls == []                                   # no prerequisite, no unpinned install
    assert [a[1:] for a, k in pinned.calls] == [(SERVER, exact)]
    marker = json.loads((tmp_path / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "a" * 64
    assert json.loads((tmp_path / "prerequisite-versions.json").read_text()) == {
        "channel": env["REPO"], "requested_package": SERVER, "installed_as_dependency": [CHECK],
        "installed_versions": {CHECK: exact, SERVER: exact}}


def test_a_newer_upgrade_check_is_recorded_not_rejected(monkeypatch, tmp_path):
    # DEB declares Pre-Depends: pgedge-ai-dba-upgrade-check (>= the server's version), so the channel's
    # newest one may come in; it is recorded, never certified or compared.
    m = load(monkeypatch, _GA_DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(SERVER, CHECK)))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((True, "ok", "b" * 64)))
    versions = {SERVER: "9.8.7-1.trixie", CHECK: "9.8.8-1.trixie"}
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: versions.get(n))
    assert outcome(m.test_ai_db_workbench_install, "x", "deb", SERVER) == ("passed", "")
    recorded = json.loads((tmp_path / "prerequisite-versions.json").read_text())["installed_versions"]
    assert recorded == {SERVER: "9.8.7-1.trixie", CHECK: "9.8.8-1.trixie"}


def test_a_request_the_backstop_rejects_is_refused_before_any_install(monkeypatch, tmp_path):
    m = load(monkeypatch, dict(_GA_RPM, PEP_COMPONENT="radar", PEP_PACKAGE_NAME="pgedge-radar"))
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    status, msg = outcome(m.test_ai_db_workbench_install, "oel9-amd64", "rhel", "pgedge-radar")
    assert status == "failed" and "cannot be served" in msg and "is not ai_db_workbench" in msg
    assert ip.calls == [] and pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_a_failed_pinned_install_fails_without_install_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((False, "checksum mismatch", None)))
    status, msg = outcome(m.test_ai_db_workbench_install, "oel9-amd64", "rhel", SERVER)
    assert status == "failed" and "pinned install of pgedge-ai-dba-server=9.8.7-1.el9 failed: checksum mismatch" in msg
    assert not (tmp_path / "install-evidence.json").exists()


@pytest.mark.parametrize("ctype,present", [
    ("rhel", (SERVER, CHECK, COLLECTOR)),                  # a sibling package beside the request
    ("deb", (SERVER, CHECK, CLIENT)),
    ("rhel", (SERVER, CHECK, ALERTER)),
    ("rhel", (SERVER, CHECK, "pgedge-ai-dba-server-debuginfo")),
    ("rhel", (SERVER,)),                                    # the declared dependency is missing
    ("deb", ()),                                            # nothing of the component is installed
], ids=["collector", "client", "alerter", "debuginfo", "no-upgrade-check", "absent"])
def test_any_other_workbench_package_fails_the_install_step(monkeypatch, tmp_path, ctype, present):
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(*present, "pgedge-ai-kb-x")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((True, "ok", "a" * 64)))
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    status, msg = outcome(m.test_ai_db_workbench_install, "x", ctype, SERVER)
    assert status == "failed" and "expected exactly the requested pgedge-ai-dba-server and its dependency" in msg


@pytest.mark.parametrize("ctype", ["rhel", "deb"])
def test_a_certified_name_outside_the_prefix_is_still_counted(monkeypatch, ctype):
    # Every workbench name starts with pgedge-ai-dba- today, so the prefix alone finds them. The policy
    # lookup is what still counts the certified package if the policy ever names it otherwise.
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    monkeypatch.setattr(m._pr, "COMPONENT_PACKAGES",
                        dict(m._pr.COMPONENT_PACKAGES, ai_db_workbench=("pgedge-workbench-server",)))
    listing = _pkg_list("pgedge-workbench-server", CHECK, "pgedge-postgresql-17")
    assert m.installed_workbench_packages(_Container(listing), ctype) == [CHECK, "pgedge-workbench-server"]


def test_component_tests_share_the_integration_helper_instances(monkeypatch):
    # Patching package_management / pep_evidence must reach the shared install + identity steps.
    m = load(monkeypatch, _GA_RPM)
    assert m.pep_request_env is m.pep_integration_mode.pep_request_env
    assert m.package_management is m.pep_integration_mode.package_management


# --------------------------------------------------------------------------- identity
def _installed(m, tmp_path, exact):
    m.pep_integration_mode.pep_evidence.write_install_evidence(
        m.INTEGRATION_REQUEST, "run-A", "pinned", exact, str(tmp_path / "install-evidence.json"),
        installed_sha256="a" * 64)


@pytest.mark.parametrize("observed,ok", [("9.8.7-1.el9", True), ("9.8.6-1.el9", False),
                                         ("9.8.7-rc1_1.el9", False)], ids=["exact", "older", "rc"])
def test_identity_proves_the_exact_requested_package_version(monkeypatch, tmp_path, observed, ok):
    m = load(monkeypatch, _GA_RPM)
    c = _Container(_nothing)
    _wire(monkeypatch, tmp_path, m, c)
    _installed(m, tmp_path, "9.8.7-1.el9")
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: observed)
    status, msg = outcome(m.test_ai_db_workbench_identity, "oel9-amd64", "rhel")
    assert status == ("passed" if ok else "failed")
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")
    assert c.cmds == []                                     # no binary is probed for identity


def test_identity_without_install_evidence_fails_with_persisted_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    status, msg = outcome(m.test_ai_db_workbench_identity, "oel9-amd64", "rhel")
    assert status == "failed" and "install-before-identity precondition failed" in msg
    assert (tmp_path / "identity-evidence.json").exists()


# --------------------------------------------------------------------------- release facts
@pytest.mark.parametrize("env", [_GA_RPM, _GA_DEB, _RC_RPM, _RC_DEB, _TEST_RPM, _TEST_DEB,
                                 dict(_GA_RPM, PEP_EXPECTED_BUILDNUM=None, PEP_EXPECTED_RPM=None)],
                         ids=["ga-rpm", "ga-deb", "rc-rpm", "rc-deb", "test-rpm", "test-deb", "tag-only"])
def test_release_facts_that_follow_the_package_version_rule_agree(monkeypatch, env):
    m = load(monkeypatch, env)
    assert m.release_version_problems(m.INTEGRATION_REQUEST) == []


@pytest.mark.parametrize("override,problem", [
    ({"effective_tag": None}, "effective_tag is required"),
    ({"effective_tag": "v9.8"}, "is not v<major>.<minor>.<patch>"),
    ({"effective_tag": "v9.8.7-rc-1"}, "is not v<major>.<minor>.<patch>"),
    ({"effective_tag": "v9.8.8"}, "does not match expected_version '9.8.7'"),
    ({"effective_tag": "v9.8.7-rc.1"}, "expected_buildnum '1' does not match"),
    ({"expected_buildnum": "2"}, "expected_buildnum '2' does not match"),
    ({"expected_rpm": "9.8.7-rc1_1.el9"}, "expected_rpm '9.8.7-rc1_1.el9' does not match"),
    ({"expected_rpm": "9.8.7-1"}, "expected_rpm '9.8.7-1' does not match"),
    ({"expected_rpm": "9.8.7-10.el9"}, "expected_rpm '9.8.7-10.el9' does not match"),
], ids=["no-tag", "malformed-tag", "dashed-suffix", "tag-vs-version", "tag-vs-buildnum", "buildnum",
        "rpm-string", "rpm-no-dist", "rpm-prefix-only"])
def test_inconsistent_release_facts_are_reported(monkeypatch, override, problem):
    m = load(monkeypatch, _GA_RPM)
    problems = m.release_version_problems(dict(m.INTEGRATION_REQUEST, **override))
    assert any(problem in p for p in problems), problems


def test_an_inconsistent_deb_string_is_reported(monkeypatch):
    m = load(monkeypatch, _GA_DEB)
    for wrong in ("9.8.7~rc1-1.trixie", "9.8.7-2.trixie", "9.8.7-test1-1.trixie", "9.8.7-1"):
        problems = m.release_version_problems(dict(m.INTEGRATION_REQUEST, expected_deb=wrong))
        assert any("expected_deb %r does not match" % wrong in p for p in problems), wrong


@pytest.mark.parametrize("env,exact", [
    (dict(_GA_RPM, PEP_EXPECTED_BUILDNUM="test1_1"), "9.8.7-1.el9"),
    (dict(_GA_RPM, PEP_EFFECTIVE_TAG=None), "9.8.7-1.el9"),
], ids=["buildnum-disagrees", "no-tag"])
def test_identity_fails_when_the_release_facts_disagree_even_if_the_version_matches(monkeypatch, tmp_path, env,
                                                                                    exact):
    m = load(monkeypatch, env)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    _installed(m, tmp_path, exact)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: exact)
    status, msg = outcome(m.test_ai_db_workbench_identity, "oel9-amd64", "rhel")
    assert status == "failed" and "release facts disagree" in msg
    assert (tmp_path / "identity-evidence.json").exists()  # persisted before the failure


# --------------------------------------------------------------------------- integration skips
@pytest.mark.parametrize("test,args,why", [
    ("test_ai_db_workbench_package_version", ("oel9-amd64", "rhel", SERVER),
     "superseded by test_ai_db_workbench_identity"),
    ("test_ai_db_workbench_upgrade", ("oel9-amd64", "rhel", SERVER), "certification never upgrades"),
    ("test_binary_version", ("oel9-amd64", "rhel", SERVER, "/usr/bin/ai-dba-server"),
     "not tied to the release tag"),
], ids=["package-version", "upgrade", "binary-version"])
def test_integration_mode_only_skips_documented_superseded_or_impossible_steps(monkeypatch, test, args, why):
    m = load(monkeypatch, dict(_GA_RPM, UPGRADE="true", PGEDGE_AI_DBA_VERSION="9.8.7"))
    ip = _Spy((True, "rhel", "installed"))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(getattr(m, test), *args)
    assert status == "skipped" and why in msg and ip.calls == [] and c.cmds == []


# --------------------------------------------------------------------------- stripped ELF
_FILE = "ELF 64-bit LSB executable, x86-64, version 1 (SYSV), statically linked, Go BuildID=x, BuildID[sha1]=y, "


@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
@pytest.mark.parametrize("out,problem", [
    (_FILE + "stripped", None),
    (_FILE + "not stripped", "it is not stripped"),
    (_FILE + "with debug_info, not stripped", "it is not stripped"),
    (_FILE.rstrip(", "), "does not report it as stripped"),
    ("POSIX shell script, ASCII text executable", "not an ELF binary"),
    ("cannot open `/usr/bin/ai-dba-server' (No such file or directory)", "not an ELF binary"),
], ids=["stripped", "not-stripped", "debug-info", "unreported", "script", "missing"])
def test_the_strip_check_requires_a_stripped_elf_in_both_modes(monkeypatch, env, out, problem):
    m = load(monkeypatch, env)
    found = m.elf_strip_problem(out)
    assert (found is None) if problem is None else (problem in found)
    c = _Container(lambda cmd: (0, out + "\n") if cmd == ["file", "-b", "/usr/bin/ai-dba-server"] else (1, "unexpected"))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_binary_stripped, "x", "rhel", SERVER, "/usr/bin/ai-dba-server")
    assert status == ("passed" if problem is None else "failed"), msg


# --------------------------------------------------------------------------- required checks
_INTEGRATION_TESTS = ("test_configure_repository", "test_ai_db_workbench_install", "test_ai_db_workbench_identity",
                      "test_verify_bundled_files", "test_verify_sbom", "test_verify_license_file",
                      "test_verify_readme_file", "test_verify_systemd_service", "test_verify_package_dependencies",
                      "test_binary_stripped", "test_ai_db_workbench_uninstall", "test_pgedge_cleanup")


def test_integration_mode_fails_a_missing_container(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(missing=True))
    for test in _INTEGRATION_TESTS:
        status, msg = _call(m, test)
        assert status == "failed" and "not found" in msg, test


def test_integration_mode_fails_a_missing_bundled_file_reference(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for ai-dba-server")))
    status, msg = outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel", SERVER)
    assert status == "failed" and "No expected file found" in msg


def test_a_missing_reference_still_fails_if_skip_or_fail_returns(monkeypatch):
    # _skip_or_fail always raises today; if it ever returned, the check must still fail, not pass.
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for ai-dba-server")))
    returning = _Spy(None)
    monkeypatch.setattr(m, "_skip_or_fail", returning)
    status, msg = outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel", SERVER)
    assert returning.calls == [(("No expected file found for ai-dba-server",), {})]
    assert status == "failed" and "No expected file found for ai-dba-server" in msg


def test_integration_mode_fails_an_unset_sbom_location(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setitem(m.ai_db_workbench_sbom_info, SERVER, None)
    status, msg = outcome(m.test_verify_sbom, "oel9-amd64", "rhel", SERVER)
    assert status == "failed" and "No SBOM info configured" in msg


def _sbom(fail_on):
    def respond(cmd):
        text = cmd if isinstance(cmd, str) else " ".join(cmd)
        if fail_on in text:
            return 1, "error: %s" % fail_on
        if "sq verify" in text and "--help" not in text:
            return 0, "1 authenticated signature.\n"
        return 0, ""
    return respond


@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_a_missing_or_unverified_sbom_fails_in_both_modes(monkeypatch, env):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m.machine_prereq_setup, "ensure_sq_installed", lambda c: None)
    monkeypatch.setattr(m, "client", _Client(_Container(_sbom("no-such-marker"))))
    assert outcome(m.test_verify_sbom, "x", "rhel", SERVER) == ("passed", "")
    assert outcome(m.test_verify_sbom, "x", "deb", SERVER) == ("passed", "")
    for fail_on, ctype in (("wget", "rhel"), ("sq verify --signature-file", "rhel"), ("cd /usr/share/pgedge-ai-dba-server", "deb")):
        monkeypatch.setattr(m, "client", _Client(_Container(_sbom(fail_on))))
        assert outcome(m.test_verify_sbom, "x", ctype, SERVER)[0] == "failed", fail_on


@pytest.mark.parametrize("ctype,query", [("rhel", "rpm -qR pgedge-ai-dba-server | grep -q 'pgedge-ai-dba-upgrade-check'"),
                                         ("deb", "dpkg -s pgedge-ai-dba-server 2>/dev/null | grep -i Depends | "
                                                 "grep -q 'pgedge-ai-dba-upgrade-check'")], ids=["rpm", "deb"])
def test_integration_mode_checks_the_servers_declared_upgrade_check_dependency(monkeypatch, ctype, query):
    # packaging/rpm/ai-dba-workbench.spec: Requires(pre): pgedge-ai-dba-upgrade-check = %{version}-%{release};
    # packaging/deb/debian/control: Pre-Depends: pgedge-ai-dba-upgrade-check (>= ${binary:Version}).
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    for rc, status in ((0, "passed"), (1, "failed")):
        c = _Container(lambda cmd: (rc, "") if cmd == ["bash", "-c", query] else (2, "unexpected"))
        monkeypatch.setattr(m, "client", _Client(c))
        assert outcome(m.test_verify_package_dependencies, "x", ctype, SERVER)[0] == status


def test_integration_mode_uninstalls_only_the_requested_package(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    un = _Spy((True, "rhel", "removed"))
    monkeypatch.setattr(m.package_management, "uninstall_package", un)
    for (name, ctype, pkg) in m.all_container_package_combinations:
        assert outcome(m.test_ai_db_workbench_uninstall, name, ctype, pkg) == ("passed", "")
    assert [a[1] for a, k in un.calls] == [SERVER]


# --------------------------------------------------------------------------- expected-output references
# The server payload the workbench packaging defines at the base SHA:
#   RPM packaging/rpm/ai-dba-workbench.spec %files -n pgedge-ai-dba-server (lines 236-248), with %{_unitdir}
#       = /usr/lib/systemd/system on EL; %license/%doc take absolute paths, so no doc directory is owned;
#   DEB packaging/deb/debian/rules override_dh_auto_install (lines 20, 30-34) and dh_installsystemd (line 12,
#       unit copied in by packaging/build-deb.sh line 67); dh_compress gzips README.md (over 4 KiB) but not
#       LICENSE.md (965 bytes); dh_installchangelogs adds changelog.Debian.gz. The state directories (rules
#       lines 35-36) are left out: see test_the_deb_reference_leaves_out_the_state_directories_....
_RPM_PAYLOAD = ["/etc/pgedge", "/etc/pgedge/ai-dba-server.yaml", "/usr/bin/ai-dba-server",
                "/usr/lib/systemd/system/pgedge-ai-dba-server.service",
                "/usr/share/doc/pgedge-ai-dba-server/LICENSE.md", "/usr/share/doc/pgedge-ai-dba-server/README.md",
                "/usr/share/pgedge-ai-dba-server/pgedge-ai-dba-server-sbom.json",
                "/usr/share/pgedge-ai-dba-server/pgedge-ai-dba-server-sbom.json.asc",
                "/var/lib/pgedge", "/var/lib/pgedge/ai-dba-server", "/var/log/pgedge", "/var/log/pgedge/ai-dba-server"]
_DEB_PAYLOAD = ["/etc/pgedge/ai-dba-server.yaml", "/lib/systemd/system/pgedge-ai-dba-server.service",
                "/usr/bin/ai-dba-server", "/usr/share/doc/pgedge-ai-dba-server/LICENSE.md",
                "/usr/share/doc/pgedge-ai-dba-server/README.md.gz",
                "/usr/share/doc/pgedge-ai-dba-server/changelog.Debian.gz",
                "/usr/share/pgedge-ai-dba-server/pgedge-ai-dba-server-sbom.json",
                "/usr/share/pgedge-ai-dba-server/pgedge-ai-dba-server-sbom.json.asc"]


def test_the_expected_output_references_list_the_packaged_server_payload():
    assert (_REPO / "expected-output" / "rpm" / "ai-dba-server").read_text().splitlines() == _RPM_PAYLOAD
    assert (_REPO / "expected-output" / "deb" / "ai-dba-server").read_text().splitlines() == _DEB_PAYLOAD
    # Only the certified server gets a reference; the uncertified packages keep none.
    for fam in ("rpm", "deb"):
        for name in ("ai-dba-collector", "ai-dba-alerter", "ai-dba-client", "ai-dba-upgrade-check"):
            assert not (_REPO / "expected-output" / fam / name).exists(), (fam, name)


def _listing(cmd_wanted, files):
    return lambda cmd: (0, "".join(f + "\n" for f in files)) if cmd == cmd_wanted else (1, "unexpected")


@pytest.mark.parametrize("ctype,listing_cmd,payload,extras", [
    ("rhel", "rpm -ql pgedge-ai-dba-server", _RPM_PAYLOAD,
     ["/usr/lib/.build-id", "/usr/lib/.build-id/0a", "/usr/lib/.build-id/0a/0123456789abcdef0123456789abcdef012345"]),
    ("deb", "dpkg -L pgedge-ai-dba-server", _DEB_PAYLOAD,
     ["/.", "/etc", "/etc/pgedge", "/usr", "/usr/bin", "/usr/share", "/usr/share/doc",
      "/usr/share/doc/pgedge-ai-dba-server", "/usr/share/pgedge-ai-dba-server", "/var", "/var/lib",
      "/var/lib/pgedge", "/var/lib/pgedge/ai-dba-server", "/var/log", "/var/log/pgedge",
      "/var/log/pgedge/ai-dba-server"]),
], ids=["rpm", "deb"])
@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_the_real_bundled_file_check_finds_the_server_reference(monkeypatch, env, ctype, listing_cmd, payload,
                                                                extras):
    # The real verify_bundled_files derives "ai-dba-server" from pgedge-ai-dba-server and compares the
    # package's file list against expected-output/<family>/ai-dba-server: every listed entry must be
    # installed. Entries the package manager adds around the payload are reported as extra.
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_listing(listing_cmd, payload + extras))))
    assert outcome(m.test_verify_bundled_files, "x", ctype, SERVER) == ("passed", "")
    for missing in (payload[0], "/usr/bin/ai-dba-server",
                    "/usr/share/pgedge-ai-dba-server/pgedge-ai-dba-server-sbom.json.asc"):
        monkeypatch.setattr(m, "client", _Client(_Container(_listing(listing_cmd, [f for f in payload if f != missing]))))
        status, msg = outcome(m.test_verify_bundled_files, "x", ctype, SERVER)
        assert status == "failed" and "Missing files" in msg, missing


_STATE_DIRS = ["/var/lib/pgedge/ai-dba-server", "/var/log/pgedge/ai-dba-server"]


def test_the_deb_reference_leaves_out_the_state_directories_its_comparison_cannot_check(monkeypatch, tmp_path):
    # The shared DEB comparison (aspects/file_management.py) accepts a missing entry when ONE installed
    # entry has the same basename, so /usr/bin/ai-dba-server would stand in for either state directory
    # (debian/rules lines 35-36) and listing them would claim a check that cannot fail. The RPM
    # comparison is exact, so the RPM reference keeps them (spec %files lines 245-248).
    assert not set(_STATE_DIRS) & set(_DEB_PAYLOAD) and set(_STATE_DIRS) <= set(_RPM_PAYLOAD)
    m = load(monkeypatch, _GA_DEB)
    (tmp_path / "expected-output" / "deb").mkdir(parents=True)
    (tmp_path / "expected-output" / "deb" / "ai-dba-server").write_text("\n".join(_DEB_PAYLOAD + _STATE_DIRS) + "\n")
    ok, details, _ = m.file_management.verify_bundled_files(
        _Container(_listing("dpkg -L pgedge-ai-dba-server", _DEB_PAYLOAD)), "x", "deb", SERVER, SERVER, tmp_path)
    assert ok and details["missing_files"] == []           # both directories "found" via /usr/bin/ai-dba-server
    m = load(monkeypatch, _GA_RPM)
    for gone in _STATE_DIRS:
        monkeypatch.setattr(m, "client", _Client(_Container(_listing(
            "rpm -ql pgedge-ai-dba-server", [f for f in _RPM_PAYLOAD if f != gone]))))
        assert outcome(m.test_verify_bundled_files, "x", "rhel", SERVER)[0] == "failed", gone


def test_a_deb_unit_under_usr_lib_still_matches(monkeypatch):
    # Newer debhelper installs units under /usr/lib/systemd/system; the DEB comparison resolves a
    # missing entry by its unique basename, so either layout matches the one reference.
    m = load(monkeypatch, _GA_DEB)
    moved = [f.replace("/lib/systemd/", "/usr/lib/systemd/") if f.startswith("/lib/") else f for f in _DEB_PAYLOAD]
    monkeypatch.setattr(m, "client", _Client(_Container(_listing("dpkg -L pgedge-ai-dba-server", moved))))
    assert outcome(m.test_verify_bundled_files, "x", "deb", SERVER) == ("passed", "")


# The RPM layout the channels serve today for the server (release and staging, el9 x86_64), from the
# public dnf filelists metadata; the build-id entry is replaced by a synthetic one. Early alphas put the
# SBOM directly under /usr/share.
_SERVED_RPM = ["/etc/pgedge", "/etc/pgedge/ai-dba-server.yaml", "/usr/bin/ai-dba-server", "/usr/lib/.build-id",
               "/usr/lib/.build-id/0a", "/usr/lib/.build-id/0a/0123456789abcdef0123456789abcdef012345",
               "/usr/lib/systemd/system/pgedge-ai-dba-server.service",
               "/usr/share/doc/pgedge-ai-dba-server/LICENSE.md", "/usr/share/doc/pgedge-ai-dba-server/README.md",
               "/usr/share/pgedge-ai-dba-server/pgedge-ai-dba-server-sbom.json",
               "/usr/share/pgedge-ai-dba-server/pgedge-ai-dba-server-sbom.json.asc",
               "/var/lib/pgedge", "/var/lib/pgedge/ai-dba-server", "/var/log/pgedge", "/var/log/pgedge/ai-dba-server"]
_EARLY_ALPHA_RPM = [f for f in _SERVED_RPM if "/doc/" not in f and "/pgedge-ai-dba-server/pgedge-ai-dba" not in f] + [
    "/usr/share/pgedge-ai-dba-server-sbom.json", "/usr/share/pgedge-ai-dba-server-sbom.json.asc"]


def test_standalone_runs_against_the_served_rpm_layout(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_listing("rpm -ql pgedge-ai-dba-server", _SERVED_RPM))))
    assert outcome(m.test_verify_bundled_files, "x", "rhel", SERVER) == ("passed", "")
    monkeypatch.setattr(m, "client", _Client(_Container(_listing("rpm -ql pgedge-ai-dba-server", _EARLY_ALPHA_RPM))))
    status, msg = outcome(m.test_verify_bundled_files, "x", "rhel", SERVER)
    assert status == "failed" and "Missing files (4)" in msg
