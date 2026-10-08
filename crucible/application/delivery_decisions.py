"""Foundry's decisions on the delivery half (04, 09, 23).

`ci-decision` and `head-decision` are the two places where Crucible has recorded facts,
stopped, and needs judgment. Crucible records the judgment and performs its mechanical
consequence. When the App holds Actions write it re-runs failed jobs through the
GitHub API (issue 435); otherwise it records the intent and raises a wake for the
operator. Hades merges a certified head when policy enables it.
"""

from __future__ import annotations

from typing import Any

from crucible.application.errors import (
    ForbiddenError,
    NotFoundError,
    TransitionNotAllowedError,
)
from crucible.application.observation import supersede_for_head
from crucible.application.task_access import require_task_principal
from crucible.application.transitions import move_task, record_event
from crucible.application.wakes import create_wake
from crucible.contracts.api import CIDecisionRequest, HeadDecisionRequest
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    CIAction,
    CICertification,
    CIDecision,
    HeadAction,
    Principal,
    Role,
    Task,
)
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.github import GitHubClient, GitHubError, InstallationToken
from crucible.ports.repository import UnitOfWork


def _orchestrator(principal: Principal, what: str) -> None:
    if principal.role not in (Role.ORCHESTRATOR, Role.OPERATOR):
        raise ForbiddenError(f"only an orchestrator or operator principal records {what}")


def record_ci_decision(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    request: CIDecisionRequest,
    github_client: GitHubClient | None = None,
) -> Task:
    """23: the cause from the fixed enum and the action.

    When the action is ``rerun`` and the installation grants Actions write,
    Hades re-runs the failed jobs through the App API and records the new
    attempt (issue 435).  When the installation lacks that permission the
    intent is recorded and a wake is raised for the operator (the legacy
    hand-off path)."""
    _orchestrator(principal, "a CI decision")
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    if task.state is not TaskState.CI_CERTIFICATION_FAILED:
        raise TransitionNotAllowedError(
            f"a CI decision is recorded in ci_certification_failed; task is {task.state.value}"
        )
    certifications = [
        c for c in uow.ci_certifications.list_for_task(task.id) if c.state == "failed"
    ]
    certification = certifications[-1] if certifications else None
    decision = CIDecision(
        id=new_id(),
        task_id=task.id,
        ci_certification_id=certification.id if certification else None,
        principal_id=principal.id,
        cause=request.cause.value,
        action=request.action.value,
        reasoning=request.reasoning,
        created_at=clock.now(),
    )
    uow.ci_decisions.add(decision)
    record_event(
        uow,
        clock,
        EventKind.CI_DECISION_RECORDED,
        principal=principal.name,
        task_id=task.id,
        payload={
            "ci_decision_id": decision.id,
            "certification_id": decision.ci_certification_id,
            "cause": decision.cause,
            "action": decision.action,
            "reasoning": decision.reasoning,
            "head_sha": task.head_sha,
            # hades FDY-0139: the failed runs this decision is about. After a rerun they
            # are not counted again; any other failure is a new one.
            "stale_failures": [
                {"run_id": row.get("run_id"), "completed_at": row.get("completed_at")}
                for row in (certification.failure.get("all") or [] if certification else [])
                # A row recorded before FDY-0139 carries no run id; listing it would match
                # nothing, so it is left out and the decision falls back to its time.
                if isinstance(row, dict) and row.get("run_id")
            ],
        },
    )
    if request.action is CIAction.RERUN:
        # Issue 435: when the installation grants Actions write, Hades re-runs the
        # failed jobs itself and records the attempt it started; otherwise the wake is
        # the hand-off to the operator, as before (23, 22).
        attempt = _rerun_through_app(uow, task, certification, github_client)
        if attempt is not None:
            assert certification is not None
            certification.failure["rerun_attempt"] = attempt
            certification.failure["rerun_decision"] = decision.id
            uow.ci_certifications.put(certification)
            note = (
                f"re-run requested, attempt {attempt} running; Hades re-ran the failed "
                "jobs through the App (Actions write)"
            )
        else:
            note = (
                f"waiting on a re-run: a CI re-run was decided for {task.head_sha}; "
                "re-run it on GitHub, Hades cannot (no Actions write)"
            )
        move_task(
            uow,
            clock,
            task,
            TaskState.AWAITING_CI_CERTIFICATION,
            EventKind.TASK_AWAITING_CI_CERTIFICATION,
            principal=principal.name,
            payload={
                "ci_decision_id": decision.id,
                "head_sha": task.head_sha,
                "note": note,
                "rerun_attempt": attempt,
            },
        )
        # The wake carries the line the Board and the task page show while the task
        # waits: the hand-off itself without Actions write, the running attempt with it.
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.CI_RERUN_NEEDED,
            summary=note,
            task=task,
            raised_by=principal.name,
        )
        return task
    if request.action is CIAction.REJECT:
        move_task(
            uow,
            clock,
            task,
            TaskState.REJECTED,
            EventKind.TASK_REJECTED,
            principal=principal.name,
            payload={"ci_decision_id": decision.id, "cause": decision.cause},
        )
        return task
    if request.action is CIAction.CANCEL:
        move_task(
            uow,
            clock,
            task,
            TaskState.CANCELLED,
            EventKind.TASK_CANCELLED,
            principal=principal.name,
            payload={"ci_decision_id": decision.id, "cause": decision.cause},
        )
        return task
    # `correct`: the task waits here until a correction contract is attached (09).
    return task


