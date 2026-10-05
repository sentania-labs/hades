"""Runtime settings saved in ``provider_settings``.

Every runtime setting has one name, one seed, and one application boundary.  Callers
resolve the value here so a saved row always wins over deployment input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from crucible.domain.entities import ProviderSetting
from crucible.ports.repository import UnitOfWork

SettingSource = Literal["saved", "environment", "default"]
ApplicationBoundary = Literal["immediately", "next tick", "next launch"]


@dataclass(frozen=True, slots=True)
class RuntimeValue:
    name: str
    value: Any
    source: SettingSource
    applies: ApplicationBoundary
    reason: str = ""


def resolve(
    uow: UnitOfWork,
    *,
    name: str,
    field: str,
    seed: Any,
    seed_source: SettingSource,
    default: Any,
    applies: ApplicationBoundary,
) -> RuntimeValue:
    """Resolve one scalar setting. A malformed saved document cannot silently alter it."""
    repository = getattr(uow, "provider_settings", None)
    row = repository.get(name) if repository is not None else None
    if row is not None and isinstance(row.document, dict) and field in row.document:
        return RuntimeValue(name, row.document[field], "saved", applies, row.reason)
    return RuntimeValue(
        name,
        seed if seed is not None else default,
        seed_source if seed is not None else "default",
        applies,
    )


def save_scalar(
    uow: UnitOfWork,
    *,
    name: str,
    field: str,
    value: Any,
    principal: str,
    reason: str,
    now: Any,
) -> ProviderSetting:
    row = ProviderSetting(
        name=name,
        document={field: value},
        updated_at=now,
        updated_by=principal,
        reason=reason,
    )
    return uow.provider_settings.put(row)


__all__ = ["RuntimeValue", "resolve", "save_scalar"]
