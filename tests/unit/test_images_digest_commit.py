"""CI owns the *_DIGEST lines of images/manifest.env (FDY-0310).

On a branch, an images build that reproduces every tag and harness version but not a
digest writes the digests back and commits them as the CI bot; a tag or harness
mismatch still fails, main and tags never commit, and the bot's own commit never
commits again.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "images" / "digest_commit.py"
IMAGES_SH = REPOSITORY / "tools" / "images" / "images.sh"
CI = REPOSITORY / ".github" / "workflows" / "ci.yml"
RELEASE = REPOSITORY / ".github" / "workflows" / "release.yml"
SPEC = REPOSITORY / "docs" / "spec" / "13-local-operation.md"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("digest_commit", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The dataclass decorator looks its module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


digest_commit = _load()

MANIFEST = (REPOSITORY / "images" / "manifest.env").read_text(encoding="utf-8")
OTHER_DIGEST = "sha256:" + "1" * 64


def with_line(text: str, key: str, value: str) -> str:
    return re.sub(rf"^{key}=.*$", f"{key}={value}", text, count=1, flags=re.MULTILINE)


# The comparison.


def test_an_identical_manifest_is_reproduced_with_nothing_to_commit() -> None:
    comparison = digest_commit.compare(MANIFEST, MANIFEST)
    assert comparison.inputs_reproduced
    assert comparison.changed_digests == ()
    assert not comparison.digests_only


def test_a_digest_only_difference_names_the_images() -> None:
    built = with_line(MANIFEST, "WORKER_DIGEST", OTHER_DIGEST)
    comparison = digest_commit.compare(MANIFEST, built)
    assert comparison.digests_only
    assert comparison.changed_digests == ("WORKER",)

    both = with_line(built, "SCRIPT_HARNESS_DIGEST", OTHER_DIGEST)
    assert digest_commit.compare(MANIFEST, both).changed_digests == ("SCRIPT_HARNESS", "WORKER")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("WORKER", "crucible-worker:20260101-000000000000"),
        ("SCRIPT_HARNESS", "crucible-worker:script-harness-1.0.0-000000000000"),
        ("WORKER_HARNESSES", "agy:9.9.9,claude_code:2.1.280,codex:0.156.0,hermes:0.19.0"),
        ("SCRIPT_HARNESS_HARNESSES", "script-harness:9.9.9"),
    ],
)
def test_a_tag_or_harness_difference_is_a_build_input_mismatch(key: str, value: str) -> None:
    built = with_line(with_line(MANIFEST, "WORKER_DIGEST", OTHER_DIGEST), key, value)
    comparison = digest_commit.compare(MANIFEST, built)
    assert not comparison.inputs_reproduced
    assert not comparison.digests_only


def test_an_added_removed_or_renamed_entry_is_a_mismatch() -> None:
    assert not digest_commit.compare(MANIFEST, MANIFEST + "EXTRA=1\n").inputs_reproduced
    trimmed = "".join(
        line for line in MANIFEST.splitlines(keepends=True) if "WORKER_DIGEST" not in line
    )
    assert not digest_commit.compare(MANIFEST, trimmed).inputs_reproduced
    renamed = MANIFEST.replace("WORKER_DIGEST=", "WORKER_DIGESTS=")
    assert not digest_commit.compare(MANIFEST, renamed).inputs_reproduced
    reheadered = MANIFEST.replace("# Written by", "# Typed by", 1)
    assert not digest_commit.compare(MANIFEST, reheadered).inputs_reproduced


def test_the_commit_message_names_the_images_and_carries_the_trailer() -> None:
    message = digest_commit.commit_message(("SCRIPT_HARNESS", "WORKER"))
    subject = message.splitlines()[0]
    assert "script-harness" in subject
    assert "worker" in subject
    assert digest_commit.TRAILER in message.splitlines()
    assert chr(0x2014) not in message  # no em-dash (CONTRIBUTING)


def test_apply_writes_only_digests_and_refuses_anything_else(tmp_path: Path) -> None:
    declared = tmp_path / "manifest.env"
    built = tmp_path / "built.env"
    declared.write_text(MANIFEST, encoding="utf-8")
    built.write_text(with_line(MANIFEST, "WORKER_DIGEST", OTHER_DIGEST), encoding="utf-8")
    assert digest_commit.main(["apply", str(declared), str(built)]) == 0
    assert declared.read_text(encoding="utf-8") == built.read_text(encoding="utf-8")

    declared.write_text(MANIFEST, encoding="utf-8")
    built.write_text(
        with_line(with_line(MANIFEST, "WORKER_DIGEST", OTHER_DIGEST), "WORKER", "x:y"),
        encoding="utf-8",
    )
    assert digest_commit.main(["apply", str(declared), str(built)]) == 1
    assert declared.read_text(encoding="utf-8") == MANIFEST


# Where it may commit.


def test_a_branch_push_or_dispatch_may_commit() -> None:
    for event in ("push", "workflow_dispatch"):
        allowed, _ = digest_commit.may_commit(
            event=event, ref="refs/heads/crucible/FDY-0310", head_message="Change a thing\n"
        )
        assert allowed


@pytest.mark.parametrize(
    ("event", "ref"),
    [
        ("push", "refs/heads/main"),
        ("workflow_dispatch", "refs/heads/main"),
        ("push", "refs/tags/v1.2.3"),
        ("pull_request", "refs/pull/12/merge"),
        ("schedule", "refs/heads/crucible/FDY-0310"),
    ],
)
def test_main_tags_and_other_events_never_commit(event: str, ref: str) -> None:
    allowed, reason = digest_commit.may_commit(event=event, ref=ref, head_message="x\n")
    assert not allowed
    assert reason


def test_the_default_branch_is_whatever_the_repository_names() -> None:
    allowed, _ = digest_commit.may_commit(
        event="push", ref="refs/heads/trunk", head_message="x", default_branch="trunk"
    )
    assert not allowed


def test_the_bots_own_digest_commit_never_commits_again() -> None:
    message = digest_commit.commit_message(("WORKER",))
    allowed, reason = digest_commit.may_commit(
        event="push", ref="refs/heads/crucible/FDY-0310", head_message=message
    )
    assert not allowed
    assert "own digest commit" in reason
    # The CLI the workflow calls says the same with its exit status.
    assert (
        digest_commit.main(
            [
                "may-commit",
                "--event",
                "workflow_dispatch",
                "--ref",
                "refs/heads/crucible/FDY-0310",
                "--head-message",
                message,
            ]
        )
        == 1
    )


# The commit itself.


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, text=True, capture_output=True
    ).stdout


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repository = tmp_path / "checkout"
    (repository / "images").mkdir(parents=True)
    (repository / "images" / "manifest.env").write_text(MANIFEST, encoding="utf-8")
    (repository / "README.md").write_text("readme\n", encoding="utf-8")
    git(repository, "init", "--quiet", "--initial-branch=crucible/FDY-0310")
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=someone",
        "-c",
        "user.email=someone@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "Start",
    )
    return repository


def test_the_commit_touches_only_digest_lines_as_the_bot(checkout: Path) -> None:
    manifest = checkout / "images" / "manifest.env"
    manifest.write_text(with_line(MANIFEST, "WORKER_DIGEST", OTHER_DIGEST), encoding="utf-8")

    assert digest_commit.commit(checkout) == ("WORKER",)

    assert git(checkout, "rev-list", "--count", "HEAD") == "2\n"
    assert git(checkout, "log", "-1", "--format=%an <%ae>") == (
        f"{digest_commit.BOT_NAME} <{digest_commit.BOT_EMAIL}>\n"
    )
    assert git(checkout, "show", "--format=", "--name-only", "HEAD") == "images/manifest.env\n"
    changed = [
        line
        for line in git(checkout, "show", "--format=", "--unified=0", "HEAD").splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert changed == [
        "-" + next(line for line in MANIFEST.splitlines() if line.startswith("WORKER_DIGEST=")),
        f"+WORKER_DIGEST={OTHER_DIGEST}",
    ]
    head_message = git(checkout, "log", "-1", "--format=%B")
    assert "worker" in head_message.splitlines()[0]
    # The head is now the bot's own commit: the next run does not commit again.
    allowed, _ = digest_commit.may_commit(
        event="workflow_dispatch", ref="refs/heads/crucible/FDY-0310", head_message=head_message
    )
    assert not allowed
    assert git(checkout, "status", "--porcelain") == ""


def test_nothing_to_commit_is_not_an_error(checkout: Path) -> None:
    assert digest_commit.commit(checkout) == ()
    assert git(checkout, "rev-list", "--count", "HEAD") == "1\n"


def test_the_commit_refuses_a_tag_change(checkout: Path) -> None:
    manifest = checkout / "images" / "manifest.env"
    manifest.write_text(with_line(MANIFEST, "WORKER", "crucible-worker:other"), encoding="utf-8")
    with pytest.raises(ValueError, match="more than its digest lines"):
        digest_commit.commit(checkout)
    assert git(checkout, "rev-list", "--count", "HEAD") == "1\n"


def test_the_commit_refuses_any_other_changed_file(checkout: Path) -> None:
    manifest = checkout / "images" / "manifest.env"
    manifest.write_text(with_line(MANIFEST, "WORKER_DIGEST", OTHER_DIGEST), encoding="utf-8")
    (checkout / "README.md").write_text("changed\n", encoding="utf-8")
    assert digest_commit.main(["commit", "--repository", str(checkout)]) == 1
    (checkout / "README.md").write_text("readme\n", encoding="utf-8")
    (checkout / "stray.txt").write_text("new\n", encoding="utf-8")
    assert digest_commit.main(["commit", "--repository", str(checkout)]) == 1
    assert git(checkout, "rev-list", "--count", "HEAD") == "1\n"


# images.sh check end to end, with the Docker shim check-manifest.sh uses: it runs the
# real build.sh tag and harness calculation and reports an all-zero image, whose OCI
# digest is never the one the manifest records. So the build reproduces every tag and
# harness version and differs only in its digests, the case CI commits.


def shim(target: Path) -> Path:
    source = (REPOSITORY / "images" / "check-manifest.sh").read_text(encoding="utf-8")
    body = source.split("cat > \"$tmp/bin/docker\" <<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
    bin_dir = target / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(body + "\n", encoding="utf-8")
    docker.chmod(0o755)
    return bin_dir


@pytest.fixture
def staged_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    shutil.copytree(
        REPOSITORY / "images", repository / "images", ignore=shutil.ignore_patterns("out")
    )
    (repository / "tools" / "images").mkdir(parents=True)
    for script in (IMAGES_SH, SCRIPT):
        shutil.copy2(script, repository / "tools" / "images" / script.name)
    return repository


def images_check(
    repository: Path, tmp_path: Path, writeback: str
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PATH": f"{shim(tmp_path)}{os.pathsep}{os.environ['PATH']}",
        "DIGEST_WRITEBACK": writeback,
        "IMAGES_STAGE_ROOT": str(tmp_path),
        "DOCKER": "docker",
    }
    return subprocess.run(
        [str(repository / "tools" / "images" / "images.sh"), "check"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_images_check_without_writeback_fails_on_a_digest_as_before(
    staged_repository: Path, tmp_path: Path
) -> None:
    result = images_check(staged_repository, tmp_path, writeback="")
    assert result.returncode == 1, result.stderr
    assert "does not reproduce images/manifest.env" in result.stderr
    assert (staged_repository / "images" / "manifest.env").read_text(encoding="utf-8") == MANIFEST


def test_images_check_with_writeback_passes_and_writes_only_the_digests(
    staged_repository: Path, tmp_path: Path
) -> None:
    result = images_check(staged_repository, tmp_path, writeback="1")
    assert result.returncode == 0, result.stderr
    written = (staged_repository / "images" / "manifest.env").read_text(encoding="utf-8")
    comparison = digest_commit.compare(MANIFEST, written)
    assert comparison.digests_only
    assert set(comparison.changed_digests) == {"WORKER", "SCRIPT_HARNESS"}
    assert "only digests differed" in result.stdout


def test_images_check_with_writeback_still_fails_on_a_harness_mismatch(
    staged_repository: Path, tmp_path: Path
) -> None:
    manifest = staged_repository / "images" / "manifest.env"
    stale = with_line(MANIFEST, "SCRIPT_HARNESS_HARNESSES", "script-harness:0.0.1")
    manifest.write_text(stale, encoding="utf-8")
    result = images_check(staged_repository, tmp_path, writeback="1")
    assert result.returncode == 1
    assert "does not reproduce images/manifest.env" in result.stderr
    assert manifest.read_text(encoding="utf-8") == stale


def test_images_check_with_writeback_still_fails_on_a_tag_mismatch(
    staged_repository: Path, tmp_path: Path
) -> None:
    manifest = staged_repository / "images" / "manifest.env"
    stale = with_line(MANIFEST, "WORKER", "crucible-worker:20260101-000000000000")
    manifest.write_text(stale, encoding="utf-8")
    result = images_check(staged_repository, tmp_path, writeback="1")
    assert result.returncode == 1
    assert manifest.read_text(encoding="utf-8") == stale


# The workflows.


def images_job() -> dict[str, Any]:
    document = yaml.safe_load(CI.read_text(encoding="utf-8"))
    job: dict[str, Any] = document["jobs"]["images"]
    return job


def step(job: dict[str, Any], name: str) -> dict[str, Any]:
    found: dict[str, Any] = next(item for item in job["steps"] if item.get("name") == name)
    return found


def test_the_images_job_commits_only_through_the_guarded_steps() -> None:
    job = images_job()
    assert job["permissions"] == {"contents": "write", "actions": "write"}
    assert job["steps"][0]["with"]["persist-credentials"] is False

    decide = step(job, "decide whether this run may commit corrected digests")
    assert "digest_commit.py may-commit" in decide["run"]
    assert "github.event.repository.default_branch" in decide["env"]["DEFAULT_BRANCH"]

    build = step(job, "build both images and compare with images/manifest.env")
    assert build["env"]["DIGEST_WRITEBACK"] == "${{ steps.writeback.outputs.value }}"
    assert 'DIGEST_WRITEBACK="$DIGEST_WRITEBACK"' in build["run"]

    commit = step(job, "commit the corrected digests to the branch")
    assert commit["if"] == "steps.writeback.outputs.value == '1'"
    assert "digest_commit.py commit" in commit["run"]
    assert "--force" not in commit["run"]
    assert "HEAD:refs/heads/${BRANCH}" in commit["run"]

    dispatch = step(job, "run CI on the digest commit")
    assert dispatch["if"] == "steps.digest_commit.outputs.committed == 'true'"
    assert "gh workflow run ci.yml" in dispatch["run"]


def test_ci_never_runs_on_tags_and_the_release_never_writes_back() -> None:
    document = yaml.safe_load(CI.read_text(encoding="utf-8"))
    # PyYAML reads the bare `on` key as True.
    triggers = document[True]
    assert "tags" not in triggers["push"]
    release = RELEASE.read_text(encoding="utf-8")
    assert "make images-check NO_CACHE=1" in release
    assert "DIGEST_WRITEBACK" not in release
    assert "digest_commit" not in release


def test_the_spec_says_ci_owns_the_digest_lines() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    assert "**CI owns the digest lines**" in spec
    assert "Humans and workers never edit them" in spec
