"""PR observation: what Crucible does with one poll (23, 09).

Polling is the complete observation path; webhooks only shorten latency, so a delivery is
turned into the same normalized records and then the same functions here run. That is why
this module takes an `Observation` snapshot rather than a client: the poll-only path and
the webhook-accelerated path are the same code, and an integration test asserts they end
in the same rows.

Everything here runs inside one fenced transaction. Nothing calls GitHub; the supervisor
does the I/O and hands the snapshot over.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from crucible.application.auto_merge import auto_merge_enabled, certified_jobs_green
from crucible.application.decisions import open_escalation
from crucible.application.publish import (
    external_review_trigger,
    open_review_cycle,
    reopen_or_cancel,
)
from crucible.application.transitions import move_task, record_event
from crucible.application.wakes import create_wake, repeat_allowed
from crucible.contracts.task_contract import contract_sha256
from crucible.contracts.wake import WakeReason
from crucible.domain.certification import (
    CertificationState,
    CheckSource,
    ObservedCheck,
    certify,
    required_checks_from_policy,
    wait_timeout_hours,
)
from crucible.domain.entities import (
    CIAction,
    CICertification,
    Decision,
    DispositionKind,
    ExternalReview,
    ExternalReviewCycle,
    GateResultRecord,
    PullRequest,
    PullRequestHead,
    PullRequestState,
    PushedBy,
    Reaction,
    ReviewComment,
    Task,
    TaskContract,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.external_review import (
    CLEAN_REACTION,
    Cycle,
    CycleState,
    Signal,
    SignalKind,
    apply_signal,
    completed_rounds,
    final_sha_satisfied,
    head_at,
    is_accepted,
    required_rounds,
    reviewer_logins,
)
from crucible.domain.gates import (
    POST_PR_GATES,
    PUBLICATION_GATES,
    DeliveryInput,
    GateName,
    GateOutcome,
    GateResult,
    configured,
    evaluate_delivery,
)
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import CORRECTION_STATES, TaskState
from crucible.domain.waivers import (
    ACCEPT_NO_CI,
    WAIVE_EXTERNAL_REVIEW,
    latest_waivers,
    waiver_words,
)
from crucible.ports.clock import Clock
from crucible.ports.github import Observation
from crucible.ports.repository import UnitOfWork

log = logging.getLogger("crucible.observation")

PHASE_PUBLICATION = "publication"
PHASE_POST_PR = "post_pr"

# The task states in which a pull request is observed (23).
OBSERVED_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.AWAITING_EXTERNAL_REVIEW,
        TaskState.EXTERNAL_FEEDBACK_RECEIVED,
        TaskState.AWAITING_CI_CERTIFICATION,
        TaskState.CI_CERTIFICATION_FAILED,
        TaskState.READY_FOR_MERGE,
        TaskState.HEAD_DIVERGED,
    }
)
# hades #360: the PR of a task in any of these is polled, a correction's included, so a
# merge made while the correction runs is seen. Only an open PR is polled at all.
POLLED_STATES: frozenset[TaskState] = OBSERVED_STATES | CORRECTION_STATES
# hades #379: the correction states a first publication passes through too. In these a
# correction is under way only when an earlier publication of the task completed.
PUBLICATION_STATES: frozenset[TaskState] = frozenset(
    {TaskState.PUBLISHING, TaskState.PUBLISH_FAILED}
)
# The states a head change moves to `head_diverged` from (09).
DIVERGENCE_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.AWAITING_EXTERNAL_REVIEW,
        TaskState.EXTERNAL_FEEDBACK_RECEIVED,
        TaskState.AWAITING_CI_CERTIFICATION,
        TaskState.READY_FOR_MERGE,
    }
)
DEFAULT_EXTERNAL_TIMEOUT_HOURS = 24
DEFAULT_CI_TIMEOUT_HOURS = 6


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ObservationResult:
    changed: bool = False
    diverged: bool = False
    accepted_signals: int = 0
    new_comments: int = 0
    edited_feedback: int = 0
    completed_cycles: int = 0
    certification: str = ""
    state: str = ""
    notes: list[str] = field(default_factory=list)
    review_refusal: bool = False


CODEX_ACCOUNT_REFUSAL = "to use codex here, create a codex account"


def policy_for(uow: UnitOfWork, task: Task) -> dict[str, Any]:
    stored = uow.policies.get(task.policy_name, task.policy_version)
    return stored.document if stored else {}


def correction_in_flight(uow: UnitOfWork, task: Task) -> bool:
    """A correction is under way against the task's pull request (hades #360, #379).

    Every correction state but publishing and publish_failed is reached with a pull
    request only by a correction. Those two are also a first publication's, which can
    fail after it opened the pull request; that one is polled and settled as any
    published task is, and only a task an earlier publication completed for is
    correcting there."""
    if task.state not in CORRECTION_STATES:
        return False
    if task.state not in PUBLICATION_STATES:
        return True
    return uow.events.latest_for_task_kind(task.id, EventKind.PUBLISH_COMPLETED.value) is not None


def accepted_head(uow: UnitOfWork, task: Task) -> str:
    """The head every state after `publishing` is bound to (09)."""
    return task.head_sha or ""


@dataclass(frozen=True, slots=True)
class ReplacedHeads:
    """Heads Crucible pushed to this pull request that the accepted head replaced, and
    when the accepted head first appeared on it.

    hades FDY-0139: feedback on a replaced head is settled. A `fix` disposition is
    Foundry saying the work is not done; the correction that follows is the answer, and
    once its head is the accepted one, the comments made on the head it replaced ask
    nothing more. Without this a `fix` held the task for ever, because dispositions are
    add-only and the comments stay on the pull request. Only a comment made before the
    accepted head appeared is settled: a reply on an old thread after the correction is
    new feedback, whatever head its thread started on. A head someone else pushed is
    normally never in the set: it went through `head_diverged` and was never Crucible's
    to settle. A recorded `recollect` decision is the exception: it explicitly adopts
    that head, so its observation time starts the replacement."""

    heads: frozenset[str] = frozenset()
    since: datetime | None = None

    def settles(self, reviewed_sha: str | None, created_at: datetime) -> bool:
        return (
            self.since is not None
            and bool(reviewed_sha)
            and reviewed_sha in self.heads
            and created_at < self.since
        )


def _last_written(comment: ReviewComment) -> datetime:
    """When the comment's text was last written: its creation, or a later edit."""
    if comment.updated_at is not None and comment.updated_at > comment.created_at:
        return comment.updated_at
    return comment.created_at


def replaced_heads(uow: UnitOfWork, task: Task, pull_request: PullRequest) -> ReplacedHeads:
    head = accepted_head(uow, task)
    if not head:
        return ReplacedHeads()
    all_rows = list(uow.pull_request_heads.list_for_pull_request(pull_request.id))
    rows = [row for row in all_rows if row.pushed_by is PushedBy.CRUCIBLE]
    since = min((row.observed_at for row in rows if row.sha == head), default=None)
    head_decision = uow.events.latest_for_task_kind(task.id, EventKind.HEAD_DECISION_RECORDED.value)
    if (
        since is None
        and head_decision is not None
        and head_decision.payload.get("action") in ("recollect", "adopt")
        and head_decision.payload.get("observed_head") == head
    ):
        since = min(
            (row.observed_at for row in all_rows if row.sha == head),
            default=None,
        )
    return ReplacedHeads(
        heads=frozenset(row.sha for row in rows if row.sha != head),
        since=since,
    )


def task_waivers(uow: UnitOfWork, task: Task) -> dict[str, Decision]:
    """The operator's waiver decisions on this task, newest of each kind (ADR 0025)."""
    return latest_waivers(uow.decisions.list_for_task(task.id))


# ----- heads and divergence ---------------------------------------------


def observe_head(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observed_sha: str,
    result: ObservationResult,
) -> None:
    """A head Crucible did not push moves the task to `head_diverged` (09, 23)."""
    if not observed_sha or observed_sha == pull_request.head_sha:
        return
    known = {
        head.sha: head for head in uow.pull_request_heads.list_for_pull_request(pull_request.id)
    }
    now = clock.now()
    previous = pull_request.head_sha
    pull_request.head_sha = observed_sha
    uow.pull_requests.save(pull_request)
    ours = observed_sha in known and known[observed_sha].pushed_by is PushedBy.CRUCIBLE
    if not ours:
        uow.pull_request_heads.add(
            PullRequestHead(
                id=new_id(),
                pull_request_id=pull_request.id,
                sha=observed_sha,
                pushed_by=PushedBy.OTHER,
                observed_at=now,
            )
        )
    record_event(
        uow,
        clock,
        EventKind.PULL_REQUEST_HEAD_OBSERVED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        payload={
            "pull_request": pull_request.number,
            "from": previous,
            "to": observed_sha,
            "pushed_by": PushedBy.CRUCIBLE.value if ours else PushedBy.OTHER.value,
        },
    )
    result.changed = True
    if ours or task.state not in DIVERGENCE_STATES:
        return
    supersede_for_head(uow, clock, task=task, reason="head_diverged", new_head=observed_sha)
    move_task(
        uow,
        clock,
        task,
        TaskState.HEAD_DIVERGED,
        EventKind.TASK_HEAD_DIVERGED,
        payload={
            "pull_request": pull_request.number,
            "accepted_head": previous,
            "observed_head": observed_sha,
            "note": (
                "nothing about the new SHA is trusted; CI on it is recorded and cannot "
                "move the task (23)"
            ),
        },
    )
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=WakeReason.HEAD_DIVERGED,
        summary=(
            f"pull request #{pull_request.number} moved from {previous} to {observed_sha} "
            "out of band; the previous head's acceptance, review, and gates are superseded"
        ),
        task=task,
        extra_links={"head_decision": f"/v1/tasks/{task.id}/head-decision"},
    )
    result.diverged = True
    result.changed = True


