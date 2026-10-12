"""Issue 361: a finished attempt survives slow Kubernetes garbage collection."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution import kubernetes as kubernetes_module
from crucible.adapters.execution.k8sapi import KubernetesApiError, KubernetesUnavailableError
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.execution.kubernetes import KubernetesProvider
from crucible.application import supervisor as supervisor_module
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import Attempt, ExecutionRole
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState
from crucible.ports.execution import (
    CleanupPolicy,
    CollectionPendingError,
    Handle,
    LaunchSpec,
    ObservationState,
    ProviderError,
    Workspace,
)
from crucible.settings import SupervisorSettings
from tests.fixtures import FakeClock
from tests.unit.kubernetes_fixtures import build, spec

NOW = datetime(2026, 10, 2, tzinfo=UTC)


@dataclass
class DelayedPods:
    """Keep real fake-API Pod objects after delete, for a deterministic poll budget."""

    api: FakeKubernetesApi
    role: str = k8sspec.ROLE_VERIFIER
    polls: int = 20
    grace: int = 30
    unavailable_at: int | None = 2
    elapsed: float = 0.0
    observed: int = 0
    pending: dict[str, int] = field(default_factory=dict)
    deletes: list[tuple[str, str, Mapping[str, Any]]] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_delete = self.api.delete
        real_list = self.api.list_objects
        real_get = self.api.get

        def role_of(body: Mapping[str, Any]) -> str:
            return str(body.get("metadata", {}).get("labels", {}).get(k8sspec.LABEL_ROLE, ""))

        def delayed_delete(kind: str, name: str, **kwargs: Any) -> None:
            self.deletes.append((kind, name, kwargs))
            kept = {
                key: obj
                for key, obj in self.api.objects.items()
                if key[0] == "pods"
                and role_of(obj.body) == self.role
                and (
                    (kind == "jobs" and obj.body["metadata"]["labels"].get("job-name") == name)
                    or (kind == "pods" and key[1] == name)
                )
            }
            real_delete(kind, name, **kwargs)
            for key, obj in kept.items():
                obj.body.setdefault("spec", {})["terminationGracePeriodSeconds"] = self.grace
                obj.body["metadata"]["deletionTimestamp"] = NOW.isoformat()
                self.pending.setdefault(key[1], self.polls)
                self.api.objects[key] = obj

        def advance(names: list[str]) -> None:
            if not names:
                return
            self.elapsed += 1
            self.observed += 1
            if self.observed == self.unavailable_at:
                raise KubernetesUnavailableError(503, "one missed deletion poll")
            for name in names:
                self.pending[name] -= 1
                if self.pending[name] <= 0:
                    real_delete("pods", name)
                    del self.pending[name]

        def delayed_list(kind: str, **kwargs: Any) -> list[dict[str, Any]]:
            rows = real_list(kind, **kwargs)
            if kind == "pods":
                advance(
                    [
                        str(row["metadata"]["name"])
                        for row in rows
                        if row["metadata"]["name"] in self.pending
                    ]
                )
                rows = real_list(kind, **kwargs)
            return rows

        def delayed_get(kind: str, name: str) -> dict[str, Any]:
            if kind == "pods" and name in self.pending:
                advance([name])
            return real_get(kind, name)

        monkeypatch.setattr(self.api, "delete", delayed_delete)
        monkeypatch.setattr(self.api, "list_objects", delayed_list)
        monkeypatch.setattr(self.api, "get", delayed_get)
        # Patch only this module's clock, leaving asyncio's real clock untouched.
        monkeypatch.setattr(
            kubernetes_module, "time", SimpleNamespace(monotonic=lambda: self.elapsed)
        )


async def finished_worker() -> tuple[
    FakeKubernetesApi, KubernetesProvider, LaunchSpec, Workspace, Handle
]:
    api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    while (await provider.observe(handle)).state is ObservationState.RUNNING:
        pass
    return api, provider, launch, workspace, handle


def supervisor_for(
    monkeypatch: pytest.MonkeyPatch,
    provider: KubernetesProvider,
    launch: LaunchSpec,
    workspace: Workspace,
    handle: Handle,
    *,
    retry_ticks: int = 5,
) -> tuple[Supervisor, Attempt, MagicMock]:
    """Exercise the supervisor's real observe/collection tick; mock persistence only."""
    attempt = Attempt(
        launch.attempt_id,
        "execution",
        launch.task_id,
        1,
        AttemptState.RUNNING,
        NOW,
        handle=handle.ref,
        logs_drained_at=NOW,
    )
    supervisor = Supervisor(
        MagicMock(),
        {provider.name: provider},
        FakeClock(NOW),
        holder="test",
        artifact_store=MagicMock(),
        collection_retry_ticks=retry_ticks,
    )
    supervisor._handles[attempt.id] = handle
    supervisor._workspaces[attempt.id] = workspace
    monkeypatch.setattr(supervisor, "_execution_provider_name", lambda _: provider.name)
    monkeypatch.setattr(supervisor, "_spec_for", AsyncMock(return_value=launch))
    monkeypatch.setattr(
        supervisor, "_list_live", lambda: [attempt] if attempt.state is AttemptState.RUNNING else []
    )
    monkeypatch.setattr(supervisor, "_quota_checkpoint_pending", lambda _: False)

    uow = MagicMock()
    uow.attempts.get.return_value = attempt
    uow.tasks.get.return_value = SimpleNamespace(id=launch.task_id, external_id=launch.external_id)
    uow.executions.get.return_value = SimpleNamespace(
        role=ExecutionRole.IMPLEMENT, harness=launch.harness
    )
    uow.claims.get.return_value = None
    monkeypatch.setattr(supervisor, "_fenced", lambda: nullcontext(uow))
    monkeypatch.setattr(supervisor, "_record_credential_sync", MagicMock())
    monkeypatch.setattr(supervisor, "_record_wall_time", MagicMock())
    monkeypatch.setattr(supervisor, "_classify_and_finish", MagicMock())
    monkeypatch.setattr(
        supervisor_module, "record_collection_evidence", MagicMock(return_value=None)
    )
    finish = MagicMock(wraps=supervisor._finish_exited)
    monkeypatch.setattr(supervisor, "_finish_exited", finish)
    return supervisor, attempt, finish


