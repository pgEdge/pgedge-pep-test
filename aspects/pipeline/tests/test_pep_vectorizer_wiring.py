"""Docker-free tests that exercise the REAL functions in component-test/test_pep_vectorizer.py.

The module is loaded by path with docker.from_env and load_dotenv neutralized and with exactly the
environment of the case under test (the platform filter, integration request and backstop are
decided at import time), then driven with a fake docker client/container and spied package helpers
-- the same approach as test_pep_snowflake_wiring.py. Versions are synthetic (9.8.7) so nothing here
depends on today's vectorizer release.

Covers: standalone behaviour preserved; the request family overriding PLATFORM_FILTER; requests for
the wrong package, PG major, family or component rejected before any install; the server, then
pgvector (the extension's hard dependency), then the pinned package, with versions recorded; a
missing prerequisite or a failed pinned install; only the requested vectorizer package installed
(an -llvmjit companion or another PG major fails); the identity step; the expected extension
version derived from the request with its release facts required to agree; CREATE EXTENSION
(vector first) and the library owner, server major and exact extension version; missing references
failing in integration mode and skipping standalone; the provider-dependent steps (API key copy,
OpenAI smoke) skipped in integration mode with their reason and unchanged standalone; and the
expected file lists against the packaging at the release workflow's base.
"""
import importlib.util
import itertools
import json
import os
import sys

import docker
import dotenv
import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
_PATH = os.path.join(_ROOT, "component-test", "test_pep_vectorizer.py")
_counter = itertools.count()

# Config-file style values that disagree with the requests below: integration mode must ignore them.
_CONFIG = {"VECTORIZER_PACKAGE": "pgedge-vectorizer_16", "DEB_VECTORIZER_PACKAGE": "pgedge-postgresql-16-vectorizer",
           "PGEDGE_VECTORIZER_17_VERSION": "1.1", "REPO": "staging",
           "CONTAINERS": "rocky9-amd64", "DEB_CONTAINERS": "debian13-amd64"}
_RPM = dict(_CONFIG, PEP_INTEGRATION_MODE="1", PEP_COMPONENT="vectorizer", PEP_PACKAGE_NAME="pgedge-vectorizer_17",
            PEP_CHANNEL="staging", PEP_EXPECTED_VERSION="9.8.7", PEP_FAMILY="rpm", PEP_ARCH_FILTER="amd64",
            PEP_CONTAINER_ALIAS="rocky9-amd64", PG_MAJOR_VERSION="17", PEP_EXPECTED_RPM="9.8.7-1.el9",
            PEP_EXPECTED_BUILDNUM="1", PEP_EFFECTIVE_TAG="v9.8.7", PLATFORM_FILTER="deb")
_DEB = dict(_RPM, PEP_PACKAGE_NAME="pgedge-postgresql-17-vectorizer", PEP_FAMILY="deb",
            PEP_CONTAINER_ALIAS="debian13-amd64", PEP_EXPECTED_RPM=None, PEP_EXPECTED_DEB="9.8.7-1.trixie",
            PLATFORM_FILTER="rpm")
# A pre-release (the rehearsal shape): parse-release-tag gives version 9.8.7 and buildnum dev1_1.
_RPM_DEV = dict(_RPM, PEP_EFFECTIVE_TAG="v9.8.7-dev1", PEP_EXPECTED_BUILDNUM="dev1_1",
                PEP_EXPECTED_RPM="9.8.7-dev1_1.el9", PEP_CHANNEL="daily")
_DEB_DEV = dict(_DEB, PEP_EFFECTIVE_TAG="v9.8.7-dev1", PEP_EXPECTED_BUILDNUM="dev1_1",
                PEP_EXPECTED_DEB="9.8.7~dev1-1.trixie", PEP_CHANNEL="daily")
_STANDALONE = dict(_CONFIG, VECTORIZER_PACKAGE="pgedge-vectorizer_17", DEB_VECTORIZER_PACKAGE=None,
                   PG_MAJOR_VERSION="17", REPO="release")
_KEYS = sorted(set(_RPM) | set(_DEB) | {
    "PEP_EXPECTED_DEB", "PEP_EXPECTED_BINARY", "PEP_SCENARIO", "PEP_MODE", "UPGRADE",
    "UPGRADE_REPO", "SKIP_CLEANUP", "PG_PORT", "PG_DATA_DIR", "PG_USER", "DEB_PG_USER", "PG_BIN_PATH",
    "DEB_PG_BIN_PATH", "RHEL_PG_PATH", "DEB_PG_PATH", "DEB_PG_SHARE_PATH", "VECTORIZER_BUNDLED_FILES",
    "DEB_VECTORIZER_BUNDLED_FILES", "CHECK_EXTENSIONS", "BASE_EXTENSIONS", "COMPONENTS",
    "PGEDGE_OPEN_API_KEY_PATH"})


def load(monkeypatch, env):
    """Load a fresh copy of the vectorizer component test under exactly ``env`` (None values unset)."""
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    orig_docker, orig_dotenv = docker.from_env, dotenv.load_dotenv
    docker.from_env = lambda *a, **k: None
    dotenv.load_dotenv = lambda *a, **k: False       # never read a local .env: the case's env is exact
    try:
        name = "vectorizer_under_test_%d" % next(_counter)
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

    def exec_run(self, cmd, user=None, **kwargs):
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
        ret = self._ret(*a, **k) if callable(self._ret) else self._ret
        if isinstance(ret, Exception):
            raise ret
        return ret


