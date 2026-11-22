"""A flat (PG-independent) component that builds several runtime packages per cell but certifies one.

Products such as an MCP server (server + CLI + an arch-independent web UI) or a workbench (server +
agents + a noarch client) build every package in each OS/arch cell. PEP's flat policy names the one
package the component's test certifies (``allowed_runtime_package_names``); the optional
``uncertified_runtime_package_names`` (with a required ``uncertified_reason``) names the other runtime
packages the release builds but PEP does not test. Through the REAL chain (cert-plan reducer ->
invocation planner -> cert-result reducer -> gate -> report) this proves:
  * only the certified package becomes a test target; the named companions are listed once per cell
    under "Runtime packages not certified by policy" with the policy's reason, are never coverage
    gaps, and do not block or degrade the decision -- including an arch-independent (noarch/all)
    companion that each arch cell builds;
  * a runtime package such an entry does NOT name is never certified and never silently dropped: the
    cert-plan records it as ``package_unclassified``, the invocation planner makes it a member-scope
    ``unclassified_runtime_package`` coverage gap, so coverage is partial and the existing policy
    applies (observe reports, gate blocks), and the HTML and Markdown reports show it as a gap;
  * source and debug packages stay silent policy exclusions, and a plain flat entry (no companion
    list) keeps an unnamed runtime package as the silent ``package_not_allowed`` exclusion it always was;
  * a malformed flat-uncertified policy fails the cert-plan closed.
Versions and companion names are synthetic. The invocation planner's backstop validates every target
against the REGISTERED policy of the release's component, so the certified package here is rag's
registered ``pgedge-rag-server2`` with synthetic companions; the registered multi-package entries are
covered through the same chain by their own tests.
"""
import copy
from pathlib import Path

import pytest

import pep_cert_gate as G
import pep_cert_plan as CP
import pep_cert_report as R
import pep_cert_result as CR
import pep_invocation_plan as P
import test_pep_coverage_gaps as CG

SERVER = "pgedge-rag-server2"
POLICY = {"allowed_runtime_package_names": [SERVER],
          "uncertified_runtime_package_names": ["p-cli", "p-web"],
          "uncertified_reason": "built beside the server; the test installs only the server",
          "expected_binary_version": ""}
CLEAN = {"observe": ("pass", "allow", "success", "clean_pass"), "gate": ("pass", "allow", "success", "clean_pass")}


def members(family, os_token, arch):
    noarch = "noarch" if family == "rpm" else "all"
    return [CG.pkg(family, os_token, arch, name=SERVER),
            CG.pkg(family, os_token, arch, name="p-cli"),
            CG.pkg(family, os_token, arch, name="p-web", native=noarch,
                   sha=("web" + os_token + arch).encode().hex()[:64].ljust(64, "0")),
            CG.pkg(family, os_token, arch, name=SERVER, cls="source", native="src", sha="5" * 64,
                   path="p-server-2.0.0.src.rpm")][: 4 if family == "rpm" else 3]


def certify(cells, policy=None, *, mode="full", simulated=False, component="rag"):
    jobs, arts = [], []
    for i, c in enumerate(cells):
        cid = c["cell"]["cell_id"]
        jobs.append({"cell_id": cid, "job_id": i + 1, "run_attempt": 1, "status": "completed",
                     "conclusion": "success"})
        arts.append({"name": cid, "id": 100 + i, "members": c["members"]})
    publication = {"rpm": "skipped", "deb": "skipped"} if simulated else {"rpm": "success", "deb": "success"}
    cert_plan = CP.reduce({
        "provenance": dict(CG.PROVENANCE),
        "release_intent": {"logical_component": component, "intended_version": "2.0.0",
                           "intended_buildnum": "1", "effective_tag": "v2.0.0", "channel": "staging",
                           "simulated": simulated},
        "component_policy": copy.deepcopy(POLICY if policy is None else policy),
        "planned_cells": [c["cell"] for c in cells], "job_records": jobs, "artifacts": arts,
        "publication_results": publication, "execution_mode": mode})
    assert cert_plan["plan_resolved"] is True, cert_plan["errors"]
    plan = P.build_invocation_plan(cert_plan, CG.EXEC_CATALOG, CG.ENABLED, execution_mode=mode)
    assert plan["plan_resolved"] is True, plan["errors"]
    decided = {}
    for enforcement in ("observe", "gate"):
        summaries = [CG._summary(inv, "pass", enforcement, preview=(mode == "preview"))
                     for inv in plan["matrix"]["include"]]
        result = CR.build_cert_result(plan, summaries, "1")
        assert result["result_resolved"] is True, result["errors"]
        decided[enforcement] = (result, G.decide(result, enforcement))
    return cert_plan, plan, decided