async def test_finished_attempt_waits_past_fifteen_seconds_and_an_api_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, provider, launch, workspace, handle = await finished_worker()
    delayed = DelayedPods(api, role=k8sspec.ROLE_VERIFIER)
    delayed.install(monkeypatch)
    state = AsyncMock(wraps=provider._workspace_state)
    monkeypatch.setattr(provider, "_workspace_state", state)
    supervisor, attempt, finish = supervisor_for(monkeypatch, provider, launch, workspace, handle)

    await supervisor._observe_attempts()
    assert attempt.state is AttemptState.COLLECTED
    assert attempt.exit_class is ExitClass.COMPLETED
    finish.assert_called_once()
    outputs = finish.call_args.args[2]
    assert finish.call_args.args[3] is None  # No environment failure at the persistence boundary.
    assert outputs.report is not None
    assert outputs.verifications and all(check.exit_code == 0 for check in outputs.verifications)
    assert outputs.workspace_state.checked and not outputs.workspace_state.leftover
    assert not delayed.pending
    state.assert_awaited_once()
    assert delayed.observed > 15
    await supervisor._observe_attempts()
    finish.assert_called_once()
    await provider.cleanup(workspace, CleanupPolicy.DELETE, launch)
    assert not api.list_objects("pods")
    assert not api.list_objects("jobs")
    await supervisor.stop()


async def test_verifier_delete_sends_zero_grace_and_keeps_background_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, provider, launch, workspace, handle = await finished_worker()
    delayed = DelayedPods(api)
    delayed.install(monkeypatch)
    await provider.collect(handle, workspace, launch)
    job = k8sspec.object_name("verifier", launch.attempt_id)
    requests = [(kind, kwargs) for kind, name, kwargs in delayed.deletes if name.startswith(job)]
    assert {kind for kind, _ in requests} == {"jobs", "pods"}
    assert all(kwargs.get("grace_period_seconds") == 0 for _, kwargs in requests)
    assert all(kwargs.get("propagation", "Background") == "Background" for _, kwargs in requests)


async def test_collection_resumes_on_next_tick_without_rerunning_finished_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, provider, launch, workspace, handle = await finished_worker()
    delayed = DelayedPods(api, polls=45)
    delayed.install(monkeypatch)
    state = AsyncMock(wraps=provider._workspace_state)
    monkeypatch.setattr(provider, "_workspace_state", state)
    supervisor, attempt, finish = supervisor_for(monkeypatch, provider, launch, workspace, handle)

    await supervisor._observe_attempts()
    assert attempt.state is AttemptState.RUNNING
    finish.assert_not_called()
    state.assert_not_awaited()
    assert delayed.pending
    assert api.claims

    await supervisor._observe_attempts()
    finish.assert_called_once()
    assert finish.call_args.args[3] is None
    assert finish.call_args.args[2].report is not None
    state.assert_awaited_once()
    for role in ("verifier",):
        assert (
            len(
                [
                    row
                    for row in api.created
                    if row["kind"] == "jobs" and row["name"].startswith(role)
                ]
            )
            == 1
        )
    await supervisor._observe_attempts()
    finish.assert_called_once()
    assert not supervisor._collect_pending_ticks
    await provider.cleanup(workspace, CleanupPolicy.DELETE, launch)
    assert not api.list_objects("pods")
    assert not api.list_objects("jobs")
    assert not provider._collection_role_exits
    await supervisor.stop()


