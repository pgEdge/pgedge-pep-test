#!/usr/bin/env python3
"""Client certificate for the gated pgEdge repositories.

The repositories authenticate clients with mutual TLS, and pgedge-release
ships its repository definitions disabled: pgedge-subscription-manager
enables them once a certificate is installed and verified. Supply the pair
through the environment, either as PEM contents or as paths:

    PGEDGE_REPO_CLIENT_CRT       PEM of the certificate, leaf then intermediate
    PGEDGE_REPO_CLIENT_KEY       PEM of the matching private key
    PGEDGE_REPO_CLIENT_CRT_FILE  path to the certificate instead
    PGEDGE_REPO_CLIENT_KEY_FILE  path to the key instead

With neither supplied the repositories stay disabled and no pgEdge package
installs, so the caller reports that rather than failing obscurely later.

The key never appears in a command: it travels as a tar on stdin, through
docker's archive API, or on the stdin of a root write over SSH. Both runners
here echo the commands they are given, and a process list is readable by any
local user, so embedding it either way would put the private key in CI logs.

A failed setup is recorded for pep-integration.yml (see record_setup_failure),
which reports it as an infrastructure failure rather than a product one.
"""

import io
import os
import subprocess
import tarfile
from pathlib import Path

# Where pgedge-subscription-manager keeps the pair. It derives the same paths
# itself; these are only what gets written before handing them over.
CERT_DIR = {"rhel": "/etc/pki/pgedge", "ubuntu": "/etc/pgedge"}

# What failed, as recorded for pep-integration.yml: no pair was supplied, the
# pair could not be installed, or the repositories it enables could not be
# refreshed.
MISSING_CERTIFICATE = "missing"
CERTIFICATE_FAILURE = "certificate"
REPOSITORY_FAILURE = "repository"


class _SecretText(str):
    """A PEM that keeps its value out of repr().

    pytest renders a failing frame's arguments with repr() into the JUnit and
    HTML reports, which are uploaded as artifacts and are not masked, so a key
    passed as a plain str would land in them. str() and formatting still give
    the value.
    """
    __slots__ = ()

    def __repr__(self):
        return "<redacted>"


class _SecretBytes(bytes):
    """Bytes that carry the pair; repr() is redacted, as for _SecretText."""
    __slots__ = ()

    def __repr__(self):
        return "<redacted>"


def record_setup_failure(category):
    """Record a failed certificate or repository setup for pep-integration.yml.

    It sets PEP_REPO_AUTH_FAILURE_FILE and reports the run as an infrastructure
    failure; anywhere else (a standalone run) this does nothing. Only the
    one-word category is written, never a message that could carry the pair.
    """
    path = os.environ.get("PEP_REPO_AUTH_FAILURE_FILE", "").strip()
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(category + "\n")


def clear_setup_failure():
    """Forget an earlier recorded failure once setup has succeeded after it.

    The Debian prerequisite step installs the pair too, and repository setup
    always repeats it, so a passing setup is what counts.
    """
    path = os.environ.get("PEP_REPO_AUTH_FAILURE_FILE", "").strip()
    if path:
        Path(path).unlink(missing_ok=True)


def read_client_certificate():
    """Return (crt_pem, key_pem) from the environment, or (None, None).

    The values keep themselves out of repr(); see _SecretText.
    """

    def _value(name):
        inline = os.environ.get(f"PGEDGE_REPO_CLIENT_{name}", "").strip()
        if inline:
            return inline
        path = os.environ.get(f"PGEDGE_REPO_CLIENT_{name}_FILE", "").strip()
        if path:
            return Path(path).read_text().strip()
        return ""

    crt, key = _value("CRT"), _value("KEY")
    if crt and key:
        return _SecretText(crt), _SecretText(key)
    return None, None


def _files(crt, key):
    """(name, bytes, mode) for each file of the pair."""
    return [
        (name, _SecretBytes((body.rstrip("\n") + "\n").encode()), mode)
        for name, body, mode in (("client.crt", crt, 0o644), ("client.key", key, 0o600))
    ]


