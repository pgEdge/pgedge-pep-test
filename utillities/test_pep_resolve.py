import importlib.util, sys
from pathlib import Path
_spec = importlib.util.spec_from_file_location(
    "pep_resolve", str(Path(__file__).parent / "pep_resolve.py"))
rz = importlib.util.module_from_spec(_spec); sys.modules["pep_resolve"] = rz
_spec.loader.exec_module(rz)


def test_caller_wins_over_config():
    out = rz.resolve(["repo"], caller={"repo": "daily"}, config_pg={"repo": "release"})
    assert out["repo"] == {"value": "daily", "source": "caller"}


def test_standalone_no_caller_uses_config_then_dotenv():
    # No caller layer must NEVER surface as 'caller' (standalone precedence intact).
    out = rz.resolve(["repo"], caller={}, config_pg={"repo": "release"})
    assert out["repo"] == {"value": "release", "source": "config_pg"}
    out2 = rz.resolve(["repo"], caller={}, config_pg={}, dotenv={"repo": "staging"})
    assert out2["repo"] == {"value": "staging", "source": "dotenv"}


def test_default_fallback():
    out = rz.resolve(["scenario"], defaults={"scenario": "certification"})
    assert out["scenario"] == {"value": "certification", "source": "defaults"}


def test_empty_string_is_skipped_not_selected():
    out = rz.resolve(["repo"], caller={"repo": ""}, config_pg={"repo": "release"})
    assert out["repo"] == {"value": "release", "source": "config_pg"}


def test_unset_key_has_null_value_and_source():
    out = rz.resolve(["repo"])
    assert out["repo"] == {"value": None, "source": None}


def test_multiple_keys_resolve_independently():
    out = rz.resolve(["repo", "scenario"],
                     caller={"repo": "daily"},
                     defaults={"scenario": "certification"})
    assert out["repo"] == {"value": "daily", "source": "caller"}
    assert out["scenario"] == {"value": "certification", "source": "defaults"}
