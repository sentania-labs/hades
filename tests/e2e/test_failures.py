"""Timeout, loss, orphan, restart, disconnect, and the checkout lease (18)."""

from __future__ import annotations

import asyncio
import time
from itertools import pairwise

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.docker import DockerProvider
from crucible.application.supervisor import Supervisor
from tests.e2e import daemon
from tests.e2e.conftest import (
    NET_WORKERS,
    RUN_ID,
    OriginFactory,
    e2e_contract,
    event_kinds,
    register,
    run_until,
    submit_and_start,
)

pytestmark = [
    pytest.mark.e2e,
    # Real containers, and the first case also pays for the session's stack; the waits
    # inside allow up to four minutes (issue 192).
    pytest.mark.timeout(600),
]

# How long a wait for a launch or a settled task may take, by the clock.
WAIT_SECONDS = 240
DONE = {"accepted", "pre_pr_gates_failed"}


async def _attempt_id(client: TestClient, task_id: str) -> str:
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["latest_attempt"], "no attempt yet"
    return str(view["latest_attempt"]["id"])


async def _wait_running(
    supervisor: Supervisor, client: TestClient, task_id: str, *, ticks: int = 40
) -> dict[str, object]:
    # At least `ticks` ticks and at least WAIT_SECONDS: a launch runs beside the tick
    # (hades #190), so a count of ticks alone is no budget.
    started = time.monotonic()
    done = 0
    while done < ticks or time.monotonic() - started < WAIT_SECONDS:
        await supervisor.tick()
        done += 1
        attempt = client.get(f"/v1/tasks/{task_id}").json().get("latest_attempt")
        if attempt and attempt["state"] == "running":
            return dict(attempt)
        await asyncio.sleep(0.25)
    raise AssertionError("worker never reached running")


async def test_a_timeout_drains_then_kills(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    engine: Engine,
    worker_image: str,
) -> None:
    """10: on expiry, SIGTERM, wait the grace, then SIGKILL. S5: --init is what makes
    the SIGTERM land at all, so the worker exits 143 rather than 137 after the grace."""
    url = origin("timeout", "hang")
    register(ctx, "timeout", url)
    document = e2e_contract("E2E-0010", "timeout", worker_image)
    document["execution_request"]["timeout_seconds"] = 15
    task_id = submit_and_start(client, document)

    state = await run_until(supervisor, client, task_id, DONE, max_ticks=80, pause=1.0)
    assert state in DONE
    attempt_id = await _attempt_id(client, task_id)
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT exit_class, exit_code, termination_reason, drain_deadline, "
                "logs_drained_at FROM attempts WHERE id = :id"
            ),
            {"id": attempt_id},
        ).one()
    assert row.exit_class == "timeout"
    assert row.termination_reason == "timeout"
    assert row.drain_deadline is not None
    assert row.exit_code in (143, 137), row.exit_code
    assert row.logs_drained_at is not None
    assert "attempt_timeout_drain" in event_kinds(client, task_id)
    assert daemon.container_ids(f"crucible.attempt={attempt_id}") == []


async def test_a_worker_removed_out_of_band_is_lost(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    engine: Engine,
    worker_image: str,
) -> None:
    """10: `lost` only when the daemon cannot see the container."""
    url = origin("loss", "hang")
    register(ctx, "loss", url)
    document = e2e_contract("E2E-0011", "loss", worker_image)
    document["lifecycle"] = {"max_attempts": 1, "retry_on": [], "cleanup": "policy"}
    task_id = submit_and_start(client, document)

    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        await supervisor.tick()
        attempt = client.get(f"/v1/tasks/{task_id}").json().get("latest_attempt")
        if attempt and attempt["state"] == "running":
            break
        await asyncio.sleep(0.5)
    attempt_id = await _attempt_id(client, task_id)
    containers = daemon.container_ids(f"crucible.attempt={attempt_id}")
    assert containers, "the worker container was never created"
    daemon.rm(*containers)

    await run_until(supervisor, client, task_id, DONE, max_ticks=30, pause=0.5)
    with engine.begin() as conn:
        exit_class = conn.execute(
            text("SELECT exit_class FROM attempts WHERE id = :id"), {"id": attempt_id}
        ).scalar_one()
    assert exit_class == "lost"
    assert "attempt_lost" in event_kinds(client, task_id)


