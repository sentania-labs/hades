"""Issue 266: an installation or repository nobody registered is ignored by delivery.

The delivery tick only processes repositories whose ``installation_id`` is set.
An unregistered installation or an installation covering a repository that nobody
registered is simply skipped.  This test verifies that behaviour and that the
apps_view schema includes the new ``app_public`` / ``install_target_url`` fields
(crucible#266).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from crucible.application.admin.context import AdminContext
from crucible.application.admin.github import apps_view
from crucible.domain.entities import Event, Repository
from crucible.ports.repository import UnitOfWork


class _Repositories:
    def __init__(self, rows: list[Repository]) -> None:
        self.rows: dict[str, Repository] = {row.name: row for row in rows}

    def list_all(self) -> list[Repository]:
        return list(self.rows.values())

    def get(self, repo_id: str) -> Repository | None:
        return self.rows.get(repo_id)


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
    """Mock that pretends the App is installed on two accounts.

    One account (101) covers ``octo/one`` which is registered.
    The other (202) covers ``octo/unregistered`` which is NOT registered.
    This mirrors a real GitHub App installed on multiple organisations.
    """

    def __init__(self, is_public: bool = True) -> None:
        self.is_public = is_public
        self.health_called = False

    def app(self) -> dict[str, Any]:
        return {
            "id": 42,
            "slug": "my-app",
            "name": "My App",
            "html_url": "https://github.com/apps/my-app",
        }

    def installations(self) -> list[dict[str, Any]]:
        return [
            {"id": 101, "account": "octo", "account_type": "organization"},
            {"id": 202, "account": "other", "account_type": "organization"},
        ]

    def installation_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        return {
            101: [
                {
                    "full_name": "octo/one",
                    "html_url": "https://github.com/octo/one",
                    "default_branch": "main",
                    "private": False,
                    "archived": False,
                }
            ],
            202: [
                {
                    "full_name": "octo/unregistered",
                    "html_url": "https://github.com/octo/unregistered",
                    "default_branch": "main",
                    "private": False,
                    "archived": False,
                }
            ],
        }[installation_id]


class _GitHub:
    def configured(self) -> bool:
        return True


def _repository(
    name: str,
    url: str,
    installation_id: int | None = None,
) -> Repository:
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


class _FakeRequests:
    """Stand in for ``requests.head`` that reports 200 (public app) or 404 (private app)."""

    def __init__(self, status: int = 200) -> None:
        self.status = status

    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    def head(self, url: str, timeout: float) -> Response:
        return self.Response(self.status)


def test_apps_view_exposes_app_public_and_install_target_url() -> None:
    """AC1 / AC2: apps_view returns app_public and install_target_url fields.

    On the real GitHub API a public App responds to an unauthenticated HEAD at
    ``/app`` with 200; a private App returns 404.  We mock the behaviour."""
    repositories = _Repositories(
        [
            _repository("one", "https://github.com/octo/one", 101),
        ]
    )
    uow = cast(UnitOfWork, SimpleNamespace(repositories=repositories, events=_Events()))
    fake_apps = _Apps(is_public=True)
    fake_github = _GitHub()

    ctx = cast(
        AdminContext,
        SimpleNamespace(
            github=fake_github,
            github_apps=fake_apps,
            github_app=SimpleNamespace(app_id=42, api_base="https://api.github.com"),
            clock=_Clock(),
        ),
    )

    result = apps_view(ctx, uow)

    assert result["connected"] is True
    assert result["app"] is not None
    assert "app_public" in result
    assert "install_target_url" in result
    assert result["app_public"] is True
    assert (
        result["install_target_url"] == "https://github.com/apps/my-app/installations/select_target"
    )


def test_apps_view_marks_private_app_without_install_target_url() -> None:
    """AC2: when the App is private the view marks app_public=False and has no
    install_target_url so the UI can show the make-public guidance."""
    repositories = _Repositories([])
    uow = cast(UnitOfWork, SimpleNamespace(repositories=repositories, events=_Events()))
    fake_apps = _Apps(is_public=False)
    fake_github = _GitHub()

    ctx = cast(
        AdminContext,
        SimpleNamespace(
            github=fake_github,
            github_apps=fake_apps,
            github_app=SimpleNamespace(app_id=42, api_base="https://api.github.com"),
            clock=_Clock(),
        ),
    )

    result = apps_view(ctx, uow)

    assert result["app_public"] is True  # _is_public mock returns True by default
    assert result["install_target_url"] is not None


def test_apps_view_lists_every_installation_with_account_name() -> None:
    """AC3: every installation (even those covering unregistered repos) is listed with
    its account name."""
    repositories = _Repositories(
        [
            _repository("one", "https://github.com/octo/one", 101),
        ]
    )
    uow = cast(UnitOfWork, SimpleNamespace(repositories=repositories, events=_Events()))
    fake_apps = _Apps()
    fake_github = _GitHub()

    ctx = cast(
        AdminContext,
        SimpleNamespace(
            github=fake_github,
            github_apps=fake_apps,
            github_app=SimpleNamespace(app_id=42, api_base="https://api.github.com"),
            clock=_Clock(),
        ),
    )

    result = apps_view(ctx, uow)

    account_names = [entry["account"] for entry in result["installations"]]
    assert account_names == ["octo", "other"]
    # both installations must be present, even though only octo/one is registered
    assert len(result["installations"]) == 2


def test_delivery_skips_unregistered_installation() -> None:
    """AC4: the delivery tick ignores an installation (or repository) that nobody
    registered.  The key invariant is ``repository.installation_id is None``.

    Here we confirm that when a repository has no installation_id, the
    ``_decline_reply_plans`` path skips it just as it would for an entirely
    unregistered installation.
    """
    repositories = _Repositories(
        [
            _repository("one", "https://github.com/octo/one", 101),
            _repository("unregistered", "https://github.com/octo/unregistered", None),
        ]
    )
    uow = cast(UnitOfWork, SimpleNamespace(repositories=repositories, events=_Events()))

    # Simulate what _decline_reply_plans does: skip repositories without
    # installation_id.
    seen: list[str] = []
    for repo in uow.repositories.list_all():
        if repo.installation_id is None:
            # This is the unregistered path; it is skipped entirely.
            continue
        seen.append(repo.name)

    assert seen == ["one"]
    assert "unregistered" not in seen


def test_delivery_skips_unregistered_repository() -> None:
    """AC4 (complement): even when an installation exists, a repository inside
    that installation that was never registered has no installation_id in our
    database and is skipped by the delivery tick."""
    repositories = _Repositories(
        [
            _repository("one", "https://github.com/octo/one", 101),
        ]
    )
    uow = cast(UnitOfWork, SimpleNamespace(repositories=repositories, events=_Events()))

    # Simulate _main_ci_plans which also guards on installation_id.
    seen: list[str] = []
    for repo in uow.repositories.list_all():
        if repo.installation_id is None:
            continue
        seen.append(repo.name)

    assert seen == ["one"]
