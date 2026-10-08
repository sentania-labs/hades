"""SQLAlchemy repositories for the C2 record tables: policies, routing policies,
artifacts, evidence, review reports, gate results, acceptance, decisions, escalations,
dispositions, wakes, and attempt metrics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from crucible.adapters.persistence.models import (
    AcceptanceResultRow,
    ArtifactRow,
    AttemptMetricsRow,
    BootstrapImportRow,
    DecisionRow,
    EscalationRow,
    EvidenceRow,
    GateResultRow,
    GitHubManifestStateRow,
    HarnessImageRow,
    HarnessStateRow,
    PolicyRow,
    PrincipalRow,
    ProviderSettingRow,
    ReviewDispositionRow,
    ReviewReportRow,
    RoutingPolicyRow,
    TaskRow,
    WakeRow,
)
from crucible.domain.entities import (
    AcceptanceResult,
    AcceptanceVerdict,
    Artifact,
    AttemptMetrics,
    BootstrapImport,
    Decision,
    DispositionKind,
    Escalation,
    EscalationState,
    EvidenceRecord,
    GateResultRecord,
    GitHubManifestState,
    HarnessImage,
    HarnessState,
    Policy,
    ProviderSetting,
    ReviewDisposition,
    ReviewReportRecord,
    RoutingPolicyRecord,
    Wake,
)
from crucible.domain.time import ensure_utc


def _dt(value: datetime | None) -> datetime | None:
    return ensure_utc(value) if value is not None else None


class Policies:
    """Versions are immutable once a task references them (05b)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: PolicyRow) -> Policy:
        return Policy(
            name=row.name,
            version=row.version,
            document=row.document,
            created_at=ensure_utc(row.created_at),
            retired_at=_dt(row.retired_at),
        )

    def get(self, name: str, version: int) -> Policy | None:
        row = self._s.get(PolicyRow, (name, version))
        return self._to_entity(row) if row else None

    def list_versions(self, name: str) -> Sequence[Policy]:
        rows = self._s.scalars(
            select(PolicyRow).where(PolicyRow.name == name).order_by(PolicyRow.version)
        ).all()
        return [self._to_entity(r) for r in rows]

    def list_names(self) -> Sequence[str]:
        """Distinct policy names sorted."""
        rows = self._s.scalars(select(PolicyRow.name).distinct().order_by(PolicyRow.name)).all()
        return list(rows)

    def is_referenced(self, name: str, version: int) -> bool:
        count = self._s.scalar(
            select(func.count())
            .select_from(TaskRow)
            .where(TaskRow.policy_name == name, TaskRow.policy_version == version)
        )
        return bool(count)

    def put(self, policy: Policy) -> Policy:
        row = self._s.get(PolicyRow, (policy.name, policy.version))
        if row is None:
            row = PolicyRow(name=policy.name, version=policy.version, created_at=policy.created_at)
            self._s.add(row)
        row.document = policy.document
        row.retired_at = policy.retired_at
        self._s.flush()
        return self._to_entity(row)


class RoutingPolicies:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: RoutingPolicyRow) -> RoutingPolicyRecord:
        return RoutingPolicyRecord(
            name=row.name,
            version=row.version,
            document=row.document,
            created_at=ensure_utc(row.created_at),
            retired_at=_dt(row.retired_at),
        )

    def get(self, name: str, version: int) -> RoutingPolicyRecord | None:
        row = self._s.get(RoutingPolicyRow, (name, version))
        return self._to_entity(row) if row else None

    def list_versions(self, name: str) -> Sequence[RoutingPolicyRecord]:
        rows = self._s.scalars(
            select(RoutingPolicyRow)
            .where(RoutingPolicyRow.name == name)
            .order_by(RoutingPolicyRow.version)
        ).all()
        return [self._to_entity(row) for row in rows]

    def is_referenced(self, name: str, version: int) -> bool:
        """A routing policy is referenced when a policy version points at it."""
        rows = self._s.scalars(select(PolicyRow)).all()
        for row in rows:
            ref = (row.document or {}).get("routing", {}).get("policy", {})
            if ref.get("name") == name and int(ref.get("version", 0)) == version:
                return True
        return False

    def put(self, policy: RoutingPolicyRecord) -> RoutingPolicyRecord:
        row = self._s.get(RoutingPolicyRow, (policy.name, policy.version))
        if row is None:
            row = RoutingPolicyRow(
                name=policy.name, version=policy.version, created_at=policy.created_at
            )
            self._s.add(row)
        row.document = policy.document
        row.retired_at = policy.retired_at
        self._s.flush()
        return self._to_entity(row)


