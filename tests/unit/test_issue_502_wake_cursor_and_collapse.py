"""hades #502: `GET /v1/wakes` pages by wake id, and a repeating notice keeps exactly one
open wake per task per cause.

AC1: 300 pending wakes sharing one `created_at` page through the cursor, each once, then
`next_cursor` null. AC4: a caller that still sends `since` gets a valid page. AC2: an
overdue condition that persists with the wake unacked leaves one open wake; after an ack
and another interval a new one is raised. AC3: a pending wake about a pull request that
has merged is acked by the system with a recorded reason on the next pass."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from crucible.application.decisions import repeat_stale_escalation_wakes
from crucible.application.observation import repeat_overdue_wakes
from crucible.application.queries import decode_cursor, wake_list
from crucible.application.transitions import record_event
from crucible.application.wakes import (
    ack_wake,
    close_wakes_for_finished_pull_requests,
    create_wake,
    repeat_allowed,
)
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    Escalation,
    EscalationState,
    Event,
    Principal,
    PullRequest,
    PullRequestState,
    Role,
    Task,
    Wake,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.lifecycle import TaskState
from crucible.ports.repository import UnitOfWork
from tests.fixtures import FakeClock

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
PRINCIPAL_ID = "01PRINCIPAL000000000000001"
TASK_ID = "01TASK00000000000000000001"
OVERDUE_REASONS = (WakeReason.EXTERNAL_REVIEW_OVERDUE, WakeReason.CI_CERTIFICATION_OVERDUE)


# ----- an in-memory unit of work with the wake repository's real contract ------------


class FakeWakes:
    """The in-memory twin of `Wakes` in records.py: id order, `after_id` strictly after."""

    def __init__(self) -> None:
        self.rows: dict[str, Wake] = {}

    def add(self, wake: Wake) -> None:
        self.rows[wake.id] = wake

    def get(self, wake_id: str, *, for_update: bool = False) -> Wake | None:
        return self.rows.get(wake_id)

    def save(self, wake: Wake) -> None:
        self.rows[wake.id] = wake

    def list_for_principal(
        self,
        principal_id: str,
        *,
        since: datetime | None,
        include_acked: bool,
        limit: int,
        after_id: str | None = None,
    ) -> Sequence[Wake]:
        rows = [w for w in self.rows.values() if w.principal_id == principal_id]
        if not include_acked:
            rows = [w for w in rows if w.acked_at is None]
        if after_id is not None:
            rows = [w for w in rows if w.id > after_id]
        if since is not None:
            rows = [w for w in rows if w.created_at >= since]
        return sorted(rows, key=lambda w: w.id)[:limit]

    def list_for_task(
        self, task_id: str, *, reason: str, include_acked: bool = True
    ) -> Sequence[Wake]:
        rows = [w for w in self.rows.values() if w.task_id == task_id and w.reason == reason]
        if not include_acked:
            rows = [w for w in rows if w.acked_at is None]
        return sorted(rows, key=lambda w: w.id)

    def list_unacked_for_reasons(self, reasons: Sequence[str]) -> Sequence[Wake]:
        rows = [w for w in self.rows.values() if w.acked_at is None and w.reason in reasons]
        return sorted(rows, key=lambda w: w.id)

    def open_for(self, task_id: str, reason: WakeReason) -> list[Wake]:
        return list(self.list_for_task(task_id, reason=reason.value, include_acked=False))


class FakeEvents:
    def __init__(self) -> None:
        self.rows: list[Event] = []

    def append(self, event: Event) -> Event:
        event.seq = len(self.rows) + 1
        self.rows.append(event)
        return event

    def latest_for_task_kind(self, task_id: str, kind: str) -> Event | None:
        matches = [e for e in self.rows if e.task_id == task_id and e.kind == kind]
        return matches[-1] if matches else None

    def of_kind(self, kind: EventKind) -> list[Event]:
        return [e for e in self.rows if e.kind == kind.value]


class FakeById:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    def add(self, row: Any) -> None:
        self.rows[row.id] = row

    def get(self, row_id: str, *, for_update: bool = False) -> Any | None:
        return self.rows.get(row_id)

    def save(self, row: Any) -> None:
        self.rows[row.id] = row


class FakePullRequests(FakeById):
    def get_for_task(self, task_id: str, *, for_update: bool = False) -> PullRequest | None:
        return next((p for p in self.rows.values() if p.task_id == task_id), None)


class FakeEscalations(FakeById):
    def __init__(self) -> None:
        super().__init__()
        self.saved: list[Escalation] = []

    def save(self, row: Any) -> None:
        super().save(row)
        self.saved.append(row)

    def list_open(self) -> Sequence[Escalation]:
        return [e for e in self.rows.values() if e.state is EscalationState.OPEN]


@dataclass
class FakeUnitOfWork:
    wakes: FakeWakes = field(default_factory=FakeWakes)
    events: FakeEvents = field(default_factory=FakeEvents)
    tasks: FakeById = field(default_factory=FakeById)
    pull_requests: FakePullRequests = field(default_factory=FakePullRequests)
    escalations: FakeEscalations = field(default_factory=FakeEscalations)
    principals: FakeById = field(default_factory=FakeById)

    def commit(self) -> None:
        pass

    @property
    def port(self) -> UnitOfWork:
        """The fake under the port's type, for the application functions it serves."""
        return cast(UnitOfWork, self)


