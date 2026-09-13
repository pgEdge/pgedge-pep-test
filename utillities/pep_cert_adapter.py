"""Offline PEP evidence adapter (cert-plan input builder).

PURE and DETERMINISTIC: no GitHub API calls, no network, no rpm/dpkg execution,
no subprocess, no container inspection, no package downloading. This module
transforms ALREADY-CAPTURED release evidence into the structured envelope that
``pep_cert_plan.reduce()`` consumes.

Division of labour (the core rule): **the adapter measures and structures
evidence; the committed reducer decides what that evidence means.** So this
module never re-implements the reducer's build/publication/package-selection or
eligibility logic. In particular it does NOT decide latest-attempt-wins, job
deduplication/conflict, artifact ambiguity, target selection, identity or
eligibility — it only produces faithful, deterministic reducer input and lets
``reduce()`` do the rest.

Responsibilities:
  1. ``planned_cells_from_detector`` — detector RPM/DEB matrix -> planned_cells,
     using ONLY the explicit detector identity fields (never parsing cell_id,
     never copying legacy representative pg_version/pg_major into a decoupled
     cell). Every planned cell is preserved.
  2. ``job_records_from_jobs`` — captured Jobs-API records -> job_records via one
     exact producer-neutral marker ``[pep-cell:<cell_id>]`` (no project display
     templates). Unrelated jobs are ignored; malformed/duplicate/conflicting
     markers fail closed.
  3. ``artifact_records`` — captured artifact inventory (+ optional per-cell
     receipts) -> cell-associated artifact records, via one artifact-safe marker
     ``[pep-cell.<cell_id>]`` or a receipt keyed by immutable artifact id.
     Expired artifacts are treated as absent; conflicting associations fail
     closed; multiple live artifacts for one cell are emitted so the reducer
     marks the cell ambiguous. Pre-inspected ``members`` pass through verbatim.
  4. ``combine_pages`` — pure combiner for paginated captures; asserts the
     record count matches ``total_count`` and fails closed on missing/malformed
     pages or duplicate record ids (never a partial result).
  5. ``normalize_publication_results`` / ``assemble_reducer_input`` — normalize
     the family publication map and assemble the full reducer envelope. An
     optional manifest is audit context only and never overrides structured
     evidence.

Fail-closed discipline: this module raises exactly one exception type,
``AdapterError``, and only for evidence it cannot faithfully structure. It never
leaks a raw ``KeyError``/``TypeError`` for JSON-compatible input. Evidence the
reducer is designed to adjudicate (e.g. two jobs disagreeing for one cell) is
passed through, not pre-judged here.

Stdlib only. Unit-testable via ``pytest utillities/test_pep_cert_adapter.py``.
"""
from __future__ import annotations

import re

# Families mirrored from the reducer's shared vocabulary (kept local so the
# adapter has no import-time coupling to the reducer).
FAMILIES = ("rpm", "deb")

# Supported per-family publication outcomes. These are GitHub step/job outcomes,
# a closed set — not open-ended job conclusions — so validating against them
# surfaces caller bugs (typo'd keys, wrong values) instead of silently mislabelling
# a family as skipped. If the build side ever emits a richer vocabulary, extend
# this single constant.
PUBLICATION_RESULTS = ("success", "failure", "cancelled", "skipped")

# Producer-neutral cell markers. The JOB marker lives in a job DISPLAY NAME
# (colon is legal there). The ARTIFACT marker lives in a GitHub artifact NAME,
# where ':' is illegal, so an artifact-safe '.' form is used instead. Each is the
# ONE exact convention for its surface; nothing else is parsed.
_JOB_MARKER_RE = re.compile(r"\[pep-cell:([^\[\]]*)\]")
_ART_MARKER_RE = re.compile(r"\[pep-cell\.([^\[\]]*)\]")
_JOB_MARKER_HINT = "[pep-cell:"
_ART_MARKER_HINT = "[pep-cell."


class AdapterError(Exception):
    """Raised when captured evidence cannot be faithfully structured. Callers
    treat this as fail-closed: no partial plan is produced."""


# --- strict scalar validators (GitHub ids are positive ints; bool excluded) --
def _valid_id(x):
    """A captured GitHub id (job/artifact/receipt) must be a positive integer,
    with bool explicitly excluded (``True``/``False`` are ints in Python)."""
    return isinstance(x, int) and not isinstance(x, bool) and x >= 1


def _valid_count(x):
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def _id_set(ids):
    """Sanitize a caller-supplied planned_cell_ids into a hashable set of the
    nonblank string ids (a non-string cell id can never be a marker target).
    Total: a non-list raises AdapterError rather than a TypeError."""
    if not isinstance(ids, (list, tuple, set, frozenset)):
        raise AdapterError("planned_cell_ids must be a list/tuple/set")
    return {i for i in ids if isinstance(i, str) and i != ""}


