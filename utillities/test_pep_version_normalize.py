"""Unit tests for utillities.pep_version_normalize."""
import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "pep_version_normalize", str(Path(__file__).parent / "pep_version_normalize.py"))
nz = importlib.util.module_from_spec(_spec)
sys.modules["pep_version_normalize"] = nz
_spec.loader.exec_module(nz)


def test_strips_rpm_dist():           assert nz.normalize_version("1.0.0-1.el9", "pgedge-rag-server") == "1.0.0"
def test_pads_deb_two_part():         assert nz.normalize_version("16.11-1.bullseye", "pgedge-postgresql-16") == "16.11.0"
def test_beta_preserved():            assert nz.normalize_version("1.0.0-beta2", "pgedge-rag-server") == "1.0.0.beta2"
def test_plain_non_beta():            assert nz.normalize_version("1.0.0", "pgedge-lolor") == "1.0.0"
