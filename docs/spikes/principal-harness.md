# Principal harness spike: Claude Code headless and Codex app-server

Hades #208, accepted first increment, work item 1 (FDY-0581). Run 2026-10-08, 2:28 to
2:36 PM CT, inside a Hades worker Pod on the lab (Kubernetes provider, NFS checkout)
with the worker image's own binaries: Claude Code 2.1.280, Codex CLI 0.156.0, Node
22.16.0. Every number below comes from commands run in that container by
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
egress allowlist, and an unauthenticated turn hangs in a reconnect loop instead of
failing; so no authenticated Codex turn was proven here, and that is the first
unsupported capability on that path.

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

Claude: in the long-lived process a third turn ("count from 1 to 40 in words, then
say the codeword") was sent and the process got SIGKILL after its first `assistant`
event (`events_seen_before_kill: ["system", "assistant"]`, exit -9). The session file
afterwards ended `..., user, attachment, assistant, queue-operation, queue-operation,
user, attachment` with the interrupted request as the last `user` line and no
assistant line after it; the partial assistant text was not recorded. A new process
with `--resume <session>` answered the question "what was my last request, did you
finish it, what is the codeword" with:

```
Last request: Count from 1 to 40 in words, one per line, then say the codeword.
Finished: No, I did not complete it.
Codeword: ORCHID-42
```

So the CLI neither replays the interrupted turn nor loses the conversation: the
unanswered user line is the marker of an interrupted generation, and the next prompt
decides what happens. Side effects a tool had already done before the kill would
stand (none here: no tools). Hades's `principal_turns` row, not the session file,
must say the turn was interrupted; the session file only shows a user line without an
answer, which a resume alone does not surface.

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

Why not Codex first: no credential is mounted in a worker today unless the attempt
is a Codex attempt, the ChatGPT backend is not in the Pod's allowlist, an
unauthenticated turn hangs rather than fails, the host has no resume, and the sandbox
that would scope tools cannot run in the worker (S2). Everything else on that path
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
- Mid-turn kill: the partial assistant output is not in the session file; only the
  unanswered user line is. An in-process interrupt of a running turn over stdin was not
  exercised.
- Credential refresh: none; the token is long-lived and expiry means a new login.
  Concurrent use by a worker was fine in this run.

Codex app-server (0.156.0):

- Subscription auth: no credential in a non-Codex worker; chatgpt.com and
  api.openai.com not in this Pod's allowlist; no authenticated turn proven. An
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
