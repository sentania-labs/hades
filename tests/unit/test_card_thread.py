"""FDY-0595: card rooms share the principal panel, with card-local history."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from crucible.adapters.api.deps import app_context, unit_of_work
from crucible.adapters.api.routers import tasks as tasks_api
from crucible.adapters.persistence.records import Rooms
from crucible.adapters.ui.pages import board, room, tasks
from crucible.application.queries import task_view
from crucible.application.rooms import _card_context, session_start, switch_room
from crucible.contracts.api import TaskView
from crucible.domain.entities import LedgerDecision
from crucible.domain.lifecycle import TaskState
from crucible.domain.rooms import RoomKind, RoomState, RoomTurn, TurnRole
from crucible.settings import ServiceSettings, Settings
from tests.fixtures import FakeClock
from tests.unit.test_issue_360_ready_for_merge_correction import NOW
from tests.unit.test_issue_489_card_actions import (
    HEAD,
    OBSERVER,
    OPERATOR,
    TASK_ID,
    CardStore,
    store_for,
    stuck_fixture,
)
from tests.unit.test_rooms import _Memory

TEMPLATES = Path("crucible/adapters/ui/templates")


def client_for(
    store: CardStore, monkeypatch: pytest.MonkeyPatch, *, observer: bool = False
) -> TestClient:
    """The real page and UI room routes over fake rooms, tasks and decisions APIs."""
    principal = OBSERVER if observer else OPERATOR
    ctx = SimpleNamespace(
        clock=FakeClock(NOW),
        settings=Settings(service=ServiceSettings(render_timezone="America/Chicago")),
        database_url="postgresql+psycopg://fake/hades",
        providers=[],
    )
    ctx.settings.rooms.models = ["claude-opus-5-5", "claude-sonnet-4-6"]
    app = FastAPI()
    app.state.ctx = ctx
    for router in (board.router, tasks.router, room.router):
        app.include_router(router)
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: store
    for module in (board, room):
        monkeypatch.setattr(module, "_require", lambda *args: (principal, "csrf"))
    return TestClient(app, follow_redirects=False)


def test_first_use_creates_one_card_room_and_reuses_shared_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = stuck_fixture()
    with client_for(store, monkeypatch) as client:
        first = client.get(f"/ui/tasks/{TASK_ID}")
        second = client.get(f"/ui/board/{TASK_ID}")
    assert first.status_code == second.status_code == 200
    assert len(store.rooms.rows) == 1
    created = next(iter(store.rooms.rows.values()))
    assert created.kind is RoomKind.CARD and created.card_task_id == TASK_ID
    assert created.harness == "claude_code" and created.model == "claude-opus-5-5"
    assert created.scope_task_ids == [TASK_ID]
    html = first.text
    assert f'data-room-id="{created.id}"' in html
    assert "Project default: claude_code (claude-opus-5-5)" in html
    assert "room-composer" in html and "room-interrupt" in html and "new EventSource" in html
    assert "room-turn--system" in html and "Second line, kept as typed." in html
    thread = html.split('aria-label="Thread"', 1)[1].split("<aside", 1)[0]
    assert "Save note" not in thread and "data-action=" not in thread
    assert "Clicks live here, beside the thread, never in it." in html
    assert '{% include "room_panel.html" %}' in (TEMPLATES / "card.html").read_text()
    assert '{% include "room_panel.html" %}' in (TEMPLATES / "room.html").read_text()


def test_observer_never_creates_a_room_and_can_read_notes_and_existing_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = stuck_fixture()
    with client_for(store, monkeypatch, observer=True) as client:
        empty = client.get(f"/ui/tasks/{TASK_ID}").text
    assert not store.rooms.rows
    assert "There is no card room to observe yet" in empty
    assert "Second line, kept as typed." in empty
    with client_for(store, monkeypatch) as client:
        client.get(f"/ui/tasks/{TASK_ID}")
    with client_for(store, monkeypatch, observer=True) as client:
        html = client.get(f"/ui/tasks/{TASK_ID}").text
        room_id = next(iter(store.rooms.rows))
        for action in ("messages", "interrupt", "switch"):
            response = client.post(
                f"/ui/room/{room_id}/{action}", json={"text": "x"}, headers={"X-CSRF-Token": "csrf"}
            )
            assert response.status_code == 403
    assert 'id="room-composer"' not in html and "new EventSource" not in html
    assert 'id="room-target" disabled' in html
    assert len(store.rooms.rows) == 1


def test_history_matches_card_ids_only_and_uses_local_time(monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_for(TaskState.SUBMITTED)
    for index, (words, applies) in enumerate(
        [("Ship this card", [TASK_ID]), ("Keep the tests", ["EX-0001"]), ("Unrelated", ["other"])]
    ):
        store.decision_ledger.add(
            LedgerDecision(
                id=f"decision-{index}",
                verbatim=words,
                channel="principal-room",
                principal="scott",
                said_at=NOW,
                transcript_ref="room:test#1",
                applies_to=applies,
            )
        )
    with client_for(store, monkeypatch) as client:
        html = client.get(f"/ui/tasks/{TASK_ID}").text
    history = html.split('aria-label="History"', 1)[1]
    assert "Ship this card" in history and "Keep the tests" in history
    assert "Unrelated" not in history and "principal-room" in history
    assert "CDT" in history and NOW.isoformat() not in history


def test_card_history_window_and_switch_stay_in_its_room(monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_for(TaskState.SUBMITTED)
    with client_for(store, monkeypatch) as client:
        client.get(f"/ui/tasks/{TASK_ID}")
        card_room = next(iter(store.rooms.rows.values()))
        for seq in range(1, 73):
            store.room_turns.add(
                RoomTurn(
                    id=f"turn-{seq}",
                    room_id=card_room.id,
                    seq=seq,
                    role=TurnRole.USER,
                    text=f"words {seq}",
                    started_at=NOW,
                    ended_at=NOW,
                )
            )
        html = client.get(f"/ui/tasks/{TASK_ID}").text
        assert f'href="/ui/tasks/{TASK_ID}?window=100"' in html
        assert 'data-seq="1"' not in html
        older = client.get(f"/ui/tasks/{TASK_ID}?window=100").text
        assert 'data-seq="1"' in older
        switch_room(
            store.uow(),
            FakeClock(NOW),
            principal=OPERATOR,
            room_id=card_room.id,
            harness="claude_code",
            model="claude-sonnet-4-6",
        )
        switched = client.get(f"/ui/tasks/{TASK_ID}").text
        assert "Project default:" not in switched
        assert "Switched to claude_code (claude-sonnet-4-6)" in switched
        assert 'value="claude_code|claude-sonnet-4-6" selected' in switched
        assert client.get(f"/ui/tasks/{TASK_ID}?window=bad").status_code == 200


@pytest.mark.parametrize("questions", [[], [{"id": "question-1", "text": "Which branch?"}]])
def test_missing_questions_api_leaves_answer_path_out(
    monkeypatch: pytest.MonkeyPatch, questions: list[dict[str, str]]
) -> None:
    # FDY-0586 is absent on origin/main. Even an extra upstream field must not
    # expose an Answer button backed by an endpoint that does not exist.
    assert "questions" not in TaskView.model_fields
    assert not any("questions" in getattr(route, "path", "") for route in tasks_api.router.routes)
    monkeypatch.setattr(
        board,
        "task_view",
        lambda uow, task_id: task_view(uow, task_id).model_copy(update={"questions": questions}),
    )
    with client_for(store_for(TaskState.SUBMITTED), monkeypatch) as client:
        html = client.get(f"/ui/tasks/{TASK_ID}").text
    assert "Answer and resume" not in html
    assert "Which branch?" not in html
    assert 'id="room-composer"' in html


def test_card_session_start_has_contract_delivery_and_attempt_transcript_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = stuck_fixture()
    with client_for(store, monkeypatch) as client:
        client.get(f"/ui/tasks/{TASK_ID}")
    card_room = next(iter(store.rooms.rows.values()))
    # Session assembly uses the fake memory API as well as the card repositories.
    uow: Any = store
    uow.memory = _Memory()
    start = session_start(uow, card_room, identity="You are Hades.")
    for words in (
        "Return 409 on duplicate import ID",
        "Objective:",
        "Acceptance criteria:",
        "AC1:",
        "/pull/489 (open)",
        HEAD,
        "unit failed",
        "Attempt transcript references:",
        "/v1/attempts/01ATTEMPT48900000000000002/logs",
        "Second line, kept as typed.",
    ):
        assert words in start.card
    assert start.card in start.text
    card_room.kind = RoomKind.PRINCIPAL
    card_room.card_task_id = None
    assert _card_context(uow, card_room) is None


def test_card_layout_has_a_390px_single_column_and_local_composer() -> None:
    css = Path("crucible/adapters/ui/static/neon.css").read_text()
    assert ".card-conversation { grid-template-columns: minmax(0, 1fr); }" in css
    assert "@media (max-width: 700px)" in css
    assert ".card-thread .room-composer { position: static;" in css
    assert ".card-thread .room-turn { box-sizing: border-box; min-width: 0; }" in css


def test_card_uses_the_stream_script_with_inject_interrupt_and_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with client_for(store_for(TaskState.SUBMITTED), monkeypatch) as client:
        html = client.get(f"/ui/tasks/{TASK_ID}").text
    script = html.split("<script>\n(() => {", 1)[1].split("</script>", 1)[0]
    fixture = Path("tests/unit/room_script_fixture.cjs").read_text()
    result = subprocess.run(
        ["node", "-e", fixture],
        input=json.dumps("(() => {" + script),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_closed_card_room_keeps_history_without_a_new_composer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_for(TaskState.SUBMITTED)
    with client_for(store, monkeypatch) as client:
        client.get(f"/ui/tasks/{TASK_ID}")
        next(iter(store.rooms.rows.values())).state = RoomState.CLOSED
        html = client.get(f"/ui/tasks/{TASK_ID}").text
    assert len(store.rooms.rows) == 1 and 'id="room-composer"' not in html


def test_card_room_repository_filters_before_limit() -> None:
    session = Mock()
    session.execute.return_value.scalars.return_value = []
    Rooms(session).list_recent(limit=1, kind=RoomKind.CARD, card_task_id=TASK_ID)
    statement = session.execute.call_args.args[0]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "rooms.kind = 'card'" in sql
    assert f"rooms.card_task_id = '{TASK_ID}'" in sql
    assert sql.index("WHERE") < sql.index("LIMIT 1")


def test_card_composer_rejects_forged_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_for(TaskState.SUBMITTED)
    with client_for(store, monkeypatch) as client:
        client.get(f"/ui/tasks/{TASK_ID}")
        room_id = next(iter(store.rooms.rows))
        response = client.post(f"/ui/room/{room_id}/messages", json={"text": "forged"})
    assert response.status_code == 403
    assert not store.room_turns.rows
