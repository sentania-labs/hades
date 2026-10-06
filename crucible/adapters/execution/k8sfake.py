"""An in-memory Kubernetes API for the unit and integration tiers (18, 26).

C8a proves the Kubernetes provider without a cluster: every object it creates, every
Pod state it reads, every log it pulls and every byte it reads back off a workspace claim
is answered here. What a real cluster adds is scheduling, admission, a CNI and a kubelet,
and those are C8b's `make e2e-kind` tier; what this fake exists to prove is that the
provider asks for the right objects and reads the answers correctly.

The shape is the fake execution provider's (`fake.py`): the image tag selects a scripted
behaviour, and the roles of an attempt act it out. A Job's Pod terminates at once for
every role but the worker, which stays Running for a scripted number of observations, so
the lifecycle tests can tick a supervisor through a run the way they do against the other
two providers.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tarfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO, Any

from crucible.adapters.execution.fake import (
    BEHAVIORS,
    REPORTING_BEHAVIORS,
    REVIEW_BEHAVIORS,
    changed_paths,
    default_report,
    default_review_report,
    fake_commit_policy,
    synthetic_diff,
    synthetic_head_sha,
    synthetic_review_diff,
)
from crucible.adapters.execution.k8sapi import (
    ExecResult,
    KubernetesApiError,
    KubernetesUnavailableError,
    LogFrame,
)
from crucible.adapters.execution.k8sregistry import RegistryError
from crucible.adapters.execution.k8sspec import (
    CONTAINER_NAME,
    LABEL_ATTEMPT,
    LABEL_CANARY,
    LABEL_ROLE,
    ROLE_BUNDLE,
    ROLE_CACHE_REFRESHER,
    ROLE_CANARY,
    ROLE_CLEANER,
    ROLE_COLLECTOR,
    ROLE_LOGIN,
    ROLE_LOGIN_LOCK,
    ROLE_PREPARER,
    ROLE_PUBLISHER,
    ROLE_READER,
    ROLE_VERIFIER,
    ROLE_WORKER,
)
from crucible.adapters.execution.scripts import (
    ACTIVITY_MARKER,
    CHANGED_BLOBS_DIR,
    MERGE_MAIN_MARKER,
    PUBLISH_BUNDLE_LEAF,
    PUBLISH_LEAF,
    PUBLISH_LEAF_MARKER,
    REVIEW_DIFF_DIR,
)
from crucible.domain.time import parse_rfc3339
from crucible.ports.execution import ImageInfo

_TAG = re.compile(r"^.*:.*fake-(?P<behavior>[a-z]+(?:-[a-z]+)*?)(?:-(?P<n>\d+))?$")


class FakeRegistry:
    """The registry half: a reference resolves to a digest and the two labels a launch
    is refused on (07, 13)."""

    def __init__(self, api: FakeKubernetesApi | None = None) -> None:
        # A real registry answers a tag with a digest and the tag is gone from the
        # reference the Pod runs. The fake records the mapping back so a role can still
        # read the behaviour a test spelled in the tag.
        self._api = api
        self.auths: dict[str, Any] = {}
        self._images: dict[str, ImageInfo] = {}
        self._tags: dict[str, list[str]] = {}
        self.unavailable: set[str] = set()

    def register(
        self,
        reference: str,
        *,
        harness: str = "script-harness",
        version: str = "1.0.0",
        labels: Mapping[str, str] | None = None,
    ) -> ImageInfo:
        digest = "sha256:" + hashlib.sha256(reference.encode()).hexdigest()
        resolved = {"crucible.harness": harness, "crucible.harness_version": version}
        if labels is not None:
            resolved = dict(labels)
        info = ImageInfo.from_labels(
            f"{reference.split(':', maxsplit=1)[0]}@{digest}", digest, resolved
        )
        self._images[reference] = info
        if self._api is not None:
            self._api.image_tags[info.reference] = reference
        repository = reference.rsplit(":", 1)[0]
        self._tags.setdefault(repository, []).append(reference.rsplit(":", 1)[-1])
        return info

    def resolve(self, reference: str, *, deadline: float | None = None) -> ImageInfo:
        if reference in self.unavailable:
            raise RegistryError(f"{reference} is not in the registry")
        info = self._images.get(reference)
        if info is None:
            raise RegistryError(f"{reference} is not in the registry")
        return info

    def list_tags(self, repository: str, *, deadline: float | None = None) -> list[str]:
        return list(self._tags.get(repository, []))


@dataclass
class _Worker:
    """The scripted worker of one attempt, exactly as `fake.py` scripts it."""

    behavior: str
    remaining: int
    observations: int = 0
    exit_code: int | None = None
    terminated: bool = False
    drained: bool = False
    kills: int = 0


@dataclass
class FakeLogin:
    """What the fake cluster's login Pod acts out (25, 26): the lines its CLI prints, a
    prompt it waits at for a pasted code (or none, for a device flow that finishes on
    its own), the files it leaves under its home, and its exit code. Paths are the
    absolute paths inside the Pod."""

    lines: list[str] = field(
        default_factory=lambda: ["Open https://example.invalid/device", "Device code C7AA-TEST"]
    )
    prompt: str | None = None
    after_code: list[str] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    exit_code: int = 0
    # How many log reads a flow without a prompt takes before its CLI exits.
    finish_after: int = 1
    # A Pod that never reports the CLI's exit (it is still waiting when time runs out).
    never_exits: bool = False


@dataclass
class _LoginRun:
    script: FakeLogin
    reads: int = 0
    codes: list[bytes] = field(default_factory=list)
    exited: bool = False


@dataclass(frozen=True)
class _SpecStub:
    """What `fake.py`'s report builders read off a launch spec."""

    attempt_id: str
    external_id: str
    contract: dict[str, Any]


@dataclass
class _Object:
    kind: str
    name: str
    body: dict[str, Any]
    deleted: bool = False


def _opens_dns(peer: Mapping[str, Any], translates_services: bool) -> bool:
    """Whether one policy peer lets a pod reach the fake cluster's resolver."""
    if "ipBlock" in peer:
        return not translates_services
    namespace = (peer.get("namespaceSelector") or {}).get("matchLabels") or {}
    pods = (peer.get("podSelector") or {}).get("matchLabels") or {}
    return namespace == {"kubernetes.io/metadata.name": "kube-system"} and pods == {
        "k8s-app": "kube-dns"
    }


