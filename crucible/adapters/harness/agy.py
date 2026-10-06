"""The AGY adapter (07, S1, S3, S5, S6).

Launch: `agy -p "<pointer>" --model <model> --effort <effort>
--dangerously-skip-permissions --add-dir <identity> --output-format stream-json
--print-timeout <timeout>`. The prompt is a pointer under 1 KB (the per-argument ceiling
is the kernel's 128 KiB, S3) and the bundle travels by `--add-dir`. `--print-timeout`
defaults to five minutes in the CLI, so it is set to the attempt's own timeout.

Credential: the token file under `.gemini/antigravity-cli/`, mounted at the CLI's
config directory. 12 started it at `ro` pending evidence: AGY refreshes in memory once
the access token is past its one-hour expiry, and S1 saw the read-only mount refuse the
save. The C5 live run supplied the evidence: the first Crucible-side run after the
expiry renewed the access token and the copy carried a newer `token.expiry`, so the
minimum is rw-narrow and the file syncs back by that field. Google does not rotate
the refresh token on ordinary renewal, so isolated copies may run concurrently.

Commands (issue 128): the pinned 1.2.8 CLI has no launch-level command timeout and no
switch for backgrounding. Its `run_command` tool takes `Blocking` and
`WaitMsBeforeAsync` from the model, per call, and a command still running after that
wait continues in the background ("Background command is still running after %ds",
from the binary's strings, 2026-09-25); neither `agy --help` nor the public CLI docs
name a setting that changes it. `--print-timeout` bounds the whole turn and is the
attempt's own timeout. The one mitigation is in the prompt: run commands blocking, for
up to the launch's command timeout, and never end the turn with one running. What print
mode does with a background command at exit was not observed: AGY cannot run without a
Google login, so it was not reproduced. Its transcript format for background commands
is unknown, so this adapter has no in-flight evidence to read and its attempts are
classified by exit code and report alone. For the same reason it has no live command
tracker (issue 152): a silent AGY command counts against the stall limits.
"""

