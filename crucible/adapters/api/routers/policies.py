"""/policies, /routing (04, 05b). Upload is admin; a referenced version is immutable."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query

from crucible.adapters.api.deps import Admin, Ctx, Reader, UoW
from crucible.application.admin import credentials
from crucible.application.errors import NotFoundError
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
    RoutingPolicyView,
    RoutingUsageView,
)

router = APIRouter()


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
    """Without `policy_version` the newest version of the policy is used, so the report
    follows the routing policy new tasks are admitted against."""
    if policy_version is None:
        versions = list(uow.policies.list_versions(policy))
        stored = max(versions, key=lambda p: p.version) if versions else None
    else:
        stored = uow.policies.get(policy, policy_version)
    if stored is None:
        raise NotFoundError(f"policy {policy}/{policy_version or 'latest'} does not exist")
    routing = load_routing(uow, stored.document)
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
    principal: Admin,
    document: Annotated[dict[str, Any], Body()],
) -> RoutingPolicyView:
    routing = put_routing_policy(
        uow,
        ctx.clock,
        principal=principal,
        name=name,
        version=version,
        document=document,
    )
    uow.commit()
    return RoutingPolicyView(
        name=routing.name,
        version=routing.version,
        document=routing.document,
        created_at=routing.created_at,
        retired_at=routing.retired_at,
    )
