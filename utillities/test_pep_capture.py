"""Offline tests for the capture-verification layer (utillities/pep_capture.py).

Pure/deterministic: no network, no clock, no docker, no real rpm/dpkg. Uses fake
`rpm`/`dpkg-deb` executables and magic-byte package files (the same technique as
test_pep_pkg_inspect.py / test_pep_receipt_build.py) plus generated temporary ZIPs,
so nothing is downloaded and no binary package is committed.

Coverage: strict pep-receipt/2 parsing and every field/type boundary; API-digest vs
canonical-receipt-digest normalization; every unsafe/duplicate ZIP shape; raw archive
digest mismatches; missing/expired/conflicting artifacts; member-set and member-SHA
mismatches; full identity mismatches (incl package class and epoch); valid-plus-invalid
and multiple-association suppression; EvidenceError caught per cell vs CaptureSystemError
propagated; deterministic ordering/serialization; bounded/streaming reads with an
unbounded-read guard; temp-file cleanup on success and failure; the preserved Spike-0
attempt-2/attempt-3 rerun mechanics flowing capture -> adapter -> reducer; and a
static no-network/no-clock guard on the module source.
"""
import copy
import hashlib
import io
import json
import os
import stat
import zipfile
import zlib
from pathlib import Path

import pytest

import pep_capture as C
import pep_cert_adapter as A
import pep_cert_plan as R
import pep_pkg_inspect as I

HERE = Path(__file__).parent
FX = HERE / "cert_plan_fixtures"
GOLDEN = json.loads((HERE / "pkg_inspect_fixtures" / "golden.json").read_text())
CASES = GOLDEN["cases"]

_D64 = "ab" * 32


# --------------------------------------------------------------------------- #
# fake tooling + package/zip/receipt builders
# --------------------------------------------------------------------------- #
def _fake_tool(tmp_path, name, output_text):
    out = tmp_path / (name + ".out")
    out.write_bytes(output_text.encode("utf-8"))
    scr = tmp_path / name
    scr.write_text('#!/bin/sh\ncat %s\n' % json.dumps(str(out)))
    scr.chmod(0o755)
    return str(scr)


def use_fake_rpm(tmp_path, monkeypatch, case="rpm_runtime"):
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", CASES[case]["tool_output"]))


def use_fake_deb(tmp_path, monkeypatch, case="deb_runtime"):
    monkeypatch.setattr(I, "DPKG_DEB_BIN", _fake_tool(tmp_path, "dpkg-deb", CASES[case]["tool_output"]))


def _fake_rpm_router(tmp_path, monkeypatch, runtime="rpm_runtime", source="rpm_source_x86"):
    """A fake rpm that emits source output for a *.src.rpm path, else runtime (mirrors the
    receipt-build test harness), so a single artifact can carry runtime + source members."""
    ro = tmp_path / "rpm_runtime.out"; ro.write_bytes(CASES[runtime]["tool_output"].encode("utf-8"))
    so = tmp_path / "rpm_source.out"; so.write_bytes(CASES[source]["tool_output"].encode("utf-8"))
    scr = tmp_path / "rpm"
    scr.write_text(
        '#!/bin/sh\npkg=""\nfor a in "$@"; do pkg="$a"; done\n'
        'case "$pkg" in\n  *.src.rpm) cat %s ;;\n  *) cat %s ;;\nesac\n'
        % (json.dumps(str(so)), json.dumps(str(ro))))
    scr.chmod(0o755)
    monkeypatch.setattr(I, "RPM_BIN", str(scr))


