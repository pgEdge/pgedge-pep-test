#!/usr/bin/env python3
"""
CI-only cross-slice consolidated report generator.

Reads the downloaded per-slice artifact tree produced by the PEP Regression
GitHub Actions workflow and renders a single consolidated-report.html that
spans every slice in the run.

This script is intentionally separate from the report generator embedded in
run_pep_tf.sh: local/sequential reporting is unchanged. It never renames or
flattens per-slice files; it only reads them.

Slice metadata comes from each slice's workflow-summary.txt, with a fallback
to the slice directory name (test-logs-r<N>-a<M>-pg<P>-<family>-<arch>).
Component is derived from the directory path (<slice>/<component>/<pg>/...),
not from report filename string-splitting. Test results are parsed from the
JUnit XML body; report XMLs that are duplicated under a consolidated-* folder
are excluded so results are not double-counted.
"""

import argparse
import html
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


# Directory name pattern: test-logs-r<run>-a<attempt>-pg<pg>-<family>-<arch>
_DIRNAME_RE = re.compile(
    r"test-logs-r(?P<run>\d+)-a(?P<attempt>\d+)-pg(?P<pg>[^-]+)-(?P<family>[^-]+)-(?P<arch>.+)$"
)


def parse_slice_metadata(slice_dir: Path) -> dict:
    """Return slice identity for one per-slice artifact directory.

    Primary source: workflow-summary.txt. Fallback: the directory name.
    Always returns pg/family/arch (possibly 'unknown') and a metadata_source.
    """
    meta = {
        "pg": "unknown", "family": "unknown", "arch": "unknown",
        "runner_label": "", "runner_arch": "",
        "run_id": "", "run_attempt": "", "run_number": "",
        "event_name": "", "actor": "",
        "repo": "", "components": "", "execution_mode": "", "sha": "", "ref": "",
        "metadata_source": "",
    }
    summary = slice_dir / "workflow-summary.txt"
    if summary.is_file():
        kv = {}
        for line in summary.read_text(errors="replace").splitlines():
            m = re.match(r"\[workflow-summary\]\s+([^=]+)=(.*)$", line)
            if m:
                kv[m.group(1).strip()] = m.group(2).strip()
        meta["pg"] = kv.get("pg", meta["pg"])
        meta["family"] = kv.get("family", meta["family"])
        meta["arch"] = kv.get("arch", meta["arch"])
        meta["runner_label"] = kv.get("runner.label", "")
        meta["runner_arch"] = kv.get("runner.arch", "")
        meta["run_id"] = kv.get("github.run_id", "")
        meta["run_attempt"] = kv.get("github.run_attempt", "")
        meta["run_number"] = kv.get("github.run_number", "")
        meta["event_name"] = kv.get("github.event_name", "")
        meta["actor"] = kv.get("github.actor", "")
        # Effective run-selection fields (for the report header).
        meta["repo"] = kv.get("repo", "")
        meta["components"] = kv.get("components", "")
        meta["execution_mode"] = kv.get("execution_mode", "")
        meta["sha"] = kv.get("github.sha", "")
        meta["ref"] = kv.get("github.ref", "")
        meta["metadata_source"] = "workflow-summary.txt"
        # If summary somehow lacked the slice keys, backfill from dir name.
        if meta["pg"] == "unknown" or meta["family"] == "unknown" or meta["arch"] == "unknown":
            _backfill_from_dirname(slice_dir, meta)
        return meta

    _backfill_from_dirname(slice_dir, meta)
    meta["metadata_source"] = "directory-name"
    return meta


def _backfill_from_dirname(slice_dir: Path, meta: dict) -> None:
    m = _DIRNAME_RE.search(slice_dir.name)
    if not m:
        return
    if meta["pg"] == "unknown":
        meta["pg"] = m.group("pg")
    if meta["family"] == "unknown":
        meta["family"] = m.group("family")
    if meta["arch"] == "unknown":
        meta["arch"] = m.group("arch")
    if not meta["run_number"]:
        meta["run_number"] = m.group("run")
    if not meta["run_attempt"]:
        meta["run_attempt"] = m.group("attempt")


def discover_report_xmls(slice_dir: Path) -> list:
    """All report-*.xml under the slice, EXCLUDING the duplicate copies that
    live under any consolidated-* directory (those are byte-identical copies
    the framework makes for its own per-slice consolidated page).
    """
    result = []
    for xml in slice_dir.rglob("report-*.xml"):
        if any(part.startswith("consolidated-") for part in xml.relative_to(slice_dir).parts):
            continue
        result.append(xml)
    return result


