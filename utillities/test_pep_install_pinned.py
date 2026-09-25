"""Docker-free structural tests for aspects.package_management.install_pinned, the
verified pinned install: download the exact pin into the package manager's cache,
identify and hash the ONE cached file for it, install that pin from the cache only,
and tie the install to the hashed file. A caller-supplied value is always a single
argv element and never crosses `sh -c`; every refusal happens before the install
unless it is a post-install tie failure."""
import importlib.util
import sys
from pathlib import Path

import pytest

_p = Path(__file__).resolve().parent.parent / "aspects" / "package_management.py"
_spec = importlib.util.spec_from_file_location("pm_inst", str(_p))
pm = importlib.util.module_from_spec(_spec)
sys.modules["pm_inst"] = pm
_spec.loader.exec_module(pm)

PKG = "pgedge-rag-server2"
SHA = "a" * 64
OTHER_SHA = "b" * 64
HDR = "c" * 64
PIN = {"deb": "2.0.0-1.bookworm", "rpm": "2.0.0-1.el9"}
NATIVE = {"deb": "arm64", "rpm": "aarch64"}
INDEP = {"deb": "all", "rpm": "noarch"}
CACHE_DIR = {"deb": "/var/cache/apt/archives/", "rpm": "/var/cache/dnf/pgedge-1/packages/"}
# What each package manager prints (exit 1) for a package that is not installed.
NOT_INSTALLED = {"deb": "dpkg-query: no packages found matching %s" % PKG,
                 "rpm": "package %s is not installed" % PKG}


def cached(family, *, name=PKG, version=None, arch=None, sha=SHA, header=HDR, fname=None):
    """One file in the package cache: its own metadata, digest and (RPM) header digest."""
    version = PIN[family] if version is None else version
    arch = NATIVE[family] if arch is None else arch
    fname = fname or ("%s_%s_%s.deb" % (name, version, arch) if family == "deb"
                      else "%s-%s.%s.rpm" % (name, version, arch))
    return {"path": CACHE_DIR[family] + fname, "name": name, "version": version, "arch": arch,
            "sha": sha, "header": header, "readable": True}


DEP = {"deb": cached("deb", name="logrotate", version="3.21.0-1", sha="1" * 64),
       "rpm": cached("rpm", name="logrotate", version="3.18.0-12.el9", sha="1" * 64, header="2" * 64)}


