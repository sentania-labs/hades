"""hades #447: Hades renumbers a branch's new migrations at merge-main and holds a pull
request whose schema changes overlap another's.

AC1 runs the real merge-main script against a bare origin: a branch migration numbered
like one main already has is renumbered past main's highest, its down_revision points
at main's head, and Crucible's own commit is pushed. AC2 and AC3 drive the delivery
coordinator over the #411 fixtures (fake GitHub, fake publisher): two open pull requests
whose migrations touch the same table hold the later one with a `schema_overlap` wake the
Board shows naming the first, and the held one gets merge-main once the first merged;
two touching different tables are not held.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

from crucible.adapters.execution import scripts
from crucible.adapters.execution.publisher import merge_outcome_from_files
from crucible.application.admin.board import waiting_line
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import CICertification, PullRequest, PullRequestState
from crucible.domain.lifecycle import TaskState
from tests.unit.test_issue_360_ready_for_merge_correction import (
    NOW,
    OLD_HEAD,
    PR_ID,
    PR_NUMBER,
    TASK_ID,
)
from tests.unit.test_issue_411_merge_mechanics import (
    MERGED_HEAD,
    REPOSITORY,
    WORK_BRANCH,
    _task,
    _wakes,
    _world,
)

# ----- AC1: the merge-main script renumbers the branch's migration -------------


def _git(cwd: Path | str, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


SCRIPT_BRANCH = "crucible/task"
VERSIONS = "crucible/adapters/persistence/migrations/versions"


def _migration(revision: str, down: str, body: str) -> str:
    return (
        f'"""{revision}\n\nRevision ID: {revision}\nRevises: {down}\n"""\n\n'
        f'revision = "{revision}"\ndown_revision = "{down}"\n\n\n'
        f"def upgrade():\n    {body}\n"
    )


def _remote_migrations(tmp_path: Path) -> tuple[Path, str]:
    """An origin whose main has 0047_base and 0048_main; the work branch, cut before
    0048, adds its own 0047_branch on top of 0046_prev: two heads once merged."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    work = tmp_path / "seed"
    _git(tmp_path, "clone", "--quiet", str(origin), str(work))
    versions = work / VERSIONS
    versions.mkdir(parents=True)

    (versions / "_0047_base.py").write_text(
        _migration("0047_base", "0046_prev", "op.create_table('users')")
    )
    _git(work, "add", ".")
    _git(work, "commit", "--quiet", "-m", "base")
    _git(work, "push", "--quiet", "origin", "HEAD:refs/heads/main")

    _git(work, "checkout", "--quiet", "-b", SCRIPT_BRANCH)
    (versions / "_0047_branch.py").write_text(
        _migration("0047_branch", "0046_prev", "op.create_table('branch_table')")
    )
    _git(work, "add", ".")
    _git(work, "commit", "--quiet", "-m", "branch")
    _git(work, "push", "--quiet", "origin", f"HEAD:refs/heads/{SCRIPT_BRANCH}")
    tip = _git(work, "rev-parse", "HEAD")

    _git(work, "checkout", "--quiet", "main")
    (versions / "_0048_main.py").write_text(
        _migration("0048_main", "0047_base", "op.add_column('users', sa.Column('email'))")
    )
    _git(work, "add", ".")
    _git(work, "commit", "--quiet", "-m", "main moved")
    _git(work, "push", "--quiet", "origin", "HEAD:refs/heads/main")
    return origin, tip


def _run_merge_main(tmp_path: Path, origin: Path, tip: str) -> tuple[Any, dict[str, str]]:
    root = tmp_path / "worker"
    root.mkdir()
    script = scripts.merge_main_script(
        clone_url=str(origin),
        work_branch=SCRIPT_BRANCH,
        base_ref="main",
        expected_head=tip,
        author_name="Crucible",
        author_email="crucible@example.com",
        token_dir=str(root),
        out_dir=str(root / "out"),
        work_root=str(root),
    )
    (root / "token").write_text("a-test-token")
    done = subprocess.run(
        ["sh", "-c", script],
        input=b"a-test-token",
        env={"PATH": "/usr/bin:/bin"},
        cwd=root,
        capture_output=True,
        check=False,
    )
    files = {
        path.name: path.read_text(encoding="utf-8")
        for path in (root / "out").iterdir()
        if path.is_file()
    }
    return merge_outcome_from_files(files, done.returncode), files


