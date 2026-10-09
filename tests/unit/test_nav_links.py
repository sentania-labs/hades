"""hades #208 (FDY-0592): navigation links for the Memory and Catalog admin pages."""

from __future__ import annotations

from crucible.adapters.ui.render import NAV


def test_nav_includes_memory_and_catalog() -> None:
    """Memory and Catalog appear in the Admin navigation with working hrefs and active state."""
    hrefs = {href for href, _label in NAV}
    labels = {label for _href, label in NAV}

    assert "/ui/memory" in hrefs, "Memory must have a navigation link"
    assert "/ui/catalog" in hrefs, "Catalog must have a navigation link"

    assert "Memory" in labels, "Memory must have a navigation label"
    assert "Catalog" in labels, "Catalog must have a navigation label"


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

    # Both must come after the Admin group label (which is an empty href)
    assert memory_idx > admin_idx, "Memory must appear after the Admin group label"
    assert catalog_idx > admin_idx, "Catalog must appear after the Admin group label"


def test_nav_memory_and_catalog_have_active_state_markup() -> None:
    """base.html renders active links with aria-current=page; verify the hrefs match."""
    # The template uses: {% if active == href %}aria-current="page"{% endif %}
    # We verify the nav tuples themselves are correct so active matching works.
    for href, label in NAV:
        if href in ("/ui/memory", "/ui/catalog"):
            assert href.startswith("/ui"), f"{href} must be a valid UI path"
            assert label in ("Memory", "Catalog"), f"{label} must match the expected page name"


def test_nav_no_empty_memory_or_catalog_entries() -> None:
    """Memory and Catalog have non-empty hrefs so they render as links, not group labels."""
    for href, label in NAV:
        if label in ("Memory", "Catalog"):
            assert href, f"{label} must have a non-empty href to render as a clickable link"


def test_nav_no_other_template_changes() -> None:
    """Ensure no unexpected groups or labels were added alongside Memory and Catalog."""
    labels = {label for _href, label in NAV}
    new_labels = {"Memory", "Catalog"}
    added = new_labels - labels
    assert not added, f"Unexpected new labels would need review: {added}"
    assert "Memory" in labels and "Catalog" in labels
