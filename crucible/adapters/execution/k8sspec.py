"""The Kubernetes objects an attempt becomes (26), as pure functions.

Nothing here does I/O, reads a clock, or talks to a cluster. Everything the provider
sends to the API server is rendered by one of these functions, so the pod shape of 26
is a thing a unit test reads field by field rather than a shape that only exists inside
a live cluster (requirement of 18: what a gate depends on is tested where it is cheap).

Two enforcements sit on this shape: these functions, and the namespace's Pod Security
admission at `restricted`, which refuses the same fields again from outside Crucible
(26). The create-request policy of 13 does not survive to the cluster as a request
filter, because the API server has admission; what survives is this module.
"""

from __future__ import annotations

import base64
import ipaddress
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from crucible.domain.cluster_egress import (
    DEFAULT_DNS_NAMESPACE,
    DEFAULT_DNS_POD_LABELS,
    label_problem,
    namespace_problem,
)
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    OUTPUT_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    VERIFY_MOUNT,
    WORK_MOUNT,
)

LABEL_ATTEMPT = "crucible.attempt"
LABEL_TASK = "crucible.task"
LABEL_OWNER = "crucible.owner"
LABEL_ROLE = "crucible.role"
LABEL_RETAIN = "crucible.retain"
# The readiness canary's own id. Never `crucible.attempt`: the retention sweep deletes
# whatever carries an attempt id Crucible does not track, which a canary always is.
LABEL_CANARY = "crucible.canary"
# What an administrative run is (`login` or `probe`, 25). The probe borrows an attempt's
# objects and so carries `crucible.attempt` too; this label is what keeps the retention
# sweep and reconcile off it while the API process that owns it is still running.
LABEL_ADMIN = "crucible.admin"
# A login Job's own id and the harness it signs in. A login never carries
# `crucible.attempt`, so nothing that sweeps attempts can reach it.
LABEL_LOGIN = "crucible.login"
LABEL_HARNESS = "crucible.harness"
# The harness credential Secret is the service's own (ADR 0015): created by it when
# absent, written only by it (login, the Hermes key, sync-back), and never by GitOps.
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
MANAGED_BY_CRUCIBLE = "crucible"
LABEL_CREDENTIAL = "crucible.credential"
ADMIN_LOGIN = "login"
ADMIN_PROBE = "probe"
ANNOTATION_EGRESS = "crucible.io/egress-hosts"

ROLE_WORKER = "worker"
ROLE_PREPARER = "preparer"
# 26: the one writer of the shared reference cache; the preparer only reads it (#55).
ROLE_CACHE_REFRESHER = "cache-refresher"
ROLE_COLLECTOR = "collector"
ROLE_BUNDLE = "bundle-verifier"
ROLE_VERIFIER = "verifier"
ROLE_PUBLISHER = "publisher"
ROLE_LOGIN = "login"
# The ConfigMap that is one harness's login lock across every api replica (25, 26).
ROLE_LOGIN_LOCK = "login-lock"
ROLE_READER = "reader"
ROLE_CLEANER = "cleaner"
ROLE_CANARY = "canary"

WORKER_UID = 1000
CACHE_MOUNT = "/crucible/cache"
# Where the per-attempt Secret is presented to the init container that seeds a writable
# copy of it. A Secret volume is always read-only in Kubernetes, whatever the mount says,
# so `rw-narrow` cannot be the Secret itself (12, and the note in docs/implementation-notes).
CREDENTIAL_SOURCE_MOUNT = "/crucible/credential-source"
# The leaf of the workspace PVC that holds the writable per-attempt credential copy, the
# same leaf and the same 0700/0600 shape the Docker provider uses (12).
CREDENTIAL_LEAF = "credential"
TEMPLATE_PREFIX = "harness"

# The container name every role's single container carries. `pods/log` and `pods/exec`
# both want one, and a fixed name means neither call has to guess.
CONTAINER_NAME = "crucible"
CREDENTIAL_INIT_CONTAINER = "credential-seed"

