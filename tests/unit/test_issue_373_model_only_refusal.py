"""Hades #373: a model-only refusal excludes that model, not the whole pool; pool marks
are capped and announced.

2026-10-02 1:36 AM CT, FDY-0256 on claude-fable-5-1: Claude Code answered HTTP 429
`api_error: model_requires_usage_credits` with "You're out of usage credits. Switch to
another model to continue." Hades marked pool anthropic-sub exhausted for two days and
every Claude candidate was turned away, while an Opus attempt ran through the mark. The
operator's confirmation (2026-10-02 10:30 AM CT): the reset was right for that model, the
scope was wrong; the mark should have excluded claude-fable-5-1 and left Opus and Sonnet
eligible.

Now (07, 16): a refusal whose words name the model's own credit requirement or tell the
user to switch models excludes that model until the refusal's reset, or the pool's
`default_cooldown_seconds` when it states none, and the task reroutes to the next
candidate in the same pool through the `excluded_routes` path. Only the account's own
refusal, the `rate_limit_event` with status "rejected", marks the pool; a mark from a
signal with no stated reset expires at the default cooldown; and every pool mark that
opens an exhaustion raises the one wake naming the pool, the reason and the reset.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from crucible.adapters.harness.claude_code import ClaudeCodeAdapter, _provider_quota_refusal
from crucible.application.routing import model_mark_key
from crucible.application.supervisor import Supervisor, _Pending
from crucible.application.wakes import pool_exhausted_summary
from crucible.contracts.policy import RoutingPolicyV1
from crucible.domain.entities import AttemptMetrics
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
from tests.unit.test_routing_preference import _document as _preference_document

FABLE = "claude-fable-5-1"
OPUS = "claude-opus-5-5"
CODEX_FALLBACK = "gpt-5-codex"
POOL = "anthropic-sub"
COOLDOWN = 18000
ACCOUNT_REASON = "harness reported quota_exhausted"
MODEL_REASON = "harness reported quota_exhausted for this model only"
# The issue's own words for the refusal.
REFUSAL_TEXT = "You're out of usage credits. Switch to another model to continue."


def _lines(*documents: dict[str, Any]) -> str:
    return "".join(json.dumps(document) + "\n" for document in documents)


def _window_event(status: str = "allowed", **info: Any) -> dict[str, Any]:
    """The `rate_limit_event` the CLI (2.1.280) writes on every run. With extra usage
    switched off for the account it says `overageStatus` rejected and
    `overageDisabledReason` out_of_credits beside the window's own status, which is
    "allowed" on an ordinary run (observed 2026-10-08) and "rejected" when the window is
    used up (the C5b live sample, 2026-09-17)."""
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": status,
            "rateLimitType": "five_hour",
            "overageStatus": "rejected",
            "overageDisabledReason": "out_of_credits",
            "isUsingOverage": False,
            **info,
        },
    }


def _fable_refusal(reset: str | None = None) -> str:
    """The Fable launch's stream: the ordinary window event (with the weekly window's
    reset, which is not the refusal's), the CLI's synthetic message carrying the 429,
    then its error result."""
    synthetic: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "model": "<synthetic>",
            "content": [
                {
                    "type": "text",
                    "text": (
                        'API Error: 429 {"type":"error","error":{"type":"api_error",'
                        '"message":"model_requires_usage_credits"}}. ' + REFUSAL_TEXT
                    ),
                }
            ],
        },
        "error": "api_error",
    }
    if reset is not None:
        synthetic["resetsAt"] = reset
    result = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "terminal_reason": "api_error",
    }
    return _lines(
        _window_event(resetsAt=1791702000, unifiedWindows={"seven_day": {"utilization": 0.9}}),
        synthetic,
        result,
    )


def _account_refusal(reset: int | None = None) -> str:
    """The account's refusal as the CLI emits it when the window is used up (C5b)."""
    info = {} if reset is None else {"resetsAt": reset}
    return _lines(
        _window_event("rejected", **info),
        {"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "api_error"},
    )


def _exit(code: int = 1) -> ExitInfo:
    return ExitInfo(exit_code=code, report_present=False, blocked_present=False)


def _claude_on_anthropic_sub(
    monkeypatch: pytest.MonkeyPatch, models: list[dict[str, Any]] | None = None
) -> tuple[Any, Any, Any, list[Any], dict[str, Any]]:
    """A running Claude Code attempt on claude-fable-5-1 in anthropic-sub, with Opus in
    the same pool and a Codex fallback in another, and anthropic-sub's seeded five-hour
    default cooldown (05b)."""
    supervisor, pending, uow, attempts = _running(monkeypatch, harness="claude_code")
    routing = _routing(
        models
        if models is not None
        else [
            _model(FABLE, harness="claude_code", pool=POOL),
            _model(OPUS, harness="claude_code", pool=POOL),
            _model(CODEX_FALLBACK, harness="codex", pool="openai-sub"),
        ]
    ).model_dump(mode="json")
    routing["pools"][POOL]["default_cooldown_seconds"] = COOLDOWN
    uow.routing_policies.get.return_value = MagicMock(document=routing)
    pending.execution.model = FABLE
    pending.attempt.selected_model = FABLE
    pending.attempt.selected_harness = "claude_code"
    pending.attempt.selected_pool = POOL
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


def _candidate(selection: Any, model: str) -> dict[str, Any]:
    candidates = selection if isinstance(selection, list) else selection.candidates
    return next(c for c in candidates if c["model"] == model)


# ----- AC1: the Fable refusal excludes Fable until its reset; the pool stays open ------


def test_the_fable_refusal_is_quota_exhausted_and_a_model_only_event() -> None:
    adapter = ClaudeCodeAdapter()
    assert adapter.classify_exit(_exit(), _fable_refusal(), "") is ExitClass.QUOTA_EXHAUSTED
    event = adapter.provider_quota_event(_fable_refusal(), "", now=NOW)
    assert event is not None
    assert event.model_only is True
    # The refusal states no reset; the weekly window's reset on the ordinary event is
    # the account's window, not this model's, and is not read as the refusal's.
    assert event.reset_at is None


def test_the_fable_refusal_excludes_fable_and_leaves_anthropic_sub_unmarked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, marks = _claude_on_anthropic_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _fable_refusal())
    assert pending.attempt.exit_class is ExitClass.QUOTA_EXHAUSTED
    assert POOL not in marks
    mark = marks[model_mark_key(FABLE, "claude_code")]
    assert mark.reset_at == NOW + timedelta(seconds=COOLDOWN)
    assert mark.attempt_id == pending.attempt.id
    assert mark.reason == MODEL_REASON
    assert not any(e.kind == EventKind.POOL_EXHAUSTED.value for e in _events(uow))
    excluded = next(e for e in _events(uow) if e.kind == EventKind.QUOTA_EXHAUSTED.value)
    assert excluded.payload["scope"] == "model"
    assert excluded.payload["model"] == FABLE
    assert excluded.payload["pool"] == POOL
    assert excluded.payload["reset_at"] == mark.reset_at.isoformat()
    assert excluded.payload["source"] == "policy_default_cooldown"


