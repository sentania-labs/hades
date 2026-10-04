# 09. Lifecycle state machines

Every state is a column value guarded by a transition table in
`crucible/domain/lifecycle.py`. An illegal transition raises and is recorded
as an event; nothing bypasses the table. Each transition writes one event in
the same database transaction as the state change.

## Task

Two halves. The supervision half runs a worker to a collected head. The
delivery half (only for `pull_request` deliverables) takes that head to a
merged PR. Corrections loop back through the supervision half against the
existing branch.

```
submitted --start--> scheduled --launch--> running
running --attempt collected--> reported          (every exit class, see below)
running --attempt blocked--> blocked
running --attempt retry--> scheduled              (policy permitted a new attempt)
running --attempt quota_exhausted, another candidate in the tier--> scheduled     (reroute, 16: WIP committed, pool marked, reroute count +1)
running --attempt quota_exhausted, no candidate, wait within cap--> awaiting_quota --wake (informational)-->
awaiting_quota --resume_at reached, a candidate exists--> scheduled
awaiting_quota --resume_at reached, still no candidate, wait within cap--> awaiting_quota   (resume_at moves to the next reset; no new wake)
awaiting_quota --wait cap exceeded, or reroute cap exceeded--> reported --wake-->
awaiting_quota --cancel--> cancelled
blocked --decision--> scheduled

reported --a blocking pre-PR gate fails--> pre_pr_gates_failed --wake-->
reported --no blocking gate fails, internal review required for this head--> awaiting_internal_review --wake-->
reported --no blocking gate fails, internal review not required for this head--> gates_passed
awaiting_internal_review --ReviewReport recorded for this head--> gates_passed
gates_passed --wake--> awaiting_acceptance

awaiting_acceptance --accept, deliverable is artifacts--> accepted
awaiting_acceptance --accept, deliverable is branch or pull_request--> publishing
awaiting_acceptance --reject--> rejected
awaiting_acceptance --needs_more_work (correction attached)--> scheduled
pre_pr_gates_failed --correction attached--> scheduled
pre_pr_gates_failed --reject--> rejected

publishing --branch pushed and verified, deliverable is branch--> accepted
publishing --branch pushed and verified, PR opened or head updated, rounds outstanding--> awaiting_external_review
publishing --branch pushed and verified, PR opened or head updated, rounds satisfied--> awaiting_ci_certification
publishing --push, verification, or PR call failed--> publish_failed --wake-->
publish_failed --retry publish (decision)--> publishing
publish_failed --cancel--> cancelled

awaiting_external_review --review signal from allowlisted login--> external_feedback_received --wake-->
awaiting_ci_certification --review signal from allowlisted login--> external_feedback_received --wake-->
awaiting_external_review --wait_timeout_hours elapsed--> (repeat wake, reason external_review_overdue; no state change)
awaiting_external_review --operator waived the remaining rounds (ADR 0025)--> awaiting_ci_certification
external_feedback_received --every comment dispositioned, none is fix, rounds satisfied--> awaiting_ci_certification
external_feedback_received --every comment dispositioned, none is fix, rounds outstanding--> awaiting_external_review
external_feedback_received --correction attached--> scheduled

awaiting_ci_certification --required checks green on the accepted head--> ready_for_merge --wake-->
awaiting_ci_certification --a required check failed on the accepted head--> ci_certification_failed --wake-->
ready_for_merge --a required check on the accepted head turns red--> ci_certification_failed --wake-->
ready_for_merge --new review signal from allowlisted login--> external_feedback_received --wake-->
ci_certification_failed --ci-decision rerun--> awaiting_ci_certification
ci_certification_failed --green certification observed on the accepted head--> awaiting_ci_certification
ci_certification_failed --ci-decision correct, correction attached--> scheduled
ci_certification_failed --ci-decision reject--> rejected
ready_for_merge --correction attached--> scheduled

{awaiting_external_review, external_feedback_received, awaiting_ci_certification,
 ci_certification_failed, ready_for_merge}
    --PR conflicting, head Crucible's, merge of main clean--> (head replaced; ready_for_merge
      and ci_certification_failed go to awaiting_ci_certification) --wake pull_request_conflicting-->
    --PR conflicting, head Crucible's, merge of main stopped on conflicts--> scheduled
      (a merge-main `correct` execution from the remote branch tip) --wake pull_request_conflicting-->

{awaiting_external_review, external_feedback_received, awaiting_ci_certification, ready_for_merge}
    --PR head changed out of band--> head_diverged --wake-->
head_diverged --head-decision adopt (legacy name: recollect)--> scheduled    (a `correct` execution recollected from the remote work branch tip; the new head gets its own claim, then all gates, review, acceptance start over)
head_diverged --head-decision reject--> rejected
head_diverged --cancel--> cancelled

{awaiting_external_review, external_feedback_received, awaiting_ci_certification,
 ci_certification_failed, head_diverged, ready_for_merge} --PR merged (observed)--> merged --wake-->
{scheduled, awaiting_quota, running, blocked, reported, pre_pr_gates_failed,
 awaiting_internal_review, gates_passed, awaiting_acceptance, publishing,
 publish_failed}
    --PR merged (observed) while a correction runs against it--> merged --wake-->
    (a live attempt is ended as a cancel ends it; the task stays merged; the merged
    head is compared with the last head Crucible pushed, and a mismatch escalates)
{awaiting_external_review, external_feedback_received, awaiting_ci_certification,
 ci_certification_failed, head_diverged, ready_for_merge} --PR closed unmerged (observed)--> rejected --wake-->
merged --included in a release contract--> release_candidate
release_candidate --release succeeded--> released
release_candidate --release failed or cancelled--> merged
{accepted, merged, released} --close (orchestrator POST)--> closed

{submitted, scheduled, blocked, awaiting_internal_review, awaiting_acceptance,
 pre_pr_gates_failed, publish_failed, awaiting_external_review,
 external_feedback_received, awaiting_ci_certification, ci_certification_failed,
 head_diverged, ready_for_merge} --cancel--> cancelled
running --cancel--> cancelling --all attempts terminal--> cancelled
```

