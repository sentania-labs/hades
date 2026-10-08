"""/health, /ready, /supervisor (04)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import Query, Response
from sqlalchemy import text

from crucible import __version__
from crucible.adapters.api.deps import Ctx, Mutator, Reader, UoW
from crucible.adapters.persistence.migrate import is_current
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.queries import (
    supervisor_health,
    supervisor_view,
    wake_list,
    wake_view,
)
from crucible.application.wakes import ack_wake
from crucible.contracts.api import (
    HealthView,
    ReadyCheck,
    ReadyView,
    SupervisorView,
    WakeAckRequest,
    WakeList,
    WakeView,
)

router = ThreadedAPIRouter()


@router.get("/health", response_model=HealthView)
def health() -> HealthView:
    return HealthView(status="ok", version=__version__)


@router.get("/ready", response_model=ReadyView, responses={503: {"model": ReadyView}})
def ready(ctx: Ctx, response: Response) -> ReadyView:
    """Whether this API process can serve: the database answers and its schema is at
    head. The supervisor's health is reported beside it and does not decide it (hades
    #190): a supervisor in trouble must not take the operator's only window into the
    system offline. `/v1/supervisor` and the admin UI's banner are where it shows."""
    database = ReadyCheck(ok=False, detail="not checked")
    migrations = ReadyCheck(ok=False, detail="not checked")
    supervisor = ReadyCheck(ok=False, detail="not checked")
    try:
        with ctx.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database = ReadyCheck(ok=True, detail="reachable")
        ok, detail = is_current(ctx.engine, ctx.database_url)
        migrations = ReadyCheck(ok=ok, detail=detail)
        with ctx.uow_factory() as uow:
            sup_ok, sup_detail = supervisor_health(uow, ctx.clock.now(), ctx.lease_ttl_seconds)
        supervisor = ReadyCheck(ok=sup_ok, detail=sup_detail)
    except Exception as exc:  # the readiness probe must never raise
        database = ReadyCheck(ok=False, detail=type(exc).__name__)
    view = ReadyView(
        ready=database.ok and migrations.ok,
        database=database,
        migrations=migrations,
        supervisor=supervisor,
    )
    if not view.ready:
        response.status_code = 503
    return view


@router.get("/supervisor", response_model=SupervisorView)
def supervisor_status(ctx: Ctx, uow: UoW, _principal: Reader) -> SupervisorView:
    return supervisor_view(uow, ctx.providers, ctx.clock.now(), ctx.lease_ttl_seconds)


@router.get("/wakes", response_model=WakeList)
def list_wakes(
    uow: UoW,
    principal: Reader,
    since: datetime | None = None,
    include_acked: bool = False,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    cursor: str | None = None,
) -> WakeList:
    """Pending wakes for the caller's principal (04, 17). Poll is the durable fallback.

    Pages by wake id through `cursor`, the opaque form of the last id returned (hades
    #502). `since` is the older time filter: a caller that still sends it gets a valid
    page narrowed to wakes created at or after it."""
    return wake_list(
        uow,
        principal_id=principal.id,
        since=since,
        include_acked=include_acked,
        limit=limit,
        cursor=cursor,
    )


@router.post("/wakes/{wake_id}/ack", response_model=WakeView)
def ack(wake_id: str, body: WakeAckRequest, ctx: Ctx, uow: UoW, principal: Mutator) -> WakeView:
    wake = ack_wake(uow, ctx.clock, principal=principal, wake_id=wake_id, note=body.note)
    uow.commit()
    return wake_view(uow, wake)
