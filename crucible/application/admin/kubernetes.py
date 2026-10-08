"""The Kubernetes provider's runtime settings, administered (25): the cluster egress
selectors and the short-role timeout, and the worker capacity the namespace quota
gives (hades #423).

`kubernetes.egress` names the cluster resolver's pods and, when the local model
endpoint runs inside the cluster, the gateway's pods and port (crucible#91). The
settings file seeds it; a save writes the database row every process reads back
within 15 seconds, so the supervisor follows an edit made here without a restart, and
the readiness canary runs again under the new values before any launch uses them.

`kubernetes.timeouts` is the same shape of setting: the seconds the short roles (the
bundle verifier, the cleaner, the Job that readies a claim for the publisher) may run
once their Pod is Running (the lab findings of 2026-09-29).
"""

from __future__ import annotations

from typing import Any

from crucible.application.admin.context import AdminContext, admin_event, guard_mutation
from crucible.application.errors import ContractValidationError
from crucible.application.runtime_settings import resolve
from crucible.domain import role_timeouts
from crucible.domain.cluster_egress import SETTING_NAME, ClusterEgress, parse_cluster_egress
from crucible.domain.entities import ProviderSetting
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork


def _reload(ctx: AdminContext) -> None:
    provider = ctx.providers.get("kubernetes")
    reload = getattr(provider, "reload_settings", None)
    if callable(reload):
        reload()


async def capacity_view(ctx: AdminContext) -> dict[str, Any]:
    """hades #423: how many worker Pods the Kubernetes provider admits at once and why:
    the namespace quota's headroom, the Pods kept for Hades's own short-role Pods (gate
    probe, collector, canary, login, preparer) and the worker capacity that leaves, or
    the configured fallback when the namespace has no quota. Read from the cluster now;
    the last reading when the API server does not answer."""
    provider = ctx.providers.get("kubernetes")
    reader = getattr(provider, "worker_capacity", None)
    if provider is None or not callable(reader):
        return {"provider_enabled": False}
    try:
        capacity = await reader()
    except Exception as exc:  # the page still renders; the reason is in the document
        return {"provider_enabled": True, "error": f"{type(exc).__name__}: {exc}"}
    return {"provider_enabled": True, **capacity.as_dict()}


def egress_view(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    """What is in force and where it came from. `source` is `database` once an
    administrator has saved it, and `settings` (the file's values) until then."""
    row = uow.provider_settings.get(SETTING_NAME)
    seed = ctx.kubernetes_egress_seed or ClusterEgress().as_document()
    provider = ctx.providers.get("kubernetes")
    return {
        "setting": SETTING_NAME,
        "source": "database" if row is not None else "settings",
        "document": row.document if row is not None else seed,
        "settings_file": seed,
        "updated_at": row.updated_at.isoformat() if row is not None else None,
        "updated_by": row.updated_by if row is not None else None,
        "reason": row.reason if row is not None else None,
        "provider_enabled": provider is not None,
        "protected_namespaces": list(ctx.kubernetes_protected_namespaces),
    }


def save_egress(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    document: dict[str, Any],
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation="kubernetes set-egress"
    )
    try:
        # A save replaces the whole setting, so both halves are stated. A missing one
        # would otherwise read as "no selector", and leaving `dns` out of an edit of
        # the endpoint would quietly take DNS away from every worker on a Cilium cluster.
        missing = [key for key in ("dns", "local_endpoint") if key not in document]
        if missing:
            raise ValueError(
                f"{' and '.join(missing)} must be given; an empty namespace is how a "
                "selector is turned off"
            )
        egress = parse_cluster_egress(
            document, protected_namespaces=ctx.kubernetes_protected_namespaces
        )
    except ValueError as exc:
        raise ContractValidationError(
            f"the {SETTING_NAME} setting is not valid: {exc}",
            errors=[{"path": "document", "message": str(exc)}],
        ) from exc
    before = egress_view(ctx, uow)
    uow.provider_settings.put(
        ProviderSetting(
            name=SETTING_NAME,
            document=egress.as_document(),
            updated_at=ctx.clock.now(),
            updated_by=principal,
            reason=reason,
        )
    )
    admin_event(
        uow,
        ctx,
        EventKind.KUBERNETES_EGRESS_UPDATED,
        principal=principal,
        reason=reason,
        before={"source": before["source"], **before["document"]},
        after={"source": "database", **egress.as_document()},
    )
    _reload(ctx)
    return egress_view(ctx, uow)


def timeouts_view(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    """The short-role timeout in force and where it came from, as `egress_view` says."""
    row = uow.provider_settings.get(role_timeouts.SETTING_NAME)
    retry = resolve(
        uow,
        name=role_timeouts.SETTING_NAME,
        field="api_retry_seconds",
        seed=None,
        seed_source="environment",
        default=role_timeouts.DEFAULT_API_RETRY_SECONDS,
        applies="next launch",
    )
    seed = {
        "role_timeout_seconds": ctx.kubernetes_role_timeout_seed,
        "api_retry_seconds": role_timeouts.DEFAULT_API_RETRY_SECONDS,
    }
    document = {**seed, **(row.document if row is not None else {})}
    return {
        "setting": role_timeouts.SETTING_NAME,
        "source": "database" if row is not None else "settings",
        "document": document,
        "settings_file": seed,
        "api_retry_seconds_source": retry.source,
        "api_retry_seconds_applies": retry.applies,
        "applies": "next launch",
        "bounds": {
            "min": role_timeouts.MIN_ROLE_TIMEOUT_SECONDS,
            "max": role_timeouts.MAX_ROLE_TIMEOUT_SECONDS,
        },
        "api_retry_bounds": {
            "min": role_timeouts.MIN_API_RETRY_SECONDS,
            "max": role_timeouts.MAX_API_RETRY_SECONDS,
        },
        "updated_at": row.updated_at.isoformat() if row is not None else None,
        "updated_by": row.updated_by if row is not None else None,
        "reason": row.reason if row is not None else None,
        "provider_enabled": ctx.providers.get("kubernetes") is not None,
    }


def save_timeouts(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    document: dict[str, Any],
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation="kubernetes set-timeouts"
    )
    try:
        current = timeouts_view(ctx, uow)["document"]
        checked = role_timeouts.parse_role_timeouts({**current, **document})
    except ValueError as exc:
        raise ContractValidationError(
            f"the {role_timeouts.SETTING_NAME} setting is not valid: {exc}",
            errors=[{"path": "role_timeout_seconds", "message": str(exc)}],
        ) from exc
    before = timeouts_view(ctx, uow)
    uow.provider_settings.put(
        ProviderSetting(
            name=role_timeouts.SETTING_NAME,
            document=checked,
            updated_at=ctx.clock.now(),
            updated_by=principal,
            reason=reason,
        )
    )
    admin_event(
        uow,
        ctx,
        EventKind.KUBERNETES_TIMEOUTS_UPDATED,
        principal=principal,
        reason=reason,
        before={"source": before["source"], **before["document"]},
        after={"source": "database", **checked},
    )
    _reload(ctx)
    return timeouts_view(ctx, uow)


__all__ = ["egress_view", "save_egress", "save_timeouts", "timeouts_view"]
