"""SQLAlchemy unit of work. One transaction per unit; the supervisor sets its fenced
token with SET LOCAL at the start of every transaction, never per connection (14)."""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

from sqlalchemy import Engine, create_engine, delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from crucible.adapters.persistence.delivery import (
    CICertifications,
    CIDecisions,
    ExternalReviewCycles,
    ExternalReviews,
    GitHubDeliveries,
    PullRequestHeads,
    PullRequests,
    Reactions,
    ReviewComments,
)
from crucible.adapters.persistence.models import (
    AttemptRow,
    CompletionClaimRow,
    EventRow,
    ExecutionRow,
    HeartbeatRow,
    IdempotencyKeyRow,
    LeaseRow,
    LogChunkRow,
    PoolExhaustionRow,
    PrincipalRow,
    RepositoryRow,
    RetentionActionRow,
    SupervisorStatusRow,
    TaskContractRow,
    TaskRow,
    UiSessionRow,
)
from crucible.adapters.persistence.records import (
    Acceptances,
    Artifacts,
    AttemptMetricsRepo,
    BootstrapImports,
    DecisionLedger,
    Decisions,
    Dispositions,
    Escalations,
    Evidences,
    GateResults,
    GitHubManifestStates,
    HarnessImages,
    HarnessStates,
    MemoryItems,
    Policies,
    ProviderSettings,
    ReviewReports,
    Rooms,
    RoomTurns,
    RoutingPolicies,
    TaskNotes,
    Wakes,
)
from crucible.domain.entities import (
    Attempt,
    CompletionClaimRecord,
    Event,
    Execution,
    ExecutionRole,
    Heartbeat,
    Lease,
    LogChunkRecord,
    PoolExhaustion,
    Principal,
    Repository,
    RetentionAction,
    Role,
    SupervisorStatus,
    Task,
    TaskContract,
    UiSession,
)
from crucible.domain.exit_class import ExitClass
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.domain.time import ensure_utc
from crucible.ports.repository import (
    AcceptanceRepository,
    AppendOnlyViolationError,
    ArtifactRepository,
    AttemptMetricsRepository,
    AttemptRepository,
    BootstrapImportRepository,
    CICertificationRepository,
    CIDecisionRepository,
    ClaimRepository,
    ContractRepository,
    DecisionLedgerRepository,
    DecisionRepository,
    DispositionRepository,
    EscalationRepository,
    EventRepository,
    EvidenceRepository,
    ExecutionRepository,
    ExternalReviewCycleRepository,
    ExternalReviewRepository,
    FencedTokenRejectedError,
    GateResultRepository,
    GitHubDeliveryRepository,
    GitHubManifestStateRepository,
    HarnessImageRepository,
    HarnessStateRepository,
    HeartbeatRepository,
    IdempotencyKeyTakenError,
    IdempotencyRepository,
    LeaseRepository,
    LogRepository,
    MemoryRepository,
    PolicyRepository,
    PoolExhaustionRepository,
    PrincipalRepository,
    ProviderSettingRepository,
    PullRequestHeadRepository,
    PullRequestRepository,
    ReactionRepository,
    RepositoryRegistry,
    RetentionRepository,
    ReviewCommentRepository,
    ReviewReportRepository,
    RoomRepository,
    RoomTurnRepository,
    RoutingPolicyRepository,
    SupervisorStatusRepository,
    TaskNoteRepository,
    TaskRepository,
    UiSessionRepository,
    UnitOfWork,
    WakeRepository,
)

SUPERVISOR_LEASE_KIND = "supervisor"
SUPERVISOR_LEASE_KEY = "supervisor"
ATTEMPT_LEASE_KIND = "attempt"
CHECKOUT_LEASE_KIND = "checkout"
# 10: gzip a chunk above this size; the column is bytea either way.
GZIP_THRESHOLD_BYTES = 4096
FENCED_TOKEN_SETTING = "crucible.fenced_token"
FENCED_TOKEN_SQLSTATE = "CRU01"
APPEND_ONLY_SQLSTATE = "CRU02"


def translate_error(exc: BaseException) -> Exception | None:
    """Map the database's trigger errors onto port-level exceptions."""
    if not isinstance(exc, DBAPIError):
        return None
    sqlstate = getattr(exc.orig, "sqlstate", None)
    if sqlstate == FENCED_TOKEN_SQLSTATE:
        return FencedTokenRejectedError(str(exc.orig).splitlines()[0])
    if sqlstate == APPEND_ONLY_SQLSTATE:
        return AppendOnlyViolationError(str(exc.orig).splitlines()[0])
    return None


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, future=True)


def _dt(value: datetime | None) -> datetime | None:
    return ensure_utc(value) if value is not None else None


class Principals:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: PrincipalRow) -> Principal:
        return Principal(
            id=row.id,
            name=row.name,
            role=Role(row.role),
            created_at=ensure_utc(row.created_at),
            disabled_at=_dt(row.disabled_at),
        )

    def get(self, principal_id: str) -> Principal | None:
        row = self._s.get(PrincipalRow, principal_id)
        return self._to_entity(row) if row else None

    def get_by_name(self, name: str) -> Principal | None:
        row = self._s.scalar(select(PrincipalRow).where(PrincipalRow.name == name))
        return self._to_entity(row) if row else None

    def add(self, principal: Principal, token_salt: bytes, token_hash: bytes) -> None:
        self._s.add(
            PrincipalRow(
                id=principal.id,
                name=principal.name,
                role=principal.role.value,
                token_salt=token_salt,
                token_hash=token_hash,
                created_at=principal.created_at,
                disabled_at=principal.disabled_at,
            )
        )
        self._s.flush()

    def credentials(self, principal_id: str) -> tuple[bytes, bytes] | None:
        row = self._s.get(PrincipalRow, principal_id)
        if row is None or row.disabled_at is not None:
            return None
        return row.token_salt, row.token_hash

    def rotate(self, principal_id: str, token_salt: bytes, token_hash: bytes) -> None:
        self._s.execute(
            update(PrincipalRow)
            .where(PrincipalRow.id == principal_id)
            .values(token_salt=token_salt, token_hash=token_hash)
        )

    def list_all(self) -> Sequence[Principal]:
        rows = self._s.scalars(select(PrincipalRow).order_by(PrincipalRow.name)).all()
        return [self._to_entity(r) for r in rows]

    def disable(self, principal_id: str, at: datetime) -> bool:
        row = self._s.get(PrincipalRow, principal_id)
        if row is None or row.disabled_at is not None:
            return False
        row.disabled_at = at
        self._s.flush()
        return True

    def rename(self, principal_id: str, name: str) -> bool:
        row = self._s.get(PrincipalRow, principal_id)
        if row is None:
            return False
        row.name = name
        self._s.flush()
        return True


