"""ExitClass: the one enum contracts, policies, and failure semantics share (07, 16)."""

from __future__ import annotations

import shlex
from enum import StrEnum

EXIT_CODE_BLOCKED = 75
EXIT_CODE_ENVIRONMENT = 70


class ExitClass(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITHOUT_REPORT = "completed_without_report"
    # The harness exited while its own tooling reported a command still running (issue
    # 128): never a clean completion, whatever the exit code and the report say.
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"
    ENVIRONMENT = "environment"
    AUTH_FAILURE = "auth_failure"
    INFRASTRUCTURE = "infrastructure"
    PROVIDER_ERROR = "provider_error"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TIMEOUT = "timeout"
    # Crucible ended the worker because nothing it did was seen for the policy's
    # stall_fail_seconds (10, FDY-0140): a stall, which is not the attempt's timeout.
    STALLED = "stalled"
    KILLED = "killed"
    CRASHED = "crashed"
    LOST = "lost"
    UNKNOWN = "unknown"


# Issue 278: the shape of a stall Crucible ended before the time-based limit, kept on the
# attempt so quality feedback can count it. A loop is the same command run this many
# times in a row (`loop:wait` for a shell `wait`, `loop:empty_command` for a command with
# nothing in it, `loop:command` for any other); `no_activity` is a local-route worker
# that made no tool call before its first-response deadline.
STALL_LOOP_WAIT = "loop:wait"
STALL_LOOP_EMPTY_COMMAND = "loop:empty_command"
STALL_LOOP_COMMAND = "loop:command"
STALL_NO_ACTIVITY = "no_activity"
STALL_SHAPES: frozenset[str] = frozenset(
    {STALL_LOOP_WAIT, STALL_LOOP_EMPTY_COMMAND, STALL_LOOP_COMMAND, STALL_NO_ACTIVITY}
)
_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})


def shell_body(command: str) -> str:
    """What a command runs once its shell wrapper is taken off: `/bin/bash -lc wait` is
    `wait`. A command that is not `<shell> -c <body>` is its own body."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return command.strip()
    if not argv or argv[0].rsplit("/", 1)[-1] not in _SHELLS:
        return command.strip()
    for index, arg in enumerate(argv[1:], start=1):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
            return " ".join(argv[index + 1 : index + 2]).strip()
    return command.strip()


def loop_shape(command: str) -> str:
    """The stall shape of a command repeated in a loop (issue 278)."""
    body = shell_body(command).rstrip(";").strip()
    if not body:
        return STALL_LOOP_EMPTY_COMMAND
    if body == "wait":
        return STALL_LOOP_WAIT
    return STALL_LOOP_COMMAND


# The classes that say the harness finished its turn cleanly. A zero exit code alone
# is not enough: an `incomplete` attempt exits 0 too (issue 128).
CLEAN_EXIT_CLASSES: frozenset[ExitClass] = frozenset(
    {ExitClass.COMPLETED, ExitClass.COMPLETED_WITHOUT_REPORT}
)


def classify_exit(
    *,
    exit_code: int | None,
    report_present: bool,
    blocked_present: bool,
    lost: bool = False,
    timed_out: bool = False,
    killed: bool = False,
) -> ExitClass:
    """Deterministic classification of an attempt's exit (07 report parsing, 16 table).

    Precedence: loss, then a termination Crucible itself performed, then the code.
    """
    if lost:
        return ExitClass.LOST
    if timed_out:
        return ExitClass.TIMEOUT
    if killed:
        return ExitClass.KILLED
    if exit_code is None:
        return ExitClass.UNKNOWN
    if blocked_present and exit_code in (0, EXIT_CODE_BLOCKED):
        # FDY-0140: a model cannot choose its harness's exit code, so `blocked.md` on a
        # clean exit is the escalation whatever the code, and it wins over a report.
        return ExitClass.BLOCKED
    if exit_code == 0:
        return ExitClass.COMPLETED if report_present else ExitClass.COMPLETED_WITHOUT_REPORT
    if exit_code == EXIT_CODE_BLOCKED:
        # exit 75 without blocked.md is a plain failure (07)
        return ExitClass.CRASHED
    if exit_code == EXIT_CODE_ENVIRONMENT:
        return ExitClass.ENVIRONMENT
    return ExitClass.CRASHED
