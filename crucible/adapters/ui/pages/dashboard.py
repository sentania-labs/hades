"""`/ui`, which was the Status page. hades #576 U5 and #214 folded Status into the
Board's service strip and the Admin About block, and #169 moved its setup list to Set
up, so `/ui` now sends the operator to whichever of those two comes first."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.session import _require
from crucible.application.admin import setup

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    undone = ctx.admin is not None and setup.undone_count(setup.setup_steps(ctx.admin, uow))
    return RedirectResponse("/ui/setup" if undone else "/ui/board", status_code=303)
