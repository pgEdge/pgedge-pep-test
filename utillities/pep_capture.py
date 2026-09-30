"""Offline capture-verification layer (capture-evidence/1 + reducer input builder).

PURE and DETERMINISTIC: no GitHub API calls, no network, no clock, no environment
reads, no GitHub context. This module INDEPENDENTLY re-verifies a workflow run's
already-downloaded release evidence and then assembles the structured envelope that
``pep_cert_plan.reduce()`` consumes. Downloading, redirect handling and credential
management belong to a later I/O shell (Slice 1d); this module only ever reads local
ZIP bytes it is handed (as filesystem paths or seekable file objects).

Division of labour: the receipt action already emitted a ``pep-receipt/2`` binding a
build cell's packages to the immutable package artifact GitHub recorded. This module
CLOSES that loop: it hashes the receipt and package archives against GitHub's own API
digests, binds each receipt to its package artifact by immutable id and canonical
digest, recomputes every member checksum from the downloaded bytes, and re-inspects
every package with the committed ``pep_pkg_inspect`` implementation so the package's
own header identity must equal the receipt. Only fully verified evidence becomes a
reducer artifact record; the committed reducer still decides what that evidence means.

Trust and failure model (fail closed throughout):
  * ``CaptureSystemError`` — an unusable GLOBAL input or an inconsistency that prevents
    a reliable run-wide capture (bad pagination, undecodable detector matrices, a live
    referenced artifact whose bytes were never provided, malformed publication map or
    provenance), OR a local capture I/O failure (unusable temp root, unreadable archive,
    extraction-write or cleanup fault) — the latter sanitized to a fixed message that never
    carries a local/temporary path. It propagates and no plan is produced.
  * ``EvidenceError`` — a CELL-LOCAL receipt/package rejection, carrying a STABLE closed
    ``code`` (see ``REJECTION_CODES``) plus bounded, sanitized ``detail``. The collector
    catches it per planned cell; downstream branches on ``code`` only, NEVER on message
    text. A rejected or conflicting cell yields NO artifact record, so the unchanged
    reducer reports it as ``incomplete``/``never_ran`` (per job evidence) and never as
    eligible; capture-evidence retains the more precise rejection/ambiguity.

Multiplicity is treated as UNTRUSTED: the complete artifact inventory is adversarial
input, so a cell with more than one live receipt artifact is ambiguous and suppresses
all candidates. GitHub's own rejection of duplicate artifact names may make this rare
but is never RELIED ON for correctness.

Streaming discipline: raw archives are hashed incrementally; ZIP members are hashed and
extracted one at a time through bounded reads (never ``extractall``, never a whole-file
read). Package header inspection materializes one already-validated member in a fresh
private temporary directory. Cleanup is required before a successful capture can complete
and is attempted without masking an existing failure. No artifact data is accumulated
across cells, and no token, URL, query string, signed redirect, Authorization value or
temporary path is ever placed in the reducer envelope, the capture evidence, or a
persisted output.

Stdlib only (plus the committed ``pep_pkg_inspect`` and ``pep_cert_adapter``).
Unit-testable via ``pytest utillities/test_pep_capture.py``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
import zlib

import pep_cert_adapter as A
import pep_pkg_inspect as I

RECEIPT_SCHEMA = "pep-receipt/2"
CAPTURE_SCHEMA = "capture-evidence/1"
RECEIPT_FILENAME = "receipt.json"
RECEIPT_ARTIFACT_PREFIX = "pep-receipt-"

# The receipt's exact top-level and member key sets (a receipt is REQUIRED to carry
# exactly these; a member is a complete pep-members/1 record).
_RECEIPT_TOP_KEYS = frozenset(
    ("schema", "cell_id", "artifact_id", "artifact_name", "archive_digest", "members"))
_MEMBER_KEYS = frozenset(I._MEMBER_ORDER)

_HEX_LC = frozenset("0123456789abcdef")

_CHUNK = 1024 * 1024                 # streaming read granularity (bounded memory)
_MAX_RECEIPT_BYTES = 1024 * 1024     # a receipt.json is tiny; cap defensively (zip-bomb guard)
_MAX_DETAIL = 200                    # bounded, sanitized EvidenceError detail

# --- stable closed rejection-code vocabulary --------------------------------
# Machine-stable codes ONLY. Downstream decisions branch on these, never on the
# human-readable ``detail`` string.
RECEIPT_ZIP_UNSAFE = "RECEIPT_ZIP_UNSAFE"
RECEIPT_ZIP_NOT_SINGLE = "RECEIPT_ZIP_NOT_SINGLE"
RECEIPT_ARCHIVE_DIGEST_MISMATCH = "RECEIPT_ARCHIVE_DIGEST_MISMATCH"
RECEIPT_JSON_MALFORMED = "RECEIPT_JSON_MALFORMED"
RECEIPT_SCHEMA_INVALID = "RECEIPT_SCHEMA_INVALID"
RECEIPT_FIELD_INVALID = "RECEIPT_FIELD_INVALID"
RECEIPT_EXPIRY_MALFORMED = "RECEIPT_EXPIRY_MALFORMED"
PACKAGE_ARTIFACT_ABSENT = "PACKAGE_ARTIFACT_ABSENT"
PACKAGE_ARTIFACT_EXPIRED = "PACKAGE_ARTIFACT_EXPIRED"
PACKAGE_ARCHIVE_DIGEST_MISMATCH = "PACKAGE_ARCHIVE_DIGEST_MISMATCH"
PACKAGE_ZIP_UNSAFE = "PACKAGE_ZIP_UNSAFE"
PACKAGE_MEMBER_SET_MISMATCH = "PACKAGE_MEMBER_SET_MISMATCH"
PACKAGE_BINDING_MISMATCH = "PACKAGE_BINDING_MISMATCH"
MEMBER_SHA_MISMATCH = "MEMBER_SHA_MISMATCH"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
CELL_AMBIGUOUS_ASSOCIATIONS = "CELL_AMBIGUOUS_ASSOCIATIONS"

REJECTION_CODES = frozenset((
    RECEIPT_ZIP_UNSAFE, RECEIPT_ZIP_NOT_SINGLE, RECEIPT_ARCHIVE_DIGEST_MISMATCH,
    RECEIPT_JSON_MALFORMED, RECEIPT_SCHEMA_INVALID, RECEIPT_FIELD_INVALID,
    RECEIPT_EXPIRY_MALFORMED,
    PACKAGE_ARTIFACT_ABSENT, PACKAGE_ARTIFACT_EXPIRED, PACKAGE_ARCHIVE_DIGEST_MISMATCH,
    PACKAGE_ZIP_UNSAFE, PACKAGE_MEMBER_SET_MISMATCH, PACKAGE_BINDING_MISMATCH,
    MEMBER_SHA_MISMATCH, IDENTITY_MISMATCH, CELL_AMBIGUOUS_ASSOCIATIONS,
))

# Untrusted-ZIP member read failures (CRC-corrupted / truncated / encrypted /
# unsupported compression / bad zlib stream) are normalized into a cell-local
# EvidenceError rather than escaping raw. OSError is deliberately EXCLUDED so a
# genuine local disk fault (e.g. a full extraction disk) is not mislabelled as
# untrusted evidence.
_ZIP_MEMBER_ERRORS = (zipfile.BadZipFile, RuntimeError, NotImplementedError,
                      EOFError, zlib.error)

# Verdict vocabulary for a per-planned-cell capture result.
VERDICTS = ("accepted", "rejected", "ambiguous", "absent")


class CaptureSystemError(Exception):
    """A systemic condition that prevents a reliable run-wide capture. Propagates;
    no plan is produced. Carries only a bounded, credential-free message."""

    def __init__(self, detail=""):
        self.detail = _bounded(detail)
        super().__init__(self.detail)


class EvidenceError(Exception):
    """A cell-local receipt/package rejection with a STABLE closed ``code`` and a
    bounded, sanitized ``detail``. The collector catches this per planned cell."""

    def __init__(self, code, detail=""):
        if code not in REJECTION_CODES:
            raise ValueError("unknown rejection code: %r" % (code,))   # programming guard
        self.code = code
        self.detail = _bounded(detail)
        super().__init__("%s: %s" % (code, self.detail))


# --- small, total helpers ---------------------------------------------------
def _bounded(detail):
    """Sanitize a human detail string: printable ASCII/uni only, length-capped.
    NEVER carries a URL, token, temp path or Authorization value by construction —
    callers pass only codes, ids, member paths and fixed phrases."""
    if not isinstance(detail, str):
        detail = str(detail)
    detail = "".join(ch for ch in detail if ch >= " " and ch != "\x7f")
    return detail[:_MAX_DETAIL]


def _is_pos_int(x):
    return isinstance(x, int) and not isinstance(x, bool) and x >= 1


def _is_nonneg_int(x):
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def _nonblank_unpadded_str(x):
    return isinstance(x, str) and x != "" and x.strip() == x


def _canonical_hex(x):
    """A canonical bare lowercase 64-hex SHA-256 (no prefix, no padding). Used for
    a RECEIPT's own digest/sha fields, which must ALREADY be canonical — never
    normalized-into-acceptance."""
    return isinstance(x, str) and len(x) == 64 and set(x) <= _HEX_LC


def normalize_api_digest(value):
    """Normalize a GitHub API artifact digest to bare lowercase 64-hex, stripping a
    single optional ``sha256:`` prefix (API digests only). Returns ``None`` for any
    malformed/absent value (callers decide the code). NEVER applied to receipt fields."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.lower().startswith("sha256:"):
        s = s[len("sha256:"):].strip()
    s = s.lower()
    return s if (len(s) == 64 and set(s) <= _HEX_LC) else None


