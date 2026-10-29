"""Repository client certificate in pep-integration.yml: honest infrastructure classification.

A full run needs the pgEdge repository client certificate. Without a complete pair the
repositories stay disabled, so every install would fail and, under observe, read as a product
failure. These tests run the workflow's real step scripts (the certificate check and Summarize)
in bash, with a stub ``python3`` that records the classification flag Summarize chooses, so they
pin behaviour rather than wording:

  * a full run with a missing or incomplete pair is an infrastructure failure, and the framework
    does not run;
  * a certificate or repository-refresh failure inside the framework (recorded by the aspects as a
    one-word marker) is an infrastructure failure;
  * a full run with a complete pair and no marker keeps the product verdict;
  * preview needs no certificate and still classifies as preview.

Synthetic values only; nothing here touches a real credential, network or container.
"""
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_WF_TEXT = (_REPO / ".github" / "workflows" / "pep-integration.yml").read_text()
_CRT = "-----BEGIN CERTIFICATE-----\nc3ludGhldGljLWNlcnQ=\n-----END CERTIFICATE-----"
_KEY = "-----BEGIN PRIVATE KEY-----\nc3ludGhldGljLWtleQ==\n-----END PRIVATE KEY-----"


def _step(name_prefix):
    """The text of one step of the ``run`` job, from ``- name: <prefix>`` to the next step."""
    out, capturing = [], False
    for ln in _WF_TEXT.splitlines(keepends=True):
        if re.match(r"^      - name: '?%s" % re.escape(name_prefix), ln):
            capturing = True
        elif capturing and re.match(r"^      - (name|uses):", ln):
            break
        if capturing:
            out.append(ln)
    assert out, "step not found: %s" % name_prefix
    return "".join(out)


def _script(step_text):
    """The step's ``run: |`` block, dedented, as bash would receive it."""
    body = step_text.split("        run: |\n", 1)[1]
    lines = []
    for ln in body.splitlines(keepends=True):
        if ln.strip() and not ln.startswith("          "):
            break
        lines.append(ln)
    return textwrap.dedent("".join(lines))


def _run(script, env, cwd):
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                          env=env, cwd=cwd, capture_output=True, text=True)


