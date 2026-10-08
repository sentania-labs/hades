"""The external review round on PR #16: six findings, all dispositioned fix.

One test per finding, named so the disposition is traceable to what proves it."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from crucible.contracts.task_contract import TaskContractV1
from crucible.domain.gates import GateName
from tests.integration.conftest import (
    ARTIFACTS_DELIVERABLE,
    correction_document,
    event_kinds,
    legacy_acceptance_state,
    run_to_settled,
    run_until,
    submit_and_start,
)

pytestmark = pytest.mark.integration

REVIEW_EXECUTION = {
    "harness": "codex",
    "model": "gpt-5.6-luna",
    "provider": "fake",
    "image": "crucible-worker:fake-review",
    "timeout_seconds": 600,
    "rationale": "A non-author review of the collected head.",
}


def contract_of(client: TestClient, task_id: str) -> dict[str, Any]:
    return dict(client.get(f"/v1/tasks/{task_id}").json()["contract"])


def gates(client: TestClient, attempt_id: str) -> dict[str, str]:
    return {
        row["gate"]: row["result"]
        for row in client.get(f"/v1/attempts/{attempt_id}/gates").json()["items"]
    }


def unblock(client: TestClient, task_id: str, **overrides: Any) -> Any:
    escalation = client.get(f"/v1/tasks/{task_id}").json()["open_escalations"][0]
    body = {
        "kind": "scope_clarified",
        "verbatim": "Treat the duplicate as a 409 and carry on.",
        "resolves": escalation["id"],
        "escalation_id": escalation["id"],
        "reschedule": True,
    }
    body.update(overrides)
    return client.post(f"/v1/tasks/{task_id}/decisions", json=body)


async def test_orchestrator_cannot_add_a_pin_by_amendment_or_correction(
    client: TestClient, supervisor: Supervisor
) -> None:
    amend_task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", external_id="C6B-PIN-AMEND", start=False
    )
    amendment = contract_of(client, amend_task_id)
    amendment["execution_request"].update(
        {
            "harness": "codex",
            "model": "gpt-5.6-luna",
            "pin_reason": "orchestrator must not be allowed to pin",
        }
    )
    refused = client.post(
        f"/v1/tasks/{amend_task_id}/amend",
        json={"contract": amendment, "reason": "try to pin"},
    )
    assert refused.status_code == 403
    assert "only an operator may" in refused.json()["detail"]

    correction_task_id = submit_and_start(
        client, "crucible-worker:fake-no-report", external_id="C6B-PIN-CORRECTION"
    )
    assert await run_to_settled(supervisor, client, correction_task_id) == "pre_pr_gates_failed"
    correction = correction_document(
        client, correction_task_id, image="crucible-worker:fake-succeed"
    )
    correction["execution_request"].update(
        {
            "harness": "codex",
            "model": "gpt-5.6-luna",
            "pin_reason": "orchestrator must not be allowed to pin",
        }
    )
    refused = client.post(f"/v1/tasks/{correction_task_id}/corrections", json=correction)
    assert refused.status_code == 403
    assert "only an operator may" in refused.json()["detail"]


async def test_amendment_and_correction_refuse_a_provider_this_deployment_does_not_run(
    ctx: AppContext, tokens: dict[str, str], client: TestClient, supervisor: Supervisor
) -> None:
    """crucible#124: every path that stores a contract version refuses an unwired
    provider, not only submit. Each task is created while the fake provider is wired, then
    amended or corrected through a deployment that runs no provider (test fixtures off)."""
    amend_task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", external_id="UNWIRED-AMEND", start=False
    )
    correction_task_id = submit_and_start(
        client, "crucible-worker:fake-verification-fails", external_id="UNWIRED-CORRECTION"
    )
    assert await run_to_settled(supervisor, client, correction_task_id) == "pre_pr_gates_failed"
    amendment = contract_of(client, amend_task_id)
    correction = correction_document(
        client, correction_task_id, image="crucible-worker:fake-succeed"
    )

    production = dataclasses.replace(ctx, providers=[])
    with TestClient(
        create_app(production), headers={"Authorization": f"Bearer {tokens['orchestrator']}"}
    ) as unwired:
        amended = unwired.post(
            f"/v1/tasks/{amend_task_id}/amend",
            json={"contract": amendment, "reason": "same contract, fixtures now off"},
        )
        corrected = unwired.post(f"/v1/tasks/{correction_task_id}/corrections", json=correction)

    for response in (amended, corrected):
        assert response.status_code == 422, response.text
        assert "execution_request.provider" in [e["path"] for e in response.json()["errors"]]
    assert contract_of(client, amend_task_id) == amendment
    view = client.get(f"/v1/tasks/{correction_task_id}").json()
    assert (view["state"], view["contract_version"]) == ("pre_pr_gates_failed", 1)

    # The same versions through the deployment that wires the fake provider are accepted,
    # so the refusal above is the provider check and nothing else.
    assert (
        client.post(
            f"/v1/tasks/{amend_task_id}/amend",
            json={"contract": amendment, "reason": "fixtures on"},
        ).status_code
        == 200
    )
    assert (
        client.post(f"/v1/tasks/{correction_task_id}/corrections", json=correction).status_code
        == 200
    )


# ----- 1: a decision on a blocked task must create new work --------------------


async def test_a_decision_on_a_blocked_task_launches_a_new_attempt(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    """09: `blocked --decision--> scheduled`. Scheduled means work starts, not that the
    task sits matching the execution that already blocked."""
    task_id = submit_and_start(client, "crucible-worker:fake-blocked")
    assert await run_until(supervisor, client, task_id, {"blocked"}) == "blocked"
    first = client.get(f"/v1/tasks/{task_id}").json()
    assert len(first["executions"][0]["attempts"]) == 1

    # The decision answers the question, so the next attempt can get further.
    provider.script("EX-0001", "succeed")
    assert unblock(client, task_id).status_code == 201

    await supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] != "scheduled", "the tick must create work, not leave the task scheduled"
    assert [a["number"] for a in view["executions"][0]["attempts"]] == [1, 2]
    assert "execution_resumed" in event_kinds(client, task_id)

    state = await run_to_settled(supervisor, client, task_id)
    assert state == "publishing"
    attempts = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"]
    assert [(a["number"], a["state"]) for a in attempts] == [(1, "blocked"), (2, "succeeded")]


async def test_a_decision_without_reschedule_leaves_the_task_blocked(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-blocked")
    await run_until(supervisor, client, task_id, {"blocked"})
    assert unblock(client, task_id, reschedule=False).status_code == 201
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "blocked"
    assert len(view["executions"][0]["attempts"]) == 1


# ----- 3: a later contract version satisfies the submit-time rules -------------


async def test_an_amendment_to_a_disabled_model_is_refused(
    client: TestClient, supervisor: Supervisor, tokens: dict[str, str]
) -> None:
    # The seeded roster (routing version 2) has every model enabled, so this test
    # uploads scratch version 50 with one model disabled and a matching policy.
    admin = {"Authorization": f"Bearer {tokens['admin']}"}
    routing = client.get("/v1/routing/default-routing/2").json()["document"]
    routing["version"] = 50
    disabled = next(m for m in routing["models"] if m["model"] == "gemini-3.8-flash-low")
    disabled["enabled"] = False
    assert (
        client.put("/v1/routing/default-routing/50", json=routing, headers=admin).status_code == 200
    )
    policy = client.get("/v1/policies/default-software/2").json()["document"]
    policy["version"] = 50
    policy["routing"]["policy"]["version"] = 50
    assert (
        client.put("/v1/policies/default-software/50", json=policy, headers=admin).status_code
        == 200
    )
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-succeed",
        start=False,
        policy={"name": "default-software", "version": 50},
    )
    document = contract_of(client, task_id)
    document["execution_request"] = {
        **document["execution_request"],
        "model": "gemini-3.8-flash-low",
        "harness": "agy",
        "pin_reason": "exercise disabled pinned model validation",
        "tier": "trivial",
    }
    r = client.post(
        f"/v1/tasks/{task_id}/amend",
        json={"contract": document, "reason": "x"},
        headers={"Authorization": f"Bearer {tokens['operator']}"},
    )
    assert r.status_code == 422
    assert any("disabled" in e["message"] for e in r.json()["errors"])


async def test_an_amendment_to_an_image_outside_the_allowlist_is_refused(
    client: TestClient,
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=False)
    document = contract_of(client, task_id)
    document["execution_request"] = {
        **document["execution_request"],
        "image": "docker.io/library/python:3.12",
    }
    r = client.post(f"/v1/tasks/{task_id}/amend", json={"contract": document, "reason": "x"})
    assert r.status_code == 422
    assert any(e["path"] == "execution_request.image" for e in r.json()["errors"])


async def test_an_amendment_beyond_the_policy_timeout_is_refused(client: TestClient) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=False)
    document = contract_of(client, task_id)
    document["execution_request"] = {**document["execution_request"], "timeout_seconds": 999999}
    r = client.post(f"/v1/tasks/{task_id}/amend", json={"contract": document, "reason": "x"})
    assert r.status_code == 422
    assert any(e["path"] == "execution_request.timeout_seconds" for e in r.json()["errors"])


async def test_a_correction_to_a_disallowed_image_is_refused(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-prohibited-path")
    await run_to_settled(supervisor, client, task_id)
    document = correction_document(client, task_id, image="docker.io/library/python:3.12")
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert r.status_code == 422
    assert any(e["path"] == "execution_request.image" for e in r.json()["errors"])


async def test_a_correction_beyond_the_attempt_cap_is_refused(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-prohibited-path")
    await run_to_settled(supervisor, client, task_id)
    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    document["lifecycle"] = {**document["lifecycle"], "max_attempts": 99}
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert r.status_code == 422
    assert any(e["path"] == "lifecycle.max_attempts" for e in r.json()["errors"])


# ----- 4: a proof-affecting amendment in awaiting_acceptance --------------------


async def test_a_non_proof_amendment_in_awaiting_acceptance_is_allowed(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await legacy_acceptance_state(supervisor, client, task_id)
    document = contract_of(client, task_id)
    document["title"] = "A clearer title for the same work"
    document["context"] = [*document["context"], {"kind": "doc", "ref": "docs/ledger.md"}]
    r = client.post(f"/v1/tasks/{task_id}/amend", json={"contract": document, "reason": "context"})
    assert r.status_code == 200, r.text
    assert r.json()["contract_version"] == 2 and r.json()["state"] == "awaiting_acceptance"


@pytest.mark.parametrize("field", ["acceptance_criteria", "required_verification"])
async def test_a_proof_affecting_amendment_in_awaiting_acceptance_is_refused(
    client: TestClient, supervisor: Supervisor, field: str
) -> None:
    """The recorded gate results answered the previous version's question. Changing it
    would leave a `pass` standing for a question that was never asked (11)."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await legacy_acceptance_state(supervisor, client, task_id)
    document = contract_of(client, task_id)
    if field == "acceptance_criteria":
        document[field] = [
            *document[field],
            {"id": "AC3", "text": "A criterion nothing was ever checked against."},
        ]
    else:
        document[field] = [
            *document[field],
            {"id": "V5", "command": "make test-unit", "expect_exit": 0, "kind": "command"},
        ]
    # Keep the replacement valid so the refusal exercises the amendment rule.
    TaskContractV1.model_validate(document)
    r = client.post(f"/v1/tasks/{task_id}/amend", json={"contract": document, "reason": "x"})
    assert r.status_code == 422
    assert any(e["path"] == field for e in r.json()["errors"])
    assert "correction" in " ".join(e["message"] for e in r.json()["errors"])
    assert client.get(f"/v1/tasks/{task_id}").json()["contract_version"] == 1


