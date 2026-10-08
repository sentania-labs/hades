"""Branch ownership enforcement: integration tests for submit.

hades #564: verify that submit_task refuses a duplicate work_branch
and derives crucible/<external_id> when omitted, through the API.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fixtures import contract_document

pytestmark = pytest.mark.integration


def test_submit_refuses_duplicate_work_branch(client: TestClient) -> None:
    """AC1, AC4: submit returns 422 naming the owning task when the branch is already
    owned by another task on the same repository."""
    # First task submits successfully with an explicit branch
    r1 = client.post(
        "/v1/tasks",
        json=contract_document(external_id="EX-OLD", work_branch="crucible/dup-branch"),
    )
    assert r1.status_code == 201

    # Second task tries the same branch - should be refused
    r2 = client.post(
        "/v1/tasks",
        json=contract_document(external_id="EX-NEW", work_branch="crucible/dup-branch"),
    )
    assert r2.status_code == 422
    body = r2.json()
    assert body["type"] == "urn:crucible:problem:contract-invalid"
    assert any("EX-OLD" in e.get("message", "") for e in body["errors"])


def test_submit_accepts_omitted_work_branch(client: TestClient) -> None:
    """AC2: a contract with no work_branch is accepted and derives crucible/<external_id>.

    The derived branch must not conflict with the first task's branch,
    confirming the derivation happened at submit time.
    """
    base = contract_document(external_id="EX-NOBRANCH")
    del base["repository"]["work_branch"]

    r = client.post("/v1/tasks", json=base)
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    # Verify the stored contract has the derived branch
    r2 = client.get(f"/v1/tasks/{task_id}")
    assert r2.status_code == 200
    body = r2.json()
    repo_section = body.get("repository", {})
    assert repo_section.get("work_branch") == "crucible/EX-NOBRANCH"

    # A third task with an explicit different branch should still work
    r3 = client.post(
        "/v1/tasks",
        json=contract_document(external_id="EX-OTHER", work_branch="crucible/other-branch"),
    )
    assert r3.status_code == 201
