"""Qwen Code 0.25: local gateway, read-only Hermes credential, stream-json evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from crucible.adapters.harness import base
from crucible.adapters.harness.hermes import HermesAdapter
from crucible.domain.exit_class import ExitClass, classify_exit
from crucible.domain.harness_concurrency import HARNESS_CONCURRENCY
from crucible.domain.harness_settings import (
    DEFAULT_QWEN_CONTEXT_LENGTH,
    DEFAULT_QWEN_MAX_OUTPUT_TOKENS,
)
from crucible.domain.infrastructure import Interruption
from crucible.ports.harness import (
    AdapterLaunch,
    CredentialSpec,
    ExitInfo,
    HarnessCapabilities,
    LaunchContext,
    ParsedReport,
    ProviderQuotaEvent,
    ReportMetrics,
    TranscriptFormat,
    VersionRange,
)

NAME = "qwen_code"
# The engine's full window, not an input-only allowance. Qwen clamps its output
# request to the remaining window. RoutingModel.context_length overrides this.
DEFAULT_CONTEXT_LENGTH = DEFAULT_QWEN_CONTEXT_LENGTH


def context_length(settings: Mapping[str, Any]) -> int:
    value = settings.get("context_length")
    if value is None:
        return DEFAULT_CONTEXT_LENGTH
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("Qwen context_length must be a positive integer")
    return value


def max_output_tokens(settings: Mapping[str, Any]) -> int:
    """hades #498: the response cap, from the attempt's effective settings."""
    value = settings.get("max_output_tokens")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_QWEN_MAX_OUTPUT_TOKENS
    return value


def _result(report_dir: Path | None) -> dict[str, Any] | None:
    if report_dir is None:
        return None
    result = None
    for event in base.json_lines(report_dir / base.TRANSCRIPT_NAME):
        if event.get("type") == "result":
            result = event
    return result


def _stats_usage(stats: Any) -> tuple[int | None, int | None, int | None]:
    """Qwen Code's session `stats`: `models.<name>.tokens` with `prompt`, `candidates`
    and `cached`, summed over the models."""
    models = stats.get("models") if isinstance(stats, dict) else None
    tokens_in = tokens_out = cache = None
    for entry in models.values() if isinstance(models, dict) else ():
        tokens = entry.get("tokens") if isinstance(entry, dict) else None
        if not isinstance(tokens, dict):
            continue
        tokens_in = base.add(tokens_in, base.integer(tokens.get("prompt")))
        tokens_out = base.add(tokens_out, base.integer(tokens.get("candidates")))
        cache = base.add(cache, base.integer(tokens.get("cached")))
    return tokens_in, tokens_out, cache


def _usage(report_dir: Path) -> tuple[int | None, int | None, int | None]:
    """hades #604: (tokens in, tokens out, cache reads) of the whole run. Every launch
    ends in its own result event (a relaunch after a transport error is a new session,
    hades #490), so the run is the sum over result events. A result's `usage` is read
    first and its `stats` when it has no usage; Qwen reports no cost."""
    tokens_in = tokens_out = cache = None
    for event in base.json_lines(report_dir / base.TRANSCRIPT_NAME):
        if event.get("type") != "result":
            continue
        usage = event.get("usage")
        i, o = base.usage_totals(usage)
        c = base.cache_read(usage)
        if i is None and o is None:
            i, o, c = _stats_usage(event.get("stats"))
        tokens_in = base.add(tokens_in, i)
        tokens_out = base.add(tokens_out, o)
        cache = base.add(cache, c)
    return tokens_in, tokens_out, cache


