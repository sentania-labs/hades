from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.unit_of_work import make_engine
from crucible.application import routing as routing_module
from crucible.application.admin.context import AdminContext
from crucible.application.harnesses import set_harness_enabled
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import HarnessImage, Policy, PoolExhaustion, RoutingPolicyRecord
from crucible.ports.execution import CleanupPolicy
from tests.fixtures import FakeClock, contract_document
from tests.integration.conftest import make_supervisor

pytestmark = pytest.mark.integration


def _model(model: str, harness: str, pool: str) -> dict[str, Any]:
    return {
        "id": model,
        "harness": harness,
        "endpoint": "subscription",
        "capability": "mid",
        "cost": "low",
        "speed": "fast",
        "pool": pool,
        "weight": 1,
        "enabled": True,
    }


def _install_policy(
    ctx: AppContext,
    clock: FakeClock,
    *,
    version: int,
    models: list[dict[str, Any]],
    reroute_max: int = 3,
    wait_max: int = 60,
    cooldown: int = 30,
    workspace_on_failure: str | None = None,
) -> None:
    with ctx.uow_factory() as uow:
        seeded = uow.policies.get("default-software", 3)
        assert seeded is not None
        routing = {
            "schema_version": "1.0",
            "name": "class-routing-test",
            "version": version,
            "tiers": {
                "trivial": {"allowed_capability": ["mid"], "prefer": ["mid"]},
                "standard": {"allowed_capability": ["mid"], "prefer": ["mid"]},
                "complex": {"allowed_capability": ["mid"], "prefer": ["mid"]},
            },
            "models": models,
            "pools": {
                model["pool"]: {
                    "window": "1h",
                    "budget_units": "attempts",
                    "soft_limit": 0,
                    "default_cooldown_seconds": cooldown,
                }
                for model in models
            },
            "rotation": {
                "strategy": "weighted-least-recent",
                "quality_feedback": True,
                "quality_window": 20,
            },
            "reroute": {
                "reroute_max": reroute_max,
                "resume_max_wait_seconds": wait_max,
            },
        }
        policy = copy.deepcopy(seeded.document)
        policy["version"] = version
        policy["routing"] = {"policy": {"name": "class-routing-test", "version": version}}
        if workspace_on_failure is not None:
            policy["cleanup"]["workspace_on_failure"] = workspace_on_failure
        uow.routing_policies.put(
            RoutingPolicyRecord(
                name="class-routing-test",
                version=version,
                document=routing,
                created_at=clock.now(),
            )
        )
        uow.policies.put(
            Policy(
                name="default-software",
                version=version,
                document=policy,
                created_at=clock.now(),
            )
        )
        uow.commit()


def _promote(ctx: AppContext, clock: FakeClock, harness: str, image: str) -> None:
    with ctx.uow_factory() as uow:
        uow.harness_images.put(
            HarnessImage(
                harness=harness,
                digest=f"sha256:{harness}-{image}",
                reference=image,
                version="1.0.0",
                updated_at=clock.now(),
                updated_by="tests",
                reason="class routing integration fixture",
            )
        )
        uow.commit()


def _class_contract(external_id: str, version: int) -> dict[str, Any]:
    document = contract_document(external_id=external_id)
    document["repository"]["work_branch"] = f"crucible/{external_id}"
    document["policy"] = {"name": "default-software", "version": version}
    for field in ("harness", "model", "pin_reason", "image"):
        document["execution_request"].pop(field, None)
    return document


def _submit(client: TestClient, external_id: str, version: int) -> str:
    response = client.post("/v1/tasks", json=_class_contract(external_id, version))
    assert response.status_code == 201, response.text
    task_id = str(response.json()["id"])
    response = client.post(
        f"/v1/tasks/{task_id}/start",
        json={"provider": "fake", "policy_version": version},
    )
    assert response.status_code == 200, response.text
    return task_id


