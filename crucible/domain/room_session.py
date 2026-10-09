"""What a room's runner is told when it starts (hades #208): one text, assembled the
same way for every room kind.

In order: the identity (config/principal/IDENTITY.md), the room itself, the card for a
card room, what the shared memory recalls for the room's subject, the ledger decisions
that touch it, a rolling summary of the room's older turns, and the room's last N turns
verbatim. The subject is the card's title and objective for a card room, and the text
of the last turns for the principal room. A runner always starts a new harness session
from this text: the transcript is Hades's, so a switch or an idle reclaim replays it
rather than resuming a session the old runner held.

Nothing here does I/O; the application reads the record and hands it in."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from crucible.domain.entities import LedgerDecision, MemoryItem
from crucible.domain.memory import subject_keywords
from crucible.domain.rooms import LOCAL_ZONE, Room, RoomKind, RoomTurn, TurnRole, local_time

# The room's own last turns, verbatim, at session start.
VERBATIM_TURNS = 20
# Older turns are summarized one line each, the newest of them, at most this many lines.
SUMMARY_LINES = 40
SUMMARY_LINE_CHARS = 200
# How many of the last turns make the principal room's subject, and how long it may be
# (GET /v1/memory takes a subject of at most 1024 characters).
SUBJECT_TURNS = 6
SUBJECT_MAX_CHARS = 1024
RECALL_LIMIT = 20
DECISION_LIMIT = 20

_SPEAKER = {TurnRole.USER: "Operator", TurnRole.ASSISTANT: "Hades", TurnRole.SYSTEM: "System"}


@dataclass(frozen=True, slots=True)
class CardContext:
    """The card a card room is about, as the session start shows it."""

    task_id: str
    external_id: str
    title: str
    project: str
    state: str
    objective: str


@dataclass(frozen=True, slots=True)
class SessionStart:
    """The parts, each its own text so a test can read one alone, and the whole."""

    identity: str
    room: str
    card: str
    recall: str
    decisions: str
    summary: str
    turns: str
    subject: str
    sections: tuple[str, ...] = field(default=())

    @property
    def text(self) -> str:
        return "\n\n".join(part for part in self.sections if part.strip()) + "\n"


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def subject_for(room: Room, card: CardContext | None, turns: Sequence[RoomTurn]) -> str:
    """What the recall and the decision match ask about: the card's title and objective
    for a card room; for the principal room, the words of its last turns (user and
    assistant, newest last), clipped to what GET /v1/memory takes."""
    if room.kind is RoomKind.CARD and card is not None:
        text = f"{card.title} {card.objective}"
    else:
        spoken = [t.text for t in turns if t.role is not TurnRole.SYSTEM and t.text.strip()]
        text = " ".join(spoken[-SUBJECT_TURNS:])
    flat = " ".join(text.split())
    return flat[-SUBJECT_MAX_CHARS:] if len(flat) > SUBJECT_MAX_CHARS else flat


def decisions_touching(
    decisions: Iterable[LedgerDecision],
    *,
    names: Sequence[str],
    subject: str,
    limit: int = DECISION_LIMIT,
) -> list[LedgerDecision]:
    """The ledger lines that apply to one of `names` (the room, its card, the tasks it
    filed) or whose words carry one of the subject's words, newest first."""
    wanted = {name for name in names if name}
    keywords = subject_keywords(subject)
    found = [
        d
        for d in decisions
        if wanted.intersection(d.applies_to) or any(k in d.verbatim.lower() for k in keywords)
    ]
    found.sort(key=lambda d: (d.said_at, d.id), reverse=True)
    return found[:limit]


def room_section(room: Room, card: CardContext | None) -> str:
    what = (
        f"a card room about {card.external_id} ({card.title})"
        if room.kind is RoomKind.CARD and card is not None
        else "the principal room"
    )
    return (
        "## This room\n"
        f"Room {room.id} is {what}. It runs on {room.harness} with model {room.model}. "
        f"Hades keeps this room's transcript; times here are {LOCAL_ZONE} local time. "
        "Your only tools are the Hades tools. A decision is recorded only when you call "
        "hades_record_decision with the operator's words."
    )


def card_section(card: CardContext | None) -> str:
    if card is None:
        return ""
    return (
        "## The card\n"
        f"{card.external_id}: {card.title}\n"
        f"Project {card.project}, state {card.state}, task id {card.task_id}.\n"
        f"Objective:\n{card.objective.strip()}"
    )


def recall_section(items: Sequence[MemoryItem]) -> str:
    if not items:
        return "## What Hades remembers about this\nNothing in memory matches this subject."
    lines = [
        f"- {item.text.strip()} ({item.source}, observed {local_time(item.observed_at)})"
        for item in items
    ]
    return "## What Hades remembers about this\n" + "\n".join(lines)


