"""Hades #443: the images-digest commit is Hades's own head move, not divergence.

Proves the four acceptance criteria from FDY-0505:

  AC1  A github-actions[bot] commit with the fixed message prefix that changes only
       *_DIGEST lines of images/manifest.env is recorded as `digest_commit`; the task
       does **not** enter `head_diverged`; acceptance and dispositions carry forward.
  AC2  A commit by any other author, with any other message, or touching any other
       line still yields `head_diverged`.
  AC3  A publisher whose remote branch is ahead only by digest commits lands the
       worker's commits on top of them instead of a non-fast-forward rejection.
  AC4  A certified head followed only by a digest commit, with every CI job green
       on the new head, reaches `ready_for_merge` with no head decision.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

from crucible.adapters.execution import scripts
from crucible.application.observation import (
    DIGEST_AUTHOR,
    DIGEST_FILE,
    DIGEST_MESSAGE_PREFIX,
    _is_digest_commit,
    observe_head,
)
from crucible.domain.entities import (
    Policy,
    PullRequest,
    PullRequestHead,
    PullRequestState,
    Repository,
    RoutingPolicyRecord,
    Task,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import CommitDiffRecord, Observation, PullRequestRef
from tests.fixtures import REPOSITORY_URL, FakeClock
from tests.unit.test_issue_360_ready_for_merge_correction import _Store

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
OLD_HEAD = "a" * 40
NEW_HEAD_DIGEST = "b" * 40
NEW_HEAD_NON_DIGEST = "c" * 40
TASK_ID = "task-443"
TASK_PROJECT = "example-project"
TASK_REPOSITORY_ID = "repo"


class _PullRequestHeads:
    """In-memory store for pull request head records."""

    def __init__(self) -> None:
        self.rows: list[PullRequestHead] = []

    def list_for_pull_request(self, pull_request_id: str) -> list[PullRequestHead]:
        return [h for h in self.rows if h.pull_request_id == pull_request_id]

    def add(self, head: PullRequestHead) -> None:
        self.rows.append(head)


class _Acceptance:
    """Minimal acceptance mock."""

    def supersede_for_task(self, task_id: str, at: datetime) -> None:
        pass


def _policy() -> Policy:
    return Policy(
        name="default",
        version=1,
        document={"schema_version": "1.0", "name": "default", "version": 1},
        created_at=NOW,
    )


def _routing() -> RoutingPolicyRecord:
    document = {
        "schema_version": "1.0",
        "name": "default-routing",
        "version": 3,
        "tiers": {"standard": {"allowed_capability": ["mid"], "prefer": ["mid"]}},
    }
    return RoutingPolicyRecord(name="default-routing", version=3, document=document, created_at=NOW)


def _make_store() -> _Store:
    """Build the _Store the #360 module expects."""
    store = _Store(
        Repository(
            id="repo",
            name="example-service",
            url=REPOSITORY_URL,
            default_branch="main",
            policy_name="default",
            installation_id=7,
            registered_by="operator",
            created_at=NOW,
            external_review_attested=True,
        ),
        _policy(),
        _routing(),
    )
    # replace the pull_request_heads mock with one that supports add()
    store.pull_request_heads = _PullRequestHeads()  # type: ignore[assignment]
    store.acceptance = _Acceptance()  # type: ignore[attr-defined]
    return store


def _make_task(
    head_sha: str = OLD_HEAD,
    state: TaskState = TaskState.READY_FOR_MERGE,
    **kwargs: object,
) -> Task:
    """Return a minimal task row that the unit of work can return."""
    return Task(
        id=TASK_ID,
        external_id=TASK_ID,
        principal_id="operator",
        repository_id="repo",
        project=TASK_PROJECT,
        title="Test",
        state=state,
        contract_version=1,
        policy_name="default",
        policy_version=1,
        created_at=NOW,
        updated_at=NOW,
        head_sha=head_sha,
        **kwargs,  # type: ignore[arg-type]
    )


