"""POST /tasks/{id}/start: Foundry's dispatch decision. Moves the task to scheduled and
records the request; the supervisor materializes the execution and first attempt on
its next tick, because execution and attempt rows are fenced to the supervisor (14)."""

from __future__ import annotations

from crucible.application.errors import (
    ContractValidationError,
    NotFoundError,
    TransitionNotAllowedError,
)
from crucible.application.transitions import move_task, require_contract
from crucible.contracts.api import StartRequest
from crucible.contracts.task_contract import TaskContractV1
from crucible.domain.entities import Principal, Task
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork


def start_task(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, task_id: str, request: StartRequest
) -> Task:
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    if task.state in (TaskState.PROPOSED, TaskState.SENT_BACK):
        # hades #424: nobody has authorized a proposal; an operator's approval starts it.
        raise TransitionNotAllowedError(
            f"task {task.id} is {task.state.value}; a proposed task starts only when an "
            "operator approves it"
        )
    stored = require_contract(uow, task)
    contract = TaskContractV1.model_validate(stored.document)
    problems = []
    req = contract.execution_request
    if request.overrides is not None and any(
        v is not None for v in request.overrides.model_dump().values()
    ):
        problems.append(
            {"path": "overrides", "message": "overrides create an amendment, which is C2"}
        )
    expected_harness = req.pinned_harness.value if req.pinned_harness else None
    for field, expected, given in (
        ("harness", expected_harness, request.harness.value if request.harness else None),
        ("model", req.pinned_model, request.model),
        ("provider", req.provider.value, request.provider.value if request.provider else None),
        ("image", req.image, request.image),
    ):
        if given is not None and expected != given:
            problems.append(
                {
                    "path": field,
                    "message": f"contract says {expected!r}; the contract is authoritative",
                }
            )
    if request.policy_version != contract.policy.version:
        problems.append(
            {
                "path": "policy_version",
                "message": f"contract names version {contract.policy.version}",
            }
        )
    if problems:
        raise ContractValidationError("start request disagrees with the contract", errors=problems)
    move_task(
        uow,
        clock,
        task,
        TaskState.SCHEDULED,
        EventKind.TASK_SCHEDULED,
        principal=principal.name,
        payload={
            "tier": req.tier.value,
            "pin": (
                {
                    "harness": expected_harness,
                    "model": req.pinned_model,
                    "reason": req.effective_pin_reason,
                }
                if req.pinned_model
                else None
            ),
            "effort": request.effort or req.effort,
            "provider": req.provider.value,
            "policy": {"name": contract.policy.name, "version": contract.policy.version},
            "contract_version": task.contract_version,
        },
    )
    return task
