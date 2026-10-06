"""PolicyV1 and RoutingPolicyV1 (05b).

Every tunable the specification mentions lives in a policy document, so nothing is a
magic default in code. Validation here is the intra-document part; the rules that need
a principal (operator-only fields) live in the application layer.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from crucible.contracts.common import StrictModel, check_major_version
from crucible.domain.command_timeout import DEFAULT_COMMAND_TIMEOUT_BOUNDS
from crucible.domain.endpoints import validate_endpoint
from crucible.domain.exit_class import ExitClass
from crucible.domain.gates import (
    ALL_GATES,
    ALWAYS_ADVISORY_GATES,
    ALWAYS_BLOCKING_GATES,
    DEFAULT_ADVISORY_GATES,
    ENFORCED_PRE_PR_GATES,
    POST_PR_GATES,
    PRE_PR_GATES,
    PUBLICATION_GATES,
)

# 05b: these two may only be set true by an operator or admin principal.
OPERATOR_ONLY_FIELDS: tuple[tuple[str, str], ...] = (
    ("ci_certification", "allow_no_ci"),
    ("deliverables", "allow_branch_only"),
    ("release", "require_operator_approval"),
)


class Bounds(StrictModel):
    min: int = Field(ge=1)
    max: int = Field(ge=1)
    default: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Bounds:
        if not self.min <= self.default <= self.max:
            raise ValueError("bounds must satisfy min <= default <= max")
        return self


class AttemptCap(StrictModel):
    max: int = Field(ge=1)
    default: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> AttemptCap:
        if self.default > self.max:
            raise ValueError("max_attempts default exceeds max")
        return self


class Limits(StrictModel):
    timeout_seconds: Bounds
    # Issue 128: the per-command timeout every harness is launched with, in
    # milliseconds. A contract may narrow it within these bounds; the launch never
    # exceeds the attempt's own timeout_seconds. Absent on versions uploaded before it
    # existed, which take the operator's default of 60 minutes.
    command_timeout_ms: Bounds = Field(
        default_factory=lambda: Bounds.model_validate(DEFAULT_COMMAND_TIMEOUT_BOUNDS)
    )
    max_attempts: AttemptCap
    grace_seconds: int = Field(ge=0)
    stall_warn_seconds: int = Field(ge=1)
    stall_fail_seconds: int = Field(ge=1)
    auth_retry_delay_seconds: int = Field(ge=0)
    escalation_stale_hours: int = Field(ge=1)
    wake_retry_hours: int = Field(ge=1)
    publish_retry_max: int = Field(default=3, ge=0)

    @model_validator(mode="after")
    def _stall_order(self) -> Limits:
        if self.stall_warn_seconds > self.stall_fail_seconds:
            raise ValueError("stall_warn_seconds must not exceed stall_fail_seconds")
        return self


class Retry(StrictModel):
    eligible_classes: list[ExitClass]
    auth_failure_max: int = Field(ge=0)

    @field_validator("eligible_classes")
    @classmethod
    def _unique(cls, value: list[ExitClass]) -> list[ExitClass]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate entry in retry.eligible_classes")
        return value


class Concurrency(StrictModel):
    per_provider: int = Field(ge=1)
    per_harness: dict[str, int]

    @field_validator("per_harness")
    @classmethod
    def _positive(cls, value: dict[str, int]) -> dict[str, int]:
        for harness, limit in value.items():
            if limit < 1:
                raise ValueError(f"concurrency.per_harness.{harness} must be at least 1")
        return value


class Resources(StrictModel):
    cpus: int = Field(ge=1)
    memory: str = Field(min_length=1)
    pids: int = Field(ge=1)
    tmpfs_total: str = Field(min_length=1)
    # Kubernetes-only (26, issue 93): the container's CPU request as a fraction of
    # `cpus`, so a small cluster can schedule a Burstable pod instead of demanding the
    # whole limit up front. A fraction rather than an absolute value tracks `cpus`
    # automatically when a task's policy changes it, and never exceeds the limit by
    # construction. Defaults to a value that fits the 3 x 4-CPU lab (issue 93): three
    # concurrent attempts at the default 2-CPU limit request 3 CPU total, not 6.
    cpu_request_fraction: float = Field(default=0.5, gt=0, le=1)
    # Kubernetes-only (26): the memory request as a fraction of `memory`. Defaults to 1
    # (request equals limit), which keeps the existing Guaranteed-for-memory behavior:
    # a worker promised the policy's memory is not the first thing evicted under node
    # pressure (16). A deployment whose cluster is memory-constrained as well as
    # CPU-constrained may lower this the same way.
    memory_request_fraction: float = Field(default=1.0, gt=0, le=1)


class Network(StrictModel):
    mode: Literal["egress-proxy", "none"]
    egress_allowlist: list[str]
    harness_endpoints: str = Field(min_length=1)

    @field_validator("egress_allowlist")
    @classmethod
    def _hostnames(cls, value: list[str]) -> list[str]:
        for host in value:
            if "*" in host or "/" in host or not host.strip():
                raise ValueError(f"egress_allowlist entry {host!r} must be a bare hostname")
        return value


class NamedVersion(StrictModel):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)


class RoutingPolicyRef(NamedVersion):
    # A delivery policy normally follows new versions of the named routing policy.
    # Set this only when an operator deliberately wants this exact version retained.
    # Strict, so a string such as "true" is refused rather than read as unpinned.
    pinned: StrictBool = False


class RoutingRef(StrictModel):
    policy: RoutingPolicyRef


class Images(StrictModel):
    allowlist: list[str] = Field(min_length=1)
    require_default_or_retained: bool


class Git(StrictModel):
    author_name: str = Field(min_length=1)
    author_email: str = Field(min_length=1)
    commit_trailer: str = Field(min_length=1)
    work_branch_pattern: str = Field(min_length=1)
    protected_branches: list[str]


class RepositoryRules(StrictModel):
    required_checks: list[str]
    # hades #184: the programs those checks call beyond the first word of each (`uv`,
    # `gitleaks` behind `make lint` and `make scan`, or `node` and `npm` for JavaScript).
    # A declaration only: Crucible does
    # not read it at run time; `make images-policy-check` proves each resolves in the
    # worker image, so a shipped policy and the image cannot disagree about them.
    required_programs: list[str] = Field(default_factory=list)

    @field_validator("required_programs")
    @classmethod
    def _bare_program_names(cls, value: list[str]) -> list[str]:
        for program in value:
            if not program or program != program.strip() or any(c.isspace() for c in program):
                raise ValueError(f"{program!r} is not a single program name")
        return value


class Gates(StrictModel):
    pre_pr: list[str]
    publication: list[str]
    post_pr: list[str]
    skipped: list[str]
    # ADR 0024: the pre-PR gates whose failure is carried to the reviewer instead of
    # stopping the task; every other pre-PR gate blocks. Absent (a version written before
    # the field existed) means the default set in crucible.domain.gates.
    advisory: list[str] | None = None

    @field_validator("advisory")
    @classmethod
    def _advisory(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(set(value)) != len(value):
            raise ValueError("duplicate entry in gates.advisory")
        fixed = sorted(set(value) & ALWAYS_BLOCKING_GATES)
        if fixed:
            raise ValueError(f"gates.advisory may not include {fixed}: these always block")
        always = sorted(set(value) & ALWAYS_ADVISORY_GATES)
        if always:
            raise ValueError(
                f"gates.advisory may not include {always}: these are always advisory "
                "and are not listed in a policy"
            )
        stray = sorted(set(value) - PRE_PR_GATES)
        if stray:
            raise ValueError(f"gates.advisory names gates that are not pre-PR gates: {stray}")
        return value

    @model_validator(mode="after")
    def _partition(self) -> Gates:
        groups = (self.pre_pr, self.publication, self.post_pr, self.skipped)
        listed = [gate for group in groups for gate in group]
        enforced = sorted(set(listed) & (ENFORCED_PRE_PR_GATES - PRE_PR_GATES))
        if enforced:
            raise ValueError(f"{enforced} always run before review and are not listed in a policy")
        unknown = sorted(set(listed) - ALL_GATES)
        if unknown:
            raise ValueError(f"unknown gates: {unknown}")
        if len(listed) != len(set(listed)):
            raise ValueError("a gate appears in more than one group")
        missing = sorted(ALL_GATES - set(listed))
        if missing:
            raise ValueError(f"gates in no group: {missing}")
        for group_name, group, expected in (
            ("pre_pr", self.pre_pr, PRE_PR_GATES),
            ("publication", self.publication, PUBLICATION_GATES),
            ("post_pr", self.post_pr, POST_PR_GATES),
        ):
            stray = sorted(set(group) - expected)
            if stray:
                raise ValueError(f"gates.{group_name} lists gates from another phase: {stray}")
        return self


class Deliverables(StrictModel):
    allow_branch_only: bool
    on_out_of_band_head: Literal["block"]


class Delivery(StrictModel):
    auto_merge: bool = True


class PullRequestRules(StrictModel):
    require_pre_pr_verification: bool
    open_only_after_pre_pr_gates_pass: bool
    publish_requires_acceptance: bool
    title_from: Literal["claim"]
    body_template: Literal["default"]
    closing_refs: Literal["contract_only"]


class InternalReview(StrictModel):
    required: bool
    required_for_corrections: bool
    reviewer_must_not_be_author: bool
    executor: Literal["orchestrator_or_crucible", "orchestrator", "crucible"]


class ExternalReview(StrictModel):
    provider: str | None = None
    request_on_publish: bool = True
    trigger_comment: str | None = None
    reviewer_logins: list[str]
    required_rounds: int = Field(ge=0)
    retrigger_after_correction: bool
    require_review_on_final_sha: bool
    require_feedback_disposition: bool
    accepted_signals: list[str]
    components: list[str] = Field(default_factory=lambda: ["code"])
    round_counting: str = Field(min_length=1)
    wait_timeout_hours: int = Field(ge=1)

    @model_validator(mode="after")
    def _logins_when_required(self) -> ExternalReview:
        if self.required_rounds > 0 and not self.reviewer_logins:
            raise ValueError("reviewer_logins must be non-empty when required_rounds is above 0")
        if self.trigger_comment is None and self.provider == "codex":
            self.trigger_comment = "@codex review"
        if self.provider and self.request_on_publish and not self.trigger_comment:
            raise ValueError(
                f"external review provider {self.provider!r} requires trigger_comment "
                "when request_on_publish is true"
            )
        return self


class CiCertification(StrictModel):
    require_green_on_final_sha: bool
    required_checks: list[str] = Field(
        default_factory=list,
        description="Optional explicit narrowing of observed runs by name; empty counts all runs.",
    )
    allow_no_ci: bool
    on_failure: Literal["escalate"]
    automatic_retry: bool
    automatic_worker_correction: bool
    wait_timeout_hours: int = Field(ge=1)


class ReleaseRules(StrictModel):
    require_operator_approval: bool
    authorization_recorder: Literal["orchestrator_relay", "operator_token"]
    trigger: Literal["tag"]
    tag_pattern: str = Field(min_length=1)
    version_files: list[str]
    changelog_required: bool


class Cleanup(StrictModel):
    workspace_on_success: Literal["keep_diff_only", "keep", "delete"]
    workspace_on_failure: Literal["keep_diff_only", "keep", "delete"]
    container_remove: Literal["always", "never"]
    credential_volume_remove: str = Field(min_length=1)


class Retention(StrictModel):
    logs_and_transcripts_days: int = Field(ge=1)
    bootstrap_archive_days: int = Field(ge=1)
    completed_workspaces_days: int = Field(ge=1)
    wakes_after_ack_days: int = Field(ge=1)
    indefinite: list[str]


class PolicyV1(StrictModel):
    schema_version: str
    name: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    description: str = Field(min_length=1)
    limits: Limits
    retry: Retry
    concurrency: Concurrency
    resources: Resources
    network: Network
    routing: RoutingRef
    images: Images
    git: Git
    repository: RepositoryRules
    gates: Gates
    deliverables: Deliverables
    delivery: Delivery = Field(default_factory=Delivery)
    pull_request: PullRequestRules
    internal_review: InternalReview
    external_review: ExternalReview
    ci_certification: CiCertification
    release: ReleaseRules
    cleanup: Cleanup
    retention: Retention

    @field_validator("schema_version")
    @classmethod
    def _version_supported(cls, value: str) -> str:
        return check_major_version(value)

    @model_validator(mode="after")
    def _external_rounds_zero_skips_gates(self) -> PolicyV1:
        if self.external_review.required_rounds == 0:
            for gate in ("external_review_rounds", "feedback_dispositions_complete"):
                if gate not in self.gates.skipped:
                    raise ValueError(
                        f"required_rounds is 0, so {gate} must be listed in gates.skipped"
                    )
        return self

    def operator_only_settings(self) -> list[str]:
        """The 05b fields whose current value only an operator or admin may upload."""
        out: list[str] = []
        if self.ci_certification.allow_no_ci:
            out.append("ci_certification.allow_no_ci")
        if self.deliverables.allow_branch_only:
            out.append("deliverables.allow_branch_only")
        if not self.release.require_operator_approval:
            out.append("release.require_operator_approval")
        # ADR 0024: turning a safety gate advisory is the operator's call.
        for gate in sorted(set(self.gates.advisory or ()) - DEFAULT_ADVISORY_GATES):
            out.append(f"gates.advisory.{gate}")
        return out


# ADR 0028: the tiers whose default pool order puts the local pools first. Hermes on the
# local gateway is the default doer; `complex` (scoping and structural work) has no pool
# preference by default, so its capability preference (frontier) decides.
LOCAL_FIRST_TIERS = frozenset({"trivial", "standard"})


class RoutingTier(StrictModel):
    allowed_capability: list[Literal["small", "mid", "frontier"]] = Field(min_length=1)
    prefer: list[Literal["small", "mid", "frontier"]] = Field(min_length=1)
    # ADR 0028: pools in the order routing tries them, ahead of `prefer`. Absent (every
    # version before 0028) reads as the default: `RoutingPolicyV1.preferred_pools`.
    prefer_pools: list[str] | None = None

    @model_validator(mode="after")
    def _prefer_subset(self) -> RoutingTier:
        stray = sorted(set(self.prefer) - set(self.allowed_capability))
        if stray:
            raise ValueError(f"prefer lists capabilities the tier does not allow: {stray}")
        return self


class ChatTemplateKwargs(StrictModel):
    """Request options retained with one routing entry.

    Hermes 0.19 cannot receive this option from its non-interactive CLI. Hades #388: the
    Hermes adapter passes it to the image wrapper, whose bootstrap puts it on each
    request, and the attempt records the value it was launched with.
    """

    enable_thinking: bool = False


class RoutingModel(StrictModel):
    # Full engine window for Qwen Code; absent uses its documented 131072 default.
    context_length: int | None = Field(default=None, gt=0, strict=True)
    # Gateway alias when two harnesses share one model but need distinct routing IDs.
    model_name: str | None = Field(default=None, min_length=1)
    id: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    endpoint: Literal["subscription", "local"]
    endpoint_url: str | None = None
    capability: Literal["small", "mid", "frontier"]
    cost: Literal["none", "low", "medium", "high"]
    speed: Literal["slow", "medium", "fast"]
    pool: str = Field(min_length=1)
    weight: int = Field(ge=0)
    enabled: bool
    disabled_reason: str | None = None
    chat_template_kwargs: ChatTemplateKwargs = Field(default_factory=ChatTemplateKwargs)

    @model_validator(mode="after")
    def _local_needs_endpoint(self) -> RoutingModel:
        if self.endpoint == "local" and not self.endpoint_url and self.enabled:
            raise ValueError(f"local model {self.id!r} must carry endpoint_url")
        if self.endpoint == "local" and not self.endpoint_url and not self.disabled_reason:
            raise ValueError(
                f"disabled local model {self.id!r} without endpoint_url must record why"
            )
        if self.endpoint == "local" and self.endpoint_url:
            validate_endpoint("local", self.endpoint_url)
        if self.endpoint == "subscription" and self.endpoint_url:
            raise ValueError(f"subscription model {self.id!r} must not carry endpoint_url")
        if self.enabled and self.disabled_reason:
            raise ValueError(f"enabled model {self.id!r} must not carry disabled_reason")
        return self


BudgetUnit = Literal["attempts", "tokens_out", "cost_units"]


class RoutingPool(StrictModel):
    window: str = Field(pattern=r"^[0-9]+[hm]$")
    budget_units: BudgetUnit
    soft_limit: int = Field(ge=0)
    # Absent on immutable versions 1 and 2. Only version 3 uses reactive marks.
    default_cooldown_seconds: int = Field(default=3600, ge=1)
    max_concurrency: int | None = Field(default=None, ge=1)


class Rotation(StrictModel):
    strategy: str = Field(min_length=1)
    quality_feedback: bool
    quality_window: int = Field(ge=1, le=1000)
    # ADR 0028: a model is demoted in a project when, over its last `quality_window`
    # attempts that reached the gates, at least `demote_min_sample` did and at least
    # `demote_failure_percent` of them failed a blocking gate. Two failures at least:
    # one failure never demotes. A demoted model gets a probe attempt once its last
    # attempt is `probe_after_minutes` old, so it can recover. Absent reads as these.
    demote_failure_percent: int = Field(default=50, ge=1, le=100)
    demote_min_sample: int = Field(default=5, ge=2)
    probe_after_minutes: int = Field(default=60, ge=1, le=10080)

    @model_validator(mode="after")
    def _sample_fits_window(self) -> Rotation:
        if "demote_min_sample" not in self.model_fields_set:
            # A version written before ADR 0028 has no minimum sample, and its window may be
            # as small as 1. Its default fits that window, so the immutable document still
            # loads; a window of 1 cannot hold two failures, so it never demotes.
            self.demote_min_sample = max(2, min(5, self.quality_window))
            return self
        # A minimum sample the window cannot hold would switch demotion off unseen.
        if self.demote_min_sample > self.quality_window:
            raise ValueError(
                f"demote_min_sample {self.demote_min_sample} exceeds quality_window "
                f"{self.quality_window}"
            )
        return self


class Reroute(StrictModel):
    reroute_max: int = Field(default=3, ge=0)
    resume_max_wait_seconds: int = Field(default=86400, ge=1)


class RoutingPolicyV1(StrictModel):
    schema_version: str
    name: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    tiers: dict[str, RoutingTier]
    models: list[RoutingModel] = Field(min_length=1)
    pools: dict[str, RoutingPool]
    rotation: Rotation
    reroute: Reroute = Field(default_factory=Reroute)

    @field_validator("schema_version")
    @classmethod
    def _version_supported(cls, value: str) -> str:
        return check_major_version(value)

    @model_validator(mode="after")
    def _coherent(self) -> RoutingPolicyV1:
        ids = [m.id for m in self.models]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate model id")
        unknown_pools = sorted({m.pool for m in self.models} - set(self.pools))
        if unknown_pools:
            raise ValueError(f"models reference pools the policy does not define: {unknown_pools}")
        if not self.tiers:
            raise ValueError("at least one tier is required")
        for tier, rule in self.tiers.items():
            stray = sorted(set(rule.prefer_pools or []) - set(self.pools))
            if stray:
                raise ValueError(f"tier {tier} prefers pools the policy does not define: {stray}")
            if len(set(rule.prefer_pools or [])) != len(rule.prefer_pools or []):
                raise ValueError(f"tier {tier} lists a preferred pool twice")
        return self

    def model(self, model_id: str) -> RoutingModel | None:
        return next((m for m in self.models if m.id == model_id), None)

    def local_pools(self) -> list[str]:
        """Pools holding a model on a local endpoint (Hermes on the gateway), by name."""
        return sorted({m.pool for m in self.models if m.endpoint == "local"})

    def preferred_pools(self, tier: str) -> list[str]:
        """ADR 0028: the pool order routing tries first for `tier`. A tier that sets
        `prefer_pools` gets exactly that; one that does not gets the default, the local
        pools for `trivial` and `standard` and no preference otherwise."""
        rule = self.tiers.get(tier)
        if rule is None:
            return []
        if rule.prefer_pools is not None:
            return list(rule.prefer_pools)
        return self.local_pools() if tier in LOCAL_FIRST_TIERS else []


def parse_policy(document: object) -> PolicyV1:
    return PolicyV1.model_validate(document)


def parse_routing_policy(document: object) -> RoutingPolicyV1:
    return RoutingPolicyV1.model_validate(document)


def window_seconds(window: str) -> int:
    """'5h' or '90m' as seconds. The pattern on RoutingPool.window guarantees the shape."""
    value, unit = int(window[:-1]), window[-1]
    return value * (3600 if unit == "h" else 60)


def policy_document(policy: PolicyV1) -> dict[str, Any]:
    return policy.model_dump(mode="json")
