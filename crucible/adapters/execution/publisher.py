"""The publisher container (23 "Publication", S10).

One hardened, throwaway container per publication. It gets the branch bundle read-only,
a tmpfs for the token, a tmpfs home to work in, an output directory, and the egress
network. It never gets the worker's checkout, the worker's `.git` directory, the
database, the socket, or any credential other than the one token, and that token arrives
on stdin and lives only on a tmpfs the container loses when it stops.

What it does inside: verify the bundle, fetch the work branch from it, assert the fetched
head is the collected head Crucible recorded, and push without force. It does not check
commit authors or trailers (operator decision, 2026-09-29): the reviewer sees the author
at collection, and the task record is the paper trail. Every API call after the push is
Crucible's own, so the container needs no API token beyond git's.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any

from crucible.adapters.execution import scripts
from crucible.adapters.execution.create_policy import (
    CreateRequestRefusedError,
)
from crucible.adapters.execution.create_policy import (
    check as check_create,
)
from crucible.adapters.execution.docker import (
    LABEL_ATTEMPT,
    LABEL_OWNER,
    LABEL_ROLE,
    LABEL_TASK,
    DockerProvider,
)
from crucible.adapters.execution.dockerapi import DockerApiError
from crucible.domain.secrets import redact
from crucible.ports.execution import LaunchSpec
from crucible.ports.github import InstallationToken
from crucible.ports.publish import (
    MergeMainOutcome,
    MergeMainRequest,
    PublishOutcome,
    PublishRequest,
)

log = logging.getLogger("crucible.publisher")

ROLE_PUBLISHER = "publisher"
# The script's exit for "the bundle is missing or no longer matches its seal", checked
# inside the container before any remote is contacted.
BUNDLE_SEAL_REFUSED = 7
# S10: a token is valid for an hour whatever the container does, so a publisher that
# outlives ten minutes is treated as failed rather than left to hold one.
MAX_PUBLISHER_SECONDS = 600


@dataclass(frozen=True, slots=True)
class PublisherConfig:
    """23 step 3: the egress network with an allowlist of `github.com` and
    `api.github.com` only. `network` defaults to the publisher's own, not the workers',
    because the workers' proxy permits every model endpoint a harness needs and a
    container holding a GitHub credential has no business reaching any of them."""

    network: str = "crucible-publish"
    egress_proxy: str | None = None
    no_proxy: str = "localhost,127.0.0.1"
    credential_host: str = "github.com"
    timeout_seconds: int = MAX_PUBLISHER_SECONDS
    token_tmpfs_bytes: int = 64 * 1024


class DockerPublisher:
    """The `Publisher` port on the same daemon, through the same socket proxy."""

    def __init__(self, provider: DockerProvider, config: PublisherConfig | None = None) -> None:
        self._provider = provider
        self.config = config or PublisherConfig()
        self._network_ready = False

    @property
    def _client(self) -> Any:
        return self._provider.client

    def _root(self, attempt_id: str) -> Path:
        return Path(self._provider.config.artifact_root) / "publish" / attempt_id

    async def _ensure_network(self) -> None:
        """The publisher's own internal network. `internal` means no default route: the
        only way out is the proxy this network is joined to, which is the point."""
        if self._network_ready or self.config.network in ("none", ""):
            return
        try:
            await asyncio.to_thread(self._client.inspect_network, self.config.network)
        except DockerApiError as exc:
            if exc.status != 404:
                raise
            await asyncio.to_thread(self._client.create_network, self.config.network, internal=True)
        self._network_ready = True

    async def push(self, request: PublishRequest, token: InstallationToken) -> PublishOutcome:
        """Run one publisher container to completion and report what it did."""
        await self._ensure_network()
        root = self._root(request.attempt_id)
        await asyncio.to_thread(shutil.rmtree, root, True)
        await asyncio.to_thread(self._stage, root, request.bundle_path, request.bundle_sha256)
        script = scripts.publisher_script(
            clone_url=request.repository_url,
            work_branch=request.work_branch,
            base_ref=request.base_ref,
            expected_head=request.expected_head,
            author_name=request.author_name,
            author_email=request.author_email,
            credential_host=self.config.credential_host,
            bundle_sha256=request.bundle_sha256,
            owned_remote_heads=request.owned_remote_heads,
            owner=request.owner,
        )
        env = {
            "HOME": "/home/worker",
            "CRUCIBLE_ATTEMPT_ID": request.attempt_id,
        }
        if self.config.egress_proxy:
            env.update(
                {
                    "HTTPS_PROXY": self.config.egress_proxy,
                    "https_proxy": self.config.egress_proxy,
                    "NO_PROXY": self.config.no_proxy,
                    "no_proxy": self.config.no_proxy,
                }
            )
        body = self._body(request, script=script, env=env)
        name = f"crucible-publisher-{request.attempt_id}"
        container_id = ""
        exit_code = -1
        try:
            try:
                # The image is the attempt's own resolved digest, which the policy
                # allowlist admits by tag; the provider resolves it the same way for
                # every throwaway container it creates.
                check_create(
                    body,
                    self._provider._create_policy(request_spec(request), resolved=request.image),
                )
            except CreateRequestRefusedError as exc:
                return PublishOutcome(
                    pushed=False,
                    head_sha="",
                    step="create",
                    detail=f"the create-request policy refused the publisher: {exc}",
                )
            container_id = await asyncio.to_thread(self._client.create_container, name, body)
            await asyncio.to_thread(self._client.start_container, container_id)
            # The value leaves memory here and nowhere else: stdin to a tmpfs (S10).
            await asyncio.to_thread(
                self._client.write_stdin, container_id, token.reveal().encode("utf-8")
            )
            exit_code = int(
                await asyncio.to_thread(
                    self._client.wait_container,
                    container_id,
                    timeout=float(min(request.timeout_seconds, self.config.timeout_seconds)),
                )
            )
        except DockerApiError as exc:
            return PublishOutcome(
                pushed=False,
                head_sha="",
                step="container",
                detail=f"the publisher container could not run: {exc}",
                exit_code=-1,
            )
        except (TimeoutError, OSError, HTTPException) as exc:
            if container_id:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._client.kill_container, container_id)
            return PublishOutcome(
                pushed=False,
                head_sha="",
                step="timeout",
                detail=(
                    f"the publisher did not finish within {self.config.timeout_seconds}s "
                    f"({type(exc).__name__})"
                ),
                exit_code=-2,
            )
        finally:
            if container_id:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._client.remove_container, container_id, force=True)
        return await asyncio.to_thread(self._read_outcome, root / "out", exit_code)

    async def merge_main(
        self, request: MergeMainRequest, token: InstallationToken
    ) -> MergeMainOutcome:
        """hades #411: one throwaway container that merges the base into the remote work
        branch tip with no conflict resolution, and pushes a clean merge with a lease.

        It is the publisher's container in every respect but its inputs: no bundle is
        mounted, because the remote tip is the fact being merged, and only an output
        directory of its own is."""
        await self._ensure_network()
        root = self._root(request.attempt_id) / MERGE_MAIN_LEAF
        await asyncio.to_thread(shutil.rmtree, root, True)
        await asyncio.to_thread(self._stage_output, root)
        script = scripts.merge_main_script(
            clone_url=request.repository_url,
            work_branch=request.work_branch,
            base_ref=request.base_ref,
            expected_head=request.expected_head,
            author_name=request.author_name,
            author_email=request.author_email,
            credential_host=self.config.credential_host,
        )
        env = {"HOME": "/home/worker", "CRUCIBLE_ATTEMPT_ID": request.attempt_id}
        if self.config.egress_proxy:
            env.update(
                {
                    "HTTPS_PROXY": self.config.egress_proxy,
                    "https_proxy": self.config.egress_proxy,
                    "NO_PROXY": self.config.no_proxy,
                    "no_proxy": self.config.no_proxy,
                }
            )
        body = self._body(
            request,
            script=script,
            env=env,
            mounts=[
                self._provider._volume_mount(
                    f"publish/{request.attempt_id}/{MERGE_MAIN_LEAF}/out",
                    scripts.PUBLISH_MOUNT,
                    read_only=False,
                )
            ],
        )
        name = f"crucible-merge-main-{request.attempt_id}"
        container_id = ""
        try:
            try:
                check_create(
                    body,
                    self._provider._create_policy(request_spec(request), resolved=request.image),
                )
            except CreateRequestRefusedError as exc:
                return MergeMainOutcome(
                    merged=False,
                    step="create",
                    detail=f"the create-request policy refused the merge-main container: {exc}",
                    exit_code=-1,
                )
            container_id = await asyncio.to_thread(self._client.create_container, name, body)
            await asyncio.to_thread(self._client.start_container, container_id)
            await asyncio.to_thread(
                self._client.write_stdin, container_id, token.reveal().encode("utf-8")
            )
            exit_code = int(
                await asyncio.to_thread(
                    self._client.wait_container,
                    container_id,
                    timeout=float(min(request.timeout_seconds, self.config.timeout_seconds)),
                )
            )
        except DockerApiError as exc:
            return MergeMainOutcome(
                merged=False,
                step="container",
                detail=f"the merge-main container could not run: {exc}",
                exit_code=-1,
            )
        except (TimeoutError, OSError, HTTPException) as exc:
            if container_id:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._client.kill_container, container_id)
            return MergeMainOutcome(
                merged=False,
                step="timeout",
                detail=(
                    f"the merge-main container did not finish within "
                    f"{self.config.timeout_seconds}s ({type(exc).__name__})"
                ),
                exit_code=-2,
            )
        finally:
            if container_id:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._client.remove_container, container_id, force=True)
        return await asyncio.to_thread(
            lambda: merge_outcome_from_files(
                {
                    name: _read(root / "out" / name, limit=limit)
                    for name, limit in MERGE_OUTCOME_FILES.items()
                },
                exit_code,
            )
        )

    def _stage_output(self, root: Path) -> None:
        directory = root / "out"
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(self._provider.config.workspace_dir_mode)
        root.chmod(self._provider.config.workspace_dir_mode)

    def _stage(self, root: Path, bundle_path: str, expected_sha256: str) -> None:
        """Copy the branch bundle into a directory of its own and make the output dir.

        The publisher gets the bundle and nothing else of the collector's output: the
        diff, the report copy, and the fresh tree are evidence the gates read, and a
        container holding a credential has no business seeing them (08, 23)."""
        source = Path(bundle_path)
        if not source.is_file():
            raise FileNotFoundError(f"no branch bundle at {bundle_path}")
        actual_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        if not expected_sha256 or actual_sha256 != expected_sha256:
            raise ValueError("the branch bundle no longer matches its sealed sha256")
        for leaf in ("bundle", "out"):
            directory = root / leaf
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(self._provider.config.workspace_dir_mode)
        target = root / "bundle" / "work_branch.bundle"
        shutil.copyfile(source, target)
        target.chmod(0o644)
        root.chmod(self._provider.config.workspace_dir_mode)

    def _body(
        self,
        request: PublishRequest | MergeMainRequest,
        *,
        script: str,
        env: dict[str, str],
        mounts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        spec = request_spec(request)
        host = self._provider._hardened(spec, network=self.config.network)
        host["Tmpfs"] = {
            **host["Tmpfs"],
            scripts.TOKEN_MOUNT: (
                f"rw,nosuid,nodev,noexec,size={self.config.token_tmpfs_bytes},"
                "mode=0700,uid=1000,gid=1000"
            ),
        }
        host["Mounts"] = (
            mounts
            if mounts is not None
            else [
                self._provider._volume_mount(
                    f"publish/{request.attempt_id}/bundle", scripts.BUNDLE_MOUNT, read_only=True
                ),
                self._provider._volume_mount(
                    f"publish/{request.attempt_id}/out", scripts.PUBLISH_MOUNT, read_only=False
                ),
            ]
        )
        return {
            "Image": request.image,
            "Cmd": ["sh", "-c", script],
            "User": "1000:1000",
            "WorkingDir": "/home/worker",
            "Env": [f"{k}={v}" for k, v in sorted(env.items())],
            "Labels": {
                LABEL_ATTEMPT: request.attempt_id,
                LABEL_TASK: request.task_id,
                LABEL_OWNER: request.owner,
                LABEL_ROLE: ROLE_PUBLISHER,
            },
            "Tty": False,
            # `docker run -i`: stdin stays open until the attach closes it, which is how
            # the token arrives without ever being on argv or in the environment.
            "OpenStdin": True,
            "StdinOnce": True,
            "AttachStdin": True,
            "HostConfig": host,
        }

    def _read_outcome(self, root: Path, exit_code: int) -> PublishOutcome:
        return outcome_from_files(
            {name: _read(root / name, limit=limit) for name, limit in OUTCOME_FILES.items()},
            exit_code,
        )

    async def cleanup(self, attempt_ids: Sequence[str]) -> int:
        removed = 0
        for attempt_id in attempt_ids:
            root = self._root(attempt_id)
            if root.exists():
                await asyncio.to_thread(shutil.rmtree, root, True)
                removed += 1
        return removed


def request_spec(request: PublishRequest | MergeMainRequest) -> LaunchSpec:
    """A LaunchSpec-shaped view of the publish request.

    The create-request policy and the hardened body are written against a launch spec,
    and the publisher is a launch with a different script. Building a full spec here
    would mean carrying a contract the publisher never reads, so this is the narrow
    stand-in, with the fields those two functions actually use."""
    return LaunchSpec(
        attempt_id=request.attempt_id,
        task_id=request.task_id,
        external_id="",
        owner=request.owner,
        harness="",
        model="",
        image=request.image,
        contract={},
        policy={str(k): v for k, v in request.policy.items()},
        env={},
        timeout_seconds=request.timeout_seconds,
        network="policy",
        role="publish",
        repository_url=request.repository_url,
    )


# What the publisher script leaves in its output directory, and how much of each file is
# read. Both providers read these same files and turn them into an outcome with the same
# function, so the rules for what a publication did cannot drift between them (23).
OUTCOME_FILES: dict[str, int] = {
    "step.txt": 4000,
    "bundle-head.txt": 4000,
    "error.txt": 4000,
    "push.txt": 4000,
    "remote-head-before.txt": 4000,
    "publisher.log": 8000,
    "schema.json": 131072,
}


def outcome_from_files(files: Mapping[str, str], exit_code: int) -> PublishOutcome:
    """The outcome of one publisher run, from the text of its output files.

    A file that is absent is the empty string. Each text is cut to the length in
    `OUTCOME_FILES` here, whatever the caller read, so a provider that read more (the
    Kubernetes reader takes whole files) records exactly what the Docker one does."""

    def text(name: str) -> str:
        return (files.get(name) or "")[: OUTCOME_FILES.get(name, 4000)].strip()

    step = text("step.txt") or "unknown"
    head = text("bundle-head.txt")
    detail = redact(text("error.txt"))
    pushed = text("push.txt") == "ok" and exit_code == 0
    schema_changes = None
    if text("schema.json"):
        with contextlib.suppress(json.JSONDecodeError):
            schema_changes = json.loads(text("schema.json"))
    return PublishOutcome(
        pushed=pushed,
        head_sha=head,
        schema_changes=schema_changes,
        step=step,
        detail=detail,
        exit_code=exit_code,
        remote_head_before=text("remote-head-before.txt"),
        # Crucible's own container output, redacted before it is recorded: git can be
        # made to print a header and a remote can answer with anything (12).
        log_tail=redact(text("publisher.log")[-8000:]),
    )


# What the merge-main script leaves in its output directory (hades #411).
MERGE_OUTCOME_FILES: dict[str, int] = {
    "step.txt": 4000,
    "error.txt": 4000,
    "push.txt": 4000,
    "merge-head.txt": 4000,
    "conflicts.txt": 64 * 1024,
    "remote-head-before.txt": 4000,
    "schema.json": 131072,
}
# Where a merge-main run's output lands beside the publication's, so neither reads the
# other's files back.
MERGE_MAIN_LEAF = "merge-main"


def merge_outcome_from_files(files: Mapping[str, str], exit_code: int) -> MergeMainOutcome:
    """The outcome of one merge-main run, from the text of its output files. Both
    providers read it with this function, as they read a publication's (23)."""

    def text(name: str) -> str:
        return (files.get(name) or "")[: MERGE_OUTCOME_FILES.get(name, 4000)].strip()

    merged = text("push.txt") == "ok" and exit_code == 0
    conflicts = tuple(line.strip() for line in text("conflicts.txt").splitlines() if line.strip())
    schema_changes = None
    if text("schema.json"):
        with contextlib.suppress(json.JSONDecodeError):
            schema_changes = json.loads(text("schema.json"))
    return MergeMainOutcome(
        merged=merged,
        head_sha=text("merge-head.txt") if merged else "",
        conflicting_files=conflicts if exit_code == scripts.MERGE_MAIN_CONFLICT else (),
        schema_changes=schema_changes,
        detail=redact(text("error.txt")),
        step=text("step.txt") or "unknown",
        exit_code=exit_code,
    )


def _read(path: Path, *, limit: int = 4000) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace").strip()
    except OSError:
        return ""
