"""Issue 278: a worker in a degenerate command loop is ended as a stall, not at the
time-based stall limit.

Foundry's scoping, 2026-10-05: count identical consecutive commands from the transcript
and stop the attempt at a bounded count with exit class `stalled` and the repeated
command in the reason; on the local route, stop a worker that makes no tool call before
a first-response deadline; record the shape (loop:wait, loop:empty_command,
no_activity) on the attempt. The time-based stall path stays as it was.

These tests drive the supervisor's real observe tick with fake `codex exec --json`
transcripts arriving in its log store; only persistence and the provider are faked.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.application import supervisor as supervisor_module
from crucible.application.harnesses import HarnessRegistry
from crucible.application.supervisor import (
    COMMAND_LOOP_REPEATS,
    LOCAL_FIRST_RESPONSE_SECONDS,
    TERMINATION_STALL,
    Supervisor,
    degenerate_stall,
)
from crucible.domain.entities import (
    Attempt,
    Event,
    Execution,
    ExecutionRole,
    Heartbeat,
    LogChunkRecord,
)
from crucible.domain.events import EventKind
from crucible.domain.exit_class import (
    STALL_LOOP_COMMAND,
    STALL_LOOP_EMPTY_COMMAND,
    STALL_LOOP_WAIT,
    STALL_NO_ACTIVITY,
    ExitClass,
    classify_exit,
    loop_shape,
    shell_body,
)
from crucible.domain.lifecycle import AttemptState, ExecutionState
from crucible.ports.execution import Observation, ObservationState
from crucible.ports.harness import CommandLoopTracker
from tests.fixtures import FakeClock

START = datetime(2026, 10, 5, 14, tzinfo=UTC)
TICK = 5
ACTIVITY_SIGNALS = {"log_advanced", "fs_changed", "command_running", "progress_line"}

# ----- fake transcripts -----------------------------------------------------------------


def line(document: dict[str, Any]) -> str:
    return json.dumps(document) + "\n"


def turn_begins() -> list[str]:
    return [
        line({"type": "thread.started", "thread_id": "t-278"}),
        line({"type": "turn.started"}),
    ]


def command(item_id: str, text: str) -> list[str]:
    item = {"id": item_id, "type": "command_execution", "command": text}
    return [
        line({"type": "item.started", "item": {**item, "status": "in_progress"}}),
        line({"type": "item.completed", "item": {**item, "exit_code": 0, "status": "completed"}}),
    ]


def file_change(item_id: str) -> list[str]:
    item = {"id": item_id, "type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]}
    return [line({"type": "item.completed", "item": item})]


def message(item_id: str, text: str) -> list[str]:
    return [
        line(
            {
                "type": "item.completed",
                "item": {"id": item_id, "type": "agent_message", "text": text},
            }
        )
    ]


Timeline = list[tuple[int, list[str]]]


def loop_of(text: str, times: int, *, every: int = 3, begin: int = 10) -> Timeline:
    timeline: Timeline = [(begin, turn_begins())]
    for n in range(times):
        timeline.append((begin + every * (n + 1), command(f"item_{n}", text)))
    return timeline


# ----- the supervisor, with persistence and the provider faked --------------------------


@dataclass
class Store:
    """The slice of a UnitOfWork the observe tick reads and writes."""

    attempt: Attempt
    execution: Execution
    chunks: list[LogChunkRecord] = field(default_factory=list)
    beats: list[Heartbeat] = field(default_factory=list)
    recorded: list[Event] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.attempts = SimpleNamespace(
            get=lambda attempt_id, for_update=False: (
                self.attempt if attempt_id == self.attempt.id else None
            ),
            save=self._save,
        )
        self.executions = SimpleNamespace(get=lambda _id: self.execution)
        self.contracts = SimpleNamespace(get=lambda *_args: None)
        self.logs = SimpleNamespace(list_for_attempt=self._list_chunks)
        self.heartbeats = SimpleNamespace(
            latest_activity=lambda _id: next(
                (b for b in reversed(self.beats) if b.signal in ACTIVITY_SIGNALS), None
            ),
            latest_signal=lambda _id: self.beats[-1] if self.beats else None,
            append=self.beats.append,
        )
        self.events = SimpleNamespace(
            latest_for_task_kind=lambda _task, kind: next(
                (e for e in reversed(self.recorded) if e.kind == kind), None
            ),
            append=self._append_event,
        )
        self.leases = SimpleNamespace(
            verify_supervisor=lambda *_args: True, upsert_attempt_lease=lambda *_args: None
        )

    def _save(self, attempt: Attempt) -> None:
        self.attempt = attempt

    def _append_event(self, event: Event) -> Event:
        self.recorded.append(event)
        return event

    def _list_chunks(self, _attempt_id: str, *, after_id: int, limit: int) -> list[LogChunkRecord]:
        return [c for c in self.chunks if (c.id or 0) > after_id][:limit]

    def set_fenced_token(self, _token: int) -> None:
        return None

    def commit(self) -> None:
        return None

    def events_of(self, kind: EventKind) -> list[Event]:
        return [e for e in self.recorded if e.kind == kind.value]


@dataclass
class Run:
    supervisor: Supervisor
    store: Store
    clock: FakeClock
    provider: SimpleNamespace
    timeline: Timeline

    @property
    def stopped(self) -> bool:
        return bool(self.provider.terminate.await_args_list)

    async def until_stopped(self, seconds: int) -> int | None:
        """Tick every TICK seconds up to `seconds`, storing each transcript line that is
        due as a log chunk, as a log pull would. Returns when the worker was told to
        drain, in seconds from START, or None."""
        pending = sorted(self.timeline, key=lambda entry: entry[0])
        for second in range(TICK, seconds + 1, TICK):
            self.clock.advance(
                (START + timedelta(seconds=second) - self.clock.now()).total_seconds()
            )
            due = [text for at, lines in pending if at <= second for text in lines]
            pending = [(at, lines) for at, lines in pending if at > second]
            if due:
                content = "".join(due).encode()
                self.store.chunks.append(
                    LogChunkRecord(
                        id=len(self.store.chunks) + 1,
                        attempt_id=self.store.attempt.id,
                        stream="stdout",
                        offset_start=0,
                        offset_end=len(content),
                        ts=self.clock.now(),
                        line_sha256="0" * 64,
                        content=content,
                    )
                )
            await self.supervisor._observe_one(self.store.attempt)
            if self.stopped:
                return second
        return None


def run_for(
    monkeypatch: pytest.MonkeyPatch,
    timeline: Timeline,
    *,
    endpoint: str = "local",
    harness: str = "codex",
) -> Run:
    clock = FakeClock(START)
    attempt = Attempt(
        "attempt-278",
        "execution-278",
        "task-278",
        1,
        AttemptState.RUNNING,
        START,
        handle="worker-278",
        started_at=START,
        timeout_at=START + timedelta(hours=1),
        selected_harness=harness,
        selected_model="local-model",
    )
    execution = Execution(
        "execution-278",
        "task-278",
        ExecutionRole.IMPLEMENT,
        1,
        harness,
        "local-model",
        None,
        "fake",
        "worker:latest",
        {"limits": {"stall_warn_seconds": 300, "stall_fail_seconds": 1800}},
        ExecutionState.ACTIVE,
        2,
        [],
        3600,
        START,
    )
    store = Store(attempt, execution)

    @contextmanager
    def uow_factory() -> Iterator[Store]:
        yield store

    provider = SimpleNamespace(
        name="fake",
        observe=AsyncMock(return_value=Observation(ObservationState.RUNNING)),
        terminate=AsyncMock(),
    )
    supervisor = Supervisor(
        uow_factory,  # type: ignore[arg-type]
        {"fake": provider},
        clock,
        holder="test",
        artifact_store=MagicMock(),
        harnesses=HarnessRegistry([CodexAdapter(), ClaudeCodeAdapter()]),
    )
    supervisor.fenced_token = 1
    monkeypatch.setattr(supervisor, "_execution_provider_name", lambda _attempt: "fake")
    monkeypatch.setattr(supervisor, "_pull_logs", AsyncMock())
    monkeypatch.setattr(supervisor, "_workspace_changed", AsyncMock(return_value=False))
    monkeypatch.setattr(supervisor, "_renew_attempt_lease", MagicMock())
    # The quiet warning (and its wake) is the time-based path's, unchanged here.
    monkeypatch.setattr(supervisor, "_record_stall_warning", MagicMock())
    route = SimpleNamespace(endpoint=endpoint)
    monkeypatch.setattr(
        supervisor_module,
        "load_attempt_routing",
        lambda _uow, _policy, _version: SimpleNamespace(model=lambda _model, _harness: route),
    )
    return Run(supervisor, store, clock, provider, timeline)


# ----- AC1, AC2: a command loop -----------------------------------------------------------


async def test_a_wait_loop_is_stopped_within_a_minute_naming_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = loop_of("/bin/bash -lc wait", 10)
    tenth = timeline[-1][0]
    run = run_for(monkeypatch, timeline)

    stopped = await run.until_stopped(1800)

    assert stopped is not None and stopped <= tenth + 60
    run.provider.terminate.assert_awaited_once()
    assert run.provider.terminate.await_args.args[1] == "drain"
    attempt = run.store.attempt
    assert attempt.termination_reason == TERMINATION_STALL
    assert attempt.stall_shape == STALL_LOOP_WAIT
    assert attempt.termination_detail is not None
    assert "/bin/bash -lc wait" in attempt.termination_detail
    assert f"{COMMAND_LOOP_REPEATS} times in a row" in attempt.termination_detail
    (stalled,) = run.store.events_of(EventKind.WORKER_STALLED)
    assert stalled.payload["stall_shape"] == STALL_LOOP_WAIT
    assert "/bin/bash -lc wait" in stalled.payload["detail"]
    (drain,) = run.store.events_of(EventKind.ATTEMPT_TIMEOUT_DRAIN)
    assert drain.payload["reason"] == "stall"
    assert drain.payload["stall_shape"] == STALL_LOOP_WAIT
    # The drained attempt finishes as the time-based stall does: termination reason
    # `stall` on a timed-out exit is recorded `stalled` when it is collected.
    assert (
        classify_exit(exit_code=143, report_present=False, blocked_present=False, timed_out=True)
        is ExitClass.TIMEOUT
    )


async def test_an_empty_command_loop_is_recorded_as_loop_empty_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = run_for(monkeypatch, loop_of("", 10), endpoint="subscription")

    assert await run.until_stopped(600) is not None
    assert run.store.attempt.stall_shape == STALL_LOOP_EMPTY_COMMAND
    assert run.store.attempt.termination_detail is not None
    assert 'in a row: ""' in run.store.attempt.termination_detail


async def test_a_shell_wrapped_empty_command_is_an_empty_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = run_for(monkeypatch, loop_of("/bin/bash -lc ''", 10))

    assert await run.until_stopped(600) is not None
    assert run.store.attempt.stall_shape == STALL_LOOP_EMPTY_COMMAND


async def test_any_other_command_ten_times_in_a_row_is_a_loop_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = run_for(monkeypatch, loop_of("/bin/bash -lc 'git status'", 10, every=5))

    assert await run.until_stopped(600) is not None
    assert run.store.attempt.stall_shape == STALL_LOOP_COMMAND
    assert run.store.attempt.termination_detail is not None
    assert "git status" in run.store.attempt.termination_detail


async def test_a_loop_is_stopped_on_the_subscription_route_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = run_for(monkeypatch, loop_of("/bin/bash -lc wait", 10), endpoint="subscription")

    assert await run.until_stopped(600) is not None
    assert run.store.attempt.stall_shape == STALL_LOOP_WAIT


async def test_fewer_repeats_than_the_bound_are_not_a_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = loop_of("/bin/bash -lc wait", COMMAND_LOOP_REPEATS - 1)
    timeline.append((200, command("item_other", "/bin/bash -lc 'make test'")))
    run = run_for(monkeypatch, timeline)

    assert await run.until_stopped(600) is None
    assert run.store.attempt.stall_shape is None


async def test_a_command_repeated_around_edits_is_iteration_not_a_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: Timeline = [(5, turn_begins())]
    for n in range(12):
        timeline.append((10 + 10 * n, command(f"item_t{n}", "/bin/bash -lc 'make test'")))
        timeline.append((15 + 10 * n, file_change(f"item_f{n}")))
    run = run_for(monkeypatch, timeline)

    assert await run.until_stopped(600) is None


async def test_a_command_that_edits_through_the_shell_is_iteration_until_it_stops_editing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script that fixes one failure per call shows the log only `command_execution`
    items; the supervisor's own workspace check verifies the edits. Once the edits stop,
    the same command repeated is a loop again, counted from the last edit."""
    timeline: Timeline = [(5, turn_begins())]
    for n in range(30):
        timeline.append((15 + 5 * n, command(f"item_{n}", "/bin/bash -lc ./fix-next.sh")))
    run = run_for(monkeypatch, timeline)
    edits_until = START + timedelta(seconds=100)

    async def files_moved(*_args: object, **_kwargs: object) -> bool:
        return run.clock.now() <= edits_until

    monkeypatch.setattr(run.supervisor, "_workspace_changed", files_moved)

    stopped = await run.until_stopped(600)

    # 18 calls while editing. The reset on the tick that verifies the last edit ends
    # the run before that tick's chunk is fed, so the call in it starts the new run;
    # the loop is the 8th call from there, not at 1800 and not before the edits stop.
    assert stopped is not None and 100 + 5 * (COMMAND_LOOP_REPEATS - 1) <= stopped <= 160
    assert run.store.attempt.stall_shape == STALL_LOOP_COMMAND
    assert run.store.attempt.termination_detail is not None
    assert "./fix-next.sh" in run.store.attempt.termination_detail
    assert f"{COMMAND_LOOP_REPEATS} times in a row" in run.store.attempt.termination_detail


