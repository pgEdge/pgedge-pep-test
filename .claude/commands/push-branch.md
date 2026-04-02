---
name: push-branch
description: Safely push the current branch after a rebase. Handles unresolved merge conflicts, warns about sensitive staged files, stashes uncommitted changes, and force-pushes with --force-with-lease. Use after rebasing with main or when git push is rejected as non-fast-forward.
---

Safely push the current branch to remote. Follow every step in order — do not skip.

## Step 1 — Inspect current state

Run these commands and record the output:
- `git status`
- `git log --oneline -5`
- `git log --oneline origin/$(git branch --show-current) -5 2>/dev/null || echo "remote branch not yet tracked"`

## Step 2 — Detect and warn about sensitive staged files

Scan staged files (`git diff --cached --name-only`) for patterns that suggest secrets or credentials:
- Any file inside `keys/`, `secrets/`, `.secrets/`
- Files named `*.pem`, `*.key`, `*.p12`, `*.pfx`, `open_api_key`, `*credentials*`, `*token*`
- Files named `.env`, `.env.*`

For each sensitive file found:
1. **Stop and warn the user** — do not proceed until the user confirms whether to exclude it
2. Suggest: `git restore --staged <file>` and adding it to `.gitignore`

If no sensitive files are found, continue.

## Step 3 — Resolve unresolved merge conflicts

Run `git status --short | grep "^[UADuad][UADuad]"` to find conflicted files.

For each conflicted file:
- `UD` or `DU` (one side deleted, other modified): ask the user whether to keep or delete
  - Most common: deleted by `them` (main) → run `git rm <file>` to accept their deletion
- `AA`, `UU` (both modified): open the file, show the conflict markers, and ask the user how to resolve
- `DD` (both deleted): run `git rm <file>`

After resolving all conflicts, run `git add` on each resolved file, then:
```
git commit -m "Resolve merge conflicts after rebase"
```

If there are no conflicts, skip the commit.

## Step 4 — Stash uncommitted changes (if any)

Check `git status` for unstaged or staged-but-uncommitted changes (excluding sensitive files already handled in Step 2).

If any exist:
```
git stash push -m "push-branch skill: stash before force push"
```

Record whether a stash was created so it can be restored in Step 6.

## Step 5 — Force push

```
git push --force-with-lease
```

If this fails because the remote ref was updated by someone else since the last fetch, stop and tell the user: "Remote has new commits — review them with `git fetch && git log HEAD..origin/<branch>` before deciding to overwrite."

Do NOT use `--force` (without `--lease`).

## Step 6 — Restore stashed changes

If a stash was created in Step 4:
```
git stash pop
```

If the pop produces conflicts, show them to the user and explain how to resolve.

## Step 7 — Final status

Run `git status` and `git log --oneline -5` and print a summary:
- Branch pushed: yes/no
- Conflicts resolved: list of files
- Sensitive files excluded: list of files (remind user to add them to `.gitignore`)
- Stash restored: yes/no