async def test_a_labelled_container_with_no_attempt_row_is_removed(
    supervisor: Supervisor, worker_image: str
) -> None:
    """10 step 3: anything with a Crucible label and no live attempt row is an orphan."""
    name = f"crucible-e2e-orphan-{RUN_ID}"
    daemon.run_detached(
        name,
        [
            "--label",
            "crucible.attempt=01ORPHANORPHANORPHANORPHAN",
            "--label",
            "crucible.task=01ORPHANTASKORPHANTASKORPH",
            "--label",
            "crucible.owner=e2e",
            "--label",
            "crucible.role=worker",
            "--user",
            "1000:1000",
            "--network",
            NET_WORKERS,
            worker_image,
            "sh",
            "-c",
            "while :; do sleep 1; done",
        ],
    )
    try:
        result = await supervisor.tick()
        assert result.orphans >= 1
        assert daemon.container_ids("crucible.attempt=01ORPHANORPHANORPHANORPHAN") == []
    finally:
        daemon.rm(name)


async def test_a_restart_re_attaches_and_resumes_the_log_offset(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    provider: DockerProvider,
    origin: OriginFactory,
    engine: Engine,
    worker_image: str,
) -> None:
    """18: Crucible restart with a worker still running; logs resume from the offset."""
    url = origin("restart", "succeed")
    register(ctx, "restart", url)
    task_id = submit_and_start(client, e2e_contract("E2E-0012", "restart", worker_image))

    # First supervisor: launch, then stand down mid-run.
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        await supervisor.tick()
        attempt = client.get(f"/v1/tasks/{task_id}").json().get("latest_attempt")
        if attempt and attempt["state"] == "running":
            break
        await asyncio.sleep(0.2)
    attempt_id = await _attempt_id(client, task_id)
    await supervisor.stop()

    with engine.begin() as conn:
        before = conn.execute(
            text("SELECT count(*) FROM log_chunks WHERE attempt_id = :id"), {"id": attempt_id}
        ).scalar_one()

    # A new supervisor, a new provider instance: nothing in memory carries over. It has
    # to find the worker by label (10) and resume the stream by (timestamp, hash).
    fresh_provider = DockerProvider(provider.config)
    successor = Supervisor(
        ctx.uow_factory,
        {"docker": fresh_provider},
        ctx.clock,
        holder=f"e2e-successor-{RUN_ID}",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=120,
        grace_seconds=5,
    )
    await run_until(successor, client, task_id, DONE, max_ticks=60, pause=0.5)

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT content, gzipped, offset_start, offset_end FROM log_chunks "
                "WHERE attempt_id = :id ORDER BY id"
            ),
            {"id": attempt_id},
        ).all()
    assert len(rows) >= before
    body = b"".join(r.content for r in rows if not r.gzipped).decode("utf-8", "replace")
    lines = [line for line in body.splitlines() if "read identity bundle" in line]
    # `docker logs --since` is inclusive (S8): a resume by timestamp alone would
    # repeat the boundary line on every pull.
    assert len(lines) == 1, f"the resume duplicated a line: {lines}"
    offsets = [(r.offset_start, r.offset_end) for r in rows]
    assert all(a[1] == b[0] for a, b in pairwise(offsets))


async def test_the_run_completes_with_no_client_attached(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    worker_image: str,
) -> None:
    """18: the API client exits after start; the run completes and a wake is waiting."""
    url = origin("disconnect", "succeed")
    register(ctx, "disconnect", url)
    task_id = submit_and_start(client, e2e_contract("E2E-0013", "disconnect", worker_image))
    client.close()

    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        await supervisor.tick()
        with ctx.uow_factory() as uow:
            task = uow.tasks.get(task_id)
            assert task is not None
            if task.state.value in DONE:
                break
        await asyncio.sleep(0.5)
    with ctx.uow_factory() as uow:
        task = uow.tasks.get(task_id)
        assert task is not None and task.state.value in DONE
        waiting = uow.wakes.list_for_principal(
            task.principal_id, since=None, include_acked=False, limit=50
        )
    assert task.state.value == "accepted"
    accepted = [wake for wake in waiting if wake.task_id == task_id]
    assert len(accepted) == 1, "Foundry must hear that the artifacts are accepted"
    wake = accepted[0]
    assert wake.reason == "accepted"
    assert wake.payload["task"]["state"] == "accepted"
    assert wake.payload["summary"] == (
        "accepted, artifacts are ready; no branch or PR publication was requested."
    )
    assert wake.payload["links"]["artifacts"].endswith("/artifacts")


async def test_a_second_task_on_the_same_branch_is_refused_at_submit(
    ctx: AppContext,
    client: TestClient,
    origin: OriginFactory,
    worker_image: str,
) -> None:
    """hades #564: a work branch belongs to one task. This case used to submit a second
    task on the first one's branch and watch the checkout lease of 10 hold it pending.
    The lease is released with every terminal attempt state, and a retry or correction
    attempt of the same task is only created after that, so no path of one task's own
    reaches a held lease; the second task never reaches the lease at all now, because
    its submission is refused, naming the owner. The lease itself is proven on the
    integration tier (test_readiness_gaps)."""
    url = origin("lease", "hang")
    register(ctx, "lease", url)
    first = e2e_contract("E2E-0014", "lease", worker_image)
    response = client.post("/v1/tasks", json=first)
    assert response.status_code == 201, response.text
    second = e2e_contract("E2E-0015", "lease", worker_image)
    second["repository"]["work_branch"] = "crucible/E2E-0014"
    response = client.post("/v1/tasks", json=second)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["type"] == "urn:crucible:problem:contract-invalid"
    (problem,) = [e for e in body["errors"] if e["path"] == "repository.work_branch"]
    assert "E2E-0014" in problem["message"] and "crucible/E2E-0014" in problem["message"]
    assert "a branch belongs to one task" in problem["message"]
    listed = client.get("/v1/tasks", params={"repository": "lease"}).json()
    assert [t["external_id"] for t in listed["items"]] == ["E2E-0014"]