def test_an_opus_candidate_launches_next_in_the_same_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, attempts, _marks = _claude_on_anthropic_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _fable_refusal())
    assert pending.task.state is TaskState.SCHEDULED
    assert len(attempts) == 2
    nxt = attempts[-1]
    assert nxt.routing_excluded_pools == []
    rerouted = next(e for e in _events(uow) if e.kind == EventKind.TASK_REROUTED.value)
    assert rerouted.payload["from_pool"] == POOL
    assert rerouted.payload["to_attempt_id"] == nxt.id
    assert rerouted.payload["excluded_model"] == FABLE
    assert rerouted.payload["excluded_harness"] == "claude_code"
    assert rerouted.payload["next_attempt_id"] == nxt.id
    assert rerouted.payload["why"] == (
        "previous model refused this model only; rerouted within its pool"
    )
    ordered = rerouted.payload["ordered_candidates"]
    assert _candidate(ordered, OPUS)["eligible"]
    assert not _candidate(ordered, FABLE)["eligible"]
    # The next attempt routes as the launch will: Opus, in anthropic-sub, with Fable
    # turned away by its own mark and by the reroute's exclusion.
    selection = _selection(supervisor, uow, pending, nxt)
    assert selection.selected is not None and selection.selected.id == OPUS
    assert selection.selected.pool == POOL
    fable = _candidate(selection, FABLE)
    assert not fable["eligible"]
    assert any(reason.startswith("model excluded until ") for reason in fable["excluded"])
    assert not any("pool exhausted" in reason for reason in fable["excluded"])
    assert _candidate(selection, OPUS)["excluded"] == []


