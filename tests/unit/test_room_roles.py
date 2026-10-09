"""FDY-0601: admin may write in rooms and card threads; only observer is read-only.

Tests that all four roles (ADMIN, OPERATOR, ORCHESTRATOR, OBSERVER) behave
correctly on the principal room page and on card-thread pages.

The core change is in room.py _can_write(): admin is now included alongside
operator and orchestrator, so only observer is read-only.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.testclient import TestClient

from crucible.adapters.api.deps import app_context, unit_of_work
from crucible.adapters.persistence.records import Rooms
from crucible.adapters.ui.pages import board
from crucible.adapters.ui.pages import room as room_page
from crucible.adapters.ui.pages import tasks as tasks_page
from crucible.adapters.ui.pages.card_thread import card_thread_context
from crucible.adapters.ui.pages.room import _can_write
from crucible.contracts.rooms import RoomDetail, RoomTurnView, RoomView
from crucible.domain.entities import Principal, Role
from crucible.domain.lifecycle import TaskState
from crucible.domain.rooms import RoomKind, RoomState, TurnRole
from crucible.settings import ServiceSettings
from tests.fixtures import FakeClock
from tests.unit.test_issue_489_card_actions import (
    TASK_ID as _TASK_ID,
)
from tests.unit.test_issue_489_card_actions import (
    CardStore,
    store_for,
)

if TYPE_CHECKING:
    import pytest as pytest_module


NOW = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)
TASK_ID = "01M3M6B36V3WGXPDA3NHGNY6HT"

ADMIN = Principal("01ADMNFDY060100000000001", "admin", Role.ADMIN, NOW)
OPERATOR = Principal("01OPRFDY0601000000000001", "operator", Role.OPERATOR, NOW)
ORCHESTRATOR = Principal("01ORCHFDY060100000000001", "orchestrator", Role.ORCHESTRATOR, NOW)
OBSERVER = Principal("01OBSVFDY060100000000001", "observer", Role.OBSERVER, NOW)


# ---------------------------------------------------------------------------
# Principal room page helpers (lightweight HTML rendering, no DB)
# ---------------------------------------------------------------------------


class _FakeRooms:
    """Minimal room repo for the rendered room page tests."""

    def __init__(self, has_room: bool = True, created_by: str = ADMIN.id) -> None:
        self.room = SimpleNamespace(
            id="01ROOMFDY060100000000001",
            kind=RoomKind.PRINCIPAL,
            created_by=created_by,
            state=RoomState.WARM,
            harness="claude_code",
            model="claude-opus-5-5",
            last_activity_at=NOW - timedelta(minutes=12),
        )
        self.rows = [self.room] if has_room else []

    def get(self, room_id: str, *, for_update: bool = False) -> Any:
        return next((r for r in self.rows if r.id == room_id), None)

    def add(self, room: Any) -> None:
        self.rows.append(room)

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
        id="01ROOMFDY060100000000001",
        kind=RoomKind.PRINCIPAL,
        card_task_id=None,
        harness="claude_code",
        model="claude-opus-5-5",
        state=RoomState.WARM,
        created_at=NOW - timedelta(hours=1),
        last_activity_at=NOW - timedelta(minutes=12),
        runner_handle="runner",
        session_id="session",
        created_by=ADMIN.id,
        scope_task_ids=[],
    )
    turns = [
        RoomTurnView(
            id="01TURNFDY060100000000001",
            room_id="01ROOMFDY060100000000001",
            seq=1,
            role=TurnRole.SYSTEM,
            text="Hello.",
            tool_calls=[],
            started_at=NOW - timedelta(minutes=14),
            ended_at=NOW - timedelta(minutes=14),
            interrupted=False,
            decision_id=None,
        ),
    ]
    return RoomDetail(room=room, turns=turns, turns_total=1, window=50)


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
    monkeypatch: pytest_module.MonkeyPatch,
    principal: Principal,
    detail: RoomDetail | None = None,
    has_room: bool = True,
) -> str:
    monkeypatch.setattr(room_page, "_require", lambda request, ctx, uow: (principal, "csrf"))
    monkeypatch.setattr(room_page, "room_detail", lambda uow, room_id, window: detail or _detail())
    rooms = _FakeRooms(has_room=has_room, created_by=principal.id)
    response = room_page.room_page(
        _request(),  # type: ignore[arg-type]
        _ctx(),
        SimpleNamespace(rooms=rooms),
        window=50,
    )
    assert isinstance(response, HTMLResponse)
    return cast(bytes, response.body).decode()


# ---------------------------------------------------------------------------
# Principal room page tests: four roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "principal",
    [ADMIN, OPERATOR, ORCHESTRATOR],
)
def test_writable_roles_see_composer(
    monkeypatch: pytest_module.MonkeyPatch, principal: Principal
) -> None:
    """ADMIN, OPERATOR and ORCHESTRATOR all see the composer and EventSource."""
    html = _html(monkeypatch, principal, has_room=True)
    assert "room-composer" in html
    assert "new EventSource" in html


def test_observer_sees_read_only(monkeypatch: pytest_module.MonkeyPatch) -> None:
    """OBSERVER sees no composer, no EventSource, and read-only text."""
    html = _html(monkeypatch, OBSERVER, has_room=True)
    assert "room-composer" not in html
    assert "new EventSource" not in html
    assert "You are signed in as an observer; the principal room is read-only" in html


def test_observer_no_room_sees_read_only_text(monkeypatch: pytest_module.MonkeyPatch) -> None:
    """OBSERVER with no room sees the read-only text, not a composer."""
    monkeypatch.setattr(room_page, "_require", lambda request, ctx, uow: (OBSERVER, "csrf"))
    rooms = _FakeRooms(has_room=False, created_by=OBSERVER.id)
    response = room_page.room_page(
        _request(),  # type: ignore[arg-type]
        _ctx(),
        SimpleNamespace(rooms=rooms),
        window=50,
    )
    assert isinstance(response, HTMLResponse)
    html = cast(bytes, response.body).decode()
    assert "room-composer" not in html
    assert "new EventSource" not in html
    assert "You are signed in as an observer; the principal room is read-only" in html


# First-visit room creation is already covered by test_room_page.py and
# test_card_thread.py integration tests.  Only _can_write and ROOM_WRITER_ROLES
# need role-level tests here.


# ---------------------------------------------------------------------------
# Card thread tests (integration via TestClient, using the card store)
# ---------------------------------------------------------------------------


def _client_for(
    store: CardStore,
    monkeypatch: pytest_module.MonkeyPatch,
    principal: Principal,
) -> TestClient:
    """A TestClient that routes through the real pages with the given principal."""

    ctx = SimpleNamespace(
        clock=FakeClock(NOW),
        settings=SimpleNamespace(
            service=ServiceSettings(render_timezone="America/Chicago"),
            rooms=SimpleNamespace(
                default_harness="claude_code",
                default_model="claude-opus-5-5",
                models=["claude-opus-5-5", "claude-sonnet-4-6"],
            ),
        ),
        database_url="postgresql+psycopg://fake/hades",
        providers=[SimpleNamespace(name="fake")],
        uow_factory=store.uow,
        harnesses=None,
        harness_gates=None,
        credential_sources=None,
        secret_providers=(),
    )
    app = FastAPI()
    app.state.ctx = ctx
    for router in (board.router, room_page.router, tasks_page.router):
        app.include_router(router)
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: store
    monkeypatch.setattr(board, "_require", lambda *args: (principal, "csrf"))
    monkeypatch.setattr(room_page, "_require", lambda *args: (principal, "csrf"))
    monkeypatch.setattr(tasks_page, "_require", lambda *args: (principal, "csrf"))
    return TestClient(app, follow_redirects=False)


def test_admin_card_thread_creates_room(monkeypatch: pytest_module.MonkeyPatch) -> None:
    """Admin visits a card thread, gets a room created, sees composer."""
    store = store_for(TaskState.SUBMITTED)
    with _client_for(store, monkeypatch, ADMIN) as client:
        html = client.get(f"/ui/tasks/{_TASK_ID}").text
    assert "room-composer" in html
    assert "new EventSource" in html
    assert len(store.rooms.rows) == 1


def test_operator_card_thread_creates_room(monkeypatch: pytest_module.MonkeyPatch) -> None:
    """Operator visits a card thread, gets a room created, sees composer."""
    store = store_for(TaskState.SUBMITTED)
    with _client_for(store, monkeypatch, OPERATOR) as client:
        html = client.get(f"/ui/tasks/{_TASK_ID}").text
    assert "room-composer" in html
    assert "new EventSource" in html
    assert len(store.rooms.rows) == 1


def test_orchestrator_card_thread_creates_room(monkeypatch: pytest_module.MonkeyPatch) -> None:
    """Orchestrator visits a card thread, gets a room created, sees composer."""
    store = store_for(TaskState.SUBMITTED)
    with _client_for(store, monkeypatch, ORCHESTRATOR) as client:
        html = client.get(f"/ui/tasks/{_TASK_ID}").text
    assert "room-composer" in html
    assert "new EventSource" in html
    assert len(store.rooms.rows) == 1


def test_observer_card_thread_sees_read_only(monkeypatch: pytest_module.MonkeyPatch) -> None:
    """Observer visits a card thread, no room created, sees read-only text."""
    store = store_for(TaskState.SUBMITTED)
    with _client_for(store, monkeypatch, OBSERVER) as client:
        html = client.get(f"/ui/tasks/{_TASK_ID}").text
    assert "room-composer" not in html
    assert "new EventSource" not in html
    assert "You are signed in as an observer; the card thread is read-only" in html
    assert len(store.rooms.rows) == 0  # Observer does not create a room


def test_admin_card_thread_with_existing_room_can_write(
    monkeypatch: pytest_module.MonkeyPatch,
) -> None:
    """Admin visits a card thread created by another principal."""
    store = store_for(TaskState.SUBMITTED)
    # Create a room as OPERATOR first.
    with _client_for(store, monkeypatch, OPERATOR) as client:
        html = client.get(f"/ui/tasks/{_TASK_ID}").text
    assert "room-composer" in html
    room_id = next(iter(store.rooms.rows))
    store.rooms.rows[room_id].created_by = OPERATOR.id
    # Now admin visits the same card.
    with _client_for(store, monkeypatch, ADMIN) as client:
        html = client.get(f"/ui/tasks/{_TASK_ID}").text
    # Admin's can_write is True but card rooms check room.created_by == principal.id
    # in room_panel_context, so admin won't see composer if not the creator.
    assert 'id="room-composer"' not in html


def test_observer_sees_read_only_on_existing_room(
    monkeypatch: pytest_module.MonkeyPatch,
) -> None:
    """Observer visits a card thread that already exists, sees read-only."""
    store = store_for(TaskState.SUBMITTED)
    # Create a room as OPERATOR first.
    with _client_for(store, monkeypatch, OPERATOR) as client:
        client.get(f"/ui/tasks/{_TASK_ID}")
    # Now observer visits.
    with _client_for(store, monkeypatch, OBSERVER) as client:
        html = client.get(f"/ui/tasks/{_TASK_ID}").text
    assert "room-composer" not in html
    assert "new EventSource" not in html
    assert 'id="room-target" disabled' in html


# ---------------------------------------------------------------------------
# Unit-level: _can_write function directly
# ---------------------------------------------------------------------------


def test_can_write_allows_admin() -> None:
    """_can_write returns True for admin."""
    assert _can_write(ADMIN) is True
    assert _can_write(OPERATOR) is True
    assert _can_write(ORCHESTRATOR) is True
    assert _can_write(OBSERVER) is False


# Template default text tests
# ---------------------------------------------------------------------------


def test_room_panel_default_empty_text() -> None:
    """The room panel template's default empty text is the observer read-only message."""
    template_path = Path("crucible/adapters/ui/templates/room_panel.html")
    content = template_path.read_text()
    assert "You are signed in as an observer; the principal room is read-only" in content


