from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from crucible.adapters.ui.pages import credentials as page
from crucible.application.admin import credentials as credentials_service
from crucible.domain.entities import Policy, Principal, Role


@pytest.mark.asyncio
async def test_credentials_page_renders_running_workers_against_active_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = Principal("01ADMIN00000000000000000", "admin", Role.ADMIN, datetime.now(UTC))
    policy = Policy(
        "default-software",
        7,
        {"concurrency": {"per_harness": {"codex": 3}}},
        datetime.now(UTC),
    )
    uow = SimpleNamespace(
        attempts=SimpleNamespace(
            list_in_states=lambda _states: [SimpleNamespace(execution_id="execution-1")]
        ),
        executions=SimpleNamespace(get=lambda _execution_id: SimpleNamespace(harness="codex")),
        events=SimpleNamespace(list_global=lambda **_kwargs: []),
        supervisor_status=SimpleNamespace(get=lambda: SimpleNamespace(refresh_request_cursor=None)),
        policies=SimpleNamespace(list_versions=lambda _name: [policy]),
    )
    ctx = SimpleNamespace(
        admin=SimpleNamespace(harnesses=SimpleNamespace(names=lambda: ("codex",))),
    )
    captured: dict[str, Any] = {}

    monkeypatch.setattr(page, "_require", lambda *_args: (principal, "csrf"))
    monkeypatch.setattr(credentials_service, "read_secrets", _empty_secrets)
    monkeypatch.setattr(credentials_service, "secret_store", lambda _ctx: None)
    monkeypatch.setattr(
        credentials_service,
        "state_view",
        lambda *_args: {
            "state": "validated",
            "mount_mode": "renewer",
            "last_launch_outcome": None,
            "session_compatibility": "shared",
        },
    )

    def render(*_args: object, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(page, "_page", render)
    request = Request({"type": "http", "method": "GET", "path": "/ui/credentials"})

    await page.credentials_page(request, ctx, uow)  # type: ignore[arg-type]

    row = captured["sections"][0]["rows"][0]
    assert row[3] == "renewer"
    assert row[4] == "1 of 3"


async def _empty_secrets(*_args: object, **_kwargs: object) -> dict[str, Any]:
    return {}
