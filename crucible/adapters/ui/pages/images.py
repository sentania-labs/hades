from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.render import _page
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    images,
)
from crucible.domain.entities import Principal, Role

router = APIRouter(prefix="/ui", include_in_schema=False)


def _image_label(entry: dict[str, Any] | None, harness: str) -> str:
    if not entry:
        return "none"
    return f"{entry['reference']} ({harness} {entry['version']})"


def _image_tag(reference: str) -> str:
    """Return the tag portion of a worker reference (the part after the first colon)."""
    if ":" in reference:
        return reference.split(":", 1)[1]
    return reference


def _image_rows(rows: list[dict[str, Any]], *, admin: bool) -> list[list[Any]]:
    """One row per harness (ADR 0018): its default, the image a rollback returns to, and
    a pulldown of the images that carry it at a supported version."""
    out: list[list[Any]] = []
    for row in rows:
        harness = row["harness"]
        current = row.get("current")
        previous = row.get("previous")
        actions: list[dict[str, Any]] = []
        if admin and row["choices"]:
            actions.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/image-promote",
                    "label": "Promote",
                    "primary": True,
                    "reason": "optional",
                    "hidden": {"harness": harness},
                    "select": {
                        "name": "digest",
                        "label": f"Image for {harness}",
                        "options": [
                            (choice["digest"], choice["reference"]) for choice in row["choices"]
                        ],
                        "selected": (current or {}).get("digest"),
                    },
                }
            )
        if admin and previous:
            actions.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/image-rollback",
                    "label": f"Roll back to {previous['reference']}",
                    "reason": "optional",
                    "hidden": {"harness": harness},
                }
            )
        out.append(
            [
                harness,
                (
                    {
                        "kind": "note",
                        "value": _image_tag(current["reference"]),
                        "title": current["reference"],
                        "hint": f"{harness} {current['version']}",
                    }
                    if current
                    else {"kind": "status", "value": "none promoted", "tone": "warn"}
                ),
                {
                    "kind": "note",
                    "value": _image_tag(previous["reference"]),
                    "title": previous["reference"],
                }
                if previous
                else "none",
                {"kind": "actions", "items": actions}
                if actions
                else {
                    "kind": "note",
                    "value": "No image to offer",
                    "hint": (
                        f"No provider sees a release image with {harness} "
                        f"{row['supported_versions']}"
                    ),
                },
            ]
        )
    return out


@router.get("/images", response_class=HTMLResponse)
async def images_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    rows = await images.defaults(ctx.admin, uow)
    items = await images.list_all(ctx.admin, uow)
    sections: list[dict[str, Any]] = [
        {
            "title": "Worker image per harness",
            "note": (
                "Each harness runs its own default image. Promoting one moves only that "
                "harness; Roll back returns it to the image it had before."
            ),
            "columns": ["Harness", "Current image", "Previous image", "Change"],
            "rows": _image_rows(rows, admin=principal.role is Role.ADMIN),
            "details_label": "Every image the providers see",
            "details": [
                {
                    "title": "Images",
                    "columns": ["Reference", "Harnesses", "Default for", "Digest"],
                    "rows": [
                        [
                            {
                                "kind": "note",
                                "value": _image_tag(item.get("reference") or ""),
                                "title": item.get("reference"),
                            },
                            ", ".join(
                                f"{name} {version}"
                                for name, version in sorted((item.get("harnesses") or {}).items())
                            ),
                            ", ".join(item.get("default_for") or []) or "none",
                            item.get("digest"),
                        ]
                        for item in items
                    ],
                }
            ],
        }
    ]
    return _page(
        request,
        principal,
        csrf,
        active="/ui/images",
        heading="Images",
        intro="Which worker image each harness runs. CI proof tags (ci-*) are not listed.",
        sections=sections,
    )


async def _action_image_promote(
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
    await images.promote(
        ctx.admin,
        uow,
        principal=principal.name,
        harness=form.get("harness", ""),
        digest=form.get("digest", ""),
        reason=reason,
    )
    return None


register("image-promote", _action_image_promote)


async def _action_image_rollback(
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
    await images.rollback(
        ctx.admin,
        uow,
        principal=principal.name,
        harness=form.get("harness", ""),
        reason=reason,
    )
    return None


register("image-rollback", _action_image_rollback)
