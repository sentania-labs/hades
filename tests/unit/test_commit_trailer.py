"""Crucible puts the attempt trailer on the worker's commits as a courtesy, the collector
records who authored them for the reviewer, and accepted commits need neither
(hades FDY-0135, FDY-0143: the operator's decision of 2026-09-29).

These run real git against a scratch repository: the hook as the identity bundle writes
it, the collector script as the collector container runs it, and the publisher script
pushing into a local bare repository."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from crucible.adapters.execution import identity, kubernetes, scripts
from crucible.adapters.execution.collected import read_commit_policy
from crucible.adapters.execution.publisher import outcome_from_files
from crucible.contracts.policy import Gates
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    OUTPUT_MOUNT,
    REPORT_MOUNT,
    CommitPolicyCheck,
)
from tests.fixtures import contract_document

AUTHOR = "crucible-worker@users.noreply.github.com"
POLICY = {"git": {"author_name": "crucible-worker", "author_email": AUTHOR}}


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env
    ).stdout


def _bundle(tmp_path: Path, external_id: str = "HT-0007") -> Path:
    directory = tmp_path / "identity"
    identity.write_bundle(
        directory,
        contract=contract_document(),
        policy=POLICY,
        external_id=external_id,
        owner="foundry",
        work_branch="crucible/test",
        network_mode="none",
        report_schema={},
    )
    return directory


def _checkout(tmp_path: Path, hooks: Path | None) -> Path:
    """A checkout configured the way the preparer leaves it: the policy's author, and
    core.hooksPath at the identity bundle's hook directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "crucible-worker")
    _git(repo, "config", "user.email", AUTHOR)
    if hooks is not None:
        _git(repo, "config", "core.hooksPath", str(hooks))
    return repo


def _commit(repo: Path, name: str, *messages: str, env: dict[str, str] | None = None) -> None:
    (repo / name).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", name, env=env)
    args = [part for message in messages for part in ("-m", message)]
    _git(repo, "commit", "-q", *args, env=env)


def _trailers(repo: Path, rev: str = "HEAD") -> list[str]:
    out = _git(repo, "show", "-s", "--format=%(trailers:key=Crucible-Attempt,valueonly)", rev)
    return [line for line in out.splitlines() if line.strip()]


def _git_only_path(tmp_path: Path) -> dict[str, str]:
    """An environment whose PATH holds git and nothing else: the hook can reach no curl,
    no ssh and no other program, so whatever it does, it does with git alone."""
    bin_dir = tmp_path / "git-only"
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    (bin_dir / "git").symlink_to(git)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PATH"] = str(bin_dir)
    # Nothing it could use to reach a network either way.
    env["http_proxy"] = env["https_proxy"] = "http://127.0.0.1:9"
    return env


def test_the_bundle_carries_an_executable_read_only_commit_msg_hook(tmp_path: Path) -> None:
    hook = _bundle(tmp_path) / scripts.COMMIT_HOOK_DIR / "commit-msg"
    assert hook.stat().st_mode & 0o777 == 0o555
    text = hook.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert "KEY='Crucible-Attempt'" in text
    assert "VALUE='HT-0007'" in text
    # The only program it runs is git's own trailer editor, on the message file.
    commands = [
        line
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" not in line.split(" ")[0]
    ]
    assert [c for c in commands if "git" in c] == [
        "exec git interpret-trailers --in-place --no-divider --if-exists doNothing \\"
    ]
    assert '  --if-missing add --trailer "$KEY: $VALUE" "$1"' in text


def test_the_hook_value_is_data_whatever_the_external_id_holds(tmp_path: Path) -> None:
    hostile = "HT-1'; touch /tmp/crucible-pwned; '"
    text = (_bundle(tmp_path, hostile) / "hooks" / "commit-msg").read_text(encoding="utf-8")
    assert scripts._quote(hostile) in text


def test_the_hook_adds_the_trailer_once(tmp_path: Path) -> None:
    hooks = _bundle(tmp_path) / scripts.COMMIT_HOOK_DIR
    repo = _checkout(tmp_path, hooks)
    env = _git_only_path(tmp_path)
    _commit(repo, "greet.sh", "Add greet.sh, tests/test_greet.sh, and Makefile", env=env)
    assert _trailers(repo) == ["HT-0007"]
    message = _git(repo, "show", "-s", "--format=%B", "HEAD")
    assert message.rstrip("\n").endswith("\n\nCrucible-Attempt: HT-0007")
    # An amend runs the hook again and finds the trailer already there.
    _git(repo, "commit", "-q", "--amend", "--no-edit", env=env)
    assert _trailers(repo) == ["HT-0007"]
    _git(repo, "commit", "-q", "--amend", "-m", "Reworded", env=env)
    assert _trailers(repo) == ["HT-0007"]


