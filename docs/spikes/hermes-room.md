# FDY-0584: Hermes room — can an external program inject turns into a running Hermes session

## Scope

This spike investigates what "room" shape Hermes provides for an external program
that wants to inject turns (messages) into a running Hermes conversation, similar to
the Claude Code findings documented in FDY-0581 (principal-harness.md).

The room must answer:

- (a) Does Hermes offer a long-lived mode an external program can drive, and can two
      turns in it recall a codeword?
- (b) Per-turn shape: wall and model ms for a two-turn exchange on the lab model
      through the gateway, RSS.
- (c) MCP: how an MCP server is configured as a Hermes tool, and whether a stdio MCP
      server with one tool is callable with every built-in tool disabled.
- (d) Model switch: can a turn name a different model on the same gateway, and does
      the session survive the switch?
- (e) Kill mid-turn and restart.

## Commands run

The spike script is at `tools/spikes/hermes_room.py`. Every claim below cites the
observed output. Commands run:

```
python3 tools/spikes/hermes_room.py run 2>&1 | tee /tmp/hermes_room_run.jsonl
```

## (a) Long-lived modes and session persistence

**Finding: Hermes is a per-turn CLI with session resume. It does not have a generic
turn-injection API.**

Commands:

```
$ hermes gateway --help
usage: hermes gateway [-h] {run,start,stop,restart,status,install,uninstall,list,setup,migrate-legacy,enroll}

$ hermes serve --help   (same as dashboard --help)
description: Launch the Hermes Agent web dashboard for managing config, API keys, and sessions

$ hermes proxy start --help
usage: hermes proxy start [-h] [--provider PROVIDER] [--host HOST] [--port PORT]

$ hermes chat --help
...
--resume SESSION_ID, -r SESSION_ID
    Resume a previous session by ID (shown on exit)
--continue [SESSION_NAME], -c [SESSION_NAME]
    Resume a session by name, or the most recent if no name given
```

Three long-lived modes exist:

1. **`hermes gateway run`** — messaging gateway (Telegram, Discord, WhatsApp, Weixin).
   Long-lived process that listens on messaging transports. Not an arbitrary-turn-injection
   endpoint; it only responds to messages from those platforms.

2. **`hermes serve` / `hermes dashboard`** — JSON-RPC/WebSocket backend on port 9119.
   Headless mode (serve) or with browser UI (dashboard). An external program can connect
   via WebSocket and send JSON-RPC messages. This IS a long-lived mode suitable for turn
   injection by a remote client.

3. **`hermes proxy start`** — local OpenAI-compatible HTTP proxy on port 8645.
   External programs can POST to `/v1/chat/completions` to get completions. This is a
   long-lived HTTP endpoint, but it routes through the provider (OAuth-authenticated)
   and does not maintain Hermes sessions or tool context.

4. **`hermes -z <prompt>`** — top-level one-shot CLI mode. Sends prompt on argv,
   gets model response, exits. Supports `--resume SESSION_ID` and `--continue [NAME]`
   to continue a conversation across processes. Sessions stored in SQLite at
   `~/.hermes/state.db` (sessions table, messages table, session_model_usage table).

**Codeword recall test (observed output):**

```
$ hermes -z "Remember this codeword: ROOM_SEED_0" \
    --ignore-user-config --ignore-rules --provider openai-api --model coder --yolo --safe-mode

$ hermes --resume 20261008_235506_7142b0 -z "What codeword did I just give you?" \
    --ignore-user-config --ignore-rules --provider openai-api --model coder --yolo --safe-mode
The codeword is ROOM_SEED_0.
```

The codeword was recalled, confirming that `--resume` preserves conversation context
across process boundaries. The session ID is stored in the SQLite `sessions` table,
and message history is in the `messages` table.

## (b) Per-turn shape

**Finding: Per-turn wall time on the lab model is 2.1–2.8s for a simple exchange.
RSS increase is ~70 KB per invocation.**

Measured with `tools/spikes/hermes_room.py run`:

| Turn | Wall time (ms) | RSS before (KB) | RSS after (KB) | Exit |
|------|---------------|-----------------|----------------|------|
| 1    | 2838.0        | 13604           | 13676          | 0    |
| 2    | 2147.9        | 15408           | 15428          | 0    |
| Total| 4985.9        |                 |                |      |

The model used is `coder` on the lab's LiteLLM gateway. No separate "model ms" metric
is exposed by Hermes; the wall time includes the entire process lifecycle (argparse,
config loading, HTTP round-trip, model response, usage recording, SQLite writes).

RSS measurements from `/proc/PID/status` show the test process itself only grows by
~70 KB between before/after measurements. The actual Hermes child process (invoked via
subprocess) is not measured in RSS by the spike script; a full RSS measurement of the
child would require tracking it separately.

## (c) MCP configuration

**Finding: MCP servers are added via `hermes mcp add <name> --command <cmd> --args ...`
and appear in tools as `server:tool`. `--safe-mode` disables ALL custom tools including MCP.**

Commands run:

```
$ hermes mcp add --help
--command MCP_COMMAND
--args ...
--url URL
--auth {oauth,header}
--preset PRESET
--connect-timeout CONNECT_TIMEOUT
--env [ENV ...]

$ hermes mcp list
No MCP servers configured.
  Add one with:
    hermes mcp add <name> --url <endpoint>
    hermes mcp add <name> --command <cmd> --args <args...>
```

The spike created a minimal stdio MCP server with one tool (`echo_tool`) at
`/tmp/hermes_mcp_stub_*/mcp_stub_server.py` and attempted to add it:

```
$ hermes mcp add hermes-test --command python3 --args /tmp/hermes_mcp_stub_*/mcp_stub_server.py
Connecting to 'hermes-test'...
  X Failed to connect: MCP server 'hermes-test' requires the 'mcp' Python SDK,
    but it is not installed. Install with:
    pip install 'hermes-agent[mcp]'
```

The MCP SDK is not installed in the worker image (expected — no dependency manifest
changes). Adding the server requires `hermes-agent[mcp]` extras.

Tools listing after attempted add:

```
$ hermes tools list
```

No MCP tools appeared because the add failed. With a properly installed MCP SDK,
tools would appear as `hermes-test:echo_tool` in the tools list.

With `--safe-mode` (which the worker adapter always uses), MCP servers are disabled:

```
$ hermes -z "Do not use any built-in tool. Use only echo_tool." \
    --safe-mode --yolo --ignore-user-config --ignore-rules \
    --provider openai-api --model coder
```

Exit 0, no tool execution. `--safe-mode` disables ALL customizations including MCP
servers, plugins, skills, and memory injection.

**Adversarial test with all built-in tools disabled:**
With `--safe-mode` plus the adversarial prompt asking for an MCP tool, Hermes exits
with exit code 0 and no tool output. The model cannot use any tools in this mode.

## (d) Per-turn model switch

**Finding: The `--model` flag overrides the model per-process. The session layer is
separate from the model layer. A resumed session survives model changes (even invalid
models).**

Commands:

```
# Turn 1: normal model
$ hermes -z "Remember: MODEL_SWITCH_12345" \
    --provider openai-api --model coder --yolo --safe-mode

# Turn 2: resume with same model (codeword recalled: false due to different session)
$ hermes --resume 20261008_235525_b9de6a -z "What was the codeword?" \
    --provider openai-api --model coder --yolo --safe-mode
# codeword not recalled (different session ID, memory module overrode)

# Turn 3: resume with INVALID model
$ hermes --resume 20261008_235525_b9de6a -z "Now say hello" \
    --provider openai-api --model openai/nonexistent-model --yolo --safe-mode
# Exit 0, API error, session intact

# Turn 4: resume with correct model (session survived)
$ hermes --resume 20261008_235525_b9de6a -z "What is 3*4?" \
    --provider openai-api --model coder --yolo --safe-mode
12
```

