"""output_plugin_libraries follows the PostgreSQL server actually being configured.

The GUC exists only from 16.15 / 17.11 / 18.5 / 19beta3. An older server refuses to start when it is
set; a newer one will not load spock_output unless it is. The decision therefore comes from
`<pgbin>/postgres --version` of the exact server a cluster is created with, after it is installed,
and never from the configured PG_VERSION (configuration/configN.env). A channel can serve older
point releases than the configs name: daily served 16.13 / 17.9 / 18.3 while the configs said
16.15 / 17.11 / 18.6, and every node in every Spock test run failed to start. A pg_config on PATH is
no substitute either: with PG18 and PG19 installed side by side it reports PG19.

Docker-free: the helpers run against fake containers, and the component tests' REAL functions are
loaded by path with docker.from_env neutralized (the approach of test_pep_ace_version.py) and run
against the same fakes. Covers configured-versus-installed mismatches in both directions, both sides
of every threshold, detection failures (no fallback to PG_VERSION), different versions per target
and per bin directory, and the start_server log tail.
"""
import ast
import importlib.util
import itertools
import os
import re
import sys

import docker
import pytest

from aspects import pg_server_management as psm

_COMPONENT_TEST = os.path.join(os.path.dirname(__file__), "..", "..", "component-test")
_counter = itertools.count()

DEB18 = "/usr/lib/postgresql/18/bin"
DEB19 = "/usr/lib/postgresql/19/bin"
RPM18 = "/usr/pgsql-18/bin"

# What `postgres --version` prints on the pgEdge builds (DEB adds the package version).
BANNER = {
    "18.3": "postgres (PostgreSQL) 18.3 (Ubuntu 18.3-1.noble)",
    "18.4": "postgres (PostgreSQL) 18.4",
    "18.6": "postgres (PostgreSQL) 18.6 (Ubuntu 18.6-1.noble)",
    "19rc1": "postgres (PostgreSQL) 19rc1 (Ubuntu 19~rc1-1.noble)",
}


class StopHere(Exception):
    """Raised by a fake to end a long component test once the part under test has run."""


class FakeServer:
    """A running container with PostgreSQL installs keyed by bin directory.

    Answers `<bin>/postgres --version`, records postgresql.conf appends per data directory (and
    replays them for init_cluster's tail check), starts servers, serves a server log, and records
    every command with its user.
    """
    status = "running"

    def __init__(self, versions, version_exit=0, start_exit=0, logfile="", stop_on=None):
        self.versions = versions          # {bin_dir: `postgres --version` output, or None if absent}
        self.version_exit = version_exit
        self.start_exit = start_exit
        self.logfile = logfile
        self.stop_on = stop_on            # substring of a command that ends the test (StopHere)
        self.conf = {}                    # pgdata -> appended lines
        self.files = {}                   # path -> content written with cat > path << 'EOF'
        self.calls = []

    def exec_run(self, cmd, user=None, **kwargs):
        c = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append((c, user))
        if self.stop_on and self.stop_on in c:
            raise StopHere(c)
        m = re.fullmatch(r"(\S+)/postgres --version", c)
        if m:
            banner = self.versions.get(m.group(1))
            if banner is None:
                return 127, ("sh: 1: %s/postgres: not found\n" % m.group(1)).encode()
            return self.version_exit, (banner + "\n").encode()
        m = re.search(r'echo \\"(.*)\\" >> (\S+)/postgresql\.conf"$', c)
        if m:
            self.conf.setdefault(m.group(2), []).append(m.group(1))
            return 0, b""
        m = re.fullmatch(r"tail -n \d+ (\S+)/postgresql\.conf", c)
        if m:
            return 0, ("\n".join(self.conf.get(m.group(1), [])) + "\n").encode()
        if "/pg_ctl " in c and c.endswith(" start"):
            if self.start_exit:
                return self.start_exit, b"waiting for server to start.... stopped waiting\npg_ctl: could not start server\nExamine the log output.\n"
            return 0, b"server started\n"
        if re.fullmatch(r"tail -n \d+ \S+/logfile", c):
            return 0, self.logfile.encode()
        m = re.search(r"cat > (\S+) << '(\w+)'\n(.*)\n\2$", c, re.S)
        if m:
            self.files[m.group(1)] = m.group(3)
            return 0, b""
        return 0, b""

    def version_queries(self):
        return [(c, u) for c, u in self.calls if c.endswith("/postgres --version")]


