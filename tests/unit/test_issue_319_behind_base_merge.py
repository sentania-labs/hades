"""Regression proof for Hades #319: every delivery uses the current base."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from crucible.contracts.wake import WakeReason
from crucible.domain.gates import GateName, GateResult, evaluate_gate
from crucible.domain.lifecycle import ExecutionState, TaskState
from tests.unit.test_gates import _ev, _gi, _passing_evidence
from tests.unit.test_issue_360_ready_for_merge_correction import OLD_HEAD, PR_ID, TASK_ID
from tests.unit.test_issue_411_merge_mechanics import (
    MERGED_HEAD,
    _poll,
    _task,
    _wakes,
    _world,
)


def test_behind_ci_head_is_merged_by_publisher_without_worker_or_wake(tmp_path: Path) -> None:
    store, clock, supervisor, client, _publisher = _world(
        tmp_path, state=TaskState.AWAITING_CI_CERTIFICATION
    )
    client.pull().mergeable_state, client.pull().mergeable = "behind", True
    executions_before = list(store.executions.list_for_task(TASK_ID))

    assert _poll(store, clock, supervisor) == 1

    assert client.repo.branches[client.pull().head_branch] == MERGED_HEAD
    assert list(store.executions.list_for_task(TASK_ID)) == executions_before
    assert _wakes(store, WakeReason.PULL_REQUEST_CONFLICTING) == []
    assert _task(store).state is TaskState.AWAITING_CI_CERTIFICATION


def test_behind_conflict_names_files_and_does_not_change_branch(tmp_path: Path) -> None:
    store, clock, supervisor, client, publisher = _world(
        tmp_path, state=TaskState.AWAITING_CI_CERTIFICATION
    )
    client.pull().mergeable_state, client.pull().mergeable = "behind", True
    publisher.merge_conflicts = ["src/service.py", "tests/test_service.py"]

    _poll(store, clock, supervisor)

    assert client.repo.branches[client.pull().head_branch] == OLD_HEAD
    wake = _wakes(store, WakeReason.PULL_REQUEST_CONFLICTING)
    assert len(wake) == 1
    assert "src/service.py, tests/test_service.py" in wake[0].payload["summary"]


def test_scope_uses_only_worker_commit_paths_not_base_reachable_commits() -> None:
    evidence = _passing_evidence()
    bundle = dict(evidence[2].payload)
    # A base commit touched infrastructure/base.yml, but BASE..HEAD did not include it.
    bundle["commit_paths"] = ["src/ledger/change.py", "outside/worker.txt"]
    evidence[2] = _ev("bundle_head", bundle, ident=3)
    evidence[3] = _ev(
        "diff_paths",
        {"paths": ["src/ledger/change.py", "infrastructure/base.yml"]},
        ident=4,
    )
    contract = _gi(evidence).contract
    contract["scope"]["allowed_paths"] = ["src/**"]

    outcome = evaluate_gate(GateName.SCOPE_CONTAINED, _gi(evidence, contract=contract))

    assert outcome.result is GateResult.FAIL
    assert "outside/worker.txt" in outcome.detail
    # The base-only path is present in the diff fixture, but the trusted two-dot commit
    # list excludes it, so it is not charged to the worker.
    assert "infrastructure/base.yml" not in outcome.detail


def test_active_worker_blocks_merge_and_due_deliveries_are_oldest_first(tmp_path: Path) -> None:
    store, clock, supervisor, client, publisher = _world(
        tmp_path, state=TaskState.AWAITING_CI_CERTIFICATION
    )
    client.pull().mergeable_state, client.pull().mergeable = "behind", True
    execution = store.executions.list_for_task(TASK_ID)[-1]
    execution.state = ExecutionState.ACTIVE
    store.executions.save(execution)

    _poll(store, clock, supervisor)
    assert publisher.merges == []
    assert client.repo.branches[client.pull().head_branch] == OLD_HEAD

    # Build a second, newer open delivery in the deliberately insertion-ordered fake
    # store. The coordinator must return the older task first regardless of that order.
    execution.state = ExecutionState.SUCCEEDED
    store.executions.save(execution)
    old = _task(store)
    old.created_at = old.created_at - timedelta(hours=1)
    newer_id, newer_pr, newer_execution, newer_attempt = "task-new", "pr-new", "exec-new", "try-new"
    store.tasks.add(replace(old, id=newer_id, created_at=old.created_at + timedelta(minutes=30)))
    pull = store.pull_requests.get(PR_ID)
    assert pull is not None
    store.pull_requests.add(replace(pull, id=newer_pr, task_id=newer_id, number=pull.number + 1))
    store.executions.add(replace(execution, id=newer_execution, task_id=newer_id))
    attempt = store.attempts.list_for_task(TASK_ID)[-1]
    store.attempts.add(
        replace(attempt, id=newer_attempt, task_id=newer_id, execution_id=newer_execution)
    )

    clock.advance(600)
    plans = supervisor.delivery._due_polls()
    assert [plan.task_id for plan in plans[:2]] == [TASK_ID, newer_id]
