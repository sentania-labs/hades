"""The publisher on Kubernetes (23 "Publication", 26, ADR 0007).

The same port, the same script and the same outcome rules as the Docker publisher
(`publisher.py`); only the carriers differ, because a cluster has no stdin to write a
token into and no host directory to stage a bundle in:

- one Job per push, role `publisher`, with its own NetworkPolicy: the git host and
  nothing else, resolved and pinned into the Pod's `hostAliases` (26, hades #191);
- the installation token in a Secret created for this push alone, mounted as a Secret
  volume (memory-backed, read-only) at the path the script reads, never in an env var,
  an argument, the Job, or a log, and deleted as soon as the Job's Pod is gone, on
  every path;
- the branch bundle read straight off the attempt's workspace claim, from where the
  collector left it, as a single-file read-only mount: the Pod never sees the worker's
  checkout, its `.git`, the diff or the report (08). The script hashes it against the
  collector's seal and runs `git bundle verify` again before anything reaches a remote;
- the script's output files written to the claim's `publish` leaf and read back through
  the provider's reader Pod over exec, never through a Pod log (12).

`cleanup` removes whatever a push of the named attempts left: the Job, its Pod, the
NetworkPolicy and the token Secret.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from crucible.adapters.execution import k8sspec, scripts
from crucible.adapters.execution import kubernetes as k8s
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.k8sspec import EgressPlan, Limits, Mount, SpecError
from crucible.adapters.execution.publisher import (
    MAX_PUBLISHER_SECONDS,
    MERGE_OUTCOME_FILES,
    OUTCOME_FILES,
    merge_outcome_from_files,
    outcome_from_files,
    request_spec,
)
from crucible.domain.secrets import redact
from crucible.ports.execution import WORK_MOUNT, LaunchSpec, ProviderError
from crucible.ports.github import InstallationToken
from crucible.ports.publish import (
    MergeMainOutcome,
    MergeMainRequest,
    Publisher,
    PublishOutcome,
    PublishRequest,
)

log = logging.getLogger("crucible.publisher")

TOKEN_KEY = "token"
# The claim leaf the script writes its outcome files to. It holds the step, the heads
# and git's own messages, never the token (the script reads that from its mount only).
PUBLISH_LEAF = scripts.PUBLISH_LEAF
# The one leaf of a workspace claim a bundle may be published from: where the collector
# writes it (`scripts.collector_script`) and where `build_plan` points.
BUNDLE_LEAF = scripts.PUBLISH_BUNDLE_LEAF
# How much of one outcome file is read back. The outcome keeps a few kilobytes of each
# (`OUTCOME_FILES`); this only bounds the read of a file git wrote into.
OUTCOME_READ_LIMIT = 256 * 1024


@dataclass(frozen=True, slots=True)
class KubernetesPublisherConfig:
    credential_host: str = "github.com"
    timeout_seconds: int = MAX_PUBLISHER_SECONDS


def token_secret_name(attempt_id: str) -> str:
    return k8sspec.object_name("publish-token", attempt_id)


def job_name(attempt_id: str) -> str:
    return k8sspec.object_name(k8s.OBJECT_PREFIX[k8sspec.ROLE_PUBLISHER], attempt_id)


def policy_name(attempt_id: str) -> str:
    return k8sspec.object_name(f"np-{k8sspec.ROLE_PUBLISHER}", attempt_id)


def bundle_leaf(bundle_path: str, *, namespace: str, attempt_id: str) -> str | None:
    """The claim leaf of `bundle_path`, when it is this attempt's collected bundle.

    `k8s://<namespace>/ws-<attempt>/output/work_branch.bundle` is the only answer: a path
    into another attempt's claim, another namespace, or any other leaf is refused rather
    than mounted, because the bundle is the one carrier of the worker's commits (23)."""
    expected = f"k8s://{namespace}/{k8sspec.object_name('ws', attempt_id)}/{BUNDLE_LEAF}"
    return BUNDLE_LEAF if bundle_path == expected else None


