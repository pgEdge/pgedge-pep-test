# CI — `PEP Regression` GitHub Actions workflow

This repository ships a GitHub Actions workflow at `.github/workflows/pep-regression.yml` that lets you run the pgEdge PEP regression framework across PostgreSQL versions, package families, and architectures in parallel from the GitHub Actions UI.

The workflow wraps the existing `run_pep_tf.sh` framework without changing its behavior. Each matrix slice spins up one GitHub-hosted runner and invokes the same script you'd run locally, just with slice-specific flags.

## Quick start

1. Open the **Actions** tab.
2. Click **PEP Regression** in the left sidebar.
3. Click **Run workflow** (top right).
4. **Use workflow from**: pick the branch you want to test (e.g. `gha`).
5. Adjust inputs as needed (see below). Default is `execution_mode=preview`, which is fast and side-effect-free.
6. Click the green **Run workflow** button.

## Inputs

| Input | Type | Default | Purpose |
|---|---|---|---|
| `pg_versions` | text | `all` | PG versions to test. Values: `16`, `17`, `18`, `all`, or CSV (e.g. `16,17`). **Matrix-driving.** |
| `families` | text | `all` | Package families. Values: `rpm`, `deb`, `all`, or `rpm,deb`. **Matrix-driving.** |
| `arches` | text | `all` | Architectures. Values: `arm64`, `amd64`, `all`, or CSV. **Matrix-driving.** |
| `components` | text | `all` | Components to test. `all` or CSV of names (e.g. `pgbouncer,server`). Same vocabulary as `run_pep_tf.sh --components`. Passed through to every slice. |
| `repo` | choice | `release` | pgEdge repo channel. `release` / `staging` / `daily`. Passed through. |
| `execution_mode` | choice | `preview` | `preview` = `--help` + `--dry-run` only (no Docker pulls, no pytest, ~seconds per slice). `full` = real framework execution. |

The first three are cross-producted into a matrix. With all three at `all`, that's 3 × 2 × 2 = **12 slices** running in parallel.

The `components` input is validated in the plan job — invalid component names fail the plan job before any test slice spawns. Same for invalid `pg_versions`, `families`, `arches`.

## Matrix shape

```
pg_versions × families × arches
```

| `arch` | Runner image |
|---|---|
| `arm64` | `ubuntu-24.04-arm` |
| `amd64` | `ubuntu-24.04` |

A small `plan` job runs first, validates the inputs, and emits the matrix as JSON to `$GITHUB_OUTPUT`. The `test` job consumes that via `fromJson(needs.plan.outputs.matrix)` and spawns one runner per slice.

## What runs inside each slice

The slice's runner checks out the repo, sets up Python 3.11, installs `requirements.txt`, verifies Docker, and then invokes the framework with the slice's flags:

```bash
./run_pep_tf.sh \
  --pgver <P> \
  --platforms <F> \
  --arch <A> \
  --components <CSV from inputs.components> \
  --repo <inputs.repo> \
  [--dry-run]                  # only in preview mode
```

The framework then iterates over every enabled container in `configuration/containers_list.json` that matches the slice's family+arch — exactly as `pytest` parametrizes locally. Multiple containers run sequentially **within** a single slice; slices run in **parallel** across separate runner VMs.

## Preview vs full mode

| Mode | What runs per slice | Cost | Side effects |
|---|---|---|---|
| `preview` (default) | `./run_pep_tf.sh --help` + `--dry-run`. The dry-run resolves containers, prints `[container-resolution]` and `[image-resolution]` lines, then exits. | seconds | None — no Docker pulls, no pytest, no package installs |
| `full` | `./run_pep_tf.sh` without `--dry-run` (the real framework). | minutes per container per slice | Docker pulls, prereq installs, pytest. Real regression. |

Use `preview` to verify the workflow would do the right thing without paying real CI cost — e.g. after changing `containers_list.json` or before kicking off a long matrix run.

## Artifacts

Every slice uploads `test-logs/` as an artifact. Names include the run number and run attempt so successive runs (and re-runs of the same run) don't collide:

```
test-logs-r{run_number}-a{run_attempt}-pg{N}-{family}-{arch}
```

In addition, an `aggregate` job runs after the matrix and packages every per-slice artifact into a single consolidated archive:

```
pep-regression-r{run_number}-a{run_attempt}-all-slices
```

One download for everything, or per-slice for targeted triage.

### `workflow-summary.txt`

Every artifact contains a compact `workflow-summary.txt` (~25 lines) that captures:

- Timestamp, GitHub context (sha, ref, run_id, run_number, run_attempt, event_name, actor)
- Runner identity (os, arch, name, label)
- Slice metadata (pg, family, arch) and inputs (components, repo, execution_mode)
- Python and Docker versions on the runner
- The exact framework CLI invocation (also captured separately as `framework-command.txt`)
- Dry-run output (preview mode) or framework reports manifest (full mode)

For triage, attach this single file — it carries everything you'd otherwise dig through the full job log for.

### Full-mode artifact contents

In full mode each slice's artifact also contains the framework's own outputs under `test-logs/`:

