"""Docker-free tests that exercise the REAL functions in component-test/test_pep_snowflake.py.

The module is loaded by path with docker.from_env and load_dotenv neutralized and with exactly the
environment of the case under test (the platform filter, integration request, backstop and
snowflake.node are decided at import time), then driven with a fake docker client/container and
spied package helpers -- the same approach as test_pep_spock_wiring.py. Versions are synthetic
(9.8.7) so nothing here depends on today's snowflake release.

Covers: standalone behaviour preserved; the request family overriding PLATFORM_FILTER; requests for
the wrong package, PG major, family or component rejected before any install; the server companion
installed before the pinned package; only the requested snowflake package installed (an -llvmjit
companion or another PG major fails); the identity step; the library owner, server major and exact
extension version; missing references failing in integration mode and skipping standalone; the
expected file lists against the published packages' own listings, with the license required in both
families; and the snowflake checks (exact node, strict stripped check, id burst, epoch bracket, node
enforcement, per-sequence ids, sequence conversion).
"""
import importlib.util
import itertools
import json
import os
import sys

import docker
import dotenv
import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "component-test", "test_pep_snowflake.py")
_counter = itertools.count()

# Config-file style values that disagree with the requests below: integration mode must ignore them.
_CONFIG = {"SNOWFLAKE_PACKAGE": "pgedge-snowflake_16", "DEB_SNOWFLAKE_PACKAGE": "pgedge-postgresql-16-snowflake",
           "PGEDGE_SNOWFLAKE_17_VERSION": "2.6.0", "SNOWFLAKE_NODE": "1", "REPO": "staging",
           "CONTAINERS": "rocky9-amd64", "DEB_CONTAINERS": "debian13-amd64"}
_RPM = dict(_CONFIG, PEP_INTEGRATION_MODE="1", PEP_COMPONENT="snowflake", PEP_PACKAGE_NAME="pgedge-snowflake_17",
            PEP_CHANNEL="staging", PEP_EXPECTED_VERSION="9.8.7", PEP_FAMILY="rpm", PEP_ARCH_FILTER="amd64",
            PEP_CONTAINER_ALIAS="rocky9-amd64", PG_MAJOR_VERSION="17", PEP_EXPECTED_RPM="9.8.7-1.el9",
            PEP_EXPECTED_BUILDNUM="1", PLATFORM_FILTER="deb")
_DEB = dict(_RPM, PEP_PACKAGE_NAME="pgedge-postgresql-17-snowflake", PEP_FAMILY="deb",
            PEP_CONTAINER_ALIAS="debian13-amd64", PEP_EXPECTED_RPM=None, PEP_EXPECTED_DEB="9.8.7-1.trixie",
            PLATFORM_FILTER="rpm")
_STANDALONE = dict(_CONFIG, SNOWFLAKE_PACKAGE="pgedge-snowflake_17", DEB_SNOWFLAKE_PACKAGE=None,
                   PG_MAJOR_VERSION="17", REPO="release")
_KEYS = sorted(set(_RPM) | set(_DEB) | {
    "PEP_EXPECTED_DEB", "PEP_EFFECTIVE_TAG", "PEP_EXPECTED_BINARY", "PEP_SCENARIO", "PEP_MODE", "UPGRADE",
    "UPGRADE_REPO", "SKIP_CLEANUP", "PG_PORT", "PG_DATA_DIR", "PG_USER", "DEB_PG_USER", "PG_BIN_PATH",
    "DEB_PG_BIN_PATH", "RHEL_PG_PATH", "DEB_PG_PATH", "DEB_PG_SHARE_PATH", "RHEL_SNOWFLAKE_LIB",
    "DEB_SNOWFLAKE_LIB", "SNOWFLAKE_STRIPPED_LIB"})


def load(monkeypatch, env):
    """Load a fresh copy of the snowflake component test under exactly ``env`` (None values unset)."""
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    orig_docker, orig_dotenv = docker.from_env, dotenv.load_dotenv
    docker.from_env = lambda *a, **k: None
    dotenv.load_dotenv = lambda *a, **k: False       # never read a local .env: the case's env is exact
    try:
        name = "snowflake_under_test_%d" % next(_counter)
        spec = importlib.util.spec_from_file_location(name, _PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        docker.from_env, dotenv.load_dotenv = orig_docker, orig_dotenv


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
                if container is None:
                    raise docker.errors.NotFound("No such container: %s" % name)
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
    """exec_run responder for the package-manager listing behind the only-package check."""
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qa"]:
            return 0, "\n".join(names) + "\n"
        if cmd[0] == "dpkg-query" and cmd[1] == "-W":
            return 0, "".join("%s install ok installed\n" % n for n in names)
        return 0, ""
    return respond


def _psql(session, query=lambda cmd: (0, "")):
    """exec_run responder: psql_session calls (list commands) are answered by ``session(statements)``,
    pg_server_management.execute_psql_query calls (string commands) by ``query(cmd)``."""
    def respond(cmd):
        if isinstance(cmd, list) and cmd[0].endswith("/psql"):
            return session([cmd[i + 1] for i, a in enumerate(cmd) if a == "-c"])
        if isinstance(cmd, str) and "/psql " in cmd:
            return query(cmd)
        return 1, "unexpected %r" % (cmd,)
    return respond


def _wire(monkeypatch, tmp_path, m, container):
    monkeypatch.setattr(m, "client", _Client(container))
    for k, f in (("PEP_INSTALL_OUT", "install-evidence.json"), ("PEP_IDENTITY_OUT", "identity-evidence.json"),
                 ("PEP_OBSERVED_OUT", "observed-identity.json"), ("PEP_PREREQ_OUT", "prerequisite-versions.json")):
        monkeypatch.setenv(k, str(tmp_path / f))
    monkeypatch.setenv("PEP_RUN_TOKEN", "run-A")


def _outcome(fn, *args):
    """Run a real test function and report ("passed"|"skipped"|"failed", message), so an unexpected
    skip is never reported as a skipped test here."""
    try:
        fn(*args)
    except pytest.skip.Exception as e:
        return "skipped", str(e)
    except (pytest.fail.Exception, AssertionError) as e:
        return "failed", str(e)
    return "passed", ""


def sf(msec, node, count):
    """A snowflake id from its fields (snowflake.c int64FromSnowflake)."""
    return (msec << 22) | (node << 12) | count


# --------------------------------------------------------------------------- selection and backstop
def test_standalone_keeps_the_config_packages_and_platform_filter(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, PLATFORM_FILTER="deb"))
    assert m.INTEGRATION_REQUEST is None and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == [("debian13-amd64", "deb")]
    assert m.get_container_config("rhel")["snowflake_package"] == "pgedge-snowflake_17"
    assert m.get_container_config("deb")["snowflake_package"] == "pgedge-postgresql-17-snowflake"
    assert m.snowflake_version == "2.6.0"
    m = load(monkeypatch, dict(_STANDALONE, PLATFORM_FILTER=None))
    assert m.all_containers == [("rocky9-amd64", "rhel"), ("debian13-amd64", "deb")]


