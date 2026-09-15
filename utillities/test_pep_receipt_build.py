"""Repeatable, stdlib-only tests for the pep-package-receipt composite action's
builder (.github/actions/pep-package-receipt/receipt_build.py).

Uses fake `rpm`/`dpkg-deb` executables and magic-byte fake package files (the same
technique as test_pep_pkg_inspect.py), so NO real rpm/dpkg is needed and no binary
package is committed. Covers the pep-receipt/2 contract and every fail-closed case.
"""
import json
import sys
from pathlib import Path

import pytest

import pep_pkg_inspect as I

# Import the composite action's builder from its action directory.
_ACTION_DIR = Path(__file__).resolve().parents[1] / ".github" / "actions" / "pep-package-receipt"
sys.path.insert(0, str(_ACTION_DIR))
import receipt_build as R  # noqa: E402

_GOLDEN = json.loads((Path(__file__).parent / "pkg_inspect_fixtures" / "golden.json").read_text())
_CASES = _GOLDEN["cases"]

_D64 = "ab" * 32   # a valid 64-hex digest


# --- fake tooling ------------------------------------------------------------
def _fake_tool(tmp_path, name, output_text):
    out = tmp_path / (name + ".out")
    out.write_bytes(output_text.encode("utf-8"))
    scr = tmp_path / name
    scr.write_text('#!/bin/sh\ncat %s\n' % json.dumps(str(out)))
    scr.chmod(0o755)
    return str(scr)


def _fake_rpm_router(tmp_path, runtime_out, source_out):
    """A fake rpm that emits source output for a *.src.rpm path, else runtime."""
    ro = tmp_path / "rpm_runtime.out"; ro.write_bytes(runtime_out.encode("utf-8"))
    so = tmp_path / "rpm_source.out"; so.write_bytes(source_out.encode("utf-8"))
    scr = tmp_path / "rpm"
    scr.write_text(
        '#!/bin/sh\npkg=""\nfor a in "$@"; do pkg="$a"; done\n'
        'case "$pkg" in\n  *.src.rpm) cat %s ;;\n  *) cat %s ;;\nesac\n'
        % (json.dumps(str(so)), json.dumps(str(ro)))
    )
    scr.chmod(0o755)
    return str(scr)


def _pkg(dirpath, name, magic, body=b"BODY"):
    p = Path(dirpath) / name
    p.write_bytes(magic + body)
    return str(p)


def _rpm_dir(tmp_path):
    d = tmp_path / "pkgout"; d.mkdir()
    return d


# --- happy paths -------------------------------------------------------------
def test_build_receipt_rpm_single_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    d = _rpm_dir(tmp_path)
    _pkg(d, "pgedge-rag-server2-2.0.0-1.el9.x86_64.rpm", I._RPM_MAGIC, b"AAA")
    rec = R.build_receipt("rag-rpm-amd64", "rpm", str(d), "12345",
                          "pkg-rag-rpm-amd64-999", "sha256:" + _D64)
    assert rec["schema"] == "pep-receipt/2"
    assert rec["cell_id"] == "rag-rpm-amd64"
    assert rec["artifact_id"] == 12345           # int, positive
    assert rec["artifact_name"] == "pkg-rag-rpm-amd64-999"
    assert rec["archive_digest"] == _D64          # 'sha256:' prefix normalized away
    assert list(rec.keys()) == ["schema", "cell_id", "artifact_id",
                                "artifact_name", "archive_digest", "members"]
    assert len(rec["members"]) == 1
    m = rec["members"][0]
    assert list(m.keys()) == list(I._MEMBER_ORDER)   # complete pep-members/1 record
    assert m["package_class"] == "runtime" and m["native_arch"] == "x86_64"
    assert m["artifact_member_path"] == "pgedge-rag-server2-2.0.0-1.el9.x86_64.rpm"
    assert m["epoch"] is None                        # epoch retained


def test_build_receipt_rpm_runtime_plus_source(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RPM_BIN", _fake_rpm_router(
        tmp_path, _CASES["rpm_runtime"]["tool_output"], _CASES["rpm_source_x86"]["tool_output"]))
    d = _rpm_dir(tmp_path)
    _pkg(d, "pgedge-rag-server2-2.0.0-1.el9.x86_64.rpm", I._RPM_MAGIC, b"BIN")
    _pkg(d, "pgedge-rag-server2-2.0.0-1.el9.src.rpm", I._RPM_MAGIC, b"SRC")
    rec = R.build_receipt("rag-rpm-amd64", "rpm", str(d), 7, "pkg-x", _D64)
    classes = {m["artifact_member_path"]: m["package_class"] for m in rec["members"]}
    assert classes == {
        "pgedge-rag-server2-2.0.0-1.el9.src.rpm": "source",       # runtime + source both kept
        "pgedge-rag-server2-2.0.0-1.el9.x86_64.rpm": "runtime",
    }
    # deterministic order: sorted by artifact_member_path (src < x86_64)
    assert [m["artifact_member_path"] for m in rec["members"]] == [
        "pgedge-rag-server2-2.0.0-1.el9.src.rpm",
        "pgedge-rag-server2-2.0.0-1.el9.x86_64.rpm",
    ]


def test_build_receipt_deb_single(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "DPKG_DEB_BIN", _fake_tool(tmp_path, "dpkg-deb", _CASES["deb_runtime"]["tool_output"]))
    d = _rpm_dir(tmp_path)
    _pkg(d, "pgedge-rag-server2_2.0.0-1.noble_arm64.deb", I._DEB_MAGIC, b"DEB")
    rec = R.build_receipt("rag-deb-arm64", "deb", str(d), "88", "pkg-deb", _D64)
    assert rec["schema"] == "pep-receipt/2"
    assert len(rec["members"]) == 1
    m = rec["members"][0]
    assert m["package_class"] == "runtime" and m["native_arch"] == "arm64"
    assert m["version"] == "2.0.0" and m["release"] == "1.noble"


def test_build_receipt_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    d = _rpm_dir(tmp_path)
    _pkg(d, "pgedge-rag-server2-2.0.0-1.el9.x86_64.rpm", I._RPM_MAGIC, b"AAA")
    a = R.receipt_json(R.build_receipt("c1", "rpm", str(d), "1", "n", _D64))
    b = R.receipt_json(R.build_receipt("c1", "rpm", str(d), "1", "n", _D64))
    assert a == b and a.endswith("\n")


# --- input validation (unit) -------------------------------------------------
@pytest.mark.parametrize("bad", ["", "   ", " x", "x ", "a/b", "a b", "..", ".", "a\\b", "a:b", "a*b", None, 5])
def test_validate_cell_id_rejected(bad):
    with pytest.raises(R.ReceiptError):
        R.validate_cell_id(bad)


def test_validate_cell_id_ok():
    for good in ("rag-rpm-amd64", "a.b_c-9", "X", "x" * 128):
        assert R.validate_cell_id(good) == good


def test_long_detector_shaped_cell_id_preserved_verbatim(tmp_path, monkeypatch):
    # The detector owns cell_id and may emit ids > 128 chars (long component names);
    # there is no arbitrary local ceiling. The value is opaque and preserved exactly.
    long_cid = "pgedge-" + "reallylongcomponentname" * 6 + "-rpm-amd64-pg17-daily-rocky9"
    assert len(long_cid) > 128
    assert R.validate_cell_id(long_cid) == long_cid                     # no cap, verbatim
    assert R.receipt_artifact_name(long_cid) == "pep-receipt-" + long_cid
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    d = _rpm_dir(tmp_path)
    _pkg(d, "a.rpm", I._RPM_MAGIC, b"AAA")
    rec = R.build_receipt(long_cid, "rpm", str(d), "1", "n", _D64)
    assert rec["cell_id"] == long_cid                                   # verbatim in the receipt


@pytest.mark.parametrize("bad", ["RPM", "srpm", "", "tar", "rpm ", None, 1])
def test_validate_family_rejected(bad):
    with pytest.raises(R.ReceiptError):
        R.validate_family(bad)


@pytest.mark.parametrize("bad", ["0", "-5", "1.5", "abc", "", "12a", " 12", "12\n", True, 0, -1, 2.0])
def test_validate_artifact_id_rejected(bad):
    with pytest.raises(R.ReceiptError):
        R.validate_artifact_id(bad)


@pytest.mark.parametrize("good,expected", [("12", 12), (34, 34), ("1", 1)])
def test_validate_artifact_id_ok(good, expected):
    assert R.validate_artifact_id(good) == expected


@pytest.mark.parametrize("bad", ["", "   ", " x", "x ", None, 5])
def test_validate_artifact_name_rejected(bad):
    with pytest.raises(R.ReceiptError):
        R.validate_artifact_name(bad)


