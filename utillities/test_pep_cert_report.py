"""Focused tests for pep_cert_report.

Integration cases run through the REAL certification pipeline, so fixture shapes
cannot drift from what the workflow emits:

    pep_result_summary.build_summary   per-leg summary.json, as each leg writes it
    -> pep_result_io.main              collector: ledger + cert-result/1 (real reducer)
    -> pep_cert_gate.main              pep-cert-decision/1
    -> pep_cert_report.main            the report under test

Only the reducer's input plan and the per-leg JUnit XML are synthetic, and both are
validated by that real code (a malformed plan would fail closed). No archives or
downloaded run artifacts are committed. A few unit tests exercise defensive helpers
directly with small hand-built inputs; those are labelled as such.
"""
import json
import os
import re
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

import pep_cert_gate as G
import pep_cert_report as R
import pep_result_io as IO
import pep_result_summary as RS

SHA = "c" * 40
PEP = "d" * 40
REPO = "pgEdge/pgedge-rag-server"
REF = "refs/tags/v2.0.0"
PROVEN = {"l2a": "proven", "l2b": "not_attempted", "l1": "proven"}
REPORT_REL = "consolidated-20260101_000000/report-rpm-rag-16.xml"
JSON3 = ("cert-result.json", "cert-decision.json", "collection-ledger.json")
INV_A = "rag-oel9-amd64-pg16-aaaaaaaaaaaaaaa1"
INV_B = "rag-alma10-arm64-pg16-bbbbbbbbbbbbbbb2"


# --------------------------------------------------------------------------- #
# Real-pipeline fixture builders
# --------------------------------------------------------------------------- #
def planned(inv, *, alias="oel9-amd64", pg="16", family="rpm", arch="amd64",
            pkg="pgedge-rag-server2"):
    rel = "1.el9" if family == "rpm" else "1.trixie"
    return {"invocation_id": inv, "component": "rag", "producer_repo": REPO,
            "container_alias": alias, "pg_major": pg, "family": family, "arch": arch,
            "channel": "staging", "effective_tag": "v2.0.0",
            "expected_version": "2.0.0", "expected_buildnum": "1",
            "expected_rpm": "2.0.0-" + rel if family == "rpm" else "",
            "expected_deb": "2.0.0-" + rel if family == "deb" else "",
            "expected_binary": "", "package_name": pkg,
            "package": {"name": pkg, "version": "2.0.0", "release": rel,
                        "native_arch": "x86_64" if arch == "amd64" else "aarch64",
                        "sha256": "e" * 64},
            "source_cell_id": "pepcell.v1.%s.x.%s.pkg" % (family, arch),
            "source_target_id": "pepcell.v1.%s.x.%s.pkg::%s" % (family, arch, pkg)}


def plan(entries, gaps=()):
    return {"schema": "pep-invocation-plan/1", "plan_resolved": True, "errors": [],
            "provenance": {"repository": REPO, "run_id": "100", "run_attempt": "1",
                           "sha": SHA, "ref": REF},
            "release": {"logical_component": "rag", "intended_version": "2.0.0",
                        "intended_buildnum": "1", "effective_tag": "v2.0.0",
                        "channel": "staging"},
            "coverage_gaps": list(gaps), "matrix": {"include": list(entries)}}