@pytest.mark.parametrize("env,containers", [(_RPM, [("rocky9-amd64", "rhel")]),
                                            (_DEB, [("debian13-amd64", "deb")])], ids=["rpm", "deb"])
def test_the_request_selects_the_platform_and_package_never_the_config(monkeypatch, env, containers):
    m = load(monkeypatch, env)                   # PLATFORM_FILTER and *SNOWFLAKE_PACKAGE disagree
    assert m.INTEGRATION_PROBLEM is None
    assert m.all_containers == containers
    assert (m.get_container_config("rhel")["snowflake_package"]
            == m.get_container_config("deb")["snowflake_package"] == env["PEP_PACKAGE_NAME"])


@pytest.mark.parametrize("over,reason", [
    ({"PEP_PACKAGE_NAME": "pgedge-snowflake_16"}, "pg_major_mismatch"),
    ({"PEP_PACKAGE_NAME": "pgedge-postgresql-17-snowflake"}, "package_family_mismatch"),
    ({"PEP_PACKAGE_NAME": "pgedge-snowflake_17-llvmjit"}, "runtime_not_certified"),
    ({"PEP_PACKAGE_NAME": "pgedge-snowflake17"}, "package_not_allowed"),
    ({"PEP_COMPONENT": "spock"}, "package_not_allowed"),
], ids=["other-pg", "other-family", "llvmjit", "malformed", "other-component"])
def test_a_request_the_policy_rejects_fails_at_load_before_any_install(monkeypatch, over, reason):
    with pytest.raises(Exception, match=reason):
        load(monkeypatch, dict(_RPM, **over))


def test_the_backstop_names_each_problem(monkeypatch):
    m = load(monkeypatch, _RPM)
    req = dict(m.INTEGRATION_REQUEST)
    assert m._integration_problem(req, "17") is None
    assert "PG_MAJOR_VERSION=16" in m._integration_problem(req, "16")
    assert "pg_major_mismatch" in m._integration_problem(dict(req, package_name="pgedge-snowflake_16"), "17")
    assert "package_family_mismatch" in m._integration_problem(dict(req, family="deb"), "17")
    assert "runtime_not_certified" in m._integration_problem(
        dict(req, package_name="pgedge-snowflake_17-llvmjit"), "17")
    assert "not the PG-coupled snowflake" in m._integration_problem(dict(req, component="rag"), "17")
    assert m._integration_problem(None, "17") is None


