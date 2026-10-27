"""Spock (a PG-coupled component) through the REAL certification chain: cert-plan reducer ->
invocation planner -> cert-result reducer -> gate -> report, against the REAL spock entry in
aspects/pipeline/pep_capture_policy.json.

What these prove:
  * the package that is certified is bound to the cell's family, its BUILD PostgreSQL major and the
    series of the release version (5.x -> spock50, 6.x -> spock60), for RPM and DEB;
  * a spock package for the wrong family, PG major or series is rejected and stays VISIBLE as a
    coverage gap -- including when the same cell also has the valid package;
  * the RPM -llvmjit companion is noted as "not certified by policy" in the plan, the result and the
    report, and is never a coverage gap (so it cannot turn a clean run partial on its own);
  * the planner's backstop rejects a mismatched target even if one reached it;
  * PG19 stays an explicit pg_not_supported gap;
  * Spock 6 uses the same design (unit level only -- no live v6 compatibility is claimed).
Platform data is synthetic, as in test_pep_coverage_gaps.
"""
import copy
from pathlib import Path

import pytest

import pep_cert_gate as G
import pep_cert_plan as CP
import pep_cert_report as R
import pep_cert_result as CR
import pep_invocation_plan as P
import pep_request as PR
import test_pep_coverage_gaps as CG      # the shared summary/outcome/reconciliation helpers

SPOCK = PR.COMPONENT_POLICY["spock"]
EXEC_CATALOG = dict(CG.EXEC_CATALOG, supported_pg_majors=["16", "17", "18"])
CLEAN = {"observe": ("pass", "allow", "success", "clean_pass"),
         "gate": ("pass", "allow", "success", "clean_pass")}


def name(family, pg, series="50", suffix=""):
    base = ("pgedge-spock%s_%s" % (series, pg)) if family == "rpm" else ("pgedge-postgresql-%s-spock%s" % (pg, series))
    return base + suffix


def member(family, os_token, arch, pkg, *, version="5.0.13", cls="runtime", sha=None, path=None):
    native = {"amd64": "x86_64", "arm64": "aarch64"}[arch] if family == "rpm" else arch
    release = ("1." + os_token.replace("-", "")) if family == "rpm" else ("1." + os_token)
    return {"package_name": pkg, "epoch": None, "version": version, "release": release,
            "native_arch": native, "package_class": cls,
            "sha256": sha or (pkg + os_token + arch).encode().hex()[:64].ljust(64, "0"),
            "artifact_member_path": path or "%s-%s-%s.%s.%s" % (pkg, version, release, native, family)}


def cell(family, os_token, arch, pg, members, *, job="success"):
    cid = "pepcell.v1.%s.%s.%s.pg%s" % (family, os_token, arch, pg)
    return {"cell": {"cell_id": cid, "artifact_name": cid, "family": family, "os": os_token,
                     "normalized_arch": arch, "pg_coupled": True, "build_pg_major": pg,
                     "build_pg_version": pg + ".5"},
            "job": job, "members": members}


def certify(cells, *, version="5.0.13", policy=None, outcome=lambda inv: "pass", component_policy=None,
            mode="full", simulated=False, publication=None):
    if publication is None:
        publication = {"rpm": "skipped", "deb": "skipped"} if simulated else {"rpm": "success", "deb": "success"}
    jobs, arts = [], []
    for i, c in enumerate(cells):
        cid = c["cell"]["cell_id"]
        jobs.append({"cell_id": cid, "job_id": i + 1, "run_attempt": 1, "status": "completed",
                     "conclusion": c["job"]})
        arts.append({"name": cid, "id": 100 + i, "members": c["members"]})
    cert_plan = CP.reduce({
        "provenance": dict(CG.PROVENANCE),          # the shared summaries are stamped with it
        "release_intent": {"logical_component": "spock", "intended_version": version, "intended_buildnum": "1",
                           "effective_tag": "v" + version, "channel": "staging", "simulated": simulated},
        "component_policy": copy.deepcopy(policy if policy is not None else SPOCK),
        "planned_cells": [c["cell"] for c in cells], "job_records": jobs, "artifacts": arts,
        "publication_results": publication, "execution_mode": mode})
    assert cert_plan["plan_resolved"] is True, cert_plan["errors"]
    plan = P.build_invocation_plan(cert_plan, EXEC_CATALOG, CG.ENABLED, execution_mode=mode,
                                   component_policy=component_policy)
    assert plan["plan_resolved"] is True, plan["errors"]
    decided = {}
    for enforcement in ("observe", "gate"):
        summaries = [CG._summary(inv, outcome(inv), enforcement, preview=(mode == "preview"))
                     for inv in plan["matrix"]["include"]]
        result = CR.build_cert_result(plan, summaries, "1")
        assert result["result_resolved"] is True, result["errors"]
        decided[enforcement] = (result, G.decide(result, enforcement))
    return cert_plan, plan, decided


