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
                 extra_reports=(), flat=False, digest="e" * 64):
    """Write one uploaded pep-summary artifact the way a real leg produces it. `digest`
    is the verified install's package digest (default: planned()'s "e"*64)."""
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
                                  invocation_id=inv,
                                  installed_package_sha256=None if preview else digest)
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
        m = re.search(r'<tr id="leg-%s"[^>]*>.*?</tr>' % re.escape(inv), self.html, re.S)
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
    return [int(n) for n in re.findall(r'class="num">(\d+)<', row)]


VIEW = "View &rarr;"


def card(html, title):
    m = re.search(r'<h3>%s</h3><div class="value">(\d+)</div>' % re.escape(title), html)
    assert m, "no card %r" % title
    return int(m.group(1))


def tc_line(html):
    m = re.search(r'<b>(\d+)</b> total &middot; <b>(\d+)</b> passed &middot; <b>(\d+)</b> failed '
                  r'&middot; <b>(\d+)</b> skipped', html)
    assert m, "no test-case line"
    return tuple(int(x) for x in m.groups())


def vstate(html):
    m = re.search(r'class="vstate">([A-Z]+)<', html)
    assert m, "no verdict state"
    return m.group(1)


def assert_links(out: Path, html: str):
    """Every overview link and action resolves inside the bundle: in-page anchors to
    an element id, openComponent() to a group, files to a real file, and every
    detail page's back-links to a real file."""
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    for href in re.findall(r'href="([^"]+)"', html):
        if href.startswith("#"):
            assert href[1:] in ids, href
        elif not href.startswith("https://"):
            page = out / href
            assert page.is_file(), href
            for back in re.findall(r'href="(\.\./[^"]+)"', page.read_text()):
                assert (page.parent / back).resolve().is_file(), back
    for slug in re.findall(r'openComponent\(&quot;([^&]+)&quot;\)', html):
        assert "comp-" + slug in ids, slug


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
        assert _nums(row) == [c["tests"], passed, c["failures"] + c["errors"], c["skipped"]]
        assert ("st-fail" if leg["test_verdict"] == "fail" else "st-pass") in row
        assert run.detail(leg["invocation_id"]).is_file()
        # the matrix cell and the group row both open this leg's own detail page
        href = 'href="details/cert-detail-%s.html"' % leg["invocation_id"]
        assert run.html.count(href) == 2
    assert card(run.html, "Report issues") == 0
    assert_links(run.out, run.html)


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
    assert VIEW in run.row(INV_A) and "st-pass" in run.row(INV_A)
    assert "attempt-one failure" not in run.detail(INV_A).read_text()
    # The synthesized missing leg gets no detail and no false report issue.
    row_b = run.row(INV_B)
    assert "INFRA FAILURE" in row_b and "missing_result" in row_b and VIEW not in row_b
    assert not run.detail(INV_B).exists()
    assert card(run.html, "Report issues") == 0
    # History is visible for audit but excluded from current totals.
    assert "Prior-attempt results (2)" in run.html
    assert "plan attempt 1, aggregation attempt 2" in run.html
    current = sum(l["counts"]["tests"] for l in run.result["legs"] if l["counts"])
    history = sum(h["counts"]["tests"] for h in run.result["historical_results"])
    assert current == 4 and history == 7                    # A@1 (4) + B@1 (3) excluded
    assert tc_line(run.html)[0] == current
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
    assert vstate(run.html) == "INCOMPLETE" and 'class="vstate">PASS<' not in run.html
    assert 'class="heat' not in run.html                       # no legs -> no matrix
    assert '<details class="audit" id="axes" open>' in run.html  # untrusted -> axes shown


# --------------------------------------------------------------------------- #
# Legs without per-case evidence that is legitimately absent
# --------------------------------------------------------------------------- #
def test_missing_leg_without_any_artifact(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A)
    run = pipeline(tmp_path, [planned(INV_A), planned(INV_B, alias="alma10-arm64", arch="arm64")],
                   ["pep-summary-a-a1"])
    row_b = run.row(INV_B)
    assert "INFRA FAILURE" in row_b and "missing_result" in row_b and VIEW not in row_b
    assert card(run.html, "Report issues") == 0
    # never a pass: amber MISSING cell pointing at its own row, counted once
    cell = re.search(r'<a class="h cell issue" href="#leg-%s"[^>]*>MISSING</a>' % INV_B, run.html)
    assert cell, "missing leg is not an amber MISSING cell"
    assert (card(run.html, "Passed"), card(run.html, "Incomplete / not run")) == (1, 1)
    assert "1 planned test run produced no result" in run.html
    assert_links(run.out, run.html)