- `server/{pg}/report-{family}-server-{pg}.html` — pytest HTML report
- `server/{pg}/report-{family}-server-{pg}.xml` — JUnit XML
- `server/{pg}/index.html` — per-PG component index
- `server/index.html` — component index
- `index.html` — master index
- `consolidated-{timestamp}/` — framework's consolidated report

## Optional: Docker Hub authentication

With many parallel runners pulling base images simultaneously, anonymous Docker Hub pulls can hit rate limits. The workflow has a conditional auth step that activates when **both** of these repo secrets are set:

- `DOCKERHUB_USERNAME` — your Docker Hub username
- `DOCKERHUB_TOKEN` — a Docker Hub Personal Access Token (Public Repo Read-Only scope is sufficient)

To configure: **Settings → Secrets and variables → Actions → New repository secret**, twice (one per secret name above). Once set, full-mode runs will authenticate before pulling.

The auth step is also gated on `execution_mode == 'full'` — preview-mode runs skip it cleanly since no images are pulled. Without secrets, the step shows as skipped and pulls run anonymously.

## Concurrency

The workflow has a concurrency group keyed on `github.ref`:

```yaml
concurrency:
  group: pep-regression-${{ github.ref }}
  cancel-in-progress: false
```

GitHub's behavior under this configuration is "at most one running + at most one pending per group". An in-flight matrix run is **not** cancelled by a re-trigger of the same branch — the running job is preserved. A third trigger in rapid succession cancels the pending one in place (FIFO is not guaranteed beyond one pending), but the running job is always protected.

## Scheduled trigger — currently disabled

A `schedule:` block is committed in the workflow YAML but commented out. When the workflow has run reliably for some time, enabling it is a single uncomment. Under `schedule:` the workflow runs with all inputs at their defaults; the `parse()` helper in the plan job and the `inputs.X || 'default'` fallbacks in the test job handle the empty-inputs shape correctly.

## Local equivalents

Each slice runs exactly what you can run on your laptop:

```bash
./run_pep_tf.sh \
  --pgver 17 --platforms deb --arch arm64 \
  --components server --repo release
```

The `--arch` flag is optional. Omitting it leaves all enabled containers of the matching family in scope (the pre-existing behavior). With `--arch arm64`, only containers whose name contains `-arm` are kept; with `--arch amd64`, only `-amd`. Validation rejects anything else with exit 2.

The `--dry-run` flag (also optional) resolves containers, prints what would run, and exits. Useful for confirming a slice's container/image picks before paying the full runtime cost.

## `configuration/containers_list.json` is a test-time variable

This file controls which containers each slice will actually exercise. Enable or disable entries depending on what coverage you want at any given time:

- A small enabled set → quick runs, narrow coverage
- A wider enabled set → broader coverage, longer per-slice runtime
- A slice whose enabled set resolves to zero containers → exits gracefully with `[container-resolution] … -> 0 container(s): (none)`

The framework and workflow are designed to handle any combination correctly. There is no single "correct" shape for this file. For broad regression coverage, enable as many entries as you have confidence in; for focused investigation, disable everything except the OSes you care about.

## Troubleshooting

| Symptom | Likely cause | Resolution |
|---|---|---|
| Plan job exits 2 with `[plan-job] ERROR: invalid X value(s)` | An input value isn't in the allowed set | Fix the input value; see the input table above for accepted values |
| Plan job exits 2 with `[plan-job] ERROR: components value … has no valid entries after parsing` | `components` is empty or all commas | Provide at least one valid component name, or leave at `all` |
| A slice's `runs-on` header shows the wrong runner | (Should not happen — the plan job pins runner per arch) | File as a workflow bug if you see it |
| `Login Succeeded` doesn't appear in `[dockerhub] Docker Hub login` step on a full-mode run | Secrets `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` not configured, or `execution_mode != full` | Configure secrets (see Docker Hub authentication section) |
| Slice fails at framework prereq step on a specific OS | Framework-level finding — record but the workflow itself is correctly surfacing it | Triage as a framework issue, not a workflow issue |
| Slice fails with `Base image 'X' not found and could not be pulled` | Docker Hub doesn't publish the requested image+arch combination | The container's docker-pull-fix correctly surfaces this; disable that container in `containers_list.json` until upstream availability is confirmed |
| Run #N's deb slices were slow / hung at scale | Specific container interactions under wider matrix can surface framework-side issues | The main-branch `setup_debian` refactor (DEBIAN_FRONTEND, universe repo for sq) closed the previously-known hangs |

## Workflow-level acceptance summary

The workflow is designed to:

- Spawn up to 12 parallel runners (PG × family × arch)
- Pin each runner to the correct architecture
- Validate inputs in the plan job before any test slice spawns
- Iterate containers sequentially within a slice via pytest parametrize (the framework's design)
- Capture compact per-slice triage info into `workflow-summary.txt`
- Aggregate all per-slice artifacts into one consolidated archive
- Keep `preview` mode the default so accidental cost is bounded

Framework-level findings — the kinds of issues regression testing surfaces (OS-specific test failures, version mismatches, SBOM tool gaps) — are uploaded as part of the per-slice reports and remain the framework team's domain to triage. The workflow's job is to make them visible across the matrix, not to fix them.
