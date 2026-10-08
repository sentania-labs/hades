# 07. Harness adapter contracts

A harness adapter turns (attempt, identity bundle, credentials spec) into a
launch specification the execution provider can run, and turns the finished
run back into a parsed report. Adapters contain no lifecycle logic.

## Interface (`crucible/ports/harness.py`)

```python
class HarnessAdapter(Protocol):
    name: HarnessName                     # "claude_code" | "codex" | "agy" | "hermes" | "qwen_code" | "script-harness" (e2e only, 18)
    supported_versions: VersionRange      # tested range; launch refused outside it
    def capabilities(self) -> HarnessCapabilities: ...
    def credential_spec(self) -> CredentialSpec: ...
    def build_launch(self, ctx: LaunchContext) -> LaunchSpec: ...
    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport: ...
    def classify_exit(self, exit: ExitInfo, stdout_tail: str, stderr_tail: str) -> ExitClass: ...
    # both tails: Claude Code and AGY report a missing or expired login on stdout (S5)
```

`LaunchSpec`: image, command and args, stdin bytes or file, env (no secret
values, only names that the provider resolves from mounts), mounts, working
dir, user, resource limits, network mode, expected exit semantics.

`ExitClass` is the one enum used by contracts, policies, and 16:
`completed`, `completed_without_report`, `blocked`, `environment`,
`auth_failure`, `infrastructure`, `provider_error`, `quota_exhausted`, `timeout`, `stalled`,
`killed`, `crashed`, `lost`, `incomplete`, `unknown`. `infrastructure` (hades #353, #346) is
a model call the gateway or provider failed (502, 503, 504, refused, reset, at capacity)
as each adapter reads it from its own CLI's error events, or a worker whose container
never started; it is retried under its own budget, never consumes an attempt, and is
never gated. Which classes may retry is a policy decision (05b
`retry.eligible_classes`), narrowed by the contract's `retry_on`; a retry
happens only when the class is in both. Gate failures are never a class and
never retry.

## Common rules

- Non-interactive only. No TTY. Permission prompts disabled by the harness's
  own flag; the container is the boundary (12).
- Auth failures are classified from both stdout and stderr tails (S5),
  and only on a non-zero exit: the auth and quota patterns never turn a
  successful run into a failure, and never reclassify a termination
  Crucible performed.
- Codex is launched with `--disable plugins` so it does not contact
  github.com or chatgpt.com for plugin sync (S6).
- The task contract and identity are delivered as files. Argv carries only
  a short pointer. This is forced by AGY's 128 KB argv ceiling and applied
  uniformly.
- Working directory is the checkout. Home is a per-attempt scratch dir
  (`/home/worker`), not a host home.
- stdout and stderr are captured by the provider, chunked, and stored (10).
- Version pinning: each adapter declares the harness version range it was
  tested with; the worker image carries the installed version in a label;
  the attempt records the image digest it ran; `GET /harnesses` reports
  installed and supported versions. A combination outside the range is a
  launch-time refusal with a wake, not a warning. The image must also
  carry a `crucible.harness` label equal to the harness the execution
  asks for; an image that names no harness, or names a different one, is
  refused before anything is seeded, whatever its version label says. An
  image is never launched with a credential on the strength of a version
  label alone. Harness CLIs never
  self-update inside a worker: each image sets the CLI's auto-update
  opt-out and the root filesystem is read-only (13, S11).
- Retries and corrections keep the image digest of the task's first
  attempt unless the correction contract names a different image.
- No `gh` in worker images and no GitHub credential: adapters never
  instruct a worker to push or open a PR.

## Harness version bump checklist (issue 155)

When bumping a harness pin in `images/worker/Dockerfile`, run the
following steps to keep the transcript fixtures in sync:

1. Run `make e2e-command-timeout` to generate transcript reports in
   the harnesses' pytest temporary directories (one per harness,
   beneath `tmp_path`).
2. Copy the new reports from the temporary directories into
   `tests/fixtures_data/transcripts/` under the new version name
   (for example,
   `claude-code-2.1.281-background-completed.jsonl` replaces
   `claude-code-2.1.280-background-completed.jsonl`). Remove any old
   versioned files so the set of fixture versions is exact; a stale
   file causes the pin-match guard in `tests/unit/test_issue_155_fixture_pin_match.py`
   to fail.
