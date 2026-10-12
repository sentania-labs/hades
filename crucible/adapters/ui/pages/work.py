"""The Work pages that are not the board (Workers, All tasks) on the Neon card and
table components (hades #576 U6). Same sections as `_page`, rendered by `work.html`,
whose tables fold into labelled rows at phone width instead of scrolling sideways."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from crucible.adapters.ui.render import (
    _base,
    _empty_sections,
    _localize,
    _reason_fields,
    templates,
)
from crucible.domain.entities import Principal


def work_page(
    request: Request,
    principal: Principal,
    csrf: str,
    *,
    active: str,
    heading: str,
    intro: str,
    sections: list[dict[str, Any]],
) -> HTMLResponse:
    """Times in the operator's configured zone, America/Chicago unless set otherwise."""
    timezone = "America/Chicago"
    settings = getattr(getattr(request.app.state, "ctx", None), "settings", None)
    if settings is not None:
        timezone = settings.service.render_timezone
    context = _base(
        request, principal, csrf, title=heading, active=active, hidden=_empty_sections(request)
    )
    context.update(
        heading=heading,
        intro=str(_localize(intro, timezone)),
        sections=_reason_fields(_localize(sections, timezone)),
    )
    return templates.TemplateResponse(request=request, name="work.html", context=context)


__all__ = ["work_page"]
