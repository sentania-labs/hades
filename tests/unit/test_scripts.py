"""The generated scripts treat contract-derived values as data (08).

The commands a contract lists under `required_verification` are executed as given: that
is the point of the gate. Everything else a contract carries, refs, paths, check ids, is
bound to a shell variable from a single-quoted literal and referenced quoted, so it is
data to `sh` whatever it contains.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from crucible.adapters.execution import scripts, workspace
from crucible.adapters.execution.collected import read_outputs
from crucible.ports.execution import IDENTITY_MOUNT, OUTPUT_MOUNT, REPORT_MOUNT, LaunchSpec
from tests.collector_tools import collector_env
from tests.fixtures import contract_document

HOSTILE_REFS = [
    "crucible/$(touch /tmp/crucible-pwned)",
    "crucible/`touch /tmp/crucible-pwned`",
    "crucible/x; touch /tmp/crucible-pwned",
    "crucible/x' ; touch /tmp/crucible-pwned ; '",
    "crucible/x\ntouch /tmp/crucible-pwned",
    "--upload-pack=touch /tmp/crucible-pwned",
]


def preparer(work_branch: str, base_ref: str = "main", claude_md_wins: bool = True) -> str:
    return scripts.preparer_script(
        url="/crucible/origin",
        base_ref=base_ref,
        work_branch=work_branch,
        from_remote_branch=False,
        cache_name=None,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
        origin_placeholder=workspace.ORIGIN_PLACEHOLDER,
        claude_md_wins=claude_md_wins,
        shims=workspace.SHIM_NAMES,
        exclude_entries=workspace.EXCLUDE_ENTRIES,
        identity_mount=IDENTITY_MOUNT,
    )


def test_only_agents_md_is_a_generated_shim() -> None:
    script = preparer("crucible/test")
    assert "for shim in 'AGENTS.md'" in script
    assert "for shim in 'CLAUDE.md'" not in script


def test_a_project_claude_md_suppresses_the_agents_md_shim() -> None:
    script = preparer("crucible/test")
    assert 'if [ "$CLAUDE_MD_WINS" = "1" ] && [ "$shim" = "AGENTS.md" ]' in script


@pytest.mark.parametrize(
    ("harness", "claude_md_wins"),
    [("claude_code", True), ("codex", False), ("agy", False), ("future_claude", True)],
)
@pytest.mark.parametrize("project_file", ["neither", "claude", "agents"])
def test_shim_policy_runs_the_rendered_prepare_script(
    harness: str, claude_md_wins: bool, project_file: str, tmp_path: Path
) -> None:
    """The rendered script applies the harness-specific policy to a real checkout."""
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    (origin / "README").write_text("base\n", encoding="utf-8")
    if project_file == "claude":
        (origin / "CLAUDE.md").write_text("project instructions\n", encoding="utf-8")
    elif project_file == "agents":
        (origin / "AGENTS.md").write_text("project instructions\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=origin,
        check=True,
    )
    work = tmp_path / "work"
    script = preparer("crucible/test", claude_md_wins=claude_md_wins)
    script = script.replace("/crucible/origin", str(origin))
    script = script.replace("/crucible/work", str(work))
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    checkout = work / "repo"
    shim = checkout / "AGENTS.md"
    if project_file == "agents":
        assert shim.read_text(encoding="utf-8") == "project instructions\n"
    elif project_file == "claude" and claude_md_wins:
        assert not shim.exists()
    else:
        assert shim.is_file()
        assert shim.read_text(encoding="utf-8") == (
            "Read /crucible/identity/IDENTITY.md first; it is the task contract for this run.\n"
        )


@pytest.mark.parametrize("harness", ["claude_code", "codex", "agy"])
@pytest.mark.parametrize("claude_kind", ["directory", "symlink"])
def test_claude_code_policy_handles_non_regular_claude_path(
    harness: str, claude_kind: str, tmp_path: Path
) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    if claude_kind == "directory":
        (origin / "CLAUDE.md").mkdir()
        (origin / "CLAUDE.md" / "README").write_text("project instructions\n", encoding="utf-8")
    else:
        (origin / "instructions").write_text("project instructions\n", encoding="utf-8")
        (origin / "CLAUDE.md").symlink_to("instructions")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=origin,
        check=True,
    )
    work = tmp_path / "work"
    script = preparer("crucible/test", claude_md_wins=harness == "claude_code")
    script = script.replace("/crucible/origin", str(origin))
    script = script.replace("/crucible/work", str(work))
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    shim = work / "repo" / "AGENTS.md"
    if harness == "claude_code":
        assert not shim.exists()
    else:
        assert shim.is_file()


def parses(script: str) -> None:
    """`sh -n` on the generated text: a quoting mistake is a syntax error or worse."""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        result = subprocess.run(["sh", "-n", path], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.parametrize("ref", HOSTILE_REFS)
def test_a_hostile_ref_is_data_in_every_generated_script(ref: str) -> None:
    for script in (
        preparer(ref),
        preparer("crucible/x", ref),
        scripts.collector_script(base_ref=ref, work_branch=ref, size_cap_bytes=1024),
    ):
        parses(script)
        # The value appears only inside a single-quoted binding, never bare.
        for line in script.splitlines():
            if ref.splitlines()[0] in line:
                assert line.startswith(("WORK_BRANCH='", "BASE_REF='")), line


@pytest.mark.parametrize("ref", HOSTILE_REFS)
def test_a_hostile_ref_does_not_execute(ref: str, tmp_path: Path) -> None:
    """Run the generated preparer under a stub git and assert the payload never ran."""
    marker = tmp_path / "pwned"
    stub = tmp_path / "bin"
    stub.mkdir()
    # A git that records its argv and always fails to find a ref, so the script takes
    # its error path with the hostile value in hand.
    (stub / "git").write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$ARGV_LOG"\nexit 1\n', encoding="utf-8"
    )
    (stub / "git").chmod(0o755)
    (stub / "touch").write_text(
        f"#!/bin/sh\nprintf 'ran\\n' > {marker}\nexit 0\n", encoding="utf-8"
    )
    (stub / "touch").chmod(0o755)
    argv_log = tmp_path / "argv.log"
    script = tmp_path / "preparer.sh"
    script.write_text(preparer(ref), encoding="utf-8")
    subprocess.run(
        ["sh", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{stub}:{shutil.which('sh') and '/usr/bin:/bin'}",
            "ARGV_LOG": str(argv_log),
            "HOME": str(tmp_path),
        },
    )
    assert not marker.exists(), "the ref executed"
    if argv_log.exists():
        # Whatever git was handed, it was one argument, not a command.
        assert "touch /tmp/crucible-pwned" not in argv_log.read_text().replace(ref, "")


def test_quota_checkpoint_ignores_worker_filter_and_signing_programs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    output = tmp_path / "output"
    report = tmp_path / "report"
    repo.mkdir()
    output.mkdir()
    report.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    (output / "prepared-base.txt").write_text(
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "main"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    subprocess.run(["git", "checkout", "-q", "-b", "crucible/test"], cwd=repo, check=True)
    filter_sentinel = tmp_path / "filter-ran"
    signing_sentinel = tmp_path / "signing-ran"
    subprocess.run(
        [
            "git",
            "config",
            "filter.evil.clean",
            f"sh -c 'touch {filter_sentinel}; cat'",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "commit.gpgsign", "true"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "gpg.program", f"sh -c 'touch {signing_sentinel}; exit 1'"],
        cwd=repo,
        check=True,
    )
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("checkpoint\n", encoding="utf-8")
    generated = scripts.collector_script(
        base_ref="main",
        work_branch="crucible/test",
        size_cap_bytes=1024,
        attempt_id="attempt-1",
        quota_checkpoint=True,
    )
    generated = generated.replace(scripts.REPO_MOUNT, str(repo))
    generated = generated.replace(OUTPUT_MOUNT, str(output))
    generated = generated.replace(REPORT_MOUNT, str(report))
    result = subprocess.run(["sh", "-c", generated], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not filter_sentinel.exists()
    assert not signing_sentinel.exists()
    assert (
        subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .startswith("wip(crucible): attempt attempt-1")
    )


def test_quota_checkpoint_refuses_a_worker_commondir_redirect(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    common = tmp_path / "worker-common"
    output = tmp_path / "output"
    report = tmp_path / "report"
    for path in (repo, common, output, report):
        path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=common, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    (output / "prepared-base.txt").write_text(
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "main"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    subprocess.run(["git", "checkout", "-q", "-b", "crucible/test"], cwd=repo, check=True)
    filter_sentinel = tmp_path / "redirect-filter-ran"
    hook_sentinel = tmp_path / "redirect-hook-ran"
    hooks = common / ".git" / "hooks-redirected"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {hook_sentinel}\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.hooksPath", str(hooks)], cwd=common, check=True)
    subprocess.run(
        [
            "git",
            "config",
            "filter.evil.clean",
            f"sh -c 'touch {filter_sentinel}; cat'",
        ],
        cwd=common,
        check=True,
    )
    (repo / ".git" / "commondir").write_text(str(common / ".git") + "\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("checkpoint\n", encoding="utf-8")
    generated = scripts.collector_script(
        base_ref="main",
        work_branch="crucible/test",
        size_cap_bytes=1024,
        attempt_id="attempt-redirect",
        quota_checkpoint=True,
    )
    generated = generated.replace(scripts.REPO_MOUNT, str(repo))
    generated = generated.replace(OUTPUT_MOUNT, str(output))
    generated = generated.replace(REPORT_MOUNT, str(report))
    result = subprocess.run(["sh", "-c", generated], capture_output=True, text=True, check=False)
    assert result.returncode == 4
    assert "commondir redirect" in result.stderr
    assert "commondir redirect" in (output / "checkpoint-refusal.txt").read_text()
    assert not filter_sentinel.exists()
    assert not hook_sentinel.exists()


def _work_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A checkout on `crucible/test` one commit past `main`, with a report and an
    output directory beside it, as the collector finds them."""
    repo, output, report = tmp_path / "repo", tmp_path / "output", tmp_path / "report"
    for path in (repo, output, report):
        path.mkdir()
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", *identity, "commit", "-q", "-m", "base"], cwd=repo, check=True)
    (output / "prepared-base.txt").write_text(
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "main"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    subprocess.run(["git", "checkout", "-q", "-b", "crucible/test"], cwd=repo, check=True)
    (repo / "committed.txt").write_text("committed by the worker\n", encoding="utf-8")
    subprocess.run(["git", "add", "committed.txt"], cwd=repo, check=True)
    subprocess.run(["git", *identity, "commit", "-q", "-m", "work"], cwd=repo, check=True)
    return repo, output, report


def _collect(
    repo: Path, output: Path, report: Path, **kw: object
) -> subprocess.CompletedProcess[str]:
    generated = scripts.collector_script(
        base_ref="main",
        work_branch="crucible/test",
        size_cap_bytes=1024,
        **kw,  # type: ignore[arg-type]
    )
    generated = generated.replace(scripts.REPO_MOUNT, str(repo))
    generated = generated.replace(OUTPUT_MOUNT, str(output))
    generated = generated.replace(REPORT_MOUNT, str(report))
    return subprocess.run(
        ["sh", "-c", generated],
        capture_output=True,
        text=True,
        check=False,
        env=collector_env(output.parent),
    )


def test_edits_left_uncommitted_are_committed_as_the_fixed_author_with_the_trailer(
    tmp_path: Path,
) -> None:
    """FDY-0140: a model that forgot to commit still has its edits collected."""
    repo, output, report = _work_repo(tmp_path)
    (repo / "tracked.txt").write_text("edited, never committed\n", encoding="utf-8")
    (repo / "new.txt").write_text("created, never added\n", encoding="utf-8")
    result = _collect(
        repo,
        output,
        report,
        attempt_id="01ATTEMPT",
        author_name="Policy Author",
        author_email="policy@example.invalid",
        commit_trailer="Crucible-Attempt",
        trailer_value="EX-0001",
    )
    assert result.returncode == 0, result.stderr
    changed = set((output / "changed.txt").read_text().replace("\0", " ").split())
    assert {"committed.txt", "tracked.txt", "new.txt"} <= changed
    assert (output / "commits.txt").read_text().strip() == "2"
    assert (output / "leftover-committed.txt").read_text().strip() == "01ATTEMPT"
    shown = subprocess.run(
        [
            "git",
            "log",
            "-1",
            "--format=%an <%ae>%n%cn <%ce>%n%s%n%(trailers:key=Crucible-Attempt,valueonly)",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert shown[0] == shown[1] == ("crucible-worker <crucible-worker@users.noreply.github.com>")
    assert shown[2] == "crucible: commit what attempt 01ATTEMPT left uncommitted"
    # The value the commit hook gives the worker's own commits: the task's external id.
    assert shown[3] == "EX-0001"
    # The bundle carries the extra commit, so it is what the review sees.
    assert (output / "work_branch.bundle").stat().st_size > 0
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert status.stdout == ""


def test_a_clean_tree_gets_no_extra_commit(tmp_path: Path) -> None:
    repo, output, report = _work_repo(tmp_path)
    result = _collect(repo, output, report, attempt_id="01ATTEMPT")
    assert result.returncode == 0, result.stderr
    assert (output / "commits.txt").read_text().strip() == "1"
    assert not (output / "leftover-committed.txt").exists()


def test_changed_files_ignore_commits_main_gained_after_the_fork(tmp_path: Path) -> None:
    repo, output, report = tmp_path / "repo", tmp_path / "output", tmp_path / "report"
    for path in (repo, output, report):
        path.mkdir()
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", *identity, "commit", "-q", "-m", "base"], cwd=repo, check=True)
    (output / "prepared-base.txt").write_text(
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "main"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    subprocess.run(["git", "checkout", "-q", "-b", "crucible/test"], cwd=repo, check=True)
    (repo / "a.txt").write_text("branch work\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", *identity, "commit", "-q", "-m", "branch work"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    (repo / "b.txt").write_text("main moved on\n", encoding="utf-8")
    subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
    subprocess.run(["git", *identity, "commit", "-q", "-m", "main work"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "crucible/test"], cwd=repo, check=True)

    result = _collect(repo, output, report, attempt_id="01ATTEMPT")

    assert result.returncode == 0, result.stderr
    assert set((output / "changed.txt").read_text().replace("\0", " ").split()) == {"a.txt"}
    assert set((output / "commit-paths.txt").read_text().replace("\0", " ").split()) == {"a.txt"}
    for filename in ("diff.patch", "diffstat.txt"):
        diff = (output / filename).read_text()
        assert "a.txt" in diff
        assert "b.txt" not in diff


def test_collector_fails_when_branch_has_no_merge_base(tmp_path: Path) -> None:
    repo, output, report = _work_repo(tmp_path)
    subprocess.run(["git", "checkout", "--orphan", "unrelated"], cwd=repo, check=True)
    subprocess.run(["git", "rm", "-rf", "."], cwd=repo, check=True)
    (repo / "prohibited.txt").write_text("committed prohibited path\n", encoding="utf-8")
    subprocess.run(["git", "add", "prohibited.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "unrelated root",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "branch", "-M", "crucible/test"], cwd=repo, check=True)

    result = _collect(repo, output, report, attempt_id="01ATTEMPT")

    assert result.returncode != 0
    failure = (output / "collection-failed.txt").read_text()
    assert "cannot resolve merge base" in failure
    assert failure in result.stderr
    assert not (output / "collector.ok").exists()
    for filename in ("changed.txt", "diff.patch", "diffstat.txt"):
        assert not (output / filename).exists()


def test_a_redirected_git_dir_skips_the_leftover_commit_without_a_refusal(
    tmp_path: Path,
) -> None:
    """Outside a quota checkpoint, an unsafe `.git` is no checkpoint refusal: the
    uncommitted edit is left, with a note, and nothing is committed through it."""
    repo, output, report = _work_repo(tmp_path)
    (repo / ".git" / "commondir").write_text(str(tmp_path) + "\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
    result = _collect(repo, output, report, attempt_id="01ATTEMPT")
    assert "commondir redirect" in result.stderr
    assert not (output / "checkpoint-refusal.txt").exists()
    assert not (output / "leftover-committed.txt").exists()
    assert result.returncode != 4


def test_build_output_is_never_swept_into_the_leftover_commit(tmp_path: Path) -> None:
    """Caches and build output a repository forgot to ignore stay out of the commit;
    the worker's edit beside them goes in."""
    repo, output, report = _work_repo(tmp_path)
    (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
    for junk in ("src/__pycache__/mod.cpython-312.pyc", "node_modules/x/index.js", ".coverage"):
        (repo / junk).parent.mkdir(parents=True, exist_ok=True)
        (repo / junk).write_text("junk\n", encoding="utf-8")
    result = _collect(repo, output, report, attempt_id="01ATTEMPT")
    assert result.returncode == 0, result.stderr
    changed = set((output / "changed.txt").read_text().replace("\0", " ").split())
    assert "tracked.txt" in changed
    assert not any(
        "__pycache__" in path or "node_modules" in path or path == ".coverage" for path in changed
    ), changed


def test_build_output_the_worker_already_staged_stays_out_of_the_leftover_commit(
    tmp_path: Path,
) -> None:
    """A worker that ran `git add` on build output and then exited without committing:
    the exclusions still hold, because an entry already in the index is unstaged."""
    repo, output, report = _work_repo(tmp_path)
    (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
    for junk in ("node_modules/x/index.js", ".venv/bin/tool", ".coverage"):
        (repo / junk).parent.mkdir(parents=True, exist_ok=True)
        (repo / junk).write_text("junk\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A", "-f", "."], cwd=repo, check=True)
    result = _collect(repo, output, report, attempt_id="01ATTEMPT")
    assert result.returncode == 0, result.stderr
    changed = set((output / "changed.txt").read_text().replace("\0", " ").split())
    assert "tracked.txt" in changed
    assert not any(
        "node_modules" in path or ".venv" in path or path == ".coverage" for path in changed
    ), changed


@pytest.mark.parametrize("breakage", ["missing", "directory"])
def test_a_broken_git_config_skips_the_leftover_commit_and_still_collects(
    tmp_path: Path, breakage: str
) -> None:
    """A worker that removed or replaced `.git/config` loses only the leftover commit,
    with a note the collection keeps; what it committed is still collected."""
    repo, output, report = _work_repo(tmp_path)
    (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
    (repo / ".git" / "config").unlink()
    if breakage == "directory":
        (repo / ".git" / "config").mkdir()
    result = _collect(repo, output, report, attempt_id="01ATTEMPT")
    assert "config is not a regular file" in (output / "leftover-refusal.txt").read_text()
    assert not (output / "checkpoint-refusal.txt").exists()
    if breakage == "missing":
        assert result.returncode == 0, result.stderr
        assert (output / "collector.ok").is_file()
        assert (output / "commits.txt").read_text().strip() == "1"
    else:
        # No git command can read a repository whose config is a directory, so the
        # collection itself fails, as it did before the leftover commit existed.
        assert result.returncode != 0


def test_the_leftover_commit_and_its_note_are_read_back(tmp_path: Path) -> None:
    """FDY-0140: what the collector did with uncommitted work reaches the supervisor,
    which records it on `attempt_collected`."""
    repo, output, report = _work_repo(tmp_path)
    (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
    assert _collect(repo, output, report, attempt_id="01ATTEMPT").returncode == 0
    spec = LaunchSpec(
        attempt_id="01ATTEMPT",
        task_id="01TASK",
        external_id="EX-0001",
        role="implement",
        harness="script-harness",
        model="none",
        image="crucible-worker:test",
        timeout_seconds=600,
        contract=contract_document(),
    )
    outputs = read_outputs(
        output,
        tmp_path / "verify",
        spec=spec,
        bundle_verified=True,
        collector_exit=0,
        verifications=(),
        tail_bytes=1024,
    )
    assert outputs.leftover_committed is True and outputs.leftover_note is None
    (output / "leftover-committed.txt").unlink()
    (output / "leftover-refusal.txt").write_text("uncommitted work was not committed: x\n")
    again = read_outputs(
        output,
        tmp_path / "verify",
        spec=spec,
        bundle_verified=True,
        collector_exit=0,
        verifications=(),
        tail_bytes=1024,
    )
    assert again.leftover_committed is False
    assert again.leftover_note == "uncommitted work was not committed: x"


def test_the_activity_walk_fingerprints_the_worker_trees_and_sees_a_change(
    tmp_path: Path,
) -> None:
    """FDY-0140: what the Kubernetes provider runs in a live worker. A missing home is
    still a whole walk; a change anywhere in the trees changes the answer."""
    repo, report, home = tmp_path / "repo", tmp_path / "report", tmp_path / "home"
    for path in (repo, report, home):
        path.mkdir()
    (home / "state.db").write_bytes(b"turn 1")
    script = scripts.ACTIVITY_SCRIPT.replace(scripts.REPO_MOUNT, str(repo)).replace(
        REPORT_MOUNT, str(report)
    )

    def ask(home_dir: Path) -> tuple[int, int, int] | None:
        result = subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            check=False,
            env={"HOME": str(home_dir), "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        return scripts.parse_activity(result.stdout)

    first = ask(home)
    assert first is not None and first[1] == 4
    assert ask(home) == first
    (home / "state.db").write_bytes(b"turn 2, longer")
    assert ask(home) != first
    assert ask(tmp_path / "absent") is not None
    assert scripts.parse_activity(b"incomplete\n") is None
    assert scripts.parse_activity(b"") is None


def test_a_verification_id_cannot_collide_with_another() -> None:
    """`a/b` and `a_b` are two ids, so they get two files and two exit codes."""
    assert scripts.encode_check_id("a/b") == "a%2Fb"
    assert scripts.encode_check_id("a_b") == "a_b"
    assert scripts.encode_check_id("a/b") != scripts.encode_check_id("a_b")
    assert scripts.encode_check_id("..") not in ("", ".", "..")
    assert "/" not in scripts.encode_check_id("../../etc/passwd")


def test_the_verifier_writes_one_file_per_id(tmp_path: Path) -> None:
    """Two ids that a plain substitution would collapse produce distinct exits."""
    checks = [("a/b", "exit 3"), ("a_b", "exit 4"), ("plain", "exit 0")]
    script = scripts.verifier_script(checks)
    parses(script)
    verify = tmp_path / "verify"
    repo = tmp_path / "repo"
    verify.mkdir()
    repo.mkdir()
    body = script.replace(scripts.VERIFY_MOUNT, str(verify)).replace(scripts.REPO_MOUNT, str(repo))
    subprocess.run(["sh", "-c", body], check=True, capture_output=True)
    exits = {path.stem: path.read_text().strip() for path in verify.glob("*.exit")}
    assert exits == {"a%2Fb": "3", "a_b": "4", "plain": "0"}
    manifest = dict(
        line.split("\t", 1)
        for line in (verify / scripts.MANIFEST).read_text().splitlines()
        if "\t" in line
    )
    assert manifest == {"a%2Fb": "a/b", "a_b": "a_b", "plain": "plain"}


def test_the_verifier_records_each_commands_wall_clock(tmp_path: Path) -> None:
    """hades #184: seconds per command, measured in the verifier, read back as evidence."""
    from crucible.adapters.execution.collected import read_verifications  # noqa: PLC0415

    checks = [("quick", "true"), ("slow", "sleep 1; exit 2")]
    script = scripts.verifier_script(checks)
    parses(script)
    verify = tmp_path / "verify"
    repo = tmp_path / "repo"
    verify.mkdir()
    repo.mkdir()
    body = script.replace(scripts.VERIFY_MOUNT, str(verify)).replace(scripts.REPO_MOUNT, str(repo))
    subprocess.run(["sh", "-c", body], check=True, capture_output=True)
    seconds = {path.stem: int(path.read_text()) for path in verify.glob("*.seconds")}
    assert seconds["quick"] <= 1 and 1 <= seconds["slow"] <= 3
    spec = SimpleNamespace(contract={"required_verification": []})
    runs = {run.id: run for run in read_verifications(verify, spec, checks)}  # type: ignore[arg-type]
    assert runs["slow"].seconds == seconds["slow"] and runs["slow"].exit_code == 2
    (verify / "quick.seconds").write_text("garbage\n")
    (verify / "slow.seconds").unlink()
    runs = {run.id: run for run in read_verifications(verify, spec, checks)}  # type: ignore[arg-type]
    assert runs["quick"].seconds is None and runs["slow"].seconds is None


def test_a_verification_id_that_looks_like_a_command_stays_a_file_name(tmp_path: Path) -> None:
    script = scripts.verifier_script([("x'; touch /tmp/crucible-pwned; '", "exit 7")])
    parses(script)
    assert "touch /tmp/crucible-pwned" in script  # inside the quoted id, as data
    verify = tmp_path / "verify"
    repo = tmp_path / "repo"
    verify.mkdir()
    repo.mkdir()
    body = script.replace(scripts.VERIFY_MOUNT, str(verify)).replace(scripts.REPO_MOUNT, str(repo))
    subprocess.run(["sh", "-c", body], check=True, capture_output=True)
    assert not Path("/tmp/crucible-pwned").exists()
    (exit_file,) = list(verify.glob("*.exit"))
    assert exit_file.read_text().strip() == "7"


def test_a_preparer_reads_the_cache_and_never_refreshes_it() -> None:
    """crucible#55, hades #137: on both providers the refresher is the cache's only
    writer; the preparer script has no fetch or mirror clone to run."""
    common = {
        "url": "https://github.com/example-org/example-service",
        "base_ref": "main",
        "work_branch": "crucible/test",
        "from_remote_branch": False,
        "cache_name": "0123456789abcdef",
        "author_name": "crucible-worker",
        "author_email": "crucible-worker@users.noreply.github.com",
        "origin_placeholder": workspace.ORIGIN_PLACEHOLDER,
        "claude_md_wins": True,
        "shims": workspace.SHIM_NAMES,
        "exclude_entries": workspace.EXCLUDE_ENTRIES,
        "identity_mount": IDENTITY_MOUNT,
    }
    preparer = scripts.preparer_script(**common)  # type: ignore[arg-type]
    cache = "/crucible/cache/0123456789abcdef.git"
    assert "fetch --prune origin" not in preparer and "clone --mirror" not in preparer
    assert f'rm -rf "{cache}"' not in preparer
    assert f"--reference {cache} --dissociate" in preparer

    refresh = scripts.cache_refresh_script(
        url="https://github.com/example-org/example-service", cache_name="0123456789abcdef"
    )
    assert f'--git-dir "{cache}" fetch --prune origin' in refresh
    assert f'clone --mirror -- "$CLONE_URL" "{cache}"' in refresh
    assert "CLONE_URL='https://github.com/example-org/example-service'" in refresh


def _refresh_in(tmp_path: Path, url: str) -> tuple[str, Path]:
    cache = tmp_path / "cache"
    cache.mkdir()
    script = scripts.cache_refresh_script(url=url, cache_name="0123456789abcdef")
    return script.replace(scripts.CACHE_MOUNT, str(cache)), cache / "0123456789abcdef.git"


def test_a_refresh_whose_remote_does_not_answer_costs_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hades #191: an unanswered connect waited out the kernel's SYN retries twice, about
    270 seconds. The refresh gives the remote CACHE_REFRESH_CONNECT_SECONDS to answer a
    ref listing and otherwise leaves the mirror as it is, without fetch or clone."""
    import time  # noqa: PLC0415

    monkeypatch.setattr(scripts, "CACHE_REFRESH_CONNECT_SECONDS", 1)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    stub = bin_dir / "git"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *ls-remote*) exec sleep 30 ;;\n"
        f'  *) echo "$*" >> {calls} ;;\n'
        "esac\n"
    )
    stub.chmod(0o755)
    script, mirror = _refresh_in(tmp_path, "https://github.com/example-org/example-service")
    started = time.monotonic()
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 10
    assert "did not answer within 1s" in result.stderr
    assert not calls.exists() and not mirror.exists()


def test_a_refresh_whose_remote_answers_builds_the_mirror(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    (origin / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qm", "a"],
        cwd=origin,
        check=True,
    )
    script, mirror = _refresh_in(tmp_path, str(origin))
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert (mirror / "HEAD").is_file()
