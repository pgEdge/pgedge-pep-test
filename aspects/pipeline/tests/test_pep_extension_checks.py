"""Docker-free tests for the shared extension checks (aspects/pipeline/pep_extension_checks.py).

The helper is driven with a fake container and spied package helpers, as in
test_pep_spock_wiring.py. Spock is the only PG-coupled component every checkout has, so its policy
entry stands in for "a PG-coupled extension"; nothing here is Spock-specific. Covers: the server
companion installed before the pinned package; prerequisite and pinned-install failures; exactly the
requested package installed (an uncertified companion or another PG major or family fails); version
facts recorded; the library owner, server major and extension version checks; the two extension
version rules; and the strict stripped check.
"""
import importlib.util
import json
import os
import sys

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "pep_extension_checks.py")
_spec = importlib.util.spec_from_file_location("pep_extension_checks_under_test", _PATH)
X = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(X)

_RAW = {"component": "spock", "package_name": "pgedge-spock50_17", "family": "rpm", "pg_major": "17",
        "expected_version": "5.0.13", "expected_rpm": "5.0.13-1.el9", "expected_buildnum": "1",
        "channel": "staging", "arch": "amd64", "container_alias": "rocky9-amd64"}
REQ_RPM = X._pr.normalize_request(_RAW)       # real normalized requests, as the component tests get them
REQ_DEB = X._pr.normalize_request(dict({k: v for k, v in _RAW.items() if k != "expected_rpm"},
                                       package_name="pgedge-postgresql-17-spock50", family="deb",
                                       expected_deb="5.0.13-1.trixie", container_alias="debian13-amd64"))


class _Container:
    status = "running"

    def __init__(self, respond):
        self.respond, self.cmds = respond, []

    def exec_run(self, cmd, user=None):
        self.cmds.append(cmd)
        ec, text = self.respond(cmd)
        return ec, text.encode()


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


def _pkg_list(*names, status="install ok installed"):
    def respond(cmd):
        if cmd[:2] == ["rpm", "-qa"]:
            return 0, "\n".join(names) + "\n"
        if cmd[0] == "dpkg-query" and cmd[1] == "-W":
            return 0, "".join("%s %s\n" % (n, status) for n in names)
        return 0, ""
    return respond


@pytest.fixture
def wired(monkeypatch, tmp_path):
    for k, f in (("PEP_INSTALL_OUT", "install-evidence.json"), ("PEP_PREREQ_OUT", "prerequisite-versions.json")):
        monkeypatch.setenv(k, str(tmp_path / f))
    monkeypatch.setenv("PEP_RUN_TOKEN", "run-A")
    return tmp_path


def test_shares_one_instance_of_the_integration_helpers():
    from aspects import package_management
    assert X.package_management is package_management
    assert X.package_management is X.pep_integration_mode.package_management
    assert X._pp is sys.modules["pep_package_policy"]


@pytest.mark.parametrize("ctype,want", [("rhel", ["pgedge-postgresql17-server"]),
                                        ("deb", ["pgedge-postgresql-17"])])
def test_server_companion_is_the_server_for_the_requested_major(ctype, want):
    assert X.server_companions(ctype, "17") == want


# --------------------------------------------------------------------------- installed set
def test_installed_set_counts_every_package_of_the_component_and_nothing_else():
    c = _Container(_pkg_list("pgedge-spock50_17", "pgedge-spock50_17-llvmjit", "pgedge-spock50_16",
                             "pgedge-postgresql-17-spock50", "pgedge-spock50_17-debuginfo",
                             "pgedge-postgresql17-server", "pgedge-rag-server2"))
    assert X.installed_component_packages(c, "rhel", REQ_RPM) == [
        "pgedge-postgresql-17-spock50", "pgedge-spock50_16", "pgedge-spock50_17", "pgedge-spock50_17-llvmjit"]


def test_installed_set_ignores_deb_packages_that_are_not_installed():
    c = _Container(_pkg_list("pgedge-postgresql-16-spock50", status="deinstall ok config-files"))
    assert X.installed_component_packages(c, "deb", REQ_DEB) == []


def test_installed_set_is_none_when_the_query_fails():
    assert X.installed_component_packages(_Container(lambda cmd: (1, "boom")), "rhel", REQ_RPM) is None


