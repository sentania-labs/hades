"""The lab keeps running (lab findings of 2026-09-29).

Kept workspaces are released once nothing needs them, so claims no longer fill the
namespace quota; a collection runs beside the tick, so a slow one holds back neither
the lease nor another launch; and a collection the cluster could not answer is tried
again rather than failing the attempt.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application import supervisor as supervisor_module
from tests.fixtures import FakeClock
from tests.integration.conftest import (
    event_kinds,
    make_supervisor,
    run_to_settled,
    run_until,
    submit_and_start,
)

pytestmark = pytest.mark.integration

CANCEL = {"reason": "done with it", "verbatim": "cancel it", "decided_by": "scott"}
RETRYING = {"max_attempts": 3, "retry_on": ["environment"], "cleanup": "policy"}


def _attempts(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    view = client.get(f"/v1/tasks/{task_id}").json()
    return [a for e in view["executions"] for a in e["attempts"]]


def _events(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 200, **({"cursor": cursor} if cursor else {})}
        page = client.get(f"/v1/tasks/{task_id}/events", params=params).json()
        out.extend(page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            return out


def _released(client: TestClient, task_id: str) -> dict[str, str]:
    events = _events(client, task_id)
    return {
        str(e["payload"]["subject"]): str(e["payload"]["reason"])
        for e in events
        if e["kind"] == "retention_applied" and e["payload"].get("kind") == "workspace"
    }


def _cancel(client: TestClient, tokens: dict[str, str], task_id: str) -> None:
    response = client.post(
        f"/v1/tasks/{task_id}/cancel",
        json=CANCEL,
        headers={"Authorization": f"Bearer {tokens['operator']}"},
    )
    assert response.status_code == 200, response.text


# ----- kept workspaces are released ----------------------------------------


async def test_kept_workspaces_go_when_nothing_needs_them_and_not_before(
    client: TestClient,
    tokens: dict[str, str],
    ctx: AppContext,
    provider: FakeProvider,
    clock: FakeClock,
) -> None:
    """Three attempts fail and are kept. The two earlier ones go once the policy's 14
    days have passed; the latest stays however long it has been, because a publication
    or a correction may still read it, until the task is cancelled."""
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(
        client, "crucible-worker:fake-environment", "EX-KEPT", lifecycle=RETRYING
    )
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    attempts = [str(a["id"]) for a in _attempts(client, task_id)]
    assert len(attempts) == 3
    assert set(provider.cleaned) >= set(attempts)
    await supervisor.tick()
    assert provider.released == [] and _released(client, task_id) == {}

    clock.advance(15 * 86400)
    await supervisor.tick()
    assert sorted(provider.released) == sorted(attempts[:2])
    assert _released(client, task_id) == {
        attempts[0]: "retention_window",
        attempts[1]: "retention_window",
    }

    # A provider that cannot remove it now is asked again next tick, not recorded.
    _cancel(client, tokens, task_id)
    provider.release_fails = True
    await supervisor.tick()
    assert attempts[2] not in _released(client, task_id)
    provider.release_fails = False
    await supervisor.tick()
    assert _released(client, task_id)[attempts[2]] == "task_cancelled"

    # Idempotent: nothing is released twice.
    before = list(provider.released)
    await supervisor.tick()
    assert provider.released == before
    await supervisor.stop()


async def test_a_cancelled_tasks_workspace_goes_on_the_next_tick(
    client: TestClient,
    tokens: dict[str, str],
    ctx: AppContext,
    provider: FakeProvider,
) -> None:
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", "EX-DONE")
    assert await run_to_settled(supervisor, client, task_id) == "publishing"
    (attempt,) = _attempts(client, task_id)
    await supervisor.tick()
    assert provider.released == []
    _cancel(client, tokens, task_id)
    await supervisor.tick()
    assert provider.released == [attempt["id"]]
    assert "retention_applied" in event_kinds(client, task_id)
    await supervisor.stop()


# ----- a collection runs beside the tick ------------------------------------


async def test_a_long_collection_holds_back_neither_the_lease_nor_another_launch(
    client: TestClient, ctx: AppContext, provider: FakeProvider, clock: FakeClock
) -> None:
    """Collect used to run inside the tick, up to about 90 minutes: the lease lapsed,
    readiness failed, and nothing else launched meanwhile."""
    supervisor = make_supervisor(ctx, provider, collect_wait_seconds=0.2)
    slow = submit_and_start(client, "crucible-worker:fake-succeed", "EX-SLOW")
    hold = provider.hold_collect("EX-SLOW")
    for _ in range(6):
        await supervisor.tick()
        if supervisor._collects:
            break
    (attempt,) = _attempts(client, slow)
    assert attempt["id"] in supervisor._collects
    assert attempt["state"] in ("running", "terminating")

    other = submit_and_start(client, "crucible-worker:fake-succeed", "EX-OTHER")
    for _ in range(2):
        clock.advance(31)
        started = time.monotonic()
        result = await supervisor.tick()
        assert time.monotonic() - started < 5
        assert result.held
        status = client.get("/v1/supervisor").json()
        assert status["healthy"] is True, status
    assert _attempts(client, other)[0]["started_at"] is not None
    assert "attempt_collected" not in event_kinds(client, slow)

    hold.set()
    # A collection that ends between ticks reports the task then; the next tick's gates
    # move it on.
    review = {"publishing"}
    assert await run_until(supervisor, client, slow, review) == "publishing"
    assert await run_until(supervisor, client, other, review) == "publishing"
    await supervisor.stop()


async def test_a_collection_in_flight_is_abandoned_with_the_lease_and_collected_again(
    client: TestClient, ctx: AppContext, provider: FakeProvider
) -> None:
    supervisor = make_supervisor(ctx, provider, collect_wait_seconds=0.2)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", "EX-ABANDON")
    provider.hold_collect("EX-ABANDON")
    for _ in range(6):
        await supervisor.tick()
        if supervisor._collects:
            break
    assert supervisor._collects
    await supervisor.stop()
    assert not supervisor._collects
    assert "attempt_collected" not in event_kinds(client, task_id)

    successor = make_supervisor(ctx, provider, holder="sup-b")
    review = {"publishing"}
    assert await run_until(successor, client, task_id, review, max_ticks=20) == ("publishing")
    await successor.stop()


# ----- a collection the cluster could not answer is tried again -------------


async def test_a_collection_the_cluster_could_not_answer_is_tried_again(
    client: TestClient,
    ctx: AppContext,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "COLLECT_RETRY_INTERVAL_SECONDS", 0)
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", "EX-FLAKY")
    provider.collect_unavailable("EX-FLAKY", times=2)
    review = {"publishing"}
    assert await run_until(supervisor, client, task_id, review) == "publishing"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "completed"
    assert provider.collect_calls.count(attempt["id"]) == 3
    await supervisor.stop()


async def test_a_collection_that_never_gets_an_answer_fails_as_environment_in_the_end(
    client: TestClient,
    ctx: AppContext,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "COLLECT_RETRY_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(supervisor_module, "COLLECT_RETRY_WINDOW_SECONDS", 0)
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-succeed",
        "EX-DOWN",
        lifecycle={"max_attempts": 1, "retry_on": [], "cleanup": "policy"},
    )
    provider.collect_unavailable("EX-DOWN", times=100)
    failed = {"pre_pr_gates_failed"}
    assert await run_until(supervisor, client, task_id, failed) == "pre_pr_gates_failed"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "environment"
    await supervisor.stop()


async def test_the_tick_is_not_waiting_on_a_held_collection(
    client: TestClient, ctx: AppContext, provider: FakeProvider
) -> None:
    """The bounded wait: a tick waits at most `collect_wait_seconds` for a collection
    it started, whatever the collection does."""
    supervisor = make_supervisor(ctx, provider, collect_wait_seconds=0.2)
    submit_and_start(client, "crucible-worker:fake-succeed", "EX-BOUNDED")
    hold = provider.hold_collect("EX-BOUNDED")
    for _ in range(6):
        started = time.monotonic()
        await supervisor.tick()
        assert time.monotonic() - started < 3
    hold.set()
    await asyncio.sleep(0)
    await supervisor.stop()


# ----- review of 2026-09-29: what runs after a collection -------------------


async def test_a_quota_checkpoint_push_in_flight_is_neither_cleaned_nor_pushed_twice(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    provider: FakeProvider,
) -> None:
    """The collection task pushes a quota checkpoint after it finished the attempt. While
    that push runs, cleanup must not remove the workspace (and the push's Job with it),
    and the next tick's checkpoint resume must not start a second push."""
    from tests.integration.test_class_routing import (  # noqa: PLC0415
        _install_policy,
        _model,
        _promote,
        _submit,
    )

    _install_policy(
        ctx,
        clock,
        version=90,
        models=[
            _model("a-quota-model", "codex", "lab-pool-a"),
            _model("b-success-model", "agy", "lab-pool-b"),
        ],
    )
    _promote(ctx, clock, "codex", "crucible-worker:fake-quota")
    _promote(ctx, clock, "agy", "crucible-worker:fake-succeed")
    supervisor = make_supervisor(ctx, provider, collect_wait_seconds=0.2)
    task_id = _submit(client, "LAB-CHECKPOINT", 90)

    original = supervisor._complete_quota_checkpoint
    release = asyncio.Event()
    pushes: list[str] = []

    async def held_push(attempt_id: str) -> None:
        pushes.append(attempt_id)
        await release.wait()
        await original(attempt_id)

    supervisor._complete_quota_checkpoint = held_push  # type: ignore[method-assign]
    for _ in range(4):
        await supervisor.tick()
        if pushes:
            break
    assert len(pushes) == 1
    attempt_id = pushes[0]
    for _ in range(2):
        await supervisor.tick()
    assert pushes == [attempt_id], "a second push of the same checkpoint started"
    assert attempt_id not in provider.cleaned, "cleanup ran under a push in flight"

    release.set()
    for _ in range(6):
        await supervisor.tick()
        if attempt_id in provider.cleaned:
            break
    assert attempt_id in provider.cleaned
    assert any(e["kind"] == "task_rerouted" for e in _events(client, task_id))
    await supervisor.stop()


async def test_a_workspace_cleanup_deleted_is_never_released_again(
    client: TestClient,
    tokens: dict[str, str],
    ctx: AppContext,
    provider: FakeProvider,
) -> None:
    """Only a kept workspace is the retention step's to release: one the cleanup policy
    already deleted records no second deletion."""
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-succeed",
        "EX-DELETED",
        lifecycle={"max_attempts": 1, "retry_on": [], "cleanup": "policy"},
    )
    assert await run_to_settled(supervisor, client, task_id) == "publishing"
    (attempt,) = _attempts(client, task_id)
    with ctx.uow_factory() as uow:
        cleaned = [
            e
            for e in uow.events.list_for_task(task_id, after_seq=0, limit=500)
            if e.kind == "attempt_cleaned_up"
        ]
    assert cleaned and cleaned[0].payload["workspace"] == "keep_diff_only"
    # The same attempt, recorded as deleted at cleanup, drops out of the sweep.
    from sqlalchemy import text  # noqa: PLC0415

    assert ctx.engine is not None
    # The event log is append-only and fenced; the test sets both guards aside for one
    # rewrite, as the migrations do, rather than run a second policy version.
    with ctx.engine.begin() as connection:
        for trigger in ("trg_events_append_only", "trg_events_fenced"):
            connection.execute(text(f"ALTER TABLE events DISABLE TRIGGER {trigger}"))
        connection.execute(
            text(
                "UPDATE events SET payload = jsonb_set(payload, '{workspace}', '\"delete\"') "
                "WHERE kind = 'attempt_cleaned_up' AND attempt_id = :id"
            ),
            {"id": attempt["id"]},
        )
        for trigger in ("trg_events_append_only", "trg_events_fenced"):
            connection.execute(text(f"ALTER TABLE events ENABLE TRIGGER {trigger}"))
    _cancel(client, tokens, task_id)
    for _ in range(3):
        await supervisor.tick()
    assert provider.released == []
    assert _released(client, task_id) == {}
    await supervisor.stop()