def test_the_hook_keeps_a_trailer_the_worker_already_wrote(tmp_path: Path) -> None:
    hooks = _bundle(tmp_path) / scripts.COMMIT_HOOK_DIR
    repo = _checkout(tmp_path, hooks)
    env = _git_only_path(tmp_path)
    _commit(repo, "a.txt", "Add a", "Crucible-Attempt: HT-0007", env=env)
    assert _trailers(repo) == ["HT-0007"]
    # A harness that wrote the key with another value keeps its own; never a second one.
    _commit(repo, "b.txt", "Add b", "Crucible-Attempt: attempt-42", env=env)
    assert _trailers(repo) == ["attempt-42"]
    # The key only in prose is not a trailer, so the hook still adds one.
    _commit(repo, "c.txt", "Add c", "Crucible-Attempt: in prose\nand more text", env=env)
    assert _trailers(repo) == ["HT-0007"]


def test_the_hook_is_skipped_by_no_verify(tmp_path: Path) -> None:
    """The hook is a courtesy: `--no-verify` skips it, and since FDY-0143 nothing checks
    the trailer, so a commit without it is still published."""
    hooks = _bundle(tmp_path) / scripts.COMMIT_HOOK_DIR
    repo = _checkout(tmp_path, hooks)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "--no-verify", "-m", "Add a")
    assert _trailers(repo) == []


def test_the_preparer_points_the_checkout_at_crucibles_hooks_only() -> None:
    from tests.unit.test_scripts import preparer  # noqa: PLC0415

    script = preparer("crucible/test")
    assert 'config core.hooksPath "$IDENTITY_MOUNT/hooks"' in script
    assert f"IDENTITY_MOUNT='{IDENTITY_MOUNT}'" in script
    # The preparer's own git still runs with no hooks at all.
    assert "-c core.hooksPath=/dev/null" in script


def test_identity_md_says_only_commit_on_the_branch(tmp_path: Path) -> None:
    """FDY-0143: the worker is told to commit on its branch, and nothing about the
    trailer, the author, hooks or `--no-verify`."""
    text = (_bundle(tmp_path) / "IDENTITY.md").read_text(encoding="utf-8")
    # FDY-0140 made it a line of the Scope section; FDY-0143 made it this one line.
    section = text.split("## Scope", 1)[1].split("## ", 1)[0]
    assert "- Commit your work on `crucible/test`; never push." in section
    for word in ("trailer", "Crucible-Attempt", "author", "hook", "--no-verify"):
        assert word not in section
    policy_md = (tmp_path / "identity" / "policy.md").read_text(encoding="utf-8")
    assert "- commit_policy" in policy_md


def test_kubernetes_projects_the_hook_executable_and_the_rest_read_only() -> None:
    assert kubernetes._identity_item("hooks__commit-msg", "hooks/commit-msg") == {
        "key": "hooks__commit-msg",
        "path": "hooks/commit-msg",
        "mode": 0o555,
    }
    assert kubernetes._identity_item("IDENTITY.md", "IDENTITY.md") == {
        "key": "IDENTITY.md",
        "path": "IDENTITY.md",
    }


def test_a_policy_cannot_list_the_enforced_gate() -> None:
    with pytest.raises(ValueError, match="always run before review"):
        Gates(pre_pr=["commit_policy"], publication=[], post_pr=[], skipped=[])


# ----- the collector records authors for the reviewer ------------------------------