@pytest.mark.parametrize("retry_ticks", [1, 3])
async def test_collection_retry_ticks_are_bounded_and_preserve_the_reason(
    monkeypatch: pytest.MonkeyPatch,
    retry_ticks: int,
) -> None:
    api, provider, launch, workspace, handle = await finished_worker()
    delayed = DelayedPods(api, polls=1000)
    delayed.install(monkeypatch)
    state = AsyncMock(wraps=provider._workspace_state)
    monkeypatch.setattr(provider, "_workspace_state", state)
    supervisor, attempt, finish = supervisor_for(
        monkeypatch,
        provider,
        launch,
        workspace,
        handle,
        retry_ticks=retry_ticks,
    )
    for _ in range(retry_ticks - 1):
        await supervisor._observe_attempts()
        finish.assert_not_called()
    await supervisor._observe_attempts()
    finish.assert_called_once()
    assert attempt.exit_class is ExitClass.ENVIRONMENT
    reason = finish.call_args.args[3]
    assert "Pods for Job 'verifier-" in reason
    assert "still present" in reason
    assert f"{retry_ticks} collection ticks" in reason
    assert finish.call_args.args[2].report is None
    state.assert_not_awaited()
    await supervisor._observe_attempts()
    finish.assert_called_once()
    assert not supervisor._collect_pending_ticks
    # Cleanup does not wait on the lingering Pod: it asks for it to go and moves on.
    await provider.cleanup(workspace, CleanupPolicy.DELETE, launch)
    assert not api.list_objects("jobs")
    assert all(row["metadata"].get("deletionTimestamp") for row in api.list_objects("pods"))
    await supervisor.stop()


@pytest.mark.parametrize("job_wait", [False, True])
async def test_wait_uses_the_observed_pod_grace_period_plus_a_margin(
    monkeypatch: pytest.MonkeyPatch,
    job_wait: bool,
) -> None:
    api, _registry, provider = build()
    api.create(
        "pods",
        {
            "metadata": {
                "name": "long-grace",
                "labels": {
                    "job-name": "long-grace-job",
                    k8sspec.LABEL_ROLE: k8sspec.ROLE_VERIFIER,
                },
            }
        },
    )
    delayed = DelayedPods(api, polls=1000, grace=60)
    delayed.install(monkeypatch)
    api.delete("pods", "long-grace")
    with pytest.raises(CollectionPendingError, match="65 seconds"):
        if job_wait:
            await provider._await_job_pods_gone("long-grace-job", collection=True)
        else:
            await provider._await_pod_gone("long-grace", collection=True)
    assert delayed.elapsed == 65


def test_collection_retry_setting_requires_a_positive_tick_count() -> None:
    assert SupervisorSettings(collection_retry_ticks=7).collection_retry_ticks == 7
    with pytest.raises(ValidationError):
        SupervisorSettings(collection_retry_ticks=0)


@pytest.mark.parametrize("job_wait", [False, True])
async def test_an_unavailable_api_never_confirms_deletion(
    monkeypatch: pytest.MonkeyPatch,
    job_wait: bool,
) -> None:
    api, _registry, provider = build()
    elapsed = 0.0

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        nonlocal elapsed
        elapsed += 1
        raise KubernetesUnavailableError(503, "API down")

    monkeypatch.setattr(api, "get", unavailable)
    monkeypatch.setattr(api, "list_objects", unavailable)
    monkeypatch.setattr(kubernetes_module, "time", SimpleNamespace(monotonic=lambda: elapsed))
    with pytest.raises(CollectionPendingError, match="API was unavailable"):
        if job_wait:
            await provider._await_job_pods_gone("unreachable-job", collection=True)
        else:
            await provider._await_pod_gone("unreachable-pod", collection=True)
    assert elapsed == 35


