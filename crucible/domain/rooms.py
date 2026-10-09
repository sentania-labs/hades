"""Rooms: conversations whose transcript Hades owns (hades #208, ADR 0031).

A room is one conversation with Hades: the principal room, or a room about one card
(a task). Hades writes every turn to its own record (`room_turns`) before any harness
sees it, so a runner is a cache that can be stopped, reclaimed when idle, switched to
another harness or model, and started again from the record. The runner is a Pod (or a
container under `make up`) running `tools/room_runner.py`; it reads injected turns from
the room's inbox and streams its reply back as events on the assistant turn.

This module is the one definition of the room's states and what moves between them, and
of the words the room writes itself (the switch line). Nothing here does I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The operator is in America/Chicago (CONTRIBUTING: local Central time in operator-facing
# text); every time a room writes in words is in that zone.
LOCAL_ZONE = "America/Chicago"

# hades #208: only Claude Code is a room harness in this increment. Any other harness is
# refused with 409 until its runner exists.
ROOM_HARNESSES: frozenset[str] = frozenset({"claude_code"})

DEFAULT_IDLE_TIMEOUT_MINUTES = 30
MAX_IDLE_TIMEOUT_MINUTES = 24 * 60

# GET /v1/rooms/{id} returns at most this many of the newest turns by default.
TURN_WINDOW_DEFAULT = 50
TURN_WINDOW_MAX = 500

MAX_MESSAGE_CHARS = 32_000
MAX_DELTA_CHARS = 64_000
MAX_TOOL_CALLS_PER_TURN = 200

# The Hades tools a room runner exposes to the model, as in-process SDK MCP tools on the
# server named `hades`. `hades_answer_question` waits on the comment-states API (FDY-0586),
# which is not on main yet, so it is not here.
TOOL_SERVER = "hades"
HADES_TOOLS: tuple[str, ...] = (
    "hades_recall",
    "hades_record_decision",
    "hades_file_card",
    "hades_read_task",
    "hades_post_note",
)
# What the SDK calls them: `mcp__<server>__<tool>`. These, and only these, are allowed.
ALLOWED_TOOLS: tuple[str, ...] = tuple(f"mcp__{TOOL_SERVER}__{name}" for name in HADES_TOOLS)
# The built-ins a room never has (docs/spikes/room-runner-sdk.md (b)); `tools=[]` removes
# them from the model's list as well, and this list is the second lock.
DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Agent",
)

# The ledger channel a decision recorded in a room carries.
ROOM_CHANNEL = "room"


class RoomKind(StrEnum):
    PRINCIPAL = "principal"
    CARD = "card"


class RoomState(StrEnum):
    """`idle`: no runner. `starting`: a runner was launched and has not asked for work
    yet. `warm`: a runner is polling the inbox. `interrupted`: the operator stopped the
    turn that is running and the runner has not yet said it stopped. `closed`: the room
    takes nothing more."""

    IDLE = "idle"
    STARTING = "starting"
    WARM = "warm"
    INTERRUPTED = "interrupted"
    CLOSED = "closed"


class TurnRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class RoomEvent(StrEnum):
    """What moves a room between states."""

    # A message arrived and no runner is up: Hades launches one.
    LAUNCH = "launch"
    # The launch failed; the message stays in the record for the next one.
    LAUNCH_FAILED = "launch_failed"
    # The runner asked the inbox for work.
    RUNNER_READY = "runner_ready"
    # The operator stopped the running turn.
    INTERRUPT = "interrupt"
    # The runner said the turn ended (finished or stopped).
    TURN_ENDED = "turn_ended"
    # The runner goes away: a switch, the idle reclaim, or the runner exiting on its own.
    STOP = "stop"
    CLOSE = "close"


class RoomTransitionError(Exception):
    """A room event the room's state does not allow."""


_LIVE = (RoomState.STARTING, RoomState.WARM, RoomState.INTERRUPTED)

TRANSITIONS: dict[tuple[RoomState, RoomEvent], RoomState] = {
    (RoomState.IDLE, RoomEvent.LAUNCH): RoomState.STARTING,
    (RoomState.STARTING, RoomEvent.LAUNCH_FAILED): RoomState.IDLE,
    (RoomState.STARTING, RoomEvent.RUNNER_READY): RoomState.WARM,
    (RoomState.WARM, RoomEvent.RUNNER_READY): RoomState.WARM,
    (RoomState.INTERRUPTED, RoomEvent.RUNNER_READY): RoomState.INTERRUPTED,
    (RoomState.WARM, RoomEvent.INTERRUPT): RoomState.INTERRUPTED,
    (RoomState.WARM, RoomEvent.TURN_ENDED): RoomState.WARM,
    (RoomState.INTERRUPTED, RoomEvent.TURN_ENDED): RoomState.WARM,
    **{(state, RoomEvent.STOP): RoomState.IDLE for state in (RoomState.IDLE, *_LIVE)},
    **{(state, RoomEvent.CLOSE): RoomState.CLOSED for state in (RoomState.IDLE, *_LIVE)},
}


