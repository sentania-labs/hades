"""hades #424: proposed tasks the operator approves and starts as a batch from the Board.

The orchestrator writes a contract with `POST /v1/tasks?proposed=true`; it is stored in
`proposed`, which nothing can start. The operator approves it (with or without a note),
sends it back, or rejects it, from the API or the UI, each with a reason on an audit
event. Several proposals approved in one action are queued in the order selected.

The application code runs as the API and the supervisor run it, over the in-memory store
of the #360 tests; the Board is projected from the same rows.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from crucible.adapters.api.deps import app_context, current_principal, unit_of_work
from crucible.adapters.api.problems import install_problem_handlers
from crucible.adapters.api.routers import tasks as tasks_router
from crucible.adapters.ui.pages import board as board_page
from crucible.adapters.ui.pages import proposals as proposals_page
from crucible.adapters.ui.render import _localize, templates
from crucible.application.admin import audit
from crucible.application.admin.board import COLUMN_BY_STATE, board_view
from crucible.application.corrections import amend_task
from crucible.application.errors import (
    ContractValidationError,
    ForbiddenError,
    TransitionNotAllowedError,
)
from crucible.application.proposals import (
    OPERATOR_DIRECTION,
    approve_batch,
    approve_task,
    reject_proposal,
    send_back_task,
)
from crucible.application.start_task import start_task
from crucible.application.submit_task import submit_task
from crucible.contracts.api import StartRequest
from crucible.domain.entities import Principal, Repository, Role, Task, Wake
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState, is_allowed
from tests.fixtures import REPOSITORY_URL, FakeClock, contract_document
from tests.unit.admin_ui_fixtures import base_context
from tests.unit.test_board import Policies, Repo
from tests.unit.test_issue_334_kanban_board import empty_uow
from tests.unit.test_issue_360_ready_for_merge_correction import (
    NOW,
    PRINCIPAL_ID,
    REPOSITORY_ID,
    _GateResults,
    _NoDecisions,
    _policy,
    _routing,
    _Store,
    _supervisor,
    _Tasks,
    _Wakes,
)

OPERATOR = Principal(
    id="01OPERATOR4240000000000001", name="scott", role=Role.OPERATOR, created_at=NOW
)
ORCHESTRATOR = Principal(id=PRINCIPAL_ID, name="foundry", role=Role.ORCHESTRATOR, created_at=NOW)
NOTE = "Keep the 409 body short.\n  Two spaces and a second line, exactly as written."


# ----- the store, with what submission, the wakes and the views read --------------------


class _ProposalTasks(_Tasks):
    def get_by_external_id(self, principal_id: str, external_id: str) -> Task | None:
        return next(
            (
                t
                for t in self.rows.values()
                if (t.principal_id, t.external_id) == (principal_id, external_id)
            ),
            None,
        )


class _Principals:
    def __init__(self) -> None:
        self.rows = {p.id: p for p in (OPERATOR, ORCHESTRATOR)}

    def get(self, principal_id: str) -> Principal | None:
        return self.rows.get(principal_id)


class _TaskGateResults(_GateResults):
    def list_for_task(self, task_id: str) -> list[Any]:
        return [r for r in self.rows if r.task_id == task_id]


class _AllWakes(_Wakes):
    def list_for_task(self, task_id: str) -> list[Wake]:
        return [w for w in self.rows if w.task_id == task_id]

    def list_for_principal(self, principal_id: str, **_: Any) -> list[Wake]:
        return [w for w in self.rows if w.principal_id == principal_id and w.acked_at is None]


def _store() -> _Store:
    store = _Store(
        Repository(
            id=REPOSITORY_ID,
            name="example-service",
            url=REPOSITORY_URL,
            default_branch="main",
            policy_name="default-software",
            installation_id=7,
            registered_by="operator",
            created_at=NOW,
            external_review_attested=True,
        ),
        _policy(),
        _routing(),
    )
    store.tasks = _ProposalTasks()
    store.wakes = _AllWakes()
    store.gate_results = _TaskGateResults()
    for name in ("acceptance", "decisions", "review_reports", "claims"):
        setattr(store, name, _NoDecisions())
    store.principals = _Principals()  # type: ignore[attr-defined]
    return store


def _propose(store: _Store, clock: FakeClock, external_id: str = "EX-0001") -> Task:
    body = contract_document(
        external_id=external_id,
        title=f"Proposal {external_id}",
        repository={
            "name": "example-service",
            "base_ref": "main",
            "work_branch": f"crucible/{external_id}",
        },
    )
    task, _stored = submit_task(
        store.uow(), clock, principal=ORCHESTRATOR, body=body, proposed=True
    )
    clock.advance(1)
    return task


def _events(store: _Store, kind: EventKind, task_id: str | None = None) -> list[Any]:
    return [
        e
        for e in store.events.rows
        if e.kind == kind.value and (task_id is None or e.task_id == task_id)
    ]


def _audit_kinds(store: _Store) -> list[str]:
    uow = SimpleNamespace(events=Repo(list(store.events.rows)))
    return [item["kind"] for item in audit.tail(uow, cursor=None, limit=100)["items"]]


# ----- AC1: a proposal is stored unauthorized, shown readably, and cannot be scheduled ----


def test_a_proposal_is_stored_in_proposed_and_is_not_a_submission() -> None:
    store, clock = _store(), FakeClock(NOW)

    task = _propose(store, clock)

    assert task.state is TaskState.PROPOSED
    assert [e.kind for e in store.events.rows if e.task_id == task.id] == ["task_proposed"]


def test_a_proposal_cannot_be_started_or_scheduled_until_approved(tmp_path: Path) -> None:
    store, clock = _store(), FakeClock(NOW)
    task = _propose(store, clock)

    with pytest.raises(TransitionNotAllowedError, match="operator approves"):
        start_task(
            store.uow(),
            clock,
            principal=ORCHESTRATOR,
            task_id=task.id,
            request=StartRequest(policy_version=2),
        )
    # The lifecycle table has no edge from a proposal to the queue but approval's.
    assert not is_allowed("task", TaskState.PROPOSED, TaskState.SCHEDULED)
    assert is_allowed("task", TaskState.PROPOSED, TaskState.SUBMITTED)
    supervisor, _provider = _supervisor(store, clock, tmp_path)
    supervisor._materialize_scheduled()
    assert store.executions.rows == {}
    assert store.attempts.rows == {}
    assert task.state is TaskState.PROPOSED


def test_the_orchestrator_cannot_approve_its_own_proposal() -> None:
    store, clock = _store(), FakeClock(NOW)
    task = _propose(store, clock)

    with pytest.raises(ForbiddenError):
        approve_task(store.uow(), clock, principal=ORCHESTRATOR, task_id=task.id, reason="mine")
    with pytest.raises(ForbiddenError):
        reject_proposal(store.uow(), clock, principal=ORCHESTRATOR, task_id=task.id, reason="mine")
    assert task.state is TaskState.PROPOSED


def _board(store: _Store) -> dict[str, Any]:
    uow = empty_uow()
    uow.tasks = Repo(sorted(store.tasks.rows.values(), key=lambda t: t.id))
    uow.contracts = Repo(list(store.contracts.rows))
    uow.events = Repo(list(store.events.rows))
    uow.wakes = Repo(list(store.wakes.rows))
    uow.policies = Policies()
    return board_view(uow, NOW)


def _column(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    column = next(c for c in document["kanban"]["columns"] if c["key"] == key)
    return [card for group in column["parents"] for card in group["tasks"]]


def _render(path: str, sections: list[dict[str, Any]]) -> str:
    return templates.get_template("page.html").render(
        **{**base_context(path), "active": path},
        heading="Fixture",
        intro="Fixture",
        sections=_localize(sections, "America/Chicago"),
        badge=None,
    )


def test_a_proposal_is_on_the_board_in_the_proposed_column_waiting_for_the_operator() -> None:
    store, clock = _store(), FakeClock(NOW)
    task = _propose(store, clock)

    document = _board(store)

    assert COLUMN_BY_STATE[TaskState.PROPOSED] == "proposed"
    [card] = _column(document, "proposed")
    assert card["id"] == task.id
    assert card["holder"]["kind"] == "operator"
    assert _column(document, "queued") == []
    html = _render("/ui/board", [board_page._kanban_section(document)])
    proposed = html[html.index('data-column="proposed"') : html.index('data-column="queued"')]
    assert 'href="/ui/tasks/' + task.id + '"' in proposed
    assert "Proposal EX-0001" in proposed


def test_the_tasks_page_renders_the_contract_readably_not_as_json() -> None:
    store, clock = _store(), FakeClock(NOW)
    task = _propose(store, clock)

    sections = proposals_page.proposal_sections(store.uow(), OPERATOR)
    html = _render("/ui/tasks", sections)

    assert f"Proposed: {task.external_id}" in html
    assert "Importing a duplicate ID must fail with 409 and no partial write." in html
    assert "<li>AC1: Duplicate ID import returns 409.</li>" in html
    assert "<li>V1: make lint</li>" in html
    assert "<li>V4: artifact report/run-evidence.md</li>" in html
    assert f'<a href="{REPOSITORY_URL}/issues/17"' in html
    # Not the stored JSON document.
    assert '"acceptance_criteria"' not in html
    assert "{&#39;id&#39;" not in html
    # The operator's four answers, each asking for a reason.
    for label in ("Approve", "Approve with note", "Send back", "Reject"):
        assert f">{label}</button>" in html
    assert html.count('name="reason"') == 4
    # An observer reads the contract but is offered no answer.
    observer = Principal(id="o", name="reader", role=Role.OBSERVER, created_at=NOW)
    plain = _render("/ui/tasks", proposals_page.proposal_sections(store.uow(), observer))
    assert "Importing a duplicate ID" in plain
    assert ">Approve</button>" not in plain


# ----- AC2: each answer works, from the API and the UI, with an audit row ------------------


def test_approve_moves_the_proposal_to_submitted_and_starts_it(tmp_path: Path) -> None:
    store, clock = _store(), FakeClock(NOW)
    task = _propose(store, clock)

    approve_task(store.uow(), clock, principal=OPERATOR, task_id=task.id, reason="good scope")

    assert task.state is TaskState.SCHEDULED
    [approved] = _events(store, EventKind.TASK_APPROVED, task.id)
    assert approved.principal == "scott"
    assert approved.payload["from"] == "proposed"
    assert approved.payload["to"] == "submitted"
    assert approved.payload["reason"] == "good scope"
    assert approved.payload["note"] is None
    [scheduled] = _events(store, EventKind.TASK_SCHEDULED, task.id)
    assert scheduled.payload["policy"] == {"name": "default-software", "version": 2}
    assert task.contract_version == 1
    assert "task_approved" in _audit_kinds(store)
    supervisor, _provider = _supervisor(store, clock, tmp_path)
    supervisor._materialize_scheduled()
    assert len(store.attempts.rows) == 1


def test_approve_with_note_appends_the_note_to_the_objective_verbatim() -> None:
    store, clock = _store(), FakeClock(NOW)
    task = _propose(store, clock)
    objective = contract_document()["objective"]

    approve_task(
        store.uow(), clock, principal=OPERATOR, task_id=task.id, reason="direction", note=NOTE
    )

    assert task.state is TaskState.SCHEDULED
    assert task.contract_version == 2
    stored = store.contracts.get(task.id, 2)
    assert stored is not None
    assert stored.document["objective"] == f"{objective}\n\n{OPERATOR_DIRECTION} {NOTE}"
    assert stored.document["objective"].endswith(NOTE)
    # The first version, the orchestrator's, is unchanged.
    first = store.contracts.get(task.id, 1)
    assert first is not None
    assert first.document["objective"] == objective
    [approved] = _events(store, EventKind.TASK_APPROVED, task.id)
    assert approved.payload["note"] == NOTE
    assert approved.payload["contract_version"] == 2
    [scheduled] = _events(store, EventKind.TASK_SCHEDULED, task.id)
    assert scheduled.payload["contract_version"] == 2


def test_send_back_returns_the_proposal_with_the_note_as_a_wake_and_amend_proposes_it_again() -> (
    None
):
    store, clock = _store(), FakeClock(NOW)
    task = _propose(store, clock)

    send_back_task(
        store.uow(), clock, principal=OPERATOR, task_id=task.id, reason="unclear", note=NOTE
    )

    assert task.state is TaskState.SENT_BACK
    [wake] = store.wakes.rows
    assert wake.principal_id == ORCHESTRATOR.id
    assert wake.reason == "sent_back"
    assert wake.payload["summary"] == NOTE
    [sent] = _events(store, EventKind.TASK_SENT_BACK, task.id)
    assert sent.payload == {
        "from": "proposed",
        "to": "sent_back",
        "reason": "unclear",
        "note": NOTE,
    }
    assert "task_sent_back" in _audit_kinds(store)
    # Sent back is not authorized either.
    with pytest.raises(TransitionNotAllowedError):
        start_task(
            store.uow(),
            clock,
            principal=ORCHESTRATOR,
            task_id=task.id,
            request=StartRequest(policy_version=2),
        )
    # The orchestrator answers by amending the contract, which proposes it again.
    amended = contract_document(objective="Return 409 with a short body on a duplicate ID.")
    again = amend_task(store.uow(), clock, principal=ORCHESTRATOR, task_id=task.id, body=amended)
    assert again.state is TaskState.PROPOSED
    assert again.contract_version == 2
    assert _events(store, EventKind.TASK_PROPOSED, task.id)[-1].payload["again"] is True


def test_reject_ends_the_proposal_with_an_audit_row_and_a_wake() -> None:
    store, clock = _store(), FakeClock(NOW)
    task = _propose(store, clock)

    reject_proposal(store.uow(), clock, principal=OPERATOR, task_id=task.id, reason="not now")

    assert task.state is TaskState.REJECTED
    assert task.closed_at is not None
    [rejected] = _events(store, EventKind.TASK_PROPOSAL_REJECTED, task.id)
    assert rejected.payload["reason"] == "not now"
    assert "task_proposal_rejected" in _audit_kinds(store)
    [wake] = store.wakes.rows
    assert wake.reason == "proposal_rejected"
    assert "not now" in wake.payload["summary"]


def test_every_answer_needs_a_reason_and_only_a_proposal_is_answered() -> None:
    store, clock = _store(), FakeClock(NOW)
    task = _propose(store, clock)

    with pytest.raises(ContractValidationError):
        approve_task(store.uow(), clock, principal=OPERATOR, task_id=task.id, reason="  ")
    with pytest.raises(ContractValidationError):
        send_back_task(
            store.uow(), clock, principal=OPERATOR, task_id=task.id, reason="r", note=" "
        )
    assert task.state is TaskState.PROPOSED
    approve_task(store.uow(), clock, principal=OPERATOR, task_id=task.id, reason="ok")
    with pytest.raises(TransitionNotAllowedError):
        reject_proposal(store.uow(), clock, principal=OPERATOR, task_id=task.id, reason="late")


def _api(store: _Store, clock: FakeClock, principal: Principal) -> TestClient:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(tasks_router.router, prefix="/v1")
    ctx = SimpleNamespace(
        uow_factory=store.uow,
        clock=clock,
        providers=[SimpleNamespace(name="fake")],
        harnesses=None,
        harness_gates={},
        credential_sources={},
        secret_providers=frozenset(),
    )
    app.state.ctx = ctx
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: store
    app.dependency_overrides[current_principal] = lambda: principal
    return TestClient(app)


def test_the_api_proposes_and_answers_each_way() -> None:
    store, clock = _store(), FakeClock(NOW)
    ids = {}
    with _api(store, clock, ORCHESTRATOR) as client:
        for external_id in ("EX-A", "EX-B", "EX-C", "EX-D"):
            body = contract_document(
                external_id=external_id,
                repository={
                    "name": "example-service",
                    "base_ref": "main",
                    "work_branch": f"crucible/{external_id}",
                },
            )
            response = client.post("/v1/tasks?proposed=true", json=body)
            assert response.status_code == 201, response.text
            assert response.json()["state"] == "proposed"
            ids[external_id] = response.json()["id"]
        # The orchestrator's own start is refused, and so is an answer from it.
        start = client.post(f"/v1/tasks/{ids['EX-A']}/start", json={"policy_version": 2})
        assert start.status_code == 409
        refused = client.post(f"/v1/tasks/{ids['EX-A']}/approve", json={"reason": "mine"})
        assert refused.status_code == 403
    with _api(store, clock, OPERATOR) as client:
        approved = client.post(f"/v1/tasks/{ids['EX-A']}/approve", json={"reason": "go"})
        noted = client.post(f"/v1/tasks/{ids['EX-B']}/approve", json={"reason": "go", "note": NOTE})
        sent = client.post(
            f"/v1/tasks/{ids['EX-C']}/send-back", json={"reason": "unclear", "note": NOTE}
        )
        rejected = client.post(f"/v1/tasks/{ids['EX-D']}/reject", json={"reason": "no"})
        no_reason = client.post(f"/v1/tasks/{ids['EX-D']}/reject", json={})
    assert [r.json()["state"] for r in (approved, noted, sent, rejected)] == [
        "scheduled",
        "scheduled",
        "sent_back",
        "rejected",
    ]
    assert no_reason.status_code == 422
    noted_contract = store.contracts.get(ids["EX-B"], 2)
    assert noted_contract is not None
    assert noted_contract.document["objective"].endswith(f"{OPERATOR_DIRECTION} {NOTE}")
    assert _audit_kinds(store) == [
        "task_approved",
        "task_approved",
        "task_sent_back",
        "task_proposal_rejected",
    ]


def _ui(
    store: _Store, clock: FakeClock, principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    app = FastAPI()
    app.include_router(proposals_page.router)
    ctx = SimpleNamespace(clock=clock)
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: store
    monkeypatch.setattr(proposals_page, "_require", lambda request, ctx, uow: (principal, "tok"))
    return TestClient(app, follow_redirects=False)


def test_the_ui_answers_each_way(monkeypatch: pytest.MonkeyPatch) -> None:
    store, clock = _store(), FakeClock(NOW)
    tasks = [_propose(store, clock, f"EX-{n}") for n in range(5)]
    answers = [
        {"proposal_action": "approve", "reason": "go"},
        {"proposal_action": "approve_with_note", "reason": "go", "note": NOTE},
        {"proposal_action": "send_back", "reason": "unclear", "note": NOTE},
        {"proposal_action": "reject", "reason": "no"},
    ]
    with _ui(store, clock, OPERATOR, monkeypatch) as client:
        for task, form in zip(tasks, answers, strict=False):
            response = client.post(
                f"/ui/tasks/{task.id}/proposal",
                data={**form, "csrf": "tok", "return_to": "/ui/tasks"},
            )
            assert response.status_code == 303
            assert "kind=ok" in response.headers["location"], response.headers["location"]
        # A forged form, and an answer without its note, change nothing.
        forged = client.post(
            f"/ui/tasks/{tasks[4].id}/proposal",
            data={"proposal_action": "approve", "reason": "go", "csrf": "nope"},
        )
        noteless = client.post(
            f"/ui/tasks/{tasks[4].id}/proposal",
            data={"proposal_action": "approve_with_note", "reason": "go", "csrf": "tok"},
        )
    assert "kind=bad" in forged.headers["location"]
    assert "kind=bad" in noteless.headers["location"]
    assert [t.state for t in tasks] == [
        TaskState.SCHEDULED,
        TaskState.SCHEDULED,
        TaskState.SENT_BACK,
        TaskState.REJECTED,
        TaskState.PROPOSED,
    ]
    noted = store.contracts.get(tasks[1].id, 2)
    assert noted is not None
    assert noted.document["objective"].endswith(f"{OPERATOR_DIRECTION} {NOTE}")
    assert _audit_kinds(store) == [
        "task_approved",
        "task_approved",
        "task_sent_back",
        "task_proposal_rejected",
    ]


def test_an_observer_cannot_answer_from_the_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    store, clock = _store(), FakeClock(NOW)
    task = _propose(store, clock)
    observer = Principal(id="o", name="reader", role=Role.OBSERVER, created_at=NOW)
    with _ui(store, clock, observer, monkeypatch) as client:
        response = client.post(
            f"/ui/tasks/{task.id}/proposal",
            data={"proposal_action": "approve", "reason": "go", "csrf": "tok"},
        )
    assert "kind=bad" in response.headers["location"]
    assert task.state is TaskState.PROPOSED


# ----- AC3: a batch approval records the selection order and schedules in it ---------------


def _three(store: _Store, clock: FakeClock) -> dict[str, Task]:
    return {name: _propose(store, clock, name) for name in ("EX-A", "EX-B", "EX-C")}


def test_a_batch_is_approved_in_one_action_and_records_the_selection_order(
    tmp_path: Path,
) -> None:
    store, clock = _store(), FakeClock(NOW)
    tasks = _three(store, clock)
    selected = [tasks["EX-C"].id, tasks["EX-A"].id, tasks["EX-B"].id]

    batch_id, approved = approve_batch(
        store.uow(), clock, principal=OPERATOR, task_ids=selected, reason="today's batch"
    )

    assert [t.id for t in approved] == selected
    assert all(t.state is TaskState.SCHEDULED for t in tasks.values())
    events = _events(store, EventKind.TASK_APPROVED)
    assert [e.task_id for e in events] == selected
    for position, event in enumerate(events, 1):
        assert event.payload["reason"] == "today's batch"
        assert event.payload["batch"] == {
            "id": batch_id,
            "position": position,
            "of": 3,
            "order": ["EX-C", "EX-A", "EX-B"],
        }
    # Scheduled in that order, and so materialized and launched in that order, although
    # the task ids (and the attempt ids) sort the other way.
    assert [e.task_id for e in _events(store, EventKind.TASK_SCHEDULED)] == selected
    supervisor, _provider = _supervisor(store, clock, tmp_path)
    supervisor._materialize_scheduled()
    created = [e.task_id for e in _events(store, EventKind.EXECUTION_CREATED)]
    assert created == selected
    _reverse_attempt_ids(store, selected)
    assert [item.task.id for item in supervisor._list_pending()] == selected


def _reverse_attempt_ids(store: _Store, selected: list[str]) -> None:
    """Give the attempts ids that sort against the selection, so only the queue order
    can put them back in it."""
    rows = {}
    for index, attempt in enumerate(
        sorted(store.attempts.rows.values(), key=lambda a: selected.index(a.task_id))
    ):
        renamed = replace(attempt, id=f"01ATTEMPT424{9 - index:014d}")
        rows[renamed.id] = renamed
    store.attempts.rows = rows


def test_the_board_shows_the_batch_queued_in_the_selected_order() -> None:
    store, clock = _store(), FakeClock(NOW)
    tasks = _three(store, clock)
    left = _propose(store, clock, "EX-D")
    selected = [tasks["EX-B"].id, tasks["EX-C"].id, tasks["EX-A"].id]
    approve_batch(store.uow(), clock, principal=OPERATOR, task_ids=selected, reason="batch")

    document = _board(store)

    queued = _column(document, "queued")
    assert [card["id"] for card in queued] == selected
    assert [card["queue"]["position"] for card in queued] == [1, 2, 3]
    assert [card["queue"]["batch"]["position"] for card in queued] == [1, 2, 3]
    assert [card["id"] for card in _column(document, "proposed")] == [left.id]
    html = _render("/ui/board", [board_page._kanban_section(document)])
    column = html[html.index('data-column="queued"') : html.index('data-column="running"')]
    titles = re.findall(r'admin-kanban-card-title">([^<]+)<', column)
    assert titles == ["Proposal EX-B", "Proposal EX-C", "Proposal EX-A"]
    assert "Queue position 1, batch 1 of 3" in column


def test_a_batch_is_all_or_nothing() -> None:
    store, clock = _store(), FakeClock(NOW)
    tasks = _three(store, clock)
    reject_proposal(store.uow(), clock, principal=OPERATOR, task_id=tasks["EX-B"].id, reason="no")

    with pytest.raises(TransitionNotAllowedError):
        approve_batch(
            store.uow(),
            clock,
            principal=OPERATOR,
            task_ids=[tasks["EX-A"].id, tasks["EX-B"].id],
            reason="batch",
        )
    with pytest.raises(ContractValidationError):
        approve_batch(
            store.uow(),
            clock,
            principal=OPERATOR,
            task_ids=[tasks["EX-A"].id, tasks["EX-A"].id],
            reason="batch",
        )
    assert tasks["EX-A"].state is TaskState.PROPOSED
    assert _events(store, EventKind.TASK_APPROVED) == []


def test_the_batch_form_on_the_board_and_the_api_keep_the_selection_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, clock = _store(), FakeClock(NOW)
    tasks = _three(store, clock)
    section = proposals_page.batch_section(store.uow(), OPERATOR)
    assert section is not None
    html = _render("/ui/board", [section])
    assert 'action="/ui/tasks/proposals/approve"' in html
    assert html.count('<select class="lat-select" name="order_') == 3
    assert 'name="return_to" value="/ui/board"' in html

    with _ui(store, clock, OPERATOR, monkeypatch) as client:
        response = client.post(
            "/ui/tasks/proposals/approve",
            data={
                "csrf": "tok",
                "return_to": "/ui/board",
                "reason": "morning batch",
                f"order_{tasks['EX-A'].id}": "2",
                f"order_{tasks['EX-B'].id}": "",
                f"order_{tasks['EX-C'].id}": "1",
            },
        )
    assert response.headers["location"].startswith("/ui/board?kind=ok")
    events = _events(store, EventKind.TASK_APPROVED)
    assert [e.task_id for e in events] == [tasks["EX-C"].id, tasks["EX-A"].id]
    assert events[0].payload["batch"]["order"] == ["EX-C", "EX-A"]
    assert tasks["EX-B"].state is TaskState.PROPOSED

    other = _propose(store, clock, "EX-E")
    with _api(store, clock, OPERATOR) as client:
        response = client.post(
            "/v1/tasks/approvals",
            json={"task_ids": [other.id, tasks["EX-B"].id], "reason": "afternoon"},
        )
    assert response.status_code == 200, response.text
    assert [t["id"] for t in response.json()["tasks"]] == [other.id, tasks["EX-B"].id]
    last = _events(store, EventKind.TASK_APPROVED)[-2:]
    assert [e.payload["batch"]["position"] for e in last] == [1, 2]


def test_a_repeated_position_is_refused_rather_than_guessed() -> None:
    with pytest.raises(Exception, match="same position"):
        proposals_page.selection_order({"order_a": "1", "order_b": "1"})
    assert proposals_page.selection_order({"order_a": "2", "order_b": "1", "order_c": ""}) == [
        "b",
        "a",
    ]
