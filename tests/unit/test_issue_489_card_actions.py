"""hades #489 step 3: the opened board card, operator notes, and the phase actions.

The application code runs as the API and the UI run it, over the in-memory store of the
#360 tests. A note is stored, listed newest first, and opens the worker's IDENTITY.md;
each phase action maps to its existing operation and records the operator's words as
the decision's verbatim; the Next phase button applies the published default move; the
card renders a stuck task in words, read-only for an observer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from crucible.adapters.api.deps import app_context, current_principal, unit_of_work
from crucible.adapters.api.problems import install_problem_handlers
from crucible.adapters.api.routers import tasks as tasks_router
from crucible.adapters.execution.identity import render_identity_md, write_bundle
from crucible.adapters.ui.pages import board as board_page
from crucible.application.admin import audit
from crucible.application.admin.board_card import board_card_view
from crucible.application.admin.board_lanes import LANES, lane_for_state
from crucible.application.board_actions import (
    DEFAULT_MOVES,
    MOVES,
    apply_move,
    default_move_table,
    next_phase,
    valid_moves,
)
from crucible.application.errors import (
    ConflictError,
    ForbiddenError,
    TransitionNotAllowedError,
)
from crucible.application.queries import task_view
from crucible.application.task_notes import add_note, list_notes, operator_notes_for
from crucible.contracts.common import to_document
from crucible.contracts.completion_claim import CompletionClaimV1
from crucible.contracts.task_contract import TaskContractV1, contract_sha256
from crucible.domain.entities import (
    AcceptanceResult,
    Attempt,
    CICertification,
    Decision,
    Escalation,
    EscalationState,
    Event,
    EvidenceRecord,
    Execution,
    ExecutionRole,
    ExternalReviewCycle,
    GateResultRecord,
    Principal,
    PullRequest,
    PullRequestState,
    Repository,
    Role,
    Task,
    TaskContract,
    TaskNote,
)
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from tests.fixtures import REPOSITORY_URL, FakeClock, contract_document
from tests.unit.test_issue_360_ready_for_merge_correction import (
    NOW,
    PRINCIPAL_ID,
    REPOSITORY_ID,
    _Escalations,
    _Events,
    _policy,
    _routing,
    _Store,
)
from tests.unit.test_issue_424_proposed_tasks import _AllWakes, _TaskGateResults

TASK_ID = "01TASK489CARDACTIONS00001"
PR_ID = "01PULL489CARDACTIONS00001"
HEAD = "0123456789abcdef0123456789abcdef01234567"
OPERATOR = Principal(
    id="01OPER489000000000000001", name="scott", role=Role.OPERATOR, created_at=NOW
)
ADMIN = Principal(id="01ADMN489000000000000001", name="root", role=Role.ADMIN, created_at=NOW)
OBSERVER = Principal(
    id="01OBSV489000000000000001", name="reader", role=Role.OBSERVER, created_at=NOW
)
ORCHESTRATOR = Principal(id=PRINCIPAL_ID, name="foundry", role=Role.ORCHESTRATOR, created_at=NOW)
NOTE = "Resume things and report if there are issues.\nSecond line, kept as typed."
_S = TaskState


# ----- the store ------------------------------------------------------------------


class _Notes:
    def __init__(self) -> None:
        self.rows: list[TaskNote] = []

    def add(self, note: TaskNote) -> None:
        self.rows.append(note)

    def list_for_task(self, task_id: str) -> list[TaskNote]:
        # Oldest first on purpose: the service orders, the fake does not.
        return [n for n in self.rows if n.task_id == task_id]


class _Decisions:
    def __init__(self) -> None:
        self.rows: list[Decision] = []

    def add(self, decision: Decision) -> None:
        self.rows.append(decision)

    def list_for_task(self, task_id: str) -> list[Decision]:
        return [d for d in self.rows if d.task_id == task_id]

    def list_for_comments(
        self, comment_ids: Sequence[str], body_sha256: dict[str, str] | None = None
    ) -> list[Any]:
        return []


class _OpenEscalations(_Escalations):
    def get(self, escalation_id: str, *, for_update: bool = False) -> Escalation | None:
        return next((e for e in self.rows if e.id == escalation_id), None)

    def save(self, escalation: Escalation) -> None:
        self.rows = [escalation if e.id == escalation.id else e for e in self.rows]

    def list_open(self) -> list[Escalation]:
        return [e for e in self.rows if e.state is EscalationState.OPEN]


class _Acceptance:
    def __init__(self) -> None:
        self.rows: list[AcceptanceResult] = []

    def add(self, result: AcceptanceResult) -> None:
        self.rows.append(result)

    def list_for_task(self, task_id: str) -> list[AcceptanceResult]:
        return [a for a in self.rows if a.task_id == task_id]

    def supersede_for_task(self, task_id: str, at: Any) -> None:
        for row in self.rows:
            if row.task_id == task_id and row.superseded_at is None:
                row.superseded_at = at


class _Certifications:
    def __init__(self) -> None:
        self.rows: list[CICertification] = []

    def list_for_task(self, task_id: str) -> list[CICertification]:
        return [c for c in self.rows if c.task_id == task_id]


class _Retention:
    def list_recent(self, limit: int) -> list[Any]:
        return []


class _Principals:
    def __init__(self) -> None:
        self.rows = {p.id: p for p in (OPERATOR, ADMIN, OBSERVER, ORCHESTRATOR)}

    def get(self, principal_id: str) -> Principal | None:
        return self.rows.get(principal_id)


class _AuditEvents(_Events):
    def list_global(
        self, *, after_seq: int, kind: str | None, since: Any, limit: int
    ) -> list[Event]:
        return [e for e in self.rows if int(e.seq or 0) > after_seq][:limit]


class CardStore(_Store):
    events: _AuditEvents
    escalations: _OpenEscalations

    def __init__(self) -> None:
        super().__init__(
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
        self.events = _AuditEvents()
        self.task_notes = _Notes()
        self.decisions = _Decisions()  # type: ignore[assignment]
        self.escalations = _OpenEscalations()
        self.gate_results = _TaskGateResults()
        self.acceptance = _Acceptance()
        self.ci_certifications = _Certifications()
        self.retention = _Retention()
        self.principals = _Principals()
        self.wakes = _AllWakes()


def _document() -> dict[str, Any]:
    return to_document(TaskContractV1.model_validate(contract_document()))


def _task(state: TaskState, *, head: str | None = HEAD) -> Task:
    return Task(
        id=TASK_ID,
        external_id="EX-0001",
        principal_id=PRINCIPAL_ID,
        project="example-service",
        title="Return 409 on duplicate import ID",
        state=state,
        contract_version=1,
        policy_name="default-software",
        policy_version=2,
        repository_id=REPOSITORY_ID,
        created_at=NOW - timedelta(hours=3),
        updated_at=NOW - timedelta(minutes=40),
        head_sha=head,
    )


def _execution(number: int, role: ExecutionRole, version: int) -> Execution:
    return Execution(
        id=f"01EXEC489000000000000000{number}",
        task_id=TASK_ID,
        role=role,
        contract_version=version,
        harness="codex",
        model="gpt-test",
        effort="high",
        provider="fake",
        image="crucible-worker:fake-succeed",
        policy_snapshot=_policy().document,
        state=ExecutionState.SUCCEEDED,
        max_attempts=2,
        retry_on=["environment", "lost"],
        timeout_seconds=3600,
        created_at=NOW - timedelta(hours=3) + timedelta(hours=number),
    )


def _event(store: CardStore, kind: EventKind, payload: dict[str, Any], ts: Any = NOW) -> None:
    store.events.append(
        Event(
            seq=None,
            ts=ts,
            kind=kind.value,
            principal="crucible",
            verified=True,
            payload=payload,
            task_id=TASK_ID,
        )
    )


def store_for(state: TaskState, *, pull_request: bool = True) -> CardStore:
    """A task in `state` with one implementing attempt and, by default, a published PR."""
    store = CardStore()
    store.tasks.add(_task(state))
    document = _document()
    store.contracts.add(
        TaskContract(
            id="01CONTRACT489000000000001",
            task_id=TASK_ID,
            version=1,
            document=document,
            sha256=contract_sha256(document),
            submitted_at=NOW - timedelta(hours=3),
        )
    )
    execution = _execution(1, ExecutionRole.IMPLEMENT, 1)
    store.executions.add(execution)
    store.attempts.add(
        Attempt(
            id="01ATTEMPT48900000000000001",
            execution_id=execution.id,
            task_id=TASK_ID,
            number=1,
            state=AttemptState.SUCCEEDED,
            created_at=NOW - timedelta(hours=2, minutes=50),
            started_at=NOW - timedelta(hours=2, minutes=45),
            ended_at=NOW - timedelta(hours=2, minutes=5),
            exit_code=0,
            exit_class=ExitClass.COMPLETED,
            selected_harness="codex",
            selected_model="gpt-test",
        )
    )
    if pull_request:
        store.pull_requests.add(
            PullRequest(
                id=PR_ID,
                task_id=TASK_ID,
                repository_id=REPOSITORY_ID,
                number=489,
                url=f"{REPOSITORY_URL}/pull/489",
                base_ref="main",
                work_branch="crucible/EX-0001",
                state=PullRequestState.OPEN,
                head_sha=HEAD,
                opened_at=NOW - timedelta(hours=2),
            )
        )
        store.review_cycles.add(
            ExternalReviewCycle(
                id="01CYCLE489000000000000001",
                pull_request_id=PR_ID,
                head_sha=HEAD,
                components=["review"],
                completed_components={"review": "github-review-1"},
                state="completed",
                opened_at=NOW - timedelta(hours=2),
                completed_at=NOW - timedelta(hours=1, minutes=30),
            )
        )
        _event(
            store,
            EventKind.PUBLISH_COMPLETED,
            {"head_sha": HEAD, "pull_request": 489},
            NOW - timedelta(hours=2),
        )
    return store


def stuck_fixture() -> CardStore:
    """A task in the Stuck lane: CI did not certify the corrected head, after one
    correction whose attempt was ended for a stall; an open escalation names a missing
    program; the operator has left two notes."""
    store = store_for(_S.CI_CERTIFICATION_FAILED)
    correction = _document()
    correction["correction"] = {
        "of_version": 1,
        "reason": "ci_certification",
        "addresses": [],
        "instructions": "Fix the unit job: the fixture path moved.",
        "resume_from": "remote_branch",
        "request_internal_review": False,
    }
    store.contracts.add(
        TaskContract(
            id="01CONTRACT489000000000002",
            task_id=TASK_ID,
            version=2,
            document=correction,
            sha256=contract_sha256(correction),
            submitted_at=NOW - timedelta(hours=1, minutes=20),
        )
    )
    task = store.tasks.get(TASK_ID)
    assert task is not None
    task.contract_version = 2
    execution = _execution(2, ExecutionRole.CORRECT, 2)
    store.executions.add(execution)
    store.attempts.add(
        Attempt(
            id="01ATTEMPT48900000000000002",
            execution_id=execution.id,
            task_id=TASK_ID,
            number=1,
            state=AttemptState.FAILED,
            created_at=NOW - timedelta(hours=1, minutes=15),
            started_at=NOW - timedelta(hours=1, minutes=10),
            ended_at=NOW - timedelta(minutes=50),
            exit_code=137,
            exit_class=ExitClass.STALLED,
            selected_harness="claude_code",
            selected_model="claude-fable-5-1",
            termination_detail="Ran `wait` 40 times in a row. Nothing else happened.",
        )
    )
    store.gate_results.rows.append(
        GateResultRecord(
            id="01GATE489000000000000001",
            task_id=TASK_ID,
            attempt_id="01ATTEMPT48900000000000002",
            head_sha=HEAD,
            gate="verification_ran",
            phase="pre_pr",
            result="pass",
            detail="",
            evidence_ids=[],
            evaluated_at=NOW - timedelta(minutes=48),
        )
    )
    store.ci_certifications.rows.append(
        CICertification(
            id="01CERT489000000000000001",
            pull_request_id=PR_ID,
            task_id=TASK_ID,
            head_sha=HEAD,
            state="failed",
            required_checks=["unit"],
            check_runs=[{"name": "unit", "conclusion": "failure"}],
            failure={"job": "unit"},
            detail="unit failed: tests/unit/test_ledger.py::test_duplicate. See the job log.",
            evaluated_at=NOW - timedelta(minutes=41),
        )
    )
    store.escalations.add(
        Escalation(
            id="01ESC489000000000000000001",
            task_id=TASK_ID,
            attempt_id="01ATTEMPT48900000000000002",
            state=EscalationState.OPEN,
            question="The image has no gitleaks. I tried uv run gitleaks and apt; both fail.",
            opened_at=NOW - timedelta(minutes=45),
            reason="missing_capability",
        )
    )
    clock = FakeClock(NOW - timedelta(minutes=30))
    add_note(store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, text="Older note.")
    clock.advance(600)
    add_note(store.uow(), clock, principal=ADMIN, task_id=TASK_ID, text=NOTE)
    return store


def _kinds(store: CardStore) -> list[str]:
    """The event kinds written after the fixture's own publication event."""
    return [kind for kind in store.events.kinds() if kind != EventKind.PUBLISH_COMPLETED.value]


