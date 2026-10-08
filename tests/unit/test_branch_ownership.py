"""Branch ownership enforcement: submit refuses duplicate work_branch, derives
crucible/<external_id> when omitted, and the publisher script validates ownership.

hades #564
"""

from __future__ import annotations

from typing import Any

from crucible.adapters.execution import scripts
from crucible.application.submit_task import (
    _check_branch_ownership,
    _derive_work_branch,
)
from crucible.contracts.task_contract import TaskContractV1
from crucible.domain.lifecycle import TaskState
from tests.fixtures import contract_document

# ---- _derive_work_branch -----------------------------------------------------


def test_derive_work_branch_omitted() -> None:
    """AC2: when work_branch is None, it becomes crucible/<external_id>."""
    base = contract_document()
    base["repository"]["work_branch"] = None
    contract = TaskContractV1.model_validate(base)
    assert contract.repository.work_branch is None
    derived = _derive_work_branch(contract)
    assert derived.repository.work_branch == "crucible/EX-0001"
    # external_id was not changed
    assert derived.external_id == "EX-0001"


def test_derive_work_branch_explicit_unchanged() -> None:
    """When work_branch is present it is left alone."""
    base = contract_document()
    base["repository"]["work_branch"] = "crucible/MY-TASK"
    contract = TaskContractV1.model_validate(base)
    derived = _derive_work_branch(contract)
    assert derived.repository.work_branch == "crucible/MY-TASK"


# ---- _check_branch_ownership -------------------------------------------------


class _FakeTaskRow:
    """A minimal task row for the ownership check."""

    def __init__(
        self,
        task_id: str,
        repository_id: str,
        contract_version: int,
        external_id: str,
    ) -> None:
        self.id = task_id
        self.repository_id = repository_id
        self.contract_version = contract_version
        self.external_id = external_id
        self.state = TaskState.SUBMITTED