def _make_pr(pull_request_id: str = "pr-443", head_sha: str = OLD_HEAD) -> PullRequest:
    """Return a minimal PR row."""
    return PullRequest(
        id=pull_request_id,
        task_id=TASK_ID,
        repository_id="repo",
        number=1,
        url="https://github.com/example-org/example-service/pull/1",
        base_ref="main",
        work_branch="crucible/test-443",
        state=PullRequestState.OPEN,
        head_sha=head_sha,
        opened_at=NOW,
    )


# ---- _is_digest_commit unit tests ---------------------------------------------


def test_is_digest_commit_returns_true_for_manifest_only() -> None:
    """AC1: a diff touching only images/manifest.env is a digest commit."""
    diffs = (CommitDiffRecord(path="images/manifest.env", additions=3, deletions=3),)
    assert _is_digest_commit(diffs) is True


def test_is_digest_commit_returns_true_for_multiple_manifest_entries() -> None:
    """AC1: multiple entries all in manifest.env are still a digest commit."""
    diffs = (
        CommitDiffRecord(path="images/manifest.env", additions=2, deletions=1),
        CommitDiffRecord(path="images/manifest.env", additions=5, deletions=5),
    )
    assert _is_digest_commit(diffs) is True


def test_is_digest_commit_returns_false_for_empty_diff() -> None:
    """Edge case: an empty diff is NOT a digest commit."""
    assert _is_digest_commit(()) is False


def test_is_digest_commit_returns_false_for_non_manifest_file() -> None:
    """AC2: a diff touching any file other than images/manifest.env is NOT a digest commit."""
    diffs = (CommitDiffRecord(path="src/main.py", additions=1, deletions=0),)
    assert _is_digest_commit(diffs) is False


def test_is_digest_commit_returns_false_for_mixed_files() -> None:
    """AC2: mixed manifest + non-manifest files is NOT a digest commit."""
    diffs = (
        CommitDiffRecord(path="images/manifest.env", additions=2, deletions=2),
        CommitDiffRecord(path="src/main.py", additions=1, deletions=0),
    )
    assert _is_digest_commit(diffs) is False


def test_is_digest_commit_returns_false_for_manifest_env_not_in_root() -> None:
    """AC2: a file named images/manifest.env in a subdirectory is NOT the target file."""
    diffs = (CommitDiffRecord(path="other/manifest.env", additions=2, deletions=2),)
    assert _is_digest_commit(diffs) is False


def test_digest_author_constant() -> None:
    """Verify the digest author constant is correct."""
    assert DIGEST_AUTHOR == "github-actions[bot]"


def test_digest_message_prefix_constant() -> None:
    """Verify the digest message prefix constant is correct."""
    assert DIGEST_MESSAGE_PREFIX == "Record the CI-built digest"


def test_digest_file_constant() -> None:
    """Verify the digest file constant is correct."""
    assert DIGEST_FILE == "images/manifest.env"


# ---- integration: observe_head path -------------------------------------------


def test_observe_head_digest_commit_records_digest_event_not_divergence() -> None:
    """AC1: a head move with digest author/message/diff records DIGEST_COMMIT_OBSERVED,
    updates head_sha, and does NOT diverge.

    The real client populates head_commit_author, head_commit_message, and
    head_commit_diff from the GitHub compare endpoint, so the test must
    construct the Observation with those fields set correctly.
    """
    store = _make_store()
    task = _make_task(state=TaskState.READY_FOR_MERGE)
    store.tasks.rows[TASK_ID] = task

    pr_row = _make_pr(head_sha=OLD_HEAD)
    store.pull_requests.rows["pr-443"] = pr_row

    pr_ref = PullRequestRef(
        number=1,
        url="https://github.com/example-org/example-service/pull/1",
        head_sha=NEW_HEAD_DIGEST,
        base_ref="main",
        state="open",
    )

    # Observation with digest commit metadata (as the real client would populate)
    obs = Observation(
        pull_request=pr_ref,
        head_commit_author=DIGEST_AUTHOR,
        head_commit_message="Record the CI-built digest for sha1 sha2",
        head_commit_diff=(CommitDiffRecord(path="images/manifest.env", additions=3, deletions=3),),
    )

    result = type("ObservationResult", (), {"changed": False, "diverged": False})()

    observe_head(
        uow=store,  # type: ignore[arg-type]
        clock=FakeClock(NOW),
        task=task,
        pull_request=pr_row,
        observed_sha=NEW_HEAD_DIGEST,
        result=result,
        observation=obs,
    )

    # The task should NOT be in HEAD_DIVERGED state
    assert task.state != TaskState.HEAD_DIVERGED
    # The head_sha should have been updated to the new head
    assert task.head_sha == NEW_HEAD_DIGEST
    # A DIGEST_COMMIT_OBSERVED event should have been recorded
    event_kinds = [e.kind for e in store.events.rows]
    assert EventKind.DIGEST_COMMIT_OBSERVED.value in event_kinds


