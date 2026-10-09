"""Handoff events during bootstrap (hades #208 item 2).

While Foundry and Hades share the work, a decision or an action passes between them:
Foundry accepts a head, merges a pull request, cancels a task or reroutes it to another
harness, and Hades hands the same kinds of decision back when it needs a person. Each
pass is one `handoff_recorded` event on the task: who (the principal), when (the local
Central time an operator reads, beside the event's own timestamp) and the words as
said. The event is on `GET /v1/tasks/{id}/events` and summarized on the task view."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from crucible.application.transitions import record_event
from crucible.domain.entities import Event, Task
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.time import local_text
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork


class HandoffAction(StrEnum):
    ACCEPT = "accept"
    MERGE = "merge"
    CANCEL = "cancel"
    REROUTE = "reroute"


class HandoffDirection(StrEnum):
    # Foundry (the orchestrator, or the operator acting for it) hands Hades a decision.
    FOUNDRY_TO_HADES = "foundry_to_hades"
    # Hades hands the decision or the action to Foundry.
    HADES_TO_FOUNDRY = "hades_to_foundry"


def record_handoff(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    action: HandoffAction,
    direction: HandoffDirection,
    principal: str,
    words: str,
    attempt_id: str | None = None,
    execution_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> Event:
    """Record one handoff. `words` are the principal's own: the acceptance reasoning,
    the cancel verbatim, the merge sentence, the reroute's why. `principal` is the one
    handing over; Hades's own handoffs are recorded under `crucible`."""
    now = clock.now()
    return record_event(
        uow,
        clock,
        EventKind.HANDOFF_RECORDED,
        principal=principal or PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        execution_id=execution_id,
        attempt_id=attempt_id,
        payload={
            "action": action.value,
            "direction": direction.value,
            "from": "foundry" if direction is HandoffDirection.FOUNDRY_TO_HADES else "hades",
            "to": "hades" if direction is HandoffDirection.FOUNDRY_TO_HADES else "foundry",
            "principal": principal or PRINCIPAL_CRUCIBLE,
            "local_time": local_text(now),
            "words": words,
            # `reason` is what the Audit page shows beside the event.
            "reason": words,
            "task_state": task.state.value,
            **(detail or {}),
        },
    )


def handoff_view(event: Event) -> dict[str, Any]:
    payload = event.payload
    return {
        "seq": event.seq,
        "action": payload.get("action"),
        "direction": payload.get("direction"),
        "from": payload.get("from"),
        "to": payload.get("to"),
        "principal": payload.get("principal", event.principal),
        "local_time": payload.get("local_time", local_text(event.ts)),
        "words": payload.get("words", ""),
        "attempt_id": event.attempt_id,
        "task_state": payload.get("task_state"),
    }


def handoff_views(events: Sequence[Event]) -> list[dict[str, Any]]:
    """The handoffs among a task's events, in event order."""
    return [
        handoff_view(event)
        for event in events
        if getattr(event, "kind", None) == EventKind.HANDOFF_RECORDED.value
    ]


__all__ = [
    "HandoffAction",
    "HandoffDirection",
    "handoff_view",
    "handoff_views",
    "record_handoff",
]
