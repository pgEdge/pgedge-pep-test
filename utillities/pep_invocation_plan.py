#!/usr/bin/env python3
"""Derive the PEP test-invocation matrix from a resolved certification plan.

PURE and DETERMINISTIC: no GitHub API calls, no rpm/dpkg execution, no container
inspection, no clock, no environment. This module consumes an ALREADY-RESOLVED
``cert-plan/1`` document (the capture stage's output) plus centralized PEP
execution capability data, and emits a deterministic ``pep-invocation-plan/1``
document whose ``matrix.include`` list is GitHub-matrix-ready for ``pep-integration.yml``.

Scope of this slice (nothing else):
  * Consider the runnable package targets proven by capture: STRICT ``eligibility == 'eligible'``
    (certifiable, in either mode) plus, in preview mode ONLY, the separate additive
    ``preview_eligibility == 'eligible'`` (a simulated, built + identity-confirmed, truthfully-
    unpublished dry-run target). The coordinator's ``execution_mode`` is the single intent: it is
    stamped into the cert-plan at capture and RE-VALIDATED here (a preview plan cannot be consumed
    as full, and vice versa). A selective build's cert-plan simply contains fewer runnable targets;
    a broad/ALL build contains more. Selection is the plan's own content — there is NO build-mode flag.
  * Reconcile each eligible target with the platforms PEP can CURRENTLY execute — family,
    OS/platform, architecture and PostgreSQL applicability.
  * PG-coupled packages stay on their recorded build PG major; PG-decoupled packages expand
    across the centrally-configured supported PG majors.
  * Carry the INDEPENDENTLY-INSPECTED physical package identity and release channel into every
    invocation, and derive the family-specific expected package-manager string
    (``expected_rpm``/``expected_deb`` = ``<package.version>-<package.release>``) DIRECTLY from
    that inspected identity — never reconstructed from intended_version/buildnum — so the
    downstream test pins the exact package (L2a) rather than installing 'latest'.
  * Report unsupported/unmapped/uncertifiable eligible cells EXPLICITLY as coverage gaps — never
    silently drop an eligible package, and never return an apparently-complete empty matrix.
  * Account for EVERY planned cell (the cert-plan cells are the build intent): a cell with no
    selected target is one cell-scope gap, a selected target that is not runnable in this mode is
    one target-scope gap, and each rejected file of an allowed runtime package (in a cell that
    still yielded a target) is one member-scope gap. Packages excluded by policy (source/debug, or
    names the component does not ship as runtime) are intentional and never gaps.

Sources of truth (this module does NOT re-encode a fixed platform universe):
  * OS / architecture / currently-runnable containers: ``configuration/containers_list.json``,
    reused via ``container_resolver.load_catalog`` (only ``enabled`` entries are executable).
  * Supported PG majors + the packaging-os-token -> container-os bridge: the PEP-owned
    ``pep_exec_catalog.json`` (schema ``pep-exec-catalog/1``), which COMPOSES with the container
    catalog rather than duplicating arch/enabled state.
  * The logical-component -> accepted physical-package registry and the valid release channels:
    REUSED from ``pep_request`` (``COMPONENT_PACKAGES`` / ``VALID_CHANNELS``) through a single
    by-path import — the same authoritative contract ``normalize_request`` enforces downstream,
    never a second copy.

Fail-closed (JSON-compatible input NEVER raises; malformed shapes produce an unresolved plan
with ``errors`` and an EMPTY matrix): an unresolved/wrong-schema source plan, a blank/unsupported
release channel/component/version, a malformed exec catalog, an ambiguous ``(os_token, family)``
mapping, a non-workflow-safe invocation identity, or a duplicate invocation identity.

Stdlib only. Unit-testable via ``pytest utillities/test_pep_invocation_plan.py``.
"""
from __future__ import annotations

import hashlib
import importlib.util as _ilu
import json
import re
import sys as _sys
from pathlib import Path as _Path

# Reuse the AUTHORITATIVE PEP request contract (the logical-component -> accepted physical
# package registry and the valid release channels) through the smallest clean boundary. We do NOT
# duplicate the component/package registry here — this is the SAME table normalize_request
# validates against. Reuse the already-imported module when present (so every caller shares one
# pep_request instance); otherwise load it by path (like pep_verify loads pep_identity) and
# register it under its own name so a later `import pep_request` resolves to the same object.
if "pep_request" in _sys.modules:
    _pr = _sys.modules["pep_request"]
else:
    _pr_spec = _ilu.spec_from_file_location("pep_request", str(_Path(__file__).with_name("pep_request.py")))
    _pr = _ilu.module_from_spec(_pr_spec)
    _sys.modules["pep_request"] = _pr
    _pr_spec.loader.exec_module(_pr)
COMPONENT_PACKAGES = _pr.COMPONENT_PACKAGES      # {logical_component: (accepted physical package, ...)}
VALID_CHANNELS = _pr.VALID_CHANNELS              # ("release", "staging", "daily")

# Same reuse boundary for pep_verify: its assert_safe_version IS the PEP contract for a pinnable
# exact version-release token (the identical check choose_install applies at install time), so the
# planner validates the token it emits with that contract rather than a divergent local grammar.
if "pep_verify" in _sys.modules:
    _pv = _sys.modules["pep_verify"]
