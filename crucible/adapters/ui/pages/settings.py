from __future__ import annotations

import os
import tomllib
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.render import _page, _redirect
from crucible.adapters.ui.session import _require
from crucible.application.admin import credentials, delivery, kubernetes, status_cache
from crucible.domain.entities import Principal, Role

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        out: list[tuple[str, Any]] = []
        for key in sorted(value):
            out.extend(_flatten(value[key], f"{prefix}.{key}" if prefix else str(key)))
        return out
    return [(prefix, value)]


# The settings-file keys the `kubernetes.egress` admin setting replaces at runtime.
_EGRESS_SEEDS = [
    ["kubernetes", key]
    for key in (
        "dns_namespace",
        "dns_pod_labels",
        "local_endpoint_namespace",
        "local_endpoint_pod_labels",
        "local_endpoint_port",
    )
]


def _setting_applies(path: str, settings: Any) -> bool:
    """Whether a restart-bound setting does anything on this deployment (crucible#125):
    a provider's settings apply only while it is enabled (its `enabled` row stays, so
    the page still says it is off), and a credential's directory settings do not apply
    where the credentials are Secrets the service owns (Kubernetes without Docker, ADR
    0015)."""
    parts = path.split(".")
    if parts[0] in ("docker", "kubernetes") and parts[-1] != "enabled":
        return bool(getattr(settings, parts[0]).enabled)
    secrets_held = settings.kubernetes.enabled and not settings.docker.enabled
    return not (parts[0] == "credentials" and secrets_held and parts[-1] in ("path", "source"))


# 26 and issue 61: the one restart-bound setting that widens what a worker can reach. The
# page intro already says every setting here is read at start (crucible#115).
_BROAD_EGRESS_REASON = (
    "On, the git and login roles' egress is the public internet on 443 minus the denied "
    "ranges, GitHub included, instead of the resolved allowlist. A worker and a verifier "
    "always get the resolved allowlist."
)


# hades #174: a harness's configuration entry is where it starts, not a lock.
_HARNESS_DEFAULT_REASON = (
    "The starting value only. Enable or disable the harness on Harnesses; an "
    "administrator's decision there wins and needs no restart."
)


def _settings_rows(settings: Any) -> list[list[Any]]:
    if settings is None or not hasattr(settings, "model_dump"):
        return []
    config_path = os.environ.get("CRUCIBLE_CONFIG") or settings.model_config.get("toml_file")
    file_document: dict[str, Any] = {}
    if config_path:
        try:
            with open(config_path, "rb") as handle:
                file_document = tomllib.load(handle)
        except OSError:
            pass
    sensitive = {"database.url", "wake.secret", "wake.webhook_url"}
    rows = []
    for path, value in _flatten(settings.model_dump(mode="json")):
        if not _setting_applies(path, settings):
            continue
        env_name = "CRUCIBLE_" + path.replace(".", "__").upper()
        cursor: Any = file_document
        in_file = True
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                in_file = False
                break
            cursor = cursor[part]
        source = "environment" if env_name in os.environ else "file" if in_file else "default"
        shown = value
        if path in sensitive:
            shown = "present" if value else "absent"
        elif isinstance(value, str) and "://" in value:
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                hostname = parsed.hostname or ""
                if parsed.port is not None:
                    hostname += f":{parsed.port}"
                shown = urlunsplit(
                    (
                        parsed.scheme,
                        f"[credentials]@{hostname}",
                        parsed.path,
                        parsed.query,
                        parsed.fragment,
                    )
                )
        reason = (
            "The value is never shown."
            if path in sensitive
            else "Seeds the egress selectors; edit them on Routing, where a saved value wins."
            if path.split(".")[:2] in _EGRESS_SEEDS
            else "Seeds the short-role timeout; edit it on Routing, where a saved value wins."
            if path == "kubernetes.role_timeout_seconds"
            else _BROAD_EGRESS_REASON
            if path == "kubernetes.broad_egress"
            else _HARNESS_DEFAULT_REASON
            if path.startswith("harnesses.") and path.endswith(".enabled")
            else ""
        )
        rows.append([path, shown, source, reason])
    return rows