def principal() -> Principal:
    return Principal(id=PRINCIPAL_ID, name="foundry", role=Role.ORCHESTRATOR, created_at=START)


def task(state: TaskState = TaskState.AWAITING_EXTERNAL_REVIEW) -> Task:
    return Task(
        id=TASK_ID,
        external_id="FDY-0502",
        principal_id=PRINCIPAL_ID,
        project="hades",
        title="hades #502",
        state=state,
        contract_version=1,
        policy_name="default-software",
        policy_version=1,
        repository_id="01REPO00000000000000000001",
        created_at=START - timedelta(days=3),
        updated_at=START - timedelta(days=3),
    )


def pull_request(state: PullRequestState = PullRequestState.OPEN) -> PullRequest:
    return PullRequest(
        id="01PR000000000000000000000001",
        task_id=TASK_ID,
        repository_id="01REPO00000000000000000001",
        number=502,
        url="https://github.com/sentania-labs/hades/pull/502",
        base_ref="main",
        work_branch="crucible/FDY-0502",
        state=state,
        head_sha="abc1234",
        opened_at=START - timedelta(days=3),
    )


def waiting_uow(
    clock: FakeClock, *, state: TaskState = TaskState.AWAITING_EXTERNAL_REVIEW
) -> tuple[FakeUnitOfWork, Task, PullRequest]:
    """A task that entered `state` at the clock's current time, with its pull request."""
    uow = FakeUnitOfWork()
    uow.principals.add(principal())
    t = task(state)
    uow.tasks.add(t)
    pr = pull_request()
    uow.pull_requests.add(pr)
    entered = (
        EventKind.TASK_AWAITING_EXTERNAL_REVIEW
        if state is TaskState.AWAITING_EXTERNAL_REVIEW
        else EventKind.TASK_AWAITING_CI_CERTIFICATION
    )
    record_event(uow.port, clock, entered, principal=PRINCIPAL_CRUCIBLE, task_id=t.id)
    return uow, t, pr


def wakes_in_one_instant(uow: FakeUnitOfWork, clock: FakeClock, t: Task, count: int) -> list[str]:
    """`count` pending wakes that share one `created_at`: what a flood looks like."""
    return [
        create_wake(
            uow.port,
            clock,
            principal_id=PRINCIPAL_ID,
            reason=WakeReason.EXTERNAL_REVIEW_OVERDUE,
            summary=f"wake {i}",
            task=t,
        ).id
        for i in range(count)
    ]


POLICY_1H: dict[str, Any] = {
    "external_review": {"wait_timeout_hours": 1},
    "ci_certification": {"wait_timeout_hours": 1},
}


# ----- AC1 and AC4: the cursor ----------------------------------------------------------


