"""PG-coupled extensions without a series (snowflake, lolor, safesession) through the REAL policy,
request validation and certification chain (cert-plan reducer -> invocation planner -> cert-result
reducer -> gate), against their REAL entries in aspects/pipeline/pep_capture_policy.json.

Every PG-coupled entry without series_by_version_major is covered; PACKAGES pins that set, so
registering such an extension is a deliberate one-line change here. What these prove, per extension:
  * the entry names exactly the packages its release workflow builds, with the RPM -llvmjit
    companion reported as not certified (and why);
  * a request is accepted only for the package of its own family and PostgreSQL major, whatever the
    release version (the names carry no version or series), and every crossed, debug, uncertified
    or other-component package is rejected;
  * no physical package name belongs to two components;
  * a package is certified on its own build PG major only; a wrong family/PG member stays visible as
    a gap; the -llvmjit companion is noted, never a gap; debug and source packages stay silent;
    PG19 stays an explicit pg_not_supported gap.
Platform data is synthetic, as in test_pep_spock_certification; release versions are synthetic so
nothing depends on today's release number.
"""
import copy

import pytest

import pep_cert_gate as G
import pep_cert_plan as CP
import pep_cert_result as CR
import pep_invocation_plan as P
import pep_request as PR
import test_pep_coverage_gaps as CG
import test_pep_spock_certification as SC     # the generic member/cell builders and exec catalog

_pp = PR._pp

# component -> (RPM runtime template, DEB runtime template), as its release workflow names them.
PACKAGES = {
    "snowflake": ("pgedge-snowflake_{pg}", "pgedge-postgresql-{pg}-snowflake"),
    "safesession": ("pgedge-safesession_{pg}", "pgedge-postgresql-{pg}-safesession"),
}
EXTENSIONS = sorted(c for c, e in PR.COMPONENT_POLICY.items()
                    if _pp.is_coupled(e) and "series_by_version_major" not in e)
VERSIONS = ("1.0", "2.6.1", "9.8.7", "10.0.0")          # synthetic: names must not depend on them
CLEAN = SC.CLEAN


def name(comp, family, pg, suffix=""):
    rpm, deb = PACKAGES[comp]
    return (rpm if family == "rpm" else deb).replace("{pg}", pg) + suffix


def _req(comp, **over):
    raw = {"component": comp, "package_name": name(comp, "rpm", "17") if comp in PACKAGES else None,
           "channel": "daily",
           "expected_version": "9.8.7", "family": "rpm", "arch": "amd64", "pg_major": "17",
           "container_alias": "rocky9-amd64"}
    raw.update(over)
    return raw


def certify(comp, cells, *, version="9.8.7"):
    jobs, arts = [], []
    for i, c in enumerate(cells):
        cid = c["cell"]["cell_id"]
        jobs.append({"cell_id": cid, "job_id": i + 1, "run_attempt": 1, "status": "completed",
                     "conclusion": c["job"]})
        arts.append({"name": cid, "id": 100 + i, "members": c["members"]})
    cert_plan = CP.reduce({
        "provenance": dict(CG.PROVENANCE),
        "release_intent": {"logical_component": comp, "intended_version": version, "intended_buildnum": "1",
                           "effective_tag": "v" + version, "channel": "daily", "simulated": False},
        "component_policy": copy.deepcopy(PR.COMPONENT_POLICY[comp]),
        "planned_cells": [c["cell"] for c in cells], "job_records": jobs, "artifacts": arts,
        "publication_results": {"rpm": "success", "deb": "success"}, "execution_mode": "full"})
    assert cert_plan["plan_resolved"] is True, cert_plan["errors"]
    plan = P.build_invocation_plan(cert_plan, SC.EXEC_CATALOG, CG.ENABLED, execution_mode="full")
    assert plan["plan_resolved"] is True, plan["errors"]
    decided = {}
    for enforcement in ("observe", "gate"):
        summaries = [CG._summary(inv, "pass", enforcement, preview=False) for inv in plan["matrix"]["include"]]
        result = CR.build_cert_result(plan, summaries, "1")
        assert result["result_resolved"] is True, result["errors"]
        decided[enforcement] = (result, G.decide(result, enforcement))
    return cert_plan, plan, decided


def _member(comp, family, os_token, pg, *, suffix="", version="9.8.7", **kw):
    return SC.member(family, os_token, "amd64", name(comp, family, pg, suffix), version=version, **kw)


# --------------------------------------------------------------------------- #
# the registered set and each entry
# --------------------------------------------------------------------------- #
def test_registered_series_less_extensions_are_exactly_the_pinned_set():
    assert EXTENSIONS == sorted(PACKAGES)


