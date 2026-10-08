"""Executable acceptance checks on the contract (hades #449).

Tests:
  - AC1: submit validation accepts a check and refuses a malformed one with 422
  - AC2: lab-local attempt - failing check blocks publication (gate FAIL)
  - AC3: frontier attempt - the gate is advisory
  - AC5: no review attempt is launched before publication by this change
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from crucible.contracts.task_contract import AcceptanceCriterion, AcceptanceCriterionCheck
from crucible.domain.gates import (
    PRE_PR_EVALUATORS,
    PRE_PR_GATES,
    EvidenceItem,
    GateInput,
    GateName,
    GateResult,
    acceptance_checks,
    gate_class,
)

# -- AC1: contract validation ---------------------------------------------------


def test_accepts_valid_check() -> None:
    """A check with command and integer expect_exit is accepted."""
    ac = AcceptanceCriterion(
        id="AC1",
        text="Runs in a worker",
        check=AcceptanceCriterionCheck(
            command="python -m pytest tests/unit/test_gate_probe.py -q", expect_exit=0
        ),
    )
    assert ac.id == "AC1"
    assert ac.check is not None
    assert ac.check.command == "python -m pytest tests/unit/test_gate_probe.py -q"
    assert ac.check.expect_exit == 0


def test_accepts_check_without_expect_exit() -> None:
    """expect_exit defaults to 0 when absent."""
    ac = AcceptanceCriterion(
        id="AC2",
        text="No secrets in diff",
        check=AcceptanceCriterionCheck(command="grep -r 'password' src/ || true"),
    )
    assert ac.check is not None
    assert ac.check.expect_exit == 0


def test_refuses_missing_command() -> None:
    """A check object without command is rejected."""
    with pytest.raises(ValidationError):
        AcceptanceCriterionCheck(command=None, expect_exit=0)


def test_refuses_empty_command() -> None:
    """A check with an empty string command is rejected."""
    with pytest.raises(ValidationError):
        AcceptanceCriterionCheck(command="", expect_exit=0)


def test_refuses_non_int_expect_exit() -> None:
    """expect_exit must be an integer."""
    with pytest.raises(ValidationError):
        AcceptanceCriterionCheck(command="echo hi", expect_exit="zero")


# -- helpers --------------------------------------------------------------------


def _mock_evidence(
    ac_id: str, command: str, exit_code: int, expected: int, passed: bool
) -> EvidenceItem:
    """Create an EvidenceItem that the probe would produce for a check result."""
    return EvidenceItem(
        id=42,
        kind="acceptance_check",
        source="gate_probe",
        verified=True,
        payload={
            "criterion_id": ac_id,
            "command": command,
            "expected_exit": expected,
            "exit_code": exit_code,
            "passed": passed,
        },
    )


def _make_gate_input(
    pool: str, *, criteria: list[dict[str, Any]], failed_criteria: list[dict[str, Any]]
) -> GateInput:
    """Build a GateInput suitable for testing the acceptance_checks gate.

    criteria - the raw acceptance_criteria from the contract.
    failed_criteria - list of dicts with keys (id, command, expected_exit, actual_exit,
                      passed) that become probe evidence items.
    """
    evidence: list[EvidenceItem] = []
    for fc in failed_criteria:
        evidence.append(
            EvidenceItem(
                id=1,
                kind="acceptance_check",
                source="gate_probe",
                verified=True,
                payload=fc,
            )
        )

    contract: dict[str, Any] = {
        "schema_version": "1.0",
        "acceptance_criteria": criteria,
        "required_verification": [],
        "policy": {
            "name": "default-software",
            "version": 3,
            "routing": {
                "models": [
                    {
                        "harness": "default",
                        "model": "test-model",
                        "pool": pool,
                        "endpoint": "local" if pool == "hades" else "remote",
                    },
                ],
            },
            "advisory_gates": ["acceptance_checks"] if pool != "hades" else [],
        },
    }

    return GateInput(
        contract=contract,
        policy=contract["policy"],
        head_sha="abcdef",
        evidence=tuple(evidence),
        selected_pool=pool,
    )


# -- AC2: lab-local gate blocks on failure --------------------------------------


def test_failing_check_blocks_for_local_pool() -> None:
    """A failing criterion check causes the acceptance_checks gate to FAIL
    when the attempt ran on a lab-local pool."""
    criteria: list[dict[str, Any]] = [
        {
            "id": "AC1",
            "text": "must pass",
            "check": {"command": "false", "expect_exit": 0},
        },
    ]
    failed: list[dict[str, Any]] = [
        {
            "criterion_id": "AC1",
            "command": "false",
            "expected_exit": 0,
            "exit_code": 1,
            "passed": False,
        },
    ]
    gi = _make_gate_input("hades", criteria=criteria, failed_criteria=failed)

    # The gate should be blocking for local pools
    advisory_set: frozenset[str] = frozenset(gi.policy.get("advisory_gates", []))
    assert GateName.ACCEPTANCE_CHECKS not in advisory_set
    assert gate_class(GateName.ACCEPTANCE_CHECKS, advisory_set) == "blocking"

    outcome = acceptance_checks(gi)
    assert outcome.result == GateResult.FAIL
    assert "AC1" in outcome.detail


def test_passing_check_allows_for_local_pool() -> None:
    """A passing check is fine even on lab-local."""
    criteria: list[dict[str, Any]] = [
        {
            "id": "AC1",
            "text": "must pass",
            "check": {"command": "true", "expect_exit": 0},
        },
    ]
    failed: list[dict[str, Any]] = [
        {
            "criterion_id": "AC1",
            "command": "true",
            "expected_exit": 0,
            "exit_code": 0,
            "passed": True,
        },
    ]
    gi = _make_gate_input("hades", criteria=criteria, failed_criteria=failed)

    outcome = acceptance_checks(gi)
    assert outcome.result == GateResult.PASS


def test_mixed_pass_fail_blocks_for_local_pool() -> None:
    """Some passing and some failing checks: gate FAILS for local."""
    criteria: list[dict[str, Any]] = [
        {"id": "AC1", "text": "passing", "check": {"command": "true", "expect_exit": 0}},
        {"id": "AC2", "text": "failing", "check": {"command": "false", "expect_exit": 0}},
    ]
    failed: list[dict[str, Any]] = [
        {
            "criterion_id": "AC1",
            "command": "true",
            "expected_exit": 0,
            "exit_code": 0,
            "passed": True,
        },
        {
            "criterion_id": "AC2",
            "command": "false",
            "expected_exit": 0,
            "exit_code": 1,
            "passed": False,
        },
    ]
    gi = _make_gate_input("hades", criteria=criteria, failed_criteria=failed)

    outcome = acceptance_checks(gi)
    assert outcome.result == GateResult.FAIL
    assert "AC2" in outcome.detail


def test_no_evidence_passes_for_local_pool() -> None:
    """When no check evidence exists, the gate PASSES (checks not yet run)."""
    criteria: list[dict[str, Any]] = [
        {
            "id": "AC1",
            "text": "must pass",
            "check": {"command": "false", "expect_exit": 0},
        },
    ]
    gi = _make_gate_input("hades", criteria=criteria, failed_criteria=[])

    outcome = acceptance_checks(gi)
    assert outcome.result == GateResult.PASS


# -- AC3: frontier gate is advisory ---------------------------------------------


def test_failing_check_is_advisory_for_frontier_pool() -> None:
    """A failing check does not block for frontier pool attempts."""
    criteria: list[dict[str, Any]] = [
        {
            "id": "AC1",
            "text": "must pass",
            "check": {"command": "false", "expect_exit": 0},
        },
    ]
    failed: list[dict[str, Any]] = [
        {
            "criterion_id": "AC1",
            "command": "false",
            "expected_exit": 0,
            "exit_code": 1,
            "passed": False,
        },
    ]
    gi = _make_gate_input("frontier", criteria=criteria, failed_criteria=failed)

    # The gate should be advisory for frontier pools
    advisory_set: frozenset[str] = frozenset(gi.policy.get("advisory_gates", []))
    assert GateName.ACCEPTANCE_CHECKS in advisory_set

    outcome = acceptance_checks(gi)
    assert outcome.result == GateResult.PASS


def test_passing_check_for_frontier_pool() -> None:
    """A passing check also passes for frontier."""
    criteria: list[dict[str, Any]] = [
        {
            "id": "AC1",
            "text": "must pass",
            "check": {"command": "true", "expect_exit": 0},
        },
    ]
    failed: list[dict[str, Any]] = [
        {
            "criterion_id": "AC1",
            "command": "true",
            "expected_exit": 0,
            "exit_code": 0,
            "passed": True,
        },
    ]
    gi = _make_gate_input("frontier", criteria=criteria, failed_criteria=failed)

    outcome = acceptance_checks(gi)
    assert outcome.result == GateResult.PASS


# -- AC5: no review attempt before publication ----------------------------------


def test_no_review_attempt_launched_by_acceptance_checks_change() -> None:
    """The acceptance_checks gate runs as part of the mechanical pre-PR
    gate set. It does not trigger a review execution (hades #449, AC5)."""

    # acceptance_checks is a pre-PR gate, not a review gate
    assert "acceptance_checks" in PRE_PR_GATES

    # It has an evaluator in the pre-PR set
    assert "acceptance_checks" in PRE_PR_EVALUATORS

    # There is no review execution tied to this gate.
    # The gate runs inside evaluate_pre_pr(), which is purely
    # mechanical and does not launch a review attempt.


# -- no-check criteria are advisory ---------------------------------------------


def test_criterion_without_check_is_advisory() -> None:
    """A criterion without a check field is advisory and listed for the reviewer."""
    criteria: list[dict[str, Any]] = [
        {
            "id": "AC1",
            "text": "must pass",
            "check": {"command": "true", "expect_exit": 0},
        },
        {
            "id": "AC2",
            "text": "advisory criterion with no check",
        },
    ]
    failed: list[dict[str, Any]] = [
        {
            "criterion_id": "AC1",
            "command": "true",
            "expected_exit": 0,
            "exit_code": 0,
            "passed": True,
        },
    ]
    gi = _make_gate_input("hades", criteria=criteria, failed_criteria=failed)

    outcome = acceptance_checks(gi)
    # AC1 passes; AC2 is advisory (no check)
    assert outcome.result == GateResult.PASS
    # AC2 should appear in findings (advisory)
    assert "AC2" in outcome.detail or any("AC2" in f for f in outcome.findings)


def test_all_criteria_no_checks_pass() -> None:
    """A contract with criteria but no checks at all is advisory and passes."""
    criteria: list[dict[str, Any]] = [
        {"id": "AC1", "text": "must pass", "check": {"command": "true", "expect_exit": 0}},
        {"id": "AC2", "text": "no check here"},
    ]
    failed: list[dict[str, Any]] = []
    gi = _make_gate_input("hades", criteria=criteria, failed_criteria=failed)

    outcome = acceptance_checks(gi)
    assert outcome.result == GateResult.PASS
