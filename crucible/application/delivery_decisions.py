"""Foundry's decisions on the delivery half (04, 09, 23).

`ci-decision` and `head-decision` are the two places where Crucible has recorded facts,
stopped, and needs judgment. Crucible records the judgment and performs its mechanical
consequence. It never re-runs a workflow (that needs Actions write, which the App does
not hold) or closes a pull request. Hades merges a certified head when policy enables it.
"""

from __future__ import annotations

from typing import Any

from crucible.application.errors import (
    ForbiddenError,
    NotFoundError,
    TransitionNotAllowedError,
)
from crucible.application.observation import supersede_for_head
from crucible.application.task_access import require_task_principal
from crucible.application.transitions import move_task, record_event
from crucible.application.wakes import create_wake
from crucible.contracts.api import CIDecisionRequest, HeadDecisionRequest
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    CIAction,
    CIDecision,
    HeadAction,
    Principal,
    Role,
    Task,
)
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork


def _orchestrator(principal: Principal, what: str) -> None:
    if principal.role not in (Role.ORCHESTRATOR, Role.OPERATOR):
        raise ForbiddenError(f"only an orchestrator or operator principal records {what}")


def record_ci_decision(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    request: CIDecisionRequest,
) -> Task:
    """23: the cause from the fixed enum and the action. No automatic retry ever."""
    _orchestrator(principal, "a CI decision")
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    if task.state is not TaskState.CI_CERTIFICATION_FAILED:
        raise TransitionNotAllowedError(
            f"a CI decision is recorded in ci_certification_failed; task is {task.state.value}"
        )
    certifications = [
        c for c in uow.ci_certifications.list_for_task(task.id) if c.state == "failed"
    ]
    certification = certifications[-1] if certifications else None
    decision = CIDecision(
        id=new_id(),
        task_id=task.id,
        ci_certification_id=certification.id if certification else None,
        principal_id=principal.id,
        cause=request.cause.value,
        action=request.action.value,
        reasoning=request.reasoning,
        created_at=clock.now(),
    )
    uow.ci_decisions.add(decision)
    record_event(
        uow,
        clock,
        EventKind.CI_DECISION_RECORDED,
        principal=principal.name,
        task_id=task.id,
        payload={
            "ci_decision_id": decision.id,
            "certification_id": decision.ci_certification_id,
            "cause": decision.cause,
            "action": decision.action,
            "reasoning": decision.reasoning,
            "head_sha": task.head_sha,
            # hades FDY-0139: the failed runs this decision is about. After a rerun they
            # are not counted again; any other failure is a new one.
            "stale_failures": [
                {"run_id": row.get("run_id"), "completed_at": row.get("completed_at")}
                for row in (certification.failure.get("all") or [] if certification else [])
                # A row recorded before FDY-0139 carries no run id; listing it would match
                # nothing, so it is left out and the decision falls back to its time.
                if isinstance(row, dict) and row.get("run_id")
            ],
        },
    )
    if request.action is CIAction.RERUN:
        # Re-running a workflow needs Actions write, which ADR 0007 does not grant. The
        # intent is recorded and the operator performs it on GitHub (23, 22).
        move_task(
            uow,
            clock,
            task,
            TaskState.AWAITING_CI_CERTIFICATION,
            EventKind.TASK_AWAITING_CI_CERTIFICATION,
            principal=principal.name,
            payload={
                "ci_decision_id": decision.id,
                "head_sha": task.head_sha,
                "note": (
                    "rerun recorded; the operator re-runs it on GitHub, because the App "
                    "holds no Actions write (23)"
                ),
            },
        )
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.CI_RERUN_NEEDED,
            summary=(
                f"a CI re-run was decided for {task.head_sha}; re-run it on GitHub, "
                "Crucible cannot (no Actions write)"
            ),
            task=task,
            raised_by=principal.name,
        )
        return task
    if request.action is CIAction.REJECT:
        move_task(
            uow,
            clock,
            task,
            TaskState.REJECTED,
            EventKind.TASK_REJECTED,
            principal=principal.name,
            payload={"ci_decision_id": decision.id, "cause": decision.cause},
        )
        return task
    if request.action is CIAction.CANCEL:
        move_task(
            uow,
            clock,
            task,
            TaskState.CANCELLED,
            EventKind.TASK_CANCELLED,
            principal=principal.name,
            payload={"ci_decision_id": decision.id, "cause": decision.cause},
        )
        return task
    # `correct`: the task waits here until a correction contract is attached (09).
    return task


def record_head_decision(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    request: HeadDecisionRequest,
) -> Task:
    """09: adopt/recollect, reject, or cancel a head Crucible did not push."""
    _orchestrator(principal, "a head decision")
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    if task.state is not TaskState.HEAD_DIVERGED:
        raise TransitionNotAllowedError(
            f"a head decision is recorded in head_diverged; task is {task.state.value}"
        )
    pull_request = uow.pull_requests.get_for_task(task.id)
    payload: dict[str, Any] = {
        "action": request.action.value,
        "reasoning": request.reasoning,
        "accepted_head": task.head_sha,
        "observed_head": pull_request.head_sha if pull_request else None,
        "pull_request": pull_request.number if pull_request else None,
    }
    record_event(
        uow,
        clock,
        EventKind.HEAD_DECISION_RECORDED,
        principal=principal.name,
        task_id=task.id,
        payload=payload,
    )
    if request.action is HeadAction.REJECT:
        move_task(
            uow,
            clock,
            task,
            TaskState.REJECTED,
            EventKind.TASK_REJECTED,
            principal=principal.name,
            payload=payload,
        )
        return task
    if request.action is HeadAction.CANCEL:
        move_task(
            uow,
            clock,
            task,
            TaskState.CANCELLED,
            EventKind.TASK_CANCELLED,
            principal=principal.name,
            payload=payload,
        )
        return task
    # `adopt` (formerly named `recollect`): everything about the old head is superseded
    # and the task re-enters
    # supervision against the remote work branch, which is where the new head is. The
    # new head then goes through the whole pre-PR path, internal review as the policy
    # and Foundry decide, acceptance, and `publishing`, which finds the remote already
    # matching. See docs/implementation-notes/c4.md for why this re-enters at
    # `scheduled` rather than at `reported`.
    supersede_for_head(
        uow,
        clock,
        task=task,
        reason=request.action.value,
        new_head=pull_request.head_sha if pull_request else "",
        principal=principal.name,
    )
    stored = uow.contracts.get(task.id, task.contract_version)
    assert stored is not None
    execution_request = stored.document["execution_request"]
    task.head_sha = None
    uow.tasks.save(task)
    move_task(
        uow,
        clock,
        task,
        TaskState.SCHEDULED,
        EventKind.TASK_SCHEDULED,
        principal=principal.name,
        payload={
            "role": "correct",
            "reason": request.action.value,
            "contract_version": task.contract_version,
            "tier": execution_request["tier"],
            "provider": execution_request["provider"],
            "policy": {"name": task.policy_name, "version": task.policy_version},
            "resume_from_work_branch": True,
        },
    )
    return task
