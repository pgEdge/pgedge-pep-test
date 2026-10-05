"""Structural contract test for the reusable capture workflow .github/workflows/pep-capture.yml.

Stdlib-only (no PyYAML, matching test_pep_certify_workflow.py). It pins the FAILURE-PATH
diagnostic contract: when capture fails systemically, pep_capture_io._fail writes a sanitized
capture-out/capture-error.json ({capture_status, error}, redacted) and exits nonzero. The
workflow must preserve exactly that one file as a failure-only artifact under a deterministic
name that does not depend on (skipped) earlier steps, keep the job failed, advertise nothing
new, and leave the success-path evidence/plan uploads unchanged.

YAML comments are stripped before any matching so a comment can never satisfy an assertion.
"""
import re
from pathlib import Path

import pep_capture as C
import pep_result_io as RIO

_REPO = Path(__file__).resolve().parents[2]
_CAPTURE = _REPO / ".github" / "workflows" / "pep-capture.yml"

_ERROR_FILE = "capture-out/capture-error.json"
_PIN_RE = r"actions/upload-artifact@[0-9a-f]{40}"


def _strip_comments(text):
    """Drop full-line YAML comments and trailing whitespace-preceded `` # ...`` comments."""
    out = []
    for ln in text.splitlines():
        if re.match(r"^\s*#", ln):
            continue
        out.append(re.sub(r"\s+#.*$", "", ln))
    return "\n".join(out) + "\n"


_TEXT = _strip_comments(_CAPTURE.read_text())


def _steps():
    """Ordered step texts of the capture job (each from ``      - `` to the next step)."""
    steps, cur, in_steps = [], None, False
    for ln in _TEXT.splitlines(keepends=True):
        if re.match(r"^    steps:\s*$", ln):
            in_steps = True
            continue
        if not in_steps:
            continue
        if re.match(r"^      - ", ln):
            if cur is not None:
                steps.append("".join(cur))
            cur = [ln]
        elif cur is not None:
            if ln.strip() and re.match(r"^ {0,5}\S", ln):          # dedented out of steps:
                break
            cur.append(ln)
    if cur is not None:
        steps.append("".join(cur))
    return steps


def _step_id(step):
    m = re.search(r"^        id:\s*(\S+)\s*$", step, re.M)
    return m.group(1) if m else None


def _step_by_id(step_id):
    found = [s for s in _steps() if _step_id(s) == step_id]
    assert len(found) == 1, (step_id, len(found))
    return found[0]


def _uses(step):
    m = re.search(r"^        uses:\s*(\S+)\s*$", step, re.M)
    return m.group(1) if m else None


def _with(step):
    """The ``with:`` mapping of a step as {key: raw scalar value}."""
    out, capturing = {}, False
    for ln in step.splitlines():
        if re.match(r"^        with:\s*$", ln):
            capturing = True
            continue
        if capturing:
            if not ln.strip():
                continue
            m = re.match(r"^          ([A-Za-z0-9_-]+):\s*(.*?)\s*$", ln)
            if not m:
                break
            out[m.group(1)] = m.group(2)
    return out


def _error_step():
    found = [s for s in _steps() if "capture-error" in s]
    assert len(found) == 1, "expected exactly one step referencing capture-error, got %d" % len(found)
    return found[0]


def _outputs_blocks():
    """Every 4-space ``outputs:`` block (workflow_call outputs and the capture job outputs)."""
    blocks, cur = [], None
    for ln in _TEXT.splitlines(keepends=True):
        if re.match(r"^    outputs:\s*$", ln):
            cur = []
            continue
        if cur is not None:
            if ln.strip() and re.match(r"^ {0,4}\S", ln):
                blocks.append("".join(cur))
                cur = None
                continue
            cur.append(ln)
    if cur is not None:
        blocks.append("".join(cur))
    return blocks


def test_comments_are_stripped_before_matching():
    assert "# v7.0.1" not in _TEXT and not re.search(r"^\s*#", _TEXT, re.M)


# --------------------------------------------------------------------------- #
# failure-only upload of the sanitized capture-error.json
# --------------------------------------------------------------------------- #
def test_error_upload_step_runs_only_on_failure():
    step = _error_step()
    assert re.search(r"^        if:\s*(\$\{\{\s*)?failure\(\)(\s*\}\})?\s*$", step, re.M), step
    # never always()/!cancelled(): those would also run on success
    assert "always()" not in step and "cancelled()" not in step


def test_error_upload_uses_the_same_pinned_upload_artifact():
    step = _error_step()
    uses = _uses(step)
    assert uses and re.fullmatch(_PIN_RE, uses), uses              # 40-hex pin, never @vN
    assert uses == _uses(_step_by_id("up_evidence")) == _uses(_step_by_id("up_plan"))


