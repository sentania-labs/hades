# 28. Rooms and the room runner

Hades #208, the principal increment (FDY-0590), and ADR 0031: rooms are conversations
whose transcript Hades owns. This page is the contract for the two tables, the API the
operator's clients and the room's runner use, the runner's launch, its session start,
its tools, and its lifecycle. The room page is a separate task; this is the backend.

## What a room is

A room is one conversation with Hades. There is the principal room, where anything comes
up, and card rooms, each about one card (a task). Both work the same way.

Hades writes every turn to `room_turns` before any harness sees it: the operator's words
when they arrive, the assistant's reply as it streams in, and a system line when the room
changes. The harness session the runner holds is a cache. A runner can be stopped,
reclaimed when idle, replaced by another harness or model, or lost with its Pod, and the
next runner starts from the record.

## Tables (migration 0059)

`rooms`: `id`, `kind` (`principal` or `card`), `card_task_id` (the card's task, only for a
card room), `harness`, `model`, `state` (`idle`, `starting`, `warm`, `interrupted`,
`closed`), `created_at`, `last_activity_at`, `runner_handle` (the runner's Pod or
container while one is up), `session_id` (the harness session that runner holds),
`created_by` (the principal the room acts as), `scope_task_ids` (the tasks its runner
token may act on), `runner_key_salt` and `runner_key_digest` (the salted digest of the
runner's token while a runner is up; the token itself is never stored), `inbox_cursor`
(the last user turn handed to the runner), `pending_control` (`interrupt` or `stop`, for
the runner's next poll), and `runner_seen_at` (the runner's last poll).

`room_turns`: `id`, `room_id`, `seq` (unique per room), `role` (`user`, `assistant`,
`system`), `text`, `tool_calls` (JSON: every tool call the runner's PreToolUse hook
recorded on the turn, with its input and `tool_use_id`), `started_at`, `ended_at` (None
while an assistant turn streams), `interrupted`, and `decision_id` (the ledger line
recorded from this turn's words).

Seven event kinds join the audit: `room_created`, `room_runner_launched`,
`room_runner_stopped`, `room_interrupted`, `room_switched`, `room_closed` and
`room_idle_timeout_updated`. The transcript itself is the room's record; the events say
what Hades did to the room and its runner.

## Lifecycle

```
            message, no runner              runner polls the inbox
   idle ─────────────────────────> starting ─────────────────────> warm
    ^  ^                              │                            │  ^
    │  └──── launch failed ───────────┘                 interrupt  │  │ runner ends the turn
    │                                                              v  │
    │                                                         interrupted
    │
    └── stop: a switch, the idle reclaim, a runner that went away or exited
        (from starting, warm or interrupted)

   close: from any state but closed, to closed, which takes nothing more
```

- **idle**: no runner. A message writes the user turn, moves the room to `starting` and
  launches a runner; only the request that made that move launches one, so two messages
  at once start one runner.
- **starting**: a runner was launched and has not polled yet. More messages queue in the
  record. A runner that has not polled within ten minutes of its launch is taken as never
  started and the room goes back to idle.
- **warm**: the runner polls the inbox. The inbox hands out the oldest user turn not yet
  delivered, and opens its assistant turn, only while no assistant turn is open: one turn
  at a time. A warm runner that has not polled for 100 seconds (four long polls) is taken
  as gone: its open turn ends as interrupted and the next message launches a new runner.
- **interrupted**: the operator stopped the running turn. The runner reads `interrupt`
  on its next poll (within a second while a turn runs), calls `client.interrupt()`, and
  ends the turn as interrupted, which brings the room back to `warm`. With no runner up,
  Hades ends the open turn itself.
- **closed**: the runner is stopped and the room refuses every write with 409.

The **idle reclaim**: a warm room whose last activity (a message delivered, an event of
its turn) is older than `rooms.idle_timeout_minutes` with nothing to answer is stopped.
Hades does it when the runner polls (the poll answers `stop`), on every message to any
room, and when the runner says it is exiting because its own idle timer ran out
(`POST /rooms/{id}/runner/exit`). A stop ends any open turn as interrupted, forgets the
runner's token, its handle and its session, and removes the runner at the provider.

A **switch** writes the system turn `Switched to <harness> (<model>) at <local time>.
Same history, same memory.`, stops the runner, and sets the room's harness and model. The
next message starts a new runner, whose session start replays the transcript.

## API

Routes under `/v1`, documented in the OpenAPI document. The operator's routes take the
orchestrator or operator role to write; any principal, an observer included, reads.

| Route | What it does |
| --- | --- |
| `POST /rooms` | `{kind, harness, model, card_task_id?}`. A card room names its task by id or external id. 201 with the room. |
| `GET /rooms` | The rooms, most recently active first. |
| `GET /rooms/{id}?window=N` | The room and its newest N turns (default 50, at most 500), oldest of them first, with `turns_total`. |
| `POST /rooms/{id}/messages` | `{text}`. Writes the user turn; when no runner is up, launches one. 201 with the turn. A runner that cannot be started is a 503 whose message stays in the record for the next one. |
| `GET /rooms/{id}/stream?after_seq=&max_seconds=` | Server-sent events: `room` (state), `turn` (a turn as first seen), `delta` (text appended to an open assistant turn), `turn_end`, `closed`. Ends after `max_seconds` (default 300) or at close; reconnect with the last seq seen. |
| `POST /rooms/{id}/interrupt` | Stops the running turn; 409 when none runs. |
| `POST /rooms/{id}/switch` | `{harness, model}`. The switch line, the runner stopped. Returns the room and the system turn. |
| `POST /rooms/{id}/close` | Stops the runner; the room takes nothing more. |

A harness other than `claude_code` is refused with 409 on create and on switch, with a
detail that says it is not yet a room harness.

The runner's routes take only the room-scoped token Hades minted for that room's runner
(`crr_<room id>.<secret>`). An ordinary bearer token is refused there (401), a runner
token of another room is refused (403), and a runner token is refused on every other
route because nothing else accepts its form (401).

| Route | What it does |
| --- | --- |
| `GET /rooms/{id}/session` | The session start: the system prompt text, harness, model, allowed and disallowed tools, idle timeout. |
| `GET /rooms/{id}/inbox?wait=` | The long poll (at most 25 s): the next user turn and its assistant seq, `interrupt`, `stop`, or nothing. Each look is its own short transaction. |
| `POST /rooms/{id}/turns/{seq}/events` | `{events: [...]}`: `delta` (text), `tool_call` (name, input, tool_use_id), `session` (session_id), `end` (interrupted, error). Answers the pending control, so a runner learns of an interrupt from its own post too. 409 once the turn has ended. |
| `POST /rooms/{id}/tools/{name}` | One Hades tool call, run with the room's authority (below). |
| `POST /rooms/{id}/runner/exit` | The runner is exiting; the room goes idle and the runner is removed. |

## The launch

A room runner is launched through the existing provider the way an attempt's worker is,
with no checkout and no repository, and runs, from `/crucible/room` where
`tools/room_runner.py` is mounted read-only:

```
uv run --no-project --with claude-agent-sdk python tools/room_runner.py
```

wrapped by the same launch wrapper an attempt's harness gets (it probes the egress
allowlist, reads `CRUCIBLE_ENV_FROM_FILES` into the environment, and keeps the runner a
direct child). `rooms.provider` picks the provider; Kubernetes is the default when it is
enabled, Docker otherwise (`make up`). The image is `rooms.image`, else the harness's
promoted worker image (ADR 0018).

On **Kubernetes** a launch creates four objects in the workers namespace, each named
`room-<room id>` and labelled `crucible.room=<room id>` and `crucible.role=room-runner`
(never `crucible.attempt`, so attempt reconcile and retention leave them alone):

- a Secret with the harness's token file copied from the service-owned harness Secret
  (ADR 0015), the way `prepare` seeds an attempt's `cred-<attempt>`, and the room token;
- a ConfigMap with `tools/room_runner.py`;
- a NetworkPolicy: cluster DNS, the resolved `rooms.egress_allowlist` (by default
  `api.anthropic.com`, `pypi.org` and `files.pythonhosted.org`, the last two for the
  `uv --with` install), and the Hades API pods (`rooms.api_namespace`,
  `rooms.api_pod_labels`, `rooms.api_port`) and nothing else of that namespace;
- a Job of one Pod in 26's shape: non-root, read-only root, every capability dropped, no
  service account token, 1 CPU and 1 GiB. The token file is projected read-only at the
  adapter's mount target (`/home/worker/.claude/oauth-token` for Claude Code), and the
  wrapper exports it as `CLAUDE_CODE_OAUTH_TOKEN` exactly as a `claude_code` attempt
  gets it. The room token is a file at `/crucible/room-token/token` on a read-only mount
  of its own, never an environment value.

On **Docker** the script, the token file and the room token are written under the
artifact root (`rooms/room-<room id>/`) and mounted read-only; the container is hardened
as an attempt's worker is (13) and joins the workers network behind the egress proxy.
The runner reaches Hades at `rooms.api_url` through that proxy, so the proxy's allowlist
has to permit the API's host and port for `make up`; this path was not exercised with a
live daemon in FDY-0590.

`rooms.api_url` is how the runner reaches Hades from where it runs (for example
`http://crucible-api.crucible.svc.cluster.local:8080`). With none set, a room takes
messages and every launch is refused with a 503 that says so.

**CLAUDE_CONFIG_DIR is an emptyDir** (a tmpfs under Docker) at `/crucible/room-config`,
one per room runner, writable. That is acceptable for this increment and has one
consequence: the harness session does not outlive the Pod. Inside one Pod a child that
dies is replaced by a new client resumed from the session (the spike's finding (f)); a
new Pod always starts a new session from the record. A persistent per-room volume would
let a new runner resume instead; nothing in the record would change.

## The runner

`tools/room_runner.py` scrubs every `CLAUDE*` and `ANTHROPIC*` variable from its
environment first (the SDK copies the parent's environment into its child), keeping the
OAuth token only to hand to the child. It reads the room token from
`HADES_ROOM_TOKEN_FILE`, asks for its session start, and builds one `ClaudeSDKClient`
with:

```
ClaudeAgentOptions(
    cli_path="/usr/local/bin/claude",
    cwd="/home/worker/rooms/<room id>",          # fixed per room
    model=<the room's model>,
    tools=[],
    permission_mode="dontAsk",
    extra_args={"permission-prompts": "none"},
    allowed_tools=[<the Hades tools, as mcp__hades__...>],
    disallowed_tools=["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit",
                      "WebFetch", "WebSearch", "Agent"],
    include_partial_messages=True,
    system_prompt={"type": "preset", "preset": "claude_code", "append": <session start>},
    env={"CLAUDE_CODE_OAUTH_TOKEN": <token>, "CLAUDE_CONFIG_DIR": "/crucible/room-config"},
    mcp_servers={"hades": <in-process SDK MCP server of the Hades tools>},
    hooks={"PreToolUse": [HookMatcher(hooks=[<records the call on the turn>])]},
)
```

It long-polls the inbox. Each message is one `client.query()`; its partial text is posted
as `delta` events (at most every quarter second, and within a poll when the reply
pauses), and the turn ends with an `end` event. While a turn runs it polls the inbox
every second for `interrupt`. When the CLI child dies (`ProcessError`,
`CLIConnectionError`) the turn ends as interrupted and the runner builds a new client
with `resume=<session id>`, never `connect()` on the old one. It exits on `stop`, on a
refused token, or after the idle timeout with no message, telling Hades it is exiting.

## The session start

The same for every room kind, as `crucible.domain.room_session.assemble` builds it:

1. the identity, `config/principal/IDENTITY.md`;
2. the room: its kind, harness and model, local time, and that only the Hades tools exist;
3. for a card room, the card: external id, title, project, state and objective;
4. the recall: what `GET /v1/memory` returns for the room's subject (the same rule, run
   in Hades), with the card's project and external id as tags in a card room;
5. the decisions that touch it: ledger lines that apply to the room, its card or the
   cards it filed, or whose words carry the subject's words;
6. a rolling summary of the room's older turns: one clipped line each for the newest 40
   before the window, with their tool calls, decisions and interruptions, and a count of
   what is left out;
7. the room's last 20 turns, verbatim.

The subject is the card's title and objective for a card room, and the words of the last
six turns for the principal room. User turns not yet handed to the runner are left out:
the inbox delivers them.

## The Hades tools

In-process SDK MCP tools on the server `hades`, each a call to
`POST /rooms/{id}/tools/{name}` with the room token. The room acts as the principal who
created it, and only within its scope: its card, and every card it filed.

- `hades_recall(subject, tags)`: the shared memory recall.
- `hades_record_decision(verbatim, applies_to)`: a ledger line in channel `room`, said by
  the room's principal, with the room in `applies_to` and a transcript ref to the turn.
  The words must be the operator's own as they stand in one of the room's user turns
  (409 otherwise), and every task `applies_to` names must be in scope (403). The turn
  gets the decision's id.
- `hades_file_card(title, objective, project)`: a proposed task (hades #424), built from
  the project's newest contract with the next external id, one acceptance criterion that
  says the objective is met, and no work branch. Nothing starts it until an operator
  approves it. The new card joins the room's scope.
- `hades_read_task(task_id)`: a task in scope, with its objective and notes.
- `hades_post_note(task_id, text)`: a note on a task in scope, marked not verbatim.

`hades_answer_question(task_id, question_id, text)` is not offered: it needs the
comment-states API (FDY-0586), which is not on main.

Approval detection is not part of this: a decision is recorded only when the agent calls
`hades_record_decision`.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `rooms.idle_timeout_minutes` | 30 | How long a warm runner waits for a message before it exits and its room goes idle. A runtime setting: the settings page saves it (administrators), a saved value wins over the `rooms.idle_timeout_minutes` seed in the settings file, and it applies at the next launch. |
| `rooms.api_url` | empty | How a runner reaches Hades. |
| `rooms.provider` | Kubernetes when enabled, else Docker | Which provider runs runners. |
| `rooms.image` | the harness's promoted image | The runner's image. |
| `rooms.egress_allowlist` | `api.anthropic.com`, `pypi.org`, `files.pythonhosted.org` | What a runner may reach besides Hades. |
| `rooms.api_namespace`, `rooms.api_pod_labels`, `rooms.api_port` | `crucible`, the API Deployment's labels, 8080 | The API pods the runner's NetworkPolicy opens. |
| `rooms.identity_path`, `rooms.runner_script` | the service's `/app` copies | Where the identity and the runner script are read from. |
| `rooms.max_runner_seconds` | 43200 | The runner Job's hard deadline. |

## Not in this increment

The room page (a separate task); a supervisor-tick sweep for rooms nobody touches (the
reclaim runs on room routes and on the runner's own exit notice; a runner that dies
silently is found on the next message); harnesses other than Claude Code; a persistent
per-room CLAUDE_CONFIG_DIR; `hades_answer_question`; approval detection.
