"""Wakes (17). The row is committed in the same transaction as the state change that
caused it; delivery is best effort afterwards and `GET /v1/wakes` is the durable
fallback Foundry polls on every start of session."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from crucible.application.errors import ConflictError, ForbiddenError, NotFoundError
from crucible.application.transitions import record_event
from crucible.contracts.common import to_document
from crucible.contracts.wake import WakeReason, WakeTask, WakeV1
from crucible.domain.entities import Principal, PullRequestState, Task, Wake
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.ids import new_id
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

# Exponential backoff in seconds between delivery attempts, then `wake_retry_hours`
# from policy stops the retries and leaves the row for poll (17).
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (30, 60, 120, 300, 600, 1800, 3600)
DEFAULT_WAKE_RETRY_HOURS = 24

# hades #502: the repeating notices about a pull request. One about a pull request that
# has since merged or closed is about nothing, and the system acks it.
PULL_REQUEST_OVERDUE_REASONS: tuple[WakeReason, ...] = (
    WakeReason.EXTERNAL_REVIEW_OVERDUE,
    WakeReason.CI_CERTIFICATION_OVERDUE,
)


def wake_document(wake: Wake, *, principal_name: str) -> dict[str, Any]:
    payload = dict(wake.payload)
    task = payload.pop("task", None)
    model = WakeV1(
        id=wake.id,
        principal=principal_name,
        reason=WakeReason(wake.reason),
        task=WakeTask.model_validate(task) if task else None,
        attempt_id=payload.pop("attempt_id", None),
        pull_request=payload.pop("pull_request", None),
        summary=str(payload.pop("summary", "")),
        for_reviewer=[
            {"gate": str(i.get("gate", "")), "detail": str(i.get("detail", ""))}
            for i in payload.pop("for_reviewer", []) or []
            if isinstance(i, dict)
        ],
        links={str(k): str(v) for k, v in (payload.pop("links", {}) or {}).items()},
        created_at=wake.created_at,
    )
    return to_document(model)


def create_wake(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal_id: str,
    reason: WakeReason,
    summary: str,
    task: Task | None = None,
    attempt_id: str | None = None,
    extra_links: dict[str, str] | None = None,
    raised_by: str = PRINCIPAL_CRUCIBLE,
    for_reviewer: list[dict[str, str]] | None = None,
) -> Wake:
    """Write the wake row. The caller commits it with the state change it belongs to.

    `raised_by` is the principal on the event: `crucible` for a supervisor wake, the
    acting principal for one an API call causes, because events whose principal is
    `crucible` are fenced to the supervisor (14)."""
    now = clock.now()
    links: dict[str, str] = {}
    payload: dict[str, Any] = {"summary": summary}
    if task is not None:
        links["task"] = f"/v1/tasks/{task.id}"
        links["events"] = f"/v1/tasks/{task.id}/events"
        payload["task"] = {
            "id": task.id,
            "external_id": task.external_id,
            "state": task.state.value,
        }
    if attempt_id is not None:
        links["attempt"] = f"/v1/attempts/{attempt_id}"
        payload["attempt_id"] = attempt_id
    links.update(extra_links or {})
    payload["links"] = links
    if for_reviewer:
        payload["for_reviewer"] = [dict(i) for i in for_reviewer]
    wake = Wake(
        id=new_id(),
        principal_id=principal_id,
        task_id=task.id if task is not None else None,
        reason=reason.value,
        payload=payload,
        created_at=now,
        next_attempt_at=now,
    )
    uow.wakes.add(wake)
    record_event(
        uow,
        clock,
        EventKind.WAKE_CREATED,
        principal=raised_by,
        task_id=wake.task_id,
        attempt_id=attempt_id,
        payload={"wake_id": wake.id, "reason": wake.reason, "summary": summary},
    )
    return wake


def pool_exhausted_summary(pool: str, reset_at: datetime, reason: str) -> str:
    """The one sentence a pool exhaustion tells Foundry (hades #378): which pool, why,
    and until when its models are out of routing. Shared by every wake that reports a
    pool mark, so the words are the same wherever the mark was made."""
    return (
        f"pool {pool} is exhausted ({reason}); its models are excluded from routing "
        f"until {reset_at.isoformat()}"
    )


def create_pool_exhausted_wake(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    attempt_id: str,
    pool: str,
    reset_at: datetime,
    reason: str,
) -> Wake:
    """One wake per exhaustion of a pool (hades #378): raised when a mark opens, not for
    every attempt the same mark later turns away. The caller decides the "once"."""
    return create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=WakeReason.QUOTA_EXHAUSTED,
        summary=pool_exhausted_summary(pool, reset_at, reason),
        task=task,
        attempt_id=attempt_id,
        extra_links={"routing_usage": "/v1/routing/usage"},
    )


def wake_body(uow: UnitOfWork, wake: Wake) -> bytes:
    principal = uow.principals.get(wake.principal_id)
    name = principal.name if principal else wake.principal_id
    return json.dumps(wake_document(wake, principal_name=name), sort_keys=True).encode("utf-8")


def next_backoff(attempts: int) -> int:
    index = min(max(attempts, 1), len(RETRY_BACKOFF_SECONDS)) - 1
    return RETRY_BACKOFF_SECONDS[index]


def record_delivery(
    uow: UnitOfWork,
    clock: Clock,
    wake_id: str,
    *,
    ok: bool,
    detail: str,
    retry_hours: int = DEFAULT_WAKE_RETRY_HOURS,
) -> None:
    """Fold one delivery attempt into the wake row and its event."""
    wake = uow.wakes.get(wake_id, for_update=True)
    if wake is None:
        return
    now = clock.now()
    wake.attempts += 1
    if ok:
        wake.delivered_at = now
        wake.next_attempt_at = None
        wake.last_error = None
        uow.wakes.save(wake)
        record_event(
            uow,
            clock,
            EventKind.WAKE_DELIVERED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=wake.task_id,
            payload={"wake_id": wake.id, "attempts": wake.attempts, "detail": detail},
        )
        return
    wake.last_error = detail[:500]
    deadline = wake.created_at + timedelta(hours=retry_hours)
    if now >= deadline:
        wake.gave_up_at = now
        wake.next_attempt_at = None
        uow.wakes.save(wake)
        record_event(
            uow,
            clock,
            EventKind.WAKE_DELIVERY_ABANDONED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=wake.task_id,
            payload={
                "wake_id": wake.id,
                "attempts": wake.attempts,
                "detail": detail,
                "note": "the row stays for GET /v1/wakes",
            },
        )
        return
    wake.next_attempt_at = now + timedelta(seconds=next_backoff(wake.attempts))
    uow.wakes.save(wake)
    record_event(
        uow,
        clock,
        EventKind.WAKE_DELIVERY_FAILED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=wake.task_id,
        payload={
            "wake_id": wake.id,
            "attempts": wake.attempts,
            "detail": detail,
            "next_attempt_at": wake.next_attempt_at.isoformat(),
        },
    )


def ack_wake(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, wake_id: str, note: str
) -> Wake:
    wake = uow.wakes.get(wake_id, for_update=True)
    if wake is None:
        raise NotFoundError(f"wake {wake_id} not found")
    if wake.principal_id != principal.id:
        raise ForbiddenError("a wake may only be acked by the principal it was raised for")
    if wake.acked_at is not None:
        raise ConflictError(f"wake {wake_id} was already acked")
    wake.acked_at = clock.now()
    wake.ack_note = note
    uow.wakes.save(wake)
    record_event(
        uow,
        clock,
        EventKind.WAKE_ACKED,
        principal=principal.name,
        task_id=wake.task_id,
        payload={"wake_id": wake.id, "note": note},
    )
    return wake


def system_ack_wake(uow: UnitOfWork, clock: Clock, *, wake: Wake, reason: str) -> Wake:
    """Crucible closes a wake of its own (hades #502): the ack note is the reason, and
    the `wake_acked` event is written under the `crucible` principal so the record shows
    the system, not Foundry, dealt with it. An already acked wake is left as it is."""
    if wake.acked_at is not None:
        return wake
    wake.acked_at = clock.now()
    wake.ack_note = reason
    uow.wakes.save(wake)
    record_event(
        uow,
        clock,
        EventKind.WAKE_ACKED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=wake.task_id,
        payload={"wake_id": wake.id, "note": reason, "acked_by": PRINCIPAL_CRUCIBLE},
    )
    return wake


def repeat_allowed(previous: Sequence[Wake], *, now: datetime, interval: timedelta) -> bool:
    """Whether a repeating notice may raise another wake for one task and one cause
    (hades #502). `previous` is every wake already raised for that pair. An unacked one
    blocks the repeat outright: exactly one open wake per task per cause, however long
    the condition persists. Once every one of them is acked, the repeat waits `interval`
    from the latest ack, so the notice comes back only when the condition still holds a
    full interval after Foundry last dealt with it."""
    acks: list[datetime] = []
    for wake in previous:
        if wake.acked_at is None:
            return False
        acks.append(wake.acked_at)
    return not acks or now - max(acks) >= interval


def close_wakes_for_finished_pull_requests(uow: UnitOfWork, clock: Clock) -> int:
    """hades #502: every unacked `external_review_overdue` or `ci_certification_overdue`
    wake whose pull request has since merged or closed is acked by the system with a
    reason. The supervisor runs this every tick, so the pass after the merge is observed
    closes the notice. Returns how many it closed."""
    closed = 0
    reasons = [reason.value for reason in PULL_REQUEST_OVERDUE_REASONS]
    for wake in uow.wakes.list_unacked_for_reasons(reasons):
        if wake.task_id is None:
            continue
        pull_request = uow.pull_requests.get_for_task(wake.task_id)
        if pull_request is None:
            continue
        if pull_request.state is PullRequestState.MERGED:
            outcome = "merged"
        elif pull_request.state is PullRequestState.CLOSED:
            outcome = "closed without being merged"
        else:
            continue
        system_ack_wake(
            uow,
            clock,
            wake=wake,
            reason=(
                f"closed by Crucible: pull request #{pull_request.number} has {outcome}, "
                f"so the {wake.reason} notice no longer applies (hades #502)"
            ),
        )
        closed += 1
    return closed


def retry_hours_from_policy(policy_document: dict[str, Any] | None) -> int:
    if not policy_document:
        return DEFAULT_WAKE_RETRY_HOURS
    return int(policy_document.get("limits", {}).get("wake_retry_hours", DEFAULT_WAKE_RETRY_HOURS))


def since_filter(value: datetime | None) -> datetime | None:
    return value
