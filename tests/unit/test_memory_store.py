"""hades #208 (FDY-0587): the shared memory store, the recall API, the append-only
decision ledger, the task-decision mirror, and the Admin Memory page.

Transcripts stay per channel. Decisions and memory are shared by every channel and every
persona. Minion findings become memory only when Hades or the operator promotes them.

AC1: migration 0058 creates both tables and is the single head (the populated-database
run is tests/integration/test_migrations.py, CI's tier). AC2: recall is bounded, newest
first, matches scope tags and subject words, and never returns a superseded item. AC3:
promote, supersede, forget and decision append are role-checked; the ledger has no edit
and no delete anywhere. AC4: a decision recorded on a task is mirrored into the ledger
with channel `task` and the task id in `applies_to`. AC5: the page renders both tabs
from the same services with local times and click-only Edit and Forget.
"""

from __future__ import annotations

import io
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from starlette.testclient import TestClient

from crucible.adapters.api.deps import app_context, current_principal, unit_of_work
from crucible.adapters.api.problems import install_problem_handlers
from crucible.adapters.api.routers import memory as memory_router
from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.migrations.versions import (
    _0058_memory_and_decisions as m58,
)
from crucible.adapters.persistence.models import Base
from crucible.adapters.persistence.records import DecisionLedger, MemoryItems
from crucible.adapters.ui.pages import memory as memory_page
from crucible.application.decisions import record_decision
from crucible.application.errors import ConflictError, ForbiddenError, NotFoundError
from crucible.application.memory import (
    forget_memory,
    list_ledger_decisions,
    list_memory,
    promote_memory,
    recall_memory,
    record_ledger_decision,
    supersede_memory,
)
from crucible.contracts.api import (
    DecisionRequest,
    LedgerDecisionRequest,
    MemoryPromoteRequest,
    MemorySupersedeRequest,
)
from crucible.domain.entities import (
    Decision,
    Event,
    LedgerDecision,
    MemoryItem,
    Principal,
    Role,
    Task,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from crucible.domain.memory import (
    RECALL_MAX_LIMIT,
    matches,
    normalize_tags,
    recall,
    subject_keywords,
)
from crucible.ports.repository import DecisionLedgerRepository, MemoryRepository, UnitOfWork
from tests.fixtures import FakeClock

NOW = datetime(2026, 10, 8, 18, 30, tzinfo=UTC)
NOW_LOCAL = "2026-10-08 01:30:00 PM CDT"
TASK_ID = "01TASK208MEMORY0000000001"
PRINCIPAL_ID = "01PRIN208MEMORY0000000001"
ORCHESTRATOR = Principal(id=PRINCIPAL_ID, name="hades", role=Role.ORCHESTRATOR, created_at=NOW)
OPERATOR = Principal(
    id="01OPER208MEMORY0000000001", name="scott", role=Role.OPERATOR, created_at=NOW
)
ADMIN = Principal(id="01ADMN208MEMORY0000000001", name="root", role=Role.ADMIN, created_at=NOW)
OBSERVER = Principal(
    id="01OBSV208MEMORY0000000001", name="reader", role=Role.OBSERVER, created_at=NOW
)
EXPLANATION = (
    "Transcripts stay per channel. Decisions and memory are shared by every channel and "
    "every persona. Minion findings become memory only when Hades or Scott promotes them."
)


# ----- an in-memory store with the same rule as the SQL repositories ----------------


class _Memory:
    def __init__(self) -> None:
        self.rows: dict[str, MemoryItem] = {}

    def add(self, item: MemoryItem) -> None:
        self.rows[item.id] = item

    def get(self, item_id: str, *, for_update: bool = False) -> MemoryItem | None:
        return self.rows.get(item_id)

    def retire(self, item_id: str, *, superseded_by: str | None, at: datetime) -> None:
        row = self.rows[item_id]
        row.superseded_by = superseded_by
        row.superseded_at = at

    def recall(
        self, *, tags: Sequence[str], keywords: Sequence[str], limit: int
    ) -> list[MemoryItem]:
        # Oldest first on purpose: the service orders, the fake does not.
        wanted = [r for r in self.rows.values() if matches(r, tags=tags, keywords=keywords)]
        wanted.sort(key=lambda r: (r.observed_at, r.id))
        return wanted[-limit:]

    def list_recent(self, *, limit: int, include_superseded: bool = False) -> list[MemoryItem]:
        rows = [r for r in self.rows.values() if include_superseded or r.current]
        rows.sort(key=lambda r: (r.observed_at, r.id))
        return rows[-limit:]


class _Ledger:
    def __init__(self) -> None:
        self.rows: list[LedgerDecision] = []

    def add(self, decision: LedgerDecision) -> None:
        self.rows.append(decision)

    def list_recent(self, *, limit: int, channel: str | None = None) -> list[LedgerDecision]:
        rows = [r for r in self.rows if channel is None or r.channel == channel]
        return rows[-limit:]


class _Events:
    def __init__(self) -> None:
        self.rows: list[Event] = []

    def append(self, event: Event) -> Event:
        event.seq = len(self.rows) + 1
        self.rows.append(event)
        return event


class _Tasks:
    def __init__(self, task: Task | None) -> None:
        self.task = task

    def get(self, task_id: str, *, for_update: bool = False) -> Task | None:
        return self.task if self.task is not None and self.task.id == task_id else None


class _Decisions:
    def __init__(self) -> None:
        self.rows: list[Decision] = []

    def add(self, decision: Decision) -> None:
        self.rows.append(decision)

    def list_for_task(self, task_id: str) -> list[Decision]:
        return [d for d in self.rows if d.task_id == task_id]


class _Store:
    def __init__(self, task: Task | None = None) -> None:
        self.memory = _Memory()
        self.decision_ledger = _Ledger()
        self.events = _Events()
        self.tasks = _Tasks(task)
        self.decisions = _Decisions()
        self.escalations = SimpleNamespace(get=lambda *_a, **_k: None)
        self.committed = 0

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
        self.committed += 1

    def rollback(self) -> None:
        return None

    def set_fenced_token(self, fenced_token: int) -> None:
        return None

    def uow(self) -> UnitOfWork:
        return cast(UnitOfWork, self)


def _item(
    text: str,
    *,
    tags: Sequence[str] = (),
    age: timedelta = timedelta(0),
    item_id: str | None = None,
    superseded_at: datetime | None = None,
) -> MemoryItem:
    observed = NOW - age
    return MemoryItem(
        id=item_id or f"01MEM{abs(hash((text, age))) % 10**20:020d}"[:26].ljust(26, "0"),
        text=text,
        source="minion:FDY-0581",
        observed_at=observed,
        scope_tags=list(tags),
        promoted_by="hades",
        promoted_at=observed,
        superseded_at=superseded_at,
    )


def _task() -> Task:
    return Task(
        id=TASK_ID,
        external_id="FDY-0587",
        principal_id=PRINCIPAL_ID,
        project="hades",
        title="Memory and decisions",
        state=TaskState.BLOCKED,
        contract_version=1,
        policy_name="hades-self-hosting",
        policy_version=30,
        repository_id="01REPO208MEMORY0000000001",
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(minutes=5),
    )


def _kinds(store: _Store) -> list[str]:
    return [e.kind for e in store.events.rows]


# ----- AC1: migration 0058 ----------------------------------------------------------


def _render(step: Any) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        step()
    return buffer.getvalue()


def test_0058_is_the_single_head_and_follows_the_previous_one() -> None:
    script = ScriptDirectory.from_config(migrate.alembic_config("postgresql://unused/unused"))
    assert script.get_heads() == ["0058_memory_and_decisions"]
    revision = script.get_revision("0058_memory_and_decisions")
    assert revision is not None
    # hades #208 item 2 took 0057 (comment delivery); 0058 chains from it (hades #447).
    assert revision.down_revision == "0057_comment_delivery"
    assert m58.revision == "0058_memory_and_decisions"


def test_0058_creates_both_tables_with_the_named_columns_and_an_append_only_ledger() -> None:
    sql = _render(m58.upgrade)
    assert "CREATE TABLE memory_items" in sql
    assert "CREATE TABLE decision_ledger" in sql
    memory_columns = ("id", "text", "source", "observed_at", "scope_tags", "promoted_by")
    for column in (*memory_columns, "promoted_at", "superseded_by", "superseded_at"):
        assert re.search(rf"\b{column}\b", sql.split("CREATE TABLE decision_ledger")[0])
    ledger = sql.split("CREATE TABLE decision_ledger")[1]
    for column in ("id", "principal", "channel", "said_at", "verbatim", "transcript_ref"):
        assert re.search(rf"\b{column}\b", ledger)
    for column in ("applies_to", "acted_by", "acted_at"):
        assert re.search(rf"\b{column}\b", ledger)
    assert (
        "CREATE TRIGGER trg_decision_ledger_append_only BEFORE UPDATE OR DELETE "
        "ON decision_ledger" in sql
    )
    assert "crucible_reject_mutation()" in sql
    for kind in m58.EVENT_KINDS:
        assert f"'{kind}'" in sql
    down = _render(m58.downgrade)
    assert "DROP TABLE decision_ledger" in down and "DROP TABLE memory_items" in down
    assert "CREATE TABLE IF NOT EXISTS events_0058_archive" in down


def test_the_orm_mirrors_the_migration_and_the_event_kinds_match_the_enum() -> None:
    memory = Base.metadata.tables["memory_items"]
    assert set(memory.columns.keys()) == {
        "id",
        "text",
        "source",
        "observed_at",
        "scope_tags",
        "promoted_by",
        "promoted_at",
        "superseded_by",
        "superseded_at",
    }
    ledger = Base.metadata.tables["decision_ledger"]
    assert set(ledger.columns.keys()) == {
        "id",
        "principal",
        "channel",
        "said_at",
        "verbatim",
        "transcript_ref",
        "applies_to",
        "acted_by",
        "acted_at",
    }
    assert set(m58._event_kinds()) == {k.value for k in EventKind}
    assert {
        "memory_promoted",
        "memory_superseded",
        "memory_forgotten",
        "ledger_decision_recorded",
    } <= set(m58.EVENT_KINDS)


# ----- AC2: recall ------------------------------------------------------------------


def test_recall_matches_scope_tags_case_insensitively() -> None:
    hades = _item("The lab cluster has one node.", tags=["project:hades"])
    other = _item("The kettle is on the left.", tags=["home"])
    untagged = _item("No tags at all.")
    found = recall([hades, other, untagged], tags=["Project:Hades"])
    assert found == [hades]
    assert normalize_tags([" Project:Hades ", "project:hades", ""]) == ["project:hades"]


def test_recall_matches_the_subject_words_in_the_text() -> None:
    cluster = _item("The lab cluster has one node.")
    kettle = _item("The kettle is on the left.")
    assert subject_keywords("a Cluster of nodes") == ["cluster", "nodes"]
    assert subject_keywords("a to of") == []
    assert recall([cluster, kettle], subject="where is the Cluster") == [cluster]
    # Short words carry no meaning and a subject of only short words matches by tags
    # alone; with no tags either, that is an unfiltered recall.
    assert recall([cluster, kettle], subject="is on") == sorted(
        [cluster, kettle], key=lambda i: (i.observed_at, i.id), reverse=True
    )


def test_recall_is_newest_first_and_bounded() -> None:
    items = [
        _item(f"fact {n} about the cluster", age=timedelta(minutes=n), item_id=f"{n:026d}")
        for n in range(30)
    ]
    found = recall(reversed(items), subject="cluster", limit=5)
    assert [i.id for i in found] == [f"{n:026d}" for n in range(5)]
    assert len(recall(items, subject="cluster", limit=10_000)) == min(30, RECALL_MAX_LIMIT)
    assert len(recall(items, subject="cluster", limit=0)) == 1


def test_recall_never_returns_a_superseded_or_forgotten_item() -> None:
    old = _item("The cluster has one node.", tags=["hades"], item_id="0" * 25 + "A")
    new = _item("The cluster has two nodes.", tags=["hades"], item_id="0" * 25 + "B")
    old.superseded_by, old.superseded_at = new.id, NOW
    forgotten = _item("The cluster is purple.", tags=["hades"], superseded_at=NOW)
    assert recall([old, new, forgotten], tags=["hades"]) == [new]
    assert recall([old, new, forgotten], subject="cluster") == [new]
    assert recall([old, new, forgotten]) == [new]
    assert not matches(old, tags=["hades"], keywords=["cluster"])


def test_the_recall_service_normalizes_tags_and_bounds_the_limit() -> None:
    store = _Store()
    for n in range(3):
        store.memory.add(_item(f"fact {n} on hades", tags=["Hades"], age=timedelta(minutes=n)))
    store.memory.add(_item("the kettle", tags=["home"]))
    view = recall_memory(store.uow(), subject="Hades facts", tags=[" HADES "], limit=2)
    assert view.tags == ["hades"] and view.limit == 2 and view.subject == "Hades facts"
    assert [i.text for i in view.items] == ["fact 0 on hades", "fact 1 on hades"]
    assert recall_memory(store.uow(), subject=None, tags=[], limit=10_000).limit == RECALL_MAX_LIMIT
    assert len(recall_memory(store.uow(), subject=None, tags=[], limit=None).items) == 4


# ----- AC3: roles, supersede, forget, append-only ------------------------------------


def _promote(store: _Store, clock: FakeClock, principal: Principal, text: str) -> MemoryItem:
    return promote_memory(
        store.uow(),
        clock,
        principal=principal,
        request=MemoryPromoteRequest(text=text, source="minion:FDY-0581", scope_tags=["Hades"]),
    )


def test_only_hades_or_the_operator_promotes_supersedes_forgets_or_appends() -> None:
    store, clock = _Store(), FakeClock(NOW)
    item = _promote(store, clock, ORCHESTRATOR, "The cluster has one node.")
    line = LedgerDecisionRequest(principal="scott", channel="telegram", verbatim="Build the mvp")
    with pytest.raises(ForbiddenError):
        _promote(store, clock, OBSERVER, "no")
    with pytest.raises(ForbiddenError):
        supersede_memory(
            store.uow(),
            clock,
            principal=OBSERVER,
            item_id=item.id,
            request=MemorySupersedeRequest(text="no"),
        )
    with pytest.raises(ForbiddenError):
        forget_memory(store.uow(), clock, principal=OBSERVER, item_id=item.id)
    with pytest.raises(ForbiddenError):
        record_ledger_decision(store.uow(), clock, principal=OBSERVER, request=line)
    assert store.memory.rows[item.id].current and store.decision_ledger.rows == []
    assert _kinds(store) == ["memory_promoted"]
    for principal in (OPERATOR, ADMIN):
        promoted = _promote(store, clock, principal, f"{principal.name} promoted this")
        assert promoted.promoted_by == principal.name
        record_ledger_decision(store.uow(), clock, principal=principal, request=line)
    assert len(store.decision_ledger.rows) == 2


def test_supersede_links_the_old_item_to_the_new_one_and_refuses_a_second_time() -> None:
    store, clock = _Store(), FakeClock(NOW)
    old = _promote(store, clock, ORCHESTRATOR, "The cluster has one node.")
    clock.advance(60)
    new = supersede_memory(
        store.uow(),
        clock,
        principal=OPERATOR,
        item_id=old.id,
        request=MemorySupersedeRequest(text="The cluster has two nodes."),
    )
    retired = store.memory.rows[old.id]
    assert retired.superseded_by == new.id and retired.superseded_at == clock.now()
    assert new.current and new.promoted_by == "scott" and new.text == "The cluster has two nodes."
    # A field left out keeps the old value; the tags came through normalized.
    assert new.source == old.source and new.scope_tags == ["hades"]
    assert recall(store.memory.rows.values(), tags=["hades"]) == [new]
    with pytest.raises(ConflictError):
        supersede_memory(
            store.uow(),
            clock,
            principal=OPERATOR,
            item_id=old.id,
            request=MemorySupersedeRequest(text="again"),
        )
    with pytest.raises(NotFoundError):
        forget_memory(store.uow(), clock, principal=OPERATOR, item_id="01NOSUCHITEM000000000000")
    assert _kinds(store) == ["memory_promoted", "memory_superseded"]
    assert store.events.rows[1].payload["superseded_by"] == new.id


def test_supersede_keeps_the_observation_time_unless_the_request_gives_one() -> None:
    """An Edit corrects the words; it must not make an old fact look newly observed, so
    the superseding item keeps the old observation time when the request leaves it out."""
    store, clock = _Store(), FakeClock(NOW)
    observed = NOW - timedelta(days=3)
    old = promote_memory(
        store.uow(),
        clock,
        principal=ORCHESTRATOR,
        request=MemoryPromoteRequest(
            text="The cluster has one node.", source="scott", observed_at=observed
        ),
    )
    clock.advance(3600)
    kept = supersede_memory(
        store.uow(),
        clock,
        principal=OPERATOR,
        item_id=old.id,
        request=MemorySupersedeRequest(text="The cluster has two nodes."),
    )
    assert kept.observed_at == observed and kept.promoted_at == clock.now()
    assert kept.observed_at != kept.promoted_at
    clock.advance(60)
    given = supersede_memory(
        store.uow(),
        clock,
        principal=OPERATOR,
        item_id=kept.id,
        request=MemorySupersedeRequest(text="The cluster has three nodes.", observed_at=NOW),
    )
    assert given.observed_at == NOW
    # The page lists newest observed first, so the kept time keeps a later fact on top.
    later = _promote(store, clock, ORCHESTRATOR, "A newer fact.")
    assert [item.id for item in list_memory(store.uow())] == [later.id, given.id]


def test_forget_supersedes_with_no_replacement() -> None:
    store, clock = _Store(), FakeClock(NOW)
    item = _promote(store, clock, ORCHESTRATOR, "The cluster is purple.")
    clock.advance(5)
    gone = forget_memory(store.uow(), clock, principal=ADMIN, item_id=item.id)
    assert gone.superseded_by is None and gone.superseded_at == clock.now()
    assert recall(store.memory.rows.values()) == []
    with pytest.raises(ConflictError, match="forgotten"):
        forget_memory(store.uow(), clock, principal=ADMIN, item_id=item.id)
    assert _kinds(store) == ["memory_promoted", "memory_forgotten"]


def test_a_decision_needs_principal_channel_and_verbatim_and_defaults_said_at() -> None:
    store, clock = _Store(), FakeClock(NOW)
    with pytest.raises(ValueError, match="verbatim"):
        LedgerDecisionRequest(principal="scott", channel="telegram", verbatim="   ")
    with pytest.raises(ValueError):
        LedgerDecisionRequest(principal="scott", channel="telegram")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="acted_by"):
        LedgerDecisionRequest(principal="s", channel="c", verbatim="words", acted_at=NOW)
    said = record_ledger_decision(
        store.uow(),
        clock,
        principal=ORCHESTRATOR,
        request=LedgerDecisionRequest(
            principal="scott", channel="telegram", verbatim="Build the mvp"
        ),
    )
    assert said.said_at == NOW and said.transcript_ref is None and said.applies_to == []
    assert said.acted_by is None and said.acted_at is None
    acted = record_ledger_decision(
        store.uow(),
        clock,
        principal=ORCHESTRATOR,
        request=LedgerDecisionRequest(
            principal="scott",
            channel="telegram",
            verbatim="Ship it tonight",
            said_at=NOW - timedelta(hours=1),
            transcript_ref="telegram:chat/42#msg/917",
            applies_to=["FDY-0587", " "],
            acted_by="hades",
        ),
    )
    assert acted.said_at == NOW - timedelta(hours=1) and acted.acted_at == NOW
    assert acted.applies_to == ["FDY-0587"] and acted.transcript_ref == "telegram:chat/42#msg/917"
    assert [line.id for line in list_ledger_decisions(store.uow())] == [said.id, acted.id]
    assert _kinds(store) == ["ledger_decision_recorded"] * 2
    assert store.events.rows[0].payload["reason"] == "Build the mvp"


