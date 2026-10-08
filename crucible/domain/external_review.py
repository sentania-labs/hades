"""External review as cycles, components, and completed rounds (23, ADR 0008).

Pure logic. A *round* is one completed review cycle on a published head, never an
individual signal: a cycle carries the components the policy expects and completes only
when every one of them has a terminal result. The provider does not label its signals by
component, so the rules are stated once here and tested on their own:

- a review object or a review comment attaches to the component its body names, else to
  `code`;
- a reaction-only clean result (`+1` from an allowlisted login with no review object in
  the cycle) completes every configured component at once, because the provider emits
  one combined verdict;
- a reaction carries no commit id, so its head binding is inferred from the PR head at
  the reaction's `created_at` against the head history, and it is recorded as inferred.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

DEFAULT_COMPONENTS: tuple[str, ...] = ("code",)
DEFAULT_REVIEWER_LOGINS: tuple[str, ...] = ("chatgpt-codex-connector[bot]",)
CLEAN_REACTION = "+1"
PICKUP_REACTION = "eyes"
# 23: "the bot's summary comment is edited in place and never counts as a round; the
# review object, its comments, or the bot's no-findings comment do". The summary comment
# arrives within about ten seconds of the pull request opening, minutes before the
# verdict, and is then edited (S12). Counting it as a round completes the cycle before
# any review exists, so a comment is not a round-completing signal by default.
DEFAULT_ACCEPTED_SIGNALS: frozenset[str] = frozenset({"reaction:+1", "review"})
# The HTML marker the provider puts at the top of that summary comment (S12, PR #1 on
# the live record). A comment carrying it is never a round, even where a policy has
# deliberately added `comment` to `accepted_signals`.
SUMMARY_MARKER = "codex-pull-request-review-summary"
# hades #343: the connector declines a round under more than one wording ("create a
# Codex account and connect to github", "create an environment for this repo"), and has
# changed wording before. Matched by this common prefix rather than either exact
# sentence, so a wording neither Hades nor the issue has seen yet is still caught.
CODEX_REFUSAL_PREFIX = "to use codex here"


class SignalKind(StrEnum):
    REVIEW = "review"
    COMMENT = "comment"
    REACTION = "reaction"


class CycleState(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class Signal:
    """One thing an identity did on the PR, as Crucible observed it."""

    kind: SignalKind
    login: str
    github_id: str
    created_at: datetime
    body: str = ""
    reviewed_sha: str | None = None
    content: str = ""
    has_findings: bool = False

    @property
    def is_clean_reaction(self) -> bool:
        return self.kind is SignalKind.REACTION and self.content == CLEAN_REACTION


@dataclass(slots=True)
class Cycle:
    """One review cycle: the components the policy expects on one published head."""

    id: str
    head_sha: str
    components: tuple[str, ...]
    opened_at: datetime
    state: CycleState = CycleState.OPEN
    completed_components: dict[str, str] = field(default_factory=dict)
    completed_at: datetime | None = None

    @property
    def outstanding(self) -> tuple[str, ...]:
        return tuple(c for c in self.components if c not in self.completed_components)

    @property
    def complete(self) -> bool:
        return not self.outstanding


def configured_components(policy: dict[str, object]) -> tuple[str, ...]:
    section = policy.get("external_review")
    raw = section.get("components") if isinstance(section, dict) else None
    if not raw or not isinstance(raw, list):
        return DEFAULT_COMPONENTS
    out = tuple(str(c) for c in raw if str(c))
    return out or DEFAULT_COMPONENTS


def reviewer_logins(policy: dict[str, object]) -> frozenset[str]:
    section = policy.get("external_review")
    raw = section.get("reviewer_logins") if isinstance(section, dict) else None
    if not raw or not isinstance(raw, list):
        return frozenset(DEFAULT_REVIEWER_LOGINS)
    return frozenset(str(login) for login in raw)


def accepted_signals(policy: dict[str, object]) -> frozenset[str]:
    section = policy.get("external_review")
    raw = section.get("accepted_signals") if isinstance(section, dict) else None
    if not raw or not isinstance(raw, list):
        return DEFAULT_ACCEPTED_SIGNALS
    return frozenset(str(s) for s in raw)


def is_provider_summary(body: str) -> bool:
    """The provider's own summary comment, by the marker it writes into the body (S12)."""
    return SUMMARY_MARKER in body.lower()


