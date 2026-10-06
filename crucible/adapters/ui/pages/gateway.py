from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.render import _page, _redirect, _without_migration
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    credentials,
    gateway,
    routing,
)
from crucible.application.errors import (
    ConflictError,
)
from crucible.domain.entities import Principal, Role

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)

CAPABILITY_OPTIONS = [("small", "small"), ("mid", "mid"), ("frontier", "frontier")]
# hades #437: a routing version that flips a model or a pool cap overrides a decision.
REASON_LABEL = (
    "Reason (required when the save enables or disables a model or changes the pool cap: "
    "name the decision it supersedes)"
)


def _followers_words(followers: dict[str, list[str]]) -> str:
    projects = ", ".join(followers.get("unpinned_projects") or []) or "none"
    policies = ", ".join(followers.get("unpinned_policies") or []) or "none"
    return f"projects {projects} (delivery policies {policies})"


@router.get("/gateway", response_class=HTMLResponse)
async def gateway_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    """crucible#119, #121: the gateway URL and the Hermes key in one place, a test of
    both in plain words, and the gateway's own model list to pick from."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    secrets = await credentials.read_secrets(ctx.admin, [credentials.HERMES])
    view = gateway.gateway_view(ctx.admin, uow, secrets.get(credentials.HERMES))
    _models_requested = request.query_params.get("models") == "1"
    offered = await gateway.models_view(ctx.admin, uow, fetch=_models_requested)
    passed = view["last_outcome"] == "probe:completed"
    followers = _followers_words(routing.routing_followers(uow))
    # crucible#115: one row in plain words; the credential's state is on Credentials.
    sections: list[dict[str, Any]] = [
        {
            "title": "Gateway",
            "columns": ["URL", "Key", "Last test"],
            "rows": [
                [
                    view["endpoint_url"] or "not set",
                    "set" if view["key_set"] else "not set",
                    {
                        "kind": "status",
                        "value": view["last_test"],
                        "tone": "ok" if passed else "warn",
                        "hint": view["last_tested_at"] or "",
                    },
                ]
            ],
        }
    ]
    listing: dict[str, Any] = {
        "title": "Models the key can see",
        "note": offered["error"]
        or (
            f"Gateway {offered['endpoint_url']} lists {offered['offered_count']} "
            "model(s) for this key. Tick the ones to use. Saving publishes a new routing "
            "policy version for every project that follows routing unpinned: "
            f"{followers}. Before it publishes, this page shows what the version changes "
            "and asks you to confirm. A model the gateway no longer offers is disabled, "
            "not removed."
        ),
    }
    if _models_requested:
        # Build rows and model-choice form only when the operator asked for models.
        if principal.role is not Role.ADMIN:
            listing.update(
                columns=["Model", "Offered", "Hermes", "Codex", "Thinking", "Capability", "Note"],
                rows=[
                    [
                        row["id"],
                        row["offered"],
                        row["enabled"],
                        row["codex_enabled"],
                        row["enable_thinking"],
                        row["capability"],
                        _without_migration(row["note"]),
                    ]
                    for row in offered["models"]
                ],
            )
        else:
            rows: list[list[Any]] = []
            for index, row in enumerate(offered["models"]):
                rows.append(
                    [
                        {"kind": "hidden", "name": f"model.{index}.id", "value": row["id"]},
                        {
                            "kind": "checkbox",
                            "name": f"model.{index}.enabled",
                            "value": row["enabled"],
                            "label": f"use {row['id']}",
                        },
                        {
                            "kind": "checkbox",
                            "name": f"model.{index}.codex",
                            "value": row["codex_enabled"],
                            "label": f"use Codex for {row['id']}",
                        },
                        {
                            "kind": "checkbox",
                            "name": f"model.{index}.thinking",
                            "value": row["enable_thinking"],
                            "label": f"thinking for {row['id']}",
                        },
                        {
                            "kind": "select",
                            "name": f"model.{index}.capability",
                            "value": row["capability"],
                            "options": CAPABILITY_OPTIONS,
                            "label": f"capability of {row['id']}",
                        },
                        {"value": _without_migration(row["note"])},
                    ]
                )
            listing["form"] = {
                "action": "/ui/actions/gateway-models",
                "label": "Save model choices",
                "fields": [
                    {
                        "kind": "grid",
                        "label": "",
                        "columns": ["Model", "Hermes", "Codex", "Thinking", "Capability", "Note"],
                        "rows": rows,
                    },
                    {
                        "name": "max_concurrency",
                        "label": "Pool max concurrency",
                        "kind": "number",
                        "value": (offered["pool"] or {}).get("max_concurrency") or 4,
                        "required": True,
                    },
                    {"name": "reason", "label": "Reason", "reason_label": REASON_LABEL},
                ],
            }
        sections.append(listing)
    else:
        # Not fetched: show a note so the operator can list the models on demand.
        listing["button"] = {
            "href": "/ui/gateway?models=1",
            "label": "List the gateway's models",
        }
        sections.append(listing)

    # Admin controls are always built regardless of fetch state.
    if principal.role is Role.ADMIN:
        sections.append(
            {
                "title": "Set the gateway URL and key",
                "note": (
                    "The URL ends in /v1. Codex and Hermes share this LiteLLM virtual key; "
                    "it is stored as the Hermes credential and never shown. Saving tests "
                    "both. A changed URL publishes a new routing policy version for every "
                    f"project that follows routing unpinned: {followers}. That version "
                    "changes only the local entries' URL; it enables or disables no model "
                    "and changes no pool cap."
                ),
                "form": {
                    "action": "/ui/actions/gateway-save",
                    "label": "Save and test",
                    "collapsed": "Change the URL or key" if view["endpoint_url"] else None,
                    "fields": [
                        {
                            "name": "endpoint_url",
                            "label": "Gateway URL",
                            "kind": "url",
                            "value": view["endpoint_url"] or "",
                            "placeholder": "https://llm.example.internal/v1",
                            "required": True,
                        },
                        {
                            "name": "api_key",
                            "label": "Key (empty keeps the current one)"
                            if view["key_set"]
                            else "Key",
                            "kind": "password",
                            "required": not view["key_set"],
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
        if view["endpoint_url"]:
            # A check: no reason is asked for (crucible#117).
            sections[0]["form"] = {
                "action": "/ui/actions/gateway-test",
                "label": "Test the gateway again",
                "fields": [{"name": "reason", "label": "Reason"}],
            }
    sections.append(_hermes_limits_section(gateway.hermes_limits_view(uow), principal))
    return _page(
        request,
        principal,
        csrf,
        active="/ui/gateway",
        heading="Local gateway",
        intro="The gateway Hermes uses: its URL, its key, a test of both, and its models.",
        sections=sections,
        badge="tested" if view["last_outcome"] == "probe:completed" else "not verified",
        badge_kind="ok" if view["last_outcome"] == "probe:completed" else "warn",
    )


def _hermes_limits_section(limits: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """FDY-0140: how far one Hermes run may go, and the window it is told the model has.
    Hades #388: and the response allowance the gateway reserves out of that window."""
    context = limits["context_length"]
    section: dict[str, Any] = {
        "title": "Local run limits",
        "note": (
            "Applied from the next launch; an attempt keeps the values it started with. "
            "Max turns applies to Hermes only. "
            "Context length is the model's window in tokens for Hermes and local Codex; "
            "0 lets Hermes find it from the gateway and uses 131072 for Codex. "
            "Max output tokens is the response allowance the gateway reserves out of the "
            "window on every request; Hermes sends it and keeps its input below the "
            "window less this allowance."
        ),
        "columns": ["Max turns", "Context length", "Max output tokens", "Source"],
        "rows": [
            [
                limits["max_turns"],
                context or "found by Hermes",
                limits["max_output_tokens"],
                f"saved by {limits['updated_by']}" if limits["saved"] else "default",
            ]
        ],
    }
    if principal.role is Role.ADMIN:
        low_turns, high_turns = limits["max_turns_range"]
        low_context, high_context = limits["context_length_range"]
        low_output, high_output = limits["max_output_tokens_range"]
        section["form"] = {
            "action": "/ui/actions/hermes-limits",
            "label": "Save limits",
            "collapsed": "Change the limits",
            "fields": [
                {
                    "name": "max_turns",
                    "label": f"Max turns ({low_turns} to {high_turns})",
                    "kind": "number",
                    "value": limits["max_turns"],
                    "required": True,
                },
                {
                    "name": "context_length",
                    "label": f"Context length (0, or {low_context} to {high_context})",
                    "kind": "number",
                    "value": context,
                    "required": True,
                },
                {
                    "name": "max_output_tokens",
                    "label": f"Max output tokens ({low_output} to {high_output})",
                    "kind": "number",
                    "value": limits["max_output_tokens"],
                    "required": True,
                },
                {"name": "reason", "label": "Reason", "required": True},
            ],
        }
    return section


