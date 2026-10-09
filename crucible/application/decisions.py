"""Decisions, escalations, and review dispositions (03, 04, 09).

An escalation is opened when a worker blocks; a Decision that references it closes it and
lets the task be scheduled again. A disposition is Foundry's reading of one external
review comment; Crucible records it, the `feedback_dispositions_complete` gate counts
them, and nothing is ever forwarded to a worker (ADR 0008)."""

from __future__ import annotations

from datetime import timedelta

from crucible.application.errors import ForbiddenError, NotFoundError, TransitionNotAllowedError
from crucible.application.memory import mirror_task_decision
from crucible.application.task_access import require_task_principal
from crucible.application.transitions import move_task, record_event, require_contract
from crucible.application.wakes import create_wake, repeat_allowed
from crucible.contracts.api import DecisionRequest, DispositionRequest
from crucible.contracts.wake import WakeReason
from crucible.domain.decisions import OPERATOR_ONLY_DECISION_KINDS
from crucible.domain.entities import (
    Decision,
    Escalation,
    EscalationState,
    Principal,
    ReviewDisposition,
    Role,
    Task,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState, check_transition
from crucible.domain.waivers import WAIVABLE_STATES, WAIVER_KINDS
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

DEFAULT_ESCALATION_STALE_HOURS = 24


def open_escalation(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    attempt_id: str | None,
    question: str,
    wake_reason: WakeReason = WakeReason.BLOCKED,
    summary: str | None = None,
    reason: str | None = None,
) -> Escalation:
    """09: entering `blocked` opens an escalation and creates a wake. The supervisor
    opens one the same way for a publication that cannot start (23), with its own wake
    reason and summary.

    hades #393: `reason` is the one the worker's `blocked.md` named (missing_capability
    or ambiguous_contract) and `question` is its statement verbatim; both go on the
    escalation and its event, and the wake summary names the reason. A caller asking
    the operator to decide sets reason="decision" (or "design" for a design question);
    absent a reason, the escalation belongs to Foundry."""
    now = clock.now()
    escalation = Escalation(
        id=new_id(),
        task_id=task.id,
        attempt_id=attempt_id,
        state=EscalationState.OPEN,
        question=question,
        opened_at=now,
        last_wake_at=now,
        reason=reason,
    )
    uow.escalations.add(escalation)
    record_event(
        uow,
        clock,
        EventKind.ESCALATION_OPENED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        attempt_id=attempt_id,
        payload={
            "escalation_id": escalation.id,
            "question": question,
            **({"reason": reason} if reason else {}),
        },
    )
    if summary is None:
        summary = (
            f"the worker stopped ({reason}) and opened escalation {escalation.id}"
            if reason
            else f"the worker blocked and opened escalation {escalation.id}"
        )
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=wake_reason,
        summary=summary,
        task=task,
        attempt_id=attempt_id,
        extra_links={"decisions": f"/v1/tasks/{task.id}/decisions"},
    )
    return escalation


def repeat_stale_escalation_wakes(uow: UnitOfWork, clock: Clock, *, stale_hours: int) -> int:
    """An escalation older than `escalation_stale_hours` produces a repeat wake (09).

    hades #502: exactly one open `escalation_stale` wake per task. While it is unacked
    no copy is raised, however many stale escalations the task has or how long they
    stay open; once it is acked the notice comes back only when an escalation is still
    open a full `stale_hours` after the ack. One task's stale escalations share one
    wake, whose summary names them all. Returns how many wakes were raised."""
    now = clock.now()
    interval = timedelta(hours=stale_hours)
    tasks: dict[str, Task] = {}
    stale: dict[str, list[Escalation]] = {}
    for escalation in uow.escalations.list_open():
        last = escalation.last_wake_at or escalation.opened_at
        if now - last < interval:
            continue
        task = tasks.get(escalation.task_id)
        if task is None:
            task = uow.tasks.get(escalation.task_id)
        if task is None:
            continue
        # Skip escalations on tasks that are in a terminal state.
        if task.state in (TaskState.CANCELLED, TaskState.REJECTED, TaskState.CLOSED):
            continue
        tasks[task.id] = task
        stale.setdefault(task.id, []).append(escalation)
    repeated = 0
    for task_id, escalations in stale.items():
        task = tasks[task_id]
        previous = uow.wakes.list_for_task(task.id, reason=WakeReason.ESCALATION_STALE.value)
        if not repeat_allowed(previous, now=now, interval=interval):
            continue
        escalations.sort(key=lambda e: e.opened_at)
        if len(escalations) == 1:
            summary = (
                f"escalation {escalations[0].id} has been open since "
                f"{escalations[0].opened_at.isoformat()} with no decision"
            )
        else:
            names = ", ".join(e.id for e in escalations)
            summary = (
                f"escalations {names} have been open since "
                f"{escalations[0].opened_at.isoformat()} with no decision"
            )
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.ESCALATION_STALE,
            summary=summary,
            task=task,
            attempt_id=escalations[-1].attempt_id,
        )
        for escalation in escalations:
            escalation.last_wake_at = now
            uow.escalations.save(escalation)
        repeated += 1
    return repeated


