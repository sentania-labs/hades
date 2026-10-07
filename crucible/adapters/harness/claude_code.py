"""The Claude Code adapter (07, S1, S1b, S5).

Launch: `claude -p --permission-mode bypassPermissions --append-system-prompt-file
IDENTITY.md --output-format stream-json --verbose --model <model>` with the pointer
prompt on stdin. `stream-json` requires `--verbose` in print mode.

Credential: the dedicated Crucible session uses the CLI's long-lived token (S1b), kept
as the file `oauth-token` in the credential directory and delivered through the CLI's
documented variable at container start, the one exception 07 allows to file-only
delivery. The top-level state file `.claude.json` is seeded beside it so the CLI finds
the state it expects, and `CLAUDE_CONFIG_DIR` points at the mounted copy so both live in
one read-only directory (S1). Parallel workers share no writable credential state.
Neither file is written back: the token does not refresh, and the
state file is state, not a credential. `settings.json` is a Crucible-owned template
mounted read-only on top, so no hook, MCP server or plugin definition reaches a worker.

Commands (issue 128): the CLI moves a Bash command that outlives its timeout to the
background, and a headless run that ends its turn then kills it on exit (reproduced on
2.1.280 against a stub model, 2026-09-25). The launch sets
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, so a command past its timeout is ended and
reported to the model instead, and both `BASH_DEFAULT_TIMEOUT_MS` and
`BASH_MAX_TIMEOUT_MS` to the launch's command timeout. The stream-json transcript's
`task_started`, `task_updated` and `task_notification` events are the CLI's own record
of a backgrounded command; one the CLI backgrounded on its own and still open at the
final `result` is a blocking call cut off, and makes a clean exit `incomplete`. One the
model asked for with `run_in_background` is a background process the model chose to
leave, and dies with the sandbox; it is not unfinished work (issue 153).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from crucible.adapters.harness import base
from crucible.adapters.harness.interruption import ProviderFailure, interruption_from, tail_lines
from crucible.domain.exit_class import ExitClass
from crucible.domain.harness_concurrency import HARNESS_CONCURRENCY
from crucible.domain.infrastructure import Interruption
from crucible.ports.harness import (
    CLAUDE_CODE_BINARY,
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

NAME = "claude_code"
# A backgrounded task that ended on its own; anything else at exit was still running.
FINISHED_TASK_STATUSES = frozenset({"completed", "failed"})
STOPPED_TASK_STATUSES = frozenset({"killed", "stopped"})
CONFIG_DIR = "/home/worker/.claude"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

AUTH_PATTERNS = base.patterns(
    "Not logged in",
    "Please run /login",
    "Invalid API key",
    "authentication_error",
    "Invalid authentication credentials",
    "OAuth token has expired",
    "OAuth token revoked",
    "invalid x-api-key",
)
QUOTA_PATTERNS = base.patterns(
    "hit your limit",
    "hit your usage limit",
    "usage limit reached",
    "out of extra usage",
    "rate_limit_error",
    "Credit balance is too low",
    # FDY-0514: a model-only refusal whose body tells the user to switch models.
    "model_requires_usage_credits",
)
# What the CLI actually emits when the subscription window is used up (first live
# sample, C5b, 08:58 CDT on 2026-09-17): a `rate_limit_event` whose status is
# "rejected", with `out_of_credits` as the overage reason, then a synthetic result with
# `terminal_reason` `api_error` and exit 1. A `rate_limit_event` alone is not a signal
# (the CLI emits one with status "allowed" on ordinary runs) and neither is a rejected
# status on its own: any failing run whose tail happens to carry one, a GitHub payload
# or a fixture among them, would otherwise be read as an exhausted quota and retried
# under the wrong rule. The signal is the two together in the one event line.
QUOTA_PATTERNS += base.correlated(
    r"rate_limit_event.*\"status\"\s*:\s*\"rejected\"",
    r"rate_limit_event.*out_of_credits",
)


# FDY-0514: text that tells the user to switch models (a model-only refusal).
_MODEL_REFUSAL_PATTERNS = base.patterns(
    "switch to a different model",
    "try a different model",
    "switch model",
    "try switching models",
)


def _provider_quota_refusal(document: Mapping[str, Any]) -> bool:
    if document.get("type") != "rate_limit_event":
        return False
    info = document.get("rate_limit_info")
    if not isinstance(info, dict):
        return False
    return any(
        info.get(key) in {"rejected", "out_of_credits"}
        for key in ("status", "overageStatus", "overageDisabledReason")
    )


def _is_account_level_refusal(document: Mapping[str, Any]) -> bool:
    """Return True when the refusal is account-level (should mark the pool), not
    model-only (which should exclude only the model and reroute within the pool).

    Account-level signals are:
    - rate_limit_event with status 'rejected' and overage 'out_of_credits'
    - out_of_credits with no model qualifier in the refusal text
    - rate-limit headers (handled elsewhere in the supervisor)
    """
    if document.get("type") != "rate_limit_event":
        return False
    info = document.get("rate_limit_info")
    if not isinstance(info, dict):
        return False
    status = info.get("status")
    return status == "rejected" and "out_of_credits" in str(
        info.get("overageReason", info.get("overageReasonCode", ""))
    )


def _is_model_refusal(text: str) -> bool:
    """Return True when *text* tells the user to switch models (a model-only signal)."""
    return base.first_match((text,), _MODEL_REFUSAL_PATTERNS) is not None


class ClaudeCodeAdapter:
    name = NAME
    concurrency = HARNESS_CONCURRENCY[NAME]
    parallel_attempts_safe = concurrency.parallel_attempts_safe
    supported_versions = VersionRange("2.1.277", "2.2.0")

    def quota_reset_at(self, stdout_tail: str, stderr_tail: str) -> datetime | None:
        return base.quota_reset_at(stdout_tail, stderr_tail, quota=QUOTA_PATTERNS)

    def provider_quota_event(
        self, stdout_tail: str, stderr_tail: str, now: datetime | None = None
    ) -> ProviderQuotaEvent | None:
        return base.provider_quota_event(
            stdout_tail,
            stderr_tail,
            predicate=_provider_quota_refusal,
            now=now,
            is_account_level=lambda doc, _line: _is_account_level_refusal(doc),
        )

    def provider_quota_exhausted(self, stdout_tail: str, stderr_tail: str) -> bool:
        return self.provider_quota_event(stdout_tail, stderr_tail) is not None

    def is_model_refusal(self, stdout_tail: str, stderr_tail: str) -> bool:
        """FDY-0514: True when the exit was quota-exhausted but only the model is at
        fault (model_requires_usage_credits or model-switch text)."""
        return _is_model_refusal(stdout_tail) or _is_model_refusal(stderr_tail)

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            prompt_on_stdin=True,
            model_flag=True,
            effort_flag=False,
            transcript_format=TranscriptFormat.STREAM_JSON,
            endpoints=("api.anthropic.com",),
            shim="AGENTS.md",
            claude_md_wins=True,
            # `setup-token` exchanges the pasted code at platform.claude.com/v1/oauth/token,
            # then asks api.anthropic.com/api/oauth/claude_cli/roles about the account
            # before it prints the token. Observed on the lab on 2026-09-29: with only
            # platform.claude.com allowed, a real code hung silently after the exchange.
            # The browser, not the CLI, visits claude.ai. api.anthropic.com is also the
            # model endpoint, but a login Pod runs only `setup-token`, never a model.
            login_endpoints=("api.anthropic.com", "platform.claude.com"),
        )

    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=NAME,
            mount_target=CONFIG_DIR,
            auth_files=(
                AuthFile("oauth-token", env_var=TOKEN_ENV, sync_back=False),
                AuthFile(".claude.json", json=True, required=False, sync_back=False),
            ),
            minimum_mode=MountMode(self.concurrency.minimum_mode),
            config_dir_env="CLAUDE_CONFIG_DIR",
            templates={"settings.json": "{}\n"},
            login_hint=(
                "CLAUDE_CONFIG_DIR=<dir> claude setup-token; the token is shown once and is "
                "kept as <dir>/oauth-token, mode 600"
            ),
        )

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        argv = (
            CLAUDE_CODE_BINARY,
            "-p",
            "--permission-mode",
            "bypassPermissions",
            "--append-system-prompt-file",
            f"{ctx.identity_mount}/IDENTITY.md",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            ctx.model,
        )
        spec = self.credential_spec()
        timeout = str(ctx.command_timeout)
        env = {
            # Issue 128: a command past its timeout is ended, never moved to the
            # background where the headless exit would kill it unseen.
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "BASH_DEFAULT_TIMEOUT_MS": timeout,
            "BASH_MAX_TIMEOUT_MS": timeout,
            **(spec.env() if ctx.credential_mounted else {}),
        }
        return AdapterLaunch(
            argv=argv,
            env=env,
            env_from_files=spec.env_from_files() if ctx.credential_mounted else {},
            stdin_text=base.POINTER_PROMPT,
            transcript_path=f"{ctx.report_mount}/{base.TRANSCRIPT_NAME}",
            workdir=ctx.repo_mount,
        )

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport:
        transcript = report_dir / base.TRANSCRIPT_NAME
        metrics, lines = _metrics(transcript)
        return base.parse_report_dir(
            report_dir,
            exit,
            metrics=metrics,
            transcript_lines=lines,
            in_flight=in_flight(transcript),
        )

    def classify_exit(
        self, exit: ExitInfo, stdout_tail: str, stderr_tail: str, report_dir: Path | None = None
    ) -> ExitClass:
        exit_class = base.classify_with_patterns(
            exit,
            stdout_tail,
            stderr_tail,
            auth=AUTH_PATTERNS,
            quota=QUOTA_PATTERNS,
            interruption=self.interruption(exit, stdout_tail, stderr_tail, report_dir),
        )
        pending = in_flight(report_dir / base.TRANSCRIPT_NAME) if report_dir else ()
        return base.with_in_flight(exit_class, pending)

    def interruption(
        self,
        exit: ExitInfo,
        stdout_tail: str,
        stderr_tail: str,
        report_dir: Path | None = None,
    ) -> Interruption | None:
        if (
            exit.exit_code in (None, 0)
            or exit.lost
            or exit.timed_out
            or exit.killed
            or exit.oom_killed
        ):
            return None
        return interruption_from(_last_api_failure(stdout_tail, stderr_tail))

    def command_tracker(self) -> CommandTracker:
        return CommandTracker()


# `api_retry` error categories that are the request's own fault: a retry of the same
# request cannot succeed, so an absent status there is not a connection failure.
_REQUEST_ERRORS = frozenset(
    {"authentication_failed", "billing_error", "invalid_request", "max_output_tokens"}
)


def _api_retry_failure(event: Mapping[str, Any]) -> ProviderFailure:
    """The CLI's `{"type":"system","subtype":"api_retry","attempt":n,"max_retries":m,
    "retry_delay_ms":d,"error_status":503,"error":"server_error"}`. `error_status` is the
    HTTP status of the failed call, null when no response came (a connection error);
    529 is Anthropic's overloaded answer, the model at capacity."""
    raw_status = event.get("error_status")
    status = (
        raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else None
    )
    category = event.get("error")
    message = (
        f"api_retry {event.get('attempt')}/{event.get('max_retries')}: "
        f"status {status if status is not None else 'none'}, error {category}"
    )
    return ProviderFailure(
        message,
        status=status,
        transport=status is None and category not in _REQUEST_ERRORS,
        quota=category == "rate_limit" and status != 529,
    )


def _last_api_failure(*tails: str) -> ProviderFailure | None:
    """The retry event of the model call the run ended on. Read last first: the CLI's
    synthetic error message and error result after the last retry are its report of that
    failure, while a real assistant message or a successful result means a later call
    worked and the retries before it are not why the run ended."""
    for line in tail_lines(*tails):
        event = base.json_object(line.strip())
        if event is None:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "api_retry":
            return _api_retry_failure(event)
        if kind == "result" and event.get("is_error") is not True:
            return None
        if kind == "assistant":
            message = event.get("message")
            synthetic = isinstance(message, dict) and message.get("model") == "<synthetic>"
            if not synthetic and "error" not in event:
                return None
    return None


def in_flight(transcript: Path) -> tuple[str, ...]:
    """Commands the CLI moved to the background on its own and still had open when its
    final `result` arrived: the auto-background trap (issue 128).

    A task the CLI reports `completed` or `failed` ended on its own, whenever that was.
    One it reports `killed` or `stopped` before the final result was ended during the
    run (the model stopped it); the same status after the final result is the exit
    killing it, which is the trap. A task with no end at all is in flight too. A task
    whose `tool_use` asked for `run_in_background` is the model's own background
    process, never counted (issue 153)."""
    events = list(base.json_lines(transcript))
    results = [i for i, event in enumerate(events) if event.get("type") == "result"]
    last_result = results[-1] if results else len(events)
    chosen = _background_requests(events)
    open_tasks: dict[str, str] = {}
    for index, event in enumerate(events):
        if event.get("type") != "system":
            continue
        subtype = event.get("subtype")
        task_id = event.get("task_id")
        if not isinstance(task_id, str):
            continue
        if subtype == "task_started" and event.get("is_backgrounded") is True:
            if event.get("tool_use_id") not in chosen:
                open_tasks[task_id] = str(event.get("description") or task_id)
        elif subtype in ("task_updated", "task_notification"):
            patch = event.get("patch")
            status = event.get("status") or (
                patch.get("status") if isinstance(patch, dict) else None
            )
            stopped_in_run = status in STOPPED_TASK_STATUSES and index < last_result
            if status in FINISHED_TASK_STATUSES or stopped_in_run:
                open_tasks.pop(task_id, None)
    return tuple(
        base.in_flight_summary(f"background task {task_id}", description)
        for task_id, description in open_tasks.items()
    )


def _background_requests(events: list[dict[str, Any]]) -> frozenset[str]:
    """The `tool_use` ids whose input asked for `run_in_background`: the model chose to
    background them. Observed on 2.1.280 (2026-09-27, stub model): the CLI's own
    backgrounding leaves the input as the model sent it, without the key, and under
    `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` the key is refused as an unexpected
    parameter, so no task starts."""
    chosen: set[str] = set()
    for event in events:
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if event.get("type") != "assistant" or not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = block.get("id")
            arguments = block.get("input")
            if not isinstance(tool_id, str) or not isinstance(arguments, dict):
                continue
            if arguments.get("run_in_background") is True:
                chosen.add(tool_id)
    return frozenset(chosen)


class CommandTracker(base.LineTracker):
    """Issue 152: tool calls the stream-json log has started and not answered.

    An `assistant` event carries each `tool_use` block before the tool runs, and the
    `user` event carrying its `tool_result` comes when it ends. A later assistant
    message from the same agent also closes them, since the model is only called again
    once every result is back; that keeps a result line too long to read from holding
    the clock forever. A backgrounded task is running until the CLI reports its end."""

    def __init__(self) -> None:
        super().__init__()
        # tool_use id -> (agent it belongs to, the message that asked, summary)
        self._tools: dict[str, tuple[str | None, str | None, str]] = {}
        self._tasks: dict[str, str] = {}

    def line(self, text: str) -> None:
        event = base.json_object(text)
        if event is None:
            return
        kind = event.get("type")
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if kind == "assistant" and isinstance(message, dict):
            agent = event.get("parent_tool_use_id")
            message_id = message.get("id")
            for open_id, (owner, asked_in, _) in list(self._tools.items()):
                if owner == agent and asked_in != message_id:
                    del self._tools[open_id]
            for block in blocks if isinstance(blocks, list) else ():
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id")
                    if isinstance(tool_id, str):
                        self._tools[tool_id] = (agent, message_id, _tool_summary(block))
        elif kind == "user":
            for block in blocks if isinstance(blocks, list) else ():
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._tools.pop(str(block.get("tool_use_id")), None)
        elif kind == "system":
            task_id = event.get("task_id")
            if not isinstance(task_id, str):
                return
            subtype = event.get("subtype")
            if subtype == "task_started" and event.get("is_backgrounded") is True:
                self._tasks[task_id] = base.in_flight_summary(
                    f"background task {task_id}", str(event.get("description") or task_id)
                )
            elif subtype in ("task_updated", "task_notification"):
                patch = event.get("patch")
                status = event.get("status") or (
                    patch.get("status") if isinstance(patch, dict) else None
                )
                if status in FINISHED_TASK_STATUSES or status in STOPPED_TASK_STATUSES:
                    self._tasks.pop(task_id, None)

    @property
    def running(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (tool_id, summary) for tool_id, (_, _, summary) in self._tools.items()
        ) + tuple(self._tasks.items())


def _tool_summary(block: dict[str, Any]) -> str:
    name = str(block.get("name") or "tool")
    arguments = block.get("input")
    detail = arguments.get("command") if isinstance(arguments, dict) else None
    return base.in_flight_summary(f"tool {name}", str(detail or block.get("id")))


def _metrics(transcript: Path) -> tuple[ReportMetrics, int]:
    """The final `result` line carries `usage`, `total_cost_usd` and `modelUsage`; the
    `system` init line names the model. Both are the CLI's own report (S1)."""
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None
    count = 0
    for event in base.json_lines(transcript):
        count += 1
        kind = event.get("type")
        if kind == "system" and isinstance(event.get("model"), str):
            model = str(event["model"])
        if kind == "result":
            tokens_in, tokens_out = base.usage_totals(event.get("usage"))
            cost = base.cost_usd(event.get("total_cost_usd"))
            used: Any = event.get("modelUsage")
            if isinstance(used, dict) and used:
                model = str(next(iter(used)))
    source = "harness_transcript" if count else "none"
    return ReportMetrics(model, tokens_in, tokens_out, cost, source), count