def derive_component_from_path(xml_path: Path, slice_dir: Path) -> str:
    """Component is the first path segment under the slice directory:
    <slice>/<component>/<pg>/report-*.xml  ->  <component>.
    Falls back to 'unknown' if the layout is unexpected.
    """
    rel = xml_path.relative_to(slice_dir).parts
    if len(rel) >= 1:
        return rel[0]
    return "unknown"


# Match "[<container>-<rhel|deb>" where the type is followed by ] or -.
# Prevents a false match on 'debian' inside names like auto-debian13-amd.
_CONTAINER_RE = re.compile(r"\[(.+)-(rhel|deb)(?=[-\]])")


def _normalize_container(raw: str) -> str:
    """Strip an extension prefix so 'bloom-auto-alma10-arm' -> 'auto-alma10-arm'."""
    for pfx in ("auto-", "my-"):
        idx = raw.find(pfx)
        if idx >= 0:
            return raw[idx:]
    return raw


def _looks_like_container(name: str) -> bool:
    """Containers in containers_list.json all begin with 'auto-' or 'my-'.
    Gates the bracket FALLBACK so arbitrary pytest parameters (a port number,
    a feature flag, etc.) are not mistaken for containers.
    """
    return name.startswith("auto-") or name.startswith("my-")


# Two distinct non-container buckets (kept separate so the report can tell them
# apart and totals stay honest). The labels double as the grouping keys; the
# parentheses guarantee they never collide with a real container name.
#   NO_CONTAINERS_LABEL      : pytest '[NOTSET]' placeholder skips emitted when a
#                              matrix target resolved zero containers (empty
#                              parameter set). These are report-scope metadata,
#                              NOT real tests -> excluded from totals.
#   NOT_CONTAINER_SCOPED_LABEL: genuine tests that simply are not parametrized by
#                              container and actually ran -> counted normally.
NO_CONTAINERS_LABEL = "(none in scope)"
NOT_CONTAINER_SCOPED_LABEL = "(not container-scoped)"


def parse_junit_xml(xml_path: Path) -> dict:
    """Parse one JUnit XML, grouping every test case by base container.

    Iterates ALL <testcase> elements anywhere in the tree (single suite,
    multiple <testsuite> under <testsuites>, etc.) so counts cannot be
    silently undercounted.

    Container resolution, in order:
      * Primary  : canonical pytest param form '[<container>-<rhel|deb>'.
      * Fallback : a bracketed param that, after normalization, still looks
                   like a container (starts with auto-/my-).
      * Otherwise: 'unattributed' (counted, never dropped).
    """
    groups = {}

    def bucket(name):
        return groups.setdefault(
            name, {"tests": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0}
        )

    tree = ET.parse(xml_path)
    root = tree.getroot()

    for tc in root.iter("testcase"):
        name = tc.get("name", "")
        tc_time = float(tc.get("time", 0) or 0)

        m = _CONTAINER_RE.search(name)
        if m:
            container = _normalize_container(m.group(1))
        else:
            m2 = re.search(r"\[([^\]]+)\]", name)
            if m2:
                raw = m2.group(1)
                # 'NOTSET' is pytest's empty-parameter-set sentinel. It appears
                # either alone ('[NOTSET]') or as a '-'-delimited token in a
                # doubly-parametrized id ('[bloom-NOTSET]' = extension set,
                # container empty). Both mean the matrix target had no containers
                # in scope -> report metadata, not a real test.
                if "NOTSET" in raw.split("-"):
                    container = NO_CONTAINERS_LABEL
                else:
                    candidate = _normalize_container(raw)
                    container = candidate if _looks_like_container(candidate) else NOT_CONTAINER_SCOPED_LABEL
            else:
                container = NOT_CONTAINER_SCOPED_LABEL

        g = bucket(container)
        g["tests"] += 1
        g["time"] += tc_time
        if tc.find("failure") is not None or tc.find("error") is not None:
            g["failed"] += 1
        elif tc.find("skipped") is not None:
            g["skipped"] += 1
        else:
            g["passed"] += 1

    return groups


def _status_for(stats: dict) -> tuple:
    if stats["failed"] > 0:
        return "FAILED", "failed"
    if stats["tests"] > 0 and stats["skipped"] == stats["tests"]:
        return "SKIPPED", "skipped"
    return "PASSED", "passed"


