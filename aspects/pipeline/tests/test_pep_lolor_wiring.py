"""Docker-free tests that exercise the REAL functions in component-test/test_pep_lolor.py.

The module is loaded by path with docker.from_env (and load_dotenv) neutralized and with the
environment of the case under test (the integration request and platform filter are decided at
import time), then driven with a fake docker client/container and spied package helpers -- the same
approach as test_pep_spock_wiring.py. Versions are synthetic (9.8.7) so nothing here depends on
today's lolor release; the package version (9.8.7-1.el9), the SQL extension version (9.8.7) and the
library (lolor.so, no binary version) are kept distinct.

Covers: request validation at load (wrong package, PG major, family, uncertified companion); the
request family overriding PLATFORM_FILTER; the backstop; the server companion before the pinned
package; failed installs leaving no evidence; only the requested lolor package installed; identity;
the binding of the running server (library owner, PG major, exact extension version); missing
references failing in integration mode and skipping standalone; the strict stripped check; the
function swap on CREATE/DROP; the large-object round trip, node and replication checks failing on a
SQL error or a wrong value; the replication timeout; and the standalone paths kept as they were.
"""
import importlib.util
import itertools
import json
import os
import re
import sys

import docker
import dotenv
import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "component-test", "test_pep_lolor.py")
_counter = itertools.count()

_RPM = {"PEP_INTEGRATION_MODE": "1", "PEP_COMPONENT": "lolor", "PEP_PACKAGE_NAME": "pgedge-lolor_17",
        "PEP_CHANNEL": "staging", "PEP_EXPECTED_VERSION": "9.8.7", "PEP_FAMILY": "rpm",
        "PEP_ARCH_FILTER": "amd64", "PEP_CONTAINER_ALIAS": "rocky9-amd64", "PG_MAJOR_VERSION": "17",
        "PEP_EXPECTED_RPM": "9.8.7-1.el9", "PEP_EXPECTED_BUILDNUM": "1",
        "REPO": "staging", "CONTAINERS": "rocky9-amd64", "DEB_CONTAINERS": "debian13-amd64",
        "PG_PORT": "6433", "PG_DATA_DIR": "/tmp/n1"}
_DEB = dict(_RPM, PEP_PACKAGE_NAME="pgedge-postgresql-17-lolor", PEP_FAMILY="deb",
            PEP_CONTAINER_ALIAS="debian13-amd64", PEP_EXPECTED_RPM=None, PEP_EXPECTED_DEB="9.8.7-1.trixie")
# Standalone: the config names a different (older) version, which integration mode must never use.
_STANDALONE = {"PG_MAJOR_VERSION": "17", "CONTAINERS": "rocky9-amd64", "DEB_CONTAINERS": "debian13-amd64",
               "PGEDGE_LOLOR_17_VERSION": "9.8.6", "PG_PORT": "6433", "PG_DATA_DIR": "/tmp/n1"}
_KEYS = sorted(set(_RPM) | set(_DEB) | {
    "PLATFORM_FILTER", "PGEDGE_LOLOR_17_VERSION", "CHECK_EXTENSIONS", "BASE_EXTENSIONS", "LOLOR_PACKAGE",
    "DEB_LOLOR_PACKAGE", "RHEL_LOLOR_LIB", "DEB_LOLOR_LIB", "LOLOR_STRIPPED_LIB", "PEP_MODE", "PEP_SCENARIO",
    "PEP_EFFECTIVE_TAG", "PEP_EXPECTED_BINARY", "UPGRADE", "PG_BIN_PATH", "DEB_PG_BIN_PATH", "RHEL_PG_PATH",
    "DEB_PG_PATH", "PG_USER", "DEB_PG_USER"})


def load(monkeypatch, env):
    """Load a fresh copy of the lolor component test under exactly ``env`` (None values unset)."""
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    orig = docker.from_env
    docker.from_env = lambda *a, **k: None
    try:
        name = "lolor_under_test_%d" % next(_counter)
        spec = importlib.util.spec_from_file_location(name, _PATH)
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
    def __init__(self, container=None):
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


def _script(cmd):
    """``(label, port, sql)`` of a psql script run by run_sql, or None for any other command."""
    if not (isinstance(cmd, list) and cmd[:2] == ["bash", "-c"] and "LOLOR_EOSQL" in cmd[2]):
        return None
    label = re.search(r"-- lolor-check: (.*)\n", cmd[2]).group(1)
    port = re.search(r" -p (\S+) ", cmd[2]).group(1)
    return label, port, cmd[2]


def _labelled(**values):
    return "".join("%s=%s\n" % (k, v) for k, v in values.items() if v is not None)


def _fn_lines(m, enabled=True):
    out = []
    for name, args, symbol in m.SWAPPED_FUNCTIONS:
        if enabled:
            out.append("fn=%s(%s)|c|$libdir/lolor|%s" % (name, args, symbol))
            out.append("fn=%s_orig(%s)|internal||be_%s" % (name, args, name))
        else:
            out.append("fn=%s(%s)|internal||be_%s" % (name, args, name))
    return out


_LOID = (12345 << 4) | 1          # node 1 = PRIMARY_NODE in its low 4 bits


def _round_trip(m, **over):
    vals = {"node": "1", "lob_block": "2048", "core_totals_before": "0,0", "loid": str(_LOID),
            "written": str(len(m.PAYLOAD)), "tell_after_write": str(len(m.PAYLOAD)), "seek_start": "0",
            "read": m.PAYLOAD.hex(), "tell_after_read": str(len(m.PAYLOAD)), "truncate": "0",
            "size_after_truncate": str(m.TRUNCATE_TO), "close": "0", "lolor_meta_rows": "1",
            "lolor_pages": "2", "lolor_data": m.PAYLOAD[:m.TRUNCATE_TO].hex(),
            "lo_get": m.PAYLOAD[:m.TRUNCATE_TO].hex(), "core_rows_for_loid": "0,0",
            "core_totals_after": "0,0", "unlink": "1", "rows_after_unlink": "0,0",
            "explicit_oid": str(m.EXPLICIT_OID), "explicit_unlink": "1",
            "session_node_oid": str((777 << 4) | 15), "session_unlink": "1"}
    vals.update(over)
    return _labelled(**vals)


