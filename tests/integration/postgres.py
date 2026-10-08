"""Controller-owned Postgres lifecycle, registered by the suite-wide conftest."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

# The same digest compose.yaml pins, so the tier and the stack run one Postgres build.
POSTGRES_IMAGE = (
    "postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
)


def includes_integration(config: pytest.Config) -> bool:
    """Start only when a selected path could collect integration tests."""
    integration = Path(__file__).parent.resolve()
    for arg in config.args:
        path = (config.invocation_params.dir / arg.split("::", 1)[0]).resolve()
        if path == integration or path in integration.parents or integration in path.parents:
            return True
    return False


SERVER_URL = pytest.StashKey[str | None]()
SERVER_STOP = pytest.StashKey[Callable[[], None]]()
WORKER_SERVER_URL = "integration_postgres_url"


def lock_and_share(directory: Path, start: Callable[[], str]) -> str:
    """Publish a server URL once, only after startup succeeds, under a Linux file lock."""
    with (directory / "postgres.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            url_file = directory / "postgres.url"
            if not url_file.exists():
                url = start()
                pending = directory / "postgres.url.tmp"
                pending.write_text(url, encoding="utf-8")
                pending.replace(url_file)
            return url_file.read_text(encoding="utf-8")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def pytest_configure(config: pytest.Config) -> None:
    """The controller owns Postgres, so worker exit order cannot shorten its lifetime."""
    if hasattr(config, "workerinput"):
        config.stash[SERVER_URL] = config.workerinput.get(
            WORKER_SERVER_URL, os.environ.get("CRUCIBLE_TEST_DATABASE_URL")
        )
        return
    config.stash[SERVER_URL] = os.environ.get("CRUCIBLE_TEST_DATABASE_URL")
    if config.stash[SERVER_URL] or config.option.collectonly or not includes_integration(config):
        return

    import docker as _docker  # type: ignore[import-untyped]  # noqa: PLC0415
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    client = None
    try:
        client = _docker.from_env()
        client.ping()
    except Exception:
        # Preserve the tier's skip when Docker is unavailable.
        return
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()

    pg = PostgresContainer(POSTGRES_IMAGE, driver="psycopg")
    # Register before startup so partial startup failures also get cleaned up.
    config.stash[SERVER_STOP] = pg.stop

    def start() -> str:
        pg.start()
        return str(pg.get_connection_url())

    # With controller ownership, workers receive the URL over xdist's channel;
    # publication files need only live during controller configuration.
    with TemporaryDirectory(prefix="integration-postgres-") as directory:
        config.stash[SERVER_URL] = lock_and_share(Path(directory), start)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    node.workerinput[WORKER_SERVER_URL] = node.config.stash[SERVER_URL]


def pytest_unconfigure(config: pytest.Config) -> None:
    if not hasattr(config, "workerinput") and SERVER_STOP in config.stash:
        stop = config.stash[SERVER_STOP]
        del config.stash[SERVER_STOP]
        stop()
