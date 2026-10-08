# Issue 485 local workload

Run from a checkout with its installed environment:

    PYTHONPATH=. uv run python tools/benchmarks/issue_485.py

For origin/main, copy this runner and tests/unit/issue_485_fixture.py into a
detached worktree and use the same Python environment with that worktree on
PYTHONPATH. No network provider or cluster is involved.

The fixture seeds 500 total tasks round-robin across every TaskState, with one
execution, running attempt, gate result and unacknowledged wake per task. It uses
SQLite with JSON and timezone adaptations for the production ORM. The fake
Kubernetes provider returns one image and healthy status, with a 20 ms asynchronous
delay per discovery or health call. The changed branch prepopulates the shared
snapshot, as the supervisor would; main calls the fake provider during requests.
Both use a signed admin session and the app's TestClient. Each page is warmed
once, then timed alone and with four requests released from a barrier through one
shared client/event loop. SQL statement counts include request authentication.

These are controlled local measurements, not Foundry's lab workload or deployment
measurements. SQLite, tiny fake-provider latency, host scheduling and Python's GIL
limit what these times say about production concurrency. CI owns Postgres,
container and cluster integration; Foundry owns the post-deploy lab rerun.

The required regression invokes the runner in a subprocess with 50 tasks per
state, checks the board's bounded SQL count and single grouped count query, then
checks archival filtering across every state. The subprocess isolates SQLite
metadata adaptations from other tests.

Request bodies are buffered on the ASGI loop before threaded handlers consume
them. Local admin commands bypass the supervisor cache. API processes read the
supervisor snapshot from provider_settings; an empty database snapshot stays
empty until a supervisor refresh, and the last snapshot remains readable during
supervisor downtime. Administrators can change the 60-second default TTL in the
existing Runtime settings table without a restart.

Migration 0050 extends the event-kind constraint for audited TTL changes. Apply
migrations before serving the changed version. Its PostgreSQL upgrade/downgrade
execution remains part of CI's integration tier.
