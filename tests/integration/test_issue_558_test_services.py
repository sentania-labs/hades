"""hades #558, #85: a declared test service travels from the uploaded policy and the
submitted contract to the provider's launch spec, through the API and the supervisor.

The fake provider renders no container, so the Docker and Kubernetes shapes are proved
by their unit tiers (tests/unit/test_docker_services.py, test_k8sspec_services.py);
what this tier proves is the path to them: the policy snapshot and the contract an
execution launches with carry the declaration exactly as `declared_services` reads it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.policies import put_policy
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import Principal, Role
from crucible.domain.test_services import (
    POSTGRES_IMAGE,
    TEST_DATABASE_ENV,
    TEST_DATABASE_URL,
    DeclaredService,
    declared_services,
    services_env,
)
from tests.fixtures import contract_document
from tests.integration.conftest import put_seeded_policy_in_force

pytestmark = pytest.mark.integration

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "policies" / "hades-self-hosting.yaml"


def _admin(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['admin']}"}


def _policy_with_services(ctx: AppContext, services: list[dict[str, Any]]) -> int:
    """A new default-software version, the one in force plus `services`."""
    put_seeded_policy_in_force(ctx)
    with ctx.uow_factory() as uow:
        newest = max(uow.policies.list_versions("default-software"), key=lambda p: p.version)
        version = newest.version + 1
        document = copy.deepcopy(newest.document)
        document["version"] = version
        document["services"] = services
        put_policy(
            uow,
            ctx.clock,
            principal=Principal(
                id="tests", name="tests", role=Role.ADMIN, created_at=ctx.clock.now()
            ),
            name="default-software",
            version=version,
            document=document,
            reason="hades #558: the integration tier's Postgres beside the worker",
        )
        uow.commit()
    return version


async def _launched_spec(
    client: TestClient,
    provider: FakeProvider,
    supervisor: Supervisor,
    *,
    external_id: str,
    policy_version: int,
    request_services: list[dict[str, Any]] | None,
) -> Any:
    document = contract_document(external_id=external_id)
    document["repository"]["work_branch"] = f"crucible/{external_id}"
    document["policy"] = {"name": "default-software", "version": policy_version}
    document["execution_request"]["image"] = "crucible-worker:fake-succeed"
    if request_services is not None:
        document["execution_request"]["services"] = request_services
    response = client.post("/v1/tasks", json=document)
    assert response.status_code == 201, response.text
    task_id = str(response.json()["id"])
    response = client.post(
        f"/v1/tasks/{task_id}/start",
        json={
            "provider": "fake",
            "image": "crucible-worker:fake-succeed",
            "policy_version": policy_version,
        },
    )
    assert response.status_code == 200, response.text
    before = set(provider._workers)
    for _ in range(3):
        await supervisor.tick()
        if set(provider._workers) - before:
            break
    (attempt_id,) = set(provider._workers) - before
    return provider._workers[attempt_id].spec


async def test_the_policys_service_reaches_the_launch_spec(
    client: TestClient,
    ctx: AppContext,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    version = _policy_with_services(ctx, [{"kind": "postgres"}])
    stored = client.get(f"/v1/policies/default-software/{version}").json()["document"]
    assert stored["services"] == [
        {
            "kind": "postgres",
            "image": POSTGRES_IMAGE,
            "enabled": True,
            "resources": {"cpus": 1.0, "memory": "1GiB", "storage": "1Gi"},
        }
    ]
    spec = await _launched_spec(
        client,
        provider,
        supervisor,
        external_id="EX-558-POLICY",
        policy_version=version,
        request_services=None,
    )
    services = declared_services(spec.policy, spec.contract)
    assert services == (DeclaredService(kind="postgres", image=POSTGRES_IMAGE),)
    assert services_env(services) == {TEST_DATABASE_ENV: TEST_DATABASE_URL}
    assert services[0].digest == (
        "sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
    )


async def test_the_contracts_entry_replaces_or_drops_the_policys(
    client: TestClient,
    ctx: AppContext,
    provider: FakeProvider,
    supervisor: Supervisor,
) -> None:
    version = _policy_with_services(ctx, [{"kind": "postgres"}])
    replaced = await _launched_spec(
        client,
        provider,
        supervisor,
        external_id="EX-558-REPLACE",
        policy_version=version,
        request_services=[{"kind": "postgres", "resources": {"cpus": 0.5, "memory": "512MiB"}}],
    )
    (service,) = declared_services(replaced.policy, replaced.contract)
    assert (service.cpus, service.memory, service.storage) == (0.5, "512MiB", "1Gi")
    dropped = await _launched_spec(
        client,
        provider,
        supervisor,
        external_id="EX-558-DROP",
        policy_version=version,
        request_services=[{"kind": "postgres", "enabled": False}],
    )
    assert declared_services(dropped.policy, dropped.contract) == ()
    assert TEST_DATABASE_ENV not in services_env(())


def test_a_contract_with_an_unpinned_service_image_is_refused(
    client: TestClient, ctx: AppContext
) -> None:
    put_seeded_policy_in_force(ctx)
    document = contract_document(external_id="EX-558-UNPINNED")
    document["repository"]["work_branch"] = "crucible/EX-558-UNPINNED"
    document["execution_request"]["services"] = [{"kind": "postgres", "image": "postgres:16"}]
    response = client.post("/v1/tasks", json=document)
    assert response.status_code == 422, response.text
    assert "pinned by digest" in response.text


def test_the_shipped_self_hosting_policy_declares_the_service_and_uploads(
    client: TestClient, ctx: AppContext, tokens: dict[str, str]
) -> None:
    """The example this repository runs under (hades #184) enables the service, so the
    next self-hosting task can run `make test-integration` in the worker."""
    put_seeded_policy_in_force(ctx)
    usage = client.get("/v1/routing/usage", params={"policy": "default-software"}).json()
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["routing"] = {"policy": usage["routing_policy"]}
    assert document["services"] == [{"kind": "postgres"}]
    # The example's number is a template (its header says so); an installation uploads
    # it as its next free number, which here is the next after whatever earlier tests
    # left behind.
    with ctx.uow_factory() as uow:
        versions = [p.version for p in uow.policies.list_versions("hades-self-hosting")]
    document["version"] = max(versions, default=1) + 1
    put = client.put(
        f"/v1/policies/hades-self-hosting/{document['version']}",
        json=document,
        headers=_admin(tokens),
    )
    assert put.status_code == 200, put.text
    stored = client.get(f"/v1/policies/hades-self-hosting/{document['version']}").json()["document"]
    assert stored["services"][0]["kind"] == "postgres"
    assert stored["services"][0]["image"] == POSTGRES_IMAGE
    assert declared_services(stored) == (DeclaredService(kind="postgres", image=POSTGRES_IMAGE),)
