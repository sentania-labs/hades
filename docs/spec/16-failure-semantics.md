# 16. Failure, restart, retry, cancellation, cleanup, and retention

## Failure classes and default handling

| Class | Meaning | Default |
|---|---|---|
| `completed` | exit 0 and report present | gates |
| `blocked` | `blocked.md` present on a clean exit: exit 0, or 75 where the harness does not use 75 itself (a model cannot set its harness's exit code, FDY-0140). The file's reason line, `missing_capability` or `ambiguous_contract`, and its statement verbatim go on the attempt and the escalation (hades #393) | escalation carrying the reason and the statement, wake, task `blocked`; never retried, no retry consumed, no pool marked |
| `environment` | exit 70, the provider failed before the harness ran, the kernel killed the worker out of memory (exit 137 with the daemon's OOM flag), or the harness was refused | retry if attempts remain; else `failed`. A harness refusal (07, 25) is the exception: it is never retried, because the same refusal would come back. A preparer Job whose Pods outlive the Kubernetes provider's deletion wait (26, hades #503) is not an environment failure yet: the attempt goes back to `pending` with the provider's message on `harness_launch_deferred` (`prepare_pod_wait`, the retry number and its delay), the task back to `scheduled` with a `resume_at` 30 s, then 60 s, then 120 s away, and the same attempt row is prepared again, so none of those retries counts against `max_attempts`. The fourth such failure ends the attempt `environment`, its detail saying whether the Job had completed or was still running when the wait gave up, and the rule above applies from there. The bundle the next correction resumes from survives every one of those failures (retention, below). An attempt that dies before launch records why (below, hades #370) |
| `auth_failure` | harness reported auth problem (adapter classified) | retry per policy (`retry.auth_failure_max`, after `auth_retry_delay_seconds`); wake regardless |
| `quota_exhausted` | harness reported rate or quota limit | reroute (below): mark the pool (or, for a model-only refusal, exclude the model and leave the pool open, hades #373), commit WIP, new attempt on the next candidate in the tier; if none, `awaiting_quota` until the earliest reset; caps exceeded or task pinned to the exhausted pool: task `reported` with the class visible, wake |
| `provider_error` | the harness reported that the model provider or the endpoint failed (adapter classified): the gateway refused or dropped the connection past the launch wrapper's own retries (07, hades #490), or answered a 5xx that is not a gateway status (a 502, 503 or 504, or no answer at all read by an adapter that types it, is `infrastructure`, hades #353) | reroute as a model-only refusal does (below, hades #490): the route that failed is excluded for the next attempt, no pool mark of its own (ADR 0028 marks the pool on the second provider error in a row), a new attempt on the next eligible candidate in the tier resumes from this attempt's sealed bundle, counted against `reroute_max` and never against `max_attempts`; no candidate left, or the cap reached: task `reported` with the class visible, wake |
| `timeout` | contract timeout with no commit on the branch; with commits the exit is `ended_by_budget` (hades #498) | no retry; gates run on what exists; wake |
| `ended_by_budget` | the attempt's time limit or the harness's turn limit stopped the run with commits on the branch (hades #498) | a normal end, not a failure: the bundle is collected and the same gates run as for `completed`; passing gates publish, failing ones go to correction; no retry consumed, no wake for the stop itself |
| `stalled` | Crucible ended the worker for a stall (below) | as `timeout`: no retry; gates run on what exists; wake (`timed_out`) |
| `killed` | terminated by request | `cancelled` |
| `crashed` | non-zero exit not otherwise classified | no retry by default; wake |
| `lost` | provider cannot find the worker | retry if attempts remain and policy allows `lost`; else `failed` |
| `completed_without_report` | exit 0, no report and no commit on the branch; with commits the exit is `completed` and the missing report is advisory (hades #498) | the work gates fail (`commits_present`); no retry; wake |
| `incomplete` | the harness exited cleanly while its own transcript shows a command it was waiting on cut off by the exit (07, issue 128): today only Claude Code's auto-background, a Bash call the CLI moved to the background on its own | attempt `failed`, never a completion, whatever the report claims; the cut-off commands are on `attempt_collected` as `work_in_flight`; no retry, as for `crashed`. A background process the worker chose to leave running (a server, a Codex session it did not poll, a Hermes `background=true` process) is not `incomplete` and is not recorded: it dies with the sandbox, and unfinished work is caught by the pre-PR gates and CI (issue 153) |

A stall is `stalled`, with termination reason `stall` (FDY-0140; it was
recorded as `timeout` before): no activity (10) for the policy's
`stall_fail_seconds`. A command the harness reports in flight is activity (05b,
issue 152), so a long silent build or test run is bounded by its command timeout
and the attempt's `timeout_seconds`, not by the stall limit; a worker with
nothing in flight and nothing written still stalls out. A file changing in the
checkout, the report directory, or on Kubernetes the worker's home (where a
harness keeps its session state) is activity too: the Kubernetes provider reads
that off the running Pod, since its workspace is not a local path, and the Hermes
launch wrapper writes a line whenever Hermes's session store changes. So a Hermes
run that is taking turns is not stalled while it works silently; for AGY, and a
Hermes foreground command that writes nothing, the stall limit counts their
silent commands as before.

A worker in a degenerate run is ended as a stall too, without waiting for
`stall_fail_seconds` (issue 278; how each harness reports it is in 07): the same
command started 8 times in a row, or, on a local endpoint, no tool call 300 seconds
after the harness's turn began. The attempt is drained on the tick that sees it,
with termination reason `stall`, so it is `stalled` as above. The attempt also
records `stall_shape` and `termination_detail`: the shape is `loop:wait` (a shell
`wait`), `loop:empty_command` (a command with nothing in it once its shell wrapper is
off), `loop:command` (any other repeated command) or `no_activity`, so quality
feedback can count it; the detail names the repeated command. Both are on the
`attempt_timeout_drain` and `worker_stalled` events, the attempt view and the
`exit_info` evidence, and the failure wake quotes the detail. A stall at the time
limit records neither.

Retry is never a way to re-roll the worker's judgment. A class retries only
when it is in both the policy's `retry.eligible_classes` and the contract's
`retry_on`. Each retry is a new attempt with the same contract version and a
fresh workspace. A correction execution starts from the remote
`work_branch` head Crucible pushed (08), so published work is never
abandoned or force-pushed over; a plain retry of an unpublished attempt
starts from `base_ref`, because nothing of the failed attempt was ever
pushed.

## An attempt that dies before launch records why (hades #370)

A prepare or launch the provider could not carry out ends the attempt
`environment` at once, `collected` with `stage` (`prepare` or `launch`) and
the provider's words as `detail` on `attempt_collected`, the same words as
`termination_detail` ("prepare: ..."), an `exit_info` evidence row, and the
wake's summary when no retry remains ("attempt N ended environment at
prepare: ...; no retry remaining", `wakes.environment_failure_summary`). The
cases of 2026-10-02 showed the words were not enough on their own, so since
hades #370 the record says:

- **what the preparer said.** A preparer that exits non-zero, times out or
  stalls ends with its last output lines in the detail and the wake, and its
  whole stdout and stderr (uncut, a timed-out preparer's included) kept as the attempt's `crucible/preparer.log`
  artifact (type `preparer_log`) with an `artifact_present` evidence row of
  role `preparer_log`; a secret pattern in it is redacted first (14). The
  Kubernetes provider's path is in 26; the Docker provider keeps the tail in
  the detail as before.
- **a stall is named as one.** A preparer whose log does not change for the
  provider's stall bound (26: `preparer_stall_seconds`, 300 by default, well
  below the 900 s prepare timeout) is ended there with "the preparer Job
  could not build the checkout (stalled): ... wrote no log output for 300s
  while it ran" and its last output, not after the full prepare timeout with
  "(timed out): ... did not finish". Every form of the preparer's failure
  begins with the words the detail had before hades #370, "the preparer Job
  could not build the checkout", and names the cause in the parenthesis: the
  exit code, "stalled", "timed out" or "its Pod never ran".
- **a quota refusal is a wait, never the attempt's failure.** A worker or
  preparer Job the namespace quota refuses at admission, and any other object
  of the preparation a quota 403 refuses, raises `LaunchWaitError`: the
  attempt returns to `pending` and the task to `scheduled`, no exit class,
  end time or termination detail is recorded, no retry is consumed, and the
  refusal (the 403 body or the `FailedCreate` event's message) is the
  `detail` of the `harness_launch_deferred` event verbatim. A later tick
  launches it again (hades #423 for the claim and the Jobs; #370 for the
  rest). Deleting the orphan claims that filled the quota is hades #394.
- **a correction names its resume source.** When a correction's preparer
  fails, the detail and the wake say what it was resuming from, the remote
  work branch (`crucible/<id>`) or the preceding attempt's sealed bundle
  (naming the attempt), before the preparer's words, so a bundle that is gone
  or a branch that could not be fetched is told apart from a failed clone of
  the base.

## Quota reroute and resume (C6b)

A quota exit is not a failure of the worker's judgment, so it is handled
apart from retry. It counts against `reroute.reroute_max` in the routing
policy, never against `lifecycle.max_attempts`, and it never needs
`retry_on` to name it. The sequence, all in one supervisor transaction per
step:

1. The collector runs. Uncommitted changes in the worktree are committed to
   the work branch as one commit whose message begins `wip(crucible):` and
   names the attempt, then pushed; the SHA goes in the event. Nothing is
   discarded silently and nothing is left uncommitted. Squash on merge
   removes the WIP commit from `main`.
2. The attempt's pool is marked exhausted until `reset_at` (05b): the reset
   the refusal states, else now plus the pool's `default_cooldown_seconds`,
   so a mark from a signal that states no reset for the pool expires no
   later than the default cooldown (hades #373). Every pool mark that opens
   an exhaustion raises one wake naming the pool, the reason and the reset
   time (hades #378): the `reported` or `awaiting_quota` wake of steps 3 and
   4 carries that sentence when the refusal wrote the mark, the reroute of
   step 3 raises the pool's own, and an attempt refused while the mark is
   already in force extends the mark without a wake of the pool's own. The
   local-endpoint mark of ADR 0028 (05b) raises the same wake when it opens.
   A model-only refusal (hades #373; 07, Claude Code) writes no pool mark:
   the refused model is excluded until the refusal's reset, or the pool's
   `default_cooldown_seconds` when it states none, as a mark keyed
   `model:<harness>:<model>` (the route) in the same table, listed and cleared like a pool mark, with
   a `quota_exhausted` event of scope `model`; the pool stays open and no
   pool wake is raised.
3. Selection runs again for the tier with marked pools excluded. A
   candidate: a new attempt on the same contract version, resumed from the
   remote work branch as corrections are, and a `reroute` event naming the
   pool left, the model chosen, and the ordered candidates. One
   `quota_exhausted` wake naming the pool and its reset when this refusal
   opened the pool's exhaustion; otherwise no wake. After a model-only
   refusal the reroute stays inside the pool: selection runs with that
   model excluded (the `excluded_routes` path a capacity refusal's retry
   takes, carried on the `reroute` event as `excluded_model`, `excluded_harness` and
   `next_attempt_id`) and its mark turns the model away for every task until
   the reset; the next candidate in the same pool launches.
4. No candidate: the task moves to `awaiting_quota` with `resume_at` the
   earliest `reset_at` among the tier's pools and its models' own exclusions,
   and one informational wake (17). The supervisor tick relaunches at `resume_at` through step 3. Past
   `reroute.resume_max_wait_seconds`, or past `reroute_max`, the task ends
   `reported` with the class visible and a wake, which is the pre-C6b
   behaviour.

A pinned task (05) skips step 3: it waits for its own pool's reset within
the cap or ends `reported`. A quota refusal at launch-time reservation
(the pool over Crucible's own soft limit since selection) is a routing
race, not a provider fact: it creates no exhaustion mark and no checkpoint,
and it goes straight to step 3 with the refused pool excluded, recorded as
a `reroute` event with source `reserve`. It counts toward `reroute_max`
like any other reroute. (Amended 2026-09-20 after the C6b implementation:
the original text sent this case to a wake, which with class-based
selection is a round trip for a decision the rule already makes.)

## Provider error reroute (hades #490)

A `provider_error` exit of an attempt that ran takes step 3 of the quota
reroute with the failed route excluded, as a model-only refusal does: the
`(harness, model)` route the attempt ran on is carried on the `reroute`
event as `excluded_model`, `excluded_harness` and `next_attempt_id`, with
`why` "previous attempt ended provider_error on its route; rerouted to the
next eligible candidate". The next attempt's selection turns that route
away for this task alone, and later attempts in the same reroute chain keep
every earlier provider-error route excluded, so they cannot circle back to a
route that already failed. Nothing is checkpointed or pushed and no `wip`
commit is made: the next attempt resumes from this attempt's sealed bundle,
which the collector writes for a failed attempt with no commit too (08). No
pool mark is written here; ADR 0028's mark for a gateway that failed twice
in a row is written before the reroute and raises the pool's own wake, and
the reroute then leaves the marked pool for the tier's fallbacks. The
reroute itself raises no wake. It counts toward `reroute_max` like any
other reroute, never toward `lifecycle.max_attempts`, and the attempt is
not an ordinary one for retry eligibility. With no eligible candidate, or
past the cap, the execution fails and the task ends `reported` with the
class visible and one wake saying so, which is what every provider error
did before. The launch wrappers' own retries come first (07): Qwen Code's
and Hermes's wrappers start the harness again, up to three times with 5,
15 and 45 second pauses, when the run ended on a transport-level API error
(the gateway gave no answer at all), so a blip never reaches the
supervisor; an error the gateway answered is not retried there.

Timed resumes from `awaiting_quota` count toward `reroute_max` together
with reroutes, per contract version. Attempts created by a reroute or a
resume do not count toward `lifecycle.max_attempts`; retry eligibility
compares the number of non-quota attempts. Once an execution has pushed
any head (a checkpoint or a completed attempt's branch), every later
attempt on that execution resumes from the remote work branch, whatever
created it.

## Delivery-half failures (23)

| Situation | Handling |
|---|---|
| push rejected (remote moved) | `publish_failed`, remote head recorded, wake; never force |
| PR API error | `publish_failed` with response class; wake; Foundry may retry publish |
| required CI check failed | `ci_certification_failed`; evidence captured; wake; no retry, no correction until Foundry decides |
| external review not received in `wait_timeout_hours` | repeat wake; state unchanged |
| head changed by someone else | task `head_diverged`; previous head's acceptance, review, and gates superseded; wake; Foundry chooses recollect or reject; Crucible never overwrites |
| PR closed without merge | task `rejected`, closer recorded |
| release gate failed or tag push rejected | release `gates_failed`; wake; nothing pushed or re-pushed |
| release workflow failed | release `workflow_failed`; wake; no re-tag |

## Restart of Crucible

Supervisor restart triggers reconciliation (10). Workers keep running in
their containers during the restart; the provider's `reconcile` re-attaches
by label. Log capture resumes from the last stored timestamp-and-hash
position (10). Attempt leases that expired during the outage are renewed if
the container is alive; a worker is never marked lost merely because
Crucible was down, only because the provider cannot see it. A publisher job
interrupted mid-way is re-verified on restart: if the remote head already
equals the bundle head the push step is complete; if a PR for the task
exists the open step is complete; otherwise the job re-runs. Stored normalized
webhook deliveries are processed after restart; polling covers anything
delivered while down.

API restart is stateless. In-flight requests fail with 503 and the client
retries with the same idempotency key.

## Cancellation

`POST /tasks/{id}/cancel` records the verbatim reason, moves the task to
`cancelling` if an attempt is running and terminates it with `drain`, or to
`cancelled` at once otherwise. Whatever the worker wrote to the report
directory before termination is collected and stored (it may include a
partial report, kept as an artifact but never parsed as a claim). The
checkout is kept per cleanup policy so partial work is recoverable by a
person. An open PR is never closed by Crucible on cancel.

A cancel that arrives while an attempt is being launched stops the launch
at the step it is on: the launch asks whether the task was cancelled before
the spec is built, before the cache refresh, before the preparer, while the
refresher or preparer Job (Kubernetes) or the preparer container (Docker,
every 2 seconds) runs, in the transaction that would move the attempt to
`launching`, just before the provider creates the worker, and in the
transaction that would record the worker `running`. A cancelled launch
removes what its step made and ends the attempt `killed` with its `stage`
recorded on `attempt_collected`. A cancel that lands after the provider
created the worker settles the attempt in that last transaction, so it is
never `running`, and the launch then kills the worker it started; anything
of it the kill leaves is removed by provider retention. A supervisor that
finds such a worker after a restart or a lease change settles the attempt the
same way instead of adopting it. The cancel sweep
acts only on `pending` and `running` attempts, so it never acts on an
attempt whose launch is in flight. The task reaches `cancelled` as soon as
the launch reaches its next check, which on the kind tier was seconds after
the cancel (hades #189, 2026-09-28).

## Cleanup policy (per policy document, 05b)

| Setting | Options | Default |
|---|---|---|
| `workspace_on_success` | delete, keep, keep_diff_only | keep_diff_only |
| `workspace_on_failure` | delete, keep | keep |
| `container_remove` | always, on_success, never | always, after `logs_drained` |
| `credential_volume_remove` | immediately_after_validated_sync | immediately_after_validated_sync (the field keeps its name; the copy is a directory, 12) |

The credential copy is not subject to `workspace_on_success` or
`workspace_on_failure`: it is removed under every option, `keep` included,
and on every path that never reaches a validated sync at all, including a
start that failed after seeding, a worker the provider lost, and a
transport failure during the read-back (12; on Kubernetes that last one
collects again first, and cleanup removes the copy if it never can).

A cleanup pass runs each reconcile tick. Every deletion is an event and a
`RetentionAction` row naming the policy version that authorized it.
Nothing that a gate consumed is deleted before the task is terminal.

An attempt that ended before its worker launched (`started_at` null: an
`environment` exit at prepare or launch, a `killed` cancel during launch, a
`quota_exhausted` refusal at reserve, an `infrastructure` start failure)
never records `logs_drained`, which the cleanup pass waits for. Its
workspace is cleaned up by a pass of its own, under `delete` whatever the
policy says, because no gate consumed it and no worker wrote on it; the
attempt is recorded cleaned with `attempt_cleaned_up` as any other, and an
`infrastructure` exit waits the attempt lease first. A workspace that is a
resume source for a correction or an interruption retry (an attempt with a
verified bundle head, 08) is kept instead, with the retention label, and
released by the step below; collection is the only thing that records a
bundle head, so no attempt that never launched is one today. The provider
retention sweep does not hold back such an attempt's objects either, so a
claim leaked before this rule existed drains on its own; a claim with the
retention label is honoured (hades #394).

A workspace a cleanup policy kept (`keep`, `keep_diff_only`) is released by
the retention step, one `RetentionAction` of kind `workspace` per attempt,
once its task is terminal (closed, rejected, cancelled) or the task's work
was published, or once `completed_workspaces_days` have passed since
cleanup, whichever is first. It is never released while something may still
read it: the task's latest implementing or correcting attempt stays until
its own bundle is published (acceptance, the publisher and a republish read
the bundle off it), and an attempt whose quota checkpoint never reached the
remote stays while its task is open. A prepare failure never discards the
bundle the next correction resumes from: while the task's latest implementing
or correcting attempt has no workspace of its own (pending, sent back to
pending by the preparer's pod-gone wait, or failed at prepare), the newest
earlier attempt that has one stays (hades #503), and a correction request
finds that attempt's sealed bundle rather than answering
`previous_attempt_bundle_gone`. Before the lab findings of 2026-09-29
nothing released a kept workspace, and on Kubernetes the kept claims filled
the namespace's claim quota.

## Retention (initial defaults, configurable per policy)

| Data | Retention |
|---|---|
| events, gate results, decisions, acceptance results, dispositions | indefinite |
| completion claims, review reports, evidence, external reviews, CI certifications, release records | indefinite |
| diffs and artifact metadata | indefinite; artifact bytes size-capped per attempt by policy |
| worker logs and transcripts | 90 days, then deleted with an event |
| completed worker workspaces | 14 days after cleanup, sooner once the task is terminal or published, never while still needed (above) |
| per-attempt credential copies | removed immediately after validated synchronization, and on every path that skips it |
| wakes | 30 days after ack |
| bootstrap SQLite archive | 180 days |

Retention cleanup is deterministic (a pure function of policy, clock, and
rows), auditable (each action is an event), and idempotent.
