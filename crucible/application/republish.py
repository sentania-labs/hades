"""Manual recovery from a failed publication (09, 23)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from crucible.application.errors import ForbiddenError, NotFoundError, TransitionNotAllowedError
from crucible.application.publish import collected_bundle_sha256
from crucible.application.review import latest_work_attempt
from crucible.application.task_access import require_task_principal
from crucible.application.transitions import move_task
from crucible.contracts.api import PublishRetryRequest
from crucible.domain.entities import AcceptanceVerdict, Principal, Role, Task
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

DEFAULT_PUBLISH_RETRY_MAX = 3


def republish_task(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    request: PublishRetryRequest,
) -> Task:
    """Authorize one retry without changing the accepted head or bundle."""
    if principal.role not in (Role.ORCHESTRATOR, Role.OPERATOR, Role.ADMIN):
        raise ForbiddenError("only an orchestrator or operator principal requests republish")
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    if task.state is not TaskState.PUBLISH_FAILED:
        raise TransitionNotAllowedError(
            f"republish is accepted only in publish_failed; task is {task.state.value}"
        )

    work = latest_work_attempt(uow, task)
    if work is None:
        raise TransitionNotAllowedError("the task has no implementing or correcting attempt")
    attempt, execution = work
    accepted = [
        result
        for result in uow.acceptance.list_for_task(task.id)
        if result.superseded_at is None and result.verdict is AcceptanceVerdict.ACCEPTED
    ]
    if not accepted or accepted[-1].head_sha != (task.head_sha or ""):
        raise TransitionNotAllowedError(
            "republish requires the still-current AcceptanceResult for the same head"
        )

    failure = uow.events.latest_for_task_kind(task.id, EventKind.TASK_PUBLISH_FAILED.value)
    started = uow.events.latest_for_task_kind(task.id, EventKind.PUBLISH_STARTED.value)
    if failure is None or started is None:
        raise TransitionNotAllowedError("the failed publication has no complete audit record")
    expected_bundle = f"{attempt.workspace_path}/output/work_branch.bundle"
    recorded_head = str(failure.payload.get("head_sha") or started.payload.get("head_sha") or "")
    recorded_bundle = str(started.payload.get("bundle") or "")
    recorded_bundle_sha256 = str(started.payload.get("bundle_sha256") or "")
    if recorded_head != (task.head_sha or ""):
        raise TransitionNotAllowedError(
            f"the failed publication was for {recorded_head}; the task head is {task.head_sha}"
        )
    if failure.attempt_id not in (None, attempt.id) or recorded_bundle != expected_bundle:
        raise TransitionNotAllowedError(
            "republish requires the same sealed bundle and implementing attempt"
        )
    # If the previous failure was a bundle-seal / claim-gone failure, refuse
    # without consuming a retry.
    failure_step = str(failure.payload.get("step") or "")
    failure_detail = str(failure.payload.get("detail") or "")
    if failure_step == "bundle-seal":
        lower = failure_detail.lower()
        if "gone" in lower or "missing" in lower or "no branch bundle" in lower:
            raise TransitionNotAllowedError(f"republish is not possible: {failure_detail}")

    sealed_sha256 = collected_bundle_sha256(uow, attempt) or recorded_bundle_sha256
    bundle_file = Path(expected_bundle)
    ws = str(attempt.workspace_path)
    if bundle_file.is_file():
        current_sha256 = hashlib.sha256(bundle_file.read_bytes()).hexdigest()
    elif ws.startswith("fake:///") or ws.startswith("k8s://"):
        current_sha256 = sealed_sha256
    else:
        current_sha256 = ""
    if (
        not recorded_bundle_sha256
        or not sealed_sha256
        or recorded_bundle_sha256 != sealed_sha256
        or current_sha256 != sealed_sha256
    ):
        raise TransitionNotAllowedError(
            "republish requires the unchanged sealed bundle from the accepted attempt"
        )

    policy = execution.policy_snapshot or {}
    retry_max = int(policy.get("limits", {}).get("publish_retry_max", DEFAULT_PUBLISH_RETRY_MAX))
    previous = uow.events.latest_for_task_kind(task.id, EventKind.TASK_PUBLISHING.value)
    retries_used = int((previous.payload if previous else {}).get("retry_number", 0))
    if retries_used >= retry_max:
        raise TransitionNotAllowedError(
            f"publication retry cap reached: {retries_used} of {retry_max} retries used"
        )
    retry_number = retries_used + 1
    move_task(
        uow,
        clock,
        task,
        TaskState.PUBLISHING,
        EventKind.TASK_PUBLISHING,
        principal=principal.name,
        attempt_id=attempt.id,
        payload={
            "head_sha": task.head_sha,
            "bundle": recorded_bundle,
            "bundle_sha256": sealed_sha256,
            "reason": request.reason,
            "retry_number": retry_number,
            "publish_retry_max": retry_max,
            # Re-fetch and re-check ownership even after a post-push failure: a
            # checkpoint may have reached the work branch since then (#403).
            "resume_step": "push",
        },
    )
    return task
