from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import crucible
from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.render import _operator_label, _page, _panel, _safe_value
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    status,
)
from crucible.application.errors import (
    ConflictError,
)

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


def _first_run_path(readiness: dict[str, Any]) -> list[dict[str, Any]]:
    """crucible#169: the first-run setup path. Five numbered steps, each linking to its
    page, shown as not done or done from the readiness state the Status page already
    computes. The path disappears once every step is done. No em-dashes."""
    steps: list[dict[str, Any]] = []
    harnesses = readiness.get("harnesses", [])
    readiness_steps = readiness.get("steps", [])

    # Step 1: Local gateway -- Hermes/Qwen Code needs an endpoint and a valid credential.
    # Done only when at least one hermes/qwen_code harness is ready (meaning endpoint
    # and credential are set and verified).
    gateway_done = False
    for h in harnesses:
        if h["name"] in ("hermes", "qwen_code") and h.get("state") == "ready":
            gateway_done = True
            break
    steps.append(
        {
            "number": 1,
            "label": "Local gateway",
            "link": "/ui/gateway",
            "done": gateway_done,
            "detail": "Set the gateway URL and credential for Hermes.",
        }
    )

    # Step 2: Images -- at least one harness has a promoted default image.
    images_done = any(
        h.get("state") in ("ready", "not_ready") and h.get("default_image") for h in harnesses
    )
    steps.append(
        {
            "number": 2,
            "label": "Images",
            "link": "/ui/images",
            "done": images_done,
            "detail": "Promote a worker image for each harness.",
        }
    )

    # Step 3: Credentials -- all non-Hermes harnesses have valid credentials.
    credentials_done = True
    credential_names = {"credential_missing", "credential_unreadable", "credential_invalid"}
    for h in harnesses:
        if h["name"] in ("hermes", "qwen_code"):
            continue
        for s in h.get("steps", []):
            if s.get("code", "") in credential_names:
                credentials_done = False
    # Also check readiness top-level: if no_ready_harness is present and there
    # are non-Hermes harnesses, credentials may still be missing.
    has_non_hermes_with_creds = any(h["name"] not in ("hermes", "qwen_code") for h in harnesses)
    if has_non_hermes_with_creds and not credentials_done:
        pass
    elif has_non_hermes_with_creds and not readiness.get("ready_harnesses"):
        # If no harness is ready at all, credentials may still be incomplete.
        credentials_done = False
    steps.append(
        {
            "number": 3,
            "label": "Credentials",
            "link": "/ui/credentials",
            "done": credentials_done,
            "detail": "Log in for each harness that needs a credential.",
        }
    )

    # Step 4: GitHub -- a repository is registered and the app is connected.
    github_done = all(
        s.get("code") not in ("no_repository", "github_app_not_connected") for s in readiness_steps
    )
    steps.append(
        {
            "number": 4,
            "label": "GitHub",
            "link": "/ui/github",
            "done": github_done,
            "detail": "Register a repository and connect the GitHub App.",
        }
    )

    # Step 5: Test each harness -- all non-off harnesses are ready.
    active_harnesses = [h for h in harnesses if h.get("state") != "off"]
    if active_harnesses:
        harness_test_done = all(h.get("state") == "ready" for h in active_harnesses)
    else:
        harness_test_done = False
    steps.append(
        {
            "number": 5,
            "label": "Test each harness",
            "link": "/ui/harnesses",
            "done": harness_test_done,
            "detail": "Run a test for every harness.",
        }
    )

    return steps


def _readiness_sections(readiness: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Any]]:
    """crucible#123: the to-do list from the status document's `readiness` part, which is
    computed from the same state the other pages show. One row per missing step, each
    with the page that fixes it; test fixtures are not in it. The per-harness list goes
    behind Details (crucible#115); its one-line summary is returned for the Service table."""
    todo = [[step["text"], step["fix"]] for step in readiness["steps"]]
    rows: list[list[Any]] = []
    for harness in readiness["harnesses"]:
        # A harness's own gaps are the to-do list only while none is ready; once one is,
        # the others' gaps are not what stands before a task (they stay under Details).
        if not readiness["ready_harnesses"]:
            todo.extend([step["text"], step["fix"]] for step in harness["steps"])
        if harness["steps"]:
            rows.extend(
                [harness["name"], "not ready", step["text"], step["fix"]]
                for step in harness["steps"]
            )
        else:
            rows.append(
                [
                    harness["name"],
                    "ready" if harness["state"] == "ready" else "off",
                    harness["note"],
                    "",
                ]
            )
    sections: list[dict[str, Any]] = []
    if todo:
        sections.append(
            {"title": "Before a task can run", "columns": ["Action", "Fix page"], "rows": todo}
        )
    ready = readiness["ready_harnesses"]
    summary = [
        "Harnesses",
        {
            "kind": "status",
            "value": f"ready: {', '.join(ready)}" if ready else "none ready",
            "tone": "ok" if ready else "warn",
        },
        {"kind": "link", "href": "/ui/harnesses", "label": "Open Harnesses"},
    ]
    detail = {
        "title": "Harness readiness",
        "columns": ["Harness", "State", "What is missing", "Fix page"],
        "rows": rows,
    }
    return sections, [summary, detail]


