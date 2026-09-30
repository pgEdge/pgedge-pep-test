#!/usr/bin/env python3
"""Local result-collection shell for PEP certification (NO network access).

This is the OFFLINE half of the coordinator's aggregate step. A later
``pep-certify.yml`` job lists this run's artifacts via the Actions REST API,
excludes expired IDs, downloads the remaining candidates by IMMUTABLE ID with a
pinned ``actions/download-artifact``, and then hands this module three purely
local inputs: the resolved ``pep-invocation-plan/1`` (the planner's output), a
NORMALIZED artifacts listing (the array the coordinator builds from the paginated
run-artifacts API), and the directory the action extracted the candidate ZIPs
into. This module owns NONE of that transport — API/listing/download failures are
the coordinator's, and no HTTP, token or URL handling lives here.

What it does own (deterministic, stdlib-only, fail-closed):
  * Discover candidate artifacts by a COARSE name prefix. The prefix is a filter
    only and is NEVER parsed for invocation identity — acceptance is by immutable
    artifact ID plus the validated CONTENT of each ``summary.json``.
  * Strictly validate each candidate's required API metadata, reject duplicate IDs
    or names, and ignore unrelated non-prefix artifacts.
  * Associate each non-expired candidate with the extraction layout the mechanics
    spike proved for ``actions/download-artifact`` (one artifact -> flat, with a
    named subdirectory accepted when present; multiple artifacts -> per-name
    subdirectories), resolving paths safely and refusing any name that escapes the
    download root.
  * Enforce the atomic-result contract: exactly one ROOT-level ``summary.json`` per
    non-expired candidate; a missing, nested-only or multiple ``summary.json`` is a
    per-artifact violation.
  * Preserve fail-closed evidence: a valid parsed JSON value (of ANY type) is passed
    UNCHANGED to the pure ``pep_cert_result.build_cert_result`` (which owns all
    content classification incl. non-object rejection); a malformed/absent/duplicate
    summary or any structural anomaly becomes exactly one malformed sentinel
    (``None``) in the reducer input, so the anomaly deterministically fails the
    certification closed instead of silently vanishing.
  * Emit a sanitized ``pep-collection-ledger/1`` (allowlisted fields, deterministic
    artifact-ID ordering) that records WHAT was collected without ever storing a
    token, an API/archive URL, a raw REST object or a summary body.

Generic: this module encodes NO platform, architecture, PostgreSQL version,
component or expected-invocation-count knowledge. The plan supplies the expected
invocations; the summaries self-identify; the reducer reconciles.

Exit code: 0 only when local collection ran and the reducer produced
``result_resolved == true`` (a resolved-but-incomplete or product-fail result is
still 0 — the coordinator derives workflow colour from the result axes); non-zero
when the result is unresolved. Once the arguments and output destinations are valid
and local collection has begun, BOTH the ledger and the cert-result are written
before the status is returned — including for a malformed plan, listing or summary
(which still produce a ledger plus a fail-closed cert-result). A systemic CLI error
detected up front (e.g. the two outputs resolving to one path) returns non-zero
without writing either document.

Stdlib only (plus the committed ``pep_cert_result``). Offline-testable via
``pytest utillities/test_pep_result_io.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import pep_cert_result as CR      # noqa: E402  pure attempt-aware reducer

LEDGER_SCHEMA = "pep-collection-ledger/1"
DEFAULT_NAME_PREFIX = "pep-summary-"
SUMMARY_FILE = "summary.json"

# Per-candidate extraction classifications. Exactly one of these is recorded per
# discovered candidate. Only "one_summary" contributes a value to the reducer;
# "expired" contributes nothing (it becomes missing_result where expected); every
# other value is a per-artifact anomaly contributing a malformed (None) sentinel.
EX_ONE = "one_summary"
EX_EXPIRED = "expired"
EX_MISSING = "missing_summary"
EX_MULTIPLE = "multiple_summaries"
EX_NESTED_ONLY = "nested_only"          # a summary.json exists, but not at the root (wrong path)
EX_MALFORMED_JSON = "malformed_json"
EX_AMBIGUOUS = "ambiguous_layout"
EX_UNSAFE_NAME = "unsafe_name"           # the artifact NAME is not a safe single path segment
EX_UNSAFE_PATH = "unsafe_path"           # a symlinked root/summary, or a path escaping the roots
EX_INVALID_META = "invalid_metadata"

_ANOMALY_EXTRACTIONS = frozenset({
    EX_MISSING, EX_MULTIPLE, EX_NESTED_ONLY, EX_MALFORMED_JSON,
    EX_AMBIGUOUS, EX_UNSAFE_NAME, EX_UNSAFE_PATH, EX_INVALID_META,
})


# --------------------------------------------------------------------------- #
# small type predicates (booleans are never valid ints)
# --------------------------------------------------------------------------- #
def _is_pos_int(x):
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def _is_nonneg_int(x):
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def _nonblank_str(x):
    return isinstance(x, str) and x.strip() != ""


# --------------------------------------------------------------------------- #
# safe filesystem helpers (no path may escape the download root)
# --------------------------------------------------------------------------- #
def _safe_child_dir(download_dir, name):
    """The LITERAL path ``download_dir/<name>`` when ``name`` is a safe single path
    segment; else None. TOTAL over any JSON string (never raises, incl. an embedded
    NUL): rejects a non-string, empty, ``.``/``..``, a NUL, or any path separator, so a
    name can never escape the download root. Symlink checks on the returned path are
    the caller's (this function performs no filesystem access, so it cannot raise)."""
    if not isinstance(name, str) or name == "" or name in (".", ".."):
        return None
    if "\0" in name:
        return None
    if "/" in name or "\\" in name or os.sep in name or (os.altsep and os.altsep in name):
        return None
    return os.path.join(download_dir, name)


