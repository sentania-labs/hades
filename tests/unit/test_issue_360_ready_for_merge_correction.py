"""Hades #360: a correction is accepted once a task is ready_for_merge.

The application code runs as the supervisor and the API run it: `attach_correction`,
the supervisor's materialisation and cancellation sweep, and the delivery coordinator's
publication finish and pull request poll. Underneath is an in-memory store holding the
same rows the SQL repositories hold, because the unit tier has no Postgres; only GitHub
and the worker's exit are stood in for, by the fake provider and a recording client.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest
import yaml

from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.corrections import attach_correction
from crucible.application.errors import ContractValidationError, TransitionNotAllowedError
from crucible.application.publish import PublishPlan, request_external_review
from crucible.application.supervisor import Supervisor
from crucible.application.transitions import move_task
from crucible.contracts.task_contract import contract_sha256
from crucible.domain.entities import (
    Artifact,
    Attempt,
    Escalation,
    Event,
    EvidenceRecord,
    Execution,
    ExecutionRole,
    ExternalReviewCycle,
    GateResultRecord,
    HarnessState,
    Policy,
    Principal,
    ProviderSetting,
    PullRequest,
    PullRequestHead,
    PullRequestState,
    PushedBy,
    Repository,
    Role,
    RoutingPolicyRecord,
    Task,
    TaskContract,
    Wake,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import (
    AttemptState,
    ExecutionState,
    TaskState,
)
from crucible.ports.execution import CollectedOutputs
from crucible.ports.github import (
    CommentRecord,
    GitHubClient,
    InstallationToken,
    Observation,
    PullRequestRef,
)
from crucible.ports.repository import UnitOfWork
from tests.fixtures import REPOSITORY_URL, FakeClock, contract_document

ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "01TASK360READYFORMERGE001"
PRINCIPAL_ID = "01PRINC360READYFORMERGE01"
REPOSITORY_ID = "01REPO360READYFORMERGE001"
PR_ID = "01PULL360READYFORMERGE001"
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40
MERGE_SHA = "c" * 40
PR_NUMBER = 363
TRIGGER = "@codex review"
APP_LOGIN = "crucible-app[bot]"


# ----- the in-memory store -------------------------------------------------


class _Tasks:
    def __init__(self) -> None:
        self.rows: dict[str, Task] = {}

    def add(self, task: Task) -> None:
        self.rows[task.id] = task

    def get(self, task_id: str, *, for_update: bool = False) -> Task | None:
        return self.rows.get(task_id)

    def save(self, task: Task) -> None:
        self.rows[task.id] = task

    def list_by_state(self, state: TaskState, *, for_update: bool = False) -> list[Task]:
        return [t for t in self.rows.values() if t.state is state]


class _Contracts:
    def __init__(self) -> None:
        self.rows: list[TaskContract] = []

    def add(self, contract: TaskContract) -> None:
        self.rows.append(contract)

    def get(self, task_id: str, version: int) -> TaskContract | None:
        return next((c for c in self.rows if (c.task_id, c.version) == (task_id, version)), None)

    def list_for_task(self, task_id: str) -> list[TaskContract]:
        return [c for c in self.rows if c.task_id == task_id]


class _Executions:
    def __init__(self) -> None:
        self.rows: dict[str, Execution] = {}

    def add(self, execution: Execution) -> None:
        self.rows[execution.id] = execution

    def get(self, execution_id: str, *, for_update: bool = False) -> Execution | None:
        return self.rows.get(execution_id)

    def save(self, execution: Execution) -> None:
        self.rows[execution.id] = execution

    def list_for_task(self, task_id: str) -> list[Execution]:
        return sorted(
            (e for e in self.rows.values() if e.task_id == task_id), key=lambda e: e.created_at
        )


class _Attempts:
    def __init__(self) -> None:
        self.rows: dict[str, Attempt] = {}

    def add(self, attempt: Attempt) -> None:
        self.rows[attempt.id] = attempt

    def get(self, attempt_id: str, *, for_update: bool = False) -> Attempt | None:
        return self.rows.get(attempt_id)

    def save(self, attempt: Attempt) -> None:
        self.rows[attempt.id] = attempt

    def list_for_execution(self, execution_id: str) -> list[Attempt]:
        return sorted(
            (a for a in self.rows.values() if a.execution_id == execution_id), key=lambda a: a.id
        )

    def list_for_task(self, task_id: str) -> list[Attempt]:
        return sorted((a for a in self.rows.values() if a.task_id == task_id), key=lambda a: a.id)

    def list_in_states(self, states: Sequence[AttemptState]) -> list[Attempt]:
        return [a for a in self.rows.values() if a.state in states]


class _Events:
    def __init__(self) -> None:
        self.rows: list[Event] = []

    def append(self, event: Event) -> Event:
        stored = replace(event, seq=len(self.rows) + 1)
        self.rows.append(stored)
        return stored

    def list_for_task(self, task_id: str, *, after_seq: int = 0, limit: int = 1000) -> list[Event]:
        matches = [
            e
            for e in self.rows
            if getattr(e, "task_id", None) == task_id and getattr(e, "seq", 0) > after_seq
        ]
        return matches[:limit]

    def latest_for_task_kind(self, task_id: str, kind: str) -> Event | None:
        return next(
            (e for e in reversed(self.rows) if e.task_id == task_id and e.kind == kind), None
        )

    def kinds(self) -> list[str]:
        return [e.kind for e in self.rows]


class _Repositories:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def get(self, repository_id: str) -> Repository | None:
        return self.repository if repository_id == self.repository.id else None

    def get_by_name(self, name: str) -> Repository | None:
        return self.repository if name == self.repository.name else None

    def upsert(self, repository: Repository) -> Repository:
        self.repository = repository
        return self.repository


class _Policies:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def get(self, name: str, version: int) -> Policy | None:
        same = (name, version) == (self.policy.name, self.policy.version)
        return self.policy if same else None


class _RoutingPolicies:
    def __init__(self, record: RoutingPolicyRecord) -> None:
        self.record = record

    def get(self, name: str, version: int) -> RoutingPolicyRecord | None:
        same = (name, version) == (self.record.name, self.record.version)
        return self.record if same else None

    def list_versions(self, name: str) -> list[RoutingPolicyRecord]:
        return [self.record] if name == self.record.name else []


class _PullRequests:
    def __init__(self) -> None:
        self.rows: dict[str, PullRequest] = {}

    def add(self, pull_request: PullRequest) -> None:
        self.rows[pull_request.id] = pull_request

    def get(self, pull_request_id: str, *, for_update: bool = False) -> PullRequest | None:
        return self.rows.get(pull_request_id)

    def get_for_task(self, task_id: str, *, for_update: bool = False) -> PullRequest | None:
        return next((p for p in self.rows.values() if p.task_id == task_id), None)

    def save(self, pull_request: PullRequest) -> None:
        self.rows[pull_request.id] = pull_request

    def list_in_states(self, states: Sequence[PullRequestState]) -> list[PullRequest]:
        return [p for p in self.rows.values() if p.state in states]


class _ReviewCycles:
    def __init__(self) -> None:
        self.rows: list[ExternalReviewCycle] = []

    def add(self, cycle: ExternalReviewCycle) -> None:
        self.rows.append(cycle)

    def save(self, cycle: ExternalReviewCycle) -> None:
        self.rows = [cycle if c.id == cycle.id else c for c in self.rows]

    def list_for_pull_request(self, pull_request_id: str) -> list[ExternalReviewCycle]:
        return [c for c in self.rows if c.pull_request_id == pull_request_id]


class _ReviewComments:
    """The PR has no review comments; the round was completed by a clean reaction."""

    def list_for_pull_request(self, pull_request_id: str) -> list[Any]:
        return []


class _NothingOnThePullRequest:
    """No recorded heads pushed out of band and no review rows beyond the cycle."""

    def list_for_pull_request(self, pull_request_id: str) -> list[Any]:
        return []


class _CrucibleHeads:
    """The heads recorded on the PR: the first publication's, pushed by Crucible."""

    def __init__(self) -> None:
        self.rows: list[PullRequestHead] = [
            PullRequestHead(
                id="01HEAD3600000000000000001",
                pull_request_id=PR_ID,
                sha=OLD_HEAD,
                pushed_by=PushedBy.CRUCIBLE,
                observed_at=NOW,
            )
        ]

    def add(self, head: PullRequestHead) -> None:
        self.rows.append(head)

    def list_for_pull_request(self, pull_request_id: str) -> list[PullRequestHead]:
        return [h for h in self.rows if h.pull_request_id == pull_request_id]


