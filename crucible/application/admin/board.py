"""Read-only operator board assembled from bounded repository scans."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from crucible.application.queries import (
    board_batch_records,
    board_imported_attempts,
    board_latest_unacked_wakes,
)
from crucible.domain.entities import PullRequestState
from crucible.domain.lifecycle import TASK_TERMINAL, AttemptState, ExecutionState, TaskState
from crucible.ports.repository import UnitOfWork

QUALITY_DAYS = 14

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
    contract_documents, comments_by_pr, dispositions = board_batch_records(
        uow,
        {task.id: task.contract_version for task in active},
        [pr.id for pr in recent_prs],
    )
    contracts = _contract_fields(contract_documents, active)
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
    "GROUPS",
    "board_view",
    "ci_summary",
    "eta_bound",
    "quality_totals",
    "waiting_group",
    "waiting_line",
]
