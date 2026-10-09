"""Device tokens (hades #576, U9): a named, long-lived bearer token for one device.

An administrator mints one for a phone or the iOS app, bound to a role (operator unless
another is named). The token is shown once, in the mint response, and is the token of
the device's own principal, `device:<name>`, so it authenticates /v1 like any bearer
token. A browser on the device may exchange it once at `POST /ui/device-sign-in` for an
ordinary server-side UI session (ADR 0030). Every use records the time and the user
agent; the audit carries the mint, the uses and the revocation. Times an operator reads
are local Central beside the RFC 3339 instants."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
    refuse_secret_shaped,
)
from crucible.application.auth import DEVICE_PREFIX, MintedToken, is_device, mint_device_token
from crucible.application.errors import (
    ConflictError,
    ContractValidationError,
    UnauthorizedError,
)
from crucible.application.transitions import record_event
from crucible.domain.entities import Device, Principal, Role
from crucible.domain.events import EventKind
from crucible.domain.time import local_text, rfc3339
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

# The width of devices.name; `device:` plus this fits principals.name.
NAME_MAX = 100
DEFAULT_ROLE = Role.OPERATOR
# How far apart two writes of the last-used time are, at least, for a device whose user
# agent did not change: the same cadence as a UI session's last-seen time.
TOUCH_INTERVAL = timedelta(minutes=1)
# A use is an audit event when it is the device's first, comes from a new user agent,
# or follows an hour without one; the requests in between only move last_used_at.
EVENT_INTERVAL = timedelta(hours=1)
AGENT_MAX = 512
WITHHELD = "[withheld: looked like a credential]"


def _local(value: datetime | None) -> str | None:
    return local_text(value) if value is not None else None


def _instant(value: datetime | None) -> str | None:
    return rfc3339(value) if value is not None else None


def _agent(user_agent: str | None) -> str | None:
    """The user agent as recorded: trimmed, bounded, and never a credential-shaped
    string, because the audit serves it back."""
    if user_agent is None or not user_agent.strip():
        return None
    agent = user_agent.strip()[:AGENT_MAX]
    try:
        refuse_secret_shaped(agent, field="user_agent")
    except ContractValidationError:
        return WITHHELD
    return agent


def _name(name: str) -> str:
    if (
        not name
        or name != name.strip()
        or len(name) > NAME_MAX
        or any(not ch.isprintable() for ch in name)
    ):
        raise ConflictError(
            f"a device name is 1 to {NAME_MAX} printable characters with no surrounding spaces"
        )
    try:
        refuse_secret_shaped(name, field="name")
    except ContractValidationError as exc:
        raise ConflictError("a device name may not look like a credential") from exc
    return name


def view(device: Device, principal: Principal | None) -> dict[str, Any]:
    """One device as the API lists it. Never the token: only its hash exists."""
    revoked_at = device.revoked_at or (principal.disabled_at if principal else None)
    return {
        "id": device.principal_id,
        "name": device.name,
        "principal": principal.name if principal else f"{DEVICE_PREFIX}{device.name}",
        "role": principal.role.value if principal else None,
        "state": "revoked" if revoked_at is not None else "active",
        "created_by": device.created_by,
        "created_at": rfc3339(device.created_at),
        "created_local": local_text(device.created_at),
        "last_used_at": _instant(device.last_used_at),
        "last_used_local": _local(device.last_used_at),
        "last_user_agent": device.last_user_agent,
        "session_exchanged": device.exchanged_at is not None,
        "exchanged_at": _instant(device.exchanged_at),
        "exchanged_local": _local(device.exchanged_at),
        "revoked_at": _instant(revoked_at),
        "revoked_local": _local(revoked_at),
        "revoked_by": device.revoked_by,
    }


def list_devices(uow: UnitOfWork) -> list[dict[str, Any]]:
    return [view(item, uow.principals.get(item.principal_id)) for item in uow.devices.list_all()]


def mint(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    name: str,
    role: str | None,
    reason: str | None,
) -> tuple[MintedToken, dict[str, Any]]:
    """A new device and its token. The token is returned here and nowhere else."""
    reason = guard_mutation(ctx, uow, reason, principal=principal, operation="devices mint")
    name = _name(name)
    try:
        selected = Role(role) if role else DEFAULT_ROLE
    except ValueError as exc:
        roles = ", ".join(item.value for item in Role)
        raise ConflictError(f"role {role!r} is not one of {roles}") from exc
    if uow.devices.get_by_name(name) is not None:
        raise ConflictError(f"a device named {name!r} already exists")
    try:
        minted = mint_device_token(uow, ctx.clock, device=name, role=selected)
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    device = Device(
        principal_id=minted.principal.id,
        name=name,
        created_by=principal,
        created_at=ctx.clock.now(),
    )
    uow.devices.add(device)
    admin_event(
        uow,
        ctx,
        EventKind.DEVICE_TOKEN_MINTED,
        principal=principal,
        reason=reason,
        before=None,
        after={"id": device.principal_id, "device": name, "role": selected.value},
        local_time=local_text(device.created_at),
    )
    return minted, view(device, minted.principal)


def revoke(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    device_id: str,
    reason: str | None,
) -> dict[str, Any]:
    """Disable the device's principal, which ends its token and every UI session it
    opened (a session of a disabled principal is not a session, ADR 0030)."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation="devices revoke", reason_required=True
    )
    device = uow.devices.get(device_id)
    target = uow.principals.get(device_id) if device is not None else None
    if device is None or target is None:
        raise ConflictError("the device does not exist")
    if device.revoked_at is not None or target.disabled_at is not None:
        raise ConflictError(f"the device {device.name!r} is already revoked")
    if target.name == principal:
        raise ConflictError("a device cannot revoke its own token")
    now = ctx.clock.now()
    uow.principals.disable(device_id, now)
    uow.devices.revoke(device_id, now, principal)
    admin_event(
        uow,
        ctx,
        EventKind.DEVICE_TOKEN_REVOKED,
        principal=principal,
        reason=reason,
        before={"id": device_id, "device": device.name, "enabled": True},
        after={"id": device_id, "device": device.name, "enabled": False},
        local_time=local_text(now),
    )
    revoked = uow.devices.get(device_id) or device
    return {**view(revoked, uow.principals.get(device_id)), "revoked": True}


