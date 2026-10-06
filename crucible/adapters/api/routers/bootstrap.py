"""/v1/import/bootstrap (04, 15), admin role only. The body of the submit is the
`BootstrapExportV1` bundle itself, exactly as `foundry-ledger export --format crucible`
writes it, so the reason and the owning principal travel as query parameters. Every
handler is a thin call into crucible/application/admin/bootstrap.py; the CLI calls the
same functions in process."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Body, Query, Response

from crucible.adapters.api.deps import Admin, Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.admin import bootstrap
from crucible.application.admin.context import AdminContext
from crucible.application.errors import ConflictError

router = ThreadedAPIRouter()


def _admin(ctx: Ctx) -> AdminContext:
    if ctx.admin is None:
        raise ConflictError("the administrative surface is not configured")
    return ctx.admin


def _reason(value: str | None) -> str | None:
    """Only a non-empty string is a reason (25)."""
    if isinstance(value, str) and value.strip():
        return value
    return None


@router.post("/import/bootstrap")
def submit_bootstrap(
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    response: Response,
    bundle: Annotated[Any, Body()],
    reason: Annotated[str | None, Query()] = None,
    owner: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """15 steps 1 to 4: validate the bundle in full, write every record in one
    transaction under an import in state `verified`, and return the verification
    report. A bundle already imported (same content hash) is replayed with 200; a new
    import is 201; any validation failure is 422 with every problem and nothing stored.
    `owner` names the principal the tasks belong to (the orchestrator's, normally);
    it defaults to the caller."""
    report, created = bootstrap.submit(
        _admin(ctx),
        uow,
        principal=principal.name,
        bundle=bundle,
        reason=_reason(reason),
        owner=owner,
    )
    uow.commit()
    response.status_code = 201 if created else 200
    return report


@router.get("/import/bootstrap")
def list_bootstrap_imports(uow: UoW, _principal: Admin) -> dict[str, Any]:
    return {"items": bootstrap.list_imports(uow)}


@router.get("/import/bootstrap/{import_id}")
def show_bootstrap_import(import_id: str, uow: UoW, _principal: Admin) -> dict[str, Any]:
    return bootstrap.show(uow, import_id)


@router.post("/import/bootstrap/{import_id}/commit")
def commit_bootstrap_import(
    import_id: str,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """15 step 5: the import becomes authoritative and a handoff event lands on every
    imported task. 409 when the import is not `verified` or another holds authority."""
    report = bootstrap.commit(
        _admin(ctx),
        uow,
        principal=principal.name,
        import_id=import_id,
        reason=_reason(body.get("reason")),
    )
    uow.commit()
    return report


@router.post("/import/bootstrap/{import_id}/discard")
def discard_bootstrap_import(
    import_id: str,
    ctx: Ctx,
    uow: UoW,
    principal: Admin,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """ADR 0029: withdraw a verified import. Its tasks are retired, never deleted, and
    their external ids are free for a fresh import. 409 unless the import is `verified`."""
    report = bootstrap.discard(
        _admin(ctx),
        uow,
        principal=principal.name,
        import_id=import_id,
        reason=_reason(body.get("reason")),
    )
    uow.commit()
    return report
