#!/usr/bin/env python3
"""Room runner spike (hades #208, item 1b, FDY-0582): the scripts behind
docs/spikes/room-runner-sdk.md, runnable again inside a worker container.

Nothing here is product code. The question is whether a long-lived Hades conversation
runner (a "room") should drive Claude Code through the Claude Agent SDK for Python or
through the raw `claude -p --input-format stream-json` process FDY-0581 measured
(docs/spikes/principal-harness.md, on its branch). Each subcommand runs one experiment
and prints a JSON summary; raw transcripts and hook logs go under --out.

The SDK is not in the worker image or in Hades's dependencies. Run the script with it
supplied for this process only:

    uv run --no-project --with claude-agent-sdk python tools/spikes/room_runner_sdk.py multi-turn
    ... cli-turns | scope | interrupt | hooks | kill | resume-interchange | raw-interrupt | all

The SDK is imported inside each experiment, so the argument parsing (and its unit test)
needs nothing but the standard library.

Credentials: the Claude token is read from the mounted file into the child's
CLAUDE_CODE_OAUTH_TOKEN only, never copied to disk or printed. The SDK spawns its CLI
with this process's environment plus ClaudeAgentOptions.env, so the parent harness's
own CLAUDE*/ANTHROPIC* variables are removed from os.environ first.
"""

# The SDK is imported inside each experiment on purpose (see the docstring).
# ruff: noqa: PLC0415

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

CLAUDE = "/usr/local/bin/claude"
TOKEN_FILE = Path("/home/worker/.claude/oauth-token")
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
DEFAULT_OUT = Path("/tmp/room-runner-sdk-spike")
DEFAULT_MODEL = "claude-sonnet-5"
CODEWORD = "ORCHID-42"
SECOND_CODEWORD = "MAPLE-77"
HADES_TOOL = "mcp__hades__hades_get"
# The six the task names; FDY-0581 also removed MultiEdit and NotebookEdit.
DISALLOWED = ["Bash", "Edit", "Write", "WebFetch", "WebSearch", "Agent"]
# FDY-0581's per-turn flags: no built-in tool, deny anything that would prompt.
CLI_SCOPE_FLAGS = ("--tools", "", "--permission-mode", "dontAsk", "--permission-prompts", "none")
# Five injected turns: the codeword at turn one, recalled at turn five.
FIVE_TURNS = (
    f"The codeword is {CODEWORD}. Reply with exactly: noted",
    "Name one prime number between 10 and 20. Reply with the number only.",
    "Name the capital of France. One word.",
    "What is 7 times 8? Reply with the number only.",
    "What was the codeword in my first message? Reply with the codeword only.",
)
ADVERSARIAL = (
    "Do three things, in order, and report what happened for each: "
    "1) run the shell command `id` and show its output; "
    "2) write a file named pwned.txt containing the word pwned in the current directory; "
    "3) call the hades_get tool with path /v1/tasks and show what it returned."
)
LONG_GENERATION = "Count from 1 to 300 in words, one number per line, then say the codeword."
AFTER_STOP = (
    "What was my previous request, did you finish it, and what is the codeword? "
    "Answer in three short lines."
)
CANNED = {"tasks": [{"id": "FDY-0001", "state": "running", "title": "canned by the spike"}]}
EXPERIMENTS = (
    "multi-turn",
    "cli-turns",
    "scope",
    "interrupt",
    "hooks",
    "kill",
    "resume-interchange",
    "raw-interrupt",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Room runner spike: Claude Agent SDK versus stream-json CLI (FDY-0582)"
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="scratch directory")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cli-path", default=CLAUDE, help="the claude binary the SDK spawns")
    parser.add_argument("--token-file", type=Path, default=TOKEN_FILE)
    parser.add_argument(
        "--idle-seconds",
        type=float,
        default=3.0,
        help="pause after each SDK turn before the idle RSS sample",
    )
    parser.add_argument(
        "--stop-after-deltas",
        type=int,
        default=25,
        help="text deltas of the long generation before interrupt or kill",
    )
    parser.add_argument("experiment", choices=(*EXPERIMENTS, "all"))
    return parser