else:
    _pv_spec = _ilu.spec_from_file_location("pep_verify", str(_Path(__file__).with_name("pep_verify.py")))
    _pv = _ilu.module_from_spec(_pv_spec)
    _sys.modules["pep_verify"] = _pv
    _pv_spec.loader.exec_module(_pv)

SCHEMA = "pep-invocation-plan/1"
EXEC_CATALOG_SCHEMA = "pep-exec-catalog/1"
CERT_PLAN_SCHEMA = "cert-plan/1"

FAMILIES = ("rpm", "deb")
ARCHES = ("amd64", "arm64")
EXECUTION_MODES = ("preview", "full")   # coordinator execution intent, bound into the plan

# The reusable workflow (pep-integration.yml preflight) accepts an invocation_id only if it
# matches this charset/length; the planner is the producer, so it enforces the SAME rule.
_INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Coverage-gap reasons (a target that produces zero invocations records exactly one).
GAP_UNSUPPORTED_FAMILY = "unsupported_family"
GAP_UNSUPPORTED_ARCH = "unsupported_arch"
GAP_UNSUPPORTED_OS = "unsupported_os"                # os token not mapped in the exec catalog
GAP_NO_ENABLED_PLATFORM = "no_enabled_platform"     # mapped, but no enabled container for this arch
GAP_PG_NOT_SUPPORTED = "pg_not_supported"           # coupled build PG major not centrally supported
GAP_INVALID_PG_COUPLING = "invalid_pg_coupling"     # pg_coupled but no usable build_pg_major
GAP_UNSUPPORTED_COMPONENT = "unsupported_component"  # logical component/physical package not in the PEP registry
GAP_IDENTITY_UNCONFIRMED = "identity_unconfirmed"   # package_identity_state != 'confirmed'
GAP_PACKAGE_HAS_EPOCH = "package_has_epoch"         # epoch-bearing package (not pinnable as version-release)
GAP_MALFORMED_TARGET = "malformed_target"           # a required field is missing/blank/wrong-typed

# Gap SCOPE: which planned unit went uncertified. Every planned cell yields >=1 invocation or >=1
# gap, and every selected target is covered or carries exactly one target-scope gap.
GAP_SCOPE_CELL = "cell"        # a planned cell that yielded no selected target
GAP_SCOPE_TARGET = "target"    # a selected target that is not runnable, or runnable but not executable
GAP_SCOPE_MEMBER = "member"    # a rejected allowed runtime file in a cell that still has a target

# Cell-scope reasons. A build that never became available is ``build_<build_state>`` (the cert-plan's
# own vocabulary), except a SUCCESSFUL build job without a verified package artifact (absent,
# rejected or ambiguous receipt): the build ran but its evidence was lost.
GAP_PACKAGE_EVIDENCE_MISSING = "package_evidence_missing"
GAP_TARGET_AMBIGUOUS = "target_ambiguous"           # >1 package with the same name + arch in one cell
GAP_NO_RUNTIME_TARGET = "no_runtime_target"         # built, but no allowed runtime package with valid evidence
# A selected target not runnable in the requested mode; the cert-plan's reason is the gap detail.
GAP_TARGET_INELIGIBLE = "target_ineligible"
# An allowed runtime package the cert-plan rejected for its evidence; its exclusion reasons are the detail.
GAP_MEMBER_REJECTED = "member_rejected"

# cert-plan/1 member exclusions that are deliberate policy, not evidence defects: never gaps.
_POLICY_EXCLUSIONS = frozenset({"non_runtime", "package_not_allowed"})


def _nonblank_str(x):
    return isinstance(x, str) and x.strip() != ""


def _blank_to_empty(v):
    """Canonicalize an OPTIONAL workflow input the planner treats as ABSENT — None, empty, or
    whitespace-only — to "" (which the workflow's `[ -n ]` guard drops), so a blank optional can
    never reach normalize_request as a value it rejects with "provided but is empty". A nonblank
    value is emitted verbatim; normalize_request applies its own strip, so a padded nonblank behaves
    identically downstream (this adds no separate normalization policy)."""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return ""
    return v


def _is_canonical_pg_major(x):
    """A PG major given in canonical form: a bare decimal with no padding or whitespace
    (``"16"`` ok; ``" 16"``, ``"016"``, ``"08"`` rejected)."""
    return isinstance(x, str) and x.isdigit() and str(int(x)) == x


def _is_canonical_token(s):
    """A catalog token (os_token / catalog_os) in canonical form: nonblank and free of any
    leading/trailing/internal whitespace (so ``" el-9"`` or ``"el 9"`` are rejected, not stripped)."""
    return _nonblank_str(s) and s == s.strip() and not any(ch.isspace() for ch in s)


