"""Measure database polling from N live SSE log tails on a local test database.

Run ``uv run python tools/benchmarks/issue_37_sse_tail_concurrency.py [tails] [seconds]``.
The benchmark uses the same short-lived SQL unit of work as the API route and reports
SQL queries per second plus pool checkouts and peak simultaneous checked-out connections.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from sqlalchemy import event
from starlette.requests import Request

from crucible.adapters.api.deps import SseTailLimiter
from crucible.adapters.api.routers import records
from tests.unit.issue_485_fixture import workload


class _Request:
    """Minimal stub that satisfies the async interface the SSE route calls."""

    def __init__(self) -> None:
        self.headers = {"accept": "text/event-stream"}
        self.state = SimpleNamespace()
        self.url = SimpleNamespace(path="/v1/attempts/attempt-0/logs")
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


def run(tails: int = 20, seconds: float = 2.0) -> dict[str, float | int]:
    """Open ``tails`` routes, poll for ``seconds``, then return database load metrics."""
    if tails < 1:
        raise ValueError("tails must be positive")
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    with tempfile.TemporaryDirectory() as directory:
        _app, ctx, engine = workload(Path(directory) / "bench.db", count=1)
        ctx.sse_tail_limiter = SseTailLimiter(tails)
        principal = SimpleNamespace(id="operator", name="operator")
        lock = threading.Lock()
        counters = {"queries": 0, "checkouts": 0, "checked_out": 0, "peak_checked_out": 0}

        def count_query(  # type: ignore[no-untyped-def]
            _conn, _cursor, _statement, _parameters, _context, _executemany
        ) -> None:
            with lock:
                counters["queries"] += 1

        def checkout(  # type: ignore[no-untyped-def]
            _dbapi_connection, _connection_record, _connection_proxy
        ) -> None:
            with lock:
                counters["checkouts"] += 1
                counters["checked_out"] += 1
                peak = counters["peak_checked_out"]
                current = counters["checked_out"]
                if current > peak:
                    counters["peak_checked_out"] = current

        def checkin(  # type: ignore[no-untyped-def]
            _dbapi_connection, _connection_record
        ) -> None:
            with lock:
                counters["checked_out"] -= 1

        event.listen(engine, "before_cursor_execute", count_query)
        event.listen(engine, "checkout", checkout)
        event.listen(engine, "checkin", checkin)

        async def measure() -> float:
            requests = [_Request() for _ in range(tails)]
            with patch.object(records, "authenticate", return_value=principal):
                responses = await asyncio.gather(
                    *(
                        records.get_attempt_logs(
                            "attempt-0",
                            cast(Request, request),
                            ctx,
                            authorization="Bearer benchmark",
                        )
                        for request in requests
                    )
                )
                assert all(response.status_code == 200 for response in responses)

                async def consume(response: object) -> None:
                    async for _item in response.body_iterator:  # type: ignore[attr-defined]
                        pass

                # Reset counters so that queries_per_second and connection metrics
                # describe only the timed polling interval, not the response-creation
                # phase above (01M4CF3ME8TP0VFVRATX36MJJ7).
                counters["queries"] = 0
                counters["checkouts"] = 0
                counters["checked_out"] = 0
                counters["peak_checked_out"] = 0

                consumers = [asyncio.create_task(consume(response)) for response in responses]
                started = time.perf_counter()
                await asyncio.sleep(seconds)
                for request in requests:
                    request.disconnected = True
                await asyncio.gather(*consumers)
                return time.perf_counter() - started

        elapsed = asyncio.run(measure())
        event.remove(engine, "before_cursor_execute", count_query)
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
        engine.dispose()
    return {
        "tails": tails,
        "seconds": round(elapsed, 3),
        "queries": counters["queries"],
        "queries_per_second": round(counters["queries"] / elapsed, 2),
        "connection_checkouts": counters["checkouts"],
        "peak_connections_in_use": counters["peak_checked_out"],
    }


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    print(json.dumps(run(count, duration), indent=2))
