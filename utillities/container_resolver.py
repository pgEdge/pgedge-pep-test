"""Container target resolver for the PEP Regression framework.

Owns:
- Loading and validating configuration/containers_list.json
- Resolving runtime overrides (--containers, PEP_CONTAINERS) against the catalog
- Per-(family, arch) target filtering
- The --list-containers display

See docs/superpowers/specs/2026-06-04-v2.2-container-target-override-design.md
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


class ResolverError(Exception):
    """Catalog or override validation failure. Callers exit non-zero."""


_BLOCK_TO_FAMILY = {"rhel": "rpm", "deb": "deb"}
_CANONICAL_ARCH_SUFFIXES = {"-arm": "arm64", "-amd": "amd64"}
_ALIAS_ARCH_SUFFIXES = {"-arm64": "arm64", "-amd64": "amd64"}


@dataclass(frozen=True)
class CatalogEntry:
    name: str           # canonical, e.g. 'auto-rocky9-arm'
    alias: str          # user-facing, e.g. 'rocky9-arm64'
    description: str
    enabled: bool
    family: str         # 'rpm' or 'deb' (user-facing)
    arch: str           # 'arm64' or 'amd64'


@dataclass(frozen=True)
class Catalog:
    entries: tuple
    lookup_index: dict   # lower(alias_or_name) -> canonical name


def load_catalog(path) -> Catalog:
    """Load and validate the catalog. Raises ResolverError on failure."""
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as e:
        raise ResolverError(f"catalog not found: {path}") from e
    except json.JSONDecodeError as e:
        raise ResolverError(f"{path}: parse error: {e}") from e

    entries = []
    for block_name in ("rhel", "deb"):
        block = data.get(block_name, [])
        family = _BLOCK_TO_FAMILY[block_name]
        for idx, raw in enumerate(block):
            entries.append(_build_entry(raw, family, block_name, idx, path))

    _validate_uniqueness(entries)
    lookup_index = {}
    for e in entries:
        lookup_index[e.name.lower()] = e.name
        lookup_index[e.alias.lower()] = e.name
    return Catalog(entries=tuple(entries), lookup_index=lookup_index)


def _build_entry(raw, family, block_name, idx, path):
    name = (raw.get("name") or "").strip()
    alias = (raw.get("alias") or "").strip()
    description = raw.get("description", "")
    enabled = bool(raw.get("enabled", False))

    if not name:
        raise ResolverError(f"{path}: entry at {block_name}[{idx}] has empty name")
    arch = _derive_arch_from_name(name)
    if arch is None:
        raise ResolverError(
            f"{path}: name {name!r} must end with -arm or -amd "
            f"(arch suffix is load-bearing)"
        )
    if not alias:
        raise ResolverError(f"{path}: {name}: alias missing")
    alias_arch = _derive_arch_from_alias(alias)
    if alias_arch is None:
        raise ResolverError(
            f"{path}: alias {alias!r} must end with -arm64 or -amd64"
        )
    if alias_arch != arch:
        raise ResolverError(
            f"{path}: entry {name}: alias {alias!r} arch ({alias_arch}) "
            f"disagrees with name's arch ({arch})"
        )
    return CatalogEntry(name=name, alias=alias, description=description,
                        enabled=enabled, family=family, arch=arch)


def _derive_arch_from_name(name):
    for suf, val in _CANONICAL_ARCH_SUFFIXES.items():
        if name.endswith(suf):
            return val
    return None


def _derive_arch_from_alias(alias):
    for suf, val in _ALIAS_ARCH_SUFFIXES.items():
        if alias.endswith(suf):
            return val
    return None


def _validate_uniqueness(entries):
    seen_alias = {}
    names = {e.name for e in entries}
    for e in entries:
        key = e.alias.lower()
        if key in seen_alias:
            raise ResolverError(
                f"alias {e.alias!r} used by both {seen_alias[key]} and {e.name}"
            )
        seen_alias[key] = e.name
        # Defense-in-depth: with the current suffix rules (names end in
        # -arm/-amd, aliases end in -arm64/-amd64) this branch is unreachable
        # for catalogs that pass per-entry suffix validation. Kept anyway in
        # case the suffix policy is ever relaxed in a future release.
        if e.alias in names and e.alias != e.name:
            raise ResolverError(
                f"alias {e.alias!r} collides with name of another entry"
            )


def _parse_override(raw):
    """Split-and-trim a raw override string. Returns list[str] of >=1 token,
    or None if raw is None/whitespace. Raises ResolverError if raw is non-empty
    but parses to zero entries (e.g. ',' or ', ,')."""
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    tokens = [t.strip() for t in stripped.split(",")]
    tokens = [t for t in tokens if t]
    if not tokens:
        raise ResolverError(
            f"override value {raw!r} has no valid entries"
        )
    return tokens


def _resolve_override_tokens(catalog, raw_override, env_override):
    """Apply the preference hierarchy and resolve tokens to canonical names.

    Returns: (canonical_names: list[str], source: 'cli'|'env'|'default').
    Default-path returns ([], 'default'); callers materialize the enabled
    subset themselves.
    """
    tokens = _parse_override(raw_override)
    source = "cli" if tokens is not None else None
    if tokens is None:
        tokens = _parse_override(env_override)
        if tokens is not None:
            source = "env"
    if tokens is None:
        return [], "default"

    # 'all' handling
    if any(t.lower() == "all" for t in tokens):
        if len(tokens) > 1:
            raise ResolverError(
                f"'all' must be the only token in --containers; "
                f"mix not supported (got: {', '.join(tokens)})"
            )
        return [e.name for e in catalog.entries], source

    canonical = []
    seen = set()
    for tok in tokens:
        canon = catalog.lookup_index.get(tok.lower())
        if canon is None:
            valid_aliases = sorted({e.alias for e in catalog.entries})
            valid_names = sorted({e.name for e in catalog.entries})
            raise ResolverError(
                f"Unknown container {tok!r}. "
                f"Valid aliases: {valid_aliases}; valid names: {valid_names}"
            )
        if canon in seen:
            sys.stderr.write(
                f"[container-override] dedup'd {tok!r} (same as {canon!r})\n"
            )
            continue
        seen.add(canon)
        canonical.append(canon)
    return canonical, source


def validate_global(catalog, raw_override, env_override, scope_families, scope_arches):
    """Run-level validation. Returns (resolved_canonical_names, source_label).

    Source label: 'cli', 'env', or 'default'.

    On the override path, fails fast if no requested container's
    (family, arch) is in the (scope_families, scope_arches) cross-product.

    On the default path, returns the catalog's enabled:true subset and does
    NOT perform the global-zero check (preserves pre-v2.2 behavior).
    """
    resolved, source = _resolve_override_tokens(catalog, raw_override, env_override)

    if source == "default":
        resolved = [e.name for e in catalog.entries if e.enabled]
        return resolved, source

    by_name = {e.name: e for e in catalog.entries}
    in_scope_anywhere = any(
        by_name[c].family in scope_families and by_name[c].arch in scope_arches
        for c in resolved
    )
    if not in_scope_anywhere:
        raise ResolverError(
            f"All {len(resolved)} requested containers are out of scope for the "
            f"selected --platforms / --arch.\n"
            f"Requested: {resolved}.\n"
            f"Scope: families={sorted(scope_families)}, "
            f"arches={sorted(scope_arches)}.\n"
            f"Either expand the scope, or pick containers that match it."
        )
    return resolved, source
