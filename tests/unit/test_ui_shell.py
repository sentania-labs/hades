"""hades #576 U5, #169, #214: the shell every signed-in page shares.

Every registered page renders for an administrator with the grouped navigation (Work,
Set up while a step is undone, Admin, and Diagnostics closed), its own entry active, the
phone drawer, and no table markup that scrolls sideways. The pages run on the SQLite
workload of the issue 485 benchmark, so this stays in the unit tier."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer
from sqlalchemy.orm import Session

from crucible.adapters.persistence import models as m
from crucible.adapters.persistence.migrations.versions._0001_walking_skeleton import (
    DEFAULT_POLICY,
)
from crucible.adapters.persistence.migrations.versions._0008_harness_adapters import (
    VERIFIED_ROUTING,
)
from crucible.adapters.ui import render
from crucible.adapters.ui.router import router
from crucible.adapters.ui.session import COOKIE, _serializer
from crucible.application.admin import setup
from crucible.domain.entities import UiSession
from tests.unit.issue_485_fixture import workload

TASK = "task-00000"
ATTEMPT = "attempt-0"

# Every GET route under /ui is a page this test renders, or is listed here with why it
# is not one, so a new page is rendered here the day it is registered.
NOT_PAGES = {
    "/ui": "redirects to Set up or the Board",
    "/ui/sign-in": "the signed-out form, outside the shell",
    "/ui/github/callback": "the GitHub App manifest return, a redirect",
    "/ui/github/installed": "the GitHub App installation return, a redirect",
    "/ui/routing/models": "an anchor on Routing, a redirect",
    "/ui/routing/tiers": "an anchor on Routing, a redirect",
    "/ui/room/{room_id}/stream": "an event stream",
    "/ui/artifacts/{artifact_id}/content": "an artifact's bytes",
    "/ui/bootstrap/{import_id}": "needs a bootstrap import the workload has none of",
}

PATH_VALUES = {"{task_id}": TASK, "{attempt_id}": ATTEMPT, "{harness}": "codex"}

OVERFLOW_MARKUP = ("lat-table-scroll", "admin-table-wrap", "overflow-x", "overflow:")


class _NoSession:
    """The unit of work without its SQL session, so the Board's and Usage's Postgres-only
    bulk reads take the repository path they keep for a store without one."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __enter__(self) -> _NoSession:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._inner.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        if name == "session":
            raise AttributeError(name)
        return getattr(self._inner, name)


@pytest.fixture(scope="module")
def shell(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, Any]]:
    # SQLite numbers a row only through an INTEGER primary key.
    widened = [
        column
        for table in m.Base.metadata.tables.values()
        for column in table.columns
        if column.primary_key and isinstance(column.type, BigInteger)
    ]
    for column in widened:
        column.type = Integer()
    try:
        app, ctx, engine = workload(Path(tmp_path_factory.mktemp("shell")) / "db.sqlite", 3)
    finally:
        for column in widened:
            column.type = BigInteger()
    now = datetime.now(UTC)
    with Session(engine) as db:
        # The oldest pending wake is read as an aggregate, which SQLite returns without its
        # offset once another test has configured the mappers; this test needs no wakes.
        db.query(m.WakeRow).delete()
        db.add(
            m.RoutingPolicyRow(
                name="default-routing", version=2, document=VERIFIED_ROUTING, created_at=now
            )
        )
        policy = copy.deepcopy(DEFAULT_POLICY)
        policy["routing"] = {"policy": {"name": "default-routing", "version": 2}}
        db.add(m.PolicyRow(name="default-software", version=1, document=policy, created_at=now))
        db.commit()
    with ctx.uow_factory() as uow:
        uow.ui_sessions.create(
            UiSession(
                id="shell-session",
                principal_id="operator",
                csrf="shell-csrf",
                created_at=now,
                expires_at=now + timedelta(hours=1),
                last_seen_at=now,
            )
        )
        uow.commit()
    factory = ctx.uow_factory

    def without_session() -> _NoSession:
        return _NoSession(factory())

    ctx.uow_factory = without_session
    ctx.admin.uow_factory = without_session
    client = TestClient(app)
    client.cookies.set(COOKIE, _serializer(ctx).dumps("shell-session"))
    yield client, ctx
    engine.dispose()


def _pages() -> list[str]:
    paths = sorted(
        {
            route.path
            for route in router.routes
            if isinstance(route, APIRoute) and "GET" in (route.methods or set())
        }
    )
    pages = []
    for route_path in paths:
        if route_path in NOT_PAGES:
            continue
        path = route_path
        for placeholder, value in PATH_VALUES.items():
            path = path.replace(placeholder, value)
        assert "{" not in path, f"{route_path} is neither rendered here nor listed in NOT_PAGES"
        pages.append(path)
    return pages


def _nav(body: str) -> str:
    start = body.index('id="shell-nav"')
    return body[start : body.index("</nav>", start)]


def test_every_page_is_listed_or_rendered() -> None:
    pages = _pages()
    assert "/ui/setup" in pages and "/ui/board" in pages and "/ui/settings" in pages
    assert len(pages) >= 25


@pytest.mark.parametrize("path", _pages())
def test_every_page_renders_for_an_admin_with_the_navigation(
    shell: tuple[TestClient, Any], path: str
) -> None:
    client, _ctx = shell
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200, (path, response.text[:400])
    body = response.text
    nav = _nav(body)
    for group in ("Work", "Admin", "Diagnostics"):
        assert f'data-group="{group}"' in nav, (path, group)
    for href, _label in (entry for _, entries in render.NAV_GROUPS for entry in entries):
        assert f'href="{href}"' in nav, (path, href)
    # The drawer a phone opens the navigation with.
    assert 'id="shell-drawer"' in body and 'for="shell-drawer"' in body
    # Its own entry is the active one, and only one is.
    assert nav.count('aria-current="page"') == 1, path
    for marker in OVERFLOW_MARKUP:
        assert marker not in body, (path, marker)
    for table in re.findall(r"<table[^>]*>", body):
        assert "neon-table" in table or "card-" in table, (path, table)


