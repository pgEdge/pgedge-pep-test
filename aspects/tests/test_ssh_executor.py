"""SSHExecutor.exec_run output shapes (aspects/ssh_executor.py).

With AWS_MODE=true, component-test/conftest.py makes docker.from_env() return an
AWSInstanceClient whose containers.get() is an SSHExecutor, so it must answer exec_run the way
docker-py does: (exit_code, output bytes), with stdout and stderr merged, or with demux=True
(exit_code, (stdout, stderr)), each stream None when empty. ACE's binary version check asks for
demux=True and parses stdout only.

Network-free: paramiko.SSHClient is replaced by a fake, so the real constructor, connection check,
command building and retry loop run without opening a connection or touching cloud resources.
"""
import importlib.util
import itertools
import os
import sys
from pathlib import Path

import docker
import paramiko
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # repo root: `aspects` imports
from aspects import aws_client, ssh_executor  # noqa: E402

_ACE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "component-test", "test_pep_ace.py")
_counter = itertools.count()
_INSTANCE = {"name": "rocky9-amd64", "host": "203.0.113.10", "username": "ec2-user", "enabled": True}


class _Stream:
    """One side of a paramiko exec_command; stdout's channel carries the exit status."""

    def __init__(self, data, exit_code=0):
        self.data, self.exit_code, self.channel = data, exit_code, self

    def read(self):
        return self.data

    def settimeout(self, timeout):
        pass

    def recv_exit_status(self):
        return self.exit_code


@pytest.fixture
def ssh(monkeypatch):
    """Install a fake paramiko.SSHClient answering each exec_command from ``replies`` in turn:
    (stdout, stderr, exit_code) or an exception to raise. Returns the record of what was sent."""
    monkeypatch.delenv("AWS_SSH_KEY_PATH", raising=False)
    monkeypatch.setattr(ssh_executor.time, "sleep", lambda seconds: None)
    record = {"replies": [], "sent": [], "connects": 0}

    class FakeSSHClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            record["connects"] += 1

        def get_transport(self):
            return self

        def is_active(self):
            return True

        def close(self):
            pass

        def exec_command(self, cmd, timeout):
            record["sent"].append((cmd, timeout))
            reply = record["replies"].pop(0)
            if isinstance(reply, Exception):
                raise reply
            out, err, exit_code = reply
            return None, _Stream(out, exit_code), _Stream(err)

    monkeypatch.setattr(ssh_executor.paramiko, "SSHClient", FakeSSHClient)
    return record


def executor(ssh, *replies):
    ssh["replies"].extend(replies)
    return ssh_executor.SSHExecutor(_INSTANCE["name"], _INSTANCE["host"], _INSTANCE["username"])


def test_demux_returns_stdout_and_stderr_separately(ssh):
    ex = executor(ssh, (b"ace version 3.0.0\n", b"WARN config file 'ace.yaml' not found\n", 0))
    assert ex.exec_run(["/usr/bin/ace", "--version"], user="root", demux=True) == \
        (0, (b"ace version 3.0.0\n", b"WARN config file 'ace.yaml' not found\n"))


@pytest.mark.parametrize("out,err,streams", [
    (b"out\n", b"", (b"out\n", None)),
    (b"", b"err\n", (None, b"err\n")),
    (b"", b"", (None, None)),
])
def test_demux_uses_none_for_an_empty_stream(ssh, out, err, streams):
    assert executor(ssh, (out, err, 0)).exec_run("true", demux=True) == (0, streams)


@pytest.mark.parametrize("kwargs", [{}, {"demux": False}])
def test_without_demux_the_output_stays_merged_bytes(ssh, kwargs):
    ex = executor(ssh, (b"out\n", b"err\n", 0), (b"only\n", b"", 0))
    assert ex.exec_run("cmd", **kwargs) == (0, b"out\nerr\n")
    assert ex.exec_run("cmd", **kwargs) == (0, b"only\n")


@pytest.mark.parametrize("demux,expected", [
    (True, (3, (None, b"boom\n"))),
    (False, (3, b"boom\n")),
])
def test_a_non_zero_exit_code_keeps_the_output_shape(ssh, demux, expected):
    assert executor(ssh, (b"", b"boom\n", 3)).exec_run("false", demux=demux) == expected


def test_command_user_and_timeout_are_unchanged(ssh):
    ex = executor(ssh, (b"", b"", 0), (b"", b"", 0))
    ex.exec_run(["/usr/bin/ace", "--version"], user="root", demux=True, timeout=42)
    ex.exec_run("psql -c 'select 1'", user="postgres")
    assert ssh["sent"] == [("sudo /usr/bin/ace --version", 42),
                           ("sudo -u postgres psql -c 'select 1'", 300)]


def test_a_transient_ssh_error_is_retried_and_keeps_the_demux_shape(ssh):
    ex = executor(ssh, paramiko.SSHException("connection reset"), (b"ace version 3.0.0\n", b"", 0))
    assert ex.exec_run(["/usr/bin/ace", "--version"], demux=True) == (0, (b"ace version 3.0.0\n", None))
    assert ssh["connects"] == 2


# ACE's real binary version check through the AWS_MODE route: docker.from_env() returns an
# AWSInstanceClient (as component-test/conftest.py arranges) whose container is an SSHExecutor.
def run_ace_over_ssh(monkeypatch, ssh, expected, reply):
    monkeypatch.setenv("PGEDGE_ACE_VERSION", expected)
    monkeypatch.setattr(aws_client, "load_aws_instances", lambda: {_INSTANCE["name"]: _INSTANCE})
    monkeypatch.setattr(docker, "from_env", lambda **kwargs: aws_client.AWSInstanceClient())
    name = "ace_over_ssh_%d" % next(_counter)
    spec = importlib.util.spec_from_file_location(name, _ACE_PATH)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    ssh["replies"].append(reply)
    try:
        mod.test_ace_binary_version(_INSTANCE["name"], "rhel")
    except (AssertionError, pytest.fail.Exception) as e:
        return "failed", str(e)
    return "passed", ""


def test_the_ace_version_check_reads_stdout_over_ssh_despite_a_stderr_warning(monkeypatch, ssh):
    reply = (b"ace version 3.0.0-rc.3\n", b"WARN config file 'ace.yaml' not found\n", 0)
    assert run_ace_over_ssh(monkeypatch, ssh, "3.0.0-rc.3", reply) == ("passed", "")
    assert ssh["sent"] == [("sudo /usr/bin/ace --version", 300)]


def test_the_ace_version_check_rejects_a_wrong_version_over_ssh(monkeypatch, ssh):
    outcome, message = run_ace_over_ssh(monkeypatch, ssh, "3.0.0-rc.3", (b"ace version 3.0.0-rc.2\n", b"", 0))
    assert outcome == "failed"
    assert "3.0.0-rc.2" in message
