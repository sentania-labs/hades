"""The Kubernetes end-to-end tier against a real kind API, kubelet, CNI and PVC."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import subprocess
import time
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution import k8sspec
from crucible.adapters.execution import kubernetes as kubernetes_module
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.execution.k8sapi import (
    KubernetesApiError,
    KubernetesClient,
    kubeconfig_access,
)
from crucible.adapters.execution.k8spublisher import KubernetesPublisher
from crucible.adapters.execution.k8sregistry import CraneRegistryClient
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.adapters.github.appauth import AppAuthenticator, AppConfig
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.transport import RestTransport
from crucible.adapters.harness.registry import default_registry as application_harnesses
from crucible.adapters.harness.script import ScriptHarnessAdapter
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application import supervisor as supervisor_module
from crucible.application.admin.context import AdminContext
from crucible.application.admin.login import FLOWS, LoginFlow
from crucible.application.auth import mint_token
from crucible.application.delivery_tick import DeliveryConfig
from crucible.application.harnesses import HarnessRegistry
from crucible.application.repositories import register_repository
from crucible.application.supervisor import Supervisor
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.egress_probe import find_probe
from crucible.domain.entities import Role
from crucible.ports.execution import (
    REPORT_MOUNT,
    CleanupPolicy,
    LaunchRefusedError,
    LaunchSpec,
    LogOffset,
    ObservationState,
    ProviderError,
)
from crucible.ports.harness import (
    AdapterLaunch,
    AuthFile,
    CredentialSpec,
    HarnessCapabilities,
    LaunchContext,
    MountMode,
    TranscriptFormat,
)
from tests.e2e.conftest import (
    e2e_contract,
    event_kinds,
    gate_results,
    register,
    run_until,
    submit_and_start,
)
from tests.e2e.policy import e2e_policy_document, e2e_routing_document
from tests.e2e.repo import make_origin
from tests.e2e.test_class_routing import _install_class_policy
from tests.e2e.test_isolation import MUST_BE_REFUSED
from tests.fixtures import contract_document, promote_for_test
from tests.integration.fake_github import FakeGitHubServer
from tests.login_captures import replay_script

pytestmark = [
    pytest.mark.e2e,
    # A kind case waits on pods, image pulls and the supervisor's own timeouts; the
    # cluster's first case also pays for the session fixtures (issue 192).
    pytest.mark.timeout(900),
    pytest.mark.skipif(not os.environ.get("CRUCIBLE_E2E_KIND"), reason="needs make e2e-kind"),
]

ATTEMPT_PREFIX = "01KIND00000000000000000"
# How long a supervisor-driven test waits for a launch to reach a running worker.
LAUNCH_DEADLINE_SECONDS = 240


class RecordingKubernetesClient(KubernetesClient):
    """Keep the last real readiness log after its short-lived Pod is deleted."""

    def pod_log(self, name: str, **kwargs: Any) -> Any:
        frames = super().pod_log(name, **kwargs)
        if name.startswith("crucible-canary-"):
            path = Path(os.environ["CRUCIBLE_E2E_KIND_CANARY_LOG"])
            path.write_bytes(b"".join(frame.payload for frame in frames))
        return frames


class CredentialScriptAdapter(ScriptHarnessAdapter):
    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=self.name,
            mount_target="/home/worker/.script-harness",
            auth_files=(AuthFile("auth.json", json=True),),
            minimum_mode=MountMode.RO,
        )


@pytest.fixture(scope="session")
def api() -> KubernetesClient:
    return RecordingKubernetesClient(
        kubeconfig_access(os.environ["CRUCIBLE_E2E_KIND_KUBECONFIG"]),
        "hades-workers",
        timeout=15,
    )


@pytest.fixture(scope="session")
def registry() -> CraneRegistryClient:
    # The real crane against the tier's own registry on host loopback, which crane
    # reaches over plain HTTP only because it is localhost (e2e-kind.sh puts the pinned
    # binary on PATH).
    return CraneRegistryClient(timeout=15)


@pytest.fixture
def provider(api: KubernetesClient, registry: CraneRegistryClient) -> KubernetesProvider:
    return _provider(api, registry)


def _provider(
    api: KubernetesClient,
    registry: CraneRegistryClient,
    *,
    harnesses: HarnessRegistry | None = None,
    resolver: Any = None,
    **overrides: Any,
) -> KubernetesProvider:
    settings: dict[str, Any] = {
        "storage_class": "standard",
        "workspace_size": "64Mi",
        "cache_claim": "hades-reference-cache",
        "poll_interval_seconds": 0.25,
        "launch_timeout_seconds": 45,
        "prepare_timeout_seconds": 90,
        "collector_timeout_seconds": 90,
        "verifier_timeout_seconds": 90,
        "cluster_dns_ip": os.environ["CRUCIBLE_E2E_KIND_DNS_IP"],
        "broad_egress": True,
        "image_repositories": (os.environ["CRUCIBLE_E2E_KIND_REGISTRY"],),
        # kind's containerd gives every container a private cgroup namespace, so the
        # canary cannot see the pod-level cgroup `tools/kind/e2e-kind.sh` configures
        # `podPidsLimit: 512` on (95); this is lab-admin's attestation of that same
        # number for this disposable cluster, the same way a real deployment would.
        "pod_pid_limit_override": 512,
    }
    settings.update(overrides)
    return KubernetesProvider(
        KubernetesConfig(**settings),
        api,
        registry,
        harnesses=harnesses,
        resolver=resolver or _tier_resolver,
    )


def _tier_resolver(host: str) -> list[str]:
    """The provider's lookup as the tier's cluster DNS answers it (hades #425).

    A worker's and a verifier's allowlist is always resolved, `broad_egress` or not, and
    the tier's policy allowlists github.com. Cluster DNS answers it with the stand-in
    git host (`GIT_HOST_DNS`, the CoreDNS patch in tools/kind/e2e-kind.sh), so the
    provider resolves it the same way rather than through the runner's own DNS, whose
    answer is the real GitHub. Any other name is looked up as the provider would."""
    if host in ("github.com", "api.github.com"):
        return [f"{GIT_HOST_DNS}/32"]
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({f"{info[4][0]}/32" for info in infos})


def _origin(name: str, behavior: str = "succeed", extra: dict[str, str] | None = None) -> str:
    cache = Path(os.environ["CRUCIBLE_E2E_KIND_CACHE"])
    host_url = make_origin(cache, name, behavior, extra=extra)
    bare = Path(host_url)
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(bare),
            "config",
            "remote.origin.url",
            f"file:///crucible/cache/{bare.name}",
        ],
        check=True,
    )
    return f"file:///crucible/cache/{bare.name}"


def _spec(
    number: int,
    repository_url: str,
    *,
    command: tuple[str, ...] = (),
    harness: str = "script-harness",
    network_hosts: tuple[str, ...] = (),
) -> LaunchSpec:
    attempt_id = f"{ATTEMPT_PREFIX}{number:02d}"
    document = contract_document(external_id=f"E2E-KIND-{number:02d}")
    document["repository"] = {
        "name": f"kind-{number}",
        "base_ref": "main",
        "work_branch": f"crucible/E2E-KIND-{number:02d}",
    }
    document["scope"] = {
        "allowed_paths": ["src/**", "checks/**"],
        "prohibited_paths": [".github/**"],
        "may_add_dependencies": False,
        "may_modify_ci": False,
    }
    document["required_verification"] = [
        {"id": "V1", "command": "sh checks/lint.sh", "expect_exit": 0},
        {"id": "V2", "command": "sh checks/test.sh", "expect_exit": 0},
    ]
    document["execution_request"]["provider"] = "kubernetes"
    return LaunchSpec(
        attempt_id=attempt_id,
        task_id=f"01KINDTASK0000000000000{number:02d}",
        external_id=f"E2E-KIND-{number:02d}",
        role="implement",
        harness=harness,
        model="none",
        image=os.environ["CRUCIBLE_E2E_KIND_REGISTRY"],
        timeout_seconds=60,
        contract=document,
        command=command,
        network="policy",
        endpoint="subscription",
        policy={
            "images": {"allowlist": ["localhost:*/*"]},
            "network": {"mode": "egress-proxy", "egress_allowlist": list(network_hosts)},
            "resources": {"cpus": 0.25, "memory": "256MiB", "ephemeral_storage": "256Mi"},
            "limits": {"grace_seconds": 2},
        },
        repository_url=repository_url,
    )


async def _terminal(provider: KubernetesProvider, handle: Any, timeout: float = 90) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = await provider.observe(handle)
        if observed.state is not ObservationState.RUNNING:
            return observed
        await asyncio.sleep(0.25)
    raise AssertionError("the real worker Pod never became terminal")


async def _running(provider: KubernetesProvider, handle: Any, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = await provider._pod_of(handle.ref)
        if pod and str((pod.get("status") or {}).get("phase")) == "Running":
            return
        await asyncio.sleep(0.25)
    raise AssertionError("the real worker Pod never ran")


async def _pods_gone(api: KubernetesClient, attempt_id: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    selector = f"{k8sspec.LABEL_ATTEMPT}={attempt_id}"
    while time.monotonic() < deadline:
        if not api.list_objects("pods", label_selector=selector):
            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"pods for {attempt_id} survived deletion")


def _network_destinations() -> dict[str, str]:
    return {
        "api-server": f"https://{os.environ['CRUCIBLE_E2E_KIND_API_IP']}:443/version",
        "cluster-dns-wrong-port": f"http://{os.environ['CRUCIBLE_E2E_KIND_DNS_IP']}:443/",
        "another-namespace": f"http://{os.environ['CRUCIBLE_E2E_KIND_PEER_IP']}:443/",
        "link-local": "http://169.254.169.254:443/",
        "lab-10": "http://10.0.0.1:443/",
        "lab-172": "http://172.16.0.1:443/",
        "lab-192": "http://192.168.0.1:443/",
        "lab-carrier": "http://100.64.0.1:443/",
    }


def _network_script(destinations: dict[str, str], *, attempts: int) -> str:
    probes = "\n".join(
        f"( reached=0; for i in $(seq 1 {attempts}); do "
        f"if curl -k -sS -o /dev/null --connect-timeout 1 --max-time 1 '{url}'; "
        f"then reached=1; break; fi; sleep 0.25; done; "
        f"if [ $reached -eq 1 ]; then echo '{name}=reached'; else echo '{name}=denied'; fi ) &"
        for name, url in destinations.items()
    )
    return f"{probes}\nwait"


async def _unrestricted_network_control(api: KubernetesClient, destinations: dict[str, str]) -> str:
    name = "network-reachability-control"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": "hades-workers"},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "serviceAccountName": "hades-worker",
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "control",
                    "image": os.environ["CRUCIBLE_E2E_KIND_REGISTRY"],
                    "command": ["sh", "-c", _network_script(destinations, attempts=20)],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"cpu": "250m", "memory": "128Mi"},
                    },
                }
            ],
        },
    }
    with contextlib.suppress(KubernetesApiError):
        api.delete("pods", name, grace_period_seconds=0)
    api.create("pods", pod)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        phase = str((api.get("pods", name).get("status") or {}).get("phase", ""))
        if phase in ("Succeeded", "Failed"):
            frames = api.pod_log(name, container="control", timestamps=False)
            api.delete("pods", name, grace_period_seconds=0)
            assert phase == "Succeeded"
            return b"".join(frame.payload for frame in frames).decode("utf-8", "replace")
        await asyncio.sleep(0.25)
    raise AssertionError("the unrestricted network control Pod did not finish")


async def test_row_5_7_11_full_lifecycle_on_a_real_pod_and_pvc(
    provider: KubernetesProvider, api: KubernetesClient
) -> None:
    """Readiness rows 5, 7 and 11: logs, failure detection and safe termination."""
    spec = _spec(1, _origin("full-lifecycle"))
    workspace = await provider.prepare(spec)
    handle = await provider.launch(workspace, spec)
    assert handle.image_digest and "@sha256:" in handle.image_digest
    observed = await _terminal(provider, handle)
    assert observed.state is ObservationState.EXITED and observed.exit_code == 0
    chunks = await provider.logs(handle, LogOffset())
    assert b"read identity bundle" in b"".join(chunk.content for chunk in chunks)
    outputs = await provider.collect(handle, workspace, spec)
    assert outputs.report is not None
    assert {run.id for run in outputs.verifications if run.ran} == {"V1", "V2"}
    assert outputs.bundle is not None
    await provider.cleanup(workspace, CleanupPolicy.DELETE, spec)
    await _pods_gone(api, spec.attempt_id)


async def test_rows_5_7_11_23_supervisor_restart_and_full_gate_lifecycle(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    provider: KubernetesProvider,
    api: KubernetesClient,
    registry: CraneRegistryClient,
) -> None:
    """The shared app and Supervisor lifecycle, backed by a real Job and PVC."""
    clock = SystemClock()
    harnesses = application_harnesses(test_fixtures=True)
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=clock,
        providers=[provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(artifact_root / "kind-store"),
        harnesses=harnesses,
    )
    tokens: dict[str, str] = {}
    with ctx.uow_factory() as uow:
        for role in Role:
            tokens[role.value] = mint_token(uow, clock, name=f"kind-{role.value}", role=role).token
        uow.commit()

    app = create_app(ctx)
    image = os.environ["CRUCIBLE_E2E_KIND_REGISTRY"]
    resolved = await asyncio.to_thread(registry.resolve, image)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
        routing = e2e_routing_document()
        assert admin.put(
            f"/v1/routing/{routing['name']}/{routing['version']}", json=routing
        ).status_code in (
            200,
            201,
        )
        policy = e2e_policy_document()
        policy["description"] = "The e2e script harness on the Kubernetes provider."
        policy["images"]["allowlist"] = ["localhost:*/*"]
        policy["resources"] = {
            "cpus": 1,
            "memory": "256MiB",
            "pids": 128,
            "tmpfs_total": "256MiB",
        }
        assert admin.put(
            f"/v1/policies/{policy['name']}/{policy['version']}", json=policy
        ).status_code in (200, 201)
        with ctx.uow_factory() as uow:
            promote_for_test(
                uow,
                digest=resolved.digest,
                reference=resolved.reference,
                harnesses=dict(resolved.harnesses) or {"script-harness": "1.0.0"},
                at=clock.now(),
                by="e2e-kind",
                reason="kind script harness image",
            )
            uow.commit()

    with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as client:
        origin = _origin("supervisor-lifecycle")
        register(ctx, "supervisor-lifecycle", origin)
        document = e2e_contract("E2E-KIND-SUPERVISOR", "supervisor-lifecycle", image)
        document["execution_request"]["provider"] = "kubernetes"
        task_id = submit_and_start(client, document)
        first = Supervisor(
            ctx.uow_factory,
            {"kubernetes": provider},
            clock,
            holder="e2e-kind-first",
            artifact_store=ctx.artifact_store,
            lease_ttl_seconds=120,
            grace_seconds=5,
            harnesses=harnesses,
        )
        # By the clock, not by ticks: a launch runs beside the tick (hades #190), so a
        # tick no longer lasts as long as the launch it starts.
        deadline = time.monotonic() + LAUNCH_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            await first.tick()
            attempt = client.get(f"/v1/tasks/{task_id}").json().get("latest_attempt")
            if attempt and attempt["state"] == "running":
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("the Kubernetes-backed Supervisor never launched a worker")
        await first.stop()

        successor_provider = _provider(api, registry)
        successor = Supervisor(
            ctx.uow_factory,
            {"kubernetes": successor_provider},
            clock,
            holder="e2e-kind-successor",
            artifact_store=ctx.artifact_store,
            lease_ttl_seconds=120,
            grace_seconds=5,
            harnesses=harnesses,
        )
        state = await run_until(
            successor,
            client,
            task_id,
            {"accepted", "pre_pr_gates_failed"},
            max_ticks=90,
            pause=0.5,
        )
        results = gate_results(client, task_id)
        assert state == "accepted", json.dumps(results, sort_keys=True)
        for gate in (
            "verification_ran",
            "workspace_clean",
            "commits_present",
            "no_injected_files",
            "no_secrets",
        ):
            assert results[gate] == "pass", results

        attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT image_digest, identity_sha256, logs_drained_at, cleaned_up_at "
                    "FROM attempts WHERE id = :id"
                ),
                {"id": attempt_id},
            ).one()
            chunks = connection.execute(
                text("SELECT content, gzipped FROM log_chunks WHERE attempt_id = :id ORDER BY id"),
                {"id": attempt_id},
            ).all()
        assert row.image_digest and "@sha256:" in row.image_digest
        assert row.identity_sha256 and len(row.identity_sha256) == 64
        assert row.logs_drained_at is not None
        assert row.cleaned_up_at is not None and row.cleaned_up_at >= row.logs_drained_at
        body = b"".join(chunk.content for chunk in chunks if not chunk.gzipped).decode(
            "utf-8", "replace"
        )
        assert body.count("read identity bundle") == 1
        for kind in (
            "workspace_prepared",
            "image_resolved",
            "attempt_logs_drained",
            "verification_completed",
            "attempt_cleaned_up",
        ):
            assert kind in event_kinds(client, task_id)

        timeout_origin = _origin("supervisor-timeout", "hang")
        register(ctx, "supervisor-timeout", timeout_origin)
        timeout_document = e2e_contract("E2E-KIND-TIMEOUT", "supervisor-timeout", image)
        timeout_document["execution_request"].update(
            {"provider": "kubernetes", "timeout_seconds": 5}
        )
        timeout_task = submit_and_start(client, timeout_document)
        assert await run_until(
            successor,
            client,
            timeout_task,
            {"accepted", "pre_pr_gates_failed"},
            max_ticks=60,
            pause=0.5,
        ) in {"accepted", "pre_pr_gates_failed"}
        timeout_attempt = client.get(f"/v1/tasks/{timeout_task}").json()["latest_attempt"]
        with engine.begin() as connection:
            timeout_row = connection.execute(
                text("SELECT exit_class, termination_reason FROM attempts WHERE id = :id"),
                {"id": timeout_attempt["id"]},
            ).one()
        assert timeout_row.exit_class == "timeout"
        assert timeout_row.termination_reason == "timeout"
        assert "attempt_timeout_drain" in event_kinds(client, timeout_task)

        # Row 7: completion, timeout, cancellation and stall are proven below and
        # elsewhere in this function; a plain worker failure (no timeout, no signal)
        # is not, so it gets its own case rather than being implied by the others.
        crash_origin = _origin("supervisor-crash", "crash")
        register(ctx, "supervisor-crash", crash_origin)
        crash_document = e2e_contract("E2E-KIND-CRASH", "supervisor-crash", image)
        crash_document["execution_request"]["provider"] = "kubernetes"
        crash_task = submit_and_start(client, crash_document)
        assert (
            await run_until(
                successor, client, crash_task, {"pre_pr_gates_failed"}, max_ticks=60, pause=0.5
            )
            == "pre_pr_gates_failed"
        )
        crash_attempt = client.get(f"/v1/tasks/{crash_task}").json()["latest_attempt"]
        with engine.begin() as connection:
            crash_row = connection.execute(
                text("SELECT exit_class FROM attempts WHERE id = :id"),
                {"id": crash_attempt["id"]},
            ).one()
        assert crash_row.exit_class == "crashed"

        cancel_origin = _origin("supervisor-cancel", "hang")
        register(ctx, "supervisor-cancel", cancel_origin)
        cancel_document = e2e_contract("E2E-KIND-CANCEL", "supervisor-cancel", image)
        cancel_document["execution_request"]["provider"] = "kubernetes"
        cancel_task = submit_and_start(client, cancel_document)
        deadline = time.monotonic() + LAUNCH_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            await successor.tick()
            cancel_attempt = client.get(f"/v1/tasks/{cancel_task}").json().get("latest_attempt")
            if cancel_attempt and cancel_attempt["state"] == "running":
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("the cancellation worker never reached running")
        # Row 11: a kill proves the worker stops; it says nothing about what the
        # collector does with the report a killed worker had partly written. Plant one
        # before the cancel so a real Pod's file is what the collector reads back.
        # `running` is recorded once the Job exists, before its Pod is scheduled or its
        # worker container has started, so wait for the container itself.
        cancel_pod_name = ""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not cancel_pod_name:
            for cancel_pod in api.list_objects(
                "pods", label_selector=f"{k8sspec.LABEL_ATTEMPT}={cancel_attempt['id']}"
            ):
                statuses = (cancel_pod.get("status") or {}).get("containerStatuses") or []
                if any(
                    item.get("name") == k8sspec.CONTAINER_NAME
                    and "running" in (item.get("state") or {})
                    for item in statuses
                ):
                    cancel_pod_name = str(cancel_pod["metadata"]["name"])
                    break
            else:
                await asyncio.sleep(0.25)
        assert cancel_pod_name, "the cancellation worker's container never started"
        exec_result = api.pod_exec(
            cancel_pod_name,
            [
                "sh",
                "-c",
                f"printf '%s\\n' 'schema_version: 1.0' 'summary: interrupted' "
                f"> {REPORT_MOUNT}/report.yaml",
            ],
            container=k8sspec.CONTAINER_NAME,
        )
        assert exec_result.exit_code == 0, exec_result
        cancelled = client.post(
            f"/v1/tasks/{cancel_task}/cancel",
            json={
                "reason": "kind cancellation case",
                "verbatim": "cancel the kind test worker",
                "decided_by": "tests",
            },
        )
        assert cancelled.status_code == 200
        assert (
            await run_until(successor, client, cancel_task, {"cancelled"}, max_ticks=40, pause=0.5)
            == "cancelled"
        )
        cancel_attempt_id = client.get(f"/v1/tasks/{cancel_task}").json()["latest_attempt"]["id"]
        assert client.get(f"/v1/tasks/{cancel_task}").json()["latest_attempt"]["exit_class"] in (
            "killed",
            "cancelled",
        )
        cancel_artifacts = client.get(f"/v1/attempts/{cancel_attempt_id}/artifacts").json()["items"]
        partial = [item for item in cancel_artifacts if item["type"] == "partial_report"]
        assert len(partial) == 1 and partial[0]["filename"] == "report/report.yaml", (
            cancel_artifacts
        )

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE policies SET document = jsonb_set(jsonb_set(document, "
                    "'{limits,stall_warn_seconds}', '2'), '{limits,stall_fail_seconds}', '4') "
                    "WHERE name='e2e-script' AND version=1"
                )
            )
        stall_origin = _origin("supervisor-stall", "hang")
        register(ctx, "supervisor-stall", stall_origin)
        stall_document = e2e_contract("E2E-KIND-STALL", "supervisor-stall", image)
        stall_document["execution_request"]["provider"] = "kubernetes"
        stall_task = submit_and_start(client, stall_document)
        await run_until(
            successor,
            client,
            stall_task,
            {"accepted", "pre_pr_gates_failed"},
            max_ticks=50,
            pause=0.5,
        )
        stall_attempt = client.get(f"/v1/tasks/{stall_task}").json()["latest_attempt"]
        with engine.begin() as connection:
            stall_row = connection.execute(
                text("SELECT exit_class, termination_reason FROM attempts WHERE id = :id"),
                {"id": stall_attempt["id"]},
            ).one()
        assert stall_row.exit_class == "stalled"
        assert stall_row.termination_reason == "stall"
        assert {"worker_quiet", "worker_stalled"} <= set(event_kinds(client, stall_task))

        detached_origin = _origin("supervisor-detached")
        register(ctx, "supervisor-detached", detached_origin)
        detached_document = e2e_contract("E2E-KIND-DETACHED", "supervisor-detached", image)
        detached_document["execution_request"]["provider"] = "kubernetes"
        detached_document["required_verification"].extend(
            [
                {"id": "check/one", "command": "sh checks/lint.sh", "expect_exit": 0},
                {"id": "check_one", "command": "exit 3", "expect_exit": 3},
                # hades #250: a task whose checks all pass on the base is blocked before launch.
                {"id": "V9", "command": "test -f src/e2e_change.txt", "expect_exit": 0},
            ]
        )
        detached_task = submit_and_start(client, detached_document)
        for _ in range(90):
            await successor.tick()
            with ctx.uow_factory() as uow:
                detached = uow.tasks.get(detached_task)
                assert detached is not None
                if detached.state.value in ("accepted", "pre_pr_gates_failed"):
                    break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("the detached Kubernetes task did not finish")
        detached_attempt = client.get(f"/v1/tasks/{detached_task}").json()["latest_attempt"]
        evidence = client.get(f"/v1/attempts/{detached_attempt['id']}/evidence").json()["items"]
        verification_ids = {
            item["payload"]["id"] for item in evidence if item["kind"] == "verification_run"
        }
        assert {"check/one", "check_one"} <= verification_ids
        with ctx.uow_factory() as uow:
            detached_task_row = uow.tasks.get(detached_task)
            assert detached_task_row is not None
            assert uow.wakes.list_for_principal(
                detached_task_row.principal_id, since=None, include_acked=False, limit=50
            )

        orphan = _spec(40, _origin("supervisor-orphan"), command=("sh", "-c", "sleep 600"))
        orphan_workspace = await successor_provider.prepare(orphan)
        orphan_handle = await successor_provider.launch(orphan_workspace, orphan)
        await _running(successor_provider, orphan_handle)
        assert (await successor.tick()).orphans >= 1
        await _pods_gone(api, orphan.attempt_id)
        await successor_provider.cleanup(orphan_workspace, CleanupPolicy.DELETE, orphan)


async def test_row_12_concurrent_attempts_use_distinct_claims(
    provider: KubernetesProvider, api: KubernetesClient
) -> None:
    """Readiness row 12: two live attempts never share a working tree."""
    origin = _origin("concurrent-claims")
    first = _spec(30, origin, command=("sh", "-c", "sleep 3"))
    second = _spec(31, origin, command=("sh", "-c", "sleep 3"))
    first_ws, second_ws = await asyncio.gather(provider.prepare(first), provider.prepare(second))
    first_handle, second_handle = await asyncio.gather(
        provider.launch(first_ws, first), provider.launch(second_ws, second)
    )
    await asyncio.gather(_terminal(provider, first_handle), _terminal(provider, second_handle))
    attempts = {first.attempt_id, second.attempt_id}
    claims = {
        str((row.get("metadata") or {}).get("name"))
        for row in api.list_objects("persistentvolumeclaims")
        if str((row.get("metadata") or {}).get("labels", {}).get(k8sspec.LABEL_ATTEMPT)) in attempts
    }
    assert claims == {
        k8sspec.object_name("ws", first.attempt_id),
        k8sspec.object_name("ws", second.attempt_id),
    }
    await asyncio.gather(
        provider.cleanup(first_ws, CleanupPolicy.DELETE, first),
        provider.cleanup(second_ws, CleanupPolicy.DELETE, second),
    )


async def test_network_policy_denies_every_kubernetes_destination_from_the_worker(
    provider: KubernetesProvider, api: KubernetesClient
) -> None:
    """Readiness row 12: the real CNI denies every destination listed by spec 26."""
    destinations = _network_destinations()
    default_deny = api.get("networkpolicies", "default-deny")
    api.delete("networkpolicies", "default-deny")
    try:
        control = await _unrestricted_network_control(api, destinations)
        for name in destinations:
            assert f"{name}=reached" in control, control
    finally:
        restored = {key: value for key, value in default_deny.items() if key != "status"}
        metadata = restored["metadata"]
        restored["metadata"] = {
            key: value
            for key, value in metadata.items()
            if key in ("name", "namespace", "labels", "annotations")
        }
        # A suppressed failure here would leave every later case in the session
        # running without the default-deny NetworkPolicy in place (74).
        api.create("networkpolicies", restored)
    await asyncio.sleep(2)
    spec = _spec(
        2,
        _origin("network-denials"),
        command=("sh", "-c", _network_script(destinations, attempts=20)),
        network_hosts=("example.com",),
    )
    workspace = await provider.prepare(spec)
    handle = await provider.launch(workspace, spec)
    assert (await _terminal(provider, handle)).exit_code == 0
    body = b"".join(chunk.content for chunk in await provider.logs(handle, LogOffset())).decode()
    for name in destinations:
        assert f"{name}=denied" in body, body
        assert f"{name}=reached" not in body, body
    await provider.cleanup(workspace, CleanupPolicy.DELETE, spec)
    # 73: the reachable control ran once, before the denial phase, with no proof any
    # destination was still reachable once the policy came off again. A destination
    # that went away between phases for reasons other than the policy would read as
    # denied either way, so re-run the control with the same budget after.
    default_deny = api.get("networkpolicies", "default-deny")
    api.delete("networkpolicies", "default-deny")
    try:
        control = await _unrestricted_network_control(api, destinations)
        for name in destinations:
            assert f"{name}=reached" in control, control
    finally:
        restored = {key: value for key, value in default_deny.items() if key != "status"}
        metadata = restored["metadata"]
        restored["metadata"] = {
            key: value
            for key, value in metadata.items()
            if key in ("name", "namespace", "labels", "annotations")
        }
        api.create("networkpolicies", restored)
    # The same settle the first restore gets, so the next case does not launch before
    # Calico enforces the deny again.
    await asyncio.sleep(2)


async def test_deleted_pod_is_lost_and_sigterm_ignoring_pod_dies_at_grace(
    provider: KubernetesProvider, api: KubernetesClient
) -> None:
    """Rows 7 and 11: out-of-band loss, then kubelet SIGKILL after the grace."""
    lost_spec = _spec(3, _origin("lost"), command=("sh", "-c", "sleep 600"))
    lost_ws = await provider.prepare(lost_spec)
    lost_handle = await provider.launch(lost_ws, lost_spec)
    await _running(provider, lost_handle)
    # 103: lost requires having seen the Pod at least once, to tell it apart from a
    # Job whose Pod the controller has not created yet.
    assert (await provider.observe(lost_handle)).state is ObservationState.RUNNING
    pod = await provider._pod_of(lost_handle.ref)
    assert pod is not None
    api.delete("pods", str(pod["metadata"]["name"]), grace_period_seconds=0)
    await _pods_gone(api, lost_spec.attempt_id)
    assert (await provider.observe(lost_handle)).state is ObservationState.LOST
    await provider.cleanup(lost_ws, CleanupPolicy.DELETE, lost_spec)

    # 71: spec 26 says a Pod "evicted or deleted out of band" is `lost`. This evicts
    # through the Eviction API, which is what `kubectl drain` calls, rather than
    # deleting. The supervisor's Role may not create `pods/eviction`, so the tier's
    # cluster-admin kubeconfig (KUBECONFIG, set by e2e-kind.sh) makes the request, as
    # an operator draining the node would.
    evicted_spec = _spec(6, _origin("evicted"), command=("sh", "-c", "sleep 600"))
    evicted_ws = await provider.prepare(evicted_spec)
    evicted_handle = await provider.launch(evicted_ws, evicted_spec)
    await _running(provider, evicted_handle)
    assert (await provider.observe(evicted_handle)).state is ObservationState.RUNNING
    evicted_pod = await provider._pod_of(evicted_handle.ref)
    assert evicted_pod is not None
    evicted_name = str(evicted_pod["metadata"]["name"])
    eviction = {
        "apiVersion": "policy/v1",
        "kind": "Eviction",
        "metadata": {"name": evicted_name, "namespace": "hades-workers"},
        "deleteOptions": {"gracePeriodSeconds": 0},
    }
    subprocess.run(
        [
            "kubectl",
            "create",
            "--raw",
            f"/api/v1/namespaces/hades-workers/pods/{evicted_name}/eviction",
            "-f",
            "-",
        ],
        input=json.dumps(eviction),
        text=True,
        capture_output=True,
        check=True,
    )
    await _pods_gone(api, evicted_spec.attempt_id)
    assert (await provider.observe(evicted_handle)).state is ObservationState.LOST
    await provider.cleanup(evicted_ws, CleanupPolicy.DELETE, evicted_spec)

    stubborn = _spec(
        4,
        _origin("stubborn"),
        command=("sh", "-c", "trap '' TERM; echo ready; while :; do sleep 1; done"),
    )
    stubborn_ws = await provider.prepare(stubborn)
    stubborn_handle = await provider.launch(stubborn_ws, stubborn)
    await _running(provider, stubborn_handle)
    started = time.monotonic()
    await provider.terminate(stubborn_handle, "drain")
    assert (await _terminal(provider, stubborn_handle, timeout=15)).state is ObservationState.EXITED
    assert time.monotonic() - started >= 1.5
    await provider.cleanup(stubborn_ws, CleanupPolicy.DELETE, stubborn)


async def test_restart_adopts_the_job_and_resumes_logs(
    provider: KubernetesProvider, api: KubernetesClient, registry: CraneRegistryClient
) -> None:
    """Readiness row 5: a fresh provider adopts the Job and resumes after its offset."""
    script = "i=1; while [ $i -le 8 ]; do echo resume-$i; i=$((i+1)); sleep 1; done"
    spec = _spec(5, _origin("restart"), command=("sh", "-c", script))
    workspace = await provider.prepare(spec)
    handle = await provider.launch(workspace, spec)
    await _running(provider, handle)
    first = await provider.logs(handle, LogOffset())
    assert first
    last = first[-1]
    offset = LogOffset(
        index=sum(chunk.lines for chunk in first),
        timestamp=last.ts.isoformat() if last.ts else None,
        line_sha256=last.line_sha256,
        occurrence=last.occurrence,
    )
    successor = _provider(api, registry)
    adopted = await successor.reconcile()
    adopted_handle = next(item for item in adopted if item.attempt_id == spec.attempt_id)
    await asyncio.sleep(2)
    resumed = await successor.logs(adopted_handle, offset)
    assert resumed
    assert not set(b"".join(c.content for c in first).splitlines()) & set(
        b"".join(c.content for c in resumed).splitlines()
    )
    await _terminal(successor, adopted_handle)
    await successor.cleanup(workspace, CleanupPolicy.DELETE, spec)


async def test_an_adopted_attempt_drains_with_its_policy_grace_and_reports_pod_limits(
    provider: KubernetesProvider, api: KubernetesClient, registry: CraneRegistryClient
) -> None:
    """Issues 66 and 76 on a real API server: a fresh provider adopts a worker, reads
    its limits off the live Pod (as the API server canonicalised them), and drains it
    with the task policy's grace period rather than a 60 s default."""
    spec = _spec(
        50,
        _origin("adopt-grace"),
        command=("sh", "-c", "trap '' TERM; echo ready; while :; do sleep 1; done"),
    )
    spec = replace(spec, policy={**spec.policy, "limits": {"grace_seconds": 7}})
    workspace = await provider.prepare(spec)
    handle = await provider.launch(workspace, spec)
    await _running(provider, handle)
    successor = _provider(api, registry)
    adopted = next(h for h in await successor.reconcile() if h.attempt_id == spec.attempt_id)
    launched = successor._launched[spec.attempt_id]
    pod = await successor._pod_of(adopted.ref)
    assert pod is not None
    print("live limits:", json.dumps(pod["spec"]["containers"][0]["resources"]))
    assert launched.limits_source == "pod"
    assert launched.limits.as_dict() == provider._limits(spec).as_dict()
    assert launched.limits.grace_seconds == 7
    await successor.terminate(adopted, "drain")
    draining = api.get("pods", str(pod["metadata"]["name"]))
    print("deletionGracePeriodSeconds:", draining["metadata"].get("deletionGracePeriodSeconds"))
    assert draining["metadata"].get("deletionGracePeriodSeconds") == 7
    assert (await _terminal(successor, adopted, timeout=30)).state is ObservationState.EXITED
    await successor.cleanup(workspace, CleanupPolicy.DELETE, spec)


def _pod_body(name: str, resources: dict[str, Any], *, pod_level: bool) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": "crucible",
        "image": os.environ["CRUCIBLE_E2E_KIND_REGISTRY"],
        "command": ["true"],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
    }
    spec: dict[str, Any] = {"restartPolicy": "Never", "containers": [container]}
    if pod_level:
        spec["resources"] = resources
    else:
        container["resources"] = resources
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name}, "spec": spec}


