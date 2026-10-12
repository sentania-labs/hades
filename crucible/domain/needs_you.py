"""What the operator must do about a task, in plain words (hades FDY-0629).

Every card opens with exactly one of three states, and the board's first lane holds the
cards in the second and third:

- "Nothing from you." with one sentence of what is happening now and one of what happens
  next, read from a small table of plain phrases keyed by the task's state and its latest
  attempt. No state name, gate name, hash, command or id reaches the operator from here.
- "Your decision:" with the open escalation's question as the worker wrote it, the
  options it listed, and the answer form.
- "Waiting on you for:" with the items the contract's `operator_needs` names, for a task
  held on operator evidence (blocked or submitted).

Pure: the projections gather `NeedsFacts` from the task, its contract, its open
escalation and its latest attempt, and `needs_you` decides."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from crucible.domain.lifecycle import TaskState
from crucible.domain.stuck_reasons import words_list

_S = TaskState

NOTHING = "nothing"
DECISION = "decision"
EVIDENCE = "evidence"

LABELS = {
    NOTHING: "Nothing from you.",
    DECISION: "Your decision:",
    EVIDENCE: "Waiting on you for:",
}

# The board's lanes, in order. Needs you holds the decision and evidence cards; the rest
# hold "Nothing from you" cards by what is happening to them.
LANE_NEEDS_YOU = "needs_you"
LANE_WORKING = "working"
LANE_WAITING = "waiting"
LANE_DONE = "done"
BOARD_LANES: tuple[tuple[str, str, str], ...] = (
    (LANE_NEEDS_YOU, "Needs you", "A decision or something only you can supply."),
    (LANE_WORKING, "Working", "A worker, Hades or Foundry is on it. Nothing needed."),
    (LANE_WAITING, "Waiting on review or CI", "Review or CI is running. Nothing needed."),
    (LANE_DONE, "Done today", "Finished today: merged, closed or cancelled."),
)
LANE_NAMES = {key: name for key, name, _meaning in BOARD_LANES}

# The states whose "Nothing from you" card sits in Waiting on review or CI.
WAITING_STATES = frozenset(
    {
        _S.AWAITING_INTERNAL_REVIEW,
        _S.AWAITING_ACCEPTANCE,
        _S.AWAITING_EXTERNAL_REVIEW,
        _S.EXTERNAL_FEEDBACK_RECEIVED,
        _S.AWAITING_CI_CERTIFICATION,
        _S.READY_FOR_MERGE,
    }
)
# The states whose card is finished work, shown in Done today on the day it ended.
DONE_STATES = frozenset(
    {
        _S.MERGED,
        _S.RELEASE_CANDIDATE,
        _S.RELEASED,
        _S.CLOSED,
        _S.REJECTED,
        _S.CANCELLED,
    }
)
# The contract's `operator_needs` holds the task only in these states.
EVIDENCE_STATES = frozenset({_S.BLOCKED, _S.SUBMITTED})

# What is happening now and what happens next, by state, for a task nobody is asking the
# operator anything about. Running and blocked tasks read their attempt first (below).
PHRASES: dict[TaskState, tuple[str, str]] = {
    _S.SENT_BACK: (
        "You sent this proposal back to Foundry.",
        "Next: Foundry amends it and proposes it again.",
    ),
    _S.SUBMITTED: (
        "The task is approved and waits for a worker.",
        "Next: Hades starts it when a worker is free.",
    ),
    _S.SCHEDULED: ("Hades is starting a worker.", "Next: the worker begins the work."),
    _S.AWAITING_QUOTA: (
        "The task waits for capacity.",
        "Next: Hades starts it when a worker is free.",
    ),
    _S.RUNNING: (
        "The worker is doing the work.",
        "Next: Hades checks the result, then opens a pull request.",
    ),
    _S.REPORTED: (
        "The worker finished and Hades is checking the result.",
        "Next: review, then a pull request.",
    ),
    _S.BLOCKED: (
        "The task is stopped and Foundry is looking into it.",
        "Next: Foundry sends a correction or starts it again.",
    ),
    _S.PRE_PR_GATES_FAILED: (
        "Hades's checks caught a problem before the pull request.",
        "Next: Foundry sends the worker a correction.",
    ),
    _S.AWAITING_INTERNAL_REVIEW: (
        "The work is finished and waits for Foundry's review.",
        "Next: Hades opens the pull request if the review passes.",
    ),
    _S.GATES_PASSED: ("Hades's checks passed.", "Next: the pull request opens."),
    _S.AWAITING_ACCEPTANCE: (
        "The work passed Hades's checks and waits for Foundry to accept it.",
        "Next: the pull request opens once it is accepted.",
    ),
    _S.ACCEPTED: ("Foundry accepted the work.", "Next: Hades opens the pull request."),
    _S.PUBLISHING: ("Hades is opening the pull request.", "Next: Codex reviews it."),
    _S.PUBLISH_FAILED: (
        "Opening the pull request failed.",
        "Next: Foundry publishes again or corrects the work.",
    ),
    _S.AWAITING_EXTERNAL_REVIEW: (
        "Codex is reviewing the pull request.",
        "Next: the worker fixes any findings, then CI, then merge.",
    ),
    _S.EXTERNAL_FEEDBACK_RECEIVED: (
        "Codex left review findings.",
        "Next: Foundry sends the worker a correction, then Codex re-reviews.",
    ),
    _S.AWAITING_CI_CERTIFICATION: (
        "CI is running on the pull request.",
        "Next: Hades merges it when CI is green.",
    ),
    _S.CI_CERTIFICATION_FAILED: (
        "CI failed on the pull request.",
        "Next: Foundry decides whether the worker fixes it or CI runs again.",
    ),
    _S.HEAD_DIVERGED: (
        "Someone pushed to the work branch outside Hades.",
        "Next: Foundry decides whether that push stands.",
    ),
    _S.READY_FOR_MERGE: ("Review and CI passed.", "Next: Hades merges the pull request."),
    _S.MERGED: ("The pull request merged.", "Next: nothing. The work is in main."),
    _S.RELEASE_CANDIDATE: ("The work is in a release candidate.", "Next: the release."),
    _S.RELEASED: ("The work shipped in a release.", "Next: nothing. It is done."),
    _S.CLOSED: ("The task is closed.", "Next: nothing. It is done."),
    _S.REJECTED: (
        "The proposal was declined.",
        "Next: nothing, unless Foundry proposes it again.",
    ),
    _S.CANCELLING: ("Hades is stopping the worker.", "Next: the task ends as cancelled."),
    _S.CANCELLED: ("The task was cancelled.", "Next: nothing. It is done."),
}

# A running correction, by the reason the correction was attached for: what the worker
# is fixing and what follows.
CORRECTION_PHRASES: dict[str, tuple[str, str]] = {
    "external_review": ("review findings", "Next: Codex re-reviews, then merge."),
    "ci_certification": ("the failing CI jobs", "Next: CI runs again, then merge."),
    "pre_pr_gates": (
        "what Hades's checks caught",
        "Next: Hades checks the result, then review.",
    ),
    "internal_review": (
        "Foundry's review findings",
        "Next: Foundry reviews again, then the pull request.",
    ),
    "needs_more_work": (
        "what Foundry asked for",
        "Next: Hades checks the result, then review.",
    ),
}

# A task stopped on something Foundry owns (hades #607 reasons), without the gate names
# the stuck sentence may carry.
STUCK_PHRASES: dict[str, tuple[str, str]] = {
    "gate_proves_nothing": (
        "The checks already pass before any change, so the run could not prove anything.",
        "Next: Foundry adds a check that fails first, then the work runs again.",
    ),
    "check_cannot_run": (
        "A required check cannot run in the worker.",
        "Next: Foundry fixes the check or the image, then the work runs again.",
    ),
    "wrong_harness": (
        "The task went to a worker that cannot run it.",
        "Next: Foundry routes it to another worker, then it runs again.",
    ),
    "ambiguous_contract": (
        "The worker stopped because the task reads two ways.",
        "Next: Foundry answers its question and the worker goes on.",
    ),
    "missing_capability": (
        "The worker stopped because it lacks a program or access it needs.",
        "Next: Foundry adds it or changes the task, then the worker goes on.",
    ),
    "foundry_question": (
        "Hades stopped the task and asked Foundry a question.",
        "Next: Foundry answers it and the work goes on.",
    ),
    "awaiting_internal_review": PHRASES[_S.AWAITING_INTERNAL_REVIEW],
    "pre_pr_gates_failed": PHRASES[_S.PRE_PR_GATES_FAILED],
    "publish_failed": PHRASES[_S.PUBLISH_FAILED],
    "head_diverged": PHRASES[_S.HEAD_DIVERGED],
    "blocked": PHRASES[_S.BLOCKED],
}

PROPOSAL_QUESTION = "Foundry proposed this task. Approve it to start the work, or decline it."
PROPOSAL_ASK = "Approve or decline this proposal."

_OPTION_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|[a-z][.)])\s+(\S.*?)\s*$")
_REASON_LINE = re.compile(r"^reason:\s*\w+\s*\n?")


@dataclass(frozen=True, slots=True)
class NeedsFacts:
    """What the projection knows about one task. Every field but `state` is optional."""

    state: TaskState
    # The open escalation's question verbatim and whether it is addressed to the operator
    # (a decision or design kind) rather than to Foundry.
    question: str | None = None
    for_operator: bool = False
    # The contract's `operator_needs`.
    operator_needs: tuple[str, ...] = ()
    # The current contract version is a correction: why, and how many findings it names.
    correction_reason: str | None = None
    findings: int = 0
    # The stuck reason's key and owner (hades #607), when the task has one.
    stuck_key: str | None = None
    stuck_owner: str | None = None
    # Another task whose fix this one waits for, by its external id.
    waiting_for: str | None = None


@dataclass(frozen=True, slots=True)
class NeedsYou:
    kind: str
    now: str = ""
    next: str = ""
    question: str | None = None
    options: tuple[str, ...] = ()
    items: tuple[str, ...] = ()
    proposal: bool = False

    @property
    def label(self) -> str:
        return LABELS[self.kind]

    @property
    def ask(self) -> str:
        """One line for the board card."""
        if self.kind == EVIDENCE:
            return f"{self.label} {words_list(self.items)}."
        if self.kind == DECISION:
            return PROPOSAL_ASK if self.proposal else first_sentence(self.question or "")
        return self.now

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "now": self.now,
            "next": self.next,
            "question": self.question,
            "options": list(self.options),
            "items": list(self.items),
            "proposal": self.proposal,
            "ask": self.ask,
        }


def first_sentence(value: str) -> str:
    text = " ".join(value.split())
    match = re.search(r"(?<=[.!?])\s", text)
    return text[: match.start() + 1].rstrip() if match else text


def split_options(question: str) -> tuple[str, tuple[str, ...]]:
    """The question's text and the options it lists, when the worker wrote them as bullet
    or numbered lines; the text keeps every other line as written."""
    cleaned = _REASON_LINE.sub("", question.strip(), count=1)
    kept: list[str] = []
    options: list[str] = []
    for line in cleaned.splitlines():
        match = _OPTION_LINE.match(line)
        if match is not None:
            options.append(match.group(1))
        else:
            kept.append(line)
    text = "\n".join(kept).strip() if options else cleaned.strip()
    return text or cleaned.strip(), tuple(options)


def lane_for(state: TaskState, needs: NeedsYou) -> str:
    """The board lane the card belongs in: the decision and evidence cards in Needs you,
    the rest by what is happening to them. A done card is Done today only on the day it
    ended; the board checks the day."""
    if needs.kind != NOTHING:
        return LANE_NEEDS_YOU
    if state in DONE_STATES:
        return LANE_DONE
    if state in WAITING_STATES:
        return LANE_WAITING
    return LANE_WORKING


def _nothing(facts: NeedsFacts) -> NeedsYou:
    state = facts.state
    if facts.waiting_for:
        return NeedsYou(
            NOTHING,
            f"This waits for {facts.waiting_for} to land its fix.",
            f"Next: Foundry starts it again once {facts.waiting_for} merges.",
        )
    if state is _S.RUNNING and facts.correction_reason:
        what, after = CORRECTION_PHRASES.get(
            facts.correction_reason, CORRECTION_PHRASES["needs_more_work"]
        )
        if facts.correction_reason == "external_review" and facts.findings:
            plural = "finding" if facts.findings == 1 else "findings"
            what = f"{facts.findings} review {plural}"
        return NeedsYou(NOTHING, f"The worker is fixing {what}.", after)
    if state is _S.CI_CERTIFICATION_FAILED and facts.stuck_owner == "worker":
        return NeedsYou(
            NOTHING,
            PHRASES[state][0],
            "Next: the worker fixes it on a correction from Foundry, then CI runs again.",
        )
    if facts.stuck_key in STUCK_PHRASES and state not in (_S.CI_CERTIFICATION_FAILED,):
        now, after = STUCK_PHRASES[facts.stuck_key]
        return NeedsYou(NOTHING, now, after)
    now, after = PHRASES.get(state, ("The task is in Hades's hands.", "Next: Hades moves it on."))
    return NeedsYou(NOTHING, now, after)


def needs_you(facts: NeedsFacts) -> NeedsYou:
    """The one state the card opens with."""
    if facts.operator_needs and facts.state in EVIDENCE_STATES:
        return NeedsYou(EVIDENCE, items=tuple(facts.operator_needs))
    if facts.for_operator and facts.question:
        text, options = split_options(facts.question)
        return NeedsYou(DECISION, question=text, options=options)
    if facts.state is _S.PROPOSED:
        return NeedsYou(DECISION, question=PROPOSAL_QUESTION, proposal=True)
    return _nothing(facts)


def operator_needs_from(document: dict[str, Any] | None) -> tuple[str, ...]:
    """The contract's `operator_needs`, each item as written, blank ones dropped."""
    values = (document or {}).get("operator_needs")
    if not isinstance(values, Sequence) or isinstance(values, str):
        return ()
    return tuple(str(item).strip() for item in values if str(item).strip())


def correction_facts(document: dict[str, Any] | None) -> tuple[str | None, int]:
    """The current version's correction reason and how many findings it addresses."""
    correction = (document or {}).get("correction") or {}
    if not isinstance(correction, dict) or not correction:
        return None, 0
    addresses = correction.get("addresses")
    count = (
        len(addresses) if isinstance(addresses, Sequence) and not isinstance(addresses, str) else 0
    )
    return str(correction.get("reason") or "needs_more_work"), count


__all__ = [
    "BOARD_LANES",
    "DECISION",
    "DONE_STATES",
    "EVIDENCE",
    "EVIDENCE_STATES",
    "LABELS",
    "LANE_DONE",
    "LANE_NAMES",
    "LANE_NEEDS_YOU",
    "LANE_WAITING",
    "LANE_WORKING",
    "NOTHING",
    "PHRASES",
    "WAITING_STATES",
    "NeedsFacts",
    "NeedsYou",
    "correction_facts",
    "first_sentence",
    "lane_for",
    "needs_you",
    "operator_needs_from",
    "split_options",
]
