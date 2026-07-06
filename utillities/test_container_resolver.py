"""Unit tests for utillities.container_resolver — synthetic-catalog only."""
import importlib.util
import json
from pathlib import Path

import pytest

# Load the module under test by path so this test file works under bare
# `pytest utillities/test_container_resolver.py` invocations (the repo has
# no pytest config / __init__.py in utillities/, and v2.1's
# test_ci_consolidated_report.py uses the same pattern).
#
# The module is registered in sys.modules before exec_module so dataclass
# forward-ref resolution (which uses cls.__module__) works under Python's
# `from __future__ import annotations` semantics.
import sys
_spec = importlib.util.spec_from_file_location(
    "container_resolver",
    str(Path(__file__).parent / "container_resolver.py"),
)
cr = importlib.util.module_from_spec(_spec)
sys.modules["container_resolver"] = cr
_spec.loader.exec_module(cr)


def _write_catalog(tmp_path, rhel=None, deb=None):
    body = {"_comment": "test", "rhel": rhel or [], "deb": deb or []}
    p = tmp_path / "containers_list.json"
    p.write_text(json.dumps(body))
    return p


def _minimal_valid_catalog(tmp_path):
    rhel = [
        {"name": "auto-rocky9-arm", "alias": "rocky9-arm64",
         "description": "Rocky 9 ARM", "enabled": True},
        {"name": "auto-alma9-amd", "alias": "alma9-amd64",
         "description": "Alma 9 AMD", "enabled": False},
    ]
    deb = [
        {"name": "auto-debian12-arm", "alias": "debian12-arm64",
         "description": "Debian 12 ARM", "enabled": True},
        {"name": "auto-debian13-amd", "alias": "debian13-amd64",
         "description": "Debian 13 AMD", "enabled": True},
    ]
    return _write_catalog(tmp_path, rhel=rhel, deb=deb)


def test_load_catalog_returns_entries(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    names = {e.name for e in catalog.entries}
    assert names == {"auto-rocky9-arm", "auto-alma9-amd",
                     "auto-debian12-arm", "auto-debian13-amd"}


def test_load_catalog_family_normalization(tmp_path):
    # rhel block -> user-facing 'rpm'; deb block -> 'deb'.
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    by_name = {e.name: e for e in catalog.entries}
    assert by_name["auto-rocky9-arm"].family == "rpm"
    assert by_name["auto-debian12-arm"].family == "deb"


def test_load_catalog_arch_derivation(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    by_name = {e.name: e for e in catalog.entries}
    assert by_name["auto-rocky9-arm"].arch == "arm64"
    assert by_name["auto-alma9-amd"].arch == "amd64"


def test_load_catalog_name_missing_arch_suffix_fails(tmp_path):
    # 'auto-rocky9' has no -arm or -amd suffix; load must fail-fast.
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9", "alias": "rocky9-arm64",
         "description": "x", "enabled": True}
    ])
    with pytest.raises(cr.ResolverError, match="must end with -arm or -amd"):
        cr.load_catalog(p)


def test_load_catalog_unparseable_json_fails(tmp_path):
    p = tmp_path / "containers_list.json"
    p.write_text("{not json")
    with pytest.raises(cr.ResolverError, match="parse error"):
        cr.load_catalog(p)


def test_load_catalog_missing_alias_fails(tmp_path):
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9-arm", "alias": "", "description": "x", "enabled": True}
    ])
    with pytest.raises(cr.ResolverError, match="alias missing"):
        cr.load_catalog(p)


def test_load_catalog_alias_wrong_suffix_fails(tmp_path):
    # 'rocky9' has no -arm64 or -amd64 suffix.
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9-arm", "alias": "rocky9",
         "description": "x", "enabled": True}
    ])
    with pytest.raises(cr.ResolverError, match="must end with -arm64 or -amd64"):
        cr.load_catalog(p)


def test_load_catalog_alias_arch_disagrees_with_name_fails(tmp_path):
    # name says -arm (arm64) but alias says -amd64; load must fail.
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9-arm", "alias": "rocky9-amd64",
         "description": "x", "enabled": True}
    ])
    with pytest.raises(cr.ResolverError, match="arch.*disagrees"):
        cr.load_catalog(p)


