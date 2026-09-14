"""Offline RPM/DEB package inspector (pep-members/1 producer).

GENERIC and POLICY-FREE: given package files (already present on disk) this module
extracts the structured per-package *member* metadata that the certification
planner consumes. It knows NOTHING about component names, supported platforms,
PostgreSQL policy, package allowlists, or expected binary versions — it only
reports what a package's own headers and bytes say.

Output shape (``pep-members/1``), one record per package file, fields in this
exact order:

    package_name         str          RPM %{NAME} / DEB Package
    epoch                int | None    RPM %{EPOCHNUM} (0 -> None) / DEB "N:" prefix
    version              str          RPM %{VERSION} / DEB upstream_version
    release              str          RPM %{RELEASE} / DEB debian_revision ("" if none)
    native_arch          str          RPM %{ARCH} (preserved, incl. source RPMs) / DEB Architecture
    package_class        str          "runtime" | "source" | "debug"
    sha256               str          lowercase hex of the package FILE bytes
    artifact_member_path str          exact, case-preserving path of the file inside the artifact

Design rules (fail-closed throughout):
  * Identity and class come from HEADERS, never from the filename.
  * RPM source is classified authoritatively by ``%{SOURCEPACKAGE}``, and only
    internally consistent marker pairs are accepted:
      - ``SOURCEPACKAGE == "1"`` means source and REQUIRES ``SOURCERPM == "(none)"``;
      - ``SOURCEPACKAGE == "(none)"`` means binary and REQUIRES a nonblank,
        non-"(none)" ``SOURCERPM``;
      - every other value (including "0", blank, padded or arbitrary text) raises.
    ``%{SOURCERPM}`` only validates consistency; it is never added to the schema
    and never independently classifies. Source takes precedence over debug/runtime.
  * The real ``%{ARCH}`` header is preserved verbatim, including for source RPMs
    (which may report x86_64 or aarch64).
  * v1 ``artifact_member_path`` must be a safe flat filename (no separators,
    absolute paths, traversal, or blanks); duplicates across a batch are rejected.
  * Every emitted member is CENTRALLY validated before it is returned.
  * The public boundary is TOTAL: malformed input (bad path/pair/field types,
    missing/unexpected fields, invalid values) raises ``InspectError`` — never a
    bare TypeError/AttributeError/KeyError. This is done with explicit checks,
    not a broad catch that would hide programming defects.
  * Inspection is ALL-OR-NOTHING: on any failure nothing partial is returned.
    A batch's output is sorted by ``artifact_member_path`` so it is deterministic
    independent of caller order.
  * External tools (``rpm``, ``dpkg-deb``) are executed with an argv list and
    ``shell=False`` (explicit), stdout/stderr captured into stdlib temporary
    files (genuinely memory-bounded), with a timeout. Successful stdout is decoded
    as STRICT UTF-8 (malformed bytes fail closed); stderr diagnostics decode
    leniently.

v1 scope: ``.rpm`` (binary + source) and ``.deb`` only (no ``.dsc``/``.ddeb``
claim; DEB debug detected by a ``-dbgsym`` package name). Stdlib only.
Unit-testable via ``pytest utillities/test_pep_pkg_inspect.py``.
"""
from __future__ import annotations

import hashlib
import subprocess
import tempfile

SCHEMA = "pep-members/1"

# Tool binaries (resolved via PATH so tests can inject fake executables).
RPM_BIN = "rpm"
DPKG_DEB_BIN = "dpkg-deb"

# Package formats.
RPM = "rpm"
DEB = "deb"

# Bounds for external-tool execution.
_TOOL_TIMEOUT_S = 30
_MAX_TOOL_OUTPUT = 64 * 1024        # a header query is tiny; cap defensively
_MAX_PKG_BYTES = 2 * 1024 * 1024 * 1024   # 2 GiB hard ceiling on a package file

# Magic bytes for format detection (never trust the filename for format).
_RPM_MAGIC = b"\xed\xab\xee\xdb"
_DEB_MAGIC = b"!<arch>\n"

