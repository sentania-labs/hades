#!/usr/bin/env python3
"""Principal harness spike (hades #208, work item 1): the scripts behind
docs/spikes/principal-harness.md, runnable again inside a worker container.

Nothing here is product code. Each subcommand runs one experiment against the CLIs the
worker image carries and prints a JSON summary; raw transcripts go under --out.

Credentials: the Claude token is read from the mounted file into the CLI's documented
variable for the child process only, the way the worker launch does it. It is never
copied to disk or printed. The Codex credential is whatever the worker mount holds
(on 2026-10-08 it held nothing, which is a finding of its own).

    python3 tools/spikes/principal_harness.py claude-resume
    python3 tools/spikes/principal_harness.py claude-long-lived
    python3 tools/spikes/principal_harness.py claude-scope
    python3 tools/spikes/principal_harness.py codex-probe
    python3 tools/spikes/principal_harness.py credentials
    python3 tools/spikes/principal_harness.py all
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

CLAUDE = "/usr/local/bin/claude"
CODEX = "/usr/local/bin/codex"
CLAUDE_CREDENTIAL_DIR = Path("/home/worker/.claude")
CODEX_CREDENTIAL_DIR = Path("/home/worker/.codex")
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CODEWORD = "ORCHID-42"
DEFAULT_MODEL = "claude-sonnet-5"
# No shell, no file tools, no web tool from the built-in set: the MCP stub is the only
# tool the model can call, and dontAsk denies anything that would need a prompt.
SCOPE_FLAGS = ("--tools", "", "--permission-mode", "dontAsk", "--permission-prompts", "none")


@dataclass
class Measured:
    argv: list[str]
    exit_code: int | None
    seconds: float
    peak_rss_kb: int
    stdout_lines: int
    stderr_tail: str


@dataclass
class RssPoller:
    """Peak resident size of a child, sampled from /proc every 50 ms."""

    pid: int
    peak_kb: int = 0
    last_kb: int = 0
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> RssPoller:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def sample(self) -> int:
        try:
            for line in Path(f"/proc/{self.pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        except OSError:
            pass
        return 0

    def _run(self) -> None:
        while not self._stop.is_set():
            kb = self.sample()
            if kb:
                self.last_kb = kb
                self.peak_kb = max(self.peak_kb, kb)
            self._stop.wait(0.05)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)


def claude_env(config_dir: Path, token_file: Path) -> dict[str, str]:
    """A clean environment: the token goes into the CLI's variable, nothing else of the
    parent session (this script may itself run under a harness) leaks in."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/home/worker"),
        "TERM": "dumb",
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }
    if token_file.is_file():
        env[TOKEN_ENV] = token_file.read_text(encoding="utf-8").strip()
    return env


def run_measured(
    argv: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path,
    stdin_text: str = "",
    timeout: float = 240,
    log: Path | None = None,
) -> tuple[Measured, list[dict[str, Any]]]:
    start = time.monotonic()
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    poller = RssPoller(process.pid).start()
    try:
        out, err = process.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
    poller.stop()
    seconds = round(time.monotonic() - start, 2)
    if log is not None:
        log.write_text(out, encoding="utf-8")
        log.with_suffix(".stderr").write_text(err, encoding="utf-8")
    events = [e for e in (json_object(line) for line in out.splitlines()) if e is not None]
    measured = Measured(
        [a if a else '""' for a in argv],
        process.returncode,
        seconds,
        poller.peak_kb,
        len(out.splitlines()),
        err[-600:],
    )
    return measured, events


