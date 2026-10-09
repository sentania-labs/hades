"""hades #208 (FDY-0590): rooms, the room runner, and Hades owning the transcript.

AC1: migration 0059 and ADR 0031 exist, the tables are created with the named columns,
and the API is served with its role rules and documented in OpenAPI. AC2: a message to
a room with no warm runner launches the runner through the provider with the launch the
docs describe, and a fake runner proves inject, stream, interrupt, idle reclaim and
switch end to end against the state machine. AC3: the session start assembles identity,
recall, decisions and the bounded transcript window, each tested alone, and the rolling
summary. AC4: the Hades tools are SDK in-process tools behind a room-scoped token and a
PreToolUse hook that records calls; the token cannot act outside its room and the tasks
it names. AC5: a harness other than claude_code is refused with 409; the docs describe
the lifecycle, the launch, the emptyDir caveat and the idle timeout setting.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import io
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from starlette.testclient import TestClient

from crucible.adapters.api.deps import (
    AppContext,
    SseTailLimiter,
    app_context,
    current_principal,
    unit_of_work,
)
from crucible.adapters.api.problems import install_problem_handlers
from crucible.adapters.api.routers import rooms as rooms_router
from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.execution.room_launch import (
    LABEL_ROOM,
    ROLE_ROOM_RUNNER,
    FakeRoomLauncher,
    KubernetesRoomLauncher,
    api_rule,
    k8s_pod,
    runner_command,
    runner_env,
)
from crucible.adapters.harness.registry import default_registry
from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.migrations.versions import _0059_rooms as m59
from crucible.adapters.persistence.models import Base
from crucible.application import room_tools
from crucible.application.errors import ConflictError, ForbiddenError, NotFoundError
from crucible.application.rooms import (
    IDLE_SETTING,
    STALE_RUNNER,
    RoomConfig,
    RoomContext,
    authenticate_runner,
    idle_timeout,
    idle_timeout_value,
    mint_runner_token,
    read_identity,
    save_idle_timeout,
    session_start,
)
from crucible.domain.entities import (
    Event,
    LedgerDecision,
    MemoryItem,
    Principal,
    ProviderSetting,
    Role,
    Task,
    TaskContract,
    TaskNote,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from crucible.domain.memory import matches
from crucible.domain.room_session import (
    CardContext,
    assemble,
    decisions_touching,
    recall_section,
    rolling_summary,
    split_window,
    subject_for,
)
from crucible.domain.rooms import (
    ALLOWED_TOOLS,
    DISALLOWED_TOOLS,
    HADES_TOOLS,
    TRANSITIONS,
    Room,
    RoomEvent,
    RoomKind,
    RoomState,
    RoomTransitionError,
    RoomTurn,
    TurnRole,
    next_state,
    switch_text,
)
from crucible.domain.rooms import local_time as room_local_time
from crucible.ports.repository import UnitOfWork
from crucible.ports.rooms import (
    RUNNER_COMMAND,
    RUNNER_CONFIG_DIR,
    RUNNER_DIR,
    RUNNER_TOKEN_DIR,
    RoomRunnerError,
    RoomRunnerLaunch,
)
from tests.fixtures import FakeClock
from tests.wait import async_wait_until
from tools import room_runner

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
OPERATOR = Principal(
    id="01OPER208ROOMS00000000001", name="scott", role=Role.OPERATOR, created_at=NOW
)
HADES = Principal(
    id="01HADE208ROOMS00000000001", name="hades", role=Role.ORCHESTRATOR, created_at=NOW
)
OBSERVER = Principal(
    id="01OBSV208ROOMS00000000001", name="reader", role=Role.OBSERVER, created_at=NOW
)
ADMIN = Principal(id="01ADMN208ROOMS00000000001", name="root", role=Role.ADMIN, created_at=NOW)
CARD_ID = "01TASK208R00MS000000000001"
OTHER_ID = "01TASK208R00MS000000000002"
EM_DASH = chr(0x2014)
IDENTITY = "# Hades\nYou are Hades, Scott's principal."
CONFIG = RoomConfig(api_url="http://crucible-api.crucible.svc:8080", image="crucible-worker:test")


# ----- an in-memory record --------------------------------------------------------


class _Rooms:
    def __init__(self) -> None:
        self.rows: dict[str, Room] = {}

    def add(self, room: Room) -> None:
        self.rows[room.id] = copy.deepcopy(room)

    def get(self, room_id: str, *, for_update: bool = False) -> Room | None:
        row = self.rows.get(room_id)
        return copy.deepcopy(row) if row is not None else None

    def save(self, room: Room) -> None:
        assert room.id in self.rows
        self.rows[room.id] = copy.deepcopy(room)

    def list_recent(self, *, limit: int, include_closed: bool = True) -> list[Room]:
        rows = [r for r in self.rows.values() if include_closed or r.state is not RoomState.CLOSED]
        return [copy.deepcopy(r) for r in rows][-limit:]

    def list_live(self) -> list[Room]:
        live = (RoomState.STARTING, RoomState.WARM, RoomState.INTERRUPTED)
        return [copy.deepcopy(r) for r in self.rows.values() if r.state in live]


class _Turns:
    def __init__(self) -> None:
        self.rows: list[RoomTurn] = []

    def add(self, turn: RoomTurn) -> None:
        assert all(not (t.room_id == turn.room_id and t.seq == turn.seq) for t in self.rows)
        self.rows.append(copy.deepcopy(turn))

    def get(self, room_id: str, seq: int, *, for_update: bool = False) -> RoomTurn | None:
        for t in self.rows:
            if t.room_id == room_id and t.seq == seq:
                return copy.deepcopy(t)
        return None

    def save(self, turn: RoomTurn) -> None:
        for i, t in enumerate(self.rows):
            if t.id == turn.id:
                self.rows[i] = copy.deepcopy(turn)
                return
        raise AssertionError("save of a turn that was never added")

    def last_seq(self, room_id: str) -> int:
        return max((t.seq for t in self.rows if t.room_id == room_id), default=0)

    def count(self, room_id: str) -> int:
        return sum(1 for t in self.rows if t.room_id == room_id)

    def list_for_room(
        self, room_id: str, *, after_seq: int = 0, limit: int | None = None
    ) -> list[RoomTurn]:
        rows = sorted(
            (t for t in self.rows if t.room_id == room_id and t.seq > after_seq),
            key=lambda t: t.seq,
        )
        if limit is not None:
            rows = rows[-limit:]
        return [copy.deepcopy(t) for t in rows]

    def of(self, room_id: str) -> list[RoomTurn]:
        return self.list_for_room(room_id)


class _Memory:
    def __init__(self) -> None:
        self.rows: list[MemoryItem] = []

    def recall(
        self, *, tags: Sequence[str], keywords: Sequence[str], limit: int
    ) -> list[MemoryItem]:
        return [r for r in self.rows if matches(r, tags=tags, keywords=keywords)][:limit]


class _Ledger:
    def __init__(self) -> None:
        self.rows: list[LedgerDecision] = []

    def add(self, decision: LedgerDecision) -> None:
        self.rows.append(decision)

    def list_recent(self, *, limit: int, channel: str | None = None) -> list[LedgerDecision]:
        return [r for r in self.rows if channel is None or r.channel == channel][-limit:]


class _Events:
    def __init__(self) -> None:
        self.rows: list[Event] = []

    def append(self, event: Event) -> Event:
        event.seq = len(self.rows) + 1
        self.rows.append(event)
        return event


class _Tasks:
    def __init__(self, tasks: Sequence[Task]) -> None:
        self.rows = {t.id: t for t in tasks}

    def get(self, task_id: str, *, for_update: bool = False) -> Task | None:
        return self.rows.get(task_id)

    def search(self, **filters: Any) -> list[Task]:
        rows = sorted(self.rows.values(), key=lambda t: t.id)
        if filters.get("external_id") is not None:
            rows = [t for t in rows if t.external_id == filters["external_id"]]
        if filters.get("project") is not None:
            rows = [t for t in rows if t.project == filters["project"]]
        if filters.get("after_id") is not None:
            rows = [t for t in rows if t.id > filters["after_id"]]
        return rows[: filters.get("limit", 100)]


class _Contracts:
    def __init__(self, contracts: Sequence[TaskContract]) -> None:
        self.rows = {(c.task_id, c.version): c for c in contracts}

    def get(self, task_id: str, version: int) -> TaskContract | None:
        return self.rows.get((task_id, version))


class _Principals:
    def __init__(self) -> None:
        self.rows = {p.id: p for p in (OPERATOR, HADES, OBSERVER, ADMIN)}

    def get(self, principal_id: str) -> Principal | None:
        return self.rows.get(principal_id)


class _Notes:
    def __init__(self) -> None:
        self.rows: list[TaskNote] = []

    def add(self, note: TaskNote) -> None:
        self.rows.append(note)

    def list_for_task(self, task_id: str) -> list[TaskNote]:
        return [n for n in self.rows if n.task_id == task_id]


class _Settings:
    def __init__(self) -> None:
        self.rows: dict[str, ProviderSetting] = {}

    def get(self, name: str) -> ProviderSetting | None:
        return self.rows.get(name)

    def put(self, row: ProviderSetting) -> ProviderSetting:
        self.rows[row.name] = row
        return row


def _task(task_id: str, external_id: str, title: str, objective: str = "") -> Task:
    return Task(
        id=task_id,
        external_id=external_id,
        principal_id=OPERATOR.id,
        project="hades",
        title=title,
        state=TaskState.PROPOSED,
        contract_version=1,
        policy_name="hades-self-hosting",
        policy_version=30,
        repository_id="01REPO208ROOMS00000000001",
        created_at=NOW - timedelta(days=1) + timedelta(minutes=int(task_id[-1])),
        updated_at=NOW - timedelta(hours=1),
    )


CARD_OBJECTIVE = "Rooms and the room runner: Hades owns the transcript."


class _Store:
    def __init__(self) -> None:
        self.rooms = _Rooms()
        self.room_turns = _Turns()
        self.memory = _Memory()
        self.decision_ledger = _Ledger()
        self.events = _Events()
        card = _task(CARD_ID, "FDY-0590", "Rooms and the room runner")
        other = _task(OTHER_ID, "FDY-0591", "The room page")
        self.tasks = _Tasks([card, other])
        self.contracts = _Contracts(
            [
                TaskContract(
                    id="01CONT208ROOMS00000000001",
                    task_id=CARD_ID,
                    version=1,
                    document={"external_id": "FDY-0590", "objective": CARD_OBJECTIVE},
                    sha256="0" * 64,
                    submitted_at=NOW,
                ),
                TaskContract(
                    id="01CONT208ROOMS00000000002",
                    task_id=OTHER_ID,
                    version=1,
                    document={
                        "external_id": "FDY-0591",
                        "title": "The room page",
                        "objective": "Show a room.",
                        "repository": {"name": "hades", "work_branch": "crucible/FDY-0591"},
                        "execution_request": {"tier": "complex"},
                        "acceptance_criteria": [{"id": "AC1", "text": "the page"}],
                    },
                    sha256="1" * 64,
                    submitted_at=NOW,
                ),
            ]
        )
        self.principals = _Principals()
        self.task_notes = _Notes()
        self.provider_settings = _Settings()
        self.commits = 0

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
        self.commits += 1

    def rollback(self) -> None:
        return None

    def uow(self) -> UnitOfWork:
        return cast(UnitOfWork, self)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events.rows]


class _Api:
    """The rooms router over the in-memory record, with the fake launcher."""

    def __init__(self, principal: Principal = OPERATOR, *, config: RoomConfig = CONFIG) -> None:
        self.store = _Store()
        self.clock = FakeClock(NOW)
        self.launcher = FakeRoomLauncher()
        self.principal = principal
        self.ctx = AppContext(
            uow_factory=self.store.uow,
            clock=self.clock,
            providers=[],
            database_url="",
            engine=None,  # type: ignore[arg-type]
            artifact_store=None,  # type: ignore[arg-type]
            sse_tail_limiter=SseTailLimiter(4),
            rooms=RoomContext(config=replace(config, identity_path=None), launcher=self.launcher),
        )
        app = FastAPI()
        install_problem_handlers(app)
        app.include_router(rooms_router.router, prefix="/v1")
        app.state.ctx = self.ctx
        app.dependency_overrides[app_context] = lambda: self.ctx
        app.dependency_overrides[unit_of_work] = lambda: self.store
        app.dependency_overrides[current_principal] = lambda: self.principal
        app.dependency_overrides[rooms_router.stream_reader] = lambda: self.principal
        self.client = TestClient(app)

    def create(self, **body: Any) -> dict[str, Any]:
        response = self.client.post(
            "/v1/rooms", json={"kind": "principal", "harness": "claude_code", "model": "m", **body}
        )
        assert response.status_code == 201, response.text
        return dict(response.json())

    def say(self, room_id: str, text: str) -> dict[str, Any]:
        response = self.client.post(f"/v1/rooms/{room_id}/messages", json={"text": text})
        assert response.status_code == 201, response.text
        return dict(response.json())

    def room(self, room_id: str) -> Room:
        found = self.store.rooms.get(room_id)
        assert found is not None
        return found

    def runner(self, token: str | None = None) -> Hades:
        return Hades(self.client, token or self.launcher.token)


class Hades:
    """A fake runner: the runner's side of the protocol, driven by hand."""

    def __init__(self, client: TestClient, token: str) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}

    def get(self, path: str) -> Any:
        return self.client.get(path, headers=self.headers)

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        return self.client.post(path, json=body, headers=self.headers)

    def inbox(self, room_id: str) -> dict[str, Any]:
        response = self.get(f"/v1/rooms/{room_id}/inbox?wait=0")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def events(self, room_id: str, seq: int, *events: dict[str, Any]) -> dict[str, Any]:
        response = self.post(f"/v1/rooms/{room_id}/turns/{seq}/events", {"events": list(events)})
        assert response.status_code == 200, response.text
        return dict(response.json())