# RPM query: tab-separated, one line. EPOCHNUM is 0 when no epoch; SOURCEPACKAGE
# is "1" for a source package and "(none)" for a binary package; SOURCERPM is
# "(none)" for a source package and names the source RPM for a binary package.
_RPM_QF = "%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SOURCEPACKAGE}\t%{SOURCERPM}\n"
_RPM_FIELDS = ("name", "epochnum", "version", "release", "arch", "sourcepackage", "sourcerpm")
# RPM identity fields that must be present, nonblank and unpadded.
_RPM_IDENTITY = ("name", "version", "release", "arch")

# DEB control fields we request and accept — exactly these three.
_DEB_FIELDS = ("package", "version", "architecture")

_VALID_CLASSES = ("runtime", "source", "debug")
_HEXDIGITS = frozenset("0123456789abcdef")

_MEMBER_ORDER = ("package_name", "epoch", "version", "release",
                 "native_arch", "package_class", "sha256", "artifact_member_path")


class InspectError(Exception):
    """Raised for any package that cannot be faithfully inspected. Callers treat
    this as fail-closed: no partial members are produced."""


# --- artifact_member_path (v1: safe flat filename) --------------------------
def validate_member_path(path):
    """Return the path unchanged if it is a safe flat filename, else raise.

    v1 rejects blank, absolute, traversal, and separator-containing paths. The
    value is used verbatim and case-preserved — never normalized."""
    if not isinstance(path, str) or path == "":
        raise InspectError("artifact_member_path must be a non-empty string: %r" % (path,))
    if path.strip() != path or path.strip() == "":
        raise InspectError("artifact_member_path must not be blank or padded: %r" % (path,))
    if "\x00" in path:
        raise InspectError("artifact_member_path must not contain NUL: %r" % (path,))
    if "/" in path or "\\" in path:
        raise InspectError("artifact_member_path must be a flat filename (no separators): %r" % (path,))
    if path in (".", ".."):
        raise InspectError("artifact_member_path must not be '.' or '..': %r" % (path,))
    return path


# --- helpers ----------------------------------------------------------------
def _sha256_file(path):
    """SHA-256 hex of the file's raw bytes (streamed). Fail closed on I/O or an
    over-large file."""
    h = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_PKG_BYTES:
                    raise InspectError("package file exceeds size ceiling: %r" % (path,))
                h.update(chunk)
    except OSError as e:
        raise InspectError("cannot read package file %r: %s" % (path, e))
    return h.hexdigest()


