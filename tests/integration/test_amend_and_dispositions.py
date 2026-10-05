"""Amendment, dispositions, and the stale-escalation repeat wake (04, 09, 17)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.application.supervisor import Supervisor
from tests.fixtures import FakeClock
from tests.integration.conftest import (
    ARTIFACTS_DELIVERABLE,
    event_kinds,
    legacy_acceptance_state,
    run_to_settled,
    run_until,
    submit_and_start,
)

pytestmark = pytest.mark.integration


def amended(client: TestClient, task_id: str, **overrides: Any) -> dict[str, Any]:
    document = dict(client.get(f"/v1/tasks/{task_id}").json()["contract"])
    document.update(overrides)
    return document


def test_amend_in_submitted_creates_a_new_version(client: TestClient) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=False)
    document = amended(client, task_id, objective="A narrower objective than before.")
    r = client.post(
        f"/v1/tasks/{task_id}/amend", json={"contract": document, "reason": "scope clarified"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contract_version"] == 2 and body["state"] == "submitted"
    assert [v["version"] for v in body["contract_versions"]] == [1, 2]
    assert body["contract"]["objective"] == "A narrower objective than before."
    assert "task_amended" in event_kinds(client, task_id)


def test_amend_may_not_change_the_task_identity(client: TestClient) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=False)
    document = amended(client, task_id, external_id="EX-SOMEONE-ELSE")
    r = client.post(f"/v1/tasks/{task_id}/amend", json={"contract": document, "reason": "x"})
    assert r.status_code == 422
    assert any(e["path"] == "external_id" for e in r.json()["errors"])


def test_amend_rejects_a_correction_section(client: TestClient) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=False)
    document = amended(
        client,
        task_id,
        correction={
            "of_version": 1,
            "reason": "pre_pr_gates",
            "addresses": [],
            "instructions": "x",
            "resume_from": "remote_branch",
            "request_internal_review": False,
        },
    )
    r = client.post(f"/v1/tasks/{task_id}/amend", json={"contract": document, "reason": "x"})
    assert r.status_code == 422
    assert any(e["path"] == "correction" for e in r.json()["errors"])


async def test_amend_is_refused_outside_the_states_04_allows(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-hang")
    await supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "running"
    document = amended(client, task_id, objective="Too late.")
    r = client.post(f"/v1/tasks/{task_id}/amend", json={"contract": document, "reason": "x"})
    assert r.status_code == 409


async def test_amend_in_awaiting_acceptance_may_not_widen_scope(
    client: TestClient, supervisor: Supervisor
) -> None:
    """The gate results name a head that ran under the previous version (09)."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await legacy_acceptance_state(supervisor, client, task_id)
    document = amended(client, task_id)
    document["scope"] = {**document["scope"], "allowed_paths": ["**"]}
    r = client.post(f"/v1/tasks/{task_id}/amend", json={"contract": document, "reason": "x"})
    assert r.status_code == 422
    assert any(e["path"] == "scope.allowed_paths" for e in r.json()["errors"])


async def test_amend_may_not_turn_a_pull_request_into_an_artifacts_deliverable(
    client: TestClient, supervisor: Supervisor
) -> None:
    """Otherwise a task that must be published walks to `accepted` unpublished."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await legacy_acceptance_state(supervisor, client, task_id)
    document = amended(client, task_id, deliverables=ARTIFACTS_DELIVERABLE)
    r = client.post(f"/v1/tasks/{task_id}/amend", json={"contract": document, "reason": "x"})
    assert r.status_code == 422
    assert any(e["path"] == "deliverables" for e in r.json()["errors"])


async def test_amend_is_refused_once_the_head_is_publishing(
    client: TestClient, supervisor: Supervisor
) -> None:
    """C4 replaces C2's publish_pending flag with the `publishing` state, which is not
    amendable: the head is already on its way to the remote (09)."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await legacy_acceptance_state(supervisor, client, task_id)
    accepted = client.post(
        f"/v1/tasks/{task_id}/accept", json={"verdict": "accepted", "reasoning": "publish it"}
    ).json()
    assert accepted["state"] == "publishing"
    document = amended(client, task_id, objective="A different objective.")
    r = client.post(f"/v1/tasks/{task_id}/amend", json={"contract": document, "reason": "x"})
    assert r.status_code == 409
    assert "publishing" in r.json()["detail"]


