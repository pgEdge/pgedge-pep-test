"""Follow-up item 1 / 1b — prove the integration workflow produces a SAFE,
run-local-unique upload-artifact name, and that it does so through the
preflight-produced value rather than by re-interpolating raw inputs.

Two failure modes are guarded:
  1. COLLISION — `actions/upload-artifact` (v4+) forbids two artifacts sharing a
     name in one run. `github.run_number`/`run_attempt` are constant within a run
     and so cannot disambiguate concurrent caller jobs; the request dimensions +
     a caller-provided invocation_id are the only discriminators.
  2. UNSAFE NAME under always() — an invalid invocation_id is correctly classified
     as a validation rejection, but if Emit/Upload still interpolated the RAW input
     the report-only job would then go red when upload-artifact chokes on the name.

Rather than reimplement the naming formula in Python (which could silently drift
from the workflow), this check EXECUTES the workflow's ACTUAL preflight `run:`
block — the single source of the artifact name — and also asserts, structurally,
that Emit's summary_artifact and the Upload step's name: both consume
steps.preflight.outputs.artifact_name and never splice inputs.invocation_id.

Stdlib-only (no PyYAML): the CI unit job's setup-python interpreter is not
guaranteed to have PyYAML, and these workflow files are authored here in a
simple, flat block style, so a small indentation-aware reader suffices.
"""
import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SELFTEST = _REPO / ".github" / "workflows" / "pep-selftest.yml"
_INTEGRATION = _REPO / ".github" / "workflows" / "pep-integration.yml"
_INTEGRATION_USES = "./.github/workflows/pep-integration.yml"

# Defaults MUST mirror pep-integration.yml `on.workflow_call.inputs.*.default`.
# These reproduce GitHub's input-default behavior when a caller omits an input;
# they are NOT a copy of the naming formula (which lives only in the workflow).
_DEFAULTS = {"scenario": "certification", "mode": "observe", "execution_mode": "preview",
             "invocation_id": ""}
# env var each request field is routed through in the preflight step.
_ENV_MAP = {"component": "IN_COMPONENT", "family": "IN_FAMILY", "arch": "IN_ARCH",
            "pg_major": "IN_PG", "channel": "IN_CHANNEL", "container_alias": "IN_ALIAS",
            "scenario": "IN_SCENARIO", "execution_mode": "IN_EXECMODE",
            "invocation_id": "IN_INVOCATION_ID"}
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_bash = shutil.which("bash")
_needs_bash = unittest.skipIf(_bash is None, "bash required to execute the preflight block")


def _clean(value):
    """Strip an inline ' # comment', surrounding quotes, and whitespace."""
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _parse_caller_jobs(text):
    """Return a list of {input_key: value} dicts, one per job that `uses:` the
    integration workflow. Indentation contract (we author the file): job header
    at 2 spaces (optionally with a trailing inline comment), `uses:`/`with:` at 4
    spaces, `with:` entries at 6 spaces."""
    jobs = []
    cur = None
    in_with = False
    uses_integration = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if indent == 2 and re.match(r"^[A-Za-z0-9_-]+:(\s+#.*)?$", stripped):
            if cur is not None and uses_integration:
                jobs.append(cur)
            cur, in_with, uses_integration = {}, False, False
            continue
        if cur is None:
            continue
        if indent == 4:
            in_with = False
            if stripped.startswith("uses:"):
                uses_integration = _clean(stripped[len("uses:"):]) == _INTEGRATION_USES
            elif stripped == "with:":
                in_with = True
            continue
        if indent == 6 and in_with and ":" in stripped:
            key, _, val = stripped.partition(":")
            cur[key.strip()] = _clean(val)
    if cur is not None and uses_integration:
        jobs.append(cur)
    return jobs


