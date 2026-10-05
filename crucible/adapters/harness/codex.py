"""The Codex adapter (07, S1, S2, S5, S6).

Launch: `codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check
--disable plugins -c check_for_update_on_startup=false --json -o <last message>
--model <model> -C <checkout>` with IDENTITY.md followed by the pointer prompt on stdin.
The bypass flag stays because Codex's own sandbox cannot run inside the worker (S2); the
container is the boundary. `--skip-git-repo-check` because the checkout belongs to the
container's uid and not to a host user Codex would trust (S1). `--disable plugins` keeps
it off github.com and chatgpt.com (S6). The update opt-out is a launch flag, not image
state (S7, S11).

Credential: Hades holds the login and refreshes it in renewer mode. Workers receive
only access-token.json and run app-server with external authentication. Rollback uses
rw-narrow auth.json copies with last_refresh sync-back and per_harness.codex: 1.
config.toml is a Crucible-owned template; operator settings never reach a worker.

Commands (issue 128): the pinned models run commands through unified exec
(`shell_type: unified_exec` in the CLI's own model catalog), which returns after at most
30 seconds with a session the model must poll with `write_stdin`; a turn that ends
instead leaves the command to die with the process (reproduced on 0.156.0 against a
stub model, 2026-09-25). Turning the `unified_exec` feature off does not change the
tool, so nothing at the launch can make a command block. The launch sets
`background_terminal_max_timeout`, the longest single poll, to the launch's command
timeout so one poll can wait out a long build. A session the model leaves open when it
ends its turn is a background process that dies with the sandbox, not unfinished work
(issue 153): Codex exits only once the turn is over, so it never exits while a command
it was waiting on is still running, and its exit is classified on the exit code and
the report alone. While the run lasts, an open `command_execution` item still counts as
activity for the stall clock (issue 152). The same live log says when the model runs one
command over and over, or has called no tool at all (issue 278).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from crucible.adapters.harness import base
from crucible.adapters.harness.hermes import HermesAdapter
from crucible.adapters.harness.interruption import (
    ProviderFailure,
    file_tail,
    interruption_from,
    status_line,
    tail_lines,
)
from crucible.domain.exit_class import ExitClass
from crucible.domain.harness_concurrency import HARNESS_CONCURRENCY
from crucible.domain.harness_settings import DEFAULT_HERMES_CONTEXT_LENGTH, hermes_run_limits
from crucible.domain.infrastructure import Interruption
from crucible.ports.harness import (
    CODEX_BINARY,
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

NAME = "codex"
CONFIG_DIR = "/home/worker/.codex"
LAST_MESSAGE = "codex-last-message.md"
CONFIG_TEMPLATE = (
    "# Crucible-owned Codex configuration template (12). The credential directory a\n"
    "# worker sees holds auth.json and this file; per-project trust, MCP servers and\n"
    "# hooks are never inherited from a worker or from the operator.\n"
)

AUTH_PATTERNS = base.patterns(
    "401 Unauthorized",
    "Missing bearer or basic authentication",
    "unauthorized",
    "Not logged in",
    "not authenticated",
    "codex login",
    "invalid_api_key",
    "Your ChatGPT session has expired",
)
QUOTA_PATTERNS = base.patterns(
    "usage_limit_reached",
    "usage limit",
    "insufficient_quota",
    "rate_limit_exceeded",
    "Rate limit reached",
    "429 Too Many Requests",
)


def _provider_quota_refusal(document: Mapping[str, Any]) -> bool:
    error = document.get("error")
    return (
        document.get("type") == "turn.failed"
        and isinstance(error, dict)
        and error.get("code") == "usage_limit_reached"
    )


class CodexAdapter:
    name = NAME
    concurrency = HARNESS_CONCURRENCY[NAME]
    parallel_attempts_safe = concurrency.parallel_attempts_safe
    supported_versions = VersionRange("0.153.0", "0.157.0")

    def quota_reset_at(self, stdout_tail: str, stderr_tail: str) -> datetime | None:
        return base.quota_reset_at(stdout_tail, stderr_tail, quota=QUOTA_PATTERNS)

    def provider_quota_event(self, stdout_tail: str, stderr_tail: str) -> ProviderQuotaEvent | None:
        return base.provider_quota_event(
            stdout_tail, stderr_tail, predicate=_provider_quota_refusal
        )

    def provider_quota_exhausted(self, stdout_tail: str, stderr_tail: str) -> bool:
        return self.provider_quota_event(stdout_tail, stderr_tail) is not None

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            prompt_on_stdin=True,
            model_flag=True,
            effort_flag=True,
            transcript_format=TranscriptFormat.JSON_EVENTS,
            # S6 named api.openai.com and auth.openai.com, with chatgpt.com "only if the
            # authenticated re-run shows the ChatGPT-plan backend needs it". The C5 live
            # run showed exactly that: with `auth_mode = chatgpt` the CLI reconnects to
            # chatgpt.com until it is permitted. ab.chatgpt.com stays denied.
            endpoints=("api.openai.com", "auth.openai.com", "chatgpt.com"),
            shim="AGENTS.md",
            # `login --device-auth` asks auth.openai.com for the user code, polls its
            # deviceauth token endpoint and exchanges at /oauth/token there (the pinned
            # 0.156.0 binary's strings, 2026-09-24). api.openai.com is the model API.
            login_endpoints=("auth.openai.com",),
        )

    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=NAME,
            mount_target=CONFIG_DIR,
            auth_files=(
                AuthFile(
                    "auth.json", json=True, json_keys=("tokens",), issued_at=("last_refresh",)
                ),
            ),
            minimum_mode=MountMode.RENEWER,
            config_dir_env="CODEX_HOME",
            templates={"config.toml": CONFIG_TEMPLATE},
            login_hint=(
                "CODEX_HOME=<dir> codex login --device-auth; the device code expires in 15 minutes"
            ),
        )

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        if ctx.credential_mode is MountMode.RENEWER and ctx.endpoint == "subscription":
            argv = [
                "/usr/local/bin/crucible-codex-host",
                "--token-file",
                f"{CONFIG_DIR}/access-token.json",
                "--transcript",
                f"{ctx.report_mount}/{base.TRANSCRIPT_NAME}",
                "--last-message",
                f"{ctx.report_mount}/{LAST_MESSAGE}",
                "--cwd",
                ctx.repo_mount,
                "--model",
                ctx.model,
            ]
            if ctx.effort:
                argv += ["--effort", ctx.effort]
            return AdapterLaunch(
                argv=tuple(argv),
                stdin_files=(f"{ctx.identity_mount}/IDENTITY.md",),
                stdin_text=base.POINTER_PROMPT,
                transcript_path=None,
                workdir=ctx.repo_mount,
            )
        argv = [
            CODEX_BINARY,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--disable",
            "plugins",
            "-c",
            "check_for_update_on_startup=false",
            # Issue 128: the longest poll of a running command, from the launch.
            "-c",
            f"background_terminal_max_timeout={ctx.command_timeout}",
        ]
        if ctx.effort:
            argv += ["-c", f"model_reasoning_effort={_toml_string(ctx.effort)}"]
        argv += [
            "--json",
            "-o",
            f"{ctx.report_mount}/{LAST_MESSAGE}",
            "--model",
            ctx.model,
            "-C",
            ctx.repo_mount,
        ]
        spec = self.credential_spec()
        env = spec.env() if ctx.credential_mounted else {}
        env_from_files: dict[str, str] = {}
        if ctx.endpoint == "local" and ctx.endpoint_url:
            gateway_credential = HermesAdapter().credential_spec()
            assert gateway_credential is not None
            limits = hermes_run_limits(ctx.harness_settings)
            context_window = limits.context_length or DEFAULT_HERMES_CONTEXT_LENGTH
            env = {
                "CODEX_HOME": CONFIG_DIR,
                "OPENAI_API_KEY": "local-no-auth",
                "CRUCIBLE_CODEX_CONFIG": (
                    'model_provider = "local_gateway"\n'
                    f"model_context_window = {context_window}\n"
                    "[model_providers.local_gateway]\n"
                    'name = "Local gateway"\n'
                    f"base_url = {_toml_string(ctx.endpoint_url)}\n"
                    'env_key = "OPENAI_API_KEY"\n'
                    'wire_api = "responses"\n'
                ),
            }
            if ctx.credential_mounted:
                env_from_files = gateway_credential.env_from_files()
        return AdapterLaunch(
            argv=tuple(argv),
            env=env,
            env_from_files=env_from_files,
            stdin_files=(f"{ctx.identity_mount}/IDENTITY.md",),
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
        )

    def classify_exit(
        self, exit: ExitInfo, stdout_tail: str, stderr_tail: str, report_dir: Path | None = None
    ) -> ExitClass:
        interruption = self.interruption(exit, stdout_tail, stderr_tail, report_dir)
        if exit.exit_code == 0 and interruption is not None and not exit.blocked_present:
            # The app-server host exits 0 after a turn that ended `failed`; with no
            # report the failed turn, not the exit code, says how the run ended.
            return interruption.exit_class
        return base.classify_with_patterns(
            exit,
            stdout_tail,
            stderr_tail,
            auth=AUTH_PATTERNS,
            quota=QUOTA_PATTERNS,
            interruption=interruption,
        )

    def interruption(
        self,
        exit: ExitInfo,
        stdout_tail: str,
        stderr_tail: str,
        report_dir: Path | None = None,
    ) -> Interruption | None:
        if exit.lost or exit.timed_out or exit.killed or exit.oom_killed:
            return None
        # The app-server host writes the transcript itself and nothing to stdout; `codex
        # exec --json` writes its events to stdout, which the launch also keeps as the
        # transcript.
        transcript = file_tail(report_dir / base.TRANSCRIPT_NAME if report_dir else None)
        failure, turn_failed = _last_failure(transcript, stdout_tail, stderr_tail)
        if exit.exit_code == 0 and not (turn_failed and not exit.report_present):
            return None
        return interruption_from(failure)

    def command_tracker(self) -> CommandTracker:
        return CommandTracker()


# Issue 278: the `--json` item types that are the model calling a tool. A
# `command_execution` is counted for loops too; a `file_change` is the model editing,
# which ends any run of repeats, since a command repeated around edits is iteration.
_TOOL_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})
# The events `codex exec --json` writes once the turn has begun.
_TURN_BEGINS = frozenset({"thread.started", "turn.started"})


class CommandTracker(base.LineTracker):
    """Issue 152: `command_execution` items the live `--json` log started and has not
    completed. Only commands count: other items (a todo list) stay open for a turn.

    Issue 278: also the run of identical commands the log ends with, whether the turn
    has begun and whether the model has called any tool (a CommandLoopTracker)."""

    def __init__(self) -> None:
        super().__init__()
        self._started: dict[str, str] = {}
        self._counted: set[str] = set()
        self._last_command: str | None = None
        self._repeats = 0
        self._responding = False
        self._tool_called = False

    def line(self, text: str) -> None:
        event = base.json_object(text) or {}
        kind = event.get("type")
        if kind in _TURN_BEGINS:
            self._responding = True
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") not in _TOOL_ITEMS:
            return
        self._responding = True
        self._tool_called = True
        if item.get("type") == "file_change":
            self._last_command, self._repeats = None, 0
        if item.get("type") != "command_execution":
            return
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return
        command = str(item.get("command") or "")
        if kind == "item.started":
            if item_id not in self._counted:
                self._counted.add(item_id)
                self._count(command)
            self._started[item_id] = base.in_flight_summary(
                f"command {item_id}", command or item_id
            )
        elif kind == "item.completed":
            if item_id in self._counted:
                self._counted.discard(item_id)
            else:
                # A command that failed before it started can arrive completed only.
                self._count(command)
            self._started.pop(item_id, None)

    def _count(self, command: str) -> None:
        if command == self._last_command:
            self._repeats += 1
        else:
            self._last_command, self._repeats = command, 1

    @property
    def running(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._started.items())

    @property
    def repeated(self) -> tuple[str, int] | None:
        if self._last_command is None:
            return None
        return self._last_command, self._repeats

    @property
    def responding(self) -> bool:
        return self._responding

    @property
    def tool_called(self) -> bool:
        return self._tool_called


# Codex app-server `codexErrorInfo` variants that carry the HTTP status of the call that
# failed; with no status the request got no answer at all.
_STATUS_ERRORS = frozenset(
    {
        "httpConnectionFailed",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
        "responseTooManyFailedAttempts",
    }
)
_TURN_ENDS = frozenset({"turn.completed", "turn.failed", "turn/completed", "turn/failed"})


def _app_server_failure(error: Any) -> ProviderFailure | None:
    """An app-server `TurnError`: `message`, and `codexErrorInfo` either a unit variant
    (`"serverOverloaded"`) or a struct variant (`{"responseStreamDisconnected":
    {"httpStatusCode": 503}}`)."""
    if not isinstance(error, dict):
        return None
    message = str(error.get("message") or "")
    info = error.get("codexErrorInfo")
    status: int | None = None
    transport = False
    if isinstance(info, dict) and len(info) == 1:
        name, body = next(iter(info.items()))
        code = body.get("httpStatusCode") if isinstance(body, dict) else None
        if name in _STATUS_ERRORS:
            status = code if isinstance(code, int) and not isinstance(code, bool) else None
            transport = status is None and name != "responseTooManyFailedAttempts"
    return ProviderFailure(
        message,
        status=status or status_line(message),
        transport=transport,
        capacity=info == "serverOverloaded",
        quota=info == "usageLimitExceeded",
    )


def _exec_failure(message: Any, code: Any = None) -> ProviderFailure:
    """A `codex exec --json` error: `turn.failed` carries `{"message": ...}` and the
    `error` event a bare `message`, the CLI's own rendering of the error. The status is
    the HTTP status line it renders ("unexpected status 503 Service Unavailable: ...",
    "exceeded retry limit, last status: 503 Service Unavailable"); a request that got no
    answer is "stream disconnected before completion" or reqwest's "error sending
    request"; capacity is its fixed `ServerOverloaded` text."""
    text = str(message or "")
    return ProviderFailure(
        text,
        status=status_line(text),
        transport=text.startswith("stream disconnected before completion")
        or "error sending request for url" in text,
        capacity=text.startswith("Selected model is at capacity"),
        quota=code in {"usage_limit_reached", "insufficient_quota", "rate_limit_exceeded"},
    )