def json_object(line: str) -> dict[str, Any] | None:
    try:
        document = json.loads(line)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def assistant_text(events: Sequence[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text")))
    return texts


def result_summary(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in events if e.get("type") == "result"), {})
    return {
        "model": init.get("model"),
        "tools": init.get("tools"),
        "mcp_servers": init.get("mcp_servers"),
        "session_id": result.get("session_id") or init.get("session_id"),
        "result_subtype": result.get("subtype"),
        "is_error": result.get("is_error"),
        "num_turns": result.get("num_turns"),
        "duration_ms": result.get("duration_ms"),
        "total_cost_usd": result.get("total_cost_usd"),
        "assistant_text": assistant_text(events),
        "errors": [
            str(e.get("error") or e.get("result"))[:300] for e in events if e.get("is_error")
        ],
    }


def claude_argv(model: str, *extra: str) -> list[str]:
    return [CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--model", model, *extra]


def session_files(config_dir: Path) -> list[str]:
    projects = config_dir / "projects"
    return sorted(str(p.relative_to(config_dir)) for p in projects.rglob("*.jsonl"))


def fresh_dirs(out: Path, name: str) -> tuple[Path, Path]:
    config_dir = out / name / "config"
    workdir = out / name / "work"
    shutil.rmtree(out / name, ignore_errors=True)
    config_dir.mkdir(parents=True)
    workdir.mkdir(parents=True)
    return config_dir, workdir


# (a) resume across two processes, then after the session store is wiped


def claude_resume(args: argparse.Namespace) -> dict[str, Any]:
    config_dir, workdir = fresh_dirs(args.out, "claude-resume")
    env = claude_env(config_dir, args.token_file)
    session = str(uuid.uuid4())
    report: dict[str, Any] = {"session_id": session}

    turn1, events = run_measured(
        claude_argv(args.model, *SCOPE_FLAGS, "--session-id", session),
        env=env,
        cwd=workdir,
        stdin_text=f"The codeword is {CODEWORD}. Reply with exactly: noted",
        log=args.out / "claude-resume" / "turn1.jsonl",
    )
    report["turn1"] = {"measured": asdict(turn1), **result_summary(events)}
    report["session_files_after_turn1"] = session_files(config_dir)

    turn2, events = run_measured(
        claude_argv(args.model, *SCOPE_FLAGS, "--resume", session),
        env=env,
        cwd=workdir,
        stdin_text="What is the codeword? Reply with the codeword only.",
        log=args.out / "claude-resume" / "turn2.jsonl",
    )
    summary = result_summary(events)
    report["turn2_resume_new_process"] = {
        "measured": asdict(turn2),
        **summary,
        "remembered": any(CODEWORD in t for t in summary["assistant_text"]),
    }

    shutil.rmtree(config_dir / "projects", ignore_errors=True)
    report["session_files_after_wipe"] = session_files(config_dir)
    turn3, events = run_measured(
        claude_argv(args.model, *SCOPE_FLAGS, "--resume", session),
        env=env,
        cwd=workdir,
        stdin_text="What is the codeword? Reply with the codeword only.",
        log=args.out / "claude-resume" / "turn3.jsonl",
    )
    summary = result_summary(events)
    report["turn3_resume_after_wipe"] = {
        "measured": asdict(turn3),
        **summary,
        "remembered": any(CODEWORD in t for t in summary["assistant_text"]),
    }

    # Rebuild from Hades's own record: a new session whose first prompt carries the
    # transcript so far. The model's hidden state is not claimed, only the words.
    record = (
        "Conversation so far, from the durable record:\n"
        f"user: The codeword is {CODEWORD}. Reply with exactly: noted\n"
        "assistant: noted\n"
        "user: What is the codeword? Reply with the codeword only."
    )
    turn4, events = run_measured(
        claude_argv(args.model, *SCOPE_FLAGS, "--session-id", str(uuid.uuid4())),
        env=env,
        cwd=workdir,
        stdin_text=record,
        log=args.out / "claude-resume" / "turn4.jsonl",
    )
    summary = result_summary(events)
    report["turn4_rebuilt_from_record"] = {
        "measured": asdict(turn4),
        **summary,
        "remembered": any(CODEWORD in t for t in summary["assistant_text"]),
    }
    return report


# (c) and (e): one long-lived process fed over stdin, then killed mid-turn and resumed


class StreamJsonProcess:
    """`claude -p --input-format stream-json`: user messages in, events out."""

    def __init__(self, argv: Sequence[str], env: dict[str, str], cwd: Path, log: Path) -> None:
        self.started = time.monotonic()
        self.process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.poller = RssPoller(self.process.pid).start()
        self.log = log.open("w", encoding="utf-8")
        assert self.process.stdout is not None
        self.stdout: IO[str] = self.process.stdout

    def send(self, text: str) -> None:
        assert self.process.stdin is not None
        message = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def events(self, timeout: float) -> Iterator[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.stdout.readline()
            if not line:
                return
            self.log.write(line)
            self.log.flush()
            event = json_object(line)
            if event is not None:
                yield event

    def wait_for(self, kind: str, timeout: float = 180) -> tuple[list[dict[str, Any]], float]:
        collected: list[dict[str, Any]] = []
        start = time.monotonic()
        for event in self.events(timeout):
            collected.append(event)
            if event.get("type") == kind:
                break
        return collected, round(time.monotonic() - start, 2)

    def kill(self) -> None:
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(timeout=10)
        self.poller.stop()
        self.log.close()

    def close(self) -> int | None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.poller.stop()
        self.log.close()
        return self.process.returncode


def claude_long_lived(args: argparse.Namespace) -> dict[str, Any]:
    config_dir, workdir = fresh_dirs(args.out, "claude-long-lived")
    env = claude_env(config_dir, args.token_file)
    session = str(uuid.uuid4())
    argv = claude_argv(
        args.model, *SCOPE_FLAGS, "--input-format", "stream-json", "--session-id", session
    )
    report: dict[str, Any] = {"session_id": session, "argv": [a if a else '""' for a in argv]}
    host = StreamJsonProcess(argv, env, workdir, args.out / "claude-long-lived" / "stream.jsonl")
    # Startup: the CLI emits nothing until the first user message in stream-json input
    # mode, so startup is measured as time to the `system init` event after turn 1 is sent.
    host.send(f"The codeword is {CODEWORD}. Reply with exactly: noted")
    events, _ = host.wait_for("system", timeout=60)
    report["seconds_to_system_init"] = round(time.monotonic() - host.started, 2)
    more, turn_seconds = host.wait_for("result")
    events += more
    report["turn1"] = {"seconds": turn_seconds, **result_summary(events)}
    time.sleep(2)
    report["rss_kb_idle_after_turn1"] = host.poller.sample()
    host.send("What is the codeword? Reply with the codeword only.")
    events, turn_seconds = host.wait_for("result")
    summary = result_summary(events)
    report["turn2_same_process"] = {
        "seconds": turn_seconds,
        **summary,
        "remembered": any(CODEWORD in t for t in summary["assistant_text"]),
    }
    report["rss_kb_idle_after_turn2"] = host.poller.sample()
    report["peak_rss_kb"] = host.poller.peak_kb

    # (e) kill -9 mid-turn: send a turn, wait for the first assistant event, kill.
    host.send("Count from 1 to 40 in words, one per line, then say the codeword.")
    events, _ = host.wait_for("assistant", timeout=90)
    host.kill()
    report["killed_mid_turn"] = {
        "events_seen_before_kill": [str(e.get("type")) for e in events],
        "exit_code": host.process.returncode,
    }
    log = config_dir / "projects"
    lines = [json_object(line) for p in log.rglob("*.jsonl") for line in p.read_text().splitlines()]
    kinds = [str(e.get("type")) for e in lines if e is not None]
    report["session_file_after_kill"] = {
        "line_types": kinds,
        "last_user_text": next(
            (
                str(e["message"]["content"])[:120]
                for e in reversed(lines)
                if e is not None and e.get("type") == "user"
            ),
            None,
        ),
    }
    restarted, events = run_measured(
        claude_argv(args.model, *SCOPE_FLAGS, "--resume", session),
        env=env,
        cwd=workdir,
        stdin_text=(
            "Answer in one line each: what was my last request before this one, did you "
            "finish it, and what is the codeword?"
        ),
        log=args.out / "claude-long-lived" / "restart.jsonl",
    )
    summary = result_summary(events)
    report["restart_resume_after_kill"] = {
        "measured": asdict(restarted),
        **summary,
        "remembered": any(CODEWORD in t for t in summary["assistant_text"]),
    }
    return report


# (d) tool scoping: the MCP stub is the only tool


def mcp_stub() -> int:
    """A stdio MCP server with one tool, standing in for the Hades HTTP client."""
    log_path = Path(os.environ.get("MCP_STUB_LOG", "/tmp/mcp-stub.log"))
    for line in sys.stdin:
        request = json_object(line)
        if request is None or "id" not in request:
            continue
        method = request.get("method")
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "hades-stub", "version": "0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "hades_get",
                        "description": "GET a Hades API path and return the JSON body.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = request.get("params") or {}
            with log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(params) + "\n")
            path = str((params.get("arguments") or {}).get("path"))
            result = {"content": [{"type": "text", "text": f'GET {path} -> 200 {{"tasks": []}}'}]}
        else:
            result = {}
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n"
        )
        sys.stdout.flush()
    return 0


def claude_scope(args: argparse.Namespace) -> dict[str, Any]:
    config_dir, workdir = fresh_dirs(args.out, "claude-scope")
    env = claude_env(config_dir, args.token_file)
    call_log = args.out / "claude-scope" / "mcp-calls.jsonl"
    marker = workdir / "pwned.txt"
    mcp_config = {
        "mcpServers": {
            "hades": {
                "command": sys.executable,
                "args": [str(Path(__file__).resolve()), "mcp-stub"],
                "env": {"MCP_STUB_LOG": str(call_log)},
            }
        }
    }
    config_path = args.out / "claude-scope" / "mcp.json"
    config_path.write_text(json.dumps(mcp_config), encoding="utf-8")
    extra = (
        *SCOPE_FLAGS,
        "--strict-mcp-config",
        "--mcp-config",
        str(config_path),
        "--allowedTools",
        "mcp__hades__hades_get",
        "--disallowedTools",
        "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Agent",
    )
    prompt = (
        "Do all three, then report which succeeded and which were impossible:\n"
        f"1. Run the shell command `id` and show its output.\n"
        f"2. Write a file at {marker} containing the word hello.\n"
        "3. Call the hades_get tool with path /v1/tasks and show its result.\n"
        "If a tool does not exist, say so."
    )
    measured, events = run_measured(
        claude_argv(args.model, *extra),
        env=env,
        cwd=workdir,
        stdin_text=prompt,
        log=args.out / "claude-scope" / "turn.jsonl",
    )
    tool_uses = [
        {"name": block.get("name"), "input": block.get("input")}
        for event in events
        if event.get("type") == "assistant"
        for block in event.get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    return {
        "flags": [a if a else '""' for a in extra],
        "measured": asdict(measured),
        **result_summary(events),
        "tool_uses": tool_uses,
        "mcp_calls": call_log.read_text().splitlines() if call_log.exists() else [],
        "marker_file_written": marker.exists(),
    }


# (b) codex app-server over JSON-RPC: startup, auth, thread/resume with and without storage


class AppServer:
    def __init__(self, codex_home: Path, cwd: Path, log: Path) -> None:
        self.started = time.monotonic()
        self.process = subprocess.Popen(
            [
                CODEX,
                "app-server",
                "--disable",
                "plugins",
                "-c",
                "check_for_update_on_startup=false",
            ],
            cwd=cwd,
            env={**claude_env(cwd, Path("/nonexistent")), "CODEX_HOME": str(codex_home)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.poller = RssPoller(self.process.pid).start()
        self.log = log.open("a", encoding="utf-8")
        self.next_id = 1

    def call(self, method: str, params: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        request_id = self.next_id
        self.next_id += 1
        self.process.stdin.write(
            json.dumps({"id": request_id, "method": method, "params": params}) + "\n"
        )
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        notifications: list[str] = []
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                break
            self.log.write(line)
            document = json_object(line)
            if document is None:
                continue
            if document.get("id") == request_id:
                document["notifications_before"] = notifications
                return document
            if "method" in document:
                notifications.append(str(document["method"]))
                if document["method"] in ("turn/completed", "turn/failed"):
                    return {"id": request_id, "ended_by": document}
        return {"id": request_id, "timeout": True, "notifications_before": notifications}

    def drain(self, until: tuple[str, ...], timeout: float) -> dict[str, Any]:
        """Notifications until one of `until` arrives, for a turn that outlives its
        request's response."""
        assert self.process.stdout is not None
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                break
            self.log.write(line)
            document = json_object(line)
            if document is None or "method" not in document:
                continue
            seen.append(str(document["method"]))
            if document["method"] in until:
                return {"seen": seen, "final": document}
        return {"seen": seen, "final": None, "timed_out": True}

    def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"method": method, "params": params}) + "\n")
        self.process.stdin.flush()

    def stop(self, sig: int = signal.SIGTERM) -> int | None:
        self.process.send_signal(sig)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.poller.stop()
        self.log.close()
        return self.process.returncode


def _brief(document: dict[str, Any]) -> Any:
    text = json.dumps(document)
    return document if len(text) <= 900 else text[:900] + "..."


def codex_probe(args: argparse.Namespace) -> dict[str, Any]:
    codex_home = args.out / "codex-probe" / "home"
    workdir = args.out / "codex-probe" / "work"
    shutil.rmtree(args.out / "codex-probe", ignore_errors=True)
    codex_home.mkdir(parents=True)
    workdir.mkdir(parents=True)
    if args.codex_home.is_dir():
        for name in ("auth.json", "access-token.json", "config.toml"):
            source = args.codex_home / name
            if source.is_file():
                shutil.copy(source, codex_home / name)
    report: dict[str, Any] = {
        "credential_files_in_mount": sorted(
            p.name for p in args.codex_home.iterdir() if p.is_file()
        )
        if args.codex_home.is_dir()
        else "mount absent",
    }
    log = args.out / "codex-probe" / "rpc.jsonl"
    server = AppServer(codex_home, workdir, log)
    init = server.call(
        "initialize",
        {
            "clientInfo": {"name": "principal-spike", "version": "1"},
            "capabilities": {"experimentalApi": True},
        },
    )
    report["seconds_to_initialize"] = round(time.monotonic() - server.started, 2)
    report["initialize"] = _brief(init)
    server.notify("initialized", {})
    time.sleep(1)
    report["rss_kb_idle"] = server.poller.sample()
    report["account_read"] = _brief(server.call("account/read", {}))
    thread_params = {
        "cwd": str(workdir),
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "model": args.codex_model,
    }
    started = server.call("thread/start", thread_params)
    report["thread_start"] = _brief(started)
    thread = ((started.get("result") or {}).get("thread") or {}) if "result" in started else {}
    thread_id = str(thread.get("id") or "")
    report["thread_id"] = thread_id
    if thread_id:
        turn = server.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": f"The codeword is {CODEWORD}."}],
            },
            timeout=60,
        )
        report["turn_start"] = _brief(turn)
        report["turn_end"] = _brief(server.drain(("turn/completed", "turn/failed"), timeout=20))
    report["thread_resume_bogus_id"] = _brief(
        server.call("thread/resume", {"threadId": str(uuid.uuid4())})
    )
    report["peak_rss_kb_first_process"] = server.poller.peak_kb
    report["rollout_files_after_start"] = sorted(
        str(p.relative_to(codex_home)) for p in codex_home.rglob("*.jsonl")
    )
    report["exit_after_sigterm"] = server.stop()

    # Second process: resume the thread from CODEX_HOME storage.
    server = AppServer(codex_home, workdir, log)
    server.call("initialize", {"clientInfo": {"name": "principal-spike", "version": "1"}})
    server.notify("initialized", {})
    report["seconds_to_initialize_second_process"] = round(time.monotonic() - server.started, 2)
    if thread_id:
        report["thread_resume_second_process"] = _brief(
            server.call("thread/resume", {"threadId": thread_id})
        )
        report["thread_list_second_process"] = _brief(server.call("thread/list", {}))
    server.stop()

    # Third process: storage wiped, same thread id.
    shutil.rmtree(codex_home / "sessions", ignore_errors=True)
    shutil.rmtree(codex_home / "archived_sessions", ignore_errors=True)
    server = AppServer(codex_home, workdir, log)
    server.call("initialize", {"clientInfo": {"name": "principal-spike", "version": "1"}})
    server.notify("initialized", {})
    if thread_id:
        report["thread_resume_after_wipe"] = _brief(
            server.call("thread/resume", {"threadId": thread_id})
        )
    report["exit_after_sigkill"] = server.stop(signal.SIGKILL)
    return report


