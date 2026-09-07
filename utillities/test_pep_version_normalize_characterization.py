"""Characterization test pinning the CURRENT behavior of
aspects.package_management.normalize_version, taken BEFORE extraction.

This guards against silently changing behavior while extracting the logic
into utillities/pep_version_normalize.py (L1). Imports the current module
by path shim (no package __init__ in aspects/ or utillities/).
"""
import importlib.util
import sys
from pathlib import Path

_p = Path(__file__).resolve().parent.parent / "aspects" / "package_management.py"
_spec = importlib.util.spec_from_file_location("pm_char", str(_p))
pm = importlib.util.module_from_spec(_spec)
sys.modules["pm_char"] = pm
_spec.loader.exec_module(pm)

CASES = [
    ("1.0.0-1.el9", "pgedge-rag-server", "1.0.0"),
    ("16.11-1.bullseye", "pgedge-postgresql-16", "16.11.0"),  # pads to 3 parts
    ("1.0.0-beta2", "pgedge-rag-server", "1.0.0.beta2"),
    ("1.0.0~beta2-1.trixie", "pgedge-rag-server", "1.0.0.beta2"),  # deb tilde pre-release, via the wrapper
    ("1.0.0", "pgedge-lolor", "1.0.0"),
]


def test_current_normalize_version_snapshot():
    for raw, pkg, expected in CASES:
        assert pm.normalize_version(raw, pkg) == expected
