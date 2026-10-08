from __future__ import annotations

from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.render import _page
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    status,
)
from crucible.domain.secrets import redact

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


@router.get("/workers", response_class=HTMLResponse)
def workers_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    rows = status.workers(uow)
    return _page(
        request,
        principal,
        csrf,
        active="/ui/workers",
        heading="Active workers",
        intro="Attempts running now. Open a row's log to follow it.",
        sections=[
            {
                "title": "Active attempts",
                "empty": "No attempt is running.",
                "columns": ["Task", "Harness", "Model", "State", "Started", "Heartbeat", ""],
                "rows": [
                    [
                        item.get("external_id") or item.get("task_id"),
                        item.get("harness"),
                        item.get("model"),
                        item.get("state"),
                        item.get("started_at"),
                        item.get("last_heartbeat"),
                        # The row's own log, never a typed attempt ID (crucible#127).
                        {
                            "kind": "link",
                            "href": f"/ui/workers/{quote(str(item.get('attempt_id')))}/logs",
                            "label": "Log",
                        },
                    ]
                    for item in rows
                ],
                "details": [
                    {
                        "title": "Identifiers",
                        "columns": ["Attempt", "Task", "Image"],
                        "rows": [
                            [item.get("attempt_id"), item.get("task_id"), item.get("image_digest")]
                            for item in rows
                        ],
                    }
                ],
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
