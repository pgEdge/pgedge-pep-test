"""Docker-free structural tests for aspects.package_management.install_pinned:
a caller-supplied version is a single argv element and never crosses `sh -c`."""
import importlib.util
import sys
from pathlib import Path

import pytest

_p = Path(__file__).resolve().parent.parent / "aspects" / "package_management.py"
_spec = importlib.util.spec_from_file_location("pm_inst", str(_p))
pm = importlib.util.module_from_spec(_spec)
sys.modules["pm_inst"] = pm
_spec.loader.exec_module(pm)


class FakeContainer:
    def __init__(self, has_dnf=True):
        self.calls = []            # list of (cmd, kwargs)
        self._has_dnf = has_dnf

    def exec_run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd == ["/bin/sh", "-c", "command -v dnf"]:
            return (0 if self._has_dnf else 1, b"")
        return (0, b"ok")          # install / apt-get update succeed


def _install_calls(c):
    # the actual install call is the one containing 'install'
    return [cmd for cmd, _ in c.calls if isinstance(cmd, list) and "install" in cmd]


def _index_of_install(c):
    for i, (cmd, _) in enumerate(c.calls):
        if isinstance(cmd, list) and "install" in cmd:
            return i
    return -1


@pytest.mark.parametrize("has_dnf,sep,mgr", [(True, "-", "dnf"), (False, "=", "apt-get")])
def test_pkg_spec_is_single_argv_element_never_shell(has_dnf, sep, mgr):
    c = FakeContainer(has_dnf=has_dnf)
    ok, _ = pm.install_pinned(c, "pgedge-rag-server", "1.0.0-1.el9")
    assert ok
    inst = _install_calls(c)
    assert len(inst) == 1
    argv = inst[0]
    assert argv[0] == mgr and isinstance(argv, list)     # argv list, not a string
    # the version is ONE element, exactly package{sep}version, not split or interpolated
    assert f"pgedge-rag-server{sep}1.0.0-1.el9" in argv
    # NO caller-derived data (package name / version) ever crosses an `sh -c`. The
    # constant prep commands (probes, `dnf clean expire-cache`, apt lock/update) may
    # use `sh -c`, but they never contain the caller's package or version tokens.
    for cmd, _ in c.calls:
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[:2] == ["/bin/sh", "-c"]:
            assert "pgedge-rag-server" not in cmd[2] and "1.0.0-1.el9" not in cmd[2]


def test_install_pinned_returns_success_flag():
    c = FakeContainer(has_dnf=True)
    ok, out = pm.install_pinned(c, "pgedge-rag-server", "1.0.0-1.el9")
    assert ok is True and isinstance(out, str)


def test_unsafe_version_rejected_before_any_exec():
    c = FakeContainer(has_dnf=True)
    with pytest.raises(pm._pv.UnsafeVersionError):
        pm.install_pinned(c, "pgedge-rag-server", "1.0.0; rm -rf /")
    assert c.calls == []          # rejected BEFORE any exec_run -- no probe, no install


def test_dnf_metadata_refreshed_before_pinned_install():
    # DNF path must expire stale repo metadata (like install_package) so a
    # just-published exact RPM is resolvable -- and that refresh must run BEFORE
    # the install, not after.
    c = FakeContainer(has_dnf=True)
    ok, _ = pm.install_pinned(c, "pgedge-rag-server", "1.0.0-1.el9")
    assert ok
    refresh_idx = next((i for i, (cmd, _) in enumerate(c.calls)
                        if cmd == ["/bin/sh", "-c", "dnf clean expire-cache"]), -1)
    assert refresh_idx >= 0                        # metadata refresh happened
    assert refresh_idx < _index_of_install(c)      # ...and before the install


def test_apt_lock_and_update_prep_before_install():
    # APT path must wait out the apt/dpkg lock and refresh the index BEFORE
    # installing (preserving install_package's Debian preparation).
    c = FakeContainer(has_dnf=False)
    ok, _ = pm.install_pinned(c, "pgedge-rag-server", "2.0.0~beta1-1.trixie")
    assert ok
    inst = _index_of_install(c)
    lock_idx = next((i for i, (cmd, _) in enumerate(c.calls)
                     if isinstance(cmd, list) and len(cmd) >= 3
                     and cmd[:2] == ["/bin/sh", "-c"] and "fuser" in cmd[2]), -1)
    update_idx = next((i for i, (cmd, _) in enumerate(c.calls)
                       if cmd == ["apt-get", "update"]), -1)
    assert 0 <= lock_idx < inst                    # lock wait ran before install
    assert 0 <= update_idx < inst                  # index refresh ran before install
    assert lock_idx < update_idx                   # lock wait precedes the refresh


