"""Lifecycle state machines as data (09).

Every state is a column value guarded by a transition table here. An illegal
transition raises IllegalTransitionError; the caller records it as an event. Nothing
bypasses these tables.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class TaskState(StrEnum):
    SUBMITTED = "submitted"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    AWAITING_QUOTA = "awaiting_quota"
    REPORTED = "reported"
    BLOCKED = "blocked"
    PRE_PR_GATES_FAILED = "pre_pr_gates_failed"
    AWAITING_INTERNAL_REVIEW = "awaiting_internal_review"
    GATES_PASSED = "gates_passed"
    AWAITING_ACCEPTANCE = "awaiting_acceptance"
    ACCEPTED = "accepted"
    PUBLISHING = "publishing"
    PUBLISH_FAILED = "publish_failed"
    AWAITING_EXTERNAL_REVIEW = "awaiting_external_review"
    EXTERNAL_FEEDBACK_RECEIVED = "external_feedback_received"
    AWAITING_CI_CERTIFICATION = "awaiting_ci_certification"
    CI_CERTIFICATION_FAILED = "ci_certification_failed"
    HEAD_DIVERGED = "head_diverged"
    READY_FOR_MERGE = "ready_for_merge"
    MERGED = "merged"
    RELEASE_CANDIDATE = "release_candidate"
    RELEASED = "released"
    REJECTED = "rejected"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class EscalationState(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"


class ExecutionState(StrEnum):
    CREATED = "created"
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptState(StrEnum):
    PENDING = "pending"
    PREPARING = "preparing"
    LAUNCHING = "launching"
    RUNNING = "running"
    TERMINATING = "terminating"
    EXITED = "exited"
    COLLECTED = "collected"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"


TASK_TERMINAL: frozenset[TaskState] = frozenset(
    {TaskState.CANCELLED, TaskState.REJECTED, TaskState.CLOSED}
)
EXECUTION_TERMINAL: frozenset[ExecutionState] = frozenset(
    {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}
)
ATTEMPT_TERMINAL: frozenset[AttemptState] = frozenset(
    {AttemptState.SUCCEEDED, AttemptState.BLOCKED, AttemptState.FAILED}
)

_S = TaskState
_CANCELLABLE_AT_ONCE: tuple[TaskState, ...] = (
    _S.SUBMITTED,
    _S.SCHEDULED,
    _S.BLOCKED,
    _S.AWAITING_QUOTA,
    _S.AWAITING_INTERNAL_REVIEW,
    _S.AWAITING_ACCEPTANCE,
    _S.PRE_PR_GATES_FAILED,
    _S.PUBLISH_FAILED,
    _S.PUBLISHING,
    _S.AWAITING_EXTERNAL_REVIEW,
    _S.EXTERNAL_FEEDBACK_RECEIVED,
    _S.AWAITING_CI_CERTIFICATION,
    _S.CI_CERTIFICATION_FAILED,
    _S.HEAD_DIVERGED,
    _S.READY_FOR_MERGE,
)
# hades #360: the states a correction passes through on its way back to the same pull
# request: scheduled, running, and the pre-PR gates. The PR stays open on GitHub the
# whole time, and a person can still merge it; the merge is observed from these states
# too. hades #379: and while the corrected head is publishing, or its publication failed.
CORRECTION_STATES: frozenset[TaskState] = frozenset(
    {
        _S.SCHEDULED,
        _S.AWAITING_QUOTA,
        _S.RUNNING,
        _S.BLOCKED,
        _S.REPORTED,
        _S.PRE_PR_GATES_FAILED,
        _S.AWAITING_INTERNAL_REVIEW,
        _S.GATES_PASSED,
        _S.AWAITING_ACCEPTANCE,
        _S.PUBLISHING,
        _S.PUBLISH_FAILED,
    }
)

# (from, to) pairs. The whole table from 09 is data here even though C1 only
# drives the supervision half; the delivery half is reachable only from C2 on.
TASK_TRANSITIONS: frozenset[tuple[TaskState, TaskState]] = frozenset(
    {
        (_S.SUBMITTED, _S.SCHEDULED),
        (_S.SCHEDULED, _S.RUNNING),
        (_S.SCHEDULED, _S.AWAITING_QUOTA),
        (_S.RUNNING, _S.REPORTED),
        (_S.RUNNING, _S.BLOCKED),
        (_S.RUNNING, _S.SCHEDULED),
        (_S.RUNNING, _S.AWAITING_QUOTA),
        (_S.AWAITING_QUOTA, _S.SCHEDULED),
        (_S.AWAITING_QUOTA, _S.REPORTED),
        (_S.BLOCKED, _S.SCHEDULED),
        (_S.REPORTED, _S.PRE_PR_GATES_FAILED),
        (_S.REPORTED, _S.AWAITING_INTERNAL_REVIEW),
        (_S.REPORTED, _S.GATES_PASSED),
        (_S.AWAITING_INTERNAL_REVIEW, _S.GATES_PASSED),
        (_S.AWAITING_INTERNAL_REVIEW, _S.PRE_PR_GATES_FAILED),
        (_S.GATES_PASSED, _S.AWAITING_ACCEPTANCE),
        (_S.GATES_PASSED, _S.ACCEPTED),
        (_S.GATES_PASSED, _S.PUBLISHING),
        (_S.AWAITING_ACCEPTANCE, _S.ACCEPTED),
        (_S.AWAITING_ACCEPTANCE, _S.PUBLISHING),
        (_S.AWAITING_ACCEPTANCE, _S.REJECTED),
        (_S.AWAITING_ACCEPTANCE, _S.SCHEDULED),
        (_S.PRE_PR_GATES_FAILED, _S.SCHEDULED),
        (_S.PRE_PR_GATES_FAILED, _S.REJECTED),
        (_S.PUBLISHING, _S.ACCEPTED),
        (_S.PUBLISHING, _S.AWAITING_EXTERNAL_REVIEW),
        (_S.PUBLISHING, _S.AWAITING_CI_CERTIFICATION),
        (_S.PUBLISHING, _S.PUBLISH_FAILED),
        (_S.PUBLISH_FAILED, _S.PUBLISHING),
        (_S.PUBLISH_FAILED, _S.SCHEDULED),
        (_S.PUBLISH_FAILED, _S.CANCELLED),
        (_S.AWAITING_EXTERNAL_REVIEW, _S.EXTERNAL_FEEDBACK_RECEIVED),
        (_S.EXTERNAL_FEEDBACK_RECEIVED, _S.AWAITING_CI_CERTIFICATION),
        (_S.EXTERNAL_FEEDBACK_RECEIVED, _S.AWAITING_EXTERNAL_REVIEW),
        (_S.EXTERNAL_FEEDBACK_RECEIVED, _S.SCHEDULED),
        (_S.AWAITING_CI_CERTIFICATION, _S.READY_FOR_MERGE),
        (_S.AWAITING_CI_CERTIFICATION, _S.CI_CERTIFICATION_FAILED),
        (_S.AWAITING_CI_CERTIFICATION, _S.EXTERNAL_FEEDBACK_RECEIVED),
        (_S.CI_CERTIFICATION_FAILED, _S.AWAITING_CI_CERTIFICATION),
        (_S.CI_CERTIFICATION_FAILED, _S.SCHEDULED),
        (_S.CI_CERTIFICATION_FAILED, _S.REJECTED),
        (_S.AWAITING_EXTERNAL_REVIEW, _S.HEAD_DIVERGED),
        (_S.EXTERNAL_FEEDBACK_RECEIVED, _S.HEAD_DIVERGED),
        (_S.AWAITING_CI_CERTIFICATION, _S.HEAD_DIVERGED),
        (_S.READY_FOR_MERGE, _S.HEAD_DIVERGED),
        (_S.READY_FOR_MERGE, _S.CI_CERTIFICATION_FAILED),
        # A clean publisher-side merge of current main creates a new branch head. Its
        # checks certify that head; it does not need a test of the old head against main.
        (_S.READY_FOR_MERGE, _S.AWAITING_CI_CERTIFICATION),
        (_S.READY_FOR_MERGE, _S.EXTERNAL_FEEDBACK_RECEIVED),
        (_S.READY_FOR_MERGE, _S.SCHEDULED),
        # hades #411: a conflicting pull request whose merge of main stopped on conflicts
        # gets a merge-main correction from any delivery state its head is trusted in.
        (_S.AWAITING_EXTERNAL_REVIEW, _S.SCHEDULED),
        (_S.AWAITING_CI_CERTIFICATION, _S.SCHEDULED),
        (_S.HEAD_DIVERGED, _S.REPORTED),
        # A `recollect` decision puts the task back into supervision against the remote
        # work branch, which is where the divergent head is. 09 draws this edge to
        # `reported`; C4 re-enters at `scheduled` so the new head gets a claim and the
        # full pre-PR path rather than gates that fail for want of a report
        # (docs/implementation-notes/c4.md).
        (_S.HEAD_DIVERGED, _S.SCHEDULED),
        (_S.HEAD_DIVERGED, _S.REJECTED),
        (_S.HEAD_DIVERGED, _S.CANCELLED),
        (_S.READY_FOR_MERGE, _S.MERGED),
        (_S.READY_FOR_MERGE, _S.REJECTED),
        # hades FDY-0139: a merge is a fact about the pull request, and a person can merge
        # at any point after it is opened. An observed merge from any delivery state moves
        # the task to `merged`; otherwise the task waits for ever on a PR nobody polls.
        (_S.AWAITING_EXTERNAL_REVIEW, _S.MERGED),
        (_S.EXTERNAL_FEEDBACK_RECEIVED, _S.MERGED),
        (_S.AWAITING_CI_CERTIFICATION, _S.MERGED),
        (_S.CI_CERTIFICATION_FAILED, _S.MERGED),
        (_S.HEAD_DIVERGED, _S.MERGED),
        # hades #360: and from a correction against the open PR. A running attempt is
        # then ended by the supervisor, as for a cancel.
        *((s, _S.MERGED) for s in CORRECTION_STATES),
        # hades FDY-0139: the operator waived the outstanding external review rounds, so
        # the task stops waiting for a reviewer and goes on to certification.
        (_S.AWAITING_EXTERNAL_REVIEW, _S.AWAITING_CI_CERTIFICATION),
        # 23: "a PR closed without merge moves the task to rejected". 09's table draws
        # that edge only from ready_for_merge, but a person can close a PR at any point
        # after it is opened (docs/implementation-notes/c4.md).
        (_S.AWAITING_EXTERNAL_REVIEW, _S.REJECTED),
        (_S.EXTERNAL_FEEDBACK_RECEIVED, _S.REJECTED),
        (_S.AWAITING_CI_CERTIFICATION, _S.REJECTED),
        (_S.MERGED, _S.RELEASE_CANDIDATE),
        (_S.RELEASE_CANDIDATE, _S.RELEASED),
        (_S.RELEASE_CANDIDATE, _S.MERGED),
        (_S.ACCEPTED, _S.CLOSED),
        (_S.MERGED, _S.CLOSED),
        (_S.RELEASED, _S.CLOSED),
        (_S.RUNNING, _S.CANCELLING),
        (_S.CANCELLING, _S.CANCELLED),
        *((s, _S.CANCELLED) for s in _CANCELLABLE_AT_ONCE),
    }
)

_E = ExecutionState
EXECUTION_TRANSITIONS: frozenset[tuple[ExecutionState, ExecutionState]] = frozenset(
    {
        (_E.CREATED, _E.ACTIVE),
        (_E.CREATED, _E.CANCELLED),
        (_E.ACTIVE, _E.SUCCEEDED),
        (_E.ACTIVE, _E.FAILED),
        (_E.ACTIVE, _E.CANCELLED),
    }
)

_A = AttemptState
ATTEMPT_TRANSITIONS: frozenset[tuple[AttemptState, AttemptState]] = frozenset(
    {
        (_A.PENDING, _A.PREPARING),
        (_A.PREPARING, _A.LAUNCHING),
        (_A.LAUNCHING, _A.RUNNING),
        (_A.RUNNING, _A.EXITED),
        (_A.RUNNING, _A.TERMINATING),
        (_A.TERMINATING, _A.EXITED),
        (_A.EXITED, _A.COLLECTED),
        (_A.COLLECTED, _A.SUCCEEDED),
        (_A.COLLECTED, _A.BLOCKED),
        (_A.COLLECTED, _A.FAILED),
        (_A.PREPARING, _A.COLLECTED),
        (_A.LAUNCHING, _A.COLLECTED),
        # A pending attempt whose task was cancelled never launches.
        (_A.PENDING, _A.COLLECTED),
    }
)


_X = EscalationState
ESCALATION_TRANSITIONS: frozenset[tuple[EscalationState, EscalationState]] = frozenset(
    {
        (_X.OPEN, _X.ANSWERED),
        (_X.ANSWERED, _X.CLOSED),
        (_X.OPEN, _X.CLOSED),
    }
)


class IllegalTransitionError(Exception):
    """Raised when the transition table does not permit current -> target."""

    def __init__(self, entity: str, entity_id: str, current: str, target: str) -> None:
        super().__init__(f"{entity} {entity_id}: {current} -> {target} is not allowed")
        self.entity = entity
        self.entity_id = entity_id
        self.current = current
        self.target = target


_TABLES: Mapping[str, frozenset[tuple[StrEnum, StrEnum]]] = {
    "task": TASK_TRANSITIONS,
    "execution": EXECUTION_TRANSITIONS,
    "attempt": ATTEMPT_TRANSITIONS,
    "escalation": ESCALATION_TRANSITIONS,
}


def is_allowed(entity: str, current: StrEnum, target: StrEnum) -> bool:
    return (current, target) in _TABLES[entity]


def check_transition(entity: str, entity_id: str, current: StrEnum, target: StrEnum) -> None:
    """Raise IllegalTransitionError unless the table permits current -> target."""
    if not is_allowed(entity, current, target):
        raise IllegalTransitionError(entity, entity_id, str(current), str(target))
