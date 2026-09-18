"""Structural contract test for the reusable coordinator .github/workflows/pep-certify.yml.

Stdlib-only (no PyYAML: the CI unit interpreter is not guaranteed to have it, matching
test_pep_selftest_artifacts.py). This proves the WIRING contract the coordinator must
uphold -- the job graph, immutable-id downloads, dynamic-matrix guard, per-leg mode
forwarding, artifact-only aggregation, fail-closed enforcement, pinned actions and least
privilege -- WITHOUT re-implementing the workflow steps or the policy table.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CERTIFY = _REPO / ".github" / "workflows" / "pep-certify.yml"
_SELFTEST = _REPO / ".github" / "workflows" / "pep-selftest.yml"

_TEXT = _CERTIFY.read_text()


def _job_block(job):
    """The text of one top-level job (from ``  <job>:`` to the next ``  <name>:``)."""
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


def _step_block(job_text, name_prefix):
    """The text of one step within a job (from ``- name: <name_prefix>...`` to the next step)."""
    out, capturing = [], False
    for ln in job_text.splitlines(keepends=True):
        if re.match(r"^      - name: %s" % re.escape(name_prefix), ln):
            capturing = True
            out.append(ln)
            continue
        if capturing:
            if re.match(r"^      - (name|uses):", ln):
                break
            out.append(ln)
    return "".join(out)


# --------------------------------------------------------------------------- #
# public contract: inputs / secrets / outputs
# --------------------------------------------------------------------------- #
def test_workflow_is_reusable():
    assert re.search(r"^on:\s*$", _TEXT, re.M)
    assert "workflow_call:" in _TEXT


def _inputs_block():
    """The text of the workflow_call `inputs:` block (from ``    inputs:`` to the next
    4-space key such as ``    secrets:``)."""
    out, capturing = [], False
    for ln in _TEXT.splitlines(keepends=True):
        if re.match(r"^    inputs:\s*$", ln):
            capturing = True
            continue
        if capturing:
            if re.match(r"^    [A-Za-z0-9_-]+:\s*$", ln):
                break
            out.append(ln)
    return "".join(out)


def test_public_inputs_present():
    for inp in ("rpm_matrix", "deb_matrix", "release_intent",
                "publication_results", "enforcement", "execution_mode"):
        assert re.search(r"^      %s:" % inp, _inputs_block(), re.M), inp


def test_pep_implementation_ref_is_not_a_public_input():
    # the implementation revision is derived from job.workflow_sha, never a caller input
    assert not re.search(r"^      pep_implementation_ref:", _inputs_block(), re.M)


def test_optional_docker_secrets_declared_optional():
    for sec in ("DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"):
        assert re.search(r"^      %s:\s*\{required:\s*false\}" % sec, _TEXT, re.M), sec


def test_useful_outputs_exposed():
    for out in ("certification_conclusion", "certification_state", "execution_status",
                "test_verdict", "coverage_status", "reason_code",
                "evidence_artifact_id", "evidence_artifact_name"):
        assert re.search(r"^      %s:" % out, _TEXT, re.M), out


# --------------------------------------------------------------------------- #
# job graph: capture -> plan -> run -> aggregate -> enforce
# --------------------------------------------------------------------------- #
def test_job_graph_and_dependencies():
    for job in ("identity", "capture", "plan", "run", "aggregate", "enforce"):
        assert re.search(r"^  %s:\s*$" % job, _TEXT, re.M), job
    # identity feeds capture (gate) and every job that consumes needs.identity.outputs.pep_ref
    assert re.search(r"^    needs:\s*identity\s*$", _job_block("capture"), re.M)
    assert re.search(r"needs:\s*\[identity,\s*capture\]", _job_block("plan"))
    assert re.search(r"needs:\s*\[identity,\s*plan\]", _job_block("run"))
    assert re.search(r"needs:\s*\[identity,\s*capture,\s*plan,\s*run\]", _job_block("aggregate"))
    assert re.search(r"^    needs:\s*aggregate\s*$", _job_block("enforce"), re.M)


def test_single_reusable_boundary():
    assert "uses: ./.github/workflows/pep-capture.yml" in _job_block("capture")
    assert "uses: ./.github/workflows/pep-integration.yml" in _job_block("run")
    # exactly ONE public boundary: no other pep-certify caller is embedded here
    assert _TEXT.count("uses: ./.github/workflows/pep-certify.yml") == 0


# --------------------------------------------------------------------------- #
# plan: immutable-id cert-plan download, catalog-driven planner, uploaded plan
# --------------------------------------------------------------------------- #
def test_plan_downloads_cert_plan_by_immutable_id():
    b = _job_block("plan")
    assert "artifact-ids: ${{ needs.capture.outputs.cert_plan_artifact_id }}" in b
    assert "pep_invocation_plan.py" in b
    # catalog-driven inputs (never hardcoded platform lists)
    assert "--exec-catalog utillities/pep_exec_catalog.json" in b
    assert "--containers configuration/containers_list.json" in b
    assert "invocation_plan_artifact_id: ${{ steps.up_plan.outputs.artifact-id }}" in b


# --------------------------------------------------------------------------- #
# run: dynamic matrix guard, fail-fast:false, per-leg mode, secret forwarding
# --------------------------------------------------------------------------- #
def test_run_is_dynamic_matrix_guarded_against_empty_include():
    b = _job_block("run")
    assert re.search(r"if:\s*\$\{\{\s*needs\.plan\.outputs\.has_targets\s*==\s*'true'\s*\}\}", b)
    assert re.search(r"matrix:\s*\$\{\{\s*fromJSON\(needs\.plan\.outputs\.matrix\)\s*\}\}", b)
    assert re.search(r"fail-fast:\s*false", b)


def test_run_passes_requested_mode_to_every_leg_not_forced_observe():
    b = _job_block("run")
    assert re.search(r"mode:\s*\$\{\{\s*inputs\.enforcement\s*\}\}", b)
    # must NOT hardcode observe on the legs
    assert not re.search(r"mode:\s*observe", b)
    assert re.search(r"execution_mode:\s*\$\{\{\s*inputs\.execution_mode\s*\}\}", b)
    assert re.search(r"pep_implementation_ref:\s*\$\{\{\s*needs\.identity\.outputs\.pep_ref\s*\}\}", b)


def test_run_keeps_scenario_certification_internal_no_upgrade():
    b = _job_block("run")
    assert re.search(r"scenario:\s*certification", b)
    assert "upgrade" not in b                     # upgrade scenario is not exposed yet


def test_run_forwards_docker_secrets_explicitly():
    b = _job_block("run")
    assert re.search(r"DOCKERHUB_USERNAME:\s*\$\{\{\s*secrets\.DOCKERHUB_USERNAME\s*\}\}", b)
    assert re.search(r"DOCKERHUB_TOKEN:\s*\$\{\{\s*secrets\.DOCKERHUB_TOKEN\s*\}\}", b)


# --------------------------------------------------------------------------- #
# aggregate: artifact-only discovery, pagination, expired-exclusion, live attempt
# --------------------------------------------------------------------------- #
def test_aggregate_runs_even_when_a_leg_failed():
    assert re.search(r"if:\s*\$\{\{\s*!cancelled\(\)\s*\}\}", _job_block("aggregate"))


def test_aggregate_paginates_and_normalizes_listing():
    b = _job_block("aggregate")
    assert "per_page=100&page=" in b
    assert re.search(r"\{id,\s*name,\s*expired,\s*size_in_bytes\}", b)   # normalized allowlist


def test_aggregate_excludes_expired_and_downloads_by_immutable_id():
    b = _job_block("aggregate")
    assert "select(.expired != true)" in b                       # expired excluded from download ids
    assert re.search(r"if:\s*\$\{\{\s*steps\.collect\.outputs\.download_ids\s*!=\s*''\s*\}\}", b)  # skip when none
    assert "artifact-ids: ${{ steps.collect.outputs.download_ids }}" in b
    assert "artifact-ids: ${{ needs.plan.outputs.invocation_plan_artifact_id }}" in b


def test_aggregate_invokes_collector_with_live_run_attempt():
    b = _job_block("aggregate")
    assert "pep_result_io.py" in b
    assert "RUN_ATTEMPT: ${{ github.run_attempt }}" in b
    assert re.search(r'--current-run-attempt\s+"\$\{RUN_ATTEMPT\}"', b)


def test_aggregate_decides_with_requested_mode():
    b = _job_block("aggregate")
    assert "pep_cert_gate.py" in b
    assert "MODE: ${{ inputs.enforcement }}" in b
    assert re.search(r'--mode\s+"\$\{MODE\}"', b)


def test_aggregate_uploads_one_combined_evidence_under_always():
    b = _job_block("aggregate")
    # collector + gate write cert-result, ledger and decision under out/
    assert "--out out/cert-result.json" in b
    assert "--ledger-out out/collection-ledger.json" in b
    assert "--out out/cert-decision.json" in b
    up = re.search(r"Upload the combined certification evidence.*?path:\s*out/.*?if-no-files-found:\s*warn",
                   b, re.S)
    assert up is not None
    assert re.search(r"if:\s*\$\{\{\s*always\(\)\s*\}\}", b)


def test_aggregate_never_reads_matrix_job_outputs():
    # matrix reusable-workflow outputs collapse to the last successful setter -> the
    # aggregate must reconstruct from artifacts, never from needs.run.outputs.
    assert "needs.run.outputs" not in _TEXT


# --------------------------------------------------------------------------- #
# enforce: colour only, fail closed from the aggregate decision
# --------------------------------------------------------------------------- #
def test_enforce_fails_closed_from_decision():
    b = _job_block("enforce")
    assert "decision_available" in b
    assert re.search(r'\[\s*"\$\{AVAILABLE:-\}"\s*!=\s*"true"\s*\]', b)   # no decision -> fail
    assert re.search(r"failure\)\s*.*exit 1", b)                          # block -> fail
    assert re.search(r"\*\)\s*.*failing closed.*exit 1", b)              # unknown -> fail closed


# --------------------------------------------------------------------------- #
# cross-cutting: pinned actions, attempt-safe names, least privilege
# --------------------------------------------------------------------------- #
def test_all_actions_are_sha_pinned():
    uses = re.findall(r"uses:\s*(actions/[^\s]+)", _TEXT)
    assert uses, "expected at least one actions/* use"
    for u in uses:
        assert re.match(r"actions/[a-z0-9-]+@[0-9a-f]{40}$", u), u   # 40-hex pin, never @vN


def test_artifact_names_are_attempt_safe():
    assert "pep-certification-r${GITHUB_RUN_NUMBER}-a${GITHUB_RUN_ATTEMPT}" in _TEXT
    assert "pep-invocation-plan-r${GITHUB_RUN_NUMBER}-a${GITHUB_RUN_ATTEMPT}" in _TEXT


def test_least_privilege_permissions():
    assert re.search(r"^permissions:\s*\{\}\s*$", _TEXT, re.M)          # top-level empty
    assert re.search(r"permissions:\s*\{\}", _job_block("enforce"))    # enforce needs nothing
    assert re.search(r"permissions:\s*\{\}", _job_block("identity"))   # identity reads a context, no token
    # jobs that use the REST API / reusable workflows request read scopes explicitly
    for job in ("capture", "plan", "aggregate"):
        assert "actions: read" in _job_block(job), job


# --------------------------------------------------------------------------- #
# correction pass: preserve unresolved planner output (point 1)
# --------------------------------------------------------------------------- #
def test_plan_treats_planner_exit_0_and_1_as_handled():
    b = _job_block("plan")
    # the planner call is NOT under `set -e`; its exit code is captured and classified
    assert "set +e" in b
    assert re.search(r"rc=\$\?", b)
    # a valid pep-invocation-plan/1 document is REQUIRED before matrix/plan_resolved are read
    assert '.schema == "pep-invocation-plan/1"' in b
    assert re.search(r'\(\.plan_resolved \| type\) == "boolean"', b)
    assert re.search(r'\(\.matrix\.include \| type\) == "array"', b)


def test_plan_unexpected_or_missing_output_still_fails():
    b = _job_block("plan")
    # only exits OTHER than 0/1 are unexpected -> fail the plan job
    assert re.search(r'\[ "\$rc" -ne 0 \] && \[ "\$rc" -ne 1 \]', b)
    assert re.search(r'if \[ ! -s "\$out" \]', b)                  # missing/empty output -> fail
    # a malformed (non pep-invocation-plan/1) document -> fail (not silently treated as a plan)
    assert re.search(r'if \[ "\$safe" != "true" \]', b)
    assert b.count("failing the plan job") >= 3                    # unexpected rc, missing, malformed


def test_plan_uploads_in_both_resolved_and_unresolved_cases():
    b = _job_block("plan")
    up = _step_block(b, "Upload the invocation plan")
    assert up, "expected an invocation-plan upload step"
    assert "if-no-files-found: error" in up
    # the upload is NOT gated on resolution -> it runs for a resolved AND an unresolved plan
    assert "if:" not in up
    assert "plan_resolved" not in up
    assert "has_targets" not in up


def test_plan_requires_exit_matches_resolved():
    # exit 0 iff plan_resolved=true, exit 1 iff plan_resolved=false; any mismatch fails the plan job
    b = _job_block("plan")
    assert re.search(r'\[ "\$rc" -eq 0 \] && \[ "\$RESOLVED" != "true" \]', b)
    assert re.search(r'\[ "\$rc" -eq 1 \] && \[ "\$RESOLVED" != "false" \]', b)
    assert "inconsistent with plan_resolved" in b


# --------------------------------------------------------------------------- #
# correction pass: guard the aggregate plan download (point 2)
# --------------------------------------------------------------------------- #
def test_aggregate_skips_plan_download_when_id_blank():
    b = _job_block("aggregate")
    dl = _step_block(b, "Download the invocation plan by immutable id")
    assert dl, "expected the invocation-plan download step"
    # a blank artifact id skips the step (never a blank artifact-ids falling back to download-all)
    assert re.search(
        r"if:\s*\$\{\{\s*needs\.plan\.outputs\.invocation_plan_artifact_id\s*!=\s*''\s*\}\}", dl)


# --------------------------------------------------------------------------- #
# correction pass: native job.workflow_sha identity, no duplicate caller SHA (point 3)
# --------------------------------------------------------------------------- #
def test_identity_uses_job_workflow_sha_exactly_once_in_identity_job():
    b = _job_block("identity")
    assert b, "expected an identity job"
    # exactly ONE job.workflow_sha EXPRESSION in the whole workflow, and it lives in the identity job,
    # read through an env var, validated as a full 40-hex SHA, and exposed as the pep_ref output.
    exprs = re.findall(r"\$\{\{\s*job\.workflow_sha\s*\}\}", _TEXT)
    assert len(exprs) == 1, exprs
    assert re.search(r"WF_SHA:\s*\$\{\{\s*job\.workflow_sha\s*\}\}", b)
    assert re.search(r"\^\[0-9a-f\]\{40\}\$", b)                       # 40-hex validation
    assert re.search(r"pep_ref=\$\{WF_SHA\}", b)                       # exposed as output value
    assert re.search(r"pep_ref:\s*\$\{\{\s*steps\.id\.outputs\.pep_ref\s*\}\}", b)
    # the CALLER's workflow sha must never stand in for this workflow's revision
    assert not re.search(r"\$\{\{\s*github\.workflow_sha", _TEXT)


def test_identity_output_supplies_every_nested_workflow_and_checkout():
    # capture + run receive it as their pep_implementation_ref input; plan + aggregate check out with it
    assert re.search(r"pep_implementation_ref:\s*\$\{\{\s*needs\.identity\.outputs\.pep_ref\s*\}\}",
                     _job_block("capture"))
    assert re.search(r"pep_implementation_ref:\s*\$\{\{\s*needs\.identity\.outputs\.pep_ref\s*\}\}",
                     _job_block("run"))
    assert re.search(r"ref:\s*\$\{\{\s*needs\.identity\.outputs\.pep_ref\s*\}\}", _job_block("plan"))
    assert re.search(r"ref:\s*\$\{\{\s*needs\.identity\.outputs\.pep_ref\s*\}\}", _job_block("aggregate"))
    # the removed caller input is used nowhere
    assert "inputs.pep_implementation_ref" not in _TEXT


def test_rest_referenced_workflows_lookup_is_gone():
    # the previous REST fallback (referenced_workflows via gh api) is fully removed
    assert "referenced_workflows" not in _TEXT
    assert "gh api" not in _job_block("identity")


def test_identity_runs_before_capture():
    assert re.search(r"^    needs:\s*identity\s*$", _job_block("capture"), re.M)


def test_actionlint_suppression_is_narrow_and_singular():
    st = _SELFTEST.read_text()
    # exactly one -ignore FLAG (the `-ignore '<regex>'` command form; prose backtick mentions don't count),
    # anchored to the known job.workflow_sha false positive -- not a broad suppression of expression errors.
    assert st.count("-ignore '") == 1
    assert re.search(r"-ignore 'property \"workflow_sha\" is not defined in object type \\\{container'", st)


# --------------------------------------------------------------------------- #
# self-test lints the new workflow
# --------------------------------------------------------------------------- #
def test_selftest_actionlint_includes_certify_workflow():
    assert ".github/workflows/pep-certify.yml" in _SELFTEST.read_text()


# --------------------------------------------------------------------------- #
# execution_mode is bound through BOTH the capture and plan path (preview intent)
# --------------------------------------------------------------------------- #
def test_execution_mode_bound_through_capture_and_plan():
    # capture receives execution_mode (so the cert-plan is stamped with it)
    cap = _job_block("capture")
    assert re.search(r"execution_mode:\s*\$\{\{\s*inputs\.execution_mode\s*\}\}", cap)
    # the planner receives it via env + the --execution-mode flag (re-checked against the stamp)
    plan = _job_block("plan")
    assert re.search(r"EXECUTION_MODE:\s*\$\{\{\s*inputs\.execution_mode\s*\}\}", plan)
    assert re.search(r'--execution-mode\s+"\$\{EXECUTION_MODE\}"', plan)
