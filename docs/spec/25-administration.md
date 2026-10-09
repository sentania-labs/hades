# 25. Crucible administration: admin API, `crucible admin` CLI, credential onboarding

Harness credentials, harness availability, worker-image versions, execution
providers, GitHub App configuration, and operational health are Crucible
administrative concerns. Foundry may detect and report that a capability is
unavailable; it never stores or manages a Crucible credential. Another
orchestrator or a person can administer Crucible through the same surface
without Foundry.

## Board

The read-only Board is next to Tasks in the administration navigation. It leads
with a kanban (hades #334): one column per lifecycle stage, left to right,
Proposed, Queued, Running, Awaiting Foundry, Awaiting Codex, Awaiting CI, Ready to
merge, Blocked or failed, and Done in the last 24 hours. Proposed holds the tasks the
orchestrator proposed (hades #424), with the operator as their holder; a proposal the
operator sent back waits in Awaiting Foundry until the orchestrator amends it. Every
open task is one card, in the column for its state: the title, the external ID
small, a chip for who holds the ball (the worker with its harness and model,
Foundry, Codex, CI, the operator for a wake or a decision, or Hades between
waits) and the time since the card entered its column. The age is read from the
task's transition events, which are written in the same transaction as the state
change, so a card that went submitted then scheduled has been Queued since it was
submitted. The age colours after 30 minutes in Awaiting Foundry or Awaiting
Codex and after the policy's CI budget (`ci_certification.wait_timeout_hours`,
default 6 hours) in Awaiting CI. An open escalation moves a card to Awaiting
Foundry. Nothing is dragged; Hades moves cards as states change, and the page is
a projection over the task, event, wake and policy tables with no table of its
own. Child tasks stay nested under their parent external ID inside the column,
and the whole card links to the task page. The columns are a strip that scrolls
sideways, one column to a phone screen.

Queued is in queue order, the order the supervisor takes the tasks in: each
scheduled card shows its queue position and, when it was approved in a batch, its
place in the batch. Under the kanban, "Approve proposed tasks together" lists every
proposal with an order selector: the operator numbers the ones to approve, 1 first,
gives a reason, and approves them in one action. The numbers are the queue order;
they are recorded on each approval, and the tasks appear in Queued in that order. A
number given twice is refused rather than guessed between. Nothing is dragged.

The Tasks page leads with the same proposals: a list, the same batch form, and one
section per proposal with its contract in words (title, objective, acceptance
criteria, required verification, context links, repository, who proposed it and
when) and the operator's four answers, each needing a reason: Approve, Approve with
note (the note is appended verbatim to the objective), Send back (the note is the
orchestrator's wake), and Reject. The task page of a proposed or sent-back task shows
the same contract, and the answers while it is proposed. The answers are open to
operator and admin principals; an observer reads the contract only. Each answer is
listed on the Audit page with its reason.

Under the kanban, the earlier list is still there behind "Show the task list":
every non-terminal task as a table row, grouped by what it waits on and then by
its parent external ID, linking to the full task page and showing its closing
issues, current route, state age, newest pending wake, pull request and CI
state, corrections, and elapsed attempt time against the attempt timeout.

Routing expands the ordered candidates recorded on the current attempt, including
busy candidates skipped before the selected route. Tokens follow with both
per-attempt values and totals by harness, model, and pool. "Not recorded" means
the harness adapter did not provide usage; the Board does not estimate it.

The quality log covers tasks whose pull request opened in the last 14 days. It
shows pre-PR gate failures, Codex finding severities and dispositions,
corrections, outcome, and submit-to-merge time, with totals by harness and model.

### The card (hades #489)

Each board card opens at `/ui/board/{task_id}`. The card shows, in words: where the
task is and what it is stuck on (its lane and state, the latest attempt and how it
ended, the failing gates with their detail, the CI failure, a failed publication, and
the open escalation as the first sentence of its question); the head and CI state
(the collected head, the pushed head and branch, the latest CI certification, the gate
results on the head); the cost so far (attempts, worker minutes, Codex rounds); the
contract as text (objective, scope, acceptance criteria, required checks, dispatch
tier and rationale, expected deliverable); the attempt timeline (each attempt's role
and number, start and end, harness and model, exit class, and one line on how it
ended); the correction history (each correction version, what it corrects, its
reason, where it resumed from, and its instructions); the operator notes, newest
first; and the phase actions. It links to the issue, the pull request, the full task
page and the board. The panels flow into one column at phone width.

**Operator notes.** A note is the operator's words on one task: author, time, text
as typed, and a verbatim flag. Operators and admins post one from the card or with
`POST /v1/tasks/{id}/notes`; the task read lists them newest first. Every note is an
audit event (`task_note_recorded`) whose reason is the text. The next attempt's or
correction's `IDENTITY.md` opens with the notes under "Operator notes", before the
contract (06), so the worker reads them first.

**Phase actions.** A pull-down offers only the moves the task's state allows, each
mapped to an existing operation:

| Move | Operation | Offered when |
|---|---|---|
| Approve and queue | `approve` | `proposed` |
| Start now | `start` | `submitted` |
| Correction, resume from the PR branch | `corrections` with `resume_from: remote_branch` | a correctable state with a pull request |
| Correction, resume from the last attempt | `corrections` with `resume_from: last_attempt` | a correctable state |
| Accept the collected head | `accept` with verdict `accepted` | `awaiting_acceptance` |
| Answer the open escalation | `decisions` on the escalation (rescheduling a blocked task) | an open escalation |
| Cancel with reason | `cancel` | any state the lifecycle lets cancel |

The operator's words are required. **Go** stores them as a note and applies the
chosen move with them: the cancel reason and verbatim, the correction instructions,
the acceptance reasoning, the decision verbatim, the approval reason. **Next phase**
applies the lane's default move, published on the card: Inbox to Holding pen
(approve), Holding pen to In progress (start now), Stuck to In progress (correction,
resume from the PR branch), Waiting on Scott to In progress (answer the escalation),
In progress to Graveyard (cancel). Wins and Graveyard have no next phase. Every action
records a `task_phase_action_applied` event with the move, the operation, the lane,
and the note's text as `verbatim`, beside the operation's own event; both show on
the Audit page with the words as the reason. The actions are open to operator and
admin principals; an observer reads the card without the forms.

## Memory (hades #208)

`/ui/memory` is the Admin Memory page, two tabs over the shared memory store and the
decision ledger (27): Memory items (text, source, when, scope, promoted by, with Edit
and Forget as clicks for a principal who may write) and Decisions (the words, channel,
local time, what they apply to, the transcript link). It opens with the rule in one
line: transcripts stay per channel; decisions and memory are shared by every channel and
every persona; minion findings become memory only when Hades or Scott promotes them.
Edit supersedes an item with a corrected one and Forget supersedes it with no
replacement; nothing is deleted, and the ledger is never edited. The page reads and
writes through the same services as `GET /v1/memory`, `POST /v1/memory/{id}/supersede`
and `POST /v1/memory/{id}/forget`. It has no navigation link yet; the link is a
one-line follow-up in `render.py`.

## Shape

- **Versioned admin API** under `/v1/admin`, admin role only, generated
  into the same OpenAPI document. Every mutation is an event with the
  principal, before-and-after summary (never a value), and the reason when
  the operator gave one.
- **`crucible admin` CLI** (the `admin` group of the one `crucible`
  command; `crucible-admin` remains as a shim that prints one deprecation
  line on stderr and runs `crucible admin` with the same arguments) calls
  the same application services as the API
  (`crucible/application/admin/*`), not a private path; the CLI is a
  client of the service layer running in-process (local mode) or against
  the API (remote mode, `--api-url URL` or `--remote`). Every operation in
  the table below exists on both, with the same audit event, and parity
  tests drive both entry points; a further test holds `crucible admin` to
  every verb and argument `crucible-admin` had (the command tree captured
  at 0cf0075), and to the same API call per verb in remote mode. Four operations are CLI-only by design because they need the
  database or filesystem directly and run before or beside the service:
  `migrate`, `import` (bootstrap, which also has its verify and commit API
  in 15), `export`, and `token create`; they are audited the same way and
  the API exposes their status (migration head, import state, token list
  without secrets) but not their execution. Of those four, `migrate` and
  `token create` exist today; `import` and `export` are named here and in 14
  but arrive with the bootstrap ledger (15) in C6, and the CLI's own help
  states the gap rather than asserting commands that are not there.
- **One envelope.** Every `crucible admin` verb prints one JSON object on
  stdout (docs/client.md): `ok`, `kind`, `state` where the record has a
  lifecycle (a credential's `state`, a harness's enabled or disabled, a
  bootstrap import's state), `data` exactly as the service or the API
  returned it, `next`, `warnings`, and on failure `error` with the
  service's problem detail in the API's RFC 9457 shape, in local mode as
  in remote. Exit 0 on `ok`, 1 on a refused or failed operation, 2 on
  usage. `next` lists the admin verbs valid from the record's state: for a
  credential, what 25's operations accept from its state (`login` only in
  local mode, because only there can the harness's own CLI run); for a
  harness, the enable flag's other value; for each harness on the image
  list, `promote` for each image offered for it that is not its default and
  `rollback` while it has a previous image; for a pool, `clear-exhaustion` while its mark
  is active; for a verified import, `commit`. Each names its argv,
  including the `--api-url` or `--config` the command ran with, and what
  the caller must supply. Logs, and the interactive parts of a login, go to
  stderr.
- **Sanitized status** only. No response, log line, event payload, table
  row, or artifact ever carries a credential value, a token, a key, or a
  file's contents. Validation results are booleans, enumerations,
  timestamps, hashes of non-secret metadata, and version strings.
- **Web administration under `/ui`.** The worker-supervision readiness
  milestone is met. The server-rendered interface calls the same application
  services as the API and CLI and owns no state. Reader principals see the
  same operational pages without controls; administrator principals can use
  every mutation. Its signed, HttpOnly, SameSite=Strict cookie carries the
  same bearer token, and every form mutation also requires a CSRF token.

## Status model

`GET /v1/admin/status` returns one document; each part is also its own
resource.

| Part | Fields |
|---|---|
| `harnesses[]` | name, whether it is enabled, the configuration default and the administrator's decision with the reason each carries and the configuration's `warning` (hades #174), adapter supported range, its default image and previous image (ADR 0018), images known (reference, harness version, digest, and whether it is this harness's default or previous image), credential status (below), concurrency limit and current use, last launch outcome (a probe records itself there as `probe:<exit class>`, or `probe:inconclusive:<cause>` when it decided nothing) |
| `credentials[harness]` | `state`: `absent`, `configured` (files present, shape unchecked), `invalid` (the shape check failed, or a probe observed the provider refusing the credential; never a probe that merely did not finish), `validated` (a conclusive probe ran the credential); `mount_mode` (`ro`, `rw-narrow`); `last_validated_at`; `last_auth_failure_at` and its exit class (set only by those two conclusive outcomes); `refresh_verified` (bool, from the compatibility test); `source_fingerprint` (sha256 of the file **names and sizes**, never contents); `session_compatibility`: `unverified`, `verified`, `failed` |
| `providers[]` | name, capabilities, `health`: `ok`, `degraded`, `unavailable` with detail (daemon reachable, proxy reachable, network present, disk headroom) |
| `github` | App id (public), whether a credential is configured, key present (bool), key fingerprint (sha256 of the public key), where it is kept (`stored_in`: the Secret or directory, whether it exists and whether the service owns it), per registered repository: installation covers it, last check, webhook enabled. The App's slug, install link and installations are read live by the picker, not stored |
| `readiness` | crucible#123: `ready`, `ready_harnesses`, the global `steps` (supervisor, no repository, no harness ready), and per real harness `ready`, `off` (the configuration default keeps it off and no administrator has decided, hades #174) or `not_ready` with its `steps`, each `{code, text, fix}` naming the page that fixes it. Read from the same state the other pages show (on Kubernetes the credential is the harness Secret's state). Test fixtures (the script harness) are left out |
| `supervisor` | as `GET /supervisor` (lease, last tick, last error) |
| `workers` | active attempts with task, harness, model, image digest, started_at, last heartbeat |
| `tasks` | counts by state; lists for `blocked`, `pre_pr_gates_failed`, `publish_failed`, `ci_certification_failed`, `head_diverged` |
| `wakes` | pending count per principal, oldest pending |
| `retention` | last run, actions taken, next due, bytes reclaimed |
| `audit` | cursor into admin events |

## Operations (each is an application service; API and CLI are thin)

| Operation | API | CLI | Notes |
|---|---|---|---|
| list harnesses | `GET /admin/harnesses` | `harnesses list` | |
| disable or enable a harness | `POST /admin/harnesses/{name}/disable` and `/enable` | `harnesses disable|enable` | the administrator's decision, stored and audited, which replaces the configuration default from then on with no restart (hades #174); configuration retained; an unverified harness can be enabled and the answer carries the configuration's reason as `warning`; running attempts finish; new launches of a disabled harness refused with a wake |
| test a harness | `POST /admin/harnesses/{name}/test` | `harnesses test NAME` | crucible#118: the path a real task takes, in order, stopping at the first failure: the harness is enabled; it has its own worker image at a supported version (ADR 0018); its credential is stored; the routing policy in force names a model for it (a local model brings its endpoint URL); a worker runs that image with the credential under the worker's egress, the bounded probe below with every harness in a worker, Hermes included; and one minimal model call answers. Each step is reported pass, fail or not run, in plain words, with the failing step's cause and never the run's output. The run takes up to a couple of minutes, so it is a background job (issue 147): the POST answers `202` at once with a running marker (`status: running`, `started_at`), which is the harness's `last_test` until the result replaces it; a second POST while the marker says running, on any api replica, returns that marker and starts nothing (the start claims the run under a lock on the harness row); a marker older than fifteen minutes is a lost run, which the Harnesses page offers to test again and a start replaces; `GET /admin/harnesses/{name}/test` reads the stored test (`running`, `finished` or `not tested`), and the CLI polls it and prints the result. The last result is kept on the harness (`last_test` in `GET /admin/harnesses`, with `status: finished` and `started_at`) and shown on the Harnesses page, whose row reads running and refreshes until the result lands. A check, not a change: no reason is asked for. The probe it runs is recorded as a probe is. The script harness (a test fixture, 18) routed to a local endpoint makes that one call itself, which is how the kind tier proves the path against a stub model server |
| validate a credential | `POST /admin/credentials/{harness}/validate` | `credentials validate --harness` | shape check of the named auth files, then the bounded probe (below); returns state, timestamps, and the probe's `conclusive` and `cause`, never a verdict the run did not support |
| set the Hermes API key | `POST /admin/credentials/hermes/set` | `credentials set --harness hermes` | reads the value from a password field (the Local gateway page, with the URL) or stdin, atomically writes `api-key` mode 0600 (on Kubernetes, into the `crucible-harness-hermes` Secret the service owns, creating it when absent; ADR 0015), returns no value, and audits only `credential set`; immediately probes readiness without auth and models with auth. Every view reports only `key_set` |
| bounded auth probe | `POST /admin/credentials/{harness}/probe` | `credentials probe --harness` | launches the promoted worker image with the credential mounted, runs a one-line prompt with a 120 s timeout, records exit class, conclusiveness and cause, harness version, image digest, whether auth files changed (by hash), mount mode and duration, removes everything; never shows output beyond the exit class |
| onboard a credential | `POST /admin/credentials/{harness}/login` (starts) | `credentials login --harness` | interactive flow below; the service runs the login in the promoted worker image (a container on Docker, a Job on Kubernetes) and the CLI retains its local-host mode. Refused while an attempt of that harness holds its credential (12) |
| rotate or replace a credential source | `POST /admin/credentials/{harness}/rotate` | `credentials rotate --harness` | the operator's prepared directory is shape-checked, copied in, and left exactly as it was found; the swap is two renames; the previous directory is retained for `credential_retention_hours` then shredded; a failed swap rolls back; every step an event. Directory-held credentials only: where the credential is a Secret (Kubernetes, ADR 0015) it refuses and names the Secret, and a login with `replace` is the replacement |
| remove a credential | `POST /admin/credentials/{harness}/remove` | `credentials remove --harness` | harness becomes `absent`; the directory is shredded at once rather than retained, because the operator said remove, and the harness is disabled with that reason. Directory-held credentials only, as rotate |
| list images, promote, roll back | `GET /admin/images`, `POST /admin/images/{digest}/promote`, `POST /admin/images/rollback` | `images list`, `images promote DIGEST --harness`, `images rollback --harness` | 13, ADR 0018: promotion is per harness (the operator's decision of 2026-09-25). The list adds `defaults`, one row per harness with its current image, its previous image, and the images it may be promoted to: those that carry it at a version inside its adapter's range, release versions and `latest`, never a `ci-*` proof tag. A promotion names the harness and moves only that harness, refused when the image does not carry it inside the range; the image it replaces becomes the harness's previous image. Rollback swaps a harness's default and previous image and moves no other harness. Launches, the probe, the harness test and a login run the launching harness's own default |
| provider health | `GET /admin/providers` | `providers status` | |
| GitHub health | `GET /admin/github`, `POST /admin/github/check` | `github status|check` | check mints a token per registered repository and discards it |
| create the GitHub App | the GitHub page only | none | crucible#168: GitHub's App manifest flow, a browser round trip by GitHub's design. Create GitHub App records a start (a single-use `state`, good for 15 minutes, stored as its sha256 and bound to the administrator and, through a `SameSite=Lax` cookie, to their browser) and posts the manifest to GitHub's create page for the operator's account or a named organization: the name (default `Hades-` and six hex characters, editable), spec 23's permissions exactly, no events, the webhook off, `redirect_url` and `setup_url` on the external URL. GitHub redirects the browser to `/ui/github/callback`; the state is spent in its own transaction before the code is exchanged once (`POST /app-manifests/{code}/conversions`), and the App ID, key and webhook secret go to the service-owned store (ADR 0017). A missing, reused, expired, another administrator's or another browser's return is refused and recorded as `admin_refused`; GitHub is not asked, and the last two leave the start unspent. Audited as `github_app_manifest_started` and `github_app_connected` (`via: manifest`, the key's fingerprint, never the key). After an install GitHub sends the browser to `/ui/github/installed`, which rebinds and lands on the picker on Repositories (crucible#265). This is the only way to connect an App: there is no form, API route or CLI verb for an existing App's id and key (the operator, 2026-09-27); Replace the App creates a new one the same way |
| show or set global automatic merge | `GET`, `POST /admin/delivery/auto-merge` | Settings page (`/ui/settings`) | Hades #337 and #382: one live deployment switch, `enabled` (a JSON boolean), default true. A POST saves the `delivery.auto_merge` provider setting and records `auto_merge_updated` with the administrator, optional reason, and before/after values. No restart or live supervisor lease required. Every merge reads it again immediately before sending the GitHub request; requests already sent cannot be cancelled. Disabled leaves ready tasks for an operator. Re-enabling preserves policy opt-outs. No hold window |
| show or set the external URL | `GET`, `POST /admin/github/external-url` | `github external-url`, `github set-external-url --url` | crucible#168: the `github.external_url` setting, the address GitHub sends the browser back to. Unset (the default, or a save of an empty URL), the flow uses the `Origin` of the operator's own form post. An http(s) origin with an optional path; no credentials, query or fragment. Audited as `github_external_url_updated`. On the GitHub page under Return address |
| pick repositories | `GET /admin/github/installations`, `POST /admin/github/repositories` | `github installations`, `github add-repository` | crucible#120: each installation's repositories grouped by account, each marked with the name it is registered under; a pick registers it with the installation id, the clone URL and the default branch GitHub reports, refusing a repository the installation does not cover or one that is archived (listed and marked "archived: cannot take a pull request"). A private repository is registered as private once the App has minted and revoked a read-only checkout token for it (ADR 0019). The free-text `register a repository` stays for anything the picker cannot show, with a private checkbox (`--private`, `"private": true`) that makes the same check. In the UI the picker is on Repositories, not GitHub (crucible#265): each installation's list is paged (25 to a page), sorted by name or registered first, and filtered by name, registered, private and archived, with counts of what the installation covers, what is registered, and what is registered against it but no longer covered. Ticked repositories, or with select-all every one the filter matches on every page, are registered together under one policy, one attestation and one reason; the result names each one registered and each one skipped with why (already registered, archived, or unsupported: not covered, its name taken, or a private checkout refused). The complete batch result is stored in an audit event in the registration transaction; the redirect carries only its event reference, and the submitting administrator can reload the result on Repositories. A refusal of the batch's own (the policy's attestation) registers none |
| register a repository | `PUT /admin/repositories/{name}` | `repositories register` | 04; an administrative mutation like any other, guarded and audited here, with the previous registration as the before summary. 04's own `PUT /repositories/{name}` is a different, non-administrative surface |
| audit | `GET /admin/audit?cursor=` | `audit tail` | admin events only; the cursor is the scan position and always moves forward, so a long stretch of non-administrative events cannot strand the pages behind it |
| set and test the local gateway | `GET`, `POST /admin/gateway`, `POST /admin/gateway/test` | `gateway show`, `gateway set --endpoint-url [--key]`, `gateway test` | crucible#119: the gateway URL and the Hermes key in one step, then the Hermes probe (readiness, then `/models` with the key), reported in plain words naming the URL and the model count (`Gateway <url> reachable, key accepted, N models.`). A failed test still saves. The URL is the local model entries' `endpoint_url` once one exists and the `local.gateway` provider setting until then; a save writes both |
| pick the gateway's models | `GET`, `POST /admin/gateway/models` | `gateway models`, `gateway pick --enable --disable --thinking --capability` | crucible#121: the models the key can see beside the local entries in force, one row each; a save asks the gateway again and creates or updates the local model entries (enabled, thinking, capability) in new routing and delivery policy versions. A model the gateway does not offer cannot be enabled (refused by name); an enabled entry the gateway no longer offers is disabled with that reason, not removed |
| set the Hermes run limits | `GET`, `POST /admin/gateway/hermes-limits` | `gateway limits [--max-turns N] [--context-length N] [--max-output-tokens N]` | FDY-0140: the model turns one Hermes run may take (default 300, 10 to 5000) and the context window Hermes is told the gateway model has (default 131072 tokens; 0 lets Hermes find it; otherwise 64000 to 2000000). Hades #388: the response allowance the gateway reserves out of that window (`max_output_tokens`, default 32000, 1024 to 1000000 and below a nonzero context length; optional on the API, absent keeps the saved value). Each attempt records the context length, allowance and its routing entry's thinking setting it was launched with (`effective_settings` on the attempt). Kept as the `harness.hermes` provider setting, read at every launch; a run already going keeps its limits. The Local gateway page shows them and, for an administrator, a form to change them. Audited as `local_gateway_updated` with `change: hermes_limits` |
| show or update the local endpoint entries | `GET`, `POST /admin/routing/local-endpoint` | `routing local-endpoint`, `routing set-local-endpoint` | edits the endpoint URL, enablement, thinking preference, and pool concurrency of local model entries that already exist, by creating new immutable routing and delivery policy versions; atomically regenerates and reloads the proxy configuration. The Local gateway page is the UI for it |
| show or update the routing order and demotion | `GET`, `POST /admin/routing/preference` | `routing preference`, `routing set-preference` | ADR 0028, per tier of the routing policy in force: the pools routing tries first, in order (`tiers: {tier: [pool, ...]}`, or null for the default: the local pools first for `trivial` and `standard`), and the demotion settings (`rotation`: `quality_feedback`, `quality_window`, `demote_failure_percent`, `demote_min_sample`, `probe_after_minutes`). A save writes a new routing version and a delivery policy version naming it; a tier or setting left out keeps its value, and an unknown pool or tier, a value that is not a JSON integer or boolean, one out of range (05b), or a save that changes nothing is refused. The view says which tiers read the default. The Routing page shows the order and demotion in force and edits both |
| show or update the per-command timeout | `GET`, `POST /admin/limits/command-timeout` | `limits command-timeout`, `limits set-command-timeout` | 05b `limits.command_timeout_ms` of the policy in force: min, max and default in milliseconds. A save writes a new policy version with only that limit changed; a bound left out keeps its value, and one that is not a JSON integer, or bounds out of order, are refused. Tasks whose contracts name the new version launch with it (issue 128) |
| show or update the advisory gates | `GET`, `POST /admin/gates/advisory` | `gates advisory`, `gates set-advisory --gate NAME ...` | ADR 0024, 05b `gates.advisory` of the policy in force: which pre-PR gates are advisory (a failure goes to the reviewer) and which block. A save states the whole advisory set and writes a new policy version with only that list changed, audited as `policy_uploaded` with the reason; a gate that is not pre-PR, or `internal_review_recorded` or `no_secrets` (always blocking), is refused. `commit_policy` (FDY-0143) and `report_present` (hades #498) are always advisory: the view lists them as advisory and names them under `always_advisory`, and a save that includes either drops it rather than storing it, so the view's own list saves back. Making a gate outside the default set advisory is recorded as an operator decision (from the local CLI, on the upload event only). On the Routing page under Advisory gates |
| show or update the Kubernetes egress selectors | `GET`, `POST /admin/kubernetes/egress` | `kubernetes egress`, `kubernetes set-egress` | 26: the `kubernetes.egress` setting, the cluster resolver's and an in-cluster local endpoint's namespace, pod labels and port. The settings file seeds it and a save wins over the file; the response says which (`source`). Refused naming the field when a selector is empty, malformed, or names the workers or Crucible namespace. A save states both halves (`dns` and `local_endpoint`); a missing one is refused rather than read as off. The supervisor reads a save back within 15 seconds without a restart, and the readiness canary runs again before a launch uses it (crucible#91) |
| show or update the Kubernetes short-role timeout | `GET`, `POST /admin/kubernetes/timeouts` | `kubernetes timeouts`, `kubernetes set-timeouts --role-seconds N [--api-retry-seconds N]` | 26: the `kubernetes.timeouts` setting, `role_timeout_seconds`, how long the bundle verifier, the cleaner and the Job that readies a claim for the publisher may run once their Pod is Running (the image pull and scheduling count against the launch timeout instead). A whole number from 10 to 3600; anything else is refused naming the field. The settings file's `kubernetes.role_timeout_seconds` (120) seeds it and a save wins; the response says which (`source`). The Routing page shows it and edits it. Every process reads a save back within 15 seconds (the lab findings of 2026-09-29) |

The Kubernetes timeout command also accepts `--api-retry-seconds` for the pre-launch
transport retry budget: a whole number from 1 to 600 seconds, default 60. Omitting
the flag preserves the current budget. Local and remote commands save it with the
reason and audit event; the saved value applies at the next launch without a restart.
Generated `next` actions include the current retry budget. The Settings page shows
its effective value, source and when it applies.

Every mutation records the principal. Only the mutations that hand the
supervisor work or take away something a running worker may be using
(committing a bootstrap import, rotating a credential, removing a
credential) are refused when the supervisor lease is not held by a live
instance; every configuration write (a token, a repository, a harness
enablement, the gateway, the GitHub App, an image promotion, a policy or
runtime setting, a login) proceeds while the supervisor misses a tick, and
the supervisor reads it when it next runs. This is the operator's direction
of 2026-09-29 ("stop micromanaging"; hard failures only where the damage is
real): until then every mutation waited on a live supervisor, so one missed
tick refused every administrative change. A refusal is still recorded as
`admin_refused`. A reason is an audit note the operator may leave out; the event
records who acted, when, and what changed whether or not one was given. It
is required only where the operation is destructive or hard to reverse:
revoking a token, removing a repository, removing a credential, and
committing a bootstrap import. A read-only check (the GitHub connectivity
check, a harness test, a gateway test) never asks for one. This is the operator's decision of
2026-09-25 (crucible#117, "I shouldn't have to provide a reason for
everything"); the API, the CLI and the UI apply the same rule, because they
call the same guard. One guard applies the lease rule (where the operation
needs it), the reason rule, and the secret-shape check below, so no operation
can carry only one of them, and they bind every state-changing row of the
table above without exception: registering a repository and finishing a login
are mutations too,
and a login's completion writes `session_compatibility` and clears
`last_validated_at`, which is exactly the kind of change the rules exist
for. Where a reason is required, it is a string an operator wrote: an
absent, blank, or non-string body value is not one. The CLI takes `--reason`
before or after the verb.

A refusal is itself recorded, as an `admin_refused` event naming the
operation and why it was refused, written through a unit of work of its own
because the refusal ends the caller's transaction. Recording it is best
effort: a refusal is never made worse by a failure to record it.

The reason and the rest of a mutation's payload are served back by `GET
/admin/audit`, so the whole assembled payload is scanned before it is
written. A secret-shaped reason is refused with 422 naming the pattern and
never the value, rather than being stored or silently redacted, so an
operator who pasted a token into a reason field learns that it landed
nowhere (12).

## The bounded probe: conclusive or inconclusive

The probe is not a special path. It builds the same container an attempt
builds (the promoted worker image, the per-attempt credential copy of 12,
the egress proxy with an empty task allowlist so only the adapter's own
endpoints are reachable, the same CPU and memory bounds, 120 s), runs the
adapter's own launch for a one-line prompt, and classifies the exit through
the adapter. Its model is the cheapest enabled model for that harness in the
routing policy in force (05b); a harness that takes a model flag and has no
enabled model is a refusal that says so, never a guess, because the CLIs
reject an unknown model id. When a daemon carries several images labelled with
a harness and none of them is promoted, the probe refuses and says to promote one.
Stdout and stderr tails are classified and dropped. Everything the probe
created is removed on every path, including a failure before the credential
is seeded.

The probe's record carries `conclusive` and, when it is not, a `cause`.
Only two outcomes say anything about the credential: a run that completed,
and a run whose tail the adapter read as an authentication failure. Only the
shape check failing or that observed authentication failure sets
`last_auth_failure_at` and makes the state `invalid`. Everything else (a
timeout, a crash, a blocked or lost run, a quota refusal, or a provider error
that means the run never started) is inconclusive: it records itself with its
cause, leaves the credential's state exactly as it was, and comes back from
`validate` as `validated: false, conclusive: false, cause: "<cause>"` rather
than as a verdict. A provider that refuses to answer is a record with a cause,
not a server error.

The reason for the split is operational. The probe is bounded, and a bounded
run that ends for any reason other than the provider's answer is evidence
about the run, not about the credential. Under a rule where any unsuccessful
probe means `invalid`, a loaded daemon or a slow model response would condemn
a working credential and take that harness out of service on latency, and the
operator reading `invalid` would go looking for a revoked token that does not
exist. The 120 s bound stands; conclusiveness is what makes it safe.

Hermes uses the same result model with a bounded HTTP probe rather than a model turn.
It first calls `/health/readiness` without a bearer so endpoint reachability is distinct
from authentication, then calls the configured `/v1/models` with the saved bearer. Its
detail is a plain sentence that names the URL it used and what it proved, for example
`Gateway https://llm.example/v1 reachable, key accepted, 3 models.`, and an unset URL is
`endpoint_not_configured`, never a pass (crucible#119). No response, log line, or event
includes the bearer. The key-paste transaction emits only
the `credential_set` event even though it also updates the sanitized credential state.

## Runtime settings

A runtime setting has one declared name, seed, default and application boundary. A
saved value is a `provider_settings` row with the administrator's reason. Its audit
event records the principal, reason, prior source and value, and saved source and value.
All runtime callers use the common resolver: saved wins, then an environment or file
seed, then the application default. Settings shows the effective value, `saved`,
`environment`, or `default` as its source, and when a change applies.

`credentials.<harness>.mount_mode` is edited on Credentials and applies at the next
launch. The three modes are `ro`, `rw-narrow`, and `renewer`; the harness declaration
may forbid a mode. Read-only declarations, including Claude Code, refuse both
`rw-narrow` and `renewer`; Codex allows `renewer` and its serial `rw-narrow` fallback.
A running attempt keeps the mode captured when it launched. Harness
enablement and its reason are edited only on Harnesses and apply immediately. Their old
environment entries remain seeds for upgrade compatibility, not deployment controls.

`rooms.idle_timeout_minutes` (hades #208, 28) is how long a warm room runner waits for a
message before it exits and its room goes idle: default 30, seeded by the settings
file's `rooms.idle_timeout_minutes`, saved from Settings by an administrator (the
`room-idle-timeout` action, 1 to 1440 minutes), recorded as `room_idle_timeout_updated`,
and applied at the next runner launch.

## Harness enablement: a configured default, then the administrator's decision

Enabling a harness is an administrator's decision the service stores (hades
#174, ADR 0021). Two things feed it, each with its own reason string:

1. **Seed**, the starting value: the built-in `[harnesses.<name>]` value, or an
   environment entry retained for upgrades. This is visible as a seed on Settings. It
   is not a deployment setting.
2. **The administrator's decision**: the harness's row, which `harnesses
   enable` and `harnesses disable` set through the admin API, the CLI and the
   Harnesses page. The row records that an administrator decided
   (`enabled_decided`, migration 0027).

Until an administrator has decided, both have to say yes. Migration 0027
records a disable as a decision only when a principal made it (the row's
`updated_by`); a row a migration seeded off (Codex, 0008) starts undecided
with its runtime flag on, so the configuration default governs it. With the
shipped configuration an upgrade changes no harness's availability. Once one has, the stored decision alone
decides: enabling a harness the configuration keeps off is one action, it
takes effect for routing at once with no restart (the row is read on every
submit and launch), and changing the configuration afterwards changes
nothing for that harness. The configuration's reason stays visible as a
`warning` (in `GET /admin/harnesses`, in the enable and disable answers, on
the Harnesses page, and in the `harness_enabled` or `harness_disabled` event
as `configuration_warning`), never a lock; the harness test (crucible#118) is
how the operator proves an unverified harness works. A refusal names what
refused and the reason it carries: `off by the configuration default` for an
undecided harness the configuration keeps off, `disabled by an administrator`
otherwise. `GET /admin/harnesses` reports `enabled` (the outcome),
`enabled_by_configuration`, `enabled_by_administrator`,
`decided_by_administrator`, `reason` and `warning`, so "disabled" is never
ambiguous about who disabled it. A refusal is terminal for that attempt: it ends as `environment` with a
`harness_refused` event and a `harness_unavailable` wake, and the retry
rule skips it, because the same refusal would come back (16). A harness
disabled at the time a contract is submitted is a contract problem then
(05b), not a refusal a task discovers later.

## Credential onboarding workflow

`crucible admin credentials login --harness <claude_code|codex|agy>`:

0. **Where it runs.** The API and UI run the harness CLI in that harness's
   promoted worker image. The container has a TTY attached directly to the
   service, uses the worker egress proxy, mounts only that harness's subpath of
   the Crucible credential volume read-write, mounts no workspace, disables
   Docker's log driver, and is removed on completion, cancellation, or timeout.
   This keeps a one-time token out of Docker logs while allowing the service to
   capture it to the named credential file. The CLI keeps local-host mode for
   an operator workstation that already carries the executable.

   **On Kubernetes** (26, ADR 0015) the login is a Job in `crucible-workers`,
   `login-<harness>-<id>`, from the promoted worker image: no workspace claim, no
   credential mounted, a memory-backed home the CLI writes into, and a
   NetworkPolicy for the adapter's `login_endpoints` only, which never include
   its model API (crucible#58). The Job needs the namespace readiness probe to
   have passed, as a worker does. A driver in the Pod runs the CLI under a
   pseudo-terminal 4096 columns wide, so nothing wraps, and renders its output
   line by line, as a terminal would show it, before it reaches the Pod log:
   every trailing carriage return goes and then only what follows the last
   one is kept, a cursor-column move is a space (Claude Code's Ink separates
   words with them), terminal control codes are stripped and an OSC 8
   hyperlink leaves its visible URL (hades #173); the token Claude Code prints once
   is written to `oauth-token` mode 0600 and replaced by
   `[captured to oauth-token]`, and a pasted code is masked where the terminal
   echoes it. The service reads that log for the URL, the device code and the
   prompts, exactly as the Docker flow reads its TTY. A pasted code goes in over
   exec stdin, never in an argv or an environment. When the CLI exits the
   service reads the declared auth files back over exec (never through a log),
   shape-checks them, re-checks that no attempt has come to hold the credential,
   and only then writes the harness Secret whole, creating it and labelling it
   as the service's own when it is absent. It then deletes the Job and the
   policy. A login that is cancelled, times out, or whose files fail the check
   leaves the Secret exactly as it was; files that pass are stored whatever the
   CLI's exit code, as a Docker
   login leaves in its directory whatever the CLI wrote. The Job carries its own
   deadline and a TTL, so a login whose api process died still goes away.
1. Create or select the dedicated Crucible credential directory for the
   harness under the configured credential root (`credentials.<harness>.path`,
   mode 0700, owned by the Crucible service user; 13 for who creates it). A
   directory that still passes the shape check is **not** overwritten: the
   login refuses unless the caller explicitly asks to replace it, and a
   replacement retires the existing directory under rotation's retained name
   so the retention sweep shreds it on schedule. Silently truncating a
   working credential is the one thing rotation is careful about, and login
   is held to the same rule. On Kubernetes the same refusal applies to a
   Secret that passes the shape check; nothing is retired, because the Secret
   is only replaced once the new files have passed.
2. Run that harness's supported interactive login with its configuration
   and home variables pointed **directly** at that directory: Claude Code
   with `CLAUDE_CONFIG_DIR`, Codex with `CODEX_HOME`, AGY with its config
   directory variable (recorded by the adapter's `credential_spec`). The
   operator's daily-use directory is never read, copied, or referenced.
3. Complete browser or device authorization with the operator. Where the
   CLI offers a device-code flow the command prints the URL and code so the
   operator can finish from any machine; otherwise it prints the browser
   URL.
4. Validate the resulting credential structure: the named auth files
   exist, parse, and carry the expected fields; nothing is printed.
5. Run the bounded auth probe in the hardened worker image (`credentials
   validate` after the login is finished). On Kubernetes the probe is a worker
   Job on a claim of its own with the per-run copy of the Secret, under the
   worker's egress rules and the namespace readiness gate, labelled
   `crucible.admin=probe` so the supervisor neither sweeps nor adopts it.
6. Record only: validation result, `mount_mode`, file names and sizes,
   harness version, image digest, timestamp, and whether the auth files
   changed during the probe (which sets `refresh_requires_rw`).
7. Report whether the credential requires writable refresh state and set
   `mount_mode` to the stricter of the adapter's declared minimum (07; all
   three harnesses declare `rw-narrow`) and what the probe observed
   (`rw-narrow` when the probe changed files). A probe that changes nothing never
   lowers the adapter's minimum.
8. Remove all probe resources.
9. Mark `session_compatibility: unverified` until the daily-session
   compatibility test (21, S1b) passes for that harness, Claude Code
   included; a harness is not enabled for normal workers before that.

"Dedicated Crucible credential" means dedicated authentication state for
Crucible, not necessarily a separate subscription or account. Whether a
provider allows several simultaneous authenticated sessions on one
subscription account is documented per harness in 07 once S1b establishes
it; until then the field reads "unverified".

The device or browser code the CLI prints has a window, and the flow states
it up front rather than leaving the operator to discover it: Codex's device
code lasts fifteen minutes, AGY's sixty seconds, and Claude Code's
`setup-token` takes the pasted code and prints the long-lived token, which
the driver captures into the credential file mode 0600 and never displays.
The CLI's own output is not echoed.

Each harness's paste prompt is recognised from what its CLI actually prints,
captured from the pinned worker image (tests/fixtures_data/logins, hades
#173): Claude Code's `Paste code here if prompted >`, and AGY's `Or, paste the
authorization code here and press Enter:`. Codex's device flow asks for
nothing; the page shows its URL and one-time code and the CLI finishes on its
own once the operator has entered the code. Whatever a CLI's prompt, one that
has printed its sign-in URL and then nothing for five seconds is waiting, and
the page shows the code box. The Docker, local and Kubernetes paths render
the CLI's output by the same rules. A pasted code is ended with a carriage
return, which is what the Enter key sends: Claude Code reads raw and submits
only on one. The Login page lists what the operator does for that harness;
for AGY that after sign-in the browser ends on an address that does not
load (on the lab on 2026-09-27 a localhost one), and that the code is the
`code` value in that address bar (the whole address may be pasted, and a percent-encoded code is
decoded). AGY stops waiting sixty seconds after printing its link and exits
("authentication timed out"); the page shows the local time it stops, the
login then ends with that said plainly, and **Start a new login** runs a fresh
one, since a code from the old link no longer works. A login that has ended,
either way, can be started again from its page.

A login and an attempt of the same harness never overlap (12). The login
refuses to start while an attempt of the harness is between `preparing` and
the removal of its synced copy, and a launch of that harness is deferred, not
failed, while a login for it runs. On Kubernetes the login Job is what the
supervisor sees; on Docker it is the login container.

Only one login of a harness runs at a time. On Kubernetes that holds across api
replicas: a second start, from any replica, is refused naming the holder of the
harness's login lock (26), and the lock of an api that died is taken over once
the login's deadline has passed. On Docker the api is one process and its own
record of running logins is the lock.

The local gateway panel is the runtime authority after migration. An environment value
may seed version 6 on first migration, but later restarts read the active database
policy and do not overwrite it. Each save creates a new routing-policy version and a
new delivery-policy version that references it, writes the Squid configuration from the
same document, and updates the running Docker provider's exact host-and-port allowlist.

Every routing publish (the Local gateway page, the local endpoint entries, a model's
availability, a tier's rules, the routing order) computes its delta against the version
in force (hades #437): the models it enables or disables, each pool whose
`max_concurrency` changes, each tier whose pool order changes, and the delivery
policies and projects that follow routing unpinned and so receive it. A publish that
enables or disables a model or changes a pool cap is refused (a problem response on
`reason`) without a reason, which names the decision it supersedes; such a publish
raises one `routing_changed` wake listing the delta. The `routing_policy_uploaded`
event records the delta and the note. The Routing page lists each routing version with
what it changed, who published it and the reason. The Local gateway page says that a
save publishes for every unpinned project, and a model save shows the delta and asks
for confirmation before it publishes. Pinning routing per policy is not part of this.

A routing publish also names the entries it overrides (hades #606): each entry
(`harness:model`) whose enabled flag, weight, pool or tier membership (the tiers whose
`allowed_capability` includes its capability) differs from the version before it, and
each entry added or removed. A page publish carries them in its delta; `PUT
/routing/{name}/{version}` compares with the highest stored version below it and
returns `previous_version`, `overrides` and the `reason` given, and its
`routing_policy_uploaded` event records the first two. The Routing page's versions list
shows them under "What changed" beside the reason, computed from the documents, so a
version uploaded before this shows them too.

## Policies (hades #606)

`/ui/policies` is the Admin policies page, for administrators only (anyone else gets
403 and none of the document). It lists every delivery policy with the version in
force (the newest not retired) and, for the policy shown, its versions newest first
with who published each and the reason. It shows the version in force in groups:
routing (`routing.policy` name, version, pinned), services (one block per service
kind, with a "declared" box), gates, network allowlist (`network.egress_allowlist`),
limits and concurrency. The edit form lists the same groups as plain inputs, lists
comma separated, with one reason box, the publish reason, which is required. Publishing
writes the next version of the policy, built on the version in force, through
`put_policy`, the service behind `PUT /v1/policies/{name}/{version}`, so it validates
the same way and its `policy_uploaded` event records the administrator and the reason.
An edit that changes nothing is refused. Each version opens against the version before
it, one row per changed setting with its old and new value. Times are local Central
time. The page is registered without a navigation link; the navigation task adds it.

## Rotation and removal

Rotation takes a directory the operator prepared themselves. Crucible
shape-checks it, **copies** it into the configured credential root as a
staged directory, renames the live directory aside as the retained one, and
renames the staged copy into place. The operator's source is left exactly as
it was found, and the response and the event say so: a mistyped source path
can no longer cost the operator data, and nothing Crucible destroys is
outside the configured credential root. Even inside that root nothing is
destroyed at once: the retained directory goes on the retention schedule
(`[admin] credential_retention_hours`, 24 by default) and the supervisor's
retention tick shreds it when it is old enough.

The swap is two renames, so it can fail between them. When it does, the
retained directory is renamed back, the staged copy is shredded, the failure
is recorded through a unit of work of its own (the caller's is about to roll
back), and the refusal says that it was rolled back. A rotation that fails
leaves what it found, and never a configured path with nothing behind it.

Removal is the exception to retention: the directory is shredded at once,
because the operator asked for the credential to be gone, and the harness is
disabled with `credential removed: <reason>`. Running attempts finish, as for
any disable. Shredding is complete or it says so (12).

## Administrative interface

The Status page's "Before a task can run" list is the status document's
`readiness` part (crucible#123), so it can never disagree with the pages it
links to. Where a field refers to something the system knows or can discover
(the gateway's models, the App's installations and repositories), the page
offers the valid values rather than a free-text field (crucible#121).

The `/ui` interface consumes, without any other data source: harness status;
credential onboarding and validation status (including an in-progress
login's device URL); worker-image versions and promotion state; provider
health; GitHub App and repository connectivity; active workers; failed or
blocked tasks; pending Foundry wakes; retention and cleanup status; audit
events with cursors. A portal that combines Foundry chat, task and Kanban
views, Crucible execution views, and this administrative surface is a
client of the two services and owns none of their state; it never
collapses the authority boundary between them. Process settings are shown
read-only with their effective source because they bind ports, sockets,
storage, startup timing, and secret-bearing paths at process start. Runtime
policy and state remain editable through the application services.

A fresh migrated database with no administrator receives one
`first-run-admin` principal. Its token never reaches stdout, stderr or any
log, because a log stream is shipped and retained by whatever collects it
(crucible#122, ADR 0016). The migration writes it, before it commits the
principal, to one private place: on Kubernetes the Secret
`crucible-first-run-admin` (key `token`) in the service namespace, which only
the migrate Job's account may create and only the api's account may delete;
on Docker the file `first-run-admin-token`, mode 0600, in the credential
root. Its stderr says only where the token is. A token it cannot write is
never minted, and with neither place configured it mints nothing and says how
to create an administrator with `crucible admin --reason ... token create`. Only the salted
token hash is stored. The sign-in page names the place for the running
deployment. The api removes the Secret or file when the first-run principal
first signs in at `/ui`, or when it is revoked. Principal names starting
`first-run-admin` are reserved for the migration, so `token create` refuses
them. A later migration run finds an active administrator and does nothing.

A principal is renamed with `POST /admin/tokens/{principal_id}/rename`
(`crucible admin token rename ID NAME`, or the Rename form on the Tokens page),
reason required (ADR 0029). Its tasks and token follow it by id; events keep the
name they were written with. A taken or reserved name is refused, and the
first-run principal keeps its name.

## What Foundry may do

Read `/v1/admin/status` parts that its role permits (orchestrator role
gets `harnesses`, `providers`, `github` health, `workers`, `tasks`, `wakes`
read-only through `GET /v1/capabilities`), and report to the operator that
a harness is disabled, a credential is invalid or unverified, or a provider
is unavailable. It never calls the mutation endpoints.

`GET /v1/capabilities` is the status document's `harnesses`, `providers`,
`github`, `workers`, `tasks` and `wakes` parts, with two reductions and
nothing else removed:

- each harness keeps both enablement gates and their reasons, its supported
  range, its concurrency limit and use, its last launch outcome, and the
  images known for it with their references, versions, digests and promotion
  states, but its credential is reduced to `state` and
  `session_compatibility`. No path, no fingerprint, no auth file name, no
  expiry, no validation timestamp;
- `github` is reduced to whether an App is configured, whether its key is
  present, and per registered repository whether the installation covers it.
  No App id, no key fingerprint, no mint or failure timestamps.

`workers`, `tasks` and `wakes` are the status document's own parts, not
counts: the active attempts with attempt and task ids, external id, state,
harness, model, image digest, start time and last heartbeat; task counts by
state together with the id, external id and update time of every task in
`blocked`, `pre_pr_gates_failed`, `publish_failed`,
`ci_certification_failed` and `head_diverged`; and pending wakes as a count
per principal with the oldest pending timestamp and the total unacked. For
an orchestrator principal these three parts are filtered to its own work
(crucible#40): the active attempts and the task counts and lists cover only
tasks it submitted, and `wakes` counts only the wakes addressed to it, so
`unacked` is its own total. One orchestrator cannot read another's task ids,
external ids or wake counts here. An operator principal is exempt, as it is
from task ownership (04), and sees every principal's. The `harnesses`,
`providers` and `github` parts describe the service rather than any one
principal's work and are the same for every caller. That is what an
orchestrator needs to say which of its tasks is stuck and why. Nothing in the response is a
credential, a token, a key, a path, or a file name, and the view is
read-only.

## Codex refresh requests

In renewer mode, **Refresh now** records a `credential_refresh_requested` event with
the administrator's reason. The API does not perform an OAuth grant. The supervisor
handles requests on its next tick, coalescing those already queued into one refresh
and acknowledging them with its persisted request cursor. A transient failure leaves
the request pending for retry without failing the tick; a successful refresh or a
dead login is terminal. The Credentials page shows **Pending**, **Handled**, or
**None** in the Refresh request column, alongside the last refresh result. Handled
means acknowledged, including a dead-login outcome; it does not promise success.

Only the supervisor process owns a grant-capable renewer. API and local admin wiring
expose only `dead` and `last_refresh` status reads. Interactive login still runs in
the API or admin path under the login lock. Replacing its Secret and marking a login
dead carry a `metadata.resourceVersion` precondition; a conflicting write is refused.
A renewal that conflicts with an unrelated Secret edit re-reads and retries the same
fresh tokens under the new version if the stored refresh token is unchanged. A new
login takes precedence over a grant from the previous login (12).

## Main CI release hold

The release path (the tag push, 24) reads the durable `release.main_ci_hold` setting
(`release_held`), and nothing is tagged while it is held. The setting names
the red merge commit, its failing jobs, the pull requests merged since the last green
main, and the automatically opened fix-main task. Green checks clear the hold only for
the held commit itself or for main's verified current tip; a superseded older commit's
green does not (23).

### Qwen Code on Harnesses and Images

Harnesses lists all five adapters: Claude Code, Codex, AGY, Hermes and Qwen Code
(`qwen_code`). Enable Qwen Code and add a local routing model on `lab-local`;
set the gateway key on Local gateway, shared read-only with Hermes. The Images
workflow offers a separate Qwen Code promotion and rollback, using its own
`harness_images` row and the image's `crucible.harness.qwen_code.version` label.
Promoting Qwen Code never moves Hermes or the subscription harness defaults.

The Qwen wrapper writes `model.maxToolCallsPerTurn: 0` before launch to disable
the per-turn tool-call cap, and `model.generationConfig.contextWindowSize` from
the routing model's `context_length` (default 131072). This full engine window
lets Qwen reserve output within the limit; an engine with another capacity needs
its actual value on the routing entry. These settings are independent of the
Hermes run-limit form. See spec 07 for the launch and stream evidence contract.
