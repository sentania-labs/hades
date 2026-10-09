"""Acceptance for the collected head (09, 11).

Hades records acceptance from passing blocking gates and names any advisory failure in
it. The acceptance API remains available for tasks left in awaiting_acceptance by older deployments.
"""

from __future__ import annotations

from crucible.application.errors import (
    ForbiddenError,
    NotFoundError,
    TransitionNotAllowedError,
)
from crucible.application.handoffs import HandoffAction, HandoffDirection, record_handoff
from crucible.application.task_access import require_task_principal
from crucible.application.transitions import move_task, record_event, require_contract
from crucible.application.wakes import create_wake
from crucible.contracts.api import AcceptRequest
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    AcceptanceResult,
    AcceptanceVerdict,
    Decision,
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


def gate_acceptance_reasoning(*, advisory_failing: list[str], self_review_checked: bool) -> str:
    """What the automatic acceptance actually rests on, stated from the gate outcomes.

    Advisory failures do not stop acceptance (ADR 0024), so the reasoning names them and
    claims the worker self-review only when `report_present` passed."""
    reasoning = "Every blocking pre-PR gate passed."
    if self_review_checked:
        reasoning += " The completion report includes the worker self-review."
    if advisory_failing:
        reasoning += (
            " Advisory gate failures were recorded as reviewer notes: "
            + ", ".join(sorted(set(advisory_failing)))
            + "."
        )
    return reasoning


def record_gate_acceptance(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    advisory_failing: list[str],
    self_review_checked: bool,
) -> AcceptanceResult:
    """Record Hades' acceptance once every blocking gate passes.

    The task owner remains the relational principal for the result; the audit event names
    Crucible as the actor. This is an automatic consequence of the verified gate result,
    not an orchestrator decision. The reasoning states which advisory gates failed and
    whether the self-review was present, so the audit record matches the gate outcomes.
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
        reasoning=gate_acceptance_reasoning(
            advisory_failing=advisory_failing, self_review_checked=self_review_checked
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
        raise ForbiddenError(
            "only an orchestrator, operator, or admin principal records acceptance"
        )
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
    # hades #208 item 2: the acceptance decision is Foundry's (or the operator's) hand
    # to Hades; the handoff carries who, the local time and the reasoning as written.
    record_handoff(
        uow,
        clock,
        task=task,
        action=HandoffAction.ACCEPT,
        direction=HandoffDirection.FOUNDRY_TO_HADES,
        principal=principal.name,
        words=request.reasoning,
        detail={"verdict": request.verdict.value, "head_sha": head, "acceptance_id": result.id},
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
    # We create Decision entities directly (not DecisionRequest) so the
    # public model validator's kind allowlist does not block internal closure.
    # (Finding 01M4CFEK8J8BXDEB0NETRX267E)
    now = clock.now()
    for escalation in uow.escalations.list_for_task(task.id):
        if escalation.state is not EscalationState.OPEN:
            continue
        decision = Decision(
            id=new_id(),
            task_id=task.id,
            escalation_id=escalation.id,
            principal_id=principal.id,
            kind="task_closed",
            verbatim=note,
            resolves=escalation.question,
            created_at=now,
        )
        uow.decisions.add(decision)
        record_event(
            uow,
            clock,
            EventKind.DECISION_RECORDED,
            principal=principal.name,
            task_id=task.id,
            payload={
                "decision_id": decision.id,
                "kind": decision.kind,
                "escalation_id": decision.escalation_id,
                "resolves": decision.resolves,
                "verbatim": decision.verbatim,
            },
        )
        # Transition: OPEN -> ANSWERED -> CLOSED (matching record_decision).
        for target_state, event_kind in (
            (EscalationState.ANSWERED, EventKind.ESCALATION_ANSWERED),
            (EscalationState.CLOSED, EventKind.ESCALATION_CLOSED),
        ):
            escalation.state = target_state
            if target_state is EscalationState.CLOSED:
                escalation.closed_at = now
            escalation.decision_id = decision.id
            uow.escalations.save(escalation)
            record_event(
                uow,
                clock,
                event_kind,
                principal=principal.name,
                task_id=task.id,
                payload={"escalation_id": escalation.id, "decision_id": decision.id},
            )
    return task