# What a plain `networking.k8s.io/v1` CNI can express as "the internet and nothing
# inside this cluster or this lab". 26 names each of these as an explicit denial; a
# NetworkPolicy has no deny verb, so they are the `except` of the one allow (see
# `egress_policy`). IPv6 is denied entirely by never appearing in a rule.
DEFAULT_DENIED_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "127.0.0.0/8",
)


# The label every namespace carries with its own name (Kubernetes 1.21 and later). A
# selector names its namespace through it, because a namespaceSelector is the only way a
# `networking.k8s.io/v1` peer can say "that namespace and no other".
NAMESPACE_NAME_LABEL = "kubernetes.io/metadata.name"


class SpecError(Exception):
    """A launch spec cannot be rendered into a Kubernetes object."""


def object_name(prefix: str, attempt_id: str) -> str:
    """`worker-01m33dgj...`: a DNS-1123 name from a role and an attempt.

    Names are lowercase and label values are not, so the object is named from the
    lowercased ULID while `crucible.attempt` keeps the identifier exactly as Crucible
    stores it. Crockford base32 has no case-colliding pair, so the mapping is one to
    one and `reconcile` reads the attempt back from the label, never from the name."""
    return f"{prefix}-{attempt_id.lower()}"


def labels(spec: Any, role: str) -> dict[str, str]:
    """The four labels 26 puts on every object of an attempt."""
    return {
        LABEL_ATTEMPT: spec.attempt_id,
        LABEL_TASK: spec.task_id,
        LABEL_OWNER: spec.owner,
        LABEL_ROLE: role,
    }


def selector(**pairs: str) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(pairs.items()) if value)


# ----- resources ---------------------------------------------------------


def _bytes(value: Any, default: int) -> int:
    """Parse `4GiB`, `512m`, or a plain byte count. The Docker provider's own parser,
    so one policy document produces the same numbers on both providers."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip().lower().replace("i", "")
    units = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    if text and text[-1] == "b":
        text = text[:-1]
    if text and text[-1] in units:
        try:
            return int(float(text[:-1]) * units[text[-1]])
        except ValueError:
            return default
    try:
        return int(text)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Limits:
    """The effective resource limits of one attempt, recorded in its evidence (26).

    `cpu_request_fraction` and `memory_request_fraction` (issue 93) are what a pod
    requests as a fraction of what it limits, so a small cluster can schedule a
    Burstable pod instead of a Guaranteed one that demands the whole limit up front."""

    cpus: float
    memory_bytes: int
    ephemeral_storage: str
    tmpfs_bytes: int
    grace_seconds: int
    cpu_request_fraction: float = 1.0
    memory_request_fraction: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpu": self.cpu,
            "memory": self.memory,
            "cpu_request": self.cpu_request,
            "memory_request": self.memory_request,
            "ephemeral_storage": self.ephemeral_storage,
            "tmpfs": self.tmpfs,
            "termination_grace_seconds": self.grace_seconds,
        }

    @property
    def cpu(self) -> str:
        return f"{int(self.cpus * 1000)}m"

    @property
    def memory(self) -> str:
        return f"{self.memory_bytes}"

    @property
    def cpu_request(self) -> str:
        # At least 1m: a request of 0 is "unbounded" to the scheduler, which is not
        # what a fraction close to zero means.
        return f"{max(1, round(self.cpus * self.cpu_request_fraction * 1000))}m"

    @property
    def memory_request(self) -> str:
        return f"{max(1, round(self.memory_bytes * self.memory_request_fraction))}"

    @property
    def tmpfs(self) -> str:
        return f"{self.tmpfs_bytes}"


def limits_from_policy(
    policy: Mapping[str, Any], *, default_ephemeral: str = "2Gi", default_tmpfs_mb: int = 512
) -> Limits:
    resources = policy.get("resources", {}) or {}
    grace = int((policy.get("limits", {}) or {}).get("grace_seconds") or 60)
    return Limits(
        cpus=float(resources.get("cpus") or 2),
        memory_bytes=_bytes(resources.get("memory"), 4 * 1024**3),
        ephemeral_storage=str(resources.get("ephemeral_storage") or default_ephemeral),
        tmpfs_bytes=_bytes(resources.get("tmpfs_per_mount"), default_tmpfs_mb * 1024**2),
        grace_seconds=grace,
        cpu_request_fraction=float(resources.get("cpu_request_fraction") or 0.5),
        memory_request_fraction=float(resources.get("memory_request_fraction") or 1.0),
    )


# A Kubernetes resource quantity's suffixes (`resource.Quantity`): the binary ones, the
# decimal ones, and `m`, which only ever means milli.
_QUANTITY_SUFFIXES: dict[str, float] = {
    "Ki": 1024.0,
    "Mi": 1024.0**2,
    "Gi": 1024.0**3,
    "Ti": 1024.0**4,
    "Pi": 1024.0**5,
    "Ei": 1024.0**6,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
}


def quantity(value: Any) -> float | None:
    """A Kubernetes resource quantity (`2`, `500m`, `4Gi`, `1e3`) as a number, or None
    when it is not one. The API server may return a quantity in a canonical form other
    than the one Crucible sent, so a live Pod is read by value, not by string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    factor = 1.0
    for suffix in sorted(_QUANTITY_SUFFIXES, key=len, reverse=True):
        if text.endswith(suffix) and text[: -len(suffix)]:
            factor = _QUANTITY_SUFFIXES[suffix]
            text = text[: -len(suffix)]
            break
    try:
        number = float(text)
    except ValueError:
        return None
    if number < 0 or not math.isfinite(number):
        return None
    return number * factor


