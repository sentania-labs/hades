"""Harness administration (25): list, enable, disable."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crucible.application.admin import credentials
from crucible.application.admin.context import (
    AdminContext,
    guard_mutation,
)
from crucible.application.errors import NotFoundError
from crucible.application.harness_views import harness_list
from crucible.application.harnesses import set_harness_enabled
from crucible.ports.execution import ImageInfo
from crucible.ports.repository import UnitOfWork


async def list_images(ctx: AdminContext) -> list[tuple[str, ImageInfo]]:
    """Return the supervisor-owned snapshot; never contact a provider from a request."""
    if not getattr(ctx, "status_cache_enabled", False):
        return await refresh_images(ctx)
    return list(ctx.status_cache.images)


async def refresh_images(ctx: AdminContext) -> list[tuple[str, ImageInfo]]:
    """Refresh registry discovery. Only the supervisor calls this function."""
    out: list[tuple[str, ImageInfo]] = []
    for name, provider in ctx.providers.items():
        try:
            out.extend((name, image) for image in await provider.list_images())
        except Exception:  # a provider that cannot answer contributes nothing (25)
            continue
    return out


def _concurrency(uow: UnitOfWork) -> dict[str, int]:
    return dict(uow.attempts.concurrency_by_harness())


async def read_harnesses(
    ctx: AdminContext, uow: UnitOfWork, images: list[ImageInfo]
) -> tuple[list[dict[str, Any]], dict[str, credentials.SecretRead]]:
    """`list_harnesses` for an async handler: the harness Secrets are read first, once
    each, on worker threads with a bounded wait, so a slow API server never holds the
    event loop. The reads are returned too, for the rest of the same request."""
    secrets = await credentials.read_secrets(ctx, ctx.harnesses.names())
    return list_harnesses(ctx, uow, images, secrets=secrets), secrets


def list_harnesses(
    ctx: AdminContext,
    uow: UnitOfWork,
    images: list[ImageInfo],
    *,
    secrets: Mapping[str, credentials.SecretRead] | None = None,
) -> list[dict[str, Any]]:
    """25 status `harnesses[]`: the C5a view plus concurrency in use and the images
    known for each harness. `secrets` are the harness Secrets already read; without
    them each is read here, blocking, which only the CLI's local mode does."""
    views = harness_list(
        uow,
        ctx.harnesses,
        gates=ctx.harness_gates,
        sources=ctx.credential_sources,
        images=images,
    )
    in_use = _concurrency(uow)
    defaults = {d.harness: d for d in uow.harness_images.list_all()}
    out: list[dict[str, Any]] = []
    # On Kubernetes the credential is the harness Secret, which the directory-based view
    # cannot see; read the state the Credentials page reads, so no two pages disagree
    # about the same credential (crucible#123).
    secret_held = credentials.secret_store(ctx) is not None
    for view in views.items:
        entry = view.model_dump(mode="json")
        if secret_held and entry["credential"].get("state") != "not_required":
            held = credentials.state_view(ctx, uow, view.name, (secrets or {}).get(view.name))
            for key in ("state", "mount_mode", "source_fingerprint", "files", "detail", "source"):
                if key in held:
                    entry["credential"][key] = held[key]
        entry["concurrency_in_use"] = in_use.get(view.name, 0)
        default = defaults.get(view.name)
        entry["images"] = [
            {
                "reference": i.reference,
                "harness_version": i.version_of(view.name),
                "digest": i.digest,
                # Relative to this harness: promotion is per harness (ADR 0018).
                "promotion_state": (
                    "default"
                    if default is not None and default.digest == i.digest
                    else "retained"
                    if default is not None and default.previous_digest == i.digest
                    else "candidate"
                ),
            }
            for i in images
            if i.carries(view.name)
        ]
        out.append(entry)
    return out


def set_enabled(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    enabled: bool,
    reason: str | None,
) -> dict[str, Any]:
    """25: configuration retained; running attempts finish (nothing here touches them);
    new launches are refused with a wake by the registry (07).

    hades #174: this is the administrator's decision, and it replaces the configuration
    default for routing at once, with no restart. A harness the configuration keeps off
    (unverified) can be enabled; the configuration's reason comes back as `warning` and
    is recorded with the decision, and the harness test is how the operator proves it."""
    reason = guard_mutation(
        ctx,
        uow,
        reason,
        principal=principal,
        operation=f"harnesses {'enable' if enabled else 'disable'}",
    )
    if ctx.harnesses.get(harness) is None:
        raise NotFoundError(f"no adapter declares harness {harness!r}")
    gate = ctx.harness_gates.get(harness)
    warning = (
        (gate.reason or "off in configuration") if gate is not None and not gate.enabled else ""
    )
    # set_harness_enabled records the harness_enabled or harness_disabled event with
    # the principal, the reason and the before-and-after summary (C5a).
    state = set_harness_enabled(
        uow,
        ctx.clock,
        principal_name=principal,
        name=harness,
        enabled=enabled,
        reason=reason,
        warning=warning,
    )
    return {
        "harness": harness,
        "enabled": state.enabled,
        "decided_by_administrator": state.enabled_decided,
        "warning": warning,
        "reason": state.reason,
        "session_compatibility": state.session_compatibility,
        "running_attempts": _concurrency(uow).get(harness, 0),
    }
