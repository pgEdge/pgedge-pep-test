"""Docker-free tests that exercise the REAL functions in component-test/test_pep_safesession.py.

The module is loaded by path with docker.from_env and load_dotenv neutralized and with exactly the
environment of the case under test (platform filter, channel and integration request are decided
at import time), then driven with a fake docker client/container and spied package helpers, as in
test_pep_spock_wiring.py. The enforcement and binding checks run against _Cluster, a small model of
a SafeSession-enabled server (roles listed in pgedge_safesession.roles, membership, superuser
exemption, the reload, the table and its relations) whose answers can be replaced per statement.

Covers: one instance of each shared helper; standalone behaviour preserved; package, family and
channel taken from the request; wrong package / PG major / family rejected at load and by the
backstop; the server installed before the pinned package; a failed pin writing no install
evidence; any other safesession package (-llvmjit, another PG major) failing the install; identity;
the binding (library owner, server major, preload, the release workflow's series rule for the
extension version); enforcement (restricted role and its member rejected with SafeSession's own
error, an identically privileged control role and a superuser member allowed, exact final data,
cleanup); missing references failing in integration mode and skipping standalone; the strict
stripped check; and the bundled-file references against the files the packaging installs.
"""
import importlib.util
import itertools
import json
import os
import re
import sys
import types

import docker
import dotenv
import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "component-test", "test_pep_safesession.py")
_counter = itertools.count()

# Synthetic release 1.1 (not today's 1.0), so nothing here can pass by matching configuration.
_RPM = {"PEP_INTEGRATION_MODE": "1", "PEP_COMPONENT": "safesession", "PEP_PACKAGE_NAME": "pgedge-safesession_17",
        "PEP_CHANNEL": "staging", "PEP_EXPECTED_VERSION": "1.1", "PEP_FAMILY": "rpm",
        "PEP_ARCH_FILTER": "amd64", "PEP_CONTAINER_ALIAS": "rocky9-amd64", "PG_MAJOR_VERSION": "17",
        "PEP_EXPECTED_RPM": "1.1-1.el9", "PEP_EXPECTED_BUILDNUM": "1",
        "REPO": "staging", "CONTAINERS": "rocky9-amd64", "DEB_CONTAINERS": "debian13-amd64"}
_DEB = dict(_RPM, PEP_PACKAGE_NAME="pgedge-postgresql-17-safesession", PEP_FAMILY="deb",
            PEP_CONTAINER_ALIAS="debian13-amd64", PEP_EXPECTED_RPM=None, PEP_EXPECTED_DEB="1.1-1.trixie")
_STANDALONE = {"PG_MAJOR_VERSION": "17", "CONTAINERS": "rocky9-amd64", "DEB_CONTAINERS": "debian13-amd64"}

# Every variable the module reads, so an inherited value (or a .env loaded by another test) cannot
# leak into a case.
_MODULE_ENV = {"CONTAINERS", "DEB_CONTAINERS", "PLATFORM_FILTER", "REPO", "UPGRADE_REPO", "UPGRADE",
               "SKIP_CLEANUP", "PG_PORT", "PG_DATA_DIR", "PG_MAJOR_VERSION", "COMPONENT_BINARY",
               "COMPONENT_BINARY_VERSION", "PG_USER", "DEB_PG_USER", "PG_BIN_PATH", "RHEL_PG_PATH",
               "DEB_PG_BIN_PATH", "DEB_PG_PATH", "DEB_PG_SHARE_PATH", "CHECK_EXTENSIONS"}


def load(monkeypatch, env):
    """Load a fresh copy of the safesession component test under exactly ``env`` (None values unset)."""
    for k in list(os.environ):
        if k in _MODULE_ENV or k.startswith("PEP_") or "SAFESESSION" in k:
            monkeypatch.delenv(k)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(docker, "from_env", lambda *a, **k: None)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    name = "safesession_under_test_%d" % next(_counter)
    spec = importlib.util.spec_from_file_location(name, _PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(sleep=lambda s: None))
    return mod


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
                    raise docker.errors.NotFound("no such container: %s" % name)
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
    """exec_run responder for the package-manager listing used by the installed-set check."""
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qa"]:
            return 0, "\n".join(names) + "\n"
        if cmd[0] == "dpkg-query" and cmd[1] == "-W":
            return 0, "".join("%s install ok installed\n" % n for n in names)
        return 0, ""
    return respond


def _wire(monkeypatch, tmp_path, m, container):
    monkeypatch.setattr(m, "client", _Client(container))
    for k, f in (("PEP_INSTALL_OUT", "install-evidence.json"), ("PEP_IDENTITY_OUT", "identity-evidence.json"),
                 ("PEP_OBSERVED_OUT", "observed-identity.json"), ("PEP_PREREQ_OUT", "prerequisite-versions.json")):
        monkeypatch.setenv(k, str(tmp_path / f))
    monkeypatch.setenv("PEP_RUN_TOKEN", "run-A")


def _outcome(fn, *args):
    """("passed"|"skipped"|"failed"|"assert", message) of one real test function."""
    try:
        fn(*args)
    except pytest.skip.Exception as e:
        return "skipped", str(e)
    except pytest.fail.Exception as e:
        return "failed", str(e)
    except AssertionError as e:
        return "assert", str(e)
    return "passed", ""


# --------------------------------------------------------------------------- shared instances
def test_component_test_uses_one_instance_of_each_shared_helper(monkeypatch):
    m = load(monkeypatch, _RPM)
    assert m.pep_integration_mode is m.pep_extension_checks.pep_integration_mode
    assert m.pep_request_env is m.pep_integration_mode.pep_request_env
    assert m._pr is m.pep_request_env._pr is m.pep_extension_checks._pr is sys.modules["pep_request"]
    assert m._pp is m._pr._pp is m.pep_extension_checks._pp is sys.modules["pep_package_policy"]
    assert m.package_management is m.pep_extension_checks.package_management
    assert m.package_management is m.pep_integration_mode.package_management