class Box:
    """A fake container whose package manager is scripted per step. `downloads` is what
    the download step leaves in the (cleaned) cache; a successful cache-only install
    installs the cached pin, unless `install_effect` is "nothing" or the list of
    instances it leaves installed; `fail` maps a step to the (exit_code, output) it
    returns instead."""

    def __init__(self, family, *, downloads=None, installed=(), index=None, native=None,
                 install_effect="install", fail=None):
        self.family = family
        self.calls = []
        self.cache = [dict(DEP[family])]                          # stale files before the clean
        self.downloads = ([cached(family), dict(DEP[family])] if downloads is None else downloads)
        self.installed = list(installed)                          # (version, arch, header)
        self.index = [(NATIVE[family], SHA)] if index is None else index
        self.native = NATIVE[family] if native is None else native
        self.install_effect = install_effect
        self.fail = fail or {}

    # -- dispatch ---------------------------------------------------------------
    def step(self, cmd):
        if cmd == ["/bin/sh", "-c", "command -v dnf"]:
            return "probe_dnf"
        if cmd == ["/bin/sh", "-c", "command -v apt-get"]:
            return "probe_apt"
        if cmd == ["/bin/sh", "-c", "dnf clean expire-cache"]:
            return "expire"
        if cmd in (["/bin/sh", "-c", pm._APT_LIST_CACHE], ["/bin/sh", "-c", pm._DNF_LIST_CACHE]):
            return "list"
        if cmd[:2] == ["/bin/sh", "-c"]:
            return "lock_fuser" if "fuser" in cmd[2] else "lock_prep"
        if cmd == ["apt-get", "update"]:
            return "update"
        if cmd[0] in ("rpm", "dpkg-query") and cmd[1] in ("-q", "-W"):
            return "query"
        if cmd in (["apt-get", "clean"], ["dnf", "clean", "packages"]):
            return "clean"
        if "--download-only" in cmd or "--downloadonly" in cmd:
            return "download"
        if cmd in (["dpkg", "--print-architecture"], ["rpm", "--eval", "%{_arch}"]):
            return "native"
        if cmd[0] == "sha256sum":
            return "hash"
        if cmd[:2] == ["apt-cache", "show"]:
            return "index"
        if "install" in cmd:
            return "install"
        raise AssertionError("unexpected command %r" % (cmd,))

    def exec_run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        step = self.step(cmd)
        if step in self.fail:
            rc, out = self.fail[step]
            return rc, out.encode() if isinstance(out, str) else out
        return getattr(self, "_" + step)(cmd)

    # -- steps ------------------------------------------------------------------
    def _probe_dnf(self, cmd):
        return (0 if self.family == "rpm" else 1), b""

    def _probe_apt(self, cmd):
        return 0, b""

    def _expire(self, cmd):
        return 0, b""

    def _lock_prep(self, cmd):
        return 0, b""

    def _lock_fuser(self, cmd):
        return 0, b"apt lock is free"

    def _update(self, cmd):
        return 0, b"ok"

    def _query(self, cmd):
        assert cmd[-1] == PKG
        if not self.installed:
            return 1, (NOT_INSTALLED[self.family] + "\n").encode()
        if self.family == "rpm":
            return 0, "".join("%s\t%s\t%s\n" % i for i in self.installed).encode()
        return 0, "".join("install ok installed\t%s\t%s\n" % i[:2] for i in self.installed).encode()

    def _clean(self, cmd):
        self.cache = []
        return 0, b""

    def _download(self, cmd):
        self.cache = [dict(f) for f in self.downloads]
        return 0, b"downloaded"

    def _native(self, cmd):
        return 0, (self.native + "\n").encode()

    def _list(self, cmd):
        lines = []
        for f in self.cache:
            if not f["readable"]:
                lines.append(f["path"] + "\t")
            elif self.family == "rpm":
                lines.append("\t".join((f["path"], f["name"], f["version"], f["arch"], f["header"])))
            else:
                lines.append("\t".join((f["path"], f["name"], f["version"], f["arch"])))
        return 0, ("\n".join(lines) + "\n").encode()

    def _hash(self, cmd):
        f = next(f for f in self.cache if f["path"] == cmd[1])
        return 0, ("%s  %s\n" % (f["sha"], f["path"])).encode()

    def _index(self, cmd):
        assert cmd == ["apt-cache", "show", "%s=%s" % (PKG, PIN["deb"])]
        return 0, "".join("Package: %s\nVersion: %s\nArchitecture: %s\nSHA256: %s\n\n"
                          % (PKG, PIN["deb"], arch, sha) for arch, sha in self.index).encode()

    def _install(self, cmd):
        target = [f for f in self.cache if f["name"] == PKG and f["version"] == PIN[self.family]]
        if self.install_effect == "install" and target:
            f = target[0]
            self.installed = [(f["version"], f["arch"], f["header"] if self.family == "rpm" else "")]
        elif isinstance(self.install_effect, list):                 # something else got installed
            self.installed = list(self.install_effect)
        return 0, b"installed"

    # -- helpers ----------------------------------------------------------------
    def steps(self):
        return [self.step(cmd) for cmd, _ in self.calls]


def run(box, family=None):
    return pm.install_pinned(box, PKG, PIN[family or box.family])


# --------------------------------------------------------------------------- success


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_verified_install_returns_the_hashed_file_digest(family):
    box = Box(family)
    ok, out, digest = run(box)
    assert ok is True and digest == SHA
    assert "sha256=" + SHA in out and cached(family)["path"] in out
    assert box.installed[0][0] == PIN[family]