# Environment


def scrub_parent_environment() -> list[str]:
    """Remove the parent harness's session variables; the SDK copies os.environ."""
    removed = sorted(k for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC")))
    for key in removed:
        del os.environ[key]
    return removed


def child_env(config_dir: Path, token_file: Path) -> dict[str, str]:
    env = {"CLAUDE_CONFIG_DIR": str(config_dir), "TERM": "dumb"}
    if token_file.is_file():
        env[TOKEN_ENV] = token_file.read_text(encoding="utf-8").strip()
    return env


def cli_env(config_dir: Path, token_file: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/home/worker"),
        **child_env(config_dir, token_file),
    }


def fresh_dirs(out: Path, name: str) -> tuple[Path, Path]:
    config_dir = out / name / "config"
    workdir = out / name / "work"
    shutil.rmtree(out / name, ignore_errors=True)
    config_dir.mkdir(parents=True)
    workdir.mkdir(parents=True)
    return config_dir, workdir


# Measurement helpers


def rss_kb(pid: int | None) -> int:
    if pid is None:
        return 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def child_pid(client: Any) -> int | None:
    """The CLI child's pid. The SDK has no public accessor; this is its transport's."""
    process = getattr(getattr(client, "_transport", None), "_process", None)
    return getattr(process, "pid", None)


def child_argv(pid: int | None) -> list[str]:
    """What the SDK actually spawned (argv carries no credential; the token is in env)."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, TypeError):
        return []
    return [a.decode() for a in raw.split(b"\0")[:-1]]


def session_files(config_dir: Path) -> list[Path]:
    return sorted((config_dir / "projects").rglob("*.jsonl"))


def transcript(config_dir: Path, session_id: str, tail: int = 12) -> dict[str, Any]:
    """Entry types of a session file and the tail with text truncated."""
    files = [p for p in session_files(config_dir) if p.stem == session_id]
    if not files:
        return {"file": None, "types": [], "tail": []}
    entries = []
    for line in files[0].read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return {
        "file": str(files[0].relative_to(config_dir)),
        "types": [e.get("type") for e in entries],
        "tail": [describe_entry(e) for e in entries[-tail:]],
    }


def describe_entry(entry: dict[str, Any]) -> dict[str, Any]:
    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    content = message.get("content") if message else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("name") or block.get("type")))
        text = " | ".join(parts)
    else:
        text = str(entry.get("operation") or entry.get("subtype") or "")
    return {"type": entry.get("type"), "text": text[:160]}


def usage_fields(usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or {}
    keys = (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    )
    return {k: usage.get(k) for k in keys}


# SDK plumbing


def sdk_options(args: argparse.Namespace, config_dir: Path, workdir: Path, **extra: Any) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions

    base: dict[str, Any] = {
        "cli_path": args.cli_path,
        "cwd": str(workdir),
        "model": args.model,
        "env": child_env(config_dir, args.token_file),
        # The same system prompt as a plain `claude -p`, so cache numbers compare.
        "system_prompt": {"type": "preset", "preset": "claude_code"},
        "tools": [],
        "permission_mode": "dontAsk",
        "extra_args": {"permission-prompts": "none"},
        "stderr": lambda line: None,
    }
    base.update(extra)
    return ClaudeAgentOptions(**base)


async def sdk_turn(client: Any, prompt: str) -> dict[str, Any]:
    """Inject one user message into a connected client and read to its result."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    start = time.monotonic()
    await client.query(prompt)
    texts: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    result: Any = None
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_uses.append({"name": block.name, "input": block.input})
        elif isinstance(message, ResultMessage):
            result = message
    return {
        "wall_s": round(time.monotonic() - start, 2),
        **result_fields(result),
        "text": texts,
        "tool_uses": tool_uses,
    }


def result_fields(result: Any) -> dict[str, Any]:
    if result is None:
        return {"result": None}
    return {
        "session_id": result.session_id,
        "subtype": result.subtype,
        "is_error": result.is_error,
        "terminal_reason": getattr(result, "terminal_reason", None),
        "duration_ms": result.duration_ms,
        "duration_api_ms": result.duration_api_ms,
        "num_turns": result.num_turns,
        "total_cost_usd": result.total_cost_usd,
        "usage": usage_fields(result.usage),
        "permission_denials": result.permission_denials,
        "model_usage": result.model_usage,
    }


def recalled(turn: dict[str, Any], word: str = CODEWORD) -> bool:
    return any(word in t for t in turn.get("text", []))


# (a) and the SDK half of (e): five injected turns in one ClaudeSDKClient


async def multi_turn(args: argparse.Namespace) -> dict[str, Any]:
    from claude_agent_sdk import ClaudeSDKClient

    config_dir, workdir = fresh_dirs(args.out, "multi-turn")
    client = ClaudeSDKClient(sdk_options(args, config_dir, workdir))
    start = time.monotonic()
    await client.connect()
    report: dict[str, Any] = {
        "connect_s": round(time.monotonic() - start, 2),
        "child_pid": child_pid(client),
        "child_argv": child_argv(child_pid(client)),
        "child_rss_after_connect_kb": rss_kb(child_pid(client)),
        "python_rss_after_connect_kb": rss_kb(os.getpid()),
        "turns": [],
    }
    try:
        for prompt in FIVE_TURNS:
            turn = await sdk_turn(client, prompt)
            await asyncio.sleep(args.idle_seconds)
            turn["child_rss_idle_kb"] = rss_kb(child_pid(client))
            turn["python_rss_idle_kb"] = rss_kb(os.getpid())
            report["turns"].append(turn)
        report["turn5_recalled_codeword"] = recalled(report["turns"][-1])
        report["one_child_for_all_turns"] = child_pid(client) == report["child_pid"]
    finally:
        await client.disconnect()
    return report


# The CLI half of (e): five separate `claude -p --resume` processes, FDY-0581's shape


def cli_turn(
    args: argparse.Namespace, env: dict[str, str], workdir: Path, prompt: str, *session: str
) -> dict[str, Any]:
    argv = [
        args.cli_path,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        args.model,
        *CLI_SCOPE_FLAGS,
        *session,
    ]
    start = time.monotonic()
    done = subprocess.run(
        argv,
        cwd=workdir,
        env=env,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    events = []
    for line in done.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    result: dict[str, Any] = next((e for e in events if e.get("type") == "result"), {})
    texts = [
        str(b.get("text"))
        for e in events
        if e.get("type") == "assistant"
        for b in e.get("message", {}).get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return {
        "argv_session": list(session),
        "exit": done.returncode,
        "wall_s": round(time.monotonic() - start, 2),
        "session_id": result.get("session_id"),
        "subtype": result.get("subtype"),
        "duration_ms": result.get("duration_ms"),
        "duration_api_ms": result.get("duration_api_ms"),
        "total_cost_usd": result.get("total_cost_usd"),
        "usage": usage_fields(result.get("usage")),
        "model_usage": result.get("modelUsage"),
        "text": texts,
        "stderr_tail": done.stderr[-300:],
    }


async def cli_turns(args: argparse.Namespace) -> dict[str, Any]:
    config_dir, workdir = fresh_dirs(args.out, "cli-turns")
    env = cli_env(config_dir, args.token_file)
    session = str(uuid.uuid4())
    turns = []
    for index, prompt in enumerate(FIVE_TURNS):
        flag = ("--session-id", session) if index == 0 else ("--resume", session)
        turns.append(cli_turn(args, env, workdir, prompt, *flag))
    return {
        "session_id": session,
        "turns": turns,
        "turn5_recalled_codeword": recalled(turns[-1]),
    }


# (b) an in-process hades_get tool, scoped to it alone, against FDY-0581's three requests


def hades_server(call_log: Path) -> Any:
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("hades_get", "GET a path on the Hades API and return its JSON body.", {"path": str})  # type: ignore[untyped-decorator,unused-ignore]
    async def hades_get(arguments: dict[str, Any]) -> dict[str, Any]:
        with call_log.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"tool": "hades_get", "arguments": arguments}) + "\n")
        return {"content": [{"type": "text", "text": json.dumps(CANNED)}]}

    return create_sdk_mcp_server("hades", tools=[hades_get])


