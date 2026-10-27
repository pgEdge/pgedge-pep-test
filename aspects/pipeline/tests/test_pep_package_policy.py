"""Unit tests for the shared package-matching rules (aspects/pipeline/pep_package_policy.py).

Request validation, cert-plan target selection, the planner backstop and the component tests all
call these functions, so this file pins the rules themselves. Stdlib + pytest only.
"""
import importlib.util
import json
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("pep_package_policy_under_test",
                                               str(_HERE.parent / "pep_package_policy.py"))
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

SPOCK = json.loads((_HERE.parent / "pep_capture_policy.json").read_text())["components"]["spock"]
RAG = {"allowed_runtime_package_names": ["pgedge-rag-server2", "pgedge-rag-server"], "expected_binary_version": ""}


@pytest.mark.parametrize("family,pg,version,want", [
    ("rpm", "17", "5.0.13", ("pgedge-spock50_17",)),
    ("deb", "17", "5.0.13", ("pgedge-postgresql-17-spock50",)),
    ("rpm", "16", "5.1.0", ("pgedge-spock50_16",)),            # series is the major, even for 5.1
    ("rpm", "18", "6.0.0", ("pgedge-spock60_18",)),
    ("deb", "16", "6.0.0", ("pgedge-postgresql-16-spock60",)),
    ("rpm", 17, "5.0.13", ("pgedge-spock50_17",)),              # an int PG major is canonicalised
    ("rpm", "17", "7.0.0", ()),                                 # unknown series -> nothing expected
    ("rpm", "017", "5.0.13", ()),                               # non-canonical PG major
    ("rpm", None, "5.0.13", ()),
    ("apk", "17", "5.0.13", ()),
])
def test_expected_names(family, pg, version, want):
    assert pp.expected_names(SPOCK, family, pg, version) == want


def test_flat_entry_is_family_and_pg_independent():
    assert pp.expected_names(RAG, "deb", None, None) == ("pgedge-rag-server2", "pgedge-rag-server")
    assert pp.classify(RAG, "pgedge-rag-server", "rpm", "17", "9.9.9") is None
    assert pp.classify(RAG, "pgedge-spock50_17", "rpm", "17", "5.0.13") == pp.R_NOT_ALLOWED
    assert pp.describe(RAG) is None and not pp.is_coupled(RAG)


@pytest.mark.parametrize("name,family,pg,version,want", [
    ("pgedge-spock50_17", "rpm", "17", "5.0.13", None),
    ("pgedge-postgresql-17-spock50", "deb", "17", "5.0.13", None),
    ("pgedge-postgresql-17-spock50", "rpm", "17", "5.0.13", pp.R_FAMILY),
    ("pgedge-spock50_17", "deb", "17", "5.0.13", pp.R_FAMILY),
    ("pgedge-spock50_16", "rpm", "17", "5.0.13", pp.R_PG),
    ("pgedge-spock60_17", "rpm", "17", "5.0.13", pp.R_SERIES),
    ("pgedge-spock50_17", "rpm", "17", "6.0.0", pp.R_SERIES),
    ("pgedge-spock50_17", "rpm", None, "5.0.13", pp.R_NO_PG),
    ("pgedge-spock50_17", "rpm", "17", "7.0.0", pp.R_NO_SERIES),
    ("pgedge-spock50_17", "rpm", "17", "", pp.R_NO_SERIES),
    ("pgedge-spock50_17-llvmjit", "rpm", "17", "5.0.13", pp.R_UNCERTIFIED),
    ("pgedge-spock50_16-llvmjit", "rpm", "17", "5.0.13", pp.R_PG),
    ("pgedge-spock60_17-llvmjit", "rpm", "17", "5.0.13", pp.R_SERIES),
    ("pgedge-spock50_17-debuginfo", "rpm", "17", "5.0.13", pp.R_NOT_ALLOWED),
    ("pgedge-postgresql-17-spock50-dbgsym", "deb", "17", "5.0.13", pp.R_NOT_ALLOWED),
    ("pgedge-postgresql17-server", "rpm", "17", "5.0.13", pp.R_NOT_ALLOWED),
    (None, "rpm", "17", "5.0.13", pp.R_NOT_ALLOWED),
])
def test_classify_spock(name, family, pg, version, want):
    assert pp.classify(SPOCK, name, family, pg, version) == want


