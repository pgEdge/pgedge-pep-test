"""The package version that component-test/conftest.py puts in a run's report: the pytest-html
metadata (pytest_configure) and the visible title (pytest_html_report_title).

In integration mode (PEP_INTEGRATION_MODE=1, exported by run_pep_tf.sh with the request) the run
installs and checks the request's PEP_EXPECTED_VERSION, so both must name that version, not the
standalone config value. Standalone runs keep the config variable and its default. Versions are
synthetic and the config variable names come from conftest's own component map, so nothing here
depends on today's releases or config files.
"""
import importlib.util
import itertools
import os
import sys

import dotenv
import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "component-test", "conftest.py")
_counter = itertools.count()

REQUESTED = "9.8.7"        # what an integration request asks for
CONFIGURED = "1.2.3-cfg"   # what a standalone config file names

# Representative components: flat (radar), PG-coupled extensions with a map entry (snowflake,
# safesession), Spock (config variable built from SPOCK_MAJOR and the PG major) and components
# without a map entry (rag, ace), which fall back to the generic COMPONENT_VERSION entry.
COMPONENTS = ["test_pep_radar.py", "test_pep_snowflake.py", "test_pep_safesession.py",
              "test_pep_spock.py", "test_pep_rag.py", "test_pep_ace.py"]


@pytest.fixture
def conftest(monkeypatch):
    """A fresh copy of component-test/conftest.py that never reads a local .env file."""
    for k in ("PEP_INTEGRATION_MODE", "PEP_EXPECTED_VERSION", "AWS_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_MAJOR_VERSION", "17")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    name = "component_conftest_under_test_%d" % next(_counter)
    spec = importlib.util.spec_from_file_location(name, _PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Config:
    def __init__(self, test_file):
        self.args = ["component-test/" + test_file]
        self._metadata = {}


class _Report:
    def __init__(self, config):
        self.config, self.title = config, None


def _report(mod, test_file):
    """(metadata 'Package Version', title) exactly as the two hooks produce them."""
    config = _Config(test_file)
    mod.pytest_configure(config)
    report = _Report(config)
    mod.pytest_html_report_title(report)
    return config._metadata["Package Version"], report.title


def _config_var(mod, test_file):
    return mod.detect_component_from_test_file(_Config(test_file))


@pytest.mark.parametrize("test_file", COMPONENTS)
def test_integration_reports_name_the_requested_version_not_the_config_version(conftest, monkeypatch, test_file):
    info = _config_var(conftest, test_file)
    monkeypatch.setenv(info["version_env"], CONFIGURED)
    monkeypatch.setenv("PEP_INTEGRATION_MODE", "1")
    monkeypatch.setenv("PEP_EXPECTED_VERSION", REQUESTED)
    assert _report(conftest, test_file) == (REQUESTED, "%s %s Test Report (PostgreSQL 17)" % (info["name"], REQUESTED))


@pytest.mark.parametrize("test_file", COMPONENTS)
def test_standalone_reports_keep_the_config_version(conftest, monkeypatch, test_file):
    info = _config_var(conftest, test_file)
    monkeypatch.setenv(info["version_env"], CONFIGURED)
    # An inherited request version without the integration marker is not an integration run.
    monkeypatch.setenv("PEP_EXPECTED_VERSION", REQUESTED)
    assert _report(conftest, test_file) == (CONFIGURED, "%s %s Test Report (PostgreSQL 17)" % (info["name"], CONFIGURED))


@pytest.mark.parametrize("test_file", COMPONENTS)
def test_standalone_reports_keep_the_config_default_when_the_variable_is_unset(conftest, monkeypatch, test_file):
    info = _config_var(conftest, test_file)
    monkeypatch.delenv(info["version_env"], raising=False)
    default = info["version_default"]
    assert _report(conftest, test_file) == (default, "%s %s Test Report (PostgreSQL 17)" % (info["name"], default))
