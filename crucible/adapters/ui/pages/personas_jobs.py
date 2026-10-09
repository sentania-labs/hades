"""Persona builder and scheduled jobs pages, registered without navigation links."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.render import _page, _redirect
from crucible.adapters.ui.session import _csrf, _form, _require
from crucible.application.catalog import load_catalog
from crucible.application.errors import ApplicationError
from crucible.application.personas_jobs import create_job, create_persona, run_job
from crucible.application.registry import REGISTERED_HARNESSES
from crucible.contracts.api import PersonaRequest, ScheduledJobRequest
from crucible.domain.entities import Role

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


def _write_allowed(role: Role) -> bool:
    return role in {Role.ORCHESTRATOR, Role.OPERATOR}


def _routing_candidates(uow: UoW) -> tuple[list[str], list[str]]:
    """Candidate harnesses and models named by the projects' current routing policies."""
    harnesses: set[str] = set()
    models: set[str] = set()
    for policy_name in uow.policies.list_names():
        versions = uow.policies.list_versions(policy_name)
        if not versions:
            continue
        ref = (
            max(versions, key=lambda item: item.version)
            .document.get("routing", {})
            .get("policy", {})
        )
        routing = uow.routing_policies.get(str(ref.get("name", "")), int(ref.get("version", 0)))
        if routing is None:
            continue
        for candidate in routing.document.get("models", []):
            if candidate.get("enabled", True):
                harnesses.add(str(candidate.get("harness", "")))
                models.add(str(candidate.get("id", "")))
    return sorted(name for name in harnesses if name), sorted(name for name in models if name)


@router.get("/personas", response_class=HTMLResponse)
def personas_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    catalog = load_catalog()
    candidate_harnesses, candidate_models = _routing_candidates(uow)
    fields: list[dict[str, Any]] = [
        {"name": "name", "label": "Name", "required": True},
        {"name": "role_text", "label": "Role text", "kind": "textarea", "required": True},
    ]
    fields.extend(
        {"name": f"skill:{item.name}", "label": f"Skill: {item.name}", "kind": "checkbox"}
        for item in catalog.skills
    )
    fields.extend(
        {"name": f"tool:{item.name}", "label": f"Tool: {item.name}", "kind": "checkbox"}
        for item in catalog.tools
    )
    fields += [
        {
            "name": "default_harness",
            "label": "Default harness",
            "kind": "select",
            "options": [
                (name, name) for name in (candidate_harnesses or sorted(REGISTERED_HARNESSES))
            ],
        },
        {
            "name": "default_model",
            "label": "Default model from routing candidates",
            "kind": "select",
            "options": [(name, name) for name in candidate_models],
        },
        {
            "name": "default_tier",
            "label": "Default tier",
            "kind": "select",
            "options": [(name, name.title()) for name in ("trivial", "standard", "complex")],
        },
        {
            "name": "budget_usd",
            "label": "Budget (USD)",
            "kind": "number",
            "value": "0",
            "required": True,
        },
    ]
    sections: list[dict[str, Any]] = [
        {
            "title": "Personas",
            "columns": ["Name", "Role", "Skills", "Tools", "Route", "Tier", "Budget"],
            "rows": [
                [
                    p.name,
                    p.role_text,
                    p.skills,
                    p.tools,
                    f"{p.default_harness} / {p.default_model}",
                    p.default_tier,
                    f"${p.budget_usd:.2f}",
                ]
                for p in uow.personas.list_all()
            ],
            "empty": "No personas yet.",
        }
    ]
    if _write_allowed(principal.role):
        sections.append(
            {
                "title": "Build a persona",
                "note": "Personas reference tools by name; credentials live in Admin.",
                "form": {"action": "/ui/personas", "label": "Create persona", "fields": fields},
            }
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/personas",
        heading="Personas",
        intro="Build reusable roles from the read-only catalog.",
        sections=sections,
    )