def test_300_wakes_sharing_one_created_at_page_once_each_then_null() -> None:
    """AC1: every page resumes strictly after the last id, each wake appears once, and
    the last page carries `next_cursor` null."""
    clock = FakeClock(START)
    uow = FakeUnitOfWork()
    uow.principals.add(principal())
    t = task()
    uow.tasks.add(t)
    ids = wakes_in_one_instant(uow, clock, t, 300)
    assert len({w.created_at for w in uow.wakes.rows.values()}) == 1

    seen: list[str] = []
    cursors: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = wake_list(
            uow.port,
            principal_id=PRINCIPAL_ID,
            since=None,
            include_acked=False,
            limit=50,
            cursor=cursor,
        )
        pages += 1
        assert pages <= 6, "paging did not terminate"
        seen.extend(item.id for item in page.items)
        if page.next_cursor is None:
            break
        assert page.next_cursor not in cursors, "a cursor repeated, the client would refuse it"
        assert decode_cursor(page.next_cursor) == page.items[-1].id
        cursors.append(page.next_cursor)
        cursor = page.next_cursor

    assert pages == 6
    assert len(seen) == 300
    assert len(set(seen)) == 300
    assert set(seen) == set(ids)
    assert seen == sorted(seen), "pages come in wake id order"


def test_exactly_a_page_of_wakes_ends_with_next_cursor_null() -> None:
    """A principal with exactly `limit` wakes gets one full page and no second one."""
    clock = FakeClock(START)
    uow = FakeUnitOfWork()
    uow.principals.add(principal())
    t = task()
    uow.tasks.add(t)
    wakes_in_one_instant(uow, clock, t, 50)
    page = wake_list(uow.port, principal_id=PRINCIPAL_ID, since=None, include_acked=False, limit=50)
    assert len(page.items) == 50
    assert page.next_cursor is None


def test_cursor_resumes_strictly_after_the_last_id_even_when_it_was_acked() -> None:
    """The cursor is the id, not a time: acking the last wake of a page between two calls
    does not shift the next page."""
    clock = FakeClock(START)
    uow = FakeUnitOfWork()
    uow.principals.add(principal())
    t = task()
    uow.tasks.add(t)
    ids = sorted(wakes_in_one_instant(uow, clock, t, 10))
    first = wake_list(uow.port, principal_id=PRINCIPAL_ID, since=None, include_acked=False, limit=4)
    assert [i.id for i in first.items] == ids[:4]
    assert first.next_cursor is not None
    ack_wake(uow.port, clock, principal=principal(), wake_id=ids[3], note="handled")
    second = wake_list(
        uow.port,
        principal_id=PRINCIPAL_ID,
        since=None,
        include_acked=False,
        limit=4,
        cursor=first.next_cursor,
    )
    assert [i.id for i in second.items] == ids[4:8]


def test_a_caller_that_sends_since_still_gets_a_valid_page() -> None:
    """AC4: `since` narrows the page to wakes created at or after it, and the page still
    carries a usable cursor that resumes by id."""
    clock = FakeClock(START - timedelta(hours=2))
    uow = FakeUnitOfWork()
    uow.principals.add(principal())
    t = task()
    uow.tasks.add(t)
    old = wakes_in_one_instant(uow, clock, t, 3)
    clock.advance(3600)
    recent_since = clock.now()
    recent = wakes_in_one_instant(uow, clock, t, 120)

    page = wake_list(
        uow.port, principal_id=PRINCIPAL_ID, since=recent_since, include_acked=False, limit=50
    )
    assert len(page.items) == 50
    assert page.next_cursor is not None
    assert all(item.created_at >= recent_since for item in page.items)
    assert not {item.id for item in page.items} & set(old)

    seen = [item.id for item in page.items]
    cursor: str | None = page.next_cursor
    while cursor is not None:
        page = wake_list(
            uow.port,
            principal_id=PRINCIPAL_ID,
            since=recent_since,
            include_acked=False,
            limit=50,
            cursor=cursor,
        )
        seen.extend(item.id for item in page.items)
        cursor = page.next_cursor
    assert sorted(seen) == sorted(recent)
    assert len(seen) == len(set(seen)) == 120

    everything = wake_list(
        uow.port, principal_id=PRINCIPAL_ID, since=None, include_acked=False, limit=200
    )
    assert len(everything.items) == 123
    assert everything.next_cursor is None


