"""Rooms: Hades owns the transcript (hades #208, ADR 0031).

Every turn is written to `room_turns` before a runner sees it. A runner is a cache of
the conversation: started when a message finds none warm, told to stop by a switch, a
close or the idle reclaim, and started again from the record. The services here are the
state machine of crucible.domain.rooms applied to the record:

- the operator's side: create, read, inject a message, interrupt, switch, close;
- the runner's side, authenticated by the room-scoped token Hades mints at each launch:
  the session start, the inbox, and the events of the turn it answers;
- the launch and the stop, which talk to the provider outside any transaction.

Approval detection is not here: a decision is recorded only when the agent calls
`hades_record_decision` (crucible.application.room_tools)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from crucible.application.errors import (
    ConflictError,
    ContractValidationError,
    ForbiddenError,
    NotFoundError,
    RoomRunnerUnavailableError,
    UnauthorizedError,
)
from crucible.application.memory import bounded_limit
from crucible.application.routing import image_for_harness
from crucible.application.runtime_settings import RuntimeValue, resolve, save_scalar
from crucible.application.task_notes import list_notes
from crucible.application.transitions import record_event
from crucible.contracts.rooms import (
    RoomCreateRequest,
    RoomDetail,
    RoomTurnView,
    RoomView,
    RunnerEvent,
    RunnerEventsAccepted,
    RunnerInbox,
    RunnerMessage,
    RunnerSession,
)
from crucible.domain.entities import LedgerDecision, MemoryItem, Principal, Role, Task
from crucible.domain.events import EventKind
from crucible.domain.ids import is_ulid, new_id
from crucible.domain.memory import normalize_tags, recall, subject_keywords
from crucible.domain.room_session import (
    DECISION_LIMIT,
    RECALL_LIMIT,
    VERBATIM_TURNS,
    CardContext,
    SessionStart,
    assemble,
    decisions_touching,
    subject_for,
)
from crucible.domain.rooms import (
    ALLOWED_TOOLS,
    DEFAULT_IDLE_TIMEOUT_MINUTES,
    DISALLOWED_TOOLS,
    MAX_IDLE_TIMEOUT_MINUTES,
    MAX_TOOL_CALLS_PER_TURN,
    TURN_WINDOW_DEFAULT,
    TURN_WINDOW_MAX,
    Room,
    RoomEvent,
    RoomKind,
    RoomState,
    RoomTransitionError,
    RoomTurn,
    TurnRole,
    harness_refusal,
    next_state,
    runner_live,
    switch_text,
)
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork, UnitOfWorkFactory
from crucible.ports.rooms import RoomRunnerLaunch, RoomRunnerLauncher

log = logging.getLogger("crucible.rooms")

RUNNER_TOKEN_PREFIX = "crr_"
SALT_BYTES = 16
SECRET_BYTES = 32
# Who creates rooms and talks in them: Hades (orchestrator) and the operator.
ROOM_WRITER_ROLES = frozenset({Role.ORCHESTRATOR, Role.OPERATOR})
# A launched runner that has not polled the inbox by then is taken as never started
# (an image pull, the `uv run --with claude-agent-sdk` install: about 21 s cold in the
# spike, so ten minutes is generous). A warm runner that has not polled for longer
# than STALE_RUNNER is taken as gone; its long poll returns every POLL_SECONDS.
STARTING_GRACE = timedelta(minutes=10)
POLL_SECONDS = 25
STALE_RUNNER = timedelta(seconds=POLL_SECONDS * 4)
# How often a poll that changes nothing records the runner's heartbeat.
SEEN_EVERY = timedelta(seconds=10)
IDLE_SETTING = "rooms.idle_timeout"
IDLE_FIELD = "minutes"
DEFAULT_EGRESS: tuple[str, ...] = ("api.anthropic.com", "pypi.org", "files.pythonhosted.org")


@dataclass(frozen=True, slots=True)
class RoomConfig:
    """Deployment settings for rooms (the `rooms` section of the settings). `api_url` is
    how a runner reaches Hades from where it runs; with none, no runner is launched.
    `image` overrides the harness's promoted worker image."""

    api_url: str = ""
    image: str | None = None
    egress_hosts: tuple[str, ...] = DEFAULT_EGRESS
    idle_timeout_minutes: int | None = None
    identity_path: str | None = None


@dataclass(slots=True)
class RoomContext:
    """What the API process holds for rooms: the settings and the launcher of the
    provider that runs room runners (None where no provider can)."""

    config: RoomConfig = field(default_factory=RoomConfig)
    launcher: RoomRunnerLauncher | None = None


