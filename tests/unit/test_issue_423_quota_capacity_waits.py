"""Issue 423 part B, capacity: the quota-derived capacity keeps room for Hades's own
short-role Pods, a quota refusal on create is a wait and never a failure of the attempt,
and every other create refusal lands on the attempt and in the wake with the API
server's words."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.kubernetes import KubernetesConfig
from crucible.adapters.ui.pages.routing import _capacity_words
from crucible.application.admin import kubernetes as kubernetes_admin
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import Attempt, Execution, ExecutionRole, Task
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.ports.execution import (
    LaunchSpec,
    LaunchWaitError,
    ObservationState,
    ProviderError,
    WorkerCapacity,
    Workspace,
)
from tests.fixtures import FakeClock
from tests.unit.kubernetes_fixtures import build, spec
from tests.unit.test_class_routing import NOW
from tests.unit.test_routing import _routing_setup

# The lab quota the issue describes: limits.cpu=32 against a 3 CPU limit per Pod.
LAB_QUOTA = {"limits.cpu": "32", "limits.memory": "128Gi", "count/jobs.batch": "60"}
THREE_CPU_POLICY = {"resources": {"cpus": 3, "memory": "4GiB"}}


def _quota(api: Any, hard: dict[str, str], name: str = "hades-workers") -> None:
    api.create("resourcequotas", {"metadata": {"name": name}, "spec": {"hard": hard}})


def _config(**overrides: Any) -> KubernetesConfig:
    values: dict[str, Any] = {
        "poll_interval_seconds": 0,
        "launch_timeout_seconds": 3600,
        "storage_class": "lab-ssd",
        **overrides,
    }
    return KubernetesConfig(**values)


# ----- the capacity keeps room for the short-role Pods --------------------------------


async def test_the_lab_quota_admits_nine_workers_and_keeps_one_pod_for_a_probe() -> None:
    api, _registry, provider = build(config=_config())
    _quota(api, LAB_QUOTA)
    provider._last_limits = k8sspec.limits_from_policy(THREE_CPU_POLICY)

    capacity = await provider.worker_capacity()

    assert capacity.headroom == 10
    assert capacity.reserved_pods == 1
    assert capacity.workers == 9
    assert k8sspec.quantity(capacity.reservation["each"]["cpu"]) == 3.0
    assert "hades-workers limits.cpu" in capacity.source
    assert provider.capabilities().max_concurrency == 9


async def test_the_health_checks_expose_headroom_reservation_and_capacity() -> None:
    api, _registry, provider = build(config=_config())
    _quota(api, LAB_QUOTA)
    provider._last_limits = k8sspec.limits_from_policy(THREE_CPU_POLICY)

    health = await provider.health()

    assert health.checks["max_concurrency"] == 9
    assert health.checks["quota_headroom"] == 10
    assert health.checks["short_role_pods_reserved"] == 1
    assert health.checks["short_role_reservation"]["pods"] == 1
    assert health.checks["worker_capacity"] == 9
    assert "limits.cpu" in health.checks["capacity_source"]
    assert "gate probe" in health.checks["capacity_detail"]


async def test_more_reserved_pods_leave_fewer_workers() -> None:
    api, _registry, provider = build(config=_config(short_role_pods=2))
    _quota(api, LAB_QUOTA)
    provider._last_limits = k8sspec.limits_from_policy(THREE_CPU_POLICY)
    assert (await provider.worker_capacity()).workers == 8


async def test_an_existing_preparer_satisfies_the_short_role_reservation() -> None:
    """The kind tier has room for three 1-CPU Pods. A hanging preparer is already in
    quota usage and in the supervisor's held-attempt count, so reserving it again must
    not stop the second task after that task's probe has completed."""
    api, _registry, provider = build(config=_config())
    _quota(
        api,
        {
            "count/jobs.batch": "17",
            "requests.cpu": "3",
            "limits.cpu": "6",
            "requests.memory": "12Gi",
            "limits.memory": "12Gi",
        },
    )
    provider._last_limits = k8sspec.limits_from_policy(
        {"resources": {"cpus": 1, "memory": "256MiB"}}
    )
    api.pending_forever.add("prepare-hanging")
    api.create(
        "pods",
        {
            "metadata": {
                "name": "prepare-hanging",
                "labels": {k8sspec.LABEL_ROLE: k8sspec.ROLE_PREPARER},
            },
            "status": {"phase": "Running"},
        },
    )

    capacity = await provider.worker_capacity()

    assert capacity.headroom == 3
    assert capacity.reserved_pods == 0
    assert capacity.workers == 3


