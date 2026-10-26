"""Tests for pipeline/pep_preview_check.py, the PEP Certification Preview assertion.

The good run is rebuilt from the committed fixture inputs (one simulated preview run, see
preview_check_fixtures/README.md) by the REAL collector, gate and report, so the checker is always
exercised against documents the current code produces. Each failure case then breaks one fact
and asserts that the check reports it.
"""
import copy
import json
import re
import shlex
import shutil
from pathlib import Path

import pytest

import pep_cert_gate as G
import pep_cert_report as REP
import pep_preview_check as PC
import pep_result_io as RIO

_FIX = Path(__file__).resolve().parent / "preview_check_fixtures"
_SHA = "1f0e" + "c" * 36
EXPECT = {"EXPECT_REPOSITORY": "pgEdge/pgedge-pep-test", "EXPECT_RUN_ID": "9100000001",
          "EXPECT_RUN_NUMBER": "77", "EXPECT_RUN_ATTEMPT": "1", "EXPECT_SHA": _SHA,
          "EXPECT_REF": "refs/pull/99/merge", "EXPECT_PEP_SHA": _SHA}


def _load(name):
    return json.loads((_FIX / name).read_text())


@pytest.fixture(scope="module")
def good(tmp_path_factory):
    """(docs, env, paths) for the simulated run, built once by the real aggregate code."""
    d = tmp_path_factory.mktemp("preview")
    (d / "dl").mkdir()
    for name, summary in _load("summaries.json").items():
        (d / "dl" / name).mkdir()
        (d / "dl" / name / "summary.json").write_text(json.dumps(summary))
    for name in ("invocation-plan.json", "listing.json", "capture-evidence.json", "cert-plan.json"):
        (d / name).write_text((_FIX / name).read_text())
    out = d / "out"
    out.mkdir()
    assert RIO.main(["--plan", str(d / "invocation-plan.json"), "--artifacts-listing", str(d / "listing.json"),
                     "--download-dir", str(d / "dl"), "--current-run-attempt", "1",
                     "--out", str(out / "cert-result.json"), "--ledger-out", str(out / "collection-ledger.json")]) == 0
    assert G.main(["--result", str(out / "cert-result.json"), "--mode", "observe",
                   "--out", str(out / "cert-decision.json")]) == 0
    assert REP.main(["--result", str(out / "cert-result.json"), "--decision", str(out / "cert-decision.json"),
                     "--ledger", str(out / "collection-ledger.json"), "--legs", str(d / "dl"), "--out", str(out),
                     "--markdown", str(d / "summary.md")]) == 0
    decision = json.loads((out / "cert-decision.json").read_text())
    evidence_id = next(a["id"] for a in _load("listing.json") if a["name"] == "pep-certification-r77-a1")
    env = dict(EXPECT, CERTIFY_RESULT="success", CERT_CONCLUSION=decision["workflow_conclusion"],
               CERT_STATE=decision["certification_state"], CERT_EXECUTION=decision["axes"]["execution_status"],
               CERT_TESTS=decision["axes"]["test_verdict"], CERT_COVERAGE=decision["axes"]["coverage_status"],
               CERT_REASON=decision["reason_code"], CERT_EVIDENCE_ID=str(evidence_id),
               CERT_EVIDENCE_NAME="pep-certification-r77-a1")
    docs = {"cert_result": json.loads((out / "cert-result.json").read_text()), "decision": decision,
            "ledger": json.loads((out / "collection-ledger.json").read_text()),
            "capture_evidence": _load("capture-evidence.json"), "cert_plan": _load("cert-plan.json"),
            "listing": _load("listing.json"), "report_html": (out / PC.REPORT_FILE).read_text()}
    return docs, env, {"evidence_dir": out, "root": d}


def _failures(good, mutate=None, env_changes=None):
    docs, env, _ = good
    docs, env = copy.deepcopy(docs), dict(env, **(env_changes or {}))
    if mutate:
        mutate(docs)
    return PC.check_documents(docs, env, PC.expectations(env))


def _assert_reports(failures, needle):
    assert failures, "the check passed although %r should have failed it" % needle
    assert any(needle in m for m in failures), failures


# --------------------------------------------------------------------------- the good run
def test_the_simulated_preview_run_passes_every_check(good):
    docs, env, _ = good
    exp = PC.expectations(env)
    assert PC.check_coordinator(env, exp) == []
    ids, failures = PC.select_capture_ids(docs["listing"], exp)
    assert failures == [] and set(ids) == {"capture_evidence_id", "cert_plan_id"}
    assert PC.check_documents(docs, env, exp) == []


