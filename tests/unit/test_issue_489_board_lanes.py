"""Issue 489: the read-only Board is bounded, compact, and complete."""

from __future__ import annotations

import time
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from crucible.adapters.ui.pages import board as page
from crucible.application.admin.board_lanes import LANE_BY_STATE, LANES, board_lanes_view
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
                created_at=NOW,
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


def test_query_render_time_and_html_size_bounds(monkeypatch: Any) -> None:
    uow, calls = fixture(500)
    started = time.perf_counter()
    document = board_lanes_view(uow, NOW)
    assert time.perf_counter() - started < 1
    assert calls == {"counts": 1, "lanes": 5}

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