def cells():
    return [CG.planned("rpm", "el-9", "amd64", members=members("rpm", "el-9", "amd64")),
            CG.planned("deb", "trixie", "amd64", members=members("deb", "trixie", "amd64"))]


def test_only_the_certified_package_is_tested_and_companions_are_listed_with_the_reason(tmp_path):
    cp, plan, decided = certify(cells())
    assert sorted((i["package_name"], i["family"]) for i in plan["matrix"]["include"]) == \
        [(SERVER, "deb"), (SERVER, "rpm")]
    reasons = {(c["family"], m["package_name"], m.get("native_arch")): m.get("exclusion_reasons")
               for c in cp["cells"] for m in c["members"]}
    assert reasons[("rpm", "p-cli", "x86_64")] == ["runtime_not_certified"]
    assert reasons[("rpm", "p-web", "noarch")] == ["runtime_not_certified"]
    assert reasons[("deb", "p-web", "all")] == ["runtime_not_certified"]
    assert "runtime_not_certified" not in reasons[("rpm", SERVER, "src")]      # source stays silent
    assert cp["component_policy"]["uncertified_runtime_package_names"] == ["p-cli", "p-web"]
    assert cp["component_policy"]["uncertified_reason"] == POLICY["uncertified_reason"]
    listed = sorted((u["family"], u["physical_package"], u["arch"], u["build_pg_major"], u["reason"])
                    for u in plan["uncertified_runtime_packages"])
    assert listed == [(f, n, "amd64", None, POLICY["uncertified_reason"])
                      for f in ("deb", "rpm") for n in ("p-cli", "p-web")]
    assert plan["coverage_gaps"] == []
    CG.assert_reconciles(cp, plan)
    assert CG.outcomes(decided) == CLEAN
    result, decision = decided["gate"]
    assert result["uncertified_runtime_packages"] == plan["uncertified_runtime_packages"]
    assert result["coverage_status"] == "complete"
    R.render_report(result, decision, {}, Path(tmp_path) / "out", markdown_path=Path(tmp_path) / "s.md")
    html = (Path(tmp_path) / "out" / "consolidated-report.html").read_text()
    assert "Runtime packages not certified by policy (4)" in html and "p-web" in html and "p-cli" in html
    # A companion may still be installed as a dependency of the package under test (a server that
    # requires a helper package), and its build receipt does record its identity and checksum, so the
    # wording claims only that PEP does not certify it or verify it as an installed target.
    section = html[html.index('<h2 id="uncertified">'):]
    section = section[:section.index("</table></div>")]
    md = (Path(tmp_path) / "s.md").read_text()
    md_line = next(line for line in md.splitlines() if line.startswith("**Not certified by policy:**"))
    for text in (section, md_line):
        assert "does not certify" in text and "not install" not in text and "not installed" not in text, text
        assert "build receipts record their identities and checksums" in text, text
        assert "does not verify them as installed certification targets" in text, text
        assert "no identity or checksum" not in text and "no checksum" not in text, text
    assert "may still be installed as a dependency of the package under test" in section
    # one listed package reads in the singular
    one = dict(result, uncertified_runtime_packages=result["uncertified_runtime_packages"][:1])
    R.render_report(one, decision, {}, Path(tmp_path) / "one", markdown_path=Path(tmp_path) / "one.md")
    one_line = next(line for line in (Path(tmp_path) / "one.md").read_text().splitlines()
                    if line.startswith("**Not certified by policy:**"))
    assert ("build receipt records its identity and checksum" in one_line
            and "does not verify it as an installed certification target" in one_line), one_line