def _payloads(store: CardStore, kind: EventKind) -> list[dict[str, Any]]:
    return [e.payload for e in store.events.rows if e.kind == kind.value]


# ----- AC2: the note round-trips into IDENTITY.md ------------------------------------


def test_a_note_is_stored_listed_newest_first_and_opens_identity_md(tmp_path: Path) -> None:
    store, clock = store_for(_S.SUBMITTED), FakeClock(NOW)
    first = add_note(store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, text="First.")
    clock.advance(60)
    second = add_note(store.uow(), clock, principal=ADMIN, task_id=TASK_ID, text=NOTE)

    assert first.author == "scott" and first.verbatim is True
    assert [n.id for n in list_notes(store.uow(), TASK_ID)] == [second.id, first.id]
    view = task_view(store.uow(), TASK_ID)
    assert [n["text"] for n in view.notes] == [NOTE, "First."]
    assert view.notes[0]["author"] == "root"
    recorded = _payloads(store, EventKind.TASK_NOTE_RECORDED)
    assert [p["reason"] for p in recorded] == ["First.", NOTE]
    assert recorded[1]["verbatim"] is True and recorded[1]["note_id"] == second.id

    notes = operator_notes_for(store.uow(), TASK_ID)
    assert [n["text"] for n in notes] == [NOTE, "First."]
    text = render_identity_md(
        contract=_document(),
        policy=_policy().document,
        external_id="EX-0001",
        owner="foundry",
        work_branch="crucible/EX-0001",
        network_mode="policy",
        operator_notes=notes,
    )
    heading = text.index("## Operator notes")
    assert text.startswith("# Task EX-0001")
    assert heading < text.index("You are working on task") < text.index("## Objective")
    assert text.index(NOTE.splitlines()[0]) < text.index("First.")
    assert "  Second line, kept as typed." in text
    assert "- root at " in text and "- scott at " in text

    written, _digest = write_bundle(
        tmp_path / "identity",
        contract=_document(),
        policy=_policy().document,
        external_id="EX-0001",
        owner="foundry",
        work_branch="crucible/EX-0001",
        network_mode="policy",
        report_schema=CompletionClaimV1.model_json_schema(),
        operator_notes=notes,
    )
    assert written == (tmp_path / "identity" / "IDENTITY.md").read_text(encoding="utf-8")
    assert "## Operator notes" in written

    plain = render_identity_md(
        contract=_document(),
        policy=_policy().document,
        external_id="EX-0001",
        owner="foundry",
        work_branch="crucible/EX-0001",
        network_mode="policy",
    )
    assert "Operator notes" not in plain


