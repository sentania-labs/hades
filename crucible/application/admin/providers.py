"""Provider health (25): name, capabilities, `ok`, `degraded` or `unavailable`."""

from __future__ import annotations

from typing import Any

from crucible.application.admin import status_cache
from crucible.application.admin.context import AdminContext


async def providers_status(ctx: AdminContext) -> list[dict[str, Any]]:
    """Return cached health; request paths never probe a provider."""
    if not getattr(ctx, "status_cache_enabled", False):
        return await refresh_providers_status(ctx)
    return [dict(item) for item in status_cache.read(ctx).providers]


async def refresh_providers_status(ctx: AdminContext) -> list[dict[str, Any]]:
    """Probe provider health for the supervisor-owned status snapshot."""
    out: list[dict[str, Any]] = []
    for name, provider in ctx.providers.items():
        try:
            health = await provider.health()
            state, checks = health.state, health.checks
        except Exception as exc:  # the status document never raises for one provider
            state, checks = "unavailable", {"error": type(exc).__name__}
        out.append(
            {
                "name": name,
                "capabilities": provider.capabilities().as_dict(),
                "health": state,
                "checks": checks,
            }
        )
    return out