# --------------------------------------------------------------------------- #
# exec catalog (supported PG majors + os-token -> container-os bridge)
# --------------------------------------------------------------------------- #
def validate_exec_catalog(doc):
    """Return ``(supported_pg_majors, os_map, errors)``.

    ``supported_pg_majors`` is an ascending list of canonical numeric-string majors; ``os_map``
    maps ``(os_token, family) -> tuple(catalog_os...)``. ``errors`` is a deterministic list (empty
    == valid). Read-only; never raises for JSON-compatible input. Padded/whitespace-bearing PG
    majors, os tokens or catalog OS names are REJECTED (never silently canonicalized), and
    duplicates are detected on those canonical values."""
    errors = []
    if not isinstance(doc, dict):
        return [], {}, ["exec catalog is not an object"]
    if doc.get("schema") != EXEC_CATALOG_SCHEMA:
        errors.append("exec catalog schema must be %r" % EXEC_CATALOG_SCHEMA)

    raw_pgs = doc.get("supported_pg_majors")
    pgs = []
    if not isinstance(raw_pgs, list) or not raw_pgs:
        errors.append("exec catalog supported_pg_majors must be a nonempty list")
    else:
        for p in raw_pgs:
            if not _is_canonical_pg_major(p):
                errors.append("exec catalog supported_pg_majors entry %r must be a canonical "
                              "numeric string (no padding/whitespace)" % (p,))
        if not [e for e in errors if "supported_pg_majors entry" in e]:
            if len({p for p in raw_pgs}) != len(raw_pgs):
                errors.append("exec catalog supported_pg_majors must be unique")
            pgs = sorted({p for p in raw_pgs}, key=lambda s: (int(s), s))

    os_map = {}
    raw_platforms = doc.get("platforms")
    if not isinstance(raw_platforms, list):
        errors.append("exec catalog platforms must be a list")
    else:
        for i, entry in enumerate(raw_platforms):
            if not isinstance(entry, dict):
                errors.append("exec catalog platforms[%d] is not an object" % i)
                continue
            tok, fam, cos = entry.get("os_token"), entry.get("family"), entry.get("catalog_os")
            if not _is_canonical_token(tok):
                errors.append("exec catalog platforms[%d].os_token must be a canonical nonblank "
                              "string (no whitespace)" % i)
            if fam not in FAMILIES:
                errors.append("exec catalog platforms[%d].family must be one of %s" % (i, ",".join(FAMILIES)))
            if not (isinstance(cos, list) and cos and all(_is_canonical_token(x) for x in cos)):
                errors.append("exec catalog platforms[%d].catalog_os must be a nonempty list of "
                              "canonical nonblank strings" % i)
                continue
            if len({x for x in cos}) != len(cos):
                errors.append("exec catalog platforms[%d].catalog_os must be unique" % i)
            if _is_canonical_token(tok) and fam in FAMILIES:
                key = (tok, fam)
                if key in os_map:
                    # An ambiguous mapping: the same (os_token, family) declared twice.
                    errors.append("exec catalog has an ambiguous mapping for os_token %r family %r" % (tok, fam))
                else:
                    os_map[key] = tuple(cos)
    if errors:
        return [], {}, errors
    return pgs, os_map, errors


def enabled_platforms_from_catalog(catalog):
    """Normalize a ``container_resolver.Catalog`` into ``{(family, arch, catalog_os): alias}`` for
    ENABLED entries only (the platforms PEP can currently execute). ``catalog_os`` is the OS portion
    of the alias (``alias == '<catalog_os>-<arch>'``). Disabled or malformed entries are skipped."""
    out = {}
    for e in getattr(catalog, "entries", ()):  # tuple of frozen CatalogEntry
        fam, arch, alias, enabled = getattr(e, "family", None), getattr(e, "arch", None), \
            getattr(e, "alias", None), getattr(e, "enabled", None)
        if enabled is not True or fam not in FAMILIES or arch not in ARCHES:
            continue
        if not isinstance(alias, str):
            continue
        suffix = "-" + arch
        if not alias.endswith(suffix):
            continue
        catalog_os = alias[: -len(suffix)]
        if catalog_os:
            out[(fam, arch, catalog_os)] = alias
    return out