def test_the_fixture_expectation_is_six_previews_of_two_targets():
    assert PC.EXPECTED_PREVIEWS == 6 and PC.EXPECTED_PG_MAJORS == ("16", "17", "18")
    plan = _load("invocation-plan.json")
    assert len(plan["matrix"]["include"]) == PC.EXPECTED_PREVIEWS
    assert sorted({e["source_cell_id"] for e in plan["matrix"]["include"]}) == sorted(PC.FIXTURE_CELLS)


# --------------------------------------------------------------------------- coordinator
@pytest.mark.parametrize("changes,needle", [
    ({"CERTIFY_RESULT": "failure"}, "coordinator job result"),
    ({"CERTIFY_RESULT": "skipped"}, "coordinator job result"),
    ({"CERTIFY_RESULT": ""}, "coordinator job result"),
    ({"CERT_STATE": "pass"}, "CERT_STATE"),
    ({"CERT_CONCLUSION": "failure"}, "CERT_CONCLUSION"),
    ({"CERT_EXECUTION": "completed"}, "CERT_EXECUTION"),
    ({"CERT_TESTS": "pass"}, "CERT_TESTS"),
    ({"CERT_COVERAGE": "complete"}, "CERT_COVERAGE"),
    ({"CERT_REASON": "missing_result"}, "CERT_REASON"),
    ({"CERT_EVIDENCE_ID": ""}, "not a positive integer"),
    ({"CERT_EVIDENCE_ID": "0"}, "not a positive integer"),
    ({"CERT_EVIDENCE_ID": "12a"}, "not a positive integer"),
    ({"CERT_EVIDENCE_NAME": "pep-certification-r77-a2"}, "evidence artifact name"),
], ids=["job-failed", "job-skipped", "job-unknown", "state-pass", "conclusion-failure", "executed",
        "tests-pass", "coverage-complete", "reason", "id-blank", "id-zero", "id-text", "name-attempt"])
def test_coordinator_check_requires_a_genuine_preview(good, changes, needle):
    env = dict(good[1], **changes)
    _assert_reports(PC.check_coordinator(env, PC.expectations(env)), needle)


# --------------------------------------------------------------------------- select
def _select(good, mutate):
    listing = copy.deepcopy(good[0]["listing"])
    mutate(listing)
    return PC.select_capture_ids(listing, PC.expectations(good[1]))


def _rename(listing, old, new):
    next(a for a in listing if a["name"] == old)["name"] = new


@pytest.mark.parametrize("mutate,needle", [
    (lambda l: l.remove(next(a for a in l if a["name"] == "pep-cert-plan-r77-a1")), "pep-cert-plan-r77-a1, found 0"),
    (lambda l: _rename(l, "pep-capture-evidence-r77-a1", "pep-capture-evidence-r77-a0"), "found 0"),
    (lambda l: next(a for a in l if a["name"] == "pep-cert-plan-r77-a1").update(expired=True), "expired"),
    (lambda l: l.append(dict(l[0], id=99999)), "repeats an artifact name"),
    (lambda l: l.append(dict(l[0], name="other")), "repeats an artifact id"),
    (lambda l: l.append({"id": "7", "name": "x", "expired": False}), "invalid artifact id"),
], ids=["no-plan", "other-attempt", "expired", "dup-name", "dup-id", "bad-id"])
def test_select_takes_only_this_attempts_single_documents(good, mutate, needle):
    ids, failures = _select(good, mutate)
    assert ids == {}
    _assert_reports(failures, needle)


# --------------------------------------------------------------------------- documents
def _legs(d):
    return d["cert_result"]["legs"]


def _target(d, i=0):
    return d["cert_plan"]["cells"][i]["targets"][0]


def _reject_receipt(d):
    d["capture_evidence"]["cells"][0]["verdict"] = "rejected"
    d["capture_evidence"]["counts"].update(accepted_receipt_cells=1, rejected_receipt_cells=1)


def _drop_leg(d):
    _legs(d).pop()
    d["cert_result"]["counts"]["expected_invocations"] = 5


def _missing_leg(d):
    _legs(d)[0].update(reconciliation="missing", execution_status="infra_failure", reason_code="missing_result")


def _ran_tests(d):
    _legs(d)[0].update(execution_status="completed", test_verdict="pass", counts={"tests": 3, "failures": 0,
                                                                                 "errors": 0, "skipped": 0})


def _claim_package_proof(d):
    _legs(d)[0].update(installed_package_sha256="a" * 64, package_digest="match")


def _claim_identity(d):
    _legs(d)[0]["identity_evidence"]["l2a"] = "proven"


