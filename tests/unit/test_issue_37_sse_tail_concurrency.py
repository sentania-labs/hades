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

    async def _is_disconnected() -> bool:
        return False

    request = cast(
        Request,
        SimpleNamespace(
            headers={"accept": "text/event-stream"},
            state=SimpleNamespace(),
            url=SimpleNamespace(path="/v1/attempts/attempt-1/logs"),
            is_disconnected=_is_disconnected,
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


def test_tail_permit_releases_on_background_even_if_the_generator_never_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hades PR #527 review: on the pinned ASGI 2.3 stack, a disconnect can win the
    race against the SSE generator's first iteration, and aclose() on a generator
    that never started running executes none of its body. The permit must still be
    released, so release is tied to the response's BackgroundTask rather than a
    try/finally inside the generator; this proves release happens with the
    generator's body_iterator never touched."""
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

    async def _is_disconnected() -> bool:
        return False

    request = cast(
        Request,
        SimpleNamespace(
            headers={"accept": "text/event-stream"},
            state=SimpleNamespace(),
            url=SimpleNamespace(path="/v1/attempts/attempt-1/logs"),
            is_disconnected=_is_disconnected,
        ),
    )
    monkeypatch.setattr(records, "authenticate", lambda _uow, _token: principal)

    first = asyncio.run(
        records.get_attempt_logs("attempt-1", request, ctx, authorization="Bearer token")
    )
    assert first.background is not None

    still_refused = asyncio.run(
        records.get_attempt_logs("attempt-1", request, ctx, authorization="Bearer token")
    )
    assert still_refused.status_code == 429

    # Simulate Starlette calling the response's BackgroundTask after the
    # stream_response/listen_for_disconnect race resolves, without ever having
    # called `first.body_iterator.__anext__()` (the generator never started).
    asyncio.run(first.background())

    freed = asyncio.run(
        records.get_attempt_logs("attempt-1", request, ctx, authorization="Bearer token")
    )
    assert freed.media_type == "text/event-stream"
    ctx.sse_tail_limiter.release()


def test_two_separate_event_loops_each_open_a_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limiter may live across event-loop boundaries (e.g. a shared test
    fixture).  Each ``asyncio.run()`` creates a fresh loop; the limiter must
    never raise ``RuntimeError: ... is bound to a different event loop``.

    This proves the fix for the e2e failure:
    ``tests/e2e/test_failures.py::test_live_log_tail_delivers_while_worker_is_running``
    where a shared AppContext carrying the limiter is used from two different
    TestClient event loops.
    """
    principal = SimpleNamespace(id="reader", name="reader", role=Role.OBSERVER)
    attempt = SimpleNamespace(logs_drained_at=None)
    uow = SimpleNamespace(
        attempts=SimpleNamespace(get=lambda _attempt_id: attempt),
        logs=SimpleNamespace(list_from_offset=lambda *_args, **_kwargs: []),
    )
    # A single limiter shared across both loops.
    ctx = cast(
        AppContext,
        SimpleNamespace(
            uow_factory=lambda: nullcontext(uow),
            sse_tail_limiter=SseTailLimiter(20),
        ),
    )

    async def _is_disconnected() -> bool:
        return False

    request = cast(
        Request,
        SimpleNamespace(
            headers={"accept": "text/event-stream"},
            state=SimpleNamespace(),
            url=SimpleNamespace(path="/v1/attempts/attempt-1/logs"),
            is_disconnected=_is_disconnected,
        ),
    )
    monkeypatch.setattr(records, "authenticate", lambda _uow, _token: principal)

    # Loop 1
    loop1_tail = asyncio.run(
        records.get_attempt_logs("attempt-1", request, ctx, authorization="Bearer token")
    )
    assert loop1_tail.media_type == "text/event-stream"

    # Loop 2 (completely different event loop)
    loop2_tail = asyncio.run(
        records.get_attempt_logs("attempt-1", request, ctx, authorization="Bearer token")
    )
    assert loop2_tail.media_type == "text/event-stream"

    # Release both permits so the limiter can be reused.
    ctx.sse_tail_limiter.release()
    ctx.sse_tail_limiter.release()