def junit(container="auto-oel9-amd-rhel", *, n_pass=2, n_skip=1, n_error=0,
          fail_msgs=(), suite_tests=None):
    """A pytest-shaped JUnit document whose <testsuite> attributes agree with its
    <testcase> elements (as pytest writes them) unless `suite_tests` overrides."""
    cls = 'classname="component-test.test_pep_rag"'
    cases = ['<testcase %s name="test_p%d[%s]" time="0.1"/>' % (cls, i, container)
             for i in range(n_pass)]
    cases += ['<testcase %s name="test_f%d[%s]" time="0.2"><failure message="%s">trace-f%d'
              '</failure></testcase>' % (cls, i, container, escape(m, {'"': "&quot;"}), i)
              for i, m in enumerate(fail_msgs)]
    cases += ['<testcase %s name="test_s%d[%s]" time="0.0"><skipped message="skip"/>'
              '</testcase>' % (cls, i, container) for i in range(n_skip)]
    cases += ['<testcase %s name="test_e%d[%s]" time="0.0"><error message="teardown boom">'
              'trace-e%d</error></testcase>' % (cls, i, container, i) for i in range(n_error)]
    tests = (n_pass + len(fail_msgs) + n_skip + n_error) if suite_tests is None else suite_tests
    return ('<testsuites><testsuite name="pytest" tests="%d" failures="%d" errors="%d" '
            'skipped="%d">%s</testsuite></testsuites>'
            % (tests, len(fail_msgs), n_error, n_skip, "".join(cases)))


def leg_artifact(dl, name, inv, *, attempt="1", xml=None, manifest="ok", preview=False,
                 extra_reports=(), flat=False):
    """Write one uploaded pep-summary artifact the way a real leg produces it."""
    root = Path(dl) if flat else Path(dl) / name
    root.mkdir(parents=True, exist_ok=True)
    reports = []
    if not preview:
        p = root / REPORT_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(junit() if xml is None else xml)
        p.with_suffix(".html").write_text("<html><body>pytest-html for %s</body></html>" % inv)
        reports.append(p)
        for rel, text in extra_reports:
            q = root / rel
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_text(text)
            reports.append(q)
    prov = {"caller_repo": REPO, "caller_run_id": "100", "caller_run_attempt": attempt,
            "caller_sha": SHA, "caller_ref": REF,
            "pep_requested_ref": PEP, "pep_resolved_sha": PEP}
    summary, _ = RS.build_summary(reports=reports, mode="observe", preview=preview,
                                  identity_evidence=PROVEN, provenance=prov,
                                  invocation_id=inv)
    (root / "summary.json").write_text(json.dumps(summary))
    listed = ["test-logs/" + str(r.relative_to(root)) for r in reports]
    if manifest == "ok":
        (root / "current-run.json").write_text(json.dumps(
            {"report_dir": "test-logs/consolidated-20260101_000000", "reports": listed}))
    elif manifest == "empty":
        (root / "current-run.json").write_text(json.dumps({"reports": []}))
    return summary


class Run:
    def __init__(self, out, html, result, decision):
        self.out, self.html, self.result, self.decision = out, html, result, decision

    def row(self, inv):
        m = re.search(r"<tr[^>]*>(?:(?!</tr>).)*<code>%s</code>(?:(?!</tr>).)*</tr>"
                      % re.escape(inv), self.html, re.S)
        assert m, "no overview row for %s" % inv
        return m.group(0)

    def detail(self, inv):
        return self.out / "details" / ("cert-detail-%s.html" % inv)


def pipeline(tmp_path, entries, names, *, attempt="1", gaps=(), mode="observe"):
    """Run collector -> reducer -> gate -> report exactly as the aggregate job does."""
    dl, out = tmp_path / "dl", tmp_path / "out"
    dl.mkdir(exist_ok=True)
    out.mkdir()
    (tmp_path / "plan.json").write_text(json.dumps(plan(entries, gaps)))
    (tmp_path / "listing.json").write_text(json.dumps(
        [{"id": 1000 + i, "name": n, "size_in_bytes": 1024, "expired": False}
         for i, n in enumerate(names)]))
    IO.main(["--plan", str(tmp_path / "plan.json"),
             "--artifacts-listing", str(tmp_path / "listing.json"),
             "--download-dir", str(dl), "--current-run-attempt", attempt,
             "--out", str(out / "cert-result.json"),
             "--ledger-out", str(out / "collection-ledger.json")])
    G.main(["--result", str(out / "cert-result.json"), "--mode", mode,
            "--out", str(out / "cert-decision.json")])
    before = {f: (out / f).read_bytes() for f in JSON3}
    rc = R.main(["--result", str(out / "cert-result.json"),
                 "--decision", str(out / "cert-decision.json"),
                 "--ledger", str(out / "collection-ledger.json"),
                 "--legs", str(dl), "--out", str(out)])
    assert rc == 0
    for f in JSON3:  # the report never edits the authoritative JSON
        assert (out / f).read_bytes() == before[f], f
    return Run(out, (out / "consolidated-report.html").read_text(encoding="utf-8"),
               json.loads((out / "cert-result.json").read_text()),
               json.loads((out / "cert-decision.json").read_text()))