def _heads(versions: Path) -> set[str]:
    revisions: dict[str, str] = {}
    for path in versions.glob("_*.py"):
        text = path.read_text()
        revision = text.split('revision = "')[1].split('"')[0]
        down = text.split('down_revision = "')[1].split('"')[0]
        revisions[revision] = down
    return set(revisions) - set(revisions.values())


def test_ac1_migration_renumbered_past_main_highest(tmp_path: Path) -> None:
    origin, tip = _remote_migrations(tmp_path)
    outcome, files = _run_merge_main(tmp_path, origin, tip)

    assert outcome.merged is True, files.get("error.txt")
    assert outcome.exit_code == 0
    assert outcome.head_sha != tip
    schema = json.loads(files["schema.json"])
    assert schema["tables"] == ["branch_table"]

    verify = tmp_path / "verify"
    _git(tmp_path, "clone", "--quiet", str(origin), str(verify))
    _git(verify, "checkout", "--quiet", SCRIPT_BRANCH)
    versions = verify / VERSIONS
    assert not (versions / "_0047_branch.py").exists()
    content = (versions / "_0049_branch.py").read_text()
    assert 'revision = "0049_branch"' in content
    assert 'down_revision = "0048_main"' in content
    assert "Revision ID: 0049_branch" in content
    assert "Revises: 0048_main" in content
    # Main's own migrations were not touched, and the graph has one head.
    assert (versions / "_0047_base.py").read_text() == _migration(
        "0047_base", "0046_prev", "op.create_table('users')"
    )
    assert _heads(versions) == {"0049_branch"}
    # Crucible's own commit, above the merge of main.
    subjects = _git(verify, "log", "--format=%s", "-3").splitlines()
    assert subjects[0] == "Crucible: renumber migrations to follow main"
    assert subjects[1].startswith("Merge origin/main into")


# ----- AC2, AC3: the delivery coordinator ---------------------------------------

OTHER_NUMBER = 100


def _ready_world(tmp_path: Path, *, tables: list[str]) -> tuple[Any, ...]:
    """The #411 world with a clean, certified pull request whose migrations touch
    `tables`, as the publisher recorded them (hades #447)."""
    store, clock, supervisor, client, publisher = _world(tmp_path)
    client.pull().mergeable_state, client.pull().mergeable = "clean", True
    client.fake.set_check(REPOSITORY, OLD_HEAD, name="test")
    store.ci_certifications.put(  # type: ignore[attr-defined]
        CICertification(
            id="01CERT4470000000000000001",
            pull_request_id=PR_ID,
            task_id=TASK_ID,
            head_sha=OLD_HEAD,
            state="green",
            required_checks=["test"],
            check_runs=[{"status": "completed", "conclusion": "success", "source": "check_run"}],
            failure={},
            detail="",
            evaluated_at=NOW,
        )
    )
    pull_request = store.pull_requests.get(PR_ID)
    assert pull_request is not None
    pull_request.mergeable_state = "clean"
    pull_request.schema_tables = tables
    pull_request.schema_columns = ["note"]
    pull_request.schema_models = ["crucible/adapters/persistence/models.py"]
    return store, clock, supervisor, client, publisher


def _other_pull_request(store: Any, *, tables: list[str]) -> PullRequest:
    """An older open pull request of another task in the same repository."""
    ours = store.pull_requests.get(PR_ID)
    other = PullRequest(
        id="01PULL447OTHER00000000001",
        task_id="01TASK447OTHER00000000001",
        repository_id=ours.repository_id,
        number=OTHER_NUMBER,
        url=f"{ours.url.rsplit('/', 1)[0]}/{OTHER_NUMBER}",
        base_ref="main",
        work_branch="crucible/EX-0100",
        state=PullRequestState.OPEN,
        head_sha="b" * 40,
        opened_at=NOW - timedelta(hours=1),
        schema_tables=tables,
        schema_columns=[],
        schema_models=["crucible/adapters/persistence/models.py"],
    )
    store.pull_requests.add(other)
    return other


