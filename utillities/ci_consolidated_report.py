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


if __name__ == "__main__":  # pragma: no cover (wired up in a later task)
    pass
