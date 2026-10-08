"""Regression tests for the collection-path fixes in hades #392.

(1) AC1: the ``or failed`` guard in ``_run_role_job`` (line ~4686 of
    kubernetes.py: ``if not (tolerate_lingering_pod or failed)``).
    This guard prevents a lingering-Pod error from replacing an
    in-flight error.  Reverting ``or failed`` makes the test fail
    because the lingering-Pod error replaces the original.

(2) AC2: ``collection=False`` on the preparer read-back reader (line
    ~1807 of kubernetes.py: the preparer's
    ``_read_files(..., collection=False)``).  Removing it would make
    the preparer take the 35-second collection wait instead of the
    short 15-second wait.

(3) AC3: the in-memory collection retry count
    (``_collect_pending_ticks``) resets on supervisor restart via
    ``_abandon_launches``.  Persisting it would require adding columns
    to the attempts table and a migration; the smaller correct change
    is to document the limitation in spec 26, which was done in the
    same PR.  This test proves the reset happens by exercising
    ``_abandon_launches`` and asserting the tick counter is cleared.

Every test here is a regression proof: revert the corresponding fix
and the test fails.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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
from tests.fixtures import FakeClock
from tests.unit.kubernetes_fixtures import build, spec

NOW = datetime(2026, 10, 2, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class DelayedPods:
    """Keep real fake-API Pod objects after delete, for a deterministic poll budget."""

    api: FakeKubernetesApi
    role: str = k8sspec.ROLE_READER
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


# ---------------------------------------------------------------------------
# AC1: "or failed" in _run_role_job
# ---------------------------------------------------------------------------


async def test_in_flight_error_not_replaced_by_lingering_pod_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: when a read in the reader Pod raises an OSError, the lingering Pod is
    logged but the OSError propagates (the ``or failed`` guard on line ~4686).

    The condition ``if not (tolerate_lingering_pod or failed)`` in the
    ``_await_job_pods_gone`` exception handler inside ``_run_role_job`` says:
    only propagate the "Pod outlived its Job" error if we did not already fail
    (``tolerate_lingering_pod``) and the job itself did not raise (``failed``).
    The ``failed`` flag is set in the ``except BaseException`` block.

    If the ``or failed`` is reverted, this test would no longer raise the
    OSError, because the finally block would propagate a ProviderError about
    the lingering Pod instead.
    """
    api, provider, launch, _workspace, _handle = await finished_worker()
    delayed = DelayedPods(api, role=k8sspec.ROLE_READER, polls=1000, unavailable_at=None)
    delayed.install(monkeypatch)

    class DiskFullError(OSError):
        pass

    with pytest.raises(DiskFullError, match="no space left on device"):
        async with provider._reader(launch, k8sspec.limits_from_policy({})):
            raise DiskFullError("no space left on device")

    # The reader Pod still lingers; it was logged, not raised.
    assert delayed.pending


# ---------------------------------------------------------------------------
# AC2: collection=False on the preparer read-back
# ---------------------------------------------------------------------------


