"""Repeatable, stdlib-only tests for utillities/pep_pkg_inspect.py.

Edge cases use mocked tool output (golden.json) and the pure parser/classifier
functions; the end-to-end subprocess path uses fake `rpm`/`dpkg-deb` executables
and tiny fake package files (correct magic bytes). No binary packages, no network.
"""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import pep_pkg_inspect as I

_FIX = Path(__file__).parent / "pkg_inspect_fixtures" / "golden.json"
_GOLDEN = json.loads(_FIX.read_text())
_CASES = _GOLDEN["cases"]
_SENT = _GOLDEN["sha256_sentinel"]


# --- golden parse + classify (pure, mocked tool output) ----------------------
def test_schema_constant():
    assert I.SCHEMA == "pep-members/1"


def test_golden_cases_parse_classify_and_field_order():
    for name, c in _CASES.items():
        if c["kind"] == "rpm":
            fields = I.parse_rpm_output(c["tool_output"])
            m = I.member_from_rpm(fields, _SENT, c["artifact_member_path"])
        else:
            fields = I.parse_deb_output(c["tool_output"])
            m = I.member_from_deb(fields, _SENT, c["artifact_member_path"])
        assert m == c["expected"], name
        assert list(m.keys()) == list(I._MEMBER_ORDER), name   # deterministic field order


# --- artifact_member_path validation (v1: safe flat filename) ----------------
def test_member_path_valid_is_case_preserved():
    assert I.validate_member_path("Foo-2.0.0-1.el9.X86_64.RPM") == "Foo-2.0.0-1.el9.X86_64.RPM"
    assert I.validate_member_path("pkg_1.2.3_all.deb") == "pkg_1.2.3_all.deb"


@pytest.mark.parametrize("bad", [
    "", "   ", " x.rpm", "x.rpm ", "/abs.rpm", "a/b.rpm", "a\\b.deb",
    "..", ".", "dir/", "sub/dir/x.rpm", "with\x00nul.rpm",
])
def test_member_path_rejected(bad):
    with pytest.raises(I.InspectError):
        I.validate_member_path(bad)


@pytest.mark.parametrize("bad", [None, 123, [], {}])
def test_member_path_non_string_rejected(bad):
    with pytest.raises(I.InspectError):
        I.validate_member_path(bad)


# --- RPM epoch --------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [("0", None), ("(none)", None), ("", None), ("2", 2), (" 3 ", 3)])
def test_parse_epochnum_ok(raw, expected):
    assert I._parse_epochnum(raw) == expected


def test_parse_epochnum_malformed():
    with pytest.raises(I.InspectError):
        I._parse_epochnum("x")


def test_parse_epochnum_non_string():
    for bad in (None, 2, 2.0, []):
        with pytest.raises(I.InspectError):
            I._parse_epochnum(bad)


# --- RPM classification: SOURCEPACKAGE authoritative + SOURCERPM consistency --
def test_classify_rpm_consistent_forms():
    # SOURCEPACKAGE == "1" (source) requires SOURCERPM == "(none)".
    assert I._classify_rpm("pkg", "1", "(none)") == "source"
    assert I._classify_rpm("pkg-debuginfo", "1", "(none)") == "source"     # source precedes debug name
    # SOURCEPACKAGE == "(none)" (binary) requires a nonblank, non-"(none)" SOURCERPM.
    assert I._classify_rpm("pkg", "(none)", "pkg-1.0-1.src.rpm") == "runtime"
    assert I._classify_rpm("pkg-debuginfo", "(none)", "pkg-1.0-1.src.rpm") == "debug"
    assert I._classify_rpm("pkg-debugsource", "(none)", "pkg-1.0-1.src.rpm") == "debug"


def test_classify_rpm_devel_libs_noarch_are_runtime():
    # -devel/-libs are NOT debug; only -debuginfo/-debugsource are.
    assert I._classify_rpm("pkg-devel", "(none)", "pkg-1.0-1.src.rpm") == "runtime"
    assert I._classify_rpm("pkg-libs", "(none)", "pkg-1.0-1.src.rpm") == "runtime"
    # noarch is preserved as native_arch and classified runtime.
    m = I.member_from_rpm(
        {"name": "pkg", "epochnum": "0", "version": "1.0", "release": "1.el9",
         "arch": "noarch", "sourcepackage": "(none)", "sourcerpm": "pkg-1.0-1.el9.src.rpm"},
        _SENT, "pkg-1.0-1.el9.noarch.rpm")
    assert m["native_arch"] == "noarch"
    assert m["package_class"] == "runtime"


