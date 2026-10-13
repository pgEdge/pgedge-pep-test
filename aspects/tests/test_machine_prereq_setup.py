"""Prerequisite setup in a root container (aspects/machine_prereq_setup.py).

Regression for the Oracle Linux 9 failures in RAG Release run 38242831031: every container
command already runs as root, but the OS handlers prefix `sudo`, and in oraclelinux:9 root's
sudo fails its PAM account check ("Authentication service cannot retrieve authentication
info"). file, wget and sequoia-sq were never installed, yet setup reported success.

Two kinds of test:
  * real /bin/sh runs with a stub `sudo` on PATH -- one that fails like that image, one that
    works -- comparing the root-shell command against the original command run with a
    working sudo, so the oracle is the shell itself, not the implementation;
  * a fake container for the setup flow (no Docker), where an un-shimmed `sudo` fails PAM.
"""
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # repo root: `aspects` imports
from aspects import machine_prereq_setup as M  # noqa: E402
from aspects import ssh_executor  # noqa: E402

OL9 = 'NAME="Oracle Linux Server"\nID="ol"\nVERSION_ID="9.8"\n'
ROCKY9 = 'NAME="Rocky Linux"\nID="rocky"\nVERSION_ID="9.3"\n'
DEBIAN12 = 'NAME="Debian GNU/Linux"\nID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n'
PAM = "sudo: PAM account management error: Authentication service cannot retrieve authentication info"


# --------------------------------------------------------------------------- #
# Real shell: the root-shell command behaves like the original with a working sudo
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def sudo_stubs(tmp_path_factory):
    """Two directories, each holding an executable `sudo`: 'broken' fails like root's sudo in
    oraclelinux:9; 'working' runs the command (dropping a leading -u <user>)."""
    root = tmp_path_factory.mktemp("sudo-stubs")
    scripts = {
        "broken": '#!/bin/sh\necho "%s" >&2\nexit 1\n' % PAM,
        "working": '#!/bin/sh\nif [ "$1" = -u ]; then shift 2; fi\nexec "$@"\n',
    }
    dirs = {}
    for name, body in scripts.items():
        d = root / name
        d.mkdir()
        p = d / "sudo"
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        dirs[name] = str(d)
    return dirs


