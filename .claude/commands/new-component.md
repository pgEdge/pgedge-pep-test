---
name: new-component
description: Interactively scaffold a complete new pgEdge component. Asks guided questions about the component, updates all config files, creates expected-output placeholders, generates the test file, and wires everything into run_pep_tf.sh, conftest.py, README.md, and optionally test_pep_repo_health.py.
argument-hint: "<component-name>  (e.g. patroni, pgaudit, myext)"
---

You are scaffolding a new pgEdge component named **$ARGUMENTS**.

Work through the phases below in order. Ask the user each question, wait for the answer, then proceed. Never batch all questions together — ask one group at a time so the conversation is easy to follow.

---

## Phase 1 — Gather component information

Ask the following questions as a numbered list in a single message:

> I need a few details about the **$ARGUMENTS** component before I start. Please answer each question:
>
> 1. Is this component **coupled** (tied to a PostgreSQL major version, e.g. `pgedge-lolor_16`) or **decoupled** (standalone, e.g. `pgedge-patroni`)?
> 2. What is the **RHEL/RPM package name**? (e.g. `pgedge-myext` or `pgedge-myext_16`). For multiple packages list them all.
> 3. Is the **DEB package name the same** as the RPM name, or different? If different, provide the DEB package name(s).
> 4. What is the **current package version**? (e.g. `1.0.0`, `4.1.0-beta2`)
> 5. Does this component install a **standalone binary** (e.g. `/usr/bin/pgedge-myext`)? If yes, give the full path(s) per package. If no, type `none`.
> 6. Does this component install **PostgreSQL extensions**? If yes, list the extension names separated by commas (e.g. `myext,myext_extra`). If no, type `none`.
> 7. Do the packages install a **LICENSE file**? Expected path: `/usr/share/licenses/<package>/LICENSE`. Answer `yes` or `no`.
> 8. Do the packages install a **README file**? Expected path: `/usr/share/doc/<package>/README.md`. Answer `yes` or `no`.
> 9. Would you like to **provide the expected file list** for each package now? Options: **a)** paste the output of `rpm -ql <pkg>` or `dpkg -L <pkg>` for each package now, **b)** create TODO placeholders, **c)** skip bundled-file verification.
> 10. Are there **package-level dependencies** between these packages or on other pgEdge packages? If yes, list them as `package → dependency` (one per line). If no, type `none`.
> 11. Should this component also be added to **`test_pep_repo_health.py`**? Answer `yes` or `no`. `repo_health` installs every enabled component together on one host to catch conflicts between them. Answering `yes` adds the package to `packages_test_matrix.json`, the repo_health version maps, and — where applicable — the preload-library and `ALL_EXTENSIONS` lists.

Wait for the user's answers, then store:
- `component` = `$ARGUMENTS` (snake_case)
- `COMPONENT` = uppercase of `$ARGUMENTS`
- `title` = title-cased display name (e.g. `My Ext`)
- `coupled` = true/false
- `rhel_pkg` = RHEL package name(s) — may be a list for multi-package components
- `deb_pkg` = DEB package name(s) (may equal `rhel_pkg`)
- `version` = package version
- `binary_path` = full binary path(s) per package or empty
- `extensions` = comma-separated list or empty
- `has_license` = true/false (from question 7)
- `has_readme` = true/false (from question 8)
- `bundled_files_option` = a/b/c (from question 9)
- `dependencies` = map of package → [dependencies] or empty (from question 10)
- `add_to_repo_health` = true/false (from question 11)

Derive naming variables:
- `short_name` = `rhel_pkg` with `pgedge-` prefix stripped, and any `_16`/`_17`/`_18` suffix stripped
  e.g. `pgedge-lolor_16` → `lolor`, `pgedge-patroni` → `patroni`
- `SHORT` = uppercase of `short_name`

---

## Phase 2 — Update configuration files

For each of the three files — `configuration/config16.env`, `configuration/config17.env`, `configuration/config18.env` — append a new section at the end of the file.

