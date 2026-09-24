"""Structural contract + planning tests for the reusable published-package replay.

Stdlib-only (no PyYAML, matching test_pep_certify_workflow.py). The workflow part
proves the WIRING contract (workflow_call-only, pinned actions, separate per-family
matrices, stable [pep-cell:] markers, absolute pep-certify pin, least privilege). The
planning part proves the full-mode replay evidence for the 14-cell RAG shape plans to
30 invocations and 4 coverage gaps through the REAL reducer + invocation planner.
"""
import json
import re
import tempfile
from pathlib import Path

import pep_cert_plan as P
import pep_invocation_plan as I
from pep_cert_plan import _expected_native

_REPO = Path(__file__).resolve().parent.parent
_WF = _REPO / ".github" / "workflows" / "pep-published-replay.yml"
_TEXT = _WF.read_text()
_CERT_PIN = "a448072edfbcf0acccfb86cc6da9e6995b50d0fc"


def _job_block(job):
    out, capturing = [], False
    for ln in _TEXT.splitlines(keepends=True):
        if re.match(r"^  %s:\s*$" % re.escape(job), ln):
            capturing = True
            out.append(ln)
            continue
        if capturing:
            if re.match(r"^  [A-Za-z0-9_-]+:\s*$", ln):
                break
            out.append(ln)
    return "".join(out)


def _on_block():
    out, capturing = [], False
    for ln in _TEXT.splitlines(keepends=True):
        if re.match(r"^on:\s*$", ln):
            capturing = True
            continue
        if capturing:
            if re.match(r"^[A-Za-z0-9_-]+:", ln):
                break
            out.append(ln)
    return "".join(out)


# --- trigger / topology -----------------------------------------------------
def test_is_workflow_call_only():
    assert re.search(r"^on:\s*$", _TEXT, re.M)
    on = _on_block()
    assert "workflow_call:" in on
    for forbidden in ("push:", "pull_request:", "workflow_dispatch:", "schedule:", "repository_dispatch:"):
        assert forbidden not in on, "trigger %s must not be present" % forbidden


def test_top_level_least_privilege():
    assert re.search(r"^permissions:\s*\{\}\s*$", _TEXT, re.M)


def test_report_job_has_no_permissions():
    assert re.search(r"^    permissions:\s*\{\}\s*$", _job_block("report"), re.M)


# --- separate per-family matrices + stable markers --------------------------
def test_separate_rpm_deb_matrices():
    assert "fromJSON(inputs.rpm_matrix)" in _job_block("retrieve-rpm")
    assert "fromJSON(inputs.deb_matrix)" in _job_block("retrieve-deb")


def test_stable_pep_cell_markers_in_job_names():
    assert "[pep-cell:${{ matrix.cell_id }}]" in _job_block("retrieve-rpm")
    assert "[pep-cell:${{ matrix.cell_id }}]" in _job_block("retrieve-deb")


# --- selective families: validate-before-expand + presence gating -----------
def test_plan_job_validates_matrices_and_emits_presence():
    pj = _job_block("plan")
    assert "pep_replay_reconcile.py validate" in pj
    assert "has_rpm:" in pj and "has_deb:" in pj


def test_plan_job_validates_release_identity_before_fanout():
    # The plan job must carry the release-identity env so malformed identity fails
    # BEFORE any retrieval fan-out (the validate step reuses the identity validator).
    pj = _job_block("plan")
    for var in ("LOGICAL_COMPONENT:", "VERSION:", "BUILDNUM:", "TAG:", "CHANNEL:"):
        assert var in pj, var


def test_retrieval_jobs_gated_on_family_presence():
    assert "needs.plan.outputs.has_rpm == 'true'" in _job_block("retrieve-rpm")
    assert "needs.plan.outputs.has_deb == 'true'" in _job_block("retrieve-deb")


def test_reconcile_consumes_matrix_job_results():
    rj = _job_block("reconcile")
    assert "RPM_JOB_RESULT: ${{ needs.retrieve-rpm.result }}" in rj
    assert "DEB_JOB_RESULT: ${{ needs.retrieve-deb.result }}" in rj


def test_retrieval_matrices_are_not_fail_fast():
    for job in ("retrieve-rpm", "retrieve-deb"):
        assert re.search(r"fail-fast:\s*false", _job_block(job)), job


# --- pins -------------------------------------------------------------------
def test_absolute_pep_certify_pin():
    cj = _job_block("certify")
    assert ("uses: pgEdge/pgedge-pep-test/.github/workflows/pep-certify.yml@" + _CERT_PIN) in cj
    assert "enforcement: observe" in cj
    assert "execution_mode: full" in cj


def test_receipt_action_pinned_absolute():
    assert _TEXT.count(
        "uses: pgEdge/pgedge-pep-test/.github/actions/pep-package-receipt@" + _CERT_PIN) == 2


