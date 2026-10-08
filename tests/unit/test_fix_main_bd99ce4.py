"""Red main after #467 (bd99ce4): the Images page gives Qwen Code one control (issue 186).

The row for `qwen_code` offers a single `/ui/actions/image-change` form with one
dropdown, the previous image marked `(previous)` and no Reason input; choosing the
previous image rolls back, choosing any other promotes. There is no
`/ui/actions/image-promote` any more."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.harness.registry import default_registry
from crucible.adapters.ui.actions import handlers as ui_handlers
from crucible.adapters.ui.pages.images import _image_rows
from crucible.application.admin import images
from crucible.domain.entities import HarnessImage
from crucible.ports.execution import ImageInfo

HARNESS = "qwen_code"
CURRENT = ImageInfo(
    reference="crucible-worker:0.2.0",
    digest="sha256:" + "a" * 64,
    harnesses={"hermes": "0.19.0", HARNESS: "0.25.0"},
)
PREVIOUS = ImageInfo(
    reference="crucible-worker:0.1.0",
    digest="sha256:" + "b" * 64,
    harnesses={"hermes": "0.19.0", HARNESS: "0.25.0"},
)


def _stored() -> HarnessImage:
    return HarnessImage(
        harness=HARNESS,
        digest=CURRENT.digest,
        reference=CURRENT.reference,
        version="0.25.0",
        updated_at=datetime(2026, 10, 1, tzinfo=UTC),
        updated_by="admin",
        previous_digest=PREVIOUS.digest,
        previous_reference=PREVIOUS.reference,
        previous_version="0.25.0",
    )


def _uow() -> Any:
    stored = _stored()
    return SimpleNamespace(
        harness_images=SimpleNamespace(get=lambda name: stored if name == HARNESS else None)
    )


@pytest.fixture
def admin_ctx(monkeypatch: pytest.MonkeyPatch) -> Any:
    async def listed(_ctx: Any) -> list[tuple[str, ImageInfo]]:
        return [("kubernetes", CURRENT), ("kubernetes", PREVIOUS)]

    monkeypatch.setattr(images, "provider_images", listed)
    return SimpleNamespace(harnesses=default_registry())


async def _qwen_action(ctx: Any) -> dict[str, Any]:
    rows = await images.defaults(ctx, _uow())
    qwen = next(row for row in rows if row["harness"] == HARNESS)
    rendered = _image_rows([qwen], admin=True)
    actions = rendered[0][-1]
    assert actions["kind"] == "actions"
    items: list[dict[str, Any]] = actions["items"]
    assert len(items) == 1, "one action per row"
    return items[0]


async def test_qwen_row_has_one_image_change_action(admin_ctx: Any) -> None:
    action = await _qwen_action(admin_ctx)
    assert action["kind"] == "form"
    assert action["action"] == "/ui/actions/image-change"
    assert action["label"] == "Change image"
    assert action["hidden"] == {"harness": HARNESS}
    assert "reason" not in action


async def test_qwen_dropdown_marks_the_previous_image(admin_ctx: Any) -> None:
    action = await _qwen_action(admin_ctx)
    select = action["select"]
    assert select["name"] == "digest"
    assert select["selected"] == CURRENT.digest
    assert select["options"] == [
        (CURRENT.digest, CURRENT.reference),
        (PREVIOUS.digest, f"{PREVIOUS.reference} (previous)"),
    ]


def test_promote_and_rollback_actions_are_not_registered() -> None:
    assert "image-change" in ui_handlers
    assert "image-promote" not in ui_handlers
    assert "image-rollback" not in ui_handlers


@pytest.mark.parametrize(
    ("digest", "expected"),
    [(PREVIOUS.digest, "rollback"), ("sha256:" + "c" * 64, "promote")],
)
async def test_qwen_choice_dispatches_rollback_or_promote(
    monkeypatch: pytest.MonkeyPatch, digest: str, expected: str
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def promote(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("promote", kwargs))
        return {}

    async def rollback(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("rollback", kwargs))
        return {}

    monkeypatch.setattr(images, "promote", promote)
    monkeypatch.setattr(images, "rollback", rollback)
    ctx: Any = SimpleNamespace(admin=SimpleNamespace())
    principal: Any = SimpleNamespace(name="admin")
    request: Any = SimpleNamespace()
    form = {"harness": HARNESS, "digest": digest}

    await ui_handlers["image-change"](
        request, "image-change", ctx, _uow(), principal, "csrf", form, None
    )

    assert [name for name, _ in calls] == [expected]
    kwargs = calls[0][1]
    assert kwargs["harness"] == HARNESS
    assert kwargs["principal"] == "admin"
    assert kwargs["reason"] is None
    if expected == "promote":
        assert kwargs["digest"] == digest
