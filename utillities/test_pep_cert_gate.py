"""Offline unit tests for utillities.pep_cert_gate (the pure observe/gate policy).

The helper consumes a cert-result/1 + requested mode and emits a pep-cert-decision/1
that separates certification truth from workflow enforcement. Tests build minimal
cert-result docs by hand (the helper reads axes; it never reduces).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "pep_cert_gate", str(Path(__file__).parent / "pep_cert_gate.py")
)
gate = importlib.util.module_from_spec(_spec)
sys.modules["pep_cert_gate"] = gate
_spec.loader.exec_module(gate)


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _leg(mode="observe", reconciliation="matched", iid="rag-a"):
    # the reducer's package proof, as it records it on a leg it calls completed
    return {"invocation_id": iid, "reconciliation": reconciliation, "enforcement_mode": mode,
            "package_digest": "match", "unproven_identity_rungs": []}


def _missing_leg(iid="rag-b"):
    return {"invocation_id": iid, "reconciliation": "missing", "enforcement_mode": None}


def _cr(*, execution_status="completed", test_verdict="pass", coverage_status="complete",
        result_resolved=True, reason_code=None, legs=None, schema="cert-result/1"):
    return {
        "schema": schema,
        "result_resolved": result_resolved,
        "errors": [],
        "reason_code": reason_code,
        "execution_status": execution_status,
        "test_verdict": test_verdict,
        "coverage_status": coverage_status,
        "legs": [_leg()] if legs is None else legs,
    }


def _d(cr, mode):
    return gate.decide(cr, mode)


def _tuple(dec):
    return (dec["certification_state"], dec["policy_decision"], dec["workflow_conclusion"])


# --------------------------------------------------------------------------- #
# row 1: fail-closed pre-conditions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["", "audit", "OBSERVE", "gate ", None, 1])
def test_invalid_mode_blocks(mode):
    dec = _d(_cr(), mode)
    assert _tuple(dec) == ("incomplete", "block", "failure")
    assert dec["reason_code"] == "invalid_mode"
    assert dec["requested_mode"] == mode                 # echoed verbatim


@pytest.mark.parametrize("bad", [None, "not-a-dict", 42, [], {"schema": "cert-result/1"}])
def test_malformed_or_missing_result_blocks(bad):
    dec = _d(bad, "observe")
    assert _tuple(dec) == ("incomplete", "block", "failure")
    assert dec["reason_code"] in ("malformed_result", "unresolved", "wrong_schema")


def test_wrong_schema_blocks():
    dec = _d(_cr(schema="something/9"), "observe")
    assert dec["reason_code"] == "wrong_schema"
    assert _tuple(dec) == ("incomplete", "block", "failure")


def test_unresolved_blocks():
    dec = _d(_cr(result_resolved=False), "gate")
    assert dec["reason_code"] == "unresolved"
    assert _tuple(dec) == ("incomplete", "block", "failure")


@pytest.mark.parametrize("axis,val", [
    ("execution_status", "weird"),
    ("test_verdict", "maybe"),
    ("coverage_status", "sometimes"),
])
def test_out_of_vocab_axis_is_malformed(axis, val):
    cr = _cr()
    cr[axis] = val
    dec = _d(cr, "observe")
    assert dec["reason_code"] == "malformed_result"
    assert _tuple(dec) == ("incomplete", "block", "failure")


def test_non_list_legs_is_malformed():
    cr = _cr()
    cr["legs"] = "nope"
    dec = _d(cr, "observe")
    assert dec["reason_code"] == "malformed_result"


def test_matched_leg_mode_mismatch_blocks():
    dec = _d(_cr(legs=[_leg("gate")]), "observe")       # matched leg ran gate; requested observe
    assert dec["reason_code"] == "mode_mismatch"
    assert _tuple(dec) == ("incomplete", "block", "failure")


def test_matched_leg_mode_agreement_is_allowed():
    dec = _d(_cr(legs=[_leg("gate"), _leg("gate", iid="rag-b")]), "gate")
    assert _tuple(dec) == ("pass", "allow", "success")   # modes agree -> normal policy


def test_missing_leg_null_mode_does_not_cause_mismatch():
    # A missing leg (enforcement_mode null) must not independently create a mismatch;
    # the infra/missing status blocks via the execution axis instead.
    cr = _cr(execution_status="infra_failure", test_verdict="not_run", coverage_status="partial",
             reason_code="missing_result", legs=[_missing_leg()])
    dec = _d(cr, "observe")
    assert dec["reason_code"] == "missing_result"        # NOT mode_mismatch
    assert _tuple(dec) == ("incomplete", "block", "failure")


# --------------------------------------------------------------------------- #
# row 2: infra_failure / incomplete execution -> block both modes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["observe", "gate"])
def test_infra_failure_blocks_both(mode):
    cr = _cr(execution_status="infra_failure", test_verdict="not_run", coverage_status="partial",
             reason_code="infra_leg", legs=[_leg(mode)])
    assert _tuple(_d(cr, mode)) == ("incomplete", "block", "failure")


@pytest.mark.parametrize("mode", ["observe", "gate"])
def test_missing_result_blocks_both(mode):
    cr = _cr(execution_status="infra_failure", test_verdict="not_run", coverage_status="partial",
             reason_code="missing_result", legs=[_leg(mode), _missing_leg()])
    dec = _d(cr, mode)
    assert dec["reason_code"] == "missing_result"
    assert _tuple(dec) == ("incomplete", "block", "failure")


@pytest.mark.parametrize("mode", ["observe", "gate"])
def test_execution_incomplete_blocks_both(mode):
    cr = _cr(execution_status="incomplete", test_verdict="not_run", coverage_status="partial",
             reason_code="leg_incomplete", legs=[_leg(mode)])
    dec = _d(cr, mode)
    assert dec["reason_code"] == "execution_incomplete"
    assert _tuple(dec) == ("incomplete", "block", "failure")


@pytest.mark.parametrize("mode", ["observe", "gate"])
def test_zero_eligible_blocks_both(mode):
    cr = _cr(execution_status="incomplete", test_verdict="not_run", coverage_status="none",
             reason_code="zero_eligible", legs=[])
    dec = _d(cr, mode)
    assert dec["reason_code"] == "zero_eligible"
    assert _tuple(dec) == ("incomplete", "block", "failure")


@pytest.mark.parametrize("mode", ["observe", "gate"])
@pytest.mark.parametrize("es,verdict,rc", [
    ("incomplete", "pass", "package_digest_mismatch"),
    ("incomplete", "fail", "package_digest_mismatch"),      # failures kept, still not a product_fail
    ("infra_failure", "not_run", "package_digest_mismatch"),
    ("incomplete", "pass", "package_digest_missing"),
    ("incomplete", "pass", "identity_unproven"),
    ("incomplete", "fail", "identity_unproven"),
])
def test_package_proof_reasons_block_both_modes_and_are_named(mode, es, verdict, rc):
    cr = _cr(execution_status=es, test_verdict=verdict, coverage_status="partial",
             reason_code=rc, legs=[_leg(mode)])
    dec = _d(cr, mode)
    assert _tuple(dec) == ("incomplete", "block", "failure")
    assert dec["reason_code"] == rc                      # read from the reducer, never re-derived
    assert dec["axes"]["reason_code"] == rc


@pytest.mark.parametrize("mode", ["observe", "gate"])
@pytest.mark.parametrize("verdict", ["pass", "fail"])
@pytest.mark.parametrize("proof", [
    {"package_digest": "mismatch"}, {"package_digest": "missing"}, {"package_digest": None},
    {"unproven_identity_rungs": ["l2a"]}, {"unproven_identity_rungs": None},
    "absent",                                         # a result produced before package proof existed
])
def test_completed_result_without_package_proof_is_contradictory(mode, verdict, proof):
    leg = _leg(mode)
    if proof == "absent":
        del leg["package_digest"], leg["unproven_identity_rungs"]
    else:
        leg.update(proof)
    cr = _cr(test_verdict=verdict, legs=[_leg(mode, iid="rag-ok"), leg])
    dec = _d(cr, mode)
    assert _tuple(dec) == ("incomplete", "block", "failure")
    assert dec["reason_code"] == "contradictory_axes"


@pytest.mark.parametrize("es,rc,named", [
    ("infra_failure", "infra_leg", "infra_failure"),
    ("incomplete", "mixed_mode", "execution_incomplete"),
    ("incomplete", "some_future_reason", "execution_incomplete"),
])
def test_other_reasons_still_collapse_as_before(es, rc, named):
    cr = _cr(execution_status=es, test_verdict="not_run", coverage_status="partial",
             reason_code=rc, legs=[_leg()])
    assert _d(cr, "observe")["reason_code"] == named


# --------------------------------------------------------------------------- #
# row 3: preview
# --------------------------------------------------------------------------- #
def _preview_cr(mode):
    # The reducer emits an all-preview aggregate as preview + not_run + partial coverage,
    # reason_code null, all legs matched.
    return _cr(execution_status="preview", test_verdict="not_run", coverage_status="partial",
               legs=[_leg(mode)])


def test_preview_observe_reports_success_but_state_preview():
    dec = _d(_preview_cr("observe"), "observe")
    assert _tuple(dec) == ("preview", "report", "success")
    assert dec["certification_state"] == "preview"       # NEVER reported as pass
    assert dec["reason_code"] == "preview"


def test_preview_gate_fails():
    dec = _d(_preview_cr("gate"), "gate")
    assert _tuple(dec) == ("preview", "block", "failure")


# --------------------------------------------------------------------------- #
# row 4: completed + not_run -> block both
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["observe", "gate"])
def test_completed_not_run_blocks_both(mode):
    cr = _cr(execution_status="completed", test_verdict="not_run", coverage_status="complete",
             legs=[_leg(mode)])
    dec = _d(cr, mode)
    assert dec["reason_code"] == "not_run"
    assert _tuple(dec) == ("incomplete", "block", "failure")


# --------------------------------------------------------------------------- #
# row 5: completed product failure
# --------------------------------------------------------------------------- #
def test_product_fail_observe_success_but_state_fail():
    cr = _cr(test_verdict="fail", legs=[_leg("observe")])
    dec = _d(cr, "observe")
    assert _tuple(dec) == ("fail", "report", "success")
    assert dec["certification_state"] == "fail"          # truth preserved despite green
    assert dec["reason_code"] == "product_fail"


def test_product_fail_gate_fails():
    cr = _cr(test_verdict="fail", legs=[_leg("gate")])
    dec = _d(cr, "gate")
    assert _tuple(dec) == ("fail", "block", "failure")


# --------------------------------------------------------------------------- #
# row 6: completed pass + partial coverage
# --------------------------------------------------------------------------- #
def test_partial_coverage_observe_success_but_state_incomplete():
    cr = _cr(test_verdict="pass", coverage_status="partial", legs=[_leg("observe")])
    dec = _d(cr, "observe")
    assert _tuple(dec) == ("incomplete", "report", "success")
    assert dec["certification_state"] == "incomplete"
    assert dec["reason_code"] == "partial_coverage"


def test_partial_coverage_gate_fails():
    cr = _cr(test_verdict="pass", coverage_status="partial", legs=[_leg("gate")])
    dec = _d(cr, "gate")
    assert _tuple(dec) == ("incomplete", "block", "failure")


# --------------------------------------------------------------------------- #
# row 7: completed pass + complete coverage -> pass both
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["observe", "gate"])
def test_clean_pass_succeeds_both(mode):
    dec = _d(_cr(test_verdict="pass", coverage_status="complete", legs=[_leg(mode)]), mode)
    assert _tuple(dec) == ("pass", "allow", "success")
    assert dec["reason_code"] == "clean_pass"


# --------------------------------------------------------------------------- #
# contradictory axes fall-through -> block fail-closed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["observe", "gate"])
def test_completed_pass_none_coverage_is_contradictory(mode):
    cr = _cr(test_verdict="pass", coverage_status="none", legs=[_leg(mode)])
    dec = _d(cr, mode)
    assert dec["reason_code"] == "contradictory_axes"
    assert _tuple(dec) == ("incomplete", "block", "failure")


# --------------------------------------------------------------------------- #
# correction pass: strengthened structural validation + axis contradictions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("leg", [
    {"invocation_id": "x", "reconciliation": "matched", "enforcement_mode": None},   # null mode
    {"invocation_id": "x", "reconciliation": "matched"},                             # absent mode key
    {"invocation_id": "x", "reconciliation": "matched", "enforcement_mode": 7},      # non-string mode
])
def test_matched_leg_bad_mode_is_mode_mismatch(leg):
    dec = _d(_cr(legs=[leg]), "observe")
    assert dec["reason_code"] == "mode_mismatch"
    assert _tuple(dec) == ("incomplete", "block", "failure")


def test_non_dict_leg_is_malformed():
    dec = _d(_cr(legs=["not-a-dict"]), "observe")
    assert dec["reason_code"] == "malformed_result"


def test_unknown_reconciliation_is_malformed():
    dec = _d(_cr(legs=[{"invocation_id": "x", "reconciliation": "weird", "enforcement_mode": "observe"}]),
             "observe")
    assert dec["reason_code"] == "malformed_result"


@pytest.mark.parametrize("errors", [["boom"], "oops", None, {}, 0])
def test_resolved_result_with_bad_errors_is_malformed(errors):
    cr = _cr(legs=[_leg("observe")])
    cr["errors"] = errors                                  # anything but exactly [] is malformed
    dec = _d(cr, "observe")
    assert dec["reason_code"] == "malformed_result"


@pytest.mark.parametrize("rc", [42, [], {"x": 1}, 3.5, True])
def test_malformed_reason_code_type_is_malformed(rc):
    cr = _cr(legs=[_leg("observe")])
    cr["reason_code"] = rc
    dec = _d(cr, "observe")
    assert dec["reason_code"] == "malformed_result"


def test_preview_pass_is_contradictory():
    cr = _cr(execution_status="preview", test_verdict="pass", coverage_status="partial",
             legs=[_leg("observe")])
    assert _d(cr, "observe")["reason_code"] == "contradictory_axes"


@pytest.mark.parametrize("cov", ["complete", "none"])
def test_preview_wrong_coverage_is_contradictory(cov):
    cr = _cr(execution_status="preview", test_verdict="not_run", coverage_status=cov,
             legs=[_leg("observe")])
    assert _d(cr, "observe")["reason_code"] == "contradictory_axes"


@pytest.mark.parametrize("legs", [[], [_missing_leg()]])
def test_preview_zero_or_missing_legs_is_contradictory(legs):
    cr = _cr(execution_status="preview", test_verdict="not_run", coverage_status="partial", legs=legs)
    assert _d(cr, "observe")["reason_code"] == "contradictory_axes"


@pytest.mark.parametrize("mode", ["observe", "gate"])
def test_completed_fail_none_coverage_is_contradictory(mode):
    cr = _cr(test_verdict="fail", coverage_status="none", legs=[_leg(mode)])
    assert _d(cr, mode)["reason_code"] == "contradictory_axes"


def test_completed_pass_with_missing_leg_is_contradictory():
    cr = _cr(test_verdict="pass", coverage_status="complete", legs=[_leg("observe"), _missing_leg()])
    dec = _d(cr, "observe")
    assert dec["reason_code"] == "contradictory_axes"
    assert _tuple(dec) == ("incomplete", "block", "failure")


@pytest.mark.parametrize("es", ["completed", "preview"])
def test_completed_or_preview_with_non_null_reason_code_is_contradictory(es):
    if es == "preview":
        cr = _cr(execution_status="preview", test_verdict="not_run", coverage_status="partial",
                 legs=[_leg("observe")])
    else:
        cr = _cr(legs=[_leg("observe")])
    cr["reason_code"] = "leg_incomplete"                    # reducer emits null here
    assert _d(cr, "observe")["reason_code"] == "contradictory_axes"


# --------------------------------------------------------------------------- #
# copied axes + determinism + conclusion/policy consistency
# --------------------------------------------------------------------------- #
def test_axes_are_copied_verbatim():
    # An infra_failure aggregate legitimately carries a non-null reason_code; the axes
    # are copied straight through regardless of the policy decision.
    cr = _cr(execution_status="infra_failure", test_verdict="not_run", coverage_status="partial",
             reason_code="infra_leg", legs=[_missing_leg()])
    dec = _d(cr, "observe")
    assert dec["axes"] == {
        "result_resolved": True, "execution_status": "infra_failure",
        "test_verdict": "not_run", "coverage_status": "partial", "reason_code": "infra_leg",
    }
    assert dec["schema"] == "pep-cert-decision/1"


def test_axes_null_when_malformed():
    dec = _d({"schema": "cert-result/1", "result_resolved": True,
              "execution_status": "weird", "test_verdict": "pass",
              "coverage_status": "complete", "legs": []}, "observe")
    assert dec["axes"]["execution_status"] is None       # out-of-vocab -> null
    assert dec["axes"]["test_verdict"] == "pass"


def test_conclusion_consistent_with_policy_across_a_matrix():
    # Each case is built per-mode so matched legs carry the requested mode -- the case
    # exercises its INTENDED policy branch rather than passing through mode_mismatch.
    def _cases(mode):
        return [
            ("clean_pass", _cr(legs=[_leg(mode)])),
            ("product_fail", _cr(test_verdict="fail", legs=[_leg(mode)])),
            ("partial_coverage", _cr(test_verdict="pass", coverage_status="partial", legs=[_leg(mode)])),
            ("preview", _preview_cr(mode)),
            ("infra", _cr(execution_status="infra_failure", test_verdict="not_run",
                          coverage_status="partial", reason_code="infra_leg", legs=[_missing_leg()])),
        ]
    for mode in ("observe", "gate"):
        for label, cr in _cases(mode):
            dec = _d(cr, mode)
            # never a mode_mismatch pass-through
            assert dec["reason_code"] != "mode_mismatch", (label, mode)
            expect = "success" if dec["policy_decision"] in ("allow", "report") else "failure"
            assert dec["workflow_conclusion"] == expect, (label, mode)


def test_to_json_is_stable_and_deterministic():
    cr = _cr(test_verdict="fail", legs=[_leg("observe")])
    a = gate.to_json(_d(cr, "observe"))
    b = gate.to_json(_d(cr, "observe"))
    assert a == b
    assert a.endswith("}\n")


# --------------------------------------------------------------------------- #
# the six explicit proofs required
# --------------------------------------------------------------------------- #
def test_proof_product_fail_observe_green_but_state_fail():
    dec = _d(_cr(test_verdict="fail", legs=[_leg("observe")]), "observe")
    assert dec["workflow_conclusion"] == "success" and dec["certification_state"] == "fail"


def test_proof_partial_observe_green_but_state_incomplete():
    dec = _d(_cr(test_verdict="pass", coverage_status="partial", legs=[_leg("observe")]), "observe")
    assert dec["workflow_conclusion"] == "success" and dec["certification_state"] == "incomplete"


def test_proof_preview_observe_green_but_state_preview():
    dec = _d(_preview_cr("observe"), "observe")
    assert dec["workflow_conclusion"] == "success" and dec["certification_state"] == "preview"


def test_proof_preview_gate_fails():
    dec = _d(_preview_cr("gate"), "gate")
    assert dec["workflow_conclusion"] == "failure"


@pytest.mark.parametrize("mode", ["observe", "gate"])
def test_proof_completed_not_run_fails_both(mode):
    dec = _d(_cr(test_verdict="not_run", legs=[_leg(mode)]), mode)
    assert dec["workflow_conclusion"] == "failure"


@pytest.mark.parametrize("mode", ["observe", "gate"])
def test_proof_clean_pass_succeeds_both(mode):
    dec = _d(_cr(legs=[_leg(mode)]), mode)
    assert dec["workflow_conclusion"] == "success" and dec["certification_state"] == "pass"


# --------------------------------------------------------------------------- #
# CLI: exit codes, output writing, malformed/unreadable input, misuse
# --------------------------------------------------------------------------- #
def test_main_clean_pass_writes_decision_returns_zero(tmp_path):
    (tmp_path / "cr.json").write_text(json.dumps(_cr(legs=[_leg("observe")])))
    out = tmp_path / "decision.json"
    code = gate.main(["--result", str(tmp_path / "cr.json"), "--mode", "observe", "--out", str(out)])
    assert code == 0
    dec = json.loads(out.read_text())
    assert dec["schema"] == "pep-cert-decision/1"
    assert dec["workflow_conclusion"] == "success" and dec["certification_state"] == "pass"


def test_main_gate_block_returns_one_and_writes_decision(tmp_path):
    (tmp_path / "cr.json").write_text(json.dumps(_cr(test_verdict="fail", legs=[_leg("gate")])))
    out = tmp_path / "decision.json"
    code = gate.main(["--result", str(tmp_path / "cr.json"), "--mode", "gate", "--out", str(out)])
    assert code == 1
    assert json.loads(out.read_text())["certification_state"] == "fail"


def test_main_unreadable_result_blocks_with_decision_exit1(tmp_path):
    out = tmp_path / "decision.json"
    code = gate.main(["--result", str(tmp_path / "nope.json"), "--mode", "observe", "--out", str(out)])
    assert code == 1                                     # a fail-closed BLOCK is a trustworthy decision
    dec = json.loads(out.read_text())
    assert dec["reason_code"] == "malformed_result" and dec["policy_decision"] == "block"


def test_main_malformed_result_json_blocks_exit1(tmp_path):
    (tmp_path / "cr.json").write_text("{bad json")
    out = tmp_path / "decision.json"
    code = gate.main(["--result", str(tmp_path / "cr.json"), "--mode", "gate", "--out", str(out)])
    assert code == 1
    assert json.loads(out.read_text())["policy_decision"] == "block"


def test_main_invalid_mode_blocks_exit1_not_argparse(tmp_path):
    # An invalid --mode is a fail-closed decision (exit 1) with a written document,
    # NOT argparse misuse (exit 2).
    (tmp_path / "cr.json").write_text(json.dumps(_cr(legs=[_leg("observe")])))
    out = tmp_path / "decision.json"
    code = gate.main(["--result", str(tmp_path / "cr.json"), "--mode", "audit", "--out", str(out)])
    assert code == 1
    assert json.loads(out.read_text())["reason_code"] == "invalid_mode"


def test_main_unwritable_out_is_config_misuse_exit2(tmp_path):
    (tmp_path / "cr.json").write_text(json.dumps(_cr(legs=[_leg("observe")])))
    bad_out = tmp_path / "no-such-dir" / "decision.json"   # parent directory does not exist
    code = gate.main(["--result", str(tmp_path / "cr.json"), "--mode", "observe", "--out", str(bad_out)])
    assert code == 2
    assert not bad_out.exists()


def test_main_missing_required_arg_is_argparse_exit(tmp_path):
    (tmp_path / "cr.json").write_text(json.dumps(_cr()))
    with pytest.raises(SystemExit):
        gate.main(["--result", str(tmp_path / "cr.json"), "--mode", "observe"])  # no --out
