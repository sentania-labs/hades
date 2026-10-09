# 04. Versioned API contracts

Base path `/v1`. JSON only. OpenAPI generated from the Pydantic models in
`crucible/contracts/` and published at `/v1/openapi.json`. Breaking changes
create `/v2`; `/v1` keeps serving for at least one minor release after.

## Authentication

Bearer tokens, created by `crucible admin token create --principal <name>
--role <role>`. Stored as salted hashes. Roles:

| Role | May |
|---|---|
| `orchestrator` | everything below except admin |
| `operator` | everything an orchestrator may, plus record decisions of kind `release_authorization` and other operator-only decision kinds |
| `observer` | GET only |
| `admin` | token management, repository registration, bootstrap import, policy upload, forced reconcile, image promotion |

Every mutating request records `principal` on the resulting event. Workers
never hold a token. The GitHub webhook endpoint uses HMAC verification
instead of a bearer token (below).

The administrative HTML interface at `/ui` authenticates the same principals.
Its sign-in form exchanges the bearer token for a signed, HttpOnly,
SameSite=Strict session cookie, not a second identity. Reader roles receive
read-only pages and the `admin` role receives controls. Every mutating form
also carries a signed-session CSRF value. Sign-out clears the cookie.

## Conventions

- IDs are ULIDs. Orchestrator-stable IDs travel in `external_id`.
- Idempotency: every POST that creates accepts `Idempotency-Key`; a repeat
  with the same key and body returns the original result; the same key with
  a different body is 422 with problem type `idempotency-key-reuse`.
- Pagination: `?limit=&cursor=` on every list; responses carry `next_cursor`.
- Errors: RFC 9457 problem details with a stable `type` URI per error class.
- Time: RFC 3339 with offset in responses; requests accept the same.
- Every response includes `schema_version` of the resource.

## Endpoints

### Tasks