def record_decision(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, task_id: str, request: DecisionRequest
) -> Task:
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    if request.kind in OPERATOR_ONLY_DECISION_KINDS and principal.role not in (
        Role.OPERATOR,
        Role.ADMIN,
    ):
        raise ForbiddenError(f"decision kind {request.kind!r} is operator-only (04)")
    escalation = None
    if request.escalation_id is not None:
        escalation = uow.escalations.get(request.escalation_id, for_update=True)
        if escalation is None or escalation.task_id != task.id:
            raise NotFoundError(f"escalation {request.escalation_id} not found on this task")
        # Validated before anything is written: 11 level 4 says the verbatim words are the
        # record, and a refusal after the insert would roll them back with the rest.
        check_transition("escalation", escalation.id, escalation.state, EscalationState.ANSWERED)
    if request.kind in WAIVER_KINDS and task.state not in WAIVABLE_STATES:
        # A waiver is about a pull request Crucible is watching; anywhere else it would be
        # a record that waives nothing, or one that surprises a later state.
        raise TransitionNotAllowedError(
            f"a {request.kind} decision is recorded while a pull request is under "
            f"observation; task is {task.state.value}"
        )
    if request.reschedule and task.state is not TaskState.BLOCKED:
        raise TransitionNotAllowedError(
            f"reschedule applies to a blocked task; task is {task.state.value}"
        )
    decision = Decision(
        id=new_id(),
        task_id=task.id,
        escalation_id=escalation.id if escalation else None,
        principal_id=principal.id,
        kind=request.kind,
        verbatim=request.verbatim,
        resolves=request.resolves,
        created_at=clock.now(),
    )
    uow.decisions.add(decision)
    # hades #208: from here on a task decision is also a line of the shared ledger,
    # channel `task`, with the task id in applies_to. History is not migrated.
    mirrored = mirror_task_decision(uow, clock, principal=principal, task=task, decision=decision)
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
            "ledger_decision_id": mirrored.id,
        },
    )
    if escalation is not None:
        for target, kind in (
            (EscalationState.ANSWERED, EventKind.ESCALATION_ANSWERED),
            (EscalationState.CLOSED, EventKind.ESCALATION_CLOSED),
        ):
            escalation.state = target
            if target is EscalationState.CLOSED:
                escalation.closed_at = clock.now()
            escalation.decision_id = decision.id
            uow.escalations.save(escalation)
            record_event(
                uow,
                clock,
                kind,
                principal=principal.name,
                task_id=task.id,
                payload={"escalation_id": escalation.id, "decision_id": decision.id},
            )
    if request.reschedule:
        stored = require_contract(uow, task)
        request_body = stored.document["execution_request"]
        move_task(
            uow,
            clock,
            task,
            TaskState.SCHEDULED,
            EventKind.TASK_SCHEDULED,
            principal=principal.name,
            payload={
                "decision_id": decision.id,
                "contract_version": task.contract_version,
                "harness": request_body["harness"],
                "model": request_body["model"],
                "provider": request_body["provider"],
                "image": request_body["image"],
                "policy": {"name": task.policy_name, "version": task.policy_version},
            },
        )
    return task


def record_disposition(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    request: DispositionRequest,
) -> ReviewDisposition:
    task = uow.tasks.get(task_id)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    if principal.role not in (Role.ORCHESTRATOR, Role.OPERATOR):
        raise ForbiddenError("only an orchestrator or operator principal records dispositions")
    comment = uow.review_comments.get(request.review_comment_id)
    if comment is None:
        raise NotFoundError(f"review comment {request.review_comment_id!r} is unknown")
    pull_request = uow.pull_requests.get_for_task(task.id)
    if pull_request is None or comment.pull_request_id != pull_request.id:
        raise NotFoundError(
            f"review comment {request.review_comment_id!r} is not on this task's pull request"
        )
    existing = uow.dispositions.get_by_comment(request.review_comment_id, comment.body_sha256)
    if existing is not None:
        raise TransitionNotAllowedError(
            f"review comment {request.review_comment_id} already has a disposition"
        )
    # A `fix` disposition is Foundry's finding, not an instruction to a worker: Crucible
    # never forwards feedback (ADR 0008). The correction contract that follows is
    # Foundry's own act through POST /tasks/{id}/corrections.
    return store_disposition(
        uow,
        clock,
        principal=principal,
        task_id=task_id,
        request=request,
        comment_body_sha256=comment.body_sha256,
    )


def store_disposition(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    request: DispositionRequest,
    comment_body_sha256: str,
) -> ReviewDisposition:
    """The row-writing half, kept separate so C4 can call it once comments exist."""
    disposition = ReviewDisposition(
        id=new_id(),
        review_comment_id=request.review_comment_id,
        comment_body_sha256=comment_body_sha256,
        principal_id=principal.id,
        disposition=request.disposition,
        reasoning=request.reasoning,
        created_at=clock.now(),
    )
    uow.dispositions.add(disposition)
    record_event(
        uow,
        clock,
        EventKind.DISPOSITION_RECORDED,
        principal=principal.name,
        task_id=task_id,
        payload={
            "disposition_id": disposition.id,
            "review_comment_id": disposition.review_comment_id,
            "disposition": disposition.disposition.value,
        },
    )
    return disposition