# --- strict pep-receipt/2 parsing -------------------------------------------
def _strict_json(raw):
    """Parse trusted-shape-but-untrusted-content JSON bytes fail-closed: strict UTF-8,
    duplicate keys rejected, NaN/Infinity rejected. Raises ``EvidenceError`` with
    ``RECEIPT_JSON_MALFORMED`` on any problem (never a bare ValueError)."""
    if not isinstance(raw, (bytes, bytearray)):
        raise EvidenceError(RECEIPT_JSON_MALFORMED, "receipt bytes required")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise EvidenceError(RECEIPT_JSON_MALFORMED, "receipt is not valid UTF-8")

    def _no_dup(pairs):
        seen = set()
        for k, _v in pairs:
            if k in seen:
                raise EvidenceError(RECEIPT_JSON_MALFORMED, "duplicate JSON key")
            seen.add(k)
        return dict(pairs)

    def _bad_const(_tok):
        raise EvidenceError(RECEIPT_JSON_MALFORMED, "non-finite JSON constant")

    try:
        return json.loads(text, object_pairs_hook=_no_dup, parse_constant=_bad_const)
    except EvidenceError:
        raise
    except (ValueError, RecursionError):
        raise EvidenceError(RECEIPT_JSON_MALFORMED, "not valid JSON")


