"""Hades #337: the supervisor squash-merges exactly the certified head."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, Mock

import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.testclient import TestClient

from crucible.adapters.api.deps import app_context, current_principal, require_admin, unit_of_work
from crucible.adapters.api.problems import install_problem_handlers
from crucible.adapters.api.routers import admin
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.transport import RestTransport
from crucible.adapters.ui.pages import settings
from crucible.application.admin import delivery
from crucible.application.admin.context import AdminContext
from crucible.application.delivery_tick import DeliveryCoordinator, MergePlan
from crucible.application.errors import ContractValidationError
from crucible.domain.entities import Principal, PullRequestState, PushedBy, Role
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import (
    GitHubClient,
    GitHubError,
    InstallationToken,
    MergeResult,
    PullRequestRef,
)
from tests.fixtures import FakeClock

HEAD = "a" * 40
MOVED = "b" * 40
MERGE = "c" * 40
NOW = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)


class Host:
    def __init__(self, uow: MagicMock) -> None:
        self.uow = uow

    @contextmanager
    def _fenced(self) -> Iterator[MagicMock]:
        yield self.uow

    async def _db(self, fn: Any) -> Any:
        return fn()


class FakeGitHub:
    def __init__(self, *, head: str = HEAD, refusal: GitHubError | None = None) -> None:
        self.head = head
        self.base = "main"
        self.state = "open"
        self.mergeable = "clean"
        self.merged = False
        self.before_read: Any = None
        self.refusal = refusal
        self.merge_calls: list[str] = []

    def installation_token(self, **kwargs: Any) -> InstallationToken:
        return InstallationToken(
            "test-token", expires_at=NOW + timedelta(hours=1), repository="org/repo"
        )

    def get_pull_request(self, token: InstallationToken, **kwargs: Any) -> PullRequestRef:
        if self.before_read:
            self.before_read()
        return PullRequestRef(
            number=7,
            url="https://github.invalid/org/repo/pull/7",
            head_sha=self.head,
            base_ref=self.base,
            state=self.state,
            mergeable_state=self.mergeable,
            merged=self.merged,
            merged_at=NOW if self.merged else None,
            merged_by="person" if self.merged else None,
            merge_commit_sha=MERGE if self.merged else None,
        )

    def merge_pull_request(
        self, token: InstallationToken, *, expected_head_sha: str, **kwargs: Any
    ) -> MergeResult:
        self.merge_calls.append(expected_head_sha)
        if self.refusal is not None:
            raise self.refusal
        return MergeResult(sha=MERGE, merged_at=NOW, merged_by="hades[bot]")


def setup() -> tuple[DeliveryCoordinator, FakeGitHub, MagicMock, Any, MergePlan]:
    task: Any = SimpleNamespace(
        id="task-1",
        external_id="FDY-0267",
        repository_id="repo-1",
        principal_id="principal-1",
        policy_name="default-software",
        policy_version=1,
        state=TaskState.READY_FOR_MERGE,
        head_sha=HEAD,
        updated_at=NOW,
        closed_at=None,
    )
    pull_request = SimpleNamespace(
        id="pr-1",
        task_id=task.id,
        url="https://github.invalid/org/repo/pull/7",
        state=PullRequestState.OPEN,
        number=7,
        head_sha=HEAD,
        merge_sha=None,
        merged_at=None,
        merged_by=None,
        base_ref="main",
        observed_head_sha=HEAD,
        observed_base_ref="main",
        mergeable_state="clean",
        mergeable=True,
        merge_refusal_cause=None,
        merge_refusal_head_sha=None,
        merge_refusal_base_ref=None,
        merge_refusal_mergeable_state=None,
        merge_refusal_count=0,
        merge_retry_at=None,
    )
    uow = MagicMock()
    uow.provider_settings.get.return_value = None
    uow.policies.get.return_value = SimpleNamespace(document={})
    uow.ci_certifications.get_for_head.return_value = SimpleNamespace(
        state="green",
        check_runs=[{"status": "completed", "conclusion": "success", "source": "check_run"}],
    )
    uow.pull_requests.get_for_task.return_value = pull_request
    uow.tasks.list_by_state.return_value = [task]
    uow.repositories.get.return_value = SimpleNamespace(
        url="https://github.com/org/repo", installation_id=42
    )
    uow.tasks.get.return_value = task
    uow.pull_requests.get.return_value = pull_request
    github = FakeGitHub()
    coordinator = DeliveryCoordinator(Host(uow), FakeClock(NOW), github=cast(GitHubClient, github))
    plan = MergePlan(
        task_id=task.id,
        pull_request_id=pull_request.id,
        number=7,
        repository_name="org/repo",
        installation_id=42,
        certified_head_sha=HEAD,
        base_ref="main",
    )
    return coordinator, github, uow, task, plan


@pytest.mark.asyncio
async def test_certified_head_merges_and_records_github_response() -> None:
    coordinator, github, uow, task, plan = setup()

    assert await coordinator._merge_one(plan) is True

    assert github.merge_calls == [HEAD]
    pull_request = uow.pull_requests.get.return_value
    assert pull_request.state is PullRequestState.MERGED
    assert pull_request.merge_sha == MERGE
    assert pull_request.merged_at == NOW
    assert pull_request.merged_by == "hades[bot]"
    assert task.state is TaskState.MERGED


@pytest.mark.asyncio
async def test_moved_head_is_not_merged() -> None:
    coordinator, github, _uow, task, plan = setup()
    github.head = MOVED

    assert await coordinator._merge_one(plan) is False
    assert github.merge_calls == []
    assert task.state is TaskState.READY_FOR_MERGE


@pytest.mark.asyncio
async def test_merge_refusal_wakes_with_the_cause() -> None:
    coordinator, github, uow, task, plan = setup()
    github.refusal = GitHubError(409, "base branch has conflicts")

    assert await coordinator._merge_one(plan) is False

    wake = uow.wakes.add.call_args.args[0]
    assert wake.reason == "ready_for_merge"
    assert "base branch has conflicts" in wake.payload["summary"]
    assert task.state is TaskState.READY_FOR_MERGE


def test_auto_merge_false_leaves_ready_task_alone() -> None:
    coordinator, github, uow, task, _plan = setup()
    uow.tasks.list_by_state.return_value = [task]
    uow.policies.get.return_value = SimpleNamespace(document={"delivery": {"auto_merge": False}})

    assert coordinator._ready_merges() == []
    assert github.merge_calls == []
    assert task.state is TaskState.READY_FOR_MERGE


def test_default_selects_certified_head_without_a_hold_window() -> None:
    coordinator, _github, _uow, _task, plan = setup()
    assert coordinator._ready_merges() == [plan]


@pytest.mark.asyncio
async def test_global_switch_is_live_for_existing_coordinator() -> None:
    coordinator, github, uow, task, plan = setup()
    uow.provider_settings.get.return_value = SimpleNamespace(document={"enabled": False})
    assert coordinator._ready_merges() == []
    assert await coordinator._merge_one(plan) is False
    assert github.merge_calls == []
    assert task.state is TaskState.READY_FOR_MERGE
    uow.provider_settings.get.return_value.document["enabled"] = True
    assert coordinator._ready_merges() == [plan]
    assert await coordinator._merge_one(plan) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["switch", "head", "state", "policy", "ci"])
async def test_rechecks_decision_after_github_read(change: str) -> None:
    coordinator, github, uow, task, plan = setup()

    def change_during_read() -> None:
        if change == "switch":
            uow.provider_settings.get.return_value = SimpleNamespace(document={"enabled": False})
        elif change == "head":
            task.head_sha = MOVED
        elif change == "state":
            task.state = TaskState.SCHEDULED
        elif change == "policy":
            uow.policies.get.return_value.document = {"delivery": {"auto_merge": False}}
        else:
            uow.ci_certifications.get_for_head.return_value.state = "pending"

    github.before_read = change_during_read
    assert await coordinator._merge_one(plan) is False
    assert github.merge_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("base", "release"), ("state", "closed")])
async def test_live_base_and_open_state_are_required(field: str, value: str) -> None:
    coordinator, github, uow, _task, plan = setup()
    setattr(github, field, value)
    assert await coordinator._merge_one(plan) is False
    assert github.merge_calls == []
    assert value in uow.wakes.add.call_args.args[0].payload["summary"]
    # A refused observation must never adopt the retargeted base for the next tick.
    assert coordinator._ready_merges() == []
    coordinator._clock.advance(seconds=61)  # type: ignore[attr-defined]
    assert coordinator._ready_merges()[0].base_ref == "main"
    assert await coordinator._merge_one(plan) is False
    assert github.merge_calls == []
    assert uow.wakes.add.call_count == 1


@pytest.mark.asyncio
async def test_persistent_refusal_backs_off_and_wakes_only_for_a_new_cause() -> None:
    coordinator, github, uow, _task, plan = setup()
    github.refusal = GitHubError(409, "conflict with main")
    assert await coordinator._merge_one(plan) is False
    assert coordinator._ready_merges() == []
    coordinator._clock.advance(seconds=60)  # type: ignore[attr-defined]
    assert coordinator._ready_merges() == [plan]
    assert await coordinator._merge_one(plan) is False
    assert uow.wakes.add.call_count == 1
    assert uow.pull_requests.get.return_value.merge_retry_at == NOW + timedelta(seconds=180)
    github.refusal = GitHubError(403, "branch protection requires approval")
    assert await coordinator._merge_one(plan) is False
    assert uow.wakes.add.call_count == 2
    assert "branch protection" in uow.wakes.add.call_args.args[0].payload["summary"]


@pytest.mark.asyncio
async def test_person_merge_is_recorded_without_another_merge_call() -> None:
    coordinator, github, uow, task, plan = setup()
    github.merged = True
    github.state = "closed"
    assert await coordinator._merge_one(plan) is True
    assert github.merge_calls == []
    assert task.state is TaskState.MERGED
    assert uow.pull_requests.get.return_value.merged_by == "person"


@pytest.mark.parametrize("state", ["pending", "failed", "skipped"])
def test_not_green_is_not_selected(state: str) -> None:
    coordinator, _github, uow, _task, _plan = setup()
    uow.ci_certifications.get_for_head.return_value.state = state
    assert coordinator._ready_merges() == []


@pytest.mark.parametrize("conclusion", ["failure", "neutral", None])
def test_green_narrowed_policy_does_not_hide_an_unpassed_job(conclusion: str | None) -> None:
    coordinator, _github, uow, _task, _plan = setup()
    uow.ci_certifications.get_for_head.return_value.check_runs.append(
        {"status": "completed" if conclusion else "in_progress", "conclusion": conclusion}
    )
    assert coordinator._ready_merges() == []


@pytest.mark.asyncio
async def test_observe_merges_on_the_tick_gates_become_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, github, _uow, task, _plan = setup()
    task.state = TaskState.AWAITING_CI_CERTIFICATION
    monkeypatch.setattr(coordinator, "_due_polls", lambda: [])
    monkeypatch.setattr(
        coordinator, "_evaluate_gates", lambda: setattr(task, "state", TaskState.READY_FOR_MERGE)
    )
    await coordinator.observe()
    assert github.merge_calls == [HEAD]
    assert task.state is TaskState.MERGED


def admin_context() -> tuple[Any, Any, Any]:
    _coordinator, _github, uow, _task, _plan = setup()
    principal = Principal("admin-1", "admin", Role.ADMIN, NOW)
    ctx = SimpleNamespace(
        admin=AdminContext(uow_factory=Mock(), clock=FakeClock(NOW), providers={}, harnesses=Mock())
    )
    uow.provider_settings.put.side_effect = lambda row: setattr(
        uow.provider_settings.get, "return_value", row
    )
    return ctx, uow, principal


def test_admin_api_switch_is_persisted_and_audited() -> None:
    ctx, uow, principal = admin_context()
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(admin.router, prefix="/v1")
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: uow
    app.dependency_overrides[require_admin] = lambda: principal
    with TestClient(app) as client:
        assert client.get("/v1/admin/delivery/auto-merge").json() == {"enabled": True}
        response = client.post("/v1/admin/delivery/auto-merge", json={"enabled": False})
        assert response.status_code == 200
        assert response.json() == {"enabled": False}
        assert client.get("/v1/admin/delivery/auto-merge").json() == {"enabled": False}
    assert uow.provider_settings.put.call_args.args[0].updated_by == "admin"
    assert uow.events.append.call_args.args[0].kind == "auto_merge_updated"


@pytest.mark.parametrize("enabled", [None, "false", 0, 1])
def test_admin_switch_rejects_non_booleans(enabled: Any) -> None:
    ctx, uow, principal = admin_context()
    with pytest.raises(ContractValidationError, match="JSON boolean"):
        delivery.save_auto_merge(ctx.admin, uow, principal=principal, enabled=enabled, reason=None)
    uow.provider_settings.put.assert_not_called()


@pytest.mark.asyncio
async def test_admin_page_shows_and_changes_the_same_live_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    ctx, uow, principal = admin_context()
    ctx.settings = None
    app = FastAPI()
    app.state.ctx = ctx
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/ui/settings",
            "headers": [],
            "app": app,
            "query_string": b"",
        }
    )
    monkeypatch.setattr(settings, "_require", lambda *_args: (principal, "csrf-value"))
    response = settings.settings_page(request, ctx, uow)
    assert b"Disable auto-merge" in bytes(response.body)
    assert b"/ui/actions/auto-merge" in bytes(response.body)
    await settings._action_auto_merge(
        request, "auto-merge", ctx, uow, principal, "csrf-value", {"enabled": "false"}, None
    )
    assert delivery.auto_merge_view(uow) == {"enabled": False}
    response = settings.settings_page(request, ctx, uow)
    assert b"Enable auto-merge" in bytes(response.body)
    principal.role = Role.OBSERVER
    response = settings.settings_page(request, ctx, uow)
    assert b"/ui/actions/auto-merge" not in bytes(response.body)


def test_non_admin_cannot_change_the_global_switch() -> None:

    ctx, uow, principal = admin_context()
    principal.role = Role.OBSERVER
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(admin.router, prefix="/v1")
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: uow
    app.dependency_overrides[current_principal] = lambda: principal
    with TestClient(app) as client:
        response = client.post("/v1/admin/delivery/auto-merge", json={"enabled": False})
        assert response.status_code == 403
    uow.provider_settings.put.assert_not_called()


def test_merge_response_settles_a_correction_started_during_the_request() -> None:
    coordinator, _github, uow, task, plan = setup()
    task.state = TaskState.RUNNING
    task.head_sha = MOVED
    uow.pull_request_heads.list_for_pull_request.return_value = [
        SimpleNamespace(pushed_by=PushedBy.CRUCIBLE, sha=HEAD)
    ]
    uow.events.list_for_task.return_value = []
    coordinator._record_merge(plan, MergeResult(sha=MERGE, merged_at=NOW, merged_by="hades[bot]"))
    assert task.state is TaskState.MERGED
    assert task.head_sha == HEAD
    assert uow.pull_requests.get.return_value.merge_sha == MERGE
    events = [call.args[0] for call in uow.events.append.call_args_list]
    changed = next(event for event in events if event.kind == "pull_request_state_changed")
    assert changed.payload["head_sha"] == HEAD


def test_github_adapter_sends_squash_and_expected_sha_with_the_app_token() -> None:
    transport = Mock(spec=RestTransport)
    transport.request.return_value = (200, {"merged": True, "sha": MERGE}, {})
    transport.get.return_value = {
        "number": 7,
        "html_url": "https://github.invalid/org/repo/pull/7",
        "head": {"sha": HEAD},
        "base": {"ref": "main"},
        "state": "closed",
        "merged": True,
        "merged_at": NOW.isoformat(),
        "merged_by": {"login": "hades[bot]"},
        "merge_commit_sha": MERGE,
    }
    token = FakeGitHub().installation_token()
    client = RestGitHubClient(Mock(), transport)
    result = client.merge_pull_request(
        token, repository="org/repo", number=7, expected_head_sha=HEAD
    )
    assert result == MergeResult(sha=MERGE, merged_at=NOW, merged_by="hades[bot]")
    transport.request.assert_called_once_with(
        "PUT",
        "/repos/org/repo/pulls/7/merge",
        bearer=token.reveal(),
        body={"merge_method": "squash", "sha": HEAD},
    )


@pytest.mark.parametrize("status", [200, 403, 405, 409, 412])
def test_github_adapter_preserves_refusal_cause(status: int) -> None:
    transport = Mock(spec=RestTransport)
    transport.request.return_value = (
        status,
        {"merged": False, "message": "precondition failed"},
        {},
    )
    client = RestGitHubClient(Mock(), transport)
    with pytest.raises(GitHubError, match="precondition failed"):
        client.merge_pull_request(
            FakeGitHub().installation_token(),
            repository="org/repo",
            number=7,
            expected_head_sha=HEAD,
        )
    transport.get.assert_not_called()
