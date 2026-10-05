"""Reconciliation idempotence, supervisor restart mid-attempt, orphan removal (10, 18)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from crucible.ports.execution import LaunchSpec
from tests.fixtures import FakeClock, contract_document
from tests.integration.conftest import (
    ARTIFACTS_DELIVERABLE,
    event_kinds,
    make_supervisor,
    run_to_settled,
    submit_and_start,
)

pytestmark = pytest.mark.integration

STATE_TABLES = (
    "tasks",
    "executions",
    "attempts",
    "events",
    "completion_claims",
    "task_contracts",
    # C2 record tables (14): reconciliation must not touch these a second time either.
    "artifacts",
    "evidence",
    "gate_results",
    "review_reports",
    "acceptance_results",
    "decisions",
    "escalations",
    "wakes",
    "attempt_metrics",
)


def snapshot(engine: Engine) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    with engine.connect() as conn:
        for table in STATE_TABLES:
            rows = conn.execute(text(f"SELECT * FROM {table} ORDER BY 1")).mappings().all()
            out[table] = [dict(r) for r in rows]
    return out


async def test_reconcile_twice_changes_nothing_after_completion(
    client: TestClient, supervisor: Supervisor, engine: Engine
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert await run_to_settled(supervisor, client, task_id) == "publishing"
    before = snapshot(engine)
    await supervisor.reconcile()
    middle = snapshot(engine)
    await supervisor.reconcile()
    after = snapshot(engine)
    assert before == middle == after


async def test_reconcile_twice_changes_nothing_after_acceptance(
    client: TestClient, supervisor: Supervisor, engine: Engine
) -> None:
    """The gate, evidence, wake, and metrics rows settle exactly once (10)."""
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    await run_to_settled(supervisor, client, task_id)
    await supervisor.tick()
    before = snapshot(engine)
    await supervisor.reconcile()
    middle = snapshot(engine)
    await supervisor.reconcile()
    after = snapshot(engine)
    assert before == middle == after


async def test_reconcile_twice_changes_nothing_after_a_gate_failure(
    client: TestClient, supervisor: Supervisor, engine: Engine
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-prohibited-path")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    await supervisor.tick()
    before = snapshot(engine)
    await supervisor.reconcile()
    await supervisor.reconcile()
    assert snapshot(engine) == before


async def test_reconcile_twice_changes_nothing_mid_attempt(
    client: TestClient, supervisor: Supervisor, engine: Engine
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-hang")
    await supervisor.tick()
    first = snapshot(engine)
    await supervisor.reconcile()
    second = snapshot(engine)
    await supervisor.reconcile()
    third = snapshot(engine)
    # Lease renewal and the liveness row change; task, execution, attempt, event state do not.
    assert first == second == third
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "running"


async def test_supervisor_restart_mid_attempt(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    a = make_supervisor(ctx, provider, holder="sup-a", lease_ttl_seconds=30)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed-4")
    await a.tick()
    await a.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "running"
    (attempt,) = view["executions"][0]["attempts"]
    assert attempt["state"] == "running"
    del a  # crash: no release, the lease simply expires

    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b", lease_ttl_seconds=30)
    assert (await b.tick()).held
    assert await run_to_settled(b, client, task_id) == "publishing"
    view = client.get(f"/v1/tasks/{task_id}").json()
    attempts = [x for e in view["executions"] for x in e["attempts"]]
    assert len(attempts) == 1 and attempts[0]["id"] == attempt["id"]
    assert attempts[0]["state"] == "succeeded"
    kinds = event_kinds(client, task_id)
    assert kinds.count("attempt_created") == 1 and kinds.count("attempt_running") == 1
    global_events = client.get("/v1/events", params={"kind": "supervisor_lease_acquired"}).json()
    holders = [e["payload"]["holder"] for e in global_events["items"]]
    assert holders == ["sup-a", "sup-b"]
    assert global_events["items"][-1]["payload"]["takeover"] is True


async def test_restart_between_launch_and_running_adopts_the_worker(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    """Crucible died after the provider launched the worker but before recording it."""
    a = make_supervisor(ctx, provider, holder="sup-a")
    task_id = submit_and_start(client, "crucible-worker:fake-succeed-3")
    await a.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    (attempt,) = view["executions"][0]["attempts"]
    # Rewind the record to `launching` with no handle, as if the last write never happened.
    with ctx.uow_factory() as uow:
        uow.set_fenced_token(a.fenced_token or 0)
        row = uow.attempts.get(attempt["id"], for_update=True)
        assert row is not None
        from crucible.domain.lifecycle import AttemptState  # noqa: PLC0415

        row.state = AttemptState.LAUNCHING
        row.handle = None
        row.started_at = None
        uow.attempts.save(row)
        uow.commit()
    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b")
    await b.tick()
    kinds = event_kinds(client, task_id)
    assert "attempt_adopted" in kinds
    assert await run_to_settled(b, client, task_id) == "publishing"


async def test_orphan_handle_is_removed(
    ctx: AppContext, provider: FakeProvider, client: TestClient, supervisor: Supervisor
) -> None:
    spec = LaunchSpec(
        attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        task_id="none",
        external_id="ORPHAN",
        role="implement",
        harness="codex",
        model="m",
        image="crucible-worker:fake-hang",
        timeout_seconds=60,
        contract=contract_document(),
    )
    await provider.launch(await provider.prepare(spec), spec)
    result = await supervisor.tick()
    assert result.orphans == 1
    assert await provider.reconcile() == []
    orphan_events = client.get("/v1/events", params={"kind": "orphan_removed"}).json()["items"]
    assert len(orphan_events) == 1 and orphan_events[0]["attempt_id"] is None


async def test_worker_removed_out_of_band_is_lost(
    client: TestClient, provider: FakeProvider, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-hang",
        lifecycle={"max_attempts": 1, "retry_on": ["lost"], "cleanup": "policy"},
    )
    await supervisor.tick()
    (attempt,) = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]
    provider.remove_out_of_band(str(attempt["id"]))
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    assert "attempt_lost" in event_kinds(client, task_id)


async def test_stranded_launch_without_handle_is_environment_failure(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    """Crucible died after `preparing` was recorded and before any worker existed."""
    a = make_supervisor(ctx, provider, holder="sup-a")
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-succeed-5",
        lifecycle={"max_attempts": 2, "retry_on": ["environment"], "cleanup": "policy"},
    )
    await a.tick()
    (attempt,) = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]
    from crucible.domain.lifecycle import AttemptState  # noqa: PLC0415

    provider.remove_out_of_band(str(attempt["id"]))
    with ctx.uow_factory() as uow:
        uow.set_fenced_token(a.fenced_token or 0)
        row = uow.attempts.get(attempt["id"], for_update=True)
        assert row is not None
        row.state = AttemptState.PREPARING
        row.handle = None
        uow.attempts.save(row)
        uow.commit()
    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b")
    await b.tick()
    kinds = event_kinds(client, task_id)
    assert "task_retry_scheduled" in kinds
    attempts = [
        x for e in client.get(f"/v1/tasks/{task_id}").json()["executions"] for x in e["attempts"]
    ]
    assert attempts[0]["exit_class"] == "environment" and len(attempts) == 2
    assert await run_to_settled(b, client, task_id) == "publishing"


async def test_worker_surviving_kill_is_killed_again(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider, clock: FakeClock
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-immortal")
    await supervisor.tick()
    (attempt,) = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]
    worker = provider.worker(str(attempt["id"]))
    assert worker is not None
    clock.advance(3600)
    await supervisor.tick()
    assert worker.drains == 1
    clock.advance(60)
    await supervisor.tick()
    assert worker.kills == 1, "first kill issued at the grace deadline"
    await supervisor.tick()
    assert worker.kills == 2 and worker.drains == 1, "re-killed, never re-drained"
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    kinds = event_kinds(client, task_id)
    assert kinds.count("attempt_timeout_drain") == 1 and kinds.count("attempt_timeout_kill") == 1


async def test_illegal_supervisor_transition_is_recorded(
    ctx: AppContext, provider: FakeProvider, client: TestClient
) -> None:
    from crucible.application.transitions import move_task  # noqa: PLC0415
    from crucible.domain.events import EventKind  # noqa: PLC0415
    from crucible.domain.lifecycle import IllegalTransitionError, TaskState  # noqa: PLC0415

    sup: Supervisor = make_supervisor(ctx, provider)
    await sup.tick()
    task_id = client.post("/v1/tasks", json=contract_document()).json()["id"]
    with pytest.raises(IllegalTransitionError), sup._fenced() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        assert task is not None
        move_task(uow, ctx.clock, task, TaskState.REPORTED, EventKind.TASK_REPORTED)
    kinds = event_kinds(client, task_id)
    assert kinds == ["task_submitted", "transition_rejected"]
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "submitted"


class _SupervisorDied(BaseException):
    """A process death: not an Exception, so nothing in the tick can catch it."""


async def _crash_launch_at(
    ctx: AppContext, provider: FakeProvider, client: TestClient, where: str, image: str
) -> tuple[str, str]:
    """Run supervisor A until it dies at `where`; return (task_id, attempt_id)."""
    from unittest.mock import patch  # noqa: PLC0415

    a = make_supervisor(ctx, provider, holder="sup-a")
    task_id = submit_and_start(
        client,
        image,
        lifecycle={"max_attempts": 2, "retry_on": ["environment"], "cleanup": "policy"},
    )

    def die(*_args: object, **_kw: object) -> None:
        raise _SupervisorDied("supervisor process died here")

    target = {"before_prepare": (provider, "prepare"), "before_launch": (provider, "launch")}.get(
        where, (a, "_mark_running")
    )
    with patch.object(*target, side_effect=die), pytest.raises(_SupervisorDied):
        await a.tick()
    (attempt,) = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]
    return task_id, str(attempt["id"])


def _attempts(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    return [
        x for e in client.get(f"/v1/tasks/{task_id}").json()["executions"] for x in e["attempts"]
    ]


async def test_crash_before_prepare_is_environment_on_reconcile(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    task_id, attempt_id = await _crash_launch_at(
        ctx, provider, client, "before_prepare", "crucible-worker:fake-succeed"
    )
    assert client.get(f"/v1/attempts/{attempt_id}").json()["state"] == "preparing"
    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b")
    await b.tick()
    first = client.get(f"/v1/attempts/{attempt_id}").json()
    assert first["state"] == "failed" and first["exit_class"] == "environment"
    assert "task_retry_scheduled" in event_kinds(client, task_id)
    assert await run_to_settled(b, client, task_id) == "publishing"
    attempts = _attempts(client, task_id)
    assert [a["number"] for a in attempts] == [1, 2] and attempts[1]["state"] == "succeeded"


async def test_crash_before_launch_is_environment_on_reconcile(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    task_id, attempt_id = await _crash_launch_at(
        ctx, provider, client, "before_launch", "crucible-worker:fake-succeed"
    )
    assert client.get(f"/v1/attempts/{attempt_id}").json()["state"] == "launching"
    assert provider.worker(attempt_id) is None
    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b")
    await b.tick()
    first = client.get(f"/v1/attempts/{attempt_id}").json()
    assert first["state"] == "failed" and first["exit_class"] == "environment"
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    collected = next(e for e in events if e["kind"] == "attempt_collected")
    assert collected["payload"]["stage"] == "reconcile"
    assert await run_to_settled(b, client, task_id) == "publishing"
    assert len(_attempts(client, task_id)) == 2


async def test_crash_after_launch_adopts_the_worker_on_reconcile(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    task_id, attempt_id = await _crash_launch_at(
        ctx, provider, client, "after_launch", "crucible-worker:fake-succeed-3"
    )
    assert client.get(f"/v1/attempts/{attempt_id}").json()["state"] == "launching"
    assert provider.worker(attempt_id) is not None, "the worker outlived the supervisor"
    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b")
    await b.tick()
    adopted = client.get(f"/v1/attempts/{attempt_id}").json()
    assert adopted["state"] == "running" and adopted["handle"] == f"fake-{attempt_id}"
    assert adopted["lease"]["holder"] == "sup-b"
    assert "attempt_adopted" in event_kinds(client, task_id)
    assert await run_to_settled(b, client, task_id) == "publishing"
    attempts = _attempts(client, task_id)
    assert len(attempts) == 1 and attempts[0]["state"] == "succeeded"


async def test_crash_after_launch_with_a_vanished_worker_is_environment(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    task_id, attempt_id = await _crash_launch_at(
        ctx, provider, client, "after_launch", "crucible-worker:fake-succeed-3"
    )
    provider.remove_out_of_band(attempt_id)
    clock.advance(31)
    b = make_supervisor(ctx, provider, holder="sup-b")
    await b.tick()
    first = client.get(f"/v1/attempts/{attempt_id}").json()
    assert first["state"] == "failed" and first["exit_class"] == "environment"
    assert await run_to_settled(b, client, task_id) == "publishing"
    assert len(_attempts(client, task_id)) == 2
