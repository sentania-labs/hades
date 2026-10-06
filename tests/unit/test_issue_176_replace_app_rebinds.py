"""Issue 176: installing a replacement App rebinds existing repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from crucible.application.admin.context import AdminContext
from crucible.application.admin.github import rebind_repositories
from crucible.domain.entities import Event, Repository
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork


class _Repositories:
    def __init__(self, rows: list[Repository]) -> None:
        self.rows = {row.name: row for row in rows}

    def list_all(self) -> list[Repository]:
        return list(self.rows.values())

    def upsert(self, repository: Repository) -> Repository:
        self.rows[repository.name] = repository
        return repository


class _Events:
    def __init__(self) -> None:
        self.rows: list[Event] = []

    def append(self, event: Event) -> Event:
        event.seq = len(self.rows) + 1
        self.rows.append(event)
        return event


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 10, 6, tzinfo=UTC)


class _Apps:
    def installations(self) -> list[dict[str, Any]]:
        return [{"id": 202}, {"id": 101}]

    def installation_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        return {
            101: [{"full_name": "octo/one", "html_url": "https://github.com/octo/one"}],
            202: [{"full_name": "octo/two", "html_url": "https://github.com/octo/two"}],
        }[installation_id]


def _repository(name: str, url: str, installation_id: int) -> Repository:
    return Repository(
        id=name,
        name=name,
        url=url,
        default_branch="main",
        policy_name="default-software",
        installation_id=installation_id,
        registered_by="admin",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octo/one.git",
        "https://github.com/octo/one",
        "https://github.com/OCTO/ONE.git/",
        "git@github.com:octo/one.git",
        "git@github.com:OCTO/ONE",
        "ssh://git@github.com/octo/one.git",
        "ssh://git@github.com:22/octo/one.git",
    ],
)
def test_installing_replacement_rebinds_visible_repositories_and_audits_each(url: str) -> None:
    repositories = _Repositories(
        [
            _repository("one", url, 1),
            _repository("two", "https://github.com/octo/two", 2),
        ]
    )
    events = _Events()
    uow = cast(UnitOfWork, SimpleNamespace(repositories=repositories, events=events))
    ctx = cast(
        AdminContext,
        SimpleNamespace(
            github_apps=_Apps(),
            github_app=SimpleNamespace(app_id=8, api_base="https://api.github.com"),
            clock=_Clock(),
        ),
    )

    result = rebind_repositories(ctx, uow, principal="operator")

    assert result == {"rebound": ["one", "two"], "unavailable": []}
    assert repositories.rows["one"].installation_id == 101
    assert repositories.rows["two"].installation_id == 202
    assert repositories.rows["one"].url == url
    assert [event.kind for event in events.rows] == [
        EventKind.REPOSITORY_REBOUND.value,
        EventKind.REPOSITORY_REBOUND.value,
    ]
    assert [event.payload["repository"] for event in events.rows] == ["one", "two"]
    assert events.rows[0].payload["before"]["installation_id"] == 1
    assert events.rows[0].payload["after"]["installation_id"] == 101


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octo/hidden",
        "git@github.com:octo/hidden.git",
        "git@elsewhere.example:octo/one.git",
        "https://elsewhere.example/octo/one",
    ],
)
def test_repository_hidden_from_new_app_is_named_and_left_unchanged(url: str) -> None:
    hidden = _repository("hidden-repository", url, 77)
    repositories = _Repositories([hidden])
    events = _Events()
    uow = cast(UnitOfWork, SimpleNamespace(repositories=repositories, events=events))
    ctx = cast(
        AdminContext,
        SimpleNamespace(
            github_apps=_Apps(),
            github_app=SimpleNamespace(app_id=8, api_base="https://api.github.com"),
            clock=_Clock(),
        ),
    )

    result = rebind_repositories(ctx, uow, principal="operator")

    assert result == {"rebound": [], "unavailable": ["hidden-repository"]}
    assert repositories.rows["hidden-repository"] is hidden
    assert hidden.installation_id == 77
    assert events.rows == []
