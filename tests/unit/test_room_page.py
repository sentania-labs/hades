"""FDY-0594: the server-rendered principal room surface."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from fastapi.responses import HTMLResponse

from crucible.adapters.ui.pages import room as page
from crucible.contracts.rooms import RoomDetail, RoomTurnView, RoomView
from crucible.domain.entities import Principal, Role
from crucible.domain.rooms import RoomKind, RoomState, TurnRole

if TYPE_CHECKING:
    import pytest

NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)
OPERATOR = Principal("01OPERROOMPAGE00000000001", "scott", Role.OPERATOR, NOW)
OBSERVER = Principal("01OBSVROOMPAGE00000000001", "reader", Role.OBSERVER, NOW)
ROOM_ID = "01ROOMPAGE000000000000001"


class FakeRoomsApi:
    """The room-facing repository boundary used by the rendered page."""

    def __init__(self) -> None:
        self.room = SimpleNamespace(
            id=ROOM_ID,
            kind=RoomKind.PRINCIPAL,
            created_by=OPERATOR.id,
            state=RoomState.WARM,
            harness="claude_code",
            model="claude-opus-5-5",
            last_activity_at=NOW - timedelta(minutes=12),
        )

    def list_recent(self, *, limit: int, include_closed: bool) -> list[Any]:
        assert limit == 500 and not include_closed
        return [self.room]


def _detail() -> RoomDetail:
    room = RoomView(
        id=ROOM_ID,
        kind=RoomKind.PRINCIPAL,
        card_task_id=None,
        harness="claude_code",
        model="claude-opus-5-5",
        state=RoomState.WARM,
        created_at=NOW - timedelta(hours=1),
        last_activity_at=NOW - timedelta(minutes=12),
        runner_handle="runner",
        session_id="session",
        created_by=OPERATOR.id,
        scope_task_ids=[],
    )
    turns = [
        RoomTurnView(
            id="01TURNROOMPAGE000000000001",
            room_id=ROOM_ID,
            seq=1,
            role=TurnRole.SYSTEM,
            text="Switched to claude_code (claude-opus-5-5). Same history, same memory.",
            tool_calls=[],
            started_at=NOW - timedelta(minutes=14),
            ended_at=NOW - timedelta(minutes=14),
            interrupted=False,
            decision_id=None,
        ),
        RoomTurnView(
            id="01TURNROOMPAGE000000000002",
            room_id=ROOM_ID,
            seq=2,
            role=TurnRole.ASSISTANT,
            text="I will keep the restart sequence explicit.",
            tool_calls=[{"name": "hades_recall", "input": {"subject": "Coppermind restarts"}}],
            started_at=NOW - timedelta(minutes=13),
            ended_at=NOW - timedelta(minutes=12),
            interrupted=False,
            decision_id="01DECROOMPAGE000000000001",
        ),
    ]
    return RoomDetail(room=room, turns=turns, turns_total=72, window=50)


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        scope={"type": "http", "method": "GET", "path": "/ui/room", "headers": []},
        query_params={},
        app=SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace())),
    )


def _ctx() -> Any:
    return SimpleNamespace(
        clock=SimpleNamespace(now=lambda: NOW),
        settings=SimpleNamespace(
            service=SimpleNamespace(render_timezone="America/Chicago"),
            rooms=SimpleNamespace(
                default_harness="claude_code",
                default_model="claude-opus-5-5",
                models=["claude-opus-5-5"],
            ),
        ),
    )


def _html(monkeypatch: pytest.MonkeyPatch, principal: Principal) -> str:
    monkeypatch.setattr(page, "_require", lambda request, ctx, uow: (principal, "csrf"))
    monkeypatch.setattr(page, "room_detail", lambda uow, room_id, window: _detail())
    response = page.room_page(
        _request(),  # type: ignore[arg-type]
        _ctx(),
        SimpleNamespace(rooms=FakeRoomsApi()),
        window=50,
    )
    assert isinstance(response, HTMLResponse)
    return cast(bytes, response.body).decode()


def test_room_page_renders_transcript_system_tools_selector_and_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _html(monkeypatch, OPERATOR)

    assert "room-turn--system" in html and "Same history, same memory" in html
    assert "read memory: Coppermind restarts" in html
    assert "room-system-line" in html and "Decision recorded at" in html
    assert "01DECROOMPAGE000000000001" in html
    assert "History · load 50 older turns" in html
    assert "Claude · Opus 5.5" in html
    assert "Connected: Claude (Opus 5.5), warm 12 min" in html


def test_room_page_serves_stream_script_and_observer_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = _html(monkeypatch, OPERATOR)
    assert "new EventSource" in operator
    assert "/stream?after_seq=" in operator
    assert "room-interrupt" in operator and "room-composer" in operator

    observer = _html(monkeypatch, OBSERVER)
    assert "Observer access is read-only" in observer
    assert "room-composer" not in observer and "new EventSource" not in observer