def test_preview_legs_are_not_report_issues(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, preview=True, manifest="missing")
    leg_artifact(tmp_path / "dl", "pep-summary-b-a1", INV_B, preview=True, manifest="missing")
    run = pipeline(tmp_path, [planned(INV_A), planned(INV_B, alias="alma10-arm64", arch="arm64")],
                   ["pep-summary-a-a1", "pep-summary-b-a1"])
    assert run.decision["certification_state"] == "preview"
    assert "PREVIEW" in run.row(INV_A) and "PREVIEW" in run.row(INV_B)
    assert card(run.html, "Report issues") == 0 and card(run.html, "Preview") == 2
    assert run.html.count('class="h cell c-preview"') == 2 and 'class="h cell ok' not in run.html
    assert vstate(run.html) == "PREVIEW"
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
    assert VIEW in row and "partial report set" in run.html
    assert run.detail(INV_A).is_file()


def test_zero_parsed_cases_while_leg_claims_tests(tmp_path):
    xml = ('<testsuites><testsuite name="pytest" tests="5" failures="0" errors="0" '
           'skipped="0"></testsuite></testsuites>')
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, xml=xml)
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"])
    assert run.result["legs"][0]["test_verdict"] == "pass"            # authoritative
    row = run.row(INV_A)
    assert "st-pass" in row and "NO DETAIL" in row
    assert "report parsed zero test cases (the test run claims 5)" in run.html
    assert not run.detail(INV_A).exists()


def test_parsed_counts_disagree_with_authoritative_counts(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A,
                 xml=junit(n_pass=2, n_skip=1, suite_tests=4))    # 3 elements, attrs say 4
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"])
    assert run.result["legs"][0]["counts"]["tests"] == 4
    assert "counts differ from authoritative test-run counts" in run.html
    assert VIEW in run.row(INV_A) and "st-pass" in run.row(INV_A)
    # a report issue stays visible on a passing leg: matrix marker, row link, card, banner
    assert re.search(r'<a class="h cell ok issue-marker" href="details/cert-detail-%s.html"[^>]*>'
                     r'&#9888; 0/4</a>' % INV_A, run.html)
    assert 'href="#issue-%s"' % INV_A in run.row(INV_A) and 'id="issue-%s"' % INV_A in run.html
    assert card(run.html, "Report issues") == 1 and card(run.html, "Passed") == 1
    assert "see <a href=\"#issues\">report issues</a>" in run.html
    assert_links(run.out, run.html)


@pytest.mark.parametrize("manifest", ["missing", "empty"])
def test_manifest_without_reports_while_leg_claims_tests(tmp_path, manifest):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, manifest=manifest,
                 xml=junit(fail_msgs=["boom"]))
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"])
    row = run.row(INV_A)
    assert "st-fail" in row and "NO DETAIL" in row
    assert "no test-case report listed for this test run" in run.html


def test_single_artifact_flat_download_layout(tmp_path):
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, flat=True)
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"])
    ledger = json.loads((run.out / "collection-ledger.json").read_text())
    assert ledger["candidates"][0]["source_path"] == "summary.json"  # collector flat layout
    assert run.detail(INV_A).is_file() and VIEW in run.row(INV_A)


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
    assert tc_line(run.html) == (5, 2, 2, 1)
    assert "counts differ" not in run.html


def test_failures_are_listed_first(tmp_path):
    # A (oel9) fails and B (alma10) passes: the matrix keeps platform order, while the
    # family group lists the failure first.
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, xml=junit(fail_msgs=["boom"]))
    leg_artifact(tmp_path / "dl", "pep-summary-b-a1", INV_B, xml=junit(n_pass=3))
    run = pipeline(tmp_path, [planned(INV_A), planned(INV_B, alias="alma10-arm64", arch="arm64")],
                   ["pep-summary-a-a1", "pep-summary-b-a1"])
    assert run.html.index('id="leg-%s"' % INV_A) < run.html.index('id="leg-%s"' % INV_B)
    assert run.html.index(">alma10-arm64</div>") < run.html.index(">oel9-amd64</div>")


def test_planned_coverage_gaps_are_listed_as_not_tested(tmp_path):
    gap = {"arch": "amd64", "cell_id": "pepcell.v1.deb.bookworm.amd64.pkg", "detail": "bookworm",
           "family": "deb", "os": "bookworm", "physical_package": "pgedge-rag-server2",
           "reason": "no_enabled_platform",
           "target_id": "pepcell.v1.deb.bookworm.amd64.pkg::pgedge-rag-server2"}
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A)
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"], gaps=[gap])
    assert run.result["coverage_status"] == "partial"
    assert "Not tested" in run.html and "NO ENABLED PLATFORM" in run.html and "bookworm" in run.html


