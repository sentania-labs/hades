"""hades #184: the self-hosting policy uploads through the policies API, and a task under
it routes to the Hermes gateway model the operator picked, exactly as one under
default-software does. Nothing in submission, start or selection reads the policy's
name."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import HarnessImage
from tests.fixtures import FakeClock, contract_document
from tests.integration.conftest import put_seeded_policy_in_force

pytestmark = pytest.mark.integration

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "policies" / "hades-self-hosting.yaml"
GATEWAY = "http://litellm.litellm.svc.cluster.local:4000/v1"


def _admin(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['admin']}"}


def _operator_picks_fast(client: TestClient, tokens: dict[str, str]) -> dict[str, Any]:
    """The routing half of what the gateway page leaves behind once the operator picks
    the gateway's `fast` model for Hermes: a routing version with that entry enabled
    (admin/gateway.py, publish_routing). The caller writes the default-software version
    that names it."""
    in_force = client.get("/v1/routing/usage", params={"policy": "default-software"}).json()
    ref = in_force["routing_policy"]
    routing: dict[str, Any] = client.get(f"/v1/routing/{ref['name']}/{ref['version']}").json()[
        "document"
    ]
    routing = copy.deepcopy(routing)
    routing["version"] = ref["version"] + 100
    local = next(m for m in routing["models"] if m["endpoint"] == "local")
    routing["models"].append(
        {
            **local,
            "model": "fast",
            "endpoint_url": GATEWAY,
            "capability": "mid",
            "speed": "fast",
            "enabled": True,
            "disabled_reason": None,
        }
    )
    # Only the picked model is enabled, as the gateway page leaves it with one pick.
    for model in routing["models"]:
        if model["model"] != "fast":
            model["enabled"] = False
            model["disabled_reason"] = "not picked in this test"
    put = client.put(
        f"/v1/routing/{routing['name']}/{routing['version']}", json=routing, headers=_admin(tokens)
    )
    assert put.status_code == 200, put.text
    return routing


def _policy_in_force(ctx: AppContext) -> tuple[int, dict[str, Any]]:
    with ctx.uow_factory() as uow:
        newest = max(uow.policies.list_versions("default-software"), key=lambda p: p.version)
        return newest.version, copy.deepcopy(newest.document)


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_the_self_hosting_policy_routes_to_the_picked_hermes_model(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    supervisor: Supervisor,
    tokens: dict[str, str],
) -> None:
    put_seeded_policy_in_force(ctx)
    routing = _operator_picks_fast(client, tokens)
    version, default_software = _policy_in_force(ctx)
    default_software["version"] = version + 1
    default_software["routing"] = {
        "policy": {"name": routing["name"], "version": routing["version"]}
    }
    put = client.put(
        f"/v1/policies/default-software/{version + 1}",
        json=default_software,
        headers=_admin(tokens),
    )
    assert put.status_code == 200, put.text

    # The documented upload (docs/deployment.md): read the routing policy the
    # default-software in force names, write it into the example, PUT the example.
    usage = client.get("/v1/routing/usage", params={"policy": "default-software"}).json()
    assert usage["routing_policy"] == {"name": routing["name"], "version": routing["version"]}
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["routing"] = {"policy": usage["routing_policy"]}
    put = client.put("/v1/policies/hades-self-hosting/1", json=document, headers=_admin(tokens))
    assert put.status_code == 200, put.text
    stored = client.get("/v1/policies/hades-self-hosting/1").json()["document"]
    assert stored["repository"]["required_checks"] == ["make lint", "make test-unit", "make scan"]
    assert "gitleaks" in stored["repository"]["required_programs"]

    with ctx.uow_factory() as uow:
        uow.harness_images.put(
            HarnessImage(
                harness="hermes",
                digest="sha256:hermes-fake-succeed",
                reference="crucible-worker:fake-succeed",
                version="0.19.0",
                updated_at=clock.now(),
                updated_by="tests",
                reason="hades #184 routing fixture",
            )
        )
        uow.commit()

    contract = contract_document(external_id="HADES-184")
    contract["repository"]["work_branch"] = "crucible/HADES-184"
    contract["policy"] = {"name": "hades-self-hosting", "version": 1}
    contract["required_verification"] = [
        {"id": "V1", "command": "make lint", "expect_exit": 0},
        {"id": "V2", "command": "make test-unit", "expect_exit": 0},
        {"id": "V3", "command": "make scan", "expect_exit": 0},
        {"id": "V9", "command": "test -f made-by-the-worker", "expect_exit": 0},
    ]
    for field in ("harness", "model", "pin_reason", "image"):
        contract["execution_request"].pop(field, None)

    # A contract that still names `make test` is refused only for the check it lacks.
    missing = copy.deepcopy(contract)
    missing["required_verification"][1]["command"] = "make test"
    refused = client.post("/v1/tasks", json=missing)
    assert refused.status_code == 422, refused.text
    assert "missing the policy-required check 'make test-unit'" in refused.text

    submitted = client.post("/v1/tasks", json=contract)
    assert submitted.status_code == 201, submitted.text
    task_id = str(submitted.json()["id"])
    started = client.post(
        f"/v1/tasks/{task_id}/start", json={"provider": "fake", "policy_version": 1}
    )
    assert started.status_code == 200, started.text

    await supervisor.tick()
    task = client.get(f"/v1/tasks/{task_id}").json()
    attempt = task["executions"][0]["attempts"][0]
    assert (attempt["harness"], attempt["model"]) == ("hermes", "fast"), attempt
    assert attempt["image"] == "crucible-worker:fake-succeed"