class KubernetesPublisher:
    """The `Publisher` port as a Job in the workers namespace, through the provider."""

    def __init__(
        self, provider: k8s.KubernetesProvider, config: KubernetesPublisherConfig | None = None
    ) -> None:
        self._provider = provider
        self.config = config or KubernetesPublisherConfig()

    def _plan(self) -> EgressPlan:
        """26: the publisher does the git traffic and nothing else. The credential host
        is added when it is not github.com, as it is for a private checkout (ADR 0019),
        because that is where the push goes."""
        plan = EgressPlan(hosts=("api.github.com", "github.com"))
        host, _, port = self.config.credential_host.lower().partition(":")
        if host in plan.hosts and port in ("", "443"):
            return plan
        if port in ("", "443"):
            return replace(plan, hosts=(*plan.hosts, host))
        return replace(plan, endpoints=(f"{host}:{port}",))

    async def push(self, request: PublishRequest, token: InstallationToken) -> PublishOutcome:
        """Run one publisher Job to completion and report what it did."""
        provider = self._provider
        spec = request_spec(request)
        leaf = bundle_leaf(
            request.bundle_path,
            namespace=provider.config.namespace,
            attempt_id=request.attempt_id,
        )
        if leaf is None:
            return _refused(
                "bundle-seal",
                f"the bundle path {request.bundle_path!r} is not this attempt's collected "
                "bundle on its workspace claim",
            )
        if not request.bundle_sha256:
            return _refused("bundle-seal", "the branch bundle has no recorded sha256 seal")
        if not request.image:
            return _refused("create", "the publish request names no image")
        # The image is the attempt's own resolved digest, admitted when the attempt
        # launched, or the operator's `github.publisher_image`; the Docker publisher
        # admits it on the same terms (`_create_policy(..., resolved=request.image)`).
        claim = k8sspec.object_name("ws", request.attempt_id)
        try:
            await provider._call(provider.client.get, "persistentvolumeclaims", claim)
        except KubernetesApiError as exc:
            if exc.status == 404:
                return _refused(
                    "bundle-seal",
                    f"the workspace claim {claim!r} that holds the branch bundle is gone",
                )
            return _refused("container", f"the workspace claim could not be read: {exc}")
        try:
            probe = await provider.ensure_ready()
        except Exception as exc:
            return _refused("namespace", f"the workers namespace could not be probed: {exc}")
        if not probe.passed:
            # 26: a namespace whose egress enforcement is not proven runs no Pod that
            # holds a credential, the publisher's included.
            return _refused("namespace", f"the workers namespace is not ready ({probe.detail})")

        limits = provider._limits(spec)
        refusal = await self._prepare_claim(spec, request, claim, limits)
        if refusal is not None:
            return refusal
        timeout = int(min(request.timeout_seconds, self.config.timeout_seconds))
        script = scripts.publisher_script(
            clone_url=request.repository_url,
            work_branch=request.work_branch,
            base_ref=request.base_ref,
            expected_head=request.expected_head,
            author_name=request.author_name,
            author_email=request.author_email,
            credential_host=self.config.credential_host,
            token_source="file",
            bundle_sha256=request.bundle_sha256,
            owned_remote_heads=request.owned_remote_heads,
            owner=request.owner,
        )
        exit_code, failed = await self._run_with_token(
            spec,
            request,
            token,
            script=script,
            mounts=[
                Mount(
                    "ws",
                    f"{scripts.BUNDLE_MOUNT}/work_branch.bundle",
                    read_only=True,
                    sub_path=leaf,
                ),
                Mount("ws", scripts.PUBLISH_MOUNT, sub_path=PUBLISH_LEAF),
                Mount("publish-token", scripts.TOKEN_MOUNT, read_only=True),
            ],
            limits=limits,
            timeout=timeout,
        )
        if failed is not None:
            return _refused("container", failed)
        if exit_code == k8s.JOB_TIMED_OUT:
            return PublishOutcome(
                pushed=False,
                head_sha="",
                step="timeout",
                detail=f"the publisher did not finish within {timeout}s",
                exit_code=-2,
            )
        try:
            files = await provider._read_files(
                spec,
                [f"{PUBLISH_LEAF}/{name}" for name in OUTCOME_FILES],
                limits,
                limit=OUTCOME_READ_LIMIT,
            )
        except (ProviderError, KubernetesApiError) as exc:
            if exit_code == 0:
                # The script exits 0 only after `git push` succeeded, so the push
                # happened; the caller confirms the remote head before it records one.
                return PublishOutcome(
                    pushed=True,
                    head_sha=request.expected_head,
                    step="done",
                    detail=redact(
                        f"the publisher exited 0; its outcome files could not be read: {exc}"
                    ),
                    exit_code=0,
                )
            return PublishOutcome(
                pushed=False,
                head_sha="",
                step="outcome",
                detail=redact(
                    f"the publisher exited {exit_code} and its outcome could not be read: {exc}"
                ),
                exit_code=exit_code,
            )

        return outcome_from_files(
            {name: _text(files.get(f"{PUBLISH_LEAF}/{name}")) for name in OUTCOME_FILES},
            exit_code,
        )

    async def _run_with_token(
        self,
        spec: LaunchSpec,
        request: PublishRequest | MergeMainRequest,
        token: InstallationToken,
        *,
        script: str,
        mounts: list[Mount],
        limits: Limits,
        timeout: int,
    ) -> tuple[int, str | None]:
        """The token in a Secret of its own, one publisher Job that mounts it, and the
        Secret deleted on every path. The Job's exit, or why it could not run."""
        provider = self._provider
        secret = token_secret_name(request.attempt_id)
        exit_code = k8s.JOB_API_ERROR
        try:
            # A Secret of this name can only be left by an earlier push of this attempt,
            # holding a token already expired or revoked; the create must not keep it.
            if not await provider._delete_token_secret(secret, what="publisher"):
                return exit_code, (
                    f"the token Secret {secret!r} left by an earlier push could not be removed"
                )
            await provider._call(
                provider.client.create,
                "secrets",
                k8sspec.secret(
                    name=secret,
                    namespace=provider.config.namespace,
                    object_labels=provider._labels(spec, k8sspec.ROLE_PUBLISHER),
                    data={TOKEN_KEY: token.reveal().encode("utf-8")},
                ),
            )
            exit_code = await provider._run_role_job(
                spec,
                role=k8sspec.ROLE_PUBLISHER,
                image=request.image,
                script=script,
                mounts=mounts,
                volumes=[provider._claim_volume(request.attempt_id), token_volume(secret)],
                limits=limits,
                timeout=timeout,
                plan=self._plan(),
                env={"HOME": "/home/worker", "CRUCIBLE_ATTEMPT_ID": request.attempt_id},
                tolerate_lingering_pod=True,
                # A full namespace is a wait for a slot, not a failed publication.
                wait_for_quota=True,
            )
        except (KubernetesApiError, SpecError, ProviderError) as exc:
            return exit_code, f"the publisher Job could not run: {exc}"
        finally:
            # The Job's Pod has ended by now (a Pod slow to be removed is terminated,
            # not running). Deleted on every path, a cancel included; a deletion that
            # fails twice is logged, the next push of this attempt deletes it first,
            # and the retention sweep removes it once the attempt is no longer live.
            if not await provider._delete_token_secret(secret, what="publisher"):
                log.warning(
                    "the publisher token Secret outlived its push",
                    extra={"attempt_id": request.attempt_id, "secret": secret},
                )
        if exit_code == k8s.JOB_API_ERROR:
            return exit_code, (
                "the publisher Job could not run: "
                f"{redact(provider.last_error.get(k8sspec.ROLE_PUBLISHER, ''))}"
            )
        return exit_code, None

    async def merge_main(
        self, request: MergeMainRequest, token: InstallationToken
    ) -> MergeMainOutcome:
        """hades #411: merge the base into the remote work branch tip in a publisher Job.

        The Job is the publisher's, with the same Secret, NetworkPolicy and outcome leaf
        of the attempt's workspace claim, and no bundle mounted: the remote tip is what
        is merged. A claim that is gone or not ready refuses the merge, which the
        caller hands to a worker correction."""
        provider = self._provider
        spec = request_spec(request)
        if not request.image:
            return _merge_refused("create", "the merge-main request names no image")
        claim = k8sspec.object_name("ws", request.attempt_id)
        try:
            await provider._call(provider.client.get, "persistentvolumeclaims", claim)
        except KubernetesApiError as exc:
            if exc.status == 404:
                return _merge_refused(
                    "claim", f"the workspace claim {claim!r} the merge would write to is gone"
                )
            return _merge_refused("container", f"the workspace claim could not be read: {exc}")
        try:
            probe = await provider.ensure_ready()
        except Exception as exc:
            return _merge_refused("namespace", f"the workers namespace could not be probed: {exc}")
        if not probe.passed:
            return _merge_refused(
                "namespace", f"the workers namespace is not ready ({probe.detail})"
            )
        limits = provider._limits(spec)
        refusal = await self._prepare_claim(spec, request, claim, limits)
        if refusal is not None:
            return _merge_refused(refusal.step, refusal.detail)
        timeout = int(min(request.timeout_seconds, self.config.timeout_seconds))
        script = scripts.merge_main_script(
            clone_url=request.repository_url,
            work_branch=request.work_branch,
            base_ref=request.base_ref,
            expected_head=request.expected_head,
            author_name=request.author_name,
            author_email=request.author_email,
            credential_host=self.config.credential_host,
            token_source="file",
        )
        exit_code, failed = await self._run_with_token(
            spec,
            request,
            token,
            script=script,
            mounts=[
                Mount("ws", scripts.PUBLISH_MOUNT, sub_path=PUBLISH_LEAF),
                Mount("publish-token", scripts.TOKEN_MOUNT, read_only=True),
            ],
            limits=limits,
            timeout=timeout,
        )
        if failed is not None:
            return _merge_refused("container", failed)
        if exit_code == k8s.JOB_TIMED_OUT:
            return MergeMainOutcome(
                merged=False,
                step="timeout",
                detail=f"the merge-main Job did not finish within {timeout}s",
                exit_code=-2,
            )
        try:
            files = await provider._read_files(
                spec,
                [f"{PUBLISH_LEAF}/{name}" for name in MERGE_OUTCOME_FILES],
                limits,
                limit=OUTCOME_READ_LIMIT,
            )
        except (ProviderError, KubernetesApiError) as exc:
            # Unlike a publication, a merge whose new head cannot be read back is not
            # recorded: the next poll sees the pushed head as Crucible's own push only
            # through its outcome, so this is reported as not merged.
            return _merge_refused(
                "outcome",
                f"the merge-main Job exited {exit_code}; its outcome is unreadable: {exc}",
            )
        return merge_outcome_from_files(
            {name: _text(files.get(f"{PUBLISH_LEAF}/{name}")) for name in MERGE_OUTCOME_FILES},
            exit_code,
        )

    async def _prepare_claim(
        self,
        spec: LaunchSpec,
        request: PublishRequest | MergeMainRequest,
        claim: str,
        limits: Limits,
    ) -> PublishOutcome | None:
        """Check the bundle and create the `publish` leaf before any Pod mounts either.

        Neither subPath is left for the kubelet to create when the publisher Pod starts: on
        an NFS claim a directory it makes as root may be unwritable by the worker uid, or
        refused, and the Pod then fails setup with the token already placed and nothing in
        its log. This Job runs as the worker uid in the preparer role, with the whole claim
        and no network, before the Secret exists. None when the claim is ready."""
        provider = self._provider
        try:
            code = await provider._run_role_job(
                spec,
                role=k8sspec.ROLE_PREPARER,
                image=request.image,
                script=scripts.publish_leaf_script(),
                mounts=[Mount("ws", WORK_MOUNT)],
                volumes=[provider._claim_volume(request.attempt_id)],
                limits=limits,
                # How long the Job that readies the claim may run, from its Pod Running.
                timeout=provider.config.role_timeout_seconds,
                plan=EgressPlan(),
                wait_for_quota=True,
            )
        except (KubernetesApiError, SpecError, ProviderError) as exc:
            return _refused(
                "publish-leaf",
                f"the Job that prepares the {PUBLISH_LEAF}/ leaf could not run: {exc}",
            )
        if code == 0:
            return None
        tail = provider.last_error.get(k8sspec.ROLE_PREPARER, "")
        if code == 7:
            return _refused(
                "bundle-seal",
                f"no branch bundle at {BUNDLE_LEAF} on the workspace claim {claim!r}",
            )
        if code == 8:
            return _refused(
                "publish-leaf",
                f"the {PUBLISH_LEAF}/ leaf of the workspace claim {claim!r} is not a directory "
                f"the worker uid owns and can write, as it does output/: {tail}",
            )
        return _refused(
            "publish-leaf",
            f"the Job that prepares the {PUBLISH_LEAF}/ leaf of the workspace claim {claim!r} "
            f"exited {code}: {tail}",
        )

    async def cleanup(self, attempt_ids: Sequence[str]) -> int:
        """Remove the Job, its Pod, the NetworkPolicy and the token Secret of each named
        attempt's push. What is already gone counts as removed."""
        provider = self._provider
        removed = 0
        for attempt_id in attempt_ids:
            job = job_name(attempt_id)
            gone = True
            for kind, name in (("jobs", job), ("networkpolicies", policy_name(attempt_id))):
                gone = await _delete(provider, kind, name) and gone
            with contextlib.suppress(KubernetesApiError):
                for pod in await provider._call(
                    provider.client.list_objects, "pods", label_selector=f"job-name={job}"
                ):
                    name = str((pod.get("metadata") or {}).get("name") or "")
                    if name:
                        gone = await _delete(provider, "pods", name) and gone
            gone = (
                await provider._delete_token_secret(token_secret_name(attempt_id), what="publisher")
                and gone
            )
            removed += int(gone)
        return removed