def test_policy_registers_safesession_as_pg_coupled_with_an_uncertified_rpm_jit_companion(monkeypatch):
    m = load(monkeypatch, _RPM)
    entry = m._pr.COMPONENT_POLICY["safesession"]
    assert m._pp.is_coupled(entry) and "series_by_version_major" not in entry
    assert m._pp.expected_names(entry, "rpm", "17", "1.1") == ("pgedge-safesession_17",)
    assert m._pp.expected_names(entry, "deb", "17", "1.1") == ("pgedge-postgresql-17-safesession",)
    assert m._pp.classify(entry, "pgedge-safesession_17-llvmjit", "rpm", "17", "1.1") == m._pp.R_UNCERTIFIED
    assert "deb" not in entry["uncertified_runtime_package_templates"]


# --------------------------------------------------------------------------- configuration by mode
def test_standalone_keeps_the_configured_values(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, PLATFORM_FILTER="deb", REPO="daily", SAFESESSION_PACKAGE="cfg-rpm",
                               SAFESESSION_EXTENSIONS="pgedge_safesession,other", SAFESESSION_STRIPPED_LIB="x.so"))
    assert m.INTEGRATION_REQUEST is None and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == [("debian13-amd64", "deb")]
    assert m.repo == "daily"
    assert m.base_extensions == ["pgedge_safesession", "other"] and m.safesession_stripped_lib == "x.so"
    assert m.get_container_config("rhel")["safesession_package"] == "cfg-rpm"
    assert m.get_container_config("rhel")["license_path"] == "/usr/share/licenses/cfg-rpm/LICENCE.md"
    assert m.get_container_config("deb")["safesession_package"] == "pgedge-postgresql-17-safesession"
    m = load(monkeypatch, _STANDALONE)
    assert m.all_containers == [("rocky9-amd64", "rhel"), ("debian13-amd64", "deb")] and m.repo == "release"


@pytest.mark.parametrize("env,ctype,containers", [
    (dict(_RPM, PLATFORM_FILTER="deb"), "rhel", [("rocky9-amd64", "rhel")]),
    (dict(_DEB, PLATFORM_FILTER="rpm"), "deb", [("debian13-amd64", "deb")]),
], ids=["rpm-request-deb-filter", "deb-request-rpm-filter"])
def test_integration_takes_package_family_and_channel_from_the_request(monkeypatch, env, ctype, containers):
    m = load(monkeypatch, dict(env, REPO="release", SAFESESSION_PACKAGE="cfg", DEB_SAFESESSION_PACKAGE="cfg",
                               SAFESESSION_EXTENSIONS="other", SAFESESSION_STRIPPED_LIB="other.so",
                               SAFESESSION_SBOM="other.json"))
    assert m.INTEGRATION_PROBLEM is None
    assert m.all_containers == containers
    assert m.repo == "staging"
    cfg, pkg = m.get_container_config(ctype), env["PEP_PACKAGE_NAME"]
    assert cfg["safesession_package"] == pkg
    assert "/%s/" % pkg in cfg["license_path"] and cfg["readme_path"] == "/usr/share/doc/%s/README.md" % pkg
    assert m.base_extensions == ["pgedge_safesession"]
    assert (m.safesession_stripped_lib, m.safesession_sbom) == ("pgedge_safesession.so", "pgedge-safesession-sbom.json")


@pytest.mark.parametrize("over,reason", [
    ({"PEP_PACKAGE_NAME": "pgedge-spock50_17"}, "package_not_allowed"),
    ({"PEP_PACKAGE_NAME": "pgedge-safesession_16"}, "pg_major_mismatch"),
    ({"PEP_PACKAGE_NAME": "pgedge-postgresql-17-safesession"}, "package_family_mismatch"),
    ({"PEP_FAMILY": "deb", "PEP_CONTAINER_ALIAS": "debian13-amd64", "PEP_EXPECTED_RPM": None,
      "PEP_EXPECTED_DEB": "1.1-1.trixie"}, "package_family_mismatch"),
    ({"PEP_PACKAGE_NAME": "pgedge-safesession_17-llvmjit"}, "runtime_not_certified"),
], ids=["wrong-package", "wrong-pg", "wrong-family-name", "wrong-family", "llvmjit"])
def test_a_request_for_the_wrong_package_pg_major_or_family_is_rejected_at_load(monkeypatch, over, reason):
    with pytest.raises(Exception, match=reason):
        load(monkeypatch, dict(_RPM, **over))


def test_backstop_names_each_problem(monkeypatch):
    m = load(monkeypatch, _RPM)
    req = dict(m.INTEGRATION_REQUEST)
    assert m._integration_problem(req, "17") is None
    assert "PG_MAJOR_VERSION=16" in m._integration_problem(req, "16")
    assert "not the PG-coupled safesession" in m._integration_problem(dict(req, component="spock"), "17")
    assert "not the PG-coupled safesession" in m._integration_problem(dict(req, component="rag"), "17")
    assert "pg_major_mismatch" in m._integration_problem(dict(req, package_name="pgedge-safesession_16"), "17")
    assert "package_family_mismatch" in m._integration_problem(dict(req, family="deb"), "17")
    assert "runtime_not_certified" in m._integration_problem(
        dict(req, package_name="pgedge-safesession_17-llvmjit"), "17")
    assert m._integration_problem(None, "17") is None


