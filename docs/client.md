# The `crucible` client: a reference for agents

`crucible` is the one command for Crucible. `crucible serve` runs the service;
`crucible admin ...` is the operator's console (25); the other verbs are the
orchestrator's client of `/v1` (04). Every command except `serve` and `--help`
prints exactly one JSON object on stdout, the envelope. An agent can drive the
tool from the envelope and `crucible --help` alone: read `ok`, act on `next`,
and on failure read `error`.

`crucible schema` prints the envelope's JSON schema and the schema of `data`
for every `kind`.

The web Routing page exposes routing tunables directly. An administrator can enable or
disable each model, order every tier's pools, and choose its allowed capabilities. Each
form takes an audit reason and publishes the next immutable routing version. Delivery
policies follow new versions of the routing policy they name unless
`routing.policy.pinned` is deliberately set. A submitted task keeps its policy
snapshot as recorded, but with an unpinned reference every attempt, a correction's
or a retry's included, routes with the newest published routing version at the
time it is routed, so a model disabled or removed since is never selected. Each
attempt records the routing version it routed with. A pinned reference keeps
its version.

## CI certification

Certification counts every observed non-skipped check run and workflow job on
the accepted head SHA. All must conclude `success` for green; any failure
fails, and queued or running jobs keep it pending. No observed runs is pending,
never green. Check suites do not count; `neutral` results remain pending.
Branch protection and ruleset names do not participate.

`ci_certification.required_checks` defaults to `[]`. Set it only to explicitly
narrow which observed run names count; it does not require unobserved names to
appear. Certification details report counts, such as "9 of 9 jobs succeeded
on <sha>" or "2 of 9 jobs still running". The existing explicit `allow_no_ci`
policy or operator `accept_no_ci` decision can skip absent CI, never pass it.
An `accept_no_ci` waiver requires an empty policy narrowing as well as no
non-skipped runs on the head. A narrowing that names a check that has not
appeared keeps the empty observation pending.

## Configuration

One precedence for each setting: the flag, then the environment, then the
client configuration file, then the default.

| Setting | Flag | Environment | File key | Default |
|---|---|---|---|---|
| base URL | `--api-url URL` (before the verb) | `CRUCIBLE_URL` | `url` | none |
| token | none, on purpose | `CRUCIBLE_TOKEN`; `crucible admin` reads `CRUCIBLE_ADMIN_TOKEN` first | `token_file` (a path) | none |
| time zone for `--table` | `--timezone` | `CRUCIBLE_TIMEZONE` | `timezone` | America/Chicago |

The file is `$CRUCIBLE_CLIENT_CONFIG`, else `~/.config/crucible/client.toml`
(`$XDG_CONFIG_HOME` is honored). It takes those three keys and no others:

```toml
url = "https://crucible.example"
token_file = "~/.config/crucible/token"
timezone = "America/Chicago"
```

The token never goes on the command line, where every process on the host
could read it. A base URL with credentials in it (`https://user:pass@host`)
is refused, because the URL is echoed into `next`.

`crucible admin` runs in process against the server's own configuration
(`--config`, the server TOML) unless it is given `--api-url URL` or
`--remote` (the URL from `CRUCIBLE_URL` or the file); then it calls the API
with the token. `migrate` is local only.

### Board

`GET /v1/admin/board` is an admin-only, read-only view of the same data as the
operator page at `/ui/board`. It returns `in_flight` groups, the `kanban`
(hades #334: `columns` left to right, each with `key`, `name`, `reserved`,
`note` and `parents`, every parent holding its `tasks` as cards with `holder`,
`age` and `queue` (hades #424: in Queued, `position` in queue order and the
approval `batch` when there was one; the column is listed in queue order), and `thresholds` with the attention and done windows), current-attempt
`routing`, per-attempt and rolled-up `tokens`, and the 14-day `quality` log.
A card's `age.late` is true past the column's threshold; `age.budget_seconds` is
null where a column has none. Task and attempt timestamps are RFC 3339 in JSON.
The HTML page renders them in the configured operator timezone.

