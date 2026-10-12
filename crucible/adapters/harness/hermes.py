"""Hermes 0.19 worker adapter for local OpenAI-compatible endpoints.

Commands (issue 128): Hermes never moves a foreground command to the background; one
that outlives `TERMINAL_TIMEOUT` (default 180 seconds) is killed and the model is told
(exit 124), and a model may not ask for more than `TERMINAL_MAX_FOREGROUND_TIMEOUT`
(default 600 seconds). The launch sets both to the launch's command timeout. A model can
still start a command with `background=true`; under `-z` Hermes says it cannot deliver
the completion and exits without waiting (reproduced on 0.19.0 against a stub model,
2026-09-25). Such a process dies with the sandbox and is not unfinished work (issue
153), and a foreground command never outlives Hermes, so Hermes's exit is classified on
its usage record, exit code and report alone.

During the run (issue 152) the launch wrapper counts the entries of Hermes's own process
registry, `processes.json` in HERMES_HOME, and writes the count to stderr when it
changes, the only live evidence there is: `-z` sends Hermes's own output to /dev/null,
and a foreground command never enters the registry.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from crucible.adapters.harness import base
from crucible.adapters.harness.interruption import (
    ProviderFailure,
    interruption_from,
    status_line,
    tail_lines,
)
from crucible.domain.exit_class import EXIT_CODE_BLOCKED, ExitClass, classify_exit
from crucible.domain.harness_concurrency import HARNESS_CONCURRENCY
from crucible.domain.harness_settings import hermes_run_limits
from crucible.domain.infrastructure import Interruption
from crucible.ports.harness import (
    HERMES_BINARY,
    AdapterLaunch,
    AuthFile,
    CredentialSpec,
    ExitInfo,
    HarnessCapabilities,
    LaunchContext,
    MountMode,
    ParsedReport,
    ProviderQuotaEvent,
    ReportMetrics,
    TranscriptFormat,
    VersionRange,
)

NAME = "hermes"
HERMES_HOME = "/home/worker/.hermes"
AUTH_DIR = "/home/worker/.hermes-auth"
USAGE_NAME = "hermes-usage.json"
# Hermes's process registry checkpoint, counted by the launch wrapper (issue 152).
PROCESSES_FILE = f"{HERMES_HOME}/processes.json"
# What the launch wrapper writes to stderr when the registry's count changes (07).
RUNNING_LINE = re.compile(r"crucible-launch: commands running: (\d+)")

PROVIDER_PATTERNS = base.patterns(
    "connection refused",
    "connection error",
    "failed to connect",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "HTTP 500",
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
)
# Hades #353: what Hermes's OpenAI client writes for the call it failed on. The client
# renders a status error as "Error code: 503 - {...}" and a call with no answer as
# "Connection error." or "Request timed out."; the gateway's own page names its status.
_CLIENT_STATUS = re.compile(r"\bError code: (\d{3})\b")
_GATEWAY_WORDS = (
    ("bad gateway", 502),
    ("service unavailable", 503),
    ("gateway timeout", 504),
    ("http 502", 502),
    ("http 503", 503),
    ("http 504", 504),
)
TRANSPORT_PATTERNS = base.patterns(
    "connection refused",
    "connection reset",
    "connection error",
    "failed to connect",
    "request timed out",
)
QUOTA_PATTERNS = base.patterns(
    "quota exceeded",
    "insufficient_quota",
    "rate limit exceeded",
    "429 Too Many Requests",
)


def _usage(report_dir: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if report_dir is None:
        return None, "Hermes usage record was not available to the classifier"
    path = report_dir / USAGE_NAME
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"missing {USAGE_NAME}"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"unparsable {USAGE_NAME}: {type(exc).__name__}"
    if not isinstance(document, dict):
        return None, f"unparsable {USAGE_NAME}: root is not an object"
    completed = document.get("completed")
    failed = document.get("failed")
    if completed is None and failed is True:
        # #387: Hermes 0.19 writes `completed: null` when its agent raised. The run did
        # not complete, and the record still carries its model, session and tokens.
        completed = document["completed"] = False
    if not isinstance(completed, bool) or not isinstance(failed, bool):
        return None, f"unparsable {USAGE_NAME}: completed and failed must be booleans"
    return document, None


def _metrics(document: Mapping[str, Any] | None) -> ReportMetrics:
    if document is None:
        return ReportMetrics()

    def integer(name: str) -> int | None:
        value = document.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    cost = document.get("estimated_cost_usd")
    return ReportMetrics(
        model=str(document["model"]) if isinstance(document.get("model"), str) else None,
        tokens_in=integer("input_tokens"),
        tokens_out=integer("output_tokens"),
        # hades #604: the wrapper's usage record names the cache reads `cache_read_tokens`.
        tokens_cache_read=integer("cache_read_tokens"),
        cost_usd=float(cost)
        if isinstance(cost, int | float) and not isinstance(cost, bool)
        else None,
        source="hermes_usage",
        duration_ms=integer("duration_ms"),
        tool_calls=integer("tool_calls"),
    )


def _limit_reached(document: Mapping[str, Any] | None) -> str | None:
    """FDY-0140: the launch wrapper marks a run that ended on its turn budget."""
    if document is None or document.get("turn_limit_reached") is not True:
        return None
    calls = document.get("api_calls")
    return (
        f"Hermes reached its turn limit of {document.get('max_turns')} "
        f"after {calls} model calls and stopped before finishing"
    )


def _client_failure(*tails: str) -> ProviderFailure | None:
    """The last line in which Hermes's client reported the model call it failed on."""
    for line in tail_lines(*tails):
        text = line.strip()
        lowered = text.lower()
        client = _CLIENT_STATUS.search(text)
        status = int(client.group(1)) if client else status_line(text)
        if status is None:
            status = next((code for words, code in _GATEWAY_WORDS if words in lowered), None)
        transport = base.first_match((text,), TRANSPORT_PATTERNS) is not None
        if status is not None or transport:
            return ProviderFailure(text[:2000], status=status, transport=transport)
    return None


