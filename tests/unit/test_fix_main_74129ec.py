"""Red main after #527 (74129ec): the SSE tail tests used bare sleeps (issue 37).

The unit tier refuses `time.sleep`/`asyncio.sleep` outside `tests/wait.py` and its
allowlist (issue 193), and #527's tail tests slept twice, which turned `test`,
`images` (the unit tier in the worker image) and `ci` red. The tests now wait on
observed state. This file pins that, and that the limit itself still holds: one
tail more than the configured limit gets a problem response with Retry-After, and
the benchmark still reports queries per second and connection use for N tails."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.api.deps import SseTailLimiter
from crucible.adapters.api.routers import records
from crucible.domain.entities import Role
from tests.unit.issue_485_fixture import workload
from tests.unit.test_issue_193_no_bare_sleeps import SLEEP_ALLOWLIST, _bare_sleeps
from tools.benchmarks.issue_37_sse_tail_concurrency import run

TESTS = Path(__file__).parents[1]
TAIL_TESTS = Path("unit/test_issue_37_sse_tail_concurrency.py")
LIMIT = 2


def test_the_sse_tail_tests_wait_on_state_rather_than_sleeping() -> None:
    assert TAIL_TESTS not in SLEEP_ALLOWLIST
    assert _bare_sleeps(TESTS / TAIL_TESTS) == []


def _request() -> Any:
    async def is_disconnected() -> bool:
        return False

    return SimpleNamespace(
        headers={"accept": "text/event-stream"},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/v1/attempts/attempt-0/logs"),
        is_disconnected=is_disconnected,
    )


def test_one_tail_over_the_configured_limit_is_refused_with_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, ctx, engine = workload(tmp_path / "tails.db", count=1)
    ctx.sse_tail_limiter = SseTailLimiter(LIMIT)
    principal = SimpleNamespace(id="operator", name="operator", role=Role.OBSERVER)
    monkeypatch.setattr(records, "authenticate", lambda _uow, _token: principal)

    async def open_tails() -> list[Any]:
        return [
            await records.get_attempt_logs("attempt-0", _request(), ctx, authorization="Bearer t")
            for _ in range(LIMIT + 1)
        ]

    try:
        *admitted, refused = asyncio.run(open_tails())
        assert [response.media_type for response in admitted] == ["text/event-stream"] * LIMIT
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == "1"
        assert refused.media_type == "application/problem+json"
        assert ctx.sse_tail_limiter.in_use == LIMIT

        for response in admitted:
            asyncio.run(response.background())
        assert ctx.sse_tail_limiter.in_use == 0
    finally:
        engine.dispose()


def test_the_benchmark_reports_queries_per_second_and_connection_use() -> None:
    measured = run(tails=3, seconds=0.3)

    assert measured["tails"] == 3
    assert measured["queries_per_second"] > 0
    assert measured["connection_checkouts"] > 0
    assert measured["peak_connections_in_use"] >= 1