# ----- AC2: one open wake per task per cause --------------------------------------------


@pytest.mark.parametrize(
    "state", [TaskState.AWAITING_EXTERNAL_REVIEW, TaskState.AWAITING_CI_CERTIFICATION]
)
def test_overdue_condition_persisting_unacked_leaves_exactly_one_open_wake(
    state: TaskState,
) -> None:
    """AC2, first half: twelve passes over an hour past the timeout, the wake never
    acked, exactly one open wake for the task and cause."""
    clock = FakeClock(START)
    uow, t, pr = waiting_uow(clock, state=state)
    reason = (
        WakeReason.EXTERNAL_REVIEW_OVERDUE
        if state is TaskState.AWAITING_EXTERNAL_REVIEW
        else WakeReason.CI_CERTIFICATION_OVERDUE
    )

    clock.advance(30 * 60)
    assert repeat_overdue_wakes(uow.port, clock, task=t, pull_request=pr, policy=POLICY_1H) is False
    assert uow.wakes.open_for(t.id, reason) == []

    clock.advance(30 * 60)
    assert repeat_overdue_wakes(uow.port, clock, task=t, pull_request=pr, policy=POLICY_1H) is True
    raised = []
    for _ in range(12):
        clock.advance(5 * 60)
        raised.append(
            repeat_overdue_wakes(uow.port, clock, task=t, pull_request=pr, policy=POLICY_1H)
        )
    assert raised == [False] * 12
    open_wakes = uow.wakes.open_for(t.id, reason)
    assert len(open_wakes) == 1
    assert len(uow.wakes.rows) == 1
    assert len(uow.events.of_kind(EventKind.WAKE_CREATED)) == 1
    assert open_wakes[0].payload["summary"].startswith("#502 has waited more than 1h")


def test_after_an_ack_and_another_interval_the_overdue_wake_is_raised_again() -> None:
    """AC2, second half: the ack releases the collapse, the repeat waits a full interval
    after the ack, then one new wake is raised while the condition still holds."""
    clock = FakeClock(START)
    uow, t, pr = waiting_uow(clock)
    reason = WakeReason.EXTERNAL_REVIEW_OVERDUE
    clock.advance(3600)
    assert repeat_overdue_wakes(uow.port, clock, task=t, pull_request=pr, policy=POLICY_1H) is True
    first = uow.wakes.open_for(t.id, reason)[0]

    clock.advance(40 * 60)
    ack_wake(uow.port, clock, principal=principal(), wake_id=first.id, note="looked at it")
    acked_at = clock.now()
    assert uow.wakes.open_for(t.id, reason) == []

    clock.advance(30 * 60)
    assert repeat_overdue_wakes(uow.port, clock, task=t, pull_request=pr, policy=POLICY_1H) is False
    assert uow.wakes.open_for(t.id, reason) == []

    clock.advance(30 * 60)
    assert clock.now() - acked_at == timedelta(hours=1)
    assert repeat_overdue_wakes(uow.port, clock, task=t, pull_request=pr, policy=POLICY_1H) is True
    reopened = uow.wakes.open_for(t.id, reason)
    assert len(reopened) == 1
    assert reopened[0].id != first.id
    assert len(uow.wakes.rows) == 2

    clock.advance(5 * 60)
    assert repeat_overdue_wakes(uow.port, clock, task=t, pull_request=pr, policy=POLICY_1H) is False
    assert len(uow.wakes.rows) == 2


def test_overdue_wake_is_not_raised_once_the_condition_has_ended() -> None:
    """After the ack the repeat needs the condition to still hold: a task that left the
    waiting state raises nothing."""
    clock = FakeClock(START)
    uow, t, pr = waiting_uow(clock)
    clock.advance(3600)
    assert repeat_overdue_wakes(uow.port, clock, task=t, pull_request=pr, policy=POLICY_1H) is True
    first = uow.wakes.open_for(t.id, WakeReason.EXTERNAL_REVIEW_OVERDUE)[0]
    ack_wake(uow.port, clock, principal=principal(), wake_id=first.id, note="handled")
    t.state = TaskState.READY_FOR_MERGE
    clock.advance(2 * 3600)
    assert repeat_overdue_wakes(uow.port, clock, task=t, pull_request=pr, policy=POLICY_1H) is False
    assert len(uow.wakes.rows) == 1