async def test_a_loop_verdict_asks_the_throttled_probe_once_more_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a provider whose workspace is probed no more often than a command renews
    activity, the repeats between two probes may be edits; the verdict asks once more,
    past the throttle, and stands only when the files did not move."""
    run = run_for(monkeypatch, loop_of("/bin/bash -lc ./fix-next.sh", 30, every=5))
    asked: list[bool] = []
    moved = True

    async def throttled(*_args: object, force: bool = False) -> bool:
        asked.append(force)
        return force and moved

    monkeypatch.setattr(run.supervisor, "_workspace_changed", throttled)

    assert await run.until_stopped(100) is None
    assert asked.count(True) >= 1
    assert asked.count(True) < asked.count(False)
    assert run.store.attempt.stall_shape is None

    moved = False
    stopped = await run.until_stopped(600)

    assert stopped is not None
    assert run.store.attempt.stall_shape == STALL_LOOP_COMMAND


async def test_a_forced_workspace_check_asks_the_probe_past_the_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = run_for(monkeypatch, [])
    fingerprints = iter([(1, 1, 1), (2, 2, 2), (3, 3, 3)])
    probe = AsyncMock(side_effect=lambda *_args: next(fingerprints))
    run.provider.activity = probe
    attempt, handle = run.store.attempt, SimpleNamespace(ref="worker-278")
    changed = Supervisor._workspace_changed.__get__(run.supervisor)

    assert await changed(attempt, run.provider, handle) is False
    run.clock.advance(TICK)
    assert await changed(attempt, run.provider, handle) is False
    assert probe.await_count == 1
    assert await changed(attempt, run.provider, handle, force=True) is True
    assert probe.await_count == 2
    run.clock.advance(TICK)
    assert await changed(attempt, run.provider, handle) is False
    assert probe.await_count == 2


async def test_alternating_commands_are_not_identical_consecutive_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: Timeline = [(5, turn_begins())]
    for n in range(12):
        text = "/bin/bash -lc wait" if n % 2 else "/bin/bash -lc 'git status'"
        timeline.append((10 + 5 * n, command(f"item_{n}", text)))
    run = run_for(monkeypatch, timeline)

    assert await run.until_stopped(600) is None


async def test_a_loop_is_found_again_after_a_supervisor_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new supervisor replays the whole stored log, so the count survives a takeover."""
    run = run_for(monkeypatch, loop_of("/bin/bash -lc wait", 10, every=1))
    run.store.chunks.append(
        LogChunkRecord(
            id=1,
            attempt_id=run.store.attempt.id,
            stream="stdout",
            offset_start=0,
            offset_end=0,
            ts=START,
            line_sha256="0" * 64,
            content="".join(t for _, lines in run.timeline for t in lines).encode(),
        )
    )
    run.timeline = []

    assert await run.until_stopped(TICK) == TICK
    assert run.store.attempt.stall_shape == STALL_LOOP_WAIT


