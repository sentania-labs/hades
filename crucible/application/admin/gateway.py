"""The local gateway (crucible#119, #121): one place for the URL and the Hermes key, a
test of both, and the models the key can see, picked rather than typed.

The URL is the local model entries' `endpoint_url` once one exists, and the
`local.gateway` setting until then, so it can be set before there is any entry to carry
it. Saving the URL writes both. The key is the Hermes credential (ADR 0015 on
Kubernetes, a mode 0600 file with the Docker provider); it is never returned, logged or
audited. The test is the Hermes probe: the gateway's readiness check, then `/models`
with the key, reported in plain words that name the URL.

Picking models writes the local model entries of a new routing policy version, the same
way the Routing page's policy edits do. A model the gateway does not offer cannot be
enabled; an entry for a model the gateway no longer offers is disabled with that reason
rather than removed, so a task that names it still resolves to a stated refusal.
"""

from __future__ import annotations

import asyncio
import copy
import urllib.error
from typing import Any

from crucible.application.admin import credentials
from crucible.application.admin.context import AdminContext, admin_event, guard_mutation
from crucible.application.admin.routing import (
    GATEWAY_SETTING,
    active_documents,
    gateway_url,
    local_endpoint_view,
    publish_delta,
    publish_routing,
)
from crucible.application.errors import ConflictError, ContractValidationError, NotFoundError
from crucible.contracts.policy import parse_routing_policy
from crucible.domain.endpoints import validate_endpoint
from crucible.domain.entities import Principal, ProviderSetting
from crucible.domain.events import EventKind
from crucible.domain.harness_settings import (
    CONTEXT_LENGTH_RANGE,
    MAX_OUTPUT_TOKENS_RANGE,
    MAX_TURNS_RANGE,
    HermesRunLimits,
    hermes_limit_problems,
    hermes_run_limits,
    setting_name,
)
from crucible.ports.repository import UnitOfWork

HERMES = credentials.HERMES
LIST_TIMEOUT_SECONDS = 10.0
CAPABILITIES = ("small", "mid", "frontier")
DEFAULT_POOL = "lab-local"
# The pool a first local model lands in when the routing policy in force has none: the
# same shape 0017_lab_local seeds.
DEFAULT_POOL_DOCUMENT: dict[str, Any] = {
    "window": "1h",
    "budget_units": "attempts",
    "soft_limit": 0,
    "default_cooldown_seconds": 3600,
    "max_concurrency": 4,
}
SEED_REASON_MARK = "endpoint environment seed is not configured"
NOT_PICKED = "not picked on the Local gateway page yet"
NOT_OFFERED = "the gateway no longer offers this model"

# What a recorded Hermes probe outcome means, in the words the gateway page and the
# Status page both use (crucible#119, #123).
CAUSES = {
    "endpoint_not_configured": "the gateway URL is not set",
    "key_not_set": "no Hermes key is stored",
    "endpoint_unreachable": "the gateway could not be reached",
    "provider_unavailable": "the probe could not run",
}


class GatewayError(ConflictError):
    slug = "local-gateway"
    title = "Local gateway refused"


def plain_outcome(outcome: str | None) -> str:
    """A recorded launch or probe outcome as a sentence fragment an operator reads."""
    if not outcome:
        return "never tested"
    if outcome == "probe:completed":
        return "the last test passed"
    if outcome == "probe:auth_failure":
        return "the last test was refused: the key was not accepted"
    if outcome.startswith("probe:inconclusive:"):
        cause = outcome.split(":", 2)[2]
        if cause.startswith("readiness_http_"):
            words = f"the gateway's readiness check answered HTTP {cause.rsplit('_', 1)[-1]}"
        elif cause.startswith("models_http_"):
            words = f"listing the gateway's models answered HTTP {cause.rsplit('_', 1)[-1]}"
        else:
            words = CAUSES.get(cause, cause.replace("_", " "))
        return f"the last test was inconclusive: {words}"
    if outcome.startswith("probe:"):
        return f"the last test ended {outcome.split(':', 1)[1].replace('_', ' ')}"
    return f"the last launch ended {outcome.replace('_', ' ')}"