def test_load_catalog_duplicate_alias_fails(tmp_path):
    p = _write_catalog(tmp_path,
        rhel=[
            {"name": "auto-rocky9-arm", "alias": "shared-arm64",
             "description": "x", "enabled": True}
        ],
        deb=[
            {"name": "auto-debian12-arm", "alias": "shared-arm64",
             "description": "y", "enabled": True}
        ])
    with pytest.raises(cr.ResolverError, match="used by both"):
        cr.load_catalog(p)


def test_lookup_index_resolves_aliases_and_canonical(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    # alias -> canonical
    assert catalog.lookup_index["rocky9-arm64"] == "auto-rocky9-arm"
    # canonical -> canonical
    assert catalog.lookup_index["auto-debian13-amd"] == "auto-debian13-amd"
    # case-insensitive lookups
    assert catalog.lookup_index["DEBIAN12-arm64".lower()] == "auto-debian12-arm"


def test_real_catalog_loads_with_all_aliases():
    """The shipped configuration/containers_list.json must load cleanly
    and expose an alias on every entry."""
    catalog = cr.load_catalog("configuration/containers_list.json")
    assert len(catalog.entries) == 17, (
        f"expected 17 catalog entries, got {len(catalog.entries)}"
    )
    for e in catalog.entries:
        assert e.alias, f"{e.name}: alias must be non-empty"
        assert e.alias.endswith(("-arm64", "-amd64"))


def test_real_catalog_specific_aliases():
    catalog = cr.load_catalog("configuration/containers_list.json")
    by_name = {e.name: e.alias for e in catalog.entries}
    assert by_name["auto-rocky9-arm"] == "rocky9-arm64"
    assert by_name["auto-debian13-amd"] == "debian13-amd64"
    assert by_name["auto-ubuntu2404-arm"] == "ubuntu2404-arm64"
    assert by_name["auto-ubuntu2604-arm"] == "ubuntu2604-arm64"
    assert by_name["auto-ubuntu2604-amd"] == "ubuntu2604-amd64"


def test_override_default_path_returns_enabled_subset(tmp_path, capsys):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, source = cr._resolve_override_tokens(catalog, None, None)
    assert source == "default"
    # Default path returns [] here; the enabled subset is materialized by callers.
    assert resolved == []


def test_override_cli_beats_env(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, source = cr._resolve_override_tokens(
        catalog, "rocky9-arm64", "alma9-amd64"
    )
    assert source == "cli"
    assert [e.name for e in resolved] == ["auto-rocky9-arm"]


def test_override_env_used_when_cli_absent(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, source = cr._resolve_override_tokens(catalog, None, "rocky9-arm64")
    assert source == "env"
    assert [e.name for e in resolved] == ["auto-rocky9-arm"]


def test_override_cli_whitespace_falls_through_to_env(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    canonical, source = cr._resolve_override_tokens(
        catalog, "   ", "rocky9-arm64"
    )
    assert source == "env"


def test_override_comma_only_fails(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    with pytest.raises(cr.ResolverError, match="no valid entries"):
        cr._resolve_override_tokens(catalog, ", ,", None)


def test_override_unknown_token_fails(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    with pytest.raises(cr.ResolverError, match="Unknown container 'foo'"):
        cr._resolve_override_tokens(catalog, "foo", None)


def test_override_all_token_returns_full_catalog(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, source = cr._resolve_override_tokens(catalog, "all", None)
    assert source == "cli"
    # All 4 entries from the fixture, including the enabled:false ones.
    # 'all' is real-catalog-only: no implicit opposite-arch counterparts.
    assert {e.name for e in resolved} == {
        "auto-rocky9-arm", "auto-alma9-amd",
        "auto-debian12-arm", "auto-debian13-amd",
    }


def test_override_all_must_be_sole_token(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    with pytest.raises(cr.ResolverError, match="'all' must be the only token"):
        cr._resolve_override_tokens(catalog, "all, rocky9-arm64", None)


def test_override_dedup_alias_and_canonical(tmp_path, capsys):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, source = cr._resolve_override_tokens(
        catalog, "rocky9-arm64, auto-rocky9-arm", None
    )
    assert [e.name for e in resolved] == ["auto-rocky9-arm"]
    err = capsys.readouterr().err
    assert "dedup'd" in err


# ---------------------------------------------------------------------------
# Opposite-arch counterpart synthesis (implicit targets).
#
# The minimal fixture has each OS present under exactly one arch:
#   rocky9   -> only -arm   (rpm)   |  alma9    -> only -amd  (rpm)
#   debian12 -> only -arm   (deb)   |  debian13 -> only -amd  (deb)
# so every counterpart below is a genuine lookup miss that must synthesize.
# ---------------------------------------------------------------------------

def test_resolve_token_returns_real_entry_first(tmp_path):
    """Real catalog entries always win; synthesis only on a miss."""
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    e = cr.resolve_token(catalog, "rocky9-arm64")
    assert e.name == "auto-rocky9-arm"
    assert e.enabled is True  # the real entry, not a synthesized (enabled=False) one


def test_resolve_token_synthesizes_counterpart_from_alias(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    e = cr.resolve_token(catalog, "rocky9-amd64")  # only rocky9-arm64 is real
    assert e is not None
    assert e.name == "auto-rocky9-amd"      # sibling prefix preserved, suffix flipped
    assert e.alias == "rocky9-amd64"
    assert e.family == "rpm"                # inherited from sibling
    assert e.arch == "amd64"
    assert e.enabled is False               # synthesized entries are not "enabled"


def test_resolve_token_synthesizes_counterpart_from_canonical_name(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    e = cr.resolve_token(catalog, "auto-debian12-amd")  # only auto-debian12-arm is real
    assert e is not None
    assert e.name == "auto-debian12-amd"
    assert e.alias == "debian12-amd64"
    assert e.family == "deb"
    assert e.arch == "amd64"


def test_resolve_token_preserves_nonstandard_prefix(tmp_path):
    """Prefix (e.g. 'my-') is preserved; only the arch suffix flips."""
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "rhel": [{"name": "my-rocky9-amd", "alias": "rocky9-amd64",
                  "description": "Rocky 9 AMD", "enabled": True}],
        "deb": [{"name": "auto-debian12-arm", "alias": "debian12-arm64",
                 "description": "Debian 12 ARM", "enabled": True}],
    }))
    catalog = cr.load_catalog(p)
    e = cr.resolve_token(catalog, "rocky9-arm64")  # counterpart of my-rocky9-amd
    assert e is not None
    assert e.name == "my-rocky9-arm"   # 'my-' prefix preserved
    assert e.alias == "rocky9-arm64"
    assert e.arch == "arm64"


def _mixed_prefix_catalog(tmp_path):
    """Catalog where rocky9 exists under BOTH arches but with different name
    prefixes: auto-rocky9-arm (arm64) and my-rocky9-amd (amd64). Exercises the
    'real entry wins by target identity, not just by input token' rule."""
    p = tmp_path / "mixed.json"
    p.write_text(json.dumps({
        "rhel": [
            {"name": "auto-rocky9-arm", "alias": "rocky9-arm64",
             "description": "Rocky 9 ARM", "enabled": True},
            {"name": "my-rocky9-amd", "alias": "rocky9-amd64",
             "description": "Rocky 9 AMD", "enabled": False},
        ],
        "deb": [
            {"name": "auto-debian12-arm", "alias": "debian12-arm64",
             "description": "Debian 12 ARM", "enabled": True},
        ],
    }))
    return p


def test_resolve_token_inferred_canonical_returns_real_diff_prefix(tmp_path):
    """auto-rocky9-amd (inferred canonical) must return the REAL my-rocky9-amd
    entry — not a synthesized auto-rocky9-amd duplicate — because rocky9-amd64
    already exists in the catalog under a different prefix."""
    catalog = cr.load_catalog(_mixed_prefix_catalog(tmp_path))
    e = cr.resolve_token(catalog, "auto-rocky9-amd")
    assert e is not None
    assert e.name == "my-rocky9-amd"   # the real entry, not synthesized auto-*
    assert e.alias == "rocky9-amd64"
    assert e.arch == "amd64"


def test_resolve_token_inferred_canonical_returns_real_arm_diff_prefix(tmp_path):
    """my-rocky9-arm (inferred canonical) must return the REAL auto-rocky9-arm
    entry because rocky9-arm64 already exists under a different prefix."""
    catalog = cr.load_catalog(_mixed_prefix_catalog(tmp_path))
    e = cr.resolve_token(catalog, "my-rocky9-arm")
    assert e is not None
    assert e.name == "auto-rocky9-arm"  # the real entry, not synthesized my-*
    assert e.enabled is True            # proves it's the real (enabled) entry
    assert e.arch == "arm64"


def test_override_alias_and_inferred_canonical_dedupe_to_real(tmp_path, capsys):
    """rocky9-amd64 (alias -> real my-rocky9-amd) and auto-rocky9-amd (inferred
    canonical, also -> real my-rocky9-amd) collapse to ONE real entry."""
    catalog = cr.load_catalog(_mixed_prefix_catalog(tmp_path))
    resolved, source = cr._resolve_override_tokens(
        catalog, "rocky9-amd64, auto-rocky9-amd", None
    )
    assert [e.name for e in resolved] == ["my-rocky9-amd"]  # exactly one, real
    assert "dedup'd" in capsys.readouterr().err


def test_resolve_token_unknown_os_returns_none(tmp_path):
    """Truly unknown OS/version (absent under both arches) does NOT synthesize."""
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    assert cr.resolve_token(catalog, "fedora99-amd64") is None
    assert cr.resolve_token(catalog, "auto-fedora99-amd") is None
    assert cr.resolve_token(catalog, "not-arch-suffixed") is None


def test_override_accepts_counterpart_alias(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, source = cr._resolve_override_tokens(catalog, "debian13-arm64", None)
    assert source == "cli"
    assert [e.name for e in resolved] == ["auto-debian13-arm"]
    assert resolved[0].arch == "arm64" and resolved[0].family == "deb"


def test_override_unknown_os_still_fails_fast(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    with pytest.raises(cr.ResolverError, match="Unknown container 'fedora99-amd64'"):
        cr._resolve_override_tokens(catalog, "fedora99-amd64", None)


def test_override_dedup_alias_and_canonical_of_synthesized(tmp_path, capsys):
    """An alias and the canonical form of the SAME synthesized counterpart
    collapse to a single effective entry."""
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, source = cr._resolve_override_tokens(
        catalog, "debian12-amd64, auto-debian12-amd", None
    )
    assert [e.name for e in resolved] == ["auto-debian12-amd"]  # exactly one
    assert "dedup'd" in capsys.readouterr().err


def test_override_all_excludes_counterparts(tmp_path):
    """'all' expands to the real catalog only — never implicit counterparts."""
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, _ = cr._resolve_override_tokens(catalog, "all", None)
    names = {e.name for e in resolved}
    # None of the synthesizable counterparts appear.
    for absent in ("auto-rocky9-amd", "auto-alma9-arm",
                   "auto-debian12-amd", "auto-debian13-arm"):
        assert absent not in names


def test_validate_global_accepts_counterpart_in_scope(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, source = cr.validate_global(
        catalog, "debian13-arm64", None,           # synthesized deb/arm64
        scope_families={"deb"}, scope_arches={"arm64"},
    )
    assert source == "cli"
    assert resolved == ["auto-debian13-arm"]


def test_resolve_for_target_filters_synthesized_by_arch(tmp_path):
    """A synthesized deb/amd64 counterpart is kept for the amd64 target and
    filtered out of the arm64 target."""
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    # debian12-amd64 is synthesized (only debian12-arm64 is real).
    eff_amd, oos_amd, _ = cr.resolve_for_target(
        catalog, "debian12-amd64", None, target_family="deb", target_arch="amd64"
    )
    assert eff_amd == ["auto-debian12-amd"] and oos_amd == []
    eff_arm, oos_arm, _ = cr.resolve_for_target(
        catalog, "debian12-amd64", None, target_family="deb", target_arch="arm64"
    )
    assert eff_arm == [] and oos_arm == ["auto-debian12-amd"]


def test_validate_global_default_path_returns_enabled_subset(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, source = cr.validate_global(
        catalog, None, None,
        scope_families={"rpm", "deb"}, scope_arches={"arm64", "amd64"},
    )
    assert source == "default"
    assert set(resolved) == {"auto-rocky9-arm", "auto-debian12-arm",
                             "auto-debian13-amd"}  # enabled:true entries


def test_validate_global_override_in_scope_passes(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, source = cr.validate_global(
        catalog, "rocky9-arm64, debian12-arm64", None,
        scope_families={"rpm", "deb"}, scope_arches={"arm64", "amd64"},
    )
    assert source == "cli"
    assert set(resolved) == {"auto-rocky9-arm", "auto-debian12-arm"}


def test_validate_global_global_zero_fails(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    # Asking for deb containers with rpm-only scope -> global zero
    with pytest.raises(cr.ResolverError, match="out of scope for the selected"):
        cr.validate_global(
            catalog, "debian12-arm64, debian13-amd64", None,
            scope_families={"rpm"}, scope_arches={"arm64", "amd64"},
        )


def test_validate_global_default_path_skips_global_zero(tmp_path):
    """Even if the catalog default produces an empty set for a narrow scope,
    the default path must not fail-fast. Global-zero applies to overrides only.
    """
    # Build a catalog where ALL enabled entries are deb-family.
    p = _write_catalog(tmp_path,
        rhel=[{"name": "auto-rocky9-arm", "alias": "rocky9-arm64",
               "description": "x", "enabled": False}],
        deb=[{"name": "auto-debian12-arm", "alias": "debian12-arm64",
              "description": "y", "enabled": True}])
    catalog = cr.load_catalog(p)
    # Scope = rpm only. Default path -> enabled subset has only the deb entry.
    # We expect this to RETURN (not raise) because no override was supplied.
    resolved, source = cr.validate_global(
        catalog, None, None,
        scope_families={"rpm"}, scope_arches={"arm64"},
    )
    assert source == "default"
    # validate_global returns the entire enabled subset; downstream
    # resolve_for_target() narrows per target.
    assert resolved == ["auto-debian12-arm"]


def test_validate_global_partial_match_ok(tmp_path):
    """Some containers in scope, some out: passes (per-target filter handles them)."""
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    resolved, source = cr.validate_global(
        catalog, "rocky9-arm64, debian12-arm64", None,
        scope_families={"rpm"}, scope_arches={"arm64"},
    )
    # rocky9-arm64 is in (rpm,arm64); debian12-arm64 is in (deb,arm64).
    # With scope={rpm}, only rocky9 is in_scope_anywhere; debian12 is not.
    # But in_scope_anywhere is true (rocky9 matches), so validate_global passes.
    assert source == "cli"
    assert set(resolved) == {"auto-rocky9-arm", "auto-debian12-arm"}


def test_resolve_for_target_concrete_arch_filters_out_other_family(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    effective, oos, source = cr.resolve_for_target(
        catalog, "rocky9-arm64, debian12-arm64", None,
        target_family="rpm", target_arch="arm64",
    )
    assert effective == ["auto-rocky9-arm"]
    assert oos == ["auto-debian12-arm"]
    assert source == "cli"


def test_resolve_for_target_concrete_arch_filters_out_other_arch(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    effective, oos, source = cr.resolve_for_target(
        catalog, "debian12-arm64, debian13-amd64", None,
        target_family="deb", target_arch="arm64",
    )
    assert effective == ["auto-debian12-arm"]
    assert oos == ["auto-debian13-amd"]
    assert source == "cli"


def test_resolve_for_target_no_arch_filter_returns_both_arches(tmp_path):
    """target_arch=None means 'no arch filter' — both arm64 and amd64
    entries of the target family must land in `effective`."""
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    effective, oos, source = cr.resolve_for_target(
        catalog, "debian12-arm64, debian13-amd64", None,
        target_family="deb", target_arch=None,
    )
    assert set(effective) == {"auto-debian12-arm", "auto-debian13-amd"}
    assert oos == []
    assert source == "cli"


def test_resolve_for_target_default_path_uses_enabled_subset(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    effective, oos, source = cr.resolve_for_target(
        catalog, None, None,
        target_family="rpm", target_arch="arm64",
    )
    # Default catalog: rocky9-arm enabled:true, alma9-amd enabled:false.
    # Target rpm/arm64 → only rocky9.
    assert effective == ["auto-rocky9-arm"]
    assert source == "default"
    # On default path, deb-family enabled entries also get filtered out per
    # target — but the resolver's job is to filter, not to label them as
    # "out-of-scope overrides". oos is therefore populated; callers should
    # treat it as informational only on the default path (the CLI dispatcher
    # suppresses the per-entry out-of-scope log on default path).
    assert "auto-debian12-arm" in oos


def test_resolve_for_target_override_can_select_disabled_container(tmp_path):
    """An explicit override may select a container with enabled:false."""
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    effective, oos, source = cr.resolve_for_target(
        catalog, "alma9-amd64", None,                # enabled:false
        target_family="rpm", target_arch="amd64",
    )
    assert effective == ["auto-alma9-amd"]
    assert source == "cli"


def test_list_containers_table_includes_required_columns(tmp_path):
    p = _minimal_valid_catalog(tmp_path)
    catalog = cr.load_catalog(p)
    table = cr.list_containers(catalog)
    # Header presence
    for col in ("ALIAS", "CANONICAL NAME", "FAMILY", "ARCH", "ENABLED", "DESCRIPTION"):
        assert col in table
    # Each entry's alias + canonical appears
    assert "rocky9-arm64" in table
    assert "auto-rocky9-arm" in table
    # Family is user-facing (rpm/deb, not rhel)
    assert "rpm" in table
    assert "deb" in table
    assert "rhel" not in table


import subprocess


def _run_cli(*args, env_extra=None, cwd=None):
    """Invoke the resolver module as a script. Returns CompletedProcess."""
    env = dict(__import__("os").environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["python3", "utillities/container_resolver.py", *args],
        capture_output=True, text=True, env=env, cwd=cwd,
    )


def test_cli_list_containers_against_real_catalog():
    cp = _run_cli("list-containers")
    assert cp.returncode == 0
    # Real catalog has 17 entries
    body_lines = [l for l in cp.stdout.splitlines() if l and "ALIAS" not in l]
    assert len(body_lines) == 17


def test_cli_validate_global_default_emits_no_override_chatter():
    cp = _run_cli(
        "validate-global",
        "--scope-families", "rpm,deb",
        "--scope-arches", "arm64,amd64",
    )
    assert cp.returncode == 0
    # Default path: stdout is the enabled-subset CSV; stderr is intentionally
    # silent on [container-override] so logs stay byte-identical to pre-v2.2.
    assert "[container-override]" not in cp.stderr
    # Non-empty CSV
    assert cp.stdout.strip()


def test_cli_validate_global_override_in_scope_returns_csv(tmp_path):
    cp = _run_cli(
        "validate-global",
        "--containers", "rocky9-arm64, debian12-arm64",
        "--scope-families", "rpm,deb",
        "--scope-arches", "arm64",
    )
    assert cp.returncode == 0
    assert "source=cli" in cp.stderr
    assert "auto-rocky9-arm" in cp.stdout
    assert "auto-debian12-arm" in cp.stdout


def test_cli_validate_global_global_zero_exits_2():
    cp = _run_cli(
        "validate-global",
        "--containers", "debian12-arm64",
        "--scope-families", "rpm",
        "--scope-arches", "arm64",
    )
    assert cp.returncode == 2
    assert "out of scope" in cp.stderr


def test_cli_validate_global_unknown_token_exits_2():
    cp = _run_cli(
        "validate-global",
        "--containers", "not-a-real-thing-arm64",
        "--scope-families", "rpm,deb",
        "--scope-arches", "arm64,amd64",
    )
    assert cp.returncode == 2
    assert "Unknown container" in cp.stderr


def test_cli_resolve_for_target_emits_out_of_scope_log():
    cp = _run_cli(
        "resolve-for-target",
        "--containers", "rocky9-arm64, debian12-arm64",
        "--target-family", "rpm",
        "--target-arch", "arm64",
    )
    assert cp.returncode == 0
    assert "out-of-scope for this target" in cp.stderr
    assert "auto-rocky9-arm" in cp.stdout
    assert "auto-debian12-arm" not in cp.stdout  # filtered out


def test_cli_resolve_for_target_default_path_is_silent_on_override():
    """Default path (no --containers) must not emit [container-override] lines
    so default-path runs look like pre-v2.2 runs in the logs."""
    cp = _run_cli(
        "resolve-for-target",
        "--target-family", "rpm",
        "--target-arch", "arm64",
    )
    assert cp.returncode == 0
    assert "[container-override]" not in cp.stderr


def test_cli_resolve_for_target_no_arch_filter_includes_both_arches():
    cp = _run_cli(
        "resolve-for-target",
        "--containers", "debian12-arm64, debian13-amd64",
        "--target-family", "deb",
        # --target-arch omitted -> empty string -> None (no arch filter)
    )
    assert cp.returncode == 0
    assert "auto-debian12-arm" in cp.stdout
    assert "auto-debian13-amd" in cp.stdout


def test_cli_env_override_used_when_cli_absent():
    cp = _run_cli(
        "validate-global",
        "--scope-families", "rpm",
        "--scope-arches", "arm64",
        env_extra={"PEP_CONTAINERS": "rocky9-arm64"},
    )
    assert cp.returncode == 0
    assert "source=env" in cp.stderr
    assert "auto-rocky9-arm" in cp.stdout


def test_load_catalog_enabled_missing_fails(tmp_path):
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9-arm", "alias": "rocky9-arm64", "description": "x"}
        # 'enabled' deliberately missing
    ])
    with pytest.raises(cr.ResolverError, match="enabled"):
        cr.load_catalog(p)


def test_load_catalog_enabled_string_value_fails(tmp_path):
    # JSON string "false" must NOT pass (truthy in Python, but not a bool).
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9-arm", "alias": "rocky9-arm64",
         "description": "x", "enabled": "false"}
    ])
    with pytest.raises(cr.ResolverError, match="enabled.*bool"):
        cr.load_catalog(p)


def test_load_catalog_enabled_numeric_value_fails(tmp_path):
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9-arm", "alias": "rocky9-arm64",
         "description": "x", "enabled": 1}
    ])
    with pytest.raises(cr.ResolverError, match="enabled.*bool"):
        cr.load_catalog(p)


def test_load_catalog_description_missing_fails(tmp_path):
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9-arm", "alias": "rocky9-arm64", "enabled": True}
        # 'description' deliberately missing
    ])
    with pytest.raises(cr.ResolverError, match="description"):
        cr.load_catalog(p)


def test_load_catalog_description_non_string_fails(tmp_path):
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9-arm", "alias": "rocky9-arm64",
         "description": 123, "enabled": True}
    ])
    with pytest.raises(cr.ResolverError, match="description.*string"):
        cr.load_catalog(p)


def test_load_catalog_duplicate_canonical_name_fails(tmp_path):
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9-arm", "alias": "rocky9-arm64",
         "description": "first", "enabled": True},
        {"name": "auto-rocky9-arm", "alias": "alma9-arm64",
         "description": "second", "enabled": True},
    ])
    with pytest.raises(cr.ResolverError, match="duplicate name"):
        cr.load_catalog(p)