async def test_a_quota_for_one_pod_still_admits_one_worker() -> None:
    """A lone attempt's probe, preparer and worker run one after another and never meet,
    so a reservation that would leave nothing is not what keeps the namespace idle."""
    api, _registry, provider = build(config=_config())
    _quota(api, {"pods": "1"})
    capacity = await provider.worker_capacity()
    assert capacity.headroom == 1
    assert capacity.workers == 1


async def test_without_a_quota_the_configured_fallback_is_the_capacity() -> None:
    _api, _registry, provider = build(config=_config(max_concurrency=4))
    capacity = await provider.worker_capacity()
    assert capacity.workers == 4
    assert capacity.headroom is None
    assert "kubernetes.max_concurrency" in capacity.source
    health = await provider.health()
    assert health.checks["max_concurrency"] == 4
    assert "no ResourceQuota" in health.checks["capacity_source"]


async def test_the_fewest_workers_any_resource_admits_binds() -> None:
    """Memory for six Pods beside CPU for ten: five workers, and the source says why."""
    api, _registry, provider = build(config=_config())
    _quota(api, {**LAB_QUOTA, "limits.memory": "24Gi"})
    provider._last_limits = k8sspec.limits_from_policy(THREE_CPU_POLICY)
    capacity = await provider.worker_capacity()
    assert capacity.headroom == 6
    assert capacity.workers == 5
    assert capacity.source.endswith("hades-workers limits.memory binds")


# ----- a quota refusal on create is a wait, never a failure ---------------------------


async def test_a_worker_the_quota_refused_waits_and_leaves_nothing_behind() -> None:
    api, _registry, provider = build(config=_config())
    launch = spec()
    workspace = await provider.prepare(launch)
    api.quota_refused_roles.add(k8sspec.ROLE_WORKER)

    with pytest.raises(LaunchWaitError, match="exceeded quota"):
        await provider.launch(workspace, launch)

    assert launch.attempt_id not in provider._launched
    names = {name for kind, name in api.objects if kind in ("jobs", "networkpolicies")}
    assert not any(name.startswith(("worker", "np-worker")) for name in names)
    assert not api.secret_exists(k8sspec.object_name("cred", launch.attempt_id))


async def test_a_refusal_seen_after_the_launch_window_keeps_the_attempt_waiting() -> None:
    """The Job controller was slow to try: `launch` saw no Pod and no refusal. The
    refusal `observe` then reads keeps the attempt running while the controller retries,
    not a launch failure, and the launch timeout counts from the refusal."""
    api, _registry, provider = build(config=_config())
    launch = spec()
    workspace = await provider.prepare(launch)
    api.no_pod_yet.add(launch.attempt_id)
    handle = await provider.launch(workspace, launch)
    # Past the launch timeout with no Pod would be a launch failure; a quota refusal is
    # what explains the missing Pod, and it restarts the clock.
    provider.config = replace(provider.config, launch_timeout_seconds=0)
    job = api.get("jobs", handle.ref)
    api.events.append(
        {
            "reason": "FailedCreate",
            "involvedObject": {"kind": "Job", "name": handle.ref, "uid": job["metadata"]["uid"]},
            "message": (
                'Error creating: pods "worker-x" is forbidden: exceeded quota: hades-workers, '
                "requested: limits.cpu=3, used: limits.cpu=30, limited: limits.cpu=32"
            ),
        }
    )

    observation = await provider.observe(handle)

    assert observation.state is ObservationState.RUNNING
    assert "waiting for room" in (observation.detail or "")
    assert "exceeded quota" in (observation.detail or "")


