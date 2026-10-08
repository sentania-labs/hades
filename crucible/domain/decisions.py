"""Decision kind taxonomy (04, 09, FDY-0509).

A closed list of accepted decision kinds for ``POST /tasks/{id}/decisions``.
Foundry and the operator only use these kinds; anything else is a 422 error.
Existing decisions stored with other kinds remain readable — only new requests
are validated.

Internal closure kinds (``task_cancelled``, ``task_closed``) are used by
cancel_task / close_task to close open escalations when a task ends.
They are NOT part of the public allowlist (Finding 01M4CFEK8J8BXDEB0NETRX267E):
only ``PublicDecisionKind`` is exposed through the API endpoint.
"""

from __future__ import annotations

from crucible.domain.waivers import WAIVER_KINDS

# Public decision kinds accepted by ``POST /tasks/{id}/decisions`` (FDY-0509).
#
# ``waiver``-family kinds come from domain/waivers.py.
# ``escalation_answer`` is the kind Foundry's client sends to answer an escalation.
# ``release_authorization`` authorizes a release (04, 09).
# ``accept`` accepts the head (04, 09).
# ``recollect`` recollects the head (09).
# ``scope_clarified`` is the kind used by the blocked-task flow to clarify
#   scope before rescheduling (09).
#
# ``task_cancelled`` / ``task_closed`` are internal system-recorded kinds
# used by cancel_task / close_task; they are intentionally excluded from
# the public list so a task owner cannot fabricate lifecycle events.
PUBLIC_DECISION_KINDS: frozenset[str] = frozenset(
    {
        *WAIVER_KINDS,
        "escalation_answer",
        "release_authorization",
        "accept",
        "recollect",
        "scope_clarified",
    }
)

# Internal kinds that the system itself uses to close escalations.
# These are valid for stored decisions (backwards-compatible reads) but
# are NOT exposed through the public API endpoint.
_INTERNAL_KINDS: frozenset[str] = frozenset({"task_cancelled", "task_closed"})

# The full set of accepted decision kinds (FDY-0509).
# Kept for backwards-compatible reads and schema output.
ACCEPTED_DECISION_KINDS: frozenset[str] = PUBLIC_DECISION_KINDS | _INTERNAL_KINDS

# Operator-only kinds: waiver kinds and release_authorization.
# Orchestrators cannot record these (04).
OPERATOR_ONLY_DECISION_KINDS: frozenset[str] = frozenset({"release_authorization", *WAIVER_KINDS})