# ----- the idle timeout setting ------------------------------------------------


def idle_timeout_value(uow: UnitOfWork, seed: int | None = None) -> RuntimeValue:
    """`rooms.idle_timeout_minutes`: how long a warm runner waits for a message before
    it exits and its room goes idle. A saved value wins over the deployment's seed."""
    return resolve(
        uow,
        name=IDLE_SETTING,
        field=IDLE_FIELD,
        seed=seed,
        seed_source="environment",
        default=DEFAULT_IDLE_TIMEOUT_MINUTES,
        applies="next launch",
    )


def idle_timeout(uow: UnitOfWork, seed: int | None = None) -> timedelta:
    value = idle_timeout_value(uow, seed).value
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = DEFAULT_IDLE_TIMEOUT_MINUTES
    return timedelta(minutes=max(1, min(minutes, MAX_IDLE_TIMEOUT_MINUTES)))


def validate_idle_minutes(minutes: int) -> int:
    if not 1 <= int(minutes) <= MAX_IDLE_TIMEOUT_MINUTES:
        raise ContractValidationError(
            f"the room idle timeout is between 1 and {MAX_IDLE_TIMEOUT_MINUTES} minutes"
        )
    return int(minutes)


def save_idle_timeout(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, minutes: int, reason: str
) -> None:
    before = {"minutes": idle_timeout_value(uow).value}
    minutes = validate_idle_minutes(minutes)
    save_scalar(
        uow,
        name=IDLE_SETTING,
        field=IDLE_FIELD,
        value=minutes,
        principal=principal.name,
        reason=reason,
        now=clock.now(),
    )
    record_event(
        uow,
        clock,
        EventKind.ROOM_IDLE_TIMEOUT_UPDATED,
        principal=principal.name,
        payload={"reason": reason, "before": before, "after": {"minutes": minutes}},
    )


# ----- the runner token --------------------------------------------------------


def _digest(salt: bytes, secret: str) -> bytes:
    return hashlib.sha256(salt + secret.encode("utf-8")).digest()


def mint_runner_token(room: Room) -> str:
    """A fresh token for this room's next runner: `crr_<room id>.<secret>`. The room
    keeps the salted digest; the value is returned once and stored nowhere."""
    secret = secrets.token_urlsafe(SECRET_BYTES)
    salt = secrets.token_bytes(SALT_BYTES)
    room.runner_key_salt = salt
    room.runner_key_digest = _digest(salt, secret)
    return f"{RUNNER_TOKEN_PREFIX}{room.id}.{secret}"


def revoke_runner_token(room: Room) -> None:
    room.runner_key_salt = None
    room.runner_key_digest = None


def is_runner_token(token: str) -> bool:
    return token.startswith(RUNNER_TOKEN_PREFIX)


def authenticate_runner(uow: UnitOfWork, room_id: str, token: str) -> Room:
    """The room a runner token belongs to, which must be the room in the path. A token
    of another room is refused (403), and anything else that is not this room's live
    token is not recognized (401). An ordinary bearer token is never a runner token."""
    if not is_runner_token(token):
        raise UnauthorizedError("a room runner token is required")
    named, sep, secret = token[len(RUNNER_TOKEN_PREFIX) :].partition(".")
    if not sep or not is_ulid(named) or not secret:
        raise UnauthorizedError("token not recognized")
    if named != room_id:
        raise ForbiddenError("this runner token belongs to another room")
    room = uow.rooms.get(room_id, for_update=True)
    if room is None or room.runner_key_salt is None or room.runner_key_digest is None:
        raise UnauthorizedError("token not recognized")
    if not hmac.compare_digest(room.runner_key_digest, _digest(room.runner_key_salt, secret)):
        raise UnauthorizedError("token not recognized")
    return room


# ----- views -------------------------------------------------------------------


def room_view(room: Room) -> RoomView:
    return RoomView(
        id=room.id,
        kind=room.kind,
        card_task_id=room.card_task_id,
        harness=room.harness,
        model=room.model,
        state=room.state,
        created_at=room.created_at,
        last_activity_at=room.last_activity_at,
        runner_handle=room.runner_handle,
        session_id=room.session_id,
        created_by=room.created_by,
        scope_task_ids=list(room.scope_task_ids),
    )


def turn_view(turn: RoomTurn) -> RoomTurnView:
    return RoomTurnView(
        id=turn.id,
        room_id=turn.room_id,
        seq=turn.seq,
        role=turn.role,
        text=turn.text,
        tool_calls=[dict(call) for call in turn.tool_calls],
        started_at=turn.started_at,
        ended_at=turn.ended_at,
        interrupted=turn.interrupted,
        decision_id=turn.decision_id,
    )


