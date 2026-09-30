"""The single PEP-owned component/package policy.

`utillities/pep_capture_policy.json` (schema pep-capture-policy/1) is the ONE authority for
which runtime package names belong to each logical component. These tests prove the three
consumers -- release capture (pep_capture_io.resolve_component_policy), invocation planning
(pep_invocation_plan) and per-run request validation (pep_request.normalize_request) -- all
agree on the SAME mapping, that adding/retiring a mapping is a localized JSON change, and that
a missing/malformed policy fails clearly.

Stdlib only. Runs in the exact PEP Self-Test unit selection via the test_pep_*.py glob.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

# Plain imports (pytest puts utillities/ on sys.path) reuse the SAME module singletons the rest
# of the suite uses -- importantly, pep_invocation_plan binds pep_request.COMPONENT_PACKAGES by
# identity, so we must not re-exec/clobber those modules here.
import pep_request as pr           # noqa: E402
import pep_capture_io as IO        # noqa: E402
import pep_invocation_plan as P    # noqa: E402

_HERE = Path(__file__).resolve().parent
_POLICY = _HERE / "pep_capture_policy.json"


# --------------------------------------------------------------------------- #
# one source: request registry is derived from the policy file
# --------------------------------------------------------------------------- #
def test_request_registry_is_derived_from_the_policy_file():
    doc = json.loads(_POLICY.read_text(encoding="utf-8"))
    want = {c: tuple(e["allowed_runtime_package_names"]) for c, e in doc["components"].items()}
    assert pr.COMPONENT_PACKAGES == want


def test_policy_defines_both_rag_and_smoke():
    # rag retains canonical-first order; the internal synthetic fixture component is present
    assert pr.COMPONENT_PACKAGES["rag"] == ("pgedge-rag-server2", "pgedge-rag-server")
    assert pr.COMPONENT_PACKAGES["pgedge-capture-smoke"] == ("pgedge-capture-smoke",)


def test_planner_shares_the_request_object_not_a_copy():
    # the planner reuses the SAME in-memory mapping -> one authority, never a second registry
    assert P.COMPONENT_PACKAGES is pr.COMPONENT_PACKAGES


def test_capture_and_request_agree_for_every_component():
    # capture resolves each component from the SAME file; accepted package sets must match
    for comp, packages in pr.COMPONENT_PACKAGES.items():
        pol = IO.resolve_component_policy(str(_POLICY), comp)
        assert tuple(pol["allowed_runtime_package_names"]) == packages, comp


# --------------------------------------------------------------------------- #
# request-validation semantics preserved on the shared mapping
# --------------------------------------------------------------------------- #
def _req(**over):
    raw = {
        "component": "rag", "package_name": "pgedge-rag-server", "channel": "daily",
        "expected_version": "1.0.0", "family": "rpm", "arch": "amd64", "pg_major": "17",
        "container_alias": "rocky9-amd64",
    }
    raw.update(over)
    return raw


def test_request_accepts_rag_active_and_predecessor():
    for pkg in ("pgedge-rag-server2", "pgedge-rag-server"):
        req = pr.normalize_request(_req(package_name=pkg, expected_version="2.0.0"))
        assert req["component"] == "rag" and req["package_name"] == pkg


def test_request_accepts_smoke_component_end_to_end():
    # the coordinator's synthetic legs use component=pgedge-capture-smoke; the request layer
    # (which the legs run through normalize_request) must now accept it.
    req = pr.normalize_request(_req(component="pgedge-capture-smoke",
                                    package_name="pgedge-capture-smoke"))
    assert req["component"] == "pgedge-capture-smoke"
    assert req["package_name"] == "pgedge-capture-smoke"


def test_unrelated_and_unknown_pairs_still_rejected():
    with pytest.raises(pr.RequestError):                       # unknown component
        pr.normalize_request(_req(component="mcp", package_name="pgedge-postgres-mcp"))
    with pytest.raises(pr.RequestError):                       # rag package on smoke component
        pr.normalize_request(_req(component="pgedge-capture-smoke",
                                  package_name="pgedge-rag-server"))
    with pytest.raises(pr.RequestError):                       # smoke package on rag component
        pr.normalize_request(_req(package_name="pgedge-capture-smoke"))


# --------------------------------------------------------------------------- #
# malformed / unknown policy fails clearly (fail closed)
# --------------------------------------------------------------------------- #
def _write(tmp_path, obj):
    p = tmp_path / "policy.json"
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return str(p)


def test_malformed_or_unknown_policy_fails_clearly(tmp_path):
    with pytest.raises(pr.ComponentPolicyError):              # missing file
        pr.load_component_packages(str(tmp_path / "nope.json"))
    with pytest.raises(pr.ComponentPolicyError):              # not JSON
        pr.load_component_packages(_write(tmp_path, "{not json"))
    with pytest.raises(pr.ComponentPolicyError):              # wrong schema
        pr.load_component_packages(_write(tmp_path, {"schema": "x", "components": {"c": {
            "allowed_runtime_package_names": ["p"]}}}))
    with pytest.raises(pr.ComponentPolicyError):              # no components
        pr.load_component_packages(_write(tmp_path, {"schema": "pep-capture-policy/1"}))
    with pytest.raises(pr.ComponentPolicyError):              # entry without package names
        pr.load_component_packages(_write(tmp_path, {"schema": "pep-capture-policy/1",
            "components": {"c": {}}}))
    with pytest.raises(pr.ComponentPolicyError):              # blank package name
        pr.load_component_packages(_write(tmp_path, {"schema": "pep-capture-policy/1",
            "components": {"c": {"allowed_runtime_package_names": [" "]}}}))
    with pytest.raises(pr.ComponentPolicyError):              # duplicate package names
        pr.load_component_packages(_write(tmp_path, {"schema": "pep-capture-policy/1",
            "components": {"c": {"allowed_runtime_package_names": ["p", "p"]}}}))


def test_wellformed_custom_policy_loads_and_preserves_order(tmp_path):
    ok = _write(tmp_path, {"schema": "pep-capture-policy/1", "components": {
        "c": {"allowed_runtime_package_names": ["a", "b"], "expected_binary_version": "9.9"}}})
    assert pr.load_component_packages(ok) == {"c": ("a", "b")}


# --------------------------------------------------------------------------- #
# existing capture-smoke behavior intact
# --------------------------------------------------------------------------- #
def test_capture_smoke_component_still_resolves():
    pol = IO.resolve_component_policy(str(_POLICY), "pgedge-capture-smoke")
    assert pol["allowed_runtime_package_names"] == ["pgedge-capture-smoke"]
    assert pol.get("expected_binary_version") == ""


def test_capture_resolves_rag_from_the_same_file():
    pol = IO.resolve_component_policy(str(_POLICY), "rag")
    assert pol["allowed_runtime_package_names"] == ["pgedge-rag-server2", "pgedge-rag-server"]


# --------------------------------------------------------------------------- #
# durable: the shared pep_request object is import-ORDER independent
# (does NOT rely on pytest collecting files alphabetically)
# --------------------------------------------------------------------------- #
_ORDER_CHECK = """
import importlib, sys
order = {order!r}
first = None
for name in order:
    importlib.import_module(name)
    cur = id(sys.modules["pep_request"])
    if first is None:
        first = cur
    elif cur != first:
        raise SystemExit("pep_request was REPLACED after importing " + name)
