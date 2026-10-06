# C6b implementation notes: class routing and reactive quota reroute

C6b changes the execution boundary from a model request to a capability-class
request. Crucible selects and records the concrete model, harness, image, pool, and
ordered candidate list for every work attempt. An operator may still pin a model, but
the contract must also carry a reason and the pin never falls through to another model.

## Decisions

1. The scheduler uses a routing preview only to decide whether work must wait. After
   the checkout lease is held, the supervisor selects and persists the authoritative
   route in the fenced transaction that moves the attempt to `preparing`. The
   `attempt_launching` event repeats the selected model, harness, image, pool, and
   ordered candidates after workspace preparation.
2. Routing order is capability preference, quality demotion, then weighted
   least-recent. A model never launched on the project ranks first by id. Otherwise
   the largest `(now - last_launched_at) * weight` ranks first, with model id as the
   final tie break. The metrics query reads only the newest quality window per model
   directly for the project, without a task-list page.
3. A worker quota exit creates a durable mark for the selected pool only when the
   harness emitted its authoritative provider-refusal event. Quota-shaped agent text
   can reroute that task, but cannot write shared pool state. Selection omits active
   marks and `GET /v1/routing/usage` exposes the expiry and reason. A reasoned admin
   clear records who cleared it and keeps the row as history.
4. A reroute is a new attempt on the same execution and contract version. It records
   the previous pool, the new model and harness, the candidate decision, and the WIP
   head. Timed resumes and reroutes share `reroute_max`. Quota attempts do not consume
   the ordinary `max_attempts` budget.
5. When no eligible pool remains, the task releases its checkout lease and enters
   `awaiting_quota`. The first wait creates one informational wake. A supervisor tick
   at `resume_at` selects again, including after a supervisor restart. The policy wait
   deadline ends through the ordinary reported path.
6. The quota collector stages all worker changes and, when there are any, makes one
   commit whose subject starts `wip(crucible): attempt`. It never pushes. After the
   supervisor evaluates `scope_contained`, `no_injected_files`, and `no_secrets`, a
   separate no-network checkpoint container pushes a local origin, or the existing
   isolated publisher pushes GitHub with a short-lived installation token. Crucible
   confirms the remote head before it records the reroute. The Docker end-to-end test
   proves that the second attempt starts from that head, and the GitHub integration
   test proves that `branch_pushed` precedes `task_rerouted`.
7. A launch-time reserve race re-evaluates the capability class and reroutes or waits
   without recording a WIP commit, because no worker ran. Review executions retain
   the established refusal path instead of creating an implementation reroute.
8. A successful checkpoint push marks the execution as remote-backed. Every later
   attempt on that execution, including an ordinary environment retry after a reroute,
   starts from the remote work branch.
9. The pool excluded by a task-local quota reroute is stored on the pending attempt.
   The launch selection uses that exclusion once, so a preferred model cannot be
   selected again before the eligible fallback model.

During a quota checkpoint, the collector ignores system and global Git configuration,
temporarily replaces `.git/config` with Crucible's minimal configuration, and runs:

```text
GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
GIT_DIR="$REPO/.git" GIT_COMMON_DIR="$REPO/.git" \
git -c core.fsmonitor= -c core.hooksPath="$EMPTY_HOOKS" \
    -c diff.external= -c core.pager=cat -c safe.directory=* -C "$REPO" add -A
GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
GIT_DIR="$REPO/.git" GIT_COMMON_DIR="$REPO/.git" \
git -c core.fsmonitor= -c core.hooksPath="$EMPTY_HOOKS" \
    -c diff.external= -c core.pager=cat -c safe.directory=* -C "$REPO" commit ...
```

The minimal local configuration sets `commit.gpgsign=false`, `tag.gpgsign=false`,
`core.hooksPath` to a newly created empty directory, and `core.fsmonitor=false`. It
contains no `filter.*` command, so a worker-authored `.gitattributes` filter is a
no-op. The worker's local configuration is restored after collection. Checkpointing
is refused before Git runs unless `.git` is a real directory with no `commondir`
redirect. The refusal follows the unsafe-checkpoint path and names the cause in the
wake.

## Harness reset observations