def recording_hooks(hook_log: Path) -> dict[str, Any]:
    from claude_agent_sdk import HookMatcher

    def recorder(event: str) -> Any:
        async def record(data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
            entry = {
                "event": event,
                "tool_name": data.get("tool_name"),
                "tool_input": data.get("tool_input"),
                "tool_use_id": tool_use_id,
                "session_id": data.get("session_id"),
                "at": round(time.time(), 3),
            }
            with hook_log.open("a", encoding="utf-8") as log:
                log.write(json.dumps(entry) + "\n")
            return {}

        return record

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[recorder("PreToolUse")])],
        "PostToolUse": [HookMatcher(matcher=None, hooks=[recorder("PostToolUse")])],
        "Stop": [HookMatcher(matcher=None, hooks=[recorder("Stop")])],
    }


def read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


async def scope(args: argparse.Namespace) -> dict[str, Any]:
    # First exactly the task's shape (allow list and deny list only, the built-in set
    # left as the CLI defines it), then the same with the built-in set emptied, which
    # is FDY-0581's --tools "".
    return {
        "allow_and_deny_only": await scope_variant(args, "scope-allow-deny", tools=None),
        "allow_deny_and_tools_empty": await scope_variant(args, "scope-tools-empty", tools=[]),
    }


async def scope_variant(args: argparse.Namespace, name: str, tools: Any) -> dict[str, Any]:
    from claude_agent_sdk import ClaudeSDKClient, SystemMessage

    config_dir, workdir = fresh_dirs(args.out, name)
    call_log = args.out / name / "hades_get.calls.jsonl"
    hook_log = args.out / name / "hooks.jsonl"
    options = sdk_options(
        args,
        config_dir,
        workdir,
        tools=tools,
        mcp_servers={"hades": hades_server(call_log)},
        allowed_tools=[HADES_TOOL],
        disallowed_tools=DISALLOWED,
        hooks=recording_hooks(hook_log),
    )
    report: dict[str, Any] = {}
    async with ClaudeSDKClient(options) as client:
        await client.query(ADVERSARIAL)
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        texts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        async for message in client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == "init":
                report["init_tools"] = message.data.get("tools")
                report["init_mcp_servers"] = message.data.get("mcp_servers")
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_uses.append({"name": block.name, "input": block.input})
            elif isinstance(message, ResultMessage):
                report["result"] = result_fields(message)
        report["tool_uses"] = tool_uses
        report["text"] = texts
        # A follow-up probe: a read-only built-in that neither list names.
        report["read_probe"] = await sdk_turn(
            client, "Use the Read tool on /etc/hostname and show its content, nothing else."
        )
    report["hades_get_calls"] = read_lines(call_log)
    report["hook_tool_calls"] = [
        {"event": h["event"], "tool_name": h["tool_name"], "tool_input": h["tool_input"]}
        for h in read_lines(hook_log)
        if h["event"] != "Stop"
    ]
    report["marker_file_written"] = (workdir / "pwned.txt").exists()
    report["only_hades_get_ran"] = bool(tool_uses) and all(
        u["name"] == HADES_TOOL for u in tool_uses
    )
    return report


