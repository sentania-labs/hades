"""API request and response schemas for /v1 (04). Every response carries schema_version."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from crucible.contracts.common import SCHEMA_VERSION, Rfc3339, StrictModel
from crucible.contracts.task_contract import HarnessName, ProviderName
from crucible.domain.entities import (
    CI_RERUN_CAUSES,
    AcceptanceVerdict,
    CIAction,
    CICause,
    DispositionKind,
    HeadAction,
)
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = SCHEMA_VERSION


class ContractVersionView(Response):
    version: int
    sha256: str
    submitted_at: Rfc3339


class AttemptSummary(Response):
    ordered_candidates: list[dict[str, Any]] = Field(default_factory=list)
    id: str
    execution_id: str
    number: int
    state: AttemptState
    exit_code: int | None
    exit_class: ExitClass | None
    started_at: Rfc3339 | None
    ended_at: Rfc3339 | None
    handle: str | None
    model: str | None = None
    harness: str | None = None
    image: str | None = None
    pool: str | None = None
    routing_version: int | None = None
    # hades #388: context_length, max_output_tokens and thinking, as launched.
    effective_settings: dict[str, Any] | None = None
    # hades #425: the launch wrapper's egress probe, `{"hosts": [{"host", "reachable",
    # "curl_exit", "ms", "detail"}, ...], "recorded_at"}`; None until the worker log
    # carried it, or for an attempt with no allowlisted host.
    egress_probe: dict[str, Any] | None = None
    reroute_from_attempt_id: str | None = None
    resume_from_remote: bool = False


class ExecutionSummary(Response):
    id: str
    role: str
    state: ExecutionState
    harness: str
    model: str
    provider: str
    image: str
    contract_version: int
    max_attempts: int
    created_at: Rfc3339
    ended_at: Rfc3339 | None
    attempts: list[AttemptSummary]


class DeliveryView(Response):
    """The task's paper trail on GitHub (hades FDY-0143): the branch Crucible pushed,
    the head it pushed, the pull request, and, once merged, the merge commit and who
    merged it. Each is null until it happens."""

    work_branch: str | None = None
    pushed_head: str | None = None
    pushed_at: Rfc3339 | None = None
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    pull_request_state: str | None = None
    merge_sha: str | None = None
    merged_by: str | None = None
    merged_at: Rfc3339 | None = None


class TaskView(Response):
    id: str
    external_id: str
    title: str
    project: str
    state: TaskState
    principal: str
    repository: str
    policy: dict[str, Any]
    contract_version: int
    created_at: Rfc3339
    updated_at: Rfc3339
    closed_at: Rfc3339 | None
    contract_versions: list[ContractVersionView]
    contract: dict[str, Any]
    executions: list[ExecutionSummary]
    latest_attempt: AttemptSummary | None
    head_sha: str | None
    gate_summary: dict[str, Any]
    pull_request: dict[str, Any] | None
    delivery: DeliveryView = Field(default_factory=DeliveryView)
    open_escalations: list[dict[str, Any]]
    review_reports: list[dict[str, Any]]
    acceptance_results: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    unacked_wakes: int
    resume_at: Rfc3339 | None = None
    reroute_chain: list[dict[str, Any]] = Field(default_factory=list)
    gate_probes: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TaskListItem(Response):
    id: str
    external_id: str
    title: str
    project: str
    state: TaskState
    repository: str
    contract_version: int
    created_at: Rfc3339
    updated_at: Rfc3339


class TaskList(Response):
    items: list[TaskListItem]
    next_cursor: str | None


class StartOverrides(StrictModel):
    """C1 accepts the shape and refuses any override (amendments are C2)."""

    model: str | None = None
    effort: str | None = None
    image: str | None = None
    timeout_seconds: int | None = None


class StartRequest(StrictModel):
    harness: HarnessName | None = None
    model: str | None = Field(default=None, min_length=1)
    provider: ProviderName | None = None
    image: str | None = Field(default=None, min_length=1)
    policy_version: int = Field(ge=1)
    effort: str | None = None
    overrides: StartOverrides | None = None


class CancelRequest(StrictModel):
    reason: str = Field(min_length=1)
    verbatim: str = Field(min_length=1, description="The deciding principal's own words.")
    decided_by: str = Field(min_length=1)


class EventView(Response):
    seq: int
    ts: Rfc3339
    kind: str
    task_id: str | None
    execution_id: str | None
    attempt_id: str | None
    principal: str
    verified: bool
    payload: dict[str, Any]


class EventList(Response):
    items: list[EventView]
    next_cursor: str | None


class AttemptView(Response):
    id: str
    execution_id: str
    task_id: str
    number: int
    state: AttemptState
    handle: str | None
    workspace_path: str | None
    identity_sha256: str | None
    image_digest: str | None
    started_at: Rfc3339 | None
    ended_at: Rfc3339 | None
    exit_code: int | None
    exit_class: ExitClass | None
    timeout_at: Rfc3339 | None
    termination_reason: str | None
    lease: dict[str, Any] | None
    heartbeat_summary: dict[str, Any]
    report: dict[str, Any] | None
    model: str | None = None
    harness: str | None = None
    image: str | None = None
    pool: str | None = None
    routing_version: int | None = None
    effective_settings: dict[str, Any] | None = None
    egress_probe: dict[str, Any] | None = None
    ordered_candidates: list[dict[str, Any]] = Field(default_factory=list)
    resume_from_remote: bool = False
    # Issue 278: a stall ended early: its shape (loop:wait, loop:empty_command,
    # loop:command, no_activity) and the reason, naming the repeated command.
    stall_shape: str | None = None
    termination_detail: str | None = None
    # hades #393: why a worker stopped with `blocked.md` (missing_capability or
    # ambiguous_contract) and what it said, verbatim. Both None for any other ending.
    blocked_reason: str | None = None
    blocked_statement: str | None = None


class ExecutionView(Response):
    id: str
    task_id: str
    role: str
    state: ExecutionState
    contract_version: int
    harness: str
    model: str
    effort: str | None
    provider: str
    image: str
    max_attempts: int
    retry_on: list[str]
    timeout_seconds: int
    policy_snapshot: dict[str, Any]
    created_at: Rfc3339
    ended_at: Rfc3339 | None
    attempts: list[AttemptSummary]


class HealthView(Response):
    status: Literal["ok"]
    version: str


class ReadyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    detail: str


class ReadyView(Response):
    # hades #190: the database and the migrations decide `ready`; the supervisor's
    # check is reported and does not.
    ready: bool
    database: ReadyCheck
    migrations: ReadyCheck
    supervisor: ReadyCheck


class SupervisorView(Response):
    lease: dict[str, Any] | None
    last_tick_at: Rfc3339 | None
    last_success_at: Rfc3339 | None
    last_error_at: Rfc3339 | None
    last_error: str | None
    healthy: bool
    health_detail: str
    tick_ms: int | None
    counts: dict[str, int]
    providers: list[dict[str, Any]]
    github: dict[str, Any] | None


class HarnessCredentialView(Response):
    """25: sanitized. State, mode, a fingerprint of file names and sizes, timestamps."""

    state: str
    mount_mode: str | None
    source_fingerprint: str | None
    files: list[dict[str, Any]]
    detail: str
    session_compatibility: str
    refresh_requires_rw: bool | None
    mount_mode_observed: str | None
    last_validated_at: Rfc3339 | None
    last_auth_failure_at: Rfc3339 | None
    last_launch_at: Rfc3339 | None
    last_launch_outcome: str | None


class HarnessView(Response):
    """07 and 13: installed and supported versions, the enable flags, the credential."""

    name: str
    enabled: bool
    # The configuration entry is the starting value only (hades #174): once an
    # administrator has enabled or disabled the harness, `decided_by_administrator` is
    # true and `enabled_by_administrator` alone decides `enabled`; until then `enabled`
    # needs both.
    enabled_by_configuration: bool
    enabled_by_administrator: bool
    reason: str
    supported_versions: str
    installed_versions: list[str]
    capabilities: dict[str, Any]
    credential: HarnessCredentialView
    # The harness's own default worker image and the one a rollback returns to (13, ADR
    # 0018): reference, digest, and the version of this harness the image pins.
    default_image: dict[str, str] | None = None
    previous_image: dict[str, str] | None = None
    # The last harness test (crucible#118): pass or fail, and each step in plain words.
    last_test: dict[str, Any] | None = None
    decided_by_administrator: bool = False
    # Why the configuration keeps the harness off (an unverified session), shown as a
    # warning whatever the administrator decided; empty when the configuration has it on.
    warning: str = ""


class HarnessList(Response):
    items: list[HarnessView]


class ImageView(Response):
    """13: a worker image the provider can see. `harnesses` is every harness the image
    carries, name to pinned version. Promotion is per harness (ADR 0018):
    `supported_for` names the harnesses whose version is inside the adapter's tested
    range, which is what promoting the image for that harness requires; `default_for`
    and `previous_for` name the harnesses it is the default, or the rollback image, of.
    `supported` is true when every harness it carries is supported, and
    `promotion_state` summarises the three lists: `default`, `retained`, `candidate`."""

    reference: str
    digest: str
    harnesses: dict[str, str]
    supported: bool
    promotion_state: str
    provider: str
    supported_for: list[str] = Field(default_factory=list)
    default_for: list[str] = Field(default_factory=list)
    previous_for: list[str] = Field(default_factory=list)


class ImageList(Response):
    items: list[ImageView]


class ExternalReviewAttestation(StrictModel):
    """23: GitHub exposes the reviewer's "review all pull requests" setting nowhere, so
    onboarding records the operator's word for it, with who attested and when."""

    attested_all_prs: bool = False
    attested_by: str | None = None
    note: str | None = None


class RepositoryRegistration(StrictModel):
    url: str = Field(min_length=1)
    default_branch: str = Field(min_length=1)
    policy_name: str = Field(min_length=1)
    installation_id: int | None = None
    external_review: ExternalReviewAttestation = Field(default_factory=ExternalReviewAttestation)
    # ADR 0019: clone with a read-only GitHub App installation token. Needs the App
    # connected and `installation_id` naming an installation that can read the
    # repository; registration refuses it otherwise.
    private: bool = False


class RepositoryView(Response):
    id: str
    name: str
    url: str
    default_branch: str
    policy_name: str
    installation_id: int | None
    registered_by: str
    created_at: Rfc3339
    external_review_attested: bool = False
    attested_by: str | None = None
    attested_at: Rfc3339 | None = None
    private: bool = False
    codex_review_refused_at: Rfc3339 | None = None


# ----- C2: review, acceptance, corrections, decisions, wakes, policies, artifacts ----


class ReviewExecutionRequest(StrictModel):
    """Ask Crucible to run a `review` execution on the collected head (04, 11)."""

    harness: HarnessName
    model: str = Field(min_length=1)
    provider: ProviderName
    image: str = Field(min_length=1)
    effort: str | None = None
    timeout_seconds: int = Field(ge=1)
    rationale: str = Field(min_length=1)


class ReviewRequest(StrictModel):
    report: dict[str, Any] | None = None
    execution: ReviewExecutionRequest | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ReviewRequest:
        if (self.report is None) == (self.execution is None):
            raise ValueError("name exactly one of report or execution")
        return self


class AcceptRequest(StrictModel):
    verdict: AcceptanceVerdict
    reasoning: str = Field(min_length=1)
    head_sha: str | None = None


class DecisionRequest(StrictModel):
    kind: str = Field(min_length=1, max_length=48)
    verbatim: str = Field(min_length=2, description="The deciding principal's own words.")
    resolves: str = Field(min_length=2)
    escalation_id: str | None = None
    reschedule: bool = False

    @model_validator(mode="after")
    def _validate_kind(self) -> DecisionRequest:
        """FDY-0509: kind must be in the public closed list accepted by the server.

        Internal closure kinds (task_cancelled, task_closed) are excluded so
        a task owner cannot fabricate lifecycle events.  Internal callers
        (cancel_task, close_task) bypass the model and write to UoW directly.
        (Finding 01M4CFEK8J8BXDEB0NETRX267E)
        """
        from crucible.domain.decisions import ACCEPTED_DECISION_KINDS  # noqa: PLC0415

        if self.kind not in ACCEPTED_DECISION_KINDS:
            raise ValueError(
                f"decision kind {self.kind!r} is not accepted. "
                f"Accepted kinds: {sorted(ACCEPTED_DECISION_KINDS)}"
            )
        return self


class DispositionRequest(StrictModel):
    review_comment_id: str = Field(min_length=1, max_length=64)
    disposition: DispositionKind
    reasoning: str = Field(min_length=1)


class CloseRequest(StrictModel):
    note: str = Field(min_length=1)


# ----- C4: GitHub delivery (04, 23) --------------------------------------


class CIDecisionRequest(StrictModel):
    """Foundry's reading of a CI failure (23). The cause enum is fixed; a rerun is
    recorded and the operator performs it, because re-running needs Actions write."""

    cause: CICause
    action: CIAction
    reasoning: str = Field(min_length=1)

    @model_validator(mode="after")
    def _cause_matches_action(self) -> CIDecisionRequest:
        """hades #356: `correct` needs a cause other than ci_infrastructure and
        flaky_test; `rerun` needs one of those two. `reject` and `cancel` are
        unconstrained."""
        is_rerun_cause = self.cause in CI_RERUN_CAUSES
        rerun_causes = ", ".join(c.value for c in CI_RERUN_CAUSES)
        if self.action is CIAction.CORRECT and is_rerun_cause:
            raise ValueError(f"a correct action needs a cause other than {rerun_causes}")
        if self.action is CIAction.RERUN and not is_rerun_cause:
            raise ValueError(f"a rerun action needs one of: {rerun_causes}")
        return self


class HeadDecisionRequest(StrictModel):
    """What to do about a head Crucible did not push (09, 23)."""

    action: HeadAction
    reasoning: str = Field(min_length=1)


class PublishRetryRequest(StrictModel):
    reason: str = Field(min_length=1)


# hades #424: the operator's answers to a proposed task. Each carries the reason that is
# recorded on its audit event.
class ApproveRequest(StrictModel):
    reason: str = Field(min_length=1)
    note: str | None = Field(
        default=None,
        min_length=1,
        description="Operator direction, appended verbatim to the contract's objective.",
    )


class SendBackRequest(StrictModel):
    reason: str = Field(min_length=1)
    note: str = Field(min_length=1, description="What the orchestrator receives as its wake.")


class RejectProposalRequest(StrictModel):
    reason: str = Field(min_length=1)


class BatchApproveRequest(StrictModel):
    task_ids: list[str] = Field(
        min_length=1,
        max_length=50,
        description="Proposed tasks in the order selected; that order is the queue order.",
    )
    reason: str = Field(min_length=1)


class BatchApprovalView(Response):
    batch_id: str
    tasks: list[TaskView]


class PullRequestHeadView(Response):
    sha: str
    pushed_by: str
    observed_at: Rfc3339


class ExternalReviewView(Response):
    id: str
    reviewer_login: str
    signal: str
    github_id: str
    reviewed_sha: str | None
    sha_inferred: bool
    state: str
    accepted: bool
    received_at: Rfc3339


class ReviewCommentView(Response):
    id: str
    github_id: str
    kind: str
    login: str
    path: str | None
    line: int | None
    body: str
    reviewed_sha: str | None
    created_at: Rfc3339
    updated_at: Rfc3339
    disposition: dict[str, Any] | None = None


class ReviewCycleView(Response):
    id: str
    head_sha: str
    components: list[str]
    completed_components: dict[str, str]
    state: str
    trigger: str
    opened_at: Rfc3339
    completed_at: Rfc3339 | None


class ReactionView(Response):
    subject_kind: str
    subject_github_id: str
    github_id: str
    login: str
    content: str
    observed_at: Rfc3339
    removed_at: Rfc3339 | None


class CICertificationView(Response):
    id: str
    head_sha: str
    state: str
    detail: str
    required_checks: list[Any]
    check_runs: list[Any]
    failure: dict[str, Any]
    evaluated_at: Rfc3339
    change_class: str


class CIDecisionView(Response):
    id: str
    cause: str
    action: str
    reasoning: str
    principal: str
    created_at: Rfc3339


