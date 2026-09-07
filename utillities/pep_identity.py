"""Pure identity-comparison helpers for PEP build-integrated testing.

Compare an OBSERVED installed identity against an EXPECTED identity supplied by
the caller. These perform strict (L2) comparison: L2a = package-manager version
(RPM version-release / DEB version), L2b = binary self-reported version. The
coarser L1 component-version match (`component_version_matches`) reuses the
shared `pep_version_normalize.normalize_version` (itself extracted from
aspects.package_management.normalize_version, which now delegates to it).

Stdlib only -> unit-testable in isolation via `pytest utillities/test_pep_identity.py`.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

# Architecture tokens that may trail an RPM NVRA (e.g. ...-1.el9.x86_64).
_RPM_ARCH_RE = re.compile(r"\.(x86_64|aarch64|arm64|noarch|i686|ppc64le|s390x)$")
_BINARY_VERSION_RE = re.compile(r"^\s*version\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)

_nz_spec = importlib.util.spec_from_file_location(
    "pep_version_normalize", str(Path(__file__).parent / "pep_version_normalize.py")
)
_nz = importlib.util.module_from_spec(_nz_spec)
sys.modules.setdefault("pep_version_normalize", _nz)
_nz_spec.loader.exec_module(_nz)


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


def component_version_matches(expected: str, observed: str) -> bool:
    """True iff the normalized expected version is a substring of the normalized
    observed version (coarse, L1).

    Argument order matches the sibling comparators in this module
    (`rpm_identity_matches(expected, observed, ...)`,
    `binary_version_matches(expected, output)`).

    The substring/containment check is DELIBERATE, not an approximation: it
    mirrors the framework's existing standalone check in
    aspects.package_management.verify_package_version, which does
    `normalized_expected not in normalized_installed`. This is intentionally
    coarse (L1/degraded) — e.g. a shorter expected can substring-match a
    longer observed (`"1.0"` matching within `"1.0.12"`) — and that coarseness
    is inherited from the shipping behavior this comparator preserves, not
    introduced here. Uses the shared `normalize_version` so this stays
    consistent with aspects.package_management's own normalization.
    """
    return _nz.normalize_version(expected) in _nz.normalize_version(observed)


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
    not_proven > proven > not_attempted. A MISSING rung key means 'not_attempted';
    a SUPPLIED value outside {proven, not_proven, not_attempted} is a contract
    error and raises ValueError (never silently downgraded to 'not_attempted').
    """
    out = {}
    for rung in _RUNGS:
        best = 0
        for ev in evidences:
            val = (ev or {}).get(rung, "not_attempted")
            if val not in _RANK:
                raise ValueError(
                    f"unknown evidence value {val!r} for rung {rung!r}; "
                    f"expected one of {sorted(_RANK)}")
            best = max(best, _RANK[val])
        out[rung] = _UNRANK[best]
    return out