# ----- AC2, AC3: no tool call before the first-response deadline -------------------------


async def test_a_local_worker_with_no_tool_call_is_stopped_as_no_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The turn begins two minutes in (the preparer and the image pull are not counted)
    # and the model only talks.
    begun = 120
    timeline: Timeline = [(begun, turn_begins()), (200, message("item_0", "Let me think."))]
    run = run_for(monkeypatch, timeline, endpoint="local")

    stopped = await run.until_stopped(1800)

    assert stopped is not None
    assert (
        begun + LOCAL_FIRST_RESPONSE_SECONDS <= stopped < begun + LOCAL_FIRST_RESPONSE_SECONDS + 60
    )
    attempt = run.store.attempt
    assert attempt.termination_reason == TERMINATION_STALL
    assert attempt.stall_shape == STALL_NO_ACTIVITY
    assert attempt.termination_detail is not None
    assert "no tool call" in attempt.termination_detail
    (stalled,) = run.store.events_of(EventKind.WORKER_STALLED)
    assert stalled.payload["stall_shape"] == STALL_NO_ACTIVITY


async def test_the_first_response_clock_starts_at_turn_started_not_thread_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pinned transcript writes `thread.started` before `turn.started`: the thread
    exists, the model is not yet working. A slow start between the two is not counted."""
    thread, turn = 10, 400
    timeline: Timeline = [
        (thread, [line({"type": "thread.started", "thread_id": "t-278"})]),
        (turn, [line({"type": "turn.started"})]),
        (turn + 30, message("item_0", "Let me think.")),
    ]
    run = run_for(monkeypatch, timeline, endpoint="local")

    stopped = await run.until_stopped(1800)

    assert stopped is not None
    assert turn + LOCAL_FIRST_RESPONSE_SECONDS <= stopped < turn + LOCAL_FIRST_RESPONSE_SECONDS + 60
    assert run.store.attempt.stall_shape == STALL_NO_ACTIVITY


async def test_the_first_response_deadline_is_for_the_local_route_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = run_for(monkeypatch, [(10, turn_begins())], endpoint="subscription")

    assert await run.until_stopped(LOCAL_FIRST_RESPONSE_SECONDS + 120) is None
    assert run.store.attempt.stall_shape is None


async def test_a_local_worker_that_calls_a_tool_is_not_no_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: Timeline = [(10, turn_begins()), (60, command("item_0", "/bin/bash -lc ls"))]
    run = run_for(monkeypatch, timeline, endpoint="local")

    assert await run.until_stopped(LOCAL_FIRST_RESPONSE_SECONDS + 120) is None


async def test_a_harness_that_cannot_say_keeps_the_time_based_limits_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code's tracker is no CommandLoopTracker: its silent run still stalls at
    stall_fail_seconds, recorded with no shape, as before issue 278."""
    run = run_for(monkeypatch, [], endpoint="local", harness="claude_code")

    stopped = await run.until_stopped(1900)

    assert stopped == 1800
    assert run.store.attempt.termination_reason == TERMINATION_STALL
    assert run.store.attempt.stall_shape is None
    (stalled,) = run.store.events_of(EventKind.WORKER_STALLED)
    assert stalled.payload == {"reason": "stall"}


