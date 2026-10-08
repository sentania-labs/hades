"""Hades #486 compatibility under the #513 routing-reference model."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from crucible.application.admin import credentials, gateway
from crucible.application.admin.context import AdminContext

URL = "http://gateway.internal:4000/v1"


def _entries() -> list[dict[str, Any]]:
    return [
        {"model": "coder", "harness": harness, "endpoint": "local", "enabled": True}
        for harness in ("hermes", "qwen_code")
    ] + [
        {
            "model": "stale",
            "harness": "hermes",
            "endpoint": "local",
            "enabled": False,
            "vanished_at": "2026-10-06T00:00:00+00:00",
        }
    ]


def _context() -> AdminContext:
    return cast(
        AdminContext,
        SimpleNamespace(clock=SimpleNamespace(now=lambda: datetime(2026, 10, 6, tzinfo=UTC))),
    )


def test_listing_groups_routes_under_the_gateway_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "gateway_url", lambda _uow: (URL, "routing"))
    monkeypatch.setattr(gateway, "_local_entries", lambda _uow: (_entries(), {}))
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args: "key")
    monkeypatch.setattr(gateway, "fetch_models", lambda *_args: ["coder"])

    view = asyncio.run(gateway.models_view(_context(), cast(Any, object())))

    assert [row["model"] for row in view["models"]] == ["coder", "stale"]
    assert [item["harness"] for item in view["models"][0]["harnesses"]] == [
        "hermes",
        "qwen_code",
    ]
    assert view["models"][1]["offered"] is False


def test_legacy_route_id_is_never_the_displayed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = {
        "id": "qwen-coder",
        "model_name": "coder",
        "harness": "qwen_code",
        "endpoint": "local",
        "enabled": True,
    }
    monkeypatch.setattr(gateway, "gateway_url", lambda _uow: (URL, "routing"))
    monkeypatch.setattr(gateway, "_local_entries", lambda _uow: ([legacy], {}))
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args: "key")
    monkeypatch.setattr(gateway, "fetch_models", lambda *_args: ["coder"])

    row = asyncio.run(gateway.models_view(_context(), cast(Any, object())))["models"][0]

    assert row["model"] == "coder"
    assert "qwen-coder" not in repr(row)
