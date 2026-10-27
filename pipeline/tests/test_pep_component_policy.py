"""The single PEP-owned component/package policy.

`aspects/pipeline/pep_capture_policy.json` (schema pep-capture-policy/1) is the ONE authority for
which runtime package names belong to each logical component. These tests prove the three
consumers -- release capture (pep_capture_io.resolve_component_policy), invocation planning
(pep_invocation_plan) and per-run request validation (pep_request.normalize_request) -- all
agree on the SAME mapping, that adding/retiring a mapping is a localized JSON change, and that
a missing/malformed policy fails clearly.

Stdlib only. Runs in the exact PEP Self-Test unit selection (pipeline/tests/).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

# Plain imports (pytest puts utillities/ on sys.path) reuse the SAME module singletons the rest
# of the suite uses -- importantly, pep_invocation_plan binds pep_request.COMPONENT_PACKAGES (and
# COMPONENT_POLICY) by
# identity, so we must not re-exec/clobber those modules here.
import pep_request as pr           # noqa: E402
import pep_capture_io as IO        # noqa: E402
import pep_invocation_plan as P    # noqa: E402

_HERE = Path(__file__).resolve().parent
_HELPERS = _HERE.parents[1] / "aspects" / "pipeline"   # the shared request contract, its loaders and the policy
_POLICY = _HELPERS / "pep_capture_policy.json"


# --------------------------------------------------------------------------- #
# one source: request registry is derived from the policy file
# --------------------------------------------------------------------------- #
def test_request_registry_is_derived_from_the_policy_file():
    doc = json.loads(_POLICY.read_text(encoding="utf-8"))
    assert pr.COMPONENT_POLICY == doc["components"]
    # the flat name registry holds exactly the flat entries; PG-coupled entries have no fixed list
    want = {c: tuple(e["allowed_runtime_package_names"]) for c, e in doc["components"].items()
            if "allowed_runtime_package_names" in e}
    assert pr.COMPONENT_PACKAGES == want
    assert set(want) | {"spock"} == set(doc["components"])


def test_policy_defines_both_rag_and_smoke():
    # rag retains canonical-first order; the internal synthetic fixture component is present
    assert pr.COMPONENT_PACKAGES["rag"] == ("pgedge-rag-server2", "pgedge-rag-server")
    assert pr.COMPONENT_PACKAGES["pgedge-capture-smoke"] == ("pgedge-capture-smoke",)


def test_policy_defines_spock_as_one_pg_coupled_component():
    spock = pr.COMPONENT_POLICY["spock"]
    assert spock["pg_coupled"] is True
    assert spock["runtime_package_templates"] == {
        "rpm": ["pgedge-spock{series}_{pg}"], "deb": ["pgedge-postgresql-{pg}-spock{series}"]}
    assert spock["series_by_version_major"] == {"5": "50", "6": "60"}
    assert spock["uncertified_runtime_package_templates"] == {"rpm": ["pgedge-spock{series}_{pg}-llvmjit"]}
    assert spock["uncertified_reason"].strip()
    assert "spock" not in pr.COMPONENT_PACKAGES


def test_planner_shares_the_request_object_not_a_copy():
    # the planner reuses the SAME in-memory mapping -> one authority, never a second registry
    assert P.COMPONENT_PACKAGES is pr.COMPONENT_PACKAGES
    assert P.COMPONENT_POLICY is pr.COMPONENT_POLICY


def test_capture_and_request_agree_for_every_component():
    # capture resolves each component from the SAME file; accepted package sets must match
    for comp, packages in pr.COMPONENT_PACKAGES.items():
        pol = IO.resolve_component_policy(str(_POLICY), comp)
        assert tuple(pol["allowed_runtime_package_names"]) == packages, comp
    for comp, entry in pr.COMPONENT_POLICY.items():
        assert IO.resolve_component_policy(str(_POLICY), comp) == entry, comp


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


def _spock(**over):
    return _req(**dict({"component": "spock", "package_name": "pgedge-spock50_17",
                        "expected_version": "5.0.13"}, **over))


@pytest.mark.parametrize("family,alias,pkg,version,pg", [
    ("rpm", "rocky9-amd64", "pgedge-spock50_17", "5.0.13", "17"),
    ("deb", "debian13-amd64", "pgedge-postgresql-17-spock50", "5.0.13", "17"),
    ("rpm", "rocky9-amd64", "pgedge-spock60_18", "6.0.0", "18"),
    ("deb", "debian13-amd64", "pgedge-postgresql-16-spock60", "6.0.0", "16"),
], ids=["rpm-v5", "deb-v5", "rpm-v6", "deb-v6"])
def test_request_accepts_spock_package_for_its_family_pg_and_series(family, alias, pkg, version, pg):
    req = pr.normalize_request(_spock(family=family, container_alias=alias, package_name=pkg,
                                      expected_version=version, pg_major=pg))
    assert (req["component"], req["package_name"], req["family"]) == ("spock", pkg, family)


@pytest.mark.parametrize("over,why", [
    ({"package_name": "pgedge-postgresql-17-spock50"}, "package_family_mismatch"),
    ({"package_name": "pgedge-spock50_16"}, "pg_major_mismatch"),
    ({"pg_major": "18"}, "pg_major_mismatch"),
    ({"package_name": "pgedge-spock60_17"}, "series_mismatch"),
    ({"expected_version": "6.0.0"}, "series_mismatch"),
    ({"expected_version": "7.0.0"}, "series_unknown"),
    ({"package_name": "pgedge-spock50_17-llvmjit"}, "runtime_not_certified"),
    ({"package_name": "pgedge-spock50_17-debuginfo"}, "package_not_allowed"),
    ({"package_name": "pgedge-rag-server2"}, "package_not_allowed"),
], ids=["family", "pkg-pg", "req-pg", "pkg-series", "ver-series", "unknown-series", "llvmjit",
        "debuginfo", "unrelated"])
def test_request_rejects_mismatched_spock_package(over, why):
    with pytest.raises(pr.RequestError, match=why):
        pr.normalize_request(_spock(**over))


def test_rag_package_on_spock_and_spock_package_on_rag_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_req(package_name="pgedge-spock50_17"))


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


_SPOCK_LIKE = {"pg_coupled": True,
               "runtime_package_templates": {"rpm": ["p{series}_{pg}"], "deb": ["p-{pg}-{series}"]},
               "series_by_version_major": {"5": "50"}}


@pytest.mark.parametrize("entry", [
    dict(_SPOCK_LIKE, pg_coupled="true"),                                   # not strictly True
    dict(_SPOCK_LIKE, allowed_runtime_package_names=["p"]),                 # flat + coupled mixed
    dict(_SPOCK_LIKE, runtime_package_templates={}),                        # no templates
    dict(_SPOCK_LIKE, runtime_package_templates={"apk": ["p{pg}"]}),        # unknown family
    dict(_SPOCK_LIKE, runtime_package_templates={"rpm": ["p{series}"]}),    # no {pg}
    dict(_SPOCK_LIKE, runtime_package_templates={"rpm": ["p{pg}{pg}"]}),    # {pg} twice
    dict(_SPOCK_LIKE, runtime_package_templates={"rpm": ["p{arch}_{pg}"]}), # unknown placeholder
    dict(_SPOCK_LIKE, runtime_package_templates={"rpm": ["p{pg"]}),         # malformed placeholder
    {k: v for k, v in _SPOCK_LIKE.items() if k != "series_by_version_major"},  # {series} unmapped
    dict(_SPOCK_LIKE, series_by_version_major={"5": "fifty"}),             # non-digit series
    dict(_SPOCK_LIKE, series_by_version_major={"5": "50", "6": "50"}),     # two majors, one series
    dict(_SPOCK_LIKE, uncertified_runtime_package_templates={"rpm": ["p{series}_{pg}-x"]}),  # no reason
    dict(_SPOCK_LIKE, uncertified_reason="why"),                            # reason, no templates
], ids=["pg_coupled-not-true", "mixed-flat", "empty-templates", "unknown-family", "no-pg",
        "pg-twice", "unknown-placeholder", "malformed-placeholder", "series-unmapped",
        "bad-series", "dup-series", "uncertified-no-reason", "reason-no-templates"])
def test_malformed_pg_coupled_policy_fails_in_request_and_capture(tmp_path, entry):
    path = _write(tmp_path, {"schema": "pep-capture-policy/1", "components": {"c": entry}})
    with pytest.raises(pr.ComponentPolicyError):
        pr.load_component_policy(path)
    with pytest.raises(IO.CaptureIOError):
        IO.resolve_component_policy(path, "c")


def test_wellformed_pg_coupled_policy_loads_in_request_and_capture(tmp_path):
    path = _write(tmp_path, {"schema": "pep-capture-policy/1", "components": {
        "c": _SPOCK_LIKE, "f": {"allowed_runtime_package_names": ["a"]}}})
    assert pr.load_component_policy(path)["c"] == _SPOCK_LIKE
    assert pr.load_component_packages(path) == {"f": ("a",)}
    assert IO.resolve_component_policy(path, "c") == _SPOCK_LIKE


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
sys.path[:0] = {paths!r}
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
assert P.COMPONENT_POLICY is R.COMPONENT_POLICY, "planner COMPONENT_POLICY is not the request object"
assert P._pp is R._pp is sys.modules["pep_package_policy"], "package-policy rules are not one module"
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
    paths = [str(_HERE.parent), str(_HELPERS)]  # pipeline/ (planner, resolver) + aspects/pipeline/ (request, env)
    proc = subprocess.run([sys.executable, "-c", _ORDER_CHECK.format(order=order, paths=paths)],
                          cwd=str(_HERE), capture_output=True, text=True)
    assert proc.returncode == 0, (
        "import order %r replaced pep_request or broke the shared object:\n"
        "STDOUT: %s\nSTDERR: %s" % (order, proc.stdout, proc.stderr))
    assert proc.stdout.strip().splitlines()[-1] == "OK", proc.stdout
