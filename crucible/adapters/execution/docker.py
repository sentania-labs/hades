"""The Docker execution provider (08, 13).

Talks to the daemon only through the socket proxy. Creates workers, collectors, bundle
verifiers, and verifiers; nothing it creates ever receives the socket, the proxy
endpoint, the database, or another attempt's credential mount.

Four containers per attempt, after the preparer (and, for a remote repository with the
reference cache on, the cache refresher that runs just before it, hades #137):

- the **worker**, from the allowlisted harness image, on the internal workers network
  with the egress proxy as its only way out;
- the **collector**, `--network none`, the checkout and the report directory read-only,
  which produces the diff, the changed paths, the head, the log, the bundle, a fresh
  tree, and a copy of the report;
- the **bundle verifier**, `--network none`, the collector's output read-only, which
  runs `git bundle verify` and nothing else;
- the **verifier**, network per policy, which re-runs each `required_verification`
  command from the collected tree. It is the one container that runs commands the
  repository defines, so it sees its own tree and its own log directory, never the
  collector's output.

The credential (12): the preparer creates an empty `credential` directory in the
workspace owned by the worker's uid; the provider seeds it with only the named auth
files through the daemon's archive endpoint before the worker starts, mounts it at the
path the harness expects (read-only or narrow-writable per the adapter and the
configuration) with the Crucible-owned templates read-only on top, reads the named files
back the same way after exit, syncs back only a valid, newer file, and removes the copy
at once. No credential value is ever in `Env`, in `Cmd`, in a bind source, in a log, or
in this process's argv.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import select
import shutil
import tarfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any, Literal

from crucible.adapters.execution import identity as identity_bundle
from crucible.adapters.execution import scripts, workspace
from crucible.adapters.execution.collected import (
    read_outputs as _read_outputs,
)
from crucible.adapters.execution.collected import (
    read_verifications as _read_verifications,
)
from crucible.adapters.execution.create_policy import (
    CreatePolicy,
    CreateRequestRefusedError,
    image_allowed,
)
from crucible.adapters.execution.create_policy import (
    check as check_create,
)
from crucible.adapters.execution.dockerapi import DockerApiError, DockerClient, LogFrame
from crucible.adapters.execution.endpoint_health import probe_model_endpoint
from crucible.adapters.execution.logstream import chunks as _chunks
from crucible.adapters.harness.registry import default_registry
from crucible.application.credential_renewer import (
    access_token_document,
    atomic_write,
    worker_credential_spec,
)
from crucible.application.harnesses import (
    HarnessRegistry,
    check_image_version,
    effective_mount_mode,
    egress_allowlist,
)
from crucible.contracts.completion_claim import CompletionClaimV1
from crucible.domain.exit_class import ExitClass
from crucible.domain.ids import new_id
from crucible.domain.secrets import redact
from crucible.domain.time import parse_rfc3339
from crucible.ports.execution import (
    HARNESSES_LABEL,
    IDENTITY_MOUNT,
    LEGACY_HARNESS_LABEL,
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
    CollectedOutputs,
    CredentialFileSync,
    CredentialSync,
    Handle,
    ImageInfo,
    IsolationLevel,
    LaunchCancelledError,
    LaunchRefusedError,
    LaunchSpec,
    LogChunk,
    LogOffset,
    Observation,
    ObservationState,
    ProbeRequest,
    ProbeResult,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
    VerificationRun,
    WorkerStartError,
    Workspace,
    WorkspaceState,
    image_harnesses,
)
from crucible.ports.github import InstallationToken
from crucible.ports.harness import (
    AuthFile,
    CredentialSource,
    CredentialSpec,
    ExitInfo,
    LaunchContext,
    MountMode,
)

log = logging.getLogger("crucible.provider.docker")

PROVIDER_NAME = "docker"
# What a throwaway container's exit code is when Crucible never got one: the API call
# failed, or the wait timed out and the container was killed. Distinct from any code a
# container can exit with, so a caller can tell "it failed" from "it never finished".
THROWAWAY_API_ERROR = -1
THROWAWAY_TIMED_OUT = -2
LABEL_ATTEMPT = "crucible.attempt"
LABEL_TASK = "crucible.task"
LABEL_OWNER = "crucible.owner"
LABEL_ROLE = "crucible.role"
# Which harness a login container signs in, so another process can see the login (12).
LABEL_HARNESS = "crucible.harness"
ROLE_WORKER = "worker"
ROLE_PREPARER = "preparer"
# hades #137: the reference cache's only writer, a container of its own that runs before
# the preparer, as the Kubernetes refresher Job does (26, #55).
ROLE_CACHE_REFRESHER = "cache-refresher"
ROLE_CLEANER = "cleaner"
ROLE_COLLECTOR = "collector"
ROLE_BUNDLE = "bundle-verifier"
ROLE_VERIFIER = "verifier"
ROLE_QUOTA_CHECKPOINT = "quota-checkpoint"
ROLE_LOGIN = "login"
# The per-attempt credential copy, as a workspace leaf and the template subdirectory of
# the identity bundle the read-only files on top of it come from (12).
CREDENTIAL_LEAF = "credential"
TEMPLATE_LEAF = "identity/harness"
WORKER_UID = 1000

# Crucible's launch wrapper (07, 12). The positional parameters are the harness argv.
# CRUCIBLE_ENV_FROM_FILES names variables to fill from files inside the credential copy:
# the one exception 07 allows to file-only delivery, resolved here inside the container
# and never placed in the create request. CRUCIBLE_STDIN_FILES and CRUCIBLE_PROMPT are
# what the harness reads on stdin; CRUCIBLE_TRANSCRIPT is where its stdout is teed so
# the stream becomes an artifact. With `pipefail`, the wrapper's exit is the harness's.
# CRUCIBLE_IN_FLIGHT_FILE names a harness state file listing its running commands, one
# `"session_id"` per command (Hermes's process registry, issue 152). While the harness
# runs, the wrapper counts them every 10 seconds and writes the count to stderr whenever
# it changes, so the supervisor can see a command in flight during the run. Only the
# count leaves the file. CRUCIBLE_EGRESS_ALLOWLIST (hades #425) lists the hosts the
# attempt's egress permits: before the harness starts, `egress_probe` tries each name on
# 443 the way the harness would (through HTTPS_PROXY when it is set, straight through the
# NetworkPolicy when it is not), for at most ten seconds per host, and writes one
# `crucible-egress-probe: {...}` line to stderr (crucible/domain/egress_probe.py), which
# the supervisor records on the attempt. A host it cannot reach is reported, never a
# reason not to start; a `host:port` entry is a local endpoint and is left alone.
LAUNCH_WRAPPER = r"""set -u
egress_probe() {
  list=${CRUCIBLE_EGRESS_ALLOWLIST:-}
  [ -n "$list" ] || return 0
  old_ifs=$IFS
  IFS=,
  set -- $list
  IFS=$old_ifs
  rows=
  for host do
    case "$host" in *:*) continue;; esac
    result=$(curl -sS -k -o /dev/null --connect-timeout 5 --max-time 10 \
      -w '\ncrucible_time=%{time_total}' "https://$host/" 2>&1)
    code=$?
    case "$result" in
      *crucible_time=*)
        seconds=${result##*crucible_time=}
        detail=${result%crucible_time=*}
        ;;
      *) seconds=0; detail=$result;;
    esac
    case "$code" in 0|35|52) reachable=true;; *) reachable=false;; esac
    # jq replaces malformed UTF-8 and --ascii-output keeps the pods/log marker ASCII.
    # Sequential checks need no temporary-file utilities beyond the guaranteed shell.
    row=$(jq -acn \
      --ascii-output \
      --arg host "$host" \
      --argjson reachable "$reachable" \
      --argjson curl_exit "$code" \
      --arg seconds "$seconds" \
      --arg detail "$detail" \
      '{host:$host,reachable:$reachable,curl_exit:$curl_exit,
        ms:(($seconds | tonumber) * 1000 | round),
        detail:($detail | gsub("[\\n\\r\\t]"; " ") | .[0:200])}') || continue
    rows="$rows${rows:+,}$row"
  done
  printf 'crucible-egress-probe: {"hosts":[%s]}\n' "$rows" >&2
}
egress_probe
if [ -n "${CRUCIBLE_CODEX_CONFIG:-}" ]; then
  mkdir -p "$CODEX_HOME" || exit 1
  printf '%s\n' "$CRUCIBLE_CODEX_CONFIG" > "$CODEX_HOME/config.toml" || exit 1
  unset CRUCIBLE_CODEX_CONFIG
