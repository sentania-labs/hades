from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.render import _document_section, _page
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    bootstrap,
)
from crucible.application.errors import (
    ApplicationError,
)
from crucible.domain.entities import Principal, Role

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


@router.get("/bootstrap", response_class=HTMLResponse)
def bootstrap_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    items = bootstrap.list_imports(uow)
    admin = principal.role is Role.ADMIN

    def actions(item: dict[str, Any]) -> dict[str, Any]:
        # The row's own actions, never a typed import ID (crucible#127).
        entries: list[dict[str, Any]] = [
            {
                "kind": "link",
                "href": f"/ui/bootstrap/{quote(item['import_id'])}",
                "label": "Show",
            }
        ]
        if admin and item["state"] == "verified":
            entries.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/bootstrap-commit",
                    "label": "Commit",
                    "danger": True,
                    "reason": True,
                    "hidden": {"import_id": item["import_id"]},
                }
            )
            # ADR 0029: an import that will not be committed is withdrawn, so a fresh
            # export of the same ledger can be imported.
            entries.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/bootstrap-discard",
                    "label": "Discard",
                    "danger": True,
                    "reason": True,
                    "hidden": {"import_id": item["import_id"]},
                }
            )
        return {"kind": "actions", "items": entries}

    return _page(
        request,
        principal,
        csrf,
        active="/ui/bootstrap",
        data_page="bootstrap",
        heading="Bootstrap imports",
        intro=(
            "Ledgers imported from Foundry, the commit that makes one authoritative, and the "
            "discard that withdraws one that will not be committed."
        ),
        sections=[
            {
                "title": "Imports",
                "empty": "No ledger has been imported.",
                "columns": ["Import", "State", "Tasks", "Verified", "Committed", ""],
                "rows": [
                    [
                        item["import_id"],
                        item["state"],
                        (item.get("counts") or {}).get("tasks", 0),
                        item["verified_at"],
                        item["committed_at"] or "no",
                        actions(item),
                    ]
                    for item in items
                ],
            }
        ],
    )


@router.get("/bootstrap/{import_id}", response_class=HTMLResponse)
def bootstrap_import_page(request: Request, import_id: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    try:
        document = bootstrap.show(uow, import_id)
    except ApplicationError as exc:
        return RedirectResponse(
            f"/ui/bootstrap?kind=bad&message={quote(exc.detail or exc.title)}", status_code=303
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/bootstrap",
        data_page="bootstrap",
        heading="Bootstrap manifest",
        intro=import_id,
        sections=[_document_section("Manifest", document)],
    )


async def _action_bootstrap_commit(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    assert ctx.admin is not None
    bootstrap.commit(
        ctx.admin,
        uow,
        principal=principal.name,
        import_id=form.get("import_id", ""),
        reason=reason,
    )
    return None


register("bootstrap-commit", _action_bootstrap_commit)


async def _action_bootstrap_discard(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    assert ctx.admin is not None
    bootstrap.discard(
        ctx.admin,
        uow,
        principal=principal.name,
        import_id=form.get("import_id", ""),
        reason=reason,
    )
    return None


register("bootstrap-discard", _action_bootstrap_discard)
