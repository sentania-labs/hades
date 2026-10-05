from __future__ import annotations

from itertools import pairwise

import pytest

from crucible.domain.lifecycle import (
    ATTEMPT_TRANSITIONS,
    ESCALATION_TRANSITIONS,
    EXECUTION_TRANSITIONS,
    TASK_TERMINAL,
    TASK_TRANSITIONS,
    AttemptState,
    EscalationState,
    ExecutionState,
    IllegalTransitionError,
    TaskState,
    check_transition,
    is_allowed,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskState.SUBMITTED, TaskState.SCHEDULED),
        (TaskState.SCHEDULED, TaskState.RUNNING),
        (TaskState.RUNNING, TaskState.REPORTED),
        (TaskState.RUNNING, TaskState.BLOCKED),
        (TaskState.RUNNING, TaskState.SCHEDULED),
        (TaskState.RUNNING, TaskState.CANCELLING),
        (TaskState.CANCELLING, TaskState.CANCELLED),
        (TaskState.SUBMITTED, TaskState.CANCELLED),
        (TaskState.BLOCKED, TaskState.SCHEDULED),
        (TaskState.REPORTED, TaskState.GATES_PASSED),
        (TaskState.READY_FOR_MERGE, TaskState.MERGED),
        (TaskState.MERGED, TaskState.CLOSED),
    ],
)
def test_task_allowed(current: TaskState, target: TaskState) -> None:
    check_transition("task", "t", current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskState.SUBMITTED, TaskState.RUNNING),
        (TaskState.SUBMITTED, TaskState.REPORTED),
        (TaskState.SCHEDULED, TaskState.REPORTED),
        (TaskState.REPORTED, TaskState.RUNNING),
        (TaskState.CANCELLED, TaskState.SCHEDULED),
        (TaskState.CLOSED, TaskState.SUBMITTED),
        (TaskState.RUNNING, TaskState.CANCELLED),
        (TaskState.REPORTED, TaskState.CANCELLED),
        (TaskState.ACCEPTED, TaskState.PUBLISHING),
    ],
)
def test_task_disallowed(current: TaskState, target: TaskState) -> None:
    with pytest.raises(IllegalTransitionError) as exc:
        check_transition("task", "t1", current, target)
    assert exc.value.entity == "task"
    assert exc.value.entity_id == "t1"
    assert str(exc.value) == f"task t1: {current.value} -> {target.value} is not allowed"


def test_terminal_task_states_have_no_exit() -> None:
    for state in TASK_TERMINAL:
        assert not any(src == state for src, _ in TASK_TRANSITIONS)


def test_every_task_state_is_reachable() -> None:
    reachable = {dst for _, dst in TASK_TRANSITIONS} | {TaskState.SUBMITTED}
    assert reachable == set(TaskState)


def test_execution_table() -> None:
    assert is_allowed("execution", ExecutionState.CREATED, ExecutionState.ACTIVE)
    assert is_allowed("execution", ExecutionState.ACTIVE, ExecutionState.SUCCEEDED)
    assert not is_allowed("execution", ExecutionState.SUCCEEDED, ExecutionState.ACTIVE)
    assert not is_allowed("execution", ExecutionState.CREATED, ExecutionState.SUCCEEDED)
    assert len(EXECUTION_TRANSITIONS) == 5


def test_attempt_table() -> None:
    path = [
        AttemptState.PENDING,
        AttemptState.PREPARING,
        AttemptState.LAUNCHING,
        AttemptState.RUNNING,
        AttemptState.EXITED,
        AttemptState.COLLECTED,
        AttemptState.SUCCEEDED,
    ]
    for a, b in pairwise(path):
        check_transition("attempt", "a", a, b)
    assert is_allowed("attempt", AttemptState.RUNNING, AttemptState.TERMINATING)
    assert is_allowed("attempt", AttemptState.PREPARING, AttemptState.COLLECTED)
    assert not is_allowed("attempt", AttemptState.RUNNING, AttemptState.SUCCEEDED)
    assert not is_allowed("attempt", AttemptState.EXITED, AttemptState.SUCCEEDED)
    assert not is_allowed("attempt", AttemptState.SUCCEEDED, AttemptState.PENDING)
    assert (AttemptState.PENDING, AttemptState.RUNNING) not in ATTEMPT_TRANSITIONS


# ----- C2: the supervision half past `reported`, and escalations ---------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskState.REPORTED, TaskState.PRE_PR_GATES_FAILED),
        (TaskState.REPORTED, TaskState.AWAITING_INTERNAL_REVIEW),
        (TaskState.AWAITING_INTERNAL_REVIEW, TaskState.GATES_PASSED),
        (TaskState.GATES_PASSED, TaskState.AWAITING_ACCEPTANCE),
        (TaskState.GATES_PASSED, TaskState.ACCEPTED),
        (TaskState.GATES_PASSED, TaskState.PUBLISHING),
        (TaskState.AWAITING_ACCEPTANCE, TaskState.ACCEPTED),
        (TaskState.AWAITING_ACCEPTANCE, TaskState.REJECTED),
        (TaskState.AWAITING_ACCEPTANCE, TaskState.SCHEDULED),
        (TaskState.PRE_PR_GATES_FAILED, TaskState.SCHEDULED),
        (TaskState.PRE_PR_GATES_FAILED, TaskState.REJECTED),
        (TaskState.ACCEPTED, TaskState.CLOSED),
        (TaskState.AWAITING_INTERNAL_REVIEW, TaskState.CANCELLED),
        (TaskState.AWAITING_ACCEPTANCE, TaskState.CANCELLED),
        (TaskState.PRE_PR_GATES_FAILED, TaskState.CANCELLED),
    ],
)
def test_c2_task_allowed(current: TaskState, target: TaskState) -> None:
    check_transition("task", "t", current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskState.REPORTED, TaskState.AWAITING_ACCEPTANCE),
        (TaskState.AWAITING_INTERNAL_REVIEW, TaskState.AWAITING_ACCEPTANCE),
        (TaskState.AWAITING_ACCEPTANCE, TaskState.GATES_PASSED),
        (TaskState.PRE_PR_GATES_FAILED, TaskState.ACCEPTED),
        (TaskState.PRE_PR_GATES_FAILED, TaskState.GATES_PASSED),
        (TaskState.REJECTED, TaskState.SCHEDULED),
        (TaskState.ACCEPTED, TaskState.AWAITING_ACCEPTANCE),
    ],
)
def test_c2_task_disallowed(current: TaskState, target: TaskState) -> None:
    with pytest.raises(IllegalTransitionError):
        check_transition("task", "t", current, target)


def test_escalation_table() -> None:
    check_transition("escalation", "e", EscalationState.OPEN, EscalationState.ANSWERED)
    check_transition("escalation", "e", EscalationState.ANSWERED, EscalationState.CLOSED)
    check_transition("escalation", "e", EscalationState.OPEN, EscalationState.CLOSED)
    assert not is_allowed("escalation", EscalationState.CLOSED, EscalationState.OPEN)
    assert not is_allowed("escalation", EscalationState.ANSWERED, EscalationState.OPEN)
    assert len(ESCALATION_TRANSITIONS) == 3
