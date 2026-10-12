"""FDY-0585: the role-aware board document."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from crucible.adapters.api.deps import app_context, unit_of_work
from crucible.adapters.ui import session
from crucible.adapters.ui.router import router
from crucible.application.admin import setup, status
from crucible.application.admin.board_lanes import LANE_BY_STATE
from crucible.application.board_resource import board_resource
from crucible.domain.entities import EscalationState, Principal, PullRequestState, Role
from crucible.domain.lifecycle import TaskState
from tests.unit.test_board import NOW, Repo, row
from tests.unit.test_issue_489_board_lanes import fixture, task
from tests.unit.test_ui_sessions import Sessions, context
from tools.smoke import compose_smoke


def principal(role: Role) -> Principal:
    return Principal(id=f"p-{role.value}", name=role.value, role=role, created_at=NOW)


def test_first_run_sign_in_renders_the_smoke_landing_and_empty_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow the smoke's explicit next=/ui through real sessions and templates."""
    ctx = context()
    ctx.admin = object()
    ctx.first_run = SimpleNamespace(discard=Mock())
    admin = Principal("first-admin", "first-run-admin", Role.ADMIN, NOW)
    uow, _calls = fixture(0)
    uow.ui_sessions = Sessions()
    uow.principals = SimpleNamespace(get=lambda key: admin if key == admin.id else None)
    uow.commit = Mock()
    monkeypatch.setattr(session, "authenticate", lambda _uow, _token: admin)
    # hades #169: a fresh deployment has every first-run step to do.
    monkeypatch.setattr(
        setup,
        "setup_steps",
        lambda _ctx, _uow: [
            {
                "number": 1,
                "key": "github_app",
                "label": "GitHub App",
                "link": "/ui/github",
                "done": False,
                "detail": "Create or connect the GitHub App.",
            }
        ],
    )
    monkeypatch.setattr(
        status,
        "status",
        AsyncMock(
            return_value={
                "readiness": {
                    "ready": False,
                    "ready_harnesses": [],
                    "harnesses": [],
                    "steps": [
                        {
                            "code": "no_repository",
                            "text": "Register a repository.",
                            "fix": "/ui/repositories",
                        }
                    ],
                },
                "supervisor": {"healthy": True, "last_tick_at": None},
                "tasks": {"lists": {}},
                "workers": [],
                "providers": [],
                "wakes": {"unacked": 0},
            }
        ),
    )
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(router)
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: uow
    preauth = session._preauth_serializer(ctx).dumps({"csrf": "preauth-csrf"})
    with TestClient(app) as client:
        client.cookies.set(session.PREAUTH_COOKIE, preauth, path="/ui/sign-in")
        landing = client.post(
            "/ui/sign-in",
            data={"csrf": "preauth-csrf", "token": "fixture-only", "next": "/ui"},
        )
        assert landing.status_code == 200
        # hades #169: /ui sends a deployment with a step to do to Set up.
        assert landing.url.path == "/ui/setup"
        assert [response.headers["location"] for response in landing.history] == [
            "/ui",
            "/ui/setup",
        ]
        assert compose_smoke.FIRST_RUN_LANDING_MARKER in landing.content
        assert "First-run steps" in landing.text
        assert "GitHub App" in landing.text
        ctx.first_run.discard.assert_called_once()

        board = client.get("/ui/board")
        assert board.status_code == 200
        assert compose_smoke.BOARD_MARKER in board.content
        assert board.text.count('class="board-lane"') == 7
        # hades #607: the empty Stuck lane shows its two owner groups, not "No cards."
        assert board.text.count("No cards.") == 4
        assert "Nothing stuck waits on you." in board.text
        assert "Nothing stuck waits on Foundry." in board.text
        assert 'class="board-card"' not in board.text