def is_codex_refusal(body: str) -> bool:
    """A chatgpt-codex-connector reply declining the round, not a review (hades #343).

    Posted as a plain issue comment, seconds after the App's trigger, instead of a
    review object or the pre-verdict summary. Matched by its common prefix so both
    wordings seen on sentania-labs/hades#343 ("...create a Codex account and connect
    to github" and "...create an environment for this repo") are caught by the one
    check; never counted as a comment needing a disposition, and never a signal that
    can complete a cycle."""
    return body.strip().lower().startswith(CODEX_REFUSAL_PREFIX)


def required_rounds(policy: dict[str, object]) -> int:
    section = policy.get("external_review")
    if not isinstance(section, dict):
        return 1
    return int(section.get("required_rounds", 1))


def is_accepted(signal: Signal, policy: dict[str, object]) -> bool:
    """23: a round counts only from an allowlisted login, and only for a signal shape the
    policy accepts. Everything else is recorded and satisfies nothing."""
    if signal.login not in reviewer_logins(policy):
        return False
    allowed = accepted_signals(policy)
    if signal.kind is SignalKind.REACTION:
        return f"reaction:{signal.content}" in allowed
    if signal.kind is SignalKind.COMMENT and is_provider_summary(signal.body):
        # 23: the summary comment is edited in place and never counts as a round. It is
        # recorded like any other comment; it just cannot complete a cycle, even under a
        # policy that accepts comments generally.
        return False
    return signal.kind.value in allowed


def component_for(signal: Signal, components: Sequence[str]) -> tuple[str, ...]:
    """Which components this signal completes.

    GitHub labels nothing by component, so a review object or comment attaches to the
    component its body names if any and to `code` otherwise; a clean reaction completes
    every configured component at once."""
    if signal.is_clean_reaction:
        return tuple(components)
    lowered = signal.body.lower()
    named = tuple(c for c in components if c.lower() in lowered)
    if named:
        return named
    return ("code",) if "code" in components else (components[0],) if components else ()


def head_at(
    heads: Sequence[tuple[str, datetime]], moment: datetime, *, fallback: str = ""
) -> tuple[str, bool]:
    """The PR head at a moment, from the head history, and whether it is inferred.

    Used only for a reaction, which carries no commit id (S12). Returns the newest head
    observed at or before the moment; before the first entry it is the fallback."""
    ordered = sorted(heads, key=lambda item: item[1])
    chosen = ""
    for sha, observed_at in ordered:
        if observed_at <= moment:
            chosen = sha
    return (chosen or fallback, True)


def apply_signal(
    cycle: Cycle, signal: Signal, *, at: datetime
) -> tuple[Cycle, tuple[str, ...], bool]:
    """Attach an accepted signal to an open cycle.

    Returns the cycle, the components this signal completed, and whether the cycle
    completed as a result. A signal with findings still completes its component: the
    cycle has a terminal result for it, and the findings become dispositions."""
    if cycle.state is not CycleState.OPEN:
        return cycle, (), False
    completed: list[str] = []
    for component in component_for(signal, cycle.components):
        if component in cycle.components and component not in cycle.completed_components:
            cycle.completed_components[component] = signal.github_id
            completed.append(component)
    just_completed = False
    if cycle.complete and cycle.state is CycleState.OPEN:
        cycle.state = CycleState.COMPLETED
        cycle.completed_at = at
        just_completed = True
    return cycle, tuple(completed), just_completed


def completed_rounds(cycles: Sequence[Cycle]) -> int:
    """05b `round_counting: completed_cycles`: rounds are completed cycles, per PR,
    across heads, never individual signals."""
    return sum(1 for cycle in cycles if cycle.state is CycleState.COMPLETED)


def final_sha_satisfied(
    cycles: Sequence[Cycle], signals: Sequence[Signal], accepted_head: str
) -> tuple[bool, str]:
    """`require_review_on_final_sha`: the last accepted signal must name the accepted head.

    A reaction names no SHA, so a reaction-only result cannot satisfy this (S12); the
    detail says so rather than letting an inferred binding stand in for a field."""
    named = [s for s in signals if s.kind is not SignalKind.REACTION and s.reviewed_sha]
    if not named:
        if any(s.kind is SignalKind.REACTION for s in signals):
            return False, (
                "the only accepted signal is a reaction, which carries no commit id; "
                "require_review_on_final_sha needs a review object (23)"
            )
        return False, "no accepted signal names a reviewed SHA"
    latest = max(named, key=lambda s: s.created_at)
    if latest.reviewed_sha != accepted_head:
        return False, (
            f"the last accepted signal names {latest.reviewed_sha}, not the accepted head "
            f"{accepted_head}"
        )
    return True, f"the last accepted signal names the accepted head {accepted_head}"
