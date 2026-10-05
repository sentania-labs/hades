"""Read-only operator board assembled from bounded repository scans."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from crucible.application.observation import DEFAULT_CI_TIMEOUT_HOURS
from crucible.application.queries import (
    board_batch_records,
    board_imported_attempts,
    board_latest_unacked_wakes,
)
from crucible.domain.certification import wait_timeout_hours
from crucible.domain.entities import PullRequestState
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TASK_TERMINAL, AttemptState, ExecutionState, TaskState
from crucible.ports.repository import UnitOfWork

QUALITY_DAYS = 14

# hades #334: the kanban's columns, left to right, keyed for the template and the admin API.
# The first holds the proposals of hades #424, waiting for the operator's answer.
KANBAN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("proposed", "Proposed"),
    ("queued", "Queued"),
    ("running", "Running"),
    ("foundry", "Awaiting Foundry"),
    ("codex", "Awaiting Codex"),
    ("ci", "Awaiting CI"),
    ("merge", "Ready to merge"),
    ("blocked", "Blocked or failed"),
    ("done", "Done in the last 24 hours"),
)
RESERVED_COLUMNS: frozenset[str] = frozenset()
COLUMN_NOTES = {
    "proposed": "Waiting for the operator: approve, send back or reject. Open a card to answer it.",
    "queued": "In queue order: the first card starts first.",
}
# A card in Awaiting Foundry or Awaiting Codex colours after this long; one in Awaiting CI
# colours after the policy's CI budget (`ci_certification.wait_timeout_hours`).
ATTENTION_MINUTES = 30
DONE_HOURS = 24

_C = TaskState
COLUMN_BY_STATE: dict[TaskState, str] = {
    _C.PROPOSED: "proposed",
    # The orchestrator holds a proposal the operator sent back, until it amends it.
    _C.SENT_BACK: "foundry",
    _C.SUBMITTED: "queued",
    _C.SCHEDULED: "queued",
    _C.AWAITING_QUOTA: "queued",
    _C.RUNNING: "running",
    _C.CANCELLING: "running",
    # Hades holds these between the worker and the next wait: gates, acceptance,
    # publication. They are short and the chip says what Hades is doing.
    _C.REPORTED: "running",
    _C.GATES_PASSED: "running",
    _C.ACCEPTED: "running",
    _C.PUBLISHING: "running",
    _C.AWAITING_INTERNAL_REVIEW: "foundry",
    _C.AWAITING_ACCEPTANCE: "foundry",
    _C.EXTERNAL_FEEDBACK_RECEIVED: "foundry",
    _C.AWAITING_EXTERNAL_REVIEW: "codex",
    _C.AWAITING_CI_CERTIFICATION: "ci",
    _C.READY_FOR_MERGE: "merge",
    _C.BLOCKED: "blocked",
    _C.PRE_PR_GATES_FAILED: "blocked",
    _C.PUBLISH_FAILED: "blocked",
    _C.CI_CERTIFICATION_FAILED: "blocked",
    _C.HEAD_DIVERGED: "blocked",
    _C.MERGED: "done",
    _C.RELEASE_CANDIDATE: "done",
    _C.RELEASED: "done",
    _C.CLOSED: "done",
    _C.CANCELLED: "done",
    _C.REJECTED: "done",
}

# The states a queued card has a place in the queue in: the supervisor takes these.
QUEUED_STATES: frozenset[TaskState] = frozenset({_C.SCHEDULED, _C.AWAITING_QUOTA})

# The operator decides these from the task page or the API; the other blocked states
# carry a wake the operator reads on the Wakes page.
OPERATOR_DECISION_STATES: frozenset[TaskState] = frozenset(
    {_C.HEAD_DIVERGED, _C.CI_CERTIFICATION_FAILED}
)

# Each transition writes its event in the same transaction as the state change (09), so
# the event stream is the record of when a card entered a column. A state with no event
# of its own (release candidate, released) falls back to the task's `updated_at`.
STATE_BY_ENTRY_EVENT: dict[str, TaskState] = {
    EventKind.TASK_PROPOSED.value: _C.PROPOSED,
    EventKind.TASK_APPROVED.value: _C.SUBMITTED,
    EventKind.TASK_SENT_BACK.value: _C.SENT_BACK,
    EventKind.TASK_PROPOSAL_REJECTED.value: _C.REJECTED,
    EventKind.TASK_SUBMITTED.value: _C.SUBMITTED,
    EventKind.TASK_SCHEDULED.value: _C.SCHEDULED,
    EventKind.TASK_RETRY_SCHEDULED.value: _C.SCHEDULED,
    EventKind.TASK_AWAITING_QUOTA.value: _C.AWAITING_QUOTA,
    EventKind.TASK_RUNNING.value: _C.RUNNING,
    EventKind.TASK_REPORTED.value: _C.REPORTED,
    EventKind.TASK_BLOCKED.value: _C.BLOCKED,
    EventKind.TASK_PRE_PR_GATES_FAILED.value: _C.PRE_PR_GATES_FAILED,
    EventKind.TASK_AWAITING_INTERNAL_REVIEW.value: _C.AWAITING_INTERNAL_REVIEW,
    EventKind.TASK_GATES_PASSED.value: _C.GATES_PASSED,
    EventKind.TASK_AWAITING_ACCEPTANCE.value: _C.AWAITING_ACCEPTANCE,
    EventKind.TASK_ACCEPTED.value: _C.ACCEPTED,
    EventKind.TASK_PUBLISHING.value: _C.PUBLISHING,
    EventKind.TASK_PUBLISH_FAILED.value: _C.PUBLISH_FAILED,
    EventKind.TASK_AWAITING_EXTERNAL_REVIEW.value: _C.AWAITING_EXTERNAL_REVIEW,
    EventKind.TASK_EXTERNAL_FEEDBACK_RECEIVED.value: _C.EXTERNAL_FEEDBACK_RECEIVED,
    EventKind.TASK_AWAITING_CI_CERTIFICATION.value: _C.AWAITING_CI_CERTIFICATION,
    EventKind.TASK_CI_CERTIFICATION_FAILED.value: _C.CI_CERTIFICATION_FAILED,
    EventKind.TASK_HEAD_DIVERGED.value: _C.HEAD_DIVERGED,
    EventKind.TASK_READY_FOR_MERGE.value: _C.READY_FOR_MERGE,
    EventKind.TASK_MERGED.value: _C.MERGED,
    EventKind.TASK_CANCELLING.value: _C.CANCELLING,
    EventKind.TASK_CANCELLED.value: _C.CANCELLED,
    EventKind.TASK_REJECTED.value: _C.REJECTED,
    EventKind.TASK_CLOSED.value: _C.CLOSED,
}

DONE_WORDS = {
    _C.MERGED: "Merged",
    _C.RELEASE_CANDIDATE: "Release candidate",
    _C.RELEASED: "Released",
    _C.CLOSED: "Closed",
    _C.CANCELLED: "Cancelled",
    _C.REJECTED: "Rejected",
}

GROUPS = (
    "Running",
    "Awaiting Foundry: internal review",
    "Awaiting Foundry: acceptance",
    "Awaiting Foundry: decision",
    "Awaiting Foundry: escalation",
    "Awaiting external: Codex review",
    "Awaiting external: CI",
    "Awaiting external: merge queue",
    "Blocked or failed gates",
)

GROUP_BY_STATE = {
    TaskState.SENT_BACK: GROUPS[3],
    TaskState.AWAITING_INTERNAL_REVIEW: GROUPS[1],
    TaskState.AWAITING_ACCEPTANCE: GROUPS[2],
    TaskState.EXTERNAL_FEEDBACK_RECEIVED: GROUPS[3],
    TaskState.AWAITING_EXTERNAL_REVIEW: GROUPS[5],
    TaskState.AWAITING_CI_CERTIFICATION: GROUPS[6],
    TaskState.READY_FOR_MERGE: GROUPS[7],
    TaskState.BLOCKED: GROUPS[8],
    TaskState.PRE_PR_GATES_FAILED: GROUPS[8],
    TaskState.PUBLISH_FAILED: GROUPS[8],
    TaskState.CI_CERTIFICATION_FAILED: GROUPS[8],
    TaskState.HEAD_DIVERGED: GROUPS[8],
}


def _events(uow: UnitOfWork, since: datetime | None = None) -> list[Any]:
    rows: list[Any] = []
    after = 0
    while True:
        page = list(uow.events.list_global(after_seq=after, kind=None, since=since, limit=1000))
        rows.extend(page)
        if len(page) < 1000:
            return rows
        after = int(page[-1].seq or after)


def _all_tasks(uow: UnitOfWork) -> list[Any]:
    rows: list[Any] = []
    after: str | None = None
    while True:
        page = list(
            uow.tasks.search(
                state=None,
                project=None,
                repository_id=None,
                external_id=None,
                updated_since=None,
                after_id=after,
                limit=200,
            )
        )
        rows.extend(page)
        if len(page) < 200:
            return rows
        after = page[-1].id


def waiting_group(state: TaskState, *, has_open_escalation: bool = False) -> str:
    if has_open_escalation:
        return GROUPS[4]
    return GROUP_BY_STATE.get(state, GROUPS[0])


def waiting_line(wake: Any | None, state: TaskState) -> str:
    if wake is None:
        return state.value.replace("_", " ")
    summary = wake.payload.get("summary") if isinstance(wake.payload, dict) else None
    return str(summary or wake.reason).replace("\n", " ").strip()


def ci_summary(state: str | None) -> dict[str, str]:
    value = (state or "none").lower()
    if value in {"green", "success", "successful", "skipped"}:
        return {"status": "green", "label": "green"}
    if value in {"failed", "failure", "error", "cancelled"}:
        return {"status": "red", "label": "red"}
    if value in {"pending", "queued", "in_progress", "running"}:
        return {"status": "running", "label": "running"}
    return {"status": "none", "label": "none"}


def eta_bound(attempt: Any | None, timeout_seconds: int | None, now: datetime) -> dict[str, Any]:
    if attempt is None or attempt.started_at is None or timeout_seconds is None:
        return {"elapsed_seconds": None, "timeout_seconds": timeout_seconds, "label": "not started"}
    ended = attempt.ended_at or now
    elapsed = max(0, int((ended - attempt.started_at).total_seconds()))
    return {
        "elapsed_seconds": elapsed,
        "timeout_seconds": timeout_seconds,
        "label": f"{elapsed}s / {timeout_seconds}s",
    }


def _latest_by(items: Iterable[Any], key: Any, time: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        name = str(key(item))
        if name not in result or time(item) > time(result[name]):
            result[name] = item
    return result


def _event_maps(
    events: list[Any],
) -> tuple[dict[str, int], dict[str, list[str]], dict[str, Any], dict[str, Any]]:
    corrections: Counter[str] = Counter()
    failed: dict[str, list[str]] = defaultdict(list)
    latest_ci: dict[str, Any] = {}
    latest_ci_decision: dict[str, Any] = {}
    for event in events:
        if not event.task_id:
            continue
        if event.kind == "task_correction_attached":
            corrections[event.task_id] += 1
        if event.kind == "gates_evaluated" and event.payload.get("phase") == "pre_pr":
            for gate in event.payload.get("failing", []):
                name = str(gate)
                if name not in failed[event.task_id]:
                    failed[event.task_id].append(name)
        if event.kind == "ci_certification_recorded":
            previous = latest_ci.get(event.task_id)
            if previous is None or event.ts > previous.ts:
                latest_ci[event.task_id] = event
        if event.kind == "ci_decision_recorded":
            # hades #356: the operator's diagnosis, shown beside the task's CI state.
            previous_decision = latest_ci_decision.get(event.task_id)
            if previous_decision is None or event.ts > previous_decision.ts:
                latest_ci_decision[event.task_id] = event
    return dict(corrections), failed, latest_ci, latest_ci_decision


def _contract_fields(
    documents: dict[str, dict[str, Any]], tasks: list[Any]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for task in tasks:
        document = documents.get(task.id, {})
        issues = [
            str(issue)
            for delivery in document.get("deliverables", [])
            for issue in delivery.get("closes", [])
        ]
        result[task.id] = {
            "parent_external_id": document.get("parent_external_id"),
            "issues": issues,
            "repository_url": (document.get("repository") or {}).get("url"),
        }
    return result


def _issue(issue: str, repository_url: str | None) -> dict[str, str]:
    href = issue if issue.startswith("https://") else ""
    if not href and repository_url and issue.startswith("#"):
        href = f"{repository_url.removesuffix('.git').rstrip('/')}/issues/{issue[1:]}"
    return {"label": issue, "url": href}


def _routing(attempt: Any) -> dict[str, Any]:
    candidates = []
    for index, candidate in enumerate(attempt.ordered_candidates, 1):
        chosen = (
            candidate.get("model") == attempt.selected_model
            and candidate.get("harness") == attempt.selected_harness
            and candidate.get("pool") == attempt.selected_pool
        )
        reason = "chosen"
        if candidate.get("busy"):
            reason = "busy, skipped"
        elif not chosen:
            reason = str(candidate.get("reason") or "fallback")
        candidates.append({"order": index, **candidate, "chosen": chosen, "reason": reason})
    return {"attempt_id": attempt.id, "candidates": candidates}


def _token_view(metrics: list[Any]) -> dict[str, Any]:
    attempts = []
    totals: dict[tuple[str, str, str], dict[str, Any]] = {}
    for metric in metrics:
        recorded = metric.tokens_in is not None or metric.tokens_out is not None
        row = {
            "attempt_id": metric.attempt_id,
            "harness": metric.harness,
            "model": metric.model,
            "pool": metric.pool,
            "tokens_in": metric.tokens_in,
            "tokens_out": metric.tokens_out,
            "recording": "recorded" if recorded else "not recorded",
        }
        attempts.append(row)
        key = (metric.harness, metric.model, metric.pool)
        total = totals.setdefault(
            key,
            {
                "harness": key[0],
                "model": key[1],
                "pool": key[2],
                "tokens_in": 0,
                "tokens_out": 0,
                "recorded_attempts": 0,
                "unrecorded_attempts": 0,
            },
        )
        if recorded:
            total["tokens_in"] += metric.tokens_in or 0
            total["tokens_out"] += metric.tokens_out or 0
            total["recorded_attempts"] += 1
        else:
            total["unrecorded_attempts"] += 1
    return {"attempts": attempts, "totals": list(totals.values())}


def quality_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["harness"], row["model"])
        total = totals.setdefault(
            key,
            {
                "harness": key[0],
                "model": key[1],
                "tasks": 0,
                "failed_gates": 0,
                "findings": {},
                "corrections": 0,
                "merged": 0,
                "cancelled": 0,
                "open": 0,
            },
        )
        total["tasks"] += 1
        total["failed_gates"] += len(row["failed_gates"])
        total["corrections"] += row["corrections"]
        total[row["outcome"]] += 1
        for severity, dispositions in row["findings"].items():
            bucket = total["findings"].setdefault(severity, {})
            for disposition, count in dispositions.items():
                bucket[disposition] = bucket.get(disposition, 0) + count
    return list(totals.values())


def _severity(body: str) -> str:
    match = re.search(r"\b(P[0-3]|critical|high|medium|low|major|minor)\b", body, re.I)
    return match.group(1).lower() if match else "unspecified"


def kanban_column(state: TaskState, *, has_open_escalation: bool = False) -> str:
    """The column a task's card sits in. An open escalation moves a card that is not done
    to Awaiting Foundry, as the list view's escalation group does."""
    column = COLUMN_BY_STATE[state]
    if has_open_escalation and column != "done":
        return "foundry"
    return column


