# mypy: ignore-errors
"""Issue 186: Images page, one dropdown per row, previous image marked, no reason inputs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from crucible.adapters.ui.actions import handlers as ui_handlers
from crucible.adapters.ui.pages import images as images_ui
from crucible.adapters.ui.render import templates as jinja_templates
from crucible.domain.entities import HarnessImage
from tests.unit.admin_ui_fixtures import base_context


def _make_row(
    harness: str,
    current: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    choices: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "harness": harness,
        "current": current,
        "previous": previous,
        "choices": choices,
        "supported_versions": ">=1.0",
    }


class TestImagesRowLayout:
    """Each row has one dropdown and one action; no reason inputs."""

    def test_single_form_per_row(self) -> None:
        """One form with a select per harness row."""
        rows = [
            _make_row(
                "hermes",
                current={"reference": "w:1.0", "digest": "sha256:a", "version": "1.0"},
                previous={"reference": "w:0.9", "digest": "sha256:b", "version": "0.9"},
                choices=[
                    {"digest": "sha256:a", "reference": "w:1.0"},
                    {"digest": "sha256:c", "reference": "w:2.0"},
                ],
            ),
        ]
        result = images_ui._image_rows(rows, admin=True)
        assert len(result) == 1
        harness_col, _current_col, _prev_col, actions_col = result[0]
        assert harness_col == "hermes"
        assert actions_col["kind"] == "actions"
        items = actions_col["items"]
        assert len(items) == 1, "one form per row"
        form = items[0]
        assert form["kind"] == "form"
        assert form["label"] == "Change image"
        assert form["action"] == "/ui/actions/image-change"
        assert "reason" not in form, "no reason field on row action"

    def test_select_contains_all_choices(self) -> None:
        """The dropdown has one option per digest in choices plus previous if not
        in choices."""
        rows = [
            _make_row(
                "hermes",
                current={"reference": "w:1.0", "digest": "sha256:a", "version": "1.0"},
                previous={"reference": "w:0.9", "digest": "sha256:b", "version": "0.9"},
                choices=[
                    {"digest": "sha256:a", "reference": "w:1.0"},
                    {"digest": "sha256:c", "reference": "w:2.0"},
                ],
            ),
        ]
        result = images_ui._image_rows(rows, admin=True)
        form = result[0][3]["items"][0]
        options = form["select"]["options"]
        values = [opt[0] for opt in options]
        # sha256:b (previous) is not in choices, so it should be added
        assert "sha256:a" in values
        assert "sha256:c" in values
        assert "sha256:b" in values
        assert len(values) == 3

    def test_select_includes_choices_when_previous_in_choices(self) -> None:
        """When previous digest is in the choices, it gets the (previous) label
        but still only once."""
        rows = [
            _make_row(
                "hermes",
                current={"reference": "w:1.0", "digest": "sha256:a", "version": "1.0"},
                previous={"reference": "w:0.9", "digest": "sha256:b", "version": "0.9"},
                choices=[
                    {"digest": "sha256:a", "reference": "w:1.0"},
                    {"digest": "sha256:b", "reference": "w:0.9"},
                    {"digest": "sha256:c", "reference": "w:2.0"},
                ],
            ),
        ]
        result = images_ui._image_rows(rows, admin=True)
        form = result[0][3]["items"][0]
        options = form["select"]["options"]
        values = [opt[0] for opt in options]
        assert "sha256:a" in values
        assert "sha256:b" in values
        assert "sha256:c" in values
        # Previous is in choices, so no extra entry
        assert len(values) == 3

    def test_previous_marked_with_label(self) -> None:
        """The previous image in the dropdown has '(previous)' suffix."""
        rows = [
            _make_row(
                "hermes",
                current={"reference": "w:1.0", "digest": "sha256:a", "version": "1.0"},
                previous={"reference": "w:0.9", "digest": "sha256:b", "version": "0.9"},
                choices=[
                    {"digest": "sha256:a", "reference": "w:1.0"},
                    {"digest": "sha256:b", "reference": "w:0.9"},
                    {"digest": "sha256:c", "reference": "w:2.0"},
                ],
            ),
        ]
        result = images_ui._image_rows(rows, admin=True)
        form = result[0][3]["items"][0]
        options = form["select"]["options"]
        labels = {opt[1]: opt[0] for opt in options}
        # sha256:b is the previous digest; its label should contain "(previous)"
        assert "w:0.9 (previous)" in labels
        assert labels["w:0.9 (previous)"] == "sha256:b"

    def test_previous_digest_not_in_choices_included(self) -> None:
        """When the previous digest is not in the available choices, it still
        appears."""
        rows = [
            _make_row(
                "hermes",
                current={"reference": "w:1.0", "digest": "sha256:a", "version": "1.0"},
                previous={"reference": "w:0.8", "digest": "sha256:x", "version": "0.8"},
                choices=[
                    {"digest": "sha256:a", "reference": "w:1.0"},
                    {"digest": "sha256:c", "reference": "w:2.0"},
                ],
            ),
        ]
        result = images_ui._image_rows(rows, admin=True)
        form = result[0][3]["items"][0]
        options = form["select"]["options"]
        values = [opt[0] for opt in options]
        assert "sha256:x" in values

    def test_no_reason_input_in_rendered_html(self) -> None:
        """The rendered page for /ui/images does not contain a reason input."""
        sections = [
            {
                "title": "Worker image per harness",
                "columns": [
                    "Harness",
                    "Current image",
                    "Previous image",
                    "Change",
                ],
                "rows": [
                    [
                        "hermes",
                        {
                            "kind": "note",
                            "value": "w:1.0",
                            "hint": "hermes 1.0",
                        },
                        "w:0.9 (hermes 0.9)",
                        {
                            "kind": "actions",
                            "items": [
                                {
                                    "kind": "form",
                                    "action": "/ui/actions/image-change",
                                    "label": "Change image",
                                    "primary": True,
                                    "hidden": {"harness": "hermes"},
                                    "select": {
                                        "name": "digest",
                                        "label": "Image for hermes",
                                        "options": [
                                            ("sha256:a", "w:1.0 (previous)"),
                                            ("sha256:c", "w:2.0"),
                                        ],
                                        "selected": "sha256:a",
                                    },
                                }
                            ],
                        },
                    ],
                ],
            },
        ]
        rendered = jinja_templates.get_template("page.html").render(
            **base_context("/ui/images"),
            heading="Images",
            intro="Test",
            sections=sections,
            badge=None,
        )
        assert 'name="reason"' not in rendered


class TestImagesUnifiedAction:
    """The single action endpoint dispatches promote or rollback."""

    @pytest.mark.asyncio
    async def test_promote_when_digest_not_previous(self) -> None:
        """Selecting a digest that is not the previous one triggers promote."""
        uow = SimpleNamespace(
            harness_images=SimpleNamespace(
                get=lambda _h: HarnessImage(  # type: ignore[arg-type]
                    harness="hermes",
                    digest="sha256:a",
                    reference="w:1.0",
                    version="1.0",
                    updated_at=None,  # type: ignore[arg-type]
                    updated_by="admin",
                    reason=None,  # type: ignore[arg-type]
                    previous_digest="sha256:b",
                    previous_reference="w:0.9",
                    previous_version="0.9",
                ),
                put=lambda h: h,
            ),
            commit=lambda: None,
        )

        ctx = SimpleNamespace(
            admin=SimpleNamespace(
                harnesses={
                    "hermes": SimpleNamespace(
                        name="hermes",
                        supported_versions=SimpleNamespace(supports=lambda v: True),
                        default_image="w:1.0",
                    )
                },
                clock=SimpleNamespace(now=lambda: "now"),
            ),
        )

        calls: list[str] = []

        async def fake_promote(*args, **kwargs):
            calls.append("promote")
            return {}

        async def fake_rollback(*args, **kwargs):
            calls.append("rollback")
            return {}

        principal = SimpleNamespace(name="admin")
        form = {"harness": "hermes", "digest": "sha256:c"}

        with (
            patch("crucible.application.admin.images.promote", fake_promote),
            patch("crucible.application.admin.images.rollback", fake_rollback),
        ):
            handler = ui_handlers["image-change"]
            await handler(  # type: ignore[arg-type]
                SimpleNamespace(),  # type: ignore[arg-type]
                "image-change",
                ctx,  # type: ignore[arg-type]
                uow,
                principal,  # type: ignore[arg-type]
                "csrf",
                form,
                None,
            )
            assert calls == ["promote"], f"expected promote, got {calls}"

    @pytest.mark.asyncio
    async def test_rollback_when_digest_is_previous(self) -> None:
        """Selecting the previous digest triggers rollback."""
        uow = SimpleNamespace(
            harness_images=SimpleNamespace(
                get=lambda _h: HarnessImage(  # type: ignore[arg-type]
                    harness="hermes",
                    digest="sha256:a",
                    reference="w:1.0",
                    version="1.0",
                    updated_at=None,  # type: ignore[arg-type]
                    updated_by="admin",
                    reason=None,  # type: ignore[arg-type]
                    previous_digest="sha256:b",
                    previous_reference="w:0.9",
                    previous_version="0.9",
                ),
                put=lambda h: h,
            ),
            commit=lambda: None,
        )

        ctx = SimpleNamespace(
            admin=SimpleNamespace(
                harnesses={
                    "hermes": SimpleNamespace(
                        name="hermes",
                        supported_versions=SimpleNamespace(supports=lambda v: True),
                        default_image="w:1.0",
                    )
                },
                clock=SimpleNamespace(now=lambda: "now"),
            ),
        )

        calls: list[str] = []

        async def fake_promote(*args, **kwargs):
            calls.append("promote")
            return {}

        async def fake_rollback(*args, **kwargs):
            calls.append("rollback")
            return {}

        principal = SimpleNamespace(name="admin")
        # sha256:b is the previous digest
        form = {"harness": "hermes", "digest": "sha256:b"}

        with (
            patch("crucible.application.admin.images.promote", fake_promote),
            patch("crucible.application.admin.images.rollback", fake_rollback),
        ):
            handler = ui_handlers["image-change"]
            await handler(  # type: ignore[arg-type]
                SimpleNamespace(),  # type: ignore[arg-type]
                "image-change",
                ctx,  # type: ignore[arg-type]
                uow,
                principal,  # type: ignore[arg-type]
                "csrf",
                form,
                None,
            )
            assert calls == ["rollback"], f"expected rollback, got {calls}"

    @pytest.mark.asyncio
    async def test_promote_when_no_previous(self) -> None:
        """When there is no previous image, selecting any digest promotes."""
        uow = SimpleNamespace(
            harness_images=SimpleNamespace(
                get=lambda _h: HarnessImage(  # type: ignore[arg-type]
                    harness="hermes",
                    digest="sha256:a",
                    reference="w:1.0",
                    version="1.0",
                    updated_at=None,  # type: ignore[arg-type]
                    updated_by="admin",
                    reason=None,  # type: ignore[arg-type]
                    previous_digest=None,
                    previous_reference=None,
                    previous_version=None,
                ),
                put=lambda h: h,
            ),
            commit=lambda: None,
        )

        ctx = SimpleNamespace(
            admin=SimpleNamespace(
                harnesses={
                    "hermes": SimpleNamespace(
                        name="hermes",
                        supported_versions=SimpleNamespace(supports=lambda v: True),
                        default_image="w:1.0",
                    )
                },
                clock=SimpleNamespace(now=lambda: "now"),
            ),
        )

        calls: list[str] = []

        async def fake_promote(*args, **kwargs):
            calls.append("promote")
            return {}

        async def fake_rollback(*args, **kwargs):
            calls.append("rollback")
            return {}

        principal = SimpleNamespace(name="admin")
        form = {"harness": "hermes", "digest": "sha256:c"}

        with (
            patch("crucible.application.admin.images.promote", fake_promote),
            patch("crucible.application.admin.images.rollback", fake_rollback),
        ):
            handler = ui_handlers["image-change"]
            await handler(  # type: ignore[arg-type]
                SimpleNamespace(),  # type: ignore[arg-type]
                "image-change",
                ctx,  # type: ignore[arg-type]
                uow,
                principal,  # type: ignore[arg-type]
                "csrf",
                form,
                None,
            )
            assert calls == ["promote"], f"expected promote, got {calls}"
