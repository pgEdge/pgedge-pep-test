"""Unit + CLI tests for pep_request_env (Task 3 Step 0). Docker-free, stdlib."""
from __future__ import annotations
import importlib.util as _ilu
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_MODULE = _HERE / "pep_request_env.py"

_spec = _ilu.spec_from_file_location("pep_request_env", str(_MODULE))
_env_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_env_mod)

_pr = _env_mod._pr  # the pep_request module loaded by the adapter


def _full_env():
    """A complete, valid PEP_* mapping (marker set)."""
    return {
        "PEP_INTEGRATION_MODE": "1",
        "PEP_COMPONENT": "rag",
        "PEP_PACKAGE_NAME": "pgedge-rag-server",
        "PEP_CHANNEL": "daily",
        "PEP_EXPECTED_VERSION": "1.0.0",
        "PEP_FAMILY": "rpm",
        "PEP_ARCH_FILTER": "amd64",
        "PG_MAJOR_VERSION": "17",
        "PEP_CONTAINER_ALIAS": "rocky9-amd64",
    }


def test_returns_none_when_marker_unset():
    env = _full_env()
    del env["PEP_INTEGRATION_MODE"]
    assert _env_mod.build_request_from_env(env) is None


def test_returns_normalized_request_from_full_env():
    req = _env_mod.build_request_from_env(_full_env())
    assert req is not None
    assert req["component"] == "rag"
    assert req["package_name"] == "pgedge-rag-server"
    assert req["channel"] == "daily"
    assert req["expected_version"] == "1.0.0"
    assert req["family"] == "rpm"
    assert req["pg_major"] == "17"
    # defaults applied
    assert req["scenario"] == "certification"
    assert req["mode"] == "observe"


def test_raises_on_missing_required_field():
    env = _full_env()
    del env["PEP_EXPECTED_VERSION"]
    with pytest.raises(_pr.RequestError):
        _env_mod.build_request_from_env(env)


def test_raises_on_invalid_enum():
    env = _full_env()
    env["PEP_CHANNEL"] = "bogus"
    with pytest.raises(_pr.RequestError):
        _env_mod.build_request_from_env(env)


# ── CLI-level tests via subprocess (exit-code contract) ──────────────────────

def _run_cli(env):
    return subprocess.run(
        [sys.executable, "utillities/pep_request_env.py"],
        cwd=str(_REPO_ROOT), env=env,
        capture_output=True, text=True,
    )


def test_cli_valid_env_exits_0():
    proc = _run_cli(_full_env())
    assert proc.returncode == 0, proc.stderr


def test_cli_invalid_env_exits_3():
    env = _full_env()
    env["PEP_PACKAGE_NAME"] = "wrong-package"  # mismatched pair
    proc = _run_cli(env)
    assert proc.returncode == 3, proc.stderr


def test_cli_missing_field_exits_3():
    env = _full_env()
    del env["PEP_CONTAINER_ALIAS"]
    proc = _run_cli(env)
    assert proc.returncode == 3, proc.stderr


def test_cli_marker_unset_exits_3():
    env = _full_env()
    del env["PEP_INTEGRATION_MODE"]
    proc = _run_cli(env)
    assert proc.returncode == 3, proc.stderr