def supersede_for_head(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    reason: str,
    new_head: str,
    principal: str = PRINCIPAL_CRUCIBLE,
) -> None:
    """09: the previous head's acceptance, review report, and gate results are marked
    superseded and kept as history. Nothing is deleted; a superseded row is the record
    that this head was once believed."""
    now = clock.now()
    uow.acceptance.supersede_for_task(task.id, now)
    superseded_reports = 0
    for report in uow.review_reports.list_for_task(task.id):
        if report.superseded_at is None and report.head_sha == (task.head_sha or ""):
            uow.review_reports.supersede(report.id, now)
            superseded_reports += 1
    record_event(
        uow,
        clock,
        EventKind.SUPERSEDED_FOR_HEAD,
        principal=principal,
        task_id=task.id,
        payload={
            "reason": reason,
            "superseded_head": task.head_sha,
            "new_head": new_head,
            "review_reports": superseded_reports,
        },
    )


# ----- reactions ---------------------------------------------------------


def record_reactions(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    result: ObservationResult,
) -> list[Signal]:
    """Store what a reaction poll saw, and note what disappeared.

    Polling cannot tell a deleted reaction from one that never existed, and it cannot see
    one created and deleted between two cycles (S12). A reaction that was recorded and is
    now absent is marked removed, with no actor and no delete time, because neither is
    knowable."""
    now = clock.now()
    if not observation.reactions_observable:
        if pull_request.reactions_observable:
            pull_request.reactions_observable = False
            uow.pull_requests.save(pull_request)
            record_event(
                uow,
                clock,
                EventKind.REACTIONS_UNOBSERVABLE,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                payload={
                    "pull_request": pull_request.number,
                    "detail": observation.reactions_detail,
                    "note": (
                        "the App lacks Issues read, which is the one permission the "
                        "PR-level reactions endpoint needs; a clean external review is "
                        "signalled only there (23, S12). Recorded, not fatal."
                    ),
                },
            )
            result.notes.append("reactions unobservable")
            result.changed = True
        return []
    if not pull_request.reactions_observable:
        pull_request.reactions_observable = True
        uow.pull_requests.save(pull_request)
        result.changed = True
    stored = {
        (r.subject_kind, r.subject_github_id, r.github_id): r
        for r in uow.reactions.list_for_pull_request(pull_request.id)
    }
    seen: set[tuple[str, str, str]] = set()
    signals: list[Signal] = []
    for observed in observation.reactions:
        key = (observed.subject_kind, observed.subject_github_id, observed.github_id)
        seen.add(key)
        existing = stored.get(key)
        if existing is not None:
            if existing.removed_at is not None:
                existing.removed_at = None
                uow.reactions.save(existing)
                result.changed = True
            continue
        if not uow.reactions.add(
            Reaction(
                id=new_id(),
                pull_request_id=pull_request.id,
                subject_kind=observed.subject_kind,
                subject_github_id=observed.subject_github_id,
                github_id=observed.github_id,
                login=observed.login,
                content=observed.content,
                created_at=observed.created_at,
                observed_at=now,
            )
        ):
            # Already recorded by an earlier tick this one did not see. Nothing new
            # happened, so nothing is recorded and nothing becomes a signal.
            continue
        record_event(
            uow,
            clock,
            EventKind.REACTION_RECEIVED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "subject_kind": observed.subject_kind,
                "subject_github_id": observed.subject_github_id,
                "login": observed.login,
                "content": observed.content,
                "created_at": observed.created_at.isoformat(),
            },
        )
        result.changed = True
        if observed.subject_kind == "pull_request":
            signals.append(
                Signal(
                    kind=SignalKind.REACTION,
                    login=observed.login,
                    github_id=observed.github_id,
                    created_at=observed.created_at,
                    content=observed.content,
                )
            )
    for key, existing in stored.items():
        if key in seen or existing.removed_at is not None:
            continue
        existing.removed_at = now
        uow.reactions.save(existing)
        record_event(
            uow,
            clock,
            EventKind.REACTION_REMOVED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "subject_kind": existing.subject_kind,
                "content": existing.content,
                "login": existing.login,
                "note": (
                    "observed absent; polling cannot see the deletion itself, only that "
                    "the set shrank (S12)"
                ),
            },
        )
        result.changed = True
    return signals


# ----- reviews and comments ----------------------------------------------


def record_reviews(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    result: ObservationResult,
) -> list[Signal]:
    signals: list[Signal] = []
    for review in observation.reviews:
        if uow.external_reviews.get_by_github(pull_request.id, "review", review.github_id):
            continue
        signals.append(
            Signal(
                kind=SignalKind.REVIEW,
                login=review.login,
                github_id=review.github_id,
                created_at=review.submitted_at,
                body=review.body,
                reviewed_sha=review.commit_id,
                has_findings=review.state.upper() in ("CHANGES_REQUESTED", "COMMENTED"),
            )
        )
    return signals


def record_comments(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    allowlist: frozenset[str],
    result: ObservationResult,
    replaced: ReplacedHeads | None = None,
    policy: dict[str, Any] | None = None,
) -> list[Signal]:
    """Store review comments and issue comments, updating one edited in place.

    The reviewer's summary comment is edited rather than replaced (S12), so a changed
    body or `updated_at` is a change; the row keeps its identity and the edit never counts
    as a round. Only an inline review comment is feedback that needs a disposition, so
    only its edit counts as feedback: the summary is an issue comment, and its edit is
    recorded and asks nothing of Foundry (hades FDY-0139). A comment on a head a
    correction replaced is recorded and settled, so it does not count either."""
    signals: list[Signal] = []
    now = clock.now()
    settled = replaced or ReplacedHeads()
    request = uow.events.latest_for_task_kind(task.id, EventKind.EXTERNAL_REVIEW_REQUESTED.value)
    request_payload = request.payload if request is not None else {}
    request_id = str(request_payload.get("comment_id") or "")
    request_login = str(request_payload.get("comment_login") or "")
    trigger = external_review_trigger(policy or {})
    for comment in (*observation.review_comments, *observation.issue_comments):
        if comment.kind == "issue_comment" and (
            (request_id and comment.github_id == request_id)
            or (
                trigger is not None
                and request_login
                and comment.login == request_login
                and comment.body == trigger
            )
        ):
            continue
        connector_refusal = (
            comment.kind == "issue_comment"
            and comment.login in allowlist
            and CODEX_ACCOUNT_REFUSAL in comment.body.lower()
        )
        existing = uow.review_comments.get_by_github(
            pull_request.id, comment.kind, comment.github_id
        )
        digest = _sha(comment.body)
        if existing is not None:
            if existing.body_sha256 != digest or existing.updated_at < comment.updated_at:
                body_changed = existing.body_sha256 != digest
                disposition_invalidated = False
                if body_changed and comment.login in allowlist and comment.kind == "review_comment":
                    # The edit is judged by when it happened, not when the comment was
                    # first made: new text after the correction is new feedback.
                    edited_at = comment.updated_at or now
                    if not settled.settles(existing.reviewed_sha, edited_at):
                        result.edited_feedback += 1
                        result.new_comments += 1
                    old_disposition = uow.dispositions.get_by_comment(
                        existing.id, existing.body_sha256
                    )
                    if old_disposition is not None:
                        disposition_invalidated = True
                        record_event(
                            uow,
                            clock,
                            EventKind.DISPOSITION_INVALIDATED,
                            principal=PRINCIPAL_CRUCIBLE,
                            task_id=task.id,
                            payload={
                                "disposition_id": old_disposition.id,
                                "review_comment_id": existing.id,
                                "old_body_sha256": existing.body_sha256,
                                "new_body_sha256": digest,
                            },
                        )
                existing.body = comment.body
                existing.body_sha256 = digest
                existing.updated_at = comment.updated_at
                uow.review_comments.save(existing)
                record_event(
                    uow,
                    clock,
                    EventKind.REVIEW_COMMENT_RECEIVED
                    if comment.kind == "review_comment"
                    else EventKind.ISSUE_COMMENT_RECEIVED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    payload={
                        "pull_request": pull_request.number,
                        "comment_id": existing.id,
                        "github_id": comment.github_id,
                        "login": comment.login,
                        "edited": True,
                        "allowlisted": comment.login in allowlist,
                        "disposition_invalidated": disposition_invalidated,
                        "body_sha256": digest,
                        "note": "edited in place; an edit never counts as a round (23)",
                    },
                )
                result.changed = True
            continue
        row = ReviewComment(
            id=new_id(),
            pull_request_id=pull_request.id,
            external_review_id=None,
            github_id=comment.github_id,
            kind=comment.kind,
            login=comment.login,
            path=comment.path,
            line=comment.line,
            body=comment.body,
            body_sha256=digest,
            reviewed_sha=comment.commit_id,
            created_at=comment.created_at,
            updated_at=comment.updated_at or now,
        )
        if not uow.review_comments.add(row):
            continue
        record_event(
            uow,
            clock,
            EventKind.REVIEW_COMMENT_RECEIVED
            if comment.kind == "review_comment"
            else EventKind.ISSUE_COMMENT_RECEIVED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "comment_id": row.id,
                "github_id": comment.github_id,
                "login": comment.login,
                "path": comment.path,
                "line": comment.line,
                "body_sha256": digest,
                "allowlisted": comment.login in allowlist,
            },
        )
        result.changed = True
        if connector_refusal:
            result.review_refusal = True
            result.notes.append("the Codex connector refused the review because no account exists")
            continue
        if (
            comment.kind == "review_comment"
            and comment.login in allowlist
            and not settled.settles(comment.commit_id, comment.created_at)
        ):
            # Only what the dispositions gate counts: a comment from another login, a
            # standalone issue comment, or one on a head a correction replaced, is
            # recorded and asks nothing of Foundry.
            result.new_comments += 1
        # An inline review comment belongs to its review object, which carries the
        # round; only a standalone comment is a signal of its own.
        if comment.kind == "issue_comment":
            signals.append(
                Signal(
                    kind=SignalKind.COMMENT,
                    login=comment.login,
                    github_id=comment.github_id,
                    created_at=comment.created_at,
                    body=comment.body,
                    has_findings=True,
                )
            )
    return signals