def limits_from_pod(pod: Mapping[str, Any], fallback: Limits) -> Limits:
    """The limits a live Pod (or a Job's pod template) actually carries (issues 66, 76).

    What the API server stored is the effective shape: admission may have defaulted or
    rewritten what Crucible asked for, and an adopted attempt has no policy in memory at
    all. Every field is read from `pod` where it is present and parses, and taken from
    `fallback` only where it is not, so a partial or unfamiliar Pod never invents a
    number. A request is kept against the effective limit (the Pod's own, or the
    fallback's where the Pod's is missing or unparsable) rather than discarded for the
    policy default fraction, so a live request still means something even without a
    matching live limit."""
    containers = [c for c in pod.get("containers") or [] if isinstance(c, Mapping)]
    container: Mapping[str, Any] = next(
        (c for c in containers if c.get("name") == CONTAINER_NAME),
        containers[0] if containers else {},
    )
    resources = container.get("resources") or {}
    limit = resources.get("limits") or {}
    request = resources.get("requests") or {}
    cpus = quantity(limit.get("cpu"))
    memory = quantity(limit.get("memory"))
    cpu_request = quantity(request.get("cpu"))
    memory_request = quantity(request.get("memory"))
    tmpfs: int | None = None
    for volume in pod.get("volumes") or []:
        if not isinstance(volume, Mapping) or volume.get("name") != "tmp":
            continue
        size = quantity((volume.get("emptyDir") or {}).get("sizeLimit"))
        tmpfs = int(size) if size is not None else None
    grace = pod.get("terminationGracePeriodSeconds")
    effective_cpus = cpus if cpus else fallback.cpus
    effective_memory = int(memory) if memory else fallback.memory_bytes
    return Limits(
        cpus=effective_cpus,
        memory_bytes=effective_memory,
        ephemeral_storage=str(limit.get("ephemeral-storage") or fallback.ephemeral_storage),
        tmpfs_bytes=tmpfs if tmpfs is not None else fallback.tmpfs_bytes,
        grace_seconds=(
            int(grace)
            if isinstance(grace, int) and not isinstance(grace, bool) and grace >= 0
            else fallback.grace_seconds
        ),
        cpu_request_fraction=(
            cpu_request / effective_cpus
            if cpu_request is not None and effective_cpus
            else fallback.cpu_request_fraction
        ),
        memory_request_fraction=(
            memory_request / effective_memory
            if memory_request is not None and effective_memory
            else fallback.memory_request_fraction
        ),
    )


