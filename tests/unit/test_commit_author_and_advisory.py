"""FDY-0604: task commits share one author and advisory failures annotate."""

from __future__ import annotations

import asyncio
from pathlib import Path

from crucible.adapters.execution import scripts
from crucible.adapters.execution.docker import LAUNCH_WRAPPER
from crucible.application.publish import build_plan
from crucible.application.review import latest_work_attempt
from crucible.domain.git_identity import COMMIT_AUTHOR_EMAIL, COMMIT_AUTHOR_NAME
from crucible.domain.lifecycle import TaskState
from tests.unit.test_issue_379_merge_during_publish import _task
from tests.unit.test_issue_402_self_review_acceptance import _collected


def test_every_harness_gets_the_fixed_commit_author() -> None:
    for variable in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        assert f"export {variable}={COMMIT_AUTHOR_NAME}" in LAUNCH_WRAPPER
    for variable in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        assert f"export {variable}={COMMIT_AUTHOR_EMAIL}" in LAUNCH_WRAPPER


def test_hades_git_scripts_ignore_a_foreign_policy_author() -> None:
    publisher = scripts.publisher_script(
        clone_url="https://example.invalid/repo.git",
        work_branch="work",
        base_ref="main",
        expected_head="a" * 40,
        author_name="Foreign Name",
        author_email="foreign@example.invalid",
    )
    merge = scripts.merge_main_script(
        clone_url="https://example.invalid/repo.git",
        work_branch="work",
        base_ref="main",
        expected_head="a" * 40,
        author_name="Foreign Name",
        author_email="foreign@example.invalid",
    )
    for generated in (publisher, merge):
        assert f"export CRUCIBLE_AUTHOR_NAME='{COMMIT_AUTHOR_NAME}'" in generated
        assert f"export CRUCIBLE_AUTHOR_EMAIL='{COMMIT_AUTHOR_EMAIL}'" in generated
        assert "Foreign Name" not in generated
        assert "foreign@example.invalid" not in generated


def test_commit_policy_excludes_commits_reachable_from_prepared_main() -> None:
    collector = scripts.collector_script(base_ref="main", work_branch="work", size_cap_bytes=1000)
    assert (
        'commit_policy_check "$POLICY_FROM..HEAD" "$OUT/commit-policy" "$PREPARED_BASE"'
        in collector
    )
    assert 'cat "$OUT/prepared-base.txt"' in collector


def test_advisory_failure_annotates_and_publishes(tmp_path: Path) -> None:
    store, supervisor, _github, _publisher = _collected(tmp_path, advisory_failed=True)
    supervisor._evaluate_pending_gates()
    assert _task(store).state is TaskState.PUBLISHING
    assert len(store.acceptance.rows) == 1
    task = _task(store)
    work = latest_work_attempt(store.uow(), task)
    assert work is not None
    body = build_plan(store.uow(), task, work).body
    assert "## Reviewer notes" in body
    assert "scope_contained:" in body
    assert asyncio.run(supervisor.delivery.publish()) == 1
    assert _task(store).state is TaskState.AWAITING_EXTERNAL_REVIEW


def test_blocking_failure_still_parks(tmp_path: Path) -> None:
    store, supervisor, _github, publisher = _collected(tmp_path, failed_check=True)
    supervisor._evaluate_pending_gates()
    assert _task(store).state is TaskState.PRE_PR_GATES_FAILED
    assert store.acceptance.rows == []
    assert asyncio.run(supervisor.delivery.publish()) == 0
    assert publisher.pushes == []