def _sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    found = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            found.append((lines["event"], json.loads(lines["data"])))
    return found


# ----- AC1: the migration, the ADR, the tables, the API and its roles ------------------


def _render(step: Any) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        step()
    return buffer.getvalue()


def test_0059_sits_on_0058_in_the_single_chain_and_adr_0031_exists() -> None:
    script = ScriptDirectory.from_config(migrate.alembic_config("postgresql://unused/unused"))
    # hades #208 item 2 (comment delivery) is numbered above 0059_rooms; the chain stays
    # single and linear, with 0059 on 0058 in it.
    heads = script.get_heads()
    assert len(heads) == 1
    assert "0059_rooms" in {r.revision for r in script.iterate_revisions(heads[0], "base")}
    revision = script.get_revision("0059_rooms")
    assert revision is not None and revision.down_revision == "0058_memory_and_decisions"
    adr = REPO / "docs" / "adr" / "0031-rooms-hades-owns-the-transcript.md"
    assert adr.is_file()
    assert adr.read_text(encoding="utf-8").startswith(
        "# ADR 0031: Rooms: Hades owns the transcript"
    )


def test_0059_creates_rooms_and_room_turns_with_the_named_columns() -> None:
    sql = _render(m59.upgrade)
    rooms, turns = sql.split("CREATE TABLE room_turns")
    assert "CREATE TABLE rooms" in rooms
    for column in (
        "id",
        "kind",
        "card_task_id",
        "harness",
        "model",
        "state",
        "created_at",
        "last_activity_at",
        "runner_handle",
        "session_id",
    ):
        assert re.search(rf"\b{column}\b", rooms), column
    for column in (
        "room_id",
        "seq",
        "role",
        "text",
        "tool_calls",
        "started_at",
        "ended_at",
        "interrupted",
        "decision_id",
    ):
        assert re.search(rf"\b{column}\b", turns), column
    assert "'idle', 'starting', 'warm', 'interrupted', 'closed'" in sql
    assert "'principal', 'card'" in sql and "'user', 'assistant', 'system'" in sql
    assert "uq_room_turns_room_seq" in sql
    for kind in m59.EVENT_KINDS:
        assert f"'{kind}'" in sql
    down = _render(m59.downgrade)
    assert "DROP TABLE room_turns" in down and "DROP TABLE rooms" in down
    # No column holds the runner's token; only its salted digest, while a runner is up.
    assert "token" not in " ".join(Base.metadata.tables["rooms"].columns.keys())


def test_the_orm_mirrors_0059_and_the_event_kinds_match_the_enum() -> None:
    assert set(Base.metadata.tables["room_turns"].columns.keys()) == {
        "id",
        "room_id",
        "seq",
        "role",
        "text",
        "tool_calls",
        "started_at",
        "ended_at",
        "interrupted",
        "decision_id",
    }
    rooms = set(Base.metadata.tables["rooms"].columns.keys())
    assert {
        "id",
        "kind",
        "card_task_id",
        "harness",
        "model",
        "state",
        "created_at",
        "last_activity_at",
        "runner_handle",
        "session_id",
    } <= rooms
    for column in re.findall(r'sa\.Column\(\s*"(\w+)"', (REPO / m59.__file__).read_text()):
        assert column in rooms | set(Base.metadata.tables["room_turns"].columns.keys())
    # 0059's kinds are all in the enum; the chain's head, 0060_comment_delivery now, owns
    # the CHECK constraint and test_schema_hygiene holds it to the whole enum.
    assert set(m59._event_kinds()) <= {k.value for k in EventKind}


def test_the_room_routes_are_in_openapi() -> None:
    from crucible.adapters.api.app import create_app  # noqa: PLC0415

    context = AppContext(
        uow_factory=None,  # type: ignore[arg-type]
        clock=None,  # type: ignore[arg-type]
        providers=[],
        database_url="",
        engine=None,  # type: ignore[arg-type]
        artifact_store=None,  # type: ignore[arg-type]
    )
    paths = create_app(context).openapi()["paths"]
    expected = {
        "/v1/rooms": {"get", "post"},
        "/v1/rooms/{room_id}": {"get"},
        "/v1/rooms/{room_id}/messages": {"post"},
        "/v1/rooms/{room_id}/stream": {"get"},
        "/v1/rooms/{room_id}/interrupt": {"post"},
        "/v1/rooms/{room_id}/switch": {"post"},
        "/v1/rooms/{room_id}/close": {"post"},
        "/v1/rooms/{room_id}/session": {"get"},
        "/v1/rooms/{room_id}/inbox": {"get"},
        "/v1/rooms/{room_id}/turns/{seq}/events": {"post"},
        "/v1/rooms/{room_id}/tools/{tool}": {"post"},
        "/v1/rooms/{room_id}/runner/exit": {"post"},
    }
    for path, methods in expected.items():
        assert methods <= set(paths[path]), path


