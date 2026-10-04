"""Hades #402: the worker self-review is the internal review and acceptance."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crucible.application.gates import evaluate_and_advance
from crucible.contracts.completion_claim import parse_claim
from crucible.domain.entities import ExecutionRole
from crucible.domain.gates import EvidenceItem, GateInput, GateResult, report_present
from crucible.domain.lifecycle import TaskState
from tests.unit.test_routing import _routing_setup


def _self_review() -> dict[str, object]:
    return {
        "documentation": ["docs/spec/11-definition-of-done.md"],
        "acceptance_criteria": [
            {"id": "AC1", "status": "met", "evidence": "the publication test"},
            {"id": "AC2", "status": "met", "evidence": "the missing-section test"},
        ],
        "omissions": [],
    }


def _report() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "summary": "Use the worker review to publish.",
        "self_review": _self_review(),
        "acceptance_mapping": _self_review()["acceptance_criteria"],
        "proposed_pull_request": {"title": "Publish reviewed worker reports", "body": "Done."},
        "limitations": [],
        "risks": [],
        "blockers": [],
        "follow_ups": [],
    }


@pytest.mark.parametrize("role", [ExecutionRole.IMPLEMENT, ExecutionRole.CORRECT])
def test_passing_attempt_records_acceptance_and_starts_publication_without_a_call(
    monkeypatch: pytest.MonkeyPatch, role: ExecutionRole
) -> None:
    supervisor, pending, uow = _routing_setup(monkeypatch, all_busy=False)
    pending.task.state = TaskState.REPORTED
    pending.task.head_sha = "a" * 40
    pending.execution.role = role
    pending.execution.policy_snapshot = {"gates": {"pre_pr": ["report_present"]}}
    if role is ExecutionRole.CORRECT:
        pending.contract["correction"] = {"reason": "pre_pr_gates"}
    uow.contracts.get.return_value = SimpleNamespace(document=pending.contract)
    uow.evidence.list_for_attempt.return_value = [
        SimpleNamespace(
            id=1,
            attempt_id=pending.attempt.id,
            kind="artifact_present",
            source="crucible",
            verified=True,
            payload={"role": "completion_claim", "parsed_ok": True, "parse_errors": []},
            artifact_id=None,
        ),
        SimpleNamespace(
            id=2,
            attempt_id=pending.attempt.id,
            kind="scanner_result",
            source="crucible",
            verified=True,
            payload={"findings": [], "diff_scanned": True, "scanned": ["diff", "report"]},
            artifact_id=None,
        ),
    ]
    uow.evidence.list_for_task.return_value = []
    uow.gate_results.list_for_attempt.return_value = []

    evaluate_and_advance(
        uow,
        supervisor._clock,
        task=pending.task,
        attempt=pending.attempt,
        execution=pending.execution,
    )

    assert pending.task.state is TaskState.PUBLISHING
    acceptance = uow.acceptance.add.call_args.args[0]
    assert acceptance.head_sha == pending.task.head_sha
    assert acceptance.reasoning.startswith("Every blocking pre-PR gate passed")
    uow.wakes.add.assert_not_called()


def test_missing_self_review_fails_report_present_and_names_the_section() -> None:
    document = _report()
    del document["self_review"]
    claim, errors = parse_claim(document)
    assert claim is None
    evidence = EvidenceItem(
        id=1,
        kind="artifact_present",
        source="crucible",
        verified=True,
        payload={
            "role": "completion_claim",
            "parsed_ok": False,
            "parse_errors": errors,
        },
    )
    outcome = report_present(GateInput(contract={}, policy={}, head_sha=None, evidence=(evidence,)))
    assert outcome.result is GateResult.FAIL
    assert outcome.always_blocks
    assert "self_review" in outcome.detail