def _pkg_list(*names):
    """exec_run responder for the package-manager listing behind the only-package check."""
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
    """Run a real test function and report ("passed"|"skipped"|"failed", message), so an unexpected
    skip is never reported as a skipped test here."""
    try:
        fn(*args)
    except pytest.skip.Exception as e:
        return "skipped", str(e)
    except (pytest.fail.Exception, AssertionError) as e:
        return "failed", str(e)
    return "passed", ""


# --------------------------------------------------------------------------- selection and backstop
def test_standalone_keeps_the_config_packages_and_platform_filter(monkeypatch):
    m = load(monkeypatch, dict(_STANDALONE, PLATFORM_FILTER="deb"))
    assert m.INTEGRATION_REQUEST is None and m.INTEGRATION_PROBLEM is None
    assert m.all_containers == [("debian13-amd64", "deb")]
    assert m.get_container_config("rhel")["vectorizer_package"] == "pgedge-vectorizer_17"
    assert m.get_container_config("deb")["vectorizer_package"] == "pgedge-postgresql-17-vectorizer"
    assert m.vectorizer_version == "1.1"
    m = load(monkeypatch, dict(_STANDALONE, PLATFORM_FILTER=None))
    assert m.all_containers == [("rocky9-amd64", "rhel"), ("debian13-amd64", "deb")]


@pytest.mark.parametrize("env,containers", [(_RPM, [("rocky9-amd64", "rhel")]),
                                            (_DEB, [("debian13-amd64", "deb")])], ids=["rpm", "deb"])
def test_the_request_selects_the_platform_and_package_never_the_config(monkeypatch, env, containers):
    m = load(monkeypatch, env)                   # PLATFORM_FILTER and *VECTORIZER_PACKAGE disagree
    assert m.INTEGRATION_PROBLEM is None
    assert m.all_containers == containers
    assert (m.get_container_config("rhel")["vectorizer_package"]
            == m.get_container_config("deb")["vectorizer_package"] == env["PEP_PACKAGE_NAME"])


@pytest.mark.parametrize("over,reason", [
    ({"PEP_PACKAGE_NAME": "pgedge-vectorizer_16"}, "pg_major_mismatch"),
    ({"PEP_PACKAGE_NAME": "pgedge-postgresql-17-vectorizer"}, "package_family_mismatch"),
    ({"PEP_PACKAGE_NAME": "pgedge-vectorizer_17-llvmjit"}, "runtime_not_certified"),
    ({"PEP_PACKAGE_NAME": "pgedge-pgvector_17"}, "package_not_allowed"),
    ({"PEP_PACKAGE_NAME": "pgedge-pg-vectorize_17"}, "package_not_allowed"),
    ({"PEP_COMPONENT": "spock"}, "package_not_allowed"),
], ids=["other-pg", "other-family", "llvmjit", "pgvector", "pg-vectorize", "other-component"])
def test_a_request_the_policy_rejects_fails_at_load_before_any_install(monkeypatch, over, reason):
    with pytest.raises(Exception, match=reason):
        load(monkeypatch, dict(_RPM, **over))


def test_the_backstop_names_each_problem(monkeypatch):
    m = load(monkeypatch, _RPM)
    req = dict(m.INTEGRATION_REQUEST)
    assert m._integration_problem(req, "17") is None
    assert "PG_MAJOR_VERSION=16" in m._integration_problem(req, "16")
    assert "pg_major_mismatch" in m._integration_problem(dict(req, package_name="pgedge-vectorizer_16"), "17")
    assert "package_family_mismatch" in m._integration_problem(dict(req, family="deb"), "17")
    assert "runtime_not_certified" in m._integration_problem(
        dict(req, package_name="pgedge-vectorizer_17-llvmjit"), "17")
    assert "not the PG-coupled vectorizer" in m._integration_problem(dict(req, component="snowflake"), "17")
    assert m._integration_problem(None, "17") is None


def test_another_components_request_is_refused_before_any_install(monkeypatch, tmp_path):
    # A valid snowflake request reaching the vectorizer test: it loads, but the backstop refuses it.
    m = load(monkeypatch, dict(_RPM, PEP_COMPONENT="snowflake", PEP_PACKAGE_NAME="pgedge-snowflake_17"))
    assert "not the PG-coupled vectorizer component" in m.INTEGRATION_PROBLEM
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
def test_server_then_pgvector_then_the_pinned_request_and_versions_are_recorded(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(
        "pgedge-vectorizer_17", "pgedge-postgresql17-server", "pgedge-pgvector_17", "pgedge-vectorizer_17-debuginfo")))
    order = []
    ip = _Spy(lambda *a, **k: (True, "rhel", "installed"), order, "prerequisite")
    pinned = _Spy((True, "ok", "f" * 64), order, "pinned")
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    monkeypatch.setattr(m.package_management, "query_installed_version",
                        lambda c, n: {"pgedge-vectorizer_17": "9.8.7-1.el9",
                                      "pgedge-pgvector_17": "0.8.7-1.el9"}.get(n, "17.11-1PGEDGE.el9"))

    m.test_component_install("rocky9-amd64", "rhel")

    assert order == ["prerequisite", "prerequisite", "pinned"]
    assert [c[1]["package_name"] for c in ip.calls] == [["pgedge-postgresql17-server"], ["pgedge-pgvector_17"]]
    assert all(c[1]["install_pg_server"] is False and c[1]["pg_major_version"] == "17" for c in ip.calls)
    assert pinned.calls[0][0][1:] == ("pgedge-vectorizer_17", "9.8.7-1.el9")
    marker = json.loads((tmp_path / "install-evidence.json").read_text())
    assert marker["install_kind"] == "pinned" and marker["installed_sha256"] == "f" * 64
    assert json.loads((tmp_path / "prerequisite-versions.json").read_text()) == {
        "channel": "staging", "pg_major": "17", "requested_package": "pgedge-vectorizer_17",
        "installed_versions": {"pgedge-postgresql17-server": "17.11-1PGEDGE.el9",
                               "pgedge-pgvector_17": "0.8.7-1.el9",
                               "pgedge-vectorizer_17": "9.8.7-1.el9"}}


