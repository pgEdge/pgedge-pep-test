"""End-to-end package-proof outcomes: the REAL summarizer CLI (fed the side files a
pep-integration leg writes, exactly as its Summarize step passes them) -> the REAL
cert-result reducer -> the REAL gate, in both enforcement modes. The install evidence is
the real run- and target-bound marker, and the marker-binding test drives the REAL
component install and identity tests (loaded as test_pep_rag_wiring loads them). Plus the
workflow contract that carries the verified digest from install-evidence.json into the
summary."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, str(_HERE / (name + ".py")))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


RS = _load("pep_result_summary")
CR = _load("pep_cert_result")
G = _load("pep_cert_gate")
import test_pep_rag_wiring as W                  # the real component test, Docker neutralized

IID = "rag-debian12-amd64-pg17-0123456789abcdef"
PKG = "pgedge-rag-server2"
PIN = "2.0.0-1.bookworm"
RUN_TOKEN = "100-1"                              # the bridge's GITHUB_RUN_ID-GITHUB_RUN_ATTEMPT
PLANNED = "c" * 64
OTHER = "e" * 64
SHA = "a" * 40
PEP = "b" * 40
PASS_XML = ('<testsuite name="s" tests="3" failures="0" errors="0" skipped="1">'
            '<testcase name="a"/><testcase name="b"/><testcase name="c"><skipped/></testcase></testsuite>')
FAIL_XML = ('<testsuite name="s" tests="3" failures="1" errors="0" skipped="0">'
            '<testcase name="a"/><testcase name="b"><failure/></testcase><testcase name="c"/></testsuite>')
PROVEN = {"l2a": "proven", "l2b": "not_attempted", "l1": "proven"}      # the RAG replay's shape
# what record_precondition_failure writes when the install never succeeded
PRECONDITION_FAILED = {"l2a": "not_proven", "l2b": "not_attempted", "l1": "not_attempted"}
VERSION_MISMATCH = {"l2a": "not_proven", "l2b": "not_attempted", "l1": "proven"}


def _plan(expected_binary=""):
    inv = {
        "invocation_id": IID, "component": "rag", "package_name": "pgedge-rag-server2",
        "channel": "staging", "expected_version": "2.0.0", "container_alias": "debian12-amd64",
        "pg_major": "17", "family": "deb", "arch": "amd64", "expected_buildnum": "1",
        "effective_tag": "v2.0.0", "expected_rpm": "", "expected_deb": "2.0.0-1.bookworm",
        "expected_binary": expected_binary,
        "package": {"name": "pgedge-rag-server2", "version": "2.0.0", "release": "1.bookworm",
                    "sha256": PLANNED, "native_arch": "amd64"},
        "source_cell_id": "pepcell.v1.deb.bookworm.amd64.pkg", "source_target_id": "t1",
        "producer_repo": "pgEdge/pgedge-rag-server",
    }
    return {"schema": "pep-invocation-plan/1", "plan_resolved": True, "errors": [],
            "execution_mode": "full",
            "provenance": {"repository": "pgEdge/pgedge-rag-server", "run_id": "100",
                           "run_attempt": "1", "sha": SHA, "ref": "refs/tags/v2.0.0"},
            "release": {"logical_component": "rag", "channel": "staging", "intended_version": "2.0.0",
                        "intended_buildnum": "1", "effective_tag": "v2.0.0"},
            "matrix": {"include": [inv]}, "coverage_gaps": []}


def _request():
    """The normalized integration request pep-integration builds for _plan()'s invocation."""
    return W._int_req(PEP_PACKAGE_NAME=PKG, PEP_CHANNEL="staging", PEP_EXPECTED_VERSION="2.0.0",
                      PEP_FAMILY="deb", PEP_ARCH_FILTER="amd64", PEP_CONTAINER_ALIAS="debian12-amd64",
                      PG_MAJOR_VERSION="17", PEP_EXPECTED_DEB=PIN)


