"""hades #564: a work branch belongs to one task.

Submission refuses a branch another task on the repository already has, in any state;
a contract without `repository.work_branch` gets `crucible/<external_id>`; and the
publisher refuses to move a branch whose tip another task pushed, naming the pull
request that branch heads.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crucible.application.errors import ContractValidationError
from crucible.application.publish import build_plan
from crucible.application.submit_task import parse_contract, submit_task
from crucible.contracts.task_contract import TaskContractV1, correction_narrows
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from tests.fixtures import FakeClock, contract_document
from tests.unit.test_commit_trailer import _git
from tests.unit.test_issue_360_ready_for_merge_correction import NOW, _correction_attempt
from tests.unit.test_issue_379_merge_during_publish import (
    _correcting,
    _publish,
    _supervisor,
    _task,
)
from tests.unit.test_issue_403_checkpoint_publish_lease import (
    BRANCH,
    _bundle,
    _commit,
    _RealPublisher,
    _run,
)
from tests.unit.test_issue_403_checkpoint_publish_lease import (
    _history as _checkpoint_history,
)
from tests.unit.test_issue_424_proposed_tasks import ORCHESTRATOR, _api, _store

OWNER = "FDY-0524"
COPIED = "crucible/FDY-0524"


def _document(external_id: str, branch: str | None = COPIED) -> dict[str, object]:
    document = contract_document(external_id=external_id)
    repository = document["repository"]
    if branch is None:
        del repository["work_branch"]
    else:
        repository["work_branch"] = branch
    return document


# ----- derivation (AC2) -----------------------------------------------------------------


def test_an_omitted_work_branch_is_crucible_external_id() -> None:
    contract = parse_contract(_document("FDY-0579", branch=None))
    assert contract.repository.work_branch == "crucible/FDY-0579"
    assert TaskContractV1.model_validate(_document("X", branch=None)).repository.work_branch is None


def test_an_explicit_work_branch_is_unchanged() -> None:
    assert parse_contract(_document("FDY-0579", "crucible/own")).repository.work_branch == (
        "crucible/own"
    )


def test_a_derived_branch_that_is_no_ref_is_refused() -> None:
    with pytest.raises(ContractValidationError) as exc:
        parse_contract(_document("bad id..", branch=None))
    (problem,) = exc.value.errors
    assert problem["path"] == "repository.work_branch"
    assert "crucible/bad id.." in problem["message"]


def test_a_derived_branch_meets_the_policy_pattern_and_protected_branches() -> None:
    store, clock = _store(), FakeClock(NOW)
    task, stored = submit_task(
        store.uow(), clock, principal=ORCHESTRATOR, body=_document("FDY-0579", branch=None)
    )
    assert stored.document["repository"]["work_branch"] == "crucible/FDY-0579"
    assert task.external_id == "FDY-0579"
    # Validation of an explicit branch is as it was.
    with _api(_store(), clock, ORCHESTRATOR) as client:
        refused = client.post("/v1/tasks", json=_document("FDY-0580", "main"))
    assert refused.status_code == 422
    assert "repository.work_branch" in refused.text


def test_a_correction_that_omits_the_derived_branch_keeps_the_identity() -> None:
    previous = parse_contract(_document("FDY-0579", branch=None))
    correction = parse_contract(_document("FDY-0579", branch=None))
    assert not correction_narrows(previous, correction)
    explicit = parse_contract(_document("FDY-0579", "crucible/FDY-0579"))
    assert not correction_narrows(previous, explicit)
    moved = parse_contract(_document("FDY-0579", "crucible/elsewhere"))
    assert any(p["path"] == "external_id" for p in correction_narrows(previous, moved))


# ----- submit refuses another task's branch (AC1) ------------------------------------


def _refusal(client: TestClient, external_id: str, branch: str | None = COPIED) -> str:
    response = client.post("/v1/tasks", json=_document(external_id, branch))
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["type"] == "urn:crucible:problem:contract-invalid"
    (problem,) = [e for e in body["errors"] if e["path"] == "repository.work_branch"]
    return str(problem["message"])


@pytest.mark.parametrize(
    "state", [TaskState.SUBMITTED, TaskState.RUNNING, TaskState.MERGED, TaskState.CANCELLED]
)
def test_submit_refuses_another_tasks_branch_in_any_state(state: TaskState) -> None:
    store, clock = _store(), FakeClock(NOW)
    owner, _ = submit_task(store.uow(), clock, principal=ORCHESTRATOR, body=_document(OWNER))
    owner.state = state
    with _api(store, clock, ORCHESTRATOR) as client:
        message = _refusal(client, "FDY-0579")
    assert OWNER in message and COPIED in message and state.value in message
    assert [t.external_id for t in store.tasks.rows.values()] == [OWNER]


def test_submit_refuses_a_derived_branch_another_task_named() -> None:
    store, clock = _store(), FakeClock(NOW)
    submit_task(
        store.uow(), clock, principal=ORCHESTRATOR, body=_document(OWNER, "crucible/FDY-0579")
    )
    with _api(store, clock, ORCHESTRATOR) as client:
        assert OWNER in _refusal(client, "FDY-0579", branch=None)


def test_distinct_branches_and_the_same_external_id_are_not_ownership_problems() -> None:
    store, clock = _store(), FakeClock(NOW)
    submit_task(store.uow(), clock, principal=ORCHESTRATOR, body=_document(OWNER))
    with _api(store, clock, ORCHESTRATOR) as client:
        own = client.post("/v1/tasks", json=_document("FDY-0579", branch=None))
        again = client.post("/v1/tasks", json=_document(OWNER))
    assert own.status_code == 201, own.text
    # The owner's own external id is the duplicate answer (409), not a branch problem.
    assert again.status_code == 409, again.text


def test_the_same_branch_on_another_repository_is_not_owned() -> None:
    store, clock = _store(), FakeClock(NOW)
    owner, _ = submit_task(store.uow(), clock, principal=ORCHESTRATOR, body=_document(OWNER))
    owner.repository_id = "01OTHERREPOSITORY000000000"
    with _api(store, clock, ORCHESTRATOR) as client:
        response = client.post("/v1/tasks", json=_document("FDY-0579"))
    assert response.status_code == 201, response.text


# ----- the publisher refuses another task's branch (AC3) ------------------------------


def _put(repo: Path, origin: Path, sha: str, ref: str) -> None:
    _git(repo, "push", "-q", "-f", str(origin), f"{sha}:{ref}")


def _foreign_tip(tmp_path: Path, *, pull: int | None) -> tuple[Path, Path, str, str]:
    """A remote branch whose tip another task (FDY-0524) pushed, optionally the head of
    its pull request, and this task's (FDY-0579) bundle head for the same branch."""
    repo, origin, _, _ = _checkpoint_history(tmp_path, trailer=False)
    _git(repo, "reset", "-q", "--hard", "main")
    foreign = _commit(repo, "theirs.txt", "theirs\n", f"their work\n\nCrucible-Attempt: {OWNER}")
    _put(repo, origin, foreign, f"refs/heads/{BRANCH}")
    if pull is not None:
        _put(repo, origin, foreign, f"refs/pull/{pull}/head")
    _git(repo, "reset", "-q", "--hard", "main")
    head = _commit(repo, "ours.txt", "ours\n", "our work\n\nCrucible-Attempt: FDY-0579")
    return repo, origin, foreign, head


