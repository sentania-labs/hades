"""Room runners through the existing providers (hades #208, ADR 0031).

A room runner is launched the way an attempt's worker is, with no checkout and no
repository: the worker image, the hardened shape of 26 (or 13 under Docker), the
harness credential delivered the way the harness's attempts get it, and egress to the
model API, the package index `uv run --with claude-agent-sdk` installs from, and Hades.
The command is the launch wrapper around

    uv run --no-project --with claude-agent-sdk python tools/room_runner.py

run from RUNNER_DIR, where `tools/room_runner.py` is mounted read-only. For Claude Code
the wrapper reads `CLAUDE_CODE_OAUTH_TOKEN` from the credential projection at the
adapter's mount target, exactly as a `claude_code` attempt's wrapper does
(`CredentialSpec.env_from_files`). The runner's own CLAUDE_CONFIG_DIR is RUNNER_CONFIG_DIR,
a writable per-room emptyDir (a tmpfs under Docker), so the harness session lives as
long as the runner and no longer. The room-scoped token is a file on its own read-only
mount, never an environment value.

The pure functions render what is sent; the two launchers do the I/O through the
providers' own clients, labels and egress machinery. Room objects carry `crucible.room`
and never `crucible.attempt`, so attempt reconcile and retention never touch them."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.docker import LAUNCH_WRAPPER, DockerProvider
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.k8sspec import EgressPlan, Limits, Mount, PeerSelector, PodRequest
from crucible.adapters.execution.kubernetes import KubernetesProvider
from crucible.adapters.harness.registry import default_registry
from crucible.application.harnesses import HarnessRegistry
from crucible.ports.execution import LaunchSpec
from crucible.ports.harness import CredentialSpec
from crucible.ports.rooms import (
    RUNNER_COMMAND,
    RUNNER_CONFIG_DIR,
    RUNNER_DIR,
    RUNNER_TOKEN_DIR,
    RUNNER_TOKEN_FILE,
    RoomRunnerError,
    RoomRunnerLaunch,
)

log = logging.getLogger("crucible.rooms.launch")

LABEL_ROOM = "crucible.room"
ROLE_ROOM_RUNNER = "room-runner"
RUNNER_SCRIPT = "tools/room_runner.py"
SCRIPT_KEY = "room_runner.py"
TOKEN_KEY = "token"
CLI_PATH = "/usr/local/bin/claude"
# A room runner holds one Claude Code child (about 217 MB idle in the spike) and its
# Python process (about 75 MB), plus `uv`'s install; one CPU and 1 GiB is ample.
ROOM_LIMITS = Limits(
    cpus=1.0,
    memory_bytes=1024**3,
    ephemeral_storage="2Gi",
    tmpfs_bytes=512 * 1024**2,
    grace_seconds=30,
    cpu_request_fraction=0.25,
    memory_request_fraction=0.5,
)
CONFIG_VOLUME_BYTES = 256 * 1024**2
# A runner exits on its own after the idle timeout; this is the Job's hard ceiling.
DEFAULT_MAX_RUNNER_SECONDS = 12 * 3600


def object_name(room_id: str) -> str:
    return f"room-{room_id.lower()}"


def handle_for(provider: str, room_id: str) -> str:
    return f"{provider}:{object_name(room_id)}"


def room_of(handle: str) -> tuple[str, str]:
    provider, _, name = handle.partition(":")
    if not name.startswith("room-"):
        raise RoomRunnerError(f"{handle!r} is not a room runner handle")
    return provider, name


def runner_script(path: str | None = None) -> str:
    """tools/room_runner.py: the configured path, else the working directory (the
    service image's /app), else next to this checkout."""
    candidates = [Path(path)] if path else []
    candidates.append(Path.cwd() / RUNNER_SCRIPT)
    candidates.append(Path(__file__).resolve().parents[3] / RUNNER_SCRIPT)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise RoomRunnerError(f"{RUNNER_SCRIPT} is not readable; the room runner cannot start")


def credential_spec(harnesses: HarnessRegistry, harness: str) -> CredentialSpec:
    adapter = harnesses.get(harness)
    spec = adapter.credential_spec() if adapter is not None else None
    if spec is None:
        raise RoomRunnerError(f"{harness} declares no credential, so no room can run on it")
    return spec


def runner_command() -> list[str]:
    """The launch wrapper around the runner, as an attempt's harness is wrapped: it
    probes the egress allowlist, reads CRUCIBLE_ENV_FROM_FILES into the environment and
    keeps the runner a direct child that receives the TERM."""
    return ["bash", "-o", "pipefail", "-c", LAUNCH_WRAPPER, "crucible-launch", *RUNNER_COMMAND]


def runner_env(
    launch: RoomRunnerLaunch, credential: CredentialSpec, *, egress_hosts: Sequence[str]
) -> dict[str, str]:
    """The runner's environment: names and paths only, never a credential value."""
    return {
        "HOME": "/home/worker",
        "UV_CACHE_DIR": "/tmp/uv-cache",
        "UV_NO_PROGRESS": "1",
        "PYTHONUNBUFFERED": "1",
        "HADES_API_URL": launch.api_url,
        "HADES_ROOM_ID": launch.room_id,
        "HADES_ROOM_TOKEN_FILE": RUNNER_TOKEN_FILE,
        "ROOM_HARNESS": launch.harness,
        "ROOM_MODEL": launch.model,
        "ROOM_CWD": launch.cwd,
        "ROOM_CLI_PATH": CLI_PATH,
        "ROOM_CLAUDE_CONFIG_DIR": RUNNER_CONFIG_DIR,
        "ROOM_IDLE_TIMEOUT_SECONDS": str(launch.idle_timeout_seconds),
        "CRUCIBLE_EGRESS_ALLOWLIST": ",".join(egress_hosts),
        # The one way the harness credential reaches the runner: the wrapper reads each
        # file into its variable, as it does for an attempt (07).
        "CRUCIBLE_ENV_FROM_FILES": " ".join(
            f"{var}={path}" for var, path in sorted(credential.env_from_files().items())
        ),
    }


def credential_files(credential: CredentialSpec) -> list[str]:
    """The auth files a room runner needs: the ones that become an environment value."""
    return [auth.name for auth in credential.auth_files if auth.env_var]


def _launch_spec(launch: RoomRunnerLaunch) -> LaunchSpec:
    """The attempt-shaped spec the providers' image and egress checks take."""
    return LaunchSpec(
        attempt_id=launch.room_id,
        task_id="",
        external_id=object_name(launch.room_id),
        role=ROLE_ROOM_RUNNER,
        harness=launch.harness,
        model=launch.model,
        image=launch.image,
        timeout_seconds=DEFAULT_MAX_RUNNER_SECONDS,
        contract={},
        owner=launch.owner,
    )


# ----- Kubernetes ---------------------------------------------------------------


def k8s_labels(launch: RoomRunnerLaunch) -> dict[str, str]:
    return {
        LABEL_ROOM: launch.room_id,
        k8sspec.LABEL_ROLE: ROLE_ROOM_RUNNER,
        k8sspec.LABEL_OWNER: launch.owner,
    }


def k8s_pod(
    launch: RoomRunnerLaunch,
    credential: CredentialSpec,
    *,
    image: str,
    egress_hosts: Sequence[str],
    service_account: str,
    image_pull_secret: str | None,
    host_aliases: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The runner's Pod: 26's shape (non-root, read-only root, no capabilities, no
    service account token), with the credential projection at the adapter's mount
    target, the token and the runner script on read-only mounts of their own, and an
    emptyDir for CLAUDE_CONFIG_DIR."""
    name = object_name(launch.room_id)
    files = credential_files(credential)
    volumes = [
        *k8sspec.base_volumes(ROOM_LIMITS),
        {
            "name": "room-credential",
            "secret": {
                "secretName": name,
                "defaultMode": 0o400,
                "items": [{"key": _key(f), "path": f, "mode": 0o400} for f in files],
            },
        },
        {
            "name": "room-token",
            "secret": {
                "secretName": name,
                "defaultMode": 0o400,
                "items": [{"key": TOKEN_KEY, "path": "token", "mode": 0o400}],
            },
        },
        {
            "name": "room-runner",
            "configMap": {
                "name": name,
                "defaultMode": 0o444,
                "items": [{"key": SCRIPT_KEY, "path": RUNNER_SCRIPT}],
            },
        },
        # hades #208: the per-room CLAUDE_CONFIG_DIR. An emptyDir for now, so the
        # harness session goes with the Pod; Hades replays the transcript into the next.
        {"name": "room-config", "emptyDir": {"sizeLimit": str(CONFIG_VOLUME_BYTES)}},
    ]
    mounts = [
        *k8sspec.base_mounts(),
        Mount("room-credential", credential.mount_target, read_only=True),
        Mount("room-token", RUNNER_TOKEN_DIR, read_only=True),
        Mount("room-runner", RUNNER_DIR, read_only=True),
        Mount("room-config", RUNNER_CONFIG_DIR),
    ]
    return k8sspec.pod_spec(
        PodRequest(
            role=ROLE_ROOM_RUNNER,
            image=image,
            command=runner_command(),
            limits=ROOM_LIMITS,
            env=runner_env(launch, credential, egress_hosts=egress_hosts),
            mounts=mounts,
            volumes=volumes,
            working_dir=RUNNER_DIR,
            service_account=service_account,
            image_pull_secret=image_pull_secret,
            host_aliases=host_aliases,
        )
    )


def api_rule(selector: PeerSelector, port: int) -> dict[str, Any]:
    """The one destination inside the cluster a room runner may reach: the Hades API
    pods, on the API port. `check_selector` refuses Crucible's own namespace for every
    worker; this rule names it on purpose, and names nothing else of it."""
    if not selector.pod_labels:
        raise RoomRunnerError("the Hades API selector names no pod labels")
    return {"to": [selector.peer()], "ports": [{"protocol": "TCP", "port": int(port)}]}


def _key(name: str) -> str:
    return name.replace("/", "_")


@dataclass(slots=True)
class KubernetesRoomLauncher:
    """Room runners as Jobs in the workers namespace, through the Kubernetes provider."""

    provider: KubernetesProvider
    api_namespace: str = "hades"
    api_pod_labels: Mapping[str, str] = field(
        default_factory=lambda: {
            "app.kubernetes.io/name": "crucible",
            "app.kubernetes.io/component": "api",
        }
    )
    api_port: int = 8080
    script_path: str | None = None
    max_runner_seconds: int = DEFAULT_MAX_RUNNER_SECONDS
    name: str = "kubernetes"

    async def launch(self, launch: RoomRunnerLaunch) -> str:
        p = self.provider
        spec = _launch_spec(launch)
        credential = credential_spec(p.harnesses, launch.harness)
        script = runner_script(self.script_path)
        await p._require_ready(spec)
        image = await p._resolve_image(spec, use_backoff=True)
        name = object_name(launch.room_id)
        labels = k8s_labels(launch)
        selector = {LABEL_ROOM: launch.room_id, k8sspec.LABEL_ROLE: ROLE_ROOM_RUNNER}
        try:
            source = await p._call_with_backoff(
                p.client.get, "secrets", p.config.credential_secret_name(launch.harness)
            )
        except KubernetesApiError as exc:
            raise RoomRunnerError(
                f"the {launch.harness} credential Secret is not readable ({exc.status})"
            ) from exc
        data = source.get("data") or {}
        payload: dict[str, bytes] = {TOKEN_KEY: launch.token.encode("utf-8")}
        for file in credential_files(credential):
            raw = data.get(_key(file))
            if not raw:
                raise RoomRunnerError(
                    f"the {launch.harness} credential Secret has no {file}; log in first"
                )
            payload[_key(file)] = base64.b64decode(str(raw))
        plan = await p._resolve_plan(EgressPlan(hosts=tuple(launch.egress_hosts)), broad=False)
        policy = p._policy_body(
            name, labels, launch.room_id, ROLE_ROOM_RUNNER, plan, pod_selector=selector
        )
        policy["spec"]["egress"].append(
            api_rule(PeerSelector.of(self.api_namespace, self.api_pod_labels), self.api_port)
        )
        pod = k8s_pod(
            launch,
            credential,
            image=image,
            egress_hosts=launch.egress_hosts,
            service_account=p.config.service_account,
            image_pull_secret=p.config.image_pull_secret,
            host_aliases=k8sspec.host_aliases(plan),
        )
        job = k8sspec.job(
            name=name,
            namespace=p.config.namespace,
            object_labels=labels,
            pod=pod,
            active_deadline_seconds=self.max_runner_seconds,
        )
        bodies: list[tuple[str, dict[str, Any]]] = [
            (
                "secrets",
                k8sspec.secret(
                    name=name, namespace=p.config.namespace, object_labels=labels, data=payload
                ),
            ),
            (
                "configmaps",
                k8sspec.config_map(
                    name=name,
                    namespace=p.config.namespace,
                    object_labels=labels,
                    data={SCRIPT_KEY: script},
                ),
            ),
            ("networkpolicies", policy),
            ("jobs", job),
        ]
        try:
            for kind, body in bodies:
                await p._create_with_backoff(kind, body)
        except Exception as exc:
            await self._remove(name)
            raise RoomRunnerError(f"could not start the room runner: {exc}") from exc
        return handle_for(self.name, launch.room_id)

    async def stop(self, handle: str) -> None:
        _provider, name = room_of(handle)
        await self._remove(name)

    async def _remove(self, name: str) -> None:
        p = self.provider
        for kind in ("jobs", "networkpolicies", "configmaps", "secrets"):
            with contextlib.suppress(KubernetesApiError):
                await p._call(p.client.delete, kind, name)


# ----- Docker (make up) -----------------------------------------------------------


@dataclass(slots=True)
class DockerRoomLauncher:
    """Room runners as containers on the workers network, through the Docker provider.

    The script, the token and the credential file are written under the artifact root
    (`rooms/<room>/`), which the service shares with every container it creates, and
    mounted read-only from there; CLAUDE_CONFIG_DIR is a tmpfs. The runner reaches the
    model API and the package index through the egress proxy, and Hades at `api_url`
    through the same proxy, which therefore has to permit the API's host and port."""

    provider: DockerProvider
    script_path: str | None = None
    harnesses: HarnessRegistry = field(default_factory=default_registry)
    name: str = "docker"

    def _root(self, room_id: str) -> Path:
        return Path(self.provider.config.artifact_root) / "rooms" / object_name(room_id)

    def _write(self, launch: RoomRunnerLaunch, credential: CredentialSpec) -> None:
        source = self.provider._credential_source(launch.harness)
        if source is None or not source.path:
            raise RoomRunnerError(f"no {launch.harness} credential is configured")
        root = self._root(launch.room_id)
        shutil.rmtree(root, ignore_errors=True)
        for leaf, mode in (("runner/tools", 0o755), ("credential", 0o700), ("token", 0o700)):
            (root / leaf).mkdir(parents=True, exist_ok=True)
            (root / leaf).chmod(mode)
        script = root / "runner" / RUNNER_SCRIPT
        script.write_text(runner_script(self.script_path), encoding="utf-8")
        script.chmod(0o444)
        for file in credential_files(credential):
            origin = credential.source_path(source.path, file)
            if not origin.is_file():
                raise RoomRunnerError(f"the {launch.harness} credential has no {file}")
            target = root / "credential" / file
            target.write_bytes(origin.read_bytes())
            target.chmod(0o400)
        token = root / "token" / "token"
        token.write_text(launch.token, encoding="utf-8")
        token.chmod(0o400)

    def body(
        self, launch: RoomRunnerLaunch, credential: CredentialSpec, *, image: str
    ) -> dict[str, Any]:
        p = self.provider
        spec = replace(_launch_spec(launch), image=image)
        network, env = p._network_and_env(
            replace(
                spec,
                policy={"network": {"egress_allowlist": list(launch.egress_hosts)}},
            )
        )
        host_config = p._hardened(spec, network=network)
        host_config["Memory"] = ROOM_LIMITS.memory_bytes
        host_config["MemorySwap"] = ROOM_LIMITS.memory_bytes
        host_config["NanoCpus"] = int(ROOM_LIMITS.cpus * 1_000_000_000)
        host_config["Tmpfs"][RUNNER_CONFIG_DIR] = (
            f"rw,nosuid,nodev,size={CONFIG_VOLUME_BYTES},uid=1000,gid=1000,mode=0700"
        )
        relative = f"rooms/{object_name(launch.room_id)}"
        host_config["Mounts"] = [
            p._volume_mount(f"{relative}/runner", RUNNER_DIR, read_only=True),
            p._volume_mount(f"{relative}/credential", credential.mount_target, read_only=True),
            p._volume_mount(f"{relative}/token", RUNNER_TOKEN_DIR, read_only=True),
        ]
        environment = {
            **{k: v for k, v in env.items() if k.startswith(("HTTP", "http", "NO_", "no_"))},
            **runner_env(launch, credential, egress_hosts=launch.egress_hosts),
        }
        return {
            "Image": image,
            "Cmd": runner_command(),
            "User": "1000:1000",
            "WorkingDir": RUNNER_DIR,
            "Env": [f"{k}={v}" for k, v in sorted(environment.items())],
            "Labels": {
                LABEL_ROOM: launch.room_id,
                k8sspec.LABEL_ROLE: ROLE_ROOM_RUNNER,
                k8sspec.LABEL_OWNER: launch.owner,
            },
            "Tty": False,
            "HostConfig": host_config,
        }

    async def launch(self, launch: RoomRunnerLaunch) -> str:
        p = self.provider
        credential = credential_spec(self.harnesses, launch.harness)
        await p._ensure_network()
        image = await p._resolve_image(_launch_spec(launch))
        await asyncio.to_thread(self._write, launch, credential)
        name = object_name(launch.room_id)
        container_id = ""
        try:
            container_id = await p._call(
                p.client.create_container, name, self.body(launch, credential, image=image)
            )
            await p._call(p.client.start_container, container_id)
        except Exception as exc:
            if container_id:
                with contextlib.suppress(Exception):
                    await p._call(p.client.remove_container, container_id, force=True)
            await asyncio.to_thread(shutil.rmtree, self._root(launch.room_id), True)
            raise RoomRunnerError(f"could not start the room runner: {exc}") from exc
        return handle_for(self.name, launch.room_id)

    async def stop(self, handle: str) -> None:
        p = self.provider
        _provider, name = room_of(handle)
        with contextlib.suppress(Exception):
            await p._call(p.client.remove_container, name, force=True)
        root = Path(p.config.artifact_root) / "rooms" / name
        await asyncio.to_thread(shutil.rmtree, root, True)


# ----- the test double ------------------------------------------------------------


@dataclass(slots=True)
class FakeRoomLauncher:
    """Records every launch and stop and runs nothing; a test drives the runner's side
    of the protocol itself with the token the launch carried."""

    name: str = "fake"
    launches: list[RoomRunnerLaunch] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    fail_with: str | None = None

    async def launch(self, launch: RoomRunnerLaunch) -> str:
        if self.fail_with:
            raise RoomRunnerError(self.fail_with)
        self.launches.append(launch)
        return f"{self.name}:{object_name(launch.room_id)}-{len(self.launches)}"

    async def stop(self, handle: str) -> None:
        self.stops.append(handle)

    @property
    def token(self) -> str:
        return self.launches[-1].token


__all__ = [
    "CLI_PATH",
    "LABEL_ROOM",
    "ROLE_ROOM_RUNNER",
    "ROOM_LIMITS",
    "RUNNER_SCRIPT",
    "DockerRoomLauncher",
    "FakeRoomLauncher",
    "KubernetesRoomLauncher",
    "api_rule",
    "credential_files",
    "credential_spec",
    "handle_for",
    "k8s_labels",
    "k8s_pod",
    "object_name",
    "room_of",
    "runner_command",
    "runner_env",
    "runner_script",
]
