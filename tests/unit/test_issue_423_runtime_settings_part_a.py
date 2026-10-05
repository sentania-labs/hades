"""Issue 423 part A: saved credential modes and the shared runtime resolver."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.harness.registry import default_registry
from crucible.application.admin.context import AdminContext
from crucible.application.admin.credentials import mount_mode_value, set_mount_mode
from crucible.application.errors import ContractValidationError
from crucible.application.policies import validate_policy
from crucible.domain.entities import ProviderSetting
from crucible.domain.events import EventKind
from tests.fixtures import FakeClock
from tests.unit.test_policy_schema import seeded_policy


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
        clock=FakeClock(datetime(2026, 10, 5, tzinfo=UTC)),
        providers={},
        harnesses=default_registry(),
    )
    return ctx, uow


def _policy(cap: int) -> dict[str, Any]:
    document = seeded_policy()
    document["concurrency"]["per_harness"]["codex"] = cap
    return document


def test_saved_mode_changes_policy_validation_and_is_audited() -> None:
    ctx, uow = _context()
    saved = set_mount_mode(
        ctx, uow, principal="admin", harness="codex", mode="renewer", reason="parallel"
    )
    assert saved["mount_mode_source"] == "saved"
    assert saved["mount_mode_applies"] == "next launch"
    validate_policy(
        _policy(4),
        name="default-software",
        version=1,
        concurrency_modes={"codex": mount_mode_value(ctx, uow, "codex").value},
    )
    assert uow.events.items[-1].kind == EventKind.CREDENTIAL_MOUNT_MODE_SET.value

    set_mount_mode(
        ctx, uow, principal="admin", harness="codex", mode="rw-narrow", reason="compatibility"
    )
    with pytest.raises(ContractValidationError):
        validate_policy(
            _policy(4),
            name="default-software",
            version=1,
            concurrency_modes={"codex": mount_mode_value(ctx, uow, "codex").value},
        )


def test_harness_declaration_refuses_an_unsupported_mode() -> None:
    ctx, uow = _context()
    with pytest.raises(ContractValidationError, match="does not support renewer"):
        set_mount_mode(
            ctx,
            uow,
            principal="admin",
            harness="claude_code",
            mode="renewer",
            reason="try it",
        )
