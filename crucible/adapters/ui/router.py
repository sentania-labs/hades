"""Aggregate the HTML administration routes under /ui."""

from fastapi import APIRouter

from crucible.adapters.ui import actions, session
from crucible.adapters.ui.pages import (
    audit,
    board,
    bootstrap,
    credentials,
    dashboard,
    gateway,
    github,
    harnesses,
    images,
    proposals,
    repositories,
    retention,
    routing,
    routing_models,
    settings,
    tasks,
    tokens,
    wakes,
    workers,
)

router = APIRouter(prefix="/ui", include_in_schema=False)

# Keep the prefixed APIRoutes flat, including the dashboard at exactly /ui.
router.routes.extend(session.router.routes)
router.routes.extend(dashboard.router.routes)
router.routes.extend(board.router.routes)
router.routes.extend(harnesses.router.routes)
router.routes.extend(credentials.router.routes)
router.routes.extend(gateway.router.routes)
router.routes.extend(images.router.routes)
router.routes.extend(routing.router.routes)
router.routes.extend(routing_models.router.routes)
router.routes.extend(repositories.router.routes)
router.routes.extend(tokens.router.routes)
router.routes.extend(github.router.routes)
router.routes.extend(workers.router.routes)
router.routes.extend(proposals.router.routes)
router.routes.extend(tasks.router.routes)
router.routes.extend(wakes.router.routes)
router.routes.extend(retention.router.routes)
router.routes.extend(audit.router.routes)
router.routes.extend(bootstrap.router.routes)
router.routes.extend(settings.router.routes)
router.routes.extend(actions.router.routes)
