"""Request and response schemas for /v1/rooms (hades #208, ADR 0031).

Two audiences use these routes. The operator's clients (orchestrator and operator roles;
an observer reads) create rooms, inject messages, watch the stream, interrupt, switch and
close. The room's own runner, holding the room-scoped token Hades minted for it, reads
its session start and its inbox, posts the events of the turn it is answering, and
calls the Hades tools; that token is good for its own room and nothing else."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from crucible.contracts.api import Response
from crucible.contracts.common import Rfc3339, StrictModel
from crucible.domain.rooms import (
    MAX_DELTA_CHARS,
    MAX_MESSAGE_CHARS,
    RoomKind,
    RoomState,
    TurnRole,
)

_NAME = Field(min_length=1, max_length=128)


class RoomCreateRequest(StrictModel):
    """A principal room, or a card room that names its task (`card_task_id`, the task's
    id or its external id). `harness` takes any string so that a harness that is not a
    room harness yet is refused with 409, not a schema error."""

    kind: RoomKind
    harness: str = Field(min_length=1, max_length=64)
    model: str = _NAME
    card_task_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _card(self) -> RoomCreateRequest:
        if self.kind is RoomKind.CARD and not self.card_task_id:
            raise ValueError("a card room names its task in card_task_id")
        if self.kind is RoomKind.PRINCIPAL and self.card_task_id:
            raise ValueError("the principal room names no task")
        return self


class RoomMessageRequest(StrictModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    @model_validator(mode="after")
    def _words(self) -> RoomMessageRequest:
        if not self.text.strip():
            raise ValueError("a message needs some words")
        return self


class RoomSwitchRequest(StrictModel):
    harness: str = Field(min_length=1, max_length=64)
    model: str = _NAME


class RoomTurnView(Response):
    id: str
    room_id: str
    seq: int
    role: TurnRole
    text: str
    tool_calls: list[dict[str, Any]]
    started_at: Rfc3339
    ended_at: Rfc3339 | None
    interrupted: bool
    decision_id: str | None


class RoomView(Response):
    id: str
    kind: RoomKind
    card_task_id: str | None
    harness: str
    model: str
    state: RoomState
    created_at: Rfc3339
    last_activity_at: Rfc3339
    runner_handle: str | None
    session_id: str | None
    created_by: str
    scope_task_ids: list[str]


class RoomDetail(Response):
    """The room and a bounded window of its newest turns, oldest of them first.
    `turns_total` counts every turn; `window` is how many were asked for."""

    room: RoomView
    turns: list[RoomTurnView]
    turns_total: int
    window: int


class RoomList(Response):
    items: list[RoomView]


class RoomSwitched(Response):
    """The room after a switch, and the system turn the switch wrote."""

    room: RoomView
    turn: RoomTurnView


class RunnerExit(StrictModel):
    """The runner says it is exiting: `idle` when its own idle timer ran out."""

    reason: str = Field(default="idle", min_length=1, max_length=200)


# ----- the runner's side --------------------------------------------------


class RunnerSession(Response):
    """What a runner starts its harness session with: the system prompt text Hades
    assembled (identity, card, recall, decisions, summary, last turns), the harness and
    model, the Hades tools it may expose, and the idle timeout after which it exits."""

    room_id: str
    harness: str
    model: str
    system_prompt: str
    subject: str
    allowed_tools: list[str]
    disallowed_tools: list[str]
    idle_timeout_seconds: int


class RunnerMessage(Response):
    """One injected user turn, and the open assistant turn the runner's reply streams
    into (`POST /rooms/{id}/turns/{assistant_seq}/events`)."""

    user_seq: int
    assistant_seq: int
    text: str


class RunnerInbox(Response):
    """`control` is `interrupt` (stop the running turn), `stop` (exit now: a switch, a
    close or the idle reclaim), or None. `message` is the next user turn, handed out
    only while no assistant turn is open."""

    control: Literal["interrupt", "stop"] | None
    message: RunnerMessage | None


class RunnerEvent(StrictModel):
    """One event of the turn being answered. `delta` appends text; `tool_call` is the
    PreToolUse hook's record of a call; `session` names the harness session; `end`
    closes the turn (`interrupted` when it was stopped or failed, `error` saying why)."""

    kind: Literal["delta", "tool_call", "session", "end"]
    text: str | None = Field(default=None, max_length=MAX_DELTA_CHARS)
    name: str | None = Field(default=None, max_length=256)
    input: dict[str, Any] | None = None
    tool_use_id: str | None = Field(default=None, max_length=256)
    session_id: str | None = Field(default=None, max_length=256)
    interrupted: bool = False
    error: str | None = Field(default=None, max_length=2000)


class RunnerEvents(StrictModel):
    events: list[RunnerEvent] = Field(min_length=1, max_length=500)


class RunnerEventsAccepted(Response):
    seq: int
    accepted: int
    ended: bool
    control: Literal["interrupt", "stop"] | None


class RoomToolRequest(StrictModel):
    """A Hades tool call from the runner: the tool's arguments as the model gave them."""

    arguments: dict[str, Any] = Field(default_factory=dict)


class RoomToolResult(Response):
    tool: str
    ok: bool
    result: dict[str, Any]


__all__ = [
    "RoomCreateRequest",
    "RoomDetail",
    "RoomList",
    "RoomMessageRequest",
    "RoomSwitchRequest",
    "RoomSwitched",
    "RoomToolRequest",
    "RoomToolResult",
    "RoomTurnView",
    "RoomView",
    "RunnerEvent",
    "RunnerEvents",
    "RunnerEventsAccepted",
    "RunnerExit",
    "RunnerInbox",
    "RunnerMessage",
    "RunnerSession",
]
