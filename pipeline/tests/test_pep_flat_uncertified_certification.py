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
  * source packages stay silent, and a runtime package the policy does not name stays the silent
    ``package_not_allowed`` exclusion it always was (plain flat entries are unchanged);
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


def certify(cells, policy=None, *, mode="full", simulated=False):
    jobs, arts = [], []
    for i, c in enumerate(cells):
        cid = c["cell"]["cell_id"]
        jobs.append({"cell_id": cid, "job_id": i + 1, "run_attempt": 1, "status": "completed",
                     "conclusion": "success"})
        arts.append({"name": cid, "id": 100 + i, "members": c["members"]})
    publication = {"rpm": "skipped", "deb": "skipped"} if simulated else {"rpm": "success", "deb": "success"}
    cert_plan = CP.reduce({
        "provenance": dict(CG.PROVENANCE),
        "release_intent": {"logical_component": "rag", "intended_version": "2.0.0", "intended_buildnum": "1",
                           "effective_tag": "v2.0.0", "channel": "staging", "simulated": simulated},
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
    # requires a helper package), so the wording claims only that PEP does not certify it.
    section = html[html.index('<h2 id="uncertified">'):]
    section = section[:section.index("</table></div>")]
    md = (Path(tmp_path) / "s.md").read_text()
    for text in (section, md):
        assert "does not certify" in text and "not install" not in text and "not installed" not in text, text


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
