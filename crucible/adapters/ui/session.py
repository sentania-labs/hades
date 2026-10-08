from __future__ import annotations

import asyncio
import hmac
import os
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.render import _base, templates
from crucible.application.auth import authenticate
from crucible.application.errors import (
    ForbiddenError,
)
from crucible.application.first_run import discard_after_use
from crucible.domain.entities import Principal, Role, UiSession
from crucible.ports.repository import UnitOfWork

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)

COOKIE = "crucible_ui"


PREAUTH_COOKIE = "crucible_ui_preauth"


SESSION_MAX_AGE = 12 * 60 * 60


PREAUTH_MAX_AGE = 10 * 60


def _serializer(ctx: Any) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(ctx.ui_signing_key, salt="crucible-ui-session-v1")


def _preauth_serializer(ctx: Any) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(ctx.ui_signing_key, salt="crucible-ui-preauth-v1")


def _session_id(request: Request, ctx: Any) -> str | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        session_id = _serializer(ctx).loads(raw, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return session_id if isinstance(session_id, str) else None


def _session(request: Request, ctx: Any, uow: UnitOfWork) -> tuple[Principal, str] | None:
    session_id = _session_id(request, ctx)
    if session_id is None:
        return None
    session = uow.ui_sessions.get(session_id)
    now = ctx.clock.now()
    if session is None or session.expires_at <= now:
        return None
    principal = uow.principals.get(session.principal_id)
    if principal is None or principal.disabled_at is not None:
        return None
    if session.last_seen_at <= now - timedelta(minutes=1):
        uow.ui_sessions.touch(session.id, now)
        uow.commit()
    return principal, session.csrf


def _require(
    request: Request, ctx: Any, uow: UnitOfWork
) -> tuple[Principal, str] | RedirectResponse:
    found = _session(request, ctx, uow)
    if found is not None:
        return found
    return RedirectResponse(f"/ui/sign-in?next={quote(request.url.path)}", status_code=303)


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", "replace")
    return {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}


def _admin(principal: Principal) -> None:
    if principal.role is not Role.ADMIN:
        raise ForbiddenError("admin role required")


def _csrf(form: dict[str, str], expected: str) -> None:
    supplied = form.get("csrf", "")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise ForbiddenError("the form expired or its CSRF token is invalid")


@router.get("/sign-in", response_class=HTMLResponse)
def sign_in_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    if _session(request, ctx, uow) is not None:
        return RedirectResponse("/ui", status_code=303)
    return _sign_in_form(request, ctx, next_path=request.query_params.get("next", "/ui"))


def _sign_in_form(
    request: Request,
    ctx: Any,
    *,
    next_path: str,
    message: str | None = None,
    status_code: int = 200,
) -> Response:
    csrf = os.urandom(24).hex()
    context = _base(request, None, title="Sign in", active="")
    context.update(
        next=next_path,
        csrf=csrf,
        message=message,
        message_kind="bad",
        first_run_where=ctx.first_run.where() if ctx.first_run is not None else None,
    )
    response = templates.TemplateResponse(
        request=request, name="signin.html", context=context, status_code=status_code
    )
    response.set_cookie(
        PREAUTH_COOKIE,
        _preauth_serializer(ctx).dumps({"csrf": csrf}),
        max_age=PREAUTH_MAX_AGE,
        httponly=True,
        samesite="strict",
        path="/ui/sign-in",
    )
    return response


@router.post("/sign-in")
async def sign_in(request: Request, ctx: Ctx, uow: UoW) -> Response:
    form = await _form(request)
    raw_preauth = request.cookies.get(PREAUTH_COOKIE, "")
    try:
        preauth = _preauth_serializer(ctx).loads(raw_preauth, max_age=PREAUTH_MAX_AGE)
        expected = preauth.get("csrf", "") if isinstance(preauth, dict) else ""
        _csrf(form, expected)
    except (BadSignature, SignatureExpired, ForbiddenError):
        return _sign_in_form(
            request,
            ctx,
            next_path=form.get("next", "/ui"),
            message="The sign-in form expired or its CSRF token is invalid.",
            status_code=403,
        )
    token = form.get("token", "")
    principal = authenticate(uow, token)
    if principal is None:
        return _sign_in_form(
            request,
            ctx,
            next_path=form.get("next", "/ui"),
            message="Token not recognized.",
            status_code=401,
        )
    now = ctx.clock.now()
    csrf = os.urandom(24).hex()
    session_id = os.urandom(32).hex()
    uow.ui_sessions.delete_expired(now)
    uow.ui_sessions.create(
        UiSession(
            id=session_id,
            principal_id=principal.id,
            csrf=csrf,
            created_at=now,
            expires_at=now + timedelta(seconds=SESSION_MAX_AGE),
            last_seen_at=now,
        )
    )
    uow.commit()
    # ADR 0016: discard the first-run token only after the session is committed,
    # so a failed insert or commit leaves the token available for a retry.
    await asyncio.to_thread(discard_after_use, ctx.first_run, principal.name)
    value = _serializer(ctx).dumps(session_id)
    target = form.get("next", "/ui")
    if not target.startswith("/ui") or target.startswith("//"):
        target = "/ui"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE,
        value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        path="/ui",
    )
    response.delete_cookie(PREAUTH_COOKIE, path="/ui/sign-in", httponly=True, samesite="strict")
    return response


@router.post("/sign-out")
async def sign_out(request: Request, ctx: Ctx, uow: UoW) -> RedirectResponse:
    form = await _form(request)
    found = _session(request, ctx, uow)
    if found is not None:
        _csrf(form, found[1])
        session_id = _session_id(request, ctx)
        assert session_id is not None
        uow.ui_sessions.delete(session_id)
        uow.commit()
    response = RedirectResponse("/ui/sign-in", status_code=303)
    response.delete_cookie(COOKIE, path="/ui", httponly=True, samesite="strict")
    return response
