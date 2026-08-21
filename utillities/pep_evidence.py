"""Persist per-rung identity evidence and derive the pass/fail verdict for a PEP
integration run. Pure + Docker-free: the container observations are gathered by
the caller (the component test) and passed in as `observed`; this module only
asserts identity (via pep_verify), writes the evidence file BEFORE returning any
verdict so a later failing assertion can never lose it, and returns the list of
problems (empty == identity proven for this request). Stdlib only (imports
pep_verify by path)."""
from __future__ import annotations
import json as _json
import importlib.util as _ilu
from pathlib import Path as _Path

_pv_spec = _ilu.spec_from_file_location(
    "pep_verify", str(_Path(__file__).with_name("pep_verify.py")))
_pv = _ilu.module_from_spec(_pv_spec)
_pv_spec.loader.exec_module(_pv)


def record_identity_verdict(observed, request, out_path, *, binary_missing=False):
    """Assert per-rung identity, PERSIST it, and return (evidence, problems).

    evidence is the {l2a, l2b, l1} dict from pep_verify.assert_identity. problems
    is a list of human-readable failure reasons; an empty list means identity is
    proven for this request. The evidence is written to out_path BEFORE problems
    are computed, so a caller that fails on a non-empty problems list can never
    lose the evidence.

    A rung only causes a problem when the request declared it attemptable_now:
      * a missing binary observation the run actually needs (l2b attemptable) is a
        harness/request configuration fault, surfaced explicitly rather than as a
        silent skip;
      * an attemptable l2a/l2b that is not_proven fails;
      * l1 must always be proven in integration mode (a missing component-version
        observation leaves l1 not_attempted, which is a failure here).
    """
    ev = _pv.assert_identity(observed, request, request["package_name"])
    p = _Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(ev))
    now = request["attemptable_now"]
    problems = []
    if binary_missing and now["l2b"]:
        problems.append(
            f"harness/request misconfig: binary path missing but l2b attemptable (evidence={ev})")
    failed = [r for r in ("l2a", "l2b") if now[r] and ev[r] == "not_proven"]
    if failed:
        problems.append(f"identity not proven for attemptable rung(s): {failed} (evidence={ev})")
    if ev["l1"] != "proven":
        problems.append(f"component-version match failed: {ev['l1']} (evidence={ev})")
    return ev, problems