def _nums(row):
    return [int(n) for n in re.findall(r"class=num>(\d+)<", row)]


# --------------------------------------------------------------------------- #
# Happy path, links, multi-package identity
# --------------------------------------------------------------------------- #
def test_happy_path_rows_counts_links_match_the_json(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A,
                 xml=junit(fail_msgs=["AssertionError: boom"]))
    leg_artifact(tmp_path / "dl", "pep-summary-b-a1", INV_B,
                 xml=junit(container="auto-alma10-arm-rhel", n_pass=3, n_skip=0))
    run = pipeline(tmp_path, [planned(INV_A), planned(INV_B, alias="alma10-arm64", arch="arm64")],
                   ["pep-summary-a-a1", "pep-summary-b-a1"])
    assert run.result["result_resolved"] is True
    for leg in run.result["legs"]:
        c = leg["counts"]
        passed = c["tests"] - c["failures"] - c["errors"] - c["skipped"]
        row = run.row(leg["invocation_id"])
        assert _nums(row)[1:] == [c["tests"], passed, c["failures"] + c["errors"], c["skipped"]]
        assert ("st-fail" if leg["test_verdict"] == "fail" else "st-pass") in row
        assert run.detail(leg["invocation_id"]).is_file()
    assert "Report issues</h3><div class=\"v\" style=\"color:#1e293b\">0<" in run.html
    # every overview link and every detail back-link resolves inside the bundle
    for href in re.findall(r'href="(details/[^"]+)"', run.html):
        page = run.out / href
        assert page.is_file()
        for back in re.findall(r'href="(\.\./[^"]+)"', page.read_text()):
            assert (page.parent / back).resolve().is_file(), back


def test_two_packages_on_one_platform_are_separate_and_unambiguous(tmp_path):
    inv_p, inv_q = INV_A, "rag-oel9-amd64-pg16-ccccccccccccccc3"
    leg_artifact(tmp_path / "dl", "pep-summary-p-a1", inv_p)
    leg_artifact(tmp_path / "dl", "pep-summary-q-a1", inv_q)
    run = pipeline(tmp_path, [planned(inv_p, pkg="pgedge-rag-server2"),
                              planned(inv_q, pkg="pgedge-rag-extra")],
                   ["pep-summary-p-a1", "pep-summary-q-a1"])
    assert "pgedge-rag-server2" in run.row(inv_p) and "pgedge-rag-extra" in run.row(inv_q)
    page_p, page_q = run.detail(inv_p).read_text(), run.detail(inv_q).read_text()
    assert "oel9-amd64 · pgedge-rag-server2" in page_p and "pgedge-rag-extra" not in page_p
    assert "oel9-amd64 · pgedge-rag-extra" in page_q and "pgedge-rag-server2" not in page_q
    assert 'title="%s"' % inv_p in page_p and 'title="%s"' % inv_q in page_q