def to_cycle(row: ExternalReviewCycle) -> Cycle:
    return Cycle(
        id=row.id,
        head_sha=row.head_sha,
        components=tuple(row.components),
        opened_at=row.opened_at,
        state=CycleState(row.state),
        completed_components=dict(row.completed_components),
        completed_at=row.completed_at,
    )


def attach_signals(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    signals: list[Signal],
    policy: dict[str, Any],
    result: ObservationResult,
) -> None:
    """Record each signal and attach the accepted ones to the open cycle (23).

    A signal from a login that is not allowlisted is recorded and satisfies nothing; that
    is the whole of ADR 0008's bound on who may move this task."""
    if not signals:
        return
    now = clock.now()
    heads = [
        (head.sha, head.observed_at)
        for head in uow.pull_request_heads.list_for_pull_request(pull_request.id)
    ]
    rows = list(uow.review_cycles.list_for_pull_request(pull_request.id))
    open_rows = [row for row in rows if row.state == CycleState.OPEN.value]
    for signal in sorted(signals, key=lambda s: s.created_at):
        accepted = is_accepted(signal, policy)
        reviewed_sha = signal.reviewed_sha
        inferred = False
        if reviewed_sha is None:
            reviewed_sha, inferred = head_at(
                heads, signal.created_at, fallback=pull_request.head_sha
            )
        review = ExternalReview(
            id=new_id(),
            pull_request_id=pull_request.id,
            cycle_id=None,
            reviewer_login=signal.login,
            signal=signal.kind.value,
            github_id=signal.github_id,
            reviewed_sha=reviewed_sha,
            body=signal.body,
            body_sha256=_sha(signal.body),
            received_at=now,
            state=signal.content or "",
            accepted=accepted,
            sha_inferred=inferred,
        )
        if not accepted:
            if not uow.external_reviews.add(review):
                continue
            record_event(
                uow,
                clock,
                EventKind.EXTERNAL_REVIEW_IGNORED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                payload={
                    "pull_request": pull_request.number,
                    "login": signal.login,
                    "signal": signal.kind.value,
                    "github_id": signal.github_id,
                    "reason": (
                        "the login is not in external_review.reviewer_logins, or the "
                        "signal shape is not accepted; recorded, satisfies nothing (23)"
                    ),
                },
            )
            result.changed = True
            continue
        target = _cycle_for(open_rows, reviewed_sha)
        if target is not None:
            review.cycle_id = target.id
        if not uow.external_reviews.add(review):
            continue
        record_event(
            uow,
            clock,
            EventKind.EXTERNAL_REVIEW_RECEIVED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "login": signal.login,
                "signal": signal.kind.value,
                "github_id": signal.github_id,
                "reviewed_sha": reviewed_sha,
                "sha_inferred": inferred,
                "cycle_id": review.cycle_id,
                "content": signal.content,
            },
        )
        result.accepted_signals += 1
        result.changed = True
        if target is None:
            continue
        domain_cycle = to_cycle(target)
        domain_cycle, completed, just_done = apply_signal(domain_cycle, signal, at=now)
        target.completed_components = dict(domain_cycle.completed_components)
        target.state = domain_cycle.state.value
        target.completed_at = domain_cycle.completed_at
        uow.review_cycles.save(target)
        if just_done:
            result.completed_cycles += 1
            record_event(
                uow,
                clock,
                EventKind.EXTERNAL_REVIEW_CYCLE_COMPLETED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                payload={
                    "cycle_id": target.id,
                    "head_sha": target.head_sha,
                    "components": list(target.components),
                    "completed_by": list(completed),
                    "clean": signal.is_clean_reaction,
                },
            )
            open_rows = [row for row in open_rows if row.id != target.id]


def _cycle_for(rows: list[ExternalReviewCycle], head_sha: str) -> ExternalReviewCycle | None:
    """The open cycle a signal attaches to: the one on its head, else the oldest open."""
    for row in rows:
        if row.head_sha == head_sha:
            return row
    return rows[0] if rows else None


# ----- CI certification ---------------------------------------------------


def _source(name: str) -> CheckSource:
    try:
        return CheckSource(name)
    except ValueError:
        return CheckSource.CHECK_RUN


