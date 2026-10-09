"""hades #208 item 2: comment delivery states, minion questions as records, and the
handoff events between Foundry and Hades during bootstrap.

The application code runs as the API and the supervisor run it, over the in-memory store
of the hades #489 card tests, extended with a note repository that saves and a question
repository. AC1: a note's delivery state moves only on the supervisor's evidence and the
author cannot set it. AC2: a worker's question is a record on the task, listed by the task
read and answered by one POST that corrects the attempt with the answer. AC3: accept,
merge, cancel and reroute handoffs carry the principal, the local time and the words and
are on the task's events. AC4: the comment delivery migration owns the new columns,
table and kinds.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.testclient import TestClient

from crucible.adapters.api.deps import app_context, current_principal, unit_of_work
from crucible.adapters.api.problems import install_problem_handlers
from crucible.adapters.api.routers import tasks as tasks_router
from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.models import MinionQuestionRow, TaskNoteRow
from crucible.application import supervisor as supervisor_module
from crucible.application.acceptance import record_acceptance
from crucible.application.board_actions import apply_move
from crucible.application.cancel_task import cancel_task
from crucible.application.errors import ConflictError, ForbiddenError
from crucible.application.handoffs import HandoffAction, HandoffDirection, record_handoff
from crucible.application.minion_questions import (
    ACTION_CORRECTED,
    ACTION_RECORDED,
    answer_question,
    ask_question,
    list_questions,
)
from crucible.application.queries import task_events, task_view
from crucible.application.task_notes import (
    acknowledge_notes,
    add_note,
    list_notes,
    mark_notes_acted_on,
    note_view,
    report_references,
)
from crucible.contracts.api import AcceptRequest, CancelRequest, NoteRequest
from crucible.domain.entities import (
    AcceptanceVerdict,
    Escalation,
    EscalationState,
    MinionQuestion,
    NoteDeliveryState,
    TaskNote,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from crucible.domain.time import local_text
from crucible.ports.execution import CollectedOutputs
from tests.fixtures import FakeClock, migration_by_slug
from tests.unit.test_gates import HEAD as HEAD_498
from tests.unit.test_issue_360_ready_for_merge_correction import NOW
from tests.unit.test_issue_489_card_actions import (
    ADMIN,
    HEAD,
    NOTE,
    OBSERVER,
    OPERATOR,
    ORCHESTRATOR,
    TASK_ID,
    CardStore,
    _blocked_with_escalation,
    _sealed_last_attempt,
    store_for,
)
from tests.unit.test_issue_498_gates_judge_the_work import _bundle, _finished

ATTEMPT_ID = "01ATTEMPT48900000000000001"
LOCAL_TIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} C[DS]T$")
_S = TaskState


# ----- the store: notes that save, and questions -------------------------------------


class _SavingNotes:
    def __init__(self) -> None:
        self.rows: list[TaskNote] = []

    def add(self, note: TaskNote) -> None:
        self.rows.append(note)

    def get(self, note_id: str) -> TaskNote | None:
        return next((n for n in self.rows if n.id == note_id), None)

    def save(self, note: TaskNote) -> None:
        self.rows = [note if n.id == note.id else n for n in self.rows]

    def list_for_task(self, task_id: str) -> list[TaskNote]:
        return [n for n in self.rows if n.task_id == task_id]


class _Questions:
    def __init__(self) -> None:
        self.rows: list[MinionQuestion] = []

    def add(self, question: MinionQuestion) -> None:
        self.rows.append(question)

    def get(self, question_id: str, *, for_update: bool = False) -> MinionQuestion | None:
        return next((q for q in self.rows if q.id == question_id), None)

    def save(self, question: MinionQuestion) -> None:
        self.rows = [question if q.id == question.id else q for q in self.rows]

    def list_for_task(self, task_id: str) -> list[MinionQuestion]:
        # Newest first on purpose: the service orders, the fake does not.
        return sorted((q for q in self.rows if q.task_id == task_id), key=lambda q: q.asked_at)[
            ::-1
        ]


class DeliveryStore(CardStore):
    task_notes: _SavingNotes  # type: ignore[assignment]
    minion_questions: _Questions

    def __init__(self) -> None:
        super().__init__()
        self.task_notes = _SavingNotes()
        self.minion_questions = _Questions()


def _store(state: TaskState, *, pull_request: bool = True) -> DeliveryStore:
    base = store_for(state, pull_request=pull_request)
    store = DeliveryStore()
    for name, value in vars(base).items():
        if name not in ("task_notes", "minion_questions"):
            setattr(store, name, value)
    return store


def _events(store: CardStore, kind: EventKind) -> list[Any]:
    return [e for e in store.events.rows if e.kind == kind.value]


def _api(store: CardStore, clock: FakeClock, principal: Any) -> TestClient:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(tasks_router.router, prefix="/v1")
    ctx = SimpleNamespace(
        uow_factory=store.uow,
        clock=clock,
        harnesses=None,
        harness_gates={},
        credential_sources={},
        secret_providers=frozenset(),
        providers=[SimpleNamespace(name="fake")],
    )
    app.state.ctx = ctx
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: store
    app.dependency_overrides[current_principal] = lambda: principal
    return TestClient(app)


def _attempt(store: CardStore) -> Any:
    attempt = store.attempts.get(ATTEMPT_ID)
    assert attempt is not None
    return attempt


# ----- AC1: delivery states from the supervisor's evidence -------------------------


def test_local_text_is_central_wall_time_without_utc_or_z() -> None:
    summer = local_text(datetime(2026, 7, 4, 17, 30, tzinfo=UTC))
    winter = local_text(datetime(2026, 1, 15, 17, 30, tzinfo=UTC))
    assert summer == "2026-07-04 12:30 CDT" and winter == "2026-01-15 11:30 CST"
    for text in (summer, winter):
        assert LOCAL_TIME.match(text) and "UTC" not in text and "Z" not in text


def test_a_new_note_is_awaiting_and_its_author_cannot_set_the_state() -> None:
    store, clock = _store(_S.SUBMITTED), FakeClock(NOW)
    note = add_note(store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, text=NOTE)
    assert note.delivery_state is NoteDeliveryState.AWAITING
    view = note_view(note)
    assert view["delivery_state"] == "awaiting"
    assert view["acknowledged"] is None and view["acted_on"] is None

    # The body has no state field: naming one is an unknown field, refused.
    with pytest.raises(ValidationError):
        NoteRequest.model_validate({"text": NOTE, "delivery_state": "acted_on"})
    client = _api(store, clock, OPERATOR)
    refused = client.post(
        f"/v1/tasks/{TASK_ID}/notes",
        json={"text": "Mark me done", "delivery_state": "acted_on"},
    )
    assert refused.status_code == 422
    accepted = client.post(f"/v1/tasks/{TASK_ID}/notes", json={"text": "As typed"})
    assert accepted.status_code == 201
    posted = next(n for n in accepted.json()["notes"] if n["text"] == "As typed")
    assert posted["delivery_state"] == "awaiting"
    # Nothing an author does moves a note: every note on the task is still awaiting.
    assert {n.delivery_state for n in list_notes(store.uow(), TASK_ID)} == {
        NoteDeliveryState.AWAITING
    }
    assert _events(store, EventKind.TASK_NOTE_ACKNOWLEDGED) == []


def test_a_note_in_the_attempts_identity_is_acknowledged_with_attempt_and_local_time() -> None:
    store, clock = _store(_S.SUBMITTED), FakeClock(NOW)
    given = add_note(store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, text=NOTE)
    later = add_note(store.uow(), clock, principal=ADMIN, task_id=TASK_ID, text="Written later")
    clock.advance(90)

    # The supervisor's evidence: the views it rendered into the attempt's IDENTITY.md.
    moved = acknowledge_notes(
        store.uow(), clock, task_id=TASK_ID, attempt_id=ATTEMPT_ID, included=[note_view(given)]
    )

    assert [n.id for n in moved] == [given.id]
    stored = store.task_notes.get(given.id)
    assert stored is not None
    assert stored.delivery_state is NoteDeliveryState.ACKNOWLEDGED
    assert stored.acknowledged_attempt_id == ATTEMPT_ID
    assert stored.acknowledged_at == clock.now()
    view = note_view(stored)
    assert view["acknowledged"] == {"attempt_id": ATTEMPT_ID, "at": local_text(clock.now())}
    assert LOCAL_TIME.match(view["acknowledged"]["at"])
    # A note the identity did not carry stays awaiting.
    untouched = store.task_notes.get(later.id)
    assert untouched is not None and untouched.delivery_state is NoteDeliveryState.AWAITING
    (event,) = _events(store, EventKind.TASK_NOTE_ACKNOWLEDGED)
    assert event.principal == "crucible" and event.attempt_id == ATTEMPT_ID
    assert event.payload["note_id"] == given.id and LOCAL_TIME.match(event.payload["local_time"])
    # Acknowledging again with the same evidence moves nothing: the state is set once.
    clock.advance(60)
    assert (
        acknowledge_notes(
            store.uow(),
            clock,
            task_id=TASK_ID,
            attempt_id="01ATTEMPT48900000000000002",
            included=[note_view(given)],
        )
        == []
    )
    again = store.task_notes.get(given.id)
    assert again is not None and again.acknowledged_attempt_id == ATTEMPT_ID


def test_a_note_the_report_references_is_acted_on_with_commit_and_event() -> None:
    store, clock = _store(_S.SUBMITTED), FakeClock(NOW)
    by_id = add_note(store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, text=NOTE)
    by_words = add_note(
        store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, text="Rename the flag.\nThen go."
    )
    ignored = add_note(store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, text="Not seen")
    never_given = add_note(
        store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, text="Added after launch"
    )
    acknowledge_notes(
        store.uow(),
        clock,
        task_id=TASK_ID,
        attempt_id=ATTEMPT_ID,
        included=[note_view(by_id), note_view(by_words), note_view(ignored)],
    )
    clock.advance(600)
    report = (
        f"summary: addressed operator note {by_id.id} and renamed the flag.\n"
        f"self_review: 'rename the flag.' was done as asked; {never_given.id} too."
    )

    moved = mark_notes_acted_on(
        store.uow(),
        clock,
        task_id=TASK_ID,
        attempt_id=ATTEMPT_ID,
        report_text=report,
        commit=HEAD,
        event_seq=41,
    )

    assert {n.id for n in moved} == {by_id.id, by_words.id}
    for note_id in (by_id.id, by_words.id):
        stored = store.task_notes.get(note_id)
        assert stored is not None
        assert stored.delivery_state is NoteDeliveryState.ACTED_ON
        assert stored.acted_on_attempt_id == ATTEMPT_ID
        assert stored.acted_on_commit == HEAD and stored.acted_on_event_seq == 41
        assert stored.acknowledged_attempt_id == ATTEMPT_ID
        view = note_view(stored)
        assert view["acted_on"] == {
            "attempt_id": ATTEMPT_ID,
            "at": local_text(clock.now()),
            "commit": HEAD,
            "event_seq": 41,
        }
    # Acknowledged but not referenced stays acknowledged; a note the worker was never
    # given cannot have been acted on, however the report reads.
    unreferenced = store.task_notes.get(ignored.id)
    assert unreferenced is not None
    assert unreferenced.delivery_state is NoteDeliveryState.ACKNOWLEDGED
    late = store.task_notes.get(never_given.id)
    assert late is not None and late.delivery_state is NoteDeliveryState.AWAITING
    events = _events(store, EventKind.TASK_NOTE_ACTED_ON)
    assert {e.payload["note_id"] for e in events} == {by_id.id, by_words.id}
    assert all(e.payload["commit"] == HEAD and e.payload["event_seq"] == 41 for e in events)
    # The task read shows every state, newest first.
    states = {n["id"]: n["delivery_state"] for n in task_view(store.uow(), TASK_ID).notes}
    assert states == {
        by_id.id: "acted_on",
        by_words.id: "acted_on",
        ignored.id: "acknowledged",
        never_given.id: "awaiting",
    }


@pytest.mark.parametrize(
    ("text", "report", "referenced"),
    [
        # Review finding 01M4F894YJCAZERP586AED00JQ: a short note is never quoted by
        # accident, and a quotation stands on word boundaries.
        ("a", "summary: a change was made", False),
        ("fix", "summary: added a prefix to the flag", False),
        ("fix the flag", "summary: prefix the flags", False),
        ("Rename the flag", "summary: rename the flags", False),
        ("Rename the flag", "summary: I did  rename\n the flag as asked", True),
        ("Rename the flag.\nThen go.", "self_review: 'rename the flag.' done", True),
    ],
)
def test_a_quotation_is_long_enough_and_on_word_boundaries(
    text: str, report: str, referenced: bool
) -> None:
    note = TaskNote(
        id="01NOTE0000000000000000000A",
        task_id=TASK_ID,
        principal_id=OPERATOR.id,
        author="scott",
        text=text,
        created_at=NOW,
    )
    assert report_references(note, report) is referenced
    # The id is matched exactly whatever the note's length.
    assert report_references(note, f"addressed {note.id}") is True


def test_a_report_that_does_not_parse_still_marks_the_notes_it_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding 01M4F894YGP8PN64E9FZBEQAAF: the raw text of a report that is not
    YAML is read for note references, with the `report_parse_failed` event as evidence."""
    calls: list[dict[str, Any]] = []

    def record_call(*_args: Any, **kwargs: Any) -> list[TaskNote]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(supervisor_module, "mark_notes_acted_on", record_call)
    supervisor, pending, _, _, _ = _finished(monkeypatch)
    raw = "summary: addressed note 01NOTE0000000000000000000A: renamed: the flag\n  bad: [\n"
    outputs = CollectedOutputs(
        report=None,
        report_raw=raw,
        blocked_md=None,
        diff_paths=("src/ledger/a.py",),
        bundle=_bundle("src/ledger/a.py"),
    )
    supervisor._finish_exited(pending.attempt.id, 0, outputs)
    (call,) = calls
    assert call["report_text"] == raw and call["attempt_id"] == pending.attempt.id
    assert call["commit"] == HEAD_498


