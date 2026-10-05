"""Pre-PR gate evaluation and the task transitions it drives (09, 11).

The evaluators are pure functions in `crucible.domain.gates`. Hades persists their
results, records acceptance when every blocking gate and the worker self-review pass,
and publishes without an orchestrator review or acceptance call.
"""

from __future__ import annotations

import logging
from typing import Any

from crucible.application.acceptance import (
    PUBLISHED_DELIVERABLES,
    deliverable_kinds,
    record_gate_acceptance,
)
from crucible.application.transitions import move_task, record_event
from crucible.application.wakes import create_wake
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    Attempt,
    Execution,
    ExecutionRole,
    GateResultRecord,
    Task,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.gates import (
    DEFERRED_TO_C3,
    ENFORCED_PRE_PR_GATES,
    PRE_PR_GATES,
    EvidenceItem,
    GateInput,
    GateOutcome,
    GateResult,
    PrePrVerdict,
    advisory_gates,
    blocking,
    evaluate_pre_pr,
    for_reviewer,
    pre_pr_verdict,
)
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

log = logging.getLogger("crucible.gates")

PHASE_PRE_PR = "pre_pr"


def internal_review_required(
    policy: dict[str, Any], contract: dict[str, Any], role: ExecutionRole
) -> bool:
    """The report self-review replaced the pre-publication orchestrator review (#402)."""
    return False


def evidence_items(uow: UnitOfWork, attempt_id: str, task_id: str) -> tuple[EvidenceItem, ...]:
    """Attempt evidence plus the task-scoped review evidence the review gate reads."""
    rows = list(uow.evidence.list_for_attempt(attempt_id))
    rows.extend(
        row
        for row in uow.evidence.list_for_task(task_id)
        if row.attempt_id != attempt_id and row.kind == "review_received"
    )
    return tuple(
        EvidenceItem(
            id=int(row.id or 0),
            kind=row.kind,
            source=row.source,
            verified=row.verified,
            payload=row.payload,
            artifact_id=row.artifact_id,
        )
        for row in rows
    )


def gate_input(uow: UnitOfWork, *, task: Task, attempt: Attempt, execution: Execution) -> GateInput:
    stored = uow.contracts.get(task.id, execution.contract_version)
    assert stored is not None
    policy = execution.policy_snapshot or {}
    return GateInput(
        contract=stored.document,
        policy=policy,
        head_sha=task.head_sha,
        evidence=evidence_items(uow, attempt.id, task.id),
        internal_review_required=internal_review_required(policy, stored.document, execution.role),
    )


def configured_pre_pr_gates(policy: dict[str, Any]) -> list[str]:
    """The policy names the required set (05b); with no policy document, every pre-PR gate.

    An explicit empty list is an empty set, which is not the same as no policy at all.
    The enforced gates are added either way: the publisher applies their rule whatever
    the policy says, so the policy cannot drop the early warning (hades FDY-0135)."""
    gates = policy.get("gates", {}).get("pre_pr")
    listed = sorted(PRE_PR_GATES) if gates is None else [str(g) for g in gates]
    return listed + sorted(g for g in ENFORCED_PRE_PR_GATES if g not in listed)


def persist_outcomes(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    attempt: Attempt,
    outcomes: dict[str, GateOutcome],
    advisory: frozenset[str],
) -> None:
    now = clock.now()
    for gate, outcome in outcomes.items():
        uow.gate_results.put(
            GateResultRecord(
                id=new_id(),
                task_id=task.id,
                attempt_id=attempt.id,
                head_sha=task.head_sha or "",
                gate=gate,
                phase=PHASE_PRE_PR,
                result=outcome.result.value,
                detail=outcome.detail,
                evidence_ids=list(outcome.evidence_ids),
                evaluated_at=now,
                blocking=gate not in advisory or outcome.always_blocks,
                findings=list(outcome.findings),
            )
        )


