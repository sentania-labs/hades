"""WakeV1 (17): the notification that Foundry's judgment is required.

Rows first, delivery second. `GET /v1/wakes` is the durable fallback that Foundry polls
on every start of session, so a webhook failure only delays."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from crucible.contracts.common import SCHEMA_VERSION, Rfc3339, StrictModel, check_major_version


class WakeReason(StrEnum):
    INTERNAL_REVIEW_NEEDED = "internal_review_needed"
    GATES_PASSED = "gates_passed"
    PRE_PR_GATES_FAILED = "pre_pr_gates_failed"
    BLOCKED = "blocked"
    ATTEMPT_FAILED = "attempt_failed"
    TIMED_OUT = "timed_out"
    LOST = "lost"
    STALL_WARNING = "stall_warning"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AWAITING_QUOTA = "awaiting_quota"
    AUTH_FAILURE = "auth_failure"
    ESCALATION_STALE = "escalation_stale"
    SUPERVISOR_TAKEOVER = "supervisor_takeover"
    NEEDS_MORE_WORK = "needs_more_work"
    # 23 and 17: the delivery half.
    PUBLISH_FAILED = "publish_failed"
    ACCEPTED = "accepted"
    PUBLISHED = "published"
    EXTERNAL_FEEDBACK_RECEIVED = "external_feedback_received"
    EXTERNAL_REVIEW_OVERDUE = "external_review_overdue"
    EXTERNAL_REVIEW_TRIGGER_NEEDED = "external_review_trigger_needed"
    CI_CERTIFICATION_FAILED = "ci_certification_failed"
    CI_CERTIFICATION_OVERDUE = "ci_certification_overdue"
    CI_RERUN_NEEDED = "ci_rerun_needed"
    HEAD_DIVERGED = "head_diverged"
    PULL_REQUEST_CONFLICTING = "pull_request_conflicting"
    READY_FOR_MERGE = "ready_for_merge"
    MERGED = "merged"
    # hades FDY-0139: a pull request closed without merge rejects the task, and Foundry
    # hears about it rather than finding the task terminal on its next read.
    PULL_REQUEST_CLOSED = "pull_request_closed"
    # hades #379: a publication to the task's own pull request found another pull request
    # open on the work branch; it is not adopted, and Foundry decides what becomes of it.
    OTHER_PULL_REQUEST_OPEN = "other_pull_request_open"
    # hades #379: a quota checkpoint reached the branch of a task that had already
    # finished (merged, rejected, cancelled or closed) while it was pushed.
    CHECKPOINT_AFTER_FINISH = "checkpoint_after_finish"
    # 07 and 25: a launch refused because the harness is unknown, disabled, or its image
    # carries a version outside the adapter's tested range.
    HARNESS_UNAVAILABLE = "harness_unavailable"
    # hades #424: the operator sent a proposed task back with a note, or rejected it.
    SENT_BACK = "sent_back"
    PROPOSAL_REJECTED = "proposal_rejected"


class WakeTask(StrictModel):
    id: str
    external_id: str
    state: str


class WakeV1(StrictModel):
    id: str
    schema_version: str = SCHEMA_VERSION
    principal: str
    reason: WakeReason
    task: WakeTask | None = None
    attempt_id: str | None = None
    pull_request: dict[str, Any] | None = None
    summary: str
    # ADR 0024: failed advisory gates and advisory findings, each with its detail, that
    # the reviewer is asked to weigh. Empty when there are none.
    for_reviewer: list[dict[str, str]] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)
    created_at: Rfc3339

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return check_major_version(value)
