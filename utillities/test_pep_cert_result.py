"""Unit tests for utillities.pep_cert_result (the pure cert-result/1 reducer)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "pep_cert_result", str(Path(__file__).parent / "pep_cert_result.py")
)
pcr = importlib.util.module_from_spec(_spec)
sys.modules["pep_cert_result"] = pcr
_spec.loader.exec_module(pcr)

_PLAN_SHA = "a" * 40
_PEP_SHA = "b" * 40


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _plan_prov(**over):
    p = {"repository": "pgEdge/pgedge-rag-server", "run_id": "123",
         "run_attempt": "1", "sha": _PLAN_SHA, "ref": "refs/tags/v2.0.0"}
    p.update(over)
    return p


def _caller_prov(**over):
    pp = _plan_prov()
    p = {"caller_repo": pp["repository"], "caller_run_id": pp["run_id"],
         "caller_run_attempt": pp["run_attempt"], "caller_sha": pp["sha"],
         "caller_ref": pp["ref"], "pep_requested_ref": _PEP_SHA, "pep_resolved_sha": _PEP_SHA}
    p.update(over)
    return p


def _inv(iid, *, family="rpm", arch="amd64", pg="17", alias="rocky9-amd64", **over):
    entry = {
        "invocation_id": iid,
        "component": "rag", "package_name": "pgedge-rag-server2", "channel": "release",
        "expected_version": "2.0.0", "container_alias": alias, "pg_major": pg,
        "family": family, "arch": arch,
        "expected_buildnum": "", "effective_tag": "v2.0.0",
        "expected_rpm": "2.0.0-1.el9" if family == "rpm" else "",
        "expected_deb": "2.0.0-1.noble" if family == "deb" else "",
        "expected_binary": "",
        "package": {"name": "pgedge-rag-server2", "version": "2.0.0",
                    "release": "1.el9" if family == "rpm" else "1.noble",
                    "sha256": "d" * 64, "native_arch": "x86_64"},
        "source_cell_id": "pepcell.v1.rpm.el-9.amd64.pkg",
        "source_target_id": "tgt-001",
        "producer_repo": "pgEdge/pgedge-rag-server",
    }
    entry.update(over)
    return entry


def _plan(include, *, gaps=None, prov=None, release=None):
    return {
        "schema": "pep-invocation-plan/1", "plan_resolved": True, "errors": [],
        "provenance": _plan_prov() if prov is None else prov,
        "release": release or {"logical_component": "rag", "channel": "release",
                               "intended_version": "2.0.0", "intended_buildnum": None,
                               "effective_tag": "v2.0.0"},
        "supported_pg_majors": ["16", "17", "18"],
        "matrix": {"include": include},
        "coverage_gaps": gaps or [],
        "counts": {"eligible_targets": len(include), "covered_targets": len(include),
                   "coverage_gaps": len(gaps or []), "invocations": len(include)},
    }


def _consistent_counts(execution_status, test_verdict):
    """Default counts matching the status/verdict semantics pep_result_summary.py emits,
    so a helper-built summary is self-consistent unless a test deliberately overrides."""
    if execution_status in ("preview", "infra_failure"):
        return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if test_verdict == "fail":
        return {"tests": 3, "failures": 1, "errors": 0, "skipped": 0}
    if test_verdict == "pass":
        return {"tests": 3, "failures": 0, "errors": 0, "skipped": 0}
    return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}   # not_run


def _summary(iid, *, execution_status="completed", test_verdict="pass",
             enforcement_mode="observe", provenance=None, counts=None, **extra):
    s = {
        "invocation_id": iid,
        "execution_status": execution_status,
        "test_verdict": test_verdict,
        "enforcement_mode": enforcement_mode,
        "identity_evidence": {"l2a": "proven", "l2b": "proven", "l1": "proven"},
        "counts": _consistent_counts(execution_status, test_verdict) if counts is None else counts,
        "provenance": _caller_prov() if provenance is None else provenance,
    }
    # A verified full-mode install records the planned package digest (_inv's "d"*64);
    # preview never installs. Tests override or delete it to exercise the proof rules.
    if execution_status != "preview":
        s["installed_package_sha256"] = "d" * 64
    s.update(extra)
    return s


def _build(plan, summaries, current_run_attempt="1"):
    """Call the pure reducer with an explicit aggregation attempt. Defaults to "1"
    (matching the default plan/caller attempt) so attempt-agnostic tests keep their
    original meaning; attempt-classification tests pass an explicit value. The reducer
    itself has NO default — current_run_attempt is a required argument."""
    return pcr.build_cert_result(plan, summaries, current_run_attempt)


# --------------------------------------------------------------------------- #
# happy path + planned-invocation retention + evidence preservation
# --------------------------------------------------------------------------- #
def test_all_completed_pass_is_complete_coverage_and_completed_execution():
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    res = _build(plan, [_summary("rag-a-pg17-aaaa"), _summary("rag-b-pg18-bbbb")])
    assert res["result_resolved"] is True
    assert res["execution_status"] == "completed"
    assert res["test_verdict"] == "pass"
    assert res["coverage_status"] == "complete"
    assert res["reason_code"] is None
    assert res["counts"]["expected_invocations"] == 2
    assert res["counts"]["matched"] == 2
    assert [l["invocation_id"] for l in res["legs"]] == ["rag-a-pg17-aaaa", "rag-b-pg18-bbbb"]


def test_full_planned_invocation_is_retained_per_leg():
    inv = _inv("rag-a-pg17-aaaa", source_target_id="tgt-XYZ")
    res = _build(_plan([inv]), [_summary("rag-a-pg17-aaaa")])
    leg = res["legs"][0]
    planned = leg["planned_invocation"]
    # exactly the plan entry, preserved wholesale (source IDs, SHA, exact pins)
    assert planned == inv
    assert planned["source_cell_id"] == "pepcell.v1.rpm.el-9.amd64.pkg"
    assert planned["source_target_id"] == "tgt-XYZ"
    assert planned["package"]["sha256"] == "d" * 64
    assert planned["expected_rpm"] == "2.0.0-1.el9"


def test_matched_leg_preserves_full_provenance_identity_counts():
    prov = _caller_prov()
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]),
                                [_summary("rag-a-pg17-aaaa", provenance=prov)])
    leg = res["legs"][0]
    assert leg["provenance"] == prov            # FULL provenance, not a subset
    assert leg["identity_evidence"] == {"l2a": "proven", "l2b": "proven", "l1": "proven"}
    assert leg["counts"] == {"tests": 3, "failures": 0, "errors": 0, "skipped": 0}
    assert leg["reconciliation"] == "matched"


def test_coverage_gaps_carried_verbatim_and_force_partial():
    gap = {"cell_id": "pepcell.v1.deb.bullseye.amd64.pkg", "target_id": "tgt-eol",
           "family": "deb", "os": "bullseye", "arch": "amd64",
           "physical_package": "pgedge-rag-server", "reason": "unsupported_os", "detail": "bullseye"}
    res = _build(_plan([_inv("rag-a-pg17-aaaa")], gaps=[gap]),
                                [_summary("rag-a-pg17-aaaa")])
    assert res["result_resolved"] is True
    assert res["coverage_status"] == "partial"      # a planner gap prevents complete
    assert res["execution_status"] == "completed"   # ...but execution still completed
    assert res["coverage_gaps"] == [gap]            # verbatim
    assert res["counts"]["planner_gaps"] == 1


# --------------------------------------------------------------------------- #
# missing result: affects BOTH execution and coverage
# --------------------------------------------------------------------------- #
def test_missing_result_synthesizes_infra_leg_and_partial_coverage():
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]), [])
    assert res["result_resolved"] is True           # missing is truthfully resolved
    leg = res["legs"][0]
    assert leg["reconciliation"] == "missing"
    assert leg["execution_status"] == "infra_failure"
    assert leg["test_verdict"] == "not_run"
    assert leg["reason_code"] == "missing_result"
    assert res["execution_status"] == "infra_failure"
    assert res["reason_code"] == "missing_result"
    assert res["coverage_status"] == "partial"
    assert res["test_verdict"] == "not_run"


def test_missing_alongside_completed_still_infra_and_partial():
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    res = _build(plan, [_summary("rag-a-pg17-aaaa")])   # b is missing
    assert res["execution_status"] == "infra_failure"
    assert res["reason_code"] == "missing_result"
    assert res["coverage_status"] == "partial"
    assert res["counts"]["missing"] == 1
    assert res["counts"]["matched"] == 1


# --------------------------------------------------------------------------- #
# execution precedence + aggregate independence
# --------------------------------------------------------------------------- #
def test_matched_infra_leg_rolls_up_infra_leg_reason():
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa", execution_status="infra_failure", test_verdict="not_run")])
    assert res["execution_status"] == "infra_failure"
    assert res["reason_code"] == "infra_leg"        # no missing -> infra_leg, not missing_result
    assert res["coverage_status"] == "partial"


def test_mixed_preview_and_non_preview_is_incomplete_mixed_mode():
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    res = _build(plan, [
        _summary("rag-a-pg17-aaaa", execution_status="preview", test_verdict="not_run"),
        _summary("rag-b-pg18-bbbb")])
    assert res["execution_status"] == "incomplete"
    assert res["reason_code"] == "mixed_mode"


def test_missing_takes_precedence_over_mixed_preview():
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    res = _build(plan, [
        _summary("rag-a-pg17-aaaa", execution_status="preview", test_verdict="not_run")])  # b missing
    assert res["execution_status"] == "infra_failure"
    assert res["reason_code"] == "missing_result"


def test_leg_incomplete_aggregate_classification():
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    res = _build(plan, [
        _summary("rag-a-pg17-aaaa"),                                   # completed/pass
        _summary("rag-b-pg18-bbbb", execution_status="incomplete", test_verdict="not_run")])
    assert res["execution_status"] == "incomplete"
    assert res["reason_code"] == "leg_incomplete"
    assert res["coverage_status"] == "partial"


def test_all_preview_is_preview():
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    res = _build(plan, [
        _summary("rag-a-pg17-aaaa", execution_status="preview", test_verdict="not_run"),
        _summary("rag-b-pg18-bbbb", execution_status="preview", test_verdict="not_run")])
    assert res["execution_status"] == "preview"
    assert res["reason_code"] is None
    assert res["test_verdict"] == "not_run"


def test_zero_eligible_is_incomplete_zero_eligible_and_none_coverage():
    res = _build(_plan([]), [])
    assert res["result_resolved"] is True
    assert res["execution_status"] == "incomplete"
    assert res["reason_code"] == "zero_eligible"
    assert res["coverage_status"] == "none"
    assert res["test_verdict"] == "not_run"
    assert res["legs"] == []


# --- incomplete/pass and incomplete/fail preservation (independent axes) --- #
def test_incomplete_pass_combo_is_preserved():
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa", execution_status="incomplete", test_verdict="pass")])
    leg = res["legs"][0]
    assert leg["execution_status"] == "incomplete"      # not rewritten to not_run
    assert leg["test_verdict"] == "pass"                # verdict preserved independently
    assert res["execution_status"] == "incomplete"
    assert res["reason_code"] == "leg_incomplete"
    assert res["test_verdict"] == "pass"
    assert res["coverage_status"] == "partial"          # incomplete execution -> not complete


def test_incomplete_fail_combo_is_preserved():
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa", execution_status="incomplete", test_verdict="fail")])
    leg = res["legs"][0]
    assert leg["execution_status"] == "incomplete"
    assert leg["test_verdict"] == "fail"
    assert res["execution_status"] == "incomplete"
    assert res["test_verdict"] == "fail"                # fail dominates verdict


def test_completed_fail_keeps_complete_coverage():
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa", test_verdict="fail",
                  counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0})])
    assert res["execution_status"] == "completed"
    assert res["test_verdict"] == "fail"
    assert res["coverage_status"] == "complete"         # a completed product failure is covered


def test_all_skipped_matched_is_not_run_not_pass():
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa", test_verdict="not_run")])
    assert res["execution_status"] == "completed"
    assert res["test_verdict"] == "not_run"             # one not_run leg -> aggregate not pass
    assert res["coverage_status"] == "complete"


# --------------------------------------------------------------------------- #
# structural fail-closed: malformed / unknown / duplicate
# --------------------------------------------------------------------------- #
def _assert_failed_safe(res):
    assert res["result_resolved"] is False
    assert res["reason_code"] == "validation_failure"
    assert res["execution_status"] == "infra_failure"
    assert res["test_verdict"] == "not_run"
    assert res["coverage_status"] == "none"
    assert res["legs"] == []


@pytest.mark.parametrize("bad", [
    "not-a-dict",
    {"invocation_id": "", "execution_status": "completed", "test_verdict": "pass",
     "enforcement_mode": "observe", "provenance": {}},                       # blank id
    {"invocation_id": "rag-a-pg17-aaaa", "execution_status": "weird",
     "test_verdict": "pass", "enforcement_mode": "observe", "provenance": {}},  # bad status
    {"invocation_id": "rag-a-pg17-aaaa", "execution_status": "completed",
     "test_verdict": "maybe", "enforcement_mode": "observe", "provenance": {}},  # bad verdict
    {"invocation_id": "rag-a-pg17-aaaa", "execution_status": "completed",
     "test_verdict": "pass", "enforcement_mode": "audit", "provenance": {}},   # bad mode
    {"invocation_id": "rag-a-pg17-aaaa", "execution_status": "completed",
     "test_verdict": "pass", "enforcement_mode": "observe", "provenance": "x"},  # bad provenance
])
def test_malformed_record_fails_closed(bad):
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]), [bad])
    _assert_failed_safe(res)
    assert any(u["kind"] == "malformed" for u in res["unexpected_results"])
    assert res["counts"]["unexpected_results"] >= 1


def test_unknown_record_fails_closed_and_is_audited_not_in_legs():
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]),
                                [_summary("rag-a-pg17-aaaa"), _summary("rag-z-pg17-zzzz")])
    _assert_failed_safe(res)
    kinds = {u["kind"] for u in res["unexpected_results"]}
    assert "unknown" in kinds
    assert any(u["invocation_id"] == "rag-z-pg17-zzzz" for u in res["unexpected_results"])


def test_duplicate_records_retained_without_silent_selection():
    dup_a = _summary("rag-a-pg17-aaaa", test_verdict="pass")
    dup_b = _summary("rag-a-pg17-aaaa", test_verdict="fail")   # conflicting duplicate
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]), [dup_a, dup_b])
    _assert_failed_safe(res)
    dups = [u for u in res["unexpected_results"] if u["kind"] == "duplicate"]
    assert len(dups) == 2                                # BOTH candidates preserved
    verdicts = {u["evidence"]["record"]["test_verdict"] for u in dups}
    assert verdicts == {"pass", "fail"}                 # neither silently chosen


def test_unresolved_plan_fails_closed():
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    plan["plan_resolved"] = False
    res = _build(plan, [_summary("rag-a-pg17-aaaa")])
    _assert_failed_safe(res)
    assert any("not resolved" in e for e in res["errors"])


def test_wrong_plan_schema_fails_closed():
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    plan["schema"] = "something-else/9"
    _assert_failed_safe(_build(plan, []))


# --------------------------------------------------------------------------- #
# provenance binding: the four ATTEMPT-STABLE caller fields bind directly to the plan
# (run_attempt is DELIBERATELY excluded from binding — it drives attempt
# classification instead; see the future-attempt / prior-attempt tests below).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field,badval", [
    ("caller_repo", "evil/repo"),
    ("caller_run_id", "999"),
    ("caller_sha", "c" * 40),
    ("caller_ref", "refs/heads/rogue"),
])
def test_provenance_field_mismatch_fails_closed(field, badval):
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa", provenance=_caller_prov(**{field: badval}))])
    _assert_failed_safe(res)
    assert any(field in e for e in res["errors"]), res["errors"]


def test_provenance_match_resolves_true():
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]),
                                [_summary("rag-a-pg17-aaaa", provenance=_caller_prov())])
    assert res["result_resolved"] is True


def test_missing_plan_provenance_value_fails_outer_contract():
    # A resolved plan missing a provenance source value fails the OUTER contract
    # (before any binding), never binds against a blank.
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_id=None))
    res = _build(plan, [_summary("rag-a-pg17-aaaa")])
    _assert_failed_safe(res)
    assert any("run_id must be a positive decimal string" in e for e in res["errors"])


# --------------------------------------------------------------------------- #
# PEP refs: nonblank, internally consistent, full-SHA equality (record evidence)
# --------------------------------------------------------------------------- #
def test_blank_pep_requested_ref_is_malformed():
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa", provenance=_caller_prov(pep_requested_ref="  "))])
    _assert_failed_safe(res)
    assert any(u["kind"] == "malformed" for u in res["unexpected_results"])


def test_matched_legs_disagree_on_resolved_sha_fails_closed():
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    res = _build(plan, [
        _summary("rag-a-pg17-aaaa", provenance=_caller_prov(pep_requested_ref="b" * 40, pep_resolved_sha="b" * 40)),
        _summary("rag-b-pg18-bbbb", provenance=_caller_prov(pep_requested_ref="c" * 40, pep_resolved_sha="c" * 40))])
    _assert_failed_safe(res)
    assert any("disagree on pep_resolved_sha" in e for e in res["errors"])


def test_full_sha_requested_must_equal_resolved():
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa",
                  provenance=_caller_prov(pep_requested_ref="b" * 40, pep_resolved_sha="e" * 40))])
    _assert_failed_safe(res)
    assert any("full SHA but does not equal" in u["evidence"]["reason"]
               for u in res["unexpected_results"] if u["kind"] == "malformed")


def test_full_sha_equality_is_case_insensitive():
    # An uppercase requested SHA equal to a lowercase resolved SHA is consistent.
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa",
                  provenance=_caller_prov(pep_requested_ref="B" * 40, pep_resolved_sha="b" * 40))])
    assert res["result_resolved"] is True


def test_non_sha_requested_ref_is_not_required_to_equal_resolved():
    # A tag/branch ref cannot be proven to resolve to a SHA by the pure reducer, so
    # requested != resolved is allowed as long as both are nonblank and consistent.
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa",
                  provenance=_caller_prov(pep_requested_ref="refs/tags/v2.0.0", pep_resolved_sha="b" * 40))])
    assert res["result_resolved"] is True


# --------------------------------------------------------------------------- #
# enforcement mode: matched summaries must agree
# --------------------------------------------------------------------------- #
def test_mixed_enforcement_modes_rejected():
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    res = _build(plan, [
        _summary("rag-a-pg17-aaaa", enforcement_mode="observe"),
        _summary("rag-b-pg18-bbbb", enforcement_mode="gate")])
    _assert_failed_safe(res)
    assert any("enforcement_mode" in e for e in res["errors"])


def test_enforcement_mode_preserved_per_leg():
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]),
                                [_summary("rag-a-pg17-aaaa", enforcement_mode="gate")])
    assert res["legs"][0]["enforcement_mode"] == "gate"


# --------------------------------------------------------------------------- #
# determinism, purity, one-leg-per-expected
# --------------------------------------------------------------------------- #
def test_exactly_one_leg_per_expected_regardless_of_summary_order():
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    forward = _build(plan, [_summary("rag-a-pg17-aaaa"), _summary("rag-b-pg18-bbbb")])
    reverse = _build(plan, [_summary("rag-b-pg18-bbbb"), _summary("rag-a-pg17-aaaa")])
    assert len(forward["legs"]) == 2
    assert pcr.to_json(forward) == pcr.to_json(reverse)   # byte-identical, order-independent


def test_build_does_not_mutate_inputs():
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    snapshot = json.dumps(plan, sort_keys=True)
    summaries = [_summary("rag-a-pg17-aaaa")]
    res = _build(plan, summaries)
    res["legs"][0]["planned_invocation"]["source_target_id"] = "MUTATED"
    assert json.dumps(plan, sort_keys=True) == snapshot   # deepcopy protected the input


def test_to_json_is_stable_across_repeated_builds():
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    a = pcr.to_json(_build(plan, [_summary("rag-a-pg17-aaaa")]))
    b = pcr.to_json(_build(plan, [_summary("rag-a-pg17-aaaa")]))
    assert a == b


# --------------------------------------------------------------------------- #
# main() standalone dry-run (impure edge)
# --------------------------------------------------------------------------- #
def test_main_reads_plan_and_summaries_dir(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    sdir = tmp_path / "summaries"
    sdir.mkdir()
    (sdir / "a.json").write_text(json.dumps(_summary("rag-a-pg17-aaaa")))
    out = tmp_path / "cert-result.json"
    code = pcr.main(["--plan", str(tmp_path / "plan.json"),
                     "--summaries-dir", str(sdir), "--current-run-attempt", "1", "--out", str(out)])
    data = json.loads(out.read_text())
    assert code == 0
    assert data["result_resolved"] is True
    assert data["execution_status"] == "completed"
    assert data["coverage_status"] == "complete"


def test_main_unreadable_plan_fails_closed_exit1(tmp_path):
    out = tmp_path / "cert-result.json"
    code = pcr.main(["--plan", str(tmp_path / "nope.json"),
                     "--current-run-attempt", "1", "--out", str(out)])
    data = json.loads(out.read_text())
    assert code == 1
    assert data["result_resolved"] is False


def test_main_requires_current_run_attempt(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    # --current-run-attempt is required: argparse exits (SystemExit 2) when it is absent.
    with pytest.raises(SystemExit):
        pcr.main(["--plan", str(tmp_path / "plan.json"), "--out", str(tmp_path / "o.json")])


# --------------------------------------------------------------------------- #
# correction 1: complete atomic-result evidence is required for a matched summary
# --------------------------------------------------------------------------- #
def test_missing_identity_evidence_is_malformed():
    s = _summary("rag-a-pg17-aaaa")
    del s["identity_evidence"]
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]), [s])
    _assert_failed_safe(res)
    assert any(u["kind"] == "malformed" for u in res["unexpected_results"])


@pytest.mark.parametrize("ev", [
    {"l2a": "proven", "l2b": "proven"},                                    # missing rung
    {"l2a": "proven", "l2b": "proven", "l1": "proven", "l3": "proven"},    # extra rung
    {"l2a": "maybe", "l2b": "proven", "l1": "proven"},                     # invalid value
    {"l2a": ["proven"], "l2b": "proven", "l1": "proven"},                  # non-string value
    "not-an-object",
])
def test_malformed_identity_evidence_is_malformed(ev):
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]),
                                [_summary("rag-a-pg17-aaaa", identity_evidence=ev)])
    _assert_failed_safe(res)
    assert any(u["kind"] == "malformed" for u in res["unexpected_results"])


def test_missing_counts_is_malformed_and_never_substituted():
    s = _summary("rag-a-pg17-aaaa")
    del s["counts"]
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]), [s])
    _assert_failed_safe(res)
    assert res["legs"] == []          # never a leg carrying counts={}


@pytest.mark.parametrize("counts", [
    {"tests": -1, "failures": 0, "errors": 0, "skipped": 0},        # negative
    {"tests": True, "failures": 0, "errors": 0, "skipped": 0},      # boolean tests
    {"tests": 3, "failures": False, "errors": 0, "skipped": 0},     # boolean count
    {"tests": 3, "failures": 0, "errors": 0, "skipped": "1"},       # non-integer
    {"tests": 3.0, "failures": 0, "errors": 0, "skipped": 0},       # float
    {"tests": 1, "failures": 1, "errors": 1, "skipped": 0},         # sum exceeds tests
    {"tests": 3, "failures": 0, "errors": 0},                       # missing key
    {"tests": 3, "failures": 0, "errors": 0, "skipped": 0, "x": 0}, # extra key
    "not-an-object",
])
def test_inconsistent_counts_is_malformed(counts):
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]),
                                [_summary("rag-a-pg17-aaaa", counts=counts)])
    _assert_failed_safe(res)
    assert any(u["kind"] == "malformed" for u in res["unexpected_results"])


@pytest.mark.parametrize("sha", ["z" * 40, "a" * 39, "a" * 41, "", "  ", "A" * 40 + " "])
def test_malformed_pep_resolved_sha_is_malformed(sha):
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa",
                  provenance=_caller_prov(pep_requested_ref="refs/tags/x", pep_resolved_sha=sha))])
    _assert_failed_safe(res)
    assert any(u["kind"] == "malformed" for u in res["unexpected_results"])


def test_incomplete_provenance_field_is_malformed():
    prov = _caller_prov()
    del prov["caller_ref"]
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]),
                                [_summary("rag-a-pg17-aaaa", provenance=prov)])
    _assert_failed_safe(res)
    assert any(u["kind"] == "malformed" for u in res["unexpected_results"])


# --------------------------------------------------------------------------- #
# correction 2: the invocation-plan OUTER contract is validated before aggregation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mutate", [
    lambda p: p.__setitem__("provenance", "corrupt"),
    lambda p: p.__setitem__("provenance",
                            {"repository": "r", "run_id": "1", "run_attempt": "1", "sha": "a" * 40}),  # missing ref
    lambda p: p.__setitem__("provenance",
                            {"repository": "", "run_id": "1", "run_attempt": "1", "sha": "a" * 40, "ref": "x"}),  # blank
    lambda p: p.__setitem__("release", "not-an-object"),
    lambda p: p.__setitem__("coverage_gaps", "corrupt"),
    lambda p: p.__setitem__("matrix", {"include": "nope"}),
    lambda p: p.__setitem__("matrix", "nope"),
])
def test_malformed_outer_plan_fails_closed(mutate):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    mutate(plan)
    _assert_failed_safe(_build(plan, [_summary("rag-a-pg17-aaaa")]))


def test_corrupt_coverage_gaps_cannot_yield_complete_coverage():
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    plan["coverage_gaps"] = "corrupt"
    res = _build(plan, [_summary("rag-a-pg17-aaaa")])
    assert res["result_resolved"] is False
    assert res["coverage_status"] == "none"          # never "complete"
    assert any("coverage_gaps must be a list" in e for e in res["errors"])


# --------------------------------------------------------------------------- #
# correction 3: invocation-id validation is whole-string exact (reducer side)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("iid", [
    "rag-a\n", "rag\na", "rag-a\r", "rag-a\r\n", "rag a", "rag/a", "x" * 65,
])
def test_summary_invocation_id_is_whole_string_validated(iid):
    s = _summary("placeholder")
    s["invocation_id"] = iid                         # id fails fullmatch -> malformed
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]), [s])
    _assert_failed_safe(res)
    assert any(u["kind"] == "malformed" for u in res["unexpected_results"])


# --------------------------------------------------------------------------- #
# correction 4: fail-closed audit evidence is complete and order-independent
# --------------------------------------------------------------------------- #
def test_duplicate_audit_is_order_independent_and_complete():
    a = _summary("rag-a-pg17-aaaa", test_verdict="pass")
    b = _summary("rag-a-pg17-aaaa", test_verdict="fail",
                 counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0})
    forward = _build(_plan([_inv("rag-a-pg17-aaaa")]), [a, b])
    reverse = _build(_plan([_inv("rag-a-pg17-aaaa")]), [b, a])
    assert pcr.to_json(forward) == pcr.to_json(reverse)          # byte-identical
    dups = [u for u in forward["unexpected_results"] if u["kind"] == "duplicate"]
    assert len(dups) == 2
    records = [u["evidence"]["record"] for u in dups]
    assert {r["test_verdict"] for r in records} == {"pass", "fail"}   # both retained
    # complete records: full counts object preserved on each (not reduced to a subset)
    assert all(set(r["counts"].keys()) == {"tests", "failures", "errors", "skipped"}
               for r in records)


def test_non_dict_malformed_keeps_bounded_repr():
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]), ["x" * 500])
    _assert_failed_safe(res)
    ev = res["unexpected_results"][0]["evidence"]
    assert "repr" in ev and len(ev["repr"]) <= 200


def test_malformed_evidence_retains_complete_record():
    # A malformed record (bad counts) is preserved COMPLETE in the audit — including
    # its identity_evidence, provenance and reason fields — for later diagnosis.
    bad = _summary("rag-a-pg17-aaaa", counts={"tests": 1, "failures": 5, "errors": 0, "skipped": 0})
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]), [bad])
    _assert_failed_safe(res)
    rec = res["unexpected_results"][0]["evidence"]["record"]
    assert rec["identity_evidence"] == {"l2a": "proven", "l2b": "proven", "l1": "proven"}
    assert set(rec["provenance"].keys()) >= {"caller_repo", "pep_resolved_sha"}
    assert rec["counts"] == {"tests": 1, "failures": 5, "errors": 0, "skipped": 0}


# --------------------------------------------------------------------------- #
# contract 1: contradictory status/verdict/count evidence is malformed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status,verdict,counts", [
    ("completed", "pass", {"tests": 3, "failures": 1, "errors": 0, "skipped": 0}),   # pass with failures
    ("completed", "pass", {"tests": 2, "failures": 0, "errors": 0, "skipped": 2}),   # pass, zero executed
    ("completed", "fail", {"tests": 3, "failures": 0, "errors": 0, "skipped": 0}),   # fail, no failures/errors
    ("completed", "not_run", {"tests": 3, "failures": 0, "errors": 0, "skipped": 0}),  # not_run with executed
    ("preview", "not_run", {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}),  # preview nonzero counts
    ("preview", "pass", {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}),     # preview claiming pass
    ("infra_failure", "not_run", {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}),  # infra nonzero
    ("infra_failure", "fail", {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}),      # infra claiming fail
])
def test_contradictory_status_verdict_counts_is_malformed(status, verdict, counts):
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa", execution_status=status, test_verdict=verdict, counts=counts)])
    _assert_failed_safe(res)                          # cannot become complete/pass
    assert res["execution_status"] != "completed"
    assert res["test_verdict"] != "pass"
    assert any(u["kind"] == "malformed" for u in res["unexpected_results"])


def test_legit_incomplete_pass_with_supporting_counts_is_accepted():
    # incomplete/pass is legitimate when the counts support pass (some parsed, none failed).
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")]),
        [_summary("rag-a-pg17-aaaa", execution_status="incomplete", test_verdict="pass",
                  counts={"tests": 3, "failures": 0, "errors": 0, "skipped": 1})])
    assert res["result_resolved"] is True
    assert res["legs"][0]["execution_status"] == "incomplete"
    assert res["legs"][0]["test_verdict"] == "pass"


# --------------------------------------------------------------------------- #
# contract 2: top-level provenance is a stable seven-key block
# --------------------------------------------------------------------------- #
_SEVEN_KEYS = {"repository", "run_id", "run_attempt", "sha", "ref",
               "pep_requested_ref", "pep_resolved_sha"}


def test_top_provenance_happy_path_seven_keys_with_pep():
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]), [_summary("rag-a-pg17-aaaa")])
    p = res["provenance"]
    assert set(p.keys()) == _SEVEN_KEYS
    assert p["repository"] == "pgEdge/pgedge-rag-server"
    assert p["run_id"] == "123" and p["run_attempt"] == "1"
    assert p["sha"] == "a" * 40 and p["ref"] == "refs/tags/v2.0.0"
    assert p["pep_requested_ref"] == "b" * 40           # from the common atomic provenance
    assert p["pep_resolved_sha"] == "b" * 40


def test_top_provenance_missing_only_keeps_pep_from_matched():
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    res = _build(plan, [_summary("rag-a-pg17-aaaa")])   # b missing
    p = res["provenance"]
    assert set(p.keys()) == _SEVEN_KEYS
    assert p["pep_requested_ref"] == "b" * 40           # still populated from the matched leg
    assert p["pep_resolved_sha"] == "b" * 40


def test_top_provenance_zero_eligible_pep_fields_null():
    res = _build(_plan([]), [])
    p = res["provenance"]
    assert set(p.keys()) == _SEVEN_KEYS
    assert p["pep_requested_ref"] is None and p["pep_resolved_sha"] is None  # no result to source
    assert p["repository"] == "pgEdge/pgedge-rag-server"                     # five plan fields present


def test_top_provenance_fail_closed_seven_keys_pep_null():
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    plan["plan_resolved"] = False
    res = _build(plan, [_summary("rag-a-pg17-aaaa")])
    assert res["result_resolved"] is False
    p = res["provenance"]
    assert set(p.keys()) == _SEVEN_KEYS
    assert p["pep_requested_ref"] is None and p["pep_resolved_sha"] is None
    assert p["repository"] == "pgEdge/pgedge-rag-server"    # trustworthy plan value retained


@pytest.mark.parametrize("bad_key,bad_val", [
    ("repository", {}),           # object
    ("repository", 123),          # numeric
    ("repository", ""),           # blank
    ("ref", {"x": 1}),            # object
    ("ref", "   "),               # blank
    ("sha", "z" * 40),            # non-hex
    ("sha", "a" * 39),            # wrong length
    ("run_id", 1),                # int, not a string
    ("run_id", "0"),              # zero
    ("run_attempt", True),        # boolean
    ("run_attempt", [1]),         # array
])
def test_fail_closed_nulls_only_malformed_provenance_field(bad_key, bad_val):
    # Malformed plan provenance must NOT be echoed back as trustworthy: the offending
    # field is null while every other valid plan field is retained independently.
    good = {"repository": "pgEdge/pgedge-rag-server", "run_id": "123", "run_attempt": "1",
            "sha": "a" * 40, "ref": "refs/tags/v2.0.0"}
    res = _build(
        _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(**{bad_key: bad_val})),
        [_summary("rag-a-pg17-aaaa")])
    assert res["result_resolved"] is False           # malformed provenance -> fail closed
    p = res["provenance"]
    assert set(p.keys()) == _SEVEN_KEYS               # stable seven-key shape
    assert p[bad_key] is None                         # offending field nulled
    for k in ("repository", "run_id", "run_attempt", "sha", "ref"):
        if k != bad_key:
            assert p[k] == good[k], k                  # other valid fields preserved
    assert p["pep_requested_ref"] is None and p["pep_resolved_sha"] is None


# --------------------------------------------------------------------------- #
# contract 3: plan provenance value types + resolved-plan errors contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prov", [
    _plan_prov(repository=""),            # blank repository
    _plan_prov(repository=123),           # non-string repository
    _plan_prov(ref="  "),                 # blank ref
    _plan_prov(ref=None),                 # missing ref
    _plan_prov(sha="a" * 39),             # short sha
    _plan_prov(sha="z" * 40),             # non-hex sha
    _plan_prov(run_id="0"),               # zero
    _plan_prov(run_id="-1"),              # negative
    _plan_prov(run_id=1),                 # int, not a string
    _plan_prov(run_id=True),              # boolean
    _plan_prov(run_attempt={"x": 1}),     # object
    _plan_prov(run_attempt=[1]),          # array
    _plan_prov(run_attempt="0"),          # zero
])
def test_malformed_plan_provenance_types_fail_closed(prov):
    res = _build(_plan([_inv("rag-a-pg17-aaaa")], prov=prov),
                                [_summary("rag-a-pg17-aaaa")])
    _assert_failed_safe(res)


def test_resolved_plan_carrying_errors_fails_closed():
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    plan["errors"] = ["contradiction"]
    res = _build(plan, [_summary("rag-a-pg17-aaaa")])
    _assert_failed_safe(res)
    assert any("must not carry errors" in e for e in res["errors"])


def test_plan_errors_must_be_a_list():
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    plan["errors"] = "oops"
    res = _build(plan, [_summary("rag-a-pg17-aaaa")])
    _assert_failed_safe(res)
    assert any("errors must be a list" in e for e in res["errors"])


# --------------------------------------------------------------------------- #
# attempt-awareness (Slice A): current/prior/future classification vs the LIVE
# aggregation attempt; historical audit; stable-shape attempt_context.
# --------------------------------------------------------------------------- #
def test_attempt_fresh_all_current_is_complete():
    # Fresh run: plan attempt 1, aggregation 1, every summary current -> unchanged
    # complete/completed result, empty history, attempt_context 1/1.
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")],
                 prov=_plan_prov(run_attempt="1"))
    res = _build(plan, [_summary("rag-a-pg17-aaaa"), _summary("rag-b-pg18-bbbb")],
                 current_run_attempt="1")
    assert res["result_resolved"] is True
    assert res["execution_status"] == "completed"
    assert res["coverage_status"] == "complete"
    assert res["historical_results"] == []
    assert res["attempt_context"] == {"plan_run_attempt": "1", "aggregation_run_attempt": "1"}
    assert res["provenance"]["run_attempt"] == "1"   # source plan attempt, unchanged


def test_rerun_failed_prior_only_alpha_is_missing_and_historical():
    # Rerun-failed: plan carried forward at attempt 1, aggregation is attempt 2, and the
    # only alpha summary is the carried-forward attempt-1 (prior) result. Alpha has NO
    # current evidence -> missing_result leg; the prior is audited, never promoted.
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="1"))
    prior = _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="1"))
    res = _build(plan, [prior], current_run_attempt="2")
    assert res["result_resolved"] is True
    leg = res["legs"][0]
    assert leg["reconciliation"] == "missing"
    assert leg["reason_code"] == "missing_result"
    assert res["reason_code"] == "missing_result"
    assert res["execution_status"] == "infra_failure"
    assert res["coverage_status"] == "partial"
    hist = res["historical_results"]
    assert len(hist) == 1
    assert hist[0]["invocation_id"] == "rag-a-pg17-aaaa"
    assert hist[0]["producing_attempt"] == "1"
    assert hist[0]["provenance"]["caller_run_attempt"] == "1"    # full provenance retained
    assert res["attempt_context"] == {"plan_run_attempt": "1", "aggregation_run_attempt": "2"}
    # no current result -> top-level PEP refs are null
    assert res["provenance"]["pep_requested_ref"] is None
    assert res["provenance"]["pep_resolved_sha"] is None


def test_mixed_current_and_prior_for_one_invocation():
    # One current (attempt 3) plus one prior (attempt 1) for the same id: the current
    # result fills the leg; the prior is retained as history only.
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="1"))
    current = _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="3"))
    prior = _summary("rag-a-pg17-aaaa", test_verdict="fail",
                     counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0},
                     provenance=_caller_prov(caller_run_attempt="1"))
    res = _build(plan, [current, prior], current_run_attempt="3")
    assert res["result_resolved"] is True
    leg = res["legs"][0]
    assert leg["reconciliation"] == "matched"
    assert leg["test_verdict"] == "pass"                # the CURRENT result is used
    assert res["execution_status"] == "completed"
    hist = res["historical_results"]
    assert [h["producing_attempt"] for h in hist] == ["1"]
    assert hist[0]["test_verdict"] == "fail"            # prior retained verbatim, not promoted
    assert res["provenance"]["pep_requested_ref"] == "b" * 40   # from the current match


def test_rerun_all_all_current_is_complete():
    # Rerun-all: plan and every summary re-executed at attempt 3, aggregation 3 -> all
    # current, complete coverage, empty history.
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")],
                 prov=_plan_prov(run_attempt="3"))
    res = _build(plan, [
        _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="3")),
        _summary("rag-b-pg18-bbbb", provenance=_caller_prov(caller_run_attempt="3"))],
        current_run_attempt="3")
    assert res["result_resolved"] is True
    assert res["execution_status"] == "completed"
    assert res["coverage_status"] == "complete"
    assert res["historical_results"] == []
    assert res["attempt_context"] == {"plan_run_attempt": "3", "aggregation_run_attempt": "3"}


def test_future_attempt_summary_fails_closed():
    # A producing attempt greater than the live aggregation attempt is a violation.
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="1"))
    res = _build(plan, [_summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="2"))],
                 current_run_attempt="1")
    _assert_failed_safe(res)
    assert any(u["kind"] == "future" for u in res["unexpected_results"])
    assert res["historical_results"] == []


def test_plan_attempt_greater_than_aggregation_fails_closed():
    # The source plan/capture attempt can never exceed the live aggregation attempt.
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="3"))
    res = _build(plan, [_summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="2"))],
                 current_run_attempt="2")
    _assert_failed_safe(res)
    assert any("greater than current_run_attempt" in e for e in res["errors"])


def test_wrong_stable_provenance_is_foreign_validation_failure():
    # A mismatch on any attempt-stable field (here run_id) is foreign regardless of attempt.
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]),
                 [_summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_id="999"))],
                 current_run_attempt="1")
    _assert_failed_safe(res)
    assert any(u["kind"] == "foreign" for u in res["unexpected_results"])
    assert any("caller_run_id" in e for e in res["errors"])


def test_duplicate_current_fails_closed():
    a = _summary("rag-a-pg17-aaaa", test_verdict="pass")
    b = _summary("rag-a-pg17-aaaa", test_verdict="fail",
                 counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0})
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]), [a, b], current_run_attempt="1")
    _assert_failed_safe(res)
    dups = [u for u in res["unexpected_results"] if u["kind"] == "duplicate"]
    assert len(dups) == 2                                 # both current candidates retained


def test_duplicate_same_historical_attempt_fails_closed():
    # Two prior records sharing the SAME invocation AND producing attempt are ambiguous.
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="1"))
    p1 = _summary("rag-a-pg17-aaaa", test_verdict="pass",
                  provenance=_caller_prov(caller_run_attempt="1"))
    p2 = _summary("rag-a-pg17-aaaa", test_verdict="fail",
                  counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0},
                  provenance=_caller_prov(caller_run_attempt="1"))
    res = _build(plan, [p1, p2], current_run_attempt="3")
    _assert_failed_safe(res)
    dh = [u for u in res["unexpected_results"] if u["kind"] == "duplicate_historical"]
    assert len(dh) == 2
    assert res["historical_results"] == []


def test_multiple_distinct_historical_attempts_allowed_and_deterministic():
    # Distinct producing attempts (1 and 2) for one id are audited; a current (3) fills
    # the leg. Output is byte-identical under reordered input.
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="1"))
    cur = _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="3"))
    h1 = _summary("rag-a-pg17-aaaa", test_verdict="fail",
                  counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0},
                  provenance=_caller_prov(caller_run_attempt="1"))
    h2 = _summary("rag-a-pg17-aaaa", test_verdict="not_run",
                  provenance=_caller_prov(caller_run_attempt="2"))
    forward = _build(plan, [cur, h1, h2], current_run_attempt="3")
    reverse = _build(plan, [h2, cur, h1], current_run_attempt="3")
    assert forward["result_resolved"] is True
    assert [h["producing_attempt"] for h in forward["historical_results"]] == ["1", "2"]
    assert pcr.to_json(forward) == pcr.to_json(reverse)   # byte-identical, order-independent


def test_attempt_output_byte_identical_under_reordered_summaries():
    # Mixed current+prior across two invocations, reordered -> byte-identical JSON.
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")],
                 prov=_plan_prov(run_attempt="1"))
    a_cur = _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="2"))
    a_prior = _summary("rag-a-pg17-aaaa", test_verdict="fail",
                       counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0},
                       provenance=_caller_prov(caller_run_attempt="1"))
    b_cur = _summary("rag-b-pg18-bbbb", provenance=_caller_prov(caller_run_attempt="2"))
    fwd = _build(plan, [a_cur, a_prior, b_cur], current_run_attempt="2")
    rev = _build(plan, [b_cur, a_prior, a_cur], current_run_attempt="2")
    assert fwd["result_resolved"] is True
    assert fwd["coverage_status"] == "complete"
    assert pcr.to_json(fwd) == pcr.to_json(rev)


@pytest.mark.parametrize("bad", ["0", "-1", "", "  ", "abc", "1.0", "1 ", None, 1, True, [1], {"x": 1}])
def test_malformed_current_run_attempt_fails_closed(bad):
    # The pure reducer requires current_run_attempt as a positive decimal string; a bad
    # value fails closed (never defaulted from the plan) and nulls the aggregation attempt.
    res = pcr.build_cert_result(_plan([_inv("rag-a-pg17-aaaa")]),
                                [_summary("rag-a-pg17-aaaa")], bad)
    _assert_failed_safe(res)
    assert any("current_run_attempt must be a positive decimal string" in e for e in res["errors"])
    assert res["attempt_context"]["aggregation_run_attempt"] is None
    assert res["historical_results"] == []


def test_fail_closed_has_stable_attempt_context_and_empty_history():
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    plan["plan_resolved"] = False
    res = _build(plan, [_summary("rag-a-pg17-aaaa")], current_run_attempt="2")
    assert res["result_resolved"] is False
    assert res["historical_results"] == []
    assert res["attempt_context"] == {"plan_run_attempt": "1", "aggregation_run_attempt": "2"}


def test_caller_run_attempt_must_be_positive_decimal():
    # A nonblank-but-non-numeric producing attempt is malformed (cannot be classified).
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]),
                 [_summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="one"))],
                 current_run_attempt="1")
    _assert_failed_safe(res)
    assert any(u["kind"] == "malformed" for u in res["unexpected_results"])


def test_historical_duplicate_detected_by_numeric_attempt_not_spelling():
    # "1" and "01" are the SAME producing attempt numerically -> duplicate_historical,
    # even though their string spellings differ. (Both are valid positive decimals.)
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="1"))
    h_one = _summary("rag-a-pg17-aaaa", test_verdict="pass",
                     provenance=_caller_prov(caller_run_attempt="1"))
    h_oh_one = _summary("rag-a-pg17-aaaa", test_verdict="fail",
                        counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0},
                        provenance=_caller_prov(caller_run_attempt="01"))
    res = _build(plan, [h_one, h_oh_one], current_run_attempt="3")
    _assert_failed_safe(res)
    dh = [u for u in res["unexpected_results"] if u["kind"] == "duplicate_historical"]
    assert len(dh) == 2                                   # both retained as evidence
    assert res["historical_results"] == []               # none promoted to history


def test_distinct_numeric_historical_attempts_1_and_2_remain_allowed():
    # "1" and "2" are distinct numeric attempts -> both audited, not a duplicate.
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="1"))
    cur = _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="3"))
    h1 = _summary("rag-a-pg17-aaaa", test_verdict="fail",
                  counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0},
                  provenance=_caller_prov(caller_run_attempt="1"))
    h2 = _summary("rag-a-pg17-aaaa", test_verdict="not_run",
                  provenance=_caller_prov(caller_run_attempt="2"))
    res = _build(plan, [cur, h1, h2], current_run_attempt="3")
    assert res["result_resolved"] is True
    assert not any(u["kind"] == "duplicate_historical" for u in res["unexpected_results"])
    assert [h["producing_attempt"] for h in res["historical_results"]] == ["1", "2"]


def test_numeric_grouping_preserves_original_spelling_and_ordering():
    # A lone "01" prior is audited with its ORIGINAL spelling preserved in the output,
    # and ordering stays deterministic under reordered input.
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="1"))
    cur = _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="3"))
    h_oh_one = _summary("rag-a-pg17-aaaa", test_verdict="not_run",
                        provenance=_caller_prov(caller_run_attempt="01"))
    h_two = _summary("rag-a-pg17-aaaa", test_verdict="fail",
                     counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0},
                     provenance=_caller_prov(caller_run_attempt="2"))
    fwd = _build(plan, [cur, h_oh_one, h_two], current_run_attempt="3")
    rev = _build(plan, [h_two, cur, h_oh_one], current_run_attempt="3")
    assert fwd["result_resolved"] is True
    # sorted by numeric attempt (1 before 2), original spelling "01" preserved verbatim
    assert [h["producing_attempt"] for h in fwd["historical_results"]] == ["01", "2"]
    assert pcr.to_json(fwd) == pcr.to_json(rev)           # deterministic under reorder


# --------------------------------------------------------------------------- #
# package proof: the installed digest and the planned identity rungs
# --------------------------------------------------------------------------- #
_OTHER = "e" * 64


def _one(summary, **inv_over):
    return _build(_plan([_inv("rag-a-pg17-aaaa", **inv_over)]), [summary])


def _no_digest(**kw):
    s = _summary("rag-a-pg17-aaaa", **kw)
    del s["installed_package_sha256"]                    # a summary that predates the field
    return s


def test_matching_digest_and_proven_identity_certify():
    res = _one(_summary("rag-a-pg17-aaaa"))
    leg = res["legs"][0]
    assert (leg["execution_status"], leg["reason_code"], leg["package_digest"]) == ("completed", None, "match")
    assert leg["installed_package_sha256"] == "d" * 64 and leg["unproven_identity_rungs"] == []
    assert (res["execution_status"], res["coverage_status"], res["reason_code"]) == ("completed", "complete", None)


def test_planned_digest_compares_case_insensitively():
    inv = _inv("rag-a-pg17-aaaa")
    inv["package"]["sha256"] = "D" * 64
    res = _build(_plan([inv]), [_summary("rag-a-pg17-aaaa")])
    assert res["legs"][0]["package_digest"] == "match" and res["execution_status"] == "completed"


def test_mismatched_digest_blocks_but_keeps_the_verdict_and_counts():
    res = _one(_summary("rag-a-pg17-aaaa", installed_package_sha256=_OTHER))
    leg = res["legs"][0]
    assert (leg["execution_status"], leg["test_verdict"], leg["reason_code"]) == (
        "incomplete", "pass", "package_digest_mismatch")
    assert leg["package_digest"] == "mismatch" and leg["installed_package_sha256"] == _OTHER
    assert leg["counts"] == {"tests": 3, "failures": 0, "errors": 0, "skipped": 0}
    assert (res["execution_status"], res["test_verdict"], res["reason_code"]) == (
        "incomplete", "pass", "package_digest_mismatch")
    assert res["coverage_status"] == "partial"
    assert res["counts"]["completed"] == 0 and res["counts"]["incomplete"] == 1


@pytest.mark.parametrize("summary", [
    pytest.param(_summary("rag-a-pg17-aaaa", installed_package_sha256=None), id="null"),
    pytest.param(_no_digest(), id="absent-older-summary"),
])
def test_missing_digest_blocks(summary):
    res = _one(summary)
    leg = res["legs"][0]
    assert res["result_resolved"] is True                 # an absent field is not malformed
    assert (leg["execution_status"], leg["reason_code"], leg["package_digest"]) == (
        "incomplete", "package_digest_missing", "missing")
    assert leg["installed_package_sha256"] is None
    assert res["reason_code"] == "package_digest_missing"


@pytest.mark.parametrize("bad", ["D" * 64, "d" * 63, "g" * 64, 7, True, ["d" * 64], ""])
def test_malformed_observed_digest_fails_closed(bad):
    res = _one(_summary("rag-a-pg17-aaaa", installed_package_sha256=bad))
    assert res["result_resolved"] is False and res["reason_code"] == "validation_failure"
    assert res["unexpected_results"][0]["kind"] == "malformed"
    assert "installed_package_sha256" in res["unexpected_results"][0]["evidence"]["reason"]


@pytest.mark.parametrize("pkg", [
    {"name": "x"}, {"name": "x", "sha256": ""}, {"name": "x", "sha256": "d" * 63}, None, "d" * 64])
def test_plan_without_a_valid_planned_digest_fails_closed(pkg):
    inv = _inv("rag-a-pg17-aaaa")
    inv["package"] = pkg
    res = _build(_plan([inv]), [_summary("rag-a-pg17-aaaa")])
    assert res["result_resolved"] is False
    assert any("package.sha256" in e for e in res["errors"])


# Every JSON type a corrupt plan could carry where a string belongs.
_NON_STRINGS = [None, 7, 1.5, True, [], ["2.0.0-1.el9"], {}, {"rpm": "2.0.0-1.el9"}]


def _assert_fails_closed(res):
    """A fail-closed validation result: well formed, serializable, blocking in both modes."""
    spec = importlib.util.spec_from_file_location("pep_cert_gate", str(Path(__file__).parent / "pep_cert_gate.py"))
    G = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(G)
    assert res["result_resolved"] is False and res["reason_code"] == "validation_failure"
    assert res["legs"] == [] and res["execution_status"] == "infra_failure"
    pcr.to_json(res)
    for mode in ("observe", "gate"):
        assert G.decide(res, mode)["workflow_conclusion"] == "failure"


@pytest.mark.parametrize("family", ["rpm", "deb"])
@pytest.mark.parametrize("key,bad", [
    ("own", ""), ("own", "   "), ("own", "absent"),
    ("own", "2.0.0-2.el9"),                        # a pin, but not the planned package's version-release
    ("other", "2.0.0-1.el9"), ("other", "absent"),  # opposite family must be ""
    ("expected_binary", "absent"), ("expected_binary", " "), ("expected_binary", "\t"),
] + [(k, v) for k in ("own", "other", "expected_binary") for v in _NON_STRINGS])
def test_plan_entry_without_its_exact_family_pin_fails_closed(family, key, bad):
    # Every planner-produced certification invocation pins its exact package; a corrupt
    # entry must never read as "L2a not planned".
    inv = _inv("rag-a-pg17-aaaa", family=family)
    field = {"own": "expected_" + family, "other": "expected_" + ("deb" if family == "rpm" else "rpm")}.get(key, key)
    if bad == "absent":
        del inv[field]
    else:
        inv[field] = bad
    res = _build(_plan([inv]), [_summary("rag-a-pg17-aaaa")])
    _assert_fails_closed(res)
    assert any(field in e for e in res["errors"]), res["errors"]


@pytest.mark.parametrize("family", ["", "RPM", "apk", "absent"] + _NON_STRINGS + [["rpm"], {"deb": 1}])
def test_plan_entry_with_an_invalid_family_fails_closed_never_raises(family):
    # Regression: an array/object family is unhashable and used to raise TypeError.
    inv = _inv("rag-a-pg17-aaaa")
    if family == "absent":
        del inv["family"]
    else:
        inv["family"] = family
    res = _build(_plan([inv]), [_summary("rag-a-pg17-aaaa")])
    _assert_fails_closed(res)
    assert any("is not rpm or deb" in e for e in res["errors"])


@pytest.mark.parametrize("field", ["version", "release"])
@pytest.mark.parametrize("bad", ["", "  ", "absent"] + _NON_STRINGS)
def test_plan_package_without_string_version_release_fails_closed(field, bad):
    inv = _inv("rag-a-pg17-aaaa")
    if bad == "absent":
        del inv["package"][field]
    else:
        inv["package"][field] = bad
    res = _build(_plan([inv]), [_summary("rag-a-pg17-aaaa")])
    _assert_fails_closed(res)
    assert any("version and release must be nonblank strings" in e for e in res["errors"])


@pytest.mark.parametrize("family", ["rpm", "deb"])
def test_blanked_pins_cannot_turn_an_unproven_l2a_into_a_pass(family):
    # Regression: both pins blanked in the plan, l2a not_proven, digest still matching.
    # This used to read as "L2a not planned" and reach clean_pass.
    spec = importlib.util.spec_from_file_location("pep_cert_gate", str(Path(__file__).parent / "pep_cert_gate.py"))
    G = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(G)
    inv = _inv("rag-a-pg17-aaaa", family=family, expected_rpm="", expected_deb="")
    s = _summary("rag-a-pg17-aaaa", identity_evidence=_ident(l2a="not_proven", l2b="not_attempted"))
    res = _build(_plan([inv]), [s])
    assert res["result_resolved"] is False
    for mode in ("observe", "gate"):
        dec = G.decide(res, mode)
        assert (dec["certification_state"], dec["workflow_conclusion"]) == ("incomplete", "failure")


def test_l2a_is_always_required_for_a_certification_invocation():
    res = _one(_summary("rag-a-pg17-aaaa", identity_evidence=_ident(l2a="not_attempted")))
    leg = res["legs"][0]
    assert (leg["reason_code"], leg["unproven_identity_rungs"]) == ("identity_unproven", ["l2a"])


def test_preview_leg_is_exempt_from_package_proof():
    s = _summary("rag-a-pg17-aaaa", execution_status="preview", test_verdict="not_run",
                 identity_evidence={"l2a": "not_attempted", "l2b": "not_attempted", "l1": "not_attempted"})
    res = _one(s)
    leg = res["legs"][0]
    assert (leg["execution_status"], leg["package_digest"], leg["unproven_identity_rungs"]) == (
        "preview", "not_required", [])
    assert leg["reason_code"] is None and res["execution_status"] == "preview"


def _ident(l2a="proven", l2b="proven", l1="proven"):
    return {"l2a": l2a, "l2b": l2b, "l1": l1}


@pytest.mark.parametrize("ev,inv_over,unproven", [
    (_ident(l2a="not_proven"), {}, ["l2a"]),                                   # version mismatch
    (_ident(l1="not_attempted"), {}, ["l1"]),                                  # identity never queried
    (_ident(l2a="not_proven", l1="not_attempted"), {}, ["l2a", "l1"]),         # failed precondition
    (_ident(l2b="not_attempted"), {"expected_binary": "2.0.0"}, ["l2b"]),      # binary planned
    (_ident(l2b="not_proven"), {"expected_binary": "2.0.0"}, ["l2b"]),
])
def test_unproven_planned_identity_blocks(ev, inv_over, unproven):
    res = _one(_summary("rag-a-pg17-aaaa", identity_evidence=ev), **inv_over)
    leg = res["legs"][0]
    assert (leg["execution_status"], leg["reason_code"]) == ("incomplete", "identity_unproven")
    assert leg["unproven_identity_rungs"] == unproven and leg["package_digest"] == "match"
    assert res["reason_code"] == "identity_unproven"


@pytest.mark.parametrize("ev", [
    _ident(l2b="not_attempted"),                                 # the RAG replay's evidence
    _ident(l2b="not_proven"),                                    # l2b not planned -> not required
])
def test_l2b_is_required_only_when_an_expected_binary_is_planned(ev):
    res = _one(_summary("rag-a-pg17-aaaa", identity_evidence=ev))
    leg = res["legs"][0]
    assert (leg["execution_status"], leg["reason_code"], leg["unproven_identity_rungs"]) == (
        "completed", None, [])
    assert res["reason_code"] is None and res["coverage_status"] == "complete"


def test_leg_reason_precedence_is_mismatch_then_missing_then_identity():
    bad_id = _ident(l2a="not_proven")
    mism = _one(_summary("rag-a-pg17-aaaa", installed_package_sha256=_OTHER, identity_evidence=bad_id))
    miss = _one(_summary("rag-a-pg17-aaaa", installed_package_sha256=None, identity_evidence=bad_id))
    assert mism["legs"][0]["reason_code"] == "package_digest_mismatch"
    assert miss["legs"][0]["reason_code"] == "package_digest_missing"
    # the identity problem is still recorded beside the digest reason
    assert mism["legs"][0]["unproven_identity_rungs"] == miss["legs"][0]["unproven_identity_rungs"] == ["l2a"]


def test_product_failure_on_unproven_bytes_keeps_failures_but_does_not_count_as_product_fail():
    res = _one(_summary("rag-a-pg17-aaaa", test_verdict="fail", installed_package_sha256=_OTHER))
    leg = res["legs"][0]
    assert (leg["execution_status"], leg["test_verdict"], leg["reason_code"]) == (
        "incomplete", "fail", "package_digest_mismatch")
    assert leg["counts"]["failures"] == 1
    assert (res["execution_status"], res["test_verdict"], res["reason_code"]) == (
        "incomplete", "fail", "package_digest_mismatch")


def test_mismatch_is_recorded_on_an_already_incomplete_leg():
    s = _summary("rag-a-pg17-aaaa", execution_status="incomplete", test_verdict="pass",
                 installed_package_sha256=_OTHER, reason="some reports unreadable")
    leg = _one(s)["legs"][0]
    assert (leg["execution_status"], leg["reason_code"], leg["reason"]) == (
        "incomplete", "package_digest_mismatch", "some reports unreadable")


def test_mismatch_on_an_infra_leg_is_named_but_status_stays_infra():
    s = _summary("rag-a-pg17-aaaa", execution_status="infra_failure", test_verdict="not_run",
                 installed_package_sha256=_OTHER)
    res = _one(s)
    assert (res["legs"][0]["execution_status"], res["legs"][0]["reason_code"]) == (
        "infra_failure", "package_digest_mismatch")
    assert (res["execution_status"], res["reason_code"]) == ("infra_failure", "package_digest_mismatch")


def test_missing_digest_on_an_infra_or_incomplete_leg_keeps_its_own_reason():
    infra = _one(_summary("rag-a-pg17-aaaa", execution_status="infra_failure", test_verdict="not_run",
                          installed_package_sha256=None))
    inc = _one(_summary("rag-a-pg17-aaaa", execution_status="incomplete", test_verdict="pass",
                        installed_package_sha256=None))
    assert infra["legs"][0]["reason_code"] is None and infra["reason_code"] == "infra_leg"
    assert inc["legs"][0]["reason_code"] is None and inc["reason_code"] == "leg_incomplete"
    assert infra["legs"][0]["package_digest"] == inc["legs"][0]["package_digest"] == "missing"


def test_missing_leg_does_not_evaluate_package_proof():
    res = _build(_plan([_inv("rag-a-pg17-aaaa")]), [])
    leg = res["legs"][0]
    assert (leg["package_digest"], leg["installed_package_sha256"], leg["unproven_identity_rungs"]) == (
        None, None, None)
    assert res["reason_code"] == "missing_result"


def _aggregate(*summaries):
    ids = ["rag-a-pg17-aaaa", "rag-b-pg17-bbbb", "rag-c-pg17-cccc"][:len(summaries)]
    plan = _plan([_inv(i) for i in ids])
    return _build(plan, [s(i) for s, i in zip(summaries, ids) if s is not None])


_S_OK = lambda i: _summary(i)
_S_MISMATCH = lambda i: _summary(i, installed_package_sha256=_OTHER)
_S_NO_DIGEST = lambda i: _summary(i, installed_package_sha256=None)
_S_NO_IDENTITY = lambda i: _summary(i, identity_evidence=_ident(l2a="not_proven"))
_S_INFRA = lambda i: _summary(i, execution_status="infra_failure", test_verdict="not_run",
                              installed_package_sha256=None)
_S_PREVIEW = lambda i: _summary(i, execution_status="preview", test_verdict="not_run")
_S_INCOMPLETE = lambda i: _summary(i, execution_status="incomplete", test_verdict="pass")


@pytest.mark.parametrize("legs,status,reason", [
    ((_S_MISMATCH, None), "infra_failure", "package_digest_mismatch"),        # beats missing_result
    ((_S_MISMATCH, _S_INFRA), "infra_failure", "package_digest_mismatch"),    # beats infra_leg
    ((_S_MISMATCH, _S_PREVIEW), "incomplete", "package_digest_mismatch"),     # beats mixed_mode
    ((_S_NO_DIGEST, None), "infra_failure", "missing_result"),
    ((_S_NO_IDENTITY, _S_INFRA), "infra_failure", "infra_leg"),
    ((_S_NO_DIGEST, _S_PREVIEW), "incomplete", "mixed_mode"),
    ((_S_NO_IDENTITY, _S_NO_DIGEST), "incomplete", "package_digest_missing"),
    ((_S_INCOMPLETE, _S_NO_IDENTITY), "incomplete", "identity_unproven"),     # beats leg_incomplete
    ((_S_INCOMPLETE, _S_OK), "incomplete", "leg_incomplete"),
    ((_S_OK, _S_OK), "completed", None),
])
def test_aggregate_reason_precedence(legs, status, reason):
    res = _aggregate(*legs)
    assert (res["execution_status"], res["reason_code"]) == (status, reason)


def test_package_proof_is_deterministic_under_reordering():
    ids = ["rag-a-pg17-aaaa", "rag-b-pg17-bbbb", "rag-c-pg17-cccc"]
    plan = _plan([_inv(i) for i in ids])
    ss = [_S_MISMATCH(ids[0]), _S_NO_DIGEST(ids[1]), _S_NO_IDENTITY(ids[2])]
    assert pcr.to_json(_build(plan, ss)) == pcr.to_json(_build(plan, list(reversed(ss))))


def test_prior_attempt_summary_without_the_field_is_kept_as_history():
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="1"))
    prior = _no_digest(provenance=_caller_prov(caller_run_attempt="1"))
    cur = _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="2"))
    res = _build(plan, [cur, prior], current_run_attempt="2")
    assert res["result_resolved"] is True and res["reason_code"] is None
    assert [(h["producing_attempt"], h["installed_package_sha256"]) for h in res["historical_results"]] == [
        ("1", None)]
    # history never fills or blocks a leg
    assert res["legs"][0]["package_digest"] == "match" and res["execution_status"] == "completed"


def test_prior_attempt_digest_is_audited_verbatim_and_never_promoted():
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="1"))
    prior = _summary("rag-a-pg17-aaaa", installed_package_sha256=_OTHER,
                     provenance=_caller_prov(caller_run_attempt="1"))
    res = _build(plan, [prior], current_run_attempt="2")
    assert res["historical_results"][0]["installed_package_sha256"] == _OTHER
    assert res["legs"][0]["reconciliation"] == "missing" and res["reason_code"] == "missing_result"
