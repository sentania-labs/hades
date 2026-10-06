"""Hades #378: AGY's "Individual quota reached ... Resets in 3h52m" result line is the
account's own quota. Before this, `_provider_quota_refusal` matched only
RESOURCE_EXHAUSTED, so that line produced no provider_quota_event, google-sub was never
marked, and the refused attempt went to the pre-PR gates as a crash.

Now the line (and a structured 429) is the authoritative refusal: the reset it states
as a duration becomes `reset_at` counted from the observation, google-sub is marked
exhausted (the pool's default cooldown when no reset is stated), its models leave the
tier's candidates until the reset, the attempt is rerouted, and Foundry gets one wake
naming the pool and the reset time. MODEL_CAPACITY_EXHAUSTED stays a capacity failure.

The messages below are the issue's own words; the ellipsis is the issue's, standing for
the CLI's text between the two phrases, on which nothing here depends.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from crucible.adapters.harness import base
from crucible.adapters.harness.agy import QUOTA_PATTERNS, AgyAdapter, _provider_quota_refusal
from crucible.application.supervisor import _Pending
from crucible.application.wakes import pool_exhausted_summary
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import TaskState
from crucible.ports.harness import ExitInfo
from tests.unit.test_class_routing import NOW, _model, _routing
from tests.unit.test_issue_353_infrastructure_interruptions import (
    _events,
    _finish,
    _running,
    _wakes,
)

# The result line the issue reports (status ERROR, 2026-10-05) and the one AC1 names.
QUOTA_REACHED_3H52M = "Individual quota reached ... Resets in 3h52m"
QUOTA_REACHED_3H56M = "Individual quota reached ... Resets in 3h56m"
CAPACITY = (
    "RESOURCE_EXHAUSTED: MODEL_CAPACITY_EXHAUSTED: The model is currently at capacity. "
    "Please try again later."
)
OBSERVED = datetime(2026, 10, 5, 14, 3, tzinfo=UTC)
GEMINI = "gemini-3.1-pro-high"
FALLBACK = "claude-fallback"


def _tail(error: Any, *, shape: str = "event", status: str = "ERROR") -> str:
    """AGY's stream as the provider collected it: `init`, then the final `result` line,
    in the 1.2.4 `event` shape (found live) or S3's `type` shape."""
    init = {"event": "init", "init": {"model": GEMINI, "session_id": "agy-378"}}
    body = {"status": status, "error": error, "usage": {"input_tokens": 90, "output_tokens": 0}}
    result = {"event": "result", "result": body} if shape == "event" else {"type": "result", **body}
    return json.dumps(init) + "\n" + json.dumps(result) + "\n"


def _exit(code: int = 1) -> ExitInfo:
    return ExitInfo(exit_code=code, report_present=False, blocked_present=False)


# ----- AC1: the result line yields a provider_quota_event with the stated reset -------


def test_quota_reached_result_line_yields_the_event_with_reset_3h56m_after_observation() -> None:
    event = AgyAdapter().provider_quota_event(_tail(QUOTA_REACHED_3H56M), "", now=OBSERVED)
    assert event is not None
    assert event.reset_at == OBSERVED + timedelta(hours=3, minutes=56)


@pytest.mark.parametrize("shape", ["event", "type"])
def test_the_issues_3h52m_line_is_recognised_in_both_stream_shapes(shape: str) -> None:
    event = AgyAdapter().provider_quota_event(
        _tail(QUOTA_REACHED_3H52M, shape=shape), "", now=OBSERVED
    )
    assert event is not None
    assert event.reset_at == OBSERVED + timedelta(hours=3, minutes=52)


def test_the_refusal_is_read_from_stderr_too() -> None:
    event = AgyAdapter().provider_quota_event("", _tail(QUOTA_REACHED_3H56M), now=OBSERVED)
    assert event is not None
    assert event.reset_at == OBSERVED + timedelta(hours=3, minutes=56)


def test_without_an_observation_time_the_reset_counts_from_the_wall_clock() -> None:
    before = datetime.now(UTC)
    event = AgyAdapter().provider_quota_event(_tail(QUOTA_REACHED_3H56M), "")
    after = datetime.now(UTC)
    assert event is not None and event.reset_at is not None
    assert before + timedelta(hours=3, minutes=56) <= event.reset_at
    assert event.reset_at <= after + timedelta(hours=3, minutes=56)