# --------------------------------------------------------------------------- #
# Attempts: partial rerun (re-run failed jobs) and duplicate results
# --------------------------------------------------------------------------- #
def test_partial_rerun_links_only_the_current_attempt(tmp_path):
    dl = tmp_path / "dl"
    # A failed in attempt 1 and was re-executed (passing) in attempt 2; B was
    # carried forward from attempt 1 only. The plan stays at capture attempt 1.
    leg_artifact(dl, "pep-summary-a-a1", INV_A, attempt="1",
                 xml=junit(fail_msgs=["attempt-one failure"]))
    leg_artifact(dl, "pep-summary-a-a2", INV_A, attempt="2", xml=junit(n_pass=3))
    leg_artifact(dl, "pep-summary-b-a1", INV_B, attempt="1")
    run = pipeline(tmp_path, [planned(INV_A), planned(INV_B, alias="alma10-arm64", arch="arm64")],
                   ["pep-summary-a-a1", "pep-summary-a-a2", "pep-summary-b-a1"], attempt="2")
    legs = {l["invocation_id"]: l for l in run.result["legs"]}
    assert run.result["provenance"]["run_attempt"] == "1"             # plan attempt
    assert legs[INV_A]["reconciliation"] == "matched"                 # current attempt 2
    assert legs[INV_B]["reconciliation"] == "missing"                 # history only
    assert len(run.result["historical_results"]) == 2
    # The current leg shows ITS attempt-2 detail, never the attempt-1 failure.
    assert "view" in run.row(INV_A) and "st-pass" in run.row(INV_A)
    assert "attempt-one failure" not in run.detail(INV_A).read_text()
    # The synthesized missing leg gets no detail and no false report issue.
    row_b = run.row(INV_B)
    assert "INFRA FAILURE" in row_b and "missing_result" in row_b and "view" not in row_b
    assert not run.detail(INV_B).exists()
    assert "Report issues</h3><div class=\"v\" style=\"color:#1e293b\">0<" in run.html
    # History is visible for audit but excluded from current totals.
    assert "Prior-attempt results (2)" in run.html
    assert "plan attempt 1, aggregation attempt 2" in run.html
    current = sum(l["counts"]["tests"] for l in run.result["legs"] if l["counts"])
    history = sum(h["counts"]["tests"] for h in run.result["historical_results"])
    assert current == 4 and history == 7                    # A@1 (4) + B@1 (3) excluded
    assert re.search(r"Test cases</h3><div class=\"v\"[^>]*>%d<" % current, run.html)
    assert run.decision["certification_state"] != "pass"


def test_duplicate_current_result_fails_closed_with_reducer_errors(tmp_path):
    dl = tmp_path / "dl"
    leg_artifact(dl, "pep-summary-a-a1", INV_A)
    leg_artifact(dl, "pep-summary-a-a1-copy", INV_A)       # same invocation + attempt
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1", "pep-summary-a-a1-copy"])
    assert run.result["result_resolved"] is False and run.result["legs"] == []
    assert "Result unresolved" in run.html
    for err in run.result["errors"]:
        assert err in run.html                              # the reducer's own words
    assert run.html.count('st-issue">DUPLICATE<') == 2
    assert run.decision["certification_state"] == "incomplete"
    assert "Certification state: INCOMPLETE" in run.html
    assert "Certification state: PASS" not in run.html


# --------------------------------------------------------------------------- #
# Legs without per-case evidence that is legitimately absent
# --------------------------------------------------------------------------- #
def test_missing_leg_without_any_artifact(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A)
    run = pipeline(tmp_path, [planned(INV_A), planned(INV_B, alias="alma10-arm64", arch="arm64")],
                   ["pep-summary-a-a1"])
    row_b = run.row(INV_B)
    assert "INFRA FAILURE" in row_b and "missing_result" in row_b and "view" not in row_b
    assert "Report issues</h3><div class=\"v\" style=\"color:#1e293b\">0<" in run.html