def _within(path, *roots):
    """realpath(path) if it resolves to a location inside EVERY given root (each
    realpath'd); else None. Total (never raises). Used to prove a summary's resolved
    path stays inside the selected artifact root and the download root before reading."""
    try:
        rp = os.path.realpath(path)
        anchors = [os.path.realpath(r) for r in roots]
    except (ValueError, OSError):
        return None
    for a in anchors:
        if rp != a and not rp.startswith(a + os.sep):
            return None
    return rp


def _scan_summaries(root):
    """(root_present, total_count, has_symlink) for ``summary.json`` at/under ``root``.
    root_present is whether a ROOT-level summary.json exists; total_count counts every
    ``summary.json`` at any depth (nested extras are detectable); has_symlink is True if
    any of them is a symlink. Directory symlinks are NOT followed while scanning."""
    root_file = os.path.join(root, SUMMARY_FILE)
    root_present = os.path.isfile(root_file)
    has_symlink = os.path.islink(root_file)
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        if SUMMARY_FILE in filenames:
            total += 1
            if os.path.islink(os.path.join(dirpath, SUMMARY_FILE)):
                has_symlink = True
    return root_present, total, has_symlink


def _load_json_file(path):
    """Parse a JSON file; return (value, ok). ok is False on any read/parse fault."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), True
    except (OSError, ValueError):
        return None, False


# --------------------------------------------------------------------------- #
# candidate discovery + strict metadata validation
# --------------------------------------------------------------------------- #
def _discover(listing, name_prefix):
    """Split a normalized artifacts listing into prefix candidates, ignored
    non-prefix artifacts and structurally invalid entries.

    Returns (candidates, invalid_entry_count, listing_ok). Each candidate is a dict:
    ``{name, raw}`` where raw is the original listing object. A non-list listing is a
    top-level structural failure (listing_ok False). A non-dict entry, or a dict
    whose ``name`` is not a nonblank string, is structurally invalid (counted, never
    silently dropped, and never treated as evidence). A well-formed entry whose name
    does not match the prefix is an unrelated artifact and is ignored."""
    if not isinstance(listing, list):
        return [], 0, False
    candidates, invalid_entries = [], 0
    for entry in listing:
        if not isinstance(entry, dict):
            invalid_entries += 1
            continue
        name = entry.get("name")
        if not _nonblank_str(name):
            invalid_entries += 1
            continue
        if not name.startswith(name_prefix):
            continue                                  # unrelated non-prefix artifact
        candidates.append({"name": name, "raw": entry})
    return candidates, invalid_entries, True


def _candidate_meta(cand, dup_ids, dup_names):
    """Validate one candidate's required metadata. Returns
    (id_or_none, size_or_none, expired_or_none, meta_valid). Booleans are never ints;
    a duplicate id or name (computed over all candidates) invalidates the candidate."""
    raw, name = cand["raw"], cand["name"]
    aid = raw.get("id")
    exp = raw.get("expired")
    size = raw.get("size_in_bytes")
    id_ok = _is_pos_int(aid)
    exp_ok = isinstance(exp, bool)
    meta_valid = (id_ok and exp_ok
                  and (not id_ok or aid not in dup_ids)
                  and name not in dup_names)
    return (
        aid if id_ok else None,
        size if _is_nonneg_int(size) else None,
        exp if exp_ok else None,
        meta_valid,
    )


# --------------------------------------------------------------------------- #
# extraction association + atomic-result contract (non-expired candidates only)
# --------------------------------------------------------------------------- #
def _resolve_root(download_dir, name, layout_count):
    """Resolve a non-expired candidate's extraction root to (root, extraction_or_none).

    Encodes the observed ``actions/download-artifact`` layouts:
      * layout_count > 1  -> per-name subdirectory ``<dir>/<name>`` only;
      * layout_count == 1 -> the action's FLAT extraction (``<dir>``) is supported,
        and a named subdirectory is accepted when present; both present is ambiguous.
    An unsafe name (escapes the root) is refused with ``unsafe_name``; a symlinked
    named root is refused with ``unsafe_path`` (even if its target stays under the
    download dir). When extraction_or_none is set the caller records that anomaly."""
    child = _safe_child_dir(download_dir, name)
    if child is None:
        return None, EX_UNSAFE_NAME
    # A named extraction root that is a symlink is refused outright (never followed).
    if os.path.islink(child):
        return None, EX_UNSAFE_PATH
    if layout_count > 1:
        if not os.path.isdir(child):
            return None, EX_MISSING            # its download subdirectory is absent
        return child, None
    # Single downloaded artifact: support flat, accept a named subdirectory, reject both.
    flat_present = os.path.isfile(os.path.join(download_dir, SUMMARY_FILE))
    named_present = os.path.isdir(child)
    if flat_present and named_present:
        return None, EX_AMBIGUOUS
    if named_present:
        return child, None
    if flat_present:
        return download_dir, None              # flat root (the download dir itself)
    return None, EX_MISSING


def _collect_nonexpired(download_dir, name, layout_count):
    """Resolve + apply the summary contract for one non-expired candidate. Returns
    (extraction, summary_count, source_rel, reducer_value, is_value).

    is_value True -> reducer_value (the parsed JSON, ANY type) is a real contribution;
    is_value False -> the candidate is an anomaly and contributes a None sentinel."""
    root, anomaly = _resolve_root(download_dir, name, layout_count)
    if anomaly is not None:
        return anomaly, 0, None, None, False
    root_present, total, has_symlink = _scan_summaries(root)
    if has_symlink:
        # A root-level or nested summary.json that is a symlink is refused (never read).
        return EX_UNSAFE_PATH, total, None, None, False
    if total == 0:
        return EX_MISSING, 0, None, None, False
    if not root_present:
        return EX_NESTED_ONLY, total, None, None, False       # wrong path
    if total > 1:
        return EX_MULTIPLE, total, None, None, False
    # Prove the resolved summary path stays inside the artifact root AND the download
    # root before reading, so no normalisation/link trick reads outside either.
    root_file = os.path.join(root, SUMMARY_FILE)
    resolved = _within(root_file, root, download_dir)
    if resolved is None:
        return EX_UNSAFE_PATH, 1, None, None, False
    value, ok = _load_json_file(resolved)
    if not ok:
        return EX_MALFORMED_JSON, 1, None, None, False
    # Exactly one root-level summary.json, parsed. Pass the value UNCHANGED (the
    # reducer owns non-object / foreign / attempt classification).
    base = os.path.realpath(download_dir)
    rel = SUMMARY_FILE if os.path.realpath(root) == base else "%s/%s" % (name, SUMMARY_FILE)
    return EX_ONE, 1, rel, value, True


# --------------------------------------------------------------------------- #
# collection core (pure of network; reads only under download_dir)
# --------------------------------------------------------------------------- #
def collect(listing, download_dir, name_prefix=DEFAULT_NAME_PREFIX):
    """Turn a normalized artifacts listing + an extraction directory into
    (summaries, ledger_candidates, meta). ``summaries`` is the reducer input (parsed
    values and None sentinels); ``ledger_candidates`` is the per-candidate allowlisted
    audit; ``meta`` carries listing_ok, invalid_entry_count and the anomaly flag.

    Deterministic and order-independent: duplicate ids/names are detected by frequency
    (so both duplicates are flagged regardless of order) and candidates are sorted by
    artifact id."""
    # An explicitly empty or whitespace-only prefix is a configuration error: it would
    # otherwise match every artifact (or, after trimming, nothing). Fail closed rather
    # than silently over- or under-matching. (The default prefix is nonblank.)
    if not _nonblank_str(name_prefix):
        meta = {"listing_ok": True, "invalid_entry_count": 0, "candidate_anomalies": 0,
                "prefix_ok": False, "anomaly": True}
        return [None], [], meta

    candidates, invalid_entries, listing_ok = _discover(listing, name_prefix)

    # Frequency-based duplicate detection (symmetric -> order-independent).
    id_counts, name_counts = {}, {}
    for cand in candidates:
        aid = cand["raw"].get("id")
        if _is_pos_int(aid):
            id_counts[aid] = id_counts.get(aid, 0) + 1
        name_counts[cand["name"]] = name_counts.get(cand["name"], 0) + 1
    dup_ids = {k for k, n in id_counts.items() if n > 1}
    dup_names = {k for k, n in name_counts.items() if n > 1}

    # First pass: validate metadata and count the non-expired "download set" so the
    # single-vs-multiple extraction layout can be chosen exactly as the action would.
    resolved = []
    layout_count = 0
    for cand in candidates:
        aid, size, expired, meta_valid = _candidate_meta(cand, dup_ids, dup_names)
        resolved.append({"cand": cand, "id": aid, "size": size,
                         "expired": expired, "meta_valid": meta_valid})
        if meta_valid and expired is False:
            layout_count += 1

    ledger_candidates, summaries = [], []
    for r in resolved:
        name = r["cand"]["name"]
        if not r["meta_valid"]:
            extraction, count, src, value, is_value = EX_INVALID_META, 0, None, None, False
        elif r["expired"] is True:
            extraction, count, src, value, is_value = EX_EXPIRED, 0, None, None, False
        else:
            extraction, count, src, value, is_value = _collect_nonexpired(
                download_dir, name, layout_count)
        entry = {
            "artifact_id": r["id"],
            "artifact_name": name,
            "size_in_bytes": r["size"],
            "expired": r["expired"],
            "extraction": extraction,
            "summary_count": count,
            "source_path": src,
        }
        ledger_candidates.append(entry)
        if extraction == EX_EXPIRED:
            continue                              # no contribution -> naturally missing_result
        if is_value:
            summaries.append(value)               # parsed JSON, unchanged
        else:
            summaries.append(None)                # malformed sentinel -> fail closed

    # Structurally invalid listing entries and a non-list listing must also fail the
    # certification closed: one malformed sentinel each, never a silent drop.
    for _ in range(invalid_entries):
        summaries.append(None)
    if not listing_ok:
        summaries.append(None)

    # Deterministic candidate ordering: valid ids ascending, then invalid-id entries,
    # tie-broken by name then by a canonical serialization (total even with dup names).
    ledger_candidates.sort(key=lambda e: (
        (0, e["artifact_id"]) if isinstance(e["artifact_id"], int) else (1, 0),
        e["artifact_name"],
        json.dumps(e, sort_keys=True, ensure_ascii=False)))

    candidate_anomalies = sum(1 for e in ledger_candidates if e["extraction"] in _ANOMALY_EXTRACTIONS)
    anomaly = bool(candidate_anomalies or invalid_entries or not listing_ok)
    meta = {
        "listing_ok": listing_ok,
        "prefix_ok": True,
        "invalid_entry_count": invalid_entries,
        "candidate_anomalies": candidate_anomalies,
        "anomaly": anomaly,
    }
    return summaries, ledger_candidates, meta


def build_ledger(ledger_candidates, meta, current_run_attempt, name_prefix):
    """Assemble the sanitized ``pep-collection-ledger/1`` from allowlisted fields only.
    Contains no token, URL, raw REST object or summary body."""
    ingested = sum(1 for e in ledger_candidates if e["extraction"] == EX_ONE)
    expired = sum(1 for e in ledger_candidates if e["extraction"] == EX_EXPIRED)
    return {
        "schema": LEDGER_SCHEMA,
        "current_run_attempt": current_run_attempt,
        "name_prefix": name_prefix,
        "collection_status": "failed" if meta["anomaly"] else "ok",
        "listing_ok": meta["listing_ok"],
        "prefix_ok": meta.get("prefix_ok", True),
        "counts": {
            "candidates": len(ledger_candidates),
            "ingested": ingested,
            "expired": expired,
            "candidate_anomalies": meta["candidate_anomalies"],
            "invalid_entries": meta["invalid_entry_count"],
        },
        "candidates": ledger_candidates,
    }


# --------------------------------------------------------------------------- #
# atomic output
# --------------------------------------------------------------------------- #
def _quiet_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _write_atomic(path, text):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        _quiet_remove(tmp)
        raise


def _ledger_json(ledger):
    return json.dumps(ledger, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# CLI (no network)
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Collect atomic PEP result summaries into a cert-result/1 (offline, no network)")
    ap.add_argument("--plan", required=True, help="resolved pep-invocation-plan/1 JSON file")
    ap.add_argument("--artifacts-listing", required=True,
                    help="normalized run-artifacts listing JSON (array; built by the coordinator)")
    ap.add_argument("--download-dir", required=True,
                    help="directory actions/download-artifact extracted candidate ZIPs into")
    ap.add_argument("--current-run-attempt", required=True,
                    help="the live aggregation attempt (github.run_attempt); positive decimal string")
    ap.add_argument("--out", required=True, help="write the cert-result/1 here")
    ap.add_argument("--ledger-out", required=True, help="write the pep-collection-ledger/1 here")
    ap.add_argument("--name-prefix", default=DEFAULT_NAME_PREFIX,
                    help="coarse candidate name prefix (never parsed for identity)")
    args = ap.parse_args(argv)

    # Systemic CLI/configuration error: the cert-result and the ledger are two distinct
    # required documents and cannot share one destination. Reject BEFORE any collection
    # so neither output is misleadingly overwritten, and write nothing.
    try:
        same_dest = os.path.realpath(args.out) == os.path.realpath(args.ledger_out)
    except (ValueError, OSError):
        same_dest = args.out == args.ledger_out
    if same_dest:
        sys.stderr.write("refusing to write cert-result and ledger to the same destination\n")
        return 2

    plan, _plan_ok = _load_json_file(args.plan)           # None -> reducer fails closed
    listing, listing_read_ok = _load_json_file(args.artifacts_listing)
    if not listing_read_ok:
        listing = None                                    # unreadable/malformed -> not a list -> fail closed

    summaries, ledger_candidates, meta = collect(listing, args.download_dir, args.name_prefix)
    ledger = build_ledger(ledger_candidates, meta, args.current_run_attempt, args.name_prefix)

    result = CR.build_cert_result(plan, summaries, args.current_run_attempt)

    # Precise guarantee: once the arguments and output destinations are valid and local
    # collection has begun, BOTH documents are written (deterministically) before the
    # resolved/unresolved status is returned -- including for a malformed plan, listing
    # or summary, which still yield a ledger plus a fail-closed cert-result.
    _write_atomic(args.ledger_out, _ledger_json(ledger))
    _write_atomic(args.out, CR.to_json(result))
    return 0 if result.get("result_resolved") else 1


if __name__ == "__main__":
    sys.exit(main())
