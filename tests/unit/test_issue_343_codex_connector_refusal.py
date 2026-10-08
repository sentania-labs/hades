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

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from crucible.adapters.persistence.models import RepositoryRow
from crucible.adapters.persistence.unit_of_work import Repositories
from crucible.application.observation import ObservationResult, record_comments
from crucible.application.publish import external_review_requires_person
from crucible.application.review import latest_work_attempt
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import PullRequest, PullRequestState, Repository, Task
from crucible.domain.external_review import is_codex_refusal
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import CommentRecord, Observation, PullRequestRef
from tests.unit.test_issue_360_ready_for_merge_correction import NOW, OLD_HEAD, TASK_ID
from tests.unit.test_issue_379_merge_during_publish import _correcting, _publish, _task, _wakes
from tests.unit.test_issue_401_codex_findings_correction import _feedback_world
from tests.unit.test_issue_402_self_review_acceptance import _collected
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
#
# These use a first publication, not `_correcting` (a `ready_for_merge` task corrected):
# that fixture already carries one completed round out of a policy that requires one, so
# a round is never owed there regardless of the automatic flag (hades #343 finding
# 01M4CDWQN19WNJTM0BD2NZ7HXN, proven below) and it cannot show a wake actually being
# raised for one.


def _first_publish(tmp_path: Path) -> tuple[Any, Any, Any, Any, Any]:
    store, supervisor, github, publisher = _collected(tmp_path, correction=False)
    task = _task(store)
    work = latest_work_attempt(store.uow(), task)
    assert work is not None
    return store, supervisor, github, publisher, work[1]


def test_policy_not_automatic_stops_the_apps_trigger_and_wakes_instead(tmp_path: Path) -> None:
    store, supervisor, github, _publisher, execution = _first_publish(tmp_path)
    execution.policy_snapshot["external_review"]["automatic"] = False
    supervisor._evaluate_pending_gates()

    assert asyncio.run(supervisor.delivery.publish()) == 1

    # github.posted only grows through post_issue_comment; reaching here with it still
    # empty proves the App's trigger was never posted.
    assert github.posted == []
    assert _task(store).state is TaskState.AWAITING_EXTERNAL_REVIEW
    wakes = _wakes(store, WakeReason.EXTERNAL_REVIEW_TRIGGER_NEEDED.value)
    assert len(wakes) == 1
    assert "not automatic" in wakes[0] or "refusal was already seen" in wakes[0]


def test_a_prior_refusal_on_the_repository_also_stops_the_trigger(tmp_path: Path) -> None:
    store, supervisor, github, _publisher, _execution = _first_publish(tmp_path)
    store.repositories.repository.codex_review_refused_at = NOW
    supervisor._evaluate_pending_gates()

    assert asyncio.run(supervisor.delivery.publish()) == 1

    assert github.posted == []
    assert _task(store).state is TaskState.AWAITING_EXTERNAL_REVIEW
    wakes = _wakes(store, WakeReason.EXTERNAL_REVIEW_TRIGGER_NEEDED.value)
    assert len(wakes) == 1


# ----- hades #343 finding 01M4CDWQN19WNJTM0BD2NZ7HXN: the needs-person wake is owed only
# when a round is actually outstanding; a correction publish additionally defers to the
# correction retrigger policy, not only the repository-level automatic flag. -------------


def test_correction_with_rounds_already_complete_skips_the_wake(tmp_path: Path) -> None:
    """`_correcting` is a `ready_for_merge` task corrected: one round of the one the
    policy requires is already completed before the correction (round counting is per
    PR across heads, 23/05b), so `_finish` sends the corrected head straight to CI; the
    needs-person wake has no outstanding round left to ask a person for."""
    store, _clock, supervisor, github, _publisher = _correcting(tmp_path)
    store.policies.policy.document["external_review"]["automatic"] = False

    assert _publish(supervisor) == 1

    assert github.posted == []
    assert _wakes(store, WakeReason.EXTERNAL_REVIEW_TRIGGER_NEEDED.value) == []
    assert _task(store).state is TaskState.AWAITING_CI_CERTIFICATION


