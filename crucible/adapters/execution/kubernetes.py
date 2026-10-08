"""The Kubernetes execution provider (08, 26).

One Job per role per attempt in the workers namespace, a PersistentVolumeClaim as the
workspace, the identity bundle as a ConfigMap, the harness credential as a per-attempt
Secret seeded from the harness's own Secret, and a per-attempt NetworkPolicy in place of
the egress proxy. No Docker socket anywhere, and no call outside the one namespace the
supervisor's ServiceAccount is bound to.

Everything above the provider is the Docker provider's, unchanged: the same identity
bundle, the same preparer, collector, bundle-verifier and verifier scripts, the same
reading of what they wrote, and the same credential rules (12). What differs is only
how a container is created and how bytes come back out of a workspace:

- a Job and a Pod instead of a container, with the pod shape of 26 enforced by this
  provider and again by the namespace's Pod Security admission;
- a NetworkPolicy instead of `HTTPS_PROXY` and a Squid allowlist;
- a short-lived **reader Pod** instead of a shared artifact volume. The Crucible pods
  never mount a workspace claim (26), so the collected output comes back as a tar on an
  exec stream, and so does a rotated auth file. Neither ever passes through a Pod log:
  the kubelet writes those to the node's disk, and a credential on a node is exactly
  what 12 forbids.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import hashlib
import ipaddress
import json
import logging
import os
import shutil
import socket
import tarfile
import tempfile
import time
import weakref
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from crucible.adapters.execution import identity as identity_bundle
from crucible.adapters.execution import k8sspec, scripts, workspace
from crucible.adapters.execution.collected import BlobTarScan, read_outputs, read_verifications
from crucible.adapters.execution.create_policy import image_allowed
from crucible.adapters.execution.endpoint_health import probe_model_endpoint
from crucible.adapters.execution.k8sapi import (
    _TRANSPORT_ERRORS,
    ExecResult,
    KubernetesApiError,
    KubernetesClient,
    KubernetesUnavailableError,
    LogFrame,
)
from crucible.adapters.execution.k8sregistry import (
    RegistryClient,
    RegistryError,
    auths_from_dockerconfigjson,
)
from crucible.adapters.execution.k8sspec import (
    EgressPlan,
    Limits,
    Mount,
    PeerSelector,
    PodRequest,
    SpecError,
)
from crucible.adapters.execution.logstream import RESUME_AT_BOUNDARY
from crucible.adapters.execution.logstream import chunks as _chunks
from crucible.adapters.harness.registry import default_registry
from crucible.application.credential_renewer import access_token_document, worker_credential_spec
from crucible.application.harnesses import (
    HarnessRegistry,
    check_image_version,
    egress_allowlist,
)
from crucible.contracts.completion_claim import CompletionClaimV1
from crucible.domain.cluster_egress import ClusterEgress, parse_cluster_egress
from crucible.domain.command_timeout import effective_command_timeout_ms
from crucible.domain.exit_class import ExitClass
from crucible.domain.ids import new_id
from crucible.domain.infrastructure import START_FAILURES
from crucible.domain.role_timeouts import (
    DEFAULT_API_RETRY_SECONDS,
    DEFAULT_ROLE_TIMEOUT_SECONDS,
    parse_role_timeouts,
)
from crucible.domain.secrets import redact
from crucible.domain.time import parse_rfc3339
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    OUTPUT_MOUNT,
    PACKAGE_CACHE_ENV,
    PACKAGE_CACHE_LEAF,
    PACKAGE_CACHE_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    VERIFIER_CACHE_LEAF,
    VERIFY_MOUNT,
    WORK_MOUNT,
    CancelCheck,
    CleanupPolicy,
    CollectedArtifact,
    CollectedOutputs,
    CollectionPendingError,
    CredentialFileSync,
    CredentialSync,
    Handle,
    ImageInfo,
    IsolationLevel,
    LaunchCancelledError,
    LaunchRefusedError,
    LaunchSpec,
    LaunchWaitError,
    LogChunk,
    LogOffset,
    Observation,
    ObservationState,
    PrepareFailedError,
    ProbeRequest,
    ProbeResult,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
    ProviderUnavailableError,
    VerificationRun,
    WorkerCapacity,
    Workspace,
    WorkspaceState,
)
from crucible.ports.github import InstallationToken
from crucible.ports.harness import AuthFile, CredentialSpec, ExitInfo, LaunchContext, MountMode

log = logging.getLogger("crucible.provider.kubernetes")


def is_transport(exc: BaseException) -> bool:
    """Classify the client boundary by status and preserved socket cause.

    k8sapi._unreachable chains the original socket exception with status zero.
    Zero alone also covers local configuration errors, which must not be retried.
    """
    if isinstance(exc, KubernetesApiError):
        if exc.status:
            return exc.status in (502, 503, 504)
        return exc.__cause__ is not None and is_transport(exc.__cause__)
    return isinstance(exc, _TRANSPORT_ERRORS)


def _inside_declared_network(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network, declared: str
) -> bool:
    candidate = ipaddress.ip_network(declared)
    return (
        network.version == candidate.version
        and int(network.network_address) >= int(candidate.network_address)
        and int(network.broadcast_address) <= int(candidate.broadcast_address)
    )


PROVIDER_NAME = "kubernetes"

# A name to the addresses a NetworkPolicy may name.
Resolver = Callable[[str], list[str]]

# What the canary resolves to prove cluster DNS works: a name every cluster serves, looked
# up through the pod's search path so the cluster domain need not be known here.
CANARY_DNS_NAME = "kubernetes.default.svc"

# How long to reuse an unsettled probe result without re-running the canary.
# Prepare and launch both call _require_ready -> ensure_ready; while the local
# endpoint is down both would otherwise run a fresh canary each time (issue 160).
_UNSETTLED_TTL_SECONDS: float = 45.0

# The runtime settings source: the `kubernetes.egress` document (None when it was never
# saved) and the enabled local endpoint URL (None when no local model is enabled).
SettingsSource = Callable[[], tuple[Mapping[str, Any] | None, str | None]]
# The saved `kubernetes.timeouts` document, or None for the settings file's value.
TimeoutsSource = Callable[[], Mapping[str, Any] | None]

# Where `prepare` records which ConfigMap key is which bundle file, so `launch` projects
# every file back to the relative path the bundle hash covers.
ANNOTATION_IDENTITY_PATHS = "crucible.io/identity-paths"

# What a role Job's exit is when Crucible never got one: the API call failed, or the
# wait ran out and the Job was deleted. Distinct from any code a container can exit
# with, so a caller can tell "it failed" from "it never finished" (the Docker provider's
# two sentinels, with the same values, because the callers are the same code).
JOB_API_ERROR = -1
JOB_TIMED_OUT = -2
# hades #370: the role's Pod ran and wrote nothing for the stall bound, so Crucible ended
# it before its timeout. Only a Job run with `stall_seconds` (the preparer) ends this way.
JOB_STALLED = -3

# One attempt may create a gate-probe, preparer, worker, collector, bundle-verifier
# and verifier Job. The probe is deleted before preparation and never overlaps this
# attempt's other Jobs, so its addition does not increase the five-Job capacity count.
# A ResourceQuota's Job count therefore needs this conversion before it can truthfully be
# shown as attempt capacity.
JOBS_PER_ATTEMPT = 5

# How many polls `launch` gives the Job controller to create the worker's Pod before it
# hands the attempt to `observe` (hades #423). A Pod normally exists within one; a
# `FailedCreate` naming the namespace quota in this window ends the launch as a wait,
# never as a running attempt the quota then fails.
LAUNCH_POD_POLLS = 5

POD_DELETION_MARGIN_SECONDS = 5
DEFAULT_POD_GRACE_SECONDS = 30
COLLECTION_ROLES = (k8sspec.ROLE_COLLECTOR, k8sspec.ROLE_BUNDLE, k8sspec.ROLE_VERIFIER)

# Deleting a Pod is the only way to signal one. Kubernetes sends SIGTERM, waits the
# grace period and then SIGKILLs, and the Pod object is gone either way, so the exit
# code the container produced is gone with it. These are the codes the Docker provider
# records for the same two acts, and `observe` falls back to them only for a Pod that
# Crucible itself terminated (16).
DRAIN_EXIT_CODE = 143
KILL_EXIT_CODE = 137

# 26's object table names the Jobs; `crucible.role` keeps the Docker provider's role
# names, so one label means the same thing on both providers. The two differ for three
# roles and that is the whole mapping.
# The key the checkout token Secret keeps a private repository's token under (ADR 0019).
CHECKOUT_TOKEN_KEY = "token"

OBJECT_PREFIX: dict[str, str] = {
    k8sspec.ROLE_PREPARER: "prepare",
    k8sspec.ROLE_CACHE_REFRESHER: "refresh-cache",
    k8sspec.ROLE_WORKER: "worker",
    k8sspec.ROLE_COLLECTOR: "collect",
    k8sspec.ROLE_BUNDLE: "verify-bundle",
    k8sspec.ROLE_VERIFIER: "verifier",
    k8sspec.ROLE_PUBLISHER: "publish",
    k8sspec.ROLE_CLEANER: "cleaner",
    k8sspec.ROLE_READER: "reader",
}

# hades #425: the worker's egress is the policy's `egress_allowlist` as written (05b:
# "hostnames the egress proxy permits for workers"), the same list the Docker provider's
# Squid permits. Until #425 this provider subtracted `github.com` and `api.github.com`
# from the worker and the verifier on 26's old sentence that a worker never reaches
# GitHub, so a policy that allowlisted github.com produced a worker whose curl to it timed
# out against the default deny while the task page said it was permitted. The git roles
# (preparer, cache refresher, publisher) still get GitHub whether or not the policy names
# it; a worker gets it only when the policy does, and holds no credential for it either way.

DEFAULT_IMAGE_ALLOWLIST: tuple[str, ...] = (
    "crucible-worker:*",
    "ghcr.io/sentania-labs/crucible-worker:*",
)

# How much of one file the reader Pod will hand back. A worker owns its credential copy
# and can leave anything at that path, so the read is bounded before it is parsed (12).
CREDENTIAL_READ_LIMIT = 1024 * 1024
# Tags resolved at once while listing images (108). Each is two crane processes. Also
# the size of the registry's own thread pool, so registry work never holds more threads
# than this, and never one of the pool every Kubernetes API call runs on.
LIST_IMAGES_CONCURRENCY = 6
# How long one listing may take, from its start to the last crane process it runs (108).
# Below the 15 seconds the harness and image endpoints wait for it, so a slow registry
# ends the listing, and every crane process in it, before an endpoint gives up.
LIST_IMAGES_DEADLINE = 12.0
# A ci-* tag is a CI proof push (the images or registry job's own resolve check), never
# a promotable image: resolving one costs the same two crane calls as a release tag, and
# they accumulate forever since nothing prunes them (111). Skipping them by prefix, not
# by requiring a release-version shape, keeps a future non-version tag (a hotfix build,
# a manual pin) listable without a code change.
LIST_IMAGES_SKIP_PREFIX = "ci-"
# How much of a worker's log one observation poll reads (issue 63): the API's
# `limitBytes`, so a poll never holds a whole long-running log in memory. A capped read
# ends at its last complete line and the next poll resumes from there (10). `sinceTime`
# is one-second granular, so a read that cannot get past its first second (more than
# the cap logged inside it) is retried larger, up to the ceiling; past the ceiling the
# rest of that second is skipped with a notice line, never read unbounded.
LOG_READ_LIMIT = 4 * 1024 * 1024
JOB_TAIL_LINES = 2000
LOG_READ_CEILING = 64 * 1024 * 1024
# How much of a collected output tar is accepted. The tree and the exported blobs are
# excluded from it, so this is the diff, the bundle, the report copy and the verifier
# logs.
OUTPUT_READ_LIMIT = 256 * 1024 * 1024
# hades #398: the blobs the worker added or changed come off the claim as their own tar,
# streamed through the secret scanner and never written to local disk, so they do not
# count against the archive above (the bundle already carries each of them once). The
# same bound, counted on its own: a stream cut at it is a collection failure, as above.
CHANGED_BLOBS_READ_LIMIT = OUTPUT_READ_LIMIT
# The one line the activity probe prints (FDY-0140).
ACTIVITY_READ_LIMIT = 4096

# How long an administrative run's objects (a probe's claim and Jobs, a login's policy)
# are left alone by the retention sweep. The API process that created them removes them
# itself; this is how long the supervisor waits before deciding that process died.
ADMIN_GRACE_SECONDS = 2 * 3600

# The login Job's size (25, 26). A login runs one CLI and holds a few kilobytes of auth
# state on its memory-backed home, so it asks for a fraction of a worker.
LOGIN_POLICY: dict[str, Any] = {
    "resources": {
        "cpus": 1,
        "memory": "1GiB",
        "memory_request_fraction": 0.5,
        "tmpfs_per_mount": "128MiB",
    },
    "limits": {"grace_seconds": 5},
}
# How long the login Pod waits after its CLI exits for the service to read the auth
# files off it over exec, before its deadline ends it. The TTL then removes the Job.
LOGIN_READBACK_SECONDS = 300
LOGIN_TTL_SECONDS = 60
# How long past the login's own deadline its lock outlives it: the image resolution and
# the namespace probe before the Job exists, and the Job's removal after. Only an api
# that died holding the lock ever waits this out; a login that ends releases it.
LOGIN_LOCK_SLACK_SECONDS = 300
ANNOTATION_LOCK_HOLDER = "crucible.io/login-holder"
ANNOTATION_LOCK_EXPIRES = "crucible.io/login-lock-expires"

# Pod phases that are not a running worker but not a loss either.
_PENDING_PHASES = frozenset({"Pending"})
# `status.reason` values that mean the Pod is gone because the cluster took it (26).
_LOST_REASONS = frozenset({"Evicted", "NodeShutdown", "Shutdown", "NodeAffinity", "NodeLost"})


class CollectionFailedError(ProviderError):
    """The collector could not produce the outputs. The attempt is an `environment`
    failure and nothing is collected from it (16)."""


class CollectionUnavailableError(CollectionFailedError, ProviderUnavailableError):
    """Collection could not finish because the cluster could not take or answer a step
    right now (the API server, the namespace quota, a reader Pod that did not start).
    The claim still holds the worker's work, so the supervisor collects again later;
    a step that already ran (the credential sync, the collector) runs or answers again."""


class LoginLockHeldError(ProviderError):
    """Another login of the harness holds its lock, in this api replica or another."""


HarnessRefusedError = LaunchRefusedError


@dataclass(frozen=True, slots=True)
class KubernetesConfig:
    """Everything the provider needs that is not on the launch spec."""

    namespace: str = "crucible-workers"
    service_account: str = "crucible-worker"
    storage_class: str = ""
    workspace_size: str = "20Gi"
    image_pull_secret: str | None = None
    # A cluster-side PVC holding the reference cache the preparer clones from. Without
    # one the preparer clones from the remote directly, as the Docker provider does
    # with `use_reference_cache` off.
    cache_claim: str | None = None
    # 26: a Pod Pending longer than this is a launch failure with the Pod's conditions
    # as the detail (image pull, no schedulable node, PVC unbound), never a stall.
    launch_timeout_seconds: int = 300
    prepare_timeout_seconds: int = 900
    # hades #370: how long the preparer's Pod may run without writing a log line before
    # it is ended as a stall, with that as the detail, instead of waiting the whole
    # `prepare_timeout_seconds` for a clone that makes no progress. The clone prints its
    # progress, so a large repository that is still transferring is not a stall. Well
    # below the prepare timeout; 0 turns the bound off.
    preparer_stall_seconds: int = 300
    collector_timeout_seconds: int = 900
    verifier_timeout_seconds: int = 3600
    # The short roles' own time, counted from when their Pod is Running (the image pull
    # and scheduling come out of `launch_timeout_seconds`): the bundle verifier, the
    # cleaner, and the Job that readies a claim for the publisher. The settings file
    # seeds it and the `kubernetes.timeouts` admin setting replaces it at runtime.
    role_timeout_seconds: int = DEFAULT_ROLE_TIMEOUT_SECONDS
    report_size_cap_bytes: int = 10 * 1024 * 1024
    log_tail_bytes: int = 64 * 1024
    # The dispatch limit when the namespace has no ResourceQuota to derive one from. With
    # a quota the capacity is the quota's (hades #423) and this number is not consulted.
    max_concurrency: int = 3
    # hades #423: how many of Hades's own short-role Pods (gate probe, collector, canary,
    # login, preparer) are kept room for beside the workers the quota admits, so a probe
    # fits while workers are at capacity. The reservation is this many Pods of the
    # largest short-role shape, which is the worker's own (the roles run at the policy
    # limits; the canary is smaller).
    short_role_pods: int = 1
    poll_interval_seconds: float = 2.0
    api_timeout_seconds: float = 30.0
    api_retry_seconds: float = DEFAULT_API_RETRY_SECONDS
    # The cluster's DNS service address. 26 allows port 53 on this address and nothing
    # else on it; every other destination inside the cluster stays denied.
    cluster_dns_ip: str = "10.96.0.10"
    # The cluster resolver and an in-cluster local endpoint as namespace and pod
    # selectors (crucible#91), which a CNI that translates a service address before it
    # evaluates policy still matches. Seeded from the settings file and replaced at
    # runtime by the `kubernetes.egress` admin setting.
    egress: ClusterEgress = field(default_factory=ClusterEgress)
    # Crucible's own namespace. No selector may name it or the workers namespace.
    control_namespace: str = "crucible"
    # The enabled local model endpoint the readiness canary proves a connection to,
    # from the routing policy in force. Empty when no local model is enabled.
    local_endpoint_url: str = ""
    # How often the runtime settings above are read back from the database, so the
    # supervisor follows an edit made through the API process without a restart.
    settings_refresh_seconds: float = 15.0
    denied_cidrs: tuple[str, ...] = k8sspec.DEFAULT_DENIED_CIDRS
    local_endpoint_cidrs: tuple[str, ...] = ()
    # 26: the allowlist is "resolved to CIDRs or FQDN rules where the CNI supports
    # them". A plain `networking.k8s.io/v1` CNI has no FQDN rule, so the names are
    # resolved here and the policy carries their addresses. Turning this off gives the
    # broad rule instead ("the public internet on 443, minus every denied range"), which
    # a deployment may want when its CNI enforces names some other way. It applies to
    # the git and login roles only; a worker and a verifier always get their resolved
    # allowlist (`_broad_for`, hades #425).
    broad_egress: bool = False
    # How long a resolved address stays in a policy before it is looked up again.
    resolve_ttl_seconds: float = 300.0
    extra_image_allowlist: tuple[str, ...] = ()
    # The harness credential Secrets in the workers namespace, by harness name (12, 26).
    # The service creates and owns them (ADR 0015); this only names them, and a harness
    # left out is `crucible-harness-<harness>` with `_` as `-`.
    credential_secrets: Mapping[str, str] = field(default_factory=dict)
    # The operator's declared mount mode per harness, as the Docker configuration
    # carries it. It may raise the adapter's minimum and never lowers it (25 step 7).
    credential_modes: Mapping[str, MountMode] = field(default_factory=dict)
    # Worker image repositories `list_images` reports the promoted tags of (25). Bare
    # repositories: the listing appends each tag the registry returns.
    image_repositories: tuple[str, ...] = ()
    # One exact, pullable worker image for the readiness canary (26). It is deliberately
    # not an entry of `image_repositories`: the listing would take it for a repository,
    # list the same tags a second time, and then build `<repo>:<probe tag>:<tag>` for
    # each of them. `parse_reference` does not reject that shape; `crane digest` does,
    # locally, before any request, and the failure drops the entry the way an
    # unavailable image is dropped. The cost is the second tag listing plus one failing
    # crane process per tag on every `GET /admin/images`.
    probe_image: str = ""
    use_reference_cache: bool = True
    # 26, issue 93: the canary is a shell script with curl, not a role pod, so it asks
    # for a fixed small size instead of the policy's limits. Small enough that a
    # namespace readiness probe never itself contests the budget a role pod needs.
    canary_cpu_millicores: int = 100
    canary_memory: str = "64Mi"
    # An operator-declared pod-level PID limit (95), for a cluster whose container
    # runtime hides the pod's own cgroup from the canary (a private cgroup namespace,
    # the default on current containerd and runc). Only fills in for a canary result the
    # gate could not read at all; a canary that positively read no limit still fails
    # regardless of this value.
    pod_pid_limit_override: int | None = None
    # The one host a private repository's checkout token is answered for (ADR 0019),
    # the same `github.credential_host` the publisher's helper uses (23). When it is not
    # github.com, the preparer and the cache refresher of a private repository may also
    # reach it.
    credential_host: str = "github.com"

    def credential_secret_name(self, harness: str) -> str:
        # A Secret name is a DNS subdomain, which has no underscore: claude_code's
        # default is `crucible-harness-claude-code`, the name the deployment maps it to.
        return self.credential_secrets.get(harness) or (
            f"crucible-harness-{harness.replace('_', '-')}"
        )


@dataclass(frozen=True, slots=True)
class NamespaceProbe:
    """The namespace readiness probe of 26: a canary Pod that must fail to reach the API
    server, and the node's pod PID limit.

    Both are facts about the cluster, not about an attempt, so they are probed once and
    shown on the status page (25). The provider refuses to launch until the probe has
    passed, because a namespace whose CNI does not enforce egress NetworkPolicy gives a
    worker the API server, and a node with no pod PID limit gives it a fork bomb."""

    passed: bool
    egress_enforced: bool
    pid_limit: int | None
    detail: str = ""
    checked: bool = True
    # What the canary found under the same egress rules a worker gets (crucible#91):
    # whether a cluster name resolved, and whether the configured local endpoint took a
    # TCP connection. None is "not checked" (no local endpoint) or "could not tell".
    dns_resolves: bool | None = None
    local_endpoint_reachable: bool | None = None
    # The endpoint-specific failure text, set only when `local_endpoint_reachable` is not
    # True (crucible#110). Kept apart from `detail`'s pass/fail verdict because a down
    # local endpoint no longer fails the probe by itself: it names the reason a launch
    # routed to that endpoint is refused, while every other launch keeps running.
    local_endpoint_detail: str | None = None
    # Where `pid_limit` came from, or why there is none (95): "cgroup-v2-parent" when
    # the pod-level cgroup was read directly, "cgroupns-private" when the container's
    # cgroup namespace hides it, "cgroup-v1" when the hierarchy is the unsupported one,
    # or "" when the probe never got far enough to know.
    pid_limit_source: str = ""
    # The canary could not run because the API server could not answer. That is no
    # verdict on the namespace: a launch that meets it fails as an environment failure
    # the retry rule covers, never a refusal, and the next launch runs the canary again.
    unavailable: bool = False

    canary_node: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "namespace_ready": self.passed,
            "egress_enforced": self.egress_enforced,
            "dns_resolves": self.dns_resolves,
            "local_endpoint_reachable": self.local_endpoint_reachable,
            "local_endpoint_detail": self.local_endpoint_detail,
            "pod_pid_limit": self.pid_limit,
            "pod_pid_limit_source": self.pid_limit_source,
            "runtime_class": "standard",
            "detail": self.detail,
            "canary_node": self.canary_node,
        }


@dataclass(frozen=True, slots=True)
class _CredentialCopy:
    """What was seeded for one attempt: the adapter's spec, the harness Secret it came
    from, the effective mode, and the sha256 of each file as seeded (in memory only)."""

    spec: CredentialSpec
    source_secret: str
    mode: MountMode
    seeded: dict[str, str | None] = field(default_factory=dict)

    @property
    def writable(self) -> bool:
        return self.mode is MountMode.RW_NARROW


@dataclass(frozen=True, slots=True)
class LoginLock:
    """One harness's login lock as this process took it: the ConfigMap's name and the
    uid of the incarnation it created, so a release or a store check never mistakes a
    lock another replica took after this one expired for its own."""

    harness: str
    name: str
    uid: str
    holder: str


@dataclass(slots=True)
class _Launched:
    job_name: str
    spec: LaunchSpec
    image_digest: str
    # The limits the worker runs under. Policy-derived at launch, then replaced by what
    # the live Pod carries once it is seen (issues 66, 76): an adopted attempt has no
    # policy in memory, and admission may have rewritten what was asked for.
    limits: Limits
    network_policy: str | None = None
    credential: _CredentialCopy | None = None
    pod_name: str | None = None
    node: str | None = None
    launched_at: float = 0.0
    # What Crucible itself did to the Pod, so a Pod that is gone because Crucible
    # deleted it is never reported as lost (16).
    terminated: str | None = None
    exit_code: int | None = None
    # Where `limits` came from, so the evidence says what it records: `policy` at
    # launch, `template` for an attempt adopted before its Pod existed, `pod` once the
    # live Pod has been read.
    limits_source: Literal["policy", "template", "pod"] = "policy"


# What an adopted attempt's `_Launched` carries before the supervisor hands the real
# launch spec back on the next collect. It names nothing and runs nothing.
_ADOPTED_SPEC = LaunchSpec(
    attempt_id="",
    task_id="",
    external_id="",
    role="adopted",
    harness="",
    model="",
    image="",
    timeout_seconds=0,
    contract={},
)


# A refresh younger than this is fresh enough: a prepare that finds one skips its own and
# only reads, so attempts of one repository started together clone side by side (#55).
CACHE_REFRESH_INTERVAL_SECONDS = 60.0


class _CacheGate:
    """A readers-writer gate over one reference cache in this process.

    The refresher writes the mirror (a fetch can prune refs and repack); a preparer
    reads it through `--reference`. Many preparers may read at once. A refresh waits for
    the readers already in to finish, and holds new ones back while it waits, so it is
    never starved. Refreshes are coalesced: a prepare refreshes only when no refresh is
    running or waiting and the last finished more than the interval ago. Only the
    supervisor prepares checkouts, so one process is the whole population (26)."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writing = False
        self._writers_waiting = 0
        self._refreshed_at: float | None = None

    def refresh_due(self, now: float, interval: float = CACHE_REFRESH_INTERVAL_SECONDS) -> bool:
        if self._writing or self._writers_waiting:
            return False
        return self._refreshed_at is None or now - self._refreshed_at >= interval

    @contextlib.asynccontextmanager
    async def reading(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._writing and not self._writers_waiting)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def writing(self) -> AsyncIterator[None]:
        # Counted before the first await, so a prepare that checks `refresh_due` right
        # after this one sees the refresh coming and does not queue a second.
        self._writers_waiting += 1
        try:
            async with self._condition:
                await self._condition.wait_for(lambda: not self._writing and self._readers == 0)
                self._writing = True
        finally:
            self._writers_waiting -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._writing = False
                self._refreshed_at = time.monotonic()
                self._condition.notify_all()