3. Verify with `uv run pytest -q tests/unit/test_issue_155_fixture_pin_match.py`
   that all pins match.

The unit test `test_issue_155_fixture_pin_match.py` asserts that every
harness with transcript fixtures has those fixtures at the pinned version;
bumping a pin without renaming the fixtures fails the test with a clear
"refresh the fixtures" message.

## Claude Code

- Version floor: `>=2.1.277,<2.2.0`. Claude Code 2.1.277 is the first release
  whose changelog says it reads `AGENTS.md` under the default
  `instructionFiles` setting. Older worker images are refused at launch.

- Launch: `claude -p --permission-mode bypassPermissions
  --append-system-prompt-file /crucible/identity/IDENTITY.md
  --output-format stream-json --verbose --model <model>` with the prompt on
  stdin: "Read /crucible/identity/IDENTITY.md and execute the task."
  (`stream-json` requires `--verbose` in print mode.)
- Credentials: subscription OAuth state. Crucible's dedicated session uses
  the CLI's long-lived token (S1b), kept as the file `oauth-token` in the
  credential directory, with the top-level state file `.claude.json`
  seeded beside it so the CLI finds the state it expects, and
  `CLAUDE_CONFIG_DIR` pointed at the mounted copy so both live in one
  directory. The token reaches the CLI through its documented environment
  variable, read from the mounted file at container start, as the one
  exception to file-only delivery. Neither file is written back: the
  long-lived token does not refresh, and `.claude.json` is state the CLI
  rewrites on every run, not a credential. The mount is `rw-narrow` (12)
  because the CLI writes that state in place. The rest of the config
  directory (settings, hooks, MCP definitions) is mounted read-only from a
  Crucible-owned template.
- Endpoints: `api.anthropic.com` (plus `mcp-proxy.anthropic.com` only if
  account MCP connectors are wanted). Confirmed by a task completed
  through the filtering proxy, so the list is no longer provisional.