# ----- the operator's side -----------------------------------------------------


def require_room_writer(principal: Principal) -> None:
    if principal.role not in ROOM_WRITER_ROLES:
        raise ForbiddenError("orchestrator or operator role required")


def require_card_room_owner(principal: Principal, room: Room) -> None:
    """Keep card-room writes aligned with the principal used by its tools."""
    if room.kind is RoomKind.CARD and room.created_by != principal.id:
        raise ForbiddenError("only the principal who created this room may write to it")


def refuse_harness(harness: str) -> None:
    refusal = harness_refusal(harness)
    if refusal is not None:
        raise ConflictError(refusal)


def resolve_task(uow: UnitOfWork, ref: str) -> Task | None:
    """A task by its id, or by its external id when exactly one task has it."""
    task = uow.tasks.get(ref) if is_ulid(ref) else None
    if task is not None:
        return task
    found = list(
        uow.tasks.search(
            state=None,
            project=None,
            repository_id=None,
            external_id=ref,
            updated_since=None,
            after_id=None,
            limit=2,
        )
    )
    return found[0] if len(found) == 1 else None


def get_room(uow: UnitOfWork, room_id: str, *, for_update: bool = False) -> Room:
    room = uow.rooms.get(room_id, for_update=for_update)
    if room is None:
        raise NotFoundError(f"room {room_id} not found")
    return room


def _move(room: Room, event: RoomEvent) -> None:
    try:
        room.state = next_state(room.state, event)
    except RoomTransitionError as exc:
        raise ConflictError(str(exc)) from exc


def _require_open(room: Room) -> None:
    if room.state is RoomState.CLOSED:
        raise ConflictError(f"room {room.id} is closed")


def create_room(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, request: RoomCreateRequest
) -> Room:
    require_room_writer(principal)
    refuse_harness(request.harness)
    card: Task | None = None
    if request.kind is RoomKind.CARD:
        assert request.card_task_id is not None
        card = resolve_task(uow, request.card_task_id)
        if card is None:
            raise NotFoundError(f"task {request.card_task_id} not found")
    now = clock.now()
    room = Room(
        id=new_id(),
        kind=request.kind,
        harness=request.harness,
        model=request.model,
        state=RoomState.IDLE,
        created_at=now,
        last_activity_at=now,
        created_by=principal.id,
        card_task_id=card.id if card is not None else None,
        scope_task_ids=[card.id] if card is not None else [],
    )
    uow.rooms.add(room)
    record_event(
        uow,
        clock,
        EventKind.ROOM_CREATED,
        principal=principal.name,
        task_id=card.id if card is not None else None,
        payload={
            "room_id": room.id,
            "kind": room.kind.value,
            "harness": room.harness,
            "model": room.model,
            "card_task_id": room.card_task_id,
        },
    )
    return room


def room_detail(uow: UnitOfWork, room_id: str, *, window: int | None = None) -> RoomDetail:
    room = get_room(uow, room_id)
    bounded = bounded_limit(window, default=TURN_WINDOW_DEFAULT, maximum=TURN_WINDOW_MAX)
    turns = uow.room_turns.list_for_room(room.id, limit=bounded)
    return RoomDetail(
        room=room_view(room),
        turns=[turn_view(t) for t in turns],
        turns_total=uow.room_turns.count(room.id),
        window=bounded,
    )


def list_rooms(
    uow: UnitOfWork, *, limit: int | None = None, include_closed: bool = True
) -> list[Room]:
    bounded = bounded_limit(limit, default=50, maximum=500)
    return list(uow.rooms.list_recent(limit=bounded, include_closed=include_closed))


def _append(uow: UnitOfWork, room: Room, role: TurnRole, text: str, at: datetime) -> RoomTurn:
    turn = RoomTurn(
        id=new_id(),
        room_id=room.id,
        seq=uow.room_turns.last_seq(room.id) + 1,
        role=role,
        text=text,
        started_at=at,
        ended_at=at if role is not TurnRole.ASSISTANT else None,
    )
    uow.room_turns.add(turn)
    return turn


def _open_turns(uow: UnitOfWork, room: Room) -> list[RoomTurn]:
    """The assistant turns still streaming. An assistant turn is created when its user
    turn is handed out, so it always sits after the inbox cursor."""
    return [
        t
        for t in uow.room_turns.list_for_room(room.id, after_seq=room.inbox_cursor)
        if t.role is TurnRole.ASSISTANT and t.open
    ]


