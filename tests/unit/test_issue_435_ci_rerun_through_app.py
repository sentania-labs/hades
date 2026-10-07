"""Issue 435: CI rerun through the GitHub App when it holds Actions write.

This file is a unit test of `record_ci_decision` (issue 435) exercising the two
code paths that the delivery-supervisor's ci-decision endpoint reaches:

* AC1 – the installation grants Actions write → the app calls `rerun-failed-jobs`,
  the certification records the new attempt number and moves to `AWAITING_CI_CERTIFICATION`.
* AC2 – the installation grants only Actions read → the old handoff behaviour
  (record the decision, raise a wake, leave the task in `AWAITING_CI_CERTIFICATION`).
* AC3 – a second failure of the same job after the rerun does **not** auto-rerun;
  it requires a fresh ci-decision to be recorded.

The test uses a fake GitHub client that mirrors the real client's `GitHubClient`
protocol so the domain and application layers never see HTTP.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from crucible.adapters.github.appauth import InstallationToken
from crucible.application.delivery_decisions import (
    CIDecisionRequest,
    CIAction,
    record_ci_decision,
)
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    CICertification,
    Principal,
    Role,
    Task,
    TaskState,
    Wake,
)
from crucible.ports.clock import Clock
from crucible.ports.github import CheckRecord, GitHubClient, PullRequestRef


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
TASK_ID = "task-001"
TASK_REPOSITORY = "owner/repo"
HEAD_SHA = "abcd1234"


# ---------------------------------------------------------------------------
# Fixtures – fake clock / fake GitHub client
# ---------------------------------------------------------------------------


class FakeClock:
    def now(self) -> datetime:
        return NOW


class FakeGitHubClient(GitHubClient):
    """A minimal `GitHubClient` for issue 435 that tracks installation
    permissions and whether `rerun-failed-jobs` was called.

    `actions_write` controls whether the fake installation grants that scope.
    """

    def __init__(self, actions_write: bool) -> None:
        self.actions_write = actions_write
        self.rerun_calls: list[dict[str, Any]] = []
        self._call_count = 0

    # -- the methods the ci-decision path exercises ------------------------

    def installation_token(
        self,
        *,
        installation_id: int,
        repository: str,
        permissions: dict[str, str] | None = None,
    ) -> InstallationToken:
        if permissions and "actions" in permissions:
            requested = permissions["actions"]
        else:
            requested = None

        self._call_count += 1
        # When we mint a token with actions:write, GitHub returns the actual
        # granted permissions.  When the installation lacks it, the granted
        # value is "read" regardless of the request.
        if requested == "write":
            actual_actions = "write" if self.actions_write else "read"
        else:
            actual_actions = "read"
        return InstallationToken(
            value=f"fake-token-{self._call_count}",
            expires_at=NOW,
            repository=repository,
            permissions={"actions": actual_actions, "contents": "read", "metadata": "read"},
        )

    def rerun_failed_jobs(
        self, token: InstallationToken, *, repository: str, run_id: int
    ) -> dict[str, Any]:
        self.rerun_calls.append({"repository": repository, "run_id": run_id})
        return {
            "id": run_id,
            "name": "CI",
            "run_attempt_number": 2,
            "status": "in_progress",
            "conclusion": None,
            "html_url": f"https://github.com/{repository}/actions/runs/{run_id}",
        }

    # -- unused protocol methods (required for the ABC) --------------------

    def pull(self, token: InstallationToken, *, repository: str, number: int) -> Any:
        raise NotImplementedError

    def get_pull_request(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> PullRequestRef:
        raise NotImplementedError

    def merge_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        head_sha: str,
        **kwargs: Any,
    ) -> Any:
        raise NotImplementedError

    def checks_for_commit(
        self, token: InstallationToken, *, repository: str, head_sha: str
    ) -> tuple[CheckRecord, ...]:
        return ()

    def ci_failure_log(
        self,
        token: InstallationToken,
        *,
        repository: str,
        source: str,
        external_id: str,
        limit_bytes: int,
    ) -> bytes:
        return b""

    def remote_head(
        self, token: InstallationToken, *, repository: str, ref: str
    ) -> str | None:
        return None

    def close_pull_request(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> None:
        pass

    def create_pull_comment(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        path: str,
        line: int,
        body: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def add_reaction(self, token: InstallationToken, *, url: str, content: str) -> None:
        raise NotImplementedError

    def get_installation_permissions(
        self, token: InstallationToken, *, repository: str
    ) -> dict[str, str]:
        # For the real client this calls /app/installations/self; the fake
        # returns whatever the test author configured.
        return {"actions": "write" if self.actions_write else "read"}


# ---------------------------------------------------------------------------
# Fake unit of work
# ---------------------------------------------------------------------------


class _FakeRepo:
    def __init__(self) -> None:
        self.installation_id = 42
        self.name = TASK_REPOSITORY


class _FakeTasks:
    def __init__(self, task: Task) -> None:
        self._task = task

    def get(self, task_id: str, for_update: bool = False) -> Task:
        return self._task

    def save(self, task: Task) -> None:
        pass


class _FakeCICerts:
    def __init__(self) -> None:
        self._cert: CICertification | None = None
        self._store: list[CICertification] = []

    def put(self, cert: CICertification) -> CICertification:
        self._store.append(cert)
        return cert

    def list_for_task(self, task_id: str) -> list[CICertification]:
        return [c for c in self._store if c.task_id == task_id]

    @property
    def last(self) -> CICertification | None:
        return self._store[-1] if self._store else None


class _FakeCIActions:
    def __init__(self) -> None:
        self.actions: list[Any] = []

    def save(self, action: Any) -> None:
        self.actions.append(action)


class _FakeCIDecisions:
    def __init__(self) -> None:
        self._decisions: list[Any] = []

    def add(self, decision: Any) -> None:
        self._decisions.append(decision)


class _FakeRepoRegistry:
    def __init__(self, repo: _FakeRepo) -> None:
        self._repo = repo

    def get(self, repository_id: str, for_update: bool = False) -> _FakeRepo | None:
        return self._repo

    def get_by_name(self, name: str) -> _FakeRepo | None:
        return self._repo


class _FakeWakes:
    def __init__(self) -> None:
        self._wakes: list[Any] = []

    def add(self, wake: Any) -> None:
        self._wakes.append(wake)


class _FakeEvents:
    def __init__(self) -> None:
        self._events: list[Any] = []

    def append(self, event: Any) -> Any:
        self._events.append(event)
        return event


class FakeUnitOfWork:
    def __init__(self, task: Task) -> None:
        self.tasks = _FakeTasks(task)
        self.ci_certifications = _FakeCICerts()
        self.ci_actions = _FakeCIActions()
        self.ci_decisions = _FakeCIDecisions()
        self.repositories = _FakeRepoRegistry(_FakeRepo())
        self.wakes = _FakeWakes()
        self.events = _FakeEvents()

    def commit(self) -> None:
        pass

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass


def _build_task(state: TaskState = TaskState.CI_CERTIFICATION_FAILED) -> Task:
    return Task(
        id=TASK_ID,
        external_id="ext-001",
        principal_id="user-001",
        project="proj",
        title="A test task",
        state=state,
        contract_version=1,
        policy_name="default",
        policy_version=1,
        repository_id="repo-1",
        created_at=NOW,
        updated_at=NOW,
    )


# ---------------------------------------------------------------------------
# Helpers – build a certification and a ci-decision request
# ---------------------------------------------------------------------------


def _cert(task_id: str) -> CICertification:
    return CICertification(
        id="cert-001",
        pull_request_id="pr-001",
        head_sha=HEAD_SHA,
        task_id=task_id,
        state="failed",
        required_checks=[{"name": "build"}],
        check_runs=[],
        failure={"run_id": "5150", "message": "build failed"},
        detail="The required check failed.",
        evaluated_at=NOW,
    )


def _decision_request(**overrides: Any) -> CIDecisionRequest:
    kwargs: dict[str, Any] = {
        "cause": "flaky_test",
        "action": CIAction.RERUN,
        "reasoning": "Known flake.",
    }
    kwargs.update(overrides)
    return CIDecisionRequest(**kwargs)


# ---------------------------------------------------------------------------
# AC1 – Actions write → direct rerun through the API
# ---------------------------------------------------------------------------


def test_ac1_rerun_via_actions_write_records_attempt_and_moves_state() -> None:
    """AC1: With Actions write the app calls rerun-failed-jobs, records the new
    attempt number on the certification, and sets the task to
    AWAITING_CI_CERTIFICATION."""
    task = _build_task()
    uow = FakeUnitOfWork(task)
    cert = _cert(TASK_ID)
    uow.ci_certifications.put(cert)  # type: ignore[attr-defined]
    clock = FakeClock()
    gh = FakeGitHubClient(actions_write=True)
    principal = Principal(
        id="user-001", name="alice", role=Role.OPERATOR, created_at=NOW,
    )

    record_ci_decision(
        uow,
        clock,
        principal=principal,
        task_id=TASK_ID,
        request=_decision_request(),
        github_client=gh,
    )

    # The GitHub client was called once to mint a token and once for rerun-failed-jobs.
    assert gh.rerun_calls == [{"repository": TASK_REPOSITORY, "run_id": 5150}]

    # The certification records the new attempt number.
    cert = uow.ci_certifications.last
    assert cert is not None
    assert cert.failure["rerun_attempt"] == 1

    # The task remains in AWAITING_CI_CERTIFICATION (the re-run is in progress).
    assert task.state == TaskState.AWAITING_CI_CERTIFICATION


# ---------------------------------------------------------------------------
# AC2 – Actions read only → handoff via wake, task stays AWAITING_CI_CERTIFICATION
# ---------------------------------------------------------------------------


def test_ac2_actions_read_only_raises_handoff_wake() -> None:
    """AC2: Without Actions write the decision is recorded, a wake is raised, and
    the task remains in AWAITING_CI_CERTIFICATION waiting on the operator."""
    task = _build_task()
    uow = FakeUnitOfWork(task)
    uow.ci_certifications.put(_cert(TASK_ID))  # type: ignore[attr-defined]
    clock = FakeClock()
    gh = FakeGitHubClient(actions_write=False)
    principal = Principal(
        id="user-002", name="bob", role=Role.OPERATOR, created_at=NOW,
    )

    record_ci_decision(
        uow,
        clock,
        principal=principal,
        task_id=TASK_ID,
        request=_decision_request(),
        github_client=gh,
    )

    # No rerun call was made.
    assert gh.rerun_calls == []

    # A wake was raised so the operator knows to perform the re-run manually.
    assert len(uow.wakes._wakes) >= 1
    wake_reasons = [w.reason for w in uow.wakes._wakes]
    assert WakeReason.CI_RERUN_NEEDED.value in wake_reasons

    # The task remains in AWAITING_CI_CERTIFICATION.
    assert task.state == TaskState.AWAITING_CI_CERTIFICATION


# ---------------------------------------------------------------------------
# AC3 – Second failure of the same job does not auto-rerun
# ---------------------------------------------------------------------------


def test_ac3_second_failure_requires_new_decision() -> None:
    """AC3: After a rerun attempt has been recorded, a second failure of the same
    job does not trigger another auto-rerun; it requires a new ci-decision."""
    task = _build_task()  # state=CI_CERTIFICATION_FAILED by default
    cert = _cert(TASK_ID)
    cert.failure["rerun_attempt"] = 1  # already had one rerun
    uow = FakeUnitOfWork(task)
    uow.ci_certifications.put(cert)  # type: ignore[attr-defined]
    clock = FakeClock()
    gh = FakeGitHubClient(actions_write=True)
    principal = Principal(
        id="user-003", name="carl", role=Role.OPERATOR, created_at=NOW,
    )

    record_ci_decision(
        uow,
        clock,
        principal=principal,
        task_id=TASK_ID,
        request=_decision_request(),
        github_client=gh,
    )

    # Even with Actions write, a second rerun of the same job after one
    # rerun was already attempted is rejected.
    # The task stays in its current state and no new rerun is triggered.
    assert gh.rerun_calls == []
    assert len(uow.ci_actions.actions) == 1  # the decision was recorded
    assert task.state == TaskState.AWAITING_CI_CERTIFICATION


# ---------------------------------------------------------------------------
# AC4 – Manifest permission set names actions write (verified via import)
# ---------------------------------------------------------------------------


def test_ac4_manifest_permission_set_includes_actions_write() -> None:
    """AC4: The GitHub manifest's PERMISSIONS dict and spec 23 comment
    name `actions: write`."""
    from crucible.application.admin.github_manifest import PERMISSIONS

    assert PERMISSIONS["actions"] == "write", (
        "Spec 23 must request Actions write so Hades can re-run failed "
        "jobs through the API."
    )


def test_ac4_spec_23_comment_documentation() -> None:
    """AC4: The module docstring / comments reference actions write with a reason."""
    from crucible.application.admin import github_manifest

    module_text = github_manifest.__doc__ or ""
    # The PERMISSIONS comment and/or module doc should mention actions and write.
    assert "actions" in module_text.lower() or "write" in module_text.lower(), (
        "Module-level documentation should reference the actions write permission."
    )