def test_deb_sequence_downloads_hashes_checks_the_index_then_installs_cache_only():
    box = Box("deb")
    run(box)
    assert box.steps() == ["probe_dnf", "probe_apt", "lock_prep", "lock_prep", "lock_fuser", "update",
                           "query", "clean", "download", "native", "list", "hash", "index",
                           "install", "query"]
    argv = dict((box.step(c), c) for c, _ in box.calls)
    assert argv["download"] == ["apt-get", "install", "-y", "--download-only", PKG + "=" + PIN["deb"]]
    assert argv["install"] == ["apt-get", "install", "-y", "--no-download", PKG + "=" + PIN["deb"]]
    envs = dict((box.step(c), kw.get("environment")) for c, kw in box.calls)
    assert envs["download"] == envs["install"] == {"DEBIAN_FRONTEND": "noninteractive"}


def test_rpm_sequence_downloads_hashes_installs_cache_only_then_ties_the_header():
    box = Box("rpm")
    run(box)
    assert box.steps() == ["probe_dnf", "expire", "query", "clean", "download", "native", "list",
                           "hash", "install", "query"]
    argv = dict((box.step(c), c) for c, _ in box.calls)
    assert argv["download"] == ["dnf", "install", "-y", "--downloadonly", PKG + "-" + PIN["rpm"]]
    assert argv["install"] == ["dnf", "-C", "install", "-y", PKG + "-" + PIN["rpm"]]


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_caller_data_never_crosses_a_shell(family):
    box = Box(family)
    run(box)
    for cmd, _ in box.calls:
        if cmd[:2] == ["/bin/sh", "-c"]:
            assert PKG not in cmd[2] and PIN[family] not in cmd[2]
            assert len(cmd) == 3                                 # no positional caller args either
    # the pin is ONE argv element, never split or interpolated
    specs = [c for c, _ in box.calls if box.step(c) in ("download", "install")]
    sep = "=" if family == "deb" else "-"
    assert all(PKG + sep + PIN[family] in c for c in specs)


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_an_arch_independent_file_is_accepted(family):
    f = cached(family, arch=INDEP[family])
    box = Box(family, downloads=[f], index=[(INDEP[family], SHA)])
    ok, _, digest = run(box)
    assert ok and digest == SHA


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_another_installed_version_is_replaced_by_the_verified_file(family):
    box = Box(family, installed=[("1.9.0-1", NATIVE[family], "9" * 64)])
    ok, _, digest = run(box)
    assert ok and digest == SHA and box.installed[0][0] == PIN[family]


# --------------------------------------------------------------- refusals before install


def _refused(box, needle):
    ok, out, digest = run(box)
    assert ok is False and digest is None
    assert needle in out, out
    return out


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_an_already_installed_pin_is_refused_as_a_no_op(family):
    box = Box(family, installed=[(PIN[family], NATIVE[family], HDR)])
    _refused(box, "already installed")
    assert not {"clean", "download", "install"} & set(box.steps())


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_a_failed_download_is_refused(family):
    box = Box(family, fail={"download": (100, "E: Unable to locate package")})
    _refused(box, "download failed: E: Unable to locate package")
    assert "install" not in box.steps()


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_a_failed_cache_clean_is_refused(family):
    box = Box(family, fail={"clean": (1, "busy")})
    _refused(box, "could not clean the package cache")
    assert "download" not in box.steps()


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_no_cached_file_for_the_pin_is_refused(family):
    box = Box(family, downloads=[dict(DEP[family])])             # only a dependency arrived
    _refused(box, "expected exactly one cached file")
    assert "install" not in box.steps()


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_two_cached_files_for_the_pin_are_ambiguous(family):
    box = Box(family, downloads=[cached(family), cached(family, fname="copy." + family, sha=OTHER_SHA)])
    out = _refused(box, "found 2")
    assert "copy." + family in out
    assert "install" not in box.steps()


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_a_wrong_arch_file_is_not_the_target(family):
    wrong = "amd64" if family == "deb" else "x86_64"
    box = Box(family, downloads=[cached(family, arch=wrong)])
    _refused(box, "found 0")


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_the_target_is_identified_by_file_metadata_not_file_name(family):
    # Named like the pin, but the file itself is another version: not the target.
    impostor = cached(family, version="2.0.0-2", fname=Path(cached(family)["path"]).name)
    box = Box(family, downloads=[impostor])
    _refused(box, "found 0")


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_an_unreadable_cached_file_is_never_selected(family):
    f = cached(family)
    f["readable"] = False
    box = Box(family, downloads=[f])
    _refused(box, "found 0")


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_an_unreadable_native_arch_is_refused(family):
    box = Box(family, native="%{_arch}" if family == "rpm" else "")
    _refused(box, "native architecture")


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_a_failed_hash_is_refused(family):
    box = Box(family, fail={"hash": (1, "sha256sum: No such file")})
    _refused(box, "could not hash")
    assert "install" not in box.steps()


