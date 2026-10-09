from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from crucible.domain.lifecycle import IllegalTransitionError, TaskState, check_transition
from tests.fixtures import FakeClock
from tests.integration.conftest import (
    ARTIFACTS_DELIVERABLE,
    event_kinds,
    run_to_settled,
    run_until,
    submit_and_start,
)

pytestmark = pytest.mark.integration

CANCEL = {"reason": "scope changed", "verbatim": "stop that one please", "decided_by": "scott"}


def _cancel(client: TestClient, task_id: str) -> dict[str, object]:
    r = client.post(f"/v1/tasks/{task_id}/cancel", json=CANCEL)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def test_cancel_running_task_drains_then_cancels(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider, clock: FakeClock
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-hang")
    await supervisor.tick()
    (attempt,) = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]
    assert _cancel(client, task_id)["state"] == "cancelling"
    await supervisor.tick()
    worker = provider.worker(str(attempt["id"]))
    assert worker is not None and worker.drains == 1
    assert client.get(f"/v1/attempts/{attempt['id']}").json()["state"] == "terminating"
    clock.advance(61)
    await supervisor.tick()
    assert worker.kills == 1
    assert await run_until(supervisor, client, task_id, {"cancelled"}) == "cancelled"
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["executions"][0]["state"] == "cancelled"
    assert view["latest_attempt"]["exit_class"] == "killed"
    assert view["closed_at"] is not None
    kinds = event_kinds(client, task_id)
    assert "task_cancel_requested" in kinds
    task_kinds = [k for k in kinds if k.startswith("task_")]
    assert task_kinds[-1] == "task_cancelled"
    assert "attempt_cancel_kill" in kinds and "attempt_timeout_kill" not in kinds
    assert "report_parsed" not in kinds
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    req = next(e for e in events if e["kind"] == "task_cancel_requested")
    assert req["payload"]["verbatim"] == "stop that one please"
    assert req["principal"] == "orchestrator-principal"


async def test_cancel_submitted_task_is_immediate(client: TestClient) -> None:
    from tests.fixtures import contract_document  # noqa: PLC0415

    r = client.post("/v1/tasks", json=contract_document())
    task_id = r.json()["id"]
    assert _cancel(client, task_id)["state"] == "cancelled"
    # hades #208 item 2: the cancel is a decision handed to Hades, recorded between the
    # request and the state change with the principal, the local time and the verbatim.
    assert event_kinds(client, task_id) == [
        "task_submitted",
        "task_cancel_requested",
        "handoff_recorded",
        "task_cancelled",
    ]
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    handoff = next(e for e in events if e["kind"] == "handoff_recorded")
    assert handoff["payload"]["action"] == "cancel"
    assert handoff["payload"]["direction"] == "foundry_to_hades"
    assert handoff["payload"]["words"] == "stop that one please"
    assert handoff["principal"] == "orchestrator-principal"


async def test_cancel_before_first_tick_creates_nothing(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert _cancel(client, task_id)["state"] == "cancelled"
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "cancelled" and view["executions"] == []


async def test_cancel_racing_launch_never_starts_a_worker(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    """The cancel lands after the attempt was materialized but before it launched."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await supervisor._db(supervisor._lease_step)
    await supervisor._db(supervisor._materialize_scheduled)
    view = client.get(f"/v1/tasks/{task_id}").json()
    (attempt,) = view["executions"][0]["attempts"]
    assert attempt["state"] == "pending"
    assert _cancel(client, task_id)["state"] == "cancelled"
    await supervisor.tick()
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "cancelled"
    assert view["executions"][0]["state"] == "cancelled"
    (attempt,) = view["executions"][0]["attempts"]
    assert attempt["state"] == "failed" and attempt["exit_class"] == "killed"
    assert provider.worker(str(attempt["id"])) is None, "no worker was ever launched"
    kinds = event_kinds(client, task_id)
    assert "attempt_preparing" not in kinds and "transition_rejected" not in kinds
    assert "execution_cancelled" in kinds


async def test_cancel_blocked_task_closes_its_execution(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-blocked")
    assert await run_until(supervisor, client, task_id, {"blocked"}) == "blocked"
    assert _cancel(client, task_id)["state"] == "cancelled"
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["executions"][0]["state"] == "cancelled"
    # The same tick releases the cancelled task's kept workspace (16), after the close.
    kinds = event_kinds(client, task_id)
    assert [k for k in kinds if k != "retention_applied"][-1] == "execution_cancelled"
    assert kinds[-1] == "retention_applied"


async def test_cancel_with_partial_report_is_not_parsed(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    """A worker that exits cleanly during the drain leaves a report; it is kept, not a claim."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed-5")
    await supervisor.tick()
    (attempt,) = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]
    worker = provider.worker(str(attempt["id"]))
    assert worker is not None
    _cancel(client, task_id)
    await supervisor.tick()
    # Simulate the worker finishing its report before the drain signal lands.
    worker.behavior = "succeed"
    worker.remaining = worker.observations + 1
    worker.state = worker.state.__class__.EXITED
    worker.exit_code = 0
    assert await run_until(supervisor, client, task_id, {"cancelled"}) == "cancelled"
    stored = client.get(f"/v1/attempts/{attempt['id']}").json()
    assert stored["report"] is None and stored["exit_class"] == "killed"
    artifacts = client.get(f"/v1/attempts/{attempt['id']}/artifacts").json()["items"]
    partial = [item for item in artifacts if item["type"] == "partial_report"]
    assert len(partial) == 1 and partial[0]["filename"] == "report/report.yaml"
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    collected = next(e for e in events if e["kind"] == "attempt_collected")
    assert collected["payload"]["partial_report_kept_unparsed"] is True


async def test_cancel_waiting_task_is_accepted(client: TestClient, supervisor: Supervisor) -> None:
    """The orchestrator may cancel while publication is waiting for its publisher."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert await run_to_settled(supervisor, client, task_id) == "publishing"
    r = client.post(f"/v1/tasks/{task_id}/cancel", json=CANCEL)
    assert r.status_code == 200 and r.json()["state"] == "cancelled"


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_cancel_from_a_state_the_table_forbids_is_409(
    client: TestClient, supervisor: Supervisor
) -> None:
    """09 gives `accepted` no cancel edge. The API refuses with a problem document and
    records the rejection as an event, and the task keeps its state."""
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "accepted"

    r = client.post(f"/v1/tasks/{task_id}/cancel", json=CANCEL)
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "urn:crucible:problem:transition-not-allowed"
    assert "accepted -> cancelled" in body["detail"]
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "accepted"
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    rejected = [e for e in events if e["kind"] == "transition_rejected"]
    assert len(rejected) == 1 and rejected[0]["payload"]["to"] == "cancelled"


def test_reported_has_no_cancel_edge(client: TestClient) -> None:
    """The transient states between `running` and a resting state are not cancellable."""
    for state in (TaskState.REPORTED, TaskState.GATES_PASSED):
        with pytest.raises(IllegalTransitionError):
            check_transition("task", "t", state, TaskState.CANCELLED)
