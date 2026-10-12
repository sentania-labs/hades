"""FDY-0633 (ADR 0033): Kubernetes attempts use Pod-owned scratch."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec, scripts
from crucible.adapters.execution.kubernetes import (
    CHECKOUT_POLICY_PREFIX,
    COLLECT_MODE_FILE,
    KubernetesConfig,
    KubernetesProvider,
)
from crucible.ports.execution import WORK_MOUNT, ObservationState
from crucible.ports.github import InstallationToken
from crucible.ports.harness import MountMode
from tests.unit.kubernetes_fixtures import build, fake_resolver, pod_of, spec

PREVIOUS = "01PREVIOUSATTEMPT000000000"
HEAD = "a" * 40
BUNDLE = b"# v2 git bundle\nsealed bytes of the previous attempt\n"


async def _worker(*, behavior: str = "success") -> tuple[Any, Any, Any, Any, Any]:
    api, _registry, provider = build()
    launch = spec()
    api.scripts[launch.attempt_id] = (behavior, 1)
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    return api, provider, launch, workspace, handle


def _containers(pod: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in pod["containers"]}


def _inits(pod: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in pod.get("initContainers") or []}


async def test_an_attempt_creates_no_persistent_volume_claim() -> None:
    api, _provider, _launch, _workspace, _handle = await _worker()
    assert not any(row["kind"] == "persistentvolumeclaims" for row in api.created)


async def test_agent_and_collector_share_the_worker_pods_scratch() -> None:
    api, _provider, _launch, _workspace, _handle = await _worker()
    pod = pod_of(api, "worker-")
    containers = _containers(pod)
    agent_mounts = {row["name"] for row in containers[k8sspec.CONTAINER_NAME]["volumeMounts"]}
    collector_mounts = {row["name"] for row in containers[k8sspec.ROLE_COLLECTOR]["volumeMounts"]}
    assert "ws" in agent_mounts & collector_mounts
    # The collector holds the scratch's root, where the provider reads and where the
    # release file is written; the agent's container never mounts that root.
    collector_paths = {
        row["mountPath"] for row in containers[k8sspec.ROLE_COLLECTOR]["volumeMounts"]
    }
    agent_paths = {row["mountPath"] for row in containers[k8sspec.CONTAINER_NAME]["volumeMounts"]}
    assert WORK_MOUNT in collector_paths
    assert WORK_MOUNT not in agent_paths


async def test_collection_runs_in_the_worker_pod_after_an_agent_timeout() -> None:
    api, provider, launch, workspace, handle = await _worker(behavior="hang")
    assert (await provider.observe(handle)).state is ObservationState.RUNNING
    await provider.terminate(handle, "drain")
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED
    collected = await provider.collect(handle, workspace, launch)
    assert api.collect_modes == [(launch.attempt_id, "normal")]
    assert collected.bundle is not None
    # The worker Pod was never deleted to end the agent: the scratch outlived it.
    assert not [name for kind, name in api.deleted if kind == "pods" and "worker" in name]


async def test_a_lost_worker_pod_still_uses_the_lost_attempt_path() -> None:
    _api, provider, _launch, _workspace, handle = await _worker(behavior="vanish")
    observation = await provider.observe(handle)
    if observation.state is ObservationState.RUNNING:
        observation = await provider.observe(handle)
    assert observation.state is ObservationState.LOST


async def test_a_quota_exit_releases_the_collector_as_a_checkpoint() -> None:
    """Finding 01M4QFDZX7KB9WE6NT34HTZVVD: the in-Pod collector learns the exit's
    classification before it packages the tree."""
    api, _registry, provider = build()
    launch = spec()
    api.scripts[launch.attempt_id] = ("quota", 1)
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    await provider.observe(handle)
    adapter = provider.harnesses.require(launch.harness)
    adapter.classify_exit = lambda *_: scripts_quota()  # type: ignore[method-assign]
    await provider.collect(handle, workspace, launch)
    assert api.collect_modes == [(launch.attempt_id, "quota")]
    collector = _containers(pod_of(api, "worker-"))[k8sspec.ROLE_COLLECTOR]
    wrapper, script = collector["command"][2], collector["command"][-1]
    assert COLLECT_MODE_FILE in wrapper
    assert f"'{COLLECT_MODE_FILE}'" in script and "QUOTA=1" in script


def scripts_quota() -> Any:
    from crucible.ports.harness import ExitClass  # noqa: PLC0415

    return ExitClass.QUOTA_EXHAUSTED


def test_the_collector_reads_its_quota_mode_at_run_time() -> None:
    rendered = scripts.collector_script(
        base_ref="main",
        work_branch="crucible/EX-0001",
        size_cap_bytes=1024,
        attempt_id="01A",
        quota_checkpoint_file="/crucible/work/.collect-mode",
    )
    assert "QUOTA=''" not in rendered
    assert "\"$(cat '/crucible/work/.collect-mode')\" = quota" in rendered
    fixed = scripts.collector_script(
        base_ref="main", work_branch="w", size_cap_bytes=1, quota_checkpoint=True
    )
    assert "QUOTA='1'" in fixed or "QUOTA=1" in fixed


async def test_a_credential_harness_keeps_the_workspace_preparer() -> None:
    """Finding 01M4QFDZX0VWJ7WXHWRFW6B4AX: the credential init containers are added
    after the checkout's, never in its place."""
    for mode in (MountMode.RO, MountMode.RW_NARROW):
        api, registry, provider = build(
            config=KubernetesConfig(
                poll_interval_seconds=0, launch_timeout_seconds=5, credential_modes={"codex": mode}
            )
        )
        image = "crucible-worker:codex-fake-succeed-2"
        registry.register(image, harness="codex", version="0.156.0")
        api.put_harness_secret(
            "hades-harness-codex",
            {"auth.json": json.dumps({"tokens": {"account_id": "<account>"}}).encode()},
        )
        launch = spec(harness="codex", image=image)
        workspace = await provider.prepare(launch)
        await provider.launch(workspace, launch)
        names = [row["name"] for row in pod_of(api, "worker-")["initContainers"]]
        assert names[0] == k8sspec.WORKER_PREPARE_CONTAINER, mode
        if mode is MountMode.RW_NARROW:
            assert k8sspec.CREDENTIAL_INIT_CONTAINER in names


