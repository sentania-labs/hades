"""The facts behind a stuck card, read from the task, its open escalation and its latest
events, handed to `crucible.domain.stuck_reasons` (hades #607). The board reads the
events in one batch for every card; the card page and a Send back read one task's."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TASK_TERMINAL
from crucible.domain.stuck_reasons import (
    OPERATOR_QUESTION_KINDS,
    StuckFacts,
    StuckReason,
    named_task,
    stuck_reason,
)
from crucible.ports.repository import UnitOfWork

# The events a reason reads; the scheduling events date a harness refusal to the run.
STUCK_EVENT_KINDS: tuple[str, ...] = (
    EventKind.TASK_BLOCKED.value,
    EventKind.GATES_EVALUATED.value,
    EventKind.CI_CERTIFICATION_RECORDED.value,
    EventKind.HARNESS_REFUSED.value,
    EventKind.TASK_SCHEDULED.value,
    EventKind.TASK_RETRY_SCHEDULED.value,
)
_TASK_ID = re.compile(r"(?<![\w-])[A-Z][A-Z0-9]*-\d+(?![\w-])")


def is_operator_question(escalation: Any) -> bool:
    """An escalation addressed to the operator rather than to Foundry."""
    if escalation is None:
        return False
    kind = str(
        getattr(escalation, "kind", None) or getattr(escalation, "reason", None) or ""
    ).lower()
    return kind in OPERATOR_QUESTION_KINDS


def failing_jobs(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """The failing checks a `ci_certification_recorded` event names, in order."""
    failure = payload.get("failure") or {}
    if not isinstance(failure, Mapping):
        return ()
    names = [
        str(item.get("check") or "")
        for item in failure.get("all") or []
        if isinstance(item, Mapping)
    ] or [str(failure.get("check") or failure.get("job") or "")]
    legacy = payload.get("job") or payload.get("check") or payload.get("name")
    if legacy:
        names.append(str(legacy))
    return tuple(dict.fromkeys(name for name in names if name))


def _seq(event: Any) -> int:
    return int(getattr(event, "seq", 0) or 0) if event is not None else 0


def stuck_facts(
    task: Any,
    escalation: Any | None,
    events: Mapping[str, Any],
    *,
    other_tasks: Iterable[str] = (),
    ci_cause: str | None = None,
) -> StuckFacts:
    """`events` maps an event kind to the task's latest event of that kind;
    `other_tasks` are the external ids of live tasks this one may wait for."""
    blocked = events.get(EventKind.TASK_BLOCKED.value)
    gates = events.get(EventKind.GATES_EVALUATED.value)
    ci = events.get(EventKind.CI_CERTIFICATION_RECORDED.value)
    refused = events.get(EventKind.HARNESS_REFUSED.value)
    scheduled = max(
        _seq(events.get(EventKind.TASK_SCHEDULED.value)),
        _seq(events.get(EventKind.TASK_RETRY_SCHEDULED.value)),
    )
    question = str(escalation.question) if escalation is not None else None
    worker_reason = getattr(escalation, "reason", None) if escalation is not None else None
    waiting_for = (
        named_task(question, other_tasks, own=str(task.external_id or ""))
        if escalation is not None and not worker_reason
        else None
    )
    return StuckFacts(
        state=task.state,
        escalation_reason=worker_reason,
        question=question,
        for_operator=is_operator_question(escalation),
        blocked_reason=str((blocked.payload or {}).get("reason") or "") or None
        if blocked is not None
        else None,
        harness_refused=refused is not None and _seq(refused) > scheduled,
        failing_gates=tuple(str(g) for g in ((gates.payload or {}).get("failing") or []))
        if gates is not None
        else (),
        failing_jobs=failing_jobs(ci.payload or {}) if ci is not None else (),
        ci_cause=ci_cause,
        waiting_for=waiting_for,
    )


def task_stuck_reason(uow: UnitOfWork, task: Any, escalation: Any | None) -> StuckReason | None:
    """One task's reason, read with one event lookup per kind."""
    events = {
        kind: event
        for kind in STUCK_EVENT_KINDS
        if (event := uow.events.latest_for_task_kind(task.id, kind)) is not None
    }
    others: list[str] = []
    lookup = getattr(uow.tasks, "get_by_external_id", None)
    if escalation is not None and lookup is not None:
        for candidate in dict.fromkeys(_TASK_ID.findall(str(escalation.question))):
            other = lookup(task.principal_id, candidate)
            if other is not None and other.id != task.id and other.state not in TASK_TERMINAL:
                others.append(candidate)
    decisions = getattr(uow, "ci_decisions", None)
    cause = None
    if decisions is not None and hasattr(decisions, "list_for_task"):
        latest = max(decisions.list_for_task(task.id), key=lambda row: row.created_at, default=None)
        cause = str(latest.cause) if latest is not None else None
    return stuck_reason(stuck_facts(task, escalation, events, other_tasks=others, ci_cause=cause))


__all__ = [
    "STUCK_EVENT_KINDS",
    "failing_jobs",
    "is_operator_question",
    "stuck_facts",
    "task_stuck_reason",
]
