# Principal harness spike: Claude Code headless and Codex app-server

Hades #208, accepted first increment, work item 1 (FDY-0581). Original run 2026-10-08, 2:28 to
2:36 PM CT, inside a Hades worker Pod on the lab (Kubernetes provider, NFS checkout)
with the worker image's own binaries: Claude Code 2.1.280, Codex CLI 0.156.0, Node
22.16.0. The original measurements below were recorded by the first attempt using
`tools/spikes/principal_harness.py`; the raw transcripts were under
`/tmp/spike/out` in the Pod and are quoted here. The script runs again with
`python3 tools/spikes/principal_harness.py all` in any worker that has the mounts.

Decision: **the first principal harness is Claude Code headless, one `claude -p`
process per turn, with `--session-id` chosen by Hades, `--resume` as a cache and
a rebuild from Hades's own message record when the resume fails.** The long-lived
`--input-format stream-json` form of the same process also works and is measured
below; it saves about 1.2 s and one process start per turn at the cost of about
215 MB resident between turns, and can replace the per-turn form later behind the
same flags. Codex app-server is the second path: its JSON-RPC surface does what the
spike asked (thread resume from its rollout file, approval policy and sandbox at
`thread/start`, clean SIGTERM exit) but no Codex credential is mounted in a worker
that was not launched as a Codex worker, the ChatGPT backend is outside this Pod's
egress allowlist in that original Pod. The correction run below authenticated Codex
in memory, but its requested model was refused. Neither run proves a successful
Codex model turn. The recommendation remains provisional on that limitation.

## What the worker had

```
$ claude --version            -> 2.1.280 (Claude Code)
$ codex --version             -> codex-cli 0.156.0
$ grep /home/worker /proc/mounts
tmpfs /home/worker/.claude tmpfs ro,relatime,size=4194304k,inode64,noswap 0 0
$ ls -laL /home/worker/.claude
-r--r----- 1 root worker 423 Oct  8 19:24 .claude.json
-r--r----- 1 root worker 109 Oct  8 19:24 oauth-token      # opaque sk-ant-oat01 string
-r--r--r-- 1 root worker   3 Oct  8 19:24 settings.json    # "{}"
$ find /home/worker/.codex -maxdepth 3 -> only tmp/arg0/...; no auth.json, no access-token.json
$ CODEX_HOME=/home/worker/.codex codex login status -> Not logged in
$ env | grep -c '^CLAUDE_CODE_OAUTH_TOKEN='                -> 0 (the launch wrapper exports it
                                                              to the harness only; the CLI does
                                                              not pass it to its children)
```