def canary_limits(*, cpu_millicores: int, memory: str) -> Limits:
    """26's readiness canary (issue 93): a shell script with curl, not a role pod.

    Fixed small ephemeral storage, tmpfs and grace period: the canary writes nothing of
    size and is deleted as soon as its one-shot phase is terminal. Request equals limit
    here, unlike a role pod's `Limits`, because the canary's limit is already the
    smallest useful size; splitting it further buys nothing."""
    return Limits(
        cpus=cpu_millicores / 1000,
        memory_bytes=_bytes(memory, 64 * 1024**2),
        ephemeral_storage="128Mi",
        tmpfs_bytes=16 * 1024**2,
        grace_seconds=5,
    )


# ----- the pod shape (26) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mount:
    name: str
    path: str
    read_only: bool = False
    sub_path: str | None = None


@dataclass(frozen=True, slots=True)
class PodRequest:
    """Everything that differs between two roles' Pods. Everything that does not differ
    is in `pod_spec` and is therefore identical for every role by construction."""

    role: str
    image: str
    command: Sequence[str]
    limits: Limits
    env: Mapping[str, str] = field(default_factory=dict)
    mounts: Sequence[Mount] = ()
    volumes: Sequence[Mapping[str, Any]] = ()
    init_containers: Sequence[Mapping[str, Any]] = ()
    working_dir: str = "/tmp"
    service_account: str = "crucible-worker"
    image_pull_secret: str | None = None
    host_aliases: Sequence[Mapping[str, Any]] = ()


def pod_spec(request: PodRequest) -> dict[str, Any]:
    """26's pod shape, verbatim, for every role.

    `runtimeClassName` is deliberately absent. Setting it to a sandboxed runtime
    (gVisor or Kata) is the microVM step the operator deferred on 2026-09-21: it
    changes this one field and nothing else in this module, which is why it is named
    here rather than left to be rediscovered.
    """
    container: dict[str, Any] = {
        "name": CONTAINER_NAME,
        "image": request.image,
        "command": list(request.command),
        "workingDir": request.working_dir,
        "env": [{"name": k, "value": v} for k, v in sorted(request.env.items())],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "limits": {
                "cpu": request.limits.cpu,
                "memory": request.limits.memory,
                "ephemeral-storage": request.limits.ephemeral_storage,
            },
            # The request is a fraction of the limit (issue 93), so a small cluster can
            # schedule a Burstable pod instead of demanding the whole limit up front.
            # Memory defaults its fraction to 1 (request equals limit), which keeps a
            # worker promised the policy's memory from being the first thing evicted
            # under node pressure, an eviction that would otherwise show up as a `lost`
            # attempt nobody caused (16); CPU has no such eviction risk; a policy may
            # still lower memory's fraction for a memory-constrained cluster.
            "requests": {
                "cpu": request.limits.cpu_request,
                "memory": request.limits.memory_request,
            },
        },
        "volumeMounts": [_mount(m) for m in request.mounts],
        "terminationMessagePolicy": "FallbackToLogsOnError",
    }
    spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "serviceAccountName": request.service_account,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "terminationGracePeriodSeconds": request.limits.grace_seconds,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": WORKER_UID,
            "runAsGroup": WORKER_UID,
            "fsGroup": WORKER_UID,
            "fsGroupChangePolicy": "OnRootMismatch",
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
        "volumes": [dict(v) for v in request.volumes],
    }
    if request.init_containers:
        spec["initContainers"] = [dict(c) for c in request.init_containers]
    if request.image_pull_secret:
        spec["imagePullSecrets"] = [{"name": request.image_pull_secret}]
    if request.host_aliases:
        spec["hostAliases"] = [dict(a) for a in request.host_aliases]
    return spec


def _mount(mount: Mount) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": mount.name,
        "mountPath": mount.path,
        "readOnly": mount.read_only,
    }
    if mount.sub_path is not None:
        out["subPath"] = mount.sub_path
    return out


def memory_volume(name: str, size_bytes: int) -> dict[str, Any]:
    """26: `/tmp` and `/home/worker` are memory-backed and size-limited, which is the
    Kubernetes spelling of the Docker provider's two tmpfs mounts (13)."""
    return {"name": name, "emptyDir": {"medium": "Memory", "sizeLimit": str(size_bytes)}}


