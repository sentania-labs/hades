"""Persistence ports. Repositories return domain entities; the unit of work owns
the transaction. Every state change and its event commit together."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol

from crucible.domain.entities import (
    AcceptanceResult,
    Artifact,
    Attempt,
    AttemptMetrics,
    BootstrapImport,
    CICertification,
    CIDecision,
    CompletionClaimRecord,
    Decision,
    Device,
    Escalation,
    Event,
    EvidenceRecord,
    Execution,
    ExecutionRole,
    ExternalReview,
    ExternalReviewCycle,
    GateResultRecord,
    GitHubDelivery,
    GitHubManifestState,
    HarnessImage,
    HarnessState,
    Heartbeat,
    Lease,
    LedgerDecision,
    LogChunkRecord,
    MemoryItem,
    MinionQuestion,
    Persona,
    Policy,
    PoolExhaustion,
    Principal,
    ProviderSetting,
    PullRequest,
    PullRequestHead,
    PullRequestState,
    Reaction,
    Repository,
    RetentionAction,
    ReviewComment,
    ReviewDisposition,
    ReviewReportRecord,
    RoutingPolicyRecord,
    ScheduledJob,
    SupervisorStatus,
    Task,
    TaskContract,
    TaskNote,
    UiSession,
    Wake,
)
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.domain.rooms import Room, RoomKind, RoomTurn


class PrincipalRepository(Protocol):
    def get(self, principal_id: str) -> Principal | None: ...

    def get_by_name(self, name: str) -> Principal | None: ...

    def add(self, principal: Principal, token_salt: bytes, token_hash: bytes) -> None: ...

    def credentials(self, principal_id: str) -> tuple[bytes, bytes] | None: ...

    def rotate(self, principal_id: str, token_salt: bytes, token_hash: bytes) -> None: ...

    def list_all(self) -> Sequence[Principal]: ...

    def disable(self, principal_id: str, at: datetime) -> bool: ...

    def rename(self, principal_id: str, name: str) -> bool: ...


class PersonaRepository(Protocol):
    def add(self, persona: Persona) -> None: ...
    def get(self, persona_id: str) -> Persona | None: ...
    def list_all(self) -> Sequence[Persona]: ...
    def save(self, persona: Persona) -> None: ...
    def delete(self, persona_id: str) -> bool: ...


class ScheduledJobRepository(Protocol):
    def add(self, job: ScheduledJob) -> None: ...
    def get(self, job_id: str, *, for_update: bool = False) -> ScheduledJob | None: ...
    def list_all(self) -> Sequence[ScheduledJob]: ...
    def list_due(self, now: datetime) -> Sequence[ScheduledJob]: ...
    def save(self, job: ScheduledJob) -> None: ...
    def delete(self, job_id: str) -> bool: ...


class UiSessionRepository(Protocol):
    def create(self, session: UiSession) -> None: ...

    def get(self, session_id: str) -> UiSession | None: ...

    def delete(self, session_id: str) -> None: ...

    def delete_expired(self, now: datetime) -> int: ...

    def touch(self, session_id: str, last_seen_at: datetime) -> None: ...


class DeviceRepository(Protocol):
    """hades #576 (U9): devices by their principal's id."""

    def add(self, device: Device) -> None: ...

    def get(self, principal_id: str) -> Device | None: ...

    def get_by_name(self, name: str) -> Device | None: ...

    def list_all(self) -> Sequence[Device]: ...

    def record_use(self, principal_id: str, at: datetime, user_agent: str | None) -> None: ...

    def mark_exchanged(self, principal_id: str, at: datetime, user_agent: str | None) -> bool:
        """Set the one exchange for a UI session; False when it was already taken."""
        ...

    def revoke(self, principal_id: str, at: datetime, by: str) -> bool: ...


class RepositoryRegistry(Protocol):
    def get_by_name(self, name: str) -> Repository | None: ...

    def get(self, repository_id: str) -> Repository | None: ...

    def upsert(self, repository: Repository) -> Repository: ...

    def list_all(self) -> Sequence[Repository]: ...

    def remove(self, name: str) -> bool: ...


