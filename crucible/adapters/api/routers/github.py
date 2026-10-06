"""`POST /v1/github/webhook` (04, 23).

The optional accelerator. Off by default: polling is the complete observation path and
this endpoint only makes the next poll happen sooner. It uses HMAC verification instead
of a bearer token, and it is the only endpoint that does.

An unsigned or mismatched delivery is rejected and counted, and nothing of it is stored,
including its claimed delivery id. An accepted one is normalized in memory to the fields
Crucible uses, its user-controlled text scanned and redacted, and stored with a SHA-256
of the original body. The raw body is never persisted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Header, Request

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.github.webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    HANDLED_EVENTS,
    SIGNATURE_HEADER,
    SignatureError,
    normalize_delivery,
    verify_signature,
)
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.errors import ForbiddenError, UnauthorizedError
from crucible.application.github_ingress import (
    record_rejection,
    record_unhandled,
    store_delivery,
)
from crucible.contracts.api import WebhookAck

router = ThreadedAPIRouter(prefix="/github")

# GitHub's own limit is 25 MiB; nothing Crucible reads from a delivery is anywhere near
# it. The body is read before the signature can be checked (the HMAC is over the raw
# body), so an unauthenticated caller decides how much this endpoint reads unless the
# endpoint decides first.
MAX_BODY_BYTES = 1024 * 1024


def _secret(ctx: Ctx) -> str:
    path = ctx.github_webhook_secret_path
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@router.post("/webhook", response_model=WebhookAck)
async def webhook(
    request: Request,
    ctx: Ctx,
    uow: UoW,
    x_github_event: Annotated[str | None, Header(alias=EVENT_HEADER)] = None,
    x_github_delivery: Annotated[str | None, Header(alias=DELIVERY_HEADER)] = None,
    x_hub_signature_256: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
) -> WebhookAck:
    if not ctx.github_webhook_enabled:
        raise ForbiddenError(
            "the GitHub webhook receiver is off in this deployment; polling is the "
            "complete observation path and this endpoint is only an accelerator (23)"
        )
    # Nothing about an unverified delivery is trusted, and that includes the event name
    # it claims: the rejection records it only when it is one Crucible handles, so an
    # unauthenticated caller cannot choose what goes into an event row (23 stores
    # nothing of a rejected delivery).
    claimed = x_github_event or ""
    event = claimed if claimed in HANDLED_EVENTS else "unrecognized"
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        record_rejection(uow, ctx.clock, event=event, reason="body over the size limit")
        uow.commit()
        raise UnauthorizedError("the delivery body is over the size limit")
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            record_rejection(uow, ctx.clock, event=event, reason="body over the size limit")
            uow.commit()
            raise UnauthorizedError("the delivery body is over the size limit")
    try:
        verify_signature(_secret(ctx), raw, x_hub_signature_256)
    except SignatureError as exc:
        record_rejection(uow, ctx.clock, event=event, reason=str(exc))
        uow.commit()
        raise UnauthorizedError(str(exc)) from exc
    # Verified from here: the headers are GitHub's, so the claimed event name is usable.
    event = claimed
    delivery_id = (x_github_delivery or "").strip()[:64]
    if not delivery_id:
        record_rejection(uow, ctx.clock, event=event, reason="no delivery id")
        uow.commit()
        raise UnauthorizedError("the delivery carries no X-GitHub-Delivery id")
    normalized = normalize_delivery(delivery_id=delivery_id, event=event, raw_body=raw)
    if normalized is None:
        accepted = record_unhandled(uow, ctx.clock, delivery_id=delivery_id, event=event)
    else:
        accepted = store_delivery(
            uow,
            ctx.clock,
            delivery_id=normalized.delivery_id,
            event=normalized.event,
            action=normalized.action,
            repository=normalized.repository,
            body_sha256=normalized.body_sha256,
            normalized=normalized.normalized,
            triggers_reaction_poll=normalized.triggers_reaction_poll,
        )
    uow.commit()
    return WebhookAck(
        delivery_id=accepted.delivery_id,
        accepted=accepted.accepted,
        duplicate=accepted.duplicate,
        detail=accepted.detail,
    )
