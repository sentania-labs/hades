"""/memory and /decisions (hades #208): the shared memory store every Hades channel
reads, and the append-only decision ledger.

Transcripts stay per channel. Decisions and memory are shared by every channel and every
persona. Minion findings become memory only when Hades or the operator promotes them.
Reads need any principal; writes need the orchestrator or operator role. The ledger has
no edit and no delete route, and its table refuses both."""

from __future__ import annotations

from typing import Annotated

from fastapi import Query

from crucible.adapters.api.deps import Ctx, Orchestrator, Reader, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.memory import (
    LEDGER_MAX_LIMIT,
    forget_memory,
    ledger_decision_view,
    list_ledger_decisions,
    memory_item_view,
    promote_memory,
    recall_memory,
    record_ledger_decision,
    supersede_memory,
)
from crucible.contracts.api import (
    LedgerDecisionList,
    LedgerDecisionRequest,
    LedgerDecisionView,
    MemoryItemView,
    MemoryPromoteRequest,
    MemoryRecall,
    MemorySupersedeRequest,
)
from crucible.domain.memory import RECALL_MAX_LIMIT

router = ThreadedAPIRouter()


def _split_tags(tags: list[str] | None) -> list[str]:
    """`?tags=a,b` and `?tags=a&tags=b` both name the tags a and b."""
    names: list[str] = []
    for value in tags or []:
        names.extend(part for part in value.split(",") if part.strip())
    return names


@router.get("/memory", response_model=MemoryRecall)
def recall(
    uow: UoW,
    _principal: Reader,
    subject: Annotated[str | None, Query(max_length=1024)] = None,
    tags: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=RECALL_MAX_LIMIT)] = None,
) -> MemoryRecall:
    return recall_memory(uow, subject=subject, tags=_split_tags(tags), limit=limit)


@router.post("/memory", response_model=MemoryItemView, status_code=201)
def promote(
    body: MemoryPromoteRequest, ctx: Ctx, uow: UoW, principal: Orchestrator
) -> MemoryItemView:
    item = promote_memory(uow, ctx.clock, principal=principal, request=body)
    uow.commit()
    return memory_item_view(item)


@router.post("/memory/{item_id}/supersede", response_model=MemoryItemView, status_code=201)
def supersede(
    item_id: str,
    body: MemorySupersedeRequest,
    ctx: Ctx,
    uow: UoW,
    principal: Orchestrator,
) -> MemoryItemView:
    item = supersede_memory(uow, ctx.clock, principal=principal, item_id=item_id, request=body)
    uow.commit()
    return memory_item_view(item)


@router.post("/memory/{item_id}/forget", response_model=MemoryItemView)
def forget(item_id: str, ctx: Ctx, uow: UoW, principal: Orchestrator) -> MemoryItemView:
    item = forget_memory(uow, ctx.clock, principal=principal, item_id=item_id)
    uow.commit()
    return memory_item_view(item)


@router.get("/decisions", response_model=LedgerDecisionList)
def decisions(
    uow: UoW,
    _principal: Reader,
    channel: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int | None, Query(ge=1, le=LEDGER_MAX_LIMIT)] = None,
) -> LedgerDecisionList:
    lines = list_ledger_decisions(uow, limit=limit, channel=channel)
    return LedgerDecisionList(items=[ledger_decision_view(line) for line in lines])


@router.post("/decisions", response_model=LedgerDecisionView, status_code=201)
def append_decision(
    body: LedgerDecisionRequest, ctx: Ctx, uow: UoW, principal: Orchestrator
) -> LedgerDecisionView:
    line = record_ledger_decision(uow, ctx.clock, principal=principal, request=body)
    uow.commit()
    return ledger_decision_view(line)