def _runtime_rows(ctx: Ctx, uow: UoW, principal: Principal) -> list[list[Any]]:
    """Runtime settings in this part, including deployment values that are only seeds."""
    rows: list[list[Any]] = []
    settings: Any = ctx.settings
    if ctx.admin is not None:
        ttl = status_cache.ttl_value(ctx.admin, uow)
        action: Any = "Administrator only"
        if principal.role is Role.ADMIN:
            action = {
                "kind": "form",
                "action": "/ui/actions/status-cache",
                "label": "Save cache TTL",
                "reason": "optional",
                "select": {
                    "name": "seconds",
                    "label": "Cache TTL (seconds)",
                    "selected": str(float(ttl.value)),
                    "options": [
                        (str(seconds), f"{seconds:g} seconds")
                        for seconds in sorted(
                            {5.0, 15.0, 30.0, 60.0, 120.0, 300.0, float(ttl.value)}
                        )
                    ],
                },
            }
        rows.append([ttl.name + ".ttl_seconds", ttl.value, ttl.source, ttl.applies, action])
        timeouts = kubernetes.timeouts_view(ctx.admin, uow)
        rows.append(
            [
                "kubernetes.timeouts.api_retry_seconds",
                timeouts["document"]["api_retry_seconds"],
                timeouts["api_retry_seconds_source"],
                timeouts["api_retry_seconds_applies"],
                {"kind": "link", "href": "/ui/routing", "label": "Edit on Routing"},
            ]
        )
        names = ctx.admin.harnesses.names()
        for name in names if isinstance(names, (list, tuple)) else ():
            adapter = ctx.admin.harnesses.get(name)
            if adapter is not None and adapter.credential_spec() is not None:
                value = credentials.mount_mode_value(ctx.admin, uow, name)
                action = "Administrator only"
                if principal.role is Role.ADMIN:
                    action = {
                        "kind": "link",
                        "href": "/ui/credentials",
                        "label": "Edit on Credentials",
                    }
                rows.append([value.name, value.value, value.source, value.applies, action])
    if settings is not None:
        for name, seed in sorted(settings.harnesses.items()):
            state = uow.harnesses.get(name)
            saved = bool(state is not None and getattr(state, "enabled_decided", False))
            rows.extend(
                [
                    [
                        f"harnesses.{name}.enabled",
                        state.enabled if saved and state is not None else seed.enabled,
                        "saved" if saved else "environment",
                        "immediately",
                        {"kind": "link", "href": "/ui/harnesses", "label": "Edit on Harnesses"},
                    ],
                    [
                        f"harnesses.{name}.reason",
                        state.reason if saved and state is not None else seed.reason,
                        "saved" if saved else "environment",
                        "immediately",
                        {"kind": "link", "href": "/ui/harnesses", "label": "Edit on Harnesses"},
                    ],
                ]
            )
    return rows


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    enabled = delivery.auto_merge_view(uow)["enabled"]
    merge_action = {
        "kind": "form",
        "action": "/ui/actions/auto-merge",
        "label": "Disable auto-merge" if enabled else "Enable auto-merge",
        "reason": "optional",
        "hidden": {"enabled": "false" if enabled else "true"},
    }
    rows = _settings_rows(ctx.settings)
    runtime_rows = _runtime_rows(ctx, uow, principal)
    # Lead with what this deployment set; the defaults it left alone go behind a click
    # (crucible#115).
    chosen = [
        [path, value, source, note] for path, value, source, note in rows if source != "default"
    ]
    defaults = [[path, value, note] for path, value, source, note in rows if source == "default"]
    return _page(
        request,
        principal,
        csrf,
        active="/ui/settings",
        heading="Settings",
        intro=(
            "Runtime settings are saved inside Hades. Deployment values seed them only "
            "until an administrator saves a value. Other deployment settings are read "
            "when the service starts."
        ),
        sections=[
            {
                "title": "Runtime settings",
                "columns": ["Setting", "Effective value", "Source", "Change applies", ""],
                "rows": runtime_rows,
            },
            {
                "title": "Automatic squash merge",
                "columns": ["Status", "Action"],
                "rows": [
                    [
                        "Enabled" if enabled else "Disabled",
                        merge_action if principal.role is Role.ADMIN else "Administrator only",
                    ]
                ],
                "intro": (
                    "Applies to every repository without a restart. Repository policy may opt out."
                ),
            },
            {
                "title": "Set on this deployment",
                "empty": "Every setting is at its default.",
                "columns": ["Setting", "Value", "Set in", "Note"],
                "rows": chosen,
                "details_label": f"Defaults left unchanged ({len(defaults)})",
                "details": [
                    {"title": "Defaults", "columns": ["Setting", "Value", "Note"], "rows": defaults}
                ],
            },
        ],
    )


async def _action_auto_merge(
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
    value = form.get("enabled")
    if value not in ("true", "false"):
        raise ValueError("enabled must be true or false")
    delivery.save_auto_merge(
        ctx.admin, uow, principal=principal, enabled=value == "true", reason=reason
    )
    uow.commit()
    return _redirect(form, "Auto-merge enabled." if value == "true" else "Auto-merge disabled.")


register("auto-merge", _action_auto_merge)


async def _action_status_cache(
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
    status_cache.save_ttl(
        ctx.admin,
        uow,
        principal=principal,
        seconds=float(form["seconds"]),
        reason=reason,
    )
    uow.commit()
    return _redirect(form, "Status cache TTL saved.")


register("status-cache", _action_status_cache)
