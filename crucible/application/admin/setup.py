"""hades #169: the first-run Set up steps, in the order an operator does them.

Each step is done or not done from live state, and names the page where it is done.
Every read is a database row, a field already on the context, or the GitHub App
credential store the delivery path itself reads (ADR 0017); nothing here reads a
registry or a provider. The Set up page and the navigation read this one list, so they
never disagree.
"""

from __future__ import annotations

from typing import Any

from crucible.application.admin import status
from crucible.ports.repository import UnitOfWork


def _github_app_done(ctx: Any) -> bool:
    """An App credential is in place right now. A wired client alone is not enough: on
    Kubernetes the client is wired for the service's Secret before the operator fills
    it, and its `configured()` is the check delivery and the GitHub page already use. A
    repository's installation id is not evidence either, since it outlives the
    credential it was registered under."""
    client = getattr(ctx, "github", None)
    if client is None:
        return False
    configured = getattr(client, "configured", None)
    return not callable(configured) or bool(configured())


def _harness_login_done(ctx: Any, uow: UnitOfWork) -> bool:
    """A real harness has a credential that passed its last check or launched a task,
    and no refusal since. Test fixtures never count (crucible#123)."""
    for state in uow.harnesses.list_all():
        if ctx is not None and status.is_test_fixture(ctx, state.name):
            continue
        proven = [
            moment
            for moment in (state.last_validated_at, state.last_successful_launch_at)
            if moment is not None
        ]
        if not proven:
            continue
        refused = state.last_auth_failure_at
        if refused is None or refused <= max(proven):
            return True
    return False


def _routing_done(uow: UnitOfWork) -> bool:
    """The routing policy in force enables at least one model."""
    return bool(status._enabled_models(uow))


def _first_task_done(uow: UnitOfWork) -> bool:
    """Any task at all, read as one row rather than a count by state."""
    return bool(
        uow.tasks.search(
            state=None,
            project=None,
            repository_id=None,
            external_id=None,
            updated_since=None,
            after_id=None,
            limit=1,
        )
    )


def setup_steps(ctx: Any, uow: UnitOfWork) -> list[dict[str, Any]]:
    """The five first-run steps, numbered, each with `done`, the page that does it and
    one sentence of what to do there. `ctx` is the administrative context, or None
    where the deployment has no administrative surface."""
    return [
        {
            "number": 1,
            "key": "github_app",
            "label": "GitHub App",
            "link": "/ui/github",
            "done": _github_app_done(ctx),
            "detail": "Create or connect the GitHub App that opens pull requests.",
        },
        {
            "number": 2,
            "key": "repository",
            "label": "Repository",
            "link": "/ui/repositories",
            "done": bool(uow.repositories.list_all()),
            "detail": "Register a repository the App is installed on.",
        },
        {
            "number": 3,
            "key": "harness_login",
            "label": "Harness login",
            "link": "/ui/credentials",
            "done": _harness_login_done(ctx, uow),
            "detail": "Log in a harness, or set the local gateway key, and validate it.",
        },
        {
            "number": 4,
            "key": "routing",
            "label": "Routing",
            "link": "/ui/routing",
            "done": _routing_done(uow),
            "detail": "Enable at least one model in the routing policy in force.",
        },
        {
            "number": 5,
            "key": "first_task",
            "label": "First task",
            "link": "/ui/room",
            "done": _first_task_done(uow),
            "detail": "Ask Hades for a first task, then watch it on the Board.",
        },
    ]


def undone_count(steps: list[dict[str, Any]]) -> int:
    return sum(1 for step in steps if not step["done"])
