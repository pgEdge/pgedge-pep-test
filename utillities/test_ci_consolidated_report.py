import importlib.util
import re
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


def test_parse_junit_xml_is_reduction_of_record_parser(tmp_path):
    # Guards the shared-model invariant: counts and per-testcase detail must
    # both derive from one in-memory pass over the XML. If parse_junit_xml is
    # ever re-implemented as an independent walk, this test breaks.
    xml = tmp_path / "report.xml"
    xml.write_text(
        '<?xml version="1.0"?>'
        '<testsuite tests="4" failures="1" skipped="1">'
        '  <testcase name="test_a[auto-alma9-arm-rhel]" time="0.5"/>'
        '  <testcase name="test_b[auto-alma9-arm-rhel]" time="0.7">'
        '    <failure message="boom">tb here</failure>'
        '  </testcase>'
        '  <testcase name="test_c[auto-oel10-arm-rhel]" time="0.2"/>'
        '  <testcase name="test_d[auto-oel10-arm-rhel]" time="0.1">'
        '    <skipped message="why"/>'
        '  </testcase>'
        '</testsuite>'
    )
    counts = ccr.parse_junit_xml(xml)
    records = ccr._parse_junit_testcases(xml)
    reduced = ccr._summarise_by_container(records)
    assert counts == reduced


def test_detail_filename_includes_all_five_dimensions():
    f = ccr._detail_filename(
        component="postgis", pg="16", family="deb", arch="arm64",
        container="auto-debian12-arm",
    )
    assert f == "detail-postgis-pg16-deb-arm64-auto-debian12-arm.html"


def test_detail_filename_slugs_unsafe_characters():
    # Component name with whitespace and a slash; the slug normaliser must
    # lowercase and reduce unsafe chars to '-'.
    f = ccr._detail_filename(
        component="My Comp/v2", pg="17", family="rpm", arch="amd64",
        container="auto-rocky9-amd",
    )
    assert f == "detail-my-comp-v2-pg17-rpm-amd64-auto-rocky9-amd.html"


def test_assign_unique_detail_filenames_resolves_slug_collisions():
    # Distinct row keys whose components nonetheless slug to the same base
    # filename. In practice this only happens when distinct component names
    # normalise identically (e.g. "Foo Bar"/"foo-bar"/"foo bar" all -> "foo-bar").
    # Real row keys are unique by construction — build_rows asserts that
    # invariant (Task 4) — so true row-tuple duplicates are not the concern
    # here; slug aliasing is.
    keys = [
        ("Foo Bar", "16", "deb", "arm64", "auto-debian12-arm"),
        ("foo-bar", "16", "deb", "arm64", "auto-debian12-arm"),
        ("foo bar", "16", "deb", "arm64", "auto-debian12-arm"),
        ("postgis", "16", "deb", "arm64", "auto-debian13-arm"),
    ]
    names = ccr._assign_unique_detail_filenames(keys)
    # Parallel list, NOT a dict — duplicate-shaped keys are preserved in
    # row order rather than collapsed by dict-key semantics.
    assert isinstance(names, list)
    assert len(names) == 4
    assert len(set(names)) == 4
    assert names[0].endswith("-foo-bar-pg16-deb-arm64-auto-debian12-arm.html")
    assert names[1].endswith("-foo-bar-pg16-deb-arm64-auto-debian12-arm-2.html")
    assert names[2].endswith("-foo-bar-pg16-deb-arm64-auto-debian12-arm-3.html")
    assert names[3].endswith("-postgis-pg16-deb-arm64-auto-debian13-arm.html")


def test_render_container_detail_page_defensively_filters_records():
    # Pass a MIXED-container record list. The renderer must scope itself
    # to the requested container without relying on the caller's filter.
    recs = [
        ccr.TestcaseRecord("auto-debian12-arm", "test_a[auto-debian12-arm-deb]",
                           0.5, "passed", "", "", ""),
        ccr.TestcaseRecord("auto-debian13-arm", "test_a[auto-debian13-arm-deb]",
                           0.6, "failed", "failure", "boom", "tb body"),
    ]
    page = ccr.render_container_detail_page(
        component="postgis", pg="16", family="deb", arch="arm64",
        container="auto-debian12-arm",
        records=recs,
        back_link_href="../test-logs-x/postgis/16/report.html",
        consolidated_filename="consolidated-report.html",
    )
    assert "auto-debian12-arm" in page
    # The unrequested container's testcase params must not appear anywhere.
    assert "test_a[auto-debian13-arm-deb]" not in page
    assert "auto-debian13-arm" not in page
    assert "Full pytest-html report" in page
    assert 'href="../test-logs-x/postgis/16/report.html"' in page


def test_render_container_detail_page_footer_uses_passed_filename():
    page = ccr.render_container_detail_page(
        component="c", pg="16", family="deb", arch="arm64",
        container="auto-x", records=[],
        back_link_href="../x.html",
        consolidated_filename="consolidated-report-v24.html",
    )
    # Footer link must reflect the actual output filename, not a hardcode.
    assert 'href="../consolidated-report-v24.html"' in page
    assert 'href="../consolidated-report.html"' not in page


def test_render_container_detail_page_escapes_and_collapses_traceback():
    recs = [
        ccr.TestcaseRecord("auto-alma9-arm", "test_<script>[auto-alma9-arm-rhel]",
                           1.0, "failed", "failure",
                           "msg with </details>", "tb with <script>alert(1)</script>"),
    ]
    page = ccr.render_container_detail_page(
        component="server", pg="17", family="rpm", arch="arm64",
        container="auto-alma9-arm",
        records=recs,
        back_link_href="../x/y.html",
        consolidated_filename="consolidated-report.html",
    )
    # raw tags not present as active markup
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;/details&gt;" in page
    # traceback wrapped in collapsed <details>
    assert "<details" in page
    assert "Show traceback" in page or "show traceback" in page.lower()