def test_rpm_file_without_a_header_digest_is_refused_before_install():
    box = Box("rpm", downloads=[cached("rpm", header="(none)")])
    _refused(box, "no SHA256 header digest")
    assert "install" not in box.steps()


def test_deb_file_that_differs_from_the_index_digest_is_refused():
    # The cached bytes are not what APT's authenticated index records for the pin.
    box = Box("deb", index=[("arm64", OTHER_SHA)])
    _refused(box, "does not match the one digest the repository index records")
    assert "install" not in box.steps()


def test_deb_index_with_two_digests_for_the_pin_is_ambiguous():
    box = Box("deb", index=[("arm64", SHA), ("arm64", OTHER_SHA)])
    _refused(box, "does not match the one digest")
    assert "install" not in box.steps()


def test_deb_index_that_cannot_be_read_is_refused():
    box = Box("deb", fail={"index": (100, "E: No packages found")})
    _refused(box, "repository index")
    assert "install" not in box.steps()


def test_deb_index_records_for_a_foreign_arch_are_ignored():
    box = Box("deb", index=[("amd64", OTHER_SHA), ("arm64", SHA)])
    ok, _, digest = run(box)
    assert ok and digest == SHA


# --------------------------------------------------------- install and post-install tie


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_a_refused_cache_only_install_is_a_failure(family):
    # e.g. the package manager rejected changed cached bytes against its metadata
    box = Box(family, fail={"install": (1, 'has incorrect checksum')})
    _refused(box, "cache-only install failed: has incorrect checksum")


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_an_install_that_left_nothing_installed_cannot_be_tied(family):
    box = Box(family, install_effect="nothing")
    _refused(box, "cannot be tied to the verified file")


def test_rpm_install_with_another_header_cannot_be_tied():
    # same version-release, different build (the rebuild case): header digests differ
    box = Box("rpm", install_effect=[(PIN["rpm"], "aarch64", "9" * 64)])
    _refused(box, "cannot be tied to the verified file")


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_two_installed_instances_cannot_be_tied(family):
    h = HDR if family == "rpm" else ""
    box = Box(family, install_effect=[(PIN[family], NATIVE[family], h), (PIN[family], INDEP[family], h)])
    _refused(box, "cannot be tied")


# ------------------------------------------------------------------ preparation steps


def test_failed_apt_update_does_not_install():
    box = Box("deb", fail={"update": (1, "E: could not refresh index")})
    ok, out, digest = run(box)
    assert ok is False and digest is None
    assert "update" in out.lower() and "could not refresh index" in out
    assert not {"download", "install"} & set(box.steps())


def test_failed_dnf_refresh_does_not_install():
    box = Box("rpm", fail={"expire": (1, "Errors during downloading metadata")})
    ok, out, digest = run(box)
    assert ok is False and digest is None
    assert "expire-cache" in out.lower() and "Errors during downloading metadata" in out
    assert not {"download", "install"} & set(box.steps())


