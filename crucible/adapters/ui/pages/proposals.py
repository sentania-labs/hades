"""hades #424: proposed tasks on the Tasks page and the Board.

A proposal is shown as its contract reads (title, objective, acceptance criteria,
required verification, context links), never as raw JSON, with the operator's four
answers beside it. Several proposals are approved in one action from either page; the
order the operator numbers them in is the queue order.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.render import _redirect
from crucible.adapters.ui.session import _csrf, _form, _require
from crucible.application.errors import ApplicationError, ConflictError, ForbiddenError
from crucible.application.proposals import (
    OPERATOR_ROLES,
    approve_batch,
    approve_task,
    reject_proposal,
    send_back_task,
)
from crucible.domain.entities import Principal, Task
from crucible.domain.lifecycle import TaskState
from crucible.ports.repository import UnitOfWork

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)

ORDER_PREFIX = "order_"


def proposed_tasks(uow: UnitOfWork) -> list[Task]:
    """The proposals, oldest first: the order the orchestrator wrote them in."""
    return sorted(
        uow.tasks.list_by_state(TaskState.PROPOSED), key=lambda task: (task.created_at, task.id)
    )


def _context_link(kind: str, ref: str, repository_url: str) -> dict[str, str]:
    base = repository_url.removesuffix(".git").rstrip("/")
    href = ""
    if ref.startswith("https://"):
        href = ref
    elif kind in {"issue", "pr"} and ref.lstrip("#").isdigit() and base.startswith("https://"):
        href = f"{base}/{'issues' if kind == 'issue' else 'pull'}/{ref.lstrip('#')}"
    return {"label": f"{kind}: {ref}", "href": href}


def _verification(item: dict[str, Any]) -> str:
    if item.get("kind", "command") == "command":
        expect = int(item.get("expect_exit", 0))
        suffix = f" (expects exit {expect})" if expect else ""
        return f"{item.get('id')}: {item.get('command')}{suffix}"
    return f"{item.get('id')}: artifact {item.get('path')}"


def contract_rows(uow: UnitOfWork, task: Task) -> list[list[Any]]:
    """The contract a person decides on, field by field, in words."""
    stored = uow.contracts.get(task.id, task.contract_version)
    document: dict[str, Any] = stored.document if stored else {}
    repository = uow.repositories.get(task.repository_id)
    repository_url = repository.url if repository else ""
    principal = uow.principals.get(task.principal_id)
    return [
        ["Title", document.get("title") or task.title],
        ["Objective", {"kind": "prose", "value": document.get("objective") or "none"}],
        [
            "Acceptance criteria",
            [
                f"{criterion.get('id')}: {criterion.get('text')}"
                for criterion in document.get("acceptance_criteria", [])
            ],
        ],
        [
            "Required verification",
            [_verification(item) for item in document.get("required_verification", [])],
        ],
        [
            "Context",
            {
                "kind": "links",
                "items": [
                    _context_link(str(item.get("kind")), str(item.get("ref")), repository_url)
                    for item in document.get("context", [])
                ],
            },
        ],
        ["Repository", (document.get("repository") or {}).get("name") or "none"],
        ["Proposed by", principal.name if principal else task.principal_id],
        ["Proposed", task.created_at.isoformat()],
        ["Contract version", task.contract_version],
    ]


def action_forms(task: Task) -> dict[str, Any]:
    """The operator's four answers, each needing a reason for the audit record."""
    action = f"/ui/tasks/{quote(task.id)}/proposal"
    return {
        "kind": "actions",
        "items": [
            {
                "action": action,
                "hidden": {"proposal_action": "approve"},
                "reason": True,
                "label": "Approve",
                "primary": True,
            },
            {
                "action": action,
                "hidden": {"proposal_action": "approve_with_note"},
                "reason": True,
                "note": "Note added to the objective (required)",
                "label": "Approve with note",
            },
            {
                "action": action,
                "hidden": {"proposal_action": "send_back"},
                "reason": True,
                "note": "Note to the orchestrator (required)",
                "label": "Send back",
            },
            {
                "action": action,
                "hidden": {"proposal_action": "reject"},
                "reason": True,
                "label": "Reject",
                "danger": True,
            },
        ],
    }


def proposal_sections(uow: UnitOfWork, principal: Principal) -> list[dict[str, Any]]:
    """One section per proposal, its contract readable and, for an operator, the forms."""
    sections = []
    for task in proposed_tasks(uow):
        rows = contract_rows(uow, task)
        if principal.role in OPERATOR_ROLES:
            rows.append(["Answer", action_forms(task)])
        sections.append(
            {
                "title": f"Proposed: {task.external_id}",
                "columns": ["Field", "Value"],
                "rows": rows,
            }
        )
    return sections