def test_orchestrator_and_operator_write_and_an_observer_only_reads() -> None:
    api = _Api(HADES)
    room = api.create()
    api.principal = OPERATOR
    api.say(room["id"], "hello")
    for principal in (OBSERVER, ADMIN):
        api.principal = principal
        assert (
            api.client.post(
                "/v1/rooms", json={"kind": "principal", "harness": "claude_code", "model": "m"}
            ).status_code
            == 403
        )
        assert (
            api.client.post(f"/v1/rooms/{room['id']}/messages", json={"text": "x"}).status_code
            == 403
        )
        for action in ("interrupt", "close"):
            assert api.client.post(f"/v1/rooms/{room['id']}/{action}").status_code == 403
        switch = {"harness": "claude_code", "model": "n"}
        assert api.client.post(f"/v1/rooms/{room['id']}/switch", json=switch).status_code == 403
    api.principal = OBSERVER
    detail = api.client.get(f"/v1/rooms/{room['id']}")
    assert detail.status_code == 200
    assert [t["text"] for t in detail.json()["turns"]] == ["hello"]
    assert api.client.get("/v1/rooms").json()["items"][0]["id"] == room["id"]


def test_a_card_room_names_its_task_by_id_or_external_id() -> None:
    api = _Api()
    by_external = api.create(kind="card", card_task_id="FDY-0590")
    assert by_external["card_task_id"] == CARD_ID and by_external["scope_task_ids"] == [CARD_ID]
    by_id = api.create(kind="card", card_task_id=CARD_ID)
    assert by_id["card_task_id"] == CARD_ID
    missing = api.client.post(
        "/v1/rooms",
        json={"kind": "card", "harness": "claude_code", "model": "m", "card_task_id": "FDY-9999"},
    )
    assert missing.status_code == 404
    unnamed = api.client.post(
        "/v1/rooms", json={"kind": "card", "harness": "claude_code", "model": "m"}
    )
    assert unnamed.status_code == 422


def test_get_returns_the_room_and_a_bounded_window_of_turns() -> None:
    api = _Api()
    room = api.create()
    api.launcher.fail_with = None
    for n in range(7):
        api.store.room_turns.add(
            RoomTurn(
                id=f"01TURN208ROOMS0000000000{n}",
                room_id=room["id"],
                seq=n + 1,
                role=TurnRole.USER,
                text=f"turn {n + 1}",
                started_at=NOW,
                ended_at=NOW,
            )
        )
    detail = api.client.get(f"/v1/rooms/{room['id']}?window=3").json()
    assert [t["seq"] for t in detail["turns"]] == [5, 6, 7]
    assert detail["turns_total"] == 7 and detail["window"] == 3
    assert detail["room"]["state"] == "idle"
    assert api.client.get(f"/v1/rooms/{room['id']}?window=100000").status_code == 422


# ----- the state machine --------------------------------------------------------------


def test_the_state_machine_allows_exactly_its_transitions() -> None:
    assert next_state(RoomState.IDLE, RoomEvent.LAUNCH) is RoomState.STARTING
    assert next_state(RoomState.STARTING, RoomEvent.RUNNER_READY) is RoomState.WARM
    assert next_state(RoomState.STARTING, RoomEvent.LAUNCH_FAILED) is RoomState.IDLE
    assert next_state(RoomState.WARM, RoomEvent.INTERRUPT) is RoomState.INTERRUPTED
    assert next_state(RoomState.INTERRUPTED, RoomEvent.TURN_ENDED) is RoomState.WARM
    for state in (RoomState.IDLE, RoomState.STARTING, RoomState.WARM, RoomState.INTERRUPTED):
        assert next_state(state, RoomEvent.STOP) is RoomState.IDLE
        assert next_state(state, RoomEvent.CLOSE) is RoomState.CLOSED
    for event in RoomEvent:
        with pytest.raises(RoomTransitionError):
            next_state(RoomState.CLOSED, event)
    with pytest.raises(RoomTransitionError):
        next_state(RoomState.WARM, RoomEvent.LAUNCH)
    with pytest.raises(RoomTransitionError):
        next_state(RoomState.IDLE, RoomEvent.INTERRUPT)
    assert all(state is not RoomState.CLOSED for state, _ in TRANSITIONS)


# ----- AC2: the launch and the fake runner end to end ----------------------------------


def test_a_message_to_a_room_with_no_warm_runner_launches_one() -> None:
    api = _Api()
    room = api.create()
    turn = api.say(room["id"], "Good afternoon.")
    assert turn["role"] == "user" and turn["seq"] == 1 and turn["text"] == "Good afternoon."
    [launch] = api.launcher.launches
    assert launch.room_id == room["id"] and launch.harness == "claude_code"
    assert launch.model == "m" and launch.image == "crucible-worker:test"
    assert launch.api_url == CONFIG.api_url
    assert launch.idle_timeout_seconds == 30 * 60
    assert launch.token.startswith(f"crr_{room['id']}.")
    assert launch.token not in repr(launch)
    stored = api.room(room["id"])
    assert stored.state is RoomState.STARTING
    assert stored.runner_handle == f"fake:room-{room['id'].lower()}-1"
    assert stored.runner_key_digest is not None
    # A second message while the runner starts launches nothing more.
    api.say(room["id"], "Are you there?")
    assert len(api.launcher.launches) == 1
    assert EventKind.ROOM_RUNNER_LAUNCHED.value in api.store.kinds()


def test_a_launch_that_fails_leaves_the_message_and_an_idle_room() -> None:
    api = _Api()
    room = api.create()
    api.launcher.fail_with = "the namespace quota has no room"
    response = api.client.post(f"/v1/rooms/{room['id']}/messages", json={"text": "hello"})
    assert response.status_code == 503
    assert "the namespace quota has no room" in response.json()["detail"]
    stored = api.room(room["id"])
    assert stored.state is RoomState.IDLE and stored.runner_key_digest is None
    assert [t.text for t in api.store.room_turns.of(room["id"])] == ["hello"]
    api.launcher.fail_with = None
    api.say(room["id"], "again")
    assert api.room(room["id"]).state is RoomState.STARTING


def test_no_api_url_means_no_launch_and_says_why() -> None:
    api = _Api(config=RoomConfig(api_url=""))
    room = api.create()
    response = api.client.post(f"/v1/rooms/{room['id']}/messages", json={"text": "hello"})
    assert response.status_code == 503 and "rooms.api_url" in response.json()["detail"]
    assert api.launcher.launches == []