def test_the_ledger_cannot_be_edited_or_deleted_anywhere() -> None:
    # The port offers add and list only; so does the SQL repository.
    port = {name for name in dir(DecisionLedgerRepository) if not name.startswith("_")}
    assert port == {"add", "list_recent"}
    sql = {name for name in dir(DecisionLedger) if not name.startswith("_")}
    assert sql == {"add", "list_recent"}
    # Memory is edited only by superseding: no save, no delete.
    memory_port = {name for name in dir(MemoryRepository) if not name.startswith("_")}
    assert memory_port == {"add", "get", "retire", "recall", "list_recent"}
    assert not {"save", "delete", "remove", "update"} & set(dir(MemoryItems))
    # The API has GET and POST and nothing that edits or removes.
    methods = {
        (str(getattr(route, "path", "")), method)
        for route in memory_router.router.routes
        for method in getattr(route, "methods", ())
    }
    assert methods == {
        ("/memory", "GET"),
        ("/memory", "POST"),
        ("/memory/{item_id}/supersede", "POST"),
        ("/memory/{item_id}/forget", "POST"),
        ("/decisions", "GET"),
        ("/decisions", "POST"),
    }


# ----- AC4: task decisions are mirrored ----------------------------------------------


def test_a_task_decision_is_mirrored_into_the_ledger_with_the_task_channel() -> None:
    store, clock = _Store(_task()), FakeClock(NOW)
    record_decision(
        store.uow(),
        clock,
        principal=ORCHESTRATOR,
        task_id=TASK_ID,
        request=DecisionRequest(
            kind="scope_clarified",
            verbatim="Name the ledger table decision_ledger; decisions is taken.",
            resolves="the table name",
        ),
    )
    assert len(store.decisions.rows) == 1
    [line] = store.decision_ledger.rows
    assert line.channel == "task" and line.applies_to == [TASK_ID]
    assert line.verbatim == "Name the ledger table decision_ledger; decisions is taken."
    assert line.principal == "hades" and line.said_at == store.decisions.rows[0].created_at
    assert line.acted_by == "hades" and line.acted_at == NOW
    assert line.transcript_ref == f"/ui/tasks/{TASK_ID}"
    recorded = next(e for e in store.events.rows if e.kind == "decision_recorded")
    assert recorded.payload["ledger_decision_id"] == line.id
    assert [row.channel for row in list_ledger_decisions(store.uow(), channel="task")] == ["task"]
    assert list_ledger_decisions(store.uow(), channel="telegram") == []