class _Server:
    """exec_run answers for a healthy lolor install. ``scripts`` overrides psql scripts by label:
    a value is ``(exit_code, text)`` or a callable returning one."""

    def __init__(self, m, package="pgedge-lolor_17", installed=None, owner=None, versions=None,
                 ext=None, scripts=None, files=None, file_out=None, sbom_present=True):
        self.m = m
        self.package = package
        self.installed = list(installed if installed is not None else [package])
        self.owner = package if owner is None else owner
        self.versions = versions or {}
        self.ext = ext or {}
        self.files = files if files is not None else ["/usr/pgsql-17/lib/lolor.so",
                                                      "/usr/pgsql-17/share/extension/lolor.control"]
        self.file_out = file_out or "ELF 64-bit LSB shared object, x86-64, dynamically linked, stripped"
        self.sbom_present = sbom_present
        self.scripts = {
            "large-object functions after CREATE EXTENSION": (0, "\n".join(_fn_lines(m)) + "\n"),
            "large-object round trip": (0, _round_trip(m)),
            "lolor.node boot value": (0, "boot=0\n"),
            "lo_creat with lolor.node 0": (3, "psql:<stdin>:3: ERROR:  value for lolor.node is not set\n"),
            "lolor.node out of range": (3, 'psql:<stdin>:2: ERROR:  17 is outside the valid range for '
                                           'parameter "lolor.node" (0 .. 16)\n'),
            "DROP EXTENSION lolor": (0, "\n".join(_fn_lines(m, enabled=False)) + "\n"
                                     + _labelled(lolor_tables=0, event_trigger=0, core_meta=1, core_unlink=1)),
        }
        self.scripts.update(scripts or {})
        self.seen = []

    def __call__(self, cmd):
        script = _script(cmd)
        if script is not None:
            label, port, _sql = script
            self.seen.append((label, port))
            answer = self.scripts.get(label, (0, ""))
            return answer() if callable(answer) else answer
        if cmd[0] == "psql":                       # the shared helper's psql_value
            port, sql = cmd[cmd.index("-p") + 1], cmd[-1]
            if "PKGLIBDIR" in sql:
                return 0, "/usr/pgsql-17/lib\n"
            if "server_version_num" in sql:
                return 0, self.versions.get(port, "170011") + "\n"
            if "extversion" in sql:
                return 0, self.ext.get(port, "9.8.7") + "\n"
        if cmd[:2] == ["rpm", "-qa"]:
            return 0, "\n".join(self.installed) + "\n"
        if cmd[:2] == ["dpkg-query", "-W"]:
            return 0, "".join("%s install ok installed\n" % n for n in self.installed)
        if cmd[:2] == ["rpm", "-qf"]:
            return (0, self.owner + "\n") if self.owner else (1, "file is not owned by any package")
        if cmd[:2] == ["dpkg-query", "-S"]:
            return (0, "%s: %s\n" % (self.owner, cmd[2])) if self.owner else (1, "no path found")
        if cmd[:2] in (["rpm", "-ql"], ["dpkg", "-L"]):
            return 0, "\n".join(self.files) + "\n"
        if cmd[:3] == ["file", "-b", "-L"]:
            return 0, self.file_out
        if cmd[:2] == ["test", "-f"]:
            return (0, "") if self.sbom_present else (1, "")
        return 0, ""


def _wire(monkeypatch, tmp_path, m, container):
    monkeypatch.setattr(m, "client", _Client(container))
    for k, f in (("PEP_INSTALL_OUT", "install-evidence.json"), ("PEP_IDENTITY_OUT", "identity-evidence.json"),
                 ("PEP_OBSERVED_OUT", "observed-identity.json"), ("PEP_PREREQ_OUT", "prerequisite-versions.json")):
        monkeypatch.setenv(k, str(tmp_path / f))
    monkeypatch.setenv("PEP_RUN_TOKEN", "run-A")


def _outcome(fn, *args):
    """Run a real test function and report ("passed"|"skipped"|"failed", message), so an
    unexpected skip is never reported as a passing or skipped wiring test."""
    try:
        fn(*args)
    except pytest.skip.Exception as e:
        return "skipped", str(e)
    except (pytest.fail.Exception, Exception) as e:      # an assertion, pytest.fail or an error
        return "failed", str(e)
    return "passed", ""


# --------------------------------------------------------------------------- request and platform
def test_component_tests_share_the_integration_helper_instances(monkeypatch):
    # Patching package_management / pep_evidence must reach the shared install + identity steps.
    m = load(monkeypatch, _RPM)
    assert m.pep_integration_mode is m.pep_extension_checks.pep_integration_mode
    assert m.pep_request_env is m.pep_integration_mode.pep_request_env
    assert m._pr is m.pep_extension_checks._pr and m._pp is m.pep_extension_checks._pp
    assert m._pp is sys.modules["pep_package_policy"]
    assert m.package_management is m.pep_extension_checks.package_management
    assert m.package_management is m.pep_integration_mode.package_management