def test_inject_stream_interrupt_idle_reclaim_and_switch_end_to_end() -> None:
    """The whole lifecycle against the record, with a fake runner on the protocol."""
    api = _Api()
    room = api.create(kind="card", card_task_id="FDY-0590")
    rid = room["id"]
    api.store.memory.rows.append(
        MemoryItem(
            id="01MEM0208ROOMS00000000001",
            text="The room runner lives in a worker Pod.",
            source="operator",
            observed_at=NOW - timedelta(days=1),
            scope_tags=["project:hades"],
            promoted_by="scott",
            promoted_at=NOW - timedelta(days=1),
        )
    )
    api.say(rid, "What is left on this card?")
    runner = api.runner()

    # The runner's session start: identity, card, recall, decisions, turns.
    session = runner.get(f"/v1/rooms/{rid}/session").json()
    assert session["allowed_tools"] == list(ALLOWED_TOOLS)
    assert session["disallowed_tools"] == list(DISALLOWED_TOOLS)
    assert session["idle_timeout_seconds"] == 1800
    assert "You are Hades" in session["system_prompt"]
    assert "FDY-0590: Rooms and the room runner" in session["system_prompt"]
    assert "The room runner lives in a worker Pod." in session["system_prompt"]
    # The message not yet handed out is the inbox's to deliver, not the prompt's.
    assert "What is left on this card?" not in session["system_prompt"]

    # Inject: the inbox hands the turn out and opens the assistant turn.
    first = runner.inbox(rid)
    assert first["control"] is None
    assert first["message"] == {
        "schema_version": "1.0",
        "user_seq": 1,
        "assistant_seq": 2,
        "text": "What is left on this card?",
    }
    assert api.room(rid).state is RoomState.WARM
    assert runner.inbox(rid) == {"schema_version": "1.0", "control": None, "message": None}

    # Stream: deltas and the turn end reach the stream.
    runner.events(rid, 2, {"kind": "session", "session_id": "sess-1"})
    runner.events(
        rid, 2, {"kind": "delta", "text": "The room "}, {"kind": "delta", "text": "page."}
    )
    accepted = runner.events(rid, 2, {"kind": "end"})
    assert accepted["ended"] is True and accepted["control"] is None
    events = _sse(api.client.get(f"/v1/rooms/{rid}/stream?max_seconds=1").text)
    assert ("room", {"room_id": rid, "state": "warm"}) in events
    assert [e for e in events if e[0] == "turn_end"] == [
        ("turn_end", {"seq": 1, "role": "user", "interrupted": False}),
        ("turn_end", {"seq": 2, "role": "assistant", "interrupted": False}),
    ]
    assert api.store.room_turns.get(rid, 2) is not None
    assert cast(RoomTurn, api.store.room_turns.get(rid, 2)).text == "The room page."
    assert api.room(rid).session_id == "sess-1"

    # A stream that is open while the reply arrives sees it as deltas.
    api.say(rid, "And then?")
    second = runner.inbox(rid)["message"]
    assert second["assistant_seq"] == 4
    runner.events(rid, 4, {"kind": "delta", "text": "First"})
    streamed = _sse(api.client.get(f"/v1/rooms/{rid}/stream?after_seq=3&max_seconds=1").text)
    assert ("turn", streamed[1][1]) == streamed[1] and streamed[1][1]["text"] == "First"
    reconnected = _sse(api.client.get(f"/v1/rooms/{rid}/stream?after_seq=4&max_seconds=1").text)
    assert any(event == "turn" and data["seq"] == 4 for event, data in reconnected)

    # Interrupt: the runner reads `interrupt` once, then ends the turn as interrupted.
    interrupted = api.client.post(f"/v1/rooms/{rid}/interrupt")
    assert interrupted.status_code == 200 and interrupted.json()["state"] == "interrupted"
    assert runner.inbox(rid)["control"] == "interrupt"
    assert runner.inbox(rid)["control"] is None
    runner.events(rid, 4, {"kind": "delta", "text": ", then"}, {"kind": "end", "interrupted": True})
    turn = cast(RoomTurn, api.store.room_turns.get(rid, 4))
    assert turn.interrupted and turn.text == "First, then" and turn.ended_at is not None
    assert api.room(rid).state is RoomState.WARM
    assert api.client.post(f"/v1/rooms/{rid}/interrupt").status_code == 409
    # An ended turn takes no more events.
    late = runner.post(
        f"/v1/rooms/{rid}/turns/4/events", {"events": [{"kind": "delta", "text": "x"}]}
    )
    assert late.status_code == 409

    # Idle reclaim: past the timeout with nothing to answer, the poll says stop, the room
    # goes idle, the token stops working and the runner is removed at the provider.
    handle = api.room(rid).runner_handle
    api.clock.advance(31 * 60)
    assert runner.inbox(rid)["control"] == "stop"
    reclaimed = api.room(rid)
    assert reclaimed.state is RoomState.IDLE and reclaimed.runner_handle is None
    assert reclaimed.session_id is None and reclaimed.runner_key_digest is None
    assert api.launcher.stops == [handle]
    assert runner.get(f"/v1/rooms/{rid}/inbox?wait=0").status_code == 401

    # The next message starts a new runner with a new token, from the record.
    api.say(rid, "Back again.")
    assert len(api.launcher.launches) == 2
    second_runner = api.runner()
    assert second_runner.headers != runner.headers
    replay = second_runner.get(f"/v1/rooms/{rid}/session").json()["system_prompt"]
    assert "[#1 Operator" in replay and "The room page." in replay and "(interrupted)" in replay
    assert second_runner.inbox(rid)["message"]["text"] == "Back again."
    second_runner.events(rid, 6, {"kind": "delta", "text": "Welcome back."}, {"kind": "end"})

    # Switch: a system turn in the words of the contract, the runner stopped, and the
    # next message a new runner with the transcript replayed.
    switched = api.client.post(
        f"/v1/rooms/{rid}/switch", json={"harness": "claude_code", "model": "claude-opus-5-5"}
    )
    assert switched.status_code == 200
    body = switched.json()
    assert body["room"]["state"] == "idle" and body["room"]["model"] == "claude-opus-5-5"
    assert body["turn"]["role"] == "system"
    assert body["turn"]["text"] == (
        "Switched to claude_code (claude-opus-5-5) at 2026-10-09 10:31 AM CDT. "
        "Same history, same memory."
    )
    assert api.launcher.stops[-1] == f"fake:room-{rid.lower()}-2"
    assert second_runner.get(f"/v1/rooms/{rid}/inbox?wait=0").status_code == 401
    api.say(rid, "Which model now?")
    third = api.launcher.launches[-1]
    assert len(api.launcher.launches) == 3 and third.model == "claude-opus-5-5"
    prompt = api.runner().get(f"/v1/rooms/{rid}/session").json()["system_prompt"]
    assert "Switched to claude_code (claude-opus-5-5)" in prompt and "Welcome back." in prompt
    assert "Which model now?" not in prompt

    # Close: the runner goes, and the room takes nothing more.
    closed = api.client.post(f"/v1/rooms/{rid}/close")
    assert closed.json()["state"] == "closed"
    assert api.client.post(f"/v1/rooms/{rid}/messages", json={"text": "x"}).status_code == 409
    assert (
        _sse(api.client.get(f"/v1/rooms/{rid}/stream?after_seq=99&max_seconds=1").text)[-1][0]
        == "closed"
    )
    for kind in (
        "room_created",
        "room_runner_launched",
        "room_interrupted",
        "room_runner_stopped",
        "room_switched",
        "room_closed",
    ):
        assert kind in api.store.kinds()


def test_a_runner_that_went_away_is_reaped_and_the_next_message_relaunches() -> None:
    api = _Api()
    room = api.create()
    api.say(room["id"], "one")
    runner = api.runner()
    runner.inbox(room["id"])
    runner.events(room["id"], 2, {"kind": "delta", "text": "half"})
    api.clock.advance(STALE_RUNNER.total_seconds() + 1)
    api.say(room["id"], "two")
    assert len(api.launcher.launches) == 2 and len(api.launcher.stops) == 1
    lost = cast(RoomTurn, api.store.room_turns.get(room["id"], 2))
    assert lost.interrupted and lost.ended_at is not None and lost.text == "half"
    assert api.room(room["id"]).state is RoomState.STARTING


def test_the_runner_exit_notice_reclaims_the_room() -> None:
    api = _Api()
    room = api.create()
    api.say(room["id"], "one")
    runner = api.runner()
    assert runner.post(f"/v1/rooms/{room['id']}/runner/exit", {"reason": "idle"}).status_code == 204
    assert api.room(room["id"]).state is RoomState.IDLE
    assert (
        api.launcher.stops and runner.get(f"/v1/rooms/{room['id']}/inbox?wait=0").status_code == 401
    )