def runs(plan):
    return sorted((i["package_name"], i["family"], i["pg_major"], i["container_alias"])
                  for i in plan["matrix"]["include"])


# --------------------------------------------------------------------------- #
# correct RPM + DEB requests certify cleanly (v5 and, at unit level, v6)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("version,series", [("5.0.13", "50"), ("6.0.0", "60")], ids=["v5", "v6"])
def test_correct_rpm_and_deb_packages_certify_for_their_own_pg_major(version, series):
    cells = [cell("rpm", "el-9", "amd64", pg, [member("rpm", "el-9", "amd64", name("rpm", pg, series), version=version)])
             for pg in ("16", "17")]
    cells.append(cell("deb", "trixie", "amd64", "18",
                      [member("deb", "trixie", "amd64", name("deb", "18", series), version=version)]))
    cp, plan, decided = certify(cells, version=version)
    # each package runs ONLY on the PG major it was built for -- never on every supported major
    assert runs(plan) == [
        (name("deb", "18", series), "deb", "18", "debian13-amd64"),
        (name("rpm", "16", series), "rpm", "16", "rocky9-amd64"),
        (name("rpm", "17", series), "rpm", "17", "rocky9-amd64"),
    ]
    assert plan["coverage_gaps"] == [] and plan["uncertified_runtime_packages"] == []
    assert cp["component_policy"]["pg_coupled"] is True
    assert cp["component_policy"]["series_by_version_major"] == {"5": "50", "6": "60"}
    CG.assert_reconciles(cp, plan)
    assert CG.outcomes(decided) == CLEAN


# --------------------------------------------------------------------------- #
# mismatched family / PG / series: rejected and visible, even beside the valid package
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad,reason", [
    (name("deb", "17"), "package_family_mismatch"),
    (name("rpm", "16"), "pg_major_mismatch"),
    (name("rpm", "17", "60"), "series_mismatch"),
    (name("rpm", "16", suffix="-llvmjit"), "pg_major_mismatch"),     # a companion for the wrong PG
], ids=["family", "pg", "series", "llvmjit-wrong-pg"])
def test_mismatched_member_beside_the_valid_package_is_a_visible_member_gap(bad, reason):
    cp, plan, decided = certify([cell("rpm", "el-9", "amd64", "17", [
        member("rpm", "el-9", "amd64", name("rpm", "17")),
        member("rpm", "el-9", "amd64", bad, sha="c" * 64, path="bad.rpm")])])
    assert runs(plan) == [(name("rpm", "17"), "rpm", "17", "rocky9-amd64")]   # the valid one still runs
    assert CG.gaps(plan) == [("member", "rpm.el-9.amd64.pg17", bad, P.GAP_MEMBER_REJECTED,
                              "%s; path=bad.rpm; native_arch=x86_64; sha256=cccccccccccc" % reason)]
    CG.assert_reconciles(cp, plan)
    assert CG.outcomes(decided) == CG.PARTIAL                                  # never clean_pass


@pytest.mark.parametrize("pkgs,reasons", [
    ([name("rpm", "16")], "pg_major_mismatch"),
    ([name("rpm", "17", "60")], "series_mismatch"),
    ([name("deb", "17"), name("rpm", "18")], "package_family_mismatch, pg_major_mismatch"),
], ids=["pg-only", "series-only", "family-and-pg"])
def test_cell_with_only_mismatched_packages_is_a_cell_gap_naming_why(pkgs, reasons):
    ok = cell("deb", "trixie", "amd64", "17", [member("deb", "trixie", "amd64", name("deb", "17"))])
    bad = cell("rpm", "el-9", "amd64", "17", [member("rpm", "el-9", "amd64", p) for p in pkgs])
    cp, plan, decided = certify([ok, bad])
    assert runs(plan) == [(name("deb", "17"), "deb", "17", "debian13-amd64")]
    assert CG.gaps(plan) == [("cell", "rpm.el-9.amd64.pg17", None, P.GAP_NO_RUNTIME_TARGET, reasons)]
    CG.assert_reconciles(cp, plan)
    assert CG.outcomes(decided) == CG.PARTIAL