def test_lolor_policy_entry_is_pg_coupled_with_an_rpm_only_uncertified_companion(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    policy = m._pr.COMPONENT_POLICY["lolor"]
    assert m._pp.expected_names(policy, "rpm", "17", "9.8.7") == ("pgedge-lolor_17",)
    assert m._pp.expected_names(policy, "deb", "17", "9.8.7") == ("pgedge-postgresql-17-lolor",)
    assert m._pp.classify(policy, "pgedge-lolor_17-llvmjit", "rpm", "17", "9.8.7") == m._pp.R_UNCERTIFIED
    assert m._pp.classify(policy, "pgedge-lolor_17-llvmjit", "deb", "17", "9.8.7") == m._pp.R_FAMILY
    assert "lolor" not in m._pr.COMPONENT_PACKAGES


@pytest.mark.parametrize("over,reason", [
    ({"PEP_PACKAGE_NAME": "pgedge-lolor_16"}, "pg_major_mismatch"),
    ({"PEP_PACKAGE_NAME": "pgedge-postgresql-17-lolor"}, "package_family_mismatch"),
    ({"PEP_PACKAGE_NAME": "pgedge-lolor_17-llvmjit"}, "runtime_not_certified"),
    ({"PEP_PACKAGE_NAME": "pgedge-spock50_17"}, "package_not_allowed"),
], ids=["other-pg", "other-family", "llvmjit", "unrelated"])
def test_a_wrong_package_is_rejected_at_load_before_any_install(monkeypatch, over, reason):
    with pytest.raises(Exception, match=reason):
        load(monkeypatch, dict(_RPM, **over))


def test_a_deb_request_with_the_rpm_name_is_rejected_at_load(monkeypatch):
    with pytest.raises(Exception, match="package_family_mismatch"):
        load(monkeypatch, dict(_DEB, PEP_PACKAGE_NAME="pgedge-lolor_17"))


@pytest.mark.parametrize("env,containers", [
    (dict(_RPM, PLATFORM_FILTER="deb"), [("rocky9-amd64", "rhel")]),
    (dict(_DEB, PLATFORM_FILTER="rpm"), [("debian13-amd64", "deb")]),
    (dict(_STANDALONE, PLATFORM_FILTER="rpm"), [("rocky9-amd64", "rhel")]),
    (_STANDALONE, [("rocky9-amd64", "rhel"), ("debian13-amd64", "deb")]),
], ids=["rpm-request-over-deb-filter", "deb-request-over-rpm-filter", "standalone-filter", "standalone-all"])
def test_the_request_family_overrides_platform_filter(monkeypatch, env, containers):
    m = load(monkeypatch, env)
    assert m.all_containers == containers


def test_integration_package_is_the_request_and_standalone_keeps_config(monkeypatch):
    m = load(monkeypatch, _DEB)
    assert m.INTEGRATION_PROBLEM is None
    assert m.get_container_config("deb")["lolor_package"] == "pgedge-postgresql-17-lolor"
    assert m.base_extensions == ["lolor"] and m.check_extensions is True
    m = load(monkeypatch, dict(_STANDALONE, LOLOR_PACKAGE="custom-lolor", CHECK_EXTENSIONS="false"))
    assert m.get_container_config("rhel")["lolor_package"] == "custom-lolor"
    assert m.get_container_config("deb")["lolor_package"] == "pgedge-postgresql-17-lolor"
    assert m.check_extensions is False and m.lolor_version == "9.8.6"


def test_backstop_names_each_problem(monkeypatch):
    m = load(monkeypatch, _RPM)
    req = dict(m.INTEGRATION_REQUEST)
    assert m._integration_problem(req, "17") is None
    assert "PG_MAJOR_VERSION=16" in m._integration_problem(req, "16")
    assert "pg_major_mismatch" in m._integration_problem(dict(req, package_name="pgedge-lolor_16"), "17")
    assert "package_family_mismatch" in m._integration_problem(dict(req, family="deb"), "17")
    assert "runtime_not_certified" in m._integration_problem(dict(req, package_name="pgedge-lolor_17-llvmjit"), "17")
    assert "not the PG-coupled lolor" in m._integration_problem(dict(req, component="spock"), "17")
    assert m._integration_problem(None, "17") is None


# --------------------------------------------------------------------------- install
def test_server_companion_installs_first_then_the_pinned_request_and_versions_are_recorded(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_Server(m, installed=["pgedge-lolor_17", "pgedge-postgresql17-server"])))
    order = []
    ip = _Spy((True, "rhel", "installed"), order, "companion")
    pinned = _Spy((True, "ok", "f" * 64), order, "pinned")
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    monkeypatch.setattr(m.package_management, "query_installed_version",
                        lambda c, n: {"pgedge-lolor_17": "9.8.7-1.el9"}.get(n, "17.11-1PGEDGE.el9"))

    m.test_component_install("rocky9-amd64", "rhel")

    assert order == ["companion", "pinned"]
    assert ip.calls[0][1]["package_name"] == ["pgedge-postgresql17-server"]
    assert pinned.calls[0][0][1:] == ("pgedge-lolor_17", "9.8.7-1.el9")
    marker = json.loads((tmp_path / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "f" * 64
    prereq = json.loads((tmp_path / "prerequisite-versions.json").read_text())
    assert prereq["installed_versions"] == {"pgedge-postgresql17-server": "17.11-1PGEDGE.el9",
                                            "pgedge-lolor_17": "9.8.7-1.el9"}


def test_deb_companion_is_the_server_for_the_requested_major(monkeypatch, tmp_path):
    m = load(monkeypatch, _DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_Server(m, package="pgedge-postgresql-17-lolor")))
    ip = _Spy((True, "debian", "installed"))
    pinned = _Spy((True, "ok", "e" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    m.test_component_install("debian13-amd64", "deb")
    assert ip.calls[0][1]["package_name"] == ["pgedge-postgresql-17"]
    assert pinned.calls[0][0][1:] == ("pgedge-postgresql-17-lolor", "9.8.7-1.trixie")


def test_missing_server_is_a_prerequisite_failure_and_nothing_is_pinned(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_Server(m, installed=[])))
    pinned = _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package",
                        _Spy(Exception("No match for argument: pgedge-postgresql17-server")))
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    with pytest.raises(pytest.fail.Exception, match="PostgreSQL 17 prerequisite unavailable from the 'staging' channel"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_failed_pinned_install_fails_loudly_without_install_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_Server(m, installed=[])))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((False, "checksum mismatch", None)))
    with pytest.raises(AssertionError, match="pinned install of pgedge-lolor_17=9.8.7-1.el9 failed: checksum mismatch"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert not (tmp_path / "install-evidence.json").exists()


@pytest.mark.parametrize("present", [
    ("pgedge-lolor_17", "pgedge-lolor_17-llvmjit"),          # the uncertified JIT companion
    ("pgedge-lolor_17", "pgedge-lolor_16"),                  # another PG major beside the request
    ("pgedge-lolor_17", "pgedge-postgresql-17-lolor"),       # the other family's package
    ("pgedge-lolor_16",),                                    # the request replaced
    (),                                                      # nothing installed
], ids=["llvmjit", "other-pg", "other-family", "replaced", "none"])
def test_any_lolor_package_other_than_the_request_fails_the_install_step(monkeypatch, tmp_path, present):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_Server(m, installed=list(present) + ["pgedge-postgresql17-server"])))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((True, "ok", "f" * 64)))
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    with pytest.raises(AssertionError, match="expected exactly the requested lolor package 'pgedge-lolor_17'"):
        m.test_component_install("rocky9-amd64", "rhel")


def test_install_step_refuses_a_request_the_backstop_rejects(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_Server(m)))
    monkeypatch.setattr(m, "INTEGRATION_PROBLEM", "request PG17 disagrees with PG_MAJOR_VERSION=16")
    ip = _Spy((True, "rhel", "installed"))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    with pytest.raises(AssertionError, match="cannot be served"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert ip.calls == []


def test_standalone_install_is_unchanged(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    c = _Container(_Server(m))
    monkeypatch.setattr(m, "client", _Client(c))
    ip = _Spy((True, "rhel", "installed"))
    pinned = _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    m.test_component_install("rocky9-amd64", "rhel")
    m.test_component_install("debian13-amd64", "deb")
    assert pinned.calls == []
    assert [call[0] for call in ip.calls] == [(c, "pgedge-lolor_17"), (c, "pgedge-postgresql-17-lolor")]


# --------------------------------------------------------------------------- identity and versions
@pytest.mark.parametrize("observed,ok", [("9.8.7-1.el9", True), ("9.8.6-1.el9", False),
                                         ("9.8.7-rc1_1.el9", False)], ids=["exact", "older", "rc"])
def test_identity_proves_the_exact_requested_version(monkeypatch, tmp_path, observed, ok):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_Server(m)))
    m.pep_integration_mode.pep_evidence.write_install_evidence(
        m.INTEGRATION_REQUEST, "run-A", "pinned", "9.8.7-1.el9", str(tmp_path / "install-evidence.json"),
        installed_sha256="f" * 64)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: observed)
    if ok:
        m.test_lolor_identity("rocky9-amd64", "rhel")
    else:
        with pytest.raises(AssertionError, match="not proven"):
            m.test_lolor_identity("rocky9-amd64", "rhel")
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")


