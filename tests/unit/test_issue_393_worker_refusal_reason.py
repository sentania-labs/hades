"""hades #393: a worker that cannot do the task as written stops and raises its hand.

`blocked.md` opens with a reason line, `missing_capability` or `ambiguous_contract`, and
the rest is the worker's statement. Crucible parses the reason onto the attempt record and
the escalation, keeps the statement verbatim, does not retry the attempt and marks no
pool; the identity tells the worker to stop rather than guess on an ambiguous contract;
a correction after a blocked attempt runs to an ordinary claim."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.execution.identity import (
    AMBIGUOUS_CONTRACT_STOP_RULE,
    render_identity_md,
)
from crucible.application.corrections import attach_correction
from crucible.application.supervisor import (
    BLOCKED_WITHOUT_STATEMENT,
    Supervisor,
    blocked_note,
)
from crucible.contracts.completion_claim import BlockedNote, parse_blocked_md
from crucible.domain.entities import (
    Attempt,
    Decision,
    Escalation,
    EscalationState,
    Execution,
    ExecutionRole,
    Principal,
    Role,
    Task,
    TaskContract,
)
from crucible.domain.events import EventKind
from crucible.domain.exit_class import (
    BLOCKED_REASON_AMBIGUOUS_CONTRACT,
    BLOCKED_REASON_MISSING_CAPABILITY,
    BLOCKED_REASONS,
    ExitClass,
    classify_exit,
)
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from tests.fixtures import FakeClock, contract_document

# The worker's statement, in its own words, as AC1 has it: `rg` is not in the image.
RG_STATEMENT = (
    "# Blocked\n"
    "\n"
    "`make lint` runs `rg` and the worker image has no `rg`: `which rg` finds nothing and\n"
    "`uv run rg --version` fails the same way. I did not write a substitute for it.\n"
    "\n"
    "Tried: `which rg`, `uv run rg --version`, reading the Makefile for another path.\n"
)
RG_BLOCKED_MD = "reason: missing_capability\n\n" + RG_STATEMENT

POLICY = {
    "limits": {"timeout_seconds": 3600, "grace_seconds": 30, "stall_fail_seconds": 600},
    "git": {"work_branch_pattern": "crucible/*", "commit_trailer": "Crucible-Attempt"},
    "gates": {},
}


# ----- the reason line ------------------------------------------------------------------


def test_the_two_reasons_are_the_ones_the_issue_names() -> None:
    assert {"missing_capability", "ambiguous_contract"} == BLOCKED_REASONS
    assert BLOCKED_REASON_MISSING_CAPABILITY in BLOCKED_REASONS
    assert BLOCKED_REASON_AMBIGUOUS_CONTRACT in BLOCKED_REASONS


def test_the_reason_line_is_parsed_and_the_statement_is_kept_verbatim() -> None:
    note = parse_blocked_md(RG_BLOCKED_MD)
    assert note == BlockedNote(reason="missing_capability", statement=RG_STATEMENT.strip("\n"))
    # Nothing of the worker's words is changed: the heading, the code marks, the blank
    # lines inside the statement and the "Tried:" line are all still there.
    assert "`which rg`, `uv run rg --version`" in note.statement
    assert note.statement.count("\n\n") == 2


@pytest.mark.parametrize(
    "line",
    [
        "reason: ambiguous_contract",
        "Reason: Ambiguous_Contract",
        "- reason: ambiguous_contract",
        "**reason**: `ambiguous_contract`",
        "  reason:ambiguous_contract  ",
    ],
)
def test_the_reason_line_is_read_in_the_forms_a_model_writes(line: str) -> None:
    text = f"# Blocked\n\n{line}\n\nWhich of the two readings of AC2 is meant?\n"
    note = parse_blocked_md(text)
    assert note.reason == "ambiguous_contract"
    assert note.statement == "# Blocked\n\nWhich of the two readings of AC2 is meant?"


def test_a_reason_crucible_does_not_know_stays_in_the_statement() -> None:
    note = parse_blocked_md("reason: tired\n\nI would rather not.\n")
    assert note.reason is None
    assert note.statement == "reason: tired\n\nI would rather not."


def test_a_file_without_a_reason_line_is_the_statement_alone() -> None:
    note = parse_blocked_md("# Blocked\n\nWhich database?\n")
    assert note == BlockedNote(reason=None, statement="# Blocked\n\nWhich database?")


def test_only_the_first_reason_line_is_the_reason() -> None:
    note = parse_blocked_md(
        "reason: missing_capability\nreason: ambiguous_contract\nno rg, and AC2 is unclear\n"
    )
    assert note.reason == "missing_capability"
    assert note.statement == "reason: ambiguous_contract\nno rg, and AC2 is unclear"


def test_the_supervisor_reads_the_note_and_redacts_a_secret_but_keeps_the_reason() -> None:
    assert blocked_note(None) == (None, None)
    assert blocked_note(RG_BLOCKED_MD) == ("missing_capability", RG_STATEMENT.strip("\n"))
    assert blocked_note("reason: missing_capability\n") == ("missing_capability", "")
    leaked = "reason: ambiguous_contract\n\nthe token is ghp_" + "A" * 36 + "\n"
    assert blocked_note(leaked) == ("ambiguous_contract", "[redacted: secret pattern]")


def test_blocked_md_on_a_clean_exit_is_blocked_with_either_reason_and_any_code() -> None:
    for code in (0, 75):
        assert (
            classify_exit(exit_code=code, report_present=False, blocked_present=True)
            is ExitClass.BLOCKED
        )


# ----- the blocked attempt: one escalation, no retry, no pool ---------------------------


def _task(state: TaskState, clock: FakeClock, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "id": "01TASK393",
        "external_id": "FDY-0334",
        "principal_id": "01PRINC393",
        "project": "hades",
        "title": "Workers stop and raise their hand",
        "state": state,
        "contract_version": 1,
        "policy_name": "hades-self-hosting",
        "policy_version": 23,
        "repository_id": "01REPO393",
        "created_at": clock.now(),
        "updated_at": clock.now(),
    }
    fields.update(overrides)
    return Task(**fields)


def _execution(task: Task, clock: FakeClock, **overrides: Any) -> Execution:
    fields: dict[str, Any] = {
        "id": "01EXEC393",
        "task_id": task.id,
        "role": ExecutionRole.IMPLEMENT,
        "contract_version": task.contract_version,
        "harness": "claude",
        "model": "model",
        "effort": None,
        "provider": "fake",
        "image": "crucible-worker:fake-blocked",
        "policy_snapshot": {},
        "state": ExecutionState.ACTIVE,
        "max_attempts": 2,
        "retry_on": ["environment", "lost"],
        "timeout_seconds": 3600,
        "created_at": clock.now(),
    }
    fields.update(overrides)
    return Execution(**fields)


def _attempt(
    execution: Execution, clock: FakeClock, *, number: int, exit_class: ExitClass, **over: Any
) -> Attempt:
    fields: dict[str, Any] = {
        "id": f"01ATT393{number}",
        "execution_id": execution.id,
        "task_id": execution.task_id,
        "number": number,
        "state": AttemptState.COLLECTED,
        "created_at": clock.now(),
        "exit_code": 75 if exit_class is ExitClass.BLOCKED else 0,
        "exit_class": exit_class,
        "selected_pool": "pool-a",
        "selected_model": "model",
        "selected_harness": "claude",
    }
    fields.update(over)
    return Attempt(**fields)


class _Recorder:
    """The parts of a unit of work the finish step writes to, kept for the assertions."""

    def __init__(self, task: Task, execution: Execution, attempts: list[Attempt]) -> None:
        self.events: list[Any] = []
        self.escalations: list[Escalation] = []
        self.new_attempts: list[Attempt] = []
        self.uow = MagicMock()
        self.uow.tasks.get.return_value = task
        self.uow.executions.get.return_value = execution
        self.uow.attempts.list_for_execution.return_value = attempts
        self.uow.attempts.add.side_effect = self.new_attempts.append
        self.uow.leases.list_checkout_leases.return_value = []
        self.uow.events.append.side_effect = self._record
        self.uow.escalations.add.side_effect = self.escalations.append
        self.uow.contracts.get.return_value = None

    def _record(self, event: Any) -> Any:
        self.events.append(event)
        return event

    def payloads(self, kind: EventKind) -> list[dict[str, Any]]:
        return [e.payload for e in self.events if e.kind == kind.value]


def _supervisor(clock: FakeClock) -> Supervisor:
    return Supervisor(
        MagicMock(), {"fake": FakeProvider()}, clock, holder="test", artifact_store=MagicMock()
    )


def _finish_blocked(
    blocked_md: str = RG_BLOCKED_MD,
) -> tuple[Task, Execution, Attempt, _Recorder, MagicMock]:
    clock = FakeClock()
    task = _task(TaskState.RUNNING, clock)
    execution = _execution(task, clock)
    attempt = _attempt(execution, clock, number=1, exit_class=ExitClass.BLOCKED)
    recorder = _Recorder(task, execution, [attempt])
    supervisor = _supervisor(clock)
    reason, statement = blocked_note(blocked_md)
    with (
        patch("crucible.application.supervisor.load_attempt_routing", return_value=None),
        patch.object(Supervisor, "_mark_pool_exhausted") as mark_pool,
    ):
        supervisor._classify_and_finish(
            recorder.uow, attempt, statement, blocked_reason=reason, claim_ok=False
        )
    return task, execution, attempt, recorder, mark_pool


def test_a_missing_capability_opens_one_escalation_with_the_reason_and_the_words() -> None:
    """AC1: blocked.md with reason missing_capability naming rg yields one escalation
    carrying the reason and the statement verbatim."""
    task, _, attempt, recorder, _ = _finish_blocked()
    assert attempt.state is AttemptState.BLOCKED
    assert task.state is TaskState.BLOCKED
    assert len(recorder.escalations) == 1
    (escalation,) = recorder.escalations
    assert escalation.reason == "missing_capability"
    assert escalation.question == RG_STATEMENT.strip("\n")
    assert "`rg`" in escalation.question
    assert escalation.attempt_id == attempt.id and escalation.state is EscalationState.OPEN
    # The attempt record holds the same two facts.
    assert attempt.blocked_reason == "missing_capability"
    assert attempt.blocked_statement == RG_STATEMENT.strip("\n")
    # And so do the events along the way.
    (blocked_attempt,) = recorder.payloads(EventKind.ATTEMPT_BLOCKED)
    assert blocked_attempt["blocked_reason"] == "missing_capability"
    (blocked_task,) = recorder.payloads(EventKind.TASK_BLOCKED)
    assert blocked_task["blocked_reason"] == "missing_capability"
    assert blocked_task["blocked_md"] == RG_STATEMENT.strip("\n")
    (opened,) = recorder.payloads(EventKind.ESCALATION_OPENED)
    assert opened["reason"] == "missing_capability"
    assert opened["question"] == RG_STATEMENT.strip("\n")
    assert opened["escalation_id"] == escalation.id
    # The wake names the reason, so the person woken knows what kind of answer is wanted.
    wake = recorder.uow.wakes.add.call_args.args[0]
    assert wake.reason == "blocked"
    assert "missing_capability" in wake.payload["summary"]
    assert escalation.id in wake.payload["summary"]


def test_a_blocked_attempt_is_not_retried_and_marks_no_pool() -> None:
    """AC1: the attempt is not retried. The execution stays open for the answer, no
    further attempt is created, and pool accounting is never reached."""
    task, execution, attempt, recorder, mark_pool = _finish_blocked()
    assert recorder.new_attempts == []
    assert execution.state is ExecutionState.ACTIVE
    assert task.state is TaskState.BLOCKED
    assert recorder.payloads(EventKind.TASK_RETRY_SCHEDULED) == []
    assert recorder.payloads(EventKind.EXECUTION_FAILED) == []
    mark_pool.assert_not_called()
    assert attempt.routing_excluded_pools == []


def test_a_blocked_attempt_consumes_no_retry_of_its_execution() -> None:
    """A decision schedules the same execution again (09). When that attempt fails with
    a retryable class, the blocked attempt before it has used none of max_attempts."""
    clock = FakeClock()
    task = _task(TaskState.RUNNING, clock)
    execution = _execution(task, clock)  # max_attempts 2, retry_on environment
    blocked = _attempt(
        execution,
        clock,
        number=1,
        exit_class=ExitClass.BLOCKED,
        state=AttemptState.BLOCKED,
        blocked_reason="ambiguous_contract",
    )
    failed = _attempt(execution, clock, number=2, exit_class=ExitClass.ENVIRONMENT, exit_code=70)
    recorder = _Recorder(task, execution, [blocked, failed])
    with patch("crucible.application.supervisor.load_attempt_routing", return_value=None):
        _supervisor(clock)._classify_and_finish(recorder.uow, failed, None)
    assert failed.state is AttemptState.FAILED
    assert task.state is TaskState.SCHEDULED
    assert [a.number for a in recorder.new_attempts] == [3]
    (scheduled,) = recorder.payloads(EventKind.TASK_RETRY_SCHEDULED)
    assert scheduled["max_attempts"] == 2


def test_a_blocked_md_with_a_reason_and_no_words_still_opens_the_escalation() -> None:
    _, _, attempt, recorder, _ = _finish_blocked("reason: ambiguous_contract\n")
    (escalation,) = recorder.escalations
    assert escalation.reason == "ambiguous_contract"
    assert escalation.question == BLOCKED_WITHOUT_STATEMENT
    assert attempt.blocked_reason == "ambiguous_contract"
    assert attempt.blocked_statement == ""


def test_a_blocked_md_without_a_reason_line_still_blocks_as_before() -> None:
    _, _, attempt, recorder, _ = _finish_blocked("# Blocked\n\nWhich database?\n")
    (escalation,) = recorder.escalations
    assert escalation.reason is None
    assert escalation.question == "# Blocked\n\nWhich database?"
    assert attempt.blocked_reason is None
    (opened,) = recorder.payloads(EventKind.ESCALATION_OPENED)
    assert "reason" not in opened
    wake = recorder.uow.wakes.add.call_args.args[0]
    assert wake.payload["summary"] == f"the worker blocked and opened escalation {escalation.id}"


async def test_the_fake_blocked_worker_writes_a_reason_line() -> None:
    """The fake provider's blocked scenario carries a reason line, so the compose and
    kind tiers exercise the parse end to end."""
    from crucible.ports.execution import LaunchSpec  # noqa: PLC0415

    provider = FakeProvider()
    spec = LaunchSpec(
        attempt_id="attempt-FDY-0334",
        task_id="t",
        external_id="FDY-0334",
        role="implement",
        harness="codex",
        model="m",
        image="crucible-worker:fake-blocked",
        timeout_seconds=60,
        contract=contract_document(),
    )
    workspace = await provider.prepare(spec)
    handle = await provider.launch(workspace, spec)
    observation = await provider.observe(handle)
    assert observation.exit_code == 75
    outputs = await provider.collect(handle, workspace)
    assert outputs.blocked_md is not None
    note = parse_blocked_md(outputs.blocked_md)
    assert note.reason == "ambiguous_contract"
    assert "needs a decision" in note.statement


# ----- the identity --------------------------------------------------------------------


def _identity(contract: dict[str, Any] | None = None) -> str:
    return render_identity_md(
        contract=contract or contract_document(),
        policy=POLICY,
        external_id="FDY-0334",
        owner="foundry",
        work_branch="crucible/FDY-0334",
        network_mode="policy",
    )


def test_the_identity_contains_the_ambiguous_contract_stop_rule() -> None:
    """AC2: the rendered identity contains the ambiguous-contract stop rule, in the
    operator's words, in the section that says how to stop."""
    text = _identity()
    stuck = text[text.index("## If you are stuck") :]
    assert AMBIGUOUS_CONTRACT_STOP_RULE in stuck
    assert "a workaround that changes the result is not a workaround, it is a wrong answer" in stuck
    assert "stop rather than pick a reading" in stuck
    assert "not retried" in stuck


