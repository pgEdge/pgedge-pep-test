"""Structural contract test for .github/workflows/pep-certification-preview.yml.

Stdlib-only (no PyYAML, matching test_pep_certify_workflow.py). It proves the WIRING: the
workflow runs on every pull request in its own Actions run, builds the checker's fixture cells
with receipts, calls the real coordinator at the revision under test as a truthful simulated
preview, and asserts the result from immutable-id downloads even when the coordinator fails.
The assertion logic itself is tested in test_pep_preview_check.py.
"""
import json
import re
from pathlib import Path

import pep_preview_check as PC

_REPO = Path(__file__).resolve().parents[2]
_WF = _REPO / ".github" / "workflows"
_TEXT = (_WF / "pep-certification-preview.yml").read_text()
_SELFTEST = (_WF / "pep-selftest.yml").read_text()
_CERTIFY = (_WF / "pep-certify.yml").read_text()

_SELFTEST_JOBS = {"unit", "preview-call", "assert-preview", "validation-call", "assert-validation",
                  "bad-execmode-call", "assert-bad-execmode", "bad-mode-call", "assert-bad-mode"}


def _code(text):
    """``text`` without full-line comments, so prose never satisfies a check."""
    return "".join(l for l in text.splitlines(keepends=True) if not l.lstrip().startswith("#"))


