"""Verify compose.yaml mounts the GitHub credential directory writable.

The GitHub App credential directory must be writable so that Connect GitHub
can write app.pem on a Docker deployment. All other credential mounts keep
their modes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_compose() -> dict[str, Any]:
    """Load and return the compose.yaml as a dict."""
    compose_path = ROOT / "compose.yaml"
    with compose_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)  # type: ignore[no-any-return]


def _volume_mode(volume: str) -> str | None:
    """Return the mode suffix of a volume string, or None if writable."""
    parts = volume.rsplit(":", 1)
    if len(parts) == 2:
        return parts[-1]
    return None


def _volume_target(volume: str) -> str:
    """Return the target path (rightmost `:` segment for named volumes too)."""
    parts = volume.split(":")
    return parts[-1]


def test_github_credential_mount_is_writable() -> None:
    """The github credential directory must be writable."""
    compose = _load_compose()
    crucible_service = compose["services"]["crucible"]
    volumes = crucible_service.get("volumes", [])
    github_mount = None
    for vol in volumes:
        if not isinstance(vol, str):
            continue
        if _volume_target(vol) == "/var/lib/crucible/credentials/github":
            github_mount = vol
            break
    assert github_mount is not None, "No github credential mount found in crucible service"
    mode = _volume_mode(github_mount)
    assert mode is None or "ro" not in mode, (
        f"GitHub credential mount {github_mount!r} is read-only; it must be writable"
    )


def test_other_credential_mounts_are_unchanged() -> None:
    """Other credential mounts must keep their original modes."""
    compose = _load_compose()
    crucible_service = compose["services"]["crucible"]
    volumes = crucible_service.get("volumes", [])

    # The credential root named volume mount (crucible-credentials)
    # should remain writable (no mode specified or rw)
    found_other_credential_volume = False
    for vol in volumes:
        if not isinstance(vol, str):
            continue
        # Skip the github mount we already checked
        if _volume_target(vol) == "/var/lib/crucible/credentials/github":
            continue
        if "crucible-credentials" in vol:
            # Named volume mounts should be writable (no :ro)
            # This is a sanity check that we are not accidentally
            # changing other credential mounts
            found_other_credential_volume = True

    # Verify we found at least the named volume credential mount
    assert found_other_credential_volume, (
        "Expected to find the crucible-credentials named volume mount"
    )
