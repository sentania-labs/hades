from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.render import _base, _page, templates
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    tokens,
)
from crucible.domain.entities import Principal, Role

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


@router.get("/tokens", response_class=HTMLResponse)
def tokens_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    items = tokens.list_principals(uow)
    admin = principal.role is Role.ADMIN

    def revoke(item: dict[str, Any]) -> Any:
        # The row's own action, never a typed ID (crucible#127). Revoking is hard to
        # reverse, so it asks for a reason (crucible#117).
        if not admin or item["disabled_at"] is not None:
            return ""
        return {
            "kind": "form",
            "action": "/ui/actions/token-revoke",
            "label": "Revoke",
            "danger": True,
            "reason": True,
            "hidden": {"principal_id": item["id"]},
        }

    sections: list[dict[str, Any]] = [
        {
            "title": "Principals",
            "columns": ["Name", "Role", "Created", "Revoked", ""],
            "rows": [
                [
                    item["name"],
                    item["role"],
                    item["created_at"],
                    item["disabled_at"] or "no",
                    revoke(item),
                ]
                for item in items
            ],
        }
    ]
    if admin:
        sections.append(
            {
                "title": "Rename principal",
                "note": (
                    "Its tasks and token follow it; past events keep the name they were "
                    "written with."
                ),
                "form": {
                    "action": "/ui/actions/token-rename",
                    "label": "Rename",
                    "fields": [
                        {
                            "name": "principal_id",
                            "label": "Principal",
                            "kind": "select",
                            "options": [
                                (item["id"], item["name"])
                                for item in items
                                if item["disabled_at"] is None
                            ],
                        },
                        {"name": "name", "label": "New name", "required": True},
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
        sections.append(
            {
                "title": "Create token",
                "note": (
                    "The token is shown on the next page once and is never stored in plaintext."
                ),
                "form": {
                    "action": "/ui/actions/token-create",
                    "label": "Create token",
                    "fields": [
                        {"name": "name", "label": "Principal name", "required": True},
                        {
                            "name": "role",
                            "label": "Role",
                            "kind": "select",
                            "options": [(role.value, role.value) for role in Role],
                        },
                        {"name": "reason", "label": "Reason"},
                    ],
                },
            }
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/tokens",
        data_page="tokens",
        heading="Tokens",
        intro="Who can sign in or call the API, and with which role.",
        sections=sections,
    )


async def _action_token_create(
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
    minted = tokens.create(
        ctx.admin,
        uow,
        principal=principal.name,
        name=form.get("name", ""),
        role=form.get("role", "observer"),
        reason=reason,
    )
    uow.commit()
    context = _base(
        request,
        principal,
        csrf,
        title="Token created",
        active="/ui/tokens",
        data_page="token-created",
    )
    context.update(
        token=minted.token,
        token_name=minted.principal.name,
        token_role=minted.principal.role.value,
    )
    response = templates.TemplateResponse(request=request, name="token_once.html", context=context)
    response.headers["Cache-Control"] = "no-store"
    return response


register("token-create", _action_token_create)


async def _action_token_revoke(
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
    revoked = tokens.revoke(
        ctx.admin,
        uow,
        principal=principal.name,
        principal_id=form.get("principal_id", ""),
        reason=reason,
    )
    uow.commit()
    await asyncio.to_thread(tokens.after_revoke, ctx.admin, revoked)
    return None


register("token-revoke", _action_token_revoke)


async def _action_token_rename(
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
    tokens.rename(
        ctx.admin,
        uow,
        principal=principal.name,
        principal_id=form.get("principal_id", ""),
        name=form.get("name", ""),
        reason=reason,
    )
    return None


register("token-rename", _action_token_rename)