class HermesAdapter:
    name = NAME
    concurrency = HARNESS_CONCURRENCY[NAME]
    parallel_attempts_safe = concurrency.parallel_attempts_safe
    supported_versions = VersionRange("0.19.0", "0.20.0")

    def quota_reset_at(self, stdout_tail: str, stderr_tail: str) -> datetime | None:
        return None

    def provider_quota_event(
        self, stdout_tail: str, stderr_tail: str, now: datetime | None = None
    ) -> ProviderQuotaEvent | None:
        # Hermes 0.19 has no structured provider-refusal event. Text may classify this
        # attempt, but it can never write shared pool exhaustion state.
        return None

    def provider_quota_exhausted(self, stdout_tail: str, stderr_tail: str) -> bool:
        return False

    def command_tracker(self) -> CommandTracker:
        return CommandTracker()

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            prompt_on_stdin=False,
            model_flag=True,
            effort_flag=False,
            transcript_format=TranscriptFormat.NONE,
            endpoints=(),
            shim=None,
        )

    def credential_spec(self) -> CredentialSpec | None:
        return CredentialSpec(
            harness=NAME,
            mount_target=AUTH_DIR,
            auth_files=(AuthFile("api-key", env_var="OPENAI_API_KEY", sync_back=False),),
            minimum_mode=MountMode(self.concurrency.minimum_mode),
            required_for_launch=False,
            login_hint="Paste the LiteLLM virtual key in the Crucible admin credential panel",
        )

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        if ctx.endpoint != "local" or ctx.endpoint_url is None:
            raise ValueError("Hermes is supported only with a configured local endpoint")
        usage_path = f"{ctx.report_mount}/{USAGE_NAME}"
        limits = hermes_run_limits(ctx.harness_settings)
        # Whole seconds, rounded up; the launch value is already within the attempt.
        seconds = str(max(1, -(-ctx.command_timeout // 1000)))
        spec = self.credential_spec()
        assert spec is not None
        return AdapterLaunch(
            argv=(
                HERMES_BINARY,
                "--ignore-user-config",
                # This disables AGENTS.md, skills, and memory injection. The identity
                # bundle remains the only instruction source for the attempt.
                "--ignore-rules",
                # Safe mode also disables plugins and MCP servers. The explicit flags
                # stay present so this launch shape documents each boundary directly.
                "--safe-mode",
                # The container is the permission boundary, as for the other adapters.
                "--yolo",
                "--provider",
                "openai-api",
                "--model",
                ctx.model,
                "--toolsets",
                "terminal,file",
                "--usage-file",
                usage_path,
                "-z",
                base.POINTER_PROMPT,
            ),
            env={
                # FDY-0140: no PATH of its own. The wrapper starts Hermes with the venv's
                # Python by path, so the commands the model runs find the image's
                # `python3`, `pytest` and `uv`, never the Hermes venv's.
                "HERMES_HOME": HERMES_HOME,
                # FDY-0140: the instructions go in the prompt, not only a pointer.
                "CRUCIBLE_HERMES_IDENTITY": f"{ctx.identity_mount}/IDENTITY.md",
                # FDY-0140: the run limits the Local gateway page sets.
                "CRUCIBLE_HERMES_MAX_TURNS": str(limits.max_turns),
                "CRUCIBLE_HERMES_CONTEXT_LENGTH": str(limits.context_length),
                # Hades #388: the response allowance the gateway reserves out of that
                # window, and the routing entry's thinking setting. The wrapper writes
                # the allowance as Hermes's `model.max_tokens`, so the compressor
                # budgets input against the window less it and each request carries it.
                "CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS": str(limits.max_output_tokens),
                "CRUCIBLE_HERMES_THINKING": "true" if limits.thinking else "false",
                "OPENAI_BASE_URL": ctx.endpoint_url,
                "OPENAI_API_KEY": "local-no-auth",
                "CRUCIBLE_HERMES_USAGE": usage_path,
                # Issue 128: Hermes reads both in whole seconds.
                "TERMINAL_TIMEOUT": seconds,
                "TERMINAL_MAX_FOREGROUND_TIMEOUT": seconds,
                # Issue 152: the process registry, counted while Hermes runs.
                "CRUCIBLE_IN_FLIGHT_FILE": PROCESSES_FILE,
            },
            env_from_files=spec.env_from_files() if ctx.credential_mounted else {},
            transcript_path=f"{ctx.report_mount}/{base.TRANSCRIPT_NAME}",
            workdir=ctx.repo_mount,
        )

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport:
        usage, error = _usage(report_dir)
        transcript = report_dir / base.TRANSCRIPT_NAME
        text = base.read_text(transcript) if transcript.is_file() else None
        parsed = base.parse_report_dir(
            report_dir,
            exit,
            metrics=_metrics(usage),
            transcript_lines=len(text.splitlines()) if text is not None else 0,
        )
        return ParsedReport(
            claim=parsed.claim,
            raw=parsed.raw,
            errors=parsed.errors,
            blocked_md=parsed.blocked_md,
            report_present=parsed.report_present,
            progress=parsed.progress,
            metrics=parsed.metrics,
            transcript_lines=parsed.transcript_lines,
            transcript_name=parsed.transcript_name,
            run_evidence_error=error,
            limit_reached=_limit_reached(usage),
        )

    def classify_exit(
        self,
        exit: ExitInfo,
        stdout_tail: str,
        stderr_tail: str,
        report_dir: Path | None = None,
    ) -> ExitClass:
        if exit.lost:
            return ExitClass.LOST
        if exit.timed_out:
            return ExitClass.TIMEOUT
        if exit.killed:
            return ExitClass.KILLED
        if exit.oom_killed:
            return ExitClass.ENVIRONMENT
        if exit.blocked_present and exit.exit_code == 0:
            # FDY-0140: the model cannot set Hermes's exit code, so `blocked.md` on a
            # clean exit is its escalation. Hermes's own 75 is a provider failure below.
            return ExitClass.BLOCKED
        usage, _ = _usage(report_dir)
        tails = (stdout_tail[-base.TAIL_LIMIT :], stderr_tail[-base.TAIL_LIMIT :])
        quota = base.first_match(tails, QUOTA_PATTERNS) is not None
        provider_error = base.first_match(tails, PROVIDER_PATTERNS) is not None
        interruption = self.interruption(exit, stdout_tail, stderr_tail, report_dir)
        if usage is not None and usage.get("failed") is True:
            if quota:
                return ExitClass.QUOTA_EXHAUSTED
            if interruption is not None:
                return interruption.exit_class
            return ExitClass.PROVIDER_ERROR if provider_error else ExitClass.CRASHED
        if exit.exit_code == EXIT_CODE_BLOCKED:
            if quota:
                return ExitClass.QUOTA_EXHAUSTED
            return interruption.exit_class if interruption is not None else ExitClass.PROVIDER_ERROR
        if provider_error:
            return ExitClass.PROVIDER_ERROR
        if usage is not None and usage.get("completed") is True:
            return (
                ExitClass.COMPLETED if exit.report_present else ExitClass.COMPLETED_WITHOUT_REPORT
            )
        if quota:
            return ExitClass.QUOTA_EXHAUSTED
        # A missing usage record is handled as an evidence anomaly. The report still
        # decides whether a clean process reached the ordinary completion path.
        return classify_exit(
            exit_code=exit.exit_code,
            report_present=exit.report_present,
            blocked_present=exit.blocked_present,
        )

    def interruption(
        self,
        exit: ExitInfo,
        stdout_tail: str,
        stderr_tail: str,
        report_dir: Path | None = None,
    ) -> Interruption | None:
        """Hermes 0.19 writes no error event. Its own word that the provider failed is
        the usage record's `failed: true` or its exit 75; the client's message for the
        failed call then says whether the gateway answered 502, 503 or 504 or did not
        answer at all. A 500 or a 4xx stays `provider_error`."""
        if exit.lost or exit.timed_out or exit.killed or exit.oom_killed:
            return None
        if exit.blocked_present and exit.exit_code == 0:
            return None
        usage, _ = _usage(report_dir)
        failed = usage is not None and usage.get("failed") is True
        if not failed and exit.exit_code != EXIT_CODE_BLOCKED:
            return None
        return interruption_from(_client_failure(stdout_tail, stderr_tail))


class CommandTracker(base.LineTracker):
    """Issue 152: the launch wrapper's count of the commands in Hermes's process
    registry, written whenever it changes. Under `-z` Hermes itself writes nothing while
    it works, and a foreground command is not in the registry, so only a background
    command is seen."""

    def __init__(self) -> None:
        super().__init__()
        self._count = 0

    def line(self, text: str) -> None:
        match = RUNNING_LINE.fullmatch(text)
        if match is not None:
            self._count = int(match.group(1))

    @property
    def running(self) -> tuple[tuple[str, str], ...]:
        if not self._count:
            return ()
        # One conceptual entry: the registry's count, not one id per process.
        return (
            (
                "process registry",
                base.in_flight_summary("process registry", f"{self._count} running"),
            ),
        )