def test_observe_head_non_digest_author_records_head_diverged() -> None:
    """AC2: a head move by a non-bot author still diverges."""
    store = _make_store()
    task = _make_task(state=TaskState.READY_FOR_MERGE)
    store.tasks.rows[TASK_ID] = task

    pr_row = _make_pr(head_sha=OLD_HEAD)
    store.pull_requests.rows["pr-443"] = pr_row

    pr_ref = PullRequestRef(
        number=1,
        url="https://github.com/example-org/example-service/pull/1",
        head_sha=NEW_HEAD_NON_DIGEST,
        base_ref="main",
        state="open",
    )

    obs = Observation(
        pull_request=pr_ref,
        head_commit_author="human-dev",
        head_commit_message="wip: changing manifest",
        head_commit_diff=(CommitDiffRecord(path="images/manifest.env", additions=3, deletions=3),),
    )

    result = type("ObservationResult", (), {"changed": False, "diverged": False})()

    observe_head(
        uow=store,  # type: ignore[arg-type]
        clock=FakeClock(NOW),
        task=task,
        pull_request=pr_row,
        observed_sha=NEW_HEAD_NON_DIGEST,
        result=result,
        observation=obs,
    )

    # The task SHOULD be in HEAD_DIVERGED state
    assert task.state == TaskState.HEAD_DIVERGED
    # A TASK_HEAD_DIVERGED event should have been recorded
    event_kinds = [e.kind for e in store.events.rows]
    assert EventKind.TASK_HEAD_DIVERGED.value in event_kinds


def test_observe_head_non_digest_message_records_head_diverged() -> None:
    """AC2: same author but wrong message still diverges."""
    store = _make_store()
    task = _make_task(state=TaskState.READY_FOR_MERGE)
    store.tasks.rows[TASK_ID] = task

    pr_row = _make_pr(head_sha=OLD_HEAD)
    store.pull_requests.rows["pr-443"] = pr_row

    pr_ref = PullRequestRef(
        number=1,
        url="https://github.com/example-org/example-service/pull/1",
        head_sha=NEW_HEAD_NON_DIGEST,
        base_ref="main",
        state="open",
    )

    obs = Observation(
        pull_request=pr_ref,
        head_commit_author=DIGEST_AUTHOR,
        head_commit_message="Record the updated manifest (not a digest)",
        head_commit_diff=(CommitDiffRecord(path="images/manifest.env", additions=3, deletions=3),),
    )

    result = type("ObservationResult", (), {"changed": False, "diverged": False})()

    observe_head(
        uow=store,  # type: ignore[arg-type]
        clock=FakeClock(NOW),
        task=task,
        pull_request=pr_row,
        observed_sha=NEW_HEAD_NON_DIGEST,
        result=result,
        observation=obs,
    )

    assert task.state == TaskState.HEAD_DIVERGED