def fetch_models(endpoint: str, bearer: str, *, timeout: float = LIST_TIMEOUT_SECONDS) -> list[str]:
    """The model ids the gateway lists for this key. Blocking. Refusals are plain words
    that name the URL; the key is never part of one."""
    try:
        status, body = credentials._http_get(
            endpoint.rstrip("/") + "/models", bearer=bearer, timeout=timeout
        )
    except (OSError, urllib.error.URLError, UnicodeError) as exc:
        raise GatewayError(
            f"Gateway {endpoint} could not be reached ({type(exc).__name__})."
        ) from None
    if status == 401:
        raise GatewayError(f"Gateway {endpoint} refused the key (HTTP 401).")
    if status != 200:
        raise GatewayError(f"Gateway {endpoint} answered HTTP {status} when asked for its models.")
    return credentials.model_ids(body)


def _local_entries(uow: UnitOfWork) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        view = local_endpoint_view(uow)
    except NotFoundError:
        return [], {}
    return list(view["models"]), dict(view["pool"])


def gateway_view(
    ctx: AdminContext, uow: UnitOfWork, secret: credentials.SecretRead | None = None
) -> dict[str, Any]:
    """The URL in force and its source, whether a key is set, the credential state, the
    last test in plain words, and the local model entries in force. Never the key.
    `secret` is the Hermes Secret an async handler already read off the event loop."""
    endpoint, source = gateway_url(uow)
    entries, pool = _local_entries(uow)
    credential: dict[str, Any] = {}
    if HERMES in ctx.harnesses.names():
        credential = credentials.state_view(ctx, uow, HERMES, secret)
    outcome = credential.get("last_launch_outcome")
    return {
        "endpoint_url": endpoint,
        "url_source": source,
        "key_set": bool(credential.get("key_set")),
        "credential_state": credential.get("state"),
        "last_tested_at": credential.get("last_launch_at"),
        "last_test": plain_outcome(outcome),
        "last_outcome": outcome,
        "models": [
            {
                "id": entry.get("id"),
                "harness": entry.get("harness"),
                "model_name": entry.get("model_name") or entry.get("id"),
                "enabled": entry.get("enabled") is True,
                "enable_thinking": (entry.get("chat_template_kwargs") or {}).get(
                    "enable_thinking", False
                )
                is True,
                "capability": entry.get("capability"),
                "disabled_reason": entry.get("disabled_reason"),
            }
            for entry in entries
        ],
        "pool": pool,
    }


async def models_view(ctx: AdminContext, uow: UnitOfWork, *, fetch: bool = True) -> dict[str, Any]:
    """What the gateway offers this key, beside the local entries in force: one row per
    model, offered or not, with what saving would do to it.

    When *fetch* is False the gateway is not contacted and a not-asked note is returned
    instead.  This keeps the gateway page fast until the operator clicks the link.
    """
    endpoint, source = gateway_url(uow)
    entries, pool = _local_entries(uow)
    offered: list[str] | None = None
    error: str | None = None
    if fetch:
        if endpoint is None:
            error = "The gateway URL is not set. Set it and the key first."
        else:
            bearer = await asyncio.to_thread(credentials.read_api_key, ctx, HERMES)
            if bearer is None:
                error = f"No Hermes key is stored, so gateway {endpoint} was not asked."
            else:
                try:
                    offered = await asyncio.to_thread(fetch_models, endpoint, bearer)
                except GatewayError as exc:
                    error = exc.detail
    else:
        error = "The gateway's models were not asked for; use the link to list them."
    codex = {
        str(entry.get("model_name") or entry["id"]): entry
        for entry in entries
        if entry.get("harness") == "codex"
    }
    by_id = {str(entry.get("id")): entry for entry in entries if entry.get("harness") != "codex"}
    for model_id in codex:
        by_id.setdefault(model_id, {})
    rows: list[dict[str, Any]] = []
    for model_id in offered or []:
        entry = by_id.get(model_id)
        display_entry = entry or codex.get(model_id)
        is_offered = None if offered is None else model_id in offered
        rows.append(
            {
                "id": model_id,
                "offered": is_offered,
                "in_policy": entry is not None,
                "enabled": bool(entry and entry.get("enabled") is True),
                "codex_enabled": bool(codex.get(model_id, {}).get("enabled")),
                "enable_thinking": bool(
                    entry and (entry.get("chat_template_kwargs") or {}).get("enable_thinking")
                ),
                "capability": (display_entry or {}).get("capability") or "mid",
                "note": _row_note(display_entry, is_offered),
            }
        )
    return {
        "endpoint_url": endpoint,
        "url_source": source,
        "reachable": offered is not None,
        "error": error,
        "offered_count": len(offered) if offered is not None else None,
        "models": rows,
        "pool": pool,
    }


