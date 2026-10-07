"""SQLAlchemy 2 typed ORM models. The migrations are hand-written; these mirror them."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ID = String(26)
TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}


class PrincipalRow(Base):
    __tablename__ = "principals"
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    role: Mapped[str] = mapped_column(String(32))
    token_salt: Mapped[bytes] = mapped_column(LargeBinary)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(TZ)
    disabled_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class UiSessionRow(Base):
    __tablename__ = "ui_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    csrf: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ)
    expires_at: Mapped[datetime] = mapped_column(TZ, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(TZ)


class RepositoryRow(Base):
    __tablename__ = "repositories"
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    url: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(String(255))
    installation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    policy_name: Mapped[str] = mapped_column(String(128))
    registered_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZ)
    external_review_attested: Mapped[bool] = mapped_column(Boolean, default=False)
    attested_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attested_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    private: Mapped[bool] = mapped_column(Boolean, default=False)


class PolicyRow(Base):
    __tablename__ = "policies"
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ)
    retired_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("principal_id", "external_id", name="uq_tasks_principal_external"),
        Index("ix_tasks_state", "state"),
        Index("ix_tasks_principal_updated", "principal_id", "updated_at"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128))
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    repository_id: Mapped[str] = mapped_column(ID, ForeignKey("repositories.id"))
    project: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(48))
    contract_version: Mapped[int] = mapped_column(Integer)
    policy_name: Mapped[str] = mapped_column(String(128))
    policy_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZ)
    updated_at: Mapped[datetime] = mapped_column(TZ)
    closed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resume_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    quota_wait_started_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class TaskContractRow(Base):
    __tablename__ = "task_contracts"
    __table_args__ = (UniqueConstraint("task_id", "version", name="uq_task_contracts_version"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    submitted_at: Mapped[datetime] = mapped_column(TZ)


class ExecutionRow(Base):
    __tablename__ = "executions"
    __table_args__ = (Index("ix_executions_task", "task_id"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    role: Mapped[str] = mapped_column(String(16))
    contract_version: Mapped[int] = mapped_column(Integer)
    harness: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    image: Mapped[str] = mapped_column(Text)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16))
    max_attempts: Mapped[int] = mapped_column(Integer)
    retry_on: Mapped[list[Any]] = mapped_column(JSONB)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZ)
    ended_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    resume_from_remote: Mapped[bool] = mapped_column(Boolean, default=False)


class AttemptRow(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("execution_id", "number", name="uq_attempts_execution_number"),
        Index("ix_attempts_state", "state"),
        Index("ix_attempts_task", "task_id"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    execution_id: Mapped[str] = mapped_column(ID, ForeignKey("executions.id"))
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    handle: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ)
    started_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timeout_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    drain_deadline: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    killed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    logs_drained_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    log_resume_ts: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    log_resume_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    log_resume_occurrence: Mapped[int] = mapped_column(Integer, default=0)
    cleaned_up_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    unsupervised: Mapped[bool] = mapped_column(Boolean, default=False)
    selected_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selected_harness: Mapped[str | None] = mapped_column(String(32), nullable=True)
    selected_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_pool: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ordered_candidates: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    routing_excluded_pools: Mapped[list[str]] = mapped_column(JSONB, default=list)
    resume_from_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    routing_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_settings: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    egress_probe: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    stall_shape: Mapped[str | None] = mapped_column(String(32), nullable=True)
    termination_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    blocked_statement: Mapped[str | None] = mapped_column(Text, nullable=True)


class LogChunkRow(Base):
    __tablename__ = "log_chunks"
    __table_args__ = (
        Index("ix_log_chunks_attempt", "attempt_id", "id"),
        UniqueConstraint("attempt_id", "offset_start", name="uq_log_chunks_attempt_offset"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[str] = mapped_column(ID, ForeignKey("attempts.id"))
    stream: Mapped[str] = mapped_column(String(8))
    offset_start: Mapped[int] = mapped_column(BigInteger)
    offset_end: Mapped[int] = mapped_column(BigInteger)
    ts: Mapped[datetime] = mapped_column(TZ)
    line_sha256: Mapped[str] = mapped_column(String(64))
    occurrence: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    gzipped: Mapped[bool] = mapped_column(Boolean)


class HeartbeatRow(Base):
    __tablename__ = "heartbeats"
    __table_args__ = (Index("ix_heartbeats_attempt_ts", "attempt_id", "ts"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[str] = mapped_column(ID, ForeignKey("attempts.id"))
    ts: Mapped[datetime] = mapped_column(TZ)
    signal: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB)


class RetentionActionRow(Base):
    __tablename__ = "retention_actions"
    __table_args__ = (
        Index("ix_retention_actions_kind", "kind", "acted_at"),
        UniqueConstraint("kind", "subject", name="uq_retention_actions_kind_subject"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48))
    subject: Mapped[str] = mapped_column(String(255))
    policy_name: Mapped[str] = mapped_column(String(128))
    policy_version: Mapped[int] = mapped_column(Integer)
    acted_at: Mapped[datetime] = mapped_column(TZ)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_task_seq", "task_id", "seq"),)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZ)
    kind: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(ID, ForeignKey("tasks.id"), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(ID, ForeignKey("executions.id"), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(ID, ForeignKey("attempts.id"), nullable=True)
    principal: Mapped[str] = mapped_column(String(160))
    verified: Mapped[bool] = mapped_column(Boolean)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class LeaseRow(Base):
    __tablename__ = "leases"
    __table_args__ = (
        UniqueConstraint("kind", "key", name="uq_leases_kind_key"),
        Index("ix_leases_expires", "expires_at"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(255))
    holder: Mapped[str] = mapped_column(String(128))
    fenced_token: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(TZ)


class CompletionClaimRow(Base):
    __tablename__ = "completion_claims"
    attempt_id: Mapped[str] = mapped_column(ID, ForeignKey("attempts.id"), primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parsed_ok: Mapped[bool] = mapped_column(Boolean)
    parse_errors: Mapped[list[Any]] = mapped_column(JSONB)


class SupervisorStatusRow(Base):
    __tablename__ = "supervisor_status"
    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_tick_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    tick_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB)
    last_success_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 339: the seq of the last CREDENTIAL_REFRESH_REQUESTED event the renewer has
    # handled, persisted so a supervisor restart does not replay every historical
    # request as pending.
    refresh_request_cursor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class IdempotencyKeyRow(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("principal_id", "key", name="uq_idempotency_principal_key"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    key: Mapped[str] = mapped_column(String(255))
    request_sha256: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ)


class RoutingPolicyRow(Base):
    __tablename__ = "routing_policies"
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ)
    retired_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class PoolExhaustionRow(Base):
    __tablename__ = "pool_exhaustions"
    pool: Mapped[str] = mapped_column(String(128), primary_key=True)
    exhausted_at: Mapped[datetime] = mapped_column(TZ)
    reset_at: Mapped[datetime] = mapped_column(TZ, index=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    attempt_id: Mapped[str] = mapped_column(ID, ForeignKey("attempts.id"))
    reason: Mapped[str] = mapped_column(Text)
    cleared_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    cleared_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    clear_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_attempt", "attempt_id"),
        Index("ix_artifacts_task", "task_id"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    attempt_id: Mapped[str | None] = mapped_column(ID, ForeignKey("attempts.id"), nullable=True)
    task_id: Mapped[str | None] = mapped_column(ID, ForeignKey("tasks.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(TZ)


class EvidenceRow(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_attempt_kind", "attempt_id", "kind"),
        Index("ix_evidence_task", "task_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[str | None] = mapped_column(ID, ForeignKey("attempts.id"), nullable=True)
    task_id: Mapped[str | None] = mapped_column(ID, ForeignKey("tasks.id"), nullable=True)
    pull_request_id: Mapped[str | None] = mapped_column(ID, nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(TZ)
    source: Mapped[str] = mapped_column(String(16))
    verified: Mapped[bool] = mapped_column(Boolean)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    artifact_id: Mapped[str | None] = mapped_column(ID, ForeignKey("artifacts.id"), nullable=True)


class ReviewReportRow(Base):
    __tablename__ = "review_reports"
    __table_args__ = (Index("ix_review_reports_task_head", "task_id", "head_sha"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    head_sha: Mapped[str] = mapped_column(String(64))
    reviewer_kind: Mapped[str] = mapped_column(String(32))
    reviewer_attempt_id: Mapped[str | None] = mapped_column(
        ID, ForeignKey("attempts.id"), nullable=True
    )
    reviewer_principal_id: Mapped[str | None] = mapped_column(
        ID, ForeignKey("principals.id"), nullable=True
    )
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    artifact_id: Mapped[str | None] = mapped_column(ID, ForeignKey("artifacts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ)
    superseded_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class GateResultRow(Base):
    __tablename__ = "gate_results"
    __table_args__ = (
        UniqueConstraint("attempt_id", "gate", "head_sha", name="uq_gate_results_attempt_gate"),
        Index("ix_gate_results_task", "task_id"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    attempt_id: Mapped[str] = mapped_column(ID, ForeignKey("attempts.id"))
    head_sha: Mapped[str] = mapped_column(String(64))
    gate: Mapped[str] = mapped_column(String(48))
    phase: Mapped[str] = mapped_column(String(16))
    result: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list[Any]] = mapped_column(ARRAY(BigInteger))
    evaluated_at: Mapped[datetime] = mapped_column(TZ)
    # ADR 0024 (0028): the gate's class when it was evaluated, and advisory findings.
    blocking: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    findings: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class AcceptanceResultRow(Base):
    __tablename__ = "acceptance_results"
    __table_args__ = (Index("ix_acceptance_task", "task_id"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    head_sha: Mapped[str] = mapped_column(String(64))
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    verdict: Mapped[str] = mapped_column(String(24))
    reasoning: Mapped[str] = mapped_column(Text)
    superseded_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ)


class EscalationRow(Base):
    __tablename__ = "escalations"
    __table_args__ = (Index("ix_escalations_task_state", "task_id", "state"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    attempt_id: Mapped[str | None] = mapped_column(ID, ForeignKey("attempts.id"), nullable=True)
    state: Mapped[str] = mapped_column(String(16))
    question: Mapped[str] = mapped_column(Text)
    opened_at: Mapped[datetime] = mapped_column(TZ)
    closed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    decision_id: Mapped[str | None] = mapped_column(ID, nullable=True)
    last_wake_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DecisionRow(Base):
    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_task", "task_id"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str | None] = mapped_column(ID, ForeignKey("tasks.id"), nullable=True)
    escalation_id: Mapped[str | None] = mapped_column(
        ID, ForeignKey("escalations.id"), nullable=True
    )
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    kind: Mapped[str] = mapped_column(String(48))
    verbatim: Mapped[str] = mapped_column(Text)
    resolves: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ)


class TaskNoteRow(Base):
    """An operator's note on a task (hades #489, 0051_task_notes)."""

    __tablename__ = "task_notes"
    __table_args__ = (Index("ix_task_notes_task_created", "task_id", "created_at"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    author: Mapped[str] = mapped_column(String(128))
    # Declared before `text`, whose name shadows SQLAlchemy's `text()` in this body.
    verbatim: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ)


class ReviewDispositionRow(Base):
    __tablename__ = "review_dispositions"
    __table_args__ = (
        UniqueConstraint(
            "review_comment_id",
            "comment_body_sha256",
            name="uq_review_dispositions_comment_body",
        ),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    review_comment_id: Mapped[str] = mapped_column(String(64))
    comment_body_sha256: Mapped[str] = mapped_column(String(64))
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    disposition: Mapped[str] = mapped_column(String(24))
    reasoning: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ)


class WakeRow(Base):
    __tablename__ = "wakes"
    __table_args__ = (
        Index(
            "ix_wakes_principal_unacked",
            "principal_id",
            "acked_at",
            postgresql_where=text("acked_at IS NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    task_id: Mapped[str | None] = mapped_column(ID, ForeignKey("tasks.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivered_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    ack_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    gave_up_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class AttemptMetricsRow(Base):
    __tablename__ = "attempt_metrics"
    __table_args__ = (Index("ix_attempt_metrics_model", "model", "created_at"),)
    attempt_id: Mapped[str] = mapped_column(ID, ForeignKey("attempts.id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    model: Mapped[str] = mapped_column(String(128))
    harness: Mapped[str] = mapped_column(String(32))
    endpoint_kind: Mapped[str] = mapped_column(String(16))
    pool: Mapped[str] = mapped_column(String(64))
    wall_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    harness_duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tool_calls: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_units: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_source: Mapped[str] = mapped_column(String(24))
    model_reported: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exit_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gates_passed: Mapped[int] = mapped_column(Integer, default=0)
    gates_failed: Mapped[int] = mapped_column(Integer, default=0)
    corrections_after: Mapped[int] = mapped_column(Integer, default=0)
    acceptance_verdict: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ)


# ----- C4: GitHub delivery (14, 23) --------------------------------------


class PullRequestRow(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repository_id", "number", name="uq_pull_requests_repo_number"),
        Index("ix_pull_requests_state", "state"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"), unique=True)
    repository_id: Mapped[str] = mapped_column(ID, ForeignKey("repositories.id"))
    number: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(Text)
    base_ref: Mapped[str] = mapped_column(String(255))
    work_branch: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(16))
    head_sha: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    body_sha256: Mapped[str] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(TZ)
    merged_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    merge_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    merged_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    closed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    last_reactions_polled_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    reactions_observable: Mapped[bool] = mapped_column(Boolean, default=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    observed_head_sha: Mapped[str] = mapped_column(String(64), default="")
    observed_base_ref: Mapped[str] = mapped_column(String(255), default="")
    mergeable_state: Mapped[str] = mapped_column(String(32), default="")
    mergeable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    merge_refusal_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    merge_refusal_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    merge_refusal_base_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    merge_refusal_mergeable_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    merge_refusal_count: Mapped[int] = mapped_column(Integer, default=0)
    merge_retry_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class PullRequestHeadRow(Base):
    __tablename__ = "pull_request_heads"
    __table_args__ = (
        UniqueConstraint("pull_request_id", "sha", name="uq_pull_request_heads_sha"),
        Index("ix_pull_request_heads_pr", "pull_request_id"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    pull_request_id: Mapped[str] = mapped_column(ID, ForeignKey("pull_requests.id"))
    sha: Mapped[str] = mapped_column(String(64))
    pushed_by: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime] = mapped_column(TZ)


class ExternalReviewCycleRow(Base):
    __tablename__ = "external_review_cycles"
    __table_args__ = (Index("ix_external_review_cycles_pr", "pull_request_id", "head_sha"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    pull_request_id: Mapped[str] = mapped_column(ID, ForeignKey("pull_requests.id"))
    head_sha: Mapped[str] = mapped_column(String(64))
    components: Mapped[list[Any]] = mapped_column(JSONB)
    completed_components: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16))
    trigger: Mapped[str] = mapped_column(String(32))
    opened_at: Mapped[datetime] = mapped_column(TZ)
    completed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class ExternalReviewRow(Base):
    __tablename__ = "external_reviews"
    __table_args__ = (
        UniqueConstraint(
            "pull_request_id", "signal", "github_id", name="uq_external_reviews_github_id"
        ),
        Index("ix_external_reviews_pr", "pull_request_id"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    pull_request_id: Mapped[str] = mapped_column(ID, ForeignKey("pull_requests.id"))
    cycle_id: Mapped[str | None] = mapped_column(
        ID, ForeignKey("external_review_cycles.id"), nullable=True
    )
    reviewer_login: Mapped[str] = mapped_column(String(128))
    signal: Mapped[str] = mapped_column(String(16))
    github_id: Mapped[str] = mapped_column(String(64))
    reviewed_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sha_inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    body_sha256: Mapped[str] = mapped_column(String(64))
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(TZ)


class ReviewCommentRow(Base):
    __tablename__ = "review_comments"
    __table_args__ = (
        UniqueConstraint(
            "pull_request_id", "kind", "github_id", name="uq_review_comments_github_id"
        ),
        Index("ix_review_comments_pr", "pull_request_id"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    pull_request_id: Mapped[str] = mapped_column(ID, ForeignKey("pull_requests.id"))
    external_review_id: Mapped[str | None] = mapped_column(
        ID, ForeignKey("external_reviews.id"), nullable=True
    )
    github_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    login: Mapped[str] = mapped_column(String(128))
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body: Mapped[str] = mapped_column(Text)
    body_sha256: Mapped[str] = mapped_column(String(64))
    reviewed_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ)
    updated_at: Mapped[datetime] = mapped_column(TZ)


class ReactionRow(Base):
    __tablename__ = "reactions"
    __table_args__ = (
        UniqueConstraint(
            "pull_request_id",
            "subject_kind",
            "subject_github_id",
            "github_id",
            name="uq_reactions_github_id",
        ),
        Index("ix_reactions_pr", "pull_request_id"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    pull_request_id: Mapped[str] = mapped_column(ID, ForeignKey("pull_requests.id"))
    subject_kind: Mapped[str] = mapped_column(String(24))
    subject_github_id: Mapped[str] = mapped_column(String(64))
    github_id: Mapped[str] = mapped_column(String(64))
    login: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(TZ)
    removed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class CICertificationRow(Base):
    __tablename__ = "ci_certifications"
    __table_args__ = (
        UniqueConstraint("pull_request_id", "head_sha", name="uq_ci_certifications_head"),
        Index("ix_ci_certifications_task", "task_id"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    pull_request_id: Mapped[str] = mapped_column(ID, ForeignKey("pull_requests.id"))
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    head_sha: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    required_checks: Mapped[list[Any]] = mapped_column(JSONB)
    check_runs: Mapped[list[Any]] = mapped_column(JSONB)
    failure: Mapped[dict[str, Any]] = mapped_column(JSONB)
    detail: Mapped[str] = mapped_column(Text)
    evaluated_at: Mapped[datetime] = mapped_column(TZ)


class CIDecisionRow(Base):
    __tablename__ = "ci_decisions"
    __table_args__ = (Index("ix_ci_decisions_task", "task_id"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    ci_certification_id: Mapped[str | None] = mapped_column(
        ID, ForeignKey("ci_certifications.id"), nullable=True
    )
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    cause: Mapped[str] = mapped_column(String(48))
    action: Mapped[str] = mapped_column(String(16))
    reasoning: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ)


class GitHubDeliveryRow(Base):
    __tablename__ = "github_deliveries"
    __table_args__ = (
        Index(
            "ix_github_deliveries_unprocessed",
            "received_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )
    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event: Mapped[str] = mapped_column(String(48))
    action: Mapped[str] = mapped_column(String(48))
    repository: Mapped[str] = mapped_column(String(255))
    received_at: Mapped[datetime] = mapped_column(TZ)
    body_sha256: Mapped[str] = mapped_column(String(64))
    normalized: Mapped[dict[str, Any]] = mapped_column(JSONB)
    processed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


# ----- C5: harness administration (07, 25) ---------------------------------


class HarnessStateRow(Base):
    __tablename__ = "harnesses"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    # hades #174: an administrator has decided, so the configuration default no longer
    # applies (0027).
    enabled_decided: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
    reason: Mapped[str] = mapped_column(Text)
    session_compatibility: Mapped[str] = mapped_column(String(16))
    mount_mode_observed: Mapped[str | None] = mapped_column(String(16), nullable=True)
    refresh_requires_rw: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_launch_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    last_launch_outcome: Mapped[str | None] = mapped_column(String(48), nullable=True)
    last_auth_failure_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    # hades #389: the most recent successful launch time (last_launch_at moves on failures).
    last_successful_launch_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    last_test: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TZ)
    updated_by: Mapped[str] = mapped_column(String(160))


class BootstrapImportRow(Base):
    __tablename__ = "bootstrap_imports"
    __table_args__ = (
        Index("ix_bootstrap_imports_content", "content_sha256"),
        # ADR 0006: at most one import holds authority, enforced by the database.
        Index(
            "uq_bootstrap_imports_authoritative",
            "state",
            unique=True,
            postgresql_where=text("state = 'authoritative'"),
        ),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    schema_version: Mapped[str] = mapped_column(String(16))
    content_sha256: Mapped[str] = mapped_column(String(64))
    source_sha256: Mapped[str] = mapped_column(String(64))
    source: Mapped[dict[str, Any]] = mapped_column(JSONB)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    imported_by: Mapped[str] = mapped_column(String(160))
    verified_at: Mapped[datetime] = mapped_column(TZ)
    committed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    committed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)


class HarnessImageRow(Base):
    """Each harness's default worker image and the one it replaced (ADR 0018)."""

    __tablename__ = "harness_images"
    harness: Mapped[str] = mapped_column(String(64), primary_key=True)
    digest: Mapped[str] = mapped_column(String(160))
    reference: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(64))
    previous_digest: Mapped[str | None] = mapped_column(String(160), nullable=True)
    previous_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(TZ)
    updated_by: Mapped[str] = mapped_column(String(160))


# ----- runtime provider settings (25, crucible#91) --------------------------


class ProviderSettingRow(Base):
    __tablename__ = "provider_settings"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(TZ)
    updated_by: Mapped[str] = mapped_column(String(160))


# ----- the GitHub App manifest flow (crucible#168) ------------------------------------


class GitHubManifestStateRow(Base):
    __tablename__ = "github_manifest_states"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    principal: Mapped[str] = mapped_column(String(160))
    app_name: Mapped[str] = mapped_column(String(64))
    organization: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ)
    expires_at: Mapped[datetime] = mapped_column(TZ)
    consumed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