def token_volume(secret: str) -> dict[str, object]:
    """The per-push token as a Secret volume: one key, owner-read only. The kubelet keeps
    a Secret volume in memory, so the value never touches the node's disk (12, S10)."""
    return {
        "name": "publish-token",
        "secret": {
            "secretName": secret,
            "defaultMode": 0o400,
            "items": [{"key": TOKEN_KEY, "path": "token", "mode": 0o400}],
            "optional": False,
        },
    }


async def _delete(provider: k8s.KubernetesProvider, kind: str, name: str) -> bool:
    try:
        await provider._call(provider.client.delete, kind, name)
    except KubernetesApiError as exc:
        return exc.status == 404
    return True


def _text(raw: object) -> str:
    """An outcome file's text. Absent, unreadable and oversized all read as empty: the
    outcome then says what the script's own exit and the files it could read say."""
    if not isinstance(raw, bytes) or raw in (k8s._TRUNCATED, k8s._UNREADABLE):
        return ""
    return raw.decode("utf-8", "replace")


def _refused(step: str, detail: str) -> PublishOutcome:
    """Nothing was pushed, and nothing that holds a token was created."""
    return PublishOutcome(pushed=False, head_sha="", step=step, detail=redact(detail), exit_code=-1)


def _merge_refused(step: str, detail: str) -> MergeMainOutcome:
    """Nothing was merged or pushed."""
    return MergeMainOutcome(merged=False, step=step, detail=redact(detail), exit_code=-1)