@pytest.mark.parametrize("pod_level", [False, True])
async def test_the_pod_api_has_no_pid_limit_field(api: KubernetesClient, pod_level: bool) -> None:
    """Issue 60: the Docker provider's `PidsLimit` has no Pod API equivalent. The API
    server refuses `pids` as a container resource and as a pod-level resource, so the
    per-pod limit cannot be set from the Pod and is the kubelet's `podPidsLimit`."""
    name = f"crucible-pids-probe-{'pod' if pod_level else 'container'}"
    resources = {
        "limits": {"cpu": "100m", "memory": "64Mi", "pids": "64"},
        "requests": {"cpu": "100m", "memory": "64Mi"},
    }
    try:
        api.create("pods", _pod_body(name, resources, pod_level=pod_level))
    except KubernetesApiError as exc:
        refused = exc
    else:
        api.delete("pods", name, grace_period_seconds=0)
        pytest.fail("the API server accepted a pids resource")
    print(f"pids refused ({'pod' if pod_level else 'container'} level):", refused)
    assert refused.status == 422
    assert "pids" in str(refused)


async def test_a_bounded_log_read_brings_every_line_once_through_the_real_api(
    provider: KubernetesProvider,
    api: KubernetesClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue 63 on a real kubelet: `limitBytes` is honoured (a read stops where it lands,
    mid-line), and polling with a small cap still stores the whole log, in order, once.
    A burst of more than the ceiling inside one second is skipped with a notice and the
    lines after it still arrive."""
    script = (
        "i=1; while [ $i -le 40 ]; do echo bounded-$i-padding-padding-padding; "
        "i=$((i+1)); sleep 0.05; done; "
        "j=1; while [ $j -le 60 ]; do "
        "echo burst-$j-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx; "
        "j=$((j+1)); done; sleep 2; echo after-the-burst"
    )
    spec = _spec(51, _origin("bounded-logs"), command=("sh", "-c", script))
    workspace = await provider.prepare(spec)
    handle = await provider.launch(workspace, spec)
    await _terminal(provider, handle)
    pod = await provider._pod_of(handle.ref)
    assert pod is not None
    pod_name = str(pod["metadata"]["name"])
    raw = b"".join(
        f.payload for f in api.pod_log(pod_name, container=k8sspec.CONTAINER_NAME, limit_bytes=100)
    )
    print("a 100-byte read:", raw)
    assert len(raw) == 100

    monkeypatch.setattr(kubernetes_module, "LOG_READ_LIMIT", 600)
    monkeypatch.setattr(kubernetes_module, "LOG_READ_CEILING", 2400)
    reads: list[int] = []
    real_log = api.pod_log

    def counted(name: str, **kwargs: Any) -> Any:
        reads.append(int(kwargs.get("limit_bytes") or 0))
        return real_log(name, **kwargs)

    monkeypatch.setattr(api, "pod_log", counted)
    offset = LogOffset()
    stored: list[bytes] = []
    for _ in range(200):
        chunks = await provider.logs(handle, offset)
        if not chunks:
            break
        for chunk in chunks:
            stored.extend(chunk.content.splitlines())
            offset = LogOffset(
                index=offset.index + chunk.lines,
                timestamp=chunk.ts.isoformat() if chunk.ts else None,
                line_sha256=chunk.line_sha256,
                occurrence=chunk.occurrence,
            )
    print("polls:", len(reads), "largest read:", max(reads), "lines stored:", len(stored))
    assert reads and max(reads) <= 2400
    bounded = [line for line in stored if line.startswith(b"bounded-")]
    assert bounded == [f"bounded-{i}-padding-padding-padding".encode() for i in range(1, 41)]
    assert any(line.startswith(b"[crucible] log lines skipped") for line in stored)
    assert stored[-1] == b"after-the-burst"
    await provider.cleanup(workspace, CleanupPolicy.DELETE, spec)


class RotatingScriptAdapter(ScriptHarnessAdapter):
    """A rw-narrow credential with an issued-at field, like the real three (12)."""

    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=self.name,
            mount_target="/home/worker/.script-harness",
            auth_files=(
                AuthFile("auth.json", json=True, json_keys=("token",), issued_at=("issued",)),
            ),
            minimum_mode=MountMode.RW_NARROW,
        )


async def test_a_failed_attempts_rotated_token_is_written_back_on_a_real_cluster(
    api: KubernetesClient, registry: CraneRegistryClient
) -> None:
    """Issue 56: the worker refreshes its token and then fails. The newer, valid file is
    read back through the reader Pod and written into the harness Secret whatever the
    exit code (12 as amended)."""
    provider = _provider(api, registry, harnesses=HarnessRegistry((RotatingScriptAdapter(),)))
    source = provider.credential_secret("script-harness")
    with contextlib.suppress(KubernetesApiError):
        api.delete("secrets", source)
    seeded = {"token": "seeded-placeholder", "issued": "2026-09-20T00:00:00Z"}
    rotated = {"token": "rotated-placeholder", "issued": "2026-09-25T00:00:00Z"}
    api.create(
        "secrets",
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": source, "namespace": "hades-workers"},
            "type": "Opaque",
            "stringData": {"auth.json": json.dumps(seeded)},
        },
    )
    rotate = (
        "printf '%s' '" + json.dumps(rotated) + "' > /home/worker/.script-harness/auth.json; "
        "echo rotated; exit 3"
    )
    spec = _spec(52, _origin("rotate-then-fail"), command=("sh", "-c", rotate))
    try:
        workspace = await provider.prepare(spec)
        handle = await provider.launch(workspace, spec)
        observed = await _terminal(provider, handle)
        assert observed.exit_code == 3, observed
        outputs = await provider.collect(handle, workspace, spec)
        sync = outputs.credential_sync
        print("credential sync:", sync)
        assert sync is not None
        assert [(f.name, f.synced) for f in sync.files] == [("auth.json", True)], sync
        stored = api.get("secrets", source)["data"]["auth.json"]
        assert json.loads(base64.b64decode(stored)) == rotated
        await provider.cleanup(workspace, CleanupPolicy.DELETE, spec)
    finally:
        with contextlib.suppress(KubernetesApiError):
            api.delete("secrets", source)


@pytest.mark.parametrize("policy", list(CleanupPolicy))
async def test_per_attempt_secret_is_removed_under_every_cleanup_policy(
    api: KubernetesClient,
    registry: CraneRegistryClient,
    policy: CleanupPolicy,
) -> None:
    harnesses = HarnessRegistry((CredentialScriptAdapter(),))
    provider = _provider(api, registry, harnesses=harnesses)
    source = "hades-harness-script-harness"
    try:
        api.create(
            "secrets",
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": source, "namespace": "hades-workers"},
                "type": "Opaque",
                "stringData": {"auth.json": json.dumps({"placeholder": "x" * 32})},
            },
        )
    except KubernetesApiError as exc:
        if exc.status != 409:
            raise
    number = 10 + list(CleanupPolicy).index(policy)
    spec = _spec(number, _origin(f"secret-{policy.value}"))
    workspace = await provider.prepare(spec)
    handle = await provider.launch(workspace, spec)
    await _terminal(provider, handle)
    await provider.cleanup(workspace, policy, spec)
    with pytest.raises(KubernetesApiError) as raised:
        api.get("secrets", k8sspec.object_name("cred", spec.attempt_id))
    assert raised.value.status == 404


async def test_probe_refuses_launches_without_default_deny(
    api: KubernetesClient, registry: CraneRegistryClient
) -> None:
    """Readiness row 12: removing enforcement makes the canary fail closed. Issue 59:
    the refusal comes at prepare, before the claim, the identity ConfigMap, the
    per-attempt credential Secret or the preparer Job (which has GitHub egress) exist."""
    spec = _spec(20, _origin("probe-refusal"))
    provider = _provider(api, registry)
    default_deny = api.get("networkpolicies", "default-deny")
    api.delete("networkpolicies", "default-deny")
    try:
        with pytest.raises(LaunchRefusedError, match="namespace is not ready"):
            await provider.prepare(spec)
        probe = await provider.ensure_ready()
        assert probe.passed is False
        assert probe.egress_enforced is False
        assert "reached the API server" in probe.detail
        selector = f"{k8sspec.LABEL_ATTEMPT}={spec.attempt_id}"
        for kind in ("persistentvolumeclaims", "configmaps", "secrets", "jobs", "pods"):
            assert not api.list_objects(kind, label_selector=selector), kind
    finally:
        restored = {key: value for key, value in default_deny.items() if key not in ("status",)}
        metadata = restored["metadata"]
        restored["metadata"] = {
            key: value
            for key, value in metadata.items()
            if key in ("name", "namespace", "labels", "annotations")
        }
        api.create("networkpolicies", restored)
        await provider._delete_attempt_objects(spec.attempt_id)


async def test_row_23_a_harness_the_image_does_not_declare_is_refused(
    api: KubernetesClient, registry: CraneRegistryClient
) -> None:
    """Readiness row 23: unsupported combinations are refused. The tier's image
    carries only the script harness's label, so an attempt that asks it for codex is
    an unsupported combination, and prepare refuses it before any Job exists."""
    provider = _provider(api, registry, harnesses=application_harnesses())
    launch = _spec(50, _origin("unsupported-harness"), harness="codex")
    with pytest.raises(LaunchRefusedError, match="the image declares harness"):
        await provider.prepare(launch)
    assert not api.list_objects(
        "persistentvolumeclaims", label_selector=f"{k8sspec.LABEL_ATTEMPT}={launch.attempt_id}"
    )


async def test_isolation_probes_are_refused_on_kubernetes(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    provider: KubernetesProvider,
    registry: CraneRegistryClient,
) -> None:
    """72: `test_isolation.py`'s probes (S4, 18, 21) had no kind counterpart. The
    worker image's `isolation` behavior is the one script both tiers launch, so this
    re-runs it through the full app and Supervisor on the Kubernetes provider and
    demands the same posture Docker proves: every probe reads `refused`, whichever
    mechanism (no Docker socket to mount, a NetworkPolicy instead of an egress proxy,
    no shared filesystem to push into) produced it."""
    clock = SystemClock()
    harnesses = application_harnesses(test_fixtures=True)
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=clock,
        providers=[provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(artifact_root / "kind-isolation-store"),
        harnesses=harnesses,
    )
    tokens: dict[str, str] = {}
    with ctx.uow_factory() as uow:
        for role in Role:
            tokens[role.value] = mint_token(
                uow, clock, name=f"kind-isolation-{role.value}", role=role
            ).token
        uow.commit()
    app = create_app(ctx)
    image = os.environ["CRUCIBLE_E2E_KIND_REGISTRY"]
    resolved = await asyncio.to_thread(registry.resolve, image)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
        routing = e2e_routing_document()
        assert admin.put(
            f"/v1/routing/{routing['name']}/{routing['version']}", json=routing
        ).status_code in (200, 201)
        policy = e2e_policy_document()
        policy["images"]["allowlist"] = ["localhost:*/*"]
        policy["resources"] = {
            "cpus": 1,
            "memory": "256MiB",
            "pids": 128,
            "tmpfs_total": "256MiB",
        }
        assert admin.put(
            f"/v1/policies/{policy['name']}/{policy['version']}", json=policy
        ).status_code in (200, 201)
        with ctx.uow_factory() as uow:
            promote_for_test(
                uow,
                digest=resolved.digest,
                reference=resolved.reference,
                harnesses=dict(resolved.harnesses) or {"script-harness": "1.0.0"},
                at=clock.now(),
                by="e2e-kind",
                reason="kind isolation probe image",
            )
            uow.commit()

    supervisor = Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        clock,
        holder="e2e-kind-isolation",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=120,
        grace_seconds=5,
        harnesses=harnesses,
    )
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as client:
        origin = _origin("isolation", "isolation")
        register(ctx, "isolation", origin)
        document = e2e_contract("E2E-KIND-ISOLATION", "isolation", image)
        document["execution_request"]["provider"] = "kubernetes"
        task_id = submit_and_start(client, document)
        await run_until(
            supervisor,
            client,
            task_id,
            {"accepted", "gates_passed", "pre_pr_gates_failed"},
            max_ticks=90,
            pause=0.5,
        )
        attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
        artifacts = client.get(f"/v1/attempts/{attempt_id}/artifacts").json()["items"]
        probe_artifact = next(a for a in artifacts if a["filename"].endswith("isolation.tsv"))
        body = client.get(f"/v1/artifacts/{probe_artifact['id']}/content").text
        results = dict(line.split("\t", 1) for line in body.splitlines() if "\t" in line)
        assert set(MUST_BE_REFUSED) <= set(results), sorted(results)
        reached = sorted(name for name, outcome in results.items() if outcome != "refused")
        assert reached == [], f"a worker on Kubernetes reached something it must not: {reached}"


async def test_scripted_quota_reroutes_on_kubernetes(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    provider: KubernetesProvider,
    registry: CraneRegistryClient,
) -> None:
    """72: `test_class_routing.py`'s reroute case had no kind counterpart. A real Pod
    runs the scripted quota worker, which exhausts its quota and exits, and the attempt
    reroutes to a successor that starts from the base. Local-origin quota checkpoints
    are Docker-only: the Kubernetes provider cannot push to the supervisor-local
    file origin, so the supervisor records the skip. Stops at the reroute; the Docker
    case goes on to prove its checkpoint reaches the local file origin."""
    clock = SystemClock()
    harnesses = application_harnesses(test_fixtures=True)
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=clock,
        providers=[provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(artifact_root / "kind-routing-store"),
        harnesses=harnesses,
    )
    tokens: dict[str, str] = {}
    with ctx.uow_factory() as uow:
        for role in Role:
            tokens[role.value] = mint_token(
                uow, clock, name=f"kind-routing-{role.value}", role=role
            ).token
        uow.commit()
    app = create_app(ctx)
    image = os.environ["CRUCIBLE_E2E_KIND_REGISTRY"]
    resolved = await asyncio.to_thread(registry.resolve, image)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
        routing = e2e_routing_document()
        assert admin.put(
            f"/v1/routing/{routing['name']}/{routing['version']}", json=routing
        ).status_code in (200, 201)
        policy = e2e_policy_document()
        policy["images"]["allowlist"] = ["localhost:*/*"]
        policy["resources"] = {
            "cpus": 1,
            "memory": "256MiB",
            "pids": 128,
            "tmpfs_total": "256MiB",
        }
        assert admin.put(
            f"/v1/policies/{policy['name']}/{policy['version']}", json=policy
        ).status_code in (200, 201)
        with ctx.uow_factory() as uow:
            promote_for_test(
                uow,
                digest=resolved.digest,
                reference=resolved.reference,
                harnesses=dict(resolved.harnesses) or {"script-harness": "1.0.0"},
                at=clock.now(),
                by="e2e-kind",
                reason="kind class routing first image",
            )
            uow.commit()
        _install_class_policy(ctx)

    supervisor = Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        clock,
        holder="e2e-kind-routing",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=120,
        grace_seconds=5,
        harnesses=harnesses,
    )
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as client:
        origin = _origin("class-routing")
        register(ctx, "class-routing", origin)
        document = e2e_contract("E2E-KIND-C6B", "class-routing", image)
        document["execution_request"]["provider"] = "kubernetes"
        document["policy"] = {"name": "e2e-script", "version": 2}
        document["scope"]["allowed_paths"].append("e2e-behavior")
        for field in ("harness", "model", "pin_reason", "image"):
            document["execution_request"].pop(field, None)
        task_id = submit_and_start(client, document)

        # A real Pod's schedule-pull-run-exit round trip spans several ticks on kind,
        # unlike Docker's near-instant container start, so this polls rather than
        # assuming one tick reaches the reroute the way the Docker case can.
        for _ in range(120):
            await supervisor.tick()
            midway = client.get(f"/v1/tasks/{task_id}").json()
            attempts = midway["executions"][0]["attempts"]
            if len(attempts) >= 2 and attempts[0].get("exit_class") == "quota_exhausted":
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("the scripted quota attempt never rerouted")
        # The reroute schedules the successor; since collection runs beside the tick
        # (lab findings of 2026-09-29) the tick after it may already have launched it.
        assert midway["state"] in ("scheduled", "running"), midway
        first, second = attempts
        assert first["exit_class"] == "quota_exhausted"
        assert first["image"] == resolved.reference
        assert second["resume_from_remote"] is False


# ----- the login Job, the service-owned Secret and the probe (25, 26, ADR 0015) ----------

# The stand-in harness's credential directory and its "model endpoint". The endpoint is
# a public name the worker's policy permits (resolved and pinned: a worker never takes the
# broad rule, hades #425), so the attempt reaching it and the login Job not reaching it is
# the policy's doing.
STAND_IN_DIR = "/home/worker/.crucible-login"
STAND_IN_MODEL_ENDPOINT = "example.com"


def _reach(host: str) -> str:
    return (
        "r=refused; for i in 1 2 3; do "
        f"if curl -sS -o /dev/null --connect-timeout 5 --max-time 10 https://{host}/; "
        "then r=reached; break; fi; sleep 1; done; "
        'echo "model-endpoint=$r"; '
    )


class StandInLoginAdapter(ScriptHarnessAdapter):
    """A subscription harness stood in for by the script-harness image, the way that
    image stands in for the real harnesses: its login (`crucible-script-harness
    login-stub`) prints a device URL and code and writes `session.json`, its credential
    is that one file, rw-narrow like the real three, and it declares a model endpoint
    and no login endpoint. Its launch refuses to run without the credential."""

    def capabilities(self) -> HarnessCapabilities:
        return replace(
            super().capabilities(), endpoints=(STAND_IN_MODEL_ENDPOINT,), login_endpoints=()
        )

    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=self.name,
            mount_target=STAND_IN_DIR,
            auth_files=(AuthFile("session.json", json=True, json_keys=("authenticated",)),),
            minimum_mode=MountMode.RW_NARROW,
            config_dir_env="CRUCIBLE_LOGIN_DIR",
        )

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        guard = (
            f'test -s "{STAND_IN_DIR}/session.json" '
            '|| { echo "no stand-in credential" >&2; exit 70; }; '
            "echo credential-present; " + _reach(STAND_IN_MODEL_ENDPOINT)
        )
        if ctx.attempt_id == "probe":
            return AdapterLaunch(argv=("sh", "-c", guard + "exit 0"), workdir=ctx.repo_mount)
        return AdapterLaunch(
            argv=("sh", "-c", guard + "exec crucible-script-harness"), workdir=ctx.repo_mount
        )


STAND_IN_FLOW = LoginFlow(
    harness="script-harness",
    argv=("crucible-script-harness", "login-stub"),
    image_binary="/usr/local/bin/crucible-script-harness",
    directory_env="CRUCIBLE_LOGIN_DIR",
    directory_subdir="",
    pastes_code=False,
    captures_token=False,
    token_pattern="",
    token_file="",
    window="stand-in device flow",
)


def _poll_login(admin: TestClient, *states: str, timeout: float = 180) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = admin.get("/v1/admin/credentials/script-harness/login").json()
        if state["state"] in states:
            return state
        time.sleep(0.5)
    raise AssertionError(f"the stand-in login never reached {states}: {state}")


async def test_login_from_an_empty_secret_to_a_probe_and_an_attempt_through_the_admin_api(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    api: KubernetesClient,
    registry: CraneRegistryClient,
) -> None:
    """#92 and #58 on a real cluster: the admin API runs the stand-in harness's login as
    a Job, stores what it wrote in the Secret the service owns, probes it, and one routed
    attempt runs with it. The login Job cannot reach the model endpoint the attempt can."""
    harnesses = HarnessRegistry((StandInLoginAdapter(),))
    provider = _provider(api, registry, harnesses=harnesses)
    clock = SystemClock()
    secret = provider.credential_secret("script-harness")
    with contextlib.suppress(KubernetesApiError):
        api.delete("secrets", secret)
    # What a GitOps-era deployment leaves behind: the Secret exists and holds nothing.
    api.create(
        "secrets",
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret, "namespace": "hades-workers"},
            "type": "Opaque",
        },
    )
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=clock,
        providers=[provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(artifact_root / "kind-login-store"),
        harnesses=harnesses,
    )
    ctx.admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=clock,
        providers={"fake": FakeProvider(), "kubernetes": provider},
        harnesses=harnesses,
        # The supervisor below ticks once up front; its lease outlives the login.
        lease_ttl_seconds=300,
        login_timeout_seconds=180,
        probe_timeout_seconds=120,
        login_flows={"script-harness": STAND_IN_FLOW},
        # The stand-in's CLI checks the model endpoint before it logs in, so the
        # login's own output says whether its Job could reach it.
        login_commands={
            "script-harness": (
                "sh",
                "-c",
                _reach(STAND_IN_MODEL_ENDPOINT)
                + "exec /usr/local/bin/crucible-script-harness login-stub",
            )
        },
    )
    tokens: dict[str, str] = {}
    with ctx.uow_factory() as uow:
        for role in Role:
            tokens[role.value] = mint_token(uow, clock, name=f"login-{role.value}", role=role).token
        uow.commit()
    supervisor = Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        clock,
        holder="e2e-kind-login",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=300,
        grace_seconds=5,
        harnesses=harnesses,
    )
    await supervisor.tick()
    image = os.environ["CRUCIBLE_E2E_KIND_REGISTRY"]
    resolved = await asyncio.to_thread(registry.resolve, image)
    app = create_app(ctx)
    try:
        with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
            routing = e2e_routing_document()
            assert admin.put(
                f"/v1/routing/{routing['name']}/{routing['version']}", json=routing
            ).status_code in (200, 201)
            policy = e2e_policy_document()
            policy["images"]["allowlist"] = ["localhost:*/*"]
            policy["resources"] = {
                "cpus": 1,
                "memory": "256MiB",
                "pids": 128,
                "tmpfs_total": "256MiB",
            }
            uploaded = admin.put(f"/v1/policies/{policy['name']}/{policy['version']}", json=policy)
            assert uploaded.status_code in (200, 201), uploaded.text
            with ctx.uow_factory() as uow:
                promote_for_test(
                    uow,
                    digest=resolved.digest,
                    reference=resolved.reference,
                    harnesses=dict(resolved.harnesses),
                    at=clock.now(),
                    by="e2e-kind",
                    reason="the stand-in login's image",
                )
                uow.commit()

            before = admin.get("/v1/admin/credentials/script-harness").json()
            assert before["state"] == "absent", before
            assert before["source"]["exists"] is True
            assert before["source"]["service_owned"] is False

            started = admin.post(
                "/v1/admin/credentials/script-harness/login", json={"reason": "kind login"}
            )
            assert started.status_code == 200, started.text
            state = _poll_login(admin, "finished", "failed")
            print("login:", json.dumps(state, indent=2))
            assert state["state"] == "finished", state
            assert state["url"] == "https://example.invalid/device"
            assert state["code"] == "C7AA-TEST"
            assert state["credential_written"] is True
            # The login lock (25, 26) was taken on the real API server and is released
            # by uid once the Job is gone.
            lock_selector = f"{k8sspec.LABEL_ROLE}={k8sspec.ROLE_LOGIN_LOCK}"
            for _ in range(60):
                if not api.list_objects("configmaps", label_selector=lock_selector):
                    break
                time.sleep(0.5)
            assert not api.list_objects("configmaps", label_selector=lock_selector)
            # 58: the login Job's egress is its login endpoints, and this harness has
            # none, so the model endpoint is refused from it.
            assert "model-endpoint=refused" in state["output_tail"], state
            finished = admin.post(
                "/v1/admin/credentials/script-harness/login/finish",
                json={"reason": "kind login done"},
            )
            assert finished.json()["shape"]["ok"] is True, finished.text
            stored = api.get("secrets", secret)
            print("secret labels:", stored["metadata"].get("labels"))
            assert stored["metadata"]["labels"][k8sspec.LABEL_MANAGED_BY] == "crucible"
            assert set(stored["data"]) == {"session.json"}
            assert not api.list_objects(
                "jobs", label_selector=f"{k8sspec.LABEL_ROLE}={k8sspec.ROLE_LOGIN}"
            )
            assert not api.list_objects(
                "networkpolicies", label_selector=f"{k8sspec.LABEL_ROLE}={k8sspec.ROLE_LOGIN}"
            )

            validated = admin.post(
                "/v1/admin/credentials/script-harness/validate", json={"reason": "kind probe"}
            )
            assert validated.status_code == 200, validated.text
            report = validated.json()
            print("validate:", json.dumps(report, indent=2))
            assert report["probe"]["exit_class"] == "completed", report
            assert report["validated"] is True
            assert report["credential"]["state"] == "validated"
            # The probe's PVC delete is issued, not waited for, and the
            # `kubernetes.io/pvc-protection` finalizer keeps it listed for a moment (110).
            probe_pvc_selector = f"{k8sspec.LABEL_ADMIN}=probe"
            for _ in range(60):
                if not api.list_objects(
                    "persistentvolumeclaims", label_selector=probe_pvc_selector
                ):
                    break
                time.sleep(0.5)
            assert not api.list_objects("persistentvolumeclaims", label_selector=probe_pvc_selector)

        with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as client:
            register(ctx, "kind-login", _origin("kind-login"))
            document = e2e_contract("E2E-KIND-LOGIN", "kind-login", image)
            document["execution_request"]["provider"] = "kubernetes"
            task_id = submit_and_start(client, document)
            result = await run_until(
                supervisor,
                client,
                task_id,
                {"accepted", "pre_pr_gates_failed"},
                max_ticks=120,
                pause=0.5,
            )
            attempt = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]
            print("attempt:", task_id, attempt["state"], attempt.get("exit_class"), result)
            with engine.begin() as connection:
                chunks = connection.execute(
                    text("SELECT content, gzipped FROM log_chunks WHERE attempt_id = :id"),
                    {"id": attempt["id"]},
                ).all()
            body = b"".join(c.content for c in chunks if not c.gzipped).decode("utf-8", "replace")
            print("attempt log:", body[-1500:])
            assert "credential-present" in body
            assert "model-endpoint=reached" in body, body
            assert attempt["exit_class"] == "completed", attempt
            events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()
            synced = [e for e in events["items"] if e["kind"] == "credential_synced"]
            print("credential sync:", json.dumps([e["payload"] for e in synced], indent=2))
    finally:
        with contextlib.suppress(KubernetesApiError):
            api.delete("secrets", secret)


# ----- hades #173: each harness's real login output, replayed through the Pod's driver ---

# What the stand-in writes once it has read the code: the stand-in credential.
_STAND_IN_SESSION = """printf '{"authenticated": true}' > "$CRUCIBLE_LOGIN_DIR/session.json"; """


def _captured_flow(harness: str) -> LoginFlow:
    """The real harness's flow (its captured prompt, its code handling, its token
    pattern, its guidance) run as the stand-in harness, whose credential is
    `session.json`. Claude Code's token pattern stays set, so the driver runs as it does
    in production, with no early flush of a quiet partial line."""
    return replace(
        FLOWS[harness],
        harness="script-harness",
        argv=("crucible-script-harness", "login-stub"),
        image_binary="/usr/local/bin/crucible-script-harness",
        directory_env="CRUCIBLE_LOGIN_DIR",
    )


def _ui_sign_in(browser: TestClient, token: str, landing: str = "/ui") -> str:
    """Sign in and return the session's CSRF token, read from `landing`. The status page
    at /ui needs the administrative surface, which `_kind_app` does not configure, so a
    test on that app names a page it does serve (hades #425)."""
    form = browser.get("/ui/sign-in")
    nonce = re.search(r'name="csrf" value="([a-f0-9]+)"', form.text)
    assert nonce is not None, form.text
    signed = browser.post(
        "/ui/sign-in",
        data={"csrf": nonce.group(1), "token": token, "next": landing},
        follow_redirects=False,
    )
    assert signed.status_code == 303, signed.text
    page = browser.get(landing)
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)
    assert csrf is not None, page.text
    return csrf.group(1)