def test_only_an_operator_or_admin_posts_a_note() -> None:
    store, clock = store_for(_S.SUBMITTED), FakeClock(NOW)
    for principal in (OBSERVER, ORCHESTRATOR):
        with pytest.raises(ForbiddenError):
            add_note(store.uow(), clock, principal=principal, task_id=TASK_ID, text="no")
    with pytest.raises(ConflictError):
        add_note(store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, text="   ")
    assert store.task_notes.rows == [] and _kinds(store) == []


def _api(store: CardStore, clock: FakeClock, principal: Principal) -> TestClient:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(tasks_router.router, prefix="/v1")
    ctx = SimpleNamespace(uow_factory=store.uow, clock=clock)
    app.state.ctx = ctx
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: store
    app.dependency_overrides[current_principal] = lambda: principal
    return TestClient(app)


def test_the_api_posts_a_note_for_operators_and_refuses_observers() -> None:
    store, clock = store_for(_S.SUBMITTED), FakeClock(NOW)
    with _api(store, clock, OPERATOR) as client:
        response = client.post(f"/v1/tasks/{TASK_ID}/notes", json={"text": NOTE})
    assert response.status_code == 201, response.text
    assert [n["text"] for n in response.json()["notes"]] == [NOTE]
    assert response.json()["notes"][0]["author"] == "scott"
    with _api(store, clock, OBSERVER) as client:
        refused = client.post(f"/v1/tasks/{TASK_ID}/notes", json={"text": "no"})
    assert refused.status_code == 403
    with _api(store, clock, OPERATOR) as client:
        read = client.get(f"/v1/tasks/{TASK_ID}")
    assert [n["text"] for n in read.json()["notes"]] == [NOTE]