def sh(argv, stub_dir):
    env = dict(os.environ, PATH=stub_dir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"))
    r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout


SHELL_CASES = [
    "printf '%s\\n' 'if sudo echo example'",           # quoted data that looks like commands
    "printf '%s\\n' 'x; sudo echo example'",
    'echo "pseudo sudoers; sudo is a word here"',
    "sudo echo plain",
    "false || sudo echo fallback",
    "echo before && sudo echo after",
    "if sudo true; then echo yes; else echo no; fi",
    "(sudo echo subshell)",
    "sudo sh -c 'echo \"a; b\"'",                        # quoted arguments reach the command intact
    "x=1; sudo printf '%s|%s\\n' \"$x\" 'two words'",
    "sudo false; echo \"exit=$?\"",                       # exit status propagates
    "for v in a b; do sudo echo \"$v\"; done",
    "sudo dnf-not-installed-anywhere 2>/dev/null || echo missing-command-handled",
]


@pytest.mark.parametrize("cmd", SHELL_CASES)
def test_root_shell_runs_like_the_original_with_a_working_sudo(cmd, sudo_stubs):
    want = sh(["/bin/sh", "-c", cmd], sudo_stubs["working"])
    got = sh(M.root_shell_command(cmd), sudo_stubs["broken"])
    assert got == want


# Cases whose result depends on a working sudo (the rest are literal text, or fail either way).
SUDO_DEPENDENT = {c for c in SHELL_CASES if c.startswith(("sudo echo", "false ||", "echo before", "if sudo",
                                                           "(sudo", "sudo sh", "x=1", "for v"))}


@pytest.mark.parametrize("cmd", SHELL_CASES)
def test_without_the_shim_the_broken_sudo_reproduces_the_oel9_failure(cmd, sudo_stubs):
    """Control: the broken stub changes exactly the sudo-dependent cases when run unshimmed,
    so the comparison above is not passing vacuously."""
    want = sh(["/bin/sh", "-c", cmd], sudo_stubs["working"])
    raw = sh(["/bin/sh", "-c", cmd], sudo_stubs["broken"])
    assert (raw != want) == (cmd in SUDO_DEPENDENT)


@pytest.mark.parametrize("cmd", ["sudo -u nobody echo switched", "sudo -E echo kept"])
def test_sudo_with_options_still_uses_the_real_sudo(cmd, sudo_stubs):
    """User switching (and any other sudo option) is never silently dropped."""
    for stubs in ("broken", "working"):
        assert sh(M.root_shell_command(cmd), sudo_stubs[stubs]) == sh(["/bin/sh", "-c", cmd], sudo_stubs[stubs])
    assert sh(M.root_shell_command(cmd), sudo_stubs["broken"])[0] == 1


def test_ssh_executor_command_runs_through_a_real_shell(sudo_stubs):
    """On AWS, SSHExecutor sends `sudo <quoted argv>`; the remote login shell runs it. The outer
    sudo is SSHExecutor's own elevation (working there); the handler text survives quoting."""
    sent = []

    class Stream:
        def __init__(self):
            self.channel = self

        def settimeout(self, t):
            pass

        def read(self):
            return b""

        def recv_exit_status(self):
            return 0

    class Client:
        def exec_command(self, cmd, timeout):
            sent.append(cmd)
            return None, Stream(), Stream()

    ex = ssh_executor.SSHExecutor.__new__(ssh_executor.SSHExecutor)
    ex._client, ex._max_retries, ex._host = Client(), 1, "test-host"
    ex._ensure_connected = lambda: None
    cmd = "printf '%s\\n' 'x; sudo echo example' && sudo echo elevated"
    ex.exec_run(M.root_shell_command(cmd), user="root")
    assert sent[0].startswith("sudo /bin/sh -c ")
    assert sh(["/bin/sh", "-c", sent[0]], sudo_stubs["working"]) == \
        sh(["/bin/sh", "-c", cmd], sudo_stubs["working"])


# --------------------------------------------------------------------------- #
# Setup flow with a fake container
# --------------------------------------------------------------------------- #
SUDO_WORD = re.compile(r"(^|[;&|(]\s*|\b(?:if|then|else|do)\s+)sudo\s")    # the fake's PAM model only
TOOL_OF = {"file": "file", "wget": "wget", "sequoia-sq": "sq", "sq": "sq"}


class FakeContainer:
    """A container whose commands run as root. With sudo_works=False it models oraclelinux:9:
    an un-shimmed `sudo` fails PAM. `broken` lists command fragments that fail on their own."""

    def __init__(self, os_release, sudo_works=False, broken=()):
        self.os_release, self.sudo_works, self.broken = os_release, sudo_works, tuple(broken)
        self.installed, self.calls = set(), []

    def exec_run(self, cmd, user=None, **kw):
        text = cmd[-1] if isinstance(cmd, list) else cmd
        self.calls.append((text, user))
        if text == "cat /etc/os-release":
            return 0, self.os_release.encode()
        probe = re.fullmatch(r"command -v (\S+)", text)
        if probe:
            return (0 if probe.group(1) in self.installed else 1), b""
        shimmed = text.startswith(M.ROOT_SUDO_SHIM)
        body = text[len(M.ROOT_SUDO_SHIM):] if shimmed else text
        if not (shimmed or self.sudo_works) and SUDO_WORD.search(body):
            return 1, (PAM + "\n").encode()
        if any(b in body for b in self.broken):
            return 1, b"Error: Unable to find a match\n"
        for pkg in re.findall(r"(?:dnf|apt-get) install -y (\S+)", body):
            if pkg in TOOL_OF:
                self.installed.add(TOOL_OF[pkg])
        return 0, b""

    def setup_commands(self):
        return [c for c, _ in self.calls if c != "cat /etc/os-release" and not c.startswith("command -v")]


@pytest.fixture(autouse=True)
def _restore_executor():
    yield
    M.set_executor(M.default_run)


@pytest.fixture
def no_apt_lock_wait(monkeypatch):
    import aspects.configure_repository as configure_repository
    monkeypatch.setattr(configure_repository, "_wait_for_apt_lock", lambda container: None)


def test_oel9_root_container_installs_essentials():
    c = FakeContainer(OL9)
    ok, os_info, message = M.install_prerequisites_on_container(c)
    assert (ok, os_info) == (True, "ol 9") and "successfully" in message
    assert c.installed >= {"file", "wget", "sq"}
    cmds = c.setup_commands()
    assert cmds and all(x.startswith(M.ROOT_SUDO_SHIM) for x in cmds)
    assert M.ROOT_SUDO_SHIM + "sudo dnf install -y wget" in cmds      # handler text unchanged
    assert {user for _, user in c.calls} == {"root"}                   # every command runs as root


def test_the_original_oel9_failure_is_no_longer_reported_as_success(monkeypatch):
    """Replays run 38242831031: no shim, so every privileged install fails PAM."""
    monkeypatch.setattr(M, "root_shell_command", lambda cmd: ["/bin/sh", "-c", cmd])
    c = FakeContainer(OL9)
    with pytest.raises(Exception, match=r"essential tools missing on ol 9: file, wget$"):
        M.install_prerequisites_on_container(c)


def test_root_container_where_sudo_works_is_unchanged():
    c = FakeContainer(ROCKY9, sudo_works=True)
    assert M.install_prerequisites_on_container(c)[0] is True
    assert c.installed >= {"file", "wget", "sq"}


def test_debian_requires_file_but_not_wget(no_apt_lock_wait):
    c = FakeContainer(DEBIAN12)
    assert M.install_prerequisites_on_container(c)[:2] == (True, "debian 12")
    assert "wget" not in c.installed                                   # not a DEB prerequisite


@pytest.mark.parametrize("broken, missing", [
    ("install -y wget", "wget"),
    ("install -y file", "file"),
])
def test_a_failed_essential_install_fails_setup(broken, missing):
    with pytest.raises(Exception, match=r"essential tools missing on ol 9: %s$" % missing):
        M.install_prerequisites_on_container(FakeContainer(OL9, broken=[broken]))


def test_debian_missing_file_fails_setup(no_apt_lock_wait):
    with pytest.raises(Exception, match=r"essential tools missing on debian 12: file$"):
        M.install_prerequisites_on_container(FakeContainer(DEBIAN12, broken=["install -y file"]))


@pytest.mark.parametrize("os_release, broken", [
    (OL9, "install -y sequoia-sq"),
    (DEBIAN12, "install -y sq"),
], ids=["ol9", "debian12"])
def test_a_failed_sq_install_is_left_to_ensure_sq_installed(os_release, broken, no_apt_lock_wait):
    """sq has its own repair-and-retry before the SBOM step, so setup does not require it."""
    c = FakeContainer(os_release, broken=[broken])
    assert M.install_prerequisites_on_container(c)[0] is True
    assert "sq" not in c.installed


# Every OS/major the dispatcher handles, as /etc/os-release reports it, including platforms
# outside RAG's matrix (disabled in containers_list.json, reachable by explicit override).
SUPPORTED = {
    "rhel 9": 'ID="rhel"\nVERSION_ID="9.6"\n', "rhel 10": 'ID="rhel"\nVERSION_ID="10.0"\n',
    "rocky 9": ROCKY9, "rocky 10": 'ID="rocky"\nVERSION_ID="10.0"\n',
    "ol 9": OL9, "ol 10": 'ID="ol"\nVERSION_ID="10.0"\n',
    "almalinux 9": 'ID="almalinux"\nVERSION_ID="9.6"\n', "almalinux 10": 'ID="almalinux"\nVERSION_ID="10.0"\n',
    "debian 11": 'ID=debian\nVERSION_ID="11"\n', "debian 12": DEBIAN12, "debian 13": 'ID=debian\nVERSION_ID="13"\n',
    "ubuntu 22": 'ID=ubuntu\nVERSION_ID="22.04"\n', "ubuntu 24": 'ID=ubuntu\nVERSION_ID="24.04"\n',
    "ubuntu 26": 'ID=ubuntu\nVERSION_ID="26.04"\n',
}


@pytest.mark.parametrize("os_info", sorted(SUPPORTED))
def test_every_supported_handler_installs_the_essential_tools(os_info, no_apt_lock_wait):
    """The essential list must never name a tool some handler does not install."""
    c = FakeContainer(SUPPORTED[os_info])
    assert M.install_prerequisites_on_container(c)[:2] == (True, os_info)
    family = "deb" if os_info.split()[0] in ("debian", "ubuntu") else "rpm"
    assert set(M.ESSENTIAL_TOOLS[family]) <= c.installed


@pytest.mark.parametrize("os_id, name", [("rocky", "Rocky"), ("almalinux", "AlmaLinux"), ("ol", "Oracle Linux")])
def test_el8_still_fails_early_as_unsupported(os_id, name):
    """EL8 images exist in CONTAINER_IMAGES but have no handler; that early failure predates this check."""
    c = FakeContainer('ID="%s"\nVERSION_ID="8.10"\n' % os_id)
    with pytest.raises(Exception, match=r"Unsupported %s version: 8$" % name):
        M.install_prerequisites_on_container(c)
    assert c.setup_commands() == []


def test_optional_setup_failures_stay_warnings(capsys):
    c = FakeContainer(OL9, broken=["epel-release", "ol9_codeready_builder", "python3-psycopg2"])
    assert M.install_prerequisites_on_container(c)[0] is True
    out = capsys.readouterr().out
    assert out.count("WARNING: Command failed") == 3 and "Prerequisites installed successfully" in out


def test_unsupported_os_is_still_skipped_not_failed():
    c = FakeContainer('ID="plan9"\nVERSION_ID="4"\n')
    assert M.install_prerequisites_on_container(c) == (True, "plan9 4", "Unsupported OS - prerequisites skipped")


# --------------------------------------------------------------------------- #
# Standalone (possibly non-root) execution keeps its privilege handling
# --------------------------------------------------------------------------- #
def test_standalone_handlers_keep_sudo():
    seen = []
    M.set_executor(lambda cmd: seen.append(cmd) or 0)
    M.setup_oracle9()
    assert "sudo dnf install -y wget" in seen and "sudo dnf install -y file" in seen


def test_standalone_default_executor_runs_commands_unchanged(monkeypatch):
    ran = []

    class Done:
        returncode = 0
    monkeypatch.setattr(M.subprocess, "run", lambda cmd, shell: ran.append(cmd) or Done())
    assert M.default_run("sudo dnf install -y wget") == 0
    assert ran == ["sudo dnf install -y wget"]