Token values are reported only when a harness adapter supplied them. An attempt
whose adapter supplied no usage has `recording: "not recorded"` and null token
values. Totals count recorded and unrecorded attempts separately and never
estimate missing usage. Merge queue position is null when no stored source
reported it.

## The envelope

```json
{
  "ok": true,
  "envelope": "1",
  "kind": "task",
  "state": "submitted",
  "principal_role": "orchestrator",
  "data": { "...": "the API's record, exactly as the API returned it" },
  "next": [ { "action": "start", "command": ["crucible", "start", "01M3...", "--policy-version", "2", "--reason", "{reason}"], "...": "..." } ],
  "warnings": []
}
```

| Field | Meaning |
|---|---|
| `ok` | `true` when the operation succeeded. |
| `envelope` | the envelope's version, `"1"`. |
| `kind` | names the shape of `data`: `task`, `task_list`, `task_detail`, `wake`, `wake_list`, `health`, and for `crucible admin` `admin_status`, `credential_state`, `credential_report`, `harness_list`, `image_list`, `token_created`, and the rest (`crucible schema`). `error` on failure. |
| `state` | the record's lifecycle state where it has one: a task's state, a credential's state, a harness's `enabled` or `disabled`, a bootstrap import's state. `null` otherwise. |
| `principal_role` | the role of the token in use as the API showed it (below): `admin`, `orchestrator` (which also stands for operator), `observer`, or `null` when unknown or not needed. |
| `data` | the API's response, never reworded. |
| `next` | the actions valid from this state for this principal (below). |
| `warnings` | strings a caller should see, such as why `next` is empty. |
| `error` | on failure only (below). |

Exit code 0 when `ok`, 1 when the operation was refused or failed, 2 on usage
(a bad argument, a missing base URL or token). `--table`, anywhere on the line,
prints a short human view instead, with times in the configured zone; a person
at a terminal uses it, an agent does not.

## Attempt report

`GET /v1/attempts/{attempt_id}/report` returns a `CompletionClaimView` with
the worker's parsed report and Crucible's observations:

| Field | Meaning |
|---|---|
| `attempt_id` | the attempt that was parsed |
| `parsed_ok` | whether the report parsed without errors |
| `parse_errors` | list of dicts with `loc` (path into the document, a list of strings), `msg` and `type` on failure, the pydantic shape; a YAML failure has `loc: []`, `type: "yaml"` and the line inside `msg`. Example: `{"loc": ["risks"], "msg": "Field required", "type": "missing"}` |
| `document` | the parsed report as a dict |
| `filled_by_crucible` | list of field names Crucible filled in |
| `differences` | list of dicts with `field` and `detail` describing mismatches |


## Following `next`

Each entry is one action:

```json
{
  "action": "accept",
  "description": "record the acceptance decision",
  "command": ["crucible", "accept", "01M3...", "--verdict", "{verdict}", "--reason", "{reason}"],
  "needs": {
    "verdict": {"choices": ["accepted", "rejected", "needs_more_work"]},
    "reason": "why; recorded with the operation (never a secret)"
  },
  "optional": [{"flag": "--head-sha", "description": "the head the verdict is for"}],
  "requires": {"roles": ["orchestrator", "operator"], "owner": "foundry"}
}
```

- `command` is the argv. Replace each `{name}` token with a value that
  `needs[name]` describes; where `needs[name]` has `choices`, use one of them.
  Pass the argv as a list; there is no shell quoting to get right.
- `optional` lists flags the caller may append.
- `requires.roles` are the roles the API accepts the action from. It is
  always a set the role in use belongs to; it is there so a caller that holds
  several tokens can tell which one to use.
