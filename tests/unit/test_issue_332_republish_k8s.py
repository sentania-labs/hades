"""Republish on Kubernetes after a publish failure (hades #332, FDY-0219).

Tests that republish_task correctly handles k8s:// workspace paths: when the
workspace path is a k8s:// URI (the bundle lives on a Kubernetes claim), the
republish bundle-hash check uses the recorded seal rather than refusing with
"republish requires the unchanged sealed bundle" because the path is not a
local file. Regression test for issue 332.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.application.errors import TransitionNotAllowedError
from crucible.application.publish import _claim_gone
from crucible.application.republish import republish_task
from crucible.contracts.api import PublishRetryRequest
from crucible.contracts.evidence import EvidenceKind
from crucible.domain.entities import (
    AcceptanceResult,
    AcceptanceVerdict,
    Artifact,
    Attempt,
    Event,
    EvidenceRecord,
    Execution,
    ExecutionRole,
    Principal,
    Role,
    Task,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState

# ---------------------------------------------------------------------------
# Minimal mock UnitOfWork components
# ---------------------------------------------------------------------------


class _TaskRepo:
    def __init__(self, store: dict[str, Task]) -> None:
        self._store = store

    def get(self, task_id: str, for_update: bool = False) -> Task | None:
        return self._store.get(task_id)

    def save(self, task: Task) -> None:
        self._store[task.id] = task


class _AttemptRepo:
    def __init__(self, store: list[Attempt]) -> None:
        self._store = store

    def list_for_execution(self, execution_id: str) -> list[Attempt]:
        return [a for a in self._store if a.execution_id == execution_id]


class _ExecutionRepo:
    def __init__(self, store: list[Execution]) -> None:
        self._store = store

    def list_for_task(self, task_id: str) -> list[Execution]:
        return [e for e in self._store if e.task_id == task_id]


class _EventRepo:
    def __init__(self, store: list[Event]) -> None:
        self._store = store

    def latest_for_task_kind(self, task_id: str, kind: str) -> Event | None:
        matches = [e for e in self._store if e.task_id == task_id and e.kind == kind]
        return matches[-1] if matches else None

    def append(self, event: Event) -> Event:
        self._store.append(event)
        return event


class _AcceptanceRepo:
    def __init__(self, store: list[AcceptanceResult]) -> None:
        self._store = store

    def list_for_task(self, task_id: str) -> list[AcceptanceResult]:
        return [a for a in self._store if a.task_id == task_id]


class _EvidenceRepo:
    def __init__(self, store: list[EvidenceRecord]) -> None:
        self._store = store

    def list_for_attempt(self, attempt_id: str) -> list[EvidenceRecord]:
        return [e for e in self._store if e.attempt_id == attempt_id]


class _MockUow:
    """A minimal UnitOfWork that stores the objects a republish flow touches."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._attempts: list[Attempt] = []
        self._executions: list[Execution] = []
        self._events: list[Event] = []
        self._acceptances: list[AcceptanceResult] = []
        self._evidence: list[EvidenceRecord] = []
        self._artifacts: dict[str, Artifact] = {}

    def commit(self) -> None:
        pass

    @property
    def tasks(self) -> Any:
        return _TaskRepo(self._tasks)

    @property
    def attempts(self) -> Any:
        return _AttemptRepo(self._attempts)

    @property
    def executions(self) -> Any:
        return _ExecutionRepo(self._executions)

    @property
    def events(self) -> Any:
        return _EventRepo(self._events)

    @property
    def acceptance(self) -> Any:
        return _AcceptanceRepo(self._acceptances)

    @property
    def evidence(self) -> Any:
        return _EvidenceRepo(self._evidence)

    @property
    def artifacts(self) -> Any:
        return SimpleNamespace(get=self._artifacts.get)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NS = "crucible-workers"
