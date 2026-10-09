"""The Hades tools a room's runner exposes to its model (hades #208).

The runner defines them as in-process SDK MCP tools; each one is a call to
`POST /v1/rooms/{id}/tools/{name}` with the room-scoped token, and Hades runs it here
with the room's authority and no more:

- `hades_recall(subject, tags)`: the shared memory recall, the same rule as GET /v1/memory.
- `hades_record_decision(verbatim, applies_to)`: a ledger line in channel `room`. The
  words must be the operator's own, as they stand in one of this room's user turns, and
  every task `applies_to` names must be in the room's scope.
- `hades_file_card(title, objective, project)`: a proposed task (hades #424) built from
  the project's newest contract, which nothing starts until an operator approves it. The
  new card joins the room's scope.
- `hades_read_task(task_id)` and `hades_post_note(task_id, text)`: only for the tasks in
  the room's scope (its card, and the cards it filed).

`hades_answer_question` waits on the comment-states API (FDY-0586), which is not on main;
it is not offered. Approval detection is not here either: nothing records a decision
unless the agent calls `hades_record_decision`."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from typing import Any

from crucible.application.errors import (
    ConflictError,
    ContractValidationError,
    DuplicateExternalIdError,
    ForbiddenError,
    NotFoundError,
)
from crucible.application.memory import record_ledger_decision
from crucible.application.rooms import resolve_task, task_objective
from crucible.application.submit_task import submit_task
from crucible.application.task_notes import add_note, list_notes
from crucible.contracts.api import LedgerDecisionRequest
from crucible.domain.entities import Principal, Task
from crucible.domain.memory import RECALL_DEFAULT_LIMIT, normalize_tags, recall, subject_keywords
from crucible.domain.rooms import HADES_TOOLS, ROOM_CHANNEL, Room, RoomTurn, TurnRole
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

Submit = Callable[..., tuple[Task, Any]]

_SPACE = re.compile(r"\s+")
MAX_ARGUMENT_CHARS = 16_000


def _text(arguments: Mapping[str, Any], name: str, *, required: bool = True) -> str:
    value = arguments.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ContractValidationError(f"{name} is required")
        return ""
    if not isinstance(value, str):
        raise ContractValidationError(f"{name} must be text")
    if len(value) > MAX_ARGUMENT_CHARS:
        raise ContractValidationError(f"{name} is longer than {MAX_ARGUMENT_CHARS} characters")
    return value


def _names(arguments: Mapping[str, Any], name: str) -> list[str]:
    value = arguments.get(name) or []
    if isinstance(value, str):
        value = [part for part in value.split(",")]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ContractValidationError(f"{name} must be a list of names")
    return [v.strip() for v in value if v.strip()]


def room_principal(uow: UnitOfWork, room: Room) -> Principal:
    """Who the room acts as: the principal who created it."""
    principal = uow.principals.get(room.created_by)
    if principal is None or principal.disabled_at is not None:
        raise ForbiddenError("the principal who created this room is no longer active")
    return principal


def scoped_task(uow: UnitOfWork, room: Room, ref: str) -> Task:
    """A task this room's token may act on, or 403. The scope is the room's card and the
    cards it filed, nothing else, whatever the task's own principal."""
    task = resolve_task(uow, ref)
    if task is None:
        raise NotFoundError(f"task {ref} not found")
    if task.id not in room.scope_task_ids:
        raise ForbiddenError(f"task {task.external_id} is outside this room's scope")
    return task


def _flat(text: str) -> str:
    return _SPACE.sub(" ", text).strip().lower()


def _spoken_in(uow: UnitOfWork, room: Room, verbatim: str) -> RoomTurn:
    """The newest user turn of this room that holds these words."""
    wanted = _flat(verbatim)
    for turn in reversed(list(uow.room_turns.list_for_room(room.id))):
        if turn.role is TurnRole.USER and wanted in _flat(turn.text):
            return turn
    raise ConflictError(
        "a decision is the operator's own words: quote them as they stand in one of this "
        "room's messages"
    )


# ----- the tools ----------------------------------------------------------------


def tool_recall(uow: UnitOfWork, room: Room, arguments: Mapping[str, Any]) -> dict[str, Any]:
    subject = _text(arguments, "subject", required=False)[:1024]
    tags = normalize_tags(_names(arguments, "tags"))
    keywords = subject_keywords(subject)
    rows = uow.memory.recall(tags=tags, keywords=keywords, limit=RECALL_DEFAULT_LIMIT)
    items = recall(rows, tags=tags, subject=subject, limit=RECALL_DEFAULT_LIMIT)
    return {
        "subject": subject or None,
        "tags": tags,
        "items": [
            {
                "id": item.id,
                "text": item.text,
                "source": item.source,
                "observed_at": item.observed_at.isoformat(),
                "scope_tags": list(item.scope_tags),
            }
            for item in items
        ],
    }


