"""Hades #411: a conflicting pull request, a red main, and an adopted head.

The delivery coordinator and the supervisor run as they do in service, over the
in-memory store the #360 tests use. GitHub is the integration tier's `FakeGitHub` state
(branches, pull requests, check runs), read through a small client here, and the
publisher is the integration tier's `FakePublisher` over the same state, so a merge of
main moves the very branch the next poll reads. The merge-main script itself runs
against real git repositories at the end.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from crucible.adapters.execution import scripts
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.execution.publisher import merge_outcome_from_files
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.delivery_decisions import record_head_decision
from crucible.application.release_hold import (
    SETTING_NAME,
    hold_document,
    release_held,
    watch_merge,
)
from crucible.application.supervisor import Supervisor
from crucible.contracts.api import HeadDecisionRequest
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    CICertification,
    ExecutionRole,
    HeadAction,
    ProviderSetting,
    PullRequestState,
    Task,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import (
    CheckRecord,
    GitHubClient,
    InstallationToken,
    MergeResult,
    Observation,
    PullRequestRef,
)
from tests.fixtures import REPOSITORY_URL, FakeClock
from tests.integration.fake_github import FakeGitHub, installation_token_value
from tests.integration.fake_github import PullRequestState as FakePull
from tests.integration.fake_publisher import FakePublisher
from tests.unit.test_issue_360_ready_for_merge_correction import (
    NOW,
    OLD_HEAD,
    PR_ID,
    PR_NUMBER,
    TASK_ID,
    _NoDecisions,
    _principal,
    _ready_for_merge,
    _Store,
)

REPOSITORY = "example-org/example-service"
WORK_BRANCH = "crucible/EX-0001"
MERGED_HEAD = "e" * 40
OTHER_HEAD = "d" * 40
MAIN_RED = "1" * 40
MAIN_OLD_GREEN = "2" * 40
MAIN_NEWER = "3" * 40


# ----- the rows the #360 store does not carry ----------------------------------


class _Settings:
    def __init__(self) -> None:
        self.rows: dict[str, ProviderSetting] = {}

    def get(self, name: str) -> ProviderSetting | None:
        return self.rows.get(name)

    def put(self, setting: ProviderSetting) -> None:
        self.rows[setting.name] = setting


class _Acceptance:
    def supersede_for_task(self, task_id: str, now: datetime) -> None:
        return None


class _ReviewReports:
    def list_for_task(self, task_id: str) -> list[Any]:
        return []

    def supersede(self, report_id: str, now: datetime) -> None:
        return None


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
    pull_request.mergeable_state = "clean"
    pull_request.mergeable = True
    return store


def _task(store: _Store, task_id: str = TASK_ID) -> Task:
    task = store.tasks.get(task_id)
    assert task is not None
    return task


# ----- GitHub: the fake's state, read as the coordinator reads GitHub -----------


class _Client:
    """The `GitHubClient` calls a poll, a merge and the main CI watch make, answered
    from the integration fake's state. Tokens are minted into that state so the fake
    publisher accepts them."""

    def __init__(self, fake: FakeGitHub) -> None:
        self.fake = fake
        self.repo = fake.add_repository(REPOSITORY, branches={"main": "0" * 40})
        self.repo.branches[WORK_BRANCH] = OLD_HEAD
        self.merged_numbers: list[int] = []

    def pull(self) -> Any:
        return self.repo.pulls[PR_NUMBER]

    def installation_token(
        self, *, installation_id: int, repository: str, permissions: dict[str, str] | None = None
    ) -> InstallationToken:
        value = installation_token_value()
        self.fake.tokens.add(value)
        return InstallationToken(value, expires_at=NOW + timedelta(hours=1), repository=repository)

    def _ref(self) -> PullRequestRef:
        pull = self.pull()
        return PullRequestRef(
            number=pull.number,
            url=f"{REPOSITORY_URL}/pull/{pull.number}",
            head_sha=pull.head_sha,
            base_ref=pull.base_ref,
            state=pull.state,
            merged=pull.merged,
            merged_at=NOW if pull.merged else None,
            merge_commit_sha=pull.merge_commit_sha,
            merged_by=pull.merged_by,
            mergeable_state=pull.mergeable_state,
            mergeable=pull.mergeable,
        )

    def _checks(self, sha: str) -> tuple[CheckRecord, ...]:
        return tuple(
            CheckRecord(
                name=run["name"],
                status=run["status"],
                conclusion=run["conclusion"],
                head_sha=sha,
                external_id=str(run["id"]),
            )
            for run in self.repo.check_runs.get(sha, [])
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
        ref = self._ref()
        return Observation(
            pull_request=ref,
            checks=self._checks(ref.head_sha),
            required_checks=("test",),
        )

    def get_pull_request(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> PullRequestRef:
        return self._ref()

    def merge_pull_request(
        self, token: InstallationToken, *, expected_head_sha: str, **kwargs: Any
    ) -> MergeResult:
        self.fake.merge(REPOSITORY, PR_NUMBER, by="crucible-app[bot]", sha=MAIN_NEWER)
        self.merged_numbers.append(PR_NUMBER)
        return MergeResult(sha=MAIN_NEWER, merged_at=NOW, merged_by="crucible-app[bot]")

    def checks_for_commit(
        self, token: InstallationToken, *, repository: str, head_sha: str
    ) -> tuple[CheckRecord, ...]:
        return self._checks(head_sha)

    def ci_failure_log(
        self,
        token: InstallationToken,
        *,
        repository: str,
        source: str,
        external_id: str,
        limit_bytes: int,
    ) -> bytes:
        return self.fake.workflow_log

    def remote_head(self, token: InstallationToken, *, repository: str, ref: str) -> str | None:
        return self.repo.branches.get(ref)


def _open_pull(client: _Client, *, mergeable_state: str = "dirty") -> None:
    client.repo.pulls[PR_NUMBER] = FakePull(
        number=PR_NUMBER,
        head_branch=WORK_BRANCH,
        base_ref="main",
        head_sha=OLD_HEAD,
        mergeable_state=mergeable_state,
        mergeable=mergeable_state != "dirty",
    )


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


def _poll(store: _Store, clock: FakeClock, supervisor: Supervisor) -> int:
    clock.advance(600)
    return asyncio.run(supervisor.delivery.observe())


def _wakes(store: _Store, reason: WakeReason) -> list[Any]:
    return [wake for wake in store.wakes.rows if wake.reason == reason.value]


def _events(store: _Store, kind: EventKind, task_id: str = TASK_ID) -> list[Any]:
    return [e for e in store.events.rows if e.kind == kind.value and e.task_id == task_id]


# ----- a conflicting pull request -----------------------------------------------


def test_a_dirty_pull_request_is_recorded_woken_once_and_merged_by_crucible(
    tmp_path: Path,
) -> None:
    store, clock, supervisor, client, publisher = _world(tmp_path)

    assert _poll(store, clock, supervisor) == 1

    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    # Crucible merged main into the known tip, with the lease on that tip.
    assert [(m.work_branch, m.base_ref, m.expected_head) for m in publisher.merges] == [
        (WORK_BRANCH, "main", OLD_HEAD)
    ]
    assert client.repo.branches[WORK_BRANCH] == MERGED_HEAD
    pushed = _events(store, EventKind.BRANCH_PUSHED)
    assert [(e.payload["head_sha"], e.payload["reason"]) for e in pushed] == [
        (MERGED_HEAD, "merge_main")
    ]
    assert pushed[0].payload["force_with_lease"] == OLD_HEAD
    # The new head is Crucible's and waits for its own checks, not for a re-test.
    task = _task(store)
    assert task.state is TaskState.AWAITING_CI_CERTIFICATION
    assert task.head_sha == pull_request.head_sha == MERGED_HEAD
    conflicting = _wakes(store, WakeReason.PULL_REQUEST_CONFLICTING)
    assert len(conflicting) == 1
    assert "Next: Crucible merges main" in conflicting[0].payload["summary"]
    assert task.contract_version == 1

    # The next poll sees the merged head as Crucible's own push: no divergence, no
    # second wake, no second merge.
    client.pull().mergeable_state, client.pull().mergeable = "clean", True
    _poll(store, clock, supervisor)
    assert _task(store).state is TaskState.AWAITING_CI_CERTIFICATION
    assert len(_wakes(store, WakeReason.PULL_REQUEST_CONFLICTING)) == 1
    assert _wakes(store, WakeReason.HEAD_DIVERGED) == []
    assert len(publisher.merges) == 1


def test_a_conflict_in_the_merge_launches_a_correction_from_the_remote_tip(
    tmp_path: Path,
) -> None:
    store, clock, supervisor, client, publisher = _world(
        tmp_path, state=TaskState.AWAITING_CI_CERTIFICATION
    )
    publisher.merge_conflicts = ["crucible/application/delivery_tick.py", "docs/spec/23.md"]

    _poll(store, clock, supervisor)

    # git reported conflicts: the branch is untouched and a worker gets the job.
    assert client.repo.branches[WORK_BRANCH] == OLD_HEAD
    assert publisher.pushes == []
    task = _task(store)
    assert task.state is TaskState.SCHEDULED
    assert task.contract_version == 2
    contract = store.contracts.get(TASK_ID, 2)
    assert contract is not None
    instructions = contract.document["correction"]["instructions"]
    assert instructions.startswith(
        "Merge origin/main, resolve every conflict keeping both behaviours, run every "
        "required check, commit the result, and report."
    )
    assert "crucible/application/delivery_tick.py, docs/spec/23.md" in instructions
    attached = _events(store, EventKind.TASK_CORRECTION_ATTACHED)[-1]
    assert attached.payload["conflicting_files"] == [
        "crucible/application/delivery_tick.py",
        "docs/spec/23.md",
    ]
    assert len(_wakes(store, WakeReason.PULL_REQUEST_CONFLICTING)) == 1

    # The supervisor starts the correction at the remote branch tip, not from the
    # previous attempt's bundle.
    supervisor._materialize_scheduled()
    corrects = [
        e for e in store.executions.list_for_task(TASK_ID) if e.role is ExecutionRole.CORRECT
    ]
    assert len(corrects) == 1
    assert corrects[0].resume_from_remote is True
    assert corrects[0].contract_version == 2

    # A further poll of the same head schedules nothing more.
    _poll(store, clock, supervisor)
    assert len(_events(store, EventKind.TASK_CORRECTION_ATTACHED)) == 1


def test_a_diverged_dirty_head_stops_at_head_diverged(tmp_path: Path) -> None:
    """Codex P1 on PR 417: an out-of-band push that also conflicts is not merged into,
    nor corrected against. It waits in `head_diverged` for the head decision."""
    store, clock, supervisor, client, publisher = _world(tmp_path)
    client.fake.push(REPOSITORY, WORK_BRANCH, OTHER_HEAD, by="operator")

    _poll(store, clock, supervisor)

    task = _task(store)
    assert task.state is TaskState.HEAD_DIVERGED
    assert task.contract_version == 1
    assert publisher.merges == []
    assert _wakes(store, WakeReason.PULL_REQUEST_CONFLICTING) == []
    assert len(_wakes(store, WakeReason.HEAD_DIVERGED)) == 1
    assert _events(store, EventKind.TASK_CORRECTION_ATTACHED) == []

    # Polled again while it waits: still nothing acts on the untrusted head.
    _poll(store, clock, supervisor)
    assert _task(store).state is TaskState.HEAD_DIVERGED
    assert publisher.merges == []


def test_adopt_recollects_the_out_of_band_head_from_the_remote_tip(tmp_path: Path) -> None:
    store, clock, supervisor, client, _publisher = _world(tmp_path)
    client.fake.push(REPOSITORY, WORK_BRANCH, OTHER_HEAD, by="operator")
    client.pull().mergeable_state, client.pull().mergeable = "clean", True
    _poll(store, clock, supervisor)
    assert _task(store).state is TaskState.HEAD_DIVERGED

    task = record_head_decision(
        store.uow(),
        clock,
        principal=_principal(),
        task_id=TASK_ID,
        request=HeadDecisionRequest(action=HeadAction.ADOPT, reasoning="the operator's fix"),
    )

    assert task.state is TaskState.SCHEDULED
    scheduled = _events(store, EventKind.TASK_SCHEDULED)[-1]
    assert scheduled.payload["reason"] == "adopt"
    assert scheduled.payload["resume_from_work_branch"] is True
    supervisor._materialize_scheduled()
    corrects = [
        e for e in store.executions.list_for_task(TASK_ID) if e.role is ExecutionRole.CORRECT
    ]
    assert len(corrects) == 1
    assert corrects[0].resume_from_remote is True


def test_ready_for_merge_merges_a_mergeable_head_behind_main_without_a_retest(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, client, _publisher = _world(tmp_path)
    client.pull().mergeable_state, client.pull().mergeable = "behind", True
    client.fake.set_check(REPOSITORY, OLD_HEAD, name="test")
    store.ci_certifications.put(  # type: ignore[attr-defined]
        CICertification(
            id="01CERT4110000000000000001",
            pull_request_id=PR_ID,
            task_id=TASK_ID,
            head_sha=OLD_HEAD,
            state="green",
            required_checks=["test"],
            check_runs=[{"status": "completed", "conclusion": "success", "source": "check_run"}],
            failure={},
            detail="",
            evaluated_at=NOW,
        )
    )
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    pull_request.mergeable_state = "behind"

    asyncio.run(supervisor.delivery.observe())

    assert client.merged_numbers == [PR_NUMBER]
    assert _task(store).state is TaskState.MERGED
    # The merge commit is now watched on main.
    document = hold_document(store.uow())
    assert [e["merge_sha"] for e in document["watching"]] == [MAIN_NEWER]
    assert document["merged_pull_requests"] == [PR_NUMBER]


# ----- red main -----------------------------------------------------------------


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


def _main_world(tmp_path: Path) -> tuple[_Store, FakeClock, Supervisor, _Client]:
    store, clock, supervisor, client, _publisher = _world(tmp_path, state=TaskState.MERGED)
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    pull_request.state = PullRequestState.MERGED
    client.pull().state, client.pull().merged = "closed", True
    return store, clock, supervisor, client


def _fix_tasks(store: _Store) -> list[Task]:
    return [t for t in store.tasks.rows.values() if "-fix-main-" in t.external_id]


def test_red_main_opens_one_fix_task_and_holds_the_release(tmp_path: Path) -> None:
    store, clock, supervisor, client = _main_world(tmp_path)
    _merged_task(store, clock, sha=MAIN_RED, at=NOW, number=PR_NUMBER)
    client.fake.set_check(REPOSITORY, MAIN_RED, name="test", conclusion="failure")

    _poll(store, clock, supervisor)
    _poll(store, clock, supervisor)

    assert release_held(store.uow())
    fixes = _fix_tasks(store)
    assert len(fixes) == 1
    assert fixes[0].state is TaskState.SCHEDULED
    contract = store.contracts.get(fixes[0].id, 1)
    assert contract is not None
    objective = contract.document["objective"]
    assert "test: failure" in objective
    assert f"#{PR_NUMBER}" in objective
    assert "the required check failed" in objective  # the failing job's output
    assert contract.document["repository"]["work_branch"] == f"crucible/{fixes[0].external_id}"
    document = hold_document(store.uow())
    assert (document["merge_sha"], document["fix_task_id"]) == (MAIN_RED, fixes[0].id)

    # A re-run turns the held commit green: the hold clears.
    client.fake.set_check(REPOSITORY, MAIN_RED, name="test", conclusion="success")
    _poll(store, clock, supervisor)
    assert not release_held(store.uow())
    assert hold_document(store.uow())["watching"] == []


def test_a_stale_green_commit_does_not_clear_a_newer_red_hold(tmp_path: Path) -> None:
    """Codex P1 on PR 417: the newer commit is red and holds the release; the older one
    finishes green later. Its green is superseded and must not lift the hold."""
    store, clock, supervisor, client = _main_world(tmp_path)
    _merged_task(store, clock, sha=MAIN_OLD_GREEN, at=NOW, number=401)
    _merged_task(store, clock, sha=MAIN_RED, at=NOW + timedelta(minutes=5), number=402)
    client.fake.set_check(
        REPOSITORY, MAIN_OLD_GREEN, name="test", status="in_progress", conclusion=None
    )
    client.fake.set_check(REPOSITORY, MAIN_RED, name="test", conclusion="failure")

    _poll(store, clock, supervisor)
    assert release_held(store.uow())

    client.fake.set_check(REPOSITORY, MAIN_OLD_GREEN, name="test", conclusion="success")
    _poll(store, clock, supervisor)

    assert release_held(store.uow())
    document = hold_document(store.uow())
    assert document["merge_sha"] == MAIN_RED
    # The superseded commit is no longer watched; the held one is.
    assert [e["merge_sha"] for e in document["watching"]] == [MAIN_RED]
    assert document["merged_pull_requests"] == [401, 402]

    # A newer green commit clears the hold only once it is main's verified tip.
    _merged_task(store, clock, sha=MAIN_NEWER, at=NOW + timedelta(minutes=9), number=403)
    client.fake.set_check(REPOSITORY, MAIN_NEWER, name="test", conclusion="success")
    client.repo.branches["main"] = MAIN_NEWER
    _poll(store, clock, supervisor)
    assert not release_held(store.uow())
    assert hold_document(store.uow())["last_green_sha"] == MAIN_NEWER


def test_a_green_commit_that_is_not_main_tip_does_not_clear_the_hold(tmp_path: Path) -> None:
    store, clock, supervisor, client = _main_world(tmp_path)
    _merged_task(store, clock, sha=MAIN_RED, at=NOW, number=402)
    client.fake.set_check(REPOSITORY, MAIN_RED, name="test", conclusion="failure")
    _poll(store, clock, supervisor)
    _merged_task(store, clock, sha=MAIN_NEWER, at=NOW + timedelta(minutes=9), number=403)
    client.fake.set_check(REPOSITORY, MAIN_NEWER, name="test", conclusion="success")
    client.repo.branches["main"] = "9" * 40

    _poll(store, clock, supervisor)

    assert release_held(store.uow())
    assert store.provider_settings.get(SETTING_NAME) is not None


# ----- the merge-main script, against real git ----------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _remote(tmp_path: Path, *, conflict: bool) -> tuple[Path, str]:
    """A bare origin with `main` and a work branch whose tip is returned. Main moves on
    after the branch is cut, in another file, or in the branch's own line."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    work = tmp_path / "seed"
    _git(tmp_path, "clone", "--quiet", str(origin), str(work))
    (work / "a.txt").write_text("base\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "--quiet", "-m", "base")
    _git(work, "push", "--quiet", "origin", "HEAD:refs/heads/main")
    _git(work, "checkout", "--quiet", "-b", WORK_BRANCH)
    (work / "a.txt").write_text("branch\n")
    _git(work, "commit", "--quiet", "-am", "branch")
    _git(work, "push", "--quiet", "origin", f"HEAD:refs/heads/{WORK_BRANCH}")
    tip = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "--quiet", "main")
    if conflict:
        (work / "a.txt").write_text("main\n")
    else:
        (work / "b.txt").write_text("main\n")
        _git(work, "add", "b.txt")
    _git(work, "commit", "--quiet", "-am", "main moves")
    _git(work, "push", "--quiet", "origin", "HEAD:refs/heads/main")
    return origin, tip