def test_cell_scope_gap_shows_dashes_not_none(tmp_path):
    # the shape pep_invocation_plan emits for a planned cell whose build failed (no target/package)
    gap = {"scope": "cell", "cell_id": "pepcell.v1.deb.trixie.arm64.pkg", "target_id": None,
           "family": "deb", "os": "trixie", "arch": "arm64", "physical_package": None,
           "reason": "build_failed", "detail": "failure"}
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A)
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"], gaps=[gap])
    table = run.html[run.html.index("planned but not certified"):]
    assert "BUILD FAILED" in table and "failure" in table and "trixie" in table
    assert "<code>—</code>" in table and ">None<" not in table


# --------------------------------------------------------------------------- #
# Overview matrix, cards and family groups
# --------------------------------------------------------------------------- #
def heat(html):
    return html[html.index('<div class="heat cert"'):html.index('<div class="legend">')]


def group(html, slug):
    m = re.search(r'<details class="component" id="comp-%s"[^>]*>.*?</details>' % re.escape(slug), html, re.S)
    assert m, "no group %s" % slug
    return m.group(0)


def chip(text, word):
    return sum(int(n) for n in re.findall(r'>(?:&#9888; )?(\d+) %s' % word, text))


GAP_T = {"scope": "target", "arch": "amd64", "cell_id": "pepcell.v1.deb.bookworm.amd64.pkg",
         "detail": "bookworm", "family": "deb", "os": "bookworm",
         "physical_package": "pgedge-rag-server2", "reason": "no_enabled_platform",
         "target_id": "pepcell.v1.deb.bookworm.amd64.pkg::pgedge-rag-server2"}
GAP_C = {"scope": "cell", "cell_id": "pepcell.v1.rpm.el-10.arm64.pkg", "target_id": None,
         "family": "rpm", "os": "el-10", "arch": "arm64", "physical_package": None,
         "reason": "build_failed", "detail": "failure"}


def gap_m(path):
    return {"scope": "member", "cell_id": "pepcell.v1.rpm.el-9.amd64.pkg", "target_id": None,
            "family": "rpm", "os": "el-9", "arch": "amd64", "physical_package": "pgedge-rag-server2",
            "reason": "member_rejected",
            "detail": "arch_mismatch; path=%s; native_arch=aarch64; sha256=ffffffffffff" % path}