def _validate_receipt_member(m):
    """Fail-closed validation of one receipt member as a complete pep-members/1 record.
    Reuses the inspector's flat-path rule so capture and inspection agree exactly."""
    if not isinstance(m, dict):
        raise EvidenceError(RECEIPT_FIELD_INVALID, "member is not an object")
    if frozenset(m) != _MEMBER_KEYS:
        raise EvidenceError(RECEIPT_FIELD_INVALID, "member has an unexpected key set")
    if not _nonblank_unpadded_str(m["package_name"]):
        raise EvidenceError(RECEIPT_FIELD_INVALID, "member package_name invalid")
    ep = m["epoch"]
    if not (ep is None or _is_nonneg_int(ep)):
        raise EvidenceError(RECEIPT_FIELD_INVALID, "member epoch invalid")
    if not _nonblank_unpadded_str(m["version"]):
        raise EvidenceError(RECEIPT_FIELD_INVALID, "member version invalid")
    if not isinstance(m["release"], str):
        raise EvidenceError(RECEIPT_FIELD_INVALID, "member release invalid")
    if not _nonblank_unpadded_str(m["native_arch"]):
        raise EvidenceError(RECEIPT_FIELD_INVALID, "member native_arch invalid")
    if m["package_class"] not in I._VALID_CLASSES:
        raise EvidenceError(RECEIPT_FIELD_INVALID, "member package_class invalid")
    if not _canonical_hex(m["sha256"]):
        raise EvidenceError(RECEIPT_FIELD_INVALID, "member sha256 not canonical 64-hex")
    try:
        I.validate_member_path(m["artifact_member_path"])
    except I.InspectError:
        raise EvidenceError(RECEIPT_FIELD_INVALID, "member path is not a safe flat filename")


def strict_parse_receipt(raw):
    """Parse and STRICTLY validate ``pep-receipt/2`` bytes into a dict, or raise a
    cell-local ``EvidenceError``. Exact top-level and member key sets; positive-integer
    ``artifact_id`` (bool excluded); nonblank/unpadded ``cell_id``/``artifact_name``;
    canonical bare-lowercase-64-hex ``archive_digest`` and member SHAs (never
    prefix-normalized); nonempty members; valid identity/class/flat-path per member;
    duplicate or case-colliding member paths rejected."""
    obj = _strict_json(raw)
    if not isinstance(obj, dict):
        raise EvidenceError(RECEIPT_JSON_MALFORMED, "receipt must be a JSON object")
    if frozenset(obj) != _RECEIPT_TOP_KEYS:
        raise EvidenceError(RECEIPT_SCHEMA_INVALID, "unexpected top-level key set")
    if obj["schema"] != RECEIPT_SCHEMA:
        raise EvidenceError(RECEIPT_SCHEMA_INVALID, "wrong schema constant")
    if not _nonblank_unpadded_str(obj["cell_id"]):
        raise EvidenceError(RECEIPT_FIELD_INVALID, "cell_id invalid")
    if not _is_pos_int(obj["artifact_id"]):
        raise EvidenceError(RECEIPT_FIELD_INVALID, "artifact_id must be a positive integer")
    if not _nonblank_unpadded_str(obj["artifact_name"]):
        raise EvidenceError(RECEIPT_FIELD_INVALID, "artifact_name invalid")
    if not _canonical_hex(obj["archive_digest"]):
        raise EvidenceError(RECEIPT_FIELD_INVALID, "archive_digest not canonical 64-hex")
    members = obj["members"]
    if not isinstance(members, list) or not members:
        raise EvidenceError(RECEIPT_FIELD_INVALID, "members must be a nonempty list")
    seen, seen_lower = set(), set()
    for m in members:
        _validate_receipt_member(m)
        p = m["artifact_member_path"]
        if p in seen:
            raise EvidenceError(RECEIPT_FIELD_INVALID, "duplicate member path")
        if p.lower() in seen_lower:
            raise EvidenceError(RECEIPT_FIELD_INVALID, "case-colliding member path")
        seen.add(p)
        seen_lower.add(p.lower())
    return obj


