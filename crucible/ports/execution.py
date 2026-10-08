"""ExecutionProvider contract (08). Same shape for fake, docker, and kubernetes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from crucible.domain.endpoints import validate_endpoint
from crucible.domain.gates import SHIM_IDENTITY_MOUNT
from crucible.domain.infrastructure import Interruption
from crucible.domain.secrets import SecretMatch
from crucible.ports.github import InstallationToken

# Where the workspace appears inside every Crucible-created container (06, 08).
REPO_MOUNT = "/crucible/repo"
# The preparer's shim text names it, and `no_injected_files` knows that text (#369).
IDENTITY_MOUNT = SHIM_IDENTITY_MOUNT
REPORT_MOUNT = "/crucible/report"
OUTPUT_MOUNT = "/crucible/out"
# The whole workspace, mounted only into the preparer so git itself creates the
# checkout directory and owns it (S9 Test E: the uid the daemon gives a container is
# not the uid a bind source on the host already has).
WORK_MOUNT = "/crucible/work"
VERIFY_MOUNT = "/crucible/verify"
# FDY-0140: the package caches (uv, pip, npm) live on the workspace, a leaf of its own
# beside the checkout, not on the worker's memory-backed home, whose size limit a
# single `uv sync` can fill. The verifier gets a leaf of its own at the same path: uv
# and pip reuse what is in their cache without checking it again, so a cache the
# worker wrote could change what the verifier's checks run.
PACKAGE_CACHE_LEAF = "pkg-cache"
VERIFIER_CACHE_LEAF = "pkg-cache-verifier"
PACKAGE_CACHE_MOUNT = "/crucible/pkg-cache"
PACKAGE_CACHE_ENV = {
    "UV_CACHE_DIR": f"{PACKAGE_CACHE_MOUNT}/uv",
    "PIP_CACHE_DIR": f"{PACKAGE_CACHE_MOUNT}/pip",
    "npm_config_cache": f"{PACKAGE_CACHE_MOUNT}/npm",
}


class IsolationLevel(StrEnum):
    NONE = "none"
    PROCESS = "process"
    CONTAINER = "container"
    POD = "pod"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    isolation: IsolationLevel
    network_control: bool
    resource_limits: bool
    shared_disk: bool
    supports_harnesses: frozenset[str]
    max_concurrency: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "isolation": self.isolation.value,
            "network_control": self.network_control,
            "resource_limits": self.resource_limits,
            "shared_disk": self.shared_disk,
            "supports_harnesses": sorted(self.supports_harnesses),
            "max_concurrency": self.max_concurrency,
        }


@dataclass(frozen=True, slots=True)
class WorkerCapacity:
    """How many worker Pods a provider admits at once, and why (hades #423).

    `workers` is the dispatch limit the supervisor holds launches to. `headroom` is what
    the provider's own ceiling (a namespace ResourceQuota) admits with nothing held
    back, and `reserved_pods` and `reservation` the short-role Pods Hades runs beside
    workers (gate probe, collector, canary, login, preparer) and the shape kept free
    for them, so a probe always fits while workers are at capacity. `source` says
    where the number came from: the quota, or a configured fallback when there is
    none."""

    workers: int
    source: str
    headroom: int | None = None
    reserved_pods: int = 0
    reservation: Mapping[str, Any] = field(default_factory=dict)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_capacity": self.workers,
            "capacity_source": self.source,
            "quota_headroom": self.headroom,
            "short_role_pods_reserved": self.reserved_pods,
            "short_role_reservation": dict(self.reservation),
            "capacity_detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """What a provider runs. Env carries names only, never secret values (07)."""

    attempt_id: str
    task_id: str
    external_id: str
    role: str
    harness: str
    model: str
    image: str
    timeout_seconds: int
    contract: dict[str, Any]
    env: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    network: Literal["policy", "none"] = "policy"
    # The policy snapshot the execution was created with (05b): resources, network
    # allowlist, git author identity, image allowlist, cleanup.
    policy: dict[str, Any] = field(default_factory=dict)
    # Who the work is for, as the `crucible.owner` container label (08).
    owner: str = "crucible"
    # The registered repository's clone url. It lives on the Repository row, not in
    # the contract, so the supervisor puts it here for `prepare` (03, 08).
    repository_url: str = ""
    # The harness adapter's launch shape (07). `env_from_files` maps a variable name to
    # a container path the provider resolves at container start: the one way a value
    # from a credential file reaches the harness environment, never through `Env`.
    env_from_files: dict[str, str] = field(default_factory=dict)
    # What the harness reads on stdin: these files, in order, then this text.
    stdin_files: tuple[str, ...] = ()
    stdin_text: str = ""
    # Where the provider tees the harness's stdout, so the transcript is an artifact.
    transcript_path: str | None = None
    effort: str | None = None
    endpoint: Literal["subscription", "local"] = "subscription"
    endpoint_url: str | None = None
    # Issue 128: the per-command timeout the harness is launched with, resolved from the
    # policy and the contract and capped at timeout_seconds. None: the default, capped.
    command_timeout_ms: int | None = None
    # FDY-0140: the harness's saved run settings (`harness.<name>`), handed to the
    # adapter's launch as LaunchContext.harness_settings.
    harness_settings: dict[str, Any] = field(default_factory=dict)
    # Hades #388: the context length, response allowance and thinking setting this
    # attempt runs with (also in harness_settings), for the supervisor to record on the
    # attempt. None when the harness reads none of them.
    effective_settings: dict[str, Any] | None = None
    # The resolved runtime credential mode captured for this launch. Running attempts
    # retain it even if an administrator changes the setting later.
    credential_mode: str | None = None
    # An unpublished correction starts from the preceding attempt's sealed bundle.
    # The provider mounts this one file into the preparer; the worker never sees the
    # preceding workspace.
    resume_bundle_path: str | None = None
    resume_bundle_attempt_id: str | None = None
    resume_bundle_head: str | None = None
    resume_bundle_sha256: str | None = None
    resume_bundle_ancestor: str | None = None

    def __post_init__(self) -> None:
        validate_endpoint(self.endpoint, self.endpoint_url)


@dataclass(frozen=True, slots=True)
class Workspace:
    attempt_id: str
    checkout_path: str
    identity_path: str
    report_path: str
    checkout_lease_id: str | None = None
    # What the collector writes into, and the hash of the rendered identity bundle (06).
    output_path: str | None = None
    identity_sha256: str | None = None
    # Which branch the checkout started from, and whether it came from the remote
    # work_branch head (a correction or a retry of published work) or from base_ref (08).
    work_branch: str | None = None
    started_from: str | None = None


@dataclass(frozen=True, slots=True)
class Handle:
    provider: str
    ref: str
    attempt_id: str
    # Resolved at launch and recorded on the attempt (08, 13).
    image_digest: str | None = None
    name: str | None = None


class ObservationState(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class Observation:
    state: ObservationState
    exit_code: int | None = None
    detail: str | None = None
    # S5: 137 with the kernel's OOM kill is an environment failure, not a crash and not
    # a kill Crucible sent. Carried as a flag so classification never parses `detail`.
    oom_killed: bool = False
    never_started: bool = False
    container_message: str | None = None
    pod_events: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class LogOffset:
    """Where a log pull resumes (10).

    Docker has no byte offsets, so the position is the last stored line's timestamp
    and its sha256. `--since` is inclusive (S8), so the pull asks for lines at or after
    the timestamp and skips until the hash matches: strict-after, never by timestamp
    alone. `index` is what an in-memory provider counts with."""

    index: int = 0
    timestamp: str | None = None
    line_sha256: str | None = None
    # Which line at that timestamp the boundary is, counting from 0. Lines can repeat
    # inside one instant, so the hash alone does not say which one was last seen.
    occurrence: int = 0

    @property
    def is_start(self) -> bool:
        return self.index == 0 and self.timestamp is None


@dataclass(frozen=True, slots=True)
class LogChunk:
    stream: Literal["stdout", "stderr"]
    content: bytes
    ts: datetime | None = None
    # The sha256 of the last line in `content` and which line at `ts` it is. The three
    # together are the resume position for the next pull (10).
    line_sha256: str | None = None
    occurrence: int = 0
    lines: int = 0


@dataclass(frozen=True, slots=True)
class CollectedArtifact:
    """One file the collector copied out of the workspace. Bytes are data, never code."""

    name: str
    type: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class PathChange:
    """One record of `git diff --raw` or `git log --diff-merges=separate --raw` (hades
    #369): the path, its status letter (A, M, D, T), and the blob id it has after the
    change, all zeros for a deletion. `no_injected_files` reads it to tell a shim the
    branch adds from the repository's own file the branch edits or deletes."""

    path: str
    status: str
    blob: str
    # #400: plain, shim, deleted, or error with a reason; empty for older collectors.
    classification: str = ""


@dataclass(frozen=True, slots=True)
class BranchBundle:
    """What `git bundle create base_ref..work_branch` plus `git bundle verify` produced.

    The Docker collector builds this in C3; the fake provider synthesizes it so the
    gate path is exercised end to end (08)."""

    head_sha: str
    base_ref: str
    work_branch: str
    commits: int
    verified: bool
    sha256: str = ""
    commit_paths: tuple[str, ...] = ()
    commit_messages: tuple[str, ...] = ()
    # hades #369: every commit's `git log --diff-merges=separate --topo-order --raw`
    # records, children before parents and one block per parent of a merge. None when
    # the collector did not record them, which the gate judges as before #369.
    commit_changes: tuple[PathChange, ...] | None = None
    # hades FDY-0135: the collector's author check. None when the collector did not
    # finish it; otherwise the commits whose author email is not the policy's, as
    # (sha, email). Information for the reviewer, not a refusal (FDY-0143).
    commit_policy: CommitPolicyCheck | None = None
    # Paths touched only by commits after the preparer's trusted head. Branch-wide
    # paths include earlier attempts and cannot establish correction coverage (#498).
    attempt_commit_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommitPolicyCheck:
    """What `commit_policy_check` found over the range the publisher will push."""

    author_problems: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationRun:
    """One `required_verification` command Crucible itself re-ran after exit, in a fresh
    verifier container from the collected tree (11). The worker's own log is a claim;
    this is the evidence."""

    id: str
    command: str
    expect_exit: int
    exit_code: int
    log_tail: str
    ran: bool = True
    detail: str = ""
    # hades #184: wall-clock seconds inside the verifier container; None when it did not
    # record one (a verifier that never finished, or an older script).
    seconds: int | None = None

    @property
    def ok(self) -> bool:
        return self.ran and self.exit_code == self.expect_exit


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    """What `workspace_clean` reads (11): nothing labelled for this attempt is left
    behind once the collector and the verifier have been removed."""

    leftover: tuple[str, ...] = ()
    checked: bool = True
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CollectedOutputs:
    """What the collector produced (08): the report directory, the diff path list, the
    branch bundle summary, and the artifacts copied out of the workspace."""

    report: dict[str, Any] | None
    report_raw: str | None
    blocked_md: str | None
    stdout_tail: str = ""
    stderr_tail: str = ""
    diff_paths: tuple[str, ...] = ()
    # The full diff against base_ref. The secret scanner needs the content, not only the
    # path list (11), so a collector that cannot produce it leaves this None and the
    # no_secrets gate refuses to report `pass`.
    diff_text: str | None = None
    # hades #398: the scanner's matches over the whole diff and over every blob the
    # worker added or changed, read in bounded chunks while the collected files exist,
    # each named by its path (`diff` for the patch). None when the adapter did not scan,
    # as a fake that hands diff_text instead. `diff_unscanned` names the changed paths
    # whose content the collector did not export: coverage the gate cannot claim.
    diff_findings: tuple[SecretMatch, ...] | None = None
    diff_unscanned: tuple[str, ...] = ()
    # hades #369: `git diff --raw` against the same merge base as diff_paths. None when
    # the collector did not record it.
    diff_changes: tuple[PathChange, ...] | None = None
    # hades #369: the injected-name paths the merge base has, so a CLAUDE.md or AGENTS.md
    # a merge of the base brings in is the repository's own. None when not recorded.
    base_paths: tuple[str, ...] | None = None
    # hades #369: the collected path lists larger than their read limit, whose tail the
    # gates cannot see; `no_injected_files` fails when there is any.
    over_limit: tuple[str, ...] = ()
    bundle: BranchBundle | None = None
    artifacts: tuple[CollectedArtifact, ...] = ()
    # The verifier container's re-run of every required_verification command, and the
    # provider's own answer to "is anything of this attempt still running" (11).
    verifications: tuple[VerificationRun, ...] = ()
    workspace_state: WorkspaceState | None = None
    # Rejections the collector made while copying the report directory out: symlinks,
    # hard links, devices, and files above the size cap (08). Each becomes an event.
    copy_rejections: tuple[dict[str, str], ...] = ()
    # What the sync-back of the per-attempt credential copy did, when there was one (12).
    credential_sync: CredentialSync | None = None
    # A quota checkpoint collector can refuse worker-controlled Git metadata before it
    # runs Git. This survives collection so the unsafe-checkpoint wake names the cause.
    checkpoint_refusal: str | None = None
    # FDY-0140: the collector committed what the worker left uncommitted, or wrote why
    # it did not.
    leftover_committed: bool = False
    leftover_note: str | None = None
    interruption: Interruption | None = None


@dataclass(frozen=True, slots=True)
class CredentialFileSync:
    """One named auth file after the run (12): present in the copy, changed against the
    source, valid in shape, and written back or not, with the reason. Never a value."""

    name: str
    present: bool
    changed: bool
    valid: bool
    synced: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "present": self.present,
            "changed": self.changed,
            "valid": self.valid,
            "synced": self.synced,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CredentialSync:
    """What the sync-back of a per-attempt credential copy did (12)."""

    harness: str
    mount_mode: str
    files: tuple[CredentialFileSync, ...]
    removed: bool
    detail: str = ""

    @property
    def changed(self) -> bool:
        return any(f.changed for f in self.files)

    def as_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "mount_mode": self.mount_mode,
            "changed": self.changed,
            "removed": self.removed,
            "files": [f.as_dict() for f in self.files],
            "detail": self.detail,
        }


HARNESSES_LABEL = "crucible.harnesses"
LEGACY_HARNESS_LABEL = "crucible.harness"
LEGACY_VERSION_LABEL = "crucible.harness_version"


def harness_version_label(harness: str) -> str:
    return f"crucible.harness.{harness}.version"


def image_harnesses(labels: Mapping[str, str]) -> dict[str, str]:
    """The harnesses an image declares, by name, with the version each is pinned at (13).

    Since C11 one worker image carries every harness: `crucible.harnesses` lists them
    and `crucible.harness.<name>.version` pins each. An image built before C11 carries
    one, as `crucible.harness` and `crucible.harness_version`, and is still read. A
    harness the list names without a version maps to "", so the version check refuses
    it by name rather than taking it for a harness the image does not carry."""
    listed = labels.get(HARNESSES_LABEL)
    if listed is not None:
        names = sorted({name.strip() for name in listed.split(",") if name.strip()})
        return {name: labels.get(harness_version_label(name), "") for name in names}
    single = labels.get(LEGACY_HARNESS_LABEL)
    if single:
        return {single: labels.get(LEGACY_VERSION_LABEL, "")}
    return {}


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """A worker image the provider can see (13): its reference, its digest, and the
    harnesses its labels declare, name to version. The worker image carries all four
    real harnesses (C11); the e2e image carries the script harness."""

    reference: str
    digest: str
    harnesses: Mapping[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_labels(cls, reference: str, digest: str, labels: Mapping[str, str]) -> ImageInfo:
        return cls(reference, digest, image_harnesses(labels), dict(labels))

    def carries(self, harness: str) -> bool:
        return harness in self.harnesses

    def version_of(self, harness: str) -> str | None:
        return self.harnesses.get(harness) or None


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """The bounded auth probe (25): the hardened worker image, the credential mounted,
    a one-line prompt, a hard timeout. The launch shape comes from the adapter; the
    provider records only what 25 allows."""

    harness: str
    image: str
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    env_from_files: dict[str, str] = field(default_factory=dict)
    stdin_files: tuple[str, ...] = ()
    stdin_text: str = ""
    identity_text: str = ""
    timeout_seconds: int = 120
    policy: dict[str, Any] = field(default_factory=dict)
    # A model behind a local endpoint (13, S16): the worker's egress opens to it exactly
    # as it does for an attempt routed there, so the harness test's model call takes the
    # path a task's does (crucible#118).
    endpoint: Literal["subscription", "local"] = "subscription"
    endpoint_url: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What the probe recorded: exit facts, the image, and whether the named auth files
    changed. Tails stay with the caller for classification; nothing here is a value."""

    exit_code: int | None
    image_digest: str
    harness_version: str | None
    duration_seconds: float
    timed_out: bool = False
    oom_killed: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""
    credential_sync: CredentialSync | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """`ok`, `degraded` or `unavailable` with what was checked (25)."""

    state: str
    checks: dict[str, Any] = field(default_factory=dict)


class CleanupPolicy(StrEnum):
    KEEP = "keep"
    DELETE = "delete"
    KEEP_DIFF_ONLY = "keep_diff_only"


class ProviderError(Exception):
    """A provider failed before or while the harness ran (exit class environment)."""


class ProviderUnavailableError(ProviderError):
    """The provider's backend could not answer right now (a refused, reset or timed-out
    connection, or a server that said it is overloaded). Nothing about the attempt was
    decided by it: a caller that can wait, such as collection, asks again later rather
    than failing the attempt."""


class PrepareJobPodsTimeoutError(ProviderError):
    """hades #503: the preparer Job's Pods were still present when the provider's
    bounded, backed-off deletion wait ran out. The message says what the Job had done
    when the wait gave up (completed with an exit code, not finished in time, or still
    running). The supervisor prepares the same attempt again a bounded number of times,
    charging the task no attempt, before it classes the failure as the environment."""


class CollectionPendingError(ProviderUnavailableError):
    """Collection is waiting for backend cleanup. Retry on the next supervisor tick,
    up to the configured collection retry limit, while keeping the workspace intact."""


class LaunchRefusedError(ProviderError):
    """A launch the provider refused on purpose (07, 13): the image's harness version is
    outside the adapter's tested range, or the harness has no credential to run with.
    The supervisor turns this into a wake, not a retry."""


class LaunchWaitError(ProviderError):
    """The provider cannot take the attempt's next Pod right now, and nothing about the
    attempt is decided by that (hades #423): the namespace quota refused the gate
    probe, the preparer or the worker. The supervisor puts the attempt back to pending
    with this message as the reason and launches it on a later tick; the attempt is
    not consumed and no exit class is recorded."""


class WorkerStartError(ProviderError):
    """Hades #346: the runtime accepted the worker but could not start its process (a
    mount that is not a directory, an executable not found, an exec format error). The
    harness never ran, so this is an infrastructure interruption, not an environment
    failure of the attempt; `observation` carries the runtime's own message."""

    def __init__(self, observation: Observation) -> None:
        super().__init__(f"the worker never started: {observation.container_message}")
        self.observation = observation


class LaunchCancelledError(ProviderError):
    """The task was cancelled while its attempt was being prepared or launched (hades
    #189). The provider stopped at the step it was on, removed that step's Job or
    container and policy, and started nothing further; the supervisor settles the
    attempt as cancelled, not as a failure. The workspace claim and anything an earlier
    step left go with the attempt's other objects in retention."""


# Asked by `prepare` before each of its steps and while it waits on one, and by
# `launch` just before it creates the worker; True means the task was cancelled and the
# call stops with LaunchCancelledError (hades #189).
CancelCheck = Callable[[], Awaitable[bool]]


class ExecutionProvider(Protocol):
    name: str

    def capabilities(self) -> ProviderCapabilities: ...

    async def credential_available(self, harness: str) -> bool: ...

    async def probe_checks(
        self,
        spec: LaunchSpec,
        checks: Sequence[dict[str, Any]],
        checkout_token: InstallationToken | None = None,
        cancelled: CancelCheck | None = None,
    ) -> tuple[VerificationRun, ...] | None:
        """Run task checks on the unchanged base before preparation.

        None explicitly means unsupported; failures raise ProviderError. A private
        repository's read-only `checkout_token` is available only to this step.
        """
        ...

    async def gate_probe_exists(self, attempt_id: str) -> bool:
        """Whether this provider has an adoptable gate-probe step for an attempt."""
        ...

    async def prepare(
        self,
        spec: LaunchSpec,
        checkout_token: InstallationToken | None = None,
        cancelled: CancelCheck | None = None,
    ) -> Workspace:
        """Build the checkout. `checkout_token` is a private repository's read-only
        installation token (ADR 0019): the provider hands it to the preparation step
        alone, never to the worker, and the caller discards it once this returns.
        `cancelled` is asked before each step (a cache refresh, the checkout) and while
        one runs; when it answers True the prepare raises LaunchCancelledError."""
        ...

    async def launch(
        self, ws: Workspace, spec: LaunchSpec, cancelled: CancelCheck | None = None
    ) -> Handle:
        """Start the worker. `cancelled` is asked once more just before the worker is
        created; when it answers True no worker is created and the launch raises
        LaunchCancelledError. A cancel after that is the supervisor's to act on."""
        ...

    async def observe(self, h: Handle) -> Observation: ...

    async def logs(self, h: Handle, since: LogOffset) -> list[LogChunk]: ...

    async def collect(
        self, h: Handle, ws: Workspace, spec: LaunchSpec | None = None
    ) -> CollectedOutputs: ...

    async def terminate(self, h: Handle, mode: Literal["drain", "kill"]) -> None: ...

    async def cleanup(
        self, ws: Workspace, policy: CleanupPolicy, spec: LaunchSpec | None = None
    ) -> None: ...

    async def release_workspace(self, ws: Workspace, spec: LaunchSpec | None = None) -> None:
        """Remove what a cleanup policy kept of an attempt's workspace (its claim, its
        directory), once the retention step decided nothing needs it any more (16).
        Idempotent: a workspace already gone is the state asked for. Raises
        ProviderError when it could not be removed, so the step tries again."""
        ...

    async def reconcile(self) -> list[Handle]: ...

    async def retention(self, keep: Sequence[str]) -> int:
        """Remove provider-side leavings for attempts that are gone (16). Returns the
        count removed. A provider with nothing to remove returns 0."""
        ...

    async def list_images(self) -> list[ImageInfo]:
        """The worker images this provider can run, with their labels (13, 25)."""
        ...

    async def discard(self, ws: Workspace, spec: LaunchSpec | None = None) -> None:
        """Remove anything secret the provider placed for an attempt that will never be
        collected: a launch that failed after the credential was seeded, or a worker
        that was lost (12). Cleanup is separate and may never run for such an attempt."""
        ...

    async def probe_credential(self, request: ProbeRequest) -> ProbeResult:
        """25: run the hardened image with the credential mounted for one prompt under a
        hard timeout, sync the named auth files back, remove everything, and report
        the exit facts and whether the files changed."""
        ...

    async def health(self) -> ProviderHealth:
        """25: daemon reachable, network present, disk headroom, as one state."""
        ...


class ActivityProbe(Protocol):
    """A provider whose workspace is not a local path (26) reads activity off the
    running worker itself (FDY-0140). The supervisor walks a local workspace on its own;
    a provider that has this method answers instead of that walk."""

    async def activity(self, h: Handle, ws: Workspace) -> tuple[int, int, int] | None:
        """A fingerprint of the worker's checkout, report directory and home: the
        newest modification time, the entry count and the total bytes. Only equality
        between two answers means anything. None when it could not be read this time,
        which is neither activity nor its absence."""
        ...
