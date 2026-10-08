"""Artifact store and upload (04, 12, 14): content addressing, secret refusal, evidence."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.application.supervisor import Supervisor
from tests.integration.conftest import run_to_settled, submit_and_start

pytestmark = pytest.mark.integration


def token_like() -> str:
    """Built at runtime so no secret-shaped literal is ever committed (12)."""
    return "gh" + "p_" + "c" * 36


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_collection_stores_the_claim_and_the_run_evidence(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    items = client.get(f"/v1/attempts/{attempt_id}/artifacts").json()["items"]
    types = sorted(a["type"] for a in items)
    assert types == ["completion_claim", "run_evidence"]
    claim = next(a for a in items if a["type"] == "completion_claim")
    assert len(claim["sha256"]) == 64 and claim["size"] > 0
    assert claim["created_by"] == "crucible"
    content = client.get(f"/v1/artifacts/{claim['id']}/content")
    assert content.status_code == 200
    assert content.headers["X-Crucible-Artifact-Sha256"] == claim["sha256"]
    assert b"task_external_id" in content.content


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_the_parsed_report_is_readable(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    body = client.get(f"/v1/attempts/{attempt_id}/report").json()
    assert body["parsed_ok"] is True and body["parse_errors"] == []
    assert body["document"]["task_external_id"] == "EX-0001"


async def test_evidence_is_readable_and_the_worker_row_is_unverified(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    items = client.get(f"/v1/attempts/{attempt_id}/evidence").json()["items"]
    kinds = {i["kind"] for i in items}
    assert {"exit_info", "diff_paths", "bundle_head", "scanner_result", "artifact_present"} <= kinds
    worker_rows = [i for i in items if i["source"] == "worker"]
    assert len(worker_rows) == 1
    assert worker_rows[0]["verified"] is False
    assert worker_rows[0]["payload"]["role"] == "worker_claim"
    assert all(i["verified"] for i in items if i["source"] == "crucible")


async def test_upload_run_evidence_becomes_evidence(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    r = client.post(
        f"/v1/attempts/{attempt_id}/artifacts",
        params={"type": "run_evidence", "filename": "report/extra-evidence.md"},
        content=b"# Seen working\n\nThe page rendered.\n",
        headers={"Content-Type": "text/markdown"},
    )
    assert r.status_code == 201, r.text
    artifact = r.json()
    assert artifact["created_by"] == "orchestrator-principal"

    # `evidence` is fenced to the supervisor (14), so the request cannot write the row a
    # gate would read. The next tick derives it from the artifact.
    before = client.get(f"/v1/attempts/{attempt_id}/evidence").json()["items"]
    assert not any(e["artifact_id"] == artifact["id"] for e in before)
    await supervisor.tick()
    evidence = client.get(f"/v1/attempts/{attempt_id}/evidence").json()["items"]
    uploaded = [e for e in evidence if e["artifact_id"] == artifact["id"]]
    assert len(uploaded) == 1
    assert uploaded[0]["source"] == "crucible" and uploaded[0]["verified"] is True
    assert uploaded[0]["payload"]["uploaded_by"] == "orchestrator-principal"
    # Running the tick again does not duplicate it.
    await supervisor.tick()
    again = client.get(f"/v1/attempts/{attempt_id}/evidence").json()["items"]
    assert len([e for e in again if e["artifact_id"] == artifact["id"]]) == 1


async def test_an_upload_carrying_a_secret_is_refused(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    before = len(client.get(f"/v1/attempts/{attempt_id}/artifacts").json()["items"])
    r = client.post(
        f"/v1/attempts/{attempt_id}/artifacts",
        params={"type": "run_evidence", "filename": "report/leak.txt"},
        content=f"here is the token {token_like()}\n".encode(),
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code == 422
    message = r.json()["errors"][0]["message"]
    assert "github_token" in message and "ghp_" not in message
    after = client.get(f"/v1/attempts/{attempt_id}/artifacts").json()["items"]
    assert len(after) == before
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    assert any(e["kind"] == "artifact_rejected" for e in events)


async def test_an_unknown_artifact_type_is_refused(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    r = client.post(
        f"/v1/attempts/{attempt_id}/artifacts",
        params={"type": "identity", "filename": "x"},
        content=b"x",
    )
    assert r.status_code == 422


async def test_an_observer_may_not_upload(
    client: TestClient, supervisor: Supervisor, tokens: dict[str, str]
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    r = client.post(
        f"/v1/attempts/{attempt_id}/artifacts",
        params={"type": "run_evidence", "filename": "x.md"},
        content=b"x",
        headers={"Authorization": f"Bearer {tokens['observer']}"},
    )
    assert r.status_code == 403


def test_artifact_and_report_not_found(client: TestClient) -> None:
    for path in (
        "/v1/artifacts/01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "/v1/attempts/01ARZ3NDEKTSV4RRFFQ69G5FAV/gates",
        "/v1/attempts/01ARZ3NDEKTSV4RRFFQ69G5FAV/report",
    ):
        r = client.get(path)
        assert r.status_code == 404 and r.json()["type"] == "urn:crucible:problem:not-found"