def test_render_container_detail_page_orders_failed_then_skipped_then_passed():
    recs = [
        ccr.TestcaseRecord("c", "test_p1[c-deb]", 0.1, "passed", "", "", ""),
        ccr.TestcaseRecord("c", "test_f1[c-deb]", 0.2, "failed", "failure", "m", "b"),
        ccr.TestcaseRecord("c", "test_s1[c-deb]", 0.3, "skipped", "skipped", "why", ""),
    ]
    page = ccr.render_container_detail_page(
        component="comp", pg="16", family="deb", arch="amd64",
        container="c", records=recs, back_link_href="../x.html",
        consolidated_filename="consolidated-report.html",
    )
    i_fail = page.find("test_f1")
    i_skip = page.find("test_s1")
    i_pass = page.find("test_p1")
    assert -1 < i_fail < i_skip < i_pass


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


def _mkrow_for_assert(pg, family, arch, component, container, status,
                      tests=0, passed=0, failed=0, skipped=0):
    # Inline copy so this block of tests doesn't depend on test ordering /
    # _mkrow being defined later in the file.
    return {
        "pg": pg, "family": family, "arch": arch, "runner_label": "r",
        "component": component, "container": container,
        "tests": tests, "passed": passed, "failed": failed, "skipped": skipped,
        "time": 1.0, "status": status,
        "status_class": status.lower().replace(" ", ""),
        "report_href": "",
    }


def test_assert_unique_real_row_keys_raises_on_true_duplicate():
    # Two synthetic real-container rows with identical
    # (pg, family, arch, component, container) tuples -- must raise loudly.
    rows = [
        _mkrow_for_assert("16", "deb", "arm64", "postgis", "auto-debian12-arm",
                          "PASSED", tests=15, passed=15),
        _mkrow_for_assert("16", "deb", "arm64", "postgis", "auto-debian12-arm",
                          "PASSED", tests=15, passed=15),
    ]
    import pytest
    with pytest.raises(AssertionError) as excinfo:
        ccr._assert_unique_real_row_keys(rows)
    msg = str(excinfo.value)
    assert "duplicate" in msg.lower()
    # The error must name the offending key so the failure is debuggable.
    assert "postgis" in msg and "auto-debian12-arm" in msg


def test_assert_unique_real_row_keys_allows_slug_collisions_with_distinct_raw_keys():
    # Two rows whose raw component NAMES would slug-collide ("Foo Bar" and
    # "foo-bar" both normalise to "foo-bar"), but whose raw row-key tuples
    # differ. Assertion runs on raw tuples, so this must NOT raise -- slug
    # collisions are the filename helper's job, not the invariant's.
    rows = [
        _mkrow_for_assert("16", "deb", "arm64", "Foo Bar", "auto-debian12-arm",
                          "PASSED", tests=1, passed=1),
        _mkrow_for_assert("16", "deb", "arm64", "foo-bar", "auto-debian12-arm",
                          "PASSED", tests=1, passed=1),
    ]
    ccr._assert_unique_real_row_keys(rows)   # no exception


def test_assert_unique_real_row_keys_ignores_skip_set_rows():
    # NO REPORTS rows from the same slice (component "-", container "-").
    # They never claim a filename, so duplicate-shaped ones must not trip
    # the invariant. Same for non-container buckets and zero-tests rows.
    rows = [
        _mkrow_for_assert("16", "deb", "arm64", "-", "-", "NO REPORTS"),
        _mkrow_for_assert("16", "deb", "arm64", "-", "-", "NO REPORTS"),
        _mkrow_for_assert("16", "deb", "arm64", "c", ccr.NO_CONTAINERS_LABEL,
                          "NO CONTAINERS SELECTED"),
        _mkrow_for_assert("16", "deb", "arm64", "c", ccr.NO_CONTAINERS_LABEL,
                          "NO CONTAINERS SELECTED"),
        _mkrow_for_assert("16", "deb", "arm64", "c", ccr.NOT_CONTAINER_SCOPED_LABEL,
                          "PASSED", tests=1, passed=1),
        _mkrow_for_assert("16", "deb", "arm64", "c", ccr.NOT_CONTAINER_SCOPED_LABEL,
                          "PASSED", tests=1, passed=1),
    ]
    ccr._assert_unique_real_row_keys(rows)   # no exception


def test_build_rows_assigns_detail_href_for_real_container_rows(tmp_path):
    agg = tmp_path / "aggregated"
    sd = agg / "test-logs-r99-a1-pg16-deb-arm64"
    _write_summary(sd, pg="16", family="deb", arch="arm64",
                   **{"runner.label": "ubuntu-24.04-arm"})
    (sd / "postgis" / "16").mkdir(parents=True)
    (sd / "postgis" / "16" / "report-deb-postgis-16.xml").write_text(
        '<?xml version="1.0"?>'
        '<testsuite tests="2" failures="0" skipped="0">'
        '  <testcase name="t1[auto-debian12-arm-deb]" time="0.1"/>'
        '  <testcase name="t2[auto-debian13-arm-deb]" time="0.2"/>'
        '</testsuite>'
    )
    rows = ccr.build_rows(agg)
    real = [r for r in rows if r["container"].startswith("auto-")]
    assert len(real) == 2
    for r in real:
        assert r["detail_href"].startswith("details/detail-postgis-")
        assert r["detail_href"].endswith(".html")
    # Distinct per container.
    assert len({r["detail_href"] for r in real}) == 2


def test_build_rows_leaves_detail_href_empty_for_no_reports(tmp_path):
    agg = tmp_path / "aggregated"
    sd = agg / "test-logs-r99-a1-pg16-deb-arm64"
    sd.mkdir(parents=True)
    rows = ccr.build_rows(agg)
    assert len(rows) == 1
    assert rows[0]["status"] == "NO REPORTS"
    assert rows[0]["detail_href"] == ""


def test_build_rows_leaves_detail_href_empty_for_non_container_buckets(tmp_path):
    # NOTSET -> NO_CONTAINERS_LABEL bucket must not claim a detail_href.
    agg = tmp_path / "aggregated"
    sd = agg / "test-logs-r99-a1-pg16-deb-arm64"
    _write_summary(sd, pg="16", family="deb", arch="arm64",
                   **{"runner.label": "ubuntu-24.04-arm"})
    (sd / "comp" / "16").mkdir(parents=True)
    (sd / "comp" / "16" / "report-deb-comp-16.xml").write_text(
        '<?xml version="1.0"?>'
        '<testsuite tests="1" failures="0" skipped="1">'
        '  <testcase name="t1[bloom-NOTSET]" time="0.1">'
        '    <skipped message="no container"/>'
        '  </testcase>'
        '</testsuite>'
    )
    rows = ccr.build_rows(agg)
    nc = [r for r in rows if r["container"] == ccr.NO_CONTAINERS_LABEL]
    assert nc and all(r["detail_href"] == "" for r in nc)