class QwenCodeAdapter:
    # credential_spec.harness stays "hermes": Kubernetes
    # seeds the existing lab-local Secret, not a second Qwen credential.
    name = NAME
    concurrency = HARNESS_CONCURRENCY[NAME]
    parallel_attempts_safe = concurrency.parallel_attempts_safe
    supported_versions = VersionRange("0.25.0", "0.26.0")

    def credential_spec(self) -> CredentialSpec | None:
        return HermesAdapter().credential_spec()

    def quota_reset_at(self, stdout_tail: str, stderr_tail: str) -> datetime | None:
        return None

    def provider_quota_event(
        self, stdout_tail: str, stderr_tail: str, now: datetime | None = None
    ) -> ProviderQuotaEvent | None:
        return None

    def provider_quota_exhausted(self, stdout_tail: str, stderr_tail: str) -> bool:
        return False

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            prompt_on_stdin=False,
            model_flag=True,
            effort_flag=False,
            transcript_format=TranscriptFormat.STREAM_JSON,
            endpoints=(),
        )

    def command_tracker(self) -> CommandTracker:
        return CommandTracker()

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        if ctx.endpoint != "local" or ctx.endpoint_url is None:
            raise ValueError("Qwen Code requires a configured local endpoint")
        credential = self.credential_spec()
        assert credential is not None
        return AdapterLaunch(
            argv=(
                "/usr/local/bin/crucible-qwen-code",
                "--yolo",
                "--auth-type",
                "openai",
                "--advisor",
                "off",
                "--output-format",
                "stream-json",
                "--max-session-turns",
                "300",
                base.POINTER_PROMPT,
            ),
            env={
                "OPENAI_BASE_URL": ctx.endpoint_url,
                "OPENAI_MODEL": ctx.model,
                "OPENAI_API_KEY": "local-no-auth",
                "CRUCIBLE_QWEN_IDENTITY": f"{ctx.identity_mount}/IDENTITY.md",
                "CRUCIBLE_QWEN_CONTEXT_LENGTH": str(context_length(ctx.harness_settings)),
                # hades #498, Hermes parity: the response cap and thinking, which is
                # always off for Qwen; the wrapper writes both into Qwen's settings and
                # restricts the tools to file and shell (no sub-agent, skill, memory,
                # web or MCP tool) and loads no context file as rules.
                "CRUCIBLE_QWEN_MAX_OUTPUT_TOKENS": str(max_output_tokens(ctx.harness_settings)),
                "CRUCIBLE_QWEN_THINKING": "false",
                # Where the wrapper looks after the run for report.yaml or blocked.md,
                # and writes a minimal report from the run log when it finds neither.
                "CRUCIBLE_QWEN_REPORT_DIR": ctx.report_mount,
            },
            env_from_files=credential.env_from_files() if ctx.credential_mounted else {},
            transcript_path=f"{ctx.report_mount}/{base.TRANSCRIPT_NAME}",
            workdir=ctx.repo_mount,
        )

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport:
        result = _result(report_dir)
        model = None
        tracker = CommandTracker()
        lines = 0
        calls: set[str] = set()
        for event in base.json_lines(report_dir / base.TRANSCRIPT_NAME):
            lines += 1
            if event.get("type") == "system" and isinstance(event.get("model"), str):
                model = event["model"]
            tracker.event(event)
            calls.update(tracker.ids)
        tokens_in, tokens_out, cache = _usage(report_dir)
        duration = (result or {}).get("duration_ms")
        metrics = ReportMetrics(
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cache_read=cache,
            source="qwen_stream_json",
            tool_calls=len(calls),
            duration_ms=duration if isinstance(duration, int) else None,
        )
        parsed = base.parse_report_dir(
            report_dir,
            exit,
            metrics=metrics,
            transcript_lines=lines,
            in_flight=[summary for _, summary in tracker.running],
        )
        return replace(
            parsed,
            run_evidence_error=None if result else "missing Qwen stream-json result event",
            limit_reached=(
                "Qwen reached its session turn limit"
                if result and result.get("subtype") == "error_max_turns"
                else None
            ),
        )

    def classify_exit(
        self,
        exit: ExitInfo,
        stdout_tail: str,
        stderr_tail: str,
        report_dir: Path | None = None,
    ) -> ExitClass:
        classified = classify_exit(
            exit_code=exit.exit_code,
            report_present=exit.report_present,
            blocked_present=exit.blocked_present,
            lost=exit.lost,
            timed_out=exit.timed_out,
            killed=exit.killed,
        )
        if classified in (ExitClass.LOST, ExitClass.TIMEOUT, ExitClass.KILLED):
            return classified
        if exit.oom_killed:
            return ExitClass.ENVIRONMENT
        if classified is ExitClass.BLOCKED:
            return classified
        result = _result(report_dir)
        if result is None:
            for line in stdout_tail.splitlines():
                event = base.json_object(line)
                if event and event.get("type") == "result":
                    result = event
        failed = (
            result is not None
            and result.get("is_error") is True
            and result.get("subtype") in {"error_during_execution", "error_max_turns"}
        )
        if exit.exit_code != 0 or failed:
            if result and result.get("subtype") == "error_max_turns":
                return ExitClass.INCOMPLETE
            # A result error belongs to the harness. Do not classify a quoted error
            # inside an assistant/tool event as the provider's failure.
            error = result.get("error", "") if result else ""
            plain_stdout = "\n".join(
                line
                for line in stdout_tail[-base.TAIL_LIMIT :].splitlines()
                if base.json_object(line) is None
            )
            text = f"{plain_stdout}\n{stderr_tail[-base.TAIL_LIMIT :]}\n{error}"
            if base.first_match(
                (text,), base.patterns("429", "quota exceeded", "insufficient_quota", "rate limit")
            ):
                return ExitClass.QUOTA_EXHAUSTED
            if base.first_match(
                (text,),
                base.patterns(
                    "400",
                    "context length",
                    "context window",
                    "connection error",
                    "500",
                    "502",
                    "503",
                    "504",
                ),
            ):
                return ExitClass.PROVIDER_ERROR
            if failed:
                return ExitClass.PROVIDER_ERROR
        if report_dir is not None:
            tracker = CommandTracker()
            for event in base.json_lines(report_dir / base.TRANSCRIPT_NAME):
                tracker.event(event)
            return base.with_in_flight(classified, [text for _, text in tracker.running])
        return classified

    def interruption(
        self,
        exit: ExitInfo,
        stdout_tail: str,
        stderr_tail: str,
        report_dir: Path | None = None,
    ) -> Interruption | None:
        # Qwen's result identifies a failed run, but does not carry a typed gateway
        # interruption. Do not promote free-form tool output to shared pool state.
        return None


class CommandTracker(base.LineTracker):
    """Qwen's assistant tool_use blocks paired with user tool_result blocks."""

    def __init__(self) -> None:
        super().__init__()
        self._tools: dict[str, str] = {}
        self.ids: set[str] = set()

    def line(self, text: str) -> None:
        event = base.json_object(text)
        if event is not None:
            self.event(event)

    def event(self, event: dict[str, Any]) -> None:
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        for block in blocks if isinstance(blocks, list) else ():
            if not isinstance(block, dict):
                continue
            if event.get("type") == "assistant" and block.get("type") == "tool_use":
                tool_id = block.get("id")
                if not isinstance(tool_id, str):
                    continue
                self.ids.add(tool_id)
                args = block.get("input")
                command = args.get("command") if isinstance(args, dict) else None
                self._tools[tool_id] = base.in_flight_summary(
                    str(block.get("name") or "tool"), str(command or block.get("name") or "tool")
                )
            elif event.get("type") == "user" and block.get("type") == "tool_result":
                self._tools.pop(str(block.get("tool_use_id")), None)

    @property
    def running(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._tools.items())
