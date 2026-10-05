"""C2 acceptance (20): a scripted client reconstructs the whole state from the API alone.

Nothing here reads the database, the supervisor object, or a log file. Everything the
client knows comes from `/v1` responses, which is what Foundry has after a disconnect."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.application.supervisor import Supervisor
from crucible.domain.gates import DEFERRED_TO_C3
from tests.integration.conftest import (
    ARTIFACTS_DELIVERABLE,
    run_to_settled,
    run_until,
    submit_and_start,
)

pytestmark = pytest.mark.integration


def reconstruct(client: TestClient, task_id: str) -> dict[str, Any]:
    """Rebuild the task's story from the API. This is the scripted client."""
    task = client.get(f"/v1/tasks/{task_id}").json()
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    attempts = [
        client.get(f"/v1/attempts/{a['id']}").json()
        for execution in task["executions"]
        for a in execution["attempts"]
    ]
    gates: dict[str, dict[str, str]] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}
    artifacts: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        rows = client.get(f"/v1/attempts/{attempt['id']}/gates").json()["items"]
        gates[attempt["id"]] = {r["gate"]: r["result"] for r in rows}
        evidence[attempt["id"]] = client.get(f"/v1/attempts/{attempt['id']}/evidence").json()[
            "items"
        ]
        artifacts[attempt["id"]] = client.get(f"/v1/attempts/{attempt['id']}/artifacts").json()[
            "items"
        ]
    return {
        "state": task["state"],
        "head_sha": task["head_sha"],
        "pull_request": task["pull_request"],
        "contract_version": task["contract_version"],
        "contract_versions": [v["version"] for v in task["contract_versions"]],
        "executions": [(e["role"], e["state"]) for e in task["executions"]],
        "attempts": [(a["number"], a["state"], a["exit_class"]) for a in attempts],
        "gates": gates,
        "evidence": evidence,
        "artifacts": artifacts,
        "reports": [a["report"] for a in attempts],
        "review_reports": task["review_reports"],
        "acceptance_results": task["acceptance_results"],
        "decisions": task["decisions"],
        "escalations": task["open_escalations"],
        "gate_summary": task["gate_summary"],
        "events": [e["kind"] for e in events],
        "wakes": [
            (w["reason"], w["acked_at"] is not None)
            for w in client.get("/v1/wakes", params={"include_acked": True}).json()["items"]
            if w["task_id"] == task_id
        ],
    }


async def test_a_client_reconstructs_the_whole_run_from_the_api(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "accepted"

    view = reconstruct(client, task_id)
    assert view["state"] == "accepted" and view["pull_request"] is None
    assert view["head_sha"] and len(view["head_sha"]) == 40
    assert view["executions"] == [("implement", "succeeded")]
    assert view["attempts"] == [(1, "succeeded", "completed")]
    (attempt_id,) = list(view["gates"])
    results = view["gates"][attempt_id]
    assert {g for g, r in results.items() if r == "pending"} == set(DEFERRED_TO_C3)
    assert view["gate_summary"]["failing"] == []
    assert view["reports"][0]["parsed_ok"] is True
    assert view["review_reports"] == []
    assert view["acceptance_results"][0]["verdict"] == "accepted"
    assert view["wakes"] == []
    assert view["events"][0] == "task_submitted"
    assert "task_accepted" in view["events"]
    # The claim the worker asserted is visible and marked unverified.
    worker = [e for e in view["evidence"][attempt_id] if e["source"] == "worker"]
    assert len(worker) == 1 and worker[0]["verified"] is False

    # A fresh client with only the task id rebuilds exactly the same picture.
    assert reconstruct(client, task_id) == view


async def test_a_client_reconstructs_a_correction_loop(
    client: TestClient, supervisor: Supervisor
) -> None:
    from tests.integration.conftest import correction_document  # noqa: PLC0415

    task_id = submit_and_start(
        client, "crucible-worker:fake-prohibited-path", deliverables=ARTIFACTS_DELIVERABLE
    )
    await run_to_settled(supervisor, client, task_id)
    failed = reconstruct(client, task_id)
    assert failed["state"] == "pre_pr_gates_failed"
    assert failed["gate_summary"]["failing"] == ["scope_contained"]

    client.post(
        f"/v1/tasks/{task_id}/corrections",
        json=correction_document(client, task_id, image="crucible-worker:fake-succeed"),
    )
    await run_to_settled(supervisor, client, task_id)
    client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "accepted", "reasoning": "The correction stayed inside scope."},
    )
    await supervisor.tick()

    view = reconstruct(client, task_id)
    assert view["state"] == "accepted"
    assert view["contract_versions"] == [1, 2] and view["contract_version"] == 2
    # The first execution succeeded as an execution: the worker exited 0 with a report.
    # The gates are what refused it, which is exactly the separation 11 draws.
    assert view["executions"] == [("implement", "succeeded"), ("correct", "succeeded")]
    assert view["head_sha"] != failed["head_sha"]
    # Both attempts keep their gate rows, so the history is readable, not overwritten.
    assert len(view["gates"]) == 2
    failing_rows = [
        gate for rows in view["gates"].values() for gate, result in rows.items() if result == "fail"
    ]
    assert failing_rows == ["scope_contained"]
    assert view["gate_summary"]["failing"] == []
    assert "task_correction_attached" in view["events"]


async def test_a_client_reconstructs_a_blocked_task_and_its_decision(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-blocked")
    assert await run_until(supervisor, client, task_id, {"blocked"}) == "blocked"
    blocked = reconstruct(client, task_id)
    assert blocked["state"] == "blocked"
    assert len(blocked["escalations"]) == 1
    assert [reason for reason, _ in blocked["wakes"]] == ["blocked"]

    escalation_id = blocked["escalations"][0]["id"]
    client.post(
        f"/v1/tasks/{task_id}/decisions",
        json={
            "kind": "scope_clarified",
            "verbatim": "Treat the duplicate as a 409 and carry on.",
            "resolves": escalation_id,
            "escalation_id": escalation_id,
            "reschedule": True,
        },
    )
    after = reconstruct(client, task_id)
    assert after["state"] == "scheduled"
    assert after["escalations"] == []
    assert after["decisions"][0]["kind"] == "scope_clarified"
    assert "escalation_closed" in after["events"]