async def test_a_cancel_waits_for_a_collection_between_its_retries(
    client: TestClient,
    tokens: dict[str, str],
    ctx: AppContext,
    provider: FakeProvider,
) -> None:
    """An attempt waiting to be collected again is still its collection's: the cancel
    sweep leaves it be, and the task settles once the collection has finished it."""
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", "EX-BACKOFF")
    provider.collect_unavailable("EX-BACKOFF", times=1)
    for _ in range(6):
        await supervisor.tick()
        if supervisor._collect_retry_at:
            break
    assert supervisor._collect_retry_at
    (attempt,) = _attempts(client, task_id)
    _cancel(client, tokens, task_id)
    await supervisor.tick()
    assert _attempts(client, task_id)[0]["state"] == attempt["state"]
    supervisor._collect_retry_at = {key: 0.0 for key in supervisor._collect_retry_at}
    assert await run_until(supervisor, client, task_id, {"cancelled"}) == "cancelled"
    assert provider.collect_calls.count(attempt["id"]) == 2
    await supervisor.stop()


async def test_the_retention_sweep_leaves_a_finished_attempt_alone_until_its_cleanup(
    client: TestClient, ctx: AppContext, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hades #237 on kind: a finished attempt whose cleanup had not run yet (its
    collection still beside the tick, or a cleanup that failed) was left out of the
    set the provider sweep keeps. Its claim had no retention label yet, so the sweep
    deleted the workspace that acceptance and the publisher read the bundle from."""
    from crucible.ports.execution import ProviderError  # noqa: PLC0415

    kept: list[list[str]] = []

    async def cleanup_refused(*_: Any, **__: Any) -> None:
        raise ProviderError("the fake cannot clean up yet")

    async def recorded(keep: Any) -> int:
        kept.append(list(keep))
        return 0

    monkeypatch.setattr(provider, "cleanup", cleanup_refused)
    monkeypatch.setattr(provider, "retention", recorded)
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", "EX-SWEEP")
    assert await run_to_settled(supervisor, client, task_id) == "publishing"
    (attempt,) = _attempts(client, task_id)
    assert attempt["state"] == "succeeded"
    await supervisor.tick()
    assert attempt["id"] in kept[-1]

    # Once cleanup has run (and labelled or deleted the claim), the sweep may judge it.
    monkeypatch.undo()
    monkeypatch.setattr(provider, "retention", recorded)
    await supervisor.tick()
    assert attempt["id"] in provider.cleaned
    await supervisor.tick()
    assert attempt["id"] not in kept[-1]
    await supervisor.stop()
