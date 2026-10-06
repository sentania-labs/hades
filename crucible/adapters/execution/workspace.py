"""Workspace layout for the Docker provider (08).

Crucible's own image carries no git, and 08 already says the collector, not the
Crucible process, is what reads a worker's tree. C3 applies the same rule to
preparation: every git command runs in a throwaway container from the worker image
(`scripts.preparer_script`), and what Crucible writes here is plain files it owns.

What the worker ends up with is a checkout whose origin resolves nowhere, an author
identity from policy, no credential helper, the shims listed in `.git/info/exclude`,
a read-only identity bundle, and an empty report directory.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from crucible.ports.execution import ProviderError

# The origin URL a worker sees. It resolves nowhere, so a push cannot even start (S4).
ORIGIN_PLACEHOLDER = "crucible-no-remote://this-checkout-cannot-push"
SHIM_NAMES: tuple[str, ...] = ("AGENTS.md",)
HARNESS_PRIVATE_ENTRIES: tuple[str, ...] = ("/.hermes/", "/.qwen/")
EXCLUDE_ENTRIES: tuple[str, ...] = (
    "# Written by Crucible at prepare; these are shims and harness state, not work (06, 11).",
    "/AGENTS.md",
    "/.crucible/",
    *HARNESS_PRIVATE_ENTRIES,
)


def harness_private_path(path: str) -> bool:
    """Whether a repository-relative path belongs to local harness runtime state."""
    return any(
        path == entry.strip("/") or path.startswith(entry.lstrip("/"))
        for entry in HARNESS_PRIVATE_ENTRIES
    )


class WorkspaceError(Exception):
    """Preparation failed. The attempt is an `environment` failure (16)."""


def require_checkout_url(url: str, credential_host: str) -> None:
    """ADR 0019: a checkout token is only ever handed to git for an https URL on the one
    configured credential host, because the helper answers for nothing else and a clone
    from anywhere else would fail later with a less useful message. Refuses otherwise.

    The comparison is the helper's own: git hands the helper the URL's host exactly as
    written, with the port when the URL names one, so `GitHub.com` or `github.com:443`
    would get no answer from a helper bound to `github.com`, and is refused here."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    if parts.scheme != "https" or host != credential_host:
        raise ProviderError(
            f"refusing to prepare: {url} is registered as private, and a private "
            f"repository is cloned with the GitHub App's token only over https from "
            f"{credential_host} (github.credential_host)"
        )
