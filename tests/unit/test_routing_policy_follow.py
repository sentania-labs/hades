from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from crucible.adapters.execution.docker import DockerConfig
from crucible.application.admin import routing
from crucible.application.admin.context import AdminContext
from crucible.domain.entities import Principal, Role


class _Versions:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def list_versions(self, name: str) -> list[Any]:
        return [row for row in self.rows if row.name == name]

    def get(self, name: str, version: int) -> Any | None:
        return next((row for row in self.rows if row.name == name and row.version == version), None)


class _Repositories:
    def __init__(self, policy_names: list[str]) -> None:
        self.rows = [SimpleNamespace(name=name, policy_name=name) for name in policy_names]

    def list_all(self) -> list[Any]:
        return self.rows


def _policy(name: str, *, pinned: bool) -> Any:
    return SimpleNamespace(
        name=name,
        version=3,
        retired_at=None,
        document={
            "version": 3,
            "description": "Delivery policy",
            "routing": {"policy": {"name": "route", "version": 7, "pinned": pinned}},
        },
    )


def _context(*, docker: Any | None = None) -> AdminContext:
    return cast(
        AdminContext,
        SimpleNamespace(
            clock=SimpleNamespace(now=lambda: datetime(2026, 9, 30, tzinfo=UTC)),
            proxy_config_path=None,
            proxy_subnet="",
            proxy_hosts=(),
            providers={"docker": docker} if docker is not None else {},
        ),
    )


def _principal() -> Principal:
    return Principal(
        id="p",
        name="operator",
        role=Role.ADMIN,
        created_at=datetime(2026, 9, 30, tzinfo=UTC),
    )


def test_all_delivery_policies_follow_routing_unless_deliberately_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _policy("default-software", pinned=False)
    assigned = _policy("repository-policy", pinned=False)
    pinned = _policy("pinned-policy", pinned=True)
    route = SimpleNamespace(name="route", version=7, retired_at=None, document={"version": 7})
    uow = SimpleNamespace(
        policies=_Versions([active, assigned, pinned]),
        routing_policies=_Versions([route]),
        repositories=_Repositories(["repository-policy", "pinned-policy"]),
    )
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(routing, "put_routing_policy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        routing,
        "put_policy",
        lambda *args, **kwargs: saved.append(deepcopy(kwargs)),
    )
    policy_version, routing_version = routing.publish_routing(
        _context(),
        uow,
        principal=_principal(),
        policy=active,
        routing=route,
        routing_document={"version": 7},
        reason="operator choice",
        note="Routing update",
    )

    assert routing_version == 8
    assert policy_version == 4
    assert {item["name"] for item in saved} == {"default-software", "repository-policy"}
    assert {item["version"] for item in saved} == {4}
    assert all(
        item["document"]["routing"] == {"policy": {"name": "route", "version": 8, "pinned": False}}
        for item in saved
    )


def test_pinned_active_policy_keeps_old_local_endpoint_authorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy("default-software", pinned=True)
    old_document = {
        "version": 7,
        "models": [
            {
                "endpoint": "local",
                "endpoint_url": "http://old-model.internal:8080/v1",
                "enabled": True,
            }
        ],
    }
    route = SimpleNamespace(name="route", version=7, retired_at=None, document=old_document)
    uow = SimpleNamespace(
        policies=_Versions([policy]),
        routing_policies=_Versions([route]),
        repositories=_Repositories([]),
        # No principal to wake: the wake itself is covered in test_issue_437.
        principals=SimpleNamespace(list_all=lambda: []),
    )
    docker = SimpleNamespace(
        config=DockerConfig(
            endpoint="unix:///docker.sock",
            artifact_root="/artifacts",
            proxy_allowlist=("pypi.org",),
        )
    )
    monkeypatch.setattr(routing, "put_routing_policy", lambda *args, **kwargs: None)
    monkeypatch.setattr(routing, "put_policy", lambda *args, **kwargs: None)

    routing.publish_routing(
        _context(docker=docker),
        uow,
        principal=_principal(),
        policy=policy,
        routing=route,
        routing_document={
            "version": 7,
            "models": [
                {
                    "endpoint": "local",
                    "endpoint_url": "http://old-model.internal:8080/v1",
                    "enabled": False,
                }
            ],
        },
        reason="disable local model",
        note="Model availability update",
    )

    assert "old-model.internal:8080" in docker.config.proxy_allowlist
