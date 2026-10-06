"""Hades #437: a routing publish names what it overrides and wakes the orchestrator.

On 2026-10-04 a save on the Local gateway page re-enabled local models for every
unpinned project and nobody noticed for a day. A publish now carries its delta: a model
enabled or disabled, or a pool cap changed, needs a reason, raises one `routing_changed`
wake listing the change and the projects that follow routing unpinned, and the Routing
and Local gateway pages show it.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from crucible.adapters.ui.pages import gateway as gateway_page
from crucible.adapters.ui.pages import routing as routing_page
from crucible.application.admin import credentials, gateway, routing
from crucible.application.admin.context import AdminContext
from crucible.application.errors import ContractValidationError
from crucible.application.wakes import wake_document
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import Event, Principal, Role, Wake
from crucible.domain.events import EventKind

NOW = datetime(2026, 10, 4, 15, tzinfo=UTC)
LOCAL_URL = "http://gateway.internal:4000/v1"


def _routing_document(*, local_enabled: bool = False, cap: int = 2) -> dict[str, Any]:
    return {
        "version": 7,
        "models": [
            {
                "id": "qwen-local",
                "harness": "hermes",
                "endpoint": "local",
                "endpoint_url": LOCAL_URL,
                "pool": "lab-local",
                "enabled": local_enabled,
                "disabled_reason": None if local_enabled else "too slow for standard work",
            },
            {
                "id": "codex-sub",
                "harness": "codex",
                "endpoint": "subscription",
                "pool": "openai-sub",
                "enabled": True,
            },
        ],
        "pools": {
            "lab-local": {"max_concurrency": cap},
            "openai-sub": {"max_concurrency": 3},
        },
        "tiers": {
            "standard": {"prefer_pools": ["openai-sub"]},
            "trivial": {"prefer_pools": None},
        },
    }


class _Versions:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def list_versions(self, name: str) -> list[Any]:
        return [row for row in self.rows if row.name == name]

    def get(self, name: str, version: int) -> Any | None:
        return next((row for row in self.rows if row.name == name and row.version == version), None)


class _Events:
    def __init__(self) -> None:
        self.rows: list[Event] = []

    def append(self, event: Event) -> Event:
        event.seq = len(self.rows) + 1
        self.rows.append(event)
        return event

    def list_global(
        self, *, after_seq: int, kind: str | None, since: datetime | None, limit: int
    ) -> list[Event]:
        found = [
            e for e in self.rows if (e.seq or 0) > after_seq and (kind is None or e.kind == kind)
        ]
        return found[:limit]


class _Wakes:
    def __init__(self) -> None:
        self.rows: list[Wake] = []

    def add(self, wake: Wake) -> None:
        self.rows.append(wake)


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


def _principal(name: str = "operator", role: Role = Role.ADMIN, pid: str = "p") -> Principal:
    return Principal(id=pid, name=name, role=role, created_at=datetime(2026, 9, 1, tzinfo=UTC))


def _world(document: dict[str, Any]) -> tuple[Any, Any, Any]:
    active = _policy("default-software", pinned=False)
    follower = _policy("hades-policy", pinned=False)
    pinned = _policy("pinned-policy", pinned=True)
    route = SimpleNamespace(
        name="route", version=7, retired_at=None, document=document, created_at=NOW
    )
    uow = SimpleNamespace(
        policies=_Versions([active, follower, pinned]),
        routing_policies=_Versions([route]),
        repositories=SimpleNamespace(
            list_all=lambda: [
                SimpleNamespace(name="hades", policy_name="hades-policy"),
                SimpleNamespace(name="foundry", policy_name="default-software"),
                SimpleNamespace(name="frozen", policy_name="pinned-policy"),
            ]
        ),
        principals=SimpleNamespace(
            list_all=lambda: [
                _principal(),
                _principal("foundry", Role.ORCHESTRATOR, "orchestrator-id"),
            ]
        ),
        wakes=_Wakes(),
        events=_Events(),
        provider_settings=SimpleNamespace(get=lambda name: None),
    )
    return uow, active, route


def _context() -> AdminContext:
    return cast(
        AdminContext,
        SimpleNamespace(
            clock=SimpleNamespace(now=lambda: NOW),
            proxy_config_path=None,
            proxy_subnet="",
            proxy_hosts=(),
            providers={},
        ),
    )


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    saved: dict[str, list[dict[str, Any]]] = {"routing": [], "policies": []}

    def put_routing(uow: Any, clock: Any, **kwargs: Any) -> None:
        saved["routing"].append(deepcopy(kwargs))
        uow.events.append(
            Event(
                seq=None,
                ts=NOW,
                kind=EventKind.ROUTING_POLICY_UPLOADED.value,
                principal=kwargs["principal"].name,
                verified=True,
                payload={
                    "routing_policy": {"name": kwargs["name"], "version": kwargs["version"]},
                    "reason": kwargs.get("reason"),
                    **(kwargs.get("extra") or {}),
                },
            )
        )
        uow.routing_policies.rows.append(
            SimpleNamespace(
                name=kwargs["name"],
                version=kwargs["version"],
                document=kwargs["document"],
                retired_at=None,
                created_at=NOW,
            )
        )

    monkeypatch.setattr(routing, "put_routing_policy", put_routing)
    monkeypatch.setattr(
        routing, "put_policy", lambda *args, **kwargs: saved["policies"].append(kwargs)
    )
    return saved


def _publish(uow: Any, policy: Any, route: Any, document: dict[str, Any], reason: str) -> None:
    routing.publish_routing(
        _context(),
        uow,
        principal=_principal(),
        policy=policy,
        routing=route,
        routing_document=document,
        reason=reason,
        note="Local gateway models update",
    )


def test_delta_names_models_caps_and_tier_order() -> None:
    before = _routing_document()
    after = _routing_document(local_enabled=True, cap=6)
    after["models"][1]["enabled"] = False
    after["tiers"]["trivial"]["prefer_pools"] = ["lab-local"]

    delta = routing.routing_delta(before, after)

    assert delta == {
        "models_enabled": ["qwen-local"],
        "models_disabled": ["codex-sub"],
        "pool_caps": [{"pool": "lab-local", "before": 2, "after": 6}],
        "tier_pool_order": [{"tier": "trivial", "before": None, "after": ["lab-local"]}],
    }
    assert routing.delta_needs_reason(delta)
    assert not routing.delta_needs_reason(
        routing.routing_delta(before, {**deepcopy(before), "tiers": after["tiers"]})
    )


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"local_enabled": True}, id="enables-a-model"),
        pytest.param({"cap": 8}, id="changes-a-pool-cap"),
    ],
)
@pytest.mark.parametrize("reason", ["", "   "])
def test_publish_that_flips_a_model_or_cap_without_a_reason_is_refused(
    stored: dict[str, list[dict[str, Any]]], change: dict[str, Any], reason: str
) -> None:
    uow, policy, route = _world(_routing_document())

    with pytest.raises(ContractValidationError) as refused:
        _publish(uow, policy, route, _routing_document(**change), reason)

    assert "supersedes" in refused.value.detail
    assert refused.value.errors == [
        {"path": "reason", "message": "must name the decision this supersedes"}
    ]
    assert stored["routing"] == [] and stored["policies"] == []
    assert uow.wakes.rows == []


def test_publish_with_a_reason_raises_one_routing_changed_wake(
    stored: dict[str, list[dict[str, Any]]],
) -> None:
    uow, policy, route = _world(_routing_document())
    document = _routing_document(local_enabled=True, cap=6)
    document["models"][1]["enabled"] = False

    _publish(uow, policy, route, document, "supersedes the 2026-09-30 decision to keep local off")

    assert len(uow.wakes.rows) == 1
    wake = uow.wakes.rows[0]
    assert wake.reason == WakeReason.ROUTING_CHANGED.value == "routing_changed"
    assert wake.principal_id == "orchestrator-id"
    summary = wake.payload["summary"]
    assert "enables qwen-local" in summary
    assert "disables codex-sub" in summary
    assert "pool lab-local max_concurrency 2 to 6" in summary
    assert "projects following routing unpinned: foundry, hades" in summary
    assert "frozen" not in summary
    assert "supersedes the 2026-09-30 decision" in summary
    assert chr(0x2014) not in summary  # no em-dash
    rendered = wake_document(wake, principal_name="foundry")
    assert rendered["reason"] == "routing_changed" and rendered["task"] is None
    # The publish event carries the delta for the Routing page.
    recorded = stored["routing"][0]["extra"]["delta"]
    assert recorded["unpinned_projects"] == ["foundry", "hades"]
    assert recorded["unpinned_policies"] == ["default-software", "hades-policy"]
    assert {item["name"] for item in stored["policies"]} == {"default-software", "hades-policy"}


def test_a_tier_order_change_needs_no_reason_and_wakes_nobody(
    stored: dict[str, list[dict[str, Any]]],
) -> None:
    uow, policy, route = _world(_routing_document())
    document = _routing_document()
    document["tiers"]["trivial"]["prefer_pools"] = ["lab-local"]

    _publish(uow, policy, route, document, "")

    assert len(stored["routing"]) == 1
    assert uow.wakes.rows == []
    assert stored["routing"][0]["extra"]["delta"]["tier_pool_order"] == [
        {"tier": "trivial", "before": None, "after": ["lab-local"]}
    ]


def test_local_endpoint_save_that_re_enables_local_models_needs_a_reason(
    stored: dict[str, list[dict[str, Any]]],
) -> None:
    uow, _policy_row, _route = _world(_routing_document())

    with pytest.raises(ContractValidationError) as refused:
        routing.save_local_endpoint(
            _context(),
            uow,
            principal=_principal(),
            endpoint_url=LOCAL_URL,
            models=[{"id": "qwen-local", "enabled": True}],
            max_concurrency=2,
            reason=None,
        )

    assert "enables qwen-local" in refused.value.detail
    assert stored["routing"] == [] and uow.wakes.rows == []


def test_routing_page_shows_each_versions_delta_publisher_and_reason(
    stored: dict[str, list[dict[str, Any]]],
) -> None:
    uow, policy, route = _world(_routing_document())
    _publish(
        uow,
        policy,
        route,
        _routing_document(local_enabled=True),
        "supersedes keeping local models off",
    )

    section = routing_page._versions_section(uow, "route")

    assert section["title"] == "Routing versions"
    assert section["columns"] == ["Version", "Published", "By", "Reason", "What changed"]
    newest, first = section["rows"]
    assert newest[0] == "8" and newest[2] == "operator"
    assert newest[3] == "supersedes keeping local models off"
    assert "enables qwen-local" in newest[4]
    assert "projects following routing unpinned: foundry, hades" in newest[4]
    assert first[0] == "7" and first[4] == "first version"


def test_gateway_model_save_previews_the_delta_before_publishing(
    monkeypatch: pytest.MonkeyPatch, stored: dict[str, list[dict[str, Any]]]
) -> None:
    uow, policy, route = _world(_routing_document())
    monkeypatch.setattr(gateway, "gateway_url", lambda uow: (LOCAL_URL, "routing"))
    monkeypatch.setattr(credentials, "read_api_key", lambda ctx, harness: "key")
    monkeypatch.setattr(gateway, "fetch_models", lambda endpoint, bearer: ["qwen-local"])
    monkeypatch.setattr(gateway, "active_documents", lambda uow: (policy, route))
    monkeypatch.setattr(gateway, "parse_routing_policy", lambda document: document)

    preview = asyncio.run(
        gateway.save_models(
            _context(),
            uow,
            principal=_principal(),
            models=[{"id": "qwen-local", "enabled": True}],
            max_concurrency=None,
            reason="",
            preview=True,
        )
    )

    assert stored["routing"] == [] and uow.wakes.rows == []
    assert preview["delta"]["models_enabled"] == ["qwen-local"]
    assert preview["delta"]["unpinned_projects"] == ["foundry", "hades"]

    shown: dict[str, Any] = {}
    monkeypatch.setattr(gateway_page, "_page", lambda *args, **kwargs: shown.update(kwargs))
    gateway_page._confirm_page(
        cast(Any, None),
        _principal(),
        "csrf",
        {"model.0.id": "qwen-local", "model.0.enabled": "true", "reason": ""},
        preview,
    )
    (section,) = shown["sections"]
    assert "publishes routing route version 8 over version 7" in section["note"]
    assert (
        "every project that follows routing unpinned: projects foundry, hades" in (section["note"])
    )
    assert ["enables qwen-local"] in section["rows"]
    fields = {field["name"]: field for field in section["form"]["fields"]}
    assert fields["confirm"]["value"] == "true"
    assert fields["model.0.enabled"] == {
        "kind": "hidden",
        "name": "model.0.enabled",
        "value": "true",
    }
