"""Administrative routing state, including the editable local model endpoint."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
    record_refusal,
)
from crucible.application.errors import ContractValidationError, NotFoundError
from crucible.application.policies import put_policy, put_routing_policy
from crucible.application.proxy_config import (
    enabled_local_endpoints,
    install_worker_proxy_config,
    worker_proxy_config,
)
from crucible.application.routing import resolve_routing
from crucible.application.wakes import create_wake
from crucible.contracts.policy import RoutingModel, routing_model_name
from crucible.contracts.wake import WakeReason
from crucible.domain.endpoints import validate_endpoint
from crucible.domain.entities import Policy, Principal, ProviderSetting, Role
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork

# The saved local gateway (crucible#119): its URL, kept in `provider_settings` so it can be
# set before any local model entry exists to carry it. Once an entry does, the entries'
# URL is what workers use and what every view reports.
GATEWAY_SETTING = "local.gateway"


def list_exhaustions(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    now = ctx.clock.now()
    return {
        "items": [
            {
                "pool": mark.pool,
                "exhausted_at": mark.exhausted_at.isoformat(),
                "reset_at": mark.reset_at.isoformat(),
                "active": mark.cleared_at is None and mark.reset_at > now,
                "task_id": mark.task_id,
                "attempt_id": mark.attempt_id,
                "reason": mark.reason,
                "cleared_at": mark.cleared_at.isoformat() if mark.cleared_at else None,
                "cleared_by": mark.cleared_by,
                "clear_reason": mark.clear_reason,
            }
            for mark in uow.pool_exhaustions.list_all()
        ]
    }


def active_policy(uow: UnitOfWork) -> Policy:
    """The newest unretired default-software version: the one the admin panels edit."""
    policies = [p for p in uow.policies.list_versions("default-software") if p.retired_at is None]
    if not policies:
        raise NotFoundError("no default-software policy is in force")
    return max(policies, key=lambda item: item.version)


def _active_documents(uow: UnitOfWork) -> tuple[Any, Any]:
    """The delivery policy in force and the routing version it routes with now: the
    named one when pinned, else the newest one not retired (hades #605). Every edit
    builds on this version, so a page never publishes over an older one than tasks use."""
    policy = active_policy(uow)
    resolved = resolve_routing(uow, policy.document)
    routing = (
        uow.routing_policies.get(resolved.name, resolved.version) if resolved is not None else None
    )
    if routing is None or routing.retired_at is not None:
        raise NotFoundError("the routing policy named by default-software is not available")
    return policy, routing


def local_endpoint_view(uow: UnitOfWork) -> dict[str, Any]:
    policy, routing = _active_documents(uow)
    models = [
        copy.deepcopy(model)
        for model in routing.document.get("models", [])
        if model.get("endpoint") == "local"
    ]
    pool_name = str(models[0].get("pool", "")) if models else ""
    pool = copy.deepcopy((routing.document.get("pools") or {}).get(pool_name) or {})
    endpoints = {model.get("endpoint_url") for model in models if model.get("endpoint_url")}
    return {
        "policy": {"name": policy.name, "version": policy.version},
        "routing_policy": {"name": routing.name, "version": routing.version},
        "endpoint_url": next(iter(endpoints)) if len(endpoints) == 1 else None,
        "models": models,
        "pool": {"name": pool_name, **pool},
    }


def gateway_url(uow: UnitOfWork) -> tuple[str | None, str]:
    """The gateway URL in force and where it comes from: `routing` when the local model
    entries of the routing policy in force carry one URL, `mixed` (and no URL) when they
    carry several, `saved` when only the `local.gateway` setting has one (no entry exists
    yet, or none carries a URL), and `none` otherwise. The Hermes probe, the gateway page
    and the Status page all read this one answer."""
    try:
        view = local_endpoint_view(uow)
    except NotFoundError:
        view = None
    if view is not None and view.get("endpoint_url"):
        return str(view["endpoint_url"]), "routing"
    urls = {m.get("endpoint_url") for m in (view or {}).get("models", []) if m.get("endpoint_url")}
    if len(urls) > 1:
        # Entries that disagree (an uploaded policy can do that) name no one gateway;
        # testing the saved URL instead would pass for a URL no entry uses.
        return None, "mixed"
    row = uow.provider_settings.get(GATEWAY_SETTING)
    saved = (row.document or {}).get("endpoint_url") if row is not None else None
    if isinstance(saved, str) and saved:
        return saved, "saved"
    return None, "none"


def _models_enabled(document: Mapping[str, Any]) -> dict[str, bool]:
    return {
        f"{model.get('harness')}:{routing_model_name(model)}": model.get("enabled") is True
        for model in document.get("models") or []
    }


# hades #606: the parts of a routing entry whose change overrides an earlier decision.
OVERRIDE_FIELDS = ("enabled", "weight", "pool", "tiers")


def _entry_tiers(entry: Mapping[str, Any], tiers: Mapping[str, Any]) -> list[str]:
    """The tiers an entry belongs to: those whose allowed capabilities include its own."""
    capability = entry.get("capability")
    return sorted(
        name
        for name, rule in tiers.items()
        if capability is not None and capability in ((rule or {}).get("allowed_capability") or [])
    )


def _override_values(entry: Mapping[str, Any] | None, tiers: Mapping[str, Any]) -> dict[str, Any]:
    if entry is None:
        return dict.fromkeys(OVERRIDE_FIELDS)
    return {
        "enabled": entry.get("enabled") is True,
        "weight": entry.get("weight"),
        "pool": entry.get("pool"),
        "tiers": _entry_tiers(entry, tiers),
    }


def entry_overrides(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """hades #606: each routing entry (harness:model) whose enabled flag, weight, pool or
    tier membership differs between two versions, with each changed field's old and new
    value. An entry only in `after` is `added`, one only in `before` is `removed`."""

    def entries(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        return {
            f"{model.get('harness')}:{routing_model_name(model)}": model
            for model in document.get("models") or []
        }

    old, new = entries(before), entries(after)
    old_tiers, new_tiers = before.get("tiers") or {}, after.get("tiers") or {}
    found: list[dict[str, Any]] = []
    for key in sorted(set(old) | set(new)):
        was = _override_values(old.get(key), old_tiers)
        now = _override_values(new.get(key), new_tiers)
        change = "added" if key not in old else "removed" if key not in new else "changed"
        fields = [
            {"field": field, "before": was[field], "after": now[field]}
            for field in OVERRIDE_FIELDS
            if was[field] != now[field] and (change != "removed")
        ]
        if change == "removed" or fields:
            found.append({"entry": key, "change": change, "fields": fields})
    return found


def _override_value_words(field: str, value: Any) -> str:
    if value is None:
        return "unset"
    if field == "enabled":
        return "enabled" if value else "disabled"
    if field == "tiers":
        return ", ".join(value) if value else "no tier"
    return str(value)


def override_words(override: Mapping[str, Any]) -> str:
    """One overridden entry in plain words (hades #606)."""
    entry = override["entry"]
    if override["change"] == "removed":
        return f"removes {entry}"

    def label(field: str) -> str:
        # The enabled flag reads as its own words: "disabled to enabled".
        return "" if field == "enabled" else f"{field} "

    if override["change"] == "added":
        parts = [
            label(item["field"]) + _override_value_words(item["field"], item["after"])
            for item in override["fields"]
        ]
        return f"adds {entry} ({', '.join(parts)})"
    parts = [
        label(item["field"])
        + f"{_override_value_words(item['field'], item['before'])} to "
        + _override_value_words(item["field"], item["after"])
        for item in override["fields"]
    ]
    return f"{entry}: {', '.join(parts)}"


def upload_overrides(
    uow: UnitOfWork, *, name: str, version: int, document: Mapping[str, Any]
) -> tuple[int | None, list[dict[str, Any]]]:
    """hades #606: the version an upload of `name` version `version` is compared with
    (the highest stored version below it) and the entries it overrides there. None and
    every entry `added` when it is the first version."""
    earlier = [item for item in uow.routing_policies.list_versions(name) if item.version < version]
    previous = max(earlier, key=lambda item: item.version) if earlier else None
    return (
        previous.version if previous is not None else None,
        entry_overrides(previous.document if previous is not None else {}, document),
    )


def routing_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """What a routing version changes against the one it replaces (hades #437): the
    models it enables or disables (a new enabled entry is enabled, a removed enabled one
    disabled), each pool whose max_concurrency changes, and each tier whose pool order
    changes, and (hades #606) each entry whose enabled flag, weight, pool or tier
    membership changes. The projects that follow routing unpinned are added by the
    caller, which knows the policies."""
    old_models, new_models = _models_enabled(before), _models_enabled(after)
    enabled = sorted(i for i, on in new_models.items() if on and not old_models.get(i, False))
    disabled = sorted(i for i, on in old_models.items() if on and not new_models.get(i, False))
    old_pools = before.get("pools") or {}
    new_pools = after.get("pools") or {}
    caps = [
        {
            "pool": name,
            "before": (old_pools.get(name) or {}).get("max_concurrency"),
            "after": (new_pools.get(name) or {}).get("max_concurrency"),
        }
        for name in sorted(set(old_pools) | set(new_pools))
        if (old_pools.get(name) or {}).get("max_concurrency")
        != (new_pools.get(name) or {}).get("max_concurrency")
    ]
    old_tiers = before.get("tiers") or {}
    new_tiers = after.get("tiers") or {}
    orders = [
        {
            "tier": name,
            "before": (old_tiers.get(name) or {}).get("prefer_pools"),
            "after": (new_tiers.get(name) or {}).get("prefer_pools"),
        }
        for name in sorted(set(old_tiers) | set(new_tiers))
        if (old_tiers.get(name) or {}).get("prefer_pools")
        != (new_tiers.get(name) or {}).get("prefer_pools")
    ]
    return {
        "models_enabled": enabled,
        "models_disabled": disabled,
        "pool_caps": caps,
        "tier_pool_order": orders,
        "entries": entry_overrides(before, after),
    }


def delta_needs_reason(delta: Mapping[str, Any]) -> bool:
    """A version that enables or disables a model or changes a pool cap overrides an
    earlier decision, so it must say which one (hades #437)."""
    return bool(
        delta.get("models_enabled") or delta.get("models_disabled") or delta.get("pool_caps")
    )


def _order_words(pools: Any) -> str:
    return ", ".join(str(item) for item in pools) if pools else "no preference"


def delta_words(delta: Mapping[str, Any]) -> list[str]:
    """The delta in plain words, one line per kind of change, for the wake summary and
    the Routing and Local gateway pages."""
    lines: list[str] = []
    if delta.get("models_enabled"):
        lines.append("enables " + ", ".join(delta["models_enabled"]))
    if delta.get("models_disabled"):
        lines.append("disables " + ", ".join(delta["models_disabled"]))
    for cap in delta.get("pool_caps") or []:
        old = "unset" if cap["before"] is None else cap["before"]
        new = "unset" if cap["after"] is None else cap["after"]
        lines.append(f"pool {cap['pool']} max_concurrency {old} to {new}")
    for order in delta.get("tier_pool_order") or []:
        lines.append(
            f"tier {order['tier']} pool order {_order_words(order['before'])} "
            f"to {_order_words(order['after'])}"
        )
    overrides = delta.get("entries") or []
    if overrides:
        lines.append("overrides " + "; ".join(override_words(item) for item in overrides))
    if not lines:
        lines.append("no model, pool cap or tier order change")
    if "unpinned_projects" in delta:
        projects = delta.get("unpinned_projects") or []
        policies = delta.get("unpinned_policies") or []
        lines.append(
            "applies to projects following routing unpinned: "
            + (", ".join(projects) if projects else "none")
            + (f" (policies {', '.join(policies)})" if policies else "")
        )
    return lines


def _following_policies(uow: UnitOfWork, policy: Any, routing_name: str) -> list[Policy]:
    """The newest unretired version of every delivery policy that names `routing_name`
    without pinning it: each one follows a publish (crucible#91)."""
    followers: list[Policy] = []
    policy_names = {policy.name, *(repo.policy_name for repo in uow.repositories.list_all())}
    for policy_name in sorted(policy_names):
        policy_versions = [
            item for item in uow.policies.list_versions(policy_name) if item.retired_at is None
        ]
        if not policy_versions:
            continue
        current = max(policy_versions, key=lambda item: item.version)
        current_ref = (current.document.get("routing") or {}).get("policy") or {}
        if current_ref.get("name") != routing_name or current_ref.get("pinned") is True:
            continue
        followers.append(current)
    return followers


def publish_delta(
    uow: UnitOfWork, *, policy: Any, routing: Any, routing_document: Mapping[str, Any]
) -> dict[str, Any]:
    """The delta publishing `routing_document` over `routing` makes, with the delivery
    policies and the projects (repositories) that follow routing unpinned and so get it.
    The Local gateway page shows this before it publishes."""
    delta = routing_delta(routing.document, routing_document)
    followers = {item.name for item in _following_policies(uow, policy, routing.name)}
    delta["unpinned_policies"] = sorted(followers)
    delta["unpinned_projects"] = sorted(
        str(repo.name) for repo in uow.repositories.list_all() if repo.policy_name in followers
    )
    return delta


def routing_followers(uow: UnitOfWork) -> dict[str, list[str]]:
    """The delivery policies and projects that follow the routing policy in force
    unpinned, so every routing publish reaches them; empty when none is in force."""
    try:
        policy, routing = _active_documents(uow)
    except NotFoundError:
        return {"unpinned_policies": [], "unpinned_projects": []}
    delta = publish_delta(uow, policy=policy, routing=routing, routing_document=routing.document)
    return {
        "unpinned_policies": delta["unpinned_policies"],
        "unpinned_projects": delta["unpinned_projects"],
    }


def routing_history(uow: UnitOfWork, name: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """The newest `limit` versions of routing policy `name`, newest first, each with who
    published it, the reason, the note, and its delta against the version before it
    (hades #437). The delta is computed from the documents, so a version published
    before the delta was recorded still shows one; the projects that followed it come
    from the publish event, when it recorded them."""
    events: dict[int, Any] = {}
    after = 0
    while True:
        batch = uow.events.list_global(
            after_seq=after,
            kind=EventKind.ROUTING_POLICY_UPLOADED.value,
            since=None,
            limit=500,
        )
        for event in batch:
            ref = event.payload.get("routing_policy") or {}
            if ref.get("name") == name:
                events[int(ref.get("version", 0))] = event
        if len(batch) < 500:
            break
        after = int(batch[-1].seq or 0)
    versions = sorted(uow.routing_policies.list_versions(name), key=lambda item: item.version)
    rows: list[dict[str, Any]] = []
    previous: Any = None
    for record in versions:
        published = events.get(record.version)
        payload = published.payload if published is not None else {}
        delta = routing_delta(previous.document, record.document) if previous is not None else None
        recorded = payload.get("delta") or {}
        if delta is not None and "unpinned_projects" in recorded:
            delta["unpinned_projects"] = list(recorded.get("unpinned_projects") or [])
            delta["unpinned_policies"] = list(recorded.get("unpinned_policies") or [])
        rows.append(
            {
                "version": record.version,
                "created_at": record.created_at.isoformat(),
                "published_by": published.principal if published is not None else None,
                "reason": payload.get("reason") or None,
                "note": payload.get("note") or None,
                "retired": record.retired_at is not None,
                "delta": delta,
            }
        )
        previous = record
    return list(reversed(rows))[:limit]


def _orchestrator(uow: UnitOfWork) -> Principal | None:
    principals = sorted(
        (p for p in uow.principals.list_all() if p.disabled_at is None),
        key=lambda p: p.created_at,
    )
    return next(
        (p for p in principals if p.role is Role.ORCHESTRATOR),
        next((p for p in principals if p.role is Role.ADMIN), None),
    )


def publish_routing(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    policy: Any,
    routing: Any,
    routing_document: dict[str, Any],
    reason: str,
    note: str,
) -> tuple[int, int]:
    """Store `routing_document` as the next version of the routing policy in force and a
    next delivery policy version that names it, then bring the worker egress in line:
    the proxy allowlist, the Docker provider's allowlist, and the Kubernetes provider's
    settings. Returns the new (policy version, routing version).

    hades #437: a version that enables or disables a model or changes a pool cap is
    refused without a reason naming the decision it supersedes, and raises one
    `routing_changed` wake listing the delta and the projects that follow it."""
    delta = publish_delta(uow, policy=policy, routing=routing, routing_document=routing_document)
    material = delta_needs_reason(delta)
    if material and principal.role is Role.ORCHESTRATOR and not (reason or "").strip():
        changes = "; ".join(delta_words(delta)[:-1])
        record_refusal(
            ctx,
            principal=principal.name,
            operation="routing publish",
            detail=f"no reason was given for: {changes}",
        )
        raise ContractValidationError(
            f"this routing version {changes}; give a reason that names the decision it supersedes",
            errors=[{"path": "reason", "message": "must name the decision this supersedes"}],
        )
    routing_versions = uow.routing_policies.list_versions(routing.name)
    next_routing_version = max(item.version for item in routing_versions) + 1
    routing_document["version"] = next_routing_version
    prior = None
    if hasattr(uow, "events"):
        prior = next(
            (
                row
                for row in routing_history(uow, routing.name, limit=1000)
                if row["version"] == routing.version
            ),
            None,
        )
    put_routing_policy(
        uow,
        ctx.clock,
        principal=principal,
        name=routing.name,
        version=next_routing_version,
        document=routing_document,
        reason=reason,
        extra={
            "delta": delta,
            "note": note,
            "previous_version": routing.version,
            "superseded_decision": prior,
        },
    )
    ref = (policy.document.get("routing") or {}).get("policy") or {}
    pinned = ref.get("pinned") is True
    next_policy_version = policy.version
    for current in _following_policies(uow, policy, routing.name):
        policy_name = current.name
        new_policy_version = (
            max(item.version for item in uow.policies.list_versions(policy_name)) + 1
        )
        policy_document = copy.deepcopy(current.document)
        policy_document["version"] = new_policy_version
        policy_document["description"] = (
            f"{current.document.get('description', 'Software delivery policy')} "
            f"{note} in version {new_policy_version}."
        )
        policy_document["routing"] = {
            "policy": {
                "name": routing.name,
                "version": next_routing_version,
                "pinned": False,
            }
        }
        put_policy(
            uow,
            ctx.clock,
            principal=principal,
            name=policy_name,
            version=new_policy_version,
            document=policy_document,
            reason=reason,
        )
        if policy_name == policy.name:
            next_policy_version = new_policy_version
    if material:
        target = _orchestrator(uow)
        if target is not None:
            create_wake(
                uow,
                ctx.clock,
                principal_id=target.id,
                reason=WakeReason.ROUTING_CHANGED,
                summary=(
                    f"routing {routing.name} version {next_routing_version} published by "
                    f"{principal.name} ({note}): "
                    + "; ".join(delta_words(delta))
                    + f". Reason: {reason}"
                ),
                extra_links={"routing": "/ui/routing"},
                raised_by=principal.name,
            )
    egress_document = routing_document
    if pinned:
        referenced_routing = uow.routing_policies.get(
            str(ref.get("name", "")), int(ref.get("version", 0))
        )
        if referenced_routing is None:
            raise NotFoundError("the routing policy named by the pinned policy is not available")
        egress_document = referenced_routing.document
    _apply_egress(ctx, egress_document)
    return next_policy_version, next_routing_version


def _apply_egress(ctx: AdminContext, egress_document: Mapping[str, Any]) -> None:
    """Bring the worker egress in line with the routing version in force: the proxy
    allowlist, the Docker provider's allowlist, and the Kubernetes provider's settings."""
    if ctx.proxy_config_path:
        rendered = worker_proxy_config(ctx.proxy_subnet, list(ctx.proxy_hosts), [egress_document])
        install_worker_proxy_config(
            Path(ctx.proxy_config_path),
            rendered,
            reload_timeout_seconds=ctx.proxy_reload_timeout_seconds,
        )
    docker = ctx.providers.get("docker")
    if docker is not None and hasattr(docker, "config"):
        base_hosts = tuple(
            host for host in getattr(docker.config, "proxy_allowlist", ()) if ":" not in host
        )
        enabled_destinations = []
        for endpoint in enabled_local_endpoints([egress_document]):
            parsed = urlsplit(endpoint)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            enabled_destinations.append(f"{parsed.hostname}:{port}")
        docker.config = replace(
            docker.config,
            proxy_allowlist=tuple(dict.fromkeys([*base_hosts, *enabled_destinations])),
        )
    # The Kubernetes readiness canary proves a connection to the enabled local endpoint;
    # a new one is read back, and proved again before a launch uses it (crucible#91).
    kubernetes = ctx.providers.get("kubernetes")
    reload = getattr(kubernetes, "reload_settings", None)
    if callable(reload):
        reload()


def sync_upload_egress(ctx: AdminContext, uow: UnitOfWork, *, name: str, version: int) -> bool:
    """hades #605: an uploaded routing version that the policy in force now routes with
    (it follows routing unpinned and this is the newest version not retired) gets the
    egress a page publish sets. False, and nothing changes, when it is not in force."""
    try:
        _policy, routing = _active_documents(uow)
    except NotFoundError:
        return False
    if (routing.name, routing.version) != (name, version):
        return False
    _apply_egress(ctx, routing.document)
    return True


def routing_in_force(uow: UnitOfWork) -> tuple[str, int] | None:
    """The (name, version) of the routing version the policy in force routes with now,
    or None when there is none to route with."""
    try:
        _policy, routing = _active_documents(uow)
    except NotFoundError:
        return None
    return routing.name, routing.version


def sync_policy_egress(
    ctx: AdminContext, uow: UnitOfWork, *, before: tuple[str, int] | None
) -> bool:
    """A delivery policy publish that changes the routing version in force (its routing
    name, version or pinned flag) gets the egress a routing publish sets, for the
    version it now routes with. `before` is `routing_in_force` read before the publish.
    False, and nothing changes, when the routing version in force is the same."""
    try:
        _policy, routing = _active_documents(uow)
    except NotFoundError:
        return False
    if (routing.name, routing.version) == before:
        return False
    _apply_egress(ctx, routing.document)
    return True


def active_documents(uow: UnitOfWork) -> tuple[Any, Any]:
    """The delivery policy in force and the routing policy it names."""
    return _active_documents(uow)


def save_local_endpoint(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    endpoint_url: str,
    models: list[dict[str, Any]],
    max_concurrency: int,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal.name, operation="routing local-endpoint update"
    )
    validate_endpoint("local", endpoint_url)
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    policy, routing = _active_documents(uow)
    before = local_endpoint_view(uow)
    routing_document = copy.deepcopy(routing.document)
    updates = {
        f"{item.get('harness', '')}:{item.get('model') or item.get('id', '')}": item
        for item in models
    }
    local_models = [
        model for model in routing_document.get("models", []) if model.get("endpoint") == "local"
    ]
    if not local_models:
        raise NotFoundError("the active routing policy has no local model entries")
    local_keys = {f"{model.get('harness')}:{routing_model_name(model)}" for model in local_models}
    # Legacy callers supplied only the model id when it was globally unique.
    for item in models:
        if not item.get("harness"):
            matches = [
                key
                for key in local_keys
                if key.endswith(f":{item.get('model') or item.get('id', '')}")
            ]
            if len(matches) == 1:
                updates[matches[0]] = updates.pop(f":{item.get('model') or item.get('id', '')}")
    unknown = sorted(set(updates) - local_keys)
    if unknown:
        raise ValueError(f"models are not local entries in the active routing policy: {unknown}")
    for model in local_models:
        model["endpoint_url"] = endpoint_url
        update = updates.get(f"{model.get('harness')}:{routing_model_name(model)}")
        if update is None:
            continue
        # Only a real True turns a flag on; the API rejects non-booleans before this.
        enabled = update.get("enabled") is True
        model["enabled"] = enabled
        model["chat_template_kwargs"] = {"enable_thinking": update.get("enable_thinking") is True}
        if enabled:
            model["disabled_reason"] = None
        else:
            model["disabled_reason"] = "operator disabled from the local endpoint panel"
    pool_name = str(local_models[0]["pool"])
    routing_document["pools"][pool_name]["max_concurrency"] = max_concurrency
    next_policy_version, next_routing_version = publish_routing(
        ctx,
        uow,
        principal=principal,
        policy=policy,
        routing=routing,
        routing_document=routing_document,
        reason=reason,
        note="Local endpoint update",
    )
    # The saved gateway URL follows, so it cannot come back stale if the entries go.
    uow.provider_settings.put(
        ProviderSetting(
            name=GATEWAY_SETTING,
            document={"endpoint_url": endpoint_url},
            updated_at=ctx.clock.now(),
            updated_by=principal.name,
            reason=reason,
        )
    )
    after = {
        "policy": {"name": policy.name, "version": next_policy_version},
        "routing_policy": {"name": routing.name, "version": next_routing_version},
        "endpoint_url": endpoint_url,
        "models": [
            RoutingModel.model_validate(model).model_dump(mode="json") for model in local_models
        ],
        "pool": {"name": pool_name, **copy.deepcopy(routing_document["pools"][pool_name])},
    }
    admin_event(
        uow,
        ctx,
        EventKind.LOCAL_ENDPOINT_UPDATED,
        principal=principal.name,
        reason=reason,
        before={
            "routing_version": before["routing_policy"]["version"],
            "endpoint_url": before["endpoint_url"],
        },
        after={
            "routing_version": next_routing_version,
            "endpoint_url": endpoint_url,
            "max_concurrency": max_concurrency,
        },
    )
    return after


def clear_exhaustion(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    pool: str,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation="routing clear-exhaustion"
    )
    before = uow.pool_exhaustions.get(pool, for_update=True)
    if before is None:
        raise NotFoundError(f"quota pool {pool!r} has no exhaustion mark")
    cleared = uow.pool_exhaustions.clear(
        pool, at=ctx.clock.now(), principal=principal, reason=reason
    )
    assert cleared is not None
    admin_event(
        uow,
        ctx,
        EventKind.POOL_EXHAUSTION_CLEARED,
        principal=principal,
        reason=reason,
        before={"pool": pool, "reset_at": before.reset_at.isoformat(), "active": True},
        after={"pool": pool, "active": False},
    )
    return {"pool": pool, "active": False, "cleared_at": cleared.cleared_at}
