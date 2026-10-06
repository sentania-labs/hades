"""/v1/admin (25), admin role only, and /v1/capabilities for the orchestrator's read-only
view. Every handler is a thin call into crucible/application/admin/*; the CLI calls the
same functions in process."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Body, Query
from fastapi.exceptions import RequestValidationError

from crucible.adapters.api.deps import Admin, Ctx, Orchestrator, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.admin import (
    audit,
    board,
    credentials,
    delivery,
    gateway,
    github,
    github_manifest,
    harness_test,
    harnesses,
    images,
    login,
    routing,
    routing_preference,
    tokens,
)
from crucible.application.admin import gate_classes as gate_classes_admin
from crucible.application.admin import kubernetes as kubernetes_admin
from crucible.application.admin import limits as limits_admin
from crucible.application.admin import providers as providers_admin
from crucible.application.admin import repositories as repositories_admin
from crucible.application.admin import status as status_admin
from crucible.application.admin.context import AdminContext
from crucible.application.errors import ConflictError, ContractValidationError
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration

router = ThreadedAPIRouter()


@router.get("/admin/board")
def admin_board(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    """The same read-only projection rendered by the operator Board page."""
    return board.board_view(uow, ctx.clock.now())


def _admin(ctx: Ctx) -> AdminContext:
    if ctx.admin is None:
        raise ConflictError("the administrative surface is not configured")
    return ctx.admin


def _reason(body: dict[str, Any] | None) -> str | None:
    """Only a non-empty string is a reason. A JSON null stringifies to "None" and a zero
    to "0", and both would pass the guard as a reason nobody wrote."""
    value = (body or {}).get("reason")
    if isinstance(value, str) and value.strip():
        return value
    return None


@router.get("/admin/routing/exhaustion")
def admin_routing_exhaustion(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return routing.list_exhaustions(_admin(ctx), uow)


@router.post("/admin/routing/exhaustion/{pool}/clear")
def admin_clear_routing_exhaustion(
    pool: str,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    result = routing.clear_exhaustion(
        _admin(ctx), uow, principal=principal.name, pool=pool, reason=_reason(body)
    )
    uow.commit()
    return result


def _whole_milliseconds(body: dict[str, Any], name: str) -> int | None:
    """A JSON integer, never a bool or a string: a limit read from truthiness or a
    stringified number is a limit nobody set. Absent keeps the value in force."""
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestValidationError(
            [{"loc": ("body", name), "msg": f"{name} must be a JSON integer", "type": "int_type"}]
        )
    return value


@router.get("/admin/limits/command-timeout")
def admin_command_timeout(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    _admin(ctx)
    return limits_admin.command_timeout_view(uow)


@router.post("/admin/limits/command-timeout")
def admin_save_command_timeout(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    result = limits_admin.save_command_timeout(
        _admin(ctx),
        uow,
        principal=principal,
        minimum=_whole_milliseconds(body, "min"),
        maximum=_whole_milliseconds(body, "max"),
        default=_whole_milliseconds(body, "default"),
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.get("/admin/gates/advisory")
def admin_gate_classes(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    _admin(ctx)
    return gate_classes_admin.gate_classes_view(uow)


@router.post("/admin/gates/advisory")
def admin_save_gate_classes(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """ADR 0024: `advisory` is the whole advisory set, a list of pre-PR gate names."""
    advisory = body.get("advisory")
    if not isinstance(advisory, list) or not all(isinstance(g, str) for g in advisory):
        raise RequestValidationError(
            [
                {
                    "loc": ("body", "advisory"),
                    "msg": "advisory must be a JSON list of gate names",
                    "type": "list_type",
                }
            ]
        )
    result = gate_classes_admin.save_gate_classes(
        _admin(ctx), uow, principal=principal, advisory=advisory, reason=_reason(body)
    )
    uow.commit()
    return result


@router.get("/admin/routing/local-endpoint")
def admin_local_endpoint(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    _admin(ctx)
    return routing.local_endpoint_view(uow)


def _require_boolean_flags(models: list[dict[str, Any]]) -> None:
    """The flags must be JSON booleans. Truthiness would read the string "false" as on,
    enabling the model and opening its proxy destination."""
    errors = [
        {
            "loc": ("body", "models", index, flag),
            "msg": f"{flag} must be a JSON boolean",
            "type": "bool_type",
        }
        for index, item in enumerate(models)
        for flag in ("enabled", "enable_thinking", "codex_enabled")
        if flag in item and not isinstance(item[flag], bool)
    ]
    if errors:
        raise RequestValidationError(errors)


@router.post("/admin/routing/local-endpoint")
def admin_save_local_endpoint(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    models = body.get("models")
    if not isinstance(models, list) or not all(isinstance(item, dict) for item in models):
        raise ConflictError("models must be a list of local model settings")
    _require_boolean_flags(models)
    result = routing.save_local_endpoint(
        _admin(ctx),
        uow,
        principal=principal,
        endpoint_url=str(body.get("endpoint_url", "")),
        models=models,
        max_concurrency=int(body.get("max_concurrency", 0)),
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.get("/admin/routing/preference")
def admin_routing_preference(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    _admin(ctx)
    return routing_preference.preference_view(uow)


def _preference_body(body: dict[str, Any]) -> tuple[dict[str, list[str] | None], dict[str, Any]]:
    """ADR 0028. `tiers` maps a tier to a list of pool names, or to null for the default;
    `rotation` holds JSON integers and `quality_feedback` a JSON boolean. A stringified
    number or flag is refused, not read: it would be a setting nobody chose."""
    errors: list[dict[str, Any]] = []
    tiers = body.get("tiers", {})
    if not isinstance(tiers, dict):
        errors.append({"loc": ("body", "tiers"), "msg": "tiers must be an object"})
        tiers = {}
    for name, pools in tiers.items():
        if pools is not None and (
            not isinstance(pools, list) or not all(isinstance(p, str) for p in pools)
        ):
            errors.append(
                {
                    "loc": ("body", "tiers", name),
                    "msg": "a tier's pools must be a list of pool names, or null for the default",
                }
            )
    rotation = body.get("rotation", {})
    if not isinstance(rotation, dict):
        errors.append({"loc": ("body", "rotation"), "msg": "rotation must be an object"})
        rotation = {}
    for name, value in rotation.items():
        if name not in routing_preference.ROTATION_FIELDS:
            errors.append(
                {"loc": ("body", "rotation", name), "msg": "not an editable rotation setting"}
            )
            continue
        wanted = bool if name == "quality_feedback" else int
        if isinstance(value, bool) is not (wanted is bool) or not isinstance(value, wanted):
            errors.append(
                {
                    "loc": ("body", "rotation", name),
                    "msg": f"{name} must be a JSON {'boolean' if wanted is bool else 'integer'}",
                }
            )
    if not tiers and not rotation and not errors:
        errors.append({"loc": ("body",), "msg": "name at least one tier or rotation setting"})
    if errors:
        raise RequestValidationError(errors)
    return tiers, rotation


@router.post("/admin/routing/preference")
def admin_save_routing_preference(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    tiers, rotation = _preference_body(body)
    result = routing_preference.save_preference(
        _admin(ctx),
        uow,
        principal=principal,
        tiers=tiers,
        rotation=rotation,
        reason=_reason(body),
    )
    uow.commit()
    return result


# ----- the local gateway (crucible#119, #121) ------------------------------------


@router.get("/admin/gateway")
def admin_gateway(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return gateway.gateway_view(_admin(ctx), uow)


@router.post("/admin/gateway")
async def admin_save_gateway(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """The gateway URL and, when `api_key` is given, the Hermes key, in one step; then
    both are tested. The answer never carries the key."""
    api_key = body.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        raise ConflictError("api_key must be a string")
    result = await gateway.save_gateway(
        _admin(ctx),
        uow,
        principal=principal,
        endpoint_url=str(body.get("endpoint_url", "")),
        api_key=api_key,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.post("/admin/gateway/test")
async def admin_test_gateway(
    ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any] | None, Body()] = None
) -> dict[str, Any]:
    """A check, so no reason is asked for; one given is recorded (crucible#117)."""
    result = await gateway.test_gateway(_admin(ctx), uow, principal=principal, reason=_reason(body))
    uow.commit()
    return result


@router.get("/admin/gateway/models")
async def admin_gateway_models(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return await gateway.models_view(_admin(ctx), uow)


@router.post("/admin/gateway/models")
async def admin_save_gateway_models(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """`models` is a list of `{id, enabled, enable_thinking, capability}`; a model the
    gateway does not offer is refused by name."""
    models = body.get("models")
    if not isinstance(models, list) or not all(isinstance(item, dict) for item in models):
        raise ConflictError("models must be a list of model picks")
    _require_boolean_flags(models)
    limit = body.get("max_concurrency")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
        raise ConflictError("max_concurrency must be a whole number")
    result = await gateway.save_models(
        _admin(ctx),
        uow,
        principal=principal,
        models=models,
        max_concurrency=limit,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.get("/admin/gateway/hermes-limits")
def admin_hermes_limits(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    """FDY-0140: the turn, context and response limits the next Hermes launch gets."""
    return gateway.hermes_limits_view(uow)


@router.post("/admin/gateway/hermes-limits")
def admin_save_hermes_limits(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """`max_turns` and `context_length`, whole numbers; `context_length` 0 lets Hermes
    find the window itself. `max_output_tokens` (hades #388), optional, is the response
    allowance the gateway reserves; absent keeps the saved one. Applies from the next
    launch."""
    values = {}
    for name in ("max_turns", "context_length"):
        value = body.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConflictError(f"{name} must be a whole number")
        values[name] = value
    allowance = body.get("max_output_tokens")
    if allowance is not None and (isinstance(allowance, bool) or not isinstance(allowance, int)):
        raise ConflictError("max_output_tokens must be a whole number")
    result = gateway.save_hermes_limits(
        _admin(ctx),
        uow,
        principal=principal,
        max_turns=values["max_turns"],
        context_length=values["context_length"],
        max_output_tokens=allowance,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.get("/admin/kubernetes/egress")
def admin_kubernetes_egress(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return kubernetes_admin.egress_view(_admin(ctx), uow)


@router.post("/admin/kubernetes/egress")
def admin_save_kubernetes_egress(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """The body is the `kubernetes.egress` document (`dns`, `local_endpoint`) and a
    `reason`; the service checks the document and refuses it naming the field."""
    result = kubernetes_admin.save_egress(
        _admin(ctx),
        uow,
        principal=principal.name,
        document={key: body[key] for key in ("dns", "local_endpoint") if key in body},
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.get("/admin/kubernetes/timeouts")
def admin_kubernetes_timeouts(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return kubernetes_admin.timeouts_view(_admin(ctx), uow)


@router.post("/admin/kubernetes/timeouts")
def admin_save_kubernetes_timeouts(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """The body is the `kubernetes.timeouts` document and a `reason`; the service
    refuses timeout and retry values that are not whole numbers within bounds."""
    result = kubernetes_admin.save_timeouts(
        _admin(ctx),
        uow,
        principal=principal.name,
        document={
            key: body[key] for key in ("role_timeout_seconds", "api_retry_seconds") if key in body
        },
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.get("/admin/status")
async def admin_status(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return await status_admin.status(_admin(ctx), uow)


@router.get("/capabilities")
async def capabilities(ctx: Ctx, uow: UoW, principal: Orchestrator) -> dict[str, Any]:
    """25: what Foundry may read: harnesses, providers, github health, and its own
    workers, tasks and wakes. Read-only; it never calls a mutation."""
    return await status_admin.capabilities(_admin(ctx), uow, principal)


# ----- harnesses ---------------------------------------------------------------


@router.get("/admin/harnesses")
async def admin_harnesses(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    admin = _admin(ctx)
    found = await harnesses.list_images(admin)
    items, _ = await harnesses.read_harnesses(admin, uow, [i for _, i in found])
    return {"items": items}


@router.post("/admin/harnesses/{name}/enable")
def admin_enable(
    name: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = harnesses.set_enabled(
        _admin(ctx), uow, principal=principal.name, harness=name, enabled=True, reason=_reason(body)
    )
    uow.commit()
    return result


@router.post("/admin/harnesses/{name}/disable")
def admin_disable(
    name: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = harnesses.set_enabled(
        _admin(ctx),
        uow,
        principal=principal.name,
        harness=name,
        enabled=False,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.post("/admin/harnesses/{name}/test")
async def admin_test_harness(
    name: str,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """crucible#118: the path a real task takes, step by step, pass or fail in plain
    words. A check, so no reason is asked for; one given is recorded."""
    result = await harness_test.test_harness(
        _admin(ctx), uow, principal=principal.name, harness=name, reason=_reason(body)
    )
    uow.commit()
    return result


# ----- credentials -------------------------------------------------------------


@router.get("/admin/credentials/{harness}")
def admin_credential(harness: str, ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return credentials.state_view(_admin(ctx), uow, harness)


@router.post("/admin/credentials/{harness}/mount-mode")
def admin_credential_mount_mode(
    harness: str,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    mode = body.get("mount_mode", body.get("mode"))
    if not isinstance(mode, str):
        raise ContractValidationError(
            "mount_mode is required",
            errors=[{"path": "mount_mode", "message": "must be a string"}],
        )
    result = credentials.set_mount_mode(
        _admin(ctx),
        uow,
        principal=principal.name,
        harness=harness,
        mode=mode,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.post("/admin/credentials/{harness}/validate")
async def admin_validate(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    report = await credentials.validate(
        _admin(ctx), uow, principal=principal.name, harness=harness, reason=_reason(body)
    )
    uow.commit()
    return report.as_dict()


@router.post("/admin/credentials/{harness}/probe")
async def admin_probe(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    report = await credentials.probe(
        _admin(ctx), uow, principal=principal.name, harness=harness, reason=_reason(body)
    )
    uow.commit()
    return report.as_dict()


@router.post("/admin/credentials/{harness}/set")
async def admin_set_credential(
    harness: str,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    value = body.get("api_key")
    if not isinstance(value, str):
        raise ConflictError("api_key must be a string")
    report = await credentials.set_api_key(
        _admin(ctx),
        uow,
        principal=principal.name,
        harness=harness,
        api_key=value,
        reason=_reason(body),
    )
    uow.commit()
    return report.as_dict()


@router.post("/admin/credentials/{harness}/login")
def admin_login(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """25: starts the harness's own login pointed at the dedicated directory and returns
    the device or browser URL; poll with GET, submit a pasted code with /login/code,
    and finish with /login/finish once the CLI has exited. `replace` is required when a
    credential that still passes the shape check is in place, and retains it first.

    The login runs the harness's own CLI in the promoted worker image. The container gets
    only the selected harness credential directory and the worker egress-proxy network."""
    result = login.start_login(
        _admin(ctx),
        uow,
        ctx.logins,
        principal=principal.name,
        harness=harness,
        reason=_reason(body),
        replace=bool(body.get("replace", False)),
    )
    uow.commit()
    return result


@router.get("/admin/credentials/{harness}/login")
def admin_login_status(harness: str, ctx: Ctx, _principal: Admin) -> dict[str, Any]:
    return login.login_status(ctx.logins, harness, ctx.admin)


@router.post("/admin/credentials/{harness}/login/code")
def admin_login_code(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = login.submit_code(
        ctx.logins,
        harness,
        str(body.get("code", "")),
        ctx=_admin(ctx),
        uow=uow,
        principal=principal.name,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.post("/admin/credentials/{harness}/login/cancel")
def admin_login_cancel(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = login.cancel_login(
        ctx.logins,
        harness,
        ctx=_admin(ctx),
        uow=uow,
        principal=principal.name,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.post("/admin/credentials/{harness}/login/finish")
def admin_login_finish(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = login.finish_login(
        _admin(ctx),
        uow,
        ctx.logins,
        principal=principal.name,
        harness=harness,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.post("/admin/credentials/{harness}/rotate")
def admin_rotate(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    report = credentials.rotate(
        _admin(ctx),
        uow,
        principal=principal.name,
        harness=harness,
        new_path=str(body.get("new_path", "")),
        reason=_reason(body),
    )
    uow.commit()
    return report.as_dict()


@router.post("/admin/credentials/{harness}/remove")
def admin_remove(
    harness: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    report = credentials.remove(
        _admin(ctx), uow, principal=principal.name, harness=harness, reason=_reason(body)
    )
    uow.commit()
    return report.as_dict()


# ----- images, providers, github, repositories, audit ----------------------------


@router.get("/admin/images")
async def admin_images(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    """Every image a provider sees, and one row per harness: its default, the image a
    rollback returns to, and the images it may be promoted to (ADR 0018)."""
    admin = _admin(ctx)
    return {
        "items": await images.list_all(admin, uow),
        "defaults": await images.defaults(admin, uow),
    }


def _harness(body: dict[str, Any] | None) -> str:
    value = (body or {}).get("harness")
    if not isinstance(value, str) or not value:
        raise ContractValidationError(
            "promotion is per harness: name the harness",
            errors=[{"path": "harness", "message": "must name a harness"}],
        )
    return value


@router.post("/admin/images/{digest:path}/promote")
async def admin_promote(
    digest: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """Make the image the default for the one harness the body names (ADR 0018)."""
    result = await images.promote(
        _admin(ctx),
        uow,
        principal=principal.name,
        harness=_harness(body),
        digest=digest,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.post("/admin/images/rollback")
async def admin_rollback(
    ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """Return the harness the body names to the image its last promotion replaced."""
    result = await images.rollback(
        _admin(ctx), uow, principal=principal.name, harness=_harness(body), reason=_reason(body)
    )
    uow.commit()
    return result


@router.get("/admin/providers")
async def admin_providers(ctx: Ctx, _principal: Admin) -> dict[str, Any]:
    return {"items": await providers_admin.providers_status(_admin(ctx))}


@router.get("/admin/github")
def admin_github(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return github.status(_admin(ctx), uow)


@router.post("/admin/github/check")
def admin_github_check(
    ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any] | None, Body()] = None
) -> dict[str, Any]:
    """A check, so no reason is asked for; one given is recorded (crucible#117)."""
    result = github.check(_admin(ctx), uow, principal=principal.name, reason=_reason(body))
    uow.commit()
    return result


@router.get("/admin/github/external-url")
def admin_github_external_url(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    """The `github.external_url` setting (crucible#168): where GitHub sends the browser
    back in the Create GitHub App flow. None: the address the browser used."""
    return github_manifest.external_url_view(_admin(ctx), uow)


@router.post("/admin/github/external-url")
def admin_github_save_external_url(
    ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """Save the override; an empty or null `url` clears it."""
    url = body.get("url")
    if url is not None and not isinstance(url, str):
        raise ConflictError("url must be a string or null")
    result = github_manifest.save_external_url(
        _admin(ctx), uow, principal=principal.name, url=url, reason=_reason(body)
    )
    uow.commit()
    return result


@router.get("/admin/github/installations")
def admin_github_installations(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return github.apps_view(_admin(ctx), uow)


@router.post("/admin/github/repositories")
def admin_github_add_repository(
    ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """Register a repository an installation covers, with the installation id and the
    default branch GitHub reports."""
    installation_id = body.get("installation_id")
    if isinstance(installation_id, bool) or not isinstance(installation_id, int):
        raise ConflictError("installation_id must be a number")
    result = github.add_repository(
        _admin(ctx),
        uow,
        principal=principal.name,
        installation_id=installation_id,
        repository=str(body.get("repository", "")),
        name=body.get("name") if isinstance(body.get("name"), str) else None,
        policy_name=str(body.get("policy_name") or "default-software"),
        attested_all_prs=body.get("attested_all_prs") is True,
        attested_by=body.get("attested_by") if isinstance(body.get("attested_by"), str) else None,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.put("/admin/repositories/{name}")
def admin_register_repository(
    name: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """25 and 04: the same registration as PUT /repositories/{name}, under /admin so the
    table's every row has an admin path, and so under the two administrative rules: a
    reason and a live supervisor lease."""
    registration = RepositoryRegistration(
        url=str(body.get("url", "")),
        default_branch=str(body.get("default_branch", "main")),
        policy_name=str(body.get("policy_name", "default-software")),
        installation_id=body.get("installation_id"),
        external_review=ExternalReviewAttestation(
            attested_all_prs=bool(body.get("attested_all_prs", False)),
            attested_by=body.get("attested_by"),
        ),
        # Parsed by the model, as PUT /v1/repositories parses it.
        private=body.get("private", False),
    )
    result = repositories_admin.register(
        _admin(ctx),
        uow,
        principal=principal.name,
        name=name,
        registration=registration,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.get("/admin/repositories")
def admin_repositories(uow: UoW, _principal: Admin) -> dict[str, Any]:
    return {"items": repositories_admin.list_all(uow)}


@router.delete("/admin/repositories/{name}")
def admin_remove_repository(
    name: str, ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    result = repositories_admin.remove(
        _admin(ctx), uow, principal=principal.name, name=name, reason=_reason(body)
    )
    uow.commit()
    return result


@router.get("/admin/tokens")
def admin_tokens(uow: UoW, _principal: Admin) -> dict[str, Any]:
    return {"items": tokens.list_principals(uow)}


@router.post("/admin/tokens")
def admin_create_token(
    ctx: Ctx, uow: UoW, principal: Admin, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    minted = tokens.create(
        _admin(ctx),
        uow,
        principal=principal.name,
        name=str(body.get("name", "")),
        role=str(body.get("role", "observer")),
        reason=_reason(body),
    )
    uow.commit()
    return {
        "principal": minted.principal.name,
        "role": minted.principal.role.value,
        "token": minted.token,
    }


@router.post("/admin/tokens/{principal_id}/revoke")
def admin_revoke_token(
    principal_id: str,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    result = tokens.revoke(
        _admin(ctx),
        uow,
        principal=principal.name,
        principal_id=principal_id,
        reason=_reason(body),
    )
    uow.commit()
    tokens.after_revoke(_admin(ctx), result)
    return result


@router.post("/admin/tokens/{principal_id}/rename")
def admin_rename_principal(
    principal_id: str,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """ADR 0029: rename a principal. Its tasks and token follow it by id."""
    name = body.get("name")
    if not isinstance(name, str):
        raise RequestValidationError(
            [{"loc": ("body", "name"), "msg": "name must be a string", "type": "value_error"}]
        )
    result = tokens.rename(
        _admin(ctx),
        uow,
        principal=principal.name,
        principal_id=principal_id,
        name=name,
        reason=_reason(body),
    )
    uow.commit()
    return result


@router.get("/admin/audit")
def admin_audit(
    uow: UoW,
    _principal: Admin,
    cursor: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return audit.tail(uow, cursor=cursor, limit=limit)


@router.get("/admin/delivery/auto-merge")
def admin_auto_merge(ctx: Ctx, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return delivery.auto_merge_view(uow)


@router.post("/admin/delivery/auto-merge")
def admin_save_auto_merge(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    result = delivery.save_auto_merge(
        _admin(ctx), uow, principal=principal, enabled=body.get("enabled"), reason=_reason(body)
    )
    uow.commit()
    return result