def _summarize(d, mode, *, xml, identity_path=None, install_path=None, preview=False):
    """Run the real summarizer CLI the way pep-integration.yml's Summarize step does:
    each side file is passed only if the leg wrote it."""
    (d / "report.xml").write_text(xml)
    (d / "provenance.json").write_text(json.dumps({
        "caller_repo": "pgEdge/pgedge-rag-server", "caller_sha": SHA, "caller_ref": "refs/tags/v2.0.0",
        "caller_run_id": "100", "caller_run_attempt": "1",
        "pep_requested_ref": PEP, "pep_resolved_sha": PEP}))
    args = ["--out", str(d / "summary.json"), "--mode", mode, "--invocation-id", IID,
            "--provenance-json", str(d / "provenance.json")]
    if preview:
        args += ["--preview"]
    else:
        args += ["--reports", str(d / "report.xml")]
        if identity_path and Path(identity_path).exists():
            args += ["--identity-json", str(identity_path)]
        if install_path and Path(install_path).exists():
            args += ["--install-json", str(install_path)]
    RS.main(args)
    return json.loads((d / "summary.json").read_text())


def _leg_summary(tmp_path, mode, *, xml=PASS_XML, identity=PROVEN, digest=PLANNED, preview=False):
    """A leg with the given identity outcome. Its install evidence is the real marker the
    component test writes after a verified install, bound to this run and target;
    digest=None means the install failed before writing one."""
    d = tmp_path / mode
    d.mkdir(parents=True)
    (d / "identity-evidence.json").write_text(json.dumps(identity))
    inst = d / "install-evidence.json"
    if digest is not None and not preview:
        W.rag.pep_evidence.write_install_evidence(_request(), RUN_TOKEN, "pinned", PIN, str(inst),
                                                  installed_sha256=digest)
    return _summarize(d, mode, xml=xml, identity_path=d / "identity-evidence.json",
                      install_path=inst, preview=preview)


def _certify(tmp_path, *, expected_binary="", **leg):
    out = {}
    for mode in ("observe", "gate"):
        result = CR.build_cert_result(_plan(expected_binary), [_leg_summary(tmp_path, mode, **leg)], "1")
        dec = G.decide(result, mode)
        out[mode] = (dec["certification_state"], dec["policy_decision"], dec["workflow_conclusion"],
                     dec["reason_code"])
        out[mode + "_leg"] = result["legs"][0]
    return out


BLOCK = ("incomplete", "block", "failure")


@pytest.mark.parametrize("case,leg,observe,gate", [
    ("digest matches, tests pass", {},
     ("pass", "allow", "success", "clean_pass"), ("pass", "allow", "success", "clean_pass")),
    ("digest matches, product test fails", {"xml": FAIL_XML},
     ("fail", "report", "success", "product_fail"), ("fail", "block", "failure", "product_fail")),
    ("digest mismatch", {"digest": OTHER},
     BLOCK + ("package_digest_mismatch",), BLOCK + ("package_digest_mismatch",)),
    ("digest mismatch, product test fails", {"digest": OTHER, "xml": FAIL_XML},
     BLOCK + ("package_digest_mismatch",), BLOCK + ("package_digest_mismatch",)),
    ("digest absent", {"digest": None},
     BLOCK + ("package_digest_missing",), BLOCK + ("package_digest_missing",)),
    ("pinned download failed (install test failed, no install evidence)",
     {"digest": None, "xml": FAIL_XML, "identity": PRECONDITION_FAILED},
     BLOCK + ("package_digest_missing",), BLOCK + ("package_digest_missing",)),
    ("version mismatch (l2a not proven)", {"xml": FAIL_XML, "identity": VERSION_MISMATCH},
     BLOCK + ("identity_unproven",), BLOCK + ("identity_unproven",)),
    ("preview", {"preview": True},
     ("preview", "report", "success", "preview"), ("preview", "block", "failure", "preview")),
])
def test_outcome_table(tmp_path, case, leg, observe, gate):
    got = _certify(tmp_path, **leg)
    assert (got["observe"], got["gate"]) == (observe, gate), case


