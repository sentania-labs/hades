from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.render import _page
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    audit,
)

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    cursor = int(request.query_params.get("cursor", "0") or 0)
    document = audit.tail(uow, cursor=cursor, limit=100)
    more = document["next_cursor"] != cursor and bool(document["items"])
    return _page(
        request,
        principal,
        csrf,
        active="/ui/audit",
        heading="Audit",
        intro="Every administrative change and refusal: who, when, and why.",
        sections=[
            {
                "title": "Changes" if not cursor else f"Changes after {cursor}",
                "empty": "No administrative change recorded.",
                "columns": ["When", "What", "Who", "Reason", ""],
                "rows": [
                    [
                        item["ts"],
                        str(item["kind"]).replace("_", " ").capitalize(),
                        item["principal"],
                        (item["payload"] or {}).get("reason") or "none given",
                        {"kind": "more", "label": "Details", "value": item["payload"]},
                    ]
                    for item in document["items"]
                ],
            },
            *(
                [
                    {
                        "title": "More",
                        "rows": [
                            [
                                {
                                    "kind": "link",
                                    "href": f"/ui/audit?cursor={document['next_cursor']}",
                                    "label": "Next page",
                                }
                            ]
                        ],
                        "columns": [""],
                    }
                ]
                if more
                else []
            ),
        ],
    )