def _poll_until(admin: TestClient, done: Any, what: str, timeout: float = 180) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = admin.get("/v1/admin/credentials/script-harness/login").json()
        if done(state):
            return state
        time.sleep(0.5)
    raise AssertionError(f"the replayed login never reached {what}: {state}")


async def test_each_captured_harness_login_reaches_its_state_and_the_page_shows_link_and_box(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    api: KubernetesClient,
    registry: CraneRegistryClient,
) -> None:
    """hades #173 on a real cluster: the login Job runs the real driver under `script`,
    with a stand-in CLI that replays each harness's captured login output byte for byte
    and then reads the code the way that CLI does (Claude Code raw, where only a
    carriage return submits; AGY a line; Codex nothing). The service reaches the right
    state from the Pod log, the Login page shows the sign-in link and, where a code is
    pasted, the code box, and a code submitted through that box reaches the CLI whole."""
    harnesses = HarnessRegistry((StandInLoginAdapter(),))
    provider = _provider(api, registry, harnesses=harnesses)
    clock = SystemClock()
    secret = provider.credential_secret("script-harness")
    with contextlib.suppress(KubernetesApiError):
        api.delete("secrets", secret)
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=clock,
        providers=[provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(artifact_root / "kind-captured-logins"),
        harnesses=harnesses,
    )
    admin_ctx = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=clock,
        providers={"fake": FakeProvider(), "kubernetes": provider},
        harnesses=harnesses,
        lease_ttl_seconds=300,
        login_timeout_seconds=180,
    )
    ctx.admin = admin_ctx
    with ctx.uow_factory() as uow:
        token = mint_token(uow, clock, name="captured-login-admin", role=Role.ADMIN).token
        uow.commit()
    supervisor = Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        clock,
        holder="e2e-kind-captured-login",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=300,
        grace_seconds=5,
        harnesses=harnesses,
    )
    await supervisor.tick()
    resolved = await asyncio.to_thread(registry.resolve, os.environ["CRUCIBLE_E2E_KIND_REGISTRY"])
    with ctx.uow_factory() as uow:
        promote_for_test(
            uow,
            digest=resolved.digest,
            reference=resolved.reference,
            harnesses=dict(resolved.harnesses),
            at=clock.now(),
            by="e2e-kind",
            reason="the captured logins' stand-in image",
        )
        uow.commit()
    app = create_app(ctx)
    page_path = "/ui/credentials/script-harness/login"
    # Claude Code's code carries its `#state`; AGY's is pasted as the whole redirect
    # address, percent-encoded, and reaches the CLI decoded.
    cases = {
        "claude_code": ("the-pasted-code#state-part", "the-pasted-code#state-part"),
        "agy": (
            "http://localhost:38123/oauth-callback?state=S&code=4%2F0Afixture-code&scope=x",
            "4/0Afixture-code",
        ),
        "codex": (None, None),
    }
    try:
        with (
            TestClient(app, headers={"Authorization": f"Bearer {token}"}) as admin,
            TestClient(app) as browser,
        ):
            csrf = _ui_sign_in(browser, token)
            for harness, (pasted, reaches) in cases.items():
                admin_ctx.login_flows["script-harness"] = _captured_flow(harness)
                admin_ctx.login_commands["script-harness"] = (
                    "bash",
                    "-c",
                    replay_script(
                        harness,
                        finish=_STAND_IN_SESSION + ("sleep 12; " if pasted is None else ""),
                    ),
                )
                started = admin.post(
                    "/v1/admin/credentials/script-harness/login",
                    json={"reason": f"kind: {harness} capture", "replace": True},
                )
                assert started.status_code == 200, started.text
                if pasted is None:
                    state = _poll_until(admin, lambda s: s["code"] is not None, "the device code")
                    assert state["state"] == "waiting_for_operator", state
                    assert state["url"] == "https://auth.openai.com/codex/device"
                    assert state["code"] == "TEST-C0DE9"
                    page = html.unescape(browser.get(page_path).text)
                    assert 'class="login-url">https://auth.openai.com/codex/device<' in page
                    assert "Device code: <code>TEST-C0DE9</code>" in page
                    assert 'name="code"' not in page
                else:
                    state = _poll_until(
                        admin, lambda s: s["state"] == "waiting_for_code", "waiting_for_code"
                    )
                    print(harness, "waiting:", json.dumps(state, indent=2))
                    flow = FLOWS[harness]
                    assert re.search(flow.prompt_pattern, state["prompt"] or ""), state
                    assert state["url"] and state["url"].startswith("https://"), state
                    assert state["guidance"] == list(flow.guidance)
                    page = html.unescape(browser.get(page_path).text)
                    assert f'class="login-url">{state["url"]}<' in page
                    assert 'name="code"' in page and "Submit code" in page
                    assert state["prompt"] in page
                    submitted = browser.post(
                        "/ui/actions/login-code",
                        data={
                            "csrf": csrf,
                            "harness": "script-harness",
                            "code": pasted,
                            "return_to": page_path,
                        },
                        follow_redirects=False,
                    )
                    assert submitted.status_code == 303, submitted.text
                    assert "kind=bad" not in submitted.headers["location"], submitted.headers
                state = _poll_until(
                    admin, lambda s: s["state"] in ("finished", "failed"), "its end"
                )
                print(harness, "ended:", json.dumps(state, indent=2))
                assert state["state"] == "finished", state
                assert state["credential_written"] is True, state
                tail = "\n".join(state["output_tail"])
                assert "chmod" not in tail
                if pasted is not None and reaches is not None:
                    assert f"stand-in read {len(reaches)} characters" in tail, tail
                    # The CLI's echo of the code reaches the page masked.
                    assert "[pasted code]" in tail, tail
                    assert pasted not in tail and reaches not in tail
    finally:
        with contextlib.suppress(KubernetesApiError):
            api.delete("secrets", secret)


