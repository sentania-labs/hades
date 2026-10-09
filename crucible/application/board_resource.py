"""Role-aware JSON projection for the operator board."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from crucible.application.admin.board_lanes import board_lanes_view
from crucible.application.board_actions import moves_for_card
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
# The board's lane key for the projection's Waiting on Scott lane.
PROJECTION_KEYS = {"waiting_on_me": "waiting_on_scott"}
# What each lane asks of the operator first; moves not named follow in move order.
LANE_ACTION_PRIORITY: dict[str, tuple[str, ...]] = {
    "inbox": ("approve", "decline"),
    "waiting_on_me": ("answer", "accept", "cancel"),
    "stuck": ("answer", "correct_remote", "correct_last", "cancel"),
    "in_progress": ("accept", "answer", "cancel"),
    "holding_pen": ("start", "answer", "cancel"),
}


def _local(moment: datetime) -> str:
    return moment.astimezone(CENTRAL).isoformat(timespec="seconds")


def _card_moves(lane: str, record: dict[str, Any]) -> list[dict[str, str]]:
    """At most two moves, chosen by what the lane asks of the operator rather than by
    the generic move order, so a Waiting on me card always offers Answer."""
    task = record["task"]
    moves = moves_for_card(
        task, escalation=record["escalation"], pull_request=record["pull_request"]
    )
    if task.state is TaskState.PROPOSED:
        moves = [
            next(move for move in moves if move["key"] == "approve"),
            {
                "key": "decline",
                "label": "Decline",
                "meaning": "Decline the proposed scope with an optional note.",
            },
        ]
    priority = LANE_ACTION_PRIORITY.get(lane, ())
    ranked = sorted(
        enumerate(moves),
        key=lambda item: (
            priority.index(item[1]["key"]) if item[1]["key"] in priority else len(priority),
            item[0],
        ),
    )
    return [move for _index, move in ranked[:2]]


def board_resource(
    uow: UnitOfWork,
    now: datetime,
    principal: Principal,
    *,
    cards_for: frozenset[str] | None = None,
) -> dict[str, Any]:
    selected = (
        frozenset(PROJECTION_KEYS.get(key, key) for key in cards_for)
        if cards_for is not None
        else None
    )
    document = board_lanes_view(uow, now, cards_for=selected)
    records = document.pop("records")
    can_act = principal.role in {Role.OPERATOR, Role.ADMIN}
    for lane in document["lanes"]:
        if lane["key"] == "waiting_on_scott":
            lane["key"] = "waiting_on_me"
            lane["name"] = "Waiting on me"
        for card in lane["cards"]:
            record = records[card["id"]]
            card["actions"] = (
                [
                    {
                        "key": move["key"],
                        "label": ACTION_LABELS.get(move["key"], move["label"]),
                        "method": "POST",
                        "path": f"/v1/board/{card['id']}/actions/{move['key']}",
                        "body": {"note": None},
                        "confirm": move["key"] in REMOVE_LIKE,
                        "note_optional": move["key"] == "decline",
                    }
                    for move in _card_moves(lane["key"], record)
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
