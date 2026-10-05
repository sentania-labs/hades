"""Proposed tasks and the operator's answers to them (hades #424).

The orchestrator writes a contract with `POST /v1/tasks?proposed=true`. It is validated
as any submission is and stored in `proposed`, which nothing can start. An operator then
approves it (with or without a note), sends it back, or rejects it, from the UI or the
API. Each answer is one audit event carrying the operator's reason. Approval goes through
`submitted` and then the same start the orchestrator makes, so what runs afterwards is
the ordinary lifecycle; a batch approval starts the tasks in the order they were selected
and records that order.
"""

from __future__ import annotations

from typing import Any

from crucible.application.corrections import _store_version
from crucible.application.errors import (
    ContractValidationError,
    ForbiddenError,
    NotFoundError,
    TransitionNotAllowedError,
)
from crucible.application.start_task import start_task
from crucible.application.transitions import move_task, require_contract
from crucible.application.wakes import create_wake
from crucible.contracts.api import StartRequest
from crucible.contracts.task_contract import TaskContractV1
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import Principal, Role, Task
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

# The words that introduce the operator's note where it is appended to the objective.
OPERATOR_DIRECTION = "Operator direction:"
OPERATOR_ROLES = frozenset({Role.OPERATOR, Role.ADMIN})


def _require_operator(principal: Principal) -> None:
    if principal.role not in OPERATOR_ROLES:
        raise ForbiddenError("only an operator answers a proposed task")


def _required(field: str, value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        raise ContractValidationError(
            f"{field} is required", errors=[{"path": field, "message": "must not be blank"}]
        )
    return text


def _proposed(uow: UnitOfWork, task_id: str) -> Task:
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    if task.state is not TaskState.PROPOSED:
        raise TransitionNotAllowedError(
            f"task {task.external_id} is {task.state.value}; only a proposed task is "
            "approved, sent back or rejected"
        )
    return task


def with_operator_direction(objective: str, note: str) -> str:
    """The objective with the note appended, the note exactly as the operator wrote it."""
    return f"{objective.rstrip()}\n\n{OPERATOR_DIRECTION} {note}"


def approve_task(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    reason: str,
    note: str | None = None,
    batch: dict[str, Any] | None = None,
) -> Task:
    """Approve a proposal: `proposed` to `submitted`, then started as the orchestrator's
    start would start it, under the policy its contract names. That policy must still be
    current (not retired); otherwise the proposal goes back for an amendment instead."""
    _require_operator(principal)
    reason = _required("reason", reason)
    task = _proposed(uow, task_id)
    stored = require_contract(uow, task)
    contract = TaskContractV1.model_validate(stored.document)
    policy = uow.policies.get(contract.policy.name, contract.policy.version)
    if policy is None or policy.retired_at is not None:
        raise ContractValidationError(
            "the proposal's policy is no longer current; send it back for an amendment",
            errors=[
                {
                    "path": "policy",
                    "message": f"{contract.policy.name}/{contract.policy.version} is "
                    + ("retired" if policy is not None else "not uploaded"),
                }
            ],
        )
    payload: dict[str, Any] = {"reason": reason, "note": None}
    if note is not None and note.strip():
        # Recorded verbatim: the objective gains the note as written, in a new contract
        # version, and the event carries the same text.
        directed = contract.model_copy(
            update={"objective": with_operator_direction(contract.objective, note)}
        )
        stored = _store_version(uow, clock, task, TaskContractV1.model_validate(directed))
        task.contract_version = stored.version
        payload["note"] = note
    payload["contract_version"] = task.contract_version
    payload["contract_sha256"] = stored.sha256
    if batch is not None:
        payload["batch"] = batch
    move_task(
        uow,
        clock,
        task,
        TaskState.SUBMITTED,
        EventKind.TASK_APPROVED,
        principal=principal.name,
        payload=payload,
    )
    return start_task(
        uow,
        clock,
        principal=principal,
        task_id=task.id,
        request=StartRequest(policy_version=contract.policy.version),
    )


def approve_batch(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_ids: list[str],
    reason: str,
) -> tuple[str, list[Task]]:
    """Approve several proposals in one action. The order of `task_ids` is the order the
    operator selected them in; it is recorded on each approval and is the order they are
    started, so the queue takes them in that order. All or none: one task that is not
    proposed refuses the whole batch."""
    _require_operator(principal)
    reason = _required("reason", reason)
    if not task_ids:
        raise ContractValidationError(
            "select at least one proposed task",
            errors=[{"path": "task_ids", "message": "must not be empty"}],
        )
    if len(set(task_ids)) != len(task_ids):
        raise ContractValidationError(
            "a task is selected twice",
            errors=[{"path": "task_ids", "message": "each task appears once"}],
        )
    tasks = [_proposed(uow, task_id) for task_id in task_ids]
    batch_id = new_id()
    order = [task.external_id for task in tasks]
    approved = [
        approve_task(
            uow,
            clock,
            principal=principal,
            task_id=task.id,
            reason=reason,
            batch={"id": batch_id, "position": position, "of": len(tasks), "order": order},
        )
        for position, task in enumerate(tasks, 1)
    ]
    return batch_id, approved


def send_back_task(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    reason: str,
    note: str,
) -> Task:
    """Return a proposal to the orchestrator: `sent_back`, and a wake whose summary is the
    operator's note. Amending the contract proposes it again."""
    _require_operator(principal)
    reason = _required("reason", reason)
    _required("note", note)
    task = _proposed(uow, task_id)
    move_task(
        uow,
        clock,
        task,
        TaskState.SENT_BACK,
        EventKind.TASK_SENT_BACK,
        principal=principal.name,
        payload={"reason": reason, "note": note},
    )
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=WakeReason.SENT_BACK,
        summary=note,
        task=task,
        raised_by=principal.name,
    )
    return task


def reject_proposal(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    reason: str,
) -> Task:
    """Refuse a proposal for good: `rejected`, and a wake telling the orchestrator why."""
    _require_operator(principal)
    reason = _required("reason", reason)
    task = _proposed(uow, task_id)
    move_task(
        uow,
        clock,
        task,
        TaskState.REJECTED,
        EventKind.TASK_PROPOSAL_REJECTED,
        principal=principal.name,
        payload={"reason": reason},
    )
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=WakeReason.PROPOSAL_REJECTED,
        summary=f"Proposal rejected: {reason}",
        task=task,
        raised_by=principal.name,
    )
    return task


__all__ = [
    "OPERATOR_DIRECTION",
    "approve_batch",
    "approve_task",
    "reject_proposal",
    "send_back_task",
    "with_operator_direction",
]