def _policies(api: Any) -> dict[str, str]:
    return {
        str(row["name"]): json.dumps(row["body"])
        for row in api.created
        if row["kind"] == "networkpolicies" and "worker" in json.dumps(row["body"]["spec"])
    }


async def test_the_worker_pod_may_reach_the_repository_for_its_own_checkout() -> None:
    """Finding 01M4QFDZX3JDW8KZG84VFGXJ0A: the init container clones from the remote,
    so the worker Pod is granted the git hosts by a checkout policy of its own, which is
    removed once the init container has run; the worker's own policy is unchanged."""
    api, _registry, provider = build()
    policy = {
        "images": {"allowlist": ["crucible-worker:*"]},
        "network": {"mode": "egress-proxy", "egress_allowlist": ["pypi.org"]},
        "resources": {"cpus": 2, "memory": "4GiB"},
        "limits": {"grace_seconds": 30},
    }
    launch = spec(policy=policy)
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    rendered = _policies(api)
    checkout_name = k8sspec.object_name(CHECKOUT_POLICY_PREFIX, launch.attempt_id)
    worker_name = k8sspec.object_name("np-worker", launch.attempt_id)
    github = fake_resolver("github.com")[0]
    assert github in rendered[checkout_name]
    assert github not in rendered[worker_name]
    assert fake_resolver("pypi.org")[0] in rendered[worker_name]
    # The init container is told the checkout's addresses too (hades #191).
    aliases = pod_of(api, "worker-")["hostAliases"]
    assert any("github.com" in row["hostnames"] for row in aliases)
    # The agent is still told only its own allowlist.
    env = {
        row["name"]: row.get("value")
        for row in _containers(pod_of(api, "worker-"))[k8sspec.CONTAINER_NAME]["env"]
    }
    assert env["CRUCIBLE_EGRESS_ALLOWLIST"] == "pypi.org"
    await provider.observe(handle)
    assert ("networkpolicies", checkout_name) in api.deleted
    assert ("networkpolicies", worker_name) not in api.deleted


