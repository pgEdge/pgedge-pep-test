"""Reader-facing presentation of certification results (pipeline/pep_result_presentation.py)
and its workflow wiring: the test-job display names, the per-run result line and the
job-summary report link.

Per-run summaries are built with the real summarizer (pep_result_summary.build_summary),
so the result line is tested against exactly what a test run writes."""
import json
import re
from pathlib import Path

import pytest

import pep_result_presentation as P
import pep_result_summary as RS

_WF = Path(__file__).resolve().parents[2] / ".github" / "workflows"
INTEGRATION = (_WF / "pep-integration.yml").read_text(encoding="utf-8")
CERTIFY = (_WF / "pep-certify.yml").read_text(encoding="utf-8")
ADAPTER = (_WF / "pep-release-certify.yml").read_text(encoding="utf-8")

INV = "rag-debian12-amd64-pg16-335a6d92023e2137"
PEP = "d" * 40
PROV = {"caller_repo": "pgEdge/pgedge-rag-server", "caller_run_id": "100", "caller_run_attempt": "1",
        "caller_sha": "c" * 40, "caller_ref": "refs/tags/v2.0.0",
        "pep_requested_ref": PEP, "pep_resolved_sha": PEP}
PROVEN = {"l2a": "proven", "l2b": "not_attempted", "l1": "proven"}
WHERE = dict(alias="debian12-amd64", pg="16", package="pgedge-rag-server2")
# This job's live context, matching PROV (the provenance the job itself recorded).
CTX = {"repository": "pgEdge/pgedge-rag-server", "run_id": "100", "run_attempt": "1",
       "sha": "c" * 40, "ref": "refs/tags/v2.0.0", "pep_requested_ref": PEP, "pep_resolved_sha": PEP}


def _junit(n_pass=9, n_fail=0, n_skip=3):
    cases = ['<testcase classname="t" name="p%d"/>' % i for i in range(n_pass)]
    cases += ['<testcase classname="t" name="f%d"><failure message="boom"/></testcase>' % i
              for i in range(n_fail)]
    cases += ['<testcase classname="t" name="s%d"><skipped/></testcase>' % i for i in range(n_skip)]
    return ('<testsuite tests="%d" failures="%d" errors="0" skipped="%d">%s</testsuite>'
            % (n_pass + n_fail + n_skip, n_fail, n_skip, "".join(cases)))


def _write(tmp_path, summary, name="summary.json"):
    p = tmp_path / name
    p.write_text(json.dumps(summary) if not isinstance(summary, str) else summary)
    return p


def _preview(tmp_path):
    s, _ = RS.build_summary(preview=True, identity_evidence=None, provenance=PROV, invocation_id=INV)
    return _write(tmp_path, s)


def _full(tmp_path, **kw):
    xml = tmp_path / "r.xml"
    xml.write_text(_junit(**kw))
    s, _ = RS.build_summary(reports=[xml], identity_evidence=PROVEN, provenance=PROV,
                            invocation_id=INV, installed_package_sha256="e" * 64)
    return _write(tmp_path, s)


def line(path, mode, inv=INV, ctx=None, **where):
    return P.result_line(path, execution_mode=mode, expected_invocation_id=inv,
                         run_context=CTX if ctx is None else ctx, **dict(WHERE, **where))


# --------------------------------------------------------------------------- #
# One result line per test run
# --------------------------------------------------------------------------- #
def test_preview_line_says_nothing_was_installed_or_tested(tmp_path):
    out = line(_preview(tmp_path), "preview")
    assert out.startswith("PEP test-run result: PREVIEW · debian12-amd64 · PG16 · pgedge-rag-server2: ")
    assert "nothing was installed and no product tests ran" in out and "not a certification" in out
    assert INV in out and "passed" not in out and "\n" not in out


def test_full_pass_line_counts_the_cases_and_names_the_installed_digest(tmp_path):
    out = line(_full(tmp_path), "full")
    assert out.startswith("PEP test-run result: PASS · ")
    assert "12 test cases: 9 passed, 0 failed, 3 skipped" in out and "SHA-256 eeeeeeeeeeee" in out
    assert out.endswith("This is one test run's result, not the package certification decision.")


def test_full_fail_line(tmp_path):
    out = line(_full(tmp_path, n_pass=7, n_fail=2), "full")
    assert out.startswith("PEP test-run result: FAIL · ") and "12 test cases: 7 passed, 2 failed, 3 skipped" in out