The section must follow this template (include only the lines that apply based on the user's answers):

```
# ============================================================
# Configuration parameters for <title>
# ============================================================

# Package name(s)
export <SHORT>_PACKAGE=<rhel_pkg>
```

If `deb_pkg` differs from `rhel_pkg`, also add:
```
export DEB_<SHORT>_PACKAGE=<deb_pkg>
```

If the component has a binary:
```
# Binary path
export <COMPONENT>_BINARY=<binary_path>
export COMPONENT_BINARY=<binary_path>
export COMPONENT_BINARY_VERSION=<version>
```

If the component has extensions:
```
# PostgreSQL extensions
export <SHORT>_EXTENSIONS=<extension1>,<extension2>
```

Always add:
```
# Package version
export PGEDGE_<SHORT>_VERSION=<version>
```

For `config17.env` and `config18.env`, use identical content to `config16.env` in this section (versions are the same across PG majors for decoupled components; adjust only if the user says they differ).

After writing all three files, print a summary of the lines added.

---

## Phase 3 — Create expected-output placeholder files

Use the `bundled_files_option` stored from question 9 in Phase 1. If the user answered **a)**, **b)**, or **c)** there, skip re-asking and proceed directly.

**If option a) (placeholders):**
Create these files with a single comment line for each package:
- `expected-output/rpm/<pkg>` containing: `# TODO: paste output of: rpm -ql <pkg>`
- `expected-output/deb/<pkg>` containing: `# TODO: paste output of: dpkg -L <pkg>`

**If option b) (user provides file lists):**
The user may have already pasted the RPM file list in Phase 1 (question 9). If not, ask them to paste it now for each package. Create `expected-output/rpm/<pkg>` with the pasted content (one path per line).
For DEB: if the user provided DEB paths too, create `expected-output/deb/<pkg>`. Otherwise create DEB files with TODO placeholders prefixed by a comment noting to verify paths after DEB install.

If `rhel_pkg == deb_pkg`, use the same filename under both `rpm/` and `deb/`.

**If option c):**
Skip this phase entirely.

---

## Phase 4 — Scaffold the test file

Read `component-test/test_pep_component_template.py` in full.

Create `component-test/test_pep_<component>.py` by applying these substitutions throughout the entire file:

| Find | Replace with |
|------|-------------|
| `lolor` | `<component>` |
| `LOLOR` | `<COMPONENT>` |
| `lolor_version` | `<component>_version` |
| `lolor_package` | `<component>_package` |
| `rhel_lolor_package` | `rhel_<component>_package` |
| `deb_lolor_package` | `deb_<component>_package` |
| `PGEDGE_LOLOR_{pg_major_version}_VERSION` | `PGEDGE_<SHORT>_VERSION` |
| `LOLOR_PACKAGE` | `<SHORT>_PACKAGE` |
| `DEB_LOLOR_PACKAGE` | `DEB_<SHORT>_PACKAGE` |
| `pgedge-lolor_{pg_major_version}` | `<rhel_pkg>` |
| `pgedge-postgresql-{pg_major_version}-lolor` | `<deb_pkg>` |
| `LOLOR_BUNDLED_FILES` | `<SHORT>_BUNDLED_FILES` |
| `DEB_LOLOR_BUNDLED_FILES` | `DEB_<SHORT>_BUNDLED_FILES` |
| default bundled-files values | `""` (empty — paths unknown) |

Additionally:
- Update `component_binary = os.getenv("COMPONENT_BINARY", "")` default to `<binary_path>` if provided, otherwise leave empty
- Update `component_version = os.getenv("COMPONENT_BINARY_VERSION", "")` default to `<version>` if binary exists, otherwise leave empty
- If the component has no extensions, remove both `test_create_extensions` and `test_extension_version` from the file entirely
- If the component has extensions, keep `test_extension_version` immediately after `test_create_extensions`; it connects to psql, runs `\dx`, greps for the extension name, and asserts the version column matches `<component>_version`
- If `has_license` is true (from question 7), add `test_verify_license_file` that checks `/usr/share/licenses/<package>/LICENSE` exists on the container
- If `has_readme` is true (from question 8), add `test_verify_readme_file` that checks `/usr/share/doc/<package>/README.md` exists on the container
- If `dependencies` is non-empty (from question 10), add `test_verify_package_dependencies` that runs `rpm -qR <pkg> | grep <dep>` (RHEL) or `dpkg -s <pkg> | grep Depends | grep <dep>` (DEB) for each declared dependency
- For multi-package components, follow the MCP test pattern: use `all_container_package_combinations` parametrization and per-package version/binary/service maps instead of single-package variables
- Update all docstrings that mention "lolor" or "LOLOR" to use the new component name

