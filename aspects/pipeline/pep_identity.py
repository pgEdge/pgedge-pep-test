"""Pure identity-comparison helpers for PEP build-integrated testing.

Compare an OBSERVED installed identity against an EXPECTED identity supplied by
the caller. These perform strict (L2) comparison: L2a = package-manager version
(RPM version-release / DEB version), L2b = binary self-reported version. The
coarser L1 component-version match (`component_version_matches`) reads the
component version out of the package-manager identity by the packaging format
release pins are built with. (The standalone package check keeps its own
normalizer, aspects.package_management.normalize_version.)

Stdlib only -> unit-testable in isolation via `pytest aspects/pipeline/tests/test_pep_identity.py`.
"""
from __future__ import annotations

import re

# Architecture tokens that may trail an RPM NVRA (e.g. ...-1.el9.x86_64).
_RPM_ARCH_RE = re.compile(r"\.(x86_64|aarch64|arm64|noarch|i686|ppc64le|s390x)$")
_BINARY_VERSION_RE = re.compile(r"^\s*version\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)

# What may follow the component version in a package-manager identity (L1). Release pins are built
# as (pipeline/pep_cert_plan.py::_expected_native, after the builders' pkg/common.sh):
#   RPM VERSION-RELEASE  <version>-<buildnum>.<dist>          1.1-1.el9, 1.1-test1_1.el9
#   DEB Version          <version>-<n>.<codename>             1.1-1.jammy
#                        <version>~<pretag>-<n>.<codename>     1.1~test1-1.jammy
# where a pre-release build number is <pretag>_<n> and both use the build-number characters
# (pep_request._BUILDNUM_RE). Neither an RPM release nor a DEB revision contains '-'.
_PRE_RELEASE = r"~[A-Za-z0-9.]+"
_RELEASE = r"-[A-Za-z0-9._]+"


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
    """True iff the component version in the observed package-manager identity
    (RPM VERSION-RELEASE or DEB Version) is exactly `expected` (coarse, L1).

    Argument order matches the sibling comparators in this module
    (`rpm_identity_matches(expected, observed, ...)`,
    `binary_version_matches(expected, output)`).

    `expected` is the component's whole version in its own format (`1.1` for a
    major.minor product, `1.2.4` for major.minor.patch); no part is added or
    dropped, so `1.1` never matches `1.1.0`, `11.1` or `1.10`. After it the
    observed identity may carry only a `~<pretag>` pre-release marker and one
    `-<release>` (`_PRE_RELEASE`, `_RELEASE`). The release itself (build number,
    pre-release in an RPM release, dist or codename) is L2a's to prove exactly,
    so this confirms the component version, not the build.
    """
    expected, observed = expected.strip(), observed.strip()
    if not expected:
        return False
    pattern = "%s(?:%s)?(?:%s)?" % (re.escape(expected), _PRE_RELEASE, _RELEASE)
    return re.fullmatch(pattern, observed) is not None


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
