from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.submit_task import parse_contract
from crucible.application.supervisor import Supervisor
from crucible.contracts.common import to_document
from crucible.contracts.task_contract import contract_sha256
from crucible.domain.entities import Task, TaskContract
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.execution import LogChunk
from tests.fixtures import FakeClock, contract_document
from tests.integration.conftest import event_kinds, submit_and_start

pytestmark = pytest.mark.integration


async def test_log_offset_paging_returns_only_new_bytes(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-hang", external_id="LOG-PAGE")
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]

    first = client.get(f"/v1/attempts/{attempt_id}/logs", params={"stream": "stdout"})
    assert first.status_code == 200
    assert b"fake worker hang start" in first.content
    offset = int(first.headers["x-crucible-log-offset"])

    supervisor._store_logs(attempt_id, (LogChunk("stdout", b"second page\n"),))
    second = client.get(
        f"/v1/attempts/{attempt_id}/logs",
        params={"stream": "stdout", "offset": offset},
    )
    assert second.content == b"second page\n"
    assert int(second.headers["x-crucible-log-offset"]) > offset


async def test_stall_warns_then_drains_and_kills_as_timeout(
    client: TestClient,
    supervisor: Supervisor,
    provider: FakeProvider,
    clock: FakeClock,
    ctx: AppContext,
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-hang", external_id="STALL-1")
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    with ctx.uow_factory() as uow:
        signals = [item.signal for item in uow.heartbeats.list_for_attempt(attempt_id)]
        initial_activity = uow.heartbeats.latest_activity(attempt_id)
    assert "container_running" in signals
    assert initial_activity is not None and initial_activity.signal == "log_advanced"

    clock.advance(301)
    await supervisor.tick()
    assert event_kinds(client, task_id).count("worker_quiet") == 1
    wakes = client.get("/v1/wakes").json()["items"]
    assert any(wake["reason"] == "stall_warning" for wake in wakes)
    assert client.get(f"/v1/attempts/{attempt_id}").json()["heartbeat_summary"]["state"] == "quiet"

    clock.advance(1500)
    await supervisor.tick()
    worker = provider.worker(attempt_id)
    assert worker is not None and worker.drains == 1
    clock.advance(61)
    await supervisor.tick()
    await supervisor.tick()
    attempt = client.get(f"/v1/attempts/{attempt_id}").json()
    assert attempt["exit_class"] == "stalled"
    assert attempt["termination_reason"] == "stall"
    assert attempt["heartbeat_summary"]["state"] == "stalled"
    assert "worker_stalled" in event_kinds(client, task_id)


def _stored_as_submitted(ctx: AppContext, like: str, document: dict[str, Any]) -> str:
    """A task and its contract, written the way submission writes them: a task row and
    a version 1 contract row, appended. hades #564: submission refuses a contract naming
    another task's branch now, so a second task on one branch, as one submitted before
    that refusal existed, is seeded here. `task_contracts` is append-only; a stored
    contract is never updated, here or anywhere."""
    contract = parse_contract(document)
    stored = to_document(contract)
    now = ctx.clock.now()
    with ctx.uow_factory() as uow:
        sibling = uow.tasks.get(like)
        assert sibling is not None
        task = Task(
            id=new_id(),
            external_id=contract.external_id,
            principal_id=sibling.principal_id,
            project=contract.project,
            title=contract.title,
            state=TaskState.SUBMITTED,
            contract_version=1,
            policy_name=contract.policy.name,
            policy_version=contract.policy.version,
            repository_id=sibling.repository_id,
            created_at=now,
            updated_at=now,
        )
        uow.tasks.add(task)
        uow.contracts.add(
            TaskContract(
                id=new_id(),
                task_id=task.id,
                version=1,
                document=stored,
                sha256=contract_sha256(stored),
                submitted_at=now,
            )
        )
        uow.commit()
    return task.id


async def test_second_checkout_waits_once_and_launches_after_release(
    client: TestClient,
    supervisor: Supervisor,
    clock: FakeClock,
    ctx: AppContext,
) -> None:
    first = submit_and_start(client, "crucible-worker:fake-hang", external_id="LEASE-A")
    document = contract_document(external_id="LEASE-B")
    document["execution_request"]["image"] = "crucible-worker:fake-succeed"
    document["repository"]["work_branch"] = "crucible/LEASE-A"
    # A branch belongs to one task (hades #564), so this second task on LEASE-A's branch
    # cannot be submitted; it is stored as one from before the refusal, and the checkout
    # lease of 10 still keeps the two attempts off one checkout.
    second = _stored_as_submitted(ctx, first, document)
    response = client.post(
        f"/v1/tasks/{second}/start",
        json={"provider": "fake", "image": "crucible-worker:fake-succeed", "policy_version": 2},
    )
    assert response.status_code == 200, response.text

    await supervisor.tick()
    blocked = client.get(f"/v1/tasks/{second}").json()["latest_attempt"]
    assert blocked["state"] == "pending"
    await supervisor.tick()
    assert event_kinds(client, second).count("checkout_lease_denied") == 1

    response = client.post(
        f"/v1/tasks/{first}/cancel",
        json={"reason": "lease test", "verbatim": "stop lease holder", "decided_by": "tests"},
    )
    assert response.status_code == 200
    await supervisor.tick()
    clock.advance(61)
    await supervisor.tick()
    await supervisor.tick()
    await supervisor.tick()
    launched = client.get(f"/v1/tasks/{second}").json()["latest_attempt"]
    assert launched["state"] != "pending"