def _pending_user_turns(uow: UnitOfWork, room: Room) -> list[RoomTurn]:
    return [
        t
        for t in uow.room_turns.list_for_room(room.id, after_seq=room.inbox_cursor)
        if t.role is TurnRole.USER
    ]


def _end_open_turns(uow: UnitOfWork, room: Room, at: datetime) -> list[int]:
    """Close every open assistant turn as interrupted: its runner is gone or going."""
    ended = []
    for turn in _open_turns(uow, room):
        turn.ended_at = at
        turn.interrupted = True
        uow.room_turns.save(turn)
        ended.append(turn.seq)
    return ended


@dataclass(frozen=True, slots=True)
class Injected:
    """A user turn written to the record, and whether a runner must be launched."""

    room: Room
    turn: RoomTurn
    launch: bool
    # A runner found gone on the way in, for the caller to remove at the provider.
    stale_handle: str | None = None


def inject_message(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, room_id: str, text: str
) -> Injected:
    """Write the user turn first; then, when no runner is up, move the room to
    `starting` so this request, and only this one, launches it."""
    require_room_writer(principal)
    room = get_room(uow, room_id, for_update=True)
    require_card_room_owner(principal, room)
    _require_open(room)
    now = clock.now()
    stale = reap_gone_runner(uow, clock, room, now=now)
    turn = _append(uow, room, TurnRole.USER, text, now)
    room.last_activity_at = now
    launch = room.state is RoomState.IDLE
    if launch:
        _move(room, RoomEvent.LAUNCH)
    uow.rooms.save(room)
    return Injected(room=room, turn=turn, launch=launch, stale_handle=stale)


def interrupt_room(uow: UnitOfWork, clock: Clock, *, principal: Principal, room_id: str) -> Room:
    """Stop the turn that is running. With a runner up, the runner reads `interrupt`
    on its next poll and ends the turn itself; with none, the turn is ended here."""
    require_room_writer(principal)
    room = get_room(uow, room_id, for_update=True)
    require_card_room_owner(principal, room)
    _require_open(room)
    now = clock.now()
    reap_gone_runner(uow, clock, room, now=now)
    open_turns = _open_turns(uow, room)
    if not open_turns:
        raise ConflictError(f"room {room.id} has no turn running")
    if room.state is RoomState.WARM:
        _move(room, RoomEvent.INTERRUPT)
        room.pending_control = "interrupt"
        ended: list[int] = []
    elif room.state is RoomState.INTERRUPTED:
        ended = []
    else:
        ended = _end_open_turns(uow, room, now)
    room.last_activity_at = now
    uow.rooms.save(room)
    record_event(
        uow,
        clock,
        EventKind.ROOM_INTERRUPTED,
        principal=principal.name,
        payload={
            "room_id": room.id,
            "seqs": [t.seq for t in open_turns],
            "ended_by_hades": ended,
            "state": room.state.value,
        },
    )
    return room


def _stop(uow: UnitOfWork, clock: Clock, room: Room, *, why: str, principal: str) -> str | None:
    """Take the runner away from the record: end its open turns as interrupted, forget
    its token, its handle and its session, and leave the room idle. Returns the handle
    the caller stops at the provider once this commits."""
    handle = room.runner_handle
    had_runner = runner_live(room.state) or handle is not None
    now = clock.now()
    ended = _end_open_turns(uow, room, now)
    if room.state is not RoomState.CLOSED:
        _move(room, RoomEvent.STOP)
    revoke_runner_token(room)
    room.runner_handle = None
    room.session_id = None
    room.pending_control = None
    room.runner_seen_at = None
    if had_runner:
        record_event(
            uow,
            clock,
            EventKind.ROOM_RUNNER_STOPPED,
            principal=principal,
            payload={"room_id": room.id, "handle": handle, "why": why, "interrupted": ended},
        )
    return handle


def switch_room(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    room_id: str,
    harness: str,
    model: str,
) -> tuple[Room, RoomTurn, str | None]:
    """A new harness or model: the system turn says so, the runner stops, and the next
    message starts a new one with the transcript replayed. Same history, same memory."""
    require_room_writer(principal)
    refuse_harness(harness)
    room = get_room(uow, room_id, for_update=True)
    require_card_room_owner(principal, room)
    _require_open(room)
    before = {"harness": room.harness, "model": room.model}
    handle = _stop(uow, clock, room, why="switch", principal=principal.name)
    now = clock.now()
    turn = _append(uow, room, TurnRole.SYSTEM, switch_text(harness, model, now), now)
    room.harness = harness
    room.model = model
    room.last_activity_at = now
    uow.rooms.save(room)
    record_event(
        uow,
        clock,
        EventKind.ROOM_SWITCHED,
        principal=principal.name,
        payload={
            "room_id": room.id,
            "before": before,
            "after": {"harness": harness, "model": model},
            "seq": turn.seq,
        },
    )
    return room, turn, handle


