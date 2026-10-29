"""Repository client certificate delivery and setup failures (aspects/pgedge_client_cert.py).

The pgEdge repositories authenticate clients with mutual TLS, so the pair must reach the target
without the key ever being readable by anyone but root, and a failed setup must say so in a way
pep-integration.yml can classify as infrastructure (a one-word marker), not as a product failure.

Network-free, synthetic credentials only:
  * Docker-like targets: the pair goes over put_archive, as before.
  * SSH targets: the REAL SSHExecutor runs against a fake paramiko client that executes each
    command with the local /bin/sh, with stub ``sudo`` and ``pgedge-subscription-manager`` on
    PATH and a stub ``cat`` that records the mode of the file being written. SFTP is refused, so
    the test also proves the key never goes through it.
  * Repository setup (configure_repository) against a fake container.
  * Transfer failures in third-party code (paramiko's real send path, a Docker transport, the
    local subprocess), checked in the failure as pytest renders it for the JUnit/HTML reports.
"""
import io
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import paramiko
import pytest
from paramiko.channel import ChannelFile, ChannelStderrFile, ChannelStdinFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # repo root: `aspects` imports
from aspects import configure_repository, pgedge_client_cert, ssh_executor  # noqa: E402

CRT = "-----BEGIN CERTIFICATE-----\nc3ludGhldGljLWxlYWY=\n-----END CERTIFICATE-----\n" \
      "-----BEGIN CERTIFICATE-----\nc3ludGhldGljLWNh\n-----END CERTIFICATE-----"
KEY = "-----BEGIN PRIVATE KEY-----\nc3ludGhldGljLXByaXZhdGUta2V5\n-----END PRIVATE KEY-----"
KEY_BODY = "c3ludGhldGljLXByaXZhdGUta2V5"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for name in ("PGEDGE_REPO_CLIENT_CRT", "PGEDGE_REPO_CLIENT_KEY", "PGEDGE_REPO_CLIENT_CRT_FILE",
                 "PGEDGE_REPO_CLIENT_KEY_FILE", "PEP_REPO_AUTH_FAILURE_FILE", "AWS_SSH_KEY_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SSH_KEY_PATH", "")


@pytest.fixture
def marker(monkeypatch, tmp_path):
    path = tmp_path / "test-logs" / "repo-auth-failure"
    monkeypatch.setenv("PEP_REPO_AUTH_FAILURE_FILE", str(path))
    return path


# --------------------------------------------------------------------------- #
# Empty and partial pairs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("crt,key", [("", ""), (CRT, ""), ("", KEY), ("  ", KEY), (CRT, "\n")])
def test_an_empty_or_partial_pair_is_no_certificate(monkeypatch, crt, key):
    monkeypatch.setenv("PGEDGE_REPO_CLIENT_CRT", crt)
    monkeypatch.setenv("PGEDGE_REPO_CLIENT_KEY", key)
    assert pgedge_client_cert.read_client_certificate() == (None, None)


def test_without_the_marker_variable_a_setup_failure_writes_nothing(tmp_path):
    pgedge_client_cert.record_setup_failure("certificate")
    assert list(tmp_path.rglob("repo-auth-failure")) == []


def rendered(excinfo):
    """The failure as pytest renders it in reports: long style, function arguments, chains."""
    return str(excinfo.getrepr(funcargs=True, style="long", chain=True))


def test_the_pair_read_from_the_environment_never_reprs_its_value(monkeypatch):
    monkeypatch.setenv("PGEDGE_REPO_CLIENT_CRT", CRT)
    monkeypatch.setenv("PGEDGE_REPO_CLIENT_KEY", KEY)
    crt, key = pgedge_client_cert.read_client_certificate()
    assert (crt, key) == (CRT, KEY) and f"{key}" == KEY
    assert KEY_BODY not in repr(key) and "c3ludGhldGljLWxlYWY" not in repr(crt)
    assert all(KEY_BODY not in repr(data) for _, data, _ in pgedge_client_cert._files(crt, key))
    assert KEY_BODY not in repr(pgedge_client_cert.tar_payload(crt, key))


# --------------------------------------------------------------------------- #
# Docker-like target: unchanged put_archive delivery
# --------------------------------------------------------------------------- #
class DockerTarget:
    def __init__(self, enable_exit=0, enable_output=b""):
        self.commands, self.archives = [], []
        self.enable_exit, self.enable_output = enable_exit, enable_output

    def exec_run(self, cmd, user=None, **kwargs):
        self.commands.append(cmd[-1])
        if "pgedge-subscription-manager" in cmd[-1]:
            return self.enable_exit, self.enable_output
        return 0, b""

    def put_archive(self, path, data):
        self.archives.append((path, data))
        return True


