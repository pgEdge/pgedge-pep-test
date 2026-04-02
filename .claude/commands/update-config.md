---
name: update-config
description: Update a component's version in all configuration env files (config16.env, config17.env, config18.env) and in conftest.py. Use when a new package version is released and the config files need to be bumped.
argument-hint: "<component> <new-version>  (e.g. patroni 4.2.0  or  pgbouncer 1.26.0)"
---

Update the version for component **$ARGUMENTS[0]** to **$ARGUMENTS[1]** across all config and conftest files.

## Step 1 — Parse arguments

- `component` = `$ARGUMENTS[0]`  (e.g. `patroni`, `pgbouncer`, `pgadmin4`, `lolor`)
- `new_version` = `$ARGUMENTS[1]`  (e.g. `4.2.0`, `1.26.0`, `9.14`)

If either argument is missing, stop and ask the user:
> Usage: `/update-config <component> <new-version>`
> Example: `/update-config patroni 4.2.0`

## Step 2 — Discover the current version line(s)

Search all three config files and conftest.py for lines that reference the component name (case-insensitive) and look like a version assignment:

Files to search:
- `configuration/config16.env`
- `configuration/config17.env`
- `configuration/config18.env`
- `component-test/conftest.py`

Pattern to find: lines matching `PGEDGE_<COMPONENT>*_VERSION` or `version_default.*<component>` (case-insensitive).

Print every matching line with its file name and line number so the user can confirm before making changes.

## Step 3 — Confirm with user

Show a diff preview:
```
Files to update:
  configuration/config16.env   PGEDGE_PATRONI_VERSION=4.1.0  →  4.2.0
  configuration/config17.env   PGEDGE_PATRONI_VERSION=4.1.0  →  4.2.0
  configuration/config18.env   PGEDGE_PATRONI_VERSION=4.1.0  →  4.2.0
  component-test/conftest.py   'version_default': '4.1.0'    →  4.2.0
```

Ask: "Proceed with these updates? (yes/no)"

Wait for the user to confirm before making any edits.

## Step 4 — Apply changes

For each matched line in each file, replace only the version string (the part after `=` or in the `version_default` value) with `new_version`.

Use the Edit tool for each individual substitution — do not bulk-replace the entire file.

Rules:
- Match the exact current version string (e.g. `4.1.0`) — do not replace partial version strings
- If a component has multiple version env vars (e.g. `PGEDGE_PATRONI_VERSION`, `PGEDGE_PATRONI_AWS_VERSION`, `PGEDGE_PATRONI_ETCD_VERSION`), update **all** of them unless the user specifies otherwise
- Preserve surrounding whitespace, quotes, and line endings exactly

## Step 5 — Verify

After edits, re-read each changed file and confirm the new version appears and the old version is gone from those lines.

Run:
```bash
grep -n "<component>" configuration/config16.env configuration/config17.env configuration/config18.env component-test/conftest.py
```

Print the result so the user can visually confirm.

## Step 6 — Summary

Print a summary table:

| File | Lines changed | Old version | New version |
|------|--------------|-------------|-------------|
| configuration/config16.env | N | 4.1.0 | 4.2.0 |
| ... | | | |

Remind the user to:
- Run the relevant test suite to verify the new version installs and passes: `./run_pep_tf.sh --components <component> --repo staging`
- Commit the config changes: `/commit` or `git commit -m "Bump <component> version to <new_version>"`