| Method | Path | Purpose |
|---|---|---|
| POST | `/tasks` | Submit a task contract (body: `TaskContractV1`). Validates, persists, returns the task in `submitted`. Does not launch. |
| POST | `/tasks?proposed=true` | Propose a task contract (hades #424). The same body and the same validation as a submission, but the task is stored in `proposed`, which is not authorized: `start` refuses it (409) and the supervisor never schedules it. Only an operator's answer moves it on. |
| POST | `/tasks/{id}/approve` | Operator or admin only, in `proposed`. Body: `reason` (required, recorded on the audit event) and an optional `note`. Moves the task to `submitted` (event `task_approved`) and starts it as `start` does, under the policy its contract names, which must not be retired. A `note` is appended verbatim to the contract's objective, after a blank line and `Operator direction:`, as a new contract version; the event carries the same note. |
| POST | `/tasks/approvals` | Operator or admin only. Body: `task_ids` (proposed tasks, in the order the operator selected them) and `reason`. Approves them in one transaction, in that order, all or none. Each `task_approved` event records `batch` with its `id`, the task's `position`, `of`, and the whole `order` by external ID. The tasks are scheduled in that order, and the supervisor takes scheduled tasks in the order they were queued, so the selection order is the queue order. Returns `batch_id` and the tasks. |
| POST | `/tasks/{id}/send-back` | Operator or admin only, in `proposed`. Body: `reason` and `note` (both required). Moves the task to `sent_back` (event `task_sent_back`) and wakes the task's orchestrator with reason `sent_back`, whose summary is the note verbatim. An `amend` in `sent_back` proposes the task again. |
| POST | `/tasks/{id}/reject` | Operator or admin only, in `proposed`. Body: `reason`. Moves the task to `rejected` (event `task_proposal_rejected`) and wakes the orchestrator with reason `proposal_rejected`. A task anywhere else is not rejected through this endpoint. |
| GET | `/tasks` | List with filters: `state`, `project`, `repository`, `external_id`, `updated_since`. |
| GET | `/tasks/{id}` | Full task view: contract versions, executions, latest attempt summary, gate summary, PR summary, open escalations, `notes` (the operator's notes, newest first, hades #489, each with its `delivery_state` and evidence, 27), `questions` (the worker's questions and their answers, oldest first, 27), `handoffs` (the decisions handed between Foundry and Hades, 27), and `delivery`, the task's paper trail: the work branch, the head Crucible pushed and when, the PR number, link and state, and once merged the merge commit SHA, who merged and when (hades FDY-0143). |
| POST | `/tasks/{id}/notes` | Operator or admin only (hades #489). Body: `text` (the operator's words, as typed) and `verbatim` (default true). Stores a note with the author and time, records `task_note_recorded` with the text as its reason, and returns the task view with `notes`. The next attempt's or correction's `IDENTITY.md` opens with the task's notes (06). |
| POST | `/tasks/{id}/questions/{question_id}/answer` | Operator, admin or orchestrator (hades #208 item 2, 27). Body: `answer_text` and optionally `resume_from` (`last_attempt`, default, or `remote_branch`). Records the answer on the worker's question (who, when, the words) and brings it back in one act: a correction version with the answer as its instructions, resumed and scheduled, which closes the escalation; a task that cannot take a correction keeps the answer and closes an open escalation with an `escalation_answer` decision. Answered once; a second answer is 409. Returns the task view with `questions`. |
| POST | `/tasks/{id}/start` | Move to `scheduled`; body names harness, model, image, provider, policy version, and optional overrides. This is Foundry's dispatch decision. Overrides create an amendment (05); until the amendment path exists (C2) the body must agree with the contract. |
| POST | `/tasks/{id}/cancel` | Request cancellation; body carries reason and the deciding principal's verbatim words. The API writes the task state and enqueues termination for the supervisor. |
| POST | `/tasks/{id}/amend` | Attach a new contract version; allowed only in `proposed`, `sent_back`, `submitted`, `blocked`, or `awaiting_acceptance`. An amendment in `sent_back` moves the task back to `proposed` (hades #424). |
| POST | `/tasks/{id}/review` | Request the internal non-author review of the current collected head. Body either names an execution request for a Crucible `review` execution, or carries an uploaded `ReviewReportV1` produced by the orchestrator through its own harness. Allowed in `awaiting_internal_review`. The API records the request; the supervisor creates the review execution, or turns the uploaded report into evidence and resolves the gate, on its next tick, so clients poll the task rather than read the outcome from the response. The reviewer identity recorded is the authenticated caller's (or the review attempt's), never the one the document names. |
| POST | `/tasks/{id}/accept` | Record an `AcceptanceResult` (accepted, rejected, needs_more_work) with reasoning for the current collected head. Orchestrator role only. |
| POST | `/tasks/{id}/republish` | In `publish_failed`, manually retry publication with a reason. The same accepted head and sealed bundle retry through the remote ownership check (23). Policy `limits.publish_retry_max` caps retries and the wake reports the cap. Orchestrator or operator role. |
| POST | `/tasks/{id}/corrections` | Attach a correction: a new contract version whose `correction` section names the review comments or CI findings it addresses, plus an execution request. Creates a `correct` execution against the existing remote branch. Allowed in `pre_pr_gates_failed`, `external_feedback_received`, `ci_certification_failed`, `ready_for_merge` (reason `needs_more_work` or `internal_review`), and after `needs_more_work`. |
| POST | `/tasks/{id}/dispositions` | Record `ReviewDisposition` rows for received external review comments. Orchestrator role. |
| POST | `/tasks/{id}/head-decision` | In `head_diverged`: `recollect` (the task re-enters `scheduled` with a `correct` execution against the remote work branch, so the new head gets a claim of its own before any gate reads it), `reject`, or `cancel`, with reasoning. |
| POST | `/tasks/{id}/ci-decision` | In `ci_certification_failed`: record the cause Foundry determined (enum in 23) and the action: `rerun` (recorded; the operator re-runs on GitHub, 23), `correct` (followed by a correction), `reject`, or `cancel`. |
| POST | `/tasks/{id}/decisions` | Record a `Decision` (verbatim text, who, what it resolves). The `kind` field is validated against a closed list (FDY-0509): `accept`, `accept_no_ci`, `escalation_answer`, `recollect`, `release_authorization`, `scope_clarified`, `waive_external_review`. An unknown kind returns 422 with the accepted list in the problem detail body. `verbatim` and `resolves` each have a two-character minimum, so a one-character response cannot stand in for an operator's recorded reasoning. `escalation_answer` is the kind Foundry's client sends to answer an escalation. `scope_clarified` is used by the blocked-task flow to clarify scope before rescheduling. Internal closure kinds (`task_cancelled`, `task_closed`) are not exposed through this endpoint; they are used only by `cancel_task` and `close_task`. |
| POST | `/tasks/{id}/close` | Orchestrator closes an `accepted`, `merged`, or `released` task. |
| GET | `/tasks/{id}/events` | Ordered events for the task and its children, including `handoff_recorded` (27): a decision handed between Foundry and Hades with `action`, `direction`, `principal`, `local_time` and `words`. |
| GET | `/tasks/{id}/pull-request` | The PR record with head history, the external review cycles and their completed components, external reviews, comments, dispositions, the observed reactions, whether reactions are observable at all, and CI certifications. |
| GET | `/events` | Global feed, `?cursor=&kind=&since=`. |

The operator's answers to a proposal (`approve`, `approvals`, `send-back`,
`reject`) are each one event with the operator as principal and the reason in its
payload; they are listed on the Audit page. An orchestrator principal, including
the one that proposed the task, gets 403 from all four.

The task mutations `cancel`, `accept`, `review`, `dispositions`, `corrections`,
`ci-decision`, `head-decision`, `decisions`, `amend`, and `close` return 403
when an orchestrator principal does not own the task. Operator and admin
principals are exempt from the ownership comparison, without changing each
endpoint's existing role requirements.

### Executions and attempts

| Method | Path | Purpose |
|---|---|---|
| GET | `/executions/{id}` | Execution with its attempts. |
| POST | `/executions/{id}/retry` | Create a new attempt now, if policy permits; body carries reason. |
| GET | `/attempts/{id}` | Attempt with worker, lease, heartbeat summary, image digest, exit info. |
| GET | `/attempts/{id}/logs` | Log chunks; `?stream=stdout|stderr&offset=`; `Accept: text/event-stream` for live tail. A process admits at most `service.max_sse_log_tails` live tails (default 20); the next tail receives an RFC 9457 `429` problem with `Retry-After: 1`, while ordinary non-streaming reads remain available. |
| GET | `/attempts/{id}/artifacts` | List artifacts with type, size, sha256. |
| POST | `/attempts/{id}/artifacts` | Upload an artifact: raw request body with `type` and `filename` as query parameters (no multipart dependency). The logical filename is kept apart from the content-addressed storage path. Principal recorded. Becomes evidence on the next supervisor tick. Orchestrator role. |
| GET | `/artifacts/{id}` | Metadata; `/artifacts/{id}/content` streams bytes. |
| GET | `/attempts/{id}/report` | Parsed `CompletionClaimV1` or 404 if none. |
| GET | `/attempts/{id}/gates` | Gate results with evidence links, each marked `classification` `blocking` or `advisory` with its advisory `findings`, and `for_reviewer`: the failed advisory gates and findings with their detail (ADR 0024). The task's `gate_summary` keeps verdict strings in `results`, carries gate messages in the sibling `details` map, carries the same `classification` and `for_reviewer`, and names only blocking failures in `failing`. |
| POST | `/attempts/{id}/terminate` | Stop the worker: `mode=drain|kill`, reason, verbatim words. |

### Releases

| Method | Path | Purpose |
|---|---|---|
| POST | `/releases` | Submit a `ReleaseContractV1` (24). Validates, verifies the referenced authorization decision exists, returns the release in `submitted`. Orchestrator role. |
| GET | `/releases/{id}` | Release with gate results, tag, observed workflow run, outcome. |
| POST | `/releases/{id}/cancel` | Withdraw before tagging. |

### Supervision and health

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness (process up). No auth. |
| GET | `/ready` | Readiness of this API process, no auth (compose healthchecks, the Kubernetes readiness probe and Foundry's probe need it). True only when both hold: database reachable; migrations at head **and** the live schema matches the ORM metadata (schema drift is not-ready, naming the first difference). The response also carries a `supervisor` check (the lease is held and the holder's last tick within the lease window succeeded, or the last error summary), which is reported and does not decide readiness: a supervisor in trouble must not take the API and the admin UI offline. `/supervisor` and a red banner on every admin UI page are where supervisor health shows (hades #190, 2026-09-28). |
| GET | `/supervisor` | Lease holder, last tick, tick duration, queue depths, provider status, GitHub observation status (last poll, webhook deliveries pending), and `github.publishing_waiting`: each task in `publishing` whose publication cannot start, with the reason the supervisor recorded, since when, and the escalation it opened, if any (23). |
| POST | `/supervisor/reconcile` | Force a reconciliation pass now. Admin. |
| GET | `/wakes` | Pending wakes for the caller's principal, in wake id order; `?limit=&cursor=&include_acked=&since=`. The page is keyed by the wake id (ULIDs are unique and ordered): `next_cursor` is the opaque form of the last id returned and the next page resumes strictly after it, so any number of wakes sharing one `created_at` page through without a repeat (hades #502). `since` is the older time filter, still honoured for a caller that sends it: it narrows the page to wakes created at or after that time and is not the pagination key. |
| POST | `/wakes/{id}/ack` | Mark handled, with what was done. The system acks its own `external_review_overdue` and `ci_certification_overdue` wake once the pull request has merged or closed, with the reason as the ack note (17). |

### Policies, repositories, harnesses, images, providers

| Method | Path | Purpose |
|---|---|---|
| GET/PUT | `/policies/{name}/{version}` | Read or upload a policy document. Versions are immutable once referenced. The shipped seed is `default-software` version 2, which names `default-routing` version 2 (05b); version 1 stays beside it for the tasks admitted under it. Where an endpoint takes a policy without a version, the newest version applies. |
| GET/PUT | `/repositories/{name}` | Register a target repository: URL, installation reference, default branch, policy, and the operator's attestation that the external reviewer reviews all pull requests there (23). Admin. Never carries a credential. |
| GET | `/harnesses` | Supported harnesses: adapter supported version range, installed versions per image, credential requirements, capability flags. |
| GET | `/images` | Worker images known to Crucible: harness, version, digest, promotion state. |
| POST | `/images/{digest}/promote` | Set promotion state (`default`, `retained`, `retired`). Admin; records the decision. |
| GET | `/providers` | Registered execution providers and their capabilities. |
| GET/PUT | `/routing/{name}/{version}` | Read or upload a routing policy (05b). Admin. |
| GET | `/routing/usage` | Per-pool usage in the current window, from AttemptMetrics, read through the routing policy the newest version of the named policy points at; `?policy_version=` selects another. |
| GET | `/routing/history` | Per-model outcomes: `?model=&project=&since=`; wall time, cost where reported, exit class, gates passed, corrections, acceptance. Foundry reads this before selecting. |

### Administration

`/v1/admin/*`, admin role, versioned with the rest: status, harnesses, credentials (validate, probe, login, rotate, remove), images, providers, github, repositories, audit. Every mutation there takes an optional reason, recorded as an audit note, and the few that hand the supervisor work (committing a bootstrap import, rotating or removing a credential) need a live supervisor lease (the operator's direction of 2026-09-29); revoking a token, removing a repository or a credential, and committing a bootstrap import require one (the operator's decision of 2026-09-25, crucible#117). Repository registration through `PUT /v1/admin/repositories/{name}` is one of those mutations; the `PUT /repositories/{name}` above is the older non-administrative form and is unchanged. Detail in 25. `GET /v1/capabilities` gives orchestrator principals the sanitized read-only subset Foundry needs to report an unavailable capability, with its workers, tasks and wakes filtered to the caller's own (an operator sees all).

### GitHub ingress

| Method | Path | Purpose |
|---|---|---|
| POST | `/github/webhook` | Optional accelerator, off by default locally (23). Verifies the `X-Hub-Signature-256` HMAC against the raw request body in memory; unsigned or mismatched deliveries are rejected and counted, and nothing of them is stored. An accepted delivery is parsed and normalized in memory to the fields Crucible uses (delivery ID, event and action, repository, PR number, head SHA, review or comment IDs, login, reviewed SHA, check conclusion), user-controlled text is passed through the secret scanner and redaction before it is kept, and only that normalized record plus a SHA-256 of the original body is stored. The raw body is never persisted. Deduplicated by delivery ID. Processed by the supervisor tick. |

### Bootstrap import

| Method | Path | Purpose |
|---|---|---|
| POST | `/import/bootstrap` | Accept a `BootstrapExportV1` bundle; returns a verification report. Admin. Detail in 15. |
| POST | `/import/bootstrap/{id}/commit` | Make the imported records authoritative after verification. |
| POST | `/import/bootstrap/{id}/discard` | Withdraw a verified import: its tasks are retired, never deleted (ADR 0029). Admin. |

## The client

`crucible` is the one command-line client of this API, shipped with it; its
orchestrator verbs (`tasks`, `task`, `wakes`, `submit`, `start`, `accept`,
`review`, `dispositions`, `corrections`, `ci-decision`, `head-decision`,
`decisions`, `cancel`, `close`, `republish`, `health`) are Foundry's former
`foundry-crucible` verb for verb, with the same arguments, paths, bodies and
`X-Foundry-Reason` header, and `crucible admin` is 25's CLI. docs/client.md is
the reference; what binds the API is this:

- Every command prints one JSON envelope: `ok`, `kind`, `state`, `data` (this
  API's response, never reworded), `next`, `warnings`, and on failure `error`
  carrying the problem document above whole. Exit 0, 1 on a refusal or failure,
  2 on usage. `crucible schema` prints the envelope's schema and each `kind`'s,
  the orchestrator ones generated from the response models here. It also
  carries `decision_kinds`, the sorted list of accepted decision kinds that
  the same constant the validator uses (FDY-0509).
- `next` is the actions valid from the record's state for the principal in use,
  each as an argv with what it needs. It follows the lifecycle table (09) and
  each endpoint's state guard; a verb this section marks orchestrator-only is
  offered only to an orchestrator or operator, and an orchestrator is reminded
  of the task's owner, because it may act only on its own tasks. What a state
  cannot show (a live lease, a matching review comment) stays the API's to refuse.
- The client never decides a role itself. A verb whose route admits one role
  class proves it by succeeding; otherwise the client asks this API with two
  read-only requests, `GET /admin/audit?limit=1` (admin guard) and
  `GET /capabilities` (orchestrator guard), and an answer other than 200 or 403
  leaves the role unknown and `next` empty. Neither request records anything.
- The bearer token comes from the environment or a token file, never a flag,
  and is redacted from everything the client prints. Redirects are refused.

## Wake delivery

Wakes are rows first. Delivery is best-effort POST to the configured webhook
with retry and backoff; `/wakes` is the durable fallback that Foundry polls on
every start-of-session. A repeating notice (`external_review_overdue`,
`ci_certification_overdue`, `escalation_stale`) keeps exactly one open wake per
task per cause, so the poll never floods (hades #502). Detail in 17.

## Versioning of contracts inside the API

`TaskContractV1`, `CompletionClaimV1`, `ReviewReportV1`, `WorkerIdentityV1`,
`PolicyV1`, `ReleaseContractV1`, `EventV1`, `WakeV1`, `BootstrapExportV1`.
Each carries `schema_version`. The API rejects unknown major versions and
records the rejection as an event on the principal.
