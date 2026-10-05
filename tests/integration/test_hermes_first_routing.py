"""ADR 0028 end to end: with Hermes, Claude Code and AGY all eligible, trivial and
standard work routes to Hermes and complex work to a frontier model; one failed Hermes
attempt does not move routing, a high blocking-failure rate does, and Hermes recovers
through a probe. Gate failures here are real ones the fake worker causes."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.harnesses import set_harness_enabled
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import Policy, RoutingPolicyRecord
from crucible.domain.exit_class import ExitClass
from crucible.ports.execution import ProviderError
from tests.fixtures import FakeClock, contract_document
from tests.integration.conftest import run_to_settled

pytestmark = pytest.mark.integration

VERSION = 90
FRONTIER = {"claude-fable-5-1", "gemini-3.1-pro-high"}
MID_FALLBACKS = {"claude-sonnet-5", "gemini-3.8-flash-high"}
SUCCEED = "crucible-worker:fake-succeed"
# Fails commits_present, a gate that blocks whichever gates are advisory (ADR 0024).
NO_COMMITS = "crucible-worker:fake-no-commits"


def _entry(model_id: str, harness: str, capability: str, pool: str) -> dict[str, Any]:
    local = harness == "hermes"
    entry: dict[str, Any] = {
        "id": model_id,
        "harness": harness,
        "endpoint": "local" if local else "subscription",
        "capability": capability,
        "cost": "none" if local else "high",
        "speed": "fast",
        "pool": pool,
        "weight": 1,
        "enabled": True,
    }
    if local:
        entry["endpoint_url"] = "http://gateway.lab.test:4000/v1"
    return entry


def _install(ctx: AppContext, clock: FakeClock, rotation: dict[str, Any]) -> None:
    """The seeded tiers (0008) with no `prefer_pools`, as every version before ADR 0028
    has them, so the default order is what routes."""
    routing = {
        "schema_version": "1.0",
        "name": "hermes-first-test",
        "version": VERSION,
        "tiers": {
            "trivial": {"allowed_capability": ["small", "mid"], "prefer": ["small"]},
            "standard": {"allowed_capability": ["mid", "small"], "prefer": ["mid"]},
            "complex": {"allowed_capability": ["frontier", "mid"], "prefer": ["frontier"]},
        },
        "models": [
            _entry("claude-haiku-4-5", "claude_code", "small", "anthropic-sub"),
            _entry("claude-sonnet-5", "claude_code", "mid", "anthropic-sub"),
            _entry("claude-fable-5-1", "claude_code", "frontier", "anthropic-sub"),
            _entry("gemini-3.8-flash-low", "agy", "small", "google-sub"),
            _entry("gemini-3.8-flash-high", "agy", "mid", "google-sub"),
            _entry("gemini-3.1-pro-high", "agy", "frontier", "google-sub"),
            _entry("coder", "hermes", "mid", "lab-local"),
        ],
        "pools": {
            pool: {"window": "5h", "budget_units": "attempts", "soft_limit": 0}
            for pool in ("anthropic-sub", "google-sub", "lab-local")
        },
        "rotation": {"strategy": "weighted-least-recent", **rotation},
    }
    with ctx.uow_factory() as uow:
        for name in ("claude_code", "agy", "hermes"):
            set_harness_enabled(
                uow,
                clock,
                principal_name="tests",
                name=name,
                enabled=True,
                reason="ADR 0028 integration: all three harnesses eligible",
            )
        seeded = uow.policies.get("default-software", 3)
        assert seeded is not None
        policy = copy.deepcopy(seeded.document)
        policy["version"] = VERSION
        policy["routing"] = {"policy": {"name": "hermes-first-test", "version": VERSION}}
        uow.routing_policies.put(
            RoutingPolicyRecord(
                name="hermes-first-test",
                version=VERSION,
                document=routing,
                created_at=clock.now(),
            )
        )
        uow.policies.put(
            Policy(
                name="default-software", version=VERSION, document=policy, created_at=clock.now()
            )
        )
        uow.commit()


def _submit(client: TestClient, external_id: str, tier: str, image: str) -> str:
    document = contract_document(external_id=external_id)
    document["required_verification"].append(
        {"id": "V5", "command": "python3 -m unittest tests.test_x", "expect_exit": 0}
    )
    document["repository"]["work_branch"] = f"crucible/{external_id}"
    document["policy"] = {"name": "default-software", "version": VERSION}
    for field in ("harness", "model", "pin_reason"):
        document["execution_request"].pop(field, None)
    document["execution_request"]["tier"] = tier
    # The fake provider runs this image whichever model routing picks.
    document["execution_request"]["image"] = image
    response = client.post("/v1/tasks", json=document)
    assert response.status_code == 201, response.text
    task_id = str(response.json()["id"])
    response = client.post(
        f"/v1/tasks/{task_id}/start", json={"provider": "fake", "policy_version": VERSION}
    )
    assert response.status_code == 200, response.text
    return task_id


def _model(client: TestClient, task_id: str) -> str:
    attempts = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]
    return str(attempts[0]["model"])


async def _route(
    client: TestClient, supervisor: Supervisor, external_id: str, tier: str, image: str
) -> tuple[str, str]:
    task_id = _submit(client, external_id, tier, image)
    await supervisor.tick()
    return task_id, _model(client, task_id)


async def _passes(client: TestClient, supervisor: Supervisor, task_id: str) -> None:
    """A clean attempt counts once its pre-PR gates pass, while it still waits for its
    review: a failure counts that early, so a pass must too."""
    assert await run_to_settled(supervisor, client, task_id) == "publishing"
    await supervisor.tick()


async def _fails(client: TestClient, supervisor: Supervisor, task_id: str) -> None:
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    await supervisor.tick()


async def test_hermes_does_the_work_and_frontier_takes_complex(
    client: TestClient, ctx: AppContext, clock: FakeClock, supervisor: Supervisor
) -> None:
    _install(ctx, clock, {"quality_feedback": True, "quality_window": 20})
    _, trivial = await _route(client, supervisor, "HF-TRIVIAL", "trivial", SUCCEED)
    _, standard = await _route(client, supervisor, "HF-STANDARD", "standard", SUCCEED)
    _, complex_ = await _route(client, supervisor, "HF-COMPLEX", "complex", SUCCEED)
    _, again = await _route(client, supervisor, "HF-STANDARD-2", "standard", SUCCEED)
    assert (trivial, standard, again) == ("coder", "coder", "coder")
    assert complex_ in FRONTIER


async def test_one_failure_holds_a_high_rate_moves_and_a_probe_recovers(
    client: TestClient, ctx: AppContext, clock: FakeClock, supervisor: Supervisor
) -> None:
    # A small window so the scenario is short: demoted at 60% over at least 2 judged.
    _install(
        ctx,
        clock,
        {
            "quality_feedback": True,
            "quality_window": 4,
            "demote_failure_percent": 60,
            "demote_min_sample": 2,
            "probe_after_minutes": 60,
        },
    )
    task, model = await _route(client, supervisor, "HF-1", "standard", SUCCEED)
    assert model == "coder"
    await _passes(client, supervisor, task)

    task, model = await _route(client, supervisor, "HF-2", "standard", NO_COMMITS)
    assert model == "coder"
    await _fails(client, supervisor, task)

    # One failure in two judged attempts is 50%, and one failure never demotes anyway.
    task, model = await _route(client, supervisor, "HF-3", "standard", NO_COMMITS)
    assert model == "coder", "one failed Hermes attempt moved routing"
    await _fails(client, supervisor, task)

    # Two of three failed a blocking gate: 67%, demoted. The fallbacks carry the work.
    _, model = await _route(client, supervisor, "HF-4", "standard", SUCCEED)
    assert model in MID_FALLBACKS

    # An hour on, Hermes gets a probe; it passes and the rate is 50%, under 60%.
    clock.advance(61 * 60)
    task, model = await _route(client, supervisor, "HF-5", "standard", SUCCEED)
    assert model == "coder", "a demoted model was never probed"
    await _passes(client, supervisor, task)

    _, model = await _route(client, supervisor, "HF-6", "standard", SUCCEED)
    assert model == "coder", "Hermes did not recover after a passing probe"


async def test_a_local_endpoint_failure_moves_work_to_the_fallbacks(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway down: one Hermes attempt ending `provider_error` moves nothing; two in
    a row mark its pool for the pool's cooldown, and the next standard task goes to a
    fallback. A subscription model's provider error marks nothing."""
    _install(ctx, clock, {"quality_feedback": True, "quality_window": 20})
    assert ctx.harnesses is not None
    for name in ("hermes", "claude_code", "agy"):
        adapter = ctx.harnesses.get(name)
        assert adapter is not None
        monkeypatch.setattr(adapter, "classify_exit", lambda *_a, **_k: ExitClass.PROVIDER_ERROR)
    task, model = await _route(client, supervisor, "HF-DOWN-1", "standard", SUCCEED)
    assert model == "coder"
    await run_to_settled(supervisor, client, task)
    with ctx.uow_factory() as uow:
        assert uow.pool_exhaustions.get("lab-local") is None, "one blip moved routing"

    task, model = await _route(client, supervisor, "HF-DOWN-1B", "standard", SUCCEED)
    assert model == "coder"
    await run_to_settled(supervisor, client, task)
    with ctx.uow_factory() as uow:
        mark = uow.pool_exhaustions.get("lab-local")
    assert mark is not None and mark.cleared_at is None
    assert mark.reason == "local endpoint failed (provider_error)"
    assert mark.reset_at > clock.now()

    task, model = await _route(client, supervisor, "HF-DOWN-2", "standard", SUCCEED)
    assert model in MID_FALLBACKS
    await run_to_settled(supervisor, client, task)
    with ctx.uow_factory() as uow:
        assert uow.pool_exhaustions.get("anthropic-sub") is None
        assert uow.pool_exhaustions.get("google-sub") is None


