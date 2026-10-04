"""Credentials page: the Codex renewer mode, workers against the cap, refresh now (PR 327)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from crucible.adapters.ui.pages import credentials as credentials_page_module
from crucible.application.admin import credentials
from crucible.domain.entities import (
    Role,
)
from crucible.domain.events import EventKind
from tests.unit.admin_ui_fixtures import (
    NOW,
    request,
)


@pytest.mark.parametrize(
    "cursor,pending,request_label",
    [(0, True, "Pending"), (2, False, "Handled"), (0, False, "None")],
)
async def test_t_auth_6_credentials_page_renewer_health_and_reason(
    monkeypatch: pytest.MonkeyPatch,
    cursor: int,
    pending: bool,
    request_label: str,
) -> None:
    principal = SimpleNamespace(name="admin", role=Role.ADMIN)
    monkeypatch.setattr(credentials_page_module, "_require", lambda *_: (principal, "csrf"))
    monkeypatch.setattr(credentials, "read_secrets", AsyncMock(return_value={}))
    monkeypatch.setattr(credentials, "secret_store", lambda *_: object())
    monkeypatch.setattr(
        credentials,
        "state_view",
        lambda *_: {
            "state": "validated",
            "mount_mode": "renewer",
            "last_launch_outcome": None,
        },
    )
    monkeypatch.setattr(
        credentials_page_module,
        "active_policy",
        lambda *_: SimpleNamespace(
            document={"concurrency": {"per_harness": {"codex": 2}}},
        ),
    )
    uow = Mock()
    uow.attempts.list_in_states.return_value = [SimpleNamespace(execution_id="running")]
    uow.executions.get.return_value = SimpleNamespace(harness="codex")
    uow.supervisor_status.get.return_value.refresh_request_cursor = cursor
    refresh_events = [
        SimpleNamespace(
            kind=EventKind.CREDENTIAL_REFRESH_FAILED.value,
            ts=NOW,
            payload={"harness": "codex", "result": "network failure"},
        ),
        SimpleNamespace(
            kind=EventKind.CREDENTIAL_REFRESHED.value,
            ts=NOW,
            payload={"harness": "codex", "result": "refreshed"},
        ),
    ]
    uow.events.list_global.side_effect = [refresh_events, [object()] if pending else []]
    ctx: Any = SimpleNamespace(
        admin=SimpleNamespace(harnesses=SimpleNamespace(names=lambda: ["codex"]))
    )
    req = request("/ui/credentials")
    req.scope["query_string"] = b""
    req.scope["app"] = SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace()))
    response = await credentials_page_module.credentials_page(req, ctx, uow)
    rendered = bytes(response.body).decode()
    for label in (
        "Mode",
        "renewer",
        "Workers / cap",
        "1 of 2",
        "Last refresh",
        "Refresh result",
        "refreshed",
        "Failures (24h)",
        "Refresh now",
        "Refresh request",
        request_label,
        "Validate",
        "Probe",
        "Log in",
    ):
        assert label in rendered
    assert 'name="reason"' in rendered
    assert 'name="verb" value="refresh"' in rendered
    assert "&gt;1&lt;" not in rendered
    assert ">1</td>" in rendered
