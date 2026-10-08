"""SQLAlchemy repositories for the C4 delivery tables (14, 23).

Pull requests, head history, review cycles and their signals, reactions, CI
certifications and decisions, and webhook deliveries. Every one of these except the two
decision tables is written only by the supervisor and is fenced accordingly, so a write
here outside a fenced transaction is refused by the database, not by a convention.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from crucible.adapters.persistence.models import (
    CICertificationRow,
    CIDecisionRow,
    ExternalReviewCycleRow,
    ExternalReviewRow,
    GitHubDeliveryRow,
    PullRequestHeadRow,
    PullRequestRow,
    ReactionRow,
    ReviewCommentRow,
)
from crucible.domain.entities import (
    CICertification,
    CIDecision,
    ExternalReview,
    ExternalReviewCycle,
    GitHubDelivery,
    PullRequest,
    PullRequestHead,
    PullRequestState,
    PushedBy,
    Reaction,
    ReviewComment,
)
from crucible.domain.time import ensure_utc


def _dt(value: datetime | None) -> datetime | None:
    return ensure_utc(value) if value is not None else None


class PullRequests:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: PullRequestRow) -> PullRequest:
        return PullRequest(
            id=row.id,
            task_id=row.task_id,
            repository_id=row.repository_id,
            number=row.number,
            url=row.url,
            base_ref=row.base_ref,
            work_branch=row.work_branch,
            state=PullRequestState(row.state),
            head_sha=row.head_sha,
            title=row.title,
            body_sha256=row.body_sha256,
            opened_at=ensure_utc(row.opened_at),
            merged_at=_dt(row.merged_at),
            merge_sha=row.merge_sha,
            merged_by=row.merged_by,
            closed_at=_dt(row.closed_at),
            closed_by=row.closed_by,
            last_polled_at=_dt(row.last_polled_at),
            last_reactions_polled_at=_dt(row.last_reactions_polled_at),
            reactions_observable=row.reactions_observable,
            cancelled_at=_dt(row.cancelled_at),
            observed_head_sha=row.observed_head_sha,
            observed_base_ref=row.observed_base_ref,
            mergeable_state=row.mergeable_state,
            mergeable=row.mergeable,
            merge_refusal_cause=row.merge_refusal_cause,
            merge_refusal_head_sha=row.merge_refusal_head_sha,
            merge_refusal_base_ref=row.merge_refusal_base_ref,
            merge_refusal_mergeable_state=row.merge_refusal_mergeable_state,
            merge_refusal_count=row.merge_refusal_count,
            merge_retry_at=_dt(row.merge_retry_at),
        )

    def add(self, pull_request: PullRequest) -> None:
        self._s.add(
            PullRequestRow(
                id=pull_request.id,
                task_id=pull_request.task_id,
                repository_id=pull_request.repository_id,
                number=pull_request.number,
                url=pull_request.url,
                base_ref=pull_request.base_ref,
                work_branch=pull_request.work_branch,
                state=pull_request.state.value,
                head_sha=pull_request.head_sha,
                title=pull_request.title,
                body_sha256=pull_request.body_sha256,
                opened_at=pull_request.opened_at,
                merged_at=pull_request.merged_at,
                merge_sha=pull_request.merge_sha,
                merged_by=pull_request.merged_by,
                closed_at=pull_request.closed_at,
                closed_by=pull_request.closed_by,
                last_polled_at=pull_request.last_polled_at,
                last_reactions_polled_at=pull_request.last_reactions_polled_at,
                reactions_observable=pull_request.reactions_observable,
                cancelled_at=pull_request.cancelled_at,
                observed_head_sha=pull_request.observed_head_sha,
                observed_base_ref=pull_request.observed_base_ref,
                mergeable_state=pull_request.mergeable_state,
                mergeable=pull_request.mergeable,
                merge_refusal_cause=pull_request.merge_refusal_cause,
                merge_refusal_head_sha=pull_request.merge_refusal_head_sha,
                merge_refusal_base_ref=pull_request.merge_refusal_base_ref,
                merge_refusal_mergeable_state=pull_request.merge_refusal_mergeable_state,
                merge_refusal_count=pull_request.merge_refusal_count,
                merge_retry_at=pull_request.merge_retry_at,
            )
        )
        self._s.flush()

    def get(self, pull_request_id: str, *, for_update: bool = False) -> PullRequest | None:
        stmt = select(PullRequestRow).where(PullRequestRow.id == pull_request_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = self._s.scalars(stmt).one_or_none()
        return self._to_entity(row) if row else None

    def get_for_task(self, task_id: str, *, for_update: bool = False) -> PullRequest | None:
        stmt = select(PullRequestRow).where(PullRequestRow.task_id == task_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = self._s.scalars(stmt).one_or_none()
        return self._to_entity(row) if row else None

    def save(self, pull_request: PullRequest) -> None:
        row = self._s.get(PullRequestRow, pull_request.id)
        if row is None:
            return
        row.number = pull_request.number
        row.url = pull_request.url
        row.state = pull_request.state.value
        row.head_sha = pull_request.head_sha
        row.title = pull_request.title
        row.body_sha256 = pull_request.body_sha256
        row.base_ref = pull_request.base_ref
        row.merged_at = pull_request.merged_at
        row.merge_sha = pull_request.merge_sha
        row.merged_by = pull_request.merged_by
        row.closed_at = pull_request.closed_at
        row.closed_by = pull_request.closed_by
        row.last_polled_at = pull_request.last_polled_at
        row.last_reactions_polled_at = pull_request.last_reactions_polled_at
        row.reactions_observable = pull_request.reactions_observable
        row.cancelled_at = pull_request.cancelled_at
        row.observed_head_sha = pull_request.observed_head_sha
        row.observed_base_ref = pull_request.observed_base_ref
        row.mergeable_state = pull_request.mergeable_state
        row.mergeable = pull_request.mergeable
        row.merge_refusal_cause = pull_request.merge_refusal_cause
        row.merge_refusal_head_sha = pull_request.merge_refusal_head_sha
        row.merge_refusal_base_ref = pull_request.merge_refusal_base_ref
        row.merge_refusal_mergeable_state = pull_request.merge_refusal_mergeable_state
        row.merge_refusal_count = pull_request.merge_refusal_count
        row.merge_retry_at = pull_request.merge_retry_at
        self._s.flush()

    def list_in_states(self, states: Sequence[PullRequestState]) -> Sequence[PullRequest]:
        rows = self._s.scalars(
            select(PullRequestRow)
            .where(PullRequestRow.state.in_([s.value for s in states]))
            .order_by(PullRequestRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class PullRequestHeads:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: PullRequestHeadRow) -> PullRequestHead:
        return PullRequestHead(
            id=row.id,
            pull_request_id=row.pull_request_id,
            sha=row.sha,
            pushed_by=PushedBy(row.pushed_by),
            observed_at=ensure_utc(row.observed_at),
        )

    def add(self, head: PullRequestHead) -> bool:
        """Insert, or report that this (pull request, sha) is already recorded.

        A head Crucible pushed and then observed must not become a second row that looks
        like an out-of-band push."""
        # RETURNING rather than rowcount: an INSERT ... ON CONFLICT DO NOTHING can
        # report an unknown rowcount, and "unknown" read as truthy would call every
        # repeat a new head.
        inserted = self._s.execute(
            pg_insert(PullRequestHeadRow)
            .values(
                id=head.id,
                pull_request_id=head.pull_request_id,
                sha=head.sha,
                pushed_by=head.pushed_by.value,
                observed_at=head.observed_at,
            )
            .on_conflict_do_nothing(constraint="uq_pull_request_heads_sha")
            .returning(PullRequestHeadRow.id)
        ).scalar_one_or_none()
        self._s.flush()
        return inserted is not None

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[PullRequestHead]:
        rows = self._s.scalars(
            select(PullRequestHeadRow)
            .where(PullRequestHeadRow.pull_request_id == pull_request_id)
            .order_by(PullRequestHeadRow.observed_at, PullRequestHeadRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class ExternalReviewCycles:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: ExternalReviewCycleRow) -> ExternalReviewCycle:
        return ExternalReviewCycle(
            id=row.id,
            pull_request_id=row.pull_request_id,
            head_sha=row.head_sha,
            components=[str(c) for c in row.components],
            completed_components={str(k): str(v) for k, v in row.completed_components.items()},
            state=row.state,
            opened_at=ensure_utc(row.opened_at),
            completed_at=_dt(row.completed_at),
            trigger=row.trigger,
        )

    def add(self, cycle: ExternalReviewCycle) -> None:
        self._s.add(
            ExternalReviewCycleRow(
                id=cycle.id,
                pull_request_id=cycle.pull_request_id,
                head_sha=cycle.head_sha,
                components=list(cycle.components),
                completed_components=dict(cycle.completed_components),
                state=cycle.state,
                trigger=cycle.trigger,
                opened_at=cycle.opened_at,
                completed_at=cycle.completed_at,
            )
        )
        self._s.flush()

    def save(self, cycle: ExternalReviewCycle) -> None:
        row = self._s.get(ExternalReviewCycleRow, cycle.id)
        if row is None:
            return
        row.completed_components = dict(cycle.completed_components)
        row.state = cycle.state
        row.completed_at = cycle.completed_at
        self._s.flush()

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[ExternalReviewCycle]:
        rows = self._s.scalars(
            select(ExternalReviewCycleRow)
            .where(ExternalReviewCycleRow.pull_request_id == pull_request_id)
            .order_by(ExternalReviewCycleRow.opened_at, ExternalReviewCycleRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class ExternalReviews:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: ExternalReviewRow) -> ExternalReview:
        return ExternalReview(
            id=row.id,
            pull_request_id=row.pull_request_id,
            cycle_id=row.cycle_id,
            reviewer_login=row.reviewer_login,
            signal=row.signal,
            github_id=row.github_id,
            reviewed_sha=row.reviewed_sha,
            body=row.body,
            body_sha256=row.body_sha256,
            received_at=ensure_utc(row.received_at),
            state=row.state,
            accepted=row.accepted,
            sha_inferred=row.sha_inferred,
        )

    def add(self, review: ExternalReview) -> bool:
        """Insert, or report that this signal is already recorded.

        A poll can see the same object twice in one batch, and GitHub re-delivers. An
        IntegrityError here would roll back the whole tick, so the duplicate is a no-op
        and the caller learns which it was."""
        inserted = self._s.execute(
            pg_insert(ExternalReviewRow)
            .values(
                id=review.id,
                pull_request_id=review.pull_request_id,
                cycle_id=review.cycle_id,
                reviewer_login=review.reviewer_login,
                signal=review.signal,
                github_id=review.github_id,
                reviewed_sha=review.reviewed_sha,
                sha_inferred=review.sha_inferred,
                state=review.state,
                body=review.body,
                body_sha256=review.body_sha256,
                accepted=review.accepted,
                received_at=review.received_at,
            )
            .on_conflict_do_nothing(constraint="uq_external_reviews_github_id")
            .returning(ExternalReviewRow.id)
        ).scalar_one_or_none()
        self._s.flush()
        return inserted is not None

    def get_by_github(
        self, pull_request_id: str, signal: str, github_id: str
    ) -> ExternalReview | None:
        row = self._s.scalars(
            select(ExternalReviewRow).where(
                ExternalReviewRow.pull_request_id == pull_request_id,
                ExternalReviewRow.signal == signal,
                ExternalReviewRow.github_id == github_id,
            )
        ).one_or_none()
        return self._to_entity(row) if row else None

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[ExternalReview]:
        rows = self._s.scalars(
            select(ExternalReviewRow)
            .where(ExternalReviewRow.pull_request_id == pull_request_id)
            .order_by(ExternalReviewRow.received_at, ExternalReviewRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class ReviewComments:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: ReviewCommentRow) -> ReviewComment:
        return ReviewComment(
            id=row.id,
            pull_request_id=row.pull_request_id,
            external_review_id=row.external_review_id,
            github_id=row.github_id,
            kind=row.kind,
            login=row.login,
            path=row.path,
            line=row.line,
            body=row.body,
            body_sha256=row.body_sha256,
            created_at=ensure_utc(row.created_at),
            updated_at=ensure_utc(row.updated_at),
            reviewed_sha=row.reviewed_sha,
        )

    def add(self, comment: ReviewComment) -> bool:
        """Insert, or report that this comment is already recorded (see ExternalReviews)."""
        inserted = self._s.execute(
            pg_insert(ReviewCommentRow)
            .values(
                id=comment.id,
                pull_request_id=comment.pull_request_id,
                external_review_id=comment.external_review_id,
                github_id=comment.github_id,
                kind=comment.kind,
                login=comment.login,
                path=comment.path,
                line=comment.line,
                body=comment.body,
                body_sha256=comment.body_sha256,
                reviewed_sha=comment.reviewed_sha,
                created_at=comment.created_at,
                updated_at=comment.updated_at,
            )
            .on_conflict_do_nothing(constraint="uq_review_comments_github_id")
            .returning(ReviewCommentRow.id)
        ).scalar_one_or_none()
        self._s.flush()
        return inserted is not None

    def save(self, comment: ReviewComment) -> None:
        """An issue comment the reviewer edits in place is a change, not a new object
        (S12), so the body and its hash are updated and the row keeps its id."""
        row = self._s.get(ReviewCommentRow, comment.id)
        if row is None:
            return
        row.body = comment.body
        row.body_sha256 = comment.body_sha256
        row.updated_at = comment.updated_at
        row.external_review_id = comment.external_review_id
        self._s.flush()

    def get(self, comment_id: str) -> ReviewComment | None:
        row = self._s.get(ReviewCommentRow, comment_id)
        return self._to_entity(row) if row else None

    def get_by_github(
        self, pull_request_id: str, kind: str, github_id: str
    ) -> ReviewComment | None:
        row = self._s.scalars(
            select(ReviewCommentRow).where(
                ReviewCommentRow.pull_request_id == pull_request_id,
                ReviewCommentRow.kind == kind,
                ReviewCommentRow.github_id == github_id,
            )
        ).one_or_none()
        return self._to_entity(row) if row else None

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[ReviewComment]:
        rows = self._s.scalars(
            select(ReviewCommentRow)
            .where(ReviewCommentRow.pull_request_id == pull_request_id)
            .order_by(ReviewCommentRow.created_at, ReviewCommentRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class Reactions:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: ReactionRow) -> Reaction:
        return Reaction(
            id=row.id,
            pull_request_id=row.pull_request_id,
            subject_kind=row.subject_kind,
            subject_github_id=row.subject_github_id,
            github_id=row.github_id,
            login=row.login,
            content=row.content,
            created_at=_dt(row.created_at),
            observed_at=ensure_utc(row.observed_at),
            removed_at=_dt(row.removed_at),
        )

    def add(self, reaction: Reaction) -> bool:
        """Insert, or report that this reaction is already recorded (see ExternalReviews)."""
        inserted = self._s.execute(
            pg_insert(ReactionRow)
            .values(
                id=reaction.id,
                pull_request_id=reaction.pull_request_id,
                subject_kind=reaction.subject_kind,
                subject_github_id=reaction.subject_github_id,
                github_id=reaction.github_id,
                login=reaction.login,
                content=reaction.content,
                created_at=reaction.created_at,
                observed_at=reaction.observed_at,
                removed_at=reaction.removed_at,
            )
            .on_conflict_do_nothing(constraint="uq_reactions_github_id")
            .returning(ReactionRow.id)
        ).scalar_one_or_none()
        self._s.flush()
        return inserted is not None

    def save(self, reaction: Reaction) -> None:
        row = self._s.get(ReactionRow, reaction.id)
        if row is None:
            return
        row.removed_at = reaction.removed_at
        self._s.flush()

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[Reaction]:
        rows = self._s.scalars(
            select(ReactionRow)
            .where(ReactionRow.pull_request_id == pull_request_id)
            .order_by(ReactionRow.observed_at, ReactionRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class CICertifications:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: CICertificationRow) -> CICertification:
        return CICertification(
            id=row.id,
            pull_request_id=row.pull_request_id,
            task_id=row.task_id,
            head_sha=row.head_sha,
            state=row.state,
            required_checks=list(row.required_checks),
            check_runs=list(row.check_runs),
            failure=dict(row.failure),
            detail=row.detail,
            evaluated_at=ensure_utc(row.evaluated_at),
            change_class=row.change_class,
        )

    def put(self, certification: CICertification) -> CICertification:
        """One row per (pull request, head): re-evaluating a head updates it in place, so
        running the tick twice with nothing new changes nothing (10)."""
        row = self._s.scalars(
            select(CICertificationRow).where(
                CICertificationRow.pull_request_id == certification.pull_request_id,
                CICertificationRow.head_sha == certification.head_sha,
            )
        ).one_or_none()
        if row is None:
            # One row per (pull request, head). A concurrent tick that inserted it first
            # must not roll this one back: the insert yields, and the value is read back.
            inserted = self._s.execute(
                pg_insert(CICertificationRow)
                .values(
                    id=certification.id,
                    pull_request_id=certification.pull_request_id,
                    task_id=certification.task_id,
                    head_sha=certification.head_sha,
                    state=certification.state,
                    required_checks=list(certification.required_checks),
                    check_runs=list(certification.check_runs),
                    failure=dict(certification.failure),
                    detail=certification.detail,
                    evaluated_at=certification.evaluated_at,
                    change_class=certification.change_class,
                )
                .on_conflict_do_nothing(constraint="uq_ci_certifications_head")
                .returning(CICertificationRow.id)
            ).scalar_one_or_none()
            self._s.flush()
            if inserted is not None:
                return certification
            row = self._s.scalars(
                select(CICertificationRow).where(
                    CICertificationRow.pull_request_id == certification.pull_request_id,
                    CICertificationRow.head_sha == certification.head_sha,
                )
            ).one()
        row.state = certification.state
        row.required_checks = list(certification.required_checks)
        row.check_runs = list(certification.check_runs)
        row.failure = dict(certification.failure)
        row.detail = certification.detail
        row.evaluated_at = certification.evaluated_at
        row.change_class = certification.change_class
        self._s.flush()
        return self._to_entity(row)

    def get_for_head(self, pull_request_id: str, head_sha: str) -> CICertification | None:
        row = self._s.scalars(
            select(CICertificationRow).where(
                CICertificationRow.pull_request_id == pull_request_id,
                CICertificationRow.head_sha == head_sha,
            )
        ).one_or_none()
        return self._to_entity(row) if row else None

    def list_for_task(self, task_id: str) -> Sequence[CICertification]:
        rows = self._s.scalars(
            select(CICertificationRow)
            .where(CICertificationRow.task_id == task_id)
            .order_by(CICertificationRow.evaluated_at, CICertificationRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class CIDecisions:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: CIDecisionRow) -> CIDecision:
        return CIDecision(
            id=row.id,
            task_id=row.task_id,
            ci_certification_id=row.ci_certification_id,
            principal_id=row.principal_id,
            cause=row.cause,
            action=row.action,
            reasoning=row.reasoning,
            created_at=ensure_utc(row.created_at),
        )

    def add(self, decision: CIDecision) -> None:
        self._s.add(
            CIDecisionRow(
                id=decision.id,
                task_id=decision.task_id,
                ci_certification_id=decision.ci_certification_id,
                principal_id=decision.principal_id,
                cause=decision.cause,
                action=decision.action,
                reasoning=decision.reasoning,
                created_at=decision.created_at,
            )
        )
        self._s.flush()

    def list_for_task(self, task_id: str) -> Sequence[CIDecision]:
        rows = self._s.scalars(
            select(CIDecisionRow)
            .where(CIDecisionRow.task_id == task_id)
            .order_by(CIDecisionRow.created_at, CIDecisionRow.id)
        ).all()
        return [self._to_entity(r) for r in rows]


class GitHubDeliveries:
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _to_entity(row: GitHubDeliveryRow) -> GitHubDelivery:
        return GitHubDelivery(
            delivery_id=row.delivery_id,
            event=row.event,
            action=row.action,
            repository=row.repository,
            body_sha256=row.body_sha256,
            normalized=dict(row.normalized),
            received_at=ensure_utc(row.received_at),
            processed_at=_dt(row.processed_at),
        )

    def add(self, delivery: GitHubDelivery) -> bool:
        """Insert, or report that this delivery id was already stored (04: deduplicated
        by delivery id). GitHub re-delivers, and a redelivery must change nothing."""
        inserted = self._s.execute(
            pg_insert(GitHubDeliveryRow)
            .values(
                delivery_id=delivery.delivery_id,
                event=delivery.event,
                action=delivery.action,
                repository=delivery.repository,
                received_at=delivery.received_at,
                body_sha256=delivery.body_sha256,
                normalized=delivery.normalized,
                processed_at=delivery.processed_at,
            )
            .on_conflict_do_nothing(index_elements=["delivery_id"])
            .returning(GitHubDeliveryRow.delivery_id)
        ).scalar_one_or_none()
        self._s.flush()
        return inserted is not None

    def get(self, delivery_id: str) -> GitHubDelivery | None:
        row = self._s.get(GitHubDeliveryRow, delivery_id)
        return self._to_entity(row) if row else None

    def list_unprocessed(self, limit: int = 100) -> Sequence[GitHubDelivery]:
        rows = self._s.scalars(
            select(GitHubDeliveryRow)
            .where(GitHubDeliveryRow.processed_at.is_(None))
            .order_by(GitHubDeliveryRow.received_at)
            .limit(limit)
        ).all()
        return [self._to_entity(r) for r in rows]

    def count_unprocessed(self) -> int:
        return int(
            self._s.scalar(
                select(func.count())
                .select_from(GitHubDeliveryRow)
                .where(GitHubDeliveryRow.processed_at.is_(None))
            )
            or 0
        )

    def mark_processed(self, delivery_id: str, at: datetime) -> None:
        row = self._s.get(GitHubDeliveryRow, delivery_id)
        if row is not None:
            row.processed_at = at
            self._s.flush()