def _job_ids(text):
    jobs = text[text.index("\njobs:\n"):]
    return re.findall(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$", jobs, re.M)


def _job_block(job, text=_TEXT):
    """The text of one top-level job (from ``  <job>:`` to the next ``  <name>:``)."""
    out, capturing = [], False
    for ln in text.splitlines(keepends=True):
        if re.match(r"^  %s:\s*$" % re.escape(job), ln):
            capturing = True
            out.append(ln)
            continue
        if capturing:
            if re.match(r"^  [A-Za-z0-9_-]+:\s*$", ln):
                break
            out.append(ln)
    return _code("".join(out))


def _on_block():
    m = re.search(r"^on:\n((?:[ #].*\n|\n)*?)(?=^\S)", _TEXT, re.M)
    return _code(m.group(1))


def _with_json(key):
    m = re.search(r"^      %s: '(.*)'\s*$" % key, _job_block("certify"), re.M)
    assert m, key
    return json.loads(m.group(1))


def _steps(job):
    """(name-or-uses, text) for each step of ``job``, in order."""
    blocks = re.split(r"^      - ", _job_block(job), flags=re.M)[1:]
    return [(b.splitlines()[0], b) for b in blocks]


# --------------------------------------------------------------------------- triggers / isolation
def test_runs_on_every_pull_request_and_by_hand():
    on = _on_block()
    assert re.search(r"^  pull_request: \{\}$", on, re.M)
    assert re.search(r"^  workflow_dispatch: \{\}$", on, re.M)
    for word in ("paths", "branches", "tags", "push"):
        assert word not in on, word


def test_is_its_own_workflow_separate_from_the_selftest():
    assert re.search(r"^name: PEP Certification Preview$", _TEXT, re.M)
    assert "pep-certification-preview.yml" not in re.sub(r"^.*actionlint.*$", "", _SELFTEST, flags=re.M)
    assert "pep-selftest.yml" not in _code(_TEXT)
    assert not set(_job_ids(_TEXT)) & _SELFTEST_JOBS


def test_selftest_keeps_its_nine_jobs():
    assert set(_job_ids(_SELFTEST)) == _SELFTEST_JOBS


def test_selftest_lints_the_preview_workflow_with_the_same_narrow_suppression():
    lint = next(l for l in _SELFTEST.splitlines() if "rhysd/actionlint@sha256:" in l and l.strip().startswith("run:"))
    assert ".github/workflows/pep-certification-preview.yml" in lint
    assert _SELFTEST.count("-ignore '") == 1


# --------------------------------------------------------------------------- privileges
def test_least_privilege_and_no_secrets():
    assert re.search(r"^permissions: \{\}$", _TEXT, re.M)
    build = _job_block("build")
    assert "contents: read" in build and "actions:" not in build
    for job in ("certify", "assert"):
        block = _job_block(job)
        assert "contents: read" in block and "actions: read" in block, job
    for word in ("write", "secrets"):
        assert word not in _code(_TEXT), word


def test_every_external_action_is_pinned_to_a_full_sha():
    uses = re.findall(r"uses:\s*(\S+)", _code(_TEXT))
    assert uses
    for u in uses:
        assert u.startswith("./") or re.search(r"@[0-9a-f]{40}$", u), u


# --------------------------------------------------------------------------- fixture build
def test_build_cells_are_the_checkers_fixture_cells_with_markers():
    build = _job_block("build")
    assert "name: build [pep-cell:${{ matrix.cell_id }}]" in build
    cells = dict(re.findall(r"- cell_id: (\S+)\n\s+family: (\S+)", build))
    assert sorted(cells) == sorted(PC.FIXTURE_CELLS)
    assert sorted(cells.values()) == ["deb", "rpm"]
    smoke = (_WF / "pep-capture-smoke.yml").read_text()
    assert not set(cells) & set(re.findall(r"- cell_id: (\S+)", smoke))     # distinct check names


def test_build_uploads_the_package_then_records_its_receipt_with_the_local_action():
    steps = _steps("build")
    up = next(i for i, (_, b) in enumerate(steps) if "id: up" in b)
    rc = next(i for i, (_, b) in enumerate(steps) if "uses: ./.github/actions/pep-package-receipt" in b)
    assert up < rc
    upload, receipt = steps[up][1], steps[rc][1]
    assert "actions/upload-artifact@" in upload
    name = re.search(r"name: (certpreview-pkg-\S+)", upload).group(1)
    assert "package_artifact_name: %s" % name in receipt
    assert "package_artifact_id: ${{ steps.up.outputs.artifact-id }}" in receipt
    assert "package_artifact_digest: ${{ steps.up.outputs.artifact-digest }}" in receipt
    assert "cell_id: ${{ matrix.cell_id }}" in receipt


def test_build_makes_the_fixture_package_named_by_the_checker():
    build = _job_block("build")
    assert build.count("Package: %s" % PC.PACKAGE) == 1 and build.count("Name: %s" % PC.PACKAGE) == 1


# --------------------------------------------------------------------------- the coordinator call
def test_certify_calls_the_real_coordinator_at_the_revision_under_test():
    certify = _job_block("certify")
    assert re.search(r"^    uses: \./\.github/workflows/pep-certify\.yml$", certify, re.M)   # relative: no @ref
    assert re.search(r"^    needs: build$", certify, re.M)
    assert "if: ${{ !cancelled() }}" in certify


def test_certify_is_a_truthful_simulated_preview_in_observe_mode():
    certify = _job_block("certify")
    assert re.search(r"^      execution_mode: preview$", certify, re.M)
    assert re.search(r"^      enforcement: observe$", certify, re.M)
    intent = _with_json("release_intent")
    assert intent["simulated"] is True and intent["logical_component"] == PC.COMPONENT
    assert _with_json("publication_results") == {"rpm": "skipped", "deb": "skipped"}
    cells = _with_json("rpm_matrix")["include"] + _with_json("deb_matrix")["include"]
    assert sorted(c["cell_id"] for c in cells) == sorted(PC.FIXTURE_CELLS)
    assert all(c["pg_coupled"] is False and c["normalized_arch"] == "amd64" for c in cells)
    assert {c["family"] for c in _with_json("rpm_matrix")["include"]} == {"rpm"}
    assert {c["family"] for c in _with_json("deb_matrix")["include"]} == {"deb"}


def test_the_fixture_component_is_in_peps_component_policy():
    policy = json.loads((_REPO / "aspects" / "pipeline" / "pep_capture_policy.json").read_text())
    assert PC.PACKAGE in policy["components"][PC.COMPONENT]["allowed_runtime_package_names"]


# --------------------------------------------------------------------------- the assertion job
def test_assert_runs_even_when_the_coordinator_fails():
    job = _job_block("assert")
    assert re.search(r"^    needs: certify$", job, re.M)
    assert "if: ${{ always() }}" in job


def test_assert_expects_this_run_and_the_revision_under_test():
    job = _job_block("assert")
    for key, expr in (("EXPECT_REPOSITORY", "github.repository"), ("EXPECT_RUN_ID", "github.run_id"),
                      ("EXPECT_RUN_NUMBER", "github.run_number"), ("EXPECT_RUN_ATTEMPT", "github.run_attempt"),
                      ("EXPECT_SHA", "github.sha"), ("EXPECT_REF", "github.ref"),
                      ("EXPECT_PEP_SHA", "github.workflow_sha")):
        assert re.search(r"^      %s: \$\{\{ %s \}\}$" % (key, re.escape(expr)), job, re.M), key
    assert set(PC.EXPECT_KEYS) == {k for k in re.findall(r"^      (EXPECT_[A-Z_]+):", job, re.M)}


def test_assert_reads_only_outputs_the_coordinator_declares():
    used = set(re.findall(r"needs\.certify\.outputs\.([a-z_]+)", _job_block("assert")))
    declared = set(re.findall(r"^      ([a-z_]+):\s+\{description:", _CERTIFY, re.M))
    assert used and used <= declared
    assert "needs.certify.result" in _job_block("assert")


def test_assert_checks_the_coordinator_before_any_download_and_downloads_only_by_id():
    steps = _steps("assert")
    first = next(i for i, (_, b) in enumerate(steps) if "pep_preview_check.py coordinator" in b)
    downloads = [(i, b) for i, (_, b) in enumerate(steps) if "actions/download-artifact@" in b]
    assert len(downloads) == 3 and all(i > first for i, _ in downloads)
    ids = sorted(re.search(r"artifact-ids: \$\{\{ (\S+) \}\}", b).group(1) for _, b in downloads)
    assert ids == ["needs.certify.outputs.evidence_artifact_id", "steps.select.outputs.capture_evidence_id",
                   "steps.select.outputs.cert_plan_id"]
    for _, b in downloads:
        assert "pattern:" not in b and "name:" not in b.split("with:", 1)[1]


def test_assert_verifies_the_downloaded_documents_and_the_listing():
    steps = _steps("assert")
    select = next(b for _, b in steps if "id: select" in b)
    assert "pipeline/pep_artifact_listing.py" in select and "--listing listing.json" in select
    assert "pep_preview_check.py select --listing listing.json --github-output \"$GITHUB_OUTPUT\"" in select
    verify = next(b for _, b in steps if "pep_preview_check.py verify" in b)
    paths = set(re.findall(r"^          path: (\S+)$", _job_block("assert"), re.M))
    assert "--evidence-dir dl-evidence" in verify and "dl-evidence" in paths
    assert "--capture-evidence dl-capture/capture-evidence.json" in verify and "dl-capture" in paths
    assert "--cert-plan dl-cert-plan/cert-plan.json" in verify and "dl-cert-plan" in paths
    assert "--listing listing.json" in verify
    assert steps.index(next(s for s in steps if "pep_preview_check.py verify" in s[1])) == len(steps) - 1
