from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.render import _page, _redirect
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    audit,
    harness_test,
    harnesses,
    status,
)
from crucible.application.admin.providers import providers_status
from crucible.domain.entities import Principal, Role

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)

# Human-friendly display names for harnesses; the key is the identifier shown in
# the table.  Operators read the display name and get the identifier on hover via
# the title attribute (issue 149).
DISPLAY_NAME: dict[str, str] = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "agy": "AGY",
    "hermes": "Hermes",
    "qwen_code": "Qwen Code",
    "script-harness": "Script harness",
}


def _harness_display(name: str) -> dict[str, str]:
    """Return a note cell that shows a display name with the identifier on hover."""
    display = DISPLAY_NAME.get(name, name)
    return {
        "kind": "note",
        "value": display,
        "hint": name,
    }


# The first readiness step of a harness in one word (crucible#115, #123).
STEP_WORDS = {
    "disabled": "disabled",
    "credential_missing": "needs a credential",
    "credential_unreadable": "credential unreadable",
    "credential_invalid": "credential refused",
    "credential_not_verified": "credential not verified",
    "endpoint_not_configured": "needs the gateway",
    "no_enabled_model": "needs a model",
    "endpoint_unreachable": "gateway unreachable",
    "no_promoted_image": "needs an image",
    "promoted_image_missing": "image no longer listed",
}


def _harness_status(item: dict[str, Any], ready: dict[str, Any] | None) -> dict[str, Any]:
    """The one word an operator acts on, most blocking first, from the same readiness
    Status shows. A test fixture has no readiness entry and is judged on its image."""
    if not item["enabled_by_configuration"] and not item.get("decided_by_administrator"):
        # hades #174: the configuration is the starting value, and Enable here decides.
        return {
            "kind": "status",
            "value": "off by default",
            "tone": "warn",
            "hint": f"{item.get('warning') or 'off in configuration'}. Enable it to use it.",
        }
    if ready is not None and ready["steps"]:
        step = ready["steps"][0]
        return {
            "kind": "status",
            "value": STEP_WORDS.get(step["code"], "not ready"),
            "tone": "warn",
            "hint": step["text"] if len(ready["steps"]) == 1 else None,
        }
    if item.get("warning") and item["enabled"]:
        return {
            "kind": "status",
            "value": "ready, unverified",
            "tone": "warn",
            "hint": f"{item['warning']}. Test proves it.",
        }
    if ready is None and not item["enabled"]:
        return {"kind": "status", "value": "disabled", "tone": "warn"}
    if ready is None and not item.get("default_image"):
        return {"kind": "status", "value": "needs an image", "tone": "warn"}
    return {"kind": "status", "value": "ready", "tone": "ok"}


def _last_gateway_or_routing_change_ts(uow: UoW) -> str | None:
    """Return the timestamp of the most recent gateway or routing change, or None."""
    admin_kindswithts = (
        "local_gateway_updated",
        "routing_policy_uploaded",
    )
    latest: str | None = None
    # Scan forward from the beginning to find the latest matching event.
    cursor: int | None = 0
    while True:
        result = audit.tail(uow, cursor=cursor, limit=200)
        items = result["items"]
        if not items:
            break
        for item in items:
            if item["kind"] in admin_kindswithts:
                ts = item["ts"]
                if latest is None or ts > latest:
                    latest = ts
        cursor = result["next_cursor"]
        if not items or len(items) < 200:
            break
    return latest


def _test_cell(last: dict[str, Any] | None, uow: UoW | None = None) -> dict[str, Any]:
    if not last:
        return {"kind": "note", "value": "not tested yet"}
    tones = {"pass": "ok", "fail": "bad", "not run": "accent"}
    steps_out: list[dict[str, Any]] = [
        {**step, "tone": tones.get(str(step.get("result")), "accent")}
        for step in last.get("steps", [])
        if step.get("result") != "not run"
    ]
    result: dict[str, Any] = {
        "kind": "steps",
        "items": steps_out,
        "tested_at": last.get("tested_at"),
    }
    # Check staleness: is the stored test older than the last gateway/routing change?
    if uow is not None and last.get("tested_at"):
        latest_change = _last_gateway_or_routing_change_ts(uow)
        if latest_change is not None and last["tested_at"] < latest_change:
            result["note"] = (
                "this result is from before you changed the gateway or routing; run Test again"
            )
    return result


