"""Regression coverage for the UI shell merge at cf0b0ad8."""

from __future__ import annotations

from unittest.mock import Mock

from crucible.adapters.ui import render
from crucible.adapters.ui.pages.work import work_page


def test_work_pages_keep_the_shell_render_compatibility_hook() -> None:
    """The Work renderer imports this hook while Diagnostics stays in navigation."""
    assert work_page is not None
    assert render._empty_sections(Mock()) == frozenset()
