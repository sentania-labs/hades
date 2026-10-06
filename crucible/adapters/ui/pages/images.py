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


def _image_rows(rows: list[dict[str, Any]], *, admin: bool) -> list[list[Any]]:
    """One row per harness (ADR 0018): its default, the image a rollback returns to, and
    a single pulldown of all available images. Selecting the previous image triggers a
    rollback; selecting any other image promotes it."""
    out: list[list[Any]] = []
    for row in rows:
        harness = row["harness"]
        current = row.get("current")
        previous = row.get("previous")
        previous_digest = (previous or {}).get("digest")
        actions: list[dict[str, Any]] = []
        if admin and row["choices"]:
            # Build options: all choices, with the previous one marked "(previous)".
            # Include current image (always in choices) and previous image (if separate).
            # Ensure previous_digest appears even if it is not in choices (rare edge).
            choice_digests: set[str] = set()
            options: list[tuple[str, str]] = []
            for choice in row["choices"]:
                digest = choice["digest"]
                reference = choice["reference"]
                choice_digests.add(digest)
                if digest == previous_digest:
                    options.append((digest, f"{reference} (previous)"))
                else:
                    options.append((digest, reference))
            if previous_digest and previous_digest not in choice_digests:
                # Previous is not available through promote; include it with its label.
                prev_label = _image_label(previous, harness) if previous else "previous"
                options.append((previous_digest, f"{prev_label} (previous)"))
            actions.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/image-change",
                    "label": "Change image",
                    "primary": True,
                    "hidden": {"harness": harness},
                    "select": {
                        "name": "digest",
                        "label": f"Image for {harness}",
                        "options": options,
                        "selected": (current or {}).get("digest"),
                    },
                }
            )
        out.append(
            [
                harness,
                (
                    {
                        "kind": "note",
                        "value": current["reference"],
                        "hint": f"{harness} {current['version']}",
                    }
                    if current
                    else {"kind": "status", "value": "none promoted", "tone": "warn"}
                ),
                _image_label(previous, harness) if previous else "none",
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
                            item.get("reference"),
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


async def _action_image_change(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    """Unified handler: promote or roll back based on whether the selected digest
    matches the previous image. The dropdown in _image_rows includes every choice
    with the previous one marked '(previous)'; selecting it is a rollback."""
    assert ctx.admin is not None
    harness = form.get("harness", "")
    digest = form.get("digest", "")
    if not harness or not digest:
        return None
    # Determine if this is a rollback: the selected digest is the previous one.
    existing = uow.harness_images.get(harness)
    is_rollback = False
    if existing is not None and existing.previous_digest:
        is_rollback = existing.previous_digest == digest
    if is_rollback:
        await images.rollback(
            ctx.admin,
            uow,
            principal=principal.name,
            harness=harness,
            reason=reason,
        )
    else:
        await images.promote(
            ctx.admin,
            uow,
            principal=principal.name,
            harness=harness,
            digest=digest,
            reason=reason,
        )
    return None


register("image-change", _action_image_change)
