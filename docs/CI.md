# CI — `PEP Regression` GitHub Actions workflow

This repository ships a GitHub Actions workflow at `.github/workflows/pep-regression.yml` that lets you run the pgEdge PEP regression framework across PostgreSQL versions, package families, and architectures in parallel from the GitHub Actions UI.

The workflow wraps the existing `run_pep_tf.sh` framework without changing its behavior. Each matrix target spins up one GitHub-hosted runner and invokes the same script you'd run locally, just with matrix target-specific flags.

> A second, **caller-facing** workflow — `.github/workflows/pep-integration.yml` — lets another repository’s release pipeline call PEP to install and certify **one** published package as a `workflow_call` job. See [PEP Integration reusable workflow](#pep-integration-reusable-workflow-pep-integrationyml) at the end of this document.

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
| `components` | text | `all` | Components to test. `all` or CSV of names (e.g. `pgbouncer,server`). Same vocabulary as `run_pep_tf.sh --components`. Passed through to every matrix target. |
| `repo` | choice | `release` | pgEdge repo channel. `release` / `staging` / `daily`. Passed through. |
| `execution_mode` | choice | `preview` | `preview` = `--help` + `--dry-run` only (no Docker pulls, no pytest, ~seconds per matrix target). `full` = real framework execution. |
| `containers` | text | *(empty)* | Custom container override (csv). Empty = use `containers_list.json` defaults. Accepts both aliases and canonical names, plus a listed platform's opposite arch even if only one arch is listed (e.g. `ubuntu2404-amd64` when only `ubuntu2404-arm64` exists) — the counterpart is synthesized on the fly. Special value `all` (sole token) = entire catalog, including `enabled: false`, but is **catalog-only** and does NOT include these implicit counterparts. Example: `'rocky9-arm64, ubuntu2404-amd64'`. See [Selecting container targets at runtime](#selecting-container-targets-at-runtime) for the full table and validation behavior. |

The first three are cross-producted into a matrix. With all three at `all`, that's 3 × 2 × 2 = **12 matrix targets** running in parallel.

The `components` input is validated in the plan job — invalid component names fail the plan job before any test matrix target spawns. Same for invalid `pg_versions`, `families`, `arches`, and `containers` (global-zero, unknown names, etc.).

## Matrix shape

```
pg_versions × families × arches
```

| `arch` | Runner image |
|---|---|
| `arm64` | `ubuntu-24.04-arm` |
| `amd64` | `ubuntu-24.04` |

A small `plan` job runs first, validates the inputs, and emits the matrix as JSON to `$GITHUB_OUTPUT`. The `test` job consumes that via `fromJson(needs.plan.outputs.matrix)` and spawns one runner per matrix target.

## What runs inside each matrix target

The matrix target's runner checks out the repo, sets up Python 3.11, installs `requirements.txt`, verifies Docker, and then invokes the framework with the matrix target's flags:

```bash
./run_pep_tf.sh \
  --pgver <P> \
  --platforms <F> \
  --arch <A> \
  --components <CSV from inputs.components> \
  --repo <inputs.repo> \
  --containers <inputs.containers>   # empty = catalog defaults; otherwise a custom allow-list
  [--dry-run]                        # only in preview mode
```

The framework then iterates over the containers selected for this matrix target's family+arch — exactly as `pytest` parametrizes locally. The selected set is either:

- The catalog's `enabled: true` entries (the default behavior, used when `inputs.containers` is empty), or
- The user's custom allow-list, filtered down to this matrix target's `(family, arch)` cell (when `inputs.containers` is non-empty — see [Selecting container targets at runtime](#selecting-container-targets-at-runtime)).

Multiple containers run sequentially **within** a single matrix target; matrix targets run in **parallel** across separate runner VMs.

## Preview vs full mode

| Mode | What runs per matrix target | Cost | Side effects |
|---|---|---|---|
| `preview` (default) | `./run_pep_tf.sh --help` + `--dry-run`. The dry-run resolves containers, prints `[container-resolution]` and `[image-resolution]` lines, then exits. | seconds | None — no Docker pulls, no pytest, no package installs |
| `full` | `./run_pep_tf.sh` without `--dry-run` (the real framework). | minutes per container per matrix target | Docker pulls, prereq installs, pytest. Real regression. |

Use `preview` to verify the workflow would do the right thing without paying real CI cost — e.g. after changing `containers_list.json` or before kicking off a long matrix run.

## Artifacts

Every matrix target uploads `test-logs/` as an artifact. Names include the run number and run attempt so successive runs (and re-runs of the same run) don't collide:

```
test-logs-r{run_number}-a{run_attempt}-pg{N}-{family}-{arch}
```

In addition, an `aggregate` job runs after the matrix and packages every per-target artifact into a single consolidated archive:

```
pep-regression-r{run_number}-a{run_attempt}-all-slices
```

One download for everything, or per-target for targeted triage.

### `workflow-summary.txt`

Every artifact contains a compact `workflow-summary.txt` (~25 lines) that captures:

- Timestamp, GitHub context (sha, ref, run_id, run_number, run_attempt, event_name, actor)
- Runner identity (os, arch, name, label)
- Matrix-target metadata (pg, family, arch) and inputs (components, repo, execution_mode)
- Python and Docker versions on the runner
- The exact framework CLI invocation (also captured separately as `framework-command.txt`)
- Dry-run output (preview mode) or framework reports manifest (full mode)

For triage, attach this single file — it carries everything you'd otherwise dig through the full job log for.

### Full-mode artifact contents

In full mode each matrix target's artifact also contains the framework's own outputs under `test-logs/`:

- `server/{pg}/report-{family}-server-{pg}.html` — pytest HTML report
- `server/{pg}/report-{family}-server-{pg}.xml` — JUnit XML
- `server/{pg}/index.html` — per-PG component index
- `server/index.html` — component index
- `index.html` — master index
- `consolidated-{timestamp}/` — framework's consolidated report

### Report layers — three distinct things

A workflow run produces reports at three levels. They are easy to confuse, so:

| Layer | Where | Scope | Produced by |
|---|---|---|---|
| **Per-component report** | `{component}/{pg}/report-{family}-{component}-{pg}.html` inside each per-target artifact | One component, one PG, one matrix target | The framework (`run_pep_tf.sh`), unchanged |
| **Per-target consolidated** | `consolidated-{timestamp}/index.html` inside each per-target artifact | All components within a single matrix target | The framework, unchanged |
| **Cross-target consolidated (new in v2.1)** | `consolidated-report.html` at the root of the `pep-regression-r{N}-a{M}-all-slices` aggregate artifact | Every component across every matrix target in the whole run | The CI-only generator `utillities/ci_consolidated_report.py`, run by the aggregate job |

The **aggregate artifact** (`pep-regression-r{N}-a{M}-all-slices`) is the single
downloadable archive. It bundles all per-target trees (each with its own
per-component and per-target-consolidated reports) plus the new cross-target
`consolidated-report.html` at its root. Open that file first for the
whole-run view; drill into the per-target trees for detail.

For preview-mode runs (execution_mode=preview) no tests run, so
`consolidated-report.html` is a short placeholder noting that full mode is
needed to produce results.

Note on status: a matrix target/runner being green reflects workflow completion, not
test outcomes. The cross-target report carries an explicit banner about this,
shows real PASS/FAILED/SKIPPED counts per row, and includes a "Report Issues"
count for matrix targets that produced no reports, unparseable reports, or zero test
cases — so report-integrity problems are visible even when no test failed.

## Selecting container targets at runtime

By default, every matrix target uses the containers marked `enabled: true` in
`configuration/containers_list.json`. To choose a custom set per run without
editing the catalog file, use the `containers` workflow input (in CI) or the
`--containers` flag (locally). The override is a **global allow-list**: each
matrix target filters it down to that target's own `(family, arch)`.

### Catalog aliases (user-facing names)

Both the user-facing alias and the canonical name are accepted by the override.
Aliases are shorter and match the workflow's `arches` vocabulary (`-arm64` /
`-amd64`).

| Alias              | Canonical name             | Family | Arch    | Enabled (default) | Description                |
|---|---|---|---|---|---|
| `rocky9-arm64`     | `auto-rocky9-arm`          | rpm    | arm64   | true              | Rocky Linux 9 / ARM64      |
| `rocky10-arm64`    | `auto-rocky10-arm`         | rpm    | arm64   | false             | Rocky Linux 10 / ARM64     |
| `alma9-arm64`      | `auto-alma9-arm`           | rpm    | arm64   | false             | AlmaLinux 9 / ARM64        |
| `alma10-arm64`     | `auto-alma10-arm`          | rpm    | arm64   | false             | AlmaLinux 10 / ARM64       |
| `oel9-arm64`       | `auto-oel9-arm`            | rpm    | arm64   | false             | Oracle Linux 9 / ARM64     |
| `oel10-arm64`      | `auto-oel10-arm`           | rpm    | arm64   | false             | Oracle Linux 10 / ARM64    |
| `rocky9-amd64`     | `my-rocky9-amd`            | rpm    | amd64   | false             | Rocky Linux 9 / AMD64      |
| `alma9-amd64`      | `auto-alma9-amd`           | rpm    | amd64   | false             | AlmaLinux 9 / AMD64        |
| `oel9-amd64`       | `auto-oel9-amd`            | rpm    | amd64   | false             | Oracle Linux 9 / AMD64     |
| `ubuntu2204-arm64` | `auto-ubuntu2204-arm`      | deb    | arm64   | true              | Ubuntu 22.04 LTS / ARM64   |
| `ubuntu2404-arm64` | `auto-ubuntu2404-arm`      | deb    | arm64   | false             | Ubuntu 24.04 LTS / ARM64   |
| `debian11-arm64`   | `auto-debian11-arm`        | deb    | arm64   | false             | Debian 11 Bullseye / ARM64 |
| `debian12-arm64`   | `auto-debian12-arm`        | deb    | arm64   | false             | Debian 12 Bookworm / ARM64 |
| `debian13-arm64`   | `auto-debian13-arm`        | deb    | arm64   | false             | Debian 13 Trixie / ARM64   |
| `debian13-amd64`   | `auto-debian13-amd`        | deb    | amd64   | false             | Debian 13 Trixie / AMD64   |
| `ubuntu2604-arm64` | `auto-ubuntu2604-arm`      | deb    | arm64   | true              | Ubuntu 26.04 LTS / ARM64   |
| `ubuntu2604-amd64` | `auto-ubuntu2604-amd`      | deb    | amd64   | true              | Ubuntu 26.04 LTS / AMD64   |

The "Enabled (default)" column mirrors `configuration/containers_list.json` as of
this branch. It changes as the catalog is edited; for the live state at any
moment, run `./run_pep_tf.sh --list-containers` locally.

### Implicit opposite-arch counterparts

The catalog lists many platforms under a single architecture (e.g. only
`ubuntu2404-arm64`). Because pgEdge platform support is arm64/amd64-equivalent,
**the opposite architecture of any listed platform is also selectable via the
override, even though it has no catalog entry.** Request it by the natural alias
or canonical name and the resolver synthesizes the counterpart on the fly:

```bash
# ubuntu2404 is listed only as -arm64, but the amd64 counterpart is accepted:
./run_pep_tf.sh --pgver 17 --platforms deb --arch amd64 --components server \
  --containers ubuntu2404-amd64
# -> resolves to auto-ubuntu2404-amd, image ubuntu:24.04, platform linux/amd64
```

Rules and caveats:

- **Real catalog entries always win.** Synthesis happens only on a lookup miss;
  a name/alias already in the catalog resolves to its real entry.
- **Counterpart only, never a new OS.** A token synthesizes only when the *same*
  OS/version exists under the other arch. A genuinely unknown OS/version still
  **fails fast**.
- **The synthesized canonical name** (e.g. `auto-ubuntu2404-amd`) preserves the
  sibling's prefix and flips only the arch suffix. It is an **internal runtime
  identifier** used to drive image/platform resolution — not a promise that a
  future explicit catalog entry will use that exact name.
- **`all` is catalog-only.** The `all` token expands to the real catalog and
  does **not** include implicit counterparts. After this feature, "all catalog
  entries" and "all supported implicit targets" are deliberately not the same
  set — name a counterpart explicitly to run it.
- **Equivalence is assumed, not verified.** If a platform were ever genuinely
  single-arch, its synthesized counterpart would be accepted here and fail later
  at `docker pull` (no matching manifest) rather than up front.

### CI examples

| Goal | `containers` input | Other inputs |
|---|---|---|
| Default behavior (use catalog `enabled: true`) | *(empty)* | any |
| Run two specific targets across the full matrix | `rocky9-arm64, ubuntu2404-arm64` | `families=all`, `arches=all` |
| Run a listed platform's unlisted opposite arch (implicit counterpart) | `ubuntu2404-amd64` | `families=deb`, `arches=amd64` |
| Smoke against every catalog entry (incl. `enabled: false`) | `all` | as desired |
| Single-platform run pinned to one container | `alma9-arm64` | `families=rpm`, `arches=arm64` |

### Local CLI examples

```bash
# List the catalog (alias, canonical name, family, arch, enabled, description)
./run_pep_tf.sh --list-containers

# Use override locally
./run_pep_tf.sh --pgver 17 --platforms all --arch arm64 --components server \
  --containers rocky9-arm64,ubuntu2404-arm64

# Set via env (CLI flag wins if both are set)
PEP_CONTAINERS=rocky9-arm64 ./run_pep_tf.sh --pgver 17 --platforms rpm --components server
```

### Validation behavior

| Situation | Outcome |
|---|---|
| `containers` empty | Use catalog `enabled: true` subset (current behavior, byte-equivalent logs). |
| Override contains an alias or canonical name | Both accepted. |
| Override names a listed platform's opposite arch (e.g. `ubuntu2404-amd64` when only `-arm64` is listed) | Accepted — the counterpart is synthesized on the fly and resolves to the same image. Real catalog entries still win first. |
| Override is `all` (sole token) | Expand to the entire catalog, including `enabled: false`. **Catalog-only** — does NOT include implicit opposite-arch counterparts. |
| Override mixes `all` with other tokens | **Fail-fast.** |
| Unknown name / alias | **Fail-fast** with the valid names + aliases listed in the diagnostic. |
| Container's family or arch matches none of the selected `families` / `arches` (global-zero) | **Fail-fast** in the CI plan job before any test target spawns; locally at script startup. |
| Container valid catalog-wide but not in *this matrix target's* (family, arch) | Logged as out-of-scope; that target proceeds with its remaining matches (or surfaces as `NO CONTAINERS SELECTED` in the consolidated report if it has none). |
| Container with `enabled: false` is explicitly requested | Allowed — override wins. The `enabled:` flag only gates the default-membership; an explicit override may still select it. |
| `--target aws` combined with any non-empty override | **Fail-fast.** The override applies only to `--target docker`. |

### Preference hierarchy

CLI flag > env var > catalog default:

| Layer | Source | When it wins |
|---|---|---|
| CLI flag (locally) / workflow input (in CI) | `--containers <csv>` / `inputs.containers` | non-empty value present |
| `PEP_CONTAINERS` env var (locally only) | shell environment | when the layer above is empty; in CI the workflow explicitly blanks `PEP_CONTAINERS` so no inherited env can leak |
| Catalog default | `enabled: true` entries in `containers_list.json` | when both above are empty |

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

Each matrix target runs exactly what you can run on your laptop:

```bash
./run_pep_tf.sh \
  --pgver 17 --platforms deb --arch arm64 \
  --components server --repo release
```

The `--arch` flag is optional. Omitting it leaves all enabled containers of the matching family in scope (the pre-existing behavior). With `--arch arm64`, only containers whose name contains `-arm` are kept; with `--arch amd64`, only `-amd`. Validation rejects anything else with exit 2.

The `--dry-run` flag (also optional) resolves containers, prints what would run, and exits. Useful for confirming a matrix target's container/image picks before paying the full runtime cost.

## `configuration/containers_list.json` is a test-time variable

This file controls which containers each matrix target will actually exercise. Enable or disable entries depending on what coverage you want at any given time:

- A small enabled set → quick runs, narrow coverage
- A wider enabled set → broader coverage, longer per-target runtime
- A matrix target whose enabled set resolves to zero containers → exits gracefully with `[container-resolution] … -> 0 container(s): (none)`

The framework and workflow are designed to handle any combination correctly. There is no single "correct" shape for this file. For broad regression coverage, enable as many entries as you have confidence in; for focused investigation, disable everything except the OSes you care about.

## Troubleshooting

| Symptom | Likely cause | Resolution |
|---|---|---|
| Plan job exits 2 with `[plan-job] ERROR: invalid X value(s)` | An input value isn't in the allowed set | Fix the input value; see the input table above for accepted values |
| Plan job exits 2 with `[plan-job] ERROR: components value … has no valid entries after parsing` | `components` is empty or all commas | Provide at least one valid component name, or leave at `all` |
| A matrix target's `runs-on` header shows the wrong runner | (Should not happen — the plan job pins runner per arch) | File as a workflow bug if you see it |
| `Login Succeeded` doesn't appear in `[dockerhub] Docker Hub login` step on a full-mode run | Secrets `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` not configured, or `execution_mode != full` | Configure secrets (see Docker Hub authentication section) |
| A matrix target fails at framework prereq step on a specific OS | Framework-level finding — record but the workflow itself is correctly surfacing it | Triage as a framework issue, not a workflow issue |
| A matrix target fails with `Base image 'X' not found and could not be pulled` | Docker Hub doesn't publish the requested image+arch combination | The container's docker-pull-fix correctly surfaces this; disable that container in `containers_list.json` until upstream availability is confirmed |
| Run #N's deb matrix targets were slow / hung at scale | Specific container interactions under wider matrix can surface framework-side issues | The main-branch `setup_debian` refactor (DEBIAN_FRONTEND, universe repo for sq) closed the previously-known hangs |

## Workflow-level acceptance summary

The workflow is designed to:

- Spawn up to 12 parallel runners (PG × family × arch)
- Pin each runner to the correct architecture
- Validate inputs in the plan job before any test matrix target spawns
- Iterate containers sequentially within a matrix target via pytest parametrize (the framework's design)
- Capture compact per-target triage info into `workflow-summary.txt`
- Aggregate all per-target artifacts into one consolidated archive
- Keep `preview` mode the default so accidental cost is bounded

Framework-level findings — the kinds of issues regression testing surfaces (OS-specific test failures, version mismatches, SBOM tool gaps) — are uploaded as part of the per-target reports and remain the framework team's domain to triage. The workflow's job is to make them visible across the matrix, not to fix them.

---

## PEP Integration reusable workflow (`pep-integration.yml`)

`.github/workflows/pep-integration.yml` is a `workflow_call` reusable workflow. A build/release pipeline in another repo (e.g. a component that just published a package) calls it to install **one** already-published target and report a truthful, per-rung identity result — without failing the caller's release (in the default `observe` mode). It is single-target: one component / family / arch / PG major / container per call.

Because `pgedge-pep-test` is public, cross-repo callers need **no read token** — the workflow checks itself out with the built-in `GITHUB_TOKEN`. Pin the implementation you call with an **immutable commit SHA** via `pep_implementation_ref`.

### Minimal cross-repo caller

```yaml
# in the caller repo, e.g. .github/workflows/release.yml
jobs:
  pep-certify:
    uses: pgEdge/pgedge-pep-test/.github/workflows/pep-integration.yml@<PEP_COMMIT_SHA>
    with:
      # what to install
      component: rag
      package_name: pgedge-rag-server
      channel: staging                 # release | staging | daily
      pg_major: "17"
      family: deb                      # rpm | deb
      arch: amd64                      # amd64 | arm64
      container_alias: debian13-amd64  # catalog target (see containers_list.json)
      # caller-supplied build identity
      expected_version: "2.0.0"        # required (component version -> L1)
      expected_deb: "2.0.0~beta1-1.trixie"   # optional, explicit -> L2a
      expected_binary: "2.0.0-beta1"         # optional, explicit -> L2b
      # run controls
      scenario: certification          # POC scenario (see limitations)
      mode: observe                    # observe | gate
      execution_mode: full             # preview | full
      invocation_id: rag-deb-amd64-pg17   # unique per call within one run
      pep_implementation_ref: <PEP_COMMIT_SHA>   # use the SAME full SHA as in uses:
    secrets:                           # optional — authenticated image pulls
      DOCKERHUB_USERNAME: ${{ secrets.DOCKERHUB_USERNAME }}
      DOCKERHUB_TOKEN: ${{ secrets.DOCKERHUB_TOKEN }}
```

### Inputs

**Required:** `component`, `package_name`, `channel` (`release`/`staging`/`daily`), `expected_version`, `container_alias`, `pg_major`, `family` (`rpm`/`deb`), `arch` (`amd64`/`arm64`), and `pep_implementation_ref`.

**Pinning responsibility (caller-owned):** use the **same immutable full commit SHA** in *both* the job-level `uses:` reference and `pep_implementation_ref` — `uses:` selects which workflow definition GitHub loads, and `pep_implementation_ref` is the ref the workflow then checks out and runs. The workflow records both the requested ref and the resolved SHA (`git rev-parse HEAD`) into `provenance.json` for audit, but it does **not** enforce that the ref is a full SHA (versus a branch/tag) and does **not** verify that the two caller references match. Keeping them identical and immutable is the caller's responsibility.

**Optional exact-identity inputs** (default `""`): `expected_rpm`, `expected_deb`, `expected_binary`, plus `expected_buildnum` and `effective_tag`. Supplying an explicit `expected_rpm`/`expected_deb` makes **L2a** attemptable; an explicit `expected_binary` makes **L2b** attemptable. `expected_buildnum`/`effective_tag` are accepted and validated and currently mark the corresponding rung as *derivation-pending*, but they do **not** by themselves make L2 attemptable and are **not** currently surfaced as dedicated fields in provenance or the result summary (deriving exact strings from them is a deliberate, still-open decision). So the run stays at L1 unless you pass the explicit `expected_*` strings.

**Run controls** (optional): `scenario` (default `certification`), `mode` (`observe`/`gate`, default `observe`), `execution_mode` (`preview`/`full`, default `preview`), `invocation_id` (default `""`).

**Caller-supplied build identity vs PEP defaults:** the caller is authoritative for *what build is being certified*. PEP's `configuration/config{PG}.env` and repo-root `.env` continue to supply framework/standalone test settings (prereqs, paths, and the like), but the integration `expected_version` and the `expected_*` identity fields remain **caller-controlled** and are **not** backfilled from those files. The resolved channel and scenario the run actually used are reported back in `resolved-config.json` with their source.

### preview vs full

- **`preview`** (default): `--dry-run` only — no Docker, no install, no pytest. Fast wiring/validation check; `execution_status=preview`, `test_verdict=not_run`.
- **`full`**: real install + identity + component tests inside a container for the chosen `container_alias`.

### observe vs gate

- **`observe`** (default, and the POC mode): the job stays green even when the summarizer records a handled `infra_failure` or a `test_verdict=fail` — the caller's release is never blocked. Read the outcome from the outputs/artifact.
- **`gate`**: the summarize step exits non-zero (fails the job) unless the run is `completed` + `pass`. Gate is implemented but **not yet exercised on live CI** — treat production gating as deferred (see limitations).

### Outputs

`execution_status` (`completed` / `preview` / `incomplete` / `infra_failure`), `test_verdict` (`pass` / `fail` / `not_run`), `enforcement_mode`, `identity_evidence` (JSON `{l2a,l2b,l1}`, each `proven`/`not_proven`/`not_attempted`), `resolved_channel` (`{value,source}`), and `summary_artifact` (the uploaded artifact name).

### Artifacts + `invocation_id` uniqueness

Every call **always** uploads the `test-logs/` directory, but **its contents vary by outcome**. A `preview`/validation call produces a subset (e.g. `summary.json`, `provenance.json`, and — once the resolver runs — `resolved-config.json`), with no install/identity evidence or product reports. A **successful full run** additionally supplies `install-evidence.json`, `identity-evidence.json`, the `current-run.json` manifest, and the JUnit/HTML test reports. The artifact name is built from the target dimensions; when a single GitHub run calls this workflow more than once with otherwise-identical dimensions (e.g. two calls differing only by `mode`), give each call a distinct `invocation_id` — `upload-artifact` forbids duplicate names within one run, and `invocation_id` is the discriminator folded into the name (charset `A-Za-z0-9._-`, ≤64).

### What L1 / L2a / L2b actually prove

- **L2a — exact package-manager version-release identity (strong):** the installed package's exact NVR (RPM) or `Version` (DEB) string equals the caller's `expected_rpm`/`expected_deb`. Proves the exact package-manager version-release was installed.
- **L2b — exact binary version identity (strong):** the binary's self-reported version string equals `expected_binary`.
- **L1 — component version (weak / degraded):** the component's reported version *contains* the normalized `expected_version`. This is a coarse substring/containment check — it confirms the right version line, **not** the exact build. A run that proves only L1 must not be described as exact-build.

L2a and L2b are **exact version-string** matches, not a byte-level or checksum/content assertion (there is no L3 content proof in this POC). Each rung is independent and reported separately; a rung that is attemptable but unproven (mismatch or missing observation) makes `test_verdict=fail`. L2 is reachable only when the corresponding explicit `expected_*` string is supplied.

### DockerHub credentials (optional)

`DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` are optional secrets. Pass them **explicitly** (as above) — not `secrets: inherit` — to authenticate base-image pulls in `full` mode and avoid anonymous rate limits. Omit them to rely on anonymous pulls. No read token of any kind is required.

### Current limitations (POC)

- **`certification` is the only wired scenario.** It is a no-replacement run: certification forces the effective upgrade off, resolved once as `upgrade={value:"false", source:"scenario_policy"}` and read back by the runtime, so no package replacement or channel switch happens around the identity being certified.
- **`scenario=upgrade` is rejected** (validation error → `incomplete`/`not_run`). The upgrade-replacement flow is not implemented yet.
- **Production gating is deferred.** `mode=gate` exists but has not been run on live CI; the POC runs `observe`.
- **Single target per call**, and L2 requires explicit `expected_*` strings (no automatic derivation from buildnum/tag yet).
- The live full-mode path has been validated for a **DEB** target; the RPM full-mode path is covered by unit tests but not yet run live.

The next step is a real cross-repo caller in a component's release pipeline (report-only), which exercises this workflow end-to-end from the publishing side.
