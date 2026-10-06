"""Read-only swim-lane projection for the operator board (hades #489)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from crucible.application.admin.board import age_words
from crucible.application.queries import board_batch_records, board_imported_attempts
from crucible.domain.entities import PullRequestState
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.ports.repository import UnitOfWork

_S = TaskState

LANES: tuple[tuple[str, str, str], ...] = (
    (
        "waiting_on_scott",
        "Waiting on Scott",
        "A decision or design answer is needed before work can continue.",
    ),
    (
        "stuck",
        "Stuck",
        "Work stopped on a failed gate, CI job, provider error, or another blocker.",
    ),
    (
        "in_progress",
        "In progress",
        "Work is running or moving through review, certification, and merge.",
    ),
    (
        "holding_pen",
        "Holding pen",
        "Approved and proposed work waits here in dispatch order for capacity.",
    ),
    ("inbox", "Inbox", "New intake will arrive here in a later step."),
    ("wins", "Wins", "Work accepted or merged, with today's wins first."),
    ("graveyard", "Graveyard", "Work that ended without shipping, with its reason."),
)

LANE_BY_STATE: dict[TaskState, str] = {
    _S.PROPOSED: "holding_pen",
    _S.SENT_BACK: "holding_pen",
    _S.SUBMITTED: "holding_pen",
    _S.SCHEDULED: "in_progress",
    _S.RUNNING: "in_progress",
    _S.AWAITING_QUOTA: "holding_pen",
    _S.REPORTED: "in_progress",
    _S.BLOCKED: "stuck",
    _S.PRE_PR_GATES_FAILED: "stuck",
    _S.AWAITING_INTERNAL_REVIEW: "in_progress",
    _S.GATES_PASSED: "in_progress",
    _S.AWAITING_ACCEPTANCE: "in_progress",
    _S.ACCEPTED: "wins",
    _S.PUBLISHING: "in_progress",
    _S.PUBLISH_FAILED: "stuck",
    _S.AWAITING_EXTERNAL_REVIEW: "in_progress",
    _S.EXTERNAL_FEEDBACK_RECEIVED: "in_progress",
    _S.AWAITING_CI_CERTIFICATION: "in_progress",
    _S.CI_CERTIFICATION_FAILED: "stuck",
    _S.HEAD_DIVERGED: "stuck",
    _S.READY_FOR_MERGE: "in_progress",
    _S.MERGED: "wins",
    _S.RELEASE_CANDIDATE: "wins",
    _S.RELEASED: "wins",
    _S.REJECTED: "graveyard",
    _S.CANCELLING: "in_progress",
    _S.CANCELLED: "graveyard",
    _S.CLOSED: "graveyard",
}

COLLAPSED_LANES = frozenset({"wins", "graveyard"})
DECISION_KINDS = frozenset(
    {"ambiguous_contract", "decision", "design", "design_question", "decision_question"}
)
ENTRY_EVENT_BY_STATE = {
    state: getattr(EventKind, f"TASK_{state.name}", None) for state in TaskState
}
ENTRY_EVENT_KINDS = tuple(
    event.value for event in ENTRY_EVENT_BY_STATE.values() if event is not None
)
DETAIL_EVENT_KINDS = (
    EventKind.GATES_EVALUATED.value,
    EventKind.CI_CERTIFICATION_RECORDED.value,
    *ENTRY_EVENT_KINDS,
)


def lane_for_state(state: TaskState, *, waiting_on_scott: bool = False) -> str:
    if waiting_on_scott and LANE_BY_STATE[state] not in COLLAPSED_LANES:
        return "waiting_on_scott"
    return LANE_BY_STATE[state]


def _first_sentence(value: str) -> str:
    text = " ".join(value.split())
    match = re.search(r"(?<=[.!?])\s", text)
    return text[: match.start() + 1].rstrip() if match else text


def _is_scott_question(escalation: Any) -> bool:
    kind = str(
        getattr(escalation, "kind", None) or getattr(escalation, "reason", None) or ""
    ).lower()
    return kind in DECISION_KINDS


def _issue(issue: str, repository_url: str | None) -> dict[str, str]:
    url = issue if issue.startswith("https://") else ""
    if not url and repository_url and issue.startswith("#"):
        url = f"{repository_url.removesuffix('.git').rstrip('/')}/issues/{issue[1:]}"
    return {"label": issue, "url": url}


def _contract_fields(document: dict[str, Any]) -> dict[str, Any]:
    issues = [
        str(issue)
        for delivery in document.get("deliverables", [])
        for issue in delivery.get("closes", [])
    ]
    repository_url = (document.get("repository") or {}).get("url")
    execution = document.get("execution") or document.get("execution_request") or {}
    return {
        "issues": [_issue(issue, repository_url) for issue in issues],
        "tier": str(execution.get("tier") or document.get("tier") or "not recorded"),
    }


def _latest_by(rows: Iterable[Any], key: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for row in rows:
        name = str(key(row))
        previous = found.get(name)
        if previous is None or row.created_at > previous.created_at:
            found[name] = row
    return found


def _event_time(event: Any | None, fallback: datetime) -> datetime:
    return event.ts if event is not None else fallback


def _waiting_words(
    task: Any,
    lane: str,
    escalation: Any | None,
    events: dict[tuple[str, str], Any],
) -> str:
    if lane == "waiting_on_scott" and escalation is not None:
        return _first_sentence(str(escalation.question))
    if task.state is _S.PRE_PR_GATES_FAILED:
        event = events.get((task.id, EventKind.GATES_EVALUATED.value))
        failing = (event.payload if event else {}).get("failing") or []
        return "Failing gates: " + (", ".join(map(str, failing)) or "not recorded")
    if task.state is _S.CI_CERTIFICATION_FAILED:
        event = events.get((task.id, EventKind.CI_CERTIFICATION_RECORDED.value))
        payload = event.payload if event else {}
        job = payload.get("job") or payload.get("check") or payload.get("name")
        return f"CI job: {job or 'not recorded'}"
    return str(task.state.value).replace("_", " ")


def _recorded_reason(task: Any, events: dict[tuple[str, str], Any]) -> str:
    kind = ENTRY_EVENT_BY_STATE.get(task.state)
    event = events.get((task.id, kind.value)) if kind is not None else None
    payload = event.payload if event else {}
    return str(payload.get("reason") or payload.get("summary") or "Reason not recorded")


def _replacement(reason: str, tasks_by_external: dict[str, Any]) -> dict[str, str] | None:
    for external_id, task in tasks_by_external.items():
        if external_id and re.search(rf"(?<![\w-]){re.escape(external_id)}(?![\w-])", reason):
            return {"external_id": external_id, "task_id": task.id}
    return None


def _states_for_lane(key: str) -> tuple[TaskState, ...]:
    return tuple(state for state, lane in LANE_BY_STATE.items() if lane == key)


def board_lanes_view(uow: UnitOfWork, now: datetime) -> dict[str, Any]:
    """Build the board with a fixed number of repository reads as task count grows."""
    counts = dict(uow.tasks.count_by_state())
    rows_by_lane = {
        key: list(uow.tasks.list_in_states(_states_for_lane(key)))
        for key, _name, _meaning in LANES
        if key not in {"waiting_on_scott", "inbox"}
    }
    tasks = [task for rows in rows_by_lane.values() for task in rows]
    task_ids = {task.id for task in tasks}
    contracts, _comments, _dispositions = board_batch_records(
        uow, {task.id: task.contract_version for task in tasks}, []
    )
    attempts = list(uow.attempts.list_in_states(list(AttemptState)))
    attempts.extend(board_imported_attempts(uow, task_ids))
    current_attempt = _latest_by(attempts, lambda row: row.task_id)
    pull_requests = {
        row.task_id: row
        for row in uow.pull_requests.list_in_states(list(PullRequestState))
        if row.task_id in task_ids
    }
    open_escalations = [row for row in uow.escalations.list_open() if row.task_id in task_ids]
    escalation_by_task = _latest_by(open_escalations, lambda row: row.task_id)
    events = dict(uow.events.latest_for_tasks_kinds(list(task_ids), DETAIL_EVENT_KINDS))
    tasks_by_external = {task.external_id: task for task in tasks}

    cards: dict[str, list[dict[str, Any]]] = {key: [] for key, _name, _meaning in LANES}
    for task in tasks:
        escalation = escalation_by_task.get(task.id)
        lane_key = lane_for_state(
            task.state,
            waiting_on_scott=bool(escalation and _is_scott_question(escalation)),
        )
        attempt = current_attempt.get(task.id)
        pr = pull_requests.get(task.id)
        entry_kind = ENTRY_EVENT_BY_STATE.get(task.state)
        entry = events.get((task.id, entry_kind.value)) if entry_kind is not None else None
        entered_at = (
            escalation.opened_at
            if lane_key == "waiting_on_scott" and escalation is not None
            else _event_time(entry, task.closed_at or task.updated_at)
        )
        fields = _contract_fields(contracts.get(task.id, {}))
        reason = _recorded_reason(task, events) if lane_key == "graveyard" else None
        cards[lane_key].append(
            {
                "id": task.id,
                "external_id": task.external_id,
                "project": task.project,
                "title": task.title,
                "issues": fields["issues"],
                "pull_request": ({"number": pr.number, "url": pr.url} if pr is not None else None),
                "harness": attempt.selected_harness if attempt else None,
                "model": attempt.selected_model if attempt else None,
                "tier": fields["tier"],
                "waiting_on": _waiting_words(task, lane_key, escalation, events),
                "age": {
                    "entered_at": entered_at,
                    "label": age_words(max(0, int((now - entered_at).total_seconds()))),
                },
                "reason": reason,
                "replacement": _replacement(reason, tasks_by_external) if reason else None,
            }
        )

    today = now.date()
    lanes = []
    for key, name, meaning in LANES:
        ordered = sorted(cards[key], key=lambda card: card["age"]["entered_at"])
        if key == "holding_pen":
            ordered.sort(key=lambda card: card["age"]["entered_at"])
        lane_document: dict[str, Any] = {
            "key": key,
            "name": name,
            "meaning": meaning,
            "collapsed": key in COLLAPSED_LANES,
            "count": len(ordered),
            "cards": ordered,
        }
        if key == "wins":
            lane_document["count_today"] = sum(
                card["age"]["entered_at"].date() == today for card in ordered
            )
            lane_document["count_all_time"] = sum(
                counts.get(state, 0) for state in _states_for_lane("wins")
            )
        lanes.append(lane_document)
    return {"generated_at": now, "lanes": lanes}


__all__ = ["COLLAPSED_LANES", "LANES", "LANE_BY_STATE", "board_lanes_view", "lane_for_state"]
