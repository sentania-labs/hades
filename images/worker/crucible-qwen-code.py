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
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

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


class RunLog:
    """What the stream-json said as it went by: the shell commands the model ran and the
    result event that ended the run."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.tool_calls = 0
        self.result: dict[str, object] | None = None

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

    def how_it_ended(self, code: int) -> str:
        result = self.result or {}
        subtype = str(result.get("subtype") or "")
        if subtype == "error_max_turns":
            return f"Qwen Code stopped at its session turn limit (exit {code})"
        if subtype == "success":
            return f"Qwen Code ended its turn (exit {code})"
        if subtype:
            error = str(result.get("error") or "")[:COMMAND_LIMIT]
            return f"Qwen Code ended with {subtype} (exit {code})" + (f": {error}" if error else "")
        return f"Qwen Code exited {code} without a result event"


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


def run(argv: list[str], log: RunLog) -> int:
    """Run Qwen with its stdout passed through line by line, SIGTERM and SIGINT
    forwarded, and the exit status kept."""
    child = subprocess.Popen(argv, stdout=subprocess.PIPE)

    def forward(signum: int, _frame: object) -> None:
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    out = sys.stdout.buffer
    assert child.stdout is not None
    for raw in child.stdout:
        out.write(raw)
        out.flush()
        log.line(raw.decode("utf-8", "replace"))
    code = child.wait()
    return code if code >= 0 else 128 - code


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
    code = run(argv, log)
    ensure_report(report_dir, log, code, repo, start)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