def test_deb_prerequisites_are_the_server_and_pgvector_for_the_requested_major(monkeypatch, tmp_path):
    m = load(monkeypatch, _DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(
        "pgedge-postgresql-17-vectorizer", "pgedge-postgresql-17", "pgedge-postgresql-17-pgvector")))
    ip, pinned = _Spy((True, "debian", "installed")), _Spy((True, "ok", "e" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    m.test_component_install("debian13-amd64", "deb")
    assert [c[1]["package_name"] for c in ip.calls] == [["pgedge-postgresql-17"], ["pgedge-postgresql-17-pgvector"]]
    assert pinned.calls[0][0][1:] == ("pgedge-postgresql-17-vectorizer", "9.8.7-1.trixie")
    assert sorted(json.loads((tmp_path / "prerequisite-versions.json").read_text())["installed_versions"]) == [
        "pgedge-postgresql-17", "pgedge-postgresql-17-pgvector", "pgedge-postgresql-17-vectorizer"]


def test_a_missing_server_is_a_prerequisite_failure_and_nothing_else_is_installed(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    ip = _Spy(Exception("Failed to install pgedge-postgresql17-server: No match"))
    pinned = _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    with pytest.raises(pytest.fail.Exception, match="PostgreSQL 17 prerequisite unavailable from the 'staging' channel"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert len(ip.calls) == 1 and pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_a_missing_pgvector_is_a_prerequisite_failure_and_nothing_is_pinned(monkeypatch, tmp_path):
    m = load(monkeypatch, _DEB)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))

    def install(container, package_name, **k):
        if package_name == ["pgedge-postgresql-17-pgvector"]:
            raise Exception("Failed to install pgedge-postgresql-17-pgvector: Unable to locate package")
        return True, "debian", "installed"
    pinned = _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", install)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    with pytest.raises(pytest.fail.Exception,
                       match="pgvector prerequisite unavailable from the 'staging' channel "
                             r"\(pgedge-postgresql-17-pgvector\); pgedge-postgresql-17-vectorizer was not installed"):
        m.test_component_install("debian13-amd64", "deb")
    assert pinned.calls == [] and not (tmp_path / "install-evidence.json").exists()


def test_failed_pinned_install_fails_without_install_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((False, "checksum mismatch", None)))
    with pytest.raises(AssertionError, match="pinned install of pgedge-vectorizer_17=9.8.7-1.el9 failed: checksum mismatch"):
        m.test_component_install("rocky9-amd64", "rhel")
    assert not (tmp_path / "install-evidence.json").exists()


@pytest.mark.parametrize("present", [
    ("pgedge-vectorizer_17", "pgedge-vectorizer_17-llvmjit"),        # the uncertified JIT companion
    ("pgedge-vectorizer_17", "pgedge-vectorizer_16"),                # another PG major beside the request
    ("pgedge-vectorizer_17", "pgedge-postgresql-17-vectorizer"),     # the other family's package
    ("pgedge-vectorizer_16",),                                       # the request replaced
    (),                                                              # nothing installed
], ids=["llvmjit", "other-pg", "other-family", "replaced", "none"])
def test_any_vectorizer_package_but_exactly_the_request_fails_the_install(monkeypatch, tmp_path, present):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list(*present, "pgedge-postgresql17-server", "pgedge-pgvector_17")))
    monkeypatch.setattr(m.package_management, "install_package", _Spy((True, "rhel", "installed")))
    monkeypatch.setattr(m.package_management, "install_pinned", _Spy((True, "ok", "f" * 64)))
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: "x")
    with pytest.raises(AssertionError, match="expected exactly the requested vectorizer package"):
        m.test_component_install("rocky9-amd64", "rhel")


def test_standalone_install_is_unchanged(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    ip, pinned = _Spy((True, "rhel", "installed")), _Spy((True, "ok", "f" * 64))
    monkeypatch.setattr(m.package_management, "install_package", ip)
    monkeypatch.setattr(m.package_management, "install_pinned", pinned)
    m.test_component_install("rocky9-amd64", "rhel")
    assert pinned.calls == [] and len(ip.calls) == 1
    assert ip.calls[0][0][1:] == ("pgedge-vectorizer_17",) and ip.calls[0][1] == {}


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
                                         ("9.8.7-dev1_1.el9", False), ("1.1-1.el9", False)],
                         ids=["exact", "older", "dev", "config-release"])
