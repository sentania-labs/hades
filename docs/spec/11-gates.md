# 11. Gates

The pre-PR gate set runs on the collected branch before publication. Every gate is
blocking or advisory (ADR 0024): a blocking failure stops the task; an advisory one
is carried to the reviewer.

## Pre-PR gate list

| Gate | Class | Purpose |
|---|---|---|
| `report_present` | advisory (always, hades #498) | The worker submitted a parseable report with self-review; a gap is listed for the reviewer and Hades composes the completion record itself |
| `exit_clean` | blocking | The worker exited with a clean class; a budget end with commits (`ended_by_budget`) is clean whatever the code |
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