def test_repeat_allowed_is_the_rule() -> None:
    """One open wake blocks; after every ack, a full interval from the latest ack."""
    interval = timedelta(hours=1)
    now = START

    def wake(created: datetime, acked: datetime | None) -> Wake:
        return Wake(
            id=created.isoformat(),
            principal_id=PRINCIPAL_ID,
            task_id=TASK_ID,
            reason="x",
            payload={},
            created_at=created,
            acked_at=acked,
        )

    assert repeat_allowed([], now=now, interval=interval) is True
    assert repeat_allowed([wake(now - interval * 5, None)], now=now, interval=interval) is False
    assert (
        repeat_allowed(
            [wake(now - interval * 5, now - interval * 2), wake(now - interval, None)],
            now=now,
            interval=interval,
        )
        is False
    )
    assert (
        repeat_allowed([wake(now - interval * 2, now - interval / 2)], now=now, interval=interval)
        is False
    )
    assert (
        repeat_allowed([wake(now - interval * 2, now - interval)], now=now, interval=interval)
        is True
    )


def test_stale_escalations_on_one_task_share_one_open_wake() -> None:
    """Two stale escalations on one task, four passes: one `escalation_stale` wake naming
    both, and `last_wake_at` moved on both so the pass does not re-read them as due."""
    clock = FakeClock(START)
    uow = FakeUnitOfWork()
    uow.principals.add(principal())
    t = task(TaskState.BLOCKED)
    uow.tasks.add(t)
    for i, age in enumerate((48, 25)):
        uow.escalations.add(
            Escalation(
                id=f"01ESC0000000000000000000{i}",
                task_id=t.id,
                attempt_id=f"01ATT0000000000000000000{i}",
                state=EscalationState.OPEN,
                question=f"question {i}",
                opened_at=START - timedelta(hours=age),
            )
        )

    assert repeat_stale_escalation_wakes(uow.port, clock, stale_hours=24) == 1
    for _ in range(3):
        clock.advance(20 * 60)
        assert repeat_stale_escalation_wakes(uow.port, clock, stale_hours=24) == 0
    open_wakes = uow.wakes.open_for(t.id, WakeReason.ESCALATION_STALE)
    assert len(open_wakes) == 1
    assert len(uow.wakes.rows) == 1
    summary = open_wakes[0].payload["summary"]
    assert "01ESC00000000000000000000" in summary
    assert "01ESC00000000000000000001" in summary
    assert "have been open since" in summary
    assert all(e.last_wake_at == START for e in uow.escalations.rows.values())


def test_stale_escalation_wake_returns_after_ack_and_another_interval() -> None:
    """AC2 for `escalation_stale`: the condition persists for well past the interval
    unacked, one wake; after the ack and a full interval, one more."""
    clock = FakeClock(START)
    uow = FakeUnitOfWork()
    uow.principals.add(principal())
    t = task(TaskState.BLOCKED)
    uow.tasks.add(t)
    uow.escalations.add(
        Escalation(
            id="01ESC00000000000000000000",
            task_id=t.id,
            attempt_id=None,
            state=EscalationState.OPEN,
            question="which reading?",
            opened_at=START - timedelta(hours=25),
        )
    )
    assert repeat_stale_escalation_wakes(uow.port, clock, stale_hours=24) == 1
    first = uow.wakes.open_for(t.id, WakeReason.ESCALATION_STALE)[0]
    assert first.payload["summary"].startswith("escalation 01ESC00000000000000000000 has been open")

    # Two days unacked, one pass an hour: still the one wake.
    for _ in range(48):
        clock.advance(3600)
        assert repeat_stale_escalation_wakes(uow.port, clock, stale_hours=24) == 0
    assert len(uow.wakes.rows) == 1

    ack_wake(uow.port, clock, principal=principal(), wake_id=first.id, note="asked Scott")
    clock.advance(23 * 3600)
    assert repeat_stale_escalation_wakes(uow.port, clock, stale_hours=24) == 0
    clock.advance(3600)
    assert repeat_stale_escalation_wakes(uow.port, clock, stale_hours=24) == 1
    reopened = uow.wakes.open_for(t.id, WakeReason.ESCALATION_STALE)
    assert len(reopened) == 1
    assert reopened[0].id != first.id


