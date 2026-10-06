"""A correction on a probe-blocked task is accepted and launches (hades #412)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crucible.adapters.execution.fake import FakeProvider
from crucible.application.corrections import (
    PREVIOUS_BUNDLE_GONE,
    _unpublished_bundle_problem,
)
from crucible.domain.entities import (
    Attempt,
    Execution,
    ExecutionRole,
    Principal,
    Role,
    TaskContract,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import (
    AttemptState,
    ExecutionState,
    TaskState,
)
from tests.fixtures import FakeClock, contract_document
from tests.unit.test_routing import _routing_setup


def _contract_v1(doc: dict[str, Any]) -> Any:
    from crucible.contracts.task_contract import TaskContractV1  # noqa: PLC0415

    return TaskContractV1.model_validate(doc)


def _make_stored_contract(task: Any, contract: dict[str, Any]) -> TaskContract:
    return TaskContract(
        id="01CONTRACT1234567890AB",
        task_id=task.id,
        version=1,
        document=contract,
        sha256="abc123",
        submitted_at=NOW,
    )


NOW = datetime.now(UTC)


async def test_gate_probe_blocked_task_accepts_a_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task blocked with gate_proves_nothing is accepted for correction.

    The gate probe emits attempt_exited with never_started=True, so the
    _unpublished_bundle_problem check (#346) passes and a correction is
    accepted and moves the task to scheduled.
    """
    from crucible.ports.execution import LaunchSpec  # noqa: PLC0415

    supervisor, item, uow = _routing_setup(monkeypatch, all_busy=False)
    item.attempt.state = AttemptState.PREPARING
    item.task.state = TaskState.RUNNING
    item.contract["required_verification"] = [
        {"id": "V1", "command": "make lint"},
        {"id": "V4", "command": "test -f made-by-the-worker"},
    ]
    uow.executions.list_for_task.return_value = [item.execution]
    uow.attempts.list_for_execution.return_value = [item.attempt]
    monkeypatch.setattr(supervisor, "_settle_if_cancelled", lambda *_: False)
    monkeypatch.setattr(supervisor, "_cancel_check", lambda *_: AsyncMock(return_value=False))
    monkeypatch.setattr(supervisor, "_release_checkout_leases", MagicMock())
    supervisor._github = None
    provider = FakeProvider()

    spec = LaunchSpec(
        attempt_id=item.attempt.id,
        task_id=item.task.id,
        external_id=item.task.external_id,
        role="implement",
        harness="script-harness",
        model="test",
        image="fake:succeed",
        timeout_seconds=60,
        contract=item.contract,
    )

    # Probe blocks: all checks pass on the unchanged repo
    provider.gate_probe_exits = {"V4": 0}
    assert not await supervisor._probe_before_prepare(item, provider, spec)
    assert item.attempt.termination_reason == "gate_proves_nothing"
    assert item.task.state is TaskState.BLOCKED
    assert item.attempt.started_at is None
    assert "V4 passes on the unchanged repo" in uow.escalations.add.call_args.args[0].question
    # The probe blocked attempt has an attempt_exited event with never_started
    exited_events = [
        call.args[0]
        for call in uow.events.append.call_args_list
        if call.args[0].kind == EventKind.ATTEMPT_EXITED.value
    ]
    probe_exited = next(e for e in exited_events if e.attempt_id == item.attempt.id)
    assert probe_exited.payload.get("never_started") is True


async def test_correction_resumes_from_probe_blocked_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """A correction on a probe-blocked task passes _unpublished_bundle_problem.

    The gate probe attempt has an ATTEMPT_EXITED with never_started=True, so the
    #346 exemption lets it find the sealed bundle from the previous attempt.
    """
    from crucible.application.corrections import attach_correction  # noqa: PLC0415

    supervisor, item, uow = _routing_setup(monkeypatch, all_busy=False)
    item.task.state = TaskState.BLOCKED
    item.contract["required_verification"] = [
        {"id": "V1", "command": "make lint"},
        {"id": "V4", "command": "test -f made-by-the-worker"},
    ]
    uow.executions.list_for_task.return_value = [item.execution]
    uow.attempts.list_for_execution.return_value = [item.attempt]
    monkeypatch.setattr(supervisor, "_settle_if_cancelled", lambda *_: False)
    monkeypatch.setattr(supervisor, "_cancel_check", lambda *_: AsyncMock(return_value=False))
    monkeypatch.setattr(supervisor, "_release_checkout_leases", MagicMock())
    supervisor._github = None

    # Pretend the probe already emitted ATTEMPT_EXITED with never_started=True
    exited_event = SimpleNamespace(
        attempt_id=item.attempt.id,
        payload={"never_started": True, "exit_class": "blocked"},
    )
    uow.events.latest_for_task_kind.side_effect = lambda _task_id, kind: (
        exited_event if kind == EventKind.ATTEMPT_EXITED.value else None
    )

    contract_doc = contract_document()
    contract_doc["correction"] = {
        "of_version": 1,
        "reason": "internal_review",
        "instructions": "add a new test",
        "addresses": [],
        "request_internal_review": False,
        "resume_from": "last_attempt",
    }
    # Mock the stored contract for _previous()
    stored = _make_stored_contract(item.task, contract_doc)
    uow.contracts.get.return_value = stored
    uow.attempts.list_for_task.return_value = [item.attempt]
    uow.events.latest_for_task_kind.return_value = None

    principal = Principal(
        id="op-1", name="operator", role=Role.OPERATOR, created_at=NOW, disabled_at=None
    )

    with (
        patch(
            "crucible.application.corrections.parse_contract",
            side_effect=lambda x: _contract_v1(contract_doc),
        ),
        patch("crucible.application.corrections.require_task_principal"),
        patch("crucible.application.corrections.require_operator_for_pin"),
        patch("crucible.application.corrections.eligible_harness_names", return_value=set()),
        patch("crucible.application.corrections.validate_against_registry", return_value=[]),
        patch("crucible.application.corrections.correction_narrows", return_value=[]),
        patch(
            "crucible.application.corrections.move_task",
            side_effect=lambda *args, **kwargs: setattr(args[2], "state", args[3]),
        ),
    ):
        task = attach_correction(
            uow,
            FakeClock(),
            principal=principal,
            task_id=item.task.id,
            body=contract_doc,
        )

    # The correction is accepted and the task moves to scheduled
    assert task.state is TaskState.SCHEDULED