def observed_checks(observation: Observation) -> tuple[ObservedCheck, ...]:
    return tuple(
        ObservedCheck(
            name=check.name,
            status=check.status,
            conclusion=check.conclusion,
            head_sha=check.head_sha,
            source=_source(check.source),
            url=check.url,
            external_id=check.external_id,
            workflow=check.workflow,
            job=check.job,
            completed_at=check.completed_at,
        )
        for check in observation.checks
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# A failed run as a re-run decision names it: GitHub's id and when it concluded. A
# re-run of a job is a new id; a re-run of a workflow keeps the id and concludes later.
FailedRun = tuple[str, str | None]


def pending_rerun(
    uow: UnitOfWork, task: Task, *, head_sha: str, failures: list[FailedRun]
) -> str | None:
    """The id of the re-run decision these failures are stale under, or None.

    hades FDY-0139: a `rerun` decision is about the failures it was recorded for, which
    its event lists. While those are still what GitHub shows, they are not counted
    again: otherwise the task went straight back to `ci_certification_failed` on the
    next poll, before anyone could re-run anything. Any other failure is a new one. A
    decision whose event lists no failures is judged by time: a failure that concluded
    before it is the old one."""
    event = uow.events.latest_for_task_kind(task.id, EventKind.CI_DECISION_RECORDED.value)
    if event is None:
        return None
    payload = event.payload
    if payload.get("action") != CIAction.RERUN.value or payload.get("head_sha") != head_sha:
        return None
    decision_id = str(payload.get("ci_decision_id", ""))
    listed = payload.get("stale_failures")
    # An entry with no run id (a failure recorded before FDY-0139) identifies nothing, so
    # a decision that lists only those is judged by time instead.
    known = {
        (str(item["run_id"]), item.get("completed_at"))
        for item in (listed if isinstance(listed, list) else [])
        if isinstance(item, dict) and item.get("run_id")
    }
    if known:
        return decision_id if all(run in known for run in failures) else None
    concluded = [_parse_iso(at) for _, at in failures]
    if all(at is None or at <= event.ts for at in concluded):
        return decision_id
    return None


def certification_failures(certification: CICertification) -> list[FailedRun]:
    """The failed runs a stored certification recorded."""
    rows = certification.failure.get("all")
    if not isinstance(rows, list):
        return [(str(certification.failure.get("run_id", "")), None)]
    return [
        (str(row.get("run_id") or ""), row.get("completed_at"))
        for row in rows
        if isinstance(row, dict)
    ]


def certify_head(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    policy: dict[str, Any],
    head_sha: str,
    log_excerpt: str = "",
    log_fetched: bool = False,
) -> CICertification:
    """Compute and record the certification for the accepted head (23, ADR 0009).

    Two recorded decisions shape it (hades FDY-0139): an `accept_no_ci` waiver turns a
    head with no policy narrowing and no non-skipped runs into `skipped`, and a
    `rerun` decision keeps the failure it was about from being counted again until a
    fresh result arrives."""
    checks = observed_checks(observation)
    outcome = certify(
        policy,
        head_sha=head_sha,
        observed=checks,
    )
    state = outcome.state
    detail = outcome.detail
    previous = uow.ci_certifications.get_for_head(pull_request.id, head_sha)
    failure: dict[str, Any] = {}
    if state is CertificationState.FAILED:
        first = outcome.failures[0]
        failure = {
            "check": first.name,
            "workflow": first.workflow,
            "job": first.job,
            "conclusion": first.conclusion,
            "head_sha": first.head_sha,
            "url": first.url,
            "run_id": first.external_id,
            "source": first.source.value,
            "all": [
                {
                    "check": c.name,
                    "conclusion": c.conclusion,
                    "url": c.url,
                    "run_id": c.external_id,
                    "completed_at": _iso(c.completed_at),
                }
                for c in outcome.failures
            ],
        }
        # The excerpt is fetched once per failed run, not on every poll, even when it
        # came back empty; the stored one is kept while the same run is the failure.
        same_run = previous is not None and previous.failure.get("run_id") == first.external_id
        if not log_fetched and previous is not None and same_run:
            log_excerpt = str(previous.failure.get("log_excerpt", ""))
            log_fetched = bool(previous.failure.get("log_fetched"))
        if log_excerpt:
            failure["log_excerpt"] = log_excerpt
        if log_fetched:
            failure["log_fetched"] = True
        rerun = pending_rerun(
            uow,
            task,
            head_sha=head_sha,
            failures=[(c.external_id, _iso(c.completed_at)) for c in outcome.failures],
        )
        if rerun is not None:
            state = CertificationState.PENDING
            names = ", ".join(sorted({c.name for c in outcome.failures}))
            detail = (
                f"a CI re-run was decided (ci decision {rerun}); the failure it was about "
                f"({names}) is not counted again. Waiting for a result on {head_sha} that "
                "is not the one the decision was about"
            )
            failure["stale_after_rerun"] = rerun
    elif state is CertificationState.PENDING:
        waiver = task_waivers(uow, task).get(ACCEPT_NO_CI)
        # Nothing ran: no check run or workflow run on the head, a suite being only a
        # container and a run a path filter skipped being no run at all.
        ran = [
            c
            for c in checks
            if c.head_sha == head_sha
            and c.source is not CheckSource.CHECK_SUITE
            and not (c.concluded and c.conclusion == "skipped")
        ]
        # A policy narrowing expects CI even before any matching run appears.
        # Excluded observed runs also cannot be waived as absent CI.
        if waiver is not None and not ran and not required_checks_from_policy(policy):
            state = CertificationState.SKIPPED
            detail = (
                f"the operator accepted that this repository has no CI for this task "
                f"({waiver_words(waiver)})"
            )
    certification = CICertification(
        id=new_id(),
        pull_request_id=pull_request.id,
        task_id=task.id,
        head_sha=head_sha,
        state=state.value,
        required_checks=list(outcome.required),
        check_runs=[
            {
                "name": c.name,
                "status": c.status,
                "conclusion": c.conclusion,
                "source": c.source.value,
                "url": c.url,
                "workflow": c.workflow,
                "completed_at": _iso(c.completed_at),
            }
            for c in outcome.observed
        ],
        failure=failure,
        detail=detail,
        evaluated_at=clock.now(),
    )
    stored = uow.ci_certifications.put(certification)
    if previous is None or previous.state != stored.state or previous.detail != stored.detail:
        record_event(
            uow,
            clock,
            EventKind.CI_CERTIFICATION_RECORDED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "certification_id": stored.id,
                "head_sha": head_sha,
                "state": stored.state,
                "detail": stored.detail,
                "required_checks": stored.required_checks,
                "required_from": outcome.source,
                "failure": {k: v for k, v in failure.items() if k != "log_excerpt"},
            },
        )
    return stored


# ----- merge, close, and advancement -------------------------------------


def observe_state(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    result: ObservationResult,
) -> None:
    """Record a merge by Hades or a person, or a pull request closed unmerged (23)."""
    ref = observation.pull_request
    now = clock.now()
    if ref.merged and pull_request.state is not PullRequestState.MERGED:
        pull_request.state = PullRequestState.MERGED
        pull_request.merged_at = ref.merged_at or now
        pull_request.merge_sha = ref.merge_commit_sha
        pull_request.merged_by = ref.merged_by
        uow.pull_requests.save(pull_request)
        record_event(
            uow,
            clock,
            EventKind.PULL_REQUEST_STATE_CHANGED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "state": "merged",
                "merge_sha": pull_request.merge_sha,
                "merged_by": pull_request.merged_by,
                # hades #379: the head GitHub merged, compared with the last head
                # Crucible pushed when a correction was under way.
                "head_sha": ref.head_sha,
            },
        )
        result.changed = True
        settle_pull_request_state(
            uow, clock, task=task, pull_request=pull_request, merged_head=ref.head_sha
        )
        return
    if ref.state == "closed" and not ref.merged and pull_request.state is PullRequestState.OPEN:
        pull_request.state = PullRequestState.CLOSED
        pull_request.closed_at = ref.closed_at or now
        pull_request.closed_by = ref.closed_by
        uow.pull_requests.save(pull_request)
        record_event(
            uow,
            clock,
            EventKind.PULL_REQUEST_STATE_CHANGED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "state": "closed",
                "closed_by": pull_request.closed_by,
            },
        )
        result.changed = True
        settle_pull_request_state(uow, clock, task=task, pull_request=pull_request)


def pushed_heads(
    uow: UnitOfWork, task: Task, pull_request: PullRequest
) -> tuple[dict[str, bool], str | None]:
    """Heads Crucible confirmed on the work branch and the latest recorded one.

    The heads recorded on the PR as pushed by Crucible, which include the PR's first
    head from before branch_pushed records, and every branch_pushed head. Never the PR
    row's current head: a poll sets that to a head someone else pushed (hades #379).
    The bool records whether a head was only pushed as a quota checkpoint.
    """
    heads: dict[str, bool] = {}
    latest: str | None = None
    for row in uow.pull_request_heads.list_for_pull_request(pull_request.id):
        if row.pushed_by is PushedBy.CRUCIBLE and row.sha:
            heads[row.sha] = False
            latest = row.sha
    after = 0
    while True:
        events = uow.events.list_for_task(task.id, after_seq=after, limit=1000)
        if not events:
            break
        for event in events:
            if event.kind != EventKind.BRANCH_PUSHED.value:
                continue
            head = str(event.payload.get("head_sha") or "")
            if head:
                heads[head] = bool(event.payload.get("checkpoint"))
                latest = head
        after = int(events[-1].seq or after)
    return heads, latest


def recorded_merged_head(uow: UnitOfWork, task: Task) -> str | None:
    """The head GitHub reported merged, as the merge was recorded (hades #379)."""
    changed = uow.events.latest_for_task_kind(task.id, EventKind.PULL_REQUEST_STATE_CHANGED.value)
    if changed is None or changed.payload.get("state") != "merged":
        return None
    return str(changed.payload.get("head_sha") or "") or None


