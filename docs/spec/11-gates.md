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
| `acceptance_checks` | blocking (lab-local pool) / advisory | Crucible's re-run of each acceptance criterion's executable check passed (hades #449) |

## Acceptance checks gate (`acceptance_checks`)

An acceptance criterion may carry an executable `check` (`command`, `expect_exit`,
default 0; 05). Foundry writes these when it scopes, and the worker sees each one under
its criterion in `IDENTITY.md`, verbatim, so it can run it itself.

After the worker exits, the verifier container that re-runs every `required_verification`
command from the collected tree also runs each criterion check, under the id
`acceptance:<criterion id>`, and records its exit as a `verification_run` row. The PR
body's verification table lists these runs beside the required ones. The gate then reads
those rows before any pull request is opened:

- On an attempt that ran on a **lab-local pool** (a pool holding a model on a local
  endpoint, by the routing version the attempt was routed with), a check that did not
  run or exited other than it expects fails the gate and stops the task, whatever the
  policy's advisory list says. The detail names the criterion and the exit, for
  example ``criterion AC2: `uv run pytest -q tests/unit/test_x.py` exited 1, expected 0``.
- On any other attempt (a frontier pool, or no recorded pool) the gate is advisory: it
  passes, and each failure is listed for the reviewer.
- A criterion without a check is listed for the reviewer, as before; the gate is
  skipped when no criterion carries one.

Like `report_present` and `commit_policy`, the gate always runs and is not listed in a
policy's gate groups. It launches no review attempt: the judgement is mechanical.

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