# ----- AC3 and AC4: each phase action maps to its operation, with the words recorded ----


def _sealed_last_attempt(store: CardStore, tmp_path: Path) -> None:
    """A gate-failed attempt whose sealed bundle is still on disk (resume from it)."""
    workspace = tmp_path / "attempt-1"
    (workspace / "output").mkdir(parents=True)
    (workspace / "output" / "work_branch.bundle").write_bytes(b"bundle")
    attempt = store.attempts.get("01ATTEMPT48900000000000001")
    assert attempt is not None
    attempt.workspace_path = str(workspace)
    store.evidence.add(
        EvidenceRecord(
            id=None,
            attempt_id=attempt.id,
            task_id=TASK_ID,
            kind="bundle_head",
            observed_at=NOW,
            source="collector",
            verified=True,
            payload={"bundle_verified": True, "head_sha": HEAD, "bundle_sha256": "seal"},
        )
    )


def _blocked_with_escalation(store: CardStore) -> Escalation:
    escalation = Escalation(
        id="01ESC489000000000000000002",
        task_id=TASK_ID,
        attempt_id="01ATTEMPT48900000000000001",
        state=EscalationState.OPEN,
        question="Which layout should we use? More context follows.",
        opened_at=NOW - timedelta(minutes=5),
        reason="ambiguous_contract",
    )
    store.escalations.add(escalation)
    return escalation