def test_an_empty_report_moves_nothing() -> None:
    store, clock = _store(_S.SUBMITTED), FakeClock(NOW)
    note = add_note(store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, text=NOTE)
    acknowledge_notes(
        store.uow(), clock, task_id=TASK_ID, attempt_id=ATTEMPT_ID, included=[note_view(note)]
    )
    assert (
        mark_notes_acted_on(
            store.uow(),
            clock,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            report_text=None,
            commit=None,
            event_seq=None,
        )
        == []
    )


# ----- AC2: a minion question is a record, answered by one call ----------------------


def _question(store: DeliveryStore, clock: FakeClock) -> tuple[MinionQuestion, Any]:
    escalation = _blocked_with_escalation(store)
    task = store.tasks.get(TASK_ID)
    assert task is not None
    question = ask_question(
        store.uow(),
        clock,
        task=task,
        attempt=_attempt(store),
        question_text=escalation.question,
        escalation=escalation,
    )
    assert question is not None
    return question, escalation


def test_a_blocked_workers_question_is_a_record_on_the_task() -> None:
    store, clock = _store(_S.BLOCKED), FakeClock(NOW)
    question, escalation = _question(store, clock)

    assert question.asked_by_attempt_id == ATTEMPT_ID and question.asked_at == NOW
    assert question.question_text == escalation.question
    assert question.escalation_id == escalation.id and question.answered_at is None
    listed = task_view(store.uow(), TASK_ID).questions
    assert listed == [
        {
            "id": question.id,
            "question_text": escalation.question,
            "asked_by_attempt_id": ATTEMPT_ID,
            "asked_at": local_text(NOW),
            "escalation_id": escalation.id,
            "answered": False,
            "answered_by": None,
            "answered_at": None,
            "answer_text": None,
            "answer_action": None,
            "answer_contract_version": None,
        }
    ]
    assert LOCAL_TIME.match(listed[0]["asked_at"])
    (event,) = _events(store, EventKind.MINION_QUESTION_ASKED)
    assert event.attempt_id == ATTEMPT_ID and event.payload["question_text"] == escalation.question
    # GET /v1/tasks/{id} is where the board and a card thread read it.
    body = _api(store, clock, OBSERVER).get(f"/v1/tasks/{TASK_ID}").json()
    assert body["questions"][0]["id"] == question.id