def test_a_docker_target_gets_the_pair_as_a_root_owned_archive():
    target = DockerTarget()
    pgedge_client_cert.install_on_target(target, "rhel", CRT, KEY)
    [(path, data)] = target.archives
    assert path == "/etc/pki/pgedge"
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = {m.name: (m.mode, m.uid, m.gid) for m in tar.getmembers()}
    assert members == {"client.crt": (0o644, 0, 0), "client.key": (0o600, 0, 0)}
    assert all(KEY_BODY not in c and "CERTIFICATE" not in c for c in target.commands)


def test_a_rejected_pair_on_docker_is_marked_as_a_certificate_failure(marker):
    target = DockerTarget(enable_exit=1, enable_output=b"ERROR: that certificate does not match the key")
    with pytest.raises(Exception) as err:
        pgedge_client_cert.install_on_target(target, "ubuntu", CRT, KEY)
    assert "does not match" in str(err.value)
    # Reports render the failing frame's arguments; the key must not be among them.
    assert KEY_BODY not in rendered(err) and "c3ludGhldGljLWxlYWY" not in rendered(err)
    assert marker.read_text() == "certificate\n"


# --------------------------------------------------------------------------- #
# SSH target: the real SSHExecutor, commands run locally with stubs
# --------------------------------------------------------------------------- #
class _Pending:
    """A command started by exec_command; it runs once its stdin is closed or its result is read."""

    def __init__(self, cmd, env, timeout):
        self.cmd, self.env, self.timeout = cmd, env, timeout
        self.buf, self.proc = b"", None

    def run(self):
        if self.proc is None:
            self.proc = subprocess.run(["/bin/sh", "-c", self.cmd], input=self.buf, env=self.env,
                                       capture_output=True, timeout=30)
        return self.proc


class _Stdin:
    def __init__(self, pending):
        self.pending, self.channel = pending, self

    def write(self, data):
        assert self.pending.proc is None, "stdin written after the command ran"
        self.pending.buf += data if isinstance(data, bytes) else data.encode()

    def shutdown_write(self):
        self.pending.run()


class _Out:
    def __init__(self, pending, which):
        self.pending, self.which, self.channel = pending, which, self

    def read(self):
        proc = self.pending.run()
        return proc.stdout if self.which == "out" else proc.stderr

    def settimeout(self, timeout):
        pass

    def recv_exit_status(self):
        return self.pending.run().returncode


