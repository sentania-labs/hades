"""Hades #488: secret gates judge additions and identify correctable matches."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from crucible.adapters.execution import scripts, workspace
from crucible.adapters.execution.collected import scan_changed_content
from crucible.adapters.harness.hermes import HERMES_HOME
from crucible.application.queries import gate_summary
from crucible.domain.gates import GateResult, no_secrets
from crucible.ports.execution import WORK_MOUNT

KEY = "gh" + "p_" + "A" * 36


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path, base: str = "fixture = 'plain'\n") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "fixture.py").write_text(base)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "crucible/test")
    return repo


def _output(repo: Path, tmp_path: Path) -> Path:
    output = tmp_path / "output"
    output.mkdir()
    base = _git(repo, "merge-base", "main", "HEAD")
    (output / "diff.patch").write_text(_git(repo, "diff", base, "HEAD") + "\n")
    raw = subprocess.run(
        ["git", "diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    (output / "diff-raw.txt").write_bytes(raw)
    (output / scripts.CHANGED_BLOBS_DIR).mkdir()
    return output


@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_preexisting_fixture_is_not_judged(operation: str, tmp_path: Path) -> None:
    repo = _repo(tmp_path, f"fixture = '{KEY}'\nneighbor = 'old'\n")
    if operation == "edit":
        (repo / "fixture.py").write_text(f"fixture = '{KEY}'\nneighbor = 'new'\n")
    else:
        (repo / "fixture.py").write_text("neighbor = 'old'\n")
    _git(repo, "commit", "-qam", operation)
    findings, _ = scan_changed_content(_output(repo, tmp_path))
    assert findings == ()


@pytest.mark.parametrize("prefix", ["fixture = ", "++ b/", "++ ", "+++ "])
def test_added_fixture_fails_with_path_rule_and_excerpt(tmp_path: Path, prefix: str) -> None:
    repo = _repo(tmp_path)
    (repo / "fixture.py").write_text(f"{prefix}{KEY}\n")
    _git(repo, "commit", "-qam", "add key")
    findings, _ = scan_changed_content(_output(repo, tmp_path))
    assert findings is not None
    assert [(m.path, m.pattern, m.excerpt) for m in findings] == [
        ("diff:fixture.py", "github_token", "ghp...AAA")
    ]
    item = type(
        "Item",
        (),
        {
            "id": "scan",
            "payload": {
                "findings": [
                    {"where": m.path, "pattern": m.pattern, "excerpt": m.excerpt} for m in findings
                ],
                "diff_scanned": True,
                "scanned": ["diff"],
            },
        },
    )()
    gate_input = type("Input", (), {"one": lambda self, kind: item})()
    outcome = no_secrets(gate_input)
    assert outcome.result is GateResult.FAIL
    assert "diff:fixture.py:github_token:ghp...AAA" in outcome.detail


def test_header_shaped_content_cannot_change_path_or_exclude_hunk(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "fixture.py").write_text(f"++ b/.hermes/state.db\n{KEY}\n")
    _git(repo, "commit", "-qam", "header shaped content")
    findings, _ = scan_changed_content(_output(repo, tmp_path))
    assert findings is not None
    assert [(m.path, m.pattern) for m in findings] == [("diff:fixture.py", "github_token")]


def test_hunk_state_resets_between_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "fixture.py").write_text("changed\n")
    # A secret-shaped filename is metadata, not an added source line. Sorting it
    # after fixture.py exercises leaving a hunk before recognizing the next header.
    (repo / KEY).write_text("plain\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two files")
    findings, _ = scan_changed_content(_output(repo, tmp_path))
    assert findings == ()


def test_gate_summary_keeps_result_shape_and_match_detail() -> None:
    row = SimpleNamespace(
        gate="no_secrets",
        result="fail",
        detail="secret pattern matched at diff:x:github_token:ghp...AAA",
        blocking=True,
        findings=[],
        head_sha="head",
    )
    uow = SimpleNamespace(
        tasks=SimpleNamespace(get=lambda _task_id: SimpleNamespace(head_sha="head")),
        gate_results=SimpleNamespace(list_for_task=lambda _task_id: [row]),
    )
    summary = gate_summary(uow, "task")
    assert summary["results"]["no_secrets"] == "fail"
    assert summary["details"]["no_secrets"] == row.detail


def _wrapper(name: str) -> ModuleType:
    path = Path(__file__).parents[2] / "images" / "worker" / f"crucible-{name}.py"
    spec = importlib.util.spec_from_file_location(f"issue488_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("harness", ["hermes", "qwen-code"])
def test_real_wrapper_state_is_excluded_from_scan(harness: str, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    module = _wrapper(harness)
    if harness == "hermes":
        relative = Path(HERMES_HOME).relative_to("/home/worker")
        state = repo / relative
        module.write_settings(state, 131072, 32000)
        secret_file = state / "state.db"
    else:
        state = repo / ".qwen"
        module.write_settings(repo, 131072)
        secret_file = state / "oauth_creds.json"
    secret_file.write_text(KEY)
    _git(repo, "add", "-f", str(state.relative_to(repo)))
    _git(repo, "commit", "-qm", f"accidental {harness} state")
    findings, _ = scan_changed_content(_output(repo, tmp_path))
    assert findings == ()
    assert f"/{state.relative_to(repo).as_posix().split('/')[0]}/" in workspace.EXCLUDE_ENTRIES


@pytest.mark.parametrize("prefix", ["fixture = ", "++ b/", "++ ", "+++ "])
def test_resume_reports_match_and_still_prepares_a_correction(tmp_path: Path, prefix: str) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = _repo(tmp_path)
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    (seed / "fixture.py").write_text(f"{prefix}{KEY}\n")
    _git(seed, "commit", "-qam", "failed attempt")
    head = _git(seed, "rev-parse", "HEAD")
    bundle = tmp_path / "attempt.bundle"
    _git(seed, "bundle", "create", str(bundle), "main..crucible/test")
    work = tmp_path / "resume"
    identity = tmp_path / "identity"
    identity.mkdir()
    script = scripts.preparer_script(
        url=str(origin),
        base_ref="main",
        work_branch="crucible/test",
        from_remote_branch=True,
        cache_name=None,
        author_name="test",
        author_email="test@example.invalid",
        origin_placeholder="crucible-no-remote://test",
        claude_md_wins=False,
        shims=(),
        exclude_entries=workspace.EXCLUDE_ENTRIES,
        identity_mount=str(identity),
        resume_bundle=str(bundle),
        resume_bundle_head=head,
        resume_bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
    ).replace(WORK_MOUNT, str(work))
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "path=fixture.py rule=github_token excerpt=ghp...AAA" in result.stderr
    assert KEY not in result.stderr
    assert _git(work / "repo", "rev-parse", "HEAD") == head
