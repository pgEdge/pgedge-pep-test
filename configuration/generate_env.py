#!/usr/bin/env python3
"""
Regenerate ALL_PACKAGES and DEB_ALL_PACKAGES in config16/17/18/19.env
from configuration/packages_test_matrix.json.

Workflow:
  1. Edit packages_test_matrix.json  (set enabled: true/false per component)
  2. python configuration/generate_env.py
  3. Run tests

Usage:
    python configuration/generate_env.py              # Update all config env files
    python configuration/generate_env.py --dry-run    # Print what would change, no writes
    python configuration/generate_env.py --pg 18      # Only update config18.env
"""

import argparse
import json
import re
import sys
from pathlib import Path

CONFIG_DIR = Path(__file__).parent
MATRIX_FILE = CONFIG_DIR / "packages_test_matrix.json"
PG_VERSIONS = [16, 17, 18, 19]


def build_package_list(components: list, pg: int, platform: str) -> str:
    """Return a deduplicated comma-separated package list for the given platform."""
    seen: set = set()
    pkgs: list = []
    for comp in components:
        if not comp.get("enabled", False):
            continue
        if "pg_versions" in comp and pg not in comp["pg_versions"]:
            continue
        raw = comp.get(platform)
        if not raw:
            continue
        pkg = raw.replace("{PG}", str(pg))
        if pkg not in seen:
            seen.add(pkg)
            pkgs.append(pkg)
    return ",".join(pkgs)


def update_config(path: Path, pg: int, rhel: str, deb: str, dry_run: bool) -> None:
    original = path.read_text()

    updated = re.sub(
        r"^export ALL_PACKAGES=.*$",
        f"export ALL_PACKAGES={rhel}",
        original,
        flags=re.MULTILINE,
    )
    updated = re.sub(
        r"^export DEB_ALL_PACKAGES=.*$",
        f"export DEB_ALL_PACKAGES={deb}",
        updated,
        flags=re.MULTILINE,
    )

    if updated == original:
        print(f"  [PG{pg}] {path.name} — no changes")
        return

    if dry_run:
        print(f"  [PG{pg}] {path.name} — would update:")
        print(f"    RHEL ({len(rhel.split(','))} pkgs): {rhel[:100]}{'...' if len(rhel) > 100 else ''}")
        print(f"    DEB  ({len(deb.split(','))} pkgs): {deb[:100]}{'...' if len(deb) > 100 else ''}")
    else:
        path.write_text(updated)
        rhel_count = len(rhel.split(","))
        deb_count  = len(deb.split(","))
        print(f"  [PG{pg}] {path.name} updated  (RHEL: {rhel_count} pkgs, DEB: {deb_count} pkgs)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate ALL_PACKAGES / DEB_ALL_PACKAGES from packages_test_matrix.json"
    )
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    parser.add_argument("--pg", type=int, choices=PG_VERSIONS, metavar="VERSION",
                        help=f"Target a single PG version ({', '.join(str(v) for v in PG_VERSIONS)})")
    args = parser.parse_args()

    if not MATRIX_FILE.exists():
        sys.exit(f"ERROR: {MATRIX_FILE} not found")

    with MATRIX_FILE.open() as f:
        matrix = json.load(f)
    components = matrix["components"]

    # Summary of enabled/disabled state
    enabled  = [c["name"] for c in components if c.get("enabled")]
    disabled = [c["name"] for c in components if not c.get("enabled")]
    print(f"Matrix: {len(enabled)} enabled, {len(disabled)} disabled")
    if disabled:
        print(f"  Disabled: {', '.join(disabled)}")

    print()
    versions = [args.pg] if args.pg else PG_VERSIONS
    for pg in versions:
        config_file = CONFIG_DIR / f"config{pg}.env"
        if not config_file.exists():
            print(f"  [PG{pg}] {config_file.name} not found — skipping")
            continue
        rhel_pkgs = build_package_list(components, pg, "rhel")
        deb_pkgs  = build_package_list(components, pg, "deb")
        update_config(config_file, pg, rhel_pkgs, deb_pkgs, args.dry_run)

    print()
    if args.dry_run:
        print("Dry run complete. Re-run without --dry-run to apply changes.")
    else:
        print("Done. Commit configuration/ to persist the updated package lists.")


if __name__ == "__main__":
    main()