UNNAMED = "p-agent"
GAP = "unclassified_runtime_package"


def _with(extra):
    """cells() with ``extra(family, os_token, arch)`` members added to every cell."""
    return [CG.planned(f, o, "amd64", members=members(f, o, "amd64") + extra(f, o, "amd64"))
            for f, o in (("rpm", "el-9"), ("deb", "trixie"))]


def _render(tmp_path, decided, mode):
    result, decision = decided[mode]
    out = Path(tmp_path) / mode
    R.render_report(result, decision, {}, out / "html", markdown_path=out / "summary.md")
    html = (out / "html" / "consolidated-report.html").read_text()
    md = (out / "summary.md").read_text()
    return html, md


def _html_section(html, anchor):
    start = html.find('<h2 id="%s">' % anchor)
    if start < 0:
        return ""
    return html[start:html.index("</table></div>", start)]


def _md_gap_rows(md):
    return [line for line in md.splitlines() if line.startswith("| ") and "gap:" in line]


def _assert_unnamed_is_a_gap_everywhere(tmp_path, cp, plan, decided, unnamed, server, companions):
    # capture/planning: the cert-plan keeps the member, unselected, with its own reason
    recs = [m for c in cp["cells"] for m in c["members"] if m["package_name"] == unnamed]
    assert len(recs) == 2 and all(m["selected"] is False and m["exclusion_reasons"] == ["package_unclassified"]
                                  for m in recs), recs
    # invocation planning: the certified package is still the only target; one member gap per cell
    assert sorted((i["package_name"], i["family"]) for i in plan["matrix"]["include"]) == \
        [(server, "deb"), (server, "rpm")]
    assert sorted((g["family"], g["scope"], g["physical_package"], g["reason"]) for g in plan["coverage_gaps"]) == \
        [("deb", "member", unnamed, GAP), ("rpm", "member", unnamed, GAP)]
    assert all(g["detail"].startswith("package_unclassified; path=") for g in plan["coverage_gaps"])
    assert {u["physical_package"] for u in plan["uncertified_runtime_packages"]} == set(companions)
    assert plan["counts"]["gaps_by_scope"]["member"] == 2
    CG.assert_reconciles(cp, plan)
    # result: partial coverage carrying the gaps; the decision is the existing partial-coverage policy
    for mode in ("observe", "gate"):
        result, _ = decided[mode]
        assert result["coverage_status"] == "partial" and result["test_verdict"] == "pass"
        assert sorted(g["physical_package"] for g in result["coverage_gaps"]) == [unnamed, unnamed]
        assert unnamed not in {u["physical_package"] for u in result["uncertified_runtime_packages"]}
    assert CG.outcomes(decided) == CG.PARTIAL
    # HTML and Markdown: listed as a gap, never as a not-certified companion
    for mode in ("observe", "gate"):
        html, md = _render(tmp_path, decided, mode)
        gaps = _html_section(html, "gaps")
        rows = [r for r in gaps.split('<tr id="gap-')[1:] if "<code>%s</code>" % unnamed in r]
        assert len(rows) == 2 and all("UNCLASSIFIED RUNTIME PACKAGE" in r and "names neither as certified" in r
                                      for r in rows), gaps
        assert unnamed not in _html_section(html, "uncertified")
        rows = _md_gap_rows(md)
        assert "**Coverage gaps (2)**" in md and len([r for r in rows if unnamed in r]) == 2, md
        unc = [line for line in md.splitlines() if line.startswith("**Not certified by policy:**")]
        assert len(unc) == 1 and unnamed not in unc[0] and all(c in unc[0] for c in companions)


