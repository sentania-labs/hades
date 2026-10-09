"""The Kubernetes provider against the fake API (08, 26).

No cluster here: the fake answers the API server so the refusals, the state mapping and
the credential paths are exercised deterministically. The real cluster is C8b's
`make e2e-kind` tier.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution import kubernetes as kubernetes_module
from crucible.adapters.execution.k8sapi import (
    ExecResult,
    KubernetesApiError,
    KubernetesUnavailableError,
    LogFrame,
)
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.execution.kubernetes import (
    CollectionFailedError,
    CollectionUnavailableError,
    HarnessRefusedError,
    KubernetesConfig,
    KubernetesProvider,
    NamespaceProbe,
)
from crucible.ports.execution import (
    CleanupPolicy,
    ExecutionProvider,
    Handle,
    LaunchCancelledError,
    LaunchRefusedError,
    LogOffset,
    ObservationState,
    ProviderError,
)
from crucible.ports.execution import (
    CleanupPolicy as Cleanup,
)
from tests.unit.kubernetes_fixtures import HOST_ADDRESSES, IMAGE, build, pod_of, spec

CODEX_IMAGE = "crucible-worker:codex-fake-succeed-2"


def _auth(last_refresh: str) -> bytes:
    return json.dumps(
        {"tokens": {"access_token": "not-a-real-value"}, "last_refresh": last_refresh}
    ).encode()


@pytest.mark.asyncio
async def test_dead_codex_credential_refuses_a_new_kubernetes_launch() -> None:
    api, registry, provider = build(harness="codex")
    registry.register(CODEX_IMAGE, harness="codex", version="0.153.4")
    provider.set_credential_dead_check(lambda: True)

    with pytest.raises(HarnessRefusedError, match="credential is dead"):
        await provider.prepare(spec(harness="codex", image=CODEX_IMAGE))

    assert api.created == []


async def run_to_exit(provider: KubernetesProvider, handle: Handle) -> Any:
    observation = await provider.observe(handle)
    while observation.state is ObservationState.RUNNING:
        observation = await provider.observe(handle)
    return observation


async def prepared(**kwargs: Any) -> Any:
    api, registry, provider = build(**kwargs.pop("build", {}))
    launch = spec(**kwargs)
    workspace = await provider.prepare(launch)
    return api, registry, provider, launch, workspace


async def refused_at_prepare(**kwargs: Any) -> Any:
    """Issue 59: a namespace the probe has not passed gets no object of the attempt,
    no preparer Pod and no copy of its credential, not only no worker."""
    api, _registry, provider = build(**kwargs.pop("build", {}))
    with pytest.raises(LaunchRefusedError, match="not ready"):
        await provider.prepare(spec(**kwargs))
    for kind in ("persistentvolumeclaims", "configmaps", "secrets", "jobs"):
        assert not [row for row in api.created if row["kind"] == kind], (
            f"prepare created {kind} before the readiness gate"
        )
    return api, provider


# ----- the port ------------------------------------------------------------


def test_the_provider_satisfies_the_execution_port() -> None:
    _api, _registry, provider = build()
    checked: ExecutionProvider = provider
    assert checked.name == "kubernetes"


def test_capabilities_are_26s() -> None:
    _api, _registry, provider = build()
    capabilities = provider.capabilities().as_dict()
    assert capabilities["isolation"] == "pod"
    assert capabilities["network_control"] is True
    assert capabilities["resource_limits"] is True
    # 26: nothing of a workspace is ever visible to the Crucible process.
    assert capabilities["shared_disk"] is False
    assert capabilities["max_concurrency"] == 3


async def test_max_concurrency_comes_from_the_namespace_resource_quota() -> None:
    """Seven Pods admitted, one kept for Hades's own short-role Pods (hades #423): six
    workers at once."""
    api, _registry, provider = build()
    api.create("resourcequotas", {"metadata": {"name": "workers"}, "spec": {"hard": {"pods": "7"}}})
    await provider.health()
    assert provider.capabilities().max_concurrency == 6


async def test_a_job_quota_reports_attempt_capacity_not_raw_jobs() -> None:
    """Fifteen Jobs: one kept for a short-role Pod's Job, fourteen left is two attempts of
    five Jobs each, not fifteen workers."""
    api, _registry, provider = build()
    api.create(
        "resourcequotas",
        {"metadata": {"name": "workers"}, "spec": {"hard": {"count/jobs.batch": "15"}}},
    )
    await provider.health()
    assert provider.capabilities().max_concurrency == 2


# The new base quota (5 * 3 + 2 = 17) also reports three attempts: sixteen Jobs after
# the reserved one still hold three attempts' five.
async def test_the_base_quota_reports_three_attempts() -> None:
    api, _registry, provider = build()
    api.create(
        "resourcequotas",
        {"metadata": {"name": "workers"}, "spec": {"hard": {"count/jobs.batch": "17"}}},
    )
    await provider.health()
    assert provider.capabilities().max_concurrency == 3


# ----- the namespace readiness probe (26) ----------------------------------


async def test_the_probe_passes_when_the_canary_cannot_reach_the_api_server() -> None:
    _api, _registry, provider = build()
    await provider.prepare(spec())
    probe = await provider.ensure_ready()
    assert probe == NamespaceProbe(
        True,
        True,
        4096,
        "namespace ready",
        dns_resolves=True,
        pid_limit_source="cgroup-v2-parent",
        canary_node="lab-node-1",
    )
    health = await provider.health()
    assert health.state == "ok"
    assert health.checks["egress_enforced"] is True
    assert health.checks["pod_pid_limit"] == 4096
    assert health.checks["pod_pid_limit_source"] == "cgroup-v2-parent"
    assert health.checks["runtime_class"] == "standard"


async def test_the_canary_requests_a_small_fixed_size_not_the_role_pods_size() -> None:
    """Issue 93: the canary is a shell script with curl, not a role pod, so it must
    never inherit the policy's 2-CPU / 4Gi default limits."""
    api, _registry, provider = build(
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            storage_class="lab-ssd",
            image_pull_secret="ghcr-pull",
            canary_cpu_millicores=50,
            canary_memory="32Mi",
        )
    )
    await provider.prepare(spec())
    assert (await provider.ensure_ready()).passed
    pod = next(
        row["body"]["spec"]
        for row in api.created
        if str(row["name"]).startswith("crucible-canary-")
    )
    resources = pod["containers"][0]["resources"]
    assert resources["limits"]["cpu"] == "50m"
    assert resources["limits"]["memory"] == str(32 * 1024**2)
    assert resources["requests"] == {"cpu": "50m", "memory": str(32 * 1024**2)}


async def test_the_probe_requests_log_lines_without_timestamp_prefixes() -> None:
    api, _registry, provider = build()
    await provider._resolve_image(spec())
    real = api.pod_log
    requested: list[bool] = []

    def pod_log(*args: Any, **kwargs: Any) -> Any:
        requested.append(bool(kwargs.get("timestamps", True)))
        return real(*args, **kwargs)

    api.pod_log = pod_log  # type: ignore[method-assign]
    assert (await provider.ensure_ready()).passed
    # Two canaries: the namespace's own rules, then a worker's.
    assert requested == [False, False]


async def test_concurrent_readiness_checks_share_one_canary() -> None:
    _api, _registry, provider = build()
    await provider._resolve_image(spec())
    real = provider._run_probe
    calls = 0

    async def delayed_probe() -> NamespaceProbe:
        nonlocal calls
        calls += 1
        await asyncio.to_thread(threading.Event().wait, 0.01)
        return await real()

    provider._run_probe = delayed_probe  # type: ignore[method-assign]
    first, second = await asyncio.gather(provider.ensure_ready(), provider.ensure_ready())
    assert first.passed and second.passed
    assert calls == 1


async def test_a_namespace_whose_cni_does_not_enforce_egress_refuses_every_launch() -> None:
    _api, provider = await refused_at_prepare(build={"egress_enforced": False})
    probe = await provider.ensure_ready()
    assert probe.passed is False and probe.egress_enforced is False
    assert (await provider.health()).state == "degraded"


async def test_a_node_with_no_pod_pid_limit_refuses_every_launch() -> None:
    _api, provider = await refused_at_prepare(build={"pod_pid_limit": None})
    probe = await provider.ensure_ready()
    assert probe.passed is False and probe.pid_limit is None
    assert "podPidsLimit is not set" in probe.detail
    assert probe.pid_limit_source == "cgroup-v2-parent"


async def test_a_private_cgroup_namespace_reports_inconclusive_not_a_pass() -> None:
    """The container's own cgroup limit (95's bug) must never stand in for the pod-level
    one: when the runtime hides the parent cgroup, the gate says so and still refuses,
    even though a container-scope number is sitting right there in `pids.max`."""
    _api, provider = await refused_at_prepare(build={"pod_pid_limit_source": "cgroupns-private"})
    probe = await provider.ensure_ready()
    assert probe.passed is False
    assert probe.pid_limit is None
    assert probe.pid_limit_source == "cgroupns-private"
    assert "cgroup namespace isolation" in probe.detail


async def test_an_operator_declared_limit_covers_a_private_cgroup_namespace() -> None:
    """95's follow-up: a cluster whose runtime hides the pod cgroup (the common case)
    would otherwise never launch anything. lab-admin's explicit, out-of-band
    attestation is the one way past that, and it is clearly not the same thing as the
    canary confirming the number itself."""
    config = KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        pod_pid_limit_override=512,
    )
    _api, _registry, provider, launch, workspace = await prepared(
        build={"config": config, "pod_pid_limit_source": "cgroupns-private"}
    )
    probe = await provider.ensure_ready()
    assert probe.passed is True
    assert probe.pid_limit == 512
    assert probe.pid_limit_source == "operator-declared"
    await provider.launch(workspace, launch)


async def test_an_operator_declared_limit_never_overrides_a_confirmed_absence() -> None:
    """The override fills a gap the canary could not see into; it never contradicts an
    answer the canary actually read, since that answer is more current than a
    declaration lab-admin made once at deploy time."""
    config = KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        pod_pid_limit_override=512,
    )
    _api, provider = await refused_at_prepare(build={"config": config, "pod_pid_limit": None})
    probe = await provider.ensure_ready()
    assert probe.passed is False
    assert probe.pid_limit is None
    assert probe.pid_limit_source == "cgroup-v2-parent"


