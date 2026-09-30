"""Release adapter inputs (utillities/pep_release_inputs.py) and the adapter workflow
contract (.github/workflows/pep-release-certify.yml).

The GOLDEN documents below are the exact bytes RAG's release.yml (4d3685e) built in its
own pep-evidence job for the same facts, so a release pipeline moving to the adapter
hands pep-certify identical inputs. The matrices use real detector cell entries."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_WF = _HERE.parent / ".github" / "workflows"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, str(_HERE / (name + ".py")))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


RI = _load("pep_release_inputs")
CP = _load("pep_cert_plan")


def _cell(family, os_token, arch, image):
    return {"component": "pkg", "pg_version": "", "pg_major": "", "image": image, "arch": arch,
            "per_pg": "false", "pg_in_name": "false",
            "cell_id": "pepcell.v1.%s.%s.%s.pkg" % (family, os_token, arch), "family": family,
            "os": os_token, "normalized_arch": arch, "pg_coupled": False,
            "build_pg_major": None, "build_pg_version": None}


RPM = json.dumps({"include": [_cell("rpm", "el-9", "arm64", "almalinux:9"),
                              _cell("rpm", "el-10", "amd64", "almalinux:10")]})
DEB = json.dumps({"include": [_cell("deb", "noble", "amd64", "ubuntu:noble"),
                              _cell("deb", "bookworm", "arm64", "debian:bookworm")]})
EMPTY = '{"include":[]}'

REAL = {"rpm_matrix": RPM, "deb_matrix": DEB, "component": "rag", "version": "2.0.0", "buildnum": "1",
        "effective_tag": "v2.0.0", "channel": "staging", "simulated": "false",
        "rpm_publication": "success", "deb_publication": "success", "enforcement": "observe"}
SIMULATED = dict(REAL, effective_tag="v2.0.0-test1", simulated="true",
                 rpm_publication="skipped", deb_publication="skipped")

INTENT_REAL = ('{"logical_component":"rag","intended_version":"2.0.0","intended_buildnum":"1",'
               '"effective_tag":"v2.0.0","channel":"staging","simulated":false}')
INTENT_SIM = ('{"logical_component":"rag","intended_version":"2.0.0","intended_buildnum":"1",'
              '"effective_tag":"v2.0.0-test1","channel":"staging","simulated":true}')


def _inputs(c):
    return {k: c[k] for k in ("release_intent", "publication_results", "execution_mode", "enforcement")}


# --------------------------------------------------------------- equivalence (golden)
@pytest.mark.parametrize("facts,want", [
    (REAL, {"release_intent": INTENT_REAL, "publication_results": '{"rpm":"success","deb":"success"}',
            "execution_mode": "full", "enforcement": "observe"}),
    (SIMULATED, {"release_intent": INTENT_SIM, "publication_results": '{"rpm":"skipped","deb":"skipped"}',
                 "execution_mode": "preview", "enforcement": "observe"}),
    (dict(REAL, rpm_publication="failure"),
     {"release_intent": INTENT_REAL, "publication_results": '{"rpm":"failure","deb":"success"}',
      "execution_mode": "full", "enforcement": "observe"}),
    (dict(REAL, rpm_publication="cancelled", deb_publication="skipped"),
     {"release_intent": INTENT_REAL, "publication_results": '{"rpm":"cancelled","deb":"skipped"}',
      "execution_mode": "full", "enforcement": "observe"}),
], ids=["real-tag", "simulated", "rpm-publication-failed", "cancelled-and-skipped"])
def test_matches_the_documents_rag_built_itself(facts, want):
    assert _inputs(RI.compose(facts)) == want


def test_cell_counts_are_reported_per_family():
    c = RI.compose(REAL)
    assert (c["rpm_cells"], c["deb_cells"]) == ("2", "2")


def test_utf8_is_kept_as_jq_writes_it():
    c = RI.compose(dict(REAL, effective_tag="v2.0.0-βtest"))
    assert '"effective_tag":"v2.0.0-βtest"' in c["release_intent"]


def test_gate_enforcement_is_passed_through():
    assert RI.compose(dict(REAL, enforcement="gate"))["enforcement"] == "gate"


# --------------------------------------------------------------- publication results
def test_zero_cell_family_without_a_result_is_left_out_not_invented():
    c = RI.compose(dict(REAL, deb_matrix=EMPTY, deb_publication=""))
    assert c["publication_results"] == '{"rpm":"success"}'
    assert c["deb_cells"] == "0"


def test_zero_cell_family_with_an_explicit_result_keeps_it():
    c = RI.compose(dict(REAL, deb_matrix=EMPTY, deb_publication="skipped"))
    assert c["publication_results"] == '{"rpm":"success","deb":"skipped"}'


@pytest.mark.parametrize("absent", ["", None])
def test_family_with_cells_requires_its_result(absent):
    with pytest.raises(RI.InputError, match="rpm_publication is absent although the detector reported 2 rpm"):
        RI.compose(dict(REAL, rpm_publication=absent))


def test_explicit_skipped_is_distinct_from_absent_for_a_family_with_cells():
    assert RI.compose(dict(REAL, rpm_publication="skipped"))["publication_results"] == \
        '{"rpm":"skipped","deb":"success"}'
    with pytest.raises(RI.InputError):
        RI.compose(dict(REAL, rpm_publication=""))


@pytest.mark.parametrize("value", ["succeeded", "SUCCESS", " success", "neutral", "timed_out", "true"])
def test_publication_result_must_be_a_job_conclusion(value):
    with pytest.raises(RI.InputError, match="deb_publication must be one of"):
        RI.compose(dict(REAL, deb_publication=value))


# --------------------------------------------------------------- simulated / enforcement / identity
@pytest.mark.parametrize("value", ["", None, "True", "FALSE", "yes", "1", " true", "false "])
def test_simulated_must_be_exactly_true_or_false(value):
    with pytest.raises(RI.InputError, match="simulated must be exactly"):
        RI.compose(dict(REAL, simulated=value))


@pytest.mark.parametrize("value", ["", "Observe", "strict", None])
def test_enforcement_must_be_observe_or_gate(value):
    with pytest.raises(RI.InputError, match="enforcement must be one of"):
        RI.compose(dict(REAL, enforcement=value))


@pytest.mark.parametrize("field", ["component", "version", "buildnum", "effective_tag", "channel"])
@pytest.mark.parametrize("value", ["", None, "   ", " 2.0.0", "2.0.0 ", "2.0\n0", "a\tb"])
def test_identity_fields_are_nonblank_unpadded_and_single_line(field, value):
    with pytest.raises(RI.InputError, match="%s must be a nonblank string" % field):
        RI.compose(dict(REAL, **{field: value}))


# --------------------------------------------------------------- detector matrices
@pytest.mark.parametrize("raw,why", [
    ("", "rpm_matrix is missing"),
    (None, "rpm_matrix is missing"),
    ("not json", "not valid JSON"),
    ("[]", "must be a JSON object with an 'include' list"),
    ('{"include": {}}', "must be a JSON object with an 'include' list"),
    ('{"cells": []}', "must be a JSON object with an 'include' list"),
    ('{"include": [1]}', r"include\[0\] is not an object"),
    ('{"include": [{"family": "rpm"}]}', r"include\[0\] has no valid cell_id"),
    ('{"include": [{"cell_id": "", "family": "rpm"}]}', r"include\[0\] has no valid cell_id"),
    ('{"include": [{"cell_id": " pepcell.v1.rpm.el-9.arm64.pkg", "family": "rpm"}]}', "has no valid cell_id"),
    ('{"include": [{"cell_id": "pepcell.v1.rpm.el-9.arm64.pkg"}]}', "has family None, expected 'rpm'"),
    (json.dumps({"include": [_cell("deb", "noble", "amd64", "ubuntu:noble")]}), "has family 'deb', expected 'rpm'"),
])
def test_malformed_matrix_is_rejected_never_counted_as_empty(raw, why):
    with pytest.raises(RI.InputError, match=why):
        RI.compose(dict(REAL, rpm_matrix=raw, rpm_publication=""))


def test_duplicate_cell_ids_are_rejected():
    dup = json.dumps({"include": [_cell("rpm", "el-9", "arm64", "almalinux:9")] * 2})
    with pytest.raises(RI.InputError, match="appears more than once"):
        RI.compose(dict(REAL, rpm_matrix=dup))


def test_both_families_empty_is_structurally_valid():
    c = RI.compose(dict(REAL, rpm_matrix=EMPTY, deb_matrix=EMPTY, rpm_publication="", deb_publication=""))
    assert (c["publication_results"], c["rpm_cells"], c["deb_cells"]) == ("{}", "0", "0")


# --------------------------------------------------------------- accepted by PEP's cert-plan
@pytest.mark.parametrize("facts", [REAL, SIMULATED, dict(REAL, deb_matrix=EMPTY, deb_publication="")])
def test_composed_documents_satisfy_the_cert_plan_contract(facts):
    c = RI.compose(facts)
    intent, pubs = json.loads(c["release_intent"]), json.loads(c["publication_results"])
    assert CP._validate_envelope({"release_intent": intent, "publication_results": pubs}) == []
    assert CP._parse_simulated(intent["simulated"]) == (facts["simulated"] == "true", True)
    assert c["execution_mode"] in CP.EXECUTION_MODES


def test_absent_zero_cell_family_reads_as_unpublished_in_the_cert_plan():
    pubs = json.loads(RI.compose(dict(REAL, deb_matrix=EMPTY, deb_publication=""))["publication_results"])
    assert CP._resolve_publication("deb", "available", pubs, False) == ("publish_skipped", "family_push_skipped")
    assert CP._resolve_publication("rpm", "available", pubs, False) == ("publish_confirmed", "family_push_success")


# --------------------------------------------------------------- CLI
def _env(facts, tmp_path=None):
    env = {var: facts[name] for name, var in RI.ENV.items() if facts.get(name) is not None}
    if tmp_path is not None:
        env["GITHUB_OUTPUT"] = str(tmp_path / "out")
    return env


def test_compose_cli_appends_exactly_the_four_inputs_and_counts(tmp_path, capsys):
    assert RI.main(["compose"], _env(REAL, tmp_path)) == 0
    lines = (tmp_path / "out").read_text().splitlines()
    assert lines == ["release_intent=" + INTENT_REAL, 'publication_results={"rpm":"success","deb":"success"}',
                     "execution_mode=full", "enforcement=observe", "rpm_cells=2", "deb_cells=2"]


def test_compose_cli_rejects_with_an_annotation_and_writes_nothing(tmp_path, capsys):
    assert RI.main(["compose"], _env(dict(REAL, simulated="maybe"), tmp_path)) == 2
    assert "::error::release inputs rejected: simulated must be exactly" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_unknown_command_is_a_usage_error():
    assert RI.main(["frobnicate"], {}) == 64


# --------------------------------------------------------------- summary
PASS = {"certification_state": "pass", "certification_conclusion": "success", "execution_status": "completed",
        "test_verdict": "pass", "coverage_status": "complete", "reason_code": "clean_pass",
        "evidence_artifact_name": "pep-certification-r5-a1"}
PEP_SHA = "c" * 40


def test_summary_of_a_clean_real_release():
    s = RI.render_summary(REAL, "success", "success", PASS, PEP_SHA)
    assert s.count("published (the publication job succeeded)") == 2
    assert "Certification completed: state `pass`, reason `clean_pass`" in s
    assert "never changes it" in s and "`full`, enforcement `observe`" in s
    assert "did not" not in s


def test_summary_of_a_product_failure_under_observe_stays_truthful():
    out = dict(PASS, certification_state="fail", test_verdict="fail", reason_code="product_fail")
    s = RI.render_summary(REAL, "success", "success", out, PEP_SHA)
    assert "state `fail`, reason `product_fail`, workflow conclusion `success`" in s


def test_certification_failure_does_not_claim_publication_was_undone():
    out = dict(PASS, certification_state="incomplete", certification_conclusion="failure",
               reason_code="package_digest_mismatch")
    s = RI.render_summary(REAL, "success", "failure", out, PEP_SHA)
    assert "Certification did not pass: the certify job failed (state `incomplete`, reason `package_digest_mismatch`)" in s
    assert "does not undo or change publication" in s
    assert s.count("published (the publication job succeeded)") == 2


def test_certify_failure_without_outputs_still_explains():
    s = RI.render_summary(REAL, "success", "failure", {}, PEP_SHA)
    assert "the certify job failed; see the certify jobs" in s


def test_rejected_inputs_summary_shows_the_reason_and_the_raw_results():
    facts = dict(REAL, rpm_publication="", deb_publication="succeeded")
    s = RI.render_summary(facts, "failure", "skipped", {}, PEP_SHA)
    assert "Certification did not run: the release inputs were rejected (rpm_publication is absent" in s
    assert "missing: no publication result was provided" in s
    assert "invalid result `succeeded`" in s
    assert "published (the publication job succeeded)" not in s


def test_failed_publication_is_never_reported_as_published():
    s = RI.render_summary(dict(REAL, rpm_publication="failure"), "success", "success", PASS, PEP_SHA)
    assert "`failure`: not confirmed: the publication job failed" in s
    assert s.count("published (the publication job succeeded)") == 1


def test_simulated_summary_expects_no_publication():
    s = RI.render_summary(SIMULATED, "success", "success",
                          dict(PASS, certification_state="preview", reason_code="preview"), PEP_SHA)
    assert "Simulated run: no publication is expected" in s
    assert s.count("not published: the publication job was skipped") == 2
    assert "Conflict" not in s


def test_simulated_run_reporting_success_is_shown_as_a_conflict_not_a_publication():
    facts = dict(SIMULATED, rpm_publication="success")
    s = RI.render_summary(facts, "success", "success", dict(PASS, certification_state="preview"), PEP_SHA)
    assert "nothing was published" not in s
    assert "published (the publication job succeeded)" not in s
    assert "Conflict: this run is simulated, but the RPM publication job reports success." in s
    assert "| RPM | 2 cells | `success`: conflict: the run is simulated, yet this publication job reports success" in s
    assert "| DEB | 2 cells | `skipped`: not published: the publication job was skipped |" in s


def test_simulated_run_with_both_families_reporting_success():
    s = RI.render_summary(dict(SIMULATED, rpm_publication="success", deb_publication="success"),
                          "success", "success", {}, PEP_SHA)
    assert "the RPM and DEB publication jobs report success" in s
    assert s.count("conflict: the run is simulated") == 2


def test_simulated_success_keeps_the_compose_output_and_the_cert_plan_fails_closed():
    c = RI.compose(dict(SIMULATED, rpm_publication="success"))
    assert _inputs(c) == {"release_intent": INTENT_SIM, "publication_results": '{"rpm":"success","deb":"skipped"}',
                          "execution_mode": "preview", "enforcement": "observe"}
    pubs = json.loads(c["publication_results"])
    assert CP._resolve_publication("rpm", "available", pubs, True) == \
        ("publish_unconfirmed", "simulated_with_family_push_success")


def test_normalize_failure_with_valid_facts_is_not_called_a_rejection():
    s = RI.render_summary(REAL, "failure", "skipped", {}, PEP_SHA)
    assert "rejected" not in s
    assert "the release inputs job failed before handing certify its inputs" in s
    assert "The release facts themselves are valid" in s
    assert s.count("published (the publication job succeeded)") == 2


def test_a_known_rejection_is_named_even_when_normalize_failed():
    s = RI.render_summary(dict(REAL, simulated="maybe"), "failure", "skipped", {}, PEP_SHA)
    assert "the release inputs were rejected (simulated must be exactly 'true' or 'false'" in s
    assert "release inputs job failed before" not in s


def test_cancelled_normalize_is_reported_as_cancelled():
    assert "Certification was cancelled." in RI.render_summary(REAL, "cancelled", "skipped", {}, PEP_SHA)


def test_docs_caller_example_withholds_docker_hub_secrets_on_simulated_runs():
    section = (_HERE.parent / "docs" / "CI.md").read_text().split("## Release certification adapter", 1)[1]
    example = section.split("```yaml", 1)[1].split("```", 1)[0]
    for name in ("DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"):
        assert re.search(r"^      %s:\s+\$\{\{ needs\.determine-repo-type\.outputs\.simulated == 'false' "
                         r"&& secrets\.%s \|\| '' \}\}$" % (name, name), example, re.M), name
    assert "A rejected input fails the adapter's input job, so the certify job is skipped." in section


def test_zero_cell_family_summary():
    s = RI.render_summary(dict(REAL, deb_matrix=EMPTY, deb_publication=""), "success", "success", PASS, PEP_SHA)
    assert "| DEB | 0 cells | no cells, nothing to publish |" in s


def test_summary_values_cannot_break_the_table():
    s = RI.render_summary(dict(REAL, component="ra|g`\nx"), "success", "success", PASS, PEP_SHA)
    assert "`ra/g' x`" in s


def test_summary_never_raises():
    s = RI.render_summary({}, None, None, {}, None)
    assert s.startswith("## PEP release certification")


def test_summary_cli_reads_the_environment(capsys):
    env = dict(_env(REAL), NORMALIZE_RESULT="success", CERTIFY_RESULT="success", CERT_STATE="pass",
               CERT_CONCLUSION="success", CERT_REASON="clean_pass", PEP_SHA=PEP_SHA)
    assert RI.main(["summary"], env) == 0
    assert "Certification completed: state `pass`, reason `clean_pass`" in capsys.readouterr().out


# --------------------------------------------------------------- workflow contract
ADAPTER = (_WF / "pep-release-certify.yml").read_text()
CERTIFIER = (_WF / "pep-certify.yml").read_text()


def _call_block(text):
    """The workflow_call section (inputs, secrets, outputs) of a workflow."""
    return text.split("workflow_call:", 1)[1].split("\njobs:", 1)[0]


def _declared(text, section):
    block = _call_block(text).split("\n    %s:\n" % section, 1)[1]
    names = []
    for line in block.splitlines():
        if re.match(r"^    \S", line):
            break
        m = re.match(r"^      ([A-Za-z_]+):", line)
        if m:
            names.append(m.group(1))
    return names


def _job(text, name):
    return text.split("\n  %s:\n" % name, 1)[1].split("\n  # ---", 1)[0]


def test_adapter_declares_the_release_facts():
    assert _declared(ADAPTER, "inputs") == ["rpm_matrix", "deb_matrix", "component", "version", "buildnum",
                                            "effective_tag", "channel", "simulated", "rpm_publication",
                                            "deb_publication", "enforcement"]
    assert 'rpm_publication: {required: false, type: string, default: ""}' in ADAPTER
    assert 'deb_publication: {required: false, type: string, default: ""}' in ADAPTER
    assert "enforcement:     {required: false, type: string, default: observe}" in ADAPTER


def test_adapter_passes_through_every_certifier_output():
    names = _declared(CERTIFIER, "outputs")
    assert _declared(ADAPTER, "outputs") == names
    for n in names:
        assert '"${{ jobs.certify.outputs.%s }}"' % n in ADAPTER


def test_adapter_calls_the_certifier_at_its_own_commit_with_its_existing_inputs():
    certify = _job(ADAPTER, "certify")
    assert "    uses: ./.github/workflows/pep-certify.yml\n" in certify
    assert "pgedge-pep-test/.github/workflows/pep-certify.yml@" not in ADAPTER
    passed = re.findall(r"^      ([a-z_]+):\s+\$\{\{", certify.split("    with:\n", 1)[1].split("    secrets:", 1)[0], re.M)
    assert passed == _declared(CERTIFIER, "inputs")


def test_only_docker_hub_secrets_are_forwarded_and_only_for_full_mode():
    assert "secrets: inherit" not in ADAPTER
    assert _declared(ADAPTER, "secrets") == ["DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"]
    forwarded = _job(ADAPTER, "certify").split("    secrets:\n", 1)[1]
    assert re.findall(r"^      ([A-Z_]+):", forwarded, re.M) == ["DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"]
    for s in ("DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"):
        want = r"^      %s:\s+\$\{\{ needs\.normalize\.outputs\.execution_mode == 'full' && secrets\.%s \|\| '' \}\}$" % (s, s)
        assert re.search(want, forwarded, re.M), s
    assert len(re.findall(r"secrets\.", ADAPTER)) == 2


def test_summary_always_runs_and_cannot_change_the_outcome():
    summary = ADAPTER.split("\n  summary:\n", 1)[1]
    assert "    needs: [normalize, certify]\n    if: ${{ always() }}\n" in summary
    assert "    continue-on-error: true" in summary
    assert "if: ${{ failure() }}" in summary          # a minimal summary if rendering fails


def test_facts_reach_scripts_only_through_env():
    for line in ADAPTER.splitlines():
        if "${{ inputs." in line:
            assert re.match(r"^\s+[A-Za-z_]+:\s+\$\{\{ inputs\.[a-z_]+ \}\}\s*$", line), line


def test_adapter_actions_are_pinned_and_permissions_start_empty():
    assert "\npermissions: {}\n" in ADAPTER
    code = "\n".join(l for l in ADAPTER.splitlines() if not l.lstrip().startswith("#"))
    for ref in re.findall(r"uses:\s+(\S+)", code):
        assert ref.startswith("./") or re.search(r"@[0-9a-f]{40}$", ref), ref


def test_selftest_lints_the_adapter():
    selftest = (_WF / "pep-selftest.yml").read_text()
    lint = [l for l in selftest.splitlines() if "rhysd/actionlint@sha256:" in l][0]
    assert ".github/workflows/pep-release-certify.yml" in lint and ".github/workflows/pep-certify.yml" in lint