def test_one_post_answers_the_question_and_corrects_the_attempt_with_the_answer(
    tmp_path: Path,
) -> None:
    store, clock = _store(_S.BLOCKED, pull_request=False), FakeClock(NOW)
    _sealed_last_attempt(store, tmp_path)
    question, escalation = _question(store, clock)
    clock.advance(300)
    answer = "Use the two-column layout; the sidebar stays on the right."

    response = _api(store, clock, OPERATOR).post(
        f"/v1/tasks/{TASK_ID}/questions/{question.id}/answer", json={"answer_text": answer}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "scheduled" and body["contract_version"] == 2
    version = store.contracts.get(TASK_ID, 2)
    assert version is not None
    correction = version.document["correction"]
    assert correction["instructions"] == answer
    assert correction["resume_from"] == "last_attempt" and correction["of_version"] == 1
    # The escalation the same stop opened is closed by the correction's decision.
    closed = store.escalations.get(escalation.id)
    assert closed is not None and closed.state is EscalationState.CLOSED
    decisions = store.decisions.list_for_task(TASK_ID)
    assert [d.kind for d in decisions] == ["correction"] and decisions[0].verbatim == answer
    # The record carries who answered, when, the words and how they reached the worker.
    (shown,) = body["questions"]
    assert shown["answered"] is True and shown["answered_by"] == "scott"
    assert shown["answered_at"] == local_text(clock.now()) and shown["answer_text"] == answer
    assert shown["answer_action"] == ACTION_CORRECTED and shown["answer_contract_version"] == 2
    stored = store.minion_questions.get(question.id)
    assert stored is not None and stored.answered_by == OPERATOR.id
    (event,) = _events(store, EventKind.MINION_QUESTION_ANSWERED)
    assert event.principal == "scott" and event.payload["answer_text"] == answer
    assert event.payload["resume_from"] == "last_attempt"
    # Answered once: a second answer is a conflict, with the first on record.
    again = _api(store, clock, OPERATOR).post(
        f"/v1/tasks/{TASK_ID}/questions/{question.id}/answer", json={"answer_text": "No, three."}
    )
    assert again.status_code == 409
    assert store.minion_questions.get(question.id) == stored


def test_an_answer_resumed_from_the_branch_starts_at_its_tip_and_closes_its_own_escalation() -> (
    None
):
    """Review findings 01M4F894YBNWW9FYASC2X5QME0 and 01M4F894YEWCG4KBQK4PVFQ3HP:
    `remote_branch` reaches the scheduling event the supervisor reads, and of two open
    escalations the one the question names is the one the correction closes."""
    store, clock = _store(_S.BLOCKED), FakeClock(NOW)
    other = Escalation(
        id="01ESC489000000000000000001",
        task_id=TASK_ID,
        attempt_id=ATTEMPT_ID,
        state=EscalationState.OPEN,
        question="An earlier, unrelated question.",
        opened_at=NOW - timedelta(minutes=30),
        reason="ambiguous_contract",
    )
    store.escalations.add(other)
    question, escalation = _question(store, clock)
    answer = "Keep the published layout and add the sidebar."

    task, answered = answer_question(
        store.uow(),
        clock,
        principal=OPERATOR,
        task_id=TASK_ID,
        question_id=question.id,
        answer_text=answer,
        resume_from="remote_branch",
    )

    assert task.state is _S.SCHEDULED and answered.answer_action == ACTION_CORRECTED
    version = store.contracts.get(TASK_ID, 2)
    assert version is not None and version.document["correction"]["resume_from"] == "remote_branch"
    scheduled = _events(store, EventKind.TASK_SCHEDULED)[-1]
    assert scheduled.payload["resume_from_work_branch"] is True
    closed = store.escalations.get(escalation.id)
    assert closed is not None and closed.state is EscalationState.CLOSED
    untouched = store.escalations.get(other.id)
    assert untouched is not None and untouched.state is EscalationState.OPEN
    (decision,) = store.decisions.list_for_task(TASK_ID)
    assert decision.escalation_id == escalation.id and decision.verbatim == answer


def test_an_answer_from_the_last_attempt_does_not_resume_from_the_branch(
    tmp_path: Path,
) -> None:
    store, clock = _store(_S.BLOCKED, pull_request=False), FakeClock(NOW)
    _sealed_last_attempt(store, tmp_path)
    question, _ = _question(store, clock)
    answer_question(
        store.uow(),
        clock,
        principal=OPERATOR,
        task_id=TASK_ID,
        question_id=question.id,
        answer_text="Use the two-column layout.",
    )
    scheduled = _events(store, EventKind.TASK_SCHEDULED)[-1]
    assert "resume_from_work_branch" not in scheduled.payload


def test_the_boards_answer_move_goes_through_the_question_record(tmp_path: Path) -> None:
    store, clock = _store(_S.BLOCKED, pull_request=False), FakeClock(NOW)
    _sealed_last_attempt(store, tmp_path)
    question, escalation = _question(store, clock)

    result = apply_move(
        store.uow(), clock, principal=OPERATOR, task_id=TASK_ID, move="answer", note_text=NOTE
    )

    assert result.task.state is _S.SCHEDULED and result.message.startswith("Answered the question")
    answered = store.minion_questions.get(question.id)
    assert answered is not None and answered.answer_text == NOTE
    assert answered.answer_action == ACTION_CORRECTED and answered.answered_by_name == "scott"
    version = store.contracts.get(TASK_ID, 2)
    assert version is not None and version.document["correction"]["instructions"] == NOTE
    closed = store.escalations.get(escalation.id)
    assert closed is not None and closed.state is EscalationState.CLOSED
    # The note the move stored is the same words, awaiting the corrected attempt.
    assert [n.text for n in list_notes(store.uow(), TASK_ID)] == [NOTE]


def test_an_answer_the_task_cannot_resume_is_recorded_and_closes_the_escalation() -> None:
    store, clock = _store(_S.RUNNING), FakeClock(NOW)
    question, escalation = _question(store, clock)

    task, answered = answer_question(
        store.uow(),
        clock,
        principal=ORCHESTRATOR,
        task_id=TASK_ID,
        question_id=question.id,
        answer_text="Keep going with the first reading.",
    )

    assert task.state is _S.RUNNING and answered.answer_action == ACTION_RECORDED
    assert answered.answer_contract_version is None and answered.answered_by_name == "foundry"
    closed = store.escalations.get(escalation.id)
    assert closed is not None and closed.state is EscalationState.CLOSED
    assert [d.kind for d in store.decisions.list_for_task(TASK_ID)] == ["escalation_answer"]


def test_only_an_operator_admin_or_orchestrator_answers_and_the_answer_needs_words() -> None:
    store, clock = _store(_S.BLOCKED), FakeClock(NOW)
    question, _escalation = _question(store, clock)
    with pytest.raises(ForbiddenError):
        answer_question(
            store.uow(),
            clock,
            principal=OBSERVER,
            task_id=TASK_ID,
            question_id=question.id,
            answer_text="read only",
        )
    with pytest.raises(ConflictError):
        answer_question(
            store.uow(),
            clock,
            principal=OPERATOR,
            task_id=TASK_ID,
            question_id=question.id,
            answer_text="   ",
        )
    refused = _api(store, clock, OBSERVER).post(
        f"/v1/tasks/{TASK_ID}/questions/{question.id}/answer", json={"answer_text": "no"}
    )
    assert refused.status_code == 403
    assert [q.answered_at for q in list_questions(store.uow(), TASK_ID)] == [None]


# ----- AC3: handoffs between Foundry and Hades --------------------------------------


def _handoffs(store: CardStore) -> list[dict[str, Any]]:
    return [e.payload for e in _events(store, EventKind.HANDOFF_RECORDED)]


def test_accept_and_cancel_are_handed_to_hades_with_principal_time_and_words() -> None:
    store, clock = _store(_S.AWAITING_ACCEPTANCE), FakeClock(NOW)
    reasoning = "The diff does what the issue asked; CI is green on the head."
    record_acceptance(
        store.uow(),
        clock,
        principal=ORCHESTRATOR,
        task_id=TASK_ID,
        request=AcceptRequest(verdict=AcceptanceVerdict.ACCEPTED, reasoning=reasoning),
    )
    clock.advance(120)
    cancel_task(
        store.uow(),
        clock,
        principal=OPERATOR,
        task_id=TASK_ID,
        request=CancelRequest(
            reason="superseded", verbatim="Drop it; #600 covers this.", decided_by="scott"
        ),
    )

    accept, cancel = _handoffs(store)
    assert accept["action"] == "accept" and accept["direction"] == "foundry_to_hades"
    assert accept["principal"] == "foundry" and accept["words"] == reasoning
    assert accept["local_time"] == local_text(NOW) and LOCAL_TIME.match(accept["local_time"])
    assert cancel["action"] == "cancel" and cancel["direction"] == "foundry_to_hades"
    assert cancel["principal"] == "scott" and cancel["words"] == "Drop it; #600 covers this."
    assert cancel["local_time"] == local_text(clock.now())
    for payload in (accept, cancel):
        assert "UTC" not in payload["local_time"] and "Z" not in payload["local_time"]
    # Listed on GET /v1/tasks/{id}/events and summarized on the task.
    listed = task_events(store.uow(), TASK_ID, cursor=None, limit=200)
    kinds = [e.kind for e in listed.items]
    assert kinds.count("handoff_recorded") == 2
    shown = task_view(store.uow(), TASK_ID).handoffs
    assert [(h["action"], h["principal"], h["words"]) for h in shown] == [
        ("accept", "foundry", reasoning),
        ("cancel", "scott", "Drop it; #600 covers this."),
    ]
    body = _api(store, clock, OBSERVER).get(f"/v1/tasks/{TASK_ID}/events").json()
    handoffs = [e for e in body["items"] if e["kind"] == "handoff_recorded"]
    assert [e["payload"]["action"] for e in handoffs] == ["accept", "cancel"]
    assert handoffs[0]["principal"] == "foundry" and handoffs[1]["principal"] == "scott"


def test_merge_and_reroute_handoffs_record_both_directions() -> None:
    store, clock = _store(_S.READY_FOR_MERGE), FakeClock(NOW)
    task = store.tasks.get(TASK_ID)
    assert task is not None
    record_handoff(
        store.uow(),
        clock,
        task=task,
        action=HandoffAction.MERGE,
        direction=HandoffDirection.HADES_TO_FOUNDRY,
        principal="crucible",
        words="PR #489 is ready for merge; the operator performs the merge.",
    )
    record_handoff(
        store.uow(),
        clock,
        task=task,
        action=HandoffAction.MERGE,
        direction=HandoffDirection.FOUNDRY_TO_HADES,
        principal="scott",
        words="pull request #489 was merged by scott as abc123",
    )
    record_handoff(
        store.uow(),
        clock,
        task=task,
        action=HandoffAction.REROUTE,
        direction=HandoffDirection.HADES_TO_FOUNDRY,
        principal="crucible",
        words="previous pool reported quota exhaustion",
        attempt_id=ATTEMPT_ID,
    )
    record_handoff(
        store.uow(),
        clock,
        task=task,
        action=HandoffAction.REROUTE,
        direction=HandoffDirection.FOUNDRY_TO_HADES,
        principal="foundry",
        words="Pin this to codex; hermes keeps timing out.",
    )

    rows = _handoffs(store)
    assert [(r["action"], r["from"], r["to"], r["principal"]) for r in rows] == [
        ("merge", "hades", "foundry", "crucible"),
        ("merge", "foundry", "hades", "scott"),
        ("reroute", "hades", "foundry", "crucible"),
        ("reroute", "foundry", "hades", "foundry"),
    ]
    assert all(LOCAL_TIME.match(r["local_time"]) and r["words"] == r["reason"] for r in rows)
    shown = task_view(store.uow(), TASK_ID).handoffs
    assert [h["direction"] for h in shown] == [
        "hades_to_foundry",
        "foundry_to_hades",
        "hades_to_foundry",
        "foundry_to_hades",
    ]
    assert shown[2]["attempt_id"] == ATTEMPT_ID
    events = [e for e in store.events.rows if e.kind == "handoff_recorded"]
    assert [e.principal for e in events] == ["crucible", "scott", "crucible", "foundry"]


# ----- AC4: the comment delivery migration ---------------------------------------------


def test_the_comment_delivery_migration_owns_the_new_kinds_columns_and_table() -> None:
    # The revision is found by its slug, never imported by number: Hades renumbers a
    # branch's new migration past main's highest and points it at main's head when it
    # merges main into the branch (hades #447, CONTRIBUTING), so a pinned module name
    # fails at collection the moment that happens.
    script = migration_by_slug("comment_delivery")
    m = script.module
    assert Path(script.path).name == f"_{m.revision}.py"
    assert m.revision == script.revision
    directory = ScriptDirectory.from_config(migrate.alembic_config("postgresql://unused/unused"))
    heads = directory.get_heads()
    assert len(heads) == 1
    # Head first. The revision sits above 0060_personas_scheduled_jobs, the head when it
    # was last numbered, so every database already at that head runs it; a revision
    # placed below an applied head never runs there.
    chain = [r.revision for r in directory.iterate_revisions(heads[0], "base")]
    assert m.revision in chain
    assert chain.index("0059_rooms") > chain.index(m.revision)
    assert chain.index("0060_personas_scheduled_jobs") > chain.index(m.revision)
    assert set(m.EVENT_KINDS) == {
        "task_note_acknowledged",
        "task_note_acted_on",
        "minion_question_asked",
        "minion_question_answered",
        "handoff_recorded",
    }
    # It adds its five kinds to whatever the nearest kinds-setting revision below
    # permits, found by walking down from down_revision (0060_personas_scheduled_jobs
    # sets none), and the chain's head owns the CHECK constraint with every kind.
    below = directory.get_revision(m.down_revision)
    while below is not None and not hasattr(below.module, "_event_kinds"):
        assert isinstance(below.down_revision, str)
        below = directory.get_revision(below.down_revision)
    assert below is not None
    previous_kinds = set(below.module._event_kinds())
    assert set(m._previous_event_kinds()) == previous_kinds
    assert previous_kinds.isdisjoint(m.EVENT_KINDS)
    assert set(m._event_kinds()) == previous_kinds | set(m.EVENT_KINDS)
    head = directory.get_revision(heads[0])
    assert head is not None
    assert set(head.module._event_kinds()) == {k.value for k in EventKind}
    note_columns = {c.name for c in TaskNoteRow.__table__.columns}
    assert set(m.NOTE_COLUMNS) <= note_columns
    assert TaskNoteRow.__table__.columns["delivery_state"].server_default is not None
    question_columns = {c.name for c in MinionQuestionRow.__table__.columns}
    assert question_columns == {
        "id",
        "task_id",
        "asked_by_attempt_id",
        "escalation_id",
        "question_text",
        "asked_at",
        "answered_by",
        "answered_by_name",
        "answered_at",
        "answer_text",
        "answer_action",
        "answer_contract_version",
    }
    assert MinionQuestionRow.__tablename__ == "minion_questions"