def _event_failure(event: Mapping[str, Any]) -> tuple[str, ProviderFailure | None, bool]:
    """(kind, failure, the turn failed) for one transcript event. The app-server host
    keeps each notification as it came, with `type` added from its `method`."""
    method = event.get("method")
    kind = str(method if isinstance(method, str) else event.get("type") or "")
    params = event.get("params")
    body: Mapping[str, Any] = params if isinstance(params, dict) else {}
    if kind == "turn/completed":
        turn = body.get("turn")
        if isinstance(turn, dict) and turn.get("status") == "failed":
            return kind, _app_server_failure(turn.get("error")), True
        return kind, None, False
    if kind == "turn/failed":
        return kind, _app_server_failure(body.get("error")), True
    if kind == "error" and isinstance(method, str):
        return kind, _app_server_failure(body.get("error")), False
    if kind == "turn.failed":
        error = event.get("error")
        if isinstance(error, dict):
            return kind, _exec_failure(error.get("message"), error.get("code")), True
        return kind, _exec_failure(error), True
    if kind == "error":
        return kind, _exec_failure(event.get("message")), False
    item = event.get("item")
    if kind == "item.completed" and isinstance(item, dict) and item.get("type") == "error":
        # exec reports a non-fatal error (a stream retry, a warning) as an error item.
        return "error", _exec_failure(item.get("message")), False
    return kind, None, False