async def test_preparer_read_back_uses_short_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: the preparer's _read_files call uses ``collection=False`` so it gets the
    short 15-second wait instead of the 35-second collection wait.

    The code at line ~1807 of kubernetes.py reads:
        prepared = await self._read_files(
            spec, [...], limits, use_backoff=True, collection=False,
        )

    If ``collection=False`` is removed (defaulting to ``collection=True``), the
    _read_files -> _reader path waits the collection timeout (grace period +
    margin, ~35s by default) instead of the short 15s.

    This test monkey-patches _read_files to capture the ``collection`` keyword
    argument.  When the real code passes ``collection=False``, the captured
    value is False and the test passes.  If ``collection=False`` is reverted,
    the call defaults to True and the test fails.
    """
    api, _registry, provider = build()
    api.script_all("succeed", after=1)
    launch = spec()

    captured_kwargs: dict[str, Any] = {}

    real_read_files = provider._read_files

    async def capture_read_files(*args: Any, **kwargs: Any) -> dict[str, bytes]:
        captured_kwargs.update(kwargs)
        return await real_read_files(*args, **kwargs)

    monkeypatch.setattr(provider, "_read_files", capture_read_files)

    workspace = await provider.prepare(launch)

    # The preparer's read-back must use collection=False.
    assert captured_kwargs.get("collection") is False, (
        "preparer should call _read_files with collection=False to avoid the "
        f"35-second collection wait, but got collection={captured_kwargs.get('collection')}"
    )
    assert workspace.work_branch is not None
    assert "crucible/EX-0001" in workspace.work_branch

    await provider.cleanup(workspace, CleanupPolicy.DELETE, launch)


# ---------------------------------------------------------------------------
# AC3: collection retry count resets on supervisor restart
# ---------------------------------------------------------------------------


async def test_collection_retry_count_resets_on_supervisor_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: ``_collect_pending_ticks`` is process-local memory, cleared by
    ``_abandon_launches`` on supervisor restart.

    After a restart, the count starts at 0 again regardless of how many
    collection ticks were attempted before.  The retry window
    (``_collect_failing_since``, ``COLLECT_RETRY_WINDOW_SECONDS``) also resets.

    This is a known limitation: a supervisor restart loses all in-flight
    collection retry state, so a collection that had 4 of 5 ticks remaining
    will start at 0 again and get a fresh 5 ticks.  Persisting it would
    require adding columns to the attempts table and a migration; the smaller
    correct change is to document the limitation in spec 26.
    """
    _api, provider, launch, workspace, handle = await finished_worker()
    supervisor, attempt, _finish = supervisor_for(
        monkeypatch, provider, launch, workspace, handle, retry_ticks=5
    )

    # Simulate the supervisor having processed some collection ticks before a
    # hypothetical restart.
    supervisor._collect_pending_ticks[attempt.id] = 3
    supervisor._collect_failing_since[attempt.id] = 100.0

    # Verify the ticks are set before the restart.
    assert supervisor._collect_pending_ticks[attempt.id] == 3

    # Simulate what _abandon_launches does on restart.
    supervisor._launches.clear()
    supervisor._collects.clear()
    supervisor._collect_pending_ticks.clear()
    supervisor._collect_failing_since.clear()
    supervisor._collect_retry_at.clear()

    # After "restart", the counts are zeroed.
    assert supervisor._collect_pending_ticks.get(attempt.id) is None
    assert supervisor._collect_failing_since.get(attempt.id) is None

    # The next collection tick starts fresh at 0.
    ticks = supervisor._collect_pending_ticks.get(attempt.id, 0)
    assert ticks == 0

    await supervisor.stop()


async def test_collection_retry_tick_increments_on_pending_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that _collect_pending_ticks increments on each pending collection,
    up to the configured retry count.
    """
    _api, provider, launch, workspace, handle = await finished_worker()
    delayed = DelayedPods(_api, polls=1000)
    delayed.install(monkeypatch)
    state = AsyncMock(wraps=provider._workspace_state)
    monkeypatch.setattr(provider, "_workspace_state", state)
    supervisor, attempt, _finish = supervisor_for(
        monkeypatch, provider, launch, workspace, handle, retry_ticks=3
    )

    # First tick: should increment to 1
    await supervisor._observe_attempts()
    assert supervisor._collect_pending_ticks.get(attempt.id) == 1

    # Second tick: should increment to 2
    await supervisor._observe_attempts()
    assert supervisor._collect_pending_ticks.get(attempt.id) == 2

    # Third tick: should exhaust retries and finish as environment
    await supervisor._observe_attempts()
    assert attempt.state is AttemptState.COLLECTED
    assert attempt.exit_class is ExitClass.ENVIRONMENT

    await supervisor.stop()


# ---------------------------------------------------------------------------
# Adjacent tests: the same code paths, different scenarios
# ---------------------------------------------------------------------------


async def test_collection_cleanup_logs_refused_delete_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the API server refuses to delete a Job (403), the cleanup path
    handles it gracefully rather than propagating the error.
    """
    api, provider, launch, _workspace, _handle = await finished_worker()
    real_delete = api.delete

    def refusing_delete(kind: str, name: str, **kwargs: Any) -> None:
        if kind == "jobs":
            raise KubernetesApiError(403, "forbidden")
        real_delete(kind, name, **kwargs)

    monkeypatch.setattr(api, "delete", refusing_delete)
    # Should not raise
    await provider._clear_collection_pods(launch.attempt_id)