Terminal: `cancelled`, `rejected`, `closed`. There is no task-level
`failed`: a failed attempt with no retry remaining still produces a
`reported` task whose gates then fail (`exit_clean`, `report_present`), so
Foundry always sees the outcome through the same path. Foundry alone moves
`awaiting_acceptance`, `pre_pr_gates_failed`, `external_feedback_received`,
`ci_certification_failed`, `head_diverged`, and `publish_failed` forward
and issues `close`.
Crucible alone moves everything else, and only Crucible touches GitHub.

A correction re-enters at `scheduled` with a `correct` execution whose
workspace starts from the remote `work_branch` head (08). It then passes
through `reported`, every mechanical pre-PR gate including the full
verification re-run, acceptance, and `publishing` again; the push updates
the PR head. A correction does not automatically require another internal
review: `awaiting_internal_review` is entered for a corrected head only
when the correction contract sets `request_internal_review: true` (Foundry
asks for one when the correction is substantial, expands scope, or creates
architectural risk) or the policy sets
`internal_review.required_for_corrections: true`. Because the default
policy has `retrigger_after_correction: false` and `required_rounds: 1`,
the second pass through `publishing` lands in `awaiting_ci_certification`,
never back in `awaiting_external_review`.

That includes a correction attached in `ready_for_merge`, after Foundry's
own review of the full diff found a defect (hades #360). The round already
counted on the PR stays counted, so the corrected head goes through the
pre-PR gates, is published to the same PR, is certified on its own CI, and
returns to `ready_for_merge` without a new external review request. The PR
stays open while the correction runs and is still polled for a merge: a
merge observed in any state of the correction moves the task to `merged`,
publishing and publish_failed included (hades #379).

Only `needs_more_work` or `internal_review` can start a correction from
`ready_for_merge`.

Publishing and publish_failed count as correction states only when an
earlier publication of the task completed; a first publication that fails
after opening its PR is polled in full, and a merge of it is an early merge.

When a merge wins over a correction, the task's head is the merged head when
it is among the heads Crucible pushed and confirmed on the remote, not a
corrected head that was collected and never pushed; when the merged head is
not among them, the task keeps the latest head Crucible pushed. The pushed
heads are the PR's head records marked as pushed by Crucible and the
`branch_pushed` events, never the PR's current head, which a poll sets to a
head someone else pushed (hades #379). The
`task_merged` event records the head GitHub merged, the latest recorded pushed
head, whether the merged head was a quota checkpoint, whether the merged head
is among the pushed heads, and any head Crucible pushed after the merged one;
the wake says which. When the merged head is not a pushed head, or the head
merged was only a quota checkpoint (which passed no gate and was never
accepted), or Crucible pushed a later head after it, the wake is an escalation
instead. A head pushed after the merge is escalated the same way whichever
record comes first: a poll that records the merge before the push is
confirmed, a lookup that records it after, or a quota checkpoint pushed after
a merge nobody had seen yet. A push confirmed after the task is already merged
is not recorded as a pushed head, and the publication that made it stops
there. A merge recorded without its head (before hades #379) is taken to be of
the latest recorded pushed head. A quota checkpoint is not pushed, nor
recorded, once the task is merged or otherwise finished (`release_candidate`,
`released`, `rejected`, `cancelled`, `closed`); one that lands while the task
finishes is escalated with `checkpoint_after_finish`, which says nothing was
merged by it.

A PR closed unmerged while a correction is under way, or after a first
publication failed, is recorded on the PR (which is then no longer polled)
and wakes Foundry with `pull_request_closed`; the task stays where it is,
because a correction state has no edge to `rejected`. In `publish_failed`,
the operator can reopen the PR and then republish, or cancel; a publication
that finds the PR closed itself says the same. In running,
gates, or `awaiting_acceptance`, the correction continues but publication
will fail unless the PR is reopened, so cancellation is the usual answer.

Once the corrected head is the accepted head, review feedback made on the
heads Crucible pushed before it, before the corrected head appeared, is
**settled**: the correction is the answer to it, so a `fix` disposition on
it no longer holds the task, and a comment on it asks for no disposition.
Dispositions stay add-only; nothing is deleted. Feedback on the accepted
head itself still needs one, and so does a comment made after the
corrected head appeared, even as a reply on an old thread. A head someone else
pushed is never settled this way; it goes through `head_diverged`.

**Every state after `publishing` is bound to the accepted head.** Gate
results, the review report, and the AcceptanceResult all name the SHA
they were made for. A PR head that Crucible did not push invalidates all
of them: the task moves to `head_diverged` and nothing about the new SHA
is trusted, however green its CI. Foundry decides whether to `recollect`
(the task re-enters at `scheduled` with a `correct` execution against the
remote `work_branch`, so the divergent head gets a completion claim of its
own, and then re-runs pre-PR gates, internal review when applicable, and
acceptance before `publishing` re-verifies it) or to reject. Re-entering at
`reported` instead would put the new head in front of gates with no claim
behind it, so `report_present` would fail and a correction would be the
only way forward anyway.

**Branch-only deliverables** (`branch`, allowed only under a policy with
`deliverables.allow_branch_only: true`) pass through `publishing` like a
PR deliverable: the bundle head is pushed and `branch_pushed_at_head`
verified, then the task moves to `accepted`. Nothing is accepted
unpublished.

**A PR closed without merge rejects the task from wherever it is.** A
person can close a PR at any point after it is opened, so the `rejected`
edge is drawn from every observed state, not only from `ready_for_merge`
(23). The closer is recorded where the provider exposes it.

Cancelling a task after `publishing` never closes the PR; Crucible records
the cancellation on the PR record and leaves the PR to the operator.
Whether an open PR is closed is a person's act.

`POST /attempts/{id}/terminate` on the running attempt moves the attempt to
`cancelled`; the task then follows the retry rule (a terminate is class
`killed`, never retry-eligible), so it goes to `reported`. Terminating an
attempt is not cancelling the task; `POST /tasks/{id}/cancel` is.

## Execution

`created` -> `active` -> `succeeded` | `failed` | `cancelled`. Role
`implement`, `correct`, or `review`. `succeeded` when the final attempt is
`succeeded`; `failed` when attempts are exhausted or a non-retryable class
occurred; `cancelled` on task cancel. A `review` execution's success means
a parsed `ReviewReportV1` exists from an attempt that exited 0 with a clean
exit class (an `incomplete` review is not recorded, issue 128); its verdict
does not change task state by itself (Foundry's acceptance does).

## Attempt

```
pending --prepare--> preparing --ready--> launching --started--> running
running --provider reports exit--> exited
running --timeout--> exited            (after drain then kill; exit_class timeout; the attempt row holds drain_deadline, killed_at, termination_reason)
running --terminate--> terminating --provider reports exit--> exited   (exit_class killed)
running --provider cannot see it--> exited   (exit_class lost)
exited --collect done, logs drained--> collected
collected --classify--> succeeded | blocked | failed
{preparing, launching} --error--> collected (exit_class environment)
```

Selection (05b) happens once per attempt inside the fenced launch
sequence, at its start, before the workspace is prepared: the attempt row
carries the selected harness, model, image, and the ordered candidate list
from that moment on, and the `launching` event repeats the decision. A
reroute is simply the next attempt selecting again with the exhausted pool
excluded.

`collected` is the single point where outputs, logs, artifacts, evidence,
the branch bundle, and the parsed report exist. For an attempt classified
`quota_exhausted` the collector also commits any uncommitted change in the
worktree to the work branch as one `wip(crucible):` commit naming the
attempt, and pushes it, so the next attempt resumes from the remote branch
(16). Classification:
`succeeded` requires exit 0 and a parsed report; `blocked` requires exit 75
and `blocked.md`; everything else is `failed` with the recorded exit class.
The task transition happens on classification: `succeeded` and `failed`
both lead to task `reported` (unless a retry is permitted), `blocked` leads
to task `blocked`.

Lease expiry is not a transition. Loss is decided only by provider
observation (10).

## Worker

`injected` -> `alive` (first heartbeat) -> `quiet` (no activity for
`stall_warn_seconds`) -> `stalled` (past `stall_fail_seconds`; attempt is
terminated with exit_class `timeout`, reason `stall`) | `exited`.

## PullRequest

`opening` -> `open` -> `merged` | `closed`. Head history is a list of
(SHA, pushed_by: crucible | other, observed_at). A head Crucible did not
push is recorded with `pushed_by: other`, moves the task to
`head_diverged`, and wakes Foundry; Crucible never force-pushes over it. A
merge of main pushed by Crucible (23) is recorded `pushed_by: crucible`.

## Release (24)

`submitted` -> `verifying` -> `gates_failed` | `tagging` -> `tagged` ->
`workflow_running` -> `succeeded` | `workflow_failed`; `cancelled` from any
state before `tagging`.

## Gate

Per attempt (pre-PR) or per PR head (publication, post-PR) per gate:
`pending` -> `pass` | `fail` | `skipped` | `deferred` (no evaluator in
this phase yet; non-blocking, never pass; 11) | `error` (evaluator could
not run; treated as fail). Pre-PR gates evaluate once per collected head.
Post-PR gates re-evaluate each reconcile tick and on every processed GitHub
delivery for the head until they resolve or the task is terminal.

## Escalation

`open` -> `answered` (a Decision references it) -> `closed`. An escalation
older than `escalation_stale_hours` produces a repeat wake, not a state
change.

## Transition side effects (always in the same transaction)

| Transition | Side effects |
|---|---|
| task `start` | the API records the start request and moves the task to `scheduled`; the supervisor materializes the execution and first attempt (`pending`) on its next tick, because those tables are fenced to the supervisor (14) |
| attempt `launching` | checkout lease taken; identity bundle hash recorded; image digest resolved and recorded; container name `crucible-<attempt_id>` reserved |
| attempt `running` | attempt lease created; worker `injected` |
| attempt `exited` | final log drain scheduled; provider handle retained until `collected` |
| attempt `collected` | artifacts, evidence, claim rows, branch bundle; verification re-run scheduled (11); checkout lease released per cleanup policy |
| attempt classified | retry decision per policy; task transition |
| task `reported` | pre-PR gate evaluation scheduled |
| task `awaiting_internal_review` | wake (reason `internal_review_needed`); if policy `executor` is `crucible` and the contract names a reviewer execution request, a `review` execution is created |
| task `gates_passed` / `pre_pr_gates_failed` | wake created for the submitting principal |
| task `publishing` | publisher job enqueued: mint installation token, push bundle head, open or update PR, render body; events before and after each GitHub call |
| task `publish_failed` to `publishing` | an orchestrator or operator supplies a reason through `republish`; the same accepted head and sealed bundle resume at the failed step, subject to `limits.publish_retry_max`; no tick retries automatically |
| task `awaiting_external_review` | PR observation registered (polling and webhook routing) |
| task `external_feedback_received` | ExternalReview and comment rows written; wake |
| task `ci_certification_failed` | CICertification row with captured check, workflow, job, log pointers, head SHA; wake; no retry, no correction |
| task `head_diverged` | previous head's acceptance, review, and gate results marked `superseded` (rows kept); PR observation continues; wake |
| task `ready_for_merge` | wake (reason `ready_for_merge`) |
| task `merged` | merge SHA and merger recorded; wake (informational) |
| task `blocked` | escalation opened; wake created |
| task `cancelling` | running attempt terminated (`drain`); leases released when terminal |
| task `closed` | cleanup pass eligible for all workspaces of the task |
