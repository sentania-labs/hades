"""hades #227: scope checking for crucible-report (allowed_paths, prohibited_paths).

A temporary git repository with a base commit, one allowed and one disallowed change,
and a contract whose allowed_paths is ``["src/**"]`` and prohibited_paths is
``[".github/**"]``; the checker names only the disallowed path.  Also covers ``**``
allowed_paths (everything allowed) with one prohibited change.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CHECKER = _ROOT / "images" / "worker" / "crucible-report.py"


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location("crucible_report_scope", _CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _create_minimal_report() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "self_review": {
            "documentation": ["No documentation change needed for this test fixture."],
            "acceptance_criteria": [
                {"id": "AC1", "status": "met", "evidence": "Test fixture"},
                {"id": "AC2", "status": "met", "evidence": "Test fixture"},
            ],
            "omissions": [],
        },
        "summary": "test",
        "acceptance_mapping": [{"id": "AC1", "status": "met", "evidence": ""}],
        "proposed_pull_request": {"title": "t", "body": "b"},
        "limitations": [],
        "risks": [],
        "blockers": [],
        "follow_ups": [],
    }


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )


@pytest.fixture()
def temp_git_repo(tmp_path: Path) -> Path:
    """Create an empty git repo at *tmp_path* and return the path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, ["init"])
    _run_git(repo, ["config", "user.email", "test@test.com"])
    _run_git(repo, ["config", "user.name", "Test"])
    return repo


class TestScopeChecking:
    """Tests for the out-of-scope change detection (hades #227)."""

    def test_allowed_and_disallowed_changes(self, temp_git_repo: Path) -> None:
        """One allowed path (src/**) and one disallowed (.github/**)."""
        repo = temp_git_repo

        # Create a base commit and a second commit so we can diff.
        (repo / "README.md").write_text("base", encoding="utf-8")
        _run_git(repo, ["add", "README.md"])
        _run_git(repo, ["commit", "-m", "base"])

        # Create the "allowed" change.
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "main.py").write_text("allowed", encoding="utf-8")
        _run_git(repo, ["add", "src/main.py"])
        _run_git(repo, ["commit", "-m", "allowed"])

        # Create the "disallowed" change.
        (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (repo / ".github" / "workflows" / "ci.yml").write_text("disallowed", encoding="utf-8")
        _run_git(repo, ["add", ".github/workflows/ci.yml"])
        _run_git(repo, ["commit", "-m", "disallowed"])

        # Compute changed files relative to the base commit.
        base_commit = _run_git(repo, ["rev-parse", "HEAD~1"]).stdout.strip()
        diff_result = _run_git(repo, ["diff", "--name-only", base_commit, "HEAD"])
        changed = [p for p in diff_result.stdout.strip().splitlines() if p]

        # Contract with allowed and prohibited paths.
        scope_data = {
            "allowed_paths": ["src/**"],
            "prohibited_paths": [".github/**"],
        }

        report = _create_minimal_report()
        problems = checker.check(
            report,
            criteria=None,
            changed=changed,
            scope=scope_data,
        )

        # Only the .github path should be flagged.
        problem_paths = [p for p in problems if "you changed" in p]
        assert len(problem_paths) == 1
        assert ".github/workflows/ci.yml" in problem_paths[0]
        assert "src/main.py" not in problem_paths[0]

    def test_double_star_allowed_paths_allows_everything(self, temp_git_repo: Path) -> None:
        """When allowed_paths is ``["**"]``, every path is allowed.
        A prohibited path is still caught.
        """
        repo = temp_git_repo

        (repo / "README.md").write_text("base", encoding="utf-8")
        _run_git(repo, ["add", "README.md"])
        _run_git(repo, ["commit", "-m", "base"])

        (repo / "anywhere.txt").write_text("ok", encoding="utf-8")
        _run_git(repo, ["add", "anywhere.txt"])
        _run_git(repo, ["commit", "-m", "anywhere"])

        (repo / ".github").mkdir(parents=True, exist_ok=True)
        (repo / ".github" / "x.yml").write_text("bad", encoding="utf-8")
        _run_git(repo, ["add", ".github/x.yml"])
        _run_git(repo, ["commit", "-m", "prohibited"])

        base_commit = _run_git(repo, ["rev-parse", "HEAD~2"]).stdout.strip()
        diff_result = _run_git(repo, ["diff", "--name-only", base_commit, "HEAD"])
        changed = [p for p in diff_result.stdout.strip().splitlines() if p]

        scope_data = {
            "allowed_paths": ["**"],
            "prohibited_paths": [".github/**"],
        }

        problems = checker.check(
            _create_minimal_report(),
            criteria=None,
            changed=changed,
            scope=scope_data,
        )

        problem_paths = [p for p in problems if "you changed" in p]
        assert len(problem_paths) == 1
        assert ".github/x.yml" in problem_paths[0]
        assert "anywhere.txt" not in problem_paths[0]

    def test_git_unavailable_skips_scope(self) -> None:
        """When scope_changed is None, scope check is skipped."""
        report = _create_minimal_report()
        problems = checker.check(report, criteria=None, changed=None, scope=None)
        assert problems == []

    def test_no_prohibited_paths_allows_all(self, temp_git_repo: Path) -> None:
        """Without prohibited_paths and with a wildcard allowed, nothing is flagged."""
        repo = temp_git_repo

        (repo / "README.md").write_text("base", encoding="utf-8")
        _run_git(repo, ["add", "README.md"])
        _run_git(repo, ["commit", "-m", "base"])

        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "main.py").write_text("ok", encoding="utf-8")
        _run_git(repo, ["add", "src/main.py"])
        _run_git(repo, ["commit", "-m", "allowed"])

        base_commit = _run_git(repo, ["rev-parse", "HEAD~1"]).stdout.strip()
        diff_result = _run_git(repo, ["diff", "--name-only", base_commit, "HEAD"])
        changed = [p for p in diff_result.stdout.strip().splitlines() if p]

        scope_data = {
            "allowed_paths": ["src/**"],
            "prohibited_paths": [],
        }

        problems = checker.check(
            _create_minimal_report(),
            criteria=None,
            changed=changed,
            scope=scope_data,
        )
        problem_paths = [p for p in problems if "you changed" in p]
        assert problem_paths == []
