"""Hades #354: Codex on a local lane is sent a model name it knows.

Before this, the supervisor always sent a harness the routing entry's own `model` (the
lane): Codex was launched `--model fast`, which its catalog does not recognise, so it
fell back to generic tool, prompt, output-token and compaction defaults. A routing
entry may now carry `harness_model_name`, the name actually sent to its harness (for
example a gateway alias Codex's catalog recognises), while `model` keeps the lane name
routing, pools and evidence always read. The entry's own `context_length` and
`max_output_tokens` also feed Codex's local provider config ahead of the
Hermes-administered defaults. An entry that sets none of this launches exactly as
before.
"""

from __future__ import annotations

import copy
import tomllib
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.harness.registry import default_registry
from crucible.application.routing import launch_model_name
from crucible.application.supervisor import Supervisor, _Pending
from crucible.contracts.policy import RoutingModel
from crucible.domain.entities import AttemptMetrics, Execution, ExecutionRole, RoutingPolicyRecord
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import AttemptState
from crucible.ports.execution import LaunchRefusedError, Workspace
from crucible.ports.harness import CredentialSource
from tests.fixtures import FakeClock
from tests.unit.test_codex_local import routing
from tests.unit.test_issue_254_routing_at_launch import _Metrics, _store
from tests.unit.test_issue_360_ready_for_merge_correction import (
    NOW,
    _attach,
    _correction,
    _correction_attempt,
    _routing,
    _supervisor,
)


def test_launch_model_name_prefers_the_override() -> None:
    """The pure helper underlying every launch and the evidence payload."""
    override = RoutingModel.model_validate(
        {
            "model": "fast",
            "harness": "codex",
            "endpoint": "subscription",
            "capability": "mid",
            "cost": "low",
            "speed": "fast",
            "pool": "p",
            "weight": 1,
            "enabled": True,
            "harness_model_name": "gpt-5.4",
        }
    )
    assert launch_model_name(override, "fast") == "gpt-5.4"
    no_override = override.model_copy(update={"harness_model_name": None})
    assert launch_model_name(no_override, "fast") == "fast"
    assert launch_model_name(None, "fast") == "fast"


@pytest.mark.asyncio
async def test_codex_local_launch_sends_the_harness_model_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC1: a Codex attempt on a local entry whose Codex model name is set launches
    with --model set to that name; `model` (the lane) is unaffected."""
    route = routing()
    route.models[1].harness_model_name = "gpt-5.4"
    monkeypatch.setattr("crucible.application.supervisor.load_attempt_routing", lambda *_: route)

    def setting(name: str) -> Any:
        return SimpleNamespace(document={"context_length": 96000, "max_output_tokens": 16000})

    uow: Any = SimpleNamespace(provider_settings=SimpleNamespace(get=setting))
    supervisor = object.__new__(Supervisor)
    supervisor._uow_factory = lambda: nullcontext(uow)  # type: ignore[assignment]
    supervisor._harnesses = default_registry()
    (tmp_path / "api-key").write_text("test-key")
    supervisor._credential_sources = {"hermes": CredentialSource(str(tmp_path))}
    attempt: Any = SimpleNamespace(
        id="attempt",
        number=1,
        selected_harness="codex",
        selected_model="z-codex",
        selected_image="image",
        resume_from_remote=False,
        routing_version=None,
        effective_settings=None,
    )
    execution: Any = SimpleNamespace(
        role=ExecutionRole.IMPLEMENT,
        harness="codex",
        model="z-codex",
        image="image",
        policy_snapshot={},
        timeout_seconds=60,
        effort=None,
        provider="docker",
    )
    task: Any = SimpleNamespace(id="task", external_id="FDY-0515", principal_id="tests")
    launch = await supervisor._build_spec(attempt, execution, task, {})

    assert launch.model == "z-codex"
    assert launch.command[launch.command.index("--model") + 1] == "gpt-5.4"


@pytest.mark.asyncio
async def test_codex_local_provider_config_reads_the_model_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC2: Codex's local provider config takes model_context_window and the max
    output tokens from the model entry, not the Hermes default."""
    route = routing()
    route.models[1].context_length = 200_000
    route.models[1].max_output_tokens = 50_000
    monkeypatch.setattr("crucible.application.supervisor.load_attempt_routing", lambda *_: route)

    def setting(name: str) -> Any:
        return SimpleNamespace(document={"context_length": 96000, "max_output_tokens": 16000})

    uow: Any = SimpleNamespace(provider_settings=SimpleNamespace(get=setting))
    supervisor = object.__new__(Supervisor)
    supervisor._uow_factory = lambda: nullcontext(uow)  # type: ignore[assignment]
    supervisor._harnesses = default_registry()
    (tmp_path / "api-key").write_text("test-key")
    supervisor._credential_sources = {"hermes": CredentialSource(str(tmp_path))}
    attempt: Any = SimpleNamespace(
        id="attempt",
        number=1,
        selected_harness="codex",
        selected_model="z-codex",
        selected_image="image",
        resume_from_remote=False,
        routing_version=None,
        effective_settings=None,
    )
    execution: Any = SimpleNamespace(
        role=ExecutionRole.IMPLEMENT,
        harness="codex",
        model="z-codex",
        image="image",
        policy_snapshot={},
        timeout_seconds=60,
        effort=None,
        provider="docker",
    )
    task: Any = SimpleNamespace(id="task", external_id="FDY-0515", principal_id="tests")
    launch = await supervisor._build_spec(attempt, execution, task, {})

    document = tomllib.loads(launch.env["CRUCIBLE_CODEX_CONFIG"])
    assert document["model_context_window"] == 200_000
    assert document["model_max_output_tokens"] == 50_000
    # The lane still reads the Hermes-administered doc on `provider_settings`.
    assert launch.effective_settings == {
        "context_length": 200_000,
        "max_output_tokens": 50_000,
        "thinking": False,
    }