# --------------------------------------------------------------------------- #
# per-target reconciliation
# --------------------------------------------------------------------------- #
def _validate_eligible_target(target, release, component_packages):
    """Validate the fields THIS module actually consumes from an eligible target, returning
    ``(gap_reason_or_None, detail_or_None)``. A malformed shape yields a coverage gap (never a
    raised exception, never a partially-null matrix entry). Family/arch/OS/PG capability are
    checked by the caller with their own dedicated gap reasons; this focuses on component/package
    identity agreement, the inspected physical package object, and PG-coupling type safety."""
    lc_release = release.get("logical_component")
    lc_target = target.get("logical_component")
    if not _nonblank_str(lc_target) or lc_target != lc_release:
        return GAP_MALFORMED_TARGET, "logical_component blank or disagrees with release"
    pkg_name = target.get("physical_package")
    if not _nonblank_str(pkg_name):
        return GAP_MALFORMED_TARGET, "physical_package is blank"

    # Reuse the authoritative PEP component<->package registry: an unknown logical component or a
    # physical package not accepted for it is a CAPABILITY gap (see module notes), never an
    # invocation normalize_request would predictably reject later.
    if component_packages is not None:
        allowed = component_packages.get(lc_release)
        if allowed is None:
            return GAP_UNSUPPORTED_COMPONENT, "unknown logical component %r" % (lc_release,)
        if pkg_name not in allowed:
            return GAP_UNSUPPORTED_COMPONENT, ("physical package %r not accepted for component %r"
                                               % (pkg_name, lc_release))

    if not isinstance(target.get("pg_coupled"), bool):
        return GAP_MALFORMED_TARGET, "pg_coupled must be strictly boolean"

    pkg = target.get("package")
    if not isinstance(pkg, dict):
        return GAP_MALFORMED_TARGET, "package must be an object"
    if not _nonblank_str(pkg.get("name")) or pkg.get("name") != pkg_name:
        return GAP_MALFORMED_TARGET, "package.name blank or disagrees with physical_package"
    # version + release: nonblank, string, unpadded. The exact "<version>-<release>" token is then
    # validated with PEP's OWN safe-version contract (pep_verify.assert_safe_version) rather than a
    # divergent per-field grammar, so a valid Debian version bearing hyphens (e.g.
    # "2.0.0-beta-1.trixie") stays pinnable while a shell-unsafe token is still rejected.
    version, rel = pkg.get("version"), pkg.get("release")
    for label, val in (("version", version), ("release", rel)):
        if not (isinstance(val, str) and val.strip() != "" and val == val.strip()):
            return GAP_MALFORMED_TARGET, "package.%s must be a nonblank, unpadded string" % label
    try:
        _pv.assert_safe_version("%s-%s" % (version, rel))
    except _pv.UnsafeVersionError:
        return GAP_MALFORMED_TARGET, "package version-release is not a PEP-safe pin token"
    sha = pkg.get("sha256")
    if not (isinstance(sha, str) and _SHA256_RE.match(sha.lower())):
        return GAP_MALFORMED_TARGET, "package.sha256 is not a 64-hex digest"
    # The inspector canonicalizes a missing/zero epoch to None; require EXACTLY None so a stray
    # "", "0" or 0 (a non-canonical or epoch-bearing value) never becomes a bare version-release pin.
    if pkg.get("epoch") is not None:
        return GAP_PACKAGE_HAS_EPOCH, "epoch must be canonicalized to None, got %r" % (pkg.get("epoch"),)

    if target.get("package_identity_state") != "confirmed":
        return GAP_IDENTITY_UNCONFIRMED, str(target.get("package_identity_state"))

    expected = target.get("expected")
    if not isinstance(expected, dict):
        return GAP_MALFORMED_TARGET, "expected must be an object"
    ebv = expected.get("expected_binary_version")
    if not (ebv is None or isinstance(ebv, str)):
        return GAP_MALFORMED_TARGET, "expected_binary_version must be None or a string"
    return None, None


def _applicable_pg_majors(target, supported_pg_majors):
    """(pgs, gap_reason_or_None). PG-coupled -> only the recorded build major (canonical + centrally
    supported); PG-decoupled -> all centrally supported majors. ``pg_coupled`` type-safety is
    already guaranteed by ``_validate_eligible_target`` (strictly boolean)."""
    if target.get("pg_coupled") is True:
        major = target.get("build_pg_major")
        if not _is_canonical_pg_major(major):
            return [], GAP_INVALID_PG_COUPLING
        if major not in supported_pg_majors:
            return [], GAP_PG_NOT_SUPPORTED
        return [major], None
    # decoupled -> all supported majors
    return list(supported_pg_majors), None


def _enabled_aliases(family, arch, os_token, os_map, enabled_platforms):
    """(aliases, gap_reason_or_None). Map the packaging os token to container OS identities, then
    keep only the currently-enabled aliases for this family+arch."""
    catalog_os = os_map.get((os_token, family))
    if catalog_os is None:
        return [], GAP_UNSUPPORTED_OS
    aliases = []
    for cos in sorted(catalog_os):
        alias = enabled_platforms.get((family, arch, cos))
        if alias is not None:
            aliases.append(alias)
    if not aliases:
        return [], GAP_NO_ENABLED_PLATFORM
    return sorted(set(aliases)), None


def _invocation_id(component, package_name, alias, pg_major, version, release, sha256):
    """A deterministic, workflow-safe (``^[A-Za-z0-9._-]{1,64}$``) invocation identity.

    A readable, sanitized prefix (component/alias/pg) is joined to a truncated (64-bit) hex digest of
    the FULL distinguishing identity — component, physical package, container alias, PG major, and the
    inspected version/release/sha256. The digest distinguishes multiple physical packages that share
    component/family/arch/PG/container; truncating it to 64 bits makes an ACCIDENTAL collision
    negligible (not impossible), and the duplicate-identity check in build_invocation_plan remains the
    fail-closed safeguard. The readable prefix is cosmetic and never carries uniqueness."""
    canon = "\x1f".join((
        component or "", package_name or "", alias or "", str(pg_major or ""),
        version or "", release or "", (sha256 or "").lower()))
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]      # 64 bits
    prefix = _ID_UNSAFE_RE.sub("-", "%s-%s-pg%s" % (component or "", alias or "", pg_major or ""))
    prefix = prefix.strip("-._")[:47].strip("-._") or "inv"             # budget: 47 + '-' + 16 == 64
    return "%s-%s" % (prefix, digest)