def test_the_kubernetes_pod_is_the_described_launch() -> None:
    credential = default_registry().require("claude_code").credential_spec()
    assert credential is not None
    launch = RoomRunnerLaunch(
        room_id="01ROOM208ROOMS00000000001",
        harness="claude_code",
        model="claude-sonnet-5",
        image="crucible-worker:x",
        api_url="http://crucible-api.crucible.svc:8080",
        idle_timeout_seconds=1800,
        egress_hosts=("api.anthropic.com", "pypi.org", "files.pythonhosted.org"),
        token="crr_secret-value",
    )
    pod = k8s_pod(
        launch,
        credential,
        image="crucible-worker@sha256:abc",
        egress_hosts=launch.egress_hosts,
        service_account="crucible-worker",
        image_pull_secret=None,
    )
    [container] = pod["containers"]
    assert container["command"][-7:] == list(RUNNER_COMMAND)
    assert RUNNER_COMMAND == (
        "uv",
        "run",
        "--no-project",
        "--with",
        "claude-agent-sdk",
        "python",
        "tools/room_runner.py",
    )
    assert container["command"][:4] == ["bash", "-o", "pipefail", "-c"]
    assert container["workingDir"] == RUNNER_DIR
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["CRUCIBLE_ENV_FROM_FILES"] == (
        f"CLAUDE_CODE_OAUTH_TOKEN={credential.mount_target}/oauth-token"
    )
    assert env["ROOM_CLAUDE_CONFIG_DIR"] == RUNNER_CONFIG_DIR
    assert env["ROOM_CWD"] == "/home/worker/rooms/01room208rooms00000000001"
    assert env["HADES_ROOM_TOKEN_FILE"] == f"{RUNNER_TOKEN_DIR}/token"
    # No credential value and no token anywhere in the Pod.
    assert "crr_secret-value" not in json.dumps(pod)
    assert not any(k.startswith(("CLAUDE", "ANTHROPIC")) for k in env)
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    assert mounts[credential.mount_target]["readOnly"] is True
    assert mounts[RUNNER_TOKEN_DIR]["readOnly"] is True
    assert mounts[RUNNER_DIR]["readOnly"] is True
    assert mounts[RUNNER_CONFIG_DIR]["readOnly"] is False
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert "emptyDir" in volumes["room-config"]
    assert volumes["room-runner"]["configMap"]["items"] == [
        {"key": "room_runner.py", "path": "tools/room_runner.py"}
    ]
    assert [i["key"] for i in volumes["room-credential"]["secret"]["items"]] == ["oauth-token"]
    assert pod["automountServiceAccountToken"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["securityContext"]["runAsNonRoot"] is True
    # No checkout and no repository.
    assert "/crucible/repo" not in json.dumps(pod)


class _StubKubernetesProvider:
    """The parts of the Kubernetes provider the room launcher drives, recorded."""

    def __init__(self) -> None:
        self.harnesses = default_registry()
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []
        self.config = SimpleNamespace(
            namespace="crucible-workers",
            service_account="crucible-worker",
            image_pull_secret=None,
            cluster_dns_ip="10.96.0.10",
            credential_secret_name=lambda harness: f"harness-{harness}",
        )
        self.client = SimpleNamespace(
            get=lambda kind, name: {"data": {"oauth-token": base64.b64encode(b"example").decode()}},
            delete=lambda kind, name: self.deleted.append((kind, name)),
        )

    async def _require_ready(self, spec: Any) -> None:
        return None

    async def _resolve_image(self, spec: Any, *, use_backoff: bool = False) -> str:
        return f"{spec.image}@sha256:abc"

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    async def _call_with_backoff(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    async def _resolve_plan(self, plan: Any, *, broad: bool | None = None) -> Any:
        return replace(plan, cidrs=("160.79.104.10/32",))

    def _policy_body(
        self, name: str, labels: Any, attempt_id: str, role: str, plan: Any, pod_selector: Any
    ) -> dict[str, Any]:
        return k8sspec.egress_policy(
            name=name,
            namespace="crucible-workers",
            object_labels=labels,
            attempt_id=attempt_id,
            role=role,
            plan=plan,
            dns_server="10.96.0.10",
            pod_selector=pod_selector,
        )

    async def _create_with_backoff(self, kind: str, body: Mapping[str, Any]) -> None:
        self.created.append((kind, dict(body)))


def test_the_kubernetes_launcher_creates_the_runner_job_through_the_provider() -> None:
    provider = _StubKubernetesProvider()
    launcher = KubernetesRoomLauncher(
        cast(Any, provider), script_path=str(REPO / "tools/room_runner.py")
    )
    launch = RoomRunnerLaunch(
        room_id="01ROOM208ROOMS00000000001",
        harness="claude_code",
        model="claude-sonnet-5",
        image="crucible-worker:x",
        api_url="http://crucible-api.crucible.svc:8080",
        idle_timeout_seconds=1800,
        egress_hosts=("api.anthropic.com",),
        token="crr_01ROOM208ROOMS00000000001.secret",
    )
    handle = asyncio.run(launcher.launch(launch))
    assert handle == "kubernetes:room-01room208rooms00000000001"
    assert [kind for kind, _ in provider.created] == [
        "secrets",
        "configmaps",
        "networkpolicies",
        "jobs",
    ]
    bodies = dict(provider.created)
    secret = bodies["secrets"]
    assert set(secret["data"]) == {"oauth-token", "token"}
    for body in bodies.values():
        labels = body["metadata"]["labels"]
        assert (
            labels[LABEL_ROOM] == launch.room_id and labels[k8sspec.LABEL_ROLE] == ROLE_ROOM_RUNNER
        )
        assert k8sspec.LABEL_ATTEMPT not in labels
    assert "room_runner.py" in bodies["configmaps"]["data"]
    egress = bodies["networkpolicies"]["spec"]["egress"]
    assert egress[-1] == {
        "to": [
            {
                "namespaceSelector": {"matchLabels": {k8sspec.NAMESPACE_NAME_LABEL: "crucible"}},
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/component": "api",
                        "app.kubernetes.io/name": "crucible",
                    }
                },
            }
        ],
        "ports": [{"protocol": "TCP", "port": 8080}],
    }
    job = bodies["jobs"]
    assert job["spec"]["backoffLimit"] == 0
    assert (
        job["spec"]["template"]["spec"]["containers"][0]["image"] == "crucible-worker:x@sha256:abc"
    )
    asyncio.run(launcher.stop(handle))
    assert {kind for kind, _ in provider.deleted} == {
        "jobs",
        "networkpolicies",
        "configmaps",
        "secrets",
    }
    with pytest.raises(RoomRunnerError, match="pod labels"):
        api_rule(k8sspec.PeerSelector.of("crucible", {}), 8080)


def test_the_docker_container_is_the_same_launch(tmp_path: Path) -> None:
    from crucible.adapters.execution.room_launch import DockerRoomLauncher  # noqa: PLC0415

    provider = DockerProvider(
        DockerConfig(
            endpoint="tcp://docker-socket-proxy:2375",
            artifact_root=str(tmp_path),
            egress_proxy="http://egress-proxy:3128",
        ),
        client=cast(Any, object()),
    )
    launcher = DockerRoomLauncher(provider)
    credential = default_registry().require("claude_code").credential_spec()
    assert credential is not None
    launch = RoomRunnerLaunch(
        room_id="01ROOM208ROOMS00000000001",
        harness="claude_code",
        model="m",
        image="crucible-worker:x",
        api_url="http://crucible:8080",
        idle_timeout_seconds=600,
        egress_hosts=("api.anthropic.com", "pypi.org"),
        token="crr_secret",
    )
    body = launcher.body(launch, credential, image="crucible-worker@sha256:abc")
    assert body["Cmd"] == runner_command() and body["WorkingDir"] == RUNNER_DIR
    env = dict(e.split("=", 1) for e in body["Env"])
    assert env["HTTPS_PROXY"] == "http://egress-proxy:3128"
    assert (
        env["CRUCIBLE_ENV_FROM_FILES"]
        == runner_env(launch, credential, egress_hosts=())["CRUCIBLE_ENV_FROM_FILES"]
    )
    host = body["HostConfig"]
    assert host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"]
    assert RUNNER_CONFIG_DIR in host["Tmpfs"]
    targets = {m["Target"]: m["ReadOnly"] for m in host["Mounts"]}
    assert targets == {RUNNER_DIR: True, credential.mount_target: True, RUNNER_TOKEN_DIR: True}
    assert "crr_secret" not in json.dumps(body)


# ----- AC2, the real runner against the protocol, with a fake SDK client --------------


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class StreamEvent:
    def __init__(self, text: str) -> None:
        self.event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}


class AssistantMessage:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class ResultMessage:
    def __init__(self, session_id: str, *, is_error: bool = False) -> None:
        self.session_id = session_id
        self.subtype = "error_during_execution" if is_error else "success"
        self.is_error = is_error


class _FakeClient:
    """The ClaudeSDKClient surface the runner uses: each query answers with deltas."""

    def __init__(self, runner: list[room_runner.RoomRunner], resume: str | None) -> None:
        self.runner = runner
        self.resume = resume
        self.prompts: list[str] = []
        self.interrupted = asyncio.Event()
        self.disconnected = False

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        prompt = self.prompts[-1]
        if prompt.startswith("tool:"):
            await self.runner[0].pre_tool_use(
                {"tool_name": "mcp__hades__hades_recall", "tool_input": {"subject": "rooms"}},
                "toolu_1",
                None,
            )
            yield StreamEvent("recalled")
            yield ResultMessage("sess-a")
            return
        if prompt.startswith("long:"):
            yield StreamEvent("one ")
            # A long generation, until the runner interrupts it.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.interrupted.wait(), 10)
            yield ResultMessage("sess-a", is_error=True)
            return
        yield StreamEvent("Hello ")
        yield StreamEvent("Scott.")
        yield AssistantMessage("Hello Scott.")
        yield ResultMessage("sess-a")

    async def interrupt(self) -> None:
        self.interrupted.set()

    async def disconnect(self) -> None:
        self.disconnected = True


def _transport(client: TestClient, token: str) -> room_runner.Transport:
    def call(
        method: str, path: str, body: Mapping[str, Any] | None, timeout: float
    ) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        response = client.request(method, f"/v1{path}", json=body, headers=headers)
        return response.status_code, response.json() if response.content else None

    return call


def _runner(
    api: _Api, room_id: str, *, idle: int = 1800
) -> tuple[room_runner.RoomRunner, list[_FakeClient]]:
    config = room_runner.RunnerConfig(
        api_url="http://hades",
        room_id=room_id,
        token=api.launcher.token,
        model="m",
        cwd="/tmp/room",
        cli_path="/usr/local/bin/claude",
        config_dir="/crucible/room-config",
        idle_timeout_seconds=idle,
        poll_seconds=0,
        turn_poll_seconds=0,
    )
    hades = room_runner.Hades(room_id, _transport(api.client, api.launcher.token))
    ref: list[room_runner.RoomRunner] = []
    clients: list[_FakeClient] = []

    async def factory(resume: str | None) -> room_runner.Client:
        client = _FakeClient(ref, resume)
        clients.append(client)
        return client

    runner = room_runner.RoomRunner(config, hades, factory)
    ref.append(runner)
    return runner, clients


async def _until(predicate: Any) -> None:
    await async_wait_until(predicate, timeout=10, describe="the runner to reach the record")