class _NoDecisions:
    """Foundry recorded no decision, waiver or disposition on this task."""

    def list_for_task(self, task_id: str) -> list[Any]:
        return []

    def list_for_comments(
        self, comment_ids: Sequence[str], body_sha256: dict[str, str] | None = None
    ) -> list[Any]:
        return []


class _GateResults:
    def __init__(self) -> None:
        self.rows: list[GateResultRecord] = []

    def put(self, result: GateResultRecord) -> None:
        self.rows.append(result)

    def list_for_attempt(self, attempt_id: str) -> list[GateResultRecord]:
        return [r for r in self.rows if r.attempt_id == attempt_id]


class _Harnesses:
    def __init__(self) -> None:
        self.rows: dict[str, HarnessState] = {}

    def get(self, name: str) -> HarnessState | None:
        return self.rows.get(name)

    def put(self, state: HarnessState) -> HarnessState:
        self.rows[state.name] = state
        return state


class _Leases:
    """The attempt's lease, released when it ends; it held no checkout lease."""

    def __init__(self) -> None:
        self.released: list[str] = []

    def release_attempt_lease(self, attempt_id: str) -> None:
        self.released.append(attempt_id)

    def list_checkout_leases(self) -> list[Any]:
        return []


class _Evidence:
    def __init__(self) -> None:
        self.rows: list[EvidenceRecord] = []

    def add(self, evidence: EvidenceRecord) -> EvidenceRecord:
        stored = replace(evidence, id=len(self.rows) + 1)
        self.rows.append(stored)
        return stored

    def list_for_attempt(self, attempt_id: str) -> list[EvidenceRecord]:
        return [e for e in self.rows if e.attempt_id == attempt_id]

    def list_for_task(self, task_id: str) -> list[EvidenceRecord]:
        return [e for e in self.rows if e.task_id == task_id]


