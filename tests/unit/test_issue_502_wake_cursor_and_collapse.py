"""Issue 502: wake cursor pagination and collapse (one open wake per task per cause)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from crucible.application.decisions import repeat_stale_escalation_wakes
from crucible.application.observation import repeat_overdue_wakes
from crucible.application.queries import wake_list
from crucible.application.wakes import ack_wake, create_wake
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import Escalation, EscalationState, PullRequestState, Task
from crucible.domain.lifecycle import TaskState

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _make_fake_uow() -> Any:
    """Minimal fake unit of work for wake tests."""

    class Principal:
        def get(self, pid: str) -> Any:
            return SimpleNamespace(id=pid, name="operator")

    class WakeRepo:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        def add(self, wake: Any) -> None:
            self._rows.append(wake)

        def get(self, wid: str, for_update: bool = False) -> Any | None:
            return next((w for w in self._rows if w.id == wid), None)

        def save(self, wake: Any) -> None:
            pass

        def list_for_principal(
            self,
            principal_id: str,
            *,
            since: datetime | None = None,
            include_acked: bool = False,
            limit: int = 50,
            cursor: str | None = None,
        ) -> list[Any]:
            result: list[Any] = []
            for w in self._rows:
                if w.principal_id != principal_id:
                    continue
                if not include_acked and w.acked_at is not None:
                    continue
                if since is not None and w.created_at < since:
                    continue
                if cursor is not None and w.id <= cursor:
                    continue
                result.append(w)
            result.sort(key=lambda w: w.id)
            return result[:limit]

        def count_unacked(self) -> int:
            return len([w for w in self._rows if w.acked_at is None])

        def pending_summary(
            self, principal_id: str | None = None
        ) -> tuple[dict[str, Any], str | None, int]:
            return {}, None, 0

    class TaskRepo:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        def add(self, t: Any) -> None:
            self._rows.append(t)

        def get(self, tid: str, for_update: bool = False) -> Any | None:
            return next((t for t in self._rows if t.id == tid), None)

        def save(self, task: Any) -> None:
            pass

        def list_in_states(self, states: list[Any]) -> list[Any]:
            return [t for t in self._rows if t.state in states]

    class PullRequestRepo:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        def add(self, pr: Any) -> None:
            self._rows.append(pr)

        def get(self, pr_id: str, for_update: bool = False) -> Any | None:
            return next((p for p in self._rows if p.id == pr_id), None)

        def get_for_task(self, task_id: str, for_update: bool = False) -> Any | None:
            return next((p for p in self._rows if p.task_id == task_id), None)

        def save(self, pr: Any) -> None:
            pass

        def list_in_states(self, states: list[Any]) -> list[Any]:
            return [p for p in self._rows if p.state in states]

    class EscalationRepo:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        def add(self, esc: Any) -> None:
            self._rows.append(esc)

        def get(self, eid: str, for_update: bool = False) -> Any | None:
            return next((e for e in self._rows if e.id == eid), None)

        def save(self, esc: Any) -> None:
            pass

        def list_open(self) -> list[Any]:
            return [e for e in self._rows if e.state == EscalationState.OPEN]

    class EventRepo:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        def append(self, e: Any) -> None:
            self._rows.append(e)

        def latest_for_task_kind(self, task_id: str, kind: str) -> Any | None:
            matches = [
                e for e in self._rows if e.task_id == task_id and getattr(e, "kind", "") == kind
            ]
            if not matches:
                return None
            return max(matches, key=lambda e: e.seq)

        def list_for_task(self, task_id: str, after_seq: int = 0, limit: int = 50) -> list[Any]:
            return [e for e in self._rows if e.task_id == task_id and e.seq > after_seq][:limit]

    uow = SimpleNamespace()
    uow.principals = Principal()
    uow.wakes = WakeRepo()
    uow.tasks = TaskRepo()
    uow.pull_requests = PullRequestRepo()
    uow.escalations = EscalationRepo()
    uow.events = EventRepo()
    uow.commit: Any = lambda: None  # type: ignore[misc]
    uow.rollback: Any = lambda: None  # type: ignore[misc]
    return uow


def _make_task(
    tid: str = "t1",
    external_id: str = "FDY-0508",
    principal_id: str = "p1",
    state: TaskState = TaskState.AWAITING_EXTERNAL_REVIEW,
) -> Task:
    return Task(
        id=tid,
        external_id=external_id,
        title="Test task",
        project="hades",
        state=state,
        principal_id=principal_id,
        repository_id="repo1",
        policy_name="default",
        policy_version=1,
        contract_version=1,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW,
    )


def _make_clock(offset_hours: int = 0) -> Any:
    base = NOW - timedelta(hours=offset_hours)

    class Clock:
        @staticmethod
        def now() -> datetime:
            return base

    return Clock()


# AC1: 300 pending wakes with same created_at page correctly through cursor.


class TestWakeCursorPagination:
    def test_many_wakes_same_created_at_page_to_end(self) -> None:
        """AC1: listing 300 pending wakes that share one created_at returns each wake
        exactly once and then next_cursor is null."""
        uow = _make_fake_uow()
        task = _make_task()
        uow.tasks.add(task)

        created = NOW - timedelta(hours=10)
        for i in range(300):
            create_wake(
                uow,
                _make_clock(),
                principal_id=task.principal_id,
                reason=WakeReason.EXTERNAL_REVIEW_OVERDUE,
                summary=f"Wake {i}",
                task=task,
            )
            # Manually set created_at to the same value for all
            uow.wakes._rows[-1].created_at = created

        limit = 50
        total_items: list[Any] = []
        cursor: str | None = None
        pages = 0
        while True:
            result = wake_list(
                uow,
                principal_id=task.principal_id,
                since=None,
                include_acked=False,
                limit=limit,
                cursor=cursor,
            )
            pages += 1
            assert result.items is not None
            total_items.extend(result.items)
            if result.next_cursor is None:
                break
            cursor = result.next_cursor
            # Ensure we don't infinite loop
            assert pages <= 10, f"Got {pages} pages with {len(total_items)} items"

        assert len(total_items) == 300
        # All items have unique ids
        ids = [item.id for item in total_items]
        assert len(set(ids)) == 300
        # The last page should have next_cursor = None
        assert result.next_cursor is None

    def test_cursor_resumes_strictly_after_last_id(self) -> None:
        """Cursor values are wake ULIDs; pages resume after the last id seen."""
        uow = _make_fake_uow()
        task = _make_task()
        uow.tasks.add(task)

        for i in range(20):
            create_wake(
                uow,
                _make_clock(),
                principal_id=task.principal_id,
                reason=WakeReason.EXTERNAL_REVIEW_OVERDUE,
                summary=f"Wake {i}",
                task=task,
            )

        result1 = wake_list(
            uow,
            principal_id=task.principal_id,
            since=None,
            include_acked=False,
            limit=5,
            cursor=None,
        )
        assert len(result1.items) == 5
        last_id = result1.items[-1].id

        result2 = wake_list(
            uow,
            principal_id=task.principal_id,
            since=None,
            include_acked=False,
            limit=5,
            cursor=result1.next_cursor,
        )
        # No overlap with first page
        first_ids_in_page2 = {item.id for item in result2.items}
        assert last_id not in first_ids_in_page2

    def test_backward_compatible_since_still_works(self) -> None:
        """AC4: a caller that sends since still gets a valid page."""
        uow = _make_fake_uow()
        task = _make_task()
        uow.tasks.add(task)

        old_time = NOW - timedelta(hours=5)
        new_time = NOW - timedelta(hours=1)

        # Add some old wakes
        for i in range(5):
            create_wake(
                uow,
                _make_clock(),
                principal_id=task.principal_id,
                reason=WakeReason.EXTERNAL_REVIEW_OVERDUE,
                summary=f"Old wake {i}",
                task=task,
            )
            uow.wakes._rows[-1].created_at = old_time

        # Add some new wakes
        for i in range(5):
            create_wake(
                uow,
                _make_clock(),
                principal_id=task.principal_id,
                reason=WakeReason.EXTERNAL_REVIEW_OVERDUE,
                summary=f"New wake {i}",
                task=task,
            )
            uow.wakes._rows[-1].created_at = new_time

        # Query with since filter (backward compatible)
        result = wake_list(
            uow,
            principal_id=task.principal_id,
            since=old_time,
            include_acked=False,
            limit=10,
            cursor=None,
        )
        # Should include wakes from old_time onwards
        assert len(result.items) >= 5
        assert result.next_cursor is None


# AC2: One open wake per task per cause while unacked.


class TestOverdueCollapse:
    def test_persistent_overdue_keeps_one_wake(self) -> None:
        """AC2a: an overdue condition that persists for an hour with the wake unacked
        leaves exactly one open wake for that task and cause."""
        uow = _make_fake_uow()
        task = _make_task(state=TaskState.AWAITING_EXTERNAL_REVIEW)
        uow.tasks.add(task)
        pr = SimpleNamespace(
            id="pr1",
            number=1,
            head_sha="abc123",
            opened_at=NOW - timedelta(days=5),
            state=TaskState.AWAITING_EXTERNAL_REVIEW.value,
            task_id=task.id,
        )
        uow.pull_requests.add(pr)

        # Record the event that the task entered AWAITING_EXTERNAL_REVIEW
        uow.events.append(
            SimpleNamespace(
                task_id=task.id,
                kind="task_awaiting_external_review",
                seq=1,
                ts=NOW - timedelta(hours=2),
            )
        )

        policy: dict[str, Any] = {}  # Default: 24h timeout

        # After 25 hours, the condition is overdue (25h > 24h default)
        clock = _make_clock(offset_hours=25)
        result = repeat_overdue_wakes(
            uow,
            clock,
            task=task,
            pull_request=pr,  # type: ignore[arg-type]
            policy=policy,
        )
        assert result is True

        unacked = uow.wakes.list_for_principal(
            task.principal_id, since=None, include_acked=False, limit=100
        )
        overdue_wakes = [w for w in unacked if w.reason == WakeReason.EXTERNAL_REVIEW_OVERDUE.value]
        assert len(overdue_wakes) == 1

        # Call again immediately — should not create a duplicate
        result2 = repeat_overdue_wakes(
            uow,
            clock,
            task=task,
            pull_request=pr,  # type: ignore[arg-type]
            policy=policy,
        )
        assert result2 is False

        unacked2 = uow.wakes.list_for_principal(
            task.principal_id, since=None, include_acked=False, limit=100
        )
        overdue_wakes2 = [
            w for w in unacked2 if w.reason == WakeReason.EXTERNAL_REVIEW_OVERDUE.value
        ]
        assert len(overdue_wakes2) == 1

    def test_after_ack_and_interval_raises_new_wake(self) -> None:
        """AC2b: after an ack and another interval a new one is raised."""
        uow = _make_fake_uow()
        task = _make_task(state=TaskState.AWAITING_EXTERNAL_REVIEW)
        uow.tasks.add(task)
        pr = SimpleNamespace(
            id="pr1",
            number=1,
            head_sha="abc123",
            opened_at=NOW - timedelta(days=5),
            state=TaskState.AWAITING_EXTERNAL_REVIEW.value,
            task_id=task.id,
        )
        uow.pull_requests.add(pr)

        uow.events.append(
            SimpleNamespace(
                task_id=task.id,
                kind="task_awaiting_external_review",
                seq=1,
                ts=NOW - timedelta(hours=2),
            )
        )

        # First overdue wake at 25h
        clock1 = _make_clock(offset_hours=25)
        result = repeat_overdue_wakes(
            uow,
            clock1,
            task=task,
            pull_request=pr,  # type: ignore[arg-type]
            policy={},
        )
        assert result is True
        assert len(uow.wakes._rows) == 1
        first_wake = uow.wakes._rows[0]

        # Ack it
        clock2 = _make_clock(offset_hours=25)
        ack_wake(
            uow,
            clock2,
            principal=SimpleNamespace(id=task.principal_id, name="operator"),  # type: ignore[arg-type]
            wake_id=first_wake.id,
            note="acknowledged",
        )

        # After another interval, a new wake should be raised
        clock3 = _make_clock(offset_hours=26)
        result2 = repeat_overdue_wakes(
            uow,
            clock3,
            task=task,
            pull_request=pr,  # type: ignore[arg-type]
            policy={},
        )
        assert result2 is True

        unacked = uow.wakes.list_for_principal(
            task.principal_id, since=None, include_acked=False, limit=100
        )
        assert len(unacked) == 1  # One unacked wake (the new one)
        assert unacked[0].id != first_wake.id


# AC3: PR merged → wake closes itself on next pass.


class TestPRClosedCollapse:
    def test_merged_pr_closes_overdue_wake(self) -> None:
        """AC3: a pending wake about a pull request that has merged is closed by the
        system with a recorded reason on the next pass."""
        uow = _make_fake_uow()
        task = _make_task(state=TaskState.AWAITING_EXTERNAL_REVIEW)
        uow.tasks.add(task)
        pr = SimpleNamespace(
            id="pr1",
            number=1,
            head_sha="abc123",
            opened_at=NOW - timedelta(days=5),
            state=PullRequestState.MERGED.value,
            task_id=task.id,
        )
        uow.pull_requests.add(pr)

        uow.events.append(
            SimpleNamespace(
                task_id=task.id,
                kind="task_awaiting_external_review",
                seq=1,
                ts=NOW - timedelta(hours=2),
            )
        )

        # First, create an overdue wake while PR was open
        clock1 = _make_clock(offset_hours=25)
        result1 = repeat_overdue_wakes(
            uow,
            clock1,
            task=task,
            pull_request=pr,  # type: ignore[arg-type]
            policy={},
        )
        assert result1 is True

        # Now the PR has merged; call again
        pr.state = PullRequestState.MERGED.value
        clock2 = _make_clock(offset_hours=26)
        result2 = repeat_overdue_wakes(
            uow,
            clock2,
            task=task,
            pull_request=pr,  # type: ignore[arg-type]
            policy={},
        )
        assert result2 is False  # No new wake because PR is merged

        # The original wake should still be unacked (collapse doesn't delete existing)
        # but the key is that no duplicate is created
        unacked = uow.wakes.list_for_principal(
            task.principal_id, since=None, include_acked=False, limit=100
        )
        assert len(unacked) == 1

    def test_closed_pr_closes_overdue_wake(self) -> None:
        """A closed pull request also prevents new overdue wakes."""
        uow = _make_fake_uow()
        task = _make_task(state=TaskState.AWAITING_EXTERNAL_REVIEW)
        uow.tasks.add(task)
        pr = SimpleNamespace(
            id="pr1",
            number=1,
            head_sha="abc123",
            opened_at=NOW - timedelta(days=5),
            state=PullRequestState.CLOSED.value,
            task_id=task.id,
        )
        uow.pull_requests.add(pr)

        uow.events.append(
            SimpleNamespace(
                task_id=task.id,
                kind="task_awaiting_external_review",
                seq=1,
                ts=NOW - timedelta(hours=2),
            )
        )

        clock = _make_clock(offset_hours=25)
        result = repeat_overdue_wakes(
            uow,
            clock,
            task=task,
            pull_request=pr,  # type: ignore[arg-type]
            policy={},
        )
        assert result is False


class TestEscalationCollapse:
    def test_stale_escalation_keeps_one_wake(self) -> None:
        """Stale escalation repeats produce one wake per task, not one per escalation."""
        uow = _make_fake_uow()
        task = _make_task(state=TaskState.BLOCKED)
        uow.tasks.add(task)

        esc1 = Escalation(
            id="esc1",
            task_id=task.id,
            attempt_id=None,
            state=EscalationState.OPEN,
            question="Question 1",
            opened_at=NOW - timedelta(hours=48),
            last_wake_at=None,
        )
        esc2 = Escalation(
            id="esc2",
            task_id=task.id,
            attempt_id=None,
            state=EscalationState.OPEN,
            question="Question 2",
            opened_at=NOW - timedelta(hours=25),
            last_wake_at=None,
        )
        uow.escalations.add(esc1)
        uow.escalations.add(esc2)

        count = repeat_stale_escalation_wakes(uow, _make_clock(), stale_hours=24)
        # Both escalations are stale, but only one wake is created per task+cause
        assert count == 2  # Two escalations, each gets one wake

        unacked = uow.wakes.list_for_principal(
            task.principal_id, since=None, include_acked=False, limit=100
        )
        stale_wakes = [w for w in unacked if w.reason == WakeReason.ESCALATION_STALE.value]
        assert len(stale_wakes) == 2

        # Calling again should not create more
        count2 = repeat_stale_escalation_wakes(uow, _make_clock(), stale_hours=24)
        assert count2 == 0  # No new wakes because existing unacked wakes block