def _invocation(target, release, provenance, alias, pg_major):
    """Build one invocation dict carrying every cert-derived per-target input pep-integration.yml
    needs. The expected package-manager identity is derived from the INSPECTED package object
    (``<version>-<release>``) and emitted ONLY for the target's own family, so the coordinator can
    never pass a contradictory opposite-family expected string. ``pep_implementation_ref`` and the
    run-level scenario/mode/execution_mode are NOT cert-derived; the coordinator injects them."""
    pkg = target.get("package") if isinstance(target.get("package"), dict) else {}
    component = release.get("logical_component")
    package_name = target.get("physical_package")
    family = target.get("family")
    version, rel, sha = pkg.get("version"), pkg.get("release"), pkg.get("sha256")
    expected_vr = "%s-%s" % (version, rel)
    return {
        "invocation_id": _invocation_id(component, package_name, alias, pg_major, version, rel, sha),
        # --- required pep-integration.yml inputs (cert-derived) ---
        "component": component,
        "package_name": package_name,
        "channel": release.get("channel"),
        "expected_version": release.get("intended_version"),
        "container_alias": alias,
        "pg_major": pg_major,
        "family": family,
        "arch": target.get("execution_arch"),
        # --- optional pep-integration.yml inputs (cert-derived) ---
        # Blank optionals (None/empty/whitespace-only) are canonicalized to "" so the workflow drops
        # them instead of passing a value normalize_request would reject as "provided but is empty".
        "expected_buildnum": _blank_to_empty(release.get("intended_buildnum")),
        "effective_tag": _blank_to_empty(release.get("effective_tag")),
        # EXACT package-manager identity from the inspected package, emitted ONLY for this family;
        # the opposite family's key is empty and the workflow drops it (no contradictory value).
        "expected_rpm": expected_vr if family == "rpm" else "",
        "expected_deb": expected_vr if family == "deb" else "",
        "expected_binary": _blank_to_empty((target.get("expected") or {}).get("expected_binary_version")),
        # --- verified physical package identity carried for provenance (req 5) ---
        "package": {
            "name": pkg.get("name"),
            "version": version,
            "release": rel,
            "sha256": sha,
            "native_arch": target.get("native_package_arch"),
        },
        # --- traceability back to the proving evidence ---
        "source_cell_id": target.get("_cell_id"),
        "source_target_id": target.get("target_id"),
        "producer_repo": (provenance or {}).get("repository"),
    }


def _gap(target, reason, detail=None):
    return {
        "scope": GAP_SCOPE_TARGET,
        "cell_id": target.get("_cell_id"),
        "target_id": target.get("target_id"),
        "family": target.get("family"),
        "os": target.get("os"),
        "arch": target.get("execution_arch"),
        "physical_package": target.get("physical_package"),
        "reason": reason,
        "detail": detail,
    }


def _cell_gap(cell, scope, reason, detail=None, physical_package=None):
    return {
        "scope": scope,
        "cell_id": cell.get("cell_id"),
        "target_id": None,
        "family": cell.get("family"),
        "os": cell.get("os"),
        "arch": cell.get("normalized_arch"),
        "physical_package": physical_package,
        "reason": reason,
        "detail": detail,
    }


def _members(cell):
    members = cell.get("members")
    return [m for m in members if isinstance(m, dict)] if isinstance(members, list) else []


def _exclusions(member):
    rs = member.get("exclusion_reasons")
    return sorted({r for r in rs if isinstance(r, str)}) if isinstance(rs, list) else []


def _no_target_gap(cell):
    """The single cell-scope gap for a planned cell that yielded no selected target."""
    state = cell.get("build_state")
    if state != "available":
        ev = cell.get("build_evidence") if isinstance(cell.get("build_evidence"), dict) else {}
        if (state == "incomplete" and ev.get("latest_status") == "completed"
                and ev.get("latest_conclusion") == "success" and ev.get("artifact_present") is not True):
            return _cell_gap(cell, GAP_SCOPE_CELL, GAP_PACKAGE_EVIDENCE_MISSING,
                             "build job succeeded but no verified package artifact")
        detail = next((v for v in (cell.get("ambiguity_reason"), ev.get("invalid_reason"),
                                   ev.get("latest_conclusion"), ev.get("latest_status"))
                       if _nonblank_str(v)), None)
        return _cell_gap(cell, GAP_SCOPE_CELL, "build_%s" % (state if _nonblank_str(state) else "unknown"),
                         detail)
    members = _members(cell)
    if cell.get("target_selection_state") == "target_ambiguous":
        names = sorted({str(m.get("package_name")) for m in members if not _exclusions(m)})
        return _cell_gap(cell, GAP_SCOPE_CELL, GAP_TARGET_AMBIGUOUS, ", ".join(names) or None)
    reasons = sorted({r for m in members for r in _exclusions(m)})
    return _cell_gap(cell, GAP_SCOPE_CELL, GAP_NO_RUNTIME_TARGET,
                     ", ".join(reasons) or "no inspected packages")


