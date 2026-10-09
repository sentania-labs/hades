"""/rooms (hades #208, ADR 0031): rooms whose transcript Hades owns.

The operator's routes take the orchestrator or operator role to write; any principal
reads. The runner's routes (`/session`, `/inbox`, `/turns/{seq}/events`, `/tools/{name}`,
`/runner/exit`) take only the room-scoped token Hades minted for that room's runner:
an ordinary bearer token is refused there, and a runner token is refused everywhere
else, because nothing but these routes knows its form."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from crucible.adapters.api.deps import Ctx, Orchestrator, Reader, UoW
from crucible.adapters.api.problems import problem_response
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.auth import authenticate
from crucible.application.errors import UnauthorizedError
from crucible.application.room_tools import run_tool
from crucible.application.rooms import (
    POLL_SECONDS,
    RoomContext,
    authenticate_runner,
    close_room,
    create_room,
    get_room,
    idle_timeout,
    inject_message,
    interrupt_room,
    list_rooms,
    read_identity,
    reclaim_rooms,
    record_runner_events,
    room_detail,
    room_view,
    runner_exited,
    runner_poll,
    runner_session,
    start_runner,
    stop_runner,
    switch_room,
    turn_view,
)
from crucible.contracts.rooms import (
    RoomCreateRequest,
    RoomDetail,
    RoomList,
    RoomMessageRequest,
    RoomSwitched,
    RoomSwitchRequest,
    RoomToolRequest,
    RoomToolResult,
    RoomTurnView,
    RoomView,
    RunnerEvents,
    RunnerEventsAccepted,
    RunnerExit,
    RunnerInbox,
    RunnerSession,
)
from crucible.domain.entities import Principal
from crucible.domain.rooms import TURN_WINDOW_MAX, Room, RoomState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork, UnitOfWorkFactory

router = ThreadedAPIRouter()

# How often a long poll and a stream look at the record again.
LOOK_SECONDS = 0.25
STREAM_DEFAULT_SECONDS = 300
STREAM_MAX_SECONDS = 3600


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("a bearer token is required")
    return authorization[7:].strip()


def _runner_room(uow: UnitOfWork, room_id: str, authorization: str | None) -> Room:
    return authenticate_runner(uow, room_id, _bearer(authorization))


def _idle_seed(rooms: RoomContext) -> int | None:
    return rooms.config.idle_timeout_minutes


async def _reclaim(factory: UnitOfWorkFactory, clock: Clock, rooms: RoomContext) -> None:
    """Stop the runners past their idle timeout or gone, then remove them at the
    provider. Runs on the room routes that start runners, so a reclaimed runner never
    holds a Pod for long after its room went quiet."""
    with factory() as uow:
        reclaimed = reclaim_rooms(uow, clock, idle_seed=_idle_seed(rooms))
        uow.commit()
    for item in reclaimed:
        await stop_runner(rooms, item.handle)


# ----- the operator's side -----------------------------------------------------


@router.post("/rooms", response_model=RoomView, status_code=201)
def create(body: RoomCreateRequest, ctx: Ctx, uow: UoW, principal: Orchestrator) -> RoomView:
    room = create_room(uow, ctx.clock, principal=principal, request=body)
    uow.commit()
    return room_view(room)


@router.get("/rooms", response_model=RoomList)
def rooms(
    uow: UoW,
    _principal: Reader,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    include_closed: bool = True,
) -> RoomList:
    found = list_rooms(uow, limit=limit, include_closed=include_closed)
    return RoomList(items=[room_view(room) for room in found])


@router.get("/rooms/{room_id}", response_model=RoomDetail)
def read(
    room_id: str,
    uow: UoW,
    _principal: Reader,
    window: Annotated[int | None, Query(ge=1, le=TURN_WINDOW_MAX)] = None,
) -> RoomDetail:
    return room_detail(uow, room_id, window=window)


@router.post("/rooms/{room_id}/messages", response_model=RoomTurnView, status_code=201)
async def message(
    room_id: str, body: RoomMessageRequest, ctx: Ctx, principal: Orchestrator
) -> RoomTurnView:
    """Write the user turn, then start a runner when none is warm. The turn is in the
    record before any runner sees it; a runner that cannot be started is a 503 whose
    message is still there for the next one."""
    await _reclaim(ctx.uow_factory, ctx.clock, ctx.rooms)
    with ctx.uow_factory() as uow:
        injected = inject_message(
            uow, ctx.clock, principal=principal, room_id=room_id, text=body.text
        )
        uow.commit()
    await stop_runner(ctx.rooms, injected.stale_handle)
    if injected.launch:
        await start_runner(
            ctx.uow_factory, ctx.clock, ctx.rooms, room_id=room_id, principal=principal.name
        )
    return turn_view(injected.turn)


@router.post("/rooms/{room_id}/interrupt", response_model=RoomView)
def interrupt(room_id: str, ctx: Ctx, uow: UoW, principal: Orchestrator) -> RoomView:
    room = interrupt_room(uow, ctx.clock, principal=principal, room_id=room_id)
    uow.commit()
    return room_view(room)


@router.post("/rooms/{room_id}/switch", response_model=RoomSwitched)
async def switch(
    room_id: str, body: RoomSwitchRequest, ctx: Ctx, principal: Orchestrator
) -> RoomSwitched:
    """Write the switch line and stop the runner; the next message starts a new runner
    on the new harness and model with the transcript replayed."""
    with ctx.uow_factory() as uow:
        room, turn, handle = switch_room(
            uow,
            ctx.clock,
            principal=principal,
            room_id=room_id,
            harness=body.harness,
            model=body.model,
        )
        uow.commit()
    await stop_runner(ctx.rooms, handle)
    return RoomSwitched(room=room_view(room), turn=turn_view(turn))


@router.post("/rooms/{room_id}/close", response_model=RoomView)
async def close(room_id: str, ctx: Ctx, principal: Orchestrator) -> RoomView:
    with ctx.uow_factory() as uow:
        room, handle = close_room(uow, ctx.clock, principal=principal, room_id=room_id)
        uow.commit()
    await stop_runner(ctx.rooms, handle)
    return room_view(room)


def stream_reader(
    request: Request, ctx: Ctx, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    """Any principal, authenticated in a short unit of work of its own: a stream lives
    for minutes and must not hold a pool connection for that long (as the log tail)."""
    with ctx.uow_factory() as uow:
        principal = authenticate(uow, _bearer(authorization))
    if principal is None:
        raise UnauthorizedError("token not recognized")
    request.state.principal = principal
    return principal


StreamReader = Annotated[Principal, Depends(stream_reader)]


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.get(
    "/rooms/{room_id}/stream",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream(
    room_id: str,
    request: Request,
    ctx: Ctx,
    _principal: StreamReader,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    max_seconds: Annotated[int, Query(ge=1, le=STREAM_MAX_SECONDS)] = STREAM_DEFAULT_SECONDS,
) -> Response:
    """Server-sent events: `room` (its state, on start and on every change), `turn` (a
    turn after `after_seq` as first seen, an open assistant turn with its text so far),
    `delta` (text appended to an open assistant turn), `turn_end` (the turn ended), and
    `closed`. The stream ends after `max_seconds` or when the room closes; a client
    reconnects with the last seq it saw."""
    with ctx.uow_factory() as initial:
        get_room(initial, room_id)
    permit = ctx.sse_tail_limiter.try_acquire()
    if permit is None:
        return problem_response(
            slug="sse-tail-limit-exceeded",
            title="Too many live streams",
            status=429,
            detail="the configured live stream limit is in use; retry shortly",
            instance=str(request.url.path),
            headers={"Retry-After": "1"},
        )

    async def events() -> AsyncIterator[str]:
        sent: dict[int, int] = {}
        ended: set[int] = set()
        # A reconnect names the last turn the client saw. Keep an open turn at that
        # sequence in the query so its current text, later deltas and turn_end are
        # replayed. An ended turn needs no replay and can remain behind the floor.
        with ctx.uow_factory() as fresh:
            resume_turn = fresh.room_turns.get(room_id, after_seq) if after_seq else None
        floor = after_seq - 1 if resume_turn is not None and resume_turn.open else after_seq
        state: RoomState | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_seconds
        try:
            while True:
                with ctx.uow_factory() as fresh:
                    room = fresh.rooms.get(room_id)
                    turns = list(fresh.room_turns.list_for_room(room_id, after_seq=floor))
                if room is None:
                    return
                if room.state is not state:
                    state = room.state
                    yield _sse("room", {"room_id": room.id, "state": state.value})
                for turn in turns:
                    if turn.seq not in sent:
                        sent[turn.seq] = len(turn.text)
                        yield _sse("turn", turn_view(turn).model_dump(mode="json"))
                    elif len(turn.text) > sent[turn.seq]:
                        delta = turn.text[sent[turn.seq] :]
                        sent[turn.seq] = len(turn.text)
                        yield _sse("delta", {"seq": turn.seq, "text": delta})
                    if not turn.open and turn.seq not in ended:
                        ended.add(turn.seq)
                        yield _sse(
                            "turn_end",
                            {
                                "seq": turn.seq,
                                "role": turn.role.value,
                                "interrupted": turn.interrupted,
                            },
                        )
                # Turns that ended and were sent are behind the floor from now on.
                for turn in turns:
                    if turn.seq == floor + 1 and turn.seq in ended:
                        floor = turn.seq
                if room.state is RoomState.CLOSED:
                    yield _sse("closed", {"room_id": room.id, "seq": floor})
                    return
                if loop.time() >= deadline or await request.is_disconnected():
                    return
                await asyncio.sleep(LOOK_SECONDS)
        finally:
            permit.release()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(permit.release),
    )


# ----- the runner's side -------------------------------------------------------


@router.get("/rooms/{room_id}/session", response_model=RunnerSession)
def session(
    room_id: str,
    ctx: Ctx,
    uow: UoW,
    authorization: Annotated[str | None, Header()] = None,
) -> RunnerSession:
    """The runner's session start: identity, card, recall, decisions, summary and the
    last turns, assembled the same way for every room kind."""
    room = _runner_room(uow, room_id, authorization)
    identity = read_identity(ctx.rooms.config.identity_path)
    answer = runner_session(
        uow, room, identity=identity, idle=idle_timeout(uow, _idle_seed(ctx.rooms))
    )
    uow.commit()
    return answer


@router.get("/rooms/{room_id}/inbox", response_model=RunnerInbox)
async def inbox(
    room_id: str,
    ctx: Ctx,
    authorization: Annotated[str | None, Header()] = None,
    wait: Annotated[int, Query(ge=0, le=POLL_SECONDS)] = POLL_SECONDS,
) -> RunnerInbox:
    """The runner's long poll: the next injected user turn, `interrupt`, or `stop`, or
    nothing after `wait` seconds. Each look is its own short transaction, so a poll
    never holds the room's row while it waits."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        with ctx.uow_factory() as uow:
            room = _runner_room(uow, room_id, authorization)
            answer = runner_poll(
                uow, ctx.clock, room, idle=idle_timeout(uow, _idle_seed(ctx.rooms))
            )
            uow.commit()
        if answer.reclaimed:
            await stop_runner(ctx.rooms, answer.reclaimed_handle)
        if not answer.empty or loop.time() >= deadline:
            return answer.inbox
        await asyncio.sleep(LOOK_SECONDS)


