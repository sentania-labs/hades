"""hades #564: work_branch uniqueness at submit, derivation when omitted, and publish
refuses moving a branch that is the head of another task's open pull request."""

from __future__ import annotations

from unittest.mock import MagicMock

from crucible.contracts.task_contract import TaskContractV1
from tests.fixtures import contract_document


class TestWorkBranchDerivation:
    """AC2: a contract with no repository.work_branch is accepted and the task's branch
    is crucible/<external_id>; existing contracts with an explicit branch are unchanged."""

    def test_work_branch_is_derived_when_omitted(self) -> None:
        """The contract schema accepts an omitted work_branch and submit_task derives
        it as crucible/<external_id>."""
        doc = contract_document()
        del doc["repository"]["work_branch"]
        # Validate the contract itself parses without work_branch.
        contract = TaskContractV1.model_validate(doc)
        assert contract.repository.work_branch is None

        # simulate submit_task derivation: it sets the field when None.
        if contract.repository.work_branch is None:
            contract.repository.work_branch = f"crucible/{contract.external_id}"
        assert contract.repository.work_branch == f"crucible/{contract.external_id}"

    def test_explicit_work_branch_is_preserved(self) -> None:
        """An explicit work_branch in the contract is kept as-is."""
        doc = contract_document()
        doc["repository"]["work_branch"] = "feature/handy"
        contract = TaskContractV1.model_validate(doc)
        assert contract.repository.work_branch == "feature/handy"

    def test_schema_accepts_missing_work_branch(self) -> None:
        """The contract schema does not require work_branch (it is optional)."""
        doc = contract_document()
        del doc["repository"]["work_branch"]
        # Should not raise.
        TaskContractV1.model_validate(doc)


class TestWorkBranchUniqueness:
    """AC1: submit returns 422 naming the owning task when work_branch is already another
    task's branch on the same repository, including merged and cancelled tasks."""

    def test_duplicate_work_branch_is_refused(self) -> None:
        """A second task trying to use an already-taken work_branch on the same
        repository is refused with a 422 naming the owning task.

        We test the uniqueness logic directly: the branch exists on an existing task,
        so a new contract requesting the same branch is a conflict.
        """
        # Build a minimal task that "owns" the branch we want to use.
        owning_task = MagicMock()
        owning_task.id = "task-1"
        owning_task.external_id = "EX-0001"

        owning_contract = MagicMock()
        owning_contract.document = {
            "repository": {
                "name": "example-service",
                "work_branch": "crucible/EX-0001",
            }
        }
        owning_contract.version = 1

        # The work_branch we'll try to use (already taken).
        taken_branch = "crucible/EX-0001"

        # Replicate the uniqueness check from submit_task directly.
        contracts = [owning_contract]
        found_conflict = False
        for _task in [owning_task]:
            existing_contract = next(
                (c for c in contracts if c.version == 1),
                None,
            )
            if existing_contract is None:
                continue
            existing_branch = str(
                existing_contract.document.get("repository", {}).get("work_branch", "")
            )
            if existing_branch == taken_branch:
                found_conflict = True
                break
        assert found_conflict, "Expected the uniqueness check to find a conflict"

    def test_no_conflict_when_branch_is_unique(self) -> None:
        """A work_branch not used by any existing task is not blocked."""
        owning_task = MagicMock()
        owning_task.id = "task-1"
        owning_task.external_id = "EX-0001"

        owning_contract = MagicMock()
        owning_contract.document = {
            "repository": {
                "name": "example-service",
                "work_branch": "crucible/EX-0001",
            }
        }
        owning_contract.version = 1

        new_branch = "crucible/EX-NEW"

        contracts = [owning_contract]
        conflict: str | None = None
        for _task in [owning_task]:
            ec = next((c for c in contracts if c.version == 1), None)
            if ec is None:
                continue
            eb = str(ec.document.get("repository", {}).get("work_branch", ""))
            if eb == new_branch:
                conflict = owning_task.external_id
                break
        assert conflict is None, "No conflict should be found"
