"""Issue 489: the read-only Board is bounded, compact, and complete."""

from __future__ import annotations

import time
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from crucible.adapters.ui.pages import board as page
from crucible.application.admin.board_lanes import LANE_BY_STATE, LANES, board_lanes_view
from crucible.domain.entities import Escalation, EscalationState
from crucible.domain.lifecycle import TaskState
from tests.unit.admin_ui_fixtures import request
from tests.unit.test_board import NOW, Repo, fake_uow, row


class CountingTasks(Repo):
    def __init__(self, rows: list[Any], calls: Counter[str]) -> None:
        super().__init__(rows)
        self.calls = calls

    def count_by_state(self) -> dict[TaskState, int]:
        self.calls["counts"] += 1
        return Counter(item.state for item in self.rows)

    def list_in_states(self, states: list[Any]) -> list[Any]:
        self.calls["lanes"] += 1
        return super().list_in_states(states)


class Events(Repo):
    def latest_for_tasks_kinds(
        self, task_ids: list[str], kinds: tuple[str, ...]
    ) -> dict[tuple[str, str], Any]:
        wanted = set(task_ids)
        latest: dict[tuple[str, str], Any] = {}
        for item in self.rows:
            key = (item.task_id, item.kind)
            if (
                item.task_id in wanted
                and item.kind in kinds
                and (key not in latest or item.seq > latest[key].seq)
            ):
                latest[key] = item
        return latest


def task(number: int, state: TaskState) -> Any:
    return row(
        id=f"t{number}",
        external_id=f"FDY-{number:04d}",
        principal_id="operator",
        project="hades",
        title=f"Task {number}",
        state=state,
        contract_version=1,
        policy_name="default",
        policy_version=1,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(minutes=number + 1),
        closed_at=NOW - timedelta(minutes=number + 1)
        if state in {TaskState.CANCELLED, TaskState.REJECTED, TaskState.CLOSED}
        else None,
    )


def fixture(count: int = len(TaskState)) -> tuple[Any, Counter[str]]:
    calls: Counter[str] = Counter()
    states = list(TaskState)
    tasks = [task(index, states[index % len(states)]) for index in range(count)]
    uow = fake_uow()
    uow.tasks = CountingTasks(tasks, calls)
    uow.contracts = Repo(
        [
            row(
                task_id=item.id,
                version=1,
                document={
                    "repository": {"url": "https://github.com/Foundry-SH/hades"},
                    "deliverables": [{"kind": "pull_request", "closes": ["#489"]}],
                    "execution": {"tier": "frontier"},
                },
            )
            for item in tasks
        ]
    )
    uow.attempts = Repo()
    uow.pull_requests = Repo()
    uow.escalations = Repo()
    uow.events = Events()
    return uow, calls


def test_every_task_state_has_exactly_one_lane_in_the_designed_order() -> None:
    assert set(LANE_BY_STATE) == set(TaskState)
    assert [name for _key, name, _meaning in LANES] == [
        "Waiting on Scott",
        "Stuck",
        "In progress",
        "Holding pen",
        "Inbox",
        "Wins",
        "Graveyard",
    ]
    uow, _calls = fixture()
    cards = [card for lane in board_lanes_view(uow, NOW)["lanes"] for card in lane["cards"]]
    assert len(cards) == len(TaskState)
    assert len({card["id"] for card in cards}) == len(TaskState)


def test_card_fields_scott_question_and_graveyard_replacement() -> None:
    uow, _calls = fixture()
    blocked = next(item for item in uow.tasks.rows if item.state is TaskState.BLOCKED)
    cancelled = next(item for item in uow.tasks.rows if item.state is TaskState.CANCELLED)
    replacement = next(item for item in uow.tasks.rows if item.state is TaskState.RUNNING)
    uow.escalations = Repo(
        [
            row(
                task_id=blocked.id,
                opened_at=NOW - timedelta(minutes=3),
                kind="design_question",
                question="Which layout should we use? More context follows.",
            )
        ]
    )
    uow.events = Events(
        [
            row(
                seq=1,
                task_id=cancelled.id,
                kind="task_cancelled",
                payload={"reason": f"Superseded by {replacement.external_id}"},
                ts=NOW,
            )
        ]
    )
    lanes = {lane["key"]: lane for lane in board_lanes_view(uow, NOW)["lanes"]}
    scott = next(card for card in lanes["waiting_on_scott"]["cards"] if card["id"] == blocked.id)
    assert scott["waiting_on"] == "Which layout should we use?"
    assert scott["issues"][0]["url"].endswith("/issues/489")
    assert scott["tier"] == "frontier"
    assert {"external_id", "project", "title", "harness", "model", "age"} <= scott.keys()
    dead = next(card for card in lanes["graveyard"]["cards"] if card["id"] == cancelled.id)
    assert dead["reason"] == f"Superseded by {replacement.external_id}"
    assert dead["replacement"] == {
        "external_id": replacement.external_id,
        "task_id": replacement.id,
    }
    assert lanes["wins"]["collapsed"] and lanes["graveyard"]["collapsed"]


def _lanes(uow: Any) -> dict[str, dict[str, Any]]:
    return {lane["key"]: lane for lane in board_lanes_view(uow, NOW)["lanes"]}


def _event(seq: int, task_id: str, kind: str, ts: Any, **payload: Any) -> Any:
    return row(seq=seq, task_id=task_id, kind=kind, payload=payload, ts=ts)