# --- archive digest normalization (canonical = bare lowercase 64-hex) --------
@pytest.mark.parametrize("raw,expected", [
    (_D64, _D64),
    ("sha256:" + _D64, _D64),
    ("SHA256:" + ("AB" * 32), "ab" * 32),     # prefix + uppercase normalized
    ("  " + _D64 + "  ", _D64),               # surrounding whitespace trimmed
    ("sha256:" + ("CD" * 32), "cd" * 32),
])
def test_normalize_archive_digest_ok(raw, expected):
    assert R.normalize_archive_digest(raw) == expected


@pytest.mark.parametrize("bad", [
    "", "   ", "xyz", "ab" * 31, "ab" * 33, "gg" * 32,
    "sha256:", "sha1:" + ("ab" * 20), "sha256:zz" + "ab" * 31, None, 123,
])
def test_normalize_archive_digest_rejected(bad):
    with pytest.raises(R.ReceiptError):
        R.normalize_archive_digest(bad)


def test_archive_digest_is_not_member_sha(tmp_path, monkeypatch):
    # The receipt's archive_digest is the ARCHIVE digest input, NOT any member sha256.
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    d = _rpm_dir(tmp_path)
    _pkg(d, "pgedge-rag-server2-2.0.0-1.el9.x86_64.rpm", I._RPM_MAGIC, b"AAA")
    rec = R.build_receipt("c", "rpm", str(d), "1", "n", "cd" * 32)
    assert rec["archive_digest"] == "cd" * 32
    assert rec["members"][0]["sha256"] != rec["archive_digest"]   # distinct concepts


# --- flat-directory fail-closed cases ---------------------------------------
def test_missing_and_empty_dir(tmp_path):
    with pytest.raises(R.ReceiptError):
        R.enumerate_package_dir(str(tmp_path / "nope"))
    empty = tmp_path / "empty"; empty.mkdir()
    with pytest.raises(R.ReceiptError):
        R.enumerate_package_dir(str(empty))
    with pytest.raises(R.ReceiptError):
        R.enumerate_package_dir("")


def test_nested_dir_rejected(tmp_path):
    d = _rpm_dir(tmp_path)
    (d / "nested").mkdir()
    with pytest.raises(R.ReceiptError):
        R.enumerate_package_dir(str(d))


def test_symlink_rejected(tmp_path):
    d = _rpm_dir(tmp_path)
    real = tmp_path / "real.rpm"; real.write_bytes(I._RPM_MAGIC + b"x")
    (d / "link.rpm").symlink_to(real)
    with pytest.raises(R.ReceiptError):
        R.enumerate_package_dir(str(d))


def test_symlinked_package_dir_itself_rejected(tmp_path, monkeypatch):
    # A symlink whose TARGET is a populated real directory must be rejected
    # (os.path.isdir would follow it) BEFORE any inspection runs.
    real = tmp_path / "real"; real.mkdir()
    _pkg(real, "a.rpm", I._RPM_MAGIC, b"AAA")           # populated real dir
    link = tmp_path / "linkdir"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(R.ReceiptError):
        R.enumerate_package_dir(str(link))
    # build_receipt must fail on the symlinked dir before calling the inspector
    calls = []
    orig = I.inspect_members
    monkeypatch.setattr(I, "inspect_members", lambda *a, **k: (calls.append(1), orig(*a, **k))[1])
    with pytest.raises(R.ReceiptError):
        R.build_receipt("c", "rpm", str(link), "1", "n", _D64)
    assert calls == []                                   # rejected before inspection


