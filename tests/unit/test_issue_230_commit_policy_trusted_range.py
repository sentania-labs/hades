"""hades #230: the collector's `commit_policy` range must not come from a ref the
worker can move.

Before this fix, `POLICY_FROM` was resolved from `refs/remotes/origin/$WORK_BRANCH`
inside the worker-writable checkout, at collection time, after the worker had run. A
hostile worker could move that ref to `HEAD` (`git update-ref`) and empty the
`POLICY_FROM..HEAD` range, hiding every commit's author from the check. The fix: the
preparer resolves the range's start to a commit id while it is still the checkout's
only writer, and records it in `prepared-policy-from.txt` on the output mount, which
the worker never gets (the same pattern `prepared-base.txt` already uses, 08). The
collector reads only that file.

AC2 proves a sibling defense already on main (hades #344, #369): `GIT_ENV` exports
`GIT_NO_REPLACE_OBJECTS=1` and a nonexistent `GIT_GRAFT_FILE`, so a `refs/replace`
object the worker writes in the checkout cannot change what the collector reads.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from crucible.adapters.execution import scripts, workspace
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    OUTPUT_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    WORK_MOUNT,
)
from tests.collector_tools import collector_env

_POLICY_EMAIL = "crucible-worker@users.noreply.github.com"
_WORK_BRANCH = "crucible/test"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _commit(repo: Path, message: str, *, email: str = _POLICY_EMAIL, name: str = "a") -> str:
    subprocess.run(
        [
            "git",
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "commit",
            "-q",
            "-m",
            message,
        ],
        cwd=repo,
        check=True,
    )
    return _git("rev-parse", "HEAD", cwd=repo).strip()


def _add(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True)


def _render_preparer(
    *, origin: Path, work: Path, from_remote_branch: bool, base_ref: str = "main"
) -> subprocess.CompletedProcess[str]:
    script = scripts.preparer_script(
        url=str(origin),
        base_ref=base_ref,
        work_branch=_WORK_BRANCH,
        from_remote_branch=from_remote_branch,
        cache_name=None,
        author_name="crucible-worker",
        author_email=_POLICY_EMAIL,
        origin_placeholder=workspace.ORIGIN_PLACEHOLDER,
        claude_md_wins=True,
        shims=workspace.SHIM_NAMES,
        exclude_entries=workspace.EXCLUDE_ENTRIES,
        identity_mount=IDENTITY_MOUNT,
    )
    script = script.replace(WORK_MOUNT, str(work))
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)


def _render_collector(
    *, work: Path, report: Path, base_ref: str = "main"
) -> subprocess.CompletedProcess[str]:
    generated = scripts.collector_script(
        base_ref=base_ref,
        work_branch=_WORK_BRANCH,
        size_cap_bytes=1 << 20,
        author_email=_POLICY_EMAIL,
    )
    generated = generated.replace(REPO_MOUNT, str(work / "repo"))
    generated = generated.replace(OUTPUT_MOUNT, str(work / "output"))
    generated = generated.replace(REPORT_MOUNT, str(report))
    return subprocess.run(
        ["sh", "-c", generated],
        capture_output=True,
        text=True,
        check=False,
        env=collector_env(work),
    )


def _base_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    _add(origin, "base.txt", "base\n")
    _commit(origin, "base")
    return origin


def test_moving_the_remote_tracking_ref_does_not_empty_the_commit_policy_range(
    tmp_path: Path,
) -> None:
    """AC1: a worker that moves refs/remotes/origin/<work_branch> to HEAD cannot make
    its own badly authored commit disappear from the range `commit_policy` checks."""
    origin = _base_origin(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", _WORK_BRANCH], cwd=origin, check=True)
    _add(origin, "prior.txt", "prior honest work\n")
    prior_head = _commit(origin, "prior honest work")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=origin, check=True)

    work = tmp_path / "work"
    prep = _render_preparer(origin=origin, work=work, from_remote_branch=True)
    assert prep.returncode == 0, prep.stderr
    recorded = (work / "output" / "prepared-policy-from.txt").read_text().strip()
    assert recorded == prior_head

    repo = work / "repo"
    _add(repo, "hostile.txt", "hostile change\n")
    hostile_head = _commit(repo, "hostile change", email="hostile@evil.test", name="Someone Else")
    # The attack: move the worker-writable remote-tracking ref to HEAD. If the collector
    # still resolved POLICY_FROM from this ref, the range would become HEAD..HEAD: empty.
    subprocess.run(
        ["git", "update-ref", f"refs/remotes/origin/{_WORK_BRANCH}", "HEAD"], cwd=repo, check=True
    )
    assert _git("rev-parse", f"refs/remotes/origin/{_WORK_BRANCH}", cwd=repo).strip() == (
        _git("rev-parse", "HEAD", cwd=repo).strip()
    )

    report = tmp_path / "report"
    report.mkdir()
    result = _render_collector(work=work, report=report)
    assert result.returncode == 0, result.stderr
    assert (work / "output" / "commit-policy" / "checked").is_file()
    problems = (work / "output" / "commit-policy" / "author-problems.txt").read_text()
    assert hostile_head in problems
    assert "hostile@evil.test" in problems


def test_a_replace_object_in_the_checkout_does_not_change_what_the_collector_reads(
    tmp_path: Path,
) -> None:
    """AC2: hades #344/#369's GIT_NO_REPLACE_OBJECTS=1 and nonexistent GIT_GRAFT_FILE
    already on main. A refs/replace object the worker writes swaps a commit's author
    and content for the collector's own tools (`show`, `log`, `diff`) unless replace
    objects are disabled; this proves they are."""
    origin = _base_origin(tmp_path)

    work = tmp_path / "work"
    prep = _render_preparer(origin=origin, work=work, from_remote_branch=False)
    assert prep.returncode == 0, prep.stderr

    repo = work / "repo"
    _add(repo, "secret.txt", "original\n")
    real_head = _commit(repo, "add secret", email=_POLICY_EMAIL, name="Policy Author")

    # A disconnected alternate commit: different author, different content. `git
    # replace` makes every ordinary object lookup of real_head answer with this one
    # instead, unless GIT_NO_REPLACE_OBJECTS disables the substitution.
    subprocess.run(
        ["git", "checkout", "-q", "-b", "throwaway", "refs/remotes/origin/main"],
        cwd=repo,
        check=True,
    )
    (repo / "secret.txt").write_text("tampered\n", encoding="utf-8")
    subprocess.run(["git", "add", "secret.txt"], cwd=repo, check=True)
    alt_head = _commit(repo, "tampered", email="hostile@evil.test", name="Someone Else")
    subprocess.run(["git", "checkout", "-q", _WORK_BRANCH], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-D", "throwaway"], cwd=repo, check=True)
    subprocess.run(["git", "replace", real_head, alt_head], cwd=repo, check=True)
    assert (repo / ".git" / "refs" / "replace" / real_head).is_file()

    report = tmp_path / "report"
    report.mkdir()
    result = _render_collector(work=work, report=report)
    assert result.returncode == 0, result.stderr

    diff = (work / "output" / "diff.patch").read_text()
    log = (work / "output" / "log.txt").read_text()
    assert "+original" in diff
    assert "+tampered" not in diff
    assert "hostile@evil.test" not in log
    problems = (work / "output" / "commit-policy" / "author-problems.txt").read_text()
    assert problems.strip() == ""


def test_an_honest_fresh_start_still_checks_the_whole_range_from_base(tmp_path: Path) -> None:
    """AC3: an attempt starting fresh from base_ref (no prior published work) gets the
    same result as before the fix: the range is base_ref..HEAD, so a bad author is
    still found."""
    origin = _base_origin(tmp_path)

    work = tmp_path / "work"
    prep = _render_preparer(origin=origin, work=work, from_remote_branch=False)
    assert prep.returncode == 0, prep.stderr
    base_sha = (work / "output" / "prepared-base.txt").read_text().strip()
    assert (work / "output" / "prepared-policy-from.txt").read_text().strip() == base_sha

    repo = work / "repo"
    _add(repo, "work.txt", "worker change\n")
    bad_head = _commit(
        repo, "worker change", email="someone-else@example.invalid", name="Someone Else"
    )

    report = tmp_path / "report"
    report.mkdir()
    result = _render_collector(work=work, report=report)
    assert result.returncode == 0, result.stderr
    assert (work / "output" / "commit-policy" / "checked").is_file()
    problems = (work / "output" / "commit-policy" / "author-problems.txt").read_text()
    assert bad_head in problems


def test_an_honest_resumed_run_does_not_reflag_an_already_published_commit(
    tmp_path: Path,
) -> None:
    """AC3: a correction resumed from the published branch gets the same result as
    before the fix: only the commits added since the recorded start are checked, so an
    already-published (and already-reviewed) commit is not re-flagged."""
    origin = _base_origin(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", _WORK_BRANCH], cwd=origin, check=True)
    _add(origin, "prior.txt", "prior\n")
    _commit(origin, "prior work", email="prior-reviewer@example.invalid", name="Prior Reviewer")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=origin, check=True)

    work = tmp_path / "work"
    prep = _render_preparer(origin=origin, work=work, from_remote_branch=True)
    assert prep.returncode == 0, prep.stderr

    repo = work / "repo"
    _add(repo, "new.txt", "new honest work\n")
    _commit(repo, "new honest work", email=_POLICY_EMAIL, name="crucible-worker")

    report = tmp_path / "report"
    report.mkdir()
    result = _render_collector(work=work, report=report)
    assert result.returncode == 0, result.stderr
    assert (work / "output" / "commit-policy" / "checked").is_file()
    problems = (work / "output" / "commit-policy" / "author-problems.txt").read_text()
    assert problems.strip() == ""