def age_words(seconds: int) -> str:
    """How long a card has sat in its column, in the unit an operator reads it in."""
    minutes = max(0, seconds) // 60
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


def _state_words(state: TaskState) -> str:
    return state.value.replace("_", " ")


def kanban_holder(
    column: str, state: TaskState, attempt: Any | None, wake: Any | None
) -> dict[str, str]:
    """Who holds the ball: the worker (harness and model), Foundry, Codex, CI, the
    operator for a wake or decision, or Hades between waits. `detail` is the one line
    under the chip."""
    if column == "foundry":
        # A card moved here by an open escalation says so; one here by state shows its
        # newest pending wake, as the list view does.
        in_place = COLUMN_BY_STATE[state] == "foundry"
        detail = waiting_line(wake, state) if in_place else "escalation open"
        return {"kind": "foundry", "label": "Foundry", "detail": detail}
    if column == "running" and state is _C.RUNNING and attempt is not None:
        return {
            "kind": "worker",
            "label": f"Worker: {attempt.selected_harness} / {attempt.selected_model}",
            "detail": f"pool {attempt.selected_pool}" if attempt.selected_pool else "",
        }
    if column == "codex":
        return {"kind": "codex", "label": "Codex", "detail": waiting_line(wake, state)}
    if column == "ci":
        return {"kind": "ci", "label": "CI", "detail": waiting_line(wake, state)}
    if column == "blocked":
        detail = "decision" if state in OPERATOR_DECISION_STATES else "wake"
        return {
            "kind": "operator",
            "label": "Operator",
            "detail": f"{detail}: {waiting_line(wake, state)}",
        }
    if column == "proposed":
        return {"kind": "operator", "label": "Operator", "detail": "approve, send back or reject"}
    if column == "done":
        return {"kind": "done", "label": DONE_WORDS.get(state, "Done"), "detail": ""}
    detail = "merge" if column == "merge" else _state_words(state)
    return {"kind": "hades", "label": "Hades", "detail": detail}


