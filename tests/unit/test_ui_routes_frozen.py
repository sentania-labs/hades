"""UI routes and actions frozen from origin/main before FDY-0167."""

from fastapi.routing import APIRoute

from crucible.adapters.ui.actions import handlers
from crucible.adapters.ui.router import router

FROZEN_ROUTES = [
    ("GET", "/ui"),
    ("GET", "/ui/artifacts/{artifact_id}/content"),
    ("GET", "/ui/audit"),
    ("GET", "/ui/board"),
    ("GET", "/ui/bootstrap"),
    ("GET", "/ui/bootstrap/{import_id}"),
    ("GET", "/ui/credentials"),
    ("GET", "/ui/credentials/{harness}/login"),
    ("GET", "/ui/gateway"),
    ("GET", "/ui/github"),
    ("GET", "/ui/github/callback"),
    ("GET", "/ui/github/installed"),
    ("GET", "/ui/harnesses"),
    ("GET", "/ui/images"),
    ("GET", "/ui/repositories"),
    ("GET", "/ui/retention"),
    ("GET", "/ui/routing"),
    ("GET", "/ui/routing/models"),
    ("GET", "/ui/routing/tiers"),
    ("GET", "/ui/settings"),
    ("GET", "/ui/sign-in"),
    ("GET", "/ui/tasks"),
    ("GET", "/ui/tasks/{task_id}"),
    ("GET", "/ui/tokens"),
    ("GET", "/ui/usage"),
    ("GET", "/ui/wakes"),
    ("GET", "/ui/workers"),
    ("GET", "/ui/workers/{attempt_id}/logs"),
    ("POST", "/ui/actions/{action}"),
    ("POST", "/ui/sign-in"),
    ("POST", "/ui/sign-out"),
    # hades #424: the operator's answers to proposed tasks, one at a time and as a batch.
    ("POST", "/ui/tasks/proposals/approve"),
    ("POST", "/ui/tasks/{task_id}/decisions"),
    ("POST", "/ui/tasks/{task_id}/proposal"),
]

FROZEN_ACTIONS = [
    "auto-merge",
    "status-cache",
    "bootstrap-commit",
    "bootstrap-discard",
    "command-timeout",
    "credential",
    "gate-classes",
    "gateway-models",
    "gateway-save",
    "gateway-test",
    "github-add-repository",
    "github-check",
    "github-create-app",
    "github-external-url",
    "harness",
    "harness-test",
    "hermes-limits",
    "image-change",
    "kubernetes-egress",
    "kubernetes-timeouts",
    "login-cancel",
    "login-code",
    "login-finish",
    "login-start",
    "policy-upload",
    "repository-register",
    # hades #265: the Repositories page's picker registers its ticked repositories together.
    "repository-register-batch",
    "repository-remove",
    "routing-clear",
    "routing-model",
    "routing-preference",
    "routing-tier",
    "routing-upload",
    "token-create",
    "token-rename",
    "token-revoke",
]


def test_ui_routes_frozen() -> None:
    assert (
        sorted(
            (method, route.path)
            for route in router.routes
            if isinstance(route, APIRoute)
            for method in route.methods or set()
        )
        == FROZEN_ROUTES
    )


def test_ui_actions_frozen() -> None:
    assert set(handlers) == set(FROZEN_ACTIONS)