def test_debug_and_source_packages_stay_silent_policy_exclusions():
    cp, plan, decided = certify([cell("rpm", "el-9", "amd64", "17", [
        member("rpm", "el-9", "amd64", name("rpm", "17")),
        member("rpm", "el-9", "amd64", name("rpm", "17", suffix="-debuginfo"), cls="debug"),
        member("rpm", "el-9", "amd64", name("rpm", "17", suffix="-debugsource"), cls="debug"),
        member("rpm", "el-9", "amd64", name("rpm", "17"), cls="source", sha="d" * 64, path="src.rpm")])])
    assert plan["coverage_gaps"] == [] and plan["uncertified_runtime_packages"] == []
    assert CG.outcomes(decided) == CLEAN


# --------------------------------------------------------------------------- #
# llvmjit: noted as not certified, never a coverage gap
# --------------------------------------------------------------------------- #
def test_llvmjit_companion_is_noted_not_a_gap_through_plan_result_and_report(tmp_path):
    jit = name("rpm", "17", suffix="-llvmjit")
    cp, plan, decided = certify([cell("rpm", "el-9", "amd64", "17", [
        member("rpm", "el-9", "amd64", name("rpm", "17")),
        member("rpm", "el-9", "amd64", jit, sha="e" * 64, path="jit.rpm")])])
    [m] = [m for m in cp["cells"][0]["members"] if m["package_name"] == jit]
    assert m["exclusion_reasons"] == ["runtime_not_certified"]
    assert plan["coverage_gaps"] == []
    assert plan["uncertified_runtime_packages"] == [{
        "cell_id": "pepcell.v1.rpm.el-9.amd64.pg17", "family": "rpm", "os": "el-9", "arch": "amd64",
        "build_pg_major": "17", "physical_package": jit, "path": "jit.rpm", "sha256": "e" * 64,
        "reason": SPOCK["uncertified_reason"]}]
    assert plan["counts"]["coverage_gaps"] == 0
    CG.assert_reconciles(cp, plan)
    assert CG.outcomes(decided) == CLEAN                       # noted, but it does not block or degrade
    result, decision = decided["gate"]
    assert result["uncertified_runtime_packages"] == plan["uncertified_runtime_packages"]
    assert result["coverage_status"] == "complete"
    md = Path(tmp_path) / "summary.md"
    R.render_report(result, decision, {}, Path(tmp_path) / "out", markdown_path=md)
    html = (Path(tmp_path) / "out" / "consolidated-report.html").read_text()
    assert "Runtime packages not certified by policy (1)" in html and jit in html
    assert "planned but not certified" not in html                          # no gap table at all
    text = md.read_text()
    assert "Not certified by policy" in text and jit in text and "Coverage gaps" not in text


def _jit_cells():
    return [cell("rpm", "el-9", "amd64", "17", [
        member("rpm", "el-9", "amd64", name("rpm", "17")),
        member("rpm", "el-9", "amd64", name("rpm", "17", suffix="-llvmjit"), sha="e" * 64, path="jit.rpm")])]


def _render(tmp_path, result, decision):
    """(the HTML 'not certified by policy' section, the Markdown summary's matching lines)."""
    md = Path(tmp_path) / "summary.md"
    R.render_report(result, decision, {}, Path(tmp_path) / "out", markdown_path=md)
    html = (Path(tmp_path) / "out" / "consolidated-report.html").read_text()
    start = html.index('<h2 id="uncertified">')
    section = html[start:html.index("</table></div>", start)]
    lines = [l for l in md.read_text().splitlines() if "certified by policy" in l]
    return section, "\n".join(lines)


@pytest.mark.parametrize("kw", [
    {},                                                                     # full, published
    {"mode": "preview", "simulated": True},                                 # preview: nothing published
    {"publication": {"rpm": "failure", "deb": "success"}},                  # full, RPM publication failed
], ids=["full", "preview", "failed-publication"])
def test_uncertified_wording_never_claims_publication(tmp_path, kw):
    jit = name("rpm", "17", suffix="-llvmjit")
    _, plan, decided = certify(_jit_cells(), **kw)
    assert [u["physical_package"] for u in plan["uncertified_runtime_packages"]] == [jit]
    section, md = _render(tmp_path, *decided["gate"])
    for text in (section, md):
        assert jit in text and "build artifacts" in text, text
        assert "shipped" not in text.lower() and "published" not in text.lower(), text