def test_correction_with_outstanding_round_and_no_retrigger_policy_skips_the_wake(
    tmp_path: Path,
) -> None:
    """A correction whose round was never completed, under the default policy
    (`retrigger_after_correction: false`): 23 says a correction never causes a second
    round under that policy, so the outstanding round is not this publish's to ask a
    person for either."""
    store, _clock, supervisor, github, _publisher = _correcting(tmp_path)
    store.review_cycles.rows.clear()
    store.policies.policy.document["external_review"]["automatic"] = False

    assert _publish(supervisor) == 1

    assert github.posted == []
    assert _wakes(store, WakeReason.EXTERNAL_REVIEW_TRIGGER_NEEDED.value) == []
    assert _task(store).state is TaskState.AWAITING_EXTERNAL_REVIEW


def test_correction_with_outstanding_round_and_retrigger_policy_still_wakes(
    tmp_path: Path,
) -> None:
    """With `retrigger_after_correction: true`, the operator-requested round that the
    corrected head still owes is exactly what the wake is for."""
    store, _clock, supervisor, github, _publisher = _correcting(tmp_path)
    store.review_cycles.rows.clear()
    store.policies.policy.document["external_review"]["automatic"] = False
    store.policies.policy.document["external_review"]["retrigger_after_correction"] = True

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


# ----- hades #343 finding 01M4CDWQN8D4HWG5X1567MFQPH: a remembered Codex refusal must
# not suppress another provider's trigger. -----------------------------------------------


def test_a_remembered_codex_refusal_does_not_block_another_providers_trigger() -> None:
    other_provider = {
        "external_review": {
            "provider": "other-reviewer",
            "required_rounds": 1,
            "request_on_publish": True,
        }
    }
    refused_repo = _repository()
    refused_repo.codex_review_refused_at = NOW
    assert external_review_requires_person(other_provider, refused_repo) is False

    codex = {
        **other_provider,
        "external_review": {**other_provider["external_review"], "provider": "codex"},
    }
    assert external_review_requires_person(codex, refused_repo) is True


# ----- hades #343 finding 01M4CDWQN5B02NV9KQX5Y81BJH: a routine repository update must
# not silently clear a remembered Codex refusal. -----------------------------------------


class _FakeSession:
    """Just enough of a SQLAlchemy `Session` for `Repositories.upsert`: a `scalar` that
    returns the one row this test seeded, regardless of the statement, `add`, and a
    no-op `flush`. A real session round-tripping a row through sqlite loses the
    timezone offset `ensure_utc` then rejects (a sqlite-only quirk the real, postgres,
    database does not have); this stays off the database entirely."""

    def __init__(self, existing: RepositoryRow | None) -> None:
        self._existing = existing
        self.added: list[RepositoryRow] = []

    def scalar(self, _statement: object) -> RepositoryRow | None:
        return self._existing

    def add(self, row: RepositoryRow) -> None:
        self.added.append(row)

    def flush(self) -> None:
        return None


def test_re_registering_a_repository_preserves_a_remembered_refusal() -> None:
    """`register_repository` builds a fresh `Repository` that never carries the marker
    forward (its dataclass default is `None`), so the real persistence `upsert` is what
    must not let a routine update clear it."""
    existing_row = RepositoryRow(
        id="repository",
        name="example/repo",
        url="https://github.com/example/repo",
        default_branch="main",
        installation_id=1,
        policy_name="default-software",
        registered_by="operator",
        created_at=NOW,
        external_review_attested=False,
        attested_by=None,
        attested_at=None,
        private=False,
        codex_review_refused_at=NOW,
    )
    repos = Repositories(cast(Session, _FakeSession(existing_row)))
    # The registration path never reads the marker forward; this reproduces what
    # `register_repository` passes to `upsert` on a routine update (a new installation
    # id, the marker left at its dataclass default of `None`).
    candidate = replace(_repository(), installation_id=2, codex_review_refused_at=None)

    updated = repos.upsert(candidate)

    assert updated.installation_id == 2
    assert updated.codex_review_refused_at is not None