# ----- 5: of_version names the version the task is on --------------------------


async def test_a_correction_naming_an_older_version_is_refused(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client, "crucible-worker:fake-prohibited-path", deliverables=ARTIFACTS_DELIVERABLE
    )
    await run_to_settled(supervisor, client, task_id)
    first = correction_document(client, task_id, image="crucible-worker:fake-prohibited-path")
    assert client.post(f"/v1/tasks/{task_id}/corrections", json=first).status_code == 200
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    assert client.get(f"/v1/tasks/{task_id}").json()["contract_version"] == 2

    stale = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    stale["correction"] = {**stale["correction"], "of_version": 1}
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=stale)
    assert r.status_code == 422
    assert any(e["path"] == "correction.of_version" for e in r.json()["errors"])
    assert client.get(f"/v1/tasks/{task_id}").json()["contract_version"] == 2


async def test_narrowing_is_compared_against_the_current_version(
    client: TestClient, supervisor: Supervisor
) -> None:
    """A second correction may not re-widen back to what version 1 allowed."""
    task_id = submit_and_start(client, "crucible-worker:fake-prohibited-path")
    await run_to_settled(supervisor, client, task_id)
    narrowed = correction_document(client, task_id, image="crucible-worker:fake-prohibited-path")
    narrowed["scope"] = {**narrowed["scope"], "allowed_paths": ["src/ledger/**"]}
    assert client.post(f"/v1/tasks/{task_id}/corrections", json=narrowed).status_code == 200
    await run_to_settled(supervisor, client, task_id)

    widened = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    widened["scope"] = {**widened["scope"], "allowed_paths": ["src/ledger/**", "tests/ledger/**"]}
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=widened)
    assert r.status_code == 422
    assert any(e["path"] == "scope.allowed_paths" for e in r.json()["errors"])


