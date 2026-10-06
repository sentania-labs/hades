"""Every test has a time limit (issue 192): a hang fails that test within the limit, names
it, and dumps the stack of every thread, and the run goes on to the next test."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from tests.conftest import cpu_time

ROOT = Path(__file__).resolve().parents[2]
HANGING = ROOT / "tests" / "fixtures_data" / "hanging" / "hanging_suite.py"


def test_the_repository_sets_a_default_limit_that_dumps_every_thread() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert config["timeout"] == 120
    assert config["timeout_method"] == "signal"
    assert config["faulthandler_timeout"] > config["timeout"]


def test_a_hanging_test_fails_within_the_limit_and_names_itself(tmp_path: Path) -> None:
    shutil.copy(HANGING, tmp_path / "test_hanging_fixture.py")
    started = time.monotonic()
    # The repository's own pytest configuration, with the limit shortened so the proof
    # takes seconds; everything else (the method, the dump) is what every test gets.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(ROOT / "pyproject.toml"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-o",
            "timeout=3",
            "-rA",
            str(tmp_path / "test_hanging_fixture.py"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    elapsed = time.monotonic() - started
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert elapsed < 30 * cpu_time(), out
    assert "FAILED test_hanging_fixture.py::test_hangs_forever" in out, out
    assert "Timeout (>3.0s) from pytest-timeout" in out, out
    # Every thread's stack, the stuck line included.
    assert "stuck-helper-thread" in out, out
    assert "threading.Event().wait()" in out, out
    # The run went on past the hang.
    assert "PASSED test_hanging_fixture.py::test_runs_after_the_hang" in out, out