def _codex_local_attempt_execution_task() -> tuple[Any, Any, Any]:
    attempt: Any = SimpleNamespace(
        id="attempt",
        number=1,
        selected_harness="codex",
        selected_model="z-codex",
        selected_image="image",
        resume_from_remote=False,
        routing_version=None,
        effective_settings=None,
    )
    execution: Any = SimpleNamespace(
        role=ExecutionRole.IMPLEMENT,
        harness="codex",
        model="z-codex",
        image="image",
        policy_snapshot={},
        timeout_seconds=60,
        effort=None,
        provider="docker",
    )
    task: Any = SimpleNamespace(id="task", external_id="FDY-0515", principal_id="tests")
    return attempt, execution, task


@pytest.mark.asyncio
async def test_codex_local_own_pair_exhausting_the_window_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review 01M4CCMCM2AWB85MFRQ53Y2GDB: a route whose own `max_output_tokens`
    consumes its own `context_length` would leave no input budget; the saved Hermes
    settings reject this pair (`hermes_limit_problems`), and so does this launch."""
    route = routing()
    route.models[1].context_length = 20_000
    route.models[1].max_output_tokens = 20_000
    monkeypatch.setattr("crucible.application.supervisor.load_attempt_routing", lambda *_: route)

    def setting(name: str) -> Any:
        return SimpleNamespace(document={"context_length": 96000, "max_output_tokens": 16000})

    uow: Any = SimpleNamespace(provider_settings=SimpleNamespace(get=setting))
    supervisor = object.__new__(Supervisor)
    supervisor._uow_factory = lambda: nullcontext(uow)  # type: ignore[assignment]
    supervisor._harnesses = default_registry()
    (tmp_path / "api-key").write_text("test-key")
    supervisor._credential_sources = {"hermes": CredentialSource(str(tmp_path))}
    attempt, execution, task = _codex_local_attempt_execution_task()
    with pytest.raises(LaunchRefusedError):
        await supervisor._build_spec(attempt, execution, task, {})


@pytest.mark.asyncio
async def test_codex_local_context_length_below_inherited_allowance_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review 01M4CCMCM2AWB85MFRQ53Y2GDB: a route that only overrides
    `context_length`, to a value its own inherited Hermes `max_output_tokens`
    already consumes, is refused too; the two overrides are independent."""
    route = routing()
    route.models[1].context_length = 10_000
    monkeypatch.setattr("crucible.application.supervisor.load_attempt_routing", lambda *_: route)

    def setting(name: str) -> Any:
        return SimpleNamespace(document={"context_length": 96000, "max_output_tokens": 16000})

    uow: Any = SimpleNamespace(provider_settings=SimpleNamespace(get=setting))
    supervisor = object.__new__(Supervisor)
    supervisor._uow_factory = lambda: nullcontext(uow)  # type: ignore[assignment]
    supervisor._harnesses = default_registry()
    (tmp_path / "api-key").write_text("test-key")
    supervisor._credential_sources = {"hermes": CredentialSource(str(tmp_path))}
    attempt, execution, task = _codex_local_attempt_execution_task()
    with pytest.raises(LaunchRefusedError):
        await supervisor._build_spec(attempt, execution, task, {})