def test_preview_legs_are_not_report_issues(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, preview=True, manifest="missing")
    leg_artifact(tmp_path / "dl", "pep-summary-b-a1", INV_B, preview=True, manifest="missing")
    run = pipeline(tmp_path, [planned(INV_A), planned(INV_B, alias="alma10-arm64", arch="arm64")],
                   ["pep-summary-a-a1", "pep-summary-b-a1"])
    assert run.decision["certification_state"] == "preview"
    assert "PREVIEW" in run.row(INV_A) and "PREVIEW" in run.row(INV_B)
    assert "Report issues</h3><div class=\"v\" style=\"color:#1e293b\">0<" in run.html
    assert not list((run.out / "details").glob("cert-detail-*.html"))


# --------------------------------------------------------------------------- #
# Missing, empty, partial or disagreeing per-case evidence (verdict preserved)
# --------------------------------------------------------------------------- #
def test_partially_malformed_report_set_is_incomplete_and_flagged(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A,
                 extra_reports=[("consolidated-20260101_000000/report-extra.xml", "<not-valid")])
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"])
    leg = run.result["legs"][0]
    assert leg["execution_status"] == "incomplete"                   # summarizer + reducer
    row = run.row(INV_A)
    assert "INCOMPLETE" in row and leg["reason"] in row              # reducer reason shown
    assert "view" in row and "partial report set" in run.html
    assert run.detail(INV_A).is_file()


def test_zero_parsed_cases_while_leg_claims_tests(tmp_path):
    xml = ('<testsuites><testsuite name="pytest" tests="5" failures="0" errors="0" '
           'skipped="0"></testsuite></testsuites>')
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, xml=xml)
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"])
    assert run.result["legs"][0]["test_verdict"] == "pass"            # authoritative
    row = run.row(INV_A)
    assert "st-pass" in row and "NO DETAIL" in row
    assert "report parsed zero test cases (leg claims 5)" in run.html
    assert not run.detail(INV_A).exists()


def test_parsed_counts_disagree_with_authoritative_counts(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A,
                 xml=junit(n_pass=2, n_skip=1, suite_tests=4))    # 3 elements, attrs say 4
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"])
    assert run.result["legs"][0]["counts"]["tests"] == 4
    assert "counts differ from authoritative leg counts" in run.html
    assert "view" in run.row(INV_A) and "st-pass" in run.row(INV_A)


@pytest.mark.parametrize("manifest", ["missing", "empty"])
def test_manifest_without_reports_while_leg_claims_tests(tmp_path, manifest):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, manifest=manifest,
                 xml=junit(fail_msgs=["boom"]))
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"])
    row = run.row(INV_A)
    assert "st-fail" in row and "NO DETAIL" in row
    assert "no test-case report listed for this leg" in run.html


def test_single_artifact_flat_download_layout(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, flat=True)
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"])
    ledger = json.loads((run.out / "collection-ledger.json").read_text())
    assert ledger["candidates"][0]["source_path"] == "summary.json"  # collector flat layout
    assert run.detail(INV_A).is_file() and "view" in run.row(INV_A)


# --------------------------------------------------------------------------- #
# Overview presentation: clues, card math, ordering, coverage gaps
# --------------------------------------------------------------------------- #
def test_failing_case_clues_in_overview_rows(tmp_path):
    msgs = ['AssertionError: Failed to download pgedge-rsa.pub: exec: "wget": '
            'executable file not found in $PATH',
            "Failed: 'file' command not found in container. Please install it first.",
            "AssertionError: third failure"]
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, xml=junit(fail_msgs=msgs))
    leg_artifact(tmp_path / "dl", "pep-summary-b-a1", INV_B, xml=junit(n_pass=3))
    run = pipeline(tmp_path, [planned(INV_A), planned(INV_B, alias="alma10-arm64", arch="arm64")],
                   ["pep-summary-a-a1", "pep-summary-b-a1"])
    row = run.row(INV_A)
    assert "<code>test_f0</code> &mdash; Failed to download pgedge-rsa.pub" in row
    assert "<code>test_f1</code> &mdash;" in row and "command not found in container" in row
    assert "+1 more failing case(s)" in row
    assert 'class="clue"' not in run.row(INV_B)