def test_template_metacharacters_are_literal():
    entry = {"pg_coupled": True, "runtime_package_templates": {"rpm": ["a.b+{pg}"]}}
    assert pp.validate_entry(entry) == []
    assert pp.classify(entry, "a.b+17", "rpm", "17", "1.0") is None
    assert pp.classify(entry, "axb+17", "rpm", "17", "1.0") == pp.R_NOT_ALLOWED


def test_is_component_runtime_package_matches_any_pg_series_or_family_but_not_companions():
    for n in ("pgedge-spock50_17", "pgedge-spock60_16", "pgedge-postgresql-18-spock50"):
        assert pp.is_component_runtime_package(SPOCK, n), n
    for n in ("pgedge-spock50_17-llvmjit", "pgedge-spock50_17-debuginfo", "pgedge-postgresql-17", None):
        assert not pp.is_component_runtime_package(SPOCK, n), n
    assert pp.is_component_runtime_package(RAG, "pgedge-rag-server")
    assert not pp.is_component_runtime_package(RAG, "pgedge-spock50_17")


def test_series_for_version():
    assert [pp.series_for_version(SPOCK, v) for v in ("5.0.13", "5.1.0", "6.0.0", "6", "7.0", None)] == \
        ["50", "50", "60", None, None, None]
    assert pp.series_for_version(RAG, "5.0.13") is None


def test_describe_records_the_coupled_policy():
    assert pp.describe(SPOCK) == {k: SPOCK[k] for k in (
        "pg_coupled", "runtime_package_templates", "series_by_version_major",
        "uncertified_runtime_package_templates", "uncertified_reason")}


def test_repository_policy_entries_are_valid():
    doc = json.loads((_HERE.parent / "pep_capture_policy.json").read_text())
    assert {c: pp.validate_entry(e) for c, e in doc["components"].items()} == {c: [] for c in doc["components"]}


@pytest.mark.parametrize("entry,fragment", [
    ("x", "not an object"),
    ({}, "allowed_runtime_package_names"),
    ({"allowed_runtime_package_names": ["a"], "expected_binary_version": 1}, "expected_binary_version"),
    ({"pg_coupled": False, "runtime_package_templates": {"rpm": ["p{pg}"]}}, "pg_coupled"),
    ({"pg_coupled": True}, "runtime_package_templates must be a non-empty object"),
    ({"pg_coupled": True, "runtime_package_templates": {"rpm": []}}, "non-empty list"),
    ({"pg_coupled": True, "runtime_package_templates": {"rpm": ["p{pg}", "p{pg}"]}}, "duplicate"),
    ({"pg_coupled": True, "runtime_package_templates": {"rpm": [" p{pg}"]}}, "surrounding whitespace"),
    ({"pg_coupled": True, "runtime_package_templates": {"rpm": ["p{PG}"]}}, "placeholder"),
    ({"pg_coupled": True, "runtime_package_templates": {"rpm": ["p{pg}{series}{series}"]},
      "series_by_version_major": {"5": "50"}}, "at most once"),
    ({"pg_coupled": True, "runtime_package_templates": {"rpm": ["p{pg}"]},
      "series_by_version_major": {"5": "50"}}, "no template uses {series}"),
    ({"pg_coupled": True, "runtime_package_templates": {"rpm": ["p{series}{pg}"]},
      "series_by_version_major": {"05": "50"}}, "series_by_version_major"),
])
def test_validate_entry_rejects(entry, fragment):
    errs = pp.validate_entry(entry)
    assert errs and any(fragment in e for e in errs), errs