def test_the_room_runner_answers_records_tools_stops_on_interrupt_and_exits_on_stop() -> None:
    api = _Api()
    room = api.create()
    rid = room["id"]
    api.say(rid, "Say hello.")
    runner, clients = _runner(api, rid)

    async def drive() -> str:
        task = asyncio.create_task(runner.run())
        await _until(lambda: (t := api.store.room_turns.get(rid, 2)) is not None and not t.open)
        api.say(rid, "tool: what do you remember?")
        await _until(lambda: (t := api.store.room_turns.get(rid, 4)) is not None and not t.open)
        api.say(rid, "long: count to three hundred")
        await _until(
            lambda: (t := api.store.room_turns.get(rid, 6)) is not None and "one" in t.text
        )
        assert api.client.post(f"/v1/rooms/{rid}/interrupt").status_code == 200
        await _until(lambda: not cast(RoomTurn, api.store.room_turns.get(rid, 6)).open)
        api.client.post(f"/v1/rooms/{rid}/close")
        return await asyncio.wait_for(task, 10)

    reason = asyncio.run(drive())
    assert reason in ("stop", "refused (401)")
    answered = cast(RoomTurn, api.store.room_turns.get(rid, 2))
    assert answered.text == "Hello Scott." and not answered.interrupted
    tool_turn = cast(RoomTurn, api.store.room_turns.get(rid, 4))
    assert tool_turn.text == "recalled"
    assert [(c["name"], c["input"], c["tool_use_id"]) for c in tool_turn.tool_calls] == [
        ("mcp__hades__hades_recall", {"subject": "rooms"}, "toolu_1")
    ]
    stopped = cast(RoomTurn, api.store.room_turns.get(rid, 6))
    assert stopped.interrupted and stopped.text.startswith("one")
    assert clients[0].interrupted.is_set() and clients[0].disconnected
    assert clients[0].prompts == [
        "Say hello.",
        "tool: what do you remember?",
        "long: count to three hundred",
    ]
    assert api.room(rid).state is RoomState.CLOSED


def test_the_room_runner_exits_after_its_idle_timeout_and_says_so() -> None:
    api = _Api()
    room = api.create()
    api.say(room["id"], "hello")
    runner, _clients = _runner(api, room["id"], idle=0)
    assert asyncio.run(runner.run()) == "idle"
    assert api.room(room["id"]).state is RoomState.IDLE
    assert api.launcher.stops


def test_the_runner_rebuilds_a_resumed_client_when_the_child_dies() -> None:
    api = _Api()
    room = api.create()
    rid = room["id"]
    api.say(rid, "hello")
    runner, clients = _runner(api, rid)
    runner.session_id = "sess-before"

    class ProcessError(Exception):
        pass

    async def drive() -> None:
        client = clients[0] if clients else await runner.client_factory(None)

        async def broken() -> AsyncIterator[Any]:
            yield StreamEvent("par")
            raise ProcessError("Command failed with exit code -9")

        client.receive_response = broken  # type: ignore[method-assign]
        inbox = await asyncio.to_thread(runner.hades.inbox, 0)
        replacement = await runner.answer(client, inbox["message"])
        assert replacement is not client and cast(_FakeClient, replacement).resume == "sess-before"

    asyncio.run(drive())
    turn = cast(RoomTurn, api.store.room_turns.get(rid, 2))
    assert turn.interrupted and turn.text == "par" and turn.ended_at is not None


def test_an_interrupt_watcher_failure_stops_the_model_and_propagates() -> None:
    api = _Api()
    room = api.create()
    rid = room["id"]
    api.say(rid, "long: keep going")
    runner, clients = _runner(api, rid)

    async def drive() -> None:
        client = await runner.client_factory(None)
        message = await asyncio.to_thread(runner.hades.inbox, 0)

        def failed_poll(_wait: int) -> dict[str, Any]:
            raise room_runner.HadesError(503, "temporary inbox failure")

        runner.hades.inbox = failed_poll  # type: ignore[assignment]
        with pytest.raises(room_runner.HadesError, match="temporary inbox failure"):
            await runner.answer(client, message["message"])

    asyncio.run(drive())
    assert clients[0].interrupted.is_set()
    # A failed watcher must not post a successful end and move the room back to warm.
    assert cast(RoomTurn, api.store.room_turns.get(rid, 2)).open
    assert api.room(rid).state is RoomState.WARM


# ----- AC3: the session start -----------------------------------------------------------


def _room(kind: RoomKind = RoomKind.PRINCIPAL) -> Room:
    return Room(
        id="01ROOM208ROOMS00000000009",
        kind=kind,
        harness="claude_code",
        model="claude-sonnet-5",
        state=RoomState.IDLE,
        created_at=NOW,
        last_activity_at=NOW,
        created_by=OPERATOR.id,
        card_task_id=CARD_ID if kind is RoomKind.CARD else None,
        scope_task_ids=[CARD_ID] if kind is RoomKind.CARD else [],
    )


def _turns(count: int, *, start: datetime = NOW) -> list[RoomTurn]:
    roles = (TurnRole.USER, TurnRole.ASSISTANT)
    return [
        RoomTurn(
            id=f"01TURN{n:020d}",
            room_id="01ROOM208ROOMS00000000009",
            seq=n,
            role=roles[n % 2 == 0],
            text=f"words of turn {n} about the cluster",
            started_at=start + timedelta(minutes=n),
            ended_at=start + timedelta(minutes=n),
        )
        for n in range(1, count + 1)
    ]


CARD = CardContext(
    task_id=CARD_ID,
    external_id="FDY-0590",
    title="Rooms and the room runner",
    project="hades",
    state="proposed",
    objective="Hades owns the transcript.",
)


def test_the_identity_is_config_principal_identity_md_and_comes_first() -> None:
    text = read_identity()
    assert text == (REPO / "config" / "principal" / "IDENTITY.md").read_text(encoding="utf-8")
    assert "Scott's principal" in text and EM_DASH not in text
    assert "never start work without a recorded decision" in text
    assert "America/Chicago" in text and "hades_file_card" in text
    start = assemble(identity=text, room=_room(), card=None, recall=[], decisions=[], turns=[])
    assert start.text.startswith("# Hades")
    assert start.sections[0] == text.strip()


def test_the_subject_is_the_card_for_a_card_room_and_the_last_turns_otherwise() -> None:
    assert subject_for(_room(RoomKind.CARD), CARD, _turns(3)) == (
        "Rooms and the room runner Hades owns the transcript."
    )
    principal = subject_for(_room(), None, _turns(10))
    assert principal.startswith("words of turn 5") and principal.endswith(
        "turn 10 about the cluster"
    )
    long = [replace(t, text="x" * 900) for t in _turns(4)]
    assert len(subject_for(_room(), None, long)) == 1024


def test_the_recall_section_lists_memory_with_its_source_and_local_time() -> None:
    item = MemoryItem(
        id="01MEM0208ROOMS00000000002",
        text="The lab cluster has one node.",
        source="operator",
        observed_at=NOW,
        scope_tags=[],
        promoted_by="scott",
        promoted_at=NOW,
    )
    assert recall_section([item]) == (
        "## What Hades remembers about this\n"
        "- The lab cluster has one node. (operator, observed 2026-10-09 10:00 AM CDT)"
    )
    assert "Nothing in memory" in recall_section([])
    store = _Store()
    store.memory.rows.append(item)
    start = session_start(store.uow(), _room(), identity=IDENTITY)
    assert "The lab cluster has one node." in start.recall


def test_the_decisions_touching_the_room_are_its_names_and_its_subject_words() -> None:
    def line(n: int, verbatim: str, applies: list[str]) -> LedgerDecision:
        return LedgerDecision(
            id=f"01LEDG{n:020d}",
            principal="scott",
            channel="telegram",
            said_at=NOW + timedelta(minutes=n),
            verbatim=verbatim,
            transcript_ref=None,
            applies_to=applies,
        )

    named = line(1, "Ship it", [CARD_ID])
    worded = line(2, "The cluster stays at one node", [])
    unrelated = line(3, "Buy milk", ["home"])
    found = decisions_touching(
        [named, worded, unrelated], names=[CARD_ID], subject="what about the cluster"
    )
    assert found == [worded, named]
    store = _Store()
    store.decision_ledger.rows.extend([named, worded, unrelated])
    start = session_start(store.uow(), _room(RoomKind.CARD), identity=IDENTITY)
    assert '"Ship it"' in start.decisions and "Buy milk" not in start.decisions
    assert "## The card\nFDY-0590: Rooms and the room runner" in start.card
    assert CARD_OBJECTIVE in start.card


def test_the_last_turns_are_verbatim_and_bounded() -> None:
    turns = _turns(30)
    older, recent = split_window(turns, 20)
    assert [t.seq for t in recent] == list(range(11, 31)) and len(older) == 10
    start = assemble(
        identity=IDENTITY, room=_room(), card=None, recall=[], decisions=[], turns=turns, window=20
    )
    assert start.turns.startswith("## The last 20 turns, verbatim")
    assert (
        "[#11 Operator, 2026-10-09 10:11 AM CDT]\nwords of turn 11 about the cluster" in start.turns
    )
    assert "[#30 Hades" in start.turns and "#10 " not in start.turns
    assert start.text.index(start.summary) < start.text.index(start.turns)


def test_the_rolling_summary_covers_exactly_the_older_turns() -> None:
    older = _turns(50)
    older[3].tool_calls = [{"name": "mcp__hades__hades_recall"}]
    older[3].decision_id = "01LEDG00000000000000000001"
    older[4].interrupted = True
    summary = rolling_summary(older, lines=40)
    lines = summary.splitlines()
    assert lines[0] == "## Earlier in this room"
    assert lines[1] == "50 earlier turns, from 2026-10-09 10:01 AM CDT to 2026-10-09 10:50 AM CDT."
    assert lines[2] == "(10 older turns are not shown.)"
    assert lines[3].startswith("- #11 ") and lines[-1].startswith("- #50 Hades")
    assert len(lines) == 43
    full = rolling_summary(older[:6], lines=40)
    assert (
        "- #4 Hades, 2026-10-09 10:04 AM CDT: words of turn 4 about the cluster "
        "[called mcp__hades__hades_recall; decision 01LEDG00000000000000000001 recorded]" in full
    )
    assert "[interrupted]" in full and "not shown" not in full
    clipped = rolling_summary([replace(older[0], text="word " * 200)])
    assert clipped.splitlines()[-1].endswith("...") and len(clipped.splitlines()[-1]) < 260
    assert rolling_summary([]) == ""
    # The window moves as the room grows: what the summary covers, the verbatim does not.
    start = assemble(
        identity=IDENTITY,
        room=_room(),
        card=None,
        recall=[],
        decisions=[],
        turns=_turns(25),
        window=20,
    )
    assert "5 earlier turns" in start.summary and "- #5 " in start.summary
    assert "- #6 " not in start.summary and "[#6 Hades" in start.turns