async def test_a_zero_pod_pid_limit_from_the_canary_is_not_a_limit() -> None:
    """0 is not a value `podPidsLimit` takes; a canary reporting it must not be read as
    a confirmed limit (95's Codex correction)."""
    _api, provider = await refused_at_prepare(build={"pod_pid_limit": 0})
    probe = await provider.ensure_ready()
    assert probe.passed is False and probe.pid_limit is None
    assert "podPidsLimit is not set" in probe.detail


async def test_a_non_positive_override_never_passes_the_gate() -> None:
    """The settings model refuses a non-positive override before it reaches here
    (95's Codex correction), but the gate itself never trusts one either: defense in
    depth for any caller that builds `KubernetesConfig` directly."""
    config = KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        pod_pid_limit_override=-1,
    )
    _api, provider = await refused_at_prepare(
        build={"config": config, "pod_pid_limit_source": "cgroupns-private"}
    )
    probe = await provider.ensure_ready()
    assert probe.passed is False and probe.pid_limit is None


async def test_cgroup_v1_pod_pid_limit_is_unsupported_not_a_pass() -> None:
    _api, provider = await refused_at_prepare(build={"pod_pid_limit_source": "cgroup-v1"})
    probe = await provider.ensure_ready()
    assert probe.passed is False
    assert probe.pid_limit is None
    assert probe.pid_limit_source == "cgroup-v1"
    assert "unsupported" in probe.detail


# ----- images (07, 13) -----------------------------------------------------


async def test_an_image_outside_the_policy_allowlist_is_refused() -> None:
    _api, _registry, provider = build()
    launch = spec(policy={"images": {"allowlist": ["ghcr.io/someone-else/worker:*"]}})
    with pytest.raises(ProviderError, match="outside the policy allowlist"):
        await provider.prepare(launch)


async def test_an_image_whose_harness_label_differs_is_refused() -> None:
    _api, _registry, provider = build(harness="codex")
    launch = spec()
    with pytest.raises(LaunchRefusedError, match="declares harness"):
        await provider.prepare(launch)


async def test_an_image_outside_the_tested_range_is_refused() -> None:
    _api, _registry, provider = build(version="9.9.9")
    launch = spec()
    with pytest.raises(LaunchRefusedError, match="outside the tested range"):
        await provider.prepare(launch)


async def test_the_resolved_digest_is_the_handle_and_the_recorded_image() -> None:
    _api, registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    assert handle.ref == "worker-01attempt0000000000000000a"
    assert handle.image_digest == registry.resolve(IMAGE).reference
    assert "@sha256:" in handle.image_digest


async def test_a_cancel_before_the_worker_job_is_created_creates_nothing() -> None:
    """hades #189: the launch's last look comes after the readiness gate and the image
    resolution it awaits; a cancel there creates no worker Job and no worker policy."""
    api, _registry, provider, launch, workspace = await prepared()

    async def cancelled() -> bool:
        return True

    before = len(api.created)
    with pytest.raises(LaunchCancelledError, match="before the worker was created"):
        await provider.launch(workspace, launch, cancelled)
    assert api.created[before:] == []
    # The same launch with no cancel creates the worker's policy and Job.
    await provider.launch(workspace, launch)
    assert {row["kind"] for row in api.created[before:]} >= {"jobs", "networkpolicies"}


# ----- observe (26) --------------------------------------------------------


async def test_a_running_pod_is_running_and_a_terminated_one_is_its_exit_code() -> None:
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    assert (await provider.observe(handle)).state is ObservationState.RUNNING
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED and observation.exit_code == 0


async def test_an_oom_kill_is_flagged_rather_than_parsed_out_of_the_detail() -> None:
    api, _registry, provider, launch, workspace = await prepared(image=IMAGE)
    api.script(launch.attempt_id, "oom", after=1)
    handle = await provider.launch(workspace, launch)
    observation = await run_to_exit(provider, handle)
    assert observation.exit_code == 137 and observation.oom_killed is True


async def test_a_pod_that_is_gone_with_nothing_crucible_did_is_lost() -> None:
    """26: a Pod that existed and disappeared is lost, not exited. The observer must
    have actually seen it first, or there is nothing to tell it apart from a Job whose
    Pod the controller has not created yet (below)."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    assert (await provider.observe(handle)).state is ObservationState.RUNNING
    api.remove_pod_out_of_band(launch.attempt_id)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.LOST


async def test_a_job_with_no_pod_yet_is_pending_not_lost() -> None:
    """103, 26: a Job controller that has not created a Pod yet (a busy node, a slow
    tick) reads as pending, not lost, until the launch timeout."""
    api, _registry, provider, launch, workspace = await prepared()
    api.no_pod_yet.add(launch.attempt_id)
    handle = await provider.launch(workspace, launch)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.RUNNING


async def test_a_job_with_no_pod_past_the_launch_timeout_is_a_launch_failure() -> None:
    """103, 26: past the launch timeout with no Pod, it is a launch failure naming the
    Job controller, the same class of outcome as a Pod stuck Pending."""
    api, _registry, provider, launch, workspace = await prepared()
    assert (await provider.ensure_ready()).passed
    api.no_pod_yet.add(launch.attempt_id)
    provider.config = replace(provider.config, launch_timeout_seconds=0)
    handle = await provider.launch(workspace, launch)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED and observation.exit_code == 70
    assert "the Job controller never created a Pod" in (observation.detail or "")


async def test_a_job_that_itself_disappeared_is_lost() -> None:
    """26: a Job that no longer exists at all is lost, even inside the launch window."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    api.delete("jobs", handle.ref)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.LOST
    assert "no such Job" in (observation.detail or "")


async def test_a_job_that_failed_before_any_pod_was_seen_is_lost() -> None:
    """103, 26: `backoffLimit: 0` fails the Job the moment its one Pod does, so a Pod
    gone before any poll caught it alive is still a Pod that existed and disappeared,
    not a Job that never got one, and the Job's own Failed condition says so."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    api.remove_pod_out_of_band(launch.attempt_id)
    api.objects[("jobs", handle.ref)].body["status"] = {
        "failed": 1,
        "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}],
    }
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.LOST


async def test_an_evicted_pod_is_lost() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    api.evict(launch.attempt_id)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.LOST
    assert "Evicted" in (observation.detail or "")


async def test_a_pod_crucible_drained_is_an_exit_and_never_a_loss() -> None:
    """16: a Pod that is gone because Crucible deleted it is a `killed` attempt. The
    only signal Kubernetes offers is a delete, so what Crucible did is remembered."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await provider.terminate(handle, "drain")
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED and observation.exit_code == 143
    deleted = [name for kind, name in api.deleted if kind == "pods"]
    assert deleted


async def test_a_worker_that_ignores_sigterm_is_killed_at_the_grace_period() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    api.script(launch.attempt_id, "hang", after=1)
    handle = await provider.launch(workspace, launch)
    await provider.terminate(handle, "drain")
    # The kubelet waited the grace period and the Pod is still there.
    assert (await provider.observe(handle)).state is ObservationState.RUNNING
    await provider.terminate(handle, "kill")
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED and observation.exit_code == 137


async def test_a_pod_pending_past_the_launch_timeout_is_a_launch_failure() -> None:
    """26: Pending longer than the launch timeout is a launch failure with the Pod's
    conditions as detail, not a stall. Reported as exit 70, which 16 classifies as
    `environment`, so the attempt retries and the tick keeps moving."""
    api, _registry, provider, launch, workspace = await prepared()
    assert (await provider.ensure_ready()).passed
    api.pending_forever.add(launch.attempt_id)
    provider.config = KubernetesConfig(
        poll_interval_seconds=0, launch_timeout_seconds=0, storage_class="lab-ssd"
    )
    handle = await provider.launch(workspace, launch)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED and observation.exit_code == 70
    assert "Unschedulable" in (observation.detail or "")


async def test_logs_resume_strictly_after_the_stored_position() -> None:
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    first = await provider.logs(handle, LogOffset())
    assert first and b"fake worker" in first[0].content
    resumed = await provider.logs(
        handle,
        LogOffset(
            index=first[0].lines,
            timestamp=first[0].ts.isoformat() if first[0].ts else None,
            line_sha256=first[0].line_sha256,
            occurrence=first[0].occurrence,
        ),
    )
    assert resumed == []


def _stamped(second: int, nanos: int, text: str) -> str:
    return f"2026-09-25T17:00:{second:02d}.{nanos:09d}Z {text}"


async def _drain_logs(provider: KubernetesProvider, handle: Handle) -> list[bytes]:
    """Poll as the supervisor does, carrying the resume position forward, until a poll
    brings nothing new; every line stored, in order."""
    offset = LogOffset()
    stored: list[bytes] = []
    for _ in range(50):
        chunks = await provider.logs(handle, offset)
        if not chunks:
            return stored
        for chunk in chunks:
            stored.extend(chunk.content.splitlines())
            offset = LogOffset(
                index=offset.index + chunk.lines,
                timestamp=chunk.ts.isoformat() if chunk.ts else None,
                line_sha256=chunk.line_sha256,
                occurrence=chunk.occurrence,
            )
    raise AssertionError("the log never stopped bringing new lines")


async def _worker_with_log(lines: list[str]) -> tuple[FakeKubernetesApi, Any, Handle]:
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    api.logs[f"{handle.ref}-abc12"] = lines
    return api, provider, handle


async def test_a_log_poll_asks_for_a_bounded_number_of_bytes() -> None:
    """Issue 63: every observation poll passes `limitBytes`, so no poll reads the whole
    log of a long-running worker into memory."""
    api, provider, handle = await _worker_with_log([_stamped(0, 0, "hello")])
    api.log_reads.clear()
    await provider.logs(handle, LogOffset())
    assert [r["limit_bytes"] for r in api.log_reads] == [kubernetes_module.LOG_READ_LIMIT]


