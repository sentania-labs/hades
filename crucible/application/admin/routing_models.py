"""Editable model switches and tier rules for the routing policy in force."""

from __future__ import annotations

import copy
from typing import Any

from crucible.application.admin.context import AdminContext, guard_mutation
from crucible.application.admin.routing import active_documents, publish_routing
from crucible.application.errors import ContractValidationError
from crucible.contracts.policy import RoutingPolicyV1
from crucible.domain.entities import Principal
from crucible.ports.repository import UnitOfWork

CAPABILITIES = ("small", "mid", "frontier")
HARNESS_NAMES = {
    "codex": "Codex",
    "claude_code": "Claude Code",
    "agy": "AGY",
    "hermes": "Hermes",
    "qwen_code": "Qwen Code",
}


def routing_controls_view(uow: UnitOfWork) -> dict[str, Any]:
    policy, record = active_documents(uow)
    routing = RoutingPolicyV1.model_validate(record.document)
    return {
        "policy": {"name": policy.name, "version": policy.version},
        "routing_policy": {"name": record.name, "version": record.version},
        "pinned": ((policy.document.get("routing") or {}).get("policy") or {}).get("pinned")
        is True,
        "models": [model.model_dump(mode="json") for model in routing.models],
        "pools": sorted(routing.pools),
        "tiers": {
            name: {
                "prefer_pools": routing.preferred_pools(name),
                "allowed_capability": list(rule.allowed_capability),
                "plain_words": tier_plain_words(routing, name),
            }
            for name, rule in sorted(routing.tiers.items())
        },
    }


def tier_plain_words(routing: RoutingPolicyV1, tier: str) -> str:
    """Describe the routing decision a task submitted now receives."""
    rule = routing.tiers[tier]
    ordered = routing.preferred_pools(tier)
    remaining = [name for name in routing.pools if name not in ordered]
    pools = [*ordered, *remaining]
    labels: list[str] = []
    for pool in pools:
        eligible = [
            model
            for model in routing.models
            if model.pool == pool and model.enabled and model.capability in rule.allowed_capability
        ]
        if not eligible:
            continue
        label = HARNESS_NAMES.get(
            eligible[0].harness, eligible[0].harness.replace("_", " ").title()
        )
        if label not in labels:
            labels.append(label)
    route = (
        f"{labels[0]} first" + "".join(f", then {label}" for label in labels[1:])
        if labels
        else "no enabled eligible model"
    )
    return f"{tier}: {route}; a busy first choice waits"


def save_model(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    model_id: str,
    enabled: bool,
    disabled_reason: str,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(ctx, uow, reason, principal=principal.name, operation="routing model")
    policy, record = active_documents(uow)
    document = copy.deepcopy(record.document)
    model = next((item for item in document.get("models", []) if item.get("id") == model_id), None)
    if model is None:
        raise ContractValidationError(
            "the routing model does not exist", errors=[{"path": "model", "message": model_id}]
        )
    if not enabled and not disabled_reason.strip():
        raise ContractValidationError(
            "a disabled model needs a reason",
            errors=[{"path": "disabled_reason", "message": "required when disabled"}],
        )
    model["enabled"] = enabled
    model["disabled_reason"] = None if enabled else disabled_reason.strip()
    publish_routing(
        ctx,
        uow,
        principal=principal,
        policy=policy,
        routing=record,
        routing_document=document,
        reason=reason,
        note="Model availability update",
    )
    return routing_controls_view(uow)


def save_tier(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    tier: str,
    prefer_pools: list[str],
    allowed_capability: list[str],
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(ctx, uow, reason, principal=principal.name, operation="routing tier")
    policy, record = active_documents(uow)
    document = copy.deepcopy(record.document)
    if tier not in document.get("tiers", {}):
        raise ContractValidationError(
            "the routing tier does not exist", errors=[{"path": "tier", "message": tier}]
        )
    if not allowed_capability:
        raise ContractValidationError(
            "select at least one capability",
            errors=[{"path": "allowed_capability", "message": "required"}],
        )
    document["tiers"][tier]["prefer_pools"] = prefer_pools
    document["tiers"][tier]["allowed_capability"] = allowed_capability
    document["tiers"][tier]["prefer"] = [
        cap for cap in document["tiers"][tier]["prefer"] if cap in allowed_capability
    ]
    if not document["tiers"][tier]["prefer"]:
        document["tiers"][tier]["prefer"] = list(allowed_capability)
    publish_routing(
        ctx,
        uow,
        principal=principal,
        policy=policy,
        routing=record,
        routing_document=document,
        reason=reason,
        note="Tier routing update",
    )
    return routing_controls_view(uow)
