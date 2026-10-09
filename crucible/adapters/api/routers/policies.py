"""/policies, /routing (04, 05b). Upload is admin; a referenced version is immutable."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import Body, Query

from crucible.adapters.api.deps import Admin, Ctx, Mutator, Reader, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.admin import credentials, gateway
from crucible.application.admin.routing import (
    gateway_url,
    routing_in_force,
    sync_policy_egress,
    sync_upload_egress,
    upload_overrides,
)
from crucible.application.errors import ContractValidationError, NotFoundError
from crucible.application.policies import (
    get_policy,
    get_routing_policy,
    put_policy,
    put_routing_policy,
)
from crucible.application.routing import history, load_routing, usage_report
from crucible.contracts.api import (
    PolicyView,
    RoutingHistoryView,
    RoutingOverride,
    RoutingPolicyView,
    RoutingUsageView,
)
from crucible.domain.entities import Role

router = ThreadedAPIRouter()


@router.get("/policies/{name}/{version}", response_model=PolicyView)
def read_policy(name: str, version: int, uow: UoW, _principal: Reader) -> PolicyView:
    policy = get_policy(uow, name=name, version=version)
    return PolicyView(
        name=policy.name,
        version=policy.version,
        document=policy.document,
        referenced=uow.policies.is_referenced(name, version),
        created_at=policy.created_at,
        retired_at=policy.retired_at,
    )


@router.put("/policies/{name}/{version}", response_model=PolicyView)
def upload_policy(
    name: str,
    version: int,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    document: Annotated[dict[str, Any], Body()],
) -> PolicyView:
    before = routing_in_force(uow)
    policy = put_policy(
        uow,
        ctx.clock,
        principal=principal,
        name=name,
        version=version,
        document=document,
        concurrency_modes={
            name: credentials.mount_mode_value(ctx.admin, uow, name).value
            for name in ctx.admin.harnesses.names()
            if (adapter := ctx.admin.harnesses.get(name)) is not None
            and adapter.credential_spec() is not None
        }
        if ctx.admin is not None
        else {},
    )
    # A publish that changes the routing version in force sets the egress for it.
    if ctx.admin is not None:
        sync_policy_egress(ctx.admin, uow, before=before)
    uow.commit()
    return PolicyView(
        name=policy.name,
        version=policy.version,
        document=policy.document,
        referenced=False,
        created_at=policy.created_at,
        retired_at=policy.retired_at,
    )


@router.get("/routing/usage", response_model=RoutingUsageView)
def routing_usage(
    ctx: Ctx,
    uow: UoW,
    _principal: Reader,
    policy: str = "default-software",
    policy_version: int | None = None,
) -> RoutingUsageView:
    """Without `policy_version` the newest version of the policy is used, read through
    the routing version it routes new tasks with now (unpinned follows the newest one
    not retired, hades #605). An explicit `policy_version` reports the routing version
    that policy version names, as written."""
    if policy_version is None:
        versions = list(uow.policies.list_versions(policy))
        stored = max(versions, key=lambda p: p.version) if versions else None
    else:
        stored = uow.policies.get(policy, policy_version)
    if stored is None:
        raise NotFoundError(f"policy {policy}/{policy_version or 'latest'} does not exist")
    routing = load_routing(uow, stored.document, in_force=policy_version is None)
    if routing is None:
        raise NotFoundError("the policy names a routing policy that is not uploaded")
    return RoutingUsageView(
        routing_policy={"name": routing.name, "version": routing.version},
        pools=usage_report(uow, routing, ctx.clock.now()),
    )


@router.get("/routing/history", response_model=RoutingHistoryView)
def routing_history(
    uow: UoW,
    _principal: Reader,
    model: str | None = None,
    project: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
) -> RoutingHistoryView:
    rows = history(uow, model=model, project=project, since=since)
    return RoutingHistoryView(items=rows[: limit or 200])


@router.get("/routing/{name}/{version}", response_model=RoutingPolicyView)
def read_routing(name: str, version: int, uow: UoW, _principal: Reader) -> RoutingPolicyView:
    routing = get_routing_policy(uow, name=name, version=version)
    return RoutingPolicyView(
        name=routing.name,
        version=routing.version,
        document=routing.document,
        created_at=routing.created_at,
        retired_at=routing.retired_at,
    )


@router.put("/routing/{name}/{version}", response_model=RoutingPolicyView)
def upload_routing(
    name: str,
    version: int,
    ctx: Ctx,
    uow: UoW,
    principal: Mutator,
    document: Annotated[dict[str, Any], Body()],
    reason: Annotated[str | None, Query()] = None,
) -> RoutingPolicyView:
    if principal.role is Role.ORCHESTRATOR and not (reason or "").strip():
        raise ContractValidationError(
            "an orchestrator routing publish requires a reason",
            errors=[{"path": "reason", "message": "must not be empty"}],
        )
    listing: list[str] | None = None
    if any(item.get("endpoint") == "local" for item in document.get("models", [])):
        endpoint, _source = gateway_url(uow)
        admin = ctx.admin
        bearer = credentials.read_api_key(admin) if admin is not None else None
        if endpoint is None or bearer is None:
            raise ContractValidationError(
                "local routing publish requires the gateway listing for its key",
                errors=[{"path": "models", "message": "the gateway could not be listed"}],
            )
        listing = gateway.fetch_models(endpoint, bearer)
    # hades #606: the publish names the entries it overrides against the version below
    # it, and the event records them with the reason for the Routing page's history.
    previous_version, overrides = upload_overrides(
        uow, name=name, version=version, document=document
    )
    routing = put_routing_policy(
        uow,
        ctx.clock,
        principal=principal,
        name=name,
        version=version,
        document=document,
        reason=reason,
        extra={"previous_version": previous_version, "overrides": overrides},
        local_model_listing=listing,
    )
    # hades #605: an unpinned policy routes with this version from now on when it is the
    # newest one not retired, so its egress is set as a page publish sets it.
    if ctx.admin is not None:
        sync_upload_egress(ctx.admin, uow, name=name, version=version)
    uow.commit()
    return RoutingPolicyView(
        name=routing.name,
        version=routing.version,
        document=routing.document,
        created_at=routing.created_at,
        retired_at=routing.retired_at,
        previous_version=previous_version,
        overrides=[RoutingOverride.model_validate(item) for item in overrides],
        reason=reason,
    )
