"""hades #137: the Docker preparer no longer writes the shared reference cache.

The refresh runs in a short-lived container of its own, the cache's one writer, before
the preparer; the preparer mounts the cache read-only, as the Kubernetes preparer does
beside its refresher Job (26, #55). A stub daemon records every create request, so the
mounts, the order and the cancel points are checked without a container.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution import scripts
from crucible.adapters.execution.docker import (
    ROLE_CACHE_REFRESHER,
    ROLE_PREPARER,
    DockerProvider,
)
from crucible.ports.execution import WORK_MOUNT, LaunchCancelledError, ProviderError
from crucible.ports.github import InstallationToken
from tests.unit.test_docker_provider import StubClient, config, spec
from tests.wait import async_wait_until

URL = "https://github.com/octo-lab/widgets"


class PreparingClient(StubClient):
    """The stub daemon, plus the preparer's own output files, plus what each container
    exited with and what reached its stdin."""

    def __init__(self, root: Path, *, exits: dict[str, int] | None = None) -> None:
        super().__init__()
        self.root = root
        self.exits = exits or {}
        self.started: list[str] = []
        self.stdin: list[tuple[str, bytes]] = []

    def role_of(self, container_id: str) -> str:
        index = int(container_id.removeprefix("container-")) - 1
        name = str(self.created[index]["name"])
        return name.removeprefix("crucible-").rsplit("-", 1)[0]

    def create_container(self, name: str, body: dict[str, Any]) -> str:
        if name.startswith(f"crucible-{ROLE_PREPARER}-"):
            attempt = name.removeprefix(f"crucible-{ROLE_PREPARER}-")
            output = self.root / "workspaces" / attempt / "output"
            output.mkdir(parents=True, exist_ok=True)
            (output / "prepared-head.txt").write_text("a" * 40 + "\n", encoding="utf-8")
            (output / "started-from.txt").write_text("main\n", encoding="utf-8")
        return super().create_container(name, body)

    def start_container(self, container_id: str) -> None:
        self.started.append(self.role_of(container_id))

    def wait_container(self, container_id: str, *, timeout: float) -> int:
        return self.exits.get(self.role_of(container_id), 0)

    def write_stdin(self, container_id: str, payload: bytes) -> None:
        self.stdin.append((self.role_of(container_id), payload))


def _provider(tmp_path: Path, client: StubClient, **overrides: Any) -> DockerProvider:
    return DockerProvider(replace(config(tmp_path), **overrides), client=client)  # type: ignore[arg-type]


def _created(client: StubClient, role: str) -> dict[str, Any]:
    return next(c for c in client.created if c["name"].startswith(f"crucible-{role}-"))


def _cache_mounts(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in body["HostConfig"]["Mounts"] if m["Target"] == scripts.CACHE_MOUNT]


def cache_name_of(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _token() -> InstallationToken:
    """A token-shaped value built at run time; nothing committed is one."""
    return InstallationToken(
        "ghs_" + "Q" * 36,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        repository="octo-lab/secret",
    )


# ----- AC1: the preparer's cache mount is read-only ------------------------------


async def test_the_preparer_mounts_the_cache_read_only(tmp_path: Path) -> None:
    client = PreparingClient(tmp_path)
    await _provider(tmp_path, client).prepare(replace(spec(), repository_url=URL))

    (cache,) = _cache_mounts(_created(client, ROLE_PREPARER)["body"])
    assert cache["ReadOnly"] is True
    assert cache["Source"] == f"{tmp_path}/cache"
    assert (tmp_path / "cache").is_dir()
    # The script only reads the mirror: no fetch, no mirror clone, no removal of it.
    preparer = _created(client, ROLE_PREPARER)["body"]["Cmd"][-1]
    assert "--reference /crucible/cache/" in preparer and "--dissociate" in preparer
    assert "fetch --prune" not in preparer and "clone --mirror" not in preparer
    assert 'rm -rf "/crucible/cache' not in preparer


# ----- AC2: a separate refresh container writes the cache first ------------------


async def test_a_refresher_of_its_own_writes_the_cache_before_the_preparer(
    tmp_path: Path,
) -> None:
    client = PreparingClient(tmp_path)
    await _provider(tmp_path, client).prepare(replace(spec(), repository_url=URL))

    assert [c["name"] for c in client.created] == [
        f"crucible-{ROLE_CACHE_REFRESHER}-{spec().attempt_id}",
        f"crucible-{ROLE_PREPARER}-{spec().attempt_id}",
    ]
    assert client.started == [ROLE_CACHE_REFRESHER, ROLE_PREPARER]
    # Finished and removed before the preparer was created: it is gone with it.
    assert client.removed == ["container-1", "container-2"]

    refresher = _created(client, ROLE_CACHE_REFRESHER)["body"]
    (cache,) = _cache_mounts(refresher)
    assert cache["ReadOnly"] is False
    # Only the cache: no workspace, no identity bundle, no credential, no origin.
    assert refresher["HostConfig"]["Mounts"] == [cache]
    assert refresher["Labels"]["crucible.role"] == ROLE_CACHE_REFRESHER
    assert refresher["Labels"]["crucible.attempt"] == spec().attempt_id
    script = refresher["Cmd"][-1]
    assert script == scripts.cache_refresh_script(url=URL, cache_name=cache_name_of(URL))
    assert "fetch --prune origin" in script and "clone --mirror" in script
    assert WORK_MOUNT not in script
    # The same hardened shape as every throwaway: no capabilities, read-only root.
    assert refresher["HostConfig"]["CapDrop"] == ["ALL"]
    assert refresher["HostConfig"]["ReadonlyRootfs"] is True
    assert refresher["User"] == "1000:1000"


async def test_the_refresher_shares_the_preparers_network_and_proxy(tmp_path: Path) -> None:
    """The refresher talks to the same remote the preparer clones from, through the
    same egress proxy, and nothing else of the preparer's."""
    client = PreparingClient(tmp_path)
    provider = _provider(tmp_path, client, egress_proxy="http://proxy:3128")
    await provider.prepare(replace(spec(), repository_url=URL))

    refresher = _created(client, ROLE_CACHE_REFRESHER)["body"]
    preparer = _created(client, ROLE_PREPARER)["body"]
    assert refresher["HostConfig"]["NetworkMode"] == preparer["HostConfig"]["NetworkMode"]
    assert refresher["HostConfig"]["NetworkMode"] == config(tmp_path).workers_network
    assert refresher["Env"] == preparer["Env"]
    assert "HTTPS_PROXY=http://proxy:3128" in refresher["Env"]