def _rejected_member_gaps(cell):
    """Member-scope gaps for a cell that DID yield a target: one per distinct inspected FILE of an
    allowed runtime package that the cert-plan rejected for its evidence. Policy exclusions are
    intentional and never gaps. A valid target certifies only its own file, so a rejected file that
    shares its package name (wrong arch, bad checksum, ...) is still uncertified and still a gap;
    the detail carries the file's native arch and checksum prefix so such rows stay distinguishable."""
    rejected = {}
    for m in _members(cell):
        rs = _exclusions(m)
        if not rs or _POLICY_EXCLUSIONS.intersection(rs):
            continue
        ident = tuple(m.get(k) if _nonblank_str(m.get(k)) else None
                      for k in ("package_name", "native_arch", "version", "release", "sha256"))
        rejected.setdefault(ident, set()).update(rs)
    gaps = []
    for ident in sorted(rejected, key=lambda i: tuple(v or "" for v in i)):
        name, native, _, _, sha = ident
        detail = ",".join(sorted(rejected[ident]))
        if native:
            detail += "; native_arch=%s" % native
        if sha:
            detail += "; sha256=%s" % sha[:12]
        gaps.append(_cell_gap(cell, GAP_SCOPE_MEMBER, GAP_MEMBER_REJECTED, detail, physical_package=name))
    return gaps


def _cells_structure_errors(cert_plan):
    """A resolved cert-plan always carries identified cell objects with target-object lists; any
    other shape would let a planned cell vanish unaccounted, so it fails the plan closed."""
    cells = cert_plan.get("cells")
    if not isinstance(cells, list):
        return ["source cert-plan cells must be a list"]
    errors = []
    for i, cell in enumerate(cells):
        if not isinstance(cell, dict) or not _nonblank_str(cell.get("cell_id")):
            errors.append("source cert-plan cells[%d] must be an object with a nonblank cell_id" % i)
        elif not (isinstance(cell.get("targets"), list)
                  and all(isinstance(t, dict) for t in cell["targets"])):
            errors.append("source cert-plan cells[%d].targets must be a list of objects" % i)
    return errors


# --------------------------------------------------------------------------- #
# top-level pure planner
# --------------------------------------------------------------------------- #
def _account_cells(cert_plan, execution_mode):
    """Split every planned cell into runnable targets and coverage gaps (never mutates input).

    Returns ``(runnable, gaps, n_selected)``. Each runnable target is tagged with its owning
    ``_cell_id``. A target runs when its STRICT ``eligibility == 'eligible'`` (certifiable; either
    mode). In ``preview`` mode ONLY, a target also runs when its separate additive
    ``preview_eligibility == 'eligible'`` (a simulated, built + identity-confirmed,
    truthfully-unpublished dry-run target); a non-runnable preview target reports that preview
    reason. ``full`` mode never runs preview-only targets. The caller (build_invocation_plan) has
    already verified the stamped mode and the cell structure."""
    preview = (execution_mode == "preview")
    reason_key = "preview_eligibility_reason" if preview else "eligibility_reason"
    runnable, gaps, n_selected = [], [], 0
    for cell in cert_plan["cells"]:
        targets = cell["targets"]
        if not targets:
            gaps.append(_no_target_gap(cell))
            continue
        n_selected += len(targets)
        for t in targets:
            tagged = dict(t)
            tagged["_cell_id"] = cell["cell_id"]
            if (t.get("eligibility") == "eligible") or (preview and t.get("preview_eligibility") == "eligible"):
                runnable.append(tagged)
            else:
                detail = t.get(reason_key)
                gaps.append(_gap(tagged, GAP_TARGET_INELIGIBLE, detail if _nonblank_str(detail) else None))
        gaps.extend(_rejected_member_gaps(cell))
    return runnable, gaps, n_selected