def test_the_fable_exclusion_lifts_at_its_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, attempts, marks = _claude_on_anthropic_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _fable_refusal())
    fresh = replace(attempts[-1], id="a-later-task-attempt")
    before = _selection(supervisor, uow, pending, fresh)
    assert not _candidate(before, FABLE)["eligible"]
    supervisor._clock.advance(COOLDOWN + 1)
    assert marks[model_mark_key(FABLE, "claude_code")].reset_at < supervisor._clock.now()
    after = _selection(supervisor, uow, pending, fresh)
    assert _candidate(after, FABLE)["eligible"]


def test_a_refusal_that_states_a_reset_excludes_fable_until_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, marks = _claude_on_anthropic_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _fable_refusal(reset="2026-09-21T02:00:00Z"))
    assert marks[model_mark_key(FABLE, "claude_code")].reset_at == datetime(
        2026, 9, 21, 2, tzinfo=UTC
    )
    assert POOL not in marks
    excluded = next(e for e in _events(uow) if e.kind == EventKind.QUOTA_EXHAUSTED.value)
    assert excluded.payload["source"] == "harness"


def test_a_model_refusal_that_reaches_the_tail_as_plain_text_excludes_the_model() -> None:
    adapter = ClaudeCodeAdapter()
    assert adapter.classify_exit(_exit(), "", REFUSAL_TEXT) is ExitClass.QUOTA_EXHAUSTED
    event = adapter.provider_quota_event("", REFUSAL_TEXT, now=NOW)
    assert event is not None and event.model_only is True and event.reset_at is None


@pytest.mark.parametrize(
    "words",
    [
        "model_requires_usage_credits",
        "You're out of usage credits.",
        "Please switch to a different model.",
        "Try another model for this request.",
    ],
)
def test_any_error_event_whose_words_say_to_switch_models_is_model_only(words: str) -> None:
    event = {"type": "result", "is_error": True, "result": words}
    assert _provider_quota_refusal(event) is True
    quota = ClaudeCodeAdapter().provider_quota_event(_lines(event), "", now=NOW)
    assert quota is not None and quota.model_only is True


def test_the_ordinary_window_event_with_overage_off_is_not_a_refusal() -> None:
    """The allowed event carries out_of_credits on every run while extra usage is off;
    a failing run with it in the tail is the failure it is, and marks nothing."""
    adapter = ClaudeCodeAdapter()
    assert _provider_quota_refusal(_window_event()) is False
    crash = _lines(_window_event(), {"type": "result", "is_error": True, "result": "tool crashed"})
    assert adapter.classify_exit(_exit(), crash, "") is ExitClass.CRASHED
    assert adapter.provider_quota_event(crash, "", now=NOW) is None