def test_an_unnamed_runtime_package_beside_declared_companions_is_a_visible_gap(tmp_path):
    cp, plan, decided = certify(_with(lambda f, o, a: [CG.pkg(f, o, a, name=UNNAMED)]))
    _assert_unnamed_is_a_gap_everywhere(tmp_path, cp, plan, decided, UNNAMED, SERVER, ["p-cli", "p-web"])


def test_an_unnamed_arch_independent_package_is_a_gap_in_every_cell_that_builds_it():
    cp, plan, _ = certify(_with(lambda f, o, a: [CG.pkg(f, o, a, name="p-docs",
                                                        native="noarch" if f == "rpm" else "all")]))
    assert sorted((g["family"], g["physical_package"], g["reason"]) for g in plan["coverage_gaps"]) == \
        [("deb", "p-docs", GAP), ("rpm", "p-docs", GAP)]


@pytest.mark.parametrize("cls,native", [("debug", None), ("source", "src")])
def test_unnamed_debug_and_source_packages_stay_silent_policy_exclusions(cls, native):
    cp, plan, decided = certify(_with(lambda f, o, a: [CG.pkg(f, o, a, name=UNNAMED + "-dbg", cls=cls,
                                                              native=native, sha="d" * 64)]))
    recs = [m for c in cp["cells"] for m in c["members"] if m["package_name"] == UNNAMED + "-dbg"]
    assert recs and all("non_runtime" in m["exclusion_reasons"] for m in recs)
    assert plan["coverage_gaps"] == [] and CG.outcomes(decided) == CLEAN


def test_a_cell_with_no_certified_package_names_the_unclassified_one_in_its_gap():
    only = [CG.planned("rpm", "el-9", "amd64", members=[CG.pkg("rpm", "el-9", "amd64", name="p-cli"),
                                                        CG.pkg("rpm", "el-9", "amd64", name=UNNAMED)]),
            CG.planned("deb", "trixie", "amd64", members=members("deb", "trixie", "amd64"))]
    cp, plan, decided = certify(only)
    assert [(g["scope"], g["reason"], g["detail"]) for g in plan["coverage_gaps"]] == \
        [("cell", "no_runtime_target", "package_unclassified, runtime_not_certified")]
    # the one cell gap names the unclassified file; the listed companion stays in its own section
    [gap] = plan["coverage_gaps"]
    assert [(r["physical_package"], r["reasons"]) for r in gap["rejected_packages"]] == \
        [(UNNAMED, ["package_unclassified"])]
    assert gap["rejected_packages"][0]["path"] and gap["rejected_packages"][0]["sha256"]
    assert CG.outcomes(decided) == CG.PARTIAL


def test_an_unnamed_runtime_package_is_a_gap_in_a_preview_too():
    _, plan, decided = certify(_with(lambda f, o, a: [CG.pkg(f, o, a, name=UNNAMED)]),
                               mode="preview", simulated=True)
    assert sorted((g["family"], g["physical_package"], g["reason"]) for g in plan["coverage_gaps"]) == \
        [("deb", UNNAMED, GAP), ("rpm", UNNAMED, GAP)]
    assert {m: d["reason_code"] for m, (_, d) in decided.items()} == {"observe": "preview", "gate": "preview"}


def test_a_preview_lists_the_same_companions_without_claiming_publication():
    _, plan, decided = certify(cells(), mode="preview", simulated=True)
    assert {u["physical_package"] for u in plan["uncertified_runtime_packages"]} == {"p-cli", "p-web"}
    assert all(i["package_name"] == SERVER for i in plan["matrix"]["include"])


def test_a_plain_flat_entry_keeps_unnamed_runtime_packages_silent():
    plain = {"allowed_runtime_package_names": [SERVER], "expected_binary_version": ""}
    cp, plan, decided = certify(cells(), plain)
    assert plan["uncertified_runtime_packages"] == [] and plan["coverage_gaps"] == []
    assert {tuple(m["exclusion_reasons"]) for c in cp["cells"] for m in c["members"]
            if m["package_name"] in ("p-cli", "p-web")} == {("package_not_allowed",)}
    assert "uncertified_runtime_package_names" not in cp["component_policy"]
    assert CG.outcomes(decided) == CLEAN


