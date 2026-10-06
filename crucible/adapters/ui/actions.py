from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.render import _redirect
from crucible.adapters.ui.session import _admin, _csrf, _form, _require
from crucible.application.errors import (
    ApplicationError,
    ConflictError,
)
from crucible.domain.entities import Principal

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


ActionHandler = Callable[
    [Request, str, Ctx, UoW, Principal, str, dict[str, str], str | None],
    Awaitable[Response | None],
]
handlers: dict[str, ActionHandler] = {}


def register(name: str, handler: ActionHandler) -> None:
    handlers[name] = handler


@router.post("/actions/{action}")
async def action(request: Request, action: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    try:
        _csrf(form, csrf)
        _admin(principal)
        if ctx.admin is None:
            raise ConflictError("the administrative surface is not configured")
        reason = form.get("reason")
        handler = handlers.get(action)
        if handler is None:
            raise ConflictError(f"unknown UI action {action!r}")
        response = await handler(request, action, ctx, uow, principal, csrf, form, reason)
        if response is not None:
            return response
        uow.commit()
        return _redirect(form, f"Completed: {action}.")
    except (ApplicationError, ValueError, json.JSONDecodeError) as exc:
        detail = exc.detail if isinstance(exc, ApplicationError) else str(exc)
        return _redirect(form, detail, kind="bad")