def test_main_writes_per_container_detail_files(tmp_path):
    # Realistic fixture mirroring the r55 layout: one XML covering 3
    # containers must yield 3 distinct, container-scoped detail pages.
    sd = tmp_path / "test-logs-r1-a1-pg16-deb-arm64"
    _write_summary(sd, pg="16", family="deb", arch="arm64",
                   **{"runner.label": "ubuntu-24.04-arm"})
    (sd / "postgis" / "16").mkdir(parents=True)
    (sd / "postgis" / "16" / "report-deb-postgis-16.xml").write_text(
        '<?xml version="1.0"?>'
        '<testsuite tests="3" failures="1" skipped="0">'
        '  <testcase name="test_ok[auto-debian12-arm-deb]" time="0.1"/>'
        '  <testcase name="test_ok[auto-debian13-arm-deb]" time="0.2"/>'
        '  <testcase name="test_bad[auto-ubuntu2404-arm-deb]" time="0.3">'
        '    <failure message="SBOM verification failed">tb body</failure>'
        '  </testcase>'
        '</testsuite>',
        encoding="utf-8",
    )
    (sd / "postgis" / "16" / "report-deb-postgis-16.html").write_text(
        "<html/>", encoding="utf-8")
    out = tmp_path / "consolidated-report.html"

    rc = ccr.main(["--input-dir", str(tmp_path), "--output", str(out)])
    assert rc == 0

    details_dir = tmp_path / "details"
    assert details_dir.is_dir()
    files = sorted(p.name for p in details_dir.glob("*.html"))
    assert files == [
        "detail-postgis-pg16-deb-arm64-auto-debian12-arm.html",
        "detail-postgis-pg16-deb-arm64-auto-debian13-arm.html",
        "detail-postgis-pg16-deb-arm64-auto-ubuntu2404-arm.html",
    ]
    # The ubuntu page must contain only its own testcase.
    ubuntu_path = details_dir / "detail-postgis-pg16-deb-arm64-auto-ubuntu2404-arm.html"
    ubuntu = ubuntu_path.read_text(encoding="utf-8")
    assert "auto-ubuntu2404-arm" in ubuntu
    assert "auto-debian12-arm" not in ubuntu
    assert "auto-debian13-arm" not in ubuntu
    # Back-link to the framework's combined report resolves from details/.
    expected_back = "../test-logs-r1-a1-pg16-deb-arm64/postgis/16/report-deb-postgis-16.html"
    assert f'href="{expected_back}"' in ubuntu
    assert (details_dir / expected_back).resolve().is_file()


def test_main_writes_detail_pages_with_utf8_encoding(tmp_path):
    # Non-ASCII characters in the failure body must survive round-trip.
    sd = tmp_path / "test-logs-r1-a1-pg16-deb-arm64"
    _write_summary(sd, pg="16", family="deb", arch="arm64",
                   **{"runner.label": "ubuntu-24.04-arm"})
    (sd / "comp" / "16").mkdir(parents=True)
    (sd / "comp" / "16" / "report-deb-comp-16.xml").write_text(
        '<?xml version="1.0"?>'
        '<testsuite tests="1" failures="1" skipped="0">'
        '  <testcase name="test_x[auto-debian12-arm-deb]" time="0.1">'
        '    <failure message="boom é café 中文">'
        'traceback é 中文</failure>'
        '  </testcase>'
        '</testsuite>',
        encoding="utf-8",
    )
    (sd / "comp" / "16" / "report-deb-comp-16.html").write_text(
        "<html/>", encoding="utf-8")
    out = tmp_path / "consolidated-report.html"
    rc = ccr.main(["--input-dir", str(tmp_path), "--output", str(out)])
    assert rc == 0
    detail = (tmp_path / "details" /
              "detail-comp-pg16-deb-arm64-auto-debian12-arm.html").read_text(encoding="utf-8")
    assert "café" in detail
    assert "中文" in detail


def test_main_uses_actual_output_filename_in_footer_back_link(tmp_path):
    # The footer link must reflect the actual output filename, not a hardcode.
    sd = tmp_path / "test-logs-r1-a1-pg16-deb-arm64"
    _write_summary(sd, pg="16", family="deb", arch="arm64",
                   **{"runner.label": "ubuntu-24.04-arm"})
    (sd / "c" / "16").mkdir(parents=True)
    (sd / "c" / "16" / "report-deb-c-16.xml").write_text(
        '<?xml version="1.0"?>'
        '<testsuite tests="1" failures="0" skipped="0">'
        '  <testcase name="t[auto-debian12-arm-deb]" time="0.1"/>'
        '</testsuite>',
        encoding="utf-8",
    )
    out = tmp_path / "consolidated-report-v24.html"   # non-default filename
    rc = ccr.main(["--input-dir", str(tmp_path), "--output", str(out)])
    assert rc == 0
    detail = (tmp_path / "details" /
              "detail-c-pg16-deb-arm64-auto-debian12-arm.html").read_text(encoding="utf-8")
    assert 'href="../consolidated-report-v24.html"' in detail
    assert 'href="../consolidated-report.html"' not in detail


def test_main_removes_stale_detail_files_only(tmp_path):
    # Seed a stale detail file from a hypothetical prior run, plus unrelated
    # files the cleanup must NOT touch.
    details = tmp_path / "details"
    details.mkdir()
    stale = details / "detail-stale-pg16-deb-arm64-auto-foo.html"
    stale.write_text("stale", encoding="utf-8")
    keep_txt = details / "NOTES.txt"
    keep_txt.write_text("keep me", encoding="utf-8")
    keep_html = details / "index.html"   # not detail-*.html
    keep_html.write_text("keep also", encoding="utf-8")
    # Empty input -> no new detail files, but the stale one must go.
    (tmp_path / "test-logs-r1-a1-pg16-deb-arm64").mkdir(parents=True)
    out = tmp_path / "consolidated-report.html"
    rc = ccr.main(["--input-dir", str(tmp_path), "--output", str(out)])
    assert rc == 0
    assert not stale.exists()
    assert keep_txt.exists() and keep_txt.read_text(encoding="utf-8") == "keep me"
    assert keep_html.exists() and keep_html.read_text(encoding="utf-8") == "keep also"


