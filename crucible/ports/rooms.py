"""The room runner launcher (hades #208, ADR 0031).

A provider that can run a room's runner: one Pod (Kubernetes) or one container (Docker,
for `make up`) from the worker image, with no checkout and no repository, running
`uv run --no-project --with claude-agent-sdk python tools/room_runner.py`. The harness
credential reaches it the way it reaches an attempt of that harness; the room-scoped
token Hades minted for this launch reaches it as a file, never in the environment."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# The runner's own command, inside the worker image, from the runner directory.
RUNNER_COMMAND: tuple[str, ...] = (
    "uv",
    "run",
    "--no-project",
    "--with",
    "claude-agent-sdk",
    "python",
    "tools/room_runner.py",
)
# Where the runner directory (tools/room_runner.py) is mounted, read-only, and the
# runner's working directory.
RUNNER_DIR = "/crucible/room"
# The room token's file, on a read-only mount of its own.
RUNNER_TOKEN_DIR = "/crucible/room-token"
RUNNER_TOKEN_FILE = f"{RUNNER_TOKEN_DIR}/token"
# CLAUDE_CONFIG_DIR, on a writable per-room volume: an emptyDir (Kubernetes) or a
# tmpfs (Docker), so the harness session dies with the runner. Hades replays the
# transcript into the next one; nothing depends on the session surviving.
RUNNER_CONFIG_DIR = "/crucible/room-config"
# The harness's working directory, fixed per room; the session directory is named from it.
RUNNER_WORK_ROOT = "/home/worker/rooms"


@dataclass(frozen=True, slots=True)
class RoomRunnerLaunch:
    """What one launch needs. `token` is the room-scoped token, shown here once and
    stored nowhere; `api_url` is how the runner reaches Hades from where it runs."""

    room_id: str
    harness: str
    model: str
    image: str
    api_url: str
    idle_timeout_seconds: int
    owner: str = "crucible"
    egress_hosts: tuple[str, ...] = ()
    token: str = field(default="", repr=False)

    @property
    def cwd(self) -> str:
        return f"{RUNNER_WORK_ROOT}/{self.room_id.lower()}"


class RoomRunnerError(Exception):
    """The provider could not start or stop a room runner."""


class RoomRunnerLauncher(Protocol):
    name: str

    async def launch(self, launch: RoomRunnerLaunch) -> str:
        """Start the runner and return its handle (`<provider>:<object name>`)."""
        ...

    async def stop(self, handle: str) -> None:
        """Remove the runner and everything its launch created. A runner already gone
        is the state asked for."""
        ...


__all__ = [
    "RUNNER_COMMAND",
    "RUNNER_CONFIG_DIR",
    "RUNNER_DIR",
    "RUNNER_TOKEN_DIR",
    "RUNNER_TOKEN_FILE",
    "RUNNER_WORK_ROOT",
    "RoomRunnerError",
    "RoomRunnerLaunch",
    "RoomRunnerLauncher",
]