def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _zip_paths(zip_path, arc_to_file, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(zip_path, "w", compression) as zf:
        for arc, fp in arc_to_file.items():
            zf.write(fp, arcname=arc)
    return str(zip_path)


def _corrupt(zip_path, needle, repl):
    """Flip bytes inside a STORED zip member's raw data (needle and repl MUST be equal
    length so entry offsets stay valid). The member's stored CRC-32 then no longer matches,
    so reading the member raises during member reading — not during archive hashing."""
    assert len(needle) == len(repl)
    data = Path(zip_path).read_bytes()
    assert needle in data, "needle not found in stored zip"
    Path(zip_path).write_bytes(data.replace(needle, repl, 1))


def _write_pkg(dirpath, name, magic, body):
    p = Path(dirpath) / name
    p.write_bytes(magic + body)
    return str(p)


class Cell(dict):
    __getattr__ = dict.__getitem__


def build_cell(tmp_path, monkeypatch, family="rpm", cell_id="rag-rpm-amd64", pkg_id=111,
               receipt_id=222, aname=None, case=None, body=None, member_path=None,
               receipt_mutator=None, pkg_inv_digest=None, receipt_inv_digest=None,
               pkg_expired=False, receipt_expired=False, stored=False, corrupt_pkg=None):
    """Build a fully-consistent (cell, package artifact, receipt artifact) triple with
    real bytes, real archive/member SHAs and matching inventory + blobs, for rpm or deb.
    Optional hooks let a test tamper the receipt/inventory, store uncompressed, or corrupt
    a stored member (recomputing the archive digest AFTER corruption)."""
    if family == "rpm":
        use_fake_rpm(tmp_path, monkeypatch, case or "rpm_runtime")
        magic, exp_fam = I._RPM_MAGIC, I.RPM
        case = case or "rpm_runtime"
    else:
        use_fake_deb(tmp_path, monkeypatch, case or "deb_runtime")
        magic, exp_fam = I._DEB_MAGIC, I.DEB
        case = case or "deb_runtime"
    mp = member_path if member_path is not None else CASES[case]["artifact_member_path"]
    body = body if body is not None else (cell_id.encode() + b"-BODY")
    safe = cell_id.replace("/", "_")
    d = tmp_path / ("src-" + safe)
    d.mkdir()
    fpath = _write_pkg(d, mp, magic, body)
    members = I.inspect_members([(fpath, mp)], expected_family=exp_fam)
    comp = zipfile.ZIP_STORED if (stored or corrupt_pkg) else zipfile.ZIP_DEFLATED
    pkg_zip = _zip_paths(tmp_path / ("pkg-" + safe + ".zip"), {mp: fpath}, compression=comp)
    if corrupt_pkg:
        _corrupt(pkg_zip, *corrupt_pkg)
    pkg_digest = _sha_file(pkg_zip)                       # digest reflects the (corrupted) archive
    name = aname if aname is not None else ("pkg-" + cell_id)
    receipt = {"schema": "pep-receipt/2", "cell_id": cell_id, "artifact_id": pkg_id,
               "artifact_name": name, "archive_digest": pkg_digest, "members": members}
    if receipt_mutator is not None:
        receipt = receipt_mutator(copy.deepcopy(receipt))
    rdir = tmp_path / ("rc-" + safe)
    rdir.mkdir()
    rjson = rdir / "receipt.json"
    rjson.write_text(json.dumps(receipt, indent=2) + "\n")
    receipt_zip = _zip_paths(tmp_path / ("receipt-" + safe + ".zip"), {"receipt.json": str(rjson)})
    receipt_digest = _sha_file(receipt_zip)
    inv = [
        {"id": pkg_id, "name": name, "digest": pkg_inv_digest or ("sha256:" + pkg_digest),
         "expired": pkg_expired, "size_in_bytes": os.path.getsize(pkg_zip),
         "created_at": "2026-01-01T00:00:00Z", "archive_download_url": "https://secret/blob?sig=TOKEN"},
        {"id": receipt_id, "name": "pep-receipt-" + cell_id,
         "digest": receipt_inv_digest or ("sha256:" + receipt_digest), "expired": receipt_expired,
         "size_in_bytes": os.path.getsize(receipt_zip), "created_at": "2026-01-01T00:00:00Z",
         "archive_download_url": "https://secret/blob?sig=TOKEN"},
    ]
    blobs = {pkg_id: pkg_zip, receipt_id: receipt_zip}
    return Cell(cell_id=cell_id, family=family, receipt=receipt, members=members, member_path=mp,
                pkg_zip=pkg_zip, receipt_zip=receipt_zip, pkg_digest=pkg_digest,
                receipt_digest=receipt_digest, inv=inv, blobs=blobs, pkg_id=pkg_id,
                receipt_id=receipt_id, aname=name)


def build_rpm_cell(*args, **kwargs):
    return build_cell(*args, family="rpm", **kwargs)


def _canon_receipt():
    m = copy.deepcopy(CASES["rpm_runtime"]["expected"])
    return {"schema": "pep-receipt/2", "cell_id": "c1", "artifact_id": 5,
            "artifact_name": "pkg-c1", "archive_digest": _D64, "members": [m]}


def _raw(obj):
    return json.dumps(obj).encode("utf-8")


# --------------------------------------------------------------------------- #
# A. strict pep-receipt/2 parsing
# --------------------------------------------------------------------------- #
def test_strict_parse_happy():
    r = C.strict_parse_receipt(_raw(_canon_receipt()))
    assert r["schema"] == "pep-receipt/2" and r["members"][0]["package_class"] == "runtime"


def test_strict_parse_duplicate_json_key():
    raw = b'{"schema":"pep-receipt/2","schema":"x","cell_id":"c1","artifact_id":5,' \
          b'"artifact_name":"n","archive_digest":"' + (b"ab" * 32) + b'","members":[]}'
    with pytest.raises(C.EvidenceError) as ei:
        C.strict_parse_receipt(raw)
    assert ei.value.code == C.RECEIPT_JSON_MALFORMED


@pytest.mark.parametrize("tok", ["NaN", "Infinity", "-Infinity"])
def test_strict_parse_nonfinite_rejected(tok):
    raw = ('{"schema":"pep-receipt/2","cell_id":"c1","artifact_id":%s,'
           '"artifact_name":"n","archive_digest":"%s","members":[]}' % (tok, _D64)).encode()
    with pytest.raises(C.EvidenceError) as ei:
        C.strict_parse_receipt(raw)
    assert ei.value.code == C.RECEIPT_JSON_MALFORMED


def test_strict_parse_bad_utf8():
    with pytest.raises(C.EvidenceError) as ei:
        C.strict_parse_receipt(b'\xff\xfe not utf8')
    assert ei.value.code == C.RECEIPT_JSON_MALFORMED


def test_strict_parse_not_json_and_non_object():
    for raw in (b"not json", b"[1,2,3]", b'"a string"', b"123"):
        with pytest.raises(C.EvidenceError) as ei:
            C.strict_parse_receipt(raw)
        assert ei.value.code == C.RECEIPT_JSON_MALFORMED


@pytest.mark.parametrize("mutate", [
    lambda r: {**r, "extra": 1},                       # extra top-level key
    lambda r: {k: v for k, v in r.items() if k != "members"},   # missing key
])
def test_strict_parse_top_level_key_set(mutate):
    with pytest.raises(C.EvidenceError) as ei:
        C.strict_parse_receipt(_raw(mutate(_canon_receipt())))
    assert ei.value.code == C.RECEIPT_SCHEMA_INVALID


def test_strict_parse_wrong_schema_constant():
    r = _canon_receipt(); r["schema"] = "pep-receipt/1"
    with pytest.raises(C.EvidenceError) as ei:
        C.strict_parse_receipt(_raw(r))
    assert ei.value.code == C.RECEIPT_SCHEMA_INVALID


@pytest.mark.parametrize("field,bad", [
    ("cell_id", ""), ("cell_id", " x"), ("cell_id", "x "), ("cell_id", 5),
    ("artifact_id", 0), ("artifact_id", -1), ("artifact_id", True), ("artifact_id", "5"), ("artifact_id", 1.0),
    ("artifact_name", ""), ("artifact_name", "  "), ("artifact_name", " n"), ("artifact_name", 7),
    ("archive_digest", "sha256:" + _D64), ("archive_digest", ("AB" * 32)),
    ("archive_digest", "ab" * 31), ("archive_digest", "ab" * 33), ("archive_digest", "zz" * 32),
    ("members", []), ("members", "x"), ("members", {}),
])
def test_strict_parse_top_field_boundaries(field, bad):
    r = _canon_receipt(); r[field] = bad
    with pytest.raises(C.EvidenceError) as ei:
        C.strict_parse_receipt(_raw(r))
    assert ei.value.code in (C.RECEIPT_FIELD_INVALID, C.RECEIPT_SCHEMA_INVALID)


def _mut_member(**changes):
    r = _canon_receipt()
    r["members"][0].update(changes)
    return r


@pytest.mark.parametrize("changes", [
    {"package_name": ""}, {"package_name": " x"}, {"package_name": 3},
    {"epoch": True}, {"epoch": -1}, {"epoch": "0"},
    {"version": ""}, {"version": " 1"},
    {"release": 5},
    {"native_arch": ""}, {"native_arch": "x "},
    {"package_class": "bogus"}, {"package_class": ""},
    {"sha256": "AB" * 32}, {"sha256": "sha256:" + ("ab" * 32)}, {"sha256": "ab" * 31},
    {"artifact_member_path": "a/b.rpm"}, {"artifact_member_path": ".."},
    {"artifact_member_path": ""}, {"artifact_member_path": "a\\b"},
])
def test_strict_parse_member_field_boundaries(changes):
    with pytest.raises(C.EvidenceError) as ei:
        C.strict_parse_receipt(_raw(_mut_member(**changes)))
    assert ei.value.code == C.RECEIPT_FIELD_INVALID


def test_strict_parse_member_extra_or_missing_key():
    r = _canon_receipt(); r["members"][0]["surprise"] = 1
    with pytest.raises(C.EvidenceError) as ei:
        C.strict_parse_receipt(_raw(r))
    assert ei.value.code == C.RECEIPT_FIELD_INVALID
    r2 = _canon_receipt(); del r2["members"][0]["epoch"]
    with pytest.raises(C.EvidenceError) as ei2:
        C.strict_parse_receipt(_raw(r2))
    assert ei2.value.code == C.RECEIPT_FIELD_INVALID


def test_strict_parse_duplicate_and_case_colliding_member_paths():
    m1 = copy.deepcopy(CASES["rpm_runtime"]["expected"])
    m2 = copy.deepcopy(CASES["rpm_runtime"]["expected"])
    m2["sha256"] = "cd" * 32
    dup = {"schema": "pep-receipt/2", "cell_id": "c1", "artifact_id": 5, "artifact_name": "n",
           "archive_digest": _D64, "members": [m1, m2]}   # identical member_path
    with pytest.raises(C.EvidenceError) as ei:
        C.strict_parse_receipt(_raw(dup))
    assert ei.value.code == C.RECEIPT_FIELD_INVALID
    m2b = copy.deepcopy(m2); m2b["artifact_member_path"] = m1["artifact_member_path"].upper()
    coll = {**dup, "members": [m1, m2b]}
    with pytest.raises(C.EvidenceError) as ei2:
        C.strict_parse_receipt(_raw(coll))
    assert ei2.value.code == C.RECEIPT_FIELD_INVALID


def test_strict_parse_accepts_epoch_int_and_null():
    r = _mut_member(epoch=2)
    assert C.strict_parse_receipt(_raw(r))["members"][0]["epoch"] == 2
    r0 = _mut_member(epoch=None)
    assert C.strict_parse_receipt(_raw(r0))["members"][0]["epoch"] is None


# --------------------------------------------------------------------------- #
# B. digest normalization: API (prefix-normalized) vs receipt (canonical only)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    (_D64, _D64), ("sha256:" + _D64, _D64), ("SHA256:" + ("AB" * 32), "ab" * 32),
    ("  " + _D64 + "  ", _D64),
])
def test_normalize_api_digest_ok(raw, expected):
    assert C.normalize_api_digest(raw) == expected


@pytest.mark.parametrize("bad", ["", "xyz", "ab" * 31, "gg" * 32, "sha256:", None, 123, "sha1:" + "ab" * 20])
def test_normalize_api_digest_none(bad):
    assert C.normalize_api_digest(bad) is None


def test_receipt_digest_is_never_prefix_normalized():
    # A 'sha256:'-prefixed receipt field is NONCANONICAL and must be rejected,
    # even though the same prefix is normalized for an API digest.
    r = _canon_receipt(); r["archive_digest"] = "sha256:" + _D64
    with pytest.raises(C.EvidenceError) as ei:
        C.strict_parse_receipt(_raw(r))
    assert ei.value.code == C.RECEIPT_FIELD_INVALID


# --------------------------------------------------------------------------- #
# C. ZIP safety: every unsafe/duplicate shape
# --------------------------------------------------------------------------- #
def _zip_with(zip_path, entries):
    """entries: list of (name, data, mode_bits_or_None)."""
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, data, mode in entries:
            if mode is None:
                zf.writestr(name, data)
            else:
                zi = zipfile.ZipInfo(name)
                zi.external_attr = mode << 16
                zf.writestr(zi, data)
    return str(zip_path)


