"""Guarded state changes. Each writes the state and its event in one transaction (09)."""

from __future__ import annotations

from typing import Any

from crucible.application.errors import TransitionNotAllowedError
from crucible.domain.entities import Attempt, Event, Execution, Task, TaskContract
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.lifecycle import (
    AttemptState,
    ExecutionState,
    IllegalTransitionError,
    TaskState,
    check_transition,
)
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork


def record_event(
    uow: UnitOfWork,
    clock: Clock,
    kind: EventKind,
    *,
    principal: str,
    task_id: str | None = None,
    execution_id: str | None = None,
    attempt_id: str | None = None,
    payload: dict[str, Any] | None = None,
    verified: bool = True,
) -> Event:
    return uow.events.append(
        Event(
            seq=None,
            ts=clock.now(),
            kind=kind.value,
            principal=principal,
            verified=verified,
            payload=payload or {},
            task_id=task_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
        )
    )


def move_task(
    uow: UnitOfWork,
    clock: Clock,
    task: Task,
    target: TaskState,
    kind: EventKind,
    *,
    principal: str = PRINCIPAL_CRUCIBLE,
    payload: dict[str, Any] | None = None,
    execution_id: str | None = None,
    attempt_id: str | None = None,
) -> Event:
    check_transition("task", task.id, task.state, target)
    body = {"from": task.state.value, "to": target.value, **(payload or {})}
    task.state = target
    task.updated_at = clock.now()
    if target in (TaskState.CANCELLED, TaskState.CLOSED, TaskState.REJECTED):
        task.closed_at = task.updated_at
    uow.tasks.save(task)
    return record_event(
        uow,
        clock,
        kind,
        principal=principal,
        task_id=task.id,
        execution_id=execution_id,
        attempt_id=attempt_id,
        payload=body,
    )


def move_execution(
    uow: UnitOfWork,
    clock: Clock,
    execution: Execution,
    target: ExecutionState,
    kind: EventKind,
    *,
    payload: dict[str, Any] | None = None,
) -> Event:
    check_transition("execution", execution.id, execution.state, target)
    body = {"from": execution.state.value, "to": target.value, **(payload or {})}
    execution.state = target
    if target in (ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED):
        execution.ended_at = clock.now()
    uow.executions.save(execution)
    return record_event(
        uow,
        clock,
        kind,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=execution.task_id,
        execution_id=execution.id,
        payload=body,
    )


def move_attempt(
    uow: UnitOfWork,
    clock: Clock,
    attempt: Attempt,
    target: AttemptState,
    kind: EventKind,
    *,
    payload: dict[str, Any] | None = None,
) -> Event:
    check_transition("attempt", attempt.id, attempt.state, target)
    body = {"from": attempt.state.value, "to": target.value, **(payload or {})}
    attempt.state = target
    uow.attempts.save(attempt)
    return record_event(
        uow,
        clock,
        kind,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=attempt.task_id,
        execution_id=attempt.execution_id,
        attempt_id=attempt.id,
        payload=body,
    )


def record_rejected_transition(
    uow: UnitOfWork, clock: Clock, error: IllegalTransitionError, *, principal: str
) -> Event:
    """An illegal transition is recorded as an event (09). Call in a fresh transaction,
    since the one that attempted it has rolled back."""
    return record_event(
        uow,
        clock,
        EventKind.TRANSITION_REJECTED,
        principal=principal,
        task_id=error.entity_id if error.entity == "task" else None,
        execution_id=error.entity_id if error.entity == "execution" else None,
        attempt_id=error.entity_id if error.entity == "attempt" else None,
        payload={
            "entity": error.entity,
            "entity_id": error.entity_id,
            "from": error.current,
            "to": error.target,
        },
    )


def require_contract(uow: UnitOfWork, task: Task) -> TaskContract:
    """The stored contract of the task's current version. A task imported from the
    bootstrap ledger (15) is a record with `contract_version 0` and no contract, so
    anything that would run, accept, reschedule or amend it is refused here with the
    reason rather than failing on the missing document."""
    stored = uow.contracts.get(task.id, task.contract_version)
    if stored is None:
        raise TransitionNotAllowedError(
            f"task {task.id} ({task.external_id}) carries no contract (version "
            f"{task.contract_version}): a task imported from the bootstrap ledger is a "
            "record; submit a new task for the work, or cancel or close this one (15)"
        )
    return stored


def queue_key(uow: UnitOfWork, task_id: str) -> tuple[int, str]:
    """Where a scheduled task stands in the queue: the event that last put it there.
    A batch approval schedules its tasks in the selected order, one event after another,
    so they are taken in that order; a task with no such event sorts first, by id."""
    latest = 0
    for kind in (EventKind.TASK_SCHEDULED, EventKind.TASK_RETRY_SCHEDULED):
        event = uow.events.latest_for_task_kind(task_id, kind.value)
        if event is not None and event.seq is not None:
            latest = max(latest, int(event.seq))
    return latest, task_id