def test_board_document_has_approved_lanes_counts_and_local_times() -> None:
    uow, _calls = fixture(3)
    document = board_resource(uow, NOW, principal(Role.OPERATOR))
    assert [lane["key"] for lane in document["lanes"]] == [
        "inbox",
        "waiting_on_me",
        "stuck",
        "in_progress",
        "holding_pen",
        "wins",
        "graveyard",
    ]
    assert document["needs_me"] == document["counts"]["waiting_on_me"]
    assert not document["generated_at"].endswith(("Z", "+00:00"))


def test_observer_has_no_actions_and_collapsed_cards_load_on_demand() -> None:
    uow, _calls = fixture(3)
    document = board_resource(uow, NOW, principal(Role.OBSERVER), cards_for=frozenset({"wins"}))
    assert all(not lane["cards"] for lane in document["lanes"] if lane["key"] != "wins")
    assert all(not card["actions"] for lane in document["lanes"] for card in lane["cards"])


class CountingRepo(Repo):
    """Records every repository read the board makes, by method and task."""

    def __init__(self, rows: list[Any], calls: Counter[str]) -> None:
        super().__init__(rows)
        self.calls = calls

    def get(self, task_id: str, version: int) -> Any | None:
        self.calls[f"contract:{task_id}"] += 1
        return super().get(task_id, version)

    def list_for_task(self, task_id: str) -> list[Any]:
        self.calls["list_for_task"] += 1
        return super().list_for_task(task_id)

    def get_for_task(self, task_id: str) -> Any | None:
        self.calls["get_for_task"] += 1
        return None


def test_live_board_builds_no_collapsed_cards_and_keeps_their_counts() -> None:
    uow, calls = fixture(len(TaskState) * 2)
    uow.contracts = CountingRepo(uow.contracts.rows, calls)
    terminal = {
        task.id for task in uow.tasks.rows if LANE_BY_STATE[task.state] in {"wins", "graveyard"}
    }
    live = frozenset({"inbox", "waiting_on_me", "stuck", "in_progress", "holding_pen"})
    document = board_resource(uow, NOW, principal(Role.OPERATOR), cards_for=live)
    assert not any(calls[f"contract:{task_id}"] for task_id in terminal)
    graveyard_states = [s for s, lane in LANE_BY_STATE.items() if lane == "graveyard"]
    wins_states = [s for s, lane in LANE_BY_STATE.items() if lane == "wins"]
    rows = uow.tasks.rows
    assert document["counts"]["graveyard"] == sum(t.state in graveyard_states for t in rows)
    assert document["counts"]["wins"] == sum(t.state in wins_states for t in rows)
    full = board_resource(uow, NOW, principal(Role.OPERATOR))
    assert full["counts"] == document["counts"]
    wins = {lane["key"]: lane for lane in full["lanes"]}["wins"]
    assert {lane["key"]: lane for lane in document["lanes"]}["wins"]["count_today"] == (
        wins["count_today"]
    )


def test_actions_use_batched_records_without_per_card_reads() -> None:
    uow, calls = fixture(30)
    uow.escalations = CountingRepo([], calls)
    uow.pull_requests = CountingRepo([], calls)
    board_resource(uow, NOW, principal(Role.OPERATOR))
    assert calls["list_for_task"] == 0
    assert calls["get_for_task"] == 0


def test_waiting_on_me_card_with_pull_request_keeps_answer() -> None:
    uow, _calls = fixture(0)
    waiting = task(1, TaskState.EXTERNAL_FEEDBACK_RECEIVED)
    uow.tasks.rows = [waiting]
    uow.escalations = Repo(
        [
            row(
                id="e1",
                task_id=waiting.id,
                state=EscalationState.OPEN,
                opened_at=NOW - timedelta(minutes=3),
                reason="decision",
                question="Which layout should we use?",
            )
        ]
    )
    uow.pull_requests = Repo(
        [row(task_id=waiting.id, state=PullRequestState.OPEN, number=7, url="https://x/7")]
    )
    document = board_resource(uow, NOW, principal(Role.OPERATOR))
    lanes = {lane["key"]: lane for lane in document["lanes"]}
    (card,) = lanes["waiting_on_me"]["cards"]
    assert card["actions"][0]["key"] == "answer"
    assert len(card["actions"]) <= 2