# ----- the pieces ---------------------------------------------------------------------------


def test_the_loop_bound_is_within_the_accepted_range() -> None:
    assert 5 <= COMMAND_LOOP_REPEATS <= 10


@pytest.mark.parametrize(
    ("text", "body", "shape"),
    [
        ("/bin/bash -lc wait", "wait", STALL_LOOP_WAIT),
        ("bash -lc 'wait'", "wait", STALL_LOOP_WAIT),
        ("/bin/sh -c 'wait;'", "wait;", STALL_LOOP_WAIT),
        ("", "", STALL_LOOP_EMPTY_COMMAND),
        ("/bin/bash -lc ''", "", STALL_LOOP_EMPTY_COMMAND),
        ("/bin/bash -lc", "", STALL_LOOP_EMPTY_COMMAND),
        ("/bin/bash -lc 'git status'", "git status", STALL_LOOP_COMMAND),
        ("wait-for-it db:5432", "wait-for-it db:5432", STALL_LOOP_COMMAND),
        (
            "/bin/bash -lc 'echo \"unbalanced",
            "/bin/bash -lc 'echo \"unbalanced",
            STALL_LOOP_COMMAND,
        ),
    ],
)
def test_loop_shapes(text: str, body: str, shape: str) -> None:
    assert shell_body(text) == body
    assert loop_shape(text) == shape