def test_main_skips_detail_for_no_reports_rows(tmp_path):
    (tmp_path / "test-logs-r1-a1-pg16-deb-arm64").mkdir(parents=True)
    out = tmp_path / "consolidated-report.html"
    rc = ccr.main(["--input-dir", str(tmp_path), "--output", str(out)])
    assert rc == 0
    # No detail files written (the only row is NO REPORTS).
    assert not (tmp_path / "details").exists() or \
           not any((tmp_path / "details").glob("detail-*.html"))


def test_main_drops_records_scaffolding_before_render(tmp_path):
    # _records must NEVER reach render_html(). Verify post-main rows are
    # clean of internal scaffolding keys.
    sd = tmp_path / "test-logs-r1-a1-pg16-deb-arm64"
    _write_summary(sd, pg="16", family="deb", arch="arm64",
                   **{"runner.label": "ubuntu-24.04-arm"})
    (sd / "c" / "16").mkdir(parents=True)
    (sd / "c" / "16" / "report-deb-c-16.xml").write_text(
        '<?xml version="1.0"?>'
        '<testsuite tests="1" failures="0" skipped="0">'
        '  <testcase name="t[auto-debian12-arm-deb]" time="0.1"/>'
        '</testsuite>',
        encoding="utf-8",
    )
    # Also seed a NO REPORTS slice to confirm _records is dropped even on
    # rows that never carried it -- belt-and-braces.
    (tmp_path / "test-logs-r1-a1-pg17-rpm-arm64").mkdir(parents=True)

    out = tmp_path / "consolidated-report.html"
    rc = ccr.main(["--input-dir", str(tmp_path), "--output", str(out)])
    assert rc == 0
    # The rendered HTML must not leak the literal '_records' key (would
    # appear if a row dict was ever serialised to the page).
    page = out.read_text(encoding="utf-8")
    assert "_records" not in page


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


# ---- v2.3: component slug helpers ------------------------------------------


def test_component_slug_basic():
    assert ccr._component_slug("ace") == "ace"
    assert ccr._component_slug("pg_stat_monitor") == "pg_stat_monitor"
    assert ccr._component_slug("failover-spock") == "failover-spock"


def test_component_slug_normalizes_special_chars():
    # Spaces, dots, mixed case, punctuation all collapse to '-'; trailing
    # dashes are trimmed.
    assert ccr._component_slug("My Cool Comp!") == "my-cool-comp"
    assert ccr._component_slug("foo.bar") == "foo-bar"
    assert ccr._component_slug("FOO/BAR") == "foo-bar"
    assert ccr._component_slug("  weird   ") == "weird"


def test_component_slug_unattributed_is_reserved():
    # The '-' component marker (NO REPORTS rows) gets the reserved slug
    # so the trailing unattributed section has a stable anchor.
    assert ccr._component_slug(ccr.UNATTRIBUTED_COMPONENT) == ccr.UNATTRIBUTED_SLUG
    assert ccr.UNATTRIBUTED_SLUG == "unattributed"


def test_component_slug_empty_falls_back_to_unnamed():
    assert ccr._component_slug("!!!") == "unnamed"
    assert ccr._component_slug("") == "unnamed"


def test_assign_unique_slugs_first_claimant_wins():
    # Iteration order is preserved; the first name to normalize to a given
    # slug keeps the base; later collisions get '-2', '-3'.
    m = ccr._assign_unique_slugs(["foo", "Foo", "foo-bar", "foo.bar", "FOO!"])
    assert m == {
        "foo": "foo",
        "Foo": "foo-2",
        "foo-bar": "foo-bar",
        "foo.bar": "foo-bar-2",
        "FOO!": "foo-3",
    }


def test_assign_unique_slugs_handles_unattributed():
    m = ccr._assign_unique_slugs(["server", "-", "ace"])
    assert m["-"] == "unattributed"
    # The marker does not collide with a real component named 'unattributed'.
    m2 = ccr._assign_unique_slugs(["unattributed", "-"])
    assert m2["unattributed"] == "unattributed"
    assert m2["-"] == "unattributed-2"


# ---- v2.3: heatmap aggregation ---------------------------------------------


def _mkrow(pg, family, arch, component, container, status,
           tests=0, passed=0, failed=0, skipped=0):
    return {
        "pg": pg, "family": family, "arch": arch, "runner_label": "r",
        "component": component, "container": container,
        "tests": tests, "passed": passed, "failed": failed, "skipped": skipped,
        "time": 1.0, "status": status,
        "status_class": status.lower().replace(" ", ""),
        "report_href": "", "detail_href": "",
    }


def test_component_section_report_column_links_to_detail_href():
    # The Report column's primary link must point at the per-container
    # detail page, not the framework's combined pytest-html. report_href
    # is preserved on the row but is no longer the Report-column target.
    row = _mkrow("16", "deb", "arm64", "postgis", "auto-debian12-arm",
                 "FAILED", tests=15, passed=14, failed=1)
    row["detail_href"] = "details/detail-postgis-pg16-deb-arm64-auto-debian12-arm.html"
    row["report_href"] = "test-logs-r1-a1-pg16-deb-arm64/postgis/16/report-deb-postgis-16.html"
    ctx = {"run_number": "1", "run_attempt": "1", "run_id": "x",
           "event_name": "workflow_dispatch", "actor": "tester", "slice_count": 1}
    out = ccr.render_html([row], ctx)
    # The primary report-link link in the row table points at detail_href.
    m = re.search(r'<a class="report-link" href="([^"]+)"', out)
    assert m, "no report-link found in rendered HTML"
    assert m.group(1) == row["detail_href"]
    # The combined report_href is NOT what the Report column links to.
    assert f'class="report-link" href="{row["report_href"]}"' not in out