def _row_note(entry: dict[str, Any] | None, offered: bool | None) -> str:
    if offered is False:
        return (
            "not offered by the gateway; saving disables it"
            if entry and entry.get("enabled")
            else "not offered by the gateway"
        )
    if entry is None:
        return "offered; tick it to add it" if offered else ""
    if entry.get("enabled"):
        return "in use"
    return f"disabled: {entry.get('disabled_reason') or 'no reason recorded'}"


async def save_gateway(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    endpoint_url: str,
    api_key: str | None,
    reason: str | None,
) -> dict[str, Any]:
    """Set the URL (and the key, when one is given) in one step, then test both. The
    answer carries the test in plain words; a failed test still saves, because the
    operator may be setting the gateway up before it is running."""
    reason = guard_mutation(ctx, uow, reason, principal=principal.name, operation="gateway set")
    endpoint_url = endpoint_url.strip()
    try:
        validate_endpoint("local", endpoint_url)
    except ValueError as exc:
        raise ContractValidationError(
            f"the gateway URL is not valid: {exc}",
            errors=[{"path": "endpoint_url", "message": str(exc)}],
        ) from None
    if HERMES not in ctx.harnesses.names():
        raise GatewayError("no Hermes adapter is registered, so there is no gateway to set")
    key_given = bool(api_key and api_key.strip())
    before, _ = gateway_url(uow)
    uow.provider_settings.put(
        ProviderSetting(
            name=GATEWAY_SETTING,
            document={"endpoint_url": endpoint_url},
            updated_at=ctx.clock.now(),
            updated_by=principal.name,
            reason=reason,
        )
    )
    routing_version: int | None = None
    try:
        policy, routing = active_documents(uow)
    except NotFoundError:
        policy = routing = None
    if policy is not None and routing is not None:
        document = copy.deepcopy(routing.document)
        local = [m for m in document.get("models", []) if m.get("endpoint") == "local"]
        if local and any(m.get("endpoint_url") != endpoint_url for m in local):
            for model in local:
                model["endpoint_url"] = endpoint_url
                # The seed's reason said the URL was missing; it no longer is.
                if not model.get("enabled") and SEED_REASON_MARK in str(
                    model.get("disabled_reason") or ""
                ):
                    model["disabled_reason"] = NOT_PICKED
            _, routing_version = publish_routing(
                ctx,
                uow,
                principal=principal,
                policy=policy,
                routing=routing,
                routing_document=document,
                reason=reason,
                note="Local gateway URL update",
            )
    admin_event(
        uow,
        ctx,
        EventKind.LOCAL_GATEWAY_UPDATED,
        principal=principal.name,
        reason=reason,
        before={"endpoint_url": before},
        after={
            "endpoint_url": endpoint_url,
            "key_changed": key_given,
            "routing_version": routing_version,
        },
        change="gateway",
    )
    if key_given or before != endpoint_url:
        # A new key or URL has not been tested yet: an earlier pass must not keep the
        # credential reading as verified if this test turns out inconclusive, and a new
        # key has not been refused by anyone yet (#123).
        state = uow.harnesses.get(HERMES)
        if state is not None:
            state.last_validated_at = None
            if key_given:
                state.last_auth_failure_at = None
            state.updated_at = ctx.clock.now()
            uow.harnesses.put(state)
    if key_given:
        # Last of the writes: the key leaves the transaction (a Secret or a file), so
        # everything that can still refuse has already run (#119 review).
        assert api_key is not None
        await credentials.write_api_key(
            ctx, uow, principal=principal.name, harness=HERMES, api_key=api_key, reason=reason
        )
    return await _tested(ctx, uow, principal=principal.name, reason=reason)


