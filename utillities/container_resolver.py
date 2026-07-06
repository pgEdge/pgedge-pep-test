"""Container target resolver for the PEP Regression framework.

Owns:
- Loading and validating configuration/containers_list.json
- Resolving runtime overrides (--containers, PEP_CONTAINERS) against the catalog
- Per-(family, arch) target filtering
- The --list-containers display

User-facing docs: see the "Selecting container targets at runtime" section
in docs/CI.md.
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
        if block_name not in data:
            raise ResolverError(
                f"{path}: required '{block_name}' block is missing"
            )
        block = data[block_name]
        if not isinstance(block, list):
            raise ResolverError(
                f"{path}: '{block_name}' block must be a list (got {type(block).__name__})"
            )
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
    if not isinstance(raw, dict):
        raise ResolverError(
            f"{path}: entry at {block_name}[{idx}] is not an object"
        )

    # name: must be present and a string. Type-check BEFORE .strip() to avoid
    # raw AttributeError on numeric/list/dict values.
    if "name" not in raw:
        raise ResolverError(
            f"{path}: {block_name}[{idx}]: name missing"
        )
    raw_name = raw["name"]
    if not isinstance(raw_name, str):
        raise ResolverError(
            f"{path}: {block_name}[{idx}]: name must be a string "
            f"(got {type(raw_name).__name__})"
        )
    name = raw_name.strip()

    # description: must be present and a string
    if "description" not in raw:
        raise ResolverError(
            f"{path}: {block_name}[{idx}] ({name or 'unnamed'}): description missing"
        )
    description = raw["description"]
    if not isinstance(description, str):
        raise ResolverError(
            f"{path}: {block_name}[{idx}] ({name or 'unnamed'}): description must be a string"
        )

    # enabled: must be present and a real JSON bool. JSON's true/false parse to
    # Python bool; anything else (string, number, null, missing) is rejected.
    # Note: bool is a subclass of int in Python, but isinstance(x, bool) is
    # True only for actual bools, so this rejects integers like 0/1 correctly.
    if "enabled" not in raw:
        raise ResolverError(
            f"{path}: {block_name}[{idx}] ({name or 'unnamed'}): enabled missing"
        )
    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        raise ResolverError(
            f"{path}: {block_name}[{idx}] ({name or 'unnamed'}): "
            f"enabled must be a JSON bool (got {type(enabled).__name__})"
        )

    # alias: must be present and a string. Type-check BEFORE .strip().
    if "alias" not in raw:
        raise ResolverError(f"{path}: {name}: alias missing")
    raw_alias = raw["alias"]
    if not isinstance(raw_alias, str):
        raise ResolverError(
            f"{path}: {name}: alias must be a string "
            f"(got {type(raw_alias).__name__})"
        )
    alias = raw_alias.strip()

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


def _flip_arch_suffix(token, is_alias):
    """Return `token` with its trailing arch suffix flipped to the other arch.

    Canonical names use -arm/-amd; aliases use -arm64/-amd64. Only the arch
    suffix is touched — any prefix (auto-, my-, or the OS/version base) is
    preserved verbatim. Returns the token unchanged if it has no recognized
    suffix.
    """
    if is_alias:
        if token.endswith("-amd64"):
            return token[:-len("-amd64")] + "-arm64"
        if token.endswith("-arm64"):
            return token[:-len("-arm64")] + "-amd64"
    else:
        if token.endswith("-amd"):
            return token[:-len("-amd")] + "-arm"
        if token.endswith("-arm"):
            return token[:-len("-arm")] + "-amd"
    return token


def _entry_by_name(catalog, name):
    """Return the real CatalogEntry with this canonical name, or None."""
    for e in catalog.entries:
        if e.name == name:
            return e
    return None


def resolve_token(catalog, token):
    """Resolve one token to a CatalogEntry, real or synthesized.

    Resolution order — REAL CATALOG ENTRIES ALWAYS WIN:
      1. If the token matches a catalog alias or canonical name, return that
         real entry. Synthesis never happens for a token the catalog defines;
         the prefix-preservation rule below applies only to a genuinely
         missing counterpart.
      2. On a lookup MISS, find the opposite-arch sibling and compute the
         would-be target identity by flipping the sibling's real alias/name.
         If that target alias OR name already exists in the catalog (possibly
         under a DIFFERENT prefix, e.g. the real my-rocky9-amd for an
         auto-rocky9-amd input), return that REAL entry — real entries win by
         target identity, not just by exact input token, so inferred canonical
         forms dedupe against the catalog instead of duplicating a target.
      3. Only when the target is genuinely absent under this arch, synthesize
         an in-memory CatalogEntry for the requested arch. The synthesized
         canonical name is the sibling's real name with ONLY the arch suffix
         flipped (prefix preserved, e.g. my-rocky9-amd -> my-rocky9-arm). This
         name is an INTERNAL runtime identifier used to drive image/platform
         resolution; it is NOT a promise that a future explicit catalog entry
         will use the same name.
      4. If the OS/version is absent under both arches, return None. Callers
         treat None as an unknown container and fail fast.

    Accepts either the alias form (…-arm64/…-amd64) or the canonical name form
    (…-arm/…-amd), so it also serves as the consolidated report's
    name -> entry (for alias display) lookup.
    """
    tok = token.strip()
    canon = catalog.lookup_index.get(tok.lower())
    if canon is not None:
        return _entry_by_name(catalog, canon)

    # Lookup miss: try opposite-arch counterpart synthesis.
    arch = _derive_arch_from_name(tok)
    is_alias = False
    if arch is None:
        arch = _derive_arch_from_alias(tok)
        is_alias = arch is not None
    if arch is None:
        return None  # not an arch-suffixed token — genuinely unknown

    sibling_token = _flip_arch_suffix(tok, is_alias)
    sib_canon = catalog.lookup_index.get(sibling_token.lower())
    if sib_canon is None:
        return None  # OS/version absent under both arches — genuinely unknown

    sibling = _entry_by_name(catalog, sib_canon)
    # Compute the would-be target identity by flipping the SIBLING's real name
    # and alias. A real catalog entry may already provide this target under a
    # different prefix (e.g. sibling auto-rocky9-arm -> target alias
    # rocky9-amd64, which is the real my-rocky9-amd). Real entries must win by
    # target-alias/name identity too — not just by the exact input token — so
    # inferred canonical forms dedupe against the real catalog rather than
    # creating a duplicate logical target.
    target_name = _flip_arch_suffix(sibling.name, is_alias=False)
    target_alias = _flip_arch_suffix(sibling.alias, is_alias=True)
    real_canon = (catalog.lookup_index.get(target_alias.lower())
                  or catalog.lookup_index.get(target_name.lower()))
    if real_canon is not None:
        return _entry_by_name(catalog, real_canon)

    # Genuinely absent under this arch — synthesize the counterpart.
    return CatalogEntry(
        name=target_name,
        alias=target_alias,
        description=f"{sibling.description} (implicit {arch} counterpart)",
        enabled=False,
        family=sibling.family,
        arch=arch,
    )


def _validate_uniqueness(entries):
    # Canonical name uniqueness (case-insensitive, matching the lookup namespace).
    seen_name = {}
    for e in entries:
        key = e.name.lower()
        if key in seen_name:
            raise ResolverError(
                f"duplicate name {e.name!r} (also seen as {seen_name[key]!r})"
            )
        seen_name[key] = e.name

    # Alias uniqueness (case-insensitive).
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
    """Apply the preference hierarchy and resolve tokens to CatalogEntry objects.

    Returns: (entries: list[CatalogEntry], source: 'cli'|'env'|'default').
    Entries may be real (from the catalog) or synthesized opposite-arch
    counterparts (see resolve_token). Carrying entry objects — rather than
    names re-looked-up via {e.name: e for e in catalog.entries} — lets
    synthesized entries (which are NOT in catalog.entries) flow through
    validation and per-target filtering without a KeyError.

    'all' expands to the real catalog only (catalog.entries); it does NOT
    include implicit opposite-arch counterparts. Default-path returns
    ([], 'default'); callers materialize the enabled subset themselves.
    """
    tokens = _parse_override(raw_override)
    source = "cli" if tokens is not None else None
    if tokens is None:
        tokens = _parse_override(env_override)
        if tokens is not None:
            source = "env"
    if tokens is None:
        return [], "default"

    # 'all' handling — real catalog only, never implicit counterparts.
    if any(t.lower() == "all" for t in tokens):
        if len(tokens) > 1:
            raise ResolverError(
                f"'all' must be the only token in --containers; "
                f"mix not supported (got: {', '.join(tokens)})"
            )
        return list(catalog.entries), source

    entries = []
    seen = set()
    for tok in tokens:
        entry = resolve_token(catalog, tok)
        if entry is None:
            valid_aliases = sorted({e.alias for e in catalog.entries})
            valid_names = sorted({e.name for e in catalog.entries})
            raise ResolverError(
                f"Unknown container {tok!r}. Opposite-arch counterparts are "
                f"accepted only when that OS/version already exists in the "
                f"catalog under the other arch; {tok!r} matches neither a "
                f"catalog entry nor a known counterpart. "
                f"Valid aliases: {valid_aliases}; valid names: {valid_names}"
            )
        # Dedup on the resolved canonical name so an alias and its canonical
        # form (or two aliases) of the same (synthesized) entry collapse to one.
        if entry.name in seen:
            sys.stderr.write(
                f"[container-override] dedup'd {tok!r} (same as {entry.name!r})\n"
            )
            continue
        seen.add(entry.name)
        entries.append(entry)
    return entries, source


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
        return [e.name for e in catalog.entries if e.enabled], source

    # resolved is a list of CatalogEntry objects (real or synthesized), so
    # family/arch are read directly — no by_name lookup that would miss
    # synthesized counterparts.
    in_scope_anywhere = any(
        e.family in scope_families and e.arch in scope_arches
        for e in resolved
    )
    if not in_scope_anywhere:
        raise ResolverError(
            f"All {len(resolved)} requested containers are out of scope for the "
            f"selected --platforms / --arch.\n"
            f"Requested: {[e.name for e in resolved]}.\n"
            f"Scope: families={sorted(scope_families)}, "
            f"arches={sorted(scope_arches)}.\n"
            f"Either expand the scope, or pick containers that match it."
        )
    return [e.name for e in resolved], source


def resolve_for_target(catalog, raw_override, env_override, target_family, target_arch):
    """Per-(family, arch) filter on top of the resolved override.

    target_arch may be None ('no arch filter' — accepts both arches of the
    target_family). This matches pre-v2.2 local behavior when --arch is omitted.

    Returns: (effective, per_target_out_of_scope, source) where source is
    'cli', 'env', or 'default'. The source label lets the CLI dispatcher
    decide whether to emit override-related log lines (suppressed on default
    path so logs stay byte-identical to pre-v2.2 for non-override runs).

    Re-parses the override defensively in case this function is called
    without validate_global (e.g. by a future code path). Catalog errors
    surface here too.
    """
    resolved, source = _resolve_override_tokens(catalog, raw_override, env_override)
    if source == "default":
        resolved = [e for e in catalog.entries if e.enabled]

    # resolved is a list of CatalogEntry objects (real or synthesized). Filter
    # on the carried objects; emit canonical names at the boundary.
    effective = []
    per_target_out_of_scope = []
    for entry in resolved:
        family_matches = (entry.family == target_family)
        arch_matches = (target_arch is None) or (entry.arch == target_arch)
        if family_matches and arch_matches:
            effective.append(entry.name)
        else:
            per_target_out_of_scope.append(entry.name)
    return effective, per_target_out_of_scope, source


def list_containers(catalog):
    """Return a printable text table for --list-containers."""
    header = (
        f"{'ALIAS':<20} {'CANONICAL NAME':<23} {'FAMILY':<7} "
        f"{'ARCH':<7} {'ENABLED':<8} DESCRIPTION"
    )
    lines = [header]
    for e in catalog.entries:
        enabled_str = "true" if e.enabled else "false"
        lines.append(
            f"{e.alias:<20} {e.name:<23} {e.family:<7} "
            f"{e.arch:<7} {enabled_str:<8} {e.description}"
        )
    return "\n".join(lines)


import argparse
import os


def _csv_to_set(value):
    return {tok.strip() for tok in value.split(",") if tok.strip()}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="container_resolver",
        description="Catalog & runtime override resolver for the PEP Regression framework.",
    )
    parser.add_argument(
        "--catalog", default="configuration/containers_list.json",
        help="Path to containers_list.json (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_vg = sub.add_parser("validate-global",
        help="Validate override against the user's full scope (CI plan job / local startup).")
    p_vg.add_argument("--containers", default="",
        help="--containers override value (may be empty).")
    p_vg.add_argument("--scope-families", required=True,
        help="CSV of user-facing family names (rpm, deb).")
    p_vg.add_argument("--scope-arches", required=True,
        help="CSV of user-facing arch names (arm64, amd64).")

    p_rft = sub.add_parser("resolve-for-target",
        help="Resolve effective containers for one (family, arch) call.")
    p_rft.add_argument("--containers", default="")
    p_rft.add_argument("--target-family", required=True)
    p_rft.add_argument("--target-arch", default="",
        help="Empty string = no arch filter.")

    sub.add_parser("list-containers",
        help="Print the catalog as a human-readable table.")

    args = parser.parse_args(argv)

    try:
        catalog = load_catalog(args.catalog)
    except ResolverError as e:
        sys.stderr.write(f"[container-override] ERROR: {e}\n")
        return 2

    env_override = os.environ.get("PEP_CONTAINERS")

    if args.command == "validate-global":
        try:
            resolved, source = validate_global(
                catalog, args.containers or None, env_override,
                _csv_to_set(args.scope_families), _csv_to_set(args.scope_arches),
            )
        except ResolverError as e:
            sys.stderr.write(f"[container-override] ERROR: {e}\n")
            return 2
        # On the default path emit NO override-related stderr lines, so logs
        # are byte-identical to pre-v2.2 for runs that don't use the feature.
        # Only chatter when the user actually supplied an override.
        if source != "default":
            sys.stderr.write(f"[container-override] source={source}\n")
            sys.stderr.write(
                f"[container-override] requested: {', '.join(resolved)}\n"
            )
        print(",".join(resolved))
        return 0

    if args.command == "resolve-for-target":
        target_arch = args.target_arch or None
        try:
            effective, oos, source = resolve_for_target(
                catalog, args.containers or None, env_override,
                args.target_family, target_arch,
            )
        except ResolverError as e:
            sys.stderr.write(f"[container-override] ERROR: {e}\n")
            return 2
        # Suppress [container-override] log noise on the default path so
        # non-override runs see the same logs as pre-v2.2. The [container-
        # resolution] line is emitted by the shell caller and is unchanged.
        if source != "default":
            target_label = f"{args.target_family}/{target_arch or 'any-arch'}"
            for c in oos:
                # resolve_token handles synthesized counterparts too (c may be
                # a synthesized canonical name not present in catalog.entries).
                entry = resolve_token(catalog, c)
                fam = entry.family if entry else "?"
                arch = entry.arch if entry else "?"
                sys.stderr.write(
                    f"[container-override] out-of-scope for this target: "
                    f"{c} ({fam}/{arch}; current target {target_label})\n"
                )
            sys.stderr.write(
                f"[container-override] effective for target {target_label}: "
                f"{', '.join(effective) if effective else '(none)'}\n"
            )
        print(",".join(effective))
        return 0

    if args.command == "list-containers":
        print(list_containers(catalog))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