def test_identity_proves_the_exact_requested_version(monkeypatch, tmp_path, observed, ok):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    m.pep_integration_mode.pep_evidence.write_install_evidence(
        m.INTEGRATION_REQUEST, "run-A", "pinned", "9.8.7-1.el9", str(tmp_path / "install-evidence.json"),
        installed_sha256="f" * 64)
    monkeypatch.setattr(m.package_management, "query_installed_version", lambda c, n: observed)
    if ok:
        m.test_vectorizer_identity("rocky9-amd64", "rhel")
    else:
        with pytest.raises(AssertionError, match="not proven"):
            m.test_vectorizer_identity("rocky9-amd64", "rhel")
    assert json.loads((tmp_path / "identity-evidence.json").read_text())["l2a"] == ("proven" if ok else "not_proven")


def test_identity_without_install_evidence_fails_with_persisted_evidence(monkeypatch, tmp_path):
    m = load(monkeypatch, _RPM)
    _wire(monkeypatch, tmp_path, m, _Container(_pkg_list()))
    with pytest.raises(pytest.fail.Exception, match="install-before-identity precondition failed"):
        m.test_vectorizer_identity("rocky9-amd64", "rhel")
    assert (tmp_path / "identity-evidence.json").exists()


def test_identity_and_package_version_steps_by_mode(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    assert _outcome(m.test_vectorizer_identity, "rocky9-amd64", "rhel") == ("skipped", "integration mode only")
    vp = _Spy((True, "rhel", "1.1-1.el9", "ok"))
    monkeypatch.setattr(m.package_management, "verify_package_version", vp)
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    m.test_component_package_version("rocky9-amd64", "rhel")     # standalone: the configured version
    assert vp.calls[0][0][1:] == ("pgedge-vectorizer_17", "1.1")
    m = load(monkeypatch, _RPM)
    monkeypatch.setattr(m.package_management, "verify_package_version", vp)
    outcome, msg = _outcome(m.test_component_package_version, "rocky9-amd64", "rhel")
    assert outcome == "skipped" and "test_vectorizer_identity" in msg and len(vp.calls) == 1


# --------------------------------------------------------------------------- expected extension version
@pytest.mark.parametrize("env", [_RPM, _DEB, _RPM_DEV, _DEB_DEV], ids=["rpm-ga", "deb-ga", "rpm-dev", "deb-dev"])
def test_the_extension_version_is_the_release_version_exactly(monkeypatch, env):
    # release.yml requires pgedge_vectorizer.control's default_version to equal the tag's version,
    # so a pre-release (v9.8.7-dev1) installs extension 9.8.7, never 9.8.7-dev1.
    m = load(monkeypatch, env)
    assert m.expected_extension_version(m.INTEGRATION_REQUEST) == ("9.8.7", [])


@pytest.mark.parametrize("over,msg", [
    ({"effective_tag": None}, "effective_tag is required"),
    ({"effective_tag": "v9.8.7_dev1"}, "is not v<version>[-<suffix>]"),
    ({"effective_tag": "v9.8.7-"}, "is not v<version>[-<suffix>]"),
    ({"effective_tag": "v9.8"}, "does not match expected_version '9.8.7'"),
    ({"effective_tag": "v9.8.70"}, "does not match expected_version '9.8.7'"),
    ({"effective_tag": "v9.8.7-dev1"}, "expected_buildnum '1' does not match"),
    ({"expected_buildnum": "2"}, "expected_buildnum '2' does not match"),
    ({"expected_rpm": "9.8.7-2.el9"}, "expected_rpm '9.8.7-2.el9' does not match"),
    ({"expected_rpm": "9.8.7-1"}, "expected_rpm '9.8.7-1' does not match"),
    ({"expected_rpm": "9.8.7-1.el9 "}, "does not match"),
    ({"expected_rpm": "9.8.70-1.el9"}, "does not match"),
], ids=["no-tag", "bad-separator", "empty-suffix", "tag-prefix", "tag-longer", "tag-dev", "buildnum",
        "rpm-release", "rpm-no-dist", "rpm-space", "rpm-version"])
def test_disagreeing_release_facts_are_reported_not_trusted(monkeypatch, over, msg):
    m = load(monkeypatch, _RPM)
    req = dict(m.INTEGRATION_REQUEST, **over)
    version, problems = m.expected_extension_version(req)
    assert version is None and any(msg in p for p in problems), problems


@pytest.mark.parametrize("deb,ok", [("9.8.7~dev1-1.trixie", True), ("9.8.7-dev1_1.trixie", False),
                                    ("9.8.7~dev1-2.trixie", False), ("9.8.7-1.trixie", False),
                                    ("9.8.7~dev1-1", False)])
def test_the_deb_string_must_carry_the_suffix_after_a_tilde(monkeypatch, deb, ok):
    m = load(monkeypatch, _DEB_DEV)
    version, problems = m.expected_extension_version(dict(m.INTEGRATION_REQUEST, expected_deb=deb))
    assert (version == "9.8.7" and problems == []) if ok else (version is None and problems)


def test_a_dotted_suffix_is_compacted_like_parse_release_tag(monkeypatch):
    m = load(monkeypatch, dict(_RPM, PEP_EFFECTIVE_TAG="v9.8.7-rc.1", PEP_EXPECTED_BUILDNUM="rc1_1",
                               PEP_EXPECTED_RPM="9.8.7-rc1_1.el10"))
    assert m.expected_extension_version(m.INTEGRATION_REQUEST) == ("9.8.7", [])


# --------------------------------------------------------------------------- CREATE EXTENSION + binding
def _server(owner="pgedge-vectorizer_17", version="170011", ext="9.8.7", libdir="/usr/pgsql-17/lib",
            create=(0, "CREATE EXTENSION\n"), create_vector=(0, "CREATE EXTENSION\n")):
    def respond(cmd):
        if isinstance(cmd, str) and "/psql " in cmd:          # pg_server_management.execute_psql_query
            if "CREATE EXTENSION vector;" in cmd:
                return create_vector
            if "CREATE EXTENSION pgedge_vectorizer;" in cmd:
                return create
            return 1, "unexpected %r" % (cmd,)
        if cmd[0] == "psql":                                    # pep_extension_checks.psql_value
            sql = cmd[-1]
            if "PKGLIBDIR" in sql:
                return 0, libdir + "\n"
            if "server_version_num" in sql:
                return 0, version + "\n"
            if "extversion" in sql and "'pgedge_vectorizer'" in sql:
                return 0, ext + "\n"
        if cmd[:2] == ["rpm", "-qf"]:
            return (0, owner + "\n") if owner else (1, "file %s is not owned by any package" % cmd[-1])
        if cmd[:2] == ["dpkg-query", "-S"]:
            return (0, "%s: %s\n" % (owner, cmd[2])) if owner else (1, "no path found matching pattern")
        return 1, "unexpected %r" % (cmd,)
    return respond


def test_vector_then_the_extension_are_created_before_the_binding_passes(monkeypatch):
    m = load(monkeypatch, _RPM)
    c = _Container(_server())
    monkeypatch.setattr(m, "client", _Client(c))
    m.test_vectorizer_requested_extension("rocky9-amd64", "rhel")
    creates = [cmd for cmd in c.cmds if isinstance(cmd, str)]
    assert len(creates) == 2 and "CREATE EXTENSION vector;" in creates[0]
    assert "CREATE EXTENSION pgedge_vectorizer;" in creates[1] and creates[1].startswith("/usr/pgsql-17/bin/psql ")
    assert c.cmds.index(creates[1]) < next(i for i, cmd in enumerate(c.cmds) if isinstance(cmd, list))
    assert ["rpm", "-qf", "--queryformat", "%{NAME}\\n", "/usr/pgsql-17/lib/pgedge_vectorizer.so"] in c.cmds
    assert all(cmd[cmd.index("-p") + 1] == "5432" for cmd in c.cmds if isinstance(cmd, list) and cmd[0] == "psql")
    for env in (_DEB, _DEB_DEV):
        m = load(monkeypatch, env)
        monkeypatch.setattr(m, "client", _Client(_Container(_server(owner="pgedge-postgresql-17-vectorizer",
                                                                    libdir="/usr/lib/postgresql/17/lib"))))
        m.test_vectorizer_requested_extension("debian13-amd64", "deb")


@pytest.mark.parametrize("over,msg", [
    ({"owner": "pgedge-vectorizer_16"}, "not the requested 'pgedge-vectorizer_17'"),
    ({"owner": "pgedge-vectorizer_17-llvmjit"}, "not the requested"),
    ({"owner": ""}, "not the requested"),
    ({"version": "160004"}, "runs PostgreSQL 160004, not the requested PG17"),
    ({"ext": "9.8"}, "extension version is '9.8'"),
    ({"ext": "9.8.70"}, "extension version is '9.8.70'"),
    ({"ext": "9.8.7-dev1"}, "extension version is '9.8.7-dev1'"),
    ({"ext": "9.8.7~dev1"}, "extension version is '9.8.7~dev1'"),
    ({"ext": "1.1"}, "extension version is '1.1'"),              # the configured release, not the request
    ({"ext": ""}, "extension version is ''"),                    # extension not created
    ({"create_vector": (1, 'ERROR:  extension "vector" is not available')}, "CREATE EXTENSION vector failed"),
    ({"create": (1, 'ERROR:  required extension "vector" is not installed')},
     "CREATE EXTENSION pgedge_vectorizer failed"),
], ids=["owner-other-pg", "owner-llvmjit", "unowned", "pg-major", "ext-prefix", "ext-longer", "ext-dash-dev",
        "ext-tilde-dev", "ext-config", "ext-missing", "no-vector", "create-fails"])
def test_binding_rejects_a_wrong_library_major_or_extension_version(monkeypatch, over, msg):
    m = load(monkeypatch, _RPM)
    monkeypatch.setattr(m, "client", _Client(_Container(_server(**over))))
    with pytest.raises(AssertionError, match=msg.replace(".", r"\.").replace("~", r"\~")):
        m.test_vectorizer_requested_extension("rocky9-amd64", "rhel")


def test_a_request_whose_release_facts_disagree_fails_before_create_extension(monkeypatch):
    m = load(monkeypatch, dict(_RPM, PEP_EFFECTIVE_TAG="v9.8.6"))
    c = _Container(_server())
    monkeypatch.setattr(m, "client", _Client(c))
    with pytest.raises(AssertionError, match="cannot derive the expected pgedge_vectorizer version"):
        m.test_vectorizer_requested_extension("rocky9-amd64", "rhel")
    assert c.cmds == []


def test_binding_is_integration_only(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(None))
    assert _outcome(m.test_vectorizer_requested_extension, "rocky9-amd64", "rhel") == (
        "skipped", "integration mode only")


# --------------------------------------------------------------------------- provider-dependent steps
def test_api_key_copy_is_skipped_in_integration_mode_with_its_reason(monkeypatch):
    m = load(monkeypatch, _RPM)
    cp = _Spy((True, "copied"))
    monkeypatch.setattr(m.file_management, "copy_file_to_container", cp)
    monkeypatch.setattr(m, "client", _Client(None))             # never looked up
    outcome, msg = _outcome(m.test_copy_api_key, "rocky9-amd64", "rhel")
    assert outcome == "skipped" and "model-provider API key" in msg and "no paid calls" in msg
    assert cp.calls == []


def test_api_key_copy_is_unchanged_standalone(monkeypatch, tmp_path):
    key = tmp_path / "open_api_key"
    m = load(monkeypatch, dict(_STANDALONE, PGEDGE_OPEN_API_KEY_PATH=str(tmp_path / "missing")))
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    outcome, msg = _outcome(m.test_copy_api_key, "rocky9-amd64", "rhel")
    assert outcome == "failed" and "API key file not found" in msg      # a missing key still fails
    key.write_text("k")
    m = load(monkeypatch, dict(_STANDALONE, PGEDGE_OPEN_API_KEY_PATH=str(key)))
    monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
    cp = _Spy((True, "copied"))
    monkeypatch.setattr(m.file_management, "copy_file_to_container", cp)
    assert _outcome(m.test_copy_api_key, "rocky9-amd64", "rhel") == ("passed", "")
    assert cp.calls[0][1]["container_file_path"] == "/tmp/api_key_file"


def test_the_openai_smoke_is_skipped_in_integration_mode_with_its_reason(monkeypatch):
    m = load(monkeypatch, _RPM)
    monkeypatch.setattr(m, "client", _Client(None))             # never looked up
    outcome, msg = _outcome(m.test_component_functional_smoke, "rocky9-amd64", "rhel", "pgedge-vectorizer_17")
    assert outcome == "skipped" and "embedding dimension" in msg and "model-provider API key" in msg


def test_the_openai_smoke_is_unchanged_standalone(monkeypatch):
    # Standalone keeps its options and the SQL file it runs (which creates both extensions itself).
    m = load(monkeypatch, dict(_STANDALONE, CHECK_EXTENSIONS="false"))
    assert _outcome(m.test_component_functional_smoke, "rocky9-amd64", "rhel", "pgedge-vectorizer_17") == (
        "skipped", "Extension check disabled via env")
    m = load(monkeypatch, _STANDALONE)
    assert m.components == ["pgedge-vectorizer_17"] and m.check_extensions is True
    with open(os.path.join(_ROOT, "sql", "vectorizer.sql")) as f:
        sql = f.read()
    assert sql.index("CREATE EXTENSION IF NOT EXISTS vector;") < sql.index(
        "CREATE EXTENSION IF NOT EXISTS pgedge_vectorizer;")


def test_init_cluster_keeps_the_preload_and_provider_settings_in_both_modes(monkeypatch):
    # CREATE EXTENSION does not need the preload (_PG_init registers the workers only when preloaded);
    # the test preloads it as before, and the provider is configured but never called.
    for env in (_STANDALONE, _RPM):
        m = load(monkeypatch, env)
        monkeypatch.setattr(m, "client", _Client(_Container(_pkg_list())))
        init = _Spy((True, "", "initialized"))
        monkeypatch.setattr(m.pg_server_management, "init_cluster", init)
        m.test_init_cluster("rocky9-amd64", "rhel")
        gucs = init.calls[0][0][4]
        assert gucs["shared_preload_libraries"] == "'pgedge_vectorizer'"
        assert gucs["pgedge_vectorizer.provider"] == "'openai'"
        assert gucs["pgedge_vectorizer.api_key_file"] == "'/tmp/api_key_file'"


# --------------------------------------------------------------------------- missing references
_NEEDS_CONTAINER = ["test_configure_repository", "test_verify_sbom", "test_verify_bundled_files",
                    "test_init_cluster", "test_start_server", "test_check_connection", "test_stop_server",
                    "test_package_uninstall", "test_pgedge_cleanup"]


@pytest.mark.parametrize("name", _NEEDS_CONTAINER + ["test_component_install", "test_vectorizer_identity",
                                                     "test_vectorizer_requested_extension"])
def test_a_missing_container_fails_in_integration_mode(monkeypatch, name):
    m = load(monkeypatch, _RPM)
    monkeypatch.setattr(m, "client", _Client(None))
    outcome, msg = _outcome(getattr(m, name), "rocky9-amd64", "rhel")
    assert outcome == "failed" and "rocky9-amd64 not found" in msg


@pytest.mark.parametrize("name", _NEEDS_CONTAINER + ["test_component_install", "test_component_package_version",
                                                     "test_copy_api_key"])
def test_a_missing_container_still_skips_standalone(monkeypatch, name):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(None))
    outcome, msg = _outcome(getattr(m, name), "rocky9-amd64", "rhel")
    assert outcome == "skipped" and "rocky9-amd64 not found" in msg


def test_a_missing_container_still_skips_the_standalone_smoke(monkeypatch):
    m = load(monkeypatch, _STANDALONE)
    monkeypatch.setattr(m, "client", _Client(None))
    outcome, msg = _outcome(m.test_component_functional_smoke, "rocky9-amd64", "rhel", "pgedge-vectorizer_17")
    assert outcome == "skipped" and "rocky9-amd64 not found" in msg


_NO_REFERENCE = Exception("No expected file found for vectorizer at /x/expected-output/rpm (checked: /x/a)")


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
    assert outcome == "failed" and "No expected file found for vectorizer" in msg
    outcome, msg = _bundled_outcome(monkeypatch, _STANDALONE, _raises(_NO_REFERENCE))
    assert outcome == "skipped" and "No expected file found for vectorizer" in msg


@pytest.mark.parametrize("env,package", [(_RPM, "pgedge-vectorizer_17"), (_STANDALONE, "pgedge-vectorizer_17")],
                         ids=["integration", "standalone"])
def test_bundled_file_outcomes_are_unchanged_when_the_reference_exists(monkeypatch, env, package):
    seen = []
    assert _bundled_outcome(monkeypatch, env, lambda **k: seen.append(k) or (True, None, "match")) == ("passed", "")
    assert seen[0]["package_name"] == seen[0]["component"] == package
    outcome, msg = _bundled_outcome(monkeypatch, env, lambda **k: (False, {"missing_files": ["/usr/x"]}, "differ"))
    assert outcome == "failed" and "Missing files" in msg and "/usr/x" in msg
    outcome, msg = _bundled_outcome(monkeypatch, env, _raises(RuntimeError("rpm -ql failed")))
    assert outcome == "failed" and "rpm -ql failed" in msg


@pytest.mark.parametrize("env", [_RPM, _STANDALONE], ids=["integration", "standalone"])
def test_a_missing_sbom_fails(monkeypatch, env):
    m = load(monkeypatch, env)

    def respond(cmd):
        if "sq verify" in cmd:
            return 1, "Error: Failed to open pgedge-vectorizer-sbom.json: No such file or directory"
        return 0, ""
    monkeypatch.setattr(m, "client", _Client(_Container(respond)))
    outcome, msg = _outcome(m.test_verify_sbom, "rocky9-amd64", "rhel")
    assert outcome == "failed" and "SBOM verification failed" in msg


# --------------------------------------------------------------------------- expected file lists
# What the pgedge-vectorizer packaging at the caller's base (pgEdge/pgedge-vectorizer cdd022a) builds
# for PG17, derived from the source (no 1.2 package has been published):
#   * Makefile: MODULE_big pgedge_vectorizer (15 OBJS), DATA = the 1.2 install script plus nine older
#     install and upgrade scripts; pgedge_vectorizer.control;
#   * RPM, pkg/rpm/pgedge_vectorizer.spec %files: %doc README.md, %license docs/LICENCE.md, the .so,
#     the control and *sql files, the signed SBOM under %{pginstdir}/sbom (/usr/pgsql-17); JIT
#     bitcode only in the uncertified -llvmjit subpackage;
#   * DEB, pkg/deb/debian: PGXS also installs bitcode for every OBJS file (there is no separate
#     llvmjit .deb); the .install file puts the SBOM under /usr/lib/postgresql/17/sbom/; debian/docs
#     ships docs/LICENCE.md (962 bytes, below dh_compress's 4 KiB threshold, so uncompressed) and
#     rules ships README.md (16857 bytes, so README.md.gz) and the changelog.
# The packaging is unchanged since v1.1 (git diff v1.1 cdd022a -- pkg: build scripts only), so the
# 1.1-1 packages the daily and staging channels serve carry the same files except the two 1.2 SQL
# scripts (Makefile DATA at v1.1).
_SQL_12 = ["pgedge_vectorizer--1.0--1.1.sql", "pgedge_vectorizer--1.0-beta1--1.0-beta2.sql",
           "pgedge_vectorizer--1.0-beta2--1.0-beta3.sql", "pgedge_vectorizer--1.0-beta2.sql",
           "pgedge_vectorizer--1.0-beta3--1.0.sql", "pgedge_vectorizer--1.0-beta3.sql", "pgedge_vectorizer--1.0.sql",
           "pgedge_vectorizer--1.1--1.2.sql", "pgedge_vectorizer--1.1.sql", "pgedge_vectorizer--1.2.sql",
           "pgedge_vectorizer.control"]
