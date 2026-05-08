---
name: select-run-mode
description: Configure CONTAINERS and DEB_CONTAINERS in all config env files for single platform, repo verification (unique platforms), or all-platforms run. Prompts user for the desired mode and, for single-platform mode, the specific container names.
argument-hint: "[single|unique|all]  (optional — prompted if omitted)"
---

Configure `export CONTAINERS` and `export DEB_CONTAINERS` in all three config env files by commenting and uncommenting the correct lines based on the chosen run mode.

---

## Step 1 — Determine run mode

Check `$ARGUMENTS[0]` (case-insensitive):

| Argument value | Mode |
|---|---|
| `single`, `1`, `s` | Single platform |
| `unique`, `repo`, `verify`, `2`, `u` | Repo verification (unique platforms) |
| `all`, `3`, `a` | All platforms |
| anything else / omitted | Prompt the user |

If the argument is missing or unrecognized, ask the user:

> Which run mode would you like to configure?
>
> **1. Single platform** — one RHEL container + one DEB container (fast smoke-test)
> **2. Repo verification** — all unique RHEL platforms + all DEB platforms (no rocky9-arm duplicate)
> **3. All platforms** — every RHEL container + every DEB container (full matrix)
>
> Enter 1, 2, or 3:

Wait for the user to respond before continuing.

---

## Step 2 — For single-platform mode: ask which containers to use

If the mode is **single**, ask:

> Which RHEL container should be active?
> Available: `auto-rocky9-arm`, `auto-rocky10-arm`, `auto-alma9-arm`, `auto-alma10-arm`, `auto-oel9-arm`, `auto-oel10-arm`, `my-rocky9-amd`, `auto-alma9-amd`, `auto-oel9-amd`
> (default: `auto-rocky9-arm` — press Enter to accept)

Then ask:

> Which DEB container should be active?
> Available: `auto-debian11-amd`, `auto-debian12-arm`, `auto-debian13-arm`, `auto-debian13-amd`, `auto-ubuntu2204-arm`, `auto-ubuntu2404-arm`
> (default: `auto-ubuntu2204-arm` — press Enter to accept)

Wait for the user to respond. Use the defaults if the user presses Enter without typing.

Store the chosen names as `rhel_container` and `deb_container`.

---

## Step 3 — Read the current state of all config files

Read all three files in full:
- `configuration/config16.env`
- `configuration/config17.env`
- `configuration/config18.env`

Identify the exact current text of the three CONTAINERS lines and two DEB_CONTAINERS lines in each file. These are the only lines that change; everything else stays untouched.

The three CONTAINERS lines in each file (by their content pattern):
- **Line U** (unique RHEL): `export CONTAINERS=auto-rocky10-arm,...` (no rocky9-arm at start)
- **Line S** (single RHEL): `export CONTAINERS=auto-rocky9-arm` (only rocky9-arm)
- **Line A** (all RHEL): `export CONTAINERS=auto-rocky9-arm,auto-rocky10-arm,...` (rocky9-arm first, then others)

The two DEB_CONTAINERS lines in each file:
- **Line DA** (all DEB): `export DEB_CONTAINERS=auto-debian11-amd,...` or similar multi-container line
- **Line DS** (single DEB): `export DEB_CONTAINERS=auto-ubuntu2204-arm` (single container)

---

## Step 4 — Determine which lines to comment and uncomment

Apply the rules for the chosen mode:

### Mode 1 — Single platform

| Line | Action |
|------|--------|
| Line U (unique RHEL) | comment out (add `#` prefix) |
| Line S (single RHEL) | uncomment (remove `#`) AND set value to `rhel_container` |
| Line A (all RHEL) | comment out |
| Line DA (all DEB) | comment out |
| Line DS (single DEB) | uncomment AND set value to `deb_container` |

### Mode 2 — Repo verification (unique platforms)

| Line | Action |
|------|--------|
| Line U (unique RHEL) | uncomment |
| Line S (single RHEL) | comment out |
| Line A (all RHEL) | comment out |
| Line DA (all DEB) | uncomment |
| Line DS (single DEB) | comment out |

### Mode 3 — All platforms

| Line | Action |
|------|--------|
| Line U (unique RHEL) | comment out |
| Line S (single RHEL) | comment out |
| Line A (all RHEL) | uncomment |
| Line DA (all DEB) | uncomment |
| Line DS (single DEB) | comment out |

---

## Step 5 — Show a preview and confirm

Before making any edits, print a preview like:

```
Run mode: Repo verification (unique platforms)

Changes to apply in configuration/config16.env, config17.env, config18.env:

  CONTAINERS lines:
    comment:   export CONTAINERS=auto-rocky9-arm
    comment:   export CONTAINERS=auto-rocky9-arm,auto-rocky10-arm,...
    uncomment: #export CONTAINERS=auto-rocky10-arm,auto-alma9-arm,...

  DEB_CONTAINERS lines:
    uncomment: #export DEB_CONTAINERS=auto-debian11-amd,auto-debian12-arm,...
    comment:   export DEB_CONTAINERS=auto-ubuntu2204-arm
```

Ask: **"Proceed with these changes? (yes/no)"**

Wait for confirmation before editing.

---

## Step 6 — Apply edits

For **each** of the three config files, use the Edit tool to:

1. **Comment a line**: replace `export CONTAINERS=<value>` with `#export CONTAINERS=<value>` (add `#` at the start, no space between `#` and `export`).
2. **Uncomment a line**: replace `#export CONTAINERS=<value>` with `export CONTAINERS=<value>` (remove the leading `#`).
3. **Update a single-platform value**: after uncommenting the single line, if the user chose a non-default container, replace the value portion with the user-supplied name.

Important rules:
- Match the **exact** current line text (including whether it starts with `#` or not) so the Edit tool can find it uniquely.
- Apply each change as a separate Edit call per file — do not batch rewrites of the whole file.
- Do not touch any other lines in the file.
- Apply the same changes to all three files (config16.env, config17.env, config18.env).

---

## Step 7 — Verify

After all edits are done, re-read lines 1–16 of each config file and print them so the user can visually confirm the correct lines are active.

---

## Step 8 — Summary

Print a summary table:

| File | CONTAINERS active | DEB_CONTAINERS active |
|------|------------------|-----------------------|
| config16.env | `<active value>` | `<active value>` |
| config17.env | `<active value>` | `<active value>` |
| config18.env | `<active value>` | `<active value>` |

Remind the user:
- To start a test run: `./run_pep_tf.sh --components <component> --repo release`
- To commit the config change: `/commit` or `git commit -m "Configure containers for <mode> run"`