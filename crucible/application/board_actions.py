"""Phase actions from the board card (hades #489).

Each move is one of the existing operations, applied with the operator's note: the
note is stored, the audit gets a `task_phase_action_applied` event whose `verbatim` is
the note's text, and the operation runs as the API would run it. A move is offered
only where the task's state allows it; the default move for a lane is published in
`DEFAULT_MOVES` so the Next phase button and the UI table say the same thing."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from crucible.application.acceptance import record_acceptance
from crucible.application.admin.board_lanes import LANES, is_scott_question, lane_for_state
from crucible.application.cancel_task import cancel_task
from crucible.application.corrections import (
    CORRECTABLE_STATES,
    attach_correction,
    correction_document,
)
from crucible.application.decisions import record_decision
from crucible.application.errors import ConflictError, NotFoundError, TransitionNotAllowedError
from crucible.application.harnesses import HarnessRegistry
from crucible.application.minion_questions import answer_question, open_question_for
from crucible.application.proposals import approve_task
from crucible.application.start_task import start_task
from crucible.application.task_notes import add_note, require_operator
from crucible.application.transitions import record_event, require_contract
from crucible.contracts.api import AcceptRequest, CancelRequest, DecisionRequest, StartRequest
from crucible.contracts.task_contract import TaskContractV1
from crucible.domain.entities import (
    AcceptanceVerdict,
    Escalation,
    EscalationState,
    Principal,
    PullRequest,
    Task,
    TaskNote,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TASK_TERMINAL, TASK_TRANSITIONS, TaskState
from crucible.ports.clock import Clock
from crucible.ports.harness import CredentialSource, HarnessGate
from crucible.ports.repository import UnitOfWork

_S = TaskState
LANE_NAMES = {key: name for key, name, _meaning in LANES}


@dataclass(frozen=True, slots=True)
class Move:
    key: str
    label: str
    operation: str
    meaning: str


MOVES: dict[str, Move] = {
    move.key: move
    for move in (
        Move(
            "approve",
            "Approve and queue",
            "approve_task (POST /v1/tasks/{id}/approve)",
            "The proposal is approved with the note as its reason and queued to start.",
        ),
        Move(
            "start",
            "Start now",
            "start_task (POST /v1/tasks/{id}/start)",
            "The submitted task is scheduled under its contract's policy.",
        ),
        Move(
            "correct_remote",
            "Correction, resume from the PR branch",
            "attach_correction with resume_from remote_branch (POST /v1/tasks/{id}/corrections)",
            "A correction version with the note as its instructions, resumed from the "
            "pushed work branch.",
        ),
        Move(
            "correct_last",
            "Correction, resume from the last attempt",
            "attach_correction with resume_from last_attempt (POST /v1/tasks/{id}/corrections)",
            "A correction version with the note as its instructions, resumed from the last "
            "attempt's sealed bundle.",
        ),
        Move(
            "accept",
            "Accept the collected head",
            "record_acceptance with verdict accepted (POST /v1/tasks/{id}/accept)",
            "An accepted AcceptanceResult with the note as its reasoning.",
        ),
        Move(
            "answer",
            "Answer the open escalation",
            "answer_question with the note as the answer "
            "(POST /v1/tasks/{id}/questions/{question_id}/answer); record_decision on an "
            "escalation without a question record (POST /v1/tasks/{id}/decisions)",
            "The worker's question is answered with the note: a correction with the note "
            "as its instructions resumes the attempt and closes the escalation. An "
            "escalation with no question record takes a decision with the note as its "
            "verbatim; a blocked task is scheduled again.",
        ),
        Move(
            "cancel",
            "Cancel with reason",
            "cancel_task (POST /v1/tasks/{id}/cancel)",
            "The task is cancelled with the note as the reason and the verbatim.",
        ),
    )
}
MOVE_ORDER: tuple[str, ...] = tuple(MOVES)

# The default move per lane, as the UI publishes it: Inbox to Holding pen, Holding pen
# to In progress, Stuck to In progress via resume from the PR branch, Waiting on Scott
# to In progress, In progress to Graveyard. Wins and Graveyard have no next phase.
DEFAULT_MOVES: dict[str, tuple[str, str]] = {
    "inbox": ("holding_pen", "approve"),
    "holding_pen": ("in_progress", "start"),
    "stuck": ("in_progress", "correct_remote"),
    "waiting_on_scott": ("in_progress", "answer"),
    "in_progress": ("graveyard", "cancel"),
}


def default_move_table() -> list[dict[str, str]]:
    """The published table: from lane, to lane, and the move the Next phase button applies."""
    return [
        {
            "from_key": lane,
            "from": LANE_NAMES[lane],
            "to_key": to_lane,
            "to": LANE_NAMES[to_lane],
            "move": move,
            "label": MOVES[move].label,
        }
        for lane, (to_lane, move) in DEFAULT_MOVES.items()
    ]


def default_move(lane: str) -> str | None:
    found = DEFAULT_MOVES.get(lane)
    return found[1] if found else None


# The correction reason a state calls for (05): the one state that restricts the reason
# is ready_for_merge; the rest name the stage the work stopped at.
CORRECTION_REASON_BY_STATE: dict[TaskState, str] = {
    _S.PRE_PR_GATES_FAILED: "pre_pr_gates",
    _S.CI_CERTIFICATION_FAILED: "ci_certification",
    _S.AWAITING_EXTERNAL_REVIEW: "external_review",
    _S.EXTERNAL_FEEDBACK_RECEIVED: "external_review",
}
DEFAULT_CORRECTION_REASON = "needs_more_work"


def open_escalation(uow: UnitOfWork, task_id: str) -> Escalation | None:
    """The newest open escalation on the task, or None."""
    rows = [
        row for row in uow.escalations.list_for_task(task_id) if row.state is EscalationState.OPEN
    ]
    return max(rows, key=lambda row: (row.opened_at, row.id)) if rows else None


def card_lane(task: Task, escalation: Escalation | None) -> str:
    return lane_for_state(
        task.state, waiting_on_scott=bool(escalation and is_scott_question(escalation))
    )


def valid_moves(
    task: Task, *, escalation: Escalation | None, pull_request: PullRequest | None
) -> list[str]:
    """The moves the task's state allows, in `MOVE_ORDER`."""
    state = task.state
    allowed: set[str] = set()
    if state is _S.PROPOSED:
        allowed.add("approve")
    if state is _S.SUBMITTED:
        allowed.add("start")
    if state in CORRECTABLE_STATES:
        allowed.add("correct_last")
        if pull_request is not None:
            allowed.add("correct_remote")
    if state is _S.AWAITING_ACCEPTANCE:
        allowed.add("accept")
    if escalation is not None and state not in TASK_TERMINAL:
        allowed.add("answer")
    if (state, _S.CANCELLED) in TASK_TRANSITIONS or (state, _S.CANCELLING) in TASK_TRANSITIONS:
        allowed.add("cancel")
    return [key for key in MOVE_ORDER if key in allowed]