# ----- the API ----------------------------------------------------------------------


def _api(store: _Store, clock: FakeClock, principal: Principal) -> TestClient:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(memory_router.router, prefix="/v1")
    ctx = SimpleNamespace(uow_factory=store.uow, clock=clock)
    app.state.ctx = ctx
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: store
    app.dependency_overrides[current_principal] = lambda: principal
    return TestClient(app)


def test_the_api_recalls_promotes_supersedes_forgets_and_appends() -> None:
    store, clock = _Store(), FakeClock(NOW)
    body = {
        "text": "The cluster has one node.",
        "source": "minion:FDY-0581",
        "scope_tags": ["Hades"],
    }
    with _api(store, clock, OBSERVER) as client:
        assert client.post("/v1/memory", json=body).status_code == 403
        assert (
            client.post(
                "/v1/decisions", json={"principal": "scott", "channel": "x", "verbatim": "w"}
            ).status_code
            == 403
        )
        assert client.get("/v1/memory").status_code == 200
    with _api(store, clock, ORCHESTRATOR) as client:
        promoted = client.post("/v1/memory", json=body)
        assert promoted.status_code == 201, promoted.text
        item = promoted.json()
        assert item["scope_tags"] == ["hades"] and item["promoted_by"] == "hades"
        assert datetime.fromisoformat(item["observed_at"]) == NOW
        clock.advance(30)
        client.post(
            "/v1/memory",
            json={"text": "The kettle is on the left.", "source": "scott", "scope_tags": ["home"]},
        )
        by_tags = client.get("/v1/memory", params={"tags": "Hades,other"}).json()
        assert [i["id"] for i in by_tags["items"]] == [item["id"]]
        assert by_tags["tags"] == ["hades", "other"] and by_tags["limit"] == 20
        by_subject = client.get("/v1/memory", params={"subject": "where is the kettle"}).json()
        assert [i["text"] for i in by_subject["items"]] == ["The kettle is on the left."]
        everything = client.get("/v1/memory", params=[("tags", "hades"), ("tags", "home")]).json()
        assert len(everything["items"]) == 2
        assert client.get("/v1/memory", params={"limit": 0}).status_code == 422
        edited = client.post(
            f"/v1/memory/{item['id']}/supersede", json={"text": "The cluster has two nodes."}
        )
        assert edited.status_code == 201, edited.text
        assert edited.json()["scope_tags"] == ["hades"]
        again = client.post(f"/v1/memory/{item['id']}/supersede", json={"text": "x"})
        assert again.status_code == 409
        forgotten = client.post(f"/v1/memory/{edited.json()['id']}/forget")
        assert forgotten.status_code == 200 and forgotten.json()["superseded_at"] is not None
        assert forgotten.json()["superseded_by"] is None
        assert [i["text"] for i in client.get("/v1/memory").json()["items"]] == [
            "The kettle is on the left."
        ]
        refused = client.post("/v1/decisions", json={"principal": "scott", "channel": "telegram"})
        assert refused.status_code == 422
        line = client.post(
            "/v1/decisions",
            json={"principal": "scott", "channel": "telegram", "verbatim": "Build the mvp"},
        )
        assert line.status_code == 201, line.text
        assert datetime.fromisoformat(line.json()["said_at"]) == NOW + timedelta(seconds=30)
        listed = client.get("/v1/decisions", params={"channel": "telegram"}).json()
        assert [d["verbatim"] for d in listed["items"]] == ["Build the mvp"]
        assert client.get("/v1/decisions", params={"channel": "task"}).json()["items"] == []
    assert store.committed == 5


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/v1/memory",
            {"text": "t", "source": "s", "observed_at": "2026-10-08T18:30:00"},
        ),
        (
            "/v1/decisions",
            {
                "principal": "scott",
                "channel": "c",
                "verbatim": "w",
                "said_at": "2026-10-08T18:30:00",
            },
        ),
        (
            "/v1/decisions",
            {
                "principal": "scott",
                "channel": "c",
                "verbatim": "w",
                "acted_by": "hades",
                "acted_at": "2026-10-08T18:30:00",
            },
        ),
    ],
)
def test_a_naive_request_timestamp_is_refused_before_anything_is_written(
    path: str, body: dict[str, Any]
) -> None:
    """A timestamp with no offset is a 422 at validation, never a committed row followed
    by a 500 from the response serializer."""
    store, clock = _Store(), FakeClock(NOW)
    with _api(store, clock, ORCHESTRATOR) as client:
        response = client.post(path, json=body)
    assert response.status_code == 422, response.text
    assert store.memory.rows == {} and store.decision_ledger.rows == []
    assert store.committed == 0