@pytest.mark.parametrize("sp,srpm", [
    ("1", "pkg-1.0-1.src.rpm"),   # source but SOURCERPM not "(none)"
    ("1", ""),                    # source but SOURCERPM blank
    ("1", "(None)"),              # case-different placeholder is not "(none)"
    ("(none)", "(none)"),         # binary but SOURCERPM is "(none)"
    ("(none)", ""),               # binary but SOURCERPM blank
    ("(none)", "   "),            # binary but SOURCERPM blank/padded
    ("(none)", " pkg.src.rpm"),   # binary SOURCERPM padded
    ("0", "pkg.src.rpm"),         # "0" is NOT a valid SOURCEPACKAGE marker
    ("", "pkg.src.rpm"),          # blank SOURCEPACKAGE
    (" 1", "(none)"),             # padded SOURCEPACKAGE
    ("1 ", "(none)"),             # padded SOURCEPACKAGE
    ("yes", "pkg.src.rpm"),       # arbitrary text
    ("2", "pkg.src.rpm"),         # any other number
])
def test_classify_rpm_inconsistent_or_invalid_raises(sp, srpm):
    with pytest.raises(I.InspectError):
        I._classify_rpm("pkg", sp, srpm)


def test_classify_rpm_non_string_raises():
    for args in (("pkg", 1, "(none)"), ("pkg", "1", None), (None, "1", "(none)")):
        with pytest.raises(I.InspectError):
            I._classify_rpm(*args)


def test_source_arch_preserved_and_not_used_for_class():
    # source RPMs report x86_64 AND aarch64 in ARCH; class must come from SOURCEPACKAGE=1
    for case in ("rpm_source_x86", "rpm_source_arm"):
        c = _CASES[case]
        m = I.member_from_rpm(I.parse_rpm_output(c["tool_output"]), _SENT, c["artifact_member_path"])
        assert m["package_class"] == "source"
        assert m["native_arch"] == c["expected"]["native_arch"]   # preserved verbatim


# --- DEB version split -------------------------------------------------------
@pytest.mark.parametrize("ver,expected", [
    ("2.0.0-1.noble", (None, "2.0.0", "1.noble")),
    ("2:1.4-5.bookworm", (2, "1.4", "5.bookworm")),
    ("1.2.3", (None, "1.2.3", "")),                     # native package, no revision
    ("2.0.0-beta-1.trixie", (None, "2.0.0-beta", "1.trixie")),  # hyphenated upstream
])
def test_split_deb_version_ok(ver, expected):
    assert I.split_deb_version(ver) == expected


@pytest.mark.parametrize("bad", [
    "", "   ", "x:1.0-1", "2:-1",
    "2:3:1.0-1",   # multiple epoch separators
    ":1.0",        # empty epoch head
    "1.0-",        # hyphen with empty revision
    "2:1.0-",      # epoch + hyphen with empty revision
])
def test_split_deb_version_malformed(bad):
    with pytest.raises(I.InspectError):
        I.split_deb_version(bad)


def test_split_deb_version_non_string():
    for bad in (None, 2, [], {}):
        with pytest.raises(I.InspectError):
            I.split_deb_version(bad)


# --- parser structural errors -----------------------------------------------
def test_parse_rpm_output_errors():
    with pytest.raises(I.InspectError):
        I.parse_rpm_output("a\tb\tc")                    # wrong field count
    with pytest.raises(I.InspectError):
        I.parse_rpm_output("line1\nline2")               # more than one line
    with pytest.raises(I.InspectError):
        I.parse_rpm_output(None)                         # non-string
    assert I.parse_rpm_output(_CASES["rpm_runtime"]["tool_output"])["name"] == "pgedge-rag-server2"


def test_parse_deb_output_errors():
    with pytest.raises(I.InspectError):
        I.parse_deb_output("no colon here")
    with pytest.raises(I.InspectError):
        I.parse_deb_output(None)                         # non-string
    assert I.parse_deb_output(_CASES["deb_runtime"]["tool_output"])["package"] == "pgedge-rag-server2"


def test_parse_deb_output_duplicate_unexpected_missing():
    with pytest.raises(I.InspectError):                  # duplicate field
        I.parse_deb_output("Package: a\nPackage: b\nVersion: 1\nArchitecture: all\n")
    with pytest.raises(I.InspectError):                  # unexpected field
        I.parse_deb_output("Package: a\nVersion: 1\nArchitecture: all\nMaintainer: x\n")
    with pytest.raises(I.InspectError):                  # missing Architecture
        I.parse_deb_output("Package: a\nVersion: 1\n")