def test_case_insensitive_duplicate_rejected(tmp_path, monkeypatch):
    class _FE:
        def __init__(self, name): self.name = name
        def is_symlink(self): return False
        def is_dir(self, follow_symlinks=True): return False
        def is_file(self, follow_symlinks=True): return True

    class _Scan:
        def __init__(self, e): self._e = e
        def __enter__(self): return iter(self._e)
        def __exit__(self, *a): return False

    monkeypatch.setattr(R.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(R.os, "scandir", lambda p: _Scan([_FE("Pkg.rpm"), _FE("pkg.rpm")]))
    with pytest.raises(R.ReceiptError):
        R.enumerate_package_dir("whatever")


# --- inspection fail-closed cases -------------------------------------------
def test_sidecar_or_unrecognized_file_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    d = _rpm_dir(tmp_path)
    _pkg(d, "good.rpm", I._RPM_MAGIC, b"AAA")
    (d / "notes.txt").write_bytes(b"not a package")      # sidecar, no package magic
    with pytest.raises(R.ReceiptError):
        R.build_receipt("c", "rpm", str(d), "1", "n", _D64)


def test_wrong_and_mixed_family_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    monkeypatch.setattr(I, "DPKG_DEB_BIN", _fake_tool(tmp_path, "dpkg-deb", _CASES["deb_runtime"]["tool_output"]))
    # wrong family: a deb-magic file but family=rpm
    d1 = tmp_path / "d1"; d1.mkdir()
    _pkg(d1, "x.deb", I._DEB_MAGIC, b"DEB")
    with pytest.raises(R.ReceiptError):
        R.build_receipt("c", "rpm", str(d1), "1", "n", _D64)
    # mixed family: one rpm + one deb, family=rpm
    d2 = tmp_path / "d2"; d2.mkdir()
    _pkg(d2, "a.rpm", I._RPM_MAGIC, b"AAA")
    _pkg(d2, "b.deb", I._DEB_MAGIC, b"BBB")
    with pytest.raises(R.ReceiptError):
        R.build_receipt("c", "rpm", str(d2), "1", "n", _D64)


def test_inspector_failure_is_wrapped(tmp_path, monkeypatch):
    scr = tmp_path / "rpm"
    scr.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")   # tool nonzero -> InspectError
    scr.chmod(0o755)
    monkeypatch.setattr(I, "RPM_BIN", str(scr))
    d = _rpm_dir(tmp_path)
    _pkg(d, "a.rpm", I._RPM_MAGIC, b"AAA")
    with pytest.raises(R.ReceiptError):
        R.build_receipt("c", "rpm", str(d), "1", "n", _D64)


def test_zero_members_rejected(tmp_path, monkeypatch):
    d = _rpm_dir(tmp_path)
    _pkg(d, "a.rpm", I._RPM_MAGIC, b"AAA")
    monkeypatch.setattr(I, "inspect_members", lambda entries, expected_family=None: [])
    with pytest.raises(R.ReceiptError):
        R.build_receipt("c", "rpm", str(d), "1", "n", _D64)


# --- full build_receipt input rejection (integration of the validators) ------
def test_build_receipt_rejects_bad_scalars(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    d = _rpm_dir(tmp_path)
    _pkg(d, "a.rpm", I._RPM_MAGIC, b"AAA")
    base = dict(cell_id="c", family="rpm", package_dir=str(d),
                artifact_id="1", artifact_name="n", artifact_digest=_D64)
    for field, bad in [("cell_id", "a/b"), ("family", "srpm"), ("artifact_id", "0"),
                       ("artifact_name", "  "), ("artifact_digest", "nothex")]:
        kw = dict(base); kw[field] = bad
        with pytest.raises(R.ReceiptError):
            R.build_receipt(**kw)


# --- CLI main(): writes exactly one root file + emits outputs, exit codes ----
def test_main_writes_receipt_and_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    d = _rpm_dir(tmp_path)
    _pkg(d, "pgedge-rag-server2-2.0.0-1.el9.x86_64.rpm", I._RPM_MAGIC, b"AAA")
    out_dir = tmp_path / "out"
    gh = tmp_path / "ghout"; gh.write_text("")
    rc = R.main(["--cell-id", "rag-rpm-amd64", "--family", "rpm", "--package-dir", str(d),
                 "--artifact-id", "12345", "--artifact-name", "pkg-x",
                 "--artifact-digest", "sha256:" + _D64,
                 "--out-dir", str(out_dir), "--github-output", str(gh)])
    assert rc == 0
    # exactly one root file, receipt.json
    assert [p.name for p in out_dir.iterdir()] == ["receipt.json"]
    rec = json.loads((out_dir / "receipt.json").read_text())
    assert rec["schema"] == "pep-receipt/2" and rec["archive_digest"] == _D64
    outputs = dict(line.split("=", 1) for line in gh.read_text().splitlines() if "=" in line)
    assert outputs["receipt_artifact_name"] == "pep-receipt-rag-rpm-amd64"
    assert outputs["receipt_path"].endswith("out/receipt.json")
    assert outputs["member_count"] == "1"


def test_main_failure_exit_3_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    d = _rpm_dir(tmp_path)
    _pkg(d, "a.rpm", I._RPM_MAGIC, b"AAA")
    out_dir = tmp_path / "out"
    rc = R.main(["--cell-id", "bad/id", "--family", "rpm", "--package-dir", str(d),
                 "--artifact-id", "1", "--artifact-name", "n",
                 "--artifact-digest", _D64, "--out-dir", str(out_dir)])
    assert rc == 3
    assert not out_dir.exists()          # nothing written on rejection


def test_receipt_artifact_name_helper():
    assert R.receipt_artifact_name("rag-rpm-amd64") == "pep-receipt-rag-rpm-amd64"