# --------------------------------------------------------------------------- install
def _install_spies(monkeypatch, m, order=None, server=(True, "rhel", "installed"), pinned=(True, "ok", "f" * 64)):
    ip, pin = _Spy(server, order, "server"), _Spy(pinned, order, "pinned")
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pin)
    monkeypatch.setattr(m.package_management, "query_installed_version",
                        lambda c, n: "1.1-1.el9" if "safesession" in n else "17.11-1PGEDGE.el9")
    return ip, pin


def test_server_first_then_the_pinned_request_and_versions_are_recorded(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-safesession_17", "pgedge-postgresql17-server")))
    order = []
    ip, pinned = _install_spies(monkeypatch, m, order)

    m.test_component_install("rocky9-amd64", "rhel")

    assert order == ["server", "pinned"]
    assert ip.calls[0][1]["package_name"] == ["pgedge-postgresql17-server"]
    assert ip.calls[0][1]["install_pg_server"] is False
    assert pinned.calls[0][0][1:] == ("pgedge-safesession_17", "1.1-1.el9")
    marker = json.loads((tmp_path / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "f" * 64
    assert json.loads((tmp_path / "prerequisite-versions.json").read_text()) == {
        "channel": "staging", "pg_major": "17", "requested_package": "pgedge-safesession_17",
        "installed_versions": {"pgedge-postgresql17-server": "17.11-1PGEDGE.el9", "pgedge-safesession_17": "1.1-1.el9"}}


def test_deb_installs_the_server_for_the_requested_major_then_the_pinned_request(monkeypatch, tmp_path):
    m = load(monkeypatch, _DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list("pgedge-postgresql-17-safesession", "pgedge-postgresql-17")))
    ip, pinned = _install_spies(monkeypatch, m, server=(True, "debian", "installed"))
    m.test_component_install("debian13-amd64", "deb")
    assert ip.calls[0][1]["package_name"] == ["pgedge-postgresql-17"]
    assert pinned.calls[0][0][1:] == ("pgedge-postgresql-17-safesession", "1.1-1.trixie")


def test_missing_server_is_a_prerequisite_failure_and_nothing_is_pinned(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    ip, pinned = _install_spies(monkeypatch, m, server=Exception("No match for argument: pgedge-postgresql17-server"))
    with pytest.raises(pytest.fail.Exception, match="PostgreSQL 17 prerequisite unavailable from the 'staging' channel"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_failed_pinned_install_fails_without_install_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    _install_spies(monkeypatch, m, pinned=(False, "checksum mismatch", None))
    with pytest.raises(AssertionError, match="pinned install of pgedge-safesession_17=1.1-1.el9 failed: checksum mismatch"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert not (tmp_path / "install-evidence.json").exists()


@pytest.mark.parametrize("env,ctype,present", [
    (_RPM, "rhel", ("pgedge-safesession_17", "pgedge-safesession_17-llvmjit")),   # uncertified JIT companion
    (_RPM, "rhel", ("pgedge-safesession_17", "pgedge-safesession_16")),           # another PG major
    (_RPM, "rhel", ("pgedge-safesession_16",)),                                   # the request replaced
    (_DEB, "deb", ("pgedge-postgresql-17-safesession", "pgedge-postgresql-18-safesession")),
    (_DEB, "deb", ()),                                                            # nothing installed
], ids=["llvmjit", "other-pg", "replaced", "deb-other-pg", "deb-none"])
def test_any_safesession_package_but_exactly_the_request_fails_the_install(monkeypatch, tmp_path, env, ctype, present):
    m = load(monkeypatch, env)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(*present, "pgedge-postgresql17-server")))
    _install_spies(monkeypatch, m)
    with pytest.raises(AssertionError, match="expected exactly the requested safesession package"):
        m.test_component_install(env["PEP_CONTAINER_ALIAS"], ctype)


def test_install_step_refuses_a_request_the_backstop_rejects(monkeypatch, tmp_path):
    # A valid spock request reaching the safesession test: normalize_request accepts it, the
    # backstop does not, and nothing is installed.
    m = load(monkeypatch, dict(_RPM, PEP_COMPONENT="spock", PEP_PACKAGE_NAME="pgedge-spock50_17",
                               PEP_EXPECTED_VERSION="5.0.13", PEP_EXPECTED_RPM="5.0.13-1.el9"))
    assert "not the PG-coupled safesession component" in m.INTEGRATION_PROBLEM
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    ip, pinned = _install_spies(monkeypatch, m)
    with pytest.raises(AssertionError, match="cannot be served"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert ip.calls == [] and pinned.calls == []


def test_standalone_install_is_unchanged(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    ip, pinned = _install_spies(monkeypatch, m)
    m.test_component_install("rocky9-amd64", "rhel")
    m.test_component_install("debian13-amd64", "deb")
    assert pinned.calls == []
    assert [c[0][1] for c in ip.calls] == ["pgedge-safesession_17", "pgedge-postgresql-17-safesession"]


def test_integration_never_upgrades_the_pinned_package(monkeypatch):
    m = load(monkeypatch, dict(_RPM, UPGRADE="true"))
    up = _Spy((True, "rhel", "upgraded"))
    monkeypatch.setattr(m.package_management, "upgrade_package", up)
    assert _outcome(m.test_component_upgrade, "rocky9-amd64", "rhel")[0] == "skipped" and up.calls == []


# --------------------------------------------------------------------------- identity
@pytest.mark.parametrize("observed,ok", [("1.1-1.el9", True), ("1.0-1.el9", False), ("1.1-rc1_1.el9", False)],
                         ids=["exact", "older", "rc"])
def test_identity_proves_the_exact_requested_package_version(monkeypatch, tmp_path, observed, ok):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    m.pep_integration_mode.pep_evidence.write_install_evidence(
        m.INTEGRATION_REQUEST, "run-A", "pinned", "1.1-1.el9", str(tmp_path / "install-evidence.json"),
        installed_sha256="f" * 64)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: observed)
    if ok:
        m.test_safesession_identity("rocky9-amd64", "rhel")
    else:
        with pytest.raises(AssertionError, match="not proven"):
            m.test_safesession_identity("rocky9-amd64", "rhel")
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")


def test_identity_without_install_evidence_fails_with_persisted_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    with pytest.raises(pytest.fail.Exception, match="install-before-identity precondition failed"):
        m.test_safesession_identity("rocky9-amd64", "rhel")
    assert (tmp_path / "identity-evidence.json").exists()


def test_identity_and_package_version_steps_by_mode(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, PGEDGE_SAFESESSION_17_VERSION="2.5"))
    assert _outcome(m.test_safesession_identity, "rocky9-amd64", "rhel")[0] == "skipped"
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    verify = _Spy((True, "rhel", "2.5-1.el9", "ok"))
    monkeypatch.setattr(m.package_management, "verify_package_version", verify)
    m.test_component_package_version("rocky9-amd64", "rhel")
    assert verify.calls[0][0][1:] == ("pgedge-safesession_17", "2.5")       # standalone: configured version
    m = load(monkeypatch, _RPM)
    outcome, msg = _outcome(m.test_component_package_version, "rocky9-amd64", "rhel")
    assert outcome == "skipped" and "test_safesession_identity" in msg


# --------------------------------------------------------------------------- the server model
_R, _M, _C, _S = "pep_ss_restricted", "pep_ss_member", "pep_ss_control", "pep_ss_superuser"
_HINT = "This session is restricted to read-only access by the pgedge_safesession extension."
_INSERT_RE = re.compile(r"INSERT INTO pep_safesession\.data VALUES \((\d+), '(\w+)'\);")
_CREATE_RE = re.compile(r"CREATE TABLE pep_safesession\.(\w+) \(id int\);")


def _kind(sql):
    for kind, test in (("setup", lambda s: "CREATE ROLE" in s), ("drop", lambda s: s.startswith("DROP SCHEMA")),
                       ("alter", lambda s: s.startswith("ALTER SYSTEM SET")),
                       ("reset", lambda s: s.startswith("ALTER SYSTEM RESET")),
                       ("reload", lambda s: "pg_reload_conf()" in s),
                       ("preload", lambda s: s == "SHOW shared_preload_libraries;"),
                       ("show", lambda s: s == "SHOW pgedge_safesession.roles;"),
                       ("identity", lambda s: "pgedge_safesession_is_restricted()" in s),
                       ("INSERT", lambda s: s.startswith("INSERT")), ("CREATE TABLE", lambda s: s.startswith("CREATE TABLE")),
                       ("rows", lambda s: s.startswith("SELECT id, writer")), ("tables", lambda s: "string_agg(relname" in s),
                       ("libdir", lambda s: "PKGLIBDIR" in s), ("num", lambda s: "server_version_num" in s),
                       ("ext", lambda s: "extversion" in s)):
        if test(sql):
            return kind
    return None


class _Cluster(_Container):
    """A container running a SafeSession-enabled server. psql sessions are answered by a small model
    of the product: a session is restricted when its role is listed in pgedge_safesession.roles or is
    a member of a listed role, unless it is a superuser; a restricted INSERT / CREATE TABLE fails
    with SafeSession's error and hint; ALTER SYSTEM takes effect after pg_reload_conf(), in sessions
    started ``reload_delay`` sessions later (never if ``reload_applies`` is False). ``faults`` maps
    (role, statement kind) to the (exit_code, output) to return instead, without changing state."""

    SUPERUSERS = {"postgres", _S}
    MEMBERSHIP = {_M: _R, _S: _R}
    GRANTEES = {_R, _M, _C}

    def __init__(self, preload="pgedge_safesession", reload_applies=True, reload_delay=0, faults=None,
                 owner="pgedge-safesession_17", num="170011", ext="1.1", libdir="/usr/pgsql-17/lib",
                 file_out="ELF 64-bit LSB shared object, x86-64, dynamically linked, stripped"):
        super().__init__(self._respond)
        self.preload, self.reload_applies, self.reload_delay = preload, reload_applies, reload_delay
        self.faults = faults or {}
        self.owner, self.num, self.ext, self.libdir, self.file_out = owner, num, ext, libdir, file_out
        self.guc, self.pending, self.since_reload = "", None, None
        self.rows, self.tables, self.sessions = [], set(), []

    def restricted(self, role):
        listed = [r.strip() for r in self.guc.split(",") if r.strip()]
        return role not in self.SUPERUSERS and (role in listed or self.MEMBERSHIP.get(role) in listed)

    def _respond(self, cmd):
        if cmd[0].endswith("psql"):
            return self._psql(cmd[cmd.index("-U") + 1], cmd[-1])
        if cmd[:2] == ["rpm", "-qf"]:
            return (0, self.owner + "\n") if self.owner else (1, "file %s is not owned by any package" % cmd[-1])
        if cmd[:2] == ["dpkg-query", "-S"]:
            return (0, "%s: %s\n" % (self.owner, cmd[2])) if self.owner else (1, "no path found matching pattern")
        if cmd[:3] == ["file", "-b", "-L"]:
            return 0, self.file_out
        return 1, "unexpected %r" % (cmd,)

    def _psql(self, role, sql):
        kind = _kind(sql)
        self.sessions.append((role, kind, sql))
        if self.since_reload is not None:              # a new session after a reload
            if self.reload_applies and self.since_reload >= self.reload_delay:
                self.guc, self.since_reload = self.pending, None
            else:
                self.since_reload += 1
        if (role, kind) in self.faults:
            return self.faults[(role, kind)]
        if kind == "preload":
            return 0, self.preload
        if kind == "setup":
            self.rows, self.tables = [(1, "setup"), (2, "setup")], {"data"}
            return 0, ""
        if kind in ("alter", "reset"):
            self.pending = sql.split("'")[1] if kind == "alter" else ""
            return 0, ""
        if kind == "reload":
            self.since_reload = 0
            return 0, "t"
        if kind == "show":
            return 0, self.guc
        if kind == "identity":
            extra = "|%d" % len(self.rows) if "count(*)" in sql else ""
            return 0, "%s|%s%s" % (role, "t" if self.restricted(role) else "f", extra)
        if kind in ("INSERT", "CREATE TABLE"):
            if self.restricted(role):
                return 1, "ERROR:  cannot execute %s in a read-only session\nHINT:  %s" % (kind, _HINT)
            if role not in self.GRANTEES | self.SUPERUSERS:
                return 1, "ERROR:  permission denied for schema pep_safesession"
            if kind == "INSERT":
                i, w = _INSERT_RE.fullmatch(sql).groups()
                self.rows.append((int(i), w))
            else:
                self.tables.add(_CREATE_RE.fullmatch(sql).group(1))
            return 0, ""
        if kind == "rows":
            return 0, "\n".join("%d|%s" % r for r in sorted(self.rows))
        if kind == "tables":
            return 0, ",".join(sorted(self.tables))
        if kind == "drop":
            self.rows, self.tables = [], set()
            return 0, ""
        if kind == "libdir":
            return 0, self.libdir
        if kind == "num":
            return 0, self.num
        if kind == "ext":
            return 0, self.ext
        return 1, "unexpected SQL %r" % (sql,)


# --------------------------------------------------------------------------- binding (integration)
def _binding(monkeypatch, env=_RPM, ctype="rhel", **server):
    m = load(monkeypatch, env)
    c = _Cluster(**server)
    monkeypatch.setattr(m, "client", _Client(c))
    return _outcome(m.test_extension_version, env["PEP_CONTAINER_ALIAS"], ctype, "pgedge_safesession"), c


@pytest.mark.parametrize("ext", ["1.1", "1.1-alpha1"])
def test_binding_accepts_the_release_series_with_at_most_a_pre_release_marker(monkeypatch, ext):
    (outcome, msg), c = _binding(monkeypatch, ext=ext)
    assert outcome == "passed", msg
    assert ["rpm", "-qf", "--queryformat", "%{NAME}\\n", "/usr/pgsql-17/lib/pgedge_safesession.so"] in c.cmds
    assert ("postgres", "preload") in [(r, k) for r, k, _ in c.sessions]


def test_binding_passes_on_deb(monkeypatch):
    (outcome, msg), _ = _binding(monkeypatch, _DEB, "deb", owner="pgedge-postgresql-17-safesession",
                                 libdir="/usr/lib/postgresql/17/lib")
    assert outcome == "passed", msg


@pytest.mark.parametrize("over,msg", [
    ({"ext": "1.10"}, "pgedge_safesession extension version is '1.10'"),
    ({"ext": "1.1.0"}, "pgedge_safesession extension version is '1.1.0'"),
    ({"ext": "1.2"}, "pgedge_safesession extension version is '1.2'"),
    ({"ext": "1.1~beta1"}, "pgedge_safesession extension version is '1.1~beta1'"),
    ({"ext": "1.0"}, "pgedge_safesession extension version is '1.0'"),
    ({"owner": "pgedge-safesession_16"}, "not the requested 'pgedge-safesession_17'"),
    ({"owner": ""}, "not the requested"),
    ({"num": "160004"}, "runs PostgreSQL 160004, not the requested PG17"),
    ({"preload": ""}, "does not include 'pgedge_safesession'"),
    ({"preload": "pg_stat_statements, pgedge_safesession_x"}, "does not include 'pgedge_safesession'"),
], ids=["1.10", "1.1.0", "1.2", "tilde-marker", "config-1.0", "owner", "unowned", "pg-major",
        "no-preload", "other-preload"])
def test_binding_rejects_a_wrong_version_library_major_or_missing_preload(monkeypatch, over, msg):
    (outcome, text), _ = _binding(monkeypatch, **over)
    assert outcome == "assert" and msg in text, text


def test_standalone_extension_version_is_unchanged(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, SAFESESSION_EXTENSION_VERSION="2.5"))
    row = " pgedge_safesession | %s | public | Enforce read-only sessions for specified roles"
    for installed, ok in (("2.5", True), ("2.5.1", False), ("2.5-alpha1", False)):
        c = _Container(lambda cmd, v=installed: (0, row % v))
        monkeypatch.setattr(m, "client", _Client(c))
        assert (_outcome(m.test_extension_version, "rocky9-amd64", "rhel", "pgedge_safesession")[0] == "passed") is ok
        assert "\\dx" in c.cmds[0][-1]                                  # the configured \dx comparison


# --------------------------------------------------------------------------- enforcement (both modes)
_EXPECTED_SESSIONS = [
    ("postgres", "preload"), ("postgres", "setup"), ("postgres", "alter"), ("postgres", "reload"), ("postgres", "show"),
    (_R, "identity"), (_R, "INSERT"), (_R, "CREATE TABLE"),
    (_M, "identity"), (_M, "INSERT"), (_M, "CREATE TABLE"),
    (_C, "identity"), (_C, "INSERT"), (_C, "CREATE TABLE"),
    (_S, "identity"), (_S, "INSERT"),
    ("postgres", "rows"), ("postgres", "tables"),
    ("postgres", "reset"), ("postgres", "reload"), ("postgres", "drop")]


def _enforce(monkeypatch, env=_STANDALONE, ctype="rhel", alias="rocky9-amd64", **server):
    m = load(monkeypatch, env)
    c = _Cluster(**server)
    monkeypatch.setattr(m, "client", _Client(c))
    return _outcome(m.test_safesession_enforcement, alias, ctype), c


@pytest.mark.parametrize("env,ctype,alias", [(_STANDALONE, "rhel", "rocky9-amd64"), (_STANDALONE, "deb", "debian13-amd64"),
                                             (_RPM, "rhel", "rocky9-amd64"), (_DEB, "deb", "debian13-amd64")],
                         ids=["standalone-rpm", "standalone-deb", "integration-rpm", "integration-deb"])
def test_enforcement_passes_and_every_check_runs_as_the_acting_role(monkeypatch, env, ctype, alias):
    (outcome, msg), c = _enforce(monkeypatch, env, ctype, alias)
    assert outcome == "passed", msg
    assert [(r, k) for r, k, _ in c.sessions] == _EXPECTED_SESSIONS
    pgbin = "/usr/pgsql-17/bin" if ctype == "rhel" else "/usr/lib/postgresql/17/bin"
    for cmd in c.cmds:          # each statement is its own session, authenticated as the acting role
        assert cmd[0] == pgbin + "/psql" and cmd[cmd.index("-h") + 1] == "localhost"
        assert sum(1 for a in cmd if a == "-c") == 1 and "-U" in cmd
    for _role, _kind, sql in c.sessions:
        assert "SET ROLE" not in sql.upper() and "SESSION AUTHORIZATION" not in sql.upper()
    setup = c.sessions[1][2]
    for grant in ("GRANT USAGE, CREATE ON SCHEMA pep_safesession TO pep_ss_restricted, pep_ss_member, pep_ss_control",
                  "GRANT ALL ON TABLE pep_safesession.data TO pep_ss_restricted, pep_ss_member, pep_ss_control",
                  "GRANT pep_ss_restricted TO pep_ss_member, pep_ss_superuser", "CREATE ROLE pep_ss_superuser LOGIN SUPERUSER"):
        assert grant in setup
    assert c.sessions[2][2] == "ALTER SYSTEM SET pgedge_safesession.roles = 'pep_ss_restricted';"
    assert c.guc == "" and c.rows == [] and c.tables == set()                    # cleaned up


def test_enforcement_waits_for_the_reload_to_reach_new_sessions(monkeypatch):
    (outcome, msg), c = _enforce(monkeypatch, reload_delay=3)
    assert outcome == "passed", msg
    assert [k for _r, k, _ in c.sessions].count("show") == 4


def test_enforcement_fails_when_the_setting_never_takes_effect(monkeypatch):
    (outcome, msg), c = _enforce(monkeypatch, reload_applies=False)
    assert outcome == "assert" and "pgedge_safesession.roles is '' in a new session" in msg
    kinds = [k for _r, k, _ in c.sessions]
    assert kinds.count("show") == 20 and "INSERT" not in kinds and kinds[-1] == "drop"     # cleanup still ran


def test_enforcement_fails_without_the_preload_and_runs_nothing_else(monkeypatch):
    (outcome, msg), c = _enforce(monkeypatch, preload="pg_stat_statements")
    assert outcome == "assert" and "does not include 'pgedge_safesession'" in msg
    assert [k for _r, k, _ in c.sessions] == ["preload", "reset", "reload", "drop"]


_SAFESESSION_INSERT = (1, "ERROR:  cannot execute INSERT in a read-only session\nHINT:  " + _HINT)


@pytest.mark.parametrize("faults,msg", [
    ({(_R, "INSERT"): (0, "")}, "as pep_ss_restricted: INSERT succeeded"),
    ({(_R, "CREATE TABLE"): (0, "")}, "as pep_ss_restricted: CREATE TABLE succeeded"),
    ({(_R, "INSERT"): (1, "ERROR:  permission denied for table data")}, "not with SafeSession's rejection"),
    ({(_R, "INSERT"): (1, "ERROR:  cannot execute INSERT in a read-only transaction")}, "not with SafeSession's rejection"),
    ({(_R, "INSERT"): (1, "ERROR:  cannot execute INSERT in a read-only session")}, "not with SafeSession's rejection"),
    ({(_R, "INSERT"): (2, "psql: error: connection to server failed: FATAL:  role does not exist")},
     "not with SafeSession's rejection"),
    ({(_R, "CREATE TABLE"): (1, "ERROR:  permission denied for schema pep_safesession")}, "not with SafeSession's rejection"),
    ({(_R, "CREATE TABLE"): _SAFESESSION_INSERT}, "not with SafeSession's rejection"),
    ({(_R, "identity"): (0, "pep_ss_restricted|f|2")}, "restricted session: expected 'pep_ss_restricted|t|2'"),
    ({(_R, "identity"): (0, "postgres|t|2")}, "restricted session: expected"),
    ({(_R, "identity"): (1, "ERROR:  permission denied for table data")}, "as pep_ss_restricted"),
    ({(_M, "identity"): (0, "pep_ss_member|f")}, "member session: expected 'pep_ss_member|t'"),
    ({(_M, "INSERT"): (0, "")}, "as pep_ss_member: INSERT succeeded"),
    ({(_M, "CREATE TABLE"): (0, "")}, "as pep_ss_member: CREATE TABLE succeeded"),
    ({(_C, "identity"): (0, "pep_ss_control|t")}, "control session: expected 'pep_ss_control|f'"),
    ({(_C, "INSERT"): _SAFESESSION_INSERT}, "as pep_ss_control: INSERT INTO"),
    ({(_C, "CREATE TABLE"): (1, "ERROR:  cannot execute CREATE TABLE in a read-only session\nHINT:  " + _HINT)},
     "as pep_ss_control: CREATE TABLE"),
    ({(_S, "identity"): (0, "pep_ss_superuser|t")}, "superuser session: expected 'pep_ss_superuser|f'"),
    ({(_S, "INSERT"): _SAFESESSION_INSERT}, "as pep_ss_superuser: INSERT INTO"),
    ({("postgres", "rows"): (0, "1|setup\n2|setup\n10|restricted\n20|control\n30|superuser")}, "table contents"),
    ({("postgres", "rows"): (0, "1|setup\n2|setup\n20|control")}, "table contents"),
    ({("postgres", "tables"): (0, "by_control,by_restricted,data")}, "tables in pep_safesession"),
    ({("postgres", "tables"): (0, "data")}, "tables in pep_safesession"),
    ({("postgres", "reload"): (0, "f")}, "pg_reload_conf() did not signal a reload"),
    ({("postgres", "setup"): (1, "ERROR:  role \"pep_ss_restricted\" already exists")}, "failed"),
], ids=["restricted-insert-succeeds", "restricted-create-succeeds", "plain-permission-denied",
        "core-read-only-transaction", "no-hint", "connection-failed", "create-permission-denied",
        "wrong-command-in-error", "restricted-not-restricted", "not-the-session-user", "restricted-cannot-read",
        "member-not-restricted", "member-insert-succeeds", "member-create-succeeds",
        "control-restricted", "control-insert-blocked", "control-create-blocked",
        "superuser-restricted", "superuser-blocked", "extra-row", "missing-row", "extra-table", "missing-table",
        "reload-not-signalled", "setup-failed"])
def test_enforcement_rejects_every_wrong_outcome(monkeypatch, faults, msg):
    (outcome, text), c = _enforce(monkeypatch, faults=faults)
    assert outcome == "assert" and msg in text, text
    assert [k for _r, k, _ in c.sessions][-3:] == ["reset", "reload", "drop"]        # cleanup still ran


def test_enforcement_fails_when_cleanup_fails_after_a_pass(monkeypatch):
    (outcome, msg), _ = _enforce(monkeypatch, faults={("postgres", "drop"): (1, "ERROR:  role is in use")})
    assert outcome == "assert" and "enforcement cleanup failed" in msg and "role is in use" in msg


def test_a_cleanup_failure_does_not_hide_the_check_failure(monkeypatch):
    (outcome, msg), _ = _enforce(monkeypatch, faults={(_R, "INSERT"): (0, ""), ("postgres", "drop"): (1, "boom")})
    assert outcome == "assert" and "INSERT succeeded" in msg


@pytest.mark.parametrize("env,want", [(_STANDALONE, "skipped"), (_RPM, "failed")], ids=["standalone", "integration"])
def test_disabled_extension_checks_skip_standalone_and_fail_integration(monkeypatch, env, want):
    m = load(monkeypatch, dict(env, CHECK_EXTENSIONS="false"))
    monkeypatch.setattr(m, "client", _Client(_Cluster()))
    for fn, args in ((m.test_safesession_enforcement, ()), (m.test_create_extensions, ("pgedge_safesession",)),
                     (m.test_extension_version, ("pgedge_safesession",))):
        assert _outcome(fn, "rocky9-amd64", "rhel", *args)[0] == want


# --------------------------------------------------------------------------- missing references
_NO_REFERENCE = Exception("No expected file found for safesession at /x/expected-output/rpm (checked: /x/a)")


def _raises(exc):
    def verify(**kwargs):
        raise exc
    return verify


@pytest.mark.parametrize("env,want", [(_RPM, "failed"), (_STANDALONE, "skipped")], ids=["integration", "standalone"])
def test_a_missing_bundled_file_reference_fails_integration_and_skips_standalone(monkeypatch, env, want):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    monkeypatch.setattr(m.file_management, "verify_bundled_files", _raises(_NO_REFERENCE))
    outcome, msg = _outcome(m.test_verify_bundled_files, "rocky9-amd64", "rhel")
    assert outcome == want and "No expected file found" in msg


@pytest.mark.parametrize("env", [_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_bundled_file_outcomes_are_unchanged_when_the_reference_exists(monkeypatch, env):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    monkeypatch.setattr(m.file_management, "verify_bundled_files", lambda **k: (True, None, "match"))
    assert _outcome(m.test_verify_bundled_files, "rocky9-amd64", "rhel") == ("passed", "")
    monkeypatch.setattr(m.file_management, "verify_bundled_files",
                        lambda **k: (False, {"missing_files": ["/usr/x"]}, "Bundled files differ"))
    outcome, msg = _outcome(m.test_verify_bundled_files, "rocky9-amd64", "rhel")
    assert outcome == "failed" and "Missing files" in msg and "/usr/x" in msg
    monkeypatch.setattr(m.file_management, "verify_bundled_files", _raises(RuntimeError("rpm -ql failed")))
    outcome, msg = _outcome(m.test_verify_bundled_files, "rocky9-amd64", "rhel")
    assert outcome == "failed" and "rpm -ql failed" in msg


_CONTAINER_STEPS = ["test_configure_repository", "test_component_install", "test_safesession_identity",
                    "test_verify_bundled_files", "test_verify_shared_library", "test_library_stripped",
                    "test_verify_license_file", "test_verify_readme_file", "test_verify_sbom", "test_init_cluster",
                    "test_start_server", "test_check_connection", "test_safesession_enforcement",
                    "test_stop_server", "test_package_uninstall", "test_pgedge_cleanup"]


@pytest.mark.parametrize("env,want", [(_RPM, "failed"), (_STANDALONE, "skipped")], ids=["integration", "standalone"])
def test_a_missing_container_fails_integration_and_skips_standalone(monkeypatch, env, want):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(None))
    for step in _CONTAINER_STEPS + ["test_create_extensions", "test_extension_version"]:
        args = ("pgedge_safesession",) if step in ("test_create_extensions", "test_extension_version") else ()
        outcome, msg = _outcome(getattr(m, step), "rocky9-amd64", "rhel", *args)
        if step == "test_safesession_identity" and env is _STANDALONE:
            assert outcome == "skipped" and "integration mode only" in msg
            continue
        assert (outcome, step) == (want, step) and "not found or not running" in msg


@pytest.mark.parametrize("env", [_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_a_missing_sbom_fails_in_both_modes(monkeypatch, env):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m.machine_prereq_setup, "ensure_sq_installed", lambda c: None)

    def respond(cmd):
        if isinstance(cmd, str) and "sq verify --help" in cmd:
            return 0, "--signer-file --signature-file"
        if isinstance(cmd, str) and cmd.startswith("wget"):
            return 0, ""
        return 1, "Error: No such file or directory: pgedge-safesession-sbom.json"
    monkeypatch.setattr(m, "client", _Client(_Container(respond)))
    outcome, msg = _outcome(m.test_verify_sbom, "rocky9-amd64", "rhel")
    assert outcome == "assert" and "SBOM verification failed" in msg


# --------------------------------------------------------------------------- stripped
@pytest.mark.parametrize("env", [_RPM, _STANDALONE], ids=["integration", "standalone"])
@pytest.mark.parametrize("file_out,ok", [
    ("ELF 64-bit LSB shared object, x86-64, version 1 (SYSV), dynamically linked, stripped", True),
    ("ELF 64-bit LSB shared object, x86-64, version 1 (SYSV), dynamically linked, not stripped", False),
    ("ELF 64-bit LSB shared object, ARM aarch64, dynamically linked, with debug_info, not stripped", False),
], ids=["stripped", "not-stripped", "debug-info"])
def test_library_must_really_be_stripped(monkeypatch, env, file_out, ok):
    m = load(monkeypatch, env)
    c = _Cluster(file_out=file_out)
    monkeypatch.setattr(m, "client", _Client(c))
    outcome, msg = _outcome(m.test_library_stripped, "rocky9-amd64", "rhel")
    assert (outcome == "passed") is ok, msg
    assert c.cmds == [["file", "-b", "-L", "/usr/pgsql-17/lib/pgedge_safesession.so"]]


# --------------------------------------------------------------------------- bundled-file references
# The files the packaging installs for PG17 (not an install listing): RPM from
# pkg/rpm/pgedge-safesession.spec %files (the -llvmjit bitcode is in its own subpackage); DEB from
# PGXS (library, bitcode, control, sql/*.sql), debian/pgedge-postgresql-safesession.install (sbom/*),
# rules (dh_installdocs README.*; 2.2 KiB, so not compressed), changelog and copyright.
_SQL = ["pgedge_safesession--1.0-alpha1--1.0-beta1.sql", "pgedge_safesession--1.0-beta1--1.0.sql",
        "pgedge_safesession--1.0.sql", "pgedge_safesession.control"]
_PACKAGED = {
    "rhel": ["/usr/pgsql-17/lib/pgedge_safesession.so", "/usr/pgsql-17/sbom/pgedge-safesession-sbom.json",
             "/usr/pgsql-17/sbom/pgedge-safesession-sbom.json.asc"]
            + ["/usr/pgsql-17/share/extension/" + f for f in _SQL]
            + ["/usr/share/doc/pgedge-safesession_17", "/usr/share/doc/pgedge-safesession_17/README.md",
               "/usr/share/licenses/pgedge-safesession_17", "/usr/share/licenses/pgedge-safesession_17/LICENCE.md"],
    "deb": ["/.", "/usr/lib/postgresql/17/lib/bitcode/pgedge_safesession/src/pgedge_safesession.bc",
            "/usr/lib/postgresql/17/lib/bitcode/pgedge_safesession.index.bc",
            "/usr/lib/postgresql/17/lib/pgedge_safesession.so", "/usr/lib/postgresql/17/sbom/pgedge-safesession-sbom.json",
            "/usr/lib/postgresql/17/sbom/pgedge-safesession-sbom.json.asc",
            "/usr/share/doc/pgedge-postgresql-17-safesession/README.md",
            "/usr/share/doc/pgedge-postgresql-17-safesession/changelog.Debian.gz",
            "/usr/share/doc/pgedge-postgresql-17-safesession/copyright"]
           + ["/usr/share/postgresql/17/extension/" + f for f in _SQL],
}


@pytest.mark.parametrize("ctype,alias", [("rhel", "rocky9-amd64"), ("deb", "debian13-amd64")])
def test_bundled_file_references_match_what_the_packaging_installs(monkeypatch, ctype, alias):
    m = load(monkeypatch, _STANDALONE)
    files = list(_PACKAGED[ctype])

    def respond(cmd):
        assert cmd in ("rpm -ql pgedge-safesession_17", "dpkg -L pgedge-postgresql-17-safesession")
        return 0, "\n".join(files) + "\n"
    monkeypatch.setattr(m, "client", _Client(_Container(respond)))
    assert _outcome(m.test_verify_bundled_files, alias, ctype) == ("passed", "")
    # ... and each packaged file is still required (the comparison is not loosened).
    for required in ("sbom.json.asc", "README.md"):
        files[:] = [f for f in _PACKAGED[ctype] if not f.endswith(required)]
        outcome, msg = _outcome(m.test_verify_bundled_files, alias, ctype)
        assert outcome == "failed" and required.lower() in msg, msg