# ----- hades #189, #190, #191: a git host that answers, and one that never does -----

# tools/kind/e2e-kind.sh puts these on the node: .10 and .20 are range-http serving the
# tier's bare repositories as github.com; cluster DNS answers github.com with .20; .30
# drops every packet.
GIT_HOST_POLICY = "198.51.100.10"
GIT_HOST_DNS = "198.51.100.20"
GIT_HOST_SILENT = "198.51.100.30"


class CreateRecordingClient(RecordingKubernetesClient):
    """Keep every object the provider created, with when, for a test to read after the
    provider has deleted it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.made: list[tuple[float, str, dict[str, Any]]] = []

    def create(self, kind: str, body: Any, **kwargs: Any) -> Any:
        self.made.append((time.monotonic(), kind, json.loads(json.dumps(body))))
        return super().create(kind, body, **kwargs)


def _recording_client() -> CreateRecordingClient:
    return CreateRecordingClient(
        kubeconfig_access(os.environ["CRUCIBLE_E2E_KIND_KUBECONFIG"]),
        "hades-workers",
        timeout=15,
    )


def _git_host(address: str) -> Any:
    def resolve(host: str) -> list[str]:
        return [f"{address}/32"] if host in ("github.com", "api.github.com") else []

    return resolve


def _stand_in_repository(name: str) -> tuple[str, Path]:
    """A bare repository range-http serves as http://github.com:443/git/<name>.git."""
    bare = Path(make_origin(Path(os.environ["CRUCIBLE_E2E_KIND_CACHE"]), name))
    subprocess.run(["git", "--git-dir", str(bare), "update-server-info"], check=True)
    for path in bare.rglob("*"):
        path.chmod(0o777 if path.is_dir() else 0o666)
    return f"http://github.com:443/git/{bare.name}", bare


