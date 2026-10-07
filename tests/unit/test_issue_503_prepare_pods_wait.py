"""hades #503: a preparer Job whose pods are slow to clear is retried with
backoff, keeps the previous bundle, and costs no attempt."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import MethodType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo's source package is importable.
# ---------------------------------------------------------------------------
_repo = Path(__file__).resolve().parents[3]
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from crucible.adapters.execution.kubernetes import (  # noqa: E402  # isort: skip
    KubernetesConfig,
    KubernetesProvider,
    PrepareJobPodsTimeoutError,
)
from crucible.domain.entities import (  # noqa: E402  # isort: skip
    Attempt,
    Execution,
    ExecutionRole,
    ExecutionState,
    Task,
)
from crucible.domain.lifecycle import (  # noqa: E402  # isort: skip
    AttemptState,
    TaskState,
)
from tests.fixtures import FakeClock, UTC  # noqa: E402  # isort: skip


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

from datetime import datetime  # noqa: E402  # isort: skip

_TASK_ID = "task-1"
_EXEC_ID = "exec-1"


def _make_task(state: TaskState) -> Task:
    return Task(
        id=_TASK_ID,
        external_id="EX-0001",
        principal_id="p-01",
        project="example",
        title="Do a thing",
        state=state,
        contract_version=1,
        policy_name="default-software",
        policy_version=2,
        repository_id="repo-1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        head_sha="abcdef0123456789",
    )


def _make_execution(
    task_id: str = _TASK_ID,
    state: ExecutionState = ExecutionState.ACTIVE,
) -> Execution:
    return Execution(
        id=_EXEC_ID,
        task_id=task_id,
        role=ExecutionRole.IMPLEMENT,
        contract_version=1,
        harness="script-harness",
        model="gpt-5.6-sol",
        effort="high",
        provider="kubernetes",
        image="ghcr.io/example/worker:latest",
        policy_snapshot={"limits": {"publish_retry_max": 3}},
        state=state,
        max_attempts=5,
        retry_on=["environment", "resource_exhausted"],
        timeout_seconds=900,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=None,
    )


def _make_provider(
    *,
    wait_seconds: float = 15.0,
    prepare_timeout: int = 900,
) -> MagicMock:
    """Return a provider stubbed so that list_objects returns *pods*
    until a reset clears them (used for the backoff-retry tests)."""
    config = KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        prepare_pod_deletion_wait_seconds=wait_seconds,
        prepare_timeout_seconds=prepare_timeout,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
    )

    prov = MagicMock()
    prov.config = config
    prov.client = MagicMock()
    prov._call = MagicMock()

    # Bind the real method to the stub instance so *self* is the mock.
    prov._await_preparer_job_pods_gone = MethodType(
        KubernetesProvider._await_preparer_job_pods_gone, prov
    )
    return prov


# ---------------------------------------------------------------------------
# AC1 – backoff retry succeeds, pods clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac1_pods_clear_after_two_retries() -> None:
    """A preparer whose pods clear during a backoff retry finishes prepare
    without raising PrepareJobPodsTimeoutError.  The attempt goes to pending
    and eventually launches."""
    pods = [{"metadata": {"name": "preparer-pod-1"}}]
    prov = _make_provider(wait_seconds=8.0, prepare_timeout=900)

    call_count = 0

    async def fake_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if "list_objects" in str(args):
            if call_count <= 3:
                return pods
            return None
        return None

    prov._call = fake_call

    await prov._await_preparer_job_pods_gone(
        "test-job-1", job_completed=True, job_completed_reason=None
    )
    # initial check + 2 retries + final clear check = 4 calls
    assert call_count == 4


@pytest.mark.asyncio
async def test_ac1_pods_still_present_times_out() -> None:
    """When pods never clear before the timeout, a
    PrepareJobPodsTimeoutError is raised."""
    prov = _make_provider(wait_seconds=3.0, prepare_timeout=900)

    async def always_pods(*args, **kwargs):
        if "list_objects" in str(args):
            return [{"metadata": {"name": "stuck-pod"}}]
        return None

    prov._call = always_pods

    with pytest.raises(PrepareJobPodsTimeoutError) as exc_info:
        await prov._await_preparer_job_pods_gone(
            "stuck-job",
            job_completed=True,
            job_completed_reason=None,
        )

    msg = str(exc_info.value)
    assert "were still present after" in msg
    assert "the Job had completed" in msg


@pytest.mark.asyncio
async def test_ac1_job_still_running_reason_in_message() -> None:
    """The error message states the Job was still running with a reason."""
    prov = _make_provider(wait_seconds=2.0, prepare_timeout=900)

    async def always_pods(*args, **kwargs):
        if "list_objects" in str(args):
            return [{"metadata": {"name": "running-job-pod"}}]
        return None

    prov._call = always_pods

    with pytest.raises(PrepareJobPodsTimeoutError) as exc_info:
        await prov._await_preparer_job_pods_gone(
            "running-job",
            job_completed=False,
            job_completed_reason="OOMKilled",
        )

    msg = str(exc_info.value)
    assert "the Job was still running (reason: OOMKilled)" in msg


@pytest.mark.asyncio
async def test_ac1_unavailable_transient_is_ignored() -> None:
    """A KubernetesUnavailableError during list is silently ignored and the
    loop continues (the provider retries)."""
    from crucible.adapters.execution.k8sapi import (  # noqa: E402  # isort: skip
        KubernetesUnavailableError,
    )

    prov = _make_provider(wait_seconds=5.0, prepare_timeout=900)
    pods = [{"metadata": {"name": "pod-1"}}]

    call_count = 0

    async def sometimes_unavailable(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if "list_objects" not in str(args):
            return None
        if call_count <= 2:
            raise KubernetesUnavailableError(0, "API unavailable", path="")
        if call_count == 3:
            return pods
        return None

    prov._call = sometimes_unavailable

    async def fake_sleep(seconds):
        pass  # skip actual sleeps

    with patch("asyncio.sleep", fake_sleep):
        await prov._await_preparer_job_pods_gone(
            "unavailable-job", job_completed=True, job_completed_reason=None
        )
    assert call_count >= 3


# ---------------------------------------------------------------------------
# AC2 – previous_attempt_bundle_gone is NOT raised after prepare failure
# ---------------------------------------------------------------------------


def test_ac2_workspace_path_none_lets_corrections_resume() -> None:
    """When the latest work attempt has workspace_path=None (a prepare failure),
    _unpublished_bundle_problem skips it and uses the previous attempt's
    bundle instead, returning None (resumable)."""
    from crucible.application.corrections import (  # noqa: E402  # isort: skip
        _unpublished_bundle_problem,
    )
    from crucible.domain.entities import (  # noqa: E402  # isort: skip
        EvidenceRecord,
    )
    from crucible.domain.lifecycle import AttemptState

    clock = FakeClock()
    task = _make_task(TaskState.RUNNING)

    # The previous (successful) attempt has workspace_path and a bundle_head.
    good_attempt = Attempt(
        id="attempt-good",
        execution_id=_EXEC_ID,
        task_id=task.id,
        number=1,
        workspace_path="/mnt/harness-workspace-abc",
        identity_sha256="abc123",
        state=AttemptState.SUCCEEDED,
        created_at=clock.now(),
        ended_at=clock.now(),
        resume_from_remote=False,
    )

    # The failed (prepare) attempt has workspace_path=None.
    failed_attempt = Attempt(
        id="attempt-failed",
        execution_id=_EXEC_ID,
        task_id=task.id,
        number=2,
        workspace_path=None,
        identity_sha256=None,
        state=AttemptState.PREPARING,
        created_at=clock.now(),
        ended_at=None,
        resume_from_remote=False,
    )

    execution = _make_execution(task_id=task.id)

    retention_mock = MagicMock()
    retention_mock.list_recent.return_value = []  # no workspace release

    uow_mock = MagicMock()
    uow_mock.retention = retention_mock

    def _list_for_task(tid):
        return [good_attempt, failed_attempt] if tid == task.id else []

    def _list_for_execution(eid):
        return [good_attempt, failed_attempt] if eid == _EXEC_ID else []

    uow_mock.attempts.list_for_task = _list_for_task
    uow_mock.attempts.list_for_execution = _list_for_execution
    uow_mock.executions.get = lambda eid: execution if eid == _EXEC_ID else None

    def _list_for_attempt(aid):
        if aid == "attempt-good":
            return [
                EvidenceRecord(
                    id=1,
                    attempt_id=aid,
                    kind="bundle_head",
                    verified=True,
                    payload={"bundle_verified": True, "bundle_sha256": "abc123"},
                    observed_at=clock.now(),
                    source="crucible",
                )
            ]
        return []

    uow_mock.evidence.list_for_attempt = _list_for_attempt

    # Also need to handle latest_work_attempt - it should find the failed_attempt
    from crucible.application.review import latest_work_attempt  # noqa: E402  # isort: skip

    def _mock_latest_work(uow_local, task_local):
        # Return the failed attempt (latest)
        return (failed_attempt, execution)

    with patch(
        "crucible.application.corrections.latest_work_attempt", side_effect=_mock_latest_work
    ):
        result = _unpublished_bundle_problem(
            uow=uow_mock,
            task=task,
            provider="kubernetes",
        )

    # The fix: when workspace_path is None, we skip to the previous
    # attempt's bundle and return None (resumable).
    assert result is None


# ---------------------------------------------------------------------------
# AC3 – prepare failure does NOT consume the attempt budget
# ---------------------------------------------------------------------------


def test_ac3_retry_with_backoff_does_not_count_against_budget() -> None:
    """_retry_with_backoff moves the attempt to PENDING without incrementing
    the exit count.  The budget is preserved."""
    from crucible.application.supervisor import Supervisor  # noqa: E402  # isort: skip

    clock = FakeClock()
    task = _make_task(TaskState.RUNNING)

    attempt = Attempt(
        id="attempt-1",
        execution_id=_EXEC_ID,
        task_id=task.id,
        number=2,
        workspace_path=None,
        identity_sha256=None,
        state=AttemptState.PREPARING,
        created_at=clock.now(),
        ended_at=None,
        resume_from_remote=False,
    )

    execution = _make_execution(task_id=task.id)

    supervisor = Supervisor.__new__(Supervisor)
    supervisor._clock = clock

    uow_mock = MagicMock()
    uow_mock.attempts.get.return_value = attempt
    uow_mock.tasks.get.return_value = task
    uow_mock.executions.get.return_value = execution

    called_save = []

    def fake_commit():
        called_save.append(True)
        assert attempt.state == AttemptState.PENDING

    uow_mock.commit = fake_commit

    supervisor._fenced = MagicMock()
    supervisor._fenced.return_value.__enter__ = (
        lambda self: uow_mock  # type: ignore
    )
    supervisor._fenced.return_value.__exit__ = MagicMock(return_value=None)

    supervisor._release_checkout_leases = MagicMock()

    captured_payload = {}

    def fake_move(uow, clock_local, att, new_state, kind, payload=None):
        att.state = new_state
        captured_payload.update(payload or {})

    with patch("crucible.application.supervisor.move_attempt", side_effect=fake_move):
        with patch(
            "crucible.application.supervisor.move_task",
            return_value=None,
        ):
            supervisor._retry_with_backoff("attempt-1", "prepare", "pods still present")

    assert attempt.state == AttemptState.PENDING
    assert attempt.workspace_path is None
    assert len(called_save) == 1
    assert captured_payload.get("stage") == "prepare"
    assert "pods still present" in captured_payload.get("detail", "")
    assert captured_payload.get("quota_wait") is True


# ---------------------------------------------------------------------------
# AC4 – environment detail includes Job completed/running state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac4_message_includes_job_completed_state() -> None:
    """When the wait gives up, the error message states whether the Job had
    completed or was still running."""
    prov = _make_provider(wait_seconds=2.0, prepare_timeout=900)

    async def always_pods(*args, **kwargs):
        if "list_objects" in str(args):
            return [{"metadata": {"name": "pod"}}]
        return None

    prov._call = always_pods

    with pytest.raises(PrepareJobPodsTimeoutError) as exc:
        await prov._await_preparer_job_pods_gone(
            "completed-job",
            job_completed=True,
            job_completed_reason=None,
        )
    assert "the Job had completed" in str(exc.value)

    with pytest.raises(PrepareJobPodsTimeoutError) as exc:
        await prov._await_preparer_job_pods_gone(
            "running-job",
            job_completed=False,
            job_completed_reason="NodeAffinity",
        )
    assert "the Job was still running (reason: NodeAffinity)" in str(exc.value)


# ---------------------------------------------------------------------------
# Additional: verify the backoff is exponential
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac1_backoff_is_exponential() -> None:
    """The backoff doubles each time: 2 s, 4 s, 8 s, ... until the cap."""
    prov = _make_provider(
        wait_seconds=20.0,
        prepare_timeout=900,
    )

    call_times: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        call_times.append(seconds)
        await real_sleep(0)

    async def never_clear(*args, **kwargs):
        if "list_objects" in str(args):
            return [{"metadata": {"name": "pod"}}]
        return None

    prov._call = never_clear

    with patch("asyncio.sleep", fake_sleep):
        with pytest.raises(PrepareJobPodsTimeoutError):
            await prov._await_preparer_job_pods_gone(
                "exp-job",
                job_completed=True,
                job_completed_reason=None,
            )

    # Backoff values should be: 2, 4, 8, 6 (last clipped by max_timeout)
    assert len(call_times) >= 3
    assert call_times[0] == 2.0
    assert call_times[1] == 4.0
    assert call_times[2] == 8.0
