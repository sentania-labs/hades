"""Hades #267: successful gateway listings remove stale local routes promptly."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Iterator, cast
from unittest.mock import Mock

import pytest

from crucible.application.admin import credentials, gateway, routing
from crucible.application.admin.context import AdminContext
from crucible.application.supervisor import Supervisor
from crucible.domain.events import EventKind
from tests.fixtures import FakeClock

NOW = datetime(2026, 10, 6, 14, 30, tzinfo=UTC)
LOCAL_URL = "http://gateway.internal:4000/v1"


def _document(*, enabled: bool = True) -> dict[str, Any]:
    return {
        "version": 4,
        "models": [
            {
                "id": "model-a",
                "harness": "hermes",
                "endpoint": "local",
                "model_name": "model-a",
                "enabled": enabled,
            },
            {
                "id": "codex-local:model-a",
                "harness": "codex",
                "endpoint": "local",
                "model_name": "model-a",
                "enabled": enabled,
            },
        ],
    }


class _Events:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def append(self, event: Any) -> Any:
        self.rows.append(event)
        return event


class _Uow:
    def __init__(self) -> None:
        self.events = _Events()
        self.commits = 0

    def set_fenced_token(self, token: int) -> None:
        assert token == 1

    def commit(self) -> None:
        self.commits += 1


def _supervisor(uow: _Uow, ctx: AdminContext) -> Supervisor:
    supervisor = object.__new__(Supervisor)
    supervisor._admin_context = ctx
    supervisor._clock = FakeClock(NOW)
    supervisor.fenced_token = 1

    @contextmanager
    def factory() -> Iterator[_Uow]:
        yield uow

    supervisor._uow_factory = factory  # type: ignore[assignment]
    return supervisor


def _context() -> AdminContext:
    return cast(
        AdminContext,
        SimpleNamespace(
            clock=FakeClock(NOW),
            proxy_config_path=None,
            providers={},
        ),
    )


def test_successful_listing_disables_hermes_and_codex_routes_and_records_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uow, ctx = _Uow(), _context()
    document = _document()
    policy = SimpleNamespace(name="default-software", version=5, document={})
    route = SimpleNamespace(name="route", version=4, document=document)
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(routing, "active_documents", lambda _uow: (policy, route))

    def publish(*args: Any, **kwargs: Any) -> tuple[int, int]:
        published.append(deepcopy(kwargs))
        return 6, 5

    monkeypatch.setattr(routing, "publish_routing", publish)

    _supervisor(uow, ctx)._disable_unoffered_gateway_models(["model-b"])

    saved = published[0]["routing_document"]
    assert [(row["id"], row["enabled"]) for row in saved["models"]] == [
        ("model-a", False),
        ("codex-local:model-a", False),
    ]
    assert {row["disabled_reason"] for row in saved["models"]} == {gateway.NOT_OFFERED}
    assert "model-a" in published[0]["reason"]
    assert NOW.isoformat() in published[0]["reason"]
    event = uow.events.rows[-1]
    assert event.kind == EventKind.LOCAL_GATEWAY_UPDATED.value
    assert event.payload["models"] == ["model-a"]
    assert event.payload["checked_at"] == NOW.isoformat()
    assert uow.commits == 1


@pytest.mark.parametrize("failure", [gateway.GatewayError("failed"), TimeoutError()])
def test_listing_error_or_timeout_does_not_publish(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    ctx, uow = _context(), _Uow()
    supervisor = _supervisor(uow, ctx)
    supervisor._gateway_endpoint = Mock(return_value=LOCAL_URL)  # type: ignore[method-assign]
    changed = Mock()
    supervisor._disable_unoffered_gateway_models = changed  # type: ignore[method-assign]

    async def db(call: Any) -> Any:
        return call()

    supervisor._db = db  # type: ignore[method-assign]
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args: "key-a")
    monkeypatch.setattr(gateway, "fetch_models", Mock(side_effect=failure))

    asyncio.run(supervisor._check_gateway_models())
    changed.assert_not_called()
    assert uow.events.rows == [] and uow.commits == 0


def test_returning_model_never_enables_a_disabled_route(monkeypatch: pytest.MonkeyPatch) -> None:
    uow, ctx = _Uow(), _context()
    route = SimpleNamespace(name="route", version=4, document=_document(enabled=False))
    monkeypatch.setattr(
        routing,
        "active_documents",
        lambda _uow: (SimpleNamespace(name="default-software", version=5), route),
    )
    publish = Mock()
    monkeypatch.setattr(routing, "publish_routing", publish)

    _supervisor(uow, ctx)._disable_unoffered_gateway_models(["model-a"])

    publish.assert_not_called()
    assert all(row["enabled"] is False for row in route.document["models"])
    assert uow.events.rows == [] and uow.commits == 0


def test_gateway_page_model_view_only_returns_models_in_successful_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _document()["models"]
    monkeypatch.setattr(gateway, "gateway_url", lambda _uow: (LOCAL_URL, "routing"))
    monkeypatch.setattr(gateway, "_local_entries", lambda _uow: (entries, {}))
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args: "key-a")
    monkeypatch.setattr(gateway, "fetch_models", lambda *_args: ["model-b"])

    view = asyncio.run(gateway.models_view(_context(), cast(Any, object())))

    assert [row["id"] for row in view["models"]] == ["model-b"]
    assert view["models"][0]["enabled"] is False