def test_component_section_renders_dash_when_detail_href_empty():
    # A skip-set row (no detail_href) should render an em-dash, not a link.
    row = _mkrow("16", "deb", "arm64", "comp", "-", "NO REPORTS")
    ctx = {"run_number": "1", "run_attempt": "1", "run_id": "x",
           "event_name": "workflow_dispatch", "actor": "tester", "slice_count": 1}
    out = ccr.render_html([row], ctx)
    # No report-link anchor anywhere in the rendered row.
    assert 'class="report-link"' not in out


def test_aggregate_heatmap_uses_tuple_target_keys():
    rows = [_mkrow("17", "rpm", "arm64", "ace", "c1", "PASSED", tests=10, passed=10)]
    components, targets, cells, totals = ccr.aggregate_heatmap(rows)
    # Internal target keys are tuples, NEVER strings — confirms refinement #1.
    assert targets == [("17", "rpm", "arm64")]
    assert ("ace", ("17", "rpm", "arm64")) in cells


def test_aggregate_heatmap_sums_per_component_target():
    # Two containers under one (component, target) -> one cell, summed.
    rows = [
        _mkrow("17", "rpm", "arm64", "ace", "auto-alma9-arm", "PASSED",
               tests=10, passed=10),
        _mkrow("17", "rpm", "arm64", "ace", "auto-oel9-arm", "FAILED",
               tests=10, passed=9, failed=1),
    ]
    _, _, cells, totals = ccr.aggregate_heatmap(rows)
    cell = cells[("ace", ("17", "rpm", "arm64"))]
    assert cell["tests"] == 20
    assert cell["failed"] == 1
    assert cell["has_issue"] is False
    assert totals["ace"]["tests"] == 20
    assert totals["ace"]["failed"] == 1
    assert totals["ace"]["has_attention"] is True


def test_aggregate_heatmap_marks_issue_cells():
    rows = [
        _mkrow("17", "rpm", "amd64", "server", "auto-alma9-amd",
               "NO CONTAINERS SELECTED"),
        _mkrow("17", "deb", "arm64", "server", "auto-debian12-arm", "PASSED",
               tests=50, passed=50),
    ]
    _, _, cells, totals = ccr.aggregate_heatmap(rows)
    # The NO CONTAINERS SELECTED row has zero counts but is_issue must propagate.
    issue_cell = cells[("server", ("17", "rpm", "amd64"))]
    assert issue_cell["tests"] == 0
    assert issue_cell["has_issue"] is True
    ok_cell = cells[("server", ("17", "deb", "arm64"))]
    assert ok_cell["has_issue"] is False
    # Component-level has_issue reflects ANY issue cell.
    assert totals["server"]["has_issue"] is True
    assert totals["server"]["has_attention"] is True


def test_aggregate_heatmap_missing_cell_absent():
    # One component runs only on rpm; deb targets exist for other components.
    # The (rpm-only-component, deb-target) cell must NOT be in `cells`.
    rows = [
        _mkrow("17", "rpm", "arm64", "snowflake", "auto-alma9-arm", "PASSED",
               tests=25, passed=25),
        _mkrow("17", "deb", "arm64", "server", "auto-debian12-arm", "PASSED",
               tests=50, passed=50),
    ]
    components, targets, cells, _ = ccr.aggregate_heatmap(rows)
    # Both (rpm, arm64) and (deb, arm64) appear as columns ...
    assert ("17", "rpm", "arm64") in targets
    assert ("17", "deb", "arm64") in targets
    # ... but snowflake has no row under (deb, arm64) -> cell is absent.
    assert ("snowflake", ("17", "deb", "arm64")) not in cells


def test_aggregate_heatmap_excludes_unattributed():
    # A NO REPORTS row (component == '-') must NOT show up as a heatmap row.
    rows = [
        _mkrow("17", "rpm", "amd64", "-", "-", "NO REPORTS"),
        _mkrow("17", "deb", "arm64", "server", "c1", "PASSED",
               tests=1, passed=1),
    ]
    components, _, _, _ = ccr.aggregate_heatmap(rows)
    assert "-" not in components
    assert components == ["server"]


def test_aggregate_heatmap_component_order_attention_first():
    rows = [
        _mkrow("17", "rpm", "arm64", "clean_b", "c1", "PASSED",
               tests=10, passed=10),
        _mkrow("17", "rpm", "arm64", "clean_a", "c1", "PASSED",
               tests=10, passed=10),
        _mkrow("17", "rpm", "arm64", "fail_low", "c1", "FAILED",
               tests=100, passed=99, failed=1),    # 1%
        _mkrow("17", "rpm", "arm64", "fail_high", "c1", "FAILED",
               tests=10, passed=8, failed=2),     # 20%
    ]
    components, _, _, _ = ccr.aggregate_heatmap(rows)
    # Failing components come first, ordered by fail_rate descending.
    # Clean components come last, alpha.
    assert components == ["fail_high", "fail_low", "clean_a", "clean_b"]


def test_aggregate_heatmap_targets_sorted_lexicographically():
    rows = [
        _mkrow("17", "rpm", "arm64", "server", "c1", "PASSED", tests=1, passed=1),
        _mkrow("16", "deb", "amd64", "server", "c1", "PASSED", tests=1, passed=1),
        _mkrow("17", "deb", "arm64", "server", "c1", "PASSED", tests=1, passed=1),
        _mkrow("16", "rpm", "amd64", "server", "c1", "PASSED", tests=1, passed=1),
    ]
    _, targets, _, _ = ccr.aggregate_heatmap(rows)
    assert targets == [
        ("16", "deb", "amd64"),
        ("16", "rpm", "amd64"),
        ("17", "deb", "arm64"),
        ("17", "rpm", "arm64"),
    ]


# ---- v2.3: band classification + heatmap renderer --------------------------


def test_band_no_row_present():
    assert ccr._band(0, 0, False, present=False) == "empty"
    assert ccr._band(0, 0, True, present=False) == "empty"


def test_band_zero_tests_with_issue_is_issue():
    # NO CONTAINERS SELECTED / NO REPORTS surface as 'issue' even with no tests.
    assert ccr._band(0, 0, True, present=True) == "issue"