async def test_a_capped_log_read_resumes_with_the_line_it_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue 63: a capped read can stop inside a line. Only whole lines are stored, and
    the strict-after resume of 10 carries on from the last of them, so the log arrives
    complete, in order and exactly once over several polls."""
    # About 180 bytes a second against a 400-byte read: each poll gets past its first
    # second and stops inside a later line.
    monkeypatch.setattr(kubernetes_module, "LOG_READ_LIMIT", 400)
    lines = [_stamped(s, n, f"line {s}.{n} " + "x" * 20) for s in range(6) for n in range(3)]
    api, provider, handle = await _worker_with_log(lines)
    api.log_reads.clear()
    stored = await _drain_logs(provider, handle)
    assert stored == [line.partition(" ")[2].encode() for line in lines]
    assert len(api.log_reads) > 2
    assert {r["limit_bytes"] for r in api.log_reads} == {400}


async def test_a_short_truncated_log_response_resumes_with_the_whole_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue 63 follow-up: the kubelet can land short of `limitBytes` and still cut a
    line in half. Detecting a capped read by the missing trailing newline, not by exact
    byte equality against the limit, still resumes with the whole line."""
    monkeypatch.setattr(kubernetes_module, "LOG_READ_LIMIT", 400)
    lines = [_stamped(s, n, f"line {s}.{n} " + "x" * 20) for s in range(6) for n in range(3)]
    api, provider, handle = await _worker_with_log(lines)
    real = api.pod_log

    def pod_log(*args: Any, **kwargs: Any) -> Any:
        call_kwargs = dict(kwargs)
        limit = call_kwargs.pop("limit_bytes", None)
        frames = real(*args, **call_kwargs, limit_bytes=None)
        payload = b"".join(f.payload for f in frames)
        if limit is not None and len(payload) > limit:
            # A real kubelet response can land a few bytes short of the exact
            # limit and still cut a line in half.
            payload = payload[: max(0, limit - 5)]
        return [LogFrame("stdout", payload)] if payload else []

    api.pod_log = pod_log  # type: ignore[method-assign]
    api.log_reads.clear()
    stored = await _drain_logs(provider, handle)
    assert stored == [line.partition(" ")[2].encode() for line in lines]
    assert len(api.log_reads) > 2


async def test_an_unfinished_line_well_under_the_limit_waits_for_the_next_poll() -> None:
    """Issue 63 follow-up: a worker partway through writing a line, with nothing else
    new and a response nowhere near `limitBytes`, is not a capped read. It is left for
    the next regular poll instead of being forced through a growth retry to the
    ceiling, which would wrongly report it as a skipped crowded second."""
    api, provider, handle = await _worker_with_log([])

    def pod_log(name: str, **kwargs: Any) -> Any:
        api.log_reads.append(
            {
                "name": name,
                "since_time": kwargs.get("since_time"),
                "limit_bytes": kwargs.get("limit_bytes"),
            }
        )
        payload = _stamped(0, 0, "still writing").encode()[:20]
        return [LogFrame("stdout", payload)] if payload else []

    api.pod_log = pod_log  # type: ignore[method-assign]
    api.log_reads.clear()
    chunks = await provider.logs(handle, LogOffset())
    assert chunks == []
    assert len(api.log_reads) == 1