def _collect(tmp_path: Path, repo: Path) -> Path:
    output = tmp_path / "output"
    report = tmp_path / "report"
    output.mkdir()
    report.mkdir()
    # Stand in for the trusted record left by preparation in these collector fixtures.
    (output / "prepared-base.txt").write_text(
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "main"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    generated = scripts.collector_script(
        base_ref="main",
        work_branch="crucible/test",
        size_cap_bytes=1024,
        author_email=AUTHOR,
    )
    generated = generated.replace(scripts.REPO_MOUNT, str(repo))
    generated = generated.replace(OUTPUT_MOUNT, str(output))
    generated = generated.replace(REPORT_MOUNT, str(report))
    result = subprocess.run(["sh", "-c", generated], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return output


def _worker_branch(tmp_path: Path) -> Path:
    repo = _checkout(tmp_path, None)
    _git(
        repo,
        "-c",
        "user.email=someone@upstream.test",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    _git(repo, "checkout", "-q", "-b", "crucible/test")
    return repo


def test_a_commit_without_the_trailer_is_no_problem_at_collection(tmp_path: Path) -> None:
    repo = _worker_branch(tmp_path)
    _commit(repo, "greet.sh", "Add greet.sh, tests/test_greet.sh, and Makefile")
    output = _collect(tmp_path, repo)
    check = read_commit_policy(output / "commit-policy")
    assert check == CommitPolicyCheck()
    assert not (output / "commit-policy" / "trailer-problems.txt").exists()


def test_the_collector_records_a_commit_by_another_author(tmp_path: Path) -> None:
    repo = _worker_branch(tmp_path)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(
        repo,
        "-c",
        "user.email=someone@elsewhere.test",
        "commit",
        "-q",
        "-m",
        "Add a",
        "-m",
        "Crucible-Attempt: HT-0007",
    )
    head = _git(repo, "rev-parse", "HEAD").strip()
    check = read_commit_policy(_collect(tmp_path, repo) / "commit-policy")
    assert check is not None
    assert check.author_problems == ((head, "someone@elsewhere.test"),)


def test_the_collector_passes_commits_the_hook_made(tmp_path: Path) -> None:
    repo = _worker_branch(tmp_path)
    _git(repo, "config", "core.hooksPath", str(_bundle(tmp_path) / "hooks"))
    _commit(repo, "a.txt", "Add a")
    _commit(repo, "b.txt", "Add b")
    output = _collect(tmp_path, repo)
    check = read_commit_policy(output / "commit-policy")
    assert check is not None
    assert check.author_problems == ()
    # The base commit, by someone else, is not this attempt's work.
    assert (output / "commits.txt").read_text().strip() == "2"


def test_an_unfinished_check_reads_as_not_checked(tmp_path: Path) -> None:
    directory = tmp_path / "commit-policy"
    directory.mkdir()
    (directory / "author-problems.txt").write_text("", encoding="utf-8")
    assert read_commit_policy(directory) is None
    assert read_commit_policy(tmp_path / "absent") is None


def test_the_publisher_runs_no_commit_check() -> None:
    """Accepted commits have no author or trailer check. Issue 403 checks the remote tip."""
    publisher = scripts.publisher_script(
        clone_url="https://github.com/o/r.git",
        work_branch="crucible/test",
        base_ref="main",
        expected_head="a" * 40,
        author_name="crucible-worker",
        author_email=AUTHOR,
    )
    collector = scripts.collector_script(
        base_ref="main", work_branch="crucible/test", size_cap_bytes=1024
    )
    assert "commit_policy_check" not in publisher
    assert "TRAILER" not in publisher
    assert '%(trailers:key=Crucible-Attempt,valueonly)\' "$REMOTE"' in publisher
    assert "exit 6" not in publisher
    assert scripts._commit_policy_check(scripts.GIT + ' -C "$REPO"') in collector


def test_the_hook_puts_the_trailer_last_even_after_a_dashed_line(tmp_path: Path) -> None:
    """A `---` line in a body is prose. Without `--no-divider`, git reads it as the start
    of a patch and puts the trailer above it, where no check finds it."""
    hooks = _bundle(tmp_path) / scripts.COMMIT_HOOK_DIR
    repo = _checkout(tmp_path, hooks)
    _commit(repo, "a.txt", "Add a", "Notes:\n\n---\n\nMore notes", env=_git_only_path(tmp_path))
    assert _trailers(repo) == ["HT-0007"]
    message = _git(repo, "show", "-s", "--format=%B", "HEAD")
    assert message.rstrip("\n").endswith("More notes\n\nCrucible-Attempt: HT-0007")


def test_the_check_reports_a_range_it_cannot_read_as_not_checked(tmp_path: Path) -> None:
    """A check that did not run is never read as one that found nothing."""
    repo = _worker_branch(tmp_path)
    _commit(repo, "a.txt", "Add a")
    # A remote-tracking ref that names an object the checkout does not have.
    (repo / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True)
    (repo / ".git" / "refs" / "remotes" / "origin" / "crucible" / "test").parent.mkdir()
    (repo / ".git" / "refs" / "remotes" / "origin" / "crucible" / "test").write_text(
        "f" * 40 + "\n", encoding="utf-8"
    )
    output = _collect(tmp_path, repo)
    assert read_commit_policy(output / "commit-policy") is None


def _run_publisher(tmp_path: Path, repo: Path, origin: Path, head: str) -> tuple[int, Path]:
    """The real publisher script, with its container paths moved under `tmp_path`,
    pushing the collected bundle into a local bare repository."""
    root = tmp_path / "publisher"
    for name in ("tmp", "token", "publish", "bundle", "home"):
        (root / name).mkdir(parents=True)
    bundle = root / "bundle" / "work_branch.bundle"
    _git(repo, "bundle", "create", str(bundle), "main..crucible/test")
    script = scripts.publisher_script(
        clone_url="CRUCIBLE-TEST-ORIGIN",
        work_branch="crucible/test",
        base_ref="main",
        expected_head=head,
        author_name="crucible-worker",
        author_email=AUTHOR,
        bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
    )
    # `/tmp/` first: the moved paths below live under a temporary directory themselves.
    script = script.replace("/tmp/", f"{root}/tmp/")
    script = script.replace(scripts.TOKEN_MOUNT, str(root / "token"))
    script = script.replace(scripts.PUBLISH_MOUNT, str(root / "publish"))
    script = script.replace(scripts.BUNDLE_MOUNT, str(root / "bundle"))
    script = script.replace("/home/worker", str(root / "home"))
    script = script.replace("CRUCIBLE-TEST-ORIGIN", str(origin))
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["sh", "-c", script],
        input="a-test-token-value",
        # A worker checkout clears credential.helper locally. The publisher runs
        # outside that checkout, so its isolated global helper must be the one read.
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return result.returncode, root / "publish"


def test_a_bundle_without_the_trailer_by_another_author_publishes(tmp_path: Path) -> None:
    """FDY-0143: the publisher pushes the sealed bundle at the accepted head whatever the
    commits' author or trailer; the author difference is for the reviewer, before."""
    repo = _worker_branch(tmp_path)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(repo, "push", "-q", str(origin), "main")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "-c", "user.email=someone@elsewhere.test", "commit", "-q", "-m", "Add a")
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert _trailers(repo) == []
    check = read_commit_policy(_collect(tmp_path, repo) / "commit-policy")
    assert check is not None
    assert check.author_problems == ((head, "someone@elsewhere.test"),)
    code, out = _run_publisher(tmp_path, repo, origin, head)
    files = {p.name: p.read_text(encoding="utf-8") for p in out.iterdir() if p.is_file()}
    assert code == 0, files
    outcome = outcome_from_files(files, code)
    assert outcome.pushed
    assert outcome.step == "done"
    assert outcome.head_sha == head
    assert _git(origin, "rev-parse", "refs/heads/crucible/test").strip() == head


def test_the_publisher_still_refuses_a_bundle_that_is_not_the_accepted_head(
    tmp_path: Path,
) -> None:
    """The guarantees that stay: the sealed bundle, at the head Crucible accepted."""
    repo = _worker_branch(tmp_path)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(repo, "push", "-q", str(origin), "main")
    _commit(repo, "a.txt", "Add a")
    code, out = _run_publisher(tmp_path, repo, origin, "f" * 40)
    assert code == 4
    assert (out / "step.txt").read_text(encoding="utf-8").strip() == "head-mismatch"
    assert _git(origin, "branch", "--list", "crucible/test").strip() == ""


def test_the_collector_never_verifies_a_signature_a_worker_planted(tmp_path: Path) -> None:
    """A worker-written `.git/config` with `log.showSignature` and a `gpg.program` of its
    choosing must not run that program from the collector's `git show`."""
    repo = _worker_branch(tmp_path)
    sentinel = tmp_path / "gpg-ran"
    program = tmp_path / "fake-gpg"
    program.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 1\n", encoding="utf-8")
    program.chmod(0o755)
    _commit(repo, "a.txt", "Add a", "Crucible-Attempt: HT-0007")
    # A commit object with a gpgsig header, written by hand.
    raw = _git(repo, "cat-file", "commit", "HEAD")
    header, _, message = raw.partition("\n\n")
    signature = "-----BEGIN PGP SIGNATURE-----\n x\n -----END PGP SIGNATURE-----"
    signed = f"{header}\ngpgsig {signature}\n\n{message}"
    sha = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=repo,
        input=signed,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(repo, "update-ref", "refs/heads/crucible/test", sha)
    _git(repo, "config", "log.showSignature", "true")
    _git(repo, "config", "gpg.program", str(program))
    check = read_commit_policy(_collect(tmp_path, repo) / "commit-policy")
    assert check is not None
    assert not sentinel.exists()