def test_replay_shape_matrix_cards_and_family_chips_reconcile(tmp_path):
    """The replay's shape: several platforms x PG majors, one platform failing on every PG,
    gaps of every scope. Cards partition the legs, family chips add up to the cards, and
    each gap is one not-tested row spanning every PG column."""
    dl, entries, names = tmp_path / "dl", [], []
    platforms = [("oel9-amd64", "rpm", "amd64"), ("rocky9-arm64", "rpm", "arm64"),
                 ("debian13-amd64", "deb", "amd64")]
    for alias, fam, arch in platforms:
        for pg in ("16", "17"):
            inv = "rag-%s-pg%s-%s" % (alias, pg, "0" * 16)
            fails = ["wget missing"] if alias == "oel9-amd64" else []
            leg_artifact(dl, "pep-summary-" + inv, inv, xml=junit(fail_msgs=fails))
            entries.append(planned(inv, alias=alias, pg=pg, family=fam, arch=arch))
            names.append("pep-summary-" + inv)
    gaps = [GAP_T, GAP_C, gap_m("copy-a.rpm"), gap_m("copy-b.rpm")]
    run = pipeline(tmp_path, entries, names, gaps=gaps)
    assert run.decision["reason_code"] == "product_fail"
    html, grid = run.html, heat(run.html)
    # cards: legs partition exactly; gaps are counted beside the legs, not inside them
    legs = card(html, "Test runs")
    assert legs == 6 == len(run.result["legs"])
    assert (card(html, "Passed"), card(html, "Failed"), card(html, "Incomplete / not run")) == (4, 2, 0)
    assert card(html, "Coverage gaps") == 4
    scopes = "1 build cell · 1 package target · 2 rejected package files"
    assert '<div class="sub">%s</div>' % scopes in html
    assert "coverage <b>partial</b>: 4 coverage gaps with no test run (%s)" % scopes in html
    # family chips (section summaries) add up to the cards
    rpm, deb = group(html, "fam-rpm"), group(html, "fam-deb")
    sums = [s[:s.index("</summary>")] for s in (rpm, deb)]
    assert sum(chip(x, "test run") for x in sums) == legs
    assert sum(chip(x, "passed") for x in sums) == 4 and sum(chip(x, "failed<") for x in sums) == 2
    assert sum(chip(x, "not tested") for x in sums) == 4
    # rpm has a failure -> open; deb has only a gap -> flagged for attention but closed
    assert rpm.startswith('<details class="component" id="comp-fam-rpm" open data-has-attention="1">')
    assert deb.startswith('<details class="component" id="comp-fam-deb" data-has-attention="1">')
    # matrix: one cell per leg, coloured by verdict; the failing platform is a red row
    assert grid.count('class="h cell ok"') == 4 and grid.count('class="h cell bad"') == 2
    assert re.search(r'>oel9-amd64</div><a class="h cell bad"[^>]*>1/4<span>FAIL</span></a>'
                     r'<a class="h cell bad"[^>]*>1/4<span>FAIL</span></a>'
                     r'<div class="h total failtotal">2/8</div>', grid)
    # every gap: its own row, spanning PG16 + PG17 + Total, with its real scope and reason
    spans = re.findall(r'<a class="h cell c-gap" style="grid-column:span (\d+)" href="#gap-(\d+)"', grid)
    assert spans == [("3", "1"), ("3", "2"), ("3", "3"), ("3", "0")]      # rpm group, then deb
    assert "build-cell gap: build failed" in grid
    assert "package-target gap: No enabled PEP test container for this OS/arch" in grid
    assert grid.count("rejected-package-file gap: member rejected") == 2
    assert grid.count("&middot; no test run on any PG</span>") == 4     # never a PG result
    assert "planned target" not in html and "spans every PG" not in html
    legend = html[html.index('<div class="legend">'):]
    assert "with no test run on any PG; it is not a PG result" in legend
    # the machine reason code stays in the JSON and the gap table; readers get the sentence
    assert GAP_T["reason"] in {g["reason"] for g in run.result["coverage_gaps"]}
    table = html[html.index("planned but not certified"):]
    assert ('NO ENABLED PLATFORM</span><div class="rc">No enabled PEP test container for this '
            'OS/arch</div>') in table
    assert "path=copy-a.rpm" in grid and "path=copy-b.rpm" in grid
    assert "c-gap" not in "".join(re.findall(r'<a class="h cell (?:ok|bad)[^"]*"', grid))
    assert_links(run.out, html)


def test_selective_build_leaves_unplanned_pg_empty_never_pass(tmp_path):
    dl = tmp_path / "dl"
    legs = [("rag-a-pg16-1", "alma10-arm64", "16"), ("rag-a-pg17-2", "alma10-arm64", "17"),
            ("rag-o-pg16-3", "oel9-amd64", "16")]                    # oel9 built for PG16 only
    for inv, alias, pg in legs:
        leg_artifact(dl, "pep-summary-" + inv, inv)
    run = pipeline(tmp_path, [planned(i, alias=a, pg=p) for i, a, p in legs],
                   ["pep-summary-" + i for i, _, _ in legs])
    grid = heat(run.html)
    assert grid.count('class="h cell ok"') == 3                          # only real legs are green
    assert '<div class="h cell empty" title="oel9-amd64 PG17 — no test run planned">&mdash;</div>' in grid
    assert "no test run planned" in run.html[run.html.index('<div class="legend">'):]


def test_two_legs_in_one_bucket_each_keep_their_own_link(tmp_path):
    # Two distinct invocations resolve to the same platform, package and PG (for example two
    # build cells on one container). Neither may overwrite the other or be picked at random.
    dl = tmp_path / "dl"
    one, two = "rag-oel9-amd64-pg16-1111111111111111", "rag-oel9-amd64-pg16-2222222222222222"
    leg_artifact(dl, "pep-summary-one", one, xml=junit(fail_msgs=["boom"]))
    leg_artifact(dl, "pep-summary-two", two)
    e2 = dict(planned(two), source_cell_id="pepcell.v1.rpm.y.amd64.pkg",
              source_target_id="pepcell.v1.rpm.y.amd64.pkg::pgedge-rag-server2")
    run = pipeline(tmp_path, [planned(one), e2], ["pep-summary-one", "pep-summary-two"])
    grid = heat(run.html)
    m = re.search(r'<div class="h cell multi bad" title="oel9-amd64 PG16 — 2 test runs">(.*?)</div>', grid)
    assert m, "aggregate bucket not rendered as a multi-leg cell"
    links = re.findall(r'href="([^"]+)"', m.group(1))
    assert links == ["details/cert-detail-%s.html" % one, "details/cert-detail-%s.html" % two]
    assert "11111111: 1/4<span>FAIL</span>" in m.group(1) and "22222222: 0/3" in m.group(1)
    assert '<div class="h total failtotal">1/7</div>' in grid           # both legs counted once
    assert card(run.html, "Test runs") == 2


