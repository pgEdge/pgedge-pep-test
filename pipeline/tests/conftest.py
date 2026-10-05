"""Test discovery for the release-certification suites.

The modules under test live one level up, in pipeline/. The bridge and shared helpers they reuse
(pep_request, pep_verify, pep_result_summary, container_resolver, ci_consolidated_report) and the
component policy stay in utillities/. Neither directory is a package, so put both on sys.path, as
running a module by path does for its own directory.
"""
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _d in (_REPO / "pipeline", _REPO / "utillities"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