def test_a_structured_429_with_the_message_is_the_refusal_with_its_reset() -> None:
    error = {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": QUOTA_REACHED_3H52M}
    event = AgyAdapter().provider_quota_event(_tail(error), "", now=OBSERVED)
    assert event is not None
    assert event.reset_at == OBSERVED + timedelta(hours=3, minutes=52)


def test_a_structured_429_without_a_reset_leaves_reset_at_to_the_pool_default() -> None:
    error = {"code": 429, "message": "Too Many Requests"}
    event = AgyAdapter().provider_quota_event(_tail(error), "", now=OBSERVED)
    assert event is not None
    assert event.reset_at is None


def test_a_structured_resets_at_key_still_wins_over_the_duration() -> None:
    error = {
        "code": 429,
        "message": QUOTA_REACHED_3H52M,
        "resetAt": "2026-10-05T20:00:00Z",
    }
    event = AgyAdapter().provider_quota_event(_tail(error), "", now=OBSERVED)
    assert event is not None
    assert event.reset_at == datetime(2026, 10, 5, 20, 0, tzinfo=UTC)


def test_the_established_resource_exhausted_line_still_marks_without_a_reset() -> None:
    event = AgyAdapter().provider_quota_event(
        _tail("RESOURCE_EXHAUSTED: Quota exceeded for quota metric"), "", now=OBSERVED
    )
    assert event is not None
    assert event.reset_at is None


@pytest.mark.parametrize(
    "tail",
    [
        _tail("UNAVAILABLE: The service is currently unavailable."),
        _tail(QUOTA_REACHED_3H56M, status="SUCCESS"),
        json.dumps({"event": "assistant", "assistant": {"text": QUOTA_REACHED_3H56M}}) + "\n",
        "Individual quota reached ... Resets in 3h56m\n",
    ],
)
def test_only_the_error_result_line_is_the_authoritative_refusal(tail: str) -> None:
    assert AgyAdapter().provider_quota_event(tail, "", now=OBSERVED) is None


# ----- the exit class: quota, never a crash; capacity stays capacity ------------------


@pytest.mark.parametrize("error", [QUOTA_REACHED_3H52M, QUOTA_REACHED_3H56M])
def test_the_refused_exit_classifies_quota_exhausted(error: str) -> None:
    adapter = AgyAdapter()
    assert adapter.classify_exit(_exit(), _tail(error), "") is ExitClass.QUOTA_EXHAUSTED
    assert adapter.provider_quota_exhausted(_tail(error), "")
    interruption = adapter.interruption(_exit(), _tail(error), "")
    assert interruption is not None and interruption.quota and not interruption.capacity
    assert base.first_match((error,), QUOTA_PATTERNS) == "Individual quota reached"


def test_a_structured_429_without_quota_words_still_classifies_quota() -> None:
    tail = _tail({"code": 429, "message": "resource exhausted for this account"})
    assert AgyAdapter().classify_exit(_exit(), tail, "") is ExitClass.QUOTA_EXHAUSTED


def test_model_capacity_exhausted_stays_a_capacity_failure_and_marks_nothing() -> None:
    adapter = AgyAdapter()
    assert adapter.classify_exit(_exit(), _tail(CAPACITY), "") is ExitClass.INFRASTRUCTURE
    assert adapter.provider_quota_event(_tail(CAPACITY), "", now=OBSERVED) is None
    assert not adapter.provider_quota_exhausted(_tail(CAPACITY), "")
    interruption = adapter.interruption(_exit(), _tail(CAPACITY), "")
    assert interruption is not None and interruption.capacity and not interruption.quota
    assert not _provider_quota_refusal(json.loads(_tail(CAPACITY).splitlines()[-1]))


def test_a_clean_exit_is_never_turned_into_a_refusal_by_its_text() -> None:
    assert AgyAdapter().classify_exit(_exit(0), _tail(QUOTA_REACHED_3H56M), "") in {
        ExitClass.COMPLETED,
        ExitClass.COMPLETED_WITHOUT_REPORT,
    }


# ----- the duration parser ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Resets in 3h52m", timedelta(hours=3, minutes=52)),
        ("Resets in 3h56m", timedelta(hours=3, minutes=56)),
        ("Individual quota reached. Resets in 45m.", timedelta(minutes=45)),
        ("resets in 2h", timedelta(hours=2)),
        ("Resets in 1h 5m 30s", timedelta(hours=1, minutes=5, seconds=30)),
        ("Resets in 90s", timedelta(seconds=90)),
        ("Resets in 1d2h", timedelta(days=1, hours=2)),
        ("quota reset in 3h52m", timedelta(hours=3, minutes=52)),
    ],
)
def test_resets_in_durations_parse(text: str, expected: timedelta) -> None:
    assert base.reset_after(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Resets in a few hours",
        "Resets in 3 hours",
        "3h52m",
        "Resets at 19:56",
        "Resets in 3h52m2026",
        "",
    ],
)
def test_prose_that_is_not_a_duration_does_not_parse(text: str) -> None:
    assert base.reset_after(text) is None


# ----- AC2 and AC3: google-sub marked, its models excluded, the attempt rerouted -------


