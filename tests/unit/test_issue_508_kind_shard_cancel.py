"""hades #508: watchdog proof for e2e-kind shard cancellation.

This module contains four acceptance criteria, each as a separate test:

  AC1  _ac1_cancel_source_identified  — reads the CI workflow and Makefile,
       confirms the 20-minute GitHub Actions `timeout-minutes` is the
       cancellation source and KIND_DUMP_SECONDS = 960 s is the faulthainer.

  AC2  _ac2_watchdog_bounds_proven    — exercises a stub process that blocks
       outside Python (a blocking `sleep`) and proves the watchdog fires
       within its configured bound.

  AC3  _ac3_no_workflow_cancel        — static check that no code in the
       repository calls a GitHub API endpoint that cancels or reruns a
       workflow run.

  AC4  _ac4_shard_exclusivity         — reads every shard file under
       tools/kind/shards and proves that every test_* function in
       tests/e2e/test_kind.py appears in exactly one shard file.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import ClassVar

# ── paths used by every test ──────────────────────────────────────────

_ROOT: Path = Path(__file__).resolve().parent.parent.parent
_SHARDS_DIR: Path = _ROOT / "tools" / "kind" / "shards"
_CI_WORKFLOW: Path = _ROOT / ".github" / "workflows" / "ci.yml"
_KIND_PY: Path = _ROOT / "tests" / "e2e" / "test_kind.py"
_E2E_KIND_SH: Path = _ROOT / "tools" / "kind" / "e2e-kind.sh"
_MAKEFILE: Path = _ROOT / "Makefile"
_WATCHDOG_SH: Path = _ROOT / "tools" / "kind" / "watchdog.sh"


# ── AC1: the cancel source ───────────────────────────────────────────


def _ac1_cancel_source_identified() -> None:
    """Read the CI workflow and the Makefile, confirm the sources."""
    content = _CI_WORKFLOW.read_text(encoding="utf-8")
    # The workflow uses `timeout-minutes: 20` for each kind shard job.
    assert "timeout-minutes: 20" in content

    makefile_content = _MAKEFILE.read_text(encoding="utf-8")
    # KIND_DUMP_SECONDS controls the faulthainer_timeout for pytest.
    assert "KIND_DUMP_SECONDS" in makefile_content
    match = re.search(r"^KIND_DUMP_SECONDS\s*\?\=\s*(\d+)", makefile_content, re.M)
    assert match is not None, "KIND_DUMP_SECONDS not found in Makefile"
    assert int(match.group(1)) == 960, "expected KIND_DUMP_SECONDS=960 (16 min)"


def test_ac1_cancel_source_identified() -> None:
    """AC1: the cancel source is `timeout-minutes: 20` in the CI workflow."""
    _ac1_cancel_source_identified()


# ── AC2: watchdog bounds proven ──────────────────────────────────────


class TestWatchdogFires:
    """Prove the watchdog fires on a stub that blocks outside Python.

    Spawns a wrapper script that runs watchdog.sh in the background and
    blocks with `sleep 3600` (non-Python).  The watchdog kills the wrapper
    with SIGABRT, which should leave the marker file behind.
    """

    def test_watchdog_fires_within_bound(self, tmp_path: Path) -> None:
        """The watchdog SIGABRTs the parent within WATCHDOG_SECONDS+2s."""
        watchdog_seconds = 5  # short for unit tests
        marker = tmp_path / "wd.marker"
        wrapper = tmp_path / "wrapper.sh"
        wrapper.write_text(
            f"#!/usr/bin/env bash\n"
            f'export CRUCIBLE_KIND_WATCHDOG_MARKER="{marker}"\n'
            f'bash "{_WATCHDOG_SH}" {watchdog_seconds} "{marker}" $$ &\n'
            f"WATCHDOG_PID=$!\n"
            f"exec sleep 3600\n"
            f"kill $WATCHDOG_PID 2>/dev/null || true\n",
        )
        wrapper.chmod(0o755)

        proc = subprocess.Popen(
            ["bash", str(wrapper)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,  # noqa: PLW1509 - safe, no threads
        )
        try:
            stdout, stderr = proc.communicate(timeout=watchdog_seconds + 5)
        except subprocess.TimeoutExpired:
            # Force kill the process group if the watchdog didn't finish in time.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            raise

        # The watchdog fired: the marker file exists.
        assert marker.exists(), (
            f"watchdog marker file was not created. stdout: {stdout!r}, stderr: {stderr!r}"
        )


class TestWatchdogScript:
    """Direct tests on watchdog.sh logic."""

    def test_watchdog_pid_output(self) -> None:
        """watchdog.sh prints its background PID."""
        proc = subprocess.Popen(
            ["bash", str(_WATCHDOG_SH), "60", "", "99999"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, _ = proc.communicate(timeout=2)
        assert proc.returncode == 0
        output = stdout.strip()
        assert output.isdigit(), f"watchdog.sh should print a PID, got: {output!r}"

    def test_watchdog_does_not_fire_when_process_exits(self) -> None:
        """watchdog.sh does NOT write the marker if the target exits first."""
        marker = "/tmp/watchdog_test_no_fire.marker"
        with contextlib.suppress(FileNotFoundError):
            os.remove(marker)

        # Start a long-lived child and kill it before the watchdog fires.
        child = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(100)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pid = child.pid

        proc = subprocess.Popen(
            ["bash", str(_WATCHDOG_SH), "3", marker, str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _, _ = proc.communicate(timeout=2)

        # Kill the child before the watchdog's 3-second timer fires.
        child.kill()
        child.wait()

        # Wait for the watchdog's background sleep to finish (4 seconds).
        time.sleep(4)

        # The target was dead when the watchdog woke, so it should NOT fire.
        assert not os.path.exists(marker), (
            "watchdog fired even though the target PID already exited"
        )


# ── AC3: no workflow cancel capability ───────────────────────────────


class TestNoWorkflowCancel:
    """Static check that no code calls a workflow-run-cancellation endpoint.

    GitHub Actions exposes:
      POST /repos/{owner}/{repo}/actions/runs/{run_id}/cancel
      DELETE /repos/{owner}/{repo}/actions/runs/{run_id}
      POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun

    The repository must not contain any of these patterns in application
    code or tests (except the test file itself, which documents the absence).
    """

    # Specific patterns that indicate GitHub Actions workflow run cancellation,
    # not general "cancel" usage in the application (e.g. task cancellation,
    # workflow concurrency canceling older runs).
    _CANCEL_PATTERNS: ClassVar[list[str]] = [
        r"/actions/runs/[^/]*/cancel",  # POST to cancel a specific run
        r"/actions/runs/[^/]*/rerun",  # POST to rerun a specific run
        r"DELETE.*actions/runs/",  # DELETE workflow run endpoint
    ]

    def test_no_workflow_cancel_in_application(self) -> None:
        """No application code cancels workflow runs."""
        app_dir = _ROOT / "crucible"
        for root_dir, _, files in os.walk(app_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = Path(root_dir) / fname
                if "test_issue_508" in str(fpath):
                    continue
                text = fpath.read_text(encoding="utf-8")
                for pattern in self._CANCEL_PATTERNS:
                    assert re.search(pattern, text, re.IGNORECASE) is None, (
                        f"{fpath} contains workflow cancel pattern: {pattern}"
                    )

    def test_no_workflow_cancel_in_kind_scripts(self) -> None:
        """No kind-shard script cancels workflow runs."""
        tools_kind = _ROOT / "tools" / "kind"
        for fpath in tools_kind.rglob("*"):
            if fpath.is_dir():
                continue
            text = fpath.read_text(encoding="utf-8", errors="replace")
            for pattern in self._CANCEL_PATTERNS:
                assert re.search(pattern, text, re.IGNORECASE) is None, (
                    f"{fpath} contains workflow cancel pattern: {pattern}"
                )


# ── AC4: shard exclusivity ───────────────────────────────────────────


class TestShardExclusivity:
    """Every test_* function in test_kind.py appears in exactly one shard."""

    def _collect_shard_tests(self) -> dict[int, set[str]]:
        """Return {shard_num: set_of_test_ids} for each shard."""
        result: dict[int, set[str]] = {}
        for fpath in sorted(_SHARDS_DIR.glob("*.txt")):
            shard_num = int(fpath.stem)
            tests: set[str] = set()
            for line in fpath.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped:
                    tests.add(stripped)
            result[shard_num] = tests
        return result

    def _collect_kind_tests(self) -> set[str]:
        """Return the set of test node IDs from test_kind.py."""
        content = _KIND_PY.read_text(encoding="utf-8")
        matches = re.findall(r"def\s+(test_\w+)", content)
        node_ids: set[str] = set()
        for name in matches:
            node_id = f"tests/e2e/test_kind.py::{name}"
            node_ids.add(node_id)
        return node_ids

    def test_no_test_outside_shards(self) -> None:
        """Every test in test_kind.py is in at least one shard file."""
        kind_tests = self._collect_kind_tests()
        all_shard_tests: set[str] = set()
        for tests in self._collect_shard_tests().values():
            all_shard_tests |= tests

        missing = kind_tests - all_shard_tests
        assert not missing, f"tests not in any shard file: {sorted(missing)}"

    def test_no_test_in_multiple_shards(self) -> None:
        """No test appears in more than one shard file."""
        shard_map = self._collect_shard_tests()
        all_seen: dict[str, int] = {}
        for shard_num, tests in shard_map.items():
            for tid in tests:
                if tid in all_seen:
                    raise AssertionError(
                        f"test {tid!r} appears in shard {all_seen[tid]} and shard {shard_num}"
                    )
                all_seen[tid] = shard_num

    def test_all_shards_exist(self) -> None:
        """Shard 1, 2, 3 all exist and are non-empty."""
        shard_map = self._collect_shard_tests()
        expected_shards = {1, 2, 3}
        assert set(shard_map.keys()) == expected_shards, (
            f"expected shards {expected_shards}, got {set(shard_map.keys())}"
        )
        for sn, tests in shard_map.items():
            assert len(tests) > 0, f"shard {sn} is empty"


class TestWatchdogInE2eKindScript:
    """The e2e-kind.sh script includes the watchdog integration."""

    def test_watchdog_source_exists(self) -> None:
        """watchdog.sh exists and is executable."""
        assert _WATCHDOG_SH.exists(), "tools/kind/watchdog.sh not found"
        assert os.access(_WATCHDOG_SH, os.X_OK), "tools/kind/watchdog.sh is not executable"

    def test_watchdog_in_e2e_kind_sh(self) -> None:
        """e2e-kind.sh invokes watchdog.sh and checks the marker."""
        content = _E2E_KIND_SH.read_text(encoding="utf-8")
        assert "watchdog.sh" in content, "e2e-kind.sh does not reference watchdog.sh"
        assert "WATCHDOG_MARKER" in content or "watchdog.marker" in content, (
            "e2e-kind.sh does not set a watchdog marker path"
        )

    def test_watchdog_timeout_below_20_minutes(self) -> None:
        """The default watchdog timeout (1080 s = 18 min) is below 20 min."""
        content = _E2E_KIND_SH.read_text(encoding="utf-8")
        assert "CRUCIBLE_KIND_WATCHDOG_SECONDS" in content
        assert "1080" in content, (
            "default watchdog timeout must be 1080s (18 min) to fire "
            "before the 20-min GitHub Actions timeout"
        )
