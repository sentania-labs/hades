"""Hades #444: cancelled main runs must not open a fix-main task and the fix-main
contract must not carry a new-test check that already passes on main.

The delivery coordinator and the supervisor run as they do in service, over the
in-memory store the unit tests use. GitHub is stood in for with a fake client,
and the publisher is the integration tier's FakePublisher over the same state."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.release_hold import hold_document, release_held, watch_merge
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import CICertification, PullRequestState, Task
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import GitHubClient
from tests.fixtures import FakeClock
from tests.integration.fake_github import FakeGitHub
from tests.integration.fake_publisher import FakePublisher
from tests.unit.test_issue_360_ready_for_merge_correction import (
    NOW,
    PR_ID,
    PR_NUMBER,
    TASK_ID,
    _NoDecisions,
    _ready_for_merge,
    _Store,
)
from tests.unit.test_issue_411_merge_mechanics import _Client, _open_pull

REPOSITORY = "example-org/example-service"
WORK_BRANCH = "crucible/EX-0001"
MERGED_HEAD = "e" * 40
MAIN_CANCELLED = "1" * 40
MAIN_RED = "2" * 40
MAIN_NEWER = "3" * 40


class _Certifications:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], CICertification] = {}

    def get_for_head(self, pull_request_id: str, head_sha: str) -> CICertification | None:
        return self.rows.get((pull_request_id, head_sha))

    def put(self, certification: CICertification) -> CICertification:
        self.rows[(certification.pull_request_id, certification.head_sha)] = certification
        return certification


class _Rows:
    """Rows the poll records and this test never reads back."""

    def __init__(self) -> None:
        self.rows: list[Any] = []

    def list_for_pull_request(self, pull_request_id: str) -> list[Any]:
        return []

    def add(self, row: Any) -> bool:
        self.rows.append(row)
        return True

    def save(self, row: Any) -> None:
        return None


class _Settings:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    def get(self, name: str) -> Any:
        return self.rows.get(name)

    def put(self, setting: Any) -> None:
        self.rows[setting.name] = setting


class _Acceptance:
    def supersede_for_task(self, task_id: str, now: datetime) -> None:
        return None


class _ReviewReports:
    def list_for_task(self, task_id: str) -> list[Any]:
        return []

    def supersede(self, report_id: str, now: datetime) -> None:
        return None


def _store(state: TaskState = TaskState.READY_FOR_MERGE) -> _Store:
    store = _ready_for_merge()
    store.reactions = _Rows()  # type: ignore[attr-defined]
    store.ci_decisions = _NoDecisions()  # type: ignore[attr-defined]
    store.provider_settings = _Settings()  # type: ignore[assignment]
    store.acceptance = _Acceptance()  # type: ignore[attr-defined]
    store.review_reports = _ReviewReports()  # type: ignore[assignment]
    store.ci_certifications = _Certifications()  # type: ignore[attr-defined]
    _task(store).state = state
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    pull_request.state = (
        PullRequestState.MERGED if state is TaskState.MERGED else pull_request.state
    )
    pull_request.mergeable_state = "clean"
    pull_request.mergeable = True
    return store


def _task(store: _Store, task_id: str = TASK_ID) -> Task:
    task = store.tasks.get(task_id)
    assert task is not None
    return task


def _world(
    tmp_path: Path, *, state: TaskState = TaskState.READY_FOR_MERGE
) -> tuple[_Store, FakeClock, Supervisor, _Client, FakePublisher]:
    store = _store(state)
    clock = FakeClock(NOW)
    fake = FakeGitHub()
    client = _Client(fake)
    _open_pull(client)
    publisher = FakePublisher(fake, REPOSITORY, merge_head=MERGED_HEAD)
    supervisor = Supervisor(
        store.uow,
        {"fake": FakeProvider()},
        clock,
        holder="test",
        artifact_store=DiskArtifactStore(tmp_path / "artifacts"),
        github=cast(GitHubClient, client),
        publisher=publisher,
    )
    supervisor.fenced_token = 1
    return store, clock, supervisor, client, publisher


def _main_world(tmp_path: Path) -> tuple[_Store, FakeClock, Supervisor, _Client, FakePublisher]:
    return _world(tmp_path, state=TaskState.MERGED)


def _poll(store: _Store, clock: FakeClock, supervisor: Supervisor) -> int:
    clock.advance(600)
    return asyncio.run(supervisor.delivery.observe())


def _fix_tasks(store: _Store) -> list[Task]:
    return [t for t in store.tasks.rows.values() if "-fix-main-" in t.external_id]


def _merged_task(store: _Store, clock: FakeClock, *, sha: str, at: datetime, number: int) -> None:
    watch_merge(
        store.uow(),
        clock,
        task_id=TASK_ID,
        repository=REPOSITORY,
        installation_id=7,
        base_ref="main",
        merge_sha=sha,
        pull_request=number,
        merged_at=at,
    )


# ---- AC1: a cancelled-only main run opens no fix-main task ----


def test_cancelled_main_does_not_open_fix_task(tmp_path: Path) -> None:
    """AC1: a main run whose only non-success conclusions are cancelled opens no
    fix-main task and holds no release."""
    store, clock, supervisor, client, _publisher = _main_world(tmp_path)
    _merged_task(store, clock, sha=MAIN_CANCELLED, at=NOW, number=PR_NUMBER)
    # Only cancelled checks -- typical from workflow concurrency cancellation.
    client.fake.set_check(
        REPOSITORY, MAIN_CANCELLED, name="test", status="completed", conclusion="cancelled"
    )

    _poll(store, clock, supervisor)
    _poll(store, clock, supervisor)

    assert not release_held(store.uow())
    assert _fix_tasks(store) == []


def test_cancelled_with_skipped_does_not_open_fix_task(tmp_path: Path) -> None:
    """AC1 variant: cancelled alongside skipped checks is still no verdict."""
    store, clock, supervisor, client, _publisher = _main_world(tmp_path)
    _merged_task(store, clock, sha=MAIN_CANCELLED, at=NOW, number=PR_NUMBER)
    client.fake.set_check(
        REPOSITORY, MAIN_CANCELLED, name="lint", status="completed", conclusion="skipped"
    )
    client.fake.set_check(
        REPOSITORY, MAIN_CANCELLED, name="test", status="completed", conclusion="cancelled"
    )

    _poll(store, clock, supervisor)
    _poll(store, clock, supervisor)

    assert not release_held(store.uow())
    assert _fix_tasks(store) == []


# ---- AC2: only the newest completed main run is judged ----


def test_newest_completed_main_run_is_judged(tmp_path: Path) -> None:
    """AC2: when several merges land, only the newest completed main run is judged.

    An older cancelled run must not open a fix-main task; the newest run with a real
    failure should be the one that triggers it."""
    store, clock, supervisor, client, _publisher = _main_world(tmp_path)
    _merged_task(store, clock, sha=MAIN_CANCELLED, at=NOW, number=400)
    _merged_task(store, clock, sha=MAIN_RED, at=NOW + timedelta(minutes=5), number=402)
    # The older run is cancelled.
    client.fake.set_check(
        REPOSITORY, MAIN_CANCELLED, name="test", status="completed", conclusion="cancelled"
    )
    # The newer run actually fails.
    client.fake.set_check(REPOSITORY, MAIN_RED, name="test", conclusion="failure")

    _poll(store, clock, supervisor)
    _poll(store, clock, supervisor)

    # The cancelled run is superseded by the newer watermark, so it does not open
    # a fix-main. The newer red run does.
    document = hold_document(store.uow())
    assert document.get("merge_sha") == MAIN_RED
    assert document.get("held") is True
    fixes = _fix_tasks(store)
    assert len(fixes) == 1
    assert f"{MAIN_RED[:8]}" in fixes[0].external_id


def test_older_cancelled_does_not_overwrite_a_newer_green(tmp_path: Path) -> None:
    """An older cancelled run arriving late should not undo a newer green commit."""
    store, clock, supervisor, client, _publisher = _main_world(tmp_path)
    # Newer green merge.
    _merged_task(store, clock, sha=MAIN_NEWER, at=NOW + timedelta(minutes=9), number=403)
    client.fake.set_check(REPOSITORY, MAIN_NEWER, name="test", conclusion="success")
    client.repo.branches["main"] = MAIN_NEWER
    # Older cancelled merge (arrives late).
    _merged_task(store, clock, sha=MAIN_CANCELLED, at=NOW, number=400)
    client.fake.set_check(
        REPOSITORY, MAIN_CANCELLED, name="test", status="completed", conclusion="cancelled"
    )

    _poll(store, clock, supervisor)
    _poll(store, clock, supervisor)

    # The cancelled run is superseded; the green runs and clears the hold.
    assert not release_held(store.uow())


# ---- AC3: fix-main contract uses test_fix_main_<sha7>.py ----


def test_fix_main_contract_replaces_new_test_check(tmp_path: Path) -> None:
    """AC3: a fix-main contract's new-test check names
    tests/unit/test_fix_main_<sha7>.py, never the source task's test file."""
    store, clock, supervisor, client, _publisher = _main_world(tmp_path)
    _merged_task(store, clock, sha=MAIN_RED, at=NOW, number=PR_NUMBER)
    client.fake.set_check(REPOSITORY, MAIN_RED, name="test", conclusion="failure")

    _poll(store, clock, supervisor)
    _poll(store, clock, supervisor)

    fixes = _fix_tasks(store)
    assert len(fixes) == 1
    fix_task = fixes[0]
    contract = store.contracts.get(fix_task.id, 1)
    assert contract is not None
    required = contract.document.get("required_verification", [])
    # Find the fix-main test check
    fix_checks = [
        c for c in required if isinstance(c, dict) and "test_fix_main_" in str(c.get("command", ""))
    ]
    assert len(fix_checks) >= 1
    for check in fix_checks:
        assert isinstance(check, dict)
        cmd = check["command"]
        assert f"test_fix_main_{MAIN_RED[:7]}" in cmd
        assert "gate_proves_nothing" not in cmd.lower()


def test_fix_main_contract_keeps_job_names_and_errors(tmp_path: Path) -> None:
    """The objective keeps failing job names and error lines, and names the sha7 test."""
    store, clock, supervisor, client, _publisher = _main_world(tmp_path)
    _merged_task(store, clock, sha=MAIN_RED, at=NOW, number=PR_NUMBER)
    client.fake.set_check(REPOSITORY, MAIN_RED, name="integration-test", conclusion="failure")

    _poll(store, clock, supervisor)
    _poll(store, clock, supervisor)

    fixes = _fix_tasks(store)
    assert len(fixes) == 1
    fix_task = fixes[0]
    contract = store.contracts.get(fix_task.id, 1)
    assert contract is not None
    objective = contract.document["objective"]
    assert "integration-test: failure" in objective
    assert f"pull request #{PR_NUMBER}" in objective
    required = contract.document.get("required_verification", [])
    fix_checks = [
        c for c in required if isinstance(c, dict) and "test_fix_main_" in str(c.get("command", ""))
    ]
    assert len(fix_checks) >= 1
    assert f"test_fix_main_{MAIN_RED[:7]}.py" in fix_checks[0]["command"]