async def test_ticks_do_not_start_a_second_collection_while_deletion_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, provider, launch, workspace, handle = await finished_worker()
    supervisor, attempt, finish = supervisor_for(monkeypatch, provider, launch, workspace, handle)
    supervisor.collect_wait_seconds = 0.01
    entered = asyncio.Event()
    release = asyncio.Event()
    wait_for_deletion = provider._await_job_pods_gone

    async def held_deletion(
        name: str, *, timeout: float = 15, force: bool = False, collection: bool = False
    ) -> None:
        if name.startswith("verifier") and any(
            row["kind"] == "jobs" and row["name"] == name for row in api.created
        ):
            entered.set()
            await release.wait()
        await wait_for_deletion(name, timeout=timeout, force=force, collection=collection)

    monkeypatch.setattr(provider, "_await_job_pods_gone", held_deletion)
    collection = AsyncMock(wraps=provider.collect)
    monkeypatch.setattr(provider, "collect", collection)
    try:
        await supervisor._observe_attempts()
        await asyncio.wait_for(entered.wait(), timeout=1)
        in_flight = supervisor._collects[attempt.id]
        await supervisor._observe_attempts()
        assert supervisor._collects[attempt.id] is in_flight
        collection.assert_awaited_once()
        finish.assert_not_called()
        release.set()
        await asyncio.wait_for(in_flight, timeout=5)
        await supervisor._observe_attempts()
        finish.assert_called_once()
        collection.assert_awaited_once()
        assert attempt.exit_class is ExitClass.COMPLETED
        await provider.cleanup(workspace, CleanupPolicy.DELETE, launch)
        assert not api.list_objects("pods")
        assert not api.list_objects("jobs")
    finally:
        await supervisor.stop()


async def test_cleanup_never_waits_on_a_collector_pod_that_ignores_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, provider, launch, workspace, _handle = await finished_worker()
    collector = k8sspec.object_name("collect", launch.attempt_id)
    secret = k8sspec.object_name("cred", launch.attempt_id)
    labels = {k8sspec.LABEL_ATTEMPT: launch.attempt_id}
    api.create("secrets", {"metadata": {"name": secret, "labels": labels}})
    api.create(
        "pods",
        {
            "metadata": {
                "name": f"{collector}-stuck",
                "labels": {
                    **labels,
                    "job-name": collector,
                    k8sspec.LABEL_ROLE: k8sspec.ROLE_COLLECTOR,
                },
            }
        },
    )
    assert api.list_objects("jobs")
    delayed = DelayedPods(api, role=k8sspec.ROLE_COLLECTOR, polls=1000, unavailable_at=None)
    delayed.install(monkeypatch)

    await asyncio.wait_for(provider.cleanup(workspace, CleanupPolicy.DELETE, launch), timeout=5)

    assert not api.secret_exists(secret)
    assert not api.list_objects("jobs")
    assert not api.list_objects("networkpolicies")
    assert not api.claims
    # The Pod was asked to go and is Terminating; cleanup did not wait it out.
    assert [row["metadata"]["name"] for row in api.list_objects("pods")] == [f"{collector}-stuck"]
    assert delayed.elapsed < 5


async def test_a_real_read_error_is_not_replaced_while_reading_worker_scratch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api, provider, launch, _workspace, _handle = await finished_worker()

    class DiskFullError(OSError):
        pass

    with pytest.raises(DiskFullError):
        async with provider._reader(launch, k8sspec.limits_from_policy({})):
            raise DiskFullError("no space left on device")


async def test_collection_cleanup_logs_a_refused_delete_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, provider, launch, _workspace, _handle = await finished_worker()
    real_delete = api.delete

    def refusing_delete(kind: str, name: str, **kwargs: Any) -> None:
        if kind == "jobs":
            raise KubernetesApiError(403, "forbidden")
        real_delete(kind, name, **kwargs)

    monkeypatch.setattr(api, "delete", refusing_delete)
    await provider._clear_collection_pods(launch.attempt_id)


@pytest.mark.parametrize("job_wait", [False, True])
async def test_other_pods_keep_the_short_wait_and_fail_as_environment(
    monkeypatch: pytest.MonkeyPatch,
    job_wait: bool,
) -> None:
    api, _registry, provider = build()
    api.create("pods", {"metadata": {"name": "preparer", "labels": {"job-name": "prepare-job"}}})
    elapsed = 0.0

    def tick() -> float:
        nonlocal elapsed
        elapsed += 1
        return elapsed

    monkeypatch.setattr(kubernetes_module, "time", SimpleNamespace(monotonic=tick))
    with pytest.raises(ProviderError, match="still present after 15 seconds") as raised:
        if job_wait:
            await provider._await_job_pods_gone("prepare-job")
        else:
            await provider._await_pod_gone("preparer")
    assert not isinstance(raised.value, CollectionPendingError)
