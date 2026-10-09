"""hades #169: the Set up page. One ordered list of the first-run steps, each done or not
done from live state and linking to the page where it is done. The navigation shows
its entry, with the count of steps to do, only while one is undone."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.render import _operator_label, _page, _panel, _safe_value
from crucible.adapters.ui.session import _require
from crucible.application.admin import setup, status
from crucible.application.errors import ConflictError

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


PROVIDER_TONES = {"ok": "ok", "degraded": "warn", "unavailable": "bad"}


def checklist(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The steps as the page's checklist, the first undone one marked as next."""
    first = next((step["number"] for step in steps if not step["done"]), None)
    return [{**step, "next": step["number"] == first} for step in steps]


def _provider_detail(item: dict[str, Any]) -> str:
    """The one check an operator would act on: the first that failed, else capacity."""
    checks = item.get("checks") or {}
    for key, value in checks.items():
        if value is False or (isinstance(value, str) and "fail" in value.lower()):
            return f"{_operator_label(key)}: {_safe_value(key, value)}"
    capacity = (item.get("capabilities") or {}).get("max_concurrency")
    return f"up to {capacity} workers at once" if capacity else ""


def _service_rows(document: dict[str, Any]) -> list[list[Any]]:
    """What has to be running before any step matters: the supervisor and each
    execution provider."""
    supervisor = document["supervisor"]
    rows: list[list[Any]] = [
        [
            "Supervisor",
            {
                "kind": "status",
                "value": "healthy" if supervisor["healthy"] else "not healthy",
                "tone": "ok" if supervisor["healthy"] else "bad",
            },
            (supervisor.get("health_detail") or "") if not supervisor["healthy"] else "",
        ]
    ]
    rows.extend(
        [
            f"Provider: {item['name']}",
            {
                "kind": "status",
                "value": item["health"],
                "tone": PROVIDER_TONES.get(str(item["health"]), "warn"),
            },
            _provider_detail(item),
        ]
        for item in document["providers"]
    )
    return rows


def _harness_rows(readiness: dict[str, Any]) -> list[list[Any]]:
    """Each harness and what stands between it and a task, with the page that fixes it."""
    rows: list[list[Any]] = []
    for harness in readiness["harnesses"]:
        if harness["steps"]:
            rows.extend(
                [
                    harness["name"],
                    {"kind": "status", "value": "not ready", "tone": "warn"},
                    step["text"],
                    {"kind": "link", "href": step["fix"], "label": "Open"},
                ]
                for step in harness["steps"]
            )
        else:
            ready = harness["state"] == "ready"
            rows.append(
                [
                    harness["name"],
                    {
                        "kind": "status",
                        "value": "ready" if ready else "off",
                        "tone": "ok" if ready else "accent",
                    },
                    harness["note"],
                    "",
                ]
            )
    return rows


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    if ctx.admin is None:
        raise ConflictError("the administrative surface is not configured")
    steps = setup.setup_steps(ctx.admin, uow)
    remaining = setup.undone_count(steps)
    document = await status.status(ctx.admin, uow)
    return _page(
        request,
        principal,
        csrf,
        active="/ui/setup",
        heading="Set up",
        intro=(
            f"{remaining} of {len(steps)} first-run steps to do. Do them in order; each "
            "opens the page where it is done."
            if remaining
            else "Every first-run step is done. The work is on the Board."
        ),
        sections=[
            {"title": "First-run steps", "checklist": checklist(steps)},
            {
                "title": "Service",
                "columns": ["Part", "State", "Detail"],
                "rows": _service_rows(document),
                "details_label": "Provider checks",
                "details": [
                    {"title": f"Provider {item['name']}", "panel": _panel(item["checks"])}
                    for item in document["providers"]
                    if item.get("checks")
                ],
            },
            {
                "title": "Each harness",
                "columns": ["Harness", "State", "What is missing", ""],
                "rows": _harness_rows(document["readiness"]),
                "empty": "No harness is configured.",
            },
        ],
        badge=f"{remaining} to do" if remaining else "done",
        badge_kind="warn" if remaining else "ok",
    )