@pytest.mark.parametrize("over", [
    {"uncertified_reason": None},
    {"uncertified_runtime_package_names": ["p-cli", SERVER]},
    {"uncertified_runtime_package_names": []},
], ids=["no-reason", "also-certified", "empty"])
def test_a_malformed_flat_uncertified_policy_fails_the_cert_plan_closed(over):
    policy = {k: v for k, v in dict(POLICY, **over).items() if v is not None}
    jobs = [{"cell_id": c["cell"]["cell_id"], "job_id": i + 1, "run_attempt": 1, "status": "completed",
             "conclusion": "success"} for i, c in enumerate(cells())]
    cp = CP.reduce({
        "provenance": dict(CG.PROVENANCE),
        "release_intent": {"logical_component": "rag", "intended_version": "2.0.0", "intended_buildnum": "1",
                           "effective_tag": "v2.0.0", "channel": "staging", "simulated": False},
        "component_policy": policy, "planned_cells": [c["cell"] for c in cells()], "job_records": jobs,
        "artifacts": [{"name": c["cell"]["cell_id"], "id": 100 + i, "members": c["members"]}
                      for i, c in enumerate(cells())],
        "publication_results": {"rpm": "success", "deb": "success"}, "execution_mode": "full"})
    assert cp["plan_resolved"] is False
    assert any("uncertified" in e for e in cp["errors"]), cp["errors"]


# --------------------------------------------------------------------------- #
# the registered multi-package products, through the same chain
# --------------------------------------------------------------------------- #
import pep_request as PR                                       # noqa: E402

REGISTERED = sorted(c for c, e in PR.COMPONENT_POLICY.items() if "uncertified_runtime_package_names" in e)


def test_registered_multi_package_products_are_exactly_mcp_and_the_workbench():
    assert REGISTERED == ["ai_db_workbench", "mcp"]


@pytest.mark.parametrize("comp", REGISTERED)
def test_a_registered_product_certifies_its_server_and_lists_every_companion(comp):
    entry = PR.COMPONENT_POLICY[comp]
    [server] = entry["allowed_runtime_package_names"]
    companions = entry["uncertified_runtime_package_names"]

    def built(family, os_token, arch):
        out = [CG.pkg(family, os_token, arch, name=server)]
        for i, n in enumerate(companions):
            native = ("noarch" if family == "rpm" else "all") if n.endswith(("-web", "-client")) else None
            out.append(CG.pkg(family, os_token, arch, name=n, native=native,
                              sha=("%d%s%s%s" % (i, n, os_token, arch)).encode().hex()[:64].ljust(64, "0")))
        return out

    planned = [CG.planned("rpm", "el-9", "amd64", members=built("rpm", "el-9", "amd64")),
               CG.planned("deb", "trixie", "amd64", members=built("deb", "trixie", "amd64"))]
    jobs = [{"cell_id": c["cell"]["cell_id"], "job_id": i + 1, "run_attempt": 1, "status": "completed",
             "conclusion": "success"} for i, c in enumerate(planned)]
    cp = CP.reduce({
        "provenance": dict(CG.PROVENANCE),
        "release_intent": {"logical_component": comp, "intended_version": "2.0.0", "intended_buildnum": "1",
                           "effective_tag": "v2.0.0", "channel": "staging", "simulated": False},
        "component_policy": copy.deepcopy(entry), "planned_cells": [c["cell"] for c in planned],
        "job_records": jobs, "artifacts": [{"name": c["cell"]["cell_id"], "id": 100 + i, "members": c["members"]}
                                           for i, c in enumerate(planned)],
        "publication_results": {"rpm": "success", "deb": "success"}, "execution_mode": "full"})
    assert cp["plan_resolved"] is True, cp["errors"]
    plan = P.build_invocation_plan(cp, CG.EXEC_CATALOG, CG.ENABLED, execution_mode="full")
    assert plan["plan_resolved"] is True, plan["errors"]
    assert {(i["component"], i["package_name"]) for i in plan["matrix"]["include"]} == {(comp, server)}
    assert plan["coverage_gaps"] == []
    assert sorted((u["family"], u["physical_package"]) for u in plan["uncertified_runtime_packages"]) == \
        sorted((f, n) for f in ("rpm", "deb") for n in companions)
    assert {u["reason"] for u in plan["uncertified_runtime_packages"]} == {entry["uncertified_reason"]}


