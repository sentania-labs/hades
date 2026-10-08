"""hades #558, #85: a declared Postgres test service becomes a native sidecar of the
worker Job, read field by field from the rendered objects (k8sspec is pure, 26).

The worker image carries no Docker daemon, so `make test-integration` could not run in
a worker before this. A policy (or a contract under `execution_request`) declares
`services: [{kind: postgres}]`; the Job then carries one more init container with
`restartPolicy: Always`, a `pg_isready` probe, requests shaped by the policy's
fractions, and the worker's environment names the server on the Pod's own loopback.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.kubernetes import _observe_service_images, _terminated_init
from crucible.contracts.policy import parse_policy
from crucible.contracts.task_contract import TaskContractV1
from crucible.domain.test_services import (
    POSTGRES_IMAGE,
    TEST_DATABASE_ENV,
    TEST_DATABASE_URL,
    DeclaredService,
    declared_services,
    services_env,
)
from crucible.ports.execution import CleanupPolicy, LaunchSpec
from tests.fixtures import contract_document
from tests.unit.kubernetes_fixtures import build, pod_of, spec
from tests.unit.test_policy_schema import seeded_policy_v2

POLICY: dict[str, Any] = {
    "resources": {
        "cpus": 2,
        "memory": "4GiB",
        "cpu_request_fraction": 0.5,
        "memory_request_fraction": 0.25,
    },
    "limits": {"grace_seconds": 30},
    "services": [{"kind": "postgres"}],
}


def _env(container: dict[str, Any]) -> dict[str, str]:
    return {str(e["name"]): str(e["value"]) for e in container["env"]}


def _sidecar(pod: dict[str, Any]) -> dict[str, Any]:
    sidecars = [c for c in pod.get("initContainers") or [] if c["name"] == "svc-postgres"]
    assert len(sidecars) == 1, pod.get("initContainers")
    sidecar: dict[str, Any] = sidecars[0]
    return sidecar


def _rendered(policy: dict[str, Any], contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """The worker Pod `pod_spec` renders for a policy, through the declared services."""
    limits = k8sspec.limits_from_policy(policy)
    services = declared_services(policy, contract)
    return k8sspec.pod_spec(
        k8sspec.PodRequest(
            role=k8sspec.ROLE_WORKER,
            image="crucible-worker:x@sha256:" + "a" * 64,
            command=["crucible-harness"],
            limits=limits,
            env={"HOME": "/home/worker", **services_env(services)},
            services=services,
        )
    )


# ----- the declaration ------------------------------------------------------------


def test_a_policy_declares_the_postgres_service_with_the_ci_digest_by_default() -> None:
    document = seeded_policy_v2()
    document["services"] = [{"kind": "postgres"}]
    policy = parse_policy(document)
    assert [s.kind for s in policy.services] == ["postgres"]
    assert policy.services[0].image == POSTGRES_IMAGE
    assert policy.services[0].image.endswith(
        "@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
    )


def test_a_policy_without_services_declares_none() -> None:
    policy = parse_policy(seeded_policy_v2())
    assert policy.services == []
    assert declared_services(policy.model_dump(mode="json")) == ()


@pytest.mark.parametrize(
    "image",
    ["postgres:16", "postgres:16@sha256:abc", "@sha256:" + "f" * 64, "postgres@md5:" + "f" * 64],
)
def test_a_service_image_must_be_pinned_by_digest(image: str) -> None:
    document = seeded_policy_v2()
    document["services"] = [{"kind": "postgres", "image": image}]
    with pytest.raises(ValueError, match="pinned by digest"):
        parse_policy(document)


def test_only_postgres_is_a_known_service_kind() -> None:
    document = seeded_policy_v2()
    document["services"] = [{"kind": "redis"}]
    with pytest.raises(ValueError):
        parse_policy(document)


def test_a_kind_is_declared_at_most_once() -> None:
    document = seeded_policy_v2()
    document["services"] = [{"kind": "postgres"}, {"kind": "postgres"}]
    with pytest.raises(ValueError, match="more than once"):
        parse_policy(document)


def test_a_contract_declares_the_service_under_execution_request() -> None:
    document = contract_document()
    document["execution_request"]["services"] = [
        {"kind": "postgres", "resources": {"cpus": 0.5, "memory": "512MiB"}}
    ]
    contract = TaskContractV1.model_validate(document)
    assert contract.execution_request.services is not None
    assert contract.execution_request.services[0].resources.cpus == 0.5
    services = declared_services({}, contract.model_dump(mode="json"))
    assert services == (
        DeclaredService(kind="postgres", image=POSTGRES_IMAGE, cpus=0.5, memory="512MiB"),
    )


def test_the_contract_entry_replaces_the_policys_and_enabled_false_drops_it() -> None:
    policy = {"services": [{"kind": "postgres", "resources": {"cpus": 1}}]}
    replaced = {"execution_request": {"services": [{"kind": "postgres", "resources": {"cpus": 2}}]}}
    assert declared_services(policy, replaced)[0].cpus == 2
    dropped = {"execution_request": {"services": [{"kind": "postgres", "enabled": False}]}}
    assert declared_services(policy, dropped) == ()
    assert declared_services(policy, {"execution_request": {}}) == declared_services(policy)


# ----- the rendered Job ---------------------------------------------------------------


def test_the_sidecar_is_a_native_sidecar_init_container() -> None:
    sidecar = _sidecar(_rendered(POLICY))
    assert sidecar["image"] == POSTGRES_IMAGE
    # KEP-753: an init container with restartPolicy Always is started before the
    # worker, restarted if it dies, and stopped once the worker exits, so the Job
    # completes on the worker's exit and the database never keeps it alive.
    assert sidecar["restartPolicy"] == "Always"
    pod = _rendered(POLICY)
    assert pod["restartPolicy"] == "Never"
    assert [c["name"] for c in pod["containers"]] == [k8sspec.CONTAINER_NAME]


def test_the_sidecar_has_pg_isready_startup_and_readiness_probes_on_the_loopback() -> None:
    sidecar = _sidecar(_rendered(POLICY))
    command = ["pg_isready", "-h", "127.0.0.1", "-p", "5432", "-U", "crucible", "-d", "crucible"]
    assert sidecar["startupProbe"]["exec"]["command"] == command
    assert sidecar["readinessProbe"]["exec"]["command"] == command
    assert sidecar["startupProbe"]["failureThreshold"] == 60
    assert sidecar["readinessProbe"]["failureThreshold"] == 3
    assert sidecar["ports"] == [{"name": "postgres", "containerPort": 5432, "protocol": "TCP"}]


def test_the_sidecar_requests_follow_the_policys_fractions() -> None:
    sidecar = _sidecar(_rendered(POLICY))
    assert sidecar["resources"]["limits"] == {
        "cpu": "1000m",
        "memory": str(1024**3),
        "ephemeral-storage": "2Gi",
    }
    # Half the CPU limit and a quarter of the memory limit, as the worker's are.
    assert sidecar["resources"]["requests"] == {"cpu": "500m", "memory": str(1024**3 // 4)}
    worker = _rendered(POLICY)["containers"][0]
    assert worker["resources"]["requests"] == {"cpu": "1000m", "memory": str(4 * 1024**3 // 4)}


def test_declared_resources_size_the_sidecar() -> None:
    policy = copy.deepcopy(POLICY)
    policy["services"] = [
        {"kind": "postgres", "resources": {"cpus": 0.5, "memory": "512MiB", "storage": "3Gi"}}
    ]
    pod = _rendered(policy)
    sidecar = _sidecar(pod)
    assert sidecar["resources"]["limits"]["cpu"] == "500m"
    assert sidecar["resources"]["limits"]["memory"] == str(512 * 1024**2)
    assert sidecar["resources"]["requests"] == {"cpu": "250m", "memory": str(512 * 1024**2 // 4)}
    data = next(v for v in pod["volumes"] if v["name"] == "svc-postgres-data")
    assert data == {"name": "svc-postgres-data", "emptyDir": {"sizeLimit": "3Gi"}}


def test_the_sidecar_keeps_26s_container_security_context() -> None:
    sidecar = _sidecar(_rendered(POLICY))
    assert sidecar["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert sidecar["terminationMessagePolicy"] == "FallbackToLogsOnError"


def test_the_sidecar_writes_only_to_its_own_volumes() -> None:
    pod = _rendered(POLICY)
    sidecar = _sidecar(pod)
    assert sidecar["volumeMounts"] == [
        {"name": "svc-postgres-data", "mountPath": "/var/lib/postgresql/data"},
        {"name": "svc-postgres-run", "mountPath": "/var/run/postgresql"},
        {"name": "svc-postgres-tmp", "mountPath": "/tmp"},
    ]
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["svc-postgres-data"] == {
        "name": "svc-postgres-data",
        "emptyDir": {"sizeLimit": "1Gi"},
    }
    assert volumes["svc-postgres-run"]["emptyDir"]["medium"] == "Memory"
    assert volumes["svc-postgres-tmp"]["emptyDir"]["medium"] == "Memory"
    # The worker's own volumes are untouched and none of the sidecar's reach it.
    worker_mounts = {m["name"] for m in pod["containers"][0]["volumeMounts"]}
    assert not any(name.startswith("svc-") for name in worker_mounts)


def test_the_sidecar_environment_creates_the_role_and_database_the_url_names() -> None:
    sidecar = _sidecar(_rendered(POLICY))
    assert _env(sidecar) == {
        "PGDATA": "/var/lib/postgresql/data/pgdata",
        "POSTGRES_DB": "crucible",
        "POSTGRES_PASSWORD": "crucible",
        "POSTGRES_USER": "crucible",
    }


def test_the_worker_container_is_told_the_loopback_url() -> None:
    worker = _rendered(POLICY)["containers"][0]
    assert (
        _env(worker)[TEST_DATABASE_ENV] == "postgresql://crucible:crucible@127.0.0.1:5432/crucible"
    )
    assert TEST_DATABASE_URL == "postgresql://crucible:crucible@127.0.0.1:5432/crucible"


def test_no_declaration_renders_no_sidecar_and_no_variable() -> None:
    policy = {k: v for k, v in POLICY.items() if k != "services"}
    pod = _rendered(policy)
    assert "initContainers" not in pod
    assert TEST_DATABASE_ENV not in _env(pod["containers"][0])
    assert not any(v["name"].startswith("svc-") for v in pod["volumes"])


def test_the_sidecar_comes_after_the_other_init_containers() -> None:
    limits = k8sspec.limits_from_policy(POLICY)
    services = declared_services(POLICY)
    pod = k8sspec.pod_spec(
        k8sspec.PodRequest(
            role=k8sspec.ROLE_WORKER,
            image="crucible-worker:x@sha256:" + "a" * 64,
            command=["crucible-harness"],
            limits=limits,
            init_containers=[{"name": k8sspec.CREDENTIAL_INIT_CONTAINER, "image": "x"}],
            services=services,
        )
    )
    assert [c["name"] for c in pod["initContainers"]] == [
        k8sspec.CREDENTIAL_INIT_CONTAINER,
        "svc-postgres",
    ]


# ----- through the provider (the fake API) ---------------------------------------


def _spec_with_services(**overrides: Any) -> LaunchSpec:
    launch = spec(**overrides)
    policy = {**launch.policy, "services": [{"kind": "postgres"}]}
    return replace(launch, policy=policy)


async def test_the_worker_job_carries_the_sidecar_and_the_variable() -> None:
    api, _registry, provider = build()
    launch = _spec_with_services()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    job = api.objects[("jobs", handle.ref)].body
    pod = job["spec"]["template"]["spec"]
    sidecar = _sidecar(pod)
    assert sidecar["restartPolicy"] == "Always"
    worker = pod["containers"][0]
    assert _env(worker)[TEST_DATABASE_ENV] == TEST_DATABASE_URL
    # No NetworkPolicy change: the worker's policy is the same object it was without
    # a service, since loopback traffic never leaves the Pod.
    policies = [
        body
        for (kind, _name), obj in api.objects.items()
        if kind == "networkpolicies"
        for body in [obj.body]
        if body["spec"]["podSelector"]["matchLabels"].get(k8sspec.LABEL_ROLE) == "worker"
    ]
    assert len(policies) == 1
    rules = json.dumps(policies[0]["spec"]["egress"])
    assert "5432" not in rules and "127.0.0.1" not in rules


async def test_the_launch_evidence_records_the_services_and_the_digest() -> None:
    _api, _registry, provider = build()
    launch = _spec_with_services()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    while (await provider.observe(handle)).state.value == "running":
        pass
    outputs = await provider.collect(handle, workspace, launch)
    evidence = next(a for a in outputs.artifacts if a.name == "report/kubernetes-launch.json")
    document = json.loads(evidence.content)
    assert document["services"] == [
        {
            "kind": "postgres",
            "container": "svc-postgres",
            "image": POSTGRES_IMAGE,
            "image_digest": (
                "sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
            ),
            "env": TEST_DATABASE_ENV,
            "url": TEST_DATABASE_URL,
            "cpus": 1.0,
            "memory": "1GiB",
            "storage": "1Gi",
            "image_id": "",
        }
    ]
    await provider.cleanup(workspace, CleanupPolicy.KEEP_DIFF_ONLY, launch)


async def test_the_launch_evidence_records_no_services_when_none_are_declared() -> None:
    api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    while (await provider.observe(handle)).state.value == "running":
        pass
    outputs = await provider.collect(handle, workspace, launch)
    evidence = next(a for a in outputs.artifacts if a.name == "report/kubernetes-launch.json")
    assert json.loads(evidence.content)["services"] == []
    pod = pod_of(api, "worker-")
    assert "initContainers" not in pod


# ----- the sidecar's exit is never an init failure ------------------------------------


def test_a_stopped_sidecar_is_not_read_as_an_init_container_that_failed() -> None:
    """The kubelet stops the sidecar after the worker exits and its exit code is the
    server's shutdown; only a real init step's non-zero exit counts (26)."""
    stopped_sidecar = {
        "name": "svc-postgres",
        "state": {"terminated": {"exitCode": 137, "reason": "Error"}},
    }
    assert _terminated_init({"initContainerStatuses": [stopped_sidecar]}) is None
    failed_seed = {
        "name": k8sspec.CREDENTIAL_INIT_CONTAINER,
        "state": {"terminated": {"exitCode": 3, "reason": "Error"}},
    }
    found = _terminated_init({"initContainerStatuses": [stopped_sidecar, failed_seed]})
    assert found is not None
    assert found["containerName"] == k8sspec.CREDENTIAL_INIT_CONTAINER
    assert found["exitCode"] == 3


async def test_the_live_sidecar_image_is_recorded_once_the_pod_reports_it() -> None:
    _api, _registry, provider = build()
    launch = _spec_with_services()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    launched = provider._launched[launch.attempt_id]
    status = {
        "initContainerStatuses": [
            {"name": "svc-postgres", "imageID": "docker.io/library/postgres@sha256:" + "f" * 64},
            {"name": k8sspec.CREDENTIAL_INIT_CONTAINER, "imageID": "x@sha256:" + "a" * 64},
        ]
    }
    _observe_service_images(launched, status)
    assert launched.service_images == {
        "svc-postgres": "docker.io/library/postgres@sha256:" + "f" * 64
    }
    while (await provider.observe(handle)).state.value == "running":
        pass
    outputs = await provider.collect(handle, workspace, launch)
    evidence = next(a for a in outputs.artifacts if a.name == "report/kubernetes-launch.json")
    (service,) = json.loads(evidence.content)["services"]
    assert service["image_id"] == "docker.io/library/postgres@sha256:" + "f" * 64