def _entries(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        return C.safe_entries(zf, C.PACKAGE_ZIP_UNSAFE)


def test_safe_entries_happy_flat_sorted(tmp_path):
    z = _zip_with(tmp_path / "z.zip", [("b.rpm", b"1", None), ("a.rpm", b"2", None)])
    assert _entries(z) == ["a.rpm", "b.rpm"]


@pytest.mark.parametrize("entries", [
    [("sub/", b"", None)],                                    # directory (trailing slash)
    [("a/b.rpm", b"x", None)],                                # nested
    [("/abs.rpm", b"x", None)],                               # absolute
    [("a\\b.rpm", b"x", None)],                               # backslash
    [("..", b"x", None)],                                     # dotdot
    [("link", b"target", stat.S_IFLNK | 0o777)],             # symlink
    [("d", b"", stat.S_IFDIR | 0o755)],                       # dir by mode
])
def test_safe_entries_unsafe_shapes(tmp_path, entries):
    z = _zip_with(tmp_path / "z.zip", entries)
    with pytest.raises(C.EvidenceError) as ei:
        _entries(z)
    assert ei.value.code == C.PACKAGE_ZIP_UNSAFE


@pytest.mark.filterwarnings("ignore:Duplicate name:UserWarning")
def test_safe_entries_duplicate_and_case_collision(tmp_path):
    z = tmp_path / "dup.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.rpm", b"1")
        zf.writestr("a.rpm", b"2")            # exact duplicate
    with pytest.raises(C.EvidenceError) as ei:
        _entries(str(z))
    assert ei.value.code == C.PACKAGE_ZIP_UNSAFE
    z2 = _zip_with(tmp_path / "coll.zip", [("A.rpm", b"1", None), ("a.rpm", b"2", None)])
    with pytest.raises(C.EvidenceError) as ei2:
        _entries(z2)
    assert ei2.value.code == C.PACKAGE_ZIP_UNSAFE


