"""Hades #368: the issue 352 tests built their unit of work from MagicMock and a
hand-written escalation repository, so nothing proved the closing Decision rows and
the escalation state change were persisted. These four tests rewrite that proof on
an in-memory unit of work in the same style as the #360 tests' `_Store` (tests/unit/
test_issue_360_ready_for_merge_correction.py): rows kept in dicts and lists behind the
same repository methods `crucible.application.cancel_task` and
`crucible.application.decisions` call, read back through the unit of work rather than
through a MagicMock's call history. The #360 store's own `_Escalations` and
`_NoDecisions` fakes answer only `list_for_task`, since none of its tests close an
escalation or add a decision; this file's fakes add `get`, `save` and `list_open` for
escalations and `add`, `list_for_task` for decisions so a close is actually persisted
and read back.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, cast

from crucible.application.acceptance import close_task
from crucible.application.cancel_task import cancel_task
from crucible.application.decisions import repeat_stale_escalation_wakes
from crucible.contracts.api import CancelRequest
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    Decision,
    Escalation,
    EscalationState,
    Event,
    Principal,
    Role,
    Task,
    Wake,
)
from crucible.domain.lifecycle import TaskState
from crucible.ports.repository import UnitOfWork
from tests.fixtures import FakeClock

TASK_ID = "01TASK3680000000000000001"
PRINCIPAL_ID = "01PRINC3680000000000000001"


def _task(state: TaskState, **overrides: Any) -> Task:
    return Task(
        id=TASK_ID,
        external_id="FDY-0526",
        principal_id=PRINCIPAL_ID,
        project="example-service",
        title="Test task for hades #368",
        state=state,
        contract_version=1,
        policy_name="default-software",
        policy_version=2,
        repository_id="01REPO3680000000000000001",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 1, tzinfo=UTC),
        **overrides,
    )


def _principal() -> Principal:
    return Principal(
        id=PRINCIPAL_ID,
        name="operator",
        role=Role.OPERATOR,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def _escalation(
    esc_id: str, *, task_id: str = TASK_ID, question: str, opened_at: datetime
) -> Escalation:
    return Escalation(
        id=esc_id,
        task_id=task_id,
        attempt_id="01ATT30000000000000000001",
        state=EscalationState.OPEN,
        question=question,
        opened_at=opened_at,
        closed_at=None,
        decision_id=None,
        last_wake_at=opened_at,
    )


# ----- the in-memory unit of work -------------------------------------------------


class _Tasks:
    def __init__(self, task: Task) -> None:
        self.rows: dict[str, Task] = {task.id: task}

    def get(self, task_id: str, *, for_update: bool = False) -> Task | None:
        return self.rows.get(task_id)

    def save(self, task: Task) -> None:
        self.rows[task.id] = task


class _Events:
    def __init__(self) -> None:
        self.rows: list[Event] = []

    def append(self, event: Event) -> Event:
        event.seq = len(self.rows) + 1
        self.rows.append(event)
        return event


class _Escalations:
    def __init__(self, escalations: list[Escalation]) -> None:
        self.rows: dict[str, Escalation] = {e.id: deepcopy(e) for e in escalations}

    def add(self, escalation: Escalation) -> None:
        self.rows[escalation.id] = deepcopy(escalation)

    def get(self, escalation_id: str, *, for_update: bool = False) -> Escalation | None:
        escalation = self.rows.get(escalation_id)
        return deepcopy(escalation) if escalation is not None else None

    def save(self, escalation: Escalation) -> None:
        self.rows[escalation.id] = deepcopy(escalation)

    def list_for_task(self, task_id: str) -> list[Escalation]:
        return [deepcopy(e) for e in self.rows.values() if e.task_id == task_id]

    def list_open(self) -> list[Escalation]:
        return [deepcopy(e) for e in self.rows.values() if e.state is EscalationState.OPEN]


class _Decisions:
    def __init__(self) -> None:
        self.rows: list[Decision] = []

    def add(self, decision: Decision) -> None:
        self.rows.append(decision)

    def list_for_task(self, task_id: str) -> list[Decision]:
        return [d for d in self.rows if d.task_id == task_id]


class _Wakes:
    def __init__(self) -> None:
        self.rows: list[Wake] = []

    def add(self, wake: Wake) -> None:
        self.rows.append(wake)


class _Store:
    """One unit of work over rows kept in memory, in the #360 tests' style. Commit and
    rollback are no-ops: each test reads the rows back the way the next transaction
    would, through the same repository methods the application code calls."""

    def __init__(self, task: Task, escalations: list[Escalation]) -> None:
        self.tasks = _Tasks(task)
        self.events = _Events()
        self.escalations = _Escalations(escalations)
        self.decisions = _Decisions()
        self.wakes = _Wakes()

    def __enter__(self) -> _Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def set_fenced_token(self, fenced_token: int) -> None:
        return None

    def uow(self) -> UnitOfWork:
        return cast(UnitOfWork, self)


# ----- AC1: cancelling or closing persists a closing Decision and closes the escalation


def test_cancel_task_closes_open_escalation_with_real_uow() -> None:
    task = _task(TaskState.SUBMITTED)
    esc = _escalation(
        "01ESC30000000000000000001",
        question="How to reach origin?",
        opened_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    store = _Store(task, [esc])
    clock = FakeClock()
    principal = _principal()
    request = CancelRequest(
        reason="test cancel",
        verbatim="I cancel this task because of testing.",
        decided_by="test",
    )

    result = cancel_task(store.uow(), clock, principal=principal, task_id=task.id, request=request)

    assert result.state is TaskState.CANCELLED

    closed_esc = store.escalations.get(esc.id)
    assert closed_esc is not None
    assert closed_esc.state is EscalationState.CLOSED
    assert closed_esc.decision_id is not None

    decisions = store.decisions.list_for_task(task.id)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.kind == "task_cancelled"
    assert decision.verbatim == "I cancel this task because of testing."
    assert decision.resolves == "How to reach origin?"
    assert decision.escalation_id == closed_esc.id
    assert decision.task_id == task.id
    assert closed_esc.decision_id == decision.id


def test_close_task_closes_open_escalation_with_real_uow() -> None:
    task = _task(TaskState.ACCEPTED)
    esc = _escalation(
        "01ESC30000000000000000002",
        question="What is the answer to life?",
        opened_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    store = _Store(task, [esc])
    clock = FakeClock()
    principal = _principal()

    result = close_task(
        store.uow(),
        clock,
        principal=principal,
        task_id=task.id,
        note="Task is done for testing.",
    )

    assert result.state is TaskState.CLOSED

    closed_esc = store.escalations.get(esc.id)
    assert closed_esc is not None
    assert closed_esc.state is EscalationState.CLOSED
    assert closed_esc.decision_id is not None

    decisions = store.decisions.list_for_task(task.id)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.kind == "task_closed"
    assert decision.verbatim == "Task is done for testing."
    assert decision.resolves == "What is the answer to life?"
    assert decision.escalation_id == closed_esc.id
    assert decision.task_id == task.id
    assert closed_esc.decision_id == decision.id


# ----- AC2: the stale-reminder sweep skips terminal tasks, still wakes a blocked one


def test_repeat_stale_escalation_wakes_skips_terminal_tasks_with_real_uow() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    clock = FakeClock(now)
    stale_hours = 24

    for terminal_state in (TaskState.CANCELLED, TaskState.REJECTED, TaskState.CLOSED):
        task = _task(terminal_state)
        esc = _escalation(
            "01ESC30000000000000000003",
            question="Stale escalation",
            opened_at=now - timedelta(hours=stale_hours + 1),
        )
        store = _Store(task, [esc])

        repeated = repeat_stale_escalation_wakes(store.uow(), clock, stale_hours=stale_hours)

        assert repeated == 0
        assert store.wakes.rows == []
        unchanged = store.escalations.get(esc.id)
        assert unchanged is not None
        assert unchanged.state is EscalationState.OPEN
        assert unchanged.last_wake_at == esc.opened_at


def test_repeat_stale_escalation_wakes_still_wakes_for_blocked_task_with_real_uow() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    clock = FakeClock(now)
    stale_hours = 24
    task = _task(TaskState.BLOCKED)
    esc = _escalation(
        "01ESC30000000000000000004",
        question="Stale escalation on blocked task",
        opened_at=now - timedelta(hours=stale_hours + 1),
    )
    store = _Store(task, [esc])

    repeated = repeat_stale_escalation_wakes(store.uow(), clock, stale_hours=stale_hours)

    assert repeated == 1
    assert len(store.wakes.rows) == 1
    wake = store.wakes.rows[0]
    assert wake.reason == WakeReason.ESCALATION_STALE.value
    assert f"escalation {esc.id} has been open since" in wake.payload["summary"]
    assert wake.task_id == task.id
    assert wake.principal_id == task.principal_id

    updated = store.escalations.get(esc.id)
    assert updated is not None
    assert updated.state is EscalationState.OPEN
    assert updated.last_wake_at == now