def decisions_section(decisions: Sequence[LedgerDecision]) -> str:
    if not decisions:
        return "## Decisions that touch this\nNo recorded decision touches this subject."
    lines = []
    for d in decisions:
        applies = f" Applies to: {', '.join(d.applies_to)}." if d.applies_to else ""
        lines.append(
            f'- {local_time(d.said_at)}, {d.principal} in {d.channel}: "{d.verbatim.strip()}"'
            f"{applies}"
        )
    return "## Decisions that touch this\n" + "\n".join(lines)


def _summary_line(turn: RoomTurn) -> str:
    notes = []
    if turn.tool_calls:
        names = sorted({str(c.get("name", "?")) for c in turn.tool_calls})
        notes.append("called " + ", ".join(names))
    if turn.decision_id:
        notes.append(f"decision {turn.decision_id} recorded")
    if turn.interrupted:
        notes.append("interrupted")
    tail = f" [{'; '.join(notes)}]" if notes else ""
    words = _clip(turn.text, SUMMARY_LINE_CHARS) if turn.text.strip() else "(no words)"
    return f"- #{turn.seq} {_SPEAKER[turn.role]}, {local_time(turn.started_at)}: {words}{tail}"


def rolling_summary(older: Sequence[RoomTurn], *, lines: int = SUMMARY_LINES) -> str:
    """The turns before the verbatim window, one clipped line each, the newest `lines`
    of them, with a count of what is left out. The window moves as the room grows, so
    the summary always covers exactly what the verbatim turns do not."""
    if not older:
        return ""
    ordered = sorted(older, key=lambda t: t.seq)
    shown = ordered[-lines:] if lines > 0 else []
    head = (
        f"## Earlier in this room\n{len(ordered)} earlier turns, from "
        f"{local_time(ordered[0].started_at)} to {local_time(ordered[-1].started_at)}."
    )
    omitted = len(ordered) - len(shown)
    body = [f"({omitted} older turns are not shown.)"] if omitted else []
    body.extend(_summary_line(turn) for turn in shown)
    return head + "\n" + "\n".join(body)


def turns_section(recent: Sequence[RoomTurn]) -> str:
    if not recent:
        return "## The last turns, verbatim\nThis room has no turns yet."
    blocks = []
    for turn in sorted(recent, key=lambda t: t.seq):
        flags = " (interrupted)" if turn.interrupted else ""
        blocks.append(
            f"[#{turn.seq} {_SPEAKER[turn.role]}, {local_time(turn.started_at)}{flags}]\n"
            f"{turn.text}"
        )
    return f"## The last {len(recent)} turns, verbatim\n" + "\n\n".join(blocks)


def split_window(
    turns: Sequence[RoomTurn], window: int = VERBATIM_TURNS
) -> tuple[list[RoomTurn], list[RoomTurn]]:
    """(older, recent): the last `window` turns by seq, and everything before them."""
    ordered = sorted(turns, key=lambda t: t.seq)
    if window <= 0:
        return ordered, []
    return ordered[:-window], ordered[-window:]


def assemble(
    *,
    identity: str,
    room: Room,
    card: CardContext | None,
    recall: Sequence[MemoryItem],
    decisions: Sequence[LedgerDecision],
    turns: Sequence[RoomTurn],
    window: int = VERBATIM_TURNS,
    subject: str | None = None,
) -> SessionStart:
    """The whole session start. `turns` is the room's record; the open assistant turn a
    new runner is about to answer is not in it."""
    older, recent = split_window(turns, window)
    parts = {
        "identity": identity.strip(),
        "room": room_section(room, card),
        "card": card_section(card),
        "recall": recall_section(recall),
        "decisions": decisions_section(decisions),
        "summary": rolling_summary(older),
        "turns": turns_section(recent),
    }
    return SessionStart(
        **parts,
        subject=subject if subject is not None else subject_for(room, card, turns),
        sections=tuple(parts.values()),
    )


__all__ = [
    "DECISION_LIMIT",
    "RECALL_LIMIT",
    "SUBJECT_MAX_CHARS",
    "SUBJECT_TURNS",
    "SUMMARY_LINES",
    "SUMMARY_LINE_CHARS",
    "VERBATIM_TURNS",
    "CardContext",
    "SessionStart",
    "assemble",
    "card_section",
    "decisions_section",
    "decisions_touching",
    "recall_section",
    "rolling_summary",
    "room_section",
    "split_window",
    "subject_for",
    "turns_section",
]