@dataclass(slots=True)
class CorrectionDeps:
    """What `attach_correction` validates a contract against; the API's context has them."""

    harnesses: HarnessRegistry | None = None
    harness_gates: dict[str, HarnessGate] = field(default_factory=dict)
    credential_sources: dict[str, CredentialSource] = field(default_factory=dict)
    secret_providers: Collection[str] = frozenset()
    wired_providers: Collection[str] | None = None


@dataclass(slots=True)
class MoveResult:
    task: Task
    note: TaskNote
    move: Move
    lane: str
    message: str


def apply_move(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    move: str,
    note_text: str,
    deps: CorrectionDeps | None = None,
) -> MoveResult:
    """Store the note, record the action with the note as its verbatim, run the move."""
    require_operator(principal)
    chosen = MOVES.get(move)
    if chosen is None:
        raise ConflictError(f"unknown move {move!r}")
    task = uow.tasks.get(task_id)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    if not note_text.strip():
        raise ConflictError("the move needs your words: the note is the record of the decision")
    escalation = open_escalation(uow, task.id)
    pull_request = uow.pull_requests.get_for_task(task.id)
    allowed = valid_moves(task, escalation=escalation, pull_request=pull_request)
    if move not in allowed:
        raise TransitionNotAllowedError(
            f"{chosen.label} is not a move for a task in {task.state.value}; "
            f"valid: {', '.join(MOVES[key].label for key in allowed) or 'none'}"
        )
    lane = card_lane(task, escalation)
    note = add_note(uow, clock, principal=principal, task_id=task.id, text=note_text)
    record_event(
        uow,
        clock,
        EventKind.TASK_PHASE_ACTION_APPLIED,
        principal=principal.name,
        task_id=task.id,
        payload={
            "move": chosen.key,
            "label": chosen.label,
            "operation": chosen.operation,
            "lane": lane,
            "from_state": task.state.value,
            "note_id": note.id,
            "verbatim": note.text,
            "reason": note.text,
            "acted_at_local": clock.now()
            .astimezone(ZoneInfo("America/Chicago"))
            .isoformat(timespec="seconds"),
        },
    )
    deps = deps or CorrectionDeps()
    if move == "cancel":
        task = cancel_task(
            uow,
            clock,
            principal=principal,
            task_id=task.id,
            request=CancelRequest(reason=note.text, verbatim=note.text, decided_by=principal.name),
        )
        message = f"Cancelled {task.external_id}."
    elif move == "start":
        contract = TaskContractV1.model_validate(require_contract(uow, task).document)
        task = start_task(
            uow,
            clock,
            principal=principal,
            task_id=task.id,
            request=StartRequest(policy_version=contract.policy.version),
        )
        message = f"Scheduled {task.external_id}."
    elif move == "approve":
        task = approve_task(uow, clock, principal=principal, task_id=task.id, reason=note.text)
        message = f"Approved {task.external_id}; it is queued."
    elif move in {"correct_remote", "correct_last"}:
        stored = require_contract(uow, task)
        body = correction_document(
            stored.document,
            of_version=task.contract_version,
            reason=CORRECTION_REASON_BY_STATE.get(task.state, DEFAULT_CORRECTION_REASON),
            instructions=note.text,
            resume_from="remote_branch" if move == "correct_remote" else "last_attempt",
        )
        task = attach_correction(
            uow,
            clock,
            principal=principal,
            task_id=task.id,
            body=body,
            harnesses=deps.harnesses,
            harness_gates=deps.harness_gates,
            credential_sources=deps.credential_sources,
            secret_providers=deps.secret_providers,
            wired_providers=deps.wired_providers,
        )
        message = f"Attached correction version {task.contract_version} to {task.external_id}."
    elif move == "accept":
        task = record_acceptance(
            uow,
            clock,
            principal=principal,
            task_id=task.id,
            request=AcceptRequest(verdict=AcceptanceVerdict.ACCEPTED, reasoning=note.text),
        )
        message = f"Accepted {task.external_id}."
    else:
        assert escalation is not None
        question = open_question_for(uow, task.id, escalation)
        if question is not None:
            # hades #208 item 2: the board's Answer and a card thread share one record
            # and one call; the note's words are the answer the worker gets back.
            task, _answered = answer_question(
                uow,
                clock,
                principal=principal,
                task_id=task.id,
                question_id=question.id,
                answer_text=note.text,
                harnesses=deps.harnesses,
                harness_gates=deps.harness_gates,
                credential_sources=deps.credential_sources,
                secret_providers=deps.secret_providers,
                wired_providers=deps.wired_providers,
            )
            message = f"Answered the question on {task.external_id}."
        else:
            task = record_decision(
                uow,
                clock,
                principal=principal,
                task_id=task.id,
                request=DecisionRequest(
                    kind="escalation_answer",
                    verbatim=note.text,
                    resolves=escalation.question,
                    escalation_id=escalation.id,
                    reschedule=task.state is _S.BLOCKED,
                ),
            )
            message = f"Answered the escalation on {task.external_id}."
    return MoveResult(task=task, note=note, move=chosen, lane=lane, message=message)


