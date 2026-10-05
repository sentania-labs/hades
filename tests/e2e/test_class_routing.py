from __future__ import annotations

import copy
import subprocess

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import Policy, RoutingPolicyRecord
from tests.e2e import daemon
from tests.e2e.conftest import (
    RUN_ID,
    OriginFactory,
    e2e_contract,
    register,
    run_until,
    submit_and_start,
)

pytestmark = [
    pytest.mark.e2e,
    # Real containers, and the first case also pays for the session's stack; the waits
    # inside allow up to four minutes (issue 192).
    pytest.mark.timeout(600),
]


def _install_class_policy(ctx: AppContext) -> None:
    with ctx.uow_factory() as uow:
        seeded = uow.policies.get("e2e-script", 1)
        assert seeded is not None
        routing = {
            "schema_version": "1.0",
            "name": "e2e-class-routing",
            "version": 2,
            "tiers": {
                tier: {"allowed_capability": ["small"], "prefer": ["small"]}
                for tier in ("trivial", "standard", "complex")
            },
            "models": [
                {
                    "id": "a-scripted-quota",
                    "harness": "script-harness",
                    "endpoint": "subscription",
                    "capability": "small",
                    "cost": "none",
                    "speed": "fast",
                    "pool": "script-pool-a",
                    "weight": 1,
                    "enabled": True,
                },
                {
                    "id": "b-script-success",
                    "harness": "script-harness",
                    "endpoint": "subscription",
                    "capability": "small",
                    "cost": "none",
                    "speed": "fast",
                    "pool": "script-pool-b",
                    "weight": 1,
                    "enabled": True,
                },
            ],
            "pools": {
                pool: {
                    "window": "1h",
                    "budget_units": "attempts",
                    "soft_limit": 0,
                    "default_cooldown_seconds": 60,
                }
                for pool in ("script-pool-a", "script-pool-b")
            },
            "rotation": {
                "strategy": "weighted-least-recent",
                "quality_feedback": False,
                "quality_window": 10,
            },
            "reroute": {"reroute_max": 3, "resume_max_wait_seconds": 300},
        }
        policy = copy.deepcopy(seeded.document)
        policy["version"] = 2
        policy["routing"] = {"policy": {"name": "e2e-class-routing", "version": 2}}
        uow.routing_policies.put(
            RoutingPolicyRecord(
                name="e2e-class-routing",
                version=2,
                document=routing,
                created_at=ctx.clock.now(),
            )
        )
        uow.policies.put(
            Policy(name="e2e-script", version=2, document=policy, created_at=ctx.clock.now())
        )
        uow.commit()


def _promote_second_tag(ctx: AppContext, worker_image: str) -> str:
    second_image = f"crucible-worker:script-harness-reroute-{RUN_ID}"
    daemon.run("tag", worker_image, second_image)
    with ctx.uow_factory() as uow:
        current = uow.harness_images.get("script-harness")
        assert current is not None
        current.reference = second_image
        current.updated_at = ctx.clock.now()
        current.updated_by = "e2e"
        current.reason = "class routing second image"
        uow.harness_images.put(current)
        uow.commit()
    return second_image


async def test_scripted_quota_reroutes_to_a_second_image_and_remote_branch(
    client: TestClient,
    ctx: AppContext,
    origin: OriginFactory,
    worker_image: str,
    supervisor: Supervisor,
) -> None:
    url = origin("class-routing", "quota")
    register(ctx, "class-routing", url)
    _install_class_policy(ctx)
    document = e2e_contract("E2E-C6B", "class-routing", worker_image)
    document["policy"] = {"name": "e2e-script", "version": 2}
    document["scope"]["allowed_paths"].append("e2e-behavior")
    for field in ("harness", "model", "pin_reason", "image"):
        document["execution_request"].pop(field, None)
    task_id = submit_and_start(client, document)

    await supervisor.tick()
    midway = client.get(f"/v1/tasks/{task_id}").json()
    first_events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    assert midway["executions"][0]["attempts"][0]["exit_class"] == "quota_exhausted"
    assert midway["state"] == "scheduled", [
        (event["kind"], event["payload"]) for event in first_events
    ]
    first, second = midway["executions"][0]["attempts"]
    assert first["exit_class"] == "quota_exhausted"
    assert first["image"] == worker_image
    assert second["resume_from_remote"] is True
    second_image = _promote_second_tag(ctx, worker_image)

    await run_until(
        supervisor,
        client,
        task_id,
        {"accepted", "pre_pr_gates_failed", "gates_passed"},
        max_ticks=10,
        pause=0.2,
    )
    final = client.get(f"/v1/tasks/{task_id}").json()
    attempts = final["executions"][0]["attempts"]
    assert attempts[1]["state"] == "succeeded"
    assert attempts[1]["image"] == second_image
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    reroute = next(event for event in events if event["kind"] == "task_rerouted")
    assert reroute["payload"]["wip_commit_sha"]
    prepared = [event for event in events if event["kind"] == "workspace_prepared"]
    assert prepared[-1]["payload"]["started_from"] == "origin/crucible/E2E-C6B"
    # b-script-success refuses to run unless this file is in its checkout, so its
    # succeeded state above proves reroute continuity. The pushed final branch proves
    # the same checkpoint remains in the delivered history.
    final_file = subprocess.run(
        ["git", "--git-dir", url, "show", "crucible/E2E-C6B:src/quota-checkpoint.txt"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert final_file == "quota checkpoint\n"
