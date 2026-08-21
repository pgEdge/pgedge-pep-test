"""Public-repo auth contract (2026-08-20).

PEP is a PUBLIC repo, so the reusable integration workflow self-checks-out WITHOUT
a dedicated caller-supplied read token: `pep_read_token` was removed from both the
workflow's `secrets:` declaration and its checkout step, and every self-test caller
stopped passing it (a caller may not pass a secret the reusable workflow no longer
declares). This test LOCKS IN that simplification and guards the two properties
that must survive it: (1) immutable implementation-ref pinning, and (2)
resolved-SHA provenance recording.

The prohibition is on `pep_read_token` SPECIFICALLY — not on all `secrets:` blocks:
a future full-mode caller may legitimately pass the optional
`DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` the reusable workflow still accepts.

Stdlib-only (no PyYAML), matching the CI unit job's interpreter and the style of
test_pep_selftest_artifacts.py.
"""
import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_INTEGRATION = _REPO / ".github" / "workflows" / "pep-integration.yml"
_SELFTEST = _REPO / ".github" / "workflows" / "pep-selftest.yml"


def _declared_call_secrets(text):
    """Return the set of secret NAMES declared under on.workflow_call.secrets in
    the reusable workflow (keys only; comment/blank lines skipped). Used to
    assert the token contract at the declaration site rather than by a blanket
    'no secrets:' scan."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and lines[i].rstrip() != "    secrets:":
        i += 1
    if i >= len(lines):
        return set()
    base, keys = 4, set()
    i += 1
    while i < len(lines):
        ln = lines[i]
        if ln.strip() and not ln.lstrip().startswith("#"):
            indent = len(ln) - len(ln.lstrip(" "))
            if indent <= base:
                break
            m = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*):", ln)
            if m:
                keys.add(m.group(1))
        i += 1
    return keys


def _checkout_step(text):
    """Return the text of the 'Checkout PEP implementation' step, from its
    `- name:` line up to (not including) the next step at the same indent, so we
    can assert what it does and does not contain."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and "name: Checkout PEP implementation" not in lines[i]:
        i += 1
    assert i < len(lines), "checkout step not found in pep-integration.yml"
    start = i
    indent = len(lines[i]) - len(lines[i].lstrip(" "))
    i += 1
    while i < len(lines):
        ln = lines[i]
        if ln.strip() and (len(ln) - len(ln.lstrip(" "))) <= indent and ln.lstrip().startswith("- "):
            break
        i += 1
    return "\n".join(lines[start:i])


class PublicAuthContract(unittest.TestCase):
    def setUp(self):
        self.integration = _INTEGRATION.read_text()
        self.selftest = _SELFTEST.read_text()

    def test_no_read_token_anywhere_in_workflows(self):
        # The dedicated read secret is fully gone from BOTH the reusable workflow
        # (declaration + usage) and every self-test caller.
        self.assertNotIn("pep_read_token", self.integration,
                         "pep-integration.yml still references pep_read_token")
        self.assertNotIn("pep_read_token", self.selftest,
                         "pep-selftest.yml still passes pep_read_token")

    def test_checkout_has_no_explicit_token_but_keeps_pinning(self):
        step = _checkout_step(self.integration)
        # No hardcoded token on the public self-checkout ...
        for line in step.splitlines():
            self.assertFalse(line.strip().startswith("token:"),
                             f"self-checkout must not pass an explicit token: {line!r}")
        # ... but the self-checkout + immutable ref pin MUST remain.
        self.assertIn("repository: pgEdge/pgedge-pep-test", step)
        self.assertIn("ref: ${{ inputs.pep_implementation_ref }}", step,
                      "immutable implementation-ref pinning must be preserved")

    def test_pep_implementation_ref_still_required(self):
        self.assertIn("pep_implementation_ref:", self.integration)
        # required: true must still be declared for the ref input.
        self.assertIn("pep_implementation_ref: {required: true", self.integration,
                      "pep_implementation_ref must remain a required input")

    def test_resolved_sha_provenance_preserved(self):
        # The resolved commit SHA must still be recorded (independent of any token).
        self.assertIn("resolved_sha=", self.integration)
        self.assertIn("provenance.json", self.integration)

    def test_reusable_workflow_declares_no_read_token_secret(self):
        # Narrowed contract (2026-08-20): prohibit `pep_read_token` SPECIFICALLY at
        # the declaration site, NOT all secrets. The reusable workflow may still
        # declare the optional DockerHub creds a future full-mode caller can pass.
        declared = _declared_call_secrets(self.integration)
        self.assertNotIn("pep_read_token", declared,
                         f"pep_read_token must not be a declared secret; got {sorted(declared)}")
        allowed = {"DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"}
        self.assertTrue(declared <= allowed,
                        f"unexpected declared secret(s): {sorted(declared - allowed)} "
                        f"(only optional DockerHub creds are allowed)")

    def test_selftest_callers_do_not_pass_read_token(self):
        # A future caller MAY carry a `secrets:` block (e.g. DockerHub); what is
        # forbidden is passing pep_read_token. Today no caller passes any secret.
        self.assertNotIn("pep_read_token", self.selftest,
                         "no self-test caller may pass pep_read_token")


if __name__ == "__main__":
    unittest.main()