def test_the_publisher_refuses_the_head_of_another_tasks_pull_request(tmp_path: Path) -> None:
    repo, origin, foreign, head = _foreign_tip(tmp_path, pull=7)
    outcome = _run(tmp_path / "run", origin, _bundle(repo, tmp_path / "bundle"), head)
    assert not outcome.pushed
    assert outcome.step == "remote-ownership"
    assert "pull request #7" in outcome.detail and f"task {OWNER}" in outcome.detail
    assert _git(origin, "rev-parse", f"refs/heads/{BRANCH}").strip() == foreign


def test_another_tasks_trailer_is_no_proof_of_ownership(tmp_path: Path) -> None:
    """Before #564 any Crucible-Attempt trailer made the tip Hades's own; now it must name
    the task whose bundle is published."""
    repo, origin, foreign, head = _foreign_tip(tmp_path, pull=None)
    outcome = _run(tmp_path / "run", origin, _bundle(repo, tmp_path / "bundle"), head)
    assert not outcome.pushed and outcome.step == "remote-ownership"
    assert f"task {OWNER}" in outcome.detail and foreign in outcome.detail
    assert _git(origin, "rev-parse", f"refs/heads/{BRANCH}").strip() == foreign


def test_this_tasks_own_trailer_still_proves_ownership(tmp_path: Path) -> None:
    repo, origin, _, head = _foreign_tip(tmp_path, pull=7)
    _git(repo, "reset", "-q", "--hard", "main")
    checkpoint = _commit(repo, "cp.txt", "cp\n", "checkpoint\n\nCrucible-Attempt: FDY-0579")
    _put(repo, origin, checkpoint, f"refs/heads/{BRANCH}")
    _put(repo, origin, checkpoint, "refs/pull/7/head")
    _git(repo, "reset", "-q", "--hard", head)
    outcome = _run(tmp_path / "run", origin, _bundle(repo, tmp_path / "bundle"), head)
    assert outcome.pushed, outcome
    assert _git(origin, "rev-parse", f"refs/heads/{BRANCH}").strip() == head


