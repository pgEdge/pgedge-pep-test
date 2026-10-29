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

The key never appears in a command: it travels as a tar on stdin, or through
docker's archive API. Both runners here echo the commands they are given, and
a process list is readable by any local user, so embedding it either way would
put the private key in CI logs.
"""

import io
import os
import subprocess
import tarfile
from pathlib import Path

# Where pgedge-subscription-manager keeps the pair. It derives the same paths
# itself; these are only what gets written before handing them over.
CERT_DIR = {"rhel": "/etc/pki/pgedge", "ubuntu": "/etc/pgedge"}


def read_client_certificate():
    """Return (crt_pem, key_pem) from the environment, or (None, None)."""

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
        return crt, key
    return None, None


def tar_payload(crt, key):
    """The pair as a tar, ready for stdin or docker's put_archive."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, body, mode in (
            ("client.crt", crt, 0o644),
            ("client.key", key, 0o600),
        ):
            data = (body.rstrip("\n") + "\n").encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.uid = info.gid = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


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

    The pair goes over put_archive, so it never appears in a command.
    """
    cert_dir = CERT_DIR[platform]
    exit_code, output = target.exec_run(
        ["/bin/sh", "-c", f"install -d -m 0755 {cert_dir}"], user="root")
    if exit_code != 0:
        raise Exception(f"Could not create {cert_dir}: {output.decode()}")

    target.put_archive(cert_dir, tar_payload(crt, key))

    exit_code, output = target.exec_run(
        ["/bin/sh", "-c", _enable_script(cert_dir, unpack=False)], user="root")
    if exit_code != 0:
        raise Exception(f"Failed to install client certificate: {output.decode()}")


def install_locally(platform, crt, key, sudo=False):
    """Install on this host. The tar goes on stdin, never into argv."""
    cert_dir = CERT_DIR[platform]
    argv = (["sudo"] if sudo else []) + [
        "sh", "-c", _enable_script(cert_dir, unpack=True)]
    subprocess.run(argv, input=tar_payload(crt, key), check=True)


NO_CERTIFICATE_MESSAGE = (
    "No pgEdge client certificate supplied: the gated repositories stay "
    "disabled, so no package from them will install. Set "
    "PGEDGE_REPO_CLIENT_CRT and PGEDGE_REPO_CLIENT_KEY (or the _FILE variants)."
)