class PolicyRepository(Protocol):
    def get(self, name: str, version: int) -> Policy | None: ...

    def put(self, policy: Policy) -> Policy: ...

    def list_versions(self, name: str) -> Sequence[Policy]: ...

    def list_names(self) -> Sequence[str]:
        """Distinct policy names sorted."""
        ...

    def is_referenced(self, name: str, version: int) -> bool:
        """True once a task names this version; a referenced version is immutable (05b)."""
        ...


class RoutingPolicyRepository(Protocol):
    def get(self, name: str, version: int) -> RoutingPolicyRecord | None: ...

    def put(self, policy: RoutingPolicyRecord) -> RoutingPolicyRecord: ...

    def list_versions(self, name: str) -> Sequence[RoutingPolicyRecord]: ...

    def is_referenced(self, name: str, version: int) -> bool: ...


class PoolExhaustionRepository(Protocol):
    def get(self, pool: str, *, for_update: bool = False) -> PoolExhaustion | None: ...
    def put(self, mark: PoolExhaustion) -> PoolExhaustion: ...
    def list_all(self) -> Sequence[PoolExhaustion]: ...
    def clear(
        self, pool: str, *, at: datetime, principal: str, reason: str
    ) -> PoolExhaustion | None: ...


class TaskRepository(Protocol):
    def lock_work_branch(self, repository_id: str, work_branch: str) -> None:
        """Serialize ownership checks until transaction end, before reading owners."""
        ...

    def add(self, task: Task) -> None: ...

    def get(self, task_id: str, *, for_update: bool = False) -> Task | None: ...

    def get_by_external_id(self, principal_id: str, external_id: str) -> Task | None: ...

    def save(self, task: Task) -> None: ...

    def reassign(self, task_id: str, principal_id: str) -> None:
        """Move a task to another principal; only a bootstrap discard does (ADR 0029)."""
        ...

    def list_by_state(self, state: TaskState, *, for_update: bool = False) -> Sequence[Task]: ...

    def ids_for_principals(self, principal_ids: Sequence[str]) -> Sequence[str]: ...

    def count_by_state(
        self,
        *,
        principal_id: str | None = None,
        principal_ids: Sequence[str] | None = None,
    ) -> Mapping[TaskState, int]: ...

    def list_in_states(
        self, states: Sequence[TaskState], *, principal_id: str | None = None
    ) -> Sequence[Task]: ...

    def search(
        self,
        *,
        state: TaskState | None,
        project: str | None,
        repository_id: str | None,
        external_id: str | None,
        updated_since: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> Sequence[Task]: ...

    def recently_updated(
        self, *, since: datetime, limit: int, exclude_principal_ids: set[str] | None = None
    ) -> Sequence[Task]:
        """The newest updates first, at most `limit` of them."""
        ...


class ContractRepository(Protocol):
    def add(self, contract: TaskContract) -> None: ...

    def get(self, task_id: str, version: int) -> TaskContract | None: ...

    def list_for_task(self, task_id: str) -> Sequence[TaskContract]: ...


class ExecutionRepository(Protocol):
    def add(self, execution: Execution) -> None: ...

    def list_for_task_by_role(self, task_id: str, role: ExecutionRole) -> Sequence[Execution]: ...

    def get(self, execution_id: str, *, for_update: bool = False) -> Execution | None: ...

    def save(self, execution: Execution) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[Execution]: ...

    def list_by_state(self, state: ExecutionState) -> Sequence[Execution]: ...


class AttemptRepository(Protocol):
    def add(self, attempt: Attempt) -> None: ...

    def get(self, attempt_id: str, *, for_update: bool = False) -> Attempt | None: ...

    def save(self, attempt: Attempt) -> None: ...

    def list_for_execution(self, execution_id: str) -> Sequence[Attempt]: ...

    def list_for_task(self, task_id: str) -> Sequence[Attempt]: ...

    def routes_with(self, routing_name: str, routing_version: int) -> bool:
        """hades #254: True once an attempt recorded this routing policy version as the
        one it was routed with; such a version is immutable."""
        ...

    def list_in_states(
        self, states: Sequence[AttemptState], *, for_update: bool = False
    ) -> Sequence[Attempt]:
        """The attempts the supervisor and the status views scan. An unsupervised attempt
        (15: imported from the bootstrap ledger, no worker behind it) is never among
        them; `list_for_task` and `list_for_execution` still return it."""
        ...

    def worker_rows(self, *, principal_id: str | None = None) -> Sequence[Mapping[str, Any]]: ...

    def concurrency_by_harness(self) -> Mapping[str, int]: ...

    def list_cleaned_unreleased(self, retention_kind: str, *, limit: int) -> Sequence[Attempt]:
        """Supervised attempts whose cleanup kept their workspace (the cleanup event's
        `workspace` was not `delete`) and that have no retention action of
        `retention_kind` yet: the kept workspaces the retention step has still to judge
        (16), oldest cleanup first, at most `limit`."""
        ...


class EventRepository(Protocol):
    def append(self, event: Event) -> Event: ...

    def latest_for_task_kind(self, task_id: str, kind: str) -> Event | None:
        """The most recent event of one kind, so a busy task's request is still found."""
        ...

    def latest_for_tasks_kinds(
        self, task_ids: Sequence[str], kinds: Sequence[str]
    ) -> Mapping[tuple[str, str], Event]: ...

    def list_for_task(self, task_id: str, *, after_seq: int, limit: int) -> Sequence[Event]: ...

    def list_global(
        self, *, after_seq: int, kind: str | None, since: datetime | None, limit: int
    ) -> Sequence[Event]: ...


class LeaseRepository(Protocol):
    def acquire_supervisor(self, holder: str, now: datetime, ttl_seconds: int) -> Lease | None: ...

    def renew_supervisor(
        self, holder: str, fenced_token: int, now: datetime, ttl_seconds: int
    ) -> Lease | None: ...

    def verify_supervisor(self, holder: str, fenced_token: int) -> bool: ...

    def release_supervisor(self, holder: str, fenced_token: int) -> bool: ...

    def get_supervisor(self) -> Lease | None: ...

    def upsert_attempt_lease(
        self, attempt_id: str, holder: str, fenced_token: int, now: datetime, ttl_seconds: int
    ) -> Lease: ...

    def get_attempt_lease(self, attempt_id: str) -> Lease | None: ...

    def release_attempt_lease(self, attempt_id: str) -> None: ...

    def acquire_checkout_lease(
        self, key: str, holder: str, fenced_token: int, now: datetime, ttl_seconds: int
    ) -> Lease | None:
        """One attempt at a time per repository url and work branch (10). Returns None
        when a different, unexpired holder has it."""
        ...

    def get_checkout_lease(self, key: str) -> Lease | None: ...

    def release_checkout_lease(self, key: str, holder: str) -> bool: ...

    def list_checkout_leases(self) -> Sequence[Lease]: ...


class LogRepository(Protocol):
    def append(self, chunk: LogChunkRecord) -> LogChunkRecord: ...

    def last_offset(self, attempt_id: str) -> int: ...

    def count_for_attempt(self, attempt_id: str) -> int: ...

    def list_for_attempt(
        self, attempt_id: str, *, after_id: int = 0, limit: int = 500
    ) -> Sequence[LogChunkRecord]: ...

    def list_from_offset(
        self, attempt_id: str, *, offset: int, stream: str | None = None, limit: int = 500
    ) -> Sequence[LogChunkRecord]: ...

    def delete_for_attempts(self, attempt_ids: Sequence[str]) -> int: ...

    def attempts_with_logs_before(self, cutoff: datetime, limit: int) -> Sequence[str]:
        """Attempts whose newest stored chunk is older than the cutoff (16)."""
        ...


class HeartbeatRepository(Protocol):
    def append(self, heartbeat: Heartbeat) -> Heartbeat: ...

    def list_for_attempt(self, attempt_id: str, *, limit: int = 500) -> Sequence[Heartbeat]: ...

    def latest_signal(self, attempt_id: str) -> Heartbeat | None: ...

    def latest_activity(self, attempt_id: str) -> Heartbeat | None: ...


class RetentionRepository(Protocol):
    def record(self, action: RetentionAction) -> RetentionAction | None:
        """Write the action, or return None when this subject was already acted on:
        retention is idempotent and running it twice changes nothing (10)."""
        ...

    def list_recent(self, limit: int) -> Sequence[RetentionAction]: ...


class ClaimRepository(Protocol):
    def put(self, record: CompletionClaimRecord) -> None: ...

    def get(self, attempt_id: str) -> CompletionClaimRecord | None: ...


class ArtifactRepository(Protocol):
    def add(self, artifact: Artifact) -> None: ...

    def get(self, artifact_id: str) -> Artifact | None: ...

    def list_for_attempt(self, attempt_id: str) -> Sequence[Artifact]: ...

    def find_by_sha256(self, sha256: str, attempt_id: str | None) -> Artifact | None: ...


class EvidenceRepository(Protocol):
    def add(self, evidence: EvidenceRecord) -> EvidenceRecord: ...

    def list_for_attempt(self, attempt_id: str) -> Sequence[EvidenceRecord]: ...

    def list_for_task(self, task_id: str) -> Sequence[EvidenceRecord]: ...


class ReviewReportRepository(Protocol):
    def add(self, report: ReviewReportRecord) -> None: ...

    def supersede(self, report_id: str, at: datetime) -> None:
        """Mark one report superseded. The row is kept: 09 says a superseded result is
        history, not a deletion."""
        ...

    def get(self, report_id: str) -> ReviewReportRecord | None: ...

    def list_for_task(self, task_id: str) -> Sequence[ReviewReportRecord]: ...


class GateResultRepository(Protocol):
    def put(self, result: GateResultRecord) -> None: ...

    def list_for_attempt(self, attempt_id: str) -> Sequence[GateResultRecord]: ...

    def list_for_task(self, task_id: str) -> Sequence[GateResultRecord]: ...

    def list_for_tasks(self, task_ids: Sequence[str]) -> Sequence[GateResultRecord]: ...


class AcceptanceRepository(Protocol):
    def add(self, result: AcceptanceResult) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[AcceptanceResult]: ...

    def supersede_for_task(self, task_id: str, at: datetime) -> None: ...


class DecisionRepository(Protocol):
    def add(self, decision: Decision) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[Decision]: ...


class TaskNoteRepository(Protocol):
    """Operator notes on a task (hades #489), newest first when listed. `save` writes
    the delivery state the supervisor set from evidence (hades #208 item 2)."""

    def add(self, note: TaskNote) -> None: ...

    def get(self, note_id: str) -> TaskNote | None: ...

    def save(self, note: TaskNote) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[TaskNote]: ...


class MemoryRepository(Protocol):
    """The shared memory store (hades #208). Items are added and retired, never edited:
    `retire` sets `superseded_at`, and `superseded_by` when there is a replacement."""

    def add(self, item: MemoryItem) -> None: ...

    def get(self, item_id: str, *, for_update: bool = False) -> MemoryItem | None: ...

    def retire(self, item_id: str, *, superseded_by: str | None, at: datetime) -> None: ...

    def recall(
        self, *, tags: Sequence[str], keywords: Sequence[str], limit: int
    ) -> Sequence[MemoryItem]: ...

    def list_recent(
        self, *, limit: int, include_superseded: bool = False
    ) -> Sequence[MemoryItem]: ...


class DecisionLedgerRepository(Protocol):
    """The append-only decision ledger (hades #208): lines are added and listed. There
    is no save and no delete, and the table refuses both."""

    def add(self, decision: LedgerDecision) -> None: ...

    def list_recent(
        self, *, limit: int, channel: str | None = None
    ) -> Sequence[LedgerDecision]: ...


class MinionQuestionRepository(Protocol):
    """A worker's questions on a task (hades #208 item 2), oldest first when listed."""

    def add(self, question: MinionQuestion) -> None: ...

    def get(self, question_id: str, *, for_update: bool = False) -> MinionQuestion | None: ...

    def save(self, question: MinionQuestion) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[MinionQuestion]: ...


class RoomRepository(Protocol):
    """Rooms (hades #208, ADR 0031): one row per conversation, saved whole."""

    def add(self, room: Room) -> None: ...

    def get(self, room_id: str, *, for_update: bool = False) -> Room | None: ...

    def save(self, room: Room) -> None: ...

    def list_recent(
        self,
        *,
        limit: int,
        include_closed: bool = True,
        kind: RoomKind | None = None,
        created_by: str | None = None,
        card_task_id: str | None = None,
    ) -> Sequence[Room]: ...

    def list_live(self) -> Sequence[Room]:
        """The rooms whose runner is up or meant to be: starting, warm, interrupted."""
        ...


class RoomTurnRepository(Protocol):
    """A room's transcript, in seq order. Hades writes a turn before any runner sees
    it; an assistant turn is saved as its reply streams in."""

    def add(self, turn: RoomTurn) -> None: ...

    def get(self, room_id: str, seq: int, *, for_update: bool = False) -> RoomTurn | None: ...

    def save(self, turn: RoomTurn) -> None: ...

    def last_seq(self, room_id: str) -> int:
        """The highest seq in the room, 0 when it has no turns."""
        ...

    def count(self, room_id: str) -> int: ...

    def list_for_room(
        self, room_id: str, *, after_seq: int = 0, limit: int | None = None
    ) -> Sequence[RoomTurn]:
        """Turns with seq above `after_seq`, oldest first; with `limit`, the newest
        `limit` of them (still oldest first)."""
        ...


class EscalationRepository(Protocol):
    def add(self, escalation: Escalation) -> None: ...

    def get(self, escalation_id: str, *, for_update: bool = False) -> Escalation | None: ...

    def save(self, escalation: Escalation) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[Escalation]: ...

    def list_open(self) -> Sequence[Escalation]: ...


class DispositionRepository(Protocol):
    def add(self, disposition: ReviewDisposition) -> None: ...

    def get_by_comment(
        self, review_comment_id: str, comment_body_sha256: str | None = None
    ) -> ReviewDisposition | None: ...

    def list_for_comments(
        self,
        comment_ids: Sequence[str],
        comment_body_sha256_by_comment: Mapping[str, str] | None = None,
    ) -> Sequence[ReviewDisposition]: ...


# ----- C4: GitHub delivery (14, 23) --------------------------------------


class PullRequestRepository(Protocol):
    def add(self, pull_request: PullRequest) -> None: ...

    def get(self, pull_request_id: str, *, for_update: bool = False) -> PullRequest | None: ...

    def get_for_task(self, task_id: str, *, for_update: bool = False) -> PullRequest | None: ...

    def save(self, pull_request: PullRequest) -> None: ...

    def list_in_states(self, states: Sequence[PullRequestState]) -> Sequence[PullRequest]: ...


class PullRequestHeadRepository(Protocol):
    def add(self, head: PullRequestHead) -> bool:
        """True when this (pull request, sha) is new; False when it is already recorded."""
        ...

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[PullRequestHead]: ...


class ExternalReviewCycleRepository(Protocol):
    def add(self, cycle: ExternalReviewCycle) -> None: ...

    def save(self, cycle: ExternalReviewCycle) -> None: ...

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[ExternalReviewCycle]: ...


class ExternalReviewRepository(Protocol):
    def add(self, review: ExternalReview) -> bool:
        """True when stored; False when this signal was already recorded."""
        ...

    def get_by_github(
        self, pull_request_id: str, signal: str, github_id: str
    ) -> ExternalReview | None: ...

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[ExternalReview]: ...


class ReviewCommentRepository(Protocol):
    def add(self, comment: ReviewComment) -> bool:
        """True when stored; False when this comment was already recorded."""
        ...

    def save(self, comment: ReviewComment) -> None: ...

    def get(self, comment_id: str) -> ReviewComment | None: ...

    def get_by_github(
        self, pull_request_id: str, kind: str, github_id: str
    ) -> ReviewComment | None: ...

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[ReviewComment]: ...


class ReactionRepository(Protocol):
    def add(self, reaction: Reaction) -> bool:
        """True when stored; False when this reaction was already recorded."""
        ...

    def save(self, reaction: Reaction) -> None: ...

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[Reaction]: ...


class CICertificationRepository(Protocol):
    def put(self, certification: CICertification) -> CICertification: ...

    def get_for_head(self, pull_request_id: str, head_sha: str) -> CICertification | None: ...

    def list_for_task(self, task_id: str) -> Sequence[CICertification]: ...


class CIDecisionRepository(Protocol):
    def add(self, decision: CIDecision) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[CIDecision]: ...


class GitHubDeliveryRepository(Protocol):
    def add(self, delivery: GitHubDelivery) -> bool:
        """True when stored; False when this delivery id was already seen (04)."""
        ...

    def get(self, delivery_id: str) -> GitHubDelivery | None: ...

    def list_unprocessed(self, limit: int = 100) -> Sequence[GitHubDelivery]: ...

    def count_unprocessed(self) -> int: ...

    def mark_processed(self, delivery_id: str, at: datetime) -> None: ...


class WakeRepository(Protocol):
    def add(self, wake: Wake) -> None: ...

    def get(self, wake_id: str, *, for_update: bool = False) -> Wake | None: ...

    def save(self, wake: Wake) -> None: ...

    def list_for_principal(
        self,
        principal_id: str,
        *,
        since: datetime | None,
        include_acked: bool,
        limit: int,
        after_id: str | None = None,
    ) -> Sequence[Wake]:
        """The principal's wakes in id order (ULIDs are unique and ordered). `after_id`
        resumes strictly after that wake, which is what the API's opaque cursor decodes
        to (hades #502); `since` is the older time filter and is still honoured."""
        ...

    def list_for_task(
        self, task_id: str, *, reason: str, include_acked: bool = True
    ) -> Sequence[Wake]:
        """Every wake raised for one task with one reason, oldest first. A repeating
        notice reads this to keep one open wake per task per cause (hades #502)."""
        ...

    def list_unacked_for_reasons(self, reasons: Sequence[str]) -> Sequence[Wake]:
        """Every unacked wake whose reason is one of `reasons`, oldest first. The
        supervisor closes the ones about a pull request that has since merged or
        closed (hades #502)."""
        ...

    def list_undelivered(self, now: datetime) -> Sequence[Wake]: ...

    def count_unacked(self) -> int: ...

    def count_unacked_for_principal(self, principal_id: str) -> int: ...

    def pending_summary(
        self, *, principal_id: str | None = None
    ) -> tuple[Mapping[str, int], datetime | None, int]: ...

    def list_acked_before(self, cutoff: datetime, limit: int) -> Sequence[Wake]:
        """Wakes acked before the cutoff, oldest first. The caller applies each one's
        own policy window and records a RetentionAction per deletion (16)."""
        ...

    def delete(self, wake_id: str) -> bool: ...


class AttemptMetricsRepository(Protocol):
    def put(self, metrics: AttemptMetrics) -> None: ...

    def get(self, attempt_id: str) -> AttemptMetrics | None: ...

    def list_since(
        self, *, since: datetime | None, model: str | None, task_ids: Sequence[str] | None
    ) -> Sequence[AttemptMetrics]: ...

    def recent_for_project(
        self, *, project: str, models: Sequence[str], limit_per_model: int
    ) -> Sequence[AttemptMetrics]:
        """The newest metrics per model for one project, without paging through tasks."""
        ...


class SupervisorStatusRepository(Protocol):
    def get(self) -> SupervisorStatus: ...

    def write(self, status: SupervisorStatus) -> None: ...


class HarnessStateRepository(Protocol):
    """The runtime record per harness (25): enable flag, compatibility, observations.
    `for_update` locks the row until the unit of work ends, so a read-then-write (the
    harness test's running marker, issue 147) is one claim across api replicas."""

    def get(self, name: str, *, for_update: bool = False) -> HarnessState | None: ...

    def list_all(self) -> Sequence[HarnessState]: ...

    def put(self, state: HarnessState) -> HarnessState: ...


class HarnessImageRepository(Protocol):
    """Each harness's default worker image (13, ADR 0018). None: never promoted."""

    def get(self, harness: str) -> HarnessImage | None: ...

    def list_all(self) -> Sequence[HarnessImage]: ...

    def put(self, image: HarnessImage) -> HarnessImage: ...


class ProviderSettingRepository(Protocol):
    """Runtime provider settings by name (25, crucible#91). None: never saved."""

    def get(self, name: str) -> ProviderSetting | None: ...

    def put(self, setting: ProviderSetting) -> ProviderSetting: ...


class GitHubManifestStateRepository(Protocol):
    """The starts of the GitHub App manifest flow (crucible#168), by the sha256 of their
    `state` value."""

    def add(self, state: GitHubManifestState) -> None: ...

    def consume(
        self, state_hash: str, now: datetime, *, principal: str, browser_hash: str
    ) -> GitHubManifestState | None:
        """The start as it was before this call, and marked used from now on when it
        was unused and `principal` and `browser_hash` are its own: a return that is not
        the starter's cannot spend it. None when there is no such start. Locks the row,
        so two returns with the same state cannot both find it unused."""
        ...

    def prune(self, before: datetime) -> int:
        """Delete every start that expired before `before`; how many went."""
        ...


class BootstrapImportRepository(Protocol):
    """The imports of 15, newest first when listed."""

    def add(self, record: BootstrapImport) -> None: ...

    def get(self, import_id: str, *, for_update: bool = False) -> BootstrapImport | None: ...

    def get_by_content(self, content_sha256: str) -> BootstrapImport | None: ...

    def save(self, record: BootstrapImport) -> None: ...

    def list_all(self) -> Sequence[BootstrapImport]: ...

    def authoritative(self, *, for_update: bool = False) -> BootstrapImport | None: ...


class IdempotencyKeyTakenError(Exception):
    """The (principal, key) row already exists; read it back in a fresh transaction."""


class IdempotencyRepository(Protocol):
    def get(
        self, principal_id: str, key: str
    ) -> tuple[str, int | None, dict[str, Any] | None] | None:
        """(request_sha256, response_status, response_body); status is None while reserved."""
        ...

    def reserve(self, principal_id: str, key: str, *, request_sha256: str, now: datetime) -> None:
        """Insert the key row inside the current transaction; raises IdempotencyKeyTakenError
        once the conflicting row's transaction has committed."""
        ...

    def complete(
        self, principal_id: str, key: str, *, status: int, body: dict[str, Any]
    ) -> None: ...


class UnitOfWork(Protocol):
    principals: PrincipalRepository
    personas: PersonaRepository
    scheduled_jobs: ScheduledJobRepository
    ui_sessions: UiSessionRepository
    devices: DeviceRepository
    repositories: RepositoryRegistry
    policies: PolicyRepository
    tasks: TaskRepository
    contracts: ContractRepository
    executions: ExecutionRepository
    attempts: AttemptRepository
    events: EventRepository
    leases: LeaseRepository
    claims: ClaimRepository
    logs: LogRepository
    heartbeats: HeartbeatRepository
    retention: RetentionRepository
    supervisor_status: SupervisorStatusRepository
    idempotency: IdempotencyRepository
    routing_policies: RoutingPolicyRepository
    pool_exhaustions: PoolExhaustionRepository
    artifacts: ArtifactRepository
    evidence: EvidenceRepository
    review_reports: ReviewReportRepository
    gate_results: GateResultRepository
    acceptance: AcceptanceRepository
    decisions: DecisionRepository
    task_notes: TaskNoteRepository
    memory: MemoryRepository
    decision_ledger: DecisionLedgerRepository
    minion_questions: MinionQuestionRepository
    rooms: RoomRepository
    room_turns: RoomTurnRepository
    escalations: EscalationRepository
    dispositions: DispositionRepository
    wakes: WakeRepository
    attempt_metrics: AttemptMetricsRepository
    pull_requests: PullRequestRepository
    pull_request_heads: PullRequestHeadRepository
    review_cycles: ExternalReviewCycleRepository
    external_reviews: ExternalReviewRepository
    review_comments: ReviewCommentRepository
    reactions: ReactionRepository
    ci_certifications: CICertificationRepository
    ci_decisions: CIDecisionRepository
    github_deliveries: GitHubDeliveryRepository
    harnesses: HarnessStateRepository
    harness_images: HarnessImageRepository
    bootstrap_imports: BootstrapImportRepository
    provider_settings: ProviderSettingRepository
    github_manifest_states: GitHubManifestStateRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def set_fenced_token(self, fenced_token: int) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


class FencedTokenRejectedError(Exception):
    """The database refused a supervisor write because the fenced token is stale (10)."""


class AppendOnlyViolationError(Exception):
    """An UPDATE or DELETE hit an append-only table (14)."""