@pytest.mark.parametrize("change", [
    {"caller_run_id": "101"},                         # a foreign run of the same configuration
    {"caller_repo": "pgEdge/other"}, {"caller_sha": "a" * 40}, {"caller_ref": "refs/heads/x"},
])
def test_summary_from_another_workflow_run_is_unverified(tmp_path, change):
    s = json.loads(_full(tmp_path).read_text())
    s["provenance"].update(change)
    out = line(_write(tmp_path, s, "f.json"), "full")
    assert out.startswith("PEP test-run result: UNVERIFIED · ")
    assert "comes from another workflow run (%s differs)" % next(iter(change)) in out
    assert "PASS" not in out and "passed" not in out


def test_summary_from_another_attempt_is_unverified(tmp_path):
    s = json.loads(_full(tmp_path).read_text())
    s["provenance"]["caller_run_attempt"] = "2"
    out = line(_write(tmp_path, s, "a.json"), "full")
    assert out.startswith("PEP test-run result: UNVERIFIED · ")
    assert "comes from attempt 2, not this attempt 1" in out and "PASS" not in out


@pytest.mark.parametrize("key, value, words", [
    ("pep_requested_ref", "main", "names PEP ref main"),
    ("pep_resolved_sha", "b" * 40, "produced by PEP bbbbbbbbbbbb"),
])
def test_summary_from_another_pep_revision_is_unverified(tmp_path, key, value, words):
    out = line(_full(tmp_path), "full", ctx=dict(CTX, **{key: value}) if key == "pep_requested_ref"
               else dict(CTX, pep_resolved_sha=value))
    assert out.startswith("PEP test-run result: UNVERIFIED · ") and "PASS" not in out
    if key == "pep_resolved_sha":
        assert "not this job's checkout bbbbbbbbbbbb" in out
    else:
        assert "not this job's main" in out


@pytest.mark.parametrize("missing", list(P.RUN_CONTEXT_KEYS) + ["all"])
def test_missing_run_context_is_unverified(tmp_path, missing):
    ctx = {} if missing == "all" else dict(CTX, **{missing: ""})
    out = line(_full(tmp_path), "full", ctx=ctx)
    assert out.startswith("PEP test-run result: UNVERIFIED · ") and "run context is incomplete" in out


def test_matching_run_context_case_insensitive_sha(tmp_path):
    out = line(_full(tmp_path), "full", ctx=dict(CTX, pep_resolved_sha=PEP.upper()))
    assert out.startswith("PEP test-run result: PASS · ")


@pytest.mark.parametrize("kind", ["infra", "incomplete"])
def test_preview_request_failures_keep_their_classification_and_reason(tmp_path, kind):
    s, _ = RS.build_summary(identity_evidence=None, provenance=PROV, invocation_id=INV,
                            **({"infra_error": "docker preflight failed"} if kind == "infra"
                               else {"validation_error": "unsupported scenario"}))
    out = line(_write(tmp_path, s), "preview")
    word = "INFRA FAILURE" if kind == "infra" else "INCOMPLETE"
    assert out.startswith("PEP test-run result: %s · " % word)
    assert "the preview request did not complete (%s); nothing is verified." % s["reason"] in out
    assert "PREVIEW" not in out and "passed" not in out and "product tests ran" not in out


@pytest.mark.parametrize("mode", ["bogus", "", None, "Preview", "full "])
def test_unknown_execution_modes_are_rejected(tmp_path, mode):
    for p in (_preview(tmp_path), _full(tmp_path)):
        out = line(p, mode)
        assert out.startswith("PEP test-run result: UNVERIFIED · ") and "unknown execution mode" in out
        assert "PREVIEW ·" not in out and "PASS" not in out


def test_completed_evidence_is_never_a_preview_and_preview_never_completed(tmp_path):
    full = line(_full(tmp_path), "preview")
    assert full.startswith("PEP test-run result: UNVERIFIED · ") and "reports completed" in full
    prev = line(_preview(tmp_path), "full")
    assert prev.startswith("PEP test-run result: UNVERIFIED · ") and "reports preview" in prev


def test_infra_failure_and_incomplete_lines_never_claim_tests(tmp_path):
    infra, _ = RS.build_summary(infra_error="docker preflight failed", identity_evidence=None,
                                provenance=PROV, invocation_id=INV)
    out = line(_write(tmp_path, infra), "full")
    assert out.startswith("PEP test-run result: INFRA FAILURE · ") and "docker preflight failed" in out
    assert "product tests are not verified" in out and "passed" not in out
    rejected, _ = RS.build_summary(validation_error="unsupported scenario", identity_evidence=None,
                                   provenance=PROV, invocation_id=INV)
    out = line(_write(tmp_path, rejected, "v.json"), "full")
    assert out.startswith("PEP test-run result: INCOMPLETE · ") and "passed" not in out


