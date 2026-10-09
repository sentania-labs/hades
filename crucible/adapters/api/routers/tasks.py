"""/tasks (04): submit, list, get, start, cancel, events, notes, questions."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import Header, Query, Request
from fastapi.responses import JSONResponse

from crucible.adapters.api.deps import Ctx, Mutator, Operator, Orchestrator, Reader, UoW
from crucible.adapters.api.idempotency import with_idempotency
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.acceptance import close_task, record_acceptance
from crucible.application.cancel_task import cancel_task
from crucible.application.corrections import amend_task, attach_correction
from crucible.application.decisions import record_decision, record_disposition
from crucible.application.delivery_decisions import record_ci_decision, record_head_decision
from crucible.application.minion_questions import answer_question
from crucible.application.proposals import (
    approve_batch,
    approve_task,
    reject_proposal,
    send_back_task,
)
from crucible.application.queries import (
    pull_request_view,
    task_events,
    task_list,
    task_view,
)
from crucible.application.republish import republish_task
from crucible.application.review import request_review
from crucible.application.start_task import start_task
from crucible.application.submit_task import submit_task
from crucible.application.task_notes import add_note
from crucible.contracts.api import (
    AcceptRequest,
    AmendRequest,
    AnswerRequest,
    ApproveRequest,
    BatchApprovalView,
    BatchApproveRequest,
    CancelRequest,
    CIDecisionRequest,
    CloseRequest,
    DecisionRequest,
    DispositionRequest,
    EventList,
    HeadDecisionRequest,
    NoteRequest,
    PublishRetryRequest,
    PullRequestView,
    RejectProposalRequest,
    ReviewRequest,
    SendBackRequest,
    StartRequest,
    TaskList,
    TaskView,
)
from crucible.domain.lifecycle import TaskState
from crucible.ports.repository import UnitOfWork

router = ThreadedAPIRouter(prefix="/tasks")
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


@router.post("", response_model=TaskView, status_code=201)
async def submit(
    request: Request,
    ctx: Ctx,
    principal: Mutator,
    idempotency_key: IdemKey = None,
    proposed: bool = False,
) -> JSONResponse:
    """`?proposed=true` (hades #424) stores the contract as a proposal, which only an
    operator's approval starts."""
    body = await request.body()
    document: Any = await request.json() if body else {}

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task, _ = submit_task(
            uow,
            ctx.clock,
            principal=principal,
            body=document,
            harnesses=ctx.harnesses,
            harness_gates=ctx.harness_gates,
            credential_sources=ctx.credential_sources,
            secret_providers=ctx.secret_providers,
            wired_providers=frozenset(provider.name for provider in ctx.providers),
            proposed=proposed,
        )
        return 201, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=body,
        # Proposal mode changes the authorization semantics of an otherwise identical
        # contract, so it is part of the idempotency identity.
        scope=f"{request.url.path}?proposed={str(proposed).lower()}",
        produce=produce,
    )


