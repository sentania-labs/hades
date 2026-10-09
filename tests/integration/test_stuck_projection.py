"""Stuck ownership survives a database round trip and a new certification."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.application.admin.stuck import task_stuck_reason
from crucible.application.decisions import open_escalation
from crucible.application.supervisor import Supervisor
from crucible.application.transitions import record_event
from crucible.domain.entities import CICertification, CIDecision, PullRequest, PullRequestState
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.domain.stuck_reasons import Owner
from tests.fixtures import FakeClock
from tests.integration.conftest import submit_and_start

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("reason", "needs_me"),
    [
        ("decision", 1),
        ("design", 1),
        ("ambiguous_contract", 0),
        ("missing_capability", 0),
        (None, 0),
    ],
)
def test_persisted_escalation_routes_the_board(
    client: TestClient,
    clock: FakeClock,
    supervisor: Supervisor,
    tokens: dict[str, str],
    reason: str | None,
    needs_me: int,
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=False)
    assert supervisor._lease_step()
    with supervisor._fenced() as uow:
        task = uow.tasks.get(task_id)
        assert task is not None
        task.state = TaskState.BLOCKED
        uow.tasks.save(task)
        open_escalation(
            uow, clock, task=task, attempt_id=None, question="Which layout?", reason=reason
        )
        uow.commit()
    response = client.get("/v1/board", headers={"Authorization": f"Bearer {tokens['operator']}"})
    assert response.status_code == 200
    board = response.json()
    assert board["needs_me"] == needs_me
    [card] = next(lane for lane in board["lanes"] if lane["key"] == "stuck")["cards"]
    assert card["stuck"]["owner"] == ("you" if needs_me else "foundry")
    assert ("answer" in [action["key"] for action in card["actions"]]) == bool(needs_me)


def test_ci_batch_projection_matches_detail_across_certifications(
    client: TestClient,
    clock: FakeClock,
    supervisor: Supervisor,
    tokens: dict[str, str],
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=False)
    assert supervisor._lease_step()
    pr_id = new_id()
    with supervisor._fenced() as uow:
        task = uow.tasks.get(task_id)
        assert task is not None
        task.state = TaskState.CI_CERTIFICATION_FAILED
        uow.tasks.save(task)
        uow.pull_requests.add(
            PullRequest(
                id=pr_id,
                task_id=task_id,
                repository_id=task.repository_id,
                number=607,
                url="https://github.com/example/repo/pull/607",
                base_ref="main",
                work_branch="crucible/test-stuck",
                state=PullRequestState.OPEN,
                head_sha="a" * 40,
                opened_at=clock.now(),
            )
        )
        uow.commit()
    for index, owner in enumerate((Owner.WORKER, Owner.FOUNDRY)):
        with supervisor._fenced() as uow:
            task = uow.tasks.get(task_id)
            assert task is not None
            certification = uow.ci_certifications.put(
                CICertification(
                    id=new_id(),
                    pull_request_id=pr_id,
                    task_id=task_id,
                    head_sha=str(index) * 40,
                    state="failed",
                    required_checks=[],
                    check_runs=[],
                    failure={"check": "unit"},
                    detail="unit failed",
                    evaluated_at=clock.now(),
                )
            )
            record_event(
                uow,
                clock,
                EventKind.CI_CERTIFICATION_RECORDED,
                principal="crucible",
                task_id=task_id,
                payload={"certification_id": certification.id, "failure": {"check": "unit"}},
            )
            if index == 0:
                uow.ci_decisions.add(
                    CIDecision(
                        id=new_id(),
                        task_id=task_id,
                        ci_certification_id=certification.id,
                        principal_id=task.principal_id,
                        cause="implementation_defect",
                        action="correct",
                        reasoning="Fix the unit failure.",
                        created_at=clock.now(),
                    )
                )
            assert uow.ci_decisions.list_for_certifications([]) == []
            matching = uow.ci_decisions.list_for_certifications([certification.id])
            assert len(matching) == (1 if index == 0 else 0)
            uow.commit()
        with supervisor._fenced() as uow:
            task = uow.tasks.get(task_id)
            assert task is not None
            detail = task_stuck_reason(uow, task, None)
            assert detail is not None and detail.owner is owner
        board = client.get(
            "/v1/board", headers={"Authorization": f"Bearer {tokens['operator']}"}
        ).json()
        [card] = next(lane for lane in board["lanes"] if lane["key"] == "stuck")["cards"]
        assert card["stuck"] == detail.as_dict()