def test_tc_passed_card_subtracts_errors(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A,
                 xml=junit(n_pass=2, n_skip=1, n_error=1, fail_msgs=["boom"]))
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"])
    assert run.result["legs"][0]["counts"] == {"tests": 5, "failures": 1, "errors": 1, "skipped": 1}
    assert re.search(r"TC passed</h3><div class=\"v\"[^>]*>2<", run.html)
    assert re.search(r"TC failed</h3><div class=\"v\"[^>]*>2<", run.html)
    assert "counts differ" not in run.html


def test_failures_are_listed_first(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, xml=junit(n_pass=3))
    leg_artifact(tmp_path / "dl", "pep-summary-b-a1", INV_B, xml=junit(fail_msgs=["boom"]))
    run = pipeline(tmp_path, [planned(INV_A), planned(INV_B, alias="alma10-arm64", arch="arm64")],
                   ["pep-summary-a-a1", "pep-summary-b-a1"])
    assert run.html.index(INV_B) < run.html.index(INV_A)


def test_planned_coverage_gaps_are_listed_as_not_tested(tmp_path):
    gap = {"arch": "amd64", "cell_id": "pepcell.v1.deb.bookworm.amd64.pkg", "detail": "bookworm",
           "family": "deb", "os": "bookworm", "physical_package": "pgedge-rag-server2",
           "reason": "no_enabled_platform",
           "target_id": "pepcell.v1.deb.bookworm.amd64.pkg::pgedge-rag-server2"}
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A)
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"], gaps=[gap])
    assert run.result["coverage_status"] == "partial"
    assert "Not tested" in run.html and "NO ENABLED PLATFORM" in run.html and "bookworm" in run.html


# --------------------------------------------------------------------------- #
# Unit tests of defensive helpers (small hand-built inputs, labelled as such)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("es,tv,expect", [   # combinations the reducer accepts
    ("completed", "pass", ("PASS", "pass")),
    ("completed", "fail", ("FAIL", "fail")),
    ("completed", "not_run", ("NOT RUN", "notrun")),          # all tests skipped
    ("incomplete", "fail", ("INCOMPLETE", "incomplete")),
    ("incomplete", "not_run", ("INCOMPLETE", "incomplete")),
    ("infra_failure", "not_run", ("INFRA FAILURE", "infra")),   # a missing leg
    ("preview", "not_run", ("PREVIEW", "preview")),
])
def test_leg_status_vocabulary(es, tv, expect):
    assert R.leg_status({"execution_status": es, "test_verdict": tv}) == expect


def test_leg_status_unknown_value_is_never_pass():
    assert R.leg_status({"execution_status": "completed", "test_verdict": "weird"}) == ("WEIRD", "unknown")


def test_select_leg_evidence_contract():
    """Defensive: exact-provenance selection, independent of attempt numbers."""
    p1 = {"caller_run_id": "100", "caller_run_attempt": "1"}
    p2 = {"caller_run_id": "100", "caller_run_attempt": "2"}
    index = {"inv": [{"provenance": p1}, {"provenance": p2}]}
    matched = {"invocation_id": "inv", "reconciliation": "matched", "provenance": p2}
    missing = {"invocation_id": "inv", "reconciliation": "missing", "provenance": None}
    assert R.select_leg_evidence(matched, index)[0] == {"provenance": p2}
    assert R.select_leg_evidence(missing, index) == (None, "none", None)
    assert R.select_leg_evidence(dict(matched, provenance={"x": "y"}), index)[1] == "no_summary"
    dup = {"inv": [{"provenance": p2}, {"provenance": dict(p2)}]}
    assert R.select_leg_evidence(matched, dup)[1] == "ambiguous"


