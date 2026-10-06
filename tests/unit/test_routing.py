"""A local route requires executable verification specific to the task."""

from contextlib import nullcontext
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crucible.adapters.ui.pages.tasks import _busy_fallthrough
from crucible.application.queries import task_view
from crucible.application.routing import Selection, select_model
from crucible.application.submit_task import _check_routing
from crucible.application.supervisor import Supervisor, _Pending
from crucible.contracts.task_contract import TaskContractV1
from crucible.domain.entities import Attempt, Execution, ExecutionRole, Policy, Task
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from tests.fixtures import FakeClock, contract_document
from tests.unit.test_class_routing import NOW, _model, _routing, _uow


def _select(*, specific: bool = False, frontier: bool = True, pinned: bool = False) -> Selection:
    models = [
        {
            **_model("hermes", harness="hermes", pool="lab-local"),
            "endpoint": "local",
            "endpoint_url": "http://spark:4000/v1",
        },
        {
            **_model("codex-local", pool="lab-local"),
            "endpoint": "local",
            "endpoint_url": "http://spark:4000/v1",
        },
        _model("pool-peer", pool="lab-local"),
    ]
    if frontier:
        models.append(_model("frontier", capability="frontier"))
    routing = _routing(models)
    routing.tiers["standard"].allowed_capability.append("frontier")
    commands = ["make lint", "make test", "make scan"]
    if specific:
        commands.append("python3 -m unittest tests.test_x")
    return select_model(
        _uow(),
        routing,
        tier="standard",
        project="p",
        provider="fake",
        now=NOW,
        contract={"required_verification": [{"command": c} for c in commands]},
        policy_document={"repository": {"required_checks": commands[:3]}},
        pinned_model="codex-local" if pinned else None,
    )


def test_local_candidates_need_a_task_specific_check() -> None:
    result = _select()
    assert result.selected is not None and result.selected.id == "frontier"
    local = [c for c in result.candidates if c["pool"] == "lab-local"]
    assert len(local) == 3
    assert all(not c["eligible"] and c["excluded"] == ["no task-specific check"] for c in local)


def test_a_task_specific_check_allows_the_local_route() -> None:
    result = _select(specific=True)
    assert result.selected is not None and result.selected.id == "codex-local"
    assert all(c["eligible"] for c in result.candidates)


def test_operator_pin_cannot_bypass_task_specific_check() -> None:
    assert _select(pinned=True).selected is None