def test_a_naive_supersede_timestamp_is_refused_and_an_offset_is_held_as_utc() -> None:
    store, clock = _Store(), FakeClock(NOW)
    item = _promote(store, clock, ORCHESTRATOR, "The cluster has one node.")
    with _api(store, clock, ORCHESTRATOR) as client:
        naive = client.post(
            f"/v1/memory/{item.id}/supersede",
            json={"text": "two", "observed_at": "2026-10-08T18:30:00"},
        )
        assert naive.status_code == 422, naive.text
        assert store.memory.rows[item.id].current
        aware = client.post(
            f"/v1/memory/{item.id}/supersede",
            json={"text": "two", "observed_at": "2026-10-08T13:30:00-05:00"},
        )
    assert aware.status_code == 201, aware.text
    assert aware.json()["observed_at"] == "2026-10-08T18:30:00.000000+00:00"
    with pytest.raises(ValueError, match="offset"):
        MemorySupersedeRequest(text="t", observed_at=datetime(2026, 10, 8, 18, 30))
    with pytest.raises(ValueError, match="offset"):
        LedgerDecisionRequest(
            principal="s", channel="c", verbatim="w", said_at=datetime(2026, 10, 8, 18, 30)
        )


def test_the_openapi_document_names_the_memory_and_decisions_paths() -> None:
    from crucible.adapters.api.app import create_app  # noqa: PLC0415
    from crucible.adapters.api.deps import AppContext  # noqa: PLC0415

    context = AppContext(
        uow_factory=None,  # type: ignore[arg-type]
        clock=None,  # type: ignore[arg-type]
        providers=[],
        database_url="",
        engine=None,  # type: ignore[arg-type]
        artifact_store=None,  # type: ignore[arg-type]
    )
    paths = create_app(context).openapi()["paths"]
    assert {
        "/v1/memory",
        "/v1/memory/{item_id}/supersede",
        "/v1/memory/{item_id}/forget",
        "/v1/decisions",
    } <= set(paths)
    assert set(paths["/v1/decisions"]) == {"get", "post"}