class _Artifacts:
    def __init__(self) -> None:
        self.rows: list[Artifact] = []

    def add(self, artifact: Artifact) -> None:
        self.rows.append(artifact)

    def find_by_sha256(self, sha256: str, attempt_id: str | None) -> Artifact | None:
        return next(
            (a for a in self.rows if a.sha256 == sha256 and a.attempt_id == attempt_id), None
        )

    def list_for_attempt(self, attempt_id: str) -> list[Artifact]:
        return [a for a in self.rows if a.attempt_id == attempt_id]


class _Deliveries:
    """No webhook deliveries: the poll alone observes the PR."""

    def list_unprocessed(self) -> list[Any]:
        return []


class _Wakes:
    def __init__(self) -> None:
        self.rows: list[Wake] = []

    def add(self, wake: Wake) -> None:
        self.rows.append(wake)


class _NoClaims:
    """No completion claim was parsed: publication falls back to the default title."""

    def get(self, attempt_id: str) -> None:
        return None


class _Escalations:
    def __init__(self) -> None:
        self.rows: list[Escalation] = []

    def add(self, escalation: Escalation) -> None:
        self.rows.append(escalation)

    def list_for_task(self, task_id: str) -> list[Escalation]:
        return [e for e in self.rows if e.task_id == task_id]


class _NoHistory:
    """Routing's history: no attempt metrics, no promoted image, no exhausted pool."""

    def list_since(
        self, *, since: datetime | None, model: str | None, task_ids: Sequence[str] | None
    ) -> list[Any]:
        return []

    def recent_for_project(
        self, *, project: str, models: Sequence[str], limit_per_model: int
    ) -> list[Any]:
        return []

    def get(self, key: str, *, for_update: bool = False) -> None:
        return None


