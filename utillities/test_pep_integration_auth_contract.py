"""Public-repo auth contract (2026-08-20).

PEP is a PUBLIC repo, so the reusable integration workflow self-checks-out WITHOUT
any dedicated read token: `pep_read_token` was removed from both the workflow's
`secrets:` declaration and its checkout step, and every self-test caller stopped
passing it (a caller may not pass a secret the reusable workflow no longer
declares). This test LOCKS IN that simplification and guards the two properties
that must survive it: (1) immutable implementation-ref pinning, and (2)
resolved-SHA provenance recording.

Stdlib-only (no PyYAML), matching the CI unit job's interpreter and the style of
test_pep_selftest_artifacts.py.
"""
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_INTEGRATION = _REPO / ".github" / "workflows" / "pep-integration.yml"
_SELFTEST = _REPO / ".github" / "workflows" / "pep-selftest.yml"


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

    def test_selftest_callers_pass_no_secrets_block(self):
        # With no required secret to pass, the four local caller jobs carry no
        # `secrets:` block at all.
        self.assertNotIn("secrets:", self.selftest,
                         "self-test caller jobs should no longer declare a secrets: block")


if __name__ == "__main__":
    unittest.main()
