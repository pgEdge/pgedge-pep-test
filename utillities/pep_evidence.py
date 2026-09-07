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

# pep_identity is loaded by path (mirrors the pep_verify load above) so the
# observed-identity writer can record the PARSED binary token via the same
# parser identity verification uses -- never a re-implementation, never raw output.
_pid_spec = _ilu.spec_from_file_location(
    "pep_identity", str(_Path(__file__).with_name("pep_identity.py")))
_pid = _ilu.module_from_spec(_pid_spec)
_pid_spec.loader.exec_module(_pid)


def record_identity_verdict(observed, request, out_path, *, binary_missing=False,
                            run_token=None, observed_out=None):
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
    # Audit-only observed identity values, written to a SEPARATE file BEFORE problems
    # are computed (same durability guarantee as the strict evidence above), so a
    # failing assertion never loses the actual package/binary that was observed.
    # This file NEVER feeds ev/problems -- it is written from `observed`, which was
    # already frozen into `ev` on the line above.
    if observed_out is not None:
        write_observed_identity(observed, request, run_token or "", observed_out)
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


# --- Audit-only observed identity (SEPARATE from strict identity-evidence.json) ---
# Records the observed identity values gathered during a FULL integration identity
# run so the exact package/binary that was installed is auditable -- including L1-only
# runs where no expected_* pins were supplied. Its contents are purely for audit:
# they never influence the verdict, execution status, or rung calculation, and
# identity-evidence.json stays exactly {l2a,l2b,l1}.


def build_observed_identity(observed, request, run_token):
    """Build the audit-only observed-identity object, bound to this run + target.

    Records exactly the three values identity verification consumes, and nothing
    else: the exact package-manager identity (the L2a input), the PARSED binary
    version token (via the same parser L2b uses -- not the raw command output),
    and the value used for the L1 component comparison. Reuses `_target_of` so the
    complete target scope matches install-evidence.json rather than duplicating a
    partial copy."""
    fam = request["family"]
    pm_identity = observed.get("rpm") if fam == "rpm" else observed.get("deb")
    raw_binary = observed.get("binary")
    return {
        "run_token": run_token,
        "target": _target_of(request),
        "observed": {
            # exact package-manager identity string (rpm NVR / deb Version) -- the
            # same observation L2a compares; not arbitrary command output.
            "package_manager_version": pm_identity,
            # parsed binary version token via pep_identity.parse_binary_version --
            # the same token L2b compares; None if absent or unparseable.
            "binary_version": _pid.parse_binary_version(raw_binary) if raw_binary is not None else None,
            # the value L1's component_version_matches() is run against.
            "component_version": observed.get("component_version"),
        },
    }


def write_observed_identity(observed, request, run_token, out_path):
    """Build + persist the audit-only observed-identity file; returns the object."""
    obj = build_observed_identity(observed, request, run_token)
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
