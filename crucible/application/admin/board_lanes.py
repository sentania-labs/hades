"""Read-only swim-lane projection for the operator board (hades #489)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from crucible.application.admin.board import age_words
from crucible.application.personas_jobs import inbox_run, run_findings
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
        "Approved work waits here in dispatch order for capacity.",
    ),
    ("inbox", "Inbox", "Proposed work waits here for an operator to approve it."),
    ("wins", "Wins", "Work accepted or merged, with today's wins first."),
    ("graveyard", "Graveyard", "Work that ended without shipping, with its reason."),
)

LANE_BY_STATE: dict[TaskState, str] = {
    _S.PROPOSED: "inbox",
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
    # A task reaches `closed` only from accepted, merged or released (lifecycle), so a
    # closed task is finished work, not a failure.
    _S.CLOSED: "wins",
}

COLLAPSED_LANES = frozenset({"wins", "graveyard"})
DECISION_KINDS = frozenset(
    {"ambiguous_contract", "decision", "design", "design_question", "decision_question"}
)
# The events that put a task into each state. Most states have one event named for
# them; a few are entered by more than one (a proposal rejection is
# `task_proposal_rejected`, an approval submits, a retry or a quota resume schedules).
_EXTRA_ENTRY_EVENTS: dict[TaskState, tuple[EventKind, ...]] = {
    _S.REJECTED: (EventKind.TASK_PROPOSAL_REJECTED,),
    _S.SUBMITTED: (EventKind.TASK_APPROVED,),
    _S.SCHEDULED: (EventKind.TASK_RETRY_SCHEDULED, EventKind.TASK_QUOTA_RESUMED),
}
ENTRY_EVENTS_BY_STATE: dict[TaskState, tuple[EventKind, ...]] = {
    state: tuple(
        event
        for event in (
            getattr(EventKind, f"TASK_{state.name}", None),
            *_EXTRA_ENTRY_EVENTS.get(state, ()),
        )
        if event is not None
    )
    for state in TaskState
}
ENTRY_EVENT_KINDS = tuple(
    dict.fromkeys(event.value for events in ENTRY_EVENTS_BY_STATE.values() for event in events)
)
# The supervisor takes scheduled work in the order of the event that last scheduled it
# (`transitions.queue_key`); a launch held for capacity keeps that place.
DISPATCH_EVENT_KINDS = (
    EventKind.TASK_SCHEDULED.value,
    EventKind.TASK_RETRY_SCHEDULED.value,
)
DETAIL_EVENT_KINDS = tuple(
    dict.fromkeys(
        (
            EventKind.GATES_EVALUATED.value,
            EventKind.CI_CERTIFICATION_RECORDED.value,
            *ENTRY_EVENT_KINDS,
            *DISPATCH_EVENT_KINDS,
        )
    )
)


def lane_for_state(state: TaskState, *, waiting_on_scott: bool = False) -> str:
    if waiting_on_scott and LANE_BY_STATE[state] not in COLLAPSED_LANES:
        return "waiting_on_scott"
    return LANE_BY_STATE[state]


def first_sentence(value: str) -> str:
    text = " ".join(value.split())
    match = re.search(r"(?<=[.!?])\s", text)
    return text[: match.start() + 1].rstrip() if match else text


def is_scott_question(escalation: Any) -> bool:
    kind = str(
        getattr(escalation, "kind", None) or getattr(escalation, "reason", None) or ""
    ).lower()
    return kind in DECISION_KINDS


def issue_link(issue: str, repository_url: str | None) -> dict[str, str]:
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
        "issues": [issue_link(issue, repository_url) for issue in issues],
        "tier": str(execution.get("tier") or document.get("tier") or "not recorded"),
    }


def _latest_by(rows: Iterable[Any], key: Any, when: Any) -> dict[str, Any]:
    """The newest row per key, newest by `when` (attempts are created, escalations
    are opened; the two entities name that instant differently)."""
    found: dict[str, Any] = {}
    for row in rows:
        name = str(key(row))
        previous = found.get(name)
        if previous is None or when(row) > when(previous):
            found[name] = row
    return found


def _entry_event(task: Any, events: dict[tuple[str, str], Any]) -> Any | None:
    """The latest of the events that put the task in its current state."""
    found = [
        event
        for kind in ENTRY_EVENTS_BY_STATE.get(task.state, ())
        if (event := events.get((task.id, kind.value))) is not None
    ]
    return max(found, key=lambda event: (event.ts, int(event.seq or 0))) if found else None


def _dispatch_seq(task: Any, events: dict[tuple[str, str], Any]) -> int | None:
    """Where a capacity-held launch stands in the supervisor's dispatch order: the
    sequence of the event that last scheduled it, as `transitions.queue_key` ranks it."""
    if task.state is not _S.AWAITING_QUOTA:
        return None
    seqs = [
        int(event.seq or 0)
        for kind in DISPATCH_EVENT_KINDS
        if (event := events.get((task.id, kind))) is not None
    ]
    return max(seqs) if seqs else 0


def _waiting_words(
    task: Any,
    lane: str,
    escalation: Any | None,
    events: dict[tuple[str, str], Any],
) -> str:
    if lane == "waiting_on_scott" and escalation is not None:
        return first_sentence(str(escalation.question))
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
    event = _entry_event(task, events)
    payload = event.payload if event else {}
    return str(payload.get("reason") or payload.get("summary") or "Reason not recorded")


def _replacement(reason: str, tasks_by_external: dict[str, Any]) -> dict[str, str] | None:
    for external_id, task in tasks_by_external.items():
        if external_id and re.search(rf"(?<![\w-]){re.escape(external_id)}(?![\w-])", reason):
            return {"external_id": external_id, "task_id": task.id}
    return None


def _states_for_lane(key: str) -> tuple[TaskState, ...]:
    return tuple(state for state, lane in LANE_BY_STATE.items() if lane == key)


def board_lanes_view(
    uow: UnitOfWork, now: datetime, *, cards_for: frozenset[str] | None = None
) -> dict[str, Any]:
    """Build the board with a fixed number of repository reads as task count grows.

    `cards_for` names the lanes whose cards are built; every lane still has its count.
    A collapsed lane that is not selected is counted from `count_by_state` and its
    contracts, attempts, pull requests, escalations, and events are never read, so the
    terminal lanes do not slow the live board as they grow. The document's `records`
    maps each built card's task id to its task, open escalation, and pull request, so
    callers adding actions need no further reads."""
    counts = dict(uow.tasks.count_by_state())
    selected = cards_for if cards_for is not None else frozenset(key for key, _n, _m in LANES)
    # Waiting on Scott takes its cards from the live lanes, which are always listed.
    # Wins task rows are listed for today's count and graveyard replacements; only the
    # Graveyard, which nothing else reads, is left unlisted when it is not selected.
    listed = {
        key
        for key, _name, _meaning in LANES
        if key != "waiting_on_scott" and (key != "graveyard" or key in selected)
    }
    rows_by_lane = {
        key: list(uow.tasks.list_in_states(_states_for_lane(key)))
        for key, _name, _meaning in LANES
        if key in listed
    }
    tasks = [task for rows in rows_by_lane.values() for task in rows]
    live_ids = {task.id for task in tasks}
    open_escalations = [row for row in uow.escalations.list_open() if row.task_id in live_ids]
    escalation_by_task = _latest_by(
        open_escalations, lambda row: row.task_id, lambda row: row.opened_at
    )
    lane_by_task = {
        task.id: lane_for_state(
            task.state,
            waiting_on_scott=bool(
                (escalation := escalation_by_task.get(task.id)) and is_scott_question(escalation)
            ),
        )
        for task in tasks
    }
    lane_counts = Counter(lane_by_task.values())
    for key in {key for key, _name, _meaning in LANES} - listed - {"waiting_on_scott"}:
        lane_counts[key] = sum(counts.get(state, 0) for state in _states_for_lane(key))
    built = [task for task in tasks if lane_by_task[task.id] in selected]
    task_ids = {task.id for task in built}
    contracts, _comments, _dispositions = board_batch_records(
        uow, {task.id: task.contract_version for task in built}, []
    )
    attempts = (
        [row for row in uow.attempts.list_in_states(list(AttemptState)) if row.task_id in task_ids]
        if task_ids
        else []
    )
    attempts.extend(board_imported_attempts(uow, task_ids))
    current_attempt = _latest_by(attempts, lambda row: row.task_id, lambda row: row.created_at)
    pull_requests = (
        {
            row.task_id: row
            for row in uow.pull_requests.list_in_states(list(PullRequestState))
            if row.task_id in task_ids
        }
        if task_ids
        else {}
    )
    events = (
        dict(uow.events.latest_for_tasks_kinds(list(task_ids), DETAIL_EVENT_KINDS))
        if task_ids
        else {}
    )
    tasks_by_external = {task.external_id: task for task in tasks}

    cards: dict[str, list[dict[str, Any]]] = {key: [] for key, _name, _meaning in LANES}
    records: dict[str, dict[str, Any]] = {}
    for task in built:
        escalation = escalation_by_task.get(task.id)
        lane_key = lane_by_task[task.id]
        scheduled_inbox = inbox_run(contracts.get(task.id, {}))
        findings = run_findings(uow, task.id) if scheduled_inbox else None
        if scheduled_inbox and lane_key != "inbox":
            # A scheduled job's inbox_card run shows in the Inbox lane whatever its
            # state, with the run's findings as the card body; move its count too.
            lane_counts[lane_key] -= 1
            lane_counts["inbox"] += 1
            lane_key = "inbox"
        attempt = current_attempt.get(task.id)
        pr = pull_requests.get(task.id)
        records[task.id] = {"task": task, "escalation": escalation, "pull_request": pr}
        entry = _entry_event(task, events)
        entered_at = (
            escalation.opened_at
            if lane_key == "waiting_on_scott" and escalation is not None
            else (entry.ts if entry is not None else task.closed_at or task.updated_at)
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
                "waiting_on": findings or _waiting_words(task, lane_key, escalation, events),
                "age": {
                    "entered_at": entered_at,
                    "label": age_words(max(0, int((now - entered_at).total_seconds()))),
                },
                "reason": reason,
                "replacement": _replacement(reason, tasks_by_external) if reason else None,
                "dispatch_seq": _dispatch_seq(task, events),
            }
        )

    today = now.date()
    lanes = []
    for key, name, meaning in LANES:
        ordered = sorted(cards[key], key=lambda card: card["age"]["entered_at"])
        if key == "holding_pen":
            # Dispatch order: launches held for capacity first, in the order the
            # supervisor will take them, then work not yet scheduled, oldest first.
            ordered.sort(
                key=lambda card: (
                    card["dispatch_seq"] is None,
                    card["dispatch_seq"] or 0,
                    card["age"]["entered_at"],
                )
            )
        lane_document: dict[str, Any] = {
            "key": key,
            "name": name,
            "meaning": meaning,
            "collapsed": key in COLLAPSED_LANES,
            "count": lane_counts[key],
            "cards": ordered,
        }
        if key == "wins":
            lane_document["count_today"] = (
                sum(card["age"]["entered_at"].date() == today for card in ordered)
                if key in selected
                else _wins_today(uow, now, rows_by_lane["wins"])
            )
            lane_document["count_all_time"] = sum(
                counts.get(state, 0) for state in _states_for_lane("wins")
            )
        lanes.append(lane_document)
    return {"generated_at": now, "lanes": lanes, "records": records}


def _wins_today(uow: UnitOfWork, now: datetime, wins: list[Any]) -> int:
    """Wins entered today without building their cards: a task entered its lane no
    later than its last update, so only tasks updated today can count, and only their
    entry events are read."""
    today = now.date()
    candidates = [task for task in wins if task.updated_at.date() >= today]
    if not candidates:
        return 0
    events = dict(
        uow.events.latest_for_tasks_kinds([task.id for task in candidates], ENTRY_EVENT_KINDS)
    )
    entered = [
        entry.ts
        if (entry := _entry_event(task, events)) is not None
        else task.closed_at or task.updated_at
        for task in candidates
    ]
    return sum(moment.date() == today for moment in entered)


__all__ = [
    "COLLAPSED_LANES",
    "LANES",
    "LANE_BY_STATE",
    "board_lanes_view",
    "first_sentence",
    "is_scott_question",
    "issue_link",
    "lane_for_state",
]
