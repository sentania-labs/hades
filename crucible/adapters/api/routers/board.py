"""The public operator board resource and its click actions."""

from __future__ import annotations

from typing import Annotated

from fastapi import Body, Query

from crucible.adapters.api.deps import Ctx, Reader, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.board_actions import CorrectionDeps, apply_move
from crucible.application.board_resource import board_resource
from crucible.application.proposals import reject_proposal
from crucible.contracts.board import BoardResource

router = ThreadedAPIRouter(tags=["board"])


@router.get(
    "/board",
    response_model=BoardResource,
    summary="Read the seven-lane operator board",
    description=(
        "Returns lane counts, needs_me, cards, and the HTTP action available on each "
        "card. Use lanes to load collapsed Wins and Graveyard cards on demand."
    ),
)
def get_board(
    ctx: Ctx,
    uow: UoW,
    principal: Reader,
    lanes: Annotated[list[str] | None, Query()] = None,
) -> BoardResource:
    selected = frozenset(lanes) if lanes else None
    return BoardResource.model_validate(
        board_resource(uow, ctx.clock.now(), principal, cards_for=selected)
    )


@router.post("/board/{task_id}/actions/{move}")
def act_on_card(
    task_id: str,
    move: str,
    ctx: Ctx,
    uow: UoW,
    principal: Reader,
    body: Annotated[dict[str, str | None] | None, Body()] = None,
) -> dict[str, str]:
    if move == "decline":
        note = str((body or {}).get("note") or "").strip()
        task = reject_proposal(
            uow,
            ctx.clock,
            principal=principal,
            task_id=task_id,
            reason=note or f"Declined by {principal.name}",
        )
        uow.commit()
        return {"task_id": task_id, "action": move, "message": f"Declined {task.external_id}."}
    deps = CorrectionDeps(
        harnesses=ctx.harnesses,
        harness_gates=ctx.harness_gates,
        credential_sources=ctx.credential_sources,
        secret_providers=ctx.secret_providers,
        wired_providers=frozenset(provider.name for provider in ctx.providers),
    )
    result = apply_move(
        uow,
        ctx.clock,
        principal=principal,
        task_id=task_id,
        move=move,
        note_text=str((body or {}).get("note") or f"{move} by {principal.name}"),
        deps=deps,
    )
    uow.commit()
    return {"task_id": task_id, "action": move, "message": result.message}