def _jobs(client: CreateRecordingClient, attempt_id: str) -> list[tuple[float, dict[str, Any]]]:
    return [
        (at, body)
        for at, kind, body in client.made
        if kind == "jobs" and body["metadata"]["labels"].get(k8sspec.LABEL_ATTEMPT) == attempt_id
    ]


def _selects(policy: dict[str, Any], labels: dict[str, str]) -> bool:
    wanted = policy["spec"]["podSelector"].get("matchLabels", {})
    return all(labels.get(key) == value for key, value in wanted.items())


def _permits(policy: dict[str, Any], address: str) -> bool:
    return any(
        any(peer.get("ipBlock", {}).get("cidr") == f"{address}/32" for peer in rule.get("to", []))
        and any(port.get("port") == 443 for port in rule.get("ports", []))
        for rule in policy["spec"]["egress"]
    )


async def test_hades_191_git_pods_reach_the_address_their_policy_permits(
    registry: CraneRegistryClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hades #191 on Calico, with the provider's real NetworkPolicies enforced.

    The lab's refresher timed out on github.com while the preparer reached it. The
    refresher's Pod is selected by its policy, and the policy permits the address the
    provider resolved; what the policy cannot permit is the address the Pod looks up for
    itself when that answer has changed. Reproduced first without `hostAliases`, then
    shown fixed with them, and the refresh that cannot connect costs its 20 second cap."""
    client = _recording_client()
    url, bare = _stand_in_repository("hades-191")
    # Written by the refresher's uid; the tier's cleanup removes it with the scratch.
    mirror = bare.parent / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.git"

    broken = _provider(
        client,
        registry,
        resolver=_git_host(GIT_HOST_POLICY),
        broad_egress=False,
        prepare_timeout_seconds=45,
    )
    lab = _spec(61, url)
    with monkeypatch.context() as patched:
        patched.setattr(k8sspec, "host_aliases", lambda plan: [])
        with pytest.raises(ProviderError, match="preparer Job could not build"):
            await broken.prepare(lab)
    await broken._delete_attempt_objects(lab.attempt_id)
    jobs = _jobs(client, lab.attempt_id)
    refresher = next(body for _, body in jobs if body["metadata"]["name"].startswith("refresh"))
    labels = refresher["spec"]["template"]["metadata"]["labels"]
    policies = [body for _, kind, body in client.made if kind == "networkpolicies"]
    selecting = [p for p in policies if _selects(p, labels)]
    # The issue's first suspicion, ruled out: the refresher is selected, and permitted.
    assert [p["metadata"]["name"] for p in selecting] == [
        k8sspec.object_name("np-cache-refresher", lab.attempt_id)
    ]
    assert _permits(selecting[0], GIT_HOST_POLICY)
    assert "hostAliases" not in refresher["spec"]["template"]["spec"]
    assert not mirror.exists(), "the refresher reached the git host it resolved for itself"
    # The refresh that could not connect cost its cap, not two kernel connect timeouts.
    started = {body["metadata"]["name"].split("-")[0]: at for at, body in jobs}
    refresh_seconds = started["prepare"] - started["refresh"]
    print(
        f"hades-191: without hostAliases the refresher (labels {labels}) was selected by "
        f"{selecting[0]['metadata']['name']} permitting {GIT_HOST_POLICY}:443, resolved "
        f"github.com to {GIT_HOST_DNS} itself, and gave up after {refresh_seconds:.1f}s"
    )
    assert 20 <= refresh_seconds < 60, refresh_seconds

    fixed = _provider(client, registry, resolver=_git_host(GIT_HOST_POLICY), broad_egress=False)
    spec = _spec(62, url)
    workspace = await fixed.prepare(spec)
    try:
        assert (mirror / "HEAD").is_file(), "the refresher did not build the mirror"
        print(f"hades-191: with hostAliases the refresher built {mirror.name}")
        for _, body in _jobs(client, spec.attempt_id):
            assert body["spec"]["template"]["spec"]["hostAliases"] == [
                {"ip": GIT_HOST_POLICY, "hostnames": ["api.github.com", "github.com"]}
            ]
    finally:
        await fixed.cleanup(workspace, CleanupPolicy.DELETE, spec)


# hades #425: a name the policy allowlists for the worker, stood in for by the tier's
# blackhole address: a host the policy permits and that never answers.
SILENT_HOST = "silent.example"


def _stub_hosts(git_address: str, silent_address: str) -> Any:
    def resolve(host: str) -> list[str]:
        if host in ("github.com", "api.github.com"):
            return [f"{git_address}/32"]
        if host == SILENT_HOST:
            return [f"{silent_address}/32"]
        return []

    return resolve


async def test_hades_425_a_worker_reaches_an_allowlisted_host_and_the_probe_records_it(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    api: KubernetesClient,
    registry: CraneRegistryClient,
    provider: KubernetesProvider,
) -> None:
    """hades #425 on Calico, with the provider's real NetworkPolicies enforced.

    The lab's worker ran under a policy that allowlists github.com and its curl to
    github.com timed out: the provider subtracted github.com from the worker's rules on
    26's old sentence that a worker never reaches GitHub, so the allowlist was not
    enforced as written. Here the tier's stand-in github.com (range-http on
    198.51.100.10) is the allowlisted host: under resolved rules and hostAliases the
    worker fetches from it, the launch wrapper's probe reports it reachable and the
    allowlisted-but-silent host unreachable, and under the supervisor the probe lands on
    the attempt record and the task page. Part one runs on the tier's `broad_egress`,
    as the isolation test does, and the worker's curl to example.com (on no allowlist)
    is refused under the same NetworkPolicy and wrapper."""
    # Part one: the provider's resolved rules, the way the lab runs them.
    client = _recording_client()
    fixed = _provider(
        client,
        registry,
        resolver=_stub_hosts(GIT_HOST_POLICY, GIT_HOST_SILENT),
    )
    url, bare = _stand_in_repository("hades-425")
    # The tier's own `broad_egress`, which the isolation test runs under too: the worker
    # still gets its resolved allowlist, so the host it names answers and example.com,
    # which no policy names (the isolation probe's egress-not-allowlisted), does not.
    fetch = (
        f"if curl -sS --connect-timeout 5 --max-time 10 http://github.com:443/git/{bare.name}/HEAD;"
        " then echo ' git-host=reached'; else echo 'git-host=denied'; fi; "
        "if curl -sS -f --connect-timeout 5 --max-time 10 https://example.com/ >/dev/null;"
        " then echo 'unlisted=reached'; else echo 'unlisted=refused'; fi"
    )
    spec = _spec(63, url, command=("sh", "-c", fetch), network_hosts=("github.com", SILENT_HOST))
    workspace = await fixed.prepare(spec)
    try:
        handle = await fixed.launch(workspace, spec)
        observed = await _terminal(fixed, handle)
        assert observed.exit_code == 0, observed
        body = b"".join(chunk.content for chunk in await fixed.logs(handle, LogOffset())).decode(
            "utf-8", "replace"
        )
        assert "ref: refs/heads/main" in body and "git-host=reached" in body, body
        assert "unlisted=refused" in body and "unlisted=reached" not in body, body
        probe = find_probe(body)
        assert probe is not None, body
        by_host = {row["host"]: row for row in probe["hosts"]}
        assert set(by_host) == {"github.com", SILENT_HOST}, body
        # range-http speaks plain HTTP on 443, so the TLS handshake fails after the
        # connection was made: reachable, curl 35.
        assert by_host["github.com"]["reachable"] is True, by_host
        assert by_host[SILENT_HOST]["reachable"] is False, by_host
        assert by_host[SILENT_HOST]["curl_exit"] == 28, by_host
        worker = next(
            body
            for _, kind, body in client.made
            if kind == "jobs"
            and body["metadata"]["name"].startswith("worker-")
            and body["metadata"]["labels"].get(k8sspec.LABEL_ATTEMPT) == spec.attempt_id
        )
        aliases = worker["spec"]["template"]["spec"]["hostAliases"]
        assert {"ip": GIT_HOST_POLICY, "hostnames": ["github.com"]} in aliases, aliases
        policies = [body for _, kind, body in client.made if kind == "networkpolicies"]
        selecting = [
            p for p in policies if _selects(p, worker["spec"]["template"]["metadata"]["labels"])
        ]
        assert len(selecting) == 1 and _permits(selecting[0], GIT_HOST_POLICY), selecting
        assert all(
            peer.get("ipBlock", {}).get("cidr") != "0.0.0.0/0"
            for rule in selecting[0]["spec"]["egress"]
            for peer in rule.get("to", [])
        ), selecting
        assert "github.com" in selecting[0]["metadata"]["annotations"][k8sspec.ANNOTATION_EGRESS]
        print(
            f"hades-425: the worker under {selecting[0]['metadata']['name']} fetched from "
            f"github.com ({GIT_HOST_POLICY}); probe {by_host}"
        )
    finally:
        await fixed.cleanup(workspace, CleanupPolicy.DELETE, spec)

    # Part two: through the supervisor, the record on the attempt and the task page.
    # The tier's policy allowlists github.com, which cluster DNS answers as the stand-in.
    ctx, tokens, app, harnesses = _kind_app(
        engine, migrated, artifact_root, provider, registry, version=42
    )
    headers = {"Authorization": f"Bearer {tokens['operator']}"}
    with TestClient(app, headers=headers) as api_client:
        supervisor = Supervisor(
            ctx.uow_factory,
            {"kubernetes": provider},
            ctx.clock,
            holder="e2e-kind-hades-425",
            artifact_store=ctx.artifact_store,
            lease_ttl_seconds=120,
            grace_seconds=5,
            harnesses=harnesses,
        )
        task = _kind_task(api_client, ctx, "hades-425-task", _origin("hades-425-task"), version=42)
        await run_until(
            supervisor,
            api_client,
            task,
            {"accepted", "pre_pr_gates_failed"},
            max_ticks=120,
            pause=0.5,
        )
        await supervisor.stop()
        latest = api_client.get(f"/v1/tasks/{task}").json()["latest_attempt"]
        assert latest["egress_probe"] is not None, latest
        attempt = api_client.get(f"/v1/attempts/{latest['id']}").json()
        recorded = attempt["egress_probe"]
        assert [row["host"] for row in recorded["hosts"]] == ["github.com"], recorded
        assert recorded["hosts"][0]["reachable"] is True, recorded
        assert recorded["recorded_at"], recorded
    with TestClient(app) as browser:
        _ui_sign_in(browser, tokens["operator"], landing=f"/ui/tasks/{task}")
        page = browser.get(f"/ui/tasks/{task}")
        assert page.status_code == 200, page.text
        assert "Egress" in page.text and "github.com" in page.text, page.text
        assert "reachable" in page.text, page.text


def _kind_app(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    provider: KubernetesProvider,
    registry: CraneRegistryClient,
    *,
    version: int = 21,
    stall_seconds: tuple[int, int] | None = None,
) -> tuple[AppContext, dict[str, str], Any, Any]:
    """The API on the tier's database, the e2e policy at `version` (21 unless named)
    with two script-harness workers allowed at once and, when given, the stall limits
    `(warn, fail)`, and the tier's image promoted."""
    clock = SystemClock()
    harnesses = application_harnesses(test_fixtures=True)
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=clock,
        providers=[provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(artifact_root / "kind-store"),
        harnesses=harnesses,
        lease_ttl_seconds=15,
    )
    tokens: dict[str, str] = {}
    with ctx.uow_factory() as uow:
        for role in Role:
            tokens[role.value] = mint_token(uow, clock, name=f"kind-{role.value}", role=role).token
        uow.commit()
    app = create_app(ctx)
    resolved = registry.resolve(os.environ["CRUCIBLE_E2E_KIND_REGISTRY"])
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
        routing = e2e_routing_document()
        assert admin.put(
            f"/v1/routing/{routing['name']}/{routing['version']}", json=routing
        ).status_code in (200, 201)
        policy = e2e_policy_document(version=version)
        policy["images"]["allowlist"] = ["localhost:*/*"]
        policy["resources"] = {"cpus": 1, "memory": "256MiB", "pids": 128, "tmpfs_total": "256MiB"}
        policy["concurrency"]["per_harness"]["script-harness"] = 2
        if stall_seconds is not None:
            warn, fail = stall_seconds
            policy["limits"]["stall_warn_seconds"] = warn
            policy["limits"]["stall_fail_seconds"] = fail
        response = admin.put(f"/v1/policies/{policy['name']}/{policy['version']}", json=policy)
        assert response.status_code in (200, 201), response.text
    with ctx.uow_factory() as uow:
        promote_for_test(
            uow,
            digest=resolved.digest,
            reference=resolved.reference,
            harnesses=dict(resolved.harnesses) or {"script-harness": "1.0.0"},
            at=clock.now(),
            by="e2e-kind",
            reason="kind script harness image",
        )
        uow.commit()
    return ctx, tokens, app, harnesses


