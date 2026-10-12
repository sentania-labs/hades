from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.render import _page
from crucible.adapters.ui.session import _require
from crucible.application.admin.catalog import view

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


@router.get("/catalog", response_class=HTMLResponse)
def catalog_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    """Admin Catalog page. Read-only display of skills and tools."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    catalog = view()

    skill_rows: list[list[str]] = []
    for s in catalog.get("skills", []):
        skill_rows.append(
            [
                s["name"],
                s["summary"],
                s["owner"],
                s["instructions_path"],
                str(s["used_by"]),
            ]
        )

    tool_rows: list[list[str]] = []
    for t in catalog.get("tools", []):
        detail: str
        if t["kind"] == "mcp_server":
            detail = f"endpoint={t.get('endpoint', '')}"
        else:
            detail = f"command={t.get('command', '')}"
        creds = t.get("credential_ref", "")
        allowed = ", ".join(t.get("allowed_for", [])) or "all"
        tool_rows.append(
            [
                t["name"],
                t["kind"],
                detail,
                creds,
                allowed,
                str(t["used_by"]),
            ]
        )

    sections: list[dict[str, Any]] = [
        {
            "title": "Skills",
            "columns": ["Name", "Summary", "Owner", "Instructions path", "Used by"],
            "rows": skill_rows,
        },
        {
            "title": "Tools",
            "columns": [
                "Name",
                "Kind",
                "Endpoint / Command",
                "Credential ref",
                "Allowed for",
                "Used by",
            ],
            "rows": tool_rows,
        },
    ]

    return _page(
        request,
        principal,
        csrf,
        active="/ui/catalog",
        data_page="catalog",
        heading="Catalog",
        intro=(
            "Skills and tools that Hades knows. This page is read-only. "
            "Credentials are names only; actual values live in Admin only."
        ),
        sections=sections,
    )