def flags(tracker: CommandLoopTracker) -> tuple[bool, bool]:
    return tracker.responding, tracker.tool_called


def repeats(tracker: CommandLoopTracker) -> tuple[str, int] | None:
    return tracker.repeated


def test_the_codex_tracker_counts_identical_consecutive_commands() -> None:
    tracker = CodexAdapter().command_tracker()
    assert isinstance(tracker, CommandLoopTracker)
    assert repeats(tracker) is None
    assert flags(tracker) == (False, False)
    tracker.feed("stdout", "".join(turn_begins()))
    assert flags(tracker) == (True, False)
    for n in range(3):
        tracker.feed("stdout", "".join(command(f"item_{n}", "/bin/bash -lc wait")))
    assert repeats(tracker) == ("/bin/bash -lc wait", 3)
    assert flags(tracker) == (True, True)
    # A command that only completes (it failed before starting) still counts, once.
    item = {"id": "item_9", "type": "command_execution", "command": "/bin/bash -lc wait"}
    tracker.feed("stdout", line({"type": "item.completed", "item": item}))
    assert repeats(tracker) == ("/bin/bash -lc wait", 4)
    tracker.feed("stdout", "".join(file_change("item_10")))
    assert repeats(tracker) is None
    tracker.feed("stdout", "".join(command("item_11", "/bin/bash -lc wait")))
    assert repeats(tracker) == ("/bin/bash -lc wait", 1)
    assert tracker.running == ()
    # The supervisor's verified workspace change ends the run as the edit item does.
    tracker.feed("stdout", "".join(command("item_12", "/bin/bash -lc wait")))
    assert repeats(tracker) == ("/bin/bash -lc wait", 2)
    tracker.workspace_changed()
    assert repeats(tracker) is None
    tracker.feed("stdout", "".join(command("item_13", "/bin/bash -lc wait")))
    assert repeats(tracker) == ("/bin/bash -lc wait", 1)
    assert flags(tracker) == (True, True)