def test_failed_apt_lock_prep_does_not_update_or_install():
    # If lock preparation raises, install_pinned stops and runs NEITHER apt-get update
    # NOR anything after it.
    box = Box("deb", fail={"lock_fuser": (1, "lock still held")})
    ok, out, digest = run(box)
    assert ok is False and digest is None and "lock" in out.lower()
    assert not {"update", "download", "install"} & set(box.steps())


class _NoPkgMgr:
    """Neither dnf nor apt-get is present."""
    def __init__(self):
        self.calls = []

    def exec_run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return (1, b"")


def test_no_package_manager_is_clear_failure():
    c = _NoPkgMgr()
    ok, out, digest = pm.install_pinned(c, PKG, PIN["rpm"])
    assert ok is False and digest is None
    assert "no supported package manager" in out.lower()
    assert len(c.calls) == 2                                   # only the two probes


def test_unsafe_version_rejected_before_any_exec():
    # UnsafeVersionError is a programming/safety error and must PROPAGATE (never an
    # operational tuple), raised BEFORE any exec_run.
    box = Box("rpm")
    with pytest.raises(pm._pv.UnsafeVersionError):
        pm.install_pinned(box, PKG, "1.0.0; rm -rf /")
    assert box.calls == []


def test_apt_path_does_not_import_aspects_package(monkeypatch):
    # Regression (2026-08-20): the apt-lock dependency must load via the module's
    # own by-path shim, NOT `from aspects.configure_repository import ...`, so it
    # works when package_management.py is loaded by path without the repo root on
    # sys.path/PYTHONPATH. Guard __import__ to fail any 'aspects' import and prove
    # the DEB path still installs.
    import builtins
    real_import = builtins.__import__

    def guard(name, *a, **k):
        if name == "aspects" or name.startswith("aspects."):
            raise ModuleNotFoundError("No module named 'aspects' (simulated clean env)")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    box = Box("deb")
    ok, _, _ = run(box)
    assert ok and "install" in box.steps()


def test_cache_listings_read_metadata_from_each_file():
    # The constant listing scripts query each file's own metadata; they carry no
    # caller data and never trust a file name.
    assert "dpkg-deb" in pm._APT_LIST_CACHE and "${Package}" in pm._APT_LIST_CACHE
    assert "Dir::Cache::archives" in pm._APT_LIST_CACHE
    assert "rpm -qp" in pm._DNF_LIST_CACHE and "%{SHA256HEADER}" in pm._DNF_LIST_CACHE


# The listing scripts, run for real under /bin/sh. The package tools are faked on PATH:
# each prints the metadata line stored in the fake package file (as the real tool prints
# one per --showformat/--qf), writes a warning to stderr, and fails on an empty file.
_FAKE_TOOL = """#!/bin/sh
for f; do :; done
echo "warning: NOKEY" >&2
[ -s "$f" ] || { echo "error: not a package" >&2; exit 1; }
cat "$f"
"""


def _run_listing(tmp_path, script, files, env_extra=None):
    import os, subprocess
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("dpkg-deb", "rpm"):
        (bin_dir / tool).write_text(_FAKE_TOOL)
        (bin_dir / tool).chmod(0o755)
    (bin_dir / "apt-config").write_text("#!/bin/sh\necho \"ARCHIVES='$FAKE_ARCHIVES/'\"\n")
    (bin_dir / "apt-config").chmod(0o755)
    cache = tmp_path / "cache"
    cache.mkdir()
    for name, body in files.items():
        (cache / name).write_text(body)
    env = dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ["PATH"]), FAKE_ARCHIVES=str(cache))
    out = subprocess.run(["/bin/sh", "-c", script.replace("/var/cache/dnf", str(cache))],
                         capture_output=True, text=True, env=env, check=True)
    return [line.split("\t") for line in out.stdout.splitlines()], cache


