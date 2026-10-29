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


def install_script(platform):
    """Shell that reads the tar on stdin and hands the pair to the manager.

    Carries no secret, so it is safe for a runner that echoes commands.
    """
    cert_dir = CERT_DIR[platform]
    return (
        "set -e; "
        f"install -d -m 0755 {cert_dir}; "
        "umask 077; "
        f"tar -x -C {cert_dir} client.crt client.key; "
        f"chmod 0644 {cert_dir}/client.crt; "
        f"chmod 0600 {cert_dir}/client.key; "
        f"pgedge-subscription-manager install "
        f"{cert_dir}/client.crt {cert_dir}/client.key --force"
    )


NO_CERTIFICATE_MESSAGE = (
    "No pgEdge client certificate supplied: the repositories stay disabled and "
    "no pgEdge package will install. Set PGEDGE_REPO_CLIENT_CRT and "
    "PGEDGE_REPO_CLIENT_KEY (or the _FILE variants)."
)
