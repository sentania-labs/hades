"""The supervision tick (10). One active supervisor, enforced by the supervisor lease
and its fenced token; every write the supervisor makes carries that token via
SET LOCAL and the database rejects a stale one.

Tick order: lease, provider reconcile (orphans and adoption), materialize scheduled
tasks, launch pending attempts, observe running attempts (timeouts, exits, loss),
sweep cancellations, write the liveness row. A launch's slow half (build the spec,
prepare the checkout, start the worker) runs as a task of its own that the tick starts
and polls, so a prepare that takes minutes never holds back the lease or any other
attempt (hades #190). Running the tick twice with nothing
happening in between changes nothing the second time except lease expiry times and
the liveness row; that property is tested.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import stat
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, Literal, TypeVar

import yaml

from crucible.application.admin import credentials as admin_credentials
from crucible.application.admin import gateway as admin_gateway
from crucible.application.admin import routing as admin_routing
from crucible.application.admin import status_cache as admin_status_cache
from crucible.application.admin.context import AdminContext
from crucible.application.admin.harnesses import refresh_images
from crucible.application.admin.providers import refresh_providers_status
from crucible.application.checkout import (
    CheckoutRefusedError,
    checkout_token_for,
    release_checkout_token,
)
from crucible.application.decisions import (
    DEFAULT_ESCALATION_STALE_HOURS,
    open_escalation,
    repeat_stale_escalation_wakes,
)
from crucible.application.delivery_tick import DeliveryConfig, DeliveryCoordinator
from crucible.application.errors import ApplicationError, NotFoundError
from crucible.application.evidence import claim_facts, record_collection_evidence, store_artifact
from crucible.application.gates import evaluate_and_advance, gate_input
from crucible.application.harnesses import (
    CREDENTIAL_HOLDING_STATES,
    HarnessRegistry,
    effective_mount_mode,
    ingest_progress,
    record_credential_observation,
    record_launch_outcome,
)
from crucible.application.review import (
    author_attempt_ids,
    latest_work_attempt,
    record_review_report,
    review_evidence_payload,
)
from crucible.application.routing import (
    count_blocking_failures,
    current_routing_version,
    load_attempt_routing,
    reserve,
    select_model,
)
from crucible.application.runtime_settings import resolve as resolve_runtime_setting
from crucible.application.transitions import (
    move_attempt,
    move_execution,
    move_task,
    queue_key,
    record_event,
    record_rejected_transition,
)
from crucible.application.wakes import (
    create_pool_exhausted_wake,
    create_wake,
    pool_exhausted_summary,
    record_delivery,
    retry_hours_from_policy,
    wake_body,
)
from crucible.contracts.completion_claim import (
    CompletedClaim,
    complete_claim,
    load_report,
    parse_blocked_md,
    parse_claim,
)
from crucible.contracts.evidence import ROLE_RUN_EVIDENCE, EvidenceKind, EvidenceSource
from crucible.contracts.policy import RoutingPolicyV1, routing_model_name, window_seconds
from crucible.contracts.task_contract import TaskContractV1
from crucible.contracts.wake import WakeReason
from crucible.domain.command_timeout import effective_command_timeout_ms
from crucible.domain.egress_probe import (
    PROBE_MARKER,
    find_probe_line,
    probe_expected,
    rejected_record,
    unreachable_hosts,
)
from crucible.domain.entities import (
    Attempt,
    AttemptMetrics,
    CompletionClaimRecord,
    DispositionKind,
    EvidenceRecord,
    Execution,
    ExecutionRole,
    Heartbeat,
    LogChunkRecord,
    PoolExhaustion,
    Principal,
    PullRequestState,
    Repository,
    RetentionAction,
    ReviewDisposition,
    Role,
    Task,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.exit_class import (
    CLEAN_EXIT_CLASSES,
    STALL_NO_ACTIVITY,
    ExitClass,
    classify_exit,
    loop_shape,
)
from crucible.domain.gates import GateName, GateResult, evaluate_gate
from crucible.domain.harness_settings import (
    DEFAULT_QWEN_CONTEXT_LENGTH,
    effective_settings,
    setting_name,
)
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import (
    ATTEMPT_TERMINAL,
    EXECUTION_TERMINAL,
    TASK_TERMINAL,
    AttemptState,
    ExecutionState,
    IllegalTransitionError,
    TaskState,
)
from crucible.domain.secrets import find_secrets, redact
from crucible.domain.verification import task_specific_checks
from crucible.logs import log_context
from crucible.ports.artifacts import ArtifactStore
from crucible.ports.clock import Clock
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    CancelCheck,
    CleanupPolicy,
    CollectedOutputs,
    CollectionPendingError,
    ExecutionProvider,
    Handle,
    LaunchCancelledError,
    LaunchRefusedError,
    LaunchSpec,
    LaunchWaitError,
    LogChunk,
    LogOffset,
    Observation,
    ObservationState,
    ProviderError,
    ProviderUnavailableError,
    VerificationRun,
    WorkerStartError,
    Workspace,
)
from crucible.ports.github import GitHubClient
from crucible.ports.harness import (
    CommandLoopTracker,
    CommandTracker,
    CredentialSource,
    ExitInfo,
    HarnessGate,
    HarnessUnavailableError,
    LaunchContext,
    MountMode,
    ParsedReport,
)
from crucible.ports.notification import WakeDeliverer
from crucible.ports.publish import Publisher
from crucible.ports.repository import FencedTokenRejectedError, UnitOfWork, UnitOfWorkFactory

log = logging.getLogger("crucible.supervisor")

# How many seconds the workspace fingerprint walk may spend before giving up.
# Mirrors ACTIVITY_WALK_SECONDS in scripts.py so the supervisor and the worker
# use the same budget for their directory walks.
ACTIVITY_WALK_SECONDS: int = 10

T = TypeVar("T")

TERMINATION_TIMEOUT = "timeout"
TERMINATION_STALL = "stall"
TERMINATION_CANCEL = "cancel"
# The task states in which a live attempt is ended as a cancel ends it. A merge observed
# while a correction runs is one (hades #360): the PR is merged, so the corrected head has
# nowhere to go, and the task stays `merged` rather than becoming `cancelled`.
ENDS_ATTEMPTS: tuple[TaskState, ...] = (
    TaskState.CANCELLING,
    TaskState.CANCELLED,
    TaskState.MERGED,
)
# A launch the registry or the provider refused (07): recorded so the retry rule knows
# not to try the same refusal again.
TERMINATION_REFUSED = "harness_refused"
# hades #423: the attempt states that hold one of a provider's worker slots: from the
# gate probe through collection, every Pod an attempt runs is of the worker's shape and
# runs inside the slot the attempt took, so the provider's reservation for short-role
# Pods only has to cover what runs beside attempts (the canary, a login, a publish).
SLOT_HOLDING_STATES: tuple[AttemptState, ...] = (
    AttemptState.PREPARING,
    AttemptState.LAUNCHING,
    AttemptState.RUNNING,
    AttemptState.TERMINATING,
    AttemptState.EXITED,
)


def local_cap_kind(
    endpoint: str | None, exit_class: ExitClass, turn_cap_reached: bool
) -> Literal["turns", "time"] | None:
    """Name a size cap only when the attempt was routed to a local endpoint."""
    if endpoint != "local" or exit_class in {ExitClass.INFRASTRUCTURE, ExitClass.QUOTA_EXHAUSTED}:
        return None
    if turn_cap_reached:
        return "turns"
    if exit_class is ExitClass.TIMEOUT:
        return "time"
    return None


def retryable_exit(exit_class: ExitClass, retry_on: Sequence[str]) -> bool:
    return exit_class.value in retry_on and exit_class in (
        ExitClass.ENVIRONMENT,
        ExitClass.LOST,
        ExitClass.AUTH_FAILURE,
        ExitClass.TIMEOUT,
    )


def too_big_wake_summary(cap: Literal["turns", "time"]) -> str:
    cap_name = "turn" if cap == "turns" else cap
    return f"split the task: the local attempt hit its {cap_name} cap"


# The escalation question when `blocked.md` was there with nothing in it but a reason line
# (or nothing at all): the worker stopped and said why in no words of its own.
BLOCKED_WITHOUT_STATEMENT = "the worker stopped without a statement"


def blocked_note(blocked_md: str | None) -> tuple[str | None, str | None]:
    """The reason and the statement of a collected `blocked.md` (hades #393).

    (None, None) when there was no file. The reason is the file's `reason:` line when
    it names missing_capability or ambiguous_contract, else None. The statement is the
    rest of the file verbatim, or the redaction marker when the file matches a secret
    pattern (12): the reason is one of two fixed words and is kept either way."""
    if blocked_md is None:
        return None, None
    note = parse_blocked_md(blocked_md)
    if find_secrets(blocked_md):
        return note.reason, "[redacted: secret pattern]"
    return note.reason, note.statement


# 16 defaults, used when the policy names none.
DEFAULT_LOG_RETENTION_DAYS = 90
DEFAULT_WORKSPACE_RETENTION_DAYS = 14
DEFAULT_WAKE_RETENTION_DAYS = 30
RETENTION_BATCH = 200
# A collection the provider could not finish because the cluster could not answer is
# tried again every interval until the window, counted from the first failure, runs out;
# then the attempt fails as environment, as any other failed collection does.
COLLECT_RETRY_INTERVAL_SECONDS = 30
COLLECT_RETRY_WINDOW_SECONDS = 1800
# The retention action a released workspace records, one per attempt (16).
RETENTION_WORKSPACE = "workspace"
# How many kept workspaces one tick releases. An upgrade that finds a backlog spreads the
# provider calls over a few ticks instead of holding one tick for all of them.
WORKSPACE_RELEASE_BATCH = 10
# How many pulls the final log drain makes before it stops (issue 63). A provider that
# bounds one pull at 4 MiB drains 1 GiB of backlog in this many; past that the log is
# still arriving faster than it is read, and the attempt moves on with what was stored.
FINAL_DRAIN_PULLS = 256
# Issue 152: while the harness reports a command in flight, a `command_running` activity
# signal is written whenever the newest activity is at least this old, or half the
# policy's shorter stall limit when that is less. The stall clock therefore resumes from
# within this window of the command's end.
COMMAND_RUNNING_REFRESH_SECONDS = 60
COMMAND_LOG_PAGE = 500
# A command still reported this long past its command timeout no longer counts: the
# command timeout, not the stall limit, bounds a command, and it bounds it here too.
COMMAND_OVERRUN_SECONDS = 60
# Issue 278: a worker that starts the same command this many times in a row, with no
# other command and no file edit between, is in a loop (`/bin/bash -lc wait`, an empty
# command) and is ended as a stall on the next tick, not at stall_fail_seconds. The
# accepted bound is 5 to 10. An edit is one the log shows or one the supervisor's own
# workspace check verifies, so a command that edits through the shell is iteration.
COMMAND_LOOP_REPEATS = 8
# Issue 278: a worker on a local endpoint whose turn has begun and that has made no tool
# call this long after is ended as a `no_activity` stall. Counted from the harness's own
# event that the model is working on the turn (Codex's `turn.started`), so the preparer,
# the image pull and the harness's own start are not in it.
LOCAL_FIRST_RESPONSE_SECONDS = 300
# How much of a repeated command a stall reason quotes.
LOOP_COMMAND_QUOTE = 200
# Hades #353: infrastructure interruptions retried per contract version before the task
# blocks for the endpoint.
INFRASTRUCTURE_RETRY_BUDGET = 3


def worker_stall_action(
    *,
    now: datetime,
    last_activity: datetime,
    last_signal: datetime | None = None,
    warn_seconds: int,
    fail_seconds: int,
    warned_at: datetime | None,
) -> str | None:
    """Derive warn from any signal and fail from substantive activity (10)."""
    if (now - last_activity).total_seconds() >= fail_seconds:
        return "fail"
    quiet_baseline = last_signal or last_activity
    quiet = (now - quiet_baseline).total_seconds()
    if quiet >= warn_seconds and (warned_at is None or warned_at < quiet_baseline):
        return "warn"
    return None


@dataclass(frozen=True, slots=True)
class EarlyStall:
    """Issue 278: a stall found before the time-based limit: its shape and its reason."""

    shape: str
    detail: str


def degenerate_stall(
    *,
    now: datetime,
    repeated: tuple[str, int] | None,
    tool_called: bool,
    responding_since: datetime | None,
    local: bool,
    repeat_limit: int = COMMAND_LOOP_REPEATS,
    first_response_seconds: int = LOCAL_FIRST_RESPONSE_SECONDS,
) -> EarlyStall | None:
    """Issue 278: whether what the harness's live log shows is a degenerate run. The same
    command started `repeat_limit` times in a row is a loop, on any route; a local-route
    turn with no tool call `first_response_seconds` after it began is `no_activity`."""
    if repeated is not None and repeated[1] >= repeat_limit:
        command, count = repeated
        quoted = json.dumps(command[:LOOP_COMMAND_QUOTE], ensure_ascii=False)
        return EarlyStall(
            loop_shape(command),
            f"the worker ran the same command {count} times in a row: {quoted}",
        )
    if (
        local
        and not tool_called
        and responding_since is not None
        and (now - responding_since).total_seconds() >= first_response_seconds
    ):
        return EarlyStall(
            STALL_NO_ACTIVITY,
            f"the local model made no tool call in the {first_response_seconds} seconds "
            "after its turn began",
        )
    return None


def command_refresh_seconds(warn_seconds: int, fail_seconds: int) -> int:
    """How often a command in flight renews activity: once a minute, or more often when
    a stall limit is short, so neither limit can pass between two renewals."""
    return max(1, min(COMMAND_RUNNING_REFRESH_SECONDS, warn_seconds // 2, fail_seconds // 2))


def command_activity_due(
    *, now: datetime, last_activity: datetime | None, refresh_seconds: int
) -> bool:
    """Issue 152: whether a command in flight writes a fresh activity signal now."""
    return last_activity is None or (now - last_activity).total_seconds() >= refresh_seconds


def commands_counted(
    running: Sequence[tuple[str, str]],
    first_seen: dict[str, datetime],
    *,
    now: datetime,
    command_timeout_seconds: float,
) -> tuple[str, ...]:
    """The commands in flight that still count as activity, updating when each was first
    seen. `running` is `(key, summary)`: `key` is the harness's own unique id for the
    command, so a repeat of the same command, or two different commands whose summaries
    truncate to the same text, never inherit each other's age. One reported past its
    command timeout (and a minute's margin) has outlived what the timeout allows it,
    whatever keeps the harness reporting it: a Hermes background process or a Codex
    session is not ended by the harness's own timeout."""
    keys = {key for key, _ in running}
    for gone in set(first_seen) - keys:
        del first_seen[gone]
    limit = command_timeout_seconds + COMMAND_OVERRUN_SECONDS
    counted = []
    for key, summary in running:
        seen = first_seen.setdefault(key, now)
        if (now - seen).total_seconds() < limit:
            counted.append(summary)
    return tuple(counted)


def workspace_fingerprint(workspace: Workspace) -> tuple[int, int, int] | None:
    """Cheap activity fingerprint for the writable checkout and report trees.

    Returns ``(newest_mtime_ns, file_count, total_bytes)`` on a tree small enough to
    walk within *ACTIVITY_WALK_SECONDS*.  Returns ``None`` when the walk would exceed
    the budget (the stall clock is never reset from ``None``).

    The ``.git`` subtree is skipped entirely: it dominates the node count on most
    Git worktrees and has no activity signal we care about.
    """
    newest_ns = files = total_bytes = 0
    budget_ns = int(ACTIVITY_WALK_SECONDS * 1_000_000_000)  # seconds -> nanoseconds
    start_ns = time.monotonic_ns()

    def _walk(root: Path) -> Iterator[Path]:
        """Yield every entry under *root*, pruning ``.git`` and symlinked dirs.

        Uses an explicit stack so deep nesting never raises ``RecursionError``,
        and checks ``lstat`` on every entry so directory symlinks (e.g. a
        worker-controlled symlink pointing at another attempt's workspace) are
        never descended.
        """
        stack: list[Path] = [root]
        yield root
        while stack:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            dirs: list[Path] = []
            for child in entries:
                if child.name == ".git":
                    # Skip the entire .git tree without descending.
                    continue
                yield child
                try:
                    child_stat = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(child_stat.st_mode):
                    dirs.append(child)
            stack.extend(dirs)

    for root_name in (workspace.checkout_path, workspace.report_path):
        root = Path(root_name)
        try:
            for path in _walk(root):
                # Budget check before every stat (cheap monotonic_ns call).
                if time.monotonic_ns() - start_ns > budget_ns:
                    log.debug(
                        "workspace_fingerprint exceeded budget; returning None",
                        extra={"budget_seconds": ACTIVITY_WALK_SECONDS},
                    )
                    return None
                try:
                    file_stat = path.stat(follow_symlinks=False)
                except OSError:
                    continue
                newest_ns = max(newest_ns, file_stat.st_mtime_ns)
                files += 1
                if stat.S_ISREG(file_stat.st_mode):
                    total_bytes += file_stat.st_size
        except OSError:
            continue
    return newest_ns, files, total_bytes


class LeaseLostError(Exception):
    """This supervisor no longer holds the lease; it must stop acting."""


@dataclass(slots=True)
class TickResult:
    held: bool
    launched: int = 0
    observed: int = 0
    finished: int = 0
    orphans: int = 0
    wakes_delivered: int = 0
    published: int = 0
    pull_requests_polled: int = 0
    duration_ms: int = 0
    counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _CommandWatch:
    """One attempt's live-log tracker (issue 152), the last log chunk it was fed, and
    what bounds the commands it reports."""

    tracker: CommandTracker | None
    refresh_seconds: int = COMMAND_RUNNING_REFRESH_SECONDS
    command_timeout_seconds: float = 0.0
    after_id: int = 0
    first_seen: dict[str, datetime] = field(default_factory=dict)
    # Issue 278: whether the attempt runs on a local endpoint, and the stored chunk time
    # at which the tracker first said the harness's turn had begun.
    local: bool = False
    responding_since: datetime | None = None


@dataclass(slots=True)
class _CancelWork:
    task_id: str
    attempt: Attempt | None


@dataclass(slots=True)
class _Pending:
    attempt: Attempt
    execution: Execution
    task: Task
    contract: dict[str, Any]
    repository_url: str = ""
    # The registered repository, for a private one's checkout token (ADR 0019).
    repository: Repository | None = None


def _secret_holds(provider: Any, harness: str, credential: Any) -> bool:
    """Whether a provider's harness Secret holds every required auth file, so routing
    never picks a harness nobody has logged in (ADR 0015). A Secret that cannot be read
    right now is not held against the harness: the seeding says why if it still cannot."""
    try:
        files = provider.read_credential_files(harness)
    except ProviderError:
        return True
    return files is not None and all(
        files.get(auth.name) for auth in credential.auth_files if auth.required
    )


@dataclass(frozen=True, slots=True)
class _WorkspaceRelease:
    attempt: Attempt
    provider: str
    reason: str
    policy_name: str
    policy_version: int
    days: int


def workspace_release_reason(
    uow: UnitOfWork, task: Task | None, attempt: Attempt, now: datetime, days: int
) -> str | None:
    """Why the kept workspace of a cleaned-up attempt may be removed now, or None while
    it must stay (16, the lab findings of 2026-09-29).

    It goes once its task is terminal (closed, rejected, cancelled) or the task's work
    was published, or once `days` have passed since cleanup, whichever is first; but
    never while something may still read it:

    - the task's latest implementing or correcting attempt, until that attempt's own
      bundle is published: acceptance, the publisher, and a republish after a failed
      push all read the bundle off this workspace (23);
    - an attempt whose quota checkpoint never reached the remote, while its task is
      open: the workspace holds the only copy of that work."""
    if attempt.cleaned_up_at is None:
        return None
    if task is None:
        return "task_gone"
    if task.state in TASK_TERMINAL:
        return f"task_{task.state.value}"
    if uow.events.latest_for_task_kind(
        task.id, EventKind.TASK_PUBLISH_FAILED.value
    ) is not None and any(
        event.attempt_id == attempt.id
        and event.kind == EventKind.TASK_PUBLISH_FAILED.value
        and event.payload.get("step") == "quota_checkpoint"
        for event in Supervisor._all_task_events(uow, task.id)
    ):
        return None
    published = uow.events.latest_for_task_kind(task.id, EventKind.PUBLISH_COMPLETED.value)
    if attempt.exit_class in {ExitClass.INFRASTRUCTURE, ExitClass.QUOTA_EXHAUSTED} and (
        published is None or published.ts < attempt.created_at
    ):
        # Keep the sealed source through retries and start failures until publication.
        return None
    work = latest_work_attempt(uow, task)
    if (
        work is not None
        and work[0].id == attempt.id
        and (published is None or published.attempt_id != attempt.id)
    ):
        return None
    # Once a correction is materialized it becomes `latest_work_attempt`, but its
    # preparer still needs the immediately preceding unpublished attempt's bundle.
    # Keep that source across a supervisor restart until preparation has produced the
    # correction's own workspace.
    if work is not None:
        latest_attempt, latest_execution = work
        if (
            latest_execution.role is ExecutionRole.CORRECT
            and not latest_attempt.resume_from_remote
            and not latest_attempt.workspace_path
        ):
            preceding = max(
                (
                    candidate
                    for candidate_execution in uow.executions.list_for_task(task.id)
                    if candidate_execution.id != latest_execution.id
                    and candidate_execution.role is not ExecutionRole.REVIEW
                    for candidate in uow.attempts.list_for_execution(candidate_execution.id)
                ),
                key=lambda candidate: candidate.id,
                default=None,
            )
            if preceding is not None and preceding.id == attempt.id:
                return None
    if published is not None:
        return "published"
    if now - attempt.cleaned_up_at >= timedelta(days=days):
        return "retention_window"
    return None


class Supervisor:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        providers: dict[str, ExecutionProvider],
        clock: Clock,
        *,
        holder: str,
        artifact_store: ArtifactStore,
        wake_deliverer: WakeDeliverer | None = None,
        github: GitHubClient | None = None,
        publisher: Publisher | None = None,
        delivery_config: DeliveryConfig | None = None,
        lease_ttl_seconds: int = 30,
        attempt_lease_ttl_seconds: int = 60,
        checkout_lease_ttl_seconds: int = 21600,
        grace_seconds: int = 60,
        launch_wait_seconds: float | None = None,
        collect_wait_seconds: float | None = None,
        collection_retry_ticks: int = 5,
        harnesses: HarnessRegistry | None = None,
        harness_gates: Mapping[str, HarnessGate] | None = None,
        credential_sources: Mapping[str, CredentialSource] | None = None,
        credential_sweep: Callable[[UnitOfWork], int] | None = None,
        credential_renewal: Callable[[], bool] | None = None,
        admin_context: AdminContext | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._clock = clock
        # 07: the adapters. Without a registry the launch spec carries the execution's
        # names and nothing harness-specific, which is the C3 shape the fake provider
        # runs; every real deployment injects one (cli/wiring.py).
        self._harnesses = harnesses
        self._harness_gates: dict[str, HarnessGate] = dict(harness_gates or {})
        self._credential_sources: dict[str, CredentialSource] = dict(credential_sources or {})
        # 25: rotated-out credential directories are shredded once their retention
        # window has elapsed; the retention step calls this with the fenced unit of work.
        self._credential_sweep = credential_sweep
        self._credential_renewal = credential_renewal
        self._admin_context = admin_context
        self._artifacts = artifact_store
        self._wakes = wake_deliverer
        self.holder = holder
        self.lease_ttl_seconds = lease_ttl_seconds
        self.attempt_lease_ttl_seconds = attempt_lease_ttl_seconds
        # Held for the life of the attempt (10); the TTL only bounds a lease whose
        # attempt died without a supervisor to release it.
        self.checkout_lease_ttl_seconds = checkout_lease_ttl_seconds
        self.grace_seconds = grace_seconds
        # hades #190: how long a tick waits for the launches in flight before it moves
        # on and leaves them running. Well inside the lease, so a slow prepare can never
        # keep the tick from renewing it; long enough that a quick launch still ends
        # in the tick that started it.
        self.launch_wait_seconds = (
            launch_wait_seconds
            if launch_wait_seconds is not None
            else min(5.0, lease_ttl_seconds / 3)
        )
        # The same bound for the collections a tick starts, kept apart so a test can
        # hold launches to a short wait without making quick collections miss the gates
        # of the tick that finished them.
        self.collect_wait_seconds = (
            collect_wait_seconds
            if collect_wait_seconds is not None
            else min(5.0, lease_ttl_seconds / 3)
        )
        # The launches in flight, by attempt id. An attempt in here is this process's
        # to finish; nothing else in the tick touches it until its task is done.
        self._launches: dict[str, asyncio.Task[bool]] = {}
        # hades #423: each provider's worker capacity as read once per launch pass, so a
        # pass over many pending attempts reads the namespace quota once.
        self._capacity_now: dict[str, Any] = {}
        # The collections in flight, by attempt id, and the same rule: an attempt in
        # here is its collection's until it ends (lab findings of 2026-09-29).
        self._collects: dict[str, asyncio.Task[bool]] = {}
        if collection_retry_ticks < 1:
            raise ValueError("collection_retry_ticks must be at least 1")
        self.collection_retry_ticks = collection_retry_ticks
        self._collect_pending_ticks: dict[str, int] = {}
        # A collection the provider could not finish is tried again after an interval,
        # for a bounded window counted from its first failure (in memory: a restart
        # starts the window again, and the workspace stays in place meanwhile).
        self._collect_failing_since: dict[str, float] = {}
        self._collect_retry_at: dict[str, float] = {}
        # Process-lifetime counts, emitted with every authentication failure.
        self.auth_failures_by_harness: Counter[str] = Counter()
        self.fenced_token: int | None = None
        self._handles: dict[str, Handle] = {}
        self._workspaces: dict[str, Workspace] = {}
        # The harnesses a login is running for, read once per launch pass (12, 25).
        self._logins_now: frozenset[str] = frozenset()
        self._workspace_fingerprints: dict[str, tuple[int, int, int] | None] = {}
        # FDY-0140: when each attempt's provider was last asked for its activity.
        self._activity_asked: dict[str, datetime] = {}
        self._activity_refresh: dict[str, int] = {}
        # Rebuilt from the stored log after a restart or takeover, so nothing is lost.
        self._command_watches: dict[str, _CommandWatch] = {}
        # ADR 0019: a private repository's checkout token is minted through this client.
        self._github = github
        # The delivery half (23). With no GitHub client configured it is inert, which is
        # what every tier below the live one runs with.
        self.delivery = DeliveryCoordinator(
            self, clock, github=github, publisher=publisher, config=delivery_config
        )

    # ----- infrastructure -------------------------------------------------

    @property
    def holds_lease(self) -> bool:
        return self.fenced_token is not None

    @contextmanager
    def _fenced(self) -> Iterator[UnitOfWork]:
        """A transaction carrying this supervisor's fenced token."""
        if self.fenced_token is None:
            raise LeaseLostError("no lease held")
        try:
            with self._uow_factory() as uow:
                uow.set_fenced_token(self.fenced_token)
                yield uow
        except IllegalTransitionError as exc:
            # The attempting transaction rolled back; the rejection is recorded on its own (09).
            self._record_rejection(exc)
            raise
        except FencedTokenRejectedError as exc:
            log.warning("fenced write rejected; standing down", extra={"holder": self.holder})
            self.fenced_token = None
            raise LeaseLostError(str(exc)) from exc

    def _record_rejection(self, exc: IllegalTransitionError) -> None:
        if self.fenced_token is None:
            log.error("illegal transition with no lease; not recorded", extra={"error": str(exc)})
            return
        try:
            with self._uow_factory() as fresh:
                fresh.set_fenced_token(self.fenced_token)
                record_rejected_transition(fresh, self._clock, exc, principal=PRINCIPAL_CRUCIBLE)
                fresh.commit()
        except FencedTokenRejectedError:
            log.error("illegal transition; lease lost before it could be recorded")

    async def _db(self, fn: Callable[[], T]) -> T:
        return await asyncio.to_thread(fn)

    def _provider(self, name: str) -> ExecutionProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise ProviderError(f"provider {name!r} is not registered") from None

    def _handle_for(self, attempt: Attempt) -> Handle:
        handle = self._handles.get(attempt.id)
        if handle is None:
            assert attempt.handle is not None
            provider = self._execution_provider_name(attempt)
            handle = Handle(provider=provider, ref=attempt.handle, attempt_id=attempt.id)
            self._handles[attempt.id] = handle
        return handle

    def _execution_provider_name(self, attempt: Attempt) -> str:
        with self._uow_factory() as uow:
            execution = uow.executions.get(attempt.execution_id)
            assert execution is not None
            return execution.provider

    def _workspace_for(self, attempt: Attempt) -> Workspace:
        ws = self._workspaces.get(attempt.id)
        if ws is None:
            root = attempt.workspace_path or f"unknown:///{attempt.id}"
            ws = Workspace(
                attempt_id=attempt.id,
                checkout_path=f"{root}/repo",
                identity_path=f"{root}/identity",
                report_path=f"{root}/report",
                output_path=f"{root}/output",
                identity_sha256=attempt.identity_sha256,
            )
            self._workspaces[attempt.id] = ws
        return ws

    # ----- lease ------------------------------------------------------------

    def _lease_step(self) -> bool:
        now = self._clock.now()
        with self._uow_factory() as uow:
            previous = uow.leases.get_supervisor()
            if self.fenced_token is not None:
                lease = uow.leases.renew_supervisor(
                    self.holder, self.fenced_token, now, self.lease_ttl_seconds
                )
                if lease is not None:
                    uow.commit()
                    return True
                self.fenced_token = None
                log.warning("supervisor lease lost", extra={"holder": self.holder})
            lease = uow.leases.acquire_supervisor(self.holder, now, self.lease_ttl_seconds)
            if lease is None:
                uow.commit()
                return False
            self.fenced_token = lease.fenced_token
            uow.set_fenced_token(lease.fenced_token)
            takeover = previous is not None and previous.holder != self.holder
            if (
                previous is None
                or previous.holder != self.holder
                or (previous.fenced_token != lease.fenced_token)
            ):
                record_event(
                    uow,
                    self._clock,
                    EventKind.SUPERVISOR_LEASE_ACQUIRED,
                    principal=PRINCIPAL_CRUCIBLE,
                    payload={
                        "holder": self.holder,
                        "fenced_token": lease.fenced_token,
                        "previous_holder": previous.holder if previous else None,
                        "takeover": takeover,
                    },
                )
            uow.commit()
            log.info(
                "supervisor lease acquired",
                extra={"holder": self.holder, "fenced_token": lease.fenced_token},
            )
            return True

    def _renew_lease(self) -> bool:
        """Renew the lease outside a fenced transaction so a mid-tick renewal
        does not hold up work when the database is under load.

        Returns ``True`` when the renewal succeeded, ``False`` when the lease
        could not be renewed (or was lost).  Errors are logged and the lease
        is dropped silently; the next fenced call will surface the loss via
        ``LeaseLostError``.
        """
        try:
            now = self._clock.now()
            assert self.fenced_token is not None
            with self._uow_factory() as uow:
                lease = uow.leases.renew_supervisor(
                    self.holder, self.fenced_token, now, self.lease_ttl_seconds
                )
                if lease is None:
                    self.fenced_token = None
                    log.warning("supervisor lease lost", extra={"holder": self.holder})
                    uow.commit()
                    return False
                uow.commit()
                return True
        except Exception:
            log.exception("could not renew the supervisor lease")
            return False

    async def _renew_lease_async(self) -> bool:
        """Run ``_renew_lease`` on a thread so the UnitOfWork, row lock and
        commit do not block the event loop."""
        return await self._db(self._renew_lease)

    def _release_step(self) -> None:
        if self.fenced_token is None:
            return
        with self._uow_factory() as uow:
            uow.set_fenced_token(self.fenced_token)
            if uow.leases.release_supervisor(self.holder, self.fenced_token):
                record_event(
                    uow,
                    self._clock,
                    EventKind.SUPERVISOR_LEASE_RELEASED,
                    principal=PRINCIPAL_CRUCIBLE,
                    payload={"holder": self.holder, "fenced_token": self.fenced_token},
                )
            uow.commit()
        self.fenced_token = None

    async def stop(self) -> None:
        """Release the lease cleanly (a crash simply lets it expire). A launch still in
        flight is cancelled first: the step it was on removes its Job, policy and
        per-attempt Secrets on the way out, and the attempt stays in preparing or
        launching for the next supervisor to reconcile, as after a crash (10). What a
        finished step left (the workspace claim, the identity ConfigMap) goes with
        the attempt's other objects in the provider's retention sweep."""
        await self._abandon_launches()
        await self._db(self._release_step)

    async def _abandon_launches(self) -> None:
        """Cancel the launches and collections in flight. A collection cancelled here
        leaves its attempt exited and uncollected, which the next holder collects
        again from its workspace, which still holds the work, as after a crash."""
        running = [*self._launches.values(), *self._collects.values()]
        self._launches.clear()
        self._collects.clear()
        # A later holder, this process included, starts the retry window afresh.
        self._collect_pending_ticks.clear()
        self._collect_failing_since.clear()
        self._collect_retry_at.clear()
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    # ----- tick -----------------------------------------------------------

    async def tick(self) -> TickResult:
        started = time.monotonic()
        token_before = self.fenced_token
        held = await self._db(self._lease_step)
        result = TickResult(held=held)
        if not held or self.fenced_token != token_before:
            # A launch begun under a lease this process no longer holds is not its to
            # finish: the next holder reconciles the attempt (hades #190 review).
            await self._abandon_launches()
        if not held:
            return result
        try:
            await self._refresh_admin_status()
            await self._check_gateway_models()
            result.orphans = await self._reconcile_provider_handles()
            await self._resume_quota_checkpoints()
            await self._db(self._resume_quota_waits)
            await self._resume_infrastructure_waits()
            await self._db(self._materialize_scheduled)
            await self._resume_gate_probes()
            result.launched = await self._launch_pending()
            observed, finished = await self._observe_attempts()
            result.observed, result.finished = observed, finished
            await self._sweep_cancellations()
            await self._db(self._materialize_evidence)
            await self._db(self._evaluate_pending_gates)
            # After the gates, never before: 16 says nothing a gate consumed is deleted
            # while the task still needs it, and cleanup only ever runs for an attempt
            # that recorded logs_drained (08).
            # The delivery half (23): publish what acceptance released, then observe
            # every pull request in an observed state. Both are no-ops without a
            # configured GitHub client.
            if not await self._renew_lease_async():
                result.held = False
                await self._abandon_launches()
                raise LeaseLostError("mid-tick renewal lost lease before publish")
            result.published = await self.delivery.publish()
            if not await self._renew_lease_async():
                result.held = False
                await self._abandon_launches()
                raise LeaseLostError("mid-tick renewal lost lease before observe")
            result.pull_requests_polled = await self.delivery.observe()
            if not await self._renew_lease_async():
                result.held = False
                await self._abandon_launches()
                raise LeaseLostError("mid-tick renewal lost lease before cleanup")
            await self._cleanup_step()
            if not await self._renew_lease_async():
                result.held = False
                await self._abandon_launches()
                raise LeaseLostError("mid-tick renewal lost lease before retention")
            await self._retention_step()
            await self._db(self._refresh_attempt_metrics)
            await self._db(self._repeat_stale_escalations)
            result.wakes_delivered = await self._deliver_wakes()
            result.counts = await self._db(partial(self._status_step, started))
        except LeaseLostError:
            result.held = False
            await self._abandon_launches()
        except Exception as exc:
            # The liveness row must say the tick failed, or readiness lies (19).
            summary = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
            await self._db(partial(self._record_failure, summary))
            raise
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    async def _refresh_admin_status(self) -> None:
        """Refresh expensive admin reads at most once per configured TTL."""
        ctx = self._admin_context
        if ctx is None:
            return

        def ttl() -> float:
            with self._uow_factory() as uow:
                return float(admin_status_cache.ttl_value(ctx, uow).value)

        if not ctx.status_cache.due(await self._db(ttl)):
            return
        images, providers = await asyncio.gather(refresh_images(ctx), refresh_providers_status(ctx))
        if ctx.status_cache_shared:

            def persist() -> None:
                with self._fenced() as uow:
                    admin_status_cache.write(ctx, uow, images, providers)
                    uow.commit()

            await self._db(persist)
        ctx.status_cache.images = images
        ctx.status_cache.providers = providers
        ctx.status_cache.refreshed_at = time.monotonic()

    def _record_failure(self, summary: str) -> None:
        try:
            with self._fenced() as uow:
                status = uow.supervisor_status.get()
                status.holder = self.holder
                status.last_tick_at = self._clock.now()
                status.last_error_at = status.last_tick_at
                status.last_error = summary[:1000]
                uow.supervisor_status.write(status)
                uow.commit()
        except Exception:
            # Nothing more to do here: a stale last_success_at already fails readiness.
            log.exception("could not record the tick failure on the liveness row")

    async def reconcile(self) -> TickResult:
        """Reconciliation is the tick; running it twice changes nothing the second time."""
        return await self.tick()

    async def _check_gateway_models(self) -> None:
        """Disable local routes missing from a successful gateway model listing.

        Listing is deliberately outside the write transaction. A missing configuration,
        missing key, timeout, or gateway refusal makes this check a no-op. The fenced
        write re-reads routing before it applies the result, and only turns routes off.
        """
        ctx = self._admin_context
        if ctx is None:
            return
        endpoint = await self._db(self._gateway_endpoint)
        if endpoint is None:
            return
        bearer = await asyncio.to_thread(admin_credentials.read_api_key, ctx, admin_gateway.HERMES)
        if bearer is None:
            return
        try:
            offered = await asyncio.to_thread(admin_gateway.fetch_models, endpoint, bearer)
        except (admin_gateway.GatewayError, TimeoutError):
            return
        await self._db(partial(self._disable_unoffered_gateway_models, endpoint, offered))

    def _gateway_endpoint(self) -> str | None:
        with self._fenced() as uow:
            endpoint, _source = admin_routing.gateway_url(uow)
            return endpoint

    def _disable_unoffered_gateway_models(self, endpoint: str, offered: Sequence[str]) -> None:
        ctx = self._admin_context
        if ctx is None:
            return
        offered_set = set(offered)
        with self._fenced() as uow:
            active_endpoint, _source = admin_routing.gateway_url(uow)
            if active_endpoint != endpoint:
                return
            try:
                policy, routing = admin_routing.active_documents(uow)
            except NotFoundError:
                return
            document = copy.deepcopy(routing.document)
            disabled: list[str] = []
            gateway_models: set[str] = set()
            for entry in document.get("models") or []:
                if entry.get("endpoint") != "local" or entry.get("enabled") is not True:
                    continue
                model_name = routing_model_name(entry)
                if model_name in offered_set:
                    continue
                entry["enabled"] = False
                entry["disabled_reason"] = admin_gateway.NOT_OFFERED
                entry["vanished_at"] = self._clock.now().isoformat()
                disabled.append(f"{entry.get('harness')}:{model_name}")
                gateway_models.add(model_name)
            if not disabled:
                return
            checked_at = self._clock.now()
            names = ", ".join(sorted(gateway_models))
            reason = f"gateway listing at {checked_at.isoformat()} no longer offers {names}"
            principal = Principal(
                id=PRINCIPAL_CRUCIBLE,
                name=PRINCIPAL_CRUCIBLE,
                role=Role.ADMIN,
                created_at=checked_at,
            )
            _policy_version, routing_version = admin_routing.publish_routing(
                ctx,
                uow,
                principal=principal,
                policy=policy,
                routing=routing,
                routing_document=document,
                reason=reason,
                note="Gateway listing removed local models",
            )
            record_event(
                uow,
                self._clock,
                EventKind.LOCAL_GATEWAY_UPDATED,
                principal=PRINCIPAL_CRUCIBLE,
                payload={
                    "change": "models automatically disabled",
                    "models": sorted(gateway_models),
                    "routes": sorted(disabled),
                    "checked_at": checked_at.isoformat(),
                    "routing_version": routing_version,
                    "reason": reason,
                },
            )
            uow.commit()

    # ----- step: provider reconcile ---------------------------------------

    async def _reconcile_provider_handles(self) -> int:
        orphans = 0
        for name, provider in self._providers.items():
            handles = await provider.reconcile()
            for handle in handles:
                action = await self._db(partial(self._classify_handle, handle))
                if action == "orphan":
                    await provider.terminate(handle, "kill")
                    await self._db(partial(self._record_orphan, handle, name))
                    orphans += 1
        return orphans

    def _classify_handle(self, handle: Handle) -> str:
        """A provider handle with no live attempt behind it is an orphan (10 step 3)."""
        with self._uow_factory() as uow:
            attempt = uow.attempts.get(handle.attempt_id)
            if (
                attempt is None
                or attempt.state in ATTEMPT_TERMINAL
                or attempt.state
                in (
                    AttemptState.EXITED,
                    AttemptState.COLLECTED,
                )
            ):
                return "orphan"
            self._handles.setdefault(handle.attempt_id, handle)
            return "keep"

    def _record_orphan(self, handle: Handle, provider: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(handle.attempt_id)
            record_event(
                uow,
                self._clock,
                EventKind.ORPHAN_REMOVED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id if attempt else None,
                execution_id=attempt.execution_id if attempt else None,
                attempt_id=attempt.id if attempt else None,
                payload={"provider": provider, "handle": handle.ref},
            )
            uow.commit()

    # ----- step: materialize scheduled tasks ------------------------------

    def _materialize_scheduled(self) -> None:
        with self._fenced() as uow:
            # hades #424: in the order the tasks entered the queue, so a batch approval's
            # selection order is the order their attempts are created and launched.
            queued = uow.tasks.list_by_state(TaskState.SCHEDULED, for_update=True)
            for task in sorted(queued, key=lambda row: queue_key(uow, row.id)):
                attempts = uow.attempts.list_for_task(task.id)
                if any(a.state not in ATTEMPT_TERMINAL for a in attempts):
                    continue
                stored = uow.contracts.get(task.id, task.contract_version)
                assert stored is not None
                # A contract version carrying a correction section runs as a `correct`
                # execution against the existing branch (09). One execution per version.
                # So does anything scheduled after a pull request exists, including a
                # `recollect` decision: the work continues against the remote work
                # branch, never from base_ref again (09, 23).
                role = (
                    ExecutionRole.CORRECT
                    if stored.document.get("correction")
                    or uow.pull_requests.get_for_task(task.id) is not None
                    else ExecutionRole.IMPLEMENT
                )
                matching = [
                    e
                    for e in uow.executions.list_for_task(task.id)
                    if e.contract_version == task.contract_version and e.role is role
                ]
                # A task is scheduled again by a decision on a blocked task (09) as well
                # as by a correction. Being scheduled always means new work: a further
                # attempt on the execution that is still open, or a fresh execution when
                # every one for this version has ended.
                open_execution = next(
                    (e for e in matching if e.state not in EXECUTION_TERMINAL), None
                )
                if open_execution is not None:
                    attempts_so_far = uow.attempts.list_for_execution(open_execution.id)
                    number = max((a.number for a in attempts_so_far), default=0) + 1
                    self._create_attempt(uow, open_execution, number=number)
                    record_event(
                        uow,
                        self._clock,
                        EventKind.EXECUTION_RESUMED,
                        principal=PRINCIPAL_CRUCIBLE,
                        task_id=task.id,
                        execution_id=open_execution.id,
                        payload={
                            "role": open_execution.role.value,
                            "contract_version": open_execution.contract_version,
                            "attempt_number": number,
                        },
                    )
                    continue
                policy = uow.policies.get(task.policy_name, task.policy_version)
                assert policy is not None
                req = stored.document["execution_request"]
                lifecycle = stored.document["lifecycle"]
                now = self._clock.now()
                pin = req.get("pin") or {}
                execution = Execution(
                    id=new_id(),
                    task_id=task.id,
                    role=role,
                    contract_version=task.contract_version,
                    harness=str(req.get("harness") or pin.get("harness") or "unselected"),
                    model=str(req.get("model") or pin.get("model") or "unselected"),
                    effort=req.get("effort"),
                    provider=str(req["provider"]),
                    image="unselected",
                    policy_snapshot=policy.document,
                    state=ExecutionState.CREATED,
                    max_attempts=int(lifecycle["max_attempts"]),
                    retry_on=[str(x) for x in lifecycle["retry_on"]],
                    timeout_seconds=int(req["timeout_seconds"]),
                    created_at=now,
                    # A head adoption and an automatic merge-main correction start at
                    # the remote branch tip. The previous attempt's sealed bundle is
                    # deliberately not an input: the remote head is the fact being
                    # adopted or repaired.
                    resume_from_remote=bool(
                        (
                            scheduled := uow.events.latest_for_task_kind(
                                task.id, EventKind.TASK_SCHEDULED.value
                            )
                        )
                        and scheduled.payload.get("resume_from_work_branch") is True
                    ),
                )
                uow.executions.add(execution)
                record_event(
                    uow,
                    self._clock,
                    EventKind.EXECUTION_CREATED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    execution_id=execution.id,
                    payload={
                        "role": execution.role.value,
                        "harness": execution.harness,
                        "model": execution.model,
                        "provider": execution.provider,
                        "image": execution.image,
                        "max_attempts": execution.max_attempts,
                        "timeout_seconds": execution.timeout_seconds,
                    },
                )
                self._create_attempt(uow, execution, number=1)
            self._materialize_review_executions(uow)
            uow.commit()

    def _create_attempt(
        self,
        uow: UnitOfWork,
        execution: Execution,
        *,
        number: int,
        excluded_pools: set[str] | None = None,
    ) -> Attempt:
        attempt = Attempt(
            id=new_id(),
            execution_id=execution.id,
            task_id=execution.task_id,
            number=number,
            state=AttemptState.PENDING,
            created_at=self._clock.now(),
            resume_from_remote=execution.resume_from_remote,
            routing_excluded_pools=sorted(excluded_pools or set()),
        )
        uow.attempts.add(attempt)
        record_event(
            uow,
            self._clock,
            EventKind.ATTEMPT_CREATED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=attempt.task_id,
            execution_id=execution.id,
            attempt_id=attempt.id,
            payload={"number": number},
        )
        return attempt

    def _materialize_review_executions(self, uow: UnitOfWork) -> None:
        """A `review` execution the API asked for (04). Execution rows are fenced to the
        supervisor, so the request is an event and this materializes it."""
        for task in uow.tasks.list_by_state(TaskState.AWAITING_INTERNAL_REVIEW, for_update=True):
            requested = uow.events.latest_for_task_kind(
                task.id, EventKind.REVIEW_EXECUTION_REQUESTED.value
            )
            if requested is None:
                continue
            request = requested.payload
            contract_version = int(request.get("contract_version", task.contract_version))
            existing = uow.executions.list_for_task_by_role(task.id, ExecutionRole.REVIEW)
            # A failed review execution may be asked for again; a live or successful one
            # is the answer to this request.
            if any(
                e.contract_version == contract_version and e.state is not ExecutionState.FAILED
                for e in existing
            ):
                continue
            if any(
                e.contract_version == contract_version
                and e.state is ExecutionState.FAILED
                and e.created_at > requested.ts
                for e in existing
            ):
                continue
            policy = uow.policies.get(task.policy_name, task.policy_version)
            assert policy is not None
            now = self._clock.now()
            execution = Execution(
                id=new_id(),
                task_id=task.id,
                role=ExecutionRole.REVIEW,
                contract_version=contract_version,
                harness=str(request["harness"]),
                model=str(request["model"]),
                effort=request.get("effort"),
                provider=str(request["provider"]),
                image=str(request["image"]),
                policy_snapshot=policy.document,
                state=ExecutionState.CREATED,
                max_attempts=1,
                retry_on=[],
                timeout_seconds=int(request["timeout_seconds"]),
                created_at=now,
            )
            uow.executions.add(execution)
            record_event(
                uow,
                self._clock,
                EventKind.EXECUTION_CREATED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                execution_id=execution.id,
                payload={
                    "role": execution.role.value,
                    "harness": execution.harness,
                    "model": execution.model,
                    "provider": execution.provider,
                    "image": execution.image,
                    "head_sha": task.head_sha,
                    "note": "reviewer_must_not_be_author: a review never shares an attempt",
                },
            )
            self._create_attempt(uow, execution, number=1)

    # ----- step: gates, metrics, escalations, wakes -------------------------

    def _materialize_evidence(self) -> None:
        """`evidence` is fenced to the supervisor (14), so the rows a gate consumes are
        derived here from what the API recorded: uploaded artifacts and review reports.

        Idempotent: a row is written only when no evidence already points at the source."""
        with self._fenced() as uow:
            for state in (
                TaskState.REPORTED,
                TaskState.PUBLISHING,
                TaskState.AWAITING_EXTERNAL_REVIEW,
                TaskState.AWAITING_CI_CERTIFICATION,
                TaskState.EXTERNAL_FEEDBACK_RECEIVED,
                TaskState.CI_CERTIFICATION_FAILED,
                TaskState.READY_FOR_MERGE,
                TaskState.ACCEPTED,
                TaskState.AWAITING_INTERNAL_REVIEW,
                TaskState.PRE_PR_GATES_FAILED,
                TaskState.AWAITING_ACCEPTANCE,
            ):
                for task in uow.tasks.list_by_state(state):
                    self._evidence_for_task(uow, task)
            uow.commit()

    def _evidence_for_task(self, uow: UnitOfWork, task: Task) -> None:
        existing = list(uow.evidence.list_for_task(task.id))
        seen_artifacts = {e.artifact_id for e in existing if e.artifact_id is not None}
        seen_reports = {
            str(e.payload.get("review_report_id"))
            for e in existing
            if e.kind == EvidenceKind.REVIEW_RECEIVED.value
        }
        work = latest_work_attempt(uow, task)
        if work is not None:
            attempt = work[0]
            for artifact in uow.artifacts.list_for_attempt(attempt.id):
                if artifact.type != "run_evidence" or artifact.id in seen_artifacts:
                    continue
                uow.evidence.add(
                    EvidenceRecord(
                        id=None,
                        attempt_id=attempt.id,
                        task_id=task.id,
                        kind=EvidenceKind.ARTIFACT_PRESENT.value,
                        observed_at=self._clock.now(),
                        source=EvidenceSource.CRUCIBLE.value,
                        verified=True,
                        payload={
                            "role": ROLE_RUN_EVIDENCE,
                            # The gate compares the name the contract asked for, not the
                            # content-addressed path the bytes landed at.
                            "path": artifact.filename,
                            "stored_at": artifact.path,
                            "size": artifact.size,
                            "uploaded_by": artifact.created_by,
                        },
                        artifact_id=artifact.id,
                    )
                )
        authors = author_attempt_ids(uow, task)
        for report in uow.review_reports.list_for_task(task.id):
            if report.id in seen_reports:
                continue
            uow.evidence.add(
                EvidenceRecord(
                    id=None,
                    attempt_id=report.reviewer_attempt_id,
                    task_id=task.id,
                    kind=EvidenceKind.REVIEW_RECEIVED.value,
                    observed_at=self._clock.now(),
                    source=EvidenceSource.CRUCIBLE.value,
                    verified=True,
                    payload=review_evidence_payload(
                        report,
                        reviewer_is_author=report.reviewer_attempt_id in authors,
                    ),
                    artifact_id=report.artifact_id,
                )
            )

    def _evaluate_pending_gates(self) -> None:
        """10 step 5: every task in `reported` with pending gates is evaluated.

        Persisted tasks from older deployments in `awaiting_internal_review` also
        resume through the self-review gate and automatic acceptance path."""
        for state in (TaskState.REPORTED, TaskState.AWAITING_INTERNAL_REVIEW):
            with self._fenced() as uow:
                for task in uow.tasks.list_by_state(state, for_update=True):
                    work = latest_work_attempt(uow, task)
                    if work is None:
                        continue
                    attempt, execution = work
                    if attempt.state not in ATTEMPT_TERMINAL:
                        continue
                    evaluate_and_advance(
                        uow, self._clock, task=task, attempt=attempt, execution=execution
                    )
                    self._metrics_for_task(uow, task)
                uow.commit()

    def _refresh_attempt_metrics(self) -> None:
        """Fold gate counts, corrections, and the acceptance verdict into AttemptMetrics.

        The API writes acceptance and contract versions but cannot write this fenced table
        (14), so the supervisor backfills it. Writing the same values twice changes
        nothing, which keeps reconciliation idempotent."""
        with self._fenced() as uow:
            # States whose metrics can still change. A closed or cancelled task is done
            # with, and rescanning it every tick would grow the tick without end.
            # The review states too (ADR 0028): a pass waiting for its review counts
            # as soon as a failure does, or routing would read too high a failure rate.
            settled = (
                TaskState.AWAITING_INTERNAL_REVIEW,
                TaskState.GATES_PASSED,
                TaskState.AWAITING_ACCEPTANCE,
                TaskState.ACCEPTED,
                TaskState.PRE_PR_GATES_FAILED,
                TaskState.REJECTED,
            )
            for state in settled:
                for task in uow.tasks.list_by_state(state):
                    self._metrics_for_task(uow, task)
            uow.commit()

    def _metrics_for_task(self, uow: UnitOfWork, task: Task) -> None:
        acceptances = [a for a in uow.acceptance.list_for_task(task.id) if a.superseded_at is None]
        verdict = acceptances[-1].verdict.value if acceptances else None
        corrections = sum(
            1 for c in uow.contracts.list_for_task(task.id) if (c.document or {}).get("correction")
        )
        for execution in uow.executions.list_for_task(task.id):
            if execution.role is ExecutionRole.REVIEW:
                continue
            for attempt in uow.attempts.list_for_execution(execution.id):
                metrics = uow.attempt_metrics.get(attempt.id)
                if metrics is None:
                    continue
                gates = uow.gate_results.list_for_attempt(attempt.id)
                passed = sum(1 for g in gates if g.result == "pass")
                # ADR 0028: routing judges a model on blocking failures only.
                failed = count_blocking_failures(gates)
                after = sum(
                    1
                    for c in uow.contracts.list_for_task(task.id)
                    if (c.document or {}).get("correction")
                    and c.version > execution.contract_version
                )
                unchanged = (
                    metrics.gates_passed == passed
                    and metrics.gates_failed == failed
                    and metrics.corrections_after == after
                    and metrics.acceptance_verdict == verdict
                )
                if unchanged:
                    continue
                metrics.gates_passed = passed
                metrics.gates_failed = failed
                metrics.corrections_after = after
                metrics.acceptance_verdict = verdict
                uow.attempt_metrics.put(metrics)
                record_event(
                    uow,
                    self._clock,
                    EventKind.ATTEMPT_METRICS_RECORDED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={
                        "gates_passed": passed,
                        "gates_failed": failed,
                        "corrections_after": after,
                        "acceptance_verdict": verdict,
                        "corrections_total": corrections,
                    },
                )

    def _repeat_stale_escalations(self) -> None:
        with self._fenced() as uow:
            repeat_stale_escalation_wakes(
                uow, self._clock, stale_hours=self._escalation_stale_hours(uow)
            )
            uow.commit()

    def _escalation_stale_hours(self, uow: UnitOfWork) -> int:
        for escalation in uow.escalations.list_open():
            task = uow.tasks.get(escalation.task_id)
            if task is None:
                continue
            policy = uow.policies.get(task.policy_name, task.policy_version)
            if policy is not None:
                return int(
                    policy.document.get("limits", {}).get(
                        "escalation_stale_hours", DEFAULT_ESCALATION_STALE_HOURS
                    )
                )
        return DEFAULT_ESCALATION_STALE_HOURS

    async def _deliver_wakes(self) -> int:
        """10 step 8: redeliver every wake past its retry schedule. Poll is the fallback,
        so a missing or failing webhook only delays (17)."""
        if self._wakes is None or not self._wakes.configured:
            return 0
        delivered = 0
        for wake_id, body, retry_hours in await self._db(self._list_due_wakes):
            result = await self._wakes.deliver(body)
            await self._db(
                partial(self._record_wake_delivery, wake_id, result.ok, result.detail, retry_hours)
            )
            delivered += int(result.ok)
        return delivered

    def _list_due_wakes(self) -> list[tuple[str, bytes, int]]:
        out: list[tuple[str, bytes, int]] = []
        with self._uow_factory() as uow:
            for wake in uow.wakes.list_undelivered(self._clock.now()):
                policy_document = None
                if wake.task_id is not None:
                    task = uow.tasks.get(wake.task_id)
                    if task is not None:
                        policy = uow.policies.get(task.policy_name, task.policy_version)
                        policy_document = policy.document if policy else None
                out.append(
                    (wake.id, wake_body(uow, wake), retry_hours_from_policy(policy_document))
                )
        return out

    def _record_wake_delivery(self, wake_id: str, ok: bool, detail: str, retry_hours: int) -> None:
        with self._fenced() as uow:
            record_delivery(
                uow, self._clock, wake_id, ok=ok, detail=detail, retry_hours=retry_hours
            )
            uow.commit()

    # ----- step: launch pending attempts -------------------------------

    def _list_pending(self) -> list[_Pending]:
        out: list[_Pending] = []
        with self._uow_factory() as uow:
            for attempt in uow.attempts.list_in_states([AttemptState.PENDING]):
                execution = uow.executions.get(attempt.execution_id)
                task = uow.tasks.get(attempt.task_id)
                if execution is None or task is None:
                    continue
                if execution.role is ExecutionRole.REVIEW:
                    if task.state is not TaskState.AWAITING_INTERNAL_REVIEW:
                        continue
                elif task.state not in (TaskState.SCHEDULED, TaskState.RUNNING):
                    continue
                if task.resume_at is not None and task.resume_at > self._clock.now():
                    continue
                stored = uow.contracts.get(task.id, execution.contract_version)
                assert stored is not None
                repository = uow.repositories.get(task.repository_id)
                out.append(
                    _Pending(
                        attempt,
                        execution,
                        task,
                        stored.document,
                        repository.url if repository else "",
                        repository,
                    )
                )
            # hades #424: launched in queue order, not in the order of the attempt ids.
            out.sort(key=lambda item: queue_key(uow, item.task.id))
        return out

    async def _launch_pending(self) -> int:
        """Start each pending attempt's launch, then wait a bounded time for the
        launches in flight (hades #190). What finishes in that window is counted now;
        what does not keeps running and is counted by the tick that sees it end."""
        launched = self._harvest_launches()
        started: list[asyncio.Task[bool]] = []
        self._capacity_now.clear()
        for item in await self._db(self._list_pending):
            if item.attempt.id in self._launches:
                continue
            with log_context(
                task_id=item.task.id, execution_id=item.execution.id, attempt_id=item.attempt.id
            ):
                try:
                    begun = await self._begin_launch(item)
                except LeaseLostError:
                    raise
                except Exception:
                    log.exception("launch step failed; continuing with the next attempt")
                    continue
            if begun is not None:
                launch = asyncio.create_task(
                    self._finish_launch(*begun), name=f"launch-{item.attempt.id}"
                )
                self._launches[item.attempt.id] = launch
                if not task_specific_checks(item.contract, item.execution.policy_snapshot):
                    started.append(launch)
        # Only this tick's launches are waited on: one begun earlier that is still
        # running is a slow one, and waiting on it again would slow every tick.
        if started:
            await asyncio.wait(started, timeout=self.launch_wait_seconds)
        return launched + self._harvest_launches()

    async def _resume_gate_probes(self) -> None:
        """Adopt probe Jobs left by a stopped supervisor before observe strands them."""
        for item in await self._db(self._list_preparing):
            if item.attempt.id in self._launches:
                continue
            provider = self._provider(item.execution.provider)
            if not task_specific_checks(item.contract, item.execution.policy_snapshot):
                continue
            if not await provider.gate_probe_exists(item.attempt.id):
                continue
            self._launches[item.attempt.id] = asyncio.create_task(
                self._finish_launch(item, provider), name=f"launch-{item.attempt.id}"
            )

    def _list_preparing(self) -> list[_Pending]:
        out: list[_Pending] = []
        with self._uow_factory() as uow:
            for attempt in uow.attempts.list_in_states([AttemptState.PREPARING]):
                execution = uow.executions.get(attempt.execution_id)
                task = uow.tasks.get(attempt.task_id)
                if execution is None or task is None:
                    continue
                stored = uow.contracts.get(task.id, execution.contract_version)
                repository = uow.repositories.get(task.repository_id)
                if stored is not None:
                    out.append(
                        _Pending(
                            attempt,
                            execution,
                            task,
                            stored.document,
                            repository.url if repository else "",
                            repository,
                        )
                    )
        return out

    def _harvest_launches(self) -> int:
        return self._harvest(self._launches, "launch")

    def _harvest(self, running: dict[str, asyncio.Task[bool]], what: str) -> int:
        """Take the background tasks of one kind that ended and count those that
        returned True; a lost lease in any of them is the tick's."""
        done = 0
        lease_lost = False
        for attempt_id, task in list(running.items()):
            if not task.done():
                continue
            del running[attempt_id]
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None and not isinstance(error, Exception):
                # What ends the process (a crash, an exit) ends it here too.
                raise error
            if isinstance(error, LeaseLostError):
                lease_lost = True
            elif error is not None:
                with log_context(attempt_id=attempt_id):
                    log.error(
                        "%s step failed; continuing with the next attempt",
                        what,
                        exc_info=(type(error), error, error.__traceback__),
                    )
            elif task.result():
                done += 1
        if lease_lost:
            raise LeaseLostError(f"the lease was lost during a {what}")
        return done

    async def _begin_launch(self, item: _Pending) -> tuple[_Pending, ExecutionProvider] | None:
        """The quick half of a launch, in the tick itself: route, gate, take the
        checkout, and move the attempt to preparing. Each step is a short database
        transaction, and they run one attempt after another, so the per-harness cap and
        the checkout lease see every launch this tick has already begun."""
        attempt, execution, task = item.attempt, item.execution, item.task
        review = execution.role is ExecutionRole.REVIEW
        # Read per attempt, not per pass: a login started while this pass runs holds
        # back the next launch of its harness rather than racing it (12).
        self._logins_now = await self._logins_in_progress()
        # hades #478: update the Kubernetes provider's active policy so its quota
        # read divides by the correct shape, not the last launch.
        self._set_provider_policy(
            provider_name=execution.provider, policy=execution.policy_snapshot
        )
        # hades #423: a provider whose capacity comes from its own ceiling (the namespace
        # quota) says how many workers it admits at once; a launch past that waits
        # here, pending and scheduled, rather than being refused by the quota later.
        capacity_wait = await self._provider_capacity_wait(execution.provider)
        if capacity_wait is not None:
            await self._db(partial(self._defer_launch, attempt.id, capacity_wait))
            return None
        if not review:
            selection = await self._db(partial(self._preview_route, item))
            if selection is None or selection.selected is None or selection.image is None:
                # Only refuse or wait here: this attempt holds no checkout lease, so it
                # must never be moved to preparing on this path.
                await self._db(partial(self._route_pending, item, launch=False))
                return None
            execution = replace(
                execution,
                model=selection.selected.id,
                harness=selection.selected.harness,
                image=selection.image,
            )
        refusal = await self._db(partial(self._harness_gate, execution)) if review else None
        if refusal is not None:
            # The same path an environment failure at prepare takes: the attempt and the
            # execution become active first, so the refusal can end them.
            if await self._db(partial(self._mark_preparing, attempt.id)):
                await self._db(partial(self._refuse_launch, attempt.id, "registry", refusal))
            return None
        provider = self._provider(execution.provider)
        key = self.checkout_key(item.contract, task.external_id, item.repository_url)
        # Reviews have a fixed harness: the cap check and the pool slot it takes are one
        # transaction (hades #359). Routed launches check candidate capacity in
        # _route_pending after taking the checkout lease, so a busy first choice can
        # fall through to an idle harness.
        if review:
            started, busy, refusal = await self._db(
                partial(self._mark_review_preparing, attempt.id)
            )
            if busy is not None:
                await self._db(partial(self._defer_launch, attempt.id, busy))
            if refusal is not None:
                await self._db(partial(self._refuse_launch, attempt.id, "routing", refusal))
                return None
            return (replace(item, attempt=started), provider) if started else None
        if not await self._db(partial(self._take_checkout_lease, attempt.id, key)):
            # A second attempt on the same repository and branch waits; it is not a
            # failure, and nothing of the holder's checkout is disturbed (10).
            return None
        routed = await self._db(partial(self._route_pending, item))
        if routed is None:
            await self._db(partial(self._release_attempt_checkout, attempt.id))
            return None
        item = routed
        refusal = await self._db(partial(self._harness_gate, item.execution))
        if refusal is not None:
            await self._db(partial(self._refuse_launch, item.attempt.id, "registry", refusal))
            return None
        return item, provider

    def _set_provider_policy(self, provider_name: str, policy: dict[str, Any] | None) -> None:
        """hades #478: update a provider's active policy so capacity and launch
        shape come from the policy, not the last launch."""
        provider = self._providers.get(provider_name)
        setter = getattr(provider, "set_policy", None)
        if provider is None or not callable(setter):
            return
        setter(policy or {})

    async def _provider_capacity_wait(self, provider_name: str) -> str | None:
        """hades #423: why a launch on this provider waits for room, or None. Only a
        provider that derives its capacity (`worker_capacity`, the Kubernetes provider
        from the namespace quota, or its configured fallback without one) holds launches
        here; the attempts already holding a slot are counted from the database, so the
        launches this tick has begun count too."""
        provider = self._providers.get(provider_name)
        reader = getattr(provider, "worker_capacity", None)
        if provider is None or not callable(reader):
            return None
        capacity = self._capacity_now.get(provider_name)
        if capacity is None:
            try:
                capacity = await reader()
            except ProviderError as exc:
                log.warning("the provider's worker capacity could not be read: %s", exc)
                return None
            self._capacity_now[provider_name] = capacity
        workers = int(capacity.workers)
        holding = await self._db(partial(self._slots_held, provider_name))
        if holding < workers:
            return None
        return (
            f"{provider_name} admits {workers} worker(s) at once ({capacity.source}; "
            f"{capacity.reserved_pods} Pod(s) kept for Hades's own short-role Pods) and "
            f"{holding} attempt(s) hold them; the launch waits for one to finish"
        )

    def _slots_held(self, provider_name: str) -> int:
        with self._uow_factory() as uow:
            held = 0
            for attempt in uow.attempts.list_in_states(list(SLOT_HOLDING_STATES)):
                execution = uow.executions.get(attempt.execution_id)
                if execution is not None and execution.provider == provider_name:
                    held += 1
            return held

    async def _finish_launch(self, item: _Pending, provider: ExecutionProvider) -> bool:
        """The slow half, as a task of its own (hades #190): build the spec, prepare
        the checkout, and start the worker. A cancel is honoured before each step, while
        the prepare runs, just before the worker is created, and in the transaction that
        would record it running; a cancelled task never has a running worker (hades
        #189). Until that transaction the attempt is this task's alone: the cancel sweep
        acts only on pending and running attempts, and observe skips it."""
        attempt, execution, task = item.attempt, item.execution, item.task
        with log_context(task_id=task.id, execution_id=execution.id, attempt_id=attempt.id):
            if await self._db(partial(self._settle_if_cancelled, attempt.id, "spec")):
                return False
            try:
                spec = await self._build_spec(
                    attempt, execution, task, item.contract, item.repository_url
                )
                if not await self._probe_before_prepare(item, provider, spec):
                    return False
                ws = await self._prepare(provider, spec, item.repository)
            except LaunchCancelledError as exc:
                log.info("launch stopped for a cancel", extra={"detail": str(exc)})
                await self._db(partial(self._settle_if_cancelled, attempt.id, "prepare"))
                return False
            except CheckoutRefusedError as exc:
                await self._db(
                    partial(
                        self._environment_failure,
                        attempt.id,
                        "prepare",
                        f"refusing to prepare: {exc}",
                    )
                )
                return False
            except LaunchRefusedError as exc:
                await self._db(partial(self._refuse_launch, attempt.id, "prepare", str(exc)))
                return False
            except LaunchWaitError as exc:
                # hades #423: the quota had no room for the probe or the preparer. Back
                # to pending; a later tick launches it.
                await self._db(partial(self._return_to_pending, attempt.id, "prepare", str(exc)))
                return False
            except ProviderError as exc:
                detail = str(exc)
                await self._db(partial(self._environment_failure, attempt.id, "prepare", detail))
                return False
            self._workspaces[attempt.id] = ws
            if getattr(provider, "activity", None) is None:
                self._workspace_fingerprints[attempt.id] = workspace_fingerprint(ws)
            await self._db(partial(self._record_prepared, attempt.id, ws, spec.effective_settings))
            if not await self._db(partial(self._mark_launching, attempt.id, ws)):
                await self._discard(provider, ws, spec)
                self._forget_workspace(attempt.id)
                return False
            try:
                handle = await provider.launch(ws, spec, cancelled=self._cancel_check(task.id))
            except LaunchCancelledError as exc:
                log.info("launch stopped for a cancel", extra={"detail": str(exc)})
                await self._discard(provider, ws, spec)
                self._forget_workspace(attempt.id)
                await self._db(partial(self._settle_if_cancelled, attempt.id, "launch"))
                return False
            except LaunchRefusedError as exc:
                await self._discard(provider, ws, spec)
                await self._db(partial(self._refuse_launch, attempt.id, "launch", str(exc)))
                return False
            except LaunchWaitError as exc:
                # hades #423: the quota had no room for the worker. Nothing ran; the
                # attempt goes back to pending, unconsumed, and launches on a later tick.
                await self._discard(provider, ws, spec)
                self._forget_workspace(attempt.id)
                await self._db(partial(self._return_to_pending, attempt.id, "launch", str(exc)))
                return False
            except WorkerStartError as exc:
                # Hades #346: the runtime could not start the worker's process. Nothing
                # ran, so it is retried as an infrastructure interruption.
                await self._discard(provider, ws, spec)
                await self._db(partial(self._start_failure, attempt.id, exc.observation))
                return False
            except ProviderError as exc:
                detail = str(exc)
                await self._discard(provider, ws, spec)
                await self._db(partial(self._environment_failure, attempt.id, "launch", detail))
                return False
            self._handles[attempt.id] = handle
            if not await self._db(partial(self._mark_running, attempt.id, handle)):
                # The cancel landed while the provider was starting the worker. The
                # attempt is already settled; the worker it started is stopped here, and
                # anything of it that survives goes with provider retention.
                await self._stop_cancelled_worker(provider, handle, ws, spec)
                return False
            log.info("attempt launched", extra={"handle": handle.ref, "provider": provider.name})
            return True

    async def _stop_cancelled_worker(
        self, provider: ExecutionProvider, handle: Handle, ws: Workspace, spec: LaunchSpec
    ) -> None:
        log.info("worker stopped for a cancel during launch", extra={"handle": handle.ref})
        try:
            await provider.terminate(handle, "kill")
        except Exception:  # the discard below still runs; retention removes the worker
            log.exception("terminate of a cancelled launch failed; retention removes it")
        finally:
            await self._discard(provider, ws, spec)
            self._handles.pop(handle.attempt_id, None)
            self._forget_workspace(handle.attempt_id)

    def _forget_workspace(self, attempt_id: str) -> None:
        self._workspaces.pop(attempt_id, None)
        self._workspace_fingerprints.pop(attempt_id, None)
        self._activity_asked.pop(attempt_id, None)
        self._activity_refresh.pop(attempt_id, None)
        self._command_watches.pop(attempt_id, None)

    async def _build_spec(
        self,
        attempt: Attempt,
        execution: Execution,
        task: Task,
        contract: dict[str, Any],
        repository_url: str = "",
        *,
        credential_mounted: bool | None = None,
    ) -> LaunchSpec:
        env: dict[str, str] = {}
        if execution.role is ExecutionRole.REVIEW and task.head_sha:
            env["CRUCIBLE_REVIEW_HEAD_SHA"] = task.head_sha
        selected_harness = attempt.selected_harness or execution.harness
        selected_model = attempt.selected_model or execution.model
        selected_image = attempt.selected_image or execution.image
        effective_contract = copy.deepcopy(contract)
        if attempt.resume_from_remote:
            effective_contract.setdefault("repository", {})["resume_from_work_branch"] = True
        endpoint: Literal["subscription", "local"] = "subscription"
        endpoint_url = None
        with self._uow_factory() as route_uow:
            routing = load_attempt_routing(
                route_uow, execution.policy_snapshot or {}, attempt.routing_version
            )
            route = None if routing is None else routing.model(selected_model, selected_harness)
            if route is not None:
                endpoint = route.endpoint
                endpoint_url = route.endpoint_url
            settings_harness = (
                "hermes"
                if endpoint == "local" and selected_harness in {"codex", "qwen_code"}
                else selected_harness
            )
            saved = (
                route_uow.provider_settings.get(setting_name(settings_harness))
                if selected_harness
                else None
            )
            credential_sources = getattr(self, "_credential_sources", {})
            seed_source = credential_sources.get(settings_harness)
            settings_adapter = self._harnesses.get(settings_harness) if self._harnesses else None
            declaration = settings_adapter.credential_spec() if settings_adapter else None
            launch_mount_mode = (
                MountMode.RO
                if endpoint == "local" and selected_harness in {"codex", "qwen_code"}
                else MountMode(
                    resolve_runtime_setting(
                        route_uow,
                        name=f"credentials.{settings_harness}.mount_mode",
                        field="mount_mode",
                        seed=(
                            seed_source.mount_mode.value
                            if seed_source is not None and seed_source.mount_mode is not None
                            else None
                        ),
                        seed_source="environment",
                        default=(
                            MountMode.RENEWER.value
                            if settings_harness == "codex"
                            else declaration.minimum_mode.value
                        ),
                        applies="next launch",
                    ).value
                )
                if declaration is not None
                else None
            )
            resume_bundle: dict[str, str] = {}
            interruption_retry = attempt.number > 1 and any(
                prior.exit_class in {ExitClass.INFRASTRUCTURE, ExitClass.QUOTA_EXHAUSTED}
                for prior in route_uow.attempts.list_for_execution(execution.id)
            )
            can_resume_bundle = (
                execution.role is ExecutionRole.CORRECT or interruption_retry
            ) and not attempt.resume_from_remote
            published = (
                route_uow.events.latest_for_task_kind(task.id, EventKind.PUBLISH_COMPLETED.value)
                if can_resume_bundle
                else None
            )
            gate_failure = (
                route_uow.events.latest_for_task_kind(
                    task.id, EventKind.TASK_PRE_PR_GATES_FAILED.value
                )
                if can_resume_bundle
                else None
            )
            correction = contract.get("correction") or {}
            last_attempt = (
                correction.get(
                    "resume_from",
                    (
                        "last_attempt"
                        if correction.get("reason") == "pre_pr_gates"
                        else "remote_branch"
                    ),
                )
                == "last_attempt"
            )
            # Corrections and interrupted workers resume a verified, sealed workspace.
            if can_resume_bundle and (
                interruption_retry
                or published is None
                or (last_attempt and gate_failure is not None)
            ):
                preceding = [
                    candidate
                    for candidate_execution in route_uow.executions.list_for_task(task.id)
                    if candidate_execution.role is not ExecutionRole.REVIEW
                    for candidate in route_uow.attempts.list_for_execution(candidate_execution.id)
                    if candidate.id < attempt.id
                ]
                newest = max(preceding, key=lambda candidate: candidate.id, default=None)

                def failed_secret_gate(candidate: Attempt) -> bool:
                    return any(
                        row.gate == "no_secrets" and row.result == "fail"
                        for row in route_uow.gate_results.list_for_attempt(candidate.id)
                    )

                previous_attempt = max(
                    (
                        candidate
                        for candidate in preceding
                        if candidate.workspace_path
                        and (
                            published is None
                            or interruption_retry
                            or (
                                candidate is newest
                                and gate_failure is not None
                                and candidate.id == gate_failure.attempt_id
                                and not failed_secret_gate(candidate)
                            )
                        )
                        and any(
                            row.kind == EvidenceKind.BUNDLE_HEAD.value
                            and row.verified
                            and row.payload.get("bundle_verified")
                            and row.payload.get("bundle_sha256")
                            for row in route_uow.evidence.list_for_attempt(candidate.id)
                        )
                    ),
                    key=lambda candidate: candidate.id,
                    default=None,
                )
                if previous_attempt is not None:
                    bundle = next(
                        (
                            row
                            for row in reversed(
                                route_uow.evidence.list_for_attempt(previous_attempt.id)
                            )
                            if row.kind == EvidenceKind.BUNDLE_HEAD.value
                            and row.verified
                            and row.payload.get("bundle_verified")
                        ),
                        None,
                    )
                    if bundle is not None:
                        resume_bundle = {
                            "path": f"{previous_attempt.workspace_path}/output/work_branch.bundle",
                            "attempt_id": previous_attempt.id,
                            "head": str(bundle.payload.get("head_sha") or ""),
                            "sha256": str(bundle.payload.get("bundle_sha256") or ""),
                        }
        # FDY-0140: the harness's run settings as saved now, read at every launch.
        harness_settings = dict(saved.document) if saved is not None else {}
        # Hades #388: the window, response allowance and thinking setting are fixed for
        # the attempt. The first spec resolves them from the saved limits and the routing
        # entry; the launch records them, and every later spec of the attempt reuses
        # that record rather than a value saved since.
        effective: dict[str, Any] | None = None
        if settings_harness == "hermes":
            effective = attempt.effective_settings or effective_settings(
                harness_settings,
                thinking=route.chat_template_kwargs.enable_thinking if route else False,
            )
        if selected_harness == "qwen_code":
            effective = attempt.effective_settings or {
                "context_length": route.context_length
                if route and route.context_length
                else DEFAULT_QWEN_CONTEXT_LENGTH
            }
        if effective is not None:
            harness_settings.update(effective)
        # Issue 128: the policy default, narrowed by the contract, capped at the attempt.
        command_timeout_ms = effective_command_timeout_ms(
            execution.policy_snapshot, contract, execution.timeout_seconds
        )
        spec = LaunchSpec(
            attempt_id=attempt.id,
            task_id=task.id,
            external_id=task.external_id,
            role=execution.role.value,
            harness=selected_harness,
            model=selected_model,
            image=selected_image,
            timeout_seconds=execution.timeout_seconds,
            contract=effective_contract,
            env=env,
            network=contract.get("constraints", {}).get("network", "policy"),
            policy=execution.policy_snapshot or {},
            owner=task.principal_id,
            repository_url=repository_url,
            effort=execution.effort,
            endpoint=endpoint,
            endpoint_url=endpoint_url,
            command_timeout_ms=command_timeout_ms,
            harness_settings=harness_settings,
            effective_settings=dict(effective) if effective is not None else None,
            credential_mode=launch_mount_mode.value if launch_mount_mode is not None else None,
            resume_bundle_path=resume_bundle.get("path"),
            resume_bundle_attempt_id=resume_bundle.get("attempt_id"),
            resume_bundle_head=resume_bundle.get("head"),
            resume_bundle_sha256=resume_bundle.get("sha256"),
            resume_bundle_ancestor=(
                str(published.payload["head_sha"]) if published and resume_bundle else None
            ),
        )
        adapter = self._harnesses.get(selected_harness) if self._harnesses else None
        if adapter is None:
            return spec
        credential_harness = (
            "hermes"
            if selected_harness in {"codex", "qwen_code"} and endpoint == "local"
            else selected_harness
        )
        credential_adapter = self._harnesses.get(credential_harness) if self._harnesses else None
        credential = credential_adapter.credential_spec() if credential_adapter else None
        source = self._credential_sources.get(credential_harness)
        if source is not None and launch_mount_mode is not None:
            source = replace(source, mount_mode=launch_mount_mode)
        elif launch_mount_mode is not None:
            source = CredentialSource(path="", mount_mode=launch_mount_mode)
        if credential_mounted is None:
            if credential is not None:
                if source is not None and credential.held_by(source.path):
                    credential_mounted = True
                elif execution.provider in self._providers:
                    credential_mounted = await self._providers[
                        execution.provider
                    ].credential_available(credential_harness)
                else:
                    credential_mounted = False
            else:
                credential_mounted = False
        # 07: the adapter's launch shape. Argv carries the pointer; the identity and
        # the contract are files; a credential value is never in any of it.
        launch = adapter.build_launch(
            LaunchContext(
                attempt_id=attempt.id,
                model=(route.model_name or route.id) if route else selected_model,
                effort=execution.effort,
                timeout_seconds=execution.timeout_seconds,
                identity_mount=IDENTITY_MOUNT,
                report_mount=REPORT_MOUNT,
                repo_mount=REPO_MOUNT,
                credential_mounted=credential_mounted,
                credential_mode=(
                    effective_mount_mode(credential, source) if credential is not None else None
                ),
                endpoint=endpoint,
                endpoint_url=endpoint_url,
                command_timeout_ms=command_timeout_ms,
                harness_settings=harness_settings,
            )
        )
        return replace(
            spec,
            command=tuple(launch.argv),
            env={**env, **launch.env},
            env_from_files=dict(launch.env_from_files),
            stdin_files=tuple(launch.stdin_files),
            stdin_text=launch.stdin_text,
            transcript_path=launch.transcript_path,
        )

    async def _spec_for(self, attempt: Attempt) -> LaunchSpec | None:
        """Rebuild the launch spec from the database, for a collect after a restart."""

        def _fetch() -> tuple[Any, Any, Any, Any]:
            with self._uow_factory() as uow:
                execution = uow.executions.get(attempt.execution_id)
                task = uow.tasks.get(attempt.task_id)
                if execution is None or task is None:
                    return None, None, None, None
                stored = uow.contracts.get(task.id, execution.contract_version)
                repository = uow.repositories.get(task.repository_id)
                return execution, task, stored, repository

        execution, task, stored, repository = await self._db(_fetch)
        if execution is None or task is None or stored is None:
            return None
        try:
            return await self._build_spec(
                attempt, execution, task, stored.document, repository.url if repository else ""
            )
        except LaunchRefusedError:
            return await self._build_spec(
                attempt,
                execution,
                task,
                stored.document,
                repository.url if repository else "",
                credential_mounted=False,
            )

    @staticmethod
    def checkout_key(contract: dict[str, Any], external_id: str, repository_url: str = "") -> str:
        """One checkout lease per repository url and work branch (10)."""
        repository = contract.get("repository", {})
        url = repository_url or str(repository.get("url", "")) or str(repository.get("name", ""))
        branch = str(repository.get("work_branch") or f"crucible/{external_id}")
        return f"{url}#{branch}"

    def _harness_gate(self, execution: Execution) -> str | None:
        """07 and 25: the registry's answer for this execution's harness, as a refusal
        reason or None. Unknown and disabled names are refused with a wake."""
        if self._harnesses is None:
            return None
        with self._uow_factory() as uow:
            state = uow.harnesses.get(execution.harness)
        try:
            self._harnesses.resolve(execution.harness, gates=self._harness_gates, state=state)
        except HarnessUnavailableError as exc:
            return exc.reason
        return None

    async def _logins_in_progress(self) -> frozenset[str]:
        """The harnesses a login is running for, on any provider that can say (12, 25).
        A login is about to replace the credential, so a launch of that harness waits
        for it the way it waits for the per-harness cap. A listing that fails holds
        nothing back here; the provider refuses the seeding itself if a login is
        running when it gets there."""
        running: set[str] = set()
        for provider in self._providers.values():
            listing = getattr(provider, "logins_in_progress", None)
            if not callable(listing):
                continue
            try:
                running.update(await listing())
            except ProviderError as exc:
                log.warning("the running logins could not be listed: %s", exc)
        return frozenset(running)

    def _harness_busy(self, execution: Execution, routing_version: int | None = None) -> str | None:
        """05b: count credential holders against policy caps. A full harness waits."""
        with self._uow_factory() as uow:
            return self._harness_busy_in_uow(uow, execution, routing_version)

    def _harness_busy_in_uow(
        self, uow: UnitOfWork, execution: Execution, routing_version: int | None = None
    ) -> str | None:
        policy = execution.policy_snapshot or {}
        routing = load_attempt_routing(uow, policy, routing_version)
        selected = (
            routing.model(execution.model, execution.harness) if routing is not None else None
        )
        local_codex = (
            execution.harness == "codex" and selected is not None and selected.endpoint == "local"
        )
        if execution.harness in self._logins_now and not local_codex:
            return (
                f"a login for {execution.harness} is running and will replace its "
                "credential; the launch waits for it"
            )
        limit = int(
            (policy.get("concurrency", {}).get("per_harness") or {}).get(execution.harness, 1)
        )
        adapter = self._harnesses.get(execution.harness) if self._harnesses else None
        credential = adapter.credential_spec() if adapter is not None else None
        if local_codex:
            credential = None
        if credential is not None:
            source = getattr(self, "_credential_sources", {}).get(execution.harness)
            seed = source.mount_mode.value if source is not None and source.mount_mode else None
            mode = resolve_runtime_setting(
                uow,
                name=f"credentials.{execution.harness}.mount_mode",
                field="mount_mode",
                seed=seed,
                seed_source="environment",
                default=(
                    MountMode.RENEWER.value
                    if execution.harness == "codex"
                    else credential.minimum_mode.value
                ),
                applies="next launch",
            )
            source = CredentialSource(
                path=source.path if source is not None else "",
                mount_mode=MountMode(mode.value),
            )
            if effective_mount_mode(credential, source) is MountMode.RW_NARROW and not getattr(
                adapter, "parallel_attempts_safe", False
            ):
                limit = 1
        # An attempt holds its credential copy until collect has synced it back and
        # removed it, which is after `exited`. Parallel-safe adapters use the policy
        # cap; undeclared writable copies keep the conservative single slot.
        live = uow.attempts.list_in_states(list(CREDENTIAL_HOLDING_STATES))
        running = 0
        for other in live:
            other_execution = uow.executions.get(other.execution_id)
            if other_execution is not None and other_execution.harness == execution.harness:
                other_routing = load_attempt_routing(
                    uow, other_execution.policy_snapshot or {}, other.routing_version
                )
                other_model = (
                    other_routing.model(
                        other.selected_model or other_execution.model,
                        other.selected_harness or other_execution.harness,
                    )
                    if other_routing
                    else None
                )
                if execution.harness == "codex" and other_model and other_model.endpoint == "local":
                    continue
                running += 1
        # A writable credential forces the per-harness cap even for a local route. The
        # Hermes key is read-only, so its pool-wide limit remains the effective cap.
        if (
            selected is None
            or selected.endpoint == "subscription"
            or (credential is not None and credential.minimum_mode is MountMode.RW_NARROW)
        ) and running >= limit:
            return f"{running} of {limit} {execution.harness} worker(s) already running"
        if selected is not None:
            assert routing is not None
            pool_limit = self._pool_limit(uow, routing, selected.pool)
            if pool_limit is not None:
                pool_running = sum(1 for other in live if other.selected_pool == selected.pool)
                if pool_running >= pool_limit:
                    return (
                        f"{pool_running} of {pool_limit} {selected.pool} pool worker(s) "
                        "already running"
                    )
        return None

    @staticmethod
    def _pool_limit(uow: UnitOfWork, routing: RoutingPolicyV1, pool: str) -> int | None:
        """Hades #359: the strictest of the pool's max_concurrency in the task's routing
        snapshot and in the newest active version of the same routing policy, so a cap
        an operator lowers binds for tasks that carry an older routing version."""
        limits = [routing.pools[pool].max_concurrency]
        newest = max(
            (
                record
                for record in uow.routing_policies.list_versions(routing.name)
                if record.retired_at is None
            ),
            key=lambda record: record.version,
            default=None,
        )
        if newest is not None and newest.version != routing.version:
            current = RoutingPolicyV1.model_validate(newest.document)
            if pool in current.pools:
                limits.append(current.pools[pool].max_concurrency)
        caps = [limit for limit in limits if limit is not None]
        return min(caps) if caps else None

    def _checkout_lease_free(self, attempt_id: str, key: str) -> bool:
        with self._uow_factory() as uow:
            held = uow.leases.get_checkout_lease(key)
        return held is None or held.holder == attempt_id or held.expires_at <= self._clock.now()

    def _eligible_harnesses(
        self, *, needs_credential: bool = True, provider: str | None = None
    ) -> set[str] | None:
        if self._harnesses is None:
            return None
        # A provider that keeps the credentials itself (the Kubernetes provider's
        # service-owned Secrets, ADR 0015) has no directory to check here. Its seeding
        # refuses a missing or empty Secret with the reason, as it does for a pin.
        holder = self._providers.get(provider or "")
        secret_held = callable(getattr(holder, "read_credential_files", None))
        eligible: set[str] = set()
        with self._uow_factory() as uow:
            for name in self._harnesses.names():
                adapter = self._harnesses.get(name)
                if adapter is None:
                    continue
                # An endpoint-specific eligibility marker never admits a subscription
                # model without its own credential. Local Codex uses the gateway key.
                if name == "codex":
                    try:
                        self._harnesses.resolve(
                            name, gates=self._harness_gates, state=uow.harnesses.get(name)
                        )
                    except HarnessUnavailableError:
                        pass
                    else:
                        eligible.add("codex:local")
                source = self._credential_sources.get(name)
                credential = adapter.credential_spec()
                if (
                    needs_credential
                    and not secret_held
                    and credential is not None
                    and credential.required_for_launch
                    and (source is None or not Path(source.path).is_dir())
                ):
                    continue
                if (
                    needs_credential
                    and secret_held
                    and credential is not None
                    and credential.required_for_launch
                    and not _secret_holds(holder, name, credential)
                ):
                    continue
                try:
                    self._harnesses.resolve(
                        name, gates=self._harness_gates, state=uow.harnesses.get(name)
                    )
                except HarnessUnavailableError:
                    continue
                eligible.add(name)
        return eligible

    def _capacity_exclusions(self, uow: UnitOfWork, item: _Pending) -> set[tuple[str, str]]:
        excluded = set()
        for event in self._all_task_events(uow, item.task.id):
            model = event.payload.get("excluded_model")
            if event.payload.get("next_attempt_id") != item.attempt.id or not model:
                continue
            harness = event.payload.get("excluded_harness")
            if not harness and event.attempt_id:
                # Retry events saved before pair identity can be resolved from the
                # refusing attempt; never turn them into a model-wide exclusion.
                previous = uow.attempts.get(event.attempt_id)
                if previous is not None:
                    harness = previous.selected_harness
                    model = previous.selected_model or model
            if harness:
                excluded.add((str(harness), str(model)))
        return excluded

    def _selection_for(
        self,
        uow: UnitOfWork,
        item: _Pending,
        *,
        excluded_pools: set[str | None] | None = None,
        routing: RoutingPolicyV1 | None = None,
    ) -> Any:
        # hades #254: every attempt, a correction's or a retry's included, routes with
        # the routing version in force now unless the policy pins it.
        if routing is None:
            routing = load_attempt_routing(uow, item.execution.policy_snapshot or {})
        if routing is None:
            return None
        contract = TaskContractV1.model_validate(item.contract)
        request = contract.execution_request
        eligible = (
            None
            if request.pinned_model is not None
            else self._eligible_harnesses(
                needs_credential=request.provider.value != "fake",
                provider=request.provider.value,
            )
        )
        selection = select_model(
            uow,
            routing,
            tier=request.tier.value,
            project=item.task.project,
            provider=request.provider.value,
            now=self._clock.now(),
            contract=item.contract,
            policy_document=item.execution.policy_snapshot or {},
            eligible_harnesses=eligible,
            harnesses=self._harnesses,
            image_allowlist=[
                str(pattern)
                for pattern in (item.execution.policy_snapshot or {})
                .get("images", {})
                .get("allowlist", [])
            ],
            excluded_pools={pool for pool in (excluded_pools or set()) if pool is not None},
            excluded_routes=self._capacity_exclusions(uow, item),
            pinned_model=request.pinned_model,
            pinned_harness=request.pinned_harness.value if request.pinned_harness else None,
        )
        if request.provider.value == "fake" and request.image and selection.selected is not None:
            selection = replace(selection, image=request.image)
        return selection

    def _launch_selection(
        self, uow: UnitOfWork, item: _Pending
    ) -> tuple[RoutingPolicyV1 | None, Any]:
        """hades #254: how a pending attempt routes, shared by the launch preview and
        _route_pending so the two never disagree. The routing version is the one in force
        now (a pinned reference keeps its version), never one recorded on an earlier
        attempt, and the pools the attempt excludes, as a quota reroute leaves them, stay
        excluded."""
        routing = load_attempt_routing(uow, item.execution.policy_snapshot or {})
        selection = self._selection_for(
            uow,
            item,
            excluded_pools=set(item.attempt.routing_excluded_pools),
            routing=routing,
        )
        return routing, selection

    def _preview_route(self, item: _Pending) -> Any:
        with self._uow_factory() as uow:
            return self._launch_selection(uow, item)[1]

    @staticmethod
    def _selection_is_quota_blocked(selection: Any) -> bool:
        if selection is None:
            return False
        relevant: list[list[str]] = []
        disqualifying = (
            "not the operator pin",
            "harness does not match the operator pin",
            "model disabled",
            "harness disabled or has no credential",
            "capability ",
            "selected harness has no default image",
            "derived image ",
        )
        for candidate in selection.candidates:
            reasons = [str(reason) for reason in candidate.get("excluded", [])]
            if any(reason.startswith(disqualifying) for reason in reasons):
                continue
            relevant.append(reasons)
        return bool(relevant) and all(
            reasons
            and all(
                reason.startswith("pool exhausted until ") or reason == "pool is at its soft limit"
                for reason in reasons
            )
            for reasons in relevant
        )

    def _route_pending(self, item: _Pending, *, launch: bool = True) -> _Pending | None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(item.attempt.id, for_update=True)
            task = uow.tasks.get(item.task.id, for_update=True)
            execution = uow.executions.get(item.execution.id, for_update=True)
            assert attempt is not None and task is not None and execution is not None
            if attempt.state is not AttemptState.PENDING or task.state not in (
                TaskState.SCHEDULED,
                TaskState.RUNNING,
            ):
                return None
            current = replace(item, attempt=attempt, execution=execution, task=task)
            routing, selection = self._launch_selection(uow, current)
            if selection is None or selection.selected is None or selection.image is None:
                if self._selection_is_quota_blocked(selection):
                    self._enter_quota_wait(uow, task, attempt, execution, selection)
                else:
                    self._refuse_unroutable(uow, task, attempt, execution, selection)
                uow.commit()
                return None
            if not launch:
                return None
            assert routing is not None
            candidates = copy.deepcopy(list(selection.candidates))
            default_image = next(
                candidate["image"]
                for candidate in candidates
                if candidate["model"] == selection.selected.id
            )
            image_override = selection.image if selection.image != default_image else None
            skipped_busy: list[dict[str, str]] = []
            chosen = None
            chosen_image = None
            for candidate in candidates:
                if not candidate.get("eligible"):
                    continue
                model = routing.model(str(candidate["model"]), str(candidate["harness"]))
                assert model is not None
                execution.model = model.id
                execution.harness = model.harness
                execution.image = image_override or str(candidate["image"])
                busy = self._harness_busy_in_uow(uow, execution, routing.version)
                if busy is None:
                    chosen = model
                    chosen_image = execution.image
                    break
                candidate["busy"] = busy
                skipped_busy.append({"model": model.id, "harness": model.harness, "reason": busy})
            assert chosen is not None or skipped_busy
            attempt.ordered_candidates = candidates
            if chosen is None:
                latest = uow.events.latest_for_task_kind(
                    attempt.task_id, EventKind.HARNESS_LAUNCH_DEFERRED.value
                )
                if latest is None or latest.payload.get("attempt_id") != attempt.id:
                    detail = "; ".join(
                        f"{item['model']} on {item['harness']}: {item['reason']}"
                        for item in skipped_busy
                    )
                    record_event(
                        uow,
                        self._clock,
                        EventKind.HARNESS_LAUNCH_DEFERRED,
                        principal=PRINCIPAL_CRUCIBLE,
                        task_id=attempt.task_id,
                        execution_id=attempt.execution_id,
                        attempt_id=attempt.id,
                        payload={
                            "attempt_id": attempt.id,
                            "detail": detail,
                            "skipped_busy": skipped_busy,
                            "ordered_candidates": candidates,
                        },
                    )
                uow.attempts.save(attempt)
                uow.commit()
                return None
            assert chosen_image is not None
            attempt.selected_model = chosen.id
            attempt.selected_harness = chosen.harness
            attempt.selected_image = chosen_image
            attempt.selected_pool = chosen.pool
            attempt.routing_version = routing.version
            execution.model = chosen.id
            execution.harness = chosen.harness
            execution.image = chosen_image
            uow.attempts.save(attempt)
            uow.executions.save(execution)
            move_attempt(
                uow, self._clock, attempt, AttemptState.PREPARING, EventKind.ATTEMPT_PREPARING
            )
            if execution.state is ExecutionState.CREATED:
                move_execution(
                    uow, self._clock, execution, ExecutionState.ACTIVE, EventKind.EXECUTION_ACTIVE
                )
            if task.state is TaskState.SCHEDULED:
                move_task(
                    uow,
                    self._clock,
                    task,
                    TaskState.RUNNING,
                    EventKind.TASK_RUNNING,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={"attempt_number": attempt.number},
                )
            record_event(
                uow,
                self._clock,
                EventKind.ATTEMPT_ROUTED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "tier": item.contract["execution_request"]["tier"],
                    "model": chosen.id,
                    "harness": chosen.harness,
                    "image": chosen_image,
                    "pool": chosen.pool,
                    "routing_policy": {"name": routing.name, "version": routing.version},
                    "ordered_candidates": candidates,
                    "skipped_busy": skipped_busy,
                },
            )
            uow.commit()
            return replace(item, attempt=attempt, execution=execution, task=task)

    def _refuse_unroutable(
        self,
        uow: UnitOfWork,
        task: Task,
        attempt: Attempt,
        execution: Execution,
        selection: Any,
    ) -> None:
        reasons = sorted(
            {
                reason
                for candidate in (selection.candidates if selection else ())
                for reason in candidate.get("excluded", [])
            }
        )
        detail = "no eligible routing candidate"
        if reasons:
            detail += ": " + "; ".join(reasons)
        now = self._clock.now()
        capacity_refusal = any(
            candidate.get("capacity_refused") is True
            for candidate in (selection.candidates if selection else ())
        )
        attempt.exit_class = ExitClass.INFRASTRUCTURE if capacity_refusal else ExitClass.ENVIRONMENT
        if capacity_refusal:
            record_event(
                uow,
                self._clock,
                EventKind.ATTEMPT_EXITED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "exit_class": ExitClass.INFRASTRUCTURE.value,
                    "never_started": True,
                    "interruption_message": detail,
                },
            )
        attempt.ended_at = now
        attempt.ordered_candidates = list(selection.candidates) if selection else []
        uow.attempts.save(attempt)
        move_attempt(uow, self._clock, attempt, AttemptState.COLLECTED, EventKind.ATTEMPT_COLLECTED)
        move_attempt(uow, self._clock, attempt, AttemptState.FAILED, EventKind.ATTEMPT_FAILED)
        if execution.state is ExecutionState.CREATED:
            move_execution(
                uow, self._clock, execution, ExecutionState.ACTIVE, EventKind.EXECUTION_ACTIVE
            )
        move_execution(
            uow,
            self._clock,
            execution,
            ExecutionState.FAILED,
            EventKind.EXECUTION_FAILED,
            payload={"reason": detail, "ordered_candidates": attempt.ordered_candidates},
        )
        if task.state is TaskState.SCHEDULED:
            move_task(uow, self._clock, task, TaskState.RUNNING, EventKind.TASK_RUNNING)
        if "no task-specific check" in reasons:
            move_task(
                uow,
                self._clock,
                task,
                TaskState.BLOCKED,
                EventKind.TASK_BLOCKED,
                payload={"reason": detail},
            )
            open_escalation(
                uow, self._clock, task=task, attempt_id=attempt.id, question=detail, summary=detail
            )
        else:
            self._task_reported(uow, task, attempt, attempt.exit_class, {}, wake_summary=detail)

    def _needs_gate_probe(self, item: _Pending) -> bool:
        if item.execution.role is ExecutionRole.REVIEW:
            return False
        with self._uow_factory() as uow:
            # A probe refusal never launched a worker. A rescheduled refusal must
            # prove its checks again, even when the operator kept the same contract.
            previous = [
                (attempt, execution)
                for execution in uow.executions.list_for_task(item.task.id)
                if execution.role is not ExecutionRole.REVIEW
                for attempt in uow.attempts.list_for_execution(execution.id)
                if attempt.id != item.attempt.id and attempt.started_at is not None
            ]
            if not previous:
                return True
            _, execution = max(previous, key=lambda pair: pair[0].created_at)
            contract = uow.contracts.get(item.task.id, execution.contract_version)
            return contract is None or contract.document.get("required_verification") != (
                item.contract.get("required_verification")
            )

    async def _probe_before_prepare(
        self, item: _Pending, provider: ExecutionProvider, spec: LaunchSpec
    ) -> bool:
        commands = task_specific_checks(item.contract, spec.policy)
        checks = [
            check
            for check in item.contract.get("required_verification", [])
            if check.get("kind", "command") == "command"
            and " ".join(check.get("command", "").split()) in commands
        ]
        if not checks or not await self._db(partial(self._needs_gate_probe, item)):
            return True
        error = ""
        rows: tuple[VerificationRun, ...] | None = ()
        token = await checkout_token_for(self._github, item.repository)
        try:
            rows = await provider.probe_checks(
                spec,
                checks,
                checkout_token=token,
                cancelled=self._cancel_check(item.task.id),
            )
            if rows is not None and (
                len(rows) != len(checks)
                or any(
                    row.id != check["id"] or row.command != check["command"] or not row.ran
                    for row, check in zip(rows, checks, strict=False)
                )
            ):
                error = "probe returned incomplete or invalid results"
        except (LaunchCancelledError, LaunchWaitError):
            # hades #423: a probe the quota had no room for is launched again later, not
            # recorded as a check that cannot run.
            raise
        except ProviderError as exc:
            error = str(exc)
        finally:
            await release_checkout_token(self._github, token)
        if await self._db(partial(self._settle_if_cancelled, item.attempt.id, "gate_probe")):
            return False
        return await self._db(
            partial(self._record_gate_probe, item.attempt.id, checks, rows, error)
        )

    def _record_gate_probe(
        self,
        attempt_id: str,
        checks: list[dict[str, Any]],
        rows: tuple[VerificationRun, ...] | None,
        error: str,
    ) -> bool:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            task = uow.tasks.get(attempt.task_id, for_update=True)
            assert task is not None
            reason = ""
            detail = ""
            if error:
                reason = "check_cannot_run"
                detail = f"{', '.join(check['id'] for check in checks)}: {error}"
            elif rows is not None:
                missing = [row for row in rows if row.exit_code == 127]
                if missing:
                    reason = "check_cannot_run"
                    detail = "; ".join(
                        f"{row.id}: exit 127: {row.log_tail or 'program not found'}"
                        for row in missing
                    )
                elif all(
                    row.exit_code == int(check.get("expect_exit", 0))
                    for row, check in zip(rows, checks, strict=True)
                ):
                    reason = "gate_proves_nothing"
                    detail = "; ".join(
                        f"{row.id} passes on the unchanged repo (add a check that fails, "
                        "for example a new test file)"
                        for row in rows
                    )
            by_id = {row.id: row for row in rows or ()}
            for check in checks:
                row = by_id.get(check["id"])
                payload = {
                    "id": check["id"],
                    "command": check["command"],
                    "exit": row.exit_code if row else None,
                    "detail": error or (row.log_tail if row else "probe not supported, skipped"),
                }
                uow.evidence.add(
                    EvidenceRecord(
                        id=None,
                        attempt_id=attempt.id,
                        task_id=task.id,
                        kind=EvidenceKind.GATE_PROBE.value,
                        source=EvidenceSource.CRUCIBLE.value,
                        observed_at=self._clock.now(),
                        verified=True,
                        payload=payload,
                    )
                )
            if reason:
                attempt.termination_reason = reason
                attempt.ended_at = self._clock.now()
                attempt.logs_drained_at = attempt.ended_at
                # No workspace exists, and no worker budget or quota has been spent.
                attempt.cleaned_up_at = attempt.ended_at
                attempt.exit_class = ExitClass.BLOCKED
                move_attempt(
                    uow, self._clock, attempt, AttemptState.COLLECTED, EventKind.ATTEMPT_COLLECTED
                )
                move_attempt(
                    uow, self._clock, attempt, AttemptState.BLOCKED, EventKind.ATTEMPT_BLOCKED
                )
                # hades #412: emit attempt_exited with never_started so that a correction
                # on a probe-blocked task passes _unpublished_bundle_problem (#346 exemption)
                record_event(
                    uow,
                    self._clock,
                    EventKind.ATTEMPT_EXITED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={"exit_class": ExitClass.BLOCKED.value, "never_started": True},
                )
                self._release_checkout_leases(uow, attempt)
                move_task(
                    uow,
                    self._clock,
                    task,
                    TaskState.BLOCKED,
                    EventKind.TASK_BLOCKED,
                    attempt_id=attempt.id,
                    execution_id=attempt.execution_id,
                    payload={"reason": reason, "detail": detail},
                )
                open_escalation(
                    uow,
                    self._clock,
                    task=task,
                    attempt_id=attempt.id,
                    question=detail,
                    summary=f"{reason}: {detail}",
                )
            uow.commit()
            return not reason

    async def _prepare(
        self, provider: ExecutionProvider, spec: LaunchSpec, repository: Repository | None
    ) -> Workspace:
        """08, ADR 0019: build the checkout. A private repository's read-only checkout
        token is minted here, handed to the provider for the preparation step alone,
        and revoked and emptied the moment `prepare` returns or raises: the worker that
        launches next never had it, and nothing in this process keeps it. The provider
        asks whether the task was cancelled before each of its steps (hades #189)."""

        token = await checkout_token_for(self._github, repository)
        try:
            return await provider.prepare(
                spec, checkout_token=token, cancelled=self._cancel_check(spec.task_id)
            )
        finally:
            await release_checkout_token(self._github, token)

    def _cancel_check(self, task_id: str) -> CancelCheck:
        """What a provider asks while it prepares or launches. A look that fails is not
        a cancel: the provider asks again on its next poll, and a long clone is not
        failed by one database blip."""

        async def cancelled() -> bool:
            try:
                return await self._db(partial(self._task_cancelled, task_id))
            except Exception:
                log.warning("could not read whether the task was cancelled", exc_info=True)
                return False

        return cancelled

    def _task_cancelled(self, task_id: str) -> bool:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            return task is None or task.state in ENDS_ATTEMPTS

    def _settle_if_cancelled(self, attempt_id: str, stage: str) -> bool:
        """hades #189: when the attempt's task was cancelled, end the attempt here, before
        any worker exists, and let the task become cancelled. True when it did."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            task = uow.tasks.get(attempt.task_id, for_update=True)
            assert task is not None
            if task.state not in ENDS_ATTEMPTS:
                return False
            if attempt.state in (AttemptState.PREPARING, AttemptState.LAUNCHING):
                self._end_cancelled_launch(uow, attempt, task, stage)
            uow.commit()
            return True

    def _end_cancelled_launch(
        self, uow: UnitOfWork, attempt: Attempt, task: Task, stage: str
    ) -> None:
        attempt.exit_class = ExitClass.KILLED
        attempt.termination_reason = TERMINATION_CANCEL
        attempt.ended_at = self._clock.now()
        move_attempt(
            uow,
            self._clock,
            attempt,
            AttemptState.COLLECTED,
            EventKind.ATTEMPT_COLLECTED,
            payload={"reason": "task cancelled during launch", "stage": stage},
        )
        move_attempt(
            uow,
            self._clock,
            attempt,
            AttemptState.FAILED,
            EventKind.ATTEMPT_FAILED,
            payload={"exit_class": ExitClass.KILLED.value, "stage": stage},
        )
        self._release_checkout_leases(uow, attempt)
        self._finish_cancelling(uow, task)

    async def _discard(
        self, provider: ExecutionProvider, ws: Workspace | None, spec: LaunchSpec | None
    ) -> None:
        """12: an attempt that will never be collected still had a credential copy seeded
        for it; the provider removes it now, because cleanup only visits attempts whose
        logs were drained and retention never looks at a workspace it did not."""
        if ws is None:
            return
        try:
            await provider.discard(ws, spec)
        except Exception:  # the attempt still ends; the leak is logged, not hidden
            log.exception("credential copy discard failed", extra={"attempt_id": ws.attempt_id})

    def _mark_preparing(self, attempt_id: str) -> bool:
        """Begin the launch, unless the task was cancelled after the attempt was listed."""
        with self._fenced() as uow:
            loaded = self._pending_for_preparing(uow, attempt_id)
            if loaded is None:
                return False
            self._record_review_routing_version(uow, loaded[0], loaded[2])
            self._move_to_preparing(uow, *loaded)
            uow.commit()
            return True

    def _mark_review_preparing(
        self, attempt_id: str
    ) -> tuple[Attempt | None, str | None, str | None]:
        """Begin a review launch, holding its pool slot (hades #359). The caps are
        checked in the same fenced transaction that records the pool and moves the
        attempt to preparing, so the next launch counts this one. Returns the attempt
        when the launch began, why a cap held it back, and why the launch is refused:
        a review is not routed, so its model must be enabled in the routing version
        recorded on it and paired there with the harness the review launches."""
        with self._fenced() as uow:
            loaded = self._pending_for_preparing(uow, attempt_id)
            if loaded is None:
                return None, None, None
            attempt, task, execution = loaded
            self._record_review_routing_version(uow, attempt, execution)
            routing = load_attempt_routing(
                uow, execution.policy_snapshot or {}, attempt.routing_version
            )
            # Resolve the exact route; models can be shared by several harnesses.
            entry = (
                routing.model(execution.model, execution.harness) if routing is not None else None
            )
            refusal = self._review_route_refusal(routing, entry, execution)
            if refusal is None:
                busy = self._harness_busy_in_uow(uow, execution, attempt.routing_version)
                if busy is not None:
                    return None, busy, None
            attempt.selected_model = execution.model
            attempt.selected_harness = execution.harness
            attempt.selected_image = execution.image
            # A refused review holds no pool: it ends before it launches, and an
            # unverified route must never be charged or marked exhausted.
            attempt.selected_pool = entry.pool if entry is not None and refusal is None else None
            uow.attempts.save(attempt)
            # The same path an environment failure at prepare takes: the attempt and the
            # execution become active first, so a refusal can end them.
            self._move_to_preparing(uow, attempt, task, execution)
            uow.commit()
            return attempt, None, refusal

    @staticmethod
    def _record_review_routing_version(
        uow: UnitOfWork, attempt: Attempt, execution: Execution
    ) -> None:
        """hades #254: a review is not routed, so the routing version it launches with
        is recorded as it begins; the spec, the reservation, the harness count and the
        exit then read this version, not a newer one."""
        if execution.role is ExecutionRole.REVIEW and attempt.routing_version is None:
            attempt.routing_version = current_routing_version(uow, execution.policy_snapshot or {})

    @staticmethod
    def _review_route_refusal(
        routing: RoutingPolicyV1 | None, entry: Any, execution: Execution
    ) -> str | None:
        """Why a review may not launch with its model and harness, if it may not."""
        if routing is None:
            return None
        where = f"routing policy {routing.name}/{routing.version}"
        if entry is None:
            return (
                f"model {execution.model} is not paired with harness {execution.harness} in {where}"
            )
        if not entry.enabled:
            reason = f": {entry.disabled_reason}" if entry.disabled_reason else ""
            return f"model {entry.id} is disabled in {where}{reason}"
        if entry.harness != execution.harness:
            return (
                f"model {entry.id} is paired with harness {entry.harness} in {where}, "
                f"not with {execution.harness}"
            )
        return None

    @staticmethod
    def _pending_for_preparing(
        uow: UnitOfWork, attempt_id: str
    ) -> tuple[Attempt, Task, Execution] | None:
        """The attempt, task and execution locked, if the attempt may still begin."""
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert attempt is not None
        task = uow.tasks.get(attempt.task_id, for_update=True)
        execution = uow.executions.get(attempt.execution_id, for_update=True)
        assert task is not None and execution is not None
        allowed = (
            (TaskState.AWAITING_INTERNAL_REVIEW,)
            if execution.role is ExecutionRole.REVIEW
            else (TaskState.SCHEDULED, TaskState.RUNNING)
        )
        if attempt.state is not AttemptState.PENDING or task.state not in allowed:
            return None
        return attempt, task, execution

    def _move_to_preparing(
        self, uow: UnitOfWork, attempt: Attempt, task: Task, execution: Execution
    ) -> None:
        move_attempt(uow, self._clock, attempt, AttemptState.PREPARING, EventKind.ATTEMPT_PREPARING)
        if execution.state is ExecutionState.CREATED:
            move_execution(
                uow, self._clock, execution, ExecutionState.ACTIVE, EventKind.EXECUTION_ACTIVE
            )
        if execution.role is not ExecutionRole.REVIEW and task.state is TaskState.SCHEDULED:
            move_task(
                uow,
                self._clock,
                task,
                TaskState.RUNNING,
                EventKind.TASK_RUNNING,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={"attempt_number": attempt.number},
            )

    def _release_attempt_checkout(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            if attempt is not None:
                self._release_checkout_leases(uow, attempt)
            uow.commit()

    def _mark_launching(self, attempt_id: str, ws: Workspace) -> bool:
        """Reserve the quota pool and move to launching in one fenced transaction (05b).

        A pool that crossed its soft limit since submit refuses the attempt with class
        quota_exhausted and wakes Foundry."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            execution = uow.executions.get(attempt.execution_id)
            assert execution is not None
            if attempt.state is not AttemptState.PREPARING:
                # Another supervisor already ended it (a stranded launch, 10).
                return False
            current = uow.tasks.get(attempt.task_id, for_update=True)
            assert current is not None
            if current.state in ENDS_ATTEMPTS:
                # hades #189: the last look before a worker starts.
                self._end_cancelled_launch(uow, attempt, current, "launch")
                uow.commit()
                return False
            reservation = reserve(
                uow,
                execution.policy_snapshot or {},
                harness=execution.harness,
                model_id=execution.model,
                now=self._clock.now(),
                routing_version=attempt.routing_version,
            )
            if not reservation.ok:
                task = uow.tasks.get(attempt.task_id, for_update=True)
                assert task is not None
                attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
                attempt.ended_at = self._clock.now()
                record_event(
                    uow,
                    self._clock,
                    EventKind.QUOTA_EXHAUSTED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={"pool": reservation.pool, "detail": reservation.detail},
                )
                move_attempt(
                    uow,
                    self._clock,
                    attempt,
                    AttemptState.COLLECTED,
                    EventKind.ATTEMPT_COLLECTED,
                    payload={"exit_class": ExitClass.QUOTA_EXHAUSTED.value},
                )
                if execution.role is ExecutionRole.REVIEW:
                    self._classify_and_finish(uow, attempt, None)
                    uow.commit()
                    return False
                move_attempt(
                    uow,
                    self._clock,
                    attempt,
                    AttemptState.FAILED,
                    EventKind.ATTEMPT_FAILED,
                    payload={"exit_class": ExitClass.QUOTA_EXHAUSTED.value, "phase": "reserve"},
                )
                self._release_checkout_leases(uow, attempt)
                self._record_bare_evidence(uow, attempt)
                self._handle_quota_exit(uow, task, execution, attempt, source="reserve")
                uow.commit()
                return False
            self._ensure_metrics(uow, attempt, execution, reservation)
            record_event(
                uow,
                self._clock,
                EventKind.QUOTA_RESERVED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={"pool": reservation.pool, "detail": reservation.detail},
            )
            attempt.workspace_path = ws.checkout_path.removesuffix("/repo")
            attempt.identity_sha256 = ws.identity_sha256 or attempt.identity_sha256
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.LAUNCHING,
                EventKind.ATTEMPT_LAUNCHING,
                payload={
                    "workspace": attempt.workspace_path,
                    "model": attempt.selected_model,
                    "harness": attempt.selected_harness,
                    "image": attempt.selected_image,
                    "pool": attempt.selected_pool,
                    "ordered_candidates": list(attempt.ordered_candidates),
                },
            )
            uow.commit()
            return True

    def _record_launch_workspace(self, attempt_id: str, ws: Workspace) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            attempt.workspace_path = ws.checkout_path.removesuffix("/repo")
            attempt.identity_sha256 = ws.identity_sha256 or attempt.identity_sha256
            uow.attempts.save(attempt)
            uow.commit()

    def _ensure_metrics(
        self, uow: UnitOfWork, attempt: Attempt, execution: Execution, reservation: Any
    ) -> None:
        if uow.attempt_metrics.get(attempt.id) is not None:
            return
        uow.attempt_metrics.put(
            AttemptMetrics(
                attempt_id=attempt.id,
                task_id=attempt.task_id,
                model=execution.model,
                harness=execution.harness,
                endpoint_kind=reservation.endpoint_kind,
                pool=reservation.pool,
                cost_source="none",
                created_at=self._clock.now(),
            )
        )

    def _mark_running(self, attempt_id: str, handle: Handle) -> bool:
        """Record the worker running, unless the task was cancelled while it started
        (hades #189): then the attempt ends killed at stage launch in this same
        transaction, it is never running, and False tells the caller to stop the worker."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            execution = uow.executions.get(attempt.execution_id)
            assert execution is not None
            task = uow.tasks.get(attempt.task_id, for_update=True)
            assert task is not None
            if task.state in ENDS_ATTEMPTS:
                if attempt.state is AttemptState.LAUNCHING:
                    self._end_cancelled_launch(uow, attempt, task, "launch")
                uow.commit()
                return False
            now = self._clock.now()
            if handle.image_digest and attempt.image_digest != handle.image_digest:
                # 13: every attempt records the image digest it ran, resolved at launch.
                attempt.image_digest = handle.image_digest
                record_event(
                    uow,
                    self._clock,
                    EventKind.IMAGE_RESOLVED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={"image": execution.image, "digest": handle.image_digest},
                )
            attempt.handle = handle.ref
            attempt.started_at = now
            attempt.timeout_at = now + timedelta(seconds=execution.timeout_seconds)
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.RUNNING,
                EventKind.ATTEMPT_RUNNING,
                payload={"handle": handle.ref, "timeout_at": attempt.timeout_at.isoformat()},
            )
            assert self.fenced_token is not None
            uow.leases.upsert_attempt_lease(
                attempt.id, self.holder, self.fenced_token, now, self.attempt_lease_ttl_seconds
            )
            uow.heartbeats.append(
                Heartbeat(
                    id=None,
                    attempt_id=attempt.id,
                    ts=now,
                    signal="container_running",
                    detail={"handle": handle.ref},
                )
            )
            uow.commit()
            return True

    def _environment_failure(self, attempt_id: str, stage: str, detail: str) -> None:
        """A prepare or launch the provider could not carry out (a create the API server
        refused, a preparer that failed). hades #423: the provider's message is the
        attempt's recorded failure reason and the wake's summary, so the record says
        more than the exit class."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            attempt.exit_class = ExitClass.ENVIRONMENT
            attempt.ended_at = self._clock.now()
            attempt.termination_detail = redact(f"{stage}: {detail}")[:1000]
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={
                    "stage": stage,
                    "detail": redact(detail)[:1000],
                    "exit_class": ExitClass.ENVIRONMENT,
                },
            )
            self._record_bare_evidence(uow, attempt)
            self._classify_and_finish(
                uow,
                attempt,
                None,
                wake_summary=(
                    f"attempt {attempt.number} ended environment at {stage}: "
                    f"{attempt.termination_detail}; no retry remaining"
                ),
            )
            uow.commit()

    def _return_to_pending(self, attempt_id: str, stage: str, detail: str) -> None:
        """hades #423: the provider could not take the attempt's Pod right now (the
        namespace quota refused the gate probe, the preparer or the worker). Nothing
        ran, so nothing is recorded against the attempt: it goes back to pending with
        the reason, the task back to scheduled, the checkout lease is released, and a
        later tick launches it. A task cancelled meanwhile ends the attempt as a cancel
        does."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            task = uow.tasks.get(attempt.task_id, for_update=True)
            assert task is not None
            if attempt.state not in (AttemptState.PREPARING, AttemptState.LAUNCHING):
                # Another supervisor already settled it (a stranded launch, 10).
                return
            if task.state in ENDS_ATTEMPTS:
                self._end_cancelled_launch(uow, attempt, task, stage)
                uow.commit()
                return
            reason = redact(detail)[:1000]
            attempt.workspace_path = None
            attempt.identity_sha256 = None
            attempt.handle = None
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.PENDING,
                EventKind.HARNESS_LAUNCH_DEFERRED,
                payload={
                    "attempt_id": attempt.id,
                    "stage": stage,
                    "detail": reason,
                    "quota_wait": True,
                },
            )
            self._release_checkout_leases(uow, attempt)
            execution = uow.executions.get(attempt.execution_id)
            if (
                task.state is TaskState.RUNNING
                and execution is not None
                and execution.role is not ExecutionRole.REVIEW
            ):
                # The task returns to the queue where it stood: the queue reads the
                # newest scheduling event, and a head adoption's resume flag rides along.
                scheduled = uow.events.latest_for_task_kind(task.id, EventKind.TASK_SCHEDULED.value)
                move_task(
                    uow,
                    self._clock,
                    task,
                    TaskState.SCHEDULED,
                    EventKind.TASK_SCHEDULED,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={
                        "reason": "quota_wait",
                        "stage": stage,
                        "detail": reason,
                        **(
                            {"resume_from_work_branch": True}
                            if scheduled is not None
                            and scheduled.payload.get("resume_from_work_branch") is True
                            else {}
                        ),
                    },
                )
            log.info(
                "launch waits for room; the attempt is pending again",
                extra={"stage": stage, "detail": reason},
            )
            uow.commit()

    def _start_failure(self, attempt_id: str, observation: Observation) -> None:
        """The launch's runtime refused to start the worker (hades #346). The attempt is
        an infrastructure interruption with the runtime's message on its exit event and
        an evidence row, and the retry budget decides what happens next."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            attempt.exit_class = ExitClass.INFRASTRUCTURE
            attempt.exit_code = observation.exit_code
            attempt.ended_at = self._clock.now()
            message = redact(observation.container_message or "")
            events = self._pod_event_rows(observation)
            record_event(
                uow,
                self._clock,
                EventKind.ATTEMPT_EXITED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt.id,
                payload={
                    "exit_code": observation.exit_code,
                    "exit_class": ExitClass.INFRASTRUCTURE.value,
                    "never_started": True,
                    "no_commits": True,
                    "interruption_message": f"the worker never started: {message}",
                    "container_message": message,
                    "pod_events": events,
                },
            )
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={
                    "stage": "launch",
                    "detail": f"the worker never started: {message}"[:1000],
                    "exit_class": ExitClass.INFRASTRUCTURE,
                },
            )
            self._record_start_failure_evidence(uow, attempt, observation)
            self._record_bare_evidence(uow, attempt)
            self._classify_and_finish(uow, attempt, None)
            uow.commit()

    @staticmethod
    def _pod_event_rows(observation: Observation) -> list[dict[str, Any]]:
        return [
            {**row, "message": redact(str(row.get("message") or ""))}
            for row in observation.pod_events
        ]

    def _record_start_failure_evidence(
        self, uow: UnitOfWork, attempt: Attempt, observation: Observation
    ) -> None:
        uow.evidence.add(
            EvidenceRecord(
                id=None,
                attempt_id=attempt.id,
                task_id=attempt.task_id,
                kind=EvidenceKind.EXIT_INFO.value,
                observed_at=self._clock.now(),
                source=EvidenceSource.CRUCIBLE.value,
                verified=True,
                payload={
                    "exit_class": ExitClass.INFRASTRUCTURE.value,
                    "never_started": True,
                    "detail": observation.detail,
                    "container_message": redact(observation.container_message or ""),
                    "pod_events": self._pod_event_rows(observation),
                },
            )
        )

    def _defer_launch(self, attempt_id: str, detail: str) -> None:
        """The attempt stays pending; one event says why it did not launch this tick."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            assert attempt is not None
            latest = uow.events.latest_for_task_kind(
                attempt.task_id, EventKind.HARNESS_LAUNCH_DEFERRED.value
            )
            if latest is not None and latest.payload.get("attempt_id") == attempt.id:
                return
            record_event(
                uow,
                self._clock,
                EventKind.HARNESS_LAUNCH_DEFERRED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt.id,
                payload={"attempt_id": attempt.id, "detail": detail},
            )
            uow.commit()

    def _refuse_launch(self, attempt_id: str, stage: str, detail: str) -> None:
        """07, 13, 25: an unknown or disabled harness, a version outside the tested
        range, or a missing credential. A refusal, never a warning: the attempt ends as
        an environment failure that does not retry, and Foundry is woken."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            task = uow.tasks.get(attempt.task_id, for_update=True)
            execution = uow.executions.get(attempt.execution_id)
            assert task is not None and execution is not None
            attempt.termination_reason = TERMINATION_REFUSED
            record_event(
                uow,
                self._clock,
                EventKind.HARNESS_REFUSED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt.id,
                payload={"harness": execution.harness, "stage": stage, "detail": detail[:1000]},
            )
            create_wake(
                uow,
                self._clock,
                principal_id=task.principal_id,
                reason=WakeReason.HARNESS_UNAVAILABLE,
                summary=f"launch refused for harness {execution.harness}: {detail[:500]}",
                task=task,
                attempt_id=attempt.id,
                extra_links={"harnesses": "/v1/harnesses"},
            )
            record_launch_outcome(
                uow,
                self._clock,
                name=execution.harness,
                outcome="refused",
                at=self._clock.now(),
            )
            attempt.exit_class = ExitClass.ENVIRONMENT
            attempt.ended_at = self._clock.now()
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={"stage": stage, "detail": detail, "exit_class": ExitClass.ENVIRONMENT},
            )
            self._record_bare_evidence(uow, attempt)
            self._classify_and_finish(uow, attempt, None)
            uow.commit()

    def _record_bare_evidence(self, uow: UnitOfWork, attempt: Attempt) -> None:
        """An attempt that produced nothing still records its exit, so the gates that
        read it fail rather than wait (09, 11)."""
        task = uow.tasks.get(attempt.task_id)
        if task is None:
            return
        execution = uow.executions.get(attempt.execution_id)
        if execution is not None and execution.role is ExecutionRole.REVIEW:
            return
        record_collection_evidence(
            uow,
            self._clock,
            self._artifacts,
            attempt=attempt,
            task=task,
            outputs=CollectedOutputs(report=None, report_raw=None, blocked_md=None),
            claim=None,
            claim_parsed_ok=False,
            parse_errors=[],
        )
        self._record_wall_time(uow, attempt)

    # ----- checkout lease, workspace, logs, cleanup, retention -------------

    def _take_checkout_lease(self, attempt_id: str, key: str) -> bool:
        """One attempt at a time per repository and work branch (10). The holder is the
        attempt, so a takeover by another supervisor does not hand the checkout over."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            assert attempt is not None and self.fenced_token is not None
            lease = uow.leases.acquire_checkout_lease(
                key,
                attempt_id,
                self.fenced_token,
                self._clock.now(),
                self.checkout_lease_ttl_seconds,
            )
            if lease is None:
                held = uow.leases.get_checkout_lease(key)
                existing = held.holder if held else "unknown"
                last = uow.events.latest_for_task_kind(
                    attempt.task_id, EventKind.CHECKOUT_LEASE_DENIED
                )
                if last is None or last.payload.get("attempt_id") != attempt_id:
                    record_event(
                        uow,
                        self._clock,
                        EventKind.CHECKOUT_LEASE_DENIED,
                        principal=PRINCIPAL_CRUCIBLE,
                        task_id=attempt.task_id,
                        execution_id=attempt.execution_id,
                        attempt_id=attempt_id,
                        payload={"key": key, "held_by": existing, "attempt_id": attempt_id},
                    )
                uow.commit()
                return False
            record_event(
                uow,
                self._clock,
                EventKind.CHECKOUT_LEASE_TAKEN,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt_id,
                payload={"key": key},
            )
            uow.commit()
            return True

    def _release_checkout_leases(self, uow: UnitOfWork, attempt: Attempt) -> None:
        for lease in uow.leases.list_checkout_leases():
            if lease.holder != attempt.id:
                continue
            if uow.leases.release_checkout_lease(lease.key, attempt.id):
                record_event(
                    uow,
                    self._clock,
                    EventKind.CHECKOUT_LEASE_RELEASED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={"key": lease.key},
                )

    def _record_prepared(
        self, attempt_id: str, ws: Workspace, effective: dict[str, Any] | None = None
    ) -> None:
        """08 wants it recorded as an event which branch the checkout started from.
        Hades #388: the effective model settings the worker is launched with are recorded
        on the attempt here, once; a value already recorded is never replaced."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            assert attempt is not None
            if effective is not None and attempt.effective_settings is None:
                attempt.effective_settings = dict(effective)
                uow.attempts.save(attempt)
            record_event(
                uow,
                self._clock,
                EventKind.WORKSPACE_PREPARED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt_id,
                payload={
                    "work_branch": ws.work_branch,
                    "started_from": ws.started_from,
                    "identity_sha256": ws.identity_sha256,
                },
            )
            uow.commit()

    async def _pull_logs(
        self, attempt: Attempt, provider: ExecutionProvider, handle: Handle
    ) -> int:
        """One log pull, appended and the resume position advanced (10)."""
        with self._uow_factory() as uow:
            index = uow.logs.count_for_attempt(attempt.id)
        offset = LogOffset(
            index=index,
            timestamp=attempt.log_resume_ts.isoformat() if attempt.log_resume_ts else None,
            line_sha256=attempt.log_resume_sha256,
            occurrence=attempt.log_resume_occurrence,
        )
        try:
            chunks = await provider.logs(handle, offset)
        except ProviderError as exc:
            log.warning("log pull failed (%s); the next tick tries again", exc)
            return 0
        if not chunks:
            return 0
        return int(await self._db(partial(self._store_logs, attempt.id, tuple(chunks))))

    async def _drain_logs(
        self, attempt: Attempt, provider: ExecutionProvider, handle: Handle
    ) -> None:
        """The final drain: pull until a pull brings nothing (08, 10). A provider may
        bound one pull (the Kubernetes provider reads a few MiB at a time, issue 63), so
        a single pull at exit could leave the end of the log behind. Each round resumes
        from the position the last one stored, and the rounds are capped so a log that
        never stops growing cannot hold the tick."""
        current = attempt
        for _ in range(FINAL_DRAIN_PULLS):
            if not await self._pull_logs(current, provider, handle):
                return
            fresh: Attempt | None = await self._db(partial(self._fresh_attempt, attempt.id))
            current = fresh or current
        log.warning(
            "the final log drain stopped after %d pulls with more still arriving",
            FINAL_DRAIN_PULLS,
            extra={"attempt_id": attempt.id},
        )

    def _fresh_attempt(self, attempt_id: str) -> Attempt | None:
        with self._uow_factory() as uow:
            return uow.attempts.get(attempt_id)

    def _probe_expected(self, uow: UnitOfWork, attempt: Attempt) -> bool:
        """hades #425: whether the launch wrapper ran the egress probe for this attempt,
        by the rule the providers launch under: the execution's policy snapshot gives the
        worker a network and the contract does not take it away. An attempt whose
        execution or contract cannot be found is not believed either."""
        execution = uow.executions.get(attempt.execution_id)
        if execution is None:
            return False
        contract = uow.contracts.get(attempt.task_id, execution.contract_version)
        if contract is None:
            return False
        return probe_expected(execution.policy_snapshot, contract.document)

    def _store_logs(self, attempt_id: str, chunks: tuple[LogChunk, ...]) -> int:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            if attempt is None:
                return 0
            offset = uow.logs.last_offset(attempt_id)
            stored = 0
            # Whether this attempt runs the egress probe, looked up once and only when a
            # chunk carries the marker (hades #425).
            wanted: bool | None = None
            for chunk in chunks:
                if not chunk.content:
                    continue
                # 12: provider log capture passes through the redaction filter before
                # storage. The resume position keeps the hash of the raw line, which is
                # what the daemon's stream is compared against on the next pull.
                text = chunk.content.decode("utf-8", "replace")
                cleaned = redact(text)
                # A container may write arbitrary bytes. Always store the decoded and
                # redacted form so every downstream log reader receives valid UTF-8.
                content = cleaned.encode("utf-8")
                if attempt.egress_probe is None and PROBE_MARKER in cleaned:
                    # hades #425: the launch wrapper's one probe line, kept on the attempt
                    # the first time it is seen; the harness's later output never replaces
                    # it. The line is the worker's word (S4): it is read only when this
                    # attempt runs the probe at all, its first marker line is the only one
                    # read, and it is sized before it is parsed. A rejected line is
                    # recorded as the rejection, so no later line is parsed either.
                    if wanted is None:
                        wanted = self._probe_expected(uow, attempt)
                    if not wanted:
                        log.warning(
                            "egress probe line ignored: this attempt has no network, so "
                            "no probe was run and the line is the harness's",
                            extra={"attempt_id": attempt_id},
                        )
                    else:
                        probe, rejection = find_probe_line(cleaned)
                        recorded_at = self._clock.now().isoformat()
                        if rejection is not None:
                            attempt.egress_probe = {
                                **rejected_record(rejection),
                                "recorded_at": recorded_at,
                            }
                            log.warning(
                                "egress probe line rejected: %s",
                                rejection,
                                extra={"attempt_id": attempt_id},
                            )
                        elif probe is not None:
                            attempt.egress_probe = {**probe, "recorded_at": recorded_at}
                            unreachable = unreachable_hosts(probe)
                            log.info(
                                "egress probe: %d host(s) checked, unreachable: %s",
                                len(probe["hosts"]),
                                ", ".join(unreachable) or "none",
                                extra={"attempt_id": attempt_id},
                            )
                end = offset + len(content)
                uow.logs.append(
                    LogChunkRecord(
                        id=None,
                        attempt_id=attempt_id,
                        stream=chunk.stream,
                        offset_start=offset,
                        offset_end=end,
                        ts=chunk.ts or self._clock.now(),
                        line_sha256=chunk.line_sha256 or "",
                        occurrence=chunk.occurrence,
                        content=content,
                    )
                )
                offset = end
                stored += 1
                if chunk.ts is not None and chunk.line_sha256:
                    attempt.log_resume_ts = chunk.ts
                    attempt.log_resume_sha256 = chunk.line_sha256
                    attempt.log_resume_occurrence = chunk.occurrence
            if stored:
                uow.heartbeats.append(
                    Heartbeat(
                        id=None,
                        attempt_id=attempt_id,
                        ts=self._clock.now(),
                        signal="log_advanced",
                        detail={"chunks": stored, "offset": offset},
                    )
                )
                uow.attempts.save(attempt)
            uow.commit()
            return stored

    def _mark_logs_drained(self, attempt_id: str) -> None:
        """The final pull after exit. Cleanup never runs before this (08, 10)."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            if attempt is None or attempt.logs_drained_at is not None:
                return
            attempt.logs_drained_at = self._clock.now()
            uow.attempts.save(attempt)
            record_event(
                uow,
                self._clock,
                EventKind.ATTEMPT_LOGS_DRAINED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt_id,
                payload={"chunks": len(uow.logs.list_for_attempt(attempt_id, limit=10_000))},
            )
            uow.commit()

    def _list_cleanup_due(self) -> list[tuple[Attempt, str, str]]:
        """Attempts whose logs are drained, that are done, and that are not cleaned."""
        out: list[tuple[Attempt, str, str]] = []
        with self._uow_factory() as uow:
            states = [
                AttemptState.COLLECTED,
                AttemptState.SUCCEEDED,
                AttemptState.BLOCKED,
                AttemptState.FAILED,
            ]
            for attempt in uow.attempts.list_in_states(states):
                if attempt.logs_drained_at is None or attempt.cleaned_up_at is not None:
                    continue
                execution = uow.executions.get(attempt.execution_id)
                if execution is None:
                    continue
                if (
                    attempt.exit_class is ExitClass.INFRASTRUCTURE
                    and attempt.ended_at is not None
                    and self._clock.now()
                    < attempt.ended_at + timedelta(seconds=self.attempt_lease_ttl_seconds)
                ):
                    continue
                cleanup = (execution.policy_snapshot or {}).get("cleanup", {})
                succeeded = attempt.state is AttemptState.SUCCEEDED
                choice = str(
                    cleanup.get("workspace_on_success" if succeeded else "workspace_on_failure")
                    or ("keep_diff_only" if succeeded else "keep")
                )
                checkpoint_push_failed = any(
                    event.attempt_id == attempt.id
                    and event.kind == EventKind.TASK_PUBLISH_FAILED.value
                    and event.payload.get("step") == "quota_checkpoint"
                    for event in self._all_task_events(uow, attempt.task_id)
                )
                if checkpoint_push_failed or attempt.exit_class in {
                    ExitClass.INFRASTRUCTURE,
                    ExitClass.QUOTA_EXHAUSTED,
                }:
                    choice = "keep"
                out.append((attempt, execution.provider, choice))
        return out

    async def _cleanup_step(self) -> int:
        """08: remove the container, keep or delete the workspace per policy, release
        the checkout lease, and record it. Only ever after `logs_drained`."""
        cleaned = 0
        for attempt, provider_name, choice in await self._db(self._list_cleanup_due):
            if attempt.id in self._collects:
                # Finished and visible, but its task is still pushing a quota checkpoint
                # off this workspace; cleanup waits for the task to end.
                continue
            try:
                provider = self._provider(provider_name)
            except ProviderError:
                continue
            policy = {
                "delete": CleanupPolicy.DELETE,
                "keep": CleanupPolicy.KEEP,
                "keep_diff_only": CleanupPolicy.KEEP_DIFF_ONLY,
            }.get(choice, CleanupPolicy.KEEP)
            try:
                spec = await self._spec_for(attempt)
                await provider.cleanup(self._workspace_for(attempt), policy, spec)
            except ProviderError:
                log.exception("cleanup failed; the next tick tries again")
                continue
            await self._db(partial(self._mark_cleaned, attempt.id, choice))
            self._collect_pending_ticks.pop(attempt.id, None)
            self._collect_failing_since.pop(attempt.id, None)
            self._collect_retry_at.pop(attempt.id, None)
            self._workspaces.pop(attempt.id, None)
            self._workspace_fingerprints.pop(attempt.id, None)
            self._activity_asked.pop(attempt.id, None)
            self._activity_refresh.pop(attempt.id, None)
            self._command_watches.pop(attempt.id, None)
            self._handles.pop(attempt.id, None)
            cleaned += 1
        return cleaned

    def _mark_cleaned(self, attempt_id: str, choice: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            if attempt is None or attempt.cleaned_up_at is not None:
                return
            attempt.cleaned_up_at = self._clock.now()
            uow.attempts.save(attempt)
            self._release_checkout_leases(uow, attempt)
            record_event(
                uow,
                self._clock,
                EventKind.ATTEMPT_CLEANED_UP,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt_id,
                payload={"workspace": choice},
            )
            uow.commit()

    async def _retention_step(self) -> int:
        """16: deterministic, idempotent, and every deletion an event and a row."""
        applied = await self._db(self._retention_sweep)
        applied += await self._release_workspaces()
        keep = await self._db(self._live_attempt_ids)
        for provider in self._providers.values():
            try:
                applied += await provider.retention(keep)
            except ProviderError:
                log.warning("provider retention failed; the next tick tries again")
        return applied

    def _live_attempt_ids(self) -> list[str]:
        """The attempts whose provider objects the retention sweep must leave alone.

        A finished attempt stays in the set until its cleanup has run: until then its
        claim carries no retention label, and the sweep would delete a workspace that
        acceptance and the publisher still read the bundle from (hades #237). Cleanup is
        what labels a kept claim or deletes it."""
        with self._uow_factory() as uow:
            live = [
                attempt.id
                for attempt in uow.attempts.list_in_states(
                    [
                        AttemptState.PENDING,
                        AttemptState.PREPARING,
                        AttemptState.LAUNCHING,
                        AttemptState.RUNNING,
                        AttemptState.TERMINATING,
                        AttemptState.EXITED,
                        AttemptState.COLLECTED,
                    ]
                )
            ]
            live.extend(
                attempt.id
                for attempt in uow.attempts.list_in_states(sorted(ATTEMPT_TERMINAL))
                if attempt.cleaned_up_at is None
            )
            return live

    async def _release_workspaces(self) -> int:
        """16 and the lab findings of 2026-09-29: a workspace a cleanup policy kept is
        removed once nothing needs it, where before it stayed until an operator deleted
        it and the claims filled the namespace quota. `workspace_release_reason` is the
        rule; each release is a retention action and an event naming the policy version
        whose window applied."""
        released = 0
        for item in await self._db(self._list_workspace_releases):
            attempt = item.attempt
            with log_context(
                task_id=attempt.task_id, execution_id=attempt.execution_id, attempt_id=attempt.id
            ):
                try:
                    provider = self._provider(item.provider)
                    spec = await self._spec_for(attempt)
                    await provider.release_workspace(self._workspace_for(attempt), spec)
                except LeaseLostError:
                    raise
                except Exception:
                    log.warning(
                        "a kept workspace could not be released; the next tick tries again",
                        exc_info=True,
                    )
                    continue
                finally:
                    self._workspaces.pop(attempt.id, None)
                if await self._db(partial(self._record_workspace_release, item)):
                    released += 1
        return released

    def _list_workspace_releases(self) -> list[_WorkspaceRelease]:
        now = self._clock.now()
        out: list[_WorkspaceRelease] = []
        with self._uow_factory() as uow:
            for attempt in uow.attempts.list_cleaned_unreleased(
                RETENTION_WORKSPACE, limit=RETENTION_BATCH
            ):
                if len(out) >= WORKSPACE_RELEASE_BATCH:
                    break
                execution = uow.executions.get(attempt.execution_id)
                if execution is None:
                    continue
                task = uow.tasks.get(attempt.task_id)
                section, name, version = self._retention_for(uow, task)
                days = int(
                    section.get("completed_workspaces_days") or DEFAULT_WORKSPACE_RETENTION_DAYS
                )
                reason = workspace_release_reason(uow, task, attempt, now, days)
                if reason is not None:
                    out.append(
                        _WorkspaceRelease(
                            attempt=attempt,
                            provider=execution.provider,
                            reason=reason,
                            policy_name=name,
                            policy_version=version,
                            days=days,
                        )
                    )
        return out

    def _record_workspace_release(self, item: _WorkspaceRelease) -> bool:
        with self._fenced() as uow:
            row = uow.retention.record(
                RetentionAction(
                    id=new_id(),
                    kind=RETENTION_WORKSPACE,
                    subject=item.attempt.id,
                    policy_name=item.policy_name,
                    policy_version=item.policy_version,
                    acted_at=self._clock.now(),
                    detail={"reason": item.reason, "days": item.days, "provider": item.provider},
                )
            )
            if row is None:
                return False
            record_event(
                uow,
                self._clock,
                EventKind.RETENTION_APPLIED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=item.attempt.task_id,
                execution_id=item.attempt.execution_id,
                attempt_id=item.attempt.id,
                payload={
                    "kind": RETENTION_WORKSPACE,
                    "subject": item.attempt.id,
                    "reason": item.reason,
                    "days": item.days,
                },
            )
            uow.commit()
            return True

    def _retention_for(self, uow: UnitOfWork, task: Task | None) -> tuple[dict[str, Any], str, int]:
        """The retention section of the policy that governs this task, and its version.

        Every deletion names the policy version that authorized it (16), so the window
        comes from the task's own policy, never from a global default."""
        if task is None:
            return {}, "unknown", 0
        policy = uow.policies.get(task.policy_name, task.policy_version)
        section = dict((policy.document if policy else {}).get("retention", {}))
        return section, task.policy_name, task.policy_version

    def _retention_sweep(self) -> int:
        now = self._clock.now()
        applied = 0
        # Credential renewal runs outside the tick's open transaction to avoid
        # holding the fence while the OAuth token refresh hits the network.
        # A holder that lost the lease must not refresh the shared Codex login.
        if getattr(self, "fenced_token", None) is None:
            return 0
        credential_renewed = (
            self._credential_renewal() if self._credential_renewal is not None else False
        )
        if credential_renewed:
            applied += 1
        with self._fenced() as uow:
            if self._credential_sweep is not None:
                applied += self._credential_sweep(uow)

            def act(
                kind: str, subject: str, name: str, version: int, detail: dict[str, Any]
            ) -> bool:
                row = uow.retention.record(
                    RetentionAction(
                        id=new_id(),
                        kind=kind,
                        subject=subject,
                        policy_name=name,
                        policy_version=version,
                        acted_at=now,
                        detail=detail,
                    )
                )
                if row is None:
                    return False
                record_event(
                    uow,
                    self._clock,
                    EventKind.RETENTION_APPLIED,
                    principal=PRINCIPAL_CRUCIBLE,
                    payload={"kind": kind, "subject": subject, **detail},
                )
                return True

            for attempt_id in uow.logs.attempts_with_logs_before(now, RETENTION_BATCH):
                attempt = uow.attempts.get(attempt_id)
                if attempt is None:
                    continue
                task = uow.tasks.get(attempt.task_id)
                section, name, version = self._retention_for(uow, task)
                days = int(section.get("logs_and_transcripts_days") or DEFAULT_LOG_RETENTION_DAYS)
                newest = uow.logs.attempts_with_logs_before(
                    now - timedelta(days=days), RETENTION_BATCH
                )
                if attempt_id not in newest:
                    continue
                removed = uow.logs.delete_for_attempts([attempt_id])
                if act("logs", attempt_id, name, version, {"chunks": removed, "days": days}):
                    applied += 1

            floor = now - timedelta(days=1)
            for wake in uow.wakes.list_acked_before(floor, RETENTION_BATCH):
                task = uow.tasks.get(wake.task_id) if wake.task_id else None
                section, name, version = self._retention_for(uow, task)
                days = int(section.get("wakes_after_ack_days") or DEFAULT_WAKE_RETENTION_DAYS)
                if wake.acked_at is None or wake.acked_at > now - timedelta(days=days):
                    continue
                uow.wakes.delete(wake.id)
                if act("wake", wake.id, name, version, {"days": days}):
                    applied += 1
            uow.commit()
            return applied

    # ----- step: observe ---------------------------------------------------

    def _list_live(self) -> list[Attempt]:
        with self._uow_factory() as uow:
            return list(
                uow.attempts.list_in_states(
                    [
                        AttemptState.PREPARING,
                        AttemptState.LAUNCHING,
                        AttemptState.RUNNING,
                        AttemptState.TERMINATING,
                    ]
                )
            )

    async def _observe_attempts(self) -> tuple[int, int]:
        observed = 0
        finished = self._harvest(self._collects, "collection")
        started = set(self._collects)
        for attempt in await self._db(self._list_live):
            if attempt.id in self._launches:
                # Its launch is still running here; it is not stranded (hades #190).
                continue
            if attempt.id in self._collects:
                # Its collection is still running beside the tick.
                continue
            with log_context(
                task_id=attempt.task_id, execution_id=attempt.execution_id, attempt_id=attempt.id
            ):
                observed += 1
                try:
                    if await self._observe_one(attempt):
                        finished += 1
                except LeaseLostError:
                    raise
                except Exception:
                    log.exception("observe step failed; continuing with the next attempt")
        # As for launches: this tick waits a bounded time (`collect_wait_seconds`) for
        # the collections it started, so a quick one ends in the tick that began it and
        # the gates after this step see it; a slow one is counted by the tick that sees
        # it end.
        begun = [task for key, task in self._collects.items() if key not in started]
        if begun:
            await asyncio.wait(begun, timeout=self.collect_wait_seconds)
        finished += self._harvest(self._collects, "collection")
        return observed, finished

    async def _observe_one(self, attempt: Attempt) -> bool:
        if attempt.state in (AttemptState.PREPARING, AttemptState.LAUNCHING):
            return await self._reconcile_stranded(attempt)
        provider_name = await self._db(partial(self._execution_provider_name, attempt))
        provider = self._provider(provider_name)
        handle = self._handles.get(attempt.id) or Handle(
            provider=provider_name, ref=attempt.handle or "", attempt_id=attempt.id
        )
        self._handles[attempt.id] = handle
        observation = await provider.observe(handle)
        now = self._clock.now()
        if observation.state is ObservationState.RUNNING:
            if attempt.drain_deadline is not None and now >= attempt.drain_deadline:
                # Past the grace window (timeout or cancel): kill, and keep killing every
                # tick until the provider stops seeing it. The event is written once.
                await provider.terminate(handle, "kill")
                if attempt.killed_at is None:
                    await self._db(partial(self._record_kill, attempt.id))
                else:
                    log.warning("worker survived kill; retrying", extra={"handle": handle.ref})
                return False
            if (
                attempt.state is AttemptState.RUNNING
                and attempt.timeout_at is not None
                and attempt.drain_deadline is None
                and now >= attempt.timeout_at
            ):
                await provider.terminate(handle, "drain")
                await self._db(partial(self._record_drain, attempt.id, TERMINATION_TIMEOUT))
                return False
            # Pull before checking for a stall so bytes arriving on this observation
            # count as activity. A merely running container is liveness, not progress.
            await self._pull_logs(attempt, provider, handle)
            changed = await self._workspace_changed(attempt, provider, handle)
            if changed:
                await self._db(partial(self._record_workspace_activity, attempt.id))
                self._note_workspace_edit(attempt.id)
            await self._note_running_commands(attempt)
            early = await self._db(partial(self._early_stall, attempt.id))
            # A loop verdict is checked against the workspace first. A provider whose
            # workspace is probed no more often than a command renews activity
            # (Kubernetes) can have many repeats of an editing command between two
            # probes; one more probe now, past the throttle, says whether the repeats
            # were iteration. It costs one exec, once per verdict.
            if (
                early is not None
                and early.shape != STALL_NO_ACTIVITY
                and not changed
                and await self._workspace_changed(attempt, provider, handle, force=True)
            ):
                await self._db(partial(self._record_workspace_activity, attempt.id))
                self._note_workspace_edit(attempt.id)
                early = await self._db(partial(self._early_stall, attempt.id))
            if early is not None:
                await provider.terminate(handle, "drain")
                await self._db(
                    partial(self._record_drain, attempt.id, TERMINATION_STALL, early=early)
                )
                return False
            stall = await self._db(partial(self._stall_action, attempt.id))
            if stall == "fail":
                await provider.terminate(handle, "drain")
                await self._db(partial(self._record_drain, attempt.id, TERMINATION_STALL))
                return False
            if stall == "warn":
                await self._db(partial(self._record_stall_warning, attempt.id))
            await self._db(partial(self._renew_attempt_lease, attempt.id))
            return False
        if observation.state is ObservationState.LOST:
            # Nothing more can arrive from a worker the provider cannot see, and its
            # credential copy will never be synced: remove it now (12).
            await self._discard(
                provider,
                self._workspace_for(attempt),
                await self._spec_for(attempt),
            )
            await self._db(partial(self._mark_logs_drained, attempt.id))
            await self._db(partial(self._finish_lost, attempt.id, observation.detail))
            return True
        # The final drain, the collection and the finish run beside the tick, as a
        # launch does (hades #190): a collection can take the collector's, the
        # verifier's and the reader Pods' whole timeouts, and the tick has to keep
        # renewing its lease and launching other tasks meanwhile (lab findings of
        # 2026-09-29). Until it ends the attempt is that task's alone.
        retry_at = self._collect_retry_at.get(attempt.id)
        if retry_at is not None and time.monotonic() < retry_at:
            return False
        self._collects[attempt.id] = asyncio.create_task(
            self._collect_attempt(attempt, provider, handle, observation),
            name=f"collect-{attempt.id}",
        )
        return False

    async def _collect_attempt(
        self,
        attempt: Attempt,
        provider: ExecutionProvider,
        handle: Handle,
        observation: Observation,
    ) -> bool:
        """Drain, collect and finish one exited attempt. True when it finished; False
        when the provider could not answer and the collection is tried again later."""
        with log_context(
            task_id=attempt.task_id, execution_id=attempt.execution_id, attempt_id=attempt.id
        ):
            if attempt.logs_drained_at is None:
                # The final drain before anything is collected or cleaned up (08, 10).
                await self._drain_logs(attempt, provider, handle)
                await self._db(partial(self._mark_logs_drained, attempt.id))
            spec = await self._spec_for(attempt)
            collection_error: str | None = None
            try:
                outputs = await provider.collect(handle, self._workspace_for(attempt), spec)
            except CollectionPendingError as exc:
                ticks = self._collect_pending_ticks.get(attempt.id, 0) + 1
                self._collect_pending_ticks[attempt.id] = ticks
                if ticks < self.collection_retry_ticks:
                    # Also marks the attempt as collecting for stall detection. Zero
                    # makes it eligible on the next tick, without an outage backoff.
                    self._collect_retry_at[attempt.id] = 0.0
                    log.warning("collection is pending (%s); it runs again next tick", exc)
                    return False
                collection_error = (
                    f"{exc} (collection cleanup remained pending for {ticks} collection ticks)"
                )
                outputs = CollectedOutputs(report=None, report_raw=None, blocked_md=None)
                log.warning(
                    "collection failed (%s); the attempt fails as environment", collection_error
                )
            except ProviderUnavailableError as exc:
                # The cluster could not answer or take a step right now. The workspace
                # still holds the work, so the attempt is collected again rather than
                # failed, until the window runs out (lab findings of 2026-09-29).
                first = self._collect_failing_since.setdefault(attempt.id, time.monotonic())
                if time.monotonic() - first < COLLECT_RETRY_WINDOW_SECONDS:
                    self._collect_retry_at[attempt.id] = (
                        time.monotonic() + COLLECT_RETRY_INTERVAL_SECONDS
                    )
                    log.warning("collection could not finish (%s); it runs again", exc)
                    return False
                collection_error = (
                    f"{exc} (the provider could not finish a collection for "
                    f"{COLLECT_RETRY_WINDOW_SECONDS}s)"
                )
                outputs = CollectedOutputs(report=None, report_raw=None, blocked_md=None)
                log.warning("collection failed (%s); the attempt fails as environment", exc)
            except ProviderError as exc:
                # 16: a provider that failed while producing the outputs is an
                # environment failure. The attempt still finishes, with nothing
                # collected, so the next tick does not try the same collection again
                # forever.
                collection_error = str(exc)
                outputs = CollectedOutputs(report=None, report_raw=None, blocked_md=None)
                log.warning("collection failed (%s); the attempt fails as environment", exc)
            self._collect_pending_ticks.pop(attempt.id, None)
            self._collect_failing_since.pop(attempt.id, None)
            self._collect_retry_at.pop(attempt.id, None)
            await self._db(
                partial(
                    self._finish_exited,
                    attempt.id,
                    observation.exit_code,
                    outputs,
                    collection_error,
                    observation.oom_killed,
                    defer_quota=True,
                    final_observation=observation,
                )
            )
            if await self._db(partial(self._quota_checkpoint_pending, attempt.id)):
                await self._complete_quota_checkpoint(attempt.id)
            self._handles.pop(attempt.id, None)
            self._workspaces.pop(attempt.id, None)
            self._workspace_fingerprints.pop(attempt.id, None)
            self._activity_asked.pop(attempt.id, None)
            self._activity_refresh.pop(attempt.id, None)
            self._command_watches.pop(attempt.id, None)
            return True

    def _pending_quota_checkpoints(self) -> list[str]:
        with self._uow_factory() as uow:
            return [
                attempt.id
                for attempt in uow.attempts.list_in_states(
                    [AttemptState.COLLECTED, AttemptState.FAILED]
                )
                if attempt.exit_class is ExitClass.QUOTA_EXHAUSTED
                and (execution := uow.executions.get(attempt.execution_id)) is not None
                and execution.state is ExecutionState.ACTIVE
                and (task := uow.tasks.get(attempt.task_id)) is not None
                and task.state is TaskState.RUNNING
                and not self._quota_checkpoint_has_disposition(uow, attempt)
            ]

    def _quota_checkpoint_has_disposition(self, uow: UnitOfWork, attempt: Attempt) -> bool:
        return any(
            event.attempt_id == attempt.id
            and event.kind
            in {
                EventKind.TASK_REROUTED.value,
                EventKind.TASK_AWAITING_QUOTA.value,
            }
            for event in self._all_task_events(uow, attempt.task_id)
        )

    async def _resume_quota_checkpoints(self) -> None:
        for attempt_id in await self._db(self._pending_quota_checkpoints):
            if attempt_id in self._collects:
                # Its collection task pushes the checkpoint itself once it has finished
                # the attempt; a second push here would race it on the same Job.
                continue
            await self._complete_quota_checkpoint(attempt_id)

    def _quota_checkpoint_safety(self, attempt_id: str) -> tuple[bool, str]:
        with self._uow_factory() as uow:
            attempt = uow.attempts.get(attempt_id)
            assert attempt is not None
            task = uow.tasks.get(attempt.task_id)
            execution = uow.executions.get(attempt.execution_id)
            assert task is not None and execution is not None
            collection_failure = uow.events.latest_for_task_kind(
                task.id, EventKind.COLLECTION_FAILED.value
            )
            if (
                collection_failure is not None
                and collection_failure.attempt_id == attempt.id
                and collection_failure.payload.get("checkpoint_refusal") is True
            ):
                return False, str(collection_failure.payload.get("detail", "checkpoint refused"))
            inputs = gate_input(uow, task=task, attempt=attempt, execution=execution)
            outcomes = {
                gate.value: evaluate_gate(gate.value, inputs)
                for gate in (
                    GateName.SCOPE_CONTAINED,
                    GateName.NO_INJECTED_FILES,
                    GateName.NO_SECRETS,
                )
            }
        failures = [
            f"{gate}: {outcome.detail}"
            for gate, outcome in outcomes.items()
            if outcome.result is not GateResult.PASS
        ]
        return (not failures, "; ".join(failures) or "checkpoint safety gates passed")

    async def _complete_quota_checkpoint(self, attempt_id: str) -> None:
        safe, detail = await self._db(partial(self._quota_checkpoint_safety, attempt_id))
        if not safe:
            await self._db(partial(self._finish_deferred_quota, attempt_id, False, detail))
            return
        attempt = await self._db(lambda: self._attempt_by_id(attempt_id))
        if attempt is None:
            return
        spec = await self._spec_for(attempt)
        if spec is None:
            await self._db(
                partial(
                    self._finish_deferred_quota,
                    attempt_id,
                    False,
                    "the checkpoint has no reconstructable launch specification",
                )
            )
            return
        if await self._db(partial(self._task_merged, attempt.task_id)):
            # hades #379: the pull request was merged while the attempt ran. Nothing is
            # pushed to the merged branch; the attempt is ended as a cancel ends it.
            return
        provider_name = await self._db(partial(self._execution_provider_name, attempt))
        provider = self._provider(provider_name)
        local_push = getattr(provider, "push_quota_checkpoint", None)
        outcome = await local_push(self._workspace_for(attempt), spec) if local_push else None
        if outcome is None:
            repository_url = spec.repository_url
            local_origin = repository_url.startswith("/") or repository_url.startswith("file://")
            if local_push is None and local_origin:
                await self._db(
                    partial(
                        self._finish_deferred_quota,
                        attempt_id,
                        False,
                        f"local-origin quota checkpoints are Docker-only; provider "
                        f"{provider_name} did not push a checkpoint",
                        checkpoint_skipped=True,
                    )
                )
                return
            required = provider_name == "docker" and not (local_origin)
            outcome = await self.delivery.push_quota_checkpoint(attempt_id, required=required)
            if outcome is None:
                # GitHub's rate limit: the checkpoint stays pending and a later tick pushes
                # it, rather than the tick sleeping (hades FDY-0139).
                return
        await self._db(partial(self._finish_deferred_quota, attempt_id, *outcome))

    def _task_merged(self, task_id: str) -> bool:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            return task is not None and task.state is TaskState.MERGED

    def _attempt_by_id(self, attempt_id: str) -> Attempt | None:
        with self._uow_factory() as uow:
            return uow.attempts.get(attempt_id)

    def _quota_checkpoint_pending(self, attempt_id: str) -> bool:
        with self._uow_factory() as uow:
            attempt = uow.attempts.get(attempt_id)
            if attempt is None or attempt.exit_class is not ExitClass.QUOTA_EXHAUSTED:
                return False
            execution = uow.executions.get(attempt.execution_id)
            task = uow.tasks.get(attempt.task_id)
            return bool(
                execution is not None
                and execution.state is ExecutionState.ACTIVE
                and task is not None
                and task.state is TaskState.RUNNING
                and not self._quota_checkpoint_has_disposition(uow, attempt)
            )

    def _finish_deferred_quota(
        self,
        attempt_id: str,
        pushed: bool,
        detail: str,
        *,
        checkpoint_skipped: bool = False,
    ) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            execution = uow.executions.get(attempt.execution_id, for_update=True)
            task = uow.tasks.get(attempt.task_id, for_update=True)
            assert execution is not None and task is not None
            if (
                attempt.exit_class is not ExitClass.QUOTA_EXHAUSTED
                or execution.state is not ExecutionState.ACTIVE
                or task.state is not TaskState.RUNNING
            ):
                return
            if pushed:
                execution.resume_from_remote = True
                uow.executions.save(execution)
                self._handle_quota_exit(uow, task, execution, attempt)
            elif checkpoint_skipped:
                self._handle_quota_exit(
                    uow,
                    task,
                    execution,
                    attempt,
                    checkpoint_skip_detail=detail[:1000],
                )
            else:
                bundle_path = f"{attempt.workspace_path}/output/work_branch.bundle"
                record_event(
                    uow,
                    self._clock,
                    EventKind.TASK_PUBLISH_FAILED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={
                        "step": "quota_checkpoint",
                        "detail": detail[:1000],
                        "head_sha": task.head_sha,
                        "bundle_path": bundle_path,
                    },
                )
                retained = uow.retention.record(
                    RetentionAction(
                        id=new_id(),
                        kind="quota_checkpoint_retained",
                        subject=attempt.id,
                        policy_name=task.policy_name,
                        policy_version=task.policy_version,
                        acted_at=self._clock.now(),
                        detail={
                            "workspace": attempt.workspace_path,
                            "bundle_path": bundle_path,
                            "reason": "checkpoint push failed",
                        },
                    )
                )
                if retained is not None:
                    record_event(
                        uow,
                        self._clock,
                        EventKind.RETENTION_APPLIED,
                        principal=PRINCIPAL_CRUCIBLE,
                        task_id=task.id,
                        execution_id=execution.id,
                        attempt_id=attempt.id,
                        payload={
                            "kind": "quota_checkpoint_retained",
                            "subject": attempt.id,
                            "bundle_path": bundle_path,
                        },
                    )
                move_execution(
                    uow,
                    self._clock,
                    execution,
                    ExecutionState.FAILED,
                    EventKind.EXECUTION_FAILED,
                    payload={"exit_class": "quota_exhausted", "checkpoint_push": "failed"},
                )
                self._task_reported(
                    uow,
                    task,
                    attempt,
                    ExitClass.QUOTA_EXHAUSTED,
                    {},
                    wake_summary=(
                        f"checkpoint push failed; workspace retained and recovery bundle is "
                        f"{bundle_path}"
                    ),
                )
            uow.commit()

    async def _reconcile_stranded(self, attempt: Attempt) -> bool:
        """An attempt still in preparing or launching after the launch step ran was left
        there by a supervisor that died mid-launch. If the provider can see a worker for
        it, adopt it as running; otherwise collect it as environment so the retry rule
        applies (10, 16)."""
        provider_name = await self._db(partial(self._execution_provider_name, attempt))
        provider = self._provider(provider_name)
        handle = self._handles.get(attempt.id)
        if handle is None:
            discovered = {h.attempt_id: h for h in await provider.reconcile()}
            handle = discovered.get(attempt.id)
        if handle is None and attempt.handle is not None:
            handle = Handle(provider=provider_name, ref=attempt.handle, attempt_id=attempt.id)
        if handle is not None:
            observation = await provider.observe(handle)
            if observation.state is not ObservationState.LOST:
                if await self._db(partial(self._adopt, attempt.id, handle)):
                    self._handles[attempt.id] = handle
                    return False
                # hades #189: its task was cancelled while the launch that started this
                # worker was in flight; the attempt is settled, and the worker goes.
                try:
                    await provider.terminate(handle, "kill")
                except Exception:
                    log.exception("terminate of a cancelled launch failed; retention removes it")
                return True
        await self._db(
            partial(
                self._environment_failure,
                attempt.id,
                "reconcile",
                "attempt stranded in launch with no worker the provider can see",
            )
        )
        return True

    def _adopt(self, attempt_id: str, handle: Handle) -> bool:
        """Adopt a stranded attempt's worker as running. False when its task was
        cancelled meanwhile: the attempt ends killed at stage launch instead, never
        running, and the caller stops the worker (hades #189)."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            if attempt.state not in (AttemptState.PREPARING, AttemptState.LAUNCHING):
                return True
            task = uow.tasks.get(attempt.task_id, for_update=True)
            assert task is not None
            if task.state in ENDS_ATTEMPTS:
                self._end_cancelled_launch(uow, attempt, task, "launch")
                uow.commit()
                return False
            execution = uow.executions.get(attempt.execution_id)
            assert execution is not None
            now = self._clock.now()
            if attempt.state is AttemptState.PREPARING:
                move_attempt(
                    uow, self._clock, attempt, AttemptState.LAUNCHING, EventKind.ATTEMPT_LAUNCHING
                )
            attempt.handle = handle.ref
            attempt.started_at = attempt.started_at or now
            attempt.timeout_at = attempt.started_at + timedelta(seconds=execution.timeout_seconds)
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.RUNNING,
                EventKind.ATTEMPT_ADOPTED,
                payload={"handle": handle.ref},
            )
            assert self.fenced_token is not None
            uow.leases.upsert_attempt_lease(
                attempt.id, self.holder, self.fenced_token, now, self.attempt_lease_ttl_seconds
            )
            uow.commit()
            return True

    def _renew_attempt_lease(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            assert self.fenced_token is not None
            # Attempt leases are not a fenced table; check the supervisor lease explicitly.
            if not uow.leases.verify_supervisor(self.holder, self.fenced_token):
                self.fenced_token = None
                raise LeaseLostError("supervisor lease changed hands")
            uow.leases.upsert_attempt_lease(
                attempt_id,
                self.holder,
                self.fenced_token,
                self._clock.now(),
                self.attempt_lease_ttl_seconds,
            )
            uow.commit()

    def _record_drain(
        self, attempt_id: str, reason: str, *, early: EarlyStall | None = None
    ) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            now = self._clock.now()
            attempt.drain_deadline = now + timedelta(seconds=self.grace_seconds)
            attempt.termination_reason = reason
            if early is not None:
                attempt.stall_shape = early.shape
                attempt.termination_detail = early.detail
            uow.attempts.save(attempt)
            shape = {"stall_shape": early.shape, "detail": early.detail} if early else {}
            record_event(
                uow,
                self._clock,
                EventKind.ATTEMPT_TIMEOUT_DRAIN,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt.id,
                payload={
                    "reason": reason,
                    "drain_deadline": attempt.drain_deadline.isoformat(),
                    "grace_seconds": self.grace_seconds,
                    **shape,
                },
            )
            if reason == TERMINATION_STALL:
                record_event(
                    uow,
                    self._clock,
                    EventKind.WORKER_STALLED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={"reason": "stall", **shape},
                )
            uow.commit()

    def _early_stall(self, attempt_id: str) -> EarlyStall | None:
        """Issue 278: a degenerate run the live log shows, read from the tracker that
        `_note_running_commands` has just fed. Only for a harness whose tracker can say
        (a CommandLoopTracker); every other is held to the time-based limits alone."""
        watch = self._command_watches.get(attempt_id)
        tracker = watch.tracker if watch is not None else None
        if watch is None or not isinstance(tracker, CommandLoopTracker):
            return None
        with self._uow_factory() as uow:
            attempt = uow.attempts.get(attempt_id)
            if (
                attempt is None
                or attempt.state is not AttemptState.RUNNING
                or attempt.drain_deadline is not None
            ):
                return None
        return degenerate_stall(
            now=self._clock.now(),
            repeated=tracker.repeated,
            tool_called=tracker.tool_called,
            responding_since=watch.responding_since,
            local=watch.local,
        )

    def _stall_action(self, attempt_id: str) -> str | None:
        """Return the action due from verified activity, without changing state."""
        with self._uow_factory() as uow:
            attempt = uow.attempts.get(attempt_id)
            if (
                attempt is None
                or attempt.state is not AttemptState.RUNNING
                or attempt.drain_deadline is not None
            ):
                return None
            execution = uow.executions.get(attempt.execution_id)
            if execution is None:
                return None
            limits = (execution.policy_snapshot or {}).get("limits", {})
            warn = int(limits.get("stall_warn_seconds", 300))
            fail = int(limits.get("stall_fail_seconds", 1800))
            activity = uow.heartbeats.latest_activity(attempt.id)
            activity_baseline = (
                activity.ts if activity is not None else attempt.started_at or attempt.created_at
            )
            signal = uow.heartbeats.latest_signal(attempt.id)
            signal_baseline = signal.ts if signal is not None else activity_baseline
            latest = uow.events.latest_for_task_kind(attempt.task_id, EventKind.WORKER_QUIET.value)
            warned_at = (
                latest.ts if latest is not None and latest.attempt_id == attempt.id else None
            )
            return worker_stall_action(
                now=self._clock.now(),
                last_activity=activity_baseline,
                last_signal=signal_baseline,
                warn_seconds=warn,
                fail_seconds=fail,
                warned_at=warned_at,
            )

    async def _workspace_changed(
        self, attempt: Attempt, provider: ExecutionProvider, handle: Handle, *, force: bool = False
    ) -> bool:
        """Whether the worker's files moved since the last look. A local workspace is
        walked here every tick. A provider whose workspace is not local (Kubernetes)
        answers through its activity probe instead (FDY-0140), asked no more often than
        a command in flight renews activity, since each ask is an exec into the Pod,
        unless `force` (issue 278: a loop verdict asks once more before it stands)."""
        probe = getattr(provider, "activity", None)
        if probe is None:
            current: tuple[int, int, int] | None = await self._db(
                partial(workspace_fingerprint, self._workspace_for(attempt))
            )
        else:
            now = self._clock.now()
            asked = self._activity_asked.get(attempt.id)
            if asked is not None and not force:
                refresh = self._activity_refresh.get(attempt.id)
                if refresh is None:
                    refresh = await self._db(partial(self._activity_refresh_seconds, attempt))
                    self._activity_refresh[attempt.id] = refresh
                if (now - asked).total_seconds() < refresh:
                    return False
            self._activity_asked[attempt.id] = now
            try:
                current = await probe(handle, self._workspace_for(attempt))
            except Exception:
                log.exception("the activity probe failed; the stall clock runs")
                current = None
        if current is None:
            return False
        previous = self._workspace_fingerprints.get(attempt.id)
        self._workspace_fingerprints[attempt.id] = current
        return previous is not None and current != previous

    def _activity_refresh_seconds(self, attempt: Attempt) -> int:
        with self._uow_factory() as uow:
            execution = uow.executions.get(attempt.execution_id)
            limits = ((execution.policy_snapshot if execution else None) or {}).get("limits", {})
        return command_refresh_seconds(
            int(limits.get("stall_warn_seconds", 300)),
            int(limits.get("stall_fail_seconds", 1800)),
        )

    def _note_workspace_edit(self, attempt_id: str) -> None:
        """Issue 278: a verified workspace change ends the tracker's run of repeated
        commands, as an edit in the log does. Only a tracker that counts them hears it."""
        watch = self._command_watches.get(attempt_id)
        tracker = watch.tracker if watch is not None else None
        if isinstance(tracker, CommandLoopTracker):
            tracker.workspace_changed()

    def _record_workspace_activity(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            if attempt is None or attempt.state is not AttemptState.RUNNING:
                return
            uow.heartbeats.append(
                Heartbeat(
                    id=None,
                    attempt_id=attempt.id,
                    ts=self._clock.now(),
                    signal="fs_changed",
                    detail={},
                )
            )
            uow.commit()

    async def _note_running_commands(self, attempt: Attempt) -> None:
        """Issue 152. Best effort: whatever a harness's stream does to its tracker, the
        stall check and the lease renewal after this still run."""
        try:
            running = await self._db(partial(self._running_commands, attempt))
            if running:
                await self._db(partial(self._record_command_running, attempt.id, running))
        except LeaseLostError:
            raise
        except Exception:
            log.exception("reading the commands in flight failed; the stall clock runs")

    def _command_watch(self, uow: UnitOfWork, attempt: Attempt) -> _CommandWatch:
        watch = self._command_watches.get(attempt.id)
        if watch is not None:
            return watch
        execution = uow.executions.get(attempt.execution_id)
        harness = attempt.selected_harness or (execution.harness if execution else None)
        adapter = (
            self._harnesses.get(harness)
            if self._harnesses is not None and harness is not None
            else None
        )
        watch = _CommandWatch(adapter.command_tracker() if adapter is not None else None)
        if execution is not None:
            try:
                routing = load_attempt_routing(
                    uow, execution.policy_snapshot or {}, attempt.routing_version
                )
                model = attempt.selected_model or execution.model
                route = (
                    None
                    if routing is None
                    else routing.model(model, attempt.selected_harness or execution.harness)
                )
                watch.local = route is not None and route.endpoint == "local"
            except Exception:
                # Issue 278 must not cost the attempt its command tracking (issue 152):
                # with no route known, only the local first-response deadline is lost.
                log.exception("reading the attempt's route failed; no first-response deadline")
            limits = (execution.policy_snapshot or {}).get("limits", {})
            watch.refresh_seconds = command_refresh_seconds(
                int(limits.get("stall_warn_seconds", 300)),
                int(limits.get("stall_fail_seconds", 1800)),
            )
            stored = uow.contracts.get(attempt.task_id, execution.contract_version)
            watch.command_timeout_seconds = (
                effective_command_timeout_ms(
                    execution.policy_snapshot,
                    stored.document if stored is not None else None,
                    execution.timeout_seconds,
                )
                / 1000
            )
        self._command_watches[attempt.id] = watch
        return watch

    def _running_commands(self, attempt: Attempt) -> tuple[str, ...]:
        """Issue 152: what the harness's live log says it has in flight, feeding the
        attempt's tracker every stored chunk it has not yet seen, less any command
        reported for longer than its command timeout allows.

        On a restart or a takeover, `watch` is new and this replays the whole stored
        log at once. A command already running when the replay starts gets its
        `first_seen` from the chunk where the tracker first reports it, not from now,
        so its age survives the restart instead of resetting on every takeover."""
        with self._uow_factory() as uow:
            watch = self._command_watch(uow, attempt)
            tracker = watch.tracker
            if tracker is None:
                return ()
            while True:
                chunks = uow.logs.list_for_attempt(
                    attempt.id, after_id=watch.after_id, limit=COMMAND_LOG_PAGE
                )
                for chunk in chunks:
                    # Past this chunk before feeding it: a chunk that upsets the tracker
                    # is not read again on every later tick.
                    watch.after_id = chunk.id or watch.after_id
                    before = {key for key, _ in tracker.running}
                    tracker.feed(chunk.stream, chunk.content.decode("utf-8", "replace"))
                    after = {key for key, _ in tracker.running}
                    # A key gone within this same chunk (Hermes reuses one fixed key
                    # for its whole registry) must drop its age here: the cleanup in
                    # commands_counted only sees the batch's final state, so a command
                    # that ended and a same-keyed one that started would otherwise
                    # share the first command's first_seen.
                    for gone in before - after:
                        watch.first_seen.pop(gone, None)
                    for key in after - before:
                        watch.first_seen.setdefault(key, chunk.ts)
                    if (
                        watch.responding_since is None
                        and isinstance(tracker, CommandLoopTracker)
                        and tracker.responding
                    ):
                        watch.responding_since = chunk.ts
                if len(chunks) < COMMAND_LOG_PAGE:
                    break
        return commands_counted(
            tracker.running,
            watch.first_seen,
            now=self._clock.now(),
            command_timeout_seconds=watch.command_timeout_seconds,
        )

    def _record_command_running(self, attempt_id: str, running: tuple[str, ...]) -> None:
        """A command in flight is activity (issue 152, the operator's decision of
        2026-09-27), so neither stall limit counts it; its command timeout does."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            if attempt is None or attempt.state is not AttemptState.RUNNING:
                return
            now = self._clock.now()
            latest = uow.heartbeats.latest_activity(attempt.id)
            watch = self._command_watches.get(attempt.id)
            refresh = watch.refresh_seconds if watch else COMMAND_RUNNING_REFRESH_SECONDS
            if not command_activity_due(
                now=now, last_activity=latest.ts if latest else None, refresh_seconds=refresh
            ):
                return
            uow.heartbeats.append(
                Heartbeat(
                    id=None,
                    attempt_id=attempt.id,
                    ts=now,
                    signal="command_running",
                    detail={"commands": list(running[:5]), "count": len(running)},
                )
            )
            uow.commit()

    def _record_stall_warning(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            if attempt is None or attempt.state is not AttemptState.RUNNING:
                return
            execution = uow.executions.get(attempt.execution_id)
            task = uow.tasks.get(attempt.task_id)
            assert execution is not None and task is not None
            signal = uow.heartbeats.latest_signal(attempt.id)
            baseline = signal.ts if signal is not None else attempt.started_at or attempt.created_at
            latest = uow.events.latest_for_task_kind(task.id, EventKind.WORKER_QUIET.value)
            if latest is not None and latest.attempt_id == attempt.id and latest.ts >= baseline:
                return
            idle_seconds = int((self._clock.now() - baseline).total_seconds())
            record_event(
                uow,
                self._clock,
                EventKind.WORKER_QUIET,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={"idle_seconds": idle_seconds},
            )
            create_wake(
                uow,
                self._clock,
                principal_id=task.principal_id,
                reason=WakeReason.STALL_WARNING,
                summary=(
                    f"attempt {attempt.number} has made no verified progress for "
                    f"{idle_seconds} seconds"
                ),
                task=task,
                attempt_id=attempt.id,
            )
            uow.commit()

    def _record_kill(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            attempt.killed_at = self._clock.now()
            uow.attempts.save(attempt)
            kind = (
                EventKind.ATTEMPT_CANCEL_KILL
                if attempt.termination_reason == TERMINATION_CANCEL
                else EventKind.ATTEMPT_TIMEOUT_KILL
            )
            record_event(
                uow,
                self._clock,
                kind,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt.id,
                payload={"reason": attempt.termination_reason},
            )
            uow.commit()

    def _finish_lost(self, attempt_id: str, detail: str | None) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            now = self._clock.now()
            attempt.ended_at = now
            attempt.exit_class = ExitClass.LOST
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.EXITED,
                EventKind.ATTEMPT_LOST,
                payload={"detail": detail, "last_handle": attempt.handle},
            )
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={"report_present": False, "blocked_present": False},
            )
            uow.leases.release_attempt_lease(attempt.id)
            self._record_bare_evidence(uow, attempt)
            self._classify_and_finish(uow, attempt, None)
            uow.commit()

    def _finish_exited(
        self,
        attempt_id: str,
        exit_code: int | None,
        outputs: CollectedOutputs,
        collection_error: str | None = None,
        oom_killed: bool = False,
        *,
        defer_quota: bool = False,
        final_observation: Observation | None = None,
    ) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            task = uow.tasks.get(attempt.task_id, for_update=True)
            assert task is not None
            attempt.exit_code = exit_code
            attempt.ended_at = self._clock.now()
            timed_out = attempt.termination_reason in (TERMINATION_TIMEOUT, TERMINATION_STALL)
            killed = attempt.termination_reason == TERMINATION_CANCEL
            execution = uow.executions.get(attempt.execution_id)
            assert execution is not None
            stored = uow.contracts.get(
                task.id,
                getattr(execution, "contract_version", getattr(task, "contract_version", 1)),
            )
            # 07: a report file that is present but does not parse is a parse failure,
            # recorded as one; only a missing file is "without report".
            report_present = outputs.report_raw is not None or outputs.report is not None
            exit_info = ExitInfo(
                exit_code=exit_code,
                report_present=report_present,
                blocked_present=outputs.blocked_md is not None,
                oom_killed=oom_killed,
                timed_out=timed_out,
                killed=killed,
            )
            adapter = self._harnesses.get(execution.harness) if self._harnesses else None
            report_dir = (
                Path(attempt.workspace_path) / "output" / "report"
                if attempt.workspace_path
                else None
            )
            if adapter is not None:
                # 07 and S5: the adapter classifies from the code and both tails.
                attempt.exit_class = adapter.classify_exit(
                    exit_info, outputs.stdout_tail, outputs.stderr_tail, report_dir
                )
            else:
                attempt.exit_class = classify_exit(
                    exit_code=exit_code,
                    report_present=report_present,
                    blocked_present=outputs.blocked_md is not None,
                    timed_out=timed_out,
                    killed=killed,
                )
                if oom_killed and not (timed_out or killed):
                    attempt.exit_class = ExitClass.ENVIRONMENT
            interruption = outputs.interruption
            if interruption is None and adapter is not None:
                # The provider read the tails before collection; the adapter reads its
                # own transcript in the collected report too (the app-server host
                # writes its events only there).
                interruption = adapter.interruption(
                    exit_info, outputs.stdout_tail, outputs.stderr_tail, report_dir
                )
            never_started = final_observation is not None and final_observation.never_started
            if not (timed_out or killed or oom_killed):
                if never_started:
                    attempt.exit_class = ExitClass.INFRASTRUCTURE
                elif interruption is not None and attempt.exit_class in {
                    ExitClass.CRASHED,
                    ExitClass.UNKNOWN,
                    ExitClass.INFRASTRUCTURE,
                }:
                    attempt.exit_class = interruption.exit_class
            interruption_detail = (
                f"the worker never started: {final_observation.container_message}"
                if never_started and final_observation is not None
                else interruption.message
                if interruption is not None
                else (outputs.stderr_tail or outputs.stdout_tail)[-2000:]
                or "provider quota exhausted"
            )
            if (
                attempt.termination_reason == TERMINATION_STALL
                and attempt.exit_class is ExitClass.TIMEOUT
            ):
                # FDY-0140: Crucible ended it for a stall, so it is recorded as one.
                attempt.exit_class = ExitClass.STALLED
            # hades #378: the reset a harness states as "Resets in 3h52m" counts from
            # the supervisor's own clock, the moment the refusal was observed.
            provider_quota = (
                adapter.provider_quota_event(
                    outputs.stdout_tail, outputs.stderr_tail, now=self._clock.now()
                )
                if attempt.exit_class is ExitClass.QUOTA_EXHAUSTED and adapter is not None
                else None
            )
            pool_mark: tuple[PoolExhaustion, bool] | None = None
            if attempt.exit_class is ExitClass.QUOTA_EXHAUSTED:
                pool_mark = self._mark_pool_exhausted(
                    uow, attempt, execution, provider_quota.reset_at if provider_quota else None
                )
            # A collection failure makes this exit `environment` below, whatever the
            # harness said, so it is not a gateway failure and marks nothing.
            if attempt.exit_class is ExitClass.PROVIDER_ERROR and collection_error is None:
                self._mark_local_endpoint_down(uow, attempt, execution)
            parsed: ParsedReport | None = None
            if adapter is not None and report_dir is not None and report_dir.is_dir():
                parsed = adapter.parse_report(report_dir, exit_info)
            self._record_credential_sync(uow, attempt, execution, outputs)
            if collection_error is not None:
                # Preserve the original interruption even if sealing also failed.
                if attempt.exit_class not in {ExitClass.INFRASTRUCTURE, ExitClass.QUOTA_EXHAUSTED}:
                    attempt.exit_class = ExitClass.ENVIRONMENT
                record_event(
                    uow,
                    self._clock,
                    EventKind.COLLECTION_FAILED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={"detail": collection_error[:1000], "exit_code": exit_code},
                )
            elif outputs.checkpoint_refusal is not None:
                record_event(
                    uow,
                    self._clock,
                    EventKind.COLLECTION_FAILED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={
                        "detail": outputs.checkpoint_refusal[:1000],
                        "exit_code": exit_code,
                        "checkpoint_refusal": True,
                    },
                )
            interruption_payload: dict[str, Any] = {}
            if attempt.exit_class in {ExitClass.INFRASTRUCTURE, ExitClass.QUOTA_EXHAUSTED}:
                routing = load_attempt_routing(
                    uow, execution.policy_snapshot or {}, attempt.routing_version
                )
                model = (
                    routing.model(
                        attempt.selected_model or execution.model,
                        attempt.selected_harness or execution.harness,
                    )
                    if routing
                    else None
                )
                interruption_payload = {
                    "interruption_message": redact(interruption_detail),
                    "endpoint_url": model.endpoint_url if model is not None else None,
                    "capacity_refused": interruption is not None and interruption.capacity,
                    "never_started": never_started,
                    "no_commits": collection_error is None
                    and outputs.checkpoint_refusal is None
                    and (outputs.bundle is None or outputs.bundle.commits == 0),
                    "container_message": redact(final_observation.container_message or "")
                    if final_observation
                    else None,
                    "pod_events": self._pod_event_rows(final_observation)
                    if final_observation
                    else [],
                }
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.EXITED,
                EventKind.ATTEMPT_EXITED,
                payload={
                    "exit_code": exit_code,
                    "exit_class": attempt.exit_class.value,
                    "termination_reason": attempt.termination_reason,
                    "oom_killed": oom_killed,
                    **interruption_payload,
                },
            )
            if never_started and final_observation is not None:
                self._record_start_failure_evidence(uow, attempt, final_observation)
            if execution.role is ExecutionRole.REVIEW:
                self._finish_review_attempt(uow, attempt, outputs)
                uow.leases.release_attempt_lease(attempt.id)
                uow.commit()
                return
            claim_ok = False
            cancelled = attempt.termination_reason == TERMINATION_CANCEL
            if cancelled and (outputs.report_raw is not None or outputs.report is not None):
                partial_report = outputs.report_raw or yaml.safe_dump(
                    outputs.report, sort_keys=True, default_flow_style=False
                )
                if find_secrets(partial_report):
                    partial_report = redact(partial_report)
                store_artifact(
                    uow,
                    self._clock,
                    self._artifacts,
                    attempt=attempt,
                    name="report/report.yaml",
                    artifact_type="partial_report",
                    content=partial_report.encode("utf-8"),
                    content_type="application/yaml",
                )
            unparsed_errors: list[dict[str, Any]] | None = None
            if outputs.report is None and outputs.report_raw is not None and not cancelled:
                # The file exists and is not a YAML mapping (a bare colon in a value is
                # the usual cause). Its errors name the problem and position, never the
                # text; nothing of the file is stored. The report is present, so the
                # gate that reads it is for the reviewer, not a stop (ADR 0024).
                unparsed_errors = load_report(outputs.report_raw)[1] or [
                    {"loc": [], "msg": "report is not a mapping", "type": "shape"}
                ]
                record_event(
                    uow,
                    self._clock,
                    EventKind.REPORT_PARSE_FAILED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={"errors": unparsed_errors},
                )
            completed: CompletedClaim | None = None
            claim = None
            expected_findings: set[str] = set()
            if outputs.report is not None and not cancelled:
                # hades #215: Crucible's own facts in place of the worker's, then parse.
                completed = complete_claim(outputs.report, claim_facts(task, outputs))
                claim, errors = parse_claim(
                    completed.document,
                    criteria=[str(c["id"]) for c in stored.document.get("acceptance_criteria", [])]
                    if stored is not None
                    else None,
                )
                claim_ok = claim is not None
                correction = (stored.document.get("correction") if stored else None) or {}
                expected_findings = {
                    str(address.get("id"))
                    for address in correction.get("addresses", [])
                    if isinstance(address, dict) and address.get("kind") == "review_comment"
                }
                finding_counts = (
                    Counter(item.review_comment_id for item in claim.finding_dispositions)
                    if claim is not None
                    else Counter()
                )
                duplicate_ids = sorted(
                    finding_id for finding_id, count in finding_counts.items() if count > 1
                )
                if expected_findings and (
                    set(finding_counts) != expected_findings or duplicate_ids
                ):
                    errors.append(
                        {
                            "loc": ["finding_dispositions"],
                            "msg": (
                                "the correction report must disposition exactly its review "
                                "findings; expected "
                                + ", ".join(sorted(expected_findings))
                                + (
                                    "; duplicate ids: " + ", ".join(duplicate_ids)
                                    if duplicate_ids
                                    else ""
                                )
                            ),
                            "type": "value_error",
                        }
                    )
                    claim_ok = False
                # The worker's document and the completed one: Crucible's facts carry
                # names the worker chose (changed paths, report file names).
                secret_hits = find_secrets(outputs.report) + find_secrets(completed.document)
                if secret_hits:
                    errors = errors + [
                        {"loc": [m.path], "msg": f"secret pattern {m.pattern}", "type": "secret"}
                        for m in secret_hits
                    ]
                    claim_ok = False
                    document: dict[str, Any] = {"redacted": True}
                else:
                    document = completed.document
                uow.claims.put(
                    CompletionClaimRecord(
                        attempt_id=attempt.id,
                        document=document,
                        parsed_ok=claim_ok,
                        parse_errors=errors,
                    )
                )
                record_event(
                    uow,
                    self._clock,
                    EventKind.REPORT_PARSED if claim_ok else EventKind.REPORT_PARSE_FAILED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={
                        **({"errors": errors} if errors else {"schema": "CompletionClaimV1"}),
                        "filled_by_crucible": list(completed.filled),
                        "differences": [dict(d) for d in completed.differences],
                    },
                )
            blocked_reason, blocked_text = blocked_note(outputs.blocked_md)
            if attempt.exit_class is ExitClass.BLOCKED:
                # hades #393: on the attempt record before the evidence and the
                # classification read it.
                attempt.blocked_reason = blocked_reason
                attempt.blocked_statement = blocked_text
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={
                    "report_present": report_present,
                    "report_parsed": claim_ok,
                    "partial_report_kept_unparsed": cancelled and report_present,
                    "blocked_present": outputs.blocked_md is not None,
                    **({"blocked_reason": blocked_reason} if blocked_reason else {}),
                    # Issue 128: commands the harness was waiting on and cut off at its
                    # exit. A background process left running is not listed (153).
                    **(
                        {"work_in_flight": [redact(item) for item in parsed.in_flight]}
                        if parsed is not None and parsed.in_flight
                        else {}
                    ),
                    # FDY-0140: Crucible committed what the worker left uncommitted, or
                    # could not, and why. The commit itself is in the collected branch.
                    **({"uncommitted_work_committed": True} if outputs.leftover_committed else {}),
                    **(
                        {"uncommitted_work_note": redact(outputs.leftover_note)[:500]}
                        if outputs.leftover_note
                        else {}
                    ),
                },
            )
            uow.leases.release_attempt_lease(attempt.id)
            claim_document = outputs.report if (outputs.report and not cancelled) else None
            stored_claim = uow.claims.get(attempt.id) if claim_document else None
            errors = list(stored_claim.parse_errors) if stored_claim else []
            head = record_collection_evidence(
                uow,
                self._clock,
                self._artifacts,
                attempt=attempt,
                task=task,
                outputs=outputs,
                claim=claim_document,
                claim_parsed_ok=claim_ok,
                parse_errors=errors,
                parsed_report=parsed,
                completed=completed,
                unparsed_errors=unparsed_errors,
            )
            # hades #360: a correction ended by a merge never reaches the PR, so its head
            # is evidence on the attempt and not the merged task's head.
            if head and task.state is not TaskState.MERGED:
                task.head_sha = head
                task.updated_at = self._clock.now()
                uow.tasks.save(task)
            self._record_wall_time(uow, attempt)
            if parsed is not None:
                self._record_harness_metrics(uow, attempt, parsed)
                if parsed.progress:
                    recorded = ingest_progress(
                        uow,
                        self._clock,
                        attempt_id=attempt.id,
                        task_id=attempt.task_id,
                        execution_id=attempt.execution_id,
                        progress=parsed.progress,
                    )
                    uow.heartbeats.append(
                        Heartbeat(
                            id=None,
                            attempt_id=attempt.id,
                            ts=self._clock.now(),
                            signal="progress_line",
                            detail={"lines": recorded},
                        )
                    )
            self._classify_and_finish(
                uow,
                attempt,
                blocked_text,
                blocked_reason=blocked_reason,
                claim_ok=claim_ok,
                defer_quota=defer_quota,
                turn_cap_reached=parsed is not None and parsed.limit_reached is not None,
                has_commits=outputs.bundle is not None and outputs.bundle.commits > 0,
                pool_mark=pool_mark,
            )
            # A valid report from a failed or locally capped attempt is evidence,
            # but its dispositions must not settle findings or queue public replies.
            if (
                attempt.state is AttemptState.SUCCEEDED
                and claim_ok
                and claim is not None
                and expected_findings
            ):
                for finding in claim.finding_dispositions:
                    comment = uow.review_comments.get(finding.review_comment_id)
                    if (
                        comment is None
                        or uow.dispositions.get_by_comment(comment.id, comment.body_sha256)
                        is not None
                    ):
                        continue
                    kind = (
                        DispositionKind.FIX
                        if finding.disposition == "fixed"
                        else DispositionKind.DECLINE
                    )
                    reasoning = (
                        f"Fixed in commit {finding.commit}"
                        if finding.disposition == "fixed"
                        else str(finding.reason)
                    )
                    disposition = ReviewDisposition(
                        id=new_id(),
                        review_comment_id=comment.id,
                        comment_body_sha256=comment.body_sha256,
                        principal_id=task.principal_id,
                        disposition=kind,
                        reasoning=reasoning,
                        created_at=self._clock.now(),
                    )
                    uow.dispositions.add(disposition)
                    record_event(
                        uow,
                        self._clock,
                        EventKind.DISPOSITION_RECORDED,
                        principal=PRINCIPAL_CRUCIBLE,
                        task_id=task.id,
                        attempt_id=attempt.id,
                        payload={
                            "disposition_id": disposition.id,
                            "review_comment_id": comment.id,
                            "disposition": kind.value,
                            "from_worker_report": True,
                            "reply_pending": kind is DispositionKind.DECLINE,
                            "reasoning": reasoning,
                        },
                    )
            uow.commit()

    def _record_credential_sync(
        self, uow: UnitOfWork, attempt: Attempt, execution: Execution, outputs: CollectedOutputs
    ) -> None:
        """12 and 25: what the sync-back did, as an event and on the harness row. Names,
        booleans and reasons; never a value."""
        now = self._clock.now()
        auth_failure = attempt.exit_class is ExitClass.AUTH_FAILURE
        if auth_failure:
            self.auth_failures_by_harness[execution.harness] += 1
            count = self.auth_failures_by_harness[execution.harness]
            log.warning(
                "harness authentication failure: harness=%s attempt=%s auth_failure_count=%s",
                execution.harness,
                attempt.id,
                count,
                extra={
                    "harness": execution.harness,
                    "attempt_id": attempt.id,
                    "auth_failure_count": count,
                },
            )
        record_launch_outcome(
            uow,
            self._clock,
            name=execution.harness,
            outcome=attempt.exit_class.value if attempt.exit_class else "unknown",
            at=now,
            auth_failure=auth_failure,
        )
        sync = outputs.credential_sync
        if sync is None:
            return
        record_event(
            uow,
            self._clock,
            EventKind.CREDENTIAL_SYNCED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=attempt.task_id,
            execution_id=attempt.execution_id,
            attempt_id=attempt.id,
            payload=sync.as_dict(),
        )
        record_credential_observation(
            uow,
            self._clock,
            name=execution.harness,
            mount_mode=MountMode(sync.mount_mode),
            changed=sync.changed,
            at=now,
        )

    def _record_harness_metrics(
        self, uow: UnitOfWork, attempt: Attempt, parsed: ParsedReport
    ) -> None:
        """05b: what the transcript said about tokens and cost, on the metrics row the
        pools count. A harness that reports nothing leaves null, and the pool counts
        attempts (routing.py)."""
        metrics = uow.attempt_metrics.get(attempt.id)
        if metrics is None:
            return
        reported = parsed.metrics
        if reported.model is not None:
            # The model that answered, beside the one the contract named: a harness that
            # silently substituted one is visible to pool accounting and history (05b).
            metrics.model_reported = reported.model
        if reported.tokens_in is not None:
            metrics.tokens_in = reported.tokens_in
        if reported.tokens_out is not None:
            metrics.tokens_out = reported.tokens_out
        if reported.duration_ms is not None:
            metrics.harness_duration_ms = reported.duration_ms
        if reported.tool_calls is not None:
            metrics.tool_calls = reported.tool_calls
        if reported.cost_usd is not None:
            metrics.cost_units = reported.cost_usd
        if reported.source != "none":
            metrics.cost_source = reported.source
        uow.attempt_metrics.put(metrics)
        record_event(
            uow,
            self._clock,
            EventKind.ATTEMPT_METRICS_RECORDED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=attempt.task_id,
            execution_id=attempt.execution_id,
            attempt_id=attempt.id,
            payload={
                **reported.as_dict(),
                "model_requested": metrics.model,
                "transcript_lines": parsed.transcript_lines,
                "transcript": parsed.transcript_name,
                "progress_lines": len(parsed.progress),
            },
        )

    def _record_wall_time(self, uow: UnitOfWork, attempt: Attempt) -> None:
        metrics = uow.attempt_metrics.get(attempt.id)
        if metrics is None:
            return
        if attempt.started_at is not None and attempt.ended_at is not None:
            metrics.wall_ms = int((attempt.ended_at - attempt.started_at).total_seconds() * 1000)
        metrics.exit_class = attempt.exit_class.value if attempt.exit_class else None
        uow.attempt_metrics.put(metrics)

    def _finish_review_attempt(
        self, uow: UnitOfWork, attempt: Attempt, outputs: CollectedOutputs
    ) -> None:
        """A `review` execution succeeds when a ReviewReportV1 parses (09). Its verdict
        does not move the task by itself; Foundry's acceptance does (11)."""
        task = uow.tasks.get(attempt.task_id, for_update=True)
        execution = uow.executions.get(attempt.execution_id, for_update=True)
        assert task is not None and execution is not None
        recorded = False
        detail = "no review report was produced"
        clean = attempt.exit_code == 0 and attempt.exit_class in CLEAN_EXIT_CLASSES
        if outputs.report is not None and not clean:
            # Issue 128: a review that exited with work in flight (or any unclean exit)
            # did not finish, so its report is not recorded as a review.
            exit_class = attempt.exit_class.value if attempt.exit_class else "unknown"
            detail = f"the review execution ended {exit_class}, not a clean completion"
        elif outputs.report is not None:
            try:
                record_review_report(
                    uow,
                    self._clock,
                    task=task,
                    document=outputs.report,
                    reviewer_kind="crucible_review_execution",
                    reviewer_attempt_id=attempt.id,
                    reviewer_principal_id=None,
                    principal_name=PRINCIPAL_CRUCIBLE,
                )
                recorded = True
                detail = "ReviewReportV1 recorded"
            except ApplicationError as exc:
                detail = exc.detail
                if exc.event is not None:
                    # The supervisor's transaction does not roll back here, so the
                    # rejection is recorded in place rather than by the API handler.
                    uow.events.append(exc.event)
        move_attempt(
            uow,
            self._clock,
            attempt,
            AttemptState.COLLECTED,
            EventKind.ATTEMPT_COLLECTED,
            payload={"role": "review", "review_recorded": recorded, "detail": detail},
        )
        self._record_wall_time(uow, attempt)
        if recorded:
            move_attempt(
                uow, self._clock, attempt, AttemptState.SUCCEEDED, EventKind.ATTEMPT_SUCCEEDED
            )
            move_execution(
                uow, self._clock, execution, ExecutionState.SUCCEEDED, EventKind.EXECUTION_SUCCEEDED
            )
        else:
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.FAILED,
                EventKind.ATTEMPT_FAILED,
                payload={"role": "review", "detail": detail},
            )
            move_execution(
                uow,
                self._clock,
                execution,
                ExecutionState.FAILED,
                EventKind.EXECUTION_FAILED,
                payload={"role": "review", "detail": detail},
            )
            create_wake(
                uow,
                self._clock,
                principal_id=task.principal_id,
                reason=WakeReason.ATTEMPT_FAILED,
                summary=(
                    f"the review execution produced no usable ReviewReportV1 ({detail}); "
                    f"{task.head_sha} still has no non-author review"
                ),
                task=task,
                attempt_id=attempt.id,
                extra_links={"review": f"/v1/tasks/{task.id}/review"},
            )
        work = latest_work_attempt(uow, task)
        if work is not None:
            work_attempt, work_execution = work
            evaluate_and_advance(
                uow, self._clock, task=task, attempt=work_attempt, execution=work_execution
            )

    # ----- reactive quota routing -----------------------------------------

    def _routing_context(
        self,
        uow: UnitOfWork,
        task: Task,
        execution: Execution,
        routing_version: int | None = None,
    ) -> tuple[Any, TaskContractV1] | None:
        stored = uow.contracts.get(task.id, execution.contract_version)
        routing = load_attempt_routing(uow, execution.policy_snapshot or {}, routing_version)
        if stored is None or routing is None:
            return None
        return routing, TaskContractV1.model_validate(stored.document)

    def _class_pool_resets(
        self, uow: UnitOfWork, routing: Any, contract: TaskContractV1
    ) -> list[Any]:
        tier = routing.tiers[contract.execution_request.tier.value]
        pinned = contract.execution_request.pinned_model
        pools = {
            model.pool
            for model in routing.models
            if model.enabled
            and model.capability in tier.allowed_capability
            and (pinned is None or model.id == pinned)
        }
        now = self._clock.now()
        return sorted(
            mark.reset_at
            for mark in uow.pool_exhaustions.list_all()
            if mark.pool in pools and mark.cleared_at is None and mark.reset_at > now
        )

    def _enter_quota_wait(
        self,
        uow: UnitOfWork,
        task: Task,
        attempt: Attempt,
        execution: Execution,
        selection: Any,
        pool_mark: tuple[PoolExhaustion, bool] | None = None,
    ) -> None:
        mark, opened = pool_mark if pool_mark is not None else (None, False)
        sentence = (
            pool_exhausted_summary(mark.pool, mark.reset_at, mark.reason)
            if mark is not None
            else None
        )
        context = self._routing_context(uow, task, execution)
        now = self._clock.now()
        if context is None:
            if attempt.state not in ATTEMPT_TERMINAL:
                attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
                attempt.ended_at = now
                move_attempt(
                    uow, self._clock, attempt, AttemptState.COLLECTED, EventKind.ATTEMPT_COLLECTED
                )
                move_attempt(
                    uow, self._clock, attempt, AttemptState.FAILED, EventKind.ATTEMPT_FAILED
                )
            if execution.state is ExecutionState.CREATED:
                move_execution(
                    uow, self._clock, execution, ExecutionState.ACTIVE, EventKind.EXECUTION_ACTIVE
                )
            move_execution(
                uow, self._clock, execution, ExecutionState.FAILED, EventKind.EXECUTION_FAILED
            )
            if task.state is TaskState.SCHEDULED:
                move_task(
                    uow,
                    self._clock,
                    task,
                    TaskState.RUNNING,
                    EventKind.TASK_RUNNING,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={"attempt_number": attempt.number},
                )
            self._task_reported(
                uow, task, attempt, ExitClass.QUOTA_EXHAUSTED, {}, wake_summary=sentence
            )
            return
        routing, contract = context
        resets = self._class_pool_resets(uow, routing, contract)
        if not resets:
            if attempt.state not in ATTEMPT_TERMINAL:
                attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
                attempt.ended_at = now
                move_attempt(
                    uow, self._clock, attempt, AttemptState.COLLECTED, EventKind.ATTEMPT_COLLECTED
                )
                move_attempt(
                    uow, self._clock, attempt, AttemptState.FAILED, EventKind.ATTEMPT_FAILED
                )
            if execution.state is ExecutionState.CREATED:
                move_execution(
                    uow, self._clock, execution, ExecutionState.ACTIVE, EventKind.EXECUTION_ACTIVE
                )
            move_execution(
                uow, self._clock, execution, ExecutionState.FAILED, EventKind.EXECUTION_FAILED
            )
            if task.state is TaskState.SCHEDULED:
                move_task(
                    uow,
                    self._clock,
                    task,
                    TaskState.RUNNING,
                    EventKind.TASK_RUNNING,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={"attempt_number": attempt.number},
                )
            self._task_reported(
                uow, task, attempt, ExitClass.QUOTA_EXHAUSTED, {}, wake_summary=sentence
            )
            return
        if attempt.state not in ATTEMPT_TERMINAL:
            attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
            attempt.ended_at = now
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={"exit_class": ExitClass.QUOTA_EXHAUSTED.value},
            )
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.FAILED,
                EventKind.ATTEMPT_FAILED,
                payload={"exit_class": ExitClass.QUOTA_EXHAUSTED.value},
            )
        if execution.state is ExecutionState.CREATED:
            move_execution(
                uow, self._clock, execution, ExecutionState.ACTIVE, EventKind.EXECUTION_ACTIVE
            )
        first_wait = task.quota_wait_started_at is None
        task.quota_wait_started_at = task.quota_wait_started_at or now
        deadline = task.quota_wait_started_at + timedelta(
            seconds=routing.reroute.resume_max_wait_seconds
        )
        task.resume_at = min(resets[0], deadline)
        uow.tasks.save(task)
        if task.state is not TaskState.AWAITING_QUOTA:
            move_task(
                uow,
                self._clock,
                task,
                TaskState.AWAITING_QUOTA,
                EventKind.TASK_AWAITING_QUOTA,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "tier": contract.execution_request.tier.value,
                    "resume_at": task.resume_at.isoformat(),
                    "ordered_candidates": list(selection.candidates) if selection else [],
                },
            )
        if first_wait:
            create_wake(
                uow,
                self._clock,
                principal_id=task.principal_id,
                reason=WakeReason.AWAITING_QUOTA,
                summary=(
                    (f"{sentence}; " if sentence is not None else "")
                    + f"all pools for class {contract.execution_request.tier.value} are "
                    f"exhausted; Crucible will resume at {task.resume_at.isoformat()}"
                ),
                task=task,
                attempt_id=attempt.id,
            )
        elif mark is not None and opened:
            self._wake_pool_exhausted(uow, task, attempt, mark)

    def _mark_local_endpoint_down(
        self, uow: UnitOfWork, attempt: Attempt, execution: Execution
    ) -> None:
        """ADR 0028: a local model whose gateway failed twice in a row (the harness
        classified both exits `provider_error`: refused, unreachable, 5xx) takes its pool
        out of routing for the pool's default cooldown, so the tier's fallbacks carry the
        work meanwhile. One blip moves nothing. The mark is the one a quota exhaustion
        leaves: listed on Routing and clearable there. A subscription model's provider
        error leaves routing as it was."""
        task = uow.tasks.get(attempt.task_id)
        assert task is not None
        context = self._routing_context(uow, task, execution, attempt.routing_version)
        if context is None:
            return
        routing = context[0]
        entry = routing.model(execution.model, execution.harness)
        if entry is None or entry.endpoint != "local":
            return
        since = self._clock.now() - timedelta(
            seconds=window_seconds(routing.pools[entry.pool].window)
        )
        earlier = [
            row
            for row in uow.attempt_metrics.list_since(since=since, model=None, task_ids=None)
            if row.pool == entry.pool and row.attempt_id != attempt.id and row.exit_class
        ]
        if not earlier:
            return

        # The attempt that finished last, not the one launched last: attempts on one pool
        # run side by side and finish out of launch order.
        def finished(row: AttemptMetrics) -> tuple[datetime, str]:
            earlier_attempt = uow.attempts.get(row.attempt_id)
            ended = earlier_attempt.ended_at if earlier_attempt is not None else None
            return (ended or row.created_at or since, row.attempt_id)

        latest = max(earlier, key=finished)
        if latest.exit_class != ExitClass.PROVIDER_ERROR.value:
            return
        self._mark_pool_exhausted(
            uow,
            attempt,
            execution,
            None,
            reason="local endpoint failed (provider_error)",
        )

    def _mark_pool_exhausted(
        self,
        uow: UnitOfWork,
        attempt: Attempt,
        execution: Execution,
        reset_at: Any,
        *,
        reason: str = "harness reported quota_exhausted",
    ) -> tuple[PoolExhaustion, bool] | None:
        """Mark the attempt's pool exhausted until `reset_at` (05b). Returns the mark and
        whether it opened the pool's exhaustion (no mark was in force before it), or
        None when the attempt's route does not match its routing entry (hades #359).
        Foundry hears of an exhaustion once (hades #378): the caller raises that wake for
        a mark that opened, and an attempt refused while the mark is in force extends
        the mark and raises no second pool wake."""
        task = uow.tasks.get(attempt.task_id)
        assert task is not None
        context = self._routing_context(uow, task, execution, attempt.routing_version)
        if context is None or attempt.selected_pool is None:
            return None
        routing, _ = context
        # Only the pool of the route the attempt was verified to launch on is marked:
        # its model's entry in the attempt's routing version, paired with the harness
        # that ran and whose output was read (hades #359).
        entry = routing.model(
            attempt.selected_model or execution.model,
            attempt.selected_harness or execution.harness,
        )
        if (
            entry is None
            or entry.pool != attempt.selected_pool
            or entry.harness != (attempt.selected_harness or execution.harness)
        ):
            log.warning(
                "pool %s not marked exhausted: the attempt's route does not match its "
                "routing entry",
                attempt.selected_pool,
                extra={"attempt_id": attempt.id},
            )
            return None
        now = self._clock.now()
        reset, parsed_reset = self._bounded_quota_reset(
            now,
            reset_at,
            max_seconds=routing.reroute.resume_max_wait_seconds,
            default_seconds=routing.pools[attempt.selected_pool].default_cooldown_seconds,
        )
        prior = uow.pool_exhaustions.get(attempt.selected_pool)
        opened = prior is None or prior.cleared_at is not None or prior.reset_at <= now
        mark = uow.pool_exhaustions.put(
            PoolExhaustion(
                pool=attempt.selected_pool,
                exhausted_at=now,
                reset_at=reset,
                task_id=attempt.task_id,
                attempt_id=attempt.id,
                reason=reason,
            )
        )

        record_event(
            uow,
            self._clock,
            EventKind.POOL_EXHAUSTED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=attempt.task_id,
            execution_id=attempt.execution_id,
            attempt_id=attempt.id,
            payload={
                "pool": mark.pool,
                "reset_at": mark.reset_at.isoformat(),
                "source": "harness" if parsed_reset else "policy_default_cooldown",
                "reason": reason,
            },
        )
        return mark, opened

    def _wake_pool_exhausted(
        self, uow: UnitOfWork, task: Task, attempt: Attempt, mark: PoolExhaustion
    ) -> None:
        """The pool's own wake (hades #378), for a refusal whose path raises no other."""
        create_pool_exhausted_wake(
            uow,
            self._clock,
            task=task,
            attempt_id=attempt.id,
            pool=mark.pool,
            reset_at=mark.reset_at,
            reason=mark.reason,
        )

    @staticmethod
    def _bounded_quota_reset(
        now: Any, candidate: Any, *, max_seconds: int, default_seconds: int
    ) -> tuple[Any, Any]:
        del max_seconds
        accepted = candidate if candidate is not None and now < candidate else None
        return accepted or now + timedelta(seconds=default_seconds), accepted

    def _handle_quota_exit(
        self,
        uow: UnitOfWork,
        task: Task,
        execution: Execution,
        attempt: Attempt,
        *,
        source: str = "worker",
        pool_mark: tuple[PoolExhaustion, bool] | None = None,
        checkpoint_skip_detail: str | None = None,
    ) -> None:
        # hades #378: one wake per refusal names the pool and its reset. A task that
        # ends or waits says it in the wake it raises anyway; a reroute, which raised
        # none, raises the pool's own, once per exhaustion (when the mark opened).
        mark, opened = pool_mark if pool_mark is not None else (None, False)
        sentence = (
            pool_exhausted_summary(mark.pool, mark.reset_at, mark.reason)
            if mark is not None
            else None
        )
        # hades #254: the reroute routes with the version in force now, not the one the
        # exhausted attempt recorded, so a model disabled since is never launched again.
        context = self._routing_context(uow, task, execution)
        if context is None:
            move_execution(
                uow, self._clock, execution, ExecutionState.FAILED, EventKind.EXECUTION_FAILED
            )
            self._task_reported(
                uow, task, attempt, ExitClass.QUOTA_EXHAUSTED, {}, wake_summary=sentence
            )
            return
        routing, _contract = context
        reroutes = sum(
            event.kind
            in {
                EventKind.TASK_REROUTED.value,
                EventKind.TASK_QUOTA_RESUMED.value,
            }
            and int(event.payload.get("contract_version", 0)) == execution.contract_version
            for event in self._all_task_events(uow, task.id)
        )
        if task.head_sha and source == "worker":
            record_event(
                uow,
                self._clock,
                EventKind.QUOTA_WIP_COMMITTED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "commit_sha": task.head_sha,
                    "message": f"wip(crucible): attempt {attempt.id}",
                },
            )
        if reroutes >= routing.reroute.reroute_max:
            move_execution(
                uow,
                self._clock,
                execution,
                ExecutionState.FAILED,
                EventKind.EXECUTION_FAILED,
                payload={"exit_class": "quota_exhausted", "reroute_cap": reroutes},
            )
            self._task_reported(
                uow,
                task,
                attempt,
                ExitClass.QUOTA_EXHAUSTED,
                {},
                wake_summary=(
                    f"{sentence}; attempt {attempt.number} ended quota_exhausted with no "
                    f"reroute remaining (reroute_max {routing.reroute.reroute_max})"
                    if sentence is not None
                    else None
                ),
            )
            return
        stored = uow.contracts.get(task.id, execution.contract_version)
        assert stored is not None
        item = _Pending(attempt, execution, task, stored.document)
        selection = self._selection_for(
            uow, item, excluded_pools={attempt.selected_pool}, routing=routing
        )
        if selection is not None and selection.selected is not None and selection.image is not None:
            nxt = self._create_attempt(
                uow,
                execution,
                number=attempt.number + 1,
                excluded_pools={attempt.selected_pool} if attempt.selected_pool else None,
            )
            move_task(
                uow,
                self._clock,
                task,
                TaskState.SCHEDULED,
                EventKind.TASK_REROUTED,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "contract_version": execution.contract_version,
                    "from_attempt_id": attempt.id,
                    "from_pool": attempt.selected_pool,
                    "to_attempt_id": nxt.id,
                    "why": (
                        "previous pool reported quota exhaustion"
                        if source == "worker"
                        else "launch reservation found the selected pool unavailable"
                    ),
                    "source": source,
                    **(
                        {
                            "checkpoint_push": "skipped",
                            "checkpoint_detail": checkpoint_skip_detail,
                        }
                        if checkpoint_skip_detail is not None
                        else {}
                    ),
                    **({"wip_commit_sha": task.head_sha} if source == "worker" else {}),
                    "ordered_candidates": list(selection.candidates),
                },
            )
            if mark is not None and opened:
                self._wake_pool_exhausted(uow, task, attempt, mark)
            return
        self._enter_quota_wait(uow, task, attempt, execution, selection, pool_mark=pool_mark)

    @staticmethod
    def _all_task_events(uow: UnitOfWork, task_id: str) -> list[Any]:
        events: list[Any] = []
        after_seq = 0
        while True:
            page = list(uow.events.list_for_task(task_id, after_seq=after_seq, limit=1000))
            events.extend(page)
            if len(page) < 1000:
                return events
            after_seq = int(page[-1].seq or after_seq)

    def _resume_quota_waits(self) -> None:
        with self._fenced() as uow:
            now = self._clock.now()
            for task in uow.tasks.list_by_state(TaskState.AWAITING_QUOTA, for_update=True):
                if task.resume_at is not None and task.resume_at > now:
                    continue
                executions = [
                    execution
                    for execution in uow.executions.list_for_task(task.id)
                    if execution.state is ExecutionState.ACTIVE
                ]
                attempts = uow.attempts.list_for_task(task.id)
                if not executions or not attempts:
                    continue
                execution = executions[-1]
                attempt = attempts[-1]
                stored = uow.contracts.get(task.id, execution.contract_version)
                assert stored is not None
                context = self._routing_context(uow, task, execution)
                if context is None:
                    continue
                routing, contract = context
                quota_transitions = sum(
                    event.kind
                    in {
                        EventKind.TASK_REROUTED.value,
                        EventKind.TASK_QUOTA_RESUMED.value,
                    }
                    and int(event.payload.get("contract_version", 0)) == execution.contract_version
                    for event in self._all_task_events(uow, task.id)
                )
                started = task.quota_wait_started_at or now
                deadline = started + timedelta(seconds=routing.reroute.resume_max_wait_seconds)
                if now >= deadline or quota_transitions >= routing.reroute.reroute_max:
                    move_execution(
                        uow,
                        self._clock,
                        execution,
                        ExecutionState.FAILED,
                        EventKind.EXECUTION_FAILED,
                        payload={
                            "exit_class": "quota_exhausted",
                            "wait_cap_exceeded": now >= deadline,
                            "reroute_cap": quota_transitions,
                        },
                    )
                    self._task_reported(uow, task, attempt, ExitClass.QUOTA_EXHAUSTED, {})
                    continue
                selection = self._selection_for(
                    uow, _Pending(attempt, execution, task, stored.document)
                )
                if selection is not None and selection.selected is not None:
                    task.resume_at = None
                    uow.tasks.save(task)
                    move_task(
                        uow,
                        self._clock,
                        task,
                        TaskState.SCHEDULED,
                        EventKind.TASK_QUOTA_RESUMED,
                        execution_id=execution.id,
                        attempt_id=attempt.id,
                        payload={
                            "contract_version": execution.contract_version,
                            "model": selection.selected.id,
                            "pool": selection.selected.pool,
                        },
                    )
                    continue
                resets = self._class_pool_resets(uow, routing, contract)
                task.resume_at = min(resets[0], deadline) if resets else deadline
                uow.tasks.save(task)
                record_event(
                    uow,
                    self._clock,
                    EventKind.TASK_AWAITING_QUOTA,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={"resume_at": task.resume_at.isoformat(), "rechecked": True},
                )
            uow.commit()

    # ----- classification, retry, task transition --------------------------

    def _classify_and_finish(
        self,
        uow: UnitOfWork,
        attempt: Attempt,
        blocked_text: str | None,
        *,
        blocked_reason: str | None = None,
        claim_ok: bool = False,
        defer_quota: bool = False,
        turn_cap_reached: bool = False,
        has_commits: bool = True,
        wake_summary: str | None = None,
        pool_mark: tuple[PoolExhaustion, bool] | None = None,
    ) -> None:
        """Move the attempt to its terminal state and the task after it (09, 16).

        hades #393: `blocked_text` is the worker's `blocked.md` statement verbatim and
        `blocked_reason` the reason line it named (missing_capability or
        ambiguous_contract, or None). A blocked attempt keeps both, opens the one
        escalation carrying both, consumes no retry and marks no pool: this returns
        before the retry count and never reaches pool accounting.
        `pool_mark` is the exhaustion mark this exit wrote and whether it opened the
        pool's exhaustion (hades #378): the one wake the refusal raises names the pool
        and its reset, whichever path the attempt takes from here."""
        execution = uow.executions.get(attempt.execution_id, for_update=True)
        task = uow.tasks.get(attempt.task_id, for_update=True)
        assert execution is not None and task is not None
        if execution.role is ExecutionRole.REVIEW:
            # A review execution that never produced a report (prepare or launch failed,
            # the worker was lost, the quota refused it) has no path to `reported`: the
            # task is waiting in awaiting_internal_review and 09 gives it no such edge.
            self._finish_failed_review(uow, attempt, execution, task)
            return
        exit_class = attempt.exit_class or ExitClass.UNKNOWN
        local_cap = self._local_cap(uow, execution, attempt, exit_class, turn_cap_reached)
        # 10: the checkout lease is released on a terminal attempt state.
        self._release_checkout_leases(uow, attempt)
        if local_cap is not None:
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.FAILED,
                EventKind.ATTEMPT_FAILED,
                payload={"exit_class": exit_class.value, "local_cap": local_cap},
            )
        elif exit_class is ExitClass.COMPLETED and claim_ok:
            move_attempt(
                uow, self._clock, attempt, AttemptState.SUCCEEDED, EventKind.ATTEMPT_SUCCEEDED
            )
        elif exit_class is ExitClass.BLOCKED:
            attempt.blocked_reason = blocked_reason
            attempt.blocked_statement = blocked_text
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.BLOCKED,
                EventKind.ATTEMPT_BLOCKED,
                payload={"blocked_reason": blocked_reason} if blocked_reason else None,
            )
        else:
            if exit_class is ExitClass.COMPLETED and not claim_ok:
                attempt.exit_class = ExitClass.COMPLETED_WITHOUT_REPORT
                exit_class = attempt.exit_class
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.FAILED,
                EventKind.ATTEMPT_FAILED,
                payload={"exit_class": exit_class.value},
            )
        common = {"execution_id": execution.id, "attempt_id": attempt.id}
        if task.state in ENDS_ATTEMPTS:
            self._finish_cancelling(uow, task)
            return
        if attempt.state is AttemptState.SUCCEEDED:
            move_execution(
                uow, self._clock, execution, ExecutionState.SUCCEEDED, EventKind.EXECUTION_SUCCEEDED
            )
            self._task_reported(uow, task, attempt, exit_class, common)
            return
        if attempt.state is AttemptState.BLOCKED:
            move_task(
                uow,
                self._clock,
                task,
                TaskState.BLOCKED,
                EventKind.TASK_BLOCKED,
                payload={
                    **common,
                    "exit_class": exit_class.value,
                    "blocked_md": blocked_text,
                    "blocked_reason": blocked_reason,
                },
                **common,
            )
            # 09: entering `blocked` opens an escalation and creates a wake. hades #393:
            # the escalation carries the worker's reason and its statement verbatim, and
            # nothing retries the attempt; the answer comes back as a decision or a
            # correction.
            open_escalation(
                uow,
                self._clock,
                task=task,
                attempt_id=attempt.id,
                question=blocked_text or BLOCKED_WITHOUT_STATEMENT,
                reason=blocked_reason,
            )
            return
        if local_cap is not None:
            reason = f"too_big_for_local:{local_cap}"
            move_task(
                uow,
                self._clock,
                task,
                TaskState.BLOCKED,
                EventKind.TASK_BLOCKED,
                payload={**common, "exit_class": exit_class.value, "blocked_md": reason},
                **common,
            )
            open_escalation(
                uow,
                self._clock,
                task=task,
                attempt_id=attempt.id,
                question=reason,
                summary=too_big_wake_summary(local_cap),
            )
            return
        if exit_class is ExitClass.INFRASTRUCTURE:
            self._retry_interruption(uow, task, execution, attempt)
            return
        if exit_class is ExitClass.QUOTA_EXHAUSTED:
            # Reactive rerouting is only for a worker that actually ran. A reserve-time
            # refusal has no worktree to checkpoint and follows the established
            # quota-exhausted report path.
            if attempt.started_at is None:
                move_execution(
                    uow,
                    self._clock,
                    execution,
                    ExecutionState.FAILED,
                    EventKind.EXECUTION_FAILED,
                    payload={"exit_class": exit_class.value, "phase": "reserve"},
                )
                self._task_reported(uow, task, attempt, exit_class, common)
                return
            if defer_quota and has_commits:
                # The checkpoint push decides the rest later (16 step 1); the pool's
                # fact is already true, so a mark that opened is told now.
                if pool_mark is not None and pool_mark[1]:
                    self._wake_pool_exhausted(uow, task, attempt, pool_mark[0])
                return
            self._handle_quota_exit(uow, task, execution, attempt, pool_mark=pool_mark)
            return
        retryable = retryable_exit(exit_class, execution.retry_on)
        if attempt.termination_reason == TERMINATION_REFUSED:
            # 07: a refused launch would be refused again; Foundry has the wake.
            retryable = False
        # hades #393: a blocked attempt is a question, not a failure, so it consumes no
        # retry when a decision schedules the same execution again.
        ordinary_attempts = sum(
            prior.exit_class
            not in {ExitClass.QUOTA_EXHAUSTED, ExitClass.INFRASTRUCTURE, ExitClass.BLOCKED}
            and prior.termination_reason not in {"gate_proves_nothing", "check_cannot_run"}
            for prior in uow.attempts.list_for_execution(execution.id)
            if prior.state in ATTEMPT_TERMINAL
        )
        if retryable and ordinary_attempts < execution.max_attempts:
            nxt = self._create_attempt(uow, execution, number=attempt.number + 1)
            move_task(
                uow,
                self._clock,
                task,
                TaskState.SCHEDULED,
                EventKind.TASK_RETRY_SCHEDULED,
                payload={
                    **common,
                    "exit_class": exit_class.value,
                    "next_attempt_id": nxt.id,
                    "next_attempt_number": nxt.number,
                    "max_attempts": execution.max_attempts,
                },
                **common,
            )
            return
        move_execution(
            uow,
            self._clock,
            execution,
            ExecutionState.FAILED,
            EventKind.EXECUTION_FAILED,
            payload={
                "exit_class": exit_class.value,
                "attempts_used": ordinary_attempts,
                "max_attempts": execution.max_attempts,
                "retry_eligible": retryable,
            },
        )
        self._task_reported(uow, task, attempt, exit_class, common, wake_summary=wake_summary)

    def _infrastructure_waits(self) -> list[tuple[str, str, str]]:
        pending: list[tuple[str, str, str]] = []
        with self._uow_factory() as uow:
            for task in uow.tasks.list_by_state(TaskState.BLOCKED):
                event = uow.events.latest_for_task_kind(task.id, EventKind.TASK_BLOCKED.value)
                if event is None or event.payload.get("reason") != "model_endpoint_unavailable":
                    continue
                if not event.payload.get("health_retry_allowed"):
                    continue
                if event.payload.get("never_started") or not event.payload.get("endpoint_url"):
                    continue
                if task.resume_at is not None and task.resume_at > self._clock.now():
                    continue
                execution = uow.executions.get(event.execution_id or "")
                if execution is not None and execution.state is ExecutionState.ACTIVE:
                    pending.append(
                        (task.id, execution.provider, str(event.payload["endpoint_url"]))
                    )
        return pending

    async def _resume_infrastructure_waits(self) -> None:
        results: dict[tuple[str, str], bool] = {}
        for task_id, provider_name, endpoint in await self._db(self._infrastructure_waits):
            probe = getattr(self._provider(provider_name), "probe_model_endpoint", None)
            if probe is None:
                continue
            key = (provider_name, endpoint)
            if key not in results:
                results[key] = await probe(endpoint)
            await self._db(partial(self._record_endpoint_health, task_id, results[key]))

    def _record_endpoint_health(self, task_id: str, healthy: bool) -> None:
        with self._fenced() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            if task is None or task.state is not TaskState.BLOCKED:
                return
            event = uow.events.latest_for_task_kind(task.id, EventKind.TASK_BLOCKED.value)
            if event is None or event.payload.get("reason") != "model_endpoint_unavailable":
                return
            if not event.payload.get("health_retry_allowed"):
                return
            task.resume_at = None if healthy else self._clock.now() + timedelta(minutes=3)
            uow.tasks.save(task)
            if healthy:
                move_task(
                    uow,
                    self._clock,
                    task,
                    TaskState.SCHEDULED,
                    EventKind.TASK_RETRY_SCHEDULED,
                    payload={
                        "health_recovered": True,
                        "contract_version": task.contract_version,
                        "endpoint_url": event.payload.get("endpoint_url"),
                    },
                )
            uow.commit()

    def _retry_interruption(
        self, uow: UnitOfWork, task: Task, execution: Execution, attempt: Attempt
    ) -> None:
        event = uow.events.latest_for_task_kind(task.id, EventKind.ATTEMPT_EXITED.value)
        detail = event.payload if event is not None and event.attempt_id == attempt.id else {}
        message = str(detail.get("interruption_message") or "model endpoint unavailable")
        contract = uow.contracts.get(task.id, execution.contract_version)
        assert contract is not None
        version_events = [
            row
            for row in self._all_task_events(uow, task.id)
            if row.payload.get("contract_version") == execution.contract_version
        ]
        earlier_blocks = sum(
            row.kind == EventKind.TASK_BLOCKED.value
            and row.payload.get("reason") == "model_endpoint_unavailable"
            for row in version_events
        )
        # Quota exits reroute under their own budget (reroute_max); only interruptions
        # of the infrastructure itself count against this one.
        failures = sum(
            row.exit_class is ExitClass.INFRASTRUCTURE and row.created_at >= contract.submitted_at
            for row in uow.attempts.list_for_task(task.id)
        )
        if failures >= INFRASTRUCTURE_RETRY_BUDGET:
            routing = load_attempt_routing(
                uow, execution.policy_snapshot or {}, attempt.routing_version
            )
            model = (
                routing.model(
                    attempt.selected_model or execution.model,
                    attempt.selected_harness or execution.harness,
                )
                if routing
                else None
            )
            endpoint_url = model.endpoint_url if model is not None else None
            # The first block waits for the endpoint's health probe and may resume on
            # its own once; the second needs a person and opens the one escalation.
            first_block = earlier_blocks == 0
            task.resume_at = self._clock.now() + timedelta(minutes=3) if first_block else None
            uow.tasks.save(task)
            move_task(
                uow,
                self._clock,
                task,
                TaskState.BLOCKED,
                EventKind.TASK_BLOCKED,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "reason": "model_endpoint_unavailable",
                    "contract_version": execution.contract_version,
                    "health_retry_allowed": first_block,
                    "cause": message,
                    "never_started": bool(detail.get("never_started")),
                    "endpoint_url": endpoint_url
                    if attempt.exit_class is ExitClass.INFRASTRUCTURE
                    and not detail.get("capacity_refused")
                    else None,
                },
            )
            summary = (
                f"blocked: model_endpoint_unavailable after {failures} infrastructure "
                f"interruptions; {message}"
            )[:2000]
            if first_block:
                create_wake(
                    uow,
                    self._clock,
                    principal_id=task.principal_id,
                    reason=WakeReason.ATTEMPT_FAILED,
                    summary=summary,
                    task=task,
                    attempt_id=attempt.id,
                )
            else:
                open_escalation(
                    uow,
                    self._clock,
                    task=task,
                    attempt_id=attempt.id,
                    question=message,
                    summary=summary,
                    wake_reason=WakeReason.ATTEMPT_FAILED,
                )
            return
        task.resume_at = self._clock.now() + timedelta(minutes=3)
        uow.tasks.save(task)
        # The same resume rule as any further attempt (_create_attempt): a checkpoint
        # already pushed to the work branch is where the retry starts; otherwise the
        # launch resumes from the last sealed bundle.
        nxt = self._create_attempt(uow, execution, number=attempt.number + 1)
        move_task(
            uow,
            self._clock,
            task,
            TaskState.SCHEDULED,
            EventKind.TASK_RETRY_SCHEDULED,
            execution_id=execution.id,
            attempt_id=attempt.id,
            payload={
                "exit_class": str(attempt.exit_class),
                "cause": message,
                "next_attempt_id": nxt.id,
                "resume_at": task.resume_at.isoformat(),
                "excluded_harness": (attempt.selected_harness or execution.harness)
                if detail.get("capacity_refused")
                else None,
                "excluded_model": (attempt.selected_model or execution.model)
                if detail.get("capacity_refused")
                else None,
            },
        )

    @staticmethod
    def _local_cap(
        uow: UnitOfWork,
        execution: Execution,
        attempt: Attempt,
        exit_class: ExitClass,
        turn_cap_reached: bool,
    ) -> Literal["turns", "time"] | None:
        routing = load_attempt_routing(
            uow, execution.policy_snapshot or {}, attempt.routing_version
        )
        model = attempt.selected_model or execution.model
        entry = (
            routing.model(model, attempt.selected_harness or execution.harness)
            if routing is not None
            else None
        )
        return local_cap_kind(
            entry.endpoint if entry is not None else None, exit_class, turn_cap_reached
        )

    def _finish_failed_review(
        self, uow: UnitOfWork, attempt: Attempt, execution: Execution, task: Task
    ) -> None:
        """A review execution that produced no ReviewReportV1 ends without moving the task.

        09: a review execution's outcome does not change task state by itself. The task
        stays in `awaiting_internal_review` and Foundry is woken, so the review can be
        asked for again rather than the tick failing on an illegal transition forever."""
        exit_class = attempt.exit_class or ExitClass.UNKNOWN
        if attempt.state not in ATTEMPT_TERMINAL:
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.FAILED,
                EventKind.ATTEMPT_FAILED,
                payload={"role": "review", "exit_class": exit_class.value},
            )
        if execution.state not in EXECUTION_TERMINAL:
            move_execution(
                uow,
                self._clock,
                execution,
                ExecutionState.FAILED,
                EventKind.EXECUTION_FAILED,
                payload={"role": "review", "exit_class": exit_class.value},
            )
        if task.state in ENDS_ATTEMPTS:
            self._finish_cancelling(uow, task)
            return
        create_wake(
            uow,
            self._clock,
            principal_id=task.principal_id,
            reason=WakeReason.ATTEMPT_FAILED,
            summary=(
                f"the review execution ended {exit_class.value} without a ReviewReportV1; "
                f"{task.head_sha} still has no non-author review"
            ),
            task=task,
            attempt_id=attempt.id,
            extra_links={"review": f"/v1/tasks/{task.id}/review"},
        )

    # Exit classes that wake Foundry once no retry remains (17).
    _FAILURE_WAKE_REASONS: ClassVar[dict[ExitClass, WakeReason]] = {
        ExitClass.TIMEOUT: WakeReason.TIMED_OUT,
        ExitClass.STALLED: WakeReason.TIMED_OUT,
        ExitClass.LOST: WakeReason.LOST,
        ExitClass.AUTH_FAILURE: WakeReason.AUTH_FAILURE,
        ExitClass.QUOTA_EXHAUSTED: WakeReason.QUOTA_EXHAUSTED,
    }

    def _task_reported(
        self,
        uow: UnitOfWork,
        task: Task,
        attempt: Attempt,
        exit_class: ExitClass,
        common: dict[str, str],
        wake_summary: str | None = None,
    ) -> None:
        stored = uow.contracts.get(task.id, task.contract_version)
        tier = (
            stored.document.get("execution_request", {}).get("tier") if stored is not None else None
        )
        move_task(
            uow,
            self._clock,
            task,
            TaskState.REPORTED,
            EventKind.TASK_REPORTED,
            payload={
                **common,
                "exit_class": exit_class.value,
                "attempt_state": attempt.state.value,
                "head_sha": task.head_sha,
                "tier": tier,
            },
            **common,
        )
        if attempt.state is AttemptState.FAILED:
            reason = self._FAILURE_WAKE_REASONS.get(exit_class, WakeReason.ATTEMPT_FAILED)
            create_wake(
                uow,
                self._clock,
                principal_id=task.principal_id,
                reason=reason,
                summary=(
                    wake_summary
                    or (
                        f"attempt {attempt.number} ended {exit_class.value}"
                        + (
                            f" ({attempt.termination_detail})"
                            if exit_class is ExitClass.STALLED and attempt.termination_detail
                            else ""
                        )
                        + " with no retry remaining; the pre-PR gates will say so"
                    )
                ),
                task=task,
                attempt_id=attempt.id,
            )

    def _finish_cancelling(self, uow: UnitOfWork, task: Task) -> None:
        """Once no attempt is live, close open executions; a cancelling task becomes cancelled.
        A merged task stays merged (hades #360)."""
        attempts = uow.attempts.list_for_task(task.id)
        # An unsupervised attempt (15) has no worker to wait for; it stays as the record
        # of a run Crucible never observed, and the cancellation settles around it.
        if any(a.state not in ATTEMPT_TERMINAL and not a.unsupervised for a in attempts):
            return
        for e in uow.executions.list_for_task(task.id):
            if e.state in (ExecutionState.CREATED, ExecutionState.ACTIVE):
                move_execution(
                    uow, self._clock, e, ExecutionState.CANCELLED, EventKind.EXECUTION_CANCELLED
                )
        if task.state is TaskState.CANCELLING:
            move_task(uow, self._clock, task, TaskState.CANCELLED, EventKind.TASK_CANCELLED)

    # ----- step: cancellations -----------------------------------------

    def _list_cancel_work(self) -> list[_CancelWork]:
        """Live attempts of cancelling, cancelled or merged tasks, plus tasks whose
        executions can close."""
        out: list[_CancelWork] = []
        with self._uow_factory() as uow:
            for state in ENDS_ATTEMPTS:
                for task in uow.tasks.list_by_state(state):
                    attempts = uow.attempts.list_for_task(task.id)
                    live = [
                        a
                        for a in attempts
                        if a.state not in ATTEMPT_TERMINAL and not a.unsupervised
                    ]
                    out.extend(_CancelWork(task_id=task.id, attempt=a) for a in live)
                    open_exec = any(
                        e.state in (ExecutionState.CREATED, ExecutionState.ACTIVE)
                        for e in uow.executions.list_for_task(task.id)
                    )
                    if not live and (open_exec or state is TaskState.CANCELLING):
                        out.append(_CancelWork(task_id=task.id, attempt=None))
        return out

    async def _sweep_cancellations(self) -> None:
        for work in await self._db(self._list_cancel_work):
            attempt = work.attempt
            if attempt is None:
                await self._db(partial(self._settle_cancelled_task, work.task_id))
                continue
            if attempt.id in self._collects or attempt.id in self._collect_retry_at:
                # Its worker has exited and its collection is running or waiting to run
                # again; the attempt finishes on its own and the task settles later.
                continue
            with log_context(
                task_id=attempt.task_id, execution_id=attempt.execution_id, attempt_id=attempt.id
            ):
                if attempt.state is AttemptState.RUNNING:
                    provider_name = await self._db(partial(self._execution_provider_name, attempt))
                    await self._provider(provider_name).terminate(
                        self._handle_for(attempt), "drain"
                    )
                    await self._db(partial(self._mark_terminating, attempt.id))
                elif attempt.state is AttemptState.PENDING:
                    await self._db(partial(self._cancel_pending, attempt.id))

    def _mark_terminating(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            if attempt.state is not AttemptState.RUNNING:
                return
            attempt.termination_reason = TERMINATION_CANCEL
            attempt.drain_deadline = self._clock.now() + timedelta(seconds=self.grace_seconds)
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.TERMINATING,
                EventKind.ATTEMPT_TERMINATING,
                payload={"mode": "drain", "drain_deadline": attempt.drain_deadline.isoformat()},
            )
            uow.commit()

    def _cancel_pending(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            if attempt.state is not AttemptState.PENDING:
                return
            attempt.exit_class = ExitClass.KILLED
            attempt.termination_reason = TERMINATION_CANCEL
            attempt.ended_at = self._clock.now()
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={"reason": "task cancelled before launch"},
            )
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.FAILED,
                EventKind.ATTEMPT_FAILED,
                payload={"exit_class": ExitClass.KILLED.value},
            )
            task = uow.tasks.get(attempt.task_id, for_update=True)
            assert task is not None
            self._finish_cancelling(uow, task)
            uow.commit()

    def _settle_cancelled_task(self, task_id: str) -> None:
        with self._fenced() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            if task is None or task.state not in ENDS_ATTEMPTS:
                return
            self._finish_cancelling(uow, task)
            uow.commit()

    # ----- step: status -------------------------------------------------

    def _status_step(self, started: float) -> dict[str, int]:
        with self._fenced() as uow:
            counts = {
                "tasks_scheduled": len(uow.tasks.list_by_state(TaskState.SCHEDULED)),
                "tasks_running": len(uow.tasks.list_by_state(TaskState.RUNNING)),
                "tasks_cancelling": len(uow.tasks.list_by_state(TaskState.CANCELLING)),
                "attempts_pending": len(uow.attempts.list_in_states([AttemptState.PENDING])),
                "attempts_live": len(
                    uow.attempts.list_in_states([AttemptState.RUNNING, AttemptState.TERMINATING])
                ),
                # 23: the delivery half's own depths, which `GET /supervisor` reports as
                # the GitHub observation status.
                "tasks_publishing": len(uow.tasks.list_by_state(TaskState.PUBLISHING)),
                "pull_requests_observed": len(
                    uow.pull_requests.list_in_states(
                        [PullRequestState.OPENING, PullRequestState.OPEN]
                    )
                ),
                "github_deliveries_pending": uow.github_deliveries.count_unprocessed(),
            }
            status = uow.supervisor_status.get()
            status.holder = self.holder
            status.last_tick_at = self._clock.now()
            status.last_success_at = status.last_tick_at
            status.last_error = None
            status.last_error_at = None
            status.tick_ms = int((time.monotonic() - started) * 1000)
            status.counts = counts
            uow.supervisor_status.write(status)
            uow.commit()
            return counts