---

## Phase 5 — Wire into run_pep_tf.sh

Read `run_pep_tf.sh` and make these changes:

1. Add `<component>` to the `--components` help text line
2. Add menu entry (use the next available number; increment the `all)` line):
   ```
   echo "N) <component> - <title> tests"
   ```
3. Append `<component>` to `test_type_list=(...)`
4. Add RPM case in `RPM|rpm)` block:
   ```
   <component>)
     run_pytest_with_tracking "component-test/test_pep_<component>.py" "$env" "rpm" "<component>"
     ;;
   ```
5. Add DEB case in `DEB|deb)` block (same pattern)
6. Append `<component>` to the master index `for test_type in ...` loop — this is the loop near the bottom of the file (search for `for test_type in server snowflake`) that generates component cards in `test-logs/index.html`. Adding `<component>` here ensures it appears as a card in the HTML dashboard report once test results exist.

---

## Phase 6 — Wire into conftest.py

Read `component-test/conftest.py`. Add to `component_map`:

```python
'test_pep_<component>': {
    'name': '<title>',
    'version_env': 'PGEDGE_<SHORT>_VERSION',
    'version_default': '<version>',
    'rhel_package_env': '<SHORT>_PACKAGE',
    'rhel_package_default': '<rhel_pkg>',
    'deb_package_env': 'DEB_<SHORT>_PACKAGE',
    'deb_package_default': '<deb_pkg>'
},
```

---

## Phase 7 — Wire into test_pep_repo_health.py (only if `add_to_repo_health` is true)

If the user answered **no** to question 11, skip this phase entirely — do not touch
`packages_test_matrix.json`, `test_pep_repo_health.py`, or any `ALL_EXTENSIONS` line.
State in the final summary that repo_health wiring was skipped by request.

If they answered **yes**: `repo_health` installs every matrix-enabled package together
on a single host to surface conflicts between components. It is driven entirely by
`packages_test_matrix.json`, so support requires four separate edits.

**1. Add the package(s) to `configuration/packages_test_matrix.json`**

One entry per package. Use `{PG}` as the PostgreSQL-major placeholder for coupled
packages; decoupled packages use a literal name. Match the file's existing column
alignment, and set `"enabled": true` — an entry with `enabled: false` is invisible to
repo_health, so the component would silently never be tested.

```json
{"category": "extension", "name": "<short_name>", "description": "<one line>", "enabled": true, "rhel": "<rhel_pkg with {PG}>", "deb": "<deb_pkg with {PG}>"},
```

Note that enabling an entry also changes what `configuration/generate_env.py` writes
into `ALL_PACKAGES` / `DEB_ALL_PACKAGES`. Mention this to the user rather than
running the generator unprompted.

**2. Add the package(s) to both version maps in `component-test/test_pep_repo_health.py`**

A package is version-verified only when it appears in *both* the install list and the
platform's version map, so add it to `RHEL_PACKAGE_VERSION_MAP` **and**
`DEB_PACKAGE_VERSION_MAP`:

```python
# RHEL_PACKAGE_VERSION_MAP
f"pgedge-<short_name>_{pg_major_version}":            os.getenv(f"PGEDGE_<SHORT>_{pg_major_version}_VERSION"),
# DEB_PACKAGE_VERSION_MAP
f"pgedge-postgresql-{pg_major_version}-<short_name>": os.getenv(f"PGEDGE_<SHORT>_{pg_major_version}_VERSION"),
```