def _events(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    return list(client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"])


async def test_quota_exit_commits_wip_marks_pool_and_reroutes_to_another_pool(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
    tokens: dict[str, str],
) -> None:
    _install_policy(
        ctx,
        clock,
        version=80,
        models=[
            _model("a-quota-model", "codex", "pool-a"),
            _model("b-success-model", "agy", "pool-b"),
        ],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-REROUTE", 80)

    await supervisor.tick()
    launched = client.get(f"/v1/tasks/{task_id}").json()
    first_attempt = launched["executions"][0]["attempts"][0]
    assert first_attempt["model"] == "a-quota-model", first_attempt
    assert first_attempt["image"] == "crucible-worker:fake-quota", first_attempt
    midway = launched
    assert midway["state"] == "scheduled"
    assert [attempt["pool"] for attempt in midway["executions"][0]["attempts"]] == [
        "pool-a",
        None,
    ]
    assert midway["executions"][0]["attempts"][1]["resume_from_remote"] is True

    events = _events(client, task_id)
    reroute = next(event for event in events if event["kind"] == "task_rerouted")
    assert reroute["payload"]["from_pool"] == "pool-a"
    assert reroute["payload"]["to_attempt_id"] == midway["executions"][0]["attempts"][1]["id"]
    assert "model" not in reroute["payload"]
    assert reroute["payload"]["wip_commit_sha"]
    assert any(event["kind"] == "quota_wip_committed" for event in events)
    usage = client.get("/v1/routing/usage", params={"policy_version": 80}).json()
    pool_a = next(pool for pool in usage["pools"] if pool["pool"] == "pool-a")
    assert pool_a["exhausted_until"] is not None

    assert ctx.harnesses is not None
    ctx.admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=clock,
        providers={"fake": provider},
        harnesses=ctx.harnesses,
    )
    with TestClient(
        create_app(ctx), headers={"Authorization": f"Bearer {tokens['admin']}"}
    ) as admin:
        listed = admin.get("/v1/admin/routing/exhaustion").json()["items"]
        assert next(mark for mark in listed if mark["pool"] == "pool-a")["active"] is True
        cleared = admin.post(
            "/v1/admin/routing/exhaustion/pool-a/clear",
            json={"reason": "integration parity"},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["active"] is False
        audit = admin.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
        assert any(event["kind"] == "pool_exhaustion_cleared" for event in audit)

    await supervisor.tick()
    final = client.get(f"/v1/tasks/{task_id}").json()
    attempts = final["executions"][0]["attempts"]
    assert [(item["model"], item["harness"]) for item in attempts] == [
        ("a-quota-model", "codex"),
        ("b-success-model", "agy"),
    ]
    assert [item["pool"] for item in attempts] == ["pool-a", "pool-b"]
    assert attempts[1]["state"] == "succeeded"


async def test_a_quota_reroute_never_launches_a_model_disabled_in_the_current_version(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
) -> None:
    """hades #254: v80 has a and b; the first attempt exits on quota in pool-a; v81,
    published before the successor launches, disables b. The successor routes with v81
    and pool-a excluded, so it is refused rather than launched on b."""
    _install_policy(
        ctx,
        clock,
        version=80,
        models=[
            _model("a-quota-model", "codex", "pool-a"),
            _model("b-success-model", "agy", "pool-b"),
        ],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-REROUTE-254", 80)

    await supervisor.tick()
    midway = client.get(f"/v1/tasks/{task_id}").json()
    assert midway["state"] == "scheduled", midway
    attempts = midway["executions"][0]["attempts"]
    assert [item["pool"] for item in attempts] == ["pool-a", None]

    disabled = _model("b-success-model", "agy", "pool-b")
    disabled["enabled"] = False
    _install_policy(
        ctx,
        clock,
        version=81,
        models=[_model("a-quota-model", "codex", "pool-a"), disabled],
    )

    await supervisor.tick()
    final = client.get(f"/v1/tasks/{task_id}").json()
    attempts = final["executions"][0]["attempts"]
    assert all(item["model"] != "b-success-model" for item in attempts), attempts
    assert attempts[1]["state"] != "succeeded"
    assert not any(
        event["kind"] == "attempt_routed" and event["payload"]["model"] == "b-success-model"
        for event in _events(client, task_id)
    )
    with ctx.uow_factory() as uow:
        successor = uow.attempts.get(attempts[1]["id"])
        assert successor is not None
        assert successor.selected_model is None
        execution = uow.executions.get(successor.execution_id)
        assert execution is not None
        assert execution.policy_snapshot["routing"]["policy"]["version"] == 80


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_all_pools_wait_and_a_restarted_supervisor_resumes_on_schedule(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=81,
        models=[_model("only-model", "codex", "only-pool")],
        wait_max=60,
        cooldown=30,
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    task_id = _submit(client, "C6B-WAIT", 81)
    await supervisor.tick()
    await supervisor.tick()

    waiting = client.get(f"/v1/tasks/{task_id}").json()
    assert waiting["state"] == "awaiting_quota"
    assert datetime.fromisoformat(waiting["resume_at"]) == clock.now() + timedelta(seconds=30)
    assert waiting["resume_at"] is not None
    assert sum(event["kind"] == "wake_created" for event in _events(client, task_id)) == 1

    await supervisor.stop()
    _promote(ctx, clock, "codex", "crucible-worker:fake-succeed")
    clock.advance(31)
    restarted = make_supervisor(ctx, provider, holder="sup-restarted")
    await restarted.tick()
    await restarted.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["resume_at"] is None
    assert len(view["executions"][0]["attempts"]) == 2
    assert any(event["kind"] == "task_quota_resumed" for event in _events(client, task_id))
    await restarted.stop()


async def test_reroute_and_wait_caps_end_through_the_reported_path(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=82,
        models=[
            _model("a-quota-model", "codex", "cap-pool-a"),
            _model("b-model", "agy", "cap-pool-b"),
        ],
        reroute_max=0,
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-REROUTE-CAP", 82)
    await supervisor.tick()
    await supervisor.tick()
    kinds = [event["kind"] for event in _events(client, task_id)]
    assert "task_rerouted" not in kinds
    assert "task_reported" in kinds


async def test_two_supervisors_cannot_double_launch_a_class_selected_attempt(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=83,
        models=[_model("selected-model", "codex", "selected-pool")],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-FENCE", 83)
    other = make_supervisor(ctx, provider, holder="sup-other")
    first = await supervisor.tick()
    second = await other.tick()
    assert first.held is True and first.launched == 1
    assert second.held is False and second.launched == 0
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert len(view["executions"][0]["attempts"]) == 1
    await supervisor.stop()


async def test_two_supervisors_interleaved_between_selection_and_reservation_launch_once(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=97,
        models=[_model("barrier-model", "codex", "barrier-pool")],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-FENCE-BARRIER", 97)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_prepare = provider.prepare
    prepare_calls = 0

    async def pause_first_prepare(
        spec: Any, checkout_token: Any = None, cancelled: Any = None
    ) -> Any:
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 1:
            entered.set()
            await release.wait()
        return await original_prepare(spec, checkout_token, cancelled)

    provider.prepare = pause_first_prepare  # type: ignore[method-assign]
    first_tick = asyncio.create_task(supervisor.tick())
    await entered.wait()
    clock.advance(31)
    takeover = make_supervisor(ctx, provider, holder="sup-barrier-takeover")
    second_result = await takeover.tick()
    await takeover.tick()
    release.set()
    first_result = await first_tick
    view = client.get(f"/v1/tasks/{task_id}").json()
    attempts = view["executions"][0]["attempts"]
    assert second_result.held is True and first_result.held is False
    assert sum(attempt["started_at"] is not None for attempt in attempts) == 1
    assert sum(attempt["state"] == "succeeded" for attempt in attempts) == 1
    await takeover.stop()


async def test_wait_cap_ends_the_task_through_reported_with_the_class_visible(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=84,
        models=[_model("wait-model", "codex", "wait-pool")],
        wait_max=10,
        cooldown=30,
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    task_id = _submit(client, "C6B-WAIT-CAP", 84)
    await supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_quota"

    with ctx.uow_factory() as uow:
        uow.pool_exhaustions.clear(
            "wait-pool", at=clock.now(), principal="tests", reason="candidate became eligible"
        )
        uow.commit()
    _promote(ctx, clock, "codex", "crucible-worker:fake-succeed")
    clock.advance(11)
    await supervisor.tick()
    events = _events(client, task_id)
    reported = next(event for event in events if event["kind"] == "task_reported")
    assert reported["payload"]["exit_class"] == "quota_exhausted"
    assert reported["payload"]["tier"] == "standard"
    assert any(
        event["kind"] == "execution_failed" and event["payload"].get("wait_cap_exceeded") is True
        for event in events
    )


async def test_restart_recovers_a_collected_quota_checkpoint(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=85,
        models=[
            _model("a-quota-model", "codex", "recovery-pool-a"),
            _model("b-success-model", "agy", "recovery-pool-b"),
        ],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-CHECKPOINT-RECOVERY", 85)

    original = supervisor._complete_quota_checkpoint

    async def crash_once(_attempt_id: str) -> None:
        raise RuntimeError("simulated supervisor crash after collection")

    supervisor._complete_quota_checkpoint = crash_once  # type: ignore[assignment]
    await supervisor.tick()
    stranded = client.get(f"/v1/tasks/{task_id}").json()
    assert stranded["state"] == "running"
    assert stranded["executions"][0]["attempts"][0]["state"] == "failed"
    supervisor._complete_quota_checkpoint = original  # type: ignore[method-assign]
    await supervisor.stop()

    restarted = make_supervisor(ctx, provider, holder="sup-checkpoint-recovery")
    await restarted.tick()
    recovered = client.get(f"/v1/tasks/{task_id}").json()
    assert any(event["kind"] == "task_rerouted" for event in _events(client, task_id))
    assert len(recovered["executions"][0]["attempts"]) == 2
    await restarted.stop()


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_unsafe_quota_checkpoint_is_not_rerouted(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=86,
        models=[
            _model("a-quota-model", "codex", "unsafe-pool-a"),
            _model("b-success-model", "agy", "unsafe-pool-b"),
        ],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-UNSAFE-CHECKPOINT", 86)
    original_collect = provider.collect

    async def unsafe_collect(*args: Any, **kwargs: Any) -> Any:
        outputs = await original_collect(*args, **kwargs)
        return replace(outputs, diff_paths=("outside/unsafe.txt",))

    provider.collect = unsafe_collect  # type: ignore[method-assign]
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    events = _events(client, task_id)
    assert view["state"] == "pre_pr_gates_failed"
    assert not any(event["kind"] == "task_rerouted" for event in events)
    failure = next(event for event in events if event["kind"] == "task_publish_failed")
    assert "scope_contained" in failure["payload"]["detail"]
    later = clock.now() + timedelta(hours=2)
    earlier = clock.now() + timedelta(minutes=10)
    attempt_id = view["executions"][0]["attempts"][0]["id"]
    with ctx.uow_factory() as uow:
        uow.pool_exhaustions.put(
            PoolExhaustion(
                pool="unsafe-pool-a",
                exhausted_at=clock.now(),
                reset_at=later,
                task_id=task_id,
                attempt_id=attempt_id,
                reason="long reset",
            )
        )
        uow.pool_exhaustions.put(
            PoolExhaustion(
                pool="unsafe-pool-a",
                exhausted_at=clock.now(),
                reset_at=earlier,
                task_id=task_id,
                attempt_id=attempt_id,
                reason="short reset",
            )
        )
        uow.commit()
    with ctx.uow_factory() as uow:
        mark = uow.pool_exhaustions.get("unsafe-pool-a")
        assert mark is not None and mark.reset_at == later


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_downgrade_and_upgrade_preserve_an_active_quota_wait(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
    database_url: str,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=87,
        models=[_model("only-quota-model", "codex", "migration-wait-pool")],
        wait_max=60,
        cooldown=30,
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    task_id = _submit(client, "C6B-MIGRATION-WAIT", 87)
    await supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_quota"
    with ctx.uow_factory() as uow:
        execution = uow.executions.list_for_task(task_id)[0]
        assert execution.resume_from_remote is True
    await supervisor.stop()
    ctx.engine.dispose()

    migrate.downgrade(database_url, "0010_bootstrap_import")
    check = make_engine(database_url)
    with check.connect() as connection:
        assert (
            connection.execute(
                text("SELECT state FROM tasks WHERE id=:task_id"), {"task_id": task_id}
            ).scalar_one()
            == "reported"
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM task_waits_c6b_archive WHERE task_id=:task_id"),
                {"task_id": task_id},
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM pool_exhaustions_c6b_archive WHERE pool=:pool"),
                {"pool": "migration-wait-pool"},
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text(
                    "SELECT resume_from_remote FROM execution_routes_c6b_archive "
                    "WHERE execution_id=(SELECT id FROM executions WHERE task_id=:task_id)"
                ),
                {"task_id": task_id},
            ).scalar_one()
            is True
        )
        archived_route = connection.execute(
            text(
                "SELECT selected_model, selected_harness, selected_pool "
                "FROM attempt_routes_c6b_archive"
            )
        ).one()
        assert tuple(archived_route) == ("only-quota-model", "codex", "migration-wait-pool")
    check.dispose()

    migrate.upgrade(database_url)
    check = make_engine(database_url)
    with check.connect() as connection:
        restored = connection.execute(
            text("SELECT state, resume_at, quota_wait_started_at FROM tasks WHERE id=:task_id"),
            {"task_id": task_id},
        ).one()
        assert restored.state == "awaiting_quota"
        assert restored.resume_at is not None and restored.quota_wait_started_at is not None
        assert (
            connection.execute(
                text("SELECT count(*) FROM pool_exhaustions WHERE pool=:pool"),
                {"pool": "migration-wait-pool"},
            ).scalar_one()
            == 1
        )
        restored_route = connection.execute(
            text(
                "SELECT selected_model, selected_harness, selected_pool, ordered_candidates "
                "FROM attempts WHERE task_id=:task_id"
            ),
            {"task_id": task_id},
        ).one()
        assert tuple(restored_route[:3]) == (
            "only-quota-model",
            "codex",
            "migration-wait-pool",
        )
        assert restored_route.ordered_candidates
        assert (
            connection.execute(
                text("SELECT resume_from_remote FROM executions WHERE task_id=:task_id"),
                {"task_id": task_id},
            ).scalar_one()
            is True
        )
    check.dispose()

    _promote(ctx, clock, "codex", "crucible-worker:fake-succeed")
    clock.advance(31)
    restarted = make_supervisor(ctx, provider, holder="sup-migration-resume")
    await restarted.tick()
    await restarted.tick()
    resumed = client.get(f"/v1/tasks/{task_id}").json()
    attempts = resumed["executions"][0]["attempts"]
    assert len(attempts) == 2
    assert attempts[1]["resume_from_remote"] is True
    assert attempts[1]["state"] == "succeeded"
    await restarted.stop()


async def test_runtime_harness_loss_is_environment_not_quota(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=88,
        models=[_model("runtime-model", "codex", "runtime-pool")],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-RUNTIME-HARNESS-LOSS", 88)
    with ctx.uow_factory() as uow:
        set_harness_enabled(
            uow,
            clock,
            principal_name="tests",
            name="codex",
            enabled=False,
            reason="simulate runtime loss",
        )
        uow.commit()
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    attempt = view["executions"][0]["attempts"][0]
    assert attempt["exit_class"] == "environment"
    assert view["state"] == "pre_pr_gates_failed"
    assert not any(event["kind"] == "task_awaiting_quota" for event in _events(client, task_id))


async def test_reservation_race_reroutes_and_discards_prepared_workspace(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=89,
        models=[
            _model("a-race-model", "codex", "race-pool-a"),
            _model("b-race-model", "agy", "race-pool-b"),
        ],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-succeed")
    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-RESERVATION-RACE", 89)
    with ctx.uow_factory() as uow:
        task = uow.tasks.get(task_id)
        assert task is not None
        task.head_sha = "a" * 40
        uow.tasks.save(task)
        uow.commit()
    original_prepare = provider.prepare

    async def exhaust_after_prepare(
        spec: Any, checkout_token: Any = None, cancelled: Any = None
    ) -> Any:
        workspace = await original_prepare(spec, checkout_token, cancelled)
        with ctx.uow_factory() as uow:
            uow.pool_exhaustions.put(
                PoolExhaustion(
                    pool="race-pool-a",
                    exhausted_at=clock.now(),
                    reset_at=clock.now() + timedelta(minutes=5),
                    task_id=spec.task_id,
                    attempt_id=spec.attempt_id,
                    reason="reservation race",
                )
            )
            uow.commit()
        provider.prepare = original_prepare  # type: ignore[method-assign]
        return workspace

    provider.prepare = exhaust_after_prepare  # type: ignore[method-assign]
    await supervisor.tick()
    midway = client.get(f"/v1/tasks/{task_id}").json()
    attempts = midway["executions"][0]["attempts"]
    assert midway["state"] == "scheduled"
    assert attempts[0]["exit_class"] == "quota_exhausted"
    assert attempts[0]["id"] in provider.discarded
    assert attempts[1]["state"] == "pending"
    assert not any(event["kind"] == "wake_created" for event in _events(client, task_id))
    assert not any(event["kind"] == "quota_wip_committed" for event in _events(client, task_id))
    reserve_reroute = next(
        event for event in _events(client, task_id) if event["kind"] == "task_rerouted"
    )
    assert "wip_commit_sha" not in reserve_reroute["payload"]

    await supervisor.tick()
    final = client.get(f"/v1/tasks/{task_id}").json()
    assert final["executions"][0]["attempts"][1]["model"] == "b-race-model"
    assert final["executions"][0]["attempts"][1]["state"] == "succeeded"


async def test_environment_retry_after_reroute_keeps_remote_checkpoint_continuity(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=90,
        models=[
            _model("a-quota-model", "codex", "retry-pool-a"),
            _model("b-environment-model", "agy", "retry-pool-b"),
        ],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    _promote(ctx, clock, "agy", "crucible-worker:fake-environment")
    task_id = _submit(client, "C6B-REROUTE-RETRY", 90)

    await supervisor.tick()
    await supervisor.tick()
    retried = client.get(f"/v1/tasks/{task_id}").json()
    attempts = retried["executions"][0]["attempts"]
    assert [attempt["exit_class"] for attempt in attempts[:2]] == [
        "quota_exhausted",
        "environment",
    ]
    assert attempts[2]["resume_from_remote"] is True

    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    await supervisor.tick()
    final = client.get(f"/v1/tasks/{task_id}").json()
    assert final["executions"][0]["attempts"][2]["state"] == "succeeded"


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_checkpoint_push_failure_forces_workspace_and_bundle_retention(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=91,
        models=[_model("quota-model", "codex", "retention-pool")],
        workspace_on_failure="delete",
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    task_id = _submit(client, "C6B-CHECKPOINT-RETAIN", 91)

    async def fail_push(_attempt_id: str, *, required: bool) -> tuple[bool, str]:
        assert required is False
        return False, "synthetic checkpoint push failure"

    supervisor.delivery.push_quota_checkpoint = fail_push  # type: ignore[assignment]
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    attempt = view["executions"][0]["attempts"][0]
    events = _events(client, task_id)
    failure = next(event for event in events if event["kind"] == "task_publish_failed")
    bundle_path = failure["payload"]["bundle_path"]
    assert bundle_path.endswith("/output/work_branch.bundle")
    wake = next(event for event in events if event["kind"] == "wake_created")
    assert bundle_path in wake["payload"]["summary"]
    assert any(
        event["kind"] == "retention_applied"
        and event["payload"].get("kind") == "quota_checkpoint_retained"
        for event in events
    )

    await supervisor.tick()
    assert provider.cleanup_policies[attempt["id"]] is CleanupPolicy.KEEP
    with ctx.uow_factory() as uow:
        retained = next(
            action
            for action in uow.retention.list_recent(50)
            if action.kind == "quota_checkpoint_retained" and action.subject == attempt["id"]
        )
    assert retained.detail["bundle_path"] == bundle_path


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_quota_text_reroutes_only_the_task_without_marking_the_pool(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=92,
        models=[
            _model("a-quota-model", "claude_code", "text-pool-a"),
            _model("b-success-model", "agy", "text-pool-b"),
        ],
    )
    _promote(ctx, clock, "claude_code", "crucible-worker:fake-quota")
    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-TEXT-QUOTA", 92)
    original_collect = provider.collect

    async def text_only_collect(*args: Any, **kwargs: Any) -> Any:
        outputs = await original_collect(*args, **kwargs)
        if outputs.stderr_tail:
            return replace(
                outputs,
                stderr_tail=(
                    '{"type":"assistant","rate_limit_event":{"status":"rejected",'
                    '"reason":"out_of_credits"},"metadata":'
                    '{"reset_at":"2026-09-21T12:00:00Z"}}'
                ),
            )
        return outputs

    provider.collect = text_only_collect  # type: ignore[method-assign]
    await supervisor.tick()
    provider.collect = original_collect  # type: ignore[method-assign]
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "scheduled"
    with ctx.uow_factory() as uow:
        assert uow.pool_exhaustions.get("text-pool-a") is None
        attempts = uow.attempts.list_for_task(task_id)
        assert attempts[1].routing_excluded_pools == ["text-pool-a"]

    await supervisor.tick()
    final = client.get(f"/v1/tasks/{task_id}").json()
    attempts = final["executions"][0]["attempts"]
    assert [(item["model"], item["state"]) for item in attempts] == [
        ("a-quota-model", "failed"),
        ("b-success-model", "succeeded"),
    ]
    assert final["resume_at"] is None


async def test_image_unusable_candidate_does_not_turn_quota_wait_into_environment(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_image_for_harness = routing_module.image_for_harness

    def image_for_harness(uow: Any, harness: str, provider_name: str) -> str | None:
        if harness == "claude_code":
            return None
        return real_image_for_harness(uow, harness, provider_name)

    monkeypatch.setattr(routing_module, "image_for_harness", image_for_harness)
    _install_policy(
        ctx,
        clock,
        version=97,
        models=[
            _model("a-image-unusable", "claude_code", "image-pool-a"),
            _model("b-quota-model", "codex", "image-pool-b"),
        ],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    task_id = _submit(client, "C6B-IMAGE-QUOTA-WAIT", 97)

    await supervisor.tick()

    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "awaiting_quota"
    assert view["executions"][0]["attempts"][0]["model"] == "b-quota-model"
    waiting = next(
        event for event in _events(client, task_id) if event["kind"] == "task_awaiting_quota"
    )
    candidates = waiting["payload"]["ordered_candidates"]
    first = next(item for item in candidates if item["model"] == "a-image-unusable")
    second = next(item for item in candidates if item["model"] == "b-quota-model")
    assert first["excluded"] == ["selected harness has no default image"]
    assert any(reason.startswith("pool exhausted until ") for reason in second["excluded"])


async def test_timed_resumes_share_the_reroute_cap(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=93,
        models=[_model("quota-model", "codex", "resume-cap-pool")],
        reroute_max=1,
        wait_max=120,
        cooldown=10,
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    task_id = _submit(client, "C6B-RESUME-CAP", 93)
    await supervisor.tick()
    clock.advance(11)
    await supervisor.tick()
    await supervisor.tick()
    assert len(client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]) == 2
    clock.advance(11)
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "pre_pr_gates_failed"
    events = _events(client, task_id)
    assert sum(event["kind"] == "task_quota_resumed" for event in events) == 1
    assert any(
        event["kind"] == "execution_failed" and event["payload"].get("reroute_cap") == 1
        for event in events
    )


async def test_pinned_task_waits_restarts_and_ends_at_its_wait_cap(
    client: TestClient,
    tokens: dict[str, str],
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=94,
        models=[_model("pinned-quota", "codex", "pinned-pool")],
        wait_max=10,
        cooldown=30,
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    document = _class_contract("C6B-PINNED-WAIT", 94)
    document["execution_request"].update(
        {
            "harness": "codex",
            "model": "pinned-quota",
            "pin_reason": "operator holds this model for bootstrap verification",
        }
    )
    headers = {"Authorization": f"Bearer {tokens['operator']}"}
    response = client.post("/v1/tasks", json=document, headers=headers)
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    response = client.post(
        f"/v1/tasks/{task_id}/start",
        json={"provider": "fake", "policy_version": 94},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    await supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_quota"
    await supervisor.stop()
    clock.advance(11)
    restarted = make_supervisor(ctx, provider, holder="sup-pinned-wait")
    await restarted.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "pre_pr_gates_failed"
    await restarted.stop()


async def test_provider_reset_is_not_shortened_by_the_task_wait_cap(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=95,
        models=[_model("quota-model", "codex", "long-reset-pool")],
        wait_max=86400,
        cooldown=30,
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    task_id = _submit(client, "C6B-LONG-RESET", 95)
    reset_at = clock.now() + timedelta(days=2)
    original_collect = provider.collect

    async def collect_with_reset(*args: Any, **kwargs: Any) -> Any:
        outputs = await original_collect(*args, **kwargs)
        if outputs.stderr_tail:
            return replace(
                outputs,
                stderr_tail=(
                    '{"type":"turn.failed","error":{"code":"usage_limit_reached"},'
                    f'"reset_at":"{reset_at.isoformat()}"}}'
                ),
            )
        return outputs

    provider.collect = collect_with_reset  # type: ignore[method-assign]
    await supervisor.tick()
    with ctx.uow_factory() as uow:
        mark = uow.pool_exhaustions.get("long-reset-pool")
        assert mark is not None and mark.reset_at == reset_at
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert datetime.fromisoformat(view["resume_at"]) == clock.now() + timedelta(days=1)


async def test_selection_history_is_not_limited_to_the_first_task_page(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
) -> None:
    _install_policy(
        ctx,
        clock,
        version=96,
        models=[
            _model("a-history-model", "codex", "history-pool-a"),
            _model("b-history-model", "agy", "history-pool-b"),
        ],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-succeed")
    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    task_id = _submit(client, "C6B-HISTORY-PAGE", 96)
    historical_task = "9" + str(10001).zfill(25)
    historical_execution = "8" + str(10001).zfill(25)
    historical_attempt = "7" + str(10001).zfill(25)
    assert supervisor._lease_step() is True
    assert supervisor.fenced_token is not None
    with ctx.engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('crucible.fenced_token', :token, true)"),
            {"token": str(supervisor.fenced_token)},
        )
        connection.execute(
            text(
                "INSERT INTO tasks "
                "(id, external_id, principal_id, repository_id, project, title, state, "
                "contract_version, policy_name, policy_version, created_at, updated_at) "
                "SELECT '9' || lpad(gs::text, 25, '0'), 'HIST-' || gs, principal_id, "
                "repository_id, project, 'history', 'closed', 1, policy_name, policy_version, "
                "now(), now() FROM tasks, generate_series(1, 10001) gs WHERE id=:task_id"
            ),
            {"task_id": task_id},
        )
        connection.execute(
            text(
                "INSERT INTO executions "
                "(id, task_id, role, contract_version, harness, model, provider, image, "
                "policy_snapshot, state, max_attempts, retry_on, timeout_seconds, created_at, "
                "resume_from_remote) VALUES (:id, :task_id, 'implement', 1, 'codex', "
                "'a-history-model', 'fake', 'crucible-worker:fake-succeed', '{}', "
                "'succeeded', 1, '[]', 60, now(), false)"
            ),
            {"id": historical_execution, "task_id": historical_task},
        )
        connection.execute(
            text(
                "INSERT INTO attempts (id, execution_id, task_id, number, state, created_at, "
                "log_resume_occurrence, unsupervised, ordered_candidates, "
                "resume_from_remote) VALUES "
                "(:id, :execution_id, :task_id, 1, 'succeeded', now(), 0, false, '[]', false)"
            ),
            {
                "id": historical_attempt,
                "execution_id": historical_execution,
                "task_id": historical_task,
            },
        )
        connection.execute(
            text(
                "INSERT INTO attempt_metrics "
                "(attempt_id, task_id, model, harness, endpoint_kind, pool, cost_source, "
                "gates_passed, gates_failed, corrections_after, created_at) VALUES "
                "(:attempt_id, :task_id, 'a-history-model', 'codex', 'subscription', "
                "'history-pool-a', 'none', 0, 0, 0, now())"
            ),
            {"attempt_id": historical_attempt, "task_id": historical_task},
        )
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["executions"][0]["attempts"][0]["model"] == "b-history-model"
