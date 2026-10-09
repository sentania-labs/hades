"""crucible#169: the first-run Set up steps. Five numbered steps in the order an operator
does them (GitHub App, repository, harness login, routing, first task), each done or not
done from live state and linking to the page where it is done. The navigation's Set up
entry counts the undone ones and goes away once every one is done (hades #576 U5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.ui.pages.setup import checklist
from crucible.application.admin import setup, status

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _harness(name: str, **values: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "name": name,
        "last_validated_at": None,
        "last_successful_launch_at": None,
        "last_auth_failure_at": None,
    }
    fields.update(values)
    return SimpleNamespace(**fields)


def _uow(
    *,
    repositories: list[Any] | None = None,
    harnesses: list[Any] | None = None,
    tasks: list[Any] | None = None,
) -> Any:
    return SimpleNamespace(
        repositories=SimpleNamespace(list_all=lambda: repositories or []),
        harnesses=SimpleNamespace(list_all=lambda: harnesses or []),
        tasks=SimpleNamespace(search=lambda **_filters: tasks or []),
    )


def _ctx(*, github: Any = None, fixtures: frozenset[str] = frozenset()) -> Any:
    registry = {name: SimpleNamespace(test_fixture=True) for name in fixtures}
    return SimpleNamespace(github=github, harnesses=registry)


@pytest.fixture
def routing(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    enabled: set[str] = set()
    monkeypatch.setattr(status, "_enabled_models", lambda _uow: enabled)
    return enabled


def _done(ctx: Any, uow: Any) -> dict[str, bool]:
    return {step["key"]: step["done"] for step in setup.setup_steps(ctx, uow)}


def test_five_steps_in_order_each_with_its_page(routing: set[str]) -> None:
    steps = setup.setup_steps(_ctx(), _uow())
    assert [(s["number"], s["label"], s["link"]) for s in steps] == [
        (1, "GitHub App", "/ui/github"),
        (2, "Repository", "/ui/repositories"),
        (3, "Harness login", "/ui/credentials"),
        (4, "Routing", "/ui/routing"),
        (5, "First task", "/ui/room"),
    ]
    long_dash = chr(0x2014)
    assert all(s["detail"] and long_dash not in s["detail"] for s in steps)


def test_a_fresh_deployment_has_every_step_to_do(routing: set[str]) -> None:
    steps = setup.setup_steps(_ctx(), _uow())
    assert not any(s["done"] for s in steps)
    assert setup.undone_count(steps) == 5


def _client(*, configured: bool) -> Any:
    return SimpleNamespace(configured=lambda: configured)


def test_each_step_is_done_from_live_state(routing: set[str]) -> None:
    routing.add("codex")
    uow = _uow(
        repositories=[SimpleNamespace(installation_id=7)],
        harnesses=[_harness("codex", last_validated_at=NOW)],
        tasks=[object()],
    )
    ctx = _ctx(github=_client(configured=True))
    assert _done(ctx, uow) == dict.fromkeys(
        ("github_app", "repository", "harness_login", "routing", "first_task"), True
    )
    assert setup.undone_count(setup.setup_steps(ctx, uow)) == 0


def test_the_github_app_is_done_only_while_a_credential_is_configured(
    routing: set[str],
) -> None:
    assert _done(_ctx(github=_client(configured=True)), _uow())["github_app"] is True
    # Kubernetes wires a client for the service's Secret before the operator fills it.
    assert _done(_ctx(github=_client(configured=False)), _uow())["github_app"] is False
    # An installation id registered earlier does not stand in for a removed credential.
    stale = _uow(repositories=[SimpleNamespace(installation_id=7)])
    assert _done(_ctx(github=_client(configured=False)), stale)["github_app"] is False
    assert _done(_ctx(), stale)["github_app"] is False
    assert _done(_ctx(), stale)["repository"] is True


def test_a_harness_login_counts_until_a_later_refusal(routing: set[str]) -> None:
    later = NOW + timedelta(minutes=5)
    refused = _uow(harnesses=[_harness("codex", last_validated_at=NOW, last_auth_failure_at=later)])
    assert _done(_ctx(), refused)["harness_login"] is False
    relaunched = _uow(
        harnesses=[
            _harness(
                "codex",
                last_validated_at=NOW,
                last_auth_failure_at=later,
                last_successful_launch_at=later + timedelta(minutes=1),
            )
        ]
    )
    assert _done(_ctx(), relaunched)["harness_login"] is True


def test_a_test_fixture_harness_never_counts_as_a_login(routing: set[str]) -> None:
    uow = _uow(harnesses=[_harness("script-harness", last_validated_at=NOW)])
    assert _done(_ctx(fixtures=frozenset({"script-harness"})), uow)["harness_login"] is False


def test_the_checklist_marks_the_first_undone_step_as_next(routing: set[str]) -> None:
    uow = _uow(repositories=[SimpleNamespace(installation_id=7)])
    marked = checklist(setup.setup_steps(_ctx(github=_client(configured=True)), uow))
    assert [s["key"] for s in marked if s["next"]] == ["harness_login"]
    routing.add("codex")
    done = _uow(
        repositories=[SimpleNamespace(installation_id=7)],
        harnesses=[_harness("codex", last_validated_at=NOW)],
        tasks=[object()],
    )
    ready = _ctx(github=_client(configured=True))
    assert not any(s["next"] for s in checklist(setup.setup_steps(ready, done)))