# --- detector matrix -> planned_cells ---------------------------------------
def _include(matrix):
    """Return the include list of a detector matrix. A matrix MUST be either a
    bare list of entries or an object carrying a list ``include`` — an
    intentionally empty family is the explicit ``{"include": []}``. Anything else
    (a non-list ``include``, a missing key, or a non-list/non-dict matrix) is
    malformed and fails closed, so a malformed family can never silently reduce
    to zero cells and yield a resolved partial plan."""
    if isinstance(matrix, list):
        return matrix
    if isinstance(matrix, dict):
        if "include" not in matrix:
            raise AdapterError("detector matrix object is missing its 'include' list")
        inc = matrix["include"]
        if not isinstance(inc, list):
            raise AdapterError("detector matrix 'include' must be a list, got %r" % (type(inc).__name__,))
        return inc
    raise AdapterError("detector matrix must be a list or an object with an 'include' list, got %r"
                       % (type(matrix).__name__,))


def planned_cells_from_detector(*matrices):
    """Convert one or more detector matrix outputs into reducer ``planned_cells``.

    Consumes ONLY the explicit detector identity fields (cell_id, family, os,
    normalized_arch, pg_coupled, build_pg_major, build_pg_version). The cell_id
    is treated as opaque — never parsed to reconstruct other fields. The explicit
    pg_coupled / build_pg_major / build_pg_version values are copied VERBATIM
    (present keys only, wrong-typed or contradictory values included) so the
    reducer can validate them and fail closed; they are never rewritten into
    false/null. The legacy representative pg_version / pg_major are NEVER copied,
    so representative metadata cannot leak into build-PG identity.

    Every include entry yields exactly one planned cell (nothing is dropped),
    including cells that never ran or produced no artifact. A malformed entry is
    passed through unchanged so ``reduce()`` fails closed on it rather than the
    adapter silently discarding it. Total for JSON-compatible input.

    The adapter owns the artifact join key: each cell's ``artifact_name`` is set
    to its own ``cell_id`` (a stable, unique, artifact-safe per-cell value), so
    the reducer can match a cell's artifact record without any project-specific
    artifact name.
    """
    out = []
    for matrix in matrices:
        for e in _include(matrix):
            if not isinstance(e, dict):
                out.append(e)                     # malformed -> reducer flags "not_an_object"
                continue
            cid = e.get("cell_id")
            cell = {
                "cell_id": cid,
                "family": e.get("family"),
                "os": e.get("os"),
                "normalized_arch": e.get("normalized_arch"),
            }
            if isinstance(cid, str) and cid.strip() != "":
                cell["artifact_name"] = cid       # canonical per-cell artifact join key
            # Copy the explicit build-PG identity verbatim (present keys only). Never
            # synthesize false/null and never copy the legacy representative pg fields.
            for k in ("pg_coupled", "build_pg_major", "build_pg_version"):
                if k in e:
                    cell[k] = e[k]
            out.append(cell)
    return out


# --- jobs -> job_records ----------------------------------------------------
def _sole_marker(text, regex, hint, what):
    """Return the single cell_id carried by the exact marker in ``text``, or None
    when the marker hint is absent (an unrelated record).

    Fails closed if the marker is present but not exactly one clean, well-formed
    marker: every occurrence of the hint must complete into a matched marker, so
    a valid marker followed by an unmatched or malformed second marker fails.
    The cell id is used verbatim — a blank id, or one padded with (or containing)
    whitespace, is rejected rather than trimmed into validity."""
    if not isinstance(text, str):
        return None
    if hint not in text:
        return None                               # unrelated record
    markers = regex.findall(text)
    # Every hint occurrence must be exactly one complete, well-formed marker.
    if text.count(hint) != 1 or len(markers) != 1:
        raise AdapterError("malformed or duplicate %s marker: %r" % (what, text))
    cid = markers[0]
    if cid == "" or re.search(r"\s", cid):
        raise AdapterError("blank or whitespace-padded %s marker: %r" % (what, text))
    return cid


