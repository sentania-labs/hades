"""Hades #37: live log tails have a configured, process-local admission limit."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.requests import Request

from crucible.adapters.api.deps import AppContext, SseTailLimiter
from crucible.adapters.api.routers import records
from crucible.domain.entities import Role
from crucible.settings import Settings


def test_one_tail_over_the_configured_limit_returns_a_problem_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The N+1 SSE request is refused before it can enter the polling loop."""
    principal = SimpleNamespace(id="reader", name="reader", role=Role.OBSERVER)
    attempt = SimpleNamespace(logs_drained_at=None)
    uow = SimpleNamespace(
        attempts=SimpleNamespace(get=lambda _attempt_id: attempt),
        logs=SimpleNamespace(list_from_offset=lambda *_args, **_kwargs: []),
    )
    ctx = cast(
        AppContext,
        SimpleNamespace(uow_factory=lambda: nullcontext(uow), sse_tail_limiter=SseTailLimiter(1)),
    )
    request = cast(
        Request,
        SimpleNamespace(
            headers={"accept": "text/event-stream"},
            state=SimpleNamespace(),
            url=SimpleNamespace(path="/v1/attempts/attempt-1/logs"),
        ),
    )
    monkeypatch.setattr(records, "authenticate", lambda _uow, _token: principal)

    first = asyncio.run(
        records.get_attempt_logs("attempt-1", request, ctx, authorization="Bearer token")
    )
    refused = asyncio.run(
        records.get_attempt_logs("attempt-1", request, ctx, authorization="Bearer token")
    )

    assert first.media_type == "text/event-stream"
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1"
    assert refused.media_type == "application/problem+json"
    assert b'"type":"urn:crucible:problem:sse-tail-limit-exceeded"' in refused.body
    ctx.sse_tail_limiter.release()


def test_sse_tail_limit_defaults_to_the_foundry_target_and_is_configurable() -> None:
    assert Settings().service.max_sse_log_tails == 20
    assert Settings(service={"max_sse_log_tails": 3}).service.max_sse_log_tails == 3