def test_the_identity_names_both_reasons_and_where_each_belongs() -> None:
    text = _identity()
    checks = text[text.index("## Checks") : text.index("## Report")]
    stuck = text[text.index("## If you are stuck") :]
    # A missing program is still blocked.md naming it (hades #183), now with its reason.
    assert "do not write a substitute for it" in checks
    assert "report/blocked.md naming the program" in checks
    assert "`reason: missing_capability`" in checks
    assert "`reason: missing_capability`" in stuck
    assert "`reason: ambiguous_contract`" in stuck
    assert "in your own words" in stuck
    # The contract's own conditions still follow the rule.
    assert "- a required verification command does not exist" in stuck
    assert chr(0x2014) not in text  # no em dash anywhere in what the worker is told


def test_the_stop_rule_is_rendered_once_and_after_the_report_section() -> None:
    text = _identity()
    assert text.count(AMBIGUOUS_CONTRACT_STOP_RULE) == 1
    assert text.index("## Report") < text.index(AMBIGUOUS_CONTRACT_STOP_RULE)


# ----- a correction after a blocked attempt ----------------------------------------------


class _EscalationRepo:
    def __init__(self, escalations: list[Escalation]) -> None:
        self.rows = list(escalations)
        self.saved: list[Escalation] = []

    def list_for_task(self, task_id: str) -> list[Escalation]:
        return [e for e in self.rows if e.task_id == task_id]

    def save(self, escalation: Escalation) -> None:
        self.saved.append(escalation)