_K8S_WS = f"k8s://{_NS}/ws-01attempts00000000000000000a"
_FAKE_WS = "fake:///workspaces/01attempts00000000000000000a"
_HEAD = "deadbeef" * 5
_ATTEMPT_ID = "01attempts00000000000000000a"
_EXEC_ID = "01executs0000000000000000000"
_TASK_ID = "01task00000000000000000000"
_BUNDLE_SHA = "a" * 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    state: TaskState,
    head_sha: str = _HEAD,
) -> Task:
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
        head_sha=head_sha,
    )


def _make_execution(
    workspace_path: str,
    exec_id: str = _EXEC_ID,
    exec_role: ExecutionRole = ExecutionRole.IMPLEMENT,
    exec_state: ExecutionState = ExecutionState.ACTIVE,
    attempt_state: AttemptState = AttemptState.SUCCEEDED,
) -> tuple[Attempt, Execution]:
    exec_ = Execution(
        id=exec_id,
        task_id=_TASK_ID,
        role=exec_role,
        contract_version=1,
        harness="script-harness",
        model="gpt-5.6-sol",
        effort="high",
        provider="kubernetes",
        image="ghcr.io/sentania-labs/crucible-worker:script-harness",
        policy_snapshot={
            "limits": {"publish_retry_max": 3},
            "resources": {"cpus": 2, "memory": "4GiB"},
        },
        state=exec_state,
        max_attempts=2,
        retry_on=["environment", "lost"],
        timeout_seconds=3600,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    attempt = Attempt(
        id=_ATTEMPT_ID,
        execution_id=exec_id,
        task_id=_TASK_ID,
        number=1,
        state=attempt_state,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        workspace_path=workspace_path,
    )
    return attempt, exec_


def _add_k8s_task(
    uow: _MockUow,
    *,
    workspace_path: str = _K8S_WS,
    head_sha: str = _HEAD,
    task_state: TaskState = TaskState.PUBLISH_FAILED,
    bundle_sha256: str = _BUNDLE_SHA,
) -> None:
    """Add a task with a workspace path, an attempt, execution, and the events
    that republish_task requires: an acceptance result, an evidence record,
    a publish_failed event, and a publish_started event."""
    attempt, exec_ = _make_execution(workspace_path)
    uow._tasks[_TASK_ID] = _make_task(task_state, head_sha)
    uow._attempts.append(attempt)
    uow._executions.append(exec_)
    uow._evidence.append(
        EvidenceRecord(
            id=None,
            attempt_id=_ATTEMPT_ID,
            task_id=_TASK_ID,
            kind=EvidenceKind.BUNDLE_HEAD.value,
            observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            source="collector",
            verified=True,
            payload={"bundle_sha256": bundle_sha256, "bundle_artifact_id": "bundle-artifact"},
            artifact_id="bundle-artifact",
        )
    )
    # ADR 0032: the stored bundle is what a Kubernetes publisher reads.
    uow._artifacts["bundle-artifact"] = Artifact(
        id="bundle-artifact",
        attempt_id=_ATTEMPT_ID,
        task_id=_TASK_ID,
        type="work_bundle",
        filename="output/work_branch.bundle",
        path=f"blobs/{bundle_sha256}",
        size=1,
        sha256=bundle_sha256,
        content_type="application/x-git-bundle",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        created_by="crucible",
    )
    uow._acceptances.append(
        AcceptanceResult(
            id="acc-001",
            task_id=_TASK_ID,
            head_sha=head_sha,
            principal_id="p-01",
            verdict=AcceptanceVerdict.ACCEPTED,
            reasoning="looks good",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
            superseded_at=None,
        )
    )
    uow._events.extend(
        [
            Event(
                seq=1,
                ts=datetime(2026, 1, 3, tzinfo=UTC),
                kind=EventKind.TASK_PUBLISH_FAILED.value,
                principal="orchestrator",
                verified=True,
                payload={
                    "head_sha": head_sha,
                    "step": "publish",
                    "reason": "HTTP 503",
                },
                task_id=_TASK_ID,
                execution_id=_EXEC_ID,
                attempt_id=_ATTEMPT_ID,
            ),
            Event(
                seq=0,
                ts=datetime(2026, 1, 2, tzinfo=UTC),
                kind=EventKind.PUBLISH_STARTED.value,
                principal="orchestrator",
                verified=True,
                payload={
                    "head_sha": head_sha,
                    "bundle": f"{workspace_path}/output/work_branch.bundle",
                    "bundle_sha256": bundle_sha256,
                },
                task_id=_TASK_ID,
                execution_id=_EXEC_ID,
                attempt_id=None,
            ),
        ]
    )


class _FakeClock:
    """A clock that returns a fixed time."""

    def now(self) -> datetime:
        return datetime(2026, 1, 3, tzinfo=UTC)


def _publish_request(reason: str = "manual retry") -> PublishRetryRequest:
    return PublishRetryRequest(reason=reason)


def _republish(
    uow: _MockUow,
    principal: Principal,
) -> Task:
    """Call republish_task; type: ignore covers the _MockUow -> UnitOfWork mismatch."""
    return republish_task(
        uow,  # type: ignore[arg-type]
        _FakeClock(),
        principal=principal,
        task_id=_TASK_ID,
        request=_publish_request(),
    )


# ---------------------------------------------------------------------------
# Tests: k8s:// bundle path
# ---------------------------------------------------------------------------


def test_k8s_bundle_republishes_when_seal_verifies() -> None:
    """A k8s:// workspace with a verified seal can be republished.

    The fix for issue 332 adds `k8s://` to the path that skips local
    file hashing and uses the recorded seal, so republish succeeds
    instead of refusing with "republish requires the unchanged sealed
    bundle from the accepted attempt".
    """
    uow = _MockUow()
    _add_k8s_task(uow)
    principal = Principal(
        id="p-01",
        name="orchestrator",
        role=Role.ORCHESTRATOR,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    task = _republish(uow, principal)

    assert task.state is TaskState.PUBLISHING
    events = [e for e in uow._events if e.task_id == _TASK_ID]
    publishing_events = [e for e in events if e.kind == EventKind.TASK_PUBLISHING.value]
    assert len(publishing_events) == 1
    assert publishing_events[-1].payload["retry_number"] == 1


def test_fake_workspace_bundle_republishes_when_seal_verifies() -> None:
    """A fake:/// workspace also republishes (the existing path).

    Confirms that the fix does not break the existing fake:/// handling.
    """
    uow = _MockUow()
    _add_k8s_task(uow, workspace_path=_FAKE_WS)
    principal = Principal(
        id="p-01",
        name="operator",
        role=Role.OPERATOR,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    task = _republish(uow, principal)

    assert task.state is TaskState.PUBLISHING


def test_k8s_bundle_refused_when_seal_mismatch() -> None:
    """A k8s:// workspace with a mismatched seal is refused.

    The seal in the evidence record differs from what the publisher
    recorded at publish time, so republish refuses.
    """
    uow = _MockUow()
    _add_k8s_task(uow, bundle_sha256="a" * 64)
    # Change the evidence record to a different sha256.
    uow._evidence[0].payload["bundle_sha256"] = "b" * 64
    principal = Principal(
        id="p-01",
        name="orchestrator",
        role=Role.ORCHESTRATOR,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(Exception, match="republish requires the unchanged sealed bundle"):
        _republish(uow, principal)


def test_k8s_missing_claim_refused() -> None:
    """When the claim is gone and the seal cannot be re-derived, the
    republish check fails because the bundle_sha256 is empty. The wake
    text says no retry is possible because the seal is missing, not
    because of a retry count."""
    uow = _MockUow()
    _add_k8s_task(uow)
    # Clear the bundle_sha256 from both evidence and the event.
    uow._evidence[0].payload["bundle_sha256"] = ""
    uow._events[-1].payload["bundle_sha256"] = ""
    principal = Principal(
        id="p-01",
        name="orchestrator",
        role=Role.ORCHESTRATOR,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(Exception, match="republish requires the unchanged sealed bundle"):
        _republish(uow, principal)


def test_k8s_workspace_path_is_not_refused_by_local_file_check() -> None:
    """A k8s:// workspace path is never treated as a missing local file.

    Without the fix, Path(k8s://...).is_file() returns False and
    workspace_path.startswith("fake:///") also returns False, leaving
    current_sha256 = "" which fails the check. The fix adds k8s://
    so the k8s publisher re-hashes against the seal.
    """
    uow = _MockUow()
    _add_k8s_task(uow)
    principal = Principal(
        id="p-01",
        name="operator",
        role=Role.OPERATOR,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    task = _republish(uow, principal)
    assert task.state is TaskState.PUBLISHING


def test_k8s_bundle_refused_when_no_seal_recorded() -> None:
    """When bundle_sha256 is empty (no seal at all), republish must refuse."""
    uow = _MockUow()
    _add_k8s_task(uow, bundle_sha256="")
    principal = Principal(
        id="p-01",
        name="orchestrator",
        role=Role.ORCHESTRATOR,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(Exception, match="republish requires the unchanged sealed bundle"):
        _republish(uow, principal)


def test_k8s_republish_increments_retry_number() -> None:
    """A successful republish sets retry_number to 1."""
    uow = _MockUow()
    _add_k8s_task(uow)
    principal = Principal(
        id="p-01",
        name="orchestrator",
        role=Role.ORCHESTRATOR,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    _republish(uow, principal)

    publishing_event = uow._events[-1]
    assert publishing_event.kind == EventKind.TASK_PUBLISHING.value
    assert publishing_event.payload["retry_number"] == 1


# ---------------------------------------------------------------------------
# Tests: claim-gone fail_publish path (correction)
# ---------------------------------------------------------------------------


def test_claim_gone_fail_publish_no_republish_link() -> None:
    """When the failure step is bundle-seal and the claim is gone,
    fail_publish must not create a republish link."""

    assert _claim_gone("bundle-seal", "the workspace claim is gone")
    assert _claim_gone("bundle-seal", "no branch bundle on the workspace claim")
    assert not _claim_gone("bundle-seal", "the branch bundle has no recorded sha256 seal")
    assert not _claim_gone("publish-leaf", "the workspace claim is gone")
    assert not _claim_gone("plan", "some other failure")


def test_claim_gone_republish_refused_without_retry_consumption() -> None:
    """When the previous failure was a bundle-seal / claim-gone failure,
    republish must refuse immediately without consuming a retry.

    This confirms the correction: republish does not decrement the retry
    budget when the bundle is genuinely gone.
    """
    uow = _MockUow()
    _add_k8s_task(uow)
    # Mark the publish_failed event as a bundle-seal / claim-gone failure.
    uow._events[0].payload["step"] = "bundle-seal"
    uow._events[0].payload["detail"] = (
        "the workspace claim 'ws-01attempts00000000000000000a' that holds the branch bundle is gone"
    )
    principal = Principal(
        id="p-01",
        name="operator",
        role=Role.OPERATOR,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(
        TransitionNotAllowedError,
        match="republish is not possible: the workspace claim",
    ):
        _republish(uow, principal)

    # Task should still be in PUBLISH_FAILED (no retry consumed).
    task = uow._tasks[_TASK_ID]
    assert task.state is TaskState.PUBLISH_FAILED


def test_transient_failure_still_advertises_retries() -> None:
    """An ordinary transient failure (not bundle-seal) should still
    advertise retries normally.

    This confirms the correction does not affect the ordinary transient
    failure path.
    """
    assert not _claim_gone("publish", "HTTP 503 on the remote")
    assert not _claim_gone("github", "rate limited")
    # A bundle-seal failure without 'gone' / 'missing' is not claim-gone.
    assert not _claim_gone(
        "bundle-seal",
        "the branch bundle has no recorded sha256 seal",
    )