async def test_a_failed_refresh_is_logged_and_the_preparer_still_runs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A stale or absent cache costs time, never correctness: the preparer clones from
    the mirror as it was, or from the remote."""
    client = PreparingClient(tmp_path, exits={ROLE_CACHE_REFRESHER: 1})
    with caplog.at_level(logging.WARNING, logger="crucible.provider.docker"):
        ws = await _provider(tmp_path, client).prepare(replace(spec(), repository_url=URL))

    assert ws.attempt_id == spec().attempt_id
    assert client.started == [ROLE_CACHE_REFRESHER, ROLE_PREPARER]
    assert any("cache refresh failed" in r.getMessage() for r in caplog.records)
    (cache,) = _cache_mounts(_created(client, ROLE_PREPARER)["body"])
    assert cache["ReadOnly"] is True


async def test_a_failed_preparer_still_fails_the_prepare(tmp_path: Path) -> None:
    client = PreparingClient(tmp_path, exits={ROLE_PREPARER: 3})
    with pytest.raises(ProviderError, match="preparer container could not build"):
        await _provider(tmp_path, client).prepare(replace(spec(), repository_url=URL))


async def test_no_refresher_without_the_cache(tmp_path: Path) -> None:
    """With the cache off, or a repository that lives in the artifact root, there is
    nothing to refresh and the preparer is the only container."""
    client = PreparingClient(tmp_path)
    await _provider(tmp_path, client, use_reference_cache=False).prepare(
        replace(spec(), repository_url=URL)
    )
    assert [c["name"].split("-")[1] for c in client.created] == [ROLE_PREPARER]
    assert _cache_mounts(client.created[0]["body"]) == []

    origin = tmp_path / "origins" / "example.git"
    origin.mkdir(parents=True)
    client = PreparingClient(tmp_path)
    await _provider(tmp_path, client).prepare(replace(spec(), repository_url=str(origin)))
    assert [c["name"].split("-")[1] for c in client.created] == [ROLE_PREPARER]
    assert _cache_mounts(client.created[0]["body"]) == []
    assert client.created[0]["body"]["HostConfig"]["NetworkMode"] == "none"


# ----- AC3: a cancel before or during the refresh (hades #189) -------------------


async def test_a_cancel_before_the_refresh_creates_nothing(tmp_path: Path) -> None:
    client = PreparingClient(tmp_path)

    async def cancelled() -> bool:
        return True

    with pytest.raises(LaunchCancelledError, match="before the preparer started"):
        await _provider(tmp_path, client).prepare(
            replace(spec(), repository_url=URL), cancelled=cancelled
        )
    assert client.created == []


async def test_a_cancel_between_the_look_and_the_refresher_creates_nothing(
    tmp_path: Path,
) -> None:
    """The refresher's own create asks once more, after the image resolution it awaits."""
    client = PreparingClient(tmp_path)
    answers = iter([False, True])

    async def cancelled() -> bool:
        return next(answers, True)

    with pytest.raises(LaunchCancelledError, match=f"before the {ROLE_CACHE_REFRESHER} started"):
        await _provider(tmp_path, client).prepare(
            replace(spec(), repository_url=URL), cancelled=cancelled
        )
    assert client.created == []