@router.post("/approvals", response_model=BatchApprovalView)
async def approve_several(
    body: BatchApproveRequest,
    request: Request,
    ctx: Ctx,
    principal: Operator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    """hades #424: approve proposed tasks in one action; `task_ids` order is queue order."""
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        batch_id, tasks = approve_batch(
            uow, ctx.clock, principal=principal, task_ids=body.task_ids, reason=body.reason
        )
        view = BatchApprovalView(
            batch_id=batch_id, tasks=[task_view(uow, task.id) for task in tasks]
        )
        return 200, view.model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.get("", response_model=TaskList)
def list_tasks(
    uow: UoW,
    _principal: Reader,
    state: TaskState | None = None,
    project: str | None = None,
    repository: str | None = None,
    external_id: str | None = None,
    updated_since: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> TaskList:
    return task_list(
        uow,
        state=state,
        project=project,
        repository=repository,
        external_id=external_id,
        updated_since=updated_since,
        cursor=cursor,
        limit=limit,
    )


@router.get("/{task_id}", response_model=TaskView)
def get_task(task_id: str, uow: UoW, _principal: Reader) -> TaskView:
    return task_view(uow, task_id)


@router.post("/{task_id}/start", response_model=TaskView)
async def start(
    task_id: str,
    body: StartRequest,
    request: Request,
    ctx: Ctx,
    principal: Mutator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = start_task(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/approve", response_model=TaskView)
async def approve(
    task_id: str,
    body: ApproveRequest,
    request: Request,
    ctx: Ctx,
    principal: Operator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = approve_task(
            uow,
            ctx.clock,
            principal=principal,
            task_id=task_id,
            reason=body.reason,
            note=body.note,
        )
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/send-back", response_model=TaskView)
async def send_back(
    task_id: str,
    body: SendBackRequest,
    request: Request,
    ctx: Ctx,
    principal: Operator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = send_back_task(
            uow,
            ctx.clock,
            principal=principal,
            task_id=task_id,
            reason=body.reason,
            note=body.note,
        )
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/reject", response_model=TaskView)
async def reject(
    task_id: str,
    body: RejectProposalRequest,
    request: Request,
    ctx: Ctx,
    principal: Operator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    """Rejects a proposed task only; a task in delivery is rejected through its decisions."""
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = reject_proposal(
            uow, ctx.clock, principal=principal, task_id=task_id, reason=body.reason
        )
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/cancel", response_model=TaskView)
async def cancel(
    task_id: str,
    body: CancelRequest,
    request: Request,
    ctx: Ctx,
    principal: Mutator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = cancel_task(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.get("/{task_id}/events", response_model=EventList)
def events(
    task_id: str,
    uow: UoW,
    _principal: Reader,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> EventList:
    return task_events(uow, task_id, cursor=cursor, limit=limit)


@router.post("/{task_id}/review", response_model=TaskView)
async def review(
    task_id: str,
    body: ReviewRequest,
    request: Request,
    ctx: Ctx,
    principal: Orchestrator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = request_review(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/accept", response_model=TaskView)
async def accept(
    task_id: str,
    body: AcceptRequest,
    request: Request,
    ctx: Ctx,
    principal: Orchestrator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = record_acceptance(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/republish", response_model=TaskView)
async def republish(
    task_id: str,
    body: PublishRetryRequest,
    request: Request,
    ctx: Ctx,
    principal: Orchestrator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = republish_task(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/corrections", response_model=TaskView)
async def corrections(
    task_id: str, request: Request, ctx: Ctx, principal: Mutator, idempotency_key: IdemKey = None
) -> JSONResponse:
    raw = await request.body()
    document: Any = await request.json() if raw else {}

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = attach_correction(
            uow,
            ctx.clock,
            principal=principal,
            task_id=task_id,
            body=document,
            harnesses=ctx.harnesses,
            harness_gates=ctx.harness_gates,
            credential_sources=ctx.credential_sources,
            secret_providers=ctx.secret_providers,
            wired_providers=frozenset(provider.name for provider in ctx.providers),
        )
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/amend", response_model=TaskView)
async def amend(
    task_id: str,
    body: AmendRequest,
    request: Request,
    ctx: Ctx,
    principal: Mutator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = amend_task(
            uow,
            ctx.clock,
            principal=principal,
            task_id=task_id,
            body=body.contract,
            reason=body.reason,
            harnesses=ctx.harnesses,
            harness_gates=ctx.harness_gates,
            credential_sources=ctx.credential_sources,
            secret_providers=ctx.secret_providers,
            wired_providers=frozenset(provider.name for provider in ctx.providers),
        )
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/decisions", response_model=TaskView)
async def decisions(
    task_id: str,
    body: DecisionRequest,
    request: Request,
    ctx: Ctx,
    principal: Mutator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = record_decision(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
        return 201, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/dispositions", response_model=TaskView)
async def dispositions(
    task_id: str, body: DispositionRequest, ctx: Ctx, uow: UoW, principal: Orchestrator
) -> TaskView:
    record_disposition(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
    uow.commit()
    return task_view(uow, task_id)


@router.post("/{task_id}/close", response_model=TaskView)
async def close(
    task_id: str,
    body: CloseRequest,
    request: Request,
    ctx: Ctx,
    principal: Orchestrator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = close_task(uow, ctx.clock, principal=principal, task_id=task_id, note=body.note)
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/notes", response_model=TaskView, status_code=201)
async def notes(
    task_id: str,
    body: NoteRequest,
    request: Request,
    ctx: Ctx,
    principal: Operator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    """hades #489: an operator's note on the task, listed on the task read newest first
    and put at the top of the next attempt's IDENTITY.md."""
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        add_note(
            uow,
            ctx.clock,
            principal=principal,
            task_id=task_id,
            text=body.text,
            verbatim=body.verbatim,
        )
        return 201, task_view(uow, task_id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/questions/{question_id}/answer", response_model=TaskView)
async def answer(
    task_id: str,
    question_id: str,
    body: AnswerRequest,
    request: Request,
    ctx: Ctx,
    principal: Mutator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    """hades #208 item 2: answer a worker's question in one call. The answer is recorded
    on the question (who, when, the words) and brought back to the worker as a correction
    version with the answer as its instructions, resumed from `resume_from`; a task that
    cannot take a correction keeps the answer and closes its escalation with an
    `escalation_answer` decision. Operator, admin or orchestrator; the task view returned
    lists the question under `questions` with `answer_action` saying which happened."""
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task, _question = answer_question(
            uow,
            ctx.clock,
            principal=principal,
            task_id=task_id,
            question_id=question_id,
            answer_text=body.answer_text,
            resume_from=body.resume_from,
            harnesses=ctx.harnesses,
            harness_gates=ctx.harness_gates,
            credential_sources=ctx.credential_sources,
            secret_providers=ctx.secret_providers,
            wired_providers=frozenset(provider.name for provider in ctx.providers),
        )
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.get("/{task_id}/pull-request", response_model=PullRequestView)
def get_pull_request(task_id: str, uow: UoW, _principal: Reader) -> PullRequestView:
    """The PR record with head history, external reviews, dispositions, reactions, and
    CI certifications (04)."""
    return pull_request_view(uow, task_id)


@router.post("/{task_id}/ci-decision", response_model=TaskView)
async def ci_decision(
    task_id: str,
    body: CIDecisionRequest,
    request: Request,
    ctx: Ctx,
    principal: Orchestrator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = record_ci_decision(
            uow,
            ctx.clock,
            principal=principal,
            task_id=task_id,
            request=body,
            github_client=ctx.github_client,
        )
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/head-decision", response_model=TaskView)
async def head_decision(
    task_id: str,
    body: HeadDecisionRequest,
    request: Request,
    ctx: Ctx,
    principal: Orchestrator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = record_head_decision(
            uow, ctx.clock, principal=principal, task_id=task_id, request=body
        )
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )
