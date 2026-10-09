"""Domain entities as plain dataclasses. Repositories return these, never ORM rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState


class Role(StrEnum):
    ORCHESTRATOR = "orchestrator"
    OPERATOR = "operator"
    OBSERVER = "observer"
    ADMIN = "admin"

    @property
    def may_mutate(self) -> bool:
        return self is not Role.OBSERVER


class ExecutionRole(StrEnum):
    IMPLEMENT = "implement"
    CORRECT = "correct"
    REVIEW = "review"


@dataclass(slots=True)
class Principal:
    id: str
    name: str
    role: Role
    created_at: datetime
    disabled_at: datetime | None = None


@dataclass(slots=True)
class UiSession:
    id: str
    principal_id: str
    csrf: str
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime


@dataclass(slots=True)
class Repository:
    id: str
    name: str
    url: str
    default_branch: str
    policy_name: str
    installation_id: int | None
    registered_by: str
    created_at: datetime
    # 23: the operator's attestation that the external reviewer reviews all pull
    # requests here. GitHub exposes the setting nowhere, so a registration without it is
    # accepted only with external_review.required_rounds: 0.
    external_review_attested: bool = False
    attested_by: str | None = None
    attested_at: datetime | None = None
    # ADR 0019: cloned with a read-only GitHub App installation token rather than with no
    # credential. The picker takes it from GitHub; the registration form states it.
    private: bool = False
    # hades #343: when the connector last refused a round here. Set the first time a
    # refusal is observed on any task against this repository; cleared only by an
    # operator. While set, Crucible stops posting the App's trigger comment on this
    # repository and wakes the orchestrator instead.
    codex_review_refused_at: datetime | None = None


@dataclass(slots=True)
class Policy:
    name: str
    version: int
    document: dict[str, Any]
    created_at: datetime
    retired_at: datetime | None = None


@dataclass(slots=True)
class Task:
    id: str
    external_id: str
    principal_id: str
    project: str
    title: str
    state: TaskState
    contract_version: int
    policy_name: str
    policy_version: int
    repository_id: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    # The head SHA the collector produced for the latest implement or correct attempt.
    # Everything after `reported` is bound to it (09).
    head_sha: str | None = None
    resume_at: datetime | None = None
    quota_wait_started_at: datetime | None = None


@dataclass(slots=True)
class TaskContract:
    id: str
    task_id: str
    version: int
    document: dict[str, Any]
    sha256: str
    submitted_at: datetime


@dataclass(slots=True)
class Execution:
    id: str
    task_id: str
    role: ExecutionRole
    contract_version: int
    harness: str
    model: str
    effort: str | None
    provider: str
    image: str
    policy_snapshot: dict[str, Any]
    state: ExecutionState
    max_attempts: int
    retry_on: list[str]
    timeout_seconds: int
    created_at: datetime
    ended_at: datetime | None = None
    # Once any head from this execution is pushed, every later attempt starts there.
    resume_from_remote: bool = False


@dataclass(slots=True)
class Attempt:
    id: str
    execution_id: str
    task_id: str
    number: int
    state: AttemptState
    created_at: datetime
    workspace_path: str | None = None
    handle: str | None = None
    identity_sha256: str | None = None
    image_digest: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    exit_code: int | None = None
    exit_class: ExitClass | None = None
    timeout_at: datetime | None = None
    drain_deadline: datetime | None = None
    killed_at: datetime | None = None
    termination_reason: str | None = None
    # 10: the final log pull after exit sets this, and cleanup never runs before it.
    logs_drained_at: datetime | None = None
    # The resume position of the log stream: the last stored line's timestamp and its
    # sha256. Docker has no byte offsets and `--since` is inclusive (S8).
    log_resume_ts: datetime | None = None
    log_resume_sha256: str | None = None
    # Which line at `log_resume_ts` the stored boundary is, counting from 0. Lines can
    # repeat inside one instant, so the hash alone does not identify the position.
    log_resume_occurrence: int = 0
    cleaned_up_at: datetime | None = None
    # 15: an attempt imported from the bootstrap ledger stands for a worker Crucible never
    # ran and cannot observe. The supervisor's scans skip it; the task view shows it.
    unsupervised: bool = False
    selected_model: str | None = None
    selected_harness: str | None = None
    selected_image: str | None = None
    selected_pool: str | None = None
    ordered_candidates: list[dict[str, Any]] = field(default_factory=list)
    routing_excluded_pools: list[str] = field(default_factory=list)
    resume_from_remote: bool = False
    # hades #254: the routing policy version this attempt was routed with.
    routing_version: int | None = None
    # hades #388: the context length, response allowance and thinking setting the
    # harness was launched with, recorded once and reused by every later spec of it.
    effective_settings: dict[str, Any] | None = None
    # hades #425: what the launch wrapper's egress probe found before the harness
    # started, per allowlisted host (`crucible.domain.egress_probe`), read off the
    # worker log once; None until the line arrives, or for an attempt with no network.
    egress_probe: dict[str, Any] | None = None
    # Issue 278: the shape of a stall Crucible ended before the time-based limit
    # (exit_class STALL_SHAPES: loop:wait, loop:empty_command, loop:command, no_activity)
    # and the reason in words, naming the repeated command. None for any other ending.
    stall_shape: str | None = None
    termination_detail: str | None = None
    # hades #393: what a worker that stopped with `blocked.md` said. The reason is one
    # of BLOCKED_REASONS (missing_capability, ambiguous_contract) or None when the file
    # named none; the statement is the rest of the file, verbatim (redacted only when it
    # matches a secret pattern). Both None for any other ending.
    blocked_reason: str | None = None
    blocked_statement: str | None = None


@dataclass(slots=True)
class LogChunkRecord:
    """One appended run of log lines for an attempt (10)."""

    id: int | None
    attempt_id: str
    stream: str
    offset_start: int
    offset_end: int
    ts: datetime
    line_sha256: str
    content: bytes
    occurrence: int = 0
    gzipped: bool = False


@dataclass(slots=True)
class Heartbeat:
    """One observed worker liveness or activity signal (10)."""

    id: int | None
    attempt_id: str
    ts: datetime
    signal: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RetentionAction:
    """One deletion retention performed, naming the policy version that authorized it
    (16). Deterministic, idempotent, and an event of its own."""

    id: str
    kind: str
    subject: str
    policy_name: str
    policy_version: int
    acted_at: datetime
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Event:
    seq: int | None
    ts: datetime
    kind: str
    principal: str
    verified: bool
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    execution_id: str | None = None
    attempt_id: str | None = None


@dataclass(slots=True)
class Lease:
    id: str
    kind: str
    key: str
    holder: str
    fenced_token: int
    expires_at: datetime


@dataclass(slots=True)
class CompletionClaimRecord:
    attempt_id: str
    document: dict[str, Any]
    parsed_ok: bool
    parse_errors: list[dict[str, Any]]


@dataclass(slots=True)
class SupervisorStatus:
    holder: str | None
    last_tick_at: datetime | None
    tick_ms: int | None
    counts: dict[str, int]
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    # 339: the seq of the last CREDENTIAL_REFRESH_REQUESTED event the renewer has
    # handled, persisted so a supervisor restart does not replay history as pending.
    refresh_request_cursor: int | None = None


class EscalationState(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"


class AcceptanceVerdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_MORE_WORK = "needs_more_work"


class DispositionKind(StrEnum):
    FIX = "fix"
    DECLINE = "decline"
    OUT_OF_SCOPE = "out_of_scope"
    ALREADY_ADDRESSED = "already_addressed"
    QUESTION = "question"


@dataclass(slots=True)
class Artifact:
    id: str
    attempt_id: str | None
    task_id: str | None
    type: str
    # The name the producer gave it, which is what a contract's required_verification
    # names. `path` is where the bytes landed, which is the content digest (14).
    filename: str
    path: str
    size: int
    sha256: str
    content_type: str
    created_at: datetime
    created_by: str = "crucible"


@dataclass(slots=True)
class EvidenceRecord:
    id: int | None
    attempt_id: str | None
    task_id: str | None
    kind: str
    observed_at: datetime
    source: str
    verified: bool
    payload: dict[str, Any] = field(default_factory=dict)
    artifact_id: str | None = None
    pull_request_id: str | None = None


@dataclass(slots=True)
class ReviewReportRecord:
    id: str
    task_id: str
    head_sha: str
    reviewer_kind: str
    reviewer_attempt_id: str | None
    reviewer_principal_id: str | None
    document: dict[str, Any]
    created_at: datetime
    artifact_id: str | None = None
    superseded_at: datetime | None = None


@dataclass(slots=True)
class GateResultRecord:
    id: str
    task_id: str
    attempt_id: str
    head_sha: str | None
    gate: str
    phase: str
    result: str
    detail: str
    evidence_ids: list[int]
    evaluated_at: datetime
    # ADR 0024: whether a failure here stops the task, and findings for the reviewer.
    blocking: bool = True
    findings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AcceptanceResult:
    id: str
    task_id: str
    head_sha: str
    principal_id: str
    verdict: AcceptanceVerdict
    reasoning: str
    created_at: datetime
    superseded_at: datetime | None = None


@dataclass(slots=True)
class Decision:
    id: str
    task_id: str | None
    escalation_id: str | None
    principal_id: str
    kind: str
    verbatim: str
    resolves: str
    created_at: datetime


@dataclass(slots=True)
class MemoryItem:
    """One remembered fact in the store every Hades channel reads (hades #208).

    `text` is the fact in words, `source` says where it came from (an operator, a
    channel, a minion's finding), `observed_at` is when it was true, and `scope_tags`
    are the hints a recall matches against (a project, a channel, a persona). A minion's
    finding becomes an item only when Hades or the operator promotes it, which is what
    `promoted_by` and `promoted_at` record. An item is never edited in place: a change
    is a new item that supersedes it (`superseded_by`), and a forget retires it with no
    replacement, so `superseded_at` alone says it is no longer current."""

    id: str
    text: str
    source: str
    observed_at: datetime
    scope_tags: list[str]
    promoted_by: str
    promoted_at: datetime
    superseded_by: str | None = None
    superseded_at: datetime | None = None

    @property
    def current(self) -> bool:
        return self.superseded_at is None


@dataclass(slots=True)
class LedgerDecision:
    """One line of the append-only decision ledger shared by every channel (hades #208):
    whose words (`principal`), where they were said (`channel`), when, the words
    themselves (`verbatim`), where in that channel's transcript they sit
    (`transcript_ref`), what they apply to (`applies_to`, task ids or other names), and
    who acted on them and when. A line is never edited or deleted."""

    id: str
    principal: str
    channel: str
    said_at: datetime
    verbatim: str
    transcript_ref: str | None
    applies_to: list[str]
    acted_by: str | None = None
    acted_at: datetime | None = None


@dataclass(slots=True)
class TaskNote:
    """An operator's note on a task (hades #489): who wrote it, when, and the words as
    typed. `verbatim` says the text is the operator's own words rather than a summary
    Hades wrote; a note an action applies is the verbatim of that decision in the audit."""

    id: str
    task_id: str
    principal_id: str
    author: str
    text: str
    created_at: datetime
    verbatim: bool = True


@dataclass(slots=True)
class Escalation:
    id: str
    task_id: str
    attempt_id: str | None
    state: EscalationState
    question: str
    opened_at: datetime
    closed_at: datetime | None = None
    decision_id: str | None = None
    last_wake_at: datetime | None = None
    # hades #393: the reason the worker's `blocked.md` named (missing_capability or
    # ambiguous_contract), None when it named none or the escalation is not a worker's.
    reason: str | None = None


@dataclass(slots=True)
class Wake:
    id: str
    principal_id: str
    task_id: str | None
    reason: str
    payload: dict[str, Any]
    created_at: datetime
    attempts: int = 0
    delivered_at: datetime | None = None
    acked_at: datetime | None = None
    ack_note: str | None = None
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    gave_up_at: datetime | None = None


@dataclass(slots=True)
class ReviewDisposition:
    id: str
    review_comment_id: str
    comment_body_sha256: str
    principal_id: str
    disposition: DispositionKind
    reasoning: str
    created_at: datetime


@dataclass(slots=True)
class AttemptMetrics:
    attempt_id: str
    task_id: str
    model: str
    harness: str
    endpoint_kind: str
    pool: str
    wall_ms: int | None = None
    harness_duration_ms: int | None = None
    tool_calls: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_units: float | None = None
    cost_source: str = "none"
    # The model the harness's own transcript named, when it did (05b, C5).
    model_reported: str | None = None
    exit_class: str | None = None
    gates_passed: int = 0
    gates_failed: int = 0
    corrections_after: int = 0
    acceptance_verdict: str | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class RoutingPolicyRecord:
    name: str
    version: int
    document: dict[str, Any]
    created_at: datetime
    retired_at: datetime | None = None


@dataclass(slots=True)
class PoolExhaustion:
    pool: str
    exhausted_at: datetime
    reset_at: datetime
    task_id: str
    attempt_id: str
    reason: str
    cleared_at: datetime | None = None
    cleared_by: str | None = None
    clear_reason: str | None = None


# ----- harness administration (07, 25) --------------------------------------


@dataclass(slots=True)
class HarnessState:
    """The runtime record of one harness (25): the admin's enable flag with its reason,
    the session-compatibility verdict, and what the last runs observed about the
    credential. Never a value: booleans, enumerations, timestamps, and text reasons."""

    name: str
    enabled: bool
    reason: str
    session_compatibility: str
    updated_at: datetime
    updated_by: str
    mount_mode_observed: str | None = None
    refresh_requires_rw: bool | None = None
    last_launch_at: datetime | None = None
    last_launch_outcome: str | None = None
    last_auth_failure_at: datetime | None = None
    last_validated_at: datetime | None = None
    # hades #389: the most recent successful launch time, for credential-state
    # recovery (last_launch_at moves on failures too, so we track successes).
    last_successful_launch_at: datetime | None = None
    # The last harness test (crucible#118): when, pass or fail, and each step's result in
    # plain words. Never output: the steps say what was checked and why it failed.
    last_test: dict[str, Any] | None = None
    # hades #174: whether an administrator has enabled or disabled the harness through
    # the service. Until one has, the configuration entry is the starting value and a
    # launch needs it and `enabled` both; once one has, `enabled` alone decides.
    enabled_decided: bool = False


@dataclass(slots=True)
class BootstrapImport:
    """One imported bootstrap bundle (15): `verified` once its records are written,
    `authoritative` once committed. `manifest` is the verification report of step 4."""

    id: str
    state: str
    schema_version: str
    content_sha256: str
    source_sha256: str
    source: dict[str, Any]
    manifest: dict[str, Any]
    principal_id: str
    imported_by: str
    verified_at: datetime
    committed_at: datetime | None = None
    committed_by: str | None = None


@dataclass(slots=True)
class GitHubManifestState:
    """One start of the GitHub App manifest flow (crucible#168, ADR 0017 as amended).

    The `state` value GitHub carries back is never stored, only its sha256; it is good
    once, for the principal who started the flow, in the browser that started it (the
    sha256 of a nonce kept in that browser's cookie), until `expires_at`."""

    state_hash: str
    browser_hash: str
    principal: str
    app_name: str
    organization: str | None
    external_url: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass(slots=True)
class ProviderSetting:
    """One provider setting an administrator edits at runtime, as a document (25).

    The settings file seeds it; a saved row wins over the file from then on, in every
    process, because each provider reads it back rather than holding its own copy.
    `kubernetes.egress` is the one such setting (crucible#91)."""

    name: str
    document: dict[str, Any]
    updated_at: datetime
    updated_by: str
    reason: str = ""


@dataclass(slots=True)
class HarnessImage:
    """One harness's default worker image (13, ADR 0018): what its launches, probes,
    tests and logins run, and the one it replaced, which a rollback returns to.

    Promotion is per harness, the operator's decision of 2026-09-25 (crucible#116). A
    worker image may carry several harnesses; each harness's default is chosen, and
    rolled back, on its own. `version` is the version of this harness the image pins."""

    harness: str
    digest: str
    reference: str
    version: str
    updated_at: datetime
    updated_by: str
    reason: str = ""
    previous_digest: str | None = None
    previous_reference: str | None = None
    previous_version: str | None = None


# ----- GitHub delivery (23) ---------------------------------------------


class PullRequestState(StrEnum):
    OPENING = "opening"
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class PushedBy(StrEnum):
    CRUCIBLE = "crucible"
    OTHER = "other"


class CertificationStateValue(StrEnum):
    PENDING = "pending"
    GREEN = "green"
    FAILED = "failed"
    SKIPPED = "skipped"


class CICause(StrEnum):
    """23: the diagnosis Foundry records for a CI certification failure.

    `IMPLEMENTATION_DEFECT` and `MISSING_WORKER_TOOLING` (hades #356) distinguish a
    worker's own mistake from the image or harness lacking something the worker needed;
    `CI_INFRASTRUCTURE` is the third of the operator's three categories, and the rest
    are finer causes. `ci_decision.action` only allows `correct` for a cause other than
    `CI_INFRASTRUCTURE` and `FLAKY_TEST`, and only allows `rerun` for one of those two.
    """

    FALSE_PRE_PR_EVIDENCE = "false_pre_pr_evidence"
    WRONG_SHA_CHECKED = "wrong_sha_checked"
    CORRECTION_WITHOUT_CHECKS = "correction_without_checks"
    ENVIRONMENT_DRIFT = "environment_drift"
    FLAKY_TEST = "flaky_test"
    CRUCIBLE_VERIFICATION_DEFECT = "crucible_verification_defect"
    IMPLEMENTATION_DEFECT = "implementation_defect"
    MISSING_WORKER_TOOLING = "missing_worker_tooling"
    CI_INFRASTRUCTURE = "ci_infrastructure"
    OTHER = "other"


# A `correct` action needs a cause other than ci_infrastructure and flaky_test; a
# `rerun` needs one of those two (hades #356).
CI_RERUN_CAUSES = frozenset({CICause.CI_INFRASTRUCTURE, CICause.FLAKY_TEST})


class CIAction(StrEnum):
    RERUN = "rerun"
    CORRECT = "correct"
    REJECT = "reject"
    CANCEL = "cancel"


class HeadAction(StrEnum):
    ADOPT = "adopt"
    RECOLLECT = "recollect"
    REJECT = "reject"
    CANCEL = "cancel"


@dataclass(slots=True)
class PullRequest:
    id: str
    task_id: str
    repository_id: str
    number: int
    url: str
    base_ref: str
    work_branch: str
    state: PullRequestState
    head_sha: str
    opened_at: datetime
    body_sha256: str = ""
    title: str = ""
    merged_at: datetime | None = None
    merge_sha: str | None = None
    merged_by: str | None = None
    closed_at: datetime | None = None
    closed_by: str | None = None
    last_polled_at: datetime | None = None
    last_reactions_polled_at: datetime | None = None
    reactions_observable: bool = True
    schema_tables: list[str] | None = None
    schema_columns: list[str] | None = None
    schema_models: list[str] | None = None
    cancelled_at: datetime | None = None
    observed_head_sha: str = ""
    observed_base_ref: str = ""
    mergeable_state: str = ""
    mergeable: bool | None = None
    merge_refusal_cause: str | None = None
    merge_refusal_head_sha: str | None = None
    merge_refusal_base_ref: str | None = None
    merge_refusal_mergeable_state: str | None = None
    merge_refusal_count: int = 0
    merge_retry_at: datetime | None = None


@dataclass(slots=True)
class PullRequestHead:
    id: str
    pull_request_id: str
    sha: str
    pushed_by: PushedBy
    observed_at: datetime


@dataclass(slots=True)
class ExternalReviewCycle:
    """One configured review cycle on one published head (23)."""

    id: str
    pull_request_id: str
    head_sha: str
    components: list[str]
    completed_components: dict[str, str]
    state: str
    opened_at: datetime
    completed_at: datetime | None = None
    trigger: str = "publication"


@dataclass(slots=True)
class ExternalReview:
    id: str
    pull_request_id: str
    cycle_id: str | None
    reviewer_login: str
    signal: str
    github_id: str
    reviewed_sha: str | None
    body: str
    body_sha256: str
    received_at: datetime
    state: str = ""
    accepted: bool = False
    sha_inferred: bool = False


@dataclass(slots=True)
class ReviewComment:
    id: str
    pull_request_id: str
    external_review_id: str | None
    github_id: str
    kind: str
    login: str
    path: str | None
    line: int | None
    body: str
    body_sha256: str
    created_at: datetime
    updated_at: datetime
    reviewed_sha: str | None = None


@dataclass(slots=True)
class Reaction:
    id: str
    pull_request_id: str
    subject_kind: str
    subject_github_id: str
    github_id: str
    login: str
    content: str
    observed_at: datetime
    created_at: datetime | None = None
    removed_at: datetime | None = None


@dataclass(slots=True)
class CICertification:
    id: str
    pull_request_id: str
    task_id: str
    head_sha: str
    state: str
    required_checks: list[Any]
    check_runs: list[Any]
    failure: dict[str, Any]
    detail: str
    evaluated_at: datetime
    # hades #476: the class `crucible.domain.change_class.classify` assigned this head's
    # changed paths, carried from `Certification.change_class` (domain/certification.py)
    # into storage. Empty for a certification computed before #476 or when the attempt's
    # diff was never collected.
    change_class: str = ""


@dataclass(slots=True)
class CIDecision:
    id: str
    task_id: str
    ci_certification_id: str | None
    principal_id: str
    cause: str
    action: str
    reasoning: str
    created_at: datetime


@dataclass(slots=True)
class GitHubDelivery:
    delivery_id: str
    event: str
    action: str
    repository: str
    body_sha256: str
    normalized: dict[str, Any]
    received_at: datetime
    processed_at: datetime | None = None