A single env var covers both platforms even when the RPM version reads `1.0-beta1` and
the DEB version reads `1.0~beta1`: `package_management.normalize_version()` folds the
Debian tilde into the hyphen form. Do **not** introduce a separate `*_DEB_VERSION`
variable for this.

Watch for key collisions: a coupled `pgedge-<name>_{PG}` and a decoupled
`pgedge-<name>` are different packages with different versions and must stay distinct
keys in the same dict.

**3. Add to `CONDITIONAL_PRELOAD_LIBRARIES` — only if the component ships a library
that must appear in `shared_preload_libraries`**

```python
"<library_name>": {
    "rhel": f"pgedge-<short_name>_{pg_major_version}",
    "deb":  f"pgedge-postgresql-{pg_major_version}-<short_name>",
},
```

Never append the library directly to the `shared_preload_libraries` string.
PostgreSQL refuses to start when a preloaded library is missing from disk, so
`get_preload_libraries()` gates every entry on the package actually being in the
matrix-derived install list — otherwise toggling the component off in the matrix
breaks the whole run at `test_start_server`.

If you are unsure whether the component needs preloading, say so explicitly rather
than guessing silently; `test_start_server` dumps the PostgreSQL log on failure, which
makes a wrong guess easy to spot on the first run.

**4. Append each extension to `ALL_EXTENSIONS` — only if the component installs
extensions**

Add the extension name(s) to the `export ALL_EXTENSIONS=` line in **every** config env
file, so `test_create_extensions` covers them.

**Verify, don't assume.** Before reporting done, confirm:
- the package appears in both platforms' matrix-derived install lists and resolves to a
  non-empty expected version
- `get_preload_libraries()` includes the library, and **drops it** when the matrix entry
  is disabled (test this by flipping `enabled` in a scratch copy of the matrix, then
  restoring it)
- `packages_test_matrix.json` is still valid JSON with its column formatting intact
- `test_pep_repo_health.py` still collects, and the new component appears in the
  collected test ids

---

## Phase 8 — Wire into README.md

Read `README.md` and:

1. Add `<component>` to the `--components` option values table cell
2. Add `│   ├── test_pep_<component>.py` to the project structure tree (alphabetical order)
3. Add row to Supported Components table:
   `| \`<component>\` | <one-line description matching title> |`

---

## Phase 9 — Final summary

Print a complete summary table:

| File | Action |
|------|--------|
| `component-test/test_pep_<component>.py` | Created |
| `configuration/config16.env` | Appended `<component>` section |
| `configuration/config17.env` | Appended `<component>` section |
| `configuration/config18.env` | Appended `<component>` section |
| `expected-output/rpm/<rhel_pkg>` | Created (placeholder / filled) |
| `expected-output/deb/<deb_pkg>` | Created (placeholder / filled) |
| `run_pep_tf.sh` | Added menu entry, RPM/DEB cases, `test_type_list`, and report loop |
| `component-test/conftest.py` | Added `component_map` entry |
| `configuration/packages_test_matrix.json` | Added matrix entry (only if `add_to_repo_health`) |
| `component-test/test_pep_repo_health.py` | Added version-map entries + preload library (only if `add_to_repo_health`) |
| `README.md` | Updated `--components` table, project tree, and Supported Components table |
| `test-logs/index.html` | Will auto-include `<component>` card on next test run (driven by `run_pep_tf.sh` report loop) |

When `add_to_repo_health` is false, list the repo_health rows as **Skipped (not
requested)** rather than omitting them, so it is obvious the step was a choice and not
an oversight.

Then remind the user:
- Run `./run_pep_tf.sh --components <component> --repo staging` to test the new scaffold
- After the run, `test-logs/index.html` will automatically show a `<title>` card with pass/fail stats
- Fill in expected-output files if placeholders were used (install the package and run `rpm -ql <pkg>` or `dpkg -L <pkg>`)
- Commit: `git add -A && git commit -m "Add <component> component test scaffold"`