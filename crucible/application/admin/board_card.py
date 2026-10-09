"""The opened board card (hades #489): one task, in words, with what it is stuck on.

A projection over the task, its contract versions, executions and attempts, the pull
request and its CI certifications, the gate results, the escalations and the operator
notes. Nothing here writes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from crucible.application.admin.board import age_words, ci_summary
from crucible.application.admin.board_lanes import (
    LANES,
    first_sentence,
    issue_link,
)
from crucible.application.board_actions import (
    card_lane,
    default_move,
    default_move_table,
    moves_for_card,
    open_escalation,
)
from crucible.application.errors import NotFoundError
from crucible.application.queries import gate_summary
from crucible.application.task_notes import list_notes, note_view
from crucible.domain.entities import Attempt, Execution, ExecutionRole, Task
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.ports.repository import UnitOfWork

LANE_NAMES = {key: name for key, name, _meaning in LANES}
STATE_WORDS = {
    TaskState.PROPOSED: "proposed and waiting for the operator",
    TaskState.SENT_BACK: "sent back to the orchestrator",
    TaskState.BLOCKED: "blocked on a question",
    TaskState.PRE_PR_GATES_FAILED: "stopped on a failed gate before the pull request",
    TaskState.PUBLISH_FAILED: "stopped because publishing failed",
    TaskState.CI_CERTIFICATION_FAILED: "stopped because CI did not certify the head",
    TaskState.HEAD_DIVERGED: "stopped because the branch changed outside Hades",
    TaskState.AWAITING_INTERNAL_REVIEW: "waiting for the internal review",
    TaskState.AWAITING_ACCEPTANCE: "waiting for acceptance",
    TaskState.AWAITING_EXTERNAL_REVIEW: "waiting for the external review",
    TaskState.AWAITING_CI_CERTIFICATION: "waiting for CI",
    TaskState.READY_FOR_MERGE: "ready to merge",
}


def state_words(state: TaskState) -> str:
    return STATE_WORDS.get(state, state.value.replace("_", " "))


def _items(values: Any) -> list[Any]:
    return list(values) if isinstance(values, list | tuple) else []


def _paths(values: Any) -> list[str]:
    return [str(value) for value in _items(values)]


def _check_words(verification: dict[str, Any]) -> str:
    if str(verification.get("kind", "command")) == "command":
        expect = verification.get("expect_exit", 0)
        suffix = f" (expect exit {expect})" if expect not in (0, None) else ""
        return f"{verification.get('command')}{suffix}"
    return f"write {verification.get('path')}"


def contract_text(document: dict[str, Any]) -> dict[str, Any]:
    """The contract as text: objective, scope, criteria, checks, tier and deliverable."""
    scope = document.get("scope") or {}
    execution = document.get("execution_request") or document.get("execution") or {}
    repository = document.get("repository") or {}
    constraints = document.get("constraints") or {}
    deliverables = [d for d in _items(document.get("deliverables")) if isinstance(d, dict)]
    deliverable_words = [
        f"{d.get('kind', 'deliverable')}"
        + (f" to {d['target']}" if d.get("target") else "")
        + (" (draft)" if d.get("draft") else "")
        for d in deliverables
    ]
    issues = [str(issue) for d in deliverables for issue in _items(d.get("closes"))]
    issues += [
        str(c.get("ref"))
        for c in _items(document.get("context"))
        if isinstance(c, dict) and str(c.get("kind", "")) == "issue" and c.get("ref")
    ]
    repository_url = repository.get("url")
    return {
        "title": str(document.get("title") or ""),
        "objective": str(document.get("objective") or "").strip(),
        "scope": {
            "allowed_paths": _paths(scope.get("allowed_paths")),
            "prohibited_paths": _paths(scope.get("prohibited_paths")),
            "may_add_dependencies": bool(scope.get("may_add_dependencies")),
            "may_modify_ci": bool(scope.get("may_modify_ci")),
            "network": str(constraints.get("network") or "policy"),
            "prohibited_actions": [str(a) for a in _items(constraints.get("prohibited_actions"))],
        },
        "acceptance_criteria": [
            {"id": str(c.get("id", "")), "text": str(c.get("text", ""))}
            for c in _items(document.get("acceptance_criteria"))
            if isinstance(c, dict)
        ],
        "checks": [
            _check_words(v)
            for v in _items(document.get("required_verification"))
            if isinstance(v, dict)
        ],
        "tier": str(execution.get("tier") or "not recorded"),
        "rationale": str(execution.get("rationale") or ""),
        "deliverables": deliverable_words or ["not recorded"],
        "issues": [issue_link(issue, repository_url) for issue in dict.fromkeys(issues)],
        "repository": {
            "name": str(repository.get("name") or ""),
            "url": repository_url or "",
            "base_ref": str(repository.get("base_ref") or "main"),
            "work_branch": str(repository.get("work_branch") or ""),
        },
    }


def _exit_words(attempt: Attempt) -> str:
    if attempt.exit_class is not None:
        return attempt.exit_class.value.replace("_", " ")
    if attempt.state in {AttemptState.PENDING, AttemptState.PREPARING, AttemptState.LAUNCHING}:
        return "not started"
    return attempt.state.value


def attempt_line(attempt: Attempt) -> str:
    """One line on what the attempt did or why it ended."""
    if attempt.blocked_statement:
        reason = f" ({attempt.blocked_reason})" if attempt.blocked_reason else ""
        return f"Blocked{reason}: {first_sentence(attempt.blocked_statement)}"
    if attempt.termination_detail:
        return first_sentence(attempt.termination_detail)
    if attempt.termination_reason:
        return attempt.termination_reason.replace("_", " ")
    if attempt.state is AttemptState.RUNNING:
        return "Running."
    if attempt.state in {AttemptState.PENDING, AttemptState.PREPARING, AttemptState.LAUNCHING}:
        return "Not started yet."
    if attempt.exit_class is not None:
        return f"Ended {attempt.exit_class.value.replace('_', ' ')}."
    return attempt.state.value.replace("_", " ").capitalize() + "."


def _timeline(
    executions: list[Execution], attempts_by_execution: dict[str, list[Attempt]]
) -> list[dict[str, Any]]:
    rows: list[tuple[tuple[datetime, str], dict[str, Any]]] = []
    for execution in executions:
        for attempt in attempts_by_execution.get(execution.id, []):
            key = (attempt.started_at or attempt.created_at, attempt.id)
            rows.append(
                (
                    key,
                    {
                        "attempt_id": attempt.id,
                        "role": execution.role.value,
                        "number": attempt.number,
                        "started_at": attempt.started_at,
                        "ended_at": attempt.ended_at,
                        "harness": attempt.selected_harness or execution.harness,
                        "model": attempt.selected_model or execution.model,
                        "state": attempt.state.value,
                        "exit_class": _exit_words(attempt),
                        "line": attempt_line(attempt),
                    },
                )
            )
    rows.sort(key=lambda row: row[0])
    return [row for _key, row in rows]


def _corrections(versions: list[Any]) -> list[dict[str, Any]]:
    history = []
    for stored in sorted(versions, key=lambda v: v.version):
        correction = (stored.document or {}).get("correction") or {}
        if not correction:
            continue
        history.append(
            {
                "version": stored.version,
                "of_version": correction.get("of_version"),
                "submitted_at": stored.submitted_at,
                "reason": str(correction.get("reason") or ""),
                "resume_from": str(correction.get("resume_from") or "remote_branch"),
                "instructions": str(correction.get("instructions") or ""),
                "addresses": len(_items(correction.get("addresses"))),
            }
        )
    return history


def _cost(
    executions: list[Execution],
    attempts_by_execution: dict[str, list[Attempt]],
    codex_rounds: int,
    now: datetime,
) -> dict[str, Any]:
    work = [
        attempt
        for execution in executions
        if execution.role is not ExecutionRole.REVIEW
        for attempt in attempts_by_execution.get(execution.id, [])
    ]
    seconds = 0
    for attempt in work:
        if attempt.started_at is None:
            continue
        ended = attempt.ended_at or now
        seconds += max(0, int((ended - attempt.started_at).total_seconds()))
    return {
        "attempts": len(work),
        "worker_minutes": seconds // 60,
        "codex_rounds": codex_rounds,
    }


def _stuck_block(
    task: Task,
    lane: str,
    latest: Attempt | None,
    latest_execution: Execution | None,
    gates: dict[str, Any],
    escalation: Any | None,
    ci: dict[str, Any] | None,
    uow: UnitOfWork,
    now: datetime,
) -> dict[str, Any]:
    """Where the task is and what it is stuck on, in plain words."""
    since = age_words(max(0, int((now - task.updated_at).total_seconds())))
    lines = [f"In the {LANE_NAMES[lane]} lane: {state_words(task.state)}, for {since}."]
    headline = state_words(task.state).capitalize() + "."
    if latest is not None:
        harness = latest_execution.harness if latest_execution else "worker"
        who = latest.selected_harness or harness
        model = latest.selected_model or (latest_execution.model if latest_execution else None)
        who = f"{who} / {model}" if model else who
        role = latest_execution.role.value if latest_execution else "implement"
        lines.append(
            f"Latest attempt: {role} attempt {latest.number} on {who}, "
            f"{_exit_words(latest)}. {attempt_line(latest)}"
        )
    failing = list(gates.get("failing") or [])
    if failing:
        details = gates.get("details") or {}
        lines.append(
            "Failing gates: "
            + "; ".join(
                f"{gate}: {details.get(gate)}" if details.get(gate) else str(gate)
                for gate in failing
            )
            + "."
        )
        headline = f"Failing gates: {', '.join(failing)}."
    elif gates.get("results"):
        lines.append(f"Gates: {len(gates['results'])} evaluated on this head, none failing.")
    else:
        lines.append("Gates: none evaluated on this head yet.")
    if task.state is TaskState.CI_CERTIFICATION_FAILED and ci is not None:
        lines.append(f"CI: {ci['detail'] or ci['state']}.")
        headline = f"CI did not certify: {first_sentence(ci['detail'] or ci['state'])}"
    if task.state is TaskState.PUBLISH_FAILED:
        event = uow.events.latest_for_task_kind(task.id, EventKind.TASK_PUBLISH_FAILED.value)
        reason = (event.payload if event else {}).get("reason") or "reason not recorded"
        lines.append(f"Publishing failed: {reason}.")
        headline = f"Publishing failed: {first_sentence(str(reason))}"
    if escalation is not None:
        question = first_sentence(str(escalation.question))
        reason = f" ({escalation.reason})" if getattr(escalation, "reason", None) else ""
        lines.append(f"Open escalation{reason}: {question}")
        headline = question
    return {"headline": headline, "lines": lines}


def board_card_view(uow: UnitOfWork, task_id: str, now: datetime) -> dict[str, Any]:
    task = uow.tasks.get(task_id)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    versions = list(uow.contracts.list_for_task(task.id))
    current = next((v for v in versions if v.version == task.contract_version), None)
    document = dict(current.document) if current else {}
    repository = uow.repositories.get(task.repository_id)
    if repository is not None and "repository" in document:
        document["repository"] = {**document["repository"], "url": repository.url}
    executions = sorted(uow.executions.list_for_task(task.id), key=lambda e: e.created_at)
    attempts_by_execution = {
        execution.id: sorted(uow.attempts.list_for_execution(execution.id), key=lambda a: a.id)
        for execution in executions
    }
    work_attempts = [
        (attempt, execution)
        for execution in executions
        if execution.role is not ExecutionRole.REVIEW
        for attempt in attempts_by_execution[execution.id]
    ]
    latest, latest_execution = max(work_attempts, key=lambda pair: pair[0].id, default=(None, None))
    pull_request = uow.pull_requests.get_for_task(task.id)
    cycles = list(uow.review_cycles.list_for_pull_request(pull_request.id)) if pull_request else []
    certifications = list(uow.ci_certifications.list_for_task(task.id))
    certification = certifications[-1] if certifications else None
    ci = (
        {
            "state": certification.state,
            "detail": certification.detail,
            "head_sha": certification.head_sha,
            "evaluated_at": certification.evaluated_at,
            **ci_summary(certification.state),
        }
        if certification
        else None
    )
    gates = gate_summary(uow, task.id)
    escalation = open_escalation(uow, task.id)
    lane = card_lane(task, escalation)
    notes = list_notes(uow, task.id)
    moves = moves_for_card(task, escalation=escalation, pull_request=pull_request)
    default = default_move(lane)
    return {
        "id": task.id,
        "external_id": task.external_id,
        "title": task.title,
        "project": task.project,
        "state": task.state.value,
        "state_words": state_words(task.state),
        "lane": {"key": lane, "name": LANE_NAMES[lane]},
        "updated_at": task.updated_at,
        "contract": contract_text(document),
        "contract_version": task.contract_version,
        "pull_request": (
            {
                "number": pull_request.number,
                "url": pull_request.url,
                "state": pull_request.state.value,
                "head_sha": pull_request.head_sha,
                "work_branch": pull_request.work_branch,
                "merged_sha": pull_request.merge_sha,
                "merged_by": pull_request.merged_by,
                "merged_at": pull_request.merged_at,
            }
            if pull_request
            else None
        ),
        "head": {"sha": task.head_sha, "ci": ci},
        "stuck": _stuck_block(
            task, lane, latest, latest_execution, gates, escalation, ci, uow, now
        ),
        "gates": gates,
        "escalation": (
            {
                "id": escalation.id,
                "question": first_sentence(str(escalation.question)),
                "full_question": str(escalation.question),
                "reason": getattr(escalation, "reason", None),
                "opened_at": escalation.opened_at,
            }
            if escalation
            else None
        ),
        "timeline": _timeline(executions, attempts_by_execution),
        "corrections": _corrections(versions),
        "cost": _cost(
            executions,
            attempts_by_execution,
            sum(1 for cycle in cycles if cycle.state == "completed"),
            now,
        ),
        "notes": [note_view(note) for note in notes],
        "moves": moves,
        "default_move": default if default in {m["key"] for m in moves} else None,
        "default_move_label": next((m["label"] for m in moves if m["key"] == default), None),
        "default_table": default_move_table(),
        "links": {"task": f"/ui/tasks/{task.id}", "board": "/ui/board"},
    }


__all__ = ["attempt_line", "board_card_view", "contract_text", "state_words"]