CASES: list[tuple[str, TaskState, TaskState, EventKind]] = [
    ("cancel", _S.SUBMITTED, _S.CANCELLED, EventKind.TASK_CANCEL_REQUESTED),
    ("cancel", _S.RUNNING, _S.CANCELLING, EventKind.TASK_CANCEL_REQUESTED),
    ("start", _S.SUBMITTED, _S.SCHEDULED, EventKind.TASK_SCHEDULED),
    ("approve", _S.PROPOSED, _S.SCHEDULED, EventKind.TASK_APPROVED),
    (
        "correct_remote",
        _S.CI_CERTIFICATION_FAILED,
        _S.SCHEDULED,
        EventKind.TASK_CORRECTION_ATTACHED,
    ),
    ("correct_last", _S.PRE_PR_GATES_FAILED, _S.SCHEDULED, EventKind.TASK_CORRECTION_ATTACHED),
    ("accept", _S.AWAITING_ACCEPTANCE, _S.PUBLISHING, EventKind.ACCEPTANCE_RECORDED),
    ("answer", _S.BLOCKED, _S.SCHEDULED, EventKind.DECISION_RECORDED),
]


@pytest.mark.parametrize(("move", "state", "target", "operation_event"), CASES)
def test_each_phase_action_maps_to_its_operation_and_records_the_words(
    move: str, state: TaskState, target: TaskState, operation_event: EventKind, tmp_path: Path
) -> None:
    store, clock = store_for(state, pull_request=move != "correct_last"), FakeClock(NOW)
    if move == "correct_last":
        _sealed_last_attempt(store, tmp_path)
    escalation = _blocked_with_escalation(store) if move == "answer" else None

    result = apply_move(
        store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, move=move, note_text=NOTE
    )

    assert result.task.state is target
    assert result.move.key == move and result.note.text == NOTE
    kinds = _kinds(store)
    assert (
        kinds.index(EventKind.TASK_NOTE_RECORDED.value)
        < kinds.index(EventKind.TASK_PHASE_ACTION_APPLIED.value)
        < kinds.index(operation_event.value)
    )
    action = _payloads(store, EventKind.TASK_PHASE_ACTION_APPLIED)[0]
    assert action["verbatim"] == NOTE and action["reason"] == NOTE
    assert action["move"] == move and action["operation"] == MOVES[move].operation
    assert action["from_state"] == state.value and action["note_id"] == result.note.id
    assert [n.text for n in list_notes(store.uow(), TASK_ID)] == [NOTE]

    operation = _payloads(store, operation_event)[-1]
    if move == "cancel":
        assert operation["verbatim"] == NOTE and operation["reason"] == NOTE
    elif move == "approve":
        assert operation["reason"] == NOTE
    elif move == "accept":
        assert operation["reasoning"] == NOTE and operation["verdict"] == "accepted"
    elif move == "answer":
        assert escalation is not None
        assert operation["verbatim"] == NOTE and operation["escalation_id"] == escalation.id
        closed = store.escalations.get(escalation.id)
        assert closed is not None and closed.state is EscalationState.CLOSED
    elif move.startswith("correct"):
        task = store.tasks.get(TASK_ID)
        assert task is not None and task.contract_version == 2
        version = store.contracts.get(TASK_ID, 2)
        assert version is not None
        correction = version.document["correction"]
        assert correction["instructions"] == NOTE
        assert correction["resume_from"] == (
            "remote_branch" if move == "correct_remote" else "last_attempt"
        )
        assert correction["reason"] == (
            "ci_certification" if move == "correct_remote" else "pre_pr_gates"
        )
        assert operation["reason"] == correction["reason"]


def test_moves_are_offered_only_where_the_state_allows_them() -> None:
    def offered(
        state: TaskState, *, escalation: bool = False, pull_request: bool = True
    ) -> list[str]:
        store = store_for(state, pull_request=pull_request)
        found = _blocked_with_escalation(store) if escalation else None
        return valid_moves(
            store.tasks.get(TASK_ID),  # type: ignore[arg-type]
            escalation=found,
            pull_request=store.pull_requests.get_for_task(TASK_ID),
        )

    assert offered(_S.SUBMITTED) == ["start", "cancel"]
    assert offered(_S.PROPOSED) == ["approve", "cancel"]
    assert offered(_S.RUNNING) == ["cancel"]
    assert offered(_S.CI_CERTIFICATION_FAILED) == ["correct_remote", "correct_last", "cancel"]
    assert offered(_S.PRE_PR_GATES_FAILED, pull_request=False) == ["correct_last", "cancel"]
    assert offered(_S.BLOCKED, escalation=True) == [
        "correct_remote",
        "correct_last",
        "answer",
        "cancel",
    ]
    assert offered(_S.AWAITING_ACCEPTANCE) == ["correct_remote", "correct_last", "accept", "cancel"]
    assert offered(_S.READY_FOR_MERGE) == ["correct_remote", "correct_last", "cancel"]
    assert offered(_S.MERGED) == [] and offered(_S.CANCELLED, escalation=True) == []
    assert set(MOVES) == {
        "approve",
        "start",
        "correct_remote",
        "correct_last",
        "accept",
        "answer",
        "cancel",
    }

    store, clock = store_for(_S.RUNNING), FakeClock(NOW)
    with pytest.raises(TransitionNotAllowedError, match="not a move for a task in running"):
        apply_move(
            store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, move="accept", note_text=NOTE
        )
    with pytest.raises(ConflictError, match="unknown move"):
        apply_move(
            store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, move="fly", note_text=NOTE
        )
    with pytest.raises(ConflictError, match="needs your words"):
        apply_move(
            store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, move="cancel", note_text=" "
        )
    assert store.task_notes.rows == [] and _kinds(store) == []