# ----- 6: the gate compares the name the contract asked for --------------------


async def test_an_uploaded_artifact_carries_the_name_the_contract_asked_for(
    client: TestClient, supervisor: Supervisor
) -> None:
    """The contract names `report/run-evidence.md`; the bytes land at a content digest.
    The evidence the supervisor derives carries the name, or no upload could ever
    satisfy `run_evidence_present`.

    An upload is supplementary evidence on a head still being evaluated: 09 gives
    `pre_pr_gates_failed` no edge back to `gates_passed`, so a head that already failed
    is Foundry's to correct, not to top up."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert await run_to_settled(supervisor, client, task_id) == "publishing"
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]

    r = client.post(
        f"/v1/attempts/{attempt_id}/artifacts",
        params={"type": "run_evidence", "filename": "report/run-evidence.md"},
        content=b"# Seen working\n\nThe duplicate import returned 409.\n",
        headers={"Content-Type": "text/markdown"},
    )
    assert r.status_code == 201, r.text
    artifact = r.json()
    assert artifact["filename"] == "report/run-evidence.md"
    assert artifact["filename"] not in artifact["id"]

    await supervisor.tick()
    evidence = client.get(f"/v1/attempts/{attempt_id}/evidence").json()["items"]
    row = next(e for e in evidence if e["artifact_id"] == artifact["id"])
    assert row["payload"]["path"] == "report/run-evidence.md"
    assert row["payload"]["stored_at"].startswith("blobs/")
    assert row["payload"]["uploaded_by"] == "orchestrator-principal"
    # The gate still reads `pass`, now against two rows naming the same contract path.
    assert gates(client, attempt_id)[GateName.RUN_EVIDENCE_PRESENT] == "pass"


async def test_the_collectors_own_run_evidence_keeps_its_name(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    items = client.get(f"/v1/attempts/{attempt_id}/artifacts").json()["items"]
    names = {a["type"]: a["filename"] for a in items}
    assert names["run_evidence"] == "report/run-evidence.md"
    assert names["completion_claim"] == "report/completion-claim.json"
    assert gates(client, attempt_id)[GateName.RUN_EVIDENCE_PRESENT] == "pass"