class _AptUpdateFailsContainer:
    """apt-get exists and the lock clears, but `apt-get update` fails."""
    def __init__(self):
        self.calls = []
    def exec_run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd == ["/bin/sh", "-c", "command -v dnf"]:
            return (1, b"")                        # not RHEL
        if cmd == ["apt-get", "update"]:
            return (1, b"E: could not refresh index")
        return (0, b"ok")                          # apt-get probe + lock prep succeed


def test_failed_apt_update_does_not_install():
    # A failed index refresh must NOT fall through to an install against a stale
    # index -- it returns (False, <message>) and never issues the install.
    c = _AptUpdateFailsContainer()
    ok, out = pm.install_pinned(c, "pgedge-rag-server", "2.0.0~beta1-1.trixie")
    assert ok is False
    assert "update" in out.lower()
    assert "could not refresh index" in out        # preserves the command's output detail
    assert _index_of_install(c) == -1              # install never attempted


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
    c = FakeContainer(has_dnf=False)
    ok, _ = pm.install_pinned(c, "pgedge-rag-server", "2.0.0~beta1-1.trixie")
    assert ok
    assert _index_of_install(c) != -1              # DEB install ran, no 'aspects' import needed


class _DnfExpireFailsContainer:
    """dnf exists but `dnf clean expire-cache` fails."""
    def __init__(self):
        self.calls = []
    def exec_run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd == ["/bin/sh", "-c", "command -v dnf"]:
            return (0, b"")
        if cmd == ["/bin/sh", "-c", "dnf clean expire-cache"]:
            return (1, b"Errors during downloading metadata")
        return (0, b"ok")


def test_failed_dnf_refresh_does_not_install():
    # A failed metadata refresh must NOT fall through to a pinned install against a
    # stale cache -- it returns (False, <message>) and never issues the install.
    c = _DnfExpireFailsContainer()
    ok, out = pm.install_pinned(c, "pgedge-rag-server", "1.0.0-1.el9")
    assert ok is False
    assert "expire-cache" in out.lower()
    assert "Errors during downloading metadata" in out   # preserves the command's output detail
    assert _index_of_install(c) == -1              # install never attempted


class _AptLockFailsContainer:
    """apt-get exists but the apt/dpkg lock never frees, so _wait_for_apt_lock raises."""
    def __init__(self):
        self.calls = []
    def exec_run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd == ["/bin/sh", "-c", "command -v dnf"]:
            return (1, b"")
        if (isinstance(cmd, list) and len(cmd) >= 3 and cmd[:2] == ["/bin/sh", "-c"]
                and "fuser" in cmd[2]):
            return (1, b"lock still held")          # poll never clears -> raises
        return (0, b"ok")


def test_failed_apt_lock_prep_does_not_update_or_install():
    # If lock preparation raises, install_pinned returns (False, <message>) and runs
    # NEITHER apt-get update NOR the install afterwards.
    c = _AptLockFailsContainer()
    ok, out = pm.install_pinned(c, "pgedge-rag-server", "2.0.0~beta1-1.trixie")
    assert ok is False
    assert "lock" in out.lower()                    # preserves the raised exception detail
    assert ["apt-get", "update"] not in [cmd for cmd, _ in c.calls]
    assert _index_of_install(c) == -1


def test_unsafe_version_still_raises_not_operational_tuple():
    # UnsafeVersionError is a programming/safety error and must PROPAGATE, never be
    # converted into a (False, msg) operational tuple.
    c = FakeContainer(has_dnf=True)
    with pytest.raises(pm._pv.UnsafeVersionError):
        pm.install_pinned(c, "pgedge-rag-server", "1.0.0; rm -rf /")


class _NoPkgMgrContainer:
    """Neither dnf nor apt-get is present."""
    def __init__(self):
        self.calls = []
    def exec_run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return (1, b"")                            # every `command -v` fails


def test_no_package_manager_is_clear_failure():
    c = _NoPkgMgrContainer()
    ok, out = pm.install_pinned(c, "pgedge-rag-server", "1.0.0-1.el9")
    assert ok is False
    assert "no supported package manager" in out.lower()
    assert _index_of_install(c) == -1              # nothing installed


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