def batch_section(uow: UnitOfWork, principal: Principal) -> dict[str, Any] | None:
    """Approve several proposals in one action. The operator numbers the ones to approve;
    that order is the queue order, recorded on each approval."""
    tasks = proposed_tasks(uow)
    if not tasks or principal.role not in OPERATOR_ROLES:
        return None
    options = [("", "not selected")] + [(str(n), str(n)) for n in range(1, len(tasks) + 1)]
    return {
        "title": "Approve proposed tasks together",
        "note": (
            "Number the tasks to approve in the order they should start: 1 starts first. "
            "The order is recorded and the Queued column shows them in it. Every task "
            "approved here is started as it is proposed; approve one with a note from its "
            "own section."
        ),
        "form": {
            "action": "/ui/tasks/proposals/approve",
            "label": "Approve selected in this order",
            "fields": [
                {
                    "kind": "grid",
                    "label": "Proposed tasks",
                    "columns": ["Order", "Task", "Title"],
                    "rows": [
                        [
                            {
                                "kind": "select",
                                "name": f"{ORDER_PREFIX}{task.id}",
                                "label": f"Order for {task.external_id}",
                                "options": options,
                                "value": "",
                            },
                            {"value": task.external_id},
                            {"value": task.title},
                        ]
                        for task in tasks
                    ],
                },
                {"name": "reason", "label": "Reason (required; recorded)", "required": True},
            ],
        },
    }


def selection_order(form: dict[str, str]) -> list[str]:
    """The task ids the operator numbered, in their numbers' order. A number given twice
    is refused rather than guessed between."""
    chosen: list[tuple[int, str]] = []
    for key, value in form.items():
        if not key.startswith(ORDER_PREFIX) or not value.strip():
            continue
        try:
            position = int(value)
        except ValueError:
            raise ConflictError(f"{value!r} is not a position") from None
        chosen.append((position, key.removeprefix(ORDER_PREFIX)))
    positions = [position for position, _ in chosen]
    if len(set(positions)) != len(positions):
        raise ConflictError("two tasks have the same position; give each its own number")
    if not chosen:
        raise ConflictError("number at least one proposed task to approve")
    return [task_id for _, task_id in sorted(chosen)]


def _operator(principal: Principal) -> None:
    if principal.role not in OPERATOR_ROLES:
        raise ForbiddenError("operator or admin role required")


@router.post("/tasks/proposals/approve")
async def approve_selected(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    try:
        _csrf(form, csrf)
        _operator(principal)
        task_ids = selection_order(form)
        _batch_id, tasks = approve_batch(
            uow, ctx.clock, principal=principal, task_ids=task_ids, reason=form.get("reason", "")
        )
        uow.commit()
        order = ", ".join(task.external_id for task in tasks)
        return _redirect(form, f"Approved and queued in this order: {order}.")
    except ApplicationError as exc:
        return _redirect(form, exc.detail, kind="bad")


@router.post("/tasks/{task_id}/proposal")
async def answer_proposal(request: Request, task_id: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    try:
        _csrf(form, csrf)
        _operator(principal)
        action = form.get("proposal_action", "")
        reason = form.get("reason", "")
        note = form.get("note", "")
        if action == "approve":
            task = approve_task(uow, ctx.clock, principal=principal, task_id=task_id, reason=reason)
            message = f"Approved {task.external_id}; it is queued."
        elif action == "approve_with_note":
            if not note.strip():
                raise ConflictError("approve with note needs the note")
            task = approve_task(
                uow, ctx.clock, principal=principal, task_id=task_id, reason=reason, note=note
            )
            message = f"Approved {task.external_id} with your note; it is queued."
        elif action == "send_back":
            task = send_back_task(
                uow, ctx.clock, principal=principal, task_id=task_id, reason=reason, note=note
            )
            message = f"Sent {task.external_id} back to the orchestrator with your note."
        elif action == "reject":
            task = reject_proposal(
                uow, ctx.clock, principal=principal, task_id=task_id, reason=reason
            )
            message = f"Rejected {task.external_id}."
        else:
            raise ConflictError(f"unknown answer {action!r}")
        uow.commit()
        return _redirect(form, message)
    except ApplicationError as exc:
        return _redirect(form, exc.detail, kind="bad")


__all__ = [
    "action_forms",
    "batch_section",
    "contract_rows",
    "proposal_sections",
    "proposed_tasks",
    "selection_order",
]