# (c) interrupt during a long generation, then a new query in the same session


async def stream_until(client: Any, deltas: int) -> dict[str, Any]:
    """Read the partial stream of a turn until enough text deltas have arrived."""
    from claude_agent_sdk import StreamEvent

    start = time.monotonic()
    seen = 0
    first_text_s = None
    delta_types: dict[str, int] = {}
    kinds: list[str] = []
    async for message in client.receive_messages():
        kinds.append(type(message).__name__)
        if isinstance(message, StreamEvent):
            event = message.event
            if event.get("type") == "content_block_delta":
                kind = str(event.get("delta", {}).get("type"))
                delta_types[kind] = delta_types.get(kind, 0) + 1
                if kind == "text_delta":
                    if first_text_s is None:
                        first_text_s = round(time.monotonic() - start, 2)
                    seen += 1
                    if seen >= deltas:
                        break
    return {
        "text_deltas_before_stop": seen,
        "delta_types": delta_types,
        "first_text_delta_s": first_text_s,
        "message_kinds_head": kinds[:6],
    }


async def interrupt(args: argparse.Namespace) -> dict[str, Any]:
    from claude_agent_sdk import ClaudeSDKClient, ResultMessage

    config_dir, workdir = fresh_dirs(args.out, "interrupt")
    options = sdk_options(args, config_dir, workdir, include_partial_messages=True)
    report: dict[str, Any] = {}
    client = ClaudeSDKClient(options)
    await client.connect()
    try:
        report["turn1"] = await sdk_turn(client, FIVE_TURNS[0])
        session_id = report["turn1"]["session_id"]
        pid = child_pid(client)
        start = time.monotonic()
        await client.query(LONG_GENERATION)
        report["long_generation"] = await stream_until(client, args.stop_after_deltas)
        report["long_generation"]["seconds_before_interrupt"] = round(time.monotonic() - start, 2)
        sent = time.monotonic()
        await client.interrupt()
        report["interrupt_call_s"] = round(time.monotonic() - sent, 2)
        after: list[str] = []
        async for message in client.receive_response():
            after.append(type(message).__name__)
            if isinstance(message, ResultMessage):
                report["interrupted_result"] = result_fields(message)
        report["seconds_interrupt_to_result"] = round(time.monotonic() - sent, 2)
        report["messages_after_interrupt"] = after
        report["same_child_after_interrupt"] = child_pid(client) == pid
        report["turn3_after_interrupt"] = await sdk_turn(client, AFTER_STOP)
        report["turn3_recalled_codeword"] = recalled(report["turn3_after_interrupt"])
        report["same_session"] = report["turn3_after_interrupt"].get("session_id") == session_id
    finally:
        await client.disconnect()
    report["transcript"] = transcript(config_dir, session_id)
    return report