def test_identity_without_install_evidence_fails_with_persisted_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_Server(m)))
    with pytest.raises(pytest.fail.Exception, match="install-before-identity precondition failed"):
        m.test_lolor_identity("rocky9-amd64", "rhel")
    assert (tmp_path / "identity-evidence.json").exists()


def test_version_identity_and_upgrade_steps_by_mode(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    with pytest.raises(pytest.skip.Exception, match="integration mode only"):
        m.test_lolor_identity("rocky9-amd64", "rhel")
    c = _Container(_Server(m))
    monkeypatch.setattr(m, "client", _Client(c))
    verify = _Spy((True, "rhel", "9.8.6-1.el9", "ok"))
    monkeypatch.setattr(m.package_management, "verify_package_version", verify)
    m.test_component_package_version("rocky9-amd64", "rhel")          # standalone: the config version
    assert verify.calls == [((c, "pgedge-lolor_17", "9.8.6"), {})]

    m = load(monkeypatch, dict(_RPM, UPGRADE="true"))
    monkeypatch.setattr(m, "client", _Client(_Container(_Server(m))))
    with pytest.raises(pytest.skip.Exception, match="test_lolor_identity"):
        m.test_component_package_version("rocky9-amd64", "rhel")
    with pytest.raises(pytest.skip.Exception, match="never upgraded"):
        m.test_component_upgrade("rocky9-amd64", "rhel")


# --------------------------------------------------------------------------- binding
def test_binding_passes_for_the_requested_library_major_and_exact_version(monkeypatch):
    m = load(monkeypatch, _RPM)
    c = _Container(_Server(m))
    monkeypatch.setattr(m, "client", _Client(c))
    m.test_lolor_extension_binding("rocky9-amd64", "rhel")
    assert ["rpm", "-qf", "--queryformat", "%{NAME}\\n", "/usr/pgsql-17/lib/lolor.so"] in c.cmds
    assert any(cmd[0] == "psql" and "extversion" in cmd[-1] and "6433" in cmd for cmd in c.cmds)
    m = load(monkeypatch, _STANDALONE)
    with pytest.raises(pytest.skip.Exception, match="integration mode only"):
        m.test_lolor_extension_binding("rocky9-amd64", "rhel")


@pytest.mark.parametrize("over,msg", [
    ({"ext": {"6433": "9.8"}}, "lolor extension version is '9.8'"),
    ({"ext": {"6433": "9.8.70"}}, "lolor extension version is '9.8.70'"),
    ({"ext": {"6433": "9.8.7~rc1"}}, "lolor extension version is '9.8.7~rc1'"),
    ({"ext": {"6433": "9.8.7-rc.1"}}, "lolor extension version is '9.8.7-rc.1'"),
    ({"ext": {"6433": "9.8.7-test1"}}, "lolor extension version is '9.8.7-test1'"),
    ({"ext": {"6433": ""}}, "lolor extension version is ''"),
    ({"owner": "pgedge-lolor_16"}, "not the requested 'pgedge-lolor_17'"),
    ({"owner": "pgedge-lolor_17-llvmjit"}, "not the requested 'pgedge-lolor_17'"),
    ({"owner": ""}, "not the requested 'pgedge-lolor_17'"),
    ({"versions": {"6433": "160004"}}, "port 6433 runs PostgreSQL 160004"),
], ids=["prefix", "longer", "tilde-rc", "dash-rc", "test-suffix", "not-created", "owner-pg16",
        "owner-llvmjit", "unowned", "server-major"])
def test_binding_rejects_a_wrong_version_library_owner_or_server_major(monkeypatch, over, msg):
    m = load(monkeypatch, _RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_Server(m, **over))))
    with pytest.raises(AssertionError, match=re.escape(msg)):
        m.test_lolor_extension_binding("rocky9-amd64", "rhel")


# --------------------------------------------------------------------------- missing references
_NO_REFERENCE = Exception("No expected file found for lolor at /x/expected-output/rpm (checked: /x/a)")


def _bundled(monkeypatch, env, verify):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_Server(m))))
    monkeypatch.setattr(m.file_management, "verify_bundled_files", verify)
    return _outcome(m.test_verify_bundled_files, "rocky9-amd64", "rhel")


def _raises(exc):
    def verify(**kwargs):
        raise exc
    return verify


@pytest.mark.parametrize("env,outcome", [(_RPM, "failed"), (_STANDALONE, "skipped")],
                         ids=["integration-fails", "standalone-skips"])
def test_a_missing_expected_output_reference(monkeypatch, env, outcome):
    got, msg = _bundled(monkeypatch, env, _raises(_NO_REFERENCE))
    assert got == outcome and "No expected file found for lolor" in msg