- `requires.owner`, where the API checks one, is the task's principal. An
  orchestrator may act only on its own tasks; the API refuses another
  orchestrator's attempt. The client cannot learn its own principal's name
  (there is no endpoint for it), so it names the owner rather than guessing.

What `next` offers follows the API's own checks: the lifecycle table (09) and
each endpoint's state guard (an acceptance only in `awaiting_acceptance`, a
correction only in a correctable state, and so on), filtered by the
role the route admits. What a state cannot show is still the API's to decide:
a live supervisor lease for the admin mutations that need one, a review comment that exists,
a prepared directory for a rotation, an import already authoritative when
another verified one is committed. If the API refuses, the refusal comes
back in `error`; read the record again and follow its new `next`.

List kinds offer a read of each item (`crucible task {task_id}`); a wake list
offers `ack` while a wake is pending. `crucible admin` records offer admin
verbs: for a credential, what its state allows (`login`, which runs the
harness's own CLI locally or in the promoted worker image remotely, with
`--replace` when a credential is present); for a harness, the other value of
its enable flag; for an image, `promote` while it is a supported candidate;
for an exhaustion mark, `clear-exhaustion` while it is active; for a verified
bootstrap import, `commit`; for an audit page, the next page. The local
model endpoint offers one `set-local-endpoint:<model_id>` per model, already
set to flip its current enabled state. The Kubernetes egress selectors offer
one `set-egress`, prefilled with the values in force, and the per-command
timeout one `set-command-timeout`, prefilled the same way, and the advisory
gates one `set-advisory` with a `--gate` per advisory gate in force. Their commands carry the
`--api-url` or `--config` the command ran with.

## Principals

The token in use decides what the API allows and what `next` offers. The
client never refuses on its own. It learns the role from the API:

- a verb whose route admits one role class proves the role by succeeding
  (`accept`, `review`, `dispositions`, `ci-decision`, `head-decision`, `close`,
  `republish` admit the orchestrator and operator; every `crucible admin`
  remote verb admits only an admin);
- otherwise, and only when it has a lifecycle record to offer actions on, it
  asks with two read-only requests: `GET /v1/admin/audit?limit=1` (200 means
  admin) and `GET /v1/capabilities` (200 means orchestrator or operator, 403
  means observer). Neither records anything;
- any other answer leaves the role `null`, `next` empty, and a warning says
  why.

An observer is offered nothing. A command run under the wrong principal
returns the API's own refusal in `error` (`forbidden`), not a local guess.

## Handling `error`

```json
{
  "ok": false,
  "kind": "error",
  "error": {
    "code": "transition-not-allowed",
    "message": "republish is accepted only in publish_failed; task is submitted",
    "hint": "the record's state does not permit this; read it again and follow its `next` actions",
    "status": 409,
    "problem": {
      "type": "urn:crucible:problem:transition-not-allowed",
      "title": "Transition not allowed in the current state",
      "status": 409,
      "detail": "republish is accepted only in publish_failed; task is submitted",
      "instance": "/v1/tasks/01M3.../republish",
      "errors": []
    }
  }
}
```

`code` is the slug of the API's problem type when the API answered, so it is
stable across versions; `problem` is the API's RFC 9457 document whole, with
`errors` naming the fields that failed validation. `crucible admin` in local
mode builds the same document from the same service error. The client's own
codes are `usage`, `config` (base URL, token, or client file), `unreachable`,
`protocol` (the server answered outside its contract), `interrupted`, and
`internal` (a defect; the traceback is on stderr). `hint` says what a caller
can do; it is `null` for a code with no general advice.

| Code | What to do |
|---|---|
| `usage`, `config` | fix the command or the configuration; exit code 2 |
| `unauthorized` | the token was not accepted |
| `forbidden` | wrong role for this route, or another orchestrator's task |
| `not-found` | check the id |
| `transition-not-allowed`, `conflict` | read the record again and follow `next` |
| `supervisor-not-live` | this admin mutation (a bootstrap commit, a credential rotation or removal) needs a live supervisor; retry once it runs |
| `request-invalid`, `contract-invalid` | see `problem.errors` |
| `unreachable` | check the base URL and that the API is up |