class _FakeContractRow:
    """A minimal contract row."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document


class _FakeTaskRepo:
    """A minimal task repository stub."""

    def __init__(
        self,
        tasks: list[_FakeTaskRow],
        contracts: dict[tuple[str, int], _FakeContractRow],
    ) -> None:
        self._tasks = tasks
        self._contracts = contracts

    def list_for_repository(self, repository_id: str) -> list[_FakeTaskRow]:
        return [t for t in self._tasks if t.repository_id == repository_id]


class _FakeUow:
    """Minimal stub of the unit-of-work for _check_branch_ownership."""

    def __init__(self) -> None:
        self._tasks: list[_FakeTaskRow] = []
        self._contracts: dict[tuple[str, int], _FakeContractRow] = {}
        self.tasks = _FakeTaskRepo(self._tasks, self._contracts)
        self.contracts = _FakeContractsRepo(self._contracts)

    def add_task(
        self,
        task_id: str,
        repository_id: str,
        contract_version: int,
        doc: dict[str, Any],
        external_id: str = "",
    ) -> None:
        self._tasks.append(
            _FakeTaskRow(
                task_id,
                repository_id,
                contract_version,
                external_id or doc.get("external_id", ""),
            )
        )
        self._contracts[(task_id, contract_version)] = _FakeContractRow(doc)


class _FakeContractsRepo:
    """Stub for uow.contracts that exposes a .get() method."""

    def __init__(self, contracts: dict[tuple[str, int], _FakeContractRow]) -> None:
        self._contracts = contracts

    def get(self, task_id: str, version: int) -> _FakeContractRow | None:
        return self._contracts.get((task_id, version))


def test_check_ownership_refuses_duplicate_branch() -> None:
    """AC1: submitting a work_branch already owned by another task returns 422."""
    uow = _FakeUow()
    repo_id = "repo-1"
    base_doc = contract_document(external_id="EX-OLD")
    base_doc["repository"]["work_branch"] = "crucible/dup-branch"
    uow.add_task("task-old", repo_id, 1, base_doc, "EX-OLD")

    problems = _check_branch_ownership(uow, repo_id, "crucible/dup-branch")  # type: ignore[arg-type]
    assert len(problems) == 1
    assert problems[0]["path"] == "repository.work_branch"
    assert "EX-OLD" in problems[0]["message"]


def test_check_ownership_allows_unique_branch() -> None:
    """When no task uses the branch, no problem is returned."""
    uow = _FakeUow()
    repo_id = "repo-1"
    base_doc = contract_document(external_id="EX-OLD")
    base_doc["repository"]["work_branch"] = "crucible/old-branch"
    uow.add_task("task-old", repo_id, 1, base_doc, "EX-OLD")

    problems = _check_branch_ownership(uow, repo_id, "crucible/new-branch")  # type: ignore[arg-type]
    assert problems == []


def test_check_ownership_cross_repo_allows() -> None:
    """Branches on different repositories do not conflict."""
    uow = _FakeUow()
    base_doc = contract_document(external_id="EX-OLD")
    base_doc["repository"]["work_branch"] = "crucible/dup"
    uow.add_task("task-old", "repo-a", 1, base_doc, "EX-OLD")

    problems = _check_branch_ownership(uow, "repo-b", "crucible/dup")  # type: ignore[arg-type]
    assert problems == []


def test_check_ownership_across_states() -> None:
    """Branch ownership check applies regardless of task state."""
    uow = _FakeUow()
    repo_id = "repo-1"
    base_doc = contract_document(external_id="EX-MERGED")
    base_doc["repository"]["work_branch"] = "crucible/shared-branch"
    uow.add_task("task-merged", repo_id, 1, base_doc, "EX-MERGED")
    # Mutate the task row to look merged
    for t in uow._tasks:
        if t.id == "task-merged":
            t.state = TaskState.MERGED

    problems = _check_branch_ownership(uow, repo_id, "crucible/shared-branch")  # type: ignore[arg-type]
    assert len(problems) == 1
    assert "EX-MERGED" in problems[0]["message"]


# ---- publisher script: own_pr_number and attempt_id --------------------------


def test_publisher_script_contains_own_pr_number() -> None:
    """AC3: own_pr_number is passed to the script and rendered."""
    script = scripts.publisher_script(
        clone_url="https://github.com/owner/repo.git",
        work_branch="crucible/FDY-0042",
        base_ref="main",
        expected_head="a" * 40,
        author_name="test",
        author_email="test@test.com",
        own_pr_number=42,
        attempt_id="01ATTEMPT",
    )
    assert "OWN_PR_NUMBER=42" in script


def test_publisher_script_empty_own_pr_number_default() -> None:
    """When own_pr_number is None, it renders as 0."""
    script = scripts.publisher_script(
        clone_url="https://github.com/owner/repo.git",
        work_branch="crucible/FDY-0042",
        base_ref="main",
        expected_head="a" * 40,
        author_name="test",
        author_email="test@test.com",
    )
    assert "OWN_PR_NUMBER=0" in script


def test_publisher_script_contains_attempt_id() -> None:
    """The attempt ID is exported so the shell can compare it with the trailer."""
    script = scripts.publisher_script(
        clone_url="https://github.com/owner/repo.git",
        work_branch="crucible/FDY-0042",
        base_ref="main",
        expected_head="a" * 40,
        author_name="test",
        author_email="test@test.com",
        attempt_id="01ATTEMPT",
    )
    # _quote wraps in single quotes: CRUCIBLE_ATTEMPT_ID='01ATTEMPT'
    assert "CRUCIBLE_ATTEMPT_ID=" in script
    assert "01ATTEMPT" in script


def test_publisher_script_remote_ownership_compares_attempt_trailer() -> None:
    """The trailer check compares the actual attempt ID, not just presence."""
    script = scripts.publisher_script(
        clone_url="https://github.com/owner/repo.git",
        work_branch="crucible/FDY-0042",
        base_ref="main",
        expected_head="a" * 40,
        author_name="test",
        author_email="test@test.com",
        attempt_id="01ATTEMPT",
    )
    # Must contain the comparison with CRUCIBLE_ATTEMPT_ID
    assert "REMOTE_ATTEMPT" in script
    assert "CRUCIBLE_ATTEMPT_ID" in script
    assert "$REMOTE_ATTEMPT" in script
    assert "$CRUCIBLE_ATTEMPT_ID" in script


def test_publisher_script_fails_closed_on_pr_lookup_failure() -> None:
    """When the PR lookup curl fails, publication is refused (fail closed)."""
    script = scripts.publisher_script(
        clone_url="https://github.com/owner/repo.git",
        work_branch="crucible/FDY-0042",
        base_ref="main",
        expected_head="a" * 40,
        author_name="test",
        author_email="test@test.com",
        attempt_id="01ATTEMPT",
        repo_owner="owner",
        repo_name="repo",
    )
    # The script should have an error output for a failed PR lookup
    assert "remote ownership PR lookup failed" in script
    assert "exit 5" in script