def _correction_document() -> dict[str, Any]:
    document = contract_document(external_id="FDY-0334")
    document["correction"] = {
        "of_version": 1,
        "reason": "pre_pr_gates",
        "addresses": [{"kind": "internal_review", "id": "1", "disposition_id": None}],
        "instructions": "rg is in the image now; run the checks again.",
        "resume_from": "remote_branch",
        "request_internal_review": False,
    }
    return document


def _attach_correction(task: Task, escalation: Escalation, clock: FakeClock) -> list[Decision]:
    """attach_correction on a blocked task, with validation patched away as
    test_issue_289_blocked_correction does, so only the lifecycle path runs."""
    from crucible.contracts.task_contract import TaskContractV1  # noqa: PLC0415

    document = _correction_document()
    repo = _EscalationRepo([escalation])
    decisions: list[Decision] = []
    uow = MagicMock()
    uow.tasks.get.return_value = task
    uow.contracts.get.return_value = TaskContract(
        id="01CONTRACT393",
        task_id=task.id,
        version=task.contract_version,
        document=document,
        sha256="abc123",
        submitted_at=datetime(2026, 10, 5, tzinfo=UTC),
    )
    uow.escalations.list_for_task = repo.list_for_task
    uow.escalations.save = repo.save
    uow.events.append = MagicMock(side_effect=lambda event: event)
    uow.acceptance.list_for_task.return_value = []
    uow.retention.list_recent.return_value = []
    uow.decisions.add = MagicMock(side_effect=decisions.append)
    principal = Principal(
        id=task.principal_id, name="foundry", role=Role.OPERATOR, created_at=clock.now()
    )
    with (
        patch(
            "crucible.application.corrections.parse_contract",
            side_effect=lambda _: TaskContractV1.model_validate(document),
        ),
        patch("crucible.application.corrections.require_task_principal"),
        patch("crucible.application.corrections.require_operator_for_pin"),
        patch("crucible.application.corrections.eligible_harness_names", return_value=set()),
        patch("crucible.application.corrections.validate_against_registry", return_value=[]),
        patch("crucible.application.corrections.unwired_provider_problems", return_value=[]),
        patch("crucible.application.corrections.correction_narrows", return_value=[]),
        patch("crucible.application.corrections._unpublished_bundle_problem", return_value=None),
        patch(
            "crucible.application.corrections.move_task",
            side_effect=lambda *args, **kwargs: setattr(args[2], "state", args[3]),
        ),
    ):
        result = attach_correction(uow, clock, principal=principal, task_id=task.id, body=document)
    assert result.state is TaskState.SCHEDULED
    assert repo.saved and repo.saved[-1].state is EscalationState.CLOSED
    return decisions


