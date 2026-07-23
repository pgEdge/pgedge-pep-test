"""Normalize + validate a PEP build-integrated test request (scalar inputs).

Implements the request rules from the design spec (sections 3 and 5):
`expected_version` is REQUIRED and build-authoritative (never falls back to
config); the scalar target fields (family/arch/pg_major/container_alias) are required.
`component` must be known and `package_name` must be the package it maps to
(COMPONENT_PACKAGES); an expected package-manager string must match the target
family (`expected_rpm`->rpm, `expected_deb`->deb) or the request is rejected.

Identity-rung classification is deliberately CONSERVATIVE about decision gate G6
(the expected-identity transform authority):
  * EXPLICIT expected strings (expected_rpm/expected_deb/expected_binary) are
    directly assertable  -> attemptable_now.
  * expected_buildnum / effective_tag ALONE are NOT attemptable yet: turning them
    into an expected string requires a transform whose owner G6 has not decided.
    They are recorded as derivation_pending and do NOT raise identity_target.

Stdlib only -> unit-testable via `pytest utillities/test_pep_request.py`.
"""
from __future__ import annotations

import re

VALID_CHANNELS = ("release", "staging", "daily")
VALID_FAMILIES = ("rpm", "deb")
VALID_ARCHES = ("amd64", "arm64")
VALID_MODES = ("observe", "gate")
VALID_SCENARIOS = ("certification", "upgrade")
# Known component -> canonical package mapping. The POC only wires 'rag'; extend
# this table (not the call sites) as components are added. Validating the pair
# rejects an unknown component or a package_name that does not belong to it.
COMPONENT_PACKAGES = {"rag": "pgedge-rag-server"}
_BUILDNUM_RE = re.compile(r"^[A-Za-z0-9._]+$")   # e.g. 1, test1_1, beta3_1
_PG_MAJOR_RE = re.compile(r"^\d+$")


class RequestError(ValueError):
    """Raised when a request is invalid/incomplete. `.reason` is human-readable."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _require(raw: dict, key: str):
    val = raw.get(key)
    if val is None or (isinstance(val, str) and not val.strip()):
        raise RequestError(f"{key} is required")
    return val.strip() if isinstance(val, str) else val


def _optional(raw: dict, key: str):
    if key not in raw or raw[key] is None:
        return None
    v = raw[key]
    v = v.strip() if isinstance(v, str) else v
    if v == "":
        raise RequestError(f"{key} was provided but is empty")
    return v


def _enum(value, allowed, key):
    if value not in allowed:
        raise RequestError(f"{key} {value!r} invalid; must be one of {', '.join(allowed)}")
    return value


def normalize_request(raw: dict) -> dict:
    """Validate + normalize a raw request dict. Raises RequestError if invalid.

    Returns a normalized dict with `identity_target` ('L1' | 'L2'), `attemptable_now`
    {l2a,l2b,l1} (assertable given the inputs, no derivation), and
    `derivation_pending` {l2a,l2b} (would become attemptable once G6 assigns a
    derivation owner).
    """
    if not isinstance(raw, dict):
        raise RequestError(f"request must be a dict, got {type(raw).__name__}")

    # component must be known, and package_name must be the package that
    # component maps to (rejects unknown components and mismatched pairs).
    component = _enum(_require(raw, "component"), tuple(COMPONENT_PACKAGES), "component")
    package_name = _require(raw, "package_name")
    if package_name != COMPONENT_PACKAGES[component]:
        raise RequestError(
            f"package_name {package_name!r} does not match component {component!r} "
            f"(expected {COMPONENT_PACKAGES[component]!r})")
    channel = _enum(_require(raw, "channel"), VALID_CHANNELS, "channel")

    # REQUIRED and build-authoritative — never fall back to config.
    expected_version = _require(raw, "expected_version")

    # Scalar target contract (all required).
    family = _enum(_require(raw, "family"), VALID_FAMILIES, "family")
    arch = _enum(_require(raw, "arch"), VALID_ARCHES, "arch")
    pg_major = _require(raw, "pg_major")
    if not _PG_MAJOR_RE.match(str(pg_major)):
        raise RequestError(f"pg_major {pg_major!r} must be numeric")
    # Target is selected by a catalog ALIAS (the OS is embedded in the container
    # name; a bare distro/el-version cannot select a container). Catalog membership
    # + family/arch agreement is validated in Plan 2 Task 5 (needs the catalog file).
    container_alias = _require(raw, "container_alias")

    # Optional identity inputs.
    expected_buildnum = _optional(raw, "expected_buildnum")
    if expected_buildnum is not None and not _BUILDNUM_RE.match(str(expected_buildnum)):
        raise RequestError(f"expected_buildnum {expected_buildnum!r} is malformed")
    effective_tag = _optional(raw, "effective_tag")
    if effective_tag is not None and not str(effective_tag).startswith("v"):
        raise RequestError(f"effective_tag {effective_tag!r} must start with 'v'")
    expected_rpm = _optional(raw, "expected_rpm")
    expected_deb = _optional(raw, "expected_deb")
    expected_binary = _optional(raw, "expected_binary")
    # Expected package-manager identity must match the target family: an rpm
    # target cannot carry a DEB expected string, and vice versa. (expected_binary
    # is family-agnostic.) A contradictory combination is an invalid request.
    if expected_rpm and family != "rpm":
        raise RequestError("expected_rpm is only valid when family is 'rpm'")
    if expected_deb and family != "deb":
        raise RequestError("expected_deb is only valid when family is 'deb'")

    scenario = raw.get("scenario") or "certification"
    if isinstance(scenario, str):
        scenario = scenario.strip()
    scenario = _enum(scenario, VALID_SCENARIOS, "scenario")
    mode = raw.get("mode") or "observe"
    if isinstance(mode, str):
        mode = mode.strip()
    mode = _enum(mode, VALID_MODES, "mode")

    # G6-conservative rung classification.
    l2a_explicit = bool(expected_rpm or expected_deb)
    l2b_explicit = bool(expected_binary)
    attemptable_now = {"l2a": l2a_explicit, "l2b": l2b_explicit, "l1": True}
    derivation_pending = {
        "l2a": bool(expected_buildnum) and not l2a_explicit,
        "l2b": bool(effective_tag) and not l2b_explicit,
    }
    identity_target = "L2" if (l2a_explicit or l2b_explicit) else "L1"

    return {
        "component": component,
        "package_name": package_name,
        "channel": channel,
        "expected_version": expected_version,
        "family": family,
        "arch": arch,
        "pg_major": str(pg_major),
        "container_alias": container_alias,
        "expected_buildnum": expected_buildnum,
        "effective_tag": effective_tag,
        "expected_rpm": expected_rpm,
        "expected_deb": expected_deb,
        "expected_binary": expected_binary,
        "scenario": scenario,
        "mode": mode,
        "identity_target": identity_target,
        "attemptable_now": attemptable_now,
        "derivation_pending": derivation_pending,
    }