def close_room(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, room_id: str
) -> tuple[Room, str | None]:
    require_room_writer(principal)
    room = get_room(uow, room_id, for_update=True)
    require_card_room_owner(principal, room)
    _require_open(room)
    handle = _stop(uow, clock, room, why="close", principal=principal.name)
    _move(room, RoomEvent.CLOSE)
    room.last_activity_at = clock.now()
    uow.rooms.save(room)
    record_event(
        uow, clock, EventKind.ROOM_CLOSED, principal=principal.name, payload={"room_id": room.id}
    )
    return room, handle


# ----- runners that went away, and the idle reclaim ----------------------------


def runner_gone(room: Room, now: datetime) -> bool:
    """A launched runner that never polled in STARTING_GRACE, or a warm one that has not
    polled in STALE_RUNNER: its Pod died, or was never scheduled."""
    if room.state is RoomState.STARTING:
        since = room.runner_seen_at or room.last_activity_at
        return now - since > STARTING_GRACE
    if room.state in (RoomState.WARM, RoomState.INTERRUPTED):
        return room.runner_seen_at is None or now - room.runner_seen_at > STALE_RUNNER
    return False


def reap_gone_runner(uow: UnitOfWork, clock: Clock, room: Room, *, now: datetime) -> str | None:
    """Stop a runner that went away, so the next message launches a new one."""
    if not runner_gone(room, now):
        return None
    return _stop(uow, clock, room, why="runner went away", principal="crucible")


def idle_expired(room: Room, now: datetime, timeout: timedelta) -> bool:
    return room.state is RoomState.WARM and now - room.last_activity_at >= timeout


@dataclass(frozen=True, slots=True)
class Reclaimed:
    room_id: str
    handle: str | None
    why: str


def reclaim_rooms(
    uow: UnitOfWork, clock: Clock, *, idle_seed: int | None = None
) -> list[Reclaimed]:
    """Every live room whose runner is past the idle timeout with nothing to answer, or
    went away. The caller stops each handle at the provider after this commits."""
    timeout = idle_timeout(uow, idle_seed)
    now = clock.now()
    reclaimed: list[Reclaimed] = []
    for listed in uow.rooms.list_live():
        room = uow.rooms.get(listed.id, for_update=True)
        if room is None or not runner_live(room.state):
            continue
        why = ""
        if runner_gone(room, now):
            why = "runner went away"
        elif (
            idle_expired(room, now, timeout)
            and not _open_turns(uow, room)
            and not _pending_user_turns(uow, room)
        ):
            why = "idle"
        if not why:
            continue
        handle = _stop(uow, clock, room, why=why, principal="crucible")
        uow.rooms.save(room)
        reclaimed.append(Reclaimed(room.id, handle, why))
    return reclaimed


# ----- the runner's side -------------------------------------------------------


def _card_context(uow: UnitOfWork, room: Room) -> CardContext | None:
    if room.card_task_id is None:
        return None
    task = uow.tasks.get(room.card_task_id)
    if task is None:
        return None
    contract = uow.contracts.get(task.id, task.contract_version)
    document = contract.document if contract else {}
    pr = uow.pull_requests.get_for_task(task.id)
    certifications = list(uow.ci_certifications.list_for_task(task.id))
    ci = certifications[-1] if certifications else None
    attempts = uow.attempts.list_for_task(task.id)
    return CardContext(
        task_id=task.id,
        external_id=task.external_id,
        title=task.title,
        project=task.project,
        state=task.state.value,
        objective=task_objective(uow, task),
        acceptance_criteria=tuple(
            f"{criterion.get('id', '')}: {criterion.get('text', '')}"
            for criterion in document.get("acceptance_criteria", [])
        ),
        pull_request=(
            f"{pr.url} ({pr.state.value}), head {pr.head_sha}" if pr else "No pull request yet"
        ),
        ci=(f"{ci.state}: {ci.detail} (head {ci.head_sha})" if ci else "Not certified yet"),
        attempt_references=tuple(
            f"Attempt {attempt.id}: /v1/attempts/{attempt.id}/logs" for attempt in attempts
        ),
        notes=tuple(f"{note.author}: {note.text}" for note in list_notes(uow, task.id)),
    )