def test_multiple_packages_get_their_own_rows_and_package_column(tmp_path):
    inv_p, inv_q = INV_A, "rag-oel9-amd64-pg16-ccccccccccccccc3"
    leg_artifact(tmp_path / "dl", "pep-summary-p-a1", inv_p)
    leg_artifact(tmp_path / "dl", "pep-summary-q-a1", inv_q, xml=junit(fail_msgs=["boom"]))
    run = pipeline(tmp_path, [planned(inv_p, pkg="pgedge-rag-server2"),
                              planned(inv_q, pkg="pgedge-rag-extra")],
                   ["pep-summary-p-a1", "pep-summary-q-a1"])
    grid = heat(run.html)
    assert ">oel9-amd64<span>pgedge-rag-extra</span></div>" in grid
    assert ">oel9-amd64<span>pgedge-rag-server2</span></div>" in grid
    assert "multi" not in grid                                            # separate rows, not one bucket
    assert "sortGroup(this,'package')" in run.html
    assert "packages pgedge-rag-extra, pgedge-rag-server2</div>" in run.html


def test_page_reuses_the_regression_chrome_and_its_hooks(tmp_path):
    """The overview reuses the regression report's CSS and script verbatim. Pin the
    hooks the certification markup relies on, so a rename there fails here."""
    import ci_consolidated_report as CI
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A, xml=junit(fail_msgs=["boom"]))
    run = pipeline(tmp_path, [planned(INV_A)], ["pep-summary-a-a1"])
    css, script = CI._render_css(), CI._render_scripts()
    assert css in run.html and script in run.html
    for fn in ("toggleFailures", "openFailing", "expandAll", "collapseAll", "openComponent", "sortGroup"):
        assert "function %s(" % fn in script and "%s(" % fn in run.html.replace(script, "")
    for sel in ('body.failures-only details.component tbody tr[data-fail="0"]',
                "body.failures-only details.component:not([data-has-attention])",
                ".heat .ok", ".heat .bad", ".heat .issue", ".heat .empty", ".heat .issue-marker"):
        assert sel in css, sel
    assert 'id="failuresOnly"' in run.html and 'id="comp-fam-rpm"' in run.html
    assert "openComponent(&quot;fam-rpm&quot;)" in run.html
    assert '<meta name="viewport"' in run.html


def test_zero_legs_with_gaps_shows_only_not_tested_rows(tmp_path):
    run = pipeline(tmp_path, [], [], gaps=[GAP_T, GAP_C])
    assert run.result["legs"] == [] and run.decision["reason_code"] == "zero_eligible"
    grid = heat(run.html)
    assert '<div class="h head">Result</div>' in grid and "PG" not in grid.split("Result", 1)[0]
    assert grid.count('style="grid-column:span 1"') == 2 and "h cell ok" not in grid
    assert card(run.html, "Test runs") == 0 and card(run.html, "Coverage gaps") == 2
    assert vstate(run.html) == "INCOMPLETE" and "no test runs were executed" in run.html
    assert_links(run.out, run.html)


def test_tested_package_and_gap_only_package_are_never_merged(tmp_path):
    """Package A is tested; package B exists only as a coverage gap. B must be named as its
    own, untested package, never presented as a gap of A."""
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A)
    gap_b = dict(GAP_T, physical_package="pgedge-rag-extra",
                 target_id="pepcell.v1.deb.bookworm.amd64.pkg::pgedge-rag-extra")
    run = pipeline(tmp_path, [planned(INV_A, pkg="pgedge-rag-server2")], ["pep-summary-a-a1"],
                   gaps=[gap_b])
    html, grid = run.html, heat(run.html)
    assert "packages pgedge-rag-extra <b>(not tested)</b>, pgedge-rag-server2</div>" in html
    assert ">oel9-amd64<span>pgedge-rag-server2</span></div>" in grid           # tested row: A
    assert ">bookworm · amd64<span>not tested · pgedge-rag-extra</span></div>" in grid  # gap row: B
    deb = group(html, "fam-deb")
    assert "sortGroup(this,'package')" in deb
    assert re.search(r'<tr class="gaprow" id="gaprow-0".*?<td class="mono">pgedge-rag-extra</td>', deb, re.S)
    assert "pgedge-rag-server2" not in deb                                    # A is not in B's group
    assert_links(run.out, html)