## Secrets

The bearer token in use is redacted from everything the client prints,
including a problem detail that echoed it. Redirects are refused, so the
token never goes to another origin. The one secret an envelope carries on
purpose is the new token `crucible admin token create` mints: it is that
command's output, printed once and never stored in clear. The first-run
administrator token that `crucible admin migrate` mints is printed nowhere:
it goes to a Secret on Kubernetes or a mode 0600 file on Docker, and stderr
says only where (ADR 0016).

## Commands

```
crucible tasks [--state STATE]
crucible task ID [--events] [--pull-request] [--attempts] [--gates] [--report] [--evidence]
crucible wakes | crucible wakes ack ID --reason TEXT
crucible submit FILE --reason TEXT
crucible start ID --policy-version N [--harness H] [--model M] [--provider P] [--image I] [--effort E] --reason TEXT
crucible accept ID --verdict accepted|rejected|needs_more_work [--head-sha SHA] --reason TEXT
crucible review ID FILE --reason TEXT
crucible dispositions ID FILE --reason TEXT
crucible corrections ID FILE --reason TEXT
crucible ci-decision ID --cause CAUSE --action ACTION --reason TEXT
crucible head-decision ID --action ACTION --reason TEXT
crucible decisions ID --kind K --verbatim TEXT --resolves TEXT [--escalation-id E] [--reschedule] --reason TEXT
crucible cancel ID --verbatim TEXT [--decided-by NAME] --reason TEXT
crucible close ID --reason TEXT
crucible republish ID --reason TEXT
crucible health
crucible schema
crucible admin [--config F | --api-url URL | --remote] [--reason TEXT] VERB ...
crucible serve --all|--api|--supervisor [--config F]
```

Two decision kinds are the operator's way out of a delivery wait (ADR 0025),
with an operator or admin token, while the task's pull request is under
observation. `next` offers both to those roles:

```
crucible decisions ID --kind waive_external_review --verbatim "why" --resolves "the external reviewer did not review" --reason TEXT
crucible decisions ID --kind accept_no_ci --verbatim "why" --resolves "this repository has no CI for this task" --reason TEXT
```

The task page in the UI (`/ui/tasks/ID`, linked from Tasks) has a button for
each.

## External review publication policy

When `external_review.provider` is named, `required_rounds` is above zero, and
`request_on_publish` is true, publication posts `trigger_comment` on the new pull
request as the GitHub App. `request_on_publish` defaults to true. The Codex default is
`@codex review`; repositories whose reviewer starts automatically set
`request_on_publish: false`. Republish checks the pull request's issue comments and the
recorded request, and does not post the App's trigger a second time. Corrections do not
retrigger it.

`crucible admin --help` and each verb's `--help` list the admin verbs and
their arguments. An admin `--reason` goes before or after the verb and is an
optional audit note, except where the operation requires one (token revoke,
token rename, repository remove, credential remove, bootstrap commit, bootstrap
discard); `next` lists it under
`optional` where it may be left out and under `needs` where it may not.

## From the old clients

`crucible-admin ARGS` still works: it prints one deprecation line on stderr
and runs `crucible admin ARGS`. Foundry's `foundry-crucible ARGS` becomes
`crucible ARGS`, verb for verb; the shim in `docs/integration/foundry-crucible-shim/`
keeps the old name. Arguments, API calls, and bodies are unchanged, and a
test holds both old command trees to the new one. What did change:

- output is the envelope, and the old record is its `data`;
- errors are envelopes on stdout rather than text on stderr, and a refusal
  exits 1 (the old admin client exited 2), an unreachable server 1 (Foundry's
  client exited 3);