def test_member_from_rpm_missing_or_padded_fields_fail_closed():
    base = I.parse_rpm_output(_CASES["rpm_runtime"]["tool_output"])
    for k in ("name", "version", "release", "arch"):
        bad = dict(base)
        bad[k] = "   "                                   # blank/padded identity field
        with pytest.raises(I.InspectError):
            I.member_from_rpm(bad, _SENT, "x.rpm")
    for k in I._RPM_FIELDS:                               # missing key
        bad = dict(base)
        del bad[k]
        with pytest.raises(I.InspectError):
            I.member_from_rpm(bad, _SENT, "x.rpm")
    with pytest.raises(I.InspectError):                   # non-dict
        I.member_from_rpm("not-a-dict", _SENT, "x.rpm")
    bad = dict(base)
    bad["name"] = 123                                     # non-string field
    with pytest.raises(I.InspectError):
        I.member_from_rpm(bad, _SENT, "x.rpm")


def test_member_from_deb_shape_fail_closed():
    with pytest.raises(I.InspectError):
        I.member_from_deb("not-a-dict", _SENT, "x.deb")
    with pytest.raises(I.InspectError):                   # missing architecture
        I.member_from_deb({"package": "a", "version": "1.0-1"}, _SENT, "x.deb")
    with pytest.raises(I.InspectError):                   # non-string field
        I.member_from_deb({"package": "a", "version": "1.0-1", "architecture": 64}, _SENT, "x.deb")


# --- central member validation ----------------------------------------------
def test_member_central_validation_accepts_good():
    m = I._member("pkg", None, "1.0", "1.el9", "x86_64", "runtime", _SENT, "pkg.rpm")
    assert list(m.keys()) == list(I._MEMBER_ORDER)
    assert I._member("pkg", 0, "1.0", "", "all", "source", _SENT, "pkg.deb")["epoch"] == 0


@pytest.mark.parametrize("sha", [
    "a" * 63, "a" * 65, "A" * 64, "g" * 64, "", 123, None, "a" * 64 + " ",
])
def test_member_bad_sha_rejected(sha):
    with pytest.raises(I.InspectError):
        I._member("pkg", None, "1.0", "1.el9", "x86_64", "runtime", sha, "pkg.rpm")


@pytest.mark.parametrize("cls", ["", "Runtime", "src", "binary", "unknown", None, 1])
def test_member_bad_class_rejected(cls):
    with pytest.raises(I.InspectError):
        I._member("pkg", None, "1.0", "1.el9", "x86_64", cls, _SENT, "pkg.rpm")


@pytest.mark.parametrize("epoch", [-1, True, False, "2", 1.5, []])
def test_member_bad_epoch_rejected(epoch):
    with pytest.raises(I.InspectError):
        I._member("pkg", epoch, "1.0", "1.el9", "x86_64", "runtime", _SENT, "pkg.rpm")


@pytest.mark.parametrize("field,value", [
    ("package_name", ""), ("package_name", " pkg"), ("package_name", 5),
    ("version", ""), ("version", "1.0 "), ("version", None),
    ("native_arch", ""), ("native_arch", " x86_64"), ("native_arch", []),
    ("release", 5), ("release", None),                   # release must be a string (may be "")
])
def test_member_bad_identity_fields_rejected(field, value):
    kw = dict(package_name="pkg", epoch=None, version="1.0", release="1.el9",
              native_arch="x86_64", package_class="runtime", sha256=_SENT, member_path="pkg.rpm")
    kw[field if field != "member_path" else "member_path"] = value
    with pytest.raises(I.InspectError):
        I._member(kw["package_name"], kw["epoch"], kw["version"], kw["release"],
                  kw["native_arch"], kw["package_class"], kw["sha256"], kw["member_path"])


# --- format detection (magic bytes, not filename) ----------------------------
def test_detect_format(tmp_path):
    r = tmp_path / "r.bin"
    r.write_bytes(I._RPM_MAGIC + b"junk")
    assert I._detect_format(str(r)) == I.RPM
    d = tmp_path / "d.bin"
    d.write_bytes(I._DEB_MAGIC + b"junk")
    assert I._detect_format(str(d)) == I.DEB
    b = tmp_path / "b.bin"
    b.write_bytes(b"NOTAPKG!")
    with pytest.raises(I.InspectError):
        I._detect_format(str(b))
    with pytest.raises(I.InspectError):
        I._detect_format(str(tmp_path / "missing"))