async def test_gateway(
    ctx: AdminContext, uow: UnitOfWork, *, principal: Principal, reason: str | None
) -> dict[str, Any]:
    """Test the saved URL and key again: the Hermes credential validation, whose probe
    is the gateway's readiness check and then `/models` with the key."""
    reason = guard_mutation(ctx, uow, reason, principal=principal.name, operation="gateway test")
    return await _tested(ctx, uow, principal=principal.name, reason=reason)


async def _tested(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, reason: str
) -> dict[str, Any]:
    report = await credentials.validate(
        ctx, uow, principal=principal, harness=HERMES, reason=reason
    )
    probe = report.probe
    if probe is not None:
        summary = probe.detail
    elif report.shape is not None and not report.shape.ok:
        summary = "No Hermes key is stored, so the gateway was not tested."
    else:
        summary = "The gateway was not tested."
    return {
        "gateway": gateway_view(ctx, uow),
        "test": {
            "passed": bool(report.extra.get("validated")),
            "conclusive": bool(report.extra.get("conclusive")),
            "cause": report.extra.get("cause") or "",
            "summary": summary,
        },
    }


def _flag(item: dict[str, Any], name: str, index: int) -> bool:
    value = item.get(name, False)
    if not isinstance(value, bool):
        raise ContractValidationError(
            f"models[{index}].{name} must be a JSON boolean",
            errors=[{"path": f"models.{index}.{name}", "message": "must be a JSON boolean"}],
        )
    return value