@router.post("/rooms/{room_id}/turns/{seq}/events", response_model=RunnerEventsAccepted)
def turn_events(
    room_id: str,
    seq: int,
    body: RunnerEvents,
    ctx: Ctx,
    uow: UoW,
    authorization: Annotated[str | None, Header()] = None,
) -> RunnerEventsAccepted:
    room = _runner_room(uow, room_id, authorization)
    accepted = record_runner_events(uow, ctx.clock, room, seq=seq, events=body.events)
    uow.commit()
    return accepted


@router.post("/rooms/{room_id}/tools/{tool}", response_model=RoomToolResult)
def tool(
    room_id: str,
    tool: str,
    body: RoomToolRequest,
    ctx: Ctx,
    uow: UoW,
    authorization: Annotated[str | None, Header()] = None,
) -> RoomToolResult:
    """One Hades tool call, run with the room's authority: its own room, its card and
    the cards it filed. A refusal is the problem response its status names."""
    room = _runner_room(uow, room_id, authorization)
    result = run_tool(uow, ctx.clock, room, tool, body.arguments)
    uow.commit()
    return RoomToolResult(tool=tool, ok=True, result=result)


@router.post("/rooms/{room_id}/runner/exit", status_code=204)
async def exit_runner(
    room_id: str,
    body: RunnerExit,
    ctx: Ctx,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """The runner is exiting (its idle timer ran out, or it failed): the room goes idle,
    the token is forgotten, and the runner's Pod is removed."""
    with ctx.uow_factory() as uow:
        room = _runner_room(uow, room_id, authorization)
        handle = runner_exited(uow, ctx.clock, room, why=body.reason)
        uow.commit()
    await stop_runner(ctx.rooms, handle)
    return Response(status_code=204)


__all__ = ["router"]
