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
) -> dict[str, Any]:
    # Serialize first use on the task row so two browser tabs get the same room.
    if _can_write(principal):
        uow.tasks.get(card["id"], for_update=True)
    room = next(
        iter(uow.rooms.list_recent(limit=1, kind=RoomKind.CARD, card_task_id=card["id"])),
        None,
    )
    defaults = ctx.settings.rooms
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
    history = [
        {
            "verbatim": decision.verbatim,
            "channel": decision.channel,
            "time": local_time(decision.said_at, timezone),
        }
        for decision in list_ledger_decisions(uow, limit=LEDGER_MAX_LIMIT)
        if {card["id"], card["external_id"]}.intersection(decision.applies_to)
    ]
    return {
        **panel,
        "room_title": "Thread",
        "room_kicker": card["external_id"],
        "target_label": label,
        "history_url": f"/ui/tasks/{card['id']}",
        "empty_room": "There is no card room to observe yet.",
        "system_notes": card["notes"],
        "card_history": history,
    }
