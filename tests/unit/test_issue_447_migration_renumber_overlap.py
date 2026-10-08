import contextlib
import subprocess
from pathlib import Path
from typing import Any

from crucible.adapters.execution import scripts
from crucible.adapters.execution.publisher import merge_outcome_from_files
from crucible.application.delivery_tick import DeliveryCoordinator, MergePlan
from crucible.domain.entities import PullRequest
from crucible.domain.lifecycle import TaskState
from tests.fixtures import FakeClock
from tests.unit.test_issue_411_merge_mechanics import _store


def _git(cwd: Path | str, *args: str, env: dict[str, str] | None = None) -> str:
    if env is None:
        env = {}
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        print("GIT ERROR:", e.stderr)
        raise


WORK_BRANCH = "crucible/task"


def _remote_migrations(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    work = tmp_path / "seed"
    _git(tmp_path, "clone", "--quiet", str(origin), str(work))

    mig_dir = work / "crucible/adapters/persistence/migrations/versions"
    mig_dir.mkdir(parents=True)

    base_mig = mig_dir / "_0047_base.py"
    base_mig.write_text("""
revision = "0047_base"
down_revision = "0046_prev"
# Revises: 0046_prev
# Revision ID: 0047_base
def upgrade():
    op.create_table('users')
""")
    _git(work, "add", ".")
    _git(work, "commit", "--quiet", "-m", "base")
    _git(work, "push", "--quiet", "origin", "HEAD:refs/heads/main")

    _git(work, "checkout", "--quiet", "-b", WORK_BRANCH)
    branch_mig = mig_dir / "_0047_branch.py"
    branch_mig.write_text("""
revision = "0047_branch"
down_revision = "0046_prev"
# Revises: 0046_prev
# Revision ID: 0047_branch
def upgrade():
    op.create_table('branch_table')
""")
    _git(work, "add", ".")
    _git(work, "commit", "--quiet", "-m", "branch")
    _git(work, "push", "--quiet", "origin", f"HEAD:refs/heads/{WORK_BRANCH}")
    tip = _git(work, "rev-parse", "HEAD")

    _git(work, "checkout", "--quiet", "main")
    main_mig = mig_dir / "_0048_main.py"
    main_mig.write_text("""
revision = "0048_main"
down_revision = "0047_base"
# Revises: 0047_base
# Revision ID: 0048_main
""")
    _git(work, "add", ".")
    _git(work, "commit", "--quiet", "-m", "main moved")
    _git(work, "push", "--quiet", "origin", "HEAD:refs/heads/main")

    return origin, tip


def _run_merge_main(tmp_path: Path, origin: Path, tip: str) -> tuple[Any, dict]:
    root = tmp_path / "worker"
    root.mkdir()
    script = scripts.merge_main_script(
        clone_url=str(origin),
        work_branch=WORK_BRANCH,
        base_ref="main",
        expected_head=tip,
        author_name="A",
        author_email="a@b",
        token_dir=str(root),
        out_dir=str(root / "out"),
        work_root=str(root),
    )
    env = {"PATH": "/usr/bin:/bin"}
    (root / "token").write_text("a-test-token")
    done = subprocess.run(
        ["sh", "-c", script],
        input=b"a-test-token",
        env=env,
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


def test_ac1_migration_renumbered_past_main_highest(tmp_path: Path) -> None:
    origin, tip = _remote_migrations(tmp_path)
    outcome, files = _run_merge_main(tmp_path, origin, tip)

    assert outcome.merged is True
    assert outcome.exit_code == 0
    assert "schema.json" in files

    import json

    schema = json.loads(files["schema.json"])
    assert "branch_table" in schema["tables"]

    # Verify the branch now has 0049_branch.py
    work = tmp_path / "verify"
    _git(tmp_path, "clone", "--quiet", str(origin), str(work))
    _git(work, "checkout", "--quiet", WORK_BRANCH)

    mig_dir = work / "crucible/adapters/persistence/migrations/versions"
    assert not (mig_dir / "_0047_branch.py").exists()
    assert (mig_dir / "_0049_branch.py").exists()
    content = (mig_dir / "_0049_branch.py").read_text()
    assert 'revision = "0049_branch"' in content
    assert 'down_revision = "0048_main"' in content


def test_ac2_two_prs_same_table_second_held():

    store = _store(TaskState.READY_FOR_MERGE)
    clock = FakeClock()

    # We create two PRs
    pr1 = PullRequest(
        id="pr-1",
        url="http://github/pr/1",
        repository_id="repo-id",
        task_id="t-1",
        number=1,
        base_ref="main",
        work_branch="branch-1",
        state="open",
        head_sha="111",
        schema_tables=["users", "orgs"],
        schema_columns=[],
        schema_models=[],
        opened_at=clock.now(),
    )
    pr2 = PullRequest(
        id="pr-2",
        url="http://github/pr/2",
        repository_id="repo-id",
        task_id="t-1",
        number=2,
        base_ref="main",
        work_branch="branch-2",
        state="open",
        head_sha="222",
        schema_tables=["orgs"],  # OVERLAP
        schema_columns=[],
        schema_models=[],
        opened_at=clock.now(),
    )
    store.pull_requests.add(pr1)
    store.pull_requests.add(pr2)
    store.pull_requests.list_open_for_repository = lambda repo: [pr1, pr2]

    class FakeHost:
        def _fenced(self):

            @contextlib.contextmanager
            def cm():
                yield store

            return cm()

        async def _db(self, fn):
            return fn()

    coordinator = DeliveryCoordinator(FakeHost(), clock)

    plan1 = MergePlan(
        task_id="t-1",
        pull_request_id="pr-1",
        number=1,
        repository_name="repo",
        installation_id=1,
        certified_head_sha="111",
        base_ref="main",
    )
    plan2 = MergePlan(
        task_id="t-2",
        pull_request_id="pr-2",
        number=2,
        repository_name="repo",
        installation_id=1,
        certified_head_sha="222",
        base_ref="main",
    )

    # PR 1 should NOT be held (it is older, number 1)
    assert coordinator._check_schema_overlap(plan1) is None

    # PR 2 SHOULD be held (it is newer, number 2)
    overlap = coordinator._check_schema_overlap(plan2)
    assert overlap is not None
    other_number, should_hold = overlap
    assert other_number == 1
    assert should_hold is True


def test_ac3_two_prs_different_tables_merge_normally():

    store = _store(TaskState.READY_FOR_MERGE)
    clock = FakeClock()

    # We create two PRs
    pr1 = PullRequest(
        id="pr-1",
        url="http://github/pr/1",
        repository_id="repo-id",
        task_id="t-1",
        number=1,
        base_ref="main",
        work_branch="branch-1",
        state="open",
        head_sha="111",
        schema_tables=["users"],
        schema_columns=[],
        schema_models=[],
        opened_at=clock.now(),
    )
    pr2 = PullRequest(
        id="pr-2",
        url="http://github/pr/2",
        repository_id="repo-id",
        task_id="t-1",
        number=2,
        base_ref="main",
        work_branch="branch-2",
        state="open",
        head_sha="222",
        schema_tables=["orgs"],  # NO OVERLAP
        schema_columns=[],
        schema_models=[],
        opened_at=clock.now(),
    )
    store.pull_requests.add(pr1)
    store.pull_requests.add(pr2)
    store.pull_requests.list_open_for_repository = lambda repo: [pr1, pr2]

    class FakeHost:
        def _fenced(self):

            @contextlib.contextmanager
            def cm():
                yield store

            return cm()

        async def _db(self, fn):
            return fn()

    coordinator = DeliveryCoordinator(FakeHost(), clock)

    plan1 = MergePlan(
        task_id="t-1",
        pull_request_id="pr-1",
        number=1,
        repository_name="repo",
        installation_id=1,
        certified_head_sha="111",
        base_ref="main",
    )
    plan2 = MergePlan(
        task_id="t-2",
        pull_request_id="pr-2",
        number=2,
        repository_name="repo",
        installation_id=1,
        certified_head_sha="222",
        base_ref="main",
    )

    # Neither should be held
    assert coordinator._check_schema_overlap(plan1) is None
    assert coordinator._check_schema_overlap(plan2) is None