# ----- AC3: a wake about a finished pull request closes itself --------------------------


@pytest.mark.parametrize(
    ("final_state", "outcome"),
    [
        (PullRequestState.MERGED, "has merged"),
        (PullRequestState.CLOSED, "has closed without being merged"),
    ],
)
def test_pending_wake_about_a_finished_pull_request_is_acked_by_the_system(
    final_state: PullRequestState, outcome: str
) -> None:
    """AC3: the pass after the merge (or close) is observed acks the overdue wake with a
    reason, under the `crucible` principal; a wake about a still open pull request and an
    unrelated wake are left alone."""
    clock = FakeClock(START)
    uow, t, pr = waiting_uow(clock)
    clock.advance(3600)
    assert repeat_overdue_wakes(uow.port, clock, task=t, pull_request=pr, policy=POLICY_1H) is True
    overdue = uow.wakes.open_for(t.id, WakeReason.EXTERNAL_REVIEW_OVERDUE)[0]
    other = create_wake(
        uow.port,
        clock,
        principal_id=PRINCIPAL_ID,
        reason=WakeReason.MERGED,
        summary="merged",
        task=t,
    )

    # The pass that sees the pull request still open changes nothing.
    assert close_wakes_for_finished_pull_requests(uow.port, clock) == 0
    assert overdue.acked_at is None

    pr.state = final_state
    clock.advance(60)
    assert close_wakes_for_finished_pull_requests(uow.port, clock) == 1
    assert overdue.acked_at == clock.now()
    assert overdue.ack_note is not None
    assert "closed by Crucible" in overdue.ack_note
    assert f"pull request #502 {outcome}" in overdue.ack_note
    assert "external_review_overdue" in overdue.ack_note
    acks = uow.events.of_kind(EventKind.WAKE_ACKED)
    assert len(acks) == 1
    assert acks[0].principal == PRINCIPAL_CRUCIBLE
    assert acks[0].task_id == t.id
    assert acks[0].payload == {
        "wake_id": overdue.id,
        "note": overdue.ack_note,
        "acked_by": PRINCIPAL_CRUCIBLE,
    }
    assert other.acked_at is None
    assert uow.wakes.open_for(t.id, WakeReason.EXTERNAL_REVIEW_OVERDUE) == []

    # The next pass has nothing left to close, and the poll no longer shows the wake.
    assert close_wakes_for_finished_pull_requests(uow.port, clock) == 0
    pending = wake_list(
        uow.port, principal_id=PRINCIPAL_ID, since=None, include_acked=False, limit=50
    )
    assert [item.id for item in pending.items] == [other.id]


def test_system_ack_covers_ci_certification_overdue_and_skips_wakes_without_a_task() -> None:
    clock = FakeClock(START)
    uow, t, pr = waiting_uow(clock, state=TaskState.AWAITING_CI_CERTIFICATION)
    clock.advance(3600)
    assert repeat_overdue_wakes(uow.port, clock, task=t, pull_request=pr, policy=POLICY_1H) is True
    orphan = create_wake(
        uow.port,
        clock,
        principal_id=PRINCIPAL_ID,
        reason=WakeReason.CI_CERTIFICATION_OVERDUE,
        summary="no task",
    )
    pr.state = PullRequestState.MERGED
    assert close_wakes_for_finished_pull_requests(uow.port, clock) == 1
    assert uow.wakes.open_for(t.id, WakeReason.CI_CERTIFICATION_OVERDUE) == []
    assert orphan.acked_at is None