async def test_a_second_fuller_than_one_read_is_read_again_larger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sinceTime` is one-second granular, so a read capped inside the first second it
    returns cannot move on by resuming. It is read again larger, up to the ceiling."""
    monkeypatch.setattr(kubernetes_module, "LOG_READ_LIMIT", 100)
    monkeypatch.setattr(kubernetes_module, "LOG_READ_CEILING", 1600)
    lines = [_stamped(0, n, f"burst {n} " + "y" * 30) for n in range(10)]
    lines.append(_stamped(1, 0, "after"))
    api, provider, handle = await _worker_with_log(lines)
    api.log_reads.clear()
    stored = await _drain_logs(provider, handle)
    assert stored == [line.partition(" ")[2].encode() for line in lines]
    assert max(r["limit_bytes"] for r in api.log_reads) == 1600


async def test_a_second_fuller_than_the_ceiling_is_skipped_with_a_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the ceiling nothing `sinceTime` can say gets through the second, so the
    resume moves to the next second and the log says what was skipped. The poll never
    stalls and never reads unbounded."""
    monkeypatch.setattr(kubernetes_module, "LOG_READ_LIMIT", 100)
    monkeypatch.setattr(kubernetes_module, "LOG_READ_CEILING", 400)
    lines = [_stamped(0, 0, "before")]
    lines += [_stamped(1, n + 1, f"flood {n} " + "z" * 60) for n in range(20)]
    lines += [_stamped(2, 5, "after the flood")]
    api, provider, handle = await _worker_with_log(lines)
    api.log_reads.clear()
    stored = await _drain_logs(provider, handle)
    assert stored[0] == b"before"
    assert stored[-1] == b"after the flood"
    notices = [line for line in stored if line.startswith(b"[crucible] log lines skipped")]
    assert len(notices) == 1
    assert all(r["limit_bytes"] <= 400 for r in api.log_reads)


async def test_the_collection_tail_read_asks_for_bounded_lines_and_bytes() -> None:
    """The collection path (FDY-0187) asks `pod_log` with `tailLines` only; the
    byte cap is applied client-side, so the tail still ends with the worker's
    last line.

    This covers the Codex P2 fix: asking the API for both `tailLines` and
    `limitBytes` can cut off the end, so we use `tailLines` only and bound
    bytes in-process."""
    last_line = "worker is done"
    lines = [_stamped(0, 0, "start")]
    lines += [_stamped(1, n, f"step {n}") for n in range(5)]
    lines.append(_stamped(2, 0, last_line))
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    api.log_reads.clear()
    api.logs[f"{handle.ref}-abc12"] = lines
    outputs = await provider.collect(handle, workspace, launch)
    tail_entries = [r for r in api.log_reads if r["tail_lines"] is not None]
    assert len(tail_entries) >= 1
    for entry in tail_entries:
        assert entry["tail_lines"] is not None
        assert entry["limit_bytes"] is None
    # The returned tail is at most LOG_READ_LIMIT bytes and still ends with
    # the worker's last line.
    assert len(outputs.stdout_tail) <= kubernetes_module.LOG_READ_LIMIT
    assert last_line in outputs.stdout_tail


async def test_the_collection_tail_preserves_the_last_line_when_2000_lines_exceed_the_limit() -> (
    None
):
    """When the last 2000 lines (JOB_TAIL_LINES) exceed ``LOG_READ_LIMIT``,
    the client-side bound still preserves the end of the output."""
    last_line = "final step complete"
    # Generate enough lines so the tail exceeds LOG_READ_LIMIT bytes.
    # Each stamped line is roughly 40-50 bytes.
    lines = [_stamped(0, 0, "start")]
    for s in range(1, 100):
        for n in range(50):
            lines.append(_stamped(s, n, f"line {s}.{n} " + "x" * 20))
    lines.append(_stamped(99, 49, last_line))
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    api.log_reads.clear()
    api.logs[f"{handle.ref}-abc12"] = lines
    outputs = await provider.collect(handle, workspace, launch)
    # The collected tail is bounded by LOG_READ_LIMIT bytes.
    assert len(outputs.stdout_tail) <= kubernetes_module.LOG_READ_LIMIT
    # The very last line survives the client-side bound.
    assert last_line in outputs.stdout_tail


# ----- reconcile (10, 26) --------------------------------------------------


async def test_reconcile_adopts_a_live_worker_job_by_label() -> None:
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    adopted = await provider.reconcile()
    assert [(h.attempt_id, h.ref) for h in adopted] == [(launch.attempt_id, handle.ref)]
    assert adopted[0].image_digest == handle.image_digest


async def test_reconcile_recovers_the_image_after_provider_restart() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    api.script(launch.attempt_id, "hang")
    handle = await provider.launch(workspace, launch)
    provider._launched.clear()
    adopted = await provider.reconcile()
    assert adopted[0].image_digest == handle.image_digest
    assert provider._launched[launch.attempt_id].image_digest == handle.image_digest


async def test_collection_waits_for_the_reader_pod_to_finish_deleting() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    real_delete = api.delete
    real_get = api.get
    delayed: set[str] = set()

    def delayed_delete(kind: str, name: str, **kwargs: Any) -> Any:
        if kind == "pods" and name.startswith("reader-"):
            delayed.add(name)
            return {}
        return real_delete(kind, name, **kwargs)

    def delayed_get(kind: str, name: str) -> Any:
        body = real_get(kind, name)
        if kind == "pods" and name in delayed:
            delayed.remove(name)
            real_delete(kind, name)
        return body

    api.delete = delayed_delete
    api.get = delayed_get
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.workspace_state.checked
    assert outputs.workspace_state.leftover == ()


async def test_job_cleanup_waits_for_background_pod_deletion() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    real_list = api.list_objects
    polls = 0

    def delayed_list(kind: str, **kwargs: Any) -> Any:
        nonlocal polls
        rows = real_list(kind, **kwargs)
        if kind == "pods" and kwargs.get("label_selector") == f"job-name={handle.ref}" and rows:
            polls += 1
            api.delete("pods", str(rows[0]["metadata"]["name"]))
        return rows

    api.list_objects = delayed_list
    await provider._await_job_pods_gone(handle.ref, timeout=1)
    assert polls == 1


async def test_waits_fail_when_a_deleted_pod_survives_the_timeout() -> None:
    api, _registry, provider = build()
    api.create("pods", {"metadata": {"name": "surviving-pod"}})

    with pytest.raises(ProviderError, match=r"surviving-pod.*still present"):
        await provider._await_pod_gone("surviving-pod", timeout=0)


async def test_waits_fail_when_a_deleted_job_pod_survives_the_timeout() -> None:
    api, _registry, provider = build()
    api.create(
        "pods",
        {"metadata": {"name": "surviving-job-pod", "labels": {"job-name": "surviving-job"}}},
    )

    with pytest.raises(ProviderError, match=r"surviving-job.*still present"):
        await provider._await_job_pods_gone("surviving-job", timeout=0)


async def test_role_policy_is_removed_when_the_pod_deletion_wait_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _registry, provider = build()
    launch = spec()

    async def pod_wait_fails(
        _job_name: str, *, timeout: float = 15, force: bool = False, collection: bool = False
    ) -> None:
        del timeout, force, collection
        raise ProviderError("Pod was still present")

    monkeypatch.setattr(provider, "_await_job_pods_gone", pod_wait_fails)

    with pytest.raises(ProviderError, match="still present"):
        await provider._run_role_job(
            launch,
            role=k8sspec.ROLE_READER,
            image=IMAGE,
            script="true",
            mounts=(),
            volumes=(),
            limits=provider._limits(launch),
            timeout=1,
            plan=k8sspec.EgressPlan(hosts=("github.com",)),
        )

    policy_name = k8sspec.object_name(f"np-{k8sspec.ROLE_READER}", launch.attempt_id)
    assert ("networkpolicies", policy_name) in api.deleted


async def test_reconcile_does_not_adopt_a_job_whose_pod_is_finished() -> None:
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    assert await provider.reconcile() == []


async def test_reconcile_adopts_a_job_with_no_pod_yet_after_a_restart() -> None:
    """103, 26: a restarted supervisor has no memory of the launch, so a Job the
    controller has not yet given a Pod must still be adopted, or the launch timeout
    of 26 can never apply to it and `_reconcile_stranded` fails it immediately."""
    api, _registry, provider, launch, workspace = await prepared()
    api.no_pod_yet.add(launch.attempt_id)
    handle = await provider.launch(workspace, launch)
    provider._launched.clear()
    adopted = await provider.reconcile()
    assert [(h.attempt_id, h.ref) for h in adopted] == [(launch.attempt_id, handle.ref)]
    observation = await provider.observe(adopted[0])
    assert observation.state is ObservationState.RUNNING


async def test_reconcile_adopts_a_job_with_no_pod_already_past_the_launch_timeout() -> None:
    """103, 26: the Job's own creation time stands in for the launch time, so an
    attempt already past the launch timeout when a restarted supervisor finds it is
    caught on the first observation instead of being given the window again."""
    api, _registry, provider, launch, workspace = await prepared()
    api.no_pod_yet.add(launch.attempt_id)
    provider.config = replace(provider.config, launch_timeout_seconds=1)
    handle = await provider.launch(workspace, launch)
    api.objects[("jobs", handle.ref)].body["metadata"]["creationTimestamp"] = "2020-01-01T00:00:00Z"
    provider._launched.clear()
    adopted = await provider.reconcile()
    assert [(h.attempt_id, h.ref) for h in adopted] == [(launch.attempt_id, handle.ref)]
    observation = await provider.observe(adopted[0])
    assert observation.state is ObservationState.EXITED and observation.exit_code == 70


async def test_an_adopted_job_with_no_pod_yet_collects_with_the_real_image() -> None:
    """103: an attempt adopted before its Pod existed takes its image and grace period
    from the Job's template, so once the Pod appears and exits the helper Pods of
    collection run the worker image, not an empty reference the API server rejects."""
    api, _registry, provider, launch, workspace = await prepared()
    api.no_pod_yet.add(launch.attempt_id)
    handle = await provider.launch(workspace, launch)
    provider._launched.clear()
    adopted = await provider.reconcile()
    assert provider._launched[launch.attempt_id].image_digest == handle.image_digest
    assert provider._launched[launch.attempt_id].limits.grace_seconds == 30
    assert provider._launched[launch.attempt_id].limits_source == "template"
    # The Job controller gets round to it after the restart.
    api.no_pod_yet.discard(launch.attempt_id)
    api._start_job(api.objects[("jobs", handle.ref)].body)
    await run_to_exit(provider, adopted[0])
    before = len(api.created)
    await provider.collect(adopted[0], workspace, launch)
    helpers = [
        c["body"]["spec"]["template"]["spec"]["containers"][0]["image"]
        for c in api.created[before:]
        if c["kind"] == "jobs"
    ]
    assert helpers and all(image == handle.image_digest for image in helpers)


async def test_an_adopted_attempt_records_its_node() -> None:
    """A Pod found by reconcile is the only chance to learn its node, since `observe`
    restores the node only when it first learns the Pod's name (26)."""
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    provider._launched.clear()
    adopted = await provider.reconcile()
    await run_to_exit(provider, adopted[0])
    outputs = await provider.collect(adopted[0], workspace, launch)
    evidence = next(a for a in outputs.artifacts if a.name == "report/kubernetes-launch.json")
    document = json.loads(evidence.content)
    assert document["pod"] == f"{handle.ref}-abc12"
    assert document["node"] == "lab-node-1"


async def test_the_launch_evidence_records_the_node_the_canary_measured() -> None:
    """The launch evidence records the node the namespace-scope canary ran on."""
    _api, _registry, provider, launch, workspace = await prepared()
    # Ensure the canary probe passes so a launch can proceed.
    await provider.ensure_ready()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    outputs = await provider.collect(handle, workspace, launch)
    # Check the launch evidence artifact.
    evidence = next(a for a in outputs.artifacts if a.name == "report/kubernetes-launch.json")
    document = json.loads(evidence.content)
    assert document["canary_node"] == "lab-node-1"
    # Check the health checks.
    health = await provider.health()
    assert health.checks["canary_node"] == "lab-node-1"


async def test_the_launch_evidence_records_the_node_when_endpoint_plan_fails() -> None:
    """The endpoint-plan branch that cannot create a NetworkPolicy still records
    canary_node from the namespace canary."""

    def resolver(host: str) -> list[str]:
        if host == "llm.example.local":
            return []  # Force the local endpoint to not resolve
        return HOST_ADDRESSES.get(host, [])

    _, registry, provider = build(
        resolver=resolver,
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            local_endpoint_url="https://llm.example.local/v1",
            local_endpoint_cidrs=("10.10.0.0/24",),
        ),
    )
    image = "crucible-worker:script-harness-fake-succeed-2"
    registry.register(image, harness="script-harness", version="1.0.0")
    launch = spec(
        harness="script-harness",
        image=image,
        endpoint="subscription",
    )
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    outputs = await provider.collect(handle, workspace, launch)
    evidence = next(a for a in outputs.artifacts if a.name == "report/kubernetes-launch.json")
    document = json.loads(evidence.content)
    assert document["canary_node"] == "lab-node-1"
    health = await provider.health()
    assert health.checks["canary_node"] == "lab-node-1"


def _record_deletes(api: FakeKubernetesApi) -> list[tuple[str, str, int | None]]:
    """Every delete with the grace period it asked for; the fake itself keeps names."""
    seen: list[tuple[str, str, int | None]] = []
    real = api.delete

    def delete(kind: str, name: str, **kwargs: Any) -> None:
        seen.append((kind, name, kwargs.get("grace_period_seconds")))
        real(kind, name, **kwargs)

    api.delete = delete  # type: ignore[method-assign]
    return seen


async def test_an_adopted_attempt_drains_with_the_task_policys_grace_period() -> None:
    """Issue 66: after a supervisor restart the adopted attempt has no policy in memory,
    and a drain must still give the worker the policy's grace, not a 60 s default."""
    policy = {
        "images": {"allowlist": ["crucible-worker:*"]},
        "resources": {"cpus": 2, "memory": "4GiB"},
        "limits": {"grace_seconds": 45},
    }
    api, _registry, provider, launch, workspace = await prepared(policy=policy)
    handle = await provider.launch(workspace, launch)
    provider._launched.clear()
    [adopted] = await provider.reconcile()
    assert provider._launched[launch.attempt_id].limits.grace_seconds == 45
    deletes = _record_deletes(api)
    await provider.terminate(adopted, "drain")
    assert deletes == [("pods", f"{handle.ref}-abc12", 45)]


async def test_a_drain_on_a_handle_the_provider_never_saw_uses_the_pods_grace() -> None:
    """Issue 66: a handle with no `_Launched` behind it (a terminate before any
    reconcile) reads the grace period off the live Pod rather than guessing 60 s."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    provider._launched.clear()
    deletes = _record_deletes(api)
    await provider.terminate(handle, "drain")
    assert deletes == [("pods", f"{handle.ref}-abc12", 30)]


async def test_an_adopted_attempt_records_the_live_pods_limits() -> None:
    """Issue 76: the hardening evidence of an adopted attempt is what its live Pod
    carries (here, as an admission controller rewrote it), not the policy's intent."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    pod = api.objects[("pods", f"{handle.ref}-abc12")].body
    container = pod["spec"]["containers"][0]
    container["resources"] = {
        "limits": {"cpu": "1500m", "memory": "3Gi", "ephemeral-storage": "1Gi"},
        "requests": {"cpu": "750m", "memory": "3Gi"},
    }
    provider._launched.clear()
    [adopted] = await provider.reconcile()
    await run_to_exit(provider, adopted)
    outputs = await provider.collect(adopted, workspace, launch)
    evidence = next(a for a in outputs.artifacts if a.name == "report/kubernetes-launch.json")
    document = json.loads(evidence.content)
    assert document["limits_source"] == "pod"
    assert document["limits"]["cpu"] == "1500m"
    assert document["limits"]["memory"] == str(3 * 1024**3)
    assert document["limits"]["cpu_request"] == "750m"
    assert document["limits"]["ephemeral_storage"] == "1Gi"
    assert document["limits"]["termination_grace_seconds"] == 30


async def test_a_launched_attempt_records_limits_read_from_its_pod() -> None:
    """Issue 76: without a restart too, the evidence is the Pod as the API server
    stored it once the Pod has been seen."""
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    outputs = await provider.collect(handle, workspace, launch)
    evidence = next(a for a in outputs.artifacts if a.name == "report/kubernetes-launch.json")
    document = json.loads(evidence.content)
    assert document["limits_source"] == "pod"
    assert document["limits"] == provider._limits(launch).as_dict()


async def test_reconcile_does_not_adopt_a_finished_job_with_no_pod() -> None:
    """A Job that already finished before this restart and had its Pod reaped is not a
    launch still in flight; the normal cleanup pass handles it, not reconcile."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    api.objects[("jobs", handle.ref)].body["status"] = {
        "succeeded": 1,
        "conditions": [{"type": "Complete", "status": "True"}],
    }
    api.remove_pod_out_of_band(launch.attempt_id)
    provider._launched.clear()
    assert await provider.reconcile() == []


async def test_retention_removes_what_is_labelled_for_an_attempt_crucible_forgot() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    await provider.launch(workspace, launch)
    assert await provider.retention(keep=[]) > 0
    assert api.object_names("jobs") == []


# ----- credentials (12) ----------------------------------------------------


async def codex_attempt(**build_kwargs: Any) -> Any:
    api, registry, provider = build(harness="codex", **build_kwargs)
    registry.register(CODEX_IMAGE, harness="codex", version="0.153.4")
    api.put_harness_secret("hades-harness-codex", {"auth.json": _auth("2026-09-20T00:00:00Z")})
    launch = spec(harness="codex", image=CODEX_IMAGE)
    workspace = await provider.prepare(launch)
    return api, provider, launch, workspace


async def test_the_per_attempt_secret_is_copied_from_the_harness_secret() -> None:
    api, _provider, _launch, _workspace = await codex_attempt()
    copy = api.harness_secret("cred-01attempt0000000000000000a")
    assert list(copy) == ["auth.json"]
    assert copy["auth.json"] == _auth("2026-09-20T00:00:00Z")


async def test_a_required_credential_with_no_copied_keys_at_launch_refuses_it() -> None:
    """A required credential's per-attempt Secret copied by `prepare` but gone or
    emptied by the time `launch` runs must refuse rather than launch unauthenticated."""
    api, provider, launch, workspace = await codex_attempt()
    api.delete("secrets", "cred-01attempt0000000000000000a")
    with pytest.raises(
        HarnessRefusedError,
        match=(
            r"the credential Secret 'cred-01attempt0000000000000000a' for harness "
            r"'codex' holds none of its copied auth files"
        ),
    ):
        await provider.launch(workspace, launch)


async def test_a_failed_read_of_the_copy_at_launch_refuses_a_required_credential() -> None:
    """A transient API error reading the per-attempt Secret must not read as an empty
    (and therefore absent) credential for a required credential."""
    api: FakeKubernetesApi
    api, provider, launch, workspace = await codex_attempt()

    orig_get = api.get

    def fail_get(kind: str, name: str) -> dict[str, Any]:
        if kind == "secrets" and name == "cred-01attempt0000000000000000a":
            raise KubernetesApiError(500, "Internal Server Error")
        return orig_get(kind, name)

    api.get = fail_get  # type: ignore[method-assign]

    with pytest.raises(
        HarnessRefusedError,
        match=(
            r"the credential Secret 'cred-01attempt0000000000000000a' for harness "
            r"'codex' is not readable in hades-workers \(500\)"
        ),
    ):
        await provider.launch(workspace, launch)


async def test_a_missing_required_auth_file_refuses_the_launch() -> None:
    api, registry, provider = build(harness="codex")
    registry.register(CODEX_IMAGE, harness="codex", version="0.153.4")
    api.put_harness_secret("hades-harness-codex", {})
    launch = spec(harness="codex", image=CODEX_IMAGE)
    with pytest.raises(LaunchRefusedError, match="missing its auth file"):
        await provider.prepare(launch)


async def test_a_harness_with_no_secret_at_all_refuses_the_launch() -> None:
    _api, registry, provider = build(harness="codex")
    registry.register(CODEX_IMAGE, harness="codex", version="0.153.4")
    launch = spec(harness="codex", image=CODEX_IMAGE)
    with pytest.raises(LaunchRefusedError, match="not readable"):
        await provider.prepare(launch)


async def test_hermes_uses_no_secret_when_its_optional_credential_is_unconfigured() -> None:
    def resolver(host: str) -> list[str]:
        return ["10.10.0.42/32"] if host == "llm.apps.int.sentania.net" else ["151.101.0.223/32"]

    api, registry, provider = build(
        harness="hermes",
        resolver=resolver,
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            local_endpoint_cidrs=("10.10.0.0/24",),
        ),
    )
    image = "crucible-worker:hermes-fake-succeed-1"
    registry.register(image, harness="hermes", version="0.19.0")
    launch = spec(
        harness="hermes",
        image=image,
        endpoint="local",
        endpoint_url="https://llm.apps.int.sentania.net/v1",
        model="coder",
    )
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")


async def test_kubernetes_reports_a_configured_hermes_secret_as_available() -> None:
    api, _registry, provider = build(
        harness="hermes",
        config=KubernetesConfig(credential_secrets={"hermes": "hades-harness-hermes"}),
    )
    assert not await provider.credential_available("hermes")
    api.put_harness_secret("hades-harness-hermes", {})
    assert not await provider.credential_available("hermes")
    api.put_harness_secret("hades-harness-hermes", {"api-key": b"secret-token"})
    assert await provider.credential_available("hermes")


async def test_an_empty_optional_credential_secret_counts_as_absent() -> None:
    """89: an optional credential whose Secret exists but contains none of the declared
    auth files counts as absent, matching the Docker provider."""

    def resolver(host: str) -> list[str]:
        return ["10.10.0.42/32"] if host == "llm.apps.int.sentania.net" else ["151.101.0.223/32"]

    api, registry, provider = build(
        harness="hermes",
        resolver=resolver,
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            local_endpoint_cidrs=("10.10.0.0/24",),
            credential_secrets={"hermes": "hades-harness-hermes"},
        ),
    )
    api.put_harness_secret("hades-harness-hermes", {})
    image = "crucible-worker:hermes-fake-succeed-1"
    registry.register(image, harness="hermes", version="0.19.0")
    launch = spec(
        harness="hermes",
        image=image,
        endpoint="local",
        endpoint_url="https://llm.apps.int.sentania.net/v1",
        model="coder",
    )
    assert not await provider.credential_available("hermes")
    ctx = provider._launch_context(launch)
    assert ctx.credential_mounted is False

    adapter = provider.harnesses.require("hermes")
    adapter_launch = adapter.build_launch(ctx)
    assert adapter_launch.env["OPENAI_API_KEY"] == "local-no-auth"
    assert adapter_launch.env_from_files == {}
    launch = replace(
        launch,
        command=tuple(adapter_launch.argv),
        env=dict(adapter_launch.env),
        env_from_files=dict(adapter_launch.env_from_files),
    )

    workspace = await provider.prepare(launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")
    await provider.launch(workspace, launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")

    worker_pod = pod_of(api, "worker-")
    volume_names = [v["name"] for v in worker_pod["volumes"]]
    assert "cred" not in volume_names
    assert "cred-source" not in volume_names
    container = worker_pod["containers"][0]
    container_env = {e["name"]: e["value"] for e in container["env"]}
    assert "CRUCIBLE_ENV_FROM_FILES" not in container_env
    assert container_env.get("OPENAI_API_KEY") == "local-no-auth"


async def test_kubernetes_optional_credential_404_launches_with_placeholder() -> None:
    """A 404 on an optional credential launches with the placeholder."""

    def resolver(host: str) -> list[str]:
        return ["10.10.0.42/32"] if host == "llm.apps.int.sentania.net" else ["151.101.0.223/32"]

    api, registry, provider = build(
        harness="hermes",
        resolver=resolver,
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            local_endpoint_cidrs=("10.10.0.0/24",),
            credential_secrets={"hermes": "hades-harness-hermes"},
        ),
    )
    # The Secret is mapped in config but does not exist in Kubernetes (404)
    assert not await provider.credential_available("hermes")

    image = "crucible-worker:hermes-fake-succeed-1"
    registry.register(image, harness="hermes", version="0.19.0")
    launch = spec(
        harness="hermes",
        image=image,
        endpoint="local",
        endpoint_url="https://llm.apps.int.sentania.net/v1",
        model="coder",
    )
    ctx = provider._launch_context(launch)
    assert ctx.credential_mounted is False

    adapter = provider.harnesses.require("hermes")
    adapter_launch = adapter.build_launch(ctx)
    assert adapter_launch.env["OPENAI_API_KEY"] == "local-no-auth"
    assert adapter_launch.env_from_files == {}
    launch = replace(
        launch,
        command=tuple(adapter_launch.argv),
        env=dict(adapter_launch.env),
        env_from_files=dict(adapter_launch.env_from_files),
    )

    workspace = await provider.prepare(launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")
    await provider.launch(workspace, launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")

    worker_pod = pod_of(api, "worker-")
    volume_names = [v["name"] for v in worker_pod["volumes"]]
    assert "cred" not in volume_names
    assert "cred-source" not in volume_names
    container = worker_pod["containers"][0]
    container_env = {e["name"]: e["value"] for e in container["env"]}
    assert "CRUCIBLE_ENV_FROM_FILES" not in container_env
    assert container_env.get("OPENAI_API_KEY") == "local-no-auth"


async def test_kubernetes_optional_credential_500_refuses_launch_naming_status() -> None:
    """A 500 on an optional credential refuses the launch naming the status."""
    api, registry, provider = build(
        harness="hermes",
        config=KubernetesConfig(credential_secrets={"hermes": "hades-harness-hermes"}),
    )

    orig_get = api.get

    def fail_get(kind: str, name: str) -> dict[str, Any]:
        if kind == "secrets" and name == "hades-harness-hermes":
            raise KubernetesApiError(500, "Internal Server Error")
        return orig_get(kind, name)

    api.get = fail_get  # type: ignore[method-assign]

    refuses_naming_500 = (
        r"refusing to launch: the credential Secret 'hades-harness-hermes' "
        r"for harness 'hermes' is not readable in hades-workers \(500\)"
    )

    with pytest.raises(HarnessRefusedError, match=refuses_naming_500):
        await provider.credential_available("hermes")

    image = "crucible-worker:hermes-fake-succeed-1"
    registry.register(image, harness="hermes", version="0.19.0")
    launch = spec(harness="hermes", image=image)
    with pytest.raises(HarnessRefusedError, match=refuses_naming_500):
        await provider.prepare(launch)


async def test_a_required_credential_with_an_empty_secret_fails_naming_the_secret() -> None:
    """89: a required credential whose Secret is empty fails naming the empty Secret."""
    api, registry, provider = build(harness="codex")
    registry.register(CODEX_IMAGE, harness="codex", version="0.153.4")
    api.put_harness_secret("hades-harness-codex", {})
    launch = spec(harness="codex", image=CODEX_IMAGE)
    with pytest.raises(
        LaunchRefusedError,
        match=(
            r"the credential Secret 'hades-harness-codex' is empty: "
            r"missing its auth file 'auth\.json'"
        ),
    ):
        await provider.prepare(launch)


async def test_a_required_credential_with_no_declared_auth_files_fails_naming_the_secret() -> None:
    """89: a required credential whose Secret holds no declared auth files fails."""
    api, registry, provider = build(harness="codex")
    registry.register(CODEX_IMAGE, harness="codex", version="0.153.4")
    api.put_harness_secret("hades-harness-codex", {"unrelated.json": b"foo"})
    launch = spec(harness="codex", image=CODEX_IMAGE)
    with pytest.raises(
        LaunchRefusedError,
        match=(
            r"the credential Secret 'hades-harness-codex' is empty: "
            r"missing its auth file 'auth\.json'"
        ),
    ):
        await provider.prepare(launch)


async def test_a_rotated_auth_file_is_written_back_and_the_copy_removed() -> None:
    """12: a valid, newer file is synced back and the copy removed."""
    api, provider, launch, workspace = await codex_attempt()
    handle = await provider.launch(workspace, launch)
    claim = api.claims["ws-01attempt0000000000000000a"]
    claim["credential/auth.json"] = _auth("2026-09-21T00:00:00Z")
    await run_to_exit(provider, handle)
    outputs = await provider.collect(handle, workspace, launch)
    sync = outputs.credential_sync
    assert sync is not None and sync.mount_mode == "rw-narrow"
    assert [(f.name, f.synced, f.reason) for f in sync.files] == [
        ("auth.json", True, "changed; newer issued-at, written back")
    ]
    assert api.harness_secret("hades-harness-codex")["auth.json"] == _auth("2026-09-21T00:00:00Z")
    assert sync.removed and not api.secret_exists("cred-01attempt0000000000000000a")
    assert "credential/auth.json" not in claim


async def test_a_failed_attempts_newer_auth_file_is_still_written_back() -> None:
    """12, issue 56: a harness that refreshed its token before the task failed has
    rotated the refresh token; the one in the harness Secret may already be revoked.
    The newer, valid file is written back whatever the exit code."""
    api, provider, launch, workspace = await codex_attempt()
    api.script(launch.attempt_id, "crash")
    handle = await provider.launch(workspace, launch)
    api.claims["ws-01attempt0000000000000000a"]["credential/auth.json"] = _auth(
        "2026-09-21T00:00:00Z"
    )
    observation = await run_to_exit(provider, handle)
    assert observation.exit_code not in (None, 0)
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.credential_sync is not None
    assert [(f.name, f.synced) for f in outputs.credential_sync.files] == [("auth.json", True)]
    assert api.harness_secret("hades-harness-codex")["auth.json"] == _auth("2026-09-21T00:00:00Z")


async def test_an_older_auth_file_is_recorded_and_not_written_back() -> None:
    api, provider, launch, workspace = await codex_attempt()
    handle = await provider.launch(workspace, launch)
    api.claims["ws-01attempt0000000000000000a"]["credential/auth.json"] = _auth(
        "2026-09-19T00:00:00Z"
    )
    await run_to_exit(provider, handle)
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.credential_sync is not None
    assert outputs.credential_sync.files[0].synced is False
    assert "not newer" in outputs.credential_sync.files[0].reason
    assert api.harness_secret("hades-harness-codex")["auth.json"] == _auth("2026-09-20T00:00:00Z")


async def test_a_file_that_is_not_the_expected_json_shape_is_not_written_back() -> None:
    api, provider, launch, workspace = await codex_attempt()
    handle = await provider.launch(workspace, launch)
    api.claims["ws-01attempt0000000000000000a"]["credential/auth.json"] = b"not json at all"
    await run_to_exit(provider, handle)
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.credential_sync is not None
    assert "not the expected JSON shape" in outputs.credential_sync.files[0].reason
    assert api.harness_secret("hades-harness-codex")["auth.json"] == _auth("2026-09-20T00:00:00Z")


@pytest.mark.parametrize("policy", list(Cleanup))
async def test_the_per_attempt_secret_is_deleted_under_every_cleanup_policy(
    policy: CleanupPolicy,
) -> None:
    """12, 16: `keep` included. 08's "keep the workspace per policy" never keeps it."""
    api, provider, launch, workspace = await codex_attempt()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    await provider.cleanup(workspace, policy, launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")


async def test_discard_removes_the_secret_of_an_attempt_that_is_never_collected() -> None:
    api, provider, launch, workspace = await codex_attempt()
    await provider.launch(workspace, launch)
    await provider.discard(workspace, launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")


async def test_a_launch_that_fails_after_seeding_leaves_no_secret_behind() -> None:
    api, provider, launch, workspace = await codex_attempt()
    api.refuse_create.add("jobs")
    with pytest.raises(ProviderError, match="could not start the worker"):
        await provider.launch(workspace, launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")


async def test_the_writable_copy_is_a_claim_leaf_and_the_read_only_form_is_the_secret() -> None:
    """12: a Kubernetes Secret volume is read-only whatever the mount asks for, so
    `rw-narrow` is the claim leaf an init container seeds, which is the same shape and
    the same place the Docker provider puts it."""
    api, provider, launch, workspace = await codex_attempt()
    await provider.launch(workspace, launch)
    pod = next(
        row["body"]["spec"]["template"]["spec"]
        for row in api.created
        if row["kind"] == "jobs" and str(row["name"]).startswith("worker-")
    )
    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["/home/worker/.codex"] == {
        "name": "ws",
        "mountPath": "/home/worker/.codex",
        "readOnly": False,
        "subPath": "credential",
    }
    # The Crucible-owned template on top of it, read-only, from the identity bundle.
    assert mounts["/home/worker/.codex/config.toml"]["readOnly"] is True
    assert mounts["/home/worker/.codex/config.toml"]["subPath"] == "harness/config.toml"
    init = pod["initContainers"][0]
    assert init["name"] == "credential-seed"
    assert init["securityContext"]["readOnlyRootFilesystem"] is True
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["cred-source"]["secret"]["secretName"] == "cred-01attempt0000000000000000a"
    assert volumes["cred-source"]["secret"]["items"] == [
        {"key": "auth.json", "path": "auth.json", "mode": 0o400}
    ]


# ----- cleanup (08, 16) ----------------------------------------------------


async def test_delete_removes_the_claim_and_keep_labels_it_for_the_sweep() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    await provider.cleanup(workspace, CleanupPolicy.KEEP, launch)
    claim = api.objects[("persistentvolumeclaims", "ws-01attempt0000000000000000a")]
    assert claim.body["metadata"]["labels"][k8sspec.LABEL_RETAIN] == "keep"
    assert api.object_names("jobs") == [] and api.object_names("networkpolicies") == []

    api2, _r2, provider2, launch2, workspace2 = await prepared()
    handle2 = await provider2.launch(workspace2, launch2)
    await run_to_exit(provider2, handle2)
    await provider2.cleanup(workspace2, CleanupPolicy.DELETE, launch2)
    assert api2.object_names("persistentvolumeclaims") == []
    assert api2.object_names("configmaps") == []


async def test_keep_diff_only_removes_the_checkout_and_the_tree_and_keeps_the_evidence() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    await provider.collect(handle, workspace, launch)
    claim = api.claims["ws-01attempt0000000000000000a"]
    claim["output/tree/README.md"] = b"a clone of the collected state"
    await provider.cleanup(workspace, CleanupPolicy.KEEP_DIFF_ONLY, launch)
    assert not [p for p in claim if p.startswith(("repo/", "output/tree/", "credential/"))]
    assert "output/diff.patch" in claim and "output/work_branch.bundle" in claim


# ----- evidence (26) -------------------------------------------------------


async def test_the_attempt_records_26s_observability_fields() -> None:
    _api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    outputs = await provider.collect(handle, workspace, launch)
    evidence = next(a for a in outputs.artifacts if a.name == "report/kubernetes-launch.json")
    document = json.loads(evidence.content)
    assert document["image_digest"] == handle.image_digest
    assert document["job"] == "worker-01attempt0000000000000000a"
    assert document["pod"] == "worker-01attempt0000000000000000a-abc12"
    assert document["node"] == "lab-node-1"
    assert document["pod_pid_limit"] == 4096
    assert document["runtime_class"] == "standard"
    assert document["network_policy"] == "np-worker-01attempt0000000000000000a"
    assert document["limits"]["cpu"] == "2000m"
    assert document["limits"]["termination_grace_seconds"] == 30
    assert "pypi.org" in document["egress"]
    # Nothing in the record is a value (12).
    assert "auth" not in evidence.content.decode().lower()


# ----- what the adversarial review of C8a attacked --------------------------


async def test_a_canary_that_cannot_tell_does_not_pass_the_probe() -> None:
    """26's one un-fakeable gate must fail closed.

    An earlier canary tested the API server with bash's `/dev/tcp` redirect, which is
    not a feature of `sh`: under dash or busybox the redirect fails to open and the
    script would have reported `unreachable` on a namespace with no egress enforcement
    at all. An answer that is not a definite refusal to connect is `inconclusive`, and
    an inconclusive probe refuses every launch."""
    _api, provider = await refused_at_prepare(build={"canary_answer": "inconclusive"})
    probe = await provider.ensure_ready()
    assert probe.passed is False and probe.checked is False
    assert "could not tell" in probe.detail
    assert (await provider.health()).state == "degraded"


async def test_a_canary_whose_output_never_finished_is_not_a_result() -> None:
    _api, provider = await refused_at_prepare(build={"canary_done": False})
    probe = await provider.ensure_ready()
    assert probe.passed is False and probe.checked is False


async def test_a_transient_api_error_is_not_a_lost_worker() -> None:
    """16: `lost` is terminal. A 503 from the API server, or one reset connection, while
    a healthy worker runs must not fail the attempt and retry it while the original Pod
    keeps going. Nothing is decided from a look that failed."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)

    real = api.list_objects

    def flaky(kind: str, **kwargs: Any) -> Any:
        if kind == "pods":
            raise KubernetesApiError(503, "the api server is restarting")
        return real(kind, **kwargs)

    api.list_objects = flaky
    with pytest.raises(ProviderError, match="could not read the Pod"):
        await provider.observe(handle)
    api.list_objects = real
    assert (await provider.observe(handle)).state is ObservationState.RUNNING


async def test_a_completed_job_whose_pod_was_reaped_is_not_lost() -> None:
    """A successful attempt whose Pod a node drain or a TTL controller removed, read by
    a supervisor that has no memory of the launch."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    api.objects[("jobs", handle.ref)].body["status"] = {
        "succeeded": 1,
        "conditions": [{"type": "Complete", "status": "True"}],
    }
    api.remove_pod_out_of_band(launch.attempt_id)
    provider._launched.clear()
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED and observation.exit_code == 0


@pytest.mark.parametrize(
    "break_it",
    ["preparer-fails", "no-head"],
)
async def test_a_prepare_that_fails_after_seeding_leaves_no_secret(break_it: str) -> None:
    """12: the copy is removed on every path, not only the clean one. `prepare` seeds
    the per-attempt Secret before the preparer runs, and a supervisor never calls
    `cleanup` or `discard` for an attempt whose `prepare` raised."""
    api, registry, provider = build(harness="codex")
    registry.register(CODEX_IMAGE, harness="codex", version="0.153.4")
    api.put_harness_secret("hades-harness-codex", {"auth.json": _auth("2026-09-20T00:00:00Z")})
    launch = spec(harness="codex", image=CODEX_IMAGE)
    if break_it == "preparer-fails":
        api.script(launch.attempt_id, "prepare-fails")
    else:
        api.claims_suppress_head = True
    with pytest.raises(ProviderError):
        await provider.prepare(launch)
    assert not api.secret_exists("cred-01attempt0000000000000000a")
    assert "credential/auth.json" not in api.claims.get("ws-01attempt0000000000000000a", {})


async def test_a_preparer_failure_keeps_every_word_of_its_raw_untimestamped_log() -> None:
    """75: the fake's preparer failure log carries no stamp, like the real API's
    un-timestamped read. Stripping a stamp that is not there must not eat the line's
    first word the way partitioning on the first space did."""
    api, _registry, provider = build()
    launch = spec()
    api.script(launch.attempt_id, "prepare-fails")
    with pytest.raises(ProviderError, match="the fake preparer could not clone"):
        await provider.prepare(launch)


async def test_retention_removes_a_claim_for_an_attempt_crucible_forgot() -> None:
    """A failed prepare leaves a workspace claim behind; nothing else would remove it."""
    api, _registry, provider = build()
    launch = spec()
    await provider.prepare(launch)
    assert api.object_names("persistentvolumeclaims")
    assert await provider.retention(keep=[]) > 0
    assert api.object_names("persistentvolumeclaims") == []


async def test_retention_keeps_a_claim_a_cleanup_policy_kept() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    await provider.cleanup(workspace, CleanupPolicy.KEEP, launch)
    await provider.retention(keep=[])
    assert api.object_names("persistentvolumeclaims") == ["ws-01attempt0000000000000000a"]


async def test_a_truncated_collected_output_fails_the_attempt(monkeypatch: Any) -> None:
    """16: outputs Crucible could not read whole are an environment failure. A partial
    extraction would give the gates a diff and a report quietly missing files."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    monkeypatch.setattr(kubernetes_module, "OUTPUT_READ_LIMIT", 8)
    with pytest.raises(CollectionFailedError, match="truncated"):
        await provider.collect(handle, workspace, launch)
    assert api.object_names("secrets") == []


# ----- what the repository's automatic reviewer found on the PR --------------


async def test_an_absent_optional_auth_file_is_not_projected() -> None:
    """A Secret projection naming a key the Secret does not carry is a Pod the kubelet
    refuses to start. Claude Code declares `.claude.json` optional, so a harness Secret
    with only the required file is an ordinary deployment, not a broken one.

    349: in `ro` mode the credential volume is a projection that includes both the
    Secret items (only the present key) and the identity ConfigMap template entries;
    no subPath file mount is layered under another mount's target."""
    api, registry, provider = build(harness="claude_code")
    image = "crucible-worker:claude-fake-succeed-2"
    registry.register(image, harness="claude_code", version="2.1.277")
    api.put_harness_secret("hades-harness-claude-code", {"oauth-token": b"not-a-real-value"})
    launch = spec(harness="claude_code", image=image)
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    pod = next(
        row["body"]["spec"]["template"]["spec"]
        for row in api.created
        if row["kind"] == "jobs" and str(row["name"]).startswith("worker-")
    )
    volume = next(v for v in pod["volumes"] if v["name"] == "cred")
    # 349: ro mode uses a projected volume; check that only the present Secret key
    # is projected, and no absent optional key (`.claude.json`) appears.
    src_secrets = [s for s in volume["projected"]["sources"] if "secret" in s]
    assert len(src_secrets) == 1
    secret_keys = [item["key"] for item in src_secrets[0]["secret"]["items"]]
    assert secret_keys == ["oauth-token"]

    mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "cred")
    assert mount["readOnly"] is True


async def test_an_init_container_failure_is_terminal_not_running_forever() -> None:
    """A Pod whose init container failed reports `Failed` with only the init status
    terminated. Reporting `running` would hang the attempt with nothing to classify,
    collect or clean up, and the credential-seed init container makes this reachable."""
    api, _registry, provider, launch, workspace = await prepared()
    api.script(launch.attempt_id, "hang")
    handle = await provider.launch(workspace, launch)
    pod = next(
        obj for (kind, _), obj in api.objects.items() if kind == "pods" and "worker-" in obj.name
    )
    pod.body["status"] = {
        "phase": "Failed",
        "message": "init container failed",
        "initContainerStatuses": [
            {
                "name": "credential-seed",
                "state": {"terminated": {"exitCode": 1, "reason": "Error"}},
            }
        ],
    }
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED
    assert observation.exit_code == 70
    assert "credential-seed" in (observation.detail or "")


async def test_a_failed_pod_with_no_terminated_container_is_still_terminal() -> None:
    api, _registry, provider, launch, workspace = await prepared()
    api.script(launch.attempt_id, "hang")
    handle = await provider.launch(workspace, launch)
    pod = next(
        obj for (kind, _), obj in api.objects.items() if kind == "pods" and "worker-" in obj.name
    )
    pod.body["status"] = {"phase": "Failed", "reason": "CreateContainerConfigError"}
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED and observation.exit_code == 70


async def test_an_exec_stream_that_ended_early_fails_the_collection() -> None:
    """`exit_code` is None when the API server never sent the error channel, which means
    the stream ended before the command reported. Accepting it would let a partial tar
    through and produce a report quietly missing files. The claim still holds everything,
    so the failure is one the supervisor collects again after (lab findings of
    2026-09-29), not a verdict on the attempt."""
    api, _registry, provider, launch, workspace = await prepared()
    handle = await provider.launch(workspace, launch)
    await run_to_exit(provider, handle)
    real = api.pod_exec

    def truncated(name: str, command: Any, **kwargs: Any) -> Any:
        result = real(name, command, **kwargs)
        return ExecResult(result.stdout, b"", None)

    api.pod_exec = truncated
    with pytest.raises(CollectionUnavailableError, match="stream ended before"):
        await provider.collect(handle, workspace, launch)
    api.pod_exec = real


async def test_an_exec_stream_that_ended_early_is_not_an_absent_credential() -> None:
    """12: a read that failed is not 'the file was gone', and since the lab findings of
    2026-09-29 it is not a read either: the copy stays on the claim, the source is
    untouched, and the collection is one the supervisor runs again."""
    api, provider, launch, workspace = await codex_attempt()
    handle = await provider.launch(workspace, launch)
    api.claims["ws-01attempt0000000000000000a"]["credential/auth.json"] = _auth(
        "2026-09-21T00:00:00Z"
    )
    await run_to_exit(provider, handle)
    real = api.pod_exec

    def truncated(name: str, command: Any, **kwargs: Any) -> Any:
        result = real(name, command, **kwargs)
        if "tar cf -" in command[-1]:
            return result
        return ExecResult(result.stdout, b"", None)

    api.pod_exec = truncated
    with pytest.raises(CollectionUnavailableError, match="could not be read back"):
        await provider.collect(handle, workspace, launch)
    api.pod_exec = real
    assert api.harness_secret("hades-harness-codex")["auth.json"] == _auth("2026-09-20T00:00:00Z")
    assert api.claims["ws-01attempt0000000000000000a"]["credential/auth.json"] == _auth(
        "2026-09-21T00:00:00Z"
    )


async def test_a_cleaner_job_that_failed_is_not_a_successful_removal() -> None:
    """12: a credential leaf still on the claim has not been removed, whatever the
    caller would otherwise have recorded."""
    api, provider, launch, workspace = await codex_attempt()
    handle = await provider.launch(workspace, launch)
    api.claims["ws-01attempt0000000000000000a"]["credential/auth.json"] = _auth(
        "2026-09-21T00:00:00Z"
    )
    await run_to_exit(provider, handle)
    api.refuse_roles.add("cleaner")
    outputs = await provider.collect(handle, workspace, launch)
    api.refuse_roles.discard("cleaner")
    assert outputs.credential_sync is not None
    assert outputs.credential_sync.removed is False


async def test_an_adopted_pending_job_still_times_out() -> None:
    """26's Pending timeout has to survive a supervisor restart. The clock comes from
    the Job's creation timestamp, so a Pod already past the window is caught on the
    first observation rather than being given it again."""
    api, _registry, provider, launch, workspace = await prepared()
    assert (await provider.ensure_ready()).passed
    api.pending_forever.add(launch.attempt_id)
    handle = await provider.launch(workspace, launch)
    # A restarted supervisor: no memory of the launch, and the Job was created an hour
    # ago as far as the API server is concerned.
    provider._launched.clear()
    api.objects[("jobs", handle.ref)].body["metadata"]["creationTimestamp"] = "2020-01-01T00:00:00Z"
    adopted = await provider.reconcile()
    assert len(adopted) == 1
    observation = await provider.observe(adopted[0])
    assert observation.state is ObservationState.EXITED and observation.exit_code == 70
    assert "Unschedulable" in (observation.detail or "")


def test_the_readiness_canary_runs_the_configured_probe_image() -> None:
    """26: the canary needs one exact, pullable reference. A bare repository is what
    `image_repositories` holds, and a kubelet reads one as `:latest` (C9)."""
    _, _, provider = build(
        config=KubernetesConfig(
            image_repositories=("registry.example/crucible-worker",),
            probe_image="registry.example/crucible-worker:script-harness-1.0.0",
        )
    )
    assert provider._probe_image() == "registry.example/crucible-worker:script-harness-1.0.0"


def test_without_a_probe_image_the_canary_falls_back_to_the_first_repository() -> None:
    _, _, provider = build(
        config=KubernetesConfig(image_repositories=("registry.example/crucible-worker",))
    )
    assert provider._probe_image() == "registry.example/crucible-worker"


def test_a_skip_resumes_after_the_crowded_second_not_after_the_line_the_cap_cut() -> None:
    """Review of issue 63: the cut line belongs to the next second, which may be
    perfectly readable; the skip moves past the second of the whole lines only."""
    payload = (
        _stamped(20, 100_000_000, "a").encode()
        + b"\n"
        + _stamped(20, 200_000_000, "b").encode()
        + b"\n"
        + _stamped(21, 0, "the cap cut th").encode()
    )
    [notice] = kubernetes_module._skip_crowded_second(payload, 64, LogOffset())
    assert notice.ts is not None and notice.ts.isoformat() == "2026-09-25T17:00:21+00:00"


def test_a_skip_inside_one_line_longer_than_the_ceiling_passes_that_line() -> None:
    payload = _stamped(21, 5, "x" * 200).encode()[:100]
    [notice] = kubernetes_module._skip_crowded_second(payload, 100, LogOffset())
    assert notice.ts is not None and notice.ts.isoformat() == "2026-09-25T17:00:22+00:00"


def test_a_skip_with_no_readable_stamp_still_moves_past_the_stored_position() -> None:
    since = LogOffset(timestamp="2026-09-25T17:00:30.500000+00:00", line_sha256="0" * 64)
    [notice] = kubernetes_module._skip_crowded_second(b"no stamp here", 64, since)
    assert notice.ts is not None and notice.ts.isoformat() == "2026-09-25T17:00:31+00:00"


# ----- the reference cache (26, crucible#55) -------------------------------


def _job_pod(api: FakeKubernetesApi, prefix: str) -> dict[str, Any]:
    job = next(
        c["body"] for c in api.created if c["kind"] == "jobs" and c["name"].startswith(prefix)
    )
    pod: dict[str, Any] = job["spec"]["template"]["spec"]
    return pod


def _cache_volume(pod: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    volume = next(v for v in pod["volumes"] if v["name"] == "cache")
    mount = next(m for c in pod["containers"] for m in c["volumeMounts"] if m["name"] == "cache")
    return volume, mount


async def test_the_preparer_mounts_the_reference_cache_read_only() -> None:
    config = KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        cache_claim="crucible-reference-cache",
    )
    api, _registry, _provider, _launch, _workspace = await prepared(build={"config": config})

    kinds = [c["name"].split("-")[0] for c in api.created if c["kind"] == "jobs"]
    # The refresher runs, and finishes, before the preparer starts.
    assert kinds.index("refresh") < kinds.index("prepare")

    refresher = _job_pod(api, "refresh-cache-")
    volume, mount = _cache_volume(refresher)
    assert volume["persistentVolumeClaim"] == {"claimName": "crucible-reference-cache"}
    assert not mount.get("readOnly")
    # The one Pod that writes the cache carries nothing of the attempt's.
    assert {v["name"] for v in refresher["volumes"]} & {"ws", "identity", "credential"} == set()
    script = refresher["containers"][0]["command"][-1]
    assert "fetch --prune origin" in script and "clone --mirror" in script

    preparer = _job_pod(api, "prepare-")
    volume, mount = _cache_volume(preparer)
    assert volume["persistentVolumeClaim"]["readOnly"] is True
    assert mount["readOnly"] is True
    script = preparer["containers"][0]["command"][-1]
    assert "fetch --prune" not in script and "clone --mirror" not in script
    assert "--reference" in script


async def test_the_preparer_mounts_no_cache_when_none_is_configured() -> None:
    api, _registry, _provider, _launch, _workspace = await prepared()
    assert not [c for c in api.created if c["name"].startswith("refresh-cache-")]
    assert "cache" not in {v["name"] for v in _job_pod(api, "prepare-")["volumes"]}


async def test_a_refresh_waits_for_readers_and_holds_new_ones_back() -> None:
    gate = kubernetes_module._CacheGate()
    order: list[str] = []
    first_in = asyncio.Event()
    release_first = asyncio.Event()
    writer_started = asyncio.Event()
    late_reader_started = asyncio.Event()

    async def first_reader() -> None:
        async with gate.reading():
            order.append("first in")
            first_in.set()
            await release_first.wait()
            order.append("first out")

    async def writer() -> None:
        writer_started.set()
        async with gate.writing():
            order.append("write")

    async def late_reader() -> None:
        late_reader_started.set()
        async with gate.reading():
            order.append("late in")

    first = asyncio.create_task(first_reader())
    await first_in.wait()
    write = asyncio.create_task(writer())
    await writer_started.wait()
    # The refresh is waiting on the first reader; a reader that arrives now waits
    # behind the refresh rather than starving it.
    late = asyncio.create_task(late_reader())
    await late_reader_started.wait()
    assert order == ["first in"]
    release_first.set()
    await asyncio.gather(first, write, late)
    assert order == ["first in", "first out", "write", "late in"]


async def test_refreshes_are_coalesced() -> None:
    gate = kubernetes_module._CacheGate()
    assert gate.refresh_due(0.0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def writer() -> None:
        async with gate.writing():
            entered.set()
            await release.wait()

    task = asyncio.create_task(writer())
    await entered.wait()
    # A refresh running (or waiting) is the one every prepare that arrives now shares.
    assert not gate.refresh_due(10_000.0)
    release.set()
    await task
    now = time.monotonic()
    assert not gate.refresh_due(now)
    assert gate.refresh_due(now + kubernetes_module.CACHE_REFRESH_INTERVAL_SECONDS)


async def test_attempts_of_one_repository_prepared_together_share_one_refresh() -> None:
    config = KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        cache_claim="crucible-reference-cache",
    )
    api, _registry, provider = build(config=config)
    await asyncio.gather(
        provider.prepare(spec(attempt_id="01M3AAAAAAAAAAAAAAAAAAAAA1")),
        provider.prepare(spec(attempt_id="01M3AAAAAAAAAAAAAAAAAAAAA2")),
    )
    jobs = [c["name"] for c in api.created if c["kind"] == "jobs"]
    assert len([n for n in jobs if n.startswith("refresh-cache-")]) == 1
    assert len([n for n in jobs if n.startswith("prepare-")]) == 2


@pytest.fixture
def retry_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = 0.0
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(kubernetes_module, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return sleeps


def refused_connection() -> KubernetesApiError:
    error = KubernetesUnavailableError(0, "connection failed")
    error.__cause__ = ConnectionRefusedError(111, "refused")
    return error


async def test_a_refused_connection_at_prepare_is_retried_then_succeeds(
    retry_clock: list[float],
) -> None:
    api, _, provider = build()
    api.fail_next("create", 2, kind="persistentvolumeclaims", error=refused_connection())
    workspace = await provider.prepare(spec())
    assert workspace.started_from
    assert api.create_attempts.count("persistentvolumeclaims") == 3
    assert retry_clock == [1, 2]


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422, 429])
async def test_a_4xx_at_prepare_is_not_retried(
    retry_clock: list[float],
    status: int,
) -> None:
    api, _, provider = build()
    error = KubernetesApiError(status, "refusal")
    api.fail_next("create", kind="persistentvolumeclaims", error=error)
    with pytest.raises(KubernetesApiError) as caught:
        await provider.prepare(spec())
    assert caught.value is error
    assert api.create_attempts.count("persistentvolumeclaims") == 1
    assert retry_clock == []


async def test_the_retry_deadline_reraises_the_last_transport_error(
    retry_clock: list[float],
) -> None:
    api, _, provider = build()
    provider.config = replace(provider.config, api_retry_seconds=40)
    first, last = refused_connection(), refused_connection()
    api.fail_next("get", error=first)
    api.fail_next("get", 20, error=last)
    with pytest.raises(KubernetesApiError) as caught:
        await provider._call_with_backoff(api.get, "pods", "missing")
    assert caught.value is last
    assert retry_clock == [1, 2, 4, 8, 16, 9]


async def test_launch_retries_the_worker_job_create_on_a_503(
    retry_clock: list[float],
) -> None:
    api, _, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    before = api.create_attempts.count("jobs")
    api.fail_next("create", kind="jobs")
    handle = await provider.launch(workspace, launch)
    assert handle.ref.startswith("worker-")
    assert api.create_attempts.count("jobs") - before == 2
    assert retry_clock == [1]


@pytest.mark.parametrize("operation,kind", [("create", "pods"), ("pod_exec", "")])
async def test_prepared_head_reader_retries_transport_failures(
    retry_clock: list[float],
    operation: str,
    kind: str,
) -> None:
    api, _, provider = build()
    await provider._resolve_image(spec())
    assert (await provider.ensure_ready()).passed
    api.fail_next(operation, 2, kind=kind, error=refused_connection())
    workspace = await provider.prepare(spec())
    assert workspace.started_from
    assert retry_clock == [1, 2]


@pytest.mark.parametrize("kind", ["secrets", "pods", "jobs", "networkpolicies"])
async def test_a_lost_create_response_is_verified_before_accepting_conflict(
    monkeypatch: pytest.MonkeyPatch,
    retry_clock: list[float],
    kind: str,
) -> None:
    api, _, provider = build()
    original = FakeKubernetesApi.create
    lost = False

    def create(self: FakeKubernetesApi, resource: str, body: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal lost
        result = original(self, resource, body)
        if not lost:
            lost = True
            raise refused_connection()
        return result

    monkeypatch.setattr(FakeKubernetesApi, "create", create)
    await provider._create_with_backoff(kind, {"metadata": {"name": "ambiguous"}})
    assert api.create_attempts == [kind, kind]
    assert retry_clock == [1]


async def test_a_transport_failure_does_not_make_a_stale_object_acceptable(
    retry_clock: list[float],
) -> None:
    api, _, provider = build()
    body = {"metadata": {"name": "stale"}, "data": {"key": "old"}}
    api.create("secrets", body)
    api.fail_next("create", kind="secrets", error=refused_connection())
    with pytest.raises(KubernetesApiError) as caught:
        await provider._create_with_backoff("secrets", body)
    assert caught.value.status == 409
    assert retry_clock == [1]


@pytest.mark.parametrize(
    "status,expected",
    [(0, False), (403, False), (429, False), (500, False), (502, True), (503, True), (504, True)],
)
def test_transport_classification_uses_status_not_error_text(status: int, expected: bool) -> None:
    assert (
        kubernetes_module.is_transport(KubernetesApiError(status, "Connection refused")) is expected
    )


async def test_canary_transport_exhaustion_keeps_unavailable_classification(
    retry_clock: list[float],
) -> None:
    api, _, provider = build()
    provider.config = replace(provider.config, api_retry_seconds=3)
    await provider._resolve_image(spec())
    api.fail_next("create", 10, kind="pods", error=refused_connection())
    probe = await provider.ensure_ready()
    assert not probe.passed
    assert probe.unavailable
    assert retry_clock == [1, 2]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError(),
        ConnectionResetError(),
        socket.gaierror(),
        TimeoutError(),
    ],
)
@pytest.mark.parametrize("wrapped", [False, True])
async def test_socket_transport_types_are_retried(
    retry_clock: list[float],
    error: Exception,
    wrapped: bool,
) -> None:
    api, _, provider = build()
    if wrapped:
        outer = KubernetesUnavailableError(0, "wrapped transport")
        outer.__cause__ = error
        error = outer
    api.fail_next("get", error=error)
    api.create("secrets", {"metadata": {"name": "test"}})
    result = await provider._call_with_backoff(api.get, "secrets", "test")
    assert result["metadata"]["name"] == "test"
    assert retry_clock == [1]


async def test_explicit_zero_retry_deadline_overrides_config(retry_clock: list[float]) -> None:
    api, _, provider = build()
    error = refused_connection()
    api.fail_next("get", error=error)
    with pytest.raises(KubernetesApiError) as caught:
        await provider._call_with_backoff(api.get, "secrets", "test", deadline_seconds=0)
    assert caught.value is error
    assert retry_clock == []
