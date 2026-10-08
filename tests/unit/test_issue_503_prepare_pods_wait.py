"""hades #503: a preparer whose Job's Pods are slow to clear is retried with backoff,
keeps the previous bundle, and costs no attempt.

The provider half runs against the fake Kubernetes API (26): the preparer Job runs, the
provider deletes it, and a stand-in for the Job controller's background propagation
leaves the Pod behind for a while. A fake clock stands in for `time.monotonic` and
`asyncio.sleep` in the provider module, so a 15 s wait takes no time here. The
supervisor half runs `_finish_launch` as hades #423's tests do, and the corrections and
retention halves run the pure functions against an in-memory unit of work."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution import kubernetes as kubernetes_module
from crucible.adapters.execution.k8sapi import KubernetesUnavailableError
from crucible.adapters.execution.kubernetes import KubernetesConfig, PrepareJobPodsTimeoutError
from crucible.application.corrections import (
    PREVIOUS_BUNDLE_GONE,
    PREVIOUS_BUNDLE_OTHER_PROVIDER,
    _unpublished_bundle_problem,
)
from crucible.application.supervisor import (
    PREPARE_POD_WAIT_RETRY_BUDGET,
    PREPARE_POD_WAIT_RETRY_DELAY_SECONDS,
    workspace_release_reason,
)
from crucible.cli.wiring import kubernetes_config
from crucible.domain.entities import (
    Attempt,
    Event,
    EvidenceRecord,
    Execution,
    ExecutionRole,
    RetentionAction,
    Task,
)
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import ATTEMPT_TERMINAL, AttemptState, ExecutionState, TaskState
from crucible.ports.execution import LaunchCancelledError, ProviderError
from crucible.settings import Settings
from tests.unit.kubernetes_fixtures import ATTEMPT, build, spec
from tests.unit.test_class_routing import NOW
from tests.unit.test_issue_423_quota_capacity_waits import _events, _finishing

PREPARER_JOB = k8sspec.object_name("prepare", ATTEMPT)

# ----- the fake clock and the lingering Pod ------------------------------------------


class _Clock:
    """A monotonic clock that the provider's sleeps advance. Pauses of zero (the poll
    interval the unit tier uses) are not recorded; the pod-gone wait's are."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []
        self._real_sleep = asyncio.sleep

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleeps.append(seconds)
        self.now += seconds
        await self._real_sleep(0)


class _Module:
    """`time` or `asyncio` as the provider module sees it, with one name replaced."""

    def __init__(self, real: Any, **replaced: Any) -> None:
        self._real = real
        self._replaced = replaced

    def __getattr__(self, name: str) -> Any:
        if name in self._replaced:
            return self._replaced[name]
        return getattr(self._real, name)


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(kubernetes_module, "time", _Module(time, monotonic=clock.monotonic))
    monkeypatch.setattr(kubernetes_module, "asyncio", _Module(asyncio, sleep=clock.sleep))
    return clock