@router.post("/personas")
async def persona_create(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    try:
        _csrf(form, csrf)
        if not _write_allowed(principal.role):
            raise ValueError("orchestrator or operator role required")
        body = PersonaRequest(
            name=form.get("name", ""),
            role_text=form.get("role_text", ""),
            skills=[key[6:] for key in form if key.startswith("skill:")],
            tools=[key[5:] for key in form if key.startswith("tool:")],
            default_harness=form.get("default_harness", ""),
            default_model=form.get("default_model", ""),
            default_tier=form.get("default_tier", "standard"),
            budget_usd=float(form.get("budget_usd", "0")),
        )
        create_persona(uow, ctx.clock, principal, body)
        uow.commit()
        return _redirect(form, "Persona created.")
    except (ApplicationError, ValueError) as exc:
        return _redirect(form, getattr(exc, "detail", str(exc)), kind="bad")


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    personas = list(uow.personas.list_all())
    rows = [
        [
            j.name,
            next((p.name for p in personas if p.id == j.persona_id), j.persona_id),
            j.task_kind,
            j.cadence_label,
            j.results_to,
            "On" if j.enabled else "Off",
            {
                "kind": "form",
                "action": f"/ui/jobs/{j.id}/run-now",
                "label": "Run now",
                "primary": True,
            },
        ]
        for j in uow.scheduled_jobs.list_all()
    ]
    sections: list[dict[str, Any]] = [
        {
            "title": "Scheduled jobs",
            "columns": ["Name", "Persona", "Task", "Cadence", "Results", "Enabled", "Action"],
            "rows": rows,
            "empty": "No scheduled jobs yet.",
        }
    ]
    if _write_allowed(principal.role):
        sections.append(
            {
                "title": "Schedule a job",
                "note": (
                    "Carry notes forward is off by default; the run recalls its earlier "
                    "findings from memory."
                ),
                "form": {
                    "action": "/ui/jobs",
                    "label": "Create job",
                    "fields": [
                        {"name": "name", "label": "Name", "required": True},
                        {
                            "name": "persona_id",
                            "label": "Persona",
                            "kind": "select",
                            "options": [(p.id, p.name) for p in personas],
                        },
                        {
                            "name": "task_kind",
                            "label": "Task kind",
                            "kind": "select",
                            "options": [
                                ("prompt", "Plain-English prompt"),
                                ("script", "Run a script"),
                            ],
                        },
                        {
                            "name": "task_text",
                            "label": "Prompt or script",
                            "kind": "textarea",
                            "required": True,
                        },
                        {
                            "name": "cadence_preset",
                            "label": "Cadence preset",
                            "kind": "select",
                            "options": [
                                ("daily", "Daily at a time"),
                                ("weekly", "Weekly on a day"),
                                ("raw", "Raw cron"),
                            ],
                        },
                        {
                            "name": "daily_time",
                            "label": "Daily time (Central)",
                            "kind": "time",
                            "value": "09:00",
                        },
                        {
                            "name": "weekly_day",
                            "label": "Weekly day",
                            "kind": "select",
                            "options": [
                                ("1", "Monday"),
                                ("2", "Tuesday"),
                                ("3", "Wednesday"),
                                ("4", "Thursday"),
                                ("5", "Friday"),
                                ("6", "Saturday"),
                                ("0", "Sunday"),
                            ],
                        },
                        {
                            "name": "cadence",
                            "label": "Raw cron (minute hour day month weekday)",
                            "value": "0 9 * * *",
                            "required": True,
                        },
                        {
                            "name": "cadence_label",
                            "label": "Human cadence label",
                            "value": "Daily at 9:00 AM Central",
                            "required": True,
                        },
                        {
                            "name": "results_to",
                            "label": "Results destination",
                            "kind": "select",
                            "options": [
                                ("inbox_card", "Inbox card"),
                                ("chat_message", "Chat message"),
                                ("report_only", "Report only"),
                            ],
                        },
                        {
                            "name": "carry_notes_forward",
                            "label": (
                                "Carry notes forward: the run recalls its earlier findings "
                                "from memory"
                            ),
                            "kind": "checkbox",
                        },
                        {
                            "name": "project",
                            "label": "Project (registered repository name)",
                            "required": True,
                        },
                        {"name": "enabled", "label": "Enabled", "kind": "checkbox"},
                    ],
                },
            }
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/jobs",
        heading="Scheduled jobs",
        intro="Give a persona a task, cadence, and results destination. Times are America/Chicago.",
        sections=sections,
    )


@router.post("/jobs")
async def job_create(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    try:
        _csrf(form, csrf)
        if not _write_allowed(principal.role):
            raise ValueError("orchestrator or operator role required")
        body = ScheduledJobRequest(
            persona_id=form.get("persona_id", ""),
            name=form.get("name", ""),
            task_kind=form.get("task_kind", "prompt"),
            task_text=form.get("task_text", ""),
            cadence=form.get("cadence", ""),
            cadence_label=form.get("cadence_label", ""),
            results_to=form.get("results_to", "inbox_card"),
            carry_notes_forward=form.get("carry_notes_forward") == "true",
            project=form.get("project", ""),
            enabled=form.get("enabled") == "true",
        )
        create_job(uow, ctx.clock, principal, body)
        uow.commit()
        return _redirect(form, "Scheduled job created.")
    except (ApplicationError, ValueError) as exc:
        return _redirect(form, getattr(exc, "detail", str(exc)), kind="bad")


@router.post("/jobs/{job_id}/run-now")
async def job_run_now(request: Request, job_id: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    try:
        _csrf(form, csrf)
        if not _write_allowed(principal.role):
            raise ValueError("orchestrator or operator role required")
        run_job(uow, ctx.clock, principal, job_id)
        uow.commit()
        return _redirect(form, "Run filed as a normal task.")
    except (ApplicationError, ValueError) as exc:
        return _redirect(form, getattr(exc, "detail", str(exc)), kind="bad")