@pytest.mark.parametrize(
    ("path", "href"),
    [
        ("/ui/board", "/ui/board"),
        ("/ui/tasks/" + TASK, "/ui/tasks"),
        ("/ui/room", "/ui/room"),
        ("/ui/policies", "/ui/policies"),
        ("/ui/catalog", "/ui/catalog"),
        ("/ui/wakes", "/ui/wakes"),
        ("/ui/setup", "/ui/setup"),
    ],
)
def test_the_active_entry_is_the_pages_own(
    shell: tuple[TestClient, Any], path: str, href: str
) -> None:
    client, _ctx = shell
    nav = _nav(client.get(path).text)
    assert re.search(rf'href="{re.escape(href)}"[^>]*aria-current="page"', nav), (path, nav)


def test_diagnostics_is_closed_until_its_page_is_shown(shell: tuple[TestClient, Any]) -> None:
    client, _ctx = shell
    closed = _nav(client.get("/ui/board").text)
    opened = _nav(client.get("/ui/wakes").text)
    assert re.search(r'<details[^>]*data-group="Diagnostics">', closed)
    assert re.search(r'<details[^>]*data-group="Diagnostics" open>', opened)
    order = [closed.index(f'data-group="{name}"') for name in ("Work", "Admin", "Diagnostics")]
    assert order == sorted(order)


def test_set_up_is_shown_with_its_count_while_a_step_is_undone(
    shell: tuple[TestClient, Any],
) -> None:
    client, ctx = shell
    with ctx.uow_factory() as uow:
        remaining = setup.undone_count(setup.setup_steps(ctx.admin, uow))
    assert remaining > 0
    nav = _nav(client.get("/ui/board").text)
    assert nav.index('href="/ui/setup"') < nav.index('data-group="Work"')
    assert re.search(rf'class="shell-count"[^>]*>{remaining}<', nav)
    landing = client.get("/ui", follow_redirects=False)
    assert landing.status_code == 303 and landing.headers["location"] == "/ui/setup"


def test_set_up_disappears_once_every_step_is_done(
    shell: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ctx = shell
    real = setup.setup_steps

    def all_done(ctx: Any, uow: Any) -> list[dict[str, Any]]:
        return [{**step, "done": True} for step in real(ctx, uow)]

    monkeypatch.setattr(setup, "setup_steps", all_done)
    body = client.get("/ui/board").text
    assert 'href="/ui/setup"' not in _nav(body)
    landing = client.get("/ui", follow_redirects=False)
    assert landing.headers["location"] == "/ui/board"
    page = client.get("/ui/setup").text
    assert "Every first-run step is done." in page


def test_the_set_up_page_lists_the_five_steps_in_order_with_their_pages(
    shell: tuple[TestClient, Any],
) -> None:
    client, ctx = shell
    body = client.get("/ui/setup").text
    assert "<h1>Set up</h1>" in body
    found = re.findall(
        r'data-step="(\w+)".*?<b>([^<]+)</b> <span class="lat-badge lat-badge--(\w+)">'
        r'(done|not done)</span>.*?href="([^"]+)"',
        body,
    )
    assert [(key, label, href) for key, label, _tone, _state, href in found] == [
        ("github_app", "GitHub App", "/ui/github"),
        ("repository", "Repository", "/ui/repositories"),
        ("harness_login", "Harness login", "/ui/credentials"),
        ("routing", "Routing", "/ui/routing"),
        ("first_task", "First task", "/ui/room"),
    ]
    with ctx.uow_factory() as uow:
        live = {step["key"]: step["done"] for step in setup.setup_steps(ctx.admin, uow)}
    assert {key: state == "done" for key, _l, _t, state, _h in found} == live
    # The workload registers a repository, enables routing and holds tasks; it has no App
    # and no harness that has logged in.
    assert live == {
        "github_app": False,
        "repository": True,
        "harness_login": False,
        "routing": True,
        "first_task": True,
    }
    # The next step to do is the one primary action on the list.
    assert body.count("lat-btn--primary") == 1
    assert re.search(r'is-next" data-step="github_app"', body)


def test_the_board_carries_the_service_strip(shell: tuple[TestClient, Any]) -> None:
    client, _ctx = shell
    body = client.get("/ui/board").text
    strip = body[body.index('class="shell-service-strip"') :]
    strip = strip[: strip.index("</nav>")]
    for label in ("Supervisor", "Wakes", "Set up", "Version"):
        assert f"<span>{label}</span>" in strip, label
    assert "shell-service-strip" not in client.get("/ui/tasks").text


def test_the_about_block_shows_the_version_and_image_digest(
    shell: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ctx = shell
    digest = "sha256:" + "0" * 64
    monkeypatch.setattr(ctx.settings.service, "image", f"ghcr.io/example/hades:1@{digest}")
    body = client.get("/ui/settings").text
    about = body[body.index('id="about"') :]
    about = about[: about.index("</section>")]
    import crucible  # noqa: PLC0415

    assert crucible.__version__ in about
    assert "Image digest" in about and digest in about
    monkeypatch.setattr(ctx.settings.service, "image", "")
    assert "not reported" in client.get("/ui/settings").text


def test_a_sections_details_render_once(shell: tuple[TestClient, Any]) -> None:
    client, _ctx = shell
    body = client.get("/ui/settings").text
    assert body.count("Defaults left unchanged") == 1