@pytest.mark.parametrize("content, word", [
    (None, "UNAVAILABLE"),              # no summary.json at all (setup failed before it)
    ("{not json", "UNAVAILABLE"),
    ("[]", "UNVERIFIED"),               # not an object
    ("null", "UNVERIFIED"),
])
def test_missing_or_unreadable_summary_is_never_a_result(tmp_path, content, word):
    p = tmp_path / "summary.json"
    if content is not None:
        p.write_text(content)
    out = line(p, "preview")
    assert out.startswith("PEP test-run result: %s · " % word)
    assert "PREVIEW" not in out and "PASS" not in out and "passed" not in out


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(test_verdict="pass"),                              # preview claiming a pass
    lambda s: s.update(counts={"tests": 0, "failures": 0, "errors": 0}),  # incomplete counts
    lambda s: s.update(execution_status="done"),                          # out of vocabulary
    lambda s: s.pop("provenance"),                                        # incomplete evidence
    lambda s: s.update(installed_package_sha256="XYZ"),
])
def test_malformed_summary_is_unverified_not_a_preview(tmp_path, mutate):
    s = json.loads(_preview(tmp_path).read_text())
    mutate(s)
    out = line(_write(tmp_path, s, "m.json"), "preview")
    assert out.startswith("PEP test-run result: UNVERIFIED · ") and "not usable evidence" in out
    assert "nothing was installed" not in out


def test_zero_tests_in_a_full_run_never_reads_as_passed(tmp_path):
    xml = tmp_path / "r.xml"
    xml.write_text(_junit(n_pass=0, n_skip=2))
    s, _ = RS.build_summary(reports=[xml], identity_evidence=PROVEN, provenance=PROV, invocation_id=INV)
    out = line(_write(tmp_path, s), "full")
    assert out.startswith("PEP test-run result: NOT RUN · ") and "no test case executed" in out
    assert "No verified installed-package SHA-256" in out


def test_mismatched_summary_is_unverified(tmp_path):
    p = _preview(tmp_path)
    assert line(p, "preview", inv="rag-other-pg16-1").startswith("PEP test-run result: UNVERIFIED · ")
    assert "names test run %s, not this run" % INV in line(p, "preview", inv="rag-other-pg16-1")
    assert line(p, "preview", inv="").startswith("PEP test-run result: UNVERIFIED · ")
    # A preview result under a full request (or the reverse) is not shown as either.
    assert line(p, "full").startswith("PEP test-run result: UNVERIFIED · ")
    assert line(_full(tmp_path), "preview").startswith("PEP test-run result: UNVERIFIED · ")


def test_hostile_identity_values_stay_on_one_line(tmp_path):
    out = line(_preview(tmp_path), "preview", alias="x\n::error::boom\r", package="p\x1b[31m")
    assert "\n" not in out and "\r" not in out and "\x1b" not in out
    assert line(_preview(tmp_path), "preview", alias=None, pg=None, package=None).startswith(
        "PEP test-run result: PREVIEW · unknown platform · PG ? · unknown package")


def test_result_line_cli_reads_the_environment_and_never_fails(tmp_path, capsys):
    p = _preview(tmp_path)
    env = {"IN_ALIAS": "debian12-amd64", "IN_PG": "16", "IN_PKG": "pgedge-rag-server2",
           "IN_EXEC": "preview", "IN_INVOCATION_ID_VALID": INV, "IN_PEP_REF": PEP, "EXPECTED_PEP_SHA": PEP,
           "GITHUB_REPOSITORY": CTX["repository"], "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
           "GITHUB_SHA": CTX["sha"], "GITHUB_REF": CTX["ref"]}
    assert P.main(["result-line", str(p)], env) == 0
    assert capsys.readouterr().out.startswith("PEP test-run result: PREVIEW · debian12-amd64")
    assert P.main(["result-line", str(p)], dict(env, GITHUB_RUN_ATTEMPT="2")) == 0
    assert capsys.readouterr().out.startswith("PEP test-run result: UNVERIFIED · ")
    assert P.main(["result-line", str(tmp_path / "absent.json")], {}) == 0
    assert "UNAVAILABLE" in capsys.readouterr().out
    assert P.main(["bogus"], {}) == 0


