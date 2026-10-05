"""The delivery half of the supervision tick (23, 09).

Three steps, in this order: publish every task that acceptance moved to `publishing`,
process any webhook deliveries that arrived, then poll every pull request in an observed
state and re-evaluate the post-PR gates.

All the GitHub and container I/O lives here; everything it produces is applied inside the
supervisor's fenced transactions by `crucible.application.observation` and
`crucible.application.publish`, which are pure of I/O. That split is what lets the
poll-only path and the webhook path be the same code.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Protocol, TypeVar

from crucible.application.auto_merge import auto_merge_enabled, certified_jobs_green
from crucible.application.decisions import open_escalation
from crucible.application.observation import (
    DIVERGENCE_STATES,
    OBSERVED_STATES,
    POLLED_STATES,
    ObservationResult,
    advance_delivery,
    apply_observation,
    correction_in_flight,
    evaluate_delivery_gates,
    observe_state,
    policy_for,
    poll_due,
    settle_pull_request_state,
    supersede_for_head,
    to_cycle,
)
from crucible.application.publish import (
    PublishPlan,
    advance_after_publish,
    build_plan,
    external_review_trigger,
    fail_publish,
    hold_publishing,
    open_review_cycle,
    push_url_for,
    record_external_review_requested,
    record_publish_started,
    record_token_minted,
    release_publishing_hold,
    repository_slug,
    request_external_review,
    upsert_pull_request,
)
from crucible.application.release_hold import (
    hold_document,
    prune_superseded,
    set_release_hold,
    watch_merge,
    watched,
)
from crucible.application.review import latest_work_attempt
from crucible.application.transitions import move_task, record_event
from crucible.application.wakes import create_wake
from crucible.contracts.task_contract import contract_sha256
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    PullRequestHead,
    PullRequestState,
    PushedBy,
    Task,
    TaskContract,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.external_review import completed_rounds
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import CORRECTION_STATES, TaskState
from crucible.domain.publication import body_sha256
from crucible.domain.secrets import redact
from crucible.domain.time import parse_rfc3339
from crucible.domain.waivers import WAIVER_KINDS
from crucible.ports.clock import Clock
from crucible.ports.github import (
    CheckRecord,
    GitHubClient,
    GitHubError,
    InstallationToken,
    MergeResult,
    Observation,
    PullRequestRef,
)
from crucible.ports.publish import MergeMainOutcome, MergeMainRequest, Publisher, PublishRequest
from crucible.ports.repository import UnitOfWork

log = logging.getLogger("crucible.delivery")


def _other_pull_request_note(others: tuple[PullRequestRef, ...]) -> str:
    named = " and ".join(f"#{other.number} ({other.state})" for other in others)
    if len(others) == 1:
        return (
            f"pull request {named} is also on the work branch; it is not the task's and "
            "is not adopted"
        )
    return (
        f"pull requests {named} are also on the work branch; they are not the task's and "
        "are not adopted"
    )


def _other_pull_request_fields(others: tuple[PullRequestRef, ...]) -> dict[str, Any]:
    """The first other pull request by number and state, and every one of them."""
    if not others:
        return {}
    return {
        "other_pull_request": others[0].number,
        "other_pull_request_state": others[0].state,
        "other_pull_requests": [{"number": other.number, "state": other.state} for other in others],
    }


T = TypeVar("T")

# hades #411: the states a conflicting pull request is acted on in, by a merge of main or
# a merge-main correction. Not `head_diverged`, whose head nobody has trusted yet, and
# not a correction's states, whose own head will replace the conflicting one.
CONFLICT_STATES: frozenset[TaskState] = DIVERGENCE_STATES | {TaskState.CI_CERTIFICATION_FAILED}
# The head decisions that take an out-of-band head back as Crucible's (`recollect` is
# the legacy name of `adopt`).
ADOPT_ACTIONS: frozenset[str] = frozenset({"adopt", "recollect"})

# hades #379: a task in one of these is done with its branch; a quota checkpoint is never
# pushed to it or recorded against it.
FINISHED_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.MERGED,
        TaskState.RELEASE_CANDIDATE,
        TaskState.RELEASED,
        TaskState.REJECTED,
        TaskState.CANCELLED,
        TaskState.CLOSED,
    }
)


class FencedHost(Protocol):
    """What the coordinator needs of the supervisor: a fenced transaction and a thread."""

    def _fenced(self) -> AbstractContextManager[UnitOfWork]: ...

    async def _db(self, fn: Callable[[], T]) -> T: ...


@dataclass(frozen=True, slots=True)
class DeliveryConfig:
    poll_interval_seconds: int = 120
    reactions_poll_interval_seconds: int = 60
    ci_log_excerpt_bytes: int = 64 * 1024
    publisher_timeout_seconds: int = 600
    publisher_image: str | None = None


@dataclass(frozen=True, slots=True)
class PollPlan:
    task_id: str
    pull_request_id: str
    number: int
    repository_name: str
    installation_id: int | None
    base_ref: str
    attempt_id: str
    with_reactions: bool
    # The failed check whose log excerpt is still to be fetched: (source, GitHub id).
    failed_check: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class MergePlan:
    task_id: str
    pull_request_id: str
    number: int
    repository_name: str
    installation_id: int | None
    certified_head_sha: str
    base_ref: str


@dataclass(frozen=True, slots=True)
class MainCIPlan:
    task_id: str
    repository_name: str
    installation_id: int | None
    merge_sha: str
    pull_request_number: int
    # RFC 3339; the order of merges on main, which decides what supersedes what.
    merged_at: str = ""
    base_ref: str = "main"


@dataclass(frozen=True, slots=True)
class DeclineReplyPlan:
    task_id: str
    repository_name: str
    installation_id: int
    pull_request_number: int
    comment_id: str
    github_comment_id: str
    disposition_id: str
    reasoning: str


@dataclass(slots=True)
class DeliveryCounts:
    published: int = 0
    polled: int = 0
    deliveries: int = 0
    gate_passes: int = 0


class DeliveryCoordinator:
    """Publication and observation, driven from the supervisor tick."""

    def __init__(
        self,
        host: FencedHost,
        clock: Clock,
        *,
        github: GitHubClient | None = None,
        publisher: Publisher | None = None,
        config: DeliveryConfig | None = None,
    ) -> None:
        self._host = host
        self._clock = clock
        self._github = github
        self._publisher = publisher
        self.config = config or DeliveryConfig()
        # hades FDY-0139: GitHub refused a call for the rate limit. Nothing sleeps inside
        # the tick; polling and publication wait for a later tick past this time.
        self.rate_limited_until: datetime | None = None
        # hades #411: main's checks are read at the pull request poll interval, not on
        # every tick.
        self._main_ci_polled_at: datetime | None = None

    def _rate_limited(self) -> bool:
        until = self.rate_limited_until
        if until is None:
            return False
        if self._clock.now() >= until:
            self.rate_limited_until = None
            return False
        return True

    def _defer_for_rate_limit(self, exc: GitHubError) -> None:
        seconds = exc.retry_after if exc.retry_after is not None else 60.0
        self.rate_limited_until = self._clock.now() + timedelta(seconds=max(1.0, seconds))
        log.warning(
            "github rate limit: delivery waits for a later tick",
            extra={"seconds": seconds, "path": exc.path},
        )

    @property
    def enabled(self) -> bool:
        return self._github_ready()

    async def _github_ready_async(self) -> bool:
        """`_github_ready` off the event loop: on Kubernetes it reads the App Secret
        through the API server, which must never stall the supervisor's tick."""
        if self._github is None:
            return False
        return await asyncio.to_thread(self._github_ready)

    def _github_ready(self) -> bool:
        """A client exists and, when it can say so, an App credential is in place. The
        credential may arrive at runtime from the Connect GitHub flow (ADR 0017)."""
        if self._github is None:
            return False
        configured = getattr(self._github, "configured", None)
        return not callable(configured) or bool(configured())

    def _fenced(self) -> Iterator[UnitOfWork]:  # pragma: no cover - thin delegate
        raise NotImplementedError

    # ----- publication --------------------------------------------------

    async def waiting_reason(self) -> str | None:
        """Why no publication can start, or None when one can (hades FDY-0133)."""
        if self._github is None:
            return (
                "GitHub is not configured for this deployment: no GitHub App client is "
                "wired, so no branch can be pushed"
            )
        if self._publisher is None:
            return (
                "no publisher is configured for this deployment: neither a Docker nor a "
                "Kubernetes provider is wired to push the branch"
            )
        if not await self._github_ready_async():
            return (
                "GitHub is not ready: the App credential is not in place; connect the App "
                "on the GitHub page"
            )
        return None

    async def publish(self) -> int:
        if self._rate_limited():
            return 0
        reason = await self.waiting_reason()
        if reason is not None:
            # Never silent: every task waiting in `publishing` says why, is logged once,
            # and escalates after the publisher's own time limit (hades FDY-0133).
            await self._host._db(lambda: self._hold_publishing(reason))
            return 0
        plans = await self._host._db(self._take_publishing)
        done = 0
        for plan in plans:
            if self._rate_limited():
                break
            if await self._publish_one(plan):
                done += 1
        return done

    async def push_quota_checkpoint(
        self, attempt_id: str, *, required: bool
    ) -> tuple[bool, str] | None:
        """Push a collected quota checkpoint without opening or updating a pull request.

        None while GitHub's rate limit says to wait: the checkpoint stays pending for a
        later tick rather than failing (hades FDY-0139)."""
        if self._rate_limited():
            return None
        if self._github is None or self._publisher is None or not await self._github_ready_async():
            if required:
                return False, "the GitHub publisher is not configured"
            return True, "a publisher is not required for this repository"
        finished = await self._host._db(lambda: self._task_finished(attempt_id))
        if finished is not None:
            # hades #379: the task is merged or otherwise finished; a checkpoint pushed now
            # would move its branch on with work nobody reviewed.
            return False, f"the task is {finished.value}; the checkpoint is not pushed"
        plan = await self._host._db(lambda: self._checkpoint_plan(attempt_id))
        if plan is None:
            return True, "the attempt has no checkpoint to push"
        if plan.installation_id is None:
            return False, f"repository {plan.repository_name} has no installation id"
        token: InstallationToken | None = None
        try:
            token = await asyncio.to_thread(
                self._github.installation_token,
                installation_id=plan.installation_id,
                repository=plan.repository_name,
            )
            await self._host._db(lambda: self._record_minted(plan, token))
            outcome = await self._publisher.push(self._publish_request(plan), token)
            await self._host._db(lambda: self._record_publisher(plan, outcome))
            if not outcome.pushed:
                return False, outcome.detail or f"publisher exited {outcome.exit_code}"
            remote = await asyncio.to_thread(
                self._github.remote_head,
                token,
                repository=plan.repository_name,
                ref=plan.work_branch,
            )
            if remote != plan.head_sha:
                return False, f"remote branch is at {remote}, expected {plan.head_sha}"
            recorded = await self._host._db(
                lambda: self._record_pushed(plan, remote, checkpoint=True)
            )
            if not recorded:
                return False, "the task moved on while the checkpoint was pushed"
            return True, "checkpoint pushed"
        except GitHubError as exc:
            if exc.response_class == "rate_limited":
                self._defer_for_rate_limit(exc)
                return None
            return False, redact(exc.message)
        except Exception as exc:
            return False, redact(f"{type(exc).__name__}: {exc}")
        finally:
            if token is not None:
                token.discard()

    def _checkpoint_plan(self, attempt_id: str) -> PublishPlan | None:
        with self._host._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            if attempt is None or attempt.exit_class is not ExitClass.QUOTA_EXHAUSTED:
                return None
            task = uow.tasks.get(attempt.task_id)
            execution = uow.executions.get(attempt.execution_id)
            if task is None or execution is None or not task.head_sha:
                return None
            return build_plan(uow, task, (attempt, execution))

    def _task_finished(self, attempt_id: str) -> TaskState | None:
        with self._host._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            task = uow.tasks.get(attempt.task_id) if attempt is not None else None
            if task is None or task.state not in FINISHED_STATES:
                return None
            return task.state

    def _publish_request(self, plan: PublishPlan) -> PublishRequest:
        return PublishRequest(
            attempt_id=plan.attempt_id,
            task_id=plan.task_id,
            owner=plan.external_id,
            repository_url=plan.push_url,
            work_branch=plan.work_branch,
            base_ref=plan.base_ref,
            expected_head=plan.head_sha,
            bundle_path=plan.bundle_path,
            bundle_sha256=plan.bundle_sha256,
            image=self.config.publisher_image or plan.image,
            policy=plan.policy,
            author_name=str(plan.policy.get("git", {}).get("author_name", "crucible-worker")),
            author_email=str(
                plan.policy.get("git", {}).get(
                    "author_email", "crucible-worker@users.noreply.github.com"
                )
            ),
            timeout_seconds=self.config.publisher_timeout_seconds,
        )

    def _hold_publishing(self, reason: str) -> None:
        with self._host._fenced() as uow:
            for task in uow.tasks.list_by_state(TaskState.PUBLISHING, for_update=True):
                if hold_publishing(
                    uow,
                    self._clock,
                    task,
                    reason=reason,
                    escalate_after_seconds=self.config.publisher_timeout_seconds,
                ):
                    log.warning(
                        "task %s is in publishing and its publication cannot start: %s",
                        task.external_id,
                        reason,
                        extra={"task_id": task.id},
                    )
            uow.commit()

    def _take_publishing(self) -> list[PublishPlan]:
        plans: list[PublishPlan] = []
        with self._host._fenced() as uow:
            for task in uow.tasks.list_by_state(TaskState.PUBLISHING, for_update=True):
                release_publishing_hold(uow, self._clock, task)
                work = latest_work_attempt(uow, task)
                if work is None:
                    fail_publish(
                        uow,
                        self._clock,
                        task,
                        step="plan",
                        detail="no implementing or correcting attempt carries this head",
                    )
                    continue
                plan = build_plan(uow, task, work)
                record_publish_started(uow, self._clock, plan)
                if plan.problem:
                    fail_publish(
                        uow,
                        self._clock,
                        task,
                        step="title",
                        detail=plan.problem,
                        attempt_id=plan.attempt_id,
                    )
                    continue
                if plan.installation_id is None:
                    fail_publish(
                        uow,
                        self._clock,
                        task,
                        step="mint",
                        detail=(
                            f"repository {plan.repository_name} is registered without an "
                            "installation id; registration records it (23)"
                        ),
                        attempt_id=plan.attempt_id,
                    )
                    continue
                plans.append(plan)
            uow.commit()
        return plans

    async def _publish_one(self, plan: PublishPlan) -> bool:
        assert self._github is not None and self._publisher is not None
        token: InstallationToken | None = None
        github_step = "installation_token"
        # hades #379: every other pull request found open on the work branch beside the
        # task's own; none is adopted, and each is recorded and named in the wake.
        others: tuple[PullRequestRef, ...] = ()
        try:
            token = await asyncio.to_thread(
                self._github.installation_token,
                installation_id=plan.installation_id or 0,
                repository=plan.repository_name,
            )
            await self._host._db(lambda: self._record_minted(plan, token))
            if plan.existing_pr_number is not None and plan.deliverable_kind != "branch":
                # hades #379: the task's pull request is looked up before anything is
                # pushed. One merged or closed meanwhile gets no corrected head on its
                # branch, which a merge would leave behind the merged head (or a deleted
                # branch would get back).
                github_step = "github"
                known, others = await self._known_pull_request(token, plan)
                if known is None:
                    return False
            github_step = "branch_pushed_at_head"
            if plan.resume_step not in ("branch_pushed_at_head", "github"):
                outcome = await self._publisher.push(self._publish_request(plan), token)
                await self._host._db(lambda: self._record_publisher(plan, outcome))
                if not outcome.pushed:
                    await self._host._db(
                        lambda: self._fail(
                            plan,
                            step=outcome.step,
                            detail=outcome.detail or f"the publisher exited {outcome.exit_code}",
                            extra={"remote_head_before": outcome.remote_head_before},
                            others=others,
                        )
                    )
                    return False
            remote = await asyncio.to_thread(
                self._github.remote_head,
                token,
                repository=plan.repository_name,
                ref=plan.work_branch,
            )
            if remote != plan.head_sha:
                await self._host._db(
                    lambda: self._fail(
                        plan,
                        step="branch_pushed_at_head",
                        detail=(
                            f"the remote branch is at {remote}, not the accepted head "
                            f"{plan.head_sha}"
                        ),
                        others=others,
                    )
                )
                return False
            if not await self._host._db(lambda: self._record_pushed(plan, remote)):
                # hades #379: the task left publishing while the head was pushed (a poll
                # settled it as merged); the push is escalated, and nothing more of this
                # publication runs.
                return False
            if plan.deliverable_kind == "branch":
                await self._host._db(lambda: self._finish(plan, ref=None))
                return True
            github_step = "github"
            ref: PullRequestRef | None
            if plan.existing_pr_number is not None:
                # Looked up again: a merge or close can land while the head is pushed.
                ref, others = await self._known_pull_request(token, plan)
                if ref is None:
                    return False
            else:
                ref = await asyncio.to_thread(
                    self._github.find_pull_request,
                    token,
                    repository=plan.repository_name,
                    head_branch=plan.work_branch,
                )
            if ref is None or ref.state != "open":
                ref = await asyncio.to_thread(
                    self._github.create_pull_request,
                    token,
                    repository=plan.repository_name,
                    title=plan.title,
                    head_branch=plan.work_branch,
                    base_ref=plan.base_ref,
                    body=plan.body,
                    draft=plan.draft,
                )
            else:
                ref = await asyncio.to_thread(
                    self._github.update_pull_request,
                    token,
                    repository=plan.repository_name,
                    number=ref.number,
                    title=plan.title,
                    body=plan.body,
                    base_ref=plan.base_ref,
                )
            resolved = ref
            # A reused pull request has to match the contract, not merely carry the new
            # head: a PR retargeted to another base, or left as a draft the contract did
            # not ask for, delivers something else. Neither is force-corrected; the
            # publication fails and Foundry decides (23).
            mismatch: list[str] = []
            if resolved.base_ref != plan.base_ref:
                mismatch.append(
                    f"base_ref is {resolved.base_ref!r}, the contract says {plan.base_ref!r}"
                )
            if resolved.draft != plan.draft:
                mismatch.append(f"draft is {resolved.draft}, the contract says {plan.draft}")
            if mismatch:
                await self._host._db(
                    lambda: self._fail(
                        plan,
                        step="github",
                        detail=(
                            f"pull request #{resolved.number} does not match the "
                            f"contract: {'; '.join(mismatch)}"
                        ),
                        extra={"pull_request": resolved.number},
                        others=others,
                    )
                )
                return False
            await self._host._db(lambda: self._record_pull_request(plan, resolved))
            trigger = external_review_trigger(plan.policy)
            if trigger is not None:
                github_step = "external_review_request"
                previous = await self._host._db(lambda: self._external_review_request(plan))
                comment = await asyncio.to_thread(
                    request_external_review,
                    self._github,
                    token,
                    plan=plan,
                    pull_request_number=resolved.number,
                    previous=previous,
                )
                if comment is not None:
                    await self._host._db(
                        lambda: self._record_external_review_request(plan, resolved, comment)
                    )
            await self._host._db(lambda: self._finish(plan, ref=resolved, others=others))
            return True
        except GitHubError as exc:
            failure = exc
            if failure.response_class == "rate_limited":
                # Not a publication failure: the task stays in `publishing` and a later
                # tick resumes it, as a restart would (hades FDY-0139).
                self._defer_for_rate_limit(failure)
                await self._host._db(lambda: self._record_rate_limited(plan.task_id, failure))
                return False
            await self._host._db(
                lambda: self._fail(
                    plan,
                    step=github_step,
                    detail=failure.message,
                    response_class=failure.response_class,
                    others=others,
                )
            )
            return False
        except Exception as exc:  # the publisher or the transport, not a state change
            detail = f"{type(exc).__name__}: {exc}"
            log.warning("publication failed", extra={"task_id": plan.task_id, "error": detail})
            await self._host._db(
                lambda: self._fail(plan, step="publish", detail=detail, others=others)
            )
            return False
        finally:
            if token is not None:
                token.discard()

    async def _known_pull_request(
        self, token: InstallationToken, plan: PublishPlan
    ) -> tuple[PullRequestRef | None, tuple[PullRequestRef, ...]]:
        """hades #379: the task's own pull request, open, to publish to; None when it is
        merged or closed, which is then recorded. A task that has a pull request never
        gets a second one: the task's own is read by its number, and every other pull
        request open on the work branch (a reopened older one included) is never
        adopted. Those come second, for the publication to record and name in its
        wake."""
        assert self._github is not None
        number = plan.existing_pr_number
        known = await asyncio.to_thread(
            self._github.get_pull_request,
            token,
            repository=plan.repository_name,
            number=number or 0,
        )
        listed = await asyncio.to_thread(
            self._github.open_pull_requests,
            token,
            repository=plan.repository_name,
            head_branch=plan.work_branch,
        )
        others = tuple(ref for ref in listed if ref.number != number)
        if known.state == "open":
            return known, others
        if known.state == "closed" and not known.merged and not known.closed_by:
            closer = await asyncio.to_thread(
                self._github.closed_by,
                token,
                repository=plan.repository_name,
                number=known.number,
            )
            known = replace(known, closed_by=closer)
        gone = known
        await self._host._db(lambda: self._record_pull_request_gone(plan, gone, others))
        return None, others

    def _record_minted(self, plan: PublishPlan, token: InstallationToken | None) -> None:
        assert token is not None
        with self._host._fenced() as uow:
            record_token_minted(
                uow,
                self._clock,
                plan,
                expires_at=token.expires_at,
                permissions=token.permissions,
            )
            uow.commit()

    def _record_publisher(self, plan: PublishPlan, outcome: Any) -> None:
        with self._host._fenced() as uow:
            record_event(
                uow,
                self._clock,
                EventKind.PUBLISHER_FINISHED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=plan.task_id,
                attempt_id=plan.attempt_id,
                payload={
                    "pushed": outcome.pushed,
                    "step": outcome.step,
                    "exit_code": outcome.exit_code,
                    "bundle_head": outcome.head_sha,
                    "remote_head_before": outcome.remote_head_before,
                    # Both already redacted by the publisher adapter (12); truncated
                    # here so one failing container cannot fill the event log.
                    "detail": outcome.detail[:500],
                    "log_tail": outcome.log_tail[-2000:],
                },
            )
            uow.commit()

    def _record_pushed(
        self, plan: PublishPlan, remote: str | None, *, checkpoint: bool = False
    ) -> bool:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            push_is_current = task is not None and (
                (checkpoint and task.state not in FINISHED_STATES)
                or (not checkpoint and task.state is TaskState.PUBLISHING)
            )
            if not push_is_current:
                if task is not None:
                    head = remote or plan.head_sha
                    if checkpoint:
                        # A checkpoint task was never publishing, and its push merged
                        # nothing; the branch now carries an ungated head.
                        summary = (
                            f"a quota checkpoint was pushed to the branch of a task that is "
                            f"already {task.state.value}; nothing was merged"
                        )
                        question = (
                            f"{summary}. Head {head} is on the work branch and is not "
                            "recorded; decide what becomes of the branch."
                        )
                        reason = WakeReason.CHECKPOINT_AFTER_FINISH
                    else:
                        summary = (
                            f"head {head} was pushed after the task left publishing and is "
                            f"already {task.state.value}"
                        )
                        question = (
                            f"{summary}. The push is not recorded as the merged head; "
                            "decide whether the merge and branch state stand."
                        )
                        reason = WakeReason.MERGED
                    open_escalation(
                        uow,
                        self._clock,
                        task=task,
                        attempt_id=plan.attempt_id,
                        question=question,
                        wake_reason=reason,
                        summary=summary,
                    )
                    uow.commit()
                return False
            record_event(
                uow,
                self._clock,
                EventKind.BRANCH_PUSHED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=plan.task_id,
                attempt_id=plan.attempt_id,
                payload={
                    "work_branch": plan.work_branch,
                    "head_sha": remote,
                    "repository": plan.repository_name,
                    # hades #379: a quota checkpoint is pushed before any gate ran; a
                    # merge of it is escalated, never taken as a gated publication.
                    "checkpoint": checkpoint,
                },
            )
            uow.commit()
            return True

    def _record_pull_request(self, plan: PublishPlan, ref: Any) -> None:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            if task is None or task.state is not TaskState.PUBLISHING:
                return
            upsert_pull_request(
                uow,
                self._clock,
                task=task,
                plan=plan,
                ref=ref,
                body_hash=body_sha256(plan.body),
            )
            uow.commit()

    def _record_pull_request_gone(
        self, plan: PublishPlan, known: PullRequestRef, others: tuple[PullRequestRef, ...]
    ) -> None:
        """hades #379: the task's pull request, `known`, is merged or closed. The merge
        or close is recorded on the PR row as a poll would record it. A merge settles the
        task as merged (merge wins); a close fails the publication with a wake naming the
        pull request that says to reopen it and then republish, or to cancel, as the
        poll's close wake does. A merge this row does not record offers no republish.
        `others`, the pull requests open on the work branch beside it, are recorded
        either way and named in a wake."""
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            if task is None or task.state is not TaskState.PUBLISHING:
                return
            pull_request = uow.pull_requests.get_for_task(task.id, for_update=True)
            number = plan.existing_pr_number
            if pull_request is not None and known.number == pull_request.number:
                observe_state(
                    uow,
                    self._clock,
                    task=task,
                    pull_request=pull_request,
                    observation=Observation(pull_request=known),
                    result=ObservationResult(),
                )
            if pull_request is not None and pull_request.state is PullRequestState.MERGED:
                # Settled just now by the observation, or here for a merge recorded
                # before this lookup; settling a task already merged does nothing.
                settle_pull_request_state(uow, self._clock, task=task, pull_request=pull_request)
                detail = (
                    f"pull request #{number} was merged before the corrected head "
                    f"{plan.head_sha} was published to it; no new pull request is opened"
                )
                if others:
                    detail += f"; {_other_pull_request_note(others)}"
                record_event(
                    uow,
                    self._clock,
                    EventKind.TASK_PUBLISH_FAILED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    attempt_id=plan.attempt_id,
                    payload={
                        "step": "github",
                        "detail": detail,
                        "head_sha": plan.head_sha,
                        "pull_request": number,
                        "pull_request_state": "merged",
                        **_other_pull_request_fields(others),
                    },
                )
                if others:
                    # The merge wake is about the task's own pull request; the others
                    # on its branch are Foundry's to decide.
                    create_wake(
                        uow,
                        self._clock,
                        principal_id=task.principal_id,
                        reason=WakeReason.OTHER_PULL_REQUEST_OPEN,
                        summary=(
                            f"pull request #{number} was merged; {_other_pull_request_note(others)}"
                        )[:500],
                        task=task,
                        attempt_id=plan.attempt_id,
                        extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
                    )
                uow.commit()
                return
            state = "merged" if known.merged else known.state
            closer = f" by {known.closed_by}" if known.closed_by else ""
            detail = (
                f"pull request #{known.number} is {state}{closer}; the corrected head "
                f"{plan.head_sha} is not published to it and no new pull request is opened"
            )
            extra: dict[str, Any] = {"pull_request": number, "pull_request_state": state}
            if others:
                detail += f"; {_other_pull_request_note(others)}"
                extra.update(_other_pull_request_fields(others))
            fail_publish(
                uow,
                self._clock,
                task,
                step="github",
                detail=detail,
                attempt_id=plan.attempt_id,
                extra=extra,
                closed_pull_request=known.number if state == "closed" else None,
                retryable=state == "closed",
            )
            uow.commit()

    def _external_review_request(self, plan: PublishPlan) -> Any | None:
        with self._host._fenced() as uow:
            return uow.events.latest_for_task_kind(
                plan.task_id, EventKind.EXTERNAL_REVIEW_REQUESTED.value
            )

    def _record_external_review_request(self, plan: PublishPlan, ref: Any, comment: Any) -> None:
        with self._host._fenced() as uow:
            record_external_review_requested(
                uow,
                self._clock,
                plan=plan,
                pull_request_number=ref.number,
                comment=comment,
            )
            uow.commit()

    def _fail(
        self,
        plan: PublishPlan,
        *,
        step: str,
        detail: str,
        response_class: str = "",
        extra: dict[str, Any] | None = None,
        others: tuple[PullRequestRef, ...] = (),
    ) -> None:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            if task is None or task.state is not TaskState.PUBLISHING:
                # hades #379: a poll moved the task on (a merge settles it as merged)
                # while the publication ran; its failure no longer describes the task.
                return
            if others:
                detail = f"{detail}; {_other_pull_request_note(others)}"
                extra = {**(extra or {}), **_other_pull_request_fields(others)}
            fail_publish(
                uow,
                self._clock,
                task,
                step=step,
                detail=detail,
                response_class=response_class,
                attempt_id=plan.attempt_id,
                extra=extra,
            )
            uow.commit()

    def _finish(
        self, plan: PublishPlan, *, ref: Any, others: tuple[PullRequestRef, ...] = ()
    ) -> None:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            if task is None or task.state is not TaskState.PUBLISHING:
                return
            body_hash = body_sha256(plan.body)
            pull_request = None
            if ref is not None:
                pull_request = uow.pull_requests.get_for_task(task.id, for_update=True)
                if pull_request is None:
                    pull_request, _opened = upsert_pull_request(
                        uow, self._clock, task=task, plan=plan, ref=ref, body_hash=body_hash
                    )
            record_event(
                uow,
                self._clock,
                EventKind.PUBLISH_COMPLETED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                attempt_id=plan.attempt_id,
                payload={
                    "head_sha": plan.head_sha,
                    "pull_request": pull_request.number if pull_request else None,
                    "url": pull_request.url if pull_request else None,
                    "body_sha256": body_hash,
                    **_other_pull_request_fields(others),
                },
            )
            rounds = 0
            if pull_request is not None:
                cycles = uow.review_cycles.list_for_pull_request(pull_request.id)
                # Rounds are counted per pull request across heads (23), so every
                # completed cycle on this PR counts, not only the ones on this head.
                rounds = completed_rounds([to_cycle(row) for row in cycles])
                evaluate_delivery_gates(
                    uow,
                    self._clock,
                    task=task,
                    attempt_id=plan.attempt_id,
                    pull_request=pull_request,
                    policy=plan.policy,
                    certification=None,
                    branch_pushed_sha=plan.head_sha,
                    phases=("publication",),
                )
            state = advance_after_publish(
                uow,
                self._clock,
                task=task,
                plan=plan,
                pull_request=pull_request,
                completed_rounds=rounds,
            )
            if pull_request is not None and state is TaskState.AWAITING_EXTERNAL_REVIEW:
                open_review_cycle(
                    uow,
                    self._clock,
                    task_id=task.id,
                    pull_request=pull_request,
                    head_sha=plan.head_sha,
                    policy=plan.policy,
                )
            if pull_request is not None:
                create_wake(
                    uow,
                    self._clock,
                    principal_id=task.principal_id,
                    reason=WakeReason.PUBLISHED,
                    summary=f"published, PR #{pull_request.number}",
                    task=task,
                    attempt_id=plan.attempt_id,
                    extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
                )
            if others and pull_request is not None:
                # hades #379: published to the task's own pull request; the others on the
                # work branch are not the task's, and Foundry decides what becomes of them.
                create_wake(
                    uow,
                    self._clock,
                    principal_id=task.principal_id,
                    reason=WakeReason.OTHER_PULL_REQUEST_OPEN,
                    summary=(
                        f"head {plan.head_sha} was published to pull request "
                        f"#{pull_request.number}; {_other_pull_request_note(others)}"
                    )[:500],
                    task=task,
                    attempt_id=plan.attempt_id,
                    extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
                )
            uow.commit()

    # ----- observation --------------------------------------------------

    async def observe(self) -> int:
        if not await self._github_ready_async():
            return 0
        await self._post_decline_replies()
        polled = 0
        if not self._rate_limited():
            plans = await self._host._db(self._due_polls)
            for plan in plans:
                if self._rate_limited():
                    # The rest are still due and are polled on a later tick.
                    break
                if await self._observe_one(plan):
                    polled += 1
        await self._host._db(self._evaluate_gates)
        if not self._rate_limited():
            merge_plans = await self._host._db(self._ready_merges)
            for merge_plan in merge_plans:
                if self._rate_limited():
                    break
                await self._merge_one(merge_plan)
        now = self._clock.now()
        if not self._rate_limited() and (
            self._main_ci_polled_at is None
            or now - self._main_ci_polled_at >= timedelta(seconds=self.config.poll_interval_seconds)
        ):
            self._main_ci_polled_at = now
            for main_plan in await self._host._db(self._main_ci_plans):
                if self._rate_limited():
                    break
                await self._observe_main_ci(main_plan)
        return polled

    def _decline_reply_plans(self) -> list[DeclineReplyPlan]:
        plans: list[DeclineReplyPlan] = []
        with self._host._fenced() as uow:
            states = OBSERVED_STATES | CORRECTION_STATES | {TaskState.PUBLISHING}
            for state in sorted(states, key=lambda item: item.value):
                for task in uow.tasks.list_by_state(state):
                    pull = uow.pull_requests.get_for_task(task.id)
                    repository = uow.repositories.get(task.repository_id)
                    if pull is None or repository is None or repository.installation_id is None:
                        continue
                    events = uow.events.list_for_task(task.id, after_seq=0, limit=10_000)
                    posted = {
                        str(event.payload.get("disposition_id"))
                        for event in events
                        if event.kind == EventKind.DISPOSITION_RECORDED.value
                        and event.payload.get("reply_posted") is True
                    }
                    for event in events:
                        if (
                            event.kind != EventKind.DISPOSITION_RECORDED.value
                            or event.payload.get("reply_pending") is not True
                            or str(event.payload.get("disposition_id")) in posted
                        ):
                            continue
                        comment = uow.review_comments.get(
                            str(event.payload.get("review_comment_id"))
                        )
                        if comment is None:
                            continue
                        plans.append(
                            DeclineReplyPlan(
                                task_id=task.id,
                                repository_name=repository.name,
                                installation_id=repository.installation_id,
                                pull_request_number=pull.number,
                                comment_id=comment.id,
                                github_comment_id=comment.github_id,
                                disposition_id=str(event.payload["disposition_id"]),
                                reasoning=str(event.payload.get("reasoning") or "Declined"),
                            )
                        )
        return plans

    async def _post_decline_replies(self) -> None:
        if self._github is None or self._rate_limited():
            return
        for plan in await self._host._db(self._decline_reply_plans):
            token: InstallationToken | None = None
            try:
                token = await asyncio.to_thread(
                    self._github.installation_token,
                    installation_id=plan.installation_id,
                    repository=plan.repository_name,
                )
                reply = await asyncio.to_thread(
                    self._github.reply_to_review_comment,
                    token,
                    repository=plan.repository_name,
                    number=plan.pull_request_number,
                    comment_id=plan.github_comment_id,
                    body=plan.reasoning,
                )
                await self._host._db(partial(self._record_decline_reply, plan, reply.github_id))
            except GitHubError as exc:
                if exc.response_class == "rate_limited":
                    self._defer_for_rate_limit(exc)
                    return
                log.warning("posting declined finding reply failed", exc_info=True)
            finally:
                if token is not None:
                    token.discard()

    def _record_decline_reply(self, plan: DeclineReplyPlan, github_id: str) -> None:
        with self._host._fenced() as uow:
            record_event(
                uow,
                self._clock,
                EventKind.DISPOSITION_RECORDED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=plan.task_id,
                payload={
                    "disposition_id": plan.disposition_id,
                    "review_comment_id": plan.comment_id,
                    "github_reply_id": github_id,
                    "reply_posted": True,
                },
            )
            uow.commit()

    def _main_ci_plans(self) -> list[MainCIPlan]:
        """hades #411: the merge commits Crucible made whose checks on main have no
        verdict yet, oldest first. A commit merged before the newest judged one is
        superseded and never polled; the held red commit is, so a re-run is seen."""
        with self._host._fenced() as uow:
            document = hold_document(uow)
        plans = [
            MainCIPlan(
                task_id=str(entry["task_id"]),
                repository_name=str(entry["repository"]),
                installation_id=(
                    int(entry["installation_id"])
                    if entry.get("installation_id") is not None
                    else None
                ),
                merge_sha=str(entry["merge_sha"]),
                pull_request_number=int(entry["pull_request"]),
                merged_at=str(entry["merged_at"]),
                base_ref=str(entry.get("base_ref") or "main"),
            )
            for entry in watched(document)
        ]
        return sorted(plans, key=lambda plan: plan.merged_at)

    async def _observe_main_ci(self, plan: MainCIPlan) -> None:
        assert self._github is not None
        token: InstallationToken | None = None
        main_tip: str | None = None
        excerpt = ""
        try:
            token = await asyncio.to_thread(
                self._github.installation_token,
                installation_id=plan.installation_id or 0,
                repository=plan.repository_name,
            )
            checks = tuple(
                await asyncio.to_thread(
                    self._github.checks_for_commit,
                    token,
                    repository=plan.repository_name,
                    head_sha=plan.merge_sha,
                )
            )
            completed = [row for row in checks if row.source != "check_suite"]
            if not completed or any(row.status != "completed" for row in completed):
                return
            failed = [row for row in completed if row.conclusion not in {"success", "skipped"}]
            if failed:
                raw = await asyncio.to_thread(
                    self._github.ci_failure_log,
                    token,
                    repository=plan.repository_name,
                    source=failed[0].source,
                    external_id=failed[0].external_id,
                    limit_bytes=self.config.ci_log_excerpt_bytes,
                )
                # 12: a job log is text the repository controls; redacted at the fetch.
                excerpt = redact(raw.decode("utf-8", "replace")[-4000:]) if raw else ""
            else:
                # A green commit other than the held one clears the hold only when it
                # is main's current tip, which is read here rather than assumed.
                main_tip = await asyncio.to_thread(
                    self._github.remote_head,
                    token,
                    repository=plan.repository_name,
                    ref=plan.base_ref,
                )
        except GitHubError as exc:
            if exc.response_class == "rate_limited":
                self._defer_for_rate_limit(exc)
            return
        finally:
            if token is not None:
                token.discard()
        await self._host._db(lambda: self._record_main_ci(plan, failed, excerpt, main_tip))

    def _record_main_ci(
        self,
        plan: MainCIPlan,
        failed: list[CheckRecord],
        excerpt: str = "",
        main_tip: str | None = None,
    ) -> None:
        """Judge one merge commit on main, re-reading the hold in this transaction.

        Red holds the release on this commit and opens one fix-main task. Green clears
        the hold only when it is for the held commit itself or for main's verified
        current tip; any other green commit only moves the watermark, so an older green
        plan can never lift a hold a newer red commit set (hades #411)."""
        with self._host._fenced() as uow:
            document = hold_document(uow)
            if not any(entry.get("merge_sha") == plan.merge_sha for entry in watched(document)):
                # Judged already, or superseded by a newer judged commit.
                return
            held_sha = str(document.get("merge_sha") or "") if document.get("held") else ""
            if failed and held_sha == plan.merge_sha:
                # Still red where it is held: nothing new to record.
                return
            merged_at = parse_rfc3339(plan.merged_at)
            if failed:
                merged_numbers = [int(n) for n in document.get("merged_pull_requests", [])]
                if plan.pull_request_number not in merged_numbers:
                    merged_numbers.append(plan.pull_request_number)
                fix_task_id = document.get("fix_task_id") if held_sha else None
                if not fix_task_id:
                    fix_task_id = self._open_fix_main_task(
                        uow, plan, failed, merged_numbers, excerpt
                    )
                document.update(
                    {
                        "held": True,
                        "merge_sha": plan.merge_sha,
                        "merged_at": plan.merged_at,
                        "repository": plan.repository_name,
                        "failing_jobs": [
                            f"{row.name}: {row.conclusion or 'failed'}" for row in failed
                        ],
                        "merged_pull_requests": merged_numbers,
                        "fix_task_id": fix_task_id,
                    }
                )
            else:
                clears = not held_sha or plan.merge_sha in (held_sha, main_tip)
                if clears:
                    document.update(
                        {
                            "held": False,
                            "merge_sha": None,
                            "fix_task_id": None,
                            "failing_jobs": [],
                            "last_green_sha": plan.merge_sha,
                            "last_green_at": plan.merged_at,
                            # Pull requests merged after this green commit are still
                            # unproven on main.
                            "merged_pull_requests": [
                                int(entry["pull_request"])
                                for entry in watched(document)
                                if parse_rfc3339(str(entry["merged_at"])) > merged_at
                            ],
                        }
                    )
            watermark = document.get("watermark_at")
            if watermark is None or parse_rfc3339(str(watermark)) < merged_at:
                document["watermark_at"] = plan.merged_at
            keep_sha = plan.merge_sha if failed else ""
            document["watching"] = [
                entry
                for entry in watched(document)
                if entry.get("merge_sha") != plan.merge_sha or entry.get("merge_sha") == keep_sha
            ]
            prune_superseded(document)
            set_release_hold(uow, self._clock, document=document)
            uow.commit()

    def _open_fix_main_task(
        self,
        uow: UnitOfWork,
        plan: MainCIPlan,
        failed: list[CheckRecord],
        merged_numbers: list[int],
        excerpt: str = "",
    ) -> str:
        source = uow.tasks.get(plan.task_id)
        assert source is not None
        prior = uow.contracts.get(source.id, source.contract_version)
        assert prior is not None
        document = copy.deepcopy(prior.document)
        external_id = f"{source.external_id}-fix-main-{plan.merge_sha[:8]}"
        document.update(
            {
                "external_id": external_id,
                "title": f"Fix red main after PR #{plan.pull_request_number}",
                "objective": (
                    f"Main is red at {plan.merge_sha} after Crucible merged pull request "
                    f"#{plan.pull_request_number}. Fix main forward and restore every "
                    "required check. Failing jobs: "
                    + "; ".join(f"{row.name}: {row.conclusion or 'failed'}" for row in failed)
                    + ". Merged pull requests since the last green main: "
                    + ", ".join(f"#{number}" for number in merged_numbers)
                    + "."
                    + (f"\n\nFailing job output (tail):\n{excerpt}" if excerpt else "")
                ),
                "correction": None,
            }
        )
        # The fix lands from a branch of its own, cut from the red main, never from the
        # merged pull request's work branch.
        repository_section = dict(document.get("repository") or {})
        repository_section["work_branch"] = f"crucible/{external_id}"
        repository_section["base_ref"] = plan.base_ref
        document["repository"] = repository_section
        now = self._clock.now()
        task = Task(
            id=new_id(),
            external_id=external_id,
            principal_id=source.principal_id,
            project=source.project,
            title=str(document["title"]),
            state=TaskState.SCHEDULED,
            contract_version=1,
            policy_name=source.policy_name,
            policy_version=source.policy_version,
            repository_id=source.repository_id,
            created_at=now,
            updated_at=now,
        )
        uow.tasks.add(task)
        uow.contracts.add(
            TaskContract(
                id=new_id(),
                task_id=task.id,
                version=1,
                document=document,
                sha256=contract_sha256(document),
                submitted_at=now,
            )
        )
        record_event(
            uow,
            self._clock,
            EventKind.TASK_SUBMITTED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={"external_id": external_id, "reason": "fix_main", "merge_sha": plan.merge_sha},
        )
        record_event(
            uow,
            self._clock,
            EventKind.TASK_SCHEDULED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={"reason": "fix_main", "contract_version": 1},
        )
        return task.id

    def _ready_merges(self) -> list[MergePlan]:
        out: list[MergePlan] = []
        with self._host._fenced() as uow:
            if not auto_merge_enabled(uow):
                return out
            for task in uow.tasks.list_by_state(TaskState.READY_FOR_MERGE):
                policy = policy_for(uow, task)
                if not bool(policy.get("delivery", {}).get("auto_merge", True)):
                    continue
                pull_request = uow.pull_requests.get_for_task(task.id)
                if (
                    pull_request is None
                    or pull_request.state is not PullRequestState.OPEN
                    or pull_request.head_sha != task.head_sha
                    # hades #411: GitHub's word on mergeability is the only gate; a head
                    # behind current main is merged without a re-test against it. Only
                    # a conflict holds the merge, and the poll resolves that.
                    or pull_request.mergeable is False
                    or pull_request.mergeable_state == "dirty"
                ):
                    continue
                certification = uow.ci_certifications.get_for_head(
                    pull_request.id, task.head_sha or ""
                )
                if not certified_jobs_green(certification):
                    continue
                repository = uow.repositories.get(task.repository_id)
                if repository is None or not task.head_sha:
                    continue
                refusal_matches = (
                    pull_request.merge_refusal_cause is not None
                    and pull_request.merge_refusal_head_sha
                    == (pull_request.observed_head_sha or pull_request.head_sha)
                    and pull_request.merge_refusal_base_ref
                    == (pull_request.observed_base_ref or pull_request.base_ref)
                    and pull_request.merge_refusal_mergeable_state == pull_request.mergeable_state
                )
                if (
                    refusal_matches
                    and pull_request.merge_retry_at is not None
                    and pull_request.merge_retry_at > self._clock.now()
                ):
                    continue
                out.append(
                    MergePlan(
                        task_id=task.id,
                        pull_request_id=pull_request.id,
                        number=pull_request.number,
                        repository_name=repository_slug(repository),
                        installation_id=repository.installation_id,
                        certified_head_sha=task.head_sha,
                        base_ref=pull_request.base_ref,
                    )
                )
            uow.commit()
        return out

    async def _merge_one(self, plan: MergePlan) -> bool:
        """Compare the live head, then immediately merge with the same SHA precondition."""
        assert self._github is not None
        token: InstallationToken | None = None
        try:
            token = await asyncio.to_thread(
                self._github.installation_token,
                installation_id=plan.installation_id or 0,
                repository=plan.repository_name,
            )
            current = await asyncio.to_thread(
                self._github.get_pull_request,
                token,
                repository=plan.repository_name,
                number=plan.number,
            )
            if current.merged and current.head_sha == plan.certified_head_sha:
                recovered = self._merge_result_from_ref(current)
                if recovered is not None:
                    await self._host._db(partial(self._record_merge, plan, recovered))
                    return True
            if current.head_sha != plan.certified_head_sha:
                await self._host._db(
                    lambda: self._record_merge_refusal(
                        plan,
                        f"head moved from certified {plan.certified_head_sha} "
                        f"to {current.head_sha or 'unknown'}",
                        current,
                    )
                )
                return False
            if current.base_ref != plan.base_ref:
                await self._host._db(
                    lambda: self._record_merge_refusal(
                        plan,
                        f"base changed from {plan.base_ref} to {current.base_ref or 'unknown'}",
                        current,
                    )
                )
                return False
            if current.state != "open":
                await self._host._db(
                    lambda: self._record_merge_refusal(
                        plan, f"pull request state is {current.state}", current
                    )
                )
                return False
            if current.mergeable is False or current.mergeable_state == "dirty":
                await self._host._db(
                    lambda: self._record_merge_refusal(
                        plan,
                        f"pull request is not mergeable ({current.mergeable_state or 'unknown'})",
                        current,
                    )
                )
                return False
            # Re-read the switch, accepted head and certification after GitHub I/O.
            # A correction or an administrator may have changed them during the read.
            if not await self._host._db(lambda: self._merge_still_allowed(plan)):
                return False
            try:
                result = await asyncio.to_thread(
                    self._github.merge_pull_request,
                    token,
                    repository=plan.repository_name,
                    number=plan.number,
                    expected_head_sha=plan.certified_head_sha,
                )
            except GitHubError as merge_error:
                if merge_error.response_class == "rate_limited":
                    self._defer_for_rate_limit(merge_error)
                    return False
                try:
                    after = await asyncio.to_thread(
                        self._github.get_pull_request,
                        token,
                        repository=plan.repository_name,
                        number=plan.number,
                    )
                except GitHubError:
                    after = current
                recovered = self._merge_result_from_ref(after)
                if after.head_sha == plan.certified_head_sha and recovered is not None:
                    await self._host._db(lambda: self._record_merge(plan, recovered))
                    return True
                cause = (
                    f"GitHub refused the squash merge: {merge_error.message} ({merge_error.status})"
                )
                await self._host._db(lambda: self._record_merge_refusal(plan, cause, after))
                return False
        except GitHubError as exc:
            if exc.response_class == "rate_limited":
                self._defer_for_rate_limit(exc)
                return False
            cause = f"GitHub could not prepare the squash merge: {exc.message} ({exc.status})"
            await self._host._db(lambda: self._record_merge_refusal(plan, cause, None))
            return False
        finally:
            if token is not None:
                token.discard()
        await self._host._db(lambda: self._record_merge(plan, result))
        return True

    def _merge_still_allowed(self, plan: MergePlan) -> bool:
        with self._host._fenced() as uow:
            if not auto_merge_enabled(uow):
                return False
            task = uow.tasks.get(plan.task_id)
            pull_request = uow.pull_requests.get(plan.pull_request_id)
            if (
                task is None
                or pull_request is None
                or task.state is not TaskState.READY_FOR_MERGE
                or task.head_sha != plan.certified_head_sha
                or pull_request.head_sha != plan.certified_head_sha
                or pull_request.base_ref != plan.base_ref
                or pull_request.state is not PullRequestState.OPEN
                or pull_request.mergeable is False
                or pull_request.mergeable_state == "dirty"
                or not policy_for(uow, task).get("delivery", {}).get("auto_merge", True)
            ):
                return False
            certification = uow.ci_certifications.get_for_head(
                pull_request.id, plan.certified_head_sha
            )
            return certified_jobs_green(certification)

    @staticmethod
    def _merge_result_from_ref(ref: Any) -> MergeResult | None:
        if not ref.merged or ref.merged_at is None or not ref.merged_by or not ref.merge_commit_sha:
            return None
        return MergeResult(
            sha=ref.merge_commit_sha, merged_at=ref.merged_at, merged_by=ref.merged_by
        )

    def _record_merge_refusal(self, plan: MergePlan, cause: str, current: Any | None) -> None:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            pull_request = uow.pull_requests.get(plan.pull_request_id, for_update=True)
            if task is None or pull_request is None or task.state is not TaskState.READY_FOR_MERGE:
                return
            cause = redact(cause)
            previous_cause = pull_request.merge_refusal_cause
            head = current.head_sha if current is not None else pull_request.head_sha
            base = current.base_ref if current is not None else pull_request.base_ref
            mergeable = (
                current.mergeable_state if current is not None else pull_request.mergeable_state
            )
            if current is not None:
                pull_request.observed_head_sha = current.head_sha
                pull_request.observed_base_ref = current.base_ref
                pull_request.mergeable_state = current.mergeable_state
            same_refusal_state = (
                pull_request.merge_refusal_head_sha == head
                and pull_request.merge_refusal_base_ref == base
                and pull_request.merge_refusal_mergeable_state == mergeable
            )
            count = pull_request.merge_refusal_count + 1 if same_refusal_state else 1
            delay = min(60 * (2 ** min(count - 1, 5)), 30 * 60)
            pull_request.merge_refusal_cause = cause
            pull_request.merge_refusal_head_sha = head
            pull_request.merge_refusal_base_ref = base
            pull_request.merge_refusal_mergeable_state = mergeable
            pull_request.merge_refusal_count = count
            pull_request.merge_retry_at = self._clock.now() + timedelta(seconds=delay)
            uow.pull_requests.save(pull_request)
            if cause != previous_cause:
                create_wake(
                    uow,
                    self._clock,
                    principal_id=task.principal_id,
                    reason=WakeReason.READY_FOR_MERGE,
                    summary=f"pull request #{plan.number} was not auto-merged: {cause}",
                    task=task,
                    extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
                )
            uow.commit()

    def _record_merge(self, plan: MergePlan, result: MergeResult) -> None:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            pull_request = uow.pull_requests.get(plan.pull_request_id, for_update=True)
            if task is None or pull_request is None or pull_request.number != plan.number:
                return
            # A response describes a merge that already happened. Record it even if a
            # correction started while the request was in flight, using the same
            # merge-wins settlement as observation (hades #379).
            observe_state(
                uow,
                self._clock,
                task=task,
                pull_request=pull_request,
                observation=Observation(
                    pull_request=PullRequestRef(
                        number=plan.number,
                        url=pull_request.url,
                        head_sha=plan.certified_head_sha,
                        base_ref=plan.base_ref,
                        state="closed",
                        merged=True,
                        merged_at=result.merged_at,
                        merged_by=result.merged_by,
                        merge_commit_sha=result.sha,
                    )
                ),
                result=ObservationResult(),
            )
            settle_pull_request_state(uow, self._clock, task=task, pull_request=pull_request)
            if result.sha:
                # hades #411: main's checks on the merge commit decide the release hold.
                watch_merge(
                    uow,
                    self._clock,
                    task_id=task.id,
                    repository=plan.repository_name,
                    installation_id=plan.installation_id,
                    base_ref=plan.base_ref,
                    merge_sha=result.sha,
                    pull_request=plan.number,
                    merged_at=result.merged_at or self._clock.now(),
                )
            uow.commit()

    def _due_polls(self) -> list[PollPlan]:
        now = self._clock.now()
        out: list[PollPlan] = []
        with self._host._fenced() as uow:
            forced = self._forced_pull_requests(uow)
            for state in sorted(POLLED_STATES, key=lambda s: s.value):
                for task in uow.tasks.list_by_state(state):
                    pull_request = uow.pull_requests.get_for_task(task.id)
                    if pull_request is None or pull_request.state in (
                        PullRequestState.MERGED,
                        PullRequestState.CLOSED,
                    ):
                        continue
                    due, with_reactions = poll_due(
                        pull_request,
                        now=now,
                        poll_interval_seconds=self.config.poll_interval_seconds,
                        reactions_interval_seconds=self.config.reactions_poll_interval_seconds,
                        task_state=task.state,
                    )
                    if pull_request.id in forced or self._decided_since_poll(
                        uow, task.id, pull_request.last_polled_at
                    ):
                        due = True
                        # 23: a review or comment delivery triggers an immediate
                        # reaction poll for its subject, and while the PR awaits
                        # external review the reaction *is* the verdict, so any
                        # delivery about it brings the reaction poll forward.
                        with_reactions = (
                            with_reactions
                            or forced.get(pull_request.id, False)
                            or task.state is TaskState.AWAITING_EXTERNAL_REVIEW
                        )
                    if not due:
                        continue
                    # hades #360: a correction's poll only looks for the merge, so it
                    # fetches neither reactions nor a failed check's log.
                    correcting = correction_in_flight(uow, task)
                    work = latest_work_attempt(uow, task)
                    if work is None:
                        continue
                    repository = uow.repositories.get(task.repository_id)
                    if repository is None:
                        continue
                    out.append(
                        PollPlan(
                            task_id=task.id,
                            pull_request_id=pull_request.id,
                            number=pull_request.number,
                            repository_name=repository_slug(repository),
                            installation_id=repository.installation_id,
                            base_ref=pull_request.base_ref,
                            attempt_id=work[0].id,
                            with_reactions=with_reactions and not correcting,
                            failed_check=(
                                None
                                if correcting
                                else self._failed_check(uow, pull_request.id, task)
                            ),
                        )
                    )
            uow.commit()
        return out

    def _forced_pull_requests(self, uow: UnitOfWork) -> dict[str, bool]:
        """Pull requests a webhook delivery says to look at now, and whether its subject
        needs an immediate reaction poll (23)."""
        forced: dict[str, bool] = {}
        now = self._clock.now()
        for delivery in uow.github_deliveries.list_unprocessed():
            number = 0
            pr_node = delivery.normalized.get("pull_request")
            if isinstance(pr_node, dict):
                number = int(pr_node.get("number", 0))
            elif delivery.normalized.get("is_pull_request"):
                number = int(delivery.normalized.get("issue_number", 0))
            uow.github_deliveries.mark_processed(delivery.delivery_id, now)
            if not number:
                continue
            for state in POLLED_STATES:
                for task in uow.tasks.list_by_state(state):
                    pull_request = uow.pull_requests.get_for_task(task.id)
                    if pull_request is not None and pull_request.number == number:
                        forced[pull_request.id] = forced.get(pull_request.id, False) or bool(
                            delivery.normalized.get("review") or delivery.normalized.get("comment")
                        )
        return forced

    def _failed_check(
        self, uow: UnitOfWork, pull_request_id: str, task: Task
    ) -> tuple[str, str] | None:
        """The failed check whose log is still to be fetched. Once the excerpt is stored
        it is not fetched again on every poll (hades FDY-0139)."""
        certification = uow.ci_certifications.get_for_head(pull_request_id, task.head_sha or "")
        if certification is None or certification.state != "failed":
            return None
        failure = certification.failure
        run_id = str(failure.get("run_id", ""))
        if not run_id or failure.get("log_excerpt") or failure.get("log_fetched"):
            return None
        return str(failure.get("source") or "check_run"), run_id

    @staticmethod
    def _decided_since_poll(uow: UnitOfWork, task_id: str, last_polled: datetime | None) -> bool:
        """A CI decision or a waiver recorded since the last poll brings the next poll
        forward: what it changes is decided on a fresh observation, not on the one taken
        before it (hades FDY-0139)."""
        if last_polled is None:
            return False
        if any(d.created_at > last_polled for d in uow.ci_decisions.list_for_task(task_id)):
            return True
        return any(
            d.kind in WAIVER_KINDS and d.created_at > last_polled
            for d in uow.decisions.list_for_task(task_id)
        )

    async def _observe_one(self, plan: PollPlan) -> bool:
        assert self._github is not None
        token: InstallationToken | None = None
        try:
            token = await asyncio.to_thread(
                self._github.installation_token,
                installation_id=plan.installation_id or 0,
                repository=plan.repository_name,
            )
            observation = await asyncio.to_thread(
                self._github.observe,
                token,
                repository=plan.repository_name,
                number=plan.number,
                base_ref=plan.base_ref,
                with_reactions=plan.with_reactions,
            )
            excerpt = ""
            if plan.failed_check is not None:
                source, external_id = plan.failed_check
                raw = await asyncio.to_thread(
                    self._github.ci_failure_log,
                    token,
                    repository=plan.repository_name,
                    source=source,
                    external_id=external_id,
                    limit_bytes=self.config.ci_log_excerpt_bytes,
                )
                # 12: a workflow log is text the repository controls, and it lands in
                # `ci_certifications.failure`. It is scanned and redacted at the fetch
                # site, before anything can store it.
                excerpt = redact(raw.decode("utf-8", "replace")[-4000:]) if raw else ""
        except GitHubError as exc:
            failure = exc
            if failure.response_class == "rate_limited":
                self._defer_for_rate_limit(failure)
            await self._host._db(lambda: self._record_poll_error(plan, failure))
            return False
        finally:
            if token is not None:
                token.discard()
        fetched = plan.failed_check is not None
        await self._host._db(lambda: self._apply(plan, observation, excerpt, fetched))
        ref = observation.pull_request
        if ref.state == "open" and (ref.mergeable is False or ref.mergeable_state == "dirty"):
            await self._resolve_conflicting_pull_request(plan, ref.head_sha)
        return True

    async def _resolve_conflicting_pull_request(self, plan: PollPlan, head_sha: str) -> None:
        """hades #411: try the mechanical merge first, then hand real conflicts to a
        worker. Only a head Crucible pushed or adopted is acted on: a dirty head that
        arrived out of band has just moved the task to `head_diverged`, and it waits
        there for the head decision with nothing merged into it or launched against it."""
        request = await self._host._db(lambda: self._conflict_request(plan, head_sha))
        if request is None:
            return
        await self._host._db(lambda: self._record_conflict_wake(plan, head_sha))
        if self._publisher is None or self._github is None:
            await self._host._db(
                lambda: self._schedule_merge_main_correction(
                    plan, head_sha, (), "no publisher is configured to merge main"
                )
            )
            return
        token: InstallationToken | None = None
        try:
            token = await asyncio.to_thread(
                self._github.installation_token,
                installation_id=plan.installation_id or 0,
                repository=plan.repository_name,
            )
            outcome = await self._publisher.merge_main(request, token)
        except Exception as exc:
            log.warning("publisher merge-main attempt failed", exc_info=True)
            outcome = MergeMainOutcome(merged=False, step="error", detail=redact(str(exc)))
        finally:
            if token is not None:
                token.discard()
        if outcome.merged and outcome.head_sha:
            await self._host._db(lambda: self._record_merge_main_push(plan, head_sha, outcome))
        else:
            await self._host._db(
                lambda: self._schedule_merge_main_correction(
                    plan, head_sha, outcome.conflicting_files, outcome.detail or outcome.step
                )
            )

    def _conflict_request(self, plan: PollPlan, head_sha: str) -> MergeMainRequest | None:
        """The merge-main request for a conflicting head Crucible can act on, else None.

        The head must be the task's accepted head, on the pull request as observed, and
        one Crucible pushed (or adopted by a head decision), and the task must be in a
        delivery state the merge or its correction may leave. `head_diverged` is not one,
        nor is a correction already under way."""
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id)
            pull_request = uow.pull_requests.get(plan.pull_request_id)
            if (
                task is None
                or pull_request is None
                or task.state not in CONFLICT_STATES
                or pull_request.state is not PullRequestState.OPEN
                or pull_request.head_sha != head_sha
                or task.head_sha != head_sha
                or not self._trusted_head(uow, task, pull_request.id, head_sha)
            ):
                return None
            attempt = uow.attempts.get(plan.attempt_id)
            execution = uow.executions.get(attempt.execution_id) if attempt else None
            repository = uow.repositories.get(task.repository_id)
            if attempt is None or execution is None or repository is None:
                return None
            return MergeMainRequest(
                task_id=task.id,
                attempt_id=attempt.id,
                owner=task.external_id,
                repository_url=push_url_for(repository),
                work_branch=pull_request.work_branch,
                base_ref=pull_request.base_ref,
                expected_head=head_sha,
                image=self.config.publisher_image or attempt.image_digest or execution.image,
                policy=execution.policy_snapshot or {},
                workspace_path=attempt.workspace_path or "",
                timeout_seconds=self.config.publisher_timeout_seconds,
            )

    @staticmethod
    def _trusted_head(uow: UnitOfWork, task: Task, pull_request_id: str, head_sha: str) -> bool:
        if any(
            head.sha == head_sha and head.pushed_by is PushedBy.CRUCIBLE
            for head in uow.pull_request_heads.list_for_pull_request(pull_request_id)
        ):
            return True
        decision = uow.events.latest_for_task_kind(task.id, EventKind.HEAD_DECISION_RECORDED.value)
        return (
            decision is not None
            and decision.payload.get("action") in ADOPT_ACTIONS
            and decision.payload.get("observed_head") == head_sha
        )

    def _record_conflict_wake(self, plan: PollPlan, head_sha: str) -> None:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            pull_request = uow.pull_requests.get(plan.pull_request_id, for_update=True)
            if task is None or pull_request is None or pull_request.head_sha != head_sha:
                return
            pull_request.mergeable = False
            pull_request.mergeable_state = pull_request.mergeable_state or "dirty"
            uow.pull_requests.save(pull_request)
            # One wake per conflicting head: a later poll of the same head is quiet.
            if any(
                event.kind == EventKind.WAKE_CREATED.value
                and event.payload.get("reason") == WakeReason.PULL_REQUEST_CONFLICTING.value
                and f" at {head_sha} " in str(event.payload.get("summary", ""))
                for event in uow.events.list_for_task(task.id, after_seq=0, limit=10_000)
            ):
                uow.commit()
                return
            create_wake(
                uow,
                self._clock,
                principal_id=task.principal_id,
                reason=WakeReason.PULL_REQUEST_CONFLICTING,
                summary=(
                    f"pull request #{pull_request.number} at {head_sha} conflicts with "
                    f"{pull_request.base_ref}. Next: Crucible merges {pull_request.base_ref} "
                    "into the branch itself; if git reports conflicts it launches a "
                    "merge-main correction from the remote branch tip"
                ),
                task=task,
                attempt_id=plan.attempt_id,
                extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
            )
            uow.commit()

    def _record_merge_main_push(
        self, plan: PollPlan, old_head: str, outcome: MergeMainOutcome
    ) -> None:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            pull_request = uow.pull_requests.get(plan.pull_request_id, for_update=True)
            if task is None or pull_request is None or pull_request.head_sha != old_head:
                return
            supersede_for_head(
                uow,
                self._clock,
                task=task,
                reason="merge_main",
                new_head=outcome.head_sha,
            )
            task.head_sha = outcome.head_sha
            pull_request.head_sha = outcome.head_sha
            pull_request.observed_head_sha = outcome.head_sha
            pull_request.mergeable = None
            pull_request.mergeable_state = "unknown"
            uow.tasks.save(task)
            uow.pull_requests.save(pull_request)
            # Crucible's own push: the next poll sees this head as ours, not as a
            # divergence.
            uow.pull_request_heads.add(
                PullRequestHead(
                    id=new_id(),
                    pull_request_id=pull_request.id,
                    sha=outcome.head_sha,
                    pushed_by=PushedBy.CRUCIBLE,
                    observed_at=self._clock.now(),
                )
            )
            record_event(
                uow,
                self._clock,
                EventKind.BRANCH_PUSHED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                attempt_id=plan.attempt_id,
                payload={
                    "head_sha": outcome.head_sha,
                    "previous_head_sha": old_head,
                    "reason": "merge_main",
                    "force_with_lease": old_head,
                },
            )
            # The new head is certified by its own checks. A task still in external
            # review stays there; the others wait for certification of the new head.
            if task.state in (TaskState.READY_FOR_MERGE, TaskState.CI_CERTIFICATION_FAILED):
                move_task(
                    uow,
                    self._clock,
                    task,
                    TaskState.AWAITING_CI_CERTIFICATION,
                    EventKind.TASK_AWAITING_CI_CERTIFICATION,
                    payload={"head_sha": outcome.head_sha, "reason": "merge_main"},
                )
            uow.commit()

    def _schedule_merge_main_correction(
        self,
        plan: PollPlan,
        head_sha: str,
        conflicting_files: tuple[str, ...],
        merge_detail: str = "",
    ) -> None:
        """A `correct` execution under the task's own policy, started from the remote
        branch tip (`resume_from_work_branch`), never from the previous attempt's bundle,
        so the head it publishes fast-forwards the one on the pull request."""
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            pull_request = uow.pull_requests.get(plan.pull_request_id, for_update=True)
            if (
                task is None
                or pull_request is None
                or task.state not in CONFLICT_STATES
                or pull_request.head_sha != head_sha
                or task.head_sha != head_sha
            ):
                return
            attached = uow.events.latest_for_task_kind(
                task.id, EventKind.TASK_CORRECTION_ATTACHED.value
            )
            if (
                attached is not None
                and attached.payload.get("reason") == "merge_main"
                and attached.payload.get("remote_head") == head_sha
            ):
                return
            prior = uow.contracts.get(task.id, task.contract_version)
            if prior is None:
                return
            document = copy.deepcopy(prior.document)
            document["correction"] = {
                "of_version": task.contract_version,
                "reason": "ci_certification",
                "addresses": [],
                "instructions": (
                    f"Merge origin/{pull_request.base_ref}, resolve every conflict keeping "
                    "both behaviours, run every required check, commit the result, and "
                    "report."
                    + (
                        " Conflicting files: " + ", ".join(conflicting_files) + "."
                        if conflicting_files
                        else ""
                    )
                ),
                "resume_from": "remote_branch",
                "request_internal_review": False,
            }
            version = (
                max(
                    (row.version for row in uow.contracts.list_for_task(task.id)),
                    default=0,
                )
                + 1
            )
            stored = TaskContract(
                id=new_id(),
                task_id=task.id,
                version=version,
                document=document,
                sha256=contract_sha256(document),
                submitted_at=self._clock.now(),
            )
            uow.contracts.add(stored)
            task.contract_version = version
            task.head_sha = None
            uow.tasks.save(task)
            record_event(
                uow,
                self._clock,
                EventKind.TASK_CORRECTION_ATTACHED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                payload={
                    "contract_version": version,
                    "of_version": version - 1,
                    "reason": "merge_main",
                    "conflicting_files": list(conflicting_files),
                    "remote_head": head_sha,
                    "merge_main": merge_detail,
                },
            )
            move_task(
                uow,
                self._clock,
                task,
                TaskState.SCHEDULED,
                EventKind.TASK_SCHEDULED,
                payload={
                    "role": "correct",
                    "reason": "merge_main",
                    "contract_version": version,
                    "resume_from_work_branch": True,
                    "conflicting_files": list(conflicting_files),
                },
            )
            uow.commit()

    def _record_rate_limited(self, task_id: str, exc: GitHubError) -> None:
        with self._host._fenced() as uow:
            record_event(
                uow,
                self._clock,
                EventKind.GITHUB_RATE_LIMITED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task_id,
                payload={
                    "step": "publish",
                    "status": exc.status,
                    "retry_after_seconds": exc.retry_after,
                    "note": "deferred to a later tick; the task stays in publishing",
                },
            )
            uow.commit()

    def _record_poll_error(self, plan: PollPlan, exc: GitHubError) -> None:
        with self._host._fenced() as uow:
            record_event(
                uow,
                self._clock,
                EventKind.GITHUB_RATE_LIMITED
                if exc.response_class == "rate_limited"
                else EventKind.PULL_REQUEST_POLLED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=plan.task_id,
                payload={
                    "pull_request": plan.number,
                    "ok": False,
                    "response_class": exc.response_class,
                    "status": exc.status,
                    "retry_after_seconds": exc.retry_after,
                },
            )
            uow.commit()

    def _apply(self, plan: PollPlan, observation: Any, excerpt: str, fetched: bool) -> None:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            pull_request = uow.pull_requests.get(plan.pull_request_id, for_update=True)
            if task is None or pull_request is None:
                return
            apply_observation(
                uow,
                self._clock,
                task=task,
                pull_request=pull_request,
                observation=observation,
                policy=policy_for(uow, task),
                attempt_id=plan.attempt_id,
                with_reactions=plan.with_reactions,
                log_excerpt=excerpt,
                log_fetched=fetched,
            )
            uow.commit()

    def _evaluate_gates(self) -> None:
        """09: post-PR gates re-evaluate each reconcile tick until they resolve.

        This is also what turns a disposition recorded through the API into progress
        without waiting for the next poll. A task whose pull request is already recorded
        merged or closed is moved here, because such a pull request is never polled again
        (hades FDY-0139), and so is a task whose correction was under way when its pull
        request was recorded merged (hades #360)."""
        # hades #379: a correction's tasks are read without a lock, and only one whose
        # pull request is recorded merged is locked, in a transaction of its own that
        # reads it again. Locking every scheduled or running task each tick held rows
        # the supervisor was about to write, for nothing.
        with self._host._fenced() as uow:
            merged = [
                task.id
                for state in sorted(CORRECTION_STATES, key=lambda s: s.value)
                for task in uow.tasks.list_by_state(state)
                if (pull_request := uow.pull_requests.get_for_task(task.id)) is not None
                and pull_request.state is PullRequestState.MERGED
            ]
        for task_id in merged:
            with self._host._fenced() as uow:
                task = uow.tasks.get(task_id, for_update=True)
                if task is None or task.state not in CORRECTION_STATES:
                    continue
                pull_request = uow.pull_requests.get_for_task(task.id, for_update=True)
                if pull_request is None or pull_request.state is not PullRequestState.MERGED:
                    continue
                settle_pull_request_state(uow, self._clock, task=task, pull_request=pull_request)
                uow.commit()
        with self._host._fenced() as uow:
            for state in sorted(OBSERVED_STATES, key=lambda s: s.value):
                for task in uow.tasks.list_by_state(state, for_update=True):
                    pull_request = uow.pull_requests.get_for_task(task.id, for_update=True)
                    if pull_request is None:
                        continue
                    if settle_pull_request_state(
                        uow, self._clock, task=task, pull_request=pull_request
                    ):
                        continue
                    if task.state in (TaskState.READY_FOR_MERGE, TaskState.HEAD_DIVERGED):
                        # Both move only on a fresh observation: a new signal or a red
                        # check for the first, a decision for the second.
                        continue
                    work = latest_work_attempt(uow, task)
                    if work is None:
                        continue
                    policy = policy_for(uow, task)
                    certification = uow.ci_certifications.get_for_head(
                        pull_request.id, task.head_sha or ""
                    )
                    gates = evaluate_delivery_gates(
                        uow,
                        self._clock,
                        task=task,
                        attempt_id=work[0].id,
                        pull_request=pull_request,
                        policy=policy,
                        certification=certification,
                        branch_pushed_sha=task.head_sha,
                    )
                    advance_delivery(
                        uow,
                        self._clock,
                        task=task,
                        pull_request=pull_request,
                        policy=policy,
                        gates=gates,
                        certification=certification,
                        result=ObservationResult(),
                    )
            uow.commit()