class UiSessions:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: UiSessionRow) -> UiSession:
        return UiSession(
            id=row.id,
            principal_id=row.principal_id,
            csrf=row.csrf,
            created_at=ensure_utc(row.created_at),
            expires_at=ensure_utc(row.expires_at),
            last_seen_at=ensure_utc(row.last_seen_at),
        )

    def create(self, session: UiSession) -> None:
        self._s.add(
            UiSessionRow(
                id=session.id,
                principal_id=session.principal_id,
                csrf=session.csrf,
                created_at=session.created_at,
                expires_at=session.expires_at,
                last_seen_at=session.last_seen_at,
            )
        )
        self._s.flush()

    def get(self, session_id: str) -> UiSession | None:
        row = self._s.get(UiSessionRow, session_id)
        return self._to_entity(row) if row else None

    def delete(self, session_id: str) -> None:
        self._s.execute(delete(UiSessionRow).where(UiSessionRow.id == session_id))

    def delete_expired(self, now: datetime) -> int:
        result = self._s.execute(delete(UiSessionRow).where(UiSessionRow.expires_at <= now))
        return result.rowcount  # type: ignore[attr-defined,no-any-return]

    def touch(self, session_id: str, last_seen_at: datetime) -> None:
        self._s.execute(
            update(UiSessionRow)
            .where(UiSessionRow.id == session_id)
            .values(last_seen_at=last_seen_at)
        )


class Repositories:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: RepositoryRow) -> Repository:
        return Repository(
            id=row.id,
            name=row.name,
            url=row.url,
            default_branch=row.default_branch,
            policy_name=row.policy_name,
            installation_id=row.installation_id,
            registered_by=row.registered_by,
            created_at=ensure_utc(row.created_at),
            external_review_attested=row.external_review_attested,
            attested_by=row.attested_by,
            attested_at=ensure_utc(row.attested_at) if row.attested_at else None,
            private=row.private,
            codex_review_refused_at=(
                ensure_utc(row.codex_review_refused_at) if row.codex_review_refused_at else None
            ),
        )

    def get_by_name(self, name: str) -> Repository | None:
        row = self._s.scalar(select(RepositoryRow).where(RepositoryRow.name == name))
        return self._to_entity(row) if row else None

    def list_all(self) -> Sequence[Repository]:
        rows = self._s.scalars(select(RepositoryRow).order_by(RepositoryRow.name)).all()
        return [self._to_entity(r) for r in rows]

    def remove(self, name: str) -> bool:
        row = self._s.scalar(select(RepositoryRow).where(RepositoryRow.name == name))
        if row is None:
            return False
        referenced = self._s.scalar(
            select(func.count(TaskRow.id)).where(TaskRow.repository_id == row.id)
        )
        if referenced:
            return False
        self._s.delete(row)
        self._s.flush()
        return True

    def get(self, repository_id: str) -> Repository | None:
        row = self._s.get(RepositoryRow, repository_id)
        return self._to_entity(row) if row else None

    def upsert(self, repository: Repository) -> Repository:
        row = self._s.scalar(select(RepositoryRow).where(RepositoryRow.name == repository.name))
        if row is None:
            row = RepositoryRow(
                id=repository.id,
                name=repository.name,
                created_at=repository.created_at,
                registered_by=repository.registered_by,
            )
            self._s.add(row)
        row.url = repository.url
        row.default_branch = repository.default_branch
        row.installation_id = repository.installation_id
        row.policy_name = repository.policy_name
        row.registered_by = repository.registered_by
        row.external_review_attested = repository.external_review_attested
        row.attested_by = repository.attested_by
        row.attested_at = repository.attested_at
        row.private = repository.private
        # hades #343: the marker is the provider's own repository configuration, not any
        # one registration's; `register_repository` builds a fresh `Repository` that
        # never carries it forward, so a routine PUT must not clear a stored refusal.
        # There is no explicit-clear operation yet, so a `None` here is never "clear" —
        # only a non-`None` value (recorded by `mark_codex_refusal_seen`) is applied.
        if repository.codex_review_refused_at is not None:
            row.codex_review_refused_at = repository.codex_review_refused_at
        self._s.flush()
        return self._to_entity(row)