@pytest.fixture
def local_ssh(monkeypatch, tmp_path):
    """An SSHExecutor whose fake SSH client runs every command locally. Returns (executor, record)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = {"commands": [], "first_write_modes": tmp_path / "first-write-modes",
              "manager": tmp_path / "manager-calls", "manager_exit": tmp_path / "manager-exit"}
    (bindir / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    # Records the mode of every file being written through it (the temp file the key goes to)
    # before any byte reaches it, then copies stdin as cat would.
    (bindir / "cat").write_text(
        "#!%s\nimport os, sys\n"
        "st = os.fstat(1)\n"
        "with open(%r, 'a') as log:\n"
        "    log.write('%%o\\n' %% (st.st_mode & 0o777))\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n" % (sys.executable, str(record["first_write_modes"])))
    (bindir / "pgedge-subscription-manager").write_text(
        "#!/bin/sh\nprintf '%%s\\n' \"$*\" >> %s\n"
        "code=0; [ -f %s ] && read code < %s\n"
        "[ \"$code\" = 0 ] || { echo 'ERROR: that certificate does not match the key on this system.' >&2; exit \"$code\"; }\n"
        % (record["manager"], record["manager_exit"], record["manager_exit"]))
    for f in bindir.iterdir():
        f.chmod(0o755)
    env = {"PATH": "%s:%s" % (bindir, os.environ["PATH"]), "HOME": str(tmp_path)}

    class LocalSSHClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            pass

        def get_transport(self):
            return self

        def is_active(self):
            return True

        def close(self):
            pass

        def open_sftp(self):
            raise AssertionError("SFTP must not be used for the client certificate")

        def exec_command(self, cmd, timeout=None):
            record["commands"].append(cmd)
            pending = _Pending(cmd, env, timeout)
            return _Stdin(pending), _Out(pending, "out"), _Out(pending, "err")

    monkeypatch.setattr(ssh_executor.paramiko, "SSHClient", LocalSSHClient)
    monkeypatch.setitem(pgedge_client_cert.CERT_DIR, "rhel", str(tmp_path / "pki" / "pgedge"))
    executor = ssh_executor.SSHExecutor("rocky9-amd64", "203.0.113.10", "ec2-user")
    return executor, record


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_ssh_delivery_writes_the_pair_through_sudo_never_sftp(local_ssh, tmp_path):
    executor, record = local_ssh
    pgedge_client_cert.install_on_target(executor, "rhel", CRT, KEY)
    cert_dir = tmp_path / "pki" / "pgedge"
    assert (cert_dir / "client.key").read_text() == KEY + "\n"
    assert (cert_dir / "client.crt").read_text() == CRT + "\n"
    assert _mode(cert_dir / "client.key") == 0o600 and _mode(cert_dir / "client.crt") == 0o644
    assert sorted(p.name for p in cert_dir.iterdir()) == ["client.crt", "client.key"]   # no temp left
    # Every file was created 0600 before its first byte, the key included.
    assert record["first_write_modes"].read_text().split() == ["600", "600"]
    # The key never appears in a command; every write ran under sudo.
    assert all(KEY_BODY not in c and "PRIVATE KEY" not in c for c in record["commands"])
    writes = [c for c in record["commands"] if "mktemp" in c]
    assert len(writes) == 2 and all(c.startswith("sudo ") for c in writes)
    assert record["manager"].read_text().strip() == \
        "install %s/client.crt %s/client.key --force" % (cert_dir, cert_dir)


def test_ssh_delivery_replaces_a_readable_old_key_with_a_new_private_file(local_ssh, tmp_path):
    executor, record = local_ssh
    cert_dir = tmp_path / "pki" / "pgedge"
    cert_dir.mkdir(parents=True)
    old = cert_dir / "client.key"
    old.write_text("old\n")
    old.chmod(0o644)
    old_inode = os.stat(old).st_ino
    pgedge_client_cert.install_on_target(executor, "rhel", CRT, KEY)
    assert os.stat(old).st_ino != old_inode            # never written in place
    assert _mode(old) == 0o600 and old.read_text() == KEY + "\n"
    assert record["first_write_modes"].read_text().split() == ["600", "600"]


def test_a_rejected_pair_over_ssh_is_marked_and_names_no_secret(local_ssh, marker):
    executor, record = local_ssh
    record["manager_exit"].write_text("1")
    with pytest.raises(Exception) as err:
        pgedge_client_cert.install_on_target(executor, "rhel", CRT, KEY)
    assert "does not match the key" in str(err.value)
    assert KEY_BODY not in rendered(err)
    assert marker.read_text() == "certificate\n"


@pytest.mark.parametrize("path", ["relative/client.key", "/etc/pki/../shadow", "/etc/pki/pgedge/",
                                  "/etc/pki/pgedge/client\n.key", "/etc/pki//pgedge/client.key"])
def test_the_root_writer_refuses_an_unsafe_path(local_ssh, path):
    executor, record = local_ssh
    with pytest.raises(ValueError):
        executor.write_root_file(path, b"data", 0o600)
    assert record["commands"] == []


@pytest.mark.parametrize("mode", [0o1600, -1, "0600", 0o4755])
def test_the_root_writer_refuses_an_unsafe_mode(local_ssh, tmp_path, mode):
    executor, record = local_ssh
    with pytest.raises(ValueError):
        executor.write_root_file(str(tmp_path / "f"), b"data", mode)
    assert record["commands"] == []


def test_a_failed_root_write_reports_stderr_not_data(local_ssh, tmp_path):
    executor, record = local_ssh
    with pytest.raises(IOError) as err:
        executor.write_root_file(str(tmp_path / "missing-dir" / "client.key"), KEY.encode(), 0o600)
    assert KEY_BODY not in rendered(err)


def test_the_root_writer_will_not_write_into_a_directory_named_as_the_file(local_ssh, tmp_path):
    executor, record = local_ssh
    target = tmp_path / "client.key"
    target.mkdir()
    with pytest.raises(IOError):
        executor.write_root_file(str(target), KEY.encode(), 0o600)
    assert list(target.iterdir()) == [] and list(tmp_path.glob(".pep-write.*")) == []


def test_a_stalled_root_write_times_out_instead_of_hanging(monkeypatch, tmp_path):
    class Stalled:
        def __init__(self):
            self.channel = self

        def write(self, data):
            pass

        def shutdown_write(self):
            pass

        def settimeout(self, timeout):
            self.timeout = timeout

        def read(self):
            raise TimeoutError("timed out")

        def recv_exit_status(self):
            raise AssertionError("waited for an exit status that never comes")

    class Client:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            pass

        def get_transport(self):
            return self

        def is_active(self):
            return True

        def exec_command(self, cmd, timeout=None):
            stream = Stalled()
            return stream, stream, stream

    monkeypatch.setattr(ssh_executor.paramiko, "SSHClient", Client)
    executor = ssh_executor.SSHExecutor("rocky9-amd64", "203.0.113.10", "ec2-user")
    with pytest.raises(RuntimeError) as err:
        executor.write_root_file(str(tmp_path / "client.key"), KEY.encode(), 0o600, timeout=3)
    assert "timed out after 3s" in str(err.value) and KEY_BODY not in rendered(err)


# --------------------------------------------------------------------------- #
# Transfer failures inside third-party code
# --------------------------------------------------------------------------- #
class _DroppedTransport:
    """Just enough of paramiko.Transport for Channel.send(): the link drops mid-send."""

    def _send_user_message(self, message):
        raise EOFError()


class _Done:
    """A command that succeeds with no output (everything except the key's root write)."""

    def __init__(self):
        self.channel = self

    def write(self, data):
        pass

    def shutdown_write(self):
        pass

    def read(self):
        return b""

    def settimeout(self, timeout):
        pass

    def recv_exit_status(self):
        return 0


@pytest.fixture
def dropped_ssh(monkeypatch):
    """An SSHExecutor whose root write of client.key goes through paramiko's real channel files
    over a link that drops on the first packet. Channel.send() has already put the key into a
    paramiko.Message, whose repr() shows the whole packet, when the transport fails. Every
    other command, including the certificate's write, succeeds."""

    class Client:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            pass

        def get_transport(self):
            return self

        def is_active(self):
            return True

        def exec_command(self, cmd, timeout=None):
            if "mktemp" not in cmd or "client.key" not in cmd:
                return _Done(), _Done(), _Done()
            chan = paramiko.Channel(1)
            chan.transport = _DroppedTransport()
            chan.active, chan.remote_chanid = True, 2
            chan.out_window_size, chan.out_max_packet_size = 1 << 20, 32768
            return ChannelStdinFile(chan, "wb"), ChannelFile(chan, "r"), ChannelStderrFile(chan, "r")

    monkeypatch.setattr(ssh_executor.paramiko, "SSHClient", Client)
    return ssh_executor.SSHExecutor("rocky9-amd64", "203.0.113.10", "ec2-user")


def test_a_dropped_ssh_link_mid_write_is_reported_without_the_key(dropped_ssh, tmp_path):
    with pytest.raises(Exception) as err:
        dropped_ssh.write_root_file(str(tmp_path / "client.key"), KEY.encode(), 0o600)
    assert KEY_BODY not in rendered(err)
    # What failed, where, and the error class are kept; nothing else is chained in.
    assert isinstance(err.value, OSError)
    assert str(err.value) == "Writing %s as root failed on 203.0.113.10: EOFError" % (tmp_path / "client.key")
    assert err.value.__cause__ is None and err.value.__context__ is None


def test_a_dropped_ssh_link_during_setup_is_a_certificate_failure_without_the_key(dropped_ssh, marker):
    with pytest.raises(Exception) as err:
        pgedge_client_cert.install_on_target(dropped_ssh, "rhel", CRT, KEY)
    assert KEY_BODY not in rendered(err)
    assert isinstance(err.value, pgedge_client_cert.CertificateInstallError)
    assert str(err.value) == ("Could not deliver the client certificate to /etc/pki/pgedge: Writing "
                              "/etc/pki/pgedge/client.key as root failed on 203.0.113.10: EOFError")
    assert marker.read_text() == "certificate\n"


def _send_body(body):
    """Stands in for a third-party transport frame holding the request body as plain bytes."""
    raise ConnectionError("connection reset while sending %r" % (body,))


class DroppingDockerTarget(DockerTarget):
    def put_archive(self, path, data):
        _send_body(bytes(data))


def test_a_failed_docker_transfer_is_a_certificate_failure_without_the_key(marker):
    with pytest.raises(Exception) as err:
        pgedge_client_cert.install_on_target(DroppingDockerTarget(), "rhel", CRT, KEY)
    assert KEY_BODY not in rendered(err)
    assert isinstance(err.value, pgedge_client_cert.CertificateInstallError)
    assert str(err.value) == \
        "Could not deliver the client certificate to /etc/pki/pgedge: ConnectionError"
    assert err.value.__cause__ is None and err.value.__context__ is None
    assert marker.read_text() == "certificate\n"


def _key_member(payload):
    with tarfile.open(fileobj=io.BytesIO(bytes(payload))) as tar:
        return tar.extractfile("client.key").read()


def test_a_failed_local_install_keeps_the_exit_status_without_the_key(monkeypatch):
    def feed(data, argv):     # a frame inside the subprocess call holding the key as plain bytes
        raise subprocess.CalledProcessError(1, argv, output=data)

    monkeypatch.setattr(pgedge_client_cert.subprocess, "run",
                        lambda argv, input=None, check=False: feed(_key_member(input), argv))
    with pytest.raises(Exception) as err:
        pgedge_client_cert.install_locally("rhel", CRT, KEY)
    assert KEY_BODY not in rendered(err)
    assert isinstance(err.value, pgedge_client_cert.CertificateInstallError)
    assert str(err.value) == \
        "Could not install the client certificate on this host: CalledProcessError (exit status 1)"
    assert err.value.__cause__ is None and err.value.__context__ is None


# --------------------------------------------------------------------------- #
# Repository setup after the certificate is installed
# --------------------------------------------------------------------------- #
class RepoContainer(DockerTarget):
    def __init__(self, family, refresh_exit=0):
        super().__init__()
        self.family, self.refresh_exit = family, refresh_exit

    def exec_run(self, cmd, user=None, **kwargs):
        script = cmd[-1]
        self.commands.append(script)
        if script == "command -v dnf":
            return (0 if self.family == "rpm" else 1), b""
        if script == "command -v apt-get":
            return (0 if self.family == "deb" else 1), b""
        if script.startswith("dnf -q makecache") or script == "apt-get update":
            return self.refresh_exit, b"Curl error (58): Problem with the local client certificate"
        return 0, b""


@pytest.fixture
def no_apt_lock_wait(monkeypatch):
    monkeypatch.setattr(configure_repository, "_wait_for_apt_lock", lambda container: None)


@pytest.fixture
def pair(monkeypatch):
    monkeypatch.setenv("PGEDGE_REPO_CLIENT_CRT", CRT)
    monkeypatch.setenv("PGEDGE_REPO_CLIENT_KEY", KEY)


def test_rhel_refreshes_only_the_enabled_pgedge_repositories(pair, marker):
    box = RepoContainer("rpm")
    ok, platform, message = configure_repository.configure_pgedge_repository(box, "staging")
    assert ok and platform == "rhel" and "configured successfully" in message
    refresh = [c for c in box.commands if "makecache" in c]
    assert refresh == ["dnf -q makecache --repo=pgedge --repo=pgedge-noarch"]
    assert box.commands.index(refresh[0]) > next(i for i, c in enumerate(box.commands)
                                                  if "pgedge-subscription-manager" in c)
    assert not marker.exists()


@pytest.mark.parametrize("family", ["rpm", "deb"])
def test_a_refresh_failure_after_enabling_is_marked_as_repository_setup(pair, marker, no_apt_lock_wait, family):
    with pytest.raises(Exception):
        configure_repository.configure_pgedge_repository(RepoContainer(family, refresh_exit=1), "release")
    assert marker.read_text() == "repository\n"


@pytest.mark.parametrize("family", ["rpm", "deb"])
def test_a_standalone_run_without_a_pair_is_unchanged_and_marks_nothing(tmp_path, no_apt_lock_wait, family):
    box = RepoContainer(family)
    ok, _, message = configure_repository.configure_pgedge_repository(box, "release")
    assert ok and "left disabled" in message
    assert not any("makecache" in c or "pgedge-subscription-manager" in c for c in box.commands)
    assert list(tmp_path.rglob("repo-auth-failure")) == []


@pytest.mark.parametrize("family", ["rpm", "deb"])
def test_an_integration_run_that_reaches_setup_without_a_pair_is_marked_missing(marker, no_apt_lock_wait, family):
    # pep-integration.yml starts the framework only with a complete pair, so this is the
    # backstop for a pair the workflow and the aspects read differently.
    ok, _, message = configure_repository.configure_pgedge_repository(RepoContainer(family), "release")
    assert ok and "left disabled" in message
    assert marker.read_text() == "missing\n"


def test_without_a_pair_a_debian_update_failure_is_not_blamed_on_the_refresh(marker, no_apt_lock_wait):
    with pytest.raises(Exception):
        configure_repository.configure_pgedge_repository(RepoContainer("deb", refresh_exit=1), "release")
    assert marker.read_text() == "missing\n"


@pytest.mark.parametrize("family", ["rpm", "deb"])
def test_a_successful_setup_clears_an_earlier_prerequisite_failure(pair, marker, no_apt_lock_wait, family):
    # The Debian prerequisite step installs the pair too; setup here repeats it and decides.
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("certificate\n")
    configure_repository.configure_pgedge_repository(RepoContainer(family), "release")
    assert not marker.exists()
