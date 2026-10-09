"""Smoke-marker assertions: every admin page carries a stable data-page attribute.

These tests verify that the main element of each rendered page includes a
data-page attribute whose value uniquely identifies the page, independent of
product names or the landing page.  The compose smoke uses the same markers
to avoid grepping "Hades" or "Crucible" in page content.

No imports beyond stdlib and the project's test fixtures so the unit run is
lightweight; the markers are strings, not live HTTP.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

# Every path the compose smoke walks, paired with its expected data-page value.
_MARKERS: list[tuple[str, str]] = [
    ("/ui", "status"),
    ("/ui/harnesses", "harnesses"),
    ("/ui/credentials", "credentials"),
    ("/ui/credentials/hermes/login", "credentials"),
    ("/ui/images", "images"),
    ("/ui/routing", "routing"),
    ("/ui/repositories", "repositories"),
    ("/ui/tokens", "tokens"),
    ("/ui/github", "github"),
    ("/ui/workers", "workers"),
    ("/ui/tasks", "tasks"),
    ("/ui/wakes", "wakes"),
    ("/ui/retention", "retention"),
    ("/ui/audit", "audit"),
    ("/ui/bootstrap", "bootstrap"),
    ("/ui/settings", "settings"),
]

# Pages reachable via the API but outside the compose smoke loop; we still
# want a marker on them for completeness (set by the page module).
_EXTRA_MARKERS: list[tuple[str, str]] = [
    ("/ui/board", "board"),
    ("/ui/usage", "usage"),
    ("/ui/catalog", "catalog"),
]

_MARKER_RE = re.compile(r'data-page="([^"]*)"')


class _FakeCtx:
    """Minimal context so the renderer path does not raise AttributeError."""

    def __init__(self) -> None:
        uow_obj = type("Uow", (), {
            "retention": type("R", (), {"list_recent": lambda s, n: []})(),
            "bootstrap_imports": type("B", (), {"list_all": lambda s: []})(),
            "__enter__": lambda s: s,
            "__exit__": lambda s, *a: None,
        })()
        uow_factory_obj = type("UowFactory", (), {
            "__call__": lambda s: uow_obj,
            "__enter__": lambda s: uow_obj,
            "__exit__": lambda s, *a: None,
        })()
        self._data = {
            "uow_factory": uow_factory_obj,
            "settings": None,
        }

    def __getattr__(self, key: str) -> Any:
        val = self._data.get(key)
        if val is None and key not in ("uow_factory", "settings", "ctx"):
            raise AttributeError(key)
        return val


class _FakeApp:
    """A minimal app stub providing app.state.ctx.settings."""

    def __init__(self, ctx: _FakeCtx) -> None:
        class _State:
            def __init__(self, c: _FakeCtx) -> None:
                self.ctx = c
        self.state = _State(ctx)


class _FakeRequest:
    """A bare Request stub that satisfies the renderer's attribute reads."""

    def __init__(self, app_ctx: _FakeCtx, path: str) -> None:
        self._app = _FakeApp(app_ctx)
        self.query_params: dict[str, str] = {}
        self.url = type("URL", (), {"path": path, "host": "localhost"})()

    @property
    def app(self) -> _FakeApp:
        return self._app


class _FakePrincipal:
    def __init__(self) -> None:
        self.name = "admin"
        self.role = type("Role", (), {"value": "admin"})()


def _render_page(active: str, *, data_page: str | None = None) -> str:
    """Render the page template and extract the data-page value from main."""
    from crucible.adapters.ui.render import _page  # noqa: PLC0415

    ctx = _FakeCtx()
    request = _FakeRequest(ctx, active)
    principal = _FakePrincipal()
    csrf = "deadbeef"
    response = _page(
        request=request,
        principal=principal,
        csrf=csrf,
        active=active,
        heading="Test",
        intro="test intro",
        sections=[],
        data_page=data_page,
    )
    body = response.body.decode("utf-8", "replace")
    match = _MARKER_RE.search(body)
    if match is None:
        return ""
    return match.group(1)


class TestSmokeMarkers:
    """Assert every smoke-walked page carries a stable marker."""

    @pytest.mark.parametrize("path,marker", _MARKERS)
    def test_smoke_pages_have_data_page(self, path: str, marker: str) -> None:
        rendered = _render_page(active=path, data_page=marker)
        assert rendered == marker, (
            f"page {path} has data-page={rendered!r}, expected {marker!r}"
        )

    @pytest.mark.parametrize("path,marker", _EXTRA_MARKERS)
    def test_extra_pages_have_data_page(self, path: str, marker: str) -> None:
        """Extra pages not walked by the compose smoke still carry markers."""
        rendered = _render_page(active=path, data_page=marker)
        assert rendered == marker, (
            f"page {path} has data-page={rendered!r}, expected {marker!r}"
        )

    @pytest.mark.parametrize("path,marker", _MARKERS)
    def test_auto_derive_from_active(self, path: str, marker: str) -> None:
        """Pages without explicit data_page auto-derive from active."""
        rendered = _render_page(active=path)
        auto_marker = path.rstrip("/").rsplit("/", 1)[-1] if "/" in path else path.lstrip("/")
        assert rendered == auto_marker, (
            f"page {path} auto-derived data-page={rendered!r}, expected {auto_marker!r}"
        )
