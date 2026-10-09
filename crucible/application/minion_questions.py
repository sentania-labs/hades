"""A minion's question as a record, and its answer (hades #208 item 2).

A worker that stops on `blocked.md` asks a question: the words it wrote. Hades keeps the
question on the task (who asked, which attempt, when) beside the escalation the same
stop opened (09), and answers it through one call: the answer is recorded with who
answered and when, and the attempt is corrected with the answer as the correction's
instructions and resumed from its sealed bundle, which also closes the escalation with a
`correction` decision (corrections.attach_correction). The board's Answer action and a
card thread both go through `answer_question`, so they cannot disagree."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from crucible.application.corrections import (
    CORRECTABLE_STATES,
    attach_correction,
    correction_document,
)
from crucible.application.decisions import record_decision
from crucible.application.errors import ConflictError, ForbiddenError, NotFoundError
from crucible.application.harnesses import HarnessRegistry
from crucible.application.rooms import notify_card_question
from crucible.application.task_access import require_task_principal
from crucible.application.transitions import record_event, require_contract
from crucible.contracts.api import DecisionRequest
from crucible.domain.entities import (
    Attempt,
    Escalation,
    EscalationState,
    MinionQuestion,
    Principal,
    Role,
    Task,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.domain.time import local_text
from crucible.ports.clock import Clock
from crucible.ports.harness import CredentialSource, HarnessGate
from crucible.ports.repository import UnitOfWork

ANSWERING_ROLES = frozenset({Role.OPERATOR, Role.ADMIN, Role.ORCHESTRATOR})
# The correction reason a blocked task's answer carries (05): the stage the work stopped
# at has no reason of its own, so the answer is more work with the answer's words.
ANSWER_CORRECTION_REASON = "needs_more_work"
ACTION_CORRECTED = "corrected"
ACTION_RECORDED = "recorded"
RESUME_CHOICES = frozenset({"last_attempt", "remote_branch"})


def _repository(uow: UnitOfWork) -> Any:
    # A unit of work assembled before hades #208 item 2 (a test fake) has no questions.
    return getattr(uow, "minion_questions", None)


def ask_question(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    attempt: Attempt,
    question_text: str,
    escalation: Escalation | None = None,
) -> MinionQuestion | None:
    """The supervisor records the question a worker stopped on, verbatim, beside the
    escalation its stop opened. Returns None on a store without questions."""
    repository = _repository(uow)
    if repository is None:
        return None
    now = clock.now()
    question = MinionQuestion(
        id=new_id(),
        task_id=task.id,
        asked_by_attempt_id=attempt.id,
        question_text=question_text,
        asked_at=now,
        escalation_id=escalation.id if escalation else None,
    )
    repository.add(question)
    notify_card_question(uow, clock, task)
    record_event(
        uow,
        clock,
        EventKind.MINION_QUESTION_ASKED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        execution_id=attempt.execution_id,
        attempt_id=attempt.id,
        payload={
            "question_id": question.id,
            "escalation_id": question.escalation_id,
            "question_text": question_text,
            "local_time": local_text(now),
            "reason": question_text,
        },
    )
    return question


def list_questions(uow: UnitOfWork, task_id: str) -> list[MinionQuestion]:
    """Every question on the task, oldest first."""
    repository = _repository(uow)
    if repository is None:
        return []
    rows: Sequence[MinionQuestion] = repository.list_for_task(task_id)
    return sorted(rows, key=lambda row: (row.asked_at, row.id))


def open_question_for(
    uow: UnitOfWork, task_id: str, escalation: Escalation | None
) -> MinionQuestion | None:
    """The unanswered question the escalation belongs to, or the newest unanswered one."""
    unanswered = [q for q in list_questions(uow, task_id) if q.answered_at is None]
    if escalation is not None:
        for question in unanswered:
            if question.escalation_id == escalation.id:
                return question
    return unanswered[-1] if unanswered else None


def question_view(question: MinionQuestion) -> dict[str, Any]:
    """The question as the task view and the card show it, times in the operator's
    local Central time."""
    return {
        "id": question.id,
        "question_text": question.question_text,
        "asked_by_attempt_id": question.asked_by_attempt_id,
        "asked_at": local_text(question.asked_at),
        "escalation_id": question.escalation_id,
        "answered": question.answered_at is not None,
        "answered_by": question.answered_by_name,
        "answered_at": local_text(question.answered_at) if question.answered_at else None,
        "answer_text": question.answer_text,
        "answer_action": question.answer_action,
        "answer_contract_version": question.answer_contract_version,
    }


def answer_question(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    question_id: str,
    answer_text: str,
    resume_from: str = "last_attempt",
    harnesses: HarnessRegistry | None = None,
    harness_gates: dict[str, HarnessGate] | None = None,
    credential_sources: dict[str, CredentialSource] | None = None,
    secret_providers: Collection[str] = (),
    wired_providers: Collection[str] | None = None,
) -> tuple[Task, MinionQuestion]:
    """Answer the question and bring the answer back to the worker in one act.

    When the task can take a correction (every blocked task can), a correction version
    is attached with the answer as its instructions and resumed from `resume_from`
    (`last_attempt`, the sealed bundle, or `remote_branch`, the pushed branch); that
    schedules the task again (with `resume_from_work_branch` on the scheduling event when
    the branch was chosen, so the execution starts at its tip) and closes the question's
    own escalation with a `correction` decision.
    When it cannot (the task moved on, or was cancelled), the answer is recorded and an
    escalation still open is answered with an `escalation_answer` decision, so the words
    are kept either way. The question's `answer_action` says which happened."""
    if principal.role not in ANSWERING_ROLES:
        raise ForbiddenError("operator, admin or orchestrator role required to answer")
    if not answer_text.strip():
        raise ConflictError("an answer needs some words")
    if resume_from not in RESUME_CHOICES:
        raise ConflictError(f"resume_from is one of {sorted(RESUME_CHOICES)}")
    repository = _repository(uow)
    if repository is None:
        raise NotFoundError(f"question {question_id} not found")
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    question = repository.get(question_id, for_update=True)
    if question is None or question.task_id != task.id:
        raise NotFoundError(f"question {question_id} not found on this task")
    if question.answered_at is not None:
        raise ConflictError(
            f"question {question_id} was answered by {question.answered_by_name} at "
            f"{local_text(question.answered_at)}"
        )
    now = clock.now()
    if task.state in CORRECTABLE_STATES:
        stored = require_contract(uow, task)
        body = correction_document(
            stored.document,
            of_version=task.contract_version,
            reason=ANSWER_CORRECTION_REASON,
            instructions=answer_text,
            resume_from=resume_from,
        )
        task = attach_correction(
            uow,
            clock,
            principal=principal,
            task_id=task.id,
            body=body,
            harnesses=harnesses,
            harness_gates=harness_gates,
            credential_sources=credential_sources,
            secret_providers=secret_providers,
            wired_providers=wired_providers,
            escalation_id=question.escalation_id,
            resume_from_work_branch=resume_from == "remote_branch",
        )
        action = ACTION_CORRECTED
        version: int | None = task.contract_version
    else:
        action = ACTION_RECORDED
        version = None
        escalation = uow.escalations.get(question.escalation_id) if question.escalation_id else None
        if escalation is not None and escalation.state is EscalationState.OPEN:
            task = record_decision(
                uow,
                clock,
                principal=principal,
                task_id=task.id,
                request=DecisionRequest(
                    kind="escalation_answer",
                    verbatim=answer_text,
                    resolves=escalation.question,
                    escalation_id=escalation.id,
                    reschedule=task.state is TaskState.BLOCKED,
                ),
            )
    question.answered_by = principal.id
    question.answered_by_name = principal.name
    question.answered_at = now
    question.answer_text = answer_text
    question.answer_action = action
    question.answer_contract_version = version
    repository.save(question)
    record_event(
        uow,
        clock,
        EventKind.MINION_QUESTION_ANSWERED,
        principal=principal.name,
        task_id=task.id,
        attempt_id=question.asked_by_attempt_id,
        payload={
            "question_id": question.id,
            "escalation_id": question.escalation_id,
            "question_text": question.question_text,
            "answer_text": answer_text,
            "answer_action": action,
            "contract_version": version,
            "resume_from": resume_from if action == ACTION_CORRECTED else None,
            "local_time": local_text(now),
            "reason": answer_text,
        },
    )
    return task, question


__all__ = [
    "ACTION_CORRECTED",
    "ACTION_RECORDED",
    "ANSWERING_ROLES",
    "answer_question",
    "ask_question",
    "list_questions",
    "open_question_for",
    "question_view",
]
