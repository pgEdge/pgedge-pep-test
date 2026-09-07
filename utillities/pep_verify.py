"""Pure install-decision + identity-assertion logic for PEP integration.
No Docker, no env -- unit-testable in isolation. The container-bound install /
query helpers live in aspects/package_management.py; this module only decides
and compares. Stdlib only (imports pep_identity by path)."""
from __future__ import annotations
import re as _re
import importlib.util as _ilu
from pathlib import Path as _Path

_pid_spec = _ilu.spec_from_file_location(
    "pep_identity", str(_Path(__file__).with_name("pep_identity.py")))
_pid = _ilu.module_from_spec(_pid_spec)
_pid_spec.loader.exec_module(_pid)

# Allowlist for an EXACT version-release token (RPM VERSION-RELEASE, deb version).
# Defense-in-depth alongside argument-vector exec in install_pinned: this rejects any
# caller-supplied version carrying shell metacharacters BEFORE it is ever used to build
# a package spec, so a value like "1.0.0; rm -rf /" or "$(id)" cannot slip through.
_SAFE_VERSION = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+~-]*$")


class UnsafeVersionError(ValueError):
    """Raised when a pinned exact-version string is malformed or shell-unsafe."""


class InstallDecisionError(ValueError):
    """Raised when a request is internally inconsistent: an identity rung is marked
    attemptable_now but the family-specific expected identity needed to pin the
    install is absent/empty. normalize_request should never emit this; choose_install
    fails fast here rather than installing 'latest' and mutating the container before
    the contradiction would surface at identity verification."""


def assert_safe_version(exact_version):
    """Return exact_version if it matches the allowlist, else raise
    UnsafeVersionError. Pure + unit-testable (no Docker)."""
    if not isinstance(exact_version, str) or not _SAFE_VERSION.match(exact_version):
        raise UnsafeVersionError(f"unsafe/malformed exact version: {exact_version!r}")
    return exact_version


def choose_install(request):
    """Return ('pinned', <validated BARE install token>) for an explicit L2a request,
    else ('latest', None) for the degraded L1 path. A pinned token is VALIDATED here
    (assert_safe_version) so an unsafe expected_rpm/deb is rejected before install.

    RPM: expected_rpm may be a full NVR, but the install spec needs the bare
    VERSION-RELEASE -> reduce via _pid.canonical_rpm(...). DEB: expected_deb is
    already the bare dpkg Version -> used as-is. (package_name comes from the
    normalized request, so no signature change / no caller change.)

    If l2a is attemptable but the family-specific expected identity is absent/empty
    (or reduces to empty), the request is INTERNALLY INCONSISTENT: raise
    InstallDecisionError so we fail before mutating the container, rather than
    installing 'latest' and only detecting the contradiction later at identity
    verification. ('latest', None) is returned ONLY when l2a is genuinely false."""
    now = request["attemptable_now"]
    if now["l2a"]:
        if request["family"] == "rpm":
            raw = request.get("expected_rpm")
            token = _pid.canonical_rpm(raw, request.get("package_name")) if raw else None
        else:
            token = request.get("expected_deb")
        if not token:
            raise InstallDecisionError(
                f"attemptable_now.l2a is set but the {request['family']} expected "
                f"identity is absent/empty; cannot pin an exact install")
        return ("pinned", assert_safe_version(token))
    return ("latest", None)


def assert_identity(observed, request, package_name):
    """Return per-rung evidence {l2a,l2b,l1}. For an ATTEMPTABLE rung, a missing
    observation or a mismatch is 'not_proven' (a failure), never silently
    downgraded. Non-attemptable rungs stay 'not_attempted'."""
    ev = {"l2a": "not_attempted", "l2b": "not_attempted", "l1": "not_attempted"}
    now, fam = request["attemptable_now"], request["family"]
    if now["l2a"]:
        expected = request.get("expected_rpm") if fam == "rpm" else request.get("expected_deb")
        obs = observed.get("rpm") if fam == "rpm" else observed.get("deb")
        if not expected or obs is None:
            ev["l2a"] = "not_proven"
        elif fam == "rpm":
            ev["l2a"] = "proven" if _pid.rpm_identity_matches(expected, obs, package_name) else "not_proven"
        else:
            ev["l2a"] = "proven" if _pid.deb_identity_matches(expected, obs) else "not_proven"
    if now["l2b"]:
        expected, obs = request.get("expected_binary"), observed.get("binary")
        ev["l2b"] = ("not_proven" if (not expected or obs is None)
                     else ("proven" if _pid.binary_version_matches(expected, obs) else "not_proven"))
    if observed.get("component_version"):
        # component_version_matches(expected, observed) takes EXACTLY two args
        # (verified against pep_identity.py) -- do NOT pass package_name.
        ev["l1"] = ("proven" if _pid.component_version_matches(
            request["expected_version"], observed["component_version"]) else "not_proven")
    return ev
