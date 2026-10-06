"""Issue 172: Routing Details renders sentences and a collapsed policy JSON block.

This test file fails on main because _routing_policy_details does not yet exist,
and the Routing page still uses _document_section for policy documents.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from typing import Any

from crucible.adapters.ui.pages import routing as routing_module
from crucible.adapters.ui.render import _routing_policy_details, templates


def _routing(**overrides: Any) -> dict[str, Any]:
    """A minimal RoutingPolicyV1 document."""
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "name": "default-routing",
        "version": 9,
        "tiers": {
            "trivial": {"allowed_capability": ["small", "mid"], "prefer": ["small"]},
            "standard": {"allowed_capability": ["mid", "small"], "prefer": ["mid"]},
            "complex": {"allowed_capability": ["frontier", "mid"], "prefer": ["frontier"]},
        },
        "models": [
            {
                "id": "claude-haiku",
                "harness": "claude_code",
                "enabled": True,
                "endpoint_url": "https://api.example.com",
            },
            {
                "id": "gemini-flash",
                "harness": "agy",
                "enabled": True,
                "endpoint_url": "https://api.example.com",
            },
            {"id": "coder", "harness": "hermes", "enabled": True},
        ],
        "pools": {
            "anthropic-sub": {
                "window": "5h",
                "budget_units": "attempts",
                "soft_limit": 0,
                "max_concurrency": 10,
            },
            "google-sub": {
                "window": "3h",
                "budget_units": "attempts",
                "soft_limit": 0,
                "max_concurrency": 5,
            },
            "lab-local": {
                "window": "5h",
                "budget_units": "attempts",
                "soft_limit": 0,
                "max_concurrency": 2,
            },
        },
        "rotation": {
            "strategy": "weighted-least-recent",
            "quality_feedback": True,
            "quality_window": 20,
        },
    }
    document.update(overrides)
    return document


def _delivery(**overrides: Any) -> dict[str, Any]:
    """A minimal delivery policy document."""
    return {
        "schema_version": "1.0",
        "name": "default-software",
        "version": 5,
        "routing": {"policy": {"name": "default-routing", "version": 9}},
        "gate": [{"command": "python3 -m unittest tests.test_x"}],
    }


class TestRoutingPolicyDetails:
    """The new _routing_policy_details function replaces _document_section for
    policy documents in the Routing page (issue 172)."""

    # AC1: Routing Details renders no nested key-value table of a policy document.

    def test_no_panel_nested_table(self) -> None:
        """_routing_policy_details must NOT produce a panel with a table,
        which is the nested key-value table that _document_section creates."""
        routing_doc = _routing()
        section = _routing_policy_details(routing_doc)

        # The section uses columns/rows (flat table) not panel (nested table)
        assert "panel" not in section, "must not contain a panel (nested key-value table)"

        # The details use text/JSON blocks, not panels
        for detail in section.get("details", []):
            assert "panel" not in detail, "detail must not contain a panel"

    def test_no_document_section_on_routing_page(self) -> None:
        """The routing page must not use _document_section for policy documents.
        It uses _routing_policy_details instead."""
        source = routing_module.__file__
        content = pathlib.Path(source).read_text(encoding="utf-8")
        # The policy documents in details should come from _routing_policy_details,
        # not from _document_section calls inside the details list.
        # Check that there is no _document_section call for "Delivery policy" or
        # "Routing policy" in the details list.
        lines = content.splitlines()
        # Find the in_force section details list
        in_force_start = None
        for i, line in enumerate(lines):
            if '"In force"' in line and "title" in line:
                in_force_start = i
                break
        assert in_force_start is not None, "In force section not found"
        # Check the next 200 lines for _document_section on policy docs
        snippet = "\n".join(lines[in_force_start : in_force_start + 200])
        assert '_document_section("Delivery policy document"' not in snippet, (
            "In force section must not use _document_section for delivery policy"
        )
        assert '_document_section("Routing policy document"' not in snippet, (
            "In force section must not use _document_section for routing policy"
        )

    # AC2: A collapsed View policy JSON block contains the raw documents.

    def test_collapsed_json_block_exists(self) -> None:
        """The section must have details_label 'View policy JSON' and non-empty details."""
        routing_doc = _routing()
        section = _routing_policy_details(routing_doc)

        assert section["details_label"] == "View policy JSON"
        assert len(section["details"]) >= 1
        for detail in section["details"]:
            assert "title" in detail
            assert "text" in detail
            assert isinstance(detail["text"], str)

    def test_routing_json_in_detail_block(self) -> None:
        """The routing policy document is present in the detail block."""
        routing_doc = _routing()
        section = _routing_policy_details(routing_doc)

        routing_detail = next(
            (d for d in section["details"] if d["title"] == "Routing policy"), None
        )
        assert routing_detail is not None
        assert '"name": "default-routing"' in routing_detail["text"]

    def test_delivery_json_in_detail_block(self) -> None:
        """The delivery policy document is present when provided."""
        routing_doc = _routing()
        delivery_doc = _delivery()
        section = _routing_policy_details(routing_doc, delivery_policy=delivery_doc)

        delivery_detail = next(
            (d for d in section["details"] if d["title"] == "Delivery policy"), None
        )
        assert delivery_detail is not None
        assert '"name": "default-software"' in delivery_detail["text"]

    def test_no_policy_details_when_none(self) -> None:
        """When routing_policy is None, details is empty."""
        section = _routing_policy_details(None)

        assert section["details"] == []
        assert "No routing policy is loaded" in section["note"]

    # AC3: Model rows show only name, on or off, and harness by default.

    def test_model_row_columns(self) -> None:
        """Model rows show id, state, and harness columns."""
        routing_doc = _routing()
        section = _routing_policy_details(routing_doc)

        columns = section["columns"]
        assert isinstance(columns, list)
        assert len(columns) == 3
        assert "Model" in columns
        assert "State" in columns
        assert "Harness" in columns

        # Only basic columns by default
        assert len(columns) == 3

    def test_model_row_values(self) -> None:
        """Each row has id, 'on'/'off', and harness."""
        routing_doc = _routing()
        section = _routing_policy_details(routing_doc)

        for row in section["rows"]:
            assert len(row) == 3
            model_id, state, harness = row
            assert isinstance(model_id, str) and model_id
            assert state in ("on", "off")
            assert isinstance(harness, str) and harness

    def test_enabled_model_shows_on(self) -> None:
        """An enabled model row shows 'on'."""
        routing_doc = _routing()
        section = _routing_policy_details(routing_doc)

        for row in section["rows"]:
            if row[0] == "claude-haiku":
                assert row[1] == "on"

    def test_disabled_model_shows_off(self) -> None:
        """A disabled model row shows 'off'."""
        routing_doc = _routing(models=[{"id": "test-model", "harness": "hermes", "enabled": False}])
        section = _routing_policy_details(routing_doc)

        for row in section["rows"]:
            if row[0] == "test-model":
                assert row[1] == "off"

    # Sentences about harnesses, pools, and exhaustion.

    def test_harness_sentence_present(self) -> None:
        """The note includes per-harness sentences."""
        routing_doc = _routing()
        section = _routing_policy_details(routing_doc)

        note = section["note"]
        assert "hermes" in note.lower()

    def test_pool_concurrency_sentence(self) -> None:
        """The note includes pool concurrency info."""
        routing_doc = _routing()
        section = _routing_policy_details(routing_doc)

        note = section["note"]
        assert "run at once" in note.lower()

    def test_pool_exhaustion_sentence(self) -> None:
        """The note includes pool exhaustion behavior."""
        routing_doc = _routing()
        section = _routing_policy_details(routing_doc)

        note = section["note"]
        assert "pool is exhausted" in note.lower()


class TestRoutingPageTemplateRender:
    """End-to-end: the full template renders the routing details section without error.

    This covers the template path that crashed on PR 492 / head e075a88:
    ``safe_value(column, cell)`` where ``column`` was a dict instead of a string.
    """

    def test_routing_details_section_rendered_by_template(self) -> None:
        """The routing details section renders without raising through page.html.

        This was failing with ``AttributeError: 'dict' object has no attribute 'lower'``
        from ``_safe_value`` at ``render.py:245`` when column dicts were passed to the
        template, which feeds them into ``safe_value(column, cell)``.
        """
        routing_doc = _routing()
        detail_section = _routing_policy_details(routing_doc)

        # Build a minimal section that includes the detail section in its details list
        section = {
            "title": "In force",
            "columns": ["Setting", "Value", ""],
            "rows": [
                ["Delivery policy", "default-software version 5", ""],
            ],
            "details": [detail_section],
            "details_label": "View policy JSON",
        }

        # Render through Jinja2 (same path the page template uses)
        rendered = templates.get_template("page.html").render(
            request={"type": "http", "method": "GET", "path": "/ui/routing", "headers": []},
            title="Routing",
            active="/ui/routing",
            nav=(("/ui/routing", "Routing"),),
            principal=SimpleNamespace(name="admin", role=SimpleNamespace(value="admin")),
            csrf="fixture-csrf",
            message=None,
            heading="Routing",
            intro="What routes and limits a task.",
            sections=[section],
            badge=None,
        )

        assert isinstance(rendered, str)
        assert "Routing policy details" in rendered
        assert "claude-haiku" in rendered
        assert "anthropic-sub" in rendered
        assert "run at once" in rendered
        assert "pool is exhausted" in rendered
        assert "default-routing" in rendered