def tool_record_decision(
    uow: UnitOfWork, clock: Clock, room: Room, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    verbatim = _text(arguments, "verbatim")
    applies_to = _names(arguments, "applies_to")
    for name in applies_to:
        task = resolve_task(uow, name)
        if task is not None and task.id not in room.scope_task_ids:
            raise ForbiddenError(f"task {task.external_id} is outside this room's scope")
    turn = _spoken_in(uow, room, verbatim)
    principal = room_principal(uow, room)
    line = record_ledger_decision(
        uow,
        clock,
        principal=principal,
        request=LedgerDecisionRequest(
            principal=principal.name,
            channel=ROOM_CHANNEL,
            verbatim=verbatim,
            said_at=turn.started_at,
            transcript_ref=f"/v1/rooms/{room.id}#turn-{turn.seq}",
            applies_to=list(dict.fromkeys([room.id, *applies_to])),
        ),
    )
    turn.decision_id = line.id
    uow.room_turns.save(turn)
    return {
        "decision_id": line.id,
        "seq": turn.seq,
        "applies_to": list(line.applies_to),
        "said_at": line.said_at.isoformat(),
    }


def _project_tasks(uow: UnitOfWork, project: str) -> list[Task]:
    found: list[Task] = []
    after: str | None = None
    while True:
        page = list(
            uow.tasks.search(
                state=None,
                project=project,
                repository_id=None,
                external_id=None,
                updated_since=None,
                after_id=after,
                limit=500,
            )
        )
        found.extend(page)
        if len(page) < 500:
            return found
        after = page[-1].id


def next_external_id(template: str, taken: list[str]) -> str:
    """`FDY-0590` and the project's ids give `FDY-<highest + 1>`, zero-padded the same."""
    prefix, sep, number = template.rpartition("-")
    if not sep or not number.isdigit():
        raise ContractValidationError(
            f"the project's newest card {template!r} has no number to follow"
        )
    highest = int(number)
    for name in taken:
        head, dash, tail = name.rpartition("-")
        if dash and head == prefix and tail.isdigit():
            highest = max(highest, int(tail))
    return f"{prefix}-{highest + 1:0{len(number)}d}"


def card_contract(
    template: Mapping[str, Any], *, external_id: str, title: str, objective: str, room: Room
) -> dict[str, Any]:
    """A proposed card from the project's newest contract: its repository, scope,
    policy, checks and delivery, with this card's id, title and objective, one
    acceptance criterion that says the objective is met, and no correction."""
    body = copy.deepcopy(dict(template))
    body["external_id"] = external_id
    body["title"] = title
    body["objective"] = objective
    body["parent_external_id"] = None
    body["correction"] = None
    body["acceptance_criteria"] = [{"id": "AC1", "text": f"The objective is met: {title}"}]
    repository = dict(body.get("repository") or {})
    repository.pop("work_branch", None)
    body["repository"] = repository
    request = dict(body.get("execution_request") or {})
    request["rationale"] = f"Filed from room {room.id}; an operator approves it before it runs."
    body["execution_request"] = request
    return body


def tool_file_card(
    uow: UnitOfWork,
    clock: Clock,
    room: Room,
    arguments: Mapping[str, Any],
    *,
    submit: Submit = submit_task,
) -> dict[str, Any]:
    title = _text(arguments, "title").strip()
    objective = _text(arguments, "objective").strip()
    project = _text(arguments, "project").strip()
    tasks = _project_tasks(uow, project)
    if not tasks:
        raise NotFoundError(f"project {project} has no card to base a new one on")
    newest = max(tasks, key=lambda t: (t.created_at, t.id))
    contract = uow.contracts.get(newest.id, newest.contract_version)
    if contract is None:
        raise NotFoundError(f"card {newest.external_id} has no contract to base a new one on")
    principal = room_principal(uow, room)
    taken = [t.external_id for t in tasks]
    for _ in range(3):
        external_id = next_external_id(newest.external_id, taken)
        body = card_contract(
            contract.document, external_id=external_id, title=title, objective=objective, room=room
        )
        try:
            task, _stored = submit(uow, clock, principal=principal, body=body, proposed=True)
        except DuplicateExternalIdError:
            taken.append(external_id)
            continue
        room.scope_task_ids = list(dict.fromkeys([*room.scope_task_ids, task.id]))
        uow.rooms.save(room)
        return {
            "task_id": task.id,
            "external_id": task.external_id,
            "state": task.state.value,
            "title": task.title,
            "project": task.project,
        }
    raise ConflictError("could not find a free card id; try again")


def tool_read_task(uow: UnitOfWork, room: Room, arguments: Mapping[str, Any]) -> dict[str, Any]:
    task = scoped_task(uow, room, _text(arguments, "task_id").strip())
    return {
        "task_id": task.id,
        "external_id": task.external_id,
        "title": task.title,
        "project": task.project,
        "state": task.state.value,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "objective": task_objective(uow, task),
        "notes": [
            {"author": n.author, "text": n.text, "created_at": n.created_at.isoformat()}
            for n in list_notes(uow, task.id)[:20]
        ],
    }


def tool_post_note(
    uow: UnitOfWork, clock: Clock, room: Room, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    task = scoped_task(uow, room, _text(arguments, "task_id").strip())
    text = _text(arguments, "text")
    note = add_note(
        uow,
        clock,
        principal=room_principal(uow, room),
        task_id=task.id,
        text=text,
        verbatim=False,
    )
    return {"note_id": note.id, "task_id": task.id, "created_at": note.created_at.isoformat()}


def run_tool(
    uow: UnitOfWork,
    clock: Clock,
    room: Room,
    name: str,
    arguments: Mapping[str, Any],
    *,
    submit: Submit = submit_task,
) -> dict[str, Any]:
    if name not in HADES_TOOLS:
        raise NotFoundError(f"{name} is not a Hades room tool")
    if name == "hades_recall":
        return tool_recall(uow, room, arguments)
    if name == "hades_record_decision":
        return tool_record_decision(uow, clock, room, arguments)
    if name == "hades_file_card":
        return tool_file_card(uow, clock, room, arguments, submit=submit)
    if name == "hades_read_task":
        return tool_read_task(uow, room, arguments)
    return tool_post_note(uow, clock, room, arguments)


__all__ = [
    "card_contract",
    "next_external_id",
    "room_principal",
    "run_tool",
    "scoped_task",
    "tool_file_card",
    "tool_post_note",
    "tool_read_task",
    "tool_recall",
    "tool_record_decision",
]
