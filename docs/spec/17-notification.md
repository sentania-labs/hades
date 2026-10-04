# 17. Notification and Foundry-wake contract

## When Crucible wakes Foundry

Only when judgment is required or work has stopped needing it:

| Reason | State |
|---|---|
| `internal_review_needed` | `awaiting_internal_review` |
| `gates_passed` | `awaiting_acceptance` |
| `pre_pr_gates_failed` | `pre_pr_gates_failed` |
| `needs_more_work` | after a `needs_more_work` verdict, until a correction is attached |
| `blocked` | `blocked` (escalation opened) |
| `publish_failed` | `publish_failed` |
| `external_feedback_received` | `external_feedback_received` |
| `external_review_overdue` | repeat, no state change |
| `external_review_trigger_needed` | `awaiting_external_review` on a head whose cycle needs the orchestrator's trigger under the operator's account (23) |
| `ci_certification_failed` | `ci_certification_failed` |
| `ci_certification_overdue` | repeat, no state change |
| `ci_rerun_needed` | after a `ci-decision rerun`: Crucible records the intent, the operator re-runs it on GitHub (the App holds no Actions write) |
| `head_diverged` | `head_diverged` (decision required) |
| conflicting pull request, head Crucible pushed or adopted | `pull_request_conflicting`, once per head; the summary names the next action: Crucible merges main itself, and when git reports conflicts launches a merge-main correction from the remote branch tip. A dirty head pushed out of band raises `head_diverged` instead |
| `ready_for_merge` | `ready_for_merge` |
| `merged` | `merged` (informational), from any delivery state; the summary names the state when it was not `ready_for_merge` |
| `pull_request_closed` | `rejected`: the pull request was closed without merge, from any delivery state |
| `other_pull_request_open` | after a publication to the task's own pull request that found another pull request open on the work branch; it is not adopted, and Foundry decides what becomes of it (23) |
| `checkpoint_after_finish` | an escalation: a quota checkpoint was pushed to the branch of a task that finished while it was pushed (merged, rejected, cancelled or closed); nothing was merged by it, and the push is not recorded (23) |
| `release_gates_failed`, `release_succeeded`, `release_workflow_failed` | release lifecycle (24) |
| `attempt_failed`, `timed_out`, `lost` with no retry remaining | `reported` |
| `quota_exhausted`, `auth_failure` | any |
| `harness_unavailable` | launch refused: the harness is unknown, disabled by either gate (25), outside the adapter's tested version range, or has no credential. Terminal for the attempt; the retry rule skips it because the same refusal would come back |
| `escalation_stale` | repeat |
| `supervisor_takeover` | informational, once |
| `bootstrap_import_verified` | awaiting commit |

Every wake in the delivery half stands on an observation the supervisor
recorded, under the `crucible` event principal like any other supervisor
write (10), never on an orchestrator session being connected. A webhook
delivery only makes that observation happen sooner; the ingress events it
writes under the `github` principal wake nobody by themselves.

Progress is not a wake. Foundry polls or tails logs when it wants progress.
Review feedback is never sent to a worker; it is only ever carried to
Foundry.

## WakeV1

```json
{
  "id": "01J...", "schema_version": "1.0",
  "principal": "foundry",
  "reason": "external_feedback_received",
  "task": { "id": "01J...", "external_id": "FDY-0042", "state": "external_feedback_received" },
  "attempt_id": "01J...",
  "pull_request": { "number": 18, "url": "...", "head_sha": "abc123..." },
  "summary": "1 review from chatgpt-codex-connector[bot] on abc123: 3 comments, 0 dispositions recorded",
  "for_reviewer": [],
  "links": { "task": "/v1/tasks/01J...", "pull_request": "/v1/tasks/01J.../pull-request" },
  "created_at": "2026-09-16T06:10:00-05:00"
}
```

`for_reviewer` (ADR 0024) lists the failed advisory pre-PR gates and the
advisory findings, each as `{gate, detail}`, on the `internal_review_needed`,
`gates_passed` and `pre_pr_gates_failed` wakes; the summary names the gates
after "For the reviewer:", and the details, which can carry paths the worker
chose, stay in the list. It is empty on every other wake.

## Delivery

1. Row first: the wake is committed in the same transaction as the state
   change that caused it.
2. Webhook: POST to the principal's configured URL with an HMAC signature
   header; retries with exponential backoff for `wake.retry_hours`
   (default 24), then stops retrying but keeps the row.
3. Poll: `GET /v1/wakes` returns unacked wakes for the caller. Foundry's
   start-of-session procedure always polls, so webhook failure only delays.
4. Ack: `POST /v1/wakes/{id}/ack` with what Foundry did. Unacked wakes are
   listed on `GET /supervisor` as a count.

For a Foundry running inside an interactive harness on a workstation, poll
is sufficient and is the default. Moving Foundry into a persistent service
later changes only the delivery target, not the wake contract.