def base_volumes(limits: Limits) -> list[dict[str, Any]]:
    return [memory_volume("tmp", limits.tmpfs_bytes), memory_volume("home", limits.tmpfs_bytes)]


def base_mounts() -> list[Mount]:
    return [Mount("tmp", "/tmp"), Mount("home", "/home/worker")]


def job(
    *,
    name: str,
    namespace: str,
    object_labels: Mapping[str, str],
    pod: Mapping[str, Any],
    active_deadline_seconds: int,
    annotations: Mapping[str, str] | None = None,
    ttl_seconds_after_finished: int | None = None,
) -> dict[str, Any]:
    """One Job per role per attempt (26): one Pod, no retries, its own deadline.

    `backoffLimit: 0` and `restartPolicy: Never` together are what make an exit an
    exit: Kubernetes must never re-run a worker Crucible already classified (16).

    `ttl_seconds_after_finished` is for the login Job only. An attempt's Jobs are
    deleted by Crucible after `logs_drained`; a login Job is deleted by the API process
    that runs it, and the TTL is what removes it when that process died first."""
    body: dict[str, Any] = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(object_labels),
            **({"annotations": dict(annotations)} if annotations else {}),
        },
        "spec": {
            "backoffLimit": 0,
            "completions": 1,
            "parallelism": 1,
            "activeDeadlineSeconds": active_deadline_seconds,
            # Crucible deletes what it created, after `logs_drained` and never before
            # (08). A TTL controller would race that and take a worker's log with it.
            "template": {"metadata": {"labels": dict(object_labels)}, "spec": dict(pod)},
        },
    }
    if ttl_seconds_after_finished is not None:
        body["spec"]["ttlSecondsAfterFinished"] = ttl_seconds_after_finished
    return body