def test_band_zero_tests_without_issue_is_empty():
    assert ccr._band(0, 0, False, present=True) == "empty"


def test_band_thresholds():
    # ok / warn (0–5%) / bad (5–15%) / severe (>15%)
    assert ccr._band(100, 0, False, True) == "ok"
    assert ccr._band(100, 1, False, True) == "warn"        # 1%
    assert ccr._band(100, 5, False, True) == "warn"        # boundary, ≤5%
    assert ccr._band(100, 6, False, True) == "bad"         # 6%
    assert ccr._band(100, 15, False, True) == "bad"        # boundary, ≤15%
    assert ccr._band(100, 16, False, True) == "severe"     # 16%
    assert ccr._band(10, 5, False, True) == "severe"       # 50%


def test_band_real_tests_with_issue_keeps_rate_band():
    # Refinement 4 contract: when there are real tests AND an issue, the
    # band still reflects the rate. The renderer adds an extra issue-marker
    # class on top — not _band()'s job.
    assert ccr._band(100, 0, True, True) == "ok"
    assert ccr._band(100, 2, True, True) == "warn"


def test_render_heatmap_present_and_label_buttons():
    rows = [
        _mkrow("17", "rpm", "arm64", "ace", "auto-alma9-arm", "FAILED",
               tests=10, passed=8, failed=2),
        _mkrow("17", "rpm", "arm64", "server", "auto-alma9-arm", "PASSED",
               tests=100, passed=100),
    ]
    components, targets, cells, totals = ccr.aggregate_heatmap(rows)
    slugs = ccr._assign_unique_slugs(components)
    out = ccr._render_heatmap(components, targets, cells, totals, slugs)
    assert '<div class="heat"' in out
    # CSS grid column count = N targets + 2 totals cols.
    assert 'style="--cols:3"' in out          # 1 target + 2 totals
    # Each component label is a button calling openComponent('<slug>').
    assert "openComponent('ace')" in out
    assert "openComponent('server')" in out
    # Cell content is failed/total.
    assert ">2/10<" in out
    assert ">0/100<" in out


def test_render_heatmap_issue_marker_overlay():
    # Refinement 4: a cell with tests > 0 AND has_issue must show both the
    # rate band AND the issue-marker class + glyph.
    rows = [
        _mkrow("17", "rpm", "amd64", "server", "auto-alma9-amd", "PASSED",
               tests=10, passed=10),
        _mkrow("17", "rpm", "amd64", "server", ccr.NO_CONTAINERS_LABEL,
               "NO CONTAINERS SELECTED"),
    ]
    components, targets, cells, totals = ccr.aggregate_heatmap(rows)
    slugs = ccr._assign_unique_slugs(components)
    out = ccr._render_heatmap(components, targets, cells, totals, slugs)
    # The combined cell has 10 tests, 0 failed, has_issue=True.
    # Band -> 'ok'. Plus an explicit issue-marker class. Plus the &#9888; glyph.
    assert "issue-marker" in out
    assert "ok issue-marker" in out
    assert "&#9888;" in out


def test_render_heatmap_empty_cell_for_missing_combination():
    # snowflake only runs on rpm. The deb column exists (server runs there)
    # but snowflake's deb cell must render as 'empty' with a dash.
    rows = [
        _mkrow("17", "rpm", "arm64", "snowflake", "c1", "PASSED",
               tests=25, passed=25),
        _mkrow("17", "deb", "arm64", "server", "c1", "PASSED",
               tests=50, passed=50),
    ]
    components, targets, cells, totals = ccr.aggregate_heatmap(rows)
    slugs = ccr._assign_unique_slugs(components)
    out = ccr._render_heatmap(components, targets, cells, totals, slugs)
    assert "h cell empty" in out
    assert "&mdash;" in out


def test_render_heatmap_row_totals_match_aggregation():
    rows = [
        _mkrow("17", "rpm", "arm64", "ace", "c1", "FAILED",
               tests=30, passed=28, failed=2),
        _mkrow("17", "deb", "arm64", "ace", "c2", "FAILED",
               tests=20, passed=18, failed=2),
    ]
    components, targets, cells, totals = ccr.aggregate_heatmap(rows)
    slugs = ccr._assign_unique_slugs(components)
    out = ccr._render_heatmap(components, targets, cells, totals, slugs)
    # Right-edge totals must match component_totals.
    assert ">4</div>" in out    # failed total
    assert ">50</div>" in out   # tests total


def test_render_unattributed_banner_when_no_reports_present():
    rows = [
        _mkrow("17", "rpm", "amd64", "-", "-", "NO REPORTS"),
        _mkrow("17", "deb", "arm64", "server", "c1", "PASSED",
               tests=1, passed=1),
    ]
    out = ccr._render_unattributed_banner(rows)
    assert "1 matrix target" in out
    assert "no reports" in out.lower()
    assert "comp-unattributed" in out


def test_render_unattributed_banner_absent_when_clean():
    rows = [
        _mkrow("17", "deb", "arm64", "server", "c1", "PASSED",
               tests=1, passed=1),
    ]
    out = ccr._render_unattributed_banner(rows)
    assert out == ""