def _detect_format(path):
    """Detect rpm vs deb from MAGIC BYTES (not the filename)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError as e:
        raise InspectError("cannot read package file %r: %s" % (path, e))
    if head.startswith(_RPM_MAGIC):
        return RPM
    if head.startswith(_DEB_MAGIC):
        return DEB
    raise InspectError("unrecognized package format (not rpm/deb) for %r" % (path,))


def _run(argv, timeout=_TOOL_TIMEOUT_S):
    """Execute a tool with an argv list and ``shell=False`` (explicit).

    stdout/stderr are captured into stdlib temporary files, so capture is
    genuinely memory-bounded (the kernel writes to the files, not into this
    process). After a successful run at most ``_MAX_TOOL_OUTPUT + 1`` bytes of
    stdout are read and anything larger is rejected. Successful stdout is decoded
    as STRICT UTF-8 — malformed bytes fail closed; stderr, used only for
    diagnostics, is decoded leniently. A timeout kills the child and cleans up."""
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        try:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL,             # nosec B603 - argv list, shell=False
                                  stdout=out_f, stderr=err_f,
                                  shell=False, timeout=timeout, check=False)
        except FileNotFoundError:
            raise InspectError("required tool not found: %r" % (argv[0],))
        except subprocess.TimeoutExpired:
            raise InspectError("tool timed out after %ss: %r" % (timeout, argv))
        except OSError as e:
            raise InspectError("tool execution failed for %r: %s" % (argv, e))
        if proc.returncode != 0:
            err_f.seek(0)
            err = err_f.read(2048).decode("utf-8", "replace").strip()
            raise InspectError("tool %r exited %d: %s" % (argv[0], proc.returncode, err[:500]))
        out_f.seek(0)
        data = out_f.read(_MAX_TOOL_OUTPUT + 1)
        if len(data) > _MAX_TOOL_OUTPUT:
            raise InspectError("tool output too large from %r (> %d bytes)" % (argv[0], _MAX_TOOL_OUTPUT))
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise InspectError("tool %r produced non-UTF-8 stdout" % (argv[0],))


# --- RPM --------------------------------------------------------------------
def _parse_epochnum(value):
    """RPM EPOCHNUM -> int epoch or None. 0 / (none) / blank means 'no epoch'."""
    if not isinstance(value, str):
        raise InspectError("RPM epoch must be a string: %r" % (value,))
    v = value.strip()
    if v in ("", "0", "(none)"):
        return None
    if v.isdigit():
        return int(v)
    raise InspectError("malformed RPM epoch: %r" % (value,))


def _classify_rpm(name, sourcepackage, sourcerpm):
    """Classify an RPM from its source markers, accepting only consistent pairs.

    SOURCEPACKAGE is authoritative and compared EXACTLY (padded/other values are
    rejected). SOURCERPM only validates consistency. Source precedes debug/runtime;
    debug is decided by package-name suffix. Filename is never consulted."""
    if not (isinstance(name, str) and isinstance(sourcepackage, str) and isinstance(sourcerpm, str)):
        raise InspectError("RPM classification fields must be strings")
    if sourcepackage == "1":
        if sourcerpm != "(none)":
            raise InspectError("inconsistent RPM markers: SOURCEPACKAGE=1 requires SOURCERPM='(none)', got %r"
                               % (sourcerpm,))
        return "source"
    if sourcepackage == "(none)":
        if sourcerpm == "" or sourcerpm == "(none)" or sourcerpm.strip() != sourcerpm or sourcerpm.strip() == "":
            raise InspectError("inconsistent RPM markers: binary package must name a nonblank SOURCERPM, got %r"
                               % (sourcerpm,))
        if name.endswith("-debuginfo") or name.endswith("-debugsource"):
            return "debug"
        return "runtime"
    raise InspectError("unexpected RPM SOURCEPACKAGE value: %r" % (sourcepackage,))


def parse_rpm_output(raw):
    """Parse the single tab-separated ``rpm -qp`` line into a fields dict.

    Requires exactly seven fields on a single line. Structural only; identity,
    classification and value validation happen in ``member_from_rpm``/``_member``."""
    if not isinstance(raw, str):
        raise InspectError("RPM query output must be a string, got %s" % (type(raw).__name__,))
    line = raw.strip("\n")
    if "\n" in line:
        raise InspectError("expected a single RPM query line, got: %r" % (raw,))
    parts = line.split("\t")
    if len(parts) != len(_RPM_FIELDS):
        raise InspectError("unexpected RPM query field count %d (want %d): %r"
                           % (len(parts), len(_RPM_FIELDS), raw))
    return dict(zip(_RPM_FIELDS, parts))


def member_from_rpm(fields, sha256, member_path):
    """Build a pep-members/1 record from parsed RPM fields (fail-closed).

    Requires a dict carrying exactly the seven string RPM fields; rejects
    blank/padded identity fields; enforces source-marker consistency; and routes
    the result through the central member validator."""
    if not isinstance(fields, dict):
        raise InspectError("RPM fields must be a dict, got %s" % (type(fields).__name__,))
    for k in _RPM_FIELDS:
        if k not in fields:
            raise InspectError("RPM fields missing key %r" % (k,))
        if not isinstance(fields[k], str):
            raise InspectError("RPM field %r must be a string, got %s" % (k, type(fields[k]).__name__))
    for k in _RPM_IDENTITY:
        v = fields[k]
        if v == "" or v.strip() != v:
            raise InspectError("RPM %s is blank or padded: %r" % (k, v))
    epoch = _parse_epochnum(fields["epochnum"])
    pkg_class = _classify_rpm(fields["name"], fields["sourcepackage"], fields["sourcerpm"])
    return _member(fields["name"], epoch, fields["version"], fields["release"],
                   fields["arch"], pkg_class, sha256, member_path)


def _inspect_rpm(path, member_path):
    fields = parse_rpm_output(_run([RPM_BIN, "-qp", "--nosignature", "--queryformat", _RPM_QF, path]))
    return member_from_rpm(fields, _sha256_file(path), member_path)


# --- DEB --------------------------------------------------------------------
def split_deb_version(ver):
    """Split a Debian Version into (epoch:int|None, upstream:str, revision:str).

    Epoch is the optional ``N:`` prefix (a single ':'; multiple colons are
    rejected); the Debian revision is everything after the FINAL hyphen (so
    hyphenated upstream versions are preserved). A version with no hyphen has an
    empty revision; a hyphen with an empty revision is rejected."""
    if not isinstance(ver, str):
        raise InspectError("DEB Version must be a string, got %s" % (type(ver).__name__,))
    v = ver.strip()
    if v == "":
        raise InspectError("blank DEB Version")
    epoch = None
    if ":" in v:
        head, _, rest = v.partition(":")
        if ":" in rest:
            raise InspectError("malformed DEB epoch (multiple ':') in version %r" % (ver,))
        if not head.isdigit():
            raise InspectError("malformed DEB epoch in version %r" % (ver,))
        epoch = int(head)
        v = rest
    if "-" in v:
        upstream, _, revision = v.rpartition("-")
        if revision == "":
            raise InspectError("DEB version has a hyphen but an empty revision: %r" % (ver,))
    else:
        upstream, revision = v, ""
    if upstream.strip() == "":
        raise InspectError("DEB version has no upstream part: %r" % (ver,))
    return epoch, upstream, revision


def _classify_deb(name):
    """Debug by ``-dbgsym`` package name; else runtime. (v1 handles .deb only.)"""
    if not isinstance(name, str):
        raise InspectError("DEB package name must be a string")
    return "debug" if name.endswith("-dbgsym") else "runtime"


def parse_deb_output(raw):
    """Parse ``dpkg-deb --field`` 'Field: value' lines into a dict.

    Accepts EXACTLY Package, Version and Architecture (case-insensitive keys);
    rejects duplicate, missing or unexpected fields."""
    if not isinstance(raw, str):
        raise InspectError("dpkg-deb output must be a string, got %s" % (type(raw).__name__,))
    out = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise InspectError("malformed dpkg-deb field line: %r" % (line,))
        key, _, val = line.partition(":")
        k = key.strip().lower()
        if k in out:
            raise InspectError("duplicate dpkg-deb field: %r" % (key.strip(),))
        out[k] = val.strip()
    unexpected = sorted(set(out) - set(_DEB_FIELDS))
    if unexpected:
        raise InspectError("unexpected dpkg-deb field(s): %r" % (unexpected,))
    missing = [k for k in _DEB_FIELDS if k not in out]
    if missing:
        raise InspectError("missing dpkg-deb field(s): %r" % (missing,))
    return out


def member_from_deb(fields, sha256, member_path):
    """Build a pep-members/1 record from parsed DEB control fields (fail-closed)."""
    if not isinstance(fields, dict):
        raise InspectError("DEB fields must be a dict, got %s" % (type(fields).__name__,))
    for k in _DEB_FIELDS:
        if k not in fields:
            raise InspectError("DEB missing field %r: %r" % (k, fields))
        if not isinstance(fields[k], str):
            raise InspectError("DEB field %r must be a string, got %s" % (k, type(fields[k]).__name__))
    if fields["package"].strip() == "" or fields["architecture"].strip() == "":
        raise InspectError("DEB blank Package/Architecture: %r" % (fields,))
    epoch, upstream, revision = split_deb_version(fields["version"])
    return _member(fields["package"], epoch, upstream, revision, fields["architecture"],
                   _classify_deb(fields["package"]), sha256, member_path)


def _inspect_deb(path, member_path):
    fields = parse_deb_output(_run([DPKG_DEB_BIN, "--field", path, "Package", "Version", "Architecture"]))
    return member_from_deb(fields, _sha256_file(path), member_path)


# --- assembly + public API --------------------------------------------------
def _member(package_name, epoch, version, release, native_arch, package_class, sha256, member_path):
    """Central, fail-closed member constructor. Every emitted member passes here.

    Requires: nonblank/unpadded string package_name/version/native_arch; release
    is a string; epoch is null or a nonnegative integer; package_class is exactly
    runtime/source/debug; sha256 is exactly 64 lowercase hex chars; and
    artifact_member_path passes the flat-filename validator."""
    for label, val in (("package_name", package_name), ("version", version), ("native_arch", native_arch)):
        if not isinstance(val, str) or val == "" or val.strip() != val:
            raise InspectError("%s must be a nonblank, unpadded string: %r" % (label, val))
    if not isinstance(release, str):
        raise InspectError("release must be a string: %r" % (release,))
    if not (epoch is None or (isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0)):
        raise InspectError("epoch must be null or a nonnegative integer: %r" % (epoch,))
    if package_class not in _VALID_CLASSES:
        raise InspectError("package_class must be one of %r: %r" % (_VALID_CLASSES, package_class))
    if not (isinstance(sha256, str) and len(sha256) == 64 and set(sha256) <= _HEXDIGITS):
        raise InspectError("sha256 must be 64 lowercase hex chars: %r" % (sha256,))
    validate_member_path(member_path)
    return {
        "package_name": package_name,
        "epoch": epoch,
        "version": version,
        "release": release,
        "native_arch": native_arch,
        "package_class": package_class,
        "sha256": sha256,
        "artifact_member_path": member_path,
    }


def inspect_package(path, artifact_member_path):
    """Inspect ONE package file at ``path`` and return its pep-members/1 record.

    ``artifact_member_path`` is the file's exact (case-preserving) relative path
    inside the artifact; it is validated as a safe flat filename. Both the
    filesystem path type and the member path are validated BEFORE any file is
    opened. Format is detected from magic bytes. Raises ``InspectError`` on any
    problem."""
    if not isinstance(path, str) or path == "":
        raise InspectError("package path must be a non-empty string: %r" % (path,))
    mp = validate_member_path(artifact_member_path)
    fmt = _detect_format(path)
    if fmt == RPM:
        return _inspect_rpm(path, mp)
    return _inspect_deb(path, mp)


def inspect_members(entries):
    """Inspect a batch of package files, ALL-OR-NOTHING and order-independent.

    ``entries`` is a list of ``(path, artifact_member_path)`` pairs (one per
    package file in an artifact). Entry shape, filesystem path types and every
    member path are validated (and duplicates rejected) BEFORE any inspection
    runs; any failure raises ``InspectError`` and no partial result is returned.
    The returned members are sorted by ``artifact_member_path`` so output is
    deterministic regardless of caller order; field order within each member is
    stable (``_MEMBER_ORDER``)."""
    if not isinstance(entries, list):
        raise InspectError("entries must be a list of (path, artifact_member_path) pairs")
    seen = set()
    prepared = []
    for e in entries:
        if not isinstance(e, (tuple, list)) or len(e) != 2:
            raise InspectError("each entry must be a (path, artifact_member_path) pair: %r" % (e,))
        path, mp = e
        if not isinstance(path, str) or path == "":
            raise InspectError("entry path must be a non-empty string: %r" % (path,))
        vmp = validate_member_path(mp)
        if vmp in seen:
            raise InspectError("duplicate artifact_member_path: %r" % (vmp,))
        seen.add(vmp)
        prepared.append((path, vmp))
    members = [inspect_package(path, mp) for path, mp in prepared]   # any failure -> no partial
    members.sort(key=lambda m: m["artifact_member_path"])
    return members
