"""End-to-end bridge tests for run_pep_tf.sh integration mode (Task 3 Step 6).

Exercises the REAL shell bridge via subprocess in --dry-run (Docker-free): a
complete valid request previews cleanly (exit 0) and writes resolved-config.json;
an upgrade scenario and a bad channel are rejected with exit 3. Also covers the
manifest-writer snippet directly. Stdlib + pytest only; no Docker required."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "run_pep_tf.sh"

_BASE_ARGS = [
    "--integration",
    "--repo", "daily",
    "--expected-version", "1.0.0",
    "--components", "rag",
    "--package-name", "pgedge-rag-server",
    "--pgver", "17",
    "--platforms", "rpm",
    "--arch", "amd64",
    "--containers", "rocky9-amd64",
    "--scenario", "certification",
    "--dry-run",
]

_needs_tools = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="bash and python3 are required for the bridge subprocess tests",
)


def _run(args, extra_env=None):
    env = None
    if extra_env is not None:
        env = {**os.environ, **extra_env}
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, env=env,
    )


@_needs_tools
def test_valid_request_dry_run_exits_0(tmp_path):
    resolved = _REPO_ROOT / "test-logs" / "resolved-config.json"
    if resolved.exists():
        resolved.unlink()
    proc = _run(_BASE_ARGS)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    # dry-run line reflects the resolver-decided channel
    assert "repo=daily" in proc.stdout, proc.stdout
    # resolved-config.json provenance
    assert resolved.exists(), "resolved-config.json was not written"
    data = json.loads(resolved.read_text())
    assert data["repo"]["value"] == "daily"
    assert data["repo"]["source"] == "caller"
    assert "scenario" in data


@_needs_tools
def test_upgrade_scenario_rejected_exit_3():
    args = [a for a in _BASE_ARGS]
    i = args.index("certification")
    args[i] = "upgrade"
    proc = _run(args)
    assert proc.returncode == 3, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert "upgrade" in combined.lower()


@_needs_tools
def test_integration_bad_repo_rejected_by_allowlist_exit_3():
    # In integration mode the top-level --repo allowlist gate fires BEFORE the
    # resolver runs, so this exercises that gate (not the resolver's
    # defense-in-depth validation). Both paths return exit 3.
    args = [a for a in _BASE_ARGS]
    i = args.index("daily")
    args[i] = "bogus"
    proc = _run(args)
    assert proc.returncode == 3, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


@_needs_tools
def test_standalone_run_triggers_no_integration_behavior():
    # Guards the top-priority invariant: a non-integration run must not trigger
    # ANY integration behavior. Remove any integration artifacts left by other
    # tests, run a pure standalone dry-run (NO integration flags), and assert
    # this run created none of them. Relies on serial execution of the unit
    # layer, which is how the plan runs it: `pytest utillities/test_pep_*.py`.
    for f in ("test-logs/resolved-config.json", "test-logs/current-run.json"):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass
    proc = _run(["--pgver", "17", "--platforms", "rpm", "--components", "rag", "--dry-run"])
    assert proc.returncode == 0, proc.stderr
    assert not os.path.exists("test-logs/resolved-config.json"), "standalone wrote resolved-config.json"
    assert not os.path.exists("test-logs/current-run.json"), "standalone wrote current-run.json"
    assert "[integration]" not in (proc.stdout + proc.stderr)
    # PEP_INTEGRATION_MODE must not appear as set in the standalone dry-run output
    assert "PEP_INTEGRATION_MODE" not in (proc.stdout + proc.stderr)


# ── Correction pass: negative bridge cases (findings 1, 3, 4) ────────────────

@_needs_tools
def test_integration_missing_config_pg_rejected_exit_3():
    # Finding 1: in integration mode a requested PG major with no
    # configuration/config{PG}.env must be a VALIDATION rejection (exit 3), not a
    # silent skip that dry-runs to a false-green preview.
    args = [a for a in _BASE_ARGS]
    i = args.index("17")
    args[i] = "99"                       # config99.env does not exist
    proc = _run(args)
    assert proc.returncode == 3, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "config99.env" in (proc.stdout + proc.stderr)


@_needs_tools
def test_standalone_missing_config_pg_still_skips_exit_0():
    # Finding 1 guard: standalone (non-integration) behavior is UNCHANGED — a
    # missing config file is skipped and the run exits 0.
    proc = _run(["--pgver", "99", "--platforms", "rpm", "--components", "rag", "--dry-run"])
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "Skipping missing environment file" in proc.stdout


@_needs_tools
def test_inherited_optional_identity_var_does_not_contaminate():
    # Finding 3: an inherited PEP_EXPECTED_DEB in the parent environment must NOT
    # leak into a request that omitted --expected-deb. If it leaked, the adapter
    # would see expected_deb on an rpm-family target -> RequestError -> exit 3.
    # Correct behavior unsets it, so the clean rpm request still previews (exit 0).
    proc = _run(_BASE_ARGS, extra_env={"PEP_EXPECTED_DEB": "9.9.9-1.noble"})
    assert proc.returncode == 0, (
        "inherited PEP_EXPECTED_DEB contaminated the request\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


@_needs_tools
def test_integration_bad_arch_rejected_exit_3():
    # Finding 4: an invalid --arch is a request-validation rejection in
    # integration mode (exit 3), not the legacy infra exit 2.
    args = [a for a in _BASE_ARGS]
    i = args.index("amd64")
    args[i] = "sparc"
    proc = _run(args)
    assert proc.returncode == 3, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


@_needs_tools
def test_standalone_bad_arch_still_exit_2():
    # Finding 4 guard: standalone invalid --arch keeps the legacy exit 2.
    proc = _run(["--pgver", "17", "--platforms", "rpm", "--components", "rag",
                 "--arch", "sparc", "--dry-run"])
    assert proc.returncode == 2, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


@_needs_tools
def test_enforcement_mode_gate_accepted_exit_0():
    # Finding 4: the workflow's enforcement mode reaches the normalized request.
    # A valid --mode gate is accepted and previews cleanly.
    proc = _run(_BASE_ARGS + ["--mode", "gate"])
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


@_needs_tools
def test_enforcement_mode_invalid_rejected_exit_3():
    # Finding 4: an invalid --mode is rejected by normalize_request (exit 3).
    proc = _run(_BASE_ARGS + ["--mode", "bogus"])
    assert proc.returncode == 3, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


# ── Manifest-writer snippet (Step 5 boundary coverage) ───────────────────────

_MANIFEST_SNIPPET = textwrap.dedent('''\
    import json, sys
    from pathlib import Path
    Path("test-logs").mkdir(exist_ok=True)
    Path("test-logs/current-run.json").write_text(
        json.dumps({"report_dir": sys.argv[1], "reports": sys.argv[2:]}, indent=2))
''')


def _run_manifest(cwd, path_args):
    subprocess.run(
        [sys.executable, "-c", _MANIFEST_SNIPPET, *path_args],
        cwd=str(cwd), check=True,
    )
    return json.loads((Path(cwd) / "test-logs" / "current-run.json").read_text())


def test_manifest_empty_reports(tmp_path):
    data = _run_manifest(tmp_path, ["test-logs/consolidated-X"])
    assert data == {"report_dir": "test-logs/consolidated-X", "reports": []}


def test_manifest_with_two_reports(tmp_path):
    data = _run_manifest(tmp_path, ["test-logs/consolidated-X", "a.xml", "b.xml"])
    assert data["report_dir"] == "test-logs/consolidated-X"
    assert data["reports"] == ["a.xml", "b.xml"]
    # re-parses cleanly
    json.dumps(data)
