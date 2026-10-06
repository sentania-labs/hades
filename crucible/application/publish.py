"""Publication: the accepted head becomes a pushed branch and a pull request (23, 09).

Runs only after the pre-PR gates passed for the collected head, the internal review is
recorded, and Foundry's AcceptanceResult for that head is `accepted`. The order is the
one 23 fixes, and each step is an event before and after:

1. `publish_started` with the head SHA and the bundle it will push;
2. mint a repository-scoped installation token;
3. run the publisher container, which verifies the bundle, asserts the head, and pushes
   with a lease on a fetched Hades-owned tip whose work the accepted head contains;
4. from Crucible, confirm the remote head (`branch_pushed_at_head`);
5. open the PR, or leave the existing one whose head just moved, with a body rendered
   from the contract and verified evidence only;
6. `publish_completed`, then `pr_exists_head_matches`, then the task moves on.

Anything that fails between 2 and 6 is `publish_failed` with the step and the response
class. The token is never in the record, the event, or the log; only its expiry is.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from crucible.application.decisions import open_escalation
from crucible.application.transitions import move_task, record_event
from crucible.application.wakes import create_wake
from crucible.contracts.evidence import EvidenceKind
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    Attempt,
    EscalationState,
    Execution,
    ExternalReviewCycle,
    PullRequest,
    PullRequestHead,
    PullRequestState,
    PushedBy,
    Repository,
    Task,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.external_review import CycleState, configured_components, required_rounds
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.domain.publication import (
    BodyInput,
    CorrectionEntry,
    CriterionMapping,
    TitleRefusedError,
    VerifiedCheck,
    body_sha256,
    publication_owned_heads,
    render_body,
    validate_title,
)
from crucible.ports.clock import Clock
from crucible.ports.github import CommentRecord, GitHubClient, InstallationToken, PullRequestRef
from crucible.ports.repository import UnitOfWork

log = logging.getLogger("crucible.publish")

DEFAULT_TITLE = "Crucible delivery"


def external_review_trigger(policy: dict[str, Any]) -> str | None:
    """The publication trigger, or None when publication must not request a review."""
    review = policy.get("external_review", {})
    if not isinstance(review, dict):
        return None
    provider = str(review.get("provider") or "")
    if not provider or int(review.get("required_rounds", 0)) <= 0:
        return None
    if not bool(review.get("request_on_publish", True)):
        return None
    configured = review.get("trigger_comment")
    if configured is not None:
        return str(configured) or None
    return {"codex": "@codex review"}.get(provider)


def external_review_request_exists(
    previous: Any | None,
    comments: tuple[CommentRecord, ...],
    trigger: str,
    pull_request_number: int,
    app_login: str,
) -> bool:
    """Whether this PR already carries the App-authored request recorded by Crucible."""
    if previous is None or int(previous.payload.get("pull_request") or 0) != pull_request_number:
        return any(comment.login == app_login and comment.body == trigger for comment in comments)
    payload = previous.payload
    comment_id = str(payload.get("comment_id") or "")
    comment_login = str(payload.get("comment_login") or "")
    return any(
        comment.github_id == comment_id
        and comment.login == comment_login
        and comment.body == trigger
        for comment in comments
    ) or bool(comment_id)


def request_external_review(
    github: GitHubClient,
    token: InstallationToken,
    *,
    plan: PublishPlan,
    pull_request_number: int,
    previous: Any | None,
) -> CommentRecord | None:
    """Post the publication trigger if this pull request has not already received it."""
    trigger = external_review_trigger(plan.policy)
    if trigger is None:
        return None
    comments = github.issue_comments(
        token, repository=plan.repository_name, number=pull_request_number
    )
    app_login = github.authenticated_login(token)
    recover_event = (
        previous is None or int(previous.payload.get("pull_request") or 0) != pull_request_number
    )
    if external_review_request_exists(previous, comments, trigger, pull_request_number, app_login):
        if recover_event:
            return next(
                comment
                for comment in comments
                if comment.login == app_login and comment.body == trigger
            )
        return None
    return github.post_issue_comment(
        token,
        repository=plan.repository_name,
        number=pull_request_number,
        body=trigger,
    )


def record_external_review_requested(
    uow: UnitOfWork,
    clock: Clock,
    *,
    plan: PublishPlan,
    pull_request_number: int,
    comment: CommentRecord,
) -> None:
    record_event(
        uow,
        clock,
        EventKind.EXTERNAL_REVIEW_REQUESTED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=plan.task_id,
        attempt_id=plan.attempt_id,
        payload={
            "pull_request": pull_request_number,
            "provider": plan.policy.get("external_review", {}).get("provider"),
            "comment_id": comment.github_id,
            "comment_login": comment.login,
        },
    )


@dataclass(frozen=True, slots=True)
class PublishPlan:
    """Everything one publication needs, read once inside a fenced transaction.

    Nothing here is a live ORM object: the publisher and the GitHub calls run outside a
    transaction, and a plan that held rows would hold a connection with them."""

    task_id: str
    external_id: str
    principal_id: str
    attempt_id: str
    head_sha: str
    repository_id: str
    repository_name: str
    # Where the publisher pushes. Derived from the registered repository's API name, not
    # from its clone url: `repositories.url` is where Crucible's own containers *fetch*
    # from, which in a developer arrangement is a local mirror of a repository they hold
    # no credential for (docs/implementation-notes/c4.md).
    push_url: str
    installation_id: int | None
    base_ref: str
    work_branch: str
    deliverable_kind: str
    draft: bool
    image: str
    bundle_path: str
    bundle_sha256: str
    policy: dict[str, Any] = field(default_factory=dict)
    title: str = DEFAULT_TITLE
    body: str = ""
    existing_pr_number: int | None = None
    timeout_seconds: int = 600
    problem: str = ""
    resume_step: str = ""
    retry_number: int = 0
    publish_retry_max: int = 3
    owned_remote_heads: tuple[str, ...] = ()


def repository_slug(repository: Repository) -> str:
    """`owner/name`, which is what every API path needs.

    Taken from the clone url when that is a GitHub url, and from the registered name
    otherwise, so a repository whose url is a local mirror still resolves to the
    repository the App is installed on."""
    url = repository.url.rstrip("/")
    if "github.com" in url:
        if url.endswith(".git"):
            url = url[: -len(".git")]
        parts = [p for p in url.replace(":", "/").split("/") if p]
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"
    return repository.name.strip("/")


def push_url_for(repository: Repository, *, host: str = "github.com") -> str:
    """The https remote the publisher pushes to."""
    url = repository.url.rstrip("/")
    if "github.com" in url:
        return url if url.endswith(".git") else f"{url}.git"
    return f"https://{host}/{repository_slug(repository)}.git"


def verified_checks(uow: UnitOfWork, attempt_id: str) -> tuple[VerifiedCheck, ...]:
    """Crucible's own verifier runs, never the worker's report of them (11, 23)."""
    out: list[VerifiedCheck] = []
    for row in uow.evidence.list_for_attempt(attempt_id):
        if row.kind != EvidenceKind.VERIFICATION_RUN.value or not row.verified:
            continue
        out.append(
            VerifiedCheck(
                id=str(row.payload.get("id", "")),
                command=str(row.payload.get("command", "")),
                exit_code=int(row.payload.get("exit_code", -1)),
                expect_exit=int(row.payload.get("expect_exit", 0)),
                # The verifier's own log, stored as an artifact. The worker's log is never
                # what the body cites (23).
                artifact_id=row.artifact_id,
                ran=bool(row.payload.get("ran", True)),
            )
        )
    return tuple(out)


def criteria_mappings(
    contract: dict[str, Any], claim: dict[str, Any] | None
) -> tuple[CriterionMapping, ...]:
    """Each acceptance criterion with the mapping the claim proposed for it.

    The mapping is labelled by its own status word and sits beside the verification
    table; the body never presents it as a verified fact of its own (23)."""
    proposed = {
        str(m.get("id")): m
        for m in (claim or {}).get("acceptance_mapping", [])
        if isinstance(m, dict)
    }
    out: list[CriterionMapping] = []
    for criterion in contract.get("acceptance_criteria", []):
        cid = str(criterion.get("id", ""))
        mapping = proposed.get(cid, {})
        out.append(
            CriterionMapping(
                id=cid,
                text=str(criterion.get("text", "")),
                status=str(mapping.get("status", "not_reported")),
                evidence=str(mapping.get("evidence", "")),
            )
        )
    return tuple(out)


def correction_history(uow: UnitOfWork, task: Task) -> tuple[CorrectionEntry, ...]:
    out: list[CorrectionEntry] = []
    for version in uow.contracts.list_for_task(task.id):
        correction = version.document.get("correction")
        if not isinstance(correction, dict):
            continue
        out.append(
            CorrectionEntry(
                version=version.version,
                reason=str(correction.get("reason", "")),
                addresses=tuple(
                    str(a.get("ref") or a.get("id") or a) for a in correction.get("addresses", [])
                ),
            )
        )
    return tuple(out)


def review_reference(uow: UnitOfWork, task: Task) -> dict[str, str] | None:
    reports = [
        r
        for r in uow.review_reports.list_for_task(task.id)
        if r.head_sha == (task.head_sha or "") and r.superseded_at is None
    ]
    if not reports:
        return None
    report = reports[-1]
    reference = {"reviewer_kind": report.reviewer_kind, "report_id": report.id}
    if report.artifact_id:
        reference["report_artifact"] = report.artifact_id
    return reference


def collected_bundle_sha256(uow: UnitOfWork, attempt: Attempt) -> str:
    """Return the recorded seal, or seal a retained pre-upgrade bundle in place.

    C7d added the digest to bundle evidence. Attempts collected before that deployment
    have verified bundle evidence but no digest, so publication derives the seal from
    Crucible's retained workspace before the first publisher receives it. The resulting
    `publish_started` event makes that derived seal durable for any manual retry.
    """
    bundle_evidence = next(
        (
            row
            for row in reversed(uow.evidence.list_for_attempt(attempt.id))
            if row.kind == EvidenceKind.BUNDLE_HEAD.value and row.verified
        ),
        None,
    )
    recorded = str((bundle_evidence.payload if bundle_evidence else {}).get("bundle_sha256") or "")
    if recorded:
        return recorded
    bundle_path = Path(f"{attempt.workspace_path}/output/work_branch.bundle")
    return hashlib.sha256(bundle_path.read_bytes()).hexdigest() if bundle_path.is_file() else ""


def build_plan(uow: UnitOfWork, task: Task, work: tuple[Attempt, Execution]) -> PublishPlan:
    """Read everything the publication needs and render the body. Pure of I/O beyond
    the database: the GitHub calls and the container come later."""
    attempt, execution = work
    stored = uow.contracts.get(task.id, execution.contract_version)
    assert stored is not None
    contract = stored.document
    repository = uow.repositories.get(task.repository_id)
    assert repository is not None
    policy = execution.policy_snapshot or {}
    claim_record = uow.claims.get(attempt.id)
    claim = claim_record.document if claim_record and claim_record.parsed_ok else None
    repo_section = contract.get("repository", {})
    base_ref = str(repo_section.get("base_ref") or repository.default_branch or "main")
    work_branch = str(repo_section.get("work_branch") or f"crucible/{task.external_id}")
    deliverables = [
        d for d in contract.get("deliverables", []) if d.get("kind") in ("pull_request", "branch")
    ]
    deliverable = deliverables[0] if deliverables else {"kind": "pull_request"}
    closes = tuple(str(c) for c in deliverable.get("closes", []))
    proposed = str((claim or {}).get("proposed_pull_request", {}).get("title") or "")
    problem = ""
    title = DEFAULT_TITLE if not proposed else ""
    if proposed:
        try:
            title = validate_title(proposed)
        except TitleRefusedError as exc:
            problem = str(exc)
            title = ""
    body = render_body(
        BodyInput(
            external_id=task.external_id,
            objective=str(contract.get("objective", "")),
            head_sha=task.head_sha or "",
            attempt_id=attempt.id,
            harness=execution.harness,
            harness_version=str(policy.get("harness_version", "")) or execution.model,
            image_digest=attempt.image_digest or execution.image,
            criteria=criteria_mappings(contract, claim),
            checks=verified_checks(uow, attempt.id),
            review_reference=review_reference(uow, task),
            corrections=correction_history(uow, task),
            closes=closes,
            limitations=tuple(str(x) for x in (claim or {}).get("limitations", [])),
            risks=tuple(str(x) for x in (claim or {}).get("risks", [])),
            artifact_verifications=tuple(
                str(v.get("path", ""))
                for v in contract.get("required_verification", [])
                if v.get("kind") == "artifact"
            ),
        )
    )
    existing = uow.pull_requests.get_for_task(task.id)
    git_policy = policy.get("git", {})
    publishing = uow.events.latest_for_task_kind(task.id, EventKind.TASK_PUBLISHING.value)
    publishing_payload = publishing.payload if publishing else {}
    bundle_sha256 = collected_bundle_sha256(uow, attempt)
    retry_limit = publishing_payload.get("publish_retry_max")
    if retry_limit is None:
        retry_limit = policy.get("limits", {}).get("publish_retry_max", 3)
    owned_heads: set[str] = set()
    after = 0
    while events := uow.events.list_for_task(task.id, after_seq=after, limit=1000):
        owned_heads.update(
            publication_owned_heads(
                events, work_branch=work_branch, repository=repository_slug(repository)
            )
        )
        last = events[-1].seq
        if last is None or last <= after:
            break
        after = last
    return PublishPlan(
        owned_remote_heads=tuple(sorted(owned_heads)),
        task_id=task.id,
        external_id=task.external_id,
        principal_id=task.principal_id,
        attempt_id=attempt.id,
        head_sha=task.head_sha or "",
        repository_id=repository.id,
        repository_name=repository_slug(repository),
        push_url=push_url_for(repository),
        installation_id=repository.installation_id,
        base_ref=base_ref,
        work_branch=work_branch,
        deliverable_kind=str(deliverable.get("kind", "pull_request")),
        draft=bool(deliverable.get("draft", False)),
        image=attempt.image_digest or execution.image,
        bundle_path=f"{attempt.workspace_path}/output/work_branch.bundle",
        bundle_sha256=bundle_sha256,
        policy=policy,
        title=title,
        body=body,
        existing_pr_number=existing.number if existing else None,
        timeout_seconds=int(git_policy.get("publish_timeout_seconds", 600)),
        problem=problem,
        resume_step=str(publishing_payload.get("resume_step") or ""),
        retry_number=int(publishing_payload.get("retry_number", 0)),
        publish_retry_max=int(retry_limit),
    )


def record_publish_started(uow: UnitOfWork, clock: Clock, plan: PublishPlan) -> None:
    record_event(
        uow,
        clock,
        EventKind.PUBLISH_STARTED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=plan.task_id,
        attempt_id=plan.attempt_id,
        payload={
            "head_sha": plan.head_sha,
            "bundle": plan.bundle_path,
            "bundle_sha256": plan.bundle_sha256,
            "repository": plan.repository_name,
            "work_branch": plan.work_branch,
            "base_ref": plan.base_ref,
            "deliverable": plan.deliverable_kind,
            "body_sha256": body_sha256(plan.body),
            "resume_step": plan.resume_step,
            "retry_number": plan.retry_number,
            "publish_retry_max": plan.publish_retry_max,
        },
    )


def record_token_minted(
    uow: UnitOfWork,
    clock: Clock,
    plan: PublishPlan,
    *,
    expires_at: datetime,
    permissions: dict[str, str],
) -> None:
    """The expiry and the job, never the value (12)."""
    record_event(
        uow,
        clock,
        EventKind.INSTALLATION_TOKEN_MINTED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=plan.task_id,
        attempt_id=plan.attempt_id,
        payload={
            "repository": plan.repository_name,
            "installation_id": plan.installation_id,
            "expires_at": expires_at.isoformat(),
            "permissions": sorted(permissions),
            "note": "the token value is in memory and the publisher's tmpfs only (12)",
        },
    )


def _publishing_entry(uow: UnitOfWork, task: Task) -> tuple[int, datetime]:
    """When the task last entered `publishing`: the event's sequence and time. A task
    with no such event (it cannot happen through the API) counts from its last update."""
    entered = uow.events.latest_for_task_kind(task.id, EventKind.TASK_PUBLISHING.value)
    if entered is None:
        return 0, task.updated_at
    return int(entered.seq or 0), entered.ts


def _current_hold(uow: UnitOfWork, task: Task, entered_seq: int) -> dict[str, Any] | None:
    """The `task_publish_pending` record of this entry into `publishing`, if the
    publication is still waiting: none has started since it was written."""
    pending = uow.events.latest_for_task_kind(task.id, EventKind.TASK_PUBLISH_PENDING.value)
    if pending is None or int(pending.seq or 0) <= entered_seq:
        return None
    started = uow.events.latest_for_task_kind(task.id, EventKind.PUBLISH_STARTED.value)
    if started is not None and int(started.seq or 0) > int(pending.seq or 0):
        return None
    return dict(pending.payload)


def hold_publishing(
    uow: UnitOfWork, clock: Clock, task: Task, *, reason: str, escalate_after_seconds: int
) -> bool:
    """A task in `publishing` whose publication cannot start, and why (hades FDY-0133).

    The reason is recorded once per entry into `publishing`, and again only when it
    changes, as a `task_publish_pending` event the task's events, `GET /v1/supervisor`
    and the admin UI's task list all read. Once the task has waited
    `escalate_after_seconds` (the publisher's own time limit: a push that could have
    finished in that time has not started) an escalation is opened on the existing path,
    once, with a wake. Returns True when a new reason was recorded, so the caller logs it
    once per task rather than once per tick."""
    entered_seq, entered_at = _publishing_entry(uow, task)
    hold = _current_hold(uow, task, entered_seq)
    escalation_id = str((hold or {}).get("escalation_id") or "")
    recorded = False
    payload: dict[str, Any] = {
        "reason": reason,
        "waiting_since": entered_at.isoformat(),
        "escalate_after_seconds": escalate_after_seconds,
    }
    if hold is None or hold.get("reason") != reason:
        if escalation_id:
            payload["escalation_id"] = escalation_id
        record_event(
            uow,
            clock,
            EventKind.TASK_PUBLISH_PENDING,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload=payload,
        )
        recorded = True
    if not escalation_id and clock.now() - entered_at >= timedelta(seconds=escalate_after_seconds):
        minutes = max(1, escalate_after_seconds // 60)
        escalation = open_escalation(
            uow,
            clock,
            task=task,
            attempt_id=None,
            question=(
                f"publication has not started after {minutes} minute(s) in publishing: {reason}"
            )[:2000],
            wake_reason=WakeReason.PUBLISH_FAILED,
            summary=f"publication has not started: {reason}"[:500],
        )
        record_event(
            uow,
            clock,
            EventKind.TASK_PUBLISH_PENDING,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={**payload, "escalation_id": escalation.id},
        )
    return recorded


def release_publishing_hold(uow: UnitOfWork, clock: Clock, task: Task) -> None:
    """Publication started: an escalation the wait opened is closed, since what it asked
    about is over. Called before `publish_started` is recorded."""
    entered_seq, _ = _publishing_entry(uow, task)
    hold = _current_hold(uow, task, entered_seq)
    escalation_id = str((hold or {}).get("escalation_id") or "")
    if not escalation_id:
        return
    escalation = uow.escalations.get(escalation_id, for_update=True)
    if escalation is None or escalation.state is not EscalationState.OPEN:
        return
    escalation.state = EscalationState.CLOSED
    escalation.closed_at = clock.now()
    uow.escalations.save(escalation)
    record_event(
        uow,
        clock,
        EventKind.ESCALATION_CLOSED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        payload={"escalation_id": escalation.id, "reason": "publication started"},
    )


def publishing_waits(uow: UnitOfWork) -> list[dict[str, Any]]:
    """Every task in `publishing` whose publication is waiting, with the recorded reason.
    What `GET /v1/supervisor` and the admin UI show; empty when nothing waits."""
    out: list[dict[str, Any]] = []
    for task in uow.tasks.list_by_state(TaskState.PUBLISHING):
        entered_seq, _ = _publishing_entry(uow, task)
        hold = _current_hold(uow, task, entered_seq)
        if hold is None:
            continue
        out.append(
            {
                "task_id": task.id,
                "external_id": task.external_id,
                "reason": str(hold.get("reason") or ""),
                "waiting_since": hold.get("waiting_since"),
                "escalation_id": hold.get("escalation_id"),
            }
        )
    return out


def _claim_gone(step: str, detail: str) -> bool:
    """Return True when the bundle is unrecoverable (no claim or no bundle).

    The Kubernetes publisher returns step="bundle-seal" with a detail
    mentioning the claim is gone or the bundle is missing; in that case
    there is nothing to retry.
    """
    if step != "bundle-seal":
        return False
    lower = detail.lower()
    return "gone" in lower or "missing" in lower or "no branch bundle" in lower


def reopen_or_cancel(number: int) -> str:
    """hades #379: what to do with a task whose pull request was closed unmerged while a
    correction was under way; the poll's close wake and the publication's own failure
    say the same."""
    return f"reopen pull request #{number} and then republish, or cancel the task"


def fail_publish(
    uow: UnitOfWork,
    clock: Clock,
    task: Task,
    *,
    step: str,
    detail: str,
    response_class: str = "",
    attempt_id: str | None = None,
    extra: dict[str, Any] | None = None,
    closed_pull_request: int | None = None,
    retryable: bool = True,
) -> None:
    """23 step 7: the step and the API response class, never the token.

    `closed_pull_request` names the task's pull request when the publication found it
    closed (hades #379): a republish meets the same closed pull request until someone
    reopens it, so the wake says to reopen it and then republish, or to cancel.
    `retryable` False when a republish can only fail the same way (the task's pull
    request is merged), so the wake offers none."""
    payload: dict[str, Any] = {
        "step": step,
        "detail": detail,
        "head_sha": task.head_sha,
        **(extra or {}),
    }
    if response_class:
        payload["response_class"] = response_class
    if task.state is TaskState.PUBLISHING:
        move_task(
            uow,
            clock,
            task,
            TaskState.PUBLISH_FAILED,
            EventKind.TASK_PUBLISH_FAILED,
            attempt_id=attempt_id,
            payload=payload,
        )
    else:
        record_event(
            uow,
            clock,
            EventKind.TASK_PUBLISH_FAILED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            attempt_id=attempt_id,
            payload=payload,
        )
    publishing = uow.events.latest_for_task_kind(task.id, EventKind.TASK_PUBLISHING.value)
    retry_number = int((publishing.payload if publishing else {}).get("retry_number", 0))
    stored_policy = uow.policies.get(task.policy_name, task.policy_version)
    retry_limit = (publishing.payload if publishing else {}).get("publish_retry_max")
    if retry_limit is None:
        retry_limit = (
            (stored_policy.document if stored_policy else {})
            .get("limits", {})
            .get("publish_retry_max", 3)
        )
    retry_max = int(retry_limit)
    retries_remaining = max(retry_max - retry_number, 0)
    links = {"events": f"/v1/tasks/{task.id}/events"}
    claim_lost = _claim_gone(step, detail)
    started = uow.events.latest_for_task_kind(task.id, EventKind.PUBLISH_STARTED.value)
    if retryable and not claim_lost and retries_remaining and started is not None:
        links["republish"] = f"/v1/tasks/{task.id}/republish"
    if not retryable:
        summary = (
            f"publication failed at {step} on {task.head_sha}: {detail}; "
            "a republish would fail the same way, so none is offered"
        )[:500]
    elif closed_pull_request is not None:
        summary = (
            f"publication failed at {step} on {task.head_sha}: {detail}; "
            f"{reopen_or_cancel(closed_pull_request)}; manual publication retries used "
            f"{retry_number} of {retry_max}, {retries_remaining} remaining"
        )[:500]
    elif claim_lost:
        summary = (
            f"publication failed at {step} on {task.head_sha}: {detail}; "
            f"no retry is possible (bundle seal is lost)"
        )[:500]
    else:
        summary = (
            f"publication failed at {step} on {task.head_sha}: {detail}; "
            f"manual publication retries used {retry_number} of {retry_max}, "
            f"{retries_remaining} remaining"
        )[:500]
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=WakeReason.PUBLISH_FAILED,
        summary=summary,
        task=task,
        attempt_id=attempt_id,
        extra_links=links,
    )


def upsert_pull_request(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    plan: PublishPlan,
    ref: PullRequestRef,
    body_hash: str,
) -> tuple[PullRequest, bool]:
    """Write or refresh the PR row and record the head as one Crucible pushed."""
    now = clock.now()
    existing = uow.pull_requests.get_for_task(task.id, for_update=True)
    opened = existing is None
    if existing is None:
        pull_request = PullRequest(
            id=new_id(),
            task_id=task.id,
            repository_id=plan.repository_id,
            number=ref.number,
            url=ref.url,
            base_ref=plan.base_ref,
            observed_base_ref=ref.base_ref or plan.base_ref,
            work_branch=plan.work_branch,
            state=PullRequestState.OPEN,
            head_sha=plan.head_sha,
            observed_head_sha=plan.head_sha,
            title=ref.title or plan.title,
            body_sha256=body_hash,
            opened_at=now,
        )
        uow.pull_requests.add(pull_request)
    else:
        pull_request = existing
        pull_request.number = ref.number
        pull_request.url = ref.url
        pull_request.head_sha = plan.head_sha
        pull_request.observed_head_sha = plan.head_sha
        pull_request.observed_base_ref = ref.base_ref or pull_request.base_ref
        pull_request.title = ref.title or plan.title
        pull_request.body_sha256 = body_hash
        pull_request.state = PullRequestState.OPEN
        uow.pull_requests.save(pull_request)
    uow.pull_request_heads.add(
        PullRequestHead(
            id=new_id(),
            pull_request_id=pull_request.id,
            sha=plan.head_sha,
            pushed_by=PushedBy.CRUCIBLE,
            observed_at=now,
        )
    )
    record_event(
        uow,
        clock,
        EventKind.PULL_REQUEST_OPENED if opened else EventKind.PULL_REQUEST_HEAD_UPDATED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        attempt_id=plan.attempt_id,
        payload={
            "number": pull_request.number,
            "url": pull_request.url,
            "head_sha": plan.head_sha,
            "base_ref": pull_request.base_ref,
            "body_sha256": body_hash,
        },
    )
    return pull_request, opened


def advance_after_publish(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    plan: PublishPlan,
    pull_request: PullRequest | None,
    completed_rounds: int,
) -> TaskState:
    """09: branch deliverables reach `accepted`; a PR goes to external review or straight
    to certification, depending on whether the required rounds are already satisfied."""
    attempt_id = plan.attempt_id
    if plan.deliverable_kind == "branch":
        move_task(
            uow,
            clock,
            task,
            TaskState.ACCEPTED,
            EventKind.TASK_ACCEPTED,
            attempt_id=attempt_id,
            payload={
                "head_sha": plan.head_sha,
                "deliverable": "branch",
                "note": "the branch is pushed and verified; nothing is accepted unpublished",
            },
        )
        return TaskState.ACCEPTED
    rounds = required_rounds(plan.policy)
    assert pull_request is not None
    if completed_rounds >= rounds:
        move_task(
            uow,
            clock,
            task,
            TaskState.AWAITING_CI_CERTIFICATION,
            EventKind.TASK_AWAITING_CI_CERTIFICATION,
            attempt_id=attempt_id,
            payload={
                "head_sha": plan.head_sha,
                "pull_request": pull_request.number,
                "completed_rounds": completed_rounds,
                "required_rounds": rounds,
            },
        )
        return TaskState.AWAITING_CI_CERTIFICATION
    move_task(
        uow,
        clock,
        task,
        TaskState.AWAITING_EXTERNAL_REVIEW,
        EventKind.TASK_AWAITING_EXTERNAL_REVIEW,
        attempt_id=attempt_id,
        payload={
            "head_sha": plan.head_sha,
            "pull_request": pull_request.number,
            "completed_rounds": completed_rounds,
            "required_rounds": rounds,
        },
    )
    return TaskState.AWAITING_EXTERNAL_REVIEW


def open_review_cycle(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task_id: str,
    pull_request: PullRequest,
    head_sha: str,
    policy: dict[str, Any],
    trigger: str = "publication",
) -> ExternalReviewCycle | None:
    """One cycle per published head, carrying the components the policy expects (23).

    A second call for the same head is a no-op: publication is re-runnable and a repeat
    must not manufacture a round."""
    if required_rounds(policy) <= 0:
        return None
    existing = [
        cycle
        for cycle in uow.review_cycles.list_for_pull_request(pull_request.id)
        if cycle.head_sha == head_sha and cycle.trigger == trigger
    ]
    if existing:
        return existing[0]
    components = list(configured_components(policy))
    cycle = ExternalReviewCycle(
        id=new_id(),
        pull_request_id=pull_request.id,
        head_sha=head_sha,
        components=components,
        completed_components={},
        state=CycleState.OPEN.value,
        opened_at=clock.now(),
        trigger=trigger,
    )
    uow.review_cycles.add(cycle)
    record_event(
        uow,
        clock,
        EventKind.EXTERNAL_REVIEW_CYCLE_OPENED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task_id,
        payload={
            "cycle_id": cycle.id,
            "head_sha": head_sha,
            "components": components,
            "trigger": trigger,
            "pull_request": pull_request.number,
        },
    )
    return cycle
