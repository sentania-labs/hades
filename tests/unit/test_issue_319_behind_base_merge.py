"""Regression proof for Hades #319: every delivery uses the current base."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crucible.application.delivery_tick import MergePlan, PollPlan
from crucible.contracts.task_contract import contract_sha256
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import ExecutionRole, TaskContract
from crucible.domain.gates import GateName, GateResult, evaluate_gate
from crucible.domain.lifecycle import ExecutionState, TaskState
from tests.unit.test_gates import _ev, _gi, _passing_evidence
from tests.unit.test_issue_360_ready_for_merge_correction import (
    NOW,
    OLD_HEAD,
    PR_ID,
    PR_NUMBER,
    TASK_ID,
)
from tests.unit.test_issue_411_merge_mechanics import (
    MERGED_HEAD,
    REPOSITORY,
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


# ----- PR 533 review: a correction that has not launched, and its later states ----------


def _scheduled_correction(store: Any, *, resume_from: str = "remote_branch") -> None:
    """A correction attached to the published task and not yet launched: its accepted
    head was cleared on attach, and its execution has no attempt."""
    task = _task(store)
    prior = store.contracts.get(TASK_ID, 1)
    document = copy.deepcopy(prior.document)
    document["correction"] = {
        "of_version": 1,
        "reason": "external_review",
        "addresses": [],
        "instructions": "Address the review.",
        "resume_from": resume_from,
        "request_internal_review": False,
    }
    store.contracts.add(
        TaskContract(
            id="01CONTRACT319000000000002",
            task_id=TASK_ID,
            version=2,
            document=document,
            sha256=contract_sha256(document),
            submitted_at=NOW,
        )
    )
    task.contract_version = 2
    task.head_sha = None
    task.state = TaskState.SCHEDULED
    store.executions.add(
        replace(
            store.executions.list_for_task(TASK_ID)[-1],
            id="01EXEC319000000000000002",
            role=ExecutionRole.CORRECT,
            contract_version=2,
            state=ExecutionState.CREATED,
            created_at=NOW + timedelta(minutes=1),
        )
    )


def test_a_correction_not_yet_launched_starts_on_the_merged_base(tmp_path: Path) -> None:
    store, clock, supervisor, client, publisher = _world(tmp_path)
    client.pull().mergeable_state, client.pull().mergeable = "behind", True
    _scheduled_correction(store)

    _poll(store, clock, supervisor)

    # The worker will clone this tip, so it starts on the current base.
    assert [m.expected_head for m in publisher.merges] == [OLD_HEAD]
    assert client.repo.branches[client.pull().head_branch] == MERGED_HEAD
    task = _task(store)
    assert task.state is TaskState.SCHEDULED
    # It has no accepted head until it is collected.
    assert task.head_sha is None
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None and pull_request.head_sha == MERGED_HEAD
    assert _wakes(store, WakeReason.PULL_REQUEST_CONFLICTING) == []


def test_a_correction_resuming_its_last_attempt_is_not_merged_into(tmp_path: Path) -> None:
    store, clock, supervisor, client, publisher = _world(tmp_path)
    client.pull().mergeable_state, client.pull().mergeable = "behind", True
    _scheduled_correction(store, resume_from="last_attempt")

    _poll(store, clock, supervisor)

    assert publisher.merges == []
    assert client.repo.branches[client.pull().head_branch] == OLD_HEAD


def test_a_collected_correction_is_merged_after_it_publishes_not_before(
    tmp_path: Path,
) -> None:
    # Its collected head replaces Crucible's own tip at publication (issue 403), so a
    # merge pushed now would be discarded; the poll after publication merges instead.
    store, clock, supervisor, client, publisher = _world(tmp_path)
    client.pull().mergeable_state, client.pull().mergeable = "behind", True
    _scheduled_correction(store)
    store.executions.list_for_task(TASK_ID)[-1].state = ExecutionState.SUCCEEDED
    _task(store).state = TaskState.REPORTED

    _poll(store, clock, supervisor)

    assert publisher.merges == []
    assert client.repo.branches[client.pull().head_branch] == OLD_HEAD


# ----- PR 533 review: polls and merges share one oldest-first order ----------------------


def _poll_plan(task_id: str, created_at: datetime) -> PollPlan:
    return PollPlan(
        task_id=task_id,
        pull_request_id=f"pr-{task_id}",
        number=1,
        repository_name="org/repo",
        installation_id=7,
        base_ref="main",
        attempt_id=f"attempt-{task_id}",
        with_reactions=False,
        created_at=created_at,
    )


def _merge_plan(task_id: str, created_at: datetime) -> MergePlan:
    return MergePlan(
        task_id=task_id,
        pull_request_id=f"pr-{task_id}",
        number=1,
        repository_name="org/repo",
        installation_id=7,
        certified_head_sha=OLD_HEAD,
        base_ref="main",
        created_at=created_at,
    )


def _recording_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    polls: list[PollPlan],
    merges: list[MergePlan],
    *,
    limit_after: int,
) -> list[tuple[str, str]]:
    """Run one tick whose GitHub work is recorded rather than done, rate limited once
    `limit_after` steps have run."""
    _store, _clock, supervisor, _client, _publisher = _world(tmp_path)
    delivery = supervisor.delivery
    calls: list[tuple[str, str]] = []

    async def observe_one(plan: PollPlan) -> bool:
        calls.append(("poll", plan.task_id))
        return True

    async def merge_one(plan: MergePlan) -> bool:
        calls.append(("merge", plan.task_id))
        merges.remove(plan)  # merged: no longer ready
        return True

    def ready(only_task_id: str | None = None) -> list[MergePlan]:
        return [m for m in merges if only_task_id in (None, m.task_id)]

    monkeypatch.setattr(delivery, "_due_polls", lambda: list(polls))
    monkeypatch.setattr(delivery, "_ready_merges", ready)
    monkeypatch.setattr(delivery, "_evaluate_gates", lambda: None)
    monkeypatch.setattr(delivery, "_observe_one", observe_one)
    monkeypatch.setattr(delivery, "_merge_one", merge_one)
    monkeypatch.setattr(delivery, "_rate_limited", lambda: len(calls) >= limit_after)
    asyncio.run(delivery.observe())
    return calls


def test_an_older_ready_merge_goes_before_a_newer_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older, newer = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    calls = _recording_tick(
        tmp_path,
        monkeypatch,
        [_poll_plan("newer", newer)],
        [_merge_plan("older", older)],
        limit_after=1,
    )
    # The newer poll meets the rate limit; the older delivery's merge already ran.
    assert calls == [("merge", "older")]


def test_a_task_due_a_poll_is_polled_before_its_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older, newer = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    calls = _recording_tick(
        tmp_path,
        monkeypatch,
        [_poll_plan("newer", newer), _poll_plan("older", older)],
        [_merge_plan("older", older)],
        limit_after=10,
    )
    assert calls == [("poll", "older"), ("merge", "older"), ("poll", "newer")]


def test_a_head_github_reports_behind_at_merge_time_is_not_merged(tmp_path: Path) -> None:
    store, _clock, supervisor, client, _publisher = _world(tmp_path)
    client.pull().mergeable_state, client.pull().mergeable = "behind", True

    plan = replace(
        _merge_plan(TASK_ID, NOW),
        pull_request_id=PR_ID,
        number=PR_NUMBER,
        repository_name=REPOSITORY,
    )
    merged = asyncio.run(supervisor.delivery._merge_one(plan))

    assert merged is False
    assert client.merged_numbers == []
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    # Not a refusal: the next poll merges the base in silently.
    assert pull_request.mergeable_state == "behind"
    assert pull_request.merge_refusal_cause is None
    assert pull_request.last_polled_at is None
    assert store.wakes.rows == []
