"""Docker-free tests that exercise the REAL functions in component-test/test_pep_mcp.py.

The module is loaded by path with docker.from_env and load_dotenv neutralized and with the environment
of the case under test (the integration request, platform filter and component list are decided at
import time), then driven with fake containers and spied package helpers -- the approach of
test_pep_radar_wiring.py. Versions are synthetic (9.8.7), never a real MCP release. Covers:
  * standalone behaviour kept (all four configured components including the legacy
    pgedge-postgres-mcp-kb, PLATFORM_FILTER, unpinned installs, the config version map, the
    documented standalone skips);
  * integration mode: the request selects the package and platform, and the parametrization holds
    only the requested package; requests for the built-but-uncertified CLI or web UI are rejected
    at load; a request the test cannot serve is refused before anything is installed; the requested
    package is installed pinned with its checksum, with no prerequisite and nothing else; no other
    MCP package (named by the policy or by the MCP name prefixes) may be installed; the shared
    identity step, with no binary probe;
  * required checks fail instead of skipping in integration mode (missing container, missing
    expected-output reference, no SBOM directory), and the located SBOM must belong to the requested
    package alone, by the package manager's answer for exactly that path;
  * the expected-output references derived from the MCP packaging (pkg/rpm/pgedge-nla.spec and
    pkg/deb/debian/rules at pgEdge/pgedge-postgres-mcp 6448336, unchanged through v1.2.0-beta1 and
    main a08aa30), checked through the real
    file_management.verify_bundled_files, and in integration mode the DEB server binary and two state
    directories, which that comparison matches only by name, at their exact paths.
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
_MCP_PATH = _REPO / "component-test" / "test_pep_mcp.py"
_counter = itertools.count()

_FOUR = "pgedge-postgres-mcp,pgedge-postgres-mcp-kb,pgedge-nla-cli,pgedge-nla-web"
# The MCP server 9.8.7 (synthetic) as published in the staging channel (tag v9.8.7, build number 1).
_GA_RPM = {"PEP_INTEGRATION_MODE": "1", "PEP_COMPONENT": "mcp", "PEP_PACKAGE_NAME": "pgedge-postgres-mcp",
           "PEP_CHANNEL": "staging", "PEP_EXPECTED_VERSION": "9.8.7", "PEP_FAMILY": "rpm",
           "PEP_ARCH_FILTER": "amd64", "PEP_CONTAINER_ALIAS": "oel9-amd64", "PG_MAJOR_VERSION": "17",
           "PEP_EXPECTED_BUILDNUM": "1", "PEP_EFFECTIVE_TAG": "v9.8.7", "PEP_EXPECTED_RPM": "9.8.7-1.el9",
           "REPO": "staging", "CONTAINERS": "oel9-amd64", "DEB_CONTAINERS": "debian13-amd64",
           "MCP_COMPONENTS": _FOUR, "DEB_MCP_COMPONENTS": _FOUR, "DECOUPLED_COMPONENTS_SBOM": "/usr/share"}
_GA_DEB = dict(_GA_RPM, PEP_FAMILY="deb", PEP_CONTAINER_ALIAS="debian13-amd64", PEP_EXPECTED_RPM=None,
               PEP_EXPECTED_DEB="9.8.7-1.trixie")
# A daily test build (tag v9.8.7-test1 -> build number test1_1).
_TEST_DEB = dict(_GA_DEB, PEP_CHANNEL="daily", REPO="daily", PEP_EXPECTED_BUILDNUM="test1_1",
                 PEP_EFFECTIVE_TAG="v9.8.7-test1", PEP_EXPECTED_DEB="9.8.7~test1-1.trixie")
_STANDALONE = {"PG_MAJOR_VERSION": "17", "CONTAINERS": "oel9-amd64", "DEB_CONTAINERS": "debian13-amd64",
               "MCP_COMPONENTS": _FOUR, "DEB_MCP_COMPONENTS": _FOUR,
               "PGEDGE_POSTGRES_MCP_VERSION": "9.8.7", "PGEDGE_POSTGRES_MCP_KB_VERSION": "9.8.6",
               "PGEDGE_NLA_CLI_VERSION": "9.8.5", "PGEDGE_NLA_WEB_VERSION": "9.8.4",
               "DECOUPLED_COMPONENTS_SBOM": "/usr/share"}
_KEYS = sorted(set(_GA_RPM) | set(_GA_DEB) | set(_STANDALONE) | {
    "PEP_EXPECTED_DEB", "PEP_EXPECTED_BINARY", "PEP_MODE", "PEP_SCENARIO", "PLATFORM_FILTER",
    "UPGRADE", "UPGRADE_REPO", "SKIP_CLEANUP"})
_ALL = ["pgedge-postgres-mcp", "pgedge-postgres-mcp-kb", "pgedge-nla-cli", "pgedge-nla-web"]


def load(monkeypatch, env):
    """A fresh copy of the MCP component test under exactly ``env`` (None values unset)."""
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(docker, "from_env", lambda *a, **k: None)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)   # only ``env`` decides
    name = "mcp_wiring_under_test_%d" % next(_counter)
    spec = importlib.util.spec_from_file_location(name, str(_MCP_PATH))
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


def _key(cmd):
    """A command as one string, whether it was passed as a list or a shell string."""
    return cmd if isinstance(cmd, str) else " ".join(cmd)


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
    """The package-manager listing installed_mcp_packages reads (rpm -qa / dpkg-query -W)."""
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qa"]:
            return 0, "".join(n + "\n" for n in names)
        if cmd[:2] == ["dpkg-query", "-W"]:
            return 0, "".join(n + " install ok installed\n" for n in names)
        return 0, ""
    return respond


# --------------------------------------------------------------------------- standalone kept
def test_standalone_keeps_all_four_components_and_the_platform_filter(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    assert m.INTEGRATION_REQUEST is None and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == [("oel9-amd64", "rhel"), ("debian13-amd64", "deb")]
    assert m.all_container_component_combinations == (
        [("oel9-amd64", "rhel", c) for c in _ALL] + [("debian13-amd64", "deb", c) for c in _ALL])
    m = load(monkeypatch, dict(_STANDALONE, PLATFORM_FILTER="deb", DEB_MCP_COMPONENTS="pgedge-nla-cli"))
    assert m.all_containers == [("debian13-amd64", "deb")]
    assert m.all_container_component_combinations == [("debian13-amd64", "deb", "pgedge-nla-cli")]


@pytest.mark.parametrize("component", _ALL)
def test_standalone_installs_each_configured_component_unpinned(monkeypatch, component):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    assert outcome(m.test_mcp_component_install, "oel9-amd64", "rhel", component) == ("passed", "")
    assert pinned.calls == [] and ip.calls == [((c, component), {})]


@pytest.mark.parametrize("component,version", [
    ("pgedge-postgres-mcp", "9.8.7"), ("pgedge-postgres-mcp-kb", "9.8.6"),
    ("pgedge-nla-cli", "9.8.5"), ("pgedge-nla-web", "9.8.4")])
def test_standalone_package_version_check_uses_the_config_version_map(monkeypatch, component, version):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    verify = _Spy((True, "rhel", version + "-1.el9", "ok"))
    monkeypatch.setattr(m.package_management, "verify_package_version", verify)
    assert outcome(m.test_mcp_component_version, "oel9-amd64", "rhel", component) == ("passed", "")
    assert verify.calls[0][0][1:] == (component, version)


@pytest.mark.parametrize("test,env,args", [
    ("test_mcp_component_version", dict(_STANDALONE, PGEDGE_NLA_WEB_VERSION=None), ("pgedge-nla-web",)),
    ("test_mcp_component_upgrade", _STANDALONE, ("pgedge-postgres-mcp",)),
    ("test_mcp_identity", _STANDALONE, ()),
    ("test_verify_sbom", dict(_STANDALONE, DECOUPLED_COMPONENTS_SBOM=None), ()),
], ids=["version-unset", "upgrade-off", "identity-integration-only", "sbom-dir-unset"])
def test_standalone_keeps_its_documented_skips(monkeypatch, test, env, args):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    assert outcome(getattr(m, test), "oel9-amd64", "rhel", *args)[0] == "skipped"


def test_standalone_still_skips_a_missing_container_or_reference(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(missing=True))
    for test, args in (("test_configure_repository", ()), ("test_mcp_component_install", ("pgedge-nla-web",)),
                       ("test_mcp_component_version", ("pgedge-nla-cli",)),
                       ("test_verify_bundled_files", ("pgedge-postgres-mcp-kb",)), ("test_verify_sbom", ()),
                       ("test_mcp_component_uninstall", ("pgedge-postgres-mcp",)), ("test_pgedge_cleanup", ())):
        assert outcome(getattr(m, test), "oel9-amd64", "rhel", *args)[0] == "skipped", test
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for postgres-mcp-kb")))
    assert outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel", "pgedge-postgres-mcp-kb")[0] == "skipped"


# --------------------------------------------------------------------------- integration selection
@pytest.mark.parametrize("env,containers", [
    (dict(_GA_RPM, PLATFORM_FILTER="deb"), [("oel9-amd64", "rhel")]),
    (_GA_DEB, [("debian13-amd64", "deb")]),
], ids=["rpm-overrides-filter", "deb"])
def test_integration_request_selects_the_package_and_platform(monkeypatch, env, containers):
    m = load(monkeypatch, env)
    assert m.INTEGRATION_REQUEST["component"] == "mcp" and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == containers
    # Only the requested server package is exercised; never the CLI, web UI or legacy kb from config.
    assert m.all_container_component_combinations == [c + ("pgedge-postgres-mcp",) for c in containers]
    assert m.get_mcp_components_for_container("rhel") == ["pgedge-postgres-mcp"]
    assert m.get_mcp_components_for_container("deb") == ["pgedge-postgres-mcp"]
    assert m.rhel_mcp_components is not m.deb_mcp_components   # two lists, not one aliased list


@pytest.mark.parametrize("package", ["pgedge-nla-cli", "pgedge-nla-web"])
def test_a_request_for_a_built_but_uncertified_package_is_rejected_at_load(monkeypatch, package):
    with pytest.raises(Exception, match="runtime_not_certified"):
        load(monkeypatch, dict(_GA_RPM, PEP_PACKAGE_NAME=package))


@pytest.mark.parametrize("package", ["pgedge-postgres-mcp-kb", "pgedge-radar"])
def test_a_request_for_another_package_is_rejected_at_load(monkeypatch, package):
    with pytest.raises(Exception, match="does not match component 'mcp'"):
        load(monkeypatch, dict(_GA_RPM, PEP_PACKAGE_NAME=package))


def test_the_backstop_names_each_request_it_cannot_serve(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    req = dict(m.INTEGRATION_REQUEST)
    assert m.integration_problem(req, "17") is None
    assert "PG17 disagrees with PG_MAJOR_VERSION=16" in m.integration_problem(req, "16")
    assert "component 'radar' is not mcp" in m.integration_problem(dict(req, component="radar"), "17")
    assert "is not an mcp package" in m.integration_problem(dict(req, package_name="pgedge-nla-cli"), "17")
    assert m.integration_problem(None, "17") is None
    # A valid request routed to the wrong test (here a radar request) is refused, not installed.
    m = load(monkeypatch, dict(_GA_RPM, PEP_COMPONENT="radar", PEP_PACKAGE_NAME="pgedge-radar"))
    assert "component 'radar' is not mcp" in m.INTEGRATION_PROBLEM


# --------------------------------------------------------------------------- install
@pytest.mark.parametrize("env,ctype,exact", [(_GA_RPM, "rhel", "9.8.7-1.el9"),
                                             (_TEST_DEB, "deb", "9.8.7~test1-1.trixie")], ids=["rpm", "deb"])
def test_only_the_requested_package_is_installed_pinned_with_its_checksum(monkeypatch, tmp_path, env, ctype, exact):
    m = load(monkeypatch, env)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-postgres-mcp", "pgedge-ai-kb-x")))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    assert outcome(m.test_mcp_component_install, "x", ctype, "pgedge-postgres-mcp") == ("passed", "")
    assert ip.calls == []                                   # no prerequisite, no unpinned install
    assert pinned.calls[0][0][1:] == ("pgedge-postgres-mcp", exact)
    marker = json.loads((tmp_path / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "a" * 64
    assert not (tmp_path / "prerequisite-versions.json").exists()     # nothing besides the request


@pytest.mark.parametrize("component", ["pgedge-nla-cli", "pgedge-nla-web", "pgedge-postgres-mcp-kb"])
def test_integration_mode_never_installs_another_mcp_package(monkeypatch, tmp_path, component):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    status, msg = outcome(m.test_mcp_component_install, "oel9-amd64", "rhel", component)
    assert status == "failed" and "installs only the requested 'pgedge-postgres-mcp'" in msg
    assert ip.calls == [] and pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_a_request_the_backstop_rejects_is_refused_before_any_install(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m, "INTEGRATION_PROBLEM", m.integration_problem(m.INTEGRATION_REQUEST, "16"))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "a" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    status, msg = outcome(m.test_mcp_component_install, "oel9-amd64", "rhel", "pgedge-postgres-mcp")
    assert status == "failed" and "cannot be served" in msg and "PG_MAJOR_VERSION=16" in msg
    assert ip.calls == [] and pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_a_failed_pinned_install_fails_without_install_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((False, "checksum mismatch", None)))
    status, msg = outcome(m.test_mcp_component_install, "oel9-amd64", "rhel", "pgedge-postgres-mcp")
    assert status == "failed" and "pinned install of pgedge-postgres-mcp=9.8.7-1.el9 failed: checksum mismatch" in msg
    assert not (tmp_path / "install-evidence.json").exists()


@pytest.mark.parametrize("ctype,present", [
    ("rhel", ("pgedge-postgres-mcp", "pgedge-nla-cli")),          # the CLI beside the server
    ("deb", ("pgedge-postgres-mcp", "pgedge-nla-web")),           # the web UI beside the server
    ("rhel", ("pgedge-postgres-mcp", "pgedge-postgres-mcp-kb")),  # the legacy knowledge-base package
    ("deb", ("pgedge-postgres-mcp", "pgedge-nla")),               # any other pgedge-nla* package
    ("rhel", ()),                                                 # the request is not installed at all
], ids=["rpm-cli", "deb-web", "rpm-legacy-kb", "deb-nla-other", "absent"])
def test_any_mcp_package_other_than_the_request_fails_the_install_step(monkeypatch, tmp_path, ctype, present):
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(*present, "pgedge-postgresql17-server")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((True, "ok", "a" * 64)))
    status, msg = outcome(m.test_mcp_component_install, "x", ctype, "pgedge-postgres-mcp")
    assert status == "failed" and "expected exactly the requested MCP package" in msg


def _with_mcp_policy(monkeypatch, m, certified, uncertified):
    """Point the shared policy, as the module under test reads it, at other mcp package names."""
    policy = dict(m._pr.COMPONENT_POLICY)
    policy["mcp"] = dict(policy["mcp"], allowed_runtime_package_names=list(certified),
                         uncertified_runtime_package_names=list(uncertified))
    monkeypatch.setattr(m._pr, "COMPONENT_POLICY", policy)
    monkeypatch.setattr(m._pr, "COMPONENT_PACKAGES", dict(m._pr.COMPONENT_PACKAGES, mcp=tuple(certified)))


@pytest.mark.parametrize("ctype", ["rhel", "deb"])
def test_an_uncertified_companion_named_outside_the_prefixes_still_fails_the_install_step(monkeypatch, tmp_path,
                                                                                         ctype):
    # Every mcp name in the policy starts with pgedge-postgres-mcp or pgedge-nla today, so the prefixes
    # alone find them. The policy lookup is what still finds a companion the policy names otherwise.
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    listing = _pkg_list("pgedge-postgres-mcp", "pgedge-mcp-cli", "pgedge-postgresql17-server")
    assert m.installed_mcp_packages(_Container(listing), ctype) == ["pgedge-postgres-mcp"]   # not named today
    _with_mcp_policy(monkeypatch, m, ["pgedge-postgres-mcp"], ["pgedge-mcp-cli", "pgedge-nla-web"])
    assert m.installed_mcp_packages(_Container(listing), ctype) == ["pgedge-mcp-cli", "pgedge-postgres-mcp"]
    _wire(monkeypatch, tmp_path, m, _Container(listing))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((True, "ok", "a" * 64)))
    status, msg = outcome(m.test_mcp_component_install, "x", ctype, "pgedge-postgres-mcp")
    assert status == "failed" and "found ['pgedge-mcp-cli', 'pgedge-postgres-mcp']" in msg


@pytest.mark.parametrize("ctype", ["rhel", "deb"])
def test_a_certified_name_outside_the_prefixes_is_still_counted(monkeypatch, ctype):
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    _with_mcp_policy(monkeypatch, m, ["pgedge-mcp-server"], ["pgedge-nla-cli", "pgedge-nla-web"])
    listing = _pkg_list("pgedge-mcp-server", "pgedge-postgresql17-server")
    assert m.installed_mcp_packages(_Container(listing), ctype) == ["pgedge-mcp-server"]


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
    probe = _Spy("9.8.7")
    monkeypatch.setattr(m.package_management, "query_binary_version", probe)
    status, msg = outcome(m.test_mcp_identity, "oel9-amd64", "rhel")
    assert status == ("passed" if ok else "failed")
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")
    # The server has no --version flag and its version constant is set by hand, not from the tag: no binary probe.
    assert probe.calls == []


def test_identity_without_install_evidence_fails_with_persisted_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _GA_RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_nothing))
    status, msg = outcome(m.test_mcp_identity, "oel9-amd64", "rhel")
    assert status == "failed" and "install-before-identity precondition failed" in msg
    assert (tmp_path / "identity-evidence.json").exists()


@pytest.mark.parametrize("test,why", [
    ("test_mcp_component_version", "superseded by test_mcp_identity"),
    ("test_mcp_component_upgrade", "certification never upgrades"),
], ids=["package-version", "upgrade"])
def test_integration_mode_only_skips_documented_superseded_or_disabled_steps(monkeypatch, test, why):
    m = load(monkeypatch, dict(_GA_RPM, UPGRADE="true", PGEDGE_POSTGRES_MCP_VERSION="9.8.7"))
    up, verify = _Spy((True, "rhel", "upgraded")), _Spy((True, "rhel", "9.8.7-1.el9", "ok"))
    monkeypatch.setattr(m.package_management, "upgrade_package", up)
    monkeypatch.setattr(m.package_management, "verify_package_version", verify)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    status, msg = outcome(getattr(m, test), "oel9-amd64", "rhel", "pgedge-postgres-mcp")
    assert status == "skipped" and why in msg and up.calls == [] and verify.calls == []


# --------------------------------------------------------------------------- required checks
def test_integration_mode_fails_a_missing_container(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(missing=True))
    for test, args in (("test_configure_repository", ()), ("test_mcp_component_install", ("pgedge-postgres-mcp",)),
                       ("test_mcp_identity", ()), ("test_verify_bundled_files", ("pgedge-postgres-mcp",)),
                       ("test_verify_sbom", ()), ("test_mcp_component_uninstall", ("pgedge-postgres-mcp",)),
                       ("test_pgedge_cleanup", ())):
        status, msg = outcome(getattr(m, test), "oel9-amd64", "rhel", *args)
        assert status == "failed" and "not found" in msg, test


def test_integration_mode_fails_a_missing_bundled_file_reference(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for postgres-mcp")))
    status, msg = outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel", "pgedge-postgres-mcp")
    assert status == "failed" and "No expected file found" in msg


def test_a_missing_reference_still_fails_if_skip_or_fail_returns(monkeypatch):
    # _skip_or_fail always raises today; if it ever returned, the check must still fail, not pass.
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_nothing)))
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        _Spy(Exception("No expected file found for postgres-mcp")))
    returning = _Spy(None)
    monkeypatch.setattr(m, "_skip_or_fail", returning)
    status, msg = outcome(m.test_verify_bundled_files, "oel9-amd64", "rhel", "pgedge-postgres-mcp")
    assert returning.calls == [(("No expected file found for postgres-mcp",), {})]
    assert status == "failed" and "No expected file found for postgres-mcp" in msg


def test_integration_mode_fails_without_an_sbom_directory(monkeypatch):
    m = load(monkeypatch, dict(_GA_RPM, DECOUPLED_COMPONENTS_SBOM=None))
    c = _Container(_nothing)
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_verify_sbom, "oel9-amd64", "rhel")
    assert status == "failed" and "DECOUPLED_COMPONENTS_SBOM" in msg and c.cmds == []


_SBOM = "/usr/share/pgedge-postgres-mcp/pgedge-postgres-mcp-sbom.json"


def _sbom(found=_SBOM, owner="pgedge-postgres-mcp", owner_rc=0, sig="1 authenticated signature."):
    def respond(cmd):
        k = _key(cmd)
        if k.startswith("bash -c find /usr/share -maxdepth 3"):
            return 0, (found + "\n") if found else ""
        if cmd[:2] == ["rpm", "-qf"]:
            return owner_rc, owner + "\n"
        if cmd[:2] == ["dpkg-query", "-S"]:
            return owner_rc, "%s: %s\n" % (owner, cmd[2])
        if k.startswith("wget -q -O /usr/share/pgedge-postgres-mcp/pgedge-rsa.pub"):
            return 0, ""
        if k == "sq verify --help 2>&1":
            return 0, "--signer-file --signature-file"
        if "sq verify" in k and "pgedge-postgres-mcp-sbom.json.asc" in k:
            return 0, sig + "\n"
        return 1, "unexpected %r" % (cmd,)
    return respond


@pytest.mark.parametrize("env,ctype", [(_GA_RPM, "rhel"), (_GA_DEB, "deb"), (_STANDALONE, "rhel"),
                                       (_STANDALONE, "deb")],
                         ids=["integration-rpm", "integration-deb", "standalone-rpm", "standalone-deb"])
def test_the_requested_package_sbom_signature_is_verified(monkeypatch, env, ctype):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m.machine_prereq_setup, "ensure_sq_installed", lambda c: None)
    c = _Container(_sbom())
    monkeypatch.setattr(m, "client", _Client(c))
    assert outcome(m.test_verify_sbom, "x", ctype) == ("passed", "")
    owner_checked = any(cmd[:2] in (["rpm", "-qf"], ["dpkg-query", "-S"]) for cmd in c.cmds if isinstance(cmd, list))
    assert owner_checked == (env is not _STANDALONE)        # standalone unchanged: no ownership query


@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_a_missing_sbom_fails_in_both_modes(monkeypatch, env):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_sbom(found=None))))
    status, msg = outcome(m.test_verify_sbom, "x", "rhel")
    assert status == "failed" and "not found anywhere under /usr/share" in msg


@pytest.mark.parametrize("ctype,owner,rc", [
    ("rhel", "pgedge-postgres-mcp-kb", 0), ("deb", "pgedge-nla-cli", 0),
    ("rhel", "file %s is not owned by any package" % _SBOM, 1),
], ids=["rpm-other-owner", "deb-other-owner", "unowned"])
def test_an_sbom_the_requested_package_does_not_own_fails_in_integration_mode(monkeypatch, ctype, owner, rc):
    m = load(monkeypatch, _GA_RPM if ctype == "rhel" else _GA_DEB)
    monkeypatch.setattr(m.machine_prereq_setup, "ensure_sq_installed", lambda c: None)
    c = _Container(_sbom(owner=owner, owner_rc=rc))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_verify_sbom, "x", ctype)
    assert status == "failed" and _SBOM in msg and "pgedge-postgres-mcp" in msg
    assert not any("sq verify" in _key(cmd) for cmd in c.cmds)            # never verified


def test_a_bad_sbom_signature_fails(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_sbom(sig="0 authenticated signatures."))))
    assert outcome(m.test_verify_sbom, "x", "rhel")[0] == "failed"


# path_owners reads the package manager's raw answer for the located SBOM path. The _sbom fake above
# always answers for the queried path; these feed it other answers.
_SAME_BASENAME = "/opt/x/pgedge-postgres-mcp-sbom.json"
_DIVERTED = ("diversion by pgedge-nla-cli from: %s\ndiversion by pgedge-nla-cli to: %s.distrib\n"
             "pgedge-postgres-mcp: %s\n" % (_SBOM, _SBOM, _SBOM))


@pytest.mark.parametrize("rc,answer,owners", [
    (0, "pgedge-postgres-mcp: %s\n" % _SBOM, ["pgedge-postgres-mcp"]),
    (0, "pgedge-postgres-mcp:amd64: %s\n" % _SBOM, ["pgedge-postgres-mcp"]),
    (0, "pgedge-postgres-mcp: %s\n" % _SAME_BASENAME, []),
    (0, "pgedge-nla-cli: %s\n" % _SAME_BASENAME, []),
    (0, "pgedge-postgres-mcp: %s.asc\n" % _SBOM, []),
    (0, "pgedge-nla-cli: %s\npgedge-postgres-mcp: %s\n" % (_SAME_BASENAME, _SBOM), ["pgedge-postgres-mcp"]),
    (0, "pgedge-nla-cli: %s\n" % _SBOM, ["pgedge-nla-cli"]),
    (0, "pgedge-postgres-mcp, pgedge-nla-cli: %s\n" % _SBOM, ["pgedge-nla-cli", "pgedge-postgres-mcp"]),
    (1, "dpkg-query: no path found matching pattern %s\n" % _SBOM, []),
    (2, "pgedge-postgres-mcp: %s\ndpkg-query: error: cannot read the package database\n" % _SBOM, []),
], ids=["exact", "arch-qualified", "same-basename-other-dir", "same-basename-other-owner", "longer-path",
        "only-the-exact-line-counts", "other-owner", "two-owners", "unowned", "failed-query-with-output"])
def test_deb_path_owners_accepts_only_owners_of_the_exact_path(monkeypatch, rc, answer, owners):
    m = load(monkeypatch, _GA_DEB)
    c = _Container(lambda cmd: (rc, answer))
    assert m.path_owners(c, "deb", _SBOM) == (owners, answer.strip())
    assert c.cmds == [["dpkg-query", "-S", _SBOM]]          # the literal path, no pattern or resolution


def test_deb_path_owners_does_not_report_a_diverted_path_as_the_requested_package_alone(monkeypatch):
    m = load(monkeypatch, _GA_DEB)
    owners, raw = m.path_owners(_Container(lambda cmd: (0, _DIVERTED)), "deb", _SBOM)
    assert owners != ["pgedge-postgres-mcp"] and raw == _DIVERTED.strip()


@pytest.mark.parametrize("rc,answer,owners", [
    (0, "pgedge-postgres-mcp\n", ["pgedge-postgres-mcp"]),
    (0, "pgedge-postgres-mcp\npgedge-postgres-mcp\n", ["pgedge-postgres-mcp"]),   # one line per installed copy
    (0, "pgedge-nla-cli\n", ["pgedge-nla-cli"]),
    (0, "pgedge-postgres-mcp\npgedge-nla-cli\n", ["pgedge-nla-cli", "pgedge-postgres-mcp"]),
    (1, "file %s is not owned by any package\n" % _SBOM, []),
    (1, "error: file %s: No such file or directory\n" % _SBOM, []),
], ids=["exact", "duplicate-lines", "other-owner", "two-owners", "unowned", "missing"])
def test_rpm_path_owners_reads_the_owning_package_names(monkeypatch, rc, answer, owners):
    m = load(monkeypatch, _GA_RPM)
    c = _Container(lambda cmd: (rc, answer))
    assert m.path_owners(c, "rhel", _SBOM) == (owners, answer.strip())
    assert c.cmds == [["rpm", "-qf", "--queryformat", "%{NAME}\\n", _SBOM]]


@pytest.mark.parametrize("rc,answer,ok", [
    (0, "pgedge-postgres-mcp: %s\n" % _SBOM, True),
    (0, "pgedge-postgres-mcp:amd64: %s\n" % _SBOM, True),
    (0, "pgedge-postgres-mcp: %s\n" % _SAME_BASENAME, False),
    (0, "pgedge-postgres-mcp, pgedge-nla-cli: %s\n" % _SBOM, False),
    (0, _DIVERTED, False),
    (2, "pgedge-postgres-mcp: %s\ndpkg-query: error: cannot read the package database\n" % _SBOM, False),
], ids=["exact", "arch-qualified", "same-basename-other-dir", "two-owners", "diverted", "failed-query-with-output"])
def test_the_sbom_check_goes_on_only_when_the_requested_package_alone_owns_the_located_path(monkeypatch, rc,
                                                                                           answer, ok):
    m = load(monkeypatch, _GA_DEB)
    monkeypatch.setattr(m.machine_prereq_setup, "ensure_sq_installed", lambda c: None)
    others = _sbom()
    c = _Container(lambda cmd: (rc, answer) if cmd[:2] == ["dpkg-query", "-S"] else others(cmd))
    monkeypatch.setattr(m, "client", _Client(c))
    status, msg = outcome(m.test_verify_sbom, "x", "deb")
    verified = any("sq verify" in _key(cmd) and "--help" not in _key(cmd) for cmd in c.cmds)
    if ok:
        assert (status, msg, verified) == ("passed", "", True)
    else:
        assert status == "failed" and "not only the requested 'pgedge-postgres-mcp'" in msg
        assert repr(answer.strip()) in msg and not verified       # dpkg's raw answer is reported


# --------------------------------------------------------------------------- expected-output references
# The payload the MCP server package defines at pgEdge/pgedge-postgres-mcp 6448336 (pkg/ unchanged through
# v1.2.0-beta1 d3030c3 and main a08aa30 apart from the version defaults in pkg/common.sh):
#   RPM pkg/rpm/pgedge-nla.spec %files -n pgedge-postgres-mcp (:146-159; %{_unitdir} on EL9/EL10 is
#   /usr/lib/systemd/system). The kb-builder binary, its yaml and SBOM are no longer shipped (:142-144,
#   changelog :208-211).
#   DEB pkg/deb/debian/rules override_dh_auto_install (:26-34), the unit via dh_installsystemd from
#   debian/pgedge-postgres-mcp.service (pkg/build-deb.sh:74), changelog.Debian from dh_installchangelogs,
#   and dh_compress gzipping README.md (over 4 KiB) but not LICENSE.md.
_RPM_PAYLOAD = ["/etc/pgedge/postgres-mcp.env", "/etc/pgedge/postgres-mcp.yaml", "/usr/bin/pgedge-postgres-mcp",
                "/usr/lib/systemd/system/pgedge-postgres-mcp.service",
                "/usr/share/doc/pgedge-postgres-mcp/LICENSE.md", "/usr/share/doc/pgedge-postgres-mcp/README.md",
                "/usr/share/pgedge-postgres-mcp", "/usr/share/pgedge-postgres-mcp/pgedge-postgres-mcp-sbom.json",
                "/usr/share/pgedge-postgres-mcp/pgedge-postgres-mcp-sbom.json.asc", "/var/lib/pgedge",
                "/var/lib/pgedge/postgres-mcp", "/var/log/pgedge", "/var/log/pgedge/postgres-mcp"]
_DEB_PAYLOAD = ["/etc/pgedge/postgres-mcp.env", "/etc/pgedge/postgres-mcp.yaml", "/usr/bin/pgedge-postgres-mcp",
                "/usr/lib/systemd/system/pgedge-postgres-mcp.service",
                "/usr/share/doc/pgedge-postgres-mcp/LICENSE.md", "/usr/share/doc/pgedge-postgres-mcp/README.md.gz",
                "/usr/share/doc/pgedge-postgres-mcp/changelog.Debian.gz",
                "/usr/share/pgedge-postgres-mcp/pgedge-postgres-mcp-sbom.json",
                "/usr/share/pgedge-postgres-mcp/pgedge-postgres-mcp-sbom.json.asc",
                "/var/lib/pgedge/postgres-mcp", "/var/log/pgedge/postgres-mcp"]


def test_the_expected_output_references_list_the_packaged_server_payload():
    assert (_REPO / "expected-output" / "rpm" / "postgres-mcp").read_text().splitlines() == _RPM_PAYLOAD
    assert (_REPO / "expected-output" / "deb" / "postgres-mcp").read_text().splitlines() == _DEB_PAYLOAD
    for family in ("rpm", "deb"):
        assert "kb-builder" not in (_REPO / "expected-output" / family / "postgres-mcp").read_text()


@pytest.mark.parametrize("ctype,listing_cmd,payload,extras", [
    ("rhel", "rpm -ql pgedge-postgres-mcp", _RPM_PAYLOAD,
     ["/usr/lib/.build-id", "/usr/lib/.build-id/3c", "/usr/lib/.build-id/3c/0123456789abcdef0123456789abcdef012345"]),
    ("deb", "dpkg -L pgedge-postgres-mcp", _DEB_PAYLOAD,
     ["/.", "/etc", "/etc/pgedge", "/usr", "/usr/bin", "/usr/lib", "/usr/lib/systemd", "/usr/lib/systemd/system",
      "/usr/share", "/usr/share/doc", "/usr/share/doc/pgedge-postgres-mcp", "/usr/share/pgedge-postgres-mcp",
      "/var", "/var/lib", "/var/lib/pgedge", "/var/log", "/var/log/pgedge"]),
], ids=["rpm", "deb"])
@pytest.mark.parametrize("env", [_GA_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_the_real_bundled_file_check_finds_the_server_reference(monkeypatch, env, ctype, listing_cmd, payload,
                                                                 extras):
    # The real verify_bundled_files derives "postgres-mcp" from pgedge-postgres-mcp and compares the
    # package's file list against expected-output/<family>/postgres-mcp: every listed file must be
    # installed; entries the package manager adds around the payload are reported as extra.
    m = load(monkeypatch, env)

    def installed(files):
        return lambda cmd: (0, "".join(f + "\n" for f in files)) if cmd == listing_cmd else (1, "unexpected")
    monkeypatch.setattr(m, "client", _Client(_Container(installed(payload + extras))))
    assert outcome(m.test_verify_bundled_files, "x", ctype, "pgedge-postgres-mcp") == ("passed", "")
    # The binary and the SBOM signature. (A missing state directory: see
    # test_integration_mode_requires_the_deb_state_directories_at_their_exact_paths.)
    for missing in ("/usr/bin/pgedge-postgres-mcp", "/usr/share/pgedge-postgres-mcp/pgedge-postgres-mcp-sbom.json.asc"):
        monkeypatch.setattr(m, "client", _Client(_Container(installed([f for f in payload if f != missing]))))
        status, msg = outcome(m.test_verify_bundled_files, "x", ctype, "pgedge-postgres-mcp")
        assert status == "failed" and "Missing files" in msg, missing


_STATE_DIRS = ["/var/lib/pgedge/postgres-mcp", "/var/log/pgedge/postgres-mcp"]
_DEB_EXTRAS = ["/.", "/etc", "/etc/pgedge", "/usr", "/usr/bin", "/usr/lib", "/usr/lib/systemd",
               "/usr/lib/systemd/system", "/usr/share", "/usr/share/doc", "/usr/share/doc/pgedge-postgres-mcp",
               "/usr/share/pgedge-postgres-mcp", "/var", "/var/lib", "/var/lib/pgedge", "/var/log", "/var/log/pgedge"]


def _dpkg_listing(files):
    return lambda cmd: (0, "".join(f + "\n" for f in files)) if cmd == "dpkg -L pgedge-postgres-mcp" else (1, "")


@pytest.mark.parametrize("gone", _STATE_DIRS, ids=["lib", "log"])
def test_integration_mode_requires_the_deb_state_directories_at_their_exact_paths(monkeypatch, gone):
    # Both directories are named postgres-mcp, so the shared Debian comparison resolves a missing one
    # to the other by name and passes; integration mode also checks each exact path in `dpkg -L`.
    listing = [f for f in _DEB_PAYLOAD + _DEB_EXTRAS if f != gone]
    m = load(monkeypatch, _STANDALONE)
    ok, details, _ = m.file_management.verify_bundled_files(
        _Container(_dpkg_listing(listing)), "x", "deb", "pgedge-postgres-mcp", "pgedge-postgres-mcp", _REPO)
    assert ok and details["missing_files"] == []               # the shared comparison alone accepts it
    monkeypatch.setattr(m, "client", _Client(_Container(_dpkg_listing(listing))))
    assert outcome(m.test_verify_bundled_files, "x", "deb", "pgedge-postgres-mcp") == ("passed", "")  # standalone
    for env in (_GA_DEB, _TEST_DEB):
        m = load(monkeypatch, env)
        monkeypatch.setattr(m, "client", _Client(_Container(_dpkg_listing(listing))))
        status, msg = outcome(m.test_verify_bundled_files, "x", "deb", "pgedge-postgres-mcp")
        assert status == "failed" and "does not list ['%s'] at the exact path" % gone in msg, msg
        monkeypatch.setattr(m, "client", _Client(_Container(_dpkg_listing(_DEB_PAYLOAD + _DEB_EXTRAS))))
        assert outcome(m.test_verify_bundled_files, "x", "deb", "pgedge-postgres-mcp") == ("passed", "")


def test_integration_mode_requires_the_deb_binary_at_its_exact_path(monkeypatch):
    # The package installs the server at /usr/bin/pgedge-postgres-mcp (pkg/deb/debian/rules). With it
    # under /opt/bin instead, the shared Debian comparison resolves the missing entry to the installed
    # path that also ends in bin/pgedge-postgres-mcp and passes; integration mode checks the exact path.
    moved = [f for f in _DEB_PAYLOAD + _DEB_EXTRAS if f not in ("/usr/bin/pgedge-postgres-mcp", "/usr/bin")]
    moved += ["/opt", "/opt/bin", "/opt/bin/pgedge-postgres-mcp"]
    m = load(monkeypatch, _STANDALONE)
    ok, details, _ = m.file_management.verify_bundled_files(
        _Container(_dpkg_listing(moved)), "x", "deb", "pgedge-postgres-mcp", "pgedge-postgres-mcp", _REPO)
    assert ok and details["missing_files"] == []               # the shared comparison alone accepts it
    monkeypatch.setattr(m, "client", _Client(_Container(_dpkg_listing(moved))))
    assert outcome(m.test_verify_bundled_files, "x", "deb", "pgedge-postgres-mcp") == ("passed", "")  # standalone
    for env in (_GA_DEB, _TEST_DEB):
        m = load(monkeypatch, env)
        monkeypatch.setattr(m, "client", _Client(_Container(_dpkg_listing(moved))))
        status, msg = outcome(m.test_verify_bundled_files, "x", "deb", "pgedge-postgres-mcp")
        assert status == "failed" and "does not list ['/usr/bin/pgedge-postgres-mcp'] at the exact path" in msg, msg
        monkeypatch.setattr(m, "client", _Client(_Container(_dpkg_listing(_DEB_PAYLOAD + _DEB_EXTRAS))))
        assert outcome(m.test_verify_bundled_files, "x", "deb", "pgedge-postgres-mcp") == ("passed", "")


def test_the_rpm_comparison_already_requires_the_state_directories(monkeypatch):
    m = load(monkeypatch, _GA_RPM)
    for gone in _STATE_DIRS:
        listing = [f for f in _RPM_PAYLOAD if f != gone]
        monkeypatch.setattr(m, "client", _Client(_Container(
            lambda cmd: (0, "".join(f + "\n" for f in listing)) if cmd == "rpm -ql pgedge-postgres-mcp" else (1, ""))))
        status, msg = outcome(m.test_verify_bundled_files, "x", "rhel", "pgedge-postgres-mcp")
        assert status == "failed" and "Missing files (1)" in msg and gone in msg, msg


def test_a_failed_second_listing_fails_the_exact_path_check(monkeypatch):
    m = load(monkeypatch, _GA_DEB)
    calls = []

    def respond(cmd):
        calls.append(cmd)
        if cmd == "dpkg -L pgedge-postgres-mcp" and len(calls) == 1:
            return 0, "".join(f + "\n" for f in _DEB_PAYLOAD + _DEB_EXTRAS)
        return 1, "dpkg-query: package 'pgedge-postgres-mcp' is not installed"
    monkeypatch.setattr(m, "client", _Client(_Container(respond)))
    status, msg = outcome(m.test_verify_bundled_files, "x", "deb", "pgedge-postgres-mcp")
    assert status == "failed" and "dpkg -L pgedge-postgres-mcp failed" in msg and len(calls) == 2, (msg, calls)


def test_a_deb_unit_under_lib_systemd_still_matches(monkeypatch):
    # Older debhelper (jammy, bookworm) installs units under /lib/systemd/system; the reference names
    # /usr/lib/systemd/system and the Debian basename match accepts either location. The exact-path
    # check in integration mode covers the binary and the two state directories, not the unit, so either
    # unit location passes.
    m = load(monkeypatch, _GA_DEB)
    files = [f.replace("/usr/lib/systemd/", "/lib/systemd/") for f in _DEB_PAYLOAD]
    monkeypatch.setattr(m, "client", _Client(_Container(
        lambda cmd: (0, "".join(f + "\n" for f in files)) if cmd == "dpkg -L pgedge-postgres-mcp" else (1, ""))))
    assert outcome(m.test_verify_bundled_files, "x", "deb", "pgedge-postgres-mcp") == ("passed", "")
