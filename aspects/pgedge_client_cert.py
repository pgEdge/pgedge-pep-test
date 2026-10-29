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
"""

import base64
import os
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


def install_script(crt, key, platform):
    """Shell that writes the pair and hands it to pgedge-subscription-manager.

    The PEM travels base64-encoded so no quoting or newline survives the trip
    through the shell, and the key file is created at 0600 before its contents
    are written rather than chmod'ed afterwards.
    """
    cert_dir = CERT_DIR[platform]
    b64 = lambda s: base64.b64encode((s.rstrip("\n") + "\n").encode()).decode()
    return (
        "set -e; "
        f"install -d -m 0755 {cert_dir}; "
        f"install -m 0600 /dev/null {cert_dir}/client.key; "
        f"printf %s '{b64(crt)}' | base64 -d > {cert_dir}/client.crt; "
        f"chmod 0644 {cert_dir}/client.crt; "
        f"printf %s '{b64(key)}' | base64 -d > {cert_dir}/client.key; "
        f"pgedge-subscription-manager install "
        f"{cert_dir}/client.crt {cert_dir}/client.key --force"
    )


NO_CERTIFICATE_MESSAGE = (
    "No pgEdge client certificate supplied: the repositories stay disabled and "
    "no pgEdge package will install. Set PGEDGE_REPO_CLIENT_CRT and "
    "PGEDGE_REPO_CLIENT_KEY (or the _FILE variants)."
)