def test_load_catalog_duplicate_name_case_insensitive_fails(tmp_path):
    # The lookup index is lowercased, so a case-only difference is still a
    # collision in the override-resolution namespace.
    p = _write_catalog(tmp_path,
        rhel=[
            {"name": "auto-rocky9-arm", "alias": "rocky9-arm64",
             "description": "x", "enabled": True}
        ],
        deb=[
            {"name": "AUTO-rocky9-arm", "alias": "differentalias-arm64",
             "description": "y", "enabled": True}
        ])
    with pytest.raises(cr.ResolverError, match="duplicate name"):
        cr.load_catalog(p)


def test_load_catalog_missing_rhel_block_fails(tmp_path):
    # 'rhel' key absent entirely.
    p = tmp_path / "containers_list.json"
    p.write_text(json.dumps({"deb": []}))
    with pytest.raises(cr.ResolverError, match="rhel.*block"):
        cr.load_catalog(p)


def test_load_catalog_rhel_not_a_list_fails(tmp_path):
    p = tmp_path / "containers_list.json"
    p.write_text(json.dumps({"rhel": "oops", "deb": []}))
    with pytest.raises(cr.ResolverError, match="rhel.*list"):
        cr.load_catalog(p)


def test_load_catalog_name_missing_key_fails(tmp_path):
    p = _write_catalog(tmp_path, rhel=[
        {"alias": "rocky9-arm64", "description": "x", "enabled": True}
        # 'name' key absent
    ])
    with pytest.raises(cr.ResolverError, match="name"):
        cr.load_catalog(p)


def test_load_catalog_name_non_string_fails(tmp_path):
    p = _write_catalog(tmp_path, rhel=[
        {"name": 123, "alias": "rocky9-arm64",
         "description": "x", "enabled": True}
    ])
    with pytest.raises(cr.ResolverError, match="name must be a string"):
        cr.load_catalog(p)


def test_load_catalog_alias_missing_key_fails(tmp_path):
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9-arm", "description": "x", "enabled": True}
        # 'alias' key absent — distinct from the existing "empty alias" test
    ])
    with pytest.raises(cr.ResolverError, match="alias missing"):
        cr.load_catalog(p)


def test_load_catalog_alias_non_string_fails(tmp_path):
    p = _write_catalog(tmp_path, rhel=[
        {"name": "auto-rocky9-arm", "alias": 123,
         "description": "x", "enabled": True}
    ])
    with pytest.raises(cr.ResolverError, match="alias must be a string"):
        cr.load_catalog(p)
