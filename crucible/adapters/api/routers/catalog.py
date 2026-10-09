"""GET /v1/catalog - read-only catalog of skills and tools."""

from __future__ import annotations

from typing import Any

from crucible.adapters.api.deps import Ctx, Reader, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.admin.catalog import view

router = ThreadedAPIRouter()


@router.get("/catalog")
def catalog_list(_ctx: Ctx, _principal: Reader, uow: UoW) -> dict[str, Any]:
    """Return the catalog. Read-only; observers may read."""
    return view()