def build_rows(aggregated_dir: Path) -> list:
    """Walk every per-slice directory under aggregated_dir and produce one row
    per (slice, component, container). Slices with metadata but no report XMLs
    yield a single 'NO REPORTS' row so they are visible, not silently omitted.
    """
    rows = []
    slice_dirs = sorted(
        d for d in aggregated_dir.iterdir()
        if d.is_dir() and d.name.startswith("test-logs-")
    )
    for sd in slice_dirs:
        meta = parse_slice_metadata(sd)
        xmls = discover_report_xmls(sd)

        if not xmls:
            rows.append({
                "pg": meta["pg"], "family": meta["family"], "arch": meta["arch"],
                "runner_label": meta["runner_label"],
                "component": "-", "container": "-",
                "tests": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0,
                "status": "NO REPORTS", "status_class": "noreports",
                "report_href": "",
            })
            continue

        for xml in xmls:
            component = derive_component_from_path(xml, sd)
            try:
                groups = parse_junit_xml(xml)
            except Exception as e:  # malformed XML must not abort the whole report
                rows.append({
                    "pg": meta["pg"], "family": meta["family"], "arch": meta["arch"],
                    "runner_label": meta["runner_label"],
                    "component": component, "container": "-",
                    "tests": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0,
                    "status": "PARSE ERROR", "status_class": "failed",
                    "report_href": "",
                    "note": str(e),
                })
                continue
            html_path = xml.with_suffix(".html")
            href = html_path.relative_to(aggregated_dir).as_posix() if html_path.is_file() else ""
            if not groups:
                # XML parsed but had zero <testcase> elements — surface it as a
                # report-integrity issue rather than silently emitting nothing.
                rows.append({
                    "pg": meta["pg"], "family": meta["family"], "arch": meta["arch"],
                    "runner_label": meta["runner_label"],
                    "component": component, "container": "-",
                    "tests": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0,
                    "status": "NO TESTCASES", "status_class": "noreports",
                    "report_href": href,
                })
                continue
            for container in sorted(groups.keys()):
                stats = groups[container]
                if container == NO_CONTAINERS_LABEL:
                    # Matrix target resolved zero containers (NOTSET placeholder
                    # skips). Surface the row but zero the counts so it does not
                    # inflate real test totals; render_html flags it as an
                    # attention row and counts it under Report Issues.
                    rows.append({
                        "pg": meta["pg"], "family": meta["family"], "arch": meta["arch"],
                        "runner_label": meta["runner_label"],
                        "component": component, "container": container,
                        "tests": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0,
                        "status": "NO CONTAINERS SELECTED", "status_class": "noreports",
                        "report_href": href,
                    })
                    continue
                status, status_class = _status_for(stats)
                rows.append({
                    "pg": meta["pg"], "family": meta["family"], "arch": meta["arch"],
                    "runner_label": meta["runner_label"],
                    "component": component, "container": container,
                    "tests": stats["tests"], "passed": stats["passed"],
                    "failed": stats["failed"], "skipped": stats["skipped"],
                    "time": stats["time"],
                    "status": status, "status_class": status_class,
                    "report_href": href,
                })
    return rows


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


# Statuses that demand attention: test failures AND data/report problems.
# Used both for sorting (these float to the top) and for the failures-only
# toggle (these stay visible).
_ATTENTION_STATUSES = (
    "FAILED", "PARSE ERROR", "NO REPORTS", "NO TESTCASES", "NO CONTAINERS SELECTED",
)
# Statuses with no real test data — numeric cells render as a dash.
_NO_DATA_STATUSES = ("NO REPORTS", "NO TESTCASES", "NO CONTAINERS SELECTED")
# Statuses that represent a report/coverage problem (Report Issues card).
_ISSUE_STATUSES = ("NO REPORTS", "PARSE ERROR", "NO TESTCASES", "NO CONTAINERS SELECTED")


