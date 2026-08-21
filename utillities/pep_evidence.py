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


# --- Task 9: install-before-identity scope marker (kept SEPARATE from the strict
# identity-evidence.json, which must stay exactly {l2a,l2b,l1}) ---------------------


def _target_of(request):
    return {
        "package_name": request["package_name"],
        "family": request["family"],
        "arch": request.get("arch"),
        "pg_major": request.get("pg_major"),
        "container_alias": request.get("container_alias"),
        "channel": request.get("channel"),
    }


def build_install_evidence(request, run_token, install_kind, install_token):
    """Scope marker written after a SUCCESSFUL integration install. Bound to this
    run (run_token) and this target so a stale or unrelated file cannot satisfy the
    install-before-identity precondition. This is NOT the identity file — it carries
    the run/target metadata that identity-evidence.json is forbidden to hold."""
    return {
        "run_token": run_token,
        "installed": True,
        "install_kind": install_kind,       # "pinned" | "latest"
        "install_token": install_token,     # the exact pinned token, or None for latest
        "target": _target_of(request),
        "expected_version": request["expected_version"],
    }


def write_install_evidence(request, run_token, install_kind, install_token, out_path):
    """Build + persist the install scope marker; returns the written object."""
    obj = build_install_evidence(request, run_token, install_kind, install_token)
    p = _Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(obj))
    return obj


def load_json_object(path):
    """Return the parsed JSON object, or None if absent/unreadable/not an object."""
    try:
        data = _json.loads(_Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def install_precondition_problems(install_evidence, request, run_token):
    """Return [] if the install marker proves THIS run installed THIS target,
    otherwise a list of reasons (empty run token / absent / not-installed / stale
    run_token / target or expected_version mismatch). Pure + Docker-free.

    A non-empty current run token is REQUIRED: without it (e.g. a direct
    integration-mode invocation that bypassed the bridge's RESET) an empty marker
    token would compare equal to an empty current token and defeat the staleness
    check, so we fail safe."""
    if not run_token:
        return ["PEP_RUN_TOKEN is empty or unset (integration runs must set a "
                "non-empty per-run token; bypassing the bridge is not supported)"]
    if install_evidence is None:
        return ["install evidence absent (no successful install recorded this run)"]
    problems = []
    if not install_evidence.get("installed"):
        problems.append("install evidence does not record a successful install")
    if install_evidence.get("run_token") != run_token:
        problems.append(
            f"install evidence run_token {install_evidence.get('run_token')!r} != "
            f"current {run_token!r} (stale evidence from another run)")
    if install_evidence.get("target") != _target_of(request):
        problems.append(
            f"install evidence target {install_evidence.get('target')} != "
            f"current request target {_target_of(request)}")
    if install_evidence.get("expected_version") != request["expected_version"]:
        problems.append(
            f"install evidence expected_version {install_evidence.get('expected_version')!r} != "
            f"current {request['expected_version']!r}")
    return problems


def record_precondition_failure(request, out_path):
    """Persist a STRICT {l2a,l2b,l1} identity-evidence object for a FAILED
    install-before-identity precondition, so a genuine install failure is reported
    as a truthful completed/fail (evidence present, strict schema) rather than a
    masked infra_failure from an absent identity file.

    Since identity was NEVER queried here, the evidence is exactly what
    assert_identity yields from ZERO observations -- attemptable l2a/l2b ->
    'not_proven' (declared attemptable, not proven this run); l1 -> 'not_attempted'
    (no component version observed) -- consistent with assert_identity's
    missing-observation semantics. The JUnit failure is carried by the caller's
    pytest.fail, NOT by the evidence content. Returns ev."""
    ev = _pv.assert_identity({}, request, request["package_name"])
    p = _Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(ev))
    return ev
