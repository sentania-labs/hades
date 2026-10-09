"""Administrative principal tokens, with one-time display and revocation."""

from __future__ import annotations

from typing import Any

from crucible.application.admin.context import AdminContext, admin_event, guard_mutation
from crucible.application.auth import MintedToken, is_device, is_reserved_name, mint_token
from crucible.application.errors import ConflictError
from crucible.application.first_run import FIRST_RUN_PREFIX, discard_after_use, is_first_run
from crucible.domain.entities import Role
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork

# The width of principals.name.
NAME_MAX = 128


def list_principals(uow: UnitOfWork) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "name": item.name,
            "role": item.role.value,
            "created_at": item.created_at.isoformat(),
            "disabled_at": item.disabled_at.isoformat() if item.disabled_at else None,
        }
        for item in uow.principals.list_all()
    ]


def create(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    name: str,
    role: str,
    reason: str | None,
) -> MintedToken:
    reason = guard_mutation(ctx, uow, reason, principal=principal, operation="tokens create")
    if is_first_run(name):
        # Reserved, so that a name with this prefix is always the migration's first-run
        # principal, whose delivered token a sign-in removes (ADR 0016).
        raise ConflictError(f"principal names starting {FIRST_RUN_PREFIX!r} are reserved")
    try:
        selected = Role(role)
        minted = mint_token(uow, ctx.clock, name=name, role=selected)
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    admin_event(
        uow,
        ctx,
        EventKind.PRINCIPAL_CREATED,
        principal=principal,
        reason=reason,
        before=None,
        after={"name": minted.principal.name, "role": minted.principal.role.value},
    )
    return minted


def rename(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    principal_id: str,
    name: str,
    reason: str | None,
) -> dict[str, Any]:
    """Change a principal's name. Tasks, tokens and grants hold the principal's id, so
    they all follow it; events keep the name they were written with, because they are
    history (the operator's decision of 2026-09-29 that the orchestrator's account is
    named `hades`)."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation="tokens rename", reason_required=True
    )
    target = uow.principals.get(principal_id)
    if target is None:
        raise ConflictError("the principal does not exist")
    new = name.strip()
    if not new or new != name or len(new) > NAME_MAX:
        raise ConflictError(
            f"a principal name is 1 to {NAME_MAX} characters with no surrounding spaces"
        )
    if new == target.name:
        raise ConflictError(f"the principal is already named {new!r}")
    if is_reserved_name(new) or is_first_run(new):
        raise ConflictError(f"principal name {new!r} is reserved")
    if is_device(uow, target):
        # hades #576 (U9): a device's principal is named for its device row. A principal
        # that only carries the prefix, from before devices, may be renamed out of it.
        raise ConflictError("a device's principal keeps its name")
    if is_first_run(target.name):
        # The first-run token is delivered and discarded by this name (ADR 0016).
        raise ConflictError("the first-run principal keeps its name")
    if uow.principals.get_by_name(new) is not None:
        raise ConflictError(f"a principal named {new!r} already exists")
    uow.principals.rename(target.id, new)
    admin_event(
        uow,
        ctx,
        EventKind.PRINCIPAL_RENAMED,
        principal=principal,
        reason=reason,
        before={"id": target.id, "name": target.name, "role": target.role.value},
        after={"id": target.id, "name": new, "role": target.role.value},
    )
    return {"id": target.id, "name": new, "previous_name": target.name}


def after_revoke(ctx: AdminContext, result: dict[str, Any]) -> None:
    """ADR 0016: once a revoke of the first-run principal is committed, its token has
    nothing left to open and is not left lying in its Secret or file. Blocking: an
    async caller runs it on a thread."""
    discard_after_use(ctx.first_run, str(result.get("name", "")))


def revoke(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    principal_id: str,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation="tokens revoke", reason_required=True
    )
    target = uow.principals.get(principal_id)
    if target is None or target.disabled_at is not None:
        raise ConflictError("the principal does not exist or is already revoked")
    if target.name == principal:
        raise ConflictError("an administrator cannot revoke the token in use")
    uow.principals.disable(principal_id, ctx.clock.now())
    admin_event(
        uow,
        ctx,
        EventKind.PRINCIPAL_REVOKED,
        principal=principal,
        reason=reason,
        before={"name": target.name, "role": target.role.value, "enabled": True},
        after={"name": target.name, "role": target.role.value, "enabled": False},
    )
    return {"id": target.id, "name": target.name, "revoked": True}