class ByWorkspacePublisher:
    """A deployment with both providers: each attempt's bundle is published by the
    provider whose workspace holds it, which the bundle path names."""

    def __init__(self, docker: Publisher, kubernetes: Publisher) -> None:
        self._docker = docker
        self._kubernetes = kubernetes

    def _for(self, bundle_path: str) -> Publisher:
        return self._kubernetes if bundle_path.startswith("k8s://") else self._docker

    async def push(self, request: PublishRequest, token: InstallationToken) -> PublishOutcome:
        return await self._for(request.bundle_path).push(request, token)

    async def merge_main(
        self, request: MergeMainRequest, token: InstallationToken
    ) -> MergeMainOutcome:
        """hades #411: the merge writes to the workspace of the attempt it continues, so
        the provider that holds that workspace runs it, as for that attempt's push."""
        return await self._for(request.workspace_path).merge_main(request, token)

    async def cleanup(self, attempt_ids: Sequence[str]) -> int:
        return await self._docker.cleanup(attempt_ids) + await self._kubernetes.cleanup(attempt_ids)


__all__ = [
    "BUNDLE_LEAF",
    "PUBLISH_LEAF",
    "ByWorkspacePublisher",
    "KubernetesPublisher",
    "KubernetesPublisherConfig",
    "bundle_leaf",
    "job_name",
    "policy_name",
    "token_secret_name",
    "token_volume",
]