@pytest.mark.parametrize("comp", EXTENSIONS)
def test_entry_names_the_built_packages_and_the_uncertified_llvmjit_companion(comp):
    entry = PR.COMPONENT_POLICY[comp]
    rpm, deb = PACKAGES[comp]
    assert {k: v for k, v in entry.items() if k != "uncertified_reason"} == {
        "pg_coupled": True, "runtime_package_templates": {"rpm": [rpm], "deb": [deb]},
        "uncertified_runtime_package_templates": {"rpm": [rpm + "-llvmjit"]}, "expected_binary_version": ""}
    assert entry["uncertified_reason"].strip()
    assert comp not in PR.COMPONENT_PACKAGES                        # no fixed name list


@pytest.mark.parametrize("comp", EXTENSIONS)
def test_package_names_do_not_depend_on_the_release_version(comp):
    entry = PR.COMPONENT_POLICY[comp]
    for family in ("rpm", "deb"):
        names = {_pp.expected_names(entry, family, "17", v) for v in VERSIONS + (None,)}
        assert names == {(name(comp, family, "17"),)}


# --------------------------------------------------------------------------- #
# request validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("comp", EXTENSIONS)
@pytest.mark.parametrize("family,alias,pg", [("rpm", "rocky9-amd64", "16"), ("deb", "debian13-amd64", "18")])
@pytest.mark.parametrize("version", VERSIONS)
def test_request_accepts_the_package_for_its_family_and_pg_major(comp, family, alias, pg, version):
    req = PR.normalize_request(_req(comp, family=family, container_alias=alias, pg_major=pg,
                                    package_name=name(comp, family, pg), expected_version=version))
    assert (req["component"], req["package_name"], req["pg_major"]) == (comp, name(comp, family, pg), pg)


@pytest.mark.parametrize("comp", EXTENSIONS)
@pytest.mark.parametrize("bad,why", [
    (lambda c: {"package_name": name(c, "deb", "17")}, "package_family_mismatch"),
    (lambda c: {"package_name": name(c, "rpm", "16")}, "pg_major_mismatch"),
    (lambda c: {"pg_major": "18"}, "pg_major_mismatch"),
    (lambda c: {"package_name": name(c, "rpm", "17", "-llvmjit")}, "runtime_not_certified"),
    (lambda c: {"package_name": name(c, "rpm", "17", "-debuginfo")}, "package_not_allowed"),
    (lambda c: {"package_name": "pgedge-spock50_17"}, "package_not_allowed"),
    (lambda c: {"package_name": "pgedge-ace"}, "package_not_allowed"),
], ids=["family", "pkg-pg", "req-pg", "llvmjit", "debuginfo", "spock", "flat"])
def test_request_rejects_crossed_uncertified_and_unrelated_packages(comp, bad, why):
    with pytest.raises(PR.RequestError, match=why):
        PR.normalize_request(_req(comp, **bad(comp)))


@pytest.mark.parametrize("comp", EXTENSIONS)
def test_other_components_do_not_accept_this_package(comp):
    for other in sorted(set(PR.COMPONENT_POLICY) - {comp}):
        with pytest.raises(PR.RequestError):
            PR.normalize_request(_req(other, package_name=name(comp, "rpm", "17"),
                                      expected_version="5.0.13" if other == "spock" else "9.8.7"))


def test_no_physical_package_name_belongs_to_two_components():
    owners = {}
    for comp, entry in PR.COMPONENT_POLICY.items():
        for family in ("rpm", "deb"):
            for pg in ("14", "15", "16", "17", "18", "19"):
                for version in ("5.0.13", "6.0.0", "9.8.7"):
                    found = set(_pp.expected_names(entry, family, pg, version))
                    for tmpl in (entry.get("uncertified_runtime_package_templates") or {}).get(family, []):
                        series = _pp.series_for_version(entry, version)
                        if "{series}" not in tmpl or series is not None:
                            found.add(tmpl.replace("{pg}", pg).replace("{series}", series or ""))
                    for n in found:
                        owners.setdefault(n, set()).add(comp)
    shared = {n: c for n, c in owners.items() if len(c) > 1}
    assert shared == {}


# --------------------------------------------------------------------------- #
# certification chain
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("comp", EXTENSIONS)
@pytest.mark.parametrize("version", ["1.0", "9.8.7"])
def test_rpm_and_deb_packages_certify_on_their_own_build_pg_major_only(comp, version):
    cells = [SC.cell("rpm", "el-9", "amd64", pg, [_member(comp, "rpm", "el-9", pg, version=version)])
             for pg in ("16", "17")]
    cells.append(SC.cell("deb", "trixie", "amd64", "18", [_member(comp, "deb", "trixie", "18", version=version)]))
    cp, plan, decided = certify(comp, cells, version=version)
    assert SC.runs(plan) == sorted([
        (name(comp, "deb", "18"), "deb", "18", "debian13-amd64"),
        (name(comp, "rpm", "16"), "rpm", "16", "rocky9-amd64"),
        (name(comp, "rpm", "17"), "rpm", "17", "rocky9-amd64"),
    ])
    assert all(i["component"] == comp and i["expected_version"] == version for i in plan["matrix"]["include"])
    assert plan["coverage_gaps"] == [] and plan["uncertified_runtime_packages"] == []
    CG.assert_reconciles(cp, plan)
    assert CG.outcomes(decided) == CLEAN


