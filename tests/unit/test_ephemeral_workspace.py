"""FDY-0633: Kubernetes attempts use Pod-owned scratch."""

from __future__ import annotations

from typing import Any

from crucible.adapters.execution import k8sspec
from crucible.ports.execution import ObservationState
from tests.unit.kubernetes_fixtures import build, pod_of, spec


async def _worker(*, behavior: str = "success") -> tuple[Any, Any, Any, Any, Any]:
    api, _registry, provider = build()
    launch = spec()
    api.scripts[launch.attempt_id] = (behavior, 1)
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    return api, provider, launch, workspace, handle


async def test_an_attempt_creates_no_persistent_volume_claim() -> None:
    api, _provider, _launch, _workspace, _handle = await _worker()
    assert not any(row["kind"] == "persistentvolumeclaims" for row in api.created)


async def test_agent_and_collector_share_the_worker_pods_scratch() -> None:
    api, _provider, _launch, _workspace, _handle = await _worker()
    pod = pod_of(api, "worker-")
    containers = {row["name"]: row for row in pod["containers"]}
    agent_mounts = {row["name"] for row in containers[k8sspec.CONTAINER_NAME]["volumeMounts"]}
    collector_mounts = {row["name"] for row in containers[k8sspec.ROLE_COLLECTOR]["volumeMounts"]}
    assert "ws" in agent_mounts & collector_mounts


async def test_the_timeout_signal_still_releases_the_in_pod_collector() -> None:
    api, _provider, _launch, _workspace, _handle = await _worker(behavior="hang")
    pod = pod_of(api, "worker-")
    agent = next(row for row in pod["containers"] if row["name"] == k8sspec.CONTAINER_NAME)
    collector = next(row for row in pod["containers"] if row["name"] == "collector")
    assert "trap" in agent["command"][2]
    assert ".agent-finished" in agent["command"][2]
    assert ".agent-finished" in collector["command"][2]


async def test_a_lost_worker_pod_still_uses_the_lost_attempt_path() -> None:
    _api, provider, _launch, _workspace, handle = await _worker(behavior="vanish")
    observation = await provider.observe(handle)
    if observation.state is ObservationState.RUNNING:
        observation = await provider.observe(handle)
    assert observation.state is ObservationState.LOST