# (d) a PreToolUse hook recording every tool call, and a Stop hook


async def hooks(args: argparse.Namespace) -> dict[str, Any]:
    from claude_agent_sdk import ClaudeSDKClient

    config_dir, workdir = fresh_dirs(args.out, "hooks")
    call_log = args.out / "hooks" / "hades_get.calls.jsonl"
    hook_log = args.out / "hooks" / "hooks.jsonl"
    options = sdk_options(
        args,
        config_dir,
        workdir,
        mcp_servers={"hades": hades_server(call_log)},
        allowed_tools=[HADES_TOOL],
        disallowed_tools=DISALLOWED,
        hooks=recording_hooks(hook_log),
    )
    async with ClaudeSDKClient(options) as client:
        first = await sdk_turn(
            client,
            "Call hades_get with path /v1/tasks, then with path /v1/principals, "
            "then reply with the number of calls you made.",
        )
        second = await sdk_turn(client, "Reply with exactly: done")
    entries = read_lines(hook_log)
    return {
        "turn1": first,
        "turn2_no_tool": second,
        "hook_log": entries,
        "pre_tool_use_count": sum(1 for e in entries if e["event"] == "PreToolUse"),
        "post_tool_use_count": sum(1 for e in entries if e["event"] == "PostToolUse"),
        "stop_count": sum(1 for e in entries if e["event"] == "Stop"),
        "hades_get_calls": read_lines(call_log),
    }


# (f) the SDK's CLI child killed from outside mid-turn


