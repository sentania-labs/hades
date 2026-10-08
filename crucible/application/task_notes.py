"""Operator notes on a task (hades #489).

A note is the operator's words on one task: stored by Hades, listed on the board card
newest first, and put at the top of the next attempt's or correction's IDENTITY.md
under "Operator notes" so the worker reads them before the contract. Posting one is an
operator's act; an observer reads them."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from crucible.application.errors import ConflictError, ForbiddenError, NotFoundError
from crucible.application.transitions import record_event
from crucible.domain.entities import Principal, Role, TaskNote
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
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
    return {
        "id": note.id,
        "author": note.author,
        "text": note.text,
        "verbatim": note.verbatim,
        "created_at": note.created_at.isoformat(),
    }


def operator_notes_for(uow: UnitOfWork, task_id: str) -> tuple[dict[str, Any], ...]:
    """The notes as the identity bundle renders them (06), newest first."""
    return tuple(note_view(note) for note in list_notes(uow, task_id))


__all__ = [
    "OPERATOR_ROLES",
    "add_note",
    "list_notes",
    "note_view",
    "operator_notes_for",
    "require_operator",
]