def job_records_from_jobs(jobs, planned_cell_ids):
    """Map captured GitHub Jobs-API records to reducer ``job_records``.

    Each job is associated to a cell by the exact ``[pep-cell:<cell_id>]`` marker
    in its display name. Jobs with no marker are ignored (unrelated); a marker
    for a cell that is not planned is ignored. A malformed, blank or
    multiple/conflicting marker fails closed, and a job bound to a planned cell
    must carry a valid immutable id (positive integer, bool excluded). The job's
    ``id``, ``run_attempt``, ``status`` and ``conclusion`` are preserved verbatim;
    the reducer owns deduplication, conflict and latest-attempt semantics.
    """
    if not isinstance(jobs, list):
        raise AdapterError("jobs must be a list")
    planned = _id_set(planned_cell_ids)
    out = []
    for j in jobs:
        if not isinstance(j, dict):
            raise AdapterError("job record is not an object: %r" % (j,))
        cid = _sole_marker(j.get("name"), _JOB_MARKER_RE, _JOB_MARKER_HINT, "job name")
        if cid is None or cid not in planned:
            continue
        if not _valid_id(j.get("id")):
            raise AdapterError("job associated to cell %r has an invalid id: %r" % (cid, j.get("id")))
        out.append({
            "cell_id": cid,
            "job_id": j.get("id"),
            "run_attempt": j.get("run_attempt"),
            "status": j.get("status"),
            "conclusion": j.get("conclusion"),
        })
    return out


# --- artifacts -> artifact records ------------------------------------------
def _receipt_map(receipts):
    """Build an artifact_id -> cell_id map from per-cell receipts, failing closed
    on a malformed receipt (non-object, invalid/absent artifact_id, blank or
    whitespace-padded cell_id) or two receipts binding one artifact id to
    different cells. The artifact_id must be a positive integer (bool excluded);
    the cell_id is used verbatim (never trimmed)."""
    if receipts is None:
        return {}
    if not isinstance(receipts, list):
        raise AdapterError("receipts must be a list")
    out = {}
    for r in receipts:
        if not isinstance(r, dict):
            raise AdapterError("receipt is not an object: %r" % (r,))
        aid = r.get("artifact_id")
        cid = r.get("cell_id")
        if not _valid_id(aid):
            raise AdapterError("receipt has an invalid artifact_id: %r" % (aid,))
        if not (isinstance(cid, str) and cid != "" and not re.search(r"\s", cid)):
            raise AdapterError("receipt has a blank/padded cell_id: %r" % (cid,))
        if aid in out and out[aid] != cid:
            raise AdapterError("conflicting receipts for artifact id %r: %r vs %r"
                               % (aid, out[aid], cid))
        out[aid] = cid
    return out


def artifact_records(artifact_inventory, planned_cell_ids, receipts=None):
    """Convert captured artifact inventory (+ optional per-cell receipts) into
    reducer artifact records associated to planned cells.

    Association uses ONE of two isolated conventions: the artifact-safe name
    marker ``[pep-cell.<cell_id>]``, or a receipt that references the artifact by
    its immutable id. If both are present they must agree; an artifact associated
    to two different cells fails closed. An artifact with neither is ignored
    (unrelated), as is an association to a cell that is not planned.

    An artifact associated to a planned cell (via either convention) MUST carry
    trustworthy evidence, or it fails closed so it can never become an
    available/eligible artifact: a valid immutable id (positive integer, bool
    excluded); a nonblank string source name (preserved exactly, never trimmed);
    and an EXPLICIT boolean ``expired`` — ``true`` means absent (no record),
    ``false`` means usable, and a missing/null/string/numeric expiry fails
    closed. Unrelated artifacts (no marker, no receipt) are ignored regardless of
    these fields.

    ``members`` (the pre-inspected package members) pass through verbatim; no
    download or RPM/DEB inspection happens here. Two DISTINCT live artifacts for
    one cell are emitted as two records so the reducer marks that cell ambiguous
    — the adapter does not itself resolve the ambiguity. The raw source artifact
    name is retained as inert ``source_artifact_name`` audit metadata, separate
    from the canonical reducer join key ``name``.

    Each emitted record's ``name`` is the cell's canonical artifact join key
    (its cell_id), matching the planned cell's ``artifact_name``.
    """
    if not isinstance(artifact_inventory, list):
        raise AdapterError("artifact_inventory must be a list")
    planned = _id_set(planned_cell_ids)
    receipt_cell = _receipt_map(receipts)
    out = []
    for a in artifact_inventory:
        if not isinstance(a, dict):
            raise AdapterError("artifact inventory entry is not an object: %r" % (a,))
        aid = a.get("id")
        cids = set()
        marker_cid = _sole_marker(a.get("name"), _ART_MARKER_RE, _ART_MARKER_HINT, "artifact name")
        if marker_cid is not None:
            cids.add(marker_cid)
        if _valid_id(aid) and aid in receipt_cell:   # only a valid id can match a receipt
            cids.add(receipt_cell[aid])
        if not cids:
            continue                              # unrelated artifact
        if len(cids) != 1:
            raise AdapterError("artifact maps to multiple cells: %s" % (sorted(cids),))
        cid = next(iter(cids))
        if cid not in planned:
            continue                              # association to a non-planned cell
        # An artifact bound to a planned cell must carry trustworthy evidence:
        # a valid immutable id; a nonblank string source name (preserved exactly,
        # never trimmed); and an EXPLICIT boolean expiry. Anything malformed fails
        # closed rather than becoming a usable/eligible artifact.
        if not _valid_id(aid):
            raise AdapterError("artifact associated to cell %r has an invalid id: %r" % (cid, aid))
        src_name = a.get("name")
        if not (isinstance(src_name, str) and src_name.strip() != ""):
            raise AdapterError("artifact associated to cell %r has a blank/missing source name: %r"
                               % (cid, src_name))
        expired = a.get("expired")
        if not isinstance(expired, bool):
            raise AdapterError("artifact associated to cell %r has a non-boolean 'expired': %r"
                               % (cid, expired))
        if expired:
            continue                              # expired -> absent
        rec = {"name": cid, "id": aid, "source_artifact_name": src_name}
        if "members" in a:
            rec["members"] = a.get("members")     # pre-inspected members, verbatim
        out.append(rec)
    return out