class Tasks:
    def lock_work_branch(self, repository_id: str, work_branch: str) -> None:
        # A stable, namespaced key across processes. Hash collisions only serialize
        # unrelated branches; ownership is still checked using the full values.
        key = f"work-branch:{repository_id}:{work_branch}".encode()
        lock_id = int.from_bytes(hashlib.sha256(key).digest()[:8], "big", signed=True)
        self._s.execute(select(func.pg_advisory_xact_lock(lock_id)))

    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: TaskRow) -> Task:
        return Task(
            id=row.id,
            external_id=row.external_id,
            principal_id=row.principal_id,
            project=row.project,
            title=row.title,
            state=TaskState(row.state),
            contract_version=row.contract_version,
            policy_name=row.policy_name,
            policy_version=row.policy_version,
            repository_id=row.repository_id,
            created_at=ensure_utc(row.created_at),
            updated_at=ensure_utc(row.updated_at),
            closed_at=_dt(row.closed_at),
            head_sha=row.head_sha,
            resume_at=_dt(row.resume_at),
            quota_wait_started_at=_dt(row.quota_wait_started_at),
        )

    def add(self, task: Task) -> None:
        self._s.add(
            TaskRow(
                id=task.id,
                external_id=task.external_id,
                principal_id=task.principal_id,
                repository_id=task.repository_id,
                project=task.project,
                title=task.title,
                state=task.state.value,
                contract_version=task.contract_version,
                policy_name=task.policy_name,
                policy_version=task.policy_version,
                created_at=task.created_at,
                updated_at=task.updated_at,
                closed_at=task.closed_at,
                head_sha=task.head_sha,
                resume_at=task.resume_at,
                quota_wait_started_at=task.quota_wait_started_at,
            )
        )
        self._s.flush()

    def get(self, task_id: str, *, for_update: bool = False) -> Task | None:
        stmt = select(TaskRow).where(TaskRow.id == task_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = self._s.scalar(stmt)
        return self._to_entity(row) if row else None

    def get_by_external_id(self, principal_id: str, external_id: str) -> Task | None:
        row = self._s.scalar(
            select(TaskRow).where(
                TaskRow.principal_id == principal_id, TaskRow.external_id == external_id
            )
        )
        return self._to_entity(row) if row else None

    def save(self, task: Task) -> None:
        self._s.execute(
            update(TaskRow)
            .where(TaskRow.id == task.id)
            .values(
                state=task.state.value,
                contract_version=task.contract_version,
                updated_at=task.updated_at,
                closed_at=task.closed_at,
                head_sha=task.head_sha,
                resume_at=task.resume_at,
                quota_wait_started_at=task.quota_wait_started_at,
            )
        )

    def reassign(self, task_id: str, principal_id: str) -> None:
        self._s.execute(
            update(TaskRow).where(TaskRow.id == task_id).values(principal_id=principal_id)
        )

    def list_by_state(self, state: TaskState, *, for_update: bool = False) -> Sequence[Task]:
        stmt = select(TaskRow).where(TaskRow.state == state.value).order_by(TaskRow.id)
        if for_update:
            stmt = stmt.with_for_update()
        return [self._to_entity(r) for r in self._s.scalars(stmt).all()]

    def ids_for_principals(self, principal_ids: Sequence[str]) -> Sequence[str]:
        return list(
            self._s.scalars(select(TaskRow.id).where(TaskRow.principal_id.in_(principal_ids)))
        )

    def count_by_state(
        self,
        *,
        principal_id: str | None = None,
        principal_ids: Sequence[str] | None = None,
    ) -> Mapping[TaskState, int]:
        stmt = select(TaskRow.state, func.count()).group_by(TaskRow.state)
        if principal_id is not None:
            stmt = stmt.where(TaskRow.principal_id == principal_id)
        if principal_ids is not None:
            stmt = stmt.where(TaskRow.principal_id.in_(principal_ids))
        return {TaskState(state): count for state, count in self._s.execute(stmt)}

    def list_in_states(
        self, states: Sequence[TaskState], *, principal_id: str | None = None
    ) -> Sequence[Task]:
        stmt = select(TaskRow).where(TaskRow.state.in_([state.value for state in states]))
        if principal_id is not None:
            stmt = stmt.where(TaskRow.principal_id == principal_id)
        return [self._to_entity(row) for row in self._s.scalars(stmt.order_by(TaskRow.id)).all()]

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
    ) -> Sequence[Task]:
        stmt = select(TaskRow).order_by(TaskRow.id).limit(limit)
        if state is not None:
            stmt = stmt.where(TaskRow.state == state.value)
        if project is not None:
            stmt = stmt.where(TaskRow.project == project)
        if repository_id is not None:
            stmt = stmt.where(TaskRow.repository_id == repository_id)
        if external_id is not None:
            stmt = stmt.where(TaskRow.external_id == external_id)
        if updated_since is not None:
            stmt = stmt.where(TaskRow.updated_at >= updated_since)
        if after_id is not None:
            stmt = stmt.where(TaskRow.id > after_id)
        return [self._to_entity(r) for r in self._s.scalars(stmt).all()]

    def recently_updated(
        self,
        *,
        since: datetime,
        limit: int,
        exclude_principal_ids: set[str] | None = None,
    ) -> Sequence[Task]:
        stmt = (
            select(TaskRow)
            .where(TaskRow.updated_at >= since)
            .order_by(TaskRow.updated_at.desc(), TaskRow.id.desc())
        )
        if exclude_principal_ids:
            stmt = stmt.where(TaskRow.principal_id.notin_(exclude_principal_ids))
        stmt = stmt.limit(limit)
        return [self._to_entity(r) for r in self._s.scalars(stmt).all()]


class Contracts:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: TaskContractRow) -> TaskContract:
        return TaskContract(
            id=row.id,
            task_id=row.task_id,
            version=row.version,
            document=row.document,
            sha256=row.sha256,
            submitted_at=ensure_utc(row.submitted_at),
        )

    def add(self, contract: TaskContract) -> None:
        self._s.add(
            TaskContractRow(
                id=contract.id,
                task_id=contract.task_id,
                version=contract.version,
                document=contract.document,
                sha256=contract.sha256,
                submitted_at=contract.submitted_at,
            )
        )
        self._s.flush()

    def get(self, task_id: str, version: int) -> TaskContract | None:
        row = self._s.scalar(
            select(TaskContractRow).where(
                TaskContractRow.task_id == task_id, TaskContractRow.version == version
            )
        )
        return self._to_entity(row) if row else None

    def list_for_task(self, task_id: str) -> Sequence[TaskContract]:
        rows = self._s.scalars(
            select(TaskContractRow)
            .where(TaskContractRow.task_id == task_id)
            .order_by(TaskContractRow.version)
        ).all()
        return [self._to_entity(r) for r in rows]