# --------------------------------------------------------------------------- install
def test_companion_first_then_the_pinned_request_and_versions_are_recorded(monkeypatch, wired):
    order = []
    ip = _Spy((True, "rhel", "installed"), order, "companion")
    pinned = _Spy((True, "ok", "f" * 64), order, "pinned")
    monkeypatch.setattr(X.package_management, "install_package", ip)
    monkeypatch.setattr(X.package_management, "install_pinned", pinned)
    monkeypatch.setattr(X.package_management, "query_installed_version",
                        lambda c, n: {"pgedge-spock50_17": "5.0.13-1.el9"}.get(n, "17.11-1PGEDGE.el9"))

    X.install_requested_extension(_Container(_pkg_list("pgedge-spock50_17")), "rhel", REQ_RPM, "17", "staging")

    assert order == ["companion", "pinned"]
    assert ip.calls[0][1] == {"container": ip.calls[0][1]["container"], "package_name": ["pgedge-postgresql17-server"],
                              "pg_major_version": "17", "install_pg_server": False}
    assert pinned.calls[0][0][1:] == ("pgedge-spock50_17", "5.0.13-1.el9")
    marker = json.loads((wired / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "f" * 64
    assert json.loads((wired / "prerequisite-versions.json").read_text()) == {
        "channel": "staging", "pg_major": "17", "requested_package": "pgedge-spock50_17",
        "installed_versions": {"pgedge-postgresql17-server": "17.11-1PGEDGE.el9",
                               "pgedge-spock50_17": "5.0.13-1.el9"}}


@pytest.mark.parametrize("ret", [Exception("No match for argument: pgedge-postgresql17-server"),
                                 (False, "rhel", "No match")], ids=["raises", "returns-false"])
def test_missing_server_is_a_prerequisite_failure_and_nothing_is_pinned(monkeypatch, wired, ret):
    pinned = _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(X.package_management, "install_package", _Spy(ret))
    monkeypatch.setattr(X.package_management, "install_pinned", pinned)
    with pytest.raises(pytest.fail.Exception,
                       match="PostgreSQL 17 prerequisite unavailable from the 'staging' channel"):
        X.install_requested_extension(_Container(_pkg_list()), "rhel", REQ_RPM, "17", "staging")
    assert pinned.calls == [] and not (wired / "install-evidence.json").exists()


def test_failed_pinned_install_fails_without_install_evidence(monkeypatch, wired):
    monkeypatch.setattr(X.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(X.package_management, "install_pinned", _Spy((False, "checksum mismatch", None)))
    with pytest.raises(AssertionError, match="pinned install of pgedge-spock50_17=5.0.13-1.el9 failed"):
        X.install_requested_extension(_Container(_pkg_list()), "rhel", REQ_RPM, "17", "staging")
    assert not (wired / "install-evidence.json").exists()


@pytest.mark.parametrize("present", [
    ("pgedge-spock50_17", "pgedge-spock50_17-llvmjit"),      # an uncertified companion
    ("pgedge-spock50_17", "pgedge-spock50_16"),              # another PG major
    ("pgedge-spock50_16",),                                  # the request replaced
    (),                                                      # nothing installed
], ids=["llvmjit", "other-pg", "replaced", "none"])
def test_anything_but_exactly_the_requested_package_fails(monkeypatch, wired, present):
    monkeypatch.setattr(X.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(X.package_management, "install_pinned", _Spy((True, "ok", "f" * 64)))
    monkeypatch.setattr(X.package_management, "query_installed_version", lambda c, n: "x")
    with pytest.raises(AssertionError, match="expected exactly the requested spock package"):
        X.install_requested_extension(_Container(_pkg_list(*present)), "rhel", REQ_RPM, "17", "staging")


# --------------------------------------------------------------------------- version rules
@pytest.mark.parametrize("observed,ok", [
    ("2.6.1", True), ("2.6", False), ("2.6.10", False), ("2.6.1.0", False), ("2.6.1-test1", False),
    ("2.6.1~rc1", False), ("2.6.1-rc.1", False), (" 2.6.1", False), ("", False), (None, False)])
def test_exact_version_accepts_only_the_expected_string(observed, ok):
    assert (X.exact_version("2.6.1")(observed) is None) is ok


@pytest.mark.parametrize("observed,ok", [
    ("1.1", True), ("1.1-alpha1", True), ("1.1-rc.1", True), ("1.1-rc-1", True), ("1.1-beta_2", True),
    ("1.10", False), ("1.1.0", False), ("1.2", False), ("1", False), ("1.1-", False), ("11", False),
    ("1.1~beta1", False), ("1.1.0-alpha1", False), ("-1.1", False), ("", False), (None, False)])
def test_series_version_mirrors_the_release_workflows_series_rule(observed, ok):
    assert (X.series_version("1.1")(observed) is None) is ok


# --------------------------------------------------------------------------- server binding
def _server(owner="pgedge-spock50_17", versions=("170011", "170011"), ext=("5.0.13", "5.0.13"),
            libdir="/usr/pgsql-17/lib"):
    ports = {"5432": 0, "5433": 1}

    def respond(cmd):
        if cmd[0] == "psql":
            port, sql = cmd[cmd.index("-p") + 1], cmd[-1]
            if "PKGLIBDIR" in sql:
                return 0, libdir + "\n"
            if "server_version_num" in sql:
                return 0, versions[ports[port]] + "\n"
            if "extversion" in sql:
                return 0, ext[ports[port]] + "\n"
        if cmd[:2] == ["rpm", "-qf"]:
            return (0, owner + "\n") if owner else (1, "file %s is not owned by any package" % cmd[-1])
        if cmd[:2] == ["dpkg-query", "-S"]:
            return (0, "%s: %s\n" % (owner, cmd[2])) if owner else (1, "no path found matching pattern")
        return 1, "unexpected %r" % (cmd,)
    return respond


def _verify(container, ctype="rhel", req=REQ_RPM, ports=(5432, 5433), check=None):
    return X.verify_requested_extension(container, ctype, req, ports=list(ports), pguser="postgres",
                                        library="spock.so", extname="spock", pg_major="17",
                                        version_problem=check or X.exact_version("5.0.13"))


def test_binding_passes_for_the_requested_library_major_and_version():
    c = _Container(_server())
    assert _verify(c) == {5432: ("170011", "5.0.13"), 5433: ("170011", "5.0.13")}
    assert ["rpm", "-qf", "--queryformat", "%{NAME}\\n", "/usr/pgsql-17/lib/spock.so"] in c.cmds
    _verify(_Container(_server(owner="pgedge-postgresql-17-spock50", libdir="/usr/lib/postgresql/17/lib")),
            ctype="deb", req=REQ_DEB)


@pytest.mark.parametrize("over,msg", [
    ({"owner": "pgedge-spock50_16"}, "not the requested"),
    ({"owner": "pgedge-spock50_17\npgedge-other"}, "not the requested"),
    ({"owner": ""}, "not the requested"),
    ({"libdir": "ERROR: relation does not exist"}, "PKGLIBDIR query failed"),
    ({"versions": ("170011", "160004")}, "port 5433 runs PostgreSQL 160004"),
    ({"versions": ("170011", "")}, "server_version_num query failed on port 5433"),
    ({"ext": ("5.0.13", "5.0.12")}, "port 5433: spock extension version is '5.0.12'"),
    ({"ext": ("", "5.0.13")}, "port 5432: spock extension version is ''"),
], ids=["owner", "two-owners", "unowned", "libdir", "pg-major", "pg-query", "extversion", "not-created"])
def test_binding_rejects_a_wrong_library_major_or_version(over, msg):
    with pytest.raises(AssertionError, match=msg):
        _verify(_Container(_server(**over)))


def test_deb_owner_parsing_handles_diversions_and_shared_paths():
    path = "/usr/lib/postgresql/17/lib/x.so"
    c = _Container(lambda cmd: (0, "diversion by a from: %s\na, b:amd64: %s\n" % (path, path)))
    assert X.library_owner(c, "deb", path)[0] == ["a", "b"]


# --------------------------------------------------------------------------- stripped
@pytest.mark.parametrize("ec,out,problem", [
    (0, "ELF 64-bit LSB shared object, x86-64, version 1 (SYSV), dynamically linked, stripped", None),
    (0, "ELF 64-bit LSB shared object, ARM aarch64, dynamically linked, BuildID[sha1]=ab, not stripped", "not stripped"),
    (0, "ELF 64-bit LSB executable, x86-64, statically linked, with debug_info, not stripped", "not stripped"),
    (0, "ELF 64-bit LSB shared object, x86-64, dynamically linked", "not stripped"),
    (0, "ASCII text", "not an ELF file"),
    (1, "cannot open `/x' (No such file or directory)", "failed"),
], ids=["stripped", "not-stripped", "debug-info", "no-strip-info", "not-elf", "missing"])
def test_stripped_check_requires_a_stripped_elf_file(ec, out, problem):
    got = X.stripped_problem(_Container(lambda cmd: (ec, out)), "/x")
    assert got is None if problem is None else (problem in got)


def test_skip_or_fail_by_mode():
    with pytest.raises(pytest.skip.Exception, match="why"):
        X.skip_or_fail(None, "why")
    with pytest.raises(pytest.fail.Exception, match="why"):
        X.skip_or_fail(REQ_RPM, "why")