# --------------------------------------------------------------------------- #
# Report link and job summary
# --------------------------------------------------------------------------- #
GOOD = ("https://github.com", "pgEdge/pgedge-rag-server", "37332830883", "11355805781")


def test_artifact_url_from_a_real_upload_id():
    assert P.artifact_url(*GOOD) == \
        "https://github.com/pgEdge/pgedge-rag-server/actions/runs/37332830883/artifacts/11355805781"


@pytest.mark.parametrize("i, bad", [
    (3, ""), (3, None), (3, "0"), (3, "-1"), (3, "12a"), (3, " 12"), (3, "1" * 21), (3, "١٢"),
    (3, True), (3, 12), (2, "0"), (2, ""),
    (1, "pgEdge"), (1, "a/b/c"), (1, "a b/c"), (1, "x/y)](javascript:alert(1)"),
    (0, "http://github.com"), (0, "javascript:alert(1)"), (0, "https://github.com/x"),
    (0, "https://evil.com\n"), (0, None),
])
def test_no_link_unless_every_part_is_well_formed(i, bad):
    parts = list(GOOD)
    parts[i] = bad
    assert P.artifact_url(*parts) is None


def test_report_link_line_is_truthful_without_a_link():
    assert P.report_link_line(None, "pep-certification-r22-a1") == "**Full report:** download link unavailable."
    assert P.report_link_line(None, "x", P.link_unavailable_reason("")) == (
        "**Full report:** download link unavailable: no report artifact ID was provided.")
    assert P.report_link_line(None, "x", P.link_unavailable_reason("123")) == (
        "**Full report:** download link unavailable: the link could not be formed from the run "
        "context and artifact ID.")
    ok = P.report_link_line(P.artifact_url(*GOOD), "pep-certification-r22-a1")
    assert ok.startswith("**Full report:** [Download `pep-certification-r22-a1`](https://github.com/")
    assert "`consolidated-report.html`" in ok
    hostile = P.report_link_line(P.artifact_url(*GOOD), "a`b|c](x)\n")
    assert "`a'b/c](x)`" in hostile and "\n" not in hostile


def test_certification_summary_composes_heading_link_and_body():
    s = P.certification_summary("**Certification: PASS**\n", P.artifact_url(*GOOD), "pep-certification-r9-a1")
    assert s.splitlines()[:3] == ["## PEP certification results", "",
                                  P.report_link_line(P.artifact_url(*GOOD), "pep-certification-r9-a1")]
    assert s.rstrip().endswith("**Certification: PASS**")


@pytest.mark.parametrize("body", [None, "", "   \n"])
def test_missing_body_is_said_plainly(body):
    s = P.certification_summary(body, None, None)
    assert "could not be rendered, so no results are shown" in s and "download link unavailable" in s
    assert "PASS" not in s and "PREVIEW" not in s


def test_certification_summary_cli(tmp_path, capsys):
    body = tmp_path / "summary.md"
    body.write_text("**Certification: PREVIEW**\n")
    env = {"GITHUB_SERVER_URL": GOOD[0], "GITHUB_REPOSITORY": GOOD[1], "GITHUB_RUN_ID": GOOD[2],
           "EVIDENCE_ARTIFACT_ID": GOOD[3], "EVIDENCE_ARTIFACT_NAME": "pep-certification-r22-a1"}
    assert P.main(["certification-summary", "--body", str(body)], env) == 0
    out = capsys.readouterr().out
    assert "/artifacts/11355805781)" in out and "**Certification: PREVIEW**" in out
    assert P.main(["certification-summary", "--body", str(tmp_path / "absent.md")],
                  dict(env, EVIDENCE_ARTIFACT_ID="")) == 0
    out = capsys.readouterr().out
    assert "download link unavailable: no report artifact ID was provided" in out
    assert "could not be rendered" in out and "](" not in out
    assert P.main(["certification-summary", "--body", str(body)], dict(env, GITHUB_RUN_ID="abc")) == 0
    out = capsys.readouterr().out
    assert "download link unavailable: the link could not be formed" in out and "](" not in out