# --- pagination -------------------------------------------------------------
def combine_pages(pages, items_key):
    """Combine paginated GitHub list responses into one list of records.

    ``pages`` is a non-empty list of page objects, each shaped like
    ``{"total_count": N, "<items_key>": [...]}`` (e.g. items_key="jobs" or
    "artifacts"). Fails closed — never returns a partial list — on a
    missing/malformed page, an inconsistent or unmet ``total_count`` (missing
    pages), a non-object record, a record with a missing/invalid id, or a
    duplicate record id across the combined result. Every returned record is an
    object with a valid positive-integer id.
    """
    if not (isinstance(items_key, str) and items_key.strip() != ""):
        raise AdapterError("items_key must be a nonblank string")   # before any dict lookup
    if not isinstance(pages, list) or not pages:
        raise AdapterError("pages must be a non-empty list")
    combined = []
    totals = set()
    for idx, p in enumerate(pages):
        if not isinstance(p, dict):
            raise AdapterError("page %d is not an object" % idx)
        tc = p.get("total_count")
        if not _valid_count(tc):
            raise AdapterError("page %d has an invalid total_count: %r" % (idx, tc))
        totals.add(tc)
        items = p.get(items_key)
        if not isinstance(items, list):
            raise AdapterError("page %d is missing list %r" % (idx, items_key))
        combined.extend(items)
    if len(totals) != 1:
        raise AdapterError("inconsistent total_count across pages: %s" % sorted(totals))
    total = next(iter(totals))
    if len(combined) != total:
        raise AdapterError("combined record count %d != total_count %d (missing/extra pages)"
                           % (len(combined), total))
    seen = set()
    for it in combined:
        if not isinstance(it, dict):
            raise AdapterError("combined record is not an object: %r" % (it,))
        rid = it.get("id")
        if not _valid_id(rid):
            raise AdapterError("combined record has an invalid id: %r" % (rid,))
        if rid in seen:
            raise AdapterError("duplicate record id across pages: %r" % (rid,))
        seen.add(rid)
    return combined


# --- publication + envelope -------------------------------------------------
def normalize_publication_results(raw):
    """Validate and return the family -> result publication map for the reducer.

    Requires an object. Every key must be a supported family (``rpm``/``deb``) and
    every value a supported outcome (see ``PUBLICATION_RESULTS``); an accidental
    key or a malformed value fails closed rather than being silently dropped or
    hidden as a skipped family. A missing family key is left absent and remains
    valid — the reducer treats an absent family as skipped."""
    if not isinstance(raw, dict):
        raise AdapterError("publication_results must be an object")
    out = {}
    for k, v in raw.items():
        if k not in FAMILIES:
            raise AdapterError("publication_results has an unsupported family key: %r" % (k,))
        if v not in PUBLICATION_RESULTS:
            raise AdapterError("publication_results[%r] has an unsupported result: %r" % (k, v))
        out[k] = v
    return out


def assemble_reducer_input(*, planned_cells, job_records, artifacts,
                           publication_results, release_intent, component_policy,
                           provenance, manifest=None):
    """Assemble the complete ``reduce()`` envelope from already-structured pieces.

    The publication map is normalized to the reducer's family vocabulary. The
    manifest, when provided, is attached as inert audit context only (under a key
    the reducer ignores) and never overrides detector cells, job results,
    artifact inventory, package observations or publication results.
    """
    env = {
        "planned_cells": planned_cells,
        "job_records": job_records,
        "artifacts": artifacts,
        "publication_results": normalize_publication_results(publication_results),
        "release_intent": release_intent,
        "component_policy": component_policy,
        "provenance": provenance,
    }
    if manifest is not None:
        env["audit"] = {"manifest": manifest}     # inert: reduce() consumes only known keys
    return env