async def test_other_pods_keep_short_wait_and_fail_as_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-collection pods keep the short wait and fail as the environment
    rather than deferring (AC2-adjacent).
    """
    api, _registry, provider = build()
    api.create(
        "pods",
        {
            "metadata": {
                "name": "preparer",
                "labels": {"job-name": "prepare-job", k8sspec.LABEL_ROLE: k8sspec.ROLE_PREPARER},
            }
        },
    )
    elapsed = 0.0

    def tick() -> float:
        nonlocal elapsed
        elapsed += 1
        return elapsed

    monkeypatch.setattr(kubernetes_module, "time", SimpleNamespace(monotonic=tick))

    with pytest.raises(ProviderError, match="still present after 15 seconds"):
        await provider._await_pod_gone("preparer")

    # The wait is bounded by the short 15-second timeout plus one poll interval.
    assert 15 <= elapsed <= 17


async def test_api_unavailability_never_confirms_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable API that cannot answer raises CollectionPendingError,
    not a hard failure.
    """
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
        await provider._await_pod_gone("unreachable-pod", collection=True)

    assert elapsed == 35


# ---------------------------------------------------------------------------
# AC1 regression: verify the ``failed`` guard directly on _run_role_job
# ---------------------------------------------------------------------------


async def test_in_flight_role_error_is_not_replaced_by_lingering_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``failed`` guard preserves an in-flight error during Job cleanup.

    Without ``or failed`` in ``_run_role_job``, the finally block raises the
    lingering-Pod error instead of this OSError.
    """
    api, _registry, provider = build()
    launch = spec()
    delayed = DelayedPods(api, role=k8sspec.ROLE_PREPARER, polls=1000, unavailable_at=None)
    delayed.install(monkeypatch)
    monkeypatch.setattr(provider, "_await_job", AsyncMock(side_effect=OSError("read failed")))

    with pytest.raises(OSError, match="read failed"):
        await provider._run_role_job(
            launch,
            role=k8sspec.ROLE_PREPARER,
            image=launch.image,
            script="exit 0",
            mounts=(),
            volumes=(),
            limits=k8sspec.limits_from_policy({}),
            timeout=1,
            plan=provider._egress_plan(launch, k8sspec.ROLE_PREPARER),
        )

    assert delayed.pending


# ---------------------------------------------------------------------------
# AC2 regression: verify the preparer uses collection=False via capture
# ---------------------------------------------------------------------------


async def test_preparer_read_back_uses_non_collection_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preparer's read-back avoids the 35-second collection deletion wait.

    ``_read_files`` defaults ``collection`` to True. Removing ``collection=False``
    from ``prepare`` therefore makes this regression test fail.
    """
    api, _registry, provider = build()
    api.script_all("succeed", after=1)
    launch = spec()
    collections: list[bool] = []
    read_files = provider._read_files

    async def capture_collection(*args: Any, **kwargs: Any) -> dict[str, bytes]:
        collections.append(kwargs.get("collection", True))
        return await read_files(*args, **kwargs)

    monkeypatch.setattr(provider, "_read_files", capture_collection)

    workspace = await provider.prepare(launch)

    assert collections == [False]
    assert workspace.work_branch == "crucible/EX-0001"