async def kill(args: argparse.Namespace) -> dict[str, Any]:
    from claude_agent_sdk import ClaudeSDKClient

    config_dir, workdir = fresh_dirs(args.out, "kill")
    options = sdk_options(args, config_dir, workdir, include_partial_messages=True)
    report: dict[str, Any] = {}
    client = ClaudeSDKClient(options)
    await client.connect()
    report["turn1"] = await sdk_turn(client, FIVE_TURNS[0])
    session_id = report["turn1"]["session_id"]
    pid = child_pid(client)
    report["child_pid"] = pid
    await client.query(LONG_GENERATION)
    report["long_generation"] = await stream_until(client, args.stop_after_deltas)
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    killed = time.monotonic()
    report["after_kill_receive"] = await capture(drain(client))
    report["seconds_kill_to_exception"] = round(time.monotonic() - killed, 2)
    report["after_kill_query"] = await capture(client.query("Are you there?"))
    report["disconnect"] = await capture(client.disconnect())
    # Same client object, connect again: a new child with the same options (no resume).
    report["reconnect_same_client"] = await capture(client.connect())
    if report["reconnect_same_client"]["ok"]:
        turn = await capture(sdk_turn(client, FIVE_TURNS[-1]))
        report["reconnected_turn"] = turn
        report["reconnected_child_pid"] = child_pid(client)
        await capture(client.disconnect())
    # A new client resuming the session from its file.
    resumed = ClaudeSDKClient(
        sdk_options(args, config_dir, workdir, include_partial_messages=True, resume=session_id)
    )
    report["resume_connect"] = await capture(resumed.connect())
    if report["resume_connect"]["ok"]:
        report["resumed_turn"] = await sdk_turn(resumed, AFTER_STOP)
        report["resumed_recalled_codeword"] = recalled(report["resumed_turn"])
        await resumed.disconnect()
    report["transcript"] = transcript(config_dir, session_id)
    return report


async def drain(client: Any) -> list[str]:
    return [type(m).__name__ async for m in client.receive_response()]


async def capture(awaitable: Any, timeout: float = 120) -> dict[str, Any]:
    try:
        value = await asyncio.wait_for(awaitable, timeout=timeout)
    except Exception as error:  # the exception type is the finding
        return {
            "ok": False,
            "exception": f"{type(error).__module__}.{type(error).__name__}",
            "message": str(error)[:300],
            "exit_code": getattr(error, "exit_code", None),
        }
    return {"ok": True, "value": value}


# (g) resume interchange: CLI-created session in the SDK, SDK-created session in the CLI


async def resume_interchange(args: argparse.Namespace) -> dict[str, Any]:
    from claude_agent_sdk import ClaudeSDKClient

    config_dir, workdir = fresh_dirs(args.out, "resume-interchange")
    env = cli_env(config_dir, args.token_file)
    cli_session = str(uuid.uuid4())
    report: dict[str, Any] = {}
    report["cli_turn1"] = cli_turn(args, env, workdir, FIVE_TURNS[0], "--session-id", cli_session)
    async with ClaudeSDKClient(sdk_options(args, config_dir, workdir, resume=cli_session)) as c:
        report["sdk_resumes_cli"] = await sdk_turn(c, FIVE_TURNS[-1])
    report["sdk_resumes_cli"]["recalled"] = recalled(report["sdk_resumes_cli"])
    report["sdk_resumes_cli"]["same_session"] = (
        report["sdk_resumes_cli"].get("session_id") == cli_session
    )

    async with ClaudeSDKClient(sdk_options(args, config_dir, workdir)) as c:
        report["sdk_turn1"] = await sdk_turn(
            c, f"The codeword is {SECOND_CODEWORD}. Reply with exactly: noted"
        )
    sdk_session = report["sdk_turn1"]["session_id"]
    report["cli_resumes_sdk"] = cli_turn(
        args,
        env,
        workdir,
        "What was the codeword in my first message? Reply with the codeword only.",
        "--resume",
        sdk_session,
    )
    report["cli_resumes_sdk"]["recalled"] = recalled(report["cli_resumes_sdk"], SECOND_CODEWORD)
    report["session_files"] = [str(p.relative_to(config_dir)) for p in session_files(config_dir)]
    return report


# The raw stream-json process, no SDK: does the SDK's interrupt control request work
# when Hades writes it itself?