def build_invocation_plan(cert_plan, exec_catalog, enabled_platforms, *,
                          component_packages=None, valid_channels=None, execution_mode="full"):
    """Reduce a resolved ``cert-plan/1`` + centralized execution data into a deterministic
    ``pep-invocation-plan/1`` dict. Never raises for JSON-compatible input; fail-closed shapes
    yield ``plan_resolved == false`` with ``errors`` and an EMPTY matrix.

    ``enabled_platforms`` is ``{(family, arch, catalog_os): alias}`` for currently-enabled containers
    (see ``enabled_platforms_from_catalog``). ``component_packages`` / ``valid_channels`` default to
    the authoritative ``pep_request`` contract; tests may inject a synthetic registry."""
    if component_packages is None:
        component_packages = COMPONENT_PACKAGES
    if valid_channels is None:
        valid_channels = VALID_CHANNELS

    errors = []
    if execution_mode not in EXECUTION_MODES:
        return _unresolved(["invalid execution_mode %r (want one of %s)"
                            % (execution_mode, list(EXECUTION_MODES))], execution_mode=None)
    if not isinstance(cert_plan, dict):
        return _unresolved(["source cert-plan is not an object"], execution_mode=execution_mode)
    if cert_plan.get("schema") != CERT_PLAN_SCHEMA:
        errors.append("source schema must be %r" % CERT_PLAN_SCHEMA)
    if cert_plan.get("plan_resolved") is not True:
        errors.append("source cert-plan is not resolved")
    # Bind the execution intent: a plan captured for one mode must not be consumed as another
    # (a preview plan admits simulated/unpublished dry-run targets and must never run as full).
    # An absent stamp is treated as legacy "full". Mismatch => fail closed, no matrix.
    plan_mode = cert_plan.get("execution_mode") if isinstance(cert_plan, dict) else None
    if plan_mode is None:
        plan_mode = "full"
    if plan_mode != execution_mode:
        errors.append("execution_mode mismatch: cert-plan=%r requested=%r" % (plan_mode, execution_mode))

    supported_pgs, os_map, cat_errors = validate_exec_catalog(exec_catalog)
    errors.extend(cat_errors)
    if not isinstance(enabled_platforms, dict):
        errors.append("enabled_platforms must be a mapping")
        enabled_platforms = {}

    release = cert_plan.get("release_intent") if isinstance(cert_plan.get("release_intent"), dict) else {}
    provenance = cert_plan.get("provenance") if isinstance(cert_plan.get("provenance"), dict) else {}

    # Release-level integrity (whole-plan attributes every invocation would carry). A blank/
    # unsupported component, version or channel makes EVERY invocation predictably fail
    # normalize_request downstream, so it is a whole-plan fail-closed, not a per-target gap.
    if not _nonblank_str(release.get("logical_component")):
        errors.append("release logical_component is blank")
    if not _nonblank_str(release.get("intended_version")):
        errors.append("release intended_version is blank")
    if valid_channels is not None and release.get("channel") not in valid_channels:
        errors.append("release channel %r is not a supported PEP channel" % (release.get("channel"),))
    # Optional release identity fields, when present, must satisfy the SAME pep_request contract
    # normalize_request enforces downstream (a divergent policy here would let a doomed invocation
    # through): effective_tag must carry the 'v' prefix, intended_buildnum must match the build-number
    # grammar. Absent (None/blank) is fine — the workflow drops empty optionals.
    et = release.get("effective_tag")
    if et is not None and str(et).strip() != "" and not str(et).strip().startswith("v"):
        errors.append("release effective_tag %r must start with 'v'" % (et,))
    bn = release.get("intended_buildnum")
    if bn is not None and str(bn).strip() != "" and not _pr._BUILDNUM_RE.match(str(bn).strip()):
        errors.append("release intended_buildnum %r is malformed" % (bn,))
    errors.extend(_cells_structure_errors(cert_plan))

    # Fail closed BEFORE emitting any invocation: a bad source/catalog/release must never yield a matrix.
    if errors:
        return _unresolved(errors, release=release, provenance=provenance,
                           supported_pgs=supported_pgs, execution_mode=execution_mode)

    invocations = []
    runnable, gaps, selected_n = _account_cells(cert_plan, execution_mode)
    covered_n = 0
    for target in runnable:
        family = target.get("family")
        arch = target.get("execution_arch")
        os_token = target.get("os")
        if family not in FAMILIES:
            gaps.append(_gap(target, GAP_UNSUPPORTED_FAMILY, detail=str(family)))
            continue
        if arch not in ARCHES:
            gaps.append(_gap(target, GAP_UNSUPPORTED_ARCH, detail=str(arch)))
            continue
        # Identity / component / package-object integrity (fail-closed per-target, never raising).
        val_gap, val_detail = _validate_eligible_target(target, release, component_packages)
        if val_gap is not None:
            gaps.append(_gap(target, val_gap, detail=val_detail))
            continue
        pgs, pg_gap = _applicable_pg_majors(target, supported_pgs)
        if pg_gap is not None:
            gaps.append(_gap(target, pg_gap, detail=str(target.get("build_pg_major"))))
            continue
        aliases, plat_gap = _enabled_aliases(family, arch, os_token, os_map, enabled_platforms)
        if plat_gap is not None:
            gaps.append(_gap(target, plat_gap, detail=str(os_token)))
            continue
        covered_n += 1
        for alias in aliases:
            for pg in pgs:
                invocations.append(_invocation(target, release, provenance, alias, pg))

    # A non-workflow-safe id (should be impossible by construction) or a duplicate invocation
    # identity is a source/catalog inconsistency -> fail closed (no matrix), never silently emitted
    # or deduplicated. The duplicate check is the collision detector for the digest identity.
    seen, bad, dups = {}, [], set()
    for inv in invocations:
        iid = inv["invocation_id"]
        if not _INVOCATION_ID_RE.match(iid):
            bad.append(iid)
        if iid in seen:
            dups.add(iid)
        seen[iid] = inv
    if bad:
        return _unresolved(["invalid invocation identity: %r" % s for s in sorted(set(bad))],
                           release=release, provenance=provenance, supported_pgs=supported_pgs,
                           execution_mode=execution_mode)
    if dups:
        return _unresolved(["duplicate invocation identity: %s" % s for s in sorted(dups)],
                           release=release, provenance=provenance, supported_pgs=supported_pgs,
                           execution_mode=execution_mode)

    invocations.sort(key=lambda x: x["invocation_id"])
    gaps.sort(key=lambda g: (str(g.get("cell_id") or ""), str(g.get("target_id") or ""),
                             str(g.get("physical_package") or ""), g.get("reason") or "",
                             str(g.get("detail") or "")))
    # Every count is tallied from the plan itself, never by subtracting gaps. They reconcile:
    # selected_targets == covered_targets + gaps_by_scope.target, and gaps_by_scope.cell is the
    # number of planned cells that yielded no selected target.
    by_scope = {s: sum(1 for g in gaps if g["scope"] == s)
                for s in (GAP_SCOPE_CELL, GAP_SCOPE_TARGET, GAP_SCOPE_MEMBER)}

    return {
        "schema": SCHEMA,
        "plan_resolved": True,
        "execution_mode": execution_mode,              # bound intent (matches the cert-plan's stamp)
        "errors": [],
        "provenance": {k: provenance.get(k) for k in ("repository", "run_id", "run_attempt", "sha", "ref")},
        "release": {
            "logical_component": release.get("logical_component"),
            "channel": release.get("channel"),
            "intended_version": release.get("intended_version"),
            "intended_buildnum": release.get("intended_buildnum"),
            "effective_tag": release.get("effective_tag"),
        },
        "supported_pg_majors": list(supported_pgs),
        "matrix": {"include": invocations},
        "coverage_gaps": gaps,
        "counts": {
            "planned_cells": len(cert_plan["cells"]),
            "selected_targets": selected_n,
            "eligible_targets": len(runnable),     # runnable in the requested mode
            "covered_targets": covered_n,          # runnable targets that produced >=1 invocation
            "coverage_gaps": len(gaps),
            "gaps_by_scope": by_scope,
            "invocations": len(invocations),
        },
    }