def test_only_an_operator_or_admin_applies_a_move() -> None:
    for principal in (OBSERVER, ORCHESTRATOR):
        store, clock = store_for(_S.SUBMITTED), FakeClock(NOW)
        with pytest.raises(ForbiddenError):
            apply_move(
                store.uow(),
                clock,
                principal=principal,
                task_id=TASK_ID,
                move="cancel",
                note_text=NOTE,
            )
        assert store.tasks.get(TASK_ID).state is _S.SUBMITTED  # type: ignore[union-attr]
        assert _kinds(store) == []
    store, clock = store_for(_S.SUBMITTED), FakeClock(NOW)
    result = apply_move(
        store.uow(), clock, principal=ADMIN, task_id=TASK_ID, move="start", note_text=NOTE
    )
    assert result.task.state is _S.SCHEDULED and result.note.author == "root"


# ----- AC3: the default-move table ---------------------------------------------------


def test_the_default_move_table_is_the_published_one() -> None:
    assert DEFAULT_MOVES == {
        "inbox": ("holding_pen", "approve"),
        "holding_pen": ("in_progress", "start"),
        "stuck": ("in_progress", "correct_remote"),
        "waiting_on_scott": ("in_progress", "answer"),
        "in_progress": ("graveyard", "cancel"),
    }
    assert [(row["from"], row["to"], row["label"]) for row in default_move_table()] == [
        ("Inbox", "Holding pen", "Approve and queue"),
        ("Holding pen", "In progress", "Start now"),
        ("Stuck", "In progress", "Correction, resume from the PR branch"),
        ("Waiting on Scott", "In progress", "Answer the open escalation"),
        ("In progress", "Graveyard", "Cancel with reason"),
    ]
    assert {key for key, _name, _meaning in LANES} - set(DEFAULT_MOVES) == {"wins", "graveyard"}


def test_next_phase_applies_the_default_move_for_the_lane() -> None:
    clock = FakeClock(NOW)
    proposed = store_for(_S.PROPOSED)
    card = board_card_view(proposed.uow(), TASK_ID, NOW)
    assert card["lane"]["key"] == "inbox" and card["default_move"] == "approve"
    result = next_phase(
        proposed.uow(), clock, principal=OPERATOR, task_id=TASK_ID, note_text=NOTE
    )
    assert result.lane == "inbox" and result.move.key == "approve"
    assert result.task.state is _S.SCHEDULED

    stuck = stuck_fixture()
    result = next_phase(stuck.uow(), clock, principal=OPERATOR, task_id=TASK_ID, note_text=NOTE)
    assert result.lane == "stuck" and result.move.key == "correct_remote"
    assert result.task.state is _S.SCHEDULED
    version = stuck.contracts.get(TASK_ID, 3)
    assert version is not None and version.document["correction"]["resume_from"] == "remote_branch"

    held = store_for(_S.SUBMITTED)
    result = next_phase(held.uow(), clock, principal=OPERATOR, task_id=TASK_ID, note_text=NOTE)
    assert result.lane == "holding_pen" and result.move.key == "start"
    assert result.task.state is _S.SCHEDULED

    waiting = store_for(_S.BLOCKED)
    _blocked_with_escalation(waiting)
    assert lane_for_state(_S.BLOCKED, waiting_on_scott=True) == "waiting_on_scott"
    result = next_phase(waiting.uow(), clock, principal=OPERATOR, task_id=TASK_ID, note_text=NOTE)
    assert result.lane == "waiting_on_scott" and result.move.key == "answer"
    assert result.task.state is _S.SCHEDULED

    running = store_for(_S.RUNNING)
    result = next_phase(running.uow(), clock, principal=OPERATOR, task_id=TASK_ID, note_text=NOTE)
    assert result.lane == "in_progress" and result.move.key == "cancel"
    assert result.task.state is _S.CANCELLING

    done = store_for(_S.MERGED)
    with pytest.raises(ConflictError, match="Wins lane has no next phase"):
        next_phase(done.uow(), clock, principal=OPERATOR, task_id=TASK_ID, note_text=NOTE)


