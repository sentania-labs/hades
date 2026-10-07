"""FDY-0514 / Issue 373: model-only refusal excludes only the model, not the pool."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Mapping
from unittest.mock import MagicMock

import pytest

from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.base import patterns
from crucible.contracts.completion_claim import ExitInfo
from crucible.domain.entities import Attempt, TaskState, PoolExhaustion
from crucible.domain.exit_class import ExitClass
from crucible.ports.harness import CredentialSpec, LaunchContext, MountMode, HarnessAdapter


# ---------------------------------------------------------------------------
# 1. QUOTA_PATTERNS contains "model_requires_usage_credits"
# ---------------------------------------------------------------------------


class TestQuotaPatterns:
    """QUOTA_PATTERNS must match the model_requires_usage_credits signal."""

    def test_model_requires_usage_credits_matches(self) -> None:
        """model_requires_usage_credits is in QUOTA_PATTERNS, so a line containing
        it should classify as QUOTA_EXHAUSTED when passed through the
        classify_with_patterns path."""
        adapter = ClaudeCodeAdapter()
        exit_info = ExitInfo(exit_code=1)
        exit_class = adapter.classify_exit(
            exit=exit_info,
            stdout_tail="model_requires_usage_credits",
            stderr_tail="",
            report_dir=None,
        )
        assert exit_class is ExitClass.QUOTA_EXHAUSTED

    def test_model_switch_text_classifies_as_quota(self) -> None:
        """Text telling the user to switch models also classifies as
        QUOTA_EXHAUSTED."""
        adapter = ClaudeCodeAdapter()
        exit_info = ExitInfo(exit_code=1)
        exit_class = adapter.classify_exit(
            exit=exit_info,
            stdout_tail="switch to a different model to proceed",
            stderr_tail="",
            report_dir=None,
        )
        assert exit_class is ExitClass.QUOTA_EXHAUSTED


# ---------------------------------------------------------------------------
# 2. _provider_quota_refusal recognises model_requires_usage_credits
# ---------------------------------------------------------------------------


class TestProviderQuotaRefusal:
    """_provider_quota_refusal detects model_requires_usage_credits from
    structured data."""

    def test_model_requires_usage_credits_structured(self) -> None:
        adapter = ClaudeCodeAdapter()
        event = adapter.provider_quota_event(
            stdout_tail="model_requires_usage_credits",
            stderr_tail="",
            now=datetime(2026, 10, 2, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert event is not None

    def test_plain_rate_limit_event_still_detected(self) -> None:
        """A regular rate_limit_event should still be detected."""
        adapter = ClaudeCodeAdapter()
        event = adapter.provider_quota_event(
            stdout_tail="rate limited",
            stderr_tail="rate_limit_event rejected out_of_credits",
            now=datetime(2026, 10, 2, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert event is not None


# ---------------------------------------------------------------------------
# 3. Model-only refusal: model excluded, pool NOT marked  (AC1)
# ---------------------------------------------------------------------------


class TestModelOnlyRefusal:
    """AC1: model_requires_usage_credits excludes model, leaves pool unmarked."""

    def test_claude_code_model_refusal_is_model_only(self) -> None:
        """Text telling the user to switch models is flagged as model-only."""
        adapter = ClaudeCodeAdapter()
        result = adapter.is_model_refusal(
            "model_requires_usage_credits\nswitch to a different model",
            "",
        )
        assert result is True

    def test_claude_code_account_level_not_model_only(self) -> None:
        """Account-level out_of_credits is NOT model-only."""
        adapter = ClaudeCodeAdapter()
        result = adapter.is_model_refusal(
            "rate_limit_event rejected out_of_credits",
            "",
        )
        assert result is False


# ---------------------------------------------------------------------------
# 4. Pool mark from account-level signal  (AC2)
# ---------------------------------------------------------------------------


class TestPoolMarking:
    """AC2: account-level out_of_credits marks the pool."""

    def test_structured_out_of_credits_marks_pool(self) -> None:
        """A structured rate_limit_event with status=rejected and
        overageReason=out_of_credits returns a ProviderQuotaEvent with
        is_account_level=True."""
        adapter = ClaudeCodeAdapter()
        event = adapter.provider_quota_event(
            stdout_tail="",
            stderr_tail=(
                '{"type":"rate_limit_event",'
                '"rate_limit_info":{"status":"rejected",'
                '"overageReason":"out_of_credits"}}'
            ),
            now=datetime(2026, 10, 2, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert event is not None
        assert event.is_account_level is True
        assert event.model_only_refusal is False


# ---------------------------------------------------------------------------
# 5. Pool mark reset capped at default_cooldown_seconds  (AC3)
# ---------------------------------------------------------------------------


class TestPoolMarkCap:
    """AC3: pool mark reset_at capped at default_cooldown_seconds unless
    an explicit reset is given."""

    def test_pool_mark_capped_by_cooldown(self) -> None:
        """When reset_at is explicitly provided and exceeds default_cooldown,
        the supervisor caps it.  We verify the cap formula:
        capped = min(reset_at, now + default_cooldown_seconds).

        _bounded_quota_reset applies the cap.
        """
        from crucible.application.supervisor import Supervisor

        now = datetime(2026, 10, 2, 10, 0, 0, tzinfo=timezone.utc)
        explicit_reset = now + timedelta(hours=4)

        reset, parsed = Supervisor._bounded_quota_reset(
            now,
            explicit_reset,
            max_seconds=86400,
            default_seconds=60,
        )

        # The cap is 60 seconds (default_cooldown_seconds).
        assert reset == now + timedelta(seconds=60)
        # The parsed value preserves the original.
        assert parsed == explicit_reset


# ---------------------------------------------------------------------------
# 6. Pool exhaustion wake fires for every pool mark  (AC4)
# ---------------------------------------------------------------------------


class TestPoolExhaustionWake:
    """AC4: every pool mark raises a wake with pool, reason, reset time."""

    def test_create_pool_exhausted_wake_on_pool_mark(self) -> None:
        """When the pool is marked, _wake_pool_exhausted is called which
        creates a wake via create_pool_exhausted_wake in wakes.py."""
        from crucible.application.wakes import (
            create_pool_exhausted_wake,
        )

        now = datetime(2026, 10, 2, 10, 0, 0, tzinfo=timezone.utc)
        uow = MagicMock()
        uow.pool_exhaustions.list_all.return_value = []

        clock = MagicMock()
        clock.now.return_value = now

        task = MagicMock()
        task.id = "task-1"
        task.principal_id = "user-1"

        mark = PoolExhaustion(
            pool="anthropic-sub",
            exhausted_at=now,
            reset_at=now + timedelta(minutes=30),
            task_id="task-1",
            attempt_id="att-1",
            reason="quota_exhausted",
            cleared_at=None,
        )

        supervisor = Supervisor(clock, lambda: MagicMock())

        supervisor._wake_pool_exhausted(uow, task, MagicMock(), mark)

        # Verify that a wake was created with the correct pool name, reason
        # and reset time.
        uow.wakes.create.assert_called_once()
        call_kwargs = uow.wakes.create.call_args.kwargs
        payload = call_kwargs.get("payload", {})
        assert payload.get("pool") == "anthropic-sub"
        assert payload.get("reason") == "quota_exhausted"
        assert "reset_at" in payload
