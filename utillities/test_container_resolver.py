"""Unit tests for utillities.container_resolver — synthetic-catalog only."""
import json
from pathlib import Path

import pytest

import utillities.container_resolver as cr


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