def task_objective(uow: UnitOfWork, task: Task) -> str:
    contract = uow.contracts.get(task.id, task.contract_version)
    if contract is None:
        return ""
    return str(contract.document.get("objective") or "")


def _recall_for(uow: UnitOfWork, subject: str, card: CardContext | None) -> list[MemoryItem]:
    """The same recall `GET /v1/memory` makes, for the room's subject. A card room also
    asks for its project's and its card's tags."""
    tags = normalize_tags(
        [f"project:{card.project}", card.project, card.external_id] if card is not None else []
    )
    keywords = subject_keywords(subject)
    rows = uow.memory.recall(tags=tags, keywords=keywords, limit=RECALL_LIMIT)
    return recall(rows, tags=tags, subject=subject, limit=RECALL_LIMIT)


def _decisions_for(
    uow: UnitOfWork, room: Room, card: CardContext | None, subject: str
) -> list[LedgerDecision]:
    names = [room.id, *room.scope_task_ids]
    if card is not None:
        names.append(card.external_id)
    lines = uow.decision_ledger.list_recent(limit=500)
    return decisions_touching(lines, names=names, subject=subject, limit=DECISION_LIMIT)


def read_identity(path: str | None = None) -> str:
    """config/principal/IDENTITY.md: from the configured path, else the working
    directory (the service image's /app), else next to this checkout."""
    candidates = [Path(path)] if path else []
    candidates.append(Path.cwd() / "config" / "principal" / "IDENTITY.md")
    candidates.append(Path(__file__).resolve().parents[2] / "config" / "principal" / "IDENTITY.md")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise RoomRunnerUnavailableError(
        "the principal identity (config/principal/IDENTITY.md) is not readable"
    )


def session_start(
    uow: UnitOfWork, room: Room, *, identity: str, window: int = VERBATIM_TURNS
) -> SessionStart:
    """The session start of crucible.domain.room_session for this room, read from the
    record: the user turns not yet handed out are left for the inbox to deliver."""
    turns = [
        t
        for t in uow.room_turns.list_for_room(room.id)
        if not (t.role is TurnRole.USER and t.seq > room.inbox_cursor)
    ]
    card = _card_context(uow, room)
    subject = subject_for(room, card, turns)
    return assemble(
        identity=identity,
        room=room,
        card=card,
        recall=_recall_for(uow, subject, card),
        decisions=_decisions_for(uow, room, card, subject),
        turns=turns,
        window=window,
        subject=subject,
    )


def runner_session(uow: UnitOfWork, room: Room, *, identity: str, idle: timedelta) -> RunnerSession:
    _require_open(room)
    start = session_start(uow, room, identity=identity)
    return RunnerSession(
        room_id=room.id,
        harness=room.harness,
        model=room.model,
        system_prompt=start.text,
        subject=start.subject,
        allowed_tools=list(ALLOWED_TOOLS),
        disallowed_tools=list(DISALLOWED_TOOLS),
        idle_timeout_seconds=int(idle.total_seconds()),
    )


Control = Literal["interrupt", "stop"]


def _control(room: Room) -> Control | None:
    if room.state is RoomState.CLOSED or not runner_live(room.state):
        return "stop"
    if room.pending_control == "stop":
        return "stop"
    if room.pending_control == "interrupt":
        return "interrupt"
    return None


@dataclass(frozen=True, slots=True)
class InboxAnswer:
    inbox: RunnerInbox
    # Set when this poll reclaimed the room: the handle the caller stops.
    reclaimed_handle: str | None = None
    reclaimed: bool = False

    @property
    def empty(self) -> bool:
        return self.inbox.control is None and self.inbox.message is None