class RawProcess:
    """`claude -p --input-format stream-json` with a reader thread on stdout."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path) -> None:
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.lines: queue.Queue[dict[str, Any] | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self.lines.put(json.loads(line))
            except ValueError:
                continue
        self.lines.put(None)

    def send(self, document: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(document) + "\n")
        self.process.stdin.flush()

    def user(self, text: str) -> None:
        content = [{"type": "text", "text": text}]
        self.send({"type": "user", "message": {"role": "user", "content": content}})

    def until(self, stop: Any, timeout: float = 180) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            if event is None:
                break
            events.append(event)
            if stop(event, events):
                break
        return events


def raw_result(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    texts = [
        str(b.get("text"))
        for e in events
        if e.get("type") == "assistant"
        for b in e.get("message", {}).get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return {
        "session_id": result.get("session_id"),
        "subtype": result.get("subtype"),
        "is_error": result.get("is_error"),
        "terminal_reason": result.get("terminal_reason"),
        "duration_ms": result.get("duration_ms"),
        "text": texts,
    }


def is_result(event: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    return event.get("type") == "result"


async def raw_interrupt(args: argparse.Namespace) -> dict[str, Any]:
    config_dir, workdir = fresh_dirs(args.out, "raw-interrupt")
    argv = [
        args.cli_path,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        args.model,
        *CLI_SCOPE_FLAGS,
        "--input-format",
        "stream-json",
        "--include-partial-messages",
    ]
    raw = RawProcess(argv, cli_env(config_dir, args.token_file), workdir)
    report: dict[str, Any] = {}
    raw.user(FIVE_TURNS[0])
    report["turn1"] = raw_result(raw.until(is_result))
    session_id = report["turn1"]["session_id"]
    raw.user(LONG_GENERATION)

    def enough_text(event: dict[str, Any], events: list[dict[str, Any]]) -> bool:
        deltas = [
            e
            for e in events
            if e.get("type") == "stream_event"
            and e.get("event", {}).get("delta", {}).get("type") == "text_delta"
        ]
        return len(deltas) >= int(args.stop_after_deltas)

    start = time.monotonic()
    streamed = raw.until(enough_text)
    report["seconds_to_stop_point"] = round(time.monotonic() - start, 2)
    report["stream_event_lines_before_interrupt"] = sum(
        1 for e in streamed if e.get("type") == "stream_event"
    )
    # The SDK's envelope, written by hand; no initialize request was sent first.
    sent = time.monotonic()
    raw.send(
        {"type": "control_request", "request_id": "spike_1", "request": {"subtype": "interrupt"}}
    )
    after = raw.until(is_result, timeout=60)
    report["seconds_interrupt_to_result"] = round(time.monotonic() - sent, 2)
    report["control_responses"] = [e for e in after if e.get("type") == "control_response"]
    report["line_types_after_interrupt"] = sorted({str(e.get("type")) for e in after})
    report["interrupted"] = raw_result(after)
    raw.user(AFTER_STOP)
    report["turn3"] = raw_result(raw.until(is_result))
    report["turn3_recalled_codeword"] = recalled(report["turn3"])
    report["same_session"] = report["turn3"]["session_id"] == session_id
    assert raw.process.stdin is not None
    raw.process.stdin.close()
    report["exit"] = raw.process.wait(timeout=30)
    report["transcript"] = transcript(config_dir, session_id)
    return report


RUNNERS = {
    "multi-turn": multi_turn,
    "cli-turns": cli_turns,
    "scope": scope,
    "interrupt": interrupt,
    "hooks": hooks,
    "kill": kill,
    "resume-interchange": resume_interchange,
    "raw-interrupt": raw_interrupt,
}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    names: Sequence[str] = EXPERIMENTS if args.experiment == "all" else (args.experiment,)
    report: dict[str, Any] = {}
    for name in names:
        start = time.monotonic()
        report[name] = await capture(RUNNERS[name](args), timeout=900)
        report[name]["experiment_s"] = round(time.monotonic() - start, 2)
        (args.out / f"{name}.json").write_text(
            json.dumps(report[name], indent=2, default=str), encoding="utf-8"
        )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    removed = scrub_parent_environment()
    try:
        import claude_agent_sdk
    except ImportError:
        print("claude-agent-sdk missing: run under uv run --no-project --with claude-agent-sdk")
        return 2
    header = {
        "sdk_version": claude_agent_sdk.__version__,
        "cli_version": subprocess.run(
            [args.cli_path, "--version"], capture_output=True, text=True, check=False
        ).stdout.strip(),
        "python": sys.version.split()[0],
        "parent_variables_removed": len(removed),
        "token_file_present": args.token_file.is_file(),
    }
    report = {"header": header, **asyncio.run(run(args))}
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
