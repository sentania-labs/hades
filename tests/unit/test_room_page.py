"""FDY-0594: the server-rendered principal room surface."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

from fastapi.responses import HTMLResponse

from crucible.adapters.persistence.records import Rooms
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

        self.rows = [self.room]

    def get(self, room_id: str) -> Any:
        return next((r for r in self.rows if r.id == room_id), None)

    def list_recent(
        self,
        *,
        limit: int,
        include_closed: bool,
        kind: RoomKind | None = None,
        created_by: str | None = None,
    ) -> list[Any]:
        return sorted(
            [
                r
                for r in self.rows
                if (include_closed or r.state is not RoomState.CLOSED)
                and (kind is None or r.kind is kind)
                and (created_by is None or r.created_by == created_by)
            ],
            key=lambda r: r.last_activity_at,
            reverse=True,
        )[:limit]


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


def _html(
    monkeypatch: pytest.MonkeyPatch, principal: Principal, detail: RoomDetail | None = None
) -> str:
    monkeypatch.setattr(page, "_require", lambda request, ctx, uow: (principal, "csrf"))
    monkeypatch.setattr(page, "room_detail", lambda uow, room_id, window: detail or _detail())
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
    assert "You are signed in as an observer; the principal room is read-only" in observer
    assert "room-composer" not in observer and "new EventSource" not in observer


def test_history_stops_at_window_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    detail = _detail().model_copy(update={"window": 500, "turns_total": 600})
    html = _html(monkeypatch, OPERATOR, detail)
    assert "showing the latest 500 turns (page limit)" in html
    assert 'href="/ui/room?window=' not in html
    complete = _detail().model_copy(update={"turns_total": 2})
    assert "History · start of room" in _html(monkeypatch, OPERATOR, complete)


def test_principal_lookup_ignores_newer_unrelated_rooms() -> None:
    rooms = FakeRoomsApi()
    for i in range(501):
        rooms.rows.append(
            SimpleNamespace(
                **{
                    **vars(rooms.room),
                    "id": f"card-{i}",
                    "kind": RoomKind.CARD,
                    "last_activity_at": NOW,
                }
            )
        )
        rooms.rows.append(
            SimpleNamespace(
                **{
                    **vars(rooms.room),
                    "id": f"other-{i}",
                    "created_by": OBSERVER.id,
                    "last_activity_at": NOW,
                }
            )
        )
    uow = SimpleNamespace(rooms=rooms)
    assert page._principal_room(uow, OPERATOR) is rooms.room
    assert page._principal_room(uow, OPERATOR, "other-0") is rooms.room
    assert page._principal_room(uow, OPERATOR, "card-0") is rooms.room
    assert page._principal_room(uow, OPERATOR, ROOM_ID) is rooms.room
    rooms.room.state = RoomState.CLOSED
    assert page._principal_room(uow, OPERATOR, ROOM_ID) is None
    observed = page._principal_room(uow, OBSERVER)
    assert observed is not None and observed.kind is RoomKind.PRINCIPAL


def test_room_repository_filters_before_limit() -> None:
    session = Mock()
    session.execute.return_value.scalars.return_value = []
    Rooms(session).list_recent(
        limit=1, include_closed=False, kind=RoomKind.PRINCIPAL, created_by=OPERATOR.id
    )
    statement = session.execute.call_args.args[0]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "rooms.kind = 'principal'" in sql
    assert f"rooms.created_by = '{OPERATOR.id}'" in sql
    assert "rooms.state != 'closed'" in sql
    assert sql.index("WHERE") < sql.index("LIMIT 1")


def test_served_script_message_failures_and_interrupt_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _html(monkeypatch, OPERATOR)
    script = html.split("const shell = document.querySelector('.room-shell');", 1)[1]
    script = (
        "(() => { const shell = document.querySelector('.room-shell');"
        + script.split("</script>", 1)[0]
    )
    fixture = Path(__file__).with_name("room_script_fixture.cjs").read_text()
    result = subprocess.run(
        ["node", "-e", fixture],
        input=json.dumps(script),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