def runner_poll(uow: UnitOfWork, clock: Clock, room: Room, *, idle: timedelta) -> InboxAnswer:
    """One look at the inbox for the runner: `stop` for a room that is not live any
    more or whose idle timeout passed with nothing to answer; `interrupt` once after
    the operator stopped a turn; otherwise the next user turn, with its open assistant
    turn created, when no turn is running. A look that changes nothing writes nothing
    but the heartbeat, and that at most every SEEN_EVERY."""
    now = clock.now()
    dirty = False
    if room.state is RoomState.STARTING:
        _move(room, RoomEvent.RUNNER_READY)
        dirty = True
    if runner_live(room.state) and (
        room.runner_seen_at is None or now - room.runner_seen_at >= SEEN_EVERY
    ):
        room.runner_seen_at = now
        dirty = True

    def answer(inbox: RunnerInbox, **extra: Any) -> InboxAnswer:
        if dirty:
            uow.rooms.save(room)
        return InboxAnswer(inbox, **extra)

    control = _control(room)
    if control == "stop":
        return answer(RunnerInbox(control="stop", message=None))
    if control == "interrupt":
        room.pending_control = None
        dirty = True
        return answer(RunnerInbox(control="interrupt", message=None))
    if _open_turns(uow, room):
        return answer(RunnerInbox(control=None, message=None))
    pending = _pending_user_turns(uow, room)
    if not pending:
        if idle_expired(room, now, idle):
            handle = _stop(uow, clock, room, why="idle", principal="crucible")
            dirty = True
            return answer(
                RunnerInbox(control="stop", message=None), reclaimed_handle=handle, reclaimed=True
            )
        return answer(RunnerInbox(control=None, message=None))
    user = pending[0]
    assistant = _append(uow, room, TurnRole.ASSISTANT, "", now)
    room.inbox_cursor = user.seq
    room.last_activity_at = now
    dirty = True
    return answer(
        RunnerInbox(
            control=None,
            message=RunnerMessage(user_seq=user.seq, assistant_seq=assistant.seq, text=user.text),
        )
    )


def record_runner_events(
    uow: UnitOfWork, clock: Clock, room: Room, *, seq: int, events: Sequence[RunnerEvent]
) -> RunnerEventsAccepted:
    """Apply one batch of the runner's events to the assistant turn `seq`, in order."""
    turn = uow.room_turns.get(room.id, seq, for_update=True)
    if turn is None or turn.role is not TurnRole.ASSISTANT:
        raise NotFoundError(f"room {room.id} has no assistant turn {seq}")
    if not turn.open:
        raise ConflictError(f"turn {seq} of room {room.id} has already ended")
    now = clock.now()
    accepted = 0
    for event in events:
        if turn.ended_at is not None:
            break
        accepted += 1
        if event.kind == "delta" and event.text:
            turn.text += event.text
        elif event.kind == "tool_call":
            if len(turn.tool_calls) < MAX_TOOL_CALLS_PER_TURN:
                turn.tool_calls.append(
                    {
                        "name": event.name or "",
                        "input": dict(event.input or {}),
                        "tool_use_id": event.tool_use_id,
                        "at": now.isoformat(),
                    }
                )
        elif event.kind == "session" and event.session_id:
            room.session_id = event.session_id
        elif event.kind == "end":
            if event.text and not turn.text:
                turn.text = event.text
            turn.ended_at = now
            turn.interrupted = bool(event.interrupted or event.error)
            if event.error:
                log.warning(
                    "room turn ended with an error",
                    extra={"room_id": room.id, "seq": seq, "error": event.error},
                )
            if room.state in (RoomState.WARM, RoomState.INTERRUPTED):
                _move(room, RoomEvent.TURN_ENDED)
                room.pending_control = None
    uow.room_turns.save(turn)
    room.last_activity_at = now
    if runner_live(room.state):
        room.runner_seen_at = now
    uow.rooms.save(room)
    return RunnerEventsAccepted(
        seq=seq, accepted=accepted, ended=not turn.open, control=_control(room)
    )


def runner_exited(uow: UnitOfWork, clock: Clock, room: Room, *, why: str) -> str | None:
    """The runner says it is exiting (its own idle timer, or a failure)."""
    handle = _stop(uow, clock, room, why=f"runner exited: {why}"[:200], principal="crucible")
    uow.rooms.save(room)
    return handle


# ----- the launch and the stop, outside any transaction ------------------------


def _launch_request(
    uow: UnitOfWork, room: Room, ctx: RoomContext, launcher: RoomRunnerLauncher, token: str
) -> RoomRunnerLaunch:
    config = ctx.config
    if not config.api_url:
        raise RoomRunnerUnavailableError(
            "rooms.api_url is not set, so a runner would have no way to reach Hades"
        )
    image = config.image or image_for_harness(uow, room.harness, launcher.name)
    if not image:
        raise RoomRunnerUnavailableError(
            f"no worker image is promoted for {room.harness} on {launcher.name}"
        )
    return RoomRunnerLaunch(
        room_id=room.id,
        harness=room.harness,
        model=room.model,
        image=image,
        api_url=config.api_url,
        idle_timeout_seconds=int(idle_timeout(uow, config.idle_timeout_minutes).total_seconds()),
        egress_hosts=tuple(config.egress_hosts),
        token=token,
    )


