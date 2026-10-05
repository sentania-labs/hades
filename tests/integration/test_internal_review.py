"""Operator out-of-band review uploads against a published PR."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.application.supervisor import Supervisor
from tests.integration import test_github_delivery as delivery
from tests.integration.conftest import (
    event_kinds,
    submit_and_start,
    upload_review,
)
from tests.integration.test_github_delivery import publish

app_key = delivery.app_key
github = delivery.github
github_client = delivery.github_client
publisher = delivery.publisher
delivery_supervisor = delivery.delivery_supervisor

pytestmark = pytest.mark.integration

REVIEW_EXECUTION = {
    "harness": "codex",
    "model": "gpt-5.6-luna",
    "provider": "fake",
    "image": "crucible-worker:fake-review",
    "timeout_seconds": 600,
    "rationale": "A non-author review of the collected head.",
}


def gates(client: TestClient, attempt_id: str) -> dict[str, str]:
    body = client.get(f"/v1/attempts/{attempt_id}/gates").json()
    return {row["gate"]: row["result"] for row in body["items"]}


async def test_a_request_changes_verdict_does_not_move_the_task_by_itself(
    client: TestClient, delivery_supervisor: Supervisor
) -> None:
    """Operator findings are retained without moving the published task."""
    task_id, _ = await publish(client, delivery_supervisor)
    assert upload_review(client, task_id, verdict="request_changes").status_code == 200
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_external_review"
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["review_reports"][0]["verdict"] == "request_changes"
    assert view["review_reports"][0]["findings"] == 1


async def test_a_review_for_another_head_is_refused(
    client: TestClient, delivery_supervisor: Supervisor
) -> None:
    task_id, _ = await publish(client, delivery_supervisor)
    r = upload_review(client, task_id, head_sha="e" * 40)
    assert r.status_code == 422
    assert any(e["path"] == "reviewed_head_sha" for e in r.json()["errors"])
    assert "review_report_rejected" in event_kinds(client, task_id)


async def test_an_uploaded_report_may_not_claim_a_review_execution(
    client: TestClient, delivery_supervisor: Supervisor
) -> None:
    """reviewer_must_not_be_author is mechanical (11): an upload is the orchestrator's,
    and only Crucible may record a report as a review execution's."""
    task_id, _ = await publish(client, delivery_supervisor)
    view = client.get(f"/v1/tasks/{task_id}").json()
    author = view["latest_attempt"]["id"]
    report = {
        "schema_version": "1.0",
        "task_external_id": view["external_id"],
        "reviewed_head_sha": view["head_sha"],
        "reviewer": {"kind": "crucible_review_execution", "attempt_id": author},
        "verdict": "approve",
        "findings": [],
        "summary": "I reviewed myself.",
    }
    r = client.post(f"/v1/tasks/{task_id}/review", json={"report": report})
    assert r.status_code == 403
    assert r.json()["errors"][0]["path"] == "reviewer.kind"
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_external_review"
    assert "review_report_rejected" in event_kinds(client, task_id)


async def test_an_unparsable_review_is_refused_with_paths(
    client: TestClient, delivery_supervisor: Supervisor
) -> None:
    task_id, _ = await publish(client, delivery_supervisor)
    r = client.post(f"/v1/tasks/{task_id}/review", json={"report": {"schema_version": "1.0"}})
    assert r.status_code == 422
    assert {tuple(e["loc"]) for e in r.json()["errors"]} >= {("verdict",), ("summary",)}


async def test_review_is_refused_outside_awaiting_internal_review(client: TestClient) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    r = client.post(f"/v1/tasks/{task_id}/review", json={"execution": REVIEW_EXECUTION})
    assert r.status_code == 409


async def test_review_body_must_name_exactly_one_of_report_or_execution(
    client: TestClient, delivery_supervisor: Supervisor
) -> None:
    task_id, _ = await publish(client, delivery_supervisor)
    assert client.post(f"/v1/tasks/{task_id}/review", json={}).status_code == 422
    both = {"report": {"schema_version": "1.0"}, "execution": REVIEW_EXECUTION}
    assert client.post(f"/v1/tasks/{task_id}/review", json=both).status_code == 422