def bare_pod(
    *,
    name: str,
    namespace: str,
    object_labels: Mapping[str, str],
    pod: Mapping[str, Any],
) -> dict[str, Any]:
    """A Pod with no Job around it: the reader, the cleaner, and the readiness canary.

    None of the three is a role of the attempt in 26's table; each is a short-lived
    mechanism Crucible drives and waits on itself, so a Job's retry and deadline
    machinery would only get in the way."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(object_labels)},
        "spec": dict(pod),
    }


def workspace_claim(
    *, name: str, namespace: str, object_labels: Mapping[str, str], size: str, storage_class: str
) -> dict[str, Any]:
    """`ws-<attempt>`: the workspace (26). ReadWriteOnce, because one Pod of the attempt
    writes it at a time and the roles run one after another, never together."""
    body: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(object_labels)},
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}},
    }
    if storage_class:
        body["spec"]["storageClassName"] = storage_class
    return body


def config_map(
    *,
    name: str,
    namespace: str,
    object_labels: Mapping[str, str],
    data: Mapping[str, str],
    annotations: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(object_labels),
            **({"annotations": dict(annotations)} if annotations else {}),
        },
        "data": dict(data),
    }


def secret(
    *,
    name: str,
    namespace: str,
    object_labels: Mapping[str, str],
    data: Mapping[str, bytes],
) -> dict[str, Any]:
    """`cred-<attempt>`: the per-attempt copy of one harness credential (12, 26).

    The values travel in this request body and nowhere else: not in an env var, not in
    a command, not in a volume the Crucible process reads, and not in this process's
    argv. The caller base64-encodes nothing itself; that is done here, once."""

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(object_labels)},
        "data": {
            key: base64.b64encode(value).decode("ascii") for key, value in sorted(data.items())
        },
    }


# ----- the NetworkPolicy (26) --------------------------------------------


@dataclass(frozen=True, slots=True)
class PeerSelector:
    """Pods in one namespace, named by their labels (26, crucible#91).

    This is the form of a destination that survives a CNI which translates a service
    address to its backend pods before it evaluates policy, which is what Cilium does
    with kube-proxy replacement: an `ipBlock` on a ClusterIP or a LoadBalancer address
    never matches there, and a selector on the backends always does. Calico and every
    other conforming CNI match it too, so it is plain `networking.k8s.io/v1`.

    A selector is always one namespace and at least one label. An empty pod selector
    would be every pod in that namespace, which is not a destination anybody chose."""

    namespace: str
    pod_labels: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, namespace: str, pod_labels: Mapping[str, str]) -> PeerSelector:
        return cls(namespace, tuple(sorted((str(k), str(v)) for k, v in pod_labels.items())))

    def peer(self) -> dict[str, Any]:
        return {
            "namespaceSelector": {"matchLabels": {NAMESPACE_NAME_LABEL: self.namespace}},
            "podSelector": {"matchLabels": dict(self.pod_labels)},
        }

    def as_dict(self) -> dict[str, Any]:
        return {"namespace": self.namespace, "pod_labels": dict(self.pod_labels)}


def check_selector(
    selector: PeerSelector, *, what: str, protected_namespaces: Sequence[str] = ()
) -> PeerSelector:
    """Refuse a selector that would open more than one set of pods in one namespace.

    The protected namespaces are the workers namespace and Crucible's own: a selector
    into the first is a worker reaching another attempt's pods, and into the second is a
    worker reaching Crucible's database. Neither is ever the cluster resolver or a
    model gateway, so neither is ever allowed."""
    problem = namespace_problem(selector.namespace)
    if problem is not None:
        raise SpecError(f"the {what} namespace {problem}")
    if selector.namespace in protected_namespaces:
        raise SpecError(
            f"the {what} selector names the {selector.namespace!r} namespace, which a worker "
            "may never reach"
        )
    if not selector.pod_labels:
        raise SpecError(
            f"the {what} selector names no pod labels, which would allow every pod in "
            f"{selector.namespace!r}"
        )
    for key, value in selector.pod_labels:
        problem = label_problem(key, value)
        if problem is not None:
            raise SpecError(f"the {what} pod label {problem}")
    return selector


@dataclass(frozen=True, slots=True)
class EgressPlan:
    """What one role may reach.

    `hosts` are the names the allowlist carries, which is the record of what the policy
    authorized; `cidrs` are those names resolved to addresses, which is what a
    `networking.k8s.io/v1` CNI can actually enforce (26: "resolved to CIDRs or FQDN
    rules where the CNI supports them"); `endpoints` are exact `address:port`
    destinations for a local model route (05b, S16).

    `broad` is the opt-out: a deployment whose CNI enforces FQDN rules some other way
    can ask for "the public internet on 443, minus every range 26 denies" instead of a
    resolved set. It is off by default, because that rule would let a worker reach every
    public address, whether or not the policy named it."""

    hosts: tuple[str, ...] = ()
    cidrs: tuple[str, ...] = ()
    endpoints: tuple[str, ...] = ()
    https_port: int = 443
    broad: bool = False
    # An in-cluster local endpoint, as its backend pods and their port (crucible#91).
    # When it is set the provider has replaced `endpoints` with it: the service address
    # a name resolves to is exactly what a translating CNI never matches.
    endpoint_selector: PeerSelector | None = None
    endpoint_ports: tuple[int, ...] = ()
    # Each allowlisted name with the addresses it resolved to when the policy was
    # written. The Pod is told these same addresses (`host_aliases`), so what it
    # connects to is what the policy permits even when the name's answer has since
    # changed (hades #191).
    host_addresses: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def empty(self) -> bool:
        return not self.hosts and not self.endpoints and self.endpoint_selector is None


_DNS_1123_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")


def host_aliases(plan: EgressPlan) -> list[dict[str, Any]]:
    """The Pod's `hostAliases` for a resolved plan: each allowlisted name pinned to the
    addresses its policy rule permits (hades #191).

    A policy names addresses, and a Pod resolves the name again when it connects. For a
    name whose answer changes (github.com hands out one address with a 60 second TTL,
    and a different one to different resolvers) the two drift apart, and every
    connection times out against the default deny. Writing the policy's own addresses
    into the Pod's hosts file takes the second lookup away. The broad rule permits any
    public address, so it pins nothing."""
    if plan.broad:
        return []
    by_address: dict[str, list[str]] = {}
    for written, addresses in plan.host_addresses:
        # The API server takes only a lowercase DNS-1123 name here; a name the policy
        # wrote otherwise (`Registry.NPMjs.org`, `pypi.org.`) is pinned in that form,
        # and one that still does not fit is left to the Pod's own lookup.
        host = written.lower().rstrip(".")
        if len(host) > 253 or not _DNS_1123_SUBDOMAIN.fullmatch(host):
            continue
        for cidr in addresses:
            network = ipaddress.ip_network(cidr, strict=False)
            if network.version != 4 or network.num_addresses != 1:
                continue
            names = by_address.setdefault(str(network.network_address), [])
            if host not in names:
                names.append(host)
    return [
        {"ip": address, "hostnames": sorted(names)} for address, names in sorted(by_address.items())
    ]


def _endpoint_rule(endpoint: str) -> dict[str, Any]:
    address, _, port = endpoint.rpartition(":")
    if not address or not port.isdigit():
        raise SpecError(f"{endpoint!r} is not an address:port destination")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        # 26 resolves a local route to a CIDR. A name here would silently become "the
        # whole internet on that port", which is not what the policy authorized.
        raise SpecError(
            f"the local endpoint {address!r} must be an IP address for a NetworkPolicy; "
            "a hostname cannot be expressed without a CNI that supports FQDN rules"
        ) from exc
    if parsed.version != 4:
        raise SpecError(f"the local endpoint {address!r} is IPv6, which this policy denies")
    return {
        "to": [{"ipBlock": {"cidr": f"{parsed}/32"}}],
        "ports": [{"protocol": "TCP", "port": int(port)}],
    }


def denied_by(cidr: str, denied: Sequence[str]) -> str | None:
    """The denied range an allowed destination falls inside, if any.

    This is the check that makes the denials of 26 real for a resolved allowlist. An
    allowed address is a `/32`, so asking whether a denied range sits inside it is
    always false and always was; what matters is the other direction, because a name
    that resolves to the API server's ClusterIP, to link-local, or into the lab's own
    ranges would otherwise become an allow rule for exactly the destination the policy
    denies. The caller refuses the launch rather than emitting the rule."""
    network = ipaddress.ip_network(cidr)
    for entry in denied:
        candidate = ipaddress.ip_network(entry)
        if isinstance(network, type(candidate)) and network.subnet_of(candidate):  # type: ignore[arg-type]
            return entry
    return None


def egress_policy(
    *,
    name: str,
    namespace: str,
    object_labels: Mapping[str, str],
    attempt_id: str,
    role: str,
    plan: EgressPlan,
    dns_server: str,
    denied_cidrs: Sequence[str] = DEFAULT_DENIED_CIDRS,
    dns_selector: PeerSelector | None = None,
    pod_selector: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One attempt's one role's egress, as the only thing that opens the default deny.

    The namespace carries a default-deny for ingress and egress (26), so this policy
    adds and never subtracts: every destination not named here stays denied, and the
    ranges in `denied_cidrs` stay denied even though the broad rule is `0.0.0.0/0`,
    because they are its `except`. There is no ingress section at all: nothing ever
    connects to a worker.

    Cluster DNS is allowed twice over, as the resolver's service address and as its
    pods. A CNI that evaluates policy before it translates a service address matches
    the first; one that translates first, as Cilium does with kube-proxy replacement,
    matches only the second (crucible#91). Either way it is port 53 and nothing else."""
    rules: list[dict[str, Any]] = []
    dns_peers: list[dict[str, Any]] = []
    if dns_server:
        try:
            resolver = ipaddress.ip_address(dns_server)
        except ValueError as exc:
            raise SpecError(f"the cluster DNS address {dns_server!r} is not an IP address") from exc
        dns_peers.append({"ipBlock": {"cidr": f"{resolver}/32"}})
    if dns_selector is not None:
        dns_peers.append(check_selector(dns_selector, what="cluster DNS").peer())
    if dns_peers:
        # 26: port 53, both protocols, and nothing else on the resolver.
        rules.append(
            {
                "to": dns_peers,
                "ports": [
                    {"protocol": "UDP", "port": 53},
                    {"protocol": "TCP", "port": 53},
                ],
            }
        )
    if plan.hosts:
        # The resolved addresses of the allowlist. Every one of them has already been
        # checked against `denied_cidrs` by the caller and refused if it fell inside
        # one, so nothing here can name a denied destination; the broad form keeps the
        # denials as its `except`.
        destinations: list[dict[str, Any]] = (
            [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(denied_cidrs)}}]
            if plan.broad
            else [{"ipBlock": {"cidr": cidr}} for cidr in plan.cidrs]
        )
        if destinations:
            rules.append(
                {
                    "to": destinations,
                    "ports": [{"protocol": "TCP", "port": plan.https_port}],
                }
            )
    rules.extend(_endpoint_rule(endpoint) for endpoint in plan.endpoints)
    if plan.endpoint_selector is not None:
        if not plan.endpoint_ports or any(not 0 < p < 65536 for p in plan.endpoint_ports):
            raise SpecError(
                f"the in-cluster local endpoint needs a TCP port, not {plan.endpoint_ports!r}"
            )
        rules.append(
            {
                "to": [check_selector(plan.endpoint_selector, what="local endpoint").peer()],
                "ports": [{"protocol": "TCP", "port": p} for p in plan.endpoint_ports],
            }
        )
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(object_labels),
            "annotations": {ANNOTATION_EGRESS: ",".join(plan.hosts)},
        },
        "spec": {
            "podSelector": {
                "matchLabels": dict(pod_selector)
                if pod_selector is not None
                else {LABEL_ATTEMPT: attempt_id, LABEL_ROLE: role}
            },
            "policyTypes": ["Egress"],
            "egress": rules,
        },
    }