def _unresolved(errors, release=None, provenance=None, supported_pgs=None, execution_mode=None):
    release = release or {}
    provenance = provenance or {}
    return {
        "schema": SCHEMA,
        "plan_resolved": False,
        "execution_mode": execution_mode,
        "errors": list(errors),
        "provenance": {k: provenance.get(k) for k in ("repository", "run_id", "run_attempt", "sha", "ref")},
        "release": {
            "logical_component": release.get("logical_component"),
            "channel": release.get("channel"),
            "intended_version": release.get("intended_version"),
            "intended_buildnum": release.get("intended_buildnum"),
            "effective_tag": release.get("effective_tag"),
        },
        "supported_pg_majors": list(supported_pgs or []),
        "matrix": {"include": []},          # NEVER a matrix from a fail-closed plan
        "coverage_gaps": [],
        "counts": {"planned_cells": 0, "selected_targets": 0, "eligible_targets": 0,
                   "covered_targets": 0, "coverage_gaps": 0,
                   "gaps_by_scope": {GAP_SCOPE_CELL: 0, GAP_SCOPE_TARGET: 0, GAP_SCOPE_MEMBER: 0},
                   "invocations": 0},
    }


# --------------------------------------------------------------------------- #
# impure edge: load the authoritative sources, then delegate to the pure core
# --------------------------------------------------------------------------- #
def load_exec_catalog(path):
    """Read + JSON-parse the exec catalog file. Returns the raw dict (validated by the pure core).
    A missing/malformed file returns a shape the core rejects (fail closed)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"schema": None}   # the core records a schema error and fails closed


def load_enabled_platforms(container_catalog_path):
    """Load ``configuration/containers_list.json`` via the authoritative ``container_resolver`` and
    normalize its ENABLED entries. Returns ``(enabled_platforms, error_or_None)`` — a malformed
    container catalog fails closed rather than raising."""
    import container_resolver as CR
    try:
        catalog = CR.load_catalog(container_catalog_path)
    except CR.ResolverError as e:
        return {}, "container catalog unusable: %s" % (e,)
    return enabled_platforms_from_catalog(catalog), None


def plan_from_sources(cert_plan, exec_catalog_path, container_catalog_path, execution_mode="full"):
    """Convenience: load the exec catalog + container catalog from disk, then build the plan."""
    exec_catalog = load_exec_catalog(exec_catalog_path)
    enabled_platforms, cat_err = load_enabled_platforms(container_catalog_path)
    if cat_err is not None:
        return _unresolved([cat_err], execution_mode=execution_mode)
    return build_invocation_plan(cert_plan, exec_catalog, enabled_platforms, execution_mode=execution_mode)


def to_json(plan):
    """Canonical, deterministic serialization of a pep-invocation-plan/1 dict."""
    return json.dumps(plan, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Derive the PEP test-invocation matrix from a resolved cert-plan/1")
    ap.add_argument("--cert-plan", required=True, help="resolved cert-plan/1 JSON file")
    ap.add_argument("--exec-catalog", required=True, help="pep-exec-catalog/1 JSON file")
    ap.add_argument("--containers", required=True, help="configuration/containers_list.json")
    ap.add_argument("--execution-mode", default="full", choices=list(EXECUTION_MODES),
                    help="coordinator execution intent; must match the cert-plan's stamp (default: full)")
    ap.add_argument("--out", default=None, help="write the invocation plan here (default: stdout)")
    args = ap.parse_args(argv)
    try:
        with open(args.cert_plan, "r", encoding="utf-8") as fh:
            cert_plan = json.load(fh)
    except (OSError, ValueError) as e:
        plan = _unresolved(["source cert-plan unreadable: %s" % e], execution_mode=args.execution_mode)
    else:
        plan = plan_from_sources(cert_plan, args.exec_catalog, args.containers,
                                 execution_mode=args.execution_mode)
    text = to_json(plan)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        import sys
        sys.stdout.write(text)
    return 0 if plan.get("plan_resolved") else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