@dataclass
class FakeKubernetesApi:
    """Everything the provider calls on `KubernetesClient`, in memory.

    The workspace claim is a dict of path to bytes, which is what makes the reader Pod's
    `tar` and its single-file read answerable without a filesystem."""

    namespace: str = "crucible-workers"
    # A canary that cannot reach the API server and a node with a pod PID limit: the
    # namespace 26 asks lab-admin for. A test flips either to prove the refusal.
    egress_enforced: bool = True
    # What the canary reports: `unreachable`, `reachable`, or the `inconclusive` a
    # cluster whose image carries no curl produces.
    canary_answer: str = ""
    # A preparer that runs and writes no HEAD, which is the second way `prepare` can
    # raise after the per-attempt Secret has been seeded.
    claims_suppress_head: bool = False
    # A canary whose output stops before the final line, which is a truncated log and
    # not a result.
    canary_done: bool = True
    # What the canary's DNS check and local endpoint check report when its egress
    # policy allows them (crucible#91): `resolved` or `failed` and `inconclusive` for
    # the first, `reachable`, `unreachable`, `unresolved` or `inconclusive` for the
    # second. A canary with no policy carrying a port 53 rule always fails its DNS
    # check, which is what a namespace default deny does to it.
    canary_dns: str = "resolved"
    canary_endpoint: str = "reachable"
    # A CNI that translates a service address to its pods before it evaluates policy
    # (Cilium with kube-proxy replacement): only a selector on the kube-dns pods opens
    # DNS, and an address rule on the DNS service never does (crucible#91).
    translates_services: bool = False
    pod_pid_limit: int | None = 4096
    # The canary's source for `pod_pid_limit` (95): the default simulates a cgroup
    # namespace that lets the container see its pod's parent cgroup. A test can set
    # this to `cgroupns-private` (the parent is not visible, `pod_pid_limit` is then
    # ignored) or `cgroup-v1` (unsupported) to exercise those probe outcomes.
    pod_pid_limit_source: str = "cgroup-v2-parent"
    node_name: str = "lab-node-1"
    quota_jobs: int | None = None

    objects: dict[tuple[str, str], _Object] = field(default_factory=dict)
    claims: dict[str, dict[str, bytes]] = field(default_factory=dict)
    logs: dict[str, list[str]] = field(default_factory=dict)
    # Every `pod_log` call's name, `sinceTime` and `limitBytes`, in order (issue 63).
    log_reads: list[dict[str, Any]] = field(default_factory=list)
    workers: dict[str, _Worker] = field(default_factory=dict)
    scripts: dict[str, tuple[str, int]] = field(default_factory=dict)
    # Which tag each resolved digest came from, so a role can read the behaviour a test
    # spelled in the tag (a registry answers with a digest and the tag is gone).
    image_tags: dict[str, str] = field(default_factory=dict)
    # What an attempt's roles act out when its image tag names no behaviour. The
    # integration tier sets it per case, because the image a routed task runs is the
    # promoted one and carries no behaviour in its tag.
    default_behavior: tuple[str, int] | None = None
    # A test flips this to make the next create fail, or to make a Pod unschedulable.
    refuse_create: set[str] = field(default_factory=set)
    # Roles whose Job creation fails, so a test can break one role without breaking the
    # rest of the attempt.
    refuse_roles: set[str] = field(default_factory=set)
    pending_forever: set[str] = field(default_factory=set)
    # A Job whose Pod the fake never creates, so a test can exercise the launch
    # window before the Job controller has produced anything to observe (26).
    no_pod_yet: set[str] = field(default_factory=set)
    created: list[dict[str, Any]] = field(default_factory=list)
    create_attempts: list[str] = field(default_factory=list)
    deleted: list[tuple[str, str]] = field(default_factory=list)
    # What the next login Pod acts out, and each login Pod's run by Pod name.
    login: FakeLogin = field(default_factory=FakeLogin)
    login_runs: dict[str, _LoginRun] = field(default_factory=dict)
    # Every stdin an exec was given, by Pod name, so a test sees what went in.
    exec_stdin: dict[str, list[bytes]] = field(default_factory=dict)
    # FDY-0140: what the activity probe prints for a Pod, one answer per ask.
    activity_answers: dict[str, list[bytes]] = field(default_factory=dict)
    # The Job controller's `activeDeadlineSeconds` firing on a hanging worker before
    # Crucible's own wait ends: the Job is marked failed with `DeadlineExceeded` and its
    # Pod is removed (or left terminated with 137 when `deadline_keeps_pod`).
    job_deadline_fires: bool = False
    deadline_keeps_pod: bool = False
    # The publisher (23): each push it acted out, with the remote, the branch, the head
    # and the token it found in its Secret, so a test sees what reached the push without
    # the token ever being anywhere else.
    pushes: list[dict[str, str]] = field(default_factory=list)
    # The claim's `publish/` leaf as the preparer finds it before a push: a claim on
    # storage where the worker uid cannot make or write it (an NFS export that squashes
    # it, say) fails the leaf script with exit 8, as the real script does.
    publish_leaf_unwritable: bool = False
    # Called with each push, so a test's stand-in remote can move its branch.
    on_push: Callable[[dict[str, str]], None] | None = None
    # hades #411: each merge-main run the publisher acted out, and what the next one
    # finds: conflicting paths (the run stops and pushes nothing) or a clean merge whose
    # commit is `merge_main_head`.
    merges: list[dict[str, str]] = field(default_factory=list)
    merge_main_conflicts: list[str] = field(default_factory=list)
    merge_main_head: str = "e" * 40
    # An API server that cannot answer: each entry is (call, kind, count), and the next
    # `count` calls of that name (`get`, `create`, `list_objects`, `pod_exec`, ...) on
    # that kind ("" for any kind) raise a 503, as a restarting API server does.
    outages: list[list[Any]] = field(default_factory=list)
    # Roles whose Job the namespace quota refuses a Pod for: the Job exists, no Pod is
    # ever created, and a `FailedCreate` event names the quota, as the Job controller
    # records it.
    quota_refused_roles: set[str] = field(default_factory=set)
    # Every event recorded, which outlives the object it names, as on an API server.
    events: list[dict[str, Any]] = field(default_factory=list)
    _uids: int = 0
    _rv_counter: int = 1

    # ----- test controls ------------------------------------------------

    def __post_init__(self) -> None:
        if not self.canary_answer:
            self.canary_answer = "unreachable" if self.egress_enforced else "reachable"

    def script(self, attempt_id: str, behavior: str, *, after: int = 1) -> None:
        if behavior not in BEHAVIORS:
            raise ValueError(f"unknown fake behavior {behavior!r}")
        self.scripts[attempt_id] = (behavior, after)

    def script_all(self, behavior: str, *, after: int = 1) -> None:
        if behavior not in BEHAVIORS:
            raise ValueError(f"unknown fake behavior {behavior!r}")
        self.default_behavior = (behavior, after)

    def remove_pod_out_of_band(self, attempt_id: str) -> None:
        """An operator, an eviction, or a node that went away (26)."""
        for key, obj in list(self.objects.items()):
            labels = (obj.body.get("metadata") or {}).get("labels") or {}
            if key[0] == "pods" and labels.get(LABEL_ATTEMPT) == attempt_id:
                del self.objects[key]

    def evict(self, attempt_id: str) -> None:
        for key, obj in self.objects.items():
            labels = (obj.body.get("metadata") or {}).get("labels") or {}
            if key[0] == "pods" and labels.get(LABEL_ATTEMPT) == attempt_id:
                obj.body["status"] = {"phase": "Failed", "reason": "Evicted"}

    def object_names(self, kind: str) -> list[str]:
        return sorted(name for (k, name) in self.objects if k == kind)

    def secret_exists(self, name: str) -> bool:
        return ("secrets", name) in self.objects

    def put_harness_secret(
        self, name: str, data: Mapping[str, bytes], labels: Mapping[str, str] | None = None
    ) -> None:
        self.objects[("secrets", name)] = _Object(
            "secrets",
            name,
            {
                "metadata": {
                    "name": name,
                    "namespace": self.namespace,
                    "resourceVersion": str(self._next_rv()),
                    **({"labels": dict(labels)} if labels else {}),
                },
                "data": {k: base64.b64encode(v).decode("ascii") for k, v in data.items()},
            },
        )

    def harness_secret(self, name: str) -> dict[str, bytes]:
        obj = self.objects.get(("secrets", name))
        if obj is None:
            return {}
        return {k: base64.b64decode(v) for k, v in (obj.body.get("data") or {}).items()}

    # ----- the client surface -------------------------------------------

    def fail_next(
        self, call: str, count: int = 1, *, kind: str = "", error: Exception | None = None
    ) -> None:
        """Fail the next matching calls with `error`, or a 503 by default."""
        self.outages.append([call, kind, count, error])

    def _next_rv(self) -> int:
        rv = self._rv_counter
        self._rv_counter += 1
        return rv

    def _outage(self, call: str, kind: str = "") -> None:
        for entry in self.outages:
            if entry[0] == call and entry[1] in ("", kind) and entry[2] > 0:
                entry[2] -= 1
                if len(entry) > 3 and entry[3] is not None:
                    raise entry[3]
                raise KubernetesUnavailableError(503, f"the fake API server is down ({call})")

    def version(self) -> str:
        return "v1.31.0"

    def create(self, kind: str, body: Mapping[str, Any]) -> dict[str, Any]:
        self.create_attempts.append(kind)
        self._outage("create", kind)
        metadata = dict(body.get("metadata") or {})
        name = str(metadata.get("name", ""))
        if kind in self.refuse_create:
            raise KubernetesApiError(500, f"the fake refuses to create a {kind}")
        role = str((metadata.get("labels") or {}).get(LABEL_ROLE, ""))
        if role and role in self.refuse_roles:
            raise KubernetesApiError(500, f"the fake refuses to create the {role} {kind}")
        if (kind, name) in self.objects:
            raise KubernetesApiError(409, f"{kind}/{name} already exists")
        stored: dict[str, Any] = json.loads(json.dumps(dict(body)))
        stored.setdefault("metadata", {})["resourceVersion"] = str(self._next_rv())
        if kind == "jobs" or (
            kind == "configmaps"
            and (stored.get("metadata") or {}).get("labels", {}).get(LABEL_ROLE) == ROLE_LOGIN_LOCK
        ):
            # The API server gives every object a uid; the login lock is deleted by it,
            # and a Job's events name it.
            self._uids += 1
            stored["metadata"]["uid"] = f"uid-{self._uids}"
        self.objects[(kind, name)] = _Object(kind, name, stored)
        self.created.append({"kind": kind, "name": name, "body": stored})
        if kind == "persistentvolumeclaims":
            self.claims.setdefault(name, {})
        if kind == "jobs":
            self._start_job(stored)
        if kind == "pods":
            stored.setdefault("spec", {})["nodeName"] = self.node_name
            self._start_pod(stored, owner=None)
        return stored

    def get(self, kind: str, name: str) -> dict[str, Any]:
        self._outage("get", kind)
        obj = self.objects.get((kind, name))
        if obj is None:
            raise KubernetesApiError(404, f"{kind}/{name} not found")
        if kind == "pods":
            self._advance(obj)
        return obj.body

    def list_objects(
        self, kind: str, *, label_selector: str | None = None, field_selector: str | None = None
    ) -> list[dict[str, Any]]:
        self._outage("list_objects", kind)
        if kind == "events":
            return self._events(field_selector)
        wanted = _parse_selector(label_selector)
        out: list[dict[str, Any]] = []
        for (stored_kind, _), obj in list(self.objects.items()):
            if stored_kind != kind:
                continue
            labels = (obj.body.get("metadata") or {}).get("labels") or {}
            if any(labels.get(k) != v for k, v in wanted.items() if v is not None):
                continue
            if any(k not in labels for k, v in wanted.items() if v is None):
                continue
            if kind == "pods":
                self._advance(obj)
            out.append(obj.body)
        return out

    def delete(
        self,
        kind: str,
        name: str,
        *,
        grace_period_seconds: int | None = None,
        propagation: str = "Background",
        uid: str | None = None,
    ) -> None:
        self._outage("delete", kind)
        obj = self.objects.get((kind, name))
        if obj is None:
            return
        if uid is not None and (obj.body.get("metadata") or {}).get("uid") != uid:
            raise KubernetesApiError(409, f"{kind}/{name} precondition failed: uid differs")
        if kind == "pods":
            labels = (obj.body.get("metadata") or {}).get("labels") or {}
            worker = self.workers.get(str(labels.get(LABEL_ATTEMPT, "")))
            if (
                labels.get(LABEL_ROLE) == ROLE_WORKER
                and worker is not None
                and grace_period_seconds
                and worker.behavior in ("hang", "immortal")
            ):
                # A worker that ignores SIGTERM: the kubelet waits the grace period and
                # the Pod is still there until the kill deletes it with grace zero.
                worker.drained = True
                obj.body.setdefault("metadata", {})["deletionTimestamp"] = "2026-09-21T00:00:00Z"
                return
        self.deleted.append((kind, name))
        del self.objects[(kind, name)]
        if kind == "jobs" and propagation != "Orphan":
            for key, other in list(self.objects.items()):
                labels = (other.body.get("metadata") or {}).get("labels") or {}
                if key[0] == "pods" and labels.get("job-name") == name:
                    del self.objects[key]
        if kind == "persistentvolumeclaims":
            self.claims.pop(name, None)

    def patch(
        self,
        kind: str,
        name: str,
        body: Mapping[str, Any],
        *,
        resource_version: str | None = None,
    ) -> dict[str, Any]:
        self._outage("patch", kind)
        # 339: optimistic concurrency on the patch body's metadata.resourceVersion
        # so the API server returns 409 Conflict when the stored object has changed
        # since the read (339).
        obj = self.objects.get((kind, name))
        if obj is None:
            raise KubernetesApiError(404, f"{kind}/{name} not found")
        body_version = dict(body).get("metadata", {}).get("resourceVersion")
        # resource_version keyword argument is the caller's encoding of the same
        # information; it is checked when the body does not carry metadata.
        if body_version is None and resource_version is not None:
            body_version = resource_version
        if body_version is not None:
            current_version = obj.body.get("metadata", {}).get("resourceVersion")
            if current_version is not None and current_version != body_version:
                raise KubernetesApiError(409, "conflict: resource version mismatch")
        _merge(obj.body, dict(body))
        obj.body.setdefault("metadata", {})["resourceVersion"] = str(self._next_rv())
        return obj.body

    def pod_log(
        self,
        name: str,
        *,
        container: str | None = None,
        since_time: str | None = None,
        timestamps: bool = True,
        timeout: float | None = None,
        limit_bytes: int | None = None,
        tail_lines: int | None = None,
    ) -> list[LogFrame]:
        self._outage("pod_log", "pods")
        self.log_reads.append(
            {
                "name": name,
                "since_time": since_time,
                "limit_bytes": limit_bytes,
                "tail_lines": tail_lines,
            }
        )
        lines = self.logs.get(name, [])
        if not timestamps:
            # Worker lines carry the API server's timestamp prefix in this fake. The
            # readiness canary's fixture and a preparer failure are the raw,
            # un-timestamped body already: only a genuine stamp prefix is stripped, so
            # a real un-timestamped line keeps every word (75).
            lines = [_strip_stamp(line) for line in lines]
        run = self.login_runs.get(name)
        if run is not None:
            self._advance_login(name, run)
            lines = self.logs.get(name, [])
            if not timestamps:
                lines = [_strip_stamp(line) for line in lines]
        if since_time:
            # The API server passes `sinceTime` on to the kubelet at one-second
            # granularity, and the bound is inclusive.
            floor = parse_rfc3339(since_time).replace(microsecond=0)
            lines = [line for line in lines if _line_time(line) >= floor]
        payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
        if tail_lines is not None:
            # Keep only the last N lines before applying the byte cap.
            lines = lines[-tail_lines:]
            payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
        if limit_bytes is not None:
            # `limitBytes` stops the stream where it lands, mid-line included.
            payload = payload[:limit_bytes]
        return [LogFrame("stdout", payload)] if payload else []

    def pod_exec(
        self,
        name: str,
        command: Sequence[str],
        *,
        container: str | None = None,
        timeout: float | None = None,
        limit: int = 0,
        stdin: bytes | None = None,
        stdout_to: IO[bytes] | None = None,
    ) -> ExecResult:
        self._outage("pod_exec", "pods")
        result = self._exec(name, command, stdin)
        if stdout_to is None:
            return result
        stdout_to.write(result.stdout)
        return ExecResult(b"", result.stderr, result.exit_code, stdout_size=len(result.stdout))

    def pod_exec_to(
        self,
        name: str,
        command: Sequence[str],
        into: IO[bytes],
        *,
        container: str | None = None,
        timeout: float | None = None,
        limit: int = 0,
    ) -> ExecResult:
        return self.pod_exec(name, command, container=container, limit=limit, stdout_to=into)

    def _events(self, field_selector: str | None) -> list[dict[str, Any]]:
        """The recorded events a field selector matches (kind, name, uid, reason)."""
        fields = dict(
            part.split("=", 1) for part in (field_selector or "").split(",") if "=" in part
        )
        out: list[dict[str, Any]] = []
        for event in self.events:
            involved = event.get("involvedObject") or {}
            wanted = {
                "involvedObject.kind": involved.get("kind"),
                "involvedObject.name": involved.get("name"),
                "involvedObject.uid": involved.get("uid"),
                "reason": event.get("reason"),
            }
            if all(fields.get(key) in (None, value) for key, value in wanted.items()):
                out.append(event)
        return out

    def _exec(self, name: str, command: Sequence[str], stdin: bytes | None) -> ExecResult:
        obj = self.objects.get(("pods", name))
        if obj is None:
            raise KubernetesApiError(404, f"pods/{name} not found")
        if stdin is not None:
            self.exec_stdin.setdefault(name, []).append(stdin)
        run = self.login_runs.get(name)
        if run is not None:
            return self._login_exec(name, run, command[-1], stdin)
        claim = self._claim_of(obj)
        script = command[-1]
        if ACTIVITY_MARKER in script:
            return self._activity(name, claim)
        if "tar cf -" in script:
            if f"output/{CHANGED_BLOBS_DIR}" in script and "--exclude" not in script:
                return ExecResult(_changed_blobs_tar(claim), b"", 0)
            return ExecResult(_tar(claim), b"", 0)
        match = re.match(r"^p='(?P<path>[^']*)'", script)
        if match is None:
            return ExecResult(b"", b"the fake has no answer for this command\n", 1)
        wanted = match.group("path").split("/crucible/work/", 1)[-1]
        data = claim.get(wanted)
        if data is None:
            return ExecResult(b"absent\n", b"", 0)
        return ExecResult(b"ok\n" + base64.b64encode(data), b"", 0)

    def _activity(self, name: str, claim: Mapping[str, bytes]) -> ExecResult:
        """FDY-0140: the activity probe's one line. A test scripts it per Pod through
        `activity_answers` (one answer used per ask, the last one kept); otherwise it is
        derived from the claim's checkout and report leaves."""
        answers = self.activity_answers.get(name)
        if answers:
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            return ExecResult(answer, b"", 0)
        files = sorted((k, v) for k, v in claim.items() if k.startswith(("repo/", "report/")))
        newest = int(hashlib.sha256(repr(files).encode()).hexdigest()[:12], 16)
        total = sum(len(v) for _, v in files)
        return ExecResult(f"activity {newest} {len(files)} {total}\n".encode(), b"", 0)

    # ----- acting out a role ---------------------------------------------

    def _claim_of(self, obj: _Object) -> dict[str, bytes]:
        for volume in (obj.body.get("spec") or {}).get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                return self.claims.setdefault(str(claim), {})
        return {}

    def _start_job(self, job: Mapping[str, Any]) -> None:
        name = str((job.get("metadata") or {}).get("name", ""))
        template = (job.get("spec") or {}).get("template") or {}
        labels = dict((template.get("metadata") or {}).get("labels") or {})
        labels["job-name"] = name
        attempt_id = str(labels.get(LABEL_ATTEMPT, ""))
        if name in self.no_pod_yet or attempt_id in self.no_pod_yet:
            return
        if str(labels.get(LABEL_ROLE, "")) in self.quota_refused_roles:
            uid = str((job.get("metadata") or {}).get("uid") or "")
            self.events.append(
                {
                    "reason": "FailedCreate",
                    "involvedObject": {"kind": "Job", "name": name, "uid": uid},
                    "message": (
                        f'Error creating: pods "{name}-abc12" is forbidden: exceeded quota: '
                        "crucible-workers, requested: limits.memory=4Gi, used: "
                        "limits.memory=12Gi, limited: limits.memory=12Gi"
                    ),
                }
            )
            return
        pod_name = f"{name}-abc12"
        pod: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": self.namespace, "labels": labels},
            "spec": {**dict(template.get("spec") or {}), "nodeName": self.node_name},
        }
        self.objects[("pods", pod_name)] = _Object("pods", pod_name, pod)
        # The Job controller counts the Pod it created, which is how `launch` knows the
        # quota admitted it without listing Pods (hades #423).
        stored = self.objects.get(("jobs", name))
        if stored is not None:
            stored.body.setdefault("status", {})["active"] = 1
        self._start_pod(pod, owner=name)

    def _start_pod(self, pod: Mapping[str, Any], owner: str | None) -> None:
        metadata = pod.get("metadata") or {}
        name = str(metadata.get("name", ""))
        labels = metadata.get("labels") or {}
        role = str(labels.get(LABEL_ROLE, ""))
        attempt_id = str(labels.get(LABEL_ATTEMPT, ""))
        obj = self.objects[("pods", name)]
        if name in self.pending_forever or attempt_id in self.pending_forever:
            obj.body["status"] = {
                "phase": "Pending",
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": "0/1 nodes are available",
                    }
                ],
            }
            return
        if role in (ROLE_READER, ROLE_WORKER):
            obj.body["status"] = _running(self.node_name)
        else:
            obj.body["status"] = _running(self.node_name)
        handler = {
            ROLE_PREPARER: self._act_preparer,
            ROLE_CACHE_REFRESHER: self._act_cache_refresher,
            ROLE_COLLECTOR: self._act_collector,
            ROLE_BUNDLE: self._act_bundle,
            ROLE_VERIFIER: self._act_verifier,
            ROLE_CLEANER: self._act_cleaner,
            ROLE_CANARY: self._act_canary,
            ROLE_WORKER: self._act_worker,
            ROLE_LOGIN: self._act_login,
            ROLE_PUBLISHER: self._act_publisher,
        }.get(role)
        if handler is not None:
            handler(obj, attempt_id)

    def _behavior(self, attempt_id: str, obj: _Object) -> tuple[str, int]:
        scripted = self.scripts.get(attempt_id)
        if scripted is not None:
            return scripted
        image = ""
        for container in (obj.body.get("spec") or {}).get("containers") or []:
            image = str(container.get("image", ""))
        match = _TAG.match(self.image_tags.get(image, image).split("@", 1)[0])
        if match is None or match.group("behavior") not in BEHAVIORS:
            return self.default_behavior or ("succeed", 1)
        return match.group("behavior"), int(match.group("n") or 1)

    def _deadline_exceeded(self, obj: _Object, worker: _Worker) -> None:
        worker.terminated = True
        worker.exit_code = 137
        job_name = str(((obj.body.get("metadata") or {}).get("labels") or {}).get("job-name", ""))
        job = self.objects.get(("jobs", job_name))
        if job is not None:
            condition = {
                "status": "True",
                "reason": "DeadlineExceeded",
                "message": "Job was active longer than specified deadline",
            }
            job.body["status"] = {
                "failed": 1,
                "conditions": [
                    {"type": "FailureTarget", **condition},
                    {"type": "Failed", **condition},
                ],
            }
        if self.deadline_keeps_pod:
            self._finish(obj, 137, reason="Error")
        else:
            self.objects.pop(("pods", obj.name), None)

    def _finish(self, obj: _Object, code: int, *, reason: str = "Completed") -> None:
        obj.body["status"] = {
            "phase": "Succeeded" if code == 0 else "Failed",
            "containerStatuses": [
                {
                    "name": CONTAINER_NAME,
                    "state": {"terminated": {"exitCode": code, "reason": reason}},
                }
            ],
        }

    def _canary_policy(self, canary_id: str) -> dict[str, Any] | None:
        for (kind, _name), candidate in self.objects.items():
            if kind != "networkpolicies" or candidate.deleted:
                continue
            selector = (candidate.body.get("spec") or {}).get("podSelector") or {}
            if canary_id and (selector.get("matchLabels") or {}).get(LABEL_CANARY) == canary_id:
                return candidate.body
        return None

    def _act_canary(self, obj: _Object, attempt_id: str) -> None:
        name = obj.name
        if self.pod_pid_limit_source == "cgroupns-private":
            pod_pids = "unknown"
        elif self.pod_pid_limit_source == "cgroup-v1":
            pod_pids = "unsupported"
        else:
            pod_pids = "none" if self.pod_pid_limit is None else str(self.pod_pid_limit)
        labels = (obj.body.get("metadata") or {}).get("labels") or {}
        policy = self._canary_policy(str(labels.get(LABEL_CANARY, "")))
        dns_open = policy is not None and any(
            any(int(port.get("port", 0)) == 53 for port in rule.get("ports") or [])
            and any(_opens_dns(peer, self.translates_services) for peer in rule.get("to") or [])
            for rule in (policy.get("spec") or {}).get("egress") or []
        )
        dns = self.canary_dns if dns_open or self.canary_dns == "inconclusive" else "failed"
        env = {
            item["name"]: item.get("value", "")
            for container in (obj.body.get("spec") or {}).get("containers") or []
            for item in container.get("env") or []
        }
        endpoint = self.canary_endpoint if env.get("CRUCIBLE_CANARY_ENDPOINT_URL") else "none"
        # The namespace-scope canary answers the API server and the PID limit only.
        egress_lines = (
            []
            if env.get("CRUCIBLE_CANARY_SCOPE") == "namespace"
            else [
                f"crucible-canary.dns_tool={'none' if dns == 'inconclusive' else 'getent'}",
                f"crucible-canary.dns={dns}",
                f"crucible-canary.endpoint={endpoint}",
            ]
        )
        self.logs[name] = [
            "crucible-canary.tool=curl",
            f"crucible-canary.api={self.canary_answer}",
            f"crucible-canary.curl_exit={'7' if self.egress_enforced else '0'}",
            *egress_lines,
            # The container's own cgroup limit, kept for diagnostics only (95): the
            # gate never decides on this number.
            "crucible-canary.pids=19093",
            f"crucible-canary.pod_pids={pod_pids}",
            f"crucible-canary.pod_pids_source={self.pod_pid_limit_source}",
            *(["crucible-canary.done=1"] if self.canary_done else []),
        ]
        self._finish(obj, 0)

    def _act_cache_refresher(self, obj: _Object, attempt_id: str) -> None:
        self._finish(obj, 0)

    def _act_preparer(self, obj: _Object, attempt_id: str) -> None:
        behavior, _ = self._behavior(attempt_id, obj)
        claim = self._claim_of(obj)
        container = ((obj.body.get("spec") or {}).get("containers") or [{}])[0]
        if PUBLISH_LEAF_MARKER in str((container.get("command") or [""])[-1]):
            # `scripts.publish_leaf_script`: the bundle must be a file, and the leaf a
            # directory the worker uid can write. A dict claim has no empty directories.
            if PUBLISH_BUNDLE_LEAF not in claim:
                self.logs[obj.name] = [f"no branch bundle at {PUBLISH_BUNDLE_LEAF}"]
                self._finish(obj, 7, reason="Error")
            elif self.publish_leaf_unwritable:
                self.logs[obj.name] = [f"{PUBLISH_LEAF} is not owned and writable as output is"]
                self._finish(obj, 8, reason="Error")
            else:
                self._finish(obj, 0)
            return
        if behavior == "prepare-fails":
            self.logs[obj.name] = ["the fake preparer could not clone"]
            self._finish(obj, 3, reason="Error")
            return
        if not self.claims_suppress_head:
            claim["output/prepared-head.txt"] = (synthetic_head_sha(attempt_id) + "\n").encode()
        claim["output/started-from.txt"] = b"main\n"
        claim["repo/.git/HEAD"] = b"ref: refs/heads/crucible\n"
        self._finish(obj, 0)

    def _spec_of(self, attempt_id: str) -> _SpecStub | None:
        """What the attempt's roles know about the contract: the identity ConfigMap the
        provider created, which is exactly what the worker itself was given (06)."""
        obj = self.objects.get(("configmaps", f"identity-{attempt_id.lower()}"))
        if obj is None:
            return None
        raw = (obj.body.get("data") or {}).get("contract.json")
        if not raw:
            return None
        contract = json.loads(str(raw))
        return _SpecStub(
            attempt_id=attempt_id,
            external_id=str(contract.get("external_id", "")),
            contract=contract,
        )

    def _act_collector(self, obj: _Object, attempt_id: str) -> None:
        behavior, _ = self._behavior(attempt_id, obj)
        claim = self._claim_of(obj)
        spec = self._spec_of(attempt_id)
        head = synthetic_head_sha(attempt_id)
        contract = dict(getattr(spec, "contract", {}) or {})
        repository = contract.get("repository", {})
        paths = changed_paths(contract, behavior)
        claim["output/head.txt"] = (head + "\n").encode()
        claim["output/branch.txt"] = (str(repository.get("work_branch", "")) + "\n").encode()
        claim["output/commits.txt"] = b"0\n" if behavior == "no-commits" else b"1\n"
        claim["output/changed.txt"] = ("\n".join(paths) + "\n").encode()
        claim["output/diff.patch"] = synthetic_diff(paths, behavior).encode()
        claim[f"output/{REVIEW_DIFF_DIR}/diff.patch"] = synthetic_review_diff(
            paths, behavior
        ).encode()
        claim["output/commit-paths.txt"] = ("\n".join(paths) + "\n").encode()
        claim["output/log.txt"] = (
            b"" if behavior == "no-commits" else f"{head}\x1fa fake commit\x1ffake\x1e".encode()
        )
        claim["output/work_branch.bundle"] = b"fake bundle bytes"
        claim["output/copy-rejections.tsv"] = b""
        claim["output/collector.ok"] = b"done\n"
        claim["output/bundle.log"] = b""
        # hades FDY-0135: the collector's commit check, as the Docker fake reports it.
        check = fake_commit_policy(behavior, head)
        claim["output/commit-policy/author-problems.txt"] = "".join(
            f"{sha}\t{who}\n" for sha, who in check.author_problems
        ).encode()
        claim["output/commit-policy/checked"] = b"done\n"
        report = self._report_for(spec, behavior, head)
        if report is not None:
            claim["output/report/report.yaml"] = report
        if behavior == "blocked":
            claim["output/report/blocked.md"] = (
                "reason: ambiguous_contract\n\n# Blocked\n\n"
                f"The fake worker for {attempt_id} needs a decision.\n"
            ).encode()
        self._finish(obj, 0)

    def _report_for(self, spec: Any, behavior: str, head: str) -> bytes | None:
        import yaml  # noqa: PLC0415

        if spec is None:
            return None
        if behavior == "bad-report":
            return b"title: c5: live run\nsummary: x\n"
        if behavior in REVIEW_BEHAVIORS:
            verdict = "approve" if behavior == "review" else "request_changes"
            return yaml.safe_dump(default_review_report(spec, head, verdict)).encode()
        if behavior in REPORTING_BEHAVIORS:
            return yaml.safe_dump(default_report(spec, head, behavior)).encode()
        return None

    def _act_bundle(self, obj: _Object, attempt_id: str) -> None:
        self._finish(obj, 0)

    def _act_verifier(self, obj: _Object, attempt_id: str) -> None:
        from crucible.adapters.execution.scripts import encode_check_id  # noqa: PLC0415

        behavior, _ = self._behavior(attempt_id, obj)
        claim = self._claim_of(obj)
        spec = self._spec_of(attempt_id)
        checks = [
            check
            for check in (getattr(spec, "contract", {}) or {}).get("required_verification", [])
            if str(check.get("kind", "command")) == "command"
        ]
        for index, check in enumerate(checks):
            expect = int(check.get("expect_exit", 0))
            failed = behavior == "verification-fails" and index == 0
            safe = encode_check_id(str(check.get("id")))
            claim[f"verify/{safe}.exit"] = f"{expect + 1 if failed else expect}\n".encode()
            claim[f"verify/{safe}.log"] = f"fake verifier re-ran {check.get('command')!r}".encode()
        self._finish(obj, 0)

    def _act_cleaner(self, obj: _Object, attempt_id: str) -> None:
        claim = self._claim_of(obj)
        script = ""
        for container in (obj.body.get("spec") or {}).get("containers") or []:
            script = str((container.get("command") or ["", "", ""])[-1])
        for leaf in re.findall(r'"/crucible/work/([^"]+)"', script):
            for path in [p for p in claim if p == leaf or p.startswith(f"{leaf}/")]:
                del claim[path]
        self._finish(obj, 0)

    def _act_publisher(self, obj: _Object, attempt_id: str) -> None:
        """The publisher script's observable contract, acted out: the token from the
        mounted Secret, the bundle from its single-file mount, the seal, then a push, with
        the outcome files on the claim's `publish` leaf where the reader finds them."""
        spec = obj.body.get("spec") or {}
        claim = self._claim_of(obj)
        container = (spec.get("containers") or [{}])[0]
        script = str((container.get("command") or [""])[-1])

        def bound(name: str) -> str:
            found = re.search(rf"^{name}='([^']*)'$", script, re.MULTILINE)
            return found.group(1) if found else ""

        bundle_leaf = ""
        for mount in container.get("volumeMounts") or []:
            if str(mount.get("mountPath", "")).endswith("/work_branch.bundle"):
                bundle_leaf = str(mount.get("subPath") or "")
        secret_name = ""
        for volume in spec.get("volumes") or []:
            if volume.get("name") == "publish-token":
                secret_name = str((volume.get("secret") or {}).get("secretName") or "")
        token = self.harness_secret(secret_name).get("token", b"") if secret_name else b""
        out: dict[str, bytes] = {}

        def finish(code: int) -> None:
            for key in [k for k in claim if k.startswith("publish/")]:
                del claim[key]
            for name, body in out.items():
                claim[f"publish/{name}"] = body
            self._finish(obj, code)

        if not token:
            out.update({"step.txt": b"no-token\n", "error.txt": b"no token arrived\n"})
            return finish(3)
        if MERGE_MAIN_MARKER in script:
            merge = {
                "remote": bound("CLONE_URL"),
                "branch": bound("WORK_BRANCH"),
                "base": bound("BASE_REF"),
                "lease": bound("EXPECTED"),
                "token": token.decode("utf-8"),
            }
            self.merges.append(merge)
            out["remote-head-before.txt"] = f"{merge['lease']}\n".encode()
            if self.merge_main_conflicts:
                out["conflicts.txt"] = "".join(f"{p}\n" for p in self.merge_main_conflicts).encode()
                out.update({"step.txt": b"conflict\n", "error.txt": b"conflicts\n"})
                return finish(6)
            out["merge-head.txt"] = f"{self.merge_main_head}\n".encode()
            push = {**merge, "head": self.merge_main_head}
            self.pushes.append(push)
            if self.on_push is not None:
                self.on_push(push)
            out.update({"step.txt": b"done\n", "push.txt": b"ok\n"})
            return finish(0)
        bundle = claim.get(bundle_leaf) if bundle_leaf else None
        out["step.txt"] = b"bundle-seal\n"
        if bundle is None:
            out["error.txt"] = b"no branch bundle where the collector left it\n"
            return finish(7)
        if hashlib.sha256(bundle).hexdigest() != bound("SEAL"):
            out["error.txt"] = b"the branch bundle no longer matches its sealed sha256\n"
            return finish(7)
        expected = bound("EXPECTED")
        out["bundle-head.txt"] = f"{expected}\n".encode()
        out["remote-head-before.txt"] = b"\n"
        out["publisher.log"] = b"fake publisher: fetched, verified\n"
        push = {
            "remote": bound("CLONE_URL"),
            "branch": bound("WORK_BRANCH"),
            "head": expected,
            "token": token.decode("utf-8"),
        }
        self.pushes.append(push)
        if self.on_push is not None:
            self.on_push(push)
        out.update({"step.txt": b"done\n", "push.txt": b"ok\n"})
        return finish(0)

    def _act_login(self, obj: _Object, attempt_id: str) -> None:
        """The login Pod's driver: the CLI's lines in the log, stamped as the API server
        stamps them, then the prompt it waits at, if it has one."""
        script = self.login
        self.login_runs[obj.name] = _LoginRun(script=script)
        shown = [*script.lines, *([script.prompt] if script.prompt else [])]
        self.logs[obj.name] = [f"{_stamp(i)} {line}" for i, line in enumerate(shown)]

    def _advance_login(self, name: str, run: _LoginRun) -> None:
        run.reads += 1
        if run.exited or run.script.never_exits:
            return
        if run.script.prompt is not None and not run.codes:
            return
        if run.script.prompt is None and run.reads < run.script.finish_after:
            return
        run.exited = True
        lines = self.logs.setdefault(name, [])
        for line in [*run.script.after_code, f"crucible-login.exit={run.script.exit_code}"]:
            lines.append(f"{_stamp(len(lines))} {line}")

    def _login_exec(
        self, name: str, run: _LoginRun, script: str, stdin: bytes | None
    ) -> ExecResult:
        if "CRUCIBLE_LOGIN_CONTROL" in script and '/in"' in script:
            if stdin is None:
                return ExecResult(b"", b"no code on stdin\n", 3)
            run.codes.append(stdin)
            return ExecResult(b"", b"", 0)
        match = re.match(r"^p='(?P<path>[^']*)'", script)
        if match is None:
            return ExecResult(b"", b"the fake has no answer for this command\n", 1)
        if not run.exited:
            return ExecResult(b"absent\n", b"", 0)
        data = run.script.files.get(match.group("path"))
        if data is None:
            return ExecResult(b"absent\n", b"", 0)
        return ExecResult(b"ok\n" + base64.b64encode(data), b"", 0)

    def _seed_claim(self, obj: _Object) -> None:
        """The `credential-seed` init container: the per-attempt Secret's projected
        files, copied into the claim's `credential` leaf (26)."""
        spec = obj.body.get("spec") or {}
        if not any(c.get("name") == "credential-seed" for c in spec.get("initContainers") or []):
            return
        for volume in spec.get("volumes") or []:
            if volume.get("name") != "cred-source":
                continue
            source = volume.get("secret") or {}
            secret = self.objects.get(("secrets", str(source.get("secretName", ""))))
            if secret is None:
                return
            data = secret.body.get("data") or {}
            claim = self._claim_of(obj)
            for item in source.get("items") or []:
                raw = data.get(item.get("key"))
                if raw is not None:
                    claim[f"credential/{item.get('path')}"] = base64.b64decode(str(raw))

    def _act_worker(self, obj: _Object, attempt_id: str) -> None:
        self._seed_claim(obj)
        behavior, after = self._behavior(attempt_id, obj)
        self.workers[attempt_id] = _Worker(behavior=behavior, remaining=after)
        self.logs[obj.name] = [
            f"{_stamp(0)} fake worker {behavior} start",
        ]

    def _advance(self, obj: _Object) -> None:
        """One observation of a worker Pod. Every other role is already terminal."""
        labels = (obj.body.get("metadata") or {}).get("labels") or {}
        if labels.get(LABEL_ROLE) != ROLE_WORKER:
            return
        attempt_id = str(labels.get(LABEL_ATTEMPT, ""))
        worker = self.workers.get(attempt_id)
        if worker is None or worker.terminated:
            return
        if worker.behavior in ("hang", "immortal"):
            if self.job_deadline_fires:
                self._deadline_exceeded(obj, worker)
            return
        worker.observations += 1
        if worker.observations < worker.remaining:
            return
        if worker.behavior == "vanish":
            worker.terminated = True
            self.objects.pop(("pods", obj.name), None)
            return
        code = {
            "blocked": 75,
            "blocked-nofile": 75,
            "crash": 1,
            "oom": 137,
            "environment": 70,
            "quota": 1,
        }.get(worker.behavior, 0)
        worker.terminated = True
        worker.exit_code = code
        reason = "OOMKilled" if worker.behavior == "oom" else "Completed"
        self.logs.setdefault(obj.name, []).append(f"{_stamp(1)} fake worker exit {code}")
        if worker.behavior == "quota":
            self.logs[obj.name].append(
                f'{_stamp(2)} {{"type":"turn.failed","error":{{"code":"usage_limit_reached"}}}}'
            )
        self._finish(obj, code, reason=reason)