def test_the_codex_tracker_is_responding_from_turn_started_only() -> None:
    tracker = CodexAdapter().command_tracker()
    assert isinstance(tracker, CommandLoopTracker)
    tracker.feed("stdout", line({"type": "thread.started", "thread_id": "t-278"}))
    assert flags(tracker) == (False, False)
    tracker.feed("stdout", line({"type": "turn.started"}))
    assert flags(tracker) == (True, False)


def test_degenerate_stall_rules() -> None:
    now = START + timedelta(seconds=1000)
    assert (
        degenerate_stall(
            now=now, repeated=("x", 7), tool_called=True, responding_since=START, local=True
        )
        is None
    )
    loop = degenerate_stall(
        now=now, repeated=("x", 8), tool_called=True, responding_since=START, local=False
    )
    assert loop is not None and loop.shape == STALL_LOOP_COMMAND and '"x"' in loop.detail
    long_command = "y" * 1000
    quoted = degenerate_stall(
        now=now, repeated=(long_command, 8), tool_called=True, responding_since=None, local=False
    )
    assert quoted is not None and len(quoted.detail) < 300
    # Not yet responding: the deadline has not started.
    assert (
        degenerate_stall(
            now=now, repeated=None, tool_called=False, responding_since=None, local=True
        )
        is None
    )
    late = START + timedelta(seconds=LOCAL_FIRST_RESPONSE_SECONDS)
    idle = degenerate_stall(
        now=late, repeated=None, tool_called=False, responding_since=START, local=True
    )
    assert idle is not None and idle.shape == STALL_NO_ACTIVITY
    assert (
        degenerate_stall(
            now=late - timedelta(seconds=1),
            repeated=None,
            tool_called=False,
            responding_since=START,
            local=True,
        )
        is None
    )


async def test_a_routing_failure_keeps_command_tracking_and_loop_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = run_for(monkeypatch, loop_of("/bin/bash -lc wait", 10))

    def broken(*_args: object) -> None:
        raise RuntimeError("routing document unreadable")

    monkeypatch.setattr(supervisor_module, "load_attempt_routing", broken)

    assert await run.until_stopped(600) is not None
    assert run.store.attempt.stall_shape == STALL_LOOP_WAIT