def next_state(state: RoomState, event: RoomEvent) -> RoomState:
    """The state `event` moves a room in `state` to, or RoomTransitionError."""
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise RoomTransitionError(
            f"a room that is {state.value} cannot take {event.value}"
        ) from None


def runner_live(state: RoomState) -> bool:
    """Whether a runner is, or is meant to be, up for a room in this state."""
    return state in _LIVE


@dataclass(slots=True)
class Room:
    """One conversation. `runner_handle` names the runner's Pod or container while one
    is up; `session_id` is the harness session that runner holds (a cache, never the
    record). `scope_task_ids` are the tasks the room's runner token may act on: the card
    of a card room and every card the room filed. `runner_key_salt` and
    `runner_key_digest` verify the runner's token, which Hades mints at each launch and
    forgets at each stop; the token itself is never stored. `inbox_cursor` is the seq
    of the last user turn handed to the runner, and `pending_control` an instruction the
    runner reads on its next poll (`interrupt` or `stop`). `runner_seen_at` is the
    runner's last inbox poll, which is how Hades tells a runner that went away."""

    id: str
    kind: RoomKind
    harness: str
    model: str
    state: RoomState
    created_at: datetime
    last_activity_at: datetime
    created_by: str
    card_task_id: str | None = None
    runner_handle: str | None = None
    session_id: str | None = None
    scope_task_ids: list[str] = field(default_factory=list)
    runner_key_salt: bytes | None = None
    runner_key_digest: bytes | None = None
    inbox_cursor: int = 0
    pending_control: str | None = None
    runner_seen_at: datetime | None = None


@dataclass(slots=True)
class RoomTurn:
    """One turn of a room's transcript, in `seq` order. A user or system turn is whole
    when it is written; an assistant turn is open (`ended_at` None) while its reply
    streams in, and `interrupted` when it was stopped or its runner went away.
    `tool_calls` records every tool call the runner's PreToolUse hook saw on the turn.
    `decision_id` names the ledger line recorded from this turn's words."""

    id: str
    room_id: str
    seq: int
    role: TurnRole
    text: str
    started_at: datetime
    ended_at: datetime | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False
    decision_id: str | None = None

    @property
    def open(self) -> bool:
        return self.ended_at is None


def local_time(at: datetime, zone: str = LOCAL_ZONE) -> str:
    """`2026-10-08 1:30 PM CDT`: a time as the operator reads it."""
    try:
        local = at.astimezone(ZoneInfo(zone))
    except ZoneInfoNotFoundError:
        local = at
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local:%Y-%m-%d} {hour}:{local:%M %p} {local.tzname() or 'UTC'}"


def switch_text(harness: str, model: str, at: datetime) -> str:
    """The system turn a switch writes (hades #208)."""
    return f"Switched to {harness} ({model}) at {local_time(at)}. Same history, same memory."


def harness_refusal(harness: str) -> str | None:
    """Why a harness cannot run a room, or None when it can."""
    if harness in ROOM_HARNESSES:
        return None
    return (
        f"{harness} is not yet a room harness; a room runs on "
        + ", ".join(sorted(ROOM_HARNESSES))
        + " in this release"
    )


__all__ = [
    "ALLOWED_TOOLS",
    "DEFAULT_IDLE_TIMEOUT_MINUTES",
    "DISALLOWED_TOOLS",
    "HADES_TOOLS",
    "LOCAL_ZONE",
    "MAX_DELTA_CHARS",
    "MAX_IDLE_TIMEOUT_MINUTES",
    "MAX_MESSAGE_CHARS",
    "MAX_TOOL_CALLS_PER_TURN",
    "ROOM_CHANNEL",
    "ROOM_HARNESSES",
    "TOOL_SERVER",
    "TRANSITIONS",
    "TURN_WINDOW_DEFAULT",
    "TURN_WINDOW_MAX",
    "Room",
    "RoomEvent",
    "RoomKind",
    "RoomState",
    "RoomTransitionError",
    "RoomTurn",
    "TurnRole",
    "harness_refusal",
    "local_time",
    "next_state",
    "runner_live",
    "switch_text",
]