def settle_pull_request_state(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    merged_head: str | None = None,
) -> bool:
    """Move a task whose pull request is merged or closed, and wake Foundry (23).

    hades FDY-0139: from any delivery state, not only `ready_for_merge`. A person can
    merge or close at any point after the PR opens, and a merged or closed PR is never
    polled again, so a task left behind here waited for ever. Also called on every tick
    for a task already stranded that way. True when the task moved.

    hades #360: a merge also moves a task whose correction is under way against the
    PR. A close does not: the correction publishes to the PR again, and what to do with
    a closed one is decided there.

    hades #379: when a correction was under way, the head GitHub merged (`merged_head`,
    or the one recorded with the merge) is compared with every head Crucible pushed and
    confirmed on the remote. When it is one of them, it becomes the task's head; when it
    is not, the task keeps the last head Crucible pushed, never a collected head that went
    nowhere. The wake says which; an escalation is opened instead of the plain wake when
    the merged head is not a pushed head, when it was only a quota checkpoint, or when
    Crucible pushed a later head after it (a push that landed after the merge). A merge
    recorded before #379 carries no head; the last pushed head is taken for it. A merge
    seen while a first publication is publishing or failed moves the task as an early
    merge does. A close seen in a correction state is woken about and leaves the task
    where it is, except in publishing, where the publication reports it."""
    correcting = correction_in_flight(uow, task)
    if task.state in CORRECTION_STATES and pull_request.state is PullRequestState.CLOSED:
        _wake_closed_during_correction(uow, clock, task=task, pull_request=pull_request)
        return False
    if task.state not in OBSERVED_STATES and not (
        task.state in CORRECTION_STATES and pull_request.state is PullRequestState.MERGED
    ):
        return False
    now = clock.now()
    if pull_request.state is PullRequestState.MERGED:
        merged_from = task.state.value
        early = task.state is not TaskState.READY_FOR_MERGE
        payload: dict[str, Any] = {
            "pull_request": pull_request.number,
            "merge_sha": pull_request.merge_sha,
            "merged_by": pull_request.merged_by,
            "merged_at": (pull_request.merged_at or now).isoformat(),
            "merged_from": merged_from,
        }
        head_check = ""
        head_matches = True
        checkpoint = False
        later_push: str | None = None
        if correcting:
            heads, pushed = pushed_heads(uow, task, pull_request)
            merged = merged_head or recorded_merged_head(uow, task)
            # A merge recorded before #379 carries no head: GitHub merged what was on
            # the branch then, and that is the last head Crucible pushed.
            head_matches = merged is None or merged in heads
            matched_head = merged or pushed
            checkpoint = bool(matched_head and heads.get(matched_head, False))
            if head_matches and merged is not None and pushed and pushed != merged:
                # Crucible pushed `pushed` after the head GitHub merged: the push landed
                # on the branch after the merge, whichever record of it came first.
                later_push = pushed
            payload.update(
                {
                    "merged_head": merged,
                    "merged_head_recorded": merged is not None,
                    "last_pushed_head": pushed,
                    "last_push_was_checkpoint": checkpoint,
                    "merged_head_matches": head_matches,
                    "pushed_after_merge": later_push,
                }
            )
            # The merged task's head is a head Crucible pushed, not a corrected head that
            # was collected and never pushed, nor a head someone else merged.
            if head_matches:
                task.head_sha = matched_head
            elif pushed:
                task.head_sha = pushed
            if merged is None:
                head_check = (
                    f"; the merge was recorded without its head, so the merged head is "
                    f"taken to be {pushed}, the last head Crucible pushed"
                )
            elif head_matches:
                head_check = f"; the merged head {merged} is a head Crucible pushed"
            else:
                head_check = f"; the merged head {merged} is not among the heads Crucible pushed"
            if head_matches and checkpoint:
                head_check += (
                    ", and that push was a quota checkpoint of an unfinished attempt, "
                    "which passed no gate and was never accepted"
                )
            if later_push is not None:
                head_check += (
                    f"; head {later_push} was pushed after the merge and is not the merged head"
                )
        move_task(uow, clock, task, TaskState.MERGED, EventKind.TASK_MERGED, payload=payload)
        summary = (
            f"pull request #{pull_request.number} was merged by "
            f"{pull_request.merged_by or 'someone'} as {pull_request.merge_sha}"
        )
        if correcting:
            summary += f" while a correction was under way (the task was {merged_from})"
            summary += head_check
        elif early:
            summary += f", before Crucible saw it ready for merge (the task was {merged_from})"
        if not head_matches or checkpoint or later_push is not None:
            if not head_matches:
                problem = "What was merged is not a head Crucible pushed"
                decide = "decide whether the merge stands"
            elif checkpoint:
                problem = "What was merged is an ungated quota checkpoint"
                decide = "decide whether the merge stands"
            else:
                # The same escalation as a push refused after a recorded merge.
                problem = (
                    f"Head {later_push} was pushed after the merge; it is not recorded as "
                    "the merged head"
                )
                decide = "decide whether the merge and branch state stand"
            open_escalation(
                uow,
                clock,
                task=task,
                attempt_id=None,
                question=f"{summary}. {problem}; {decide}."[:2000],
                wake_reason=WakeReason.MERGED,
                summary=summary[:500],
            )
            return True
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.MERGED,
            summary=summary,
            task=task,
            extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
        )
        return True
    if pull_request.state is PullRequestState.CLOSED:
        closed_from = task.state.value
        move_task(
            uow,
            clock,
            task,
            TaskState.REJECTED,
            EventKind.TASK_REJECTED,
            payload={
                "pull_request": pull_request.number,
                "reason": "the pull request was closed without being merged (23)",
                "closed_by": pull_request.closed_by,
                "closed_from": closed_from,
            },
        )
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.PULL_REQUEST_CLOSED,
            summary=(
                f"pull request #{pull_request.number} was closed without being merged by "
                f"{pull_request.closed_by or 'someone'} while the task was {closed_from}; "
                "the task is rejected"
            ),
            task=task,
            extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
        )
        return True
    return False


def _wake_closed_during_correction(
    uow: UnitOfWork, clock: Clock, *, task: Task, pull_request: PullRequest
) -> None:
    """hades #379: the pull request was closed unmerged while the task was in a
    correction state, or a first publication's publish_failed. The close is recorded on
    the PR, which is not polled again; the task has no edge to `rejected` from here, so
    Foundry is woken and decides. In publishing the publication finds the close itself
    and fails with a wake that names it."""
    if task.state is TaskState.PUBLISHING:
        return
    links = {"pull_request": f"/v1/tasks/{task.id}/pull-request"}
    if task.state is TaskState.PUBLISH_FAILED:
        # The wake says to reopen and then republish; it carries the way to do so.
        links["republish"] = f"/v1/tasks/{task.id}/republish"
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=WakeReason.PULL_REQUEST_CLOSED,
        summary=_closed_correction_summary(task, pull_request),
        task=task,
        extra_links=links,
    )


def _closed_correction_summary(task: Task, pull_request: PullRequest) -> str:
    start = (
        f"pull request #{pull_request.number} was closed without being merged by "
        f"{pull_request.closed_by or 'someone'} while the task was {task.state.value}; "
    )
    if task.state is TaskState.PUBLISH_FAILED:
        action = f"the task stays there; {reopen_or_cancel(pull_request.number)}"
    else:
        action = (
            "the correction continues, but publication will fail unless the pull request "
            "is reopened; cancelling the task is the usual answer"
        )
    return (start + action)[:500]


def evaluate_delivery_gates(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    attempt_id: str,
    pull_request: PullRequest | None,
    policy: dict[str, Any],
    certification: CICertification | None,
    branch_pushed_sha: str | None,
    phases: tuple[str, ...] = (PHASE_PUBLICATION, PHASE_POST_PR),
) -> dict[str, Any]:
    """Evaluate the publication and post-PR gates for the accepted head (09, 11, 23).

    Post-PR gates re-evaluate each reconcile tick until they resolve or the task is
    terminal, so this is written to be idempotent: the same input writes the same rows."""
    head = accepted_head(uow, task)
    comments = (
        list(uow.review_comments.list_for_pull_request(pull_request.id)) if pull_request else []
    )
    allowlist = reviewer_logins(policy)
    replaced = replaced_heads(uow, task, pull_request) if pull_request else ReplacedHeads()
    feedback = [c for c in comments if c.login in allowlist and c.kind == "review_comment"]
    # 09: advancement needs every comment dispositioned *and none of them fix*. A `fix`
    # is Foundry saying the work is not done; what follows it is a correction contract,
    # which clears it by replacing the head the comments belong to: a comment on a head
    # the accepted head replaced is settled and no longer counted (hades FDY-0139).
    # A comment edited after the correction is judged by its edit, so its new text needs a
    # disposition like any other fresh feedback.
    settled = [c for c in feedback if replaced.settles(c.reviewed_sha, _last_written(c))]
    needing = [c for c in feedback if not replaced.settles(c.reviewed_sha, _last_written(c))]
    recorded = list(
        uow.dispositions.list_for_comments(
            [c.id for c in needing], {c.id: c.body_sha256 for c in needing}
        )
    )
    dispositioned = {d.review_comment_id for d in recorded}
    fix_dispositions = tuple(
        d.review_comment_id for d in recorded if d.disposition is DispositionKind.FIX
    )
    cycles = (
        [to_cycle(row) for row in uow.review_cycles.list_for_pull_request(pull_request.id)]
        if pull_request
        else []
    )
    signals = (
        [
            Signal(
                kind=SignalKind(row.signal),
                login=row.reviewer_login,
                github_id=row.github_id,
                created_at=row.received_at,
                body=row.body,
                reviewed_sha=row.reviewed_sha,
                content=row.state,
            )
            for row in uow.external_reviews.list_for_pull_request(pull_request.id)
            if row.accepted
        ]
        if pull_request
        else []
    )
    waivers = task_waivers(uow, task)
    review_waiver = waivers.get(WAIVE_EXTERNAL_REVIEW)
    final_sha = None
    section = policy.get("external_review", {})
    if (
        review_waiver is None
        and isinstance(section, dict)
        and section.get("require_review_on_final_sha")
    ):
        final_sha = final_sha_satisfied(cycles, signals, head)
    di = DeliveryInput(
        policy=policy,
        accepted_head=head,
        branch_pushed_sha=branch_pushed_sha,
        pr_number=pull_request.number if pull_request else None,
        pr_head_sha=pull_request.head_sha if pull_request else None,
        pr_state=pull_request.state.value if pull_request else "",
        completed_rounds=completed_rounds(cycles),
        required_rounds=required_rounds(policy),
        undispositioned=tuple(c.id for c in needing if c.id not in dispositioned),
        fix_dispositions=fix_dispositions,
        comment_count=len(needing),
        certification_state=certification.state if certification else "",
        certification_detail=certification.detail if certification else "",
        final_sha=final_sha,
    )
    names: list[str] = []
    if PHASE_PUBLICATION in phases:
        names.extend(configured(policy, "publication", PUBLICATION_GATES))
    if PHASE_POST_PR in phases:
        names.extend(configured(policy, "post_pr", POST_PR_GATES))
    outcomes = evaluate_delivery(names, di)
    rounds_gate = GateName.EXTERNAL_REVIEW_ROUNDS.value
    rounds = outcomes.get(rounds_gate)
    if (
        review_waiver is not None
        and rounds is not None
        and rounds.result not in (GateResult.PASS, GateResult.SKIPPED)
    ):
        # ADR 0025: the operator waived the rounds still outstanding for this task. The
        # gate says so and names the decision; it does not pretend a round happened.
        outcomes[rounds_gate] = GateOutcome(
            GateResult.SKIPPED,
            f"{di.completed_rounds} of {di.required_rounds} round(s) completed; the rest "
            f"were waived by the operator ({waiver_words(review_waiver)})",
        )
    stored = {
        row.gate: (row.result, row.detail)
        for row in uow.gate_results.list_for_attempt(attempt_id)
        if row.head_sha == head
    }
    changed = False
    now = clock.now()
    for gate, outcome in outcomes.items():
        if stored.get(gate) == (outcome.result.value, outcome.detail):
            continue
        changed = True
        uow.gate_results.put(
            GateResultRecord(
                id=new_id(),
                task_id=task.id,
                attempt_id=attempt_id,
                head_sha=head,
                gate=gate,
                phase=PHASE_PUBLICATION if gate in PUBLICATION_GATES else PHASE_POST_PR,
                result=outcome.result.value,
                detail=outcome.detail,
                evidence_ids=list(outcome.evidence_ids),
                evaluated_at=now,
            )
        )
    summary = {
        "results": {gate: outcome.result.value for gate, outcome in outcomes.items()},
        "details": {gate: outcome.detail for gate, outcome in outcomes.items()},
        "completed_rounds": di.completed_rounds,
        "required_rounds": di.required_rounds,
        "undispositioned": list(di.undispositioned),
        "fix_dispositions": list(di.fix_dispositions),
        "settled_by_correction": [c.id for c in settled],
        "rounds_waived": review_waiver is not None,
    }
    if changed:
        record_event(
            uow,
            clock,
            EventKind.GATES_EVALUATED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            attempt_id=attempt_id,
            payload={"head_sha": head, "phase": "delivery", **summary},
        )
    return summary