def test_a_correction_after_a_blocked_attempt_runs_to_a_normal_claim() -> None:
    """AC3: the blocked attempt opens the escalation; the correction answers it with the
    worker's statement as what it resolves; the corrected execution's attempt completes
    with a parsed report and takes the ordinary reported path, opening nothing."""
    task, _, blocked, recorder, _ = _finish_blocked()
    (escalation,) = recorder.escalations
    clock = FakeClock()

    decisions = _attach_correction(task, escalation, clock)
    (decision,) = decisions
    assert decision.kind == "correction"
    assert decision.escalation_id == escalation.id
    assert decision.resolves == RG_STATEMENT.strip("\n")
    assert escalation.reason == "missing_capability"
    assert escalation.decision_id == decision.id
    assert task.state is TaskState.SCHEDULED

    # The correction is a new contract version, so the supervisor materialises a fresh
    # execution for it (09); its attempt ends with exit 0 and a report that parsed.
    task.contract_version = 2
    task.state = TaskState.RUNNING
    corrected = _execution(
        task, clock, id="01EXEC393C", role=ExecutionRole.CORRECT, contract_version=2
    )
    claim = _attempt(
        corrected, clock, number=1, exit_class=ExitClass.COMPLETED, exit_code=0, id="01ATT393C"
    )
    after = _Recorder(task, corrected, [claim])
    with (
        patch("crucible.application.supervisor.load_attempt_routing", return_value=None),
        patch.object(Supervisor, "_task_reported") as reported,
    ):
        _supervisor(clock)._classify_and_finish(after.uow, claim, None, claim_ok=True)
    assert claim.state is AttemptState.SUCCEEDED
    assert corrected.state is ExecutionState.SUCCEEDED
    reported.assert_called_once()
    assert reported.call_args.args[3] is ExitClass.COMPLETED
    assert after.escalations == []
    assert after.payloads(EventKind.TASK_BLOCKED) == []
    assert claim.blocked_reason is None and claim.blocked_statement is None
    # The blocked attempt before it still says why it stopped.
    assert blocked.blocked_reason == "missing_capability"