def _column_entries(events: list[Any]) -> dict[str, list[tuple[datetime, int, str]]]:
    """Per task, its state-entry events as (ts, seq, column), in order."""
    entries: dict[str, list[tuple[datetime, int, str]]] = defaultdict(list)
    for event in events:
        state = STATE_BY_ENTRY_EVENT.get(event.kind)
        if state is None or not event.task_id:
            continue
        entries[event.task_id].append((event.ts, int(event.seq or 0), COLUMN_BY_STATE[state]))
    for rows in entries.values():
        rows.sort(key=lambda item: (item[0], item[1]))
    return entries


def column_entered_at(
    entries: list[tuple[datetime, int, str]], column: str, fallback: datetime
) -> datetime:
    """When the card entered its current column: the first event of the latest run of
    entries into that column. A queued card that went submitted then scheduled has been
    queued since it was submitted. The fallback is used when the events do not end in
    the column the task is in (no entry event for the state, or an imported task)."""
    entered: datetime | None = None
    previous: str | None = None
    for ts, _seq, entry_column in entries:
        if entry_column != previous:
            entered = ts
        previous = entry_column
    if previous != column or entered is None:
        return fallback
    return entered


def _ci_budget_seconds(uow: UnitOfWork, task: Any, cache: dict[tuple[str, int], int]) -> int:
    key = (str(task.policy_name), int(task.policy_version))
    if key not in cache:
        stored = uow.policies.get(*key)
        document = stored.document if stored else {}
        cache[key] = (
            wait_timeout_hours(document, "ci_certification", DEFAULT_CI_TIMEOUT_HOURS) * 3600
        )
    return cache[key]


