"""Role-aware JSON projection for the operator board."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from crucible.application.admin.board_lanes import board_lanes_view
from crucible.application.board_actions import moves_for_card, open_escalation
from crucible.domain.entities import Principal, Role
from crucible.domain.lifecycle import TaskState
from crucible.ports.repository import UnitOfWork

CENTRAL = ZoneInfo("America/Chicago")
REMOVE_LIKE = frozenset({"cancel", "decline"})
ACTION_LABELS = {
    "approve": "Approve scope",
    "accept": "Accept result",
    "cancel": "Cancel",
}


def _local(moment: datetime) -> str:
    return moment.astimezone(CENTRAL).isoformat(timespec="seconds")


def board_resource(
    uow: UnitOfWork,
    now: datetime,
    principal: Principal,
    *,
    cards_for: frozenset[str] | None = None,
) -> dict[str, Any]:
    document = board_lanes_view(uow, now)
    can_act = principal.role in {Role.OPERATOR, Role.ADMIN}
    for lane in document["lanes"]:
        if lane["key"] == "waiting_on_scott":
            lane["key"] = "waiting_on_me"
            lane["name"] = "Waiting on me"
        if cards_for is not None and lane["key"] not in cards_for:
            lane["cards"] = []
            continue
        for card in lane["cards"]:
            try:
                task = uow.tasks.get(card["id"])
            except TypeError:
                task = next(
                    (row for row in getattr(uow.tasks, "rows", []) if row.id == card["id"]), None
                )
            assert task is not None
            escalation = open_escalation(uow, task.id)
            get_pull_request = getattr(uow.pull_requests, "get_for_task", None)
            pull_request = get_pull_request(task.id) if get_pull_request else None
            moves = moves_for_card(task, escalation=escalation, pull_request=pull_request)
            if task.state is TaskState.PROPOSED:
                moves = [
                    next(move for move in moves if move["key"] == "approve"),
                    {
                        "key": "decline",
                        "label": "Decline",
                        "meaning": "Decline the proposed scope with an optional note.",
                    },
                ]
            card["actions"] = (
                [
                    {
                        "key": move["key"],
                        "label": ACTION_LABELS.get(move["key"], move["label"]),
                        "method": "POST",
                        "path": f"/v1/board/{task.id}/actions/{move['key']}",
                        "body": {"note": None},
                        "confirm": move["key"] in REMOVE_LIKE,
                        "note_optional": move["key"] == "decline",
                    }
                    for move in moves[:2]
                ]
                if can_act
                else []
            )
            card["age"]["entered_at"] = _local(card["age"]["entered_at"])
    keys = ("inbox", "waiting_on_me", "stuck", "in_progress", "holding_pen", "wins", "graveyard")
    order = {key: index for index, key in enumerate(keys)}
    document["lanes"].sort(key=lambda lane: order[lane["key"]])
    counts = {lane["key"]: lane["count"] for lane in document["lanes"]}
    return {
        "schema_version": "1.0",
        "generated_at": _local(now),
        "needs_me": counts["waiting_on_me"],
        "counts": counts,
        "lanes": document["lanes"],
    }


__all__ = ["board_resource"]