fi
for pair in ${CRUCIBLE_ENV_FROM_FILES:-}; do
  var=${pair%%=*}; file=${pair#*=}
  if [ -r "$file" ]; then
    value=$(cat "$file")
    export "$var=$value"
    unset value
  fi
done
unset CRUCIBLE_ENV_FROM_FILES
feed() {
  for f in ${CRUCIBLE_STDIN_FILES:-}; do cat "$f"; printf '\n'; done
  if [ -n "${CRUCIBLE_PROMPT:-}" ]; then printf '%s\n' "$CRUCIBLE_PROMPT"; fi
}
watch_in_flight() {
  shown=0
  while :; do
    count=0
    if [ -f "$CRUCIBLE_IN_FLIGHT_FILE" ] && [ ! -L "$CRUCIBLE_IN_FLIGHT_FILE" ]; then
      count=$(grep -o '"session_id"' -- "$CRUCIBLE_IN_FLIGHT_FILE" 2>/dev/null | wc -l)
    fi
    count=$((count + 0))
    if [ "$count" != "$shown" ]; then
      printf 'crucible-launch: commands running: %s\n' "$count" >&2
      shown=$count
    fi
    sleep 10 >/dev/null 2>&1
  done
}
watcher=
if [ -n "${CRUCIBLE_IN_FLIGHT_FILE:-}" ]; then
  watch_in_flight </dev/null >/dev/null &
  watcher=$!
fi
# Keep the harness a direct child, including when stdin is fed or stdout is teed.
# A foreground pipeline defers Bash's TERM trap until the harness has exited.
child=
termination_requested=0
interrupted=0
forward_term() {
  termination_requested=1
  interrupted=1
  if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null || :; fi
}
trap forward_term TERM
wait_for() {
  while :; do
    interrupted=0
    wait "$1"
    result=$?
    # A trapped signal interrupts wait. Reap the child after its cleanup finishes.
    [ "$interrupted" -eq 0 ] && return "$result"
  done
}
feeder=
if [ -n "${CRUCIBLE_STDIN_FILES:-}${CRUCIBLE_PROMPT:-}" ]; then
  exec {input}< <(feed)
  feeder=$!
else
  exec {input}</dev/null
fi
transcriber=
if [ -n "${CRUCIBLE_TRANSCRIPT:-}" ]; then
  exec {output}> >(tee "$CRUCIBLE_TRANSCRIPT")
  transcriber=$!
else
  exec {output}>&1
fi
"$@" <&"$input" >&"$output" &
child=$!
exec {input}<&-
exec {output}>&-
if [ "$termination_requested" -eq 1 ]; then kill -TERM "$child" 2>/dev/null || :; fi
wait_for "$child"
status=$?
child=
# Preserve pipefail ordering and drain the transcript, including cleanup output.
if [ -n "$feeder" ]; then
  wait_for "$feeder"
  feed_status=$?
  [ "$status" -ne 0 ] || status=$feed_status
fi
if [ -n "$transcriber" ]; then
  wait_for "$transcriber"
  tee_status=$?
  [ "$tee_status" -eq 0 ] || status=$tee_status
fi
if [ -n "$watcher" ]; then kill "$watcher" 2>/dev/null; wait "$watcher" 2>/dev/null; fi
exit "$status"
"""

DEFAULT_IMAGE_ALLOWLIST: tuple[str, ...] = (
    "crucible-worker:*",
    "ghcr.io/sentania-labs/crucible-worker:*",
)


@dataclass(frozen=True, slots=True)
class DockerConfig:
    """Everything the provider needs that is not on the launch spec."""

    endpoint: str
    artifact_root: str
    # How the artifact root appears to the daemon. Under the rootless arrangement the
    # Crucible service runs as uid 1000 and the artifact root is a named volume it
    # shares with every container it creates (S9 Test E), so the daemon needs no host
    # path at all. `bind` is the developer-mode shape, where the root is a host
    # directory the daemon can see.
    mount_kind: Literal["volume", "bind"] = "volume"
    artifact_volume: str = "crucible-artifacts"
    artifact_host_root: str | None = None
    credential_root: str | None = None
    credential_host_root: str | None = None
    credential_volume: str = "crucible-credentials"
    workers_network: str = "crucible-workers"
    egress_proxy: str | None = None
    # What the egress proxy is actually configured to permit. An attempt whose
    # allowlist is not a subset of this is refused at launch rather than silently
    # running with less network than the policy promised (13).
    proxy_allowlist: tuple[str, ...] = ()
    no_proxy: str = "localhost,127.0.0.1"
    api_timeout_seconds: float = 30.0
    collector_timeout_seconds: int = 900
    verifier_timeout_seconds: int = 3600
    report_size_cap_bytes: int = 10 * 1024 * 1024
    log_tail_bytes: int = 64 * 1024
    workspace_dir_mode: int = 0o755
    use_reference_cache: bool = True
    max_concurrency: int = 3
    extra_image_allowlist: tuple[str, ...] = field(default=())
    # The configured credential directory per harness (12). A harness without one falls
    # back to `<credential_root>/<harness>` when that directory exists.
    credentials: Mapping[str, CredentialSource] = field(default_factory=dict)
    # The one host a private repository's checkout token is answered for (ADR 0019),
    # the same `github.credential_host` the publisher's helper uses (23).
    credential_host: str = "github.com"
    # The preparer's token tmpfs, which holds one installation token and nothing else.
    token_tmpfs_bytes: int = 64 * 1024


# The launch was refused by the adapter's version range or a missing credential (07,
# 13): the supervisor turns this into a wake rather than a plain environment failure.
HarnessRefusedError = LaunchRefusedError


@dataclass(frozen=True, slots=True)
class _CredentialCopy:
    """What was seeded for one attempt: the spec, the source, the effective mode, and
    the sha256 of each file as seeded (in memory only, never stored)."""

    spec: CredentialSpec
    source: CredentialSource
    mode: MountMode
    seeded: dict[str, str | None]


class CollectionFailedError(ProviderError):
    """The collector could not produce the outputs. The attempt is an `environment`
    failure and nothing is collected from it (16)."""


@dataclass(frozen=True, slots=True)
class _ResolvedImage:
    """What the daemon says about a reference: the immutable form, and its labels.

    Only this is cached. Whether an attempt may run it is a question about that
    attempt's policy, and it is asked again every launch."""

    reference: str
    labels: dict[str, str]


@dataclass(slots=True)
class _Launched:
    container_id: str
    image_digest: str
    spec: LaunchSpec
    credential: _CredentialCopy | None = None


def _mib(value: Any, default: int) -> int:
    """Parse `4GiB`, `512m`, or a plain byte count into bytes."""
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


class DockerProvider:
    """The provider of 08 against a rootless daemon behind the socket proxy (13, S9)."""

    name = PROVIDER_NAME
    # hades #189: how often a throwaway container's wait asks whether the task was
    # cancelled. Not a setting: it bounds how long a cancel waits, nothing else.
    cancel_poll_seconds = 2.0

    def __init__(
        self,
        config: DockerConfig,
        client: DockerClient | None = None,
        harnesses: HarnessRegistry | None = None,
    ) -> None:
        self.config = config
        self.client = client or DockerClient(config.endpoint, timeout=config.api_timeout_seconds)
        # `wire` always passes the deployment's registry; one built without it is a test's,
        # which may launch the script harness (crucible#124).
        self.harnesses = harnesses or default_registry(test_fixtures=True)
        self._launched: dict[str, _Launched] = {}
        self._images: dict[str, _ResolvedImage] = {}
        # The last failing output of each throwaway role, so an environment failure can
        # say what went wrong rather than only that something did.
        self.last_error: dict[str, str] = {}
        self._network_ready = False
        # Login sessions are process-local. Retention preserves only sessions driven
        # by this provider instance and reaps labels left by a crashed predecessor.
        self._active_login_ids: set[str] = set()

    # ----- helpers -----------------------------------------------------

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _root(self, attempt_id: str) -> Path:
        return Path(self.config.artifact_root) / "workspaces" / attempt_id

    def _daemon_mount(
        self, attempt_id: str, leaf: str, target: str, *, read_only: bool
    ) -> dict[str, Any]:
        """One mount of a workspace subdirectory, in whichever shape the daemon needs.

        An empty leaf is the workspace root itself, which only the preparer gets."""
        relative = f"workspaces/{attempt_id}/{leaf}".rstrip("/")
        if self.config.mount_kind == "volume":
            return {
                "Type": "volume",
                "Source": self.config.artifact_volume,
                "Target": target,
                "ReadOnly": read_only,
                "VolumeOptions": {"Subpath": relative, "NoCopy": True},
            }
        host_root = self.config.artifact_host_root or self.config.artifact_root
        return {
            "Type": "bind",
            "Source": f"{host_root.rstrip('/')}/{relative}",
            "Target": target,
            "ReadOnly": read_only,
            "BindOptions": {"Propagation": "rprivate"},
        }

    def _volume_mount(self, relative: str, target: str, *, read_only: bool) -> dict[str, Any]:
        """Mount a path under the artifact root that is not a workspace subdirectory."""
        if self.config.mount_kind == "volume":
            return {
                "Type": "volume",
                "Source": self.config.artifact_volume,
                "Target": target,
                "ReadOnly": read_only,
                "VolumeOptions": {"Subpath": relative, "NoCopy": True},
            }
        host_root = self.config.artifact_host_root or self.config.artifact_root
        return {
            "Type": "bind",
            "Source": f"{host_root.rstrip('/')}/{relative}",
            "Target": target,
            "ReadOnly": read_only,
            "BindOptions": {"Propagation": "rprivate"},
        }

    def _create_policy(self, spec: LaunchSpec, *, resolved: str = "") -> CreatePolicy:
        allowlist = self._image_allowlist(spec)
        if resolved:
            allowlist.append(resolved)
        host_root = self.config.artifact_host_root or self.config.artifact_root
        return CreatePolicy(
            image_allowlist=tuple(allowlist),
            artifact_root=host_root,
            credential_root=self.config.credential_host_root or self.config.credential_root,
            allowed_volumes=tuple(
                value
                for value in (self.config.artifact_volume, self.config.credential_volume)
                if value
            ),
        )

    def _labels(self, spec: LaunchSpec, role: str) -> dict[str, str]:
        return {
            LABEL_ATTEMPT: spec.attempt_id,
            LABEL_TASK: spec.task_id,
            LABEL_OWNER: spec.owner,
            LABEL_ROLE: role,
        }

    def _hardened(self, spec: LaunchSpec, *, network: str, tmpfs_mb: int = 512) -> dict[str, Any]:
        resources = spec.policy.get("resources", {})
        memory = _mib(resources.get("memory"), 4 * 1024**3)
        cpus = float(resources.get("cpus") or 2)
        pids = int(resources.get("pids") or 512)
        return {
            # S5: without --init the harness is PID 1 and a SIGTERM is silently
            # dropped, so drain would always end in the SIGKILL after the grace.
            "Init": True,
            "CapDrop": ["ALL"],
            "CapAdd": [],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "Privileged": False,
            "Tmpfs": {
                "/tmp": f"rw,nosuid,nodev,size={tmpfs_mb}m",
                "/home/worker": f"rw,nosuid,nodev,size={tmpfs_mb}m,uid=1000,gid=1000,mode=0750",
            },
            "Memory": memory,
            "MemorySwap": memory,
            "NanoCpus": int(cpus * 1_000_000_000),
            "PidsLimit": pids,
            "NetworkMode": network,
            "RestartPolicy": {"Name": ""},
            "AutoRemove": False,
        }

    async def _ensure_network(self) -> None:
        if self._network_ready or self.config.workers_network in ("none", ""):
            return
        try:
            await self._call(self.client.inspect_network, self.config.workers_network)
        except DockerApiError as exc:
            if exc.status != 404:
                raise
            # internal: no default route, no reach to the control plane (13).
            await self._call(self.client.create_network, self.config.workers_network, internal=True)
        self._network_ready = True

    # ----- contract ----------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            isolation=IsolationLevel.CONTAINER,
            network_control=True,
            resource_limits=True,
            shared_disk=True,
            supports_harnesses=frozenset(self.harnesses.names()),
            max_concurrency=self.config.max_concurrency,
        )

    async def credential_available(self, harness: str) -> bool:
        source = self._credential_source(harness)
        adapter = self.harnesses.get(harness)
        credential = adapter.credential_spec() if adapter is not None else None
        return source is not None and (credential is None or credential.held_by(source.path))

    async def probe_checks(
        self,
        spec: LaunchSpec,
        checks: Sequence[dict[str, Any]],
        checkout_token: InstallationToken | None = None,
        cancelled: CancelCheck | None = None,
    ) -> tuple[VerificationRun, ...] | None:
        """Docker does not yet provide an isolated pre-preparation probe."""
        return None

    async def gate_probe_exists(self, attempt_id: str) -> bool:
        return False

    async def prepare(
        self,
        spec: LaunchSpec,
        checkout_token: InstallationToken | None = None,
        cancelled: CancelCheck | None = None,
    ) -> Workspace:
        # hades #189: a cancel is honoured before each container starts and on every poll
        # while one runs: the cache refresher first (hades #137), then the preparer.
        if cancelled is not None and await cancelled():
            raise LaunchCancelledError("the task was cancelled before the preparer started")
        repository = spec.contract.get("repository", {})
        url = spec.repository_url or str(repository.get("url", ""))
        if not url:
            raise ProviderError("the contract names no repository url")
        if checkout_token is not None:
            workspace.require_checkout_url(url, self.config.credential_host)
        base_ref = str(repository.get("base_ref", "main"))
        work_branch = str(repository.get("work_branch") or f"crucible/{spec.external_id}")
        root = self._root(spec.attempt_id)
        await asyncio.to_thread(shutil.rmtree, root, True)
        paths = await asyncio.to_thread(self._make_dirs, root)
        resolved = await self._resolve_image(spec)
        adapter = self.harnesses.require(spec.harness)

        local = self._local_origin(url)
        # The preparer gets the workspace itself, so git creates the checkout directory
        # and the container's uid owns it end to end (S9 Test E).
        mounts = [self._daemon_mount(spec.attempt_id, "", WORK_MOUNT, read_only=False)]
        resume_bundle = None
        if spec.resume_bundle_path:
            resume_bundle = f"{scripts.BUNDLE_MOUNT}/work_branch.bundle"
            source = Path(spec.resume_bundle_path)
            artifact_root = Path(self.config.artifact_root).resolve()
            try:
                relative = str(source.resolve().relative_to(artifact_root))
            except ValueError as exc:
                raise ProviderError(
                    "the previous attempt bundle is outside artifact storage"
                ) from exc
            mounts.append(self._volume_mount(relative, resume_bundle, read_only=True))
        network = "none"
        env: dict[str, str] = {}
        cache_name: str | None = None
        clone_url = url
        # ADR 0019: a private repository's token goes to the two containers that talk to
        # the remote, the cache refresher and the preparer, on stdin and onto a tmpfs of
        # each one's own, as the publisher's does; the worker is a different container
        # that never has either.
        private = checkout_token is not None and local is None
        if local is not None:
            # A repository that already lives in the artifact root (the e2e origin) is
            # mounted read-only; nothing has to leave the daemon for it.
            mounts.append(self._volume_mount(local, scripts.ORIGIN_MOUNT, read_only=True))
            clone_url = scripts.ORIGIN_MOUNT
        else:
            network, env = self._network_and_env(spec)
            if self.config.use_reference_cache:
                cache_name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
                await asyncio.to_thread(self._ensure_cache_dir)
                # hades #137: the refresh runs in a container of its own, the cache's one
                # writer, before the preparer. The cache is the one volume every attempt
                # shares, so a preparer that could write it could poison every later
                # checkout (#55); the preparer mounts it read-only and only reads it.
                await self._refresh_cache(
                    spec,
                    url=url,
                    cache_name=cache_name,
                    image=resolved,
                    network=network,
                    env=env,
                    checkout_token=checkout_token if private else None,
                    cancelled=cancelled,
                )
                mounts.append(self._volume_mount("cache", scripts.CACHE_MOUNT, read_only=True))

        git_policy = spec.policy.get("git", {})
        exit_code = await self._run_throwaway(
            spec,
            role=ROLE_PREPARER,
            image=resolved,
            script=scripts.preparer_script(
                url=clone_url,
                base_ref=base_ref,
                work_branch=work_branch,
                from_remote_branch=spec.role == "correct"
                or bool(repository.get("resume_from_work_branch")),
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
                checkout_token="stdin" if private else None,
                credential_host=self.config.credential_host,
            ),
            mounts=mounts,
            network=network,
            timeout=self.config.collector_timeout_seconds,
            env=env,
            secret_stdin=checkout_token if private else None,
            cancelled=cancelled,
        )
        if exit_code != 0:
            raise ProviderError(
                f"the preparer container could not build the checkout (exit {exit_code}): "
                f"{redact(self.last_error.get(ROLE_PREPARER, ''))}"
            )
        try:
            return await asyncio.to_thread(self._finish_prepare, spec, paths, work_branch)
        except workspace.WorkspaceError as exc:
            raise ProviderError(str(exc)) from exc

    async def _refresh_cache(
        self,
        spec: LaunchSpec,
        *,
        url: str,
        cache_name: str,
        image: str,
        network: str,
        env: Mapping[str, str],
        checkout_token: InstallationToken | None,
        cancelled: CancelCheck | None,
    ) -> None:
        """hades #137: refresh the reference cache in a short-lived container of its own,
        the only container that mounts it writable, before the preparer runs; the Docker
        shape of the Kubernetes refresher Job (26, #55). It carries no workspace, no
        identity bundle and no harness credential: only the cache, the preparer's network
        and, for a private repository, the checkout token on stdin (ADR 0019).

        A refresh that fails is logged and the preparer goes on, cloning from the mirror
        as it was or from the remote: a stale or absent cache costs time, never
        correctness. A cancel is honoured before the refresher starts and on every poll
        while it runs, as the preparer's is (hades #189)."""
        code = await self._run_throwaway(
            spec,
            role=ROLE_CACHE_REFRESHER,
            image=image,
            script=scripts.cache_refresh_script(
                url=url,
                cache_name=cache_name,
                checkout_token="stdin" if checkout_token is not None else None,
                credential_host=self.config.credential_host,
            ),
            mounts=[self._volume_mount("cache", scripts.CACHE_MOUNT, read_only=False)],
            network=network,
            timeout=self.config.collector_timeout_seconds,
            env=env,
            secret_stdin=checkout_token,
            cancelled=cancelled,
        )
        if code != 0:
            log.warning(
                "the reference cache refresh failed; the preparer clones without it",
                extra={
                    "exit_code": code,
                    "detail": redact(self.last_error.get(ROLE_CACHE_REFRESHER, "")),
                },
            )

    def _ensure_cache_dir(self) -> None:
        """The shared cache directory the refresher writes and every preparer reads. It
        has to exist before either container is created, because the daemon mounts a
        path under the artifact root and will not create one."""
        cache = Path(self.config.artifact_root) / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        cache.chmod(self.config.workspace_dir_mode)

    def _make_dirs(self, root: Path) -> dict[str, Path]:
        """Everything but the checkout: the preparer's git creates that one."""
        paths = {
            "root": root,
            "identity": root / "identity",
            "report": root / "report",
            "output": root / "output",
            "verify": root / "verify",
        }
        for name, path in paths.items():
            path.mkdir(parents=True, exist_ok=True)
            if name != "identity":
                path.chmod(self.config.workspace_dir_mode)
        paths["repo"] = root / "repo"
        return paths

    def _local_origin(self, url: str) -> str | None:
        """The path inside the artifact root a local repository url names, if any."""
        candidate = url[len("file://") :] if url.startswith("file://") else url
        if not candidate.startswith("/"):
            return None
        root = Path(self.config.artifact_root).resolve()
        try:
            return str(Path(candidate).resolve().relative_to(root))
        except ValueError:
            return None

    def _finish_prepare(
        self, spec: LaunchSpec, paths: dict[str, Path], work_branch: str
    ) -> Workspace:
        output = paths["output"]
        head = (output / "prepared-head.txt").read_text(encoding="utf-8").strip()
        started_from = (output / "started-from.txt").read_text(encoding="utf-8").strip()
        if not head:
            raise workspace.WorkspaceError("the preparer produced no HEAD")
        # 12: the Crucible-owned templates that go read-only on top of the credential
        # copy. They live inside the identity bundle so the bundle hash covers them.
        adapter = self.harnesses.get(
            "hermes" if spec.harness == "codex" and spec.endpoint == "local" else spec.harness
        )
        credential = adapter.credential_spec() if adapter is not None else None
        if credential is not None and credential.templates:
            template_dir = paths["identity"] / "harness"
            template_dir.mkdir(parents=True, exist_ok=True)
            for name, content in credential.templates.items():
                target = template_dir / name
                target.write_text(content, encoding="utf-8")
                os.chmod(target, 0o444)
        _, identity_sha = identity_bundle.write_bundle(
            paths["identity"],
            contract=spec.contract,
            policy=spec.policy,
            external_id=spec.external_id,
            owner=spec.owner,
            work_branch=work_branch,
            network_mode=spec.network,
            report_schema=CompletionClaimV1.model_json_schema(),
        )
        # The preparer's own output files are not evidence; the collector rewrites the
        # directory after the run and a stale head would only confuse a reader.
        for leftover in ("prepared-head.txt", "started-from.txt"):
            (output / leftover).unlink(missing_ok=True)
        return Workspace(
            attempt_id=spec.attempt_id,
            checkout_path=str(paths["repo"]),
            identity_path=str(paths["identity"]),
            report_path=str(paths["report"]),
            output_path=str(output),
            identity_sha256=identity_sha,
            work_branch=work_branch,
            started_from=started_from,
        )

    async def _resolve_image(self, spec: LaunchSpec) -> str:
        """Resolve the tag to something immutable, refusing anything the policy or the
        adapter's tested range does not allow (07, 13).

        The cache holds only the reference-to-digest mapping, which is a fact about the
        daemon. The allowlist and the harness-version check are facts about *this*
        attempt's policy and execution, so they run on every launch: the same image
        under a different policy has to be refused, and a cache hit must not be a way
        past that."""
        cached = self._images.get(spec.image)
        if cached is None:
            try:
                image = await self._call(self.client.inspect_image, spec.image)
            except DockerApiError as exc:
                raise ProviderError(
                    f"image {spec.image!r} is not available: {exc.message}"
                ) from exc
            labels = {
                str(k): str(v) for k, v in (image.get("Config", {}).get("Labels") or {}).items()
            }
            digests = [str(d) for d in (image.get("RepoDigests") or [])]
            cached = _ResolvedImage(
                reference=digests[0] if digests else str(image.get("Id", "")), labels=labels
            )
            self._images[spec.image] = cached
        self._check_image_allowed(spec, cached)
        return cached.reference

    def _check_image_allowed(self, spec: LaunchSpec, image: _ResolvedImage) -> None:
        """Every launch, cache hit or not (07, 13)."""
        if not image_allowed(spec.image, self._image_allowlist(spec)):
            raise ProviderError(f"image {spec.image!r} is outside the policy allowlist")
        check = check_image_version(self.harnesses, spec.harness, image.labels)
        if not check.ok:
            raise HarnessRefusedError(f"refusing to launch: {check.detail}")

    def _image_allowlist(self, spec: LaunchSpec) -> list[str]:
        return [
            str(p)
            for p in (spec.policy.get("images", {}).get("allowlist") or DEFAULT_IMAGE_ALLOWLIST)
        ] + list(self.config.extra_image_allowlist)

    async def launch(
        self, ws: Workspace, spec: LaunchSpec, cancelled: CancelCheck | None = None
    ) -> Handle:
        await self._ensure_network()
        resolved = await self._resolve_image(spec)
        network, env = self._network_and_env(spec)
        body = self._worker_body(ws, spec, resolved=resolved, network=network, env=env)
        try:
            check_create(body, self._create_policy(spec, resolved=resolved))
        except CreateRequestRefusedError as exc:
            raise ProviderError(f"create-request policy refused the worker: {exc}") from exc
        # hades #189: the network and the image resolution above can take a while; a
        # cancel that landed during them creates nothing.
        if cancelled is not None and await cancelled():
            raise LaunchCancelledError("the task was cancelled before the worker was created")
        name = f"crucible-{spec.attempt_id}"
        copy = self._credential_copy(spec)
        container_id = ""
        seeded_on_disk = False
        starting = False
        try:
            container_id = await self._call(self.client.create_container, name, body)
            if copy is not None:
                # 12: the copy is seeded through the daemon into the created, not yet
                # started, container. The files land in the workspace directory the
                # preparer made, owned by the worker's uid, mode 600.
                if copy.mode is MountMode.RENEWER:
                    tar, seeded = await asyncio.to_thread(_seed_renewer_tar, copy)
                else:
                    tar, seeded = await asyncio.to_thread(_seed_tar, copy.spec, copy.source)
                copy = replace(copy, seeded=seeded)
                await self._call(self.client.put_archive, container_id, copy.spec.mount_target, tar)
                seeded_on_disk = True
            starting = True
            await self._call(self.client.start_container, container_id)
        except (DockerApiError, ProviderError) as exc:
            failure = (
                await self._start_failure(container_id, exc)
                if starting and isinstance(exc, DockerApiError)
                else None
            )
            if container_id:
                with contextlib.suppress(Exception):
                    await self._call(self.client.remove_container, container_id, force=True)
            if seeded_on_disk:
                # The files are in the workspace leaf whether or not the worker started,
                # and nothing later would visit an attempt that never ran (12).
                with contextlib.suppress(Exception):
                    await self._remove_through_daemon(ws, spec, [CREDENTIAL_LEAF])
            if failure is not None:
                raise WorkerStartError(failure) from exc
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"could not start the worker: {exc}") from exc
        self._launched[spec.attempt_id] = _Launched(container_id, resolved, spec, copy)
        return Handle(
            provider=self.name,
            ref=container_id,
            attempt_id=spec.attempt_id,
            image_digest=resolved,
            name=name,
        )

    async def _start_failure(self, container_id: str, exc: DockerApiError) -> Observation | None:
        """Hades #346: the daemon refused `/start` for the created container. Its answer
        is the runtime's reason (OCI runtime create failed: ...), and the container's
        state, read before it is removed, has the exit code the runtime recorded. A
        daemon that could not be reached is not a start failure; nor is a container
        that vanished (404)."""
        if exc.status == 404 or exc.status < 400:
            return None
        message = exc.message or str(exc)
        exit_code = 128
        with contextlib.suppress(Exception):
            state = (await self._call(self.client.inspect_container, container_id)).get("State", {})
            message = str(state.get("Error") or message)
            exit_code = int(state.get("ExitCode") or exit_code)
        return Observation(
            ObservationState.EXITED,
            exit_code=exit_code,
            detail="StartError",
            container_message=message[:2000],
            never_started=True,
        )

    def _network_and_env(self, spec: LaunchSpec) -> tuple[str, dict[str, str]]:
        env: dict[str, str] = {
            "CRUCIBLE_ATTEMPT_ID": spec.attempt_id,
            "CRUCIBLE_TASK_EXTERNAL_ID": spec.external_id,
            "CRUCIBLE_IDENTITY_DIR": IDENTITY_MOUNT,
            "CRUCIBLE_REPORT_DIR": REPORT_MOUNT,
            "CRUCIBLE_REPO_DIR": REPO_MOUNT,
            "HOME": "/home/worker",
            **PACKAGE_CACHE_ENV,
            **spec.env,
        }
        wanted = self._egress_wanted(spec)
        if wanted is None:
            return "none", env
        configured = set(self.config.proxy_allowlist)
        if configured and not set(wanted) <= configured:
            missing = sorted(set(wanted) - configured)
            raise ProviderError(
                "the egress proxy does not permit every host this attempt needs: "
                f"{missing}. Bring the proxy up with the policy's allowlist."
            )
        if self.config.egress_proxy:
            env.update(
                {
                    "HTTPS_PROXY": self.config.egress_proxy,
                    "HTTP_PROXY": self.config.egress_proxy,
                    "https_proxy": self.config.egress_proxy,
                    "http_proxy": self.config.egress_proxy,
                    "NO_PROXY": self.config.no_proxy,
                    "no_proxy": self.config.no_proxy,
                }
            )
        env["CRUCIBLE_EGRESS_ALLOWLIST"] = ",".join(wanted)
        return self.config.workers_network, env

    def _worker_body(
        self,
        ws: Workspace,
        spec: LaunchSpec,
        *,
        resolved: str,
        network: str,
        env: Mapping[str, str],
    ) -> dict[str, Any]:
        host_config = self._hardened(spec, network=network)
        host_config["Mounts"] = [
            self._daemon_mount(spec.attempt_id, "repo", REPO_MOUNT, read_only=False),
            self._daemon_mount(spec.attempt_id, "identity", IDENTITY_MOUNT, read_only=True),
            self._daemon_mount(spec.attempt_id, "report", REPORT_MOUNT, read_only=False),
            self._daemon_mount(
                spec.attempt_id, PACKAGE_CACHE_LEAF, PACKAGE_CACHE_MOUNT, read_only=False
            ),
            *self._credential_mounts(spec),
        ]
        command, launch_env = self._command(spec)
        return {
            "Image": resolved,
            "Cmd": command,
            "User": "1000:1000",
            "WorkingDir": REPO_MOUNT,
            "Env": [f"{k}={v}" for k, v in sorted({**env, **launch_env}.items())],
            "Labels": self._labels(spec, ROLE_WORKER),
            "Tty": False,
            "OpenStdin": False,
            "AttachStdin": False,
            "NetworkDisabled": False,
            "HostConfig": host_config,
        }

    def _egress_wanted(self, spec: LaunchSpec) -> tuple[str, ...] | None:
        """Every destination this attempt's egress permits (13, S6): the policy's
        `egress_allowlist`, the contract's `egress_extra`, the adapter's declared
        endpoints and a local route's `host:port`. None when the attempt has no network
        at all."""
        network_policy = str(spec.policy.get("network", {}).get("mode", "egress-proxy"))
        if spec.network == "none" or network_policy == "none":
            return None
        return tuple(
            egress_allowlist(
                self.harnesses,
                spec.harness,
                [str(h) for h in (spec.policy.get("network", {}).get("egress_allowlist") or [])],
                [str(h) for h in (spec.contract.get("constraints", {}).get("egress_extra") or [])],
                spec.endpoint_url,
            )
        )

    def _probe_hosts(self, spec: LaunchSpec) -> tuple[str, ...]:
        """hades #425: the allowlisted names the launch wrapper probes before the harness
        starts. `host:port` entries are local endpoints, which the probe leaves alone."""
        return tuple(h for h in (self._egress_wanted(spec) or ()) if ":" not in h)

    def _command(self, spec: LaunchSpec) -> tuple[list[str], dict[str, str]]:
        """The harness argv, wrapped only when the launch needs stdin, a transcript, a
        variable filled from a credential file (07), or the egress probe (hades #425: an
        attempt with allowlisted hosts). A plain argv with no network stays plain."""
        argv = list(spec.command)
        if not argv:
            adapter = self.harnesses.get(spec.harness)
            if adapter is not None:
                argv = list(adapter.build_launch(self._launch_context(spec)).argv)
        wrapped = bool(spec.env_from_files or spec.stdin_files or spec.stdin_text)
        wrapped = wrapped or bool(spec.transcript_path) or bool(self._probe_hosts(spec))
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

    def _launch_context(self, spec: LaunchSpec) -> LaunchContext:
        copy = self._credential_copy(spec)
        return LaunchContext(
            attempt_id=spec.attempt_id,
            model=spec.model,
            effort=spec.effort,
            timeout_seconds=spec.timeout_seconds,
            identity_mount=IDENTITY_MOUNT,
            report_mount=REPORT_MOUNT,
            repo_mount=REPO_MOUNT,
            credential_mounted=copy is not None,
            credential_mode=copy.mode if copy is not None else None,
            endpoint=spec.endpoint,
            endpoint_url=spec.endpoint_url,
            command_timeout_ms=spec.command_timeout_ms,
            harness_settings=spec.harness_settings,
        )

    def _credential_source(self, harness: str) -> CredentialSource | None:
        configured = self.config.credentials.get(harness)
        if configured is not None and configured.path:
            return configured
        root = self.config.credential_root
        if root and (Path(root) / harness).is_dir():
            return CredentialSource(path=str(Path(root) / harness))
        return None

    def _credential_copy(self, spec: LaunchSpec) -> _CredentialCopy | None:
        """The credential this attempt's harness needs and where it comes from (12).

        A harness that declares a credential and has no configured source is refused
        at launch: it would only fail authentication after spending an attempt."""
        adapter = self.harnesses.get(
            "hermes" if spec.harness == "codex" and spec.endpoint == "local" else spec.harness
        )
        credential = adapter.credential_spec() if adapter is not None else None
        if credential is None:
            return None
        source = self._credential_source(credential.harness)
        if (
            credential.harness == "codex"
            and source is not None
            and (Path(source.path) / "auth.json.dead").exists()
        ):
            raise HarnessRefusedError(
                "refusing to launch: the Codex credential is dead; log in again"
            )
        if source is not None and not credential.held_by(source.path):
            source = None
        if source is None:
            if not credential.required_for_launch:
                return None
            raise HarnessRefusedError(
                f"refusing to launch: no credential directory is configured for "
                f"harness {spec.harness!r} (credentials.{spec.harness}.path)"
            )
        mode = (
            MountMode.RO
            if spec.harness == "codex" and spec.endpoint == "local"
            else MountMode(spec.credential_mode)
            if spec.credential_mode is not None
            else effective_mount_mode(credential, source)
        )
        return _CredentialCopy(
            spec=(worker_credential_spec(credential) if mode is MountMode.RENEWER else credential),
            source=source,
            mode=mode,
            seeded={},
        )

    def _credential_mounts(self, spec: LaunchSpec) -> list[dict[str, Any]]:
        """The per-attempt copy for this attempt's harness and no other (12), plus the
        Crucible-owned templates read-only on top of it. The script harness has none."""
        copy = self._credential_copy(spec)
        if copy is None:
            return []
        target = copy.spec.mount_target
        mounts = [
            self._daemon_mount(
                spec.attempt_id,
                CREDENTIAL_LEAF,
                target,
                read_only=copy.mode is not MountMode.RW_NARROW,
            )
        ]
        for name in sorted(copy.spec.templates):
            mounts.append(
                self._daemon_mount(
                    spec.attempt_id, f"{TEMPLATE_LEAF}/{name}", f"{target}/{name}", read_only=True
                )
            )
        return mounts

    async def refresh_credential_projection(self, document: Mapping[str, str]) -> None:
        """Atomically replace the access-only file of every live Codex worker."""
        for attempt_id, launched in tuple(self._launched.items()):
            copy = launched.credential
            if copy is None or copy.mode is not MountMode.RENEWER:
                continue
            target = self._root(attempt_id) / CREDENTIAL_LEAF / "access-token.json"
            await asyncio.to_thread(atomic_write, target, document)

    async def _sync_credential(
        self, h: Handle, ws: Workspace, spec: LaunchSpec
    ) -> CredentialSync | None:
        """Read the named auth files back out of the stopped worker, write back only a
        valid, newer one, and remove the copy at once (12).

        `changed` is against the source as it is now, which also covers a collect after
        a supervisor restart, when the seeded hashes are gone with the process. The
        removal is in a `finally` path: a daemon that keeps failing the read-back must
        not keep the files on disk for as long as it fails."""
        launched = self._launched.get(h.attempt_id)
        copy = launched.credential if launched is not None else self._credential_copy(spec)
        if copy is None:
            return None
        files: list[CredentialFileSync] = []
        try:
            if copy.mode is MountMode.RENEWER:
                files = [
                    CredentialFileSync(
                        auth.name, True, False, True, False, "renewer-held; nothing to sync"
                    )
                    for auth in copy.spec.auth_files
                ]
            else:
                for auth in copy.spec.auth_files:
                    files.append(await self._sync_file(h, copy, auth))
        finally:
            removed = await self._remove_credential_copy(h, ws, spec)
        return CredentialSync(
            harness=copy.spec.harness,
            mount_mode=copy.mode.value,
            files=tuple(files),
            removed=removed,
            detail=(
                "" if copy.seeded else "seeded hashes unknown; changed is against the source now"
            ),
        )

    async def _remove_credential_copy(self, h: Handle, ws: Workspace, spec: LaunchSpec) -> bool:
        try:
            await self._remove_through_daemon(ws, spec, [CREDENTIAL_LEAF])
            return not (self._root(h.attempt_id) / CREDENTIAL_LEAF).exists()
        except Exception as exc:  # the sync is recorded whatever the removal did
            log.warning("credential copy removal failed", extra={"error": str(exc)})
            return False

    async def _sync_file(
        self, h: Handle, copy: _CredentialCopy, auth: AuthFile
    ) -> CredentialFileSync:
        """One named file: read back, checked, and written back or not (12). A failure
        to read, on the API or on the transport, is a recorded outcome, never an
        exception past the removal."""
        inside = f"{copy.spec.mount_target}/{auth.name}"
        try:
            archive = await self._call(self.client.get_archive, h.ref, inside)
        except (DockerApiError, TimeoutError, OSError, HTTPException) as exc:
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            return CredentialFileSync(
                auth.name, False, False, False, False, f"read failed: {detail}"
            )
        if archive is not None and archive.truncated:
            # A worker owns its copy and left something larger than any auth file at
            # this path. Nothing that size is parsed, let alone written back.
            return CredentialFileSync(
                auth.name, True, True, False, False, "changed; larger than the read limit"
            )
        if archive is not None and archive.stat and not archive.is_regular:
            # The daemon says the path is not a regular file. A symlink here is a worker
            # substituting some other file's bytes for its credential; the bytes are not
            # looked at and the source is left alone.
            return CredentialFileSync(
                auth.name, True, True, False, False, "changed; not a regular file"
            )
        data = _single_file(archive.tar) if archive is not None else None
        if data is None:
            return CredentialFileSync(auth.name, False, False, False, False, "absent after the run")
        return await asyncio.to_thread(_sync_one, copy, auth, data)

    async def _worker_tails(self, container_id: str) -> tuple[str, str]:
        """The last bytes of the worker's own stdout and stderr, for classification (S5)."""
        try:
            frames = await self._call(self.client.container_logs, container_id)
        except (DockerApiError, TimeoutError, OSError, HTTPException):
            return "", ""
        return _tails(frames, self.config.log_tail_bytes)

    async def observe(self, h: Handle) -> Observation:
        try:
            data = await self._call(self.client.inspect_container, h.ref)
        except DockerApiError as exc:
            if exc.status == 404:
                return Observation(ObservationState.LOST, detail="the daemon has no such container")
            raise ProviderError(f"inspect failed: {exc}") from exc
        state = data.get("State", {})
        status = str(state.get("Status", ""))
        message = str(state.get("Error") or "")
        started = str(state.get("StartedAt") or "")
        never_started = bool(message) and (not started or started.startswith("0001-01-01"))
        if never_started:
            return Observation(
                ObservationState.EXITED,
                exit_code=int(state.get("ExitCode", 128)),
                detail="StartError",
                container_message=message,
                never_started=True,
            )
        if status in ("created", "running", "restarting", "paused", "removing"):
            return Observation(ObservationState.RUNNING, detail=status)
        detail = status
        oom = bool(state.get("OOMKilled"))
        if oom:
            # S5: 137 with OOMKilled is an environment failure, not a kill Crucible sent.
            detail = f"{status}:oom_killed"
        return Observation(
            ObservationState.EXITED,
            exit_code=int(state.get("ExitCode", -1)),
            detail=detail,
            oom_killed=oom,
        )

    async def logs(self, h: Handle, since: LogOffset) -> list[LogChunk]:
        try:
            frames = await self._call(
                self.client.container_logs, h.ref, since=_since_param(since.timestamp)
            )
        except DockerApiError as exc:
            if exc.status == 404:
                return []
            raise ProviderError(f"log pull failed: {exc}") from exc
        return _chunks(frames, since)

    async def probe_model_endpoint(self, endpoint_url: str) -> bool:
        return await probe_model_endpoint(endpoint_url)

    async def collect(
        self, h: Handle, ws: Workspace, spec: LaunchSpec | None = None
    ) -> CollectedOutputs:
        launched = self._launched.get(h.attempt_id)
        spec = spec or (launched.spec if launched else None)
        if spec is None:
            raise ProviderError("collect needs the launch spec and the provider has none")
        root = self._root(h.attempt_id)
        output = root / "output"
        output.mkdir(parents=True, exist_ok=True)
        repository = spec.contract.get("repository", {})
        work_branch = ws.work_branch or str(
            repository.get("work_branch") or f"crucible/{spec.external_id}"
        )
        # 12: the credential copy is read back and removed before anything else runs.
        credential_sync = await self._sync_credential(h, ws, spec)
        stdout_tail, stderr_tail = await self._worker_tails(h.ref)
        observation = (
            await self.observe(h)
            if hasattr(self.client, "inspect_container")
            else Observation(ObservationState.EXITED, exit_code=1)
        )
        if observation.never_started:
            return CollectedOutputs(
                report=None,
                report_raw=None,
                blocked_md=None,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                credential_sync=credential_sync,
            )
        adapter = self.harnesses.get(spec.harness)
        quota_checkpoint = bool(
            adapter is not None
            and observation.state is ObservationState.EXITED
            and adapter.classify_exit(
                ExitInfo(exit_code=observation.exit_code),
                stdout_tail,
                stderr_tail,
                root / "report",
            )
            is ExitClass.QUOTA_EXHAUSTED
        )
        mounts = [
            # FDY-0140: writable always, since what the worker left uncommitted is
            # committed before anything is collected.
            self._daemon_mount(spec.attempt_id, "repo", REPO_MOUNT, read_only=False),
            self._daemon_mount(spec.attempt_id, "report", REPORT_MOUNT, read_only=True),
            self._daemon_mount(spec.attempt_id, "output", OUTPUT_MOUNT, read_only=False),
        ]
        collector_exit = await self._run_throwaway(
            spec,
            role=ROLE_COLLECTOR,
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
            mounts=mounts,
            network="none",
            timeout=self.config.collector_timeout_seconds,
        )
        if collector_exit == THROWAWAY_TIMED_OUT:
            # 16: a provider that failed before or while producing the outputs is an
            # environment failure on the attempt. Raising here is what makes the
            # supervisor record one instead of collecting empty outputs every tick.
            raise CollectionFailedError(
                self.last_error.get(ROLE_COLLECTOR)
                or f"the collector did not finish within {self.config.collector_timeout_seconds}s"
            )
        if collector_exit == THROWAWAY_API_ERROR:
            raise CollectionFailedError(
                self.last_error.get(ROLE_COLLECTOR) or "the collector container could not be run"
            )
        bundle_ok = False
        if (
            collector_exit == 0
            and (output / "work_branch.bundle").exists()
            and (output / "tree").is_dir()
        ):
            bundle_ok = (
                await self._run_throwaway(
                    spec,
                    role=ROLE_BUNDLE,
                    script=scripts.BUNDLE_VERIFY_SCRIPT,
                    mounts=[
                        self._daemon_mount(spec.attempt_id, "output", OUTPUT_MOUNT, read_only=True)
                    ],
                    network="none",
                    timeout=120,
                )
                == 0
            )
        report = root / "report"
        interruption = (
            adapter.interruption(
                ExitInfo(
                    exit_code=observation.exit_code,
                    report_present=(report / "report.yaml").is_file(),
                    blocked_present=(report / "blocked.md").is_file(),
                    oom_killed=observation.oom_killed,
                ),
                stdout_tail,
                stderr_tail,
                report,
            )
            if adapter is not None and observation.state is ObservationState.EXITED
            else None
        )
        interrupted = observation.never_started or quota_checkpoint or interruption is not None
        verifications = () if interrupted else await self._run_verifier(spec)
        outputs = _read_outputs(
            output,
            root / "verify",
            spec=spec,
            bundle_verified=bundle_ok,
            collector_exit=collector_exit,
            verifications=verifications,
            tail_bytes=self.config.log_tail_bytes,
        )
        state = await self._workspace_state(spec.attempt_id, keep=h.ref)
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
            artifacts=outputs.artifacts,
            verifications=outputs.verifications,
            workspace_state=state,
            copy_rejections=outputs.copy_rejections,
            credential_sync=credential_sync,
            checkpoint_refusal=outputs.checkpoint_refusal,
            leftover_committed=outputs.leftover_committed,
            leftover_note=outputs.leftover_note,
        )

    async def push_quota_checkpoint(
        self, ws: Workspace, spec: LaunchSpec
    ) -> tuple[bool, str] | None:
        """Push a local-origin checkpoint only after the supervisor approves it."""
        local_origin = self._local_origin(spec.repository_url)
        if local_origin is None:
            return None
        repository = spec.contract.get("repository", {})
        work_branch = ws.work_branch or str(
            repository.get("work_branch") or f"crucible/{spec.external_id}"
        )
        code = await self._run_throwaway(
            spec,
            role=ROLE_QUOTA_CHECKPOINT,
            script=scripts.quota_checkpoint_push_script(work_branch),
            mounts=[
                self._daemon_mount(spec.attempt_id, "repo", REPO_MOUNT, read_only=False),
                self._volume_mount(local_origin, scripts.ORIGIN_MOUNT, read_only=False),
            ],
            network="none",
            timeout=self.config.collector_timeout_seconds,
        )
        if code == 0:
            return True, "checkpoint pushed to local origin"
        return False, self.last_error.get(ROLE_QUOTA_CHECKPOINT) or (
            f"quota checkpoint container exited {code}"
        )

    async def _run_verifier(self, spec: LaunchSpec) -> tuple[VerificationRun, ...]:
        checks = [
            (str(v.get("id")), str(v.get("command")))
            for v in spec.contract.get("required_verification", [])
            if str(v.get("kind", "command")) == "command"
        ]
        if not checks:
            return ()
        tree = self._root(spec.attempt_id) / "output" / "tree"
        if not tree.exists():
            return tuple(
                VerificationRun(
                    id=check_id,
                    command=command,
                    expect_exit=0,
                    exit_code=-1,
                    log_tail="",
                    ran=False,
                    detail="the collector produced no tree to verify from",
                )
                for check_id, command in checks
            )
        network = "none" if spec.network == "none" else self.config.workers_network
        _, env = (
            self._network_and_env(spec) if network != "none" else ("none", dict(PACKAGE_CACHE_ENV))
        )
        code = await self._run_throwaway(
            spec,
            role=ROLE_VERIFIER,
            script=scripts.verifier_script(checks),
            mounts=[
                self._daemon_mount(spec.attempt_id, "output/tree", REPO_MOUNT, read_only=False),
                self._daemon_mount(spec.attempt_id, "verify", VERIFY_MOUNT, read_only=False),
                self._daemon_mount(
                    spec.attempt_id, VERIFIER_CACHE_LEAF, PACKAGE_CACHE_MOUNT, read_only=False
                ),
            ],
            network=network,
            timeout=self.config.verifier_timeout_seconds,
            env=env,
        )
        runs = _read_verifications(self._root(spec.attempt_id) / "verify", spec, checks)
        if code in (THROWAWAY_TIMED_OUT, THROWAWAY_API_ERROR):
            # 11: a command Crucible could not re-run has not been verified. The gate
            # fails with the reason rather than the attempt failing on an exception.
            detail = self.last_error.get(ROLE_VERIFIER) or (
                f"the verifier did not finish within {self.config.verifier_timeout_seconds}s"
            )
            runs = tuple(
                run if run.ran and run.exit_code >= 0 else replace(run, ran=False, detail=detail)
                for run in runs
            )
        return runs

    async def _run_throwaway(
        self,
        spec: LaunchSpec,
        *,
        role: str,
        script: str,
        mounts: list[dict[str, Any]],
        network: str,
        timeout: int,
        env: Mapping[str, str] | None = None,
        image: str | None = None,
        secret_stdin: InstallationToken | None = None,
        cancelled: CancelCheck | None = None,
    ) -> int:
        """Run one hardened, single-purpose container to completion and remove it.

        `secret_stdin` is a token the container reads from its stdin onto a tmpfs of its
        own (ADR 0019, S10): never `Env`, `Cmd`, a bind source or the writable layer,
        and gone with the container, which the `finally` below removes on every path.

        With `cancelled`, the wait asks it every `cancel_poll_seconds`; a cancel raises
        LaunchCancelledError and the `finally` force-removes the container (hades #189)."""
        launched = self._launched.get(spec.attempt_id)
        image = image or (launched.image_digest if launched else spec.image)
        host_config = self._hardened(spec, network=network)
        host_config["Mounts"] = mounts
        body: dict[str, Any] = {
            "Image": image,
            "Cmd": ["sh", "-c", script],
            "User": "1000:1000",
            "WorkingDir": "/tmp",
            "Env": [f"{k}={v}" for k, v in sorted((env or {}).items())],
            "Labels": self._labels(spec, role),
            "Tty": False,
            "HostConfig": host_config,
        }
        if secret_stdin is not None:
            host_config["Tmpfs"] = {
                **host_config["Tmpfs"],
                scripts.TOKEN_MOUNT: (
                    f"rw,nosuid,nodev,noexec,size={self.config.token_tmpfs_bytes},"
                    "mode=0700,uid=1000,gid=1000"
                ),
            }
            # `docker run -i`: stdin stays open until the attach closes it.
            body.update({"OpenStdin": True, "StdinOnce": True, "AttachStdin": True})
        check_create(body, self._create_policy(spec, resolved=str(image)))
        name = f"crucible-{role}-{spec.attempt_id}"
        container_id = ""
        try:
            if cancelled is not None and await cancelled():
                raise LaunchCancelledError(f"the task was cancelled before the {role} started")
            container_id = await self._call(self.client.create_container, name, body)
            await self._call(self.client.start_container, container_id)
            if secret_stdin is not None:
                await self._call(
                    self.client.write_stdin,
                    container_id,
                    secret_stdin.reveal().encode("utf-8"),
                )
            code = await self._wait_throwaway(
                container_id, role=role, timeout=timeout, cancelled=cancelled
            )
            if code != 0:
                # A throwaway container that failed is an environment failure, and its
                # own output is the only thing that says why.
                self.last_error[role] = await self._tail_of(container_id)
                log.warning(
                    "%s container exited %s", role, code, extra={"tail": self.last_error[role]}
                )
            return code
        except DockerApiError as exc:
            log.warning("%s container failed", role, extra={"error": str(exc)})
            self.last_error[role] = str(exc)
            return THROWAWAY_API_ERROR
        except (TimeoutError, OSError, HTTPException) as exc:
            # The wait outlived the transport. The container may still be running, so
            # it is killed before the finally clause removes it; nothing is left to be
            # waited on again, and the caller records a failure rather than retrying
            # this forever.
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self.last_error[role] = (
                f"the {role} container did not finish within {timeout}s ({detail})"
            )
            log.warning("%s container timed out", role, extra={"error": self.last_error[role]})
            if container_id:
                with contextlib.suppress(Exception):
                    await self._call(self.client.kill_container, container_id)
            return THROWAWAY_TIMED_OUT
        finally:
            if container_id:
                with contextlib.suppress(Exception):
                    await self._call(self.client.remove_container, container_id, force=True)

    async def _wait_throwaway(
        self, container_id: str, *, role: str, timeout: int, cancelled: CancelCheck | None
    ) -> int:
        """The container's exit code. The daemon's wait blocks a thread, so it runs as a
        task of its own and the cancel is asked beside it; a cancelled wait's thread
        returns once the caller's `finally` has force-removed the container."""
        waiting = asyncio.ensure_future(
            self._call(self.client.wait_container, container_id, timeout=float(timeout))
        )
        # Retrieved here so an abandoned wait never logs an unretrieved exception.
        waiting.add_done_callback(lambda t: t.cancelled() or t.exception())
        poll = self.cancel_poll_seconds if cancelled is not None else None
        while True:
            done, _ = await asyncio.wait({waiting}, timeout=poll)
            if done:
                return int(waiting.result())
            assert cancelled is not None
            if await cancelled():
                raise LaunchCancelledError(f"the task was cancelled while the {role} ran")

    async def _tail_of(self, container_id: str, limit: int = 4000) -> str:
        """What a failed throwaway container said, for the event that records it."""
        try:
            frames = await self._call(self.client.container_logs, container_id)
        except (DockerApiError, TimeoutError, OSError, HTTPException):
            return ""
        body = b"".join(frame.payload for frame in frames).decode("utf-8", "replace")
        return body[-limit:]

    async def _workspace_state(self, attempt_id: str, *, keep: str) -> WorkspaceState:
        try:
            rows = await self._call(
                self.client.list_containers, filters={"label": [f"{LABEL_ATTEMPT}={attempt_id}"]}
            )
        except DockerApiError as exc:
            return WorkspaceState(checked=False, detail=str(exc))
        leftover = tuple(
            sorted(
                str(row.get("Names", [row.get("Id")])[0]).lstrip("/")
                for row in rows
                if str(row.get("Id", "")) != keep
            )
        )
        return WorkspaceState(leftover=leftover)

    async def terminate(self, h: Handle, mode: Literal["drain", "kill"]) -> None:
        signal = "SIGTERM" if mode == "drain" else "SIGKILL"
        try:
            await self._call(self.client.kill_container, h.ref, signal=signal)
        except DockerApiError as exc:
            if exc.status != 404:
                raise ProviderError(f"terminate failed: {exc}") from exc

    async def discard(self, ws: Workspace, spec: LaunchSpec | None = None) -> None:
        """Remove the credential copy of an attempt that will never be collected (12):
        a launch that failed after seeding, or a worker the daemon lost. Nothing else
        of the workspace is touched; cleanup decides that later, or never."""
        root = self._root(ws.attempt_id)
        if not (root / CREDENTIAL_LEAF).exists():
            return
        await asyncio.to_thread(shutil.rmtree, root / CREDENTIAL_LEAF, True)
        if (root / CREDENTIAL_LEAF).exists():
            await self._remove_through_daemon(ws, spec, [CREDENTIAL_LEAF])

    async def cleanup(
        self, ws: Workspace, policy: CleanupPolicy, spec: LaunchSpec | None = None
    ) -> None:
        """Only ever called for an attempt that recorded `logs_drained` (08), or for one
        that ended before its worker launched (hades #394)."""
        for row in await self._containers_for(ws.attempt_id):
            await self._call(self.client.remove_container, str(row["Id"]), force=True)
        root = self._root(ws.attempt_id)
        if policy is CleanupPolicy.KEEP:
            # A kept workspace keeps its evidence, never its credential copy (12).
            leaves: tuple[str, ...] = (CREDENTIAL_LEAF,)
        elif policy is CleanupPolicy.DELETE:
            leaves = ("",)
        else:
            # keep_diff_only: the checkout, the verifier's tree and any credential copy
            # go; the collected evidence (diff, bundle, report copy, verifier logs) stays.
            leaves = ("repo", "output/tree", CREDENTIAL_LEAF)
        for leaf in leaves:
            await asyncio.to_thread(shutil.rmtree, root / leaf if leaf else root, True)
        left = [leaf for leaf in leaves if (root / leaf if leaf else root).exists()]
        if left:
            # The tree belongs to the container's uid, which is not Crucible's in every
            # arrangement (S9 Test E), so what the daemon made, the daemon removes.
            await self._remove_through_daemon(ws, spec, left)

    async def release_workspace(self, ws: Workspace, spec: LaunchSpec | None = None) -> None:
        """16: the workspace directory a cleanup policy kept, once the retention step
        decided nothing needs it. The same removal `cleanup` does under `delete`."""
        await self.cleanup(ws, CleanupPolicy.DELETE, spec)
        if self._root(ws.attempt_id).exists():
            raise ProviderError(f"the workspace of {ws.attempt_id} could not be removed")

    async def _remove_through_daemon(
        self, ws: Workspace, spec: LaunchSpec | None, leaves: Sequence[str]
    ) -> None:
        launched = self._launched.get(ws.attempt_id)
        spec = spec or (launched.spec if launched else None)
        if spec is None:
            log.warning(
                "workspace left in place: no launch spec to remove it with",
                extra={"attempt_id": ws.attempt_id},
            )
            return
        targets = " ".join(
            f'"{WORK_MOUNT}/{leaf}"' if leaf else f'"{WORK_MOUNT}"/* "{WORK_MOUNT}"/.[!.]*'
            for leaf in leaves
        )
        await self._run_throwaway(
            spec,
            role=ROLE_CLEANER,
            script=f"rm -rf {targets} 2>/dev/null; exit 0\n",
            mounts=[self._daemon_mount(ws.attempt_id, "", WORK_MOUNT, read_only=False)],
            network="none",
            timeout=120,
        )

    async def logins_in_progress(self) -> frozenset[str]:
        """The harnesses whose login container is running (12, 25). The login writes
        into the credential directory as it runs, so a launch that seeded from it now
        would copy a half-written session; the supervisor holds such a launch back."""
        try:
            rows = await self._call(
                self.client.list_containers,
                all_states=False,
                filters={"label": [f"{LABEL_ROLE}={ROLE_LOGIN}"]},
            )
        except DockerApiError as exc:
            raise ProviderError(f"the login containers could not be listed: {exc}") from exc
        return frozenset(
            str((row.get("Labels") or {}).get(LABEL_HARNESS, ""))
            for row in rows
            if (row.get("Labels") or {}).get(LABEL_HARNESS)
        )

    async def _containers_for(self, attempt_id: str) -> list[dict[str, Any]]:
        try:
            return await self._call(  # type: ignore[no-any-return]
                self.client.list_containers, filters={"label": [f"{LABEL_ATTEMPT}={attempt_id}"]}
            )
        except DockerApiError:
            return []

    async def reconcile(self) -> list[Handle]:
        """Adopt by label (10). Only running workers are handles."""
        try:
            rows = await self._call(
                self.client.list_containers,
                all_states=False,
                filters={"label": [f"{LABEL_ROLE}={ROLE_WORKER}"]},
            )
        except DockerApiError as exc:
            raise ProviderError(f"reconcile failed: {exc}") from exc
        handles: list[Handle] = []
        for row in rows:
            labels = {str(k): str(v) for k, v in (row.get("Labels") or {}).items()}
            attempt_id = labels.get(LABEL_ATTEMPT)
            if not attempt_id:
                continue
            handles.append(
                Handle(
                    provider=self.name,
                    ref=str(row["Id"]),
                    attempt_id=attempt_id,
                    name=str((row.get("Names") or [""])[0]).lstrip("/"),
                )
            )
        return handles

    async def retention(self, keep: Sequence[str]) -> int:
        """Remove containers and volumes labelled for attempts Crucible no longer
        tracks (16). Every removal the caller records as a RetentionAction."""
        live = set(keep)
        removed = 0
        try:
            rows = await self._call(self.client.list_containers, filters={"label": [LABEL_ATTEMPT]})
        except DockerApiError:
            return 0
        for row in rows:
            labels = {str(k): str(v) for k, v in (row.get("Labels") or {}).items()}
            attempt_id = labels.get(LABEL_ATTEMPT, "")
            if labels.get(LABEL_ROLE) == ROLE_LOGIN and attempt_id in self._active_login_ids:
                # A live process owns this administrative session. A restarted provider
                # has an empty set, so its first retention sweep reaps stale logins.
                continue
            if attempt_id and attempt_id not in live:
                await self._call(self.client.remove_container, str(row["Id"]), force=True)
                removed += 1
        return removed

    async def health(self) -> ProviderHealth:
        """25: daemon reachable through the proxy, workers network present, disk headroom."""
        checks: dict[str, Any] = {}
        state = "ok"
        try:
            checks["daemon"] = await self._call(self.client.ping)
        except Exception as exc:
            checks["daemon"] = f"unreachable: {type(exc).__name__}"
            return ProviderHealth("unavailable", checks)
        try:
            await self._call(self.client.inspect_network, self.config.workers_network)
            checks["network"] = self.config.workers_network
        except Exception:
            checks["network"] = "absent"
            state = "degraded"
        try:
            usage = shutil.disk_usage(self.config.artifact_root)
            checks["disk_free_bytes"] = usage.free
            if usage.free < 2 * 1024**3:
                state = "degraded"
        except OSError:
            checks["disk_free_bytes"] = None
            state = "degraded"
        return ProviderHealth(state, checks)

    async def probe_credential(self, request: ProbeRequest) -> ProbeResult:
        """25: the bounded auth probe. The same launch path as a worker (the hardened
        shape, the credential copy seeded and mounted, the wrapper), a one-line prompt
        with a hard timeout, the named files read back and synced, then everything
        removed: the container, the copy, the workspace."""
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
        root = self._root(probe_id)
        await asyncio.to_thread(shutil.rmtree, root, True)
        # 25: the probe removes everything. The preparer, the image resolution and the
        # identity writes can each fail, so they are inside the same try whose finally
        # removes the containers and the workspace; nothing is seeded before this point.
        started = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        oom = False
        stdout_tail = stderr_tail = ""
        sync = None
        detail = ""
        handle: Handle | None = None
        resolved = ""
        ws: Workspace | None = None
        try:
            paths = await asyncio.to_thread(self._make_dirs, root)
            ws = Workspace(
                attempt_id=probe_id,
                checkout_path=str(paths["repo"]),
                identity_path=str(paths["identity"]),
                report_path=str(paths["report"]),
                output_path=str(paths["output"]),
            )
            resolved = await self._resolve_image(spec)
            started = time.monotonic()
            # The preparer's role, reduced to what a probe needs: the directories the worker's
            # own uid must own, the credential leaf mode 700 (12, S9 Test E).
            code = await self._run_throwaway(
                spec,
                role=ROLE_PREPARER,
                image=resolved,
                script=(
                    f"set -eu; mkdir -p {WORK_MOUNT}/repo {WORK_MOUNT}/report; "
                    f"mkdir -m 0700 -p {WORK_MOUNT}/{CREDENTIAL_LEAF}\n"
                ),
                mounts=[self._daemon_mount(probe_id, "", WORK_MOUNT, read_only=False)],
                network="none",
                timeout=60,
            )
            if code != 0:
                raise ProviderError(f"the probe could not prepare its workspace (exit {code})")
            adapter = self.harnesses.get(request.harness)
            credential = adapter.credential_spec() if adapter is not None else None
            identity = paths["identity"]
            (identity / "IDENTITY.md").write_text(request.identity_text, encoding="utf-8")
            os.chmod(identity / "IDENTITY.md", 0o444)
            if credential is not None and credential.templates:
                template_dir = identity / "harness"
                template_dir.mkdir(parents=True, exist_ok=True)
                for name, content in credential.templates.items():
                    (template_dir / name).write_text(content, encoding="utf-8")
                    os.chmod(template_dir / name, 0o444)
            handle = await self.launch(ws, spec)
            try:
                exit_code = int(
                    await self._call(
                        self.client.wait_container,
                        handle.ref,
                        timeout=float(request.timeout_seconds),
                    )
                )
            except (TimeoutError, OSError, HTTPException, DockerApiError) as exc:
                timed_out = True
                detail = (
                    f"the probe did not finish within {request.timeout_seconds}s "
                    f"({type(exc).__name__})"
                )
                with contextlib.suppress(Exception):
                    await self._call(self.client.kill_container, handle.ref)
            observation = await self.observe(handle)
            if observation.state is ObservationState.EXITED:
                exit_code = observation.exit_code if exit_code is None else exit_code
                oom = observation.oom_killed
            stdout_tail, stderr_tail = await self._worker_tails(handle.ref)
            sync = await self._sync_credential(handle, ws, spec)
        finally:
            for row in await self._containers_for(probe_id):
                with contextlib.suppress(Exception):
                    await self._call(self.client.remove_container, str(row["Id"]), force=True)
            if ws is not None:
                with contextlib.suppress(Exception):
                    await self.cleanup(ws, CleanupPolicy.DELETE, spec)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(shutil.rmtree, root, True)
            self._launched.pop(probe_id, None)
        labels = self._images.get(spec.image)
        return ProbeResult(
            exit_code=exit_code,
            image_digest=resolved,
            harness_version=(
                image_harnesses(labels.labels).get(request.harness) or None if labels else None
            ),
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            oom_killed=oom,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            credential_sync=sync,
            detail=detail,
        )

    async def run_login_container(
        self,
        *,
        flow: Any,
        image: str,
        directory: str,
        session: Any,
        argv: tuple[str, ...],
        timeout: int,
    ) -> None:
        """Run one harness login with only its credential directory and proxy network.

        The TTY is attached directly and the daemon log driver is disabled. This keeps
        a one-time provider token in process memory long enough to capture it without
        placing it in Docker logs. The container is always reaped.
        """
        from crucible.application.admin.login import (  # noqa: PLC0415
            ENTER,
            ENTER_PAUSE_SECONDS,
            _consume,
        )

        login_id = f"login{new_id()}"[:26]
        adapter = self.harnesses.require(flow.harness)
        credential = adapter.credential_spec()
        if credential is None:
            raise ProviderError(f"harness {flow.harness!r} has no credential")
        source = Path(directory)
        configured_root = self.config.credential_host_root or self.config.credential_root
        if configured_root is None or source.parent != Path(configured_root):
            raise ProviderError(
                f"the {flow.harness} login credential path is not directly under the "
                "configured dedicated credential root"
            )
        source.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            policy={
                "images": {"allowlist": [image]},
                "network": {"mode": "egress-proxy", "egress_allowlist": []},
                "resources": {"memory": "4GiB", "cpus": 2, "pids": 512},
            },
            owner="crucible-admin",
        )
        await self._ensure_network()
        resolved = await self._resolve_image(spec)
        wanted = egress_allowlist(self.harnesses, flow.harness, [], [], None)
        configured = set(self.config.proxy_allowlist)
        if configured and not set(wanted) <= configured:
            raise ProviderError(
                f"the egress proxy does not permit the {flow.harness} login endpoints"
            )
        env = {
            "HOME": "/home/worker",
            "TERM": "xterm",
            flow.directory_env: (
                "/home/worker" if flow.harness == "agy" else credential.mount_target
            ),
            "CRUCIBLE_EGRESS_ALLOWLIST": ",".join(wanted),
        }
        if self.config.egress_proxy:
            env.update(
                {
                    "HTTPS_PROXY": self.config.egress_proxy,
                    "HTTP_PROXY": self.config.egress_proxy,
                    "https_proxy": self.config.egress_proxy,
                    "http_proxy": self.config.egress_proxy,
                    "NO_PROXY": self.config.no_proxy,
                    "no_proxy": self.config.no_proxy,
                }
            )
        target = "/home/worker" if flow.harness == "agy" else credential.mount_target
        if self.config.credential_volume:
            mount = {
                "Type": "volume",
                "Source": self.config.credential_volume,
                "Target": target,
                "ReadOnly": False,
                "VolumeOptions": {"Subpath": source.name},
            }
        else:
            root = self.config.credential_host_root or self.config.credential_root
            if not root:
                raise ProviderError("the Docker credential root is not configured")
            mount = {
                "Type": "bind",
                "Source": str(Path(root) / source.name),
                "Target": target,
                "ReadOnly": False,
                "BindOptions": {"Propagation": "rprivate"},
            }
        host_config = self._hardened(spec, network=self.config.workers_network, tmpfs_mb=128)
        host_config["Mounts"] = [mount]
        host_config["LogConfig"] = {"Type": "none", "Config": {}}
        body: dict[str, Any] = {
            "Image": resolved,
            # A worker image's normal entrypoint may require the task identity bundle.
            # Login is an administrative flow with no workspace or task identity, so
            # invoke the adapter's declared executable directly.
            "Entrypoint": [argv[0]],
            "Cmd": list(argv[1:]),
            "User": "1000:1000",
            "WorkingDir": "/tmp",
            "Env": [f"{key}={value}" for key, value in sorted(env.items())],
            "Labels": {**self._labels(spec, ROLE_LOGIN), LABEL_HARNESS: flow.harness},
            "Tty": True,
            "OpenStdin": True,
            "AttachStdin": True,
            "AttachStdout": True,
            "AttachStderr": True,
            "HostConfig": host_config,
        }
        check_create(body, self._create_policy(spec, resolved=resolved))
        container_id = ""
        connection = None
        response = None
        sock = None
        buffer = ""
        token_re = re.compile(flow.token_pattern) if flow.captures_token else None
        deadline = time.monotonic() + timeout
        self._active_login_ids.add(login_id)
        try:
            container_id = await self._call(
                self.client.create_container, f"crucible-login-{flow.harness}-{login_id}", body
            )
            connection, response, sock = await self._call(
                self.client.attach_interactive, container_id
            )
            await self._call(self.client.start_container, container_id)
            session.state = "waiting_for_operator"
            while True:
                if session.cancel_requested:
                    session.error = "login cancelled"
                    await self._call(self.client.kill_container, container_id)
                    break
                if time.monotonic() >= deadline:
                    session.error = "login timed out"
                    await self._call(self.client.kill_container, container_id)
                    break
                assert sock is not None
                ready, _, _ = await asyncio.to_thread(select.select, [sock], [], [], 0.25)
                if ready:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk.decode("utf-8", "replace")
                    buffer = _consume(buffer, flow, token_re, source, session, None)
                session.notice_waiting(flow)
                if session.state == "waiting_for_code":
                    code = session.wait_for_code(0)
                    if code is not None and session.error is None:
                        # The Enter key: Claude Code submits only on a carriage return,
                        # and only one that arrives apart from the code (hades #173).
                        sock.sendall(code.strip().encode("utf-8"))
                        session.state = "waiting_for_operator"
                        await asyncio.sleep(ENTER_PAUSE_SECONDS)
                        sock.sendall(ENTER.encode("utf-8"))
                state = await self._call(self.client.inspect_container, container_id)
                if not bool((state.get("State") or {}).get("Running", False)):
                    break
            if buffer:
                _consume(buffer + "\n", flow, token_re, source, session, None)
            state = await self._call(self.client.inspect_container, container_id)
            exit_code = (state.get("State") or {}).get("ExitCode")
            session.exit_code = int(exit_code) if exit_code is not None else None
            if session.cancel_requested:
                session.state = "failed"
            else:
                session.state = (
                    "finished" if session.exit_code == 0 and not session.error else "failed"
                )
            if session.state == "failed" and session.error is None:
                session.error = f"the login command exited {session.exit_code}"
        except Exception as exc:
            session.state = "failed"
            session.error = f"the login container failed: {type(exc).__name__}: {exc}"
        finally:
            self._active_login_ids.discard(login_id)
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
            if container_id:
                with contextlib.suppress(Exception):
                    await self._call(self.client.remove_container, container_id, force=True)

    async def list_images(self) -> list[ImageInfo]:
        """Every image on the daemon that declares a harness (13): the worker image's
        `crucible.harnesses` label (C11), or the single `crucible.harness` label of an
        image built before it. Docker ANDs label filters, so each is its own query."""
        rows: dict[str, dict[str, Any]] = {}
        for label in (HARNESSES_LABEL, LEGACY_HARNESS_LABEL):
            try:
                found = await self._call(self.client.list_images, {"label": [label]})
            except DockerApiError as exc:
                raise ProviderError(f"image listing failed: {exc}") from exc
            for row in found:
                rows.setdefault(str(row.get("Id", "")), row)
        images: list[ImageInfo] = []
        for row in rows.values():
            labels = {str(k): str(v) for k, v in (row.get("Labels") or {}).items()}
            digests = [str(d) for d in (row.get("RepoDigests") or [])]
            tags = [str(t) for t in (row.get("RepoTags") or []) if t and t != "<none>:<none>"]
            for reference in tags or [str(row.get("Id", ""))]:
                image = ImageInfo.from_labels(
                    reference, digests[0] if digests else str(row.get("Id", "")), labels
                )
                if image.harnesses:
                    images.append(image)
        return sorted(images, key=lambda i: i.reference)


