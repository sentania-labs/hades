"""Hades #402: collected self-reviews pass the real supervisor gates and publish.

The store is in memory; only the collector, publisher container and GitHub are fakes.
No review or acceptance API call releases either the first or the correcting attempt.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution.fake import default_report
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.cancel_task import cancel_task
from crucible.application.evidence import claim_facts, record_collection_evidence
from crucible.application.review import latest_work_attempt, request_review
from crucible.contracts.api import CancelRequest, ReviewRequest
from crucible.contracts.completion_claim import complete_claim, parse_claim
from crucible.domain.entities import AcceptanceResult
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.gates import PRE_PR_GATES, GateResult
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.ports.execution import (
    BranchBundle,
    CollectedArtifact,
    CollectedOutputs,
    CommitPolicyCheck,
    LaunchSpec,
    VerificationRun,
    WorkspaceState,
)
from crucible.ports.github import CommentRecord, InstallationToken, PullRequestRef
from tests.fixtures import FakeClock
from tests.unit.test_issue_360_ready_for_merge_correction import (
    NEW_HEAD,
    NOW,
    PR_NUMBER,
    TASK_ID,
    _attach,
    _correction,
    _principal,
    _ready_for_merge,
    _Store,
)
from tests.unit.test_issue_379_merge_during_publish import (
    _open,
    _Publisher,
    _PublishGitHub,
    _supervisor,
    _task,
)
from tests.unit.test_report_check import checker


class _Acceptances:
    def __init__(self) -> None:
        self.rows: list[AcceptanceResult] = []

    def supersede_for_task(self, task_id: str, now: datetime) -> None:
        for row in self.rows:
            if row.task_id == task_id:
                row.superseded_at = now

    def add(self, result: AcceptanceResult) -> None:
        self.rows.append(result)

    def list_for_task(self, task_id: str) -> list[AcceptanceResult]:
        return [row for row in self.rows if row.task_id == task_id]


class _ReviewReports:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def add(self, report: Any) -> None:
        self.rows.append(report)

    def list_for_task(self, task_id: str) -> list[Any]:
        return [r for r in self.rows if r.task_id == task_id]


class _SelfReviewStore(_Store):
    review_reports: _ReviewReports  # type: ignore[assignment]

    def __init__(self) -> None:
        self.__dict__.update(_ready_for_merge().__dict__)
        self.acceptance = _Acceptances()
        self.review_reports = _ReviewReports()


class _GitHub(_PublishGitHub):
    def find_pull_request(
        self, token: InstallationToken, *, repository: str, head_branch: str
    ) -> PullRequestRef | None:
        return None

    def post_issue_comment(
        self, token: InstallationToken, *, repository: str, number: int, body: str
    ) -> CommentRecord:
        self.posted.append(body)
        comment = CommentRecord(
            github_id="request-402",
            login=self.authenticated_login(token),
            body=body,
            created_at=NOW,
            updated_at=NOW,
            kind="issue_comment",
        )
        self.comments.append(comment)
        return comment


def _collected(
    tmp_path: Path,
    *,
    correction: bool = False,
    missing_review: bool = False,
    failed_check: bool = False,
    advisory_failed: bool = False,
) -> tuple[_SelfReviewStore, Any, _GitHub, _Publisher]:
    store = _SelfReviewStore()
    clock = FakeClock(NOW)
    github = _GitHub()
    publisher = _Publisher()
    supervisor = _supervisor(store, clock, tmp_path, github, publisher)
    task = _task(store)
    if correction:
        _attach(store, _correction(), clock)
        supervisor._materialize_scheduled()
    else:
        store.pull_requests.rows.clear()
    # A correction may arrive while the first external review is still outstanding.
    store.review_cycles.rows.clear()
    store.events.rows.clear()
    store.wakes.rows.clear()
    github.lookups = [_open(NEW_HEAD)]
    work = latest_work_attempt(store.uow(), task)
    assert work is not None
    attempt, execution = work
    attempt.state = AttemptState.SUCCEEDED
    attempt.exit_code = 0
    attempt.exit_class = ExitClass.COMPLETED
    execution.state = ExecutionState.SUCCEEDED
    # Exercise all real gates, including a legacy policy that required an orchestrator
    # review and listed report_present as advisory (hades #498 made it always so).
    execution.policy_snapshot["gates"] = {
        "pre_pr": sorted(PRE_PR_GATES),
        "advisory": ["report_present", "scope_contained", "criteria_mapped"],
    }
    execution.policy_snapshot["internal_review"] = {"required": True, "executor": "orchestrator"}
    execution.policy_snapshot["external_review"].update(
        {"required_rounds": 1, "provider": "codex", "request_on_publish": True}
    )
    task.state = TaskState.REPORTED
    task.head_sha = NEW_HEAD
    stored = store.contracts.get(task.id, execution.contract_version)
    assert stored is not None
    contract = stored.document
    spec = LaunchSpec(
        attempt_id=attempt.id,
        task_id=task.id,
        external_id=task.external_id,
        role=execution.role.value,
        harness=execution.harness,
        model=execution.model,
        image=execution.image,
        timeout_seconds=60,
        contract=contract,
    )
    report = default_report(spec, NEW_HEAD)
    if missing_review:
        del report["self_review"]
    outputs = CollectedOutputs(
        report=report,
        report_raw=None,
        blocked_md=None,
        diff_paths=(
            ("infrastructure/outside-the-contract.txt",)
            if advisory_failed
            else ("src/ledger/change.py",)
        ),
        diff_text="+return 409\n",
        bundle=BranchBundle(
            head_sha=NEW_HEAD,
            base_ref="main",
            work_branch="crucible/EX-0001",
            commits=1,
            verified=True,
            sha256="f" * 64,
            commit_paths=("src/ledger/change.py",),
            commit_messages=("Return 409",),
            commit_policy=CommitPolicyCheck(),
        ),
        artifacts=(
            CollectedArtifact(
                name="report/run-evidence.md",
                type="run_evidence",
                content=b"Observed 409",
                content_type="text/markdown",
            ),
        ),
        verifications=tuple(
            VerificationRun(
                id=v["id"],
                command=v["command"],
                expect_exit=0,
                exit_code=1 if failed_check and v["id"] == "V1" else 0,
                log_tail="verified",
            )
            for v in contract["required_verification"]
            if v.get("kind", "command") == "command"
        ),
        workspace_state=WorkspaceState(),
    )
    completed = complete_claim(report, claim_facts(task, outputs))
    claim, errors = parse_claim(
        completed.document, criteria=[c["id"] for c in contract["acceptance_criteria"]]
    )
    record_collection_evidence(
        store.uow(),
        clock,
        DiskArtifactStore(tmp_path / "collected"),
        attempt=attempt,
        task=task,
        outputs=outputs,
        claim=report,
        claim_parsed_ok=claim is not None,
        parse_errors=errors,
        completed=completed,
    )
    return store, supervisor, github, publisher


@pytest.mark.parametrize("correction", [False, True])
def test_self_review_publishes_through_supervisor_without_orchestrator_acceptance(
    tmp_path: Path,
    correction: bool,
) -> None:
    store, supervisor, github, publisher = _collected(tmp_path, correction=correction)
    supervisor._evaluate_pending_gates()
    assert _task(store).state is TaskState.PUBLISHING
    assert len(store.acceptance.rows) == 1
    assert store.acceptance.rows[0].head_sha == NEW_HEAD
    assert store.wakes.rows == []
    assert asyncio.run(supervisor.delivery.publish()) == 1
    assert _task(store).state is TaskState.AWAITING_EXTERNAL_REVIEW
    assert publisher.pushes == [NEW_HEAD]
    assert github.posted == ["@codex review"]
    pr = store.pull_requests.get_for_task(TASK_ID)
    assert pr is not None
    assert len(store.wakes.rows) == 1
    assert store.wakes.rows[0].reason == "published"
    assert store.wakes.rows[0].payload["summary"] == f"published, PR #{pr.number}"
    assert EventKind.TASK_AWAITING_INTERNAL_REVIEW.value not in store.events.kinds()
    assert EventKind.TASK_AWAITING_ACCEPTANCE.value not in store.events.kinds()
    event = store.events.latest_for_task_kind(TASK_ID, EventKind.ACCEPTANCE_RECORDED.value)
    assert event is not None and event.principal == "crucible" and event.payload["automatic"]
    supervisor._evaluate_pending_gates()
    assert asyncio.run(supervisor.delivery.publish()) == 0
    assert len(store.acceptance.rows) == len(store.wakes.rows) == 1


def test_missing_self_review_is_for_the_reviewer_and_names_section(tmp_path: Path) -> None:
    """hades #498: the report gate is advisory. A report without its self-review is
    listed for the reviewer, named by section, and the task waits for that review
    rather than failing its gates."""
    store, supervisor, _github, publisher = _collected(tmp_path, missing_review=True)
    supervisor._evaluate_pending_gates()
    assert _task(store).state is TaskState.AWAITING_INTERNAL_REVIEW
    outcome = next(r for r in store.gate_results.rows if r.gate == "report_present")
    assert outcome.result == GateResult.FAIL and not outcome.blocking
    assert "self_review" in outcome.detail
    assert store.acceptance.rows == []
    assert asyncio.run(supervisor.delivery.publish()) == 0
    assert publisher.pushes == []


def test_self_review_cannot_bypass_a_failed_blocking_gate(tmp_path: Path) -> None:
    store, supervisor, _github, publisher = _collected(tmp_path, failed_check=True)
    supervisor._evaluate_pending_gates()
    assert _task(store).state is TaskState.PRE_PR_GATES_FAILED
    assert store.acceptance.rows == []
    assert asyncio.run(supervisor.delivery.publish()) == 0
    assert publisher.pushes == []


def test_advisory_failure_requires_orchestrator_review_before_publication(
    tmp_path: Path,
) -> None:
    store, supervisor, github, publisher = _collected(tmp_path, advisory_failed=True)
    task = _task(store)
    supervisor._evaluate_pending_gates()
    assert task.state is TaskState.AWAITING_INTERNAL_REVIEW
    assert store.acceptance.rows == []
    assert asyncio.run(supervisor.delivery.publish()) == 0
    assert publisher.pushes == []

    request_review(
        store.uow(),
        FakeClock(NOW),
        principal=_principal(),
        task_id=TASK_ID,
        request=ReviewRequest(
            report={
                "schema_version": "1.0",
                "task_external_id": task.external_id,
                "reviewed_head_sha": task.head_sha,
                "reviewer": {"kind": "orchestrator", "principal": _principal().name},
                "summary": "The scope exception is approved.",
                "verdict": "approve",
                "findings": [],
            }
        ),
    )
    supervisor._evaluate_pending_gates()
    task = _task(store)
    assert task.state is TaskState.PUBLISHING
    assert len(store.acceptance.rows) == 1
    assert asyncio.run(supervisor.delivery.publish()) == 1
    task = _task(store)
    assert task.state is TaskState.AWAITING_EXTERNAL_REVIEW
    assert publisher.pushes == [NEW_HEAD]
    assert github.posted == ["@codex review"]


@pytest.mark.parametrize("state", [TaskState.ACCEPTED, TaskState.PUBLISHING])
def test_correction_from_accepted_or_publishing_stops_delivery_and_schedules_work(
    tmp_path: Path,
    state: TaskState,
) -> None:
    store = _SelfReviewStore()
    task = _task(store)
    task.state = state
    clock = FakeClock(NOW)
    github = _GitHub()
    publisher = _Publisher()
    supervisor = _supervisor(store, clock, tmp_path, github, publisher)

    _attach(store, _correction(), clock)

    assert task.state is TaskState.SCHEDULED
    assert asyncio.run(supervisor.delivery.publish()) == 0
    assert publisher.pushes == []


@pytest.mark.parametrize(
    "state",
    [
        TaskState.AWAITING_EXTERNAL_REVIEW,
        TaskState.AWAITING_CI_CERTIFICATION,
        TaskState.READY_FOR_MERGE,
    ],
)
def test_out_of_band_findings_do_not_gate_and_operator_can_attach_correction(
    state: TaskState,
) -> None:
    store = _SelfReviewStore()
    task = _task(store)
    task.state = state
    request_review(
        store.uow(),
        FakeClock(NOW),
        principal=_principal(),
        task_id=TASK_ID,
        request=ReviewRequest(
            report={
                "schema_version": "1.0",
                "task_external_id": task.external_id,
                "reviewed_head_sha": task.head_sha,
                "reviewer": {"kind": "orchestrator", "principal": _principal().name},
                "summary": "Adversarial review found a missing case.",
                "verdict": "request_changes",
                "findings": [],
            }
        ),
    )
    assert task.state is state
    assert len(store.review_reports.rows) == 1
    event = store.events.latest_for_task_kind(TASK_ID, EventKind.REVIEW_REPORT_RECORDED.value)
    assert event is not None and event.payload["pull_request_id"]
    correction = _correction()
    correction["correction"]["reason"] = "internal_review"
    _attach(store, correction, FakeClock(NOW))
    assert task.state is TaskState.SCHEDULED
    assert store.pull_requests.get_for_task(TASK_ID).number == PR_NUMBER  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "review",
    [
        None,
        {},
        {
            "documentation": ["docs/spec/11-definition-of-done.md"],
            "acceptance_criteria": [],
            "omissions": [],
        },
        {
            "documentation": ["No documentation change needed."],
            "acceptance_criteria": [{"id": "AC1", "status": "met", "evidence": ""}],
            "omissions": [],
        },
    ],
)
def test_incomplete_self_review_is_rejected_by_schema_and_worker_checker(review: Any) -> None:
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "summary": "Work done",
        "self_review": review,
        "acceptance_mapping": [{"id": "AC1", "status": "met", "evidence": "test"}],
        "proposed_pull_request": {"title": "Publish self reviewed reports", "body": "Done"},
        "limitations": [],
        "risks": [],
        "blockers": [],
        "follow_ups": [],
    }
    claim, errors = parse_claim(report, criteria=["AC1"])
    assert claim is None and any(e["loc"][0] == "self_review" for e in errors)
    assert any("self_review" in problem for problem in checker.check(report, criteria=["AC1"]))


@pytest.mark.parametrize(
    "state",
    [
        TaskState.ACCEPTED,
        TaskState.PUBLISHING,
        TaskState.AWAITING_EXTERNAL_REVIEW,
        TaskState.AWAITING_CI_CERTIFICATION,
        TaskState.READY_FOR_MERGE,
    ],
)
def test_orchestrator_can_cancel_after_acceptance(state: TaskState) -> None:
    store = _SelfReviewStore()
    task = _task(store)
    task.state = state
    cancel_task(
        store.uow(),
        FakeClock(NOW),
        principal=_principal(),
        task_id=TASK_ID,
        request=CancelRequest(
            reason="operator request", verbatim="Cancel this task.", decided_by="operator"
        ),
    )
    assert task.state is TaskState.CANCELLED


def test_report_gate_cannot_be_omitted_by_policy(tmp_path: Path) -> None:
    """The report gate always runs, so its gaps always reach the reviewer (hades #498)."""
    store, supervisor, _github, _publisher = _collected(tmp_path, missing_review=True)
    work = latest_work_attempt(store.uow(), _task(store))
    assert work is not None
    work[1].policy_snapshot["gates"]["pre_pr"] = []
    supervisor._evaluate_pending_gates()
    assert _task(store).state is TaskState.AWAITING_INTERNAL_REVIEW
    outcome = next(r for r in store.gate_results.rows if r.gate == "report_present")
    assert outcome.result == GateResult.FAIL and not outcome.blocking
    assert store.acceptance.rows == []


def test_specs_and_worker_identity_use_the_same_self_review_instruction(tmp_path: Path) -> None:
    from tests.unit.test_identity_bundle import build  # noqa: PLC0415

    wording = (
        "The worker self-review is the internal review. The required `self_review` section "
        "names where documentation was updated (or why no update was needed), maps every "
        "acceptance criterion with evidence, and lists anything knowingly left out and why."
    )
    identity, _, _ = build(tmp_path)
    assert wording in identity
    root = Path(__file__).resolve().parents[2]
    for name in ("05-task-contract", "09-lifecycle", "11-definition-of-done", "14-persistence"):
        text = (root / "docs/spec" / f"{name}.md").read_text()
        assert wording in " ".join(text.split())


@pytest.mark.parametrize("state", [TaskState.REPORTED, TaskState.AWAITING_INTERNAL_REVIEW])
def test_pre_upgrade_parsed_report_cannot_skip_self_review(
    tmp_path: Path, state: TaskState
) -> None:
    store, supervisor, _github, _publisher = _collected(tmp_path)
    _task(store).state = state
    report = next(
        row for row in store.evidence.rows if row.payload.get("role") == "completion_claim"
    )
    assert report.payload["parsed_ok"]
    del report.payload["self_review_checked"]
    supervisor._evaluate_pending_gates()
    assert _task(store).state is TaskState.AWAITING_INTERNAL_REVIEW
    outcome = next(row for row in store.gate_results.rows if row.gate == "report_present")
    assert not outcome.blocking and "self_review" in outcome.detail
    assert store.acceptance.rows == []


@pytest.mark.parametrize("correction", [False, True])
def test_artifact_acceptance_wakes_principal_once_without_publisher(
    tmp_path: Path, correction: bool
) -> None:
    store, supervisor, _github, publisher = _collected(tmp_path, correction=correction)
    task = _task(store)
    stored = store.contracts.get(task.id, task.contract_version)
    assert stored is not None
    stored.document["deliverables"] = [{"kind": "artifacts"}]
    supervisor.delivery._publisher = None
    supervisor.delivery._github = None

    supervisor._evaluate_pending_gates()

    assert task.state is TaskState.ACCEPTED
    assert len(store.acceptance.rows) == 1
    assert len(store.wakes.rows) == 1
    wake = store.wakes.rows[0]
    assert wake.principal_id == task.principal_id
    assert wake.task_id == task.id
    assert wake.reason == "accepted"
    assert wake.payload["task"]["state"] == "accepted"
    assert wake.payload["summary"] == (
        "accepted, artifacts are ready; no branch or PR publication was requested."
    )
    work = latest_work_attempt(store.uow(), task)
    assert work is not None
    assert wake.payload["attempt_id"] == work[0].id
    assert wake.payload["links"]["artifacts"] == f"/v1/attempts/{work[0].id}/artifacts"
    assert asyncio.run(supervisor.delivery.publish()) == 0
    supervisor._evaluate_pending_gates()
    assert len(store.acceptance.rows) == len(store.wakes.rows) == 1
    assert publisher.pushes == []
