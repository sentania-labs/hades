"""hades #564: a work branch belongs to one task, through the API and the database."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, text

from crucible.adapters.api.deps import AppContext
from crucible.application.submit_task import submit_task
from crucible.domain.lifecycle import TaskState
from tests.fixtures import contract_document

pytestmark = pytest.mark.integration

BRANCH = "crucible/FDY-0524"


def _document(external_id: str, branch: str | None = BRANCH) -> dict[str, object]:
    document = contract_document(external_id=external_id)
    if branch is None:
        del document["repository"]["work_branch"]
    else:
        document["repository"]["work_branch"] = branch
    return document


def _refused_naming(response: Response, owner: str) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["type"] == "urn:crucible:problem:contract-invalid"
    (problem,) = [e for e in body["errors"] if e["path"] == "repository.work_branch"]
    assert owner in problem["message"] and BRANCH in problem["message"]


def test_submit_refuses_another_tasks_branch_in_every_state(
    client: TestClient, ctx: AppContext
) -> None:
    """AC1: the copied-contract incident of 2026-10-08; submitted, cancelled and merged
    owners all keep their branch, and the 422 names the owner."""
    owner = client.post("/v1/tasks", json=_document("FDY-0524"))
    assert owner.status_code == 201, owner.text
    owner_id = owner.json()["id"]

    _refused_naming(client.post("/v1/tasks", json=_document("FDY-0525")), "FDY-0524")

    cancelled = client.post(
        f"/v1/tasks/{owner_id}/cancel",
        json={"reason": "test", "verbatim": "cancel", "decided_by": "test"},
    )
    assert cancelled.status_code == 200, cancelled.text
    _refused_naming(client.post("/v1/tasks", json=_document("FDY-0526")), "FDY-0524")

    with ctx.uow_factory() as uow:
        task = uow.tasks.get(owner_id)
        assert task is not None
        uow.tasks.save(replace(task, state=TaskState.MERGED))
        uow.commit()
    _refused_naming(client.post("/v1/tasks", json=_document("FDY-0527")), "FDY-0524")

    # Nothing was stored for the refused submissions.
    listed = client.get("/v1/tasks", params={"repository": "example-service"}).json()
    assert [t["external_id"] for t in listed["items"]] == ["FDY-0524"]


def test_an_omitted_branch_is_derived_and_owned(client: TestClient) -> None:
    """AC2: no repository.work_branch is accepted and stored as crucible/<external_id>;
    the derived branch is then the submitter's like any other."""
    response = client.post("/v1/tasks", json=_document("FDY-0524", branch=None))
    assert response.status_code == 201, response.text
    view = client.get(f"/v1/tasks/{response.json()['id']}").json()
    assert view["contract"]["repository"]["work_branch"] == BRANCH
    _refused_naming(client.post("/v1/tasks", json=_document("FDY-0600")), "FDY-0524")


def test_overlapping_submissions_serialize_branch_ownership(
    client: TestClient, ctx: AppContext, engine: Engine
) -> None:
    """The second transaction waits, then sees the first transaction's committed owner."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        with ctx.uow_factory() as first:
            principal = first.principals.get_by_name("orchestrator-principal")
            assert principal is not None
            submit_task(first, ctx.clock, principal=principal, body=_document("FDY-0524"))
            pending = executor.submit(client.post, "/v1/tasks", json=_document("FDY-0601"))
            try:
                deadline = monotonic() + 10
                while monotonic() < deadline:
                    with engine.connect() as connection:
                        waiting = connection.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks "
                                "WHERE locktype = 'advisory' AND NOT granted "
                                "AND database = (SELECT oid FROM pg_database "
                                "WHERE datname = current_database())"
                            )
                        )
                    if waiting:
                        break
                    assert not pending.done(), "second submission did not wait for the owner"
                    sleep(0.01)
                else:
                    pytest.fail("second submission never waited for the ownership lock")
            finally:
                first.commit()
        _refused_naming(pending.result(timeout=10), "FDY-0524")
    listed = client.get("/v1/tasks", params={"repository": "example-service"}).json()
    assert [task["external_id"] for task in listed["items"]] == ["FDY-0524"]