def _use_event(
    uow: UnitOfWork,
    clock: Clock,
    principal: Principal,
    device: Device,
    *,
    at: datetime,
    agent: str | None,
    via: str,
) -> None:
    record_event(
        uow,
        clock,
        EventKind.DEVICE_TOKEN_USED,
        principal=principal.name,
        payload={
            "id": device.principal_id,
            "device": device.name,
            "via": via,
            "user_agent": agent,
            "first_use": device.last_used_at is None,
            "local_time": local_text(at),
        },
    )


def record_use(
    uow: UnitOfWork, clock: Clock, principal: Principal, *, user_agent: str | None
) -> bool:
    """A device token authenticated a /v1 request: move its last-used time and user
    agent, and audit the use when it is the first, from a new user agent, or after an
    hour without one. True when something was written and the caller should commit."""
    if not is_device(principal):
        return False
    device = uow.devices.get(principal.id)
    if device is None or device.revoked_at is not None:
        return False
    now = clock.now()
    agent = _agent(user_agent)
    last = device.last_used_at
    changed = agent != device.last_user_agent
    if last is not None and not changed and now - last < TOUCH_INTERVAL:
        return False
    uow.devices.record_use(device.principal_id, now, agent)
    if last is None or changed or now - last >= EVENT_INTERVAL:
        _use_event(uow, clock, principal, device, at=now, agent=agent, via="api")
    return True


def exchange(
    uow: UnitOfWork, clock: Clock, principal: Principal, *, user_agent: str | None
) -> Device:
    """Take the device token's one exchange for a UI session, or refuse. The caller
    creates the session in the same transaction and commits both."""
    device = uow.devices.get(principal.id) if is_device(principal) else None
    if device is None or device.revoked_at is not None:
        raise UnauthorizedError("not a device token")
    now = clock.now()
    agent = _agent(user_agent)
    if not uow.devices.mark_exchanged(device.principal_id, now, agent):
        raise ConflictError(
            "this device token was already exchanged for a session; "
            "an administrator mints a new one"
        )
    _use_event(uow, clock, principal, device, at=now, agent=agent, via="ui_session")
    return device
