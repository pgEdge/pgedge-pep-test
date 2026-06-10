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
