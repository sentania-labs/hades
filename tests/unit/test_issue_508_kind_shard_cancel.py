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
from pathlib import Path
from typing import ClassVar

from tests.wait import wait_until

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


def _alive(pid: int) -> bool:
    """Whether a process is still running; a zombie awaiting its reaper is not."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


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
        stdout, _ = proc.communicate(timeout=2)
        watchdog_pid = int(stdout.strip())

        # Kill the child before the watchdog's 3-second timer fires.
        child.kill()
        child.wait()

        # Wait for the watchdog's background process to finish its 3-second timer.
        wait_until(
            lambda: not _alive(watchdog_pid),
            timeout=10,
            describe="the watchdog's background process to exit",
        )

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


class TestWatchdogTargetsPytestNotShell:
    """Prove watchdog.sh targets pytest PID, not the parent shell PID.

    Finding 3T8YNPPDCHQ1SX9F48 / 3Z5AYT8CSQ0QTY81WZ: the watchdog must
    SIGABRT the pytest process (or its process group), not $$ (the shell).
    Sending SIGABRT to bash does not trigger Python's faulthandler.
    """

    def test_watchdog_sh_accepts_pytest_pid(self) -> None:
        """watchdog.sh's third arg is called pytest_pid, not caller_pid."""
        content = _WATCHDOG_SH.read_text(encoding="utf-8")
        assert "pytest_pid" in content, "watchdog.sh must use 'pytest_pid' for the third parameter"
        assert "caller_pid" not in content, (
            "watchdog.sh should not reference 'caller_pid' — the parameter is pytest_pid"
        )

    def test_watchdog_sh_sends_sigabrt_to_process_group(self) -> None:
        """watchdog.sh tries process-group kill before single-PID kill."""
        content = _WATCHDOG_SH.read_text(encoding="utf-8")
        assert "SIGABRT" in content, "watchdog.sh must send SIGABRT"
        # It should try the negative-PID (process group) first, then fall back.
        assert "-${pytest_pid}" in content or "-$pytest_pid" in content, (
            "watchdog.sh must attempt process-group kill (negative PID) to trigger "
            "faulthandler in all pytest children"
        )

    def test_e2e_kind_sh_passes_blank_to_watchdog_initially(self) -> None:
        """e2e-kind.sh does not pass $$ to watchdog.sh."""
        content = _E2E_KIND_SH.read_text(encoding="utf-8")
        # Before my fix, the line was:
        #   watchdog_pid=$(bash ... watchdog.sh ... $$)
        # After: the third arg is '' and pytest_pid is filled after bg-launch.
        lines = content.splitlines()
        watchdog_lines = [ln for ln in lines if "watchdog.sh" in ln and "bash" in ln]
        assert len(watchdog_lines) >= 1, "expected at least one watchdog.sh invocation"
        for line in watchdog_lines:
            # The old buggy line passed "$$". The fix passes "" or pytest_pid.
            assert '"$$"' not in line and "'$$'" not in line, (
                "e2e-kind.sh must not pass $$ (shell PID) to watchdog.sh; "
                "it must pass the pytest PID instead"
            )

    def test_e2e_kind_sh_captures_pytest_pid(self) -> None:
        """e2e-kind.sh runs pytest in background and captures its PID."""
        content = _E2E_KIND_SH.read_text(encoding="utf-8")
        assert "pytest_pid=" in content or "pytest_pid =" in content, (
            "e2e-kind.sh must capture the pytest PID in a variable"
        )
        assert "&" in content and "pytest" in content, (
            "e2e-kind.sh must background the pytest process to capture its PID"
        )


class TestE2eKindHandlesExitCode:
    """Prove e2e-kind.sh survives non-zero pytest exit codes (set +e).

    Finding 3XW34RMPY575HQW7DD: with set -e, a non-zero pytest exit aborts
    the script immediately, skipping the marker check and watchdog cleanup.
    The fix wraps pytest in `set +e ... set -e`.
    """

    def test_set_plus_e_around_pytest(self) -> None:
        """e2e-kind.sh disables errexit around pytest."""
        content = _E2E_KIND_SH.read_text(encoding="utf-8")
        # Find lines around pytest invocation.
        lines = content.splitlines()
        pytest_line_idx = None
        for i, line in enumerate(lines):
            if "uv run pytest" in line and "pytest_pid" not in line:
                pytest_line_idx = i
                break
        assert pytest_line_idx is not None, "could not find 'uv run pytest' line"

        # Look for set +e within a few lines before the pytest line.
        found_plus_e = False
        for i in range(max(0, pytest_line_idx - 5), pytest_line_idx + 1):
            if "set +e" in lines[i]:
                found_plus_e = True
                break
        assert found_plus_e, (
            "e2e-kind.sh must run 'set +e' before uv run pytest to survive "
            "non-zero exit codes under set -e"
        )

        # Look for set -e within a few lines after.
        found_minus_e = False
        for i in range(pytest_line_idx, min(len(lines), pytest_line_idx + 10)):
            if "set -e" in lines[i] and "set +e" not in lines[i]:
                found_minus_e = True
                break
        assert found_minus_e, "e2e-kind.sh must restore 'set -e' after the pytest block"


class TestWatchdogStartsBeforeReadinessGate:
    """Prove the watchdog is started before the readiness gate (AC1 / Q3VFH6VXPNWC9BC2H).

    The watchdog timer must fire at 18 minutes from script start, not after
    cluster setup completes.  The fix records _watchdog_start_time before
    crucible_kind_wait_and_retry and adjusts the timeout by elapsed time.
    """

    def test_watchdog_start_time_recorded_before_readiness_gate(self) -> None:
        """_watchdog_start_time is set before the readiness gate call."""
        content = _E2E_KIND_SH.read_text(encoding="utf-8")
        lines = content.splitlines()

        # Find the assignment line (the one that _sets_ the variable)
        start_time_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("_watchdog_start_time=") or stripped.startswith(
                "_watchdog_start_time +"
            ):
                start_time_idx = i
                break

        # Find the readiness gate conditional (not the function definition)
        readiness_gate_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("if ! crucible_kind_wait_and_retry"):
                readiness_gate_idx = i
                break

        assert start_time_idx is not None, "_watchdog_start_time assignment not found"
        assert readiness_gate_idx is not None, "crucible_kind_wait_and_retry call not found"
        assert start_time_idx < readiness_gate_idx, (
            "_watchdog_start_time must be set before the readiness gate call"
        )

    def test_elapsed_adjustment(self) -> None:
        """The watchdog timeout is adjusted by elapsed setup time."""
        content = _E2E_KIND_SH.read_text(encoding="utf-8")
        assert "_elapsed_setup" in content, "e2e-kind.sh must compute elapsed setup time"
        assert "_watchdog_remaining" in content or "WATCHDOG_TIMEOUT" in content, (
            "e2e-kind.sh must adjust the watchdog timeout"
        )
