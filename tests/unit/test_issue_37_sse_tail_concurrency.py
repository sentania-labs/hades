"""Hades #37: live log tails have a configured, process-local admission limit."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from crucible.adapters.api.deps import SseTailLimiter
from crucible.adapters.api.routers import records
from crucible.adapters.persistence import models as m
from crucible.domain.entities import Role
from crucible.settings import Settings
from tests.unit.issue_485_fixture import workload

TAIL = "/v1/attempts/attempt-0/logs"
SSE = {"Accept": "text/event-stream", "Authorization": "Bearer tail-token"}


@pytest.fixture
def tail_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, Any, Any]]:
    """The real API over a SQLite database holding one running attempt, limit 1."""
    app, ctx, engine = workload(tmp_path / "tails.db", count=1)
    ctx.sse_tail_limiter = SseTailLimiter(1)
    principal = SimpleNamespace(id="operator", name="operator", role=Role.OBSERVER)
    monkeypatch.setattr(records, "authenticate", lambda _uow, _token: principal)
    yield app, ctx, engine
    engine.dispose()


def _open_tail(app: Any) -> tuple[threading.Thread, dict[str, Any]]:
    """Start one SSE tail in a thread; the TestClient returns when the stream closes."""
    result: dict[str, Any] = {}

    def read() -> None:
        with TestClient(app) as client, client.stream("GET", TAIL, headers=SSE) as response:
            result["status"] = response.status_code
            result["body"] = "\n".join(response.iter_lines())
        result["closed_at"] = time.monotonic()

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread, result


def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.02)


def _end_attempt(engine: Any) -> float:
    with Session(engine) as db:
        db.execute(
            update(m.AttemptRow)
            .where(m.AttemptRow.id == "attempt-0")
            .values(state="succeeded", logs_drained_at=datetime.now(UTC))
        )
        db.commit()
    return time.monotonic()


def test_one_tail_over_the_configured_limit_returns_a_problem_with_retry_after(
    tail_app: tuple[Any, Any, Any],
) -> None:
    app, ctx, engine = tail_app
    thread, first = _open_tail(app)
    _wait_for(lambda: ctx.sse_tail_limiter.in_use == 1)

    with TestClient(app) as client:
        refused = client.get(TAIL, headers=SSE)
        plain = client.get(TAIL, headers={"Authorization": SSE["Authorization"]})

    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1"
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert refused.json()["type"] == "urn:crucible:problem:sse-tail-limit-exceeded"
    assert refused.json()["status"] == 429
    # Only live tails are limited; an ordinary read of the stored bytes still works.
    assert plain.status_code == 200

    _end_attempt(engine)
    thread.join(timeout=5)
    assert first["status"] == 200
    assert ctx.sse_tail_limiter.in_use == 0


def test_an_admitted_tail_closes_with_end_within_a_second_of_the_attempt_ending(
    tail_app: tuple[Any, Any, Any],
) -> None:
    """The limiter only gates admission: it never holds the stream open or swallows
    the end signal, and the slot is free again once the end event has gone out."""
    app, ctx, engine = tail_app
    thread, tail = _open_tail(app)
    _wait_for(lambda: ctx.sse_tail_limiter.in_use == 1)
    time.sleep(0.3)  # let the tail poll at least once while the attempt runs

    ended_at = _end_attempt(engine)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert tail["status"] == 200
    assert tail["closed_at"] - ended_at < 1.0
    assert tail["body"].rstrip().splitlines()[-2] == "event: end"
    assert ctx.sse_tail_limiter.in_use == 0

    # The released slot admits the next tail, which ends at once on a drained attempt.
    thread, again = _open_tail(app)
    thread.join(timeout=5)
    assert again["status"] == 200
    assert "event: end" in again["body"]
    assert ctx.sse_tail_limiter.in_use == 0


def test_a_tail_whose_generator_never_starts_still_returns_its_slot(
    tail_app: tuple[Any, Any, Any],
) -> None:
    """A client can disconnect before the body iterator is first advanced; the
    generator's finally never runs then, so the response's background task releases
    the slot, and a permit releases only once however many paths reach it."""
    _app, ctx, _engine = tail_app

    async def is_disconnected() -> bool:
        return False

    request: Any = SimpleNamespace(
        headers={"accept": "text/event-stream"},
        state=SimpleNamespace(),
        url=SimpleNamespace(path=TAIL),
        is_disconnected=is_disconnected,
    )
    response = asyncio.run(
        records.get_attempt_logs("attempt-0", request, ctx, authorization="Bearer t")
    )
    assert response.media_type == "text/event-stream"
    assert ctx.sse_tail_limiter.in_use == 1
    refused = asyncio.run(
        records.get_attempt_logs("attempt-0", request, ctx, authorization="Bearer t")
    )
    assert refused.status_code == 429

    assert response.background is not None
    asyncio.run(response.background())
    asyncio.run(response.background())
    assert ctx.sse_tail_limiter.in_use == 0


def test_sse_tail_limit_defaults_to_the_foundry_target_and_is_configurable() -> None:
    assert Settings().service.max_sse_log_tails == 20
    assert Settings(service={"max_sse_log_tails": 3}).service.max_sse_log_tails == 3
    with pytest.raises(ValueError):
        Settings(service={"max_sse_log_tails": 0})