def test_render_unattributed_banner_uses_collision_resolved_slug():
    """When a real component normalizes to "unattributed", the NO REPORTS
    bucket gets bumped to a -N suffix by _assign_unique_slugs. The banner
    must link to the bucket's actual section, not the real component's."""
    rows = [
        _mkrow("17", "rpm", "arm64", "unattributed", "c1", "PASSED",
               tests=1, passed=1),
        _mkrow("17", "rpm", "amd64", ccr.UNATTRIBUTED_COMPONENT,
               "-", "NO REPORTS"),
    ]
    # Drive through render_html end-to-end so the slug map is the same one
    # the section renderer uses — guarantees the banner and the bucket
    # section stay in sync regardless of internal ordering.
    ctx = {"run_number": "1", "run_attempt": "1", "run_id": "x",
           "event_name": "workflow_dispatch", "actor": "hayee-bhatti",
           "slice_count": 2}
    out = ccr.render_html(rows, ctx)

    # The real "unattributed" component and the bucket must resolve to
    # different anchors — one will be "comp-unattributed" and the other
    # "comp-unattributed-2"; both must be present, distinct.
    assert 'id="comp-unattributed"' in out
    assert 'id="comp-unattributed-2"' in out

    # The banner href must point to whichever anchor the bucket section
    # actually carries — extract it from the rendered output and confirm
    # the matching section anchor exists.
    banner_match = re.search(
        r'banner banner-issue[^<]*<strong>[^<]*</strong>[^<]*'
        r'<a href="#(comp-unattributed(?:-\d+)?)">',
        out,
    )
    assert banner_match, "unattributed banner not found / wrong shape"
    target_anchor = banner_match.group(1)
    assert f'id="{target_anchor}"' in out, (
        f"banner links to #{target_anchor} but no section has that id"
    )

    # And the section the banner points at must be the bucket (display name
    # "(unattributed)"), not the real component named "unattributed".
    # Find the <details> block carrying that id and verify its summary.
    section_match = re.search(
        r'<details[^>]*id="' + re.escape(target_anchor) + r'"[^>]*>'
        r'(.*?)</details>',
        out,
        re.DOTALL,
    )
    assert section_match, "could not isolate the bucket section"
    assert "(unattributed)" in section_match.group(1), (
        "banner points at the real component, not the NO REPORTS bucket"
    )


def test_render_heatmap_empty_returns_empty_string():
    # No rows -> no heatmap.
    out = ccr._render_heatmap([], [], {}, {}, {})
    assert out == ""


def test_render_heatmap_escapes_component_names():
    rows = [_mkrow("17", "rpm", "arm64", "<bad>name", "c1", "PASSED",
                   tests=1, passed=1)]
    components, targets, cells, totals = ccr.aggregate_heatmap(rows)
    slugs = ccr._assign_unique_slugs(components)
    out = ccr._render_heatmap(components, targets, cells, totals, slugs)
    # Visible label is escaped; injection neutralized.
    assert "<bad>name" not in out
    assert "&lt;bad&gt;name" in out


def test_target_containers_groups_and_sorts():
    rows = [
        _mkrow("16", "deb", "arm64", "ace", "auto-ubuntu2404-arm", "PASSED",
               tests=1, passed=1),
        _mkrow("16", "deb", "arm64", "server", "auto-debian12-arm", "PASSED",
               tests=1, passed=1),
        _mkrow("16", "deb", "arm64", "ace", "auto-debian12-arm", "PASSED",
               tests=1, passed=1),
        _mkrow("16", "rpm", "amd64", "ace", "auto-alma9-amd", "PASSED",
               tests=1, passed=1),
        # Unattributed rows must not pollute the target->container map.
        _mkrow("16", "deb", "arm64", ccr.UNATTRIBUTED_COMPONENT, "-",
               "NO REPORTS"),
    ]
    m = ccr.target_containers(rows)
    # deb/arm64 aggregates two distinct containers, deduped + sorted.
    assert m[("16", "deb", "arm64")] == [
        "auto-debian12-arm", "auto-ubuntu2404-arm"
    ]
    assert m[("16", "rpm", "amd64")] == ["auto-alma9-amd"]
    # The unattributed "-" container is never recorded as a target.
    assert all(c != "-" for conts in m.values() for c in conts)


def test_render_heatmap_header_tooltip_lists_containers():
    rows = [
        _mkrow("16", "deb", "arm64", "ace", "auto-ubuntu2404-arm", "PASSED",
               tests=1, passed=1),
        _mkrow("16", "deb", "arm64", "ace", "auto-debian12-arm", "PASSED",
               tests=1, passed=1),
    ]
    components, targets, cells, totals = ccr.aggregate_heatmap(rows)
    slugs = ccr._assign_unique_slugs(components)
    tc = ccr.target_containers(rows)
    out = ccr._render_heatmap(components, targets, cells, totals, slugs, tc)
    # Column header carries a title listing both containers + the count.
    assert 'title="PG16 deb arm64 — 2 containers: ' \
           'auto-debian12-arm, auto-ubuntu2404-arm"' in out


def test_render_heatmap_header_tooltip_singular_container():
    rows = [
        _mkrow("16", "deb", "amd64", "ace", "auto-debian13-amd", "PASSED",
               tests=1, passed=1),
    ]
    components, targets, cells, totals = ccr.aggregate_heatmap(rows)
    slugs = ccr._assign_unique_slugs(components)
    tc = ccr.target_containers(rows)
    out = ccr._render_heatmap(components, targets, cells, totals, slugs, tc)
    # "1 container" (singular, no trailing 's').
    assert "1 container:" in out
    assert "1 containers:" not in out


def test_render_heatmap_header_tooltip_escaped():
    rows = [
        _mkrow("16", "deb", "amd64", "ace", "<evil>cont", "PASSED",
               tests=1, passed=1),
    ]
    components, targets, cells, totals = ccr.aggregate_heatmap(rows)
    slugs = ccr._assign_unique_slugs(components)
    tc = ccr.target_containers(rows)
    out = ccr._render_heatmap(components, targets, cells, totals, slugs, tc)
    assert "<evil>cont" not in out
    assert "&lt;evil&gt;cont" in out


# ---- v2.3: component section renderer --------------------------------------


def test_render_component_section_open_when_failing():
    rows = [
        _mkrow("17", "rpm", "arm64", "ace", "c1", "FAILED",
               tests=10, passed=9, failed=1),
    ]
    out = ccr._render_component_section("ace", "ace", rows)
    assert '<details class="component" id="comp-ace" open' in out
    assert 'data-has-attention="1"' in out
    # Chip set: 1 rows, 10 tests, 1 failed, 10.0% fail (skipped 0 suppressed)
    assert ">1 rows<" in out
    assert ">10 tests<" in out
    assert ">1 failed<" in out
    assert ">10.0% fail<" in out


def test_render_component_section_closed_when_clean():
    rows = [
        _mkrow("17", "deb", "arm64", "ace", "c1", "PASSED",
               tests=10, passed=10),
    ]
    out = ccr._render_component_section("ace", "ace", rows)
    # Closed: no ' open' attribute, no data-has-attention.
    assert '<details class="component" id="comp-ace"' in out
    assert 'open data-has-attention' not in out
    assert 'data-has-attention="1"' not in out
    # "failed" chip is suppressed when 0; "fail %" still shown.
    assert ">0 failed<" not in out
    assert ">0.0% fail<" in out