def _kind_task(
    client: TestClient, ctx: AppContext, name: str, url: str, *, version: int = 21
) -> str:
    register(ctx, name, url)
    document = e2e_contract(f"E2E-{name.upper()}", name, os.environ["CRUCIBLE_E2E_KIND_REGISTRY"])
    document["execution_request"]["provider"] = "kubernetes"
    document["policy"]["version"] = version
    return submit_and_start(client, document)


def _worker_objects(api: KubernetesClient, attempt_id: str) -> list[str]:
    selector = f"{k8sspec.LABEL_ATTEMPT}={attempt_id},{k8sspec.LABEL_ROLE}={k8sspec.ROLE_WORKER}"
    return [
        f"{kind}/{item['metadata']['name']}"
        for kind in ("jobs", "pods")
        for item in api.list_objects(kind, label_selector=selector)
    ]


async def _preparer_running(api: KubernetesClient, attempt_id: str, supervisor: Any) -> None:
    selector = f"{k8sspec.LABEL_ATTEMPT}={attempt_id},{k8sspec.LABEL_ROLE}={k8sspec.ROLE_PREPARER}"
    for _ in range(120):
        await supervisor.tick()
        if api.list_objects("pods", label_selector=selector):
            return
        await asyncio.sleep(0.5)
    raise AssertionError("the preparer Pod never appeared")


def _slow_provider(api: KubernetesClient, registry: CraneRegistryClient) -> KubernetesProvider:
    """A git host that drops every packet: the refresher gives up at its 20 second cap
    and the preparer hangs until its deadline unless something ends it. A task on the
    tier's own file:// origin prepares as usual beside it."""
    return _provider(
        api,
        registry,
        resolver=_git_host(GIT_HOST_SILENT),
        broad_egress=False,
        prepare_timeout_seconds=600,
    )


async def test_hades_189_a_cancel_during_a_hanging_prepare_starts_no_worker(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    api: KubernetesClient,
    registry: CraneRegistryClient,
) -> None:
    provider = _slow_provider(api, registry)
    ctx, tokens, app, harnesses = _kind_app(engine, migrated, artifact_root, provider, registry)
    supervisor = Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        ctx.clock,
        holder="e2e-kind-189",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=15,
        grace_seconds=5,
        harnesses=harnesses,
    )
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as client:
        task_id = _kind_task(client, ctx, "hades-189", "http://github.com:443/git/never.git")
        attempt_id = ""
        for _ in range(20):
            attempt = client.get(f"/v1/tasks/{task_id}").json().get("latest_attempt")
            if attempt:
                attempt_id = str(attempt["id"])
                break
            await supervisor.tick()
        assert attempt_id
        await _preparer_running(api, attempt_id, supervisor)
        assert client.get(f"/v1/attempts/{attempt_id}").json()["state"] == "preparing"

        cancel = client.post(
            f"/v1/tasks/{task_id}/cancel",
            json={"reason": "hades #189 proof", "verbatim": "cancel it", "decided_by": "tests"},
        )
        assert cancel.status_code == 200 and cancel.json()["state"] == "cancelling"
        cancelled_at = time.monotonic()
        state = ""
        ticks = 0
        while time.monotonic() - cancelled_at < 60:
            assert _worker_objects(api, attempt_id) == []
            await supervisor.tick()
            ticks += 1
            state = client.get(f"/v1/tasks/{task_id}").json()["state"]
            if state == "cancelled":
                break
            await asyncio.sleep(0.5)
        elapsed = time.monotonic() - cancelled_at
        assert state == "cancelled", state
        print(f"hades-189: cancelled {elapsed:.1f}s after the cancel, in {ticks} tick(s)")
        assert elapsed < 30
        attempt = client.get(f"/v1/attempts/{attempt_id}").json()
        assert attempt["state"] == "failed" and attempt["exit_class"] == "killed"
        events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()
        collected = next(e for e in events["items"] if e["kind"] == "attempt_collected")
        assert collected["payload"]["stage"] == "prepare"
        assert "attempt_running" not in [e["kind"] for e in events["items"]]
        # Nothing of the launch is left running, and no worker ever existed.
        await _pods_gone(api, attempt_id, timeout=30)
        assert _worker_objects(api, attempt_id) == []
    await supervisor.stop()


async def test_hades_190_a_hanging_prepare_leaves_the_lease_readiness_and_other_tasks(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    api: KubernetesClient,
    registry: CraneRegistryClient,
) -> None:
    """A prepare hanging past the lease TTL (15 s here): every tick still renews the
    lease, `/v1/ready` stays 200, and another task launches. Then the supervisor stops,
    and the API stays ready while the admin UI says the supervisor is not healthy."""
    provider = _slow_provider(api, registry)
    ctx, tokens, app, harnesses = _kind_app(engine, migrated, artifact_root, provider, registry)
    supervisor = Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        ctx.clock,
        holder="e2e-kind-190",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=15,
        grace_seconds=5,
        harnesses=harnesses,
    )
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as client:
        slow = _kind_task(client, ctx, "hades-190-slow", "http://github.com:443/git/never.git")
        await supervisor.tick()
        slow_attempt = str(client.get(f"/v1/tasks/{slow}").json()["latest_attempt"]["id"])
        await _preparer_running(api, slow_attempt, supervisor)
        # The other worker runs through the whole window, so every tick is the launch
        # and observation path; a collect is its own step and comes after (see below).
        other = _kind_task(client, ctx, "hades-190-other", _origin("hades-190-other", "hang"))

        hang_started = time.monotonic()
        longest_tick = 0.0
        other_launched = False
        while time.monotonic() - hang_started < 50:
            before = time.monotonic()
            result = await supervisor.tick()
            longest_tick = max(longest_tick, time.monotonic() - before)
            assert result.held, "the lease was lost while a prepare hung"
            ready = client.get("/v1/ready")
            assert ready.status_code == 200, ready.text
            sup = client.get("/v1/supervisor").json()
            assert sup["healthy"] is True, sup
            other_attempt = client.get(f"/v1/tasks/{other}").json().get("latest_attempt")
            other_launched = other_launched or bool(other_attempt and other_attempt["started_at"])
            await asyncio.sleep(1)
        print(f"hades-190: longest tick {longest_tick:.1f}s while the prepare hung for 50s")
        assert longest_tick < 15
        assert other_launched, "the other task never launched beside the hanging prepare"
        assert client.get(f"/v1/attempts/{slow_attempt}").json()["state"] == "preparing"
        other_view = client.get(f"/v1/tasks/{other}").json()["latest_attempt"]
        assert other_view["state"] == "running", other_view
        for task in (slow, other):
            response = client.post(
                f"/v1/tasks/{task}/cancel",
                json={"reason": "hades #190 proof done", "verbatim": "stop", "decided_by": "tests"},
            )
            assert response.status_code == 200
        for task in (slow, other):
            assert (
                await run_until(supervisor, client, task, {"cancelled"}, max_ticks=120)
                == "cancelled"
            )

        # The supervisor stops; past the lease the API is still ready and says why the
        # supervisor is not healthy, and the admin UI shows it on every page.
        await supervisor.stop()
        await asyncio.sleep(16)
        ready = client.get("/v1/ready")
        assert ready.status_code == 200 and ready.json()["supervisor"]["ok"] is False
    with TestClient(app) as browser:
        form = browser.get("/ui/sign-in")
        nonce = re.search(r'name="csrf" value="([a-f0-9]+)"', form.text)
        assert nonce is not None
        signed_in = browser.post(
            "/ui/sign-in",
            data={"csrf": nonce.group(1), "token": tokens["admin"], "next": "/ui/tasks"},
            follow_redirects=False,
        )
        assert signed_in.status_code == 303
        page = browser.get("/ui/tasks")
        assert page.status_code == 200
        assert "The supervisor is not healthy." in page.text
        assert "e2e-kind-190 released the lease and no supervisor holds it" in page.text
        print("hades-190: UI banner:", re.search(r'role="alert">(.*?)</div>', page.text).group(1))  # type: ignore[union-attr]


# ----- FDY-0133: the Kubernetes publisher ---------------------------------