class Executions:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: ExecutionRow) -> Execution:
        return Execution(
            id=row.id,
            task_id=row.task_id,
            role=ExecutionRole(row.role),
            contract_version=row.contract_version,
            harness=row.harness,
            model=row.model,
            effort=row.effort,
            provider=row.provider,
            image=row.image,
            policy_snapshot=row.policy_snapshot,
            state=ExecutionState(row.state),
            max_attempts=row.max_attempts,
            retry_on=[str(x) for x in row.retry_on],
            timeout_seconds=row.timeout_seconds,
            created_at=ensure_utc(row.created_at),
            ended_at=_dt(row.ended_at),
            resume_from_remote=bool(row.resume_from_remote),
        )

    def add(self, execution: Execution) -> None:
        self._s.add(
            ExecutionRow(
                id=execution.id,
                task_id=execution.task_id,
                role=execution.role.value,
                contract_version=execution.contract_version,
                harness=execution.harness,
                model=execution.model,
                effort=execution.effort,
                provider=execution.provider,
                image=execution.image,
                policy_snapshot=execution.policy_snapshot,
                state=execution.state.value,
                max_attempts=execution.max_attempts,
                retry_on=list(execution.retry_on),
                timeout_seconds=execution.timeout_seconds,
                created_at=execution.created_at,
                ended_at=execution.ended_at,
                resume_from_remote=execution.resume_from_remote,
            )
        )
        self._s.flush()

    def get(self, execution_id: str, *, for_update: bool = False) -> Execution | None:
        stmt = select(ExecutionRow).where(ExecutionRow.id == execution_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = self._s.scalar(stmt)
        return self._to_entity(row) if row else None

    def save(self, execution: Execution) -> None:
        self._s.execute(
            update(ExecutionRow)
            .where(ExecutionRow.id == execution.id)
            .values(
                state=execution.state.value,
                ended_at=execution.ended_at,
                harness=execution.harness,
                model=execution.model,
                effort=execution.effort,
                image=execution.image,
                resume_from_remote=execution.resume_from_remote,
                retry_on=list(execution.retry_on),
            )
        )

    def list_for_task(self, task_id: str) -> Sequence[Execution]:
        rows = self._s.scalars(
            select(ExecutionRow).where(ExecutionRow.task_id == task_id).order_by(ExecutionRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]

    def list_for_task_by_role(self, task_id: str, role: ExecutionRole) -> Sequence[Execution]:
        rows = self._s.scalars(
            select(ExecutionRow)
            .where(ExecutionRow.task_id == task_id, ExecutionRow.role == role.value)
            .order_by(ExecutionRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]

    def list_by_state(self, state: ExecutionState) -> Sequence[Execution]:
        rows = self._s.scalars(
            select(ExecutionRow).where(ExecutionRow.state == state.value).order_by(ExecutionRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class Attempts:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: AttemptRow) -> Attempt:
        return Attempt(
            id=row.id,
            execution_id=row.execution_id,
            task_id=row.task_id,
            number=row.number,
            state=AttemptState(row.state),
            created_at=ensure_utc(row.created_at),
            workspace_path=row.workspace_path,
            handle=row.handle,
            identity_sha256=row.identity_sha256,
            image_digest=row.image_digest,
            started_at=_dt(row.started_at),
            ended_at=_dt(row.ended_at),
            exit_code=row.exit_code,
            exit_class=ExitClass(row.exit_class) if row.exit_class else None,
            timeout_at=_dt(row.timeout_at),
            drain_deadline=_dt(row.drain_deadline),
            killed_at=_dt(row.killed_at),
            termination_reason=row.termination_reason,
            logs_drained_at=_dt(row.logs_drained_at),
            log_resume_ts=_dt(row.log_resume_ts),
            log_resume_sha256=row.log_resume_sha256,
            log_resume_occurrence=row.log_resume_occurrence or 0,
            cleaned_up_at=_dt(row.cleaned_up_at),
            unsupervised=bool(row.unsupervised),
            selected_model=row.selected_model,
            selected_harness=row.selected_harness,
            selected_image=row.selected_image,
            selected_pool=row.selected_pool,
            ordered_candidates=list(row.ordered_candidates or []),
            routing_excluded_pools=list(row.routing_excluded_pools or []),
            resume_from_remote=bool(row.resume_from_remote),
            routing_version=row.routing_version,
            effective_settings=(
                dict(row.effective_settings) if row.effective_settings is not None else None
            ),
            egress_probe=dict(row.egress_probe) if row.egress_probe is not None else None,
            stall_shape=row.stall_shape,
            termination_detail=row.termination_detail,
            blocked_reason=row.blocked_reason,
            blocked_statement=row.blocked_statement,
        )

    def add(self, attempt: Attempt) -> None:
        self._s.add(
            AttemptRow(
                id=attempt.id,
                execution_id=attempt.execution_id,
                task_id=attempt.task_id,
                number=attempt.number,
                state=attempt.state.value,
                created_at=attempt.created_at,
                workspace_path=attempt.workspace_path,
                handle=attempt.handle,
                identity_sha256=attempt.identity_sha256,
                image_digest=attempt.image_digest,
                started_at=attempt.started_at,
                ended_at=attempt.ended_at,
                exit_code=attempt.exit_code,
                exit_class=attempt.exit_class.value if attempt.exit_class else None,
                timeout_at=attempt.timeout_at,
                drain_deadline=attempt.drain_deadline,
                killed_at=attempt.killed_at,
                termination_reason=attempt.termination_reason,
                logs_drained_at=attempt.logs_drained_at,
                log_resume_ts=attempt.log_resume_ts,
                log_resume_sha256=attempt.log_resume_sha256,
                log_resume_occurrence=attempt.log_resume_occurrence,
                cleaned_up_at=attempt.cleaned_up_at,
                unsupervised=attempt.unsupervised,
                selected_model=attempt.selected_model,
                selected_harness=attempt.selected_harness,
                selected_image=attempt.selected_image,
                selected_pool=attempt.selected_pool,
                ordered_candidates=list(attempt.ordered_candidates),
                routing_excluded_pools=list(attempt.routing_excluded_pools),
                resume_from_remote=attempt.resume_from_remote,
                routing_version=attempt.routing_version,
                effective_settings=attempt.effective_settings,
                egress_probe=attempt.egress_probe,
                stall_shape=attempt.stall_shape,
                termination_detail=attempt.termination_detail,
                blocked_reason=attempt.blocked_reason,
                blocked_statement=attempt.blocked_statement,
            )
        )
        self._s.flush()

    def get(self, attempt_id: str, *, for_update: bool = False) -> Attempt | None:
        stmt = select(AttemptRow).where(AttemptRow.id == attempt_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = self._s.scalar(stmt)
        return self._to_entity(row) if row else None

    def save(self, attempt: Attempt) -> None:
        self._s.execute(
            update(AttemptRow)
            .where(AttemptRow.id == attempt.id)
            .values(
                state=attempt.state.value,
                workspace_path=attempt.workspace_path,
                handle=attempt.handle,
                identity_sha256=attempt.identity_sha256,
                image_digest=attempt.image_digest,
                started_at=attempt.started_at,
                ended_at=attempt.ended_at,
                exit_code=attempt.exit_code,
                exit_class=attempt.exit_class.value if attempt.exit_class else None,
                timeout_at=attempt.timeout_at,
                drain_deadline=attempt.drain_deadline,
                killed_at=attempt.killed_at,
                termination_reason=attempt.termination_reason,
                logs_drained_at=attempt.logs_drained_at,
                log_resume_ts=attempt.log_resume_ts,
                log_resume_sha256=attempt.log_resume_sha256,
                log_resume_occurrence=attempt.log_resume_occurrence,
                cleaned_up_at=attempt.cleaned_up_at,
                selected_model=attempt.selected_model,
                selected_harness=attempt.selected_harness,
                selected_image=attempt.selected_image,
                selected_pool=attempt.selected_pool,
                ordered_candidates=list(attempt.ordered_candidates),
                routing_excluded_pools=list(attempt.routing_excluded_pools),
                resume_from_remote=attempt.resume_from_remote,
                routing_version=attempt.routing_version,
                effective_settings=attempt.effective_settings,
                egress_probe=attempt.egress_probe,
                stall_shape=attempt.stall_shape,
                termination_detail=attempt.termination_detail,
                blocked_reason=attempt.blocked_reason,
                blocked_statement=attempt.blocked_statement,
            )
        )

    def list_for_execution(self, execution_id: str) -> Sequence[Attempt]:
        rows = self._s.scalars(
            select(AttemptRow)
            .where(AttemptRow.execution_id == execution_id)
            .order_by(AttemptRow.number)
        ).all()
        return [self._to_entity(r) for r in rows]

    def list_for_task(self, task_id: str) -> Sequence[Attempt]:
        rows = self._s.scalars(
            select(AttemptRow).where(AttemptRow.task_id == task_id).order_by(AttemptRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]

    def routes_with(self, routing_name: str, routing_version: int) -> bool:
        """hades #254: whether any attempt recorded this routing policy version."""
        count = self._s.scalar(
            select(func.count())
            .select_from(AttemptRow)
            .join(ExecutionRow, ExecutionRow.id == AttemptRow.execution_id)
            .where(
                AttemptRow.routing_version == routing_version,
                ExecutionRow.policy_snapshot[("routing", "policy", "name")].astext == routing_name,
            )
        )
        return bool(count)

    def list_in_states(
        self, states: Sequence[AttemptState], *, for_update: bool = False
    ) -> Sequence[Attempt]:
        # An unsupervised attempt (15) has no worker behind it: nothing to observe,
        # launch, count against a cap, or clean up, so no scan ever sees it.
        stmt = (
            select(AttemptRow)
            .where(
                AttemptRow.state.in_([s.value for s in states]),
                AttemptRow.unsupervised.is_(False),
            )
            .order_by(AttemptRow.id)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return [self._to_entity(r) for r in self._s.scalars(stmt).all()]

    def worker_rows(self, *, principal_id: str | None = None) -> Sequence[Mapping[str, Any]]:
        states = [
            AttemptState.PREPARING,
            AttemptState.LAUNCHING,
            AttemptState.RUNNING,
            AttemptState.TERMINATING,
        ]
        stmt = (
            select(AttemptRow, ExecutionRow, TaskRow)
            .join(ExecutionRow, ExecutionRow.id == AttemptRow.execution_id)
            .join(TaskRow, TaskRow.id == AttemptRow.task_id)
            .where(
                AttemptRow.state.in_([state.value for state in states]),
                AttemptRow.unsupervised.is_(False),
            )
            .order_by(AttemptRow.id)
        )
        if principal_id is not None:
            stmt = stmt.where(TaskRow.principal_id == principal_id)
        return [
            {
                "attempt": self._to_entity(attempt),
                "execution": Executions._to_entity(execution),
                "task": Tasks._to_entity(task),
            }
            for attempt, execution, task in self._s.execute(stmt)
        ]

    def concurrency_by_harness(self) -> Mapping[str, int]:
        states = [
            AttemptState.PREPARING,
            AttemptState.LAUNCHING,
            AttemptState.RUNNING,
            AttemptState.TERMINATING,
            AttemptState.EXITED,
        ]
        stmt = (
            select(ExecutionRow.harness, func.count())
            .join(AttemptRow, AttemptRow.execution_id == ExecutionRow.id)
            .where(
                AttemptRow.state.in_([state.value for state in states]),
                AttemptRow.unsupervised.is_(False),
            )
            .group_by(ExecutionRow.harness)
        )
        return {harness: int(count) for harness, count in self._s.execute(stmt)}

    def list_cleaned_unreleased(self, retention_kind: str, *, limit: int) -> Sequence[Attempt]:
        released = select(RetentionActionRow.subject).where(
            RetentionActionRow.kind == retention_kind
        )
        # A workspace cleanup already deleted has nothing left to release.
        deleted = select(EventRow.attempt_id).where(
            EventRow.kind == "attempt_cleaned_up",
            EventRow.attempt_id.is_not(None),
            EventRow.payload["workspace"].astext == "delete",
        )
        stmt = (
            select(AttemptRow)
            .where(
                AttemptRow.cleaned_up_at.is_not(None),
                AttemptRow.unsupervised.is_(False),
                AttemptRow.id.not_in(released),
                AttemptRow.id.not_in(deleted),
            )
            .order_by(AttemptRow.cleaned_up_at, AttemptRow.id)
            .limit(limit)
        )
        return [self._to_entity(r) for r in self._s.scalars(stmt).all()]


class PoolExhaustions:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: PoolExhaustionRow) -> PoolExhaustion:
        return PoolExhaustion(
            pool=row.pool,
            exhausted_at=ensure_utc(row.exhausted_at),
            reset_at=ensure_utc(row.reset_at),
            task_id=row.task_id,
            attempt_id=row.attempt_id,
            reason=row.reason,
            cleared_at=_dt(row.cleared_at),
            cleared_by=row.cleared_by,
            clear_reason=row.clear_reason,
        )

    def get(self, pool: str, *, for_update: bool = False) -> PoolExhaustion | None:
        stmt = select(PoolExhaustionRow).where(PoolExhaustionRow.pool == pool)
        if for_update:
            stmt = stmt.with_for_update()
        row = self._s.scalar(stmt)
        return self._to_entity(row) if row else None

    def put(self, mark: PoolExhaustion) -> PoolExhaustion:
        row = self._s.get(PoolExhaustionRow, mark.pool)
        if row is None:
            row = PoolExhaustionRow(pool=mark.pool)
            self._s.add(row)
        elif row.cleared_at is None and ensure_utc(row.reset_at) > mark.reset_at:
            mark.reset_at = ensure_utc(row.reset_at)
        row.exhausted_at = mark.exhausted_at
        row.reset_at = mark.reset_at
        row.task_id = mark.task_id
        row.attempt_id = mark.attempt_id
        row.reason = mark.reason
        row.cleared_at = mark.cleared_at
        row.cleared_by = mark.cleared_by
        row.clear_reason = mark.clear_reason
        self._s.flush()
        return self._to_entity(row)

    def list_all(self) -> Sequence[PoolExhaustion]:
        rows = self._s.scalars(select(PoolExhaustionRow).order_by(PoolExhaustionRow.pool)).all()
        return [self._to_entity(row) for row in rows]

    def clear(
        self, pool: str, *, at: datetime, principal: str, reason: str
    ) -> PoolExhaustion | None:
        row = self._s.get(PoolExhaustionRow, pool)
        if row is None:
            return None
        row.cleared_at = at
        row.cleared_by = principal
        row.clear_reason = reason
        self._s.flush()
        return self._to_entity(row)


class Events:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: EventRow) -> Event:
        return Event(
            seq=row.seq,
            ts=ensure_utc(row.ts),
            kind=row.kind,
            principal=row.principal,
            verified=row.verified,
            payload=row.payload,
            task_id=row.task_id,
            execution_id=row.execution_id,
            attempt_id=row.attempt_id,
        )

    def append(self, event: Event) -> Event:
        row = EventRow(
            ts=event.ts,
            kind=event.kind,
            task_id=event.task_id,
            execution_id=event.execution_id,
            attempt_id=event.attempt_id,
            principal=event.principal,
            verified=event.verified,
            payload=event.payload,
        )
        self._s.add(row)
        self._s.flush()
        event.seq = row.seq
        return event

    def latest_for_task_kind(self, task_id: str, kind: str) -> Event | None:
        row = self._s.scalars(
            select(EventRow)
            .where(EventRow.task_id == task_id, EventRow.kind == kind)
            .order_by(EventRow.seq.desc())
            .limit(1)
        ).first()
        return self._to_entity(row) if row else None

    def latest_for_tasks_kinds(
        self, task_ids: Sequence[str], kinds: Sequence[str]
    ) -> Mapping[tuple[str, str], Event]:
        if not task_ids or not kinds:
            return {}
        latest = (
            select(EventRow.task_id, EventRow.kind, func.max(EventRow.seq).label("seq"))
            .where(EventRow.task_id.in_(task_ids), EventRow.kind.in_(kinds))
            .group_by(EventRow.task_id, EventRow.kind)
            .subquery()
        )
        rows = self._s.scalars(select(EventRow).join(latest, EventRow.seq == latest.c.seq)).all()
        return {
            (row.task_id, row.kind): self._to_entity(row) for row in rows if row.task_id is not None
        }

    def list_for_task(self, task_id: str, *, after_seq: int, limit: int) -> Sequence[Event]:
        rows = self._s.scalars(
            select(EventRow)
            .where(EventRow.task_id == task_id, EventRow.seq > after_seq)
            .order_by(EventRow.seq)
            .limit(limit)
        ).all()
        return [self._to_entity(r) for r in rows]

    def list_global(
        self, *, after_seq: int, kind: str | None, since: datetime | None, limit: int
    ) -> Sequence[Event]:
        stmt = select(EventRow).where(EventRow.seq > after_seq).order_by(EventRow.seq).limit(limit)
        if kind is not None:
            stmt = stmt.where(EventRow.kind == kind)
        if since is not None:
            stmt = stmt.where(EventRow.ts >= since)
        return [self._to_entity(r) for r in self._s.scalars(stmt).all()]


class Leases:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: LeaseRow) -> Lease:
        return Lease(
            id=row.id,
            kind=row.kind,
            key=row.key,
            holder=row.holder,
            fenced_token=row.fenced_token,
            expires_at=ensure_utc(row.expires_at),
        )

    def _supervisor_row(self, *, for_update: bool) -> LeaseRow | None:
        stmt = select(LeaseRow).where(
            LeaseRow.kind == SUPERVISOR_LEASE_KIND, LeaseRow.key == SUPERVISOR_LEASE_KEY
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self._s.scalar(stmt)

    def get_supervisor(self) -> Lease | None:
        row = self._supervisor_row(for_update=False)
        return self._to_entity(row) if row else None

    def acquire_supervisor(self, holder: str, now: datetime, ttl_seconds: int) -> Lease | None:
        """Take the lease if free or expired, or renew it if already ours. The fenced
        token increases on every change of holder, so a stale holder's token is dead."""
        row = self._supervisor_row(for_update=True)
        expires = now + timedelta(seconds=ttl_seconds)
        if row is None:
            row = LeaseRow(
                id=new_id(),
                kind=SUPERVISOR_LEASE_KIND,
                key=SUPERVISOR_LEASE_KEY,
                holder=holder,
                fenced_token=1,
                expires_at=expires,
            )
            self._s.add(row)
            self._s.flush()
            return self._to_entity(row)
        if ensure_utc(row.expires_at) <= now:
            # Only an expired lease can be taken, whatever the holder name says: a process
            # that lost its token waits like any other standby. Every acquisition bumps the
            # token, so a replacement never shares one with its predecessor.
            row.holder = holder
            row.fenced_token = row.fenced_token + 1
            row.expires_at = expires
            self._s.flush()
            return self._to_entity(row)
        return None

    def renew_supervisor(
        self, holder: str, fenced_token: int, now: datetime, ttl_seconds: int
    ) -> Lease | None:
        row = self._supervisor_row(for_update=True)
        if row is None or row.holder != holder or row.fenced_token != fenced_token:
            return None
        row.expires_at = now + timedelta(seconds=ttl_seconds)
        self._s.flush()
        return self._to_entity(row)

    def verify_supervisor(self, holder: str, fenced_token: int) -> bool:
        row = self._supervisor_row(for_update=False)
        return row is not None and row.holder == holder and row.fenced_token == fenced_token

    def release_supervisor(self, holder: str, fenced_token: int) -> bool:
        row = self._supervisor_row(for_update=True)
        if row is None or row.holder != holder or row.fenced_token != fenced_token:
            return False
        row.expires_at = datetime(1970, 1, 1, tzinfo=UTC)
        self._s.flush()
        return True

    def upsert_attempt_lease(
        self, attempt_id: str, holder: str, fenced_token: int, now: datetime, ttl_seconds: int
    ) -> Lease:
        row = self._s.scalar(
            select(LeaseRow)
            .where(LeaseRow.kind == ATTEMPT_LEASE_KIND, LeaseRow.key == attempt_id)
            .with_for_update()
        )
        expires = now + timedelta(seconds=ttl_seconds)
        if row is None:
            row = LeaseRow(
                id=new_id(),
                kind=ATTEMPT_LEASE_KIND,
                key=attempt_id,
                holder=holder,
                fenced_token=fenced_token,
                expires_at=expires,
            )
            self._s.add(row)
        else:
            row.holder = holder
            row.fenced_token = fenced_token
            row.expires_at = expires
        self._s.flush()
        return self._to_entity(row)

    def get_attempt_lease(self, attempt_id: str) -> Lease | None:
        row = self._s.scalar(
            select(LeaseRow).where(LeaseRow.kind == ATTEMPT_LEASE_KIND, LeaseRow.key == attempt_id)
        )
        return self._to_entity(row) if row else None

    def release_attempt_lease(self, attempt_id: str) -> None:
        row = self._s.scalar(
            select(LeaseRow).where(LeaseRow.kind == ATTEMPT_LEASE_KIND, LeaseRow.key == attempt_id)
        )
        if row is not None:
            self._s.delete(row)
            self._s.flush()

    def acquire_checkout_lease(
        self, key: str, holder: str, fenced_token: int, now: datetime, ttl_seconds: int
    ) -> Lease | None:
        """The checkout lease of 10: one attempt at a time per repository and branch."""
        row = self._s.scalar(
            select(LeaseRow)
            .where(LeaseRow.kind == CHECKOUT_LEASE_KIND, LeaseRow.key == key)
            .with_for_update()
        )
        expires = now + timedelta(seconds=ttl_seconds)
        if row is None:
            row = LeaseRow(
                id=new_id(),
                kind=CHECKOUT_LEASE_KIND,
                key=key,
                holder=holder,
                fenced_token=fenced_token,
                expires_at=expires,
            )
            self._s.add(row)
            self._s.flush()
            return self._to_entity(row)
        if row.holder == holder or ensure_utc(row.expires_at) <= now:
            row.holder = holder
            row.fenced_token = fenced_token
            row.expires_at = expires
            self._s.flush()
            return self._to_entity(row)
        return None

    def get_checkout_lease(self, key: str) -> Lease | None:
        row = self._s.scalar(
            select(LeaseRow).where(LeaseRow.kind == CHECKOUT_LEASE_KIND, LeaseRow.key == key)
        )
        return self._to_entity(row) if row else None

    def release_checkout_lease(self, key: str, holder: str) -> bool:
        row = self._s.scalar(
            select(LeaseRow)
            .where(LeaseRow.kind == CHECKOUT_LEASE_KIND, LeaseRow.key == key)
            .with_for_update()
        )
        if row is None or row.holder != holder:
            return False
        self._s.delete(row)
        self._s.flush()
        return True

    def list_checkout_leases(self) -> Sequence[Lease]:
        rows = self._s.scalars(select(LeaseRow).where(LeaseRow.kind == CHECKOUT_LEASE_KIND)).all()
        return [self._to_entity(row) for row in rows]


class Logs:
    """The log stream (10). Chunks are appended, never updated; retention deletes."""

    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: LogChunkRow) -> LogChunkRecord:
        return LogChunkRecord(
            id=row.id,
            attempt_id=row.attempt_id,
            stream=row.stream,
            offset_start=row.offset_start,
            offset_end=row.offset_end,
            ts=ensure_utc(row.ts),
            line_sha256=row.line_sha256,
            occurrence=row.occurrence,
            content=gzip.decompress(row.content) if row.gzipped else row.content,
            gzipped=row.gzipped,
        )

    def append(self, chunk: LogChunkRecord) -> LogChunkRecord:
        gzipped = len(chunk.content) > GZIP_THRESHOLD_BYTES
        row = LogChunkRow(
            attempt_id=chunk.attempt_id,
            stream=chunk.stream,
            offset_start=chunk.offset_start,
            offset_end=chunk.offset_end,
            ts=chunk.ts,
            line_sha256=chunk.line_sha256,
            occurrence=chunk.occurrence,
            content=gzip.compress(chunk.content) if gzipped else chunk.content,
            gzipped=gzipped,
        )
        self._s.add(row)
        self._s.flush()
        chunk.id = row.id
        chunk.gzipped = gzipped
        return chunk

    def last_offset(self, attempt_id: str) -> int:
        value = self._s.scalar(
            select(func.max(LogChunkRow.offset_end)).where(LogChunkRow.attempt_id == attempt_id)
        )
        return int(value or 0)

    def count_for_attempt(self, attempt_id: str) -> int:
        value = self._s.scalar(
            select(func.count(LogChunkRow.id)).where(LogChunkRow.attempt_id == attempt_id)
        )
        return int(value or 0)

    def list_for_attempt(
        self, attempt_id: str, *, after_id: int = 0, limit: int = 500
    ) -> Sequence[LogChunkRecord]:
        rows = self._s.scalars(
            select(LogChunkRow)
            .where(LogChunkRow.attempt_id == attempt_id, LogChunkRow.id > after_id)
            .order_by(LogChunkRow.id)
            .limit(limit)
        ).all()
        return [self._to_entity(row) for row in rows]

    def list_from_offset(
        self, attempt_id: str, *, offset: int, stream: str | None = None, limit: int = 500
    ) -> Sequence[LogChunkRecord]:
        query = select(LogChunkRow).where(
            LogChunkRow.attempt_id == attempt_id, LogChunkRow.offset_end > offset
        )
        if stream is not None:
            query = query.where(LogChunkRow.stream == stream)
        rows = self._s.scalars(query.order_by(LogChunkRow.id).limit(limit)).all()
        return [self._to_entity(row) for row in rows]

    def delete_for_attempts(self, attempt_ids: Sequence[str]) -> int:
        if not attempt_ids:
            return 0
        rows = self._s.scalars(
            select(LogChunkRow.id).where(LogChunkRow.attempt_id.in_(list(attempt_ids)))
        ).all()
        self._s.execute(delete(LogChunkRow).where(LogChunkRow.attempt_id.in_(list(attempt_ids))))
        self._s.flush()
        return len(rows)

    def attempts_with_logs_before(self, cutoff: datetime, limit: int) -> Sequence[str]:
        rows = self._s.execute(
            select(LogChunkRow.attempt_id)
            .group_by(LogChunkRow.attempt_id)
            .having(func.max(LogChunkRow.ts) < cutoff)
            .limit(limit)
        ).all()
        return [str(row[0]) for row in rows]


class Heartbeats:
    """Append-only observed worker signals (10)."""

    # Progress lines are unverified and may keep a worker out of the quiet state,
    # but they do not prove useful work for the fail threshold (10).
    _ACTIVITY_SIGNALS = (
        "container_running",
        "log_advanced",
        "fs_changed",
        # Issue 152: the harness reports a command in flight.
        "command_running",
    )

    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: HeartbeatRow) -> Heartbeat:
        return Heartbeat(
            id=row.id,
            attempt_id=row.attempt_id,
            ts=ensure_utc(row.ts),
            signal=row.signal,
            detail=row.detail,
        )

    def append(self, heartbeat: Heartbeat) -> Heartbeat:
        row = HeartbeatRow(
            attempt_id=heartbeat.attempt_id,
            ts=heartbeat.ts,
            signal=heartbeat.signal,
            detail=heartbeat.detail,
        )
        self._s.add(row)
        self._s.flush()
        heartbeat.id = row.id
        return heartbeat

    def list_for_attempt(self, attempt_id: str, *, limit: int = 500) -> Sequence[Heartbeat]:
        rows = self._s.scalars(
            select(HeartbeatRow)
            .where(HeartbeatRow.attempt_id == attempt_id)
            .order_by(HeartbeatRow.ts, HeartbeatRow.id)
            .limit(limit)
        ).all()
        return [self._to_entity(row) for row in rows]

    def latest_signal(self, attempt_id: str) -> Heartbeat | None:
        row = self._s.scalar(
            select(HeartbeatRow)
            .where(HeartbeatRow.attempt_id == attempt_id)
            .order_by(HeartbeatRow.ts.desc(), HeartbeatRow.id.desc())
            .limit(1)
        )
        return self._to_entity(row) if row is not None else None

    def latest_activity(self, attempt_id: str) -> Heartbeat | None:
        row = self._s.scalar(
            select(HeartbeatRow)
            .where(
                HeartbeatRow.attempt_id == attempt_id,
                HeartbeatRow.signal.in_(self._ACTIVITY_SIGNALS),
            )
            .order_by(HeartbeatRow.ts.desc(), HeartbeatRow.id.desc())
            .limit(1)
        )
        return self._to_entity(row) if row is not None else None


class Retentions:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: RetentionActionRow) -> RetentionAction:
        return RetentionAction(
            id=row.id,
            kind=row.kind,
            subject=row.subject,
            policy_name=row.policy_name,
            policy_version=row.policy_version,
            acted_at=ensure_utc(row.acted_at),
            detail=dict(row.detail),
        )

    def record(self, action: RetentionAction) -> RetentionAction | None:
        existing = self._s.scalar(
            select(RetentionActionRow).where(
                RetentionActionRow.kind == action.kind,
                RetentionActionRow.subject == action.subject,
            )
        )
        if existing is not None:
            return None
        row = RetentionActionRow(
            id=action.id,
            kind=action.kind,
            subject=action.subject,
            policy_name=action.policy_name,
            policy_version=action.policy_version,
            acted_at=action.acted_at,
            detail=dict(action.detail),
        )
        self._s.add(row)
        self._s.flush()
        return action

    def list_recent(self, limit: int) -> Sequence[RetentionAction]:
        rows = self._s.scalars(
            select(RetentionActionRow).order_by(RetentionActionRow.acted_at.desc()).limit(limit)
        ).all()
        return [self._to_entity(row) for row in rows]


class Claims:
    def __init__(self, session: Session) -> None:
        self._s = session

    def put(self, record: CompletionClaimRecord) -> None:
        row = self._s.get(CompletionClaimRow, record.attempt_id)
        if row is None:
            row = CompletionClaimRow(attempt_id=record.attempt_id)
            self._s.add(row)
        row.document = record.document
        row.parsed_ok = record.parsed_ok
        row.parse_errors = list(record.parse_errors)
        self._s.flush()

    def get(self, attempt_id: str) -> CompletionClaimRecord | None:
        row = self._s.get(CompletionClaimRow, attempt_id)
        if row is None:
            return None
        return CompletionClaimRecord(
            attempt_id=row.attempt_id,
            document=row.document,
            parsed_ok=row.parsed_ok,
            parse_errors=[dict(e) for e in row.parse_errors],
        )


class SupervisorStatuses:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self) -> SupervisorStatus:
        row = self._s.get(SupervisorStatusRow, True)
        if row is None:
            return SupervisorStatus(holder=None, last_tick_at=None, tick_ms=None, counts={})
        return SupervisorStatus(
            holder=row.holder,
            last_tick_at=_dt(row.last_tick_at),
            tick_ms=row.tick_ms,
            counts={str(k): int(v) for k, v in row.counts.items()},
            last_success_at=_dt(row.last_success_at),
            last_error_at=_dt(row.last_error_at),
            last_error=row.last_error,
            refresh_request_cursor=row.refresh_request_cursor,
        )

    def write(self, status: SupervisorStatus) -> None:
        row = self._s.get(SupervisorStatusRow, True)
        if row is None:
            row = SupervisorStatusRow(singleton=True, counts={})
            self._s.add(row)
        row.holder = status.holder
        row.last_tick_at = status.last_tick_at
        row.tick_ms = status.tick_ms
        row.counts = dict(status.counts)
        row.last_success_at = status.last_success_at
        row.last_error_at = status.last_error_at
        row.last_error = status.last_error
        row.refresh_request_cursor = status.refresh_request_cursor
        self._s.flush()


class IdempotencyKeys:
    def __init__(self, session: Session) -> None:
        self._s = session

    def _row(self, principal_id: str, key: str) -> IdempotencyKeyRow | None:
        return self._s.scalar(
            select(IdempotencyKeyRow).where(
                IdempotencyKeyRow.principal_id == principal_id, IdempotencyKeyRow.key == key
            )
        )

    def get(
        self, principal_id: str, key: str
    ) -> tuple[str, int | None, dict[str, Any] | None] | None:
        row = self._row(principal_id, key)
        if row is None:
            return None
        return row.request_sha256, row.response_status, row.response_body

    def reserve(self, principal_id: str, key: str, *, request_sha256: str, now: datetime) -> None:
        self._s.add(
            IdempotencyKeyRow(
                id=new_id(),
                principal_id=principal_id,
                key=key,
                request_sha256=request_sha256,
                response_status=None,
                response_body=None,
                created_at=now,
            )
        )
        try:
            # On a concurrent first request PostgreSQL blocks here until the other
            # transaction commits, then raises on the unique index.
            self._s.flush()
        except IntegrityError as exc:
            raise IdempotencyKeyTakenError(key) from exc

    def complete(self, principal_id: str, key: str, *, status: int, body: dict[str, Any]) -> None:
        row = self._row(principal_id, key)
        assert row is not None, "complete without reserve"
        row.response_status = status
        row.response_body = body
        self._s.flush()


class SqlUnitOfWork:
    """One database transaction. Use as a context manager; commit explicitly."""

    principals: PrincipalRepository
    ui_sessions: UiSessionRepository
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

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory
        self._session: Session | None = None

    @property
    def session(self) -> Session:
        assert self._session is not None, "unit of work not entered"
        return self._session

    def __enter__(self) -> UnitOfWork:
        self._session = self._factory()
        self._session.begin()
        s = self._session
        self.principals = Principals(s)
        self.ui_sessions = UiSessions(s)
        self.repositories = Repositories(s)
        self.policies = Policies(s)
        self.tasks = Tasks(s)
        self.contracts = Contracts(s)
        self.executions = Executions(s)
        self.attempts = Attempts(s)
        self.events = Events(s)
        self.leases = Leases(s)
        self.claims = Claims(s)
        self.logs = Logs(s)
        self.heartbeats = Heartbeats(s)
        self.retention = Retentions(s)
        self.supervisor_status = SupervisorStatuses(s)
        self.idempotency = IdempotencyKeys(s)
        self.routing_policies = RoutingPolicies(s)
        self.pool_exhaustions = PoolExhaustions(s)
        self.artifacts = Artifacts(s)
        self.evidence = Evidences(s)
        self.review_reports = ReviewReports(s)
        self.gate_results = GateResults(s)
        self.acceptance = Acceptances(s)
        self.decisions = Decisions(s)
        self.task_notes = TaskNotes(s)
        self.memory = MemoryItems(s)
        self.decision_ledger = DecisionLedger(s)
        self.rooms = Rooms(s)
        self.room_turns = RoomTurns(s)
        self.escalations = Escalations(s)
        self.dispositions = Dispositions(s)
        self.wakes = Wakes(s)
        self.attempt_metrics = AttemptMetricsRepo(s)
        self.pull_requests = PullRequests(s)
        self.pull_request_heads = PullRequestHeads(s)
        self.review_cycles = ExternalReviewCycles(s)
        self.external_reviews = ExternalReviews(s)
        self.review_comments = ReviewComments(s)
        self.reactions = Reactions(s)
        self.ci_certifications = CICertifications(s)
        self.ci_decisions = CIDecisions(s)
        self.github_deliveries = GitHubDeliveries(s)
        self.harnesses = HarnessStates(s)
        self.harness_images = HarnessImages(s)
        self.bootstrap_imports = BootstrapImports(s)
        self.provider_settings = ProviderSettings(s)
        self.github_manifest_states = GitHubManifestStates(s)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._session is not None
        try:
            if self._session.in_transaction():
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None
        if exc is not None:
            translated = translate_error(exc)
            if translated is not None:
                raise translated from exc

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def set_fenced_token(self, fenced_token: int) -> None:
        """Transaction-local: SET LOCAL semantics via set_config(..., is_local=true)."""
        self.session.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": FENCED_TOKEN_SETTING, "value": str(fenced_token)},
        )


class SqlUnitOfWorkFactory:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def __call__(self) -> UnitOfWork:
        return SqlUnitOfWork(self._sessions)