def test_an_untrailed_foreign_tip_names_the_pull_request_it_heads(tmp_path: Path) -> None:
    repo, origin, _, head = _foreign_tip(tmp_path, pull=None)
    _git(repo, "reset", "-q", "--hard", "main")
    someone = _commit(repo, "x.txt", "x\n", "by hand")
    _put(repo, origin, someone, f"refs/heads/{BRANCH}")
    _put(repo, origin, someone, "refs/pull/9/head")
    _git(repo, "reset", "-q", "--hard", head)
    outcome = _run(tmp_path / "run", origin, _bundle(repo, tmp_path / "bundle"), head)
    assert not outcome.pushed and outcome.step == "remote-ownership"
    assert "foreign remote commit" in outcome.detail and "pull request #9" in outcome.detail


def test_the_refusal_leaves_the_task_publish_failed_with_the_reason(tmp_path: Path) -> None:
    repo, origin, foreign, head = _foreign_tip(tmp_path, pull=7)
    store, clock, _, github, _ = _correcting(tmp_path)
    task = _task(store)
    task.head_sha = head
    execution, attempt = _correction_attempt(store)
    attempt.workspace_path = str(tmp_path / "workspace")
    plan = build_plan(store.uow(), task, (attempt, execution))
    _git(repo, "branch", "-q", "-f", plan.work_branch, head)
    _put(repo, origin, foreign, f"refs/heads/{plan.work_branch}")
    _bundle(repo, Path(attempt.workspace_path) / "output", plan.work_branch)
    publisher = _RealPublisher(tmp_path / "publish", origin)
    supervisor = _supervisor(store, clock, tmp_path, github, publisher)
    _publish(supervisor)
    assert _task(store).state is TaskState.PUBLISH_FAILED
    failed = store.events.latest_for_task_kind(task.id, EventKind.TASK_PUBLISH_FAILED.value)
    assert failed is not None
    assert failed.payload["step"] == "remote-ownership"
    detail = str(failed.payload["detail"])
    assert "pull request #7" in detail and f"task {OWNER}" in detail
    assert _git(origin, "rev-parse", f"refs/heads/{plan.work_branch}").strip() == foreign