def test_another_components_request_is_refused_before_any_install(monkeypatch, tmp_path):
    # A valid spock request reaching the snowflake test: it loads, but the backstop refuses it.
    m = load(monkeypatch, dict(_RPM, PEP_COMPONENT="spock", PEP_PACKAGE_NAME="pgedge-spock50_17",
                               PEP_EXPECTED_VERSION="5.0.13", PEP_EXPECTED_RPM="5.0.13-1.el9"))
    assert "not the PG-coupled snowflake component" in m.INTEGRATION_PROBLEM
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    with pytest.raises(AssertionError, match="cannot be served"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert ip.calls == [] and pinned.calls == []


def test_install_refuses_a_request_the_backstop_rejects(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m, "INTEGRATION_PROBLEM", "request PG17 disagrees with PG_MAJOR_VERSION=16")
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    with pytest.raises(AssertionError, match="cannot be served"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert ip.calls == [] and pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_the_component_test_shares_the_helper_instances(monkeypatch):
    # Patching package_management / pep_evidence must reach the shared install + identity steps.
    m = load(monkeypatch, _RPM)
    assert m.pep_integration_mode is m.pep_extension_checks.pep_integration_mode
    assert m.pep_request_env is m.pep_integration_mode.pep_request_env
    assert m._pr is m.pep_extension_checks._pr is sys.modules["pep_request"]
    assert m._pp is m.pep_extension_checks._pp is sys.modules["pep_package_policy"]
    assert m.package_management is m.pep_integration_mode.package_management


# --------------------------------------------------------------------------- install
def test_companion_first_then_the_pinned_request_and_versions_are_recorded(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-snowflake_17", "pgedge-postgresql17-server",
                                                         "pgedge-snowflake_17-debuginfo")))
    order = []
    ip = _Spy((True, "rhel", "installed"), order, "companion")
    pinned = _Spy((True, "ok", "f" * 64), order, "pinned")
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    monkeypatch.setattr(m.package_management, "query_installed_version",
                        lambda c, n: {"pgedge-snowflake_17": "9.8.7-1.el9"}.get(n, "17.11-1PGEDGE.el9"))

    m.test_component_install("rocky9-amd64", "rhel")

    assert order == ["companion", "pinned"]
    assert ip.calls[0][1]["package_name"] == ["pgedge-postgresql17-server"]
    assert pinned.calls[0][0][1:] == ("pgedge-snowflake_17", "9.8.7-1.el9")
    marker = json.loads((tmp_path / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "f" * 64
    assert json.loads((tmp_path / "prerequisite-versions.json").read_text()) == {
        "channel": "staging", "pg_major": "17", "requested_package": "pgedge-snowflake_17",
        "installed_versions": {"pgedge-postgresql17-server": "17.11-1PGEDGE.el9",
                               "pgedge-snowflake_17": "9.8.7-1.el9"}}


def test_deb_companion_is_the_server_for_the_requested_major(monkeypatch, tmp_path):
    m = load(monkeypatch, _DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-postgresql-17-snowflake", "pgedge-postgresql-17")))
    ip, pinned = _Spy((True, "debian", "installed")), _Spy((True, "ok", "e" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    m.test_component_install("debian13-amd64", "deb")
    assert ip.calls[0][1]["package_name"] == ["pgedge-postgresql-17"]
    assert pinned.calls[0][0][1:] == ("pgedge-postgresql-17-snowflake", "9.8.7-1.trixie")


def test_missing_prerequisite_is_reported_as_such_and_nothing_is_pinned(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    pinned = _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package",
                        _Spy(Exception("Failed to install pgedge-postgresql17-server: No match")))
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    with pytest.raises(pytest.fail.Exception, match="PostgreSQL 17 prerequisite unavailable from the 'staging' channel"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_failed_pinned_install_fails_without_install_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((False, "checksum mismatch", None)))
    with pytest.raises(AssertionError, match="pinned install of pgedge-snowflake_17=9.8.7-1.el9 failed: checksum mismatch"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert not (tmp_path / "install-evidence.json").exists()


@pytest.mark.parametrize("present", [
    ("pgedge-snowflake_17", "pgedge-snowflake_17-llvmjit"),        # the uncertified JIT companion
    ("pgedge-snowflake_17", "pgedge-snowflake_16"),                # another PG major beside the request
    ("pgedge-snowflake_17", "pgedge-postgresql-17-snowflake"),     # the other family's package
    ("pgedge-snowflake_16",),                                      # the request replaced
    (),                                                            # nothing installed
], ids=["llvmjit", "other-pg", "other-family", "replaced", "none"])
def test_any_snowflake_package_but_exactly_the_request_fails_the_install(monkeypatch, tmp_path, present):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(*present, "pgedge-postgresql17-server")))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((True, "ok", "f" * 64)))
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    with pytest.raises(AssertionError, match="expected exactly the requested snowflake package"):
        m.test_component_install("rocky9-amd64", "rhel")


def test_standalone_install_is_unchanged(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    m.test_component_install("rocky9-amd64", "rhel")
    assert pinned.calls == [] and len(ip.calls) == 1
    assert ip.calls[0][0][1:] == ("pgedge-snowflake_17",) and ip.calls[0][1] == {}


def test_the_pinned_package_is_never_upgraded_in_integration_mode(monkeypatch, tmp_path):
    m = load(monkeypatch, dict(_RPM, UPGRADE="true"))
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    up = _Spy((True, "rhel", "upgraded"))
    monkeypatch.setattr(m.package_management, "upgrade_package", up)
    assert _outcome(m.test_component_upgrade, "rocky9-amd64", "rhel")[0] == "skipped" and up.calls == []
    m = load(monkeypatch, _STANDALONE)                     # standalone: UPGRADE unset still skips
    assert _outcome(m.test_component_upgrade, "rocky9-amd64", "rhel") == (
        "skipped", "Skipping upgrade tests because UPGRADE=false in env")


# --------------------------------------------------------------------------- identity
@pytest.mark.parametrize("observed,ok", [("9.8.7-1.el9", True), ("9.8.6-1.el9", False),
                                         ("9.8.7-rc1_1.el9", False), ("2.6.0-1.el9", False)],
                         ids=["exact", "older", "rc", "config-release"])
def test_identity_proves_the_exact_requested_version(monkeypatch, tmp_path, observed, ok):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    m.pep_integration_mode.pep_evidence.write_install_evidence(
        m.INTEGRATION_REQUEST, "run-A", "pinned", "9.8.7-1.el9", str(tmp_path / "install-evidence.json"),
        installed_sha256="f" * 64)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: observed)
    if ok:
        m.test_snowflake_identity("rocky9-amd64", "rhel")
    else:
        with pytest.raises(AssertionError, match="not proven"):
            m.test_snowflake_identity("rocky9-amd64", "rhel")
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")


def test_identity_without_install_evidence_fails_with_persisted_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    with pytest.raises(pytest.fail.Exception, match="install-before-identity precondition failed"):
        m.test_snowflake_identity("rocky9-amd64", "rhel")
    assert (tmp_path / "identity-evidence.json").exists()


def test_identity_and_package_version_steps_by_mode(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    assert _outcome(m.test_snowflake_identity, "rocky9-amd64", "rhel") == ("skipped", "integration mode only")
    vp = _Spy((True, "rhel", "2.6.0-1.el9", "ok"))
    monkeypatch.setattr(m.package_management, "verify_package_version", vp)
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    m.test_component_package_version("rocky9-amd64", "rhel")     # standalone: the configured version
    assert vp.calls[0][0][1:] == ("pgedge-snowflake_17", "2.6.0")
    m = load(monkeypatch, _RPM)
    monkeypatch.setattr(m.package_management, "verify_package_version", vp)
    outcome, msg = _outcome(m.test_component_package_version, "rocky9-amd64", "rhel")
    assert outcome == "skipped" and "test_snowflake_identity" in msg and len(vp.calls) == 1


# --------------------------------------------------------------------------- binding after CREATE EXTENSION
def _server(owner="pgedge-snowflake_17", version="170011", ext="9.8.7", libdir="/usr/pgsql-17/lib"):
    def respond(cmd):
        if cmd[0] == "psql":
            sql = cmd[-1]
            if "PKGLIBDIR" in sql:
                return 0, libdir + "\n"
            if "server_version_num" in sql:
                return 0, version + "\n"
            if "extversion" in sql and "'snowflake'" in sql:
                return 0, ext + "\n"
        if cmd[:2] == ["rpm", "-qf"]:
            return (0, owner + "\n") if owner else (1, "file %s is not owned by any package" % cmd[-1])
        if cmd[:2] == ["dpkg-query", "-S"]:
            return (0, "%s: %s\n" % (owner, cmd[2])) if owner else (1, "no path found matching pattern")
        return 1, "unexpected %r" % (cmd,)
    return respond


def test_binding_passes_for_the_requested_library_major_and_extension_version(monkeypatch):
    m = load(monkeypatch, _RPM)
    c = _Container(_server())
    monkeypatch.setattr(m, "client", _Client(c))
    m.test_snowflake_requested_extension("rocky9-amd64", "rhel")
    assert ["rpm", "-qf", "--queryformat", "%{NAME}\\n", "/usr/pgsql-17/lib/snowflake.so"] in c.cmds
    assert all(cmd[cmd.index("-p") + 1] == "5432" for cmd in c.cmds if cmd[0] == "psql")
    m = load(monkeypatch, _DEB)
    monkeypatch.setattr(m, "client", _Client(_Container(_server(owner="pgedge-postgresql-17-snowflake",
                                                                libdir="/usr/lib/postgresql/17/lib"))))
    m.test_snowflake_requested_extension("debian13-amd64", "deb")


@pytest.mark.parametrize("over,msg", [
    ({"owner": "pgedge-snowflake_16"}, "not the requested 'pgedge-snowflake_17'"),
    ({"owner": "pgedge-snowflake_17-llvmjit"}, "not the requested"),
    ({"owner": ""}, "not the requested"),
    ({"version": "160004"}, "runs PostgreSQL 160004, not the requested PG17"),
    ({"ext": "9.8"}, "extension version is '9.8'"),
    ({"ext": "9.8.70"}, "extension version is '9.8.70'"),
    ({"ext": "9.8.7~rc1"}, "extension version is '9.8.7~rc1'"),
    ({"ext": "9.8.7-rc.1"}, "extension version is '9.8.7-rc.1'"),
    ({"ext": "9.8.7-test1"}, "extension version is '9.8.7-test1'"),
    ({"ext": "2.6.0"}, "extension version is '2.6.0'"),          # the configured release, not the request
    ({"ext": ""}, "extension version is ''"),                    # extension not created
], ids=["owner-other-pg", "owner-llvmjit", "unowned", "pg-major", "ext-prefix", "ext-longer", "ext-tilde-rc",
        "ext-dash-rc", "ext-test", "ext-config", "ext-missing"])
def test_binding_rejects_a_wrong_library_major_or_extension_version(monkeypatch, over, msg):
    m = load(monkeypatch, _RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_server(**over))))
    with pytest.raises(AssertionError, match=msg.replace(".", r"\.").replace("~", r"\~")):
        m.test_snowflake_requested_extension("rocky9-amd64", "rhel")


def test_binding_is_integration_only(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    assert _outcome(m.test_snowflake_requested_extension, "rocky9-amd64", "rhel") == (
        "skipped", "integration mode only")


# --------------------------------------------------------------------------- missing references
_NEEDS_CONTAINER = ["test_configure_repository", "test_verify_binaries_stripped",
                    "test_verify_sbom", "test_verify_bundled_files", "test_init_cluster", "test_start_server",
                    "test_check_connection", "test_snowflake_ids_carry_the_configured_node",
                    "test_snowflake_id_time_is_bracketed", "test_snowflake_node_setting_is_enforced",
                    "test_stop_server", "test_snowflake_uninstall", "test_pgedge_cleanup"]


@pytest.mark.parametrize("name", _NEEDS_CONTAINER + ["test_component_install", "test_snowflake_identity",
                                                     "test_snowflake_requested_extension"])
def test_a_missing_container_fails_in_integration_mode(monkeypatch, name):
    m = load(monkeypatch, _RPM)
    monkeypatch.setattr(m, "client", _Client(None))
    outcome, msg = _outcome(getattr(m, name), "rocky9-amd64", "rhel")
    assert outcome == "failed" and "rocky9-amd64 not found" in msg


@pytest.mark.parametrize("name", _NEEDS_CONTAINER + ["test_component_install", "test_component_package_version"])
def test_a_missing_container_still_skips_standalone(monkeypatch, name):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(None))
    outcome, msg = _outcome(getattr(m, name), "rocky9-amd64", "rhel")
    assert outcome == "skipped" and "rocky9-amd64 not found" in msg


_NO_REFERENCE = Exception("No expected file found for snowflake at /x/expected-output/rpm (checked: /x/a)")


def _bundled_outcome(monkeypatch, env, verify):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    monkeypatch.setattr(m.file_management, "verify_bundled_files", verify)
    return _outcome(m.test_verify_bundled_files, "rocky9-amd64", "rhel")


def _raises(exc):
    def verify(**kwargs):
        raise exc
    return verify


def test_a_missing_bundled_file_reference_fails_in_integration_mode_and_skips_standalone(monkeypatch):
    outcome, msg = _bundled_outcome(monkeypatch, _RPM, _raises(_NO_REFERENCE))
    assert outcome == "failed" and "No expected file found for snowflake" in msg
    outcome, msg = _bundled_outcome(monkeypatch, _STANDALONE, _raises(_NO_REFERENCE))
    assert outcome == "skipped" and "No expected file found for snowflake" in msg


@pytest.mark.parametrize("env,package", [(_RPM, "pgedge-snowflake_17"), (_STANDALONE, "pgedge-snowflake_17")],
                         ids=["integration", "standalone"])
def test_bundled_file_outcomes_are_unchanged_when_the_reference_exists(monkeypatch, env, package):
    seen = []
    assert _bundled_outcome(monkeypatch, env, lambda **k: seen.append(k) or (True, None, "match")) == ("passed", "")
    assert seen[0]["package_name"] == seen[0]["component"] == package
    outcome, msg = _bundled_outcome(monkeypatch, env, lambda **k: (False, {"extra_files": ["/usr/x"]}, "differ"))
    assert outcome == "failed" and "Extra files" in msg and "/usr/x" in msg
    outcome, msg = _bundled_outcome(monkeypatch, env, _raises(RuntimeError("rpm -ql failed")))
    assert outcome == "failed" and "rpm -ql failed" in msg


# What the published snowflake 2.6.0-1 PG17 packages own, entry for entry, as their own package
# listings show (el9 x86_64 RPM: rpm -qp; noble amd64 DEB: dpkg-deb -c, "./" printed as "/." the
# way dpkg -L prints it). Both families ship the license: the RPM under /usr/share/licenses/,
# the DEB under /usr/share/doc/<package>/ (current snowflake packaging does this with %license
# and debian/docs; the v2.6.0 tag predates that in-repo packaging).
_SF_SQL = ["snowflake--1.0--1.1.sql", "snowflake--1.0.sql", "snowflake--1.1--1.2.sql", "snowflake--1.1.sql",
           "snowflake--1.2--2.0.sql", "snowflake--1.2.sql", "snowflake--2.0--2.2.sql", "snowflake--2.0.sql",
           "snowflake--2.2--2.3.sql", "snowflake--2.2.sql", "snowflake--2.3--2.4.sql", "snowflake--2.3.sql",
           "snowflake--2.4--2.5.0.sql", "snowflake--2.5.0--2.6.0.sql", "snowflake.control"]
_SF_DEB_DOC = "/usr/share/doc/pgedge-postgresql-17-snowflake"
_SF_PUBLISHED = {
    "rhel": ["/usr/lib/.build-id", "/usr/lib/.build-id/cf", "/usr/lib/.build-id/cf/79326c14b711fea12fcd4891f9a3b9917067bc",
             "/usr/pgsql-17/lib/snowflake.so", "/usr/pgsql-17/sbom/snowflake-sbom.json",
             "/usr/pgsql-17/sbom/snowflake-sbom.json.asc"]
            + ["/usr/pgsql-17/share/extension/" + f for f in _SF_SQL]
            + ["/usr/share/doc/pgedge-snowflake_17", "/usr/share/doc/pgedge-snowflake_17/README.md",
               "/usr/share/licenses/pgedge-snowflake_17", "/usr/share/licenses/pgedge-snowflake_17/LICENSE.md"],
    "deb": ["/.", "/usr", "/usr/lib", "/usr/lib/postgresql", "/usr/lib/postgresql/17", "/usr/lib/postgresql/17/lib",
            "/usr/lib/postgresql/17/lib/bitcode", "/usr/lib/postgresql/17/lib/bitcode/snowflake",
            "/usr/lib/postgresql/17/lib/bitcode/snowflake/snowflake.bc",
            "/usr/lib/postgresql/17/lib/bitcode/snowflake.index.bc", "/usr/lib/postgresql/17/lib/snowflake.so",
            "/usr/lib/postgresql/17/sbom", "/usr/lib/postgresql/17/sbom/snowflake-sbom.json",
            "/usr/lib/postgresql/17/sbom/snowflake-sbom.json.asc", "/usr/share", "/usr/share/doc", _SF_DEB_DOC,
            _SF_DEB_DOC + "/LICENSE.md", _SF_DEB_DOC + "/README.md", _SF_DEB_DOC + "/changelog.Debian.gz",
            "/usr/share/postgresql", "/usr/share/postgresql/17", "/usr/share/postgresql/17/extension"]
           + ["/usr/share/postgresql/17/extension/" + f for f in _SF_SQL],
}


@pytest.mark.parametrize("env,ctype,alias,listing,license_path", [
    (_RPM, "rhel", "rocky9-amd64", "rpm -ql pgedge-snowflake_17", "/usr/share/licenses/pgedge-snowflake_17/LICENSE.md"),
    (_DEB, "deb", "debian13-amd64", "dpkg -L pgedge-postgresql-17-snowflake", _SF_DEB_DOC + "/LICENSE.md"),
], ids=["rpm", "deb"])
def test_bundled_file_references_match_the_published_packages_and_require_the_license(
        monkeypatch, env, ctype, alias, listing, license_path):
    m = load(monkeypatch, env)
    files = list(_SF_PUBLISHED[ctype])

    def respond(cmd):
        assert cmd == listing, cmd
        return 0, "\n".join(files) + "\n"
    monkeypatch.setattr(m, "client", _Client(_Container(respond)))
    assert _outcome(m.test_verify_bundled_files, alias, ctype) == ("passed", "")
    # ... and a package that stops shipping its license is caught (not reported as a pass).
    files.remove(license_path)
    outcome, msg = _outcome(m.test_verify_bundled_files, alias, ctype)
    assert outcome == "failed" and "Missing files (1)" in msg and "/license.md" in msg, msg


@pytest.mark.parametrize("env", [_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_a_missing_sbom_fails(monkeypatch, env):
    m = load(monkeypatch, env)
    def respond(cmd):
        if "sq verify" in cmd:
            return 1, "Error: Failed to open snowflake-sbom.json: No such file or directory"
        return 0, ""
    monkeypatch.setattr(m, "client", _Client(_Container(respond)))
    outcome, msg = _outcome(m.test_verify_sbom, "rocky9-amd64", "rhel")
    assert outcome == "failed" and "SBOM verification failed" in msg


# --------------------------------------------------------------------------- stripped library
@pytest.mark.parametrize("env", [_RPM, _STANDALONE], ids=["integration", "standalone"])
@pytest.mark.parametrize("ec,out,ok", [
    (0, "ELF 64-bit LSB shared object, x86-64, version 1 (SYSV), dynamically linked, stripped", True),
    (0, "ELF 64-bit LSB shared object, x86-64, dynamically linked, BuildID[sha1]=ab, not stripped", False),
    (0, "ELF 64-bit LSB shared object, x86-64, dynamically linked", False),
    (0, "ASCII text", False),
    (1, "cannot open `/usr/pgsql-17/lib/snowflake.so' (No such file or directory)", False),
], ids=["stripped", "not-stripped", "no-strip-info", "not-elf", "missing"])
def test_the_snowflake_library_must_be_a_stripped_elf_file(monkeypatch, env, ec, out, ok):
    m = load(monkeypatch, env)
    c = _Container(lambda cmd: (ec, out))
    monkeypatch.setattr(m, "client", _Client(c))
    outcome, _msg = _outcome(m.test_verify_binaries_stripped, "rocky9-amd64", "rhel")
    assert outcome == ("passed" if ok else "failed")
    assert c.cmds == [["file", "-b", "-L", "/usr/pgsql-17/lib/snowflake.so"]]


# --------------------------------------------------------------------------- snowflake.node
@pytest.mark.parametrize("value,ok", [("1", True), ("512", True), ("1023", True), ("0", False), ("1024", False),
                                      ("-1", False), ("01", False), ("1.0", False), ("abc", False), ("", False),
                                      (None, False)])
def test_snowflake_node_must_be_usable_by_the_product(monkeypatch, value, ok):
    m = load(monkeypatch, _STANDALONE)
    assert (m.snowflake_node_problem(value) is None) is ok


def test_init_cluster_writes_the_configured_node(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, SNOWFLAKE_NODE="7"))
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    init = _Spy((True, "", "initialized"))
    monkeypatch.setattr(m.pg_server_management, "init_cluster", init)
    m.test_init_cluster("rocky9-amd64", "rhel")
    gucs = init.calls[0][0][4]
    assert gucs["snowflake.node"] == "7" and gucs["shared_preload_libraries"] == "'snowflake'"


@pytest.mark.parametrize("value", ["0", "1024", "x"])
def test_init_cluster_refuses_an_unusable_node(monkeypatch, value):
    m = load(monkeypatch, dict(_RPM, SNOWFLAKE_NODE=value))
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    init = _Spy((True, "", "initialized"))
    monkeypatch.setattr(m.pg_server_management, "init_cluster", init)
    with pytest.raises(AssertionError, match="not a usable snowflake.node"):
        m.test_init_cluster("rocky9-amd64", "rhel")
    assert init.calls == []


@pytest.mark.parametrize("shown,ok", [("1", True), ("10", False), ("11", False), ("0", False)])
def test_the_node_check_requires_exactly_the_configured_node(monkeypatch, shown, ok):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(_psql(lambda stmts: (0, shown + "\n") if stmts == ["SHOW snowflake.node"] else (1, "?")))
    monkeypatch.setattr(m, "client", _Client(c))
    assert _outcome(m.test_snowflake_node_parameter, "rocky9-amd64", "rhel")[0] == ("passed" if ok else "failed")


# --------------------------------------------------------------------------- format()
def test_the_format_sample_id_has_the_documented_fields(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    assert m.decode_snowflake(m.format_sample_id()) == (32465339845, 513, 7)


@pytest.mark.parametrize("reply,ok", [
    ((0, "513|7|t\n"), True),
    ((0, "7|513|t\n"), False),                     # node and count swapped
    ((0, "513|7|f\n"), False),                     # wrong time
    ((0, "513|7|t\n513|7|t\n"), False),           # more than one row
    ((1, "ERROR:  function snowflake.format(bigint) does not exist\n"), False),
], ids=["exact", "swapped", "wrong-ts", "two-rows", "error"])
def test_format_must_report_each_field_exactly(monkeypatch, reply, ok):
    m = load(monkeypatch, _STANDALONE)
    seen = []
    c = _Container(_psql(lambda stmts: (seen.append(stmts), reply)[1]))
    monkeypatch.setattr(m, "client", _Client(c))
    assert _outcome(m.test_snowflake_format_function, "rocky9-amd64", "rhel")[0] == ("passed" if ok else "failed")
    assert len(seen) == 1 and "snowflake.format(%d)" % m.format_sample_id() in seen[0][0]


# --------------------------------------------------------------------------- id burst
_MS = 1_000_000          # a synthetic millisecond since the snowflake epoch


def _burst_rows(ids, node=None, counts=None):
    out = []
    for i, x in enumerate(ids):
        _ms, n, c = (x >> 22, (x >> 12) & 0x3FF, x & 0xFFF)
        out.append("%d|%d|%d|%d" % (i + 1, x, n if node is None else node, c if counts is None else counts[i]))
    return out


def _good_burst(size, node=1):
    # clock ticks every 300 ids; within a millisecond the count increases
    return [sf(_MS + i // 300, node, i % 300) for i in range(size)]


def test_burst_rules_accept_increasing_ids_on_the_configured_node(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    ids = _good_burst(1000) + [sf(_MS + 9, 1, 4095), sf(_MS + 10, 1, 0)]   # count wrap -> next msec
    assert m.burst_problems(_burst_rows(ids), 1, len(ids)) == []


@pytest.mark.parametrize("mutate,msg", [
    (lambda ids: ids.__setitem__(5, ids[4]), "is not greater than the previous id"),
    (lambda ids: ids.__setitem__(5, ids[4] - 1), "is not greater than the previous id"),
    (lambda ids: ids.__setitem__(5, sf(_MS, 2, 5)), "expected the configured node 1"),
], ids=["repeat", "decrease", "other-node"])
def test_burst_rules_reject_non_increasing_or_foreign_ids(monkeypatch, mutate, msg):
    m = load(monkeypatch, _STANDALONE)
    ids = _good_burst(10)
    mutate(ids)
    assert any(msg in p for p in m.burst_problems(_burst_rows(ids), 1, 10))


def test_burst_rules_reject_get_node_get_count_and_row_disagreements(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    ids = _good_burst(10)
    assert "get_node()=3" in m.burst_problems(_burst_rows(ids, node=3), 1, 10)[0]
    assert "get_count()=7" in m.burst_problems(_burst_rows(ids, counts=[7] * 10), 1, 10)[1]
    assert "expected 10 ids in call order" in m.burst_problems(_burst_rows(ids)[:9], 1, 10)[0]
    assert "unexpected nextval burst output" in m.burst_problems(["ERROR:  boom"], 1, 10)[0]


def _burst_session(ids):
    def session(stmts):
        if stmts[0] == "CREATE SEQUENCE pep_sf_burst_seq" and "ORDER BY g.n" in stmts[1]:
            return 0, "\n".join(_burst_rows(ids)) + "\n"
        return 1, "unexpected %r" % (stmts,)
    return session


def test_the_burst_test_drives_the_rules(monkeypatch):
    m = load(monkeypatch, _RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_psql(_burst_session(_good_burst(m.BURST_SIZE))))))
    assert _outcome(m.test_snowflake_ids_carry_the_configured_node, "rocky9-amd64", "rhel") == ("passed", "")
    ids = _good_burst(m.BURST_SIZE)
    ids[-1] = ids[0]
    monkeypatch.setattr(m, "client", _Client(_Container(_psql(_burst_session(ids)))))
    outcome, msg = _outcome(m.test_snowflake_ids_carry_the_configured_node, "rocky9-amd64", "rhel")
    assert outcome == "failed" and "is not greater than the previous id" in msg


# --------------------------------------------------------------------------- epoch bracket
_OFFSET_MS = 1672531200 * 1000


def test_epoch_rules_accept_an_id_inside_the_bracket_inclusive(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    x = sf(_MS, 1, 0)
    unix = _MS + _OFFSET_MS
    assert m.epoch_problems(unix - 2, x, unix + 3, "%d.000" % unix, 0, 1) == []   # get_epoch() * 1000
    assert m.epoch_problems(unix, x, unix, str(unix), 0, 1) == []                # both bounds inclusive


@pytest.mark.parametrize("args,msg", [
    (dict(before=1, after=5), "outside the clock readings"),                 # id before the first reading
    (dict(before=-5, after=-1), "outside the clock readings"),               # id after the second reading
    (dict(epoch=1), "but its msec bits give"),                               # get_epoch disagrees
    (dict(epoch="abc"), "is not a number"),
    (dict(count=3), "must have count 0"),                                    # get_count disagrees
    (dict(sf_count=1), "must have count 0"),                                 # id is not a first id
    (dict(node=2), "carries node 1, expected 2"),
], ids=["early", "late", "epoch", "epoch-garbled", "get-count", "count-bits", "node"])
def test_epoch_rules_reject_each_disagreement(monkeypatch, args, msg):
    m = load(monkeypatch, _STANDALONE)
    unix = _MS + _OFFSET_MS
    x = sf(_MS, 1, args.get("sf_count", 0))
    epoch = args.get("epoch", 0)
    problems = m.epoch_problems(unix + args.get("before", -1), x, unix + args.get("after", 1),
                                epoch if isinstance(epoch, str) else str(unix + epoch), args.get("count", 0),
                                args.get("node", 1))
    assert any(msg in p for p in problems), problems


def _epoch_session(before, sf_id, after, epoch_ms, count="0"):
    def session(stmts):
        if stmts[0] == "CREATE SEQUENCE pep_sf_epoch_seq" and len(stmts) == 4:
            assert stmts[1] == stmts[3] and "clock_timestamp()" in stmts[1] and "nextval" in stmts[2]
            return 0, "%d\n%d\n%d\n" % (before, sf_id, after)
        if stmts == ["SELECT snowflake.get_epoch(%d) * 1000, snowflake.get_count(%d)" % (sf_id, sf_id)]:
            return 0, "%s|%s\n" % (epoch_ms, count)
        return 1, "unexpected %r" % (stmts,)
    return session


def test_the_bracket_test_drives_the_rules(monkeypatch):
    m = load(monkeypatch, _RPM)
    unix, x = _MS + _OFFSET_MS, sf(_MS, 1, 0)
    monkeypatch.setattr(m, "client", _Client(_Container(_psql(_epoch_session(unix - 1, x, unix + 1, "%d.000" % unix)))))
    assert _outcome(m.test_snowflake_id_time_is_bracketed, "rocky9-amd64", "rhel") == ("passed", "")
    monkeypatch.setattr(m, "client", _Client(_Container(_psql(_epoch_session(unix + 1, x, unix + 2, "%d.000" % unix)))))
    outcome, msg = _outcome(m.test_snowflake_id_time_is_bracketed, "rocky9-amd64", "rhel")
    assert outcome == "failed" and "outside the clock readings" in msg


# --------------------------------------------------------------------------- node enforcement
def _node_session(unset=(3, "ERROR:  value for snowflake.node is not set"), issued="t",
                  out_of_range=(3, 'ERROR:  1024 is outside the valid range for parameter "snowflake.node" (0 .. 1023)'),
                  other_node="2"):
    def session(stmts):
        if stmts == ["CREATE SEQUENCE pep_sf_node_seq", "SET snowflake.node = 0",
                     "SELECT snowflake.nextval('pep_sf_node_seq'::regclass)"]:
            return unset
        if len(stmts) == 1 and "pg_sequences" in stmts[0] and "pep_sf_node_seq" in stmts[0]:
            return 0, issued + "\n"
        if stmts == ["SET snowflake.node = 1024"]:
            return out_of_range
        if stmts == ["SET snowflake.node = 2",
                     "SELECT snowflake.get_node(snowflake.nextval('pep_sf_node_seq'::regclass))"]:
            return 0, other_node + "\n"
        return 1, "unexpected %r" % (stmts,)
    return session


@pytest.mark.parametrize("over,ok", [
    ({}, True),
    ({"unset": (0, "123\n")}, False),                                          # node 0 issued an id
    ({"unset": (1, "ERROR:  permission denied for sequence pep_sf_node_seq")}, False),
    ({"issued": "f"}, False),                                                  # a failed call advanced it
    ({"out_of_range": (0, "")}, False),                                        # GUC placeholder accepted 1024
    ({"out_of_range": (1, "ERROR:  syntax error")}, False),
    ({"other_node": "1"}, False),                                              # node not taken from the GUC
], ids=["enforced", "unset-accepted", "other-error", "id-issued", "range-accepted", "range-other-error",
        "guc-ignored"])
def test_node_enforcement_requires_the_products_error_and_the_guc(monkeypatch, over, ok):
    m = load(monkeypatch, _RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_psql(_node_session(**over)))))
    assert _outcome(m.test_snowflake_node_setting_is_enforced, "rocky9-amd64", "rhel")[0] == (
        "passed" if ok else "failed")


# --------------------------------------------------------------------------- multiple sequences
def _sequences_session(emp=(30, 30, 3, 3), prod=(50, 50, 2, 2)):
    def session(stmts):
        if len(stmts) == 1 and "FROM employees" in stmts[0] and "employee_id_seq" in stmts[0]:
            return 0, "%d|%d|%d|%d\n" % emp
        if len(stmts) == 1 and "FROM products" in stmts[0] and "products_id_seq" in stmts[0]:
            return 0, "%d|%d|%d|%d\n" % prod
        return 1, "unexpected %r" % (stmts,)
    return session


@pytest.mark.parametrize("over,ok,msg", [
    ({}, True, ""),
    ({"prod": (50, 49, 2, 2)}, False, "products does not draw its ids from products_id_seq"),
    ({"emp": (31, 30, 3, 3)}, False, "employees does not draw its ids from employee_id_seq"),
    ({"prod": (50, 50, 2, 1)}, False, "only 1 of 2 products ids carry node 1"),
    ({"emp": (30, 30, 2, 2)}, False, "employees has 2 rows, expected 3"),
], ids=["own-sequences", "products-other-seq", "employees-other-seq", "products-node", "employees-rows"])
def test_each_table_draws_its_ids_from_its_own_sequence(monkeypatch, over, ok, msg):
    m = load(monkeypatch, _STANDALONE)
    query = lambda cmd: (0, "Laptop | Mouse")
    monkeypatch.setattr(m, "client", _Client(_Container(_psql(_sequences_session(**over), query))))
    outcome, text = _outcome(m.test_snowflake_multiple_sequences, "rocky9-amd64", "rhel")
    assert outcome == ("passed" if ok else "failed") and msg in text


# --------------------------------------------------------------------------- sequence conversion
_CONVERTED = "bigint|snowflake.nextval('my_simple_seq'::regclass)|bigint|9223372036854775807"


def _conversion(changes="2", catalog=_CONVERTED, original=(1, 2, 3), new_node=1):
    new = [sf(_MS, new_node, 0), sf(_MS, new_node, 1), sf(_MS + 1, new_node, 0)]
    names = ["John Doe", "Jane Smith", "Bob Wilson", "Alice Johnson", "Charlie Brown", "Diana Prince"]
    ids = list(original) + new

    def session(stmts):
        if stmts == ["SET client_min_messages = warning",
                     "SELECT snowflake.convert_sequence_to_snowflake('my_simple_seq'::regclass)"]:
            return 0, changes + "\n"
        if stmts[0] == "SET search_path = public" and "pg_sequences" in stmts[1]:
            return 0, catalog + "\n"
        if stmts == ["SELECT name, id FROM users ORDER BY id"]:
            return 0, "".join("%s|%d\n" % (n, i) for n, i in sorted(zip(names, ids), key=lambda r: r[1]))
        return 1, "unexpected %r" % (stmts,)

    def query(cmd):
        if "SELECT * FROM users" in cmd:
            return 0, "".join(" %d | %s | x\n" % (i, n) for n, i in zip(names, ids))
        return 0, ""
    return _psql(session, query)


@pytest.mark.parametrize("over,ok,msg", [
    ({}, True, ""),
    ({"changes": "1"}, False, "expected 2 changes"),
    ({"catalog": _CONVERTED.replace("9223372036854775807", "4")}, False, "my_simple_seq (type|max_value)"),
    ({"catalog": _CONVERTED.replace("bigint|snowflake", "integer|snowflake")}, False, "users.id (type|default)"),
    ({"catalog": _CONVERTED.replace("snowflake.nextval", "nextval")}, False, "users.id (type|default)"),
    ({"new_node": 2}, False, "do not all carry node 1"),
], ids=["converted", "changes", "pre-2.5-maxvalue", "column-type", "default", "new-ids-node"])
def test_conversion_does_exactly_what_the_product_says(monkeypatch, over, ok, msg):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_conversion(**over))))
    outcome, text = _outcome(m.test_snowflake_convert_existing_sequence, "rocky9-amd64", "rhel")
    assert outcome == ("passed" if ok else "failed") and msg in text