async def test_a_correction_is_refused_once_the_head_is_accepted(
    client: TestClient, supervisor: Supervisor
) -> None:
    """A superseded needs_more_work is not a standing request for more work."""
    from tests.integration.conftest import correction_document  # noqa: PLC0415

    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await legacy_acceptance_state(supervisor, client, task_id)
    client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "needs_more_work", "reasoning": "AC2 is not exercised."},
    )
    client.post(
        f"/v1/tasks/{task_id}/accept", json={"verdict": "accepted", "reasoning": "On reflection."}
    )
    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    # The accept moved the task to `publishing`, which is not a correctable state: the
    # accepted head is already being pushed, and correcting it would abandon it (09).
    assert r.status_code == 409
    assert "publishing" in r.json()["detail"]
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "publishing"


def test_a_disposition_needs_a_recorded_review_comment(
    client: TestClient,
) -> None:
    """A disposition attaches to a comment Crucible observed on this task's PR (23)."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=False)
    r = client.post(
        f"/v1/tasks/{task_id}/dispositions",
        json={
            "review_comment_id": "2101",
            "disposition": "fix",
            "reasoning": "The reviewer is right.",
        },
    )
    assert r.status_code == 404
    assert "unknown" in r.json()["detail"]


def test_a_disposition_needs_an_orchestrator_principal(
    client: TestClient, tokens: dict[str, str]
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=False)
    r = client.post(
        f"/v1/tasks/{task_id}/dispositions",
        json={"review_comment_id": "2101", "disposition": "fix", "reasoning": "x"},
        headers={"Authorization": f"Bearer {tokens['observer']}"},
    )
    assert r.status_code == 403


def test_an_unknown_disposition_kind_is_refused(client: TestClient) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=False)
    r = client.post(
        f"/v1/tasks/{task_id}/dispositions",
        json={"review_comment_id": "2101", "disposition": "ignore", "reasoning": "x"},
    )
    assert r.status_code == 422


async def test_a_stale_escalation_repeats_its_wake_once_per_window(
    client: TestClient, supervisor: Supervisor, clock: FakeClock
) -> None:
    """09: an escalation older than escalation_stale_hours produces a repeat wake, not a
    state change."""
    task_id = submit_and_start(client, "crucible-worker:fake-blocked")
    assert await run_until(supervisor, client, task_id, {"blocked"}) == "blocked"
    reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
    assert reasons == ["blocked"]

    # Inside the window nothing repeats, however many ticks run.
    await supervisor.tick()
    await supervisor.tick()
    assert len(client.get("/v1/wakes").json()["items"]) == 1

    clock.advance(25 * 3600)
    await supervisor.tick()
    reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
    assert reasons == ["blocked", "escalation_stale"]
    # And the repeat is once per window, not once per tick.
    await supervisor.tick()
    assert len(client.get("/v1/wakes").json()["items"]) == 2
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "blocked"


async def test_close_an_accepted_task(client: TestClient, supervisor: Supervisor) -> None:
    """04: the orchestrator closes an accepted task; 09 makes `closed` terminal."""
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    await run_to_settled(supervisor, client, task_id)
    r = client.post(f"/v1/tasks/{task_id}/close", json={"note": "Foundry recorded the outcome."})
    assert r.status_code == 200 and r.json()["state"] == "closed"
    assert r.json()["closed_at"] is not None
    again = client.post(f"/v1/tasks/{task_id}/close", json={"note": "twice"})
    assert again.status_code == 409


def test_close_is_refused_on_a_task_that_never_ran(client: TestClient) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=False)
    r = client.post(f"/v1/tasks/{task_id}/close", json={"note": "nothing happened"})
    assert r.status_code == 409
