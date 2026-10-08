"""Hades #376: prove integration tier Docker client initialization without context manager
and guard against silent skipping when no database is reachable.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from _pytest.outcomes import Failed, Skipped

from tests.conftest import cpu_time
from tests.integration import conftest as integration_conftest
from tests.integration import postgres as fixtures


class NonContextManagerDockerClient:
    """A DockerClient matching docker-py 7.2.0: close() and ping() exist, but no __enter__."""

    def __init__(self) -> None:
        self.ping_mock = Mock()
        self.close_mock = Mock()

    def ping(self) -> None:
        self.ping_mock()

    def close(self) -> None:
        self.close_mock()


def test_ast_postgres_does_not_use_docker_as_context_manager() -> None:
    """AC1: postgres.py does not use from_env() as a context manager."""
    source_path = Path(fixtures.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                expr_str = ast.unparse(item.context_expr)
                assert "from_env" not in expr_str, (
                    f"Line {node.lineno}: from_env() must not be used in with ({expr_str})"
                )


def test_postgres_creates_and_closes_client_without_context_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: docker client without context manager is pinged, closed, and container is configured."""
    monkeypatch.delenv("CRUCIBLE_TEST_DATABASE_URL", raising=False)
    fake_client = NonContextManagerDockerClient()
    monkeypatch.setattr("docker.from_env", Mock(return_value=fake_client))

    pg = Mock()
    pg.get_connection_url.return_value = "postgresql+psycopg://localhost/shared"
    constructor = Mock(return_value=pg)
    monkeypatch.setattr("testcontainers.postgres.PostgresContainer", constructor)

    controller: Any = SimpleNamespace(
        stash=pytest.Stash(),
        option=SimpleNamespace(collectonly=False),
        args=["tests/integration"],
        invocation_params=SimpleNamespace(dir=Path(__file__).resolve().parents[2]),
    )

    fixtures.pytest_configure(controller)

    fake_client.ping_mock.assert_called_once_with()
    fake_client.close_mock.assert_called_once_with()
    assert controller.stash[fixtures.SERVER_URL] == "postgresql+psycopg://localhost/shared"

    fixtures.pytest_unconfigure(controller)
    pg.stop.assert_called_once_with()


def test_postgres_client_closed_even_when_ping_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: docker client is closed in finally block even when ping fails."""
    monkeypatch.delenv("CRUCIBLE_TEST_DATABASE_URL", raising=False)
    fake_client = NonContextManagerDockerClient()
    fake_client.ping_mock.side_effect = RuntimeError("daemon not responding")
    monkeypatch.setattr("docker.from_env", Mock(return_value=fake_client))

    controller: Any = SimpleNamespace(
        stash=pytest.Stash(),
        option=SimpleNamespace(collectonly=False),
        args=["tests/integration"],
        invocation_params=SimpleNamespace(dir=Path(__file__).resolve().parents[2]),
    )

    fixtures.pytest_configure(controller)

    fake_client.ping_mock.assert_called_once_with()
    fake_client.close_mock.assert_called_once_with()
    assert controller.stash[fixtures.SERVER_URL] is None


def test_database_url_guard_fails_when_no_database_and_no_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: database_url fixture fails when no server URL is available and opt-out is unset."""
    monkeypatch.delenv("CRUCIBLE_ALLOW_NO_DATABASE", raising=False)
    pytestconfig: Any = SimpleNamespace(stash={fixtures.SERVER_URL: None})

    fixture_fn = getattr(
        integration_conftest.database_url, "__wrapped__", integration_conftest.database_url
    )
    fixture_gen = fixture_fn("gw0", "uid-123", pytestconfig)
    assert isinstance(fixture_gen, Generator)

    with pytest.raises(Failed) as exc_info:
        next(fixture_gen)

    assert "CRUCIBLE_ALLOW_NO_DATABASE=1" in str(exc_info.value)
    assert "CRUCIBLE_TEST_DATABASE_URL" in str(exc_info.value)


def test_database_url_guard_skips_when_opt_out_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: database_url fixture skips when no server URL is available and opt-out is set."""
    monkeypatch.setenv("CRUCIBLE_ALLOW_NO_DATABASE", "1")
    pytestconfig: Any = SimpleNamespace(stash={fixtures.SERVER_URL: None})

    fixture_fn = getattr(
        integration_conftest.database_url, "__wrapped__", integration_conftest.database_url
    )
    fixture_gen = fixture_fn("gw0", "uid-123", pytestconfig)
    assert isinstance(fixture_gen, Generator)

    with pytest.raises(Skipped) as exc_info:
        next(fixture_gen)

    assert "CRUCIBLE_ALLOW_NO_DATABASE" in str(exc_info.value)


def test_make_test_integration_guard_via_subprocess(tmp_path: Path) -> None:
    """AC2: real pytest invocation fails when no database is reachable, and skips with opt-out."""
    repo = Path(__file__).resolve().parents[2]
    env_base = {
        **os.environ,
        "PYTHONPATH": str(repo),
    }
    env_base.pop("CRUCIBLE_TEST_DATABASE_URL", None)
    env_base.pop("CRUCIBLE_ALLOW_NO_DATABASE", None)

    test_file = tmp_path / "test_dummy.py"
    test_file.write_text(
        "def test_needs_db(database_url):\n    assert database_url\n",
        encoding="utf-8",
    )

    # 1. Without opt-out, must fail non-zero
    result_fail = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "-p",
            "tests.integration.conftest",
            "-q",
        ],
        cwd=tmp_path,
        env=env_base,
        capture_output=True,
        text=True,
        check=False,
        timeout=60 * cpu_time(),
    )
    assert result_fail.returncode != 0, result_fail.stdout + result_fail.stderr
    assert "FAILED" in result_fail.stdout or "ERROR" in result_fail.stdout
    assert "CRUCIBLE_ALLOW_NO_DATABASE=1" in result_fail.stdout

    # 2. With opt-out, must exit 0 with a skip
    env_opt_out = {**env_base, "CRUCIBLE_ALLOW_NO_DATABASE": "1"}
    result_skip = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "-p",
            "tests.integration.conftest",
            "-q",
        ],
        cwd=tmp_path,
        env=env_opt_out,
        capture_output=True,
        text=True,
        check=False,
        timeout=60 * cpu_time(),
    )
    assert result_skip.returncode == 0, result_skip.stdout + result_skip.stderr
    assert "1 skipped" in result_skip.stdout
