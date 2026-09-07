"""Unit coverage for the integration-mode resolver CLI's scenario policy.

The pure resolver (pep_resolve.py) only walks precedence. The certification
"no upgrade" policy lives in pep_resolve_cli._resolve() so the EFFECTIVE upgrade
decision written to resolved-config.json is the same one the runtime consumes
(no drift). These tests pin that policy directly, Docker-free. Stdlib + pytest."""
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(_HERE / filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cli = _load("pep_resolve_cli", "pep_resolve_cli.py")

_RESOLVER_ENV = (
    "PEP_CALLER_REPO", "PEP_CONFIG_REPO", "PEP_CALLER_SCENARIO", "PEP_CFG_UPGRADE",
)


def _clear(monkeypatch):
    for k in _RESOLVER_ENV:
        monkeypatch.delenv(k, raising=False)


def test_default_certification_forces_effective_upgrade_false(monkeypatch):
    # config says upgrade=true; scenario defaults to certification -> the EFFECTIVE
    # upgrade is false, sourced from policy (not the config value).
    _clear(monkeypatch)
    monkeypatch.setenv("PEP_CONFIG_REPO", "staging")
    monkeypatch.setenv("PEP_CFG_UPGRADE", "true")
    out = cli._resolve()
    assert out["scenario"]["value"] == "certification"
    assert out["upgrade"] == {"value": "false", "source": "scenario_policy"}


def test_explicit_certification_forces_effective_upgrade_false(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PEP_CALLER_SCENARIO", "certification")
    monkeypatch.setenv("PEP_CFG_UPGRADE", "true")
    out = cli._resolve()
    assert out["upgrade"] == {"value": "false", "source": "scenario_policy"}


def test_certification_policy_wins_even_when_config_absent(monkeypatch):
    # No configured upgrade at all: the effective value is still policy-sourced
    # false, so the source is always honest about WHERE the decision came from.
    _clear(monkeypatch)
    out = cli._resolve()
    assert out["upgrade"] == {"value": "false", "source": "scenario_policy"}


def test_non_certification_scenario_keeps_configured_upgrade(monkeypatch):
    # The policy is scenario-gated, NOT a blanket override: a non-certification
    # scenario preserves the configured value + provenance. (The upgrade scenario
    # is rejected later in the bridge, but the resolver must not mislabel it here.)
    _clear(monkeypatch)
    monkeypatch.setenv("PEP_CALLER_SCENARIO", "upgrade")
    monkeypatch.setenv("PEP_CFG_UPGRADE", "true")
    out = cli._resolve()
    assert out["upgrade"] == {"value": "true", "source": "config_pg"}