def _run_merge_main(tmp_path: Path, origin: Path, expected: str) -> Any:
    root = tmp_path / "run"
    (root / "tok").mkdir(parents=True)
    script = scripts.merge_main_script(
        clone_url=str(origin),
        work_branch=WORK_BRANCH,
        base_ref="main",
        expected_head=expected,
        author_name="Crucible",
        author_email="crucible@example.invalid",
        token_dir=str(root / "tok"),
        out_dir=str(root / "out"),
        work_root=str(root),
    )
    # The helper and its git config go to this test's own directory, not the shared /tmp.
    for name in ("cred-helper.sh", "gitconfig"):
        script = script.replace(f"/tmp/{name}", f"{root}/{name}")
    env = {"PATH": os.environ["PATH"]}
    done = subprocess.run(
        ["sh", "-c", script],
        input=b"a-test-token",
        env=env,
        cwd=root,
        capture_output=True,
        check=False,
    )
    files = {
        path.name: path.read_text(encoding="utf-8")
        for path in (root / "out").iterdir()
        if path.is_file()
    }
    return merge_outcome_from_files(files, done.returncode)


def test_the_merge_main_script_pushes_a_clean_merge_with_a_lease(tmp_path: Path) -> None:
    origin, tip = _remote(tmp_path, conflict=False)

    outcome = _run_merge_main(tmp_path, origin, tip)

    assert outcome.merged, outcome
    assert _git(origin, "rev-parse", f"refs/heads/{WORK_BRANCH}") == outcome.head_sha
    parents = _git(origin, "rev-list", "--parents", "-n", "1", outcome.head_sha).split()
    assert parents[1:] == [tip, _git(origin, "rev-parse", "refs/heads/main")]
    assert _git(origin, "log", "-1", "--format=%an", outcome.head_sha) == "Crucible"
    assert not (tmp_path / "run" / "tok" / "token").exists()


