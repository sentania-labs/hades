"""What the operator must do about one task (hades FDY-0629): the facts gathered for
`crucible.domain.needs_you` from the task, its current contract, its open escalation and
its stuck reason. The card page and the board's lanes both read this one function."""

from __future__ import annotations

import re
from typing import Any

from crucible.application.admin.stuck import is_operator_question
from crucible.domain.needs_you import (
    NeedsFacts,
    correction_facts,
    lane_for,
    needs_you,
    operator_needs_from,
)
from crucible.domain.stuck_reasons import StuckReason

_WAITS_FOR = re.compile(r"This waits for (\S+) to land")


def waiting_for(reason: StuckReason | None) -> str | None:
    """The other task a `waiting_on_task` reason names, read back from its sentence."""
    if reason is None or reason.key != "waiting_on_task":
        return None
    match = _WAITS_FOR.match(reason.sentence)
    return match.group(1) if match else None


def needs_block(
    task: Any,
    document: dict[str, Any] | None,
    escalation: Any | None,
    reason: StuckReason | None,
) -> dict[str, Any]:
    """What the operator must do, in plain words, with `board_lane`, the lane of the
    board the card sits in. One mapping for the card page and the board."""
    correction_reason, findings = correction_facts(document)
    needs = needs_you(
        NeedsFacts(
            state=task.state,
            question=str(escalation.question) if escalation is not None else None,
            for_operator=is_operator_question(escalation),
            operator_needs=operator_needs_from(document),
            correction_reason=correction_reason,
            findings=findings,
            stuck_key=reason.key if reason is not None else None,
            stuck_owner=reason.owner.value if reason is not None else None,
            waiting_for=waiting_for(reason),
        )
    )
    return {**needs.as_dict(), "board_lane": lane_for(task.state, needs)}


__all__ = ["needs_block", "waiting_for"]