def _routing_setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    all_busy: bool,
    image: str | None = None,
    first_harness: str = "codex",
) -> tuple[Any, Any, Any]:
    routing = _routing(
        [
            _model("a-first", harness=first_harness),
            _model("b-second", harness="agy"),
        ]
    )
    task = Task(
        "task",
        "FDY-0190",
        "foundry",
        "p",
        "Busy harness fallthrough",
        TaskState.SCHEDULED,
        1,
        "policy",
        1,
        "repo",
        NOW,
        NOW,
    )
    policy = {"routing": {"policy": {"name": routing.name, "version": routing.version}}}
    execution = Execution(
        "execution",
        task.id,
        ExecutionRole.IMPLEMENT,
        1,
        "",
        "",
        None,
        "fake",
        "",
        policy,
        ExecutionState.CREATED,
        1,
        [],
        60,
        NOW,
    )
    attempt = Attempt("attempt", execution.id, task.id, 1, AttemptState.PENDING, NOW)
    uow: Any = MagicMock()
    uow.tasks.get.return_value = task
    live_executions = [
        replace(execution, id="live-codex", model="a-first", harness=first_harness),
    ]
    if all_busy:
        live_executions.append(replace(execution, id="live-agy", model="b-second", harness="agy"))
    uow.executions.get.side_effect = lambda execution_id, **_: next(
        row for row in [execution, *live_executions] if row.id == execution_id
    )
    uow.attempts.get.return_value = attempt
    uow.attempts.list_in_states.return_value = [
        Attempt(f"attempt-{row.id}", row.id, "other-task", 1, AttemptState.RUNNING, NOW)
        for row in live_executions
    ]
    uow.attempt_metrics.list_since.return_value = []
    uow.attempt_metrics.recent_for_project.return_value = []
    uow.pool_exhaustions.get.return_value = None
    uow.harness_images.get.return_value = None
    uow.provider_settings.get.return_value = None
    uow.events.latest_for_task_kind.return_value = None
    uow.routing_policies.get.return_value = MagicMock(document=routing.model_dump(mode="json"))
    supervisor = object.__new__(Supervisor)
    supervisor._clock = FakeClock(NOW)
    # hades #423: no provider here derives a capacity, so no launch waits for room.
    supervisor._providers = {}
    supervisor._capacity_now = {}
    monkeypatch.setattr(supervisor, "_fenced", lambda: nullcontext(uow))
    monkeypatch.setattr(supervisor, "_uow_factory", lambda: nullcontext(uow), raising=False)
    supervisor._harnesses = None
    supervisor._logins_now = frozenset()
    monkeypatch.setattr(supervisor, "_eligible_harnesses", lambda **_: None)
    monkeypatch.setattr(supervisor, "_logins_in_progress", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(supervisor, "_provider", MagicMock())
    monkeypatch.setattr(supervisor, "_harness_gate", lambda _: None)
    monkeypatch.setattr(supervisor, "_checkout_lease_free", lambda *_: True)
    monkeypatch.setattr(supervisor, "_take_checkout_lease", MagicMock(return_value=True))
    monkeypatch.setattr(supervisor, "_release_attempt_checkout", MagicMock())
    contract = contract_document()
    contract["execution_request"]["image"] = image
    pending = _Pending(task=task, execution=execution, attempt=attempt, contract=contract)
    return supervisor, pending, uow


async def _routing_attempt(
    monkeypatch: pytest.MonkeyPatch, *, all_busy: bool
) -> tuple[Any, Any, Any]:
    supervisor, pending, uow = _routing_setup(monkeypatch, all_busy=all_busy)
    result = await supervisor._begin_launch(pending)
    if result is None:
        supervisor._release_attempt_checkout.assert_called_once_with(pending.attempt.id)
    return result, pending.attempt, uow


async def test_busy_first_candidate_launches_the_next_without_demotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, attempt, uow = await _routing_attempt(monkeypatch, all_busy=False)

    assert result is not None
    assert attempt.selected_model == "b-second"
    assert attempt.selected_harness == "agy"
    assert attempt.state is AttemptState.PREPARING
    first = attempt.ordered_candidates[0]
    assert first["busy"] == "1 of 1 codex worker(s) already running"
    assert first["quality"]["demoted"] is False
    routed = next(
        event
        for event in (call.args[0] for call in uow.events.append.call_args_list)
        if event.kind == EventKind.ATTEMPT_ROUTED.value
    )
    assert routed.payload["skipped_busy"] == [
        {
            "model": "a-first",
            "harness": "codex",
            "reason": "1 of 1 codex worker(s) already running",
        }
    ]
    assert uow.attempt_metrics.add.call_count == 0


async def test_all_busy_candidates_defer_with_every_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    result, attempt, uow = await _routing_attempt(monkeypatch, all_busy=True)

    assert result is None
    assert attempt.state is AttemptState.PENDING
    deferred = next(
        event
        for event in (call.args[0] for call in uow.events.append.call_args_list)
        if event.kind == EventKind.HARNESS_LAUNCH_DEFERRED.value
    )
    assert [item["model"] for item in deferred.payload["skipped_busy"]] == [
        "a-first",
        "b-second",
    ]
    assert all(candidate.get("busy") for candidate in deferred.payload["ordered_candidates"])


@pytest.mark.parametrize("image", ["crucible-worker:fake-hang", "crucible-worker:fake-blocked"])
@pytest.mark.parametrize("busy_first", [False, True])
async def test_begin_launch_preserves_explicit_fake_image(
    monkeypatch: pytest.MonkeyPatch, image: str, busy_first: bool
) -> None:
    supervisor, pending, uow = _routing_setup(monkeypatch, all_busy=False, image=image)
    if not busy_first:
        uow.attempts.list_in_states.return_value = []

    result = await supervisor._begin_launch(pending)

    assert result is not None
    item, _provider = result
    assert item.attempt.selected_model == ("b-second" if busy_first else "a-first")
    assert item.attempt.selected_image == image
    assert item.execution.image == image
    spec = await supervisor._build_spec(item.attempt, item.execution, item.task, item.contract)
    assert spec.image == image


def test_task_page_words_explain_a_busy_first_choice() -> None:
    attempt = MagicMock(
        model="b-second",
        harness="agy",
        ordered_candidates=[
            {
                "model": "a-first",
                "harness": "codex",
                "busy": "1 of 1 codex worker(s) already running",
            },
            {"model": "b-second", "harness": "agy"},
        ],
    )

    assert _busy_fallthrough(attempt) == (
        "Ran on b-second on agy, its next available choice, because a-first on codex was busy."
    )


def test_pinned_local_model_needs_a_task_specific_check() -> None:
    model = {
        **_model("codex-local", pool="lab-local"),
        "endpoint": "local",
        "endpoint_url": "http://spark:4000/v1",
    }
    routing = _routing([model])
    policy_document = {
        "routing": {"policy": {"name": routing.name, "version": routing.version}},
        "repository": {"required_checks": ["make lint", "make test", "make scan"]},
    }
    document = contract_document()
    document["execution_request"].update(
        {
            "harness": "codex",
            "model": "codex-local",
            "pin_reason": "operator selects the local model",
        }
    )
    contract = TaskContractV1.model_validate(document)
    policy = Policy("policy", 1, policy_document, NOW)
    uow = MagicMock()
    uow.routing_policies.get.return_value = MagicMock(document=routing.model_dump(mode="json"))
    uow.attempt_metrics.list_since.return_value = []
    uow.pool_exhaustions.get.return_value = None
    uow.harness_images.get.return_value = None

    problems = _check_routing(uow, FakeClock(NOW), contract, policy, None, None)

    assert {problem["message"] for problem in problems} == {"no task-specific check"}


@pytest.mark.parametrize("quota", [False, True])
def test_no_candidates_blocks_with_the_reason(quota: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    selection = _select(frontier=False)
    assert selection.selected is None
    if quota:
        for candidate in selection.candidates:
            candidate["excluded"].append("pool is at its soft limit")
    assert not Supervisor._selection_is_quota_blocked(selection)
    task = Task(
        "task",
        "FDY-0156",
        "foundry",
        "p",
        "Check routing",
        TaskState.SCHEDULED,
        1,
        "policy",
        1,
        "repo",
        NOW,
        NOW,
    )
    execution = Execution(
        "execution",
        task.id,
        ExecutionRole.IMPLEMENT,
        1,
        "",
        "",
        None,
        "fake",
        "",
        {},
        ExecutionState.CREATED,
        1,
        [],
        60,
        NOW,
    )
    attempt = Attempt("attempt", execution.id, task.id, 1, AttemptState.PENDING, NOW)
    uow: Any = MagicMock()
    uow.tasks.get.return_value = task
    uow.executions.get.return_value = execution
    uow.attempts.get.return_value = attempt
    supervisor = object.__new__(Supervisor)
    supervisor._clock = FakeClock()
    monkeypatch.setattr(supervisor, "_fenced", lambda: nullcontext(uow))
    monkeypatch.setattr(supervisor, "_selection_for", lambda *_a, **_kw: selection)
    pending = _Pending(task=task, execution=execution, attempt=attempt, contract={})
    assert supervisor._route_pending(pending) is None
    assert task.state is TaskState.BLOCKED
    assert attempt.state is AttemptState.FAILED
    assert execution.state is ExecutionState.FAILED
    wake = uow.wakes.add.call_args.args[0]
    assert wake.reason == "blocked"
    assert "no task-specific check" in wake.payload["summary"]
    assert uow.escalations.add.call_count == 1
    assert attempt.ordered_candidates == list(selection.candidates)

    # GET /v1/tasks/{id} uses this query and serializes these summaries.
    uow.principals.get.return_value = None
    uow.repositories.get.return_value = None
    uow.contracts.list_for_task.return_value = []
    uow.executions.list_for_task.return_value = [execution]
    uow.attempts.list_for_execution.return_value = [attempt]
    uow.events.list_for_task.return_value = []
    uow.pull_requests.get_for_task.return_value = None
    view = task_view(uow, task.id).model_dump(mode="json")
    candidates = view["executions"][0]["attempts"][0]["ordered_candidates"]
    assert all("no task-specific check" in c["excluded"] for c in candidates)
