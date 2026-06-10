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

    # alias index built later as more rules land
    lookup_index = {e.name.lower(): e.name for e in entries}

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
    return CatalogEntry(name=name, alias=alias, description=description,
                        enabled=enabled, family=family, arch=arch)


def _derive_arch_from_name(name):
    for suf, val in _CANONICAL_ARCH_SUFFIXES.items():
        if name.endswith(suf):
            return val
    return None
