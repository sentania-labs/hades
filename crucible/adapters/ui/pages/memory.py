"""The Admin Memory page, /ui/memory (hades #208).

Two tabs over the same application services the API uses: Memory items (text, source,
when, scope, who promoted it, with Edit and Forget as clicks) and Decisions (the words,
the channel, when, what they apply to, the transcript link). Times are the operator's
local time: the configured zone when one is set, America/Chicago when the setting is
absent or still the stored form, UTC. The page is registered without a navigation link:
base.html and render.py belong to another task this wave, and the link is a one-line
follow-up there."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.render import _base, _localize, _redirect, templates
from crucible.adapters.ui.session import _csrf, _form, _require
from crucible.application.errors import ApplicationError
from crucible.application.memory import (
    forget_memory,
    list_ledger_decisions,
    list_memory,
    supersede_memory,
)
from crucible.contracts.api import MemorySupersedeRequest
from crucible.domain.entities import LedgerDecision, MemoryItem, Principal
from crucible.domain.memory import MEMORY_WRITER_ROLES

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)

PATH = "/ui/memory"
EXPLANATION = (
    "Transcripts stay per channel. Decisions and memory are shared by every channel and "
    "every persona. Minion findings become memory only when Hades or Scott promotes them."
)
TABS = (("memory", "Memory items"), ("decisions", "Decisions"))


LOCAL_ZONE = "America/Chicago"


def _timezone(request: Request) -> str:
    """The zone the page renders in. The page shows the operator's local time, and the
    operator is in America/Chicago: the setting's own default, UTC, is the stored form
    and not a local zone, so it is not honored here. A zone the operator configured
    explicitly (anything other than UTC) is."""
    settings = getattr(request.app.state.ctx, "settings", None)
    configured = str(settings.service.render_timezone).strip() if settings is not None else ""
    if not configured or configured.upper() in {"UTC", "ETC/UTC", "Z"}:
        return LOCAL_ZONE
    return configured


def _transcript_link(ref: str | None) -> dict[str, Any] | None:
    """A transcript ref that is a URL or a UI path is a link; any other ref is shown as
    the words it is."""
    if not ref:
        return None
    if ref.startswith(("https://", "http://")) or (ref.startswith("/ui") and "//" not in ref):
        return {"href": ref, "label": ref, "external": not ref.startswith("/ui")}
    return {"href": None, "label": ref, "external": False}


def _memory_row(item: MemoryItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "text": item.text,
        "source": item.source,
        "observed_at": item.observed_at,
        "promoted_at": item.promoted_at,
        "scope_tags": list(item.scope_tags),
        "scope_tags_text": ", ".join(item.scope_tags),
        "promoted_by": item.promoted_by,
    }


def _decision_row(line: LedgerDecision) -> dict[str, Any]:
    return {
        "id": line.id,
        "verbatim": line.verbatim,
        "principal": line.principal,
        "channel": line.channel,
        "said_at": line.said_at,
        "applies_to": list(line.applies_to),
        "transcript": _transcript_link(line.transcript_ref),
        "acted_by": line.acted_by,
        "acted_at": line.acted_at,
    }


def _tab(request: Request) -> str:
    wanted = request.query_params.get("tab", "memory")
    return wanted if wanted in dict(TABS) else "memory"


@router.get("/memory", response_class=HTMLResponse)
def memory_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    tab = _tab(request)
    timezone = _timezone(request)
    rows: list[dict[str, Any]] = (
        [_memory_row(item) for item in list_memory(uow)]
        if tab == "memory"
        else [_decision_row(line) for line in list_ledger_decisions(uow)]
    )
    context = _base(request, principal, csrf, title="Memory", active=PATH)
    context.update(
        heading="Memory",
        intro=EXPLANATION,
        tab=tab,
        tabs=[
            {"key": key, "label": label, "href": f"{PATH}?tab={key}", "on": key == tab}
            for key, label in TABS
        ],
        rows=_localize(rows, timezone),
        may_write=principal.role in MEMORY_WRITER_ROLES,
    )
    return templates.TemplateResponse(request=request, name="memory.html", context=context)


def _act(
    request: Request,
    ctx: Ctx,
    uow: UoW,
    form: dict[str, str],
    principal: Principal,
    csrf: str,
    item_id: str,
    *,
    forget: bool,
) -> Response:
    form["return_to"] = f"{PATH}?tab=memory"
    try:
        _csrf(form, csrf)
        if forget:
            forget_memory(uow, ctx.clock, principal=principal, item_id=item_id)
            message = "Forgotten. The item is no longer recalled; its record stays."
        else:
            tags_field = form.get("scope_tags")
            request_body = MemorySupersedeRequest(
                text=form.get("text", ""),
                source=(form.get("source") or "").strip() or None,
                scope_tags=(
                    [tag for tag in tags_field.split(",") if tag.strip()]
                    if tags_field is not None
                    else None
                ),
            )
            supersede_memory(
                uow, ctx.clock, principal=principal, item_id=item_id, request=request_body
            )
            message = "Edited. The new item supersedes the old one, which stays as history."
        uow.commit()
        return _redirect(form, message)
    except ApplicationError as exc:
        return _redirect(form, exc.detail, kind="bad")
    except ValueError as exc:
        return _redirect(form, str(exc), kind="bad")


@router.post("/memory/{item_id}/supersede")
async def memory_supersede(request: Request, item_id: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    return _act(request, ctx, uow, form, principal, csrf, item_id, forget=False)


@router.post("/memory/{item_id}/forget")
async def memory_forget(request: Request, item_id: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    return _act(request, ctx, uow, form, principal, csrf, item_id, forget=True)