def tar_payload(crt, key):
    """The pair as a tar, ready for stdin or docker's put_archive."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data, mode in _files(crt, key):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.uid = info.gid = 0
            tar.addfile(info, io.BytesIO(data))
    return _SecretBytes(buf.getvalue())


def _enable_script(cert_dir, unpack):
    """Fix the modes and hand the pair to pgedge-subscription-manager.

    Carries no secret, so it is safe for a runner that echoes commands.
    """
    unpack_step = f"tar -x -C {cert_dir} client.crt client.key; " if unpack else ""
    return (
        "set -e; "
        f"install -d -m 0755 {cert_dir}; "
        "umask 077; "
        f"{unpack_step}"
        f"chmod 0644 {cert_dir}/client.crt; "
        f"chmod 0600 {cert_dir}/client.key; "
        f"pgedge-subscription-manager install "
        f"{cert_dir}/client.crt {cert_dir}/client.key --force"
    )


def install_on_target(target, platform, crt, key):
    """Install on a docker container or SSHExecutor.

    A container gets the pair over put_archive, which docker unpacks as root.
    An SSH login user is usually not root and cannot write the directory over
    SFTP, so SSHExecutor writes each file as root from stdin, the key created
    0600 before its first byte. Either way the pair never appears in a command.
    """
    # Rebound so a traceback through this frame shows <redacted>, whatever the
    # caller passed in.
    crt, key = _SecretText(crt), _SecretText(key)
    cert_dir = CERT_DIR[platform]
    try:
        exit_code, output = target.exec_run(
            ["/bin/sh", "-c", f"install -d -m 0755 {cert_dir}"], user="root")
        if exit_code != 0:
            raise CertificateInstallError(f"Could not create {cert_dir}: {output.decode()}")

        failure = None
        try:
            if hasattr(target, "write_root_file"):
                for name, data, mode in _files(crt, key):
                    target.write_root_file(f"{cert_dir}/{name}", data, mode)
            else:
                target.put_archive(cert_dir, tar_payload(crt, key))
        except Exception as exc:
            failure = _describe(exc)
        if failure is not None:
            raise CertificateInstallError(
                f"Could not deliver the client certificate to {cert_dir}: {failure}")

        exit_code, output = target.exec_run(
            ["/bin/sh", "-c", _enable_script(cert_dir, unpack=False)], user="root")
        if exit_code != 0:
            raise CertificateInstallError(
                f"Failed to install client certificate: {output.decode()}")
    except Exception:
        record_setup_failure(CERTIFICATE_FAILURE)
        raise


def install_locally(platform, crt, key, sudo=False):
    """Install on this host. The tar goes on stdin, never into argv."""
    crt, key = _SecretText(crt), _SecretText(key)   # see install_on_target
    cert_dir = CERT_DIR[platform]
    argv = (["sudo"] if sudo else []) + [
        "sh", "-c", _enable_script(cert_dir, unpack=True)]
    failure = None
    try:
        subprocess.run(argv, input=tar_payload(crt, key), check=True)
    except Exception as exc:
        failure = _describe(exc)
    if failure is not None:
        raise CertificateInstallError(
            f"Could not install the client certificate on this host: {failure}")


class CertificateInstallError(Exception):
    """The pair could not be delivered or installed.

    Where delivery goes through third-party code (docker-py and its HTTP stack,
    paramiko, subprocess), this is raised after that error has been handled, so
    neither its message nor its traceback is chained into what pytest writes to
    the JUnit and HTML reports: those frames can hold the pair as plain bytes
    (paramiko's Message repr() shows the packet). What is kept is in _describe.
    """


def _describe(exc):
    """What a delivery failure can safely say: the error class, the exit status
    of a failed command, or the whole message of an error PEP raised without
    the pair (SSHExecutor's RootWriteError and RootWriteTimeout)."""
    if getattr(exc, "payload_free", False):
        return str(exc)
    status = getattr(exc, "returncode", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return f"{type(exc).__name__} (exit status {status})"
    return type(exc).__name__


NO_CERTIFICATE_MESSAGE = (
    "No pgEdge client certificate supplied: the gated repositories stay "
    "disabled, so no package from them will install. Set "
    "PGEDGE_REPO_CLIENT_CRT and PGEDGE_REPO_CLIENT_KEY (or the _FILE variants)."
)
