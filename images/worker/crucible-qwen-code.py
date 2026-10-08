#!/usr/bin/python3
"""Configure Qwen's per-attempt home, run its CLI, and leave a report when the model
did not (hades #498).

The provider owns transcript capture: this wrapper's stdout is Qwen's stream-json,
passed through line by line. Like the Hermes wrapper, the full identity in the prompt
instructs the agent to write report.yaml against CompletionClaimV1; we never manufacture
a successful claim from the CLI's final prose. When the run leaves neither
report/report.yaml nor report/blocked.md, the wrapper writes a minimal report from the
run log: the commands run, the commits on the branch, and how the run ended. It carries
no self-review and no acceptance mapping, so Hades lists it for the reviewer and composes
the completion record from its own evidence.

Hermes parity (hades #498): the settings restrict the tools to file and shell, so no
sub-agent (`task`), skill, memory (`save_memory`), web or MCP tool is offered; context
files (QWEN.md, AGENTS.md) are not loaded as rules; thinking is off and the response
has an output cap. The adapter records the effective values on the attempt.

Hades #490: a local gateway that drops the connection (refused, reset, timed out, the
socket closed mid-answer) ended the run at once, and Qwen 0.25.0 does not retry the
call itself. The wrapper now reads how the run ended, the result event's error and the
CLI's own stderr, and when the words say the gateway gave no answer at all (not a 4xx or
5xx it did answer) it starts Qwen again after a pause, up to MAX_RETRIES times with the
BACKOFF_SECONDS pauses, before giving up with that run's exit status. A relaunch is a
new session over the same checkout: the model finds its edits and commits on disk and
goes on from there. The stream-json of every launch goes through to the transcript, so
the adapter reads the last result event as the run's end. A gateway that answered, with
a status or a refusal, is left to the adapter's classes and the supervisor's reroute.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO

# The CLI the image installs; a unit test points this at a fake binary.
QWEN = os.environ.get("CRUCIBLE_QWEN_BINARY") or "/usr/local/bin/qwen"
DEFAULT_REPORT_DIR = "/crucible/report"
MAX_COMMANDS = 50
MAX_COMMITS = 50
COMMAND_LIMIT = 200
# File and shell tools only. Not offered: task (sub-agents), skill, save_memory,
# web_fetch, web_search, and any MCP tool. Both `edit` and `replace` are named so the
# file edit tool is allowed under either name the CLI registers.
CORE_TOOLS = [
    "list_directory",
    "read_file",
    "read_many_files",
    "glob",
    "search_file_content",
    "write_file",
    "edit",
    "replace",
    "run_shell_command",
]
EXCLUDED_TOOLS = ["task", "skill", "save_memory", "web_fetch", "web_search", "todo_write"]
# A context file name nothing in a checkout carries: QWEN.md and AGENTS.md are then not
# loaded as rules, as Hermes's --ignore-rules does. The identity is the only instruction.
NO_RULES_FILE = "CRUCIBLE-NO-RULES.md"
# hades #490: a transport-level API error is one where the gateway gave no answer: the
# connection was refused, reset or closed before the answer was complete, or the request
# timed out. The words are what Node's fetch (undici) and Qwen's OpenAI generator write
# for those; an HTTP status the gateway did answer (400, 429, 500, 503) is never one.
TRANSPORT_PATTERNS = (
    "connection error",
    "connection refused",
    "connection reset",
    "connection closed",
    "connection terminated",
    "failed to connect",
    "request timed out",
    "socket hang up",
    "fetch failed",
    "econnreset",
    "econnrefused",
    "etimedout",
    "epipe",
    "network error",
    "other side closed",
    "premature close",
    "und_err",
)
# Three retries after the first launch, with these pauses before each.
MAX_RETRIES = 3
BACKOFF_SECONDS: tuple[float, ...] = (5.0, 15.0, 45.0)
# How much of the CLI's stderr is kept to read the failure from.
STDERR_TAIL_BYTES = 64 * 1024


def write_settings(
    home: Path, context_length: int, max_output_tokens: int = 0, thinking: bool = False
) -> dict[str, object]:
    if context_length <= 0:
        raise ValueError("Qwen context length must be positive")
    directory = home / ".qwen"
    directory.mkdir(parents=True, exist_ok=True)
    # generationConfig lives under model in Qwen 0.25.0. Its OpenAI generator
    # clamps max_tokens to the room left in this full engine window, including
    # its output margin, rather than adding 32000 to an unbounded input budget.
    generation: dict[str, object] = {
        "contextWindowSize": context_length,
        # hades #498: thinking off and the output cap, as Hermes's launch carries
        # `enable_thinking` and `model.max_tokens`.
        "enable_thinking": thinking,
    }
    if max_output_tokens > 0:
        generation["samplingParams"] = {"max_tokens": max_output_tokens}
    settings: dict[str, object] = {
        "tools": {
            "shell": {"enableInteractiveShell": False},
            "useBuiltinRipgrep": False,
            "core": list(CORE_TOOLS),
            "exclude": list(EXCLUDED_TOOLS),
        },
        "context": {"fileName": NO_RULES_FILE, "loadMemoryFromIncludeDirectories": False},
        "model": {
            "maxToolCallsPerTurn": 0,
            "generationConfig": generation,
        },
    }
    (directory / "settings.json").write_text(json.dumps(settings) + "\n", encoding="utf-8")
    return settings


def launch_argv(argv: list[str], identity: Path) -> list[str]:
    text = identity.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("Qwen identity must not be empty")
    return [QWEN, *argv[:-1], f"{text}\n\n{argv[-1]}"]


def _limit(name: str, default: int = 0) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def transport_failure(*texts: str) -> str | None:
    """The first TRANSPORT_PATTERNS word found in `texts`, or None when the gateway
    answered (or nothing says it did not)."""
    for text in texts:
        lowered = text.lower()
        for pattern in TRANSPORT_PATTERNS:
            if pattern in lowered:
                return pattern
    return None


def run_with_retry(
    launch: Callable[[int], tuple[int, str | None]],
    *,
    retries: int = MAX_RETRIES,
    backoff: Sequence[float] = BACKOFF_SECONDS,
    sleep: Callable[[float], None] | None = None,
    stopped: Callable[[], bool] = lambda: False,
    name: str = "crucible-qwen-code",
) -> tuple[int, int]:
    """hades #490: run `launch` (given the launch number, 0 first), which returns the
    exit code and the transport-level failure the run ended on, or None. A run that ended
    on one is started again after the next BACKOFF pause, up to `retries` times. The
    result is the last run's code and how many relaunches there were. `stopped` says a
    termination signal arrived, after which nothing is relaunched."""
    pause = sleep if sleep is not None else time.sleep
    code = 0
    for number in range(retries + 1):
        code, failure = launch(number)
        if failure is None or number >= retries or stopped():
            if failure is not None and number >= retries:
                print(
                    f"{name}: transport-level API error ({failure}) after {retries} "
                    f"retries; giving up with exit {code}",
                    file=sys.stderr,
                    flush=True,
                )
            return code, number
        delay = backoff[min(number, len(backoff) - 1)] if backoff else 0.0
        print(
            f"{name}: transport-level API error ({failure}); retry {number + 1} of "
            f"{retries} in {delay:g}s",
            file=sys.stderr,
            flush=True,
        )
        pause(delay)
        if stopped():
            return code, number
    return code, retries


class RunLog:
    """What the stream-json said as it went by: the shell commands the model ran and the
    result event that ended the run."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.tool_calls = 0
        self.result: dict[str, object] | None = None
        # hades #490: the launches that ended on a transport-level error and were
        # retried, the tail of the last launch's stderr, and the event a termination
        # signal sets so that nothing is relaunched after it.
        self.retries = 0
        self.stderr_tail = ""
        self.stopped = threading.Event()

    def line(self, text: str) -> None:
        text = text.strip()
        if not text.startswith("{"):
            return
        try:
            event = json.loads(text)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "result":
            self.result = event
            return
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        for block in blocks if isinstance(blocks, list) else ():
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            self.tool_calls += 1
            args = block.get("input")
            command = args.get("command") if isinstance(args, dict) else None
            if isinstance(command, str) and len(self.commands) < MAX_COMMANDS:
                self.commands.append(" ".join(command.split())[:COMMAND_LIMIT])

    def error_text(self) -> str:
        """The result event's error, whatever shape the CLI gave it."""
        result = self.result or {}
        error = result.get("error")
        if isinstance(error, dict):
            error = error.get("message") or json.dumps(error)
        return str(error or "")

    def transport_failure(self, code: int, stderr_tail: str = "") -> str | None:
        """hades #490: the transport-level failure this run ended on, or None. Only an
        error result, or an exit with no result at all, is read: a run that ended its
        turn is never one, whatever a tool printed along the way, and the words come
        from the result's own error and the CLI's stderr, never from an event the model
        or a tool wrote."""
        if self.result is not None and not self.result.get("is_error"):
            return None
        if self.result is None and code == 0:
            return None
        return transport_failure(self.error_text(), stderr_tail)

    def how_it_ended(self, code: int) -> str:
        result = self.result or {}
        subtype = str(result.get("subtype") or "")
        retried = f" after {self.retries} transport retr{'y' if self.retries == 1 else 'ies'}"
        if subtype == "error_max_turns":
            return f"Qwen Code stopped at its session turn limit (exit {code})"
        if subtype == "success":
            return f"Qwen Code ended its turn (exit {code})" + (retried if self.retries else "")
        if subtype:
            error = self.error_text()[:COMMAND_LIMIT]
            return (
                f"Qwen Code ended with {subtype} (exit {code})"
                + (retried if self.retries else "")
                + (f": {error}" if error else "")
            )
        return f"Qwen Code exited {code} without a result event" + (
            retried if self.retries else ""
        )


