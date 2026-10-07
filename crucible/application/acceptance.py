"""Acceptance for the collected head (09, 11).

Hades records acceptance from passing gates and the worker self-review. The acceptance
API remains available for tasks left in awaiting_acceptance by older deployments.
"""

from __future__ import annotations

from crucible.application.decisions import record_decision
from crucible.application.errors import (
    ForbiddenError,
    NotFoundError,
    TransitionNotAllowedError,
)
from crucible.application.task_access import require_task_principal
from crucible.application.transitions import move_task, record_event, require_contract
from crucible.application.wakes import create_wake
from crucible.contracts.api import AcceptRequest, DecisionRequest
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    AcceptanceResult,
    AcceptanceVerdict,
    EscalationState,
    Principal,
    Role,
    Task,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

PUBLISHED_DELIVERABLES = frozenset({"pull_request", "branch"})


def record_gate_acceptance(uow: UnitOfWork, clock: Clock, *, task: Task) -> AcceptanceResult:
    """Record Hades' acceptance once every blocking gate and the self-review pass.

    The task owner remains the relational principal for the result; the audit event names
    Crucible as the actor. This is an automatic consequence of the verified gate result,
    not an orchestrator decision.
    """
    head = task.head_sha or ""
    now = clock.now()
    uow.acceptance.supersede_for_task(task.id, now)
    result = AcceptanceResult(
        id=new_id(),
        task_id=task.id,
        head_sha=head,
        principal_id=task.principal_id,
        verdict=AcceptanceVerdict.ACCEPTED,
        reasoning=(
            "Every blocking pre-PR gate passed and the completion report includes "
            "the worker self-review."
        ),
        created_at=now,
    )
    uow.acceptance.add(result)
    record_event(
        uow,
        clock,
        EventKind.ACCEPTANCE_RECORDED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        payload={
            "acceptance_id": result.id,
            "head_sha": head,
            "verdict": result.verdict.value,
            "reasoning": result.reasoning,
            "automatic": True,
        },
    )
    return result


def deliverable_kinds(uow: UnitOfWork, task: Task) -> list[str]:
    stored = require_contract(uow, task)
    return [str(d.get("kind")) for d in stored.document.get("deliverables", [])]


def record_acceptance(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, task_id: str, request: AcceptRequest
) -> Task:
    if principal.role not in (Role.ORCHESTRATOR, Role.OPERATOR, Role.ADMIN):
        raise ForbiddenError("only an orchestrator, operator, or admin principal records acceptance")
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    if task.state is not TaskState.AWAITING_ACCEPTANCE:
        raise TransitionNotAllowedError(
            f"acceptance is recorded only in awaiting_acceptance; task is {task.state.value}"
        )
    head = task.head_sha or ""
    if request.head_sha and request.head_sha != head:
        raise TransitionNotAllowedError(
            f"the collected head is {head!r}; an AcceptanceResult names the head it is for"
        )
    now = clock.now()
    uow.acceptance.supersede_for_task(task.id, now)
    result = AcceptanceResult(
        id=new_id(),
        task_id=task.id,
        head_sha=head,
        principal_id=principal.id,
        verdict=request.verdict,
        reasoning=request.reasoning,
        created_at=now,
    )
    uow.acceptance.add(result)
    record_event(
        uow,
        clock,
        EventKind.ACCEPTANCE_RECORDED,
        principal=principal.name,
        task_id=task.id,
        payload={
            "acceptance_id": result.id,
            "head_sha": head,
            "verdict": request.verdict.value,
            "reasoning": request.reasoning,
        },
    )
    if request.verdict is AcceptanceVerdict.REJECTED:
        move_task(
            uow,
            clock,
            task,
            TaskState.REJECTED,
            EventKind.TASK_REJECTED,
            principal=principal.name,
            payload={"head_sha": head, "acceptance_id": result.id},
        )
        return task
    if request.verdict is AcceptanceVerdict.NEEDS_MORE_WORK:
        # The task waits here until a correction is attached (09); nothing moves yet.
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.NEEDS_MORE_WORK,
            summary=f"needs_more_work recorded on {head}; attach a correction to continue",
            task=task,
            extra_links={"corrections": f"/v1/tasks/{task.id}/corrections"},
            raised_by=principal.name,
        )
        return task
    kinds = deliverable_kinds(uow, task)
    if PUBLISHED_DELIVERABLES & set(kinds):
        # 09: a `branch` or `pull_request` deliverable goes through `publishing`, where
        # the supervisor mints a token, pushes the bundle head, and opens or updates the
        # PR. Nothing is accepted unpublished, and the API does not touch GitHub: the
        # state change is the request, the supervisor does the work (14).
        move_task(
            uow,
            clock,
            task,
            TaskState.PUBLISHING,
            EventKind.TASK_PUBLISHING,
            principal=principal.name,
            payload={
                "head_sha": head,
                "acceptance_id": result.id,
                "deliverables": kinds,
            },
        )
        return task
    move_task(
        uow,
        clock,
        task,
        TaskState.ACCEPTED,
        EventKind.TASK_ACCEPTED,
        principal=principal.name,
        payload={"head_sha": head, "acceptance_id": result.id, "deliverables": kinds},
    )
    return task


def close_task(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, task_id: str, note: str
) -> Task:
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    move_task(
        uow,
        clock,
        task,
        TaskState.CLOSED,
        EventKind.TASK_CLOSED,
        principal=principal.name,
        payload={"note": note},
    )
    # Close any open escalations with the close note as the decision.
    for escalation in uow.escalations.list_for_task(task.id):
        if escalation.state is not EscalationState.OPEN:
            continue
        # Create a decision to answer and close the escalation.
        decision_request = DecisionRequest(
            kind="task_closed",
            verbatim=note,
            resolves=escalation.question,
            escalation_id=escalation.id,
            reschedule=False,
        )
        record_decision(
            uow,
            clock,
            principal=principal,
            task_id=task.id,
            request=decision_request,
        )
    return task