def _launch_failed(uow: UnitOfWork, clock: Clock, room_id: str, reason: str) -> None:
    room = uow.rooms.get(room_id, for_update=True)
    if room is None or room.state is not RoomState.STARTING:
        return
    _move(room, RoomEvent.LAUNCH_FAILED)
    revoke_runner_token(room)
    room.runner_handle = None
    uow.rooms.save(room)
    record_event(
        uow,
        clock,
        EventKind.ROOM_RUNNER_STOPPED,
        principal="crucible",
        payload={"room_id": room.id, "handle": None, "why": f"launch failed: {reason}"[:500]},
    )


async def start_runner(
    factory: UnitOfWorkFactory, clock: Clock, ctx: RoomContext, *, room_id: str, principal: str
) -> str:
    """Launch the runner of a room that `inject_message` moved to `starting`: mint its
    token in one transaction, launch outside any, and record the handle in a third. A
    launch that fails puts the room back to idle and raises RoomRunnerUnavailableError;
    the message stays in the record for the next one."""
    launcher = ctx.launcher
    with factory() as uow:
        room = get_room(uow, room_id, for_update=True)
        if room.state is not RoomState.STARTING:
            raise ConflictError(f"room {room.id} is {room.state.value}, not starting")
        if launcher is None:
            _launch_failed(uow, clock, room.id, "no provider here runs room runners")
            uow.commit()
            raise RoomRunnerUnavailableError("no provider here runs room runners")
        token = mint_runner_token(room)
        try:
            request = _launch_request(uow, room, ctx, launcher, token)
        except RoomRunnerUnavailableError as exc:
            _launch_failed(uow, clock, room.id, exc.detail)
            uow.commit()
            raise
        uow.rooms.save(room)
        uow.commit()
    try:
        handle = await launcher.launch(request)
    except Exception as exc:  # the provider's failure, whatever it is (RoomRunnerError)
        log.warning("room runner launch failed", extra={"room_id": room_id, "error": str(exc)})
        with factory() as uow:
            _launch_failed(uow, clock, room_id, str(exc))
            uow.commit()
        raise RoomRunnerUnavailableError(f"the room runner could not be started: {exc}") from exc
    with factory() as uow:
        room = get_room(uow, room_id, for_update=True)
        if room.state is RoomState.STARTING or room.state is RoomState.WARM:
            room.runner_handle = handle
            uow.rooms.save(room)
            record_event(
                uow,
                clock,
                EventKind.ROOM_RUNNER_LAUNCHED,
                principal=principal,
                payload={
                    "room_id": room.id,
                    "handle": handle,
                    "harness": room.harness,
                    "model": room.model,
                    "image": request.image,
                    "provider": launcher.name,
                },
            )
            uow.commit()
            return handle
        uow.commit()
    # The room was switched or closed while the launch ran: the runner it got is not
    # wanted any more.
    await stop_runner(ctx, handle)
    return handle


async def stop_runner(ctx: RoomContext, handle: str | None) -> None:
    """Remove a runner at the provider. Best effort: the record already says it is
    gone, and the token it held is already revoked."""
    if not handle or ctx.launcher is None:
        return
    try:
        await ctx.launcher.stop(handle)
    except Exception as exc:  # a failure to clean up is logged, never raised to a client
        log.warning("room runner stop failed", extra={"handle": handle, "error": str(exc)})


__all__ = [
    "DEFAULT_EGRESS",
    "IDLE_SETTING",
    "POLL_SECONDS",
    "ROOM_WRITER_ROLES",
    "RUNNER_TOKEN_PREFIX",
    "STALE_RUNNER",
    "STARTING_GRACE",
    "InboxAnswer",
    "Injected",
    "Reclaimed",
    "RoomConfig",
    "RoomContext",
    "authenticate_runner",
    "close_room",
    "create_room",
    "get_room",
    "idle_timeout",
    "idle_timeout_value",
    "inject_message",
    "interrupt_room",
    "is_runner_token",
    "list_rooms",
    "mint_runner_token",
    "read_identity",
    "reap_gone_runner",
    "reclaim_rooms",
    "record_runner_events",
    "refuse_harness",
    "require_room_writer",
    "resolve_task",
    "revoke_runner_token",
    "room_detail",
    "room_view",
    "runner_exited",
    "runner_poll",
    "runner_session",
    "save_idle_timeout",
    "session_start",
    "start_runner",
    "stop_runner",
    "switch_room",
    "task_objective",
    "turn_view",
    "validate_idle_minutes",
]