def _repeat_pg(d):
    a, b = [l for l in _legs(d) if l["planned_invocation"]["source_cell_id"] == PC.FIXTURE_CELLS[0]][:2]
    b["planned_invocation"]["pg_major"] = a["planned_invocation"]["pg_major"]


def _one_target(d):
    tid = _legs(d)[0]["planned_invocation"]["source_target_id"]
    for leg in _legs(d):
        leg["planned_invocation"]["source_target_id"] = tid


def _other_package(d):
    _legs(d)[0]["planned_invocation"]["package"]["sha256"] = "b" * 64


def _report_drop_row(d):
    d["report_html"] = re.sub(r'<tr id="leg-[^"]*"[^>]*>', "", d["report_html"], count=1)


@pytest.mark.parametrize("mutate,needle", [
    (_reject_receipt, "was not accepted"),
    (lambda d: d["capture_evidence"]["counts"].update(absent_receipt_cells=1), "absent_receipt_cells"),
    (lambda d: d["cert_plan"]["release_intent"].update(simulated=False), "simulated release"),
    (lambda d: d["cert_plan"]["cells"][0].update(publication_state="published"), "publication_state"),
    (lambda d: _target(d).update(eligibility="eligible"), "must never be eligible"),
    (lambda d: _target(d).update(preview_eligibility="ineligible"), "preview_eligibility"),
    (lambda d: d["cert_plan"].update(execution_mode="full"), "execution_mode"),
    (lambda d: d["cert_result"]["coverage_gaps"].append({"reason": "no_enabled_platform"}), "coverage_gaps"),
    (lambda d: d["cert_result"]["counts"].update(planner_gaps=1), "planner_gaps"),
    (_missing_leg, "reconciliation"),
    (lambda d: d["cert_result"]["unexpected_results"].append({"kind": "foreign"}), "unexpected_results"),
    (lambda d: d["cert_result"]["historical_results"].append({"invocation_id": "x"}), "historical_results"),
    (_drop_leg, "expected exactly 6"),
    (_repeat_pg, "expected each of"),
    (_one_target, "both fixture targets"),
    (_ran_tests, "runs no product test"),
    (_claim_package_proof, "installed_package_sha256"),
    (_claim_identity, "identity evidence"),
    (_other_package, "not the captured package"),
    (lambda d: _legs(d)[0]["planned_invocation"].update(source_target_id=None), "is not a cert-plan target"),
    (lambda d: _legs(d)[0]["planned_invocation"].update(pg_major=16), "expected each of"),
    (lambda d: _legs(d)[0].update(invocation_id=None), "report run rows"),
    (lambda d: d["cert_result"]["provenance"].update(repository="someone/else"), "provenance.repository"),
    (lambda d: d["cert_result"]["provenance"].update(run_id="1"), "provenance.run_id"),
    (lambda d: d["cert_result"]["attempt_context"].update(aggregation_run_attempt="2"), "aggregation_run_attempt"),
    (lambda d: d["cert_result"]["provenance"].update(pep_resolved_sha="d" * 40), "pep_resolved_sha"),
    (lambda d: _legs(d)[0]["provenance"].update(pep_resolved_sha="d" * 40), "provenance.pep_resolved_sha"),
    (lambda d: d["capture_evidence"]["provenance"].update(run_attempt="2"), "capture evidence provenance"),
    (lambda d: d["cert_plan"]["provenance"].update(sha="e" * 40), "cert-plan provenance.sha"),
    (lambda d: d["decision"].update(certification_state="pass"), "certification_state"),
    (lambda d: d["decision"].update(requested_mode="gate"), "pep_cert_gate derives"),
    (lambda d: d["ledger"].update(collection_status="failed"), "collection_status"),
    (lambda d: d["ledger"]["candidates"][0].update(extraction="malformed_json"), "not ingested"),
    (lambda d: d["listing"].append({"id": 99001, "name": "pep-summary-extra", "expired": False}), "pep-summary-* artifacts"),
    (lambda d: d["listing"].append({"id": 99002, "name": "pep-capture-error-r77-a1", "expired": False}), "capture error"),
    (lambda d: d.update(report_html=d["report_html"].replace('data-trusted="true"', 'data-trusted="false"')), "verified decision"),
    (lambda d: d.update(report_html=d["report_html"] + '<tr class="gaprow" id="gaprow-0">'), "coverage-gap row"),
    (lambda d: d.update(report_html=d["report_html"] + '<h2 id="issues">'), "report-issues"),
    (_report_drop_row, "report run rows"),
    (lambda d: d.update(report_html=None), "missing or empty"),
    (lambda d: d["cert_plan"]["release_intent"].update(note="https://example.invalid/x"), "contains 'https://'"),
], ids=["receipt-rejected", "receipt-absent", "not-simulated", "published", "strict-eligible",
        "not-preview-eligible", "full-mode", "plan-gap", "planner-gap-count", "missing-result", "unexpected",
        "historical", "five-runs", "repeated-pg", "one-target", "tests-ran", "package-proof", "identity-proof",
        "other-package", "no-target", "pg-int", "no-run-id", "repo", "run", "attempt", "pep-sha", "leg-pep-sha", "capture-attempt", "plan-sha",
        "decision-pass", "decision-not-derived", "ledger-failed", "ledger-anomaly", "extra-summary",
        "capture-error", "report-untrusted", "report-gap", "report-issues", "report-row", "report-missing",
        "url-leak"])