def test_public_detect_format(tmp_path):
    r = tmp_path / "r.bin"
    r.write_bytes(I._RPM_MAGIC + b"x")
    d = tmp_path / "d.bin"
    d.write_bytes(I._DEB_MAGIC + b"x")
    assert I.detect_format(str(r)) == I.RPM
    assert I.detect_format(str(d)) == I.DEB
    for bad in ("", None, 123):
        with pytest.raises(I.InspectError):
            I.detect_format(bad)
    b = tmp_path / "b.bin"
    b.write_bytes(b"NOPE!!!!")
    with pytest.raises(I.InspectError):
        I.detect_format(str(b))


# --- expected_family enforcement (single-source, magic-byte based) -----------
def test_inspect_package_expected_family_ok_and_mismatch(tmp_path, monkeypatch):
    c = _CASES["rpm_source_x86"]
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", c["tool_output"]))
    rpm_path, _ = _fake_pkg(tmp_path, "pkg.rpm", I._RPM_MAGIC, b"RPMBODY")
    # matching family: inspection proceeds
    m = I.inspect_package(rpm_path, c["artifact_member_path"], expected_family=I.RPM)
    assert m["package_class"] == "source"
    # wrong family requested for an rpm-magic file: fail closed BEFORE running tools
    with pytest.raises(I.InspectError):
        I.inspect_package(rpm_path, c["artifact_member_path"], expected_family=I.DEB)
    # a deb-magic file rejected when rpm is requested (sidecar/mixed-family case)
    deb_path, _ = _fake_pkg(tmp_path, "pkg.deb", I._DEB_MAGIC, b"DEBBODY")
    with pytest.raises(I.InspectError):
        I.inspect_package(deb_path, "pkg.deb", expected_family=I.RPM)


@pytest.mark.parametrize("bad", ["", "RPM", "src", "rpm ", 1, ["rpm"]])
def test_expected_family_invalid_value_rejected(bad, tmp_path):
    p, _ = _fake_pkg(tmp_path, "pkg.rpm", I._RPM_MAGIC, b"x")
    with pytest.raises(I.InspectError):
        I.inspect_package(p, "pkg.rpm", expected_family=bad)
    with pytest.raises(I.InspectError):
        I.inspect_members([(p, "pkg.rpm")], expected_family=bad)


def test_inspect_members_expected_family_rejects_mixed(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    rpm_path, _ = _fake_pkg(tmp_path, "a.rpm", I._RPM_MAGIC, b"AAA")
    deb_path, _ = _fake_pkg(tmp_path, "b.deb", I._DEB_MAGIC, b"BBB")
    # a mixed batch fails as a whole when a single family is enforced
    with pytest.raises(I.InspectError):
        I.inspect_members([(rpm_path, "a.rpm"), (deb_path, "b.deb")], expected_family=I.RPM)
    # all-rpm batch with rpm enforced succeeds
    rpm2, _ = _fake_pkg(tmp_path, "c.rpm", I._RPM_MAGIC, b"CCC")
    members = I.inspect_members([(rpm_path, "a.rpm"), (rpm2, "c.rpm")], expected_family=I.RPM)
    assert [m["artifact_member_path"] for m in members] == ["a.rpm", "c.rpm"]


# --- _run: explicit argv + shell=False, bounded, strict UTF-8 ----------------
def test_run_uses_list_argv_and_shell_false(monkeypatch):
    seen = {}

    def spy(argv, **k):
        seen["argv"] = argv
        seen["shell"] = k.get("shell")

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(I.subprocess, "run", spy)
    assert I._run(["rpm", "-qp", "pkg"]) == ""            # empty temp file -> ""
    assert isinstance(seen["argv"], list) and seen["argv"][0] == "rpm"
    assert seen["shell"] is False


def test_run_timeout(monkeypatch):
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="rpm", timeout=1)
    monkeypatch.setattr(I.subprocess, "run", _timeout)
    with pytest.raises(I.InspectError):
        I._run(["rpm", "-qp"])


def test_run_oversized_output_is_rejected(tmp_path):
    scr = tmp_path / "big"
    scr.write_text("#!/bin/sh\nhead -c %d /dev/zero | tr '\\0' x\n" % (I._MAX_TOOL_OUTPUT + 64))
    scr.chmod(0o755)
    with pytest.raises(I.InspectError):
        I._run([str(scr)])


def test_run_non_utf8_stdout_is_rejected(tmp_path):
    scr = tmp_path / "bad"
    scr.write_text("#!/bin/sh\nprintf '\\377\\376'\n")     # invalid UTF-8 bytes
    scr.chmod(0o755)
    with pytest.raises(I.InspectError):
        I._run([str(scr)])


