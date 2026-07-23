"""Canonical version normalization for PEP (L1). Extracted verbatim from
aspects/package_management.py so there is ONE owner. Stdlib only."""
from __future__ import annotations
import re

_BETA_KEYWORDS = ("vectorizer", "anonymizer", "rag", "mcp", "nla")


def normalize_version(version_string: str, package_name: str = "") -> str:
    version = version_string.lower().strip()
    package_lower = package_name.lower()
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