# ----- log resume --------------------------------------------------------


def _since_param(timestamp: str | None) -> str | None:
    """Docker's `since` wants `<seconds>.<nanoseconds>`, not RFC 3339.

    The daemon splits the value on the dot and parses both halves as integers, so an
    RFC 3339 string is a 500. The stored offset stays RFC 3339 because that is what a
    reader and a comparison want; this is the wire form."""
    if not timestamp:
        return None
    try:
        moment = parse_rfc3339(timestamp)
    except ValueError:
        return None
    return f"{int(moment.timestamp())}.{moment.microsecond * 1000:09d}"


# ----- the credential copy (12) --------------------------------------------


def _seed_renewer_tar(copy: _CredentialCopy) -> tuple[bytes, dict[str, str | None]]:
    login = json.loads((Path(copy.source.path) / "auth.json").read_text(encoding="utf-8"))
    data = json.dumps(access_token_document(login), separators=(",", ":")).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("access-token.json")
        info.size = len(data)
        info.mode = 0o600
        info.uid = info.gid = WORKER_UID
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue(), {"access-token.json": hashlib.sha256(data).hexdigest()}


def _seed_tar(
    spec: CredentialSpec, source: CredentialSource
) -> tuple[bytes, dict[str, str | None]]:
    """A tar of the named auth files, owned by the worker's uid, mode 600. Built in
    memory; the bytes go to the daemon and the hashes stay with the provider. A required
    file that is missing refuses the launch rather than seeding a copy that cannot
    authenticate."""
    buffer = io.BytesIO()
    hashes: dict[str, str | None] = {}
    now = int(time.time())
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        directories: set[str] = set()

        def ensure_dirs(name: str) -> None:
            parts = name.split("/")[:-1]
            for index in range(1, len(parts) + 1):
                directory = "/".join(parts[:index])
                if directory in directories:
                    continue
                directories.add(directory)
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                info.uid = info.gid = WORKER_UID
                info.mtime = now
                tar.addfile(info)

        for auth in spec.auth_files:
            path = spec.source_path(source.path, auth.name)
            try:
                data = path.read_bytes()
            except OSError as exc:
                if auth.required:
                    raise HarnessRefusedError(
                        f"refusing to launch: the credential for {spec.harness!r} is missing "
                        f"its auth file {auth.name!r} ({type(exc).__name__})"
                    ) from exc
                hashes[auth.name] = None
                continue
            ensure_dirs(auth.name)
            info = tarfile.TarInfo(auth.name)
            info.size = len(data)
            info.mode = 0o600
            info.uid = info.gid = WORKER_UID
            info.mtime = now
            tar.addfile(info, io.BytesIO(data))
            hashes[auth.name] = hashlib.sha256(data).hexdigest()
        # No placeholder for the templates: the daemon mounts every declared mount,
        # the read-only template files included, before it extracts an archive, so a
        # file at that path in the tar would collide with the mount point. The daemon
        # creates the mount point itself (S1 observed the empty settings.json).
    return buffer.getvalue(), hashes