def _schedule_findings_correction(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    policy: dict[str, Any],
) -> bool:
    """Build the first Codex findings correction directly from recorded facts.

    One automatic correction is allowed for a task's external-review path. A later
    review round remains an informational wake, which prevents an autonomous loop.
    """
    prior_auto = any(
        event.payload.get("automatic_codex_findings") is True
        for event in uow.events.list_for_task(task.id, after_seq=0, limit=10_000)
        if event.kind == EventKind.TASK_CORRECTION_ATTACHED.value
    )
    if prior_auto:
        return False
    allowlist = reviewer_logins(policy)
    findings = [
        comment
        for comment in uow.review_comments.list_for_pull_request(pull_request.id)
        if comment.kind == "review_comment"
        and comment.login in allowlist
        and comment.reviewed_sha == pull_request.head_sha
        and uow.dispositions.get_by_comment(comment.id, comment.body_sha256) is None
    ]
    if not findings:
        return False
    prior = uow.contracts.get(task.id, task.contract_version)
    if prior is None:
        return False
    document = copy.deepcopy(prior.document)
    rendered = "\n\n".join(
        f"Finding {finding.id}\nPath: {finding.path or ''}\nLine: "
        f"{finding.line if finding.line is not None else ''}\nBody:\n{finding.body}"
        for finding in findings
    )
    document["correction"] = {
        "of_version": task.contract_version,
        "reason": "external_review",
        "addresses": [{"kind": "review_comment", "id": finding.id} for finding in findings],
        "instructions": (
            "Fix each finding, or decline it in the report with the reason. Preserve "
            "each finding verbatim as supplied below. Run every required verification "
            "command, commit the result, and follow the standing report rules.\n\n" + rendered
        ),
        "resume_from": "remote_branch",
        "request_internal_review": False,
    }
    version = max((row.version for row in uow.contracts.list_for_task(task.id)), default=0) + 1
    stored = TaskContract(
        id=new_id(),
        task_id=task.id,
        version=version,
        document=document,
        sha256=contract_sha256(document),
        submitted_at=clock.now(),
    )
    uow.contracts.add(stored)
    task.contract_version = version
    task.head_sha = None
    uow.tasks.save(task)
    record_event(
        uow,
        clock,
        EventKind.TASK_CORRECTION_ATTACHED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        payload={
            "contract_version": version,
            "of_version": version - 1,
            "reason": "external_review",
            "automatic_codex_findings": True,
            "review_comment_ids": [finding.id for finding in findings],
            "review_head": pull_request.head_sha,
        },
    )
    move_task(
        uow,
        clock,
        task,
        TaskState.SCHEDULED,
        EventKind.TASK_SCHEDULED,
        payload={
            "role": "correct",
            "reason": "external_review",
            "contract_version": version,
            "resume_from_work_branch": True,
            "automatic_codex_findings": True,
        },
    )
    return True