# A smart-HTTP git host on port 443 of its own Pod: the tier's range-http serves the
# bare repositories read-only over dumb HTTP, which nothing can push to. The worker image
# carries git (with `git-http-backend`) and perl and no web server, so this is the
# smallest server that turns one into the other: one fork per connection, the request
# handed to `git-http-backend` as CGI, the answer passed back, the connection closed.
# Every request line and status goes to `push-host.log` beside the repositories, never a
# header: the publisher's helper answers only for https, so no credential ever crosses
# this plain-HTTP stand-in.
GIT_PUSH_HOST_SERVER = r"""
use strict; use warnings; use IO::Socket::INET; use IPC::Open2;
$SIG{CHLD} = 'IGNORE';
my $srv = IO::Socket::INET->new(LocalAddr => '0.0.0.0', LocalPort => 443, Listen => 32,
  ReuseAddr => 1) or die "listen: $!";
open(my $log, '>>', '/srv/git/push-host.log') or die "log: $!";
select((select($log), $| = 1)[0]);
while (1) {
  my $c = $srv->accept or next;
  my $pid = fork;
  if (!defined $pid || $pid) { close $c; next; }
  $SIG{CHLD} = 'DEFAULT';
  binmode $c;
  my $line = <$c> // ''; $line =~ s/\r?\n\z//;
  my ($method, $uri) = split / /, $line;
  my %h;
  while (my $l = <$c>) {
    $l =~ s/\r?\n\z//; last if $l eq '';
    my ($k, $v) = split /:\s*/, $l, 2; $h{lc $k} = $v // '';
  }
  print $c "HTTP/1.1 100 Continue\r\n\r\n" if ($h{expect} // '') =~ /100-continue/i;
  my ($path, $query) = split /\?/, ($uri // '/'), 2;
  $path =~ s{^/git}{};
  my $body = '';
  if (defined $h{'content-length'}) {
    my $want = $h{'content-length'};
    while (length($body) < $want) {
      my $got = read($c, my $buf, $want - length($body)); last unless $got; $body .= $buf;
    }
  } elsif (($h{'transfer-encoding'} // '') =~ /chunked/i) {
    while (1) {
      my $size = <$c> // '0'; $size =~ s/\r?\n\z//; my $n = hex $size;
      if ($n == 0) { <$c>; last; }
      my $chunk = ''; while (length($chunk) < $n) {
        my $got = read($c, my $buf, $n - length($chunk)); last unless $got; $chunk .= $buf;
      }
      $body .= $chunk; <$c>;
    }
  }
  $ENV{GIT_PROJECT_ROOT} = '/srv/git'; $ENV{GIT_HTTP_EXPORT_ALL} = '1';
  $ENV{GIT_CONFIG_COUNT} = '1'; $ENV{GIT_CONFIG_KEY_0} = 'safe.directory';
  $ENV{GIT_CONFIG_VALUE_0} = '*'; $ENV{HOME} = '/tmp';
  $ENV{PATH_INFO} = $path; $ENV{REQUEST_METHOD} = $method // 'GET';
  $ENV{QUERY_STRING} = $query // ''; $ENV{CONTENT_TYPE} = $h{'content-type'} // '';
  $ENV{CONTENT_LENGTH} = length $body; $ENV{REMOTE_ADDR} = $c->peerhost // '';
  $ENV{HTTP_CONTENT_ENCODING} = $h{'content-encoding'} // '';
  $ENV{GIT_HTTP_MAX_REQUEST_BUFFER} = '100M';
  my $cgi = open2(my $out, my $in, '/usr/lib/git-core/git-http-backend');
  binmode $in; binmode $out; print $in $body; close $in;
  my $status = '200 OK'; my @headers;
  while (my $l = <$out>) {
    $l =~ s/\r?\n\z//; last if $l eq '';
    if ($l =~ /^Status:\s*(.*)/i) { $status = $1 } else { push @headers, $l }
  }
  my $rest = do { local $/; <$out> } // '';
  waitpid $cgi, 0;
  print $c "HTTP/1.1 $status\r\n", map("$_\r\n", @headers), "Connection: close\r\n\r\n", $rest;
  print $log "$method $uri $status\n";
  close $c; exit 0;
}
"""
GIT_PUSH_HOST_NAMESPACE = "crucible-kind-git"


def _admin_kubectl(*args: str, stdin: str | None = None) -> str:
    """kubectl as the tier's cluster administrator (the KUBECONFIG e2e-kind.sh exports),
    for the stand-in's own namespace; the provider never uses this account."""
    return subprocess.run(
        ["kubectl", *args], input=stdin, check=True, capture_output=True, text=True
    ).stdout


@contextlib.contextmanager
def _git_push_host() -> Any:
    """The pushable github.com: a Pod serving the tier's bare repositories (the same
    host directory range-http serves) over smart HTTP on 443, and its Pod address."""
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": GIT_PUSH_HOST_NAMESPACE},
    }
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "git-push-host",
            "namespace": GIT_PUSH_HOST_NAMESPACE,
            "labels": {"app": "git-push-host"},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "runAsNonRoot": True,
                # Port 443 without a capability: a namespaced, safe sysctl.
                "sysctls": [{"name": "net.ipv4.ip_unprivileged_port_start", "value": "0"}],
            },
            "containers": [
                {
                    "name": "git",
                    "image": os.environ["CRUCIBLE_E2E_KIND_REGISTRY"],
                    "command": ["perl", "-e", GIT_PUSH_HOST_SERVER],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "readinessProbe": {"tcpSocket": {"port": 443}, "periodSeconds": 1},
                    "volumeMounts": [{"name": "git", "mountPath": "/srv/git"}],
                }
            ],
            "volumes": [
                {"name": "git", "hostPath": {"path": "/crucible-kind-cache", "type": "Directory"}}
            ],
        },
    }
    try:
        _admin_kubectl("apply", "-f", "-", stdin=json.dumps(namespace))
        _admin_kubectl("apply", "-f", "-", stdin=json.dumps(pod))
        _admin_kubectl(
            "-n",
            GIT_PUSH_HOST_NAMESPACE,
            "wait",
            "--for=condition=Ready",
            "pod/git-push-host",
            "--timeout=120s",
        )
        address = _admin_kubectl(
            "-n",
            GIT_PUSH_HOST_NAMESPACE,
            "get",
            "pod",
            "git-push-host",
            "-o",
            "jsonpath={.status.podIP}",
        ).strip()
        assert address, "the git push host has no Pod address"
        yield address
    finally:
        with contextlib.suppress(subprocess.CalledProcessError):
            _admin_kubectl(
                "delete", "namespace", GIT_PUSH_HOST_NAMESPACE, "--wait=true", "--timeout=120s"
            )


def _denied_except(address: str) -> tuple[str, ...]:
    """26's denied ranges with one hole: the stand-in git host's own Pod address. Every
    other address in 10.0.0.0/8, and every other denied range, stays denied, and the
    publisher's policy still names that one /32 on 443 and nothing else."""
    hole = ipaddress.ip_network(f"{address}/32")
    out: list[str] = []
    for cidr in k8sspec.DEFAULT_DENIED_CIDRS:
        network = ipaddress.ip_network(cidr)
        if hole.subnet_of(network):  # type: ignore[arg-type]
            out.extend(str(n) for n in network.address_exclude(hole))  # type: ignore[arg-type]
        else:
            out.append(cidr)
    return tuple(out)


def _branch_head(bare: Path, branch: str) -> str | None:
    result = subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=*",
            "--git-dir",
            str(bare),
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def _app_key(directory: Path) -> str:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = directory / "fdy-0133-app.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return str(path)