def git_head(repo: Path) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def commits_since(repo: Path, start: str | None) -> list[str]:
    span = f"{start}..HEAD" if start else "HEAD"
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%h %s", f"--max-count={MAX_COMMITS}", span],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if done.returncode != 0:
        return []
    return [line.strip()[:COMMAND_LIMIT] for line in done.stdout.splitlines() if line.strip()]


def minimal_report(log: RunLog, code: int, commits: list[str]) -> dict[str, object]:
    """The report the wrapper writes when the model left none: facts from the run log
    and nothing judged. Hades fills the rest from its own evidence."""
    summary = (
        f"{log.how_it_ended(code)} after {log.tool_calls} tool call(s) and left no "
        "report.yaml; the Qwen launch wrapper wrote this minimal report from the run log. "
    )
    summary += (
        f"Commands run ({len(log.commands)}): " + "; ".join(log.commands)
        if log.commands
        else "No shell command was run"
    )
    summary += ". "
    summary += (
        f"Commits on the branch ({len(commits)}): " + "; ".join(commits)
        if commits
        else "No commit was made during the run"
    )
    summary += "."
    return {
        "schema_version": "1.0",
        "summary": summary,
        "limitations": [
            "The worker wrote no report.yaml. This minimal report was written by the Qwen "
            "launch wrapper from the run log and carries no self-review and no "
            "acceptance mapping; Hades composes the completion record from its own "
            "evidence."
        ],
    }


