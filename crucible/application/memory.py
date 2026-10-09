"""The shared memory store and the decision ledger (hades #208).

Transcripts stay per channel. Decisions and memory are shared by every channel and every
persona. Minion findings become memory only when Hades or the operator promotes them.
Memory is never edited in place: a change supersedes, a forget retires. The ledger is
append-only: a line is a principal's words, where and when they were said, and what was
done about them. Foundry's decisions on tasks (`POST /tasks/{id}/decisions`) are
mirrored into the ledger from this change on, with the channel `task` and the task id
in `applies_to`; history before it is not migrated."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from crucible.application.errors import ConflictError, ForbiddenError, NotFoundError
from crucible.application.transitions import record_event
from crucible.contracts.api import (
    LedgerDecisionRequest,
    LedgerDecisionView,
    MemoryItemView,
    MemoryPromoteRequest,
    MemoryRecall,
    MemorySupersedeRequest,
)
from crucible.domain.entities import Decision, LedgerDecision, MemoryItem, Principal, Task
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.domain.memory import (
    MEMORY_WRITER_ROLES,
    RECALL_DEFAULT_LIMIT,
    RECALL_MAX_LIMIT,
    normalize_tags,
    recall,
    subject_keywords,
)
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

TASK_CHANNEL = "task"
LEDGER_DEFAULT_LIMIT = 100
LEDGER_MAX_LIMIT = 500


def require_memory_writer(principal: Principal) -> None:
    """Hades (orchestrator) or the operator writes; the admin role is the operator's
    administrative role and is accepted the same way; an observer only reads."""
    if principal.role not in MEMORY_WRITER_ROLES:
        raise ForbiddenError("orchestrator or operator role required")


def bounded_limit(limit: int | None, *, default: int, maximum: int) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), maximum))


# ----- memory -------------------------------------------------------------------


def recall_memory(
    uow: UnitOfWork,
    *,
    subject: str | None,
    tags: Sequence[str],
    limit: int | None = None,
) -> MemoryRecall:
    """The current items for a subject and tags, newest observed first, bounded. The
    repository applies the rule in SQL; the domain's `recall` is run over what it
    returns, so the two can never disagree about what comes back."""
    bounded = bounded_limit(limit, default=RECALL_DEFAULT_LIMIT, maximum=RECALL_MAX_LIMIT)
    wanted = normalize_tags(tags)
    keywords = subject_keywords(subject)
    rows = uow.memory.recall(tags=wanted, keywords=keywords, limit=bounded)
    items = recall(rows, tags=wanted, subject=subject, limit=bounded)
    return MemoryRecall(
        subject=subject or None,
        tags=wanted,
        limit=bounded,
        items=[memory_item_view(item) for item in items],
    )


def list_memory(uow: UnitOfWork, *, limit: int | None = None) -> list[MemoryItem]:
    """The current items, newest observed first, for the Admin Memory page."""
    bounded = bounded_limit(limit, default=RECALL_MAX_LIMIT, maximum=RECALL_MAX_LIMIT * 5)
    rows = uow.memory.list_recent(limit=bounded, include_superseded=False)
    return sorted(rows, key=lambda item: (item.observed_at, item.id), reverse=True)


def promote_memory(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, request: MemoryPromoteRequest
) -> MemoryItem:
    """A finding becomes memory: Hades or the operator says so, and the item records
    who and when."""
    require_memory_writer(principal)
    now = clock.now()
    item = MemoryItem(
        id=new_id(),
        text=request.text,
        source=request.source,
        observed_at=request.observed_at or now,
        scope_tags=list(request.scope_tags),
        promoted_by=principal.name,
        promoted_at=now,
    )
    uow.memory.add(item)
    record_event(
        uow,
        clock,
        EventKind.MEMORY_PROMOTED,
        principal=principal.name,
        payload={
            "memory_id": item.id,
            "source": item.source,
            "scope_tags": list(item.scope_tags),
            "observed_at": item.observed_at.isoformat(),
            "reason": item.text,
        },
    )
    return item


def _current_item(uow: UnitOfWork, item_id: str) -> MemoryItem:
    item = uow.memory.get(item_id, for_update=True)
    if item is None:
        raise NotFoundError(f"memory item {item_id} not found")
    if not item.current:
        raise ConflictError(
            f"memory item {item_id} was already superseded"
            + (f" by {item.superseded_by}" if item.superseded_by else " and forgotten")
        )
    return item


def supersede_memory(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    item_id: str,
    request: MemorySupersedeRequest,
) -> MemoryItem:
    """Edit by superseding: the new item carries the correction, the old one points at
    it and stays as history. A field the request leaves out keeps the old value, the
    observation time included: an edit corrects the words, it does not make an old fact
    look newly observed."""
    require_memory_writer(principal)
    old = _current_item(uow, item_id)
    now = clock.now()
    new = MemoryItem(
        id=new_id(),
        text=request.text,
        source=request.source or old.source,
        observed_at=request.observed_at if request.observed_at is not None else old.observed_at,
        scope_tags=list(request.scope_tags if request.scope_tags is not None else old.scope_tags),
        promoted_by=principal.name,
        promoted_at=now,
    )
    uow.memory.add(new)
    uow.memory.retire(old.id, superseded_by=new.id, at=now)
    record_event(
        uow,
        clock,
        EventKind.MEMORY_SUPERSEDED,
        principal=principal.name,
        payload={
            "memory_id": old.id,
            "superseded_by": new.id,
            "source": new.source,
            "scope_tags": list(new.scope_tags),
            "reason": new.text,
        },
    )
    return new


def forget_memory(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, item_id: str
) -> MemoryItem:
    """Supersede with no replacement: the item stops being recalled and stays as the
    record of what was once remembered."""
    require_memory_writer(principal)
    old = _current_item(uow, item_id)
    now = clock.now()
    uow.memory.retire(old.id, superseded_by=None, at=now)
    record_event(
        uow,
        clock,
        EventKind.MEMORY_FORGOTTEN,
        principal=principal.name,
        payload={"memory_id": old.id, "source": old.source, "reason": old.text},
    )
    forgotten = uow.memory.get(old.id)
    return forgotten if forgotten is not None else old


def memory_item_view(item: MemoryItem) -> MemoryItemView:
    return MemoryItemView(
        id=item.id,
        text=item.text,
        source=item.source,
        observed_at=item.observed_at,
        scope_tags=list(item.scope_tags),
        promoted_by=item.promoted_by,
        promoted_at=item.promoted_at,
        superseded_by=item.superseded_by,
        superseded_at=item.superseded_at,
    )


# ----- the decision ledger ------------------------------------------------------


def record_ledger_decision(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, request: LedgerDecisionRequest
) -> LedgerDecision:
    """Append one line. `said_at` defaults to now; `acted_at` to now when `acted_by`
    is named without a time. Nothing here, or anywhere, edits or deletes a line."""
    require_memory_writer(principal)
    now = clock.now()
    acted_at: datetime | None = request.acted_at
    if request.acted_by is not None and acted_at is None:
        acted_at = now
    decision = LedgerDecision(
        id=new_id(),
        principal=request.principal,
        channel=request.channel,
        said_at=request.said_at or now,
        verbatim=request.verbatim,
        transcript_ref=request.transcript_ref,
        applies_to=list(request.applies_to),
        acted_by=request.acted_by,
        acted_at=acted_at,
    )
    uow.decision_ledger.add(decision)
    record_event(
        uow,
        clock,
        EventKind.LEDGER_DECISION_RECORDED,
        principal=principal.name,
        payload={
            "ledger_decision_id": decision.id,
            "decision_principal": decision.principal,
            "channel": decision.channel,
            "said_at": decision.said_at.isoformat(),
            "transcript_ref": decision.transcript_ref,
            "applies_to": list(decision.applies_to),
            "reason": decision.verbatim,
        },
    )
    return decision


def mirror_task_decision(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, task: Task, decision: Decision
) -> LedgerDecision:
    """A Foundry decision on a task, as the ledger sees it: channel `task`, the task id
    in `applies_to`, the deciding principal's words as said, and the recording as the
    act. The task page is where those words are shown, so it is the transcript ref."""
    mirrored = LedgerDecision(
        id=new_id(),
        principal=principal.name,
        channel=TASK_CHANNEL,
        said_at=decision.created_at,
        verbatim=decision.verbatim,
        transcript_ref=f"/ui/tasks/{task.id}",
        applies_to=[task.id],
        acted_by=principal.name,
        acted_at=clock.now(),
    )
    uow.decision_ledger.add(mirrored)
    return mirrored


def list_ledger_decisions(
    uow: UnitOfWork, *, limit: int | None = None, channel: str | None = None
) -> list[LedgerDecision]:
    """The newest lines first, bounded, optionally one channel's."""
    bounded = bounded_limit(limit, default=LEDGER_DEFAULT_LIMIT, maximum=LEDGER_MAX_LIMIT)
    rows = uow.decision_ledger.list_recent(limit=bounded, channel=channel or None)
    return sorted(rows, key=lambda line: (line.said_at, line.id), reverse=True)[:bounded]


def ledger_decision_view(decision: LedgerDecision) -> LedgerDecisionView:
    return LedgerDecisionView(
        id=decision.id,
        principal=decision.principal,
        channel=decision.channel,
        said_at=decision.said_at,
        verbatim=decision.verbatim,
        transcript_ref=decision.transcript_ref,
        applies_to=list(decision.applies_to),
        acted_by=decision.acted_by,
        acted_at=decision.acted_at,
    )


__all__ = [
    "LEDGER_DEFAULT_LIMIT",
    "LEDGER_MAX_LIMIT",
    "TASK_CHANNEL",
    "forget_memory",
    "ledger_decision_view",
    "list_ledger_decisions",
    "list_memory",
    "memory_item_view",
    "mirror_task_decision",
    "promote_memory",
    "recall_memory",
    "record_ledger_decision",
    "require_memory_writer",
    "supersede_memory",
]