_SQL_11 = [f for f in _SQL_12 if f not in ("pgedge_vectorizer--1.1--1.2.sql", "pgedge_vectorizer--1.2.sql")]
_OBJS = ["bm25", "chunking", "embed", "guc", "hybrid_chunking", "pgedge_vectorizer", "provider", "provider_common",
         "provider_gemini", "provider_ollama", "provider_openai", "provider_voyage", "queue", "tokenizer", "worker"]
_DEB_DOC = "/usr/share/doc/pgedge-postgresql-17-vectorizer"
_BC = "/usr/lib/postgresql/17/lib/bitcode"

_EXPECTED = {
    "rpm": ["/usr/pgsql-17/lib/pgedge_vectorizer.so", "/usr/pgsql-17/sbom/pgedge-vectorizer-sbom.json",
            "/usr/pgsql-17/sbom/pgedge-vectorizer-sbom.json.asc"]
           + ["/usr/pgsql-17/share/extension/" + f for f in _SQL_12]
           + ["/usr/share/doc/pgedge-vectorizer_17/README.md", "/usr/share/licenses/pgedge-vectorizer_17/LICENCE.md"],
    "deb": [_BC + "/pgedge_vectorizer", _BC + "/pgedge_vectorizer/src"]
           + ["%s/pgedge_vectorizer/src/%s.bc" % (_BC, o) for o in _OBJS]
           + [_BC + "/pgedge_vectorizer.index.bc", "/usr/lib/postgresql/17/lib/pgedge_vectorizer.so",
              "/usr/lib/postgresql/17/sbom/pgedge-vectorizer-sbom.json",
              "/usr/lib/postgresql/17/sbom/pgedge-vectorizer-sbom.json.asc",
              _DEB_DOC + "/LICENCE.md", _DEB_DOC + "/README.md.gz", _DEB_DOC + "/changelog.Debian.gz"]
           + ["/usr/share/postgresql/17/extension/" + f for f in _SQL_12],
}


