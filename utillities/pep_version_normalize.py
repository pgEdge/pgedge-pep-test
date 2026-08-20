"""Canonical version normalization for PEP (L1): the single shared normalizer
used by both the standalone package check (aspects.package_management) and the
integration identity check (utillities.pep_identity). Stdlib only."""
from __future__ import annotations
import re

_BETA_KEYWORDS = ("vectorizer", "anonymizer", "rag", "mcp", "nla")


def normalize_version(version_string: str, package_name: str = "") -> str:
    version = version_string.lower().strip()
    package_lower = package_name.lower()
    # Debian encodes pre-releases with a tilde (1.0.0~beta2) so they sort before
    # the final release; RPM and the config env files use a hyphen (1.0.0-beta2).
    # Fold the tilde to a hyphen up front so a deb-installed pre-release compares
    # equal to the expected value from the env file.
    version = version.replace('~', '-')
    version = re.sub(r'\.(?:el|rhel|centos|rocky|alma|fc|oel)\w*$', '', version)  # RPM dist
    version = re.sub(r'-\d+\.[a-z]+$', '', version)   # deb -1.bullseye
    version = re.sub(r'-\d+$', '', version)           # trailing -N
    beta_suffix = ""
    if any(k in package_lower for k in _BETA_KEYWORDS) or 'beta' in version:
        version = re.sub(r'-beta', '.beta', version)
        m = re.search(r'\.?beta(\d*)', version)
        if m:
            beta_suffix = f".beta{m.group(1)}"
            version = version[:m.start()]
    parts = version.split('.')
    while len(parts) < 3:            # pad major.minor.patch  (16.11 -> 16.11.0)
        parts.append('0')
    return '.'.join(parts[:3]) + beta_suffix
