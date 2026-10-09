"""Operator notes on a task (hades #489) and their delivery states (hades #208 item 2).

A note is the operator's words on one task: stored by Hades, listed on the board card
newest first, and put at the top of the next attempt's or correction's IDENTITY.md
under "Operator notes" so the worker reads them before the contract. Posting one is an
operator's act; an observer reads them.

A note's delivery state says how far it got: `awaiting` (written, no attempt has been
given it), `acknowledged` (the supervisor put it in an attempt's identity, a prompt or a
correction, and records the attempt and the time) and `acted_on` (that attempt's report
referenced the note; the collected commit or the event that read the report is the
evidence). The supervisor sets the state from that evidence; the author never does:
`add_note` stores every note `awaiting` and the API's note body has no state field."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from crucible.application.errors import ConflictError, ForbiddenError, NotFoundError
from crucible.application.transitions import record_event
from crucible.domain.entities import NoteDeliveryState, Principal, Role, TaskNote
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.ids import new_id
from crucible.domain.time import local_text
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

OPERATOR_ROLES = frozenset({Role.OPERATOR, Role.ADMIN})


def require_operator(principal: Principal) -> None:
    if principal.role not in OPERATOR_ROLES:
        raise ForbiddenError("operator or admin role required")


def add_note(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    text: str,
    verbatim: bool = True,
) -> TaskNote:
    """Store the operator's words on the task and record them in the audit."""
    require_operator(principal)
    task = uow.tasks.get(task_id)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    if not text.strip():
        raise ConflictError("a note needs some words")
    note = TaskNote(
        id=new_id(),
        task_id=task.id,
        principal_id=principal.id,
        author=principal.name,
        text=text,
        verbatim=verbatim,
        created_at=clock.now(),
    )
    uow.task_notes.add(note)
    record_event(
        uow,
        clock,
        EventKind.TASK_NOTE_RECORDED,
        principal=principal.name,
        task_id=task.id,
        payload={
            "note_id": note.id,
            "author": note.author,
            "verbatim": note.verbatim,
            # `reason` is what the Audit page shows beside the event; the words as typed.
            "reason": note.text,
            "text": note.text,
        },
    )
    return note


def list_notes(uow: UnitOfWork, task_id: str) -> list[TaskNote]:
    """Every note on the task, newest first."""
    repository = getattr(uow, "task_notes", None)
    if repository is None:
        # A unit of work assembled before hades #489 (a test fake) has no notes.
        return []
    rows: Sequence[TaskNote] = repository.list_for_task(task_id)
    return sorted(rows, key=lambda note: (note.created_at, note.id), reverse=True)


def note_view(note: TaskNote) -> dict[str, Any]:
    """The note as the API and the card show it. Times beside the delivery evidence are
    the operator's local Central time; `created_at` keeps the API's RFC 3339 form."""
    return {
        "id": note.id,
        "author": note.author,
        "text": note.text,
        "verbatim": note.verbatim,
        "created_at": note.created_at.isoformat(),
        "delivery_state": note.delivery_state.value,
        "acknowledged": (
            {
                "attempt_id": note.acknowledged_attempt_id,
                "at": local_text(note.acknowledged_at),
            }
            if note.acknowledged_at is not None
            else None
        ),
        "acted_on": (
            {
                "attempt_id": note.acted_on_attempt_id,
                "at": local_text(note.acted_on_at),
                "commit": note.acted_on_commit,
                "event_seq": note.acted_on_event_seq,
            }
            if note.acted_on_at is not None
            else None
        ),
    }


def _note_repository(uow: UnitOfWork) -> Any:
    repository = getattr(uow, "task_notes", None)
    # A unit of work assembled before hades #489 (a test fake) has no notes, and one
    # assembled before hades #208 item 2 cannot save one; neither has a state to move.
    if repository is None or not hasattr(repository, "save"):
        return None
    return repository


