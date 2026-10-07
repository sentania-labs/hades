"""Decision kind taxonomy (04, 09, FDY-0509).

A closed list of accepted decision kinds for ``POST /tasks/{id}/decisions``.
Foundry and the operator only use these kinds; anything else is a 422 error.
Existing decisions stored with other kinds remain readable — only new requests
are validated.
"""

from __future__ import annotations

from crucible.domain.waivers import WAIVER_KINDS

# The full set of accepted decision kinds (FDY-0509).
#
# ``waiver``-family kinds come from domain/waivers.py.
# ``escalation_answer`` is the kind Foundry's client sends to answer an escalation.
# ``release_authorization`` authorizes a release (04, 09).
# ``accept`` accepts the head (04, 09).
# ``recollect`` recollects the head (09).
# ``task_cancelled`` / ``task_closed`` are system-recorded kinds used by
#   cancel_task / close_task to close open escalations when a task ends.
ACCEPTED_DECISION_KINDS: frozenset[str] = frozenset(
    {
        *WAIVER_KINDS,
        "escalation_answer",
        "release_authorization",
        "accept",
        "recollect",
        "task_cancelled",
        "task_closed",
    }
)

# Operator-only kinds: waiver kinds and release_authorization.
# Orchestrators cannot record these (04).
OPERATOR_ONLY_DECISION_KINDS: frozenset[str] = frozenset({"release_authorization", *WAIVER_KINDS})
