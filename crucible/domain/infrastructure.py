"""Failures of the model transport or container runtime, outside worker control."""

from __future__ import annotations

from dataclasses import dataclass

from crucible.domain.exit_class import ExitClass
from crucible.domain.time import parse_rfc3339

START_FAILURES = frozenset(
    {
        "StartError",
        "RunContainerError",
        "ContainerCannotRun",
        "InvalidImageName",
        "ErrImageNeverPull",
        "ImageInspectError",
        "PostStartHookError",
        "CreateContainerError",
        "CreateContainerConfigError",
        "ImagePullBackOff",
        "ErrImagePull",
    }
)


@dataclass(frozen=True)
class Interruption:
    message: str
    capacity: bool = False
    quota: bool = False
    environment: bool = False

    @property
    def exit_class(self) -> ExitClass:
        if self.environment:
            return ExitClass.ENVIRONMENT
        return ExitClass.QUOTA_EXHAUSTED if self.quota else ExitClass.INFRASTRUCTURE


def runtime_seconds(started: str | None, finished: str | None) -> float | None:
    """Container runtime, excluding scheduling and collection delays."""
    if not started or not finished:
        return None
    try:
        elapsed = (parse_rfc3339(finished) - parse_rfc3339(started)).total_seconds()
    except (ValueError, TypeError):
        return None
    return elapsed if elapsed >= 0 else None