def acknowledge_notes(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task_id: str,
    attempt_id: str,
    included: Iterable[Any],
) -> list[TaskNote]:
    """The supervisor's evidence that the worker was given the notes: `included` are the
    note views it rendered into the attempt's IDENTITY.md. Every `awaiting` note among
    them becomes `acknowledged` with that attempt and the time; a note already past
    `awaiting` is left as it is. Returns the notes moved."""
    repository = _note_repository(uow)
    if repository is None:
        return []
    wanted = {str(item.get("id")) for item in included if isinstance(item, dict)}
    if not wanted:
        return []
    now = clock.now()
    moved: list[TaskNote] = []
    for note in repository.list_for_task(task_id):
        if note.id not in wanted or note.delivery_state is not NoteDeliveryState.AWAITING:
            continue
        note.delivery_state = NoteDeliveryState.ACKNOWLEDGED
        note.acknowledged_attempt_id = attempt_id
        note.acknowledged_at = now
        repository.save(note)
        record_event(
            uow,
            clock,
            EventKind.TASK_NOTE_ACKNOWLEDGED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task_id,
            attempt_id=attempt_id,
            payload={
                "note_id": note.id,
                "author": note.author,
                "delivery_state": note.delivery_state.value,
                "evidence": "the note was in the attempt's IDENTITY.md",
                "local_time": local_text(now),
                "reason": f"note {note.id} handed to attempt {attempt_id}",
            },
        )
        moved.append(note)
    return moved


def report_references(note: TaskNote, report_text: str) -> bool:
    """Whether the worker's report names the note: by its id, or by quoting its words
    (the first line as typed, case folded, is enough)."""
    if not report_text:
        return False
    haystack = report_text.casefold()
    if note.id.casefold() in haystack:
        return True
    first_line = note.text.strip().splitlines()[0].strip().casefold() if note.text.strip() else ""
    return bool(first_line) and first_line in haystack


def mark_notes_acted_on(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task_id: str,
    attempt_id: str,
    report_text: str | None,
    commit: str | None,
    event_seq: int | None,
) -> list[TaskNote]:
    """The supervisor's evidence that the worker addressed a note: the attempt's report
    references it. Every `acknowledged` note the report names becomes `acted_on` with
    the attempt, the time, the commit the attempt collected (when there is one) and the
    event that read the report. A note the worker was never given cannot be acted on,
    so an `awaiting` note stays where it is. Returns the notes moved."""
    repository = _note_repository(uow)
    if repository is None or not report_text:
        return []
    now = clock.now()
    moved: list[TaskNote] = []
    for note in repository.list_for_task(task_id):
        if note.delivery_state is not NoteDeliveryState.ACKNOWLEDGED:
            continue
        if not report_references(note, report_text):
            continue
        note.delivery_state = NoteDeliveryState.ACTED_ON
        note.acted_on_attempt_id = attempt_id
        note.acted_on_at = now
        note.acted_on_commit = commit
        note.acted_on_event_seq = event_seq
        repository.save(note)
        record_event(
            uow,
            clock,
            EventKind.TASK_NOTE_ACTED_ON,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task_id,
            attempt_id=attempt_id,
            payload={
                "note_id": note.id,
                "author": note.author,
                "delivery_state": note.delivery_state.value,
                "evidence": "the attempt's report references the note",
                "commit": commit,
                "event_seq": event_seq,
                "local_time": local_text(now),
                "reason": f"note {note.id} addressed by attempt {attempt_id}",
            },
        )
        moved.append(note)
    return moved


def operator_notes_for(uow: UnitOfWork, task_id: str) -> tuple[dict[str, Any], ...]:
    """The notes as the identity bundle renders them (06), newest first."""
    return tuple(note_view(note) for note in list_notes(uow, task_id))


__all__ = [
    "OPERATOR_ROLES",
    "acknowledge_notes",
    "add_note",
    "list_notes",
    "mark_notes_acted_on",
    "note_view",
    "operator_notes_for",
    "report_references",
    "require_operator",
]
