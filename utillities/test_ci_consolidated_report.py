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