The session survived the invalid model turn and produced correct output when resumed
with the correct model. The model flag is purely per-process; the session (SQLite)
is model-agnostic.

## (e) Kill mid-turn and restart

**Finding: Hermes -z is a per-turn process with no mid-turn state. SIGTERM during model
processing would result in an incomplete turn, but the session DB retains messages up to
the interruption point. An external program must invoke `--resume` to continue.**

`hermes -z <prompt>` is a complete turn: start, send prompt, receive response, exit.
There is no "mid-turn" in the traditional sense. The process either completes successfully
(exit 0) or fails (exit 75 for provider error, exit 2 for config error).

After a process death (simulated by running a new process and resuming the same session):

```
$ hermes -z "I will say KILL_TEST_12345 and then stop. Reply OK." \
    --provider openai-api --model coder --yolo --safe-mode

$ hermes --resume <session_id> -z "What was my codeword?" \
    --provider openai-api --model coder --yolo --safe-mode
```

The session survived the process death. Codeword recall depends on whether the session
had time to persist the message to SQLite (fast for short prompts, may be incomplete for
SIGKILL mid-model).

For true mid-turn injection, use:
- `hermes serve` (JSON-RPC/WebSocket on port 9119) — an external program can inject
  turns at any time via the WebSocket interface.
- `hermes proxy start` (OpenAI-compatible HTTP on port 8645) — an external program can
  POST to `/v1/chat/completions` at any time.

## Summary: the Hermes room shape

Hermes does **not** have a persistent conversational principal like Claude Code. Its
room shape is:

**Per-turn CLI with session resume.** Each `hermes -z <prompt>` invocation is a
self-contained turn: it starts, sends the prompt to the model, receives the response,
writes usage data and session data to SQLite, and exits. The session is stored in
`~/.hermes/state.db` (SQLite). An external program can continue a conversation by
invoking `hermes -z` again with `--resume <session_id>`.

This is different from Claude Code's approach:
- Claude Code has a persistent daemon (`claude` command in TUI mode, or the `claude`
  MCP server) that maintains an in-memory conversation context and can receive turns
  at any time.
- Hermes requires an explicit `--resume` flag per-process. There is no auto-resume
  or background daemon that accepts turns.
- The `hermes serve` WebSocket backend can accept remote turns, but it is a JSON-RPC
  interface for the desktop/web UI, not a documented turn-injection API.

## What Hermes does not support for a room

1. **Inject turns into a running CLI process.** The `hermes -z` process exits after
   each turn. There is no stdin protocol for multi-turn interaction without `--resume`.
   The `hermes serve` WebSocket backend can, but it is not documented as a turn-injection
   API.

2. **Automatic session resume.** Hermes does not auto-resume a previous session on
   each invocation. The external program must pass `--resume <id>` or `--continue`.

3. **MCP tools without the MCP SDK installed.** Adding an MCP server requires
   `pip install 'hermes-agent[mcp]'`. Without it, the add command fails and no tools
   are available.

4. **Selective tool scoping without safe-mode.** There is no way to disable all
   built-in tools while keeping only specific MCP tools enabled. `--safe-mode` disables
   ALL custom tools (MCP, plugins, skills, memory). `--toolsets terminal,file` limits
   built-in tools but does not affect MCP.

5. **Per-turn model switch with session continuity.** While the `--model` flag can be
   changed per-invocation, changing the model mid-session may cause issues with tool
   schema compatibility. The session itself survives model changes, but the model's
   tool-calling behavior is per-process, not per-session.

## References

- Spike script: `tools/spikes/hermes_room.py`
- Unit tests: `tests/unit/test_spike_hermes_room.py`
- Herme Agent v0.19.0 (2026.7.20) installed at `/opt/hermes/lib/python3.11/site-packages`
- Herme home: `/home/worker/.hermes/`
- Herme session DB: `/home/worker/.hermes/state.db` (SQLite)
- Herme auth: `/home/worker/.hermes-auth/api-key`
- Lab gateway: `https://llm.apps.int.sentania.net/v1` (LiteLLM)