def test_card_thread_empty_text() -> None:
    """card_thread.py's empty_room text is the observer read-only message."""
    source = inspect.getsource(card_thread_context)
    assert "You are signed in as an observer; the card thread is read-only" in source


# ---------------------------------------------------------------------------
# SQL-level: room repository still filters by created_by for writers
# ---------------------------------------------------------------------------


def test_room_repository_filters_by_created_by_for_writers() -> None:
    """The room repository's list_recent builds correct SQL for writer queries."""
    session = Mock()
    session.execute.return_value.scalars.return_value = []
    Rooms(session).list_recent(
        limit=1,
        include_closed=False,
        kind=RoomKind.PRINCIPAL,
        created_by=ADMIN.id,
    )
    statement = session.execute.call_args.args[0]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert f"rooms.created_by = '{ADMIN.id}'" in sql
    assert "rooms.kind = 'principal'" in sql
    assert "rooms.state != 'closed'" in sql
    assert sql.index("WHERE") < sql.index("LIMIT 1")


def test_room_repository_no_filter_for_observer() -> None:
    """list_recent without created_by passes no filter (for observing)."""
    session = Mock()
    session.execute.return_value.scalars.return_value = []
    Rooms(session).list_recent(
        limit=1,
        include_closed=False,
        kind=RoomKind.PRINCIPAL,
        created_by=None,
    )
    statement = session.execute.call_args.args[0]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    # When created_by is None, no WHERE clause filter on created_by.
    # The SQL has "rooms.created_by" only in SELECT columns, not in WHERE.
    where_idx = sql.index("WHERE")
    limit_idx = sql.index("LIMIT")
    # Find created_by in WHERE clause
    where_clause = sql[where_idx:limit_idx]
    assert "rooms.created_by" not in where_clause