# ----- helpers -----------------------------------------------------------


def _running(node: str) -> dict[str, Any]:
    return {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "True"}],
        "containerStatuses": [{"name": CONTAINER_NAME, "state": {"running": {}}}],
        "hostIP": "10.10.0.1",
        "nodeName": node,
    }


def _line_time(line: str) -> datetime:
    stamp = line.partition(" ")[0]
    try:
        return parse_rfc3339(stamp)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def _stamp(offset: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1_760_000_000 + offset)) + ".000000000Z"


_STAMP_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ")


def _strip_stamp(line: str) -> str:
    """Remove a `_stamp`-shaped prefix if the line has one. A line with none is a real
    API un-timestamped read already (the readiness canary's fixture, a preparer
    failure): returning it unchanged, rather than partitioning on the first space it
    happens to contain, is what keeps a multi-word raw line intact (75)."""
    match = _STAMP_PREFIX.match(line)
    return line[match.end() :] if match else line


def _parse_selector(selector: str | None) -> dict[str, str | None]:
    if not selector:
        return {}
    out: dict[str, str | None] = {}
    for part in selector.split(","):
        key, sep, value = part.partition("=")
        out[key.strip()] = value.strip() if sep else None
    return out


def _merge(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    """A JSON merge patch (RFC 7386): a null removes the key."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _tar(claim: Mapping[str, bytes]) -> bytes:
    """What the reader Pod's `tar cf -` produces off the claim, minus `output/tree` and
    the exported blobs (hades #398), which come back as their own stream."""
    return _tar_of(
        claim,
        lambda path: (
            path.startswith(("output/", "verify/"))
            and not path.startswith(("output/tree/", f"output/{CHANGED_BLOBS_DIR}/"))
        ),
    )


def _changed_blobs_tar(claim: Mapping[str, bytes]) -> bytes:
    """The reader's second stream: `output/changed-blobs` alone, for the scanner."""
    return _tar_of(claim, lambda path: path.startswith(f"output/{CHANGED_BLOBS_DIR}/"))


def _tar_of(claim: Mapping[str, bytes], wanted: Callable[[str], bool]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, content in sorted(claim.items()):
            if not wanted(path):
                continue
            info = tarfile.TarInfo(path)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


__all__ = ["FakeKubernetesApi", "FakeLogin", "FakeRegistry"]