__all__ = [
    "ADMIN_LOGIN",
    "ADMIN_PROBE",
    "ANNOTATION_EGRESS",
    "CACHE_MOUNT",
    "CONTAINER_NAME",
    "CREDENTIAL_INIT_CONTAINER",
    "CREDENTIAL_LEAF",
    "CREDENTIAL_SOURCE_MOUNT",
    "DEFAULT_DENIED_CIDRS",
    "DEFAULT_DNS_NAMESPACE",
    "DEFAULT_DNS_POD_LABELS",
    "IDENTITY_MOUNT",
    "LABEL_ADMIN",
    "LABEL_ATTEMPT",
    "LABEL_CANARY",
    "LABEL_CREDENTIAL",
    "LABEL_HARNESS",
    "LABEL_LOGIN",
    "LABEL_MANAGED_BY",
    "LABEL_OWNER",
    "LABEL_RETAIN",
    "LABEL_ROLE",
    "LABEL_TASK",
    "MANAGED_BY_CRUCIBLE",
    "NAMESPACE_NAME_LABEL",
    "OUTPUT_MOUNT",
    "REPORT_MOUNT",
    "REPO_MOUNT",
    "ROLE_BUNDLE",
    "ROLE_CACHE_REFRESHER",
    "ROLE_CANARY",
    "ROLE_CLEANER",
    "ROLE_COLLECTOR",
    "ROLE_LOGIN",
    "ROLE_LOGIN_LOCK",
    "ROLE_PREPARER",
    "ROLE_PUBLISHER",
    "ROLE_READER",
    "ROLE_VERIFIER",
    "ROLE_WORKER",
    "TEMPLATE_PREFIX",
    "VERIFY_MOUNT",
    "WORKER_UID",
    "WORK_MOUNT",
    "EgressPlan",
    "Limits",
    "Mount",
    "PeerSelector",
    "PodRequest",
    "SpecError",
    "bare_pod",
    "base_mounts",
    "base_volumes",
    "canary_limits",
    "check_selector",
    "config_map",
    "denied_by",
    "egress_policy",
    "host_aliases",
    "job",
    "labels",
    "limits_from_pod",
    "limits_from_policy",
    "memory_volume",
    "object_name",
    "pod_spec",
    "quantity",
    "secret",
    "selector",
    "workspace_claim",
]