def ensure_report(report_dir: Path, log: RunLog, code: int, repo: Path, start: str | None) -> bool:
    """Write the minimal report when the run left neither report.yaml nor blocked.md.
    True when it wrote one."""
    if (report_dir / "report.yaml").exists() or (report_dir / "blocked.md").exists():
        return False
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        document = minimal_report(log, code, commits_since(repo, start))
        # JSON is YAML: Hades's report parser and crucible-report read it as such.
        (report_dir / "report.yaml").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as error:
        print(f"crucible-qwen-code: could not write the minimal report: {error}", file=sys.stderr)
        return False
    print("crucible-qwen-code: wrote a minimal report from the run log", file=sys.stderr)
    return True


class StderrTail:
    """hades #490: the CLI's stderr passed through as it comes, with its tail kept so
    the failure the gateway left there can be read after the exit."""

    def __init__(self, limit: int = STDERR_TAIL_BYTES) -> None:
        self._limit = limit
        self._chunks: list[bytes] = []
        self._size = 0

    def feed(self, chunk: bytes) -> None:
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._chunks and self._size - len(self._chunks[0]) >= self._limit:
            self._size -= len(self._chunks.pop(0))

    @property
    def text(self) -> str:
        return b"".join(self._chunks)[-self._limit :].decode("utf-8", "replace")


def _pump_stderr(stream: IO[bytes], tail: StderrTail) -> None:
    err = getattr(sys.stderr, "buffer", None)
    for raw in stream:
        if err is not None:
            err.write(raw)
            err.flush()
        else:
            sys.stderr.write(raw.decode("utf-8", "replace"))
            sys.stderr.flush()
        tail.feed(raw)


def run(argv: list[str], log: RunLog) -> int:
    """Run Qwen with its stdout passed through line by line, its stderr passed through
    with the tail kept on the log (hades #490), SIGTERM and SIGINT forwarded, and the
    exit status kept."""
    log.result = None
    log.stderr_tail = ""
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def forward(signum: int, _frame: object) -> None:
        log.stopped.set()
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    out = sys.stdout.buffer
    assert child.stdout is not None and child.stderr is not None
    tail = StderrTail()
    pump = threading.Thread(target=_pump_stderr, args=(child.stderr, tail), daemon=True)
    pump.start()
    for raw in child.stdout:
        out.write(raw)
        out.flush()
        log.line(raw.decode("utf-8", "replace"))
    code = child.wait()
    pump.join()
    log.stderr_tail = tail.text
    return code if code >= 0 else 128 - code


def run_retrying(
    argv: list[str], log: RunLog, sleep: Callable[[float], None] | None = None
) -> int:
    """hades #490: `run`, started again after a transport-level API error, up to
    MAX_RETRIES times with BACKOFF_SECONDS between launches."""

    def launch(_number: int) -> tuple[int, str | None]:
        code = run(argv, log)
        return code, log.transport_failure(code, log.stderr_tail)

    code, log.retries = run_with_retry(launch, sleep=sleep, stopped=log.stopped.is_set)
    return code


def main() -> int:
    home = Path.home()
    write_settings(
        home,
        int(os.environ["CRUCIBLE_QWEN_CONTEXT_LENGTH"]),
        _limit("CRUCIBLE_QWEN_MAX_OUTPUT_TOKENS"),
        os.environ.get("CRUCIBLE_QWEN_THINKING") == "true",
    )
    argv = launch_argv(sys.argv[1:], Path(os.environ["CRUCIBLE_QWEN_IDENTITY"]))
    report_dir = Path(os.environ.get("CRUCIBLE_QWEN_REPORT_DIR") or DEFAULT_REPORT_DIR)
    repo = Path.cwd()
    start = git_head(repo)
    log = RunLog()
    code = run_retrying(argv, log)
    ensure_report(report_dir, log, code, repo, start)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