def kanban_age(
    column: str, entered_at: datetime, now: datetime, *, ci_budget_seconds: int | None
) -> dict[str, Any]:
    seconds = max(0, int((now - entered_at).total_seconds()))
    budget: int | None = None
    if column in {"foundry", "codex"}:
        budget = ATTENTION_MINUTES * 60
    elif column == "ci":
        budget = ci_budget_seconds
    return {
        "entered_at": entered_at,
        "seconds": seconds,
        "label": age_words(seconds),
        "late": budget is not None and seconds > budget,
        "budget_seconds": budget,
    }


QUEUE_EVENTS = frozenset({EventKind.TASK_SCHEDULED.value, EventKind.TASK_RETRY_SCHEDULED.value})


def _queue_seqs(events: list[Any]) -> dict[str, int]:
    """Per task, the event that last put it in the queue. The supervisor takes scheduled
    tasks in this order (`queue_key`), so a batch approval shows in its selected order."""
    seqs: dict[str, int] = {}
    for event in events:
        if event.kind in QUEUE_EVENTS and event.task_id:
            seqs[event.task_id] = max(seqs.get(event.task_id, 0), int(event.seq or 0))
    return seqs


def _approval_batches(events: list[Any]) -> dict[str, dict[str, Any]]:
    """Per task, the batch its latest approval was part of (hades #424)."""
    batches: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.kind == EventKind.TASK_APPROVED.value and event.task_id:
            batch = (event.payload or {}).get("batch")
            if isinstance(batch, dict):
                batches[event.task_id] = batch
            else:
                batches.pop(event.task_id, None)
    return batches