# --------------------------------------------------------------------------- #
# D. verify_receipt_artifact
# --------------------------------------------------------------------------- #
def test_verify_receipt_happy(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    got = C.verify_receipt_artifact(c.receipt_zip, "sha256:" + c.receipt_digest)
    assert got["cell_id"] == c.cell_id and got["artifact_id"] == c.pkg_id


def test_verify_receipt_accepts_seekable_file(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    with open(c.receipt_zip, "rb") as fh:
        got = C.verify_receipt_artifact(fh, c.receipt_digest)
    assert got["cell_id"] == c.cell_id


def test_verify_receipt_archive_digest_mismatch(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_receipt_artifact(c.receipt_zip, "cd" * 32)
    assert ei.value.code == C.RECEIPT_ARCHIVE_DIGEST_MISMATCH


def test_verify_receipt_malformed_api_digest(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_receipt_artifact(c.receipt_zip, "not-a-digest")
    assert ei.value.code == C.RECEIPT_ARCHIVE_DIGEST_MISMATCH


def test_verify_receipt_not_single_root(tmp_path):
    # receipt zip must be exactly one root receipt.json
    z = _zip_with(tmp_path / "r.zip", [("receipt.json", b"{}", None), ("readme", b"x", None)])
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_receipt_artifact(z, _sha_file(z))
    assert ei.value.code == C.RECEIPT_ZIP_NOT_SINGLE
    z2 = _zip_with(tmp_path / "r2.zip", [("other.json", b"{}", None)])
    with pytest.raises(C.EvidenceError) as ei2:
        C.verify_receipt_artifact(z2, _sha_file(z2))
    assert ei2.value.code == C.RECEIPT_ZIP_NOT_SINGLE


def test_verify_receipt_not_a_zip(tmp_path):
    bad = tmp_path / "bad.zip"; bad.write_bytes(b"this is not a zip file")
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_receipt_artifact(str(bad), _sha_file(str(bad)))
    assert ei.value.code == C.RECEIPT_ZIP_UNSAFE


def test_verify_receipt_json_too_large(tmp_path):
    big = tmp_path / "big.json"; big.write_bytes(b"x" * (C._MAX_RECEIPT_BYTES + 10))
    z = _zip_paths(tmp_path / "r.zip", {"receipt.json": str(big)})
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_receipt_artifact(z, _sha_file(z))
    assert ei.value.code == C.RECEIPT_JSON_MALFORMED


# --------------------------------------------------------------------------- #
# E. verify_package_artifact
# --------------------------------------------------------------------------- #
def test_verify_package_happy(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    assert C.verify_package_artifact(c.pkg_zip, "sha256:" + c.pkg_digest, c.receipt) is None


def test_verify_package_malformed_api_digest(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_package_artifact(c.pkg_zip, "bogus", c.receipt)
    assert ei.value.code == C.PACKAGE_ARCHIVE_DIGEST_MISMATCH


def test_verify_package_api_not_equal_receipt(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_package_artifact(c.pkg_zip, "cd" * 32, c.receipt)   # api != receipt.archive_digest
    assert ei.value.code == C.PACKAGE_ARCHIVE_DIGEST_MISMATCH


def test_verify_package_zip_not_equal_api(tmp_path, monkeypatch):
    # receipt.archive_digest == api == X, but the real zip hashes to something else.
    x = "cd" * 32
    c = build_rpm_cell(tmp_path, monkeypatch, receipt_mutator=lambda r: {**r, "archive_digest": x})
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_package_artifact(c.pkg_zip, x, c.receipt)
    assert ei.value.code == C.PACKAGE_ARCHIVE_DIGEST_MISMATCH


def test_verify_package_member_set_mismatch(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    # add an unexpected extra entry to the package zip, then re-point api/receipt to it
    extra = tmp_path / "extra.rpm"; extra.write_bytes(I._RPM_MAGIC + b"X")
    with zipfile.ZipFile(c.pkg_zip, "a") as zf:
        zf.write(str(extra), arcname="extra.rpm")
    new_digest = _sha_file(c.pkg_zip)
    r = {**c.receipt, "archive_digest": new_digest}
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_package_artifact(c.pkg_zip, new_digest, r)
    assert ei.value.code == C.PACKAGE_MEMBER_SET_MISMATCH


def test_verify_package_member_sha_mismatch(tmp_path, monkeypatch):
    # correct archive digest, correct member set, but the receipt's member SHA is wrong
    c = build_rpm_cell(tmp_path, monkeypatch,
                       receipt_mutator=lambda r: _with_member_sha(r, "bb" * 32))
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_package_artifact(c.pkg_zip, c.pkg_digest, c.receipt)
    assert ei.value.code == C.MEMBER_SHA_MISMATCH


def _with_member_sha(r, sha):
    r["members"][0]["sha256"] = sha
    return r


# --------------------------------------------------------------------------- #
# F. reinspect_and_require_identity (headers, not filenames; class + epoch)
# --------------------------------------------------------------------------- #
def test_reinspect_happy(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    assert C.reinspect_and_require_identity(c.pkg_zip, c.receipt, I.RPM, tmp_root=str(tmp_path)) is None


@pytest.mark.parametrize("field,val", [
    ("version", "9.9.9"), ("package_class", "source"), ("epoch", 7), ("native_arch", "aarch64"),
])
def test_reinspect_identity_mismatch(tmp_path, monkeypatch, field, val):
    # keep the member SHA correct (so this isolates the header-identity check)
    def mut(r):
        r["members"][0][field] = val
        return r
    c = build_rpm_cell(tmp_path, monkeypatch, receipt_mutator=mut)
    with pytest.raises(C.EvidenceError) as ei:
        C.reinspect_and_require_identity(c.pkg_zip, c.receipt, I.RPM, tmp_root=str(tmp_path))
    assert ei.value.code == C.IDENTITY_MISMATCH


def test_reinspect_tool_failure_is_identity_mismatch(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    scr = tmp_path / "rpm_boom"; scr.write_text("#!/bin/sh\necho boom >&2\nexit 3\n"); scr.chmod(0o755)
    monkeypatch.setattr(I, "RPM_BIN", str(scr))
    with pytest.raises(C.EvidenceError) as ei:
        C.reinspect_and_require_identity(c.pkg_zip, c.receipt, I.RPM, tmp_root=str(tmp_path))
    assert ei.value.code == C.IDENTITY_MISMATCH


def test_reinspect_temp_dirs_cleaned_on_success_and_failure(tmp_path, monkeypatch):
    created = []
    real = C.tempfile.mkdtemp

    def tracking(*a, **k):
        d = real(*a, **k)
        created.append(d)
        return d
    monkeypatch.setattr(C.tempfile, "mkdtemp", tracking)

    ok = build_rpm_cell(tmp_path, monkeypatch)
    C.reinspect_and_require_identity(ok.pkg_zip, ok.receipt, I.RPM, tmp_root=str(tmp_path))
    bad = build_rpm_cell(tmp_path, monkeypatch, cell_id="c-bad", pkg_id=333, receipt_id=444,
                         receipt_mutator=lambda r: _mut(r, "version", "0.0.0"))
    with pytest.raises(C.EvidenceError):
        C.reinspect_and_require_identity(bad.pkg_zip, bad.receipt, I.RPM, tmp_root=str(tmp_path))
    assert created, "expected temp dirs to be created"
    assert all(not os.path.exists(d) for d in created), "temp dirs must be removed on success and failure"


def _mut(r, field, val):
    r["members"][0][field] = val
    return r


# --------------------------------------------------------------------------- #
# G. bounded / streaming reads + unbounded-read guard
# --------------------------------------------------------------------------- #
class _BoundedReader:
    """A seekable reader that FAILS if read() is called unbounded or over _CHUNK."""

    def __init__(self, data):
        self._b = io.BytesIO(data)

    def read(self, size=-1):
        assert isinstance(size, int) and 0 < size <= C._CHUNK, "unbounded/oversized read: %r" % (size,)
        return self._b.read(size)

    def seek(self, *a):
        return self._b.seek(*a)


def test_archive_sha256_uses_bounded_reads():
    data = os.urandom(3 * C._CHUNK + 17)
    assert C.archive_sha256(_BoundedReader(data)) == hashlib.sha256(data).hexdigest()
    # prove the guard is real: an unbounded read raises
    with pytest.raises(AssertionError):
        _BoundedReader(b"x").read()


def test_members_never_read_through_whole_file_call(tmp_path, monkeypatch):
    # If any code path used ZipFile.read(name) (whole-member buffering) the patched
    # method would raise; the full verify + reinspect must still pass via streaming.
    c = build_rpm_cell(tmp_path, monkeypatch)

    def _boom(self, *a, **k):
        raise AssertionError("ZipFile.read (whole-file) must not be used")
    monkeypatch.setattr(zipfile.ZipFile, "read", _boom)
    assert C.verify_package_artifact(c.pkg_zip, c.pkg_digest, c.receipt) is None
    assert C.reinspect_and_require_identity(c.pkg_zip, c.receipt, I.RPM, tmp_root=str(tmp_path)) is None


# --------------------------------------------------------------------------- #
# H. collect_cell_evidence: associations, suppression, systemic vs cell-local
# --------------------------------------------------------------------------- #
def _collect(cell, planned=None, family="rpm"):
    planned = planned if planned is not None else [cell.cell_id]
    fam = {cell.cell_id: family}
    return C.collect_cell_evidence(planned, cell.inv, cell.blobs, fam)


def test_collect_accepted(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    verified, verdicts, rc = _collect(c)
    assert len(verified) == 1 and rc == 1
    assert verdicts[0]["verdict"] == "accepted"
    assert verdicts[0]["package_artifact_id"] == c.pkg_id


def test_collect_absent_no_receipt(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    inv = [e for e in c.inv if not e["name"].startswith("pep-receipt-")]   # drop receipt artifact
    verified, verdicts, rc = C.collect_cell_evidence([c.cell_id], inv, c.blobs, {c.cell_id: "rpm"})
    assert verified == [] and rc == 0 and verdicts[0]["verdict"] == "absent"


def test_collect_expired_receipt_is_absent(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, receipt_expired=True)
    verified, verdicts, rc = _collect(c)
    assert verified == [] and rc == 0 and verdicts[0]["verdict"] == "absent"
    assert verdicts[0]["detail"] == "receipt artifact expired"


def test_collect_multiple_live_receipts_is_ambiguous(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    dup = dict(c.inv[1]); dup["id"] = 999999   # a second live receipt artifact for the same cell
    inv = c.inv + [dup]
    verified, verdicts, rc = C.collect_cell_evidence([c.cell_id], inv, c.blobs, {c.cell_id: "rpm"})
    assert verified == []
    assert verdicts[0]["verdict"] == "ambiguous"
    assert verdicts[0]["code"] == C.CELL_AMBIGUOUS_ASSOCIATIONS


def test_collect_valid_plus_invalid_suppresses_valid(tmp_path, monkeypatch):
    # Two receipt artifacts for one cell: one that would verify, one garbage. Both suppressed.
    c = build_rpm_cell(tmp_path, monkeypatch)
    bad = tmp_path / "garbage.zip"; bad.write_bytes(b"not a zip")
    bad_entry = {"id": 987654, "name": "pep-receipt-" + c.cell_id, "digest": _sha_file(str(bad)),
                 "expired": False, "size_in_bytes": 10, "created_at": "2026-01-01T00:00:00Z"}
    inv = c.inv + [bad_entry]
    blobs = dict(c.blobs); blobs[987654] = str(bad)
    verified, verdicts, rc = C.collect_cell_evidence([c.cell_id], inv, blobs, {c.cell_id: "rpm"})
    assert verified == [], "the valid candidate must not be retained from a valid+invalid pair"
    assert verdicts[0]["verdict"] == "ambiguous"


def test_collect_rejected_package_absent(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    inv = [e for e in c.inv if e["id"] != c.pkg_id]     # remove the package artifact
    verified, verdicts, rc = C.collect_cell_evidence([c.cell_id], inv, c.blobs, {c.cell_id: "rpm"})
    assert verified == []
    assert verdicts[0]["verdict"] == "rejected" and verdicts[0]["code"] == C.PACKAGE_ARTIFACT_ABSENT


def test_collect_rejected_package_expired(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, pkg_expired=True)
    verified, verdicts, rc = _collect(c)
    assert verdicts[0]["verdict"] == "rejected" and verdicts[0]["code"] == C.PACKAGE_ARTIFACT_EXPIRED


def test_collect_rejected_cell_id_mismatch(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch,
                       receipt_mutator=lambda r: {**r, "cell_id": "someone-else"})
    verified, verdicts, rc = _collect(c)
    assert verdicts[0]["verdict"] == "rejected" and verdicts[0]["code"] == C.RECEIPT_FIELD_INVALID


def test_collect_rejected_name_mismatch(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    c.inv[0]["name"] = "pkg-different-name"      # inventory package name != receipt.artifact_name
    verified, verdicts, rc = _collect(c)
    assert verdicts[0]["verdict"] == "rejected" and verdicts[0]["code"] == C.PACKAGE_BINDING_MISMATCH


def test_collect_rejected_package_name_carries_marker(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, aname="pkg [pep-cell.other]")
    verified, verdicts, rc = _collect(c)
    assert verdicts[0]["verdict"] == "rejected" and verdicts[0]["code"] == C.PACKAGE_BINDING_MISMATCH


def test_collect_missing_receipt_blob_is_systemic(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    blobs = dict(c.blobs); del blobs[c.receipt_id]
    with pytest.raises(C.CaptureSystemError):
        C.collect_cell_evidence([c.cell_id], c.inv, blobs, {c.cell_id: "rpm"})


def test_collect_missing_package_blob_is_systemic(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    blobs = dict(c.blobs); del blobs[c.pkg_id]
    with pytest.raises(C.CaptureSystemError):
        C.collect_cell_evidence([c.cell_id], c.inv, blobs, {c.cell_id: "rpm"})


def test_collect_unrelated_artifact_ignored(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    inv = c.inv + [{"id": 5, "name": "some-other-artifact", "digest": _D64, "expired": False,
                    "size_in_bytes": 1, "created_at": "2026-01-01T00:00:00Z"}]
    verified, verdicts, rc = C.collect_cell_evidence([c.cell_id], inv, c.blobs, {c.cell_id: "rpm"})
    assert len(verified) == 1 and len(verdicts) == 1     # unrelated artifact does not add a cell


# --------------------------------------------------------------------------- #
# I. capture_to_reducer_input end-to-end + honest reducer mapping
# --------------------------------------------------------------------------- #
def _det(cells):
    return {"include": [{"cell_id": c, "family": "rpm", "os": "el-9", "normalized_arch": "amd64"}
                        for c in cells]}


def _job(cid, job_id, attempt=1, conclusion="success", status="completed"):
    return {"id": job_id, "name": "build [pep-cell:%s]" % cid, "run_attempt": attempt,
            "status": status, "conclusion": conclusion}


def _pages(items, key):
    return [{"total_count": len(items), key: items}]


_RI = {"channel": "staging", "effective_tag": "t", "intended_version": "0.0.0",
       "intended_buildnum": "0", "logical_component": "spike", "simulated": False}
_POL = {"allowed_runtime_package_names": [], "expected_binary_version": ""}
_PROV = {"repository": "pgEdge/x", "run_id": "1", "run_attempt": "1", "captured_at": "2026-09-15T00:00:00Z"}


def _capture(cell, jobs, monkeypatch, blobs=None, inv=None):
    return C.capture_to_reducer_input(
        detector_matrices=[_det([cell.cell_id])],
        job_pages=_pages(jobs, "jobs"),
        artifact_pages=_pages(inv if inv is not None else cell.inv, "artifacts"),
        blobs=blobs if blobs is not None else cell.blobs,
        release_intent=_RI, component_policy=_POL, publication_results={"rpm": "success"},
        provenance=_PROV)


def test_capture_accepted_reduces_to_available(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    env, ev = _capture(c, [_job(c.cell_id, 900001)], monkeypatch)
    plan = R.reduce(env)
    cell = {x["cell_id"]: x for x in plan["cells"]}[c.cell_id]
    assert cell["build_state"] == "available"
    assert ev["schema"] == "capture-evidence/1"
    assert ev["counts"]["accepted_receipt_cells"] == 1
    assert ev["counts"]["verified_package_artifacts"] == 1
    assert ev["counts"]["verified_members"] == 1


def test_capture_rejected_receipt_reduces_to_incomplete(tmp_path, monkeypatch):
    # a rejected receipt (package absent) + a successful job -> reducer 'incomplete'
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    inv = [e for e in c.inv if e["id"] != c.pkg_id]
    env, ev = _capture(c, [_job(c.cell_id, 900002)], monkeypatch, inv=inv)
    plan = R.reduce(env)
    cell = {x["cell_id"]: x for x in plan["cells"]}[c.cell_id]
    assert cell["build_state"] == "incomplete"
    assert ev["counts"]["rejected_receipt_cells"] == 1
    assert not any(t.get("eligibility") == "eligible" for t in cell["targets"])


def test_capture_rejected_receipt_no_job_reduces_to_never_ran(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    inv = [e for e in c.inv if e["id"] != c.pkg_id]
    env, ev = _capture(c, [], monkeypatch, inv=inv)
    plan = R.reduce(env)
    cell = {x["cell_id"]: x for x in plan["cells"]}[c.cell_id]
    assert cell["build_state"] == "never_ran"


def test_capture_ambiguous_associations_reduce_to_incomplete_not_ambiguous(tmp_path, monkeypatch):
    # Honest mapping: capture suppresses conflicting receipts (no artifact record), so the
    # UNCHANGED reducer reports 'incomplete' (with a successful job), NOT 'ambiguous'.
    # Capture-evidence retains the more precise ambiguity. No eligible target either way.
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    dup = dict(c.inv[1]); dup["id"] = 424242
    inv = c.inv + [dup]
    env, ev = _capture(c, [_job(c.cell_id, 900003)], monkeypatch, inv=inv)
    plan = R.reduce(env)
    cell = {x["cell_id"]: x for x in plan["cells"]}[c.cell_id]
    assert cell["build_state"] == "incomplete"           # NOT 'ambiguous'
    assert ev["counts"]["ambiguous_receipt_cells"] == 1  # evidence keeps the precise verdict
    assert not any(t.get("eligibility") == "eligible" for t in cell["targets"])


def test_capture_evidence_has_no_reducer_result_fields(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    _env, ev = _capture(c, [_job(c.cell_id, 900004)], monkeypatch)
    assert "plan_resolved" not in ev
    for forbidden in ("available_build_cells", "eligible_targets", "selected_targets", "plan_resolved"):
        assert forbidden not in ev["counts"]
    assert ev["provenance"]["captured_at"] == "2026-09-15T00:00:00Z"


def test_capture_evidence_is_credential_free(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    env, ev = _capture(c, [_job(c.cell_id, 900005)], monkeypatch)
    blob = (json.dumps(ev) + json.dumps(env)).lower()
    for secret in ("http", "download_url", "authorization", "token", "sig=", "?"):
        assert secret not in blob, "capture output must not carry %r" % secret


def test_capture_systemic_on_bad_pagination(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    bad_job_pages = [{"total_count": 5, "jobs": [_job(c.cell_id, 900006)]}]   # count != items
    with pytest.raises(C.CaptureSystemError):
        C.capture_to_reducer_input(
            detector_matrices=[_det([c.cell_id])], job_pages=bad_job_pages,
            artifact_pages=_pages(c.inv, "artifacts"), blobs=c.blobs,
            release_intent=_RI, component_policy=_POL,
            publication_results={"rpm": "success"}, provenance=_PROV)


def test_capture_systemic_on_bad_provenance(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    with pytest.raises(C.CaptureSystemError):
        C.capture_to_reducer_input(
            detector_matrices=[_det([c.cell_id])], job_pages=_pages([_job(c.cell_id, 1)], "jobs"),
            artifact_pages=_pages(c.inv, "artifacts"), blobs=c.blobs,
            release_intent=_RI, component_policy=_POL, publication_results={"rpm": "success"},
            provenance={"nested": {"url": "https://x"}})    # non-scalar provenance value


def test_capture_mixed_accepted_and_rejected_in_one_run(tmp_path, monkeypatch):
    good = build_rpm_cell(tmp_path, monkeypatch, cell_id="cell-good", pkg_id=11, receipt_id=12,
                          member_path="good.x86_64.rpm", body=b"GOOD")
    bad = build_rpm_cell(tmp_path, monkeypatch, cell_id="cell-bad", pkg_id=21, receipt_id=22,
                         member_path="bad.x86_64.rpm", body=b"BAD")
    bad_inv = [e for e in bad.inv if e["id"] != bad.pkg_id]     # bad cell: package absent
    inv = good.inv + bad_inv
    blobs = dict(good.blobs); blobs.update(bad.blobs)
    env, ev = C.capture_to_reducer_input(
        detector_matrices=[_det(["cell-good", "cell-bad"])],
        job_pages=_pages([_job("cell-good", 31), _job("cell-bad", 32)], "jobs"),
        artifact_pages=_pages(inv, "artifacts"), blobs=blobs,
        release_intent=_RI, component_policy=_POL, publication_results={"rpm": "success"},
        provenance=_PROV)
    assert ev["counts"]["accepted_receipt_cells"] == 1
    assert ev["counts"]["rejected_receipt_cells"] == 1
    plan = R.reduce(env)
    byid = {x["cell_id"]: x for x in plan["cells"]}
    assert byid["cell-good"]["build_state"] == "available"
    assert byid["cell-bad"]["build_state"] == "incomplete"


# --------------------------------------------------------------------------- #
# J. determinism + serialization
# --------------------------------------------------------------------------- #
def test_capture_is_deterministic(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    jobs = [_job(c.cell_id, 900007)]
    env1, ev1 = _capture(c, jobs, monkeypatch)
    env2, ev2 = _capture(c, jobs, monkeypatch)
    assert C.capture_evidence_to_json(ev1) == C.capture_evidence_to_json(ev2)
    assert json.dumps(env1, sort_keys=True) == json.dumps(env2, sort_keys=True)


def test_capture_evidence_ordering(tmp_path, monkeypatch):
    a = build_rpm_cell(tmp_path, monkeypatch, cell_id="zeta", pkg_id=41, receipt_id=42,
                       member_path="z.x86_64.rpm", body=b"Z")
    b = build_rpm_cell(tmp_path, monkeypatch, cell_id="alpha", pkg_id=51, receipt_id=52,
                       member_path="a.x86_64.rpm", body=b"A")
    inv = a.inv + b.inv
    blobs = dict(a.blobs); blobs.update(b.blobs)
    _env, ev = C.capture_to_reducer_input(
        detector_matrices=[_det(["zeta", "alpha"])],
        job_pages=_pages([_job("zeta", 61), _job("alpha", 62)], "jobs"),
        artifact_pages=_pages(inv, "artifacts"), blobs=blobs,
        release_intent=_RI, component_policy=_POL, publication_results={"rpm": "success"},
        provenance=_PROV)
    assert [c["cell_id"] for c in ev["cells"]] == ["alpha", "zeta"]         # sorted by cell_id
    ids = [e["id"] for e in ev["live_inventory"]]
    assert ids == sorted(ids)                                               # sorted by id


# --------------------------------------------------------------------------- #
# K. real Spike-0 rerun mechanics: capture -> adapter -> reducer
# --------------------------------------------------------------------------- #
def _load_fixture(name):
    return json.loads((FX / name).read_text())


def _run_mechanics(fixture_name, tmp_path, monkeypatch):
    """Render the preserved rerun-mechanics fixture into capture inputs (raw jobs +
    inventory + verified receipts for the SURVIVING artifacts), run capture -> reduce,
    and return (captured_plan, baseline_plan) so a test can assert they agree per cell.
    The job timeline and surviving-artifact set are used verbatim; only the omitted
    package bytes are synthesized (fake tooling)."""
    fx = _load_fixture(fixture_name)
    planned_cells = fx["planned_cells"]
    cids = [c["cell_id"] for c in planned_cells]
    detector = {"include": [{"cell_id": c["cell_id"], "family": c["family"], "os": c["os"],
                             "normalized_arch": c["normalized_arch"]} for c in planned_cells]}
    # raw jobs from the exact fixture job timeline (unique ids preserved)
    jobs = [{"id": jr["job_id"], "name": "build [pep-cell:%s]" % jr["cell_id"],
             "run_attempt": jr["run_attempt"], "status": jr["status"], "conclusion": jr["conclusion"]}
            for jr in fx["job_records"]]
    # one verified package + receipt per SURVIVING fixture artifact
    inv, blobs = [], {}
    use_fake_rpm(tmp_path, monkeypatch)
    for i, art in enumerate(fx["artifacts"]):
        # map the fixture artifact_name 'pkg-<cell>' back to its cell via planned_cells
        cell_id = next(c["cell_id"] for c in planned_cells if c["artifact_name"] == art["name"])
        pkg_id = art["id"]
        receipt_id = 9_000_000_000_000 + i
        mp = "pkg-%s.x86_64.rpm" % cell_id
        d = tmp_path / ("m-%s" % cell_id); d.mkdir()
        fpath = _write_pkg(d, mp, I._RPM_MAGIC, body=("%s-BODY" % cell_id).encode())
        members = I.inspect_members([(fpath, mp)], expected_family=I.RPM)
        pkg_zip = _zip_paths(tmp_path / ("mpkg-%s.zip" % cell_id), {mp: fpath})
        pkg_digest = _sha_file(pkg_zip)
        aname = "pkg-%s" % cell_id
        receipt = {"schema": "pep-receipt/2", "cell_id": cell_id, "artifact_id": pkg_id,
                   "artifact_name": aname, "archive_digest": pkg_digest, "members": members}
        rjson = d / "receipt.json"; rjson.write_text(json.dumps(receipt))
        receipt_zip = _zip_paths(tmp_path / ("mrec-%s.zip" % cell_id), {"receipt.json": str(rjson)})
        inv.append({"id": pkg_id, "name": aname, "digest": "sha256:" + pkg_digest, "expired": False,
                    "size_in_bytes": os.path.getsize(pkg_zip), "created_at": "2026-01-01T00:00:00Z"})
        inv.append({"id": receipt_id, "name": "pep-receipt-" + cell_id,
                    "digest": "sha256:" + _sha_file(receipt_zip), "expired": False,
                    "size_in_bytes": os.path.getsize(receipt_zip), "created_at": "2026-01-01T00:00:00Z"})
        blobs[pkg_id] = pkg_zip
        blobs[receipt_id] = receipt_zip
    env, ev = C.capture_to_reducer_input(
        detector_matrices=[detector], job_pages=[{"total_count": len(jobs), "jobs": jobs}],
        artifact_pages=[{"total_count": len(inv), "artifacts": inv}], blobs=blobs,
        release_intent=fx["release_intent"], component_policy=fx["component_policy"],
        publication_results=fx["publication_results"], provenance=fx["provenance"])
    return R.reduce(env), R.reduce(fx), cids, ev


@pytest.mark.parametrize("fixture", ["spike0_attempt2.json", "spike0_attempt3.json"])
def test_rerun_mechanics_flow_matches_baseline(fixture, tmp_path, monkeypatch):
    captured, baseline, cids, ev = _run_mechanics(fixture, tmp_path, monkeypatch)
    cap = {c["cell_id"]: c["build_state"] for c in captured["cells"]}
    base = {c["cell_id"]: c["build_state"] for c in baseline["cells"]}
    assert cap == base, "capture->adapter->reducer must reproduce the rerun build states"
    # every surviving artifact cell is verified; a dropped artifact is not fabricated
    assert ev["counts"]["verified_package_artifacts"] == len(_load_fixture(fixture)["artifacts"])


def test_rerun_attempt3_drops_failed_full_a(tmp_path, monkeypatch):
    # attempt-3 re-run-all deleted pkg-full-A and full-A's latest job failed -> 'failed';
    # the other cells carry forward as 'available'. Proves the mechanics survive capture.
    captured, baseline, cids, ev = _run_mechanics("spike0_attempt3.json", tmp_path, monkeypatch)
    cap = {c["cell_id"]: c["build_state"] for c in captured["cells"]}
    assert cap["full-A"] == "failed"
    assert cap["fj-A"] == "available" and cap["fj-B"] == "available" and cap["full-B"] == "available"


# --------------------------------------------------------------------------- #
# M. receipt-artifact expiry must fail closed (never silently treated as expired)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_expiry", ["yes", None, 1, 0, 1.5, [], {}, "false"])
def test_collect_malformed_receipt_expiry_alone_rejected(tmp_path, monkeypatch, bad_expiry):
    c = build_rpm_cell(tmp_path, monkeypatch)
    c.inv[1]["expired"] = bad_expiry               # malformed receipt-artifact expiry
    verified, verdicts, rc = _collect(c)
    assert verified == [] and rc == 0
    assert verdicts[0]["verdict"] == "rejected" and verdicts[0]["code"] == C.RECEIPT_EXPIRY_MALFORMED


def test_collect_missing_receipt_expiry_key_rejected(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    del c.inv[1]["expired"]                          # missing expiry is malformed, not "expired"
    verified, verdicts, rc = _collect(c)
    assert verdicts[0]["verdict"] == "rejected" and verdicts[0]["code"] == C.RECEIPT_EXPIRY_MALFORMED


def test_collect_malformed_expiry_beside_live_is_ambiguous(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    dup = dict(c.inv[1]); dup["id"] = 987001; dup["expired"] = "maybe"   # malformed sibling
    inv = c.inv + [dup]
    verified, verdicts, rc = C.collect_cell_evidence([c.cell_id], inv, c.blobs, {c.cell_id: "rpm"})
    assert verified == [], "a live receipt beside a malformed one must not be accepted"
    assert verdicts[0]["verdict"] == "ambiguous" and verdicts[0]["code"] == C.RECEIPT_EXPIRY_MALFORMED


def test_collect_live_plus_genuinely_expired_is_acceptable(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch)
    gone = dict(c.inv[1]); gone["id"] = 987002; gone["expired"] = True   # genuinely expired sibling
    inv = c.inv + [gone]
    verified, verdicts, rc = C.collect_cell_evidence([c.cell_id], inv, c.blobs, {c.cell_id: "rpm"})
    assert len(verified) == 1 and verdicts[0]["verdict"] == "accepted"


def test_capture_malformed_receipt_expiry_via_sanitize(tmp_path, monkeypatch):
    # A raw non-boolean expiry survives sanitization as null -> malformed -> rejected.
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    raw_inv = [dict(e) for e in c.inv]
    for e in raw_inv:
        if e["name"].startswith("pep-receipt-"):
            e["expired"] = "sometimes"
    env, ev = _capture(c, [_job(c.cell_id, 900101)], monkeypatch, inv=raw_inv)
    verdict = {x["cell_id"]: x for x in ev["cells"]}[c.cell_id]
    assert verdict["verdict"] == "rejected" and verdict["code"] == C.RECEIPT_EXPIRY_MALFORMED
    assert R.reduce(env)["cells"][0]["build_state"] in ("incomplete", "never_ran")


# --------------------------------------------------------------------------- #
# N. ZIP member read failures normalized to stable EvidenceError (receipt/package-local)
# --------------------------------------------------------------------------- #
def test_verify_package_crc_corruption_fails_during_member_read(tmp_path, monkeypatch):
    # Outer archive digest is recomputed AFTER corruption, so the archive gate passes and
    # the failure must occur while reading the corrupted member (bad CRC).
    c = build_rpm_cell(tmp_path, monkeypatch, body=b"AAA-CRCMARKER-BBB",
                       corrupt_pkg=(b"CRCMARKER", b"CRCMARK3R"))
    assert C.archive_sha256(c.pkg_zip) == c.pkg_digest          # archive hashing itself succeeds
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_package_artifact(c.pkg_zip, c.pkg_digest, c.receipt)
    assert ei.value.code == C.PACKAGE_ZIP_UNSAFE


def test_verify_receipt_crc_corruption_is_receipt_local(tmp_path):
    rdir = tmp_path / "r"; rdir.mkdir()
    rjson = rdir / "receipt.json"; rjson.write_bytes(b'{"marker":"RCMARK-ZONE","x":1}')
    z = _zip_paths(tmp_path / "r.zip", {"receipt.json": str(rjson)}, compression=zipfile.ZIP_STORED)
    _corrupt(z, b"RCMARK-ZONE", b"RCMARK-Z0NE")
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_receipt_artifact(z, _sha_file(z))              # digest recomputed post-corruption
    assert ei.value.code == C.RECEIPT_ZIP_UNSAFE


def _always_raise(exc):
    def _f(*a, **k):
        raise exc
    return _f


# The exact exception TYPES zipfile raises for encrypted (RuntimeError), unsupported
# compression (NotImplementedError), CRC/format (BadZipFile), truncation (EOFError) and
# a broken deflate stream (zlib.error) must all normalize to the stable unsafe code.
_MEMBER_READ_EXCEPTIONS = [
    RuntimeError("File is encrypted, password required"),
    NotImplementedError("That compression method is not supported"),
    zipfile.BadZipFile("Bad CRC-32"),
    EOFError(),
    zlib.error("error -3 while decompressing"),
]


@pytest.mark.parametrize("exc", _MEMBER_READ_EXCEPTIONS)
def test_package_member_read_failure_normalized(tmp_path, monkeypatch, exc):
    c = build_rpm_cell(tmp_path, monkeypatch)
    monkeypatch.setattr(zipfile.ZipFile, "open", _always_raise(exc))
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_package_artifact(c.pkg_zip, c.pkg_digest, c.receipt)
    assert ei.value.code == C.PACKAGE_ZIP_UNSAFE


@pytest.mark.parametrize("exc", _MEMBER_READ_EXCEPTIONS)
def test_receipt_member_read_failure_normalized(tmp_path, monkeypatch, exc):
    c = build_rpm_cell(tmp_path, monkeypatch)
    monkeypatch.setattr(zipfile.ZipFile, "open", _always_raise(exc))
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_receipt_artifact(c.receipt_zip, c.receipt_digest)
    assert ei.value.code == C.RECEIPT_ZIP_UNSAFE


def test_collect_crc_corrupted_package_does_not_abort_valid_cell(tmp_path, monkeypatch):
    good = build_rpm_cell(tmp_path, monkeypatch, cell_id="cell-ok", pkg_id=61, receipt_id=62,
                          member_path="ok.x86_64.rpm", body=b"OK")
    bad = build_rpm_cell(tmp_path, monkeypatch, cell_id="cell-crc", pkg_id=71, receipt_id=72,
                         member_path="crc.x86_64.rpm", body=b"AAA-CRCMARKER-BBB",
                         corrupt_pkg=(b"CRCMARKER", b"CRCMARK3R"))
    inv = good.inv + bad.inv
    blobs = dict(good.blobs); blobs.update(bad.blobs)
    verified, verdicts, rc = C.collect_cell_evidence(
        ["cell-crc", "cell-ok"], inv, blobs, {"cell-ok": "rpm", "cell-crc": "rpm"})
    byid = {v["cell_id"]: v for v in verdicts}
    assert byid["cell-ok"]["verdict"] == "accepted"
    assert byid["cell-crc"]["verdict"] == "rejected" and byid["cell-crc"]["code"] == C.PACKAGE_ZIP_UNSAFE
    assert [c for (c, _r, _i) in verified] == ["cell-ok"]


# --------------------------------------------------------------------------- #
# O. never persist a temporary extraction path in evidence
# --------------------------------------------------------------------------- #
def test_reinspect_detail_omits_temp_path(tmp_path, monkeypatch):
    # deb package inspected under expected rpm -> InspectError embeds the temp path; the
    # emitted detail must carry only a fixed reason + the safe member path.
    c = build_cell(tmp_path, monkeypatch, family="deb", cell_id="c-deb", pkg_id=91, receipt_id=92)
    with pytest.raises(C.EvidenceError) as ei:
        C.reinspect_and_require_identity(c.pkg_zip, c.receipt, I.RPM, tmp_root=str(tmp_path))
    assert ei.value.code == C.IDENTITY_MISMATCH
    assert str(tmp_path) not in ei.value.detail and "pep-capture-" not in ei.value.detail
    assert c.member_path in ei.value.detail


def test_capture_evidence_free_of_temp_path_on_tool_failure(tmp_path, monkeypatch):
    # A cell whose detector family (rpm) disagrees with a deb package -> rejected via
    # re-inspection. Serialized evidence must not leak the temp extraction root.
    c = build_cell(tmp_path, monkeypatch, family="deb", cell_id="rag-rpm-el9-amd64",
                   pkg_id=95, receipt_id=96)
    detector = {"include": [{"cell_id": c.cell_id, "family": "rpm", "os": "el-9",
                             "normalized_arch": "amd64"}]}
    env, ev = C.capture_to_reducer_input(
        detector_matrices=[detector], job_pages=_pages([_job(c.cell_id, 820)], "jobs"),
        artifact_pages=_pages(c.inv, "artifacts"), blobs=c.blobs, tmp_root=str(tmp_path),
        release_intent=_RI, component_policy=_POL, publication_results={"rpm": "success"},
        provenance=_PROV)
    verdict = {x["cell_id"]: x for x in ev["cells"]}[c.cell_id]
    assert verdict["verdict"] == "rejected" and verdict["code"] == C.IDENTITY_MISMATCH
    serialized = C.capture_evidence_to_json(ev)
    assert str(tmp_path) not in serialized and "pep-capture-" not in serialized


# --------------------------------------------------------------------------- #
# P. provenance must stay valid JSON (reject NaN / +Inf / -Inf; allow_nan backstop)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("val", [float("nan"), float("inf"), float("-inf")])
def test_provenance_non_finite_rejected(val):
    with pytest.raises(C.CaptureSystemError):
        C.capture_to_reducer_input(
            detector_matrices=[{"include": []}], job_pages=[{"total_count": 0, "jobs": []}],
            artifact_pages=[{"total_count": 0, "artifacts": []}], blobs={},
            release_intent={"channel": "staging", "simulated": False},
            component_policy={"allowed_runtime_package_names": []},
            publication_results={}, provenance={"captured_at": "t", "bad": val})


def test_capture_evidence_to_json_allow_nan_backstop():
    with pytest.raises(ValueError):
        C.capture_evidence_to_json({"schema": "capture-evidence/1", "x": float("nan")})


# --------------------------------------------------------------------------- #
# Q. positive-path coverage: DEB end-to-end + multi-member (runtime + source)
# --------------------------------------------------------------------------- #
def test_deb_capture_reduces_to_available(tmp_path, monkeypatch):
    c = build_cell(tmp_path, monkeypatch, family="deb", cell_id="rag-deb-noble-arm64",
                   pkg_id=311, receipt_id=312)
    detector = {"include": [{"cell_id": c.cell_id, "family": "deb", "os": "noble",
                             "normalized_arch": "arm64"}]}
    env, ev = C.capture_to_reducer_input(
        detector_matrices=[detector], job_pages=_pages([_job(c.cell_id, 810)], "jobs"),
        artifact_pages=_pages(c.inv, "artifacts"), blobs=c.blobs,
        release_intent=_RI, component_policy=_POL, publication_results={"deb": "success"},
        provenance=_PROV)
    assert ev["counts"]["accepted_receipt_cells"] == 1 and ev["counts"]["verified_members"] == 1
    assert len(env["artifacts"]) == 1 and len(env["artifacts"][0]["members"]) == 1
    plan = R.reduce(env)
    cell = {x["cell_id"]: x for x in plan["cells"]}[c.cell_id]
    assert cell["build_state"] == "available"


def test_multi_member_runtime_plus_source_capture(tmp_path, monkeypatch):
    _fake_rpm_router(tmp_path, monkeypatch)
    cid = "rag-rpm-el9-amd64"
    d = tmp_path / "src"; d.mkdir()
    mp_run = "pgedge-rag-server2-2.0.0-1.el9.x86_64.rpm"
    mp_src = "pgedge-rag-server2-2.0.0-1.el9.src.rpm"
    f_run = _write_pkg(d, mp_run, I._RPM_MAGIC, b"RUNTIME-BODY")
    f_src = _write_pkg(d, mp_src, I._RPM_MAGIC, b"SOURCE-BODY")
    members = I.inspect_members([(f_run, mp_run), (f_src, mp_src)], expected_family=I.RPM)
    assert {m["package_class"] for m in members} == {"runtime", "source"}
    pkg_zip = _zip_paths(tmp_path / "pkg.zip", {mp_run: f_run, mp_src: f_src})
    pkg_digest = _sha_file(pkg_zip)
    aname = "pkg-" + cid
    receipt = {"schema": "pep-receipt/2", "cell_id": cid, "artifact_id": 700,
               "artifact_name": aname, "archive_digest": pkg_digest, "members": members}
    rdir = tmp_path / "rc"; rdir.mkdir()
    rjson = rdir / "receipt.json"; rjson.write_text(json.dumps(receipt))
    receipt_zip = _zip_paths(tmp_path / "rec.zip", {"receipt.json": str(rjson)})
    inv = [{"id": 700, "name": aname, "digest": "sha256:" + pkg_digest, "expired": False,
            "size_in_bytes": os.path.getsize(pkg_zip), "created_at": "2026-01-01T00:00:00Z"},
           {"id": 701, "name": "pep-receipt-" + cid, "digest": "sha256:" + _sha_file(receipt_zip),
            "expired": False, "size_in_bytes": 1, "created_at": "2026-01-01T00:00:00Z"}]
    blobs = {700: pkg_zip, 701: receipt_zip}
    env, ev = C.capture_to_reducer_input(
        detector_matrices=[{"include": [{"cell_id": cid, "family": "rpm", "os": "el-9",
                                         "normalized_arch": "amd64"}]}],
        job_pages=_pages([_job(cid, 800)], "jobs"), artifact_pages=_pages(inv, "artifacts"),
        blobs=blobs, release_intent=_RI, component_policy=_POL,
        publication_results={"rpm": "success"}, provenance=_PROV)
    assert ev["counts"]["verified_package_artifacts"] == 1
    assert ev["counts"]["verified_members"] == 2            # both members independently reinspected
    assert len(env["artifacts"]) == 1 and len(env["artifacts"][0]["members"]) == 2
    plan = R.reduce(env)
    cell = {x["cell_id"]: x for x in plan["cells"]}[cid]
    assert cell["build_state"] == "available"
    assert {m["package_class"] for m in cell["members"]} == {"runtime", "source"}


# --------------------------------------------------------------------------- #
# R. package member expansion is bounded by the inspector's EXISTING ceiling
# --------------------------------------------------------------------------- #
def test_member_hashing_bounded_by_inspector_ceiling(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, body=b"0123456789ABCDEF")   # ~19-byte member
    monkeypatch.setattr(C, "_CHUNK", 4)                 # force multi-chunk streaming
    monkeypatch.setattr(I, "_MAX_PKG_BYTES", 8)         # reuse the inspector's OWN ceiling
    with pytest.raises(C.EvidenceError) as ei:
        C.verify_package_artifact(c.pkg_zip, c.pkg_digest, c.receipt)
    assert ei.value.code == C.PACKAGE_ZIP_UNSAFE
    assert "ceiling" in ei.value.detail


def test_member_extraction_bounded_by_inspector_ceiling(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, body=b"0123456789ABCDEF")
    monkeypatch.setattr(C, "_CHUNK", 4)
    monkeypatch.setattr(I, "_MAX_PKG_BYTES", 8)
    with pytest.raises(C.EvidenceError) as ei:
        C.reinspect_and_require_identity(c.pkg_zip, c.receipt, I.RPM, tmp_root=str(tmp_path))
    assert ei.value.code == C.PACKAGE_ZIP_UNSAFE


# --------------------------------------------------------------------------- #
# S. local capture I/O failures -> sanitized CaptureSystemError at the boundary
# --------------------------------------------------------------------------- #
def _capture_full(cell, jobs, tmp_root=None, inv=None, blobs=None):
    return C.capture_to_reducer_input(
        detector_matrices=[_det([cell.cell_id])], job_pages=_pages(jobs, "jobs"),
        artifact_pages=_pages(inv if inv is not None else cell.inv, "artifacts"),
        blobs=blobs if blobs is not None else cell.blobs, tmp_root=tmp_root,
        release_intent=_RI, component_policy=_POL, publication_results={"rpm": "success"},
        provenance=_PROV)


def _assert_sanitized(err, *forbidden):
    assert err.detail == "local capture I/O failed"        # fixed, bounded message
    blob = str(err)
    for f in forbidden:
        assert f not in blob and f not in err.detail
    assert "pep-capture-" not in blob


def test_boundary_invalid_tmp_root_is_systemic(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    bad_root = str(tmp_path / "no-such-dir")
    with pytest.raises(C.CaptureSystemError) as ei:
        _capture_full(c, [_job(c.cell_id, 900201)], tmp_root=bad_root)
    _assert_sanitized(ei.value, bad_root, str(tmp_path))


def test_boundary_missing_local_artifact_is_systemic(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    os.remove(c.pkg_zip)                                    # blob path still referenced but gone
    with pytest.raises(C.CaptureSystemError) as ei:
        _capture_full(c, [_job(c.cell_id, 900202)])
    _assert_sanitized(ei.value, c.pkg_zip, str(tmp_path))


def test_boundary_extraction_write_failure_is_systemic(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    real_open = open

    def faulty_open(path, mode="r", *a, **k):
        if "w" in mode:                                    # fail only the extraction write
            raise OSError("simulated disk-full during extraction")
        return real_open(path, mode, *a, **k)
    # shadow the builtin used inside pep_capture (module global takes precedence at lookup)
    monkeypatch.setattr(C, "open", faulty_open, raising=False)
    with pytest.raises(C.CaptureSystemError) as ei:
        _capture_full(c, [_job(c.cell_id, 900203)], tmp_root=str(tmp_path))
    _assert_sanitized(ei.value, str(tmp_path))


def test_boundary_cleanup_failure_prevents_success(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    real_rmtree = C.shutil.rmtree

    def faulty_rmtree(path, ignore_errors=False, **k):
        if ignore_errors:
            return real_rmtree(path, ignore_errors=True)   # honor best-effort (no leftover dirs)
        raise OSError("simulated cleanup failure")         # strict success-path cleanup fails
    monkeypatch.setattr(C.shutil, "rmtree", faulty_rmtree)
    with pytest.raises(C.CaptureSystemError) as ei:
        _capture_full(c, [_job(c.cell_id, 900204)], tmp_root=str(tmp_path))
    _assert_sanitized(ei.value, str(tmp_path))


def test_boundary_crc_corrupt_package_stays_cell_local(tmp_path, monkeypatch):
    # A CRC-corrupt package member is UNTRUSTED evidence: it must stay a cell-local
    # rejected verdict, never a systemic CaptureSystemError.
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64",
                       body=b"AAA-CRCMARKER-BBB", corrupt_pkg=(b"CRCMARKER", b"CRCMARK3R"))
    env, ev = _capture_full(c, [_job(c.cell_id, 900205)], tmp_root=str(tmp_path))
    verdict = {x["cell_id"]: x for x in ev["cells"]}[c.cell_id]
    assert verdict["verdict"] == "rejected" and verdict["code"] == C.PACKAGE_ZIP_UNSAFE
    assert ev["counts"]["verified_package_artifacts"] == 0


def test_boundary_crc_corrupt_receipt_stays_cell_local(tmp_path, monkeypatch):
    c = build_rpm_cell(tmp_path, monkeypatch, cell_id="rag-rpm-el9-amd64")
    # rebuild the receipt ZIP as STORED with a corruptible marker, corrupt a member byte,
    # and re-point the inventory digest so the archive gate passes and the failure is in the
    # receipt member READ.
    rdir = tmp_path / "rc2"; rdir.mkdir()
    rjson = rdir / "receipt.json"
    rjson.write_bytes(json.dumps(c.receipt).encode() + b" RCMARKERZONE")
    z = _zip_paths(tmp_path / "rc2.zip", {"receipt.json": str(rjson)}, compression=zipfile.ZIP_STORED)
    _corrupt(z, b"RCMARKERZONE", b"RCMARKERZ0NE")
    new_digest = _sha_file(z)
    for e in c.inv:
        if e["name"].startswith("pep-receipt-"):
            e["digest"] = "sha256:" + new_digest
    c.blobs[c.receipt_id] = z
    env, ev = _capture_full(c, [_job(c.cell_id, 900206)], tmp_root=str(tmp_path))
    verdict = {x["cell_id"]: x for x in ev["cells"]}[c.cell_id]
    assert verdict["verdict"] == "rejected" and verdict["code"] == C.RECEIPT_ZIP_UNSAFE


# --------------------------------------------------------------------------- #
# L. static no-network / no-clock guard on the module source
# --------------------------------------------------------------------------- #
def test_module_has_no_network_clock_or_extractall():
    # AST-based (not text-based) so docstrings that MENTION these words don't trip it:
    # the module must not IMPORT any network/clock/subprocess module, nor ACCESS a
    # forbidden attribute (extractall / os.environ / os.getenv / clock reads).
    import ast
    tree = ast.parse(Path(C.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    banned_mods = {"urllib", "requests", "socket", "http", "httplib", "ssl", "asyncio",
                   "datetime", "time", "subprocess", "select", "ftplib", "smtplib"}
    assert not (imported & banned_mods), "pep_capture imports forbidden module(s): %s" % (imported & banned_mods)
    attr_names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("extractall", "environ", "getenv", "getenvb", "popen", "system"):
        assert banned not in attr_names, "pep_capture must not access .%s" % banned