class _Store:
    """One unit of work over rows kept in memory. Commit and rollback are no-ops: each
    test reads the rows back the way the next transaction would."""

    def __init__(self, repository: Repository, policy: Policy, routing: RoutingPolicyRecord):
        self.tasks = _Tasks()
        self.contracts = _Contracts()
        self.executions = _Executions()
        self.attempts = _Attempts()
        self.events = _Events()
        self.repositories = _Repositories(repository)
        self.provider_settings: dict[str, ProviderSetting] = {}
        self.policies = _Policies(policy)
        self.routing_policies = _RoutingPolicies(routing)
        self.pull_requests = _PullRequests()
        self.review_cycles = _ReviewCycles()
        self.review_comments = _ReviewComments()
        self.github_deliveries = _Deliveries()
        self.pull_request_heads = _NothingOnThePullRequest()
        self.external_reviews = _NothingOnThePullRequest()
        self.decisions = _NoDecisions()
        self.dispositions = _NoDecisions()
        self.gate_results = _GateResults()
        self.harnesses = _Harnesses()
        self.leases = _Leases()
        self.evidence = _Evidence()
        self.artifacts = _Artifacts()
        self.wakes = _Wakes()
        self.escalations = _Escalations()
        self.claims = _NoClaims()
        self.review_reports = _NoDecisions()
        self.attempt_metrics = _NoHistory()
        self.harness_images = _NoHistory()
        self.pool_exhaustions = _NoHistory()

    def __enter__(self) -> _Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def set_fenced_token(self, fenced_token: int) -> None:
        return None

    def uow(self) -> UnitOfWork:
        return cast(UnitOfWork, self)


# ----- GitHub, as the coordinator calls it ------------------------------------