from __future__ import annotations

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
from crucible.domain.exit_class import ExitClass
from crucible.domain.harness_concurrency import HARNESS_CONCURRENCY
from crucible.domain.infrastructure import Interruption
from crucible.ports.harness import (
    AGY_BINARY,
    AdapterLaunch,
    AuthFile,
    CommandTracker,
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

NAME = "agy"
# Issue 128: AGY takes blocking and backgrounding from the model per command, and the
# launch has no setting for either, so the prompt says it. A mitigation, not a guard:
# nothing enforces it and AGY's exit with a command running is not detected.
BLOCKING_NOTE = (
    "Run every shell command blocking and wait for it to finish, up to {minutes} minutes; "
    "never end your turn while a command you started is still running."
)
CONFIG_DIR = "/home/worker/.gemini"
TOKEN_FILE = "antigravity-cli/antigravity-oauth-token"

AUTH_PATTERNS = base.patterns(
    "authentication failed or timed out",
    "authentication required",
    "Please sign in",
    "not logged in",
    "invalid_grant",
    "UNAUTHENTICATED",
    "Run 'agy' to log in",
)
# Hades #378: the account's own quota, as the CLI words it on the `result` line of a run
# it ended for it ("Individual quota reached ... Resets in 3h52m", observed 2026-10-05):
# no RPC status name, no HTTP status, and the reset as a duration rather than a time.
QUOTA_REACHED = "Individual quota reached"
QUOTA_PATTERNS = base.patterns(
    "RESOURCE_EXHAUSTED",
    QUOTA_REACHED,
    "quota exceeded",
    "Quota exceeded",
    "rate limit exceeded",
    "Too Many Requests",
)


# The Google RPC status a Cloud Code error carries, as the HTTP status it maps to.
GOOGLE_STATUSES = {"UNAVAILABLE": 503, "DEADLINE_EXCEEDED": 504, "RESOURCE_EXHAUSTED": 429}
# Cloud Code's reason when the model, not the account, is out of capacity: a 429
# RESOURCE_EXHAUSTED that is not the account's quota.
CAPACITY_REASON = "MODEL_CAPACITY_EXHAUSTED"
_LEADING_STATUS = re.compile(r"^\s*([A-Z][A-Z_]{3,})\b")


def _result_body(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The body of a `result` line in either shape the CLI has written: 1.2.4's
    `{"event": "result", "result": {...}}` (found live) or S3's `{"type": "result", ...}`;
    None for any other line."""
    kind = str(event.get("event") or event.get("type") or "")
    if kind != "result":
        return None
    nested = event.get(kind)
    return nested if isinstance(nested, dict) else event


def _provider_quota_refusal(document: Mapping[str, Any]) -> bool:
    """The authoritative refusal that marks the pool (05b): the final `result` line of a
    run that ended `ERROR` because the account's quota refused the call, whether the
    error is the RPC's RESOURCE_EXHAUSTED, a structured 429, or the CLI's own
    "Individual quota reached" sentence (hades #378). MODEL_CAPACITY_EXHAUSTED is the
    model's capacity, not the account's quota, and never marks."""
    body = _result_body(document)
    return body is not None and body.get("status") == "ERROR" and _result_failure(body).quota


def _result_failure(body: Mapping[str, Any]) -> ProviderFailure:
    """The `result` line of a run that ended `ERROR`. Its `error` is the Cloud Code error,
    either the API's JSON (`{"code": 503, "status": "UNAVAILABLE", "message": ...}`) or the
    CLI's text of it, which starts with the RPC status name."""
    error = body.get("error")
    status: int | None = None
    if isinstance(error, dict):
        code = error.get("code")
        if isinstance(code, int) and not isinstance(code, bool):
            status = code
        else:
            status = GOOGLE_STATUSES.get(str(error.get("status") or ""))
        text = str(error.get("message") or "")
        raw = str(error)
    else:
        text = raw = str(error or "")
        leading = _LEADING_STATUS.match(text)
        status = GOOGLE_STATUSES.get(leading.group(1)) if leading else None
    status = status or status_line(text)
    capacity = CAPACITY_REASON in raw
    quota = status == 429 or "RESOURCE_EXHAUSTED" in raw or QUOTA_REACHED.lower() in raw.lower()
    return ProviderFailure(
        raw[:2000],
        status=None if capacity else status,
        capacity=capacity,
        quota=quota and not capacity,
    )


def _last_result_failure(*tails: str) -> ProviderFailure | None:
    """The final `result` line decides: one that is not `ERROR` ended a run whose model
    calls worked, whatever was retried before it."""
    for line in tail_lines(*tails):
        event = base.json_object(line.strip())
        body = _result_body(event) if event is not None else None
        if body is None:
            continue
        return _result_failure(body) if body.get("status") == "ERROR" else None
    return None


class AgyAdapter:
    name = NAME
    concurrency = HARNESS_CONCURRENCY[NAME]
    parallel_attempts_safe = concurrency.parallel_attempts_safe
    supported_versions = VersionRange("1.2.0", "1.3.0")

    def quota_reset_at(self, stdout_tail: str, stderr_tail: str) -> datetime | None:
        return base.quota_reset_at(stdout_tail, stderr_tail, quota=QUOTA_PATTERNS)

    def provider_quota_event(
        self, stdout_tail: str, stderr_tail: str, now: datetime | None = None
    ) -> ProviderQuotaEvent | None:
        # The reset is "Resets in 3h52m" in the error text (hades #378), counted from
        # `now`; a result line that names none leaves the pool's default cooldown.
        return base.provider_quota_event(
            stdout_tail, stderr_tail, predicate=_provider_quota_refusal, now=now
        )

    def provider_quota_exhausted(self, stdout_tail: str, stderr_tail: str) -> bool:
        return self.provider_quota_event(stdout_tail, stderr_tail) is not None

    def command_tracker(self) -> CommandTracker | None:
        # Issue 152: no live evidence of a running command; the stall clock runs as ever.
        return None

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            prompt_on_stdin=False,
            model_flag=True,
            effort_flag=True,
            transcript_format=TranscriptFormat.STREAM_JSON,
            # S6 named the model API and the OAuth refresh grant. The C5 live runs added
            # two more: the CLI's eligibility check calls the userinfo endpoint on
            # www.googleapis.com and then fetches the account's profile picture from
            # lh3.googleusercontent.com before any turn, and fails closed when either
            # is refused.
            endpoints=(
                "daily-cloudcode-pa.googleapis.com",
                "lh3.googleusercontent.com",
                "oauth2.googleapis.com",
                "www.googleapis.com",
            ),
            shim="AGENTS.md",
            # The Google OAuth code exchange (oauth2.googleapis.com/token) and the
            # userinfo call (www.googleapis.com/oauth2/v2/userinfo), from the pinned
            # 1.2.8 binary's strings on 2026-09-24. daily-cloudcode-pa is the model API,
            # so the prompt AGY's login command ends with cannot reach it from a login
            # Job; the token is judged by its shape and then by the probe.
            login_endpoints=(
                "lh3.googleusercontent.com",  # profile picture fetch after sign-in; hades #241
                "oauth2.googleapis.com",
                "www.googleapis.com",
            ),
        )

    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=NAME,
            mount_target=CONFIG_DIR,
            source_subdir=".gemini",
            auth_files=(
                AuthFile(
                    TOKEN_FILE, json=True, json_keys=("token",), issued_at=("token", "expiry")
                ),
            ),
            # rw-narrow since the C5 live run: the first Crucible-side run after the
            # one-hour expiry refreshed the token and the copy carried a newer expiry,
            # which the sync-back wrote to the source (12: "would make it rw-narrow").
            minimum_mode=MountMode(self.concurrency.minimum_mode),
            config_dir_env=None,
            templates={},
            login_hint=(
                "HOME=<dir> agy, then paste the code within 60 seconds; have the browser "
                "signed in first"
            ),
        )

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        note = BLOCKING_NOTE.format(minutes=max(1, -(-ctx.command_timeout // 60_000)))
        argv: list[str] = [
            AGY_BINARY,
            "-p",
            f"{base.POINTER_PROMPT} {note}",
            "--model",
            ctx.model,
        ]
        if ctx.effort:
            argv += ["--effort", ctx.effort]
        argv += [
            "--dangerously-skip-permissions",
            "--add-dir",
            ctx.identity_mount,
            "--output-format",
            "stream-json",
            "--print-timeout",
            f"{max(int(ctx.timeout_seconds), 1)}s",
        ]
        return AdapterLaunch(
            argv=tuple(argv),
            transcript_path=f"{ctx.report_mount}/{base.TRANSCRIPT_NAME}",
            workdir=ctx.repo_mount,
        )

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport:
        metrics, lines = _metrics(report_dir / base.TRANSCRIPT_NAME)
        return base.parse_report_dir(report_dir, exit, metrics=metrics, transcript_lines=lines)

    def classify_exit(
        self, exit: ExitInfo, stdout_tail: str, stderr_tail: str, report_dir: Path | None = None
    ) -> ExitClass:
        # S5: AGY exits 1 for an auth failure and still emits a well-formed `result`
        # line; the class comes from its text, not from the line's presence.
        interruption = self.interruption(exit, stdout_tail, stderr_tail, report_dir)
        if interruption is not None and interruption.capacity:
            # MODEL_CAPACITY_EXHAUSTED says RESOURCE_EXHAUSTED too; it is not quota.
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
        if (
            exit.exit_code in (None, 0)
            or exit.lost
            or exit.timed_out
            or exit.killed
            or exit.oom_killed
        ):
            return None
        return interruption_from(_last_result_failure(stdout_tail, stderr_tail))


def _usage(event: dict[str, Any]) -> tuple[int | None, int | None]:
    for key in ("usage", "token_usage", "tokens", "usageMetadata"):
        tokens_in, tokens_out = base.usage_totals(event.get(key))
        if tokens_in is not None or tokens_out is not None:
            return tokens_in, tokens_out
    tokens_in = event.get("input_tokens")
    tokens_out = event.get("output_tokens")
    if isinstance(tokens_in, int) or isinstance(tokens_out, int):
        return (
            tokens_in if isinstance(tokens_in, int) else None,
            tokens_out if isinstance(tokens_out, int) else None,
        )
    return None, None


def _metrics(transcript: Path) -> tuple[ReportMetrics, int]:
    """The final `result` line carries the token usage (S3); `init` names the model.

    The 1.2.4 stream keys each line as `{"event": "result", "result": {...}}` (found
    live); S3's description read it as `type`. Both shapes are accepted."""
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    count = 0
    for event in base.json_lines(transcript):
        count += 1
        kind = str(event.get("event") or event.get("type") or "")
        nested = event.get(kind)
        body: dict[str, Any] = nested if isinstance(nested, dict) else event
        if isinstance(body.get("model"), str):
            model = str(body["model"])
        if kind == "result":
            tokens_in, tokens_out = _usage(body)
    source = "harness_transcript" if count else "none"
    return ReportMetrics(model, tokens_in, tokens_out, None, source), count