_DEC = {"schema": "pep-cert-decision/1", "reason_code": "x"}


@pytest.mark.parametrize("fields, colour, why, gloss", [
    # combinations the gate actually emits: coloured, explained only for observe/report
    (("fail", "success", "observe", "report"), "#dc2626", "because <b>observe</b> mode only reports", " &mdash; never blocks"),
    (("incomplete", "success", "observe", "report"), "#d97706", "because <b>observe</b> mode only reports", " &mdash; never blocks"),
    (("fail", "failure", "gate", "block"), "#dc2626", None, " &mdash; blocks the workflow"),
    (("incomplete", "failure", "observe", "block"), "#d97706", None, " &mdash; blocks the workflow"),
    (("pass", "success", "gate", "allow"), "#10b981", None, " &mdash; allows the workflow"),
    # contradictory or malformed fields: neutral grey, no invented policy explanation
    (("fail", "success", "gate", "block"), "#64748b", "do not agree", ""),
    (("fail", "success", "gate", "report"), "#64748b", "do not agree", ""),
    (("fail", "failure", "observe", "report"), "#64748b", "do not agree", ""),
    (("pass", "failure", "gate", "block"), "#64748b", "do not agree", ""),
    (("pass", "success", "sideways", "allow"), "#64748b", "do not agree", ""),
    (("fail", "success", None, ["report"]), "#64748b", "do not agree", ""),
])
def test_workflow_explanation_only_for_a_genuine_observe_report_decision(fields, colour, why, gloss):
    state, conclusion, mode, policy = fields
    banner = R._banner(dict(_DEC, certification_state=state, workflow_conclusion=conclusion,
                            requested_mode=mode, policy_decision=policy))
    assert re.search(r'--vc:([^"]+)"', banner).group(1) == colour
    assert vstate(banner) == state.upper()                         # the recorded state is kept
    m = re.search(r'<div class="vwhy">(.*?)</div>', banner)
    assert (m.group(1) if m else None) is None if why is None else why in m.group(1)
    pill = re.search(r'class="vwf"[^>]*>(.*?)</span>', banner).group(1)
    assert pill.endswith(gloss) if gloss else "&mdash;" not in pill
    if colour == "#64748b":
        assert "only reports" not in banner and "never blocks" not in banner