def _linger(api: Any, *, polls: int | None) -> dict[str, Any]:
    """The Job controller's background propagation, slow: deleting the preparer Job
    leaves its Pod behind until the provider has listed it `polls` times (None: for
    good). Returns the state a test reads: the Job's name and the listings seen."""
    real_delete, real_list = api.delete, api.list_objects
    state: dict[str, Any] = {"job": None, "polls": 0}

    def delete(kind: str, name: str, **kwargs: Any) -> None:
        if kind == "jobs" and name == PREPARER_JOB and (kind, name) in api.objects:
            state["job"] = name
            kwargs["propagation"] = "Orphan"
        real_delete(kind, name, **kwargs)

    def list_objects(kind: str, **kwargs: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = real_list(kind, **kwargs)
        if (
            kind == "pods"
            and state["job"] is not None
            and kwargs.get("label_selector") == f"job-name={state['job']}"
            and rows
        ):
            state["polls"] += 1
            if polls is not None and state["polls"] > polls:
                for row in rows:
                    real_delete("pods", str(row["metadata"]["name"]))
                rows = []
        return rows

    api.delete = delete
    api.list_objects = list_objects
    return state


def _config(**overrides: Any) -> KubernetesConfig:
    values: dict[str, Any] = {
        "poll_interval_seconds": 0,
        "launch_timeout_seconds": 5,
        "storage_class": "lab-ssd",
        "image_pull_secret": "ghcr-pull",
        **overrides,
    }
    return KubernetesConfig(**values)


# ----- AC1: pods that clear during the backoff let the attempt launch ----------------


async def test_pods_that_clear_during_the_backoff_let_the_attempt_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Pod outlives the first look and the first pause; the second pause is longer,
    the Pod is gone on the third look, and prepare returns a workspace the attempt
    launches from."""
    api, _registry, provider = build()
    clock = _fake_clock(monkeypatch)
    lingering = _linger(api, polls=2)
    launch = spec()

    workspace = await provider.prepare(launch)

    assert lingering["job"] == PREPARER_JOB
    assert lingering["polls"] == 3
    assert clock.sleeps == [2.0, 4.0]
    assert workspace.checkout_path.endswith("/repo")
    assert not api.list_objects("pods", label_selector=f"job-name={PREPARER_JOB}")
    handle = await provider.launch(workspace, launch)
    assert handle.ref.startswith("worker-")
    assert api.get("jobs", handle.ref)["metadata"]["name"] == handle.ref


async def test_an_api_outage_during_the_wait_is_asked_again_not_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _registry, provider = build()
    clock = _fake_clock(monkeypatch)
    lingering = _linger(api, polls=1)
    listing = api.list_objects
    outages = {"left": 2}

    def flaky(kind: str, **kwargs: Any) -> list[dict[str, Any]]:
        # The outage starts once the Job is deleted, so it lands on the pod-gone wait.
        if (
            kind == "pods"
            and lingering["job"] is not None
            and kwargs.get("label_selector") == f"job-name={PREPARER_JOB}"
            and outages["left"] > 0
        ):
            outages["left"] -= 1
            raise KubernetesUnavailableError(503, "the API server is restarting", path="/pods")
        return listing(kind, **kwargs)

    monkeypatch.setattr(api, "list_objects", flaky)

    await provider.prepare(spec())

    # Two outages, then the Pod seen once, then gone: three pauses, never a failure.
    assert outages["left"] == 0
    assert lingering["polls"] == 2
    assert clock.sleeps == [2.0, 4.0, 8.0]


# ----- AC1, AC4: pods that never clear fail the prepare after the whole wait ---------


async def test_pods_that_never_clear_fail_the_prepare_after_the_whole_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default wait is 15 s: four tries at 2, 4, 8 and 1 s, the last clipped to
    what is left, and the message says the Job had completed when the wait gave up."""
    api, _registry, provider = build()
    clock = _fake_clock(monkeypatch)
    _linger(api, polls=None)

    with pytest.raises(PrepareJobPodsTimeoutError) as raised:
        await provider.prepare(spec())

    message = str(raised.value)
    assert f"Pods for Job {PREPARER_JOB!r} were still present after 15 seconds" in message
    assert "(4 retries with backoff)" in message
    assert "the Job had completed with exit 0" in message
    assert f"Pod {PREPARER_JOB}" in message
    assert clock.sleeps == [2.0, 4.0, 8.0, 1.0]
    assert sum(clock.sleeps) == 15.0
    assert isinstance(raised.value, ProviderError)


@pytest.mark.parametrize(
    ("wait", "prepare_timeout", "pauses"),
    [
        # Each pause is clipped to what is left of the wait: never 2 s and then 3 s.
        (3.0, 900, [2.0, 1.0]),
        # The wait is bounded by prepare_timeout_seconds, whatever the setting says.
        (60.0, 5, [2.0, 3.0]),
    ],
)
async def test_every_pause_is_clipped_to_what_is_left_of_the_wait(
    monkeypatch: pytest.MonkeyPatch, wait: float, prepare_timeout: int, pauses: list[float]
) -> None:
    api, _registry, provider = build(
        config=_config(
            prepare_pod_deletion_wait_seconds=wait, prepare_timeout_seconds=prepare_timeout
        )
    )
    clock = _fake_clock(monkeypatch)
    _linger(api, polls=None)

    with pytest.raises(PrepareJobPodsTimeoutError, match="still present after") as raised:
        await provider.prepare(spec())

    assert clock.sleeps == pauses
    total = min(wait, prepare_timeout)
    assert sum(clock.sleeps) == total
    assert f"after {total:g} seconds ({len(pauses)} retries with backoff)" in str(raised.value)


async def test_the_message_says_the_job_was_still_running_when_the_wait_gave_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: the wait that follows a Job the provider stopped waiting for (a cancel, an
    API error) reports it as still running, with the lingering Pod's phase."""
    api, _registry, provider = build(config=_config(prepare_pod_deletion_wait_seconds=2.0))
    _fake_clock(monkeypatch)
    api.create(
        "pods",
        {
            "metadata": {
                "name": "prepare-stuck-x1",
                "labels": {"job-name": "prepare-stuck"},
                "deletionTimestamp": "2026-10-07T03:00:00Z",
            },
            "status": {"phase": "Running"},
        },
    )

    with pytest.raises(PrepareJobPodsTimeoutError) as raised:
        await provider._await_preparer_job_pods_gone(
            "prepare-stuck", job_outcome="the Job was still running when the wait for it ended"
        )

    message = str(raised.value)
    assert "the Job was still running when the wait for it ended" in message
    assert "Pod prepare-stuck-x1 was Running, deletion under way" in message


async def test_a_cancel_while_the_preparer_runs_is_still_a_cancel_when_its_pods_linger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel raised while the Job ran reaches the `finally` before the Job's outcome
    is known; the pod-gone wait still runs, with the Job reported as still running, and
    a Pod lingering behind the cancel is logged, not raised over it."""
    api, _registry, provider = build()
    clock = _fake_clock(monkeypatch)
    _linger(api, polls=None)
    looks = 0

    async def cancelled() -> bool:
        nonlocal looks
        looks += 1
        return looks > 2

    with pytest.raises(LaunchCancelledError, match="cancelled while"):
        await provider.prepare(spec(), cancelled=cancelled)

    assert clock.sleeps == [2.0, 4.0, 8.0, 1.0]
    assert api.list_objects("pods", label_selector=f"job-name={PREPARER_JOB}")


# ----- the wait is a deployment setting -----------------------------------------------


def test_the_deletion_wait_is_a_kubernetes_setting_with_a_15_second_default() -> None:
    assert Settings().kubernetes.prepare_pod_deletion_wait_seconds == 15.0
    assert kubernetes_config(Settings()).prepare_pod_deletion_wait_seconds == 15.0
    settings = Settings(kubernetes={"prepare_pod_deletion_wait_seconds": 40})
    assert kubernetes_config(settings).prepare_pod_deletion_wait_seconds == 40.0


# ----- AC3: the supervisor prepares again, bounded, and charges no attempt ------------

POD_WAIT = (
    f"Pods for Job {PREPARER_JOB!r} were still present after 15 seconds (4 retries with "
    f"backoff): the Job had completed with exit 0; Pod {PREPARER_JOB}-x1 was Succeeded, "
    "deletion under way"
)


def _deferrals(item: Any, count: int) -> list[Event]:
    """The attempt's earlier deferral events, as the supervisor counts them."""
    return [
        Event(
            seq=n + 1,
            ts=NOW,
            kind=EventKind.HARNESS_LAUNCH_DEFERRED.value,
            principal="crucible",
            verified=True,
            payload={"prepare_pod_wait": True, "retry": n + 1, "stage": "prepare"},
            task_id=item.task.id,
            execution_id=item.execution.id,
            attempt_id=item.attempt.id,
        )
        for n in range(count)
    ]


def _charged(uow: Any, execution: Execution) -> int:
    """The attempts `_classify_and_finish` counts against `max_attempts`: the
    execution's terminal ones, less the classes it exempts."""
    return sum(
        prior.exit_class
        not in {ExitClass.QUOTA_EXHAUSTED, ExitClass.INFRASTRUCTURE, ExitClass.BLOCKED}
        for prior in uow.attempts.list_for_execution(execution.id)
        if prior.state in ATTEMPT_TERMINAL
    )


def _pod_wait_failing(monkeypatch: pytest.MonkeyPatch, earlier: int) -> tuple[Any, Any, Any, Any]:
    supervisor, item, uow, provider, _launch = _finishing(monkeypatch)
    uow.events.list_for_task.return_value = _deferrals(item, earlier)
    monkeypatch.setattr(
        supervisor, "_prepare", AsyncMock(side_effect=PrepareJobPodsTimeoutError(POD_WAIT))
    )
    return supervisor, item, uow, provider


@pytest.mark.parametrize("earlier", [0, 1, 2])
async def test_the_pod_wait_error_prepares_the_same_attempt_again_after_a_longer_pause(
    monkeypatch: pytest.MonkeyPatch, earlier: int
) -> None:
    supervisor, item, uow, provider = _pod_wait_failing(monkeypatch, earlier)
    assert _charged(uow, item.execution) == 0

    assert not await supervisor._finish_launch(item, provider)

    assert item.attempt.state is AttemptState.PENDING
    assert item.task.state is TaskState.SCHEDULED
    assert item.attempt.exit_class is None and item.attempt.termination_reason is None
    assert item.attempt.started_at is None and item.attempt.ended_at is None
    assert item.attempt.workspace_path is None
    # AC3: the same attempt row goes around again; nothing terminal was recorded, so
    # the count the retry policy charges against max_attempts is what it was.
    assert _charged(uow, item.execution) == 0
    supervisor._classify_and_finish.assert_not_called()
    uow.escalations.add.assert_not_called()
    (deferred,) = _events(uow, EventKind.HARNESS_LAUNCH_DEFERRED)
    assert deferred.payload["prepare_pod_wait"] is True
    assert deferred.payload["retry"] == earlier + 1
    assert deferred.payload["retry_budget"] == PREPARE_POD_WAIT_RETRY_BUDGET == 3
    assert deferred.payload["stage"] == "prepare"
    assert "the Job had completed with exit 0" in deferred.payload["detail"]
    assert deferred.payload["from"] == "preparing" and deferred.payload["to"] == "pending"
    (scheduled,) = _events(uow, EventKind.TASK_SCHEDULED)
    assert scheduled.payload["reason"] == "prepare_pod_wait"
    assert "were still present after 15 seconds" in scheduled.payload["detail"]
    delay = PREPARE_POD_WAIT_RETRY_DELAY_SECONDS * 2**earlier
    assert deferred.payload["retry_delay_seconds"] == delay == [30, 60, 120][earlier]
    assert item.task.resume_at == NOW + timedelta(seconds=delay)
    supervisor._release_checkout_leases.assert_called_once()


async def test_the_fourth_pod_wait_failure_ends_the_attempt_as_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, provider = _pod_wait_failing(monkeypatch, PREPARE_POD_WAIT_RETRY_BUDGET)

    assert not await supervisor._finish_launch(item, provider)

    assert item.attempt.state is AttemptState.COLLECTED
    assert item.attempt.exit_class is ExitClass.ENVIRONMENT
    assert item.task.resume_at is None
    detail = item.attempt.termination_detail or ""
    assert detail.startswith(f"prepare: Pods for Job {PREPARER_JOB!r} were still present")
    assert "the Job had completed with exit 0" in detail
    assert "the prepare was tried 3 more times and the Pods stayed" in detail
    assert not _events(uow, EventKind.HARNESS_LAUNCH_DEFERRED)
    (collected,) = _events(uow, EventKind.ATTEMPT_COLLECTED)
    assert collected.payload["exit_class"] is ExitClass.ENVIRONMENT
    supervisor._classify_and_finish.assert_called_once()
    summary = supervisor._classify_and_finish.call_args.kwargs["wake_summary"]
    assert summary.startswith("attempt 1 ended environment at prepare: ")


async def test_a_cancelled_task_ends_the_attempt_as_a_cancel_not_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, provider = _pod_wait_failing(monkeypatch, 0)
    monkeypatch.setattr(supervisor, "_finish_cancelling", MagicMock())

    async def cancel_during_prepare(*_: Any, **__: Any) -> Any:
        item.task.state = TaskState.CANCELLING
        raise PrepareJobPodsTimeoutError(POD_WAIT)

    monkeypatch.setattr(supervisor, "_prepare", cancel_during_prepare)

    assert not await supervisor._finish_launch(item, provider)

    assert item.attempt.state is AttemptState.FAILED
    assert item.attempt.exit_class is ExitClass.KILLED
    assert not _events(uow, EventKind.HARNESS_LAUNCH_DEFERRED)


# ----- AC2: the next correction resumes from the previous attempt's bundle -----------

IMPLEMENT = "01EXECIMPLEMENT0000000000A"
CORRECT = "01EXECCORRECT000000000000B"
# The failed prepare is the newer attempt: its id sorts above the sealed one's.
SEALED = "01ATTEMPT0SEALED000000000A"
FAILED_PREPARE = "01ATTEMPT1FAILEDPREPARE00B"
SEALED_WORKSPACE = f"k8s://crucible-workers/{k8sspec.object_name('ws', SEALED)}"


def _task() -> Task:
    return Task(
        "task",
        "FDY-0507",
        "foundry",
        "p",
        "t",
        TaskState.REPORTED,
        2,
        "policy",
        1,
        "repo",
        NOW,
        NOW,
        head_sha="b" * 40,
    )


def _execution(execution_id: str, role: ExecutionRole, provider: str = "kubernetes") -> Execution:
    return Execution(
        execution_id,
        "task",
        role,
        1 if role is ExecutionRole.IMPLEMENT else 2,
        "codex",
        "m",
        None,
        provider,
        "img",
        {},
        ExecutionState.FAILED,
        2,
        ["environment"],
        60,
        NOW,
    )


def _bundle_head(attempt_id: str) -> EvidenceRecord:
    return EvidenceRecord(
        id=1,
        attempt_id=attempt_id,
        task_id="task",
        kind="bundle_head",
        verified=True,
        payload={"bundle_verified": True, "bundle_sha256": "c" * 64, "head_sha": "b" * 40},
        observed_at=NOW,
        source="crucible",
    )


def _uow(
    executions: list[Execution],
    attempts: list[Attempt],
    *,
    released: list[str] = (),  # type: ignore[assignment]
) -> Any:
    uow: Any = MagicMock()
    uow.executions.list_for_task.return_value = executions
    uow.executions.get.side_effect = lambda execution_id, **_: next(
        (row for row in executions if row.id == execution_id), None
    )
    uow.attempts.list_for_task.return_value = attempts
    uow.attempts.list_for_execution.side_effect = lambda execution_id: [
        row for row in attempts if row.execution_id == execution_id
    ]
    uow.events.latest_for_task_kind.return_value = None
    uow.events.list_for_task.return_value = []
    uow.evidence.list_for_attempt.side_effect = lambda attempt_id: (
        [_bundle_head(attempt_id)] if attempt_id == SEALED else []
    )
    uow.retention.list_recent.return_value = [
        RetentionAction(
            id=f"release-{subject}",
            kind="workspace",
            subject=subject,
            policy_name="policy",
            policy_version=1,
            acted_at=NOW,
            detail={},
        )
        for subject in released
    ]
    return uow


def _after_a_failed_prepare(
    *, same_execution: bool, provider: str = "kubernetes"
) -> tuple[list[Execution], list[Attempt]]:
    """An implementing attempt that sealed a bundle, then an attempt whose prepare failed
    (the preparer's Pods never cleared) and so has no workspace: a retry of the same
    execution, or the first attempt of a correction execution."""
    implement = _execution(IMPLEMENT, ExecutionRole.IMPLEMENT, provider)
    sealed = Attempt(
        SEALED,
        IMPLEMENT,
        "task",
        1,
        AttemptState.FAILED,
        NOW,
        workspace_path=SEALED_WORKSPACE,
        exit_class=ExitClass.ENVIRONMENT,
        ended_at=NOW,
        cleaned_up_at=NOW,
    )
    failed = Attempt(
        FAILED_PREPARE,
        IMPLEMENT if same_execution else CORRECT,
        "task",
        2 if same_execution else 1,
        AttemptState.FAILED,
        NOW + timedelta(hours=1),
        workspace_path=None,
        exit_class=ExitClass.ENVIRONMENT,
        ended_at=NOW + timedelta(hours=1),
        termination_detail=f"prepare: {POD_WAIT}",
    )
    executions = (
        [implement]
        if same_execution
        else [implement, _execution(CORRECT, ExecutionRole.CORRECT, provider)]
    )
    return executions, [sealed, failed]


@pytest.mark.parametrize("same_execution", [False, True])
def test_a_correction_after_a_failed_prepare_resumes_from_the_sealed_bundle(
    same_execution: bool,
) -> None:
    """The latest work attempt never prepared, so the bundle it would have resumed from,
    the task's last sealed one, is the one the next correction resumes from, whether
    the failed attempt belongs to a correction execution or to the same execution."""
    executions, attempts = _after_a_failed_prepare(same_execution=same_execution)
    uow = _uow(executions, attempts)

    assert _unpublished_bundle_problem(uow, _task(), "kubernetes") is None
    assert _unpublished_bundle_problem(uow, _task(), "kubernetes", last_attempt=True) is None


def test_the_sealed_bundle_is_what_the_check_reads_not_the_failed_prepare() -> None:
    executions, attempts = _after_a_failed_prepare(same_execution=False)
    # Released by retention: the sealed bundle really is gone, and the answer says so.
    uow = _uow(executions, attempts, released=[SEALED])
    problem = _unpublished_bundle_problem(uow, _task(), "kubernetes")
    assert problem == {"path": "correction", "message": PREVIOUS_BUNDLE_GONE}
    # A release recorded against the attempt that never prepared is no loss at all.
    uow = _uow(executions, attempts, released=[FAILED_PREPARE])
    assert _unpublished_bundle_problem(uow, _task(), "kubernetes") is None
    # The provider that holds the sealed bundle is the one the correction must use.
    executions, attempts = _after_a_failed_prepare(same_execution=False)
    uow = _uow(executions, attempts)
    problem = _unpublished_bundle_problem(uow, _task(), "docker")
    assert problem == {
        "path": "execution_request.provider",
        "message": PREVIOUS_BUNDLE_OTHER_PROVIDER,
    }


@pytest.mark.parametrize("same_execution", [False, True])
def test_retention_keeps_the_sealed_workspace_while_the_latest_attempt_has_none(
    same_execution: bool,
) -> None:
    """The supervisor's release rule: the workspace the next correction resumes from
    stays, past the retention window, while the task's latest implementing or
    correcting attempt has no workspace of its own; once that attempt has prepared,
    the window applies to the older one again."""
    executions, attempts = _after_a_failed_prepare(same_execution=same_execution)
    sealed, failed = attempts
    uow = _uow(executions, attempts)
    later: datetime = NOW + timedelta(days=15)

    assert workspace_release_reason(uow, _task(), sealed, later, 14) is None

    failed.workspace_path = "k8s://crucible-workers/ws-prepared-after-all"
    assert workspace_release_reason(uow, _task(), sealed, later, 14) == "retention_window"
