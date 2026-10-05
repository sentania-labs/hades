# 10. Events, leases, heartbeats, and reconciliation

## Events (EventV1)

Append-only table `events` with a global monotonic `seq` (BIGSERIAL), `ts`,
`kind`, `task_id`, `execution_id`, `attempt_id`, `principal` (who caused it:
`crucible`, an API principal, `github`, or the literal `worker`, whose
attempt is the row's own `attempt_id` rather than part of the principal
string), `payload`
(JSONB, schema per kind), and `verified` (false for anything a worker
asserted). `github` is the principal for **webhook ingress only**: the
events recording that a delivery was received, that its event was outside
the handled set, or that it was rejected. It is a third case rather than a
shade of `crucible` on purpose: an event whose principal is `crucible` is
fenced to the supervisor lease (14), and the webhook endpoint holds no
lease and authenticates no caller. Polling is the supervisor's own work, so
every polled observation and everything derived from one (head changed,
review received, reaction received, check concluded, PR closed or merged)
is recorded under `crucible`, fenced like any other supervisor write. A
rejected delivery's event carries the reason and, for the claimed event
name, only one of the handled set or `unrecognized`; nothing else an
unauthenticated caller chose is stored. Every outward-facing GitHub action
is two events: `github_call_started` and `github_call_completed` or
`github_call_failed`, with the endpoint, repository, and response class,
never a token.
Kinds are an enum; adding one is a migration. The harness kinds are
`harness_refused` (a launch refused for an unknown, disabled, unsupported
or credential-less harness, which is terminal for the attempt and not
retried), `harness_launch_deferred` (the per-harness cap held the launch
back, which is a wait rather than a failure), `harness_enabled` and
`harness_disabled` (the administrator's flag, from the admin surface, 25),
`credential_synced` (an auth file written back to the source, or the reason
it was not), `worker_progress` (a parsed progress line, principal `worker`
and `verified` false, capped per 07), and `report_parse_failed` (a
`report.yaml` that exists and does not parse, with the parser's errors and
`report_present` true, which is distinct from no report at all). The
administrative kinds, all written by the admin surface of 25 with the
principal, the reason, and a before-and-after summary, are
`credential_validated`, `credential_probed`, `credential_login_started`,
`credential_login_finished`, `credential_rotated`, `credential_removed`,
`credential_retired_shredded` (the retention sweep's shred of a retired
directory, or the failure to finish one), `image_promoted`, `github_checked`,
and `admin_refused`. `admin_refused` is the refusal of a mutation that
carried no reason, carried a secret-shaped one, or arrived while no live
supervisor held the lease; it is written outside the refused caller's
transaction, since that transaction rolls back, and best effort, since a
refusal is never made worse by a failure to record it. Harness enable and
disable keep the `harness_enabled` and `harness_disabled` kinds above.

`GET /admin/audit` is this stream filtered to the kinds an administrator
caused, and there is no second administrative log. The filter is the ten
kinds above plus `harness_enabled` and `harness_disabled`, `principal_created`,
`repository_registered` and `repository_attestation_recorded`, and
`policy_uploaded` and `routing_policy_uploaded`: administration is wider than
the credential surface, and a client that assumed only the credential kinds
would reject valid pages. Events are
never updated or deleted. Retention: forever in v0.x (volume is small); archival to object
storage is a later policy.

The API exposes events per task and a global feed with cursor. Log lines
are not events; they are a separate stream (below).

## Logs (LogStream)

`log_chunks`: `attempt_id`, `stream`, `offset_start`, `offset_end`, `ts`,
`content` (bytea, gzip above a threshold). The supervisor pulls provider
logs each tick and appends. Resume position is the last stored
`(timestamp, sha256(line))`: the pull asks the provider for lines since
that timestamp and skips until the hash matches, which avoids both
duplicates and drops among lines sharing a timestamp (Docker has no byte
offsets). Live tail streams chunks as they land. Log bytes advancing is one
heartbeat signal. An attempt records `logs_drained` after the final drain
following exit; cleanup never runs before it. A provider may bound one pull
(the Kubernetes provider reads a few MiB at a time), so the final drain pulls
until a pull brings nothing, up to 256 pulls. (Made concrete 2026-09-25,
issue 63.)

## Leases

| Lease | Held by | Renewed | Expiry meaning |
|---|---|---|---|
| supervisor | one Crucible instance | every tick (default 5 s), TTL 30 s | another instance may take over; the old one must stop acting on expiry |
| attempt | the supervisor on behalf of a running attempt | every observation tick | informational: an expired attempt lease means observation stopped (Crucible was down); loss is decided only when the provider cannot see the worker |
| checkout | an attempt, for `repository.url` + `work_branch` | for the life of the attempt | released on terminal attempt state or by reconcile after loss |

Leases are rows with `holder`, `expires_at`, `fenced_token` (monotonic).
Every write the supervisor makes carries its fenced token; a write with a
stale token is rejected by a trigger. This is what stops a paused-then-resumed
old supervisor from corrupting state after a takeover.

## Heartbeats

`heartbeats`: `attempt_id`, `ts`, `signal` (container_running, log_advanced,
fs_changed, command_running, progress_line), `detail`. `command_running` is
written while the harness's live log reports a command in flight (07, issue 152),
whenever the newest activity is older than a minute, or than half the shorter
stall limit when that is less; a command reported more than a minute past its
command timeout no longer counts. Its `detail` names up to five commands and their
`count`. `fs_changed` is written when the worker's files have moved since the last
look: the supervisor walks a local workspace (Docker) every tick; the Kubernetes
provider, whose workspace is a claim, runs a read-only `find` in the live Pod over
the checkout, the report directory and the home, no more often than a
`command_running` renewal (FDY-0140). The supervisor derives worker state:
any signal within `stall_warn_seconds` is `alive`; none within
`stall_fail_seconds` is `stalled`. Defaults: 300 s warn, 1800 s fail,
overridable per policy. A worker the live log shows in a command loop, or on a
local endpoint with no tool call before its first-response deadline, is `stalled`
on the tick that sees it, whatever its signals (16, issue 278). A worker that emits progress lines but changes
nothing for the fail window is still stalled; progress lines are unverified.

## Timeouts

`timeout_seconds` from the contract, bounded by policy. On expiry: `drain`
(SIGTERM, wait `grace_seconds`, default 60), then `kill`, collect whatever
exists, attempt `timed_out`. The report gate then fails unless a valid report
was written before the signal.

## Reconciliation

Runs at supervisor start and every `reconcile_interval` (default 60 s):

1. Take or verify the supervisor lease. If not held, do nothing.
2. For every attempt in a non-terminal state, ask the provider to observe.
   - Provider sees it running: renew attempt lease, pull logs, record
     heartbeat signals.
   - Provider sees it exited: run the exit path as if the tick had caught
     it live.
   - Provider cannot see it: mark `lost`, record an event with the last
     known observation, decide retry per policy.
3. For every provider handle with a Crucible label but no live attempt row:
   orphan; terminate and clean up; event recorded.
4. For every checkout lease past expiry with no live attempt: release.
5. For every task in `reported` with pending gates: evaluate.
6. Process stored GitHub webhook deliveries not yet processed; for every
   PR or release in an observed state whose last poll is older than
   `github.poll_interval_seconds`: poll, record changes, re-evaluate
   post-PR gates (23).
7. For every task in `publishing` whose publisher job has no live
   container: re-verify remote head and PR existence, then resume or
   mark `publish_failed`.
8. For every wake undelivered past its retry schedule: redeliver.
9. Apply retention: each deletion an event and a `RetentionAction`.
10. Write the supervisor liveness row (`last_tick`, duration, counts).

A launch has a quick half and a slow half. The quick half (route, gate, take
the checkout lease, move the attempt to `preparing`) runs in the tick, one
attempt after another. The slow half (build the spec, prepare the checkout,
which on Kubernetes is the cache refresher and the preparer Jobs, and start
the worker) runs as a task of its own that the tick starts and then waits on
for at most a third of the lease TTL, capped at 5 seconds. A launch that has
not ended by then keeps running, the tick goes on to every other step, and a
later tick counts it when it ends without waiting on it again. A supervisor
that loses its lease, or finds it was renewed under a new fenced token,
cancels the launches it still has running. The attempt is left alone by the
stranded-launch rule while this process is still launching it; after a
restart it is stranded as before. So a prepare that takes minutes never
keeps the supervisor from renewing its lease or serving another attempt
(hades #190, 2026-09-28).

Collection is the same (the lab findings of 2026-09-29): the final log
drain, `collect` and the finish of an exited attempt run as a task of their
own, which the tick waits on for the same bound and otherwise leaves running.
On Kubernetes a collection is the collector, bundle verifier, verifier and
reader Pods, up to about 90 minutes in the worst case, and it used to hold
the tick for all of it. The attempt is that task's alone until it ends: the
observe step, the cancel sweep and the stranded rule leave it be. A lost
lease or a stop cancels it, and the attempt, still `running` with its logs
drained, is collected again by the next holder from its workspace, which
still holds the work (a collection that ran partway may have written its own
output there, and the next one writes it again).
A collection the provider could not finish because its backend could not
answer (`ProviderUnavailableError`: a refused, reset or timed-out
connection, an API server that answered 429 or 5xx, a namespace quota that
refused a Pod) is tried again every 30 seconds for up to 30 minutes from the
first failure, and only then fails the attempt as environment.

Reconciliation is idempotent; running it twice changes nothing the second
time except lease expiry times and the liveness row. That property is
tested. The liveness row records the last successful tick, the last tick
error, and consecutive failures; readiness (04) reads it.

## Foundry disconnect

Nothing above references an orchestrator session. Foundry's absence only
means wakes accumulate for poll. That is the whole mechanism for "continue
while disconnected," and it is tested by killing the API client mid-run.