def _queue_order(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The Queued column in queue order: scheduled cards by the event that queued them,
    then cards not started yet, oldest first. Each scheduled card gets its position."""
    ordered = sorted(
        cards,
        key=lambda card: (
            card["queue"]["seq"] is None,
            card["queue"]["seq"] or 0,
            card["age"]["entered_at"],
        ),
    )
    position = 0
    for card in ordered:
        if card["queue"]["seq"] is not None:
            position += 1
            card["queue"]["position"] = position
    return ordered


def _kanban_groups(cards: list[dict[str, Any]], *, queued: bool = False) -> list[dict[str, Any]]:
    """Cards under their parent, oldest first (the Queued column in queue order); the list
    view's nesting, inside a column."""
    ordered = (
        _queue_order(cards) if queued else sorted(cards, key=lambda card: card["age"]["entered_at"])
    )
    groups: list[dict[str, Any]] = []
    grouped: dict[str | None, dict[str, Any]] = {}
    for card in ordered:
        parent = card["parent_external_id"]
        # Queue order is stronger than parent nesting. Keep only adjacent cards under
        # one parent there, so A, B, A remains A, B, A when rendered. Other columns
        # retain their established global parent grouping.
        group = (
            None
            if queued and (not groups or groups[-1]["parent_external_id"] != parent)
            else grouped.get(parent)
        )
        if group is None:
            group = {
                "parent_external_id": parent,
                "parent_task_id": card["parent_task_id"],
                "tasks": [],
            }
            groups.append(group)
            grouped[parent] = group
        group["tasks"].append(card)
    return groups


def _kanban(
    uow: UnitOfWork,
    now: datetime,
    *,
    tasks: list[Any],
    contracts: dict[str, dict[str, Any]],
    current_attempt: dict[str, Any],
    wake_by_task: dict[str, Any],
    open_escalations: set[str],
    events: list[Any],
) -> dict[str, Any]:
    entries = _column_entries(events)
    queue_seqs = _queue_seqs(events)
    batches = _approval_batches(events)
    task_by_external_id = {task.external_id: task for task in tasks}
    done_cutoff = now - timedelta(hours=DONE_HOURS)
    budgets: dict[tuple[str, int], int] = {}
    cards: dict[str, list[dict[str, Any]]] = {key: [] for key, _name in KANBAN_COLUMNS}
    for task in tasks:
        column = kanban_column(task.state, has_open_escalation=task.id in open_escalations)
        fallback = (task.closed_at if column == "done" else None) or task.updated_at
        entered_at = column_entered_at(entries.get(task.id, []), column, fallback)
        if column == "done" and entered_at < done_cutoff:
            continue
        fields = contracts.get(task.id, {})
        parent_external_id = fields.get("parent_external_id")
        parent = task_by_external_id.get(str(parent_external_id)) if parent_external_id else None
        attempt = current_attempt.get(task.id)
        budget = _ci_budget_seconds(uow, task, budgets) if column == "ci" else None
        cards[column].append(
            {
                "id": task.id,
                "external_id": task.external_id,
                "title": task.title,
                "state": task.state.value,
                "parent_external_id": parent_external_id,
                "parent_task_id": parent.id if parent else None,
                "holder": kanban_holder(column, task.state, attempt, wake_by_task.get(task.id)),
                "age": kanban_age(column, entered_at, now, ci_budget_seconds=budget),
                "queue": {
                    "seq": queue_seqs.get(task.id) if task.state in QUEUED_STATES else None,
                    "position": None,
                    "batch": batches.get(task.id) if column == "queued" else None,
                },
            }
        )
    return {
        "columns": [
            {
                "key": key,
                "name": name,
                "reserved": key in RESERVED_COLUMNS,
                "note": COLUMN_NOTES.get(key),
                "parents": _kanban_groups(cards[key], queued=key == "queued"),
            }
            for key, name in KANBAN_COLUMNS
        ],
        "thresholds": {"attention_minutes": ATTENTION_MINUTES, "done_hours": DONE_HOURS},
    }


def board_view(uow: UnitOfWork, now: datetime) -> dict[str, Any]:
    tasks = _all_tasks(uow)
    task_by_id = {task.id: task for task in tasks}
    active = [task for task in tasks if task.state not in TASK_TERMINAL]
    attempts = list(uow.attempts.list_in_states(list(AttemptState)))
    attempts.extend(board_imported_attempts(uow, {task.id for task in active}))
    executions = list(uow.executions.list_by_state(state) for state in ExecutionState)
    execution_rows = [row for group in executions for row in group]
    execution_by_id = {row.id: row for row in execution_rows}
    current_attempt = _latest_by(attempts, lambda row: row.task_id, lambda row: row.created_at)
    prs = list(uow.pull_requests.list_in_states(list(PullRequestState)))
    pr_by_task = {pr.task_id: pr for pr in prs}
    cutoff = now - timedelta(days=QUALITY_DAYS)
    recent_prs = [pr for pr in prs if pr.opened_at >= cutoff]
    # hades #334: a card finished in the last day keeps its parent nesting, so the contract
    # read covers recently closed, cancelled and rejected tasks as well as the active ones.
    shown = active + [
        task
        for task in tasks
        if task.state in TASK_TERMINAL
        and (task.closed_at or task.updated_at) >= now - timedelta(hours=DONE_HOURS)
    ]
    contract_documents, comments_by_pr, dispositions = board_batch_records(
        uow,
        {task.id: task.contract_version for task in shown},
        [pr.id for pr in recent_prs],
    )
    contracts = _contract_fields(contract_documents, shown)
    events = _events(uow)
    corrections, failed_gates, latest_ci, latest_ci_decision = _event_maps(events)
    metrics = list(uow.attempt_metrics.list_since(since=None, model=None, task_ids=None))
    metric_by_attempt = {row.attempt_id: row for row in metrics}
    open_escalations = {row.task_id for row in uow.escalations.list_open()}
    wakes = board_latest_unacked_wakes(
        uow,
        {task.id for task in active},
        {task.principal_id for task in active},
    )
    wake_by_task = _latest_by(
        (wake for wake in wakes if wake.task_id),
        lambda row: row.task_id,
        lambda row: row.created_at,
    )
    rows = []
    routing = []
    for task in active:
        attempt = current_attempt.get(task.id)
        execution = execution_by_id.get(attempt.execution_id) if attempt else None
        pr = pr_by_task.get(task.id)
        ci_event = latest_ci.get(task.id)
        ci_state = ci_event.payload.get("state") if ci_event else None
        ci_decision_event = latest_ci_decision.get(task.id)
        ci_cause = None
        if (
            ci_decision_event is not None
            and ci_event is not None
            and ci_decision_event.payload.get("certification_id")
            == ci_event.payload.get("certification_id")
        ):
            # hades #356 correction: a decision from a prior certification must never
            # label the current (possibly re-run) certification's row.
            ci_cause = ci_decision_event.payload.get("cause")
        fields = contracts[task.id]
        row = {
            "id": task.id,
            "external_id": task.external_id,
            "title": task.title,
            "parent_external_id": fields["parent_external_id"],
            "issues": [_issue(issue, fields["repository_url"]) for issue in fields["issues"]],
            "group": waiting_group(task.state, has_open_escalation=task.id in open_escalations),
            "harness": attempt.selected_harness if attempt else None,
            "model": attempt.selected_model if attempt else None,
            "pool": attempt.selected_pool if attempt else None,
            "state": task.state.value,
            "state_since": task.updated_at,
            "waiting_on": waiting_line(wake_by_task.get(task.id), task.state),
            "pull_request": (
                {
                    "number": pr.number,
                    "url": pr.url,
                    "ci": ci_summary(ci_state),
                    "cause": ci_cause,
                    "merge_queue_position": None,
                }
                if pr
                else None
            ),
            "corrections": corrections.get(task.id, 0),
            "eta": eta_bound(attempt, execution.timeout_seconds if execution else None, now),
        }
        rows.append(row)
        if attempt:
            routing.append(
                {"task_id": task.id, "external_id": task.external_id, **_routing(attempt)}
            )
    quality = []
    for pr in prs:
        if pr.opened_at < cutoff:
            continue
        task = task_by_id.get(pr.task_id)
        attempt = current_attempt.get(pr.task_id)
        metric = metric_by_attempt.get(attempt.id) if attempt else None
        outcome = "open"
        if task and task.state in {TaskState.CANCELLED, TaskState.REJECTED}:
            outcome = "cancelled"
        elif pr.state is PullRequestState.MERGED:
            outcome = "merged"
        elif pr.cancelled_at:
            outcome = "cancelled"
        findings: dict[str, dict[str, int]] = {}
        for comment in comments_by_pr.get(pr.id, []):
            if "codex" not in comment["login"].lower():
                continue
            severity = _severity(comment["body"])
            disposition = dispositions.get(comment["id"], "pending")
            bucket = findings.setdefault(severity, {})
            bucket[disposition] = bucket.get(disposition, 0) + 1
        quality.append(
            {
                "task_id": pr.task_id,
                "external_id": task.external_id if task else pr.task_id,
                "harness": (attempt.selected_harness if attempt else None)
                or (metric.harness if metric else "unknown"),
                "model": (attempt.selected_model if attempt else None)
                or (metric.model if metric else "unknown"),
                "failed_gates": failed_gates.get(pr.task_id, []),
                "findings": findings,
                "corrections": corrections.get(pr.task_id, 0),
                "outcome": outcome,
                "submit_to_merge_seconds": (
                    int((pr.merged_at - task.created_at).total_seconds())
                    if pr.merged_at and task
                    else None
                ),
            }
        )
    grouped = [
        {"name": group, "parents": _parents([row for row in rows if row["group"] == group])}
        for group in GROUPS
    ]
    return {
        "generated_at": now,
        "in_flight": grouped,
        "kanban": _kanban(
            uow,
            now,
            tasks=tasks,
            contracts=contracts,
            current_attempt=current_attempt,
            wake_by_task=wake_by_task,
            open_escalations=open_escalations,
            events=events,
        ),
        "routing": routing,
        "tokens": _token_view(metrics),
        "quality": {"days": QUALITY_DAYS, "totals": quality_totals(quality), "tasks": quality},
    }


def _parents(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["parent_external_id"] or "Unparented")].append(row)
    return [
        {"parent_external_id": parent, "tasks": children} for parent, children in grouped.items()
    ]


__all__ = [
    "ATTENTION_MINUTES",
    "COLUMN_BY_STATE",
    "DONE_HOURS",
    "GROUPS",
    "KANBAN_COLUMNS",
    "RESERVED_COLUMNS",
    "age_words",
    "board_view",
    "ci_summary",
    "column_entered_at",
    "eta_bound",
    "kanban_age",
    "kanban_column",
    "kanban_holder",
    "quality_totals",
    "waiting_group",
    "waiting_line",
]
