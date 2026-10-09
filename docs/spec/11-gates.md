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
| `scope_contained` | blocking (prohibited paths) / advisory | Worker commit paths are inside `allowed_paths` |
| `no_injected_files` | blocking | No injected-name or harness paths appear on the branch; shim content is checked on every added or modified path (hades #377, #400) |
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

## The verifier's log (`verification_ran`, hades #608)

The verifier runs beside the attempt's declared test services, told the same
`CRUCIBLE_TEST_DATABASE_URL` as the worker (05b, 08, 26). Each command's log is kept
as a `verify/<id>.log` artifact of at most 64 KB. A longer log keeps its head and its
tail, half each counted in encoded bytes (so undecodable output cannot crowd out
either end), with one line in place of the middle saying how many bytes the log
had: the first error is near the head, and the summary the runner prints last is at
the tail.

## The gate probe (`gate_proves_nothing`, `check_cannot_run`; hades #412, #517, #608)

Before a worker is prepared, the provider runs the task's own required checks (the
`required_verification` commands the policy's `repository.required_checks` does not
name) on a fresh checkout of `base_ref`, beside the same declared services as the
attempt. A check proves something only when it fails on the unchanged tree:

- A check whose command names a repository path the unchanged tree lacks (a word with
  a `/` or ending in `.py`, a pytest node id's `::name` cut off) fails there by
  definition, whatever its runner exits (pytest exits 4, or 2, for a path it cannot
  find). The probe's evidence records it as `new file named` with the paths. This is
  how a new test file the attempt adds is proof; it is never a reason to block. Every
  check's paths are looked up on the fresh checkout before the first command runs, so
  a check that deletes or creates a file changes nothing about what any check names.
- A check that exits other than its `expect_exit` fails on its own.
- A check that passes on the unchanged tree is not proof.
- Exit 127 (the program is missing) blocks the task as `check_cannot_run`.

When no probed check fails, the task is blocked as `gate_proves_nothing`. The refusal
names every probed check by id with its command and exit, and gives the fix: amend the
contract to add a check that fails on the unchanged repo, for example a
`uv run pytest -q tests/unit/test_issue_<n>_<slug>.py` naming the new test file, under
the next free `V<n>` id. A blocked task accepts that amendment; rescheduling it runs
the probe again before any worker, and neither refusal counts against `max_attempts`.
The Docker provider has no probe yet, and its evidence says so.

## Injected instruction, harness, and identity files (`no_injected_files`)

The gate catches three categories:

1. **Instruction-name paths** (e.g. `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `crucible-identity.md`)
   that the branch adds.  Editing or deleting an instruction-name file the merge base
   already has is the repository's own work and passes (#369).

2. **Harness-directory paths** (paths under `.claude/`, `.codex/`, `.hermes/`,
   `.gemini/`, `.crucible/`, `crucible/identity/`, `.crucible-shims/`).
   Adding a new entry under a harness directory, turning an entry into a symlink or back
   (status ``T``), or committing the shim's content still fails.  Editing or deleting a
   file the base already has under a harness directory is allowed (#446).

3. **Shim content**: writing the shim blob into any path that the gate treats as
   injected.

The gate output names the rule that fired, for example:

- `CLAUDE.md: new entry added`
- `.claude/hooks/check.sh: symlink`
- `.claude/hooks/check.sh: shim content`
- `CLAUDE.md: error: undecodable name (invalid UTF-8)`

When the merge base already has a harness-directory file, the passing detail names the
``existing harness-directory file edited or deleted`` rule. When a harness path is added or
turned into a symlink, the detail names `"new entry added"` or `"symlink"` respectively.

Task submission also returns an entry in ``TaskView.warnings`` when an
``allowed_paths`` entry can reach a harness directory. This includes direct paths and
broad globs such as ``**`` and ``src/**``; the warning is also retained on later task
views.

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

`scope_contained` checks the complete path list from commits in `BASE..HEAD`, rather
than the merge-base diff. The prepared base is a trusted commit id, not a ref the worker
can move. Two-dot reachability excludes every commit already reachable from the base,
so merging a newer base into the work branch cannot charge the base's paths to the
worker. Paths in the worker's own commits remain in the list even if a later commit or
base merge hides them from the final diff.

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