PROVIDER_TONES = {"ok": "ok", "degraded": "warn", "unavailable": "bad"}


def _provider_detail(item: dict[str, Any]) -> str:
    """The one check an operator would act on: the first that failed, else capacity."""
    checks = item.get("checks") or {}
    for key, value in checks.items():
        if value is False or (isinstance(value, str) and "fail" in value.lower()):
            return f"{_operator_label(key)}: {_safe_value(key, value)}"
    capacity = (item.get("capabilities") or {}).get("max_concurrency")
    return f"up to {capacity} workers at once" if capacity else ""


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    if ctx.admin is None:
        raise ConflictError("the administrative surface is not configured")
    document = await status.status(ctx.admin, uow)
    readiness = document["readiness"]
    sections, (harness_summary, harness_detail) = _readiness_sections(readiness)
    first_run = _first_run_path(readiness)
    any_not_done = any(not s["done"] for s in first_run)
    if any_not_done:
        sections.insert(
            0,
            {
                "title": "Before a task",
                "columns": ["#", "Step", ""],
                "rows": [
                    [
                        f"Step {s['number']}",
                        {"kind": "note", "value": s["label"], "hint": s["detail"]}
                        if not s["done"]
                        else {"kind": "status", "value": s["label"], "tone": "ok"},
                        {"kind": "link", "href": s["link"], "label": "Open"}
                        if not s["done"]
                        else "Done",
                    ]
                    for s in first_run
                ],
            },
        )
    supervisor = document["supervisor"]
    tasks_part = document["tasks"]
    attention = sum(len(rows) for rows in tasks_part["lists"].values())
    running = len(document["workers"])
    overview: list[list[Any]] = [
        [
            "Version",
            {
                "kind": "status",
                "value": crucible.__version__,
                "tone": "ok",
            },
            "",
        ],
        [
            "Supervisor",
            {
                "kind": "status",
                "value": "healthy" if supervisor["healthy"] else "not healthy",
                "tone": "ok" if supervisor["healthy"] else "bad",
            },
            {
                "kind": "note",
                "value": supervisor["health_detail"] if not supervisor["healthy"] else "",
                "hint": f"last tick {supervisor['last_tick_at'] or 'never'}",
            },
        ],
        *[
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
        ],
        [
            "Work",
            {
                "kind": "status",
                "value": f"{attention} need attention" if attention else "nothing waiting",
                "tone": "warn" if attention else "ok",
            },
            {"kind": "link", "href": "/ui/tasks", "label": f"{running} running; open Tasks"},
        ],
        [
            "Wakes",
            {
                "kind": "status",
                "value": f"{document['wakes']['unacked']} pending",
                "tone": "warn" if document["wakes"]["unacked"] else "ok",
            },
            {"kind": "link", "href": "/ui/wakes", "label": "Open Wakes"},
        ],
        harness_summary,
    ]
    internals = {
        key: value for key, value in supervisor.items() if key not in ("providers", "counts")
    }
    sections.append(
        {
            "title": "Service",
            "columns": ["Part", "State", ""],
            "rows": overview,
            "details": [
                harness_detail,
                {"title": "Supervisor", "panel": _panel(internals)},
                *[
                    {"title": f"Provider {item['name']} checks", "panel": _panel(item["checks"])}
                    for item in document["providers"]
                ],
            ],
        }
    )
    ready = readiness["ready"]
    ready_names = ", ".join(readiness["ready_harnesses"])
    return _page(
        request,
        principal,
        csrf,
        active="/ui",
        data_page="status",
        heading="Status",
        intro=(
            f"Ready for a task on {ready_names}."
            if ready
            else "Crucible cannot run a task yet. The list below says what it needs."
        ),
        sections=sections,
        badge="ready" if ready else "attention needed",
        badge_kind="ok" if ready else "warn",
    )
