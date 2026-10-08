"""Decision kind taxonomy (04, 09, FDY-0509)."""

from __future__ import annotations

from crucible.domain.waivers import WAIVER_KINDS

# Public decision kinds accepted by ``POST /tasks/{id}/decisions``.  The API
# validates new requests only; historic decision rows remain readable.
ACCEPTED_DECISION_KINDS: frozenset[str] = frozenset(
    {
        *WAIVER_KINDS,
        "accept",
        "escalation_answer",
        "recollect",
        "release_authorization",
        "scope_clarified",
    }
)

# ADR 0025: waiving the remaining external review rounds, or accepting that a
# repository has no CI, is the operator's call on one task, like authorizing a release.
OPERATOR_ONLY_DECISION_KINDS: frozenset[str] = frozenset({"release_authorization", *WAIVER_KINDS})