- Login endpoints (the Kubernetes login Job's whole egress, 26):
  `platform.claude.com`, where `setup-token` exchanges the pasted code, and
  `api.anthropic.com`, which it calls for the account's roles
  (`/api/oauth/claude_cli/roles`) before printing the token. Observed on a live
  lab login on 2026-09-29: with only the first host allowed, the login hung
  silently after the paste.
- Shim: one-line untracked `AGENTS.md` pointing at the identity file when the
  checkout has no `AGENTS.md`. Claude Code alone suppresses that shim when the
  checkout has its own `CLAUDE.md`, because it reads that file when it wins
  under the default `instructionFiles` setting, and records that the project's
  `CLAUDE.md` is in force. Codex and AGY do not read `CLAUDE.md`, so it does not
  suppress their shim. A committed `CLAUDE.md` and a committed `AGENTS.md`
  remain project files and no generated shim is written.
- Stream-json lines are parsed into progress events (tool use, text) at low
  fidelity; the full stream is stored as the transcript artifact.
- Known: nested invocation from inside another Claude session works, but
  workers never run inside a session anyway.

## Codex

- Launch: `codex exec --dangerously-bypass-approvals-and-sandbox
  --model <model> -C <checkout>` with `IDENTITY.md` followed by the prompt on
  stdin. `--sandbox read-only` and friends are not used: Codex's bubblewrap
  sandbox needs user namespaces, which the reference workstation denies, and
  the container is the boundary regardless. S2 showed Codex's sandbox
  cannot run inside the worker container at all (no bubblewrap in the
  image, and user namespaces are blocked under every seccomp profile
  tried), so it is never enabled; the container is the only boundary.
- Credentials: `credential:codex` is `rw-narrow` from the start: the CLI
  writes session and log state beside its auth file, so a read-only mount
  fails before auth is tested. Only the auth file syncs back (12).
- Shim: untracked `AGENTS.md` if absent.
- Endpoints: `api.openai.com`, `auth.openai.com`, and `chatgpt.com`.
  `chatgpt.com` is required, not conditional: with a ChatGPT-plan login
  (`auth_mode = chatgpt`) that host is the backend, and a run without it
  reconnects until it is permitted and never reaches the model.
  `ab.chatgpt.com` stays denied.
- Login endpoints: `auth.openai.com`, the device-code and token endpoints
  (the pinned binary's strings, 2026-09-24).
- The worker image carries the code-mode host companion binary the CLI
  spawns for the 5.6 model family; without it those models fail closed.
  It is an asset of the same pinned CLI release and is pinned by the
  tarball's checksum like the CLI itself (13).
- Output: run with `--json` and `-o /crucible/report/codex-last-message.md`;
  the JSON event stream is stored as the transcript artifact, the last
  message as a summary artifact; the report file is the fact.

## AGY

- Launch: `agy -p "<pointer>" --model <model> [--effort <effort>]
  --dangerously-skip-permissions --add-dir /crucible/identity
  --output-format stream-json --print-timeout <attempt timeout>`, as
  observed in the CLI's own `--help` on the
  reference install; S3 confirmed the flags and stream-json output. The
  argv ceiling is the kernel's 128 KiB per argument (S3), so the prompt
  is under 1 KB by construction and the bundle travels by `--add-dir`.
  `--print-timeout` defaults to five minutes in the CLI, which would cut a
  longer task, so it follows the attempt's own timeout. `--effort` is not
  a separate flag for every model: the Flash models carry the effort inside
  the model id (the low-effort Flash model is `gemini-3.8-flash-low`) and
  refuse the flag, so no effort is passed for them.
- Credentials: `credential:agy` mounted to the Gemini config dir,
  `rw-narrow` (12): the first Crucible-side run past the token's one-hour
  expiry refreshed it in place and the copy carried a newer expiry, so the
  token file syncs back by that field.
- Endpoints: `daily-cloudcode-pa.googleapis.com`, `oauth2.googleapis.com`,
  `www.googleapis.com`, and `lh3.googleusercontent.com`. The last two are
  the CLI's eligibility check, which calls the userinfo endpoint and then
  fetches the account's profile picture before any turn and fails closed
  when either is refused. Neither is a model endpoint; both are what the
  CLI needs. The list is no longer provisional.
- Login endpoints: `oauth2.googleapis.com` and `www.googleapis.com`, the
  code exchange and the userinfo call (the pinned binary's strings,
  2026-09-24). AGY's login command ends with a prompt to its model API, which
  a Kubernetes login Job cannot reach, so it exits non-zero there; the token
  it wrote is judged by its shape and then by the probe.
- Shim: untracked `AGENTS.md` if absent (AGY reads `AGENTS.md`; it does not
  read `GEMINI.md` reliably in headless mode per the operator's setup notes).
- Templates: none. AGY's config directory carries no settings file the
  adapter needs to pin, so nothing is mounted read-only on top of the copy
  and `config/` is not seeded.
- Output: stream-json parsed like Claude Code's, keyed by `event` rather
  than `type` (`{"event": "result", "result": {...}}`); the adapter reads
  both keys.
- Quota: the final `result` line with `status: ERROR` is the account's quota,
  and the authoritative mark signal (05b), when its error is the RPC's
  `RESOURCE_EXHAUSTED`, a structured `code: 429`, or the CLI's own sentence
  "Individual quota reached ... Resets in 3h52m" (hades #378). That sentence
  names no status, so it is matched by its words; the reset it states as a
  duration ("Resets in XhYmZs") becomes `reset_at` counted from the moment
  the supervisor observed the exit, and a refusal that states none leaves
  the pool's `default_cooldown_seconds`. `MODEL_CAPACITY_EXHAUSTED` is the
  model's capacity, not the account's quota: it stays `infrastructure`
  (hades #353) and marks nothing.

## Hermes

- Hermes 0.19.0 fronts an OpenAI-compatible local gateway. The routing model id is
  supplied by policy, with `coder` as the C10 migration entry. The adapter does not
  pin a model id. A local launch requires an `endpoint_url` ending in `/v1`.
- Its credential spec names only `api-key`, maps it to `OPENAI_API_KEY`, mounts the
  per-attempt copy read-only, and never syncs it back. When no credential source is
  configured the launch uses the literal non-secret placeholder `local-no-auth`, so an
  unauthenticated compatible endpoint remains possible.
- Launch is `crucible-hermes --ignore-user-config --ignore-rules --safe-mode
  --yolo --provider openai-api --model <routing-model> --toolsets terminal,file
  --usage-file /crucible/report/hermes-usage.json -z <pointer>`. `--yolo` is
  permitted only because the read-only worker container is the permission
  boundary. `HERMES_HOME` is a per-attempt tmpfs, so rules, memory, plugins, MCP
  configuration, and session `state.db` do not cross attempts.
- The image wrapper (FDY-0140) puts the text of `IDENTITY.md`
  (`CRUCIBLE_HERMES_IDENTITY`) in the prompt ahead of the pointer, or only the
  pointer when the text is too long for one argument; starts Hermes with the
  venv's own Python by path, with `-P` so a module in the checkout is never
  imported in place of Hermes's own, and sets no PATH, so the commands the model
  runs resolve `python3`, `pytest` and `uv` to the image's toolchain (Hermes
  prepends the directory of the `hermes` it finds to each command's PATH; the
  image's `/usr/local/bin/hermes` link makes that a directory already on it);
  and applies
  the run limits saved on the Local gateway page (25): `CRUCIBLE_HERMES_MAX_TURNS`
  (default 300; Hermes 0.19's `-z` fixes 90 and reads no setting, so a bootstrap
  sets the budget an agent is built with when its caller named none) and
  `CRUCIBLE_HERMES_CONTEXT_LENGTH` (default 131072, 0 to let Hermes find it),
  written as Hermes's own `model.context_length` in its home, and (hades #388)
  `CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS` (default 32000, the response allowance the
  gateway reserves out of the window), written as Hermes's own `model.max_tokens`,
  so every request carries it and the context compressor takes its trigger from
  the window less it (74304 rather than 98304 input tokens for 131072 and 32000).
  `CRUCIBLE_HERMES_THINKING` carries the routing entry's
  `chat_template_kwargs.enable_thinking`, which the bootstrap puts on each request
  as `extra_body.chat_template_kwargs`, the only request override it adds: a
  `max_tokens` override would replace the lower cap Hermes retries with after the
  gateway reports less room. The bootstrap caps Hermes's own retry boosts at the
  allowance and leaves lower caps as Hermes set them; before Hermes starts, the
  preflight stops the attempt unless Hermes 0.19.0 still reads `model.max_tokens`,
  boosts the way that cap expects, and computes the same compression trigger as
  the wrapper. The context length, allowance and thinking setting are resolved
  once per attempt and recorded as the attempt's `effective_settings`; a later
  spec of the same attempt reuses the record. It writes
  `crucible-hermes: working, session updated` to stderr whenever the session store
  changes, which is log activity (10). A run that ends on its turn budget is marked
  in the usage record (`turn_limit_reached`) and recorded as `harness_limit_reached`
  evidence; it is not failed for that.
- Hermes's usage JSON is mandatory run evidence. Its input and output token counts
  come from that file. The image wrapper enriches duration and tool-call count from
  the same attempt's SQLite session row before exit. Missing or unparsable usage
  fails `run_evidence_present`.
- Classification checks Crucible termination facts first, then usage and provider
  text, then report presence. `blocked.md` on exit 0 is `blocked` (FDY-0140),
  checked before the usage record, so it wins over `failed: true`; otherwise
  `failed: true` overrides exit 0. Exit 75 is Hermes's own and is
  `provider_error` unless explicit quota text makes it `quota_exhausted`. When the
  usage record or exit 75 says the provider failed and Hermes's client reports a 502,
  503 or 504 or no answer at all (refused, reset, timed out), the exit is
  `infrastructure` (hades #353) and is retried under the interruption budget; any
  other 5xx stays `provider_error`. Neither marks the pool from text.
- Crucible's launch wrapper runs the egress probe before the harness whenever the
  attempt has allowlisted hosts: every name in `CRUCIBLE_EGRESS_ALLOWLIST` is tried
  sequentially with `curl` and one `crucible-egress-probe: {...}` line goes to stderr,
  which the supervisor records on the attempt as `egress_probe` (26, hades #425):
  the first marker line only, only for an attempt that has a network, and only
  within the caps 26 gives (64 KiB, three levels, 100 rows), since the harness can
  print the marker too. A host it cannot reach never stops the harness, and the
  harness starts only after the probe's round trips (10 seconds per host at most), so even a
  harness that exits at once is observed running in the tick that launched it. The
  wrapper forwards SIGTERM
  to the harness and waits for its cleanup within the container termination grace
  period. It drains cleanup output into the transcript and preserves the harness
  exit status, including when prompt input or command monitoring is enabled.
- Crucible's launch wrapper is the sole transcript writer. The Hermes image wrapper
  inherits stdout and only enriches the usage record after the child exits.

## Report parsing (all harnesses)

`/crucible/report/report.yaml` is parsed against `CompletionClaimV1` from
the collector's copy (08), after Crucible has filled the fact fields from its
own evidence (11, hades #215). A missing file with exit 0 is
`completed_without_report`. A file that is present but does not parse is
recorded as `report_parse_failed` with the parser's errors and
`report_present` true; it is never recorded as "no report". Either way the
report gate fails hard and neither is a success; what differs is that the
record says which happened. `blocked.md` on a clean exit, 0 or 75, produces an
escalation and moves the task to `blocked`, whatever the report says: a model
cannot choose its harness's exit code (FDY-0140). Exit 75 without it is
`failed`. Progress lines are ingested as
`worker_progress` events under the principal `worker` and marked
`unverified`, at most 200 per attempt and 1000 characters per line, each
line redacted before it is stored (12).

## Local model endpoints

`LaunchContext` and `LaunchSpec` carry `endpoint` as `subscription` or `local`
and a separate optional `endpoint_url`. Local requires the URL; subscription
forbids it. The supervisor preserves both fields through launch reconstruction.
Local routing does not imply credential absence. The provider mounts the selected
harness's declared credential when one is configured. Hermes therefore receives its
read-only `api-key` copy, while a deployment without that file gets the explicit
fallback. For an optional credential, a configured directory that does not hold its
required auth files (the empty directory Compose creates) counts as not mounted.
The identity, report, and gate contracts do not change.

Hermes is the local gateway front end. Other harness and local-server
combinations remain disabled until their own compatibility and quality evidence
exists.

## Commands that outlive their timeout (issue 128)

A headless harness runs each shell command under a timeout of its own, and some
of them move a command that outlives it to the background. In a headless run the
turn can then end, the process exits, the command dies with it, and the attempt
looks finished. Every adapter launches its harness with the attempt's command
timeout (05b `limits.command_timeout_ms`, set by the contract within the
policy's bounds, capped at `timeout_seconds`). A clean exit (`completed` or
`completed_without_report`) is classified `incomplete` only when the harness's own
transcript shows a command it was waiting on cut off by the exit, and those commands
are recorded on the `attempt_collected` event as `work_in_flight`. A background
process the worker chose to leave running is not unfinished work: it dies with the
sandbox, the pre-PR gates and CI catch work that was not done, and it changes neither
the class nor the record (issue 153, the operator's decision of 2026-09-27). Each fact
below was established on the pinned worker image on 2026-09-25 and 2026-09-27, against
a stub model server with no network and no login (`make e2e-command-timeout`).

| Harness | What it does with a long command | What the launch sets | What makes a clean exit `incomplete` |
|---|---|---|---|
| Claude Code 2.1.280 | Moves a Bash command past its timeout to the background (`sleep` excepted); `-p` then ends its turn and kills it at exit | `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, so the command is ended at its timeout and reported to the model instead; `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` to the command timeout | a stream-json `task_started` (`is_backgrounded`) with no `completed` or `failed` end, or one ended by the exit after the final `result`, when the `tool_use` that started it did not ask for `run_in_background`: the CLI backgrounded a call it was waiting on. A task the model asked for is its own background process and does not count; under the launch setting the CLI refuses that parameter anyway (observed 2026-09-27) |
| Codex 0.156.0 | Unified exec returns after at most 30 s with a session the model must poll with `write_stdin`; a turn that ends instead leaves the command to die. The model catalog, not the `unified_exec` feature flag, picks this tool, so nothing at the launch makes a command block | `-c background_terminal_max_timeout=<command timeout>`, the longest single poll | nothing. Codex exits only once the model ends its turn, so a `command_execution` item still open at exit is a session the model left running, not a call Codex was waiting on |
| Hermes 0.19.0 | Never backgrounds a foreground command: kills it at `TERMINAL_TIMEOUT` and reports exit 124. A model may ask for `background=true`; under `-z` Hermes says it cannot deliver the completion and exits without waiting | `TERMINAL_TIMEOUT` and `TERMINAL_MAX_FOREGROUND_TIMEOUT`, in whole seconds | nothing. A foreground command never outlives Hermes, and a process in its registry (`HERMES_HOME/processes.json`) at exit is one the model asked to background |
| AGY 1.2.8 | `run_command` takes `Blocking` and `WaitMsBeforeAsync` from the model per call; past the wait a command continues in the background | no flag, variable or setting exists for it in `agy --help`, the binary, or the public CLI docs, so the `-p` prompt asks the model to run every command blocking for up to the command timeout and never to end its turn with one running: a mitigation nothing enforces. `--print-timeout` stays the attempt's timeout | none known; AGY cannot run without a Google login, so its exit behaviour was not observed and its attempts are classified by exit code and report alone |

### A command in flight during the run (issue 152)

The same evidence, read from the live log while the attempt runs, pauses the
stall clock (05b, 10): each adapter gives the supervisor a tracker that it feeds
every stored log chunk in order, and while the tracker reports a command running
the supervisor writes a `command_running` activity signal. A command still reported
a minute past the launch's command timeout stops counting, whatever keeps the
harness reporting it. The log is the one
source that reaches the supervisor on Docker and Kubernetes alike (Kubernetes
merges the two streams, so a tracker reads both). Each fact below was observed
on the pinned worker image on 2026-09-27 against the stub model, with the log
read every half second while a silent command ran (`make e2e-command-timeout`).

| Harness | In flight while it runs |
|---|---|
| Claude Code | a stream-json `assistant` event's `tool_use` block, from before the tool runs until the `user` event carrying its `tool_result`; also a backgrounded task until the CLI reports its end. A later message from the same agent closes its earlier calls, since the model is only asked again once every result is back |
| Codex | a `command_execution` item from `item.started` to `item.completed`, polls included. Other items do not count: a todo list stays open for the whole turn |
| Hermes | under `-z` Hermes writes nothing while it works, so the launch wrapper reads its process registry (`CRUCIBLE_IN_FLIGHT_FILE`, the same `processes.json`) every 10 seconds and writes `crucible-launch: commands running: <n>` to stderr whenever the count changes; only the count leaves the file. The registry lists background commands only: a foreground command, which Hermes ends at `TERMINAL_TIMEOUT`, gives no live evidence, and the stall limits count it as silence |
| AGY | none: no tracker, and the stall clock runs as for any silent worker |

### Degenerate runs (issue 278)

A tracker may also say when a run has gone degenerate (a `CommandLoopTracker`):
the last command it started and how many times in a row it started, whether the
harness's turn has begun, and whether the model has called any tool. The supervisor
reads it on every tick, after the tracker is fed, and ends the attempt as a stall
(16) at once when:

- the same command has started 8 times in a row (the accepted bound is 5 to 10),
  with no other command and no file edit between: a loop, on any route. A command
  repeated around edits is iteration and does not count. An edit is one the log
  shows or one the supervisor's own workspace check verifies (the fingerprint walk
  or the activity probe of 10), since a command that edits through the shell, a
  script that fixes one failure per call, shows the log only commands: the
  supervisor tells the tracker (`workspace_changed`) and the run starts over. On a
  provider whose probe is throttled (Kubernetes asks no more often than a command
  renews activity), a loop verdict asks the probe once more, past the throttle,
  before the attempt is ended, so repeats between two probes are not mistaken for
  a loop;
- the attempt runs on a local endpoint, its turn began 300 seconds ago, and the
  model has made no tool call: `no_activity`. The clock starts at the stored log
  chunk where the tracker first saw the model working on the turn, so the
  preparer, the image pull and the harness's own start are not in it.

Only Codex's tracker says this today. It counts each `command_execution` item once,
at `item.started` (or at `item.completed` for one that never started), and resets
on a `file_change` item or a verified workspace change; `turn.started` begins the
turn (`thread.started` comes before it, when the thread is created, and whatever
the CLI does between the two is not the model's time), and a `command_execution`,
`file_change`, `mcp_tool_call` or `web_search` item is a tool call. It reads the
`codex exec --json` stream, which is what the local route launches; the
subscription route's app-server host writes its events to the report transcript
and not to the log, so there only the time-based limits apply. Every other harness
keeps the time-based stall limits alone.

### Codex on the local gateway (FDY-0149, issue #249)

For `endpoint: local`, Codex uses the same read-only `api-key` credential as Hermes,
loaded into `OPENAI_API_KEY` at container start. It never mounts subscription
`auth.json`. The launch wrapper writes a fresh per-attempt
`/home/worker/.codex/config.toml` before invoking Codex. `model_provider` selects
`local_gateway`; its provider table has `base_url` (including `/v1`),
`env_key = "OPENAI_API_KEY"`, and `wire_api = "responses"`. The top-level
`model_context_window` uses the Local gateway page's context length. A saved zero,
which asks Hermes to discover its window, uses 131072 for Codex because Codex cannot
use Hermes's discovery. Set a positive context length for the actual gateway model.
The top-level `model_max_output_tokens` uses the Local gateway page's response
allowance the same way.

Hades #354: the routing entry Codex launches with is read first. Its own
`context_length` and `max_output_tokens`, when either is set, replace the Local
gateway page's figures above, resolved once per attempt like the rest of
`effective_settings` (hades #388) and reused on every later spec of that attempt. An
entry that sets neither keeps reading the Local gateway page's defaults, unchanged.
`context_length` and `max_output_tokens` are set independently, so the two can combine
(one entry's own pair, or one figure against the other inherited from the Local
gateway page) into a response reservation that consumes the whole window; that final
pair is validated before the launch config is emitted, and the launch is refused,
the same way an unknown harness or a missing credential is, rather than sent to fail
at request time with no input budget.

`--model` is the routing entry's `model` (the lane, for example `fast`) unless the
entry carries `harness_model_name`, in which case that name is sent instead, for
example `gpt-5.4`, a gateway alias Codex's own model catalog recognises for the same
backing model. Without a recognised name Codex logs `Model metadata for '<name>' not
found` and falls back to generic tool, prompt, output-token and compaction defaults,
regardless of `model_context_window` and `model_max_output_tokens` above. Routing,
pools and `GET /routing/history` always read `model`; the launch event
(`attempt_launching`) records `sent_model_name` beside it, so the lane and the name
actually sent are both evidence. An entry with no `harness_model_name` sends `model`
unchanged, exactly as before #354. Setting `harness_model_name` never changes the
gateway alias itself or a routing entry's thinking setting; those stay lab-admin's.

The launch retains `--dangerously-bypass-approvals-and-sandbox`, never adds
`--ignore-user-config`, and sends identity plus the pointer prompt through a finite
pipe, closing stdin when those bytes are sent. CODEX_HOME is never under `/tmp`.
Local egress adds the gateway endpoint instead of Codex's subscription hosts; policy
package registries remain available as for Hermes. Local Codex holds no writable
subscription credential and shares the local pool's concurrency limit. Subscription
launch, credential synchronization, and its concurrency cap of one are unchanged.

### Qwen Code (`qwen_code`, 0.25.0)

The local-only adapter launches `crucible-qwen-code`, which writes settings and
execs `qwen --yolo --auth-type openai --advisor off --output-format stream-json
--max-session-turns 300 "<IDENTITY>"`. The full IDENTITY.md precedes the pointer
prompt, including the requirement to write `report.yaml` as CompletionClaimV1.
CLI success prose is never substituted for that report. `OPENAI_BASE_URL` names
the gateway and `OPENAI_MODEL` the routed lane (`model_name` when configured).
The adapter mounts Hermes's existing `api-key` read-only and resolves it as
`OPENAI_API_KEY` at container start, with no credential sync-back.

The fresh home gets `~/.qwen/settings.json` with two model settings:

- `model.maxToolCallsPerTurn: 0` disables the per-turn tool-call cap that stopped
  10 of the 44 replay runs in #448.
- `model.generationConfig.contextWindowSize` is the full engine window, from the
  routing entry's positive integer `context_length`, or **131072** by default.
  Qwen 0.25.0 clamps output to the room left in this window, including its safety
  margin, preventing a 32000-token output reservation beyond the engine limit.
  The effective value is recorded on the attempt and reused on reconstruction.

Shell execution uses child_process rather than optional native PTY addons; search
uses the image's pinned ripgrep. The npm bundle is extracted without resolving
optional native dependencies. CI checks Node, npm and Qwen in the built image.
The provider captures stream-json as `transcript.jsonl`; the adapter reads result
usage and completion, and pairs assistant `tool_use` with user `tool_result`
blocks for command activity. A missing result is an evidence anomaly. Exit 75
with `blocked.md` is blocked; a 400 context overflow is a provider error, and a
quota refusal is quota exhausted. Unstructured refusals do not change shared pool
state. Session-turn exhaustion is incomplete.