The Claude credential is a read-only tmpfs projection of the attempt Secret, delivered
to the CLI as `CLAUDE_CODE_OAUTH_TOKEN` by the worker launch script (the `claude_code`
adapter's `CredentialSpec`). `CLAUDE_CONFIG_DIR` points at that read-only directory, so
the worker's own session is never written anywhere: `find / -name '*.jsonl' -path
'*projects*'` found nothing while this task's own `claude` ran. The spike therefore ran
its Claude processes with `CLAUDE_CONFIG_DIR` set to a writable scratch directory and
the token read from the mounted file into the child's environment only (never copied
to disk, never printed).

The Codex credential is absent because Hades mounts only the launched harness's
credential (this task ran under `claude_code`). In a Codex worker the renewer path
mounts `access-token.json` (`access_token`, `account_id`, `expires_at`) and
`crucible-codex-host` feeds it to app-server as `chatgptAuthTokens`
(docs/implementation-notes/codex-credential-renewer.md).

## (a) Claude Code: resume across processes, with and without the session store

Command shape (flags shared by every spike turn; `--tools ""` removes every built-in
tool, `dontAsk` plus `--permission-prompts none` denies anything that would prompt):

```
claude -p --output-format stream-json --verbose --model claude-sonnet-5 \
  --tools "" --permission-mode dontAsk --permission-prompts none \
  --session-id <uuid>            # turn 1, prompt on stdin
claude -p ... --resume <uuid>    # turn 2, new process
rm -rf $CLAUDE_CONFIG_DIR/projects
claude -p ... --resume <uuid>    # turn 3, new process, store wiped
claude -p ... --session-id <new> # turn 4, prompt carries the record so far
```

Observed (`python3 tools/spikes/principal_harness.py claude-resume`):

| turn | process | exit | wall s | peak RSS kB | model ms | answer |
|---|---|---|---|---|---|---|
| 1 `--session-id` "the codeword is ORCHID-42" | new | 0 | 3.15 | 227948 | 1976 | `noted` |
| 2 `--resume` "what is the codeword" | new | 0 | 2.92 | 233004 | 1951 | `ORCHID-42` |
| 3 `--resume` after `rm -rf projects/` | new | 1 | 0.77 | 206848 | 0 | none |
| 4 new session, record in the prompt | new | 0 | 3.36 | 229780 | 2374 | `ORCHID-42` |

Turn 1 wrote `projects/-tmp-spike-out-claude-resume-work/<session>.jsonl` under
`CLAUDE_CONFIG_DIR`. Turn 3's stderr was
`No conversation found with session ID: 43b7b864-14e6-44cc-8aeb-ccd13cac4dd9` and its
only stdout line was a `result` with `subtype: error_during_execution`, `is_error:
true`, `num_turns: 0`. So: a resume continues the conversation across two processes
when the session file is there, is a hard error when it is not, and the turn must then
be rebuilt from Hades's own record (turn 4, which is a new session id; the model's
hidden state and the prompt cache are not carried, only the words). The session
directory is keyed by the working directory, so the principal's turns must run from
one fixed `cwd`. `--session-id` lets Hades pick the id, so a `principal_turns` row can
record it before the process starts.

## (b) Codex app-server over JSON-RPC: thread resume

Driven the way `images/worker/crucible-codex-host.py` drives it (`codex app-server
--disable plugins -c check_for_update_on_startup=false`, `initialize` with
`experimentalApi`, `initialized`, then requests on stdin), with `CODEX_HOME` on
scratch, no credential (`python3 tools/spikes/principal_harness.py codex-probe`):

| request | result |
|---|---|
| `initialize` | response in 0.05 s; `userAgent principal-spike/0.156.0`, `codexHome` echoed |
| `account/read` | `{"account": null, "requiresOpenaiAuth": true}` |
| `thread/start` `{cwd, approvalPolicy: "never", sandbox: "read-only", model: "gpt-5-codex"}` | thread id `01a11d02-04da-...`, `path` = `sessions/2026/10/08/rollout-...jsonl` under `CODEX_HOME`, `status idle`; works without a login |
| `turn/start` with the codeword | accepted (`status inProgress`), then notifications `thread/status/changed`, `turn/started`, `item/started`, `item/completed`, `error` with `"Reconnecting... 2/5"`, `responseStreamDisconnected`, `request timed out`, `willRetry: true`; no `turn/failed` within 20 s |
| `thread/resume` with a random id | `{"error": {"code": -32600, "message": "no rollout found for thread id ..."}}` |
| SIGTERM | exit 0 |
| second process, `thread/resume` with the real id | the thread back, `preview: "The codeword is ORCHID-42."`, same rollout path; `thread/list` lists it |
| `rm -rf $CODEX_HOME/sessions`, third process, `thread/resume` | `-32600 no rollout found for thread id 01a11d02-...` |
| SIGKILL | exit -9 |

So Codex resume is the rollout file: it survives a process restart, keeps the user
input of a turn that never got an answer, and dies with the storage. There is no
import call; a wiped store means a new `thread/start` with the record in the first
input, as for Claude. One observation to check when a credential is present: the
resumed thread reported `model: "gpt-6-astra"` where `thread/start` had reported
`gpt-5-codex` (the CLI's default substituted on resume; `thread/resume` takes its own
model parameter). `crucible-codex-host` today has no resume path at all: one process is
one `thread/start` plus one `turn/start`.

## (c) Long-lived process versus a process per turn

Claude, one process fed over stdin
(`python3 tools/spikes/principal_harness.py claude-long-lived`, flags as in (a) plus
`--input-format stream-json`; each user message is one
`{"type":"user","message":{"role":"user","content":[{"type":"text","text":...}]}}` line):

| measurement | value |
|---|---|
| time from spawn to `system init` (emitted only after the first user line) | 0.56 s |
| turn 1 wall / model `duration_ms` | 1.78 s / 1806 |
| turn 2 (codeword recalled in the same process) wall / model | 2.00 s / 1996 |
| RSS idle after turn 1 / after turn 2 | 214368 kB / 215468 kB |
| peak RSS over the run | 226212 kB |
| this task's own `claude -p` after about ten minutes of work (pid 30) | 271240 kB RSS, 11 threads |

Claude, one process per turn (table in (a)): about 3.0 s wall for a 2.0 s model
call, so about 1.2 s of start and teardown per turn, and 207 to 233 MB peak for the
life of the turn, nothing between turns.

Codex app-server: 0.05 s to the `initialize` response (0.02 to 0.03 s for the second
and third processes), 85972 kB RSS idle after `initialized`, 134508 kB peak while a
turn was attempted. No authenticated turn, so no model latency.

Reading: a long-lived Claude process costs about 215 MB resident continuously and
saves about 1.2 s per turn; a per-turn process costs nothing between turns and about
230 MB and 1.2 s while one runs. For a principal that idles for hours between wakes the
per-turn shape is cheaper and simpler to supervise; the long-lived shape is a
latency optimisation with the same flags.

## (d) Tool scoping to one tool

Claude (`python3 tools/spikes/principal_harness.py claude-scope`). The Hades HTTP
client was stood in for by a stdio MCP server with one tool, `hades_get` (the script's
`mcp-stub` subcommand). Exact flags that worked:

```
claude -p --output-format stream-json --verbose --model claude-sonnet-5 \
  --tools "" --permission-mode dontAsk --permission-prompts none \
  --strict-mcp-config --mcp-config mcp.json \
  --allowedTools mcp__hades__hades_get \
  --disallowedTools Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Agent
```

The prompt asked for three things: run `id`, write `pwned.txt`, call `hades_get
/v1/tasks`. Observed: `system init` listed `tools: ["mcp__hades__hades_get"]` and
`mcp_servers: [{"name": "hades", "status": "connected"}]`; the only `tool_use` was
`mcp__hades__hades_get {"path": "/v1/tasks"}`, the stub's call log has exactly that
call, the marker file was not written (`marker_file_written: false`), and the model
reported "I don't have a Bash/shell execution tool ... only `mcp__hades__hades_get`"
and "I don't have a Write or file-editing tool". Exit 0, 8.0 s, 253344 kB peak, two
model turns, cost 0.0426 USD.

Network: no CLI flag limits where the process itself may connect; the process needs
api.anthropic.com for the model, and the Hades URL is reached only through the MCP
server. The Pod's egress allowlist (today `api.anthropic.com,...` from
`CRUCIBLE_EGRESS_ALLOWLIST`) is the control, so a principal Pod's allowlist is
api.anthropic.com plus the Hades service and nothing else (#205). Two further points
from `claude --help` on 2.1.280, not exercised: `--restricted` also removes the
command tools and WebFetch and refuses `bypassPermissions`; `--bare` cannot be used
with the subscription token ("Anthropic auth is strictly ANTHROPIC_API_KEY ... OAuth
... never read").

Codex: `thread/start` accepted `approvalPolicy: "never"` and `sandbox: "read-only"`
without a login. Whether the read-only sandbox then holds inside the worker was not
exercised (no credential; S2 found Codex's own sandbox cannot run in the worker, which
is why Hades launches with `danger-full-access` today). The binary carries
`features.shell_tool` and an MCP server table in `config.toml`, but no flag was
proven here that removes the shell tool the way `--tools ""` does for Claude.

## (e) Killed mid-turn and restarted

Claude: the original run killed after the first complete `assistant` event for
"count from 1 to 40". It did **not** prove a kill during generation. The reported
unanswered user line and the resumed model's claim that it had not finished are
observations about the interval before result/session finalization; they do not
establish what happens to partial output. The earlier conclusion about lost partial
assistant text is withdrawn.

The corrected command adds `--include-partial-messages`. It asks for 400 lines and
kills immediately on `stream_event.event.type = content_block_delta` with
`delta.type = text_delta`, before a complete `assistant` or `result` event. It records
the triggering delta and whether either completion event was consumed. If no delta
arrives, it reports `no_delta_observed`, not a successful mid-generation experiment.
A regression test verifies both a delta and an early complete assistant event.
The correction worker has no Claude credential: its live attempt returned
`Not logged in · Please run /login`, so actual mid-generation recovery remains
**not exercised**. No login was attempted. Hades must track interruption in its own
turn record; this spike does not prove safe replay of side effects.

Codex: SIGTERM exited 0, SIGKILL -9, and the thread with its unanswered input
resumed from the rollout in the next process (table in (b)). What `thread/resume` does
with a turn that was `inProgress` at the kill, with a credential, was not exercised.

## (f) Credential refresh while a worker runs

`python3 tools/spikes/principal_harness.py credentials`, before and after the runs
above:

```
.claude.json   size 423  mode 0o440  mtime 2026-10-08T19:24:33Z
oauth-token    size 109  mode 0o440  mtime 2026-10-08T19:24:33Z
settings.json  size 3    mode 0o444  mtime 2026-10-08T19:24:33Z
mount: tmpfs /home/worker/.claude ro
codex: /home/worker/.codex holds only tmp/; codex login status -> Not logged in
```

The mtimes are the Pod's start and never changed. The Claude token is the long-lived
`setup-token` credential (an opaque `sk-ant-oat01` string, no refresh token beside
it), so there is nothing for the CLI to refresh and the read-only mount could not take
a rewrite anyway. Concurrency on one credential: this task's own `claude` process and
the spike's nine Claude processes ran on the same token at the same time (the
long-lived one held it for the whole experiment) and every one authenticated; no
`rate_limit_event` with status `rejected`, no auth pattern. What was not observed:
expiry of the token (it is long-lived; when it does expire the fix is a new
`setup-token` login, there is no refresh in a worker), and anything on the Codex side.
Codex refresh in a worker is the renewer's: Hades refreshes `auth.json` at 75 percent
of the JWT lifetime, projects a new `access-token.json`, and app-server asks its client
for the new token with `account/chatgptAuthTokens/refresh`, which the host answers by
re-reading the file (90 s wait). A principal on Codex would need that same host loop
alive for the whole conversation; not exercised.

## Recommended first harness

Claude Code headless, per-turn process:

1. `claude -p --output-format stream-json --verbose --model <m> --tools ""
   --permission-mode dontAsk --permission-prompts none --strict-mcp-config
   --mcp-config <hades client> --allowedTools mcp__hades__* --disallowedTools
   Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Agent --session-id <id>`
   for the first turn and `--resume <id>` after, from one fixed working directory.
2. `CLAUDE_CONFIG_DIR` on a writable per-principal volume (the session cache), the
   token still delivered as `CLAUDE_CODE_OAUTH_TOKEN` from the read-only Secret mount.
3. When `--resume` exits 1 with `No conversation found`, start a new session id with
   the conversation record in the prompt and record the new id on the turn.
4. On restart with a turn `running`, treat an unanswered user line as interrupted;
   never resend the same prompt as if new.

Why not Codex first: the original worker lacked a Codex credential and backend
egress; the correction worker authenticated but its model was refused. No successful
model turn or enforced tool restriction was proven. The host has no resume, and S2
reported the sandbox could not run in its worker. Everything else on that path
(resume from the rollout, approval policy at `thread/start`, clean SIGTERM, small
idle footprint of 86 MB) looked usable.

## Capabilities not supported for a hosted principal

Claude Code headless (2.1.280):

- Resume: only from its own session file in `CLAUDE_CONFIG_DIR/projects/<cwd>`;
  a wiped volume is a hard error, and there is no call that imports a transcript, so a
  rebuild is a new session id with the record as text (hidden state and prompt cache
  lost). The worker's current read-only `CLAUDE_CONFIG_DIR` cannot persist a session at
  all. A later launch with different `--append-system-prompt` text is ignored on
  resume until compaction (`--system-prompt-snapshot`, from `--help`, not exercised).
- Tool scoping: no per-host network limit in the CLI; egress is the Pod's. `--bare`
  is unavailable with the subscription token. The Hades client must be an MCP server.
- Mid-turn kill: genuine interruption during generation and persistence of partial
  output remain unproven. The original test killed after a complete assistant event;
  the corrected delta-triggered test could not authenticate in the correction worker.
  An in-process interrupt over stdin was not exercised.
- Credential refresh: none; the token is long-lived and expiry means a new login.
  Concurrent use by a worker was fine in this run.

Codex app-server (0.156.0):

- Subscription auth: absent in the original non-Codex worker. The correction worker
  authenticated with its mounted token over stdin, but `gpt-5-codex` was rejected
  for the ChatGPT account; no successful model turn proven. In the original Pod, an
  unauthenticated or unreachable turn retries ("Reconnecting... n/5") instead of
  failing within 20 s.
- Resume: rollout file only; wiped store is `-32600 no rollout found`; no import;
  `crucible-codex-host` has no resume; the model recorded on the thread changed on
  resume (`gpt-5-codex` to `gpt-6-astra`), unexplained.
- Tool scoping: `approvalPolicy never` and `sandbox read-only` are accepted but the
  sandbox cannot run in the worker (S2), so enforcement is not proven; no proven way to
  remove the shell tool.
- Mid-turn kill: process exits cleanly; resume of a turn that was in progress not
  exercised.
- Credential refresh: by the host answering `account/chatgptAuthTokens/refresh` from
  the projected file; not exercised here.


## Correction run: credential handling, kill trigger and deadlines

Run 2026-10-08, approximately 6:38 to 6:44 PM CT, in the correction worker while
its existing Codex operator process was running. Read issue #208 and both comments;
this correction stays within work item 1. Binaries remain Claude 2.1.280 and
Codex 0.156.0. Original measurements above are retained as historical evidence,
except the invalid Claude mid-generation inference explicitly withdrawn in (e).

Commands actually run:

```sh
python3 tools/spikes/principal_harness.py --out /tmp/principal-correction credentials
python3 tools/spikes/principal_harness.py --out /tmp/principal-correction claude-long-lived
python3 tools/spikes/principal_harness.py --out /tmp/principal-correction codex-probe
python3 tools/spikes/principal_harness.py --out /tmp/principal-correction-final codex-probe
python3 tools/spikes/principal_harness.py --out /tmp/principal-correction-after credentials
uv run pytest tests/unit/test_spike_principal_harness.py -q
```

Claude has no `/home/worker/.claude` mount. The long-lived command accepted the
partial-message flag, emitted system init in 0.31 s, then `is_error: true` with
`Not logged in · Please run /login`; `kill_probe` says
`not exercised: first turn failed or timed out`. No new Claude generation or
recovery measurements can be claimed from this worker.

Codex has `auth.json` on its mounted credential directory, but no
`access-token.json`. The script now uses an empty scratch `CODEX_HOME` and reads
only the access token and account id into memory, preferring the projected
`access-token.json` and otherwise reading `auth.json.tokens`. Neither credential
file nor `config.toml` is copied. After `initialize` with `experimentalApi: true`
and `initialized`, each process receives this request over its stdin pipe (values
below are placeholders, and outbound requests are never logged):

```json
{"id":2,"method":"account/login/start","params":{"type":"chatgptAuthTokens","accessToken":"<mounted access token>","chatgptAccountId":"<mounted account id>"}}
```

This supplies existing tokens; it does not initiate a browser/device login or use
refresh tokens. The [official app-server authentication documentation](https://developers.openai.com/codex/app-server)
describes external token authentication; the local implementation follows
`crucible-codex-host.py`. The probe redacts supplied token/account values from
responses before logging, discards stderr, and never logs outgoing auth requests.
The scratch directory remains a conversation store, not a credential store.

The first rerun exposed missing `experimentalApi` on process 2 and 3, which refused
authentication. This was fixed before the final rerun. Final observations:

| measurement or request | observed result |
|---|---|
| initialize, process 1 / process 2 | 0.05 s / 0.03 s |
| external auth, all three processes | `result: {"type":"chatgptAuthTokens"}`, accepted |
| idle RSS / peak RSS, first process | 96432 kB / 170360 kB |
| `thread/start`, approval `never`, sandbox `read-only` | accepted |
| model `gpt-5-codex`, first turn | `turn/completed`, status `failed`, after 2084 ms |
| backend error | `The 'gpt-5-codex' model is not supported when using Codex with a ChatGPT account.` |
| resume in process 2 | same thread, preview `The codeword is ORCHID-42.` |
| resume after deleting sessions in process 3 | `-32600 no rollout found for thread id ...` |
| SIGTERM / SIGKILL | 0 / -9 |

No successful model turn, same-process second answer, genuine Codex mid-generation
kill, or sandbox enforcement was measured. These remain unsupported or unproven
capabilities for this spike, not claims that every Codex model refuses subscription
auth. The final SIGKILL was after a failed resume, not during generation.

A read-only check of the final scratch tree found no `auth.json`,
`access-token.json` or `config.toml`, and an in-memory comparison found no mounted
access token in any scratch file (`False`; the token was never printed).
The credential's mtime stayed `2026-10-08T23:38:10Z` (6:38:10 PM CT), size 4131,
mode 0600, between the before/after observations. This Codex mount is writable NFS,
unlike the original Claude mount. No refresh request or token-file change was
observed while the operator and probe used the same credential. This does not prove
expiry/refresh safety. The spike does not implement the production host's refresh
callback; it reports the bounded turn outcome if refresh is needed.

Both RPC response waits and notification drains now read via a selectable descriptor
and an explicit byte buffer, so a silent peer or an unterminated JSON line cannot
block past the deadline. App-server stderr is discarded to prevent pipe backpressure.
An `ExitStack` guarantees child teardown on exceptions; stop waits for the killed
child and closes pipes. Tests exercise silent and partial-line peers for both call
and drain, with 0.1 s deadlines, plus a peer writing 100000 stderr bytes. They verify
return within 2 s and that the child and RSS polling thread stop. Other regressions
exercise both mounted credential formats, deliberately echo secrets in replies to
verify redaction, and verify the Claude delta trigger. These are synthetic protocol
tests, not authenticated-generation evidence.

## Review findings and disposition

The following findings are preserved verbatim. All three are fixed; the live Claude
mid-generation result is explicitly left unproven because this worker has no token.

Finding 01M4EY1ZW5F9T7VHXS6XJHFXVF
Path: tools/spikes/principal_harness.py
Line: 635
Body:
**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)</sub></sub>  Keep Codex credentials out of the scratch directory**

When this probe is rerun in a Codex worker, this block copies `auth.json` and `access-token.json`—including reusable subscription secrets—into the user-selectable `--out` tree, where they remain after the experiment and may reside on persistent or shared storage. This directly defeats the spike's requirement to read but never copy credentials; authenticate app-server from the mounted token in memory, as `images/worker/crucible-codex-host.py` does, instead of cloning credential files.

Useful? React with 👍 / 👎.

Finding 01M4EY1ZW8CM7Z70XDV4H510XG
Path: tools/spikes/principal_harness.py
Line: 388
Body:
**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)</sub></sub>  Kill before the complete assistant message arrives**

This does not exercise a kill during generation: the command does not enable partial messages, whose default is false according to the [official Agent SDK reference](https://docs.claude.com/it/api/agent-sdk/python), and ordinary stream-JSON emits an `assistant` Message before the final `result`. Because tools are disabled here, the first `assistant` event is the completed counting response, so SIGKILL only tests the gap between the completed answer and result/session finalization; the document's conclusions about partial output and genuine mid-turn recovery are therefore unsupported. Enable partial-message events and kill on an actual streaming delta, or kill on a timer after the turn starts.

Useful? React with 👍 / 👎.

Finding 01M4EY1ZWA9DDYF04K57TRBKKX
Path: tools/spikes/principal_harness.py
Line: 569
Body:
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Enforce timeouts around blocking JSON-RPC reads**

If app-server becomes silent before replying or completing a turn—especially in the unreachable or unauthenticated scenario this probe intentionally exercises—`readline()` blocks indefinitely, so the deadline is never rechecked and the advertised 20/30/60-second timeouts cannot terminate the experiment. Use a selectable descriptor, reader thread/queue, or another bounded-read mechanism so `codex-probe` and `all` can reliably time out and clean up the child.

Useful? React with 👍 / 👎.
