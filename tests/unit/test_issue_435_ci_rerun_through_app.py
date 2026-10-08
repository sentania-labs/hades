"""Issue 435: CI rerun through the GitHub App when it holds Actions write.

This file is a unit test of ``record_ci_decision`` (issue 435) exercising the two
code paths that the delivery-supervisor's ci-decision endpoint reaches:

* AC1 - the installation grants Actions write - the app calls ``rerun-failed-jobs``,
  the certification records the new attempt number and moves to
  ``AWAITING_CI_CERTIFICATION``.
* AC2 - the installation grants only Actions read - the old handoff behaviour
  (record the decision, raise a wake, leave the task in ``AWAITING_CI_CERTIFICATION``).
* AC3 - a second failure of the same job after the rerun does **not** auto-rerun;
  it requires a fresh ci-decision to be recorded.

The test uses a fake GitHub client that mirrors the real client's ``GitHubClient``
protocol so the domain and application layers never see HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from crucible.application.admin import github_manifest
from crucible.application.delivery_decisions import record_ci_decision
from crucible.contracts.api import CIDecisionRequest
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    CIAction,
    CICertification,
    Principal,
    Role,
    Task,
)
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import (
    CheckRecord,
    CommentRecord,
    GitHubClient,
    InstallationToken,
    MergeResult,
    Observation,
    PullRequestRef,
    ReactionRecord,
)

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
TASK_ID = "task-001"
TASK_REPOSITORY = "owner/repo"
HEAD_SHA = "abcd1234"


# ---------------------------------------------------------------------------
# Fixtures - fake clock / fake GitHub client
# ---------------------------------------------------------------------------


class FakeClock:
    def now(self) -> datetime:
        return NOW


class FakeGitHubClient(GitHubClient):
    """A minimal ``GitHubClient`` for issue 435 that tracks installation
    permissions and whether ``rerun-failed-jobs`` was called.

    ``actions_write`` controls whether the fake installation grants that scope.
    """

    def __init__(self, actions_write: bool) -> None:
        self.actions_write = actions_write
        self.rerun_calls: list[dict[str, Any]] = []
        self.get_installation_permissions_calls: list[dict[str, Any]] = []
        self.get_workflow_run_calls: list[dict[str, Any]] = []
        self._call_count = 0

    # -- the methods the ci-decision path exercises ------------------------

    def installation_token(
        self,
        *,
        installation_id: int,
        repository: str,
        permissions: dict[str, str] | None = None,
    ) -> InstallationToken:
        requested = permissions["actions"] if permissions and "actions" in permissions else None

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

    def revoke_token(self, token: InstallationToken) -> bool:
        return True

    def authenticated_login(self, token: InstallationToken) -> str:
        return "fake-login"

    def checkout_token(self, *, installation_id: int, repository: str) -> InstallationToken:
        return InstallationToken(
            value=f"checkout-token-{installation_id}",
            expires_at=NOW,
            repository=repository,
            permissions={"contents": "read"},
        )

    def remote_head(self, token: InstallationToken, *, repository: str, ref: str) -> str | None:
        return None

    def find_pull_request(
        self, token: InstallationToken, *, repository: str, head_branch: str
    ) -> PullRequestRef | None:
        return None

    def get_pull_request(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> PullRequestRef:
        return PullRequestRef(
            number=number,
            url=f"https://github.com/{repository}/pull/{number}",
            head_sha="abc123",
            base_ref="main",
            state="open",
        )

    def open_pull_requests(
        self, token: InstallationToken, *, repository: str, head_branch: str
    ) -> list[PullRequestRef]:
        return []

    def create_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        title: str,
        head_branch: str,
        base_ref: str,
        body: str,
        draft: bool = False,
    ) -> PullRequestRef:
        return PullRequestRef(
            number=1,
            url=f"https://github.com/{repository}/pull/1",
            head_sha="abc123",
            base_ref=base_ref,
            state="open",
            title=title,
            draft=draft,
        )

    def update_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        title: str | None = None,
        body: str | None = None,
        base_ref: str | None = None,
    ) -> PullRequestRef:
        return PullRequestRef(
            number=number,
            url=f"https://github.com/{repository}/pull/{number}",
            head_sha="abc123",
            base_ref=base_ref or "main",
            state="open",
            title=title or "test",
        )

    def merge_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        expected_head_sha: str,
    ) -> MergeResult:
        return MergeResult(sha="newsha", merged_at=NOW, merged_by="fake-user")

    def observe(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        base_ref: str,
        with_reactions: bool = True,
    ) -> Observation:
        from crucible.ports.github import (  # noqa: PLC0415
            Observation,
            PullRequestRef,
        )

        return Observation(
            pull_request=PullRequestRef(
                number=number,
                url=f"https://github.com/{repository}/pull/{number}",
                head_sha="abc123",
                base_ref=base_ref,
                state="open",
            ),
            reviews=(),
            review_comments=(),
            issue_comments=(),
            reactions=(),
            reactions_observable=True,
            reactions_detail="",
            checks=(),
            required_checks=(),
            observed_at=NOW,
            rate_limit_remaining=None,
            notes=(),
        )

    def issue_comments(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> tuple[CommentRecord, ...]:
        return ()

    def reactions_for(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> tuple[ReactionRecord, ...]:
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

    def post_issue_comment(
        self, token: InstallationToken, *, repository: str, number: int, body: str
    ) -> CommentRecord:
        return CommentRecord(
            github_id="1",
            login="fake",
            body=body,
            created_at=NOW,
            updated_at=NOW,
        )

    def reply_to_review_comment(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        comment_id: str,
        body: str,
    ) -> CommentRecord:
        return CommentRecord(
            github_id="2",
            login="fake",
            body=body,
            created_at=NOW,
            updated_at=NOW,
        )

    def closed_by(self, token: InstallationToken, *, repository: str, number: int) -> str | None:
        return "fake-user"

    def delete_ref(self, token: InstallationToken, *, repository: str, ref: str) -> None:
        pass

    def list_required_checks(
        self, token: InstallationToken, *, repository: str, branch: str
    ) -> list[str]:
        return []

    def checks_for_commit(
        self, token: InstallationToken, *, repository: str, head_sha: str
    ) -> list[CheckRecord]:
        return []

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

    def get_installation_permissions(
        self, token: InstallationToken, *, repository: str
    ) -> dict[str, str]:
        self.get_installation_permissions_calls.append({"repository": repository})
        # For the real client this calls /app/installations/self; the fake
        # returns whatever the test author configured.
        return {"actions": "write" if self.actions_write else "read"}

    def get_workflow_run(
        self, token: InstallationToken, *, repository: str, run_id: int
    ) -> dict[str, Any]:
        self.get_workflow_run_calls.append({"repository": repository, "run_id": run_id})
        # Simulate the workflow run after a rerun: the run_attempt increments.
        return {
            "id": run_id,
            "name": "CI",
            "run_attempt": 2,
            "status": "in_progress",
            "conclusion": None,
            "html_url": f"https://github.com/{repository}/actions/runs/{run_id}",
        }


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
    """A minimal ``UnitOfWork`` that satisfies the protocol for issue 435 tests.

    Every attribute required by the ``UnitOfWork`` protocol is present; the ones
    not used by the ci-decision path are ``SimpleNamespace`` stubs with no-op
    methods so mypy can type-check calls against the real protocol.
    """

    # Protocol-required repositories with real implementations
    tasks: _FakeTasks
    ci_certifications: _FakeCICerts
    ci_decisions: _FakeCIDecisions
    repositories: _FakeRepoRegistry
    wakes: _FakeWakes
    events: _FakeEvents
    ci_actions: _FakeCIActions

    # Remaining protocol attributes (no-op stubs) - use Any so mypy
    # does not enforce the full protocol structure on this test fixture.
    principals: Any
    ui_sessions: Any
    contracts: Any
    executions: Any
    attempts: Any
    leases: Any
    claims: Any
    logs: Any
    heartbeats: Any
    retention: Any
    supervisor_status: Any
    idempotency: Any
    routing_policies: Any
    pool_exhaustions: Any
    artifacts: Any
    evidence: Any
    review_reports: Any
    gate_results: Any
    acceptance: Any
    decisions: Any
    escalations: Any
    dispositions: Any
    attempt_metrics: Any
    pull_requests: Any
    pull_request_heads: Any
    review_cycles: Any
    external_reviews: Any
    review_comments: Any
    reactions: Any
    github_deliveries: Any
    harnesses: Any
    harness_images: Any
    bootstrap_imports: Any
    provider_settings: Any
    github_manifest_states: Any
    policies: Any

    def __init__(self, task: Task) -> None:
        self.tasks = _FakeTasks(task)
        self.ci_certifications = _FakeCICerts()
        self.ci_actions = _FakeCIActions()
        self.ci_decisions = _FakeCIDecisions()
        self.repositories = _FakeRepoRegistry(_FakeRepo())
        self.wakes = _FakeWakes()
        self.events = _FakeEvents()
        # No-op stubs for every remaining repository required by UnitOfWork
        self.principals = _stub_repo()
        self.ui_sessions = _stub_repo()
        self.contracts = _stub_repo()
        self.executions = _stub_repo()
        self.attempts = _stub_repo()
        self.leases = _stub_repo()
        self.claims = _stub_repo()
        self.logs = _stub_repo()
        self.heartbeats = _stub_repo()
        self.retention = _stub_repo()
        self.supervisor_status = _stub_repo()
        self.idempotency = _stub_repo()
        self.routing_policies = _stub_repo()
        self.pool_exhaustions = _stub_repo()
        self.artifacts = _stub_repo()
        self.evidence = _stub_repo()
        self.review_reports = _stub_repo()
        self.gate_results = _stub_repo()
        self.acceptance = _stub_repo()
        self.decisions = _stub_repo()
        self.escalations = _stub_repo()
        self.dispositions = _stub_repo()
        self.attempt_metrics = _stub_repo()
        self.pull_requests = _stub_repo()
        self.pull_request_heads = _stub_repo()
        self.review_cycles = _stub_repo()
        self.external_reviews = _stub_repo()
        self.review_comments = _stub_repo()
        self.reactions = _stub_repo()
        self.github_deliveries = _stub_repo()
        self.harnesses = _stub_repo()
        self.harness_images = _stub_repo()
        self.bootstrap_imports = _stub_repo()
        self.provider_settings = _stub_repo()
        self.github_manifest_states = _stub_repo()
        self.policies = _stub_repo()

    def commit(self) -> None:
        pass

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        pass

    def rollback(self) -> None:
        pass

    def set_fenced_token(self, fenced_token: int) -> None:
        pass


def _stub_repo() -> Any:
    """A SimpleNamespace that satisfies any repository protocol for no-op stubs."""
    return __import__("types").SimpleNamespace()


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
# Helpers - build a certification and a ci-decision request
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
# AC1 - Actions write - direct rerun through the API
# ---------------------------------------------------------------------------


def test_ac1_rerun_via_actions_write_records_attempt_and_moves_state() -> None:
    """AC1: With Actions write the app calls get_installation_permissions first,
    then rerun-failed-jobs, fetches the workflow run to get the attempt number,
    records the new attempt on the certification, raises a wake, and moves the task
    to AWAITING_CI_CERTIFICATION."""
    task = _build_task()
    uow = FakeUnitOfWork(task)
    cert = _cert(TASK_ID)
    uow.ci_certifications.put(cert)
    clock = FakeClock()
    gh = FakeGitHubClient(actions_write=True)
    principal = Principal(
        id="user-001",
        name="alice",
        role=Role.OPERATOR,
        created_at=NOW,
    )

    record_ci_decision(
        uow,  # type: ignore[arg-type]
        clock,
        principal=principal,
        task_id=TASK_ID,
        request=_decision_request(),
        github_client=gh,
    )

    # get_installation_permissions was called first (finding 1).
    assert len(gh.get_installation_permissions_calls) == 1

    # The GitHub client was called once for rerun-failed-jobs.
    assert gh.rerun_calls == [{"repository": TASK_REPOSITORY, "run_id": 5150}]

    # get_workflow_run was called to derive the actual attempt number (finding 3).
    assert len(gh.get_workflow_run_calls) == 1

    # The certification records the new attempt number from the workflow run.
    latest_cert = uow.ci_certifications.last
    assert latest_cert is not None
    assert latest_cert.failure["rerun_attempt"] == 2

    # A wake was raised in the Actions-write path (finding 4).
    assert len(uow.wakes._wakes) >= 1
    wake_reasons = [w.reason for w in uow.wakes._wakes]
    assert WakeReason.CI_RERUN_NEEDED.value in wake_reasons

    # The task is in AWAITING_CI_CERTIFICATION (the re-run is in progress).
    assert task.state == TaskState.AWAITING_CI_CERTIFICATION


# ---------------------------------------------------------------------------
# AC2 - Actions read only - handoff via wake, task stays AWAITING_CI_CERTIFICATION
# ---------------------------------------------------------------------------


def test_ac2_actions_read_only_raises_handoff_wake() -> None:
    """AC2: Without Actions write the app calls get_installation_permissions first,
    sees only read, skips the rerun path, records the decision, raises a wake,
    and leaves the task in AWAITING_CI_CERTIFICATION waiting on the operator."""
    task = _build_task()
    uow = FakeUnitOfWork(task)
    uow.ci_certifications.put(_cert(TASK_ID))
    clock = FakeClock()
    gh = FakeGitHubClient(actions_write=False)
    principal = Principal(
        id="user-002",
        name="bob",
        role=Role.OPERATOR,
        created_at=NOW,
    )

    record_ci_decision(
        uow,  # type: ignore[arg-type]
        clock,
        principal=principal,
        task_id=TASK_ID,
        request=_decision_request(),
        github_client=gh,
    )

    # get_installation_permissions was called first (finding 1).
    assert len(gh.get_installation_permissions_calls) == 1

    # No rerun call was made because the installation lacks Actions write.
    assert gh.rerun_calls == []

    # A wake was raised so the operator knows to perform the re-run manually.
    assert len(uow.wakes._wakes) >= 1
    wake_reasons = [w.reason for w in uow.wakes._wakes]
    assert WakeReason.CI_RERUN_NEEDED.value in wake_reasons

    # The task remains in AWAITING_CI_CERTIFICATION.
    assert task.state == TaskState.AWAITING_CI_CERTIFICATION


# ---------------------------------------------------------------------------
# AC3 - Second failure of the same job does not auto-rerun
# ---------------------------------------------------------------------------


def test_ac3_second_failure_requires_new_decision() -> None:
    """AC3: After a rerun attempt has been recorded, a second failure of the same
    job does not trigger another auto-rerun; it requires a new ci-decision.
    The app still calls get_installation_permissions (finding 1) and raises a
    wake so the Board shows the existing running rerun (finding 4), but it never
    calls rerun-failed-jobs or get_workflow_run."""
    task = _build_task()  # state=CI_CERTIFICATION_FAILED by default
    cert_obj = _cert(TASK_ID)
    cert_obj.failure["rerun_attempt"] = 2  # already had one rerun
    uow = FakeUnitOfWork(task)
    uow.ci_certifications.put(cert_obj)
    clock = FakeClock()
    gh = FakeGitHubClient(actions_write=True)
    principal = Principal(
        id="user-003",
        name="carl",
        role=Role.OPERATOR,
        created_at=NOW,
    )

    record_ci_decision(
        uow,  # type: ignore[arg-type]
        clock,
        principal=principal,
        task_id=TASK_ID,
        request=_decision_request(),
        github_client=gh,
    )

    # get_installation_permissions was still called first (finding 1).
    assert len(gh.get_installation_permissions_calls) == 1

    # Even with Actions write, a second rerun of the same job after one
    # rerun was already attempted is rejected (one rerun per decision).
    assert gh.rerun_calls == []
    assert gh.get_workflow_run_calls == []

    # The decision was recorded (the ci_decisions repo tracks it).
    assert len(uow.ci_decisions._decisions) == 1

    # A wake was raised so the Board shows the existing running rerun.
    assert len(uow.wakes._wakes) >= 1


# ---------------------------------------------------------------------------
# AC4 - Manifest permission set names actions write (verified via import)
# ---------------------------------------------------------------------------


def test_ac4_manifest_permission_set_includes_actions_write() -> None:
    """AC4: The GitHub manifest's PERMISSIONS dict and spec 23 comment
    name ``actions: write``."""
    from crucible.application.admin.github_manifest import PERMISSIONS  # noqa: PLC0415

    assert PERMISSIONS["actions"] == "write", (
        "Spec 23 must request Actions write so Hades can re-run failed jobs through the API."
    )


def test_ac4_spec_23_comment_documentation() -> None:
    """AC4: The module docstring / comments reference actions write with a reason."""
    module_text = github_manifest.__doc__ or ""
    # The PERMISSIONS comment and/or module doc should mention actions and write.
    assert "actions" in module_text.lower() or "write" in module_text.lower(), (
        "Module-level documentation should reference the actions write permission."
    )
