"""The Kubernetes provider's short-role timeout, as an administered setting.

`kubernetes.timeouts` holds `role_timeout_seconds`: how long the short single-purpose
roles (the bundle verifier, the cleaner, and the Job that readies a claim for the
publisher) may run once their Pod is Running. Pulling the image and waiting for a node
are bounded by the launch timeout instead, so a slow pull no longer uses up the role's
time (the lab findings of 2026-09-29). It also holds `api_retry_seconds`, the transport
retry budget used before a worker starts. The settings file seeds the role timeout; the
retry budget defaults to 60 seconds. A saved row wins at the next launch.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SETTING_NAME = "kubernetes.timeouts"
DEFAULT_ROLE_TIMEOUT_SECONDS = 120
# A role that cannot start `git bundle verify` or an `rm` in ten seconds is not one a
# shorter bound would help; an hour is past anything these roles do.
MIN_ROLE_TIMEOUT_SECONDS = 10
MAX_ROLE_TIMEOUT_SECONDS = 3600
DEFAULT_API_RETRY_SECONDS = 60
MIN_API_RETRY_SECONDS = 1
MAX_API_RETRY_SECONDS = 600


def parse_role_timeouts(document: Mapping[str, Any]) -> dict[str, int]:
    """The setting's document, checked. Raises ValueError naming the field."""
    unknown = sorted(set(document) - {"role_timeout_seconds", "api_retry_seconds"})
    if unknown:
        raise ValueError(f"unknown fields: {unknown}")
    value = document.get("role_timeout_seconds")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("role_timeout_seconds must be a whole number of seconds")
    if not MIN_ROLE_TIMEOUT_SECONDS <= value <= MAX_ROLE_TIMEOUT_SECONDS:
        raise ValueError(
            f"role_timeout_seconds must be between {MIN_ROLE_TIMEOUT_SECONDS} and "
            f"{MAX_ROLE_TIMEOUT_SECONDS}"
        )
    checked = {"role_timeout_seconds": value}
    if "api_retry_seconds" in document:
        retry = document["api_retry_seconds"]
        if not isinstance(retry, int) or isinstance(retry, bool):
            raise ValueError("api_retry_seconds must be a whole number of seconds")
        if not MIN_API_RETRY_SECONDS <= retry <= MAX_API_RETRY_SECONDS:
            raise ValueError(
                f"api_retry_seconds must be between {MIN_API_RETRY_SECONDS} and "
                f"{MAX_API_RETRY_SECONDS}"
            )
        checked["api_retry_seconds"] = retry
    return checked


__all__ = [
    "DEFAULT_API_RETRY_SECONDS",
    "DEFAULT_ROLE_TIMEOUT_SECONDS",
    "MAX_API_RETRY_SECONDS",
    "MAX_ROLE_TIMEOUT_SECONDS",
    "MIN_API_RETRY_SECONDS",
    "MIN_ROLE_TIMEOUT_SECONDS",
    "SETTING_NAME",
    "parse_role_timeouts",
]
