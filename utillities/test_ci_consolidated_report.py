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
    assert "unattributed" in groups
    assert groups["unattributed"]["tests"] == 1
    assert groups["unattributed"]["skipped"] == 1


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


def test_parse_junit_non_container_bracket_is_unattributed(tmp_path):
    # A bracketed param that is not container-like (e.g. a port) must NOT be
    # mistaken for a container; it goes to 'unattributed'.
    xml = tmp_path / "report-deb-server-17.xml"
    xml.write_text(
        '<?xml version="1.0"?><testsuite name="pytest" tests="1" failures="0" skipped="0">'
        '<testcase classname="c" name="test_port[5432]" time="0.1"/></testsuite>'
    )
    groups = ccr.parse_junit_xml(xml)
    assert "5432" not in groups
    assert groups["unattributed"]["tests"] == 1


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
