"""POST /tasks/{id}/cancel (16): record the verbatim reason; cancelling while an attempt
runs (the supervisor terminates it), cancelled at once otherwise.

Internal closure decisions (task_cancelled) are written directly to the UoW
to avoid the public DecisionRequest validator (which enforces a two-character
minimum on verbatim).  Finding 01M4CFEK8DMF8V23ZB56ZC7S17.
"""

from __future__ import annotations

from crucible.application.errors import NotFoundError
from crucible.application.task_access import require_task_principal
from crucible.application.transitions import move_task, record_event
from crucible.contracts.api import CancelRequest
from crucible.domain.entities import Decision, EscalationState, Principal, Task
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork


def cancel_task(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, task_id: str, request: CancelRequest
) -> Task:
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    payload = {
        "reason": request.reason,
        "verbatim": request.verbatim,
        "decided_by": request.decided_by,
    }
    record_event(
        uow,
        clock,
        EventKind.TASK_CANCEL_REQUESTED,
        principal=principal.name,
        task_id=task.id,
        payload=payload,
    )
    if task.state is TaskState.RUNNING:
        move_task(
            uow,
            clock,
            task,
            TaskState.CANCELLING,
            EventKind.TASK_CANCELLING,
            principal=principal.name,
            payload=payload,
        )
    else:
        move_task(
            uow,
            clock,
            task,
            TaskState.CANCELLED,
            EventKind.TASK_CANCELLED,
            principal=principal.name,
            payload=payload,
        )
    # Close any open escalations with the cancel reason as the decision.
    # We create Decision entities directly (not DecisionRequest) so the
    # public model validator's min_length=2 on verbatim does not apply
    # to internal closure decisions.  (Finding 01M4CFEK8DMF8V23ZB56ZC7S17)
    now = clock.now()
    for escalation in uow.escalations.list_for_task(task.id):
        if escalation.state is not EscalationState.OPEN:
            continue
        decision = Decision(
            id=new_id(),
            task_id=task.id,
            escalation_id=escalation.id,
            principal_id=principal.id,
            kind="task_cancelled",
            verbatim=request.verbatim,
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
