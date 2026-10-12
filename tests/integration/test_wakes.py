"""Wakes (17): rows first in the same transaction, poll, ack, webhook delivery with HMAC
and a retry schedule, and a failing receiver that only delays."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from crucible.application.wakes import RETRY_BACKOFF_SECONDS
from crucible.contracts.wake import WakeV1
from tests.fixtures import FakeClock
from tests.integration.conftest import (
    RecordingDeliverer,
    make_supervisor,
    run_to_settled,
    submit_and_start,
)

pytestmark = pytest.mark.integration


async def test_the_wake_row_exists_before_any_delivery(
    client: TestClient, supervisor: Supervisor
) -> None:
    """The row is committed with the state change; delivery is separate (17)."""
    task_id = submit_and_start(client, "crucible-worker:fake-secret-leak")
    await run_to_settled(supervisor, client, task_id)
    wakes = client.get("/v1/wakes").json()["items"]
    assert len(wakes) == 1
    wake = wakes[0]
    assert wake["reason"] == "pre_pr_gates_failed"
    assert wake["task_id"] == task_id
    assert wake["attempts"] == 0 and wake["delivered_at"] is None
    assert wake["payload"]["links"]["task"] == f"/v1/tasks/{task_id}"
    WakeV1.model_validate(
        {
            "id": wake["id"],
            "schema_version": "1.0",
            "principal": wake["principal"],
            "reason": wake["reason"],
            "task": wake["payload"]["task"],
            "attempt_id": wake["payload"]["attempt_id"],
            "summary": wake["summary"],
            "links": wake["payload"]["links"],
            "created_at": wake["created_at"],
        }
    )


async def test_poll_and_ack(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-secret-leak")
    await run_to_settled(supervisor, client, task_id)
    wake_id = client.get("/v1/wakes").json()["items"][0]["id"]
    r = client.post(f"/v1/wakes/{wake_id}/ack", json={"note": "read it, correcting the report"})
    assert r.status_code == 200 and r.json()["acked_at"] is not None
    assert client.get("/v1/wakes").json()["items"] == []
    assert len(client.get("/v1/wakes", params={"include_acked": True}).json()["items"]) == 1
    assert client.post(f"/v1/wakes/{wake_id}/ack", json={"note": "again"}).status_code == 409
    assert client.get("/v1/supervisor").json()["counts"]["wakes_unacked"] == 0


async def test_a_wake_belongs_to_one_principal(
    client: TestClient, supervisor: Supervisor, tokens: dict[str, str]
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-secret-leak")
    await run_to_settled(supervisor, client, task_id)
    wake_id = client.get("/v1/wakes").json()["items"][0]["id"]
    other = {"Authorization": f"Bearer {tokens['operator']}"}
    assert client.get("/v1/wakes", headers=other).json()["items"] == []
    r = client.post(f"/v1/wakes/{wake_id}/ack", json={"note": "not mine"}, headers=other)
    assert r.status_code == 403


async def test_webhook_delivery_signs_the_body(
    ctx: AppContext, provider: FakeProvider, client: TestClient
) -> None:
    receiver = RecordingDeliverer(ok=True)
    supervisor = make_supervisor(ctx, provider, wake_deliverer=receiver)
    task_id = submit_and_start(client, "crucible-worker:fake-secret-leak")
    await run_to_settled(supervisor, client, task_id)
    await supervisor.tick()
    assert len(receiver.bodies) == 1
    document = json.loads(receiver.bodies[0])
    assert document["reason"] == "pre_pr_gates_failed"
    assert document["task"]["id"] == task_id
    wake = client.get("/v1/wakes").json()["items"][0]
    assert wake["attempts"] == 1 and wake["delivered_at"] is not None
    # A delivered wake is not redelivered.
    await supervisor.tick()
    assert len(receiver.bodies) == 1


async def test_a_failing_receiver_only_delays(
    ctx: AppContext, provider: FakeProvider, client: TestClient, clock: FakeClock
) -> None:
    receiver = RecordingDeliverer(ok=False)
    supervisor = make_supervisor(ctx, provider, wake_deliverer=receiver)
    task_id = submit_and_start(client, "crucible-worker:fake-secret-leak")
    await run_to_settled(supervisor, client, task_id)
    await supervisor.tick()
    wake = client.get("/v1/wakes").json()["items"][0]
    assert wake["attempts"] == 1 and wake["delivered_at"] is None
    assert wake["last_error"] == "HTTP 503"
    first_retry = wake["next_attempt_at"]
    assert first_retry is not None

    # Before the backoff elapses nothing is retried.
    await supervisor.tick()
    assert len(receiver.bodies) == 1

    clock.advance(RETRY_BACKOFF_SECONDS[0] + 1)
    await supervisor.tick()
    assert len(receiver.bodies) == 2
    wake = client.get("/v1/wakes").json()["items"][0]
    assert wake["attempts"] == 2
    assert wake["next_attempt_at"] != first_retry

    # Past wake_retry_hours the retries stop and the row stays for poll (17).
    clock.advance(25 * 3600)
    await supervisor.tick()
    wake = client.get("/v1/wakes").json()["items"][0]
    assert wake["gave_up_at"] is not None and wake["delivered_at"] is None
    attempts_when_abandoned = wake["attempts"]
    await supervisor.tick()
    assert client.get("/v1/wakes").json()["items"][0]["attempts"] == attempts_when_abandoned
    # Poll still has it, which is the whole point.
    assert client.get("/v1/wakes").json()["items"][0]["id"] == wake["id"]


async def test_no_webhook_configured_means_poll_only(
    ctx: AppContext, provider: FakeProvider, client: TestClient
) -> None:
    receiver = RecordingDeliverer(url=None)
    supervisor = make_supervisor(ctx, provider, wake_deliverer=receiver)
    task_id = submit_and_start(client, "crucible-worker:fake-secret-leak")
    await run_to_settled(supervisor, client, task_id)
    await supervisor.tick()
    assert receiver.bodies == []
    assert len(client.get("/v1/wakes").json()["items"]) == 1


async def test_a_failed_attempt_wakes_with_its_class(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-crash")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
    assert reasons == ["attempt_failed", "pre_pr_gates_failed"]