def test_index_uses_only_one_summary_candidates_inside_the_root(tmp_path):
    """Defensive: inputs the real collector never emits are still refused."""
    leg_artifact(tmp_path, "ok", INV_A)
    leg_artifact(tmp_path, "rejected", INV_B)
    (tmp_path.parent / "evil").mkdir(exist_ok=True)
    (tmp_path.parent / "evil" / "summary.json").write_text(json.dumps({"invocation_id": "x"}))
    ledger = {"candidates": [
        {"artifact_name": "ok", "expired": False, "extraction": "one_summary",
         "source_path": "ok/summary.json"},
        {"artifact_name": "rejected", "expired": False, "extraction": "multiple_summaries",
         "source_path": "rejected/summary.json"},
        {"artifact_name": "evil", "expired": False, "extraction": "one_summary",
         "source_path": "../evil/summary.json"}]}
    assert set(R.build_summary_index(tmp_path, ledger)) == {INV_A}


def _tree(root, rel, text="<x/>"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_resolve_reports_strips_prefix_rejects_escape_and_collapses_copies(tmp_path):
    root = tmp_path / "art"
    _tree(root, "consolidated-1/report.xml", "<a/>")
    _tree(root, "rag/16/report.xml", "<a/>")                  # byte-identical copy
    _tree(tmp_path, "secret.xml")
    reports, problems = R._resolve_reports(root, [
        "test-logs/consolidated-1/report.xml", "test-logs/rag/16/report.xml",
        "../secret.xml", "test-logs/missing.xml"])
    assert [p.name for p in reports] == ["report.xml"]
    assert any("escapes" in p for p in problems) and any("not found" in p for p in problems)


def test_resolve_reports_rejects_symlink_escape(tmp_path):
    root = tmp_path / "art"
    root.mkdir()
    outside = _tree(tmp_path, "outside.xml")
    try:
        os.symlink(outside, root / "link.xml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported")
    reports, problems = R._resolve_reports(root, ["test-logs/link.xml"])
    assert reports == [] and any("escapes" in p for p in problems)


def test_banner_never_trusts_an_invalid_decision():
    banner = R._banner({"certification_state": "pass"})            # no schema
    assert "UNKNOWN" in banner and "#10b981" not in banner
    ok = R._banner({"schema": R.DECISION_SCHEMA, "certification_state": "pass",
                    "workflow_conclusion": "success", "requested_mode": "gate",
                    "policy_decision": "allow", "reason_code": "clean_pass"})
    assert "Certification state: PASS" in ok and "#10b981" in ok


def _main(out, **paths):
    return R.main(["--result", str(paths.get("result", out / "cert-result.json")),
                   "--decision", str(paths.get("decision", out / "cert-decision.json")),
                   "--ledger", str(paths.get("ledger", out / "collection-ledger.json")),
                   "--legs", str(out), "--out", str(out)])


def test_main_wrong_shape_result_writes_fallback_without_false_pass(tmp_path):
    (tmp_path / "cert-result.json").write_text(json.dumps(["not", "a", "dict"]))
    (tmp_path / "cert-decision.json").write_text(json.dumps(
        {"schema": R.DECISION_SCHEMA, "certification_state": "fail",
         "workflow_conclusion": "success", "requested_mode": "observe",
         "policy_decision": "report", "reason_code": "product_fail"}))
    (tmp_path / "collection-ledger.json").write_text(json.dumps({"candidates": []}))
    assert _main(tmp_path) == 0
    html = (tmp_path / "consolidated-report.html").read_text()
    assert "must be a JSON object" in html and "Certification state: FAIL" in html


def test_main_missing_inputs_write_fallback_without_false_pass(tmp_path):
    assert _main(tmp_path, result=tmp_path / "nope.json", decision=tmp_path / "nope2.json") == 0
    html = (tmp_path / "consolidated-report.html").read_text()
    assert "Certification state: UNKNOWN" in html and "Certification state: PASS" not in html
