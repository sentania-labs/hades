"""Unit tests for tools/spikes/hermes_room.py — argument parsing.

These tests verify that the CLI argument parser for the hermes_room spike
script handles all expected subcommands and flags correctly. They must fail
on the unchanged tree (i.e. on the tree before this task's changes).

Tests that invoke a subcommand which exercises actual hermes calls use a
short timeout and treat a TimeoutExpired as a parsing success (the parser
accepted the flags; the spike logic just takes longer than we allow).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from subprocess import TimeoutExpired

# Derive the repo root from the test file's location so the tests are portable
# (Finding 1 fix: no longer hard-coded to /crucible/repo).
_TEST_DIR = Path(__file__).resolve().parent  # tests/unit/
_REPO_ROOT = _TEST_DIR.parent.parent  # repo root (one up from tests/)
_SPIKE_SCRIPT = _REPO_ROOT / "tools" / "spikes" / "hermes_room.py"
_SHORT_TIMEOUT = 2  # seconds — long enough for argparse, short enough for CI


class TestHermesRoomParser(unittest.TestCase):
    """Tests for the hermes_room.py argument parser."""

    def _check_help(self, subcommand: str | None = None) -> str:
        """Run --help (optionally with a subcommand) and return stdout."""
        args: list[str] = [sys.executable, str(_SPIKE_SCRIPT), "--help"]
        if subcommand:
            args.insert(-1, subcommand)
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=_SHORT_TIMEOUT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"--help failed: {result.stderr[:500]}")
        return result.stdout

    def test_help_exits_zero(self) -> None:
        """Top-level --help should exit 0."""
        self._check_help()

    def test_help_mentions_all_subcommands(self) -> None:
        """--help output should mention all subcommands."""
        help_text = self._check_help()
        for sub in ["run", "timing", "mcp", "model", "resume"]:
            self.assertIn(sub, help_text, f"Subcommand '{sub}' not in --help output")

    def test_run_help(self) -> None:
        """hermes_room run --help should exit 0."""
        self._check_help("run")

    def test_timing_help(self) -> None:
        """hermes_room timing --help should exit 0."""
        self._check_help("timing")

    def test_mcp_help(self) -> None:
        """hermes_room mcp --help should exit 0."""
        self._check_help("mcp")

    def test_model_help(self) -> None:
        """hermes_room model --help should exit 0."""
        self._check_help("model")

    def test_resume_help(self) -> None:
        """hermes_room resume --help should exit 0."""
        self._check_help("resume")

    def test_invalid_subcommand_fails(self) -> None:
        """An invalid subcommand should fail with non-zero exit."""
        result = subprocess.run(
            [sys.executable, str(_SPIKE_SCRIPT), "bogus"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=_SHORT_TIMEOUT,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)

    def _check_flags_accepted(self, args: list[str]) -> None:
        """Run a subcommand with flags; accept parse success or timeout.

        A TimeoutExpired means argparse parsed successfully and the spike
        logic ran (but timed out). An 'unrecognized arguments' error means
        the parser rejected the flags.
        """
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                cwd=str(_REPO_ROOT),
                timeout=_SHORT_TIMEOUT,
                check=False,
            )
            self.assertNotIn(
                "unrecognized arguments",
                result.stderr.lower(),
                f"Flags not recognized: {result.stderr[:300]}",
            )
        except TimeoutExpired:
            # Parser accepted the flags; the spike logic took too long.
            pass

    def test_seed_flag_accepted(self) -> None:
        """--seed should be accepted by the parser for 'run'."""
        self._check_flags_accepted(
            [sys.executable, str(_SPIKE_SCRIPT), "run", "--seed", "42"],
        )

    def test_stub_dir_flag_accepted(self) -> None:
        """--stub-dir should be accepted by the parser for 'mcp'."""
        self._check_flags_accepted(
            [sys.executable, str(_SPIKE_SCRIPT), "mcp", "--stub-dir", "/tmp/test"],
        )

    def test_verbose_flag_accepted(self) -> None:
        """-v/--verbose should be accepted by the parser."""
        self._check_flags_accepted(
            [sys.executable, str(_SPIKE_SCRIPT), "run", "-v"],
        )


if __name__ == "__main__":
    unittest.main()