def _extract_step_run(text, step_id):
    """Extract the literal `run: |` block of the step with `id: <step_id>`,
    dedented to runnable form. Lets the test execute the workflow's REAL bash."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and lines[i].strip() != f"id: {step_id}":
        i += 1
    assert i < len(lines), f"step id '{step_id}' not found"
    while i < len(lines) and not re.match(r"^\s+run: \|\s*$", lines[i]):
        i += 1
    assert i < len(lines), f"`run: |` block not found for step '{step_id}'"
    run_indent = len(lines[i]) - len(lines[i].lstrip(" "))
    i += 1
    block = []
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "":
            block.append("")
        elif (len(ln) - len(ln.lstrip(" "))) <= run_indent:
            break
        else:
            block.append(ln)
        i += 1
    return textwrap.dedent("\n".join(block))


def _job_env(job, run_number="1", run_attempt="1"):
    env = {}
    for field, var in _ENV_MAP.items():
        env[var] = job.get(field, _DEFAULTS.get(field, "")) or _DEFAULTS.get(field, "")
    env["IN_EXEC"] = env["IN_EXECMODE"]      # preflight validates via IN_EXEC
    env["RUN_NUMBER"] = run_number
    env["RUN_ATTEMPT"] = run_attempt
    return env


class SelfTestArtifactUniqueness(unittest.TestCase):
    def setUp(self):
        self.jobs = _parse_caller_jobs(_SELFTEST.read_text())
        self.integration_text = _INTEGRATION.read_text()
        self.preflight = _extract_step_run(self.integration_text, "preflight")

    # ---- run the workflow's ACTUAL preflight block ----
    def _run_preflight(self, env):
        out = _REPO / "test-logs"
        out.mkdir(exist_ok=True)
        # sanitize the temp filename (the invocation_id under test may contain '/')
        tag = re.sub(r"[^A-Za-z0-9._-]", "_", env.get("IN_INVOCATION_ID", "") or "empty")
        gho = out / f"_preflight_gho_{tag}.txt"
        if gho.exists():
            gho.unlink()
        proc = subprocess.run([_bash, "-c", self.preflight],
                              env={**env, "GITHUB_OUTPUT": str(gho), "PATH": __import__("os").environ["PATH"]},
                              capture_output=True, text=True)
        parsed = {}
        for line in gho.read_text().splitlines() if gho.exists() else []:
            k, _, v = line.partition("=")
            parsed[k] = v
        gho.unlink(missing_ok=True)
        return proc.returncode, parsed

    # ---- structural / non-bash checks ----
    def test_found_all_four_caller_jobs(self):
        self.assertEqual(len(self.jobs), 4,
                         f"expected 4 integration caller jobs, parsed {len(self.jobs)}")

    def test_every_caller_has_an_invocation_id(self):
        for job in self.jobs:
            self.assertTrue(job.get("invocation_id"),
                            f"caller job missing invocation_id: {job}")

    def test_name_is_wired_to_preflight_output_not_raw_inputs(self):
        # The Upload step's name: and Emit's summary_artifact must both come from
        # the preflight-produced value; raw inputs.invocation_id must NOT appear in
        # the upload name (that is the whole point of item 1b).
        upload = _extract_field(self.integration_text, "name:",
                                after_uses="actions/upload-artifact")
        self.assertEqual(upload, "${{ steps.preflight.outputs.artifact_name }}",
                         f"upload-artifact name: is not the preflight output: {upload!r}")
        self.assertNotIn("inputs.invocation_id", upload)
        self.assertIn("ARTIFACT_NAME: ${{ steps.preflight.outputs.artifact_name }}",
                      self.integration_text)
        self.assertIn("summary_artifact=$ARTIFACT_NAME", self.integration_text)

    # ---- behavioral checks: execute the real preflight ----
    @_needs_bash
    def test_run_local_artifact_names_are_unique(self):
        names = []
        for job in self.jobs:
            rc, parsed = self._run_preflight(_job_env(job))
            self.assertIn("artifact_name", parsed, f"no artifact_name for {job}")
            names.append(parsed["artifact_name"])
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(len(set(names)), len(names),
                         f"colliding artifact name(s) within one run: {dupes}\nall={names}")
        for n in names:
            self.assertRegex(n, _SAFE_NAME_RE, f"unsafe artifact name: {n!r}")

    @_needs_bash
    def test_unsafe_invocation_id_classified_without_unsafe_name(self):
        # An unsafe invocation_id (path-breaking '/') must be a validation
        # rejection (rc=3) AND must NOT reach the artifact name in unsafe form.
        env = _job_env({"component": "rag", "package_name": "pgedge-rag-server",
                        "channel": "daily", "container_alias": "rocky9-amd64",
                        "pg_major": "17", "family": "rpm", "arch": "amd64",
                        "execution_mode": "preview", "invocation_id": "bad/id"})
        returncode, parsed = self._run_preflight(env)
        self.assertEqual(returncode, 3, "unsafe invocation_id was not rejected (rc!=3)")
        self.assertEqual(parsed.get("rc"), "3")
        name = parsed.get("artifact_name", "")
        self.assertTrue(name, "preflight produced no artifact_name for the rejected input")
        self.assertRegex(name, _SAFE_NAME_RE, f"rejected input produced an unsafe name: {name!r}")
        self.assertNotIn("/", name)
        self.assertNotIn("bad/id", name)


def _extract_field(text, key, after_uses):
    """Return the value of `<key> <value>` on the first line at/after a line
    containing `uses: <after_uses>` (used to read the Upload step's name:)."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and f"uses: {after_uses}" not in lines[i]:
        i += 1
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith(key):
            return s[len(key):].strip()
        i += 1
    raise AssertionError(f"{key!r} not found after uses: {after_uses}")


if __name__ == "__main__":
    unittest.main()
