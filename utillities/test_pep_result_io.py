"""Offline unit tests for utillities.pep_result_io (the local result collector).

No network: every test builds a normalized artifacts listing + an on-disk extraction
directory (as actions/download-artifact would leave it) and drives collect()/main()
against the committed attempt-aware reducer.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "pep_result_io", str(Path(__file__).parent / "pep_result_io.py")
)
rio = importlib.util.module_from_spec(_spec)
sys.modules["pep_result_io"] = rio
_spec.loader.exec_module(rio)

_PLAN_SHA = "a" * 40
_PEP_SHA = "b" * 40
PREFIX = rio.DEFAULT_NAME_PREFIX


# --------------------------------------------------------------------------- #
# builders (self-contained; mirror the reducer's contract)
# --------------------------------------------------------------------------- #
def _plan_prov(**over):
    p = {"repository": "pgEdge/pgedge-rag-server", "run_id": "123",
         "run_attempt": "1", "sha": _PLAN_SHA, "ref": "refs/tags/v2.0.0"}
    p.update(over)
    return p


def _caller_prov(**over):
    pp = _plan_prov()
    p = {"caller_repo": pp["repository"], "caller_run_id": pp["run_id"],
         "caller_run_attempt": pp["run_attempt"], "caller_sha": pp["sha"],
         "caller_ref": pp["ref"], "pep_requested_ref": _PEP_SHA, "pep_resolved_sha": _PEP_SHA}
    p.update(over)
    return p


def _inv(iid, *, pg="17", family="rpm"):
    return {
        "invocation_id": iid, "component": "rag", "package_name": "pgedge-rag-server2",
        "channel": "release", "expected_version": "2.0.0", "container_alias": "rocky9-amd64",
        "pg_major": pg, "family": family, "arch": "amd64",
        "expected_buildnum": "", "effective_tag": "v2.0.0",
        "expected_rpm": "2.0.0-1.el9", "expected_deb": "", "expected_binary": "",
        "package": {"name": "pgedge-rag-server2", "version": "2.0.0", "release": "1.el9",
                    "sha256": "d" * 64, "native_arch": "x86_64"},
        "source_cell_id": "pepcell.v1.rpm.el-9.amd64.pkg", "source_target_id": "tgt-001",
        "producer_repo": "pgEdge/pgedge-rag-server",
    }


def _plan(include, *, prov=None, gaps=None):
    return {
        "schema": "pep-invocation-plan/1", "plan_resolved": True, "errors": [],
        "provenance": _plan_prov() if prov is None else prov,
        "release": {"logical_component": "rag", "channel": "release", "intended_version": "2.0.0",
                    "intended_buildnum": None, "effective_tag": "v2.0.0"},
        "supported_pg_majors": ["16", "17", "18"],
        "matrix": {"include": include},
        "coverage_gaps": gaps or [],
        "counts": {"eligible_targets": len(include), "covered_targets": len(include),
                   "coverage_gaps": len(gaps or []), "invocations": len(include)},
    }


def _consistent_counts(execution_status, verdict):
    if execution_status in ("preview", "infra_failure"):
        return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if verdict == "fail":
        return {"tests": 3, "failures": 1, "errors": 0, "skipped": 0}
    if verdict == "pass":
        return {"tests": 3, "failures": 0, "errors": 0, "skipped": 0}
    return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}


def _summary(iid, *, execution_status="completed", verdict="pass", mode="observe",
             provenance=None, counts=None):
    return {
        "invocation_id": iid, "execution_status": execution_status, "test_verdict": verdict,
        "enforcement_mode": mode,
        "identity_evidence": {"l2a": "proven", "l2b": "proven", "l1": "proven"},
        "counts": _consistent_counts(execution_status, verdict) if counts is None else counts,
        "provenance": _caller_prov() if provenance is None else provenance,
        # a verified full-mode install records the planned digest (_inv's "d"*64)
        "installed_package_sha256": None if execution_status == "preview" else "d" * 64,
    }


def _artifact(aid, name, *, expired=False, size=100, **extra):
    a = {"id": aid, "name": name, "expired": expired, "size_in_bytes": size}
    a.update(extra)
    return a


# --- filesystem layout helpers (mimic actions/download-artifact) ------------ #
def _write_flat(ddir, obj):
    """Single-artifact FLAT extraction: summary.json at the download root."""
    _write(Path(ddir) / "summary.json", obj)


def _write_named(ddir, artifact_name, obj, *, at_root=True, nested=False):
    """Per-artifact-name subdirectory extraction. at_root writes the root summary;
    nested additionally (or exclusively) writes one a level deeper."""
    d = Path(ddir) / artifact_name
    d.mkdir(parents=True, exist_ok=True)
    if at_root:
        _write(d / "summary.json", obj)
    if nested:
        sub = d / "logs"
        sub.mkdir(exist_ok=True)
        _write(sub / "summary.json", obj)
    return d


def _write(path, obj):
    Path(path).write_text(obj if isinstance(obj, str) else json.dumps(obj))


def _run(plan, listing, ddir, attempt="1", prefix=PREFIX):
    """Full offline pipeline: collect -> reducer -> ledger, returning
    (result, ledger, summaries, exit_code)."""
    summaries, cands, meta = rio.collect(listing, str(ddir), prefix)
    ledger = rio.build_ledger(cands, meta, attempt, prefix)
    result = rio.CR.build_cert_result(plan, summaries, attempt)
    exit_code = 0 if result.get("result_resolved") else 1
    return result, ledger, summaries, exit_code


def _byname(ledger, name):
    for c in ledger["candidates"]:
        if c["artifact_name"] == name:
            return c
    return None


# --------------------------------------------------------------------------- #
# 1. happy single-artifact flat extraction
# --------------------------------------------------------------------------- #
def test_happy_single_flat(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_flat(tmp_path, _summary("rag-a-pg17-aaaa"))
    listing = [_artifact(11, name)]
    result, ledger, summaries, code = _run(plan, listing, tmp_path)
    assert code == 0
    assert result["result_resolved"] is True
    assert result["execution_status"] == "completed"
    assert result["coverage_status"] == "complete"
    assert ledger["collection_status"] == "ok"
    c = _byname(ledger, name)
    assert c["extraction"] == "one_summary"
    assert c["summary_count"] == 1
    assert c["source_path"] == "summary.json"          # flat root
    assert ledger["counts"]["ingested"] == 1


# --------------------------------------------------------------------------- #
# 2. happy multiple-artifact named directories
# --------------------------------------------------------------------------- #
def test_happy_multiple_named(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    na = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    nb = PREFIX + "rag-b-pg18-bbbb-r123-a1"
    _write_named(tmp_path, na, _summary("rag-a-pg17-aaaa"))
    _write_named(tmp_path, nb, _summary("rag-b-pg18-bbbb"))
    listing = [_artifact(11, na), _artifact(22, nb)]
    result, ledger, summaries, code = _run(plan, listing, tmp_path)
    assert code == 0
    assert result["result_resolved"] is True
    assert result["coverage_status"] == "complete"
    assert result["counts"]["matched"] == 2
    assert _byname(ledger, na)["source_path"] == "%s/summary.json" % na
    assert ledger["counts"]["ingested"] == 2


# --------------------------------------------------------------------------- #
# 3. rerun-failed: prior + current summaries for one invocation
# --------------------------------------------------------------------------- #
def test_rerun_failed_prior_plus_current(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")], prov=_plan_prov(run_attempt="1"))
    ncur = PREFIX + "rag-a-pg17-aaaa-r123-a2"
    npri = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_named(tmp_path, ncur, _summary("rag-a-pg17-aaaa",
                                          provenance=_caller_prov(caller_run_attempt="2")))
    _write_named(tmp_path, npri, _summary("rag-a-pg17-aaaa", verdict="fail",
                                          counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0},
                                          provenance=_caller_prov(caller_run_attempt="1")))
    listing = [_artifact(11, ncur), _artifact(22, npri)]
    result, ledger, summaries, code = _run(plan, listing, tmp_path, attempt="2")
    assert code == 0
    assert result["result_resolved"] is True
    assert result["legs"][0]["reconciliation"] == "matched"
    assert result["legs"][0]["test_verdict"] == "pass"                 # current used
    assert [h["producing_attempt"] for h in result["historical_results"]] == ["1"]
    assert result["attempt_context"] == {"plan_run_attempt": "1", "aggregation_run_attempt": "2"}
    assert ledger["collection_status"] == "ok"
    assert ledger["counts"]["ingested"] == 2


# --------------------------------------------------------------------------- #
# 4. rerun-all: all current
# --------------------------------------------------------------------------- #
def test_rerun_all_current(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")],
                 prov=_plan_prov(run_attempt="3"))
    na = PREFIX + "rag-a-pg17-aaaa-r123-a3"
    nb = PREFIX + "rag-b-pg18-bbbb-r123-a3"
    _write_named(tmp_path, na, _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="3")))
    _write_named(tmp_path, nb, _summary("rag-b-pg18-bbbb", provenance=_caller_prov(caller_run_attempt="3")))
    result, ledger, summaries, code = _run(plan, [_artifact(11, na), _artifact(22, nb)], tmp_path, attempt="3")
    assert code == 0
    assert result["coverage_status"] == "complete"
    assert result["historical_results"] == []
    assert result["attempt_context"] == {"plan_run_attempt": "3", "aggregation_run_attempt": "3"}


# --------------------------------------------------------------------------- #
# 5. zero candidates -> missing_result (resolved, exit 0)
# --------------------------------------------------------------------------- #
def test_zero_candidates(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    listing = [_artifact(11, "some-other-artifact"), _artifact(22, "logs-bundle")]
    result, ledger, summaries, code = _run(plan, listing, tmp_path)
    assert code == 0
    assert result["result_resolved"] is True
    assert result["reason_code"] == "missing_result"
    assert summaries == []
    assert ledger["candidates"] == []
    assert ledger["collection_status"] == "ok"
    assert ledger["counts"]["candidates"] == 0


# --------------------------------------------------------------------------- #
# 6. expired candidate -> retained in ledger, becomes missing_result
# --------------------------------------------------------------------------- #
def test_expired_candidate(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    listing = [_artifact(11, name, expired=True)]      # no extracted dir on purpose
    result, ledger, summaries, code = _run(plan, listing, tmp_path)
    assert code == 0
    assert result["result_resolved"] is True
    assert result["reason_code"] == "missing_result"
    assert summaries == []                             # expired contributes nothing
    c = _byname(ledger, name)
    assert c["extraction"] == "expired"
    assert c["expired"] is True
    assert ledger["counts"]["expired"] == 1
    assert ledger["collection_status"] == "ok"         # expired is not an anomaly


# --------------------------------------------------------------------------- #
# 7. malformed JSON and non-object JSON
# --------------------------------------------------------------------------- #
def test_malformed_json_fails_closed(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_flat(tmp_path, "{not valid json")
    result, ledger, summaries, code = _run(plan, [_artifact(11, name)], tmp_path)
    assert code == 1
    assert result["result_resolved"] is False
    assert result["reason_code"] == "validation_failure"
    assert _byname(ledger, name)["extraction"] == "malformed_json"
    assert ledger["collection_status"] == "failed"
    assert summaries == [None]


def test_non_object_valid_json_is_passed_and_reducer_rejects(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_flat(tmp_path, "[1, 2, 3]")                 # valid JSON, not an object
    result, ledger, summaries, code = _run(plan, [_artifact(11, name)], tmp_path)
    assert code == 1
    assert result["result_resolved"] is False          # reducer rejects non-object
    assert summaries == [[1, 2, 3]]                     # passed UNCHANGED (not None)
    c = _byname(ledger, name)
    assert c["extraction"] == "one_summary"            # collector found exactly one
    assert ledger["collection_status"] == "ok"         # collection itself was fine


# --------------------------------------------------------------------------- #
# 8. missing / nested-only / multiple summaries
# --------------------------------------------------------------------------- #
def test_missing_summary_fails_closed(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    (tmp_path / name).mkdir()                           # dir exists, no summary.json
    result, ledger, summaries, code = _run(plan, [_artifact(11, name)], tmp_path)
    assert code == 1 and result["result_resolved"] is False
    assert _byname(ledger, name)["extraction"] == "missing_summary"
    assert summaries == [None]


def test_nested_only_summary_fails_closed(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    na = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    nb = PREFIX + "rag-b-pg18-bbbb-r123-a1"
    _write_named(tmp_path, na, _summary("rag-a-pg17-aaaa"))                 # ok
    _write_named(tmp_path, nb, _summary("rag-b-pg18-bbbb"), at_root=False, nested=True)  # nested only
    result, ledger, summaries, code = _run(plan, [_artifact(11, na), _artifact(22, nb)], tmp_path)
    assert code == 1 and result["result_resolved"] is False
    assert _byname(ledger, nb)["extraction"] == "nested_only"


def test_multiple_summaries_fails_closed(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    na = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    nb = PREFIX + "rag-b-pg18-bbbb-r123-a1"
    _write_named(tmp_path, na, _summary("rag-a-pg17-aaaa"))
    _write_named(tmp_path, nb, _summary("rag-b-pg18-bbbb"), at_root=True, nested=True)   # root + nested
    result, ledger, summaries, code = _run(plan, [_artifact(11, na), _artifact(22, nb)], tmp_path)
    assert code == 1 and result["result_resolved"] is False
    c = _byname(ledger, nb)
    assert c["extraction"] == "multiple_summaries"
    assert c["summary_count"] == 2


# --------------------------------------------------------------------------- #
# 9. unrelated non-prefix artifacts are ignored (never evidence)
# --------------------------------------------------------------------------- #
def test_unrelated_non_prefix_ignored(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_flat(tmp_path, _summary("rag-a-pg17-aaaa"))
    listing = [_artifact(11, name), _artifact(22, "pep-capture-evidence-r1-a1"),
               _artifact(33, "unrelated")]
    result, ledger, summaries, code = _run(plan, listing, tmp_path)
    assert code == 0
    assert [c["artifact_name"] for c in ledger["candidates"]] == [name]   # only the prefix one
    assert ledger["counts"]["candidates"] == 1


# --------------------------------------------------------------------------- #
# 10. valid prefix candidate with unknown / foreign summary content
# --------------------------------------------------------------------------- #
def test_unknown_invocation_summary_fails_closed(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-z-not-in-plan-r123-a1"
    _write_flat(tmp_path, _summary("rag-z-not-in-plan"))     # id not in plan
    result, ledger, summaries, code = _run(plan, [_artifact(11, name)], tmp_path)
    assert code == 1 and result["result_resolved"] is False
    assert any(u["kind"] == "unknown" for u in result["unexpected_results"])
    assert _byname(ledger, name)["extraction"] == "one_summary"   # collection fine; content unknown
    assert ledger["collection_status"] == "ok"


def test_foreign_provenance_summary_fails_closed(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_flat(tmp_path, _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_id="999")))
    result, ledger, summaries, code = _run(plan, [_artifact(11, name)], tmp_path)
    assert code == 1 and result["result_resolved"] is False
    assert any(u["kind"] == "foreign" for u in result["unexpected_results"])


# --------------------------------------------------------------------------- #
# 11. invalid / duplicate candidate IDs and names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_id", [0, -1, True, "5", 1.0, None])
def test_invalid_candidate_id_fails_closed(tmp_path, bad_id):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_flat(tmp_path, _summary("rag-a-pg17-aaaa"))
    result, ledger, summaries, code = _run(plan, [_artifact(bad_id, name)], tmp_path)
    assert code == 1 and result["result_resolved"] is False
    c = _byname(ledger, name)
    assert c["extraction"] == "invalid_metadata"
    assert c["artifact_id"] is None
    assert ledger["collection_status"] == "failed"


def test_invalid_expired_type_fails_closed(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_flat(tmp_path, _summary("rag-a-pg17-aaaa"))
    result, ledger, summaries, code = _run(plan, [_artifact(11, name, expired="yes")], tmp_path)
    assert code == 1
    assert _byname(ledger, name)["extraction"] == "invalid_metadata"


def test_duplicate_candidate_ids_fail_closed(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")])
    na = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    nb = PREFIX + "rag-b-pg18-bbbb-r123-a1"
    _write_named(tmp_path, na, _summary("rag-a-pg17-aaaa"))
    _write_named(tmp_path, nb, _summary("rag-b-pg18-bbbb"))
    listing = [_artifact(11, na), _artifact(11, nb)]     # duplicate id 11
    result, ledger, summaries, code = _run(plan, listing, tmp_path)
    assert code == 1 and result["result_resolved"] is False
    # BOTH duplicates flagged (order-independent frequency detection)
    assert all(_byname(ledger, n)["extraction"] == "invalid_metadata" for n in (na, nb))


def test_duplicate_candidate_names_fail_closed(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_named(tmp_path, name, _summary("rag-a-pg17-aaaa"))
    listing = [_artifact(11, name), _artifact(22, name)]  # duplicate name
    result, ledger, summaries, code = _run(plan, listing, tmp_path)
    assert code == 1 and result["result_resolved"] is False
    assert sum(1 for c in ledger["candidates"] if c["extraction"] == "invalid_metadata") == 2


# --------------------------------------------------------------------------- #
# 12. unsafe artifact-name path
# --------------------------------------------------------------------------- #
# Only names with an embedded path separator can escape the download root; a literal
# name that merely ends in ".." (no separator) is a normal, non-escaping filename.
@pytest.mark.parametrize("evil", [
    PREFIX + "../escape", PREFIX + "a/b", PREFIX + "a\\b",
])
def test_unsafe_artifact_name_fails_closed(tmp_path, evil):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    result, ledger, summaries, code = _run(plan, [_artifact(11, evil)], tmp_path)
    assert code == 1 and result["result_resolved"] is False
    assert _byname(ledger, evil)["extraction"] == "unsafe_name"
    assert ledger["collection_status"] == "failed"


# --------------------------------------------------------------------------- #
# 13. invalid current attempt
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["0", "-1", "", "abc", "1.0"])
def test_invalid_current_attempt_fails_closed(tmp_path, bad):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_flat(tmp_path, _summary("rag-a-pg17-aaaa"))
    result, ledger, summaries, code = _run(plan, [_artifact(11, name)], tmp_path, attempt=bad)
    assert code == 1
    assert result["result_resolved"] is False
    assert any("current_run_attempt" in e for e in result["errors"])
    assert ledger["current_run_attempt"] == bad         # echoed verbatim


# --------------------------------------------------------------------------- #
# 14. reordered listing -> byte-identical ledger AND cert-result
# --------------------------------------------------------------------------- #
def test_reordered_listing_is_byte_identical(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa"), _inv("rag-b-pg18-bbbb", pg="18")],
                 prov=_plan_prov(run_attempt="1"))
    na = PREFIX + "rag-a-pg17-aaaa-r123-a3"
    nb = PREFIX + "rag-b-pg18-bbbb-r123-a3"
    npri = PREFIX + "rag-a-pg17-aaaa-r123-a2"
    nexp = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_named(tmp_path, na, _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_run_attempt="3")))
    _write_named(tmp_path, nb, _summary("rag-b-pg18-bbbb", provenance=_caller_prov(caller_run_attempt="3")))
    _write_named(tmp_path, npri, _summary("rag-a-pg17-aaaa", verdict="fail",
                                          counts={"tests": 3, "failures": 1, "errors": 0, "skipped": 0},
                                          provenance=_caller_prov(caller_run_attempt="2")))
    listing = [_artifact(11, na), _artifact(22, nb), _artifact(33, npri),
               _artifact(44, nexp, expired=True)]
    r1, l1, _s1, c1 = _run(plan, list(listing), tmp_path, attempt="3")
    r2, l2, _s2, c2 = _run(plan, list(reversed(listing)), tmp_path, attempt="3")
    assert c1 == 0 and c2 == 0
    assert rio.CR.to_json(r1) == rio.CR.to_json(r2)             # cert-result byte-identical
    assert rio._ledger_json(l1) == rio._ledger_json(l2)        # ledger byte-identical
    assert r1["coverage_status"] == "complete"
    assert [h["producing_attempt"] for h in r1["historical_results"]] == ["2"]


# --------------------------------------------------------------------------- #
# 15. ledger sanitation: no token, URL, raw payload or summary body
# --------------------------------------------------------------------------- #
def test_ledger_has_no_secrets_urls_or_summary_bodies(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_flat(tmp_path, _summary("rag-a-pg17-aaaa", provenance=_caller_prov(caller_sha="c" * 40)))
    # A realistic raw REST artifact object carries URLs, a token-ish field and nested objects.
    raw = _artifact(11, name,
                    url="https://api.github.com/repos/o/r/actions/artifacts/11",
                    archive_download_url="https://api.github.com/repos/o/r/actions/artifacts/11/zip",
                    node_id="MDg6QXJ0aWZhY3Qx", token="ghs_SECRETVALUE1234567890",
                    workflow_run={"id": 999, "head_sha": "c" * 40})
    _result, ledger, _s, _c = _run(plan, [raw], tmp_path)
    blob = rio._ledger_json(ledger)
    for needle in ("http://", "https://", "archive_download_url", "ghs_", "token",
                   "node_id", "workflow_run", "head_sha", "caller_sha", "provenance",
                   "identity_evidence", "c" * 40):
        assert needle not in blob, needle
    # allowlisted fields ARE present
    c = _byname(ledger, name)
    assert set(c.keys()) == {"artifact_id", "artifact_name", "size_in_bytes",
                             "expired", "extraction", "summary_count", "source_path"}


# --------------------------------------------------------------------------- #
# structural: non-list listing / non-dict entries fail closed, never silent drop
# --------------------------------------------------------------------------- #
def test_non_list_listing_fails_closed(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    result, ledger, summaries, code = _run(plan, {"not": "a list"}, tmp_path)
    assert code == 1 and result["result_resolved"] is False
    assert ledger["listing_ok"] is False
    assert ledger["collection_status"] == "failed"
    assert summaries == [None]


def test_non_dict_and_nameless_entries_fail_closed(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    _write_flat(tmp_path, _summary("rag-a-pg17-aaaa"))
    listing = ["not-a-dict", {"id": 5, "expired": False}, _artifact(11, name)]  # 2 invalid entries
    result, ledger, summaries, code = _run(plan, listing, tmp_path)
    assert code == 1 and result["result_resolved"] is False        # invalid entries force fail-closed
    assert ledger["counts"]["invalid_entries"] == 2
    assert ledger["collection_status"] == "failed"


# --------------------------------------------------------------------------- #
# main() end-to-end: atomic files + exit code
# --------------------------------------------------------------------------- #
def test_main_writes_both_outputs_and_returns_zero(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    dl = tmp_path / "dl"
    dl.mkdir()
    _write_flat(dl, _summary("rag-a-pg17-aaaa"))
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "listing.json").write_text(json.dumps([_artifact(11, name)]))
    out = tmp_path / "cert-result.json"
    ledger = tmp_path / "ledger.json"
    code = rio.main(["--plan", str(tmp_path / "plan.json"),
                     "--artifacts-listing", str(tmp_path / "listing.json"),
                     "--download-dir", str(dl), "--current-run-attempt", "1",
                     "--out", str(out), "--ledger-out", str(ledger)])
    assert code == 0
    cr = json.loads(out.read_text())
    lg = json.loads(ledger.read_text())
    assert cr["result_resolved"] is True and cr["execution_status"] == "completed"
    assert lg["schema"] == "pep-collection-ledger/1"
    assert lg["collection_status"] == "ok"


def test_main_returns_one_on_unresolved(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    dl = tmp_path / "dl"
    dl.mkdir()
    _write_flat(dl, "{bad json")
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "listing.json").write_text(json.dumps([_artifact(11, name)]))
    out = tmp_path / "cert-result.json"
    ledger = tmp_path / "ledger.json"
    code = rio.main(["--plan", str(tmp_path / "plan.json"),
                     "--artifacts-listing", str(tmp_path / "listing.json"),
                     "--download-dir", str(dl), "--current-run-attempt", "1",
                     "--out", str(out), "--ledger-out", str(ledger)])
    assert code == 1
    assert json.loads(out.read_text())["result_resolved"] is False      # cert-result STILL written
    assert json.loads(ledger.read_text())["collection_status"] == "failed"


def test_main_requires_all_required_args(tmp_path):
    with pytest.raises(SystemExit):
        rio.main(["--plan", "p", "--artifacts-listing", "l", "--download-dir", "d",
                  "--out", "o", "--ledger-out", "g"])       # missing --current-run-attempt


# --------------------------------------------------------------------------- #
# correction 1: total, symlink-safe path handling
# --------------------------------------------------------------------------- #
def test_nul_in_artifact_name_fails_closed_without_raising(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a\x00evil"                    # embedded NUL
    # Must not raise (e.g. from realpath/lstat); produces outputs + fails closed.
    result, ledger, summaries, code = _run(plan, [_artifact(11, name)], tmp_path)
    assert code == 1 and result["result_resolved"] is False
    c = _byname(ledger, name)
    assert c["extraction"] == "unsafe_name"
    assert ledger["collection_status"] == "failed"
    assert summaries == [None]
    # the ledger still serializes cleanly (NUL escaped as \x00, no raw exception)
    assert "\\u0000" in rio._ledger_json(ledger)


def test_safe_child_dir_is_total_over_nul():
    # Direct: never raises, deterministically None.
    assert rio._safe_child_dir("/tmp/dl", "good-name") == os.path.join("/tmp/dl", "good-name")
    assert rio._safe_child_dir("/tmp/dl", "bad\x00name") is None


def test_named_directory_symlink_is_unsafe_path(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    real = tmp_path / "real_pkg"
    real.mkdir()
    _write(real / "summary.json", _summary("rag-a-pg17-aaaa"))
    (tmp_path / name).symlink_to(real, target_is_directory=True)   # named root is a symlink
    result, ledger, summaries, code = _run(plan, [_artifact(11, name)], tmp_path)
    assert code == 1 and result["result_resolved"] is False
    assert _byname(ledger, name)["extraction"] == "unsafe_path"
    assert summaries == [None]


def test_root_summary_symlink_to_outside_is_unsafe_path(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    outside = tmp_path / "outside.json"
    _write(outside, _summary("rag-a-pg17-aaaa"))       # a real JSON file OUTSIDE the download dir
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "summary.json").symlink_to(outside)          # flat root summary is a symlink
    result, ledger, summaries, code = _run(plan, [_artifact(11, name)], dl)
    assert code == 1 and result["result_resolved"] is False
    assert _byname(ledger, name)["extraction"] == "unsafe_path"
    assert summaries == [None]


def test_nested_summary_symlink_is_unsafe_path(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    outside = tmp_path / "outside.json"
    _write(outside, _summary("rag-a-pg17-aaaa"))
    d = tmp_path / name
    (d / "logs").mkdir(parents=True)
    _write(d / "summary.json", _summary("rag-a-pg17-aaaa"))     # valid root summary...
    (d / "logs" / "summary.json").symlink_to(outside)          # ...plus a nested symlink
    result, ledger, summaries, code = _run(plan, [_artifact(11, name)], tmp_path)
    assert code == 1 and result["result_resolved"] is False
    assert _byname(ledger, name)["extraction"] == "unsafe_path"


# --------------------------------------------------------------------------- #
# correction 2: prefix validation (empty / whitespace-only)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_prefix", ["", "   ", "\t"])
def test_blank_prefix_fails_closed_direct(tmp_path, bad_prefix):
    name = "pep-summary-rag-a-pg17-aaaa-r123-a1"
    _write_flat(tmp_path, _summary("rag-a-pg17-aaaa"))
    summaries, cands, meta = rio.collect([_artifact(11, name)], str(tmp_path), bad_prefix)
    assert summaries == [None]                          # forces fail closed
    assert cands == []
    assert meta["anomaly"] is True and meta["prefix_ok"] is False


def test_blank_prefix_fails_closed_cli(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = "pep-summary-rag-a-pg17-aaaa-r123-a1"
    dl = tmp_path / "dl"
    dl.mkdir()
    _write_flat(dl, _summary("rag-a-pg17-aaaa"))
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "listing.json").write_text(json.dumps([_artifact(11, name)]))
    out = tmp_path / "cr.json"
    ledger = tmp_path / "lg.json"
    code = rio.main(["--plan", str(tmp_path / "plan.json"),
                     "--artifacts-listing", str(tmp_path / "listing.json"),
                     "--download-dir", str(dl), "--current-run-attempt", "1",
                     "--out", str(out), "--ledger-out", str(ledger), "--name-prefix", "   "])
    assert code == 1
    lg = json.loads(ledger.read_text())
    assert lg["prefix_ok"] is False and lg["collection_status"] == "failed"
    assert json.loads(out.read_text())["result_resolved"] is False


# --------------------------------------------------------------------------- #
# correction 3: output-destination collision is a systemic CLI error
# --------------------------------------------------------------------------- #
def test_out_equals_ledger_out_is_rejected_without_writing(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    dl = tmp_path / "dl"
    dl.mkdir()
    _write_flat(dl, _summary("rag-a-pg17-aaaa"))
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "listing.json").write_text(json.dumps([_artifact(11, name)]))
    same = tmp_path / "both.json"
    # The two args spell the SAME destination two different ways.
    code = rio.main(["--plan", str(tmp_path / "plan.json"),
                     "--artifacts-listing", str(tmp_path / "listing.json"),
                     "--download-dir", str(dl), "--current-run-attempt", "1",
                     "--out", str(same),
                     "--ledger-out", str(tmp_path / "." / "both.json")])
    assert code == 2                                   # systemic config error
    assert not same.exists()                           # neither document written


# --------------------------------------------------------------------------- #
# correction 5: unreadable / malformed plan or listing still write both + nonzero
# --------------------------------------------------------------------------- #
def _cli(tmp_path, plan_arg, listing_arg, subdir):
    dl = tmp_path / subdir
    dl.mkdir()
    _write_flat(dl, _summary("rag-a-pg17-aaaa"))
    out = tmp_path / (subdir + "-cr.json")
    ledger = tmp_path / (subdir + "-lg.json")
    code = rio.main(["--plan", plan_arg, "--artifacts-listing", listing_arg,
                     "--download-dir", str(dl), "--current-run-attempt", "1",
                     "--out", str(out), "--ledger-out", str(ledger)])
    return code, out, ledger


def test_unreadable_plan_writes_both_and_returns_nonzero(tmp_path):
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    (tmp_path / "listing.json").write_text(json.dumps([_artifact(11, name)]))
    code, out, ledger = _cli(tmp_path, str(tmp_path / "nope.json"),
                             str(tmp_path / "listing.json"), "u1")
    assert code == 1
    assert json.loads(out.read_text())["result_resolved"] is False    # cert-result written
    assert json.loads(ledger.read_text())["schema"] == "pep-collection-ledger/1"


def test_malformed_plan_writes_both_and_returns_nonzero(tmp_path):
    name = PREFIX + "rag-a-pg17-aaaa-r123-a1"
    (tmp_path / "plan.json").write_text("{bad json")
    (tmp_path / "listing.json").write_text(json.dumps([_artifact(11, name)]))
    code, out, ledger = _cli(tmp_path, str(tmp_path / "plan.json"),
                             str(tmp_path / "listing.json"), "u2")
    assert code == 1
    assert json.loads(out.read_text())["result_resolved"] is False
    assert ledger.exists()


def test_unreadable_listing_writes_both_and_returns_nonzero(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    code, out, ledger = _cli(tmp_path, str(tmp_path / "plan.json"),
                             str(tmp_path / "missing-listing.json"), "u3")
    assert code == 1
    assert json.loads(out.read_text())["result_resolved"] is False
    assert json.loads(ledger.read_text())["collection_status"] == "failed"   # listing not a list


def test_malformed_listing_writes_both_and_returns_nonzero(tmp_path):
    plan = _plan([_inv("rag-a-pg17-aaaa")])
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "listing.json").write_text("{bad json")
    code, out, ledger = _cli(tmp_path, str(tmp_path / "plan.json"),
                             str(tmp_path / "listing.json"), "u4")
    assert code == 1
    assert json.loads(out.read_text())["result_resolved"] is False
    assert json.loads(ledger.read_text())["listing_ok"] is False
