"""Why a task is stuck, in one plain sentence, and whose move it is (hades #607).

Every reason a task stops maps here to one sentence the operator can read, an owner
(you, Foundry, or the worker on Foundry's correction) and the clicks that apply. The
board, the card page and `GET /v1/board` all read this one table, so a Stuck card never
shows the gate probe's internal text as its headline; that text stays under Details.

Pure: the caller gathers the facts (`StuckFacts`) from the task, its open escalation and
its latest events, and `stuck_reason` decides."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from crucible.domain.lifecycle import TaskState

_S = TaskState


class Owner(StrEnum):
    YOU = "you"
    FOUNDRY = "foundry"
    WORKER = "worker"


OWNER_WORDS: dict[Owner, str] = {
    Owner.YOU: "You",
    Owner.FOUNDRY: "Foundry",
    Owner.WORKER: "The worker, on a correction from Foundry",
}
OWNER_LINES: dict[Owner, str] = {
    Owner.YOU: "Waiting on you.",
    Owner.FOUNDRY: "Waiting on Foundry. Nothing for you to do.",
    Owner.WORKER: (
        "Waiting on the worker, once Foundry sends it a correction. Nothing for you to do."
    ),
}

# The Stuck lane's two groups, in the order the board shows them.
GROUP_ME = "waiting_on_me"
GROUP_FOUNDRY = "waiting_on_foundry"
GROUPS: tuple[tuple[str, str], ...] = (
    (GROUP_ME, "Waiting on me"),
    (GROUP_FOUNDRY, "Waiting on Foundry"),
)

# The clicks a stuck card can offer: Answer only when the owner is the operator.
SEND_BACK = "send_back"
CANCEL = "cancel"
ANSWER = "answer"
OPERATOR_CLICKS = (ANSWER, SEND_BACK, CANCEL)
FOUNDRY_CLICKS = (SEND_BACK, CANCEL)

# Escalation kinds that are a question for the operator rather than for Foundry. A
# worker's `ambiguous_contract` or `missing_capability` is Foundry's to answer.
OPERATOR_QUESTION_KINDS = frozenset({"decision", "design", "design_question", "decision_question"})

# Diagnoses (`CICause`) that mean the code is wrong, so the worker fixes it on a
# correction; any other cause, or none yet, is Foundry's call.
WORKER_CI_CAUSES = frozenset(
    {"implementation_defect", "false_pre_pr_evidence", "correction_without_checks"}
)

GATE_PROVES_NOTHING = (
    "The checks already pass before any change, so a run could not prove anything. "
    "Foundry adds a check that fails first."
)


@dataclass(frozen=True, slots=True)
class StuckFacts:
    """What the projection knows about one task. Every field but `state` is optional."""

    state: TaskState
    # The open escalation: the reason the worker's `blocked.md` named (or a decision
    # kind), its question verbatim, and whether it is addressed to the operator.
    escalation_reason: str | None = None
    question: str | None = None
    for_operator: bool = False
    # The reason on the `task_blocked` event (gate_proves_nothing, check_cannot_run,
    # too_big_for_local:<n>).
    blocked_reason: str | None = None
    # The harness refused the current attempt's launch (unknown, disabled, untested).
    harness_refused: bool = False
    failing_gates: tuple[str, ...] = ()
    failing_jobs: tuple[str, ...] = ()
    ci_cause: str | None = None
    # Another task whose fix this one waits for, by its external id.
    waiting_for: str | None = None


@dataclass(frozen=True, slots=True)
class StuckReason:
    key: str
    sentence: str
    owner: Owner
    quote: str | None = None

    @property
    def group(self) -> str:
        return GROUP_ME if self.owner is Owner.YOU else GROUP_FOUNDRY

    @property
    def clicks(self) -> tuple[str, ...]:
        return OPERATOR_CLICKS if self.owner is Owner.YOU else FOUNDRY_CLICKS

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "sentence": self.sentence,
            "owner": self.owner.value,
            "owner_words": OWNER_WORDS[self.owner],
            "owner_line": OWNER_LINES[self.owner],
            "quote": self.quote,
            "group": self.group,
            "clicks": list(self.clicks),
        }


def words_list(names: Sequence[str]) -> str:
    """`unit`, `unit and lint`, `unit, lint and e2e`."""
    items = [str(name) for name in names if str(name)]
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _quote(text: str | None) -> str | None:
    cleaned = " ".join(str(text or "").split())
    cleaned = re.sub(r"^reason:\s*\w+\s*", "", cleaned)
    return cleaned or None


def named_task(text: str | None, external_ids: Iterable[str], *, own: str = "") -> str | None:
    """The first other task's external id the text names as a whole word, or None."""
    if not text:
        return None
    for external_id in sorted(set(external_ids) - {own, ""}):
        if re.search(rf"(?<![\w-]){re.escape(external_id)}(?![\w-])", text):
            return external_id
    return None