def summarize(outcomes: dict[str, GateOutcome], advisory: frozenset[str]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for outcome in outcomes.values():
        counts[outcome.result.value] = counts.get(outcome.result.value, 0) + 1
    return {
        "counts": counts,
        "results": {gate: outcome.result.value for gate, outcome in outcomes.items()},
        "failing": blocking(outcomes, advisory),
        "advisory": sorted(g for g in outcomes if g in advisory),
        "for_reviewer": for_reviewer(outcomes, advisory),
        "deferred": sorted(g for g in outcomes if g in DEFERRED_TO_C3),
    }


def reviewer_note(items: list[dict[str, str]]) -> str:
    """The wake summary names the gates for the reviewer; their details, which can carry
    paths the worker chose, travel only in the wake's `for_reviewer` (ADR 0024)."""
    if not items:
        return ""
    return " For the reviewer: " + ", ".join(sorted({i["gate"] for i in items})) + "."


def _advisory_review_recorded(uow: UnitOfWork, task: Task) -> bool:
    """Whether an orchestrator has reviewed the current head's advisory findings."""
    return any(
        report.head_sha == (task.head_sha or "")
        for report in uow.review_reports.list_for_task(task.id)
    )


def _unchanged(
    uow: UnitOfWork,
    *,
    task: Task,
    attempt: Attempt,
    outcomes: dict[str, GateOutcome],
    advisory: frozenset[str],
) -> bool:
    """True when the stored rows already say exactly this for this head."""
    stored = {
        row.gate: (row.result, row.detail, row.blocking, list(row.findings))
        for row in uow.gate_results.list_for_attempt(attempt.id)
        if row.head_sha == (task.head_sha or "")
    }
    if not stored:
        return False
    return stored == {
        gate: (
            o.result.value,
            o.detail,
            gate not in advisory or o.always_blocks,
            list(o.findings),
        )
        for gate, o in outcomes.items()
    }


def evaluate_and_advance(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    attempt: Attempt,
    execution: Execution,
) -> dict[str, GateOutcome]:
    """Evaluate every pre-PR gate the policy requires and move the task (09).

    Safe to run again: the gate rows are keyed by (attempt, gate, head) and the task only
    moves when the transition table permits it."""
    gi = gate_input(uow, task=task, attempt=attempt, execution=execution)
    gates = configured_pre_pr_gates(gi.policy)
    advisory = advisory_gates(gi.policy)
    outcomes = evaluate_pre_pr(gates, gi)
    summary = summarize(outcomes, advisory)
    reviewed_advisories = (
        task.state is TaskState.AWAITING_INTERNAL_REVIEW
        and bool(summary["for_reviewer"])
        and _advisory_review_recorded(uow, task)
    )
    if (
        _unchanged(uow, task=task, attempt=attempt, outcomes=outcomes, advisory=advisory)
        and not reviewed_advisories
    ):
        # A task waiting for its internal review is re-evaluated on every tick; writing
        # the same answer again would make reconciliation not idempotent (10).
        return outcomes
    persist_outcomes(uow, clock, task=task, attempt=attempt, outcomes=outcomes, advisory=advisory)
    record_event(
        uow,
        clock,
        EventKind.GATES_EVALUATED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        execution_id=execution.id,
        attempt_id=attempt.id,
        payload={"head_sha": task.head_sha, "phase": PHASE_PRE_PR, **summary},
    )
    failing = summary["failing"]
    verdict = pre_pr_verdict(outcomes, advisory)
    if (
        task.state is not TaskState.REPORTED
        and task.state is not TaskState.AWAITING_INTERNAL_REVIEW
    ):
        return outcomes
    if verdict is PrePrVerdict.FAILED:
        published = uow.events.latest_for_task_kind(task.id, EventKind.PUBLISH_COMPLETED.value)
        published_head = (
            str(published.payload.get("head_sha"))
            if published is not None and published.payload.get("head_sha")
            else "the remote branch head"
        )
        move_task(
            uow,
            clock,
            task,
            TaskState.PRE_PR_GATES_FAILED,
            EventKind.TASK_PRE_PR_GATES_FAILED,
            execution_id=execution.id,
            attempt_id=attempt.id,
            payload={"head_sha": task.head_sha, "failing": failing},
        )
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.PRE_PR_GATES_FAILED,
            summary=(
                f"pre-PR gates failed on {task.head_sha}: {', '.join(failing)}. "
                + (
                    "The failed bundle is unsafe because no_secrets failed; the next "
                    f"correction will start from the published head {published_head}."
                    if outcomes.get("no_secrets") is not None
                    and outcomes["no_secrets"].result is GateResult.FAIL
                    else f"The next pre_pr_gates correction defaults to last_attempt at "
                    f"{task.head_sha}, subject to bundle seal, secret scan, and ancestry "
                    "verification; remote_branch is an explicit alternative."
                )
                + reviewer_note(summary["for_reviewer"])
            ),
            task=task,
            attempt_id=attempt.id,
            extra_links={"gates": f"/v1/attempts/{attempt.id}/gates"},
            for_reviewer=summary["for_reviewer"],
        )
        return outcomes
    if summary["for_reviewer"] and not reviewed_advisories:
        if task.state is TaskState.REPORTED:
            move_task(
                uow,
                clock,
                task,
                TaskState.AWAITING_INTERNAL_REVIEW,
                EventKind.TASK_AWAITING_INTERNAL_REVIEW,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "head_sha": task.head_sha,
                    "executor": "orchestrator",
                    "reason": "advisory_gate_failure",
                },
            )
            create_wake(
                uow,
                clock,
                principal_id=task.principal_id,
                reason=WakeReason.INTERNAL_REVIEW_NEEDED,
                summary=(
                    f"the blocking gates pass on {task.head_sha}; "
                    "an orchestrator review is required for advisory gate failures."
                    + reviewer_note(summary["for_reviewer"])
                ),
                task=task,
                attempt_id=attempt.id,
                extra_links={
                    "review": f"/v1/tasks/{task.id}/review",
                    "gates": f"/v1/attempts/{attempt.id}/gates",
                },
                for_reviewer=summary["for_reviewer"],
            )
        return outcomes
    move_task(
        uow,
        clock,
        task,
        TaskState.GATES_PASSED,
        EventKind.TASK_GATES_PASSED,
        execution_id=execution.id,
        attempt_id=attempt.id,
        payload={"head_sha": task.head_sha, "results": summary["results"]},
    )
    acceptance = record_gate_acceptance(uow, clock, task=task)
    kinds = deliverable_kinds(uow, task)
    destination = (
        TaskState.PUBLISHING if PUBLISHED_DELIVERABLES & set(kinds) else TaskState.ACCEPTED
    )
    event = (
        EventKind.TASK_PUBLISHING
        if destination is TaskState.PUBLISHING
        else EventKind.TASK_ACCEPTED
    )
    move_task(
        uow,
        clock,
        task,
        destination,
        event,
        execution_id=execution.id,
        attempt_id=attempt.id,
        payload={
            "head_sha": task.head_sha,
            "acceptance_id": acceptance.id,
            "deliverables": kinds,
            "automatic": True,
        },
    )
    if destination is TaskState.ACCEPTED:
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.ACCEPTED,
            summary=("accepted, artifacts are ready; no branch or PR publication was requested."),
            task=task,
            attempt_id=attempt.id,
            extra_links={"artifacts": f"/v1/attempts/{attempt.id}/artifacts"},
        )
    return outcomes


def counts_for_metrics(outcomes: dict[str, GateOutcome]) -> tuple[int, int]:
    # Counts every failure, advisory ones included: the metric is how often gates fail.
    passed = sum(1 for o in outcomes.values() if o.result is GateResult.PASS)
    failed = sum(1 for o in outcomes.values() if o.result in (GateResult.FAIL, GateResult.ERROR))
    return passed, failed