async def test_a_gate_probe_the_quota_refused_is_a_wait_not_a_failed_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _registry, provider = build(config=_config())
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    api.quota_refused_roles.add("gate-probe")
    checks = [{"id": "V4", "command": "test -f made-by-the-worker"}]

    with pytest.raises(LaunchWaitError, match="exceeded quota"):
        await provider.probe_checks(spec(), checks)

    assert not any(kind == "jobs" for kind, _ in api.objects)


async def test_a_preparer_the_quota_refused_is_a_wait() -> None:
    api, _registry, provider = build(config=_config())
    api.quota_refused_roles.add(k8sspec.ROLE_PREPARER)
    with pytest.raises(LaunchWaitError, match="exceeded quota"):
        await provider.prepare(spec())


async def test_a_workspace_claim_the_quota_refused_is_a_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _registry, provider = build(config=_config())
    original = api.create

    def refuse_claims(kind: str, body: Any) -> Any:
        if kind == "persistentvolumeclaims":
            raise KubernetesApiError(
                403,
                'persistentvolumeclaims "ws-x" is forbidden: exceeded quota: hades-workers, '
                "requested: persistentvolumeclaims=1, used: persistentvolumeclaims=40, "
                "limited: persistentvolumeclaims=40",
            )
        return original(kind, body)

    monkeypatch.setattr(api, "create", refuse_claims)
    with pytest.raises(LaunchWaitError, match="exceeded quota"):
        await provider.prepare(spec())


# ----- the supervisor holds launches to the capacity and waits on a refusal ----------


class _CapacityProvider(FakeProvider):
    """The fake provider with a derived capacity, as the Kubernetes provider has."""

    def __init__(self, workers: int) -> None:
        super().__init__()
        self.capacity = WorkerCapacity(
            workers=workers,
            source="ResourceQuota hades-workers; hades-workers limits.cpu binds",
            headroom=workers + 1,
            reserved_pods=1,
        )

    async def worker_capacity(self) -> WorkerCapacity:
        return self.capacity


def _events(uow: Any, kind: EventKind) -> list[Any]:
    return [
        event
        for event in (call.args[0] for call in uow.events.append.call_args_list)
        if event.kind == kind.value
    ]


def _launching(monkeypatch: pytest.MonkeyPatch, workers: int) -> tuple[Any, Any, Any, Any]:
    """A pending attempt on a provider whose capacity is `workers`, with one attempt of
    the provider already running (the routing setup's live codex worker)."""
    supervisor, item, uow = _routing_setup(monkeypatch, all_busy=False)
    provider = _CapacityProvider(workers)
    supervisor._providers = {"fake": provider}
    return supervisor, item, uow, provider


async def test_a_launch_past_the_capacity_stays_scheduled_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, _provider = _launching(monkeypatch, workers=1)

    assert await supervisor._begin_launch(item) is None

    assert item.attempt.state is AttemptState.PENDING
    assert item.task.state is TaskState.SCHEDULED
    assert item.attempt.exit_class is None and item.attempt.started_at is None
    deferred = _events(uow, EventKind.HARNESS_LAUNCH_DEFERRED)
    assert len(deferred) == 1
    detail = deferred[0].payload["detail"]
    assert "fake admits 1 worker(s) at once" in detail
    assert "1 Pod(s) kept for Hades's own short-role Pods" in detail
    assert "1 attempt(s) hold them" in detail
    assert "waits for one to finish" in detail
    # Not taken to preparing, so no checkout lease was held for nothing.
    supervisor._take_checkout_lease.assert_not_called()