class _Store:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    def put(self, content: bytes) -> Any:
        raise NotImplementedError

    def get(self, path: str) -> bytes:
        return self.blobs[path]

    def exists(self, path: str) -> bool:
        return path in self.blobs


async def test_a_correction_delivers_its_sealed_bundle_into_the_worker_pod() -> None:
    """Finding 01M4QFDZX5DCRSX47CMC66C577: the worker's own checkout resumes from the
    sealed bundle, not from the remote work branch."""
    import hashlib  # noqa: PLC0415

    api, _registry, provider = build()
    provider.artifact_store = _Store({"blobs/bundle": BUNDLE})
    seal = hashlib.sha256(BUNDLE).hexdigest()
    launch = replace(
        spec(),
        role="correct",
        resume_bundle_attempt_id=PREVIOUS,
        resume_bundle_head=HEAD,
        resume_bundle_sha256=seal,
        resume_bundle_artifact="blobs/bundle",
    )
    api.scripts[launch.attempt_id] = ("success", 1)
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    prepare = _inits(pod_of(api, "worker-"))[k8sspec.WORKER_PREPARE_CONTAINER]
    script = prepare["command"][-1]
    assert scripts.RESUME_DELIVERY_MARKER in script
    assert f"'{seal}'" in script and f"'{HEAD}'" in script
    assert api.worker_deliveries == [launch.attempt_id]
    worker_pods = [name for name in api.exec_stdin if name.startswith("worker-")]
    assert worker_pods and BUNDLE in api.exec_stdin[worker_pods[0]]
    assert (await provider.observe(handle)).state in (
        ObservationState.RUNNING,
        ObservationState.EXITED,
    )


def _token() -> InstallationToken:
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    return InstallationToken(
        "<installation-token>",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        repository="acme/example",
        permissions={"contents": "read", "metadata": "read"},
    )


async def test_a_restarted_supervisor_still_mounts_the_checkout_token() -> None:
    """Finding 01M4QFDZX96J2W39JNPHCP17BY: whether the worker's checkout has a token
    is read from the cluster, not from the process that ran `prepare`."""
    api, registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch, checkout_token=_token())
    name = k8sspec.object_name("checkout", launch.attempt_id)
    assert api.secret_exists(name)
    restarted = KubernetesProvider(
        replace(provider.config, probe_image=launch.image),
        api,  # type: ignore[arg-type]
        registry,
        resolver=fake_resolver,
    )
    handle = await restarted.launch(workspace, launch)
    pod = pod_of(api, "worker-")
    prepare = _inits(pod)[k8sspec.WORKER_PREPARE_CONTAINER]
    assert "CRUCIBLE_TOKEN_FILE" in prepare["command"][-1]
    assert scripts.TOKEN_MOUNT in {row["mountPath"] for row in prepare["volumeMounts"]}
    for container in pod["containers"]:
        assert scripts.TOKEN_MOUNT not in json.dumps(container), container["name"]
    # Once the init container has run, nothing needs the Secret.
    await restarted.observe(handle)
    assert not api.secret_exists(name)


@pytest.mark.parametrize("private", [False, True])
async def test_a_public_checkout_mounts_no_token(private: bool) -> None:
    api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch, checkout_token=_token() if private else None)
    await provider.launch(workspace, launch)
    prepare = _inits(pod_of(api, "worker-"))[k8sspec.WORKER_PREPARE_CONTAINER]
    assert ("CRUCIBLE_TOKEN_FILE" in prepare["command"][-1]) is private
