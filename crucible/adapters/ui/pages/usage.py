from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.pages.board import _quality_sections, _tokens_sections
from crucible.adapters.ui.render import _page
from crucible.adapters.ui.session import _require
from crucible.application.admin.board import board_view

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


@router.get("/usage", response_class=HTMLResponse)
def usage_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    document = board_view(uow, ctx.clock.now())
    return _page(
        request,
        principal,
        csrf,
        active="/ui/usage",
        heading="Usage",
        intro="Recorded token use and recent quality evidence.",
        sections=[*_tokens_sections(document), *_quality_sections(document)],
    )