@pytest.mark.parametrize("env", [_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_bundled_file_outcomes_are_unchanged_when_the_reference_exists(monkeypatch, env):
    assert _bundled(monkeypatch, env, lambda **k: (True, None, "match")) == ("passed", "")
    got, msg = _bundled(monkeypatch, env, lambda **k: (False, {"missing_files": ["/usr/x"]}, "Bundled files differ"))
    assert got == "failed" and "Missing files" in msg and "/usr/x" in msg
    got, msg = _bundled(monkeypatch, env, _raises(RuntimeError("rpm -ql failed")))
    assert got == "failed" and "rpm -ql failed" in msg


@pytest.mark.parametrize("env,outcome", [(_RPM, "failed"), (_STANDALONE, "failed")],
                         ids=["integration-fails", "standalone-fails"])
def test_missing_sbom_files(monkeypatch, env, outcome):
    m = load(monkeypatch, env)
    c = _Container(_Server(m, sbom_present=False))
    monkeypatch.setattr(m, "client", _Client(c))
    got, msg = _outcome(m.test_verify_sbom, "rocky9-amd64", "rhel")
    assert got == outcome and "lolor-sbom.json, lolor-sbom.json.asc not found in /usr/pgsql-17/sbom" in msg
    assert not any("sq verify" in str(cmd) or "wget" in str(cmd) for cmd in c.cmds)


@pytest.mark.parametrize("step", ["test_configure_repository", "test_verify_sbom", "test_init_cluster",
                                  "test_lolor_large_object_round_trip", "test_lolor_native_logical_replication",
                                  "test_stop_server"])
@pytest.mark.parametrize("env,outcome", [(_RPM, "failed"), (_STANDALONE, "skipped")],
                         ids=["integration-fails", "standalone-skips"])
def test_a_missing_container(monkeypatch, env, outcome, step):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(None))
    got, msg = _outcome(getattr(m, step), "rocky9-amd64", "rhel")
    assert got == outcome and "Container rocky9-amd64 not found" in msg


# --------------------------------------------------------------------------- stripped
@pytest.mark.parametrize("env", [_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_stripped_check_passes_only_for_a_stripped_elf_library(monkeypatch, env):
    m = load(monkeypatch, env)
    c = _Container(_Server(m))
    monkeypatch.setattr(m, "client", _Client(c))
    m.test_verify_binaries_stripped("rocky9-amd64", "rhel")
    assert ["file", "-b", "-L", "/usr/pgsql-17/lib/lolor.so"] in c.cmds
    for out in ("ELF 64-bit LSB shared object, x86-64, dynamically linked, not stripped",
                "ELF 64-bit LSB shared object, x86-64, dynamically linked, with debug_info, not stripped",
                "cannot open `/usr/pgsql-17/lib/lolor.so' (No such file or directory)"):
        monkeypatch.setattr(m, "client", _Client(_Container(_Server(m, file_out=out))))
        with pytest.raises(AssertionError, match="lolor.so"):
            m.test_verify_binaries_stripped("rocky9-amd64", "rhel")


def test_integration_stripped_check_uses_the_requested_package_files(monkeypatch):
    m = load(monkeypatch, dict(_RPM, RHEL_LOLOR_LIB="/config/lib"))
    c = _Container(_Server(m, files=["/opt/x/lib/lolor.so", "/opt/x/share/extension/lolor.control"]))
    monkeypatch.setattr(m, "client", _Client(c))
    m.test_verify_binaries_stripped("rocky9-amd64", "rhel")
    assert ["rpm", "-ql", "pgedge-lolor_17"] in c.cmds
    assert ["file", "-b", "-L", "/opt/x/lib/lolor.so"] in c.cmds
    monkeypatch.setattr(m, "client", _Client(_Container(_Server(m, files=["/opt/x/share/extension/lolor.control"]))))
    with pytest.raises(AssertionError, match="pgedge-lolor_17 ships no lolor.so"):
        m.test_verify_binaries_stripped("rocky9-amd64", "rhel")


# --------------------------------------------------------------------------- cluster settings
@pytest.mark.parametrize("env", [_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_the_cluster_sets_lolor_node(monkeypatch, env):
    m = load(monkeypatch, env)
    monkeypatch.setattr(m, "client", _Client(_Container(_Server(m))))
    init = _Spy((True, "", "ok"))
    monkeypatch.setattr(m.pg_server_management, "init_cluster", init)
    m.test_init_cluster("rocky9-amd64", "rhel")
    assert init.calls[0][0][1:] == ("/usr/pgsql-17/bin", "/tmp/n1", "postgres", {
        "shared_preload_libraries": "'lolor'", "wal_level": "logical", "max_replication_slots": "10",
        "max_wal_senders": "10", "lolor.node": "1"})


# --------------------------------------------------------------------------- SQL checks
def _sql_outcome(monkeypatch, env, step, **server):
    m = load(monkeypatch, env)
    s = _Server(m, **server)
    c = _Container(s)
    monkeypatch.setattr(m, "client", _Client(c))
    return _outcome(getattr(m, step), "rocky9-amd64", "rhel"), s, c, m


@pytest.mark.parametrize("env", [_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_the_lolor_checks_pass_on_a_healthy_server(monkeypatch, env):
    for step in ("test_lolor_replaces_lo_functions", "test_lolor_large_object_round_trip",
                 "test_lolor_node_must_be_set", "test_lolor_drop_restores_lo_functions"):
        (got, msg), s, c, m = _sql_outcome(monkeypatch, env, step)
        assert (got, msg) == ("passed", ""), step
    script = [cmd for cmd in c.cmds if _script(cmd)][0][2]
    assert "-v ON_ERROR_STOP=1" in script and " -X " in script and " -p 6433 " in script


@pytest.mark.parametrize("script", ["round_trip", "replication_write", "replica_read"])
def test_every_descriptor_is_used_inside_one_transaction(monkeypatch, script):
    # lolor's descriptors live until the end of their transaction (lolor_fsstubs.c), so every use of
    # one must sit between the script's BEGIN and COMMIT.
    m = load(monkeypatch, _STANDALONE)
    sql = {"round_trip": m.round_trip_sql, "replication_write": m.replication_write_sql,
           "replica_read": lambda: m.replica_read_sql(_REPL_LOID, 10)}[script]()
    lines = sql.splitlines()
    assert lines.count("BEGIN;") == 1 and lines.count("COMMIT;") == 1
    begin, commit = lines.index("BEGIN;"), lines.index("COMMIT;")
    used = 0
    for fn in ("lo_open(", "lowrite(", "loread(", "lo_tell(", "lo_lseek(", "lo_truncate(", "lo_close("):
        at = [i for i, line in enumerate(lines) if fn in line]
        used += len(at)
        assert all(begin < i < commit for i in at), fn
    assert used >= 3                                 # open, a read or write, and close at least


@pytest.mark.parametrize("over,msg", [
    ({"read": "00" + "ff" * 4999}, "read = "),
    ({"lo_get": "00"}, "lo_get = '00', expected"),
    ({"lolor_data": ""}, "lolor_data = ''"),
    ({"loid": str((12345 << 4) | 2)}, "carries node 2 in its low 4 bits, not lolor.node 1"),
    ({"loid": "24576"}, "carries node 0 in its low 4 bits"),
    ({"loid": "1"}, "has no counter above its node bits"),           # node 1 with a zero counter
    ({"session_node_oid": str((777 << 4) | 1)}, "carries node 1 in its low 4 bits, not lolor.node 15"),
    ({"core_rows_for_loid": "1,1"}, "core_rows_for_loid = '1,1'"),
    ({"core_totals_after": "1,1"}, "pg_catalog large-object row counts changed from '0,0' to '1,1'"),
    ({"rows_after_unlink": "1,0"}, "rows_after_unlink = '1,0'"),
    ({"unlink": "0"}, "unlink = '0'"),
    ({"written": "4999"}, "written = '4999'"),
    ({"tell_after_write": "0"}, "tell_after_write = '0'"),
    ({"size_after_truncate": "5000"}, "size_after_truncate = '5000'"),
    ({"lolor_pages": "3"}, "lolor_pages = '3', expected '2'"),
    ({"lolor_meta_rows": "0"}, "lolor_meta_rows = '0'"),
    ({"explicit_oid": "200001"}, "explicit_oid = '200001'"),
    ({"node": "0"}, "node = '0', expected '1'"),
    ({"close": None}, "close = None"),
], ids=["read", "lo_get", "lolor-data", "other-node", "unencoded", "counter-zero", "session-node",
        "core-rows", "core-totals", "unlink-rows", "unlink-result", "written", "tell", "truncate",
        "pages", "metadata", "explicit-oid", "conf-node", "missing-value"])
def test_a_wrong_round_trip_value_fails(monkeypatch, over, msg):
    m = load(monkeypatch, _STANDALONE)
    (got, text), _s, _c, _m = _sql_outcome(
        monkeypatch, _STANDALONE, "test_lolor_large_object_round_trip",
        scripts={"large-object round trip": (0, _round_trip(m, **over))})
    assert got == "failed" and msg in text, text


@pytest.mark.parametrize("step,label", [
    ("test_lolor_large_object_round_trip", "large-object round trip"),
    ("test_lolor_replaces_lo_functions", "large-object functions after CREATE EXTENSION"),
    ("test_lolor_drop_restores_lo_functions", "DROP EXTENSION lolor"),
    ("test_lolor_node_must_be_set", "lolor.node boot value"),
])
def test_a_sql_error_fails_the_check_even_after_printing_good_values(monkeypatch, step, label):
    m = load(monkeypatch, _STANDALONE)
    good = {"large-object round trip": _round_trip(m), "lolor.node boot value": "boot=0\n"}.get(label, "")
    error = good + "psql:<stdin>:9: ERROR:  invalid large-object descriptor: 0\n"
    (got, text), _s, _c, _m = _sql_outcome(monkeypatch, _STANDALONE, step, scripts={label: (3, error)})
    assert got == "failed" and "psql exited 3" in text and "invalid large-object descriptor" in text


@pytest.mark.parametrize("scripts,msg", [
    ({"lo_creat with lolor.node 0": (0, "")}, "must fail with 'value for lolor.node is not set'"),
    ({"lo_creat with lolor.node 0": (3, "ERROR:  permission denied for large object 17")},
     "must fail with 'value for lolor.node is not set'"),
    ({"lolor.node out of range": (0, "")}, "SET lolor.node = 17 must be rejected"),
    ({"lolor.node out of range": (3, 'ERROR:  unrecognized configuration parameter "lolor.node"')},
     "SET lolor.node = 17 must be rejected"),
    ({"lolor.node boot value": (0, "boot=1\n")}, "lolor.node boot value is '1'"),
], ids=["zero-accepted", "other-error", "17-accepted", "17-other-error", "boot-value"])
def test_the_node_checks_fail_when_lolor_does_not_refuse(monkeypatch, scripts, msg):
    (got, text), _s, _c, _m = _sql_outcome(monkeypatch, _STANDALONE, "test_lolor_node_must_be_set", scripts=scripts)
    assert got == "failed" and msg in text, text


def _drop(lines, sig_prefix):
    return [line for line in lines if not line.startswith("fn=" + sig_prefix)]


@pytest.mark.parametrize("mutate,msg", [
    (lambda m, L: _drop(L, "lo_open("), "missing lo_open(oid,integer) as lolor:lolor_lo_open"),
    (lambda m, L: _drop(L, "lo_get_orig(oid,bigint,integer)"), "missing lo_get_orig(oid,bigint,integer) as built-in"),
    (lambda m, L: [x.replace("|lolor_lo_get_fragment", "|lolor_lo_get") for x in L],
     "missing lo_get(oid,bigint,integer) as lolor:lolor_lo_get_fragment"),
    (lambda m, L: [x.replace("fn=lo_unlink(oid)|c|$libdir/lolor", "fn=lo_unlink(oid)|c|$libdir/other") for x in L],
     "unexpected lo_unlink(oid) as other:c:$libdir/other"),
    (lambda m, L: L + ["fn=lolor_lo_open(oid,integer)|c|$libdir/lolor|lolor_lo_open"],
     "unexpected lolor_lo_open(oid,integer) as lolor:lolor_lo_open"),
    (lambda m, L: [x.replace("fn=lo_close_orig(integer)|internal|", "fn=lo_close_orig(integer)|sql|") for x in L],
     "unexpected lo_close_orig(integer) as other:sql"),
], ids=["not-replaced", "orig-missing", "wrong-symbol", "wrong-library", "disabled-leftover", "orig-not-builtin"])
def test_a_wrong_function_swap_after_create_fails(monkeypatch, mutate, msg):
    m = load(monkeypatch, _STANDALONE)
    lines = mutate(m, _fn_lines(m))
    (got, text), _s, _c, _m = _sql_outcome(
        monkeypatch, _STANDALONE, "test_lolor_replaces_lo_functions",
        scripts={"large-object functions after CREATE EXTENSION": (0, "\n".join(lines) + "\n")})
    assert got == "failed" and msg in text, text


@pytest.mark.parametrize("extra,keys,msg", [
    (["fn=lo_open_orig(oid,integer)|internal||be_lo_open"], {}, "unexpected lo_open_orig(oid,integer) as built-in"),
    (["fn=lolor_lo_creat(integer)|c|$libdir/lolor|lolor_lo_creat"], {}, "unexpected lolor_lo_creat(integer)"),
    ([], {"lolor_tables": 1}, "lolor_tables = '1'"),
    ([], {"event_trigger": 1}, "event_trigger = '1'"),
    ([], {"core_meta": 0}, "core_meta = '0'"),
], ids=["orig-left", "lolor-left", "tables-left", "trigger-left", "builtin-not-used"])
def test_a_drop_that_leaves_lolor_behind_fails(monkeypatch, extra, keys, msg):
    m = load(monkeypatch, _STANDALONE)
    state = dict(lolor_tables=0, event_trigger=0, core_meta=1, core_unlink=1)
    state.update(keys)
    text = "\n".join(_fn_lines(m, enabled=False) + extra) + "\n" + _labelled(**state)
    (got, out), _s, _c, _m = _sql_outcome(monkeypatch, _STANDALONE, "test_lolor_drop_restores_lo_functions",
                                          scripts={"DROP EXTENSION lolor": (0, text)})
    assert got == "failed" and msg in out, out


def test_the_drop_still_lists_the_builtin_in_place_of_a_missing_lolor_function(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    lines = _drop(_fn_lines(m, enabled=False), "lo_put(")
    (got, out), _s, _c, _m = _sql_outcome(
        monkeypatch, _STANDALONE, "test_lolor_drop_restores_lo_functions",
        scripts={"DROP EXTENSION lolor": (0, "\n".join(lines) + "\n" + _labelled(
            lolor_tables=0, event_trigger=0, core_meta=1, core_unlink=1))})
    assert got == "failed" and "missing lo_put(oid,bigint,bytea) as built-in" in out


# --------------------------------------------------------------------------- native logical replication
class _Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


_REPL_LOID = (4242 << 4) | 1


def _replication(m, polls=None, read=None, write=None, **scripts):
    answers = iter(polls if polls is not None else [
        (0, _labelled(rows="0,0", bytes="")),
        (0, _labelled(rows="1,2", bytes=m.REPLICATION_PAYLOAD.hex()))])
    last = {}

    def poll():
        last["answer"] = next(answers, last.get("answer"))
        return last["answer"]
    base = {
        "wait for the initial table sync": (0, _labelled(ready="2/2")),
        "write a large object on the publisher": write or (0, _labelled(
            written=len(m.REPLICATION_PAYLOAD), close=0, loid=_REPL_LOID)),
        "poll the subscriber": poll,
        "read on the subscriber": read or (0, _labelled(
            read=m.REPLICATION_PAYLOAD.hex(), close=0, core_rows_for_loid="0,0", node=2)),
    }
    base.update(scripts)
    return base


def _run_replication(monkeypatch, env, scripts=None, stop=None, **server):
    m = load(monkeypatch, env)
    s = _Server(m, scripts=scripts(m) if scripts else _replication(m), **server)
    c = _Container(s)
    monkeypatch.setattr(m, "client", _Client(c))
    init = _Spy((True, "", "initialized"))
    start = _Spy((True, "", "started"))
    stop = stop or _Spy((True, "", "stopped"))
    clock = _Clock()
    monkeypatch.setattr(m.pg_server_management, "init_cluster", init)
    monkeypatch.setattr(m.pg_server_management, "start_server", start)
    monkeypatch.setattr(m.pg_server_management, "stop_server", stop)
    monkeypatch.setattr(m, "time", clock)
    got = _outcome(m.test_lolor_native_logical_replication, "rocky9-amd64", "rhel")
    return got, m, s, c, init, start, stop, clock


@pytest.mark.parametrize("env", [_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_replication_reads_identical_bytes_on_the_subscriber_and_cleans_up(monkeypatch, env):
    got, m, s, c, init, start, stop, clock = _run_replication(monkeypatch, env)
    assert got == ("passed", "")
    assert init.calls[0][0][2] == "/tmp/n1_lolor_subscriber" and init.calls[0][0][4]["lolor.node"] == "2"
    assert start.calls[0][0][2:4] == ("/tmp/n1_lolor_subscriber", "6434")
    assert stop.calls[0][0][2:4] == ("/tmp/n1_lolor_subscriber", "6434")
    labels = [label for label, _port in s.seen]
    assert labels == ["create lolor on the subscriber", "create the publication", "create the subscription",
                      "wait for the initial table sync",
                      "write a large object on the publisher", "poll the subscriber", "poll the subscriber",
                      "read on the subscriber", "drop the subscription", "drop the publication"]
    assert not any(cmd[0] == "tail" for cmd in c.cmds)             # no log dump on success
    ports = dict(s.seen)
    assert ports["create the publication"] == ports["write a large object on the publisher"] == "6433"
    assert ports["create the subscription"] == ports["read on the subscriber"] == "6434"
    sqls = {_script(cmd)[0]: _script(cmd)[2] for cmd in c.cmds if _script(cmd)}
    assert "FOR TABLE lolor.pg_largeobject, lolor.pg_largeobject_metadata" in sqls["create the publication"]
    assert ("'host=localhost port=6433 dbname=postgres user=postgres connect_timeout=10'"
            in sqls["create the subscription"])
    assert ["rm", "-rf", "/tmp/n1_lolor_subscriber"] in c.cmds
    assert clock.sleeps == [m.REPLICATION_POLL_S]
    binding = any(cmd[0] == "psql" and "extversion" in cmd[-1] and "6434" in cmd for cmd in c.cmds)
    assert binding is (env is _RPM)                 # the subscriber's lolor is bound in integration mode


def test_replication_timeout_fails_with_a_clear_message_and_still_cleans_up(monkeypatch):
    got, m, s, c, init, start, stop, clock = _run_replication(
        monkeypatch, _STANDALONE, scripts=lambda m: _replication(m, polls=[
            (0, _labelled(rows="1,1", bytes=m.REPLICATION_PAYLOAD[:2048].hex()))]))
    outcome, msg = got
    assert outcome == "failed"
    assert (f"large object {_REPL_LOID} written on the publisher (port 6433) was not readable with identical "
            f"bytes on the subscriber (port 6434) within 90s of native logical replication of "
            f"lolor.pg_largeobject, lolor.pg_largeobject_metadata") in msg
    assert "last subscriber state: metadata,page rows = '1,1'; 2048 bytes readable, not the 4500 bytes written" in msg
    assert clock.now >= m.REPLICATION_TIMEOUT_S and len(clock.sleeps) <= m.REPLICATION_TIMEOUT_S // m.REPLICATION_POLL_S + 1
    assert stop.calls and ["rm", "-rf", "/tmp/n1_lolor_subscriber"] in c.cmds
    assert [label for label, _p in s.seen][-2:] == ["drop the subscription", "drop the publication"]


def test_nothing_is_written_until_both_tables_finished_their_initial_sync(monkeypatch):
    states = iter([(0, _labelled(ready="0/2")), (0, _labelled(ready="1/2")), (0, _labelled(ready="2/2"))])
    got, m, s, c, init, start, stop, clock = _run_replication(
        monkeypatch, _STANDALONE,
        scripts=lambda m: _replication(m, **{"wait for the initial table sync": lambda: next(states)}))
    assert got == ("passed", "")
    labels = [label for label, _p in s.seen]
    assert labels.count("wait for the initial table sync") == 3
    assert labels.index("write a large object on the publisher") > max(
        i for i, label in enumerate(labels) if label == "wait for the initial table sync")


def test_an_initial_sync_that_never_finishes_fails_shows_the_log_and_cleans_up(monkeypatch, capsys):
    got, m, s, c, init, start, stop, clock = _run_replication(
        monkeypatch, _STANDALONE,
        scripts=lambda m: _replication(m, **{"wait for the initial table sync": (0, _labelled(ready="1/2"))}))
    assert got[0] == "failed"
    assert ("subscription lolor_pep_sub on port 6434 did not finish the initial sync of lolor.pg_largeobject, "
            "lolor.pg_largeobject_metadata within 90s (tables ready/total = '1/2'); nothing was written") in got[1]
    assert "write a large object on the publisher" not in [label for label, _p in s.seen]
    tail = ["tail", "-n", "40", "/tmp/n1_lolor_subscriber/logfile"]
    assert tail in c.cmds and c.cmds.index(tail) < c.cmds.index(["rm", "-rf", "/tmp/n1_lolor_subscriber"])
    assert "subscriber log, last 40 lines of /tmp/n1_lolor_subscriber/logfile" in capsys.readouterr().out
    assert stop.calls


def test_cleanup_problems_are_reported_even_when_the_check_already_failed(monkeypatch, capsys):
    got, *_rest = _run_replication(
        monkeypatch, _STANDALONE, stop=_Spy(Exception("pg_ctl stop failed: no server")),
        scripts=lambda m: _replication(m, write=(3, "ERROR:  value for lolor.node is not set")))
    assert got[0] == "failed" and "write a large object on the publisher: psql exited 3" in got[1]
    out = capsys.readouterr().out
    assert "replication cleanup also failed" in out and "pg_ctl stop failed" in out


# --------------------------------------------------------------------------- bounded SQL steps
@pytest.mark.parametrize("label,limit,statement_ms", [
    ("create the subscription", 60, 55000), ("write a large object on the publisher", 60, 55000),
    ("drop the subscription", 60, 55000), ("drop the publication", 60, 55000),
    ("wait for the initial table sync", 15, 10000), ("poll the subscriber", 15, 10000),
], ids=["subscribe", "write", "drop-sub", "drop-pub", "sync-poll", "data-poll"])
def test_every_replication_sql_step_is_bounded(monkeypatch, label, limit, statement_ms):
    # psql runs under timeout(1), with a connect timeout and a server-side statement timeout, so a
    # blocked connection, subscription, poll or cleanup step cannot wait for the job's time limit.
    got, m, s, c, *_rest = _run_replication(monkeypatch, _STANDALONE)
    assert got == ("passed", "")
    [script] = [cmd[2] for cmd in c.cmds if _script(cmd) and _script(cmd)[0] == label][:1]
    head = script.split("<<'LOLOR_EOSQL'")[0]
    assert head.startswith("PGCONNECT_TIMEOUT=10 PGOPTIONS='-c statement_timeout=%d' timeout -k 5 %d "
                           % (statement_ms, limit)) and "/psql " in head, head


def test_every_other_lolor_script_is_bounded_too(monkeypatch):
    for step in ("test_lolor_replaces_lo_functions", "test_lolor_large_object_round_trip",
                 "test_lolor_node_must_be_set", "test_lolor_drop_restores_lo_functions"):
        (got, _msg), s, c, m = _sql_outcome(monkeypatch, _STANDALONE, step)
        scripts = [cmd[2] for cmd in c.cmds if _script(cmd)]
        assert scripts and all(" timeout -k 5 60 " in x and x.startswith("PGCONNECT_TIMEOUT=10 ")
                               for x in scripts), step


@pytest.mark.parametrize("label", ["create the subscription", "write a large object on the publisher",
                                   "read on the subscriber"])
def test_a_step_that_hits_its_limit_fails_clearly_and_still_cleans_up(monkeypatch, label):
    got, m, s, c, init, start, stop, clock = _run_replication(
        monkeypatch, _STANDALONE, scripts=lambda m: _replication(m, **{label: (124, "")}))
    assert got[0] == "failed" and f"[{label}: timed out after 60s on port " in got[1], got[1]
    assert stop.calls and ["rm", "-rf", "/tmp/n1_lolor_subscriber"] in c.cmds
    assert [label for label, _p in s.seen][-2:] == ["drop the subscription", "drop the publication"]


def test_a_poll_that_hits_its_limit_counts_as_a_failed_poll_until_the_deadline(monkeypatch):
    got, m, s, c, init, start, stop, clock = _run_replication(
        monkeypatch, _STANDALONE, scripts=lambda m: _replication(m, polls=[(124, "")]))
    assert got[0] == "failed"
    assert "last subscriber state: psql exited 124: [poll the subscriber: timed out after 15s on port 6434]" in got[1]
    assert stop.calls


def test_a_cleanup_step_that_hits_its_limit_is_reported_and_the_rest_still_runs(monkeypatch):
    got, m, s, c, init, start, stop, clock = _run_replication(
        monkeypatch, _STANDALONE, scripts=lambda m: _replication(m, **{"drop the subscription": (137, "")}))
    assert got[0] == "failed" and "replication cleanup failed" in got[1]
    assert "DROP SUBSCRIPTION on port 6434 failed: [drop the subscription: timed out after 60s on port 6434]" in got[1]
    assert stop.calls and ["rm", "-rf", "/tmp/n1_lolor_subscriber"] in c.cmds
    assert [label for label, _p in s.seen][-1] == "drop the publication"


def test_replication_timeout_reports_a_subscriber_that_keeps_failing(monkeypatch):
    got, *_rest = _run_replication(monkeypatch, _STANDALONE, scripts=lambda m: _replication(m, polls=[
        (3, 'psql:<stdin>:1: ERROR:  relation "lolor.pg_largeobject_metadata" does not exist')]))
    assert got[0] == "failed" and "last subscriber state: psql exited 3" in got[1]


@pytest.mark.parametrize("scripts,msg", [
    (lambda m: _replication(m, read=(0, _labelled(read="00", close=0, core_rows_for_loid="0,0", node=2))),
     "reading on the subscriber: read = '00'"),
    (lambda m: _replication(m, read=(0, _labelled(read=m.REPLICATION_PAYLOAD.hex(), close=0,
                                                  core_rows_for_loid="1,1", node=2))),
     "core_rows_for_loid = '1,1'"),
    (lambda m: _replication(m, read=(0, _labelled(read=m.REPLICATION_PAYLOAD.hex(), close=0,
                                                  core_rows_for_loid="0,0", node=1))),
     "node = '1', expected '2'"),
    (lambda m: _replication(m, write=(0, _labelled(written=4500, close=0, loid=(4242 << 4) | 2))),
     "writing on the publisher: large object OID"),
    (lambda m: _replication(m, write=(3, "ERROR:  value for lolor.node is not set")),
     "write a large object on the publisher: psql exited 3"),
    (lambda m: _replication(m, **{"create the subscription": (3, "ERROR:  could not connect to the publisher")}),
     "create the subscription: psql exited 3"),
], ids=["bytes", "core-rows", "subscriber-node", "publisher-node", "write-error", "subscribe-error"])
def test_replication_fails_on_a_wrong_value_or_sql_error_and_still_cleans_up(monkeypatch, scripts, msg):
    got, m, s, c, init, start, stop, clock = _run_replication(monkeypatch, _STANDALONE, scripts=scripts)
    assert got[0] == "failed" and msg in got[1], got[1]
    assert stop.calls and [label for label, _p in s.seen][-2:] == ["drop the subscription", "drop the publication"]


def test_a_failed_cleanup_fails_an_otherwise_passing_replication_check(monkeypatch):
    got, *_rest = _run_replication(monkeypatch, _STANDALONE, stop=_Spy(Exception("pg_ctl stop failed: no server")),
                                   scripts=lambda m: _replication(m, **{"drop the publication": (1, "boom")}))
    assert got[0] == "failed" and "replication cleanup failed" in got[1]
    assert "stopping the subscriber cluster on port 6434 failed: pg_ctl stop failed" in got[1]
    assert "DROP PUBLICATION on port 6433 failed: boom" in got[1]


def test_a_subscriber_that_never_started_is_not_stopped(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    s = _Server(m)
    c = _Container(s)
    monkeypatch.setattr(m, "client", _Client(c))
    stop = _Spy((True, "", "stopped"))
    monkeypatch.setattr(m.pg_server_management, "init_cluster", _Spy(Exception("Initdb failed: disk full")))
    monkeypatch.setattr(m.pg_server_management, "stop_server", stop)
    got, msg = _outcome(m.test_lolor_native_logical_replication, "rocky9-amd64", "rhel")
    assert got == "failed" and "Initdb failed" in msg
    assert stop.calls == [] and ["rm", "-rf", "/tmp/n1_lolor_subscriber"] in c.cmds
    assert [label for label, _p in s.seen] == ["drop the publication"]