class Artifacts:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: ArtifactRow) -> Artifact:
        return Artifact(
            id=row.id,
            attempt_id=row.attempt_id,
            task_id=row.task_id,
            type=row.type,
            filename=row.filename,
            path=row.path,
            size=row.size,
            sha256=row.sha256,
            content_type=row.content_type,
            created_at=ensure_utc(row.created_at),
            created_by=row.created_by,
        )

    def add(self, artifact: Artifact) -> None:
        self._s.add(
            ArtifactRow(
                id=artifact.id,
                attempt_id=artifact.attempt_id,
                task_id=artifact.task_id,
                type=artifact.type,
                filename=artifact.filename,
                path=artifact.path,
                size=artifact.size,
                sha256=artifact.sha256,
                content_type=artifact.content_type,
                created_by=artifact.created_by,
                created_at=artifact.created_at,
            )
        )
        self._s.flush()

    def get(self, artifact_id: str) -> Artifact | None:
        row = self._s.get(ArtifactRow, artifact_id)
        return self._to_entity(row) if row else None

    def list_for_attempt(self, attempt_id: str) -> Sequence[Artifact]:
        rows = self._s.scalars(
            select(ArtifactRow).where(ArtifactRow.attempt_id == attempt_id).order_by(ArtifactRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]

    def find_by_sha256(self, sha256: str, attempt_id: str | None) -> Artifact | None:
        stmt = select(ArtifactRow).where(ArtifactRow.sha256 == sha256)
        if attempt_id is not None:
            stmt = stmt.where(ArtifactRow.attempt_id == attempt_id)
        row = self._s.scalars(stmt.order_by(ArtifactRow.id)).first()
        return self._to_entity(row) if row else None


class Evidences:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: EvidenceRow) -> EvidenceRecord:
        return EvidenceRecord(
            id=row.id,
            attempt_id=row.attempt_id,
            task_id=row.task_id,
            kind=row.kind,
            observed_at=ensure_utc(row.observed_at),
            source=row.source,
            verified=row.verified,
            payload=row.payload,
            artifact_id=row.artifact_id,
            pull_request_id=row.pull_request_id,
        )

    def add(self, evidence: EvidenceRecord) -> EvidenceRecord:
        row = EvidenceRow(
            attempt_id=evidence.attempt_id,
            task_id=evidence.task_id,
            pull_request_id=evidence.pull_request_id,
            kind=evidence.kind,
            observed_at=evidence.observed_at,
            source=evidence.source,
            verified=evidence.verified,
            payload=evidence.payload,
            artifact_id=evidence.artifact_id,
        )
        self._s.add(row)
        self._s.flush()
        evidence.id = row.id
        return evidence

    def list_for_attempt(self, attempt_id: str) -> Sequence[EvidenceRecord]:
        rows = self._s.scalars(
            select(EvidenceRow).where(EvidenceRow.attempt_id == attempt_id).order_by(EvidenceRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]

    def list_for_task(self, task_id: str) -> Sequence[EvidenceRecord]:
        rows = self._s.scalars(
            select(EvidenceRow).where(EvidenceRow.task_id == task_id).order_by(EvidenceRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class ReviewReports:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: ReviewReportRow) -> ReviewReportRecord:
        return ReviewReportRecord(
            id=row.id,
            task_id=row.task_id,
            head_sha=row.head_sha,
            reviewer_kind=row.reviewer_kind,
            reviewer_attempt_id=row.reviewer_attempt_id,
            reviewer_principal_id=row.reviewer_principal_id,
            document=row.document,
            created_at=ensure_utc(row.created_at),
            artifact_id=row.artifact_id,
            superseded_at=_dt(row.superseded_at),
        )

    def add(self, report: ReviewReportRecord) -> None:
        self._s.add(
            ReviewReportRow(
                id=report.id,
                task_id=report.task_id,
                head_sha=report.head_sha,
                reviewer_kind=report.reviewer_kind,
                reviewer_attempt_id=report.reviewer_attempt_id,
                reviewer_principal_id=report.reviewer_principal_id,
                document=report.document,
                artifact_id=report.artifact_id,
                created_at=report.created_at,
                superseded_at=report.superseded_at,
            )
        )
        self._s.flush()

    def get(self, report_id: str) -> ReviewReportRecord | None:
        row = self._s.get(ReviewReportRow, report_id)
        return self._to_entity(row) if row else None

    def list_for_task(self, task_id: str) -> Sequence[ReviewReportRecord]:
        rows = self._s.scalars(
            select(ReviewReportRow)
            .where(ReviewReportRow.task_id == task_id)
            .order_by(ReviewReportRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]

    def supersede(self, report_id: str, at: datetime) -> None:
        row = self._s.get(ReviewReportRow, report_id)
        if row is not None and row.superseded_at is None:
            row.superseded_at = at
            self._s.flush()


class GateResults:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: GateResultRow) -> GateResultRecord:
        return GateResultRecord(
            id=row.id,
            task_id=row.task_id,
            attempt_id=row.attempt_id,
            head_sha=row.head_sha,
            gate=row.gate,
            phase=row.phase,
            result=row.result,
            detail=row.detail,
            evidence_ids=[int(x) for x in row.evidence_ids],
            evaluated_at=ensure_utc(row.evaluated_at),
            blocking=bool(row.blocking),
            findings=[str(x) for x in row.findings or []],
        )

    def put(self, result: GateResultRecord) -> None:
        row = self._s.scalar(
            select(GateResultRow).where(
                GateResultRow.attempt_id == result.attempt_id,
                GateResultRow.gate == result.gate,
                GateResultRow.head_sha == (result.head_sha or ""),
            )
        )
        if row is None:
            row = GateResultRow(
                id=result.id,
                task_id=result.task_id,
                attempt_id=result.attempt_id,
                head_sha=result.head_sha or "",
                gate=result.gate,
            )
            self._s.add(row)
        row.phase = result.phase
        row.result = result.result
        row.detail = result.detail
        row.evidence_ids = list(result.evidence_ids)
        row.evaluated_at = result.evaluated_at
        row.blocking = result.blocking
        row.findings = list(result.findings)
        self._s.flush()

    def list_for_attempt(self, attempt_id: str) -> Sequence[GateResultRecord]:
        rows = self._s.scalars(
            select(GateResultRow)
            .where(GateResultRow.attempt_id == attempt_id)
            .order_by(GateResultRow.gate)
        ).all()
        return [self._to_entity(r) for r in rows]

    def list_for_task(self, task_id: str) -> Sequence[GateResultRecord]:
        rows = self._s.scalars(
            select(GateResultRow)
            .where(GateResultRow.task_id == task_id)
            .order_by(GateResultRow.attempt_id, GateResultRow.gate)
        ).all()
        return [self._to_entity(r) for r in rows]

    def list_for_tasks(self, task_ids: Sequence[str]) -> Sequence[GateResultRecord]:
        if not task_ids:
            return []
        rows = self._s.scalars(
            select(GateResultRow)
            .where(GateResultRow.task_id.in_(task_ids))
            .order_by(GateResultRow.task_id, GateResultRow.gate)
        ).all()
        return [self._to_entity(row) for row in rows]


class Acceptances:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: AcceptanceResultRow) -> AcceptanceResult:
        return AcceptanceResult(
            id=row.id,
            task_id=row.task_id,
            head_sha=row.head_sha,
            principal_id=row.principal_id,
            verdict=AcceptanceVerdict(row.verdict),
            reasoning=row.reasoning,
            created_at=ensure_utc(row.created_at),
            superseded_at=_dt(row.superseded_at),
        )

    def add(self, result: AcceptanceResult) -> None:
        self._s.add(
            AcceptanceResultRow(
                id=result.id,
                task_id=result.task_id,
                head_sha=result.head_sha,
                principal_id=result.principal_id,
                verdict=result.verdict.value,
                reasoning=result.reasoning,
                superseded_at=result.superseded_at,
                created_at=result.created_at,
            )
        )
        self._s.flush()

    def list_for_task(self, task_id: str) -> Sequence[AcceptanceResult]:
        rows = self._s.scalars(
            select(AcceptanceResultRow)
            .where(AcceptanceResultRow.task_id == task_id)
            .order_by(AcceptanceResultRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]

    def supersede_for_task(self, task_id: str, at: datetime) -> None:
        self._s.execute(
            update(AcceptanceResultRow)
            .where(
                AcceptanceResultRow.task_id == task_id,
                AcceptanceResultRow.superseded_at.is_(None),
            )
            .values(superseded_at=at)
        )


class Decisions:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: DecisionRow) -> Decision:
        return Decision(
            id=row.id,
            task_id=row.task_id,
            escalation_id=row.escalation_id,
            principal_id=row.principal_id,
            kind=row.kind,
            verbatim=row.verbatim,
            resolves=row.resolves,
            created_at=ensure_utc(row.created_at),
        )

    def add(self, decision: Decision) -> None:
        self._s.add(
            DecisionRow(
                id=decision.id,
                task_id=decision.task_id,
                escalation_id=decision.escalation_id,
                principal_id=decision.principal_id,
                kind=decision.kind,
                verbatim=decision.verbatim,
                resolves=decision.resolves,
                created_at=decision.created_at,
            )
        )
        self._s.flush()

    def list_for_task(self, task_id: str) -> Sequence[Decision]:
        rows = self._s.scalars(
            select(DecisionRow).where(DecisionRow.task_id == task_id).order_by(DecisionRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class Escalations:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: EscalationRow) -> Escalation:
        return Escalation(
            id=row.id,
            task_id=row.task_id,
            attempt_id=row.attempt_id,
            state=EscalationState(row.state),
            question=row.question,
            opened_at=ensure_utc(row.opened_at),
            closed_at=_dt(row.closed_at),
            decision_id=row.decision_id,
            last_wake_at=_dt(row.last_wake_at),
            reason=row.reason,
        )

    def add(self, escalation: Escalation) -> None:
        self._s.add(
            EscalationRow(
                id=escalation.id,
                task_id=escalation.task_id,
                attempt_id=escalation.attempt_id,
                state=escalation.state.value,
                question=escalation.question,
                opened_at=escalation.opened_at,
                closed_at=escalation.closed_at,
                decision_id=escalation.decision_id,
                last_wake_at=escalation.last_wake_at,
                reason=escalation.reason,
            )
        )
        self._s.flush()

    def get(self, escalation_id: str, *, for_update: bool = False) -> Escalation | None:
        stmt = select(EscalationRow).where(EscalationRow.id == escalation_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = self._s.scalar(stmt)
        return self._to_entity(row) if row else None

    def save(self, escalation: Escalation) -> None:
        self._s.execute(
            update(EscalationRow)
            .where(EscalationRow.id == escalation.id)
            .values(
                state=escalation.state.value,
                closed_at=escalation.closed_at,
                decision_id=escalation.decision_id,
                last_wake_at=escalation.last_wake_at,
            )
        )

    def list_for_task(self, task_id: str) -> Sequence[Escalation]:
        rows = self._s.scalars(
            select(EscalationRow).where(EscalationRow.task_id == task_id).order_by(EscalationRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]

    def list_open(self) -> Sequence[Escalation]:
        rows = self._s.scalars(
            select(EscalationRow)
            .where(EscalationRow.state == EscalationState.OPEN.value)
            .order_by(EscalationRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class Dispositions:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: ReviewDispositionRow) -> ReviewDisposition:
        return ReviewDisposition(
            id=row.id,
            review_comment_id=row.review_comment_id,
            comment_body_sha256=row.comment_body_sha256,
            principal_id=row.principal_id,
            disposition=DispositionKind(row.disposition),
            reasoning=row.reasoning,
            created_at=ensure_utc(row.created_at),
        )

    def add(self, disposition: ReviewDisposition) -> None:
        self._s.add(
            ReviewDispositionRow(
                id=disposition.id,
                review_comment_id=disposition.review_comment_id,
                comment_body_sha256=disposition.comment_body_sha256,
                principal_id=disposition.principal_id,
                disposition=disposition.disposition.value,
                reasoning=disposition.reasoning,
                created_at=disposition.created_at,
            )
        )
        self._s.flush()

    def list_for_comments(
        self,
        comment_ids: Sequence[str],
        comment_body_sha256_by_comment: Mapping[str, str] | None = None,
    ) -> Sequence[ReviewDisposition]:
        if not comment_ids:
            return []
        rows = self._s.scalars(
            select(ReviewDispositionRow).where(
                ReviewDispositionRow.review_comment_id.in_(list(comment_ids))
            )
        ).all()
        dispositions = [self._to_entity(r) for r in rows]
        if comment_body_sha256_by_comment is None:
            return dispositions
        return [
            disposition
            for disposition in dispositions
            if comment_body_sha256_by_comment.get(disposition.review_comment_id)
            == disposition.comment_body_sha256
        ]

    def get_by_comment(
        self, review_comment_id: str, comment_body_sha256: str | None = None
    ) -> ReviewDisposition | None:
        conditions = [ReviewDispositionRow.review_comment_id == review_comment_id]
        if comment_body_sha256 is not None:
            conditions.append(ReviewDispositionRow.comment_body_sha256 == comment_body_sha256)
        row = self._s.scalar(
            select(ReviewDispositionRow)
            .where(*conditions)
            .order_by(ReviewDispositionRow.created_at.desc(), ReviewDispositionRow.id.desc())
        )
        return self._to_entity(row) if row else None


class Wakes:
    def __init__(self, session: Session) -> None:
        self._s = session

    def list_acked_before(self, cutoff: datetime, limit: int) -> Sequence[Wake]:
        rows = self._s.scalars(
            select(WakeRow)
            .where(WakeRow.acked_at.is_not(None), WakeRow.acked_at < cutoff)
            .order_by(WakeRow.acked_at)
            .limit(limit)
        ).all()
        return [self._to_entity(row) for row in rows]

    def delete(self, wake_id: str) -> bool:
        row = self._s.get(WakeRow, wake_id)
        if row is None:
            return False
        self._s.delete(row)
        self._s.flush()
        return True

    @staticmethod
    def _to_entity(row: WakeRow) -> Wake:
        return Wake(
            id=row.id,
            principal_id=row.principal_id,
            task_id=row.task_id,
            reason=row.reason,
            payload=row.payload,
            created_at=ensure_utc(row.created_at),
            attempts=row.attempts,
            delivered_at=_dt(row.delivered_at),
            acked_at=_dt(row.acked_at),
            ack_note=row.ack_note,
            next_attempt_at=_dt(row.next_attempt_at),
            last_error=row.last_error,
            gave_up_at=_dt(row.gave_up_at),
        )

    def add(self, wake: Wake) -> None:
        self._s.add(
            WakeRow(
                id=wake.id,
                principal_id=wake.principal_id,
                task_id=wake.task_id,
                reason=wake.reason,
                payload=wake.payload,
                created_at=wake.created_at,
                attempts=wake.attempts,
                delivered_at=wake.delivered_at,
                acked_at=wake.acked_at,
                ack_note=wake.ack_note,
                next_attempt_at=wake.next_attempt_at,
                last_error=wake.last_error,
                gave_up_at=wake.gave_up_at,
            )
        )
        self._s.flush()

    def get(self, wake_id: str, *, for_update: bool = False) -> Wake | None:
        stmt = select(WakeRow).where(WakeRow.id == wake_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = self._s.scalar(stmt)
        return self._to_entity(row) if row else None

    def save(self, wake: Wake) -> None:
        self._s.execute(
            update(WakeRow)
            .where(WakeRow.id == wake.id)
            .values(
                attempts=wake.attempts,
                delivered_at=wake.delivered_at,
                acked_at=wake.acked_at,
                ack_note=wake.ack_note,
                next_attempt_at=wake.next_attempt_at,
                last_error=wake.last_error,
                gave_up_at=wake.gave_up_at,
            )
        )

    def list_for_principal(
        self, principal_id: str, *, since: datetime | None, include_acked: bool, limit: int
    ) -> Sequence[Wake]:
        stmt = select(WakeRow).where(WakeRow.principal_id == principal_id)
        if not include_acked:
            stmt = stmt.where(WakeRow.acked_at.is_(None))
        if since is not None:
            stmt = stmt.where(WakeRow.created_at >= since)
        rows = self._s.scalars(stmt.order_by(WakeRow.id).limit(limit)).all()
        return [self._to_entity(r) for r in rows]

    def list_undelivered(self, now: datetime) -> Sequence[Wake]:
        rows = self._s.scalars(
            select(WakeRow)
            .where(
                WakeRow.delivered_at.is_(None),
                WakeRow.acked_at.is_(None),
                WakeRow.gave_up_at.is_(None),
            )
            .order_by(WakeRow.id)
        ).all()
        ready = [
            r for r in rows if r.next_attempt_at is None or ensure_utc(r.next_attempt_at) <= now
        ]
        return [self._to_entity(r) for r in ready]

    def count_unacked(self) -> int:
        return int(
            self._s.scalar(
                select(func.count()).select_from(WakeRow).where(WakeRow.acked_at.is_(None))
            )
            or 0
        )

    def count_unacked_for_principal(self, principal_id: str) -> int:
        return int(
            self._s.scalar(
                select(func.count())
                .select_from(WakeRow)
                .where(WakeRow.acked_at.is_(None), WakeRow.principal_id == principal_id)
            )
            or 0
        )

    def pending_summary(
        self, *, principal_id: str | None = None
    ) -> tuple[Mapping[str, int], datetime | None, int]:
        stmt = (
            select(PrincipalRow.name, func.count(), func.min(WakeRow.created_at))
            .join(WakeRow, WakeRow.principal_id == PrincipalRow.id)
            .where(WakeRow.acked_at.is_(None))
            .group_by(PrincipalRow.name)
        )
        if principal_id is not None:
            stmt = stmt.where(PrincipalRow.id == principal_id)
        rows = list(self._s.execute(stmt))
        counts = {name: int(count) for name, count, _oldest in rows}
        oldest_values = [ensure_utc(oldest) for _name, _count, oldest in rows if oldest]
        return counts, min(oldest_values, default=None), sum(counts.values())


class AttemptMetricsRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: AttemptMetricsRow) -> AttemptMetrics:
        return AttemptMetrics(
            attempt_id=row.attempt_id,
            task_id=row.task_id,
            model=row.model,
            harness=row.harness,
            endpoint_kind=row.endpoint_kind,
            pool=row.pool,
            wall_ms=row.wall_ms,
            harness_duration_ms=row.harness_duration_ms,
            tool_calls=row.tool_calls,
            tokens_in=row.tokens_in,
            tokens_out=row.tokens_out,
            cost_units=row.cost_units,
            cost_source=row.cost_source,
            model_reported=row.model_reported,
            exit_class=row.exit_class,
            gates_passed=row.gates_passed,
            gates_failed=row.gates_failed,
            corrections_after=row.corrections_after,
            acceptance_verdict=row.acceptance_verdict,
            created_at=ensure_utc(row.created_at),
        )

    def put(self, metrics: AttemptMetrics) -> None:
        row = self._s.get(AttemptMetricsRow, metrics.attempt_id)
        if row is None:
            row = AttemptMetricsRow(
                attempt_id=metrics.attempt_id,
                task_id=metrics.task_id,
                created_at=metrics.created_at,
            )
            self._s.add(row)
        row.model = metrics.model
        row.harness = metrics.harness
        row.endpoint_kind = metrics.endpoint_kind
        row.pool = metrics.pool
        row.wall_ms = metrics.wall_ms
        row.harness_duration_ms = metrics.harness_duration_ms
        row.tool_calls = metrics.tool_calls
        row.tokens_in = metrics.tokens_in
        row.tokens_out = metrics.tokens_out
        row.cost_units = metrics.cost_units
        row.cost_source = metrics.cost_source
        row.model_reported = metrics.model_reported
        row.exit_class = metrics.exit_class
        row.gates_passed = metrics.gates_passed
        row.gates_failed = metrics.gates_failed
        row.corrections_after = metrics.corrections_after
        row.acceptance_verdict = metrics.acceptance_verdict
        self._s.flush()

    def get(self, attempt_id: str) -> AttemptMetrics | None:
        row = self._s.get(AttemptMetricsRow, attempt_id)
        return self._to_entity(row) if row else None

    def list_since(
        self, *, since: datetime | None, model: str | None, task_ids: Sequence[str] | None
    ) -> Sequence[AttemptMetrics]:
        stmt = select(AttemptMetricsRow)
        if since is not None:
            stmt = stmt.where(AttemptMetricsRow.created_at >= since)
        if model is not None:
            stmt = stmt.where(AttemptMetricsRow.model == model)
        if task_ids is not None:
            stmt = stmt.where(AttemptMetricsRow.task_id.in_(list(task_ids)))
        rows = self._s.scalars(stmt.order_by(AttemptMetricsRow.created_at)).all()
        return [self._to_entity(r) for r in rows]

    def recent_for_project(
        self, *, project: str, models: Sequence[str], limit_per_model: int
    ) -> Sequence[AttemptMetrics]:
        if not models:
            return []
        ranked = (
            select(
                AttemptMetricsRow.attempt_id.label("attempt_id"),
                func.row_number()
                .over(
                    partition_by=AttemptMetricsRow.model,
                    order_by=(
                        AttemptMetricsRow.created_at.desc(),
                        AttemptMetricsRow.attempt_id.desc(),
                    ),
                )
                .label("recency"),
            )
            .join(TaskRow, TaskRow.id == AttemptMetricsRow.task_id)
            .where(TaskRow.project == project, AttemptMetricsRow.model.in_(list(models)))
            .subquery()
        )
        rows = self._s.scalars(
            select(AttemptMetricsRow)
            .join(ranked, ranked.c.attempt_id == AttemptMetricsRow.attempt_id)
            .where(ranked.c.recency <= limit_per_model)
            .order_by(AttemptMetricsRow.model, AttemptMetricsRow.created_at)
        ).all()
        return [self._to_entity(row) for row in rows]


# ----- C5: harness administration (07, 25) ---------------------------------


class HarnessStates:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: HarnessStateRow) -> HarnessState:
        return HarnessState(
            name=row.name,
            enabled=row.enabled,
            enabled_decided=bool(row.enabled_decided),
            reason=row.reason,
            session_compatibility=row.session_compatibility,
            updated_at=ensure_utc(row.updated_at),
            updated_by=row.updated_by,
            mount_mode_observed=row.mount_mode_observed,
            refresh_requires_rw=row.refresh_requires_rw,
            last_launch_at=_dt(row.last_launch_at),
            last_launch_outcome=row.last_launch_outcome,
            last_auth_failure_at=_dt(row.last_auth_failure_at),
            last_validated_at=_dt(row.last_validated_at),
            last_test=dict(row.last_test) if row.last_test is not None else None,
        )

    def get(self, name: str, *, for_update: bool = False) -> HarnessState | None:
        if for_update:
            row = self._s.get(HarnessStateRow, name, with_for_update=True, populate_existing=True)
        else:
            row = self._s.get(HarnessStateRow, name)
        return self._to_entity(row) if row else None

    def list_all(self) -> Sequence[HarnessState]:
        rows = self._s.scalars(select(HarnessStateRow).order_by(HarnessStateRow.name)).all()
        return [self._to_entity(r) for r in rows]

    def put(self, state: HarnessState) -> HarnessState:
        row = self._s.get(HarnessStateRow, state.name)
        if row is None:
            row = HarnessStateRow(name=state.name)
            self._s.add(row)
        row.enabled = state.enabled
        row.enabled_decided = state.enabled_decided
        row.reason = state.reason
        row.session_compatibility = state.session_compatibility
        row.mount_mode_observed = state.mount_mode_observed
        row.refresh_requires_rw = state.refresh_requires_rw
        row.last_launch_at = state.last_launch_at
        row.last_launch_outcome = state.last_launch_outcome
        row.last_auth_failure_at = state.last_auth_failure_at
        row.last_validated_at = state.last_validated_at
        row.last_test = state.last_test
        row.updated_at = state.updated_at
        row.updated_by = state.updated_by
        self._s.flush()
        return self._to_entity(row)


class HarnessImages:
    """Each harness's default worker image (13, ADR 0018)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: HarnessImageRow) -> HarnessImage:
        return HarnessImage(
            harness=row.harness,
            digest=row.digest,
            reference=row.reference,
            version=row.version,
            updated_at=ensure_utc(row.updated_at),
            updated_by=row.updated_by,
            reason=row.reason,
            previous_digest=row.previous_digest,
            previous_reference=row.previous_reference,
            previous_version=row.previous_version,
        )

    def get(self, harness: str) -> HarnessImage | None:
        row = self._s.get(HarnessImageRow, harness)
        return self._to_entity(row) if row else None

    def list_all(self) -> Sequence[HarnessImage]:
        rows = self._s.scalars(select(HarnessImageRow).order_by(HarnessImageRow.harness)).all()
        return [self._to_entity(r) for r in rows]

    def put(self, image: HarnessImage) -> HarnessImage:
        row = self._s.get(HarnessImageRow, image.harness)
        if row is None:
            row = HarnessImageRow(harness=image.harness)
            self._s.add(row)
        row.digest = image.digest
        row.reference = image.reference
        row.version = image.version
        row.previous_digest = image.previous_digest
        row.previous_reference = image.previous_reference
        row.previous_version = image.previous_version
        row.reason = image.reason
        row.updated_at = image.updated_at
        row.updated_by = image.updated_by
        self._s.flush()
        return self._to_entity(row)


class ProviderSettings:
    """Runtime provider settings (25, crucible#91): one document per name."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, name: str) -> ProviderSetting | None:
        row = self._s.get(ProviderSettingRow, name)
        if row is None:
            return None
        return ProviderSetting(
            name=row.name,
            document=dict(row.document or {}),
            updated_at=ensure_utc(row.updated_at),
            updated_by=row.updated_by,
            reason=row.reason,
        )

    def put(self, setting: ProviderSetting) -> ProviderSetting:
        row = self._s.get(ProviderSettingRow, setting.name, with_for_update=True)
        if row is None:
            row = ProviderSettingRow(name=setting.name)
            self._s.add(row)
        row.document = dict(setting.document)
        row.reason = setting.reason
        row.updated_at = setting.updated_at
        row.updated_by = setting.updated_by
        self._s.flush()
        stored = self.get(setting.name)
        assert stored is not None
        return stored


class GitHubManifestStates:
    """The starts of the GitHub App manifest flow (crucible#168)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: GitHubManifestStateRow) -> GitHubManifestState:
        return GitHubManifestState(
            state_hash=row.state_hash,
            browser_hash=row.browser_hash,
            principal=row.principal,
            app_name=row.app_name,
            organization=row.organization,
            external_url=row.external_url,
            created_at=ensure_utc(row.created_at),
            expires_at=ensure_utc(row.expires_at),
            consumed_at=_dt(row.consumed_at),
        )

    def add(self, state: GitHubManifestState) -> None:
        self._s.add(
            GitHubManifestStateRow(
                state_hash=state.state_hash,
                browser_hash=state.browser_hash,
                principal=state.principal,
                app_name=state.app_name,
                organization=state.organization,
                external_url=state.external_url,
                created_at=state.created_at,
                expires_at=state.expires_at,
                consumed_at=state.consumed_at,
            )
        )
        self._s.flush()

    def consume(
        self, state_hash: str, now: datetime, *, principal: str, browser_hash: str
    ) -> GitHubManifestState | None:
        row = self._s.get(GitHubManifestStateRow, state_hash, with_for_update=True)
        if row is None:
            return None
        before = self._to_entity(row)
        if (
            row.consumed_at is None
            and row.principal == principal
            and row.browser_hash == browser_hash
        ):
            row.consumed_at = now
            self._s.flush()
        return before

    def prune(self, before: datetime) -> int:
        result = self._s.execute(
            delete(GitHubManifestStateRow).where(GitHubManifestStateRow.expires_at < before)
        )
        return int(getattr(result, "rowcount", 0) or 0)


class BootstrapImports:
    """The bootstrap imports of 15: verified, then authoritative; at most one authoritative."""

    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: BootstrapImportRow) -> BootstrapImport:
        return BootstrapImport(
            id=row.id,
            state=row.state,
            schema_version=row.schema_version,
            content_sha256=row.content_sha256,
            source_sha256=row.source_sha256,
            source=dict(row.source),
            manifest=dict(row.manifest),
            principal_id=row.principal_id,
            imported_by=row.imported_by,
            verified_at=ensure_utc(row.verified_at),
            committed_at=_dt(row.committed_at),
            committed_by=row.committed_by,
        )

    def add(self, record: BootstrapImport) -> None:
        self._s.add(
            BootstrapImportRow(
                id=record.id,
                state=record.state,
                schema_version=record.schema_version,
                content_sha256=record.content_sha256,
                source_sha256=record.source_sha256,
                source=dict(record.source),
                manifest=dict(record.manifest),
                principal_id=record.principal_id,
                imported_by=record.imported_by,
                verified_at=record.verified_at,
                committed_at=record.committed_at,
                committed_by=record.committed_by,
            )
        )
        self._s.flush()

    def get(self, import_id: str, *, for_update: bool = False) -> BootstrapImport | None:
        stmt = select(BootstrapImportRow).where(BootstrapImportRow.id == import_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = self._s.scalar(stmt)
        return self._to_entity(row) if row else None

    def get_by_content(self, content_sha256: str) -> BootstrapImport | None:
        row = self._s.scalars(
            select(BootstrapImportRow)
            .where(BootstrapImportRow.content_sha256 == content_sha256)
            # A discarded import is withdrawn: the same bundle imports afresh (ADR 0029).
            .where(BootstrapImportRow.state != "discarded")
            .order_by(BootstrapImportRow.id.desc())
            .limit(1)
        ).first()
        return self._to_entity(row) if row else None

    def save(self, record: BootstrapImport) -> None:
        self._s.execute(
            update(BootstrapImportRow)
            .where(BootstrapImportRow.id == record.id)
            .values(
                state=record.state,
                manifest=dict(record.manifest),
                committed_at=record.committed_at,
                committed_by=record.committed_by,
            )
        )
        self._s.flush()

    def list_all(self) -> Sequence[BootstrapImport]:
        rows = self._s.scalars(
            select(BootstrapImportRow).order_by(BootstrapImportRow.id.desc())
        ).all()
        return [self._to_entity(r) for r in rows]

    def authoritative(self, *, for_update: bool = False) -> BootstrapImport | None:
        stmt = select(BootstrapImportRow).where(BootstrapImportRow.state == "authoritative")
        if for_update:
            stmt = stmt.with_for_update()
        row = self._s.scalars(stmt.order_by(BootstrapImportRow.id).limit(1)).first()
        return self._to_entity(row) if row else None