async def save_models(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    models: list[dict[str, Any]],
    max_concurrency: int | None,
    reason: str | None,
    preview: bool = False,
) -> dict[str, Any]:
    """Create or update the local model entries from the operator's picks, in a new
    routing policy version. The gateway is asked what it offers at the moment of the
    save, so a pick is valid when it is made (crucible#121).

    With `preview`, nothing is written: the answer is the delta the save would publish
    and the projects following routing unpinned that would get it (hades #437)."""
    reason = guard_mutation(ctx, uow, reason, principal=principal.name, operation="gateway models")
    endpoint, _ = gateway_url(uow)
    if endpoint is None:
        raise GatewayError("the gateway URL is not set; set it and the key first")
    bearer = await asyncio.to_thread(credentials.read_api_key, ctx, HERMES)
    if bearer is None:
        raise GatewayError(f"no Hermes key is stored, so gateway {endpoint} cannot be asked")
    offered = await asyncio.to_thread(fetch_models, endpoint, bearer)
    if max_concurrency is not None and max_concurrency < 1:
        raise ContractValidationError(
            "max_concurrency must be at least 1",
            errors=[{"path": "max_concurrency", "message": "must be at least 1"}],
        )
    picks: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(models):
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            raise ContractValidationError(
                f"models[{index}].id is required",
                errors=[{"path": f"models.{index}.id", "message": "must not be empty"}],
            )
        capability = item.get("capability")
        if capability is not None and capability not in CAPABILITIES:
            raise ContractValidationError(
                f"models[{index}].capability must be one of {list(CAPABILITIES)}",
                errors=[{"path": f"models.{index}.capability", "message": "unknown capability"}],
            )
        picks[model_id] = {
            "enabled": _flag(item, "enabled", index) if "enabled" in item else None,
            "enable_thinking": _flag(item, "enable_thinking", index),
            "capability": capability,
            "codex_enabled": _flag(item, "codex_enabled", index)
            if "codex_enabled" in item
            else None,
        }
    policy, routing = active_documents(uow)
    document = copy.deepcopy(routing.document)
    entries = document.setdefault("models", [])
    by_id = {str(m.get("id")): m for m in entries}
    subscription = sorted(i for i in picks if i in by_id and by_id[i].get("endpoint") != "local")
    if subscription:
        raise GatewayError(
            f"{subscription} are subscription model entries of the routing policy in force, "
            "not local ones; pick a model the gateway offers"
        )
    # A new model the gateway does not list is a typo or a stale page: refused by name.
    # An entry already in force that the gateway stopped listing is disabled below with
    # that reason, whatever the pick said, so a page saved as it was shown still saves.
    known_aliases = {
        str(m.get("model_name") or m["id"]) for m in entries if m.get("endpoint") == "local"
    }
    unoffered = sorted(
        i
        for i, pick in picks.items()
        if (pick["enabled"] or pick["codex_enabled"])
        and i not in offered
        and i not in known_aliases
    )
    if unoffered:
        raise GatewayError(
            f"gateway {endpoint} does not offer {unoffered}; only a model it lists can be enabled"
        )
    local = [m for m in entries if m.get("endpoint") == "local"]
    before_enabled = sorted(str(m["id"]) for m in local if m.get("enabled"))
    pool_name = str(local[0]["pool"]) if local else DEFAULT_POOL
    pools = document.setdefault("pools", {})
    if pool_name not in pools:
        pools[pool_name] = copy.deepcopy(DEFAULT_POOL_DOCUMENT)
    if max_concurrency is not None:
        pools[pool_name]["max_concurrency"] = max_concurrency
    added: list[str] = []
    expanded: list[tuple[str, str, str, dict[str, Any]]] = []
    for model_id, pick in picks.items():
        if pick["enabled"] is not None:
            expanded.append((model_id, model_id, HERMES, pick))
        if pick["codex_enabled"] is not None:
            existing = next(
                (
                    m
                    for m in local
                    if m.get("harness") == "codex" and (m.get("model_name") or m["id"]) == model_id
                ),
                None,
            )
            route_id = str(existing["id"]) if existing else f"codex-local:{model_id}"
            if route_id in by_id and existing is None:
                raise GatewayError(f"routing id {route_id!r} is already in use")
            expanded.append(
                (route_id, model_id, "codex", {**pick, "enabled": pick["codex_enabled"]})
            )
    for model_id, gateway_model, harness, pick in expanded:
        entry = by_id.get(model_id)
        if entry is None:
            if not pick["enabled"]:
                continue  # nothing to keep for a model that was neither in use nor picked
            entry = {
                "id": model_id,
                "harness": harness,
                "model_name": gateway_model,
                "endpoint": "local",
                "endpoint_url": endpoint,
                "capability": pick["capability"] or "mid",
                "cost": "none",
                "speed": "fast",
                "pool": pool_name,
                "weight": 1,
                "enabled": True,
                "disabled_reason": None,
                "chat_template_kwargs": {"enable_thinking": pick["enable_thinking"]},
            }
            entries.append(entry)
            local.append(entry)
            added.append(model_id)
            continue
        entry["enabled"] = pick["enabled"]
        entry["disabled_reason"] = None if pick["enabled"] else "operator did not pick it"
        entry["chat_template_kwargs"] = {"enable_thinking": pick["enable_thinking"]}
        if pick["capability"]:
            entry["capability"] = pick["capability"]
    disabled_not_offered: list[str] = []
    for entry in local:
        entry["endpoint_url"] = endpoint
        if str(entry.get("model_name") or entry["id"]) not in offered and entry.get("enabled"):
            entry["enabled"] = False
            entry["disabled_reason"] = NOT_OFFERED
            disabled_not_offered.append(str(entry["id"]))
    try:
        parse_routing_policy({**document, "version": routing.version})
    except ValueError as exc:
        raise ContractValidationError(
            f"the routing policy these picks would make is not valid: {exc}",
            errors=[{"path": "models", "message": str(exc)}],
        ) from None
    if preview:
        return {
            "preview": True,
            "routing_policy": {"name": routing.name, "version": routing.version},
            "delta": publish_delta(uow, policy=policy, routing=routing, routing_document=document),
        }
    policy_version, routing_version = publish_routing(
        ctx,
        uow,
        principal=principal,
        policy=policy,
        routing=routing,
        routing_document=document,
        reason=reason,
        note="Local gateway models update",
    )
    after_enabled = sorted(str(m["id"]) for m in local if m.get("enabled"))
    admin_event(
        uow,
        ctx,
        EventKind.LOCAL_GATEWAY_UPDATED,
        principal=principal.name,
        reason=reason,
        before={"enabled": before_enabled},
        after={
            "enabled": after_enabled,
            "added": added,
            "disabled_not_offered": disabled_not_offered,
            "routing_version": routing_version,
        },
        change="models",
    )
    return {
        "policy": {"name": policy.name, "version": policy_version},
        "routing_policy": {"name": routing.name, "version": routing_version},
        "endpoint_url": endpoint,
        "enabled": after_enabled,
        "added": added,
        "disabled_not_offered": disabled_not_offered,
        "gateway": gateway_view(ctx, uow),
    }