# ----- AC5: the Admin Memory page ----------------------------------------------------


def _ui(store: _Store, principal: Principal, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(memory_page.router)
    ctx = SimpleNamespace(clock=FakeClock(NOW), providers=[])
    app.state.ctx = ctx
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: store
    monkeypatch.setattr(memory_page, "_require", lambda request, ctx, uow: (principal, "tok"))
    return TestClient(app, follow_redirects=False)


def _populated() -> _Store:
    store, clock = _Store(_task()), FakeClock(NOW)
    _promote(store, clock, ORCHESTRATOR, "The lab cluster has one node.")
    record_ledger_decision(
        store.uow(),
        clock,
        principal=ORCHESTRATOR,
        request=LedgerDecisionRequest(
            principal="scott",
            channel="telegram",
            verbatim="Build the mvp",
            transcript_ref="https://t.me/c/42/917",
            applies_to=["FDY-0587"],
        ),
    )
    record_decision(
        store.uow(),
        clock,
        principal=ORCHESTRATOR,
        task_id=TASK_ID,
        request=DecisionRequest(kind="scope_clarified", verbatim="Mirror it", resolves="it"),
    )
    return store


def test_the_page_renders_both_tabs_with_local_times(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _populated()
    with _ui(store, OPERATOR, monkeypatch) as client:
        memory = client.get("/ui/memory")
        decisions = client.get("/ui/memory?tab=decisions")
        unknown = client.get("/ui/memory?tab=other")
    assert memory.status_code == 200 and decisions.status_code == 200
    html = memory.text
    assert EXPLANATION in html
    assert 'class="lat-tabs"' in html and 'class="lat-tab is-on"' in html
    assert "Memory items</a>" in html and "Decisions</a>" in html
    assert "The lab cluster has one node." in html and "minion:FDY-0581" in html
    assert NOW_LOCAL in html and "hades" in html and "<li>hades</li>" in html
    assert "2026-10-08T18:30" not in html and "UTC" not in html and " Z" not in html
    assert 'action="/ui/memory/' in html and "/supersede" in html and "/forget" in html
    assert ">Edit</summary>" in html and ">Forget</button>" in html
    assert "<script" not in html
    assert "Build the mvp" not in html
    html = decisions.text
    assert "Build the mvp" in html and "telegram" in html and "FDY-0587" in html
    assert 'href="https://t.me/c/42/917"' in html
    assert "Mirror it" in html and "<td>task</td>" in html and TASK_ID in html
    assert f'href="/ui/tasks/{TASK_ID}"' in html
    assert NOW_LOCAL in html and "2026-10-08T18:30" not in html
    assert "/forget" not in html
    assert "The lab cluster has one node." in unknown.text


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("UTC", NOW_LOCAL),
        ("Etc/UTC", NOW_LOCAL),
        ("", NOW_LOCAL),
        ("America/Chicago", NOW_LOCAL),
        ("Europe/Berlin", "2026-10-08 08:30:00 PM CEST"),
    ],
)
def test_the_page_shows_chicago_time_under_the_default_setting_and_honors_a_set_zone(
    configured: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repository defaults and the Kubernetes base leave `render_timezone` at UTC,
    the stored form; the page still shows the operator's local time, with no UTC marker.
    A zone the operator set explicitly is honored."""
    store = _populated()
    with _ui(store, OPERATOR, monkeypatch) as client:
        client.app.state.ctx.settings = SimpleNamespace(  # type: ignore[attr-defined]
            service=SimpleNamespace(render_timezone=configured)
        )
        memory = client.get("/ui/memory").text
        decisions = client.get("/ui/memory?tab=decisions").text
    for html in (memory, decisions):
        assert expected in html, html
        assert "UTC" not in html and "2026-10-08T18:30" not in html and " Z" not in html


def test_an_observer_reads_the_page_without_the_clicks(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _populated()
    with _ui(store, OBSERVER, monkeypatch) as client:
        html = client.get("/ui/memory").text
    assert "The lab cluster has one node." in html
    assert "/forget" not in html and "/supersede" not in html
    assert 'action="/ui/memory/' not in html and ">Edit</summary>" not in html


def test_edit_and_forget_are_clicks_with_a_csrf_token(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _populated()
    [item_id] = list(store.memory.rows)
    with _ui(store, OPERATOR, monkeypatch) as client:
        stale = client.post(f"/ui/memory/{item_id}/forget", data={"csrf": "wrong"})
        assert stale.status_code == 303 and "kind=bad" in stale.headers["location"]
        assert store.memory.rows[item_id].current
        edited = client.post(
            f"/ui/memory/{item_id}/supersede",
            data={
                "csrf": "tok",
                "text": "The lab cluster has two nodes.",
                "scope_tags": "hades, lab",
            },
        )
        assert edited.status_code == 303
        assert edited.headers["location"].startswith("/ui/memory?tab=memory&kind=ok")
        new_id = store.memory.rows[item_id].superseded_by
        assert new_id is not None
        assert store.memory.rows[new_id].scope_tags == ["hades", "lab"]
        empty = client.post(f"/ui/memory/{new_id}/supersede", data={"csrf": "tok", "text": " "})
        assert "kind=bad" in empty.headers["location"] and store.memory.rows[new_id].current
        forgotten = client.post(f"/ui/memory/{new_id}/forget", data={"csrf": "tok"})
        assert forgotten.status_code == 303 and "Forgotten" in forgotten.headers["location"]
        assert not store.memory.rows[new_id].current
        twice = client.post(f"/ui/memory/{new_id}/forget", data={"csrf": "tok"})
        assert "kind=bad" in twice.headers["location"]
        html = client.get("/ui/memory").text
    assert "Nothing is remembered yet" in html
    with _ui(store, OBSERVER, monkeypatch) as client:
        refused = client.post(f"/ui/memory/{item_id}/forget", data={"csrf": "tok"})
    assert refused.status_code == 303 and "kind=bad" in refused.headers["location"]
    # Two commits: the edit and the forget. Every refusal left the store as it was.
    assert store.committed == 2