import pep_invocation_plan as P
import pep_request as R
assert P.COMPONENT_PACKAGES is R.COMPONENT_PACKAGES, "planner COMPONENT_PACKAGES is not the request object"
assert P.VALID_CHANNELS is R.VALID_CHANNELS, "planner VALID_CHANNELS is not the request object"
print("OK")
"""

# Every known pep_request loader, in several orders -- including the two the suite previously
# depended on NOT happening: a clobbering loader (pep_request / pep_resolve_cli) imported BEFORE
# the planner. Each runs in a FRESH interpreter, so module identity cannot come from collection order.
_LOADER_ORDERS = [
    ["pep_request", "pep_resolve_cli", "pep_invocation_plan"],
    ["pep_resolve_cli", "pep_invocation_plan", "pep_request", "pep_request_env"],
    ["pep_invocation_plan", "pep_request", "pep_resolve_cli", "pep_request_env"],
    ["pep_request_env", "pep_invocation_plan", "pep_resolve_cli", "pep_request"],
]


@pytest.mark.parametrize("order", _LOADER_ORDERS,
                         ids=lambda o: "-".join(m.split("_")[-1] for m in o))
def test_pep_request_singleton_is_import_order_independent(order):
    proc = subprocess.run([sys.executable, "-c", _ORDER_CHECK.format(order=order)],
                          cwd=str(_HERE), capture_output=True, text=True)
    assert proc.returncode == 0, (
        "import order %r replaced pep_request or broke the shared object:\n"
        "STDOUT: %s\nSTDERR: %s" % (order, proc.stdout, proc.stderr))
    assert proc.stdout.strip().splitlines()[-1] == "OK", proc.stdout