def test_run_nonzero_and_missing(tmp_path, monkeypatch):
    scr = tmp_path / "rpm"
    scr.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    scr.chmod(0o755)
    with pytest.raises(I.InspectError):
        I._run([str(scr)])
    with pytest.raises(I.InspectError):
        I._run([str(tmp_path / "does-not-exist-tool")])


# --- end-to-end via fake executables ----------------------------------------
def _fake_tool(tmp_path, name, output_text):
    out = tmp_path / (name + ".out")
    out.write_bytes(output_text.encode("utf-8"))
    scr = tmp_path / name
    scr.write_text('#!/bin/sh\ncat %s\n' % json.dumps(str(out)))
    scr.chmod(0o755)
    return str(scr)


def _fake_pkg(tmp_path, name, magic, body):
    p = tmp_path / name
    p.write_bytes(magic + body)
    return str(p), hashlib.sha256(magic + body).hexdigest()


def test_inspect_package_rpm_end_to_end(tmp_path, monkeypatch):
    c = _CASES["rpm_source_x86"]
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", c["tool_output"]))
    path, sha = _fake_pkg(tmp_path, "pkg.rpm", I._RPM_MAGIC, b"RPMBODY-1")
    m = I.inspect_package(path, c["artifact_member_path"])
    exp = dict(c["expected"])
    exp["sha256"] = sha
    assert m == exp                                       # class=source, arch preserved, real sha


def test_inspect_package_deb_end_to_end(tmp_path, monkeypatch):
    c = _CASES["deb_runtime"]
    monkeypatch.setattr(I, "DPKG_DEB_BIN", _fake_tool(tmp_path, "dpkg-deb", c["tool_output"]))
    path, sha = _fake_pkg(tmp_path, "pkg.deb", I._DEB_MAGIC, b"DEBBODY-2")
    m = I.inspect_package(path, c["artifact_member_path"])
    exp = dict(c["expected"])
    exp["sha256"] = sha
    assert m == exp


def test_inspect_package_bad_path_type(tmp_path):
    for bad in (None, 123, [], {}, ""):
        with pytest.raises(I.InspectError):
            I.inspect_package(bad, "x.rpm")


# --- batch: all-or-nothing + dedup + deterministic sort ---------------------
def test_inspect_members_all_or_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    p1, _ = _fake_pkg(tmp_path, "a.rpm", I._RPM_MAGIC, b"AAA")
    p2, _ = _fake_pkg(tmp_path, "b.rpm", I._RPM_MAGIC, b"BBB")
    members = I.inspect_members([(p1, "a.rpm"), (p2, "b.rpm")])
    assert [m["artifact_member_path"] for m in members] == ["a.rpm", "b.rpm"]
    assert members[0]["sha256"] != members[1]["sha256"]   # distinct bytes -> distinct sha

    with pytest.raises(I.InspectError):                    # duplicate member path
        I.inspect_members([(p1, "same.rpm"), (p2, "same.rpm")])

    bad = tmp_path / "c.rpm"
    bad.write_bytes(b"NOTAPKG")
    with pytest.raises(I.InspectError):                    # one fails -> no partial
        I.inspect_members([(p1, "a.rpm"), (str(bad), "c.rpm")])


@pytest.mark.parametrize("bad", [
    "not-a-list", ("a", "b"),                              # not a list
    [("only-one-element",)], [("a", "b", "c")],            # bad pair arity
    [(123, "x.rpm")], [(None, "x.rpm")], [([], "x.rpm")], [({}, "x.rpm")],  # bad path type
    [("p", 123)], [("p", "")],                             # bad member path
])
def test_inspect_members_malformed_input_rejected(bad):
    with pytest.raises(I.InspectError):
        I.inspect_members(bad)


def test_inspect_members_sorted_independent_of_order(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RPM_BIN", _fake_tool(tmp_path, "rpm", _CASES["rpm_runtime"]["tool_output"]))
    pa, _ = _fake_pkg(tmp_path, "aa.rpm", I._RPM_MAGIC, b"AAA")
    pb, _ = _fake_pkg(tmp_path, "bb.rpm", I._RPM_MAGIC, b"BBB")
    forward = I.inspect_members([(pa, "a.rpm"), (pb, "b.rpm")])
    reverse = I.inspect_members([(pb, "b.rpm"), (pa, "a.rpm")])   # same path->bytes mapping, reversed
    assert [m["artifact_member_path"] for m in forward] == ["a.rpm", "b.rpm"]
    assert forward == reverse                              # deterministic, caller-order independent


def test_inspect_members_empty_is_empty():
    assert I.inspect_members([]) == []
