# Room runner spike: Claude Agent SDK versus the stream-json CLI

Hades #208, item 1b (FDY-0582). This spike builds on FDY-0581
(`docs/spikes/principal-harness.md`, commit `06c7654a` on `crucible/FDY-0581`; that
file is not on `main` yet, so it was read from the branch with
`git show 06c7654a:docs/spikes/principal-harness.md`). FDY-0581 measured the per-turn
`claude -p` process and the long-lived `--input-format stream-json` process. This
spike does not repeat those measurements. It asks a narrower question: should the
long-lived conversation runner (a "room" that stays warm while Scott talks, takes
injected messages and streams replies) be built on the Claude Agent SDK for Python, or
should Hades drive the raw stream-json process itself?

Run on 2026-10-08, 6:57 to 7:00 PM CT, inside a Hades worker container launched as
`claude_code`. The binaries were the image's own: Claude Code 2.1.280 at
`/usr/local/bin/claude`. The SDK was `claude-agent-sdk` 0.2.165 from PyPI on Python
3.12.13, supplied for this one process only. It is not in the image or in Hades's
dependencies. Every number below comes from one run of:

```
uv run --no-project --with claude-agent-sdk python tools/spikes/room_runner_sdk.py \
  --out /tmp/rr/final all > /tmp/rr/final.stdout 2> /tmp/rr/final.stderr
```

That run printed one JSON report (the experiments are listed in its header) and wrote
`/tmp/rr/final/<experiment>.json` plus the session files under
`/tmp/rr/final/<experiment>/config/projects/`. Values are quoted from those files.
Where an earlier run of the same script (6:47 to 6:50 PM CT, `--out /tmp/rr/out`)
behaved differently, both are shown. The full run took 3 min 16 s. Em-dashes in
quoted model output are written here as colons, under the style rule.

## Decision

**Build the room on the Claude Agent SDK. Keep the per-turn `claude -p` process for
scheduled and one-shot runs.** The two paths share one session store, so a room
resumes a per-turn session and the reverse (g). Hades can therefore move a
conversation between them without a rebuild.