def test_an_integrity_problem_never_leaves_observe_green(tmp_path):
    got = _certify(tmp_path, digest=OTHER, xml=FAIL_XML)
    assert got["observe"][2] == "failure"
    leg = got["observe_leg"]
    # the product failure is still visible beneath the integrity problem
    assert (leg["test_verdict"], leg["counts"]["failures"]) == ("fail", 1)


def test_l2b_is_required_only_when_an_expected_binary_is_planned(tmp_path):
    assert _certify(tmp_path)["gate"][3] == "clean_pass"                   # not_attempted, not planned
    planned = _certify(tmp_path / "b", expected_binary="2.0.0")
    assert planned["gate"][3] == "identity_unproven"
    assert planned["gate_leg"]["unproven_identity_rungs"] == ["l2b"]


def _alter_marker(path, how):
    """A marker this run did not write for this target: left by an earlier run, written
    for another target or version, or never written."""
    if how == "absent":
        Path(path).unlink()
        return
    marker = json.loads(Path(path).read_text())
    if how == "stale_run":
        marker["run_token"] = "99-1"
    elif how == "wrong_target":
        marker["target"]["container_alias"] = "debian13-amd64"
    elif how == "wrong_version":
        marker["expected_version"] = "1.9.0"
    Path(path).write_text(json.dumps(marker))


@pytest.mark.parametrize("marker,reason", [
    ("current", "clean_pass"),
    ("stale_run", "identity_unproven"),
    ("wrong_target", "identity_unproven"),
    ("wrong_version", "identity_unproven"),
    ("absent", "package_digest_missing"),
])
def test_only_this_runs_marker_for_this_target_can_certify(monkeypatch, tmp_path, marker, reason):
    # The digest in every altered marker still MATCHES the plan. The real identity test
    # checks the marker's run token, target and expected version before observing
    # identity; on a mismatch it records l1=not_attempted and fails, so the reducer
    # refuses the leg whatever digest the summarizer carried from that marker.
    req = _request()
    got = {}
    for mode in ("observe", "gate"):
        d = tmp_path / mode
        d.mkdir()
        inst, ident = W._wire_integration(monkeypatch, d, req, run_token=RUN_TOKEN)
        monkeypatch.setattr(W.rag.package_management, "install_pinned", W._Spy((True, "ok", PLANNED)))
        W.rag.test_rag_component_install("c1", "deb", PKG)            # writes the real marker
        if marker != "current":
            _alter_marker(inst, marker)
        monkeypatch.setattr(W.rag.package_management, "query_installed_version", lambda c, p: PIN)
        monkeypatch.setattr(W.rag.package_management, "query_binary_version", lambda c, p: "Version: 2.0.0")
        try:
            W.rag.test_rag_identity("c1", "deb", PKG)
            xml = PASS_XML
        except (AssertionError, pytest.fail.Exception):
            xml = FAIL_XML                                             # the identity test failed
        summary = _summarize(d, mode, xml=xml, identity_path=ident, install_path=inst)
        result = CR.build_cert_result(_plan(), [summary], "1")
        dec = G.decide(result, mode)
        got[mode] = (dec["reason_code"], summary["installed_package_sha256"])
    digest = None if marker == "absent" else PLANNED
    assert got == {"observe": (reason, digest), "gate": (reason, digest)}


# --------------------------------------------------------------------------- #
# workflow contract
# --------------------------------------------------------------------------- #
_WF = _HERE.parent / ".github" / "workflows"


def test_integration_summarize_step_passes_install_evidence_when_present():
    text = (_WF / "pep-integration.yml").read_text()
    step = text[text.index("- name: Summarize (ALWAYS)"):text.index("- name: Emit outputs")]
    assert re.search(r"\[ -f test-logs/install-evidence\.json \]\s+&& args\+=\( "
                     r"--install-json test-logs/install-evidence\.json \)", step)


def test_no_expected_digest_workflow_input_was_added():
    # The digest is compared by the reducer against the plan; no leg needs it as input.
    for wf in ("pep-integration.yml", "pep-certify.yml"):
        assert "sha256" not in (_WF / wf).read_text().split("jobs:", 1)[0], wf
