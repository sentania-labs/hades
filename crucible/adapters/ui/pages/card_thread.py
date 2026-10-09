"""Card rooms use the principal room's panel and transport (hades #208)."""

from __future__ import annotations

from typing import Any

from crucible.adapters.ui.pages.room import _can_write, room_panel_context
from crucible.application.memory import LEDGER_MAX_LIMIT, list_ledger_decisions
from crucible.application.rooms import create_room
from crucible.contracts.rooms import RoomCreateRequest
from crucible.domain.entities import Principal
from crucible.domain.rooms import RoomKind, local_time


def card_thread_context(
    ctx: Any,
    uow: Any,
    principal: Principal,
    card: dict[str, Any],
    *,
    timezone: str,
    window: int = 50,
    questions: list[dict[str, Any]] | None = None,
    handoffs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    history = [
        {
            "verbatim": decision.verbatim,
            "channel": decision.channel,
            "time": local_time(decision.said_at, timezone),
        }
        for decision in list_ledger_decisions(uow, limit=LEDGER_MAX_LIMIT)
        if {card["id"], card["external_id"]}.intersection(decision.applies_to)
    ]
    history.extend(
        {
            "verbatim": handoff["words"],
            "channel": f"{handoff['from']} to {handoff['to']} · {handoff['action']}",
            "time": handoff["local_time"],
        }
        for handoff in handoffs or []
    )
    common = {
        "room_title": "Thread",
        "room_kicker": card["external_id"],
        "system_notes": card["notes"],
        "card_history": history,
        "room_timezone": timezone,
    }
    defaults = getattr(getattr(ctx, "settings", None), "rooms", None)
    if (
        defaults is None
        or getattr(uow, "rooms", None) is None
        or getattr(uow, "room_turns", None) is None
    ):
        return {
            **common,
            "room": None,
            "can_write": False,
            "empty_room": "Threads need the rooms settings; see Settings",
        }
    # Serialize first use on the task row so two browser tabs get the same room.
    if _can_write(principal):
        uow.tasks.get(card["id"], for_update=True)
    room = next(
        iter(uow.rooms.list_recent(limit=1, kind=RoomKind.CARD, card_task_id=card["id"])),
        None,
    )
    if room is None and _can_write(principal):
        room = create_room(
            uow,
            ctx.clock,
            principal=principal,
            request=RoomCreateRequest(
                kind=RoomKind.CARD,
                card_task_id=card["id"],
                harness=defaults.default_harness,
                model=defaults.default_model,
            ),
        )
        uow.commit()
    panel = room_panel_context(ctx, uow, principal, room, window)
    switched = bool(
        room and (room.harness, room.model) != (defaults.default_harness, defaults.default_model)
    )
    # A switch back to the defaults is still an explicit choice, recorded in the room.
    if room:
        switched = switched or any(
            turn.role.value == "system" and turn.text.startswith("Switched to ")
            for turn in uow.room_turns.list_for_room(room.id)
        )
    label = (
        f"Project default: {defaults.default_harness} ({defaults.default_model})"
        if not switched
        else "Talking to"
    )
    read_only_msg = "You are signed in as an observer; the card thread is read-only."
    pending = next((q for q in reversed(questions or []) if not q["answered"]), None)
    return {
        **panel,
        **common,
        "read_only_message": read_only_msg,
        "empty_room": "You are signed in as an observer; the card thread is read-only.",
        "questions": questions or [],
        "answer_url": (
            f"/ui/tasks/{card['id']}/questions/{pending['id']}/answer" if pending else None
        ),
        "target_label": label,
        "history_url": f"/ui/tasks/{card['id']}",
    }