def _whole(form: dict[str, str], name: str, label: str) -> int:
    try:
        return int(form.get(name, "").strip())
    except ValueError:
        raise ConflictError(f"{label} must be a whole number") from None


async def _action_gateway_save(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    assert ctx.admin is not None
    result = await gateway.save_gateway(
        ctx.admin,
        uow,
        principal=principal,
        endpoint_url=form.get("endpoint_url", ""),
        api_key=form.get("api_key") or None,
        reason=reason,
    )
    uow.commit()
    return _redirect(
        form,
        f"Saved. {result['test']['summary']}",
        kind="ok" if result["test"]["passed"] else "warn",
    )


register("gateway-save", _action_gateway_save)


async def _action_gateway_test(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    assert ctx.admin is not None
    result = await gateway.test_gateway(ctx.admin, uow, principal=principal, reason=reason)
    uow.commit()
    return _redirect(
        form,
        result["test"]["summary"],
        kind="ok" if result["test"]["passed"] else "warn",
    )


register("gateway-test", _action_gateway_test)


async def _action_hermes_limits(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    assert ctx.admin is not None
    gateway.save_hermes_limits(
        ctx.admin,
        uow,
        principal=principal,
        max_turns=_whole(form, "max_turns", "Max turns"),
        context_length=_whole(form, "context_length", "Context length"),
        max_output_tokens=_whole(form, "max_output_tokens", "Max output tokens"),
        reason=reason,
    )
    uow.commit()
    return _redirect(form, "Saved the Hermes run limits. The next launch uses them.")


register("hermes-limits", _action_hermes_limits)


async def _action_gateway_models(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    assert ctx.admin is not None
    picks = []
    index = 0
    while f"model.{index}.id" in form:
        picks.append(
            {
                "id": form[f"model.{index}.id"],
                "enabled": form.get(f"model.{index}.enabled") == "true",
                "codex_enabled": form.get(f"model.{index}.codex") == "true",
                "enable_thinking": form.get(f"model.{index}.thinking") == "true",
                "capability": form.get(f"model.{index}.capability") or None,
            }
        )
        index += 1
    max_concurrency = int(form.get("max_concurrency") or "0") or None
    if form.get("confirm") != "true":
        # hades #437: show the delta this save publishes, and to whom, before it does.
        preview = await gateway.save_models(
            ctx.admin,
            uow,
            principal=principal,
            models=picks,
            max_concurrency=max_concurrency,
            reason=reason,
            preview=True,
        )
        return _confirm_page(request, principal, csrf, form, preview)
    saved = await gateway.save_models(
        ctx.admin,
        uow,
        principal=principal,
        models=picks,
        max_concurrency=max_concurrency,
        reason=reason,
    )
    uow.commit()
    enabled = ", ".join(saved["enabled"]) or "none"
    dropped = saved["disabled_not_offered"]
    return _redirect(
        form,
        f"Saved routing policy version {saved['routing_policy']['version']}. "
        f"Enabled: {enabled}."
        + (f" Disabled as no longer offered: {', '.join(dropped)}." if dropped else ""),
    )


register("gateway-models", _action_gateway_models)


def _confirm_page(
    request: Request,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    preview: dict[str, Any],
) -> Response:
    """The model choices back, with the routing version they would publish spelled out
    and a button that publishes it. Nothing was written to get here."""
    delta = preview["delta"]
    current = preview["routing_policy"]
    carried = [
        {"kind": "hidden", "name": name, "value": value}
        for name, value in form.items()
        if name.startswith("model.") or name == "max_concurrency"
    ]
    needs_reason = routing.delta_needs_reason(delta)
    return _page(
        request,
        principal,
        csrf,
        active="/ui/gateway",
        heading="Local gateway",
        intro="Confirm the routing version these model choices publish.",
        sections=[
            {
                "title": "What saving publishes",
                "note": (
                    f"Saving publishes routing {current['name']} version "
                    f"{int(current['version']) + 1} over version {current['version']} for "
                    "every project that follows routing unpinned: "
                    f"{_followers_words(delta)}. Nothing is saved until you publish."
                    + (
                        " It enables or disables a model or changes a pool cap, so the "
                        "reason must name the decision it supersedes, and the orchestrator "
                        "is woken with this change."
                        if needs_reason
                        else ""
                    )
                ),
                "columns": ["Change"],
                "rows": [[line] for line in routing.delta_words(delta)],
                "form": {
                    "action": "/ui/actions/gateway-models",
                    "label": "Publish this routing version",
                    "fields": [
                        *carried,
                        {"kind": "hidden", "name": "confirm", "value": "true"},
                        {
                            "name": "reason",
                            "label": "Reason",
                            "reason_label": REASON_LABEL,
                            "value": form.get("reason", ""),
                        },
                    ],
                },
            }
        ],
    )
