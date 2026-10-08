"""Issue 435: a CI rerun decision re-runs the failed jobs through the GitHub App when the
installation grants Actions write, and hands off to the operator when it does not.

* AC1: with Actions write, ``record_ci_decision`` reads the installation's grant, calls
  ``rerun-failed-jobs`` once for the decided head's workflow run, reads the run back and
  records the new attempt on the certification.
* AC2: with Actions read only, the decision is recorded, the ``ci_rerun_needed`` wake is
  raised, and the Board and the task page say the task is waiting on a re-run, then
  "re-run requested, attempt N running" once one runs.
* AC3: a second failure of the same job after the rerun is a new failure; nothing re-runs
  it until a new decision does, and that decision re-runs once.
* AC4: the App manifest and spec 23 name Actions write and why.

The GitHub client is a fake that mirrors the ``GitHubClient`` port, so no HTTP is made
except in the tests of the real client, which use a recording transport.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, cast
from unittest.mock import MagicMock, patch

from crucible.adapters.github import normalize
from crucible.adapters.github.appauth import AppAuthenticator
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.transport import RestTransport
from crucible.application.admin import github_manifest
from crucible.application.admin.board import _holder, rerun_line, waiting_line
from crucible.application.delivery_decisions import record_ci_decision
from crucible.application.observation import certify_head
from crucible.contracts.api import CIDecisionRequest
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    CIAction,
    CICertification,
    Principal,
    Role,
    Task,
    Wake,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import (
    CheckRecord,
    CommentRecord,
    CommitDiffRecord,
    GitHubClient,
    GitHubError,
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

    def __init__(
        self,
        actions_write: bool,
        *,
        rerun_refusal: GitHubError | None = None,
        jobs: dict[int, int] | None = None,
    ) -> None:
        self.actions_write = actions_write
        self.rerun_refusal = rerun_refusal
        # job id -> workflow run id, for failures observed as Actions check runs.
        self.jobs = jobs or {}
        self.rerun_calls: list[dict[str, Any]] = []
        self.get_installation_permissions_calls: list[dict[str, Any]] = []
        self.get_workflow_run_calls: list[dict[str, Any]] = []
        self.token_requests: list[dict[str, str] | None] = []
        self.revoked: list[str] = []
        # The attempt each workflow run is on, as GitHub would report it.
        self.attempts: dict[int, int] = {}
        self._call_count = 0

    # -- the methods the ci-decision path exercises ------------------------

    def installation_token(
        self,
        *,
        installation_id: int,
        repository: str,
        permissions: dict[str, str] | None = None,
    ) -> InstallationToken:
        self.token_requests.append(permissions)
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
        self.revoked.append(token.reveal())
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

    def diff_commits(
        self,
        token: InstallationToken,
        *,
        repository: str,
        base_sha: str,
        head_sha: str,
    ) -> tuple[CommitDiffRecord, ...]:
        return ()

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
        if self.rerun_refusal is not None:
            raise self.rerun_refusal
        self.attempts[run_id] = self.attempts.get(run_id, 1) + 1
        # GitHub answers 201 with no body.
        return {}

    def get_installation_permissions(self, *, installation_id: int) -> dict[str, str]:
        self.get_installation_permissions_calls.append({"installation_id": installation_id})
        return {
            "metadata": "read",
            "checks": "read",
            "actions": "write" if self.actions_write else "read",
        }

    def workflow_run_for_job(
        self, token: InstallationToken, *, repository: str, job_id: int
    ) -> int | None:
        return self.jobs.get(job_id)

    def get_workflow_run(
        self, token: InstallationToken, *, repository: str, run_id: int
    ) -> dict[str, Any]:
        self.get_workflow_run_calls.append({"repository": repository, "run_id": run_id})
        return {"id": run_id, "status": "queued", "run_attempt": self.attempts.get(run_id, 1)}


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
        head_sha=HEAD_SHA,
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
        failure={
            "check": "CI",
            "run_id": "5150",
            "source": "workflow_run",
            "all": [
                {
                    "name": "CI",
                    "run_id": "5150",
                    "source": "workflow_run",
                    "completed_at": "2026-10-07T11:00:00+00:00",
                }
            ],
        },
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


def _cert_for_jobs(*job_ids: int) -> CICertification:
    """A failure observed as Actions check runs: each run id is a job id."""
    cert = _cert(TASK_ID)
    cert.failure = {
        "check": "build",
        "run_id": str(job_ids[0]),
        "source": "check_run",
        "all": [
            {"name": f"job-{job}", "run_id": str(job), "source": "check_run"} for job in job_ids
        ],
    }
    return cert


def _principal() -> Principal:
    return Principal(id="user-001", name="foundry", role=Role.ORCHESTRATOR, created_at=NOW)


def _decide(
    gh: FakeGitHubClient | None,
    *,
    cert: CICertification | None = None,
    task: Task | None = None,
    uow: FakeUnitOfWork | None = None,
) -> tuple[Task, FakeUnitOfWork]:
    task = task or _build_task()
    if uow is None:
        uow = FakeUnitOfWork(task)
        uow.ci_certifications.put(cert or _cert(TASK_ID))
    record_ci_decision(
        uow,  # type: ignore[arg-type]
        FakeClock(),
        principal=_principal(),
        task_id=TASK_ID,
        request=_decision_request(),
        github_client=gh,
    )
    return task, uow


def _wakes(uow: FakeUnitOfWork) -> list[Wake]:
    return list(uow.wakes._wakes)


def _decision_event(uow: FakeUnitOfWork) -> Any:
    return next(e for e in uow.events._events if e.kind == EventKind.CI_DECISION_RECORDED.value)


def _workflow_run(
    *, status: str, conclusion: str | None, attempt: int, completed_at: datetime | None = None
) -> CheckRecord:
    return CheckRecord(
        name="CI",
        status=status,
        conclusion=conclusion,
        head_sha=HEAD_SHA,
        external_id="5150",
        workflow=".github/workflows/ci.yml",
        source="workflow_run",
        completed_at=completed_at,
        run_attempt=attempt,
    )


def _observe(
    decision_event: Any, previous: CICertification | None, *checks: CheckRecord
) -> tuple[CICertification, Any]:
    """One CI poll of the decided head: the certification and the event it recorded."""
    uow = MagicMock()
    uow.events.latest_for_task_kind.return_value = decision_event
    uow.ci_certifications.get_for_head.return_value = previous
    uow.ci_certifications.put.side_effect = lambda certification: certification
    with patch("crucible.application.observation.task_waivers", return_value={}):
        certification = certify_head(
            uow,
            MagicMock(now=MagicMock(return_value=NOW)),
            task=_build_task(TaskState.AWAITING_CI_CERTIFICATION),
            pull_request=MagicMock(id="pr-001"),
            observation=Observation(pull_request=MagicMock(), checks=checks),
            policy={"ci_certification": {"required_checks": []}},
            head_sha=HEAD_SHA,
        )
    recorded = uow.events.append.call_args.args[0]
    return certification, recorded


# ---------------------------------------------------------------------------
# AC1: Actions write - Hades re-runs the failed jobs itself
# ---------------------------------------------------------------------------


def test_ac1_actions_write_reruns_the_decided_run_once_and_records_the_attempt() -> None:
    gh = FakeGitHubClient(actions_write=True)
    task, uow = _decide(gh)

    # The grant was read from the installation, not assumed.
    assert gh.get_installation_permissions_calls == [{"installation_id": 42}]
    assert gh.token_requests == [{"actions": "write", "metadata": "read"}]
    # One rerun-failed-jobs call, for the decided head's workflow run.
    assert gh.rerun_calls == [{"repository": TASK_REPOSITORY, "run_id": 5150}]
    # The attempt GitHub reports for the run afterwards is on the certification.
    certification = uow.ci_certifications.last
    assert certification is not None
    assert certification.failure["rerun_attempt"] == 2
    assert certification.failure["rerun_decision"] == uow.ci_decisions._decisions[0].id
    # The token is revoked and the task waits for that attempt's result.
    assert gh.revoked == ["fake-token-1"]
    assert task.state is TaskState.AWAITING_CI_CERTIFICATION
    [wake] = _wakes(uow)
    assert wake.reason == WakeReason.CI_RERUN_NEEDED.value
    assert wake.payload["summary"].startswith("re-run requested, attempt 2 running")


def test_ac1_failed_actions_jobs_are_resolved_to_their_run_and_rerun_once() -> None:
    # Two failed jobs of one workflow run: their check-run ids are job ids.
    gh = FakeGitHubClient(actions_write=True, jobs={777: 5150, 778: 5150})
    _decide(gh, cert=_cert_for_jobs(777, 778))

    assert gh.rerun_calls == [{"repository": TASK_REPOSITORY, "run_id": 5150}]


def test_ac1_a_github_refusal_falls_back_to_the_operator_handoff() -> None:
    gh = FakeGitHubClient(
        actions_write=True, rerun_refusal=GitHubError(403, "run is too old", path="/x")
    )
    task, uow = _decide(gh)

    assert len(gh.rerun_calls) == 1
    certification = uow.ci_certifications.last
    assert certification is not None
    assert "rerun_attempt" not in certification.failure
    assert task.state is TaskState.AWAITING_CI_CERTIFICATION
    [wake] = _wakes(uow)
    assert wake.payload["summary"].startswith("waiting on a re-run")
    assert gh.revoked == ["fake-token-1"]


def test_ac1_the_attempt_stays_on_the_certification_and_its_result_is_judged() -> None:
    gh = FakeGitHubClient(actions_write=True)
    _task, uow = _decide(gh)
    decided = uow.ci_certifications.last
    event = _decision_event(uow)

    # While attempt 2 runs, the certification names it.
    running, _ = _observe(
        event, decided, _workflow_run(status="in_progress", conclusion=None, attempt=2)
    )
    assert running.state == "pending"
    assert running.detail == "re-run requested, attempt 2 running"
    assert running.failure["rerun_attempt"] == 2

    # Attempt 2's result is judged like any other: green certifies the head.
    green, _ = _observe(
        event,
        running,
        _workflow_run(status="completed", conclusion="success", attempt=2, completed_at=NOW),
    )
    assert green.state == "green"


def test_ac1_the_rest_client_uses_the_issue_435_endpoints() -> None:
    transport = _RecordingTransport(
        {
            ("GET", "/app/installations/42"): (200, {"permissions": {"actions": "write"}}),
            ("POST", "/repos/owner/repo/actions/runs/5150/rerun-failed-jobs"): (201, None),
            ("GET", "/repos/owner/repo/actions/runs/5150"): (200, {"run_attempt": 2}),
            ("GET", "/repos/owner/repo/actions/jobs/777"): (200, {"run_id": 5150}),
            ("GET", "/repos/owner/repo/actions/jobs/9"): (404, {"message": "Not Found"}),
        }
    )
    client = RestGitHubClient(
        cast(AppAuthenticator, MagicMock(app_jwt=MagicMock(return_value="app-jwt"))),
        cast(RestTransport, transport),
    )
    token = InstallationToken(value="tok", expires_at=NOW, repository="owner/repo")

    assert client.get_installation_permissions(installation_id=42) == {"actions": "write"}
    # GitHub's 201 has no body; that is success, not an error.
    assert client.rerun_failed_jobs(token, repository="owner/repo", run_id=5150) == {}
    assert client.get_workflow_run(token, repository="owner/repo", run_id=5150)["run_attempt"] == 2
    assert client.workflow_run_for_job(token, repository="owner/repo", job_id=777) == 5150
    assert client.workflow_run_for_job(token, repository="owner/repo", job_id=9) is None
    # The installation's grant is read with the App JWT, the rest with the token.
    assert transport.bearers[0] == ("/app/installations/42", "app-jwt")
    assert all(bearer == "tok" for _path, bearer in transport.bearers[1:])


def test_ac1_an_unreadable_grant_is_no_grant() -> None:
    transport = _RecordingTransport({("GET", "/app/installations/42"): (404, {"message": "x"})})
    client = RestGitHubClient(
        cast(AppAuthenticator, MagicMock(app_jwt=MagicMock(return_value="app-jwt"))),
        cast(RestTransport, transport),
    )
    assert client.get_installation_permissions(installation_id=42) == {}


def test_ac1_a_workflow_run_carries_its_attempt() -> None:
    record = normalize.workflow_run(
        {"id": 5150, "name": "CI", "status": "in_progress", "head_sha": HEAD_SHA, "run_attempt": 3}
    )
    assert record.run_attempt == 3


# ---------------------------------------------------------------------------
# AC2: Actions read only - the wake is the hand-off, the Board and task page say so
# ---------------------------------------------------------------------------


def test_ac2_actions_read_only_records_the_decision_and_raises_the_wake() -> None:
    gh = FakeGitHubClient(actions_write=False)
    task, uow = _decide(gh)

    assert gh.get_installation_permissions_calls == [{"installation_id": 42}]
    # No write token is asked for and nothing is re-run.
    assert gh.token_requests == []
    assert gh.rerun_calls == []
    assert len(uow.ci_decisions._decisions) == 1
    assert _decision_event(uow).payload["action"] == "rerun"
    assert task.state is TaskState.AWAITING_CI_CERTIFICATION
    [wake] = _wakes(uow)
    assert wake.reason == WakeReason.CI_RERUN_NEEDED.value
    assert wake.payload["summary"].startswith("waiting on a re-run")
    assert "no Actions write" in wake.payload["summary"]


def test_ac2_without_a_client_or_grant_reading_the_handoff_stands() -> None:
    _task, uow = _decide(None)
    [wake] = _wakes(uow)
    assert wake.reason == WakeReason.CI_RERUN_NEEDED.value


def test_ac2_board_and_task_page_show_waiting_then_the_running_attempt() -> None:
    gh = FakeGitHubClient(actions_write=False)
    task, uow = _decide(gh)
    [wake] = _wakes(uow)
    event = _decision_event(uow)
    decided = uow.ci_certifications.last

    # Before an attempt runs, the Board's line is the hand-off: waiting on a re-run.
    assert waiting_line(wake, task.state).startswith("waiting on a re-run")
    assert _holder("ci", task, None, wake, None)["detail"].startswith("waiting on a re-run")
    stale, stale_event = _observe(
        event,
        decided,
        _workflow_run(
            status="completed",
            conclusion="failure",
            attempt=1,
            completed_at=datetime(2026, 10, 7, 11, 0, 0, tzinfo=UTC),
        ),
    )
    assert stale.state == "pending"
    assert stale.detail.startswith("waiting on a re-run")
    assert rerun_line(task, stale_event) is None

    # The operator re-runs it on GitHub: attempt 2 is running.
    running, running_event = _observe(
        event, stale, _workflow_run(status="in_progress", conclusion=None, attempt=2)
    )
    # The task page shows the certification's state and detail.
    assert f"{running.state}: {running.detail}" == "pending: re-run requested, attempt 2 running"
    # The Board's list view and the CI column of the kanban both say so.
    assert rerun_line(task, running_event) == "re-run requested, attempt 2 running"
    assert _holder("ci", task, None, wake, running_event)["detail"] == (
        "re-run requested, attempt 2 running"
    )


# ---------------------------------------------------------------------------
# AC3: a second failure of the same job needs a new decision
# ---------------------------------------------------------------------------


def test_ac3_a_second_failure_is_not_rerun_without_a_new_decision() -> None:
    gh = FakeGitHubClient(actions_write=True)
    task, uow = _decide(gh)
    assert len(gh.rerun_calls) == 1
    event = _decision_event(uow)

    # Attempt 2 of the same run fails again: a new failure, not the stale one.
    failed, _ = _observe(
        event,
        uow.ci_certifications.last,
        _workflow_run(status="completed", conclusion="failure", attempt=2, completed_at=NOW),
    )
    assert failed.state == "failed"
    assert failed.failure["rerun_attempt"] == 2
    # Observing it re-runs nothing: the only caller of rerun-failed-jobs is a decision.
    assert len(gh.rerun_calls) == 1
    callers = [
        path
        for path in Path("crucible").rglob("*.py")
        if "rerun_failed_jobs(" in path.read_text(encoding="utf-8")
        and "def rerun_failed_jobs(" not in path.read_text(encoding="utf-8")
    ]
    assert callers == [Path("crucible/application/delivery_decisions.py")]

    # A new decision re-runs it once more, and the next attempt is recorded.
    task.state = TaskState.CI_CERTIFICATION_FAILED
    uow.ci_certifications.put(failed)
    _decide(gh, task=task, uow=uow)
    assert gh.rerun_calls == [
        {"repository": TASK_REPOSITORY, "run_id": 5150},
        {"repository": TASK_REPOSITORY, "run_id": 5150},
    ]
    latest = uow.ci_certifications.last
    assert latest is not None
    assert latest.failure["rerun_attempt"] == 3


def test_ac3_an_earlier_decisions_attempt_is_not_shown_for_a_new_one() -> None:
    gh = FakeGitHubClient(actions_write=False)
    _task, uow = _decide(gh)
    event = _decision_event(uow)
    previous = _cert(TASK_ID)
    previous.failure = {**previous.failure, "rerun_attempt": 2, "rerun_decision": "older"}

    certification, _ = _observe(
        event,
        previous,
        _workflow_run(
            status="completed",
            conclusion="failure",
            attempt=2,
            completed_at=datetime(2026, 10, 7, 11, 0, 0, tzinfo=UTC),
        ),
    )
    assert "rerun_attempt" not in certification.failure
    assert certification.detail.startswith("waiting on a re-run")


# ---------------------------------------------------------------------------
# AC4: the manifest and spec 23 name Actions write and why
# ---------------------------------------------------------------------------


def test_ac4_the_manifest_asks_for_actions_write_and_nothing_else_new() -> None:
    assert github_manifest.PERMISSIONS == {
        "metadata": "read",
        "contents": "write",
        "pull_requests": "write",
        "checks": "read",
        "actions": "write",
        "issues": "read",
    }
    doc = github_manifest.__doc__ or ""
    assert "Actions write" in doc
    assert "re-run" in doc


def test_ac4_spec_23_names_actions_write_and_why() -> None:
    spec = Path("docs/spec/23-github-delivery.md").read_text(encoding="utf-8")
    assert "Checks read, Actions read/write, Issues read" in spec
    assert "Actions write is there so a `ci-decision` `rerun` re-runs" in spec
    assert "rerun-failed-jobs" in spec
    assert "re-run requested, attempt N running" in spec


class _RecordingTransport:
    """Answers the paths it was given and records which bearer each call used."""

    def __init__(self, answers: dict[tuple[str, str], tuple[int, Any]]) -> None:
        self.answers = answers
        self.bearers: list[tuple[str, str]] = []

    def request(
        self, method: str, path: str, *, bearer: str, **_kwargs: Any
    ) -> tuple[int, Any, dict[str, str]]:
        self.bearers.append((path, bearer))
        status, payload = self.answers[(method, path)]
        return status, payload, {}

    def get(self, path: str, *, bearer: str, **_kwargs: Any) -> Any:
        status, payload, _ = self.request("GET", path, bearer=bearer)
        if status >= 400:
            raise GitHubError(status, "refused", path=path)
        return payload