@pytest.mark.parametrize("value, want", [
    ("a|b", r"a\|b"), ("*x* _y_", r"\*x\* \_y\_"), ("[t](u)", r"\[t\](u)"), ("`c`", r"\`c\`"),
    ("<img src=x>", "&lt;img src=x&gt;"), ("a\nb\tc", "a b c"), ("~~s~~", r"\~\~s\~\~"),
    ("x" * 130, "x" * 120 + "..."), (None, ""), (True, ""),
])
def test_md_text_neutralizes_markdown_and_html(value, want):
    assert P.md_text(value) == want


def test_md_code_cannot_break_a_table():
    assert P.md_code("a|b`c\nd") == "`a/b'c d`" and P.md_code("") == "-" and P.md_code(None) == "-"


# --------------------------------------------------------------------------- #
# Workflow wiring
# --------------------------------------------------------------------------- #
def _job(text, job):
    m = re.search(r"^  %s:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)" % re.escape(job), text, re.M | re.S)
    assert m, job
    return m.group(1)


def _step(job_text, name):
    m = re.search(r"^      - name: %s\n(.*?)(?=^      - (?:name|uses):|\Z)" % re.escape(name), job_text, re.M | re.S)
    assert m, name
    return m.group(1)


def test_leg_display_names_lead_with_platform_pg_and_package_and_keep_job_ids():
    leg = _job(CERTIFY, "run")
    assert re.search(r"^    name: \$\{\{ matrix\.container_alias \}\} · PG\$\{\{ matrix\.pg_major \}\} · "
                     r"\$\{\{ matrix\.package_name \}\}$", leg, re.M)
    called = _job(INTEGRATION, "run")
    want = ("    name: ${{ inputs.execution_mode == 'full' && 'full test (install and product tests)' || "
            "(inputs.execution_mode == 'preview' && 'preview (no install, no product tests)' || "
            "'rejected (invalid execution mode)') }}")
    assert want in called.splitlines()
    # The internal job ids are unchanged, and no display name can look like a package-cell marker.
    assert re.search(r"^  run:$", CERTIFY, re.M) and re.search(r"^  run:$", INTEGRATION, re.M)
    for text in (CERTIFY, INTEGRATION):
        for n in re.findall(r"^    name: (.*)$", text, re.M):
            assert "[pep-cell" not in n and "pep-cell" not in n


def test_every_test_run_logs_one_result_line_even_after_a_failure():
    step = _step(_job(INTEGRATION, "run"), "Report the result (ALWAYS)")
    assert "        if: always()\n" in step and "        continue-on-error: true\n" in step
    assert "python3 pipeline/pep_result_presentation.py result-line test-logs/summary.json" in step
    assert "UNAVAILABLE" in step                     # the no-checkout fallback is truthful too
    assert "IN_INVOCATION_ID_VALID: ${{ steps.preflight.outputs.invocation_id }}" in step
    assert "IN_PEP_REF: ${{ inputs.pep_implementation_ref }}" in step
    assert 'EXPECTED_PEP_SHA="$(git rev-parse HEAD 2>/dev/null || true)"' in step
    # Caller values reach the step only through env, never interpolated into the script.
    script = step.split("        run: |\n", 1)[1]
    assert "${{" not in script
    # It is the last step: after Summarize (which may fail the job in gate mode) and the upload.
    names = re.findall(r"^      - name: (.*)$", _job(INTEGRATION, "run"), re.M)
    assert names[-1] == "Report the result (ALWAYS)"
    assert names.index("Summarize (ALWAYS)") < names.index("Upload artifacts (ALWAYS, unique-per-call + rerun-safe name)")


def test_certification_summary_links_only_after_the_upload_and_never_enforces():
    agg = _job(CERTIFY, "aggregate")
    names = re.findall(r"^      - name: (.*)$", agg, re.M)
    assert names.index("Write the certification job summary") == names.index(
        "Upload the combined certification evidence") + 1
    step = _step(agg, "Write the certification job summary")
    assert "        if: ${{ always() }}\n" in step and "        continue-on-error: true\n" in step
    assert "EVIDENCE_ARTIFACT_ID:   ${{ steps.up_evidence.outputs.artifact-id }}" in step
    assert '>> "$GITHUB_STEP_SUMMARY"' in step and "needs.run.outputs" not in agg
    report = _step(agg, "Render the human-readable certification report")
    # The Markdown is written outside out/, so the uploaded evidence artifact is unchanged.
    assert "--markdown report-summary/summary.md" in report and "--out out" in report


def test_release_summary_receives_the_evidence_artifact_id():
    assert "CERT_EVIDENCE_ID: ${{ needs.certify.outputs.evidence_artifact_id }}" in ADAPTER