- the admin client's remote mode refuses redirects, as Foundry's did, so an
  `--api-url` that redirects (http to https, say) must name the final URL;
- the admin client's remote mode takes `CRUCIBLE_TOKEN` or `token_file` when
  `CRUCIBLE_ADMIN_TOKEN` is not set, where it used to stop;
- `crucible admin migrate` writes the first-run administrator token to a
  Secret or a mode 0600 file and prints only where it is (ADR 0016), where it
  used to share stdout with the result;
- a login's "paste the code" prompt is on stderr, and the code is read from
  stdin;
- a `--reason` outside printable ASCII is percent-encoded in the
  `X-Foundry-Reason` header (the body carries it unchanged), where Foundry's
  client failed before sending.

## Later: MCP

An MCP server is a later interface over the same library, not a second
client. `crucible/client/` already holds everything such a server needs and
nothing it would have to strip out: the HTTP transport and its redaction, the
verbs' calls (`crucible/client/orchestrator.py`), the envelope, `next`, and
the JSON schemas `crucible schema` prints, which map onto MCP tool
definitions. Each verb would become a tool whose result is the envelope, and
`next` would become the tools offered from it. Nothing of it is built yet.

### Codex on the Local gateway page

Changing a credential mount mode first runs the Harnesses Test under the requested
mode. A failed test refuses the change with the failing step's detail and keeps the
previous setting. In Codex renewer mode, `/home/worker/.codex` is writable for runtime
state; only the access-token projection and Hades-owned `config.toml` are read-only.
Early credential-directory filesystem failures stop as `environment`, name the path
and mount mode, and do not spend an attempt.

Subscription Codex now uses the Hades credential renewer and can run workers in
parallel up to the policy cap. The Credentials page shows renewer health and offers
administrators a reasoned Refresh now action. Local-endpoint Codex continues to use
the gateway API key and does not use the renewer.

Select Hermes, Codex, or both beside a model returned by the gateway. Saving writes
a new routing version; disabling Codex leaves Hermes available as fallback. The
shared key stays in the Hermes credential store and is mounted read-only for local
Codex. The gateway must support `/v1/responses`, as LiteLLM in front of vLLM does.
The context length under Local run limits applies to both harnesses; max turns
applies only to Hermes. For Codex, a zero context length uses 131072.

The admin API accepts `codex_enabled` alongside `enabled` (Hermes) in
`POST /v1/admin/gateway/models`, for example
`{"models":[{"id":"fast","enabled":true,"codex_enabled":true}],"reason":"local coding"}`.
Omitting either flag preserves that harness's choice. The model-list response includes
`codex_enabled`. The CLI equivalent is
`crucible-admin --reason "local coding" gateway pick --harness codex --enable fast`;
`--disable fast` disables it, and the default `--harness hermes` preserves existing CLI
behavior. Pool max concurrency bounds both harnesses together. The harness must also
be enabled and have a promoted worker image, as for other routing choices.

### Parallel frontier workers

The Routing page shows the active per-harness caps. Policy
`concurrency.per_harness` can allow multiple Claude Code or AGY workers. Claude
Code uses a read-only long-lived setup token with no sync-back. AGY uses isolated
writable copies because Google keeps refresh tokens reusable. Codex permits parallel
workers in renewer mode: Hades holds and refreshes the login, while workers receive
access tokens only. Copy mode remains serial because OpenAI rotates refresh tokens.
Rollback uses `per_harness.codex: 1` and `rw-narrow` mode. The existing `auth_failure`
classification and retry-once behavior remain the safety net.

Watch supervisor warnings for `harness`, `attempt_id`, and `auth_failure_count`.
The count is cumulative per harness in the current supervisor process and resets on
restart. Existing executions keep their policy snapshot. Shipped caps stay unchanged; Foundry sets them after deploy. Writable
adapters without an explicit parallel-safety declaration remain limited to one.