@pytest.mark.parametrize("comp", REGISTERED)
def test_a_registered_product_reports_an_unexpected_runtime_package_as_a_gap(comp, tmp_path):
    # Reproduces the reported defect: before, an extra runtime package left coverage complete, the
    # decision a clean pass and the report silent. The certified server is still the only target.
    entry = PR.COMPONENT_POLICY[comp]
    [server] = entry["allowed_runtime_package_names"]
    companions = entry["uncertified_runtime_package_names"]
    unexpected = server + "-unlisted"

    def built(family, os_token, arch):
        out = [CG.pkg(family, os_token, arch, name=server)]
        for i, n in enumerate(companions + [unexpected]):
            out.append(CG.pkg(family, os_token, arch, name=n,
                              sha=("%d%s%s%s" % (i, n, os_token, arch)).encode().hex()[:64].ljust(64, "0")))
        return out

    planned = [CG.planned("rpm", "el-9", "amd64", members=built("rpm", "el-9", "amd64")),
               CG.planned("deb", "trixie", "amd64", members=built("deb", "trixie", "amd64"))]
    cp, plan, decided = certify(planned, copy.deepcopy(entry), component=comp)
    _assert_unnamed_is_a_gap_everywhere(tmp_path, cp, plan, decided, unexpected, server, companions)




def _registered_cells(comp, unexpected, with_server):
    """One rpm and one deb cell of a registered product: its companions and ``unexpected`` in each,
    and the certified server only in the families named by ``with_server``."""
    entry = PR.COMPONENT_POLICY[comp]
    [server] = entry["allowed_runtime_package_names"]

    def built(family, os_token, arch):
        names = ([server] if family in with_server else []) + entry["uncertified_runtime_package_names"] \
            + [unexpected]
        return [CG.pkg(family, os_token, arch, name=n,
                       sha=("%d%s%s%s" % (i, n, os_token, arch)).encode().hex()[:64].ljust(64, "0"))
                for i, n in enumerate(names)]
    return [CG.planned("rpm", "el-9", "amd64", members=built("rpm", "el-9", "amd64")),
            CG.planned("deb", "trixie", "amd64", members=built("deb", "trixie", "amd64"))]


def _certify_registered(comp, planned):
    """The real chain for a registered product; unlike ``certify`` it accepts a plan with no target."""
    entry = PR.COMPONENT_POLICY[comp]
    jobs = [{"cell_id": c["cell"]["cell_id"], "job_id": i + 1, "run_attempt": 1, "status": "completed",
             "conclusion": "success"} for i, c in enumerate(planned)]
    cp = CP.reduce({
        "provenance": dict(CG.PROVENANCE),
        "release_intent": {"logical_component": comp, "intended_version": "2.0.0", "intended_buildnum": "1",
                           "effective_tag": "v2.0.0", "channel": "staging", "simulated": False},
        "component_policy": copy.deepcopy(entry), "planned_cells": [c["cell"] for c in planned],
        "job_records": jobs, "artifacts": [{"name": c["cell"]["cell_id"], "id": 100 + i, "members": c["members"]}
                                           for i, c in enumerate(planned)],
        "publication_results": {"rpm": "success", "deb": "success"}, "execution_mode": "full"})
    assert cp["plan_resolved"] is True, cp["errors"]
    plan = P.build_invocation_plan(cp, CG.EXEC_CATALOG, CG.ENABLED, execution_mode="full")
    assert plan["plan_resolved"] is True, plan["errors"]
    decided = {}
    for mode in ("observe", "gate"):
        summaries = [CG._summary(inv, "pass", mode, preview=False) for inv in plan["matrix"]["include"]]
        result = CR.build_cert_result(plan, summaries, "1")
        decided[mode] = (result, G.decide(result, mode))
    return cp, plan, decided