def _agy_on_google_sub(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Any, Any, list[Any], dict[str, Any]]:
    """A running AGY attempt on google-sub, with one claude_code fallback in the tier and
    google-sub's seeded one-hour default cooldown (05b)."""
    supervisor, pending, uow, attempts = _running(monkeypatch, harness="agy")
    routing = _routing(
        [
            _model(GEMINI, harness="agy", pool="google-sub"),
            _model(FALLBACK, harness="claude_code", pool="anthropic-sub"),
        ]
    ).model_dump(mode="json")
    routing["pools"]["google-sub"]["default_cooldown_seconds"] = 3600
    uow.routing_policies.get.return_value = MagicMock(document=routing)
    pending.execution.model = GEMINI
    pending.attempt.selected_model = GEMINI
    pending.attempt.selected_harness = "agy"
    pending.attempt.selected_pool = "google-sub"
    marks: dict[str, Any] = {}

    def put(mark: Any) -> Any:
        marks[mark.pool] = mark
        return mark

    uow.pool_exhaustions.put.side_effect = put
    uow.pool_exhaustions.get.side_effect = marks.get
    uow.pool_exhaustions.list_all.side_effect = lambda: list(marks.values())
    return supervisor, pending, uow, attempts, marks


def _selection(supervisor: Any, uow: Any, pending: Any, attempt: Any) -> Any:
    # Routing as in tests/unit/test_routing.py: no credential gate on the candidates.
    supervisor._harnesses = None
    return supervisor._selection_for(
        uow, _Pending(attempt, pending.execution, pending.task, pending.contract)
    )


def test_the_refusal_marks_google_sub_until_the_stated_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, marks = _agy_on_google_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _tail(QUOTA_REACHED_3H56M))
    assert pending.attempt.exit_class is ExitClass.QUOTA_EXHAUSTED
    reset = NOW + timedelta(hours=3, minutes=56)
    assert marks["google-sub"].reset_at == reset
    assert marks["google-sub"].attempt_id == pending.attempt.id
    marked = next(e for e in _events(uow) if e.kind == EventKind.POOL_EXHAUSTED.value)
    assert marked.payload == {
        "pool": "google-sub",
        "reset_at": reset.isoformat(),
        "source": "harness",
        "reason": "harness reported quota_exhausted",
    }


def test_a_task_scheduled_before_the_reset_gets_no_gemini_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, attempts, marks = _agy_on_google_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _tail(QUOTA_REACHED_3H56M))
    nxt = attempts[-1]
    assert nxt is not pending.attempt
    selection = _selection(supervisor, uow, pending, nxt)
    assert selection.selected is not None and selection.selected.id == FALLBACK
    gemini = next(c for c in selection.candidates if c["model"] == GEMINI)
    assert not gemini["eligible"]
    assert any("exhausted until" in reason for reason in gemini["excluded"])
    # The exclusion is the mark, not the attempt: it lifts at the reset.
    supervisor._clock.advance(3 * 3600 + 56 * 60 + 1)
    assert marks["google-sub"].reset_at < supervisor._clock.now()
    later = _selection(supervisor, uow, pending, nxt)
    assert next(c for c in later.candidates if c["model"] == GEMINI)["eligible"]


def test_the_refused_attempt_is_rerouted_not_sent_to_the_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, attempts, _marks = _agy_on_google_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _tail(QUOTA_REACHED_3H56M))
    assert pending.task.state is TaskState.SCHEDULED
    assert pending.task.resume_at is None
    assert len(attempts) == 2
    rerouted = next(e for e in _events(uow) if e.kind == EventKind.TASK_REROUTED.value)
    assert rerouted.payload["from_pool"] == "google-sub"
    assert rerouted.payload["from_attempt_id"] == pending.attempt.id
    assert rerouted.payload["to_attempt_id"] == attempts[-1].id
    assert rerouted.payload["why"] == "previous pool reported quota exhaustion"
    supervisor._evaluate_pending_gates()
    uow.gate_results.add.assert_not_called()
    assert not any(e.kind == EventKind.TASK_REPORTED.value for e in _events(uow))
    exited = next(e for e in _events(uow) if e.kind == EventKind.ATTEMPT_EXITED.value)
    assert exited.payload["exit_class"] == "quota_exhausted"
    assert "Individual quota reached" in exited.payload["interruption_message"]


def test_a_refusal_without_a_stated_reset_uses_the_pools_default_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, marks = _agy_on_google_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _tail({"code": 429, "message": "Too Many Requests"}))
    assert pending.attempt.exit_class is ExitClass.QUOTA_EXHAUSTED
    assert marks["google-sub"].reset_at == NOW + timedelta(seconds=3600)
    marked = next(e for e in _events(uow) if e.kind == EventKind.POOL_EXHAUSTED.value)
    assert marked.payload["source"] == "policy_default_cooldown"
    assert pending.task.state is TaskState.SCHEDULED