def test_render_component_section_issue_chip_when_no_failures_but_issue():
    rows = [
        _mkrow("17", "rpm", "amd64", "server", ccr.NO_CONTAINERS_LABEL,
               "NO CONTAINERS SELECTED"),
    ]
    out = ccr._render_component_section("server", "server", rows)
    # No real failures (tests/failed both 0) but a report issue is present.
    assert "report issue" in out
    assert 'data-has-attention="1"' in out


def test_render_component_section_row_data_attributes():
    rows = [
        _mkrow("17", "rpm", "arm64", "ace", "auto-alma9-arm", "FAILED",
               tests=10, passed=9, failed=1),
    ]
    out = ccr._render_component_section("ace", "ace", rows)
    # Every row carries the data-* attributes that the sortGroup JS keys on.
    assert 'data-pg="17"' in out
    assert 'data-family="rpm"' in out
    assert 'data-arch="arm64"' in out
    assert 'data-container="auto-alma9-arm"' in out
    assert 'data-status="FAILED"' in out
    assert 'data-fail="1"' in out


def test_render_component_section_sort_buttons_present():
    rows = [_mkrow("17", "rpm", "arm64", "ace", "c1", "PASSED",
                   tests=1, passed=1)]
    out = ccr._render_component_section("ace", "ace", rows)
    # Five sortable columns; the remaining columns are unsortable plain th.
    for key in ("'pg'", "'family'", "'arch'", "'container'", "'status'"):
        assert f"sortGroup(this,{key})" in out
    # 'tests' / 'passed' etc. are NOT sortable.
    assert "sortGroup(this,'tests')" not in out


def test_render_component_section_escapes_dynamic_text():
    rows = [_mkrow("17", "rpm", "arm64", "<bad>", "auto&co", "PASSED",
                   tests=1, passed=1)]
    out = ccr._render_component_section("<bad>", "bad", rows)
    # Refinement 6: every visible dynamic text + data-* attr must be escaped.
    assert "<bad>" not in out.replace("&lt;bad&gt;", "")
    assert "&lt;bad&gt;" in out
    assert "auto&amp;co" in out
    # Also escaped inside data-container="..."
    assert 'data-container="auto&amp;co"' in out


def test_render_component_section_attention_rows_first():
    rows = [
        _mkrow("17", "rpm", "arm64", "ace", "auto-rocky9-arm", "PASSED",
               tests=10, passed=10),
        _mkrow("17", "rpm", "arm64", "ace", "auto-alma9-arm", "FAILED",
               tests=10, passed=9, failed=1),
    ]
    out = ccr._render_component_section("ace", "ace", rows)
    failed_pos = out.index("auto-alma9-arm")
    passed_pos = out.index("auto-rocky9-arm")
    assert failed_pos < passed_pos


def test_render_component_section_unattributed_label():
    rows = [_mkrow("17", "rpm", "amd64", "-", "-", "NO REPORTS")]
    out = ccr._render_component_section(ccr.UNATTRIBUTED_COMPONENT,
                                        ccr.UNATTRIBUTED_SLUG, rows)
    # Display label is the friendly "(unattributed)" not bare "-".
    assert "(unattributed)" in out
    assert 'id="comp-unattributed"' in out
    assert 'data-has-attention="1"' in out  # NO REPORTS is an attention row


def test_render_component_sections_orchestrates_with_unattributed():
    rows = [
        _mkrow("17", "rpm", "arm64", "ace", "c1", "PASSED",
               tests=1, passed=1),
        _mkrow("17", "rpm", "amd64", "-", "-", "NO REPORTS"),
    ]
    components, _, _, _ = ccr.aggregate_heatmap(rows)
    slugs = ccr._assign_unique_slugs(components)
    out = ccr._render_component_sections(rows, components, slugs)
    # Real component first, unattributed last.
    ace_pos = out.index('id="comp-ace"')
    un_pos = out.index('id="comp-unattributed"')
    assert ace_pos < un_pos


def test_render_component_sections_no_unattributed_when_clean():
    rows = [
        _mkrow("17", "rpm", "arm64", "ace", "c1", "PASSED",
               tests=1, passed=1),
    ]
    components, _, _, _ = ccr.aggregate_heatmap(rows)
    slugs = ccr._assign_unique_slugs(components)
    out = ccr._render_component_sections(rows, components, slugs)
    assert "comp-unattributed" not in out


# ---- v2.3: single-script-block JS contract ---------------------------------


def test_scripts_single_block_invariant():
    # The existing tests assert out.count("<script") == 1. _render_scripts()
    # must keep all JS in ONE block so that constraint survives.
    js = ccr._render_scripts()
    assert js.count("<script") == 1
    assert js.endswith("</script>")


def test_scripts_contain_all_expected_functions():
    js = ccr._render_scripts()
    for name in ("toggleFailures", "sortGroup", "openComponent",
                 "expandAll", "collapseAll", "openFailing", "STATUS_RANK"):
        assert name in js


def test_scripts_status_rank_order_attention_first():
    js = ccr._render_scripts()
    # FAILED must rank ahead of PASSED for the in-section status sort to put
    # attention rows on top (alphabetical would do the wrong thing).
    failed_pos = js.index("'FAILED':0")
    passed_pos = js.index("'PASSED':6")
    assert failed_pos < passed_pos


def test_scripts_toggle_uses_body_class_not_details_mutation():
    js = ccr._render_scripts()
    # Refinement 3: toggleFailures must use document.body.classList, NOT
    # mutate d.open on details elements. This protects the user's open/closed
    # state from being clobbered by the toggle.
    assert "document.body.classList.toggle('failures-only'" in js
    # Belt-and-suspenders: toggleFailures must not touch details state.
    fn_start = js.index("function toggleFailures")
    fn_end = js.index("function ", fn_start + 1)
    toggle_body = js[fn_start:fn_end]
    assert "details" not in toggle_body
    assert ".open" not in toggle_body


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