def advance_delivery(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    policy: dict[str, Any],
    gates: dict[str, Any],
    certification: CICertification | None,
    result: ObservationResult,
) -> None:
    """Move the task on what the gates now say (09)."""
    results = gates.get("results", {})
    dispositions_ok = results.get("feedback_dispositions_complete") in (
        GateResult.PASS.value,
        GateResult.SKIPPED.value,
    ) or "feedback_dispositions_complete" in policy.get("gates", {}).get("skipped", [])
    feedback_from = task.state
    if result.review_refusal and task.state is TaskState.AWAITING_EXTERNAL_REVIEW:
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.EXTERNAL_FEEDBACK_RECEIVED,
            summary=(
                f"external review failed on #{pull_request.number}: the Codex connector "
                "refused the round because the repository has no Codex account"
            ),
            task=task,
            extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
        )
        return
    feedback_activity = bool(
        result.accepted_signals or result.new_comments or result.edited_feedback
    )
    if feedback_activity and task.state in (
        TaskState.AWAITING_EXTERNAL_REVIEW,
        TaskState.AWAITING_CI_CERTIFICATION,
        TaskState.READY_FOR_MERGE,
    ):
        # An accepted signal wins the race with CI. It is recorded before any green or
        # failed certification can advance the task, and ready_for_merge steps back for
        # a new signal on the same head.
        move_task(
            uow,
            clock,
            task,
            TaskState.EXTERNAL_FEEDBACK_RECEIVED,
            EventKind.TASK_EXTERNAL_FEEDBACK_RECEIVED,
            payload={
                "pull_request": pull_request.number,
                "accepted_signals": result.accepted_signals,
                "new_comments": result.new_comments,
                "edited_feedback": result.edited_feedback,
                "completed_rounds": gates.get("completed_rounds"),
                "feedback_from": feedback_from.value,
            },
        )
        summary = (
            f"{result.accepted_signals} external review signal(s) on "
            f"#{pull_request.number} at {pull_request.head_sha}: "
            f"{result.new_comments} comment(s), {result.edited_feedback} edited, "
            "0 dispositions recorded"
        )
        feedback_needs_wake = True
        if (
            result.new_comments == 0
            and dispositions_ok
            and feedback_from is not TaskState.READY_FOR_MERGE
        ):
            # 23: a round with no findings has nothing to disposition. Crucible records
            # it and advances without a wake for judgment; with rounds still outstanding
            # `advance_from_feedback` sends it back to wait for the next one.
            advance_from_feedback(
                uow, clock, task=task, pull_request=pull_request, gates=gates, policy=policy
            )
            if feedback_from is TaskState.AWAITING_EXTERNAL_REVIEW:
                return
            feedback_needs_wake = False
            # A clean signal received during CI returns to certification and may use the
            # certification from this same observation. A signal after ready_for_merge
            # always leaves the task stepped back for a later reconciliation.
        if feedback_needs_wake:
            automatically_corrected = False
            if (
                result.new_comments
                and feedback_from is TaskState.AWAITING_EXTERNAL_REVIEW
                and policy.get("external_review", {}).get("provider") == "codex"
            ):
                automatically_corrected = _schedule_findings_correction(
                    uow, clock, task=task, pull_request=pull_request, policy=policy
                )
            create_wake(
                uow,
                clock,
                principal_id=task.principal_id,
                reason=WakeReason.EXTERNAL_FEEDBACK_RECEIVED,
                summary=(summary + "; Crucible launched the correction")
                if automatically_corrected
                else summary,
                task=task,
                extra_links={
                    "pull_request": f"/v1/tasks/{task.id}/pull-request",
                    "dispositions": f"/v1/tasks/{task.id}/dispositions",
                },
            )
            return
    if task.state is TaskState.EXTERNAL_FEEDBACK_RECEIVED and dispositions_ok:
        advance_from_feedback(
            uow, clock, task=task, pull_request=pull_request, gates=gates, policy=policy
        )
        return
    if (
        task.state is TaskState.AWAITING_EXTERNAL_REVIEW
        and gates.get("rounds_waived")
        and dispositions_ok
    ):
        # ADR 0025: the operator waived the rounds still outstanding, so the task stops
        # waiting for the reviewer and goes on to certification in this same pass.
        move_task(
            uow,
            clock,
            task,
            TaskState.AWAITING_CI_CERTIFICATION,
            EventKind.TASK_AWAITING_CI_CERTIFICATION,
            payload={
                "pull_request": pull_request.number,
                "completed_rounds": gates.get("completed_rounds"),
                "required_rounds": gates.get("required_rounds"),
                "note": "the remaining external review rounds were waived by the operator",
            },
        )
    if (
        task.state is TaskState.CI_CERTIFICATION_FAILED
        and certification is not None
        and certification.head_sha == accepted_head(uow, task)
        and certification.state
        in (CertificationState.GREEN.value, CertificationState.SKIPPED.value)
    ):
        # hades FDY-0139: CI went green on the accepted head after the failure, whether
        # someone re-ran it before or after a decision. The failure no longer describes
        # the head, so the task goes back to certification and on from there.
        move_task(
            uow,
            clock,
            task,
            TaskState.AWAITING_CI_CERTIFICATION,
            EventKind.TASK_AWAITING_CI_CERTIFICATION,
            payload={
                "pull_request": pull_request.number,
                "certification_id": certification.id,
                "head_sha": certification.head_sha,
                "certification": certification.state,
                "note": "a later certification on the same head is green; the failure is past",
            },
        )
    if (
        task.state
        in (
            TaskState.AWAITING_CI_CERTIFICATION,
            TaskState.READY_FOR_MERGE,
        )
        and certification is not None
    ):
        if certification.state == CertificationState.FAILED.value:
            if pending_rerun(
                uow,
                task,
                head_sha=certification.head_sha,
                failures=certification_failures(certification),
            ):
                # The failure a re-run decision was about, not a new one; the next poll
                # records it as stale and waits for the re-run's result.
                return
            move_task(
                uow,
                clock,
                task,
                TaskState.CI_CERTIFICATION_FAILED,
                EventKind.TASK_CI_CERTIFICATION_FAILED,
                payload={
                    "pull_request": pull_request.number,
                    "certification_id": certification.id,
                    "head_sha": certification.head_sha,
                    "failure": {
                        k: v for k, v in certification.failure.items() if k != "log_excerpt"
                    },
                    "note": "no automatic retry, no automatic worker correction (ADR 0009)",
                },
            )
            create_wake(
                uow,
                clock,
                principal_id=task.principal_id,
                reason=WakeReason.CI_CERTIFICATION_FAILED,
                summary=(f"required CI failed on {certification.head_sha}: {certification.detail}"),
                task=task,
                extra_links={"ci_decision": f"/v1/tasks/{task.id}/ci-decision"},
            )
            return
        if task.state is TaskState.AWAITING_CI_CERTIFICATION and certification.state in (
            CertificationState.GREEN.value,
            CertificationState.SKIPPED.value,
        ):
            if not dispositions_ok:
                move_task(
                    uow,
                    clock,
                    task,
                    TaskState.EXTERNAL_FEEDBACK_RECEIVED,
                    EventKind.TASK_EXTERNAL_FEEDBACK_RECEIVED,
                    payload={
                        "pull_request": pull_request.number,
                        "accepted_signals": result.accepted_signals,
                        "new_comments": result.new_comments,
                        "edited_feedback": result.edited_feedback,
                        "completed_rounds": gates.get("completed_rounds"),
                        "reason": "feedback dispositions are incomplete",
                    },
                )
                create_wake(
                    uow,
                    clock,
                    principal_id=task.principal_id,
                    reason=WakeReason.EXTERNAL_FEEDBACK_RECEIVED,
                    summary=(
                        f"CI is {certification.state} on {certification.head_sha}, but "
                        "review feedback still needs disposition"
                    ),
                    task=task,
                    extra_links={
                        "pull_request": f"/v1/tasks/{task.id}/pull-request",
                        "dispositions": f"/v1/tasks/{task.id}/dispositions",
                    },
                )
                return
            move_task(
                uow,
                clock,
                task,
                TaskState.READY_FOR_MERGE,
                EventKind.TASK_READY_FOR_MERGE,
                payload={
                    "pull_request": pull_request.number,
                    "head_sha": certification.head_sha,
                    "certification": certification.state,
                    "detail": certification.detail,
                },
            )
            create_wake(
                uow,
                clock,
                principal_id=task.principal_id,
                reason=WakeReason.READY_FOR_MERGE,
                summary=ready_summary(uow, task, pull_request, certification, gates),
                task=task,
                extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
            )


def advance_from_feedback(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    gates: dict[str, Any],
    policy: dict[str, Any],
) -> None:
    """09: every comment dispositioned, none is `fix`, and the rounds decide where next.
    Rounds the operator waived (ADR 0025) count as decided."""
    completed = int(gates.get("completed_rounds", 0))
    needed = int(gates.get("required_rounds", 0))
    if completed >= needed or gates.get("rounds_waived"):
        move_task(
            uow,
            clock,
            task,
            TaskState.AWAITING_CI_CERTIFICATION,
            EventKind.TASK_AWAITING_CI_CERTIFICATION,
            payload={
                "pull_request": pull_request.number,
                "completed_rounds": completed,
                "required_rounds": needed,
            },
        )
        return
    move_task(
        uow,
        clock,
        task,
        TaskState.AWAITING_EXTERNAL_REVIEW,
        EventKind.TASK_AWAITING_EXTERNAL_REVIEW,
        payload={
            "pull_request": pull_request.number,
            "completed_rounds": completed,
            "required_rounds": needed,
            "note": "rounds outstanding; the task waits for another cycle (09)",
        },
    )
    maybe_request_trigger(uow, clock, task=task, pull_request=pull_request, policy=policy)


def maybe_request_trigger(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    policy: dict[str, Any],
) -> None:
    """23: with `retrigger_after_correction`, Crucible wakes the orchestrator to post the
    trigger under the operator's account. Crucible never posts it: an App-authored
    trigger comment is refused by the provider, and it is not Crucible's act (S12)."""
    section = policy.get("external_review", {})
    if not isinstance(section, dict) or not section.get("retrigger_after_correction"):
        return
    cycles = uow.review_cycles.list_for_pull_request(pull_request.id)
    if any(
        cycle.head_sha == pull_request.head_sha and cycle.state == CycleState.OPEN.value
        for cycle in cycles
    ):
        return
    open_review_cycle(
        uow,
        clock,
        task_id=task.id,
        pull_request=pull_request,
        head_sha=pull_request.head_sha,
        policy=policy,
        trigger="retrigger",
    )
    record_event(
        uow,
        clock,
        EventKind.EXTERNAL_REVIEW_TRIGGER_NEEDED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        payload={
            "pull_request": pull_request.number,
            "head_sha": pull_request.head_sha,
            "note": (
                "the trigger comment is posted by the orchestrator under the operator's "
                "account; Crucible does not post under its App identity (23, S12)"
            ),
        },
    )
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=WakeReason.EXTERNAL_REVIEW_TRIGGER_NEEDED,
        summary=(
            f"the policy asks for a new review round on #{pull_request.number} at "
            f"{pull_request.head_sha}; post the provider's trigger under the operator's "
            "account"
        ),
        task=task,
        extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
    )


def ready_summary(
    uow: UnitOfWork,
    task: Task,
    pull_request: PullRequest,
    certification: CICertification,
    gates: dict[str, Any],
) -> str:
    """23's ready-for-merge report: Crucible produces the facts, Foundry the sentence."""
    comments = list(uow.review_comments.list_for_pull_request(pull_request.id))
    dispositions = uow.dispositions.list_for_comments(
        [c.id for c in comments], {c.id: c.body_sha256 for c in comments}
    )
    checks = ", ".join(str(name) for name in certification.required_checks) or "none required"
    auto_merge = auto_merge_enabled(uow) and bool(
        policy_for(uow, task).get("delivery", {}).get("auto_merge", True)
    )
    merge_action = (
        "Hades will squash-merge this certified head and will name any refusal."
        if auto_merge and certified_jobs_green(certification)
        else "Automatic merge waits for every CI job on the head to succeed."
        if auto_merge
        else "Automatic merge is disabled globally or by policy; the operator performs the merge."
    )
    return (
        f"{pull_request.url} is ready for merge at {pull_request.head_sha}. "
        f"External review: {gates.get('completed_rounds', 0)} of "
        f"{gates.get('required_rounds', 0)} round(s), {len(comments)} comment(s), "
        f"{len(dispositions)} disposition(s). CI: {certification.state} "
        f"({checks}). {merge_action}"
    )[:1000]


