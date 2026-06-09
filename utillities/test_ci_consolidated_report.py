import importlib.util
from pathlib import Path

# Import the module under test by path (it lives next to this test file).
_spec = importlib.util.spec_from_file_location(
    "ci_consolidated_report",
    str(Path(__file__).parent / "ci_consolidated_report.py"),
)
ccr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccr)


def _write_summary(slice_dir: Path, **fields):
    slice_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"[workflow-summary] {k}={v}" for k, v in fields.items()]
    (slice_dir / "workflow-summary.txt").write_text("\n".join(lines) + "\n")


def test_parse_slice_metadata_from_summary(tmp_path):
    sd = tmp_path / "test-logs-r31-a1-pg17-deb-arm64"
    _write_summary(
        sd, pg="17", family="deb", arch="arm64",
        **{"runner.label": "ubuntu-24.04-arm", "github.run_id": "26158296323",
           "github.run_attempt": "1", "github.run_number": "31",
           "github.event_name": "workflow_dispatch", "github.actor": "hayee-bhatti"},
    )
    meta = ccr.parse_slice_metadata(sd)
    assert meta["pg"] == "17"
    assert meta["family"] == "deb"
    assert meta["arch"] == "arm64"
    assert meta["runner_label"] == "ubuntu-24.04-arm"
    assert meta["run_id"] == "26158296323"
    assert meta["run_attempt"] == "1"
    assert meta["metadata_source"] == "workflow-summary.txt"


def test_parse_slice_metadata_fallback_to_dirname(tmp_path):
    # No workflow-summary.txt present -> derive from the directory name.
    sd = tmp_path / "test-logs-r31-a1-pg16-rpm-amd64"
    sd.mkdir(parents=True)
    meta = ccr.parse_slice_metadata(sd)
    assert meta["pg"] == "16"
    assert meta["family"] == "rpm"
    assert meta["arch"] == "amd64"
    assert meta["metadata_source"] == "directory-name"


