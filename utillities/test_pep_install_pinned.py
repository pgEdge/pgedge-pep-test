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
    # NOTHING caller-derived ever goes through `sh -c` (only the constant dnf probe may)
    for cmd, _ in c.calls:
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[:2] == ["/bin/sh", "-c"]:
            assert cmd[2] == "command -v dnf"            # the ONLY sh -c allowed


def test_install_pinned_returns_success_flag():
    c = FakeContainer(has_dnf=True)
    ok, out = pm.install_pinned(c, "pgedge-rag-server", "1.0.0-1.el9")
    assert ok is True and isinstance(out, str)


def test_unsafe_version_rejected_before_any_exec():
    c = FakeContainer(has_dnf=True)
    with pytest.raises(pm._pv.UnsafeVersionError):
        pm.install_pinned(c, "pgedge-rag-server", "1.0.0; rm -rf /")
    assert c.calls == []          # rejected BEFORE any exec_run -- no probe, no install


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