def test_apt_listing_script_lists_each_cached_deb_with_its_metadata(tmp_path):
    rows, cache = _run_listing(tmp_path, pm._APT_LIST_CACHE, {
        "a_1_arm64.deb": "%s\t2.0.0-1.bookworm\tarm64\n" % PKG,
        "broken.deb": "",
        "notes.txt": "ignored\n"})
    assert rows == [[str(cache / "a_1_arm64.deb"), PKG, "2.0.0-1.bookworm", "arm64"],
                    [str(cache / "broken.deb"), ""]]           # unreadable: path only, never selectable


def test_dnf_listing_script_lists_each_cached_rpm_with_its_header_digest(tmp_path):
    assert pm._DNF_LIST_CACHE.count("/var/cache/dnf") == 1
    rows, cache = _run_listing(tmp_path, pm._DNF_LIST_CACHE, {
        "p-1.aarch64.rpm": "%s\t2.0.0-1.el9\taarch64\t%s\n" % (PKG, HDR),
        "broken.rpm": "",
        "x.deb": "ignored\n"})
    assert rows == [[str(cache / "broken.rpm"), ""],
                    [str(cache / "p-1.aarch64.rpm"), PKG, "2.0.0-1.el9", "aarch64", HDR]]


def test_empty_caches_list_nothing(tmp_path):
    rows, _ = _run_listing(tmp_path, pm._APT_LIST_CACHE, {})
    assert rows == []


# ------------------------------------------------------ output noise, holds, bad names


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_stray_output_lines_do_not_break_arch_or_hash_parsing(family):
    # e.g. a sudo warning on the SSH executor, which merges stderr into the output
    box = Box(family)
    real = box.exec_run

    def noisy(cmd, **kw):
        rc, out = real(cmd, **kw)
        if box.step(cmd) in ("native", "hash"):
            out = b"sudo: unable to resolve host ip-10-0-0-1: Name or service not known\n" + out
        return rc, out
    box.exec_run = noisy
    ok, _, digest = pm.install_pinned(box, PKG, PIN[family])
    assert ok and digest == SHA


def test_a_held_deb_pin_is_seen_as_installed():
    box = Box("deb", installed=[(PIN["deb"], "arm64", "")])
    box._query = lambda cmd: (0, ("hold ok installed\t%s\tarm64\n" % PIN["deb"]).encode())
    _refused(box, "already installed")


# ------------------------------------------ installed-package queries: never guess "absent"


def _queries(box, *responses):
    """Answer the successive installed-package queries with `responses` (rc, output);
    every other step behaves as scripted."""
    seq = list(responses)
    box._query = lambda cmd: (lambda rc, out: (rc, out.encode()))(*seq.pop(0))
    return box


@pytest.mark.parametrize("family,response", [
    ("deb", (2, "dpkg-query: error: failed to open package info file '/var/lib/dpkg/status'")),
    ("deb", (1, "dpkg-query: no packages found matching some-other-package")),
    ("deb", (0, "install ok installed 2.0.0-1.bookworm arm64")),       # not the requested format
    ("deb", (0, "")),                                                   # no row at all
    ("deb", (0, "install ok installed\t\tarm64")),                      # installed, no version
    ("rpm", (1, "error: rpmdb: BDB0113 Thread/process failed\npackage %s is not installed" % PKG)),
    ("rpm", (2, "error: cannot open Packages database")),
    ("rpm", (0, "2.0.0-1.el9 aarch64")),                                # not the requested format
    ("rpm", (0, "")),
])
def test_an_unreadable_installed_query_is_refused_before_any_download(family, response):
    box = _queries(Box(family), response)
    _refused(box, "could not determine whether the package is already installed")
    assert not {"clean", "download", "install"} & set(box.steps())