def check_certificate(tmp_path, crt, key):
    out = tmp_path / "github_output"
    out.write_text("")
    env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out),
           "PGEDGE_REPO_CLIENT_CRT": crt, "PGEDGE_REPO_CLIENT_KEY": key}
    proc = _run(_script(_step("[repo-auth] check client certificate")), env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert crt.strip() == "" or crt.strip() not in proc.stdout + proc.stderr
    assert key.strip() == "" or key.strip() not in proc.stdout + proc.stderr + out.read_text()
    return dict(ln.split("=", 1) for ln in out.read_text().splitlines() if "=" in ln)


@pytest.mark.parametrize("crt,key,state", [
    (_CRT, _KEY, "complete"),
    ("", "", "missing"),
    ("  \n", "\t", "missing"),
    (_CRT, "", "incomplete"),
    ("", _KEY, "incomplete"),
    (_CRT, " \n ", "incomplete"),
])
def test_the_certificate_check_classifies_the_pair(tmp_path, crt, key, state):
    assert check_certificate(tmp_path, crt, key) == {"state": state}


def test_the_certificate_check_runs_only_in_full_mode_and_reads_only_the_pair():
    step = _step("[repo-auth] check client certificate")
    assert "id: repo_auth" in step
    assert "if: ${{ inputs.execution_mode == 'full' }}" in step
    assert re.findall(r"secrets\.([A-Z_]+)", step) == ["PGEDGE_REPO_CLIENT_CRT", "PGEDGE_REPO_CLIENT_KEY"]


def test_a_full_run_starts_the_framework_only_with_a_complete_pair():
    fw = _step("Run framework (preview or full)")
    assert ("if: ${{ steps.preflight.outputs.rc == '0' && "
            "(inputs.execution_mode != 'full' || steps.repo_auth.outputs.state == 'complete') }}") in fw
    # The pair still reaches the framework only in full mode.
    assert "PGEDGE_REPO_CLIENT_CRT: ${{ inputs.execution_mode == 'full' && secrets.PGEDGE_REPO_CLIENT_CRT || '' }}" in fw
    assert "PGEDGE_REPO_CLIENT_KEY: ${{ inputs.execution_mode == 'full' && secrets.PGEDGE_REPO_CLIENT_KEY || '' }}" in fw
    # The aspects record setup failures here; a stale marker never survives into a new run.
    assert "PEP_REPO_AUTH_FAILURE_FILE: ${{ github.workspace }}/test-logs/repo-auth-failure" in fw
    assert 'rm -f "$PEP_REPO_AUTH_FAILURE_FILE"' in fw


def summarize(tmp_path, *, exec_mode="full", fw_code="0", repo_auth="complete", marker=None,
              manifest=True, identity=True, docker="success"):
    """Run the real Summarize script; return the flags it passed to pep_result_summary.py."""
    stub = tmp_path / "bin"
    stub.mkdir()
    record = tmp_path / "args"
    (stub / "python3").write_text('#!/bin/sh\nfor a in "$@"; do printf "%%s\\n" "$a"; done > "%s"\n' % record)
    (stub / "python3").chmod(0o755)
    logs = tmp_path / "test-logs"
    logs.mkdir()
    if manifest:
        (logs / "current-run.json").write_text("{}")
    if identity:
        (logs / "identity-evidence.json").write_text("{}")
    if marker is not None:
        (logs / "repo-auth-failure").write_text(marker + "\n")
    env = {"PATH": "%s:%s" % (stub, os.environ["PATH"]), "IN_MODE": "observe", "IN_EXEC": exec_mode,
           "IN_PREFLIGHT": "0", "IN_INVOCATION_ID_VALID": "", "FW_CODE": fw_code, "FW_TIMED_OUT": "false",
           "DOCKER_OUTCOME": docker, "SETUP_OUTCOME": "success", "PIP_OUTCOME": "success",
           "LOGIN_OUTCOME": "", "REPO_AUTH_STATE": repo_auth}
    proc = _run(_script(_step("Summarize (ALWAYS)")), env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    args = record.read_text().splitlines()
    assert args[0] == "pipeline/pep_result_summary.py"
    flags = [(a, args[i + 1] if a.endswith("-error") else None) for i, a in enumerate(args)
             if a in ("--infra-error", "--validation-error", "--preview")]
    assert len(flags) <= 1, flags
    return flags[0] if flags else None


@pytest.mark.parametrize("state,reason", [
    ("missing", "repository client certificate not supplied (setup)"),
    ("incomplete", "repository client certificate incomplete: certificate or key missing (setup)"),
])
def test_a_full_run_without_a_complete_pair_is_an_infrastructure_failure(tmp_path, state, reason):
    # The framework was skipped, so there is no exit code, manifest or identity evidence.
    assert summarize(tmp_path, fw_code="", repo_auth=state, manifest=False, identity=False) == \
        ("--infra-error", reason)


def test_a_check_that_never_ran_is_not_blamed_on_the_certificate(tmp_path):
    # An earlier step failed or the run was cancelled: the framework did not run either.
    assert summarize(tmp_path, fw_code="", repo_auth="", manifest=False, identity=False) == \
        ("--infra-error", "run_pep_tf.sh failed unexpectedly (rc=<none>)")


@pytest.mark.parametrize("marker,reason", [
    ("certificate", "repository client certificate could not be installed (setup)"),
    ("repository", "package index refresh failed after enabling the client certificate (setup)"),
    ("missing", "repository client certificate not supplied (setup)"),
    ("something-else", "repository authentication setup failed (setup)"),
])
def test_a_certificate_setup_failure_in_the_framework_is_an_infrastructure_failure(tmp_path, marker, reason):
    assert summarize(tmp_path, marker=marker) == ("--infra-error", reason)


def test_a_complete_pair_without_a_setup_failure_keeps_the_product_verdict(tmp_path):
    # No flag: the summarizer parses the manifest and JUnit decides pass or fail.
    assert summarize(tmp_path) is None


@pytest.mark.parametrize("repo_auth", ["", "missing"])
def test_preview_needs_no_certificate(tmp_path, repo_auth):
    assert summarize(tmp_path, exec_mode="preview", repo_auth=repo_auth, manifest=False,
                     identity=False) == ("--preview", None)


def test_earlier_setup_failures_keep_their_classification(tmp_path):
    assert summarize(tmp_path, fw_code="", repo_auth="missing", docker="failure",
                     manifest=False, identity=False) == ("--infra-error", "docker preflight failed (setup)")
