"""Follow-up item 1 — prove the self-test's reusable-workflow calls cannot
produce a colliding upload-artifact name WITHIN A SINGLE RUN.

`actions/upload-artifact` (v4+) forbids two artifacts sharing a name in one run;
a collision makes the second uploader fail the whole self-test for a reason that
has nothing to do with the code under test. The artifact name is composed by
`.github/workflows/pep-integration.yml` (Emit + Upload) from a fixed set of
request dimensions plus a caller-provided `invocation_id`. Within one run
`github.run_number`/`github.run_attempt` are constant, so they are NOT a
discriminator between concurrent caller jobs — the dimensions + invocation_id
are the only thing that can keep the names distinct.

This check re-derives that name for every caller job in pep-selftest.yml (every
job that `uses:` the integration workflow) and asserts the run-local names are
pairwise unique. It is deliberately STDLIB-ONLY (no PyYAML): the CI unit job
runs on a setup-python interpreter that is not guaranteed to have PyYAML, and
the two workflow files are authored here in a simple, flat block style, so a
small indentation-aware reader is both sufficient and dependency-free.
"""
import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SELFTEST = _REPO / ".github" / "workflows" / "pep-selftest.yml"
_INTEGRATION_USES = "./.github/workflows/pep-integration.yml"

# Defaults MUST mirror pep-integration.yml `on.workflow_call.inputs.*.default`.
# If a default there changes, this dict must change with it (both are the
# contract for what an omitted input resolves to).
_DEFAULTS = {"scenario": "certification", "mode": "observe", "execution_mode": "preview",
             "invocation_id": ""}
# Order MUST mirror the artifact-name template in pep-integration.yml (Emit +
# Upload). run_number/run_attempt are omitted on purpose: they are identical for
# every job in one run and so cannot disambiguate concurrent calls. invocation_id
# is NOT in this base tuple: the workflow appends it as a `-<id>` suffix only when
# non-empty (matched in _artifact_discriminator below).
_NAME_FIELDS = ("component", "family", "arch", "pg_major", "channel",
                "container_alias", "scenario", "execution_mode")


def _clean(value):
    """Strip an inline ' # comment', surrounding quotes, and whitespace."""
    # split on the first ' #' (comment marker); none of our values contain it
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _parse_caller_jobs(text):
    """Return a list of {input_key: value} dicts, one per job that `uses:` the
    integration workflow. Indentation contract (we author the file): job header
    at 2 spaces, `uses:`/`with:` at 4 spaces, `with:` entries at 6 spaces."""
    jobs = []
    cur = None            # current job's collected fields, or None
    in_with = False       # inside the current job's `with:` block
    uses_integration = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        # a job header is `  <name>:` at 2 spaces, possibly with a trailing
        # inline comment (`  preview-call:   # caller job`).
        if indent == 2 and re.match(r"^[A-Za-z0-9_-]+:(\s+#.*)?$", stripped):
            # new job header -> flush the previous one
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
    if cur is not None and uses_integration:   # flush the final job
        jobs.append(cur)
    return jobs


def _artifact_discriminator(job):
    base = "pep-summary-" + "-".join(
        job.get(f, _DEFAULTS.get(f, "")) or _DEFAULTS.get(f, "")
        for f in _NAME_FIELDS)
    # matches the workflow: `-<invocation_id>` suffix only when non-empty
    inv = job.get("invocation_id", "") or ""
    return base + (f"-{inv}" if inv else "")


class SelfTestArtifactUniqueness(unittest.TestCase):
    def setUp(self):
        self.jobs = _parse_caller_jobs(_SELFTEST.read_text())

    def test_found_all_four_caller_jobs(self):
        # guards the parser itself: if the extraction silently returns fewer
        # jobs, the uniqueness check below would be vacuously true.
        self.assertEqual(len(self.jobs), 4,
                         f"expected 4 integration caller jobs, parsed {len(self.jobs)}")

    def test_every_caller_has_an_invocation_id(self):
        # the robust discriminator is a validated caller-provided id, not the
        # incidental dimensions — require each caller to set one explicitly.
        for job in self.jobs:
            self.assertTrue(job.get("invocation_id"),
                            f"caller job missing invocation_id: {job}")

    def test_run_local_artifact_names_are_unique(self):
        names = [_artifact_discriminator(j) for j in self.jobs]
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(len(set(names)), len(names),
                         f"colliding artifact name(s) within one run: {dupes}\nall={names}")


if __name__ == "__main__":
    unittest.main()