def _last_failure(*tails: str) -> tuple[ProviderFailure | None, bool]:
    """The failure that ended the last turn, and whether a turn ended `failed`.

    Read last first. A turn that completed ends the search: whatever failed before it
    was retried and succeeded. A failed turn whose own error names no status takes the
    status from the `error` events that came before it in the same turn, which is where
    the app-server and exec both put the retried call's HTTP status. An item after an
    error means the model answered again, so that error did not end the run."""
    for source in tails:
        failure: ProviderFailure | None = None
        turn_failed = False
        for line in tail_lines(source):
            event = base.json_object(line.strip())
            if event is None:
                continue
            kind, found, failed = _event_failure(event)
            if kind in _TURN_ENDS:
                if turn_failed or not failed:
                    break
                turn_failed = True
                failure = found
                if interruption_from(found) is not None:
                    break
                continue
            if kind == "error" and found is not None:
                if interruption_from(found) is not None:
                    failure = found
                    break
                continue
            if kind.startswith("item"):
                break
        if failure is not None or turn_failed:
            return failure, turn_failed
    return None, False


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _metrics(transcript: Path) -> tuple[ReportMetrics, int]:
    """`turn.completed` events carry `usage`; any event naming a `model` names it."""
    model: str | None = None
    tokens_in = 0
    tokens_out = 0
    seen_usage = False
    count = 0
    for event in base.json_lines(transcript):
        count += 1
        if isinstance(event.get("model"), str):
            model = str(event["model"])
        if event.get("type") == "turn.completed":
            i, o = base.usage_totals(event.get("usage"))
            if i is not None or o is not None:
                seen_usage = True
                tokens_in += i or 0
                tokens_out += o or 0
    source = "harness_transcript" if count else "none"
    return (
        ReportMetrics(
            model,
            tokens_in if seen_usage else None,
            tokens_out if seen_usage else None,
            None,
            source,
        ),
        count,
    )