def test_the_merge_main_script_reports_conflicts_and_leaves_the_branch(tmp_path: Path) -> None:
    origin, tip = _remote(tmp_path, conflict=True)

    outcome = _run_merge_main(tmp_path, origin, tip)

    assert not outcome.merged
    assert outcome.conflicting_files == ("a.txt",)
    assert outcome.exit_code == scripts.MERGE_MAIN_CONFLICT
    assert _git(origin, "rev-parse", f"refs/heads/{WORK_BRANCH}") == tip


def test_the_merge_main_script_refuses_a_tip_that_moved(tmp_path: Path) -> None:
    origin, tip = _remote(tmp_path, conflict=False)

    outcome = _run_merge_main(tmp_path, origin, OTHER_HEAD)

    assert not outcome.merged
    assert (outcome.step, outcome.exit_code) == ("head-moved", scripts.MERGE_MAIN_HEAD_MOVED)
    assert _git(origin, "rev-parse", f"refs/heads/{WORK_BRANCH}") == tip


@pytest.mark.parametrize("state", ["dirty", "clean"])
def test_the_fake_pull_request_carries_mergeable(state: str) -> None:
    fake = FakeGitHub()
    client = _Client(fake)
    _open_pull(client, mergeable_state=state)
    assert client._ref().mergeable is (state != "dirty")
