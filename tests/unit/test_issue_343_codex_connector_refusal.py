"""Hades #343: every Codex connector refusal is a failed round, not a comment.

observation.py's old CODEX_ACCOUNT_REFUSAL constant matched only "to use codex here,
create a codex account", so the connector's other wording ("...create an environment
for this repo", posted on this very issue on 2026-10-03 and 2026-10-06) was recorded as
an ordinary issue comment and the task sat in `awaiting_external_review` for hours with
no wake. These tests prove: both wordings end the round as refused and wake the
orchestrator at once on the publication path (AC1); the refusal is never counted as a
comment needing a disposition (AC2); and a repository whose policy marks the review as
not automatic never gets the App's trigger comment, a wake stands in for it (AC3).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from crucible.application.observation import ObservationResult, record_comments
from crucible.application.publish import external_review_requires_person
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import PullRequest, PullRequestState, Repository, Task
from crucible.domain.external_review import is_codex_refusal
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import CommentRecord, Observation, PullRequestRef
from tests.unit.test_issue_360_ready_for_merge_correction import NOW, OLD_HEAD, TASK_ID
from tests.unit.test_issue_379_merge_during_publish import _correcting, _publish, _wakes
from tests.unit.test_issue_401_codex_findings_correction import _feedback_world
from tests.unit.test_issue_411_merge_mechanics import _poll

ACCOUNT_VARIANT = "To use Codex here, create a Codex account and connect to github."
ENVIRONMENT_VARIANT = (
    "To use Codex here, create an environment for this repo "
    "(https://chatgpt.com/codex/cloud/settings/environments)."
)

REVIEWER = "chatgpt-codex-connector[bot]"


# ----- AC1: both wordings end the round as refused and wake at once, on the publication
# path (the task sits in awaiting_external_review, exactly as publish.py leaves it after
# posting the trigger). ------------------------------------------------------------------


@pytest.mark.parametrize("body", [ACCOUNT_VARIANT, ENVIRONMENT_VARIANT])
def test_either_refusal_wording_ends_the_round_and_wakes_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    store, clock, supervisor, client, _ = _feedback_world(tmp_path, monkeypatch, "codex")
    observe = client.observe
    refusal = CommentRecord(
        github_id="refusal",
        login=REVIEWER,
        body=body,
        created_at=NOW,
        updated_at=NOW,
        kind="issue_comment",
    )
    monkeypatch.setattr(
        client,
        "observe",
        lambda *a, **kw: replace(observe(*a, **kw), review_comments=(), issue_comments=(refusal,)),
    )

    # One observation pass: detection and the wake happen together.
    assert _poll(store, clock, supervisor) == 1

    task = store.tasks.get(TASK_ID)
    assert task is not None
    assert task.state is TaskState.AWAITING_EXTERNAL_REVIEW
    assert task.contract_version == 1  # no automatic correction was scheduled

    wakes = _wakes(store, WakeReason.EXTERNAL_REVIEW_TRIGGER_NEEDED.value)
    assert len(wakes) == 1
    assert "requested by a person" in wakes[0]


def test_is_codex_refusal_matches_both_wordings_and_nothing_else() -> None:
    assert is_codex_refusal(ACCOUNT_VARIANT)
    assert is_codex_refusal(ENVIRONMENT_VARIANT)
    assert is_codex_refusal("TO USE CODEX HERE, something new tomorrow")
    assert not is_codex_refusal("Codex Review Summary: in progress")
    assert not is_codex_refusal("Looks fine to me.")


# ----- AC2: the refusal is never counted as a comment needing a disposition. -----------


class _Comments:
    def __init__(self) -> None:
        self.rows: list[object] = []

    def get_by_github(self, *_args: object) -> None:
        return None

    def add(self, row: object) -> bool:
        self.rows.append(row)
        return True


class _Repositories:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def get(self, repository_id: str) -> Repository | None:
        return self.repository if repository_id == self.repository.id else None

    def upsert(self, repository: Repository) -> Repository:
        self.repository = repository
        return self.repository


def _minimal_uow(repository: Repository) -> SimpleNamespace:
    return SimpleNamespace(
        events=SimpleNamespace(latest_for_task_kind=lambda *_args: None, append=lambda e: e),
        review_comments=_Comments(),
        repositories=_Repositories(repository),
    )


def _repository() -> Repository:
    return Repository(
        id="repository",
        name="example/repo",
        url="https://github.com/example/repo",
        default_branch="main",
        policy_name="default-software",
        installation_id=1,
        registered_by="operator",
        created_at=NOW,
    )


def _task_and_pull() -> tuple[Task, PullRequest]:
    task = Task(
        id=TASK_ID,
        external_id="FDY-0343",
        principal_id="principal",
        project="hades",
        title="title",
        state=TaskState.AWAITING_EXTERNAL_REVIEW,
        contract_version=1,
        policy_name="default-software",
        policy_version=1,
        repository_id="repository",
        created_at=NOW,
        updated_at=NOW,
    )
    pull_request = PullRequest(
        id="pr",
        task_id=task.id,
        repository_id="repository",
        number=343,
        url="https://github.com/example/repo/pull/343",
        base_ref="main",
        work_branch="crucible/FDY-0343",
        state=PullRequestState.OPEN,
        head_sha=OLD_HEAD,
        opened_at=NOW,
    )
    return task, pull_request


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 10, 7, tzinfo=UTC)


@pytest.mark.parametrize("body", [ACCOUNT_VARIANT, ENVIRONMENT_VARIANT])
def test_the_refusal_is_recorded_but_never_a_comment_to_disposition(body: str) -> None:
    repository = _repository()
    uow = _minimal_uow(repository)
    task, pull_request = _task_and_pull()
    refusal = CommentRecord(
        github_id="refusal",
        login=REVIEWER,
        body=body,
        created_at=NOW,
        updated_at=NOW,
        kind="issue_comment",
    )
    observation = Observation(
        pull_request=PullRequestRef(
            number=pull_request.number,
            url=pull_request.url,
            head_sha=pull_request.head_sha,
            base_ref=pull_request.base_ref,
            state="open",
        ),
        issue_comments=(refusal,),
    )
    result = ObservationResult()

    signals = record_comments(
        uow,
        _Clock(),
        task=task,
        pull_request=pull_request,
        observation=observation,
        allowlist=frozenset({REVIEWER}),
        result=result,
        policy={
            "external_review": {
                "provider": "codex",
                "required_rounds": 1,
                "request_on_publish": True,
            }
        },
    )

    # Recorded (so an operator can read it), but never forwarded as a signal and never
    # counted toward the feedback that needs a disposition.
    assert [row.github_id for row in uow.review_comments.rows] == ["refusal"]
    assert signals == []
    assert result.new_comments == 0
    assert result.review_refusal is True

    # The repository remembers the refusal (hades #343: "or where a refusal was already
    # seen for that repository").
    assert uow.repositories.repository.codex_review_refused_at is not None


# ----- AC3: a repository whose policy says the review is not automatic never gets the
# App's trigger; a wake asks a person instead. -------------------------------------------


def test_policy_not_automatic_stops_the_apps_trigger_and_wakes_instead(tmp_path: Path) -> None:
    store, _clock, supervisor, github, _publisher = _correcting(tmp_path)
    store.policies.policy.document["external_review"]["automatic"] = False

    assert _publish(supervisor) == 1

    # github.posted only grows through post_issue_comment, which the fake raises on if
    # ever called (test_issue_411's _GitHub: "the corrected head must not ask for a new
    # external review"); reaching here at all proves it was never invoked.
    assert github.posted == []
    wakes = _wakes(store, WakeReason.EXTERNAL_REVIEW_TRIGGER_NEEDED.value)
    assert len(wakes) == 1
    assert "not automatic" in wakes[0] or "refusal was already seen" in wakes[0]


def test_a_prior_refusal_on_the_repository_also_stops_the_trigger(tmp_path: Path) -> None:
    store, _clock, supervisor, github, _publisher = _correcting(tmp_path)
    repository = store.repositories.repository
    repository.codex_review_refused_at = NOW

    assert _publish(supervisor) == 1

    assert github.posted == []
    wakes = _wakes(store, WakeReason.EXTERNAL_REVIEW_TRIGGER_NEEDED.value)
    assert len(wakes) == 1


def test_external_review_requires_person_pure_function() -> None:
    policy = {
        "external_review": {
            "provider": "codex",
            "required_rounds": 1,
            "request_on_publish": True,
        }
    }
    automatic_repo = _repository()
    assert external_review_requires_person(policy, automatic_repo) is False

    not_automatic = {**policy, "external_review": {**policy["external_review"], "automatic": False}}
    assert external_review_requires_person(not_automatic, automatic_repo) is True

    refused_repo = _repository()
    refused_repo.codex_review_refused_at = NOW
    assert external_review_requires_person(policy, refused_repo) is True

    # Nothing to request at all: no wake is owed either.
    no_rounds = {**policy, "external_review": {**policy["external_review"], "required_rounds": 0}}
    assert external_review_requires_person(no_rounds, refused_repo) is False