def test_a_model_only_exit_is_read_back_for_the_deferred_checkpoint_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkpoint push decides the reroute later (16 step 1); the exit's verdict is
    the mark it wrote, so the later decision stays inside the pool too."""
    supervisor, pending, uow, _attempts, _marks = _claude_on_anthropic_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _fable_refusal())
    assert supervisor._model_exclusion_for(uow, pending.attempt) == {("claude_code", FABLE)}
    assert supervisor._model_exclusion_for(uow, replace(pending.attempt, id="other")) is None


def test_with_no_other_candidate_the_task_waits_for_the_models_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, marks = _claude_on_anthropic_sub(
        monkeypatch, models=[_model(FABLE, harness="claude_code", pool=POOL)]
    )
    routing = RoutingPolicyV1.model_validate(uow.routing_policies.get.return_value.document)
    _finish(supervisor, pending.attempt, _fable_refusal())
    assert POOL not in marks
    assert pending.task.state is TaskState.AWAITING_QUOTA
    assert pending.task.resume_at == min(
        NOW + timedelta(seconds=COOLDOWN),
        NOW + timedelta(seconds=routing.reroute.resume_max_wait_seconds),
    )


# ----- AC2: an account-level refusal still marks the pool ------------------------------


def test_an_account_level_out_of_credits_refusal_still_marks_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, attempts, marks = _claude_on_anthropic_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _account_refusal())
    assert pending.attempt.exit_class is ExitClass.QUOTA_EXHAUSTED
    assert marks[POOL].attempt_id == pending.attempt.id
    assert marks[POOL].reason == ACCOUNT_REASON
    assert model_mark_key(FABLE, "claude_code") not in marks
    marked = next(e for e in _events(uow) if e.kind == EventKind.POOL_EXHAUSTED.value)
    assert marked.payload["pool"] == POOL
    rerouted = next(e for e in _events(uow) if e.kind == EventKind.TASK_REROUTED.value)
    assert rerouted.payload["why"] == "previous pool reported quota exhaustion"
    assert "excluded_model" not in rerouted.payload
    nxt = attempts[-1]
    assert nxt.routing_excluded_pools == [POOL]
    selection = _selection(supervisor, uow, pending, nxt)
    assert selection.selected is not None and selection.selected.id == CODEX_FALLBACK
    for model in (FABLE, OPUS):
        reasons = _candidate(selection, model)["excluded"]
        assert any(reason.startswith("pool exhausted until ") for reason in reasons)


def test_the_live_window_rejected_sample_is_the_account_refusal() -> None:
    adapter = ClaudeCodeAdapter()
    assert _provider_quota_refusal(_window_event("rejected")) is True
    assert adapter.classify_exit(_exit(), _account_refusal(), "") is ExitClass.QUOTA_EXHAUSTED
    event = adapter.provider_quota_event(_account_refusal(), "", now=NOW)
    assert event is not None and event.model_only is False


# ----- AC3: a pool mark with no stated reset expires at the default cooldown ----------


def test_a_pool_mark_without_a_stated_reset_expires_at_the_default_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, marks = _claude_on_anthropic_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _account_refusal())
    assert marks[POOL].reset_at == NOW + timedelta(seconds=COOLDOWN)
    assert marks[POOL].reset_at <= NOW + timedelta(seconds=COOLDOWN)
    marked = next(e for e in _events(uow) if e.kind == EventKind.POOL_EXHAUSTED.value)
    assert marked.payload["source"] == "policy_default_cooldown"
    assert marked.payload["reset_at"] == marks[POOL].reset_at.isoformat()


def test_a_pool_mark_with_the_signals_own_reset_keeps_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """05b: a reset the account's refusal states is the provider's fact, used as given."""
    supervisor, pending, uow, _attempts, marks = _claude_on_anthropic_sub(monkeypatch)
    stated = NOW + timedelta(days=2)
    _finish(supervisor, pending.attempt, _account_refusal(reset=int(stated.timestamp())))
    assert marks[POOL].reset_at == stated
    marked = next(e for e in _events(uow) if e.kind == EventKind.POOL_EXHAUSTED.value)
    assert marked.payload["source"] == "harness"


def test_the_bound_is_the_pools_default_cooldown() -> None:
    default = timedelta(seconds=60)
    assert Supervisor._bounded_quota_reset(NOW, None, max_seconds=10, default_seconds=60) == (
        NOW + default,
        None,
    )
    past = NOW - timedelta(minutes=1)
    assert Supervisor._bounded_quota_reset(NOW, past, max_seconds=10, default_seconds=60) == (
        NOW + default,
        None,
    )
    ahead = NOW + timedelta(hours=4)
    assert Supervisor._bounded_quota_reset(NOW, ahead, max_seconds=10, default_seconds=60) == (
        ahead,
        ahead,
    )


# ----- AC4: every pool mark raises a wake naming the pool, the reason and the reset ----


def test_a_pool_mark_from_a_reroute_raises_one_wake_naming_pool_reason_and_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, marks = _claude_on_anthropic_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _account_refusal())
    assert _wakes(uow) == 1
    wake = next(e for e in _events(uow) if e.kind == EventKind.WAKE_CREATED.value)
    reset = marks[POOL].reset_at
    assert wake.payload["reason"] == "quota_exhausted"
    assert wake.payload["summary"] == pool_exhausted_summary(POOL, reset, ACCOUNT_REASON)
    assert POOL in wake.payload["summary"]
    assert ACCOUNT_REASON in wake.payload["summary"]
    assert reset.isoformat() in wake.payload["summary"]


def test_a_pool_mark_with_no_candidate_left_says_the_pool_in_its_one_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, marks = _claude_on_anthropic_sub(
        monkeypatch, models=[_model(FABLE, harness="claude_code", pool=POOL)]
    )
    _finish(supervisor, pending.attempt, _account_refusal())
    assert pending.task.state is TaskState.AWAITING_QUOTA
    assert _wakes(uow) == 1
    wake = next(e for e in _events(uow) if e.kind == EventKind.WAKE_CREATED.value)
    sentence = pool_exhausted_summary(POOL, marks[POOL].reset_at, ACCOUNT_REASON)
    assert wake.payload["summary"].startswith(f"{sentence}; ")


def test_a_local_endpoint_mark_raises_the_pool_wake_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0028's mark for a gateway that failed twice goes on to a retry, which raised
    no wake, so Foundry never heard the pool leave routing. Every pool mark is told."""
    supervisor, pending, uow, _attempts, marks = _claude_on_anthropic_sub(monkeypatch)
    document = _preference_document()
    uow.routing_policies.get.return_value = MagicMock(document=document)
    routing = RoutingPolicyV1.model_validate(document)
    pending.execution.model = "coder"
    pending.execution.harness = "hermes"
    pending.attempt.selected_model = "coder"
    pending.attempt.selected_harness = "hermes"
    pending.attempt.selected_pool = "lab-local"
    uow.attempt_metrics.list_since.return_value = [
        AttemptMetrics(
            attempt_id="earlier",
            task_id="t",
            model="coder",
            harness="hermes",
            endpoint_kind="local",
            pool="lab-local",
            exit_class=ExitClass.PROVIDER_ERROR.value,
            created_at=NOW - timedelta(minutes=5),
        )
    ]
    supervisor._mark_local_endpoint_down(uow, pending.attempt, pending.execution)
    reason = "local endpoint failed (provider_error)"
    reset = NOW + timedelta(seconds=routing.pools["lab-local"].default_cooldown_seconds)
    assert marks["lab-local"].reset_at == reset
    assert _wakes(uow) == 1
    wake = next(e for e in _events(uow) if e.kind == EventKind.WAKE_CREATED.value)
    assert wake.payload["reason"] == "quota_exhausted"
    assert wake.payload["summary"] == pool_exhausted_summary("lab-local", reset, reason)


def test_a_model_only_refusal_marks_no_pool_and_raises_no_pool_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _attempts, marks = _claude_on_anthropic_sub(monkeypatch)
    _finish(supervisor, pending.attempt, _fable_refusal())
    assert POOL not in marks
    assert _wakes(uow) == 0
    assert not any(e.kind == EventKind.POOL_EXHAUSTED.value for e in _events(uow))