__all__ = [
    "GatewayError",
    "fetch_models",
    "gateway_view",
    "models_view",
    "plain_outcome",
    "save_gateway",
    "save_models",
    "test_gateway",
]


# ----- Hermes run limits (FDY-0140) ------------------------------------------------


def hermes_limits_view(uow: UnitOfWork) -> dict[str, Any]:
    """The turn, context and response limits the next Hermes launch gets, whether they
    were saved or are the defaults, and the ranges a save accepts."""
    saved = uow.provider_settings.get(setting_name(HERMES))
    limits = hermes_run_limits(saved.document if saved is not None else None)
    return {
        **limits.as_dict(),
        "saved": saved is not None,
        "updated_at": saved.updated_at.isoformat() if saved is not None else None,
        "updated_by": saved.updated_by if saved is not None else None,
        "defaults": HermesRunLimits().as_dict(),
        "max_turns_range": list(MAX_TURNS_RANGE),
        "context_length_range": list(CONTEXT_LENGTH_RANGE),
        "max_output_tokens_range": list(MAX_OUTPUT_TOKENS_RANGE),
    }


def save_hermes_limits(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    max_turns: int,
    context_length: int,
    reason: str | None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Save the Hermes run limits. They apply from the next launch; a run already going
    keeps the limits it started with (hades #388: they are recorded on the attempt).
    `max_output_tokens` None keeps the saved allowance."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal.name, operation="hermes limits set"
    )
    before = hermes_limits_view(uow)
    if max_output_tokens is None:
        max_output_tokens = int(before["max_output_tokens"])
    problems = hermes_limit_problems(max_turns, context_length, max_output_tokens)
    if problems:
        raise ContractValidationError(
            "; ".join(problems),
            errors=[{"path": "hermes_limits", "message": problem} for problem in problems],
        )
    after = HermesRunLimits(
        max_turns=max_turns, context_length=context_length, max_output_tokens=max_output_tokens
    ).as_dict()
    uow.provider_settings.put(
        ProviderSetting(
            name=setting_name(HERMES),
            document=after,
            updated_at=ctx.clock.now(),
            updated_by=principal.name,
            reason=reason,
        )
    )
    admin_event(
        uow,
        ctx,
        EventKind.LOCAL_GATEWAY_UPDATED,
        principal=principal.name,
        reason=reason,
        before={
            "max_turns": before["max_turns"],
            "context_length": before["context_length"],
            "max_output_tokens": before["max_output_tokens"],
        },
        after=after,
        change="hermes_limits",
    )
    return hermes_limits_view(uow)