def test_each_broken_fact_fails_the_check(good, mutate, needle):
    _assert_reports(_failures(good, mutate), needle)


@pytest.mark.parametrize("changes,needle", [
    ({"CERT_EVIDENCE_ID": "424242"}, "but the coordinator reported"),
    ({"CERT_REASON": "other"}, "disagrees with cert-decision"),
    ({"EXPECT_RUN_ATTEMPT": "2"}, "provenance.run_attempt"),
    ({"EXPECT_PEP_SHA": "f" * 40}, "pep_requested_ref"),
], ids=["evidence-id", "outputs", "attempt", "revision"])
def test_documents_must_match_the_run_and_the_coordinator_outputs(good, changes, needle):
    _assert_reports(_failures(good, env_changes=changes), needle)


# --------------------------------------------------------------------------- CLI
def _cli_env(good, **changes):
    return dict(good[1], **changes)


def test_cli_verify_passes_and_fails_with_exit_codes(good, tmp_path, capsys):
    _, env, paths = good
    root = paths["root"]
    argv = ["verify", "--evidence-dir", str(paths["evidence_dir"]),
            "--capture-evidence", str(root / "capture-evidence.json"), "--cert-plan", str(root / "cert-plan.json"),
            "--listing", str(root / "listing.json")]
    assert PC.main(argv, env) == 0
    assert "OK 6 previews" in capsys.readouterr().out
    assert PC.main(argv, _cli_env(good, EXPECT_RUN_ID="5")) == 1
    assert "ASSERT FAIL" in capsys.readouterr().out
    bad = argv[:]
    bad[bad.index("--cert-plan") + 1] = str(tmp_path / "absent.json")
    assert PC.main(bad, env) == 2


def test_cli_select_writes_both_ids_only_when_valid(good, tmp_path):
    _, env, paths = good
    out = tmp_path / "gh-output"
    assert PC.main(["select", "--listing", str(paths["root"] / "listing.json"), "--github-output", str(out)], env) == 0
    ids = dict(line.split("=") for line in out.read_text().splitlines())
    listing = {a["name"]: a["id"] for a in _load("listing.json")}
    assert ids == {"capture_evidence_id": str(listing["pep-capture-evidence-r77-a1"]),
                   "cert_plan_id": str(listing["pep-cert-plan-r77-a1"])}
    out2 = tmp_path / "gh-output-2"
    assert PC.main(["select", "--listing", str(paths["root"] / "listing.json"), "--github-output", str(out2)],
                   _cli_env(good, EXPECT_RUN_ATTEMPT="2")) == 1
    assert not out2.exists()


@pytest.mark.parametrize("changes", [{"EXPECT_PEP_SHA": ""}, {"EXPECT_PEP_SHA": "abc"}, {"EXPECT_RUN_ID": ""}],
                         ids=["no-sha", "short-sha", "no-run"])
def test_cli_refuses_to_run_without_trustworthy_expectations(good, changes):
    assert PC.main(["coordinator"], _cli_env(good, **changes)) == 2


# --------------------------------------------------------------------------- the workflow's real steps
# These run the assert job's steps as the workflow wires them: each step's effective environment is
# the job-level env plus the step's own env (GitHub does not carry a step's env into later steps),
# with every ${{ ... }} evaluated against a GitHub context for the simulated run, and the verify
# step's own command line is run in a workspace laid out as its download steps leave it.
_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "pep-certification-preview.yml"
_ENV_ENTRY = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_]*): (.*?)\s*$")


def _assert_job():
    text = _WORKFLOW.read_text()
    return text[re.search(r"^  assert:\s*$", text, re.M).start():]


