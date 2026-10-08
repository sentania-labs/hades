"""hades #558, #85: Docker-provider parity for a declared Postgres test service.

The Kubernetes provider runs the service as a native sidecar on the Pod's loopback
(tests/unit/test_k8sspec_services.py). Here the same declaration becomes one more
container of the attempt that joins the worker's network namespace, so the worker is
told the same `CRUCIBLE_TEST_DATABASE_URL` on either provider. The stub daemon records
what was created and in which order; no container runs.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution.docker import LABEL_SERVICE, ROLE_SERVICE
from crucible.adapters.execution.dockerapi import DockerApiError
from crucible.domain.test_services import POSTGRES_IMAGE, TEST_DATABASE_ENV, TEST_DATABASE_URL
from crucible.ports.execution import LaunchSpec, ProviderError
from tests.unit.test_docker_provider import (
    DIGEST,
    LABELS,
    StubClient,
    provider,
    spec,
    workspace_for,
)

POSTGRES_DIGEST = "postgres@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"


class ServiceStub(StubClient):
    """The stub daemon with the service image present and the start order recorded."""

    def __init__(self, *, has_postgres: bool = True) -> None:
        super().__init__()
        self.has_postgres = has_postgres
        self.started: list[str] = []

    def inspect_image(self, reference: str) -> dict[str, Any]:
        if reference == POSTGRES_IMAGE:
            if not self.has_postgres:
                raise DockerApiError(404, f"No such image: {reference}")
            return {"Id": "sha256:" + "c" * 64, "RepoDigests": [POSTGRES_DIGEST], "Config": {}}
        self.inspections += 1
        return {
            "Id": "sha256:" + "a" * 64,
            "RepoDigests": [DIGEST],
            "Config": {"Labels": dict(LABELS)},
        }

    def start_container(self, container_id: str) -> None:
        self.started.append(container_id)


def _spec() -> LaunchSpec:
    launch = spec()
    return replace(launch, policy={**launch.policy, "services": [{"kind": "postgres"}]})


def _created(client: StubClient, name_prefix: str) -> dict[str, Any]:
    rows = [row for row in client.created if str(row["name"]).startswith(name_prefix)]
    assert len(rows) == 1, [row["name"] for row in client.created]
    return rows[0]


def _env(body: dict[str, Any]) -> dict[str, str]:
    return dict(entry.split("=", 1) for entry in body["Env"])


async def _launched(tmp_path: Path, client: ServiceStub) -> tuple[Any, Any, LaunchSpec]:
    docker = provider(tmp_path, client)
    launch = _spec()
    workspace = workspace_for(tmp_path, launch.attempt_id)
    handle = await docker.launch(workspace, launch)
    return docker, (workspace, handle), launch


async def test_the_worker_is_told_the_same_loopback_url_as_on_kubernetes(tmp_path: Path) -> None:
    client = ServiceStub()
    await _launched(tmp_path, client)
    worker = _created(client, "crucible-01ATTEMPT")["body"]
    assert _env(worker)[TEST_DATABASE_ENV] == TEST_DATABASE_URL
    assert (
        _env(worker)[TEST_DATABASE_ENV] == "postgresql://crucible:crucible@127.0.0.1:5432/crucible"
    )


async def test_the_service_is_an_extra_container_in_the_workers_network_namespace(
    tmp_path: Path,
) -> None:
    client = ServiceStub()
    await _launched(tmp_path, client)
    worker_id = f"container-{client.created.index(_created(client, 'crucible-01ATTEMPT')) + 1}"
    service = _created(client, "svc-postgres-01ATTEMPT")["body"]
    assert service["Image"] == POSTGRES_IMAGE
    # The worker's own loopback: the only address the worker is told, and one nothing
    # else on the workers network can reach.
    assert service["HostConfig"]["NetworkMode"] == f"container:{worker_id}"
    assert service["User"] == "1000:1000"
    assert _env(service) == {
        "PGDATA": "/var/lib/postgresql/data/pgdata",
        "POSTGRES_DB": "crucible",
        "POSTGRES_PASSWORD": "crucible",
        "POSTGRES_USER": "crucible",
    }
    labels = service["Labels"]
    assert labels["crucible.attempt"] == "01ATTEMPT0000000000000000A"
    assert labels["crucible.role"] == ROLE_SERVICE
    assert labels[LABEL_SERVICE] == "postgres"


async def test_the_service_container_is_hardened_like_every_container_of_13(
    tmp_path: Path,
) -> None:
    client = ServiceStub()
    await _launched(tmp_path, client)
    host = _created(client, "svc-postgres-01ATTEMPT")["body"]["HostConfig"]
    assert host["CapDrop"] == ["ALL"] and host["CapAdd"] == []
    assert host["ReadonlyRootfs"] is True and host["Privileged"] is False
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["Init"] is True
    assert host["Memory"] == 1024**3 and host["MemorySwap"] == 1024**3
    assert host["NanoCpus"] == 1_000_000_000
    assert set(host["Tmpfs"]) == {"/tmp", "/var/run/postgresql", "/var/lib/postgresql/data"}
    assert host["Tmpfs"]["/var/lib/postgresql/data"] == (
        f"rw,nosuid,nodev,size={1024**3},uid=1000,gid=1000,mode=0700"
    )
    assert host["Mounts"] == [] if "Mounts" in host else True


async def test_declared_resources_size_the_service_container(tmp_path: Path) -> None:
    client = ServiceStub()
    docker = provider(tmp_path, client)
    launch = spec()
    launch = replace(
        launch,
        policy={
            **launch.policy,
            "services": [
                {
                    "kind": "postgres",
                    "resources": {"cpus": 0.5, "memory": "512MiB", "storage": "256MiB"},
                }
            ],
        },
    )
    await docker.launch(workspace_for(tmp_path, launch.attempt_id), launch)
    host = _created(client, "svc-postgres-01ATTEMPT")["body"]["HostConfig"]
    assert host["NanoCpus"] == 500_000_000
    assert host["Memory"] == 512 * 1024**2
    assert f"size={256 * 1024**2}" in host["Tmpfs"]["/var/lib/postgresql/data"]


async def test_the_worker_starts_first_and_the_service_right_after_it(tmp_path: Path) -> None:
    """Docker lets a container join another's network namespace only once that one is
    running, so the order is the worker, then its service."""
    client = ServiceStub()
    await _launched(tmp_path, client)
    worker = client.created.index(_created(client, "crucible-01ATTEMPT")) + 1
    service = client.created.index(_created(client, "svc-postgres-01ATTEMPT")) + 1
    assert worker < service
    assert client.started == [f"container-{worker}", f"container-{service}"]


async def test_a_service_image_the_daemon_lacks_refuses_the_launch_and_removes_the_worker(
    tmp_path: Path,
) -> None:
    client = ServiceStub(has_postgres=False)
    docker = provider(tmp_path, client)
    launch = _spec()
    with pytest.raises(ProviderError, match=r"postgres service image .* is not available"):
        await docker.launch(workspace_for(tmp_path, launch.attempt_id), launch)
    assert client.started == []
    assert client.removed  # the created worker did not stay behind


async def test_no_declaration_creates_no_service_and_no_variable(tmp_path: Path) -> None:
    client = ServiceStub()
    docker = provider(tmp_path, client)
    launch = spec()
    await docker.launch(workspace_for(tmp_path, launch.attempt_id), launch)
    assert not any(str(row["name"]).startswith("svc-") for row in client.created)
    assert TEST_DATABASE_ENV not in _env(_created(client, "crucible-01ATTEMPT")["body"])


async def test_the_collected_evidence_records_the_services_and_the_digest(
    tmp_path: Path,
) -> None:
    client = ServiceStub()
    docker, (workspace, handle), launch = await _launched(tmp_path, client)
    outputs = await docker.collect(handle, workspace, launch)
    evidence = next(a for a in outputs.artifacts if a.name == "report/docker-launch.json")
    assert evidence.type == "run_evidence"
    document = json.loads(evidence.content)
    assert document["provider"] == "docker"
    assert document["image_digest"] == DIGEST
    (service,) = document["services"]
    assert service["kind"] == "postgres"
    assert service["container"] == "svc-postgres"
    assert service["image"] == POSTGRES_IMAGE
    assert service["image_digest"] == (
        "sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
    )
    assert service["image_id"] == POSTGRES_DIGEST
    assert service["env"] == TEST_DATABASE_ENV
    assert service["url"] == TEST_DATABASE_URL
    assert service["service_container"].startswith("container-")


async def test_an_attempt_without_a_service_collects_no_launch_evidence(tmp_path: Path) -> None:
    client = ServiceStub()
    docker = provider(tmp_path, client)
    launch = spec()
    workspace = workspace_for(tmp_path, launch.attempt_id)
    handle = await docker.launch(workspace, launch)
    outputs = await docker.collect(handle, workspace, launch)
    assert not any(a.name == "report/docker-launch.json" for a in outputs.artifacts)
