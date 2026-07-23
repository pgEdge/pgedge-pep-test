"""Pure identity-comparison helpers for PEP build-integrated testing.

Compare an OBSERVED installed identity against an EXPECTED identity supplied by
the caller. These perform strict (L2) comparison: L2a = package-manager version
(RPM version-release / DEB version), L2b = binary self-reported version. The
coarser L1 component-version match reuses aspects.package_management.normalize_version
and is wired in Plan 2 alongside install/verify, so it is intentionally absent here.

Stdlib only -> unit-testable in isolation via `pytest utillities/test_pep_identity.py`.
"""
from __future__ import annotations

import re

# Architecture tokens that may trail an RPM NVRA (e.g. ...-1.el9.x86_64).
_RPM_ARCH_RE = re.compile(r"\.(x86_64|aarch64|arm64|noarch|i686|ppc64le|s390x)$")
_BINARY_VERSION_RE = re.compile(r"^\s*version\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def canonical_rpm(identity: str, package_name: str | None = None) -> str:
    """Reduce an RPM identity to a canonical VERSION-RELEASE[.dist] form.

    Accepts either a full NVRA (`pgedge-rag-server-1.0.0-1.el9.x86_64`) or a bare
    VERSION-RELEASE (`1.0.0-1.el9`), so it matches whatever query format the
    framework uses. Strips a leading `<name>-` prefix and a trailing `.<arch>`.
    """
    s = identity.strip()
    if package_name:
        prefix = package_name + "-"
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = _RPM_ARCH_RE.sub("", s)
    return s


def rpm_identity_matches(expected: str, observed: str,
                         package_name: str | None = None) -> bool:
    """True iff observed RPM identity equals expected (exact, L2a)."""
    return canonical_rpm(expected, package_name) == canonical_rpm(observed, package_name)


def deb_identity_matches(expected: str, observed: str) -> bool:
    """True iff observed DEB Version equals expected (exact, L2a).

    The expected DEB version already carries the `~pretag` form for pre-releases
    (e.g. `1.0.0~test1-1.noble`); comparison is strict equality after trimming.
    """
    return expected.strip() == observed.strip()


def parse_binary_version(output: str) -> str | None:
    """Extract the version token from `pgedge-rag-server -version` output.

    Looks for a `Version: <token>` line; returns None if not found.
    """
    m = _BINARY_VERSION_RE.search(output)
    return m.group(1) if m else None


def binary_version_matches(expected: str, output: str) -> bool:
    """True iff the binary's self-reported version equals expected (exact, L2b)."""
    parsed = parse_binary_version(output)
    return parsed is not None and parsed.strip() == expected.strip()


# Per-rung merge precedence: a failure anywhere dominates a success, which
# dominates 'never attempted'. Used to SAFELY combine evidence from multiple
# tests within one target (e.g. the version test contributes l2a/l1, the binary
# test contributes l2b) without a later 'proven' masking an earlier 'not_proven'.
_RANK = {"not_proven": 2, "proven": 1, "not_attempted": 0}
_UNRANK = {2: "not_proven", 1: "proven", 0: "not_attempted"}
_RUNGS = ("l2a", "l2b", "l1")


def merge_evidence(evidences: list) -> dict:
    """Combine a list of {l2a,l2b,l1} evidence dicts, per rung, safely.

    For each rung the merged value is the highest-ranked contribution:
    not_proven > proven > not_attempted. Missing keys are treated as
    'not_attempted'.
    """
    out = {}
    for rung in _RUNGS:
        best = 0
        for ev in evidences:
            best = max(best, _RANK.get((ev or {}).get(rung, "not_attempted"), 0))
        out[rung] = _UNRANK[best]
    return out
