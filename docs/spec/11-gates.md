# 11. Gates

The pre-PR gate set runs on the collected branch before publication. Every gate is
blocking or advisory (ADR 0024): a blocking failure stops the task; an advisory one
is carried to the reviewer.

## Pre-PR gate list

| Gate | Class | Purpose |
|---|---|---|
| `report_present` | blocking | The worker submitted a parseable report with self-review |
| `exit_clean` | blocking | The worker exited with a clean class |
| `commits_present` | blocking | The branch has at least one commit |
| `scope_contained` | blocking (prohibited paths) / advisory | Changed paths are inside `allowed_paths` |
| `no_injected_files` | blocking | No injected-name or harness paths appear on the branch |
| `no_secrets` | blocking | The scanner found no secrets in the diff or artifacts |
| `editor_leftovers` | blocking | No editor or merge leftovers were added to the branch |
| `verification_ran` | blocking | Crucible's own re-run of required commands passed |
| `run_evidence_present` | blocking | A run-evidence artifact is present and valid |
| `criteria_mapped` | advisory | The report maps every acceptance criterion |
| `dependencies_unchanged` | blocking | No new package dependencies were added |
| `ci_unchanged` | blocking | No changes to CI configuration |
| `workspace_clean` | blocking | No provider containers or volumes remain |
| `internal_review_recorded` | skipped | Filled by the orchestrator; the worker self-review is the review |
| `commit_policy` | advisory | Commit authorship matches the policy (FDY-0143) |

## Editor and merge leftovers (`editor_leftovers`)

When the diff adds a file whose name matches an editor or merge backup pattern, the
gate fails with a reason listing every offending path. The gate sets `always_blocks`,
so even an advisory ``scope_contained`` gate still stops the task.

Patterns (matched anywhere in the file name, except ``.#`` which must prefix a
path component — either the start of the path or immediately after a `/`):

- ``*.bak`` — Emacs backup files
- ``*.orig`` — diff ``-p`` backup files
- ``*.rej`` — rejected hunks from ``patch -p``
- ``*~`` — Vim trailing-tilde backups
- ``.*.swp`` or ``*/*.swp`` — Vim swap files
- ``.#*`` — Emacs undo/lock files (e.g. ``src/.#main.py``)

Only newly added files (diff status ``A``) are checked. A leftover that exists on
the base ref and is only edited or deleted is not flagged.  When diff change-status
information is unavailable the gate conservatively checks all changed paths.

## Commit authorship range (`commit_policy`, hades #230)

The collector's author check (`commit_policy_check`, FDY-0135) runs over a range,
`POLICY_FROM..HEAD`, so a correction only re-checks the commits added since the branch
was last published rather than every commit back to `base_ref`. `POLICY_FROM` is never
resolved from `refs/remotes/origin/$WORK_BRANCH` in the checkout the worker runs in: a
worker that moved that ref to `HEAD` could otherwise empty the range and hide every
commit's author from the check.

Instead the preparer resolves the range's start to a commit id while it is still the
checkout's only writer, before the worker starts, and records it in
`prepared-policy-from.txt` on the output mount, which the worker never gets (the same
mount and the same pattern `prepared-base.txt` already uses for the diff's base, 08).
It is the previous `origin/$WORK_BRANCH` head on a resume, the verified bundle head on
a correction resumed from a sealed bundle, or the prepared base when the attempt starts
fresh and there is nothing prior to exclude. The collector reads only that file; when it
is missing or not a commit id, the check does not run and the gate reports it could not
read the commits, same as any other failure to list or read them. `commit_policy` stays
advisory either way (FDY-0143): this changes what range is trusted, not whether the
gate can block a branch.

This only changes where the collector's advisory check gets its range inside the
worker's checkout. The publisher never shares that problem: at merge time it reads
`refs/heads/$WORK_BRANCH` from the real remote over the network (23), never from the
worker-writable checkout, so its own view of the branch stays authoritative and this
change leaves it untouched.