def test_ac2_two_prs_same_table_second_held_then_merge_main(tmp_path: Path) -> None:
    store, _clock, supervisor, client, publisher = _ready_world(tmp_path, tables=["users"])
    other = _other_pull_request(store, tables=["users", "orgs"])

    asyncio.run(supervisor.delivery.observe())

    # Held: nothing merged, no merge-main, the task still waits in ready_for_merge.
    assert client.merged_numbers == []
    assert publisher.merges == []
    assert _task(store).state is TaskState.READY_FOR_MERGE
    held = _wakes(store, WakeReason.SCHEMA_OVERLAP)
    assert len(held) == 1
    summary = held[0].payload["summary"]
    assert f"pull request #{PR_NUMBER} waits for #{OTHER_NUMBER}" in summary
    assert "users" in summary
    # The Board shows the wake's summary as what the task waits on, naming the other.
    assert f"#{OTHER_NUMBER}" in waiting_line(held[0], TaskState.READY_FOR_MERGE)

    # Another tick of the same hold is quiet: one wake per pair.
    asyncio.run(supervisor.delivery.observe())
    assert len(_wakes(store, WakeReason.SCHEMA_OVERLAP)) == 1

    # The first merges: the held one gets merge-main (merge, renumber, push), and its
    # new head goes to CI certification before the squash merge.
    other.state = PullRequestState.MERGED
    asyncio.run(supervisor.delivery.observe())

    assert [(m.work_branch, m.base_ref, m.expected_head) for m in publisher.merges] == [
        (WORK_BRANCH, "main", OLD_HEAD)
    ]
    assert client.merged_numbers == []
    assert _task(store).state is TaskState.AWAITING_CI_CERTIFICATION
    ours = store.pull_requests.get(PR_ID)
    assert ours is not None
    assert ours.head_sha == MERGED_HEAD
    assert _task(store).head_sha == MERGED_HEAD


def test_ac3_two_prs_different_tables_merge_normally(tmp_path: Path) -> None:
    store, _clock, supervisor, client, publisher = _ready_world(tmp_path, tables=["users"])
    _other_pull_request(store, tables=["orgs"])

    asyncio.run(supervisor.delivery.observe())

    # Not held: no schema_overlap wake; the migration-adding branch got merge-main.
    assert _wakes(store, WakeReason.SCHEMA_OVERLAP) == []
    assert len(publisher.merges) == 1
    assert _task(store).state is TaskState.AWAITING_CI_CERTIFICATION

    # Its new head certified, the next tick merges it as today: the branch already
    # carries main, so the fake's merge-main leaves the head where it is.
    client.fake.set_check(REPOSITORY, MERGED_HEAD, name="test")
    store.ci_certifications.put(
        CICertification(
            id="01CERT4470000000000000002",
            pull_request_id=PR_ID,
            task_id=TASK_ID,
            head_sha=MERGED_HEAD,
            state="green",
            required_checks=["test"],
            check_runs=[{"status": "completed", "conclusion": "success", "source": "check_run"}],
            failure={},
            detail="",
            evaluated_at=NOW,
        )
    )
    _task(store).state = TaskState.READY_FOR_MERGE
    client.pull().mergeable_state, client.pull().mergeable = "clean", True

    asyncio.run(supervisor.delivery.observe())

    assert client.merged_numbers == [PR_NUMBER]
    assert _task(store).state is TaskState.MERGED


def test_ac3_a_pull_request_without_migrations_merges_without_merge_main(
    tmp_path: Path,
) -> None:
    store, _clock, supervisor, client, publisher = _ready_world(tmp_path, tables=[])
    ours = store.pull_requests.get(PR_ID)
    assert ours is not None
    ours.schema_columns = []
    _other_pull_request(store, tables=["users"])

    asyncio.run(supervisor.delivery.observe())

    assert publisher.merges == []
    assert client.merged_numbers == [PR_NUMBER]
    assert _task(store).state is TaskState.MERGED