def test_admin_can_apply_the_accept_move_offered_on_the_card() -> None:
    store, clock = store_for(_S.AWAITING_ACCEPTANCE), FakeClock(NOW)
    card = board_card_view(store.uow(), TASK_ID, NOW)
    assert "accept" in [move["key"] for move in card["moves"]]

    result = apply_move(
        store.uow(), clock, principal=ADMIN, task_id=TASK_ID, move="accept", note_text=NOTE
    )

    assert result.task.state is _S.PUBLISHING
    assert result.note.author == "root"
    assert _payloads(store, EventKind.ACCEPTANCE_RECORDED)[0]["reasoning"] == NOTE


# ----- AC1: the card for a stuck task --------------------------------------------------


def test_the_card_view_reads_a_stuck_task_in_words() -> None:
    card = board_card_view(stuck_fixture().uow(), TASK_ID, NOW)
    assert card["lane"] == {"key": "stuck", "name": "Stuck"}
    contract = card["contract"]
    assert contract["objective"].startswith("Importing a duplicate ID must fail with 409")
    assert contract["scope"]["allowed_paths"] == ["src/ledger/**", "tests/ledger/**"]
    assert [c["id"] for c in contract["acceptance_criteria"]] == ["AC1", "AC2"]
    assert contract["checks"][:3] == ["make lint", "make test", "make scan"]
    assert contract["checks"][3] == "write report/run-evidence.md"
    assert contract["tier"] == "standard"
    assert contract["deliverables"] == ["pull_request to main"]
    assert contract["issues"][0]["url"] == f"{REPOSITORY_URL}/issues/17"
    assert card["pull_request"]["number"] == 489 and card["pull_request"]["state"] == "open"
    assert card["head"]["sha"] == HEAD and card["head"]["ci"]["status"] == "red"
    assert card["head"]["ci"]["detail"].startswith("unit failed")
    stuck = card["stuck"]
    assert stuck["headline"] == "The image has no gitleaks."
    assert any("Stuck lane" in line and "CI did not certify" in line for line in stuck["lines"])
    assert any(
        "claude_code / claude-fable-5-1" in line and "stalled" in line for line in stuck["lines"]
    )
    assert any(line.startswith("CI: unit failed") for line in stuck["lines"])
    assert any("Open escalation (missing_capability)" in line for line in stuck["lines"])
    assert card["escalation"]["question"] == "The image has no gitleaks."
    timeline = card["timeline"]
    assert [(r["role"], r["number"], r["harness"], r["exit_class"]) for r in timeline] == [
        ("implement", 1, "codex", "completed"),
        ("correct", 1, "claude_code", "stalled"),
    ]
    assert timeline[1]["line"] == "Ran `wait` 40 times in a row."
    assert timeline[0]["started_at"] == NOW - timedelta(hours=2, minutes=45)
    assert [(c["version"], c["reason"], c["resume_from"]) for c in card["corrections"]] == [
        (2, "ci_certification", "remote_branch")
    ]
    assert card["cost"] == {"attempts": 2, "worker_minutes": 60, "codex_rounds": 1}
    assert [n["text"] for n in card["notes"]] == [NOTE, "Older note."]
    assert [m["key"] for m in card["moves"]] == [
        "correct_remote",
        "correct_last",
        "answer",
        "cancel",
    ]
    assert card["default_move"] == "correct_remote"
    assert card["links"]["task"] == f"/ui/tasks/{TASK_ID}"


