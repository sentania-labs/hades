"""Hades #486: gateway offerings identify routes by model_name, not routing id."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import Request

from crucible.adapters.ui.pages import gateway as page
from crucible.application.admin import credentials, gateway, routing
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
    route = SimpleNamespace(name="route", version=38, document=document)
    saved: dict[str, Any] = {}
    monkeypatch.setattr(gateway, "guard_mutation", lambda *_args, **_kwargs: "issue 486")
    monkeypatch.setattr(gateway, "gateway_url", lambda _uow: (URL, "routing"))
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args: "key")
    monkeypatch.setattr(gateway, "fetch_models", lambda *_args: ["coder"])
    monkeypatch.setattr(gateway, "active_documents", lambda _uow: (policy, route))
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


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("fetch", [False, True])
def test_codex_only_model_remains_visible(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, fetch: bool
) -> None:
    entry = {**_entries()[0], "id": "codex-local:coder", "harness": "codex", "enabled": enabled}
    monkeypatch.setattr(gateway, "gateway_url", lambda _uow: (URL, "routing"))
    monkeypatch.setattr(gateway, "_local_entries", lambda _uow: ([entry], {}))
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args: "key")
    monkeypatch.setattr(gateway, "fetch_models", lambda *_args: ["coder"])

    view = asyncio.run(gateway.models_view(_context(), cast(Any, object()), fetch=fetch))

    assert len(view["models"]) == 1
    row = view["models"][0]
    assert row["id"] == row["model_name"] == "coder"
    assert row["in_policy"] is True
    assert row["offered"] is (True if fetch else None)
    assert row["enabled"] is False
    assert row["codex_editable"] is True
    assert row["codex_enabled"] is enabled
    assert row["note"].startswith("codex:")


@pytest.mark.parametrize("codex_only", [False, True])
@pytest.mark.parametrize("picked", [False, True])
def test_ui_save_uses_one_codex_control_per_model_name(
    monkeypatch: pytest.MonkeyPatch, codex_only: bool, picked: bool
) -> None:
    # Put the alias first to ensure the Codex control also works on a route whose
    # id differs from its model name.
    entries = [] if codex_only else list(reversed(_entries()[:2]))
    entries.append({**_entries()[0], "id": "codex-local:coder", "harness": "codex"})
    document = {"models": entries, "pools": {"lab-local": {"max_concurrency": 4}}}
    route = SimpleNamespace(name="route", version=38, document=document)
    policy = SimpleNamespace(name="default", version=1)
    saved: dict[str, Any] = {}
    monkeypatch.setattr(gateway, "guard_mutation", lambda *_args, **_kwargs: "issue 486")
    monkeypatch.setattr(gateway, "gateway_url", lambda _uow: (URL, "routing"))
    monkeypatch.setattr(gateway, "_local_entries", lambda _uow: (entries, {}))
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args: "key")
    monkeypatch.setattr(gateway, "fetch_models", lambda *_args: ["coder"])
    monkeypatch.setattr(gateway, "active_documents", lambda _uow: (policy, route))
    monkeypatch.setattr(gateway, "parse_routing_policy", lambda value: value)
    monkeypatch.setattr(gateway, "admin_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway, "gateway_view", lambda *_args, **_kwargs: {})

    def publish(*_args: Any, **kwargs: Any) -> tuple[int, int]:
        saved.update(deepcopy(kwargs["routing_document"]))
        return 2, 39

    monkeypatch.setattr(gateway, "publish_routing", publish)
    view = asyncio.run(gateway.models_view(_context(), cast(Any, object())))
    assert sum(row["codex_editable"] for row in view["models"]) == 1
    assert sum(row["codex_enabled"] for row in view["models"]) == 1
    principal = Principal(
        id="p", name="operator", role=Role.ADMIN, created_at=datetime(2026, 10, 6, tzinfo=UTC)
    )
    monkeypatch.setattr(page, "_require", lambda *_args: (principal, "csrf"))
    monkeypatch.setattr(credentials, "read_secrets", AsyncMock(return_value={}))
    monkeypatch.setattr(routing, "routing_followers", lambda _uow: {})
    monkeypatch.setattr(gateway, "hermes_limits_view", lambda _uow: {})
    monkeypatch.setattr(page, "_hermes_limits_section", lambda *_args: {})
    monkeypatch.setattr(
        gateway,
        "gateway_view",
        lambda *_args: dict.fromkeys(
            ["last_outcome", "endpoint_url", "key_set", "last_test", "last_tested_at"]
        ),
    )
    shown: dict[str, Any] = {}
    monkeypatch.setattr(page, "_page", lambda *_args, **kwargs: shown.update(kwargs))
    request = Request({"type": "http", "query_string": b"models=1"})
    asyncio.run(
        page.gateway_page(
            request, cast(Any, SimpleNamespace(admin=_context())), cast(Any, object())
        )
    )
    cells = [cell for row in shown["sections"][1]["form"]["fields"][0]["rows"] for cell in row]
    assert sum(cell.get("name", "").endswith(".codex") for cell in cells) == 1
    assert all(
        "saving disables" not in cell.get("value", "") for cell in cells if "name" not in cell
    )
    form = {"confirm": "true"}
    for cell in cells:
        kind = cell.get("kind")
        if kind in {"hidden", "select"}:
            form[cell["name"]] = cell["value"]
        elif kind == "checkbox":
            checked = picked if cell["name"].endswith(".codex") else cell["value"]
            if checked:
                form[cell["name"]] = "true"
    asyncio.run(
        page._action_gateway_models(
            cast(Any, object()),
            "gateway-models",
            cast(Any, SimpleNamespace(admin=_context())),
            cast(Any, SimpleNamespace(commit=lambda: None)),
            principal,
            "csrf",
            form,
            "issue 486",
        )
    )
    assert {row["id"] for row in saved["models"]} == {row["id"] for row in entries}
    for row in saved["models"]:
        assert row["enabled"] is (picked if row["harness"] == "codex" else True)
