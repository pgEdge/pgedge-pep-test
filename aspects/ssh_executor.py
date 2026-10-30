"""
SSH Executor — Docker-compatible interface for remote AWS EC2 instances.

Provides exec_run(command, user) that matches the docker-py container interface
so all existing aspects modules (package_management, configure_repository, etc.)
work unchanged against live SSH targets.
"""
import io
import os
import shlex
import socket
import tarfile
import time

import paramiko


class _Redacted(bytes):
    """Bytes whose repr() hides them in a traceback through PEP's own frames."""
    __slots__ = ()

    def __repr__(self):
        return "<redacted>"


class RootWriteError(OSError):
    """write_root_file failed. The message names the operation, path, host and
    error class or exit status, never the data, and nothing is chained to it."""
    payload_free = True


class RootWriteTimeout(RuntimeError):
    """write_root_file timed out; see RootWriteError."""
    payload_free = True


class SSHExecutor:
    """
    Wraps a paramiko SSH connection and exposes the same surface as a
    Docker container object:

        exit_code, output = executor.exec_run("dnf install -y pkg", user="root")
        assert executor.status == "running"

    Also supports file-transfer helpers used by file_management.py:
        executor.put_file(local_path, remote_path)
        executor.put_archive(remote_dir, tarstream)   # mirrors container.put_archive
    """

    def __init__(self, name, host, username, key_path=None, max_retries=3):
        self.name = name
        self._host = host
        self._username = username
        # Resolution order: explicit arg → AWS_SSH_KEY_PATH env var → SSH agent
        self._key_path = key_path or os.environ.get("AWS_SSH_KEY_PATH") or None
        self._max_retries = max_retries
        self._client = None
        self._connect()

    # ------------------------------------------------------------------
    # Docker container interface
    # ------------------------------------------------------------------

    @property
    def status(self):
        """AWS instances are assumed to be running — mirrors container.status."""
        return "running"

    def exec_run(self, command, user="root", timeout=300, demux=False, **kwargs):
        """
        Execute a command on the remote host.

        Matches docker-py's container.exec_run() return value:
            (exit_code: int, output: bytes)
        or, with demux=True, stdout and stderr kept apart (None when empty):
            (exit_code: int, (stdout: bytes | None, stderr: bytes | None))

        The `user` parameter maps to sudo -u <user> or plain sudo for root,
        mirroring Docker's user= behaviour.
        """
        self._ensure_connected()

        # Build the full command with the correct user context
        if isinstance(command, list):
            # docker-py accepts list commands too (e.g. ["/bin/sh", "-c", "..."])
            import shlex
            raw = " ".join(shlex.quote(c) for c in command)
        else:
            raw = command

        if user == "root":
            cmd = f"sudo {raw}"
        else:
            cmd = f"sudo -u {user} {raw}"

        for attempt in range(self._max_retries):
            try:
                _, stdout, stderr = self._client.exec_command(
                    cmd, timeout=timeout
                )
                stdout.channel.settimeout(timeout)
                # Read stdout first — recv_exit_status() blocks until the channel
                # is closed, which the server only does after all output is flushed.
                # Calling recv_exit_status() before read() causes a deadlock when
                # the PTY/pipe buffer is full.
                output = stdout.read()
                err_output = stderr.read()
                exit_code = stdout.channel.recv_exit_status()
                if demux:
                    return exit_code, (output or None, err_output or None)
                if err_output:
                    output = output + err_output
                return exit_code, output
            except TimeoutError as exc:
                # socket.timeout (TimeoutError) is an OSError subclass — don't retry,
                # retrying would hang for another `timeout` seconds per attempt.
                raise RuntimeError(
                    f"Command timed out after {timeout}s on {self._host}: {cmd!r}"
                ) from exc
            except (paramiko.SSHException, OSError, EOFError) as exc:
                if attempt < self._max_retries - 1:
                    print(f"  [ssh_executor] command failed (attempt {attempt + 1}): {exc} — reconnecting")
                    self._connect()
                    time.sleep(1)
                else:
                    raise

    def put_file(self, local_path, remote_path):
        """Upload a single file to the remote host.

        Tries SFTP first; falls back to 'sudo tee' via SSH exec when SFTP is
        blocked by a permission error (common on AWS instances where the login
        user lacks SFTP write rights but has passwordless sudo).
        """
        self._ensure_connected()
        try:
            sftp = self._client.open_sftp()
            try:
                sftp.put(local_path, remote_path)
                return
            finally:
                sftp.close()
        except PermissionError:
            with open(local_path, "rb") as f:
                content = f.read()
            stdin, stdout, stderr = self._client.exec_command(
                f"sudo tee {remote_path} > /dev/null"
            )
            stdin.write(content)
            stdin.channel.shutdown_write()
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                err = stderr.read().decode().strip()
                raise IOError(f"Failed to write {remote_path} via sudo tee: {err}")

    def put_archive(self, remote_dir, tarstream):
        """
        Mirrors docker-py's container.put_archive(path, data).

        Unpacks the in-memory tar archive and uploads each member to
        `remote_dir` via SFTP — used by file_management.copy_file_to_container.
        """
        self._ensure_connected()
        sftp = self._client.open_sftp()
        try:
            if hasattr(tarstream, "read"):
                data = tarstream.read()
            else:
                data = tarstream

            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    remote_path = remote_dir.rstrip("/") + "/" + os.path.basename(member.name)
                    # Ensure remote directory exists
                    self.exec_run(f"mkdir -p {remote_dir}", user="root")
                    sftp.putfo(f, remote_path)
        finally:
            sftp.close()

    def write_root_file(self, path, data, mode, timeout=60):
        """Write ``data`` to the single file ``path`` as root, with ``mode``.

        For secrets such as the repository client key, which put_archive cannot
        deliver: SFTP writes as the login user, so it cannot write a root-owned
        directory, and where it could, the file would be the user's and readable
        until a later chmod. Here the data goes on stdin to a root-owned file
        that mktemp creates 0600 in the target's directory, then takes ``mode``
        and replaces ``path`` with one rename, so no other user can read it at
        any point. Neither the data nor anything derived from it is in the
        command.

        A failure raises RootWriteError or RootWriteTimeout, carrying the path,
        host, and error class or exit status. They are raised after the
        original error has been handled, so nothing is chained to them:
        paramiko's frames hold the data (its Message repr() shows the packet
        being sent), and its messages are not ours to vouch for. The data is
        also <redacted> in this frame's own arguments.

        ``path`` must be absolute and normalized, and must not be a directory,
        so the one file it names is the only one written.
        """
        data = _Redacted(data)
        if (not isinstance(path, str) or not path.startswith("/")
                or os.path.normpath(path) != path or path == "/"
                or any(c in path for c in "\0\n\r")):
            raise ValueError(f"write_root_file needs an absolute, normalized path: {path!r}")
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
            raise ValueError(f"write_root_file needs a permission mode 0..0o777: {mode!r}")

        # mv onto a directory (or a link to one) would move the file into it.
        script = (
            'set -e; umask 077; '
            'if [ -d "$1" ]; then echo "$1 is a directory" >&2; exit 1; fi; '
            't=$(mktemp "$(dirname "$1")/.pep-write.XXXXXX"); '
            'trap \'rm -f "$t"\' EXIT; '
            'cat > "$t"; chmod "$2" "$t"; mv -f "$t" "$1"; '
            'trap - EXIT'
        )
        cmd = "sudo sh -c {} sh {} {:04o}".format(shlex.quote(script), shlex.quote(path), mode)

        self._ensure_connected()
        failure = channel = None
        try:
            stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
            channel = stdin.channel
            stdin.write(data)
            stdin.channel.shutdown_write()
            # Read before waiting for the exit status, as exec_run does: the reads
            # honour the channel timeout, recv_exit_status() does not.
            stdout.channel.settimeout(timeout)
            stdout.read()
            err = stderr.read().decode(errors="replace").strip()
            exit_code = stdout.channel.recv_exit_status()
        except (TimeoutError, socket.timeout):
            failure = RootWriteTimeout(
                f"Writing {path} as root timed out after {timeout}s on {self._host}")
        except Exception as exc:
            failure = RootWriteError(
                f"Writing {path} as root failed on {self._host}: {type(exc).__name__}")
        if failure is not None:
            # Raised here, outside the handler, so no traceback is chained to it.
            try:
                if channel is not None:
                    channel.close()
            except Exception:
                pass
            raise failure
        if exit_code != 0:
            raise RootWriteError(
                f"Failed to write {path} as root on {self._host} (exit status {exit_code}): {err}")

    # ------------------------------------------------------------------
    # Connection management (internal)
    # ------------------------------------------------------------------

    def _connect(self):
        """Establish (or re-establish) the SSH connection."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Resolve key path relative to repo root if not absolute
        pkey = None
        use_agent = False
        if self._key_path:
            key_path = self._key_path
            if not os.path.isabs(key_path):
                repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                key_path = os.path.join(repo_root, key_path)
            pkey = paramiko.RSAKey.from_private_key_file(key_path)
        else:
            # No key file — delegate to the running SSH agent (ssh-add your key first)
            use_agent = True
            print(f"  [ssh_executor] no key_file set — using SSH agent for {self._host}")

        last_exc = None
        for attempt in range(self._max_retries):
            try:
                client.connect(
                    hostname=self._host,
                    username=self._username,
                    pkey=pkey,
                    timeout=30,
                    banner_timeout=30,
                    auth_timeout=30,
                    look_for_keys=use_agent,
                    allow_agent=use_agent,
                )
                self._client = client
                print(f"  [ssh_executor] connected to {self._host} as {self._username}")
                return
            except Exception as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    time.sleep(2)

        raise RuntimeError(
            f"Failed to connect to {self._host} after {self._max_retries} attempts: {last_exc}"
        )

    def _is_connected(self):
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def _ensure_connected(self):
        if not self._is_connected():
            print(f"  [ssh_executor] connection lost to {self._host}, reconnecting...")
            self._connect()

    def close(self):
        """Close the SSH connection."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def __repr__(self):
        return f"SSHExecutor(name={self.name!r}, host={self._host!r})"