class FetchingClient(PreparingClient):
    """A refresher that fetches until it is force-removed, as one against a git host
    that never answers would."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fetching = threading.Event()
        self.gone = threading.Event()
        self.forced: list[bool] = []

    def start_container(self, container_id: str) -> None:
        super().start_container(container_id)
        if self.role_of(container_id) == ROLE_CACHE_REFRESHER:
            self.fetching.set()

    def wait_container(self, container_id: str, *, timeout: float) -> int:
        if self.role_of(container_id) != ROLE_CACHE_REFRESHER:
            return 0
        if not self.gone.wait(timeout):
            raise TimeoutError("timed out")
        return 137

    def remove_container(self, container_id: str, *, force: bool = True) -> None:
        self.forced.append(force)
        super().remove_container(container_id, force=force)
        self.gone.set()


async def test_a_cancel_during_the_refresh_removes_it_and_creates_no_preparer(
    tmp_path: Path,
) -> None:
    client = FetchingClient(tmp_path)
    provider = _provider(tmp_path, client, collector_timeout_seconds=30)
    provider.cancel_poll_seconds = 0.2
    flag = asyncio.Event()
    asked: list[float] = []

    async def cancelled() -> bool:
        asked.append(time.monotonic())
        return flag.is_set()

    async def cancel_mid_fetch() -> float:
        await asyncio.to_thread(client.fetching.wait, 5)
        await async_wait_until(
            lambda: len(asked) >= 3,
            timeout=5,
            describe="cache refresher to poll cancellation three times",
        )
        flag.set()
        return time.monotonic()

    cancelling = asyncio.create_task(cancel_mid_fetch())
    with pytest.raises(LaunchCancelledError, match=f"while the {ROLE_CACHE_REFRESHER} ran"):
        await provider.prepare(replace(spec(), repository_url=URL), cancelled=cancelled)
    settled = time.monotonic() - await cancelling

    assert len(asked) >= 3, asked
    assert settled <= provider.cancel_poll_seconds + 0.15, settled
    assert [c["name"].split("-")[1] for c in client.created] == ["cache"]
    assert client.started == [ROLE_CACHE_REFRESHER]
    assert client.removed == ["container-1"] and client.forced == [True]


# ----- ADR 0019: the private repository's token --------------------------------


async def test_a_private_repository_hands_the_token_to_the_refresher_and_the_preparer(
    tmp_path: Path,
) -> None:
    client = PreparingClient(tmp_path)
    token = _token()
    launch = replace(spec(), repository_url="https://github.com/octo-lab/secret")
    await _provider(tmp_path, client).prepare(launch, checkout_token=token)

    assert client.stdin == [
        (ROLE_CACHE_REFRESHER, token.reveal().encode("utf-8")),
        (ROLE_PREPARER, token.reveal().encode("utf-8")),
    ]
    for role in (ROLE_CACHE_REFRESHER, ROLE_PREPARER):
        body = _created(client, role)["body"]
        tmpfs = body["HostConfig"]["Tmpfs"][scripts.TOKEN_MOUNT]
        assert "noexec" in tmpfs and "mode=0700" in tmpfs
        assert body["OpenStdin"] is True and body["StdinOnce"] is True
        script = body["Cmd"][-1]
        assert 'cat > "$CRUCIBLE_TOKEN_FILE"' in script
        assert "trap drop_checkout_token EXIT" in script
        assert token.reveal() not in script
    refresher = _created(client, ROLE_CACHE_REFRESHER)["body"]["Cmd"][-1]
    assert refresher == scripts.cache_refresh_script(
        url=launch.repository_url or "",
        cache_name=cache_name_of(launch.repository_url or ""),
        checkout_token="stdin",
        credential_host=config(tmp_path).credential_host,
    )


async def test_a_public_repository_refreshes_with_no_credential(tmp_path: Path) -> None:
    client = PreparingClient(tmp_path)
    await _provider(tmp_path, client).prepare(replace(spec(), repository_url=URL))

    assert client.stdin == []
    refresher = _created(client, ROLE_CACHE_REFRESHER)["body"]
    assert scripts.TOKEN_MOUNT not in refresher["HostConfig"]["Tmpfs"]
    assert "OpenStdin" not in refresher
    assert "cred-helper" not in refresher["Cmd"][-1]