@pytest.mark.asyncio
async def test_codex_local_entry_without_override_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3: an entry with no per-harness name (and no context/output override) behaves
    exactly as today: the lane name is sent, and the Hermes-administered limits apply."""
    route = routing()
    monkeypatch.setattr("crucible.application.supervisor.load_attempt_routing", lambda *_: route)

    def setting(name: str) -> Any:
        return SimpleNamespace(document={"context_length": 96000, "max_output_tokens": 16000})

    uow: Any = SimpleNamespace(provider_settings=SimpleNamespace(get=setting))
    supervisor = object.__new__(Supervisor)
    supervisor._uow_factory = lambda: nullcontext(uow)  # type: ignore[assignment]
    supervisor._harnesses = default_registry()
    (tmp_path / "api-key").write_text("test-key")
    supervisor._credential_sources = {"hermes": CredentialSource(str(tmp_path))}
    attempt: Any = SimpleNamespace(
        id="attempt",
        number=1,
        selected_harness="codex",
        selected_model="z-codex",
        selected_image="image",
        resume_from_remote=False,
        routing_version=None,
        effective_settings=None,
    )
    execution: Any = SimpleNamespace(
        role=ExecutionRole.IMPLEMENT,
        harness="codex",
        model="z-codex",
        image="image",
        policy_snapshot={},
        timeout_seconds=60,
        effort=None,
        provider="docker",
    )
    task: Any = SimpleNamespace(id="task", external_id="FDY-0515", principal_id="tests")
    launch = await supervisor._build_spec(attempt, execution, task, {})

    assert launch.model == "z-codex"
    assert launch.command[launch.command.index("--model") + 1] == "z-codex"
    document = tomllib.loads(launch.env["CRUCIBLE_CODEX_CONFIG"])
    assert document["model_context_window"] == 96000
    assert document["model_max_output_tokens"] == 16000


def _local_model(model_id: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = copy.deepcopy(_routing().document["models"][0])
    entry.update(id=model_id, endpoint="local", endpoint_url="http://gateway.lab.test:4000/v1")
    entry.update(overrides)
    return entry


def _local_version(version: int, models: list[dict[str, Any]]) -> RoutingPolicyRecord:
    document = copy.deepcopy(_routing().document)
    document["version"] = version
    document["models"] = models
    for model in document["models"]:
        model["pool"] = "gateway"
    document["pools"]["gateway"] = dict(document["pools"]["openai-sub"])
    return RoutingPolicyRecord(
        name="default-routing",
        version=version,
        document=document,
        created_at=NOW + timedelta(minutes=version),
    )


def _route_local_correction(store: Any, tmp_path: Path) -> tuple[Execution, Any]:
    """`_route_correction` (hades #254), with a task-specific check so a local-endpoint
    model is not excluded ("no task-specific check")."""
    clock = FakeClock(NOW)
    body = _correction(
        required_verification=[
            {"id": "V1", "command": "make lint", "expect_exit": 0},
            {"id": "V2", "command": "make test", "expect_exit": 0},
            {"id": "V3", "command": "make scan", "expect_exit": 0},
            {"id": "V4", "command": "python3 -m unittest tests.test_x", "expect_exit": 0},
        ]
    )
    _attach(store, body, clock)
    supervisor, _provider = _supervisor(store, clock, tmp_path)
    supervisor._materialize_scheduled()
    execution, attempt = _correction_attempt(store)
    assert attempt.state is AttemptState.PENDING
    task = store.tasks.get(attempt.task_id)
    stored = store.contracts.get(attempt.task_id, execution.contract_version)
    assert task is not None and stored is not None
    routed = supervisor._route_pending(_Pending(attempt, execution, task, stored.document))
    assert routed is not None, "the correction was not routed"
    return _correction_attempt(store)


def test_attempt_launching_records_the_lane_and_the_sent_name(tmp_path: Path) -> None:
    """AC1: the attempt evidence (the attempt_launching event) records both the lane
    (`model`) and the name actually sent to the harness (`sent_model_name`)."""
    store = _store(_local_version(4, [_local_model("fast", harness_model_name="gpt-5.4")]))
    _execution, attempt = _route_local_correction(store, tmp_path)
    assert attempt.selected_model == "fast"
    assert attempt.selected_harness == "codex"

    metrics: list[AttemptMetrics] = []
    store.attempt_metrics = _Metrics(metrics)
    supervisor, _provider = _supervisor(store, FakeClock(NOW), tmp_path)
    root = tmp_path / "ws"
    workspace = Workspace(
        attempt_id=attempt.id,
        checkout_path=str(root / "repo"),
        identity_path=str(root / "identity"),
        report_path=str(root / "report"),
    )
    assert supervisor._mark_launching(attempt.id, workspace)

    launching = next(
        e.payload
        for e in store.events.rows
        if e.kind == EventKind.ATTEMPT_LAUNCHING.value and e.attempt_id == attempt.id
    )
    assert launching["model"] == "fast"
    assert launching["sent_model_name"] == "gpt-5.4"


def test_attempt_launching_sent_name_falls_back_to_the_lane(tmp_path: Path) -> None:
    """AC3: with no `harness_model_name`, the evidence's sent name is the lane itself."""
    store = _store(_local_version(4, [_local_model("fast")]))
    _execution, attempt = _route_local_correction(store, tmp_path)

    metrics: list[AttemptMetrics] = []
    store.attempt_metrics = _Metrics(metrics)
    supervisor, _provider = _supervisor(store, FakeClock(NOW), tmp_path)
    root = tmp_path / "ws"
    workspace = Workspace(
        attempt_id=attempt.id,
        checkout_path=str(root / "repo"),
        identity_path=str(root / "identity"),
        report_path=str(root / "report"),
    )
    assert supervisor._mark_launching(attempt.id, workspace)

    launching = next(
        e.payload
        for e in store.events.rows
        if e.kind == EventKind.ATTEMPT_LAUNCHING.value and e.attempt_id == attempt.id
    )
    assert launching["model"] == "fast"
    assert launching["sent_model_name"] == "fast"