def _assert_names_in_both_reports(tmp_path, decided, unexpected, n_rows):
    for mode in ("observe", "gate"):
        html, md = _render(tmp_path, decided, mode)
        gaps = _html_section(html, "gaps")
        rows = [r for r in gaps.split('<tr id="gap-')[1:] if "<code>%s</code>" % unexpected in r]
        assert len(rows) == n_rows, gaps
        md_rows = [r for r in _md_gap_rows(md) if unexpected in r]
        assert len(md_rows) == n_rows, md
        assert unexpected not in _html_section(html, "uncertified")


@pytest.mark.parametrize("comp", REGISTERED)
def test_a_missing_server_cell_beside_a_valid_one_names_the_unexpected_package(comp, tmp_path):
    unexpected = PR.COMPONENT_POLICY[comp]["allowed_runtime_package_names"][0] + "-unlisted"
    cp, plan, decided = _certify_registered(comp, _registered_cells(comp, unexpected, with_server=("rpm",)))
    by_family = {g["family"]: g for g in plan["coverage_gaps"]}
    assert sorted(by_family) == ["deb", "rpm"] and len(plan["coverage_gaps"]) == 2
    # the valid cell: the server is tested and the unexpected file is its own member gap
    assert (by_family["rpm"]["scope"], by_family["rpm"]["reason"], by_family["rpm"]["physical_package"]) == \
        ("member", GAP, unexpected)
    # the cell without a server: ONE cell gap, now carrying the unexpected file's identity
    deb = by_family["deb"]
    assert (deb["scope"], deb["reason"], deb["physical_package"]) == ("cell", "no_runtime_target", None)
    assert [(r["physical_package"], r["reasons"]) for r in deb["rejected_packages"]] == \
        [(unexpected, ["package_unclassified"])]
    # accounting is unchanged: one gap per no-target cell, nothing counted twice
    assert plan["counts"]["gaps_by_scope"] == {"cell": 1, "target": 0, "member": 1}
    CG.assert_reconciles(cp, plan)
    assert CG.outcomes(decided) == CG.PARTIAL
    _assert_names_in_both_reports(tmp_path, decided, unexpected, n_rows=2)


@pytest.mark.parametrize("comp", REGISTERED)
def test_a_result_with_no_server_anywhere_names_the_unexpected_package_and_blocks(comp, tmp_path):
    unexpected = PR.COMPONENT_POLICY[comp]["allowed_runtime_package_names"][0] + "-unlisted"
    cp, plan, decided = _certify_registered(comp, _registered_cells(comp, unexpected, with_server=()))
    assert plan["matrix"]["include"] == []
    assert sorted((g["family"], g["scope"], g["reason"]) for g in plan["coverage_gaps"]) == \
        [("deb", "cell", "no_runtime_target"), ("rpm", "cell", "no_runtime_target")]
    assert all([r["physical_package"] for r in g["rejected_packages"]] == [unexpected]
               for g in plan["coverage_gaps"])
    assert plan["counts"]["gaps_by_scope"] == {"cell": 2, "target": 0, "member": 0}
    CG.assert_reconciles(cp, plan)
    # the existing fail-closed decision for a release with nothing to run, in both modes
    for mode in ("observe", "gate"):
        result, decision = decided[mode]
        assert (decision["certification_state"], decision["policy_decision"], decision["workflow_conclusion"]) \
            == ("incomplete", "block", "failure"), decision
    assert decided["observe"][1]["reason_code"] == decided["gate"][1]["reason_code"]
    _assert_names_in_both_reports(tmp_path, decided, unexpected, n_rows=2)