# (f) credential files: mtimes before and after, never contents


def credentials(args: argparse.Namespace) -> dict[str, Any]:
    def describe(directory: Path) -> dict[str, Any]:
        if not directory.is_dir():
            return {"present": False}
        files: dict[str, Any] = {}
        for path in sorted(directory.iterdir()):
            if path.name.startswith(".."):
                continue
            try:
                info = path.stat()
            except OSError as exc:
                files[path.name] = {"error": type(exc).__name__}
                continue
            files[path.name] = {
                "size": info.st_size,
                "mode": oct(info.st_mode & 0o777),
                "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(info.st_mtime)),
                "is_dir": path.is_dir(),
            }
        mount = next(
            (
                line.split()[:4]
                for line in Path("/proc/mounts").read_text().splitlines()
                if line.split()[1] == str(directory)
            ),
            None,
        )
        return {"present": True, "files": files, "mount": mount}

    status = subprocess.run(
        [CODEX, "login", "status"],
        check=False,
        env={**claude_env(args.out, Path("/nonexistent")), "CODEX_HOME": str(args.codex_home)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "claude": describe(CLAUDE_CREDENTIAL_DIR),
        "codex": describe(args.codex_home),
        "codex_login_status": (status.stdout + status.stderr).strip()[:300],
        "claude_token_shape": (
            "opaque sk-ant-oat01 string, not JSON, no refresh token beside it"
            if args.token_file.is_file()
            and args.token_file.read_text(encoding="utf-8").startswith("sk-ant-oat01")
            else "absent or unexpected"
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=Path("/tmp/principal-harness-spike"))
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Claude model for the spike turns")
    parser.add_argument("--codex-model", default="gpt-5-codex")
    parser.add_argument("--token-file", type=Path, default=CLAUDE_CREDENTIAL_DIR / "oauth-token")
    parser.add_argument("--codex-home", type=Path, default=CODEX_CREDENTIAL_DIR)
    parser.add_argument(
        "experiment",
        choices=(
            "claude-resume",
            "claude-long-lived",
            "claude-scope",
            "codex-probe",
            "credentials",
            "all",
            "mcp-stub",
        ),
    )
    return parser


EXPERIMENTS = {
    "claude-resume": claude_resume,
    "claude-long-lived": claude_long_lived,
    "claude-scope": claude_scope,
    "codex-probe": codex_probe,
    "credentials": credentials,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.experiment == "mcp-stub":
        return mcp_stub()
    args.out.mkdir(parents=True, exist_ok=True)
    names = list(EXPERIMENTS) if args.experiment == "all" else [args.experiment]
    report: dict[str, Any] = {}
    for name in names:
        report[name] = EXPERIMENTS[name](args)
        (args.out / f"{name}.json").write_text(json.dumps(report[name], indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
