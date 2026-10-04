"""The publisher port (23 "Publication", ADR 0007).

The publisher is the one container that holds a GitHub credential, and it holds it on a
tmpfs for the length of one push. It never sees the worker's tree or the worker's `.git`
directory: the branch bundle the collector produced is the only carrier of the worker's
commits, and `git bundle verify` has already run over it (08, C3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

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
    timeout_seconds: int = 600


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """What the publisher container did. No token, no remote url with a credential."""

    pushed: bool
    head_sha: str
    step: str
    detail: str = ""
    exit_code: int = 0
    remote_head_before: str = ""
    log_tail: str = ""


@dataclass(frozen=True, slots=True)
class MergeMainRequest:
    """A publisher-side attempt to merge the current base into the remote work tip."""

    task_id: str
    attempt_id: str
    repository_url: str
    work_branch: str
    base_ref: str
    expected_head: str
    author_name: str = "Crucible"
    author_email: str = "crucible-worker@users.noreply.github.com"
    timeout_seconds: int = 600


@dataclass(frozen=True, slots=True)
class MergeMainOutcome:
    merged: bool
    head_sha: str = ""
    conflicting_files: tuple[str, ...] = ()
    detail: str = ""


class Publisher(Protocol):
    """Push one verified head to one repository and report what happened."""

    async def push(self, request: PublishRequest, token: InstallationToken) -> PublishOutcome: ...

    async def cleanup(self, attempt_ids: Sequence[str]) -> int: ...