def test_one_wake_names_the_pool_and_the_reset_time(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, _attempts, marks = _agy_on_google_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _tail(QUOTA_REACHED_3H56M))
    assert _wakes(uow) == 1
    wake = next(e for e in _events(uow) if e.kind == EventKind.WAKE_CREATED.value)
    reset = NOW + timedelta(hours=3, minutes=56)
    assert wake.payload["reason"] == "quota_exhausted"
    assert wake.payload["summary"] == pool_exhausted_summary(
        "google-sub", reset, "harness reported quota_exhausted"
    )
    assert "google-sub" in wake.payload["summary"]
    assert reset.isoformat() in wake.payload["summary"]
    row = uow.wakes.add.call_args.args[0]
    assert row.task_id == pending.task.id
    assert row.payload["attempt_id"] == pending.attempt.id
    assert row.payload["links"]["routing_usage"] == "/v1/routing/usage"
    assert marks["google-sub"].reset_at == reset


def test_a_refusal_while_the_mark_is_in_force_extends_it_and_wakes_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, marks = _agy_on_google_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _tail(QUOTA_REACHED_3H56M))
    assert _wakes(uow) == 1
    # A second attempt on google-sub refused while the mark is in force: the mark
    # moves to the later reset, and this attempt's reroute raises no second pool wake.
    later = NOW + timedelta(hours=4)
    result = supervisor._mark_pool_exhausted(uow, pending.attempt, pending.execution, later)
    assert result is not None
    mark, opened = result
    assert (mark.reset_at, opened) == (later, False)
    assert marks["google-sub"].reset_at == later
    pending.task.state = TaskState.RUNNING  # the rerouted attempt ran and was refused too
    supervisor._handle_quota_exit(
        uow, pending.task, pending.execution, pending.attempt, pool_mark=result
    )
    assert pending.task.state is TaskState.SCHEDULED
    assert _wakes(uow) == 1
    # Once the reset has passed, a new exhaustion is a new episode with its own wake.
    supervisor._clock.advance(5 * 3600)
    result = supervisor._mark_pool_exhausted(uow, pending.attempt, pending.execution, None)
    assert result is not None and result[1]
    pending.task.state = TaskState.RUNNING
    supervisor._handle_quota_exit(
        uow, pending.task, pending.execution, pending.attempt, pool_mark=result
    )
    assert _wakes(uow) == 2


def test_a_refusal_with_no_reroute_left_says_the_pool_in_its_one_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, _marks = _agy_on_google_sub(monkeypatch)
    uow.routing_policies.get.return_value.document["reroute"]["reroute_max"] = 0
    _finish(supervisor, pending.attempt, _tail(QUOTA_REACHED_3H56M))
    assert pending.task.state is TaskState.REPORTED
    assert _wakes(uow) == 1
    wake = next(e for e in _events(uow) if e.kind == EventKind.WAKE_CREATED.value)
    sentence = pool_exhausted_summary(
        "google-sub", NOW + timedelta(hours=3, minutes=56), "harness reported quota_exhausted"
    )
    assert wake.payload["reason"] == "quota_exhausted"
    assert wake.payload["summary"] == (
        f"{sentence}; attempt 1 ended quota_exhausted with no reroute remaining (reroute_max 0)"
    )


def test_a_refusal_with_no_candidate_left_says_the_pool_in_its_one_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, _marks = _agy_on_google_sub(monkeypatch)
    routing = uow.routing_policies.get.return_value.document
    routing["models"][1]["enabled"] = False
    _finish(supervisor, pending.attempt, _tail(QUOTA_REACHED_3H56M))
    reset = NOW + timedelta(hours=3, minutes=56)
    assert pending.task.state is TaskState.AWAITING_QUOTA
    assert pending.task.resume_at == reset
    assert _wakes(uow) == 1
    wake = next(e for e in _events(uow) if e.kind == EventKind.WAKE_CREATED.value)
    sentence = pool_exhausted_summary("google-sub", reset, "harness reported quota_exhausted")
    assert wake.payload["reason"] == "awaiting_quota"
    assert wake.payload["summary"].startswith(f"{sentence}; all pools for class ")
    assert wake.payload["summary"].endswith(f"Crucible will resume at {reset.isoformat()}")


def test_capacity_exhausted_at_the_supervisor_is_retried_without_a_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, attempts, marks = _agy_on_google_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _tail(CAPACITY))
    assert pending.attempt.exit_class is ExitClass.INFRASTRUCTURE
    assert marks == {}
    uow.pool_exhaustions.put.assert_not_called()
    assert pending.task.state is TaskState.SCHEDULED
    assert pending.task.resume_at == NOW + timedelta(minutes=3)
    assert len(attempts) == 2
    assert _wakes(uow) == 0
