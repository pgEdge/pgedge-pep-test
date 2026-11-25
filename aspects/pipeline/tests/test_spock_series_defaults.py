"""Spock series selection: Spock 5 is the default, Spock 6 (beta) is opt-in, and release
certification takes the series from the requested package.

Policy (agreed for the repair of main): ordinary runs install/verify Spock 5 (SPOCK_MAJOR=50) on
every PG major, PG19 included, until an explicit later decision; Spock 6 runs only when chosen with
SPOCK_MAJOR=60 or run_pep_tf.sh --spock 60. Both series keep their own configured versions. A
release request is authoritative: its package decides the series and SPOCK_MAJOR can never
redirect it. These tests read the shipped config files and load the component tests by path (with
docker.from_env and load_dotenv neutralized) without changing them.
"""
import importlib.util
import itertools
import os
import re
import sys

import docker
import dotenv
import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
_CONFIG = os.path.join(_ROOT, "configuration", "config%s.env")
_RUNNER = os.path.join(_ROOT, "run_pep_tf.sh")
_counter = itertools.count()
_PG_MAJORS = ["16", "17", "18", "19"]


def _exports(pg):
    """``{name: value}`` of the ``export NAME=value`` lines of config<pg>.env."""
    out = {}
    with open(_CONFIG % pg) as f:
        for line in f:
            m = re.match(r"\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.rstrip("\n"))
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def _load(monkeypatch, filename, env):
    """Load a fresh copy of component-test/<filename> under exactly ``env`` (None values unset)."""
    for k in list(os.environ):
        if k.startswith("PEP_") or k in ("SPOCK_MAJOR", "PLATFORM_FILTER", "PG_MAJOR_VERSION"):
            monkeypatch.delenv(k)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(docker, "from_env", lambda *a, **k: None)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    name = "spock_series_%s_%d" % (filename.replace(".", "_"), next(_counter))
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, "component-test", filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- configuration
@pytest.mark.parametrize("pg", _PG_MAJORS)
def test_every_config_defaults_to_spock_5_and_keeps_both_series_configured(pg):
    env = _exports(pg)
    assert env["SPOCK_MAJOR"] == "50", "config%s.env must default to Spock 5" % pg
    assert env["PG_MAJOR_VERSION"] == pg
    for series in ("50", "60"):                      # each series keeps its own configured version
        value = env.get("PGEDGE_SPOCK%s_%s_VERSION" % (series, pg), "")
        assert value and value.split(".")[0] == series[0], (series, value)


@pytest.mark.parametrize("pg", _PG_MAJORS)
def test_config_comments_never_claim_a_spock_6_default(pg):
    with open(_CONFIG % pg) as f:
        text = f.read()
    assert not re.search(r"default\s+(is\s+)?60|SPOCK_MAJOR=60", text), "config%s.env" % pg
    assert "Spock 6" in text and "beta" in text and "--spock 50|60" in text


# --------------------------------------------------------------------------- code fallbacks
# Every component test that reads SPOCK_MAJOR, and its attribute holding the selected series.
_READERS = [("test_pep_spock.py", "spock_major"), ("test_spock_patroni_failover.py", "spock_major"),
            ("test_integration_zerodowntime.py", "spock_major"), ("test_pep_ace.py", "ace_spock_major")]


@pytest.mark.parametrize("pg", _PG_MAJORS)
@pytest.mark.parametrize("filename,attr", _READERS, ids=[f for f, _ in _READERS])
def test_an_unset_spock_major_means_spock_5(monkeypatch, filename, attr, pg):
    m = _load(monkeypatch, filename, {"PG_MAJOR_VERSION": pg})
    assert getattr(m, attr) == "50"


@pytest.mark.parametrize("filename,attr", _READERS, ids=[f for f, _ in _READERS])
@pytest.mark.parametrize("series", ["50", "60"])
def test_an_explicit_spock_major_is_honoured(monkeypatch, filename, attr, series):
    m = _load(monkeypatch, filename, {"PG_MAJOR_VERSION": "19", "SPOCK_MAJOR": series})
    assert getattr(m, attr) == series


@pytest.mark.parametrize("pg", _PG_MAJORS)
def test_spock_component_test_installs_the_selected_series_packages(monkeypatch, pg):
    m = _load(monkeypatch, "test_pep_spock.py", {"PG_MAJOR_VERSION": pg})
    assert m.get_spock_pkg_name("rhel") == "pgedge-spock50_%s" % pg
    assert m.get_spock_pkg_name("deb") == "pgedge-postgresql-%s-spock50" % pg
    m = _load(monkeypatch, "test_pep_spock.py", {"PG_MAJOR_VERSION": pg, "SPOCK_MAJOR": "60"})
    assert m.get_spock_pkg_name("rhel") == "pgedge-spock60_%s" % pg
    assert m.get_spock_pkg_name("deb") == "pgedge-postgresql-%s-spock60" % pg


def test_zerodowntime_uses_the_enterprise_meta_package_unless_spock_6_is_chosen(monkeypatch):
    assert _load(monkeypatch, "test_integration_zerodowntime.py",
                 {"PG_MAJOR_VERSION": "18"}).use_spock_customize is False
    assert _load(monkeypatch, "test_integration_zerodowntime.py",
                 {"PG_MAJOR_VERSION": "18", "SPOCK_MAJOR": "60"}).use_spock_customize is True


def test_patroni_failover_picks_the_zodan_script_of_the_selected_series(monkeypatch):
    m = _load(monkeypatch, "test_spock_patroni_failover.py", {"PG_MAJOR_VERSION": "18"})
    assert m.spock_major == "50" and "600" not in m._default_zodan_by_major[m.spock_major]
    m = _load(monkeypatch, "test_spock_patroni_failover.py", {"PG_MAJOR_VERSION": "18", "SPOCK_MAJOR": "60"})
    assert m._default_zodan_by_major[m.spock_major] == "zodan-600.sql"


# --------------------------------------------------------------------------- run_pep_tf.sh --spock
def test_runner_spock_option_overrides_the_config_after_it_is_sourced():
    with open(_RUNNER) as f:
        lines = f.read().splitlines()
    option = next(i for i, l in enumerate(lines) if l.strip() == "--spock)")
    assert lines[option + 1].strip() == 'SPOCK_OVERRIDE="$2"'
    sourced = next(i for i, l in enumerate(lines) if l.strip() == 'source "$envfile"')
    export = next(i for i, l in enumerate(lines) if l.strip() == 'export SPOCK_MAJOR="$SPOCK_OVERRIDE"')
    assert export > sourced                          # the CLI choice wins over the config's 50
    assert lines[export - 1].strip() == 'if [[ -n "$SPOCK_OVERRIDE" ]]; then'   # only when given
    assert not any(re.search(r"SPOCK_MAJOR=\$?\{?[^\"$]*60", l) for l in lines)  # no hidden 60 default


# --------------------------------------------------------------------------- release requests
_REQUEST = {"PEP_INTEGRATION_MODE": "1", "PEP_COMPONENT": "spock", "PEP_CHANNEL": "staging",
            "PEP_FAMILY": "rpm", "PEP_ARCH_FILTER": "amd64", "PEP_CONTAINER_ALIAS": "rocky9-amd64",
            "PEP_EXPECTED_BUILDNUM": "1", "REPO": "staging", "CONTAINERS": "rocky9-amd64",
            "DEB_CONTAINERS": "debian13-amd64"}


@pytest.mark.parametrize("pg", ["16", "17", "18"])
@pytest.mark.parametrize("series,version,env_major", [
    ("60", "6.0.0", "50"), ("60", "6.0.0", None), ("50", "5.0.14", "60"), ("50", "5.0.14", None)],
    ids=["spock6-env-50", "spock6-env-unset", "spock5-env-60", "spock5-env-unset"])
def test_a_release_request_tests_its_own_series_whatever_spock_major_says(monkeypatch, pg, series, version,
                                                                         env_major):
    pkg = "pgedge-spock%s_%s" % (series, pg)
    env = dict(_REQUEST, PEP_PACKAGE_NAME=pkg, PEP_EXPECTED_VERSION=version, PG_MAJOR_VERSION=pg,
               PEP_EXPECTED_RPM="%s-1.el9" % version, SPOCK_MAJOR=env_major)
    m = _load(monkeypatch, "test_pep_spock.py", env)
    assert m.INTEGRATION_PROBLEM is None
    assert m.INTEGRATION_SERIES == series and m.spock_major == series
    assert m.get_spock_pkg_name("rhel") == pkg      # never redirected to the standalone default


def test_ace_certification_keeps_its_spock_5_prerequisite_whatever_spock_major_says(monkeypatch):
    env = dict(_REQUEST, PEP_COMPONENT="ace", PEP_PACKAGE_NAME="pgedge-ace", PEP_EXPECTED_VERSION="3.0.1",
               PEP_EXPECTED_RPM="3.0.1-1.el9", PG_MAJOR_VERSION="17", SPOCK_MAJOR="60")
    m = _load(monkeypatch, "test_pep_ace.py", env)
    assert m.INTEGRATION_REQUEST is not None and m.ace_spock_major == "50"