# ----- timeouts -----------------------------------------------------------


def repeat_overdue_wakes(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    policy: dict[str, Any],
) -> bool:
    """Nothing received is overdue silently (23). A repeat wake, no state change.

    The clock starts when the task entered the state it is waiting in, not when the pull
    request was opened: a correction on a three-day-old pull request enters certification
    with nothing outstanding yet, and measuring from `opened_at` would call it overdue on
    its first poll.

    hades #502: exactly one open wake per task per cause. While the last one is unacked
    no copy is raised, however long the condition persists; once it is acked the notice
    comes back only when the condition still holds a full `wait_timeout_hours` after
    the ack. A wake about a pull request that has since merged or closed is acked by
    the system in the supervisor's sweep (`close_wakes_for_finished_pull_requests`)."""
    now = clock.now()
    if task.state is TaskState.AWAITING_EXTERNAL_REVIEW:
        hours = wait_timeout_hours(policy, "external_review", DEFAULT_EXTERNAL_TIMEOUT_HOURS)
        reason = WakeReason.EXTERNAL_REVIEW_OVERDUE
        entered = EventKind.TASK_AWAITING_EXTERNAL_REVIEW
        what = "an external review signal"
        way_out = (
            "; the operator can waive the remaining rounds for this task "
            f"(decision kind {WAIVE_EXTERNAL_REVIEW})"
        )
    elif task.state is TaskState.AWAITING_CI_CERTIFICATION:
        hours = wait_timeout_hours(policy, "ci_certification", DEFAULT_CI_TIMEOUT_HOURS)
        reason = WakeReason.CI_CERTIFICATION_OVERDUE
        entered = EventKind.TASK_AWAITING_CI_CERTIFICATION
        what = "a required check conclusion"
        way_out = (
            "; if the repository has no CI, the operator can accept that for this task "
            f"(decision kind {ACCEPT_NO_CI})"
        )
    else:
        return False
    interval = timedelta(hours=hours)
    since = waiting_since(uow, task, entered, fallback=pull_request.opened_at)
    if now - since < interval:
        return False
    previous = uow.wakes.list_for_task(task.id, reason=reason.value)
    if not repeat_allowed(previous, now=now, interval=interval):
        return False
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=reason,
        summary=(
            f"#{pull_request.number} has waited more than {hours}h for {what} on "
            f"{pull_request.head_sha}{way_out}"
        ),
        task=task,
        extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
    )
    return True


def waiting_since(uow: UnitOfWork, task: Task, kind: EventKind, *, fallback: datetime) -> datetime:
    """When the task entered the state it is waiting in. Each transition writes its event
    in the same transaction as the state change (09), so the event is the record."""
    event = uow.events.latest_for_task_kind(task.id, kind.value)
    return event.ts if event is not None else fallback


def poll_due(
    pull_request: PullRequest,
    *,
    now: datetime,
    poll_interval_seconds: int,
    reactions_interval_seconds: int,
    task_state: TaskState,
) -> tuple[bool, bool]:
    """(poll now, include reactions).

    23: reactions are polled every `reactions_poll_interval_seconds` while a PR is
    `awaiting_external_review`, because the pickup reaction is transient and the clean
    verdict is a reaction; on other states they ride the ordinary poll."""
    last = pull_request.last_polled_at
    due = last is None or (now - last).total_seconds() >= poll_interval_seconds
    last_reactions = pull_request.last_reactions_polled_at
    reactions_due = (
        last_reactions is None
        or (now - last_reactions).total_seconds() >= reactions_interval_seconds
    )
    if task_state is TaskState.AWAITING_EXTERNAL_REVIEW and reactions_due:
        return True, True
    return due, due and reactions_due


def clean_reaction_present(observation: Observation, allowlist: frozenset[str]) -> bool:
    return any(
        reaction.subject_kind == "pull_request"
        and reaction.content == CLEAN_REACTION
        and reaction.login in allowlist
        for reaction in observation.reactions
    )


def apply_observation(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    policy: dict[str, Any],
    attempt_id: str,
    with_reactions: bool,
    log_excerpt: str = "",
    log_fetched: bool = False,
) -> ObservationResult:
    """One poll, applied. The whole of what a tick does with a pull request."""
    result = ObservationResult()
    now = clock.now()
    pull_request.last_polled_at = now
    pull_request.observed_head_sha = observation.pull_request.head_sha
    pull_request.observed_base_ref = observation.pull_request.base_ref
    pull_request.mergeable_state = observation.pull_request.mergeable_state
    pull_request.mergeable = observation.pull_request.mergeable
    if with_reactions:
        pull_request.last_reactions_polled_at = now
    uow.pull_requests.save(pull_request)
    if correction_in_flight(uow, task):
        return _observe_during_correction(
            uow, clock, task=task, pull_request=pull_request, observation=observation
        )
    record_event(
        uow,
        clock,
        EventKind.PULL_REQUEST_POLLED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        payload={
            "pull_request": pull_request.number,
            "head_sha": observation.pull_request.head_sha,
            "state": observation.pull_request.state,
            "mergeable": observation.pull_request.mergeable,
            "mergeable_state": observation.pull_request.mergeable_state,
            "reviews": len(observation.reviews),
            "review_comments": len(observation.review_comments),
            "issue_comments": len(observation.issue_comments),
            "reactions": len(observation.reactions),
            "checks": len(observation.checks),
            "reactions_observable": observation.reactions_observable,
            "rate_limit_remaining": observation.rate_limit_remaining,
        },
    )
    allowlist = reviewer_logins(policy)
    signals: list[Signal] = []
    signals += record_reviews(
        uow, clock, task=task, pull_request=pull_request, observation=observation, result=result
    )
    signals += record_comments(
        uow,
        clock,
        task=task,
        pull_request=pull_request,
        observation=observation,
        allowlist=allowlist,
        result=result,
        replaced=replaced_heads(uow, task, pull_request),
        policy=policy,
    )
    if with_reactions:
        signals += record_reactions(
            uow,
            clock,
            task=task,
            pull_request=pull_request,
            observation=observation,
            result=result,
        )
    attach_signals(
        uow,
        clock,
        task=task,
        pull_request=pull_request,
        signals=signals,
        policy=policy,
        result=result,
    )
    observe_head(
        uow,
        clock,
        task=task,
        pull_request=pull_request,
        observed_sha=observation.pull_request.head_sha,
        result=result,
    )
    certification: CICertification | None = None
    head = accepted_head(uow, task)
    if head:
        certification = certify_head(
            uow,
            clock,
            task=task,
            pull_request=pull_request,
            observation=observation,
            policy=policy,
            head_sha=head,
            log_excerpt=log_excerpt,
            log_fetched=log_fetched,
        )
        result.certification = certification.state
    if result.diverged:
        result.state = task.state.value
        return result
    observe_state(
        uow,
        clock,
        task=task,
        pull_request=pull_request,
        observation=observation,
        result=result,
    )
    if task.state in OBSERVED_STATES:
        gates = evaluate_delivery_gates(
            uow,
            clock,
            task=task,
            attempt_id=attempt_id,
            pull_request=pull_request,
            policy=policy,
            certification=certification,
            branch_pushed_sha=head,
        )
        advance_delivery(
            uow,
            clock,
            task=task,
            pull_request=pull_request,
            policy=policy,
            gates=gates,
            certification=certification,
            result=result,
        )
        repeat_overdue_wakes(uow, clock, task=task, pull_request=pull_request, policy=policy)
    result.state = task.state.value
    return result


def _observe_during_correction(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
) -> ObservationResult:
    """hades #360: a poll while a correction runs against the open PR looks for one fact,
    the merge. Comments, checks and the head belong to the corrected head, which the
    correction publishes; the poll after that takes them in. hades #379: and a close,
    which is recorded so the PR is not polled for ever and Foundry is woken."""
    result = ObservationResult()
    record_event(
        uow,
        clock,
        EventKind.PULL_REQUEST_POLLED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        payload={
            "pull_request": pull_request.number,
            "head_sha": observation.pull_request.head_sha,
            "state": observation.pull_request.state,
            "during_correction": task.state.value,
        },
    )
    if observation.pull_request.merged or observation.pull_request.state == "closed":
        observe_state(
            uow,
            clock,
            task=task,
            pull_request=pull_request,
            observation=observation,
            result=result,
        )
    result.state = task.state.value
    return result