@pytest.mark.parametrize("state", ["unknown ok not-installed\t\t", "deinstall ok config-files\t2.0.0-1.bookworm\tarm64"])
def test_purged_or_config_files_deb_counts_as_not_installed(state):
    box = _queries(Box("deb"), (0, state + "\n"),
                   (0, "install ok installed\t%s\tarm64\n" % PIN["deb"]))
    ok, _, digest = run(box)
    assert ok and digest == SHA


@pytest.mark.parametrize("family", ["deb", "rpm"])
def test_an_unreadable_post_install_query_cannot_be_tied(family):
    box = _queries(Box(family), (1, NOT_INSTALLED[family]), (2, "error: database locked"))
    _refused(box, "cannot be tied to the verified file")


def test_deb_misread_as_absent_still_cannot_attest_a_no_op():
    # Defence in depth: the pin IS installed but the pre-check was misled. APT then
    # downloads nothing for it (it is already the newest version) into the cleaned
    # cache, so no cached file can be hashed and nothing is attested.
    box = _queries(Box("deb", installed=[(PIN["deb"], "arm64", "")], downloads=[dict(DEP["deb"])]),
                   (1, NOT_INSTALLED["deb"]))
    _refused(box, "expected exactly one cached file")
    assert "install" not in box.steps()


def test_rpm_misread_as_absent_cannot_attest_a_no_op_of_another_build():
    # rpm prints "not installed" even when its database is unreadable. If DNF then
    # fetches the file and the cache-only install is a no-op over an already-installed
    # build, the header tie still refuses unless the installed build IS that file.
    other_build = (PIN["rpm"], "aarch64", "9" * 64)
    box = _queries(Box("rpm", installed=[other_build], install_effect="nothing"),
                   (1, NOT_INSTALLED["rpm"]),
                   (0, "%s\t%s\t%s\n" % other_build))
    _refused(box, "cannot be tied to the verified file")


@pytest.mark.parametrize("name", ["-a", "--all", "pkg name", "", "pkg;rm", None, "/abs"])
def test_unsafe_package_name_is_rejected_before_any_exec(name):
    box = Box("rpm")
    with pytest.raises(ValueError):
        pm.install_pinned(box, name, PIN["rpm"])
    assert box.calls == []


# ------------------------------------------------------------------ read-only helpers


def test_query_installed_version_rpm():
    class C:
        def exec_run(self, cmd, **kw):
            if cmd == ["/bin/sh", "-c", "command -v dnf"]:
                return (0, b"")
            return (0, b"1.0.0-1.el9\n")
    assert pm.query_installed_version(C(), "pgedge-rag-server") == "1.0.0-1.el9"


def test_query_installed_version_deb():
    class C:
        def exec_run(self, cmd, **kw):
            if cmd == ["/bin/sh", "-c", "command -v dnf"]:
                return (1, b"")
            if cmd == ["/bin/sh", "-c", "command -v apt-get"]:
                return (0, b"")
            return (0, b"2.0.0~beta1-1.trixie")
    assert pm.query_installed_version(C(), "pgedge-rag-server") == "2.0.0~beta1-1.trixie"


def test_query_installed_version_returns_none_on_failure():
    class C:
        def exec_run(self, cmd, **kw):
            if cmd == ["/bin/sh", "-c", "command -v dnf"]:
                return (0, b"")
            return (1, b"package not installed")   # rpm -q fails
    assert pm.query_installed_version(C(), "pgedge-rag-server") is None


def test_query_binary_version_returns_raw_output():
    class C:
        def exec_run(self, cmd, **kw):
            assert cmd == ["/usr/bin/pgedge-rag-server", "-version"]
            return (0, b"Name: rag\nVersion: 2.0.0-beta1\n")
    out = pm.query_binary_version(C(), "/usr/bin/pgedge-rag-server")
    assert "Version: 2.0.0-beta1" in out


def test_query_binary_version_returns_none_on_failure():
    class C:
        def exec_run(self, cmd, **kw):
            return (127, b"not found")
    assert pm.query_binary_version(C(), "/usr/bin/pgedge-rag-server") is None