def test_real_escalations_compare_by_opened_at() -> None:
    """The domain `Escalation` has `opened_at`, not `created_at`; the board must take the
    newest open one per task without a synthetic field."""
    uow, _calls = fixture()
    blocked = next(item for item in uow.tasks.rows if item.state is TaskState.BLOCKED)

    def escalation(number: int, opened_at: Any, question: str) -> Escalation:
        return Escalation(
            id=f"e{number}",
            task_id=blocked.id,
            attempt_id=None,
            state=EscalationState.OPEN,
            question=question,
            opened_at=opened_at,
            reason="ambiguous_contract",
        )

    uow.escalations = Repo(
        [
            escalation(2, NOW - timedelta(minutes=2), "Newer question? Detail."),
            escalation(1, NOW - timedelta(hours=1), "Older question? Detail."),
        ]
    )
    lanes = _lanes(uow)
    card = next(card for card in lanes["waiting_on_scott"]["cards"] if card["id"] == blocked.id)
    assert card["waiting_on"] == "Newer question?"
    assert card["age"]["entered_at"] == NOW - timedelta(minutes=2)


def test_closed_tasks_are_wins_not_graveyard() -> None:
    """`closed` is reached only from accepted, merged or released."""
    assert LANE_BY_STATE[TaskState.CLOSED] == "wins"
    uow, _calls = fixture()
    closed = next(item for item in uow.tasks.rows if item.state is TaskState.CLOSED)
    lanes = _lanes(uow)
    card = next(card for card in lanes["wins"]["cards"] if card["id"] == closed.id)
    assert card["reason"] is None and card["replacement"] is None
    assert closed.id not in {card["id"] for card in lanes["graveyard"]["cards"]}
    assert lanes["wins"]["count_all_time"] == sum(
        1 for item in uow.tasks.rows if LANE_BY_STATE[item.state] == "wins"
    )


def test_proposal_rejection_reason_and_replacement_are_shown() -> None:
    uow, _calls = fixture()
    rejected = next(item for item in uow.tasks.rows if item.state is TaskState.REJECTED)
    replacement = next(item for item in uow.tasks.rows if item.state is TaskState.SUBMITTED)
    reason = f"Duplicate of {replacement.external_id}"
    uow.events = Events(
        [_event(7, rejected.id, "task_proposal_rejected", NOW - timedelta(hours=3), reason=reason)]
    )
    card = next(card for card in _lanes(uow)["graveyard"]["cards"] if card["id"] == rejected.id)
    assert card["reason"] == reason
    assert card["replacement"] == {
        "external_id": replacement.external_id,
        "task_id": replacement.id,
    }
    assert card["age"]["entered_at"] == NOW - timedelta(hours=3)


def test_holding_pen_orders_capacity_held_launches_in_dispatch_order() -> None:
    """Two launches held for capacity: the one scheduled first dispatches first, even when
    it reached the capacity wait later than the other."""
    uow, _calls = fixture()
    first = next(item for item in uow.tasks.rows if item.state is TaskState.AWAITING_QUOTA)
    second = task(900, TaskState.AWAITING_QUOTA)
    submitted = next(item for item in uow.tasks.rows if item.state is TaskState.SUBMITTED)
    uow.tasks.rows.append(second)
    uow.events = Events(
        [
            _event(1, first.id, "task_scheduled", NOW - timedelta(hours=2)),
            _event(2, second.id, "task_scheduled", NOW - timedelta(hours=1)),
            _event(3, second.id, "task_awaiting_quota", NOW - timedelta(minutes=50)),
            _event(4, first.id, "task_awaiting_quota", NOW - timedelta(minutes=5)),
            _event(5, submitted.id, "task_submitted", NOW - timedelta(days=1)),
        ]
    )
    ordered = [card["id"] for card in _lanes(uow)["holding_pen"]["cards"]]
    assert ordered.index(first.id) < ordered.index(second.id) < ordered.index(submitted.id)


def test_query_render_time_and_html_size_bounds(monkeypatch: Any) -> None:
    uow, calls = fixture(500)
    started = time.perf_counter()
    document = board_lanes_view(uow, NOW)
    assert time.perf_counter() - started < 1
    assert calls == {"counts": 1, "lanes": 6}

    live = [
        card
        for lane in document["lanes"]
        if lane["key"] not in {"wins", "graveyard"}
        for card in lane["cards"]
    ][:60]
    lanes = []
    for lane in document["lanes"]:
        copy = {**lane, "cards": []}
        lanes.append(copy)
    lanes[2]["cards"] = live
    lanes[2]["count"] = len(live)

    monkeypatch.setattr(page, "_require", lambda *_args: (None, "csrf"))
    monkeypatch.setattr(page, "board_lanes_view", lambda *_args: {"lanes": lanes})
    ctx = SimpleNamespace(clock=SimpleNamespace(now=lambda: NOW))
    req = request("/ui/board")
    req.scope["query_string"] = b""
    response = page.board_page(req, ctx, uow)  # type: ignore[arg-type]
    assert response.status_code == 200
    assert len(response.body) < 150_000
    html = bytes(response.body).decode()
    assert html.index("Waiting on Scott") < html.index("Stuck") < html.index("In progress")
    assert "Tokens by harness" not in html and "Quality totals" not in html
    assert '<details class="board-lane" data-lane="wins">' in html