class FakeClient:
    def __init__(self, containers):
        self.containers = self
        self._containers = containers

    def get(self, name):
        return self._containers[name]


def load(monkeypatch, filename, **env):
    """A fresh copy of a component test, reading its environment at import."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(docker, "from_env", lambda *a, **k: None)
    name = "component_under_test_%d" % next(_counter)
    spec = importlib.util.spec_from_file_location(name, os.path.join(_COMPONENT_TEST, filename))
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


def has_guc(lines):
    return any(line.startswith("output_plugin_libraries =") for line in lines)


# --------------------------------------------------------------------------- the version itself

@pytest.mark.parametrize("banner,version", [
    ("postgres (PostgreSQL) 18.3 (Ubuntu 18.3-1.noble)", "18.3"),
    ("postgres (PostgreSQL) 18.4", "18.4"),
    ("postgres (PostgreSQL) 16.15 (Ubuntu 16.15-1.noble)", "16.15"),
    ("postgres (PostgreSQL) 19rc1 (Ubuntu 19~rc1-1.noble)", "19rc1"),
    ("postgres (PostgreSQL) 19beta2", "19beta2"),
    ("postgres (PostgreSQL) 19.0", "19.0"),
])
def test_server_version_reads_the_exact_server_binary(banner, version):
    server = FakeServer({DEB18: banner})
    assert psm.server_version(server, DEB18, "postgres") == version
    assert server.version_queries() == [(DEB18 + "/postgres --version", "postgres")]
    assert not any("pg_config" in c for c, _ in server.calls)


def test_server_version_accepts_a_trailing_slash_on_the_bin_directory():
    server = FakeServer({DEB18: BANNER["18.3"]})
    assert psm.server_version(server, DEB18 + "/", "postgres") == "18.3"


# --------------------------------------------------------------------------- both sides of every threshold

@pytest.mark.parametrize("version,required", [
    ("15.17", False),
    ("16.14", False), ("16.15", True),
    ("17.10", False), ("17.11", True),
    ("18.4", False), ("18.5", True),
    ("19beta2", False), ("19beta3", True), ("19rc1", True), ("19.0", True),
    ("20.0", True),
])
def test_guc_for_server_follows_every_threshold(version, required):
    server = FakeServer({RPM18: "postgres (PostgreSQL) %s" % version})
    guc = psm.output_plugin_libraries_guc_for_server(server, RPM18, "postgres")
    assert bool(guc) is required
    if required:
        assert guc == {"output_plugin_libraries": "'pgoutput, test_decoding, spock_output'"}


def test_guc_for_server_unquoted_for_patroni_and_custom_libraries(monkeypatch):
    server = FakeServer({DEB18: BANNER["18.6"]})
    monkeypatch.setenv("OUTPUT_PLUGIN_LIBRARIES", "pgoutput, spock_output")
    assert psm.output_plugin_libraries_guc_for_server(server, DEB18, "postgres", quoted=False) == {
        "output_plugin_libraries": "pgoutput, spock_output"}


# --------------------------------------------------------------------------- configured versus installed

@pytest.mark.parametrize("configured,installed,required", [
    ("18.6", "18.3", False),   # daily: configs name staging's minor, the server is older
    ("18.3", "18.6", True),    # the reverse: configs lag, the server needs the allow-list
    ("16.15", "18.4", False),  # even a configured version of another major is ignored
    ("", "18.6", True),        # nothing configured at all
])
def test_configured_pg_version_is_ignored(monkeypatch, configured, installed, required):
    monkeypatch.setenv("PG_VERSION", configured)
    server = FakeServer({DEB18: "postgres (PostgreSQL) %s" % installed})
    assert bool(psm.output_plugin_libraries_guc_for_server(server, DEB18, "postgres")) is required


def test_the_pure_helpers_need_an_explicit_version(monkeypatch):
    monkeypatch.setenv("PG_VERSION", "18.6")
    with pytest.raises(TypeError):
        psm.output_plugin_libraries_guc()
    with pytest.raises(TypeError):
        psm.requires_output_plugin_libraries()
    with pytest.raises(ValueError, match="cannot parse"):
        psm.requires_output_plugin_libraries("not-a-version")


# --------------------------------------------------------------------------- detection failures

@pytest.mark.parametrize("versions,exit_code,needle", [
    ({}, 0, "exit 127"),                                                # binary absent
    ({DEB18: "postgres: could not load library"}, 0, "no PostgreSQL version"),
    ({DEB18: ""}, 0, "no PostgreSQL version"),
    ({DEB18: "postgres (PostgreSQL) 18.3"}, 1, "exit 1"),                # non-zero exit, even with a banner
])
def test_detection_failure_is_an_error_not_a_fallback(monkeypatch, versions, exit_code, needle):
    monkeypatch.setenv("PG_VERSION", "18.6")
    server = FakeServer(versions, version_exit=exit_code)
    with pytest.raises(psm.ServerVersionError) as err:
        psm.output_plugin_libraries_guc_for_server(server, DEB18, "postgres")
    message = str(err.value)
    assert needle in message
    assert DEB18 + "/postgres --version" in message
    assert "PG_VERSION is not used" in message


# --------------------------------------------------------------------------- one decision per target

def test_each_target_and_bin_directory_gets_its_own_decision():
    daily = FakeServer({DEB18: BANNER["18.3"], DEB19: BANNER["19rc1"]})
    staging = FakeServer({DEB18: BANNER["18.6"]})
    assert psm.output_plugin_libraries_guc_for_server(daily, DEB18, "postgres") == {}
    assert psm.output_plugin_libraries_guc_for_server(daily, DEB19, "postgres") != {}
    assert psm.output_plugin_libraries_guc_for_server(staging, DEB18, "postgres") != {}
    assert [c for c, _ in daily.version_queries()] == [DEB18 + "/postgres --version", DEB19 + "/postgres --version"]


# --------------------------------------------------------------------------- start_server diagnostics

def test_start_failure_reports_a_sanitized_server_log_tail():
    log = "\n".join(
        ["LOG:  line %d" % i for i in range(40)]
        + ['LOG:  unrecognized configuration parameter "output_plugin_libraries" in file "/tmp/n1/postgresql.conf" line 895',
           "FATAL:  configuration file \"/tmp/n1/postgresql.conf\" contains errors",
           "LOG:  connection: host=db password=hunter2 user=x",
           "LOG:  PGPASSWORD=hunter3 \x1b[31mred\x1b[0m bell\x07",
           "LOG:  " + "x" * 2000]) + "\n"
    server = FakeServer({DEB18: BANNER["18.3"]}, start_exit=1, logfile=log)
    with pytest.raises(Exception) as err:
        psm.start_server(server, DEB18, "/tmp/n1", "6431", "postgres")
    message = str(err.value)
    assert "pg_ctl start failed" in message
    assert "unrecognized configuration parameter \"output_plugin_libraries\"" in message
    assert "contains errors" in message
    assert "hunter2" not in message and "hunter3" not in message
    assert "password=<redacted>" in message and "PGPASSWORD=<redacted>" in message
    assert "\x1b" not in message and "\x07" not in message
    assert "x" * 600 not in message                       # long lines are cut
    assert "LOG:  line 5\n" not in message                # only the tail is kept
    assert ("tail -n 20 /tmp/n1/logfile", "postgres") in server.calls


def test_start_failure_still_raises_when_the_log_cannot_be_read():
    server = FakeServer({DEB18: BANNER["18.3"]}, start_exit=1, logfile="")
    with pytest.raises(Exception, match="pg_ctl start failed"):
        psm.start_server(server, DEB18, "/tmp/n1", "6431", "postgres")


def test_successful_start_does_not_read_the_log():
    server = FakeServer({DEB18: BANNER["18.3"]})
    assert psm.start_server(server, DEB18, "/tmp/n1", "6431", "postgres")[0] is True
    assert not any(c.endswith("/logfile") and c.startswith("tail") for c, _ in server.calls)


# --------------------------------------------------------------------------- the real caller paths

@pytest.mark.parametrize("installed,required", [("18.3", False), ("18.6", True)])
def test_spock_nodes_follow_the_installed_server(monkeypatch, installed, required):
    mod = load(monkeypatch, "test_pep_spock.py", PG_MAJOR_VERSION="18", PG_VERSION="18.6" if not required else "18.3",
               DEB_PG_BIN_PATH=DEB18, NO_OF_NODES="2")
    server = FakeServer({DEB18: BANNER[installed]})
    mod.client = FakeClient({"deb1": server})
    mod.test_initialize_nodes("deb1", "deb")
    assert set(server.conf) == {"/tmp/n1", "/tmp/n2"}
    assert all(has_guc(lines) is required for lines in server.conf.values())
    assert len(server.version_queries()) == 1


@pytest.mark.parametrize("installed,required", [("18.3", False), ("18.6", True)])
def test_zero_downtime_nodes_follow_the_installed_server(monkeypatch, installed, required):
    mod = load(monkeypatch, "test_integration_zerodowntime.py", PG_MAJOR_VERSION="18",
               PG_VERSION="18.6" if not required else "18.3", DEB_PG_BIN_PATH=DEB18, NO_OF_NODES="2")
    server = FakeServer({DEB18: BANNER[installed]})
    mod.client = FakeClient({"deb1": server})
    mod.test_initialize_multiple_nodes("deb1", "deb")
    assert set(server.conf) == {"/tmp/n1", "/tmp/n2"}
    assert all(has_guc(lines) is required for lines in server.conf.values())


@pytest.mark.parametrize("installed,required", [("18.4", False), ("18.6", True)])
def test_patroni_clusters_and_dcs_parameters_follow_the_installed_server(monkeypatch, installed, required):
    mod = load(monkeypatch, "test_spock_patroni_failover.py", PG_MAJOR_VERSION="18",
               PG_VERSION="18.6" if not required else "18.3", PG_BIN_PATH=RPM18)
    server = FakeServer({RPM18: "postgres (PostgreSQL) %s" % installed})
    mod.client = FakeClient({"el1": server})
    mod.test_initialize_clusters("el1", "rhel")
    assert set(server.conf) == {"/tmp/n1", "/tmp/n2"}
    assert all(has_guc(lines) is required for lines in server.conf.values())
    mod.test_create_patroni_yml_n2("el1", "rhel")
    yml = server.files["/etc/patroni/n2.yml"]
    assert ("output_plugin_libraries: 'pgoutput, test_decoding, spock_output'" in yml) is required
    assert "shared_preload_libraries: 'spock'" in yml


@pytest.mark.parametrize("installed,required", [("18.3", False), ("18.6", True)])
def test_ace_cluster_follows_the_server_installed_by_its_setup(monkeypatch, installed, required):
    mod = load(monkeypatch, "test_pep_ace.py", PG_MAJOR_VERSION="18", PG_VERSION="18.6" if not required else "18.3",
               DEB_PG_BIN_PATH=DEB18, PGEDGE_ACE_VERSION="3.0.0")
    assert "output_plugin_libraries" not in mod._ace_spock_guc          # nothing decided at import
    server = FakeServer({DEB18: BANNER[installed]}, stop_on="CREATE EXTENSION")
    installed_first = []

    def install_package(container, packages, **kwargs):
        assert not server.version_queries(), "the server version was read before the server was installed"
        installed_first.append(packages)
        return True, "deb", "installed"

    monkeypatch.setattr(mod.package_management, "install_package", install_package)
    mod.client = FakeClient({"deb1": server})
    with pytest.raises(StopHere):
        mod.test_ace_functional_setup_cluster("deb1", "deb")
    assert installed_first
    assert set(server.conf) == {"/tmp/n1", "/tmp/n2", "/tmp/n3"}
    assert all(has_guc(lines) is required for lines in server.conf.values())


def test_no_component_test_decides_from_the_configured_version():
    """Every component test asks the installed server, inside a test, never at import time."""
    seen = 0
    for name in sorted(os.listdir(_COMPONENT_TEST)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(_COMPONENT_TEST, name)).read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            assert node.func.attr not in ("output_plugin_libraries_guc", "requires_output_plugin_libraries"), (
                "%s:%d decides output_plugin_libraries without asking the server" % (name, node.lineno))
        for stmt in tree.body:                      # module level, outside any function
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for node in ast.walk(stmt):
                assert not (isinstance(node, ast.Attribute) and node.attr == "output_plugin_libraries_guc_for_server"), (
                    "%s:%d asks for output_plugin_libraries at import time" % (name, node.lineno))
        seen += sum(1 for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "output_plugin_libraries_guc_for_server")
    assert seen == 6, seen   # spock 1, zero-downtime 2, patroni 2, ace 1
