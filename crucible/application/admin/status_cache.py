"""Supervisor snapshots shared with API processes; local admin commands read live."""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

from crucible.application.admin.context import (
    AdminContext,
    ProviderStatusCache,
    admin_event,
    guard_mutation,
)
from crucible.application.errors import ContractValidationError
from crucible.application.runtime_settings import RuntimeValue, resolve, save_scalar
from crucible.domain.entities import Principal, ProviderSetting
from crucible.domain.events import EventKind
from crucible.ports.execution import ImageInfo
from crucible.ports.repository import UnitOfWork

SNAPSHOT = "admin.status_snapshot"
TTL = "admin.status_cache"


def ttl_value(ctx: AdminContext, uow: UnitOfWork) -> RuntimeValue:
    return resolve(
        uow,
        name=TTL,
        field="ttl_seconds",
        seed=ctx.status_cache_ttl_seconds,
        seed_source="environment",
        default=60.0,
        applies="next tick",
    )


def read(ctx: AdminContext) -> ProviderStatusCache:
    if not getattr(ctx, "status_cache_shared", False):
        return ctx.status_cache
    with ctx.uow_factory() as uow:
        row = uow.provider_settings.get(SNAPSHOT)
    if row is None:
        return ProviderStatusCache()
    return ProviderStatusCache(
        images=[(item["provider"], ImageInfo(**item["image"])) for item in row.document["images"]],
        providers=row.document["providers"],
    )


def write(ctx: AdminContext, uow: UnitOfWork, images: list[Any], providers: list[Any]) -> None:
    uow.provider_settings.put(
        ProviderSetting(
            name=SNAPSHOT,
            document={
                "images": [{"provider": name, "image": asdict(image)} for name, image in images],
                "providers": providers,
            },
            updated_at=ctx.clock.now(),
            updated_by="supervisor",
            reason="Status snapshot refresh",
        )
    )


def save_ttl(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    seconds: float,
    reason: str | None,
) -> None:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal.name, operation="admin status cache set"
    )
    if not math.isfinite(seconds) or seconds <= 0:
        raise ContractValidationError("status cache TTL must be a positive finite number")
    before = {"ttl_seconds": ttl_value(ctx, uow).value}
    save_scalar(
        uow,
        name=TTL,
        field="ttl_seconds",
        value=seconds,
        principal=principal.name,
        reason=reason,
        now=ctx.clock.now(),
    )
    admin_event(
        uow,
        ctx,
        EventKind.STATUS_CACHE_UPDATED,
        principal=principal.name,
        reason=reason,
        before=before,
        after={"ttl_seconds": seconds},
    )
