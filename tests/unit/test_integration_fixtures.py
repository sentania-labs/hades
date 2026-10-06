"""Docker-free checks of integration server sharing and controller lifetime."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from textwrap import dedent
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest

from tests.conftest import cpu_time
from tests.integration import postgres as fixtures


def test_lock_and_share_starts_once(tmp_path: Path) -> None:
    ready = Barrier(2)
    start = Mock(return_value="postgresql+psycopg://localhost/test")

    def worker() -> str:
        ready.wait(timeout=5)
        return fixtures.lock_and_share(tmp_path, start)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        urls = [future.result(timeout=5) for future in futures]
    assert urls == [start.return_value, start.return_value]
    start.assert_called_once_with()


def test_failed_start_does_not_publish_url(tmp_path: Path) -> None:
    start = Mock(side_effect=[RuntimeError("startup failed"), "postgresql://localhost/test"])
    with pytest.raises(RuntimeError, match="startup failed"):
        fixtures.lock_and_share(tmp_path, start)
    assert not (tmp_path / "postgres.url").exists()
    assert fixtures.lock_and_share(tmp_path, start) == "postgresql://localhost/test"
    assert start.call_count == 2


@pytest.mark.parametrize("workers", [0, 2])
def test_controller_owns_container_until_shutdown(
    monkeypatch: pytest.MonkeyPatch, workers: int
) -> None:
    monkeypatch.delenv("CRUCIBLE_TEST_DATABASE_URL", raising=False)
    pg = Mock()
    pg.get_connection_url.return_value = "postgresql+psycopg://localhost/test"
    constructor = Mock(return_value=pg)
    monkeypatch.setattr("docker.from_env", MagicMock())
    monkeypatch.setattr("testcontainers.postgres.PostgresContainer", constructor)
    controller: Any = SimpleNamespace(
        stash=pytest.Stash(),
        option=SimpleNamespace(collectonly=False),
        args=["tests"],
        invocation_params=SimpleNamespace(dir=Path(__file__).resolve().parents[2]),
    )
    fixtures.pytest_configure(controller)
    for _ in range(workers):
        node = SimpleNamespace(config=controller, workerinput={})
        fixtures.pytest_configure_node(node)
        worker: Any = SimpleNamespace(stash=pytest.Stash(), workerinput=node.workerinput)
        fixtures.pytest_configure(worker)
        assert worker.stash[fixtures.SERVER_URL] == pg.get_connection_url.return_value
        fixtures.pytest_unconfigure(worker)
        pg.stop.assert_not_called()
    constructor.assert_called_once_with(fixtures.POSTGRES_IMAGE, driver="psycopg")
    pg.start.assert_called_once_with()
    fixtures.pytest_unconfigure(controller)
    fixtures.pytest_unconfigure(controller)
    pg.stop.assert_called_once_with()


def test_external_server_does_not_start_container(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://localhost/external"
    monkeypatch.setenv("CRUCIBLE_TEST_DATABASE_URL", url)
    constructor = Mock()
    monkeypatch.setattr("testcontainers.postgres.PostgresContainer", constructor)
    config: Any = SimpleNamespace(
        stash=pytest.Stash(),
        option=SimpleNamespace(collectonly=False),
        args=["tests"],
        invocation_params=SimpleNamespace(dir=Path(__file__).resolve().parents[2]),
    )
    fixtures.pytest_configure(config)
    assert config.stash[fixtures.SERVER_URL] == url
    fixtures.pytest_unconfigure(config)
    constructor.assert_not_called()


@pytest.mark.parametrize("external", [None, "postgresql+psycopg://localhost/external"])
def test_worker_missing_url_falls_back_without_starting_container(
    monkeypatch: pytest.MonkeyPatch, external: str | None
) -> None:
    if external:
        monkeypatch.setenv("CRUCIBLE_TEST_DATABASE_URL", external)
    else:
        monkeypatch.delenv("CRUCIBLE_TEST_DATABASE_URL", raising=False)
    constructor = Mock()
    monkeypatch.setattr("testcontainers.postgres.PostgresContainer", constructor)
    worker: Any = SimpleNamespace(stash=pytest.Stash(), workerinput={})
    fixtures.pytest_configure(worker)
    assert worker.stash[fixtures.SERVER_URL] == external
    fixtures.pytest_unconfigure(worker)
    constructor.assert_not_called()


@pytest.mark.parametrize("path", ["tests/unit", "tests/unit/test_integration_fixtures.py"])
def test_unit_selection_does_not_start_container(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.delenv("CRUCIBLE_TEST_DATABASE_URL", raising=False)
    constructor = Mock()
    monkeypatch.setattr("testcontainers.postgres.PostgresContainer", constructor)
    config: Any = SimpleNamespace(
        stash=pytest.Stash(),
        option=SimpleNamespace(collectonly=False),
        args=[path],
        invocation_params=SimpleNamespace(dir=Path(__file__).resolve().parents[2]),
    )
    fixtures.pytest_configure(config)
    constructor.assert_not_called()


@pytest.mark.parametrize(
    ("args", "workers"),
    [(["tests"], 2), ([], 2), (["tests/integration"], 2), (["tests/integration"], 0)],
)
def test_real_pytest_controller_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], workers: int
) -> None:
    """Exercise actual conftest discovery and xdist transport, with a fake container."""
    monkeypatch.delenv("CRUCIBLE_TEST_DATABASE_URL", raising=False)
    repo = Path(__file__).resolve().parents[2]
    suite = tmp_path / "tests"
    integration = suite / "integration"
    integration.mkdir(parents=True)
    (suite / "__init__.py").touch()
    (integration / "__init__.py").touch()
    (integration / "postgres.py").write_text(Path(fixtures.__file__).read_text())
    (suite / "conftest.py").write_text((repo / "tests/conftest.py").read_text())
    # Installed before plugin configuration in both controller and workers.
    (tmp_path / "conftest.py").write_text(
        dedent("""
        import os
        from pathlib import Path
        from unittest.mock import MagicMock
        import docker
        import testcontainers.postgres

        def record(event):
            with Path(os.environ["PG_EVENTS"]).open("a") as log:
                log.write(event + "\\n")

        class FakePostgres:
            def __init__(self, *args, **kwargs):
                pass
            def start(self):
                record("start")
            def get_connection_url(self):
                return "postgresql+psycopg://localhost/shared"
            def stop(self):
                record("stop")

        docker.from_env = MagicMock()
        testcontainers.postgres.PostgresContainer = FakePostgres
        """)
    )
    (integration / "test_shared.py").write_text(
        dedent("""
        import os
        from pathlib import Path
        import pytest
        from tests.integration.postgres import SERVER_URL

        @pytest.mark.parametrize("case", range(4))
        def test_shared(pytestconfig, worker_id, case):
            assert pytestconfig.stash[SERVER_URL] == "postgresql+psycopg://localhost/shared"
            with Path(os.environ["PG_EVENTS"]).open("a") as log:
                log.write("test " + worker_id + "\\n")
        """)
    )
    events = tmp_path / "events"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-n", str(workers), "-q"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path), "PG_EVENTS": str(events)},
        check=False,
        capture_output=True,
        text=True,
        timeout=60 * cpu_time(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = events.read_text().splitlines()
    assert lines[0] == "start"
    assert lines[-1] == "stop"
    assert lines.count("start") == lines.count("stop") == 1
    assert len([line for line in lines if line.startswith("test ")]) == 4
    if workers:
        assert {line for line in lines if line.startswith("test ")} == {
            "test gw0",
            "test gw1",
        }
