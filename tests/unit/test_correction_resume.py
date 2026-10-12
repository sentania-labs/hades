"""A correction resumes the exact preceding head, before and after publication."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from crucible.adapters.execution import scripts
from crucible.application.corrections import (
    PREVIOUS_BUNDLE_GONE,
    PREVIOUS_BUNDLE_OTHER_PROVIDER,
    _unpublished_bundle_problem,
)
from crucible.domain.entities import Task
from crucible.domain.lifecycle import TaskState
from crucible.ports.execution import WORK_MOUNT


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, str, Path]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.test")
    (seed / "file.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "file.txt")
    _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")
    _git(seed, "checkout", "-b", "crucible/FDY-0150")
    (seed / "file.txt").write_text("correct me\n", encoding="utf-8")
    _git(seed, "commit", "-am", "previous attempt")
    head = _git(seed, "rev-parse", "HEAD")
    bundle = tmp_path / "work_branch.bundle"
    _git(seed, "bundle", "create", str(bundle), "main..crucible/FDY-0150")
    return origin, seed, head, bundle


def _prepare(
    tmp_path: Path,
    origin: Path,
    *,
    bundle: Path | None = None,
    head: str | None = None,
) -> str:
    work = tmp_path / "work"
    identity = tmp_path / "identity"
    identity.mkdir()
    script = scripts.preparer_script(
        url=str(origin),
        base_ref="main",
        work_branch="crucible/FDY-0150",
        from_remote_branch=True,
        cache_name=None,
        author_name="Test",
        author_email="test@example.test",
        origin_placeholder="https://invalid.example/repository.git",
        claude_md_wins=False,
        shims=(),
        exclude_entries=(),
        identity_mount=str(identity),
        resume_bundle=str(bundle) if bundle else None,
        resume_bundle_head=head,
        resume_bundle_sha256=(hashlib.sha256(bundle.read_bytes()).hexdigest() if bundle else None),
    )
    script = script.replace(WORK_MOUNT, str(work))
    script = script.replace("/tmp/gitconfig", str(tmp_path / "gitconfig"))
    subprocess.run(["sh", "-c", script], check=True, capture_output=True, text=True)
    return _git(work / "repo", "rev-parse", "HEAD")


def test_a_pre_pr_corrections_prepared_checkout_starts_at_the_previous_head(
    tmp_path: Path,
) -> None:
    origin, _seed, head, bundle = _repository(tmp_path)

    assert _prepare(tmp_path, origin, bundle=bundle, head=head) == head


def test_a_published_tasks_correction_still_resumes_from_the_remote_branch(
    tmp_path: Path,
) -> None:
    origin, seed, head, _bundle = _repository(tmp_path)
    _git(seed, "push", "origin", "crucible/FDY-0150")

    assert _prepare(tmp_path, origin) == head


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def list_for_task(self, _task_id: str) -> list[Any]:
        return self.rows

    def list_for_execution(self, execution_id: str) -> list[Any]:
        return [row for row in self.rows if row.execution_id == execution_id]


def test_a_correction_whose_previous_bundle_is_gone_is_refused_with_a_named_reason() -> None:
    task = SimpleNamespace(id="task", state=TaskState.PRE_PR_GATES_FAILED)
    attempt = SimpleNamespace(id="attempt", execution_id="execution", workspace_path="gone")
    uow = SimpleNamespace(
        events=SimpleNamespace(latest_for_task_kind=lambda *_args: None),
        executions=_Rows([SimpleNamespace(id="execution", role="implement", provider="docker")]),
        attempts=_Rows([attempt]),
        evidence=SimpleNamespace(list_for_attempt=lambda _attempt_id: []),
        retention=SimpleNamespace(list_recent=lambda _limit: []),
    )

    assert _unpublished_bundle_problem(uow, cast(Task, task), "docker") == {
        "path": "correction",
        "message": PREVIOUS_BUNDLE_GONE,
    }


def _bundle_uow(provider: str, *, stored: bool) -> Any:
    attempt = SimpleNamespace(id="attempt", execution_id="execution", workspace_path="k8s://ws")
    execution = SimpleNamespace(id="execution", role="implement", provider=provider)
    row = SimpleNamespace(
        kind="bundle_head",
        verified=True,
        attempt_id="attempt",
        artifact_id="bundle" if stored else None,
        payload={"bundle_verified": True, "bundle_sha256": "seal", "head_sha": "head"},
    )
    artifact = SimpleNamespace(
        id="bundle", type="work_bundle", attempt_id="attempt", sha256="seal", path="blobs/x"
    )
    return SimpleNamespace(
        events=SimpleNamespace(latest_for_task_kind=lambda *_args: None),
        executions=_Rows([execution]),
        attempts=_Rows([attempt]),
        evidence=SimpleNamespace(list_for_attempt=lambda _attempt_id: [row]),
        artifacts=SimpleNamespace(get=lambda _id: artifact if stored else None),
        retention=SimpleNamespace(list_recent=lambda _limit: []),
    )


def test_a_pre_pr_correction_cannot_change_the_provider_of_an_unstored_bundle() -> None:
    """A bundle collected before ADR 0032 stored it lives only in its workspace."""
    task = SimpleNamespace(id="task", state=TaskState.PRE_PR_GATES_FAILED)
    uow = _bundle_uow("kubernetes", stored=False)

    assert _unpublished_bundle_problem(uow, cast(Task, task), "docker") == {
        "path": "execution_request.provider",
        "message": PREVIOUS_BUNDLE_OTHER_PROVIDER,
    }
    # No claim is mounted for it any more, so on Kubernetes itself it is gone.
    assert _unpublished_bundle_problem(uow, cast(Task, task), "kubernetes") == {
        "path": "correction",
        "message": PREVIOUS_BUNDLE_GONE,
    }


def test_a_stored_bundle_resumes_on_either_provider() -> None:
    """ADR 0032: the artifact store is read by whichever provider runs the correction,
    so the other-provider refusal is moot for a stored bundle."""
    task = SimpleNamespace(id="task", state=TaskState.PRE_PR_GATES_FAILED)
    for provider in ("docker", "kubernetes"):
        uow = _bundle_uow("kubernetes", stored=True)
        assert _unpublished_bundle_problem(uow, cast(Task, task), provider) is None