@pytest.mark.parametrize("breakage", ["unreadable", "missing", "wrong_shape", "render_error"])
def test_fallback_with_a_valid_pass_decision_never_shows_a_trusted_green_pass(tmp_path, monkeypatch, breakage):
    """The gate's recorded PASS is kept as text (cert-decision.json is still the policy
    record), but the page says first that the human report is incomplete and shows that
    state in neutral grey as unverified, never as a green certification pass."""
    (tmp_path / "cert-decision.json").write_text(json.dumps(
        {"schema": R.DECISION_SCHEMA, "certification_state": "pass", "workflow_conclusion": "success",
         "requested_mode": "gate", "policy_decision": "allow", "reason_code": "clean_pass"}))
    (tmp_path / "collection-ledger.json").write_text(json.dumps({"candidates": []}))
    result = tmp_path / "cert-result.json"
    if breakage == "unreadable":
        result.write_text("{not json")
    elif breakage == "wrong_shape":
        result.write_text("[]")
    elif breakage == "render_error":
        result.write_text(json.dumps({"schema": "cert-result/1", "result_resolved": True, "legs": []}))
        monkeypatch.setattr(R, "render_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _main(tmp_path) == 0
    html = (tmp_path / "consolidated-report.html").read_text()
    body = html[html.index("<body>"):]
    assert body.index("Human report incomplete.") < body.index('<div class="verdict"')
    verdict = re.search(r'<div class="verdict".*?</div></div>', body, re.S).group(0)
    assert 'data-trusted="false"' in verdict and "--vc:#64748b" in verdict and "#10b981" not in verdict
    assert 'class="vlabel">Recorded decision (not verified)<' in verdict and vstate(verdict) == "PASS"
    assert "cannot show or confirm test runs, coverage or gaps" in verdict
    assert "allows the workflow" not in verdict                      # no trusted policy gloss
    assert 'class="heat' not in html and 'class="summary"' not in html


def _hand_leg(inv, cat, family="rpm", alias=None, pg="16"):
    """Hand-built cert-result leg (defensive rendering tests only). reconciliation is not
    'matched', so no evidence lookup or report issue is involved."""
    es, tv, counts, rc, rec = {
        "pass": ("completed", "pass", (4, 0, 0, 0), None, None),
        "fail_no_cases": ("completed", "fail", (4, 0, 0, 0), None, None),
        "missing": ("infra_failure", "not_run", None, "missing_result", "missing"),
        "infra": ("infra_failure", "not_run", None, "runner_lost", None),
        "incomplete": ("incomplete", "fail", (4, 1, 0, 0), "partial_reports", None),
        "notrun": ("completed", "not_run", (3, 0, 0, 3), None, None),
        "preview": ("preview", "not_run", None, None, None),
        "unknown": ("completed", "weird", (1, 0, 0, 0), None, None),
    }[cat]
    return {"invocation_id": inv, "execution_status": es, "test_verdict": tv, "reason_code": rc,
            "reason": None, "reconciliation": rec,
            "counts": None if counts is None else dict(zip(("tests", "failures", "errors", "skipped"), counts)),
            "planned_invocation": {"container_alias": alias or "p-" + inv, "pg_major": pg,
                                   "family": family, "arch": "amd64", "component": "rag",
                                   "package": {"name": "pgedge-rag-server2"}}}


def test_every_leg_category_has_one_bucket_and_only_pass_is_green(tmp_path):
    """Defensive (hand-built result): colour and label follow the authoritative
    execution/verdict fields, never the case counts, and the card partition never double
    counts the overlapping missing/infra/not_run fields of cert-result counts."""
    cats = ["pass", "fail_no_cases", "missing", "infra", "incomplete", "notrun", "preview", "unknown"]
    legs = [_hand_leg("inv-%s" % c, c) for c in cats] + [_hand_leg("inv-apk", "pass", family="apk")]
    result = {"schema": "cert-result/1", "result_resolved": True, "legs": legs, "coverage_gaps": [],
              "coverage_status": "complete",
              # the reducer's own counts overlap (a missing leg is also infra_failure and not_run)
              "counts": {"infra_failure": 2, "missing": 1, "not_run": 4}}
    R.render_report(result, None, {}, tmp_path)
    html = (tmp_path / "consolidated-report.html").read_text()
    grid = heat(html)
    expect = {"pass": ("ok", "0/4"), "fail_no_cases": ("bad", "0/4<span>FAIL</span>"),
              "missing": ("issue", "MISSING"), "infra": ("issue", "INFRA"),
              "incomplete": ("issue", "INCOMPLETE<span>1/4</span>"),
              "notrun": ("c-notrun", "0/3<span>NOT RUN</span>"), "preview": ("c-preview", "PREVIEW"),
              "unknown": ("c-unknown", "WEIRD<span>0/1</span>")}
    for c, (cls, text) in expect.items():
        assert re.search(r'<a class="h cell %s" href="#leg-inv-%s" [^>]*>%s</a>'
                         % (cls, c, re.escape(text)), grid), c
    assert grid.count("h cell ok") == 2                           # the two real passes only
    n = card(html, "Test runs")
    assert n == 9 == (card(html, "Passed") + card(html, "Failed")
                      + card(html, "Incomplete / not run") + card(html, "Preview"))
    assert (card(html, "Passed"), card(html, "Failed"), card(html, "Incomplete / not run")) == (2, 1, 5)
    assert "1 missing · 1 infra failure · 1 incomplete · 1 not run · 1 unknown" in html
    # an unknown family is its own group, not dropped or folded into rpm/deb
    assert 'id="comp-fam-apk"' in html and ">APK <span" in grid
    assert vstate(html) == "UNKNOWN"                               # no decision -> never PASS
    assert_links(tmp_path, html)


def test_hostile_identity_values_are_escaped_everywhere(tmp_path):
    """Defensive (hand-built result): platform, family, PG and gap text reach attributes,
    onclick handlers and grid cells, so each must be escaped, never interpreted."""
    evil = '"><img src=x onerror=alert(1)>'
    leg = _hand_leg("inv-x", "fail_no_cases", family=evil, alias=evil, pg=evil)
    gap = dict(GAP_T, family=evil, os=evil, reason=evil, detail=evil, scope=evil)
    R.render_report({"schema": "cert-result/1", "result_resolved": True, "legs": [leg],
                     "coverage_gaps": [gap]}, None, {}, tmp_path)
    html = (tmp_path / "consolidated-report.html").read_text()
    assert "<img" not in html and "onerror=alert(1)>" not in html
    assert html.count("&lt;img src=x onerror=alert(1)&gt;") >= 6
    assert re.search(r'onclick="openComponent\(&quot;fam-[a-z0-9-]+&quot;\)"', html)


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
    assert vstate(banner) == "UNKNOWN" and "#10b981" not in banner
    ok = R._banner({"schema": R.DECISION_SCHEMA, "certification_state": "pass",
                    "workflow_conclusion": "success", "requested_mode": "gate",
                    "policy_decision": "allow", "reason_code": "clean_pass"})
    assert vstate(ok) == "PASS" and "#10b981" in ok
    observe = R._banner({"schema": R.DECISION_SCHEMA, "certification_state": "fail",
                         "workflow_conclusion": "success", "requested_mode": "observe",
                         "policy_decision": "report", "reason_code": "product_fail"})
    assert vstate(observe) == "FAIL" and "#10b981" not in observe
    assert "Workflow <b>success</b> &middot; observe mode" in observe
    assert "the product certification state is <b>FAIL</b>" in observe


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
    assert "must be a JSON object" in html and vstate(html) == "FAIL"


def test_main_missing_inputs_write_fallback_without_false_pass(tmp_path):
    assert _main(tmp_path, result=tmp_path / "nope.json", decision=tmp_path / "nope2.json") == 0
    html = (tmp_path / "consolidated-report.html").read_text()
    assert vstate(html) == "UNKNOWN" and 'class="vstate">PASS<' not in html


def _visible(html):
    """Text a reader can see: page text plus tooltips, without styles, scripts or ids/hrefs."""
    body = re.sub(r"<(style|script)\b.*?</\1>", " ", html, flags=re.S)
    titles = re.findall(r'\btitle="([^"]*)"', body)
    return re.sub(r"<[^>]+>", " ", body) + " " + " ".join(titles)


def test_reader_facing_wording_says_test_runs_not_legs(tmp_path):
    """One package/platform/PG suite execution is a "test run"; its individual cases are
    "test cases"; the GitHub run is the "workflow run". Covers the banners, issue table, gap
    rows and a fallback page, where the older wording used to appear."""
    leg_artifact(tmp_path / "dl", "pep-summary-a-a1", INV_A,
                 xml=junit(n_pass=2, n_skip=1, suite_tests=4))              # a report issue
    run = pipeline(tmp_path, [planned(INV_A), planned(INV_B, alias="alma10-arm64", arch="arm64")],
                   ["pep-summary-a-a1"], gaps=[GAP_T])                      # INV_B has no result
    text = _visible(run.html)
    assert not re.search(r"\blegs?\b", text, re.I), re.findall(r".{40}\blegs?\b.{40}", text, re.I)
    assert card(run.html, "Test runs") == 2 and "1 planned test run produced no result" in text
    assert "workflow run 100" in text and "Test cases" in text
    assert ">Test run</th>" in run.html                                     # report-issues table header
    (tmp_path / "bad.json").write_text("{not json")
    fb_out = tmp_path / "fb"
    R.main(["--result", str(tmp_path / "bad.json"), "--decision", str(run.out / "cert-decision.json"),
            "--ledger", str(run.out / "collection-ledger.json"), "--legs", str(tmp_path / "dl"),
            "--out", str(fb_out)])
    assert not re.search(r"\blegs?\b", _visible((fb_out / "consolidated-report.html").read_text()), re.I)


def test_package_proof_is_explained_in_source_neutral_words(tmp_path):
    """A digest mismatch reads as unproven bytes (never a product pass or fail) and says
    so in words that hold for a build receipt and a published-package replay alike; a
    matching test run's tooltip says its bytes match."""
    dl = tmp_path / "dl"
    leg_artifact(dl, "pep-summary-a-a1", INV_A)                              # planned digest
    leg_artifact(dl, "pep-summary-b-a1", INV_B, digest="0" * 64)             # other bytes
    run = pipeline(tmp_path, [planned(INV_A), planned(INV_B, alias="alma10-arm64", arch="arm64")],
                   ["pep-summary-a-a1", "pep-summary-b-a1"])
    assert run.decision["reason_code"] == "package_digest_mismatch"
    assert vstate(run.html) == "INCOMPLETE"
    bad = run.row(INV_B)
    assert "INCOMPLETE" in bad and "package_digest_mismatch" in bad
    assert "SHA-256 differs from the captured package" in bad
    assert _nums(bad) == [3, 2, 0, 1]                                        # counts still shown
    assert "installed package SHA-256 matches the captured package" in run.html
    text = _visible(run.html)
    assert not re.search(r"\b(receipt|replay|release build)\b", text, re.I)
    assert not re.search(r"\blegs?\b", text, re.I)
