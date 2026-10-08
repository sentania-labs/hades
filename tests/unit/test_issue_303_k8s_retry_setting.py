"""Issue 303: the Kubernetes launch transport retry budget is a runtime setting."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from crucible.adapters.harness.registry import default_registry
from crucible.adapters.ui.pages.settings import _runtime_rows
from crucible.application.admin.context import AdminContext
from crucible.application.admin.kubernetes import save_timeouts, timeouts_view
from crucible.application.errors import ContractValidationError
from crucible.cli import admin
from crucible.client.next import kubernetes_timeouts_actions
from crucible.domain import role_timeouts
from crucible.domain.entities import ProviderSetting, Role
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


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("retry", [None, 17])
def test_cli_forwards_retry_budget_and_preserves_omitted_value(
    remote: bool, retry: int | None
) -> None:

    argv = ["kubernetes", "set-timeouts", "--role-seconds", "120", "--reason", "fit outage window"]
    if retry is not None:
        argv += ["--api-retry-seconds", str(retry)]
    args = admin.build_parser(argparse.ArgumentParser()).parse_args(argv)
    expected = {"role_timeout_seconds": 120}
    if retry is not None:
        expected["api_retry_seconds"] = retry
    if remote:
        api = Mock()
        admin._remote(args, api)
        api.call.assert_called_once_with(
            "POST", "/v1/admin/kubernetes/timeouts", {"reason": "fit outage window", **expected}
        )
    else:
        ctx, uow = _context()
        save_timeouts(
            ctx, uow, principal="admin", document={"api_retry_seconds": 33}, reason="seed"
        )
        uow.commit = Mock()
        wiring: Any = SimpleNamespace(
            admin=ctx, ctx=SimpleNamespace(uow_factory=lambda: nullcontext(uow))
        )
        result = admin._local(args, wiring)
        assert result["document"] == {"role_timeout_seconds": 120, "api_retry_seconds": retry or 33}
        uow.commit.assert_called_once_with()


def test_generated_timeout_action_includes_current_retry_budget() -> None:

    actions = kubernetes_timeouts_actions(
        {"document": {"role_timeout_seconds": 120, "api_retry_seconds": 17}}, ["crucible-admin"]
    )
    args = admin.build_parser(argparse.ArgumentParser()).parse_args(actions[0]["command"][1:])
    assert args.api_retry_seconds == 17


def test_settings_page_lists_retry_value_source_and_next_launch() -> None:
    ctx, uow = _context()
    page_ctx: Any = SimpleNamespace(admin=ctx, settings=None)
    principal: Any = SimpleNamespace(role=Role.ADMIN)
    for saved in (False, True):
        if saved:
            save_timeouts(
                ctx, uow, principal="admin", document={"api_retry_seconds": 17}, reason="fit"
            )
        rows = _runtime_rows(page_ctx, uow, principal)
        row = next(row for row in rows if row[0] == "kubernetes.timeouts.api_retry_seconds")
        assert row[1:4] == [17 if saved else 60, "saved" if saved else "default", "next launch"]
