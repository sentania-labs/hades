"""make test-shell and make test wiring (issue 163).

Verifies that the Makefile drives ``make test-shell`` from ``make test`` and that
``test-shell`` discovers every ``*_test.sh`` under ``tools/`` without manual
maintenance.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"


def _read_makefile() -> str:
    return MAKEFILE.read_text()


class TestMakefileWiring:
    """Unit tests that exercise the Makefile structure itself."""

    def test_make_test_depends_on_test_shell(self) -> None:
        """AC2: ``make test`` lists ``test-shell`` as a prerequisite."""
        lines = _read_makefile().splitlines()
        test_line: str | None = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("test:"):
                test_line = stripped
                break
        assert test_line is not None, "no 'test:' rule in the Makefile"
        assert "test-shell" in test_line, (
            f"``make test`` does not depend on test-shell: {test_line}"
        )

    def test_test_shell_is_phony(self) -> None:
        """Ensure ``test-shell`` appears in .PHONY so it runs even if a file exists."""
        content = _read_makefile()
        phony_section: str | None = None
        for line in content.splitlines():
            if line.startswith(".PHONY:"):
                phony_section = line
                break
        assert phony_section is not None
        assert "test-shell" in phony_section

    def test_test_shell_finds_all_test_sh_files(self) -> None:
        """AC1, AC3: ``make test-shell`` discovers ``*_test.sh`` recursively.

        A one-level glob (``tools/*/*_test.sh``) silently skips a script
        directly under ``tools/`` or nested deeper than one directory, so the
        recipe must use a recursive discovery mechanism (``find``) instead.
        """
        content = _read_makefile()
        shell_found = False
        for line in content.splitlines():
            if line.strip().startswith("test-shell:"):
                shell_found = True
                continue
            if shell_found and line.startswith("\t"):
                assert "tools/*/*_test.sh" not in line, (
                    "test-shell must not rely on a one-level glob; "
                    "it must discover *_test.sh recursively"
                )
                assert "find" in line and "tools" in line and "*_test.sh" in line, (
                    f"expected a recursive find over tools/ in: {line}"
                )
                return
        pytest.fail("no recipe line found after test-shell:")

    def test_existing_cleanup_test_is_found(self) -> None:
        """AC1: the cleanup test file exists under tools/ and is executable."""
        test_file = ROOT / "tools" / "kind" / "e2e-kind_cleanup_test.sh"
        assert test_file.exists(), f"expected {test_file} to exist"
        assert test_file.is_file()
        assert test_file.stat().st_mode & 0o111, "script is not executable"

    def test_new_test_sh_is_picked_up_without_makefile_change(self) -> None:
        """AC3: a new *_test.sh anywhere under tools/ is picked up automatically.

        Covers the two cases a one-level glob (``tools/*/*_test.sh``) misses:
        a script directly under ``tools/`` and one nested two levels deep
        (``tools/foo/bar/example_test.sh``). We actually run ``make test-shell``
        and confirm both scripts executed (not merely that a glob matches),
        then remove the fixtures.
        """
        top_level = ROOT / "tools" / "zz_top_level_test.sh"
        nested_dir = ROOT / "tools" / "zz_foo" / "bar"
        nested = nested_dir / "example_test.sh"
        try:
            top_level.write_text("#!/bin/sh\necho MARKER_TOP_LEVEL\nexit 0\n")
            top_level.chmod(0o755)
            nested_dir.mkdir(parents=True)
            nested.write_text("#!/bin/sh\necho MARKER_NESTED\nexit 0\n")
            nested.chmod(0o755)

            result = subprocess.run(
                ["make", "-C", str(ROOT), "test-shell"],
                capture_output=True,
                text=True,
                check=False,
            )

            assert result.returncode == 0, result.stdout + result.stderr
            assert "MARKER_TOP_LEVEL" in result.stdout, (
                f"a *_test.sh placed directly under tools/ was not run: {result.stdout}"
            )
            assert "MARKER_NESTED" in result.stdout, (
                f"a *_test.sh nested two levels under tools/ was not run: {result.stdout}"
            )
        finally:
            top_level.unlink(missing_ok=True)
            nested.unlink(missing_ok=True)
            if nested_dir.exists():
                nested_dir.rmdir()
            if nested_dir.parent.exists():
                nested_dir.parent.rmdir()

    def test_test_shell_exits_non_zero_on_failure(self) -> None:
        """AC1: ``make test-shell`` propagates a non-zero exit from a failing script.

        We create a stub under tools/kind/ that always fails, run
        ``make test-shell``, confirm it exits non-zero, then remove the stub.
        """
        stub = ROOT / "tools" / "kind" / "e2e_fail_test.sh"
        try:
            stub.write_text("#!/bin/sh\necho 'failing' >&2\nexit 42\n")
            stub.chmod(0o755)
            result = subprocess.run(
                ["make", "-C", str(ROOT), "test-shell"],
                capture_output=True,
                text=True,
                check=False,
            )
            # The loop continues despite failure; the overall exit is non-zero.
            assert result.returncode != 0, "make test-shell should exit non-zero when a test fails"
        finally:
            stub.unlink(missing_ok=True)