class _GitHub:
    """Answers the calls a poll and a publication's review request make, and records
    every comment Crucible would post."""

    def __init__(self) -> None:
        self.merged = False
        self.posted: list[str] = []
        self.comments: list[CommentRecord] = []
        self.observed: list[int] = []

    def installation_token(
        self, *, installation_id: int, repository: str, permissions: dict[str, str] | None = None
    ) -> InstallationToken:
        return InstallationToken(
            "token",
            expires_at=datetime(2026, 10, 2, 13, tzinfo=UTC),
            repository=repository,
            permissions=permissions,
        )

    def observe(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        base_ref: str,
        with_reactions: bool,
    ) -> Observation:
        self.observed.append(number)
        return Observation(
            pull_request=PullRequestRef(
                number=number,
                url=f"{REPOSITORY_URL}/pull/{number}",
                head_sha=OLD_HEAD,
                base_ref=base_ref,
                state="closed" if self.merged else "open",
                merged=self.merged,
                merged_at=datetime(2026, 10, 2, 12, 30, tzinfo=UTC) if self.merged else None,
                merge_commit_sha=MERGE_SHA if self.merged else None,
                merged_by="maintainer" if self.merged else None,
            )
        )

    def issue_comments(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> tuple[CommentRecord, ...]:
        return tuple(self.comments)

    def authenticated_login(self, token: InstallationToken) -> str:
        return APP_LOGIN

    def post_issue_comment(
        self, token: InstallationToken, *, repository: str, number: int, body: str
    ) -> CommentRecord:
        self.posted.append(body)
        raise AssertionError("the corrected head must not ask for a new external review")


# ----- the task, ready for merge ----------------------------------------------


NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def _policy() -> Policy:
    document = yaml.safe_load(
        (ROOT / "examples" / "policies" / "default-software.yaml").read_text(encoding="utf-8")
    )
    document["version"] = 2
    return Policy(name="default-software", version=2, document=document, created_at=NOW)


def _routing() -> RoutingPolicyRecord:
    document = {
        "schema_version": "1.0",
        "name": "default-routing",
        "version": 3,
        "tiers": {"standard": {"allowed_capability": ["mid"], "prefer": ["mid"]}},
        "models": [
            {
                "id": "gpt-test",
                "harness": "codex",
                "endpoint": "subscription",
                "capability": "mid",
                "cost": "low",
                "speed": "fast",
                "pool": "openai-sub",
                "weight": 1,
                "enabled": True,
            }
        ],
        "pools": {
            "openai-sub": {
                "window": "1h",
                "budget_units": "attempts",
                "soft_limit": 0,
                "default_cooldown_seconds": 60,
            }
        },
        "rotation": {
            "strategy": "weighted-least-recent",
            "quality_feedback": True,
            "quality_window": 20,
        },
    }
    return RoutingPolicyRecord(name="default-routing", version=3, document=document, created_at=NOW)


def _principal() -> Principal:
    return Principal(id=PRINCIPAL_ID, name="foundry", role=Role.ORCHESTRATOR, created_at=NOW)


def _ready_for_merge() -> _Store:
    """A task Hades certified: published, one external round completed, CI green."""
    store = _Store(
        Repository(
            id=REPOSITORY_ID,
            name="example-service",
            url=REPOSITORY_URL,
            default_branch="main",
            policy_name="default-software",
            installation_id=7,
            registered_by="operator",
            created_at=NOW,
            external_review_attested=True,
        ),
        _policy(),
        _routing(),
    )
    store.tasks.add(
        Task(
            id=TASK_ID,
            external_id="EX-0001",
            principal_id=PRINCIPAL_ID,
            project="example-service",
            title="Return 409 on duplicate import ID",
            state=TaskState.READY_FOR_MERGE,
            contract_version=1,
            policy_name="default-software",
            policy_version=2,
            repository_id=REPOSITORY_ID,
            created_at=NOW,
            updated_at=NOW,
            head_sha=OLD_HEAD,
        )
    )
    document = contract_document()
    store.contracts.add(
        TaskContract(
            id="01CONTRACT360000000000001",
            task_id=TASK_ID,
            version=1,
            document=document,
            sha256=contract_sha256(document),
            submitted_at=NOW,
        )
    )
    store.executions.add(
        Execution(
            id="01EXEC360000000000000001",
            task_id=TASK_ID,
            role=ExecutionRole.IMPLEMENT,
            contract_version=1,
            harness="codex",
            model="gpt-test",
            effort="high",
            provider="fake",
            image="crucible-worker:fake-succeed",
            policy_snapshot=store.policies.policy.document,
            state=ExecutionState.SUCCEEDED,
            max_attempts=2,
            retry_on=["environment", "lost"],
            timeout_seconds=3600,
            created_at=NOW,
        )
    )
    store.attempts.add(
        Attempt(
            id="01ATTEMPT3600000000000001",
            execution_id="01EXEC360000000000000001",
            task_id=TASK_ID,
            number=1,
            state=AttemptState.SUCCEEDED,
            created_at=NOW,
        )
    )
    store.pull_requests.add(
        PullRequest(
            id=PR_ID,
            task_id=TASK_ID,
            repository_id=REPOSITORY_ID,
            number=PR_NUMBER,
            url=f"{REPOSITORY_URL}/pull/{PR_NUMBER}",
            base_ref="main",
            work_branch="crucible/EX-0001",
            state=PullRequestState.OPEN,
            head_sha=OLD_HEAD,
            opened_at=NOW,
        )
    )
    store.pull_request_heads = _CrucibleHeads()  # type: ignore[assignment]
    store.review_cycles.add(
        ExternalReviewCycle(
            id="01CYCLE360000000000000001",
            pull_request_id=PR_ID,
            head_sha=OLD_HEAD,
            components=["review"],
            completed_components={"review": "github-review-1"},
            state="completed",
            opened_at=NOW,
            completed_at=NOW,
        )
    )
    for kind, payload in (
        (EventKind.PUBLISH_COMPLETED, {"head_sha": OLD_HEAD, "pull_request": PR_NUMBER}),
        (
            EventKind.EXTERNAL_REVIEW_REQUESTED,
            {
                "pull_request": PR_NUMBER,
                "comment_id": "comment-1",
                "comment_login": APP_LOGIN,
            },
        ),
    ):
        store.events.append(
            Event(
                seq=None,
                ts=NOW,
                kind=kind.value,
                principal="crucible",
                verified=True,
                payload=payload,
                task_id=TASK_ID,
            )
        )
    return store


def _correction(*, of_version: int = 1, **overrides: Any) -> dict[str, Any]:
    document = contract_document(**overrides)
    document["correction"] = {
        "of_version": of_version,
        "reason": "needs_more_work",
        "addresses": [],
        "instructions": "Foundry's full-diff review found a defect; correct it.",
        "resume_from": "remote_branch",
        "request_internal_review": False,
    }
    return document


def _attach(store: _Store, body: dict[str, Any], clock: FakeClock) -> Task:
    return attach_correction(store.uow(), clock, principal=_principal(), task_id=TASK_ID, body=body)


def _supervisor(
    store: _Store, clock: FakeClock, tmp_path: Path, github: _GitHub | None = None
) -> tuple[Supervisor, FakeProvider]:
    provider = FakeProvider()
    supervisor = Supervisor(
        store.uow,
        {"fake": provider},
        clock,
        holder="test",
        artifact_store=DiskArtifactStore(tmp_path / "artifacts"),
        github=cast(GitHubClient, github) if github is not None else None,
    )
    supervisor.fenced_token = 1
    return supervisor, provider


def _task(store: _Store) -> Task:
    task = store.tasks.get(TASK_ID)
    assert task is not None
    return task


def _correction_attempt(store: _Store) -> tuple[Execution, Attempt]:
    corrects = [
        e for e in store.executions.list_for_task(TASK_ID) if e.role is ExecutionRole.CORRECT
    ]
    assert len(corrects) == 1
    attempts = store.attempts.list_for_execution(corrects[0].id)
    assert len(attempts) == 1
    return corrects[0], attempts[0]


def _scheduled_correction(tmp_path: Path) -> tuple[_Store, FakeClock, Supervisor, _GitHub]:
    store = _ready_for_merge()
    clock = FakeClock(NOW)
    github = _GitHub()
    _attach(store, _correction(), clock)
    supervisor, _provider = _supervisor(store, clock, tmp_path, github)
    supervisor._materialize_scheduled()
    return store, clock, supervisor, github


def _merge_and_poll(
    store: _Store, clock: FakeClock, supervisor: Supervisor, github: _GitHub
) -> None:
    github.merged = True
    clock.advance(300)
    polled = asyncio.run(supervisor.delivery.observe())
    assert polled == 1
    assert github.observed == [PR_NUMBER]


# ----- the correction is accepted ---------------------------------------------


def test_a_ready_for_merge_correction_schedules_a_resumed_attempt(tmp_path: Path) -> None:
    store = _ready_for_merge()
    clock = FakeClock(NOW)

    task = _attach(store, _correction(), clock)

    assert task.state is TaskState.SCHEDULED
    assert task.contract_version == 2
    assert task.head_sha is None
    assert EventKind.TASK_CORRECTION_ATTACHED.value in store.events.kinds()
    supervisor, _provider = _supervisor(store, clock, tmp_path)
    supervisor._materialize_scheduled()
    execution, attempt = _correction_attempt(store)
    # A `correct` execution is the one whose workspace starts from the remote work
    # branch, which after publication is the PR's head (08).
    assert execution.role is ExecutionRole.CORRECT
    assert execution.contract_version == 2
    assert attempt.state is AttemptState.PENDING


def test_a_repeat_of_the_same_correction_is_not_a_second_correction(tmp_path: Path) -> None:
    store = _ready_for_merge()
    clock = FakeClock(NOW)
    _attach(store, _correction(), clock)

    with pytest.raises(TransitionNotAllowedError):
        _attach(store, _correction(), clock)

    assert _task(store).contract_version == 2
    assert len(store.contracts.list_for_task(TASK_ID)) == 2
    assert store.events.kinds().count(EventKind.TASK_CORRECTION_ATTACHED.value) == 1


def test_a_stale_of_version_is_refused() -> None:
    store = _ready_for_merge()
    _attach(store, _correction(), FakeClock(NOW))
    task = _task(store)
    task.state = TaskState.READY_FOR_MERGE

    with pytest.raises(ContractValidationError, match="version the task is on") as exc:
        _attach(store, _correction(of_version=1), FakeClock(NOW))

    assert exc.value.errors[0]["path"] == "correction.of_version"
    assert _task(store).contract_version == 2


def test_a_shrinking_verification_list_is_refused() -> None:
    store = _ready_for_merge()
    verification = contract_document()["required_verification"][:-1]

    with pytest.raises(ContractValidationError) as exc:
        _attach(store, _correction(required_verification=verification), FakeClock(NOW))

    assert {
        "path": "required_verification",
        "message": "required_verification may not shrink; missing: ['V4']",
    } in exc.value.errors
    assert _task(store).state is TaskState.READY_FOR_MERGE


def test_a_correction_without_a_policy_check_is_refused() -> None:
    store = _ready_for_merge()
    verification = [
        v for v in contract_document()["required_verification"] if v.get("command") != "make scan"
    ]

    with pytest.raises(ContractValidationError) as exc:
        _attach(store, _correction(required_verification=verification), FakeClock(NOW))

    assert {
        "path": "required_verification",
        "message": "missing the policy-required check 'make scan'",
    } in exc.value.errors
    assert _task(store).state is TaskState.READY_FOR_MERGE


# ----- the counted round stays counted ----------------------------------------


def test_the_corrected_head_is_not_sent_for_a_new_external_round(tmp_path: Path) -> None:
    store, clock, supervisor, github = _scheduled_correction(tmp_path)
    _execution, attempt = _correction_attempt(store)
    task = _task(store)
    # The corrected head passes the pre-PR gates and Foundry accepts it.
    for target, kind in (
        (TaskState.RUNNING, EventKind.TASK_RUNNING),
        (TaskState.REPORTED, EventKind.TASK_REPORTED),
        (TaskState.GATES_PASSED, EventKind.TASK_GATES_PASSED),
        (TaskState.AWAITING_ACCEPTANCE, EventKind.TASK_AWAITING_ACCEPTANCE),
        (TaskState.PUBLISHING, EventKind.TASK_PUBLISHING),
    ):
        move_task(store.uow(), clock, task, target, kind)
    task.head_sha = NEW_HEAD
    plan = PublishPlan(
        task_id=TASK_ID,
        external_id="EX-0001",
        principal_id=PRINCIPAL_ID,
        attempt_id=attempt.id,
        head_sha=NEW_HEAD,
        repository_id=REPOSITORY_ID,
        repository_name="example-org/example-service",
        push_url=f"{REPOSITORY_URL}.git",
        installation_id=7,
        base_ref="main",
        work_branch="crucible/EX-0001",
        deliverable_kind="pull_request",
        draft=False,
        image="publisher",
        bundle_path="bundle",
        bundle_sha256="0" * 64,
        policy=store.policies.policy.document,
        existing_pr_number=PR_NUMBER,
    )
    github.comments = [
        CommentRecord(
            github_id="comment-1",
            login=APP_LOGIN,
            body=TRIGGER,
            created_at=NOW,
            updated_at=NOW,
            kind="issue_comment",
        )
    ]
    token = github.installation_token(installation_id=7, repository=plan.repository_name)
    previous = store.events.latest_for_task_kind(TASK_ID, EventKind.EXTERNAL_REVIEW_REQUESTED.value)

    comment = request_external_review(
        cast(GitHubClient, github),
        token,
        plan=plan,
        pull_request_number=PR_NUMBER,
        previous=previous,
    )
    same_pr = github.observe(
        token,
        repository=plan.repository_name,
        number=PR_NUMBER,
        base_ref="main",
        with_reactions=False,
    ).pull_request
    supervisor.delivery._finish(plan, ref=same_pr)

    assert comment is None
    assert github.posted == []
    # The same PR, its one completed round still counted: the corrected head goes on to
    # CI certification, not back to external review.
    assert _task(store).state is TaskState.AWAITING_CI_CERTIFICATION
    cycles = store.review_cycles.list_for_pull_request(PR_ID)
    assert [(c.head_sha, c.state) for c in cycles] == [(OLD_HEAD, "completed")]
    assert store.events.kinds().count(EventKind.EXTERNAL_REVIEW_REQUESTED.value) == 1


# ----- a merge while the correction runs --------------------------------------


def test_a_merge_while_the_correction_is_scheduled_moves_the_task_to_merged(
    tmp_path: Path,
) -> None:
    store, clock, supervisor, github = _scheduled_correction(tmp_path)

    _merge_and_poll(store, clock, supervisor, github)
    asyncio.run(supervisor._sweep_cancellations())

    assert _task(store).state is TaskState.MERGED
    execution, attempt = _correction_attempt(store)
    assert attempt.state is AttemptState.FAILED
    assert execution.state is ExecutionState.CANCELLED
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None and pull_request.state is PullRequestState.MERGED
    merged = store.events.latest_for_task_kind(TASK_ID, EventKind.TASK_MERGED.value)
    assert merged is not None and merged.payload["merged_from"] == "scheduled"


def test_a_merge_while_the_correction_runs_ends_its_attempt(tmp_path: Path) -> None:
    store, clock, supervisor, github = _scheduled_correction(tmp_path)
    execution, attempt = _correction_attempt(store)
    task = _task(store)
    move_task(store.uow(), clock, task, TaskState.RUNNING, EventKind.TASK_RUNNING)
    # The worker is up: what `_mark_preparing` and `_mark_running` record.
    execution.state = ExecutionState.ACTIVE
    attempt.state = AttemptState.RUNNING
    attempt.handle = "fake-handle"
    attempt.started_at = clock.now()

    _merge_and_poll(store, clock, supervisor, github)
    asyncio.run(supervisor._sweep_cancellations())

    assert _task(store).state is TaskState.MERGED
    draining = store.attempts.get(attempt.id)
    assert draining is not None and draining.state is AttemptState.TERMINATING
    assert draining.termination_reason == "cancel"
    # The worker drains and exits. Its collection ends the attempt and the execution,
    # the task stays merged, and the killed attempt's head does not become the task's.
    supervisor._finish_exited(
        attempt.id, 143, CollectedOutputs(report=None, report_raw=None, blocked_md=None)
    )
    ended = store.attempts.get(attempt.id)
    assert ended is not None and ended.state is AttemptState.FAILED
    closed = store.executions.get(execution.id)
    assert closed is not None and closed.state is ExecutionState.CANCELLED
    assert _task(store).state is TaskState.MERGED
    # hades #379: the merged task's head is the last head Crucible pushed.
    assert _task(store).head_sha == OLD_HEAD


def test_a_correction_that_fails_gates_after_the_merge_does_not_stay_failed(
    tmp_path: Path,
) -> None:
    store, clock, supervisor, github = _scheduled_correction(tmp_path)
    task = _task(store)
    for target, kind in (
        (TaskState.RUNNING, EventKind.TASK_RUNNING),
        (TaskState.REPORTED, EventKind.TASK_REPORTED),
        (TaskState.PRE_PR_GATES_FAILED, EventKind.TASK_PRE_PR_GATES_FAILED),
    ):
        move_task(store.uow(), clock, task, target, kind)

    _merge_and_poll(store, clock, supervisor, github)

    assert _task(store).state is TaskState.MERGED
    merged = store.events.latest_for_task_kind(TASK_ID, EventKind.TASK_MERGED.value)
    assert merged is not None and merged.payload["merged_from"] == "pre_pr_gates_failed"


def test_a_merge_already_recorded_settles_a_failed_correction(tmp_path: Path) -> None:
    store, clock, supervisor, _github = _scheduled_correction(tmp_path)
    task = _task(store)
    for target, kind in (
        (TaskState.RUNNING, EventKind.TASK_RUNNING),
        (TaskState.REPORTED, EventKind.TASK_REPORTED),
        (TaskState.PRE_PR_GATES_FAILED, EventKind.TASK_PRE_PR_GATES_FAILED),
    ):
        move_task(store.uow(), clock, task, target, kind)
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    pull_request.state = PullRequestState.MERGED
    pull_request.merge_sha = MERGE_SHA

    supervisor.delivery._evaluate_gates()

    assert _task(store).state is TaskState.MERGED
    # hades #379: a merge recorded before #379 carries no head; GitHub merged what was on
    # the branch, the last head Crucible pushed, so the merge is not escalated.
    assert store.escalations.list_for_task(TASK_ID) == []
    merged = store.events.latest_for_task_kind(TASK_ID, EventKind.TASK_MERGED.value)
    assert merged is not None
    assert merged.payload["merged_head_recorded"] is False
    assert merged.payload["last_pushed_head"] == OLD_HEAD