def _single_file(raw: bytes) -> bytes | None:
    """The first regular file in a tar the daemon returned for one path."""
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
            for member in tar:
                if member.isreg():
                    extracted = tar.extractfile(member)
                    return extracted.read() if extracted is not None else None
    except tarfile.TarError:
        return None
    return None


def _dig(document: Any, path: tuple[str, ...]) -> Any:
    current = document
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _issued_at(document: Any, path: tuple[str, ...] | None) -> datetime | None:
    if path is None:
        return None
    value = _dig(document, path)
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_rfc3339(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _sync_one(copy: _CredentialCopy, auth: AuthFile, data: bytes) -> CredentialFileSync:
    """Decide one file's sync-back (12): changed against the source, valid JSON shape,
    newer issued-at than the source, then an atomic replace, mode 600."""
    target = copy.spec.source_path(copy.source.path, auth.name)
    try:
        current = target.read_bytes()
    except OSError:
        current = None
    changed = current is None or hashlib.sha256(data).digest() != hashlib.sha256(current).digest()
    if not changed:
        return CredentialFileSync(auth.name, True, False, True, False, "unchanged")
    if not auth.sync_back:
        return CredentialFileSync(
            auth.name, True, True, True, False, "changed; state, never written back"
        )
    new_document: Any = None
    if auth.json:
        try:
            new_document = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            new_document = None
        valid = isinstance(new_document, dict) and all(
            key in new_document for key in auth.json_keys
        )
        if not valid:
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
    old_document: Any = None
    if current is not None:
        with contextlib.suppress(UnicodeDecodeError, ValueError):
            old_document = json.loads(current.decode("utf-8"))
    older = _issued_at(old_document, auth.issued_at)
    if older is not None and newer <= older:
        return CredentialFileSync(
            auth.name, True, True, True, False, "changed; not newer than the source"
        )
    temporary = target.with_name(target.name + ".crucible-sync")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporary.unlink()
        return CredentialFileSync(
            auth.name, True, True, True, False, f"changed; write back failed: {type(exc).__name__}"
        )
    return CredentialFileSync(
        auth.name, True, True, True, True, "changed; newer issued-at, written back"
    )


def _tails(frames: Sequence[LogFrame], limit: int) -> tuple[str, str]:
    out: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    for frame in frames:
        out["stderr" if frame.stream == "stderr" else "stdout"].extend(frame.payload)
    return (
        bytes(out["stdout"][-limit:]).decode("utf-8", "replace"),
        bytes(out["stderr"][-limit:]).decode("utf-8", "replace"),
    )
