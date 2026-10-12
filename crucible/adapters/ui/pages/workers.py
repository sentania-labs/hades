from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.pages.work import work_page
from crucible.adapters.ui.render import _page
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    status,
)
from crucible.domain.secrets import redact

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


# hades #576 U6: an attempt's state in colour, where the colour means the state.
STATE_TONES = {"running": "ok", "pending": "accent", "preparing": "accent", "launching": "accent"}


def worker_cards(uow: UoW, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One Neon card per active attempt: the task, its harness and model, when it started
    and last spoke, a link to its log, and the identifiers under Details."""
    cards = []
    for item in rows:
        task_id = str(item.get("task_id") or "")
        task = uow.tasks.get(task_id) if task_id else None
        state = str(item.get("state") or "")
        cards.append(
            {
                "href": f"/ui/tasks/{quote(task_id)}",
                "external_id": item.get("external_id") or task_id,
                "title": task.title if task is not None else None,
                "state": state,
                "state_words": state.replace("_", " ").capitalize() or "Unknown",
                "tone": STATE_TONES.get(state, "warn"),
                "facts": [
                    ("Harness", item.get("harness") or "not recorded"),
                    ("Model", item.get("model") or "not recorded"),
                    ("Started", item.get("started_at") or "not started"),
                    ("Heartbeat", item.get("last_heartbeat") or "none yet"),
                ],
                # The row's own log, never a typed attempt ID (crucible#127).
                "links": [
                    {
                        "href": f"/ui/workers/{quote(str(item.get('attempt_id')))}/logs",
                        "label": "Log",
                    }
                ],
                "details": [
                    ("Attempt", item.get("attempt_id")),
                    ("Task", task_id),
                    ("Image", item.get("image_digest") or "not recorded"),
                ],
            }
        )
    return cards


@router.get("/workers", response_class=HTMLResponse)
def workers_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    rows = status.workers(uow)
    return work_page(
        request,
        principal,
        csrf,
        active="/ui/workers",
        heading="Workers",
        intro="Attempts running now. Open a card's log to follow it.",
        sections=[
            {
                "title": "Active attempts",
                "count": len(rows),
                "empty": "No attempt is running.",
                "cards": worker_cards(uow, rows),
            }
        ],
    )


@router.get("/workers/{attempt_id}/logs", response_class=HTMLResponse)
def worker_logs(request: Request, attempt_id: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    chunks = uow.logs.list_from_offset(
        attempt_id, offset=max(0, uow.logs.last_offset(attempt_id) - 65536)
    )
    text = b"".join(chunk.content for chunk in chunks).decode("utf-8", "replace")
    return _page(
        request,
        principal,
        csrf,
        active="/ui/workers",
        heading=f"Log tail: {attempt_id}",
        intro="Stored stdout and stderr tail. Refresh to follow an active attempt.",
        sections=[{"title": "Tail", "text": redact(text[-65536:])}],
    )
