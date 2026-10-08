"""Regression tests for correction routing and route-event accuracy (hades #539)."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crucible.application.supervisor import Supervisor, _Pending
from crucible.domain.entities import Attempt, Execution, ExecutionRole, Task
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from tests.fixtures import FakeClock, contract_document
from tests.unit.test_class_routing import NOW, _model, _routing


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    correction: bool,
    busy_harnesses: set[str] | None = None,
    excluded_pools: set[str] | None = None,
) -> tuple[Supervisor, _Pending, Any]:
    routing = _routing(
        [
            _model("frontier-a", harness="codex", capability="frontier", pool="frontier-a"),
            _model("frontier-b", harness="agy", capability="frontier", pool="frontier-b"),
            _model("hermes", harness="hermes", capability="mid", pool="lab-local"),
        ]
    )
    routing.tiers["complex"] = routing.tiers["standard"].model_copy(
        update={
            "allowed_capability": ["frontier", "mid"],
            "prefer": ["frontier", "mid"],
            "prefer_pools": ["frontier-a", "frontier-b"],
        }
    )
    task = Task(
        "task-539",
        "FDY-0507",
        "foundry",
        "project",
        "Correction routing",
        TaskState.SCHEDULED,
        2 if correction else 1,
        "policy",
        1,
        "repo",
        NOW,
        NOW,
    )
    policy = {"routing": {"policy": {"name": routing.name, "version": routing.version}}}
    execution = Execution(
        "execution-current",
        task.id,
        ExecutionRole.CORRECT if correction else ExecutionRole.IMPLEMENT,
        task.contract_version,
        "hermes" if correction else "unselected",
        "hermes" if correction else "unselected",
        None,
        "fake",
        "unselected",
        policy,
        ExecutionState.CREATED,
        1,
        [],
        60,
        NOW,
    )
    attempt = Attempt(
        "attempt-current",
        execution.id,
        task.id,
        1,
        AttemptState.PENDING,
        NOW,
        routing_excluded_pools=sorted(excluded_pools or set()),
    )
    contract = contract_document()
    contract["execution_request"]["tier"] = "complex"
    if correction:
        contract["correction"] = {
            "of_version": 1,
            "reason": "needs_more_work",
            "addresses": [],
            "instructions": "Correct the failed attempt",
            "request_internal_review": False,
            "resume_from": "last_attempt",
        }

    uow: Any = MagicMock()
    uow.tasks.get.return_value = task
    uow.executions.get.return_value = execution
    uow.attempts.get.return_value = attempt
    uow.attempts.list_in_states.return_value = []
    uow.attempt_metrics.list_since.return_value = []
    uow.attempt_metrics.recent_for_project.return_value = []
    uow.pool_exhaustions.get.return_value = None
    uow.harness_images.get.return_value = None
    uow.provider_settings.get.return_value = None
    uow.events.latest_for_task_kind.return_value = None
    uow.routing_policies.get.return_value = SimpleNamespace(
        document=routing.model_dump(mode="json")
    )
    uow.routing_policies.list_versions.return_value = []

    supervisor = object.__new__(Supervisor)
    supervisor._clock = FakeClock(NOW)
    supervisor._providers = {}
    supervisor._capacity_now = {}
    supervisor._harnesses = None
    supervisor._logins_now = frozenset()
    monkeypatch.setattr(supervisor, "_fenced", lambda: nullcontext(uow))
    monkeypatch.setattr(supervisor, "_uow_factory", lambda: nullcontext(uow), raising=False)
    monkeypatch.setattr(supervisor, "_eligible_harnesses", lambda **_: None)
    monkeypatch.setattr(supervisor, "_logins_in_progress", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(supervisor, "_provider", MagicMock())
    monkeypatch.setattr(supervisor, "_harness_gate", lambda _: None)
    monkeypatch.setattr(supervisor, "_checkout_lease_free", lambda *_: True)
    monkeypatch.setattr(supervisor, "_take_checkout_lease", MagicMock(return_value=True))
    monkeypatch.setattr(supervisor, "_release_attempt_checkout", MagicMock())
    busy = busy_harnesses or set()
    monkeypatch.setattr(
        supervisor,
        "_harness_busy_in_uow",
        lambda _uow, row, _version: (
            f"1 of 1 {row.harness} worker(s) already running" if row.harness in busy else None
        ),
    )
    return (
        supervisor,
        _Pending(task=task, execution=execution, attempt=attempt, contract=contract),
        uow,
    )


def _routed_event(uow: Any) -> Any:
    return next(
        call.args[0]
        for call in uow.events.append.call_args_list
        if call.args[0].kind == EventKind.ATTEMPT_ROUTED.value
    )


async def test_local_attempt_bundle_does_not_pin_complex_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow = _setup(monkeypatch, correction=True)
    previous_execution = replace(
        pending.execution,
        id="execution-previous",
        role=ExecutionRole.IMPLEMENT,
        contract_version=1,
        harness="hermes",
        model="hermes",
    )
    previous_attempt = replace(
        pending.attempt,
        id="000-attempt-previous",
        execution_id=previous_execution.id,
        workspace_path="/workspaces/FDY-0507",
        selected_harness="hermes",
        selected_model="hermes",
        selected_pool="lab-local",
    )

    routed = supervisor._route_pending(pending)

    assert routed is not None
    assert (
        routed.attempt.selected_model,
        routed.attempt.selected_harness,
        routed.attempt.selected_pool,
    ) == ("frontier-a", "codex", "frontier-a")

    uow.executions.list_for_task.return_value = [previous_execution, routed.execution]
    uow.attempts.list_for_execution.side_effect = lambda execution_id: (
        [previous_attempt] if execution_id == previous_execution.id else [routed.attempt]
    )
    uow.evidence.list_for_attempt.side_effect = lambda attempt_id: (
        [
            SimpleNamespace(
                kind="bundle_head",
                verified=True,
                payload={
                    "bundle_verified": True,
                    "head_sha": "local-head",
                    "bundle_sha256": "sealed-sha256",
                },
            )
        ]
        if attempt_id == previous_attempt.id
        else []
    )
    uow.gate_results.list_for_attempt.return_value = []
    spec = await supervisor._build_spec(
        routed.attempt, routed.execution, routed.task, routed.contract
    )
    assert spec.resume_bundle_path == "/workspaces/FDY-0507/output/work_branch.bundle"
    assert spec.resume_bundle_head == "local-head"


def test_fresh_and_last_attempt_resume_choose_same_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh_supervisor, fresh, _ = _setup(monkeypatch, correction=False)
    resume_supervisor, resume, _ = _setup(monkeypatch, correction=True)
    fresh_result = fresh_supervisor._route_pending(fresh)
    resume_result = resume_supervisor._route_pending(resume)
    assert fresh_result is not None and resume_result is not None
    assert (
        fresh_result.attempt.selected_model,
        fresh_result.attempt.selected_harness,
        fresh_result.attempt.selected_pool,
    ) == (
        resume_result.attempt.selected_model,
        resume_result.attempt.selected_harness,
        resume_result.attempt.selected_pool,
    )


def test_busy_and_excluded_frontiers_record_each_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow = _setup(
        monkeypatch,
        correction=True,
        busy_harnesses={"codex"},
        excluded_pools={"frontier-b"},
    )
    result = supervisor._route_pending(pending)
    assert result is not None
    assert result.attempt.selected_pool == "lab-local"
    candidates = {row["pool"]: row for row in _routed_event(uow).payload["ordered_candidates"]}
    assert candidates["frontier-a"]["eligible"] is False
    assert candidates["frontier-a"]["busy"] == "1 of 1 codex worker(s) already running"
    assert candidates["frontier-a"]["excluded"] == ["1 of 1 codex worker(s) already running"]
    assert candidates["frontier-b"]["eligible"] is False
    assert candidates["frontier-b"]["excluded"] == ["pool excluded for the current quota reroute"]