def _hermes_exit(ctx: AppContext, monkeypatch: pytest.MonkeyPatch, outcome: dict[str, Any]) -> None:
    """Hermes classifies every exit as `outcome["class"]`, or as it would, when None."""
    assert ctx.harnesses is not None
    adapter = ctx.harnesses.get("hermes")
    assert adapter is not None
    real = adapter.classify_exit

    def classify(*args: Any, **kwargs: Any) -> ExitClass:
        return outcome["class"] or real(*args, **kwargs)

    monkeypatch.setattr(adapter, "classify_exit", classify)


def _attempt_id(client: TestClient, task_id: str) -> str:
    return str(client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"][0]["id"])


async def test_a_collection_failure_is_not_a_gateway_failure(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex on #235: an exit whose outputs could not be collected is `environment`, so
    after one real gateway failure it does not take the local pool out of routing."""
    _install(ctx, clock, {"quality_feedback": True, "quality_window": 20})
    _hermes_exit(ctx, monkeypatch, {"class": ExitClass.PROVIDER_ERROR})
    task, model = await _route(client, supervisor, "HF-COLL-1", "standard", SUCCEED)
    assert model == "coder"
    await run_to_settled(supervisor, client, task)

    async def refuse(*_a: Any, **_k: Any) -> Any:
        raise ProviderError("the cluster did not answer the collection")

    monkeypatch.setattr(provider, "collect", refuse)
    task, model = await _route(client, supervisor, "HF-COLL-2", "standard", SUCCEED)
    assert model == "coder"
    await run_to_settled(supervisor, client, task)
    with ctx.uow_factory() as uow:
        assert uow.pool_exhaustions.get("lab-local") is None


async def test_the_previous_failure_is_the_one_that_finished_last(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex on #235: attempts on one pool finish out of launch order. A gateway failure,
    then a success that finished after it, then another failure is not two in a row, even
    when the first failure was launched after the success."""
    from sqlalchemy import update  # noqa: PLC0415

    from crucible.adapters.persistence.models import AttemptMetricsRow  # noqa: PLC0415

    _install(ctx, clock, {"quality_feedback": True, "quality_window": 20})
    outcome: dict[str, Any] = {"class": ExitClass.PROVIDER_ERROR}
    _hermes_exit(ctx, monkeypatch, outcome)
    failed, model = await _route(client, supervisor, "HF-ORDER-1", "standard", SUCCEED)
    assert model == "coder"
    await run_to_settled(supervisor, client, failed)
    outcome["class"] = None
    clock.advance(60)
    succeeded, model = await _route(client, supervisor, "HF-ORDER-2", "standard", SUCCEED)
    assert model == "coder"
    await run_to_settled(supervisor, client, succeeded)
    # The failure was launched after the success, and finished before it.
    with ctx.uow_factory() as uow:
        # attempt_metrics is fenced to the supervisor (14).
        assert supervisor.fenced_token is not None
        uow.set_fenced_token(supervisor.fenced_token)
        launched = uow.attempt_metrics.get(_attempt_id(client, succeeded))
        assert launched is not None and launched.created_at is not None
        uow.session.execute(  # type: ignore[attr-defined]
            update(AttemptMetricsRow)
            .where(AttemptMetricsRow.attempt_id == _attempt_id(client, failed))
            .values(created_at=launched.created_at + timedelta(seconds=1))
        )
        uow.commit()

    outcome["class"] = ExitClass.PROVIDER_ERROR
    clock.advance(60)
    task, model = await _route(client, supervisor, "HF-ORDER-3", "standard", SUCCEED)
    assert model == "coder"
    await run_to_settled(supervisor, client, task)
    with ctx.uow_factory() as uow:
        assert uow.pool_exhaustions.get("lab-local") is None