def test_session_start_summarizes_the_complete_transcript_before_its_window() -> None:
    store = _Store()
    turns = _turns(260)
    store.room_turns.rows.extend(turns)
    room = replace(_room(), inbox_cursor=260)
    start = session_start(store.uow(), room, identity=IDENTITY)
    assert (
        "240 earlier turns, from 2026-10-09 10:01 AM CDT to 2026-10-09 2:00 PM CDT."
        in start.summary
    )
    assert "(200 older turns are not shown.)" in start.summary
    assert "- #201 " in start.summary and "- #240 " in start.summary
    assert "[#241 " in start.turns and "[#260 " in start.turns


def test_the_session_start_is_the_same_assembly_for_every_room_kind() -> None:
    store = _Store()
    principal = session_start(store.uow(), _room(), identity=IDENTITY)
    card = session_start(store.uow(), _room(RoomKind.CARD), identity=IDENTITY)
    order = ["identity", "room", "card", "recall", "decisions", "summary", "turns"]
    for start in (principal, card):
        assert start.sections == tuple(getattr(start, name) for name in order)
    assert principal.card == "" and card.card.startswith("## The card")
    assert "the principal room" in principal.room and "card room about FDY-0590" in card.room


# ----- AC4: the tools, the token, the hook ----------------------------------------------


def test_the_sdk_options_are_the_ones_the_spike_proved() -> None:
    config = room_runner.RunnerConfig(
        api_url="http://hades",
        room_id="r",
        token="crr_x",
        model="m",
        cwd="/home/worker/rooms/r",
        cli_path="/usr/local/bin/claude",
        config_dir="/crucible/room-config",
        idle_timeout_seconds=60,
        oauth_token="sk-ant-oat01-example",
    )
    session = {
        "model": "claude-sonnet-5",
        "allowed_tools": list(ALLOWED_TOOLS),
        "disallowed_tools": list(DISALLOWED_TOOLS),
        "system_prompt": "the session start",
    }
    kwargs = room_runner.options_kwargs(config, session, server="SERVER", pre_tool_use="HOOK")
    assert kwargs["cli_path"] == "/usr/local/bin/claude"
    assert kwargs["cwd"] == "/home/worker/rooms/r"
    assert kwargs["tools"] == []
    assert kwargs["permission_mode"] == "dontAsk"
    assert kwargs["extra_args"] == {"permission-prompts": "none"}
    assert kwargs["allowed_tools"] == [f"mcp__hades__{name}" for name in HADES_TOOLS]
    assert kwargs["disallowed_tools"] == [
        "Bash",
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
        "WebFetch",
        "WebSearch",
        "Agent",
    ]
    assert kwargs["include_partial_messages"] is True
    assert kwargs["mcp_servers"] == {"hades": "SERVER"}
    assert kwargs["hooks"] == {"PreToolUse": ["HOOK"]}
    assert kwargs["system_prompt"] == {
        "type": "preset",
        "preset": "claude_code",
        "append": "the session start",
    }
    assert kwargs["env"] == {
        "CLAUDE_CONFIG_DIR": "/crucible/room-config",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-example",
    }
    assert "resume" not in kwargs
    assert (
        room_runner.options_kwargs(config, session, server=1, pre_tool_use=2, resume="s")["resume"]
        == "s"
    )
    assert set(room_runner.TOOLS) == set(HADES_TOOLS)
    assert "hades_answer_question" not in room_runner.TOOLS