def _listing(family, sql):
    """The package's own listing (rpm -ql / dpkg -L, directories included) for the given SQL set."""
    if family == "rpm":
        return (["/usr/lib/.build-id", "/usr/lib/.build-id/3a", "/usr/lib/.build-id/3a/0c11d7e2f4b8a96d5e1c3b7a90f8e2d4c6b1a5",
                 "/usr/pgsql-17/lib/pgedge_vectorizer.so", "/usr/pgsql-17/sbom/pgedge-vectorizer-sbom.json",
                 "/usr/pgsql-17/sbom/pgedge-vectorizer-sbom.json.asc"]
                + ["/usr/pgsql-17/share/extension/" + f for f in sql]
                + ["/usr/share/doc/pgedge-vectorizer_17", "/usr/share/doc/pgedge-vectorizer_17/README.md",
                   "/usr/share/licenses/pgedge-vectorizer_17", "/usr/share/licenses/pgedge-vectorizer_17/LICENCE.md"])
    return (["/.", "/usr", "/usr/lib", "/usr/lib/postgresql", "/usr/lib/postgresql/17", "/usr/lib/postgresql/17/lib",
             _BC, _BC + "/pgedge_vectorizer", _BC + "/pgedge_vectorizer/src"]
            + ["%s/pgedge_vectorizer/src/%s.bc" % (_BC, o) for o in _OBJS]
            + [_BC + "/pgedge_vectorizer.index.bc", "/usr/lib/postgresql/17/lib/pgedge_vectorizer.so",
               "/usr/lib/postgresql/17/sbom", "/usr/lib/postgresql/17/sbom/pgedge-vectorizer-sbom.json",
               "/usr/lib/postgresql/17/sbom/pgedge-vectorizer-sbom.json.asc", "/usr/share", "/usr/share/doc", _DEB_DOC,
               _DEB_DOC + "/LICENCE.md", _DEB_DOC + "/README.md.gz", _DEB_DOC + "/changelog.Debian.gz",
               "/usr/share/postgresql", "/usr/share/postgresql/17", "/usr/share/postgresql/17/extension"]
            + ["/usr/share/postgresql/17/extension/" + f for f in sql])


@pytest.mark.parametrize("family", ["rpm", "deb"])
def test_the_expected_lists_name_exactly_what_the_packaging_builds(family):
    with open(os.path.join(_ROOT, "expected-output", family, "vectorizer")) as f:
        lines = [line.strip() for line in f if line.strip()]
    assert len(lines) == len(set(lines))
    assert sorted(lines) == sorted(_EXPECTED[family])


@pytest.mark.parametrize("env,ctype,alias,listing,family", [
    (_RPM, "rhel", "rocky9-amd64", "rpm -ql pgedge-vectorizer_17", "rpm"),
    (_DEB, "deb", "debian13-amd64", "dpkg -L pgedge-postgresql-17-vectorizer", "deb"),
], ids=["rpm", "deb"])
@pytest.mark.parametrize("sql", [_SQL_12, _SQL_11], ids=["base-1.2", "channel-1.1"])
def test_bundled_file_references_pass_for_the_packaging_and_catch_a_missing_file(
        monkeypatch, env, ctype, alias, listing, family, sql):
    # The base packaging passes, and so do the 1.1-1 packages the channels serve today: the shared
    # normalization collapses every pgedge_vectorizer--<version>[--<version>].sql to one name.
    m = load(monkeypatch, env)
    files = _listing(family, sql)

    def respond(cmd):
        assert cmd == listing, cmd
        return 0, "\n".join(files) + "\n"
    monkeypatch.setattr(m, "client", _Client(_Container(respond)))
    assert _outcome(m.test_verify_bundled_files, alias, ctype) == ("passed", "")
    # ... and a package that stops shipping its license, library, control file or SBOM is caught.
    lic = "/usr/share/licenses/pgedge-vectorizer_17/LICENCE.md" if family == "rpm" else _DEB_DOC + "/LICENCE.md"
    for gone in (lic, [f for f in files if f.endswith("/pgedge_vectorizer.so")][0],
                 [f for f in files if f.endswith("/pgedge_vectorizer.control")][0],
                 [f for f in files if f.endswith("-sbom.json.asc")][0]):
        files = _listing(family, sql)
        files.remove(gone)
        outcome, msg = _outcome(m.test_verify_bundled_files, alias, ctype)
        assert outcome == "failed" and "Missing files (1)" in msg, (gone, msg)


def test_a_deb_without_the_bitcode_of_a_new_source_file_is_caught(monkeypatch):
    m = load(monkeypatch, _DEB)
    files = [f for f in _listing("deb", _SQL_12) if not f.endswith("/provider_gemini.bc")]
    monkeypatch.setattr(m, "client", _Client(_Container(lambda cmd: (0, "\n".join(files) + "\n"))))
    outcome, msg = _outcome(m.test_verify_bundled_files, "debian13-amd64", "deb")
    assert outcome == "failed" and "provider_gemini.bc" in msg