class PullRequestView(Response):
    id: str
    task_id: str
    number: int
    url: str
    state: str
    base_ref: str
    work_branch: str
    head_sha: str
    title: str
    body_sha256: str
    opened_at: Rfc3339
    merged_at: Rfc3339 | None
    merge_sha: str | None
    merged_by: str | None
    closed_at: Rfc3339 | None
    closed_by: str | None
    last_polled_at: Rfc3339 | None
    reactions_observable: bool
    completed_rounds: int
    required_rounds: int
    heads: list[PullRequestHeadView]
    cycles: list[ReviewCycleView]
    external_reviews: list[ExternalReviewView]
    comments: list[ReviewCommentView]
    reactions: list[ReactionView]
    ci_certifications: list[CICertificationView]
    ci_decisions: list[CIDecisionView]
    gates: list[GateResultView]


class WebhookAck(Response):
    delivery_id: str
    accepted: bool
    duplicate: bool
    detail: str


class AmendRequest(StrictModel):
    contract: dict[str, Any]
    reason: str = Field(min_length=1)


class WakeAckRequest(StrictModel):
    note: str = Field(min_length=1, description="What Foundry did about it.")


class WakeView(Response):
    id: str
    principal: str
    reason: str
    task_id: str | None
    summary: str
    payload: dict[str, Any]
    created_at: Rfc3339
    attempts: int
    delivered_at: Rfc3339 | None
    acked_at: Rfc3339 | None
    ack_note: str | None
    next_attempt_at: Rfc3339 | None
    last_error: str | None
    gave_up_at: Rfc3339 | None


class WakeList(Response):
    items: list[WakeView]
    next_cursor: str | None


class GateResultView(Response):
    gate: str
    phase: str
    result: str
    detail: str
    head_sha: str
    evidence_ids: list[int]
    evaluated_at: Rfc3339
    # ADR 0024: `blocking` stops the task on a failure; `advisory` is for the reviewer.
    classification: Literal["blocking", "advisory"] = "blocking"
    findings: list[str] = Field(default_factory=list)


class ReviewerItem(Response):
    """A failed advisory gate or an advisory finding, with its detail (ADR 0024)."""

    gate: str
    detail: str


class GateList(Response):
    attempt_id: str
    head_sha: str | None
    items: list[GateResultView]
    counts: dict[str, int]
    for_reviewer: list[ReviewerItem] = Field(default_factory=list)


class EvidenceView(Response):
    id: int
    attempt_id: str | None
    kind: str
    source: str
    verified: bool
    observed_at: Rfc3339
    payload: dict[str, Any]
    artifact_id: str | None


class EvidenceList(Response):
    items: list[EvidenceView]


class ArtifactView(Response):
    id: str
    attempt_id: str | None
    task_id: str | None
    type: str
    filename: str
    size: int
    sha256: str
    content_type: str
    created_by: str
    created_at: Rfc3339


class ArtifactList(Response):
    items: list[ArtifactView]


class ReviewReportView(Response):
    id: str
    task_id: str
    head_sha: str
    reviewer_kind: str
    reviewer_attempt_id: str | None
    reviewer_principal: str | None
    verdict: str
    findings: int
    document: dict[str, Any]
    created_at: Rfc3339


class AcceptanceView(Response):
    id: str
    head_sha: str
    principal: str
    verdict: str
    reasoning: str
    superseded_at: Rfc3339 | None
    created_at: Rfc3339


class DecisionView(Response):
    id: str
    kind: str
    principal: str
    verbatim: str
    resolves: str
    escalation_id: str | None
    created_at: Rfc3339


class EscalationView(Response):
    id: str
    state: str
    question: str
    attempt_id: str | None
    opened_at: Rfc3339
    closed_at: Rfc3339 | None
    decision_id: str | None
    # hades #393: the reason the worker's `blocked.md` named, when it named one.
    reason: str | None = None


class PolicyView(Response):
    name: str
    version: int
    document: dict[str, Any]
    referenced: bool
    created_at: Rfc3339
    retired_at: Rfc3339 | None


class RoutingPolicyView(Response):
    name: str
    version: int
    document: dict[str, Any]
    created_at: Rfc3339
    retired_at: Rfc3339 | None


class RoutingUsageView(Response):
    routing_policy: dict[str, Any]
    pools: list[dict[str, Any]]


class RoutingHistoryView(Response):
    items: list[dict[str, Any]]


class CompletionClaimView(Response):
    attempt_id: str
    parsed_ok: bool
    parse_errors: list[dict[str, Any]]
    document: dict[str, Any]
    filled_by_crucible: list[str] = []
    differences: list[dict[str, Any]] = []
