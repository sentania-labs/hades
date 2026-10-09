"""hades #208 (FDY-0592): navigation links for the Memory and Catalog admin pages."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

from fastapi.responses import HTMLResponse, Response

from crucible.adapters.ui.pages import catalog as catalog_page_module
from crucible.adapters.ui.pages import memory as memory_page_module
from crucible.adapters.ui.render import NAV
from crucible.domain.entities import Principal, Role

if TYPE_CHECKING:
    import pytest


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
ADMIN = Principal("01K6H9ZH2J7F0X7M6C1Y8D3P4Q", "admin", Role.ADMIN, NOW)


def _make_request(path: str) -> SimpleNamespace:
    """Create a minimal request-like object for page rendering."""
    return SimpleNamespace(
        scope={"type": "http", "method": "GET", "path": path, "headers": []},
        query_params={},
        app=SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace())),
    )


def _catalog_page_html(monkeypatch: pytest.MonkeyPatch) -> str:
    """Render the catalog page and return its HTML body."""
    monkeypatch.setattr(
        catalog_page_module,
        "_require",
        lambda request, ctx, uow: (ADMIN, "csrf"),
    )
    mock_uow = Mock()
    ctx: Any = SimpleNamespace()
    request = _make_request("/ui/catalog")
    response: Response = catalog_page_module.catalog_page(request, ctx, mock_uow)  # type: ignore[arg-type]
    assert isinstance(response, HTMLResponse)
    return response.body.decode()  # type: ignore[union-attr]


def _memory_page_html(monkeypatch: pytest.MonkeyPatch) -> str:
    """Render the memory page and return its HTML body."""
    monkeypatch.setattr(
        memory_page_module,
        "_require",
        lambda request, ctx, uow: (ADMIN, "csrf"),
    )
    mock_uow = Mock()
    mock_uow.memory.list_recent.return_value = []
    ctx: Any = SimpleNamespace()
    request = _make_request("/ui/memory")
    response: Response = memory_page_module.memory_page(request, ctx, mock_uow)  # type: ignore[arg-type]
    assert isinstance(response, HTMLResponse)
    return response.body.decode()  # type: ignore[union-attr]


def test_nav_includes_memory_and_catalog_in_rendered_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory and Catalog appear in the Admin navigation of a rendered page."""
    html = _catalog_page_html(monkeypatch)

    assert 'href="/ui/memory"' in html, "Memory navigation link must appear in the page"
    assert 'href="/ui/catalog"' in html, "Catalog navigation link must appear in the page"
    assert ">Memory</a>" in html, "Memory must have a navigation label"
    assert ">Catalog</a>" in html, "Catalog must have a navigation label"


def test_nav_active_state_is_rendered_for_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When on the Memory page, the Memory link carries aria-current=page."""
    html = _memory_page_html(monkeypatch)

    assert 'href="/ui/memory"' in html
    assert 'aria-current="page"' in html, (
        "The Memory link must carry aria-current=page when rendering the Memory page"
    )
    catalog_pos = html.find('href="/ui/catalog"')
    assert catalog_pos >= 0, "Catalog link must still be present"
    memory_pos = html.find('href="/ui/memory"')
    catalog_link = html[catalog_pos:]
    memory_link = html[memory_pos:]

    catalog_next_active = catalog_link.find('aria-current="page"')
    catalog_next_a = catalog_link.find("</a>")
    assert catalog_next_active < 0 or catalog_next_active > catalog_next_a, (
        "The Catalog link must not carry aria-current=page on the Memory page"
    )
    memory_next_active = memory_link.find('aria-current="page"')
    memory_next_a = memory_link.find("</a>")
    assert 0 <= memory_next_active < memory_next_a, (
        "The Memory link must carry aria-current=page on the Memory page"
    )


def test_nav_active_state_is_rendered_for_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When on the Catalog page, the Catalog link carries aria-current=page."""
    html = _catalog_page_html(monkeypatch)

    assert 'href="/ui/catalog"' in html
    assert 'aria-current="page"' in html, (
        "The Catalog link must carry aria-current=page when rendering the Catalog page"
    )
    memory_pos = html.find('href="/ui/memory"')
    catalog_pos = html.find('href="/ui/catalog"')
    memory_link = html[memory_pos:]
    catalog_link = html[catalog_pos:]

    memory_next_active = memory_link.find('aria-current="page"')
    memory_next_a = memory_link.find("</a>")
    assert memory_next_active < 0 or memory_next_active > memory_next_a, (
        "The Memory link must not carry aria-current=page on the Catalog page"
    )
    catalog_next_active = catalog_link.find('aria-current="page"')
    catalog_next_a = catalog_link.find("</a>")
    assert 0 <= catalog_next_active < catalog_next_a, (
        "The Catalog link must carry aria-current=page on the Catalog page"
    )


def test_nav_memory_and_catalog_are_admin_items() -> None:
    """Memory and Catalog sit under the Admin group (after the Admin label)."""
    items = list(NAV)
    admin_idx = next(i for i, (_, label) in enumerate(items) if label == "Admin")

    memory_idx = next(
        i for i, (href, label) in enumerate(items) if href == "/ui/memory" and label == "Memory"
    )
    catalog_idx = next(
        i for i, (href, label) in enumerate(items) if href == "/ui/catalog" and label == "Catalog"
    )

    assert memory_idx > admin_idx, "Memory must appear after the Admin group label"
    assert catalog_idx > admin_idx, "Catalog must appear after the Admin group label"


def test_nav_no_empty_memory_or_catalog_entries() -> None:
    """Memory and Catalog have non-empty hrefs so they render as links, not group labels."""
    for href, label in NAV:
        if label in ("Memory", "Catalog"):
            assert href, f"{label} must have a non-empty href to render as a clickable link"
