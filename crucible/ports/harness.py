"""HarnessAdapter contract (07): launch shape, credential needs, report parsing, and exit
classification for one harness. Adapters contain no lifecycle logic.

The port carries no secret. A credential is named here (which files, where they mount,
how they refresh); its value is read by the provider from a configured path and reaches
the worker only as a per-attempt copy the provider seeds (12).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from crucible.domain.command_timeout import DEFAULT_COMMAND_TIMEOUT_MS
from crucible.domain.endpoints import validate_endpoint
from crucible.domain.exit_class import ExitClass
from crucible.domain.infrastructure import Interruption

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

# Where each harness's CLI lives inside the worker image (images/worker/Dockerfile).
# One image carries all four since C11, so a launch names its binary by path rather
# than by whatever a PATH lookup inside a shared image would find.
CLAUDE_CODE_BINARY = "/usr/local/bin/claude"
CODEX_BINARY = "/usr/local/bin/codex"
AGY_BINARY = "/usr/local/bin/agy"
# The Hermes launch wrapper. It starts Hermes with its virtual environment's Python by
# path and puts nothing on PATH, so no command any model runs, Hermes's included,
# resolves `python3` into that environment (FDY-0140).
HERMES_BINARY = "/usr/local/bin/crucible-hermes"

__all__ = [
    "AGY_BINARY",
    "CLAUDE_CODE_BINARY",
    "CODEX_BINARY",
    "HERMES_BINARY",
    "AdapterLaunch",
    "AuthFile",
    "CredentialSource",
    "CredentialSpec",
    "ExitClass",
    "ExitInfo",
    "HarnessAdapter",
    "HarnessCapabilities",
    "HarnessGate",
    "HarnessUnavailableError",
    "LaunchContext",
    "MountMode",
    "ParsedReport",
    "ProviderQuotaEvent",
    "ReportMetrics",
    "SessionCompatibility",
    "TranscriptFormat",
    "VersionRange",
    "parse_version",
]


class MountMode(StrEnum):
    """How the per-attempt credential copy is mounted (12)."""

    RO = "ro"
    RW_NARROW = "rw-narrow"
    RENEWER = "renewer"


class SessionCompatibility(StrEnum):
    """Whether the dedicated Crucible session has been shown to leave the operator's own
    session valid (25, S1b)."""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"


class TranscriptFormat(StrEnum):
    STREAM_JSON = "stream-json"
    JSON_EVENTS = "json-events"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ProviderQuotaEvent:
    """One authoritative structured provider refusal emitted by a harness.

    `model_only_refusal` is True when the refusal applies only to the model that
    was tried (e.g. `model_requires_usage_credits` or a model-switch suggestion);
    the supervisor then excludes that model and reroutes to the next candidate
    in the same pool without marking the whole pool (hades #373).  When False
    the signal is account-level (e.g. `out_of_credits` on the account) and the
    pool must be marked exhausted (the existing path).
    """

    reset_at: datetime | None = None
    model_only_refusal: bool = False


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION.match(value.strip())
    if match is None:
        raise ValueError(f"not a version: {value!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


@dataclass(frozen=True, slots=True)
class VersionRange:
    """The harness versions an adapter was tested with: inclusive lower bound, exclusive
    upper bound. A launch outside it is refused, never warned about (07, 13)."""

    min_version: str
    max_version_exclusive: str

    def supports(self, version: str) -> bool:
        try:
            found = parse_version(version)
        except ValueError:
            return False
        return parse_version(self.min_version) <= found < parse_version(self.max_version_exclusive)

    @property
    def text(self) -> str:
        return f">={self.min_version},<{self.max_version_exclusive}"


@dataclass(frozen=True, slots=True)
class HarnessCapabilities:
    """What the provider and the routing policy may rely on for this harness."""

    prompt_on_stdin: bool
    model_flag: bool
    effort_flag: bool
    transcript_format: TranscriptFormat
    # The model and auth hostnames this harness must reach (S6). The worker's allowlist
    # is the union of these and the policy's egress_allowlist (13).
    endpoints: tuple[str, ...]
    # The instruction file the harness reads from the checkout; the preparer writes an
    # untracked one only when the checkout has none (06, 07).
    shim: str | None = None
    # Whether a project CLAUDE.md takes precedence over the generated AGENTS.md shim.
    claude_md_wins: bool = False
    # The hostnames the harness's own login CLI reaches to finish a sign-in (25, 26),
    # and nothing else. The Kubernetes login Job gets these and not `endpoints`, so a
    # login can never reach a model API (crucible#58). Empty for a harness with no
    # interactive login.
    login_endpoints: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_on_stdin": self.prompt_on_stdin,
            "model_flag": self.model_flag,
            "effort_flag": self.effort_flag,
            "transcript_format": self.transcript_format.value,
            "endpoints": list(self.endpoints),
            "shim": self.shim,
            "claude_md_wins": self.claude_md_wins,
            "login_endpoints": list(self.login_endpoints),
        }


@dataclass(frozen=True, slots=True)
class AuthFile:
    """One file that is the credential, named relative to the mounted directory.

    `json` says the file is a JSON object and `json_keys` are the top-level keys a
    valid one carries; an opaque file is never parsed. `issued_at` is the path to the
    field that orders two versions of the file (a refresh timestamp or a token expiry);
    a file without one is never synced back, because "newest" would have no meaning
    (12). `env_var` is the one exception 07 allows to file-only delivery: the file's
    content reaches the harness through that variable at container start, resolved by
    the provider from the mounted copy, never placed in the create request."""

    name: str
    # Whether the file is a JSON object (validated on sync-back) or opaque bytes.
    json: bool = False
    json_keys: tuple[str, ...] = ()
    issued_at: tuple[str, ...] | None = None
    required: bool = True
    env_var: str | None = None
    # A file the CLI rewrites as state rather than as a credential: it is seeded so the
    # CLI finds what it expects, and never written back (12).
    sync_back: bool = True


@dataclass(frozen=True, slots=True)
class CredentialSpec:
    """Where a harness expects its credential and which files that is (07, 12)."""

    harness: str
    # The container path the seeded copy is mounted at.
    mount_target: str
    auth_files: tuple[AuthFile, ...]
    # The adapter's declared minimum (07). A probe may raise it to rw-narrow; it is
    # never lowered (25 step 7).
    minimum_mode: MountMode
    # Optional credentials keep the harness's documented unauthenticated fallback when
    # no source is configured. If a source exists, providers still mount and validate it.
    required_for_launch: bool = True
    # Which subdirectory of the configured credential path maps onto `mount_target`.
    source_subdir: str = ""
    # The environment variable that points the CLI at `mount_target`, if it has one.
    config_dir_env: str | None = None
    # Files mounted read-only on top of the copy from a Crucible-owned template, so the
    # CLI's settings, hooks and server definitions never come from a worker (12).
    templates: Mapping[str, str] = field(default_factory=dict)
    # How the operator logs in to this directory (25 step 2), for the onboarding command.
    login_hint: str = ""

    def env(self) -> dict[str, str]:
        return {self.config_dir_env: self.mount_target} if self.config_dir_env else {}

    def env_from_files(self) -> dict[str, str]:
        return {f.env_var: f"{self.mount_target}/{f.name}" for f in self.auth_files if f.env_var}

    def source_path(self, root: str, name: str) -> Path:
        base = Path(root)
        if self.source_subdir:
            base = base / self.source_subdir
        return base / name

    def held_by(self, root: str) -> bool:
        """Whether a configured source counts as mounted. A required credential is
        mounted once configured, and its seeding refuses a missing file. An optional one
        is mounted only when its required auth files exist: Compose creates its directory
        empty, and an empty directory must keep the unauthenticated fallback."""
        if self.required_for_launch:
            return True
        return all(self.source_path(root, a.name).is_file() for a in self.auth_files if a.required)


@dataclass(frozen=True, slots=True)
class HarnessGate:
    """The operator's configuration default for one harness (25, ADR 0021): a harness
    ships off until its dedicated credential session and daily-session compatibility
    are verified (S1b), and the reason travels with the flag. An administrator's stored
    decision replaces it (hades #174)."""

    enabled: bool = True
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CredentialSource:
    """A configured credential directory for one harness (12): a path and the mount
    mode the operator chose, or None to take the adapter's minimum."""

    path: str
    mount_mode: MountMode | None = None


@dataclass(frozen=True, slots=True)
class LaunchContext:
    """What an adapter needs to build a launch (07). Paths are container paths."""

    attempt_id: str
    model: str
    effort: str | None
    timeout_seconds: int
    identity_mount: str
    report_mount: str
    repo_mount: str
    credential_mounted: bool = False
    credential_mode: MountMode | None = None
    endpoint: Literal["subscription", "local"] = "subscription"
    endpoint_url: str | None = None
    # A bounded probe or harness test (25, crucible#118): one prompt, no task, no report.
    probe: bool = False
    # Issue 128: the per-command timeout, from the launch spec. None: the default.
    command_timeout_ms: int | None = None
    # FDY-0140: the harness's own run settings an administrator saved (the Hermes run
    # limits on the Local gateway page), from the launch spec. Empty: the defaults.
    harness_settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_endpoint(self.endpoint, self.endpoint_url)

    @property
    def command_timeout(self) -> int:
        """The per-command timeout in milliseconds, never above the attempt's timeout."""
        requested = self.command_timeout_ms or DEFAULT_COMMAND_TIMEOUT_MS
        return max(1, min(requested, max(int(self.timeout_seconds), 1) * 1000))


@dataclass(frozen=True, slots=True)
class AdapterLaunch:
    """The harness-specific half of a LaunchSpec (07).

    `env` carries names and non-secret values only. `env_from_files` maps a variable to a
    container path the provider resolves at container start; it is the only way a value
    from a credential file reaches the harness's environment, and it never appears in the
    create request. The prompt is a short pointer (S3); the identity and the contract
    are files."""

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    env_from_files: Mapping[str, str] = field(default_factory=dict)
    # What the harness reads on stdin: these files, in order, then this text.
    stdin_files: tuple[str, ...] = ()
    stdin_text: str = ""
    # Where the provider tees the harness's stdout so the transcript becomes an artifact.
    transcript_path: str | None = None
    workdir: str | None = None


@dataclass(frozen=True, slots=True)
class ExitInfo:
    exit_code: int | None
    report_present: bool = False
    blocked_present: bool = False
    oom_killed: bool = False
    timed_out: bool = False
    killed: bool = False
    lost: bool = False


@dataclass(frozen=True, slots=True)
class ReportMetrics:
    """What the transcript said about cost (05b): tokens and the model that answered.
    Null where a harness reports nothing; the pool then counts attempts."""

    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    source: str = "none"
    duration_ms: int | None = None
    tool_calls: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "tool_calls": self.tool_calls,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ParsedReport:
    """The report directory as an adapter read it (07). The claim is still a claim."""

    claim: dict[str, Any] | None
    raw: str | None
    errors: list[dict[str, Any]]
    blocked_md: str | None
    report_present: bool
    # Progress lines the worker wrote, ingested as unverified events (07).
    progress: tuple[dict[str, Any], ...] = ()
    metrics: ReportMetrics = field(default_factory=ReportMetrics)
    transcript_lines: int = 0
    transcript_name: str | None = None
    run_evidence_error: str | None = None
    # FDY-0140: the harness ended the run because it reached one of its own run limits
    # (Hermes's turn budget), in words. None when it did not, or cannot say.
    limit_reached: str | None = None
    # Issue 128: a command the harness's own transcript shows it was waiting on and cut
    # off at exit (Claude Code's auto-background). A background process the worker chose
    # to leave running is not listed (issue 153). Non-empty makes a clean exit
    # `incomplete`.
    in_flight: tuple[str, ...] = ()


class HarnessUnavailableError(Exception):
    """The registry refused a harness name: unknown, or disabled (07, 25)."""

    def __init__(self, harness: str, reason: str) -> None:
        super().__init__(f"harness {harness!r} is unavailable: {reason}")
        self.harness = harness
        self.reason = reason


class CommandTracker(Protocol):
    """Issue 152: whether the harness has a command running, read from its live log.

    The supervisor feeds every stored log chunk in order, from the attempt's first; a
    tracker keeps its own partial lines. While `running` is non-empty the stall clock
    does not advance (05b, 10). Each entry is `(key, summary)`: `key` is the harness's
    own unique id for the command (a Claude `tool_use` id, a Codex item id), so a repeat
    of the same command, or two different commands whose summaries truncate to the same
    text, never share an age; `summary` is display-only."""

    def feed(self, stream: str, text: str) -> None: ...

    @property
    def running(self) -> tuple[tuple[str, str], ...]: ...


@runtime_checkable
class CommandLoopTracker(CommandTracker, Protocol):
    """Issue 278: what a CommandTracker can also say about a degenerate run, read from
    the same live log. A tracker without it is held to the time-based stall limits only.

    `repeated` is the last command started and how many times in a row it has started,
    with no other command and no file edit between; `responding` is whether the
    model is working on the harness's turn (so neither the preparer's time nor the
    harness's own start is counted); `tool_called` is whether the model has made any
    tool call yet. `workspace_changed` is the supervisor telling the tracker it saw the
    worker's files move (its fingerprint or its activity probe), which ends the run of
    repeats as an edit in the log does: a command that edits through the shell shows
    the log no edit, and repeating it is iteration, not a loop."""

    @property
    def repeated(self) -> tuple[str, int] | None: ...

    def workspace_changed(self) -> None: ...

    @property
    def responding(self) -> bool: ...

    @property
    def tool_called(self) -> bool: ...


class HarnessAdapter(Protocol):
    name: str
    supported_versions: VersionRange

    def capabilities(self) -> HarnessCapabilities: ...

    def credential_spec(self) -> CredentialSpec | None: ...

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch: ...

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport: ...

    def classify_exit(
        self,
        exit: ExitInfo,
        stdout_tail: str,
        stderr_tail: str,
        report_dir: Path | None = None,
    ) -> ExitClass: ...

    def interruption(
        self,
        exit: ExitInfo,
        stdout_tail: str,
        stderr_tail: str,
        report_dir: Path | None = None,
    ) -> Interruption | None:
        """Hades #353: the model call that ended the run failed for a reason outside the
        worker (a gateway 5xx, a refused connection, capacity, quota), read from this
        harness's own error events; None for any other ending."""
        ...

    def quota_reset_at(self, stdout_tail: str, stderr_tail: str) -> datetime | None: ...

    def provider_quota_event(
        self, stdout_tail: str, stderr_tail: str, now: datetime | None = None
    ) -> ProviderQuotaEvent | None:
        """The harness's authoritative provider-refusal event with its reset, or None.
        `now` is when the refusal was observed, for a reset the event states as a
        duration (hades #378); it defaults to the wall clock."""
        ...

    def provider_quota_exhausted(self, stdout_tail: str, stderr_tail: str) -> bool:
        """True only for the harness's authoritative provider-refusal event."""
        ...

    def is_model_refusal(self, stdout_tail: str, stderr_tail: str) -> bool:
        """FDY-0514: True when the exit was quota-exhausted but only the model is at
        fault.  Subclasses override; the default is False (account-level)."""
        return False

    def command_tracker(self) -> CommandTracker | None:
        """A fresh tracker for one attempt, or None where the harness gives no live
        evidence of a running command (AGY): its stall clock runs as for any worker."""
        ...