def test_reports_without_uncertified_packages_are_unchanged(tmp_path):
    # RAG-shaped (flat) runs carry an empty list, and the report adds no section for it
    cp, plan, decided = CG.certify([CG.planned("rpm", "el-9", "amd64")])
    assert plan["uncertified_runtime_packages"] == []
    result, decision = decided["gate"]
    assert result["uncertified_runtime_packages"] == []
    md = Path(tmp_path) / "summary.md"
    R.render_report(result, decision, {}, Path(tmp_path) / "out", markdown_path=md)
    assert "not certified by policy" not in (Path(tmp_path) / "out" / "consolidated-report.html").read_text()
    assert "Not certified by policy" not in md.read_text()


def test_result_tolerates_a_plan_without_the_list_and_rejects_a_malformed_one():
    _, plan, _ = certify([cell("rpm", "el-9", "amd64", "17", [member("rpm", "el-9", "amd64", name("rpm", "17"))])])
    legacy = {k: v for k, v in plan.items() if k != "uncertified_runtime_packages"}
    assert CR.build_cert_result(legacy, [], "1")["uncertified_runtime_packages"] == []
    assert CR.build_cert_result(dict(plan, uncertified_runtime_packages=[]), [], "1")["result_resolved"] is True
    bad = CR.build_cert_result(dict(plan, uncertified_runtime_packages={"x": 1}), [], "1")
    assert bad["result_resolved"] is False
    assert "plan uncertified_runtime_packages must be a list" in bad["errors"]
    assert bad["uncertified_runtime_packages"] == []


_GOOD = {"cell_id": "pepcell.v1.rpm.el-9.amd64.pg17", "family": "rpm", "os": "el-9", "arch": "amd64",
         "build_pg_major": "17", "physical_package": "pgedge-spock50_17-llvmjit", "path": "jit.rpm",
         "sha256": "e" * 64, "reason": "JIT companion"}


@pytest.mark.parametrize("entry,fragment", [
    (None, "[0] must be an object"),
    ("pgedge-spock50_17-llvmjit", "[0] must be an object"),
    ({}, "[0].cell_id"),
    (dict(_GOOD, cell_id=" "), "[0].cell_id"),
    (dict(_GOOD, family="apk"), "[0].family"),
    (dict(_GOOD, physical_package=""), "[0].physical_package"),
    ({k: v for k, v in _GOOD.items() if k != "physical_package"}, "[0].physical_package"),
    (dict(_GOOD, sha256="abc"), "[0].sha256"),
    (dict(_GOOD, sha256=None), "[0].sha256"),
    (dict(_GOOD, reason=None), "[0].reason"),
    (dict(_GOOD, reason=" "), "[0].reason"),
    (dict(_GOOD, os=7), "[0].os"),
    (dict(_GOOD, path={}), "[0].path"),
], ids=["null", "string", "empty", "blank-cell", "bad-family", "blank-package", "no-package", "short-sha",
        "no-sha", "no-reason", "blank-reason", "os-int", "path-object"])
def test_result_fails_closed_on_a_malformed_uncertified_record(entry, fragment):
    _, plan, _ = certify(_jit_cells())
    for records in ([entry], [dict(_GOOD), entry]):                 # alone, and beside a valid record
        bad = CR.build_cert_result(dict(plan, uncertified_runtime_packages=records), [], "1")
        assert bad["result_resolved"] is False
        where = "[%d]" % (len(records) - 1)
        assert any(("plan uncertified_runtime_packages" + fragment.replace("[0]", where)) in e
                   for e in bad["errors"]), bad["errors"]
        assert bad["uncertified_runtime_packages"] == []


def test_valid_uncertified_records_with_optional_nulls_pass():
    _, plan, _ = certify(_jit_cells())
    rec = dict(_GOOD, os=None, arch=None, build_pg_major=None, path=None)
    res = CR.build_cert_result(dict(plan, uncertified_runtime_packages=[rec]), [], "1")
    assert res["result_resolved"] is True and res["uncertified_runtime_packages"] == [rec]


def test_report_shows_malformed_uncertified_records_instead_of_dropping_them(tmp_path):
    _, _, decided = certify(_jit_cells())
    result, decision = decided["gate"]
    good = result["uncertified_runtime_packages"][0]
    tampered = dict(result, uncertified_runtime_packages=[good, None, {"physical_package": ""}])
    section, md = _render(tmp_path, tampered, decision)
    assert "Runtime packages not certified by policy (3)" in section
    assert good["physical_package"] in section and section.count("malformed record") == 2
    assert good["physical_package"] in md and "2 malformed records" in md
    section, md = _render(tmp_path, dict(result, uncertified_runtime_packages={"x": 1}), decision)
    assert "malformed" in section and "malformed" in md


