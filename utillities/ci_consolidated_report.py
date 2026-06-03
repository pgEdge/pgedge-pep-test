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
                candidate = _normalize_container(m2.group(1))
                container = candidate if _looks_like_container(candidate) else "unattributed"
            else:
                container = "unattributed"

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


if __name__ == "__main__":  # pragma: no cover (wired up in a later task)
    pass
