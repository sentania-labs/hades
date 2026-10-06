"""Issue 303: the Kubernetes launch transport retry budget is a runtime setting."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.harness.registry import default_registry
from crucible.application.admin.context import AdminContext
from crucible.application.admin.kubernetes import save_timeouts, timeouts_view
from crucible.application.errors import ContractValidationError
from crucible.domain import role_timeouts
from crucible.domain.entities import ProviderSetting
from tests.fixtures import FakeClock
from tests.unit.kubernetes_fixtures import build


class _Settings:
    def __init__(self) -> None:
        self.rows: dict[str, ProviderSetting] = {}

    def get(self, name: str) -> ProviderSetting | None:
        return self.rows.get(name)

    def put(self, row: ProviderSetting) -> ProviderSetting:
        self.rows[row.name] = row
        return row


class _Events:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def append(self, event: Any) -> Any:
        self.items.append(event)
        return event


def _context() -> tuple[AdminContext, Any]:
    uow = SimpleNamespace(provider_settings=_Settings(), events=_Events())
    ctx = AdminContext(
        uow_factory=lambda: uow,
        clock=FakeClock(datetime(2026, 10, 6, tzinfo=UTC)),
        providers={},
        harnesses=default_registry(test_fixtures=True),
    )
    return ctx, uow


@pytest.mark.asyncio
async def test_saved_retry_budget_reaches_provider_on_the_next_launch_refresh() -> None:
    ctx, uow = _context()
    _api, _registry, provider = build()
    provider._settings_source = lambda: (None, None)
    provider._timeouts_source = lambda: (
        uow.provider_settings.get(role_timeouts.SETTING_NAME).document
    )
    ctx.providers["kubernetes"] = provider

    initial = timeouts_view(ctx, uow)
    assert initial["document"]["api_retry_seconds"] == 60
    assert initial["source"] == "settings"
    assert initial["applies"] == "next launch"

    saved = save_timeouts(
        ctx,
        uow,
        principal="admin",
        document={"role_timeout_seconds": 120, "api_retry_seconds": 17},
        reason="fit the API server outage window",
    )
    assert saved["source"] == "database"
    assert saved["reason"] == "fit the API server outage window"

    await provider._refresh_settings()
    assert provider.config.api_retry_seconds == 17


@pytest.mark.parametrize("value", [-1, 1_000_000, "60", None, True])
def test_retry_budget_refuses_values_outside_its_bounded_integer_contract(value: Any) -> None:
    ctx, uow = _context()
    with pytest.raises(ContractValidationError, match="api_retry_seconds"):
        save_timeouts(
            ctx,
            uow,
            principal="admin",
            document={"role_timeout_seconds": 120, "api_retry_seconds": value},
            reason="try a bad budget",
        )
    assert uow.provider_settings.rows == {}