async def test_fdy_0133_a_task_in_publishing_is_pushed_by_the_kubernetes_publisher(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    registry: CraneRegistryClient,
) -> None:
    """A task accepted on the Kubernetes provider is pushed by the Kubernetes publisher:
    a real Job under Calico with its own NetworkPolicy, the token from a per-push Secret,
    the bundle off the attempt's claim, to a real git remote, and a stand-in GitHub API
    that reads the branch head from that remote. The branch lands at the accepted head,
    the pull request opens, and the Job, its Pod, its policy and the Secret are gone.

    FDY-0135: the policy names a trailer key the script harness never writes, so the
    push succeeds only because Crucible's commit-msg hook, projected from the identity
    ConfigMap into the real worker Pod, added `Crucible-Task: E2E-FDY-0133`, and the
    commit_policy gate passed it before review."""
    name = "fdy-0133"
    url, bare = _stand_in_repository(name)
    subprocess.run(
        ["git", "--git-dir", str(bare), "config", "http.receivepack", "true"], check=True
    )
    full_name = f"git/{name}"
    with _git_push_host() as host, FakeGitHubServer() as github:
        github.state.add_repository(full_name)
        github.state.ref_source = lambda _repo, branch: _branch_head(bare, branch)
        transport = RestTransport(github.url, timeout=10.0)
        github_client = RestGitHubClient(
            AppAuthenticator(
                AppConfig(
                    app_id=4969317, private_key_path=_app_key(artifact_root), api_base=github.url
                ),
                transport,
            ),
            transport,
        )
        client_api = _recording_client()
        provider = _provider(
            client_api,
            registry,
            resolver=_git_host(host),
            broad_egress=False,
            denied_cidrs=_denied_except(host),
        )
        ctx, tokens, app, harnesses = _kind_app(engine, migrated, artifact_root, provider, registry)
        with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
            policy = admin.get("/v1/policies/e2e-script/21").json()["document"]
            policy["version"] = 22
            policy["git"]["commit_trailer"] = "Crucible-Task"
            response = admin.put("/v1/policies/e2e-script/22", json=policy)
            assert response.status_code in (200, 201), response.text
        with ctx.uow_factory() as uow:
            register_repository(
                uow,
                ctx.clock,
                principal_name="tests",
                name=name,
                registration=RepositoryRegistration(
                    url=url,
                    default_branch="main",
                    policy_name="e2e-script",
                    installation_id=1,
                    external_review=ExternalReviewAttestation(
                        attested_all_prs=True, attested_by="tests"
                    ),
                ),
            )
            uow.commit()
        supervisor = Supervisor(
            ctx.uow_factory,
            {"kubernetes": provider},
            ctx.clock,
            holder="e2e-kind-fdy-0133",
            artifact_store=ctx.artifact_store,
            lease_ttl_seconds=120,
            grace_seconds=5,
            harnesses=harnesses,
            github=github_client,
            publisher=KubernetesPublisher(provider),
            delivery_config=DeliveryConfig(
                poll_interval_seconds=0, reactions_poll_interval_seconds=0
            ),
        )
        with TestClient(
            app, headers={"Authorization": f"Bearer {tokens['orchestrator']}"}
        ) as client:
            document = e2e_contract("E2E-FDY-0133", name, os.environ["CRUCIBLE_E2E_KIND_REGISTRY"])
            document["execution_request"]["provider"] = "kubernetes"
            document["policy"]["version"] = 22
            document["deliverables"] = [
                {"kind": "pull_request", "target": "main", "draft": False, "closes": []}
            ]
            started = time.monotonic()
            task_id = submit_and_start(client, document)
            state = await run_until(
                supervisor,
                client,
                task_id,
                {
                    "awaiting_external_review",
                    "awaiting_ci_certification",
                    "ready_for_merge",
                    "publish_failed",
                    "pre_pr_gates_failed",
                },
                max_ticks=100,
                pause=0.5,
                min_seconds=0,
            )
            gates = gate_results(client, task_id)
            assert gates["commit_policy"] == "pass", gates
            assert state != "pre_pr_gates_failed", gates
            head = str(client.get(f"/v1/tasks/{task_id}").json()["head_sha"])
            branch = "crucible/E2E-FDY-0133"
            events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()
            finished = [e for e in events["items"] if e["kind"] == "publisher_finished"]
            assert state != "publish_failed", json.dumps(finished, indent=2)
            kinds = [e["kind"] for e in events["items"]]
            for kind in (
                "publish_started",
                "publisher_finished",
                "branch_pushed",
                "publish_completed",
            ):
                assert kind in kinds, kind
            # The branch is on the remote at the accepted head, and the PR carries it.
            assert _branch_head(bare, branch) == head
            trailers = subprocess.run(
                [
                    "git",
                    "-c",
                    "safe.directory=*",
                    "--git-dir",
                    str(bare),
                    "log",
                    "--format=%(trailers:key=Crucible-Task,valueonly)",
                    f"main..{head}",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()
            assert trailers == ["E2E-FDY-0133"], trailers
            [pull] = github.state.repositories[full_name].pulls.values()
            assert (pull.head_branch, pull.head_sha, pull.base_ref) == (branch, head, "main")
            attempt_id = str(client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"])
            print(
                f"fdy-0133: {branch} pushed to {url} at {head} in "
                f"{time.monotonic() - started:.1f}s; PR #{pull.number}; task {state}; "
                f"Crucible-Task trailers on the pushed commits: {trailers}"
            )

        # What the provider sent: one Secret carrying a token the App minted, one Job
        # that reads it only from a Secret volume, one policy selecting that Job's Pod.
        made = [
            (kind, body)
            for _, kind, body in client_api.made
            if body["metadata"]["labels"].get(k8sspec.LABEL_ROLE) == k8sspec.ROLE_PUBLISHER
        ]
        secrets = [body for kind, body in made if kind == "secrets"]
        jobs = [body for kind, body in made if kind == "jobs"]
        policies = [body for kind, body in made if kind == "networkpolicies"]
        assert [s["metadata"]["name"] for s in secrets] == [f"publish-token-{attempt_id.lower()}"]
        value = base64.b64decode(secrets[0]["data"]["token"]).decode()
        assert value in github.state.tokens
        assert len(jobs) == 1 and len(policies) == 1
        for body in (*jobs, *policies):
            assert value not in json.dumps(body)
        pod_spec = jobs[0]["spec"]["template"]["spec"]
        assert {v["name"]: v.get("secret", {}).get("secretName") for v in pod_spec["volumes"]}[
            "publish-token"
        ] == secrets[0]["metadata"]["name"]
        assert pod_spec["hostAliases"] == [
            {"ip": host, "hostnames": ["api.github.com", "github.com"]}
        ]
        assert _selects(policies[0], jobs[0]["spec"]["template"]["metadata"]["labels"])
        assert _permits(policies[0], host)
        # And nothing of the push is left in the namespace.
        selector = (
            f"{k8sspec.LABEL_ATTEMPT}={attempt_id},{k8sspec.LABEL_ROLE}={k8sspec.ROLE_PUBLISHER}"
        )
        leftovers = [
            f"{kind}/{item['metadata']['name']}"
            for kind in ("jobs", "pods", "secrets", "networkpolicies")
            for item in client_api.list_objects(kind, label_selector=selector)
        ]
        assert leftovers == [], leftovers
        with pytest.raises(KubernetesApiError) as gone:
            client_api.get("secrets", secrets[0]["metadata"]["name"])
        assert gone.value.status == 404
        # hades #425: the worker's egress probe speaks TLS to the stubbed github.com, which
        # is this plain-HTTP push host, so the log holds a raw ClientHello beside the lines.
        log = (bare.parent / "push-host.log").read_bytes().decode("utf-8", errors="replace")
        assert f"POST /git/{name}.git/git-receive-pack 200 OK" in log, log
        await supervisor.stop()


# ----- the lab keeps running (lab findings of 2026-09-29) --------------------


class OutageKubernetesClient(RecordingKubernetesClient):
    """The real API, except that the next `outages` creates of a role's Job go to an
    API server address that refuses the connection: the real transport failure a
    restarting API server produces, through the real `_request` path."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.outages: dict[str, int] = {}
        self.refused: list[str] = []

    def create(self, kind: str, body: Any, **kwargs: Any) -> Any:
        role = str(((body.get("metadata") or {}).get("labels") or {}).get(k8sspec.LABEL_ROLE, ""))
        if kind == "jobs" and self.outages.get(role, 0) > 0:
            self.outages[role] -= 1
            self.refused.append(role)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                closed = int(probe.getsockname()[1])
            down = KubernetesClient(
                replace(self.access, server=f"https://127.0.0.1:{closed}"), self.namespace
            )
            return down.create(kind, body)
        return super().create(kind, body, **kwargs)


def _claim_exists(api: KubernetesClient, attempt_id: str) -> bool:
    try:
        row = api.get("persistentvolumeclaims", k8sspec.object_name("ws", attempt_id))
    except KubernetesApiError as exc:
        if exc.status == 404:
            return False
        raise
    return not (row.get("metadata") or {}).get("deletionTimestamp")


async def test_lab_findings_a_long_collect_blocks_no_launch_and_the_sweep_frees_claims(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    api: KubernetesClient,
    registry: CraneRegistryClient,
) -> None:
    """A collection whose verifier runs 25 seconds, past the 15 second lease: every tick
    still renews the lease and another task launches beside it. Then the claims: both
    attempts keep theirs (keep_diff_only) while their tasks wait on review; cancelling
    one task frees its claim on the next tick, and the other task's claim, which a
    publication still needs, stays."""
    provider = _provider(api, registry)
    ctx, tokens, app, harnesses = _kind_app(engine, migrated, artifact_root, provider, registry)
    supervisor = Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        ctx.clock,
        holder="e2e-kind-lab",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=15,
        grace_seconds=5,
        harnesses=harnesses,
    )
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as client:
        name = "lab-slow-collect"
        register(ctx, name, _origin(name))
        document = e2e_contract(
            f"E2E-{name.upper()}", name, os.environ["CRUCIBLE_E2E_KIND_REGISTRY"]
        )
        document["execution_request"]["provider"] = "kubernetes"
        document["policy"]["version"] = 21
        document["required_verification"].append(
            {"id": "V5", "command": "sleep 25", "expect_exit": 0}
        )
        # hades #250: a task whose checks all pass on the base is blocked before launch.
        document["required_verification"].append(
            {"id": "V9", "command": "test -f src/e2e_change.txt", "expect_exit": 0}
        )
        slow = submit_and_start(client, document)
        deadline = time.monotonic() + LAUNCH_DEADLINE_SECONDS + 120
        while time.monotonic() < deadline and not supervisor._collects:
            await supervisor.tick()
            await asyncio.sleep(0.5)
        assert supervisor._collects, "the slow task never reached its collection"
        slow_attempt = str(client.get(f"/v1/tasks/{slow}").json()["latest_attempt"]["id"])

        other = _kind_task(client, ctx, "lab-other", _origin("lab-other"))
        collect_started = time.monotonic()
        longest_tick = 0.0
        other_launched_during_collect = False
        while slow_attempt in supervisor._collects and time.monotonic() - collect_started < 150:
            before = time.monotonic()
            result = await supervisor.tick()
            longest_tick = max(longest_tick, time.monotonic() - before)
            assert result.held, "the lease was lost while a collection ran"
            assert client.get("/v1/supervisor").json()["healthy"] is True
            other_attempt = client.get(f"/v1/tasks/{other}").json().get("latest_attempt")
            started = bool(other_attempt and other_attempt["started_at"])
            if started and slow_attempt in supervisor._collects:
                other_launched_during_collect = True
            await asyncio.sleep(1)
        collect_seconds = time.monotonic() - collect_started
        print(
            f"lab findings: collection ran {collect_seconds:.0f}s, longest tick "
            f"{longest_tick:.1f}s, other task launched during it: "
            f"{other_launched_during_collect}"
        )
        assert collect_seconds > 15, "the collection was not longer than the lease"
        assert longest_tick < 15
        assert other_launched_during_collect

        review = {"accepted", "pre_pr_gates_failed"}
        for task in (slow, other):
            state = await run_until(supervisor, client, task, review, max_ticks=120)
            assert state == "accepted", gate_results(client, task)
        other_attempt_id = str(client.get(f"/v1/tasks/{other}").json()["latest_attempt"]["id"])
        with engine.begin() as connection:
            cleaned = connection.execute(
                text(
                    "SELECT count(*) FROM attempts "
                    "WHERE id IN (:a, :b) AND cleaned_up_at IS NOT NULL"
                ),
                {"a": slow_attempt, "b": other_attempt_id},
            ).scalar()
        assert cleaned == 2
        assert _claim_exists(api, slow_attempt) and _claim_exists(api, other_attempt_id)

        response = client.post(
            f"/v1/tasks/{slow}/cancel",
            json={"reason": "lab findings proof", "verbatim": "cancel it", "decided_by": "tests"},
        )
        assert response.status_code == 200, response.text
        freed_by = time.monotonic() + 60
        while time.monotonic() < freed_by and _claim_exists(api, slow_attempt):
            await supervisor.tick()
            await asyncio.sleep(1)
        assert not _claim_exists(api, slow_attempt), "the cancelled task's claim stayed"
        assert _claim_exists(api, other_attempt_id), "a claim a task still needs was removed"
        assert "retention_applied" in event_kinds(client, slow)
        print(
            f"lab findings: {k8sspec.object_name('ws', slow_attempt)} freed after the cancel; "
            f"{k8sspec.object_name('ws', other_attempt_id)} kept for the open task"
        )
        client.post(
            f"/v1/tasks/{other}/cancel",
            json={"reason": "lab findings proof done", "verbatim": "stop", "decided_by": "tests"},
        )
        for _ in range(20):
            await supervisor.tick()
            if not _claim_exists(api, other_attempt_id):
                break
            await asyncio.sleep(1)
        assert not _claim_exists(api, other_attempt_id)
    await supervisor.stop()


async def test_lab_findings_an_api_server_outage_during_collect_loses_no_attempt(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    registry: CraneRegistryClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collector Job's create meets a refused connection twice (the real transport
    error, through the real client). The attempt is not failed as environment: it is
    collected again from the untouched claim and the task reaches review with every
    gate passing."""
    # The retry interval only paces the tier; the rule is the same at 30 seconds.
    monkeypatch.setattr(supervisor_module, "COLLECT_RETRY_INTERVAL_SECONDS", 5)
    outage_api = OutageKubernetesClient(
        kubeconfig_access(os.environ["CRUCIBLE_E2E_KIND_KUBECONFIG"]),
        "hades-workers",
        timeout=15,
    )
    provider = _provider(outage_api, registry)
    ctx, tokens, app, harnesses = _kind_app(engine, migrated, artifact_root, provider, registry)
    supervisor = Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        ctx.clock,
        holder="e2e-kind-outage",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=15,
        grace_seconds=5,
        harnesses=harnesses,
    )
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as client:
        outage_api.outages[k8sspec.ROLE_COLLECTOR] = 2
        task_id = _kind_task(client, ctx, "lab-outage", _origin("lab-outage"))
        state = await run_until(
            supervisor,
            client,
            task_id,
            {"accepted", "pre_pr_gates_failed"},
            max_ticks=240,
            min_seconds=300,
        )
        attempt = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]
        print(
            f"lab findings: collector creates refused {len(outage_api.refused)} time(s); "
            f"task {state}, attempt exit class {attempt['exit_class']}"
        )
        assert outage_api.refused == [k8sspec.ROLE_COLLECTOR, k8sspec.ROLE_COLLECTOR]
        assert state == "accepted", gate_results(client, task_id)
        assert attempt["exit_class"] == "completed"
        executions = client.get(f"/v1/tasks/{task_id}").json()["executions"]
        assert sum(len(e["attempts"]) for e in executions) == 1
        response = client.post(
            f"/v1/tasks/{task_id}/cancel",
            json={"reason": "lab findings proof done", "verbatim": "stop", "decided_by": "tests"},
        )
        assert response.status_code == 200
        await run_until(supervisor, client, task_id, {"cancelled"}, max_ticks=20, min_seconds=0)
    await supervisor.stop()


async def test_fdy_0140_a_silent_worker_is_not_stalled_and_uncommitted_edits_are_collected(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    provider: KubernetesProvider,
    registry: CraneRegistryClient,
) -> None:
    """FDY-0140 on a real cluster, with the stall limits at 2 s and 6 s. One worker
    writes nothing to its log for 20 s while a file in its home keeps changing, as Hermes
    `-z` does: the provider reads that off the Pod and the worker is not stalled. The
    other edits its checkout and never commits: the collector commits the edit as the
    policy's author with the attempt trailer, and it is in the collected branch."""
    ctx, tokens, app, harnesses = _kind_app(
        engine, migrated, artifact_root, provider, registry, version=40, stall_seconds=(2, 6)
    )
    headers = {"Authorization": f"Bearer {tokens['operator']}"}
    done = {"accepted", "pre_pr_gates_failed"}
    with TestClient(app, headers=headers) as client:
        supervisor = Supervisor(
            ctx.uow_factory,
            {"kubernetes": provider},
            ctx.clock,
            holder="e2e-kind-fdy-0140",
            artifact_store=ctx.artifact_store,
            lease_ttl_seconds=120,
            grace_seconds=5,
            harnesses=harnesses,
        )
        # One at a time, so another task's collection never holds the tick while the
        # silent worker needs observing.
        silent = _kind_task(
            client,
            ctx,
            "fdy-0140-silent",
            _origin("fdy-0140-silent", "silent-work", extra={"e2e-silent-seconds": "20\n"}),
            version=40,
        )
        await run_until(supervisor, client, silent, done, max_ticks=120, pause=0.5)
        uncommitted = _kind_task(
            client,
            ctx,
            "fdy-0140-uncommitted",
            _origin("fdy-0140-uncommitted", "no-commit"),
            version=40,
        )
        await run_until(supervisor, client, uncommitted, done, max_ticks=120, pause=0.5)
        await supervisor.stop()

        silent_id = client.get(f"/v1/tasks/{silent}").json()["latest_attempt"]["id"]
        silent_attempt = client.get(f"/v1/attempts/{silent_id}").json()
        assert silent_attempt["termination_reason"] is None, silent_attempt
        assert silent_attempt["exit_class"] == "completed", silent_attempt
        assert "worker_stalled" not in event_kinds(client, silent)
        with ctx.uow_factory() as uow:
            signals = [h.signal for h in uow.heartbeats.list_for_attempt(silent_id, limit=10_000)]
            stamps = [c.ts for c in uow.logs.list_for_attempt(silent_id, limit=500)]
        # The log itself was silent for longer than the 6 s fail limit, and what kept the
        # worker alive meanwhile was its files changing.
        gaps = [(later - earlier).total_seconds() for earlier, later in pairwise(stamps)]
        assert max(gaps) >= 15, gaps
        assert signals.count("fs_changed") >= 3, signals

        attempt = client.get(f"/v1/tasks/{uncommitted}").json()["latest_attempt"]
        assert attempt["exit_class"] == "completed", attempt
        evidence = client.get(f"/v1/attempts/{attempt['id']}/evidence").json()["items"]
        bundle = next(item["payload"] for item in evidence if item["kind"] == "bundle_head")
        assert bundle["commits"] == 1 and bundle["bundle_verified"] is True, bundle
        assert any("left uncommitted" in message for message in bundle["commit_messages"]), bundle
        assert "src/e2e_change.txt" in bundle["commit_paths"], bundle
        events = client.get(f"/v1/tasks/{uncommitted}/events", params={"limit": 200}).json()
        collected = [e for e in events["items"] if e["kind"] == "attempt_collected"]
        assert collected[-1]["payload"].get("uncommitted_work_committed") is True, collected


@pytest.mark.parametrize("command,blocked", [("true", True), ("test -f made-by-the-worker", False)])
async def test_gate_probe_on_unchanged_base_before_worker(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    registry: CraneRegistryClient,
    command: str,
    blocked: bool,
) -> None:
    """AC1/AC3: real probe logs and Job ordering, through the script harness API."""
    api = _recording_client()
    provider = _provider(api, registry)
    ctx, tokens, app, harnesses = _kind_app(engine, migrated, artifact_root, provider, registry)
    supervisor = Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        ctx.clock,
        holder="e2e-gate-probe",
        artifact_store=ctx.artifact_store,
        harnesses=harnesses,
    )
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as client:
        name = "gate-probe-true" if blocked else "gate-probe-file"
        register(ctx, name, _origin(name, "hang"))
        document = e2e_contract(
            f"E2E-{name.upper()}", name, os.environ["CRUCIBLE_E2E_KIND_REGISTRY"]
        )
        document["execution_request"]["provider"] = "kubernetes"
        document["policy"]["version"] = 21
        document["required_verification"].append({"id": "V5", "command": command, "expect_exit": 0})
        task_id = submit_and_start(client, document)
        try:
            deadline = time.monotonic() + LAUNCH_DEADLINE_SECONDS
            while time.monotonic() < deadline:
                await supervisor.tick()
                view = client.get(f"/v1/tasks/{task_id}").json()
                attempt = view["latest_attempt"]
                if view["state"] == "blocked" or (attempt and attempt["started_at"]):
                    break
                await asyncio.sleep(0.25)
            else:
                pytest.fail(f"gate probe did not settle: {view}")
            attempt_id = attempt["id"]
            probes = view["gate_probes"]
            print("gate probes:", json.dumps(probes, sort_keys=True))
            evidence = client.get(f"/v1/attempts/{attempt_id}/evidence").json()["items"]
            print(
                "gate probe evidence:",
                json.dumps(
                    [row for row in evidence if row["kind"] == "gate_probe"], sort_keys=True
                ),
            )
            assert len(probes) == 1 and probes[0]["id"] == "V5"
            assert probes[0]["exit"] == (0 if blocked else 1)
            assert any(
                row["kind"] == "gate_probe" and row["payload"]["exit"] == probes[0]["exit"]
                for row in evidence
            )
            roles = [
                body["metadata"]["labels"][k8sspec.LABEL_ROLE] for _, body in _jobs(api, attempt_id)
            ]
            assert roles[0] == "gate-probe"
            if blocked:
                assert view["state"] == "blocked" and attempt["started_at"] is None
                assert roles == ["gate-probe"]
                assert "V5 passes on the unchanged repo" in view["open_escalations"][0]["question"]
                with ctx.uow_factory() as uow:
                    row = uow.attempts.get(attempt_id)
                    assert row is not None and row.termination_reason == "gate_proves_nothing"
                assert view["unacked_wakes"] > 0
            else:
                assert "preparer" in roles and "worker" in roles
                assert roles.index("gate-probe") < roles.index("preparer") < roles.index("worker")
            assert not api.list_objects(
                "jobs",
                label_selector=f"{k8sspec.LABEL_ATTEMPT}={attempt_id},{k8sspec.LABEL_ROLE}=gate-probe",
            )
        finally:
            client.post(
                f"/v1/tasks/{task_id}/cancel",
                json={
                    "reason": "gate probe proof complete",
                    "verbatim": "stop",
                    "decided_by": "tests",
                },
            )
            await run_until(supervisor, client, task_id, {"cancelled"})
            await supervisor.stop()


class _RoboScriptAdapter(ScriptHarnessAdapter):
    """A script harness with a ro credential and a settings template (FDY-0223 / #349)."""

    name = "script-harness"

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            prompt_on_stdin=False,
            model_flag=False,
            effort_flag=False,
            transcript_format=TranscriptFormat.NONE,
            endpoints=(),
            shim=None,
        )

    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=self.name,
            mount_target="/home/worker/.script-harness",
            auth_files=(AuthFile("auth.json", json=True),),
            minimum_mode=MountMode.RO,
            templates={"settings.json": '{"ro": true}\n'},
        )


async def test_fdy_0223_ro_credential_with_template_starts_on_kubernetes(
    provider: KubernetesProvider,
    api: KubernetesClient,
    registry: CraneRegistryClient,
) -> None:
    """349 / FDY-0223: a templated harness in `ro` mode reaches the worker process.

    The credential directory is a projected read-only volume (Secret items + identity
    ConfigMap template); no mount targets a path under another mount's target. The
    worker starts without an OCI runtime error."""
    harnesses = HarnessRegistry((_RoboScriptAdapter(),))
    provider_ro = _provider(api, registry, harnesses=harnesses)

    # Seed the harness Secret.
    try:
        api.create(
            "secrets",
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "hades-harness-script-harness",
                    "namespace": "hades-workers",
                },
                "type": "Opaque",
                "stringData": {"auth.json": json.dumps({"ro": True})},
            },
        )
    except KubernetesApiError as exc:
        if exc.status != 409:
            raise

    spec = _spec(99, _origin("ro-script"), harness="script-harness")
    workspace = await provider_ro.prepare(spec)
    handle = await provider_ro.launch(workspace, spec)

    # Verify the worker Pod shape: cred volume is projected, not a plain Secret.
    jobs = api.list_objects(
        "jobs",
        label_selector=(f"{k8sspec.LABEL_ATTEMPT}={spec.attempt_id},{k8sspec.LABEL_ROLE}=worker"),
    )
    pod_spec = jobs[0]["spec"]["template"]["spec"]
    cred_volume = next(v for v in pod_spec["volumes"] if v["name"] == "cred")
    assert "projected" in cred_volume
    assert cred_volume["projected"]["sources"]
    # No subPath mount under the credential directory.
    mounts_list = pod_spec["containers"][0]["volumeMounts"]
    cred_path = "/home/worker/.script-harness"
    for m in mounts_list:
        if m["name"] == "cred":
            assert m["mountPath"] == cred_path
            assert m["readOnly"] is True
        assert not m["mountPath"].startswith(cred_path + "/") or m["name"] != "identity"

    # The worker should start and reach the terminal state.
    observed = await _terminal(provider_ro, handle)
    assert observed.state is ObservationState.EXITED
    assert observed.exit_code == 0

    await provider_ro.cleanup(workspace, CleanupPolicy.DELETE, spec)
