"""The /v1 application."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from crucible import __version__
from crucible.adapters.api.deps import AppContext
from crucible.adapters.api.problems import install_problem_handlers
from crucible.adapters.api.routers import (
    admin,
    board,
    bootstrap,
    github,
    harnesses,
    policies,
    records,
    supervision,
    tasks,
)
from crucible.adapters.ui.render import static as ui_static
from crucible.adapters.ui.router import router as ui_router

API_PREFIX = "/v1"


def create_app(ctx: AppContext) -> FastAPI:
    app = FastAPI(
        title="Hades",
        version=__version__,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
    )
    app.state.ctx = ctx

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/board", status_code=303)

    install_problem_handlers(app)
    app.mount("/ui/static", ui_static, name="ui-static")
    app.include_router(ui_router)
    app.include_router(supervision.router, prefix=API_PREFIX)
    app.include_router(board.router, prefix=API_PREFIX)
    app.include_router(tasks.router, prefix=API_PREFIX)
    app.include_router(records.router, prefix=API_PREFIX)
    app.include_router(policies.router, prefix=API_PREFIX)
    app.include_router(github.router, prefix=API_PREFIX)
    app.include_router(harnesses.router, prefix=API_PREFIX)
    app.include_router(admin.router, prefix=API_PREFIX)
    app.include_router(bootstrap.router, prefix=API_PREFIX)
    return app