The SDK is not a different engine. The child it spawns is the stream-json process
FDY-0581 measured, started with `--input-format stream-json` (see "What the worker
had"). Cache use and model cost are the same as five per-turn processes, to the
token (e). Interrupt works the same way with or without the SDK (c). So the choice
comes down to what Hades would otherwise have to write and maintain itself. With the
SDK, the Hades client runs as an in-process MCP tool (b), the PreToolUse and Stop
hooks are Python callbacks in the runner (d), and the control protocol (initialize,
interrupt, hook and tool callbacks) comes ready-made. Without the SDK, Hades would
have to reimplement that protocol against an undocumented wire format, run a stdio
MCP server, and use shell-command hooks from a settings file.

The SDK has three costs. It adds a Python process of about 75 MB next to the
217 MB CLI child. It adds a new dependency that is not in the image. And it makes one
Haiku call per session, about 0.001 USD.

## What the worker had

The harness check came first, before anything else ran:

```
$ which claude; claude --version         -> /usr/local/bin/claude, 2.1.280 (Claude Code)
$ ls -la /home/worker/.claude            -> .claude.json, oauth-token, settings.json (symlinks into ..data/)
$ test -s /home/worker/.claude/oauth-token && echo nonempty  -> nonempty
$ grep /home/worker /proc/mounts         -> tmpfs /home/worker/.claude tmpfs ro,...
$ echo $CRUCIBLE_EGRESS_ALLOWLIST        -> api.anthropic.com,files.pythonhosted.org,github.com,objects.githubusercontent.com,pypi.org
$ uv run --no-project --with claude-agent-sdk python -c "...version('claude-agent-sdk')"
  -> Installed 30 packages in 8.43s; 0.2.165   (21 s wall, cold cache)
```

So this was a `claude_code` worker. As in FDY-0581, `CLAUDE_CODE_OAUTH_TOKEN` was not
in this task's own process environment. The script read the token from the mounted
file into the child's environment only. It was never written to disk or printed.

The SDK copies the parent's `os.environ` into the child, removing only `CLAUDECODE`,
and then applies `ClaudeAgentOptions.env`. Without a scrub, the parent harness's
`CLAUDE_CODE_SESSION_ID`, messaging socket and token variables would reach the
child. The script therefore deletes every `CLAUDE*` and `ANTHROPIC*` variable first:
the header reported `"parent_variables_removed": 12`. A Hades runner must do the same
or start from a clean environment.

`CLAUDE_CONFIG_DIR` was a fresh scratch directory per experiment.

The SDK ships its own CLI (`_cli_version.py`: 2.1.294). With
`cli_path=/usr/local/bin/claude` it ran the image's 2.1.280 with no warning on stderr.
`/proc/<child>/cmdline` of the SDK's child was:

```
/usr/local/bin/claude --output-format stream-json --verbose --tools "" --model claude-sonnet-5
  --permission-mode dontAsk --permission-prompts none --input-format stream-json
```

That is FDY-0581's stream-json process with the same flags.
`system_prompt={"type": "preset", "preset": "claude_code"}` is set so that the CLI
keeps its default system prompt. The SDK's own default, `system_prompt=None`, sends
`--system-prompt ""`, and with that the cache numbers in (e) would not compare.
The SDK has no public accessor for the child's pid. The script reads
`client._transport._process.pid`.

## (a) Five injected turns in one ClaudeSDKClient

`multi-turn` (`/tmp/rr/final/multi-turn.json`) connects one client, then sends five
`client.query()` calls and reads each to its `ResultMessage`. After each result it
waits 3 s and samples VmRSS of the CLI child and of the Python process.

| turn | prompt | wall s | `duration_ms` | child RSS idle kB | Python RSS kB | reply |
|---|---|---|---|---|---|---|
| connect | | 0.77 | | 206988 | 75104 | |
| 1 | "The codeword is ORCHID-42. Reply with exactly: noted" | 1.69 | 1675 | 215884 | 75116 | `noted` |
| 2 | a prime between 10 and 20 | 1.46 | 1452 | 217000 | 75120 | `13` |
| 3 | capital of France | 2.22 | 2215 | 216968 | 75128 | `Paris` |
| 4 | 7 times 8 | 1.68 | 1676 | 218760 | 75132 | `56` |
| 5 | "What was the codeword in my first message?" | 1.82 | 1814 | 218984 | 75132 | `ORCHID-42` |

`"turn5_recalled_codeword": true`. `"one_child_for_all_turns": true`: one pid, 2485
in this run, served all five turns. Every result had the same `session_id`.

The model time per turn is the step in `duration_api_ms`, which is cumulative over the
session: 2401, 1440, 2203, 1665 and 1802 ms. Turn 1 includes the Haiku call
discussed in (e).

Wall time minus `duration_ms` is about 0.01 s, so the SDK adds no measurable latency
per turn. A warm room costs about 217 MB for the CLI child plus 75 MB for Python. The
CLI figure matches FDY-0581's raw stream-json process at 215 MB.

In the earlier run, wall times were 1.79, 1.87, 1.71, 3.37 and 1.87 s, and the child
idled at 216532 to 231808 kB.

## (b) An in-process hades_get, scoped to it alone

`scope` defines `hades_get` with `@tool("hades_get", ..., {"path": str})` and
`create_sdk_mcp_server("hades", tools=[hades_get])`. The tool returns canned JSON and
appends each call to `hades_get.calls.jsonl`. The run uses
`allowed_tools=["mcp__hades__hades_get"]`,
`disallowed_tools=["Bash", "Edit", "Write", "WebFetch", "WebSearch", "Agent"]`,
`permission_mode="dontAsk"` and a PreToolUse hook as an extra witness. It sends
FDY-0581's three requests: run `id`, write `pwned.txt`, call `hades_get /v1/tasks`.
A second turn then asks for `Read` on `/etc/hostname`, because neither list names
`Read`. The script runs the experiment twice: first exactly as the task specifies,
then with `tools=[]` added, which is the SDK spelling of FDY-0581's `--tools ""`.

| | allow and deny lists only | plus `tools=[]` |
|---|---|---|
| `system init` tools | 22: `CronCreate, CronDelete, CronList, DesignSync, EnterWorktree, ExitWorktree, Glob, Grep, ListAgents, Monitor, NotebookEdit, PushNotification, Read, RemoteTrigger, ReportFindings, ScheduleWakeup, SendMessage, Skill, TaskStop, ToolSearch, Workflow, mcp__hades__hades_get` | 1: `mcp__hades__hades_get` |
| `mcp_servers` | `hades connected, source sdk` | same |
| tool_use blocks | `ToolSearch {"query": "select:Bash,Write,Edit"}`, `ToolSearch {"query": "select:mcp__hades__hades_get"}`, `mcp__hades__hades_get {"path": "/v1/tasks"}` | `mcp__hades__hades_get {"path": "/v1/tasks"}` only |
| tool's own call log | one call, `/v1/tasks` | one call, `/v1/tasks` |
| `pwned.txt` written | no | no |
| `only_hades_get_ran` | **false** (ToolSearch ran twice) | **true** |
| Read probe | `Read {"file_path": "/etc/hostname"}` attempted; `permission_denials: [{"tool_name": "Read", ...}]`; the reply said it "was denied" | no tool call; the reply said "I don't have a Read tool available in this session" |
| result | success, 4 model turns, 18.9 s, 0.0668 USD | success, 2 model turns, 9.6 s, 0.0329 USD |

So the scoping the task describes is not enough on its own. Bash, Write and the web
tools are removed, and dontAsk denied the read-only `Read`. But 21 built-ins stay
in the model's tool list, and `ToolSearch` ran without being allowed. In the earlier
runs it ran three, two and five times. Several of the listed built-ins
(`SendMessage`, `RemoteTrigger`, `Workflow`, `CronCreate`) were not attempted, so
whether dontAsk denies them was not exercised. With `tools=[]` the list is exactly
the Hades tool, and only it ran. The model's answer in that run read:

```
1) `id` shell command: Not run. I have no Bash/shell tool available in this session,
   only `mcp__hades__hades_get`.
2) Write pwned.txt: Not done, for the same reason ...
3) hades_get /v1/tasks: Called successfully. Response: {"tasks": [{"id": "FDY-0001", ...}]}
```

The in-process tool runs inside the runner's own Python process. The real Hades client
would therefore hold its credential there, never in the CLI child.

## (c) Interrupt during a long generation, then a new query

`interrupt` uses `include_partial_messages=True`. It sends the codeword turn, then
"Count from 1 to 300 in words, one number per line, then say the codeword." It reads
`StreamEvent`s until 25 `text_delta`s have arrived, calls `await client.interrupt()`,
and reads on to the `ResultMessage`. Then it sends "What was my previous request,
did you finish it, and what is the codeword?" on the same client.

```
first text delta after 29.55 s (25 thinking deltas first), interrupt at 30.73 s
interrupt() returned in 0.00 s; ResultMessage 0.01 s after the call
messages after the interrupt: AssistantMessage, UserMessage, ResultMessage
interrupted result: subtype error_during_execution, is_error true,
  terminal_reason aborted_streaming, num_turns 2, usage all 0
same_child_after_interrupt: true; same_session: true
next turn: success, 2.23 s,
  "Finished: no, I was interrupted partway (stopped around "forty-one").
   Codeword: ORCHID-42"
```

Session file tail (`projects/-tmp-rr-final-interrupt-work/3f5ba363-....jsonl`):

```
user       Count from 1 to 300 in words, one number per line, then say the codeword.
attachment
ai-title
assistant  thinking
assistant  one\ntwo\nthree ... twenty-two\ntwenty...   (the partial text)
user       [Request interrupted by user]
queue-operation enqueue / dequeue
user       What was my previous request, did you finish it, ...
attachment
assistant  Request: count 1-300 in words ... Codeword: ORCHID-42
last-prompt, cost-state
```

So the session continues in the same process, and the transcript marks the stop with
a `[Request interrupted by user]` user line. In this run the partial assistant text
was kept. In the first interrupt run, before the earlier full run, it was not: the tail went `user`
(count), `attachment`, `user` (`[Request interrupted by user]`) with no assistant line
between. Whether partial output survives an interrupt is therefore not reliable, and
Hades should not depend on it.

**The same works without the SDK.** `raw-interrupt` starts `claude -p
--input-format stream-json --include-partial-messages` with FDY-0581's flags. It
writes the SDK's control envelope by hand, without sending the SDK's `initialize`
request first:

```
-> {"type": "control_request", "request_id": "spike_1", "request": {"subtype": "interrupt"}}
<- {"type": "control_response", "response": {"subtype": "success", "request_id": "spike_1",
    "response": {"still_queued": []}}}
result 0.01 s later: error_during_execution, terminal_reason aborted_streaming
next user line: same session_id, "Not finished: I was interrupted partway through
  (stopped around "forty"). Codeword: ORCHID-42"; stdin closed, exit 0
```

The transcript was the same as the SDK's, with the partial text kept. So interrupt is
a CLI capability that both paths reach. FDY-0581 left the in-process interrupt over
stdin unexercised. It is now proven, but only through the SDK's control envelope,
which `claude --help` does not document.

## (d) PreToolUse and Stop hooks

`hooks` uses one client and two turns. Turn 1 asks for `hades_get` on `/v1/tasks`
and then on `/v1/principals`. Turn 2 asks for a plain reply, "done". The PreToolUse,
PostToolUse and Stop callbacks each append a line to `hooks.jsonl`:

```
PreToolUse  mcp__hades__hades_get {"path": "/v1/tasks"}
PostToolUse mcp__hades__hades_get {"path": "/v1/tasks"}
PreToolUse  mcp__hades__hades_get {"path": "/v1/principals"}
PostToolUse mcp__hades__hades_get {"path": "/v1/principals"}
Stop
Stop
pre_tool_use_count 2, post_tool_use_count 2, stop_count 2; tool call log: the same two paths
```

Both hooks fire. PreToolUse fires once per tool call, with the tool's name, input
and `tool_use_id`. Stop fires once per turn, including the turn that used no tool.
The scope run in (b) adds one detail: PreToolUse fired for the `Read` call that
dontAsk then denied, so the hook sees attempts as well as executions. In that run
PostToolUse did not fire for the denied call.

## (e) Prompt cache: one SDK session versus five `claude -p --resume` processes

The same five prompts were sent through `multi-turn` (the SDK session in (a)) and
through `cli-turns`. `cli-turns` runs five processes: turn 1 with `--session-id`,
turns 2 to 5 with `--resume`, using FDY-0581's flags. The figures are from each
turn's result message. In both paths `total_cost_usd` and `duration_api_ms` are
cumulative over the session, including across `--resume` processes, so the cost of a
turn is the step.

| turn | SDK cache write / read | SDK cost step USD | CLI cache write / read | CLI cost step USD | CLI wall s / `duration_ms` |
|---|---|---|---|---|---|
| 1 | 5311 / 3401 | 0.0229362 (of which Haiku 0.000968) | 5312 / 3401 | 0.0219722 | 2.21 / 1119 |
| 2 | 68 / 8712 | 0.0020484 | 68 / 8713 | 0.0020486 | 2.07 / 1215 |
| 3 | 56 / 8780 | 0.0020340 | 56 / 8781 | 0.0020342 | 1.96 / 1138 |
| 4 | 64 / 8836 | 0.0020572 | 64 / 8837 | 0.0020574 | 1.90 / 1021 |
| 5 | 66 / 8900 | 0.0021380 | 66 / 8901 | 0.0021382 | 2.13 / 1259 |
| total | | 0.0312138 | | 0.0302506 | |

The Sonnet cost was 0.0302458 USD in the SDK session (`model_usage`) and 0.0302506
USD across the per-turn processes, and every cache write and read matches within one
token. The prompt cache is held on the server and keyed by the prompt prefix, not by
the process, so per-turn processes lose nothing to a long-lived one. The only cost
difference is one `claude-haiku-4-5` call per SDK session (about 900 to 970 input
tokens, 13 or 14 output tokens, 0.00097 to 0.00104 USD), present in every SDK run and
absent from every CLI run. Sessions the SDK created carry two `ai-title` entries
(`grep -c '"type":"ai-title"'` gave 2 for the `multi-turn` session and the SDK-created
session in (g), 0 for the `cli-turns` session and the CLI-created one in (g)), so the
Haiku call is probably the session title. That link was not verified.

The two paths differ in latency. Per-turn wall time was 1.46 to 2.22 s for the SDK
and 1.90 to 2.21 s for the CLI, and each per-turn process spent 0.8 to 1.1 s outside
its `duration_ms`. Model time steps were 1076, 1113, 1065, 952 and 1159 ms for the
CLI against the SDK's figures in (a). In the earlier run, made with the cache warm
from minutes before, the steps were 1723, 1653, 1691, 1891 and 2214 ms for the CLI
and 2296, 1851, 1691, 3357 and 1825 ms for the SDK. The model time per turn is
therefore run-to-run noise, not a property of the path.

## (f) The SDK's child killed from outside mid-turn

`kill` sends the codeword turn. It then starts the long generation, waits for 25 text
deltas, and sends `os.kill(child_pid, SIGKILL)` from the runner. That is the signal
the OOM killer sends, and the one a pod gets when its termination grace runs out.

```
receive_response():      claude_agent_sdk._errors.ProcessError
                         "Command failed with exit code -9 (exit code: -9)", exit_code -9,
                         raised 0.01 s after the kill
                         (stderr: "Fatal error in message reader: Command failed with exit code -9")
client.query(...) after: claude_agent_sdk._errors.CLIConnectionError
                         "Cannot write to terminated process (exit code: -9)"
client.disconnect():     ok
client.connect() again on the same object: ok, new child pid 2511,
                         but session 49bec346-... (new) and the reply to "what was the codeword"
                         was "This is the first message in our conversation, so there's no prior
                         message containing a codeword."
new ClaudeSDKClient(resume="e81b413f-...") : ok, same session id, 3.88 s,
                         "Previous request: count from 1 to 300 in words, one per line.
                          Status: Not completed ... Codeword: ORCHID-42"
```

The session file after the kill and the resume:

```
user       Count from 1 to 300 in words ...        (the killed turn; no assistant line after it)
user       Continue from where you left off.        isMeta: true
assistant  No response requested.                   model: <synthetic>
user       What was my previous request, ...
assistant  thinking / Previous request: ... Codeword: ORCHID-42
```

So the client cannot be reconnected. Calling `connect()` again on the same object
quietly starts a new, empty session from the same options. That is the dangerous
case: it looks like a reconnect but has lost the conversation. Hades must construct a
new client with `resume=<session_id>` from its own record. The kill loses the partial
reply, as FDY-0581 also found. The resume adds a meta "Continue from where you left
off." user line and a synthetic "No response requested." assistant line without
calling the model. Hades's own turn record has to say the turn was interrupted.

## (g) Resume interchange in one CLAUDE_CONFIG_DIR

`resume-interchange` uses one config directory and one working directory for all
four steps:

| step | process | session id | reply | recalled |
|---|---|---|---|---|
| 1 | `claude -p --session-id 0421429a-...` "The codeword is ORCHID-42" | 0421429a-... | `noted` (exit 0) | |
| 2 | `ClaudeSDKClient(resume="0421429a-...")` "What was the codeword?" | 0421429a-... | `ORCHID-42` | true, same session |
| 3 | new `ClaudeSDKClient()` "The codeword is MAPLE-77" | 5d6c4953-... | `noted` | |
| 4 | `claude -p --resume 5d6c4953-...` "What was the codeword?" | 5d6c4953-... | `MAPLE-77` (exit 0) | true |

Both directions work. Both session files sit in the same directory,
`projects/-tmp-rr-final-resume-interchange-work/`, under the same session ids. The
condition is FDY-0581's: the same `CLAUDE_CONFIG_DIR` and the same `cwd`, because
the directory name is the working directory.

## Recommended runner shape

1. **Rooms**: one `ClaudeSDKClient` per room, held in a Hades runner process. Use
   `cli_path` set to the image's `claude`, `cwd` fixed per principal and
   `CLAUDE_CONFIG_DIR` on the principal's writable volume. Pass the token in
   `options.env`, starting from an environment scrubbed of the parent's `CLAUDE*`
   and `ANTHROPIC*` variables. Set
   `system_prompt={"type": "preset", "preset": "claude_code"}`, or Hades's own prompt.
   Set `tools=[]`, put the Hades client in as `create_sdk_mcp_server` tools with
   `allowed_tools` naming them, keep the `disallowed_tools` list, and use
   `permission_mode="dontAsk"` with `extra_args={"permission-prompts": "none"}`. Each
   injected message is a `client.query()`. Replies stream from `receive_response()`,
   with `include_partial_messages=True` for live text. Stop is `client.interrupt()`,
   and a PreToolUse hook writes Hades's audit record.
2. **When the child dies**: catch `ProcessError` and `CLIConnectionError`, mark the
   turn interrupted in Hades's record, and build a new client with
   `resume=<session_id>`. Never call `connect()` again on the old client.
3. **Scheduled and one-shot runs**: FDY-0581's per-turn `claude -p` with
   `--session-id` and `--resume`, in the same config directory and `cwd`. Because (g)
   works both ways, a room can resume a scheduled run's session and the reverse.
4. **Wiped store**: as in FDY-0581, rebuild from Hades's record in a new session.

## Capabilities not supported, per path

Claude Agent SDK room (`claude-agent-sdk` 0.2.165 over Claude Code 2.1.280):

- Interrupt: works. Whether the partial assistant text of the interrupted turn is
  kept in the transcript varied between runs (kept once, dropped once). The interrupt
  of a turn in the middle of a tool call (`aborted_tools`) was not exercised.
- Outside kill: the client cannot reconnect. `connect()` on the same object starts a
  new empty session without saying so. The partial reply is lost. Recovery is a new
  client with `resume`, which also writes a meta "Continue from where you left off."
  and a synthetic reply into the transcript. The child's pid is reachable only
  through the private `client._transport._process`.
- Resume interchange: works both ways, but only within one `CLAUDE_CONFIG_DIR` and
  one `cwd`. The SDK's `session_store` and `import_session_to_store`, which might
  rebuild a wiped store, were not exercised.
- Tool scoping: `allowed_tools` plus `disallowed_tools` alone leaves 21 built-ins
  listed, and `ToolSearch` runs without being allowed. Only `tools=[]` reduces the
  list to the Hades tools. Network egress is still the pod's, as in FDY-0581.
- Cache: the same as the CLI. The SDK adds one Haiku call per session (about
  0.001 USD). With the SDK's default `system_prompt=None`, the CLI's system prompt is
  replaced by an empty one, which changes the cached prefix.
- Packaging: the SDK is not in the image or in `pyproject.toml`. It needs PyPI (in
  this allowlist) or a vendored wheel. It adds a Python process of about 75 MB per
  room, and it inherits the parent's environment unless that is scrubbed.

Raw `claude -p --input-format stream-json` (Hades drives stdin and stdout itself):

- Interrupt: works through the SDK's `control_request` envelope written by hand. That
  envelope and the `control_response` are not in `claude --help`, so Hades would own
  an undocumented protocol across CLI upgrades.
- Outside kill: as for the SDK (FDY-0581 (e)). The process exits -9 and is restarted
  with `--resume`. Hades must notice the closed stdout itself.
- Resume interchange: the same session files as the per-turn CLI and the SDK.
- Tool scoping: `--tools ""` plus `--allowedTools` and `--disallowedTools` (FDY-0581
  (d)). The Hades client must be a separate stdio MCP server process, because an
  in-process tool needs the SDK's control channel. Hooks are shell commands from a
  settings file; that was not exercised here.
- Cache: the same as the SDK, without the Haiku call.

Per-turn `claude -p` (scheduled runs):

- Interrupt: none inside a turn. The only stop is a signal to the process, with the
  outcome FDY-0581 (e) recorded.
- Outside kill: there is no process between turns to kill. A kill during a turn is
  as above.
- Resume interchange: works both ways with the SDK (g).
- Tool scoping: as for raw stream-json.
- Cache: the same cache reads and cost as a long-lived session (e). About 0.8 to
  1.1 s per turn of process start, plus about 230 MB while a turn runs (FDY-0581).