class KubernetesProvider:
    """The provider of 08 on Jobs and Pods, as 26 specifies it."""

    name = PROVIDER_NAME

    def __init__(
        self,
        config: KubernetesConfig,
        client: KubernetesClient,
        registry: RegistryClient,
        harnesses: HarnessRegistry | None = None,
        resolver: Resolver | None = None,
        settings_source: SettingsSource | None = None,
        timeouts_source: TimeoutsSource | None = None,
        credential_dead: Callable[[], bool] | None = None,
    ) -> None:
        self.config = config
        # The runtime settings (the `kubernetes.egress` admin setting and the enabled
        # local endpoint), read back from the database; None in the unit tier.
        self._settings_source = settings_source
        self._timeouts_source = timeouts_source
        self._credential_dead = credential_dead or (lambda: False)
        self._file_role_timeout = config.role_timeout_seconds
        self._file_api_retry = config.api_retry_seconds
        self._settings_read_at: float | None = None
        self._file_egress = config.egress
        # Injected so the unit tier resolves without a network and the e2e tier can
        # point at the cluster's own DNS.
        self.resolve = resolver or _resolve_host
        self.client = client
        self.registry = registry
        # `wire` always passes the deployment's registry; one built without it is a test's,
        # which may launch the script harness (crucible#124).
        self.harnesses = harnesses or default_registry(test_fixtures=True)
        self._launched: dict[str, _Launched] = {}
        self._images: dict[str, ImageInfo] = {}
        self.last_error: dict[str, str] = {}
        # The same, per role and attempt, since collections now run side by side: the
        # text, and whether the Job could not be taken right now (the API server or the
        # namespace quota) rather than refused or failed.
        self.role_errors: dict[tuple[str, str], tuple[str, bool]] = {}
        # The credential sync of an attempt whose copy was read back and removed, kept
        # until cleanup, so a collection that runs again records that outcome and not
        # "absent after the run" (in memory: a restart records the second reading).
        self._credential_syncs: dict[str, CredentialSync] = {}
        # A completed collection Job must not run again just because its Pod took
        # another tick to disappear. Cleanup forgets these process-local results.
        self._collection_role_exits: dict[tuple[str, str], int] = {}
        # Why `_await_job` ended a wait early, by Job name (a quota refusal, a Pod that
        # never started); `_run_role_job` moves it into `last_error`.
        self._job_refusals: dict[str, str] = {}
        # hades #370: why `_await_job` ended a wait for a stall, by Job name: the Pod ran
        # and wrote nothing for the stall bound. `_run_role_job` adds the last output.
        self._job_stalls: dict[str, str] = {}
        # Jobs whose wait ran out while the API server was not answering (PR 237 review):
        # that is an outage to retry, not a role that took too long.
        self._job_unanswered: set[str] = set()
        self.probe: NamespaceProbe | None = None
        # When the probe last became unsettled (local_endpoint_detail set). Cached
        # unsetttled results are reused for a bounded window so that prepare and
        # launch of the same attempt do not run the canary twice (issue 160).
        self._probe_unsettled_at: float | None = None
        # Set by `prepare` after a successful _require_ready; cleared at the start
        # of `launch`. While true, a subsequent ensure_ready() call returns the
        # cached (unsettled or settled) probe without re-running the canary.
        self._probe_cached_for_launch: bool = False
        # One lock per event loop: the API serves requests on its own loop and runs each
        # login on a loop of its own thread, and an asyncio lock belongs to one loop.
        self._probe_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        # One gate per reference cache (one per repository), per loop as above: a
        # refresh writes the mirror only while no preparer is cloning from it (#55).
        self._cache_gates: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, _CacheGate]
        ] = weakref.WeakKeyDictionary()
        # The attempt ids of the administrative runs this process has in flight (25),
        # with what each one is; their objects carry `crucible.admin` (see `_labels`).
        self._admin_runs: dict[str, str] = {}
        # The sha256 of each auth file as `prepare` seeded it, until `launch` takes it,
        # so the sync-back can say whether the harness changed a file (12). In memory
        # only: after a restart the copy is compared by issued-at alone, as before.
        self._seeded: dict[str, dict[str, str | None]] = {}
        self._quota_concurrency: int | None = None
        # hades #423: the last reading of the namespace quota as worker capacity, with
        # the short-role reservation it holds back; None until a quota has been read.
        self._quota: WorkerCapacity | None = None
        # One Pod's limits as the most recent launch asked for them, which is what the
        # quota's CPU and memory are divided by for the advertised capacity.
        self._last_limits: Limits | None = None
        # The active delivery policy document; used by _read_quota to derive the
        # per-attempt resource shape when no launch has happened yet (hades #423 / #478).
        self._policy: dict[str, Any] = {}
        self._pull_auths_loaded = False
        self._resolved: dict[str, tuple[float, tuple[str, ...]]] = {}
        # Registry reads run crane and wait on another host; they get threads of their
        # own so a slow registry cannot take the ones Kubernetes API calls need (108).
        self._registry_pool = ThreadPoolExecutor(
            max_workers=LIST_IMAGES_CONCURRENCY, thread_name_prefix="crucible-registry"
        )
        # The image listing in flight on each event loop, which every caller shares.
        self._listings: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Task[list[ImageInfo]]
        ] = weakref.WeakKeyDictionary()

    def set_credential_dead_check(self, check: Callable[[], bool]) -> None:
        """Install the supervisor-owned Codex credential health gate."""
        self._credential_dead = check

    # ----- helpers -----------------------------------------------------

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _call_with_backoff(
        self, fn: Any, *args: Any, deadline_seconds: float | None = None, **kwargs: Any
    ) -> Any:
        """Retry pre-worker transport failures within a monotonic time budget."""
        budget = self.config.api_retry_seconds if deadline_seconds is None else deadline_seconds
        deadline = time.monotonic() + max(0.0, budget)
        delay = 1.0
        attempt = 1
        while True:
            try:
                return await self._call(fn, *args, **kwargs)
            except (KubernetesApiError, OSError) as exc:
                if not is_transport(exc):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                log.info(
                    "API transport failure on attempt %d (%s); backing off",
                    attempt,
                    type(exc).__name__,
                )
                await asyncio.sleep(min(delay, remaining))
                if time.monotonic() >= deadline:
                    raise
                delay = min(delay * 2, 16.0)
                attempt += 1

    async def _create_with_backoff(self, kind: str, body: Mapping[str, Any]) -> None:
        # A unique marker survives server defaulting and controller mutations. A
        # conflict after a lost response is success only for this exact request.
        marker = "crucible.io/create-request"
        request_id = new_id()
        metadata = dict(body.get("metadata") or {})
        metadata["annotations"] = {**metadata.get("annotations", {}), marker: request_id}
        request = {**body, "metadata": metadata}
        ambiguous = False

        def create() -> Any:
            nonlocal ambiguous
            try:
                return self.client.create(kind, request)
            except KubernetesApiError as exc:
                if exc.status == 409 and ambiguous:
                    existing = self.client.get(kind, str(metadata["name"]))
                    annotations = (existing.get("metadata") or {}).get("annotations") or {}
                    if annotations.get(marker) == request_id:
                        return existing
                ambiguous = ambiguous or is_transport(exc)
                raise
            except OSError as exc:
                ambiguous = ambiguous or is_transport(exc)
                raise

        await self._call_with_backoff(create)

    async def _registry_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._registry_pool, functools.partial(fn, *args, **kwargs)
        )

    def _labels(self, spec: LaunchSpec, role: str) -> dict[str, str]:
        out = k8sspec.labels(spec, role)
        admin = self._admin_runs.get(spec.attempt_id)
        if admin:
            out[k8sspec.LABEL_ADMIN] = admin
            out[k8sspec.LABEL_HARNESS] = spec.harness
        return out

    def _limits(self, spec: LaunchSpec) -> Limits:
        return k8sspec.limits_from_policy(spec.policy)

    def _fallback_shape(self) -> Limits:
        """hades #478: the Limits derived from the active policy, or the policy
        store's default. Used by the capacity source string when no quota exists."""
        return k8sspec.limits_from_policy(self._policy)

    def set_policy(self, policy: dict[str, Any]) -> None:
        """hades #478: record the active delivery policy so the quota read can use it.

        The policy document is set by the supervisor from the current execution's
        policy_snapshot; _read_quota uses it to derive the per-attempt resource shape
        when no launch has happened yet, so the quota is never overstated.
        """
        self._policy = policy

    def _image_allowlist(self, spec: LaunchSpec) -> list[str]:
        return [
            str(p)
            for p in (spec.policy.get("images", {}).get("allowlist") or DEFAULT_IMAGE_ALLOWLIST)
        ] + list(self.config.extra_image_allowlist)

    # ----- images ------------------------------------------------------

    async def _resolve_image(self, spec: LaunchSpec, *, use_backoff: bool = False) -> str:
        """Resolve the tag to a digest through the registry and refuse what the policy
        or the adapter's tested range does not allow (07, 13, 26).

        Only the reference-to-digest-and-labels mapping is cached, because that is a
        fact about the registry. Whether *this* attempt may run it is a question about
        this attempt's policy and is asked again on every launch: the same image under
        a different policy has to be refused, and a cache hit must not be a way past
        that."""
        cached = self._images.get(spec.image)
        if cached is None:
            await self._load_pull_auths(use_backoff=use_backoff)
            try:
                cached = await self._registry_call(self.registry.resolve, spec.image)
            except RegistryError as exc:
                raise ProviderError(f"image {spec.image!r} is not available: {exc}") from exc
            self._images[spec.image] = cached
        if not image_allowed(spec.image, self._image_allowlist(spec)):
            raise ProviderError(f"image {spec.image!r} is outside the policy allowlist")
        check = check_image_version(self.harnesses, spec.harness, cached.labels)
        if not check.ok:
            raise HarnessRefusedError(f"refusing to launch: {check.detail}")
        return cached.reference

    async def _load_pull_auths(self, *, use_backoff: bool = False) -> None:
        """The registry credential is the cluster's own image pull Secret, read once.

        Nothing about it is configuration: the deployment already has to give the
        kubelet a pull secret, and reading the same one is what keeps Crucible from
        holding a second copy of a registry password (12)."""
        if self._pull_auths_loaded or not self.config.image_pull_secret:
            return
        self._pull_auths_loaded = True
        auths = getattr(self.registry, "auths", None)
        if auths is None or not isinstance(auths, dict):
            return
        try:
            call = self._call_with_backoff if use_backoff else self._call
            body = await call(self.client.get, "secrets", self.config.image_pull_secret)
        except KubernetesApiError as exc:
            log.warning("image pull secret unreadable", extra={"error": str(exc)})
            return
        raw = (body.get("data") or {}).get(".dockerconfigjson")
        if not raw:
            return
        with contextlib.suppress(RegistryError, ValueError):
            auths.update(auths_from_dockerconfigjson(base64.b64decode(str(raw))))

    # ----- the readiness probe (26) ------------------------------------

    def reload_settings(self) -> None:
        """Read the runtime settings back on the next launch or status call. The admin
        services call this after an edit; the transaction commits before either runs."""
        self._settings_read_at = None

    def apply_settings(self, document: Mapping[str, Any] | None, endpoint_url: str | None) -> None:
        """Take the `kubernetes.egress` document (None: the settings file's) and the
        enabled local endpoint. A change forgets the readiness probe, because what the
        canary proved was proved under the old rules."""
        egress = self._file_egress
        if document is not None:
            try:
                egress = parse_cluster_egress(document, protected_namespaces=self._protected())
            except ValueError as exc:
                # What is in force stays in force; the endpoint URL is still followed.
                log.error("the kubernetes.egress setting is refused: %s", exc)
                egress = self.config.egress
        updated = replace(self.config, egress=egress, local_endpoint_url=endpoint_url or "")
        if updated != self.config:
            self.config = updated
            self.probe = None

    def _protected(self) -> tuple[str, ...]:
        return tuple(n for n in (self.config.namespace, self.config.control_namespace) if n)

    async def _refresh_settings(self) -> None:
        if self._settings_source is None:
            return
        now = time.monotonic()
        if (
            self._settings_read_at is not None
            and now - self._settings_read_at < self.config.settings_refresh_seconds
        ):
            return
        self._settings_read_at = now
        try:
            document, endpoint_url = await self._call(self._settings_source)
        except Exception as exc:  # the settings file's values stay in force
            log.warning("the kubernetes runtime settings are unreadable: %s", exc)
            return
        self.apply_settings(document, endpoint_url)
        if self._timeouts_source is not None:
            try:
                timeouts = await self._call(self._timeouts_source)
            except Exception as exc:  # the value in force stays in force
                log.warning("the kubernetes.timeouts setting is unreadable: %s", exc)
                return
            self.apply_timeouts(timeouts)

    def apply_timeouts(self, document: Mapping[str, Any] | None) -> None:
        """Take the `kubernetes.timeouts` document (None: the settings file's value).
        A timeout is not a rule a canary proves, so the readiness probe stands."""
        seconds = self._file_role_timeout
        api_retry = self._file_api_retry
        if document is not None:
            try:
                checked = parse_role_timeouts(document)
                seconds = checked["role_timeout_seconds"]
                api_retry = checked.get("api_retry_seconds", api_retry)
            except ValueError as exc:
                log.error("the kubernetes.timeouts setting is refused: %s", exc)
                return
        if (
            seconds != self.config.role_timeout_seconds
            or api_retry != self.config.api_retry_seconds
        ):
            self.config = replace(
                self.config,
                role_timeout_seconds=seconds,
                api_retry_seconds=api_retry,
            )

    @staticmethod
    def _probe_is_settled(probe: NamespaceProbe) -> bool:
        """Whether `probe` needs no retry: `passed` on its own is not enough, because a
        local endpoint that failed or could not be told (crucible#110) no longer fails
        `passed`, and keeping that stale answer would refuse every Hermes launch until a
        settings change, never re-checking a gateway that came back. A problem with the
        endpoint always leaves `local_endpoint_detail` set; its absence means the
        endpoint is reachable or none is configured, either of which is settled."""
        return probe.passed and probe.local_endpoint_detail is None

    async def ensure_ready(self) -> NamespaceProbe:
        """Probe the namespace once, and keep the answer. A failed probe, or one whose
        local endpoint result is not settled, is re-run on the next call: lab-admin
        fixing the CNI, or the endpoint coming back, must not need a Crucible restart.

        Within the bounded window after `prepare` succeeds, a second call returns the
        cached result so that prepare and launch of the same attempt do not run the
        canary twice (issue 160).
        """
        await self._refresh_settings()
        if self.probe is not None and self._probe_is_settled(self.probe):
            return self.probe
        # Cache hit for an unsettled result: we are still within the same
        # attempt (prepare set the flag, launch has not cleared it yet).
        if (
            self.probe is not None
            and not self._probe_is_settled(self.probe)
            and self._probe_cached_for_launch
        ):
            return self.probe
        async with self._probe_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock()):
            if self.probe is not None and self._probe_is_settled(self.probe):
                return self.probe
            if (
                self.probe is not None
                and not self._probe_is_settled(self.probe)
                and self._probe_cached_for_launch
            ):
                return self.probe
            # A canary proves the rules it ran under. If a refresh changed them while it
            # ran, its answer is about rules no longer in force and is not kept.
            for _ in range(3):
                proved_under = self.config
                probe = await self._run_probe()
                if self.config is proved_under:
                    self.probe = probe
                    if not self._probe_is_settled(probe):
                        self._probe_unsettled_at = time.monotonic()
                    return probe
            self.probe = NamespaceProbe(
                False,
                False,
                None,
                "the egress settings changed while the canary ran; it runs again next time",
                checked=False,
            )
            return self.probe

    async def _run_probe(self) -> NamespaceProbe:
        """Two canary Pods, one after the other.

        The first runs under the namespace's own rules and nothing else, which is what
        a role with no egress gets: it must fail to reach the API server, and it reads
        the pod PID limit. A canary with a policy of its own would be isolated by that
        policy and could not tell a namespace with no default deny from one with it.

        The second runs under the rules a worker gets (crucible#91): cluster DNS and the
        enabled local endpoint, and nothing else. It must resolve a cluster name and
        connect to the endpoint, and still fail to reach the API server."""
        image = self._probe_image()
        if not image:
            return NamespaceProbe(
                False, False, None, "no image is configured to run the canary with", checked=False
            )
        namespace_run = await self._run_canary(image, scope="namespace")
        if isinstance(namespace_run, NamespaceProbe):
            return namespace_run
        namespace_log, namespace_node = namespace_run
        endpoint_url = self.config.local_endpoint_url
        endpoint_problem: str | None = None
        try:
            plan = await self._canary_endpoint_plan(endpoint_url)
        except (ProviderError, SpecError) as exc:
            # A local endpoint no rule can permit is the endpoint's failure, not the
            # namespace's (crucible#110): the worker rules canary still runs without it,
            # so DNS, default deny and the PID limit keep gating every launch, and the
            # endpoint refuses only the launches routed to it.
            endpoint_problem = (
                f"local endpoint check failed: no rule can permit it ({exc}); this blocks "
                "only launches routed to a local endpoint"
            )
            endpoint_url = ""
            plan = EgressPlan()
        rules_run = await self._run_canary(
            image, scope="worker", plan=plan, endpoint_url=endpoint_url
        )
        if isinstance(rules_run, NamespaceProbe):
            return rules_run
        rules_log, _rules_node = rules_run
        probe = _read_probe(namespace_log, rules_log, override=self.config.pod_pid_limit_override)
        if endpoint_problem is None:
            return replace(probe, canary_node=namespace_node)
        return replace(
            probe,
            detail=endpoint_problem if probe.passed else f"{probe.detail}; {endpoint_problem}",
            local_endpoint_reachable=False,
            local_endpoint_detail=endpoint_problem,
            canary_node=namespace_node,
        )

    async def _canary_endpoint_plan(self, endpoint_url: str) -> EgressPlan:
        """The worker rules canary's plan: cluster DNS and the configured local endpoint.
        Everything that can refuse the endpoint is checked here, before any object is
        created, so a failure raised from here is always the endpoint's own."""
        plan = await self._resolve_plan(self._local_endpoint_plan(EgressPlan(), endpoint_url))
        if plan.endpoint_selector is not None:
            k8sspec.check_selector(
                plan.endpoint_selector,
                what="local endpoint",
                protected_namespaces=self._protected(),
            )
        return plan

    async def _run_canary(
        self,
        image: str,
        *,
        scope: Literal["namespace", "worker"],
        plan: EgressPlan | None = None,
        endpoint_url: str = "",
    ) -> tuple[str, str] | NamespaceProbe:
        """Run one canary Pod to its end and return its log, or the probe that says why
        it could not run. `plan` is its own NetworkPolicy; None runs it under the
        namespace's rules alone."""
        canary_id = new_id()
        name = f"crucible-canary-{'ns' if scope == 'namespace' else 'rules'}-{canary_id.lower()}"
        object_labels = {
            k8sspec.LABEL_ROLE: k8sspec.ROLE_CANARY,
            k8sspec.LABEL_OWNER: "crucible",
            k8sspec.LABEL_CANARY: canary_id,
        }
        policy_name: str | None = None
        if plan is not None:
            policy_name = k8sspec.object_name("np-canary", canary_id)
            try:
                policy = self._policy_body(
                    policy_name,
                    object_labels,
                    canary_id,
                    k8sspec.ROLE_CANARY,
                    plan,
                    pod_selector={
                        k8sspec.LABEL_CANARY: canary_id,
                        k8sspec.LABEL_ROLE: k8sspec.ROLE_CANARY,
                    },
                )
            except SpecError as exc:
                # The worker egress rules themselves cannot be written (a DNS selector
                # that names a protected namespace, say): no worker could run under
                # them, so the whole probe fails, whatever the route.
                return NamespaceProbe(
                    False, False, None, f"the worker egress rules cannot be written: {exc}", False
                )
        limits = k8sspec.canary_limits(
            cpu_millicores=self.config.canary_cpu_millicores,
            memory=self.config.canary_memory,
        )
        pod = k8sspec.bare_pod(
            name=name,
            namespace=self.config.namespace,
            object_labels=object_labels,
            pod=k8sspec.pod_spec(
                PodRequest(
                    role=k8sspec.ROLE_CANARY,
                    image=image,
                    command=["sh", "-c", _CANARY_SCRIPT],
                    limits=limits,
                    env={
                        "CRUCIBLE_CANARY_SCOPE": scope,
                        "CRUCIBLE_CANARY_DNS_NAME": CANARY_DNS_NAME,
                        **({"CRUCIBLE_CANARY_ENDPOINT_URL": endpoint_url} if endpoint_url else {}),
                    },
                    mounts=k8sspec.base_mounts(),
                    volumes=k8sspec.base_volumes(limits),
                    service_account=self.config.service_account,
                    image_pull_secret=self.config.image_pull_secret,
                )
            ),
        )
        if policy_name is not None:
            try:
                await self._create_with_backoff("networkpolicies", policy)
            except KubernetesApiError as exc:
                return _canary_failed(f"the canary NetworkPolicy was refused: {exc}", exc)
        try:
            await self._create_with_backoff("pods", pod)
        except KubernetesApiError as exc:
            if policy_name is not None:
                with contextlib.suppress(KubernetesApiError):
                    await self._call(self.client.delete, "networkpolicies", policy_name)
            return _canary_failed(f"the canary Pod was refused: {exc}", exc)
        try:
            phase = await self._await_pod(name, timeout=self.config.launch_timeout_seconds)
            if phase is None:
                return NamespaceProbe(
                    False, False, None, "the canary Pod never reached a terminal phase", False
                )
            # The probe parser consumes `crucible-canary.*` keys at column one. Normal
            # worker log pulls need timestamps for resume, but the one-shot canary does
            # not, and a real API server prefixes every line when timestamps are left
            # enabled.
            try:
                body = await self._call_with_backoff(
                    self.client.pod_log,
                    name,
                    container=k8sspec.CONTAINER_NAME,
                    timestamps=False,
                )
            except KubernetesApiError as exc:
                return _canary_failed(f"the canary's log could not be read: {exc}", exc)
            log = b"".join(frame.payload for frame in body).decode("utf-8", "replace")
            pod = await self._call(self.client.get, "pods", name)
            return log, pod["spec"].get("nodeName", "")
        finally:
            with contextlib.suppress(KubernetesApiError):
                await self._call(self.client.delete, "pods", name, grace_period_seconds=0)
            with contextlib.suppress(ProviderError):
                await self._await_pod_gone(name)
            if policy_name is not None:
                with contextlib.suppress(KubernetesApiError):
                    await self._call(self.client.delete, "networkpolicies", policy_name)

    def _probe_image(self) -> str:
        """What the readiness canary runs: an image an attempt already resolved, else the
        one the deployment named, else the first configured repository.

        The last of those is a bare repository, which a kubelet reads as `:latest`, so a
        registry without that tag leaves the canary in ImagePullBackOff until the launch
        timeout and the status page reporting the namespace as not ready for a reason
        that is not about the namespace. `probe_image` is what a deployment says instead
        (C9)."""
        for image in self._images.values():
            return image.reference
        if self.config.probe_image:
            return self.config.probe_image
        for repository in self.config.image_repositories:
            return repository
        return ""

    # ----- contract ----------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        """26: `shared_disk` is false, because nothing of a workspace is ever visible to
        the Crucible process; what comes back comes back through a reader Pod."""
        return ProviderCapabilities(
            isolation=IsolationLevel.POD,
            network_control=True,
            resource_limits=True,
            shared_disk=False,
            supports_harnesses=frozenset(self.harnesses.names()),
            max_concurrency=self._quota_concurrency or self.config.max_concurrency,
        )

    async def credential_available(self, harness: str) -> bool:
        """Whether the harness's Secret will be mounted. A required credential always
        is, and its seeding refuses a Secret that is missing or empty with the reason.
        An optional one (Hermes) is mounted when its Secret holds the declared file.

        The Secret's name is the configured one or `crucible-harness-<harness>`, which
        is the name the service creates it under (ADR 0015); a mapping is no longer
        what makes a harness have a credential."""
        adapter = self.harnesses.get(harness)
        credential = adapter.credential_spec() if adapter is not None else None
        if credential is None:
            return False
        if credential.required_for_launch:
            return True
        secret_name = self.config.credential_secret_name(credential.harness)
        try:
            source = await self._call(self.client.get, "secrets", secret_name)
        except KubernetesUnavailableError:
            raise
        except KubernetesApiError as exc:
            if exc.status == 404:
                return False
            raise HarnessRefusedError(
                f"refusing to launch: the credential Secret {secret_name!r} for harness "
                f"{harness!r} is not readable in {self.config.namespace} ({exc.status})"
            ) from exc
        return _has_declared_auth_file(credential, source)

    async def probe_checks(
        self,
        spec: LaunchSpec,
        checks: Sequence[dict[str, Any]],
        checkout_token: InstallationToken | None = None,
        cancelled: CancelCheck | None = None,
    ) -> tuple[VerificationRun, ...] | None:
        """A disposable checkout of base_ref, before any preparer or worker."""
        await _stop_if_cancelled(cancelled, "before the gate probe")
        await self._require_ready(spec)
        image = await self._resolve_image(spec)
        role = "gate-probe"
        command_timeout = max(
            1,
            (effective_command_timeout_ms(spec.policy, spec.contract, spec.timeout_seconds) + 999)
            // 1000,
        )
        timeout = max(
            1,
            min(
                spec.timeout_seconds,
                self.config.prepare_timeout_seconds + command_timeout * len(checks),
            ),
        )
        url = spec.repository_url or str(spec.contract.get("repository", {}).get("url", ""))
        if checkout_token is not None:
            workspace.require_checkout_url(url, self.config.credential_host)
        token_name = (
            k8sspec.object_name("checkout", spec.attempt_id) if checkout_token is not None else None
        )
        token_mounts, token_volumes = self._checkout_token_mounts(token_name)
        plan = self._egress_plan(spec, k8sspec.ROLE_WORKER)
        checkout = self._checkout_plan(spec, k8sspec.ROLE_PREPARER, checkout_token is not None)
        # The same enforced policy machinery as the worker, with checkout egress
        # needed to fetch base_ref. There is no credential or report volume.
        plan = replace(
            plan,
            hosts=tuple(sorted(set(plan.hosts) | set(checkout.hosts))),
            endpoints=tuple(sorted(set(plan.endpoints) | set(checkout.endpoints))),
        )
        checkout_mount = Mount("probe-work", WORK_MOUNT)
        init_mounts: list[Mount] = [checkout_mount, *token_mounts]
        volumes: list[dict[str, Any]] = [
            {"name": "probe-work", "emptyDir": {}},
            *token_volumes,
        ]
        # File origins in the kind tier live on this read-only claim.
        if spec.repository_url.startswith("file:///crucible/cache/") and self.config.cache_claim:
            init_mounts.append(Mount("cache", k8sspec.CACHE_MOUNT, read_only=True))
            volumes.append(
                {
                    "name": "cache",
                    "persistentVolumeClaim": {
                        "claimName": self.config.cache_claim,
                        "readOnly": True,
                    },
                }
            )
        output: list[str] = []
        interrupted = False
        try:
            if checkout_token is not None:
                if not await self._delete_checkout_secret(spec.attempt_id):
                    raise ProviderError(
                        f"the checkout token Secret {token_name!r} left by an earlier try "
                        "could not be removed"
                    )
                await self._create_with_backoff(
                    "secrets",
                    k8sspec.secret(
                        name=str(token_name),
                        namespace=self.config.namespace,
                        object_labels=self._labels(spec, role),
                        data={CHECKOUT_TOKEN_KEY: checkout_token.reveal().encode("utf-8")},
                    ),
                )
            code = await self._run_role_job(
                spec,
                role=role,
                image=image,
                script=scripts.gate_probe_script(
                    f"{WORK_MOUNT}/repo",
                    list(checks),
                    command_timeout,
                ),
                mounts=[checkout_mount],
                volumes=volumes,
                init_containers=[
                    {
                        "name": "checkout",
                        "image": image,
                        "command": [
                            "sh",
                            "-c",
                            scripts.gate_probe_checkout_script(
                                spec.repository_url,
                                spec.contract["repository"]["base_ref"],
                                f"{WORK_MOUNT}/repo",
                                checkout_token="file" if checkout_token is not None else None,
                                credential_host=self.config.credential_host,
                            ),
                        ],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "resources": {
                            "limits": {
                                "cpu": self._limits(spec).cpu,
                                "memory": self._limits(spec).memory,
                                "ephemeral-storage": self._limits(spec).ephemeral_storage,
                            },
                            "requests": {
                                "cpu": self._limits(spec).cpu_request,
                                "memory": self._limits(spec).memory_request,
                            },
                        },
                        "volumeMounts": [
                            {"name": "tmp", "mountPath": "/tmp"},
                            *[
                                {
                                    "name": mount.name,
                                    "mountPath": mount.path,
                                    "readOnly": mount.read_only,
                                }
                                for mount in init_mounts
                            ],
                        ],
                    }
                ],
                limits=self._limits(spec),
                timeout=timeout,
                plan=plan,
                cancelled=cancelled,
                log_output=output,
                adopt_existing=True,
                preserve_on_cancel=True,
            )
        except asyncio.CancelledError:
            interrupted = True
            raise
        except KubernetesApiError as exc:
            if _quota_refused(exc):
                raise LaunchWaitError(
                    f"the namespace quota has no room for the gate probe: {exc}"
                ) from exc
            raise ProviderError(f"gate probe Job failed: {exc}") from exc
        finally:
            if (
                checkout_token is not None
                and not interrupted
                and not await self._delete_checkout_secret(spec.attempt_id)
            ):
                raise ProviderError(
                    f"the checkout token Secret {token_name!r} could not be deleted after "
                    "the gate probe"
                )
        if code != 0:
            detail = self.role_errors.get((role, spec.attempt_id), ("", False))[0]
            if code == JOB_API_ERROR and _names_quota(detail):
                # hades #423: a probe the quota refused is retried on a later tick, not
                # recorded as a check that cannot run.
                raise LaunchWaitError(
                    f"the namespace quota has no room for the gate probe: {detail}"
                )
            raise ProviderError(f"gate probe Job exit {code}: {detail}")
        rows: list[VerificationRun] = []
        for line in "".join(output).splitlines():
            try:
                result = json.loads(line)
            except ValueError:
                continue
            if not isinstance(result, dict) or not {"id", "command", "exit"} <= result.keys():
                continue
            if not isinstance(result["exit"], int):
                raise ProviderError("gate probe returned an invalid exit")
            rows.append(
                VerificationRun(
                    id=result["id"],
                    command=result["command"],
                    exit_code=result["exit"],
                    expect_exit=next(
                        int(check.get("expect_exit", 0))
                        for check in checks
                        if check["id"] == result["id"]
                    ),
                    log_tail=str(result.get("detail", "")),
                )
            )
        return tuple(rows)

    async def gate_probe_exists(self, attempt_id: str) -> bool:
        name = k8sspec.object_name("gate-probe", attempt_id)
        try:
            await self._call(self.client.get, "jobs", name)
        except KubernetesApiError as exc:
            if exc.status == 404:
                return False
            raise ProviderError(f"could not inspect gate probe Job {name!r}: {exc}") from exc
        return True

    async def prepare(
        self,
        spec: LaunchSpec,
        checkout_token: InstallationToken | None = None,
        cancelled: CancelCheck | None = None,
    ) -> Workspace:
        try:
            return await self._prepare_attempt(spec, checkout_token, cancelled)
        except LaunchWaitError:
            raise
        except KubernetesApiError as exc:
            if _quota_refused(exc):
                # hades #370: every create the preparation makes (the workspace claim,
                # the identity ConfigMap, the credential and checkout Secrets, the
                # preparer's NetworkPolicy and Job) counts against some quota resource,
                # and a 403 naming the quota on any of them is a wait for room, never
                # the attempt's failure. hades #423 made the claim and the preparer Job
                # wait; this is every other quota resource. The API server's words are
                # the reason the supervisor records.
                raise LaunchWaitError(
                    f"the namespace quota has no room for the attempt's objects: {exc}"
                ) from exc
            raise

    async def _prepare_attempt(
        self,
        spec: LaunchSpec,
        checkout_token: InstallationToken | None,
        cancelled: CancelCheck | None,
    ) -> Workspace:
        await _stop_if_cancelled(cancelled, "before the prepare")
        self._refuse_dead_codex(spec)
        # 26: the preparer renders its own egress policy (the DNS selector included),
        # and the supervisor calls prepare() before launch(). Refresh here too, or a
        # stale seed's policy is rendered and launch()'s own refresh is never reached.
        # 160: clear any stale flag from a prior attempt.
        self._probe_cached_for_launch = False
        await self._refresh_settings()
        repository = spec.contract.get("repository", {})
        url = spec.repository_url or str(repository.get("url", ""))
        if not url:
            raise ProviderError("the contract names no repository url")
        if checkout_token is not None:
            workspace.require_checkout_url(url, self.config.credential_host)
        base_ref = str(repository.get("base_ref", "main"))
        work_branch = str(repository.get("work_branch") or f"crucible/{spec.external_id}")
        resolved = await self._resolve_image(spec, use_backoff=True)
        limits = self._limits(spec)
        # 26, issue 59: the preparer is a Pod with GitHub egress and the per-attempt
        # Secret is a credential copy, so neither is made in a namespace whose egress
        # enforcement and PID limit are unproven. The image is resolved first because
        # the canary runs the image an attempt resolved when no probe image is named.
        await self._require_ready(spec)
        # 160: prepare has already run the canary; keep the result so that launch
        # (which also calls _require_ready) does not re-run it.
        self._probe_cached_for_launch = True

        # 26: the PVC, the ConfigMap and the per-attempt Secret, then the preparer Job.
        await self._delete_attempt_objects(spec.attempt_id)
        try:
            await self._create_with_backoff(
                "persistentvolumeclaims",
                k8sspec.workspace_claim(
                    name=k8sspec.object_name("ws", spec.attempt_id),
                    namespace=self.config.namespace,
                    object_labels=self._labels(spec, k8sspec.ROLE_WORKER),
                    size=self.config.workspace_size,
                    storage_class=self.config.storage_class,
                ),
            )
        except KubernetesApiError as exc:
            if _quota_refused(exc):
                # hades #423: a `persistentvolumeclaims` quota with no room is a wait.
                raise LaunchWaitError(
                    f"the namespace quota has no room for the workspace claim: {exc}"
                ) from exc
            raise
        bundle, identity_paths, identity_sha = await asyncio.to_thread(
            _render_identity, spec, work_branch, self.harnesses
        )
        await self._create_with_backoff(
            "configmaps",
            k8sspec.config_map(
                name=k8sspec.object_name("identity", spec.attempt_id),
                namespace=self.config.namespace,
                object_labels=self._labels(spec, k8sspec.ROLE_WORKER),
                data=bundle,
                annotations={ANNOTATION_IDENTITY_PATHS: json.dumps(identity_paths, sort_keys=True)},
            ),
        )
        copy = self._credential_copy(spec)
        if copy is not None:
            await self._seed_credential(spec, copy, use_backoff=True)
            self._seeded[spec.attempt_id] = dict(copy.seeded)
        try:
            return await self._prepare_checkout(
                spec,
                url=url,
                base_ref=base_ref,
                work_branch=work_branch,
                resolved=resolved,
                limits=limits,
                identity_sha=identity_sha,
                checkout_token=checkout_token,
                cancelled=cancelled,
            )
        except BaseException:
            # 12: the copy is removed on *every* path, not only the clean one. Every
            # failure past this point (a preparer Job that failed, a HEAD it never
            # produced, a reader Pod that never became ready) leaves an attempt the
            # supervisor will never collect or clean up, so the seeded Secret and the
            # writable copy go now rather than waiting for a retention sweep.
            if copy is not None:
                with contextlib.suppress(Exception):
                    await self._remove_credential(spec, copy)
            raise

    async def _prepare_checkout(
        self,
        spec: LaunchSpec,
        *,
        url: str,
        base_ref: str,
        work_branch: str,
        resolved: str,
        limits: Limits,
        identity_sha: str,
        checkout_token: InstallationToken | None = None,
        cancelled: CancelCheck | None = None,
    ) -> Workspace:
        """ADR 0019: a private repository's token is a per-attempt Secret mounted into
        the cache refresher and the preparer, the two Pods that talk to the remote, and
        into no other Pod. It is deleted as soon as the preparer's Pod is gone, on every
        path, before `launch` ever renders a worker."""
        if checkout_token is None:
            return await self._run_preparer(
                spec,
                url=url,
                base_ref=base_ref,
                work_branch=work_branch,
                resolved=resolved,
                limits=limits,
                identity_sha=identity_sha,
                token=None,
                cancelled=cancelled,
            )
        name = k8sspec.object_name("checkout", spec.attempt_id)
        try:
            # A Secret of this name can only be a leftover of an earlier try of this
            # attempt, holding a token already revoked; the create must not keep it.
            if not await self._delete_checkout_secret(spec.attempt_id):
                raise ProviderError(
                    f"the checkout token Secret {name!r} left by an earlier try could not "
                    "be removed"
                )
            await self._create_with_backoff(
                "secrets",
                k8sspec.secret(
                    name=name,
                    namespace=self.config.namespace,
                    object_labels=self._labels(spec, k8sspec.ROLE_PREPARER),
                    data={CHECKOUT_TOKEN_KEY: checkout_token.reveal().encode("utf-8")},
                ),
            )
            workspace_ready = await self._run_preparer(
                spec,
                url=url,
                base_ref=base_ref,
                work_branch=work_branch,
                resolved=resolved,
                limits=limits,
                identity_sha=identity_sha,
                token=name,
                cancelled=cancelled,
            )
        except BaseException:
            # The failure that got here is the one reported; a deletion that fails as
            # well is logged, and the retention sweep removes the object later.
            await self._delete_checkout_secret(spec.attempt_id)
            raise
        if not await self._delete_checkout_secret(spec.attempt_id):
            # The worker is never launched beside it. The token in it is revoked when
            # this returns, and `discard`, `cleanup` and the retention sweep retry.
            raise ProviderError(
                f"the checkout token Secret {name!r} could not be deleted after the "
                "preparation step, so no worker is launched beside it"
            )
        return workspace_ready

    async def _delete_checkout_secret(self, attempt_id: str) -> bool:
        """Remove the checkout token Secret; a missing one is already the goal."""
        return await self._delete_token_secret(
            k8sspec.object_name("checkout", attempt_id), what="checkout"
        )

    async def _delete_token_secret(self, name: str, *, what: str) -> bool:
        """Remove a token Secret (the checkout's, ADR 0019, or a push's, 23); a missing
        one is already the goal. True when it is gone. Never raises: it runs on failure
        paths whose own error is the one to report."""
        for _ in range(2):
            try:
                await self._call(self.client.delete, "secrets", name)
                return True
            except KubernetesApiError as exc:
                if exc.status == 404:
                    return True
                log.warning(f"{what} token secret removal failed", extra={"error": str(exc)})
            except Exception as exc:
                log.warning(
                    f"{what} token secret removal failed",
                    extra={"error": type(exc).__name__},
                )
        return False

    def _checkout_token_mounts(self, token: str | None) -> tuple[list[Mount], list[dict[str, Any]]]:
        if token is None:
            return [], []
        volume = {
            "name": "checkout-token",
            "secret": {
                "secretName": token,
                "defaultMode": 0o400,
                "items": [{"key": CHECKOUT_TOKEN_KEY, "path": "token", "mode": 0o400}],
                "optional": False,
            },
        }
        return [Mount("checkout-token", scripts.TOKEN_MOUNT, read_only=True)], [volume]

    def _checkout_plan(self, spec: LaunchSpec, role: str, private: bool) -> EgressPlan:
        """The git roles' egress, plus the credential host when a private repository's
        token is answered for a host that is not github.com (ADR 0019)."""
        plan = self._egress_plan(spec, role)
        if not private or not (plan.hosts or plan.endpoints or plan.broad):
            return plan
        host, _, port = self.config.credential_host.lower().partition(":")
        if host in plan.hosts and port in ("", "443"):
            return plan
        destination = f"{host}:{port or '443'}"
        if destination in plan.endpoints:
            return plan
        return replace(plan, endpoints=(*plan.endpoints, destination))

    async def _run_preparer(
        self,
        spec: LaunchSpec,
        *,
        url: str,
        base_ref: str,
        work_branch: str,
        resolved: str,
        limits: Limits,
        identity_sha: str,
        token: str | None,
        cancelled: CancelCheck | None = None,
    ) -> Workspace:
        repository = spec.contract.get("repository", {})
        token_mounts, token_volumes = self._checkout_token_mounts(token)
        # The shim rule is the adapter's (06): Claude Code reads AGENTS.md only where the
        # project has no CLAUDE.md of its own, and the preparer needs to be told which.
        adapter = self.harnesses.require(spec.harness)
        cache_mounts: list[Mount] = []
        cache_volumes: list[dict[str, Any]] = []
        resume_mounts: list[Mount] = []
        resume_volumes: list[dict[str, Any]] = []
        resume_bundle = None
        if spec.resume_bundle_attempt_id:
            resume_bundle = f"{scripts.BUNDLE_MOUNT}/work_branch.bundle"
            resume_mounts.append(
                Mount(
                    "resume-bundle",
                    resume_bundle,
                    read_only=True,
                    sub_path="output/work_branch.bundle",
                )
            )
            resume_volumes.append(
                {
                    "name": "resume-bundle",
                    "persistentVolumeClaim": {
                        "claimName": k8sspec.object_name("ws", spec.resume_bundle_attempt_id),
                        "readOnly": True,
                    },
                }
            )
        cache_name: str | None = None
        gate: _CacheGate | None = None
        if self.config.use_reference_cache and self.config.cache_claim:
            cache_name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            gate = self._cache_gate(cache_name)
            if gate.refresh_due(time.monotonic()):
                # hades #189: a cancel is honoured before each step and while it runs.
                await _stop_if_cancelled(cancelled, "before the cache refresh")
                async with gate.writing():
                    await self._refresh_cache(
                        spec,
                        url=url,
                        cache_name=cache_name,
                        image=resolved,
                        limits=limits,
                        token=token,
                        cancelled=cancelled,
                    )
            # 26: the preparer reads the cache and never writes it. It is the one volume
            # every attempt shares, so an attempt's Pod that could write it could poison
            # every later checkout (#55). Read-only on the claim and on the mount.
            cache_mounts.append(Mount("cache", k8sspec.CACHE_MOUNT, read_only=True))
            cache_volumes.append(
                {
                    "name": "cache",
                    "persistentVolumeClaim": {
                        "claimName": self.config.cache_claim,
                        "readOnly": True,
                    },
                }
            )
        git_policy = spec.policy.get("git", {})
        from_remote_branch = spec.role == "correct" or bool(
            repository.get("resume_from_work_branch")
        )
        # hades #370: what a correction resumes from, so a preparer that cannot fetch it
        # names the source. The mounted bundle wins, as it does in the script.
        resume_source: str | None = None
        if resume_bundle is not None:
            resume_source = f"the sealed bundle of attempt {spec.resume_bundle_attempt_id}"
        elif from_remote_branch:
            resume_source = f"the remote work branch {work_branch!r}"
        await _stop_if_cancelled(cancelled, "before the preparer")
        preparer_log: list[str] = []
        async with gate.reading() if gate is not None else contextlib.nullcontext():
            exit_code = await self._run_role_job(
                spec,
                role=k8sspec.ROLE_PREPARER,
                use_backoff=True,
                image=resolved,
                script=scripts.preparer_script(
                    url=url,
                    base_ref=base_ref,
                    work_branch=work_branch,
                    from_remote_branch=from_remote_branch,
                    cache_name=cache_name,
                    author_name=str(git_policy.get("author_name", "crucible-worker")),
                    author_email=str(
                        git_policy.get("author_email", "crucible-worker@users.noreply.github.com")
                    ),
                    origin_placeholder=workspace.ORIGIN_PLACEHOLDER,
                    claude_md_wins=adapter.capabilities().claude_md_wins,
                    shims=workspace.SHIM_NAMES,
                    exclude_entries=workspace.EXCLUDE_ENTRIES,
                    identity_mount=IDENTITY_MOUNT,
                    resume_bundle=resume_bundle,
                    resume_bundle_head=spec.resume_bundle_head,
                    resume_bundle_sha256=spec.resume_bundle_sha256,
                    resume_bundle_ancestor=spec.resume_bundle_ancestor,
                    checkout_token="file" if token else None,
                    credential_host=self.config.credential_host,
                ),
                mounts=[
                    Mount("ws", WORK_MOUNT),
                    *cache_mounts,
                    *resume_mounts,
                    *token_mounts,
                ],
                volumes=[
                    self._claim_volume(spec.attempt_id),
                    *cache_volumes,
                    *resume_volumes,
                    *token_volumes,
                ],
                limits=limits,
                timeout=self.config.prepare_timeout_seconds,
                plan=self._checkout_plan(spec, k8sspec.ROLE_PREPARER, token is not None),
                cancelled=cancelled,
                stall_seconds=self.config.preparer_stall_seconds,
                keep_log=preparer_log,
            )
        if exit_code != 0:
            detail = redact(self.last_error.get(k8sspec.ROLE_PREPARER, ""))
            if exit_code == JOB_API_ERROR and _names_quota(detail):
                # hades #423: the preparer is a separate Pod the quota counts; one it
                # refused waits for room, as the probe and the worker do. The refusal
                # itself (the Job's FailedCreate event, or the API server's 403) is the
                # detail the supervisor records (hades #370).
                raise LaunchWaitError(f"the namespace quota has no room for the preparer: {detail}")
            raise self._prepare_failed(
                exit_code, detail, resume_source, redact("".join(preparer_log))
            )
        prepared = await self._read_files(
            spec,
            ["output/prepared-head.txt", "output/started-from.txt"],
            limits,
            use_backoff=True,
            collection=False,
        )
        head = (prepared.get("output/prepared-head.txt") or b"").decode("utf-8", "replace").strip()
        if not head:
            raise ProviderError("the preparer produced no HEAD")
        root = f"k8s://{self.config.namespace}/{k8sspec.object_name('ws', spec.attempt_id)}"
        return Workspace(
            attempt_id=spec.attempt_id,
            checkout_path=f"{root}/repo",
            identity_path=f"{root}/identity",
            report_path=f"{root}/report",
            output_path=f"{root}/output",
            identity_sha256=identity_sha,
            work_branch=work_branch,
            started_from=(prepared.get("output/started-from.txt") or b"")
            .decode("utf-8", "replace")
            .strip(),
        )

    def _prepare_failed(
        self, exit_code: int, detail: str, resume_source: str | None, log: str = ""
    ) -> PrepareFailedError:
        """hades #370: the error a preparer that ran and failed raises. The message
        carries the preparer's last lines, so the attempt's environment detail and the
        wake say what the clone said; the Pod's whole log (`log`, read uncut before the
        Job was deleted) rides as `output` for the attempt's evidence; and a correction
        names the source it was resuming from, so a bundle that is gone or a remote
        branch that could not be fetched is told apart from a clone of the base that
        failed.

        Every form begins with the words the message had before hades #370, "the
        preparer Job could not build the checkout", and names the cause in the
        parenthesis that used to hold only the exit code: a stall, a timeout and a Pod
        that never ran are told apart there, and what reads the message for those
        words (the kind tier's hades #191 test, an operator's search) still finds
        them."""
        if exit_code == JOB_STALLED:
            cause = "stalled"
        elif exit_code == JOB_TIMED_OUT:
            cause = "timed out"
        elif exit_code == JOB_API_ERROR:
            cause = "its Pod never ran"
        else:
            cause = f"exit {exit_code}"
        what = f"the preparer Job could not build the checkout ({cause})"
        if resume_source is not None:
            what = f"the correction resumes from {resume_source}, and {what}"
        return PrepareFailedError(
            f"{what}: {_last_lines(detail)}",
            # Only a preparer that ran has output to keep: an API error is Crucible's own
            # words, which the detail already carries. The whole log is kept when it
            # could be read; a ran-and-failed Pod whose log read failed keeps its tail.
            output=log or (detail if exit_code >= 0 or exit_code == JOB_STALLED else ""),
            exit_code=exit_code,
            resume_source=resume_source,
        )

    def _cache_gate(self, cache_name: str) -> _CacheGate:
        gates = self._cache_gates.setdefault(asyncio.get_running_loop(), {})
        return gates.setdefault(cache_name, _CacheGate())

    async def _refresh_cache(
        self,
        spec: LaunchSpec,
        *,
        url: str,
        cache_name: str,
        image: str,
        limits: Limits,
        token: str | None = None,
        cancelled: CancelCheck | None = None,
    ) -> None:
        """26: refresh the reference cache in a Job of its own, the only Pod that mounts
        it writable (#55). It carries no workspace, no identity bundle and no
        credential, only the cache and the git remote. A refresh that exits non-zero
        is logged, and the preparer clones from the remote, or from the mirror as it
        was: a stale or absent cache costs time, never correctness. A refresher whose
        Pod cannot be confirmed gone fails the prepare instead (`_run_role_job` raises),
        because that Pod may still hold the cache writable while a preparer reads it.

        A private repository's refresher also mounts the checkout token Secret (ADR
        0019), and fetches with it; a public one's carries no credential at all."""
        token_mounts, token_volumes = self._checkout_token_mounts(token)
        code = await self._run_role_job(
            spec,
            role=k8sspec.ROLE_CACHE_REFRESHER,
            use_backoff=True,
            image=image,
            script=scripts.cache_refresh_script(
                url=url,
                cache_name=cache_name,
                checkout_token="file" if token else None,
                credential_host=self.config.credential_host,
            ),
            mounts=[Mount("cache", k8sspec.CACHE_MOUNT), *token_mounts],
            volumes=[
                {
                    "name": "cache",
                    "persistentVolumeClaim": {"claimName": self.config.cache_claim},
                },
                *token_volumes,
            ],
            limits=limits,
            timeout=self.config.prepare_timeout_seconds,
            plan=self._checkout_plan(spec, k8sspec.ROLE_CACHE_REFRESHER, token is not None),
            cancelled=cancelled,
        )
        if code != 0:
            log.warning(
                "the reference cache refresh failed; the preparer clones without it",
                extra={
                    "exit_code": code,
                    "detail": redact(self.last_error.get(k8sspec.ROLE_CACHE_REFRESHER, "")),
                },
            )

    def _check_endpoint_ready(self, probe: NamespaceProbe, spec: LaunchSpec) -> None:
        """The operator, 2026-09-23: "a down provider should only block that provider."
        DNS, default-deny and the API-server result gate every launch through
        `probe.passed`; the local endpoint gates only the launch whose own route (05's
        `LaunchSpec.endpoint`, the routing policy's field) is `local`. Nothing to check
        (`config.local_endpoint_url` empty) is not this route's failure to report; once
        the probe has something to say about it, anything but a definite pass refuses,
        the same standard the probe itself uses."""
        if spec.endpoint != "local" or not self.config.local_endpoint_url:
            return
        if probe.local_endpoint_reachable is True:
            return
        raise HarnessRefusedError(
            "refusing to launch: the local endpoint check failed for this launch's route "
            f"({probe.local_endpoint_detail or probe.detail})"
        )

    async def _require_ready(self, spec: LaunchSpec) -> None:
        """26: a namespace whose egress enforcement or pod PID limit is not proven runs
        no Pod of an attempt and holds no copy of its credential. This is a refusal, not
        a retry: the next attempt would meet the same cluster."""
        probe = await self.ensure_ready()
        if not probe.passed:
            if probe.unavailable:
                raise ProviderUnavailableError(
                    f"the workers namespace could not be checked ({probe.detail})"
                )
            raise HarnessRefusedError(
                f"refusing to launch: the workers namespace is not ready ({probe.detail})"
            )
        self._check_endpoint_ready(probe, spec)

    def _refuse_dead_codex(self, spec: LaunchSpec) -> None:
        if spec.harness == "codex" and self._credential_dead():
            raise HarnessRefusedError(
                "refusing to launch: the Codex credential is dead; log in again"
            )

    async def launch(
        self, ws: Workspace, spec: LaunchSpec, cancelled: CancelCheck | None = None
    ) -> Handle:
        self._refuse_dead_codex(spec)
        await self._require_ready(spec)
        gated_under = self.config
        resolved = await self._resolve_image(spec, use_backoff=True)
        limits = self._limits(spec)
        self._last_limits = limits
        copy = self._credential_copy(spec)
        if copy is not None:
            copy.seeded.update(self._seeded.pop(spec.attempt_id, {}))
        plan = self._egress_plan(spec, k8sspec.ROLE_WORKER)
        policy_name: str | None = None
        identity_paths = await self._identity_paths(spec.attempt_id)
        cred_secret_name = k8sspec.object_name("cred", spec.attempt_id)
        try:
            credential_keys = await self._credential_keys(spec.attempt_id) if copy else []
        except KubernetesUnavailableError:
            # Could not look is not "not readable": no refusal from a hiccup.
            raise
        except KubernetesApiError as exc:
            raise HarnessRefusedError(
                f"refusing to launch: the credential Secret {cred_secret_name!r} for harness "
                f"{spec.harness!r} is not readable in {self.config.namespace} ({exc.status})"
            ) from exc
        if copy is not None and not credential_keys:
            if copy.spec.required_for_launch:
                raise HarnessRefusedError(
                    f"refusing to launch: the credential Secret {cred_secret_name!r} for harness "
                    f"{spec.harness!r} holds none of its copied auth files"
                )
            copy = None
        job_name = k8sspec.object_name("worker", spec.attempt_id)
        try:
            if self.config is not gated_under:
                # The egress settings changed after the gate: the worker's rules are the
                # new ones, so the canary proves them first (crucible#91).
                await self._require_ready(spec)
                plan = self._egress_plan(spec, k8sspec.ROLE_WORKER)
            # hades #189: the readiness gate and the image resolution above can take a
            # while; a cancel that landed during them creates nothing.
            await _stop_if_cancelled(cancelled, "before the worker was created")
            policy_name, plan = await self._apply_policy(
                spec, k8sspec.ROLE_WORKER, plan, use_backoff=True
            )
            body = k8sspec.job(
                name=job_name,
                namespace=self.config.namespace,
                object_labels=self._labels(spec, k8sspec.ROLE_WORKER),
                pod=self._worker_pod(
                    ws,
                    spec,
                    resolved=resolved,
                    limits=limits,
                    copy=copy,
                    identity_paths=identity_paths,
                    credential_keys=credential_keys,
                    host_aliases=k8sspec.host_aliases(plan),
                ),
                active_deadline_seconds=max(60, spec.timeout_seconds + limits.grace_seconds),
            )
            await self._create_with_backoff("jobs", body)
            refusal = await self._await_worker_pod(job_name)
        except (KubernetesApiError, SpecError) as exc:
            with contextlib.suppress(Exception):
                await self._call(self.client.delete, "jobs", job_name)
            if policy_name:
                with contextlib.suppress(Exception):
                    await self._call(self.client.delete, "networkpolicies", policy_name)
            # 12: the per-attempt Secret exists from `prepare`; a launch that never
            # started must not leave it behind for nothing to come back for.
            with contextlib.suppress(Exception):
                await self._delete_credential_secret(spec.attempt_id)
            if _quota_refused(exc):
                # hades #423: the API server itself refused the Job for the quota (a
                # `count/jobs.batch` limit). A wait, not a failure of the attempt.
                raise LaunchWaitError(
                    f"the namespace quota has no room for the worker: {exc}"
                ) from exc
            raise ProviderError(f"could not start the worker: {exc}") from exc
        if refusal is not None:
            # hades #423: the Job controller could not create the worker's Pod because
            # the namespace is full. Nothing ran. The Job goes, and the attempt waits
            # for room rather than ending with an exit class it never earned.
            with contextlib.suppress(Exception):
                await self._call(self.client.delete, "jobs", job_name)
            if policy_name:
                with contextlib.suppress(Exception):
                    await self._call(self.client.delete, "networkpolicies", policy_name)
            with contextlib.suppress(Exception):
                await self._delete_credential_secret(spec.attempt_id)
            with contextlib.suppress(Exception):
                await self._await_job_pods_gone(job_name)
            raise LaunchWaitError(f"the namespace quota has no room for the worker: {refusal}")
        self._launched[spec.attempt_id] = _Launched(
            job_name=job_name,
            spec=spec,
            image_digest=resolved,
            limits=limits,
            network_policy=policy_name,
            credential=copy,
            launched_at=time.monotonic(),
        )
        return Handle(
            provider=self.name,
            ref=job_name,
            attempt_id=spec.attempt_id,
            image_digest=resolved,
            name=job_name,
        )

    async def _await_worker_pod(self, job_name: str) -> str | None:
        """hades #423: give the Job controller a few polls to create the worker's Pod.
        Returns the quota's refusal when a `FailedCreate` names it in that window, None
        once the Job counts a Pod (`status.active`, or one already ended) or when nothing
        is known by the end of the window (`observe` takes it from there). The Job's
        status is read rather than the Pods listed, so the launch never consumes a look
        at the Pod itself. A look that fails is not an answer."""
        for _ in range(LAUNCH_POD_POLLS):
            try:
                job = await self._call(self.client.get, "jobs", job_name)
            except KubernetesApiError:
                return None
            status = job.get("status") or {}
            if any(int(status.get(key) or 0) for key in ("active", "succeeded", "failed")):
                return None
            refusal = await self._quota_refusal(job_name, job)
            if refusal is not None:
                return refusal
            await asyncio.sleep(self.config.poll_interval_seconds)
        return None

    async def observe(self, h: Handle) -> Observation:
        launched = self._launched.get(h.attempt_id)
        try:
            job = await self._call(self.client.get, "jobs", h.ref)
        except KubernetesApiError as exc:
            if exc.status != 404:
                raise ProviderError(f"reading the Job failed: {exc}") from exc
            job = {}
        try:
            pod = await self._pod_of(h.ref)
        except KubernetesApiError as exc:
            # Nothing is decided from a failed look. The supervisor logs this and asks
            # again on the next tick, which is what an attempt whose state could not be
            # read deserves.
            raise ProviderError(f"could not read the Pod of {h.ref}: {exc}") from exc
        if pod is None:
            if (
                launched is not None
                and job
                and not launched.pod_name
                and launched.exit_code is None
                and launched.terminated is None
            ):
                refusal = await self._quota_refusal(h.ref, job)
                if refusal is not None:
                    # hades #423: a full namespace is a wait, never a failure. `launch`
                    # catches the usual case; one seen here (the controller was slow to
                    # try) keeps the attempt pending while the controller retries the Pod,
                    # and the launch timeout counts from the last refusal, not the
                    # launch, so waiting for room is not spent as the Pod's own start
                    # time. Before this (the lab findings of 2026-09-29) it was a launch
                    # failure with the quota's words.
                    launched.launched_at = time.monotonic()
                    return Observation(
                        ObservationState.RUNNING,
                        detail=f"waiting for room in the namespace quota: {refusal}",
                    )
            return self._observation_without_pod(h, launched, job)
        status = pod.get("status") or {}
        phase = str(status.get("phase", ""))
        if launched is not None and not launched.pod_name:
            launched.pod_name = str((pod.get("metadata") or {}).get("name") or "")
            launched.node = str((pod.get("spec") or {}).get("nodeName") or "") or None
        if launched is not None and launched.limits_source != "pod":
            _observe_limits(launched, pod)
        if phase == "Failed" and str(status.get("reason", "")) in _LOST_REASONS:
            # 26: an evicted Pod, or one whose node is gone, is `lost`, not an exit.
            # Check lost reasons before inspecting the terminated container so that a
            # node-pressure eviction (Failed/Evicted with a terminated container) is
            # classified as lost rather than exiting (issue 133, FDY-0464).
            return Observation(ObservationState.LOST, detail=f"the Pod was {status.get('reason')}")
        terminated = _terminated_state(status)
        if terminated is not None:
            code = int(terminated.get("exitCode", -1))
            if launched is not None:
                launched.exit_code = code
            oom = str(terminated.get("reason", "")) == "OOMKilled"
            detail = str(terminated.get("reason", "")) or phase
            if detail in START_FAILURES:
                return await self._start_failure_observation(
                    pod, code, detail, str(terminated.get("message") or detail)
                )
            return Observation(
                ObservationState.EXITED,
                exit_code=code,
                detail=f"{detail}:oom_killed" if oom else detail,
                oom_killed=oom,
            )
        if phase == "Failed":
            # The Pod failed without the worker's own container producing an exit. The
            # init container that seeds a `rw-narrow` credential copy is the way this
            # happens in practice: it fails, the main container never starts, and only
            # the init status carries a terminated state. Reporting `running` here would
            # hang the attempt forever with nothing to classify, collect or clean up.
            init = _terminated_init(status)
            detail = str(status.get("message") or status.get("reason") or "")
            if init is not None:
                return Observation(
                    ObservationState.EXITED,
                    exit_code=70,
                    detail=(
                        f"the {init.get('containerName', 'init')} container failed before "
                        f"the worker started (exit {init.get('exitCode')}, "
                        f"{init.get('reason', '')}) {detail}".strip()
                    ),
                )
            return Observation(
                ObservationState.EXITED,
                exit_code=70,
                detail=f"the Pod failed before the worker started: {detail or phase}",
            )
        for entry in status.get("containerStatuses") or []:
            if entry.get("name") != k8sspec.CONTAINER_NAME:
                continue
            waiting = (entry.get("state") or {}).get("waiting") or {}
            reason = str(waiting.get("reason") or "")
            if reason in START_FAILURES and (
                reason not in {"ImagePullBackOff", "ErrImagePull"}
                or self._pending_too_long(launched)
            ):
                return await self._start_failure_observation(
                    pod, 70, reason, str(waiting.get("message") or reason)
                )
        if phase in _PENDING_PHASES and self._pending_too_long(launched):
            return self._pending_failure(pod)
        return Observation(ObservationState.RUNNING, detail=phase or "Pending")

    async def _start_failure_observation(
        self, pod: Mapping[str, Any], code: int, reason: str, message: str
    ) -> Observation:
        metadata = pod.get("metadata") or {}
        selector = f"involvedObject.uid={metadata.get('uid', '')}"
        try:
            events = await self._call(self.client.list_objects, "events", field_selector=selector)
        except KubernetesApiError as exc:
            events = [{"reason": "EventsUnavailable", "message": str(exc), "count": 1}]
        return Observation(
            ObservationState.EXITED,
            exit_code=code,
            detail=reason,
            never_started=True,
            container_message=message,
            pod_events=tuple(
                {
                    "reason": row.get("reason"),
                    "message": row.get("message"),
                    "count": row.get("count", 1),
                }
                for row in events
            ),
        )

    def _observation_without_pod(
        self, h: Handle, launched: _Launched | None, job: Mapping[str, Any]
    ) -> Observation:
        """A Job whose Pod is gone, or not created yet (26). A Pod that existed and
        then disappeared is lost; a Job that has never had one is pending until the
        launch timeout, then a launch failure. The Job controller taking a few seconds
        to create the Pod on a busy node is not a loss."""
        if launched is not None and launched.exit_code is not None:
            # The exit was already observed; the Pod being reaped afterwards is not a
            # second event.
            return Observation(
                ObservationState.EXITED, exit_code=launched.exit_code, detail="pod removed"
            )
        if launched is not None and launched.terminated is not None:
            code = DRAIN_EXIT_CODE if launched.terminated == "drain" else KILL_EXIT_CODE
            launched.exit_code = code
            return Observation(
                ObservationState.EXITED,
                exit_code=code,
                detail=f"the Pod was deleted by Crucible ({launched.terminated})",
            )
        for condition in (job.get("status") or {}).get("conditions") or []:
            if not isinstance(condition, dict) or str(condition.get("status")) != "True":
                continue
            if str(condition.get("type")) == "Complete":
                # The Job finished and its Pod was garbage collected afterwards (a node
                # drain, an operator, a TTL controller someone adds). A successful
                # attempt whose Pod was reaped is not a loss.
                return Observation(ObservationState.EXITED, exit_code=0, detail="the Job completed")
            if str(condition.get("reason")) == "DeadlineExceeded":
                # The Job's own deadline fired. Crucible drains before it (26), so this
                # is the cluster killing a worker Crucible had not classified yet.
                return Observation(
                    ObservationState.EXITED,
                    exit_code=KILL_EXIT_CODE,
                    detail="the Job deadline killed the Pod",
                )
            if str(condition.get("type")) == "Failed":
                # 103: `backoffLimit: 0` fails the Job the moment its one Pod does, so a
                # Pod gone before any poll saw it is still a Pod that existed, not a
                # Job that never got one.
                return Observation(
                    ObservationState.LOST, detail="the Job failed before Crucible saw a Pod"
                )
        if not job:
            return Observation(ObservationState.LOST, detail="the namespace has no such Job")
        if launched is not None and launched.pod_name:
            # 26: this attempt had a Pod and it is gone now, unlike the case below
            # where one was never created.
            return Observation(ObservationState.LOST, detail="the Job's Pod disappeared")
        if launched is not None and self._pending_too_long(launched):
            return Observation(
                ObservationState.EXITED,
                exit_code=70,
                detail="the Job controller never created a Pod",
            )
        if launched is not None:
            return Observation(ObservationState.RUNNING, detail="Pending")
        return Observation(ObservationState.LOST, detail="the Job has no Pod")

    def _pending_too_long(self, launched: _Launched | None) -> bool:
        if launched is None or not launched.launched_at:
            return False
        return time.monotonic() - launched.launched_at > self.config.launch_timeout_seconds

    def _pending_failure(self, pod: Mapping[str, Any] | None) -> Observation:
        """26: Pending past the launch timeout is a launch failure with the Pod's
        conditions as detail, not a stall.

        It is reported as exit 70, which 16 classifies as `environment`: the provider
        failed before the harness ran, the attempt retries if the policy allows it, and
        the supervisor tick keeps moving. Raising here instead would make one unschedulable
        Pod an exception on every tick for as long as it stayed unschedulable."""
        conditions = []
        for condition in ((pod or {}).get("status") or {}).get("conditions") or []:
            if isinstance(condition, dict):
                conditions.append(
                    f"{condition.get('type')}={condition.get('status')}"
                    f"({condition.get('reason') or ''}: {condition.get('message') or ''})"
                )
        for entry in ((pod or {}).get("status") or {}).get("containerStatuses") or []:
            waiting = (entry.get("state") or {}).get("waiting") if isinstance(entry, dict) else None
            if waiting:
                conditions.append(f"waiting({waiting.get('reason')}: {waiting.get('message')})")
        detail = "; ".join(conditions) or "the Pod did not start and reported no condition"
        return Observation(
            ObservationState.EXITED,
            exit_code=70,
            detail=f"the Pod stayed Pending past the launch timeout: {detail}",
        )

    async def logs(self, h: Handle, since: LogOffset) -> list[LogChunk]:
        try:
            pod = await self._pod_of(h.ref)
        except KubernetesApiError:
            return []
        if pod is None:
            return []
        name = str((pod.get("metadata") or {}).get("name") or "")
        limit = LOG_READ_LIMIT
        while True:
            try:
                frames = await self._call(
                    self.client.pod_log,
                    name,
                    container=k8sspec.CONTAINER_NAME,
                    since_time=since.timestamp,
                    limit_bytes=limit,
                )
            except KubernetesApiError as exc:
                if exc.status == 404:
                    return []
                raise ProviderError(f"log pull failed: {exc}") from exc
            payload = b"".join(frame.payload for frame in frames)
            # The kubelet can land short of `limitBytes` and still cut a line in half,
            # so byte-count equality against `limit` alone cannot tell a whole trailing
            # line from a cut one: a payload that does not end in a newline has an
            # incomplete trailing line whatever its length, and that line is read whole
            # next time. Filling the requested limit is still the only signal that a
            # larger read might find more data; a short response with an unfinished
            # last line, with nothing else new, is left for the next regular poll
            # instead of being retried larger, since a worker still writing that line
            # would otherwise be forced through the ceiling and skipped as if it were a
            # crowded second.
            truncated = len(payload) >= limit
            incomplete = bool(payload) and not payload.endswith(b"\n")
            whole = payload[: payload.rfind(b"\n") + 1] if incomplete else payload
            chunks = _chunks([LogFrame("stdout", whole)] if whole else [], since)
            if chunks or not truncated:
                return chunks
            if limit < LOG_READ_CEILING:
                limit = min(limit * 4, LOG_READ_CEILING)
                continue
            return _skip_crowded_second(payload, limit, since)

    async def activity(self, h: Handle, ws: Workspace) -> tuple[int, int, int] | None:
        """FDY-0140: the workspace is a claim, not a local path, so the supervisor's own
        walk sees nothing. The live worker is asked instead, over exec, never through a
        log. Any failure to ask is None: the stall clock is neither reset nor pushed."""
        try:
            pod = await self._pod_of(h.ref)
        except KubernetesApiError:
            return None
        if pod is None or str((pod.get("status") or {}).get("phase", "")) != "Running":
            return None
        name = str((pod.get("metadata") or {}).get("name") or "")
        try:
            result: ExecResult = await self._call(
                self.client.pod_exec,
                name,
                ["sh", "-c", scripts.ACTIVITY_SCRIPT],
                container=k8sspec.CONTAINER_NAME,
                # The walk stops itself; this bounds a stream that never ends, so one
                # slow Pod cannot hold the supervisor's tick for the others.
                timeout=scripts.ACTIVITY_WALK_SECONDS + 5,
                limit=ACTIVITY_READ_LIMIT,
            )
        except KubernetesApiError:
            return None
        if result.exit_code != 0:
            return None
        return scripts.parse_activity(result.stdout)

    async def probe_model_endpoint(self, endpoint_url: str) -> bool:
        return await probe_model_endpoint(endpoint_url)

    async def collect(
        self, h: Handle, ws: Workspace, spec: LaunchSpec | None = None
    ) -> CollectedOutputs:
        await self._refresh_settings()
        launched = self._launched.get(h.attempt_id)
        spec = spec or (launched.spec if launched else None)
        if spec is None:
            raise ProviderError("collect needs the launch spec and the provider has none")
        await self._clear_collection_pods(spec.attempt_id)
        # The stored launch spec is authoritative after a supervisor restart. An
        # adopted provider only has the live Pod shape until the supervisor supplies
        # this spec again for collection.
        limits = self._limits(spec)
        repository = spec.contract.get("repository", {})
        work_branch = ws.work_branch or str(
            repository.get("work_branch") or f"crucible/{spec.external_id}"
        )
        # 12: the credential copy is read back and removed before anything else runs.
        credential_sync = await self._sync_credential(h, spec, limits)
        stdout_tail, stderr_tail = await self._worker_tails(h)
        observation = await self.observe(h)
        if observation.never_started:
            return CollectedOutputs(
                report=None,
                report_raw=None,
                blocked_md=None,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                artifacts=(self._launch_evidence(spec, observation),),
                credential_sync=credential_sync,
            )
        adapter = self.harnesses.get(spec.harness)
        quota_checkpoint = bool(
            adapter is not None
            and observation.state is ObservationState.EXITED
            and adapter.classify_exit(
                ExitInfo(exit_code=observation.exit_code), stdout_tail, stderr_tail, None
            )
            is ExitClass.QUOTA_EXHAUSTED
        )
        collector_exit = await self._run_role_job(
            spec,
            role=k8sspec.ROLE_COLLECTOR,
            image=launched.image_digest if launched else spec.image,
            script=scripts.collector_script(
                base_ref=str(repository.get("base_ref", "main")),
                work_branch=work_branch,
                size_cap_bytes=self.config.report_size_cap_bytes,
                attempt_id=spec.attempt_id,
                quota_checkpoint=quota_checkpoint,
                author_name=scripts.policy_git(spec.policy, "author_name"),
                author_email=scripts.policy_git(spec.policy, "author_email"),
                commit_trailer=scripts.policy_git(spec.policy, "commit_trailer"),
                trailer_value=spec.external_id,
            ),
            mounts=[
                # FDY-0140: writable always, since what the worker left uncommitted is
                # committed before anything is collected.
                Mount("ws", REPO_MOUNT, sub_path="repo"),
                Mount("ws", REPORT_MOUNT, read_only=True, sub_path="report"),
                Mount("ws", OUTPUT_MOUNT, sub_path="output"),
            ],
            volumes=[self._claim_volume(spec.attempt_id)],
            limits=limits,
            timeout=self.config.collector_timeout_seconds,
            plan=EgressPlan(),
        )
        if collector_exit in (JOB_TIMED_OUT, JOB_API_ERROR):
            text, unavailable = self.role_errors.get(
                (k8sspec.ROLE_COLLECTOR, spec.attempt_id), ("", False)
            )
            text = text or (
                f"the collector did not finish within {self.config.collector_timeout_seconds}s"
            )
            if unavailable:
                raise CollectionUnavailableError(text)
            raise CollectionFailedError(text)
        bundle_ok = False
        if collector_exit == 0:
            bundle_exit = await self._run_role_job(
                spec,
                role=k8sspec.ROLE_BUNDLE,
                image=launched.image_digest if launched else spec.image,
                script=scripts.BUNDLE_VERIFY_SCRIPT,
                mounts=[Mount("ws", OUTPUT_MOUNT, read_only=True, sub_path="output")],
                volumes=[self._claim_volume(spec.attempt_id)],
                limits=limits,
                timeout=self.config.role_timeout_seconds,
                plan=EgressPlan(),
            )
            self._raise_if_unavailable(k8sspec.ROLE_BUNDLE, spec.attempt_id, bundle_exit)
            bundle_ok = bundle_exit == 0
        interruption = (
            adapter.interruption(
                ExitInfo(exit_code=observation.exit_code, oom_killed=observation.oom_killed),
                stdout_tail,
                stderr_tail,
                None,
            )
            if adapter is not None and observation.state is ObservationState.EXITED
            else None
        )
        interrupted = observation.never_started or quota_checkpoint or interruption is not None
        verifications = () if interrupted else await self._run_verifier(spec, limits)
        with tempfile.TemporaryDirectory(prefix="crucible-k8s-") as scratch:
            root = Path(scratch)
            changed_blobs = await self._read_workspace(spec, root, limits)
            outputs = read_outputs(
                root / "output",
                root / "verify",
                spec=spec,
                bundle_verified=bundle_ok,
                collector_exit=collector_exit,
                verifications=_merge_verifications(
                    verifications, root / "verify", spec, self.config.verifier_timeout_seconds
                ),
                tail_bytes=self.config.log_tail_bytes,
                changed_blobs=changed_blobs,
            )
        state = await self._workspace_state(spec.attempt_id)
        return CollectedOutputs(
            report=outputs.report,
            report_raw=outputs.report_raw,
            blocked_md=outputs.blocked_md,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            interruption=interruption,
            diff_paths=outputs.diff_paths,
            diff_findings=outputs.diff_findings,
            diff_unscanned=outputs.diff_unscanned,
            diff_changes=outputs.diff_changes,
            base_paths=outputs.base_paths,
            over_limit=outputs.over_limit,
            bundle=outputs.bundle,
            artifacts=(*outputs.artifacts, self._launch_evidence(spec, observation)),
            verifications=outputs.verifications,
            workspace_state=state,
            copy_rejections=outputs.copy_rejections,
            credential_sync=credential_sync,
            checkpoint_refusal=outputs.checkpoint_refusal,
            leftover_committed=outputs.leftover_committed,
            leftover_note=outputs.leftover_note,
        )

    def _launch_evidence(self, spec: LaunchSpec, observation: Observation) -> CollectedArtifact:
        """26's observability list, as one artifact of the attempt.

        The image digest is already on the attempt row (the Handle carries it). The rest
        of what 26 asks an attempt to record, the Job and Pod names, the node, the
        effective limits, the pod PID limit and the NetworkPolicy applied, has no field
        of its own on the port, so it is recorded as evidence the way every other
        per-attempt fact Crucible observed is: a stored artifact with an
        `artifact_present` evidence row (11). Nothing in it is a value."""
        launched = self._launched.get(spec.attempt_id)
        probe = self.probe
        document = {
            "provider": self.name,
            "namespace": self.config.namespace,
            "image_digest": launched.image_digest if launched else "",
            "job": launched.job_name if launched else "",
            "pod": (launched.pod_name if launched else "") or "",
            "node": (launched.node if launched else "") or "",
            "canary_node": probe.canary_node if probe else "",
            # What the live Pod carried when it was seen (issue 76), else what the policy
            # asked for, and which of the two this is.
            "limits": (launched.limits if launched else self._limits(spec)).as_dict(),
            "limits_source": launched.limits_source if launched else "policy",
            "pod_pid_limit": probe.pid_limit if probe else None,
            "pod_pid_limit_source": probe.pid_limit_source if probe else "",
            "runtime_class": "standard",
            "network_policy": (launched.network_policy if launched else None),
            "egress": list(self._egress_plan(spec, k8sspec.ROLE_WORKER).hosts),
            "final_observation": {
                "state": observation.state.value,
                "exit_code": observation.exit_code,
                "detail": observation.detail,
                "never_started": observation.never_started,
                "container_message": observation.container_message,
                "pod_events": list(observation.pod_events),
            },
        }
        return CollectedArtifact(
            name="report/kubernetes-launch.json",
            type="run_evidence",
            content=json.dumps(document, indent=2, sort_keys=True).encode("utf-8"),
            content_type="application/json",
        )

    async def terminate(self, h: Handle, mode: Literal["drain", "kill"]) -> None:
        """26: drain deletes the Pod with the policy grace period, kill with grace zero.

        Deleting a Pod is the only signal Kubernetes offers. What Crucible did is
        remembered, so a Pod that is gone because Crucible deleted it is reported as an
        exit and never as a loss (16)."""
        launched = self._launched.get(h.attempt_id)
        try:
            pod = await self._pod_of(h.ref)
        except KubernetesApiError as exc:
            raise ProviderError(f"terminate could not read the Pod of {h.ref}: {exc}") from exc
        if launched is not None:
            launched.terminated = mode
        if pod is None:
            return
        # The grace period is the one the Pod was created with, which is the task
        # policy's (issue 66): an adopted attempt, or a handle the provider never
        # launched, has no policy in memory, but the live Pod always carries it.
        grace = k8sspec.limits_from_pod(
            pod.get("spec") or {},
            launched.limits if launched else k8sspec.limits_from_policy({}),
        ).grace_seconds
        name = str((pod.get("metadata") or {}).get("name") or "")
        try:
            await self._call(
                self.client.delete,
                "pods",
                name,
                grace_period_seconds=grace if mode == "drain" else 0,
            )
        except KubernetesApiError as exc:
            if exc.status != 404:
                raise ProviderError(f"terminate failed: {exc}") from exc

    async def discard(self, ws: Workspace, spec: LaunchSpec | None = None) -> None:
        """12: remove anything secret placed for an attempt that will never be
        collected. The per-attempt Secret always, and the writable copy on the claim
        when one was seeded. Nothing else of the workspace is touched."""
        self._seeded.pop(ws.attempt_id, None)
        await self._delete_credential_secret(ws.attempt_id)
        # ADR 0019: prepare deletes the checkout token Secret itself; a deletion that
        # failed there is retried here.
        await self._delete_checkout_secret(ws.attempt_id)
        launched = self._launched.get(ws.attempt_id)
        spec = spec or (launched.spec if launched else None)
        if spec is None:
            return
        copy = launched.credential if launched is not None else self._credential_copy(spec)
        if copy is None or not copy.writable:
            return
        with contextlib.suppress(Exception):
            await self._remove_from_claim(spec, [k8sspec.CREDENTIAL_LEAF])

    async def cleanup(
        self, ws: Workspace, policy: CleanupPolicy, spec: LaunchSpec | None = None
    ) -> None:
        """08, 26: only ever called for an attempt that recorded `logs_drained`, or
        for one that ended before its worker launched (hades #394), under `delete`.

        Jobs and the NetworkPolicy go; the per-attempt Secret goes under every policy,
        `keep` included (12, 16); the claim is kept or deleted per policy, and a kept
        claim carries a retention label the sweep honours."""
        launched = self._launched.get(ws.attempt_id)
        spec = spec or (launched.spec if launched else None)
        # Cleanup never waits on a collection Pod (a Pod stuck Terminating would hold
        # back the Secrets below every tick); the label delete asks for it to go.
        await self._delete_by_label(("jobs", "networkpolicies", "pods"), attempt_id=ws.attempt_id)
        await self._delete_credential_secret(ws.attempt_id)
        await self._delete_checkout_secret(ws.attempt_id)
        if policy is CleanupPolicy.DELETE:
            await self._delete_by_label(
                ("persistentvolumeclaims", "configmaps"), attempt_id=ws.attempt_id
            )
        else:
            leaves = (
                ["repo", "output/tree", k8sspec.CREDENTIAL_LEAF]
                if policy is CleanupPolicy.KEEP_DIFF_ONLY
                # The checkout, the verifier's tree and any credential copy go; the
                # collected evidence stays on the claim (08).
                else [k8sspec.CREDENTIAL_LEAF]
            )
            if spec is None:
                log.warning(
                    "a retained claim keeps its credential leaf: no launch spec to remove it with",
                    extra={"attempt_id": ws.attempt_id},
                )
            else:
                try:
                    await self._remove_from_claim(spec, leaves)
                except Exception as exc:
                    # 12: a rotated token left on a retained claim is the thing this
                    # call exists to stop. A cleanup that could not do it says so
                    # rather than reporting success.
                    log.warning(
                        "a retained claim may still hold the credential copy",
                        extra={"attempt_id": ws.attempt_id, "error": str(exc)},
                    )
            with contextlib.suppress(KubernetesApiError):
                await self._call(
                    self.client.patch,
                    "persistentvolumeclaims",
                    k8sspec.object_name("ws", ws.attempt_id),
                    {"metadata": {"labels": {k8sspec.LABEL_RETAIN: policy.value}}},
                )
        self._forget(ws.attempt_id)

    def _forget(self, attempt_id: str) -> None:
        """What this process remembers of an attempt that is cleaned up or gone."""
        self._launched.pop(attempt_id, None)
        self._seeded.pop(attempt_id, None)
        self._credential_syncs.pop(attempt_id, None)
        for key in [key for key in self._collection_role_exits if key[1] == attempt_id]:
            del self._collection_role_exits[key]
        for key in [key for key in self.role_errors if key[1] == attempt_id]:
            del self.role_errors[key]

    async def release_workspace(self, ws: Workspace, spec: LaunchSpec | None = None) -> None:
        """16: the claim a cleanup policy kept, and everything else still labelled for
        the attempt, once the retention step decided nothing needs it. A claim that is
        still there afterwards (a Pod still mounting it holds its deletion) is not
        released yet: the step tries again on a later tick."""
        self._forget(ws.attempt_id)
        await self._delete_attempt_objects(ws.attempt_id)
        claim = k8sspec.object_name("ws", ws.attempt_id)
        try:
            row = await self._call(self.client.get, "persistentvolumeclaims", claim)
        except KubernetesApiError as exc:
            if exc.status == 404:
                return
            raise
        if not (row.get("metadata") or {}).get("deletionTimestamp"):
            raise ProviderError(f"the workspace claim {claim!r} was not deleted")

    async def reconcile(self) -> list[Handle]:
        """Adopt by label (10, 26). A Job is a handle while its Pod is alive, or while
        it has none yet and may still get one before the launch timeout."""
        try:
            rows = await self._call(
                self.client.list_objects,
                "jobs",
                label_selector=k8sspec.selector(**{k8sspec.LABEL_ROLE: k8sspec.ROLE_WORKER}),
            )
        except KubernetesApiError as exc:
            raise ProviderError(f"reconcile failed: {exc}") from exc
        handles: list[Handle] = []
        for row in rows:
            metadata = row.get("metadata") or {}
            attempt_id = str((metadata.get("labels") or {}).get(k8sspec.LABEL_ATTEMPT, ""))
            name = str(metadata.get("name", ""))
            if not attempt_id or not name:
                continue
            if (metadata.get("labels") or {}).get(k8sspec.LABEL_ADMIN):
                # A credential probe's worker (25) belongs to the API process running
                # it, not to any attempt the supervisor could adopt.
                continue
            try:
                pod = await self._pod_of(name)
            except KubernetesApiError:
                continue
            # A Job's `status.active` lags its Pod, so the Pod is what says whether a
            # worker is alive: 08 adopts what is running, and (26) what is still
            # waiting on the Job controller to create its Pod. A Job with no Pod and
            # a finished condition already ran its course before this restart, not a
            # launch still in flight, so it is left for the normal cleanup pass.
            if pod is not None:
                phase = str((pod.get("status") or {}).get("phase", ""))
                if phase not in ("Pending", "Running"):
                    continue
            else:
                finished = any(
                    isinstance(condition, dict)
                    and str(condition.get("status")) == "True"
                    and str(condition.get("type")) in ("Complete", "Failed")
                    for condition in (row.get("status") or {}).get("conditions") or []
                )
                if finished:
                    continue
            if attempt_id not in self._launched:
                # A restarted supervisor has no memory of the launch, so the launch
                # timeout of 26 would never fire for an adopted attempt that will never
                # schedule or never gets a Pod. The clock comes from the Job's own
                # creation timestamp, not from now, so an attempt already past the
                # window is caught on the first observation rather than given it again.
                created = _age_seconds(str(metadata.get("creationTimestamp", "")))
                # The live Pod is what the worker runs as (issues 66, 76); the Job's
                # template is what it is built from, and is there whether or not a Pod
                # exists yet. An empty image here would reach the helper Pods of
                # collection, which the API server rejects (103).
                template_spec = ((row.get("spec") or {}).get("template") or {}).get("spec") or {}
                live_spec = (pod or {}).get("spec") or {}
                pod_spec = live_spec if live_spec.get("containers") else template_spec
                containers = pod_spec.get("containers") or []
                image = str((containers[0] if containers else {}).get("image", ""))
                self._launched[attempt_id] = _Launched(
                    job_name=name,
                    spec=_ADOPTED_SPEC,
                    image_digest=image,
                    limits=k8sspec.limits_from_pod(pod_spec, k8sspec.limits_from_policy({})),
                    launched_at=time.monotonic() - created,
                    limits_source="pod" if pod_spec is live_spec else "template",
                )
            launched = self._launched[attempt_id]
            if pod is not None and not launched.pod_name:
                # So a Pod that vanishes between this reconcile and the first `observe`
                # reads as lost, not as one the Job controller never created (103).
                launched.pod_name = str((pod.get("metadata") or {}).get("name") or "")
                # `observe` restores the node only alongside the Pod name, so it is
                # restored here too or the launch evidence records no node.
                launched.node = str((pod.get("spec") or {}).get("nodeName") or "") or None
            handles.append(
                Handle(
                    provider=self.name,
                    ref=name,
                    attempt_id=attempt_id,
                    image_digest=launched.image_digest,
                    name=name,
                )
            )
        return handles

    async def retention(self, keep: Sequence[str]) -> int:
        """Remove what is labelled for attempts Crucible no longer tracks (16)."""
        live = set(keep)
        removed = 0
        for kind in (
            "jobs",
            "pods",
            "networkpolicies",
            "secrets",
            "configmaps",
            "persistentvolumeclaims",
        ):
            try:
                rows = await self._call(
                    self.client.list_objects, kind, label_selector=k8sspec.LABEL_ATTEMPT
                )
            except KubernetesApiError:
                continue
            for row in rows:
                metadata = row.get("metadata") or {}
                labels = metadata.get("labels") or {}
                attempt_id = str(labels.get(k8sspec.LABEL_ATTEMPT, ""))
                if (
                    labels.get(k8sspec.LABEL_ADMIN)
                    and _age_seconds(str(metadata.get("creationTimestamp", "")))
                    < ADMIN_GRACE_SECONDS
                ):
                    # A probe in flight in the API process: its own `finally` removes
                    # it, and the sweep only takes over once that process is gone.
                    continue
                if kind == "persistentvolumeclaims" and labels.get(k8sspec.LABEL_RETAIN):
                    # A claim a cleanup policy deliberately kept carries the retention
                    # label; the sweep honours it (26) and the workspace retention
                    # window of 16 is what removes it later.
                    continue
                if attempt_id and attempt_id not in live:
                    with contextlib.suppress(KubernetesApiError):
                        await self._call(self.client.delete, kind, str(metadata.get("name", "")))
                        removed += 1
        return removed + await self._sweep_login_policies()

    async def _sweep_login_policies(self) -> int:
        """A login's NetworkPolicy whose Job is gone. The API process deletes both when
        the login ends; the Job's own deadline and TTL remove it when that process died,
        and this removes the policy it leaves behind. A policy younger than a minute is
        skipped: the login creates it just before its Job."""
        try:
            policies = await self._call(
                self.client.list_objects,
                "networkpolicies",
                label_selector=k8sspec.selector(**{k8sspec.LABEL_ROLE: k8sspec.ROLE_LOGIN}),
            )
            jobs = await self._call(
                self.client.list_objects,
                "jobs",
                label_selector=k8sspec.selector(**{k8sspec.LABEL_ROLE: k8sspec.ROLE_LOGIN}),
            )
        except KubernetesApiError:
            return 0
        running = {
            str(((job.get("metadata") or {}).get("labels") or {}).get(k8sspec.LABEL_LOGIN, ""))
            for job in jobs
        }
        removed = 0
        for policy in policies:
            metadata = policy.get("metadata") or {}
            login_id = str((metadata.get("labels") or {}).get(k8sspec.LABEL_LOGIN, ""))
            if login_id in running:
                continue
            if _age_seconds(str(metadata.get("creationTimestamp", ""))) < 60:
                continue
            with contextlib.suppress(KubernetesApiError):
                await self._call(
                    self.client.delete, "networkpolicies", str(metadata.get("name", ""))
                )
                removed += 1
        return removed

    async def list_images(self) -> list[ImageInfo]:
        """The promoted worker images the cluster can pull, from the registry the
        release publishes to (13, 25, 26). A cluster holds no image on Crucible's side,
        so there is nothing local to list.

        One listing runs at a time and every caller waits on it, so a page polling the
        harnesses while the registry is slow does not start one more each time (108).
        A caller that gives up stops waiting; the listing ends at its own bound."""
        loop = asyncio.get_running_loop()
        listing = self._listings.get(loop)
        if listing is None or listing.done():
            listing = loop.create_task(self._list_images())
            # Read the outcome even when every caller has gone, so a failure nobody
            # waited for is not reported as never retrieved.
            listing.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._listings[loop] = listing
        return list(await asyncio.shield(listing))

    async def _list_images(self) -> list[ImageInfo]:
        await self._load_pull_auths()
        deadline = time.monotonic() + LIST_IMAGES_DEADLINE
        # Each resolve is a registry round trip, and a repository carries dozens of tags,
        # so a few run at once; one after another, a listing outlasts the 15 seconds the
        # harness and image endpoints wait for it.
        limit = asyncio.Semaphore(LIST_IMAGES_CONCURRENCY)

        async def resolve(reference: str) -> ImageInfo | None:
            async with limit:
                try:
                    info: ImageInfo = await self._registry_call(
                        self.registry.resolve, reference, deadline=deadline
                    )
                except RegistryError:
                    return None
            return replace(info, reference=reference) if info.harnesses else None

        images: list[ImageInfo] = []
        for repository in self.config.image_repositories:
            try:
                tags = await self._registry_call(
                    self.registry.list_tags, repository, deadline=deadline
                )
            except RegistryError as exc:
                raise ProviderError(f"image listing failed for {repository}: {exc}") from exc
            tags = [tag for tag in tags if not tag.startswith(LIST_IMAGES_SKIP_PREFIX)]
            found = await asyncio.gather(*(resolve(f"{repository}:{tag}") for tag in tags))
            images.extend(info for info in found if info is not None)
        if time.monotonic() >= deadline:
            log.warning(
                "image listing reached its bound; tags not resolved in time are left out",
                extra={"bound_seconds": LIST_IMAGES_DEADLINE},
            )
        return sorted(images, key=lambda i: i.reference)

    async def health(self) -> ProviderHealth:
        """25: the API server reachable, the namespace probe, the CNI egress result, the
        pod PID limit, and the runtime class in use."""
        checks: dict[str, Any] = {"namespace": self.config.namespace}
        try:
            checks["api_server"] = await self._call(self.client.version)
        except Exception as exc:
            checks["api_server"] = f"unreachable: {type(exc).__name__}"
            return ProviderHealth("unavailable", checks)
        with contextlib.suppress(Exception):
            await self._refresh_quota()
        capacity = self.capacity_view()
        checks["max_concurrency"] = capacity.workers
        checks.update(capacity.as_dict())
        probe = await self.ensure_ready()
        checks.update(probe.as_dict())
        if not probe.checked:
            return ProviderHealth("degraded", checks)
        # An unreachable local endpoint no longer fails the probe (crucible#110: it
        # blocks only launches routed to it), but it is still a real outage worth
        # surfacing here rather than reporting the provider as fully "ok".
        ok = probe.passed and probe.local_endpoint_reachable is not False
        return ProviderHealth("ok" if ok else "degraded", checks)

    async def probe_credential(self, request: ProbeRequest) -> ProbeResult:
        """25: run the hardened image with the credential mounted for one prompt under a
        hard timeout, sync the named files back, and remove everything.

        The Kubernetes form is the attempt path cut down to what a prompt needs, as the
        Docker probe is: a claim, an identity ConfigMap holding only the probe's
        IDENTITY.md and the adapter's templates, the per-run copy of the harness Secret,
        a preparer that only makes the directories, then the worker Job under the
        worker's own egress rules and the namespace readiness gate. The rotated auth
        files come back through the reader Pod and are synced exactly as an attempt's
        are (12). Everything carries `crucible.admin=probe` and is removed in the
        `finally` under the delete policy."""
        probe_id = f"probe{new_id()}"[:26]
        spec = LaunchSpec(
            attempt_id=probe_id,
            task_id=probe_id,
            external_id="probe",
            role="probe",
            harness=request.harness,
            model="probe",
            image=request.image,
            timeout_seconds=request.timeout_seconds,
            contract={"repository": {}},
            env=dict(request.env),
            command=tuple(request.argv),
            policy=request.policy,
            owner="crucible-admin",
            env_from_files=dict(request.env_from_files),
            stdin_files=tuple(request.stdin_files),
            stdin_text=request.stdin_text,
            endpoint=request.endpoint,
            endpoint_url=request.endpoint_url,
        )
        root = f"k8s://{self.config.namespace}/{k8sspec.object_name('ws', probe_id)}"
        ws = Workspace(
            attempt_id=probe_id,
            checkout_path=f"{root}/repo",
            identity_path=f"{root}/identity",
            report_path=f"{root}/report",
            output_path=f"{root}/output",
        )
        self._admin_runs[probe_id] = k8sspec.ADMIN_PROBE
        started = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        oom = False
        stdout_tail = stderr_tail = ""
        sync: CredentialSync | None = None
        detail = ""
        resolved = ""
        try:
            resolved = await self._resolve_image(spec)
            limits = self._limits(spec)
            # Issue 59: nothing is created, and no credential copied, before the gate.
            await self._require_ready(spec)
            await self._create(
                "persistentvolumeclaims",
                k8sspec.workspace_claim(
                    name=k8sspec.object_name("ws", probe_id),
                    namespace=self.config.namespace,
                    object_labels=self._labels(spec, k8sspec.ROLE_WORKER),
                    size=self.config.workspace_size,
                    storage_class=self.config.storage_class,
                ),
            )
            data, paths = _probe_identity(request, self.harnesses)
            await self._create(
                "configmaps",
                k8sspec.config_map(
                    name=k8sspec.object_name("identity", probe_id),
                    namespace=self.config.namespace,
                    object_labels=self._labels(spec, k8sspec.ROLE_WORKER),
                    data=data,
                    annotations={ANNOTATION_IDENTITY_PATHS: json.dumps(paths, sort_keys=True)},
                ),
            )
            copy = self._credential_copy(spec)
            if copy is not None:
                # 12: a login is about to replace the Secret; a probe's copy of the one
                # it replaces would sync a superseded session back over it.
                if request.harness in await self.logins_in_progress():
                    raise ProviderError(
                        f"a login for {request.harness} is running; probe it once the "
                        "login has finished"
                    )
                await self._seed_credential(spec, copy)
                self._seeded[probe_id] = dict(copy.seeded)
            code = await self._run_role_job(
                spec,
                role=k8sspec.ROLE_PREPARER,
                image=resolved,
                script=(
                    f"set -eu; mkdir -p {WORK_MOUNT}/repo {WORK_MOUNT}/report; "
                    f"mkdir -m 0700 -p {WORK_MOUNT}/{k8sspec.CREDENTIAL_LEAF}\n"
                ),
                mounts=[Mount("ws", WORK_MOUNT)],
                volumes=[self._claim_volume(probe_id)],
                limits=limits,
                timeout=self.config.prepare_timeout_seconds,
                plan=EgressPlan(),
            )
            if code != 0:
                raise ProviderError(
                    f"the probe could not prepare its workspace (exit {code}): "
                    f"{self.last_error.get(k8sspec.ROLE_PREPARER, '')}"
                )
            handle = await self.launch(ws, spec)
            started = time.monotonic()
            # The Pod's scheduling and image pull are not the harness's time: the wait
            # counts the probe's time from Running and gives the start the launch
            # timeout. The Job's own deadline still ends a harness that runs past it.
            code_seen = await self._await_job(handle.ref, timeout=request.timeout_seconds)
            if code_seen is None:
                timed_out = True
                detail = f"the probe did not finish within {request.timeout_seconds}s"
                with contextlib.suppress(Exception):
                    await self.terminate(handle, "kill")
            elif code_seen != 0 and await self._job_deadline_exceeded(handle.ref):
                # The Job's own deadline is shorter than this wait, so a hanging harness
                # is ended by the cluster first: the exit code or the missing Pod that
                # leaves behind is the timeout, not a crash (25).
                timed_out = True
                detail = (
                    f"the probe did not finish within {request.timeout_seconds}s; the "
                    "Job's deadline ended it"
                )
            observation = await self.observe(handle)
            if observation.state is ObservationState.EXITED:
                exit_code = observation.exit_code
                oom = observation.oom_killed
            stdout_tail, stderr_tail = await self._worker_tails(handle)
            sync = await self._sync_credential(handle, spec, limits)
        finally:
            with contextlib.suppress(Exception):
                await self.cleanup(ws, CleanupPolicy.DELETE, spec)
            with contextlib.suppress(Exception):
                await self._delete_credential_secret(probe_id)
            self._launched.pop(probe_id, None)
            self._seeded.pop(probe_id, None)
            self._admin_runs.pop(probe_id, None)
        cached = self._images.get(spec.image)
        return ProbeResult(
            exit_code=exit_code,
            image_digest=resolved,
            harness_version=cached.version_of(request.harness) if cached else None,
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            oom_killed=oom,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            credential_sync=sync,
            detail=detail,
        )

    # ----- the harness credential Secret (12, ADR 0015) -----------------

    def credential_secret(self, harness: str) -> str:
        return self.config.credential_secret_name(harness)

    def read_credential_secret(self, harness: str) -> dict[str, Any] | None:
        """The harness Secret as the API server returns it, or None when it does not
        exist. Blocking: the admin services call it from their own thread."""
        try:
            return self.client.get("secrets", self.credential_secret(harness))
        except KubernetesApiError as exc:
            if exc.status == 404:
                return None
            raise ProviderError(
                f"the credential Secret {self.credential_secret(harness)!r} is not readable "
                f"in {self.config.namespace} ({exc.status})"
            ) from exc

    def read_credential_files(self, harness: str) -> dict[str, bytes] | None:
        """The adapter's declared auth files the Secret holds, by auth file name. None
        when the Secret does not exist; a declared file it lacks is simply absent."""
        return self.credential_files_in(harness, self.read_credential_secret(harness))

    def credential_files_in(
        self, harness: str, body: dict[str, Any] | None
    ) -> dict[str, bytes] | None:
        """`read_credential_files` of a Secret body already read, so a caller that needs
        the body and the files reads the Secret once."""
        adapter = self.harnesses.require(harness)
        credential = adapter.credential_spec()
        if body is None:
            return None
        data = body.get("data") or {}
        out: dict[str, bytes] = {}
        for auth in credential.auth_files if credential is not None else ():
            raw = data.get(_secret_key(auth.name))
            if raw is None:
                continue
            with contextlib.suppress(ValueError):
                out[auth.name] = base64.b64decode(str(raw))
        return out

    def write_credential_files(self, harness: str, files: Mapping[str, bytes]) -> dict[str, Any]:
        """Make the harness Secret hold exactly these auth files (ADR 0015).

        Created, labelled as the service's own, when it is absent; otherwise its data is
        replaced by one merge patch that also removes every key these files do not
        name, so the Secret never mixes two sessions. The values travel in the request
        body only. Returns what was done, never a value."""
        adapter = self.harnesses.require(harness)
        credential = adapter.credential_spec()
        if credential is None:
            raise ProviderError(f"harness {harness!r} needs no credential")
        declared = {a.name for a in credential.auth_files}
        unknown = sorted(set(files) - declared)
        if unknown:
            raise ProviderError(f"{unknown} are not auth files the {harness} adapter declares")
        name = self.credential_secret(harness)
        payload = {_secret_key(n): value for n, value in files.items()}
        body = k8sspec.secret(
            name=name,
            namespace=self.config.namespace,
            object_labels=_owned_labels(harness),
            data=payload,
        )
        try:
            self.client.create("secrets", body)
            return {"secret": name, "created": True, "files": sorted(files)}
        except KubernetesApiError as exc:
            if exc.status != 409:
                raise ProviderError(
                    f"the credential Secret {name!r} could not be created ({exc.status})"
                ) from exc
        try:
            current = self.client.get("secrets", name)
            stale = {k: None for k in (current.get("data") or {}) if k not in payload}
            self.client.patch(
                "secrets",
                name,
                {
                    "metadata": {"labels": _owned_labels(harness)},
                    "data": {**body["data"], **stale},
                },
                resource_version=current["metadata"]["resourceVersion"],
            )
        except KubernetesApiError as exc:
            raise ProviderError(
                f"the credential Secret {name!r} could not be written ({exc.status})"
            ) from exc
        return {"secret": name, "created": False, "files": sorted(files)}

    def probes_holding(self, harness: str) -> list[str]:
        """The credential probes that hold a copy of `harness`'s credential now: their
        per-run Secret exists from the seeding until the sync-back removed it (12).
        Blocking, for the login's own thread. Any api replica's probe counts."""
        selector = k8sspec.selector(
            **{k8sspec.LABEL_ADMIN: k8sspec.ADMIN_PROBE, k8sspec.LABEL_HARNESS: harness}
        )
        try:
            rows = self.client.list_objects("secrets", label_selector=selector)
        except KubernetesApiError as exc:
            raise ProviderError(f"the credential probes could not be listed: {exc}") from exc
        return sorted(
            str(((row.get("metadata") or {}).get("labels") or {}).get(k8sspec.LABEL_ATTEMPT, ""))
            for row in rows
        )

    # ----- the login lock (25, 26) --------------------------------------

    def login_lock_name(self, harness: str) -> str:
        return f"login-lock-{harness.replace('_', '-')}"[:63]

    def acquire_login_lock(self, harness: str, *, holder: str, timeout: int) -> LoginLock:
        """Take `harness`'s login lock for every api replica at once (25).

        Each api process keeps its own logins in memory, so two replicas (a rollout, or
        an overlay with more than one) could each start a login Job for the same
        harness and the last to finish would silently replace the Secret. The lock is a
        ConfigMap with a fixed name per harness: `create` is atomic on the API server,
        so exactly one replica gets it and every other gets 409 and is told who holds
        it. It carries its expiry, the login's own deadline plus slack, so the lock of
        an api that died mid-login is taken over once that deadline has passed; the
        takeover deletes that exact incarnation by uid, so two replicas reclaiming at
        once cannot both win. Blocking, for the admin service's own thread."""
        name = self.login_lock_name(harness)
        seconds = timeout + LOGIN_READBACK_SECONDS + LOGIN_LOCK_SLACK_SECONDS
        expires = datetime.fromtimestamp(time.time() + seconds, tz=UTC)
        body = k8sspec.config_map(
            name=name,
            namespace=self.config.namespace,
            object_labels={
                k8sspec.LABEL_ROLE: k8sspec.ROLE_LOGIN_LOCK,
                k8sspec.LABEL_HARNESS: harness,
                k8sspec.LABEL_OWNER: "crucible-admin",
            },
            data={},
            annotations={
                ANNOTATION_LOCK_HOLDER: holder,
                ANNOTATION_LOCK_EXPIRES: expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        for _ in range(3):
            try:
                created = self.client.create("configmaps", body)
                uid = str((created.get("metadata") or {}).get("uid") or "")
                return LoginLock(harness=harness, name=name, uid=uid, holder=holder)
            except KubernetesApiError as exc:
                if exc.status != 409:
                    raise ProviderError(
                        f"the {harness} login lock could not be taken ({exc.status}): {exc}"
                    ) from exc
            try:
                current = self.client.get("configmaps", name)
            except KubernetesApiError as exc:
                if exc.status == 404:
                    continue  # released between the create and the look; try again
                raise ProviderError(
                    f"the {harness} login lock could not be read ({exc.status}): {exc}"
                ) from exc
            metadata = current.get("metadata") or {}
            annotations = metadata.get("annotations") or {}
            held_by = str(annotations.get(ANNOTATION_LOCK_HOLDER) or "an unnamed holder")
            left = _seconds_until(str(annotations.get(ANNOTATION_LOCK_EXPIRES) or ""))
            if left > 0:
                raise LoginLockHeldError(
                    f"a login for {harness} is already in progress, held by {held_by}; if "
                    f"that api process has died its lock is taken over in {_minutes(left)}"
                )
            with contextlib.suppress(KubernetesApiError):
                self.client.delete("configmaps", name, uid=str(metadata.get("uid") or ""))
        raise LoginLockHeldError(
            f"a login for {harness} is already in progress: another api replica took its "
            "lock at the same moment"
        )

    def login_lock_held(self, lock: LoginLock) -> bool:
        """Whether `lock` is still this process's: the ConfigMap there is the one it
        created. A login whose lock expired and was taken over does not store."""
        try:
            current = self.client.get("configmaps", lock.name)
        except KubernetesApiError:
            return False
        return str((current.get("metadata") or {}).get("uid") or "") == lock.uid

    def release_login_lock(self, lock: LoginLock) -> None:
        """Give the lock back. Only the incarnation this process created is deleted; a
        lock another replica took over after this one expired is left alone."""
        with contextlib.suppress(KubernetesApiError):
            self.client.delete("configmaps", lock.name, uid=lock.uid or None)

    # ----- the login Job (25, 26) ---------------------------------------

    async def logins_in_progress(self) -> frozenset[str]:
        """The harnesses whose login Job exists and has not finished (12, 25).

        A Job is the whole of a login: it exists from before the CLI starts until the
        service has read the auth files off it and written the Secret. That makes it
        the lock another process (the supervisor) can see."""
        try:
            rows = await self._call(
                self.client.list_objects,
                "jobs",
                label_selector=k8sspec.selector(**{k8sspec.LABEL_ROLE: k8sspec.ROLE_LOGIN}),
            )
        except KubernetesApiError as exc:
            raise ProviderError(f"the login Jobs could not be listed: {exc}") from exc
        running: set[str] = set()
        for row in rows:
            metadata = row.get("metadata") or {}
            status = row.get("status") or {}
            if metadata.get("deletionTimestamp"):
                continue
            if int(status.get("succeeded") or 0) or int(status.get("failed") or 0):
                continue
            harness = str((metadata.get("labels") or {}).get(k8sspec.LABEL_HARNESS, ""))
            if harness:
                running.add(harness)
        return frozenset(running)

    async def run_login_job(
        self,
        *,
        flow: Any,
        image: str,
        session: Any,
        argv: tuple[str, ...],
        timeout: int,
        accept: Callable[[Mapping[str, bytes]], Sequence[str]],
        lock: LoginLock | None = None,
    ) -> None:
        """Run one harness login as a Job and store what it wrote in the Secret (25).

        The Pod runs the harness's own CLI under a pseudo-terminal from the promoted
        worker image, with no workspace, no credential mounted, and a NetworkPolicy for
        the adapter's login endpoints only (crucible#58). Its home is memory-backed, so
        the auth files it writes never reach a disk. Inside the Pod a driver keeps the
        one-time token Claude Code prints out of the log (it goes to `oauth-token` and
        the log says so), and strips terminal control codes, so the Pod log carries the
        URL, the device code and the prompts and nothing secret; the service reads that
        log for the operator. A pasted code goes in over exec stdin. After the CLI exits
        the service reads the declared auth files back over exec, never through a log,
        hands them to `accept` (the shape check and the concurrency rule), writes the
        Secret when `accept` finds no problem, and deletes the Job.

        Whatever the CLI's exit code, files that pass are stored: the Docker login
        leaves what the CLI wrote in the directory whatever its exit, and AGY's login
        command ends with a prompt the login Job cannot send to a model endpoint.

        Once the CLI has exited (or the login failed or was cancelled) the session reads
        `finishing`, which refuses a cancel and a code. It reads `finished` or `failed`
        only after the Job's deletion, the wait for its Pods and the release of `lock`
        have run, so an operator who retries the moment a login ends (a cancel above all)
        is not refused by that same login's lock. Each of those steps is best effort: a
        lock that could not be deleted expires on its own."""
        from crucible.application.admin.login import _consume  # noqa: PLC0415

        login_id = f"login{new_id()}"[:26]
        adapter = self.harnesses.require(flow.harness)
        credential = adapter.credential_spec()
        if credential is None:
            raise ProviderError(f"harness {flow.harness!r} has no credential")
        root = _login_root(credential)
        spec = LaunchSpec(
            attempt_id=login_id,
            task_id=login_id,
            external_id="login",
            role="login",
            harness=flow.harness,
            model="login",
            image=image,
            timeout_seconds=timeout,
            contract={"repository": {}},
            policy={**LOGIN_POLICY, "images": {"allowlist": [image]}},
            owner="crucible-admin",
        )
        name = f"login-{flow.harness.replace('_', '-')}-{login_id.lower()}"[:63]
        object_labels = {
            k8sspec.LABEL_ROLE: k8sspec.ROLE_LOGIN,
            k8sspec.LABEL_ADMIN: k8sspec.ADMIN_LOGIN,
            k8sspec.LABEL_LOGIN: login_id,
            k8sspec.LABEL_HARNESS: flow.harness,
            k8sspec.LABEL_OWNER: "crucible-admin",
        }
        policy_name: str | None = None
        job_created = False
        outcome = "failed"
        cancelled = False
        session.credential_written = False
        try:
            # The image first: a namespace probed for the first time runs its canary on
            # an image an attempt already resolved, and this is one.
            resolved = await self._resolve_image(spec)
            probe = await self.ensure_ready()
            if not probe.passed:
                raise HarnessRefusedError(
                    f"refusing to start the {flow.harness} login: the workers namespace is "
                    f"not ready ({probe.detail})"
                )
            if session.cancel_requested:
                session.error = "login cancelled"
                return
            plan = self._egress_plan(spec, k8sspec.ROLE_LOGIN)
            if not plan.empty:
                plan = await self._resolve_plan(plan, broad=self._broad_for(k8sspec.ROLE_LOGIN))
                policy_name = k8sspec.object_name("np-login", login_id)
                await self._call(
                    self.client.create,
                    "networkpolicies",
                    self._policy_body(
                        policy_name,
                        object_labels,
                        login_id,
                        k8sspec.ROLE_LOGIN,
                        plan,
                        pod_selector={
                            k8sspec.LABEL_LOGIN: login_id,
                            k8sspec.LABEL_ROLE: k8sspec.ROLE_LOGIN,
                        },
                    ),
                )
            limits = k8sspec.limits_from_policy(LOGIN_POLICY)
            env = {
                "HOME": "/home/worker",
                "TERM": "xterm",
                "SHELL": "/bin/bash",
                flow.directory_env: root,
                "CRUCIBLE_LOGIN_DIR": root,
                "CRUCIBLE_EGRESS_ALLOWLIST": ",".join(plan.hosts),
            }
            if flow.captures_token and flow.token_pattern:
                env["CRUCIBLE_LOGIN_TOKEN_PATTERN"] = flow.token_pattern
                env["CRUCIBLE_LOGIN_TOKEN_FILE"] = flow.token_file
            mounts = list(k8sspec.base_mounts())
            volumes = list(k8sspec.base_volumes(limits))
            if not (root + "/").startswith("/home/worker/"):
                mounts.append(Mount("login", root))
                volumes.append(k8sspec.memory_volume("login", limits.tmpfs_bytes))
            body = k8sspec.job(
                name=name,
                namespace=self.config.namespace,
                object_labels=object_labels,
                pod=k8sspec.pod_spec(
                    PodRequest(
                        role=k8sspec.ROLE_LOGIN,
                        image=resolved,
                        command=["bash", "-c", _LOGIN_DRIVER, "crucible-login", *argv],
                        limits=limits,
                        env=env,
                        mounts=mounts,
                        volumes=volumes,
                        service_account=self.config.service_account,
                        image_pull_secret=self.config.image_pull_secret,
                        host_aliases=k8sspec.host_aliases(plan),
                    )
                ),
                active_deadline_seconds=timeout + LOGIN_READBACK_SECONDS,
                ttl_seconds_after_finished=LOGIN_TTL_SECONDS,
            )
            await self._call(self.client.create, "jobs", body)
            job_created = True
            session.state = "waiting_for_operator"
            pod_name, exit_code = await self._follow_login(
                name, flow, session, timeout=timeout, consume=_consume, root=root
            )
            session.exit_code = exit_code
            # The outcome is decided from here: a cancel or a code the operator sends
            # now would be accepted for a CLI that has already exited.
            cancelled = session.begin_finishing()
            if exit_code is not None and pod_name and not cancelled:
                await self._store_login(pod_name, flow, credential, root, session, accept)
            if cancelled and not session.credential_written:
                session.error = session.error or "login cancelled"
            elif exit_code == 0 and session.error is None:
                outcome = "finished"
            elif session.error is None:
                session.error = f"the login command exited {exit_code}"
                if session.credential_written:
                    session.error += (
                        "; the auth files it wrote passed the shape check and are "
                        "stored in the Secret, so validate decides whether they work"
                    )
        except Exception as exc:
            outcome = "failed"
            session.error = f"the login Job failed: {type(exc).__name__}: {exc}"
        finally:
            session.begin_finishing()
            if job_created:
                with contextlib.suppress(Exception):
                    await self._call(self.client.delete, "jobs", name, grace_period_seconds=0)
                with contextlib.suppress(Exception):
                    await self._await_job_pods_gone(name)
            if policy_name:
                with contextlib.suppress(Exception):
                    await self._call(self.client.delete, "networkpolicies", policy_name)
            if lock is not None:
                # Best effort: a lock that could not be deleted expires on its own.
                with contextlib.suppress(Exception):
                    await self._call(self.release_login_lock, lock)
            session.state = outcome

    async def _follow_login(
        self,
        name: str,
        flow: Any,
        session: Any,
        *,
        timeout: int,
        consume: Callable[..., str],
        root: str,
    ) -> tuple[str | None, int | None]:
        """Read the login Pod's log for the operator until the CLI exits, feeding back a
        pasted code. Returns the Pod's name and the CLI's exit code, None when it never
        exited (cancelled, timed out, or the Pod ended first)."""
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        pod_name: str | None = None
        consumed = 0
        buffer = ""
        exit_code: int | None = None
        while True:
            if session.cancel_requested:
                session.error = "login cancelled"
                break
            if time.monotonic() >= deadline:
                session.error = "login timed out"
                break
            try:
                pod = await self._pod_of(name)
            except KubernetesApiError:
                pod = None
            if pod is None and time.monotonic() - started > self.config.launch_timeout_seconds:
                session.error = (
                    f"the login Job has had no Pod for {self.config.launch_timeout_seconds}s; "
                    "the namespace's ResourceQuota or admission may be refusing it (see the "
                    "Job's events in crucible-workers)"
                )
                break
            if pod is not None:
                pod_name = str((pod.get("metadata") or {}).get("name") or "")
                status = pod.get("status") or {}
                phase = str(status.get("phase", ""))
                if (
                    phase in _PENDING_PHASES
                    and time.monotonic() - started > self.config.launch_timeout_seconds
                ):
                    session.error = (
                        "the login Pod did not start: "
                        f"{self._pending_failure(pod).detail or 'still Pending'}"
                    )
                    break
                try:
                    frames = await self._call(
                        self.client.pod_log,
                        pod_name,
                        container=k8sspec.CONTAINER_NAME,
                        timestamps=False,
                    )
                except KubernetesApiError:
                    frames = []
                text = b"".join(f.payload for f in frames).decode("utf-8", "replace")
                if len(text) < consumed:
                    consumed = 0
                fresh, consumed = text[consumed:], len(text)
                shown: list[str] = []
                for line in fresh.splitlines(keepends=True):
                    marker = line.strip()
                    if marker.startswith(_LOGIN_EXIT_MARKER):
                        with contextlib.suppress(ValueError):
                            exit_code = int(marker[len(_LOGIN_EXIT_MARKER) :])
                        continue
                    shown.append(line)
                buffer = consume(buffer + "".join(shown), flow, None, Path(root), session, None)
                if exit_code is not None:
                    break
                session.notice_waiting(flow)
                if session.state == "waiting_for_code":
                    code = session.wait_for_code(0)
                    if code is not None and session.error is None:
                        await self._send_login_code(pod_name, code)
                        session.state = "waiting_for_operator"
                if _terminated_state(status) is not None or phase in ("Succeeded", "Failed"):
                    session.error = (
                        f"the login Pod ended ({phase or 'terminated'}) before the login "
                        "command reported its exit"
                    )
                    break
            await asyncio.sleep(max(self.config.poll_interval_seconds, 0.05))
        if buffer:
            consume(buffer + "\n", flow, None, Path(root), session, None)
        return pod_name, exit_code

    async def _send_login_code(self, pod: str, code: str) -> None:
        """The pasted code, into the CLI's terminal, over exec stdin (25). Never in the
        exec's argv, which the API server may audit, and never in an environment."""
        result = await self._call(
            self.client.pod_exec,
            pod,
            ["sh", "-c", _LOGIN_CODE_SCRIPT],
            container=k8sspec.CONTAINER_NAME,
            limit=4096,
            stdin=(code.strip() + "\n").encode("utf-8"),
        )
        if result.exit_code != 0:
            raise ProviderError(
                f"the pasted code could not be handed to the login (exit {result.exit_code})"
            )

    async def _store_login(
        self,
        pod: str,
        flow: Any,
        credential: CredentialSpec,
        root: str,
        session: Any,
        accept: Callable[[Mapping[str, bytes]], Sequence[str]],
    ) -> None:
        files: dict[str, bytes] = {}
        problems: list[str] = []
        for auth in credential.auth_files:
            data = await self._exec_read(pod, str(credential.source_path(root, auth.name)))
            if data is None:
                continue
            if data is _TRUNCATED:
                problems.append(f"{auth.name}: larger than the read limit")
            elif data is _UNREADABLE:
                problems.append(f"{auth.name}: could not be read back from the login Pod")
            else:
                files[auth.name] = data
        if flow.captures_token and flow.token_file in files:
            session.token_written = True
        if not problems:
            if not files and session.error:
                # The CLI wrote nothing and the login already says why (AGY's own wait
                # ran out, hades #173); a shape failure would only bury that.
                return
            problems.extend(await asyncio.to_thread(accept, files))
        if problems:
            session.error = "the login's auth files were not stored: " + "; ".join(problems)
            return
        await asyncio.to_thread(self.write_credential_files, flow.harness, files)
        session.credential_written = True

    async def _exec_read(
        self, pod: str, path: str, limit: int = CREDENTIAL_READ_LIMIT, *, use_backoff: bool = False
    ) -> Any:
        """One file off a running Pod over exec: its bytes, None when it is absent, or
        `_TRUNCATED` / `_UNREADABLE`. Never through a log (12)."""
        try:
            call = self._call_with_backoff if use_backoff else self._call
            result: ExecResult = await call(
                self.client.pod_exec,
                pod,
                ["sh", "-c", _read_one_script(path, limit)],
                container=k8sspec.CONTAINER_NAME,
                limit=limit * 2,
            )
        except KubernetesApiError:
            return _UNREADABLE
        if result.exit_code != 0:
            return _UNREADABLE
        status, _, payload = result.stdout.partition(b"\n")
        token = status.decode("ascii", "replace").strip()
        if token == "ok":
            try:
                return base64.b64decode(payload)
            except ValueError:
                return _UNREADABLE
        if token == "too-large":
            return _TRUNCATED
        return None

    # ----- the pod bodies ----------------------------------------------

    def _claim_volume(self, attempt_id: str, *, read_only: bool = False) -> dict[str, Any]:
        return {
            "name": "ws",
            "persistentVolumeClaim": {
                "claimName": k8sspec.object_name("ws", attempt_id),
                "readOnly": read_only,
            },
        }

    def _identity_volume(self, spec: LaunchSpec, paths: Mapping[str, str]) -> dict[str, Any]:
        """The identity bundle, key by key.

        A ConfigMap key may not hold a path separator, so `harness/settings.json`
        is stored under a flattened key and projected back to its real relative path
        here. The mapping is not guessed from the key: it is the mapping `prepare`
        recorded on the ConfigMap itself, so a template file whose own name contains
        the separator's encoding still lands where the bundle hash says it is."""
        return {
            "name": "identity",
            "configMap": {
                "name": k8sspec.object_name("identity", spec.attempt_id),
                "defaultMode": 0o444,
                "items": [_identity_item(key, path) for key, path in sorted(paths.items())],
            },
        }

    async def _identity_paths(self, attempt_id: str) -> dict[str, str]:
        body = await self._call_with_backoff(
            self.client.get, "configmaps", k8sspec.object_name("identity", attempt_id)
        )
        annotations = (body.get("metadata") or {}).get("annotations") or {}
        raw = annotations.get(ANNOTATION_IDENTITY_PATHS)
        if raw:
            with contextlib.suppress(ValueError):
                loaded = json.loads(str(raw))
                if isinstance(loaded, dict):
                    return {str(k): str(v) for k, v in loaded.items()}
        return {key: key for key in (body.get("data") or {})}

    def _worker_pod(
        self,
        ws: Workspace,
        spec: LaunchSpec,
        *,
        resolved: str,
        limits: Limits,
        copy: _CredentialCopy | None,
        identity_paths: Mapping[str, str],
        credential_keys: Sequence[str],
        host_aliases: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        if copy is None and spec.env_from_files:
            spec = replace(spec, env_from_files={})
        command, launch_env = self._command(spec)
        env = {
            "CRUCIBLE_ATTEMPT_ID": spec.attempt_id,
            "CRUCIBLE_TASK_EXTERNAL_ID": spec.external_id,
            "CRUCIBLE_IDENTITY_DIR": IDENTITY_MOUNT,
            "CRUCIBLE_REPORT_DIR": REPORT_MOUNT,
            "CRUCIBLE_REPO_DIR": REPO_MOUNT,
            "HOME": "/home/worker",
            **PACKAGE_CACHE_ENV,
            "CRUCIBLE_EGRESS_ALLOWLIST": ",".join(
                self._egress_plan(spec, k8sspec.ROLE_WORKER).hosts
            ),
            **spec.env,
            **launch_env,
        }
        mounts = [
            *k8sspec.base_mounts(),
            # 26's mount layout, with the paths the identity bundle names (06): the
            # bundle tells the worker its checkout is at REPO_MOUNT and its report
            # directory at REPORT_MOUNT, so those are where they are mounted.
            Mount("ws", REPO_MOUNT, sub_path="repo"),
            Mount("ws", REPORT_MOUNT, sub_path="report"),
            Mount("ws", PACKAGE_CACHE_MOUNT, sub_path=PACKAGE_CACHE_LEAF),
            Mount("identity", IDENTITY_MOUNT, read_only=True),
        ]
        volumes = [
            *k8sspec.base_volumes(limits),
            self._claim_volume(spec.attempt_id),
            self._identity_volume(spec, identity_paths),
        ]
        init_containers: list[dict[str, Any]] = []
        if copy is not None:
            credential_mounts, credential_volumes, init_containers = self._credential_mounts(
                spec, copy, resolved, limits, credential_keys, identity_paths
            )
            mounts.extend(credential_mounts)
            volumes.extend(credential_volumes)
            env.update(copy.spec.env())
        return k8sspec.pod_spec(
            PodRequest(
                role=k8sspec.ROLE_WORKER,
                image=resolved,
                command=command,
                limits=limits,
                env=env,
                mounts=mounts,
                volumes=volumes,
                init_containers=init_containers,
                working_dir=REPO_MOUNT,
                service_account=self.config.service_account,
                image_pull_secret=self.config.image_pull_secret,
                host_aliases=host_aliases,
            )
        )

    def _credential_mounts(
        self,
        spec: LaunchSpec,
        copy: _CredentialCopy,
        image: str,
        limits: Limits,
        present: Sequence[str],
        identity_paths: Mapping[str, str] | None = None,
    ) -> tuple[list[Mount], list[dict[str, Any]], list[dict[str, Any]]]:
        """The per-attempt credential, mounted per the adapter's declaration (12, 26).

        Read-only is the Secret itself, which is a tmpfs the worker cannot write and
        nothing ever lands on a disk for.

        `rw-narrow` cannot be the Secret: a Kubernetes Secret volume is read-only
        whatever the mount asks for, and the harnesses that declare `rw-narrow` refresh
        their own token in place (S1). So an init container copies the named files off
        the Secret into the `credential` leaf of the attempt's own claim, mode 0700 and
        0600, which is the same shape and the same properties 12 requires and the same
        place the Docker provider puts it. The rotated file is read back from there
        through the reader Pod and the copy is removed under every cleanup policy."""
        target = copy.spec.mount_target
        templates = sorted(copy.spec.templates)
        # A Secret key may not hold a path separator and an auth file's name may (AGY's
        # token sits under `antigravity-cli/`), so the volume projects each key back to
        # the relative path the adapter declared.
        # Only the keys the Secret actually carries. An adapter may declare an optional
        # auth file (Claude Code's `.claude.json`) that the harness Secret does not
        # have, and a projection naming a key that is not there is a Pod the kubelet
        # refuses to start: the worker would sit Pending with nothing to classify.
        items = [
            {"key": _secret_key(auth.name), "path": auth.name, "mode": 0o400}
            for auth in copy.spec.auth_files
            if _secret_key(auth.name) in set(present)
        ]
        source_volume = {
            "name": "cred-source" if copy.writable else "cred",
            "secret": {
                "secretName": k8sspec.object_name("cred", spec.attempt_id),
                "defaultMode": 0o400,
                "items": items,
                # A harness whose optional auth file was absent still starts; a required
                # one refused the launch before this Pod was rendered (12).
                "optional": False,
            },
        }
        mounts: list[Mount] = []
        volumes: list[dict[str, Any]] = []
        init: list[dict[str, Any]] = []
        if copy.writable:
            mounts.append(Mount("ws", target, sub_path=k8sspec.CREDENTIAL_LEAF))
            init.append(
                {
                    "name": k8sspec.CREDENTIAL_INIT_CONTAINER,
                    "image": image,
                    "command": ["sh", "-c", _seed_script(copy.spec)],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "limits": {
                            "cpu": limits.cpu,
                            "memory": limits.memory,
                            "ephemeral-storage": limits.ephemeral_storage,
                        },
                        # The same fraction as the main container's request (issue 93):
                        # Kubernetes takes a pod's effective request as the larger of the
                        # sum of its app containers and any one init container, so an
                        # init container left at the full limit would silently undo the
                        # role pod's smaller request.
                        "requests": {
                            "cpu": limits.cpu_request,
                            "memory": limits.memory_request,
                        },
                    },
                    "volumeMounts": [
                        {
                            "name": "cred-source",
                            "mountPath": k8sspec.CREDENTIAL_SOURCE_MOUNT,
                            "readOnly": True,
                        },
                        {
                            "name": "ws",
                            "mountPath": "/crucible/credential",
                            "subPath": k8sspec.CREDENTIAL_LEAF,
                        },
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            )
            # 12, 13: the Crucible-owned templates, read-only, each at its own path
            # inside the credential directory, so a worker cannot plant a hook or a
            # server definition a later worker would inherit. They come from the
            # identity bundle, so the bundle hash covers them.
            for name in templates:
                mounts.append(
                    Mount(
                        "identity",
                        f"{target}/{name}",
                        read_only=True,
                        sub_path=f"{k8sspec.TEMPLATE_PREFIX}/{name}",
                    )
                )
            volumes.append(source_volume)
        # 349: in `ro` mode the credential directory is the Secret itself,
        # read-only. A subPath file mount inside that directory cannot be
        # created by runc, so we build one projected volume whose sources
        # are the Secret items plus the identity ConfigMap template items,
        # all read-only, and mount it at `target` instead of layering a
        # file mount over the read-only Secret.
        elif identity_paths is not None:
            projection: dict[str, Any] = {
                "sources": [
                    {
                        "secret": {
                            "name": k8sspec.object_name("cred", spec.attempt_id),
                            "optional": False,
                            "items": [
                                {
                                    "key": _secret_key(auth.name),
                                    "path": auth.name,
                                    "mode": 0o400,
                                }
                                for auth in copy.spec.auth_files
                                if _secret_key(auth.name) in set(present)
                            ],
                        }
                    }
                ]
            }
            # Add the template entries from the identity ConfigMap, but only when
            # there is at least one: `ConfigMapProjection` has no `defaultMode`
            # field (the API silently drops it), and Kubernetes treats an empty
            # or absent `items` as "project every key", so a template-free
            # harness would otherwise get every key of the identity ConfigMap
            # (IDENTITY.md and the rest) projected into its credential directory.
            template_items = [
                {"key": k, "path": v.removeprefix(f"{k8sspec.TEMPLATE_PREFIX}/"), "mode": 0o444}
                for k, v in sorted(identity_paths.items())
                if v.startswith(f"{k8sspec.TEMPLATE_PREFIX}/")
            ]
            if template_items:
                projection["sources"].append(
                    {
                        "configMap": {
                            "name": k8sspec.object_name("identity", spec.attempt_id),
                            "items": template_items,
                        }
                    }
                )
            volumes.append({"name": "cred", "projected": projection})
            mounts.append(Mount("cred", target, read_only=True))
        else:
            volumes.append(source_volume)
        return mounts, volumes, init

    def _command(self, spec: LaunchSpec) -> tuple[list[str], dict[str, str]]:
        """The harness argv, wrapped exactly as the Docker provider wraps it (07), and
        always when the attempt has allowlisted hosts: the wrapper's egress probe runs
        before the harness and reports each of them (hades #425)."""
        from crucible.adapters.execution.docker import LAUNCH_WRAPPER  # noqa: PLC0415

        argv = list(spec.command)
        if not argv:
            adapter = self.harnesses.get(spec.harness)
            if adapter is not None:
                argv = list(adapter.build_launch(self._launch_context(spec)).argv)
        wrapped = bool(spec.env_from_files or spec.stdin_files or spec.stdin_text)
        wrapped = wrapped or bool(spec.transcript_path)
        wrapped = wrapped or bool(self._egress_plan(spec, k8sspec.ROLE_WORKER).hosts)
        if not wrapped:
            return argv, {}
        env: dict[str, str] = {}
        if spec.env_from_files:
            env["CRUCIBLE_ENV_FROM_FILES"] = " ".join(
                f"{var}={path}" for var, path in sorted(spec.env_from_files.items())
            )
        if spec.stdin_files:
            env["CRUCIBLE_STDIN_FILES"] = " ".join(spec.stdin_files)
        if spec.stdin_text:
            env["CRUCIBLE_PROMPT"] = spec.stdin_text
        if spec.transcript_path:
            env["CRUCIBLE_TRANSCRIPT"] = spec.transcript_path
        return ["bash", "-o", "pipefail", "-c", LAUNCH_WRAPPER, "crucible-launch", *argv], env

    def _launch_context(
        self, spec: LaunchSpec, *, credential_mounted: bool | None = None
    ) -> LaunchContext:
        if credential_mounted is None:
            adapter = self.harnesses.get(
                "hermes" if spec.harness == "codex" and spec.endpoint == "local" else spec.harness
            )
            credential = adapter.credential_spec() if adapter is not None else None
            credential_mounted = bool(spec.env_from_files) or (
                self._credential_copy(spec) is not None
                and bool(credential and credential.required_for_launch)
            )
        copy = self._credential_copy(spec)
        return LaunchContext(
            attempt_id=spec.attempt_id,
            model=spec.model,
            effort=spec.effort,
            timeout_seconds=spec.timeout_seconds,
            identity_mount=IDENTITY_MOUNT,
            report_mount=REPORT_MOUNT,
            repo_mount=REPO_MOUNT,
            credential_mounted=credential_mounted,
            credential_mode=copy.mode if copy is not None else None,
            endpoint=spec.endpoint,
            endpoint_url=spec.endpoint_url,
            command_timeout_ms=spec.command_timeout_ms,
            harness_settings=spec.harness_settings,
        )

    # ----- the network policy (26) --------------------------------------

    def _egress_plan(self, spec: LaunchSpec, role: str) -> EgressPlan:
        """What each role may reach (26), from the same allowlist source that generates
        the Squid configuration locally: the policy's `egress_allowlist`, the contract's
        `egress_extra`, the adapter's declared endpoints, and a local route's exact
        `endpoint_url` host and port (05b, 13, S6, S16)."""
        network_policy = str(spec.policy.get("network", {}).get("mode", "egress-proxy"))
        if spec.network == "none" or network_policy == "none":
            return EgressPlan()
        network = spec.policy.get("network", {})
        policy_hosts = [str(h) for h in (network.get("egress_allowlist") or [])]
        extra = [str(h) for h in (spec.contract.get("constraints", {}).get("egress_extra") or [])]
        if role == k8sspec.ROLE_WORKER:
            # The allowlist as written, GitHub included when the policy names it (hades
            # #425): the worker reaches exactly what the policy document says it may.
            wanted = tuple(
                egress_allowlist(
                    self.harnesses, spec.harness, policy_hosts, extra, spec.endpoint_url
                )
            )
        elif role in (
            k8sspec.ROLE_PREPARER,
            k8sspec.ROLE_CACHE_REFRESHER,
            k8sspec.ROLE_PUBLISHER,
        ):
            # 26: the preparer, the cache refresher and the publisher do the git
            # traffic, and nothing else, whether or not the policy names GitHub.
            wanted = ("api.github.com", "github.com")
        elif role == k8sspec.ROLE_LOGIN:
            # 26: "the harness's login endpoints only" (crucible#58). The adapter's
            # `endpoints` include its model API, which a login has no reason to reach.
            adapter = self.harnesses.get(spec.harness)
            wanted = tuple(sorted(adapter.capabilities().login_endpoints)) if adapter else ()
        elif role == k8sspec.ROLE_VERIFIER:
            # 26: the verifier gets the registries only when the policy says so. The
            # policy's own allowlist is that statement, as written (hades #425); the
            # harness endpoints are not part of it, because the verifier runs the
            # repository's commands and never a model.
            wanted = tuple(sorted(set(policy_hosts)))
        else:
            # Collector, bundle verifier, reader, cleaner: no egress at all.
            return EgressPlan()
        hosts = tuple(h for h in wanted if ":" not in h)
        endpoints = tuple(h for h in wanted if ":" in h)
        plan = EgressPlan(hosts=hosts, endpoints=endpoints)
        if role == k8sspec.ROLE_WORKER:
            plan = self._local_endpoint_plan(plan, spec.endpoint_url)
        return plan

    def _local_endpoint_plan(self, plan: EgressPlan, endpoint_url: str | None) -> EgressPlan:
        """Add the local model endpoint to a plan, in the form this cluster matches.

        Out of the cluster it is the URL's `host:port`, which `_resolve_plan` turns into
        exact addresses. In the cluster (the `kubernetes.egress` setting names its
        namespace) it is a selector on the gateway's pods and their port instead, and
        the `host:port` is taken out: it would resolve to a service address, which is
        inside a denied range and which a translating CNI never matches (crucible#91)."""
        if not endpoint_url:
            return plan
        parsed = urlsplit(endpoint_url)
        if not parsed.hostname:
            return plan
        url_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        destination = f"{parsed.hostname}:{url_port}"
        egress = self.config.egress
        if not egress.endpoint_in_cluster:
            if destination in plan.endpoints:
                return plan
            return replace(plan, endpoints=(*plan.endpoints, destination))
        return replace(
            plan,
            endpoints=tuple(e for e in plan.endpoints if e != destination),
            endpoint_selector=PeerSelector(egress.endpoint_namespace, egress.endpoint_pod_labels),
            endpoint_ports=(egress.endpoint_port or url_port,),
        )

    def _policy_body(
        self,
        name: str,
        object_labels: Mapping[str, str],
        attempt_id: str,
        role: str,
        plan: EgressPlan,
        pod_selector: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        egress = self.config.egress
        protected = self._protected()
        dns_selector = None
        if egress.dns_namespace:
            dns_selector = k8sspec.check_selector(
                PeerSelector(egress.dns_namespace, egress.dns_pod_labels),
                what="cluster DNS",
                protected_namespaces=protected,
            )
        if plan.endpoint_selector is not None:
            k8sspec.check_selector(
                plan.endpoint_selector, what="local endpoint", protected_namespaces=protected
            )
        return k8sspec.egress_policy(
            name=name,
            namespace=self.config.namespace,
            object_labels=object_labels,
            attempt_id=attempt_id,
            role=role,
            plan=plan,
            dns_server=self.config.cluster_dns_ip,
            denied_cidrs=self.config.denied_cidrs,
            dns_selector=dns_selector,
            pod_selector=pod_selector,
        )

    async def _apply_policy(
        self, spec: LaunchSpec, role: str, plan: EgressPlan, *, use_backoff: bool = False
    ) -> tuple[str | None, EgressPlan]:
        """One NetworkPolicy per attempt per role that needs egress.

        26 asks for "one policy per attempt selecting that attempt's pods, with the
        role-specific egress sets". A NetworkPolicy has one podSelector, so a single
        object per attempt would have to carry the union of every role's destinations,
        which would hand the worker GitHub and the collector the model endpoints. The
        selector therefore carries the role as well, and a role with no egress gets no
        policy at all: the namespace's default deny is already the answer for it.

        Returns the policy's name and the resolved plan, whose addresses the role's Pod
        is pinned to (`k8sspec.host_aliases`, hades #191)."""
        if plan.empty:
            return None, plan
        plan = await self._resolve_plan(plan, broad=self._broad_for(role))
        name = k8sspec.object_name(f"np-{role}", spec.attempt_id)
        body = self._policy_body(name, self._labels(spec, role), spec.attempt_id, role, plan)
        create = self._create_with_backoff if use_backoff else self._create
        await create("networkpolicies", body)
        return name, plan

    def _broad_for(self, role: str) -> bool:
        """Whether a role's policy may take the broad rule (`broad_egress`).

        Never for the worker or the verifier: their hosts are the policy's
        `egress_allowlist` as written (hades #425), and they run code the attempt
        controls. The broad rule is "the public internet on 443", so a worker whose
        allowlist named one host would reach every public address, which is what
        `test_isolation_probes_are_refused_on_kubernetes` caught once the worker's list
        stopped being empty. Their hosts are always resolved to addresses and pinned in
        the Pod's hosts file; the opt-out applies to the git and login roles, whose
        destinations are fixed by the provider rather than by the policy."""
        if role in (k8sspec.ROLE_WORKER, k8sspec.ROLE_VERIFIER):
            return False
        return self.config.broad_egress

    async def _resolve_plan(self, plan: EgressPlan, *, broad: bool | None = None) -> EgressPlan:
        """Turn the allowlist's names into the addresses a CIDR-only CNI can enforce.

        A name that does not resolve refuses the launch rather than being dropped or
        widened, which is the Docker provider's rule for the same situation: an attempt
        whose allowlist the egress path cannot actually permit is refused, never run
        with less network than the policy promised (13)."""
        resolved_endpoints: list[str] = []
        endpoint_unresolved: list[str] = []
        endpoint_forbidden: list[str] = []
        for endpoint in plan.endpoints:
            host, separator, port = endpoint.rpartition(":")
            if not separator or not host or not port.isdigit():
                raise k8sspec.SpecError(f"{endpoint!r} is not an address:port destination")
            try:
                parsed_address = ipaddress.ip_address(host)
            except ValueError:
                addresses = tuple(await self._call(self.resolve, host))
                if not addresses:
                    endpoint_unresolved.append(host)
                    continue
                for cidr in addresses:
                    network = ipaddress.ip_network(cidr)
                    address_text = str(network.network_address)
                    denied = k8sspec.denied_by(str(network), self.config.denied_cidrs)
                    explicitly_local = any(
                        _inside_declared_network(network, value)
                        for value in self.config.local_endpoint_cidrs
                    )
                    if denied is not None and not explicitly_local:
                        endpoint_forbidden.append(
                            f"{host} resolved to {address_text} inside {denied}"
                        )
                    resolved_endpoints.append(f"{address_text}:{port}")
            else:
                cidr = f"{parsed_address}/{32 if parsed_address.version == 4 else 128}"
                denied = k8sspec.denied_by(cidr, self.config.denied_cidrs)
                if denied is not None:
                    endpoint_forbidden.append(f"{host} inside {denied}")
                resolved_endpoints.append(endpoint)
        if endpoint_unresolved:
            raise ProviderError(
                "the configured local endpoint does not resolve to an address, so no "
                f"NetworkPolicy can permit it: {sorted(endpoint_unresolved)}"
            )
        if endpoint_forbidden:
            raise ProviderError(
                "the configured local endpoint names or resolves to an address this namespace "
                f"denies: {sorted(endpoint_forbidden)}"
            )
        plan = replace(plan, endpoints=tuple(dict.fromkeys(resolved_endpoints)))
        broad = self.config.broad_egress if broad is None else broad
        if broad or not plan.hosts:
            return replace(plan, broad=broad)
        cidrs: list[str] = []
        by_host: list[tuple[str, tuple[str, ...]]] = []
        unresolved: list[str] = []
        forbidden: list[str] = []
        now = time.monotonic()
        for host in plan.hosts:
            cached = self._resolved.get(host)
            if cached is not None and now - cached[0] < self.config.resolve_ttl_seconds:
                addresses = cached[1]
            else:
                addresses = tuple(await self._call(self.resolve, host))
                self._resolved[host] = (now, addresses)
            if not addresses:
                unresolved.append(host)
            for address in addresses:
                # 26: the API server, the node network, other namespaces, link-local and
                # the lab's private ranges are denied. A name that resolves into one of
                # them would otherwise become an allow rule for exactly the destination
                # the policy denies, whether by a vendor's split-horizon record, a CNAME
                # change, or a poisoned resolver. It refuses the launch.
                denied = k8sspec.denied_by(address, self.config.denied_cidrs)
                if denied is not None:
                    forbidden.append(f"{host} -> {address} inside {denied}")
            cidrs.extend(addresses)
            by_host.append((host, tuple(addresses)))
        if unresolved:
            raise ProviderError(
                "the egress allowlist names hosts that do not resolve to an address, so "
                f"no NetworkPolicy can permit them: {sorted(unresolved)}"
            )
        if forbidden:
            raise ProviderError(
                "the egress allowlist resolves into ranges this namespace denies, so no "
                f"NetworkPolicy may permit it: {sorted(forbidden)}"
            )
        return replace(plan, cidrs=tuple(dict.fromkeys(cidrs)), host_addresses=tuple(by_host))

    # ----- credentials (12) ---------------------------------------------

    def _credential_copy(self, spec: LaunchSpec) -> _CredentialCopy | None:
        adapter = self.harnesses.get(
            "hermes" if spec.harness == "codex" and spec.endpoint == "local" else spec.harness
        )
        credential = adapter.credential_spec() if adapter is not None else None
        if credential is None:
            return None
        # An optional credential whose Secret is absent is not seeded (see
        # `_seed_credential`), which keeps the adapter's unauthenticated fallback.
        secret_name = self.config.credential_secret_name(credential.harness)
        configured = (
            MountMode(spec.credential_mode)
            if spec.credential_mode is not None
            else self.config.credential_modes.get(credential.harness)
        )
        mode = configured or (
            MountMode.RENEWER if credential.harness == "codex" else credential.minimum_mode
        )
        if spec.harness == "codex" and spec.endpoint == "local":
            mode = MountMode.RO
        return _CredentialCopy(
            spec=worker_credential_spec(credential) if mode is MountMode.RENEWER else credential,
            source_secret=secret_name,
            mode=mode,
        )

    async def _credential_keys(self, attempt_id: str) -> list[str]:
        """Which auth files the per-attempt Secret actually holds, read back rather than
        assumed, so a restart between `prepare` and `launch` still projects the truth.

        A missing Secret (404) is absence: `[]`. Any other failure is not, and is
        raised rather than folded into absence, so a required credential does not
        read a transient API error as "no keys" and launch unauthenticated."""
        try:
            body = await self._call_with_backoff(
                self.client.get, "secrets", k8sspec.object_name("cred", attempt_id)
            )
        except KubernetesApiError as exc:
            if exc.status == 404:
                return []
            raise
        return [str(key) for key in (body.get("data") or {})]

    async def _seed_credential(
        self, spec: LaunchSpec, copy: _CredentialCopy, *, use_backoff: bool = False
    ) -> None:
        """26: per attempt, copy the harness Secret into `cred-<attempt>`.

        Only the named auth files, never the whole Secret: a harness Secret can hold
        state the adapter did not declare, and a copy is the narrowest thing that can
        authenticate (12). A required file that is missing refuses the launch rather
        than seeding a copy that cannot.

        A login running for the same harness does not stop the seeding: the supervisor
        holds a launch back while one runs, and a launch that slipped past that check
        holds a copy of the credential as it stands, which the login then declines to
        replace (12). Nothing an attempt holds is ever mixed with a new session."""
        try:
            call = self._call_with_backoff if use_backoff else self._call
            source = await call(self.client.get, "secrets", copy.source_secret)
        except KubernetesApiError as exc:
            if not copy.spec.required_for_launch and exc.status == 404:
                return
            raise HarnessRefusedError(
                f"refusing to launch: the credential Secret {copy.source_secret!r} for harness "
                f"{spec.harness!r} is not readable in {self.config.namespace} ({exc.status})"
            ) from exc
        present_spec = copy.spec
        codex_adapter = self.harnesses.get("codex")
        if copy.mode is MountMode.RENEWER and codex_adapter is not None:
            original = codex_adapter.credential_spec()
            if original is not None:
                present_spec = original
        if not _has_declared_auth_file(present_spec, source):
            if not copy.spec.required_for_launch:
                return
            raise HarnessRefusedError(
                f"refusing to launch: the credential Secret {copy.source_secret!r} is empty: "
                f"missing its auth file {copy.spec.auth_files[0].name!r}"
            )
        data = source.get("data") or {}
        payload: dict[str, bytes] = {}
        if copy.mode is MountMode.RENEWER:
            raw_login = data.get(_secret_key("auth.json"))
            if raw_login is None:
                raise HarnessRefusedError(
                    "refusing to launch: the Codex credential Secret has no auth.json"
                )
            login = json.loads(base64.b64decode(str(raw_login)))
            value = json.dumps(access_token_document(login), separators=(",", ":")).encode()
            payload[_secret_key("access-token.json")] = value
            copy.seeded["access-token.json"] = hashlib.sha256(value).hexdigest()
        else:
            for auth in copy.spec.auth_files:
                key = _secret_key(auth.name)
                raw = data.get(key)
                if raw is None or len(raw) == 0:
                    if auth.required:
                        raise HarnessRefusedError(
                            f"refusing to launch: the credential Secret {copy.source_secret!r} is "
                            f"missing its auth file {auth.name!r}"
                        )
                    copy.seeded[auth.name] = None
                    continue
                value = base64.b64decode(str(raw))
                payload[key] = value
                copy.seeded[auth.name] = hashlib.sha256(value).hexdigest()
        create = self._create_with_backoff if use_backoff else self._create
        await create(
            "secrets",
            k8sspec.secret(
                name=k8sspec.object_name("cred", spec.attempt_id),
                namespace=self.config.namespace,
                object_labels=self._labels(spec, k8sspec.ROLE_WORKER),
                data=payload,
            ),
        )

    async def refresh_credential_projection(self, document: Mapping[str, str]) -> None:
        """Patch every live Codex attempt Secret for kubelet to project."""
        encoded = base64.b64encode(
            json.dumps(dict(document), separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        calls = []
        for attempt_id, launched in tuple(self._launched.items()):
            copy = launched.credential
            if copy is not None and copy.mode is MountMode.RENEWER:
                calls.append(
                    self._call(
                        self.client.patch,
                        "secrets",
                        k8sspec.object_name("cred", attempt_id),
                        {"data": {_secret_key("access-token.json"): encoded}},
                    )
                )
        if calls:
            await asyncio.gather(*calls)

    async def _sync_credential(
        self, h: Handle, spec: LaunchSpec, limits: Limits
    ) -> CredentialSync | None:
        """Read the rotated auth files back and write back only a valid, newer one (12).

        The copy is removed only after it was read back (lab findings of 2026-09-29): a
        read that failed raises CollectionUnavailableError and the supervisor collects
        again. A cluster that keeps failing the read-back does not keep the copy for as
        long as it fails: the supervisor's collection window ends, and cleanup removes it
        under every policy."""
        done = self._credential_syncs.get(h.attempt_id)
        if done is not None:
            # A collection that ran again after a later step failed: the copy was read
            # back, written back and removed the first time, and that is the outcome.
            return done
        launched = self._launched.get(h.attempt_id)
        copy = launched.credential if launched is not None else self._credential_copy(spec)
        if copy is None:
            return None
        files: list[CredentialFileSync] = []
        if copy.writable:
            paths = [f"{k8sspec.CREDENTIAL_LEAF}/{a.name}" for a in copy.spec.auth_files]
            try:
                read = await self._read_files(spec, paths, limits, limit=CREDENTIAL_READ_LIMIT)
            except ProviderError as exc:
                # 12: a copy that was never read back is not removed, because the token
                # the harness rotated into it may be the only live one. Collection runs
                # again; if it never can, cleanup removes the copy under every policy.
                raise CollectionUnavailableError(
                    f"the credential copy could not be read back: {exc}"
                ) from exc
            unreadable = [path for path in paths if read.get(path) is _UNREADABLE]
            if unreadable:
                # An exec the API server broke, or a stream that ended before its status:
                # "could not read" is not "read", so the copy stays for the next try.
                raise CollectionUnavailableError(
                    f"the credential copy could not be read back: {', '.join(unreadable)}"
                )
        try:
            if copy.writable:
                for auth in copy.spec.auth_files:
                    files.append(
                        await self._sync_file(
                            copy, auth, read.get(f"{k8sspec.CREDENTIAL_LEAF}/{auth.name}")
                        )
                    )
            else:
                files = [
                    CredentialFileSync(
                        auth.name, True, False, True, False, "read-only mount; nothing to sync"
                    )
                    for auth in copy.spec.auth_files
                ]
        finally:
            # Once read, removed on every path: a sync-back that raised must not keep
            # the copy on the claim.
            removed = await self._remove_credential(spec, copy)
        sync = CredentialSync(
            harness=copy.spec.harness,
            mount_mode=copy.mode.value,
            files=tuple(files),
            removed=removed,
            detail="" if copy.seeded else "seeded hashes unknown",
        )
        if removed:
            self._credential_syncs[h.attempt_id] = sync
        return sync

    async def _sync_file(
        self, copy: _CredentialCopy, auth: AuthFile, data: bytes | None
    ) -> CredentialFileSync:
        if data is None:
            return CredentialFileSync(auth.name, False, False, False, False, "absent after the run")
        if data is _TRUNCATED:
            return CredentialFileSync(
                auth.name, True, True, False, False, "changed; larger than the read limit"
            )
        if data is _UNREADABLE:
            # 12: the sync is a recorded outcome, never an exception past the removal,
            # and "the read failed" is not "the file was absent".
            return CredentialFileSync(
                auth.name, False, False, False, False, "read failed: the exec stream ended early"
            )
        seeded = copy.seeded.get(auth.name)
        changed = seeded is None or hashlib.sha256(data).hexdigest() != seeded
        if not changed:
            return CredentialFileSync(auth.name, True, False, True, False, "unchanged")
        if not auth.sync_back:
            return CredentialFileSync(
                auth.name, True, True, True, False, "changed; state, never written back"
            )
        from crucible.adapters.execution.docker import _issued_at  # noqa: PLC0415

        new_document: Any = None
        if auth.json:
            try:
                new_document = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                new_document = None
            if not isinstance(new_document, dict) or not all(
                key in new_document for key in auth.json_keys
            ):
                return CredentialFileSync(
                    auth.name, True, True, False, False, "changed; not the expected JSON shape"
                )
        if auth.issued_at is None:
            return CredentialFileSync(
                auth.name, True, True, True, False, "changed; no issued-at field to order by"
            )
        newer = _issued_at(new_document, auth.issued_at)
        if newer is None:
            return CredentialFileSync(
                auth.name, True, True, True, False, "changed; the copy carries no issued-at"
            )

        def secret_older(secret_body: Mapping[str, Any]) -> datetime | None:
            raw = (secret_body.get("data") or {}).get(_secret_key(auth.name))
            if not raw:
                return None
            doc: Any = None
            with contextlib.suppress(UnicodeDecodeError, ValueError):
                doc = json.loads(base64.b64decode(str(raw)).decode("utf-8"))
            return _issued_at(doc, auth.issued_at)

        try:
            source = await self._call(self.client.get, "secrets", copy.source_secret)
        except KubernetesApiError as exc:
            return CredentialFileSync(
                auth.name, True, True, True, False, f"changed; source unreadable: {exc.status}"
            )
        older = secret_older(source)
        if older is not None and newer <= older:
            return CredentialFileSync(
                auth.name, True, True, True, False, "changed; not newer than the source"
            )
        # hades #315: the patch carries the Secret's resourceVersion from the read
        # above, so the API server itself does the compare-and-swap (339) and answers
        # 409 when another attempt already wrote the Secret since this read. No shared
        # pathname is ever involved; this is the whole replace, atomically.
        # On conflict (409), re-read the Secret and retry the compare-and-swap when
        # this candidate is still newer rather than treating every 409 as proof that
        # the source moved past it.
        while True:
            resource_version = (source.get("metadata") or {}).get("resourceVersion")
            try:
                await self._call(
                    self.client.patch,
                    "secrets",
                    copy.source_secret,
                    {
                        "metadata": {"labels": _owned_labels(copy.spec.harness)},
                        "data": {_secret_key(auth.name): base64.b64encode(data).decode("ascii")},
                    },
                    resource_version=resource_version,
                )
                return CredentialFileSync(
                    auth.name, True, True, True, True, "changed; newer issued-at, written back"
                )
            except KubernetesApiError as exc:
                if exc.status != 409:
                    return CredentialFileSync(
                        auth.name,
                        True,
                        True,
                        True,
                        False,
                        f"changed; write back failed: {exc.status}",
                    )
                try:
                    source = await self._call(self.client.get, "secrets", copy.source_secret)
                except KubernetesApiError as read_exc:
                    return CredentialFileSync(
                        auth.name,
                        True,
                        True,
                        True,
                        False,
                        f"changed; source unreadable: {read_exc.status}",
                    )
                older = secret_older(source)
                if older is not None and newer <= older:
                    return CredentialFileSync(
                        auth.name,
                        True,
                        True,
                        True,
                        False,
                        "changed; source moved past the candidate, skipped",
                    )

    async def _remove_credential(self, spec: LaunchSpec, copy: _CredentialCopy) -> bool:
        removed = await self._delete_credential_secret(spec.attempt_id)
        if copy.writable:
            try:
                await self._remove_from_claim(spec, [k8sspec.CREDENTIAL_LEAF])
            except Exception as exc:
                log.warning("credential copy removal failed", extra={"error": str(exc)})
                return False
        return removed

    async def _delete_credential_secret(self, attempt_id: str) -> bool:
        try:
            await self._call(self.client.delete, "secrets", k8sspec.object_name("cred", attempt_id))
        except KubernetesApiError as exc:
            log.warning("per-attempt secret removal failed", extra={"error": str(exc)})
            return False
        return True

    # ----- running a role -----------------------------------------------

    async def _run_role_job(
        self,
        spec: LaunchSpec,
        *,
        role: str,
        image: str,
        script: str,
        mounts: Sequence[Mount],
        volumes: Sequence[Mapping[str, Any]],
        limits: Limits,
        timeout: int,
        plan: EgressPlan,
        env: Mapping[str, str] | None = None,
        cancelled: CancelCheck | None = None,
        tolerate_lingering_pod: bool = False,
        wait_for_quota: bool = False,
        use_backoff: bool = False,
        log_output: list[str] | None = None,
        adopt_existing: bool = False,
        preserve_on_cancel: bool = False,
        init_containers: Sequence[Mapping[str, Any]] = (),
        stall_seconds: int = 0,
        keep_log: list[str] | None = None,
    ) -> int:
        """Run one single-purpose Job to completion and delete it. With `cancelled`, a
        cancel ends the wait (hades #189): the Job and its policy are deleted on the way
        out, as on every other path. With `tolerate_lingering_pod`, a Pod the API server
        is slow to remove after its Job is deleted is logged rather than raised, so an
        exit already seen (the publisher's, after a push that cannot be undone) is not
        replaced by an error about garbage collection. With `wait_for_quota`, a Job whose
        Pod the namespace quota refuses waits for room until its deadline rather than
        ending at once: the publisher's Jobs, where giving up is a failed publication an
        operator has to retry, not a collection the supervisor tries again.

        With `stall_seconds` (hades #370), a Pod that has been Running that long without
        writing a log line is ended as a stall, JOB_STALLED, with the bound and its last
        output as the role's error, instead of waiting for the role's timeout.

        With `keep_log` (hades #370), a Job that exits non-zero, stalls or times out has
        its Pod's whole log, uncut, appended before the Job is deleted, so the caller
        keeps it as evidence; the role's error carries only the bounded tail. A timed-out
        Pod's last output is added to the timeout reason too."""
        key = (role, spec.attempt_id)
        name = k8sspec.object_name(OBJECT_PREFIX.get(role, role), spec.attempt_id)
        collection = role in COLLECTION_ROLES
        if key in self._collection_role_exits:
            await self._await_job_pods_gone(
                name, force=role == k8sspec.ROLE_VERIFIER, collection=collection
            )
            return self._collection_role_exits[key]
        policy_name: str | None = None
        if not adopt_existing:
            with contextlib.suppress(KubernetesApiError):
                await self._call(
                    self.client.delete,
                    "jobs",
                    name,
                    grace_period_seconds=0 if role == k8sspec.ROLE_VERIFIER else None,
                )
        interrupted = False
        failed = False
        try:
            policy_name, resolved_plan = await self._apply_policy(
                spec, role, plan, use_backoff=use_backoff
            )
            body = k8sspec.job(
                name=name,
                namespace=self.config.namespace,
                object_labels=self._labels(spec, role),
                pod=k8sspec.pod_spec(
                    PodRequest(
                        role=role,
                        image=image,
                        command=["sh", "-c", script],
                        limits=limits,
                        env=dict(env or {}),
                        mounts=[*k8sspec.base_mounts(), *mounts],
                        volumes=[*k8sspec.base_volumes(limits), *volumes],
                        service_account=self.config.service_account,
                        image_pull_secret=self.config.image_pull_secret,
                        host_aliases=k8sspec.host_aliases(resolved_plan),
                        init_containers=init_containers,
                    )
                ),
                # The Job's own deadline counts from its start, image pull included;
                # Crucible's wait counts the role's time from Running (lab findings of 2026-09-29).
                active_deadline_seconds=timeout + self.config.launch_timeout_seconds,
            )
            create = self._create_with_backoff if use_backoff else self._create
            await create("jobs", body)
        except (KubernetesApiError, SpecError) as exc:
            log.warning("%s Job failed", role, extra={"error": str(exc)})
            # A Job the API server refused for the namespace quota (a count limit) is
            # as much a wait as a Pod the Job controller could not create (hades #423).
            self._role_error(
                role,
                spec.attempt_id,
                str(exc),
                isinstance(exc, KubernetesUnavailableError) or _quota_refused(exc),
            )
            # A failed create may have reached the server. In particular, a probe
            # refusal has no later workspace cleanup to remove this Job or policy.
            with contextlib.suppress(KubernetesApiError):
                await self._call(self.client.delete, "jobs", name)
            if policy_name:
                with contextlib.suppress(KubernetesApiError):
                    await self._call(self.client.delete, "networkpolicies", policy_name)
            return JOB_API_ERROR
        self.role_errors.pop((role, spec.attempt_id), None)
        try:
            code = await self._await_job(
                name,
                timeout=timeout,
                cancelled=cancelled,
                wait_for_quota=wait_for_quota,
                stall_seconds=stall_seconds,
            )
            refusal = self._job_refusals.pop(name, None)
            stall = self._job_stalls.pop(name, None)
            if stall is not None:
                # hades #370: the Pod ran and wrote nothing for the stall bound. Its
                # last lines, if it wrote any, go with the reason; the Job is deleted
                # on the way out as on every other path.
                tail = (await self._job_tail(name)).strip()
                if keep_log is not None:
                    keep_log.append(await self._job_log(name))
                self._role_error(
                    role,
                    spec.attempt_id,
                    f"{stall}; its last output: {tail}" if tail else f"{stall}; it wrote nothing",
                )
                log.warning("%s Job stalled: %s", role, stall)
                return JOB_STALLED
            if code is None:
                if name in self._job_unanswered:
                    self._job_unanswered.discard(name)
                    self._role_error(
                        role,
                        spec.attempt_id,
                        f"the API server did not answer while the {role} Job ran",
                        True,
                    )
                    # An API error, not a timeout, so every collection step's
                    # unavailable check sees it and the attempt is collected again later.
                    return JOB_API_ERROR
                reason = refusal or f"the {role} Job did not finish within {timeout}s of running"
                if keep_log is not None and refusal is None:
                    # A Pod that ran to the timeout may have said why (a clone still
                    # reporting progress); it is read before the Job is deleted.
                    whole = await self._job_log(name)
                    keep_log.append(whole)
                    if whole.strip():
                        reason = f"{reason}; its last output: {whole.strip()[-4000:]}"
                self._role_error(role, spec.attempt_id, reason)
                return JOB_TIMED_OUT
            if code == JOB_API_ERROR and refusal is not None:
                # A full namespace is a wait, not a verdict on the attempt.
                self._role_error(role, spec.attempt_id, refusal, True)
                return JOB_API_ERROR
            if log_output is not None:
                pod = await self._pod_of(name)
                if pod is not None:
                    frames = await self._call(
                        self.client.pod_log,
                        str(pod["metadata"]["name"]),
                        container=k8sspec.CONTAINER_NAME,
                        timestamps=False,
                    )
                    log_output.append(
                        b"".join(frame.payload for frame in frames).decode("utf-8", "replace")
                    )
            if code != 0:
                tail = await self._job_tail(name)
                if role == "gate-probe":
                    checkout_tail = await self._job_tail(name, container="checkout")
                    if checkout_tail and checkout_tail != tail:
                        tail = "\n".join(part for part in (checkout_tail, tail) if part)
                if keep_log is not None:
                    keep_log.append(await self._job_log(name))
                self._role_error(role, spec.attempt_id, tail)
                log.warning("%s Job exited %s: %s", role, code, tail[-1000:])
            if collection and code >= 0:
                self._collection_role_exits[key] = code
            return code
        except asyncio.CancelledError:
            interrupted = failed = True
            raise
        except BaseException:
            failed = True
            raise
        finally:
            if not (interrupted and preserve_on_cancel):
                with contextlib.suppress(KubernetesApiError):
                    await self._call(
                        self.client.delete,
                        "jobs",
                        name,
                        grace_period_seconds=0 if role == k8sspec.ROLE_VERIFIER else None,
                    )
                try:
                    await self._await_job_pods_gone(
                        name, force=role == k8sspec.ROLE_VERIFIER, collection=collection
                    )
                except (ProviderError, KubernetesApiError) as exc:
                    # An error already in flight is the one to report, not the Pod.
                    if not (tolerate_lingering_pod or failed):
                        raise
                    log.warning("%s Pod outlived its Job", role, extra={"error": str(exc)})
                finally:
                    if policy_name:
                        with contextlib.suppress(KubernetesApiError):
                            await self._call(self.client.delete, "networkpolicies", policy_name)

    def _role_error(self, role: str, attempt_id: str, text: str, unavailable: bool = False) -> None:
        self.last_error[role] = text
        self.role_errors[(role, attempt_id)] = (text, unavailable)

    async def _run_verifier(
        self, spec: LaunchSpec, limits: Limits
    ) -> tuple[VerificationRun, ...] | None:
        checks = [
            (str(v.get("id")), str(v.get("command")))
            for v in spec.contract.get("required_verification", [])
            if str(v.get("kind", "command")) == "command"
        ]
        if not checks:
            return ()
        launched = self._launched.get(spec.attempt_id)
        code = await self._run_role_job(
            spec,
            role=k8sspec.ROLE_VERIFIER,
            image=launched.image_digest if launched else spec.image,
            script=scripts.verifier_script(checks),
            mounts=[
                Mount("ws", REPO_MOUNT, sub_path="output/tree"),
                Mount("ws", VERIFY_MOUNT, sub_path="verify"),
                Mount("ws", PACKAGE_CACHE_MOUNT, sub_path=VERIFIER_CACHE_LEAF),
            ],
            volumes=[self._claim_volume(spec.attempt_id)],
            limits=limits,
            env=PACKAGE_CACHE_ENV,
            timeout=self.config.verifier_timeout_seconds,
            plan=self._egress_plan(spec, k8sspec.ROLE_VERIFIER),
        )
        self._raise_if_unavailable(k8sspec.ROLE_VERIFIER, spec.attempt_id, code)
        # None means "the verifier could not be re-run": the caller marks every command
        # unverified rather than letting a gate read an exit nobody produced (11).
        return None if code in (JOB_TIMED_OUT, JOB_API_ERROR) else ()

    def _raise_if_unavailable(self, role: str, attempt_id: str, code: int) -> None:
        """A collection step the cluster could not take right now is collected again
        later, never recorded as a verdict on the attempt's work."""
        if code != JOB_API_ERROR:
            return
        text, unavailable = self.role_errors.get((role, attempt_id), ("", False))
        if unavailable:
            raise CollectionUnavailableError(f"the {role} Job could not run: {text}")

    # ----- the reader Pod ------------------------------------------------

    @contextlib.asynccontextmanager
    async def _reader(
        self,
        spec: LaunchSpec,
        limits: Limits,
        *,
        use_backoff: bool = False,
        collection: bool = True,
    ) -> Any:
        """A short-lived Pod with the workspace claim mounted read-only (26).

        The Crucible pods never mount a claim, so this is how everything a role wrote
        comes back. It carries the same pod shape as every other role, has no egress
        policy and therefore no network at all, and is deleted in the `finally`."""
        launched = self._launched.get(spec.attempt_id)
        image = launched.image_digest if launched else spec.image
        name = k8sspec.object_name(f"reader-{new_id().lower()[-8:]}", spec.attempt_id)[:63]
        body = k8sspec.bare_pod(
            name=name,
            namespace=self.config.namespace,
            object_labels=self._labels(spec, k8sspec.ROLE_READER),
            pod=k8sspec.pod_spec(
                PodRequest(
                    role=k8sspec.ROLE_READER,
                    image=image,
                    command=["sh", "-c", f"sleep {self.config.collector_timeout_seconds}"],
                    limits=limits,
                    mounts=[*k8sspec.base_mounts(), Mount("ws", WORK_MOUNT, read_only=True)],
                    volumes=[
                        *k8sspec.base_volumes(limits),
                        self._claim_volume(spec.attempt_id, read_only=True),
                    ],
                    service_account=self.config.service_account,
                    image_pull_secret=self.config.image_pull_secret,
                )
            ),
        )
        create = self._create_with_backoff if use_backoff else self._create
        await create("pods", body)
        failed = False
        try:
            if not await self._await_running(name, timeout=self.config.launch_timeout_seconds):
                raise CollectionUnavailableError(
                    f"the reader Pod for {spec.attempt_id} never became ready"
                )
            yield name
        except BaseException:
            failed = True
            raise
        finally:
            with contextlib.suppress(KubernetesApiError):
                await self._call(self.client.delete, "pods", name, grace_period_seconds=0)
            try:
                await self._await_pod_gone(name, collection=collection)
            except (ProviderError, KubernetesApiError) as exc:
                # A real collection error (output over the limit, a full disk) is the
                # one to report; the lingering Pod is cleared on the next collection.
                if not failed:
                    raise
                log.warning("reader Pod %s outlived a failed read: %s", name, exc)

    async def _read_files(
        self,
        spec: LaunchSpec,
        paths: Sequence[str],
        limits: Limits,
        *,
        limit: int = CREDENTIAL_READ_LIMIT,
        use_backoff: bool = False,
        collection: bool = True,
    ) -> dict[str, bytes]:
        """Read named files off the workspace claim through the reader Pod.

        The bytes come back on the exec stream and never through a Pod log: the kubelet
        writes logs to the node's disk, and a rotated credential there is exactly what
        12 forbids. Each file is checked for being a regular file and bounded before it
        is read, because a worker owns what it left at that path."""
        out: dict[str, bytes] = {}
        async with self._reader(
            spec, limits, use_backoff=use_backoff, collection=collection
        ) as pod:
            for path in paths:
                # A stream that ended before the command reported a status is
                # `_UNREADABLE`: "could not read" is not "the file was absent", and 12
                # records the difference.
                data = await self._exec_read(
                    pod, f"{WORK_MOUNT}/{path}", limit, use_backoff=use_backoff
                )
                if data is not None:
                    out[path] = data
        return out

    async def _read_workspace(
        self, spec: LaunchSpec, into: Path, limits: Limits
    ) -> dict[str, str | bool | None]:
        """The collected output and the verifier's logs, as a tar off the claim.

        `output/tree` is excluded: it is a git clone the verifier already ran against
        and nothing on the Crucible side reads it. The tar is written to a scratch file
        beside `into` as it arrives and extracted from there, so the supervisor never
        holds the archive in memory (lab findings of 2026-09-29).

        `output/changed-blobs` is excluded too (hades #398): the bundle carries each of
        those blobs already, and a second copy of a large binary change would push the
        archive past its bound. The reader streams them separately, through the secret
        scanner as they arrive, and what comes back is the verdict per object id."""
        archive = into.parent / f".{into.name}-collected.tar"
        try:
            async with self._reader(spec, limits) as pod:
                try:
                    with archive.open("wb") as sink:
                        result = await self._call(
                            self.client.pod_exec_to,
                            pod,
                            ["sh", "-c", _OUTPUT_TAR_SCRIPT],
                            sink,
                            container=k8sspec.CONTAINER_NAME,
                            limit=OUTPUT_READ_LIMIT,
                        )
                except OSError as exc:
                    # The supervisor's own disk (full, unwritable): a provider failure
                    # with a cause, not an exception every later tick meets again.
                    raise CollectionFailedError(
                        f"the collected output could not be written to local disk: {exc}"
                    ) from exc
                _check_read_back(result, OUTPUT_READ_LIMIT, "the collected output")
                scan = BlobTarScan(scripts.CHANGED_BLOBS_DIR)
                try:
                    blobs_result = await self._call(
                        self.client.pod_exec_to,
                        pod,
                        ["sh", "-c", _CHANGED_BLOBS_TAR_SCRIPT],
                        scan,
                        container=k8sspec.CONTAINER_NAME,
                        limit=CHANGED_BLOBS_READ_LIMIT,
                    )
                finally:
                    blobs = scan.close()
                _check_read_back(blobs_result, CHANGED_BLOBS_READ_LIMIT, "the changed blobs")
                if scan.error is not None:
                    raise CollectionFailedError(
                        f"the changed blobs could not be read back as a tar: {scan.error}"
                    )
            if not result.stdout_size:
                return blobs
            try:
                await asyncio.to_thread(_extract, archive, into)
            except OSError as exc:
                raise CollectionFailedError(
                    f"the collected output could not be extracted on local disk: {exc}"
                ) from exc
            return blobs
        finally:
            with contextlib.suppress(OSError):
                archive.unlink(missing_ok=True)

    async def _remove_from_claim(self, spec: LaunchSpec, leaves: Sequence[str]) -> None:
        """Remove leaves of the workspace claim through a Pod, never as this process.

        What a Pod made, a Pod removes: the claim belongs to the worker's uid and the
        Crucible process never mounts it (26)."""
        targets = " ".join(f'"{WORK_MOUNT}/{leaf}"' for leaf in leaves)
        code = await self._run_role_job(
            spec,
            role=k8sspec.ROLE_CLEANER,
            image=(
                self._launched.get(spec.attempt_id)
                or _Launched("", spec, spec.image, self._limits(spec))
            ).image_digest,
            script=f"rm -rf {targets} 2>/dev/null; exit 0\n",
            mounts=[Mount("ws", WORK_MOUNT)],
            volumes=[self._claim_volume(spec.attempt_id)],
            limits=self._limits(spec),
            timeout=self.config.role_timeout_seconds,
            plan=EgressPlan(),
        )
        if code != 0:
            # 12: a credential leaf that is still on the claim has not been removed,
            # whatever the caller would otherwise have recorded. The caller turns this
            # into `removed: False` and a log line rather than a silent success.
            raise ProviderError(
                f"the cleaner Job for {spec.attempt_id} exited {code}: "
                f"{self.last_error.get(k8sspec.ROLE_CLEANER, '')}"
            )

    # ----- waiting -------------------------------------------------------

    async def _await_job(
        self,
        name: str,
        *,
        timeout: int,
        cancelled: CancelCheck | None = None,
        start_timeout: int | None = None,
        wait_for_quota: bool = False,
        stall_seconds: int = 0,
    ) -> int | None:
        """Wait for a Job's Pod to terminate and return the container's exit code. A
        cancel raises LaunchCancelledError (hades #189).

        `timeout` is the role's own time and starts when its Pod is Running: pulling
        the image and waiting for a node are bounded by `start_timeout` (the launch
        timeout by default) instead, so a short role is never spent on a slow pull
        (lab findings of 2026-09-29). None is returned when either runs out. A Job the
        API server refused to create a Pod for because of the namespace quota ends the
        wait at once with JOB_API_ERROR, and `_job_refusals` says why; with
        `wait_for_quota` it waits for room instead, and a timeout names the quota.

        With `stall_seconds` (hades #370), the Pod's last log line is read on every poll
        once it is Running; a Pod whose last line has not changed for that long is a
        stall, the wait ends with None, and `_job_stalls` says why. A log read that
        fails is neither progress nor its absence."""
        start_wait = self.config.launch_timeout_seconds if start_timeout is None else start_timeout
        started = time.monotonic()
        deadline = started + start_wait + timeout
        running = False
        unanswered = False
        progress_at = started
        last_line: bytes | None = None
        self._job_refusals.pop(name, None)
        self._job_stalls.pop(name, None)
        self._job_unanswered.discard(name)
        while time.monotonic() < deadline:
            await _stop_if_cancelled(cancelled, f"while {name} ran")
            try:
                pod = await self._pod_of(name)
            except KubernetesApiError as exc:
                # A failed look is not an answer; ask again on the next poll.
                unanswered = isinstance(exc, KubernetesUnavailableError)
                await asyncio.sleep(self.config.poll_interval_seconds)
                continue
            unanswered = False
            if pod is not None:
                terminated = _terminated_state(pod.get("status") or {})
                if terminated is not None:
                    return int(terminated.get("exitCode", -1))
                phase = str((pod.get("status") or {}).get("phase", ""))
                if phase == "Failed":
                    return JOB_API_ERROR
                if phase == "Running" and not running:
                    running = True
                    deadline = time.monotonic() + timeout
                    progress_at = time.monotonic()
                elif not running and time.monotonic() - started > start_wait:
                    self._job_refusals[name] = (
                        f"the {name} Pod did not start within {start_wait}s: "
                        f"{self._pending_failure(pod).detail}"
                    )
                    return None
                if running and stall_seconds > 0:
                    line = await self._last_log_line(pod)
                    if line is not None and line != last_line:
                        last_line = line
                        progress_at = time.monotonic()
                    elif line is not None and time.monotonic() - progress_at >= stall_seconds:
                        self._job_stalls[name] = (
                            f"the {name} Pod wrote no log output for {stall_seconds}s while "
                            f"it ran (the stall bound, below its {timeout}s timeout)"
                        )
                        return None
            else:
                try:
                    job = await self._call(self.client.get, "jobs", name)
                except KubernetesApiError as exc:
                    if exc.status == 404:
                        return JOB_API_ERROR
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue
                if int((job.get("status") or {}).get("failed") or 0):
                    return JOB_API_ERROR
                refusal = await self._quota_refusal(name, job)
                if refusal is not None:
                    self._job_refusals[name] = refusal
                    if not wait_for_quota:
                        return JOB_API_ERROR
            await asyncio.sleep(self.config.poll_interval_seconds)
        if unanswered:
            self._job_unanswered.add(name)
        return None

    async def _last_log_line(self, pod: Mapping[str, Any]) -> bytes | None:
        """The last line of a Pod's log with the API server's timestamp, or None when it
        could not be read: what `_await_job` compares between polls to see whether the
        Pod is making progress (hades #370). One line, so the read costs the same for a
        clone that has printed for ten minutes as for one that has just begun."""
        name = str((pod.get("metadata") or {}).get("name") or "")
        try:
            frames = await self._call(
                self.client.pod_log,
                name,
                container=k8sspec.CONTAINER_NAME,
                timestamps=True,
                tail_lines=1,
            )
        except KubernetesApiError:
            return None
        return b"".join(frame.payload for frame in frames)

    async def _quota_refusal(self, job_name: str, job: Mapping[str, Any]) -> str | None:
        """Why the Job controller could not create this Job's Pod, when the namespace
        quota is the reason: a `FailedCreate` condition or event naming the quota. The
        controller retries such a Pod with backoff and never fails the Job, so without
        this a full namespace is a wait for the whole timeout that says nothing."""
        for condition in (job.get("status") or {}).get("conditions") or []:
            if not isinstance(condition, dict):
                continue
            text = f"{condition.get('reason', '')} {condition.get('message', '')}"
            if "FailedCreate" in text and _names_quota(text):
                return str(condition.get("message") or text).strip()
        # Role Job names repeat for an attempt, and an event outlives its Job by an
        # hour: only this incarnation's events count, by uid, so a refusal the last try
        # met does not end the next one before its Pod is even created.
        uid = str((job.get("metadata") or {}).get("uid") or "")
        selector = f"involvedObject.kind=Job,involvedObject.name={job_name},reason=FailedCreate"
        if uid:
            selector += f",involvedObject.uid={uid}"
        try:
            events = await self._call(self.client.list_objects, "events", field_selector=selector)
        except KubernetesApiError:
            # An events list the Role does not allow, or that failed, is not a refusal.
            return None
        for event in events:
            message = str(event.get("message") or "")
            if _names_quota(message):
                return f"the namespace quota refused the Pod: {message}"
        return None

    async def _job_deadline_exceeded(self, name: str) -> bool:
        """Whether the Job controller ended this Job for running past its
        `activeDeadlineSeconds`. The controller records the condition before it removes
        the Pod, so it is there by the time the Pod's end has been seen; a few looks
        cover an API server that answers from a lagging cache."""
        for look in range(3):
            if look:
                await asyncio.sleep(self.config.poll_interval_seconds)
            try:
                job = await self._call(self.client.get, "jobs", name)
            except KubernetesApiError:
                continue
            for condition in (job.get("status") or {}).get("conditions") or []:
                if (
                    isinstance(condition, dict)
                    and str(condition.get("type")) in ("Failed", "FailureTarget")
                    and str(condition.get("status")) == "True"
                    and str(condition.get("reason")) == "DeadlineExceeded"
                ):
                    return True
            if int((job.get("status") or {}).get("failed") or 0):
                return False
        return False

    async def _await_pod(self, name: str, *, timeout: int) -> str | None:
        """The Pod's terminal phase, or None when it has none by the deadline or is
        gone. A look that failed is not an answer: it is asked again until the deadline,
        as `_await_job` does, so one API server hiccup never reads as a canary that
        failed (lab findings of 2026-09-29)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pod = await self._call(self.client.get, "pods", name)
            except KubernetesApiError as exc:
                if exc.status == 404:
                    return None
                await asyncio.sleep(self.config.poll_interval_seconds)
                continue
            phase = str((pod.get("status") or {}).get("phase", ""))
            if phase in ("Succeeded", "Failed"):
                return phase
            await asyncio.sleep(self.config.poll_interval_seconds)
        return None

    async def _await_running(self, name: str, *, timeout: int) -> bool:
        """Whether the Pod reached Running by the deadline. A failed look is asked
        again, as in `_await_pod`; a Pod that is gone or ended first never will."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pod = await self._call(self.client.get, "pods", name)
            except KubernetesApiError as exc:
                if exc.status == 404:
                    return False
                await asyncio.sleep(self.config.poll_interval_seconds)
                continue
            phase = str((pod.get("status") or {}).get("phase", ""))
            if phase == "Running":
                return True
            if phase in ("Succeeded", "Failed"):
                return False
            await asyncio.sleep(self.config.poll_interval_seconds)
        return False

    # ----- small API helpers ---------------------------------------------

    async def _create(self, kind: str, body: Mapping[str, Any]) -> None:
        try:
            await self._call(self.client.create, kind, body)
        except KubernetesApiError as exc:
            if exc.status != 409:
                raise

    async def _pod_of(self, job_name: str) -> dict[str, Any] | None:
        """The Pod of a Job, or None when the namespace genuinely has none.

        It does not swallow a transport failure. A 503 from the API server, a reset
        connection or a timed-out list is "Crucible could not look", and answering None
        would make `observe` read that as "the Pod is gone", which is `lost` and
        terminal (16): a healthy worker would be failed and retried while the original
        Pod kept running. The caller decides; the ones that do not care suppress."""
        rows = await self._call(
            self.client.list_objects, "pods", label_selector=f"job-name={job_name}"
        )
        live = [row for row in rows if not (row.get("metadata") or {}).get("deletionTimestamp")]
        candidates = live or rows
        return (
            max(
                candidates,
                key=lambda row: str((row.get("metadata") or {}).get("creationTimestamp") or ""),
            )
            if candidates
            else None
        )

    async def _clear_collection_pods(self, attempt_id: str) -> None:
        """Finish earlier cleanup before any collection step touches the workspace.

        Listing by attempt also covers reader Pods and a provider restart, when the
        process has forgotten which deletion was pending. An API refusal other than
        unavailability (403, 409) is logged and passed over, as the pre-delete before
        each Job always did; only a Pod seen to linger defers collection.
        """
        try:
            for role in COLLECTION_ROLES:
                name = k8sspec.object_name(OBJECT_PREFIX[role], attempt_id)
                force = role == k8sspec.ROLE_VERIFIER
                await self._delete_logged("jobs", name, grace_period_seconds=0 if force else None)
                with self._logged_refusal("waiting for Job Pods", name):
                    await self._await_job_pods_gone(name, force=force, collection=True)
            rows: list[dict[str, Any]] = []
            with self._logged_refusal("listing reader Pods", attempt_id):
                rows = await self._call(
                    self.client.list_objects,
                    "pods",
                    label_selector=k8sspec.selector(
                        **{
                            k8sspec.LABEL_ATTEMPT: attempt_id,
                            k8sspec.LABEL_ROLE: k8sspec.ROLE_READER,
                        }
                    ),
                )
            for row in rows:
                name = str(row["metadata"]["name"])
                await self._delete_logged("pods", name, grace_period_seconds=0)
                with self._logged_refusal("waiting for a reader Pod", name):
                    await self._await_pod_gone(name, collection=True)
        except KubernetesUnavailableError as exc:
            raise CollectionUnavailableError(
                f"collection cleanup could not reach the API: {exc}"
            ) from exc

    @contextlib.contextmanager
    def _logged_refusal(self, what: str, name: str) -> Iterator[None]:
        """Log an API refusal that is not unavailability instead of raising it."""
        try:
            yield
        except KubernetesUnavailableError:
            raise
        except KubernetesApiError as exc:
            log.warning("%s for %s was refused: %s", what, name, exc)

    async def _delete_logged(
        self, kind: str, name: str, *, grace_period_seconds: int | None = None
    ) -> None:
        with self._logged_refusal(f"deleting {kind}", name):
            await self._call(
                self.client.delete, kind, name, grace_period_seconds=grace_period_seconds
            )

    @staticmethod
    def _deletion_timeout(pod: Mapping[str, Any]) -> float:
        grace = (pod.get("spec") or {}).get("terminationGracePeriodSeconds")
        return (
            float(grace if grace is not None else DEFAULT_POD_GRACE_SECONDS)
            + POD_DELETION_MARGIN_SECONDS
        )

    async def _await_pod_gone(
        self, name: str, *, timeout: float = 15, collection: bool = False
    ) -> None:
        """Wait for an asynchronous Pod deletion before recording workspace state.

        A collection Pod (hades #361) is waited on for at least its grace period plus a
        margin, and running out defers collection to the next tick rather than failing
        it. Every other Pod keeps the short wait and fails as the environment."""
        started = time.monotonic()
        if collection:
            timeout = max(timeout, self._deletion_timeout({}))
        unavailable = False
        while time.monotonic() < started + timeout:
            try:
                pod = await self._call(self.client.get, "pods", name)
                if collection:
                    timeout = max(timeout, self._deletion_timeout(pod))
                unavailable = False
            except KubernetesUnavailableError:
                unavailable = True
            except KubernetesApiError as exc:
                if exc.status == 404:
                    return
                raise
            await asyncio.sleep(self.config.poll_interval_seconds)
        if not collection:
            raise ProviderError(f"Pod {name!r} was still present after {timeout:g} seconds")
        reason = "the API was unavailable" if unavailable else "it was still present"
        raise CollectionPendingError(
            f"Pod {name!r} deletion was not confirmed after {timeout:g} seconds: {reason}"
        )

    async def _await_job_pods_gone(
        self,
        job_name: str,
        *,
        timeout: float = 15,
        force: bool = False,
        collection: bool = False,
    ) -> None:
        """Wait for background Job propagation to remove its Pod; with `force`, delete
        the Pods with no grace (verifier Pods hold no state). `collection` waits as
        `_await_pod_gone` does for a collection Pod."""
        started = time.monotonic()
        if collection:
            timeout = max(timeout, self._deletion_timeout({}))
        unavailable = False
        while time.monotonic() < started + timeout:
            try:
                rows = await self._call(
                    self.client.list_objects, "pods", label_selector=f"job-name={job_name}"
                )
                if not rows:
                    return
                for row in rows:
                    if collection:
                        timeout = max(timeout, self._deletion_timeout(row))
                    if force:
                        # A Job's DeleteOptions do not set its dependent Pods' grace.
                        await self._delete_logged(
                            "pods", str(row["metadata"]["name"]), grace_period_seconds=0
                        )
                unavailable = False
            except KubernetesUnavailableError:
                unavailable = True
            await asyncio.sleep(self.config.poll_interval_seconds)
        if not collection:
            raise ProviderError(
                f"Pods for Job {job_name!r} were still present after {timeout:g} seconds"
            )
        reason = "the API was unavailable" if unavailable else "Pods were still present"
        raise CollectionPendingError(
            f"Pods for Job {job_name!r} deletion was not confirmed "
            f"after {timeout:g} seconds: {reason}"
        )

    async def _job_log(self, job_name: str) -> str:
        """hades #370: the whole log of a Job's Pod, with no line or byte bound, for the
        evidence a caller keeps; `_job_tail` is the bounded end for a message. A Pod or a
        read that is gone is an empty log, never an error over the one in flight."""
        try:
            pod = await self._pod_of(job_name)
            if pod is None:
                return ""
            frames = await self._call(
                self.client.pod_log,
                str((pod.get("metadata") or {}).get("name") or ""),
                container=k8sspec.CONTAINER_NAME,
                timestamps=False,
            )
        except KubernetesApiError:
            return ""
        return b"".join(f.payload for f in frames).decode("utf-8", "replace")

    async def _job_tail(
        self, job_name: str, limit: int = 4000, container: str = k8sspec.CONTAINER_NAME
    ) -> str:
        try:
            pod = await self._pod_of(job_name)
        except KubernetesApiError:
            return ""
        if pod is None:
            return ""
        name = str((pod.get("metadata") or {}).get("name") or "")
        try:
            frames = await self._call(
                self.client.pod_log,
                name,
                container=container,
                timestamps=False,
                tail_lines=JOB_TAIL_LINES,
            )
        except KubernetesApiError:
            return ""
        return b"".join(f.payload for f in frames).decode("utf-8", "replace")[-limit:]

    async def _worker_tails(self, h: Handle) -> tuple[str, str]:
        """Kubernetes merges a Pod's two streams, so the whole tail is reported as
        stdout and the stderr tail is empty. Classification reads both (S5)."""
        body = await self._job_tail(h.ref, limit=self.config.log_tail_bytes)
        return body, ""

    async def _workspace_state(self, attempt_id: str) -> WorkspaceState:
        """11: nothing labelled for this attempt is still running once the collector and
        the verifier are gone."""
        try:
            rows = await self._call(
                self.client.list_objects,
                "pods",
                label_selector=k8sspec.selector(**{k8sspec.LABEL_ATTEMPT: attempt_id}),
            )
        except KubernetesApiError as exc:
            return WorkspaceState(checked=False, detail=str(exc))
        leftover = tuple(
            sorted(
                str((row.get("metadata") or {}).get("name", ""))
                for row in rows
                if str((row.get("status") or {}).get("phase", "")) in ("Pending", "Running")
                and str(((row.get("metadata") or {}).get("labels") or {}).get(k8sspec.LABEL_ROLE))
                != k8sspec.ROLE_WORKER
            )
        )
        return WorkspaceState(leftover=leftover)

    async def _delete_by_label(self, kinds: Sequence[str], *, attempt_id: str) -> None:
        for kind in kinds:
            try:
                rows = await self._call(
                    self.client.list_objects,
                    kind,
                    label_selector=k8sspec.selector(**{k8sspec.LABEL_ATTEMPT: attempt_id}),
                )
            except KubernetesApiError:
                continue
            for row in rows:
                name = str((row.get("metadata") or {}).get("name", ""))
                if name:
                    with contextlib.suppress(KubernetesApiError):
                        await self._call(self.client.delete, kind, name)

    async def _delete_attempt_objects(self, attempt_id: str) -> None:
        await self._delete_by_label(
            ("jobs", "pods", "networkpolicies", "secrets", "configmaps", "persistentvolumeclaims"),
            attempt_id=attempt_id,
        )

    async def worker_capacity(self) -> WorkerCapacity:
        """hades #423: how many worker Pods may run at once, read from the namespace
        quota now (the last reading when the API server does not answer), with the
        short-role reservation taken out. The supervisor holds launches to this number
        and a launch past it waits for a worker to finish, so the quota is never what
        refuses a gate probe or a worker. Without a quota, `kubernetes.max_concurrency`."""
        try:
            await self._refresh_quota()
        except KubernetesApiError as exc:
            log.warning("the namespace quota could not be read: %s", exc)
        return self.capacity_view()

    def capacity_view(self) -> WorkerCapacity:
        """The capacity as last read, without an API call (the admin pages read this)."""
        if self._quota is not None:
            return self._quota
        return WorkerCapacity(
            workers=self.config.max_concurrency,
            source=(
                "kubernetes.max_concurrency (no ResourceQuota in the namespace; "
                f"shape from the active policy: "
                f"{self._fallback_shape().cpu_request} CPU / "
                f"{self._fallback_shape().memory_request} memory)"
            ),
            reserved_pods=self.config.short_role_pods,
            detail=(
                f"shape from the active policy: "
                f"{self._fallback_shape().cpu_request} CPU / "
                f"{self._fallback_shape().memory_request} memory; "
                f"no ResourceQuota names a counted resource in {self.config.namespace}; "
                f"the configured fallback of {self.config.max_concurrency} applies"
            ),
        )

    async def _refresh_quota(self) -> None:
        self._quota = await self._read_quota()
        self._quota_concurrency = self._quota.workers if self._quota is not None else None

    async def _read_quota(self) -> WorkerCapacity | None:
        """26, hades #423: worker capacity from the namespace's ResourceQuotas. For each
        counted resource, what the quota admits with nothing held back (the headroom: an
        attempt is five Jobs over its life and one Pod at a time, so `count/jobs.batch`
        is divided by five and the CPU and memory limits by one Pod's worth at the
        limits of the last launch, the defaults before one), minus the reservation for
        the short-role Pods Hades runs beside workers (`short_role_pods` Pods of the
        largest short-role shape, the worker's own). The fewest workers any resource
        then admits is the capacity. A quota that admits at least one Pod admits at
        least one worker: a lone attempt's probe, preparer and worker run one after
        another and never meet. Before the lab findings of 2026-09-29 only the Job
        count was read; before hades #423 nothing was reserved, so with workers at
        capacity every probe was refused. None when no quota names a counted resource."""
        rows = await self._call(self.client.list_objects, "resourcequotas")
        limits = k8sspec.limits_from_policy(self._policy)
        reserved = max(0, self.config.short_role_pods - await self._active_short_role_pods())
        # Per attempt, and per reserved short-role Pod (one Job, one Pod, the worker's
        # shape), for each resource a quota may count.
        per_attempt: dict[str, tuple[float, float]] = {
            "count/jobs.batch": (float(JOBS_PER_ATTEMPT), 1.0),
            "pods": (1.0, 1.0),
            "requests.cpu": (k8sspec.quantity(limits.cpu_request) or 0.0,) * 2,
            "limits.cpu": (k8sspec.quantity(limits.cpu) or 0.0,) * 2,
            "requests.memory": (k8sspec.quantity(limits.memory_request) or 0.0,) * 2,
            "limits.memory": (k8sspec.quantity(limits.memory) or 0.0,) * 2,
        }
        headroom: int | None = None
        workers: int | None = None
        binding = ""
        names: list[str] = []
        for row in rows:
            name = str((row.get("metadata") or {}).get("name") or "")
            hard = (row.get("spec") or {}).get("hard") or {}
            for key, (each, each_reserved) in per_attempt.items():
                total = k8sspec.quantity(hard.get(key)) if key in hard else None
                if total is None or each <= 0:
                    continue
                if name and name not in names:
                    names.append(name)
                fits = int(total // each)
                headroom = fits if headroom is None else min(headroom, fits)
                left = total - each_reserved * reserved
                fits_workers = int(left // each) if left > 0 else 0
                if fits >= 1:
                    fits_workers = max(1, fits_workers)
                if workers is None or fits_workers < workers:
                    workers = fits_workers
                    binding = f"{name} {key}".strip()
        if headroom is None or workers is None:
            return None
        policy_name = self._policy.get("name") if isinstance(self._policy, dict) else None
        policy_version = self._policy.get("version") if isinstance(self._policy, dict) else None
        if policy_name is not None and policy_version is not None:
            limits_source = f"policy {policy_name} v{policy_version}"
        elif policy_name is not None:
            limits_source = f"policy {policy_name}"
        else:
            limits_source = "the active policy"
        shape_source = (
            f"policy {policy_name} v{policy_version} "
            f"{limits.cpu_request} CPU / {limits.memory_request} memory"
            if policy_name is not None and policy_version is not None
            else f"{limits.cpu_request} CPU / {limits.memory_request} memory from {limits_source}"
        )
        shape = {
            "cpu": limits.cpu,
            "memory": limits.memory,
            "cpu_request": limits.cpu_request,
            "memory_request": limits.memory_request,
            "jobs": 1,
        }
        return WorkerCapacity(
            workers=workers,
            source=f"ResourceQuota {', '.join(names)}; {binding} binds; shape from {limits_source}",
            headroom=headroom,
            reserved_pods=reserved,
            reservation={"pods": reserved, "each": shape},
            detail=(
                f"shape from {shape_source}; the quota admits {headroom} Pod(s) of the worker's "
                f"shape; {reserved} kept for Hades's short-role Pods (gate probe, collector, "
                f"canary, login, preparer) leaves {workers} worker(s) at once"
            ),
        )

    async def _active_short_role_pods(self) -> int:
        """Count quota-consuming Hades Pods that already satisfy the reservation.

        ResourceQuota usage already includes these Pods. Holding back their shape again
        would count a hanging preparer, collector, probe, canary, or login twice and can
        prevent a worker from launching even though the namespace has room for it.
        Terminal Pods no longer consume pod CPU and memory quota, so they do not count.
        """
        rows = await self._call(self.client.list_objects, "pods")
        short_roles = {
            "gate-probe",
            k8sspec.ROLE_PREPARER,
            k8sspec.ROLE_COLLECTOR,
            k8sspec.ROLE_BUNDLE,
            k8sspec.ROLE_VERIFIER,
            k8sspec.ROLE_CANARY,
            k8sspec.ROLE_LOGIN,
        }
        return sum(
            1
            for row in rows
            if str((row.get("status") or {}).get("phase") or "") in ("Pending", "Running")
            and str(((row.get("metadata") or {}).get("labels") or {}).get(k8sspec.LABEL_ROLE) or "")
            in short_roles
        )


# ----- pure helpers -------------------------------------------------------


def _canary_failed(detail: str, exc: KubernetesApiError) -> NamespaceProbe:
    return NamespaceProbe(
        False,
        False,
        None,
        detail,
        checked=False,
        unavailable=isinstance(exc, KubernetesUnavailableError),
    )


def _names_quota(text: str) -> bool:
    """Whether an API server message is a ResourceQuota refusal: "exceeded quota: ..."
    when the namespace is full, "failed quota: ..." when a Pod leaves out a resource the
    quota counts."""
    lowered = text.lower()
    return "exceeded quota" in lowered or "failed quota" in lowered


def _last_lines(text: str, *, lines: int = 12, limit: int = 800) -> str:
    """The end of a role's output, for a message that has to fit an attempt's detail and
    a wake (hades #370): the last `lines` lines and no more than `limit` characters of
    them, cut from the front, so what the role said last is what the reader sees."""
    kept = "\n".join(text.strip().splitlines()[-lines:])
    return kept if len(kept) <= limit else "..." + kept[-limit:]


def _quota_refused(exc: BaseException) -> bool:
    """hades #423: whether the API server itself refused a create for the namespace
    quota (a 403 Forbidden whose message names it), as it does for a bare Pod, a claim
    or a Job under a count limit. The Job controller's refusal of a Job's Pod arrives
    as a `FailedCreate` event instead (`_quota_refusal`)."""
    return isinstance(exc, KubernetesApiError) and exc.status == 403 and _names_quota(str(exc))


async def _stop_if_cancelled(cancelled: CancelCheck | None, where: str) -> None:
    if cancelled is not None and await cancelled():
        raise LaunchCancelledError(f"the task was cancelled {where}")


def _latest_stamp(lines: bytes) -> datetime | None:
    latest: datetime | None = None
    for raw in lines.split(b"\n"):
        stamp = raw.partition(b" ")[0].decode("utf-8", "replace")
        with contextlib.suppress(ValueError):
            seen = parse_rfc3339(stamp)
            latest = seen if latest is None or seen > latest else latest
    return latest


def _skip_crowded_second(payload: bytes, limit: int, since: LogOffset) -> list[LogChunk]:
    """More than `limit` bytes of log fall inside one second, so no read `sinceTime`
    can express gets past it (issue 63). The resume moves to the start of the next
    second with one notice line in the log saying so; the lines of that second beyond
    the ceiling are not stored.

    The crowded second is the latest one among the read's whole lines, all of which
    were already stored; the line the cap cut belongs to a second that may be perfectly
    readable. Only a read with no whole line at all (one line longer than the ceiling)
    takes the cut line's second, and a read with no readable stamp the stored
    position's, so every skip moves the position forward."""
    whole, _, cut = payload.rpartition(b"\n")
    latest = _latest_stamp(whole) or _latest_stamp(cut)
    if latest is None and since.timestamp:
        with contextlib.suppress(ValueError):
            latest = parse_rfc3339(since.timestamp)
    if latest is None:
        log.warning("a capped log read carried no timestamp; the next poll tries again")
        return []
    resume = latest.replace(microsecond=0) + timedelta(seconds=1)
    notice = (
        f"[crucible] log lines skipped: more than {limit} bytes of this log fall within "
        "one second, more than one read takes; the log resumes at the next second"
    ).encode()
    return [
        LogChunk(
            stream="stdout",
            content=notice + b"\n",
            ts=resume,
            # The notice is not a line of the log: the next read keeps everything at or
            # after `resume`, including a line stamped in its first microsecond.
            line_sha256=RESUME_AT_BOUNDARY,
            occurrence=0,
            lines=1,
        )
    ]


def _observe_limits(launched: _Launched, pod: Mapping[str, Any]) -> None:
    """Take the limits from the live Pod once it is seen (issues 66, 76)."""
    spec = pod.get("spec") or {}
    if spec.get("containers"):
        launched.limits = k8sspec.limits_from_pod(spec, launched.limits)
        launched.limits_source = "pod"


def _seconds_until(timestamp: str) -> float:
    """Seconds from now until an RFC 3339 UTC stamp; 0 for one that is past or that
    does not parse, so a lock whose expiry is unreadable is reclaimable."""
    try:
        moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    return max(0.0, moment.timestamp() - time.time())


def _minutes(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute" + ("" if minutes == 1 else "s")


def _age_seconds(timestamp: str) -> float:
    """How long ago an object was created, from its RFC 3339 `creationTimestamp`."""
    if not timestamp:
        return 0.0
    try:
        return max(0.0, (datetime.now(UTC) - parse_rfc3339(timestamp)).total_seconds())
    except ValueError:
        return 0.0


def _resolve_host(host: str) -> list[str]:
    """Every IPv4 address a name resolves to, as /32 CIDRs. IPv6 is never emitted: no
    rule of this provider's policies carries a v6 block, so a v6 destination is denied
    by never appearing (26)."""
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({f"{info[4][0]}/32" for info in infos})


# Returned by `_read_files` for a path that was larger than the read limit. A distinct
# object so "the file was too big" is never confused with "the file held these bytes".
_TRUNCATED = b"\x00__crucible_truncated__"
# Returned for a path the reader Pod could not be asked about at all, because the exec
# stream ended before the command reported its status. Distinct from "absent", which is
# an answer, and from "truncated", which is a file that was there.
_UNREADABLE = b"\x00__crucible_unreadable__"


def _secret_key(name: str) -> str:
    """A Secret key for an auth file name. Keys may not contain a path separator."""
    return name.replace("/", "_")


def _owned_labels(harness: str) -> dict[str, str]:
    """The labels that mark a harness Secret as the service's own (ADR 0015)."""
    return {
        k8sspec.LABEL_MANAGED_BY: k8sspec.MANAGED_BY_CRUCIBLE,
        k8sspec.LABEL_CREDENTIAL: harness,
    }


def _login_root(credential: CredentialSpec) -> str:
    """The directory a login writes into: the credential's mount target, or its parent
    when the auth files are named from a subdirectory of the root (AGY's are under
    `.gemini`, and its CLI is pointed at the home directory above it)."""
    target = credential.mount_target.rstrip("/")
    subdir = credential.source_subdir.strip("/")
    if subdir and target.endswith("/" + subdir):
        return target[: -len(subdir) - 1]
    return target


def _probe_identity(
    request: ProbeRequest, harnesses: HarnessRegistry
) -> tuple[dict[str, str], dict[str, str]]:
    """The probe's identity ConfigMap: IDENTITY.md with the probe prompt and the
    adapter's templates, the two things the Docker probe writes (25). Keys and the paths
    they project back to, as `prepare` records them for an attempt."""
    files = {"IDENTITY.md": request.identity_text}
    adapter = harnesses.get(request.harness)
    credential = adapter.credential_spec() if adapter is not None else None
    if credential is not None:
        for name, content in credential.templates.items():
            files[f"{k8sspec.TEMPLATE_PREFIX}/{name}"] = content
    data = {_bundle_key(path): content for path, content in files.items()}
    paths = {_bundle_key(path): path for path in files}
    return data, paths


def _has_declared_auth_file(credential: CredentialSpec, secret_body: Mapping[str, Any]) -> bool:
    """True when the Secret carries at least one declared, non-empty auth file."""
    data = secret_body.get("data") or {}
    return any(
        _secret_key(auth.name) in data and bool(data[_secret_key(auth.name)])
        for auth in credential.auth_files
    )


def _bundle_key(relative: str) -> str:
    """A ConfigMap key for a bundle file. Keys are `[-._a-zA-Z0-9]+`, so the one
    separator a bundle path can carry becomes a double underscore and the volume's
    `items` maps it back to the real relative path."""
    return relative.replace("/", "__")


def _identity_item(key: str, path: str) -> dict[str, Any]:
    """One key of the identity ConfigMap projected to its path. The commit hook is the
    one executable file in the bundle: git skips a hook it cannot execute (FDY-0135)."""
    item: dict[str, Any] = {"key": key, "path": path}
    if path.startswith(f"{scripts.COMMIT_HOOK_DIR}/"):
        item["mode"] = 0o555
    return item


def _render_identity(
    spec: LaunchSpec, work_branch: str, harnesses: HarnessRegistry
) -> tuple[dict[str, str], dict[str, str], str]:
    """The identity bundle of 06, rendered exactly as the Docker provider renders it,
    then read back as ConfigMap data.

    The bundle is written to a temporary directory only so that one function writes it
    on both providers and the content hash means the same thing on both. The directory
    is removed before this returns; nothing of it is ever mounted."""
    scratch = Path(tempfile.mkdtemp(prefix="crucible-identity-"))
    try:
        adapter = harnesses.get(
            "hermes" if spec.harness == "codex" and spec.endpoint == "local" else spec.harness
        )
        credential = adapter.credential_spec() if adapter is not None else None
        if credential is not None and credential.templates:
            template_dir = scratch / k8sspec.TEMPLATE_PREFIX
            template_dir.mkdir(parents=True, exist_ok=True)
            for name, content in credential.templates.items():
                target = template_dir / name
                target.write_text(content, encoding="utf-8")
                os.chmod(target, 0o444)
        _, identity_sha = identity_bundle.write_bundle(
            scratch,
            contract=spec.contract,
            policy=spec.policy,
            external_id=spec.external_id,
            owner=spec.owner,
            work_branch=work_branch,
            network_mode=spec.network,
            report_schema=CompletionClaimV1.model_json_schema(),
        )
        data: dict[str, str] = {}
        paths: dict[str, str] = {}
        total = 0
        for path in sorted(p for p in scratch.rglob("*") if p.is_file()):
            relative = str(path.relative_to(scratch))
            content = path.read_text(encoding="utf-8")
            total += len(content.encode("utf-8"))
            key = _bundle_key(relative)
            data[key] = content
            paths[key] = relative
        if total > 1024 * 1024:
            # 08 and 26 name the ConfigMap size cap and an object-store projection above
            # it. Refusing is the honest answer until that projection exists: a
            # truncated identity bundle is a worker given the wrong contract.
            raise ProviderError(
                f"the identity bundle is {total} bytes, above the ConfigMap cap; "
                "26's projected-volume form is not implemented"
            )
        return data, paths, identity_sha
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _terminated_init(status: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The first init container that terminated non-zero, with its name attached."""
    for entry in status.get("initContainerStatuses") or []:
        if not isinstance(entry, dict):
            continue
        terminated = (entry.get("state") or {}).get("terminated")
        if isinstance(terminated, dict) and int(terminated.get("exitCode", 0)) != 0:
            return {**terminated, "containerName": entry.get("name")}
    return None


def _terminated_state(status: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for entry in status.get("containerStatuses") or []:
        if not isinstance(entry, dict) or entry.get("name") != k8sspec.CONTAINER_NAME:
            continue
        terminated = (entry.get("state") or {}).get("terminated")
        if isinstance(terminated, dict):
            return terminated
    return None


def _merge_verifications(
    ran: tuple[VerificationRun, ...] | None,
    verify: Path,
    spec: LaunchSpec,
    timeout: int,
) -> tuple[VerificationRun, ...]:
    checks = [
        (str(v.get("id")), str(v.get("command")))
        for v in spec.contract.get("required_verification", [])
        if str(v.get("kind", "command")) == "command"
    ]
    if not checks:
        return ()
    runs = read_verifications(verify, spec, checks)
    if ran is None:
        detail = f"the verifier did not finish within {timeout}s"
        return tuple(
            run if run.ran and run.exit_code >= 0 else replace(run, ran=False, detail=detail)
            for run in runs
        )
    return runs


def _check_read_back(result: ExecResult, limit: int, what: str) -> None:
    """What makes a reader Pod's stream a result: a status, a clean exit, and a size
    under its bound."""
    if result.exit_code is None:
        # The API server never sent the error channel: the stream ended early.
        # Accepting it would let a partial tar through `_extract`, which
        # suppresses tar errors; the claim still holds everything, so it is
        # read again later rather than failing the attempt.
        raise CollectionUnavailableError(
            "the reader Pod's output stream ended before the command reported "
            f"a status: {result.stderr.decode('utf-8', 'replace')[:400]}"
        )
    if result.exit_code != 0 or result.stderr:
        # A report or a diff quietly missing files is a wrong gate result
        # rather than a visible failure.
        raise CollectionFailedError(
            f"the reader Pod could not hand {what} back "
            f"(exit {result.exit_code}): "
            f"{result.stderr.decode('utf-8', 'replace')[:400]}"
        )
    if result.stdout_size >= limit:
        # 16: outputs Crucible could not read whole are an environment failure.
        # A partial extraction would give the gates a diff and a report quietly
        # missing files, which is worse than failing the attempt.
        raise CollectionFailedError(f"{what} exceeded {limit} bytes and was truncated")


def _extract(archive: Path, into: Path) -> None:
    """Extract the reader Pod's tar from the file it was streamed to. `filter="data"`
    refuses an absolute path, a `..` component, a device, a symlink out of the tree, and
    anything else that is not a plain file or directory: the tar comes off a claim a
    worker wrote into."""
    into.mkdir(parents=True, exist_ok=True)
    with (
        contextlib.suppress(tarfile.TarError, EOFError),
        tarfile.open(archive, mode="r|*") as tar,
    ):
        tar.extractall(into, filter="data")


def _canary_fields(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        if key.startswith("crucible-canary."):
            fields[key[len("crucible-canary.") :].strip()] = value.strip()
    return fields


def _read_probe(
    output: str, rules_output: str | None = None, override: int | None = None
) -> NamespaceProbe:
    """Parse the canaries' answers. Anything but a definite answer fails the probe.

    `output` is the canary under the namespace's own rules: the API server and the PID
    limit. `rules_output` is the canary under a worker's rules: DNS, the local endpoint,
    and the API server again. `done=1` is what says a script ran to the end; without it
    the output is a truncated log and nothing in it is a result."""
    fields = _canary_fields(output)
    if "api" not in fields or fields.get("done") != "1":
        return NamespaceProbe(False, False, None, "the canary produced no result", checked=False)
    rules = fields if rules_output is None else _canary_fields(rules_output)
    if rules.get("done") != "1":
        rules = {}
    answer = fields["api"]
    enforced = answer == "unreachable"
    # A canary that did not say is a canary that could not tell, never a pass.
    dns = rules.get("dns", "inconclusive")
    endpoint = rules.get("endpoint", "inconclusive")
    problems = []
    if answer == "reachable":
        problems.append("the canary reached the API server, so the CNI is not enforcing egress")
    elif not enforced:
        problems.append(
            "the canary could not tell whether it reached the API server "
            f"(tool {fields.get('tool', 'unknown')}, curl exit {fields.get('curl_exit', 'none')})"
        )
    elif rules_output is not None and rules.get("api") != "unreachable":
        # The worker rules must deny the API server too; a selector never opens it.
        enforced = False
        problems.append(
            "the canary under the worker egress rules did not prove the API server "
            f"unreachable ({rules.get('api', 'no result')})"
        )
    if dns == "failed":
        problems.append(
            "DNS check failed: the canary could not resolve a cluster name under the "
            "worker egress rules, so a worker would have no DNS (check kubernetes.egress dns)"
        )
    elif dns != "resolved":
        problems.append(
            "DNS check inconclusive: the canary image has no getent or nslookup "
            f"(tool {rules.get('dns_tool', 'unknown')})"
        )
    # The operator, 2026-09-23: "a down provider should only block that provider." An
    # unreachable local endpoint is kept out of `problems` (and so out of `passed`): the
    # DNS, default-deny and API-server results gate every launch, but the endpoint result
    # gates only the launches whose route uses it (see `_check_endpoint_ready`).
    endpoint_detail: str | None = None
    if endpoint in ("unreachable", "unresolved"):
        endpoint_detail = (
            "local endpoint check failed: the canary could not "
            + ("resolve" if endpoint == "unresolved" else "connect to")
            + " the configured local endpoint under the worker egress rules "
            f"(curl exit {rules.get('endpoint_curl_exit', 'none')}; check "
            "kubernetes.egress local_endpoint); this blocks only launches routed to a "
            "local endpoint"
        )
    elif endpoint not in ("reachable", "none"):
        endpoint_detail = (
            "local endpoint check inconclusive: the canary could not tell whether it "
            f"connected (tool {rules.get('tool', 'unknown')}, curl exit "
            f"{rules.get('endpoint_curl_exit', 'none')}); this blocks only launches "
            "routed to a local endpoint"
        )
    pid_limit, pid_source, pid_problem = _read_pod_pid_limit(fields, override)
    if pid_problem:
        problems.append(pid_problem)
    detail_parts = list(problems) + ([endpoint_detail] if endpoint_detail else [])
    return NamespaceProbe(
        passed=not problems,
        egress_enforced=enforced,
        pid_limit=pid_limit,
        detail="; ".join(detail_parts) or "namespace ready",
        # An inconclusive answer is not a probe that ran: the status page should say so
        # rather than showing a namespace that merely failed.
        checked=answer != "inconclusive"
        and dns in ("resolved", "failed")
        and endpoint in ("reachable", "unreachable", "unresolved", "none"),
        dns_resolves={"resolved": True, "failed": False}.get(dns),
        local_endpoint_reachable={
            "reachable": True,
            "unreachable": False,
            "unresolved": False,
        }.get(endpoint),
        local_endpoint_detail=endpoint_detail,
        pid_limit_source=pid_source,
    )


def _read_pod_pid_limit(
    fields: Mapping[str, str], override: int | None
) -> tuple[int | None, str, str | None]:
    """The pod-level PID limit is the kubelet's `podPidsLimit`, set on the parent of the
    canary container's own cgroup (95): the container's own `pids.max` is its runtime's
    per-container default and says nothing about a pod-level limit, which is why the
    gate no longer reads it directly. Whether the parent is even visible depends on the
    cgroup namespace the container runtime gave this container; when it is not, the
    number cannot be established from inside the pod at all.

    `override` is the operator's declared limit, used only when the canary could not
    read anything: a canary that positively read "no limit" is never overridden, since
    the operator's number could be stale and the canary's own answer is the more recent
    one. A non-positive number, from either source, is never a limit: -1 is the
    kubelet's own "PID limiting disabled", and 0 is not a value `podPidsLimit` takes."""
    raw = fields.get("pod_pids", "")
    source = fields.get("pod_pids_source", "")
    if raw.isdigit() and int(raw) > 0:
        return int(raw), source, None
    if raw == "none" or (raw.isdigit() and int(raw) <= 0):
        return (
            None,
            source,
            "the kubelet podPidsLimit is not set (the pod-level cgroup shows no limit)",
        )
    if override is not None and override > 0:
        return override, "operator-declared", None
    if raw == "unsupported":
        return None, source, "the pod-level PID limit is not readable under cgroup v1 (unsupported)"
    if source == "cgroupns-private":
        unreadable = (
            "the pod cgroup is not visible from inside the container (cgroup namespace isolation)"
        )
    elif source == "no-pids-controller":
        unreadable = "the container has no pids cgroup controller mounted"
    else:
        unreadable = "the pod-level cgroup could not be read"
    return (
        None,
        source,
        f"the pod-level PID limit could not be confirmed: {unreadable}, so the kubelet "
        "podPidsLimit cannot be established from here",
    )


def _read_one_script(path: str, limit: int) -> str:
    """Read one file off the claim: a status word, then the bytes, base64 on one line.

    Never `cat` alone: a worker owns its credential copy and could have left a symlink,
    a directory or a gigabyte at that path (12)."""
    quoted = "'" + path.replace("'", "'\"'\"'") + "'"
    return (
        f"p={quoted}\n"
        'if [ -h "$p" ]; then echo not-regular; exit 0; fi\n'
        'if [ ! -f "$p" ]; then echo absent; exit 0; fi\n'
        'size=$(wc -c < "$p")\n'
        f'if [ "$size" -gt {limit} ]; then echo too-large; exit 0; fi\n'
        "echo ok\n"
        'base64 < "$p"\n'
    )


_OUTPUT_TAR_SCRIPT = f"""cd {WORK_MOUNT} || exit 1
set --
[ -d output ] && set -- "$@" output
[ -d verify ] && set -- "$@" verify
[ $# -eq 0 ] && exit 0
exec tar cf - --exclude=output/tree --exclude=output/{scripts.CHANGED_BLOBS_DIR} "$@"
"""

# hades #398: the blobs the worker added or changed, as their own stream for the
# scanner (`BlobTarScan`). An absent directory is an older collector, and no output.
_CHANGED_BLOBS_TAR_SCRIPT = f"""cd {WORK_MOUNT} || exit 1
[ -d output/{scripts.CHANGED_BLOBS_DIR} ] || exit 0
exec tar cf - output/{scripts.CHANGED_BLOBS_DIR}
"""

# The canary of 26: it must fail to reach the API server, and it reports the node's pod
# PID limit. Both answers go to its own log, which holds nothing secret.
#
# It also runs under the egress rules a worker gets and proves the two things a worker
# needs from them (crucible#91): a cluster name resolves, and the configured local
# endpoint takes a connection. A missing tool is `inconclusive` for the same reason as
# below, and so is anything curl says that is not a definite outcome.
#
# The reachability test fails *closed*. An earlier form used bash's `/dev/tcp` redirect,
# which is not a feature of `sh`: under dash or busybox the redirect simply fails, and
# the probe would have reported "unreachable" on a namespace with no egress enforcement
# at all, which is the one answer this gate exists to refuse to invent. So the test is
# curl, whose exit code says which happened, and anything that is not a definite refusal
# to connect is `inconclusive`, which does not pass the probe.
_CANARY_SCRIPT = """
host=${KUBERNETES_SERVICE_HOST:-kubernetes.default.svc}
port=${KUBERNETES_SERVICE_PORT:-443}
if ! command -v curl >/dev/null 2>&1; then
  echo "crucible-canary.api=inconclusive"
  echo "crucible-canary.tool=none"
else
  echo "crucible-canary.tool=curl"
  connected=$(curl -sS -k -o /dev/null -w '%{time_connect}' --max-time 5 \
    "https://$host:$port/version" 2>/dev/null)
  rc=$?
  case "$rc" in
    # Connected: 0 is a response, and 22/35/52/56/60 are TLS or HTTP outcomes that all
    # required a completed TCP connection to the API server.
    0|22|35|52|56|60) echo "crucible-canary.api=reachable" ;;
    # 7 is "failed to connect", 28 is "timed out": the CNI refused the packet, unless
    # the connection completed first and only the answer was slow.
    7|28)
      case "$connected" in
        ''|0|0.000000) echo "crucible-canary.api=unreachable" ;;
        *) echo "crucible-canary.api=reachable" ;;
      esac ;;
    *) echo "crucible-canary.api=inconclusive" ;;
  esac
  echo "crucible-canary.curl_exit=$rc"
fi
# The namespace-scope canary has no policy of its own, so DNS and the endpoint are not
# its question; the worker-scope canary answers them.
if [ "${CRUCIBLE_CANARY_SCOPE:-worker}" != namespace ]; then
name=${CRUCIBLE_CANARY_DNS_NAME:-kubernetes.default.svc}
bounded=""
command -v timeout >/dev/null 2>&1 && bounded="timeout 30"
if command -v getent >/dev/null 2>&1; then
  echo "crucible-canary.dns_tool=getent"
  if $bounded getent hosts "$name" >/dev/null 2>&1; then
    echo "crucible-canary.dns=resolved"
  else
    echo "crucible-canary.dns=failed"
  fi
elif command -v nslookup >/dev/null 2>&1; then
  echo "crucible-canary.dns_tool=nslookup"
  if $bounded nslookup "$name" >/dev/null 2>&1; then
    echo "crucible-canary.dns=resolved"
  else
    echo "crucible-canary.dns=failed"
  fi
else
  echo "crucible-canary.dns_tool=none"
  echo "crucible-canary.dns=inconclusive"
fi
url=${CRUCIBLE_CANARY_ENDPOINT_URL:-}
if [ -z "$url" ]; then
  echo "crucible-canary.endpoint=none"
elif ! command -v curl >/dev/null 2>&1; then
  echo "crucible-canary.endpoint=inconclusive"
else
  # The question is whether a TCP connection is accepted, so a gateway that connects
  # and is slow to answer (a cold model) is reachable, not refused.
  connected=$(curl -sS -k -o /dev/null -w '%{time_connect}' --connect-timeout 10 \
    --max-time 20 "$url" 2>/dev/null)
  rc=$?
  case "$rc" in
    0|22|35|52|56|60) echo "crucible-canary.endpoint=reachable" ;;
    6) echo "crucible-canary.endpoint=unresolved" ;;
    7|28)
      case "$connected" in
        ''|0|0.000000) echo "crucible-canary.endpoint=unreachable" ;;
        *) echo "crucible-canary.endpoint=reachable" ;;
      esac ;;
    *) echo "crucible-canary.endpoint=inconclusive" ;;
  esac
  echo "crucible-canary.endpoint_curl_exit=$rc"
fi
fi
limit=$(cat /sys/fs/cgroup/pids.max 2>/dev/null || cat /sys/fs/cgroup/pids/pids.max 2>/dev/null)
case "$limit" in
  ''|max) echo "crucible-canary.pids=none" ;;
  *) echo "crucible-canary.pids=$limit" ;;
esac

# The pod-level limit the kubelet's podPidsLimit sets lands on the parent of this
# container's own cgroup, one level above what /sys/fs/cgroup shows here (95). Whether
# that parent is visible depends on the cgroup namespace the container runtime gave
# this container; when it is not (the container's own path in /proc/self/cgroup is its
# own root), the number cannot be read from here at all.
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
  selfline=$(grep '^0::' /proc/self/cgroup 2>/dev/null)
  path=${selfline#0::}
  if [ -z "$path" ] || [ "$path" = "/" ]; then
    echo "crucible-canary.pod_pids=unknown"
    echo "crucible-canary.pod_pids_source=cgroupns-private"
  else
    parent=$(dirname "$path")
    case "$parent" in
      /|*[!a-zA-Z0-9_./-]*) parent="" ;;
    esac
    # `parent`'s own name, not the whole path, must look like a pod-level cgroup (both
    # cgroup drivers name it with "pod": "kubepods-besteffort-pod<uid>.slice" under
    # systemd, "pod<uid>" under cgroupfs); an ancestor further up the path saying "pod"
    # does not count, or a runtime that nests one more cgroup inside the container's own
    # scope would have this match that inner cgroup, which is the container-scope
    # mistake this fix exists to remove, just moved one level up.
    base=${parent##*/}
    case "$base" in
      *pod*) podlimit=$(cat "/sys/fs/cgroup$parent/pids.max" 2>/dev/null) ;;
      *) podlimit="" ;;
    esac
    if [ -z "$podlimit" ]; then
      echo "crucible-canary.pod_pids=unknown"
      echo "crucible-canary.pod_pids_source=cgroup-v2-parent-unreadable"
    else
      case "$podlimit" in
        max) echo "crucible-canary.pod_pids=none" ;;
        *) echo "crucible-canary.pod_pids=$podlimit" ;;
      esac
      echo "crucible-canary.pod_pids_source=cgroup-v2-parent"
    fi
  fi
elif [ -d /sys/fs/cgroup/pids ]; then
  echo "crucible-canary.pod_pids=unsupported"
  echo "crucible-canary.pod_pids_source=cgroup-v1"
else
  echo "crucible-canary.pod_pids=unknown"
  echo "crucible-canary.pod_pids_source=no-pids-controller"
fi
echo "crucible-canary.done=1"
"""


# The login Pod's driver (25, 26). It runs the harness's own CLI under a pseudo-terminal
# (`script`, util-linux, in every Debian image) and is the reason the Pod log can be the
# operator's view of a login without carrying anything secret:
#
# - the CLI's output is rendered line by line, as a terminal would show it, before it
#   reaches stdout, which is the log. The terminal is 4096 columns wide so nothing the
#   CLI prints wraps. Every trailing carriage return goes (Ink ends each line `\r\r\n`
#   through the pty), then a carriage return keeps only what was drawn after it; a
#   cursor-column move is a space (Ink separates words with them); control sequences
#   (CSI, OSC and the two-byte escapes) are stripped, which leaves an OSC 8 hyperlink's
#   visible text, the URL; stray control characters go (hades #173);
# - the one-time token a CLI prints (Claude Code's `setup-token`) is written to the
#   token file under the login directory, mode 0600, and replaced in the line by the
#   note the Docker login shows;
# - a partial line is held until its newline, except a prompt waiting for input (the
#   same pattern as `PASTE_RE`), or a partial line the CLI has left unfinished for
#   three seconds. Complete tokens are captured and masked; partial tokens wait;
# - the code the operator pastes arrives over exec stdin into a FIFO that is the CLI's
#   input, ended with a carriage return, the Enter key, and is masked wherever the
#   terminal echoes it.
#
# Only directories the Pod's user owns are made private: AGY's login directory is the
# home the Pod mounts, and a chmod of it failing was shown to the operator as the
# login's status (hades #173).
#
# When the CLI exits the driver prints its exit code and waits for the service to read
# the auth files off it over exec and delete the Job; the Job's deadline ends it if the
# service never comes back. CRUCIBLE_LOGIN_CONTROL moves the control directory for a
# test that runs the driver on a host.
_LOGIN_EXIT_MARKER = "crucible-login.exit="
_LOGIN_DRIVER = r"""set -u
dir=${CRUCIBLE_LOGIN_DIR:?}
ctl=${CRUCIBLE_LOGIN_CONTROL:-/tmp/crucible-login}
umask 077
mkdir -p "$dir" "$ctl"
for owned in "$dir" "$ctl"; do
  if [ -O "$owned" ]; then chmod 0700 "$owned"; fi
done
rm -f "$ctl/in" "$ctl/pasted" "$ctl/captured-stop"
mkfifo "$ctl/in"
exec 3<>"$ctl/in"
shopt -s extglob
# The patterns below use character ranges, which bash reads in the collation order of
# the locale; C is the only order in which they mean what they say. Not exported, so
# the CLI keeps whatever locale the Pod gives it.
LC_ALL=C
token_re=${CRUCIBLE_LOGIN_TOKEN_PATTERN:-}
token_file=${CRUCIBLE_LOGIN_TOKEN_FILE:-}
# The token's literal start (`sk-ant-` for Claude Code): a quiet partial line that holds
# it may be a token still arriving, so only that line is held back, not every one.
regex_meta='[][(){}.*+?\\|^$]'
token_start=${token_re#\(}
token_start=${token_start%%$regex_meta*}
prompt_re='((paste|enter).{0,40}(code|token).{0,40}|code|token)[[:space:]]*[:>?][[:space:]]*$'
render() {
  local line=$1
  line=${line%%+($'\r')}
  line=${line##*$'\r'}
  line=${line//$'\e'\[*([0-9])[GC]/ }
  line=${line//$'\e'\[*([0-?])*([\ -\/])[@-~]/}
  line=${line//$'\e'\]*([!$'\a'$'\e'])@($'\a'|$'\e'\\)/}
  line=${line//$'\e'?([()])[A-Za-z0-9=>]/}
  line=${line//[$'\001'-$'\010'$'\013'-$'\037'$'\177']/}
  rendered=$line
}
show() {
  local line tok pasted
  render "$1"
  line=$rendered
  if [ -n "$token_re" ] && [[ $line =~ $token_re ]]; then
    tok=${BASH_REMATCH[1]}
    printf '%s\n' "$tok" > "$dir/$token_file"
    chmod 0600 "$dir/$token_file"
    line=${line//"$tok"/[captured to $token_file]}
  fi
  if [ -s "$ctl/pasted" ]; then
    pasted=$(cat "$ctl/pasted")
    [ -n "$pasted" ] && line=${line//"$pasted"/[pasted code]}
  fi
  printf '%s\n' "$line"
}
filter() {
  local buf='' chunk status lower idle=0 captured_idle=0 stopped=0 complete
  while :; do
    chunk=''
    IFS= read -r -t 1 chunk
    status=$?
    if [ "$status" -eq 0 ]; then
      show "$buf$chunk"
      buf=''
      idle=0
      captured_idle=0
      continue
    fi
    [ -n "$chunk" ] && idle=0
    buf=$buf$chunk
    if [ "$status" -le 128 ]; then
      [ -n "$buf" ] && show "$buf"
      return 0
    fi
    # setup-token can stay open after drawing the credential. Give it five more
    # quiet seconds, then let script terminate and reap its CLI on SIGTERM.
    if [ -z "$chunk" ] && [ -n "$token_file" ] && [ -s "$dir/$token_file" ]; then
      captured_idle=$((captured_idle + 1))
      if [ "$captured_idle" -ge 5 ] && [ "$stopped" -eq 0 ]; then
        : > "$ctl/captured-stop"
        kill -TERM "$(cat "$ctl/script.pid")" 2>/dev/null || :
        stopped=1
      fi
    else
      captured_idle=0
    fi
    [ -n "$buf" ] || continue
    idle=$((idle + 1))
    complete=0
    if [ -n "$token_re" ] && [[ $buf =~ $token_re ]]; then
      [ "$idle" -ge 3 ] || continue
      complete=1
    fi
    render "$buf"
    lower=${rendered,,}
    if [ "$complete" -eq 0 ] && [[ $lower =~ $prompt_re ]]; then
      show "$buf"
      buf=''
      idle=0
      continue
    fi
    # A CLI that went quiet on a partial line is showing something the operator needs:
    # an error such as Claude Code's "OAuth error ... Press Enter to retry", which it
    # draws with cursor moves and carriage returns and ends with no newline, so the
    # rendered last segment is empty (hades #173). Every visible segment is shown. Only
    # a buffer that may hold part of a token is held back.
    if [ "$idle" -ge 3 ] && { [ "$complete" -eq 1 ] || [ -z "$token_start" ] ||
      [[ $buf != *"$token_start"* ]]; }; then
      local segment shown=0 segments=()
      # read splits without glob expansion: the masked code is a row of asterisks.
      IFS=$'\r' read -r -d '' -a segments <<< "$buf"
      for segment in "${segments[@]}"; do
        render "$segment"
        [ -n "${rendered//[[:space:]]/}" ] || continue
        show "$segment"
        shown=1
      done
      [ "$shown" -eq 1 ] && { buf=''; idle=0; }
    fi
  done
}
# A wide terminal, so a CLI never wraps a token or a pasted code across two lines: the
# filter works line by line, and a wrapped tail would reach the log unmasked.
cmd="stty cols 4096 rows 50 2>/dev/null; exec $(printf '%q ' "$@")"
(
  echo "$BASHPID" > "$ctl/script.pid"
  export COLUMNS=4096 LINES=50 SHELL=/bin/bash
  exec script -qfec "$cmd" /dev/null < "$ctl/in" 2>&1
) | filter
code=${PIPESTATUS[0]}
[ -f "$ctl/captured-stop" ] && [ -s "$dir/$token_file" ] && code=0
exec 3>&-
echo "crucible-login.exit=$code"
while :; do sleep 5; done
"""

# Hands the pasted code to the login driver: the mask first, so the terminal's echo of
# the code is already masked when it arrives, then the CLI's input, ended with a
# carriage return, which is what the Enter key sends (hades #173). The carriage return
# goes a second after the code: Claude Code reads a code and its Enter that arrive
# together as one paste and never submits (ENTER_PAUSE_SECONDS in the login module).
_LOGIN_CODE_SCRIPT = (
    'd="${CRUCIBLE_LOGIN_CONTROL:-/tmp/crucible-login}"; '
    "IFS= read -r c || exit 3; umask 077; "
    'printf "%s" "$c" > "$d/pasted"; '
    'printf "%s" "$c" > "$d/in"; '
    "sleep 1; "
    'printf "\\r" > "$d/in"'
)


def _seed_script(spec: CredentialSpec) -> str:
    """The init container that makes a `rw-narrow` copy (12).

    It reads the per-attempt Secret's read-only projection and writes the same named
    files into the attempt's own claim, mode 0700 on the directory and 0600 on each
    file, owned by the worker's uid. The file list is written out rather than globbed:
    an auth file can sit in a subdirectory, and nothing but the adapter's declared files
    is ever copied."""
    names = " ".join("'" + a.name.replace("'", "'\"'\"'") + "'" for a in spec.auth_files)
    # An optional file the Secret did not carry is simply not projected, and the `-f`
    # test below skips it; nothing but the adapter's declared files is ever copied.
    return f"""set -eu
umask 077
src={k8sspec.CREDENTIAL_SOURCE_MOUNT}
dst=/crucible/credential
mkdir -p "$dst"
chmod 0700 "$dst"
for rel in {names}; do
  [ -f "$src/$rel" ] || continue
  mkdir -p "$dst/$(dirname "$rel")"
  cat < "$src/$rel" > "$dst/$rel"
  chmod 0600 "$dst/$rel"
done
"""


__all__ = [
    "PROVIDER_NAME",
    "CollectionFailedError",
    "HarnessRefusedError",
    "KubernetesConfig",
    "KubernetesProvider",
    "NamespaceProbe",
]