The adapter first parses the harness-specific structured provider-refusal event, then
reads an explicit machine timestamp from that same event under a JSON key named
`reset_at`, `resetAt`, `resets_at`, `resetsAt`, or `reset_time`. It accepts RFC 3339,
Unix seconds, or Unix milliseconds. When the event carries no such key but states the
reset as a duration ("Resets in 3h52m": hours, minutes and seconds, each optional),
`reset_at` is that duration counted from the supervisor's clock at the moment the exit
was observed (hades #378). Human prose, assistant events, and timestamps on other lines
are ignored. The Claude Code and Codex samples provide no reset time, so the seeded
policy supplies the fallback; AGY's observed refusal of 2026-10-05 states one.

| Harness | Authoritative mark signal | Reset supplied | Seeded fallback |
|---|---|---:|---:|
| Claude Code | `rate_limit_event` with `status: rejected` or `out_of_credits` | no | 18,000 seconds |
| Codex | `turn.failed` with error code `usage_limit_reached` | no | 18,000 seconds |
| AGY | `result` with `status: ERROR` and `RESOURCE_EXHAUSTED`, a structured `code: 429`, or "Individual quota reached" (hades #378) | "Resets in XhYmZs" when stated | 3,600 seconds |

The Claude Code line is the live exhaustion captured on September 17, 2026 at
8:58 AM America/Chicago and already retained in the C5 evidence. The Codex and AGY
lines are the sanitized harness-output shapes used by the established adapter tests.
The predictive quota reader evaluated by S14 and S15 is not used. Its `resetsAt`
output was a separate read, not output from the failed worker, and ADR 0014 is
rejected.

## Database and API shape

Migration 0011 adds selected routing fields to attempts, `resume_from_remote`, the
ordered candidates, task-local reroute exclusions, an execution-level remote-backed
flag, task wait timestamps, and `pool_exhaustions`. Before changing the schema, it
validates the current contract of every non-terminal task and refuses with the
incompatible task ids. Pre-C6b contracts on terminal tasks remain historical records
and are never re-validated. Its downgrade archives C6b event kinds, attempt route
state, and execution remote-backed state. The next upgrade restores them.

Task views expose each attempt's selected model, harness, image, pool, ordered
candidates, previous reroute source, and remote-resume flag. A waiting task exposes
`resume_at`. Administration uses the same service from both entry points:

```text
GET  /v1/admin/routing/exhaustion
POST /v1/admin/routing/exhaustion/{pool}/clear
crucible-admin routing exhaustion
crucible-admin --reason '<reason>' routing clear-exhaustion <pool>
```

## Narrow readings and specification notes

- A contract-supplied image remains accepted only for the fake execution provider so
  existing synthetic tests can choose fake behavior. A real selected execution
  refuses a supplied image and derives it from the promoted image manifest.
- A pinned task waits only on its pinned model's pool and never reroutes to another
  model.
- A pin in either supported contract shape is accepted only for an operator principal
  at submit, amendment, and correction.
- Candidate selection is persisted in the fenced transaction that starts preparation.
  The attempt moves to `launching` only after the selected workspace exists, because
  the lifecycle has a required `preparing` state. The launching event repeats the full
  routing decision. The specification should say "the fenced launch sequence" rather
  than imply that selection first occurs in the state transition to `launching`.
- The launch-time reserve path is treated as a routing race. An implementation attempt
  may move to another eligible pool or wait, but it does not claim worker checkpoint
  continuity. This is narrower than treating reserve refusal as a completed worker
  quota exit.

## Limitations and risks

- A failed checkpoint push stops the reroute and reports the task with the
  `quota_checkpoint` failure detail. It overrides `workspace_on_failure` to keep the
  attempt workspace and `output/work_branch.bundle`, records the retention action,
  and puts the bundle path in the wake. Crucible does not launch from a stale remote
  branch.
- Reset extraction is deliberately conservative. If a future harness changes its
  quota event to include only relative prose, Crucible uses the pool cooldown until a
  stable machine field is observed and added.
- Pool marks are keyed by policy pool name. Renaming a pool in a new policy version
  leaves the old row as history and does not transfer its exhaustion to the new name.

## Verification

| Tier | Command |
|---|---|
| lint | `make lint` |
| unit and PostgreSQL integration | `make test` |
| secret scan | `make scan` |
| Docker end to end | `make e2e` |
| Compose stack | `make smoke` |