def _ci_reason(facts: StuckFacts) -> StuckReason:
    jobs = words_list(facts.failing_jobs)
    if not facts.failing_jobs:
        what = "CI failed on the pull request, and the failing job was not recorded."
    elif len(facts.failing_jobs) == 1:
        what = f"CI failed on the pull request: the {jobs} job failed."
    else:
        what = f"CI failed on the pull request: the {jobs} jobs failed."
    if facts.ci_cause in WORKER_CI_CAUSES:
        return StuckReason(
            "ci_certification_failed",
            f"{what} The code is at fault, so the worker fixes it on a correction from Foundry.",
            Owner.WORKER,
        )
    return StuckReason(
        "ci_certification_failed",
        f"{what} Foundry decides whether the worker fixes it or CI runs again.",
        Owner.FOUNDRY,
    )


def stuck_reason(facts: StuckFacts) -> StuckReason | None:
    """The one reason the task waits, or None when it is not stuck on anything."""
    state = facts.state
    blocked = str(facts.blocked_reason or "")
    worker_reason = str(facts.escalation_reason or "")
    if facts.for_operator and facts.question is not None:
        return StuckReason(
            "operator_question",
            "Foundry asked you a question it cannot answer alone. Answer it here and the "
            "work goes on.",
            Owner.YOU,
            _quote(facts.question),
        )
    if blocked == "gate_proves_nothing":
        return StuckReason("gate_proves_nothing", GATE_PROVES_NOTHING, Owner.FOUNDRY)
    if blocked == "check_cannot_run":
        return StuckReason(
            "check_cannot_run",
            "A required check cannot run in the worker, so no run could pass it. Foundry "
            "fixes the check or the image.",
            Owner.FOUNDRY,
        )
    if (
        facts.harness_refused
        or blocked.startswith("too_big_for_local")
        or "wrong harness" in str(facts.question or "").lower()
    ):
        return StuckReason(
            "wrong_harness",
            "The task went to a harness that cannot run it. Foundry routes it to another harness.",
            Owner.FOUNDRY,
            _quote(facts.question) if worker_reason else None,
        )
    if facts.waiting_for:
        return StuckReason(
            "waiting_on_task",
            f"This waits for {facts.waiting_for} to land its fix. Foundry starts it again "
            f"once {facts.waiting_for} merges.",
            Owner.FOUNDRY,
        )
    if worker_reason == "ambiguous_contract":
        return StuckReason(
            "ambiguous_contract",
            "The worker stopped because the contract reads two ways. Foundry answers its "
            "question with a correction.",
            Owner.FOUNDRY,
            _quote(facts.question),
        )
    if worker_reason == "missing_capability":
        return StuckReason(
            "missing_capability",
            "The worker stopped because it lacks a program or access it needs. Foundry "
            "adds it or changes the contract.",
            Owner.FOUNDRY,
            _quote(facts.question),
        )
    if state is _S.CI_CERTIFICATION_FAILED:
        return _ci_reason(facts)
    if state is _S.AWAITING_INTERNAL_REVIEW:
        return StuckReason(
            "awaiting_internal_review",
            "The work is finished and waits for Foundry's review before it is published.",
            Owner.FOUNDRY,
        )
    if state is _S.PRE_PR_GATES_FAILED:
        gates = words_list(facts.failing_gates)
        what = (
            f"Hades's checks failed before the pull request ({gates})"
            if gates
            else ("Hades's checks failed before the pull request")
        )
        return StuckReason(
            "pre_pr_gates_failed",
            f"{what}. Foundry sends the worker a correction.",
            Owner.FOUNDRY,
        )
    if state is _S.PUBLISH_FAILED:
        return StuckReason(
            "publish_failed",
            "Publishing the pull request failed. Foundry publishes again or corrects the work.",
            Owner.FOUNDRY,
        )
    if state is _S.HEAD_DIVERGED:
        return StuckReason(
            "head_diverged",
            "Someone pushed to the work branch outside Hades. Foundry decides whether that "
            "push stands.",
            Owner.FOUNDRY,
        )
    if facts.question is not None:
        return StuckReason(
            "foundry_question",
            "Hades stopped the task and asked Foundry a question. Foundry answers it.",
            Owner.FOUNDRY,
            _quote(facts.question),
        )
    if state is _S.BLOCKED:
        return StuckReason(
            "blocked",
            "The task is blocked and no reason was recorded. Foundry looks into it.",
            Owner.FOUNDRY,
        )
    return None


__all__ = [
    "ANSWER",
    "CANCEL",
    "GATE_PROVES_NOTHING",
    "GROUPS",
    "GROUP_FOUNDRY",
    "GROUP_ME",
    "OPERATOR_QUESTION_KINDS",
    "OWNER_WORDS",
    "SEND_BACK",
    "Owner",
    "StuckFacts",
    "StuckReason",
    "named_task",
    "stuck_reason",
    "words_list",
]
