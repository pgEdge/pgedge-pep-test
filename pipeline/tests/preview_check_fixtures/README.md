# Certification preview check fixtures

Inputs for `pipeline/pep_preview_check.py` (`test_pep_preview_check.py`): one simulated run of
`.github/workflows/pep-certification-preview.yml`, recorded as the documents the coordinator's
aggregate step and the assert job receive. The tests rebuild cert-result, collection ledger,
cert-decision and the consolidated HTML report from these inputs with the REAL collector
(`pep_result_io.py`), gate (`pep_cert_gate.py`) and report (`pep_cert_report.py`), so the checker
is always exercised against documents the current code produces.

These are **not** a GitHub run. They were produced offline, with the code at main `6b66139`, by
driving the real steps with the workflow's exact fixture inputs:

- the workflow's build script (real `rpmbuild` / `dpkg-deb`) and the receipt action's
  `receipt_build.py`;
- `pep_capture_io.run_capture` behind a fake Actions API (the policy resolved with
  `resolve_component_policy`), then the invocation planner with PEP's catalogs;
- for each planned run, the real bridge (`run_pep_tf.sh --dry-run`) and `pep_result_summary.py`,
  called as `pep-integration.yml` calls them in preview.

Run identity used throughout: repository `pgEdge/pgedge-pep-test`, run id `9100000001`, run
number `77`, attempt `1`, ref `refs/pull/99/merge`, and one placeholder 40-hex SHA standing in for
both the PR merge commit and the revision under test (the preview workflow calls pep-certify.yml by
relative path, so the two are the same commit).

| file | what it is |
|---|---|
| `capture-evidence.json` | capture-evidence/1: both receipts accepted |
| `cert-plan.json` | cert-plan/1: simulated release, both publications skipped, 2 preview-eligible targets |
| `invocation-plan.json` | pep-invocation-plan/1: 6 preview runs (oel9-amd64 and debian12-amd64, PG 16-18) |
| `summaries.json` | the six per-run `summary.json` documents, keyed by their `pep-summary-*` artifact name |
| `listing.json` | the run's artifact listing as `pep_artifact_listing.py` writes it (14 artifacts) |

Package sizes, digests and artifact ids are those of that simulation; nothing here was published.