def test_probe_blocked_task_passes_unpublished_bundle_check() -> None:
    """A correction on a probe-blocked task passes _unpublished_bundle_problem."""
    blocked_attempt = Attempt(
        id="blocked-1",
        execution_id="exec-1",
        task_id="task-1",
        number=1,
        state=AttemptState.BLOCKED,
        created_at=NOW,
        workspace_path="docker:///var/run/crucible",
    )
    sealed_attempt = Attempt(
        id="sealed-1",
        execution_id="exec-1",
        task_id="task-1",
        number=0,
        state=AttemptState.SUCCEEDED,
        created_at=NOW,
        workspace_path="docker:///var/run/crucible-sealed",
    )
    execution = Execution(
        id="exec-1",
        task_id="task-1",
        role=ExecutionRole.IMPLEMENT,
        contract_version=1,
        harness="script-harness",
        model="test",
        effort=None,
        provider="docker",
        image="fake:succeed",
        policy_snapshot={},
        state=ExecutionState.ACTIVE,
        max_attempts=2,
        retry_on=[],
        timeout_seconds=60,
        created_at=NOW,
    )
    bundle_row = SimpleNamespace(
        kind="bundle_head",
        verified=True,
        payload={"bundle_verified": True, "bundle_sha256": "abc123"},
    )

    uow = MagicMock()
    uow.tasks.get.return_value = SimpleNamespace(
        id="task-1", state=TaskState.BLOCKED, head_sha="def456"
    )
    uow.executions.list_for_task.return_value = [execution]
    uow.attempts.list_for_execution.return_value = [blocked_attempt]
    uow.attempts.list_for_task.return_value = [sealed_attempt, blocked_attempt]
    uow.evidence.list_for_attempt.side_effect = lambda attempt_id: (
        [bundle_row] if attempt_id == "sealed-1" else []
    )
    uow.events.latest_for_task_kind.side_effect = lambda _task_id, kind: (
        SimpleNamespace(
            attempt_id="blocked-1",
            payload={"never_started": True, "exit_class": "blocked"},
        )
        if kind == EventKind.ATTEMPT_EXITED.value
        else None
    )
    uow.executions.get.return_value = execution
    uow.retention.list_recent.return_value = []

    task_ns = SimpleNamespace(id="task-1", state=TaskState.BLOCKED, head_sha="def456")
    result = _unpublished_bundle_problem(uow, task_ns, "docker")  # type: ignore[arg-type]
    assert result is None


def test_correction_on_probe_blocked_without_sealed_bundle_is_refused() -> None:
    """A correction whose last real attempt's bundle is still refused."""
    blocked_attempt = Attempt(
        id="blocked-1",
        execution_id="exec-1",
        task_id="task-1",
        number=1,
        state=AttemptState.BLOCKED,
        created_at=NOW,
        workspace_path="docker:///var/run/crucible",
    )
    execution = Execution(
        id="exec-1",
        task_id="task-1",
        role=ExecutionRole.IMPLEMENT,
        contract_version=1,
        harness="script-harness",
        model="test",
        effort=None,
        provider="docker",
        image="fake:succeed",
        policy_snapshot={},
        state=ExecutionState.ACTIVE,
        max_attempts=2,
        retry_on=[],
        timeout_seconds=60,
        created_at=NOW,
    )
    uow = MagicMock()
    uow.tasks.get.return_value = SimpleNamespace(
        id="task-1", state=TaskState.BLOCKED, head_sha="def456"
    )
    uow.executions.list_for_task.return_value = [execution]
    uow.attempts.list_for_execution.return_value = [blocked_attempt]
    uow.attempts.list_for_task.return_value = [blocked_attempt]
    uow.evidence.list_for_attempt.return_value = []
    uow.events.latest_for_task_kind.side_effect = lambda _task_id, kind: (
        SimpleNamespace(
            attempt_id="blocked-1",
            payload={"never_started": True, "exit_class": "blocked"},
        )
        if kind == EventKind.ATTEMPT_EXITED.value
        else None
    )
    uow.retention.list_recent.return_value = []

    task_ns = SimpleNamespace(id="task-1", state=TaskState.BLOCKED, head_sha="def456")
    result = _unpublished_bundle_problem(uow, task_ns, "docker")  # type: ignore[arg-type]
    assert result == {"path": "correction", "message": PREVIOUS_BUNDLE_GONE}
