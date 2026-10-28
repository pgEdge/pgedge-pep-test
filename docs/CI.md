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

Because `pgedge-pep-test` is public, cross-repo callers need **no read token** — the workflow checks itself out with the built-in `GITHUB_TOKEN`. Pin the implementation you call with an **immutable commit SHA** via `pep_implementation_ref`. (A release pipeline calls the [release certification adapter](#release-certification-adapter-pep-release-certifyyml) instead, normally through the [stable channel](#stable-channel-v1_stable).)

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

**`identity-evidence.json` vs `observed-identity.json`.** These are two different files with two different jobs:

- **`identity-evidence.json`** is the **authoritative verdict**: exactly `{l2a, l2b, l1}`, each `proven` / `not_proven` / `not_attempted`. This is what the run's identity outcome is judged on.
- **`observed-identity.json`** is **audit-only** — it never affects the verdict, execution status, or rung calculation. It records, for later inspection, the raw run and complete target scope (`run_token` + the same target fields as `install-evidence.json`), the package-manager identity actually observed, the parsed binary version, and the value used for the L1 component comparison. It exists so you can see *which exact package was installed* — especially in an L1-only run, where no `expected_*` pins are supplied and the rung verdict alone doesn't name the build.

`observed-identity.json` is written once a **full** run reaches identity observation — including an **identity-mismatch** run (the observations are recorded before the verdict, so a failing match still leaves them). A `preview` run, an early validation/setup failure, or any run that never reaches identity observation may not produce it.

### What L1 / L2a / L2b actually prove

- **L2a — exact package-manager version-release identity (strong):** the installed package's exact NVR (RPM) or `Version` (DEB) string equals the caller's `expected_rpm`/`expected_deb`. Proves the exact package-manager version-release was installed.
- **L2b — exact binary version identity (strong):** the binary's self-reported version string equals `expected_binary`.
- **L1 — component version (weak / degraded):** the component's reported version *contains* the normalized `expected_version`. This is a coarse substring/containment check — it confirms the right version line, **not** the exact build. A run that proves only L1 must not be described as exact-build.

L2a and L2b are **exact version-string** matches, not a byte-level or checksum/content assertion; the package bytes are proven separately (below). Each rung is independent and reported separately; a rung that is attemptable but unproven (mismatch or missing observation) makes `test_verdict=fail`. L2 is reachable only when the corresponding explicit `expected_*` string is supplied.

### Package bytes (certification)

A pinned install (L2a) is a **verified install**, and it is still an ordinary signed-repository install:
1. The package manager downloads the exact pin into its own cache: `apt-get install --download-only` or `dnf install --downloadonly`.
2. PEP identifies the **one** cached file whose own metadata names that package, version-release and native (or arch-independent) architecture, and hashes it with SHA-256.
3. The package manager installs the same pin **from its cache only**: `apt-get install --no-download` or `dnf -C install`. It re-verifies the cached file against its repository metadata and refuses changed bytes.
4. PEP ties the install to the hashed file:
   - DEB: the hash must equal the one SHA256 the authenticated APT index records for the pin.
   - RPM: the installed package's `SHA256HEADER` must equal the hashed file's.

A pin that is already installed is refused, because a no-op install could not be tied to the download. A missing, ambiguous or unreadable cached file is also refused. A local-file install is never used: `dnf`'s `localpkg_gpgcheck` defaults to off, and a local `.deb` bypasses APT's authenticated index. `install-evidence.json` records the digest as `installed_sha256`, and the atomic summary carries it as `installed_package_sha256` (null when no file was verified). The latest/L1 path records null.

`pep-certify.yml`'s reducer compares that digest with the planned package's captured `sha256` for every non-preview test run. It also requires the planned identity rungs: L1 and L2a always, and L2b only when the component policy plans an expected binary version. Every plan entry must carry its family's exact pin, equal to the planned package's version-release, with the other family's pin empty. A plan entry that does not fails the whole result closed. A completed test run whose digest is **mismatched** (`package_digest_mismatch`) or **missing** (`package_digest_missing`), or whose planned identity is unproven (`identity_unproven`), becomes `incomplete`, with its test counts and failures kept. It blocks in **both** `observe` and `gate`. Preview is exempt. A test run that was already incomplete or an infra failure keeps its own status and reason, but a digest mismatch is still recorded on it. The decision names the first reason present, in this order:
1. `package_digest_mismatch`;
2. `package_evidence_rejected` (build evidence that capture rejected as invalid; see **Package evidence** below);
3. `missing_result`;
4. an infra failure;
5. mixed preview/full test runs;
6. `package_digest_missing`;
7. `identity_unproven`;
8. any other incomplete test run.

The gate also refuses a completed result whose test runs do not all carry a matching digest and proven planned identity (`contradictory_axes`). The digest proves the installed bytes are the captured bytes. What that means depends on where the captured digest came from: a build receipt proves the repository served the built package, while a published-package replay proves the test used the bytes that were captured.

### DockerHub credentials (optional)

`DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` are optional secrets. Pass them **explicitly** (as above) — not `secrets: inherit` — to authenticate base-image pulls in `full` mode and avoid anonymous rate limits. Omit them to rely on anonymous pulls. No read token of any kind is required.

### Time limit, retries and plan size

- **Time limit.** Each test run's framework step runs under a 45-minute limit (`pipeline/pep_time_limit.py`), with the job's 60-minute `timeout-minutes` as a backstop.
  - Reaching the limit stops the framework's whole process group and records an infrastructure failure, "test run exceeded its 45-minute limit (timeout)", in both modes.
  - Only the wrapper's explicit flag counts as a timeout. Any other exit, including 137 from an out-of-memory kill, is reported as an unexpected framework failure.
  - If the 60-minute job limit itself is hit, Summarize, Upload and Report run on a best-effort basis only, and a missing summary counts as a missing result.
- **Retries.** Capture's GitHub API reads and downloads, and the aggregate's artifact listing, retry transient failures.
  - Retried: HTTP 429/500/502/503/504, network faults and timeouts, and a response body that stops part-way.
  - Each read gets 3 attempts in total, waiting 2 s and then 5 s. A `Retry-After` header is honoured up to 30 s.
  - Never retried: 404/410 (absence and expiry are handled as before), other HTTP statuses such as 401/403/422, a refused redirect, a certificate failure, a malformed or oversized response, and a local file error.
  - Every attempt starts again from the original URL, so the credential-origin rules apply to each one.
- **Plan size.** A plan with more than 256 test runs, GitHub's limit for one matrix, is unresolved. It has a named reason and an empty matrix, the plan job shows the reason as an annotation, and certification fails closed.
- **Side-file faults.** If one test run's identity or install evidence is unreadable, the run keeps its provenance. It counts as that run's infrastructure failure, and the other runs keep their results. An unreadable provenance file still fails the whole result closed.

### Current limitations (POC)

- **`certification` is the only wired scenario.** It is a no-replacement run: certification forces the effective upgrade off, resolved once as `upgrade={value:"false", source:"scenario_policy"}` and read back by the runtime, so no package replacement or channel switch happens around the identity being certified.
- **`scenario=upgrade` is rejected** (validation error → `incomplete`/`not_run`). The upgrade-replacement flow is not implemented yet.
- **Production gating is deferred.** `mode=gate` exists but has not been run on live CI; the POC runs `observe`.
- **Single target per call**, and L2 requires explicit `expected_*` strings (no automatic derivation from buildnum/tag yet).
- The live full-mode path has been validated for a **DEB** target; the RPM full-mode path is covered by unit tests but not yet run live.
- **Repository access is unauthenticated.** Access restrictions and client certificates for the package repositories are planned but not enforced. Rolling them out is a shared infrastructure change across PEP and its calling workflows; PEP has no certificate handling today.

The next step is a real cross-repo caller in a component's release pipeline (report-only), which exercises this workflow end-to-end from the publishing side.

## Release certification adapter (`pep-release-certify.yml`)

A release pipeline that builds and publishes packages calls `.github/workflows/pep-release-certify.yml` once, after its publication jobs. It passes only facts it owns. The adapter turns them into `pep-certify.yml`'s existing inputs, certifies with it, and always writes one summary. `pep-certify.yml` itself is unchanged: its generic inputs, outputs and decisions are the same for every caller.

```yaml
  certify:
    name: Certify packages (PEP)
    # After publication; certification never changes what was published.
    needs: [detect-matrix, determine-repo-type, build, package-rpm, package-deb, push-dnf, push-apt]
    if: always() && needs.determine-repo-type.result == 'success'
    permissions: {contents: read, actions: read}
    uses: pgEdge/pgedge-pep-test/.github/workflows/pep-release-certify.yml@v1_STABLE
    with:
      rpm_matrix:      ${{ needs.detect-matrix.outputs.rpm_matrix }}
      deb_matrix:      ${{ needs.detect-matrix.outputs.deb_matrix }}
      component:       rag
      version:         ${{ needs.determine-repo-type.outputs.component-version }}
      buildnum:        ${{ needs.determine-repo-type.outputs.component-buildnum }}
      effective_tag:   ${{ needs.determine-repo-type.outputs.effective-tag }}
      channel:         ${{ needs.determine-repo-type.outputs.repo-type }}
      simulated:       ${{ needs.determine-repo-type.outputs.simulated }}
      rpm_publication: ${{ needs.push-dnf.result }}
      deb_publication: ${{ needs.push-apt.result }}
    secrets:            # optional; withheld on simulated runs, and PEP forwards them only for full certification
      DOCKERHUB_USERNAME: ${{ needs.determine-repo-type.outputs.simulated == 'false' && secrets.DOCKERHUB_USERNAME || '' }}
      DOCKERHUB_TOKEN:    ${{ needs.determine-repo-type.outputs.simulated == 'false' && secrets.DOCKERHUB_TOKEN || '' }}
```

Each package job also keeps its `[pep-cell:<cell_id>]` name marker and its `pep-package-receipt` step, pinned to a full commit SHA, so evidence is recorded where each package is built. The receipt pin does not have to match the commit `v1_STABLE` points to (see [Stable channel](#stable-channel-v1_stable)).

**Package evidence.** Capture (`pep-capture.yml`) checks each cell's receipt against GitHub's record of the uploaded package and re-inspects the package itself. One shared table, `CLASS_BY_CODE` in `pipeline/pep_evidence_class.py`, puts every outcome in one class:
- **Accepted:** the package is planned for testing.
- **Unavailable:** no receipt (for example, the receipt step failed), only expired receipts, or a receipt whose package artifact expired or vanished (`PACKAGE_ARTIFACT_EXPIRED`, `PACKAGE_ARTIFACT_ABSENT`). The cell becomes a `package_evidence_missing` coverage gap, reported under `observe` and blocking under `gate`, like any other gap.
- **Invalid:** evidence that is present but malformed, unsafe, ambiguous or contradictory. This is every other capture code, including a malformed receipt, two live receipts for one cell, and checksum or identity mismatches. The cell becomes a `package_evidence_rejected` gap whose detail carries the capture code. The result is `incomplete` with that reason, which blocks in **both** modes. The other cells are still tested and their results kept, and neither a product failure nor a preview hides the rejection.

An unknown code is treated as invalid. The certification plan and the invocation planner both re-check each cell's verdict, code and class against the same table, so a classification that disagrees with it (for example a checksum mismatch labelled unavailable) or is malformed fails the plan closed rather than becoming an ordinary gap. If capture fails as a whole, no plan is produced and certification blocks in both modes; the job then uploads its sanitized `capture-error.json` (redacted message only) as the artifact `pep-capture-error-r<run_number>-a<run_attempt>`.

**Component packages.** `aspects/pipeline/pep_capture_policy.json` names each component's runtime packages, and `aspects/pipeline/pep_package_policy.py` applies it everywhere: request validation, certification-plan target selection, the invocation planner (as a backstop) and the component tests. An entry has one of two forms:
- **Flat** (`rag`): `allowed_runtime_package_names`, the same names for every family and PG major.
- **PG-coupled** (`spock`): `"pg_coupled": true` with per-family `runtime_package_templates`. `{pg}` is the PostgreSQL major the package was built for, and `{series}` comes from the release version's major through `series_by_version_major`. Release 5.0.13 built for PG17 is certified only as `pgedge-spock50_17` (RPM) or `pgedge-postgresql-17-spock50` (DEB); 6.x maps to `spock60`. The series always comes from the release version, never from a configured default.

A spock package built for the wrong family, PG major or series is rejected with that reason. It stays visible as a coverage gap, including when the same cell also has the valid package. Debug and source packages remain silent policy exclusions. `uncertified_runtime_package_templates` names runtime companions that are built but not tested, with a required `uncertified_reason`. For spock that is the RPM-only `-llvmjit` JIT bitcode package. When one is in a release's build artifacts, it is listed under "Runtime packages not certified by policy" in the plan, `cert-result.json`, the report and the job summary. The listing says only that the package was built and not installed or tested; it makes no claim about publication, so it reads the same in a preview, after a failed publication and in a full run. These packages are not coverage gaps, so the certification never claims every runtime package was tested. A malformed record in this list fails the result closed, and the report shows it as malformed rather than dropping it. PEP's execution catalog supports PG 16–18, so a PG19 build is a `pg_not_supported` coverage gap.

In integration mode, `test_pep_spock.py` installs the requested PG major's server packages from the same channel, without a minor-version pin. It then installs the requested spock package last, pinned to its exact version with its checksum recorded, so a companion install cannot replace it. A missing server package fails as a prerequisite failure. The test checks that the requested package is the only spock package installed, records the installed versions in `test-logs/prerequisite-versions.json`, proves identity like RAG does (`aspects/pipeline/pep_integration_mode.py`), and checks that both nodes run the requested PG major. Each node's spock library must belong to the requested package, and its extension version must equal the release version. Standalone runs (`--spock`, `SPOCK_MAJOR`) are unchanged.

Spock's release workflows do not call PEP yet. The planned first integration is the release workflow on Spock's `v5_STABLE` branch, including its branch-based `simulate_tag` rehearsal. Spock 6 uses the same policy but is still beta and has not been validated live. The separate `pgedge-enterprise-packages` workflow stays available and unchanged, and packages it publishes are not PEP-certified.

The adapter validates the facts before anything runs (`pipeline/pep_release_inputs.py`) and rejects, rather than guesses:
- Each detector matrix must be a JSON object whose `include` lists cell objects with a `cell_id` and the matrix's own `family`. Only a valid matrix can count as having zero cells.
- `<family>_publication` is that family's publication job result: `success`, `failure`, `cancelled` or `skipped`. It is required when the matrix has cells for the family. It may be omitted only for a family with zero cells, which is then left out of `publication_results` rather than reported as skipped or successful. An explicit `skipped` is kept.
- `simulated` must be exactly `true` or `false`. Only `true` selects preview (a dry run); `false` selects full certification.
- The identity values must be nonblank, without surrounding whitespace or control characters. `enforcement` is `observe` (default) or `gate`.

The adapter calls `pep-certify.yml` through a relative path, so it runs at the adapter's own commit: the caller's one reference selects the adapter, the certifier and everything they run, and the commit it resolves to is recorded for the run. Only the Docker Hub secrets are accepted. They are forwarded explicitly, and only for full certification. The adapter passes through all of `pep-certify.yml`'s outputs.

**Failure behavior.** Certification runs after publication and never changes it. A rejected input fails the adapter's input job, so the certify job is skipped. An infrastructure or incomplete result, a package-digest problem, or package evidence that capture rejected as invalid fails the certify job. Either way the caller's run fails, even under `observe`. Product test failures and coverage gaps, including a missing receipt, are reported under `observe` without failing it. In every case, including rejected inputs, the summary job writes the actual per-family publication results as passed in, and the certification outcome.

**Certification report.** `consolidated-report.html` re-derives the decision from `cert-result.json` with `pep_cert_gate.decide`, only to check that `cert-decision.json` belongs to it; the re-derived decision is never shown or substituted. If the two disagree, the page shows the recorded decision as "not verified" in grey, names the differing fields in a report issue (counted once in the report-issue total), and never shows a trusted PASS. Its axes table reads the result axes from `cert-result.json` and labels the policy values as recorded in `cert-decision.json`. The report never changes either JSON file or the workflow outcome.

**Finding the results.** Each test run is a job named for its platform/architecture, PG version and package, followed by what it does, for example `debian12-amd64 · PG16 · pgedge-rag-server2 / preview (no install, no product tests)` or `rocky9-arm64 · PG17 · pgedge-rag-server2 / full test (install and product tests)`. Its log ends with one `PEP test-run result:` line read from its own `summary.json`. The result is shown only when the reducer's checks accept the summary and it binds to this job's workflow run, attempt and PEP checkout; a summary that is missing, malformed, from another run or attempt, or for the other execution mode reads as UNAVAILABLE or UNVERIFIED, never as zero tests passed or as a preview. The line is one test run's result, not the package certification decision. The aggregate job writes the run's job summary: the certification state, a platform by PG table per package family built from the same evidence and decision checks as `consolidated-report.html`, failed test runs with their first failure, coverage gaps, and a download link to the `pep-certification-r<N>-a<M>` artifact. The link is written only from the upload's artifact id; otherwise the summary says the download link is unavailable. The release adapter's summary links the same artifact. A preview installs nothing and runs no product tests, and a green `observe` run with coverage gaps is not a complete certification; both summaries say so. All of this is reporting only: the JSON evidence, outputs, artifact names, test-run ids and policy are unchanged.

**Code layout.** The integration and certification steps live in `pipeline/`: the request resolver and run summaries that `run_pep_tf.sh` and `pep-integration.yml` call, and package inspection, capture, the certification and invocation plans, results, the gate, the report and the adapter inputs. The shared integration helpers live in `aspects/pipeline/`, following the framework's aspects layer: the request contract (`pep_request.py`, `pep_request_env.py`), install verification and identity evidence (`pep_verify.py`, `pep_identity.py`, `pep_evidence.py`, `pep_version_normalize.py`), the integration-mode install and identity steps the component tests share (`pep_integration_mode.py`), and the one component policy (`pep_capture_policy.json`) with its matching rules (`pep_package_policy.py`). `container_resolver.py` and `ci_consolidated_report.py` stay in `utillities/` because the standalone regression also uses them. Dependencies run one way: `pipeline/` uses `aspects/pipeline/` and `utillities/`, `aspects/pipeline/` uses `utillities/`, and nothing in `aspects/` or `utillities/` imports from `pipeline/`. Unit tests sit beside the code they test, in `aspects/pipeline/tests/` and `pipeline/tests/`; tests whose subject is the shell runner or a workflow (`test_pep_bridge`, `test_pep_integration_auth_contract`, `test_pep_selftest_artifacts`) sit in `pipeline/tests/` with the other workflow-contract tests. The workflows and the receipt action stay under `.github/`, where callers pin them.

## Certification preview check (`pep-certification-preview.yml`)

`PEP Certification Preview` runs the whole certification path on every pull request, in its own Actions run (it has no path filter, and it is separate from `PEP Self-Test`). Manual dispatch works once the workflow is on `main`.

- **Fixture.** Two `build [pep-cell:<cell_id>]` jobs build one tiny RPM (`el-9`, amd64) and one tiny DEB (`bookworm`, amd64) of the internal fixture component `pgedge-capture-smoke`, upload each package and record its `pep-receipt/2` with the local receipt action, as a release pipeline does. The cells are test fixtures; they define no supported platform, PG version or component.
- **Coordinator.** The `certify` job calls `pep-certify.yml` by relative path, so the coordinator and everything it checks out run at the revision under test. It is a simulated release: `simulated: true`, both publication results `skipped`, `execution_mode: preview` and `enforcement: observe`. Nothing is published, and no secrets are passed.
- **Assertion.** The `assert certification preview` job runs even when the coordinator fails and checks the run with `pipeline/pep_preview_check.py`. It reads the coordinator's outputs first, then downloads the combined evidence by the id the coordinator reported, and this attempt's capture evidence and cert-plan by the ids listed for their exact attempt-specific names. It passes only when:
  - the coordinator succeeded with a `preview` / `report` / `success` decision and a positive evidence artifact id;
  - both receipts were accepted, and the cert-plan records a simulated release with both publications skipped: each target is strictly ineligible (never claimed as published) and preview-eligible;
  - `cert-result.json` holds exactly six unique preview runs, both fixture targets on PG 16, 17 and 18, each bound to the captured package, with no planning gap, no missing, unexpected or historical result, no product test, and no installed-package or identity proof claimed;
  - every document names this repository, run and attempt and the revision under test (`github.workflow_sha`);
  - `cert-decision.json` equals the decision `pep_cert_gate` derives from `cert-result.json` and matches the coordinator's outputs; the collection ledger ingested exactly the run's six `pep-summary-*` artifacts with no anomaly; the run has no capture error artifact; and `consolidated-report.html` shows the verified preview decision, one preview row per run and no gap, issue or fallback section;
  - no persisted document contains a URL or credential text.

The six-run expectation is the check's own: PEP's catalogs decide what the coordinator plans, so a catalog change that alters the fixture's platforms or PG majors fails this check until the expectation is updated in the same change. The checker's tests rebuild the result, ledger, decision and report from the inputs in `pipeline/tests/preview_check_fixtures/` with the real collector, gate and report code.

**Retrying.** Each run uses 14 jobs: the two builds, the coordinator's identity, capture, plan, six preview runs, aggregate and enforce, and the assertion. Do not re-run its jobs, including **Re-run all jobs**: the receipt artifact names are fixed per cell, and summaries kept from an earlier attempt can invalidate the re-run. To check again, start a fresh workflow run (push a new commit, or close and reopen the pull request; once the workflow is on `main`, dispatch it).

This is not a required check yet. The `v1_STABLE` rules still require only the nine Self-Test checks (see **Protection** below); the first promotion that includes this workflow must also confirm that its run passed (promotion step 3).

## Stable channel (`v1_STABLE`)

Product release pipelines call the release adapter through the `v1_STABLE` branch, so they do not have to change their workflows whenever PEP changes:

```yaml
    uses: pgEdge/pgedge-pep-test/.github/workflows/pep-release-certify.yml@v1_STABLE
```

**`main` and `v1_STABLE`.** Reviewed PEP changes land on `main`, which moves often. `v1_STABLE` moves only when a PEP maintainer promotes one specific, tested `main` revision to it, or rolls it back. Products receive fixes, tests, report changes, platforms and components through promotions, without editing their workflows. A full commit SHA still works for a caller that needs a fixed revision.

**What stays pinned.** Each package job's `pep-package-receipt` step stays pinned to a full commit SHA, because it runs inside the build job. Its output, `pep-receipt/2`, is what PEP keeps accepting, so the receipt pin does not need to change when `v1_STABLE` moves. Direct callers of `pep-integration.yml` keep using one full SHA for both `uses:` and `pep_implementation_ref`.

**Which PEP ran.** In each run, `pep-certify.yml` reads the commit it is running from (`job.workflow_sha`) once, and checks out and passes exactly that commit to every nested workflow and checkout; the adapter's jobs read theirs the same way. That commit is recorded as the `PEP revision` row of the adapter summary, `pep_implementation_ref` and `pep_resolved_sha` in the capture evidence, `pep_requested_ref` and `pep_resolved_sha` in each test run's `summary.json` and `provenance.json`, and `provenance.pep_resolved_sha` in `cert-result.json`. Moving `v1_STABLE` later never changes what an earlier run recorded.

**Compatibility.** A promotion must keep existing callers working. Backward-compatible additions need no new channel: a new optional input or output with a default that keeps today's behaviour, new report content, tests, fixes, platforms and components. New platforms or components can change what a release is tested on, so the promotion PR lists them. Removing or renaming an input, output or secret, making an optional input required, changing what an existing input or output means, needing more caller permissions, changing the `[pep-cell:<cell_id>]` marker or package-artifact naming, or no longer accepting `pep-receipt/2` from pinned receipt steps needs a migration path, a compatibility layer or a new channel (`v2_STABLE`). The Self-Test tests that list the adapter's and certifier's declared inputs, outputs and secrets fail on any interface change, so it is reviewed on purpose.

**Promotion and rollback commands.** Paste these helper functions into a bash or zsh shell once. Each one stops at the first failed step: it prints `STOP` and returns non-zero before creating or pushing anything, and it only prints `OK` when every check passed. They use `return`, never `exit`, so a failure does not close your shell. They read and set the variables `X`, `RECORDED`, `H`, `G` and `R`. In a new shell, paste them again and set those variables from the PR description.

```sh
# True only when $1 is a full SHA naming an existing object of kind $2 (commit or tree).
pep_full_sha() {
  [ -n "${1}" ] && [ "$(git rev-parse --verify --quiet "${1}^{${2}}")" = "${1}" ]
}

# Needs X and RECORDED. Sets H to the promotion head and pushes it to promote/v1/<first 12 of X>.
pep_promote_head() {
  H=
  git fetch origin main v1_STABLE || { echo "STOP: fetch failed"; return 1; }
  pep_full_sha "${X}" commit || { echo "STOP: X is not a full commit SHA"; return 1; }
  git merge-base --is-ancestor "${X}" origin/main || { echo "STOP: X is not on main"; return 1; }
  pep_full_sha "${RECORDED}" tree || { echo "STOP: RECORDED is not a full tree SHA"; return 1; }
  [ "$(git rev-parse --verify "origin/v1_STABLE^{tree}")" = "${RECORDED}" ] ||
    { echo "STOP: v1_STABLE changed since the last record"; return 1; }
  H=$(git commit-tree "${X}^{tree}" -p origin/v1_STABLE -p "${X}" -m "Promote main ${X} to v1_STABLE") ||
    { echo "STOP: commit-tree failed"; return 1; }
  git push origin "${H}:refs/heads/promote/v1/${X:0:12}" || { echo "STOP: push failed"; return 1; }
  echo "OK: X=${X} H=${H} content=$(git rev-parse "${X}^{tree}")"
}

# Needs G. Sets R to the rollback head and pushes it to rollback/v1/<first 12 of G>.
pep_rollback_head() {
  R=
  git fetch origin v1_STABLE || { echo "STOP: fetch failed"; return 1; }
  pep_full_sha "${G}" commit || { echo "STOP: G is not a full commit SHA"; return 1; }
  git merge-base --is-ancestor "${G}" origin/v1_STABLE || { echo "STOP: G is not an earlier v1_STABLE state"; return 1; }
  R=$(git commit-tree "${G}^{tree}" -p origin/v1_STABLE -m "Roll back v1_STABLE to ${G}") ||
    { echo "STOP: commit-tree failed"; return 1; }
  git push origin "${R}:refs/heads/rollback/v1/${G:0:12}" || { echo "STOP: push failed"; return 1; }
  echo "OK: G=${G} R=${R} content=$(git rev-parse "${G}^{tree}")"
}

# pep_check_test_merge <PR number> <target SHA>: GitHub's test merge must have the target's content.
pep_check_test_merge() {
  pep_full_sha "${2}" commit || { echo "STOP: the target is not a full commit SHA"; return 1; }
  git fetch origin "refs/pull/${1}/merge" || { echo "STOP: fetch of PR ${1}'s test merge failed"; return 1; }
  [ "$(git rev-parse --verify "FETCH_HEAD^{tree}")" = "$(git rev-parse "${2}^{tree}")" ] ||
    { echo "STOP: the test merge does not have ${2}'s content"; return 1; }
  echo "OK: the test merge has ${2}'s content"
}

# pep_check_merged <target SHA> <PR head SHA>: after the merge, v1_STABLE has the target's content and
# the PR head as its second parent (and a promotion head has the target as its own second parent).
pep_check_merged() {
  pep_full_sha "${1}" commit && pep_full_sha "${2}" commit ||
    { echo "STOP: the target and PR head must be full commit SHAs"; return 1; }
  git fetch origin v1_STABLE || { echo "STOP: fetch failed"; return 1; }
  [ "$(git rev-parse --verify "origin/v1_STABLE^{tree}")" = "$(git rev-parse "${1}^{tree}")" ] ||
    { echo "STOP: v1_STABLE does not have ${1}'s content"; return 1; }
  [ "$(git rev-parse --verify --quiet "origin/v1_STABLE^2")" = "${2}" ] ||
    { echo "STOP: v1_STABLE's second parent is not the PR head"; return 1; }
  if git rev-parse --verify --quiet "${2}^2" >/dev/null; then
    [ "$(git rev-parse "${2}^2")" = "${1}" ] || { echo "STOP: the PR head's second parent is not ${1}"; return 1; }
  fi
  echo "OK: record v1_STABLE=$(git rev-parse origin/v1_STABLE) content=$(git rev-parse "origin/v1_STABLE^{tree}")"
}
```

**Promotion.** One PR per promotion, for one `main` revision. Replace each quoted placeholder with a full value.

1. Choose the `main` revision `X` and look up the content recorded by the last promotion or rollback of `v1_STABLE` (after the first, normal advance of `v1_STABLE`, that is its revision's content). Build and push the promotion head; it has exactly `X`'s content on top of the current `v1_STABLE`. Any `STOP`, such as an unexplained change on `v1_STABLE`, must be resolved before going further.
   ```sh
   X='<full SHA of the selected main revision>'
   RECORDED='<content recorded by the last promotion or rollback>'
   pep_promote_head
   ```
2. Open a PR from `promote/v1/<first 12 characters of X>` into `v1_STABLE`. Its description records `X`, `H`, the expected content, the changes (`git log --first-parent --oneline <previous X>..<X>`), the evidence, and whether the change is compatible. The PR diff is exactly `v1_STABLE` to `X`, so everything the promotion changes, including content a rollback removed, is shown for review.
3. The nine PEP Self-Test checks must pass, and a code owner other than the person who pushed the branch approves. On the first promotion that includes `pep-certification-preview.yml`, the PR's `PEP Certification Preview` run must also pass, including its `assert certification preview` job; record that run in the PR description. It is not a required check yet, so the rules do not enforce this.
4. Before merging, confirm that GitHub's test merge has `X`'s content. If `v1_STABLE` moved after step 1, close the PR and start again; do not use **Update branch**.
   ```sh
   N='<PR number>'
   pep_check_test_merge "${N}" "${X}"
   ```
5. Merge with **Create a merge commit**. Then check the result; only if it passes, delete the promote branch and record the printed `v1_STABLE` commit and content in the PR.
   ```sh
   pep_check_merged "${X}" "${H}" && git push origin --delete "promote/v1/${X:0:12}"
   ```

**Rollback.** To undo a promotion, return `v1_STABLE` to an earlier state `G` (normally the first parent of the promotion's merge commit) with a corrective commit. Nothing is force-pushed.

```sh
G='<full SHA of the v1_STABLE state to return to>'
pep_rollback_head
```

Open a PR from `rollback/v1/<first 12 characters of G>` into `v1_STABLE` that records `G`, `R`, the expected content and the reason. It needs the same checks and approval. Before merging, check the test merge; after merging with a merge commit, check the result and only then delete the branch:

```sh
N='<PR number>'
pep_check_test_merge "${N}" "${G}"
```

```sh
pep_check_merged "${G}" "${R}" && git push origin --delete "rollback/v1/${G:0:12}"
```

Callers change nothing: their next runs use the rolled-back content, and the recorded SHA shows it. The fix then lands on `main` and is promoted with the steps above; that PR's diff shows the re-applied content.

**Protection.** On `v*_STABLE` branches, an organization rule blocks force pushes and deletion. A repository rule requires a PR with one code-owner approval (not from the last pusher), merge commits only, an up-to-date branch, and these nine Self-Test checks from GitHub Actions: `unit`, `preview-call / preview (no install, no product tests)`, `validation-call / preview (no install, no product tests)`, `bad-execmode-call / rejected (invalid execution mode)`, `bad-mode-call / preview (no install, no product tests)`, `assert-preview`, `assert-validation`, `assert-bad-execmode` and `assert-bad-mode`. The check names come from Self-Test job names, so renaming one of those jobs needs the rule updated in the same change. A tag rule blocks `v*_STABLE` tags, because GitHub resolves a tag before a branch of the same name. Code owners are listed in `.github/CODEOWNERS`.