@pytest.mark.parametrize("comp", EXTENSIONS)
@pytest.mark.parametrize("bad,reason", [
    (lambda c: name(c, "deb", "17"), "package_family_mismatch"),
    (lambda c: name(c, "rpm", "16"), "pg_major_mismatch"),
    (lambda c: name(c, "rpm", "16", "-llvmjit"), "pg_major_mismatch"),
], ids=["family", "pg", "llvmjit-wrong-pg"])
def test_mismatched_member_beside_the_valid_package_is_a_visible_member_gap(comp, bad, reason):
    cp, plan, decided = certify(comp, [SC.cell("rpm", "el-9", "amd64", "17", [
        _member(comp, "rpm", "el-9", "17"),
        SC.member("rpm", "el-9", "amd64", bad(comp), version="9.8.7", sha="c" * 64, path="bad.rpm")])])
    assert SC.runs(plan) == [(name(comp, "rpm", "17"), "rpm", "17", "rocky9-amd64")]
    assert CG.gaps(plan) == [("member", "rpm.el-9.amd64.pg17", bad(comp), P.GAP_MEMBER_REJECTED,
                              "%s; path=bad.rpm; native_arch=x86_64; sha256=cccccccccccc" % reason)]
    CG.assert_reconciles(cp, plan)
    assert CG.outcomes(decided) == CG.PARTIAL


@pytest.mark.parametrize("comp", EXTENSIONS)
def test_a_cell_with_only_another_components_package_is_a_cell_gap(comp):
    ok = SC.cell("deb", "trixie", "amd64", "17", [_member(comp, "deb", "trixie", "17")])
    bad = SC.cell("rpm", "el-9", "amd64", "17", [SC.member("rpm", "el-9", "amd64", "pgedge-spock50_17")])
    cp, plan, decided = certify(comp, [ok, bad])
    assert SC.runs(plan) == [(name(comp, "deb", "17"), "deb", "17", "debian13-amd64")]
    assert CG.gaps(plan) == [("cell", "rpm.el-9.amd64.pg17", None, P.GAP_NO_RUNTIME_TARGET, "package_not_allowed")]
    assert CG.outcomes(decided) == CG.PARTIAL


@pytest.mark.parametrize("comp", EXTENSIONS)
def test_llvmjit_companion_is_noted_with_its_reason_and_is_never_a_gap(comp):
    jit = name(comp, "rpm", "17", "-llvmjit")
    cp, plan, decided = certify(comp, [SC.cell("rpm", "el-9", "amd64", "17", [
        _member(comp, "rpm", "el-9", "17"),
        SC.member("rpm", "el-9", "amd64", jit, version="9.8.7", sha="e" * 64, path="jit.rpm")])])
    assert plan["coverage_gaps"] == []
    assert plan["uncertified_runtime_packages"] == [{
        "cell_id": "pepcell.v1.rpm.el-9.amd64.pg17", "family": "rpm", "os": "el-9", "arch": "amd64",
        "build_pg_major": "17", "physical_package": jit, "path": "jit.rpm", "sha256": "e" * 64,
        "reason": PR.COMPONENT_POLICY[comp]["uncertified_reason"]}]
    CG.assert_reconciles(cp, plan)
    assert CG.outcomes(decided) == CLEAN
    assert decided["gate"][0]["coverage_status"] == "complete"


@pytest.mark.parametrize("comp", EXTENSIONS)
def test_debug_and_source_packages_stay_silent(comp):
    cp, plan, decided = certify(comp, [SC.cell("rpm", "el-9", "amd64", "17", [
        _member(comp, "rpm", "el-9", "17"),
        _member(comp, "rpm", "el-9", "17", suffix="-debuginfo", cls="debug"),
        _member(comp, "rpm", "el-9", "17", suffix="-debugsource", cls="debug"),
        _member(comp, "rpm", "el-9", "17", cls="source", sha="d" * 64, path="src.rpm")])])
    assert plan["coverage_gaps"] == [] and plan["uncertified_runtime_packages"] == []
    assert CG.outcomes(decided) == CLEAN


@pytest.mark.parametrize("comp", EXTENSIONS)
def test_pg19_build_is_an_explicit_pg_not_supported_gap(comp):
    cp, plan, decided = certify(comp, [
        SC.cell("rpm", "el-9", "amd64", "17", [_member(comp, "rpm", "el-9", "17")]),
        SC.cell("rpm", "el-9", "amd64", "19", [_member(comp, "rpm", "el-9", "19")])])
    assert SC.runs(plan) == [(name(comp, "rpm", "17"), "rpm", "17", "rocky9-amd64")]
    assert CG.gaps(plan) == [("target", "rpm.el-9.amd64.pg19", name(comp, "rpm", "19"), P.GAP_PG_NOT_SUPPORTED, "19")]
    CG.assert_reconciles(cp, plan)
    assert CG.outcomes(decided) == CG.PARTIAL