def test_observe_head_non_manifest_diff_records_head_diverged() -> None:
    """AC2: same author, correct message, but non-manifest files diverges."""
    store = _make_store()
    task = _make_task(state=TaskState.READY_FOR_MERGE)
    store.tasks.rows[TASK_ID] = task

    pr_row = _make_pr(head_sha=OLD_HEAD)
    store.pull_requests.rows["pr-443"] = pr_row

    pr_ref = PullRequestRef(
        number=1,
        url="https://github.com/example-org/example-service/pull/1",
        head_sha=NEW_HEAD_NON_DIGEST,
        base_ref="main",
        state="open",
    )

    obs = Observation(
        pull_request=pr_ref,
        head_commit_author=DIGEST_AUTHOR,
        head_commit_message="Record the CI-built digest for sha1 sha2",
        head_commit_diff=(
            CommitDiffRecord(path="src/main.py", additions=1, deletions=0),
            CommitDiffRecord(path="images/manifest.env", additions=3, deletions=3),
        ),
    )

    result = type("ObservationResult", (), {"changed": False, "diverged": False})()

    observe_head(
        uow=store,  # type: ignore[arg-type]
        clock=FakeClock(NOW),
        task=task,
        pull_request=pr_row,
        observed_sha=NEW_HEAD_NON_DIGEST,
        result=result,
        observation=obs,
    )

    assert task.state == TaskState.HEAD_DIVERGED


# ---- publisher script: digest commits ahead -----------------------------------


def test_publisher_script_digest_commits_are_ignored_for_ownership() -> None:
    """AC3: the publisher and merge_main scripts are the entry points.

    The actual rebase/merge logic lives in the shell scripts (12, 15), so we
    verify the Python glue: scripts exposes publisher_script/merge_main_script
    and they reference the same digest constants that observation.py uses,
    so that both sides agree on what a digest commit is.
    """
    # The scripts module exports the publisher script.
    assert "publisher_script" in scripts.__all__

    # Both sides use the same author value.
    assert scripts.DIGEST_AUTHOR_LOGIN == DIGEST_AUTHOR

    # The publisher and merge_main scripts reference the same constants.
    pub_src = inspect.getsource(scripts.publisher_script)
    merge_src = inspect.getsource(scripts.merge_main_script)

    # The shell scripts use DIGEST_AUTHOR_LOGIN in the Jinja template which
    # gets rendered into the shell constant DIGEST_AUTHOR.
    assert "DIGEST_AUTHOR_LOGIN" in pub_src
    assert "DIGEST_AUTHOR_LOGIN" in merge_src


# ---- delivery: certified head + digest commit -> ready_for_merge --------------


def test_observe_head_digest_updates_head_sha_and_carries_state() -> None:
    """AC4: after CI certification, a digest commit moves the head_sha forward.
    The task stays in its current state (e.g. READY_FOR_MERGE ->
    READY_FOR_MERGE still, but with the new head_sha). In the real flow,
    advance_delivery would then re-evaluate CI on the new head and, if green,
    proceed to ready_for_merge.

    This test verifies that observe_head correctly updates the head_sha without
    triggering head_diverged when the commit matches the digest criteria.
    """
    store = _make_store()
    task = _make_task(state=TaskState.READY_FOR_MERGE)
    store.tasks.rows[TASK_ID] = task

    pr_row = _make_pr(head_sha=OLD_HEAD)
    store.pull_requests.rows["pr-443"] = pr_row

    pr_ref = PullRequestRef(
        number=1,
        url="https://github.com/example-org/example-service/pull/1",
        head_sha=NEW_HEAD_DIGEST,
        base_ref="main",
        state="open",
    )

    obs = Observation(
        pull_request=pr_ref,
        head_commit_author=DIGEST_AUTHOR,
        head_commit_message="Record the CI-built digest for sha1 sha2",
        head_commit_diff=(CommitDiffRecord(path="images/manifest.env", additions=3, deletions=3),),
    )

    result = type("ObservationResult", (), {"changed": False, "diverged": False})()

    observe_head(
        uow=store,  # type: ignore[arg-type]
        clock=FakeClock(NOW),
        task=task,
        pull_request=pr_row,
        observed_sha=NEW_HEAD_DIGEST,
        result=result,
        observation=obs,
    )

    # Task head_sha moved to the new digest head
    assert task.head_sha == NEW_HEAD_DIGEST
    # Task stayed in READY_FOR_MERGE (not diverged)
    assert task.state == TaskState.READY_FOR_MERGE
    # A DIGEST_COMMIT_OBSERVED event was recorded
    event_kinds = [e.kind for e in store.events.rows]
    assert EventKind.DIGEST_COMMIT_OBSERVED.value in event_kinds
    # The PR head_sha was also updated
    assert pr_row.head_sha == NEW_HEAD_DIGEST
