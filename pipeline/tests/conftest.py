"""Test discovery for the integration and release-certification suites.

The modules under test live one level up, in pipeline/. The shared integration helpers they reuse
(pep_request, pep_verify, ...) and the component policy live in aspects/pipeline/; container_resolver
and ci_consolidated_report stay in utillities/. None of these directories is a package, so put all
three on sys.path, as running a module by path does for its own directory.
"""
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _d in (_REPO / "pipeline", _REPO / "aspects" / "pipeline", _REPO / "utillities"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
