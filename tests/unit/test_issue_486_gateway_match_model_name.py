"""Hades #486: gateway offerings identify routes by model_name, not routing id."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from crucible.application.admin import credentials, gateway
from crucible.application.admin.context import AdminContext
from crucible.domain.entities import Principal, Role

URL = "http://gateway.internal:4000/v1"


def _entries() -> list[dict[str, Any]]:
    return [
        {
            "id": "coder",
            "harness": "hermes",
            "model_name": "coder",
            "endpoint": "local",
            "endpoint_url": URL,
            "pool": "lab-local",
            "enabled": True,
        },
        {
            "id": "qwen-coder",
            "harness": "qwen_code",
            "model_name": "coder",
            "endpoint": "local",
            "endpoint_url": URL,
            "pool": "lab-local",
            "enabled": True,
        },
        {
            "id": "stale",
            "harness": "hermes",
            "model_name": None,
            "endpoint": "local",
            "endpoint_url": URL,
            "pool": "lab-local",
            "enabled": True,
        },
    ]


def _context() -> AdminContext:
    return cast(
        AdminContext,
        SimpleNamespace(clock=SimpleNamespace(now=lambda: datetime(2026, 10, 6, tzinfo=UTC))),
    )


def test_listing_matches_every_route_by_model_name_and_names_its_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway, "gateway_url", lambda _uow: (URL, "routing"))
    monkeypatch.setattr(gateway, "_local_entries", lambda _uow: (_entries(), {}))
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args: "key")
    monkeypatch.setattr(gateway, "fetch_models", lambda *_args: ["coder"])

    view = asyncio.run(gateway.models_view(_context(), cast(Any, object())))
    rows = {row["id"]: row for row in view["models"]}

    assert rows["coder"]["offered"] is True
    assert rows["qwen-coder"]["offered"] is True
    assert rows["coder"]["in_policy"] is True
    assert rows["qwen-coder"]["in_policy"] is True
    assert rows["coder"]["note"] == "hermes: in use"
    assert rows["qwen-coder"]["note"] == "qwen_code: in use"
    assert "saving disables" not in rows["qwen-coder"]["note"]
    assert rows["stale"]["offered"] is False


def test_save_keeps_shared_model_name_routes_and_disables_an_unoffered_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "version": 38,
        "models": _entries(),
        "pools": {"lab-local": {"max_concurrency": 4}},
    }
    policy = SimpleNamespace(name="default", version=1)
    routing = SimpleNamespace(name="route", version=38, document=document)
    saved: dict[str, Any] = {}
    monkeypatch.setattr(gateway, "guard_mutation", lambda *_args, **_kwargs: "issue 486")
    monkeypatch.setattr(gateway, "gateway_url", lambda _uow: (URL, "routing"))
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args: "key")
    monkeypatch.setattr(gateway, "fetch_models", lambda *_args: ["coder"])
    monkeypatch.setattr(gateway, "active_documents", lambda _uow: (policy, routing))
    monkeypatch.setattr(gateway, "parse_routing_policy", lambda value: value)

    def publish(*_args: Any, **kwargs: Any) -> tuple[int, int]:
        saved.update(deepcopy(kwargs["routing_document"]))
        return 2, 39

    monkeypatch.setattr(gateway, "publish_routing", publish)
    monkeypatch.setattr(gateway, "admin_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway, "gateway_view", lambda *_args, **_kwargs: {})
    principal = Principal(
        id="p", name="operator", role=Role.ADMIN, created_at=datetime(2026, 10, 6, tzinfo=UTC)
    )

    result = asyncio.run(
        gateway.save_models(
            _context(),
            cast(Any, object()),
            principal=principal,
            models=[
                {"id": "coder", "enabled": True},
                {"id": "qwen-coder", "enabled": True},
                {"id": "stale", "enabled": True},
            ],
            max_concurrency=None,
            reason="issue 486",
        )
    )

    enabled = {row["id"]: row["enabled"] for row in saved["models"]}
    assert enabled == {"coder": True, "qwen-coder": True, "stale": False}
    assert result["disabled_not_offered"] == ["stale"]