def test_every_uses_is_pinned_by_40hex_sha():
    for m in re.finditer(r"uses:\s*(\S+)", _TEXT):
        ref = m.group(1)
        assert "@" in ref, "unpinned action: %s" % ref
        sha = ref.split("@", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{40}", sha), "not a 40-hex pin: %s" % ref


# --- gating -----------------------------------------------------------------
def test_job_gating_expressions():
    assert re.search(r"if:\s*\$\{\{\s*always\(\)\s*\}\}", _job_block("reconcile"))
    assert "needs.reconcile.result == 'success'" in _job_block("certify")
    assert "needs.certify.result != 'skipped'" in _job_block("accept")


def test_replay_is_labelled_not_a_release():
    assert "NOT A RELEASE" in _TEXT


# --- planning fixture: 14 RAG cells -> 30 invocations + 4 coverage gaps ------
# The canonical RAG detector cells (component_name: pkg -> decoupled '.pkg').
_RAG_CELLS = [
    ("rpm", "el-9", "amd64"), ("rpm", "el-9", "arm64"),
    ("rpm", "el-10", "amd64"), ("rpm", "el-10", "arm64"),
    ("deb", "bookworm", "amd64"), ("deb", "bookworm", "arm64"),
    ("deb", "jammy", "amd64"), ("deb", "jammy", "arm64"),
    ("deb", "noble", "amd64"), ("deb", "noble", "arm64"),
    ("deb", "resolute", "amd64"), ("deb", "resolute", "arm64"),
    ("deb", "trixie", "amd64"), ("deb", "trixie", "arm64"),
]
_RPM_NATIVE = {"amd64": "x86_64", "arm64": "aarch64"}


def _full_mode_reducer_input():
    planned, jobs, arts = [], [], []
    for i, (fam, os_tok, arch) in enumerate(_RAG_CELLS):
        cid = "pepcell.v1.%s.%s.%s.pkg" % (fam, os_tok, arch)
        exp_v, exp_r = _expected_native(fam, os_tok, "2.0.0", "1")
        nat = _RPM_NATIVE[arch] if fam == "rpm" else arch
        planned.append({"cell_id": cid, "artifact_name": cid, "family": fam,
                        "os": os_tok, "normalized_arch": arch})
        jobs.append({"cell_id": cid, "job_id": 1000 + i, "run_attempt": 1,
                     "status": "completed", "conclusion": "success"})
        arts.append({"name": cid, "id": 2000 + i, "members": [{
            "package_name": "pgedge-rag-server2", "epoch": None, "version": exp_v,
            "release": exp_r, "native_arch": nat, "package_class": "runtime",
            "sha256": "a" * 64, "artifact_member_path": "pkg"}]})
    return {
        "release_intent": {"logical_component": "rag", "intended_version": "2.0.0",
                           "intended_buildnum": "1", "effective_tag": "v2.0.0",
                           "channel": "staging", "simulated": False},
        "component_policy": {"allowed_runtime_package_names": ["pgedge-rag-server2", "pgedge-rag-server"],
                             "expected_binary_version": ""},
        "provenance": {"repository": "pgEdge/pgedge-rag-server", "run_id": 1, "run_attempt": 1,
                       "ref": "x", "sha": "y", "pep_implementation_ref": "z", "pep_resolved_sha": "z"},
        "planned_cells": planned, "job_records": jobs, "artifacts": arts,
        "publication_results": {"rpm": "success", "deb": "success"}, "execution_mode": "full",
    }


def test_full_mode_plan_has_14_eligible_targets():
    plan = json.loads(P.to_json(P.reduce(_full_mode_reducer_input())))
    assert plan["plan_resolved"] is True
    assert plan["coverage_denominators"]["eligible_targets"] == 14
    assert plan["coverage_denominators"]["preview_eligible_targets"] == 0


def test_full_mode_plans_to_30_invocations_and_4_gaps():
    plan = json.loads(P.to_json(P.reduce(_full_mode_reducer_input())))
    with tempfile.TemporaryDirectory() as d:
        cpf = Path(d) / "cert-plan.json"
        outf = Path(d) / "invocation-plan.json"
        cpf.write_text(json.dumps(plan))
        rc = I.main(["--cert-plan", str(cpf),
                     "--exec-catalog", str(_REPO / "utillities" / "pep_exec_catalog.json"),
                     "--containers", str(_REPO / "configuration" / "containers_list.json"),
                     "--execution-mode", "full", "--out", str(outf)])
        assert rc == 0
        ip = json.loads(outf.read_text())
    assert len(ip["matrix"]["include"]) == 30
    assert len(ip["coverage_gaps"]) == 4
    assert all(g["reason"] == "no_enabled_platform" for g in ip["coverage_gaps"])