def render_html(rows: list, ctx: dict) -> str:
    total_tests = sum(r["tests"] for r in rows)
    total_passed = sum(r["passed"] for r in rows)
    total_failed = sum(r["failed"] for r in rows)
    total_skipped = sum(r["skipped"] for r in rows)
    total_time = sum(r["time"] for r in rows)
    # Report-data problems are counted separately so a run with missing/broken
    # reports but no test failures does not look all-green in the summary.
    report_issues = sum(1 for r in rows if r["status"] in _ISSUE_STATUSES)

    # Sort: attention rows (failures, missing/broken reports) first, then by
    # pg/family/arch/component/container.
    def sort_key(r):
        return (0 if r["status"] in _ATTENTION_STATUSES else 1,
                r["pg"], r["family"], r["arch"], r["component"], r["container"])
    rows = sorted(rows, key=sort_key)

    css = """
      body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; margin: 20px; background:#f5f5f5; }
      .header { background: linear-gradient(135deg,#667eea,#764ba2); color:#fff; padding:24px; border-radius:8px; }
      .header h1 { margin:0 0 8px 0; }
      .context { font-size:13px; opacity:.95; line-height:1.6; }
      .banner { background:#fff8e1; border:1px solid #f0d98c; color:#7a5b00; padding:12px 16px; border-radius:8px; margin:16px 0; font-size:14px; }
      .summary { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin:16px 0; }
      .card { background:#fff; padding:16px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,.1); }
      .card h3 { margin:0 0 6px 0; font-size:12px; color:#666; text-transform:uppercase; }
      .card .value { font-size:28px; font-weight:bold; }
      .card.total .value{color:#667eea;} .card.passed .value{color:#10b981;}
      .card.failed .value{color:#ef4444;} .card.skipped .value{color:#f59e0b;}
      .card.issues .value{color:#b45309;}
      .controls { margin:12px 0; font-size:14px; }
      table { width:100%; border-collapse:collapse; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 2px 4px rgba(0,0,0,.1); }
      th { background:#f8f9fa; padding:10px; text-align:left; border-bottom:2px solid #dee2e6; font-size:13px; }
      td { padding:10px; border-bottom:1px solid #dee2e6; font-size:13px; }
      tr:hover { background:#f8f9fa; }
      .badge { padding:3px 10px; border-radius:12px; font-size:11px; font-weight:600; text-transform:uppercase; }
      .badge.passed{background:#d1fae5;color:#065f46;} .badge.failed{background:#fee2e2;color:#991b1b;}
      .badge.skipped{background:#fef3c7;color:#92400e;} .badge.noreports{background:#e5e7eb;color:#374151;}
      .mono { font-family:monospace; }
      a.report-link { color:#667eea; text-decoration:none; } a.report-link:hover{text-decoration:underline;}
      .footer { margin-top:20px; text-align:center; color:#666; font-size:12px; }
    """

    def _sel(key, fallback="—"):
        v = ctx.get(key)
        return _esc(v) if v else fallback

    head = f"""<!DOCTYPE html><html><head><meta charset="utf-8"/>
<title>PEP Regression Consolidated Report</title><style>{css}</style></head><body>
<div class="header">
  <h1>PEP Regression Consolidated Report</h1>
  <div class="context">
    Run #{_esc(ctx.get('run_number'))} (attempt {_esc(ctx.get('run_attempt'))}) &middot;
    run_id {_esc(ctx.get('run_id'))} &middot;
    event {_esc(ctx.get('event_name'))} &middot;
    by {_esc(ctx.get('actor'))} &middot;
    branch {_sel('ref')} &middot; sha {_sel('sha')} &middot;
    {_esc(ctx.get('slice_count'))} matrix target(s)
  </div>
  <div class="context" style="margin-top:8px">
    <strong>Effective selection:</strong>
    PG {_sel('pg_versions')} &middot; families {_sel('families')} &middot;
    arches {_sel('arches')} &middot; components {_sel('components')} &middot;
    repo {_sel('repo')} &middot; mode {_sel('execution_mode')}
  </div>
</div>
<div class="banner">
  <strong>Note:</strong> A matrix target (runner) showing green in GitHub Actions
  reflects workflow completion, not whether every component test passed. Per-row
  PASS/FAILED/SKIPPED counts below are the source of truth for test outcomes.
</div>
<div class="summary">
  <div class="card total"><h3>Total Tests</h3><div class="value">{total_tests}</div></div>
  <div class="card passed"><h3>Passed</h3><div class="value">{total_passed}</div></div>
  <div class="card failed"><h3>Failed</h3><div class="value">{total_failed}</div></div>
  <div class="card skipped"><h3>Skipped</h3><div class="value">{total_skipped}</div></div>
  <div class="card issues"><h3>Report Issues</h3><div class="value">{report_issues}</div></div>
</div>
<div class="controls">
  <label><input type="checkbox" id="failuresOnly" onclick="toggleFailures()"> Show attention rows only (failures, missing &amp; broken reports)</label>
</div>
<table id="results"><thead><tr>
  <th>PG</th><th>Family</th><th>Arch</th><th>Component</th><th>Container</th>
  <th>Status</th><th>Tests</th><th>Passed</th><th>Failed</th><th>Skipped</th>
  <th>Time (s)</th><th>Report</th>
</tr></thead><tbody>
"""

    body_rows = []
    for r in rows:
        is_fail = "1" if r["status"] in _ATTENTION_STATUSES else "0"
        if r["report_href"]:
            link = f'<a class="report-link" href="{_esc(r["report_href"])}">View &rarr;</a>'
        else:
            link = "&mdash;"
        # No-data statuses (no reports / no testcases / no containers) show a
        # dash in the numeric columns instead of a misleading 0.
        if r["status"] in _NO_DATA_STATUSES:
            tcell = pcell = fcell = scell = timecell = "&mdash;"
        else:
            tcell, pcell = str(r["tests"]), str(r["passed"])
            fcell, scell = str(r["failed"]), str(r["skipped"])
            timecell = f"{r['time']:.2f}"
        body_rows.append(f"""<tr data-fail="{is_fail}">
  <td>{_esc(r['pg'])}</td><td>{_esc(r['family'])}</td><td>{_esc(r['arch'])}</td>
  <td>{_esc(r['component'])}</td><td class="mono">{_esc(r['container'])}</td>
  <td><span class="badge {_esc(r['status_class'])}">{_esc(r['status'])}</span></td>
  <td class="mono">{tcell}</td><td class="mono">{pcell}</td>
  <td class="mono">{fcell}</td><td class="mono">{scell}</td>
  <td class="mono">{timecell}</td><td>{link}</td>
</tr>""")

    tail = f"""</tbody></table>
<div class="footer">
  {len(rows)} row(s) &middot; total execution time {total_time:.2f}s across all matrix targets.
</div>
<script>
function toggleFailures() {{
  var on = document.getElementById('failuresOnly').checked;
  var trs = document.querySelectorAll('#results tbody tr');
  for (var i=0;i<trs.length;i++) {{
    trs[i].style.display = (on && trs[i].getAttribute('data-fail') !== '1') ? 'none' : '';
  }}
}}
</script>
</body></html>"""

    return head + "\n".join(body_rows) + tail


