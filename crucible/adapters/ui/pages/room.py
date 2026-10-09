"""The signed-in principal room page (hades #208, FDY-0594)."""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any, cast

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.api.routers import rooms as rooms_routes
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.render import _base, templates
from crucible.adapters.ui.session import _require
from crucible.application.rooms import create_room, room_detail
from crucible.contracts.rooms import RoomCreateRequest, RoomMessageRequest, RoomSwitchRequest
from crucible.domain.entities import Principal, Role
from crucible.domain.rooms import RoomKind, RoomState, local_time
from crucible.settings import Settings

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)
WINDOW = 50
ROOM_COOKIE = "crucible_ui_room"


def _can_write(principal: Principal) -> bool:
    return principal.role in {Role.OPERATOR, Role.ORCHESTRATOR}


def _principal_room(uow: Any, principal: Principal, remembered: str | None = None) -> Any | None:
    rooms = [
        room
        for room in uow.rooms.list_recent(limit=500, include_closed=False)
        if room.kind is RoomKind.PRINCIPAL
    ]
    mine = [room for room in rooms if room.created_by == principal.id]
    candidates = mine if _can_write(principal) else rooms
    open_rooms = [room for room in candidates if room.state is not RoomState.CLOSED]
    selected = next((room for room in open_rooms if room.id == remembered), None)
    if selected is not None:
        return selected
    return max(open_rooms, key=lambda room: room.last_activity_at, default=None)


def _friendly_harness(value: str) -> str:
    return {"claude_code": "Claude"}.get(value, value.replace("_", " ").title())


def _friendly_model(value: str) -> str:
    words = value.removeprefix("claude-").replace("-", " ").title()
    return words.replace("Opus 5 5", "Opus 5.5")


def _connected(room: Any, now: datetime) -> str:
    identity = f"{_friendly_harness(room.harness)} ({_friendly_model(room.model)})"
    if room.state is RoomState.WARM:
        minutes = max(0, int((now - room.last_activity_at).total_seconds() // 60))
        return f"Connected: {identity}, warm {minutes} min"
    return f"Connected: {identity}, Cold"


def _tool_line(call: dict[str, Any]) -> str:
    name = str(call.get("name", "tool")).removeprefix("mcp__hades__").removeprefix("hades_")
    raw_data = call.get("input")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    labels = {
        "recall": "read memory",
        "record_decision": "record decision",
        "file_card": "file card",
        "read_task": "read task",
        "post_note": "post note",
    }
    detail = data.get("subject") or data.get("title") or data.get("task_id") or ""
    return f"{labels.get(name, name.replace('_', ' '))}{f': {detail}' if detail else ''}"


def _turn_view(turn: Any, timezone: str) -> dict[str, Any]:
    return {
        "id": turn.id,
        "seq": turn.seq,
        "role": turn.role.value,
        "text": turn.text,
        "time": local_time(turn.started_at, timezone),
        "open": turn.ended_at is None,
        "interrupted": turn.interrupted,
        "decision_id": turn.decision_id,
        "tools": [_tool_line(call) for call in turn.tool_calls],
    }


def _timezone(ctx: Any) -> str:
    return str(ctx.settings.service.render_timezone)


@router.get("/room", response_class=HTMLResponse)
def room_page(request: Request, ctx: Ctx, uow: UoW, window: int = WINDOW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    room = _principal_room(uow, principal, getattr(request, "cookies", {}).get(ROOM_COOKIE))
    if room is None and _can_write(principal):
        settings = cast(Settings, ctx.settings).rooms
        room = create_room(
            uow,
            ctx.clock,
            principal=principal,
            request=RoomCreateRequest(
                kind=RoomKind.PRINCIPAL,
                harness=settings.default_harness,
                model=settings.default_model,
            ),
        )
        uow.commit()
    detail = room_detail(uow, room.id, window=max(1, min(window, 500))) if room else None
    turns = [_turn_view(turn, _timezone(ctx)) for turn in detail.turns] if detail else []
    settings = cast(Settings, ctx.settings).rooms
    models = list(dict.fromkeys([*(settings.models or []), settings.default_model]))
    if room and room.model not in models:
        models.append(room.model)
    response = templates.TemplateResponse(
        request=request,
        name="room.html",
        context={
            **_base(request, principal, csrf, title="Hades", active="/ui/room"),
            "room": detail.room if detail else None,
            "turns": turns,
            "turns_total": detail.turns_total if detail else 0,
            "window": detail.window if detail else window,
            "can_write": bool(room and _can_write(principal)),
            "connected": _connected(room, ctx.clock.now()) if room else "No principal room yet",
            "harness": settings.default_harness,
            "models": models,
            "friendly_harness": _friendly_harness,
            "friendly_model": _friendly_model,
        },
    )
    if room:
        response.set_cookie(ROOM_COOKIE, room.id, httponly=True, samesite="strict", path="/ui")
    return response


def _authorized(request: Request, ctx: Any, uow: Any) -> tuple[Principal, str] | Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return JSONResponse({"detail": "sign in required"}, status_code=401)
    principal, csrf = found
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied or not hmac.compare_digest(supplied, csrf):
        return JSONResponse({"detail": "the request's CSRF token is invalid"}, status_code=403)
    if not _can_write(principal):
        return JSONResponse({"detail": "the room is read-only"}, status_code=403)
    return principal, csrf


@router.post("/room/{room_id}/messages")
async def message(request: Request, room_id: str, ctx: Ctx, uow: UoW) -> Response:
    auth = _authorized(request, ctx, uow)
    if isinstance(auth, Response):
        return auth
    body = await request.json()
    turn = await rooms_routes.message(
        room_id,
        RoomMessageRequest.model_validate(body),
        ctx,
        auth[0],
    )
    return JSONResponse(turn.model_dump(mode="json"), status_code=201)


@router.post("/room/{room_id}/interrupt")
async def interrupt(request: Request, room_id: str, ctx: Ctx, uow: UoW) -> Response:
    auth = _authorized(request, ctx, uow)
    if isinstance(auth, Response):
        return auth
    room = rooms_routes.interrupt(room_id, ctx, uow, auth[0])
    return JSONResponse(room.model_dump(mode="json"))


@router.post("/room/{room_id}/switch")
async def switch(request: Request, room_id: str, ctx: Ctx, uow: UoW) -> Response:
    auth = _authorized(request, ctx, uow)
    if isinstance(auth, Response):
        return auth
    body = RoomSwitchRequest.model_validate(await request.json())
    switched = await rooms_routes.switch(room_id, body, ctx, auth[0])
    return JSONResponse(switched.model_dump(mode="json"))


@router.get("/room/{room_id}/stream")
async def stream(request: Request, room_id: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return JSONResponse({"detail": "sign in required"}, status_code=401)
    after = max(0, int(request.query_params.get("after_seq", "0")))
    return await rooms_routes.stream(room_id, request, ctx, found[0], after, 300)