# --------------------------------------------------------------------------- #
# PG19 stays deferred; coupled policy on an uncoupled cell; planner backstop; malformed policy
# --------------------------------------------------------------------------- #
def test_pg19_build_is_an_explicit_pg_not_supported_gap():
    cp, plan, decided = certify([
        cell("rpm", "el-9", "amd64", "17", [member("rpm", "el-9", "amd64", name("rpm", "17"))]),
        cell("rpm", "el-9", "amd64", "19", [member("rpm", "el-9", "amd64", name("rpm", "19"))])])
    assert runs(plan) == [(name("rpm", "17"), "rpm", "17", "rocky9-amd64")]
    assert CG.gaps(plan) == [("target", "rpm.el-9.amd64.pg19", name("rpm", "19"), P.GAP_PG_NOT_SUPPORTED, "19")]
    CG.assert_reconciles(cp, plan)
    assert CG.outcomes(decided) == CG.PARTIAL


def test_pg_coupled_policy_on_a_cell_without_pg_identity_is_a_visible_gap():
    c = cell("rpm", "el-9", "amd64", "17", [member("rpm", "el-9", "amd64", name("rpm", "17"))])
    c["cell"].update(pg_coupled=False)
    del c["cell"]["build_pg_major"], c["cell"]["build_pg_version"]
    cp, plan, _ = certify([c])
    assert plan["matrix"]["include"] == []
    assert CG.gaps(plan) == [("cell", "rpm.el-9.amd64.pg17", None, P.GAP_NO_RUNTIME_TARGET, "pg_major_unknown")]


def test_planner_backstop_rejects_a_mismatched_target_that_reached_it():
    cp, _, _ = certify([cell("rpm", "el-9", "amd64", "17", [member("rpm", "el-9", "amd64", name("rpm", "17"))])])
    for mutate, why in ((lambda t: t.update(build_pg_major="16", build_pg_version="16.5"), "pg_major_mismatch"),
                        (lambda t: t.update(family="deb"), "package_family_mismatch"),
                        (lambda t: t.update(pg_coupled=False, build_pg_major=None, build_pg_version=None),
                         "without PG coupling")):
        forged = copy.deepcopy(cp)
        mutate(forged["cells"][0]["targets"][0])
        plan = P.build_invocation_plan(forged, EXEC_CATALOG, CG.ENABLED, execution_mode="full")
        assert plan["matrix"]["include"] == [], why
        [gap] = plan["coverage_gaps"]
        assert gap["reason"] == P.GAP_PACKAGE_POLICY_MISMATCH and why in gap["detail"], gap
    forged = copy.deepcopy(cp)
    forged["release_intent"]["intended_version"] = "6.0.0"            # series no longer matches
    plan = P.build_invocation_plan(forged, EXEC_CATALOG, CG.ENABLED, execution_mode="full")
    assert plan["matrix"]["include"] == [] and "series_mismatch" in plan["coverage_gaps"][0]["detail"]


@pytest.mark.parametrize("over", [
    {"runtime_package_templates": {"rpm": ["pgedge-spock{series}"]}},          # no {pg}
    {"series_by_version_major": {"5": "50", "6": "50"}},                      # ambiguous series
    {"allowed_runtime_package_names": ["pgedge-spock50_17"]},                 # mixed flat + coupled
    {"uncertified_reason": " "},
], ids=["no-pg", "dup-series", "mixed", "blank-reason"])
def test_malformed_coupled_policy_fails_the_cert_plan_closed(over):
    inp = {"provenance": dict(CG.PROVENANCE), "planned_cells": [], "job_records": [], "artifacts": [],
           "release_intent": {"logical_component": "spock", "intended_version": "5.0.13", "intended_buildnum": "1",
                              "effective_tag": "v5.0.13", "channel": "staging", "simulated": False},
           "component_policy": dict(copy.deepcopy(SPOCK), **over), "publication_results": {}}
    plan = CP.reduce(inp)
    assert plan["plan_resolved"] is False
    assert any(e.startswith("component_policy: ") for e in plan["errors"]), plan["errors"]


def test_series_comes_from_the_release_version_not_a_default():
    # the same RPM cell content certifies only under the release version whose series it carries
    c = cell("rpm", "el-9", "amd64", "17", [member("rpm", "el-9", "amd64", name("rpm", "17", "60"), version="6.0.0")])
    _, plan6, _ = certify([c], version="6.0.0")
    assert runs(plan6) == [(name("rpm", "17", "60"), "rpm", "17", "rocky9-amd64")]
    _, plan5, _ = certify([c], version="5.0.13")
    assert plan5["matrix"]["include"] == []
    assert CG.gaps(plan5)[0][3:] == (P.GAP_NO_RUNTIME_TARGET, "series_mismatch")