@router.get("/harnesses", response_class=HTMLResponse)
async def harness_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    discovered = await harnesses.list_images(ctx.admin)
    items, secrets = await harnesses.read_harnesses(
        ctx.admin, uow, [item for _, item in discovered]
    )
    ready_by_name = {
        entry["name"]: entry
        for entry in status.harness_readiness(
            ctx.admin, uow, items, await providers_status(ctx.admin), secrets
        )
    }
    admin = principal.role is Role.ADMIN
    rows: list[list[Any]] = []
    for item in items:
        name = item["name"]
        image = item.get("default_image")
        last = item.get("last_test")
        actions: list[dict[str, Any]] = []
        if admin and item["enabled"]:
            actions.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/harness-test",
                    "label": "Test",
                    "primary": True,
                    "hidden": {"harness": name},
                }
            )
        if admin:
            # hades #174: every harness, whatever its configuration default; enabling an
            # unverified one is allowed, with the warning beside it in the Status column.
            actions.append(
                {
                    "kind": "form",
                    "action": "/ui/actions/harness",
                    "label": "Disable" if item["enabled"] else "Enable",
                    "reason": "optional",
                    "hidden": {
                        "harness": name,
                        "enabled": "false" if item["enabled"] else "true",
                    },
                }
            )
        rows.append(
            [
                _harness_display(name),
                _harness_status(item, ready_by_name.get(name)),
                (
                    {
                        "kind": "note",
                        "value": image["reference"],
                        "hint": f"{name} {image['version']}",
                    }
                    if image
                    else {"kind": "link", "href": "/ui/images", "label": "Choose on Images"}
                ),
                item["credential"]["state"].replace("_", " "),
                _test_cell(last, uow),
                {"kind": "actions", "items": actions} if actions else "",
            ]
        )
    sections: list[dict[str, Any]] = [
        {
            "title": "Harnesses",
            "note": (
                "Test runs what a task runs: the harness's image, its credential, a worker "
                "under the worker's egress, and one small model call. It takes up to a "
                "couple of minutes. Qwen Code uses the Local gateway key shared with Hermes. "
                "Its per-turn tool-call cap is disabled; its context window comes from "
                "the routing model (131072 tokens by default)."
            ),
            "columns": ["Harness", "Status", "Image", "Credential", "Last test", ""],
            "rows": rows,
            "details": [
                {
                    "title": "Gates, versions and use",
                    "columns": [
                        "Harness",
                        "Configuration default",
                        "Administrator's decision",
                        "Why",
                        "Warning",
                        "Tested versions",
                        "Running now",
                    ],
                    "rows": [
                        [
                            _harness_display(item["name"]),
                            "on" if item["enabled_by_configuration"] else "off",
                            (
                                ("enabled" if item["enabled_by_administrator"] else "disabled")
                                if item.get("decided_by_administrator")
                                else "none yet"
                            ),
                            item["reason"] or "none",
                            item.get("warning") or "none",
                            item["supported_versions"],
                            item.get("concurrency_in_use", 0),
                        ]
                        for item in items
                    ],
                    "note": (
                        "The configuration default (Settings) is where a harness starts. "
                        "Once an administrator enables or disables it with the buttons "
                        "above, that decision holds, takes effect for new tasks at once, "
                        "and needs no restart."
                    ),
                }
            ],
        }
    ]
    return _page(
        request,
        principal,
        csrf,
        active="/ui/harnesses",
        heading="Harnesses",
        intro="Whether each harness can run a task, and a test that proves it.",
        sections=sections,
    )


async def _action_harness(
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
    harnesses.set_enabled(
        ctx.admin,
        uow,
        principal=principal.name,
        harness=form.get("harness", ""),
        enabled=form.get("enabled") == "true",
        reason=reason,
    )
    return None


register("harness", _action_harness)


async def _action_harness_test(
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
    result = await harness_test.test_harness(
        ctx.admin, uow, principal=principal.name, harness=form.get("harness", "")
    )
    uow.commit()
    failed = result["failed_step"]
    message = (
        f"{result['harness']} passed every step."
        if result["ok"]
        else f"{result['harness']} failed at {failed}: "
        + next(s["detail"] for s in result["steps"] if s["name"] == failed)
    )
    return _redirect(form, message, kind="ok" if result["ok"] else "bad")


register("harness-test", _action_harness_test)