async def test_live_log_tail_delivers_while_worker_is_running(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    worker_image: str,
) -> None:
    url = origin("live-tail", "hang")
    register(ctx, "live-tail", url)
    task_id = submit_and_start(client, e2e_contract("E2E-TAIL", "live-tail", worker_image))
    attempt = await _wait_running(supervisor, client, task_id)
    attempt_id = str(attempt["id"])

    def read_tail() -> str:
        with client.stream(
            "GET",
            f"/v1/attempts/{attempt_id}/logs",
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200
            return "\n".join(response.iter_lines())

    tail = asyncio.create_task(asyncio.to_thread(read_tail))
    await asyncio.sleep(0.5)
    cancelled = client.post(
        f"/v1/tasks/{task_id}/cancel",
        json={
            "reason": "tail test complete",
            "verbatim": "stop the tail test",
            "decided_by": "tests",
        },
    )
    assert cancelled.status_code == 200
    await run_until(supervisor, client, task_id, {"cancelled"}, max_ticks=30, pause=0.5)
    body = await asyncio.wait_for(tail, timeout=10)
    assert "event: stdout" in body
    assert "sleeping until Crucible drains me" in body
    assert "event: end" in body


async def test_cancel_kills_a_real_worker_and_keeps_its_partial_report_unparsed(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    worker_image: str,
) -> None:
    url = origin("partial-kill", "hang")
    register(ctx, "partial-kill", url)
    task_id = submit_and_start(client, e2e_contract("E2E-PARTIAL", "partial-kill", worker_image))
    attempt = await _wait_running(supervisor, client, task_id)
    attempt_id = str(attempt["id"])
    container = daemon.container_ids(f"crucible.attempt={attempt_id}")[0]
    daemon.run(
        "exec",
        container,
        "sh",
        "-c",
        "printf '%s\\n' 'schema_version: 1.0' 'summary: interrupted' "
        "> /crucible/report/report.yaml",
    )
    daemon.run("kill", "--signal", "STOP", container)
    cancelled = client.post(
        f"/v1/tasks/{task_id}/cancel",
        json={
            "reason": "forced partial report",
            "verbatim": "kill this worker",
            "decided_by": "tests",
        },
    )
    assert cancelled.status_code == 200
    await supervisor.tick()
    await asyncio.sleep(6)
    await supervisor.tick()
    assert (
        await run_until(supervisor, client, task_id, {"cancelled"}, max_ticks=20, pause=0.5)
        == "cancelled"
    )
    stored = client.get(f"/v1/attempts/{attempt_id}").json()
    assert stored["exit_class"] == "killed"
    assert stored["report"] is None
    artifacts = client.get(f"/v1/attempts/{attempt_id}/artifacts").json()["items"]
    partial = [item for item in artifacts if item["type"] == "partial_report"]
    assert len(partial) == 1 and partial[0]["filename"] == "report/report.yaml"
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    collected = next(event for event in events if event["kind"] == "attempt_collected")
    assert collected["payload"]["partial_report_kept_unparsed"] is True


async def test_a_stalled_real_worker_warns_then_drains_and_kills(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    engine: Engine,
    worker_image: str,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE policies SET document = jsonb_set(jsonb_set(document, "
                "'{limits,stall_warn_seconds}', '2'), '{limits,stall_fail_seconds}', '4') "
                "WHERE name='e2e-script' AND version=1"
            )
        )
    url = origin("stall", "hang")
    register(ctx, "stall", url)
    task_id = submit_and_start(client, e2e_contract("E2E-STALL", "stall", worker_image))
    attempt = await _wait_running(supervisor, client, task_id)
    attempt_id = str(attempt["id"])
    state = await run_until(supervisor, client, task_id, DONE, max_ticks=30, pause=1.0)
    assert state in DONE
    stored = client.get(f"/v1/attempts/{attempt_id}").json()
    assert stored["exit_class"] == "stalled"
    assert stored["termination_reason"] == "stall"
    kinds = event_kinds(client, task_id)
    assert "worker_quiet" in kinds and "worker_stalled" in kinds