async def test_a_launch_within_the_capacity_begins(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, item, uow, _provider = _launching(monkeypatch, workers=2)
    assert await supervisor._begin_launch(item) is not None
    assert item.attempt.state is AttemptState.PREPARING
    assert not _events(uow, EventKind.HARNESS_LAUNCH_DEFERRED)


async def test_the_capacity_is_read_once_per_launch_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, item, _uow, provider = _launching(monkeypatch, workers=1)
    reader = AsyncMock(wraps=provider.worker_capacity)
    monkeypatch.setattr(provider, "worker_capacity", reader)
    assert await supervisor._begin_launch(item) is None
    assert await supervisor._begin_launch(item) is None
    assert reader.await_count == 1


async def test_exited_attempts_awaiting_collection_still_hold_their_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collector and the verifier run in the slot the attempt took, so an exited
    attempt counts until it is collected; a terminal one does not."""
    supervisor, item, uow, _provider = _launching(monkeypatch, workers=1)
    (live,) = uow.attempts.list_in_states.return_value
    uow.attempts.list_in_states.side_effect = lambda states, **_: (
        [live] if live.state in states else []
    )
    live.state = AttemptState.EXITED
    assert await supervisor._begin_launch(item) is None
    live.state = AttemptState.COLLECTED
    supervisor._capacity_now.clear()
    assert await supervisor._begin_launch(item) is not None


def _finishing(
    monkeypatch: pytest.MonkeyPatch, *, probe: bool = False
) -> tuple[Any, Any, Any, FakeProvider, LaunchSpec]:
    """A preparing attempt about to be probed, prepared and launched, as _finish_launch
    runs it, with the provider steps a test replaces. With `probe`, the gate probe step
    runs for real against the provider's `probe_checks`."""
    supervisor, item, uow = _routing_setup(monkeypatch, all_busy=False)
    item.attempt.state = AttemptState.PREPARING
    item.task.state = TaskState.RUNNING
    uow.executions.list_for_task.return_value = [item.execution]
    uow.attempts.list_for_execution.return_value = [item.attempt]
    monkeypatch.setattr(supervisor, "_settle_if_cancelled", lambda *_: False)
    monkeypatch.setattr(supervisor, "_cancel_check", lambda *_: AsyncMock(return_value=False))
    monkeypatch.setattr(supervisor, "_release_checkout_leases", MagicMock())
    monkeypatch.setattr(supervisor, "_record_prepared", MagicMock())
    monkeypatch.setattr(supervisor, "_record_bare_evidence", MagicMock())
    monkeypatch.setattr(supervisor, "_classify_and_finish", MagicMock())
    monkeypatch.setattr(supervisor, "_forget_workspace", MagicMock())
    supervisor._workspaces = {}
    supervisor._workspace_fingerprints = {}
    supervisor._github = None

    def mark_launching(attempt_id: str, ws: Workspace) -> bool:
        assert attempt_id == item.attempt.id
        item.attempt.state = AttemptState.LAUNCHING
        return True

    monkeypatch.setattr(supervisor, "_mark_launching", mark_launching)
    provider = FakeProvider()
    launch = LaunchSpec(
        attempt_id=item.attempt.id,
        task_id=item.task.id,
        external_id=item.task.external_id,
        role="implement",
        harness="script-harness",
        model="test",
        image="fake:succeed",
        timeout_seconds=60,
        contract=item.contract,
    )
    monkeypatch.setattr(supervisor, "_build_spec", AsyncMock(return_value=launch))
    workspace = Workspace(
        attempt_id=item.attempt.id,
        checkout_path="k8s://ws/repo",
        identity_path="k8s://ws/identity",
        report_path="k8s://ws/report",
    )
    monkeypatch.setattr(supervisor, "_prepare", AsyncMock(return_value=workspace))
    if probe:
        item.contract["required_verification"] = [
            {"id": "V4", "command": "test -f made-by-the-worker"},
        ]
    else:
        monkeypatch.setattr(supervisor, "_probe_before_prepare", AsyncMock(return_value=True))
    return supervisor, item, uow, provider, launch


REFUSAL = (
    'pods "worker-01-abc" is forbidden: exceeded quota: hades-workers, requested: '
    "limits.cpu=3, used: limits.cpu=30, limited: limits.cpu=32"
)


async def test_a_worker_create_the_quota_refused_returns_the_attempt_to_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, provider, _launch = _finishing(monkeypatch)
    monkeypatch.setattr(
        provider,
        "launch",
        AsyncMock(side_effect=LaunchWaitError(f"the namespace quota has no room: {REFUSAL}")),
    )

    assert not await supervisor._finish_launch(item, provider)

    assert item.attempt.state is AttemptState.PENDING
    assert item.task.state is TaskState.SCHEDULED
    assert item.attempt.exit_class is None
    assert item.attempt.started_at is None and item.attempt.ended_at is None
    assert item.attempt.workspace_path is None
    supervisor._classify_and_finish.assert_not_called()
    assert provider.discarded == [item.attempt.id]
    (deferred,) = _events(uow, EventKind.HARNESS_LAUNCH_DEFERRED)
    assert deferred.payload["quota_wait"] is True
    assert deferred.payload["stage"] == "launch"
    assert "exceeded quota" in deferred.payload["detail"]
    assert deferred.payload["from"] == "launching" and deferred.payload["to"] == "pending"
    (scheduled,) = _events(uow, EventKind.TASK_SCHEDULED)
    assert scheduled.payload["reason"] == "quota_wait"
    assert "exceeded quota" in scheduled.payload["detail"]
    supervisor._release_checkout_leases.assert_called_once()


async def test_a_gate_probe_the_quota_refused_is_retried_not_check_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, provider, _launch = _finishing(monkeypatch, probe=True)
    monkeypatch.setattr(
        provider,
        "probe_checks",
        AsyncMock(side_effect=LaunchWaitError(f"no room for the gate probe: {REFUSAL}")),
    )
    prepare = supervisor._prepare

    assert not await supervisor._finish_launch(item, provider)

    prepare.assert_not_called()
    assert item.attempt.state is AttemptState.PENDING
    assert item.task.state is TaskState.SCHEDULED
    assert item.attempt.termination_reason is None
    assert item.attempt.exit_class is None
    uow.escalations.add.assert_not_called()
    (deferred,) = _events(uow, EventKind.HARNESS_LAUNCH_DEFERRED)
    assert deferred.payload["stage"] == "prepare"
    assert "exceeded quota" in deferred.payload["detail"]


async def test_a_cancelled_task_ends_the_waiting_attempt_as_a_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, _uow, provider, _launch = _finishing(monkeypatch)
    monkeypatch.setattr(supervisor, "_finish_cancelling", MagicMock())
    monkeypatch.setattr(provider, "launch", AsyncMock(side_effect=LaunchWaitError(REFUSAL)))

    def cancel_on_launch(attempt_id: str, ws: Workspace) -> bool:
        item.attempt.state = AttemptState.LAUNCHING
        item.task.state = TaskState.CANCELLING
        return True

    monkeypatch.setattr(supervisor, "_mark_launching", cancel_on_launch)
    assert not await supervisor._finish_launch(item, provider)
    assert item.attempt.state is AttemptState.FAILED
    assert item.attempt.exit_class is ExitClass.KILLED


# ----- every other create refusal lands on the attempt and in the wake ----------------


DENIED = (
    "could not start the worker: 403 on /apis/batch/v1/namespaces/hades-workers/jobs: "
    'admission webhook "policy.lab" denied the request: hostPath volumes are not allowed'
)


async def test_another_create_refusal_records_the_api_servers_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, _uow, provider, _launch = _finishing(monkeypatch)
    monkeypatch.setattr(provider, "launch", AsyncMock(side_effect=ProviderError(DENIED)))

    assert not await supervisor._finish_launch(item, provider)

    assert item.attempt.exit_class is ExitClass.ENVIRONMENT
    assert item.attempt.state is AttemptState.COLLECTED
    assert item.attempt.termination_detail is not None
    assert item.attempt.termination_detail.startswith("launch: ")
    assert 'webhook "policy.lab" denied the request' in item.attempt.termination_detail
    finish = supervisor._classify_and_finish
    finish.assert_called_once()
    summary = finish.call_args.kwargs["wake_summary"]
    assert summary.startswith("attempt 1 ended environment at launch: ")
    assert 'webhook "policy.lab" denied the request' in summary
    assert provider.discarded == [item.attempt.id]


def test_the_failure_reason_reaches_the_wake_summary() -> None:
    """`_classify_and_finish` hands the summary to `_task_reported`, which writes the
    wake when no retry remains."""
    supervisor = Supervisor(
        MagicMock(), {}, FakeClock(NOW), holder="test", artifact_store=MagicMock()
    )
    task = Task(
        "task", "FDY-0441", "foundry", "p", "t", TaskState.RUNNING, 1, "policy", 1, "repo", NOW, NOW
    )
    execution = Execution(
        "execution",
        task.id,
        ExecutionRole.IMPLEMENT,
        1,
        "codex",
        "m",
        None,
        "kubernetes",
        "img",
        {},
        ExecutionState.ACTIVE,
        1,
        ["environment"],
        60,
        NOW,
    )
    attempt = Attempt(
        "attempt",
        execution.id,
        task.id,
        1,
        AttemptState.COLLECTED,
        NOW,
        exit_class=ExitClass.ENVIRONMENT,
        termination_detail=f"launch: {DENIED}",
    )
    uow: Any = MagicMock()
    uow.executions.get.return_value = execution
    uow.tasks.get.return_value = task
    uow.attempts.list_for_execution.return_value = [attempt]
    uow.contracts.get.return_value = SimpleNamespace(
        document={"execution_request": {"tier": "complex"}}
    )
    uow.leases.list_checkout_leases.return_value = []
    with (
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(supervisor, "_local_cap", lambda *_: None)
        patch.setattr(supervisor, "_fenced", lambda: nullcontext(uow))
        patch.setattr(supervisor, "_uow_factory", lambda: nullcontext(uow))
        supervisor._classify_and_finish(
            uow, attempt, None, wake_summary=f"attempt 1 ended environment at launch: {DENIED}"
        )
    assert task.state is TaskState.REPORTED
    wake = uow.wakes.add.call_args.args[0]
    assert 'webhook "policy.lab" denied the request' in wake.payload["summary"]


# ----- the admin surfaces show the reservation and the capacity -----------------------


async def test_the_kubernetes_admin_view_shows_headroom_reservation_and_capacity() -> None:
    api, _registry, provider = build(config=_config())
    _quota(api, LAB_QUOTA)
    provider._last_limits = k8sspec.limits_from_policy(THREE_CPU_POLICY)
    ctx: Any = SimpleNamespace(providers={"kubernetes": provider})

    view = await kubernetes_admin.capacity_view(ctx)

    assert view["provider_enabled"] is True
    assert view["quota_headroom"] == 10
    assert view["short_role_pods_reserved"] == 1
    assert view["worker_capacity"] == 9
    assert _capacity_words(view) == (
        "9 worker(s) at once: the quota admits 10 Pod(s), 1 kept for short-role Pods"
    )


async def test_the_admin_view_without_the_provider_or_a_quota() -> None:
    assert await kubernetes_admin.capacity_view(SimpleNamespace(providers={})) == {  # type: ignore[arg-type]
        "provider_enabled": False
    }
    assert _capacity_words({"provider_enabled": False}).startswith("not in use")
    _api, _registry, provider = build(config=_config(max_concurrency=3))
    view = await kubernetes_admin.capacity_view(
        SimpleNamespace(providers={"kubernetes": provider})  # type: ignore[arg-type]
    )
    assert view["worker_capacity"] == 3 and view["quota_headroom"] is None
    assert "kubernetes.max_concurrency" in _capacity_words(view)


def test_the_capacity_record_names_what_the_pages_show() -> None:
    capacity = WorkerCapacity(
        workers=9, source="quota", headroom=10, reserved_pods=1, reservation={"pods": 1}
    )
    assert capacity.as_dict() == {
        "worker_capacity": 9,
        "capacity_source": "quota",
        "quota_headroom": 10,
        "short_role_pods_reserved": 1,
        "short_role_reservation": {"pods": 1},
        "capacity_detail": "",
    }
    assert replace(capacity, workers=8).workers == 8