def _touch(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("<testsuites/>")


def test_discover_excludes_consolidated(tmp_path):
    sd = tmp_path / "test-logs-r31-a1-pg17-deb-arm64"
    # Canonical per-component report.
    _touch(sd / "server" / "17" / "report-deb-server-17.xml")
    _touch(sd / "pgbouncer" / "17" / "report-deb-pgbouncer-17.xml")
    # Duplicate copy under consolidated-* must be excluded.
    _touch(sd / "consolidated-20260520_110213" / "report-deb-server-17.xml")
    _touch(sd / "consolidated-20260520_110213" / "report-deb-pgbouncer-17.xml")

    found = ccr.discover_report_xmls(sd)
    names = sorted(p.relative_to(sd).as_posix() for p in found)
    assert names == [
        "pgbouncer/17/report-deb-pgbouncer-17.xml",
        "server/17/report-deb-server-17.xml",
    ]


def test_derive_component_from_path(tmp_path):
    sd = tmp_path / "test-logs-r31-a1-pg17-deb-arm64"
    xml = sd / "server" / "17" / "report-deb-server-17.xml"
    assert ccr.derive_component_from_path(xml, sd) == "server"
    xml2 = sd / "pg_stat_monitor" / "17" / "report-deb-pg_stat_monitor-17.xml"
    assert ccr.derive_component_from_path(xml2, sd) == "pg_stat_monitor"


_JUNIT_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="4" time="12.5">
  <testcase classname="component-test.test_pep_server" name="test_install[auto-debian12-arm-deb]" time="3.0"/>
  <testcase classname="component-test.test_pep_server" name="test_version[auto-debian12-arm-deb]" time="2.0"><failure message="boom"/></testcase>
  <testcase classname="component-test.test_pep_server" name="test_extn[bloom-auto-debian12-arm-deb]" time="1.0"/>
  <testcase classname="component-test.test_pep_server" name="test_orphan_no_brackets" time="0.5"><skipped message="why"/></testcase>
</testsuite></testsuites>
"""


def test_parse_junit_groups_by_container(tmp_path):
    xml = tmp_path / "report-deb-server-17.xml"
    xml.write_text(_JUNIT_SAMPLE)
    groups = ccr.parse_junit_xml(xml)
    # The 3 bracketed cases collapse to one container; extension prefix stripped.
    assert "auto-debian12-arm" in groups
    g = groups["auto-debian12-arm"]
    assert g["tests"] == 3
    assert g["passed"] == 2
    assert g["failed"] == 1
    assert g["skipped"] == 0


def test_parse_junit_unattributed_bucket(tmp_path):
    xml = tmp_path / "report-deb-server-17.xml"
    xml.write_text(_JUNIT_SAMPLE)
    groups = ccr.parse_junit_xml(xml)
    # The test with no [container] bracket must NOT be dropped.
    assert ccr.NOT_CONTAINER_SCOPED_LABEL in groups
    assert groups[ccr.NOT_CONTAINER_SCOPED_LABEL]["tests"] == 1
    assert groups[ccr.NOT_CONTAINER_SCOPED_LABEL]["skipped"] == 1


_JUNIT_MULTISUITE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="suite-a" tests="1" failures="0" skipped="0">
    <testcase classname="c" name="test_a[auto-rocky9-arm-rhel]" time="1.0"/>
  </testsuite>
  <testsuite name="suite-b" tests="1" failures="1" skipped="0">
    <testcase classname="c" name="test_b[auto-rocky9-arm-rhel]" time="1.0"><failure message="x"/></testcase>
  </testsuite>
</testsuites>
"""


def test_parse_junit_iterates_all_suites(tmp_path):
    # Must count testcases across EVERY testsuite, not just the first.
    xml = tmp_path / "report-rpm-server-17.xml"
    xml.write_text(_JUNIT_MULTISUITE)
    groups = ccr.parse_junit_xml(xml)
    g = groups["auto-rocky9-arm"]
    assert g["tests"] == 2
    assert g["passed"] == 1
    assert g["failed"] == 1


def test_parse_junit_non_container_bracket_is_not_container_scoped(tmp_path):
    # A bracketed param that is not container-like (e.g. a port) must NOT be
    # mistaken for a container; it goes to the not-container-scoped bucket.
    xml = tmp_path / "report-deb-server-17.xml"
    xml.write_text(
        '<?xml version="1.0"?><testsuite name="pytest" tests="1" failures="0" skipped="0">'
        '<testcase classname="c" name="test_port[5432]" time="0.1"/></testsuite>'
    )
    groups = ccr.parse_junit_xml(xml)
    assert "5432" not in groups
    assert groups[ccr.NOT_CONTAINER_SCOPED_LABEL]["tests"] == 1


def test_build_rows_includes_empty_slice(tmp_path):
    agg = tmp_path / "aggregated"
    # Slice A: one component with results.
    a = agg / "test-logs-r31-a1-pg17-deb-arm64"
    _write_summary(a, pg="17", family="deb", arch="arm64",
                   **{"runner.label": "ubuntu-24.04-arm"})
    (a / "server" / "17").mkdir(parents=True)
    (a / "server" / "17" / "report-deb-server-17.xml").write_text(_JUNIT_SAMPLE)
    # Slice B: summary present but NO report XMLs (early-failure slice).
    b = agg / "test-logs-r31-a1-pg17-rpm-amd64"
    _write_summary(b, pg="17", family="rpm", arch="amd64",
                   **{"runner.label": "ubuntu-24.04"})

    rows = ccr.build_rows(agg)
    # Slice A produced container rows; slice B produced one "no reports" row.
    a_rows = [r for r in rows if r["family"] == "deb"]
    b_rows = [r for r in rows if r["family"] == "rpm"]
    assert len(a_rows) >= 1
    assert len(b_rows) == 1
    assert b_rows[0]["status"] == "NO REPORTS"
    assert b_rows[0]["component"] == "-"
    assert b_rows[0]["container"] == "-"


def test_build_rows_report_with_zero_testcases(tmp_path):
    # A report XML that parses but contains no <testcase> elements must surface
    # a visible NO TESTCASES row, not be silently omitted.
    agg = tmp_path / "aggregated"
    sd = agg / "test-logs-r31-a1-pg17-deb-amd64"
    _write_summary(sd, pg="17", family="deb", arch="amd64",
                   **{"runner.label": "ubuntu-24.04"})
    empty_dir = sd / "server" / "17"
    empty_dir.mkdir(parents=True)
    (empty_dir / "report-deb-server-17.xml").write_text(
        '<?xml version="1.0"?><testsuites><testsuite name="pytest" '
        'tests="0" failures="0" skipped="0"></testsuite></testsuites>'
    )
    rows = ccr.build_rows(agg)
    assert len(rows) == 1
    assert rows[0]["status"] == "NO TESTCASES"
    assert rows[0]["component"] == "server"
    assert rows[0]["container"] == "-"
    assert rows[0]["tests"] == 0


def test_render_html_escapes_values(tmp_path):
    rows = [{
        "pg": "17", "family": "deb", "arch": "arm64",
        "runner_label": "ubuntu-24.04-arm",
        "component": "<script>", "container": "auto&co",
        "tests": 1, "passed": 1, "failed": 0, "skipped": 0, "time": 1.0,
        "status": "PASSED", "status_class": "passed", "report_href": "",
    }]
    ctx = {"run_number": "31", "run_attempt": "1", "run_id": "x",
           "event_name": "workflow_dispatch", "actor": "hayee-bhatti",
           "slice_count": 1}
    out = ccr.render_html(rows, ctx)
    # Only the legitimate toggle JS <script> should exist — an injected raw
    # component value would push the count to 2+.
    assert out.count("<script") == 1
    assert "&lt;script&gt;" in out          # user value was escaped
    assert "auto&amp;co" in out             # ampersand escaped
    assert "togglefailures" in out.lower()  # failures/attention toggle present
    assert "workflow" in out.lower() and "test" in out.lower()  # banner present


def test_render_html_totals(tmp_path):
    rows = [
        {"pg": "17", "family": "deb", "arch": "arm64", "runner_label": "r",
         "component": "server", "container": "c1", "tests": 10, "passed": 8,
         "failed": 2, "skipped": 0, "time": 5.0, "status": "FAILED",
         "status_class": "failed", "report_href": ""},
    ]
    ctx = {"run_number": "31", "run_attempt": "1", "run_id": "x",
           "event_name": "e", "actor": "a", "slice_count": 1}
    out = ccr.render_html(rows, ctx)
    assert ">10<" in out  # total tests rendered somewhere


def test_render_html_flags_report_issues(tmp_path):
    # A NO REPORTS row must (a) surface a "Report Issues" header count even
    # with zero test failures, and (b) be treated as an attention row so the
    # failures-only toggle keeps it visible.
    rows = [{
        "pg": "17", "family": "rpm", "arch": "amd64", "runner_label": "r",
        "component": "-", "container": "-", "tests": 0, "passed": 0,
        "failed": 0, "skipped": 0, "time": 0.0, "status": "NO REPORTS",
        "status_class": "noreports", "report_href": "",
    }]
    ctx = {"run_number": "31", "run_attempt": "1", "run_id": "x",
           "event_name": "e", "actor": "a", "slice_count": 1}
    out = ccr.render_html(rows, ctx)
    assert "Report Issues" in out
    assert 'data-fail="1"' in out  # NO REPORTS row participates in failures-only


# ---- Item 1: NOTSET / empty-parameter-set handling -------------------------

_JUNIT_NOTSET = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" errors="0" failures="0" skipped="3" tests="3">
  <testcase classname="component-test.test_pep_server" name="test_install_prerequisites[NOTSET]" time="0.0"><skipped message="got empty parameter set"/></testcase>
  <testcase classname="component-test.test_pep_server" name="test_check_connection[NOTSET]" time="0.0"><skipped message="got empty parameter set"/></testcase>
  <testcase classname="component-test.test_pep_server" name="test_create_extensions[bloom-NOTSET]" time="0.0"><skipped message="got empty parameter set"/></testcase>
</testsuite>
"""


def test_parse_junit_notset_bucket(tmp_path):
    xml = tmp_path / "report-rpm-server-17.xml"
    xml.write_text(_JUNIT_NOTSET)
    groups = ccr.parse_junit_xml(xml)
    # NOTSET placeholder skips must land in their own 'none in scope' bucket,
    # NOT in not-container-scoped. Both the bare '[NOTSET]' and the doubly-
    # parametrized '[bloom-NOTSET]' forms must be captured.
    assert ccr.NO_CONTAINERS_LABEL in groups
    assert ccr.NOT_CONTAINER_SCOPED_LABEL not in groups
    assert groups[ccr.NO_CONTAINERS_LABEL]["tests"] == 3


def test_build_rows_notset_becomes_no_containers_row(tmp_path):
    agg = tmp_path / "aggregated"
    sd = agg / "test-logs-r33-a1-pg17-rpm-amd64"
    _write_summary(sd, pg="17", family="rpm", arch="amd64",
                   **{"runner.label": "ubuntu-24.04"})
    (sd / "server" / "17").mkdir(parents=True)
    (sd / "server" / "17" / "report-rpm-server-17.xml").write_text(_JUNIT_NOTSET)
    rows = ccr.build_rows(agg)
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "NO CONTAINERS SELECTED"
    # Excluded from real totals: count columns are zeroed.
    assert (r["tests"], r["passed"], r["failed"], r["skipped"]) == (0, 0, 0, 0)


def test_not_container_scoped_real_tests_count(tmp_path):
    # A genuine non-container test (no bracket) actually ran -> counts normally.
    xml = tmp_path / "report-deb-server-17.xml"
    xml.write_text(
        '<?xml version="1.0"?><testsuite name="pytest" tests="1" failures="0" skipped="0">'
        '<testcase classname="c" name="test_module_level_thing" time="0.5"/></testsuite>'
    )
    groups = ccr.parse_junit_xml(xml)
    assert ccr.NOT_CONTAINER_SCOPED_LABEL in groups
    assert groups[ccr.NOT_CONTAINER_SCOPED_LABEL]["tests"] == 1
    assert groups[ccr.NOT_CONTAINER_SCOPED_LABEL]["passed"] == 1


def test_render_notset_excluded_from_totals_visible_and_counted_as_issue(tmp_path):
    rows = [
        {"pg": "17", "family": "deb", "arch": "arm64", "runner_label": "r",
         "component": "server", "container": "auto-debian12-arm", "tests": 50,
         "passed": 48, "failed": 0, "skipped": 2, "time": 10.0,
         "status": "PASSED", "status_class": "passed", "report_href": ""},
        {"pg": "17", "family": "rpm", "arch": "amd64", "runner_label": "r",
         "component": "server", "container": ccr.NO_CONTAINERS_LABEL, "tests": 0,
         "passed": 0, "failed": 0, "skipped": 0, "time": 0.0,
         "status": "NO CONTAINERS SELECTED", "status_class": "noreports",
         "report_href": ""},
    ]
    ctx = {"run_number": "33", "run_attempt": "1", "run_id": "x",
           "event_name": "workflow_dispatch", "actor": "a", "slice_count": 2}
    out = ccr.render_html(rows, ctx)
    # Totals reflect only the real 50 tests, not inflated by the NOTSET row.
    assert ">50<" in out and ">48<" in out
    # The NO CONTAINERS row is an attention row (kept by failures-only) ...
    assert out.count('data-fail="1"') >= 1
    # ... and is counted under Report Issues (card value >= 1).
    assert "Report Issues" in out


# ---- Item 2: title + terminology -------------------------------------------

def test_render_title_and_terminology(tmp_path):
    rows = [{"pg": "17", "family": "deb", "arch": "arm64", "runner_label": "r",
             "component": "server", "container": "c1", "tests": 1, "passed": 1,
             "failed": 0, "skipped": 0, "time": 1.0, "status": "PASSED",
             "status_class": "passed", "report_href": ""}]
    ctx = {"run_number": "33", "run_attempt": "1", "run_id": "x",
           "event_name": "e", "actor": "a", "slice_count": 1}
    out = ccr.render_html(rows, ctx)
    assert "PEP Regression Consolidated Report" in out
    assert "matrix target" in out.lower()
    assert "slice" not in out.lower()   # user-facing output avoids the word "slice"


# ---- Item 3: effective selection in header ---------------------------------

def test_render_effective_selection_header(tmp_path):
    rows = [{"pg": "17", "family": "deb", "arch": "arm64", "runner_label": "r",
             "component": "server", "container": "c1", "tests": 1, "passed": 1,
             "failed": 0, "skipped": 0, "time": 1.0, "status": "PASSED",
             "status_class": "passed", "report_href": ""}]
    ctx = {"run_number": "33", "run_attempt": "1", "run_id": "x",
           "event_name": "workflow_dispatch", "actor": "a", "slice_count": 4,
           "pg_versions": "16, 17, 18", "families": "deb, rpm",
           "arches": "amd64, arm64", "components": "server",
           "repo": "release", "execution_mode": "full",
           "ref": "refs/heads/v2.1-consolidated-report", "sha": "abc1234"}
    out = ccr.render_html(rows, ctx)
    for token in ["16, 17, 18", "deb, rpm", "amd64, arm64", "server",
                  "release", "full", "abc1234"]:
        assert token in out


def test_run_context_aggregates_effective_selection(tmp_path):
    agg = tmp_path / "aggregated"
    a = agg / "test-logs-r33-a1-pg17-deb-arm64"
    _write_summary(a, pg="17", family="deb", arch="arm64",
                   repo="release", components="server", execution_mode="full",
                   **{"runner.label": "ubuntu-24.04-arm",
                      "github.run_id": "26877425625", "github.run_attempt": "1",
                      "github.run_number": "33", "github.actor": "hayee-bhatti",
                      "github.sha": "abc1234def", "github.ref": "refs/heads/x"})
    b = agg / "test-logs-r33-a1-pg16-rpm-amd64"
    _write_summary(b, pg="16", family="rpm", arch="amd64",
                   repo="release", components="server", execution_mode="full",
                   **{"runner.label": "ubuntu-24.04",
                      "github.run_id": "26877425625", "github.run_attempt": "1",
                      "github.run_number": "33", "github.actor": "hayee-bhatti",
                      "github.sha": "abc1234def", "github.ref": "refs/heads/x"})
    ctx = ccr._run_context(agg, [])
    assert ctx["pg_versions"] == "16, 17"
    assert ctx["families"] == "deb, rpm"
    assert ctx["arches"] == "amd64, arm64"
    assert ctx["components"] == "server"
    assert ctx["repo"] == "release"
    assert ctx["execution_mode"] == "full"
    assert ctx["sha"] == "abc1234def"