# --- streaming ZIP verification ---------------------------------------------
def archive_sha256(src):
    """Incremental SHA-256 of a raw archive's bytes. ``src`` is a filesystem path or a
    seekable binary file object. Reads in bounded ``_CHUNK`` slices — NEVER a whole-file
    read — so a large archive never loads into memory."""
    h = hashlib.sha256()
    if hasattr(src, "read"):
        try:
            src.seek(0)
        except (OSError, ValueError):
            pass
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    else:
        with open(src, "rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest()


def _open_zip(src, unsafe_code):
    # A ``BadZipFile`` is untrusted CONTENT (the bytes are not a valid archive) -> cell-local.
    # An ``OSError`` here is a local read/infrastructure fault (unreadable/vanished blob) ->
    # it is NOT caught, so it propagates to the public boundary as a CaptureSystemError.
    try:
        return zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise EvidenceError(unsafe_code, "not a valid ZIP archive")


def safe_entries(zf, unsafe_code):
    """Return the sorted list of a ZIP's entry names after rejecting every unsafe shape:
    directories (trailing slash or dir mode), symlinks, absolute/backslash/nested/traversal
    paths, ``.``/``..``, NUL, duplicate entries and case-insensitive collisions. v1 accepts
    only safe FLAT root filenames. Raises ``EvidenceError(unsafe_code, ...)``."""
    names, seen, seen_lower = [], set(), set()
    for zi in zf.infolist():
        name = zi.filename
        if not isinstance(name, str) or name == "" or "\x00" in name:
            raise EvidenceError(unsafe_code, "invalid entry name")
        mode = (zi.external_attr >> 16) & 0xFFFF
        if name.endswith("/") or zi.is_dir() or stat.S_ISDIR(mode):
            raise EvidenceError(unsafe_code, "directory entry not allowed")
        if stat.S_ISLNK(mode):
            raise EvidenceError(unsafe_code, "symlink entry not allowed")
        if name.startswith("/") or "\\" in name or "/" in name:
            raise EvidenceError(unsafe_code, "non-flat entry path not allowed")
        if name in (".", ".."):
            raise EvidenceError(unsafe_code, "dot entry not allowed")
        if name in seen:
            raise EvidenceError(unsafe_code, "duplicate entry")
        if name.lower() in seen_lower:
            raise EvidenceError(unsafe_code, "case-colliding entry")
        seen.add(name)
        seen_lower.add(name.lower())
        names.append(name)
    return sorted(names)


def _pkg_ceiling():
    """The single package-member expansion ceiling: the inspector's OWN existing ceiling,
    reused (never a second hardcoded platform-specific limit). Read at call time so a test
    can lower it. Bounds both member hashing and extraction so a zip-bomb member cannot be
    fully hashed or written before the inspector would have rejected it."""
    return I._MAX_PKG_BYTES


def member_sha256(zf, name, unsafe_code, max_bytes=None):
    """Incremental SHA-256 of ONE ZIP member's uncompressed bytes, streamed through the
    member's own reader — never ``zf.read(name)`` (which would buffer the whole member).
    Bounded by ``max_bytes`` (the inspector ceiling) so an over-large member is rejected
    mid-stream. Any untrusted-ZIP read failure becomes ``EvidenceError(unsafe_code, ...)``."""
    h = hashlib.sha256()
    total = 0
    try:
        with zf.open(name, "r") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise EvidenceError(unsafe_code, "member exceeds size ceiling: %s" % (name,))
                h.update(chunk)
    except _ZIP_MEMBER_ERRORS:
        raise EvidenceError(unsafe_code, "unreadable ZIP member: %s" % (name,))
    return h.hexdigest()


def extract_member(zf, name, dst_dir, unsafe_code, max_bytes=None):
    """Stream ONE already-validated ZIP member to a fresh file under ``dst_dir`` (never
    ``extractall``), bounded by ``max_bytes``. ``name`` is a safe flat filename, so the
    destination basename equals it. Any untrusted-ZIP read failure becomes
    ``EvidenceError(unsafe_code, ...)``; a genuine write/OS fault propagates. Returns the
    written path."""
    dst = os.path.join(dst_dir, os.path.basename(name))
    total = 0
    with open(dst, "wb") as out:
        try:
            member = zf.open(name, "r")
        except _ZIP_MEMBER_ERRORS:
            raise EvidenceError(unsafe_code, "unreadable ZIP member: %s" % (name,))
        with member as src:
            while True:
                try:
                    chunk = src.read(_CHUNK)
                except _ZIP_MEMBER_ERRORS:
                    raise EvidenceError(unsafe_code, "unreadable ZIP member: %s" % (name,))
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise EvidenceError(unsafe_code, "member exceeds size ceiling: %s" % (name,))
                out.write(chunk)
    return dst


def _read_bounded_member(zf, name, cap, unsafe_code):
    """Read one small ZIP member (the receipt.json) fully but with a hard byte ceiling,
    streamed. Exceeding ``cap`` fails as ``RECEIPT_JSON_MALFORMED``; any untrusted-ZIP read
    failure becomes ``EvidenceError(unsafe_code, ...)`` (kept receipt-local by the caller)."""
    data = bytearray()
    try:
        with zf.open(name, "r") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > cap:
                    raise EvidenceError(RECEIPT_JSON_MALFORMED, "receipt.json exceeds size ceiling")
    except _ZIP_MEMBER_ERRORS:
        raise EvidenceError(unsafe_code, "unreadable ZIP member: %s" % (name,))
    return bytes(data)


def verify_receipt_artifact(receipt_src, receipt_api_digest):
    """Verify a downloaded RECEIPT artifact ZIP and return its strictly-parsed receipt.

    Binds the raw archive to GitHub's API digest, requires exactly one safe root
    ``receipt.json``, and strictly parses it. ``receipt_src`` is a path or seekable."""
    norm = normalize_api_digest(receipt_api_digest)
    if norm is None:
        raise EvidenceError(RECEIPT_ARCHIVE_DIGEST_MISMATCH, "malformed receipt API digest")
    if archive_sha256(receipt_src) != norm:
        raise EvidenceError(RECEIPT_ARCHIVE_DIGEST_MISMATCH, "receipt archive hash != API digest")
    with _open_zip(receipt_src, RECEIPT_ZIP_UNSAFE) as zf:
        names = safe_entries(zf, RECEIPT_ZIP_UNSAFE)
        if names != [RECEIPT_FILENAME]:
            raise EvidenceError(RECEIPT_ZIP_NOT_SINGLE, "receipt ZIP is not exactly one receipt.json")
        raw = _read_bounded_member(zf, RECEIPT_FILENAME, _MAX_RECEIPT_BYTES, RECEIPT_ZIP_UNSAFE)
    return strict_parse_receipt(raw)


def verify_package_artifact(pkg_src, pkg_api_digest, receipt):
    """Verify a downloaded PACKAGE artifact ZIP against a verified receipt (no inspection).

    Binds the raw archive to GitHub's API digest AND to the receipt's canonical
    ``archive_digest``; requires the ZIP's safe entry set to equal the receipt member
    paths EXACTLY; and recomputes every member SHA from the downloaded bytes. Raises a
    cell-local ``EvidenceError`` on any mismatch."""
    norm = normalize_api_digest(pkg_api_digest)
    if norm is None:
        raise EvidenceError(PACKAGE_ARCHIVE_DIGEST_MISMATCH, "malformed package API digest")
    if norm != receipt["archive_digest"]:
        raise EvidenceError(PACKAGE_ARCHIVE_DIGEST_MISMATCH, "package API digest != receipt archive_digest")
    if archive_sha256(pkg_src) != norm:
        raise EvidenceError(PACKAGE_ARCHIVE_DIGEST_MISMATCH, "package archive hash != API digest")
    by_path = {m["artifact_member_path"]: m for m in receipt["members"]}
    with _open_zip(pkg_src, PACKAGE_ZIP_UNSAFE) as zf:
        names = safe_entries(zf, PACKAGE_ZIP_UNSAFE)
        if names != sorted(by_path):
            raise EvidenceError(PACKAGE_MEMBER_SET_MISMATCH, "package entries != receipt member paths")
        for name in names:
            got = member_sha256(zf, name, PACKAGE_ZIP_UNSAFE, max_bytes=_pkg_ceiling())
            if got != by_path[name]["sha256"]:
                raise EvidenceError(MEMBER_SHA_MISMATCH, "member sha differs: %s" % (name,))


def reinspect_and_require_identity(pkg_src, receipt, expected_family, tmp_root=None):
    """Re-inspect every package member through the committed ``pep_pkg_inspect`` and require
    the produced pep-members/1 record to EQUAL the receipt member exactly (identity from
    headers, not filenames). Each member is materialized alone into a fresh temp dir.
    Cleanup must complete before a successful capture can return. While another failure is
    already propagating, cleanup is best-effort so it does not mask the original rejection.
    ``expected_family`` enforces the planned family when known (``I.RPM``/``I.DEB``), else
    auto-detects."""
    # SLICE 1d CARRY-FORWARD: the workflow shell MUST preflight that the required rpm /
    # dpkg-deb executables exist before capture. Missing host tooling is an INFRASTRUCTURE
    # failure; because this pass does not redesign pep_pkg_inspect, a genuinely missing tool
    # would surface here as an InspectError -> IDENTITY_MISMATCH. The shell preflight prevents
    # infrastructure absence from ever being reported as a package identity mismatch.
    by_path = {m["artifact_member_path"]: m for m in receipt["members"]}
    with _open_zip(pkg_src, PACKAGE_ZIP_UNSAFE) as zf:
        for mp in sorted(by_path):
            # mkdtemp OSError (e.g. an unusable tmp_root) is a local I/O fault -> propagates
            # to the public boundary as CaptureSystemError.
            d = tempfile.mkdtemp(prefix="pep-capture-", dir=tmp_root)
            ok = False
            try:
                fpath = extract_member(zf, mp, d, PACKAGE_ZIP_UNSAFE, max_bytes=_pkg_ceiling())
                try:
                    got = I.inspect_package(fpath, mp, expected_family=expected_family)
                except I.InspectError:
                    # NEVER surface the raw InspectError text: it can embed the temporary
                    # extraction path. Emit a bounded fixed reason + the safe member path.
                    raise EvidenceError(IDENTITY_MISMATCH, "re-inspection failed for member: %s" % (mp,))
                if got != by_path[mp]:
                    raise EvidenceError(IDENTITY_MISMATCH, "member identity differs: %s" % (mp,))
                ok = True
            finally:
                # Success path: cleanup MUST succeed -> a strict rmtree failure becomes a
                # systemic CaptureSystemError at the boundary (a cleanup failure can never
                # accompany a successful capture). Failure path: a rejection is already in
                # flight, so best-effort cleanup avoids masking that cell-local EvidenceError.
                if ok:
                    shutil.rmtree(d)
                else:
                    shutil.rmtree(d, ignore_errors=True)


# --- per-cell association + collection ---------------------------------------
def _resolve_blob(blobs, artifact_id, what):
    """A live artifact the shell inventoried MUST have downloaded bytes here; a missing
    blob is a capture-integrity failure (systemic), never a silent skip."""
    blob = blobs.get(artifact_id)
    if blob is None:
        raise CaptureSystemError("missing downloaded bytes for live %s artifact id %r"
                                 % (what, artifact_id))
    return blob


def _verify_one(cell_id, receipt_art, inv_by_id, blobs, expected_family, tmp_root):
    """Fully verify a single (cell, receipt-artifact) candidate and return the verified
    receipt, or raise a cell-local ``EvidenceError`` (or systemic ``CaptureSystemError``)."""
    receipt = verify_receipt_artifact(_resolve_blob(blobs, receipt_art["id"], "receipt"),
                                      receipt_art.get("digest"))
    if receipt["cell_id"] != cell_id:
        raise EvidenceError(RECEIPT_FIELD_INVALID, "receipt cell_id does not match its artifact")
    # Capture OWNS cell<->artifact association via verified receipts; a package artifact
    # name must not itself carry the artifact association marker (that would let it
    # cross-associate inside the adapter). Reject such a name cell-locally.
    if A._ART_MARKER_HINT in receipt["artifact_name"]:
        raise EvidenceError(PACKAGE_BINDING_MISMATCH, "package artifact name carries a cell marker")
    pkg_id = receipt["artifact_id"]
    pkg = inv_by_id.get(pkg_id)
    if pkg is None:
        raise EvidenceError(PACKAGE_ARTIFACT_ABSENT, "package artifact not in live inventory")
    exp = pkg.get("expired")
    if exp is True:
        raise EvidenceError(PACKAGE_ARTIFACT_EXPIRED, "package artifact expired")
    if exp is not False:
        raise EvidenceError(PACKAGE_BINDING_MISMATCH, "package expiry not an explicit boolean")
    if pkg.get("name") != receipt["artifact_name"]:
        raise EvidenceError(PACKAGE_BINDING_MISMATCH, "package name differs: inventory vs receipt")
    pkg_blob = _resolve_blob(blobs, pkg_id, "package")
    verify_package_artifact(pkg_blob, pkg.get("digest"), receipt)
    reinspect_and_require_identity(pkg_blob, receipt, expected_family, tmp_root=tmp_root)
    return receipt


def _expiry_state(entry):
    """Classify a receipt candidate's expiry EXPLICITLY. ``True`` -> genuinely expired
    (ignorable); ``False`` -> live; anything else (missing, null, string, numeric, list,
    object) -> ``malformed`` evidence that must never be silently treated as expired."""
    x = entry.get("expired")
    if x is True:
        return "expired"
    if x is False:
        return "live"
    return "malformed"


def collect_cell_evidence(planned_cell_ids, inventory, blobs, family_by_cell, tmp_root=None):
    """Resolve and verify each planned cell's receipt association.

    Returns ``(verified, verdicts, receipt_candidate_count)`` where ``verified`` is a list
    of ``(cell_id, receipt, receipt_artifact_id)`` for fully verified cells and ``verdicts``
    is one ordered result dict per planned cell.

    Exact-name receipt candidates are partitioned by EXPLICIT expiry (see ``_expiry_state``):

      * ``> 1`` live candidate                       -> ambiguous (CELL_AMBIGUOUS_ASSOCIATIONS)
      * exactly one live candidate + a malformed one -> ambiguous (RECEIPT_EXPIRY_MALFORMED):
        a valid live candidate beside malformed expiry evidence suppresses acceptance
      * exactly one live candidate (expired siblings ignored) -> verify it
      * no live candidate, but a malformed one       -> rejected (RECEIPT_EXPIRY_MALFORMED)
      * only genuinely-expired candidate(s)          -> absent (unavailable)
      * no candidate                                 -> absent

    Multiplicity is UNTRUSTED (GitHub's name-dedup is never relied on); no valid candidate
    is ever retained from a valid-plus-invalid pair. ``EvidenceError`` is caught per cell;
    ``CaptureSystemError`` propagates."""
    inv_by_id = {}
    for e in inventory:
        if _is_pos_int(e.get("id")):
            inv_by_id.setdefault(e["id"], e)
    verified, verdicts = [], []
    receipt_candidate_count = 0
    for cid in planned_cell_ids:
        receipt_name = RECEIPT_ARTIFACT_PREFIX + cid
        cands = [e for e in inventory if e.get("name") == receipt_name]
        live = [e for e in cands if _expiry_state(e) == "live"]
        malformed = [e for e in cands if _expiry_state(e) == "malformed"]
        receipt_candidate_count += len(live)
        rec = {"cell_id": cid, "verdict": "absent", "code": None, "detail": "",
               "receipt_artifact_id": None, "package_artifact_id": None}
        if len(live) > 1:
            rec.update(verdict="ambiguous", code=CELL_AMBIGUOUS_ASSOCIATIONS,
                       detail="multiple live receipt artifacts for one cell")
        elif len(live) == 1 and malformed:
            rec.update(verdict="ambiguous", code=RECEIPT_EXPIRY_MALFORMED,
                       detail="live receipt beside a malformed-expiry receipt")
        elif len(live) == 1:
            ra = live[0]
            rec["receipt_artifact_id"] = ra["id"] if _is_pos_int(ra.get("id")) else None
            fam = family_by_cell.get(cid)
            ef = fam if fam in (I.RPM, I.DEB) else None
            try:
                receipt = _verify_one(cid, ra, inv_by_id, blobs, ef, tmp_root)
            except EvidenceError as e:
                rec.update(verdict="rejected", code=e.code, detail=e.detail)
            else:
                rec.update(verdict="accepted", package_artifact_id=receipt["artifact_id"])
                verified.append((cid, receipt, ra["id"]))
        elif malformed:
            rec.update(verdict="rejected", code=RECEIPT_EXPIRY_MALFORMED,
                       detail="receipt artifact has malformed expiry metadata")
        else:
            rec["detail"] = "receipt artifact expired" if cands else "no receipt artifact"
        verdicts.append(rec)
    return verified, verdicts, receipt_candidate_count


# --- inventory sanitization + capture-evidence/1 -----------------------------
def _sanitize_inventory(raw_arts):
    """Reduce raw artifact records to a credential-free, capture-only inventory. Keeps
    only ``id``/``name``/``digest``/``expired``/``size_in_bytes``/``created_at`` and drops
    any ``archive_download_url`` / signed URL / query string. ``digest`` is normalized to
    bare hex (or null); ``expired`` survives only as an explicit boolean (else null)."""
    out = []
    for a in raw_arts:
        name = a.get("name")
        size = a.get("size_in_bytes")
        created = a.get("created_at")
        exp = a.get("expired")
        out.append({
            "id": a.get("id"),
            "name": name if isinstance(name, str) else None,
            "digest": normalize_api_digest(a.get("digest")),
            "expired": exp if isinstance(exp, bool) else None,
            "size_in_bytes": size if _is_nonneg_int(size) else None,
            "created_at": created if isinstance(created, str) else None,
        })
    return out


def _require_scalar_provenance(p):
    """Provenance is INJECTED by the caller and copied verbatim. It must be a flat object
    of JSON scalars — this both keeps evidence deterministic and structurally blocks a
    nested URL/redirect object from being persisted. (The I/O shell is responsible for
    never placing a token or URL in a scalar value.)"""
    if not isinstance(p, dict):
        raise CaptureSystemError("provenance must be an object")
    for k, v in p.items():
        if not isinstance(k, str):
            raise CaptureSystemError("provenance keys must be strings")
        if not (v is None or isinstance(v, (str, int, float, bool))):
            raise CaptureSystemError("provenance values must be JSON scalars")
        # NaN / +Inf / -Inf are floats but not valid JSON: reject as systemic so they
        # can never enter the evidence and break a strict JSON consumer.
        if isinstance(v, float) and not isinstance(v, bool) and (v != v or v in (float("inf"), float("-inf"))):
            raise CaptureSystemError("provenance values must be finite")


def _capture_evidence(planned_ids, verdicts, sanitized_inventory, verified,
                      provenance, receipt_candidate_count):
    """Assemble the deterministic ``capture-evidence/1`` document: injected provenance,
    capture-level counts ONLY (never reducer results), one ordered result per planned
    cell, and a sanitized, ordered live inventory."""
    counts = {
        "planned_cells": len(planned_ids),
        "receipt_candidates": receipt_candidate_count,
        "accepted_receipt_cells": sum(1 for v in verdicts if v["verdict"] == "accepted"),
        "rejected_receipt_cells": sum(1 for v in verdicts if v["verdict"] == "rejected"),
        "ambiguous_receipt_cells": sum(1 for v in verdicts if v["verdict"] == "ambiguous"),
        "absent_receipt_cells": sum(1 for v in verdicts if v["verdict"] == "absent"),
        "verified_package_artifacts": len(verified),
        "verified_members": sum(len(r["members"]) for (_c, r, _i) in verified),
    }
    cells = sorted(
        ({"cell_id": v["cell_id"], "verdict": v["verdict"], "code": v["code"],
          "detail": v["detail"], "receipt_artifact_id": v["receipt_artifact_id"],
          "package_artifact_id": v["package_artifact_id"]} for v in verdicts),
        key=lambda c: c["cell_id"])
    live_inventory = sorted(
        ({"id": e["id"], "name": e["name"], "digest": e["digest"], "expired": e["expired"],
          "size_in_bytes": e["size_in_bytes"], "created_at": e["created_at"]}
         for e in sanitized_inventory if e["expired"] is False),
        key=lambda e: (e["id"] if _is_pos_int(e["id"]) else 0, e["name"] or ""))
    return {
        "schema": CAPTURE_SCHEMA,
        "provenance": copy.deepcopy(provenance),
        "counts": counts,
        "cells": cells,
        "live_inventory": live_inventory,
    }


def capture_evidence_to_json(evidence):
    """Canonical, deterministic serialization of a capture-evidence/1 dict. ``allow_nan``
    is False as a defensive backstop: a non-finite value would raise here rather than emit
    invalid JSON (``NaN``/``Infinity``)."""
    return json.dumps(evidence, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


# --- top-level pure entry point ---------------------------------------------
def capture_to_reducer_input(*, detector_matrices, job_pages, artifact_pages, blobs,
                             release_intent, component_policy, publication_results,
                             provenance, tmp_root=None):
    """Independently verify a run's downloaded evidence and assemble ``(reducer_input,
    capture_evidence)``.

    Inputs (all already captured; this module performs NO network I/O):
      * ``detector_matrices`` — the detector RPM/DEB matrix outputs.
      * ``job_pages`` / ``artifact_pages`` — the raw paginated Jobs/Artifacts API pages.
      * ``blobs`` — a mapping ``{artifact_id: local ZIP path or seekable file}`` for the
        downloaded receipt and package artifacts.
      * ``release_intent`` / ``component_policy`` / ``publication_results`` / ``provenance``
        — injected release context (``component_policy`` stays injected so the consumer is
        never the policy owner; ``provenance`` carries the caller-supplied ``captured_at``).

    Reducer input is assembled ONLY through the committed adapter functions, with artifact
    records emitted solely from fully verified receipts/artifacts. Any global inconsistency
    raises ``CaptureSystemError``; cell-local rejections are recorded in the evidence and
    simply produce no artifact record."""
    _require_scalar_provenance(provenance)
    if not isinstance(blobs, dict):
        raise CaptureSystemError("blobs must be a mapping of artifact_id -> local ZIP")
    if not isinstance(detector_matrices, (list, tuple)):
        raise CaptureSystemError("detector_matrices must be a list")

    # 1. planned cells (opaque cell_id; family retained for expected-family enforcement).
    try:
        planned_cells = A.planned_cells_from_detector(*detector_matrices)
    except A.AdapterError as e:
        raise CaptureSystemError("detector matrices unusable: %s" % (e,))
    planned_ids, family_by_cell, seen = [], {}, set()
    for c in planned_cells:
        cid = c.get("cell_id") if isinstance(c, dict) else None
        if isinstance(cid, str) and cid.strip() != "" and cid not in seen:
            seen.add(cid)
            planned_ids.append(cid)
            family_by_cell[cid] = c.get("family")
    planned_ids.sort()

    # 2. jobs (raw pages -> combined -> marker-associated job records).
    try:
        jobs = A.combine_pages(job_pages, "jobs")
        job_records = A.job_records_from_jobs(jobs, planned_ids)
    except A.AdapterError as e:
        raise CaptureSystemError("job evidence unusable: %s" % (e,))

    # 3. artifact inventory (raw pages -> combined -> sanitized capture inventory).
    try:
        raw_arts = A.combine_pages(artifact_pages, "artifacts")
    except A.AdapterError as e:
        raise CaptureSystemError("artifact inventory unusable: %s" % (e,))
    inventory = _sanitize_inventory(raw_arts)

    # 4. per-cell verification. This is the ONLY step that touches the local filesystem
    # (archive reads, temp-dir creation, extraction, cleanup). Convert any local OSError
    # (unusable tmp_root, missing/unreadable blob, extraction-write or cleanup failure) into
    # a sanitized systemic CaptureSystemError with a FIXED message (no raw text, no paths).
    # Cell-local untrusted-evidence failures never reach here as OSError: they are caught
    # inside collect_cell_evidence as EvidenceError. Only OSError is caught, so a programming
    # error is never silently reclassified as an infrastructure failure.
    try:
        verified, verdicts, rc_count = collect_cell_evidence(
            planned_ids, inventory, blobs, family_by_cell, tmp_root=tmp_root)
    except OSError:
        raise CaptureSystemError("local capture I/O failed")

    # 5. verified artifacts/receipts -> adapter artifact records.
    ver_inv, ver_receipts = [], []
    for (cid, receipt, _rid) in verified:
        pkg_id = receipt["artifact_id"]
        ver_inv.append({"id": pkg_id, "name": receipt["artifact_name"], "expired": False,
                        "members": receipt["members"]})
        ver_receipts.append({"artifact_id": pkg_id, "cell_id": cid})
    try:
        artifacts = A.artifact_records(ver_inv, planned_ids, receipts=ver_receipts)
    except A.AdapterError as e:
        raise CaptureSystemError("verified artifact assembly failed: %s" % (e,))

    # 6. assemble the reducer envelope (manifest omitted: optional, audit-only).
    try:
        env = A.assemble_reducer_input(
            planned_cells=planned_cells, job_records=job_records, artifacts=artifacts,
            publication_results=publication_results, release_intent=release_intent,
            component_policy=component_policy, provenance=provenance)
    except A.AdapterError as e:
        raise CaptureSystemError("reducer envelope assembly failed: %s" % (e,))

    evidence = _capture_evidence(planned_ids, verdicts, inventory, verified, provenance, rc_count)
    return env, evidence