def record_head_decision(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    request: HeadDecisionRequest,
) -> Task:
    """09: adopt/recollect, reject, or cancel a head Crucible did not push."""
    _orchestrator(principal, "a head decision")
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    if task.state is not TaskState.HEAD_DIVERGED:
        raise TransitionNotAllowedError(
            f"a head decision is recorded in head_diverged; task is {task.state.value}"
        )
    pull_request = uow.pull_requests.get_for_task(task.id)
    payload: dict[str, Any] = {
        "action": request.action.value,
        "reasoning": request.reasoning,
        "accepted_head": task.head_sha,
        "observed_head": pull_request.head_sha if pull_request else None,
        "pull_request": pull_request.number if pull_request else None,
    }
    record_event(
        uow,
        clock,
        EventKind.HEAD_DECISION_RECORDED,
        principal=principal.name,
        task_id=task.id,
        payload=payload,
    )
    if request.action is HeadAction.REJECT:
        move_task(
            uow,
            clock,
            task,
            TaskState.REJECTED,
            EventKind.TASK_REJECTED,
            principal=principal.name,
            payload=payload,
        )
        return task
    if request.action is HeadAction.CANCEL:
        move_task(
            uow,
            clock,
            task,
            TaskState.CANCELLED,
            EventKind.TASK_CANCELLED,
            principal=principal.name,
            payload=payload,
        )
        return task
    # `adopt` (formerly named `recollect`): everything about the old head is superseded
    # and the task re-enters
    # supervision against the remote work branch, which is where the new head is. The
    # new head then goes through the whole pre-PR path, internal review as the policy
    # and Foundry decide, acceptance, and `publishing`, which finds the remote already
    # matching. See docs/implementation-notes/c4.md for why this re-enters at
    # `scheduled` rather than at `reported`.
    supersede_for_head(
        uow,
        clock,
        task=task,
        reason=request.action.value,
        new_head=pull_request.head_sha if pull_request else "",
        principal=principal.name,
    )
    stored = uow.contracts.get(task.id, task.contract_version)
    assert stored is not None
    execution_request = stored.document["execution_request"]
    task.head_sha = None
    uow.tasks.save(task)
    move_task(
        uow,
        clock,
        task,
        TaskState.SCHEDULED,
        EventKind.TASK_SCHEDULED,
        principal=principal.name,
        payload={
            "role": "correct",
            "reason": request.action.value,
            "contract_version": task.contract_version,
            "tier": execution_request["tier"],
            "provider": execution_request["provider"],
            "policy": {"name": task.policy_name, "version": task.policy_version},
            "resume_from_work_branch": True,
        },
    )
    return task


def _rerun_through_app(
    uow: UnitOfWork,
    task: Task,
    certification: CICertification | None,
    github_client: GitHubClient | None,
) -> int | None:
    """Re-run the failed jobs of the decided head's workflow runs, once, when the
    installation grants Actions write (issue 435). Returns the attempt now running, or
    None when Hades did not re-run anything and the operator hand-off stands.

    The grant is read from GitHub on every decision, never assumed. A refusal from
    GitHub (a run too old to re-run, one already running) is not an error for the
    decision: it falls back to the hand-off rather than losing the recorded decision."""
    if github_client is None or certification is None:
        return None
    repo = uow.repositories.get(task.repository_id)
    if repo is None or repo.installation_id is None:
        return None
    try:
        granted = github_client.get_installation_permissions(installation_id=repo.installation_id)
    except GitHubError:
        return None
    if granted.get("actions") != "write":
        return None
    try:
        token = github_client.installation_token(
            installation_id=repo.installation_id,
            repository=repo.name,
            permissions={"actions": "write", "metadata": "read"},
        )
    except GitHubError:
        return None
    try:
        attempts: list[int] = []
        for run_id in _failed_workflow_runs(github_client, token, repo.name, certification):
            try:
                github_client.rerun_failed_jobs(token, repository=repo.name, run_id=run_id)
            except GitHubError:
                continue
            # The endpoint answers 201 with no body; the run says which attempt started.
            try:
                run = github_client.get_workflow_run(token, repository=repo.name, run_id=run_id)
            except GitHubError:
                run = {}
            number = run.get("run_attempt")
            if isinstance(number, int) and not isinstance(number, bool):
                attempts.append(number)
            else:
                attempts.append(_previous_attempt(certification) + 1)
        return max(attempts) if attempts else None
    finally:
        github_client.revoke_token(token)


def _previous_attempt(certification: CICertification) -> int:
    value = certification.failure.get("rerun_attempt")
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _failed_workflow_runs(
    github_client: GitHubClient,
    token: InstallationToken,
    repository: str,
    certification: CICertification,
) -> list[int]:
    """The distinct workflow run ids behind the certification's failures, in order.

    A failure observed as a workflow run carries the run id; one observed as a check run
    from Actions carries the job id, which is resolved to its run. A row recorded before
    issue 435 has no source of its own; only the first failure's source is known then."""
    failure = certification.failure
    rows = [row for row in failure.get("all") or [] if isinstance(row, dict)]
    if not rows:
        rows = [{"run_id": failure.get("run_id"), "source": failure.get("source")}]
    elif "source" not in rows[0]:
        rows[0] = {**rows[0], "source": failure.get("source")}
    runs: list[int] = []
    for row in rows:
        raw = row.get("run_id")
        if raw is None or not str(raw).isdigit():
            continue
        if row.get("source") == "workflow_run":
            run_id: int | None = int(str(raw))
        elif row.get("source") == "check_run":
            try:
                run_id = github_client.workflow_run_for_job(
                    token, repository=repository, job_id=int(str(raw))
                )
            except GitHubError:
                run_id = None
        else:
            run_id = None
        if run_id is not None and run_id not in runs:
            runs.append(run_id)
    return runs