def test_error_upload_path_is_exactly_the_sanitized_file():
    step = _error_step()
    w = _with(step)
    assert w.get("path") == _ERROR_FILE, w
    # only name/path/if-no-files-found: no extra paths, hidden files, retention or other knobs
    assert set(w) == {"name", "path", "if-no-files-found"}, w
    assert len(re.findall(r"^\s+path:", step, re.M)) == 1
    for raw in ("capture-in", "RUNNER_TEMP", "pep-capture-dl", "*"):
        assert raw not in w["path"], raw


def test_error_upload_ignores_a_missing_file():
    # a failure before capture ran (e.g. checkout) has no capture-error.json
    assert _with(_error_step()).get("if-no-files-found") == "ignore"


def test_error_artifact_name_is_deterministic_from_github_context_only():
    step = _error_step()
    name = _with(step)["name"]
    assert re.fullmatch(
        r"pep-capture-error-r\$\{\{\s*github\.run_number\s*\}\}-a\$\{\{\s*github\.run_attempt\s*\}\}",
        name), name
    # the names step is skipped on failure -> its outputs must never feed this step
    assert "steps.names" not in step and "steps." not in step
    for expr in re.findall(r"\$\{\{(.*?)\}\}", step):
        assert expr.strip() in ("github.run_number", "github.run_attempt", "failure()"), expr


def test_error_artifact_name_cannot_collide_with_consumed_prefixes():
    lit = _with(_error_step())["name"].split("${{", 1)[0]          # literal prefix
    names = _step_by_id("names")
    produced = re.findall(r"(?:evidence|plan)_name=([a-z-]+-)r\$\{GITHUB_RUN_NUMBER\}", names)
    assert sorted(produced) == ["pep-capture-evidence-", "pep-cert-plan-"], produced
    consumed = [RIO.DEFAULT_NAME_PREFIX, C.RECEIPT_ARTIFACT_PREFIX] + produced
    assert RIO.DEFAULT_NAME_PREFIX == "pep-summary-" and C.RECEIPT_ARTIFACT_PREFIX == "pep-receipt-"
    for p in consumed:
        assert not lit.startswith(p) and not p.startswith(lit), (lit, p)


def test_error_upload_is_last_and_after_capture():
    ids = [_step_id(s) for s in _steps()]
    steps = _steps()
    assert "capture-error" in steps[-1]                             # appended last
    # the success-path order is unchanged: capture -> names -> up_evidence -> up_plan -> error upload
    order = [i for i in ids if i in ("capture", "names", "up_evidence", "up_plan")]
    assert order == ["capture", "names", "up_evidence", "up_plan"], ids
    assert ids.index("up_plan") == len(ids) - 2


def test_job_stays_failed():
    # nothing may swallow the capture failure (the failure-only upload must not turn the job green)
    assert "continue-on-error" not in _TEXT
    assert not re.search(r"^    if:", _TEXT, re.M)                 # no job-level condition


# --------------------------------------------------------------------------- #
# success path unchanged; nothing new advertised
# --------------------------------------------------------------------------- #
def test_evidence_and_plan_uploads_unchanged():
    for sid, out_name, path in (("up_evidence", "evidence_name", "capture-out/capture-evidence.json"),
                                ("up_plan", "plan_name", "capture-out/cert-plan.json")):
        step = _step_by_id(sid)
        assert not re.search(r"^\s+if:", step, re.M), sid          # default success() only
        assert "capture-error" not in step, sid
        assert re.fullmatch(_PIN_RE, _uses(step) or ""), sid
        assert _with(step) == {"name": "${{ steps.names.outputs.%s }}" % out_name,
                               "path": path, "if-no-files-found": "error"}, sid


def test_names_step_unconditional_and_unchanged():
    step = _step_by_id("names")
    assert not re.search(r"^\s+if:", step, re.M)
    assert 'echo "evidence_name=pep-capture-evidence-r${GITHUB_RUN_NUMBER}-a${GITHUB_RUN_ATTEMPT}"' in step
    assert 'echo "plan_name=pep-cert-plan-r${GITHUB_RUN_NUMBER}-a${GITHUB_RUN_ATTEMPT}"' in step


def test_outputs_do_not_advertise_the_error_artifact():
    blocks = _outputs_blocks()
    assert len(blocks) == 2, len(blocks)                            # workflow_call + capture job
    for b in blocks:
        assert b.strip()
        assert "error" not in b.lower(), b
    sid = _step_id(_error_step())
    if sid:                                                         # an id, if any, is wired nowhere
        assert "steps.%s." % sid not in _TEXT


def test_only_single_capture_out_json_files_are_uploaded():
    # never the directory, the inputs, download/tmp roots or raw API data
    for step in _steps():
        if (_uses(step) or "").startswith("actions/upload-artifact@"):
            path = _with(step).get("path", "")
            assert re.fullmatch(r"capture-out/[a-z-]+\.json", path), path
