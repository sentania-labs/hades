"""Hades #265: the installation repository picker is on Repositories, pageable,
sortable and filterable, and registers the repositories ticked (or every one the filter
matches) in one batch.

The page handlers run as the UI runs them, over an in-memory store and a fake GitHub
App directory and client: one organization with more repositories than fit on a page,
one registered already, one archived, one private repository GitHub refuses a checkout
token for, and one registered repository the installation no longer covers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from crucible.adapters.ui.actions import handlers
from crucible.adapters.ui.pages import github as github_page
from crucible.adapters.ui.pages import repositories as repositories_page
from crucible.application.admin import github
from crucible.application.admin.repositories import PickerFilter, picker_matches, picker_page
from crucible.domain.entities import Event, Principal, Repository, Role
from crucible.domain.events import EventKind
from crucible.ports.github import GitHubError, InstallationToken

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
ADMIN = Principal(id="01ADMIN2650000000000000001", name="scott", role=Role.ADMIN, created_at=NOW)
BULK = [f"octo/repo-{n:02d}" for n in range(60)]


def _covered(full_name: str, **overrides: Any) -> dict[str, Any]:
    return {
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "default_branch": "main",
        "private": False,
        "archived": False,
        **overrides,
    }


class FakeApps:
    """The App directory: what each installation covers, as GitHub lists it."""

    def __init__(self) -> None:
        self.covered: dict[int, list[dict[str, Any]]] = {
            101: [
                *(_covered(name) for name in BULK),
                _covered("octo/widgets"),
                _covered("octo/old", archived=True),
                _covered("octo/secret", private=True, default_branch="trunk"),
                _covered("octo/hidden-gem", default_branch="develop"),
            ],
            202: [_covered("someone/notes")],
        }

    def app(self) -> dict[str, Any]:
        return {
            "id": 42,
            "slug": "hades",
            "name": "Hades",
            "html_url": "https://github.com/apps/hades",
        }

    def installations(self) -> list[dict[str, Any]]:
        return [
            {"id": 202, "account": "someone", "account_type": "User"},
            {"id": 101, "account": "octo", "account_type": "Organization"},
        ]

    def installation_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        return list(self.covered[installation_id])


class FakeGitHub:
    """The App's client: a checkout token for every private repository but
    `octo/secret`, whose installation cannot read it."""

    def configured(self) -> bool:
        return True

    def checkout_token(self, *, installation_id: int, repository: str) -> InstallationToken:
        if repository == "octo/secret":
            raise GitHubError(403, "Resource not accessible by integration")
        return InstallationToken(
            "test-token", expires_at=NOW + timedelta(hours=1), repository=repository
        )

    def revoke_token(self, token: InstallationToken) -> bool:
        return True


class Repositories:
    def __init__(self, rows: list[Repository]) -> None:
        self.rows = {row.name: row for row in rows}

    def list_all(self) -> list[Repository]:
        return list(self.rows.values())

    def get_by_name(self, name: str) -> Repository | None:
        return self.rows.get(name)

    def upsert(self, repository: Repository) -> Repository:
        self.rows[repository.name] = repository
        return repository


class Events:
    def __init__(self) -> None:
        self.rows: list[Event] = []

    def append(self, event: Event) -> Event:
        event.seq = len(self.rows) + 1
        self.rows.append(event)
        return event

    def list_global(self, *, after_seq: int, kind: str | None, limit: int, **_: Any) -> list[Event]:
        return [
            event
            for event in self.rows
            if event.seq is not None
            and event.seq > after_seq
            and (kind is None or event.kind == kind)
        ][:limit]


class Policies:
    def list_names(self) -> list[str]:
        return ["default-software"]

    def list_versions(self, name: str) -> list[Any]:
        return [SimpleNamespace(name=name, version=1, document={})]


class Clock:
    def now(self) -> datetime:
        return NOW


class Store:
    def __init__(self) -> None:
        self.repositories = Repositories(
            [
                _registered("widgets", "https://github.com/octo/widgets", 101),
                # Registered against installation 101, which no longer covers it.
                _registered("gone", "https://github.com/octo/gone", 101),
            ]
        )
        self.events = Events()
        self.policies = Policies()
        self.provider_settings = SimpleNamespace(get=lambda _name: None)
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def _registered(name: str, url: str, installation_id: int) -> Repository:
    return Repository(
        id=name,
        name=name,
        url=url,
        default_branch="main",
        policy_name="default-software",
        installation_id=installation_id,
        registered_by="scott",
        created_at=NOW,
    )


def _context() -> Any:
    admin = SimpleNamespace(
        github=FakeGitHub(),
        github_apps=FakeApps(),
        github_credentials=None,
        github_app=SimpleNamespace(
            app_id=42,
            api_base="https://api.github.com",
            private_key_path=None,
            webhook_secret_path=None,
            webhook_enabled=False,
        ),
        clock=Clock(),
    )
    return SimpleNamespace(admin=admin)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Store]:
    # The App's visibility is a live HEAD to GitHub; never asked from a unit test.
    monkeypatch.setattr(github, "_is_public", lambda _ctx: True)
    for module in (repositories_page, github_page):
        monkeypatch.setattr(module, "_require", lambda *_args: (ADMIN, "csrf-value"))
    return _context(), Store()


def _request(ctx: Any, path: str, query: dict[str, str] | None = None) -> Request:
    app = FastAPI()
    app.state.ctx = ctx
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "app": app,
            "query_string": urlencode(query or {}).encode(),
        }
    )


def _page(ctx: Any, store: Store, query: dict[str, str] | None = None) -> str:
    request = _request(ctx, "/ui/repositories", query)
    response = repositories_page.repositories_page(request, ctx, cast(Any, store))
    return bytes(response.body).decode()


async def _post(ctx: Any, store: Store, form: dict[str, str]) -> str:
    handler = handlers["repository-register-batch"]
    request = _request(ctx, "/ui/actions/repository-register-batch")
    response = await handler(
        request,
        "repository-register-batch",
        ctx,
        cast(Any, store),
        ADMIN,
        "csrf-value",
        form,
        form.get("reason"),
    )
    assert response is not None and response.status_code == 303
    location = response.headers["location"]
    assert len(location) < 200
    assert "message=" not in location
    assert "set-cookie" not in response.headers
    query = dict(parse_qsl(urlsplit(location).query))
    original = dict(parse_qsl(urlsplit(form.get("return_to", "")).query))
    for key in ("name", "registered", "private", "archived", "sort", "page"):
        if key in original:
            assert query[key] == original[key]
    return _page(ctx, store, query)


def _section(body: str, installation_id: int) -> str:
    start = body.index(f'id="installation-{installation_id}"')
    end = body.find("<section", start)
    return body[start : end if end != -1 else len(body)]


# ----- AC1: open Repositories, filter an installation by name, register one -------------


@pytest.mark.asyncio
async def test_filter_an_installation_by_name_and_register_one(world: tuple[Any, Store]) -> None:
    ctx, store = world
    body = _page(ctx, store)
    octo = _section(body, 101)
    assert "octo (Organization), installation 101" in octo
    assert "Covers 64, registered 1, registered but no longer covered 1." in octo
    assert "No longer covered: gone." in octo
    # A page, not the whole organization.
    assert "Page 1 of 3: 64 of 64 match this filter." in octo
    assert "octo/hidden-gem" in octo and "octo/repo-22" in octo
    assert "octo/repo-23" not in octo and "octo/widgets" not in octo
    assert "page=2" in octo and "Next page" in octo
    # The other installation keeps its own default view.
    assert "someone/notes" in _section(body, 202)

    filtered = _section(_page(ctx, store, {"installation": "101", "name": "GEM"}), 101)
    assert "Page 1 of 1: 1 of 64 match this filter." in filtered
    assert 'name="pick:octo/hidden-gem"' in filtered
    assert "octo/repo-00" not in filtered
    assert 'value="/ui/repositories?installation=101&amp;name=GEM"' in filtered

    location = await _post(
        ctx,
        store,
        {
            "installation_id": "101",
            "pick:octo/hidden-gem": "true",
            "policy_name": "default-software",
            "attested_all_prs": "true",
            "reason": "deliver to the gem",
            "return_to": "/ui/repositories?installation=101&name=GEM",
        },
    )
    assert "Batch registration result" in location
    assert "Registered 1: octo/hidden-gem as hidden-gem." in location
    gem = store.repositories.rows["hidden-gem"]
    assert (gem.url, gem.default_branch, gem.installation_id) == (
        "https://github.com/octo/hidden-gem",
        "develop",
        101,
    )
    assert store.commits == 1
    again = _section(_page(ctx, store, {"installation": "101", "name": "gem"}), 101)
    assert 'name="pick:octo/hidden-gem"' not in again and "hidden-gem" in again


def test_sort_and_the_three_filters(world: tuple[Any, Store]) -> None:
    ctx, store = world
    registered_first = _section(
        _page(ctx, store, {"installation": "101", "sort": "registered"}), 101
    )
    rows = registered_first[registered_first.index("<tbody>") :]
    assert rows.index("octo/widgets") < rows.index("octo/hidden-gem")

    covered = github.installations_view(ctx.admin, cast(Any, store))["installations"][0][
        "repositories"
    ]

    def names(**query: str) -> list[str]:
        return [r["full_name"] for r in picker_matches(covered, PickerFilter.from_query(query))]

    assert names(registered="yes") == ["octo/widgets"]
    assert names(private="yes") == ["octo/secret"]
    assert names(archived="yes") == ["octo/old"]
    assert "octo/old" not in names(archived="no") and len(names(archived="no")) == 63
    assert names(name="repo-5", sort="registered") == [f"octo/repo-5{n}" for n in range(10)]
    # An unknown choice, sort or page shows the default view rather than refusing it.
    odd = PickerFilter.from_query({"registered": "maybe", "sort": "stars", "page": "x"})
    assert odd == PickerFilter()
    last = picker_page(covered, PickerFilter(page=99))
    assert (last["page"], last["pages"], len(last["repositories"])) == (3, 3, 14)


# ----- AC2: a batch registers every chosen one and names each skipped one --------------


@pytest.mark.asyncio
async def test_batch_registers_every_chosen_and_lists_skipped_with_reasons(
    world: tuple[Any, Store],
) -> None:
    ctx, store = world
    location = await _post(
        ctx,
        store,
        {
            "installation_id": "101",
            "pick:octo/repo-01": "true",
            "pick:octo/repo-02": "true",
            "pick:octo/widgets": "true",
            "pick:octo/old": "true",
            "pick:octo/secret": "true",
            "pick:octo/elsewhere": "true",
            "policy_name": "default-software",
            "attested_all_prs": "true",
            "attested_by": "reviewer-bot",
            "reason": "first wave",
            "return_to": "/ui/repositories?installation=101",
        },
    )
    assert "Registered 2: octo/repo-01 as repo-01, octo/repo-02 as repo-02." in location
    assert "Skipped 4:" in location
    assert "octo/widgets (already registered as widgets)" in location
    assert "octo/old (archived: cannot take a pull request)" in location
    assert "octo/secret (refusing to register:" in location
    assert "octo/elsewhere (not covered by installation 101)" in location
    for name in ("repo-01", "repo-02"):
        row = store.repositories.rows[name]
        assert row.policy_name == "default-software" and row.installation_id == 101
        assert row.external_review_attested and row.attested_by == "reviewer-bot"
    assert "secret" not in store.repositories.rows and "old" not in store.repositories.rows

    registered = [e for e in store.events.rows if e.kind == EventKind.REPOSITORY_REGISTERED.value]
    assert [e.payload["repository"] for e in registered] == ["repo-01", "repo-02"]
    assert {e.payload["reason"] for e in registered} == {"first wave"}

    result = github.add_repositories(
        ctx.admin,
        cast(Any, store),
        principal="scott",
        installation_id=101,
        repositories=["octo/repo-01", "octo/old", "octo/secret", "octo/elsewhere"],
        policy_name="default-software",
        attested_all_prs=True,
        attested_by=None,
        reason=None,
    )
    assert result["registered"] == []
    assert [(s["repository"], s["cause"]) for s in result["skipped"]] == [
        ("octo/repo-01", "already_registered"),
        ("octo/old", "archived"),
        ("octo/secret", "unsupported"),
        ("octo/elsewhere", "unsupported"),
    ]


@pytest.mark.asyncio
async def test_select_all_takes_every_repository_the_filter_matches_on_every_page(
    world: tuple[Any, Store],
) -> None:
    ctx, store = world
    page = _section(_page(ctx, store, {"installation": "101", "name": "repo-"}), 101)
    assert "Select all 60 matching this filter, on every page" in page
    assert "Page 1 of 3" in page
    location = await _post(
        ctx,
        store,
        {
            "installation_id": "101",
            "select_all": "true",
            "filter_name": "repo-",
            "filter_registered": "no",
            "filter_private": "any",
            "filter_archived": "any",
            "policy_name": "default-software",
            "attested_all_prs": "true",
            "reason": "the whole repo- family",
            "return_to": "/ui/repositories?installation=101&name=repo-",
        },
    )
    assert "Registered 60:" in location and "Skipped" not in location
    assert {f"repo-{n:02d}" for n in range(60)} <= set(store.repositories.rows)
    assert "hidden-gem" not in store.repositories.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("select_all", [True, False])
async def test_large_batch_result_survives_redirect_and_reload(
    world: tuple[Any, Store], select_all: bool
) -> None:
    ctx, store = world
    names = [f"octo/ordinary-repository-{n:03d}" for n in range(250)]
    ctx.admin.github_apps.covered[101].extend(_covered(name) for name in names)
    chosen = [*names, "octo/widgets", "octo/old", "octo/secret"]
    form = {
        "installation_id": "101",
        "attested_all_prs": "true",
        "reason": "register a large batch",
    }
    if select_all:
        form["select_all"] = "true"
    else:
        form.update({f"pick:{name}": "true" for name in chosen})
    body = unescape(await _post(ctx, store, form))
    event = store.events.rows[-1]
    assert event.kind == EventKind.REPOSITORY_BATCH_REGISTERED.value
    assert store.commits == 1
    for name in names:
        assert f"{name} as {name.split('/')[1]}" in body
        assert name.split("/")[1] in store.repositories.rows
    for skipped in event.payload["after"]["skipped"]:
        assert f"{skipped['repository']} ({skipped['reason']})" in body
    assert len(event.payload["after"]["skipped"]) == 3
    query = {"batch_result": str(event.seq)}
    # A fresh application context needs no process-local flash or browser cookie.
    reloaded = unescape(_page(_context(), store, query))
    assert repositories_page.batch_message(event.payload["after"]) in reloaded
    assert store.commits == 1
    # Another batch must not overwrite the first, including an all-skipped batch.
    skipped_body = await _post(ctx, store, form)
    assert "Registered none." in skipped_body
    assert repositories_page.batch_message(event.payload["after"]) in unescape(
        _page(ctx, store, query)
    )
    for reference in ("invalid", "0", "-1", "9" * 100, "9223372036854775808", "9999", "1"):
        assert "Batch result not found." in _page(ctx, store, {"batch_result": reference})
    event.principal = "another-admin"
    assert "Batch result not found." in _page(ctx, store, query)


@pytest.mark.asyncio
async def test_a_batch_refused_as_a_whole_registers_nothing(world: tuple[Any, Store]) -> None:
    ctx, store = world
    # The default policy asks for external review; without the attestation no one of
    # the batch can be registered, so the batch is refused before any is.
    with pytest.raises(Exception, match="attestation"):
        await _post(
            ctx,
            store,
            {
                "installation_id": "101",
                "pick:octo/repo-01": "true",
                "policy_name": "default-software",
                "reason": "no attestation",
            },
        )
    assert store.commits == 0
    with pytest.raises(Exception, match="no repositories were chosen"):
        await _post(ctx, store, {"installation_id": "101", "reason": "nothing ticked"})


# ----- AC3: the GitHub page links to Repositories and carries no picker -----------------


def test_github_page_links_to_repositories_and_has_no_picker(world: tuple[Any, Store]) -> None:
    ctx, store = world
    request = _request(ctx, "/ui/github")
    body = bytes(github_page.github_page(request, ctx, cast(Any, store)).body).decode()
    assert "Install on GitHub" in body
    assert 'href="/ui/repositories"' in body
    assert 'href="/ui/repositories?installation=101#installation-101"' in body
    for picker in (
        "github-add-repository",
        "repository-register-batch",
        "pick:",
        "octo/repo-00",
        "octo/widgets",
        'name="repository"',
    ):
        assert picker not in body, picker
    # Nothing the GitHub page shows lists an installation's repositories.
    assert (
        github.apps_view(ctx.admin, cast(Any, store), repositories=False)["installations"][0]["id"]
        == 101
    )


def test_no_em_dashes_in_the_pages_or_the_change() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "crucible/adapters/ui/pages/repositories.py",
        "crucible/adapters/ui/pages/github.py",
        "crucible/application/admin/repositories.py",
        "crucible/application/admin/github.py",
        "crucible/adapters/ui/templates/page.html",
        "tests/unit/test_issue_265_repositories_picker.py",
    ):
        assert chr(0x2014) not in (root / relative).read_text(encoding="utf-8"), relative
