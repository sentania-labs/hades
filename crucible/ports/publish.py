"""The publisher port (23 "Publication", ADR 0007).

The publisher is the one container that holds a GitHub credential, and it holds it on a
tmpfs for the length of one push. It never sees the worker's tree or the worker's `.git`
directory: the branch bundle the collector produced is the only carrier of the worker's
commits, and `git bundle verify` has already run over it (08, C3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from crucible.ports.github import InstallationToken


class PublishError(Exception):
    """The publisher could not deliver. Carries the step and a response class (23)."""

    def __init__(self, step: str, detail: str, *, response_class: str = "") -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail
        self.response_class = response_class


@dataclass(frozen=True, slots=True)
class PublishRequest:
    attempt_id: str
    task_id: str
    owner: str
    repository_url: str
    work_branch: str
    base_ref: str
    expected_head: str
    bundle_path: str
    bundle_sha256: str
    image: str
    policy: Mapping[str, object] = field(default_factory=dict)
    author_name: str = "crucible-worker"
    author_email: str = "crucible-worker@users.noreply.github.com"
    owned_remote_heads: tuple[str, ...] = ()
    timeout_seconds: int = 600


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """What the publisher container did. No token, no remote url with a credential."""

    pushed: bool
    head_sha: str
    step: str
    detail: str = ""
    # hades #447: the publisher's reading of the branch's new migrations (schema.json).
    schema_changes: dict[str, Any] | None = None
    exit_code: int = 0
    remote_head_before: str = ""
    log_tail: str = ""


@dataclass(frozen=True, slots=True)
class MergeMainRequest:
    """A publisher-side attempt to merge the current base into the remote work tip.

    hades #411: the work branch is fetched from the remote, never from a bundle, and it
    must still be at `expected_head`, the head Crucible pushed or adopted. `base_ref` is
    merged with no conflict resolution. A clean merge is committed as Crucible and pushed
    with `--force-with-lease` against `expected_head`; a conflict pushes nothing."""

    task_id: str
    attempt_id: str
    owner: str
    repository_url: str
    work_branch: str
    base_ref: str
    expected_head: str
    image: str = ""
    policy: Mapping[str, object] = field(default_factory=dict)
    # The workspace of the attempt the merge continues (`k8s://...` on Kubernetes), which
    # names the provider whose publisher runs it.
    workspace_path: str = ""
    author_name: str = "Crucible"
    author_email: str = "crucible-worker@users.noreply.github.com"
    timeout_seconds: int = 600


@dataclass(frozen=True, slots=True)
class MergeMainOutcome:
    """What a merge-main run did. `merged` means the merge commit was pushed and
    `head_sha` is the new remote head. `conflicting_files` is set when git stopped on
    conflicts, and then the remote branch was left untouched."""

    merged: bool
    head_sha: str = ""
    conflicting_files: tuple[str, ...] = ()
    # hades #447: the publisher's reading of the branch's new migrations (schema.json).
    schema_changes: dict[str, Any] | None = None
    detail: str = ""
    step: str = ""
    exit_code: int = 0


class Publisher(Protocol):
    """Push one verified head to one repository and report what happened."""

    async def push(self, request: PublishRequest, token: InstallationToken) -> PublishOutcome: ...

    async def merge_main(
        self, request: MergeMainRequest, token: InstallationToken
    ) -> MergeMainOutcome: ...

    async def cleanup(self, attempt_ids: Sequence[str]) -> int: ...