def _env_block(text, indent):
    """The ``env:`` mapping at ``indent`` spaces in ``text`` (comment lines skipped)."""
    m = re.search(r"^%senv:\n((?:%s  .*\n)+)" % (" " * indent, " " * indent), text, re.M)
    if not m:
        return {}
    lines = [l for l in m.group(1).splitlines() if not l.strip().startswith("#")]
    return dict(_ENV_ENTRY.match(l).groups() for l in lines)


def _step(marker):
    steps = re.split(r"^      - ", _assert_job(), flags=re.M)[1:]
    hits = [s for s in steps if marker in s]
    assert len(hits) == 1, marker
    return "      - " + hits[0]


def _github_context(good, **outputs):
    docs, env, _ = good
    d = docs["decision"]
    ctx = {"github.repository": EXPECT["EXPECT_REPOSITORY"], "github.run_id": EXPECT["EXPECT_RUN_ID"],
           "github.run_number": EXPECT["EXPECT_RUN_NUMBER"], "github.run_attempt": EXPECT["EXPECT_RUN_ATTEMPT"],
           "github.sha": EXPECT["EXPECT_SHA"], "github.ref": EXPECT["EXPECT_REF"],
           "github.workflow_sha": EXPECT["EXPECT_PEP_SHA"], "github.token": "unused-in-these-steps",
           "needs.certify.result": "success"}
    out = {"certification_conclusion": d["workflow_conclusion"], "certification_state": d["certification_state"],
           "execution_status": d["axes"]["execution_status"], "test_verdict": d["axes"]["test_verdict"],
           "coverage_status": d["axes"]["coverage_status"], "reason_code": d["reason_code"],
           "evidence_artifact_id": env["CERT_EVIDENCE_ID"], "evidence_artifact_name": env["CERT_EVIDENCE_NAME"]}
    out.update(outputs)
    ctx.update({"needs.certify.outputs." + k: v for k, v in out.items()})
    return ctx


def _effective_env(marker, ctx, drop=()):
    """The environment the step containing ``marker`` really receives."""
    raw = dict(_env_block(_assert_job(), 4))
    raw.update(_env_block(_step(marker), 8))
    env = {}
    for key, value in raw.items():
        if key in drop:
            continue
        m = re.fullmatch(r"\$\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}", value)
        env[key] = ctx[m.group(1)] if m else value          # an unknown expression is a test error
    return env


def _verify_argv():
    run = _step("pep_preview_check.py verify").split("run: |", 1)[1]
    argv = shlex.split(run.replace("\\\n", " "))
    assert argv[:3] == ["python3", "pipeline/pep_preview_check.py", "verify"], argv
    return argv[2:]


@pytest.fixture
def workspace(good, tmp_path, monkeypatch):
    """The assert job's working directory after its download and listing steps."""
    _, _, paths = good
    shutil.copytree(paths["evidence_dir"], tmp_path / "dl-evidence")
    (tmp_path / "dl-capture").mkdir()
    shutil.copy(paths["root"] / "capture-evidence.json", tmp_path / "dl-capture" / "capture-evidence.json")
    (tmp_path / "dl-cert-plan").mkdir()
    shutil.copy(paths["root"] / "cert-plan.json", tmp_path / "dl-cert-plan" / "cert-plan.json")
    shutil.copy(paths["root"] / "listing.json", tmp_path / "listing.json")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_workflow_steps_pass_valid_evidence_with_their_real_environment(good, workspace, capsys):
    ctx = _github_context(good)
    assert PC.main(["coordinator"], _effective_env("pep_preview_check.py coordinator", ctx)) == 0
    assert PC.main(_verify_argv(), _effective_env("pep_preview_check.py verify", ctx)) == 0, capsys.readouterr().out


@pytest.mark.parametrize("outputs,drop,needle", [
    ({"certification_state": "pass"}, (), "CERT_STATE"),
    ({"test_verdict": ""}, (), "CERT_TESTS"),
    ({"coverage_status": "complete"}, (), "CERT_COVERAGE"),
    ({}, ("CERT_CONCLUSION",), "CERT_CONCLUSION"),
    ({}, ("CERT_REASON",), "CERT_REASON"),
], ids=["state-changed", "verdict-missing", "coverage-changed", "conclusion-unwired", "reason-unwired"])
def test_the_final_step_fails_when_a_coordinator_output_differs_or_is_not_wired(good, workspace, capsys,
                                                                                outputs, drop, needle):
    env = _effective_env("pep_preview_check.py verify", _github_context(good, **outputs), drop)
    assert PC.main(_verify_argv(), env) == 1
    out = capsys.readouterr().out
    assert "disagrees with cert-decision" in out and needle in out, out