def _ui(store: CardStore, principal: Principal, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(board_page.router)
    ctx = SimpleNamespace(clock=FakeClock(NOW), providers=[SimpleNamespace(name="fake")])
    app.state.ctx = ctx
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: store
    monkeypatch.setattr(board_page, "_require", lambda request, ctx, uow: (principal, "tok"))
    return TestClient(app, follow_redirects=False)


def render_card_html(
    store: CardStore, principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> str:
    with _ui(store, principal, monkeypatch) as client:
        response = client.get(f"/ui/board/{TASK_ID}")
    assert response.status_code == 200, response.text
    return str(response.text)


def test_the_card_page_renders_the_stuck_task_and_is_read_only_for_an_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = render_card_html(stuck_fixture(), OPERATOR, monkeypatch)
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    for words in (
        "Where it is and what it is stuck on",
        "The image has no gitleaks.",
        "Importing a duplicate ID must fail with 409",
        "<b>AC1</b>",
        "<code>make lint</code>",
        "Dispatch tier",
        "pull_request to main",
        f"{REPOSITORY_URL}/issues/17",
        f"{REPOSITORY_URL}/pull/489",
        HEAD,
        "unit failed: tests/unit/test_ledger.py::test_duplicate.",
        "Attempt timeline",
        "claude_code / claude-fable-5-1",
        "Correction history",
        "Fix the unit job: the fixture path moved.",
        "Cost so far",
        "worker minutes",
        "Codex rounds",
        "Operator notes",
        "Second line, kept as typed.",
        'name="move"',
        "Correction, resume from the PR branch",
        ">Go<",
        "Next phase: Correction, resume from the PR branch",
        "Save note",
        "Default move per lane",
        "<td>Waiting on Scott</td><td>In progress</td><td>Answer the open escalation</td>",
    ):
        assert words in html, words
    # Newest note first on the page.
    assert html.index("Second line, kept as typed.") < html.index("Older note.")
    assert html.index("Where it is and what it is stuck on") < html.index("Contract</h2>")

    observer = render_card_html(stuck_fixture(), OBSERVER, monkeypatch)
    assert "Operators act here; you are reading only." in observer
    assert 'name="move"' not in observer and "Save note" not in observer
    assert "<form" not in observer.split("</header>", 1)[1].split("</nav>", 1)[1]
    assert "Default move per lane" in observer


def test_the_card_forms_apply_the_note_and_the_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_for(_S.SUBMITTED)
    with _ui(store, OPERATOR, monkeypatch) as client:
        noted = client.post(f"/ui/board/{TASK_ID}/notes", data={"note": NOTE, "csrf": "tok"})
        forged = client.post(
            f"/ui/board/{TASK_ID}/actions",
            data={"move": "cancel", "note": NOTE, "apply": "go", "csrf": "nope"},
        )
        went = client.post(
            f"/ui/board/{TASK_ID}/actions",
            data={"move": "start", "note": "Go now.", "apply": "go", "csrf": "tok"},
        )
    assert noted.status_code == 303 and "kind=ok" in noted.headers["location"]
    assert noted.headers["location"].startswith(f"/ui/board/{TASK_ID}?")
    assert "kind=bad" in forged.headers["location"]
    assert "kind=ok" in went.headers["location"] and "Start%20now" in went.headers["location"]
    assert store.tasks.get(TASK_ID).state is _S.SCHEDULED  # type: ignore[union-attr]
    assert [n.text for n in list_notes(store.uow(), TASK_ID)] == ["Go now.", NOTE]

    stuck = stuck_fixture()
    with _ui(stuck, OPERATOR, monkeypatch) as client:
        nexted = client.post(
            f"/ui/board/{TASK_ID}/actions", data={"note": NOTE, "apply": "next", "csrf": "tok"}
        )
    assert "kind=ok" in nexted.headers["location"], nexted.headers["location"]
    assert stuck.tasks.get(TASK_ID).state is _S.SCHEDULED  # type: ignore[union-attr]
    assert _payloads(stuck, EventKind.TASK_PHASE_ACTION_APPLIED)[0]["move"] == "correct_remote"

    store = store_for(_S.SUBMITTED)
    with _ui(store, OBSERVER, monkeypatch) as client:
        refused_note = client.post(f"/ui/board/{TASK_ID}/notes", data={"note": NOTE, "csrf": "tok"})
        refused_move = client.post(
            f"/ui/board/{TASK_ID}/actions",
            data={"move": "cancel", "note": NOTE, "apply": "go", "csrf": "tok"},
        )
    assert "kind=bad" in refused_note.headers["location"]
    assert "kind=bad" in refused_move.headers["location"]
    assert store.tasks.get(TASK_ID).state is _S.SUBMITTED  # type: ignore[union-attr]
    assert store.task_notes.rows == []


def test_the_audit_lists_the_note_and_the_action_with_the_words() -> None:
    store, clock = store_for(_S.SUBMITTED), FakeClock(NOW)
    apply_move(
        store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, move="cancel", note_text=NOTE
    )
    tail = audit.tail(store.uow(), cursor=None, limit=100)
    kinds = [item["kind"] for item in tail["items"]]
    assert kinds[:2] == ["task_note_recorded", "task_phase_action_applied"]
    assert [item["payload"]["reason"] for item in tail["items"][:2]] == [NOTE, NOTE]
    assert tail["items"][1]["principal"] == "scott"


def test_the_board_links_each_card_to_its_card_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.admin_ui_fixtures import request as make_request  # noqa: PLC0415
    from tests.unit.test_board import NOW as BOARD_NOW  # noqa: PLC0415
    from tests.unit.test_issue_489_board_lanes import fixture  # noqa: PLC0415

    uow, _calls = fixture(3)
    ctx = SimpleNamespace(clock=SimpleNamespace(now=lambda: BOARD_NOW))
    req = make_request("/ui/board")
    req.scope["query_string"] = b""
    monkeypatch.setattr(board_page, "_require", lambda *_args: (None, "csrf"))
    response = board_page.board_page(req, ctx, uow)  # type: ignore[arg-type]
    html = bytes(response.body).decode()
    assert 'href="/ui/board/t0"' in html
