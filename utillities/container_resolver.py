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