def next_phase(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    note_text: str,
    deps: CorrectionDeps | None = None,
) -> MoveResult:
    """Apply the published default move for the task's lane."""
    task = uow.tasks.get(task_id)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    lane = card_lane(task, open_escalation(uow, task.id))
    move = default_move(lane)
    if move is None:
        raise ConflictError(f"the {LANE_NAMES[lane]} lane has no next phase")
    return apply_move(
        uow, clock, principal=principal, task_id=task_id, move=move, note_text=note_text, deps=deps
    )


def moves_for_card(
    task: Task, *, escalation: Escalation | None, pull_request: PullRequest | None
) -> list[dict[str, str]]:
    return [
        {"key": key, "label": MOVES[key].label, "meaning": MOVES[key].meaning}
        for key in valid_moves(task, escalation=escalation, pull_request=pull_request)
    ]


def move_labels(keys: Sequence[str]) -> list[str]:
    return [MOVES[key].label for key in keys]


__all__ = [
    "CORRECTION_REASON_BY_STATE",
    "DEFAULT_MOVES",
    "MOVES",
    "MOVE_ORDER",
    "CorrectionDeps",
    "Move",
    "MoveResult",
    "apply_move",
    "card_lane",
    "correction_document",
    "default_move",
    "default_move_table",
    "move_labels",
    "moves_for_card",
    "next_phase",
    "open_escalation",
    "valid_moves",
]
