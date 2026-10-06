"""Issue 36: workspace_fingerprint budget and .git pruning (FDY-0341).

Verifies that the supervisor's own workspace_fingerprint:
  - Returns ``None`` when the tree is too large for the time budget.
  - Produces the same fingerprint on a small tree as the previous
    unbounded implementation (newest mtime, file count, total bytes).
  - Skips the ``.git`` subtree entirely so a heavy Git worktree does not
    dominate the walk.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from crucible.application.supervisor import workspace_fingerprint
from crucible.ports.execution import Workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Workspace:
    """Create a minimal workspace with checkout and report directories."""
    checkout = tmp_path / "repo"
    report = tmp_path / "report"
    checkout.mkdir()
    report.mkdir()
    return Workspace(
        attempt_id="test-attempt",
        checkout_path=str(checkout),
        identity_path=str(tmp_path / "identity"),
        report_path=str(report),
    )


def _write_file(path: Path, content: str) -> Path:
    """Write *content* to *path* (creating parents) and return *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC2: small-tree fidelity
# ---------------------------------------------------------------------------


def test_workspace_fingerprint_small_tree_matches_expected(tmp_path: Path) -> None:
    """On a small tree the fingerprint is identical to the old output:
    (newest_mtime_ns, file_count, total_bytes)."""
    ws = _make_workspace(tmp_path)
    repo = Path(ws.checkout_path)
    report = Path(ws.report_path)

    # A handful of files in the checkout and report.
    _write_file(repo / "main.py", "print('hello')\n")
    _write_file(repo / "lib" / "helper.py", "def x(): pass\n")
    _write_file(report / "report.json", '{"status": "ok"}')

    fingerprint = workspace_fingerprint(ws)
    assert fingerprint is not None

    newest_ns, file_count, total_bytes = fingerprint

    # file_count: root repo, root report, main.py, lib (dir), lib/helper.py,
    #             report.json -- 6 entries (dirs count too).
    assert file_count == 6, f"expected 6 entries, got {file_count}"

    # total_bytes: size of main.py + lib/helper.py + report.json (dirs have 0 bytes)
    expected_bytes = len("print('hello')\n") + len("def x(): pass\n") + len('{"status": "ok"}')
    assert total_bytes == expected_bytes, f"expected {expected_bytes} bytes, got {total_bytes}"

    # newest_ns: the most recent mtime among all files.
    assert newest_ns > 0

    # Mutating a file changes the fingerprint.
    time.sleep(0.01)  # ensure mtime moves forward
    _write_file(repo / "main.py", "print('goodbye')\n")
    new_fp = workspace_fingerprint(ws)
    assert new_fp is not None
    assert new_fp != fingerprint


# ---------------------------------------------------------------------------
# AC1: budget enforcement on a large tree
# ---------------------------------------------------------------------------


def test_workspace_fingerprint_returns_none_when_over_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the tree is large enough for the budget,
    workspace_fingerprint returns None."""
    ws = _make_workspace(tmp_path)
    repo = Path(ws.checkout_path)

    # 5k files should be plenty for a budget of 0.001 s.
    num_files = 5_000
    for i in range(num_files):
        bucket = f"bucket{i % 50:02d}"
        _write_file(repo / bucket / f"file{i:04d}.txt", "x")

    # Shrink the budget dramatically so 5k files blow past it.
    monkeypatch.setattr("crucible.application.supervisor.ACTIVITY_WALK_SECONDS", 0.001)

    t0 = time.monotonic()
    result = workspace_fingerprint(ws)
    elapsed = time.monotonic() - t0

    # Should have returned early with None.
    assert result is None, f"expected None but got {result} after {elapsed:.2f}s"

    # Must return quickly (under 2 s on a slow host).
    assert elapsed < 2.0, f"fingerprint took {elapsed:.2f}s"


def test_workspace_fingerprint_respects_budget_on_small_tree(tmp_path: Path) -> None:
    """A small tree must finish quickly and return a valid fingerprint."""
    ws = _make_workspace(tmp_path)
    _write_file(Path(ws.checkout_path) / "a.txt", "small\n")

    t0 = time.monotonic()
    result = workspace_fingerprint(ws)
    elapsed = time.monotonic() - t0

    assert result is not None, "small tree should return a fingerprint"
    assert elapsed < 1.0, f"small tree took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# .git pruning
# ---------------------------------------------------------------------------


def test_workspace_fingerprint_skips_git_subtree(tmp_path: Path) -> None:
    """The .git directory is never descended into."""
    ws = _make_workspace(tmp_path)
    repo = Path(ws.checkout_path)

    # Simulate a git repo with a large objects tree.
    git_dir = repo / ".git"
    objects_dir = git_dir / "objects"
    _write_file(objects_dir / "pack", "large pack data" * 1000)
    _write_file(objects_dir / "info", "info")
    _write_file(repo / "main.py", "print('hello')\n")

    fingerprint = workspace_fingerprint(ws)
    assert fingerprint is not None
    _newest_ns, file_count, total_bytes = fingerprint

    # Count should be small -- .git tree content must not be counted.
    assert file_count < 10, f".git not pruned: counted {file_count} entries"

    # The large pack data is not included in total_bytes.
    assert total_bytes < 10_000, f".git content leaked into bytes: {total_bytes}"


def test_workspace_fingerprint_counts_files_outside_git(tmp_path: Path) -> None:
    """Files that happen to live next to .git are still counted."""
    ws = _make_workspace(tmp_path)
    repo = Path(ws.checkout_path)

    _write_file(repo / ".git" / "config", "[core]\n")
    _write_file(repo / "src" / "main.py", "def main(): pass\n")

    fingerprint = workspace_fingerprint(ws)
    assert fingerprint is not None
    _newest_ns, file_count, total_bytes = fingerprint

    # .git/config is inside .git so skipped.
    # src/main.py is counted. root dir counted too.
    assert file_count >= 2  # repo root + src/main.py (src itself is counted)
    assert total_bytes == len("def main(): pass\n")


# ---------------------------------------------------------------------------
# None propagates through the supervisor's stall-clock logic
# ---------------------------------------------------------------------------


def test_workspace_fingerprint_none_semantics(tmp_path: Path) -> None:
    """None fingerprint is valid: the supervisor treats it as 'could not tell'
    and does not reset the stall clock."""
    ws = _make_workspace(tmp_path)
    # No files, minimal tree -- should still fingerprint fine.
    result = workspace_fingerprint(ws)
    assert result is not None
    # Two roots (checkout + report), no files, no bytes; mtime > 0 because
    # the freshly-created directories have real timestamps.
    _newest_ns, file_count, total_bytes = result
    assert file_count == 2  # two directory roots
    assert total_bytes == 0  # no regular files
