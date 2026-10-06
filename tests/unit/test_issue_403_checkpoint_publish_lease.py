"""Issue 403: run the real publisher against a bare remote, including republish."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from crucible.adapters.execution import scripts
from crucible.adapters.execution.publisher import outcome_from_files
from crucible.application.publish import build_plan, record_publish_started
from crucible.application.republish import republish_task
from crucible.application.transitions import move_task, record_event
from crucible.contracts.api import PublishRetryRequest
from crucible.domain.entities import AcceptanceResult, AcceptanceVerdict
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.lifecycle import TaskState
from crucible.domain.publication import publication_owned_heads
from crucible.ports.github import InstallationToken
from crucible.ports.publish import PublishOutcome, PublishRequest
from tests.unit.test_commit_trailer import _git, _worker_branch
from tests.unit.test_issue_332_republish_k8s import _AcceptanceRepo
from tests.unit.test_issue_360_ready_for_merge_correction import (
    TASK_ID,
    _correction_attempt,
    _principal,
)
from tests.unit.test_issue_379_merge_during_publish import (
    _correcting,
    _publish,
    _Publisher,
    _supervisor,
    _task,
)

BRANCH = "crucible/test"


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _history(tmp_path: Path, *, trailer: bool = True) -> tuple[Path, Path, str, str]:
    repo = _worker_branch(tmp_path)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(repo, "push", "-q", str(origin), "main")
    message = "checkpoint" + ("\n\nCrucible-Attempt: prior-attempt" if trailer else "")
    checkpoint = _commit(repo, "work.txt", "retained work\n", message)
    _git(repo, "push", "-q", str(origin), BRANCH)
    _git(repo, "reset", "--hard", "main")
    _commit(repo, "work.txt", "retained work\n", "corrected work")
    head = _commit(repo, "extra.txt", "correction\n", "complete correction")
    return repo, origin, checkpoint, head


def _bundle(repo: Path, directory: Path, branch: str = BRANCH) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    bundle = directory / "work_branch.bundle"
    _git(repo, "bundle", "create", str(bundle), f"main..{branch}")
    return bundle


def _run(
    root: Path,
    origin: Path,
    bundle: Path,
    head: str,
    *,
    branch: str = BRANCH,
    owned: tuple[str, ...] = (),
    race: str = "",
) -> PublishOutcome:
    for name in ("tmp", "token", "out", "home", "bin"):
        (root / name).mkdir(parents=True, exist_ok=True)
    script = scripts.publisher_script(
        clone_url="TEST-REMOTE",
        work_branch=branch,
        base_ref="main",
        expected_head=head,
        author_name="Hades",
        author_email="hades@example.test",
        bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        owned_remote_heads=owned,
    )
    script = script.replace("/tmp/", f"{root}/tmp/")
    for old, new in (
        (scripts.TOKEN_MOUNT, root / "token"),
        (scripts.PUBLISH_MOUNT, root / "out"),
        (scripts.BUNDLE_MOUNT, bundle.parent),
        ("/home/worker", root / "home"),
        ("TEST-REMOTE", origin),
    ):
        script = script.replace(old, str(new))
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if race:
        git = shutil.which("git")
        assert git is not None
        wrapper = root / "bin" / "git"
        wrapper.write_text(
            '#!/bin/sh\nif [ "$1" = push ]; then\n'
            f"{shlex.quote(git)} --git-dir={shlex.quote(str(origin))} "
            f"update-ref refs/heads/{branch} {race}\nfi\n"
            f'exec {shlex.quote(git)} "$@"\n'
        )
        wrapper.chmod(0o755)
        env["PATH"] = f"{wrapper.parent}:{env['PATH']}"
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=root,
        env=env,
        input="test-credential",
        capture_output=True,
        text=True,
        check=False,
    )
    return outcome_from_files(
        {p.name: p.read_text() for p in (root / "out").iterdir()}, result.returncode
    )


@pytest.mark.parametrize("trailer", [True, False])
def test_owned_checkpoint_is_replaced_by_corrected_head(tmp_path: Path, trailer: bool) -> None:
    repo, origin, checkpoint, head = _history(tmp_path, trailer=trailer)
    outcome = _run(
        tmp_path / "run",
        origin,
        _bundle(repo, tmp_path / "bundle"),
        head,
        owned=() if trailer else (checkpoint,),
    )
    assert outcome.pushed, outcome
    assert outcome.remote_head_before == checkpoint
    assert _git(origin, "rev-parse", f"refs/heads/{BRANCH}").strip() == head


@pytest.mark.parametrize("ancestor", [False, True])
def test_foreign_tip_names_commit_and_author_even_for_fast_forward(
    tmp_path: Path, ancestor: bool
) -> None:
    repo, origin, checkpoint, head = _history(tmp_path, trailer=False)
    if ancestor:
        _git(repo, "merge", "--no-edit", checkpoint)
        head = _git(repo, "rev-parse", "HEAD").strip()
    author = _git(repo, "show", "-s", "--format=%an <%ae>", checkpoint).strip()
    outcome = _run(tmp_path / "run", origin, _bundle(repo, tmp_path / "bundle"), head)
    assert not outcome.pushed
    assert outcome.step == "remote-ownership"
    assert checkpoint in outcome.detail and author in outcome.detail
    assert _git(origin, "rev-parse", f"refs/heads/{BRANCH}").strip() == checkpoint


@pytest.mark.parametrize("conflict", [False, True])
def test_checkpoint_work_cannot_be_discarded(tmp_path: Path, conflict: bool) -> None:
    repo, origin, checkpoint, _ = _history(tmp_path)
    _git(repo, "reset", "--hard", "main")
    head = _commit(repo, "work.txt" if conflict else "other.txt", "different\n", "correction")
    outcome = _run(tmp_path / "run", origin, _bundle(repo, tmp_path / "bundle"), head)
    assert not outcome.pushed
    assert outcome.step == "checkpoint-contained"
    assert checkpoint in outcome.detail
    assert _git(origin, "rev-parse", f"refs/heads/{BRANCH}").strip() == checkpoint


@pytest.mark.parametrize("absent", [False, True])
def test_exact_lease_refuses_a_concurrent_writer(tmp_path: Path, absent: bool) -> None:
    repo, origin, checkpoint, head = _history(tmp_path)
    if absent:
        _git(origin, "update-ref", "-d", f"refs/heads/{BRANCH}")
    # The checkpoint is already an object on the remote; main is a different racing tip.
    race = checkpoint if absent else _git(origin, "rev-parse", "main").strip()
    outcome = _run(tmp_path / "run", origin, _bundle(repo, tmp_path / "bundle"), head, race=race)
    assert not outcome.pushed and outcome.step == "push"
    assert _git(origin, "rev-parse", f"refs/heads/{BRANCH}").strip() == race


def test_absent_branch_and_owned_ancestor_publish(tmp_path: Path) -> None:
    repo, origin, checkpoint, _ = _history(tmp_path)
    _git(repo, "reset", "--hard", checkpoint)
    head = _commit(repo, "next.txt", "next\n", "next")
    bundle = _bundle(repo, tmp_path / "bundle")
    assert _run(tmp_path / "ancestor", origin, bundle, head).pushed
    _git(origin, "update-ref", "-d", f"refs/heads/{BRANCH}")
    assert _run(tmp_path / "absent", origin, bundle, head).pushed


class _RealPublisher(_Publisher):
    def __init__(self, root: Path, origin: Path) -> None:
        super().__init__()
        self.root, self.origin = root, origin
        self.requests: list[PublishRequest] = []

    async def push(self, request: PublishRequest, token: InstallationToken) -> PublishOutcome:
        self.requests.append(request)
        return _run(
            self.root,
            self.origin,
            Path(request.bundle_path),
            request.expected_head,
            branch=request.work_branch,
            owned=request.owned_remote_heads,
        )


@pytest.mark.parametrize("failure_step", ["push", "branch_pushed_at_head", "github"])
def test_republish_recovers_the_same_sealed_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_step: str
) -> None:
    repo, origin, checkpoint, head = _history(tmp_path, trailer=False)
    store, clock, _, github, _ = _correcting(tmp_path)
    task = _task(store)
    task.head_sha = head
    execution, attempt = _correction_attempt(store)
    attempt.workspace_path = str(tmp_path / "workspace")
    plan = build_plan(store.uow(), task, (attempt, execution))
    _git(repo, "branch", "-m", BRANCH, plan.work_branch)
    _git(origin, "update-ref", f"refs/heads/{plan.work_branch}", checkpoint)
    bundle = _bundle(repo, Path(attempt.workspace_path) / "output", plan.work_branch)
    seal = hashlib.sha256(bundle.read_bytes()).hexdigest()
    record_publish_started(store.uow(), clock, replace(plan, bundle_sha256=seal))
    move_task(
        store.uow(),
        clock,
        task,
        TaskState.PUBLISH_FAILED,
        EventKind.TASK_PUBLISH_FAILED,
        attempt_id=attempt.id,
        payload={"head_sha": head, "step": failure_step},
    )
    event = record_event(
        store.uow(),
        clock,
        EventKind.BRANCH_PUSHED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        payload={
            "head_sha": checkpoint,
            "work_branch": plan.work_branch,
            "repository": plan.repository_name,
            "checkpoint": True,
        },
    )
    # A record for another branch, repository, or principal never establishes ownership.
    for invalid in (
        replace(event, principal="worker"),
        replace(event, verified=False),
        replace(event, payload={**event.payload, "work_branch": "other"}),
        replace(event, payload={**event.payload, "repository": "other/repo"}),
    ):
        assert not publication_owned_heads(
            [invalid], work_branch=plan.work_branch, repository=plan.repository_name
        )
    monkeypatch.setattr(
        store,
        "acceptance",
        _AcceptanceRepo(
            [
                AcceptanceResult(
                    id="accepted",
                    task_id=TASK_ID,
                    head_sha=head,
                    principal_id=task.principal_id,
                    verdict=AcceptanceVerdict.ACCEPTED,
                    reasoning="verified",
                    created_at=clock.now(),
                )
            ]
        ),
        raising=False,
    )
    republish_task(
        store.uow(),
        clock,
        principal=_principal(),
        task_id=TASK_ID,
        request=PublishRetryRequest(reason="recover checkpoint publication"),
    )
    publisher = _RealPublisher(tmp_path / "republish", origin)
    github.remote = head
    supervisor = _supervisor(store, clock, tmp_path, github, publisher)
    assert _publish(supervisor) == 1
    assert publisher.requests[0].owned_remote_heads == (checkpoint,)
    assert _task(store).state is not TaskState.PUBLISH_FAILED
    assert _git(origin, "rev-parse", f"refs/heads/{plan.work_branch}").strip() == head