def _run_context(aggregated_dir: Path, rows: list) -> dict:
    """Run-level context + effective selection, derived from the per-target
    workflow-summary.txt files. The 'effective selection' (distinct pg/family/
    arch + components/repo/execution_mode) is what the run actually covered;
    raw workflow_dispatch inputs are not in the artifact (and effective values
    are clearer anyway)."""
    ctx = {"run_number": "", "run_attempt": "", "run_id": "",
           "event_name": "", "actor": "", "slice_count": 0,
           "pg_versions": "", "families": "", "arches": "",
           "components": "", "repo": "", "execution_mode": "",
           "sha": "", "ref": ""}
    slice_dirs = [d for d in aggregated_dir.iterdir()
                  if d.is_dir() and d.name.startswith("test-logs-")]
    ctx["slice_count"] = len(slice_dirs)

    pgs, fams, arches = set(), set(), set()
    run_fields_set = False
    for sd in sorted(slice_dirs):
        meta = parse_slice_metadata(sd)
        if meta["pg"] and meta["pg"] != "unknown":
            pgs.add(meta["pg"])
        if meta["family"] and meta["family"] != "unknown":
            fams.add(meta["family"])
        if meta["arch"] and meta["arch"] != "unknown":
            arches.add(meta["arch"])
        # Run-level + same-across-targets fields: take from the first target
        # that carries them.
        if not run_fields_set and (meta["run_id"] or meta["run_number"]):
            ctx.update({
                "run_number": meta["run_number"], "run_attempt": meta["run_attempt"],
                "run_id": meta["run_id"], "event_name": meta["event_name"],
                "actor": meta["actor"], "repo": meta["repo"],
                "components": meta["components"], "execution_mode": meta["execution_mode"],
                "sha": meta["sha"], "ref": meta["ref"],
            })
            run_fields_set = True

    ctx["pg_versions"] = ", ".join(sorted(pgs))
    ctx["families"] = ", ".join(sorted(fams))
    ctx["arches"] = ", ".join(sorted(arches))
    return ctx


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the cross-slice consolidated CI report.")
    ap.add_argument("--input-dir", required=True, help="Directory of downloaded per-slice artifacts.")
    ap.add_argument("--output", required=True, help="Path to write consolidated-report.html.")
    args = ap.parse_args(argv)

    aggregated = Path(args.input_dir)
    if not aggregated.is_dir():
        print(f"[ci-report] input dir not found: {aggregated}", file=sys.stderr)
        return 1

    slice_dirs = [d for d in aggregated.iterdir()
                  if d.is_dir() and d.name.startswith("test-logs-")]
    rows = build_rows(aggregated)
    ctx = _run_context(aggregated, rows)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(rows, ctx))

    fails = sum(r["failed"] for r in rows)
    print(f"[ci-report] targets={len(slice_dirs)} rows={len(rows)} "
          f"tests={sum(r['tests'] for r in rows)} failed={fails} "
          f"-> {out}")
    # Always exit 0: this is a reporting step, not a gate. It must not fail the
    # aggregate job on the basis of underlying test failures.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