def test_the_runner_scrubs_the_parents_claude_and_anthropic_variables(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("crr_abc.def\n", encoding="utf-8")
    environ = {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-example",
        "CLAUDE_CODE_SESSION_ID": "parent",
        "CLAUDECODE": "1",
        "ANTHROPIC_API_KEY": "nope",
        "HADES_API_URL": "http://hades/",
        "HADES_ROOM_ID": "r",
        "HADES_ROOM_TOKEN_FILE": str(token_file),
        "ROOM_CWD": "/home/worker/rooms/r",
        "ROOM_CLAUDE_CONFIG_DIR": "/crucible/room-config",
        "PATH": "/usr/bin",
    }
    config = room_runner.RunnerConfig.from_environment(environ)
    assert sorted(environ) == [
        "HADES_API_URL",
        "HADES_ROOM_ID",
        "HADES_ROOM_TOKEN_FILE",
        "PATH",
        "ROOM_CLAUDE_CONFIG_DIR",
        "ROOM_CWD",
    ]
    assert config.oauth_token == "sk-ant-oat01-example" and config.token == "crr_abc.def"
    assert config.api_url == "http://hades" and "sk-ant" not in repr(config)


def test_a_runner_token_acts_only_in_its_own_room() -> None:
    api = _Api()
    mine = api.create()
    other = api.create()
    api.say(mine["id"], "hello")
    token = api.launcher.token
    runner = api.runner(token)
    assert runner.get(f"/v1/rooms/{mine['id']}/inbox?wait=0").status_code == 200
    assert runner.get(f"/v1/rooms/{other['id']}/inbox?wait=0").status_code == 403
    assert runner.get(f"/v1/rooms/{other['id']}/session").status_code == 403
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert api.runner(tampered).get(f"/v1/rooms/{mine['id']}/inbox?wait=0").status_code == 401
    # An ordinary bearer token is not a runner token.
    assert (
        api.runner("cru_01OPER208ROOMS00000000001.x")
        .get(f"/v1/rooms/{mine['id']}/session")
        .status_code
        == 401
    )
    assert api.client.get(f"/v1/rooms/{mine['id']}/inbox?wait=0").status_code == 401
    # And a runner token is not an ordinary bearer token anywhere else.
    from crucible.application.auth import authenticate  # noqa: PLC0415

    assert authenticate(api.store.uow(), token) is None
    store = api.store
    room = cast(Room, store.rooms.get(mine["id"]))
    with pytest.raises(ForbiddenError):
        authenticate_runner(store.uow(), other["id"], token)
    assert authenticate_runner(store.uow(), mine["id"], token).id == room.id
    minted = mint_runner_token(room)
    assert minted.startswith(f"crr_{room.id}.") and room.runner_key_digest is not None


def _tool(api: _Api, room_id: str, name: str, **arguments: Any) -> Any:
    return api.runner().post(f"/v1/rooms/{room_id}/tools/{name}", {"arguments": arguments})


def test_the_tools_read_and_note_only_the_tasks_the_room_names() -> None:
    api = _Api()
    room = api.create(kind="card", card_task_id="FDY-0590")
    api.say(room["id"], "hello")
    read = _tool(api, room["id"], "hades_read_task", task_id="FDY-0590")
    assert read.status_code == 200
    assert read.json()["result"]["objective"] == CARD_OBJECTIVE
    assert _tool(api, room["id"], "hades_read_task", task_id="FDY-0591").status_code == 403
    assert _tool(api, room["id"], "hades_read_task", task_id=OTHER_ID).status_code == 403
    assert _tool(api, room["id"], "hades_post_note", task_id=OTHER_ID, text="x").status_code == 403
    assert _tool(api, room["id"], "hades_read_task", task_id="FDY-9999").status_code == 404
    note = _tool(api, room["id"], "hades_post_note", task_id=CARD_ID, text="The runner is up.")
    assert note.status_code == 200
    [stored] = api.store.task_notes.rows
    assert stored.task_id == CARD_ID and stored.author == "scott" and stored.verbatim is False
    assert _tool(api, room["id"], "hades_answer_question", task_id=CARD_ID).status_code == 404
    principal = api.create()
    api.say(principal["id"], "hi")
    assert _tool(api, principal["id"], "hades_read_task", task_id=CARD_ID).status_code == 403


def test_a_decision_is_the_operators_words_from_this_room_and_its_tasks() -> None:
    api = _Api()
    room = api.create(kind="card", card_task_id="FDY-0590")
    api.say(room["id"], "Build the  mvp, and keep the emptyDir for now.")
    invented = _tool(api, room["id"], "hades_record_decision", verbatim="Scott approves everything")
    assert invented.status_code == 409
    outside = _tool(
        api, room["id"], "hades_record_decision", verbatim="Build the mvp", applies_to=["FDY-0591"]
    )
    assert outside.status_code == 403
    recorded = _tool(
        api,
        room["id"],
        "hades_record_decision",
        verbatim="build the mvp, and keep the emptyDir",
        applies_to=["FDY-0590", "project:hades"],
    )
    assert recorded.status_code == 200, recorded.text
    [line] = api.store.decision_ledger.rows
    assert line.channel == "room" and line.principal == "scott"
    assert line.applies_to == [room["id"], "FDY-0590", "project:hades"]
    assert line.transcript_ref == f"/v1/rooms/{room['id']}#turn-1"
    assert cast(RoomTurn, api.store.room_turns.get(room["id"], 1)).decision_id == line.id
    assert recorded.json()["result"]["decision_id"] == line.id


def test_filing_a_card_proposes_a_task_and_adds_it_to_the_rooms_scope() -> None:
    store = _Store()
    room = _room()
    store.rooms.add(room)
    submitted: list[dict[str, Any]] = []

    def submit(
        uow: Any, clock: Any, *, principal: Principal, body: dict[str, Any], proposed: bool
    ) -> tuple[Task, Any]:
        assert proposed is True and principal == OPERATOR
        submitted.append(body)
        task = _task("01TASK208R00MS000000000003", body["external_id"], body["title"])
        store.tasks.rows[task.id] = task
        return task, None

    result = room_tools.tool_file_card(
        store.uow(),
        FakeClock(NOW),
        room,
        {"title": "The room page", "objective": "Show a room.", "project": "hades"},
        submit=submit,
    )
    assert result["external_id"] == "FDY-0592" and result["state"] == "proposed"
    [body] = submitted
    assert body["title"] == "The room page" and body["objective"] == "Show a room."
    assert "work_branch" not in body["repository"] and body["correction"] is None
    assert body["acceptance_criteria"] == [
        {"id": "AC1", "text": "The objective is met: The room page"}
    ]
    assert "Filed from room" in body["execution_request"]["rationale"]
    assert cast(Room, store.rooms.get(room.id)).scope_task_ids == ["01TASK208R00MS000000000003"]
    assert room_tools.next_external_id("FDY-0590", ["FDY-0591", "ABC-9999"]) == "FDY-0592"
    with pytest.raises(NotFoundError):
        room_tools.tool_file_card(
            store.uow(), FakeClock(NOW), room, {"title": "t", "objective": "o", "project": "none"}
        )


def test_the_recall_tool_is_the_memory_recall() -> None:
    api = _Api()
    room = api.create()
    api.say(room["id"], "hi")
    api.store.memory.rows.append(
        MemoryItem(
            id="01MEM0208ROOMS00000000003",
            text="Scott prefers local times.",
            source="operator",
            observed_at=NOW,
            scope_tags=["style"],
            promoted_by="scott",
            promoted_at=NOW,
        )
    )
    found = _tool(api, room["id"], "hades_recall", subject="what times", tags=["Style"]).json()
    assert [i["text"] for i in found["result"]["items"]] == ["Scott prefers local times."]
    assert found["result"]["tags"] == ["style"]


def test_the_pre_tool_use_hook_records_every_call_on_the_turn() -> None:
    api = _Api()
    room = api.create()
    api.say(room["id"], "hi")
    runner, _clients = _runner(api, room["id"])
    inbox = runner.hades.inbox(0)
    runner.current_seq = inbox["message"]["assistant_seq"]
    asyncio.run(
        runner.pre_tool_use(
            {"tool_name": "Read", "tool_input": {"file_path": "/etc/hostname"}}, "t1", None
        )
    )
    asyncio.run(
        runner.pre_tool_use({"tool_name": "mcp__hades__hades_recall", "tool_input": {}}, "t2", None)
    )
    turn = cast(RoomTurn, api.store.room_turns.get(room["id"], 2))
    assert [(c["name"], c["tool_use_id"]) for c in turn.tool_calls] == [
        ("Read", "t1"),
        ("mcp__hades__hades_recall", "t2"),
    ]
    result = asyncio.run(runner.call_tool("hades_read_task", {"task_id": OTHER_ID}))
    assert result["is_error"] is True and "403" in result["content"][0]["text"]


# ----- AC5: the harness refusal and the idle timeout setting ---------------------------


def test_a_harness_other_than_claude_code_is_refused_with_409() -> None:
    api = _Api()
    for harness in ("codex", "hermes", "agy", "qwen_code", "something"):
        response = api.client.post(
            "/v1/rooms", json={"kind": "principal", "harness": harness, "model": "m"}
        )
        assert response.status_code == 409
        assert response.json()["detail"].startswith(f"{harness} is not yet a room harness")
    room = api.create()
    switched = api.client.post(
        f"/v1/rooms/{room['id']}/switch", json={"harness": "codex", "model": "gpt"}
    )
    assert switched.status_code == 409 and "not yet a room harness" in switched.json()["detail"]
    assert api.room(room["id"]).harness == "claude_code"
    assert api.store.room_turns.of(room["id"]) == []


def test_the_idle_timeout_setting_defaults_to_30_and_a_saved_value_wins() -> None:
    store = _Store()
    value = idle_timeout_value(store.uow())
    assert (value.value, value.source, value.applies) == (30, "default", "next launch")
    assert idle_timeout_value(store.uow(), seed=45).value == 45
    save_idle_timeout(store.uow(), FakeClock(NOW), principal=ADMIN, minutes=10, reason="shorter")
    assert store.provider_settings.rows[IDLE_SETTING].document == {"minutes": 10}
    assert idle_timeout(store.uow(), 45) == timedelta(minutes=10)
    assert "room_idle_timeout_updated" in store.kinds()
    with pytest.raises(Exception, match="between 1 and"):
        save_idle_timeout(store.uow(), FakeClock(NOW), principal=ADMIN, minutes=0, reason="x")
    from crucible.adapters.ui.pages import settings as settings_page  # noqa: PLC0415

    api = _Api()
    row = settings_page._room_idle_row(cast(Any, api.ctx), cast(Any, api.store), ADMIN)
    assert row[0] == "rooms.idle_timeout_minutes" and row[1] == 30
    assert row[4]["action"] == "/ui/actions/room-idle-timeout"


def test_the_docs_describe_the_lifecycle_the_launch_the_emptydir_and_the_setting() -> None:
    spec = (REPO / "docs" / "spec" / "28-rooms.md").read_text(encoding="utf-8")
    for words in (
        "idle",
        "starting",
        "warm",
        "interrupted",
        "closed",
        "uv run --no-project --with claude-agent-sdk python tools/room_runner.py",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "emptyDir",
        "rooms.idle_timeout_minutes",
        "hades_answer_question",
        "permission-prompts",
    ):
        assert words in spec, words
    for path in (
        REPO / "docs" / "spec" / "28-rooms.md",
        REPO / "docs" / "adr" / "0031-rooms-hades-owns-the-transcript.md",
        REPO / "config" / "principal" / "IDENTITY.md",
        REPO / "tools" / "room_runner.py",
    ):
        assert EM_DASH not in path.read_text(encoding="utf-8"), path
    assert "28-rooms.md" in (REPO / "docs" / "README.md").read_text(encoding="utf-8")


def test_local_times_and_the_switch_line_are_in_central_time() -> None:
    assert room_local_time(NOW) == "2026-10-09 10:00 AM CDT"
    assert room_local_time(datetime(2026, 12, 1, 18, 5, tzinfo=UTC)) == "2026-12-01 12:05 PM CST"
    assert switch_text("claude_code", "claude-sonnet-5", NOW) == (
        "Switched to claude_code (claude-sonnet-5) at 2026-10-09 10:00 AM CDT. "
        "Same history, same memory."
    )


def test_the_interrupt_of_a_room_whose_runner_is_gone_ends_the_turn_itself() -> None:
    api = _Api()
    room = api.create()
    api.say(room["id"], "one")
    runner = api.runner()
    runner.inbox(room["id"])
    stored = api.room(room["id"])
    stored.state = RoomState.IDLE
    api.store.rooms.save(stored)
    response = api.client.post(f"/v1/rooms/{room['id']}/interrupt")
    assert response.status_code == 200 and response.json()["state"] == "idle"
    assert cast(RoomTurn, api.store.room_turns.get(room["id"], 2)).interrupted


def test_conflicts_are_409_and_unknowns_404() -> None:
    api = _Api()
    assert api.client.get("/v1/rooms/01NOPE208ROOMS00000000001").status_code == 404
    room = api.create()
    assert api.client.post(f"/v1/rooms/{room['id']}/interrupt").status_code == 409
    with pytest.raises(ConflictError):
        from crucible.application.rooms import interrupt_room  # noqa: PLC0415

        interrupt_room(api.store.uow(), api.clock, principal=OPERATOR, room_id=room["id"])


def test_the_wiring_picks_the_provider_that_runs_room_runners(tmp_path: Path) -> None:
    from crucible.adapters.execution.room_launch import DockerRoomLauncher  # noqa: PLC0415
    from crucible.cli.wiring import build_rooms  # noqa: PLC0415
    from crucible.settings import Settings  # noqa: PLC0415

    settings = Settings(rooms={"api_url": "http://crucible:8080", "idle_timeout_minutes": 20})
    docker = DockerProvider(
        DockerConfig(endpoint="tcp://proxy:2375", artifact_root=str(tmp_path)),
        client=cast(Any, object()),
    )
    rooms = build_rooms(settings, {"docker": docker}, default_registry())
    assert isinstance(rooms.launcher, DockerRoomLauncher)
    assert rooms.config.api_url == "http://crucible:8080"
    assert rooms.config.idle_timeout_minutes == 20
    assert rooms.config.egress_hosts == ("api.anthropic.com", "pypi.org", "files.pythonhosted.org")
    assert build_rooms(Settings(), {}, default_registry()).launcher is None
