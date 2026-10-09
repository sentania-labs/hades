"""Hades #605, #437: an unpinned policy follows the latest routing version, and a
routing publish names the entries it overrides.

Operator, 2026-10-09: "how do I change the active routing policy?" Routing versions 39
and 40 were uploaded and never applied, because an unpinned policy only followed a
version some policy referenced. Now `routing.policy.pinned: false` routes with the
newest version that is not retired at task start, `pinned: true` keeps the named
version, and the Routing page's 'In force' row says which and why. A routing publish
names each entry whose enabled flag, weight, pool or tier membership changed against
the version before it, with the reason, in its response and in the Routing page's
history.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from crucible.adapters.api.routers import policies as policies_router
from crucible.adapters.ui.pages import policies as policies_page
from crucible.adapters.ui.pages import routing as routing_page
from crucible.application.admin import policy_editor
from crucible.application.admin import routing as admin_routing
from crucible.application.routing import (
    current_routing_version,
    load_attempt_routing,
    resolve_routing,
)
from crucible.domain.entities import Event, Policy, Principal, Role, RoutingPolicyRecord
from crucible.domain.events import EventKind
from tests.fixtures import FakeClock
from tests.unit.test_issue_254_routing_at_launch import (
    _model,
    _route_correction,
    _store,
    _version,
)
from tests.unit.test_issue_606_policies_page import _document as _full_document
from tests.unit.test_issue_606_policies_page import _Policies

NOW = datetime(2026, 10, 9, 15, tzinfo=UTC)


def _entry(harness: str, model: str, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "model": model,
        "harness": harness,
        "endpoint": "subscription",
        "capability": "mid",
        "cost": "low",
        "speed": "fast",
        "pool": "openai-sub",
        "weight": 1,
        "enabled": True,
    }
    entry.update(fields)
    return entry


def _document(version: int, models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "default-routing",
        "version": version,
        "tiers": {
            "standard": {"allowed_capability": ["mid"], "prefer": ["mid"]},
            "complex": {"allowed_capability": ["mid", "frontier"], "prefer": ["frontier"]},
        },
        "models": models
        if models is not None
        else [_entry("codex", "gpt-mid"), _entry("codex", "gpt-big", capability="frontier")],
        "pools": {
            "openai-sub": {"window": "5h", "budget_units": "attempts", "soft_limit": 10},
            "spare": {"window": "5h", "budget_units": "attempts", "soft_limit": 10},
        },
        "rotation": {
            "strategy": "weighted-least-recent",
            "quality_feedback": True,
            "quality_window": 20,
        },
    }


def _record(version: int, *, retired: bool = False, **document: Any) -> RoutingPolicyRecord:
    return RoutingPolicyRecord(
        name="default-routing",
        version=version,
        document={**_document(version), **document},
        created_at=NOW + timedelta(minutes=version),
        retired_at=NOW if retired else None,
    )


class _Routing:
    """Routing versions; none of them is referenced by a policy (uploaded only)."""

    def __init__(self, records: list[RoutingPolicyRecord]) -> None:
        self.records = records

    def get(self, name: str, version: int) -> RoutingPolicyRecord | None:
        return next((r for r in self.records if (r.name, r.version) == (name, version)), None)

    def list_versions(self, name: str) -> list[RoutingPolicyRecord]:
        return sorted((r for r in self.records if r.name == name), key=lambda r: r.version)

    def is_referenced(self, name: str, version: int) -> bool:
        return False

    def put(self, record: RoutingPolicyRecord) -> RoutingPolicyRecord:
        self.records = [r for r in self.records if r.version != record.version] + [record]
        return record


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
        return [
            e for e in self.rows if (e.seq or 0) > after_seq and (kind is None or e.kind == kind)
        ][:limit]


def _policy(named: int, *, pinned: bool | None) -> dict[str, Any]:
    ref: dict[str, Any] = {"name": "default-routing", "version": named}
    if pinned is not None:
        ref["pinned"] = pinned
    return {"routing": {"policy": ref}}


def _uow(records: list[RoutingPolicyRecord]) -> Any:
    return SimpleNamespace(routing_policies=_Routing(records))


# ----- AC1: pinned false follows the newest version not retired -------------------------


def test_unpinned_follows_the_newest_version_even_when_only_uploaded() -> None:
    uow = _uow([_record(38), _record(39), _record(40)])

    resolved = resolve_routing(uow, _policy(38, pinned=False))

    assert resolved is not None
    assert (resolved.named_version, resolved.version, resolved.pinned) == (38, 40, False)
    assert current_routing_version(uow, _policy(38, pinned=False)) == 40
    routing = load_attempt_routing(uow, _policy(38, pinned=False))
    assert routing is not None and routing.version == 40
    assert "unpinned" in resolved.why and "names version 38" in resolved.why


def test_an_absent_pinned_flag_reads_as_unpinned() -> None:
    uow = _uow([_record(38), _record(39)])

    assert current_routing_version(uow, _policy(38, pinned=None)) == 39


def test_pinned_keeps_the_named_version() -> None:
    uow = _uow([_record(38), _record(39), _record(40)])

    resolved = resolve_routing(uow, _policy(38, pinned=True))

    assert resolved is not None and resolved.version == 38 and resolved.pinned
    assert resolved.why == "pinned: the delivery policy names version 38 and keeps it"


def test_a_retired_newest_version_is_skipped() -> None:
    uow = _uow([_record(38), _record(39), _record(40, retired=True)])

    assert current_routing_version(uow, _policy(38, pinned=False)) == 39


def test_the_named_version_when_it_is_the_newest() -> None:
    uow = _uow([_record(37), _record(38)])

    resolved = resolve_routing(uow, _policy(38, pinned=False))

    assert resolved is not None and resolved.version == 38
    assert "is the newest" in resolved.why


def test_every_version_retired_keeps_the_named_one() -> None:
    uow = _uow([_record(38, retired=True), _record(39, retired=True)])

    resolved = resolve_routing(uow, _policy(38, pinned=False))

    assert resolved is not None and resolved.version == 38
    assert "every version" in resolved.why


def test_a_policy_without_a_routing_reference_resolves_to_nothing() -> None:
    assert resolve_routing(_uow([_record(1)]), {}) is None


def test_an_attempt_routes_with_an_uploaded_version_at_task_start(tmp_path: Path) -> None:
    """The supervisor routes a correction (an attempt starting) with the uploaded
    version 4 that no policy references, and records it on the attempt."""
    store = _store(_version(4, [_model("gpt-new")]))
    store.routing_policies.unpublished = {4}  # type: ignore[attr-defined]

    _execution, attempt = _route_correction(store, tmp_path)

    assert attempt.selected_model == "gpt-new"
    assert attempt.routing_version == 4


def test_a_pinned_attempt_keeps_the_named_version_at_task_start(tmp_path: Path) -> None:
    store = _store(_version(4, [_model("gpt-new")]), pinned=True)
    store.routing_policies.unpublished = {4}  # type: ignore[attr-defined]

    _execution, attempt = _route_correction(store, tmp_path)

    assert attempt.routing_version == 3


def test_the_admin_pages_edit_the_version_tasks_route_with() -> None:
    """The panels publish on top of the version in force, not on the named one, so a
    page save never silently reverts an uploaded version."""
    policy = SimpleNamespace(
        name="default-software", version=5, retired_at=None, document=_policy(38, pinned=False)
    )
    uow = SimpleNamespace(
        policies=SimpleNamespace(list_versions=lambda name: [policy]),
        routing_policies=_Routing([_record(38), _record(40)]),
    )

    _active, routing = admin_routing.active_documents(uow)

    assert routing.version == 40


def test_the_routing_page_in_force_row_says_which_version_and_why() -> None:
    uow = _uow([_record(38), _record(40)])

    row = routing_page._routing_in_force(resolve_routing(uow, _policy(38, pinned=False)))
    assert row["value"] == "default-routing version 40"
    assert row["hint"] == (
        "unpinned: follows the newest version not retired; the delivery policy names version 38"
    )
    pinned = routing_page._routing_in_force(resolve_routing(uow, _policy(38, pinned=True)))
    assert pinned["value"] == "default-routing version 38"
    assert pinned["hint"].startswith("pinned:")
    assert routing_page._routing_in_force(None) == "none"


# ----- AC2: a routing publish names the overridden entries -------------------------------


def test_overrides_name_enabled_weight_pool_and_tier_changes() -> None:
    before = _document(
        39,
        [
            _entry("codex", "gpt-mid"),
            _entry("codex", "gpt-big", capability="frontier"),
            _entry("hermes", "coder", enabled=False),
            _entry("agy", "gone"),
            _entry("qwen_code", "same"),
        ],
    )
    after = _document(
        40,
        [
            _entry("codex", "gpt-mid", weight=3, pool="spare"),
            _entry("codex", "gpt-big", capability="mid"),
            _entry("hermes", "coder", enabled=True),
            _entry("qwen_code", "same"),
            _entry("claude_code", "new"),
        ],
    )

    overrides = admin_routing.entry_overrides(before, after)

    assert overrides == [
        {
            "entry": "agy:gone",
            "change": "removed",
            "fields": [],
        },
        {
            "entry": "claude_code:new",
            "change": "added",
            "fields": [
                {"field": "enabled", "before": None, "after": True},
                {"field": "weight", "before": None, "after": 1},
                {"field": "pool", "before": None, "after": "openai-sub"},
                {"field": "tiers", "before": None, "after": ["complex", "standard"]},
            ],
        },
        {
            "entry": "codex:gpt-big",
            "change": "changed",
            "fields": [{"field": "tiers", "before": ["complex"], "after": ["complex", "standard"]}],
        },
        {
            "entry": "codex:gpt-mid",
            "change": "changed",
            "fields": [
                {"field": "weight", "before": 1, "after": 3},
                {"field": "pool", "before": "openai-sub", "after": "spare"},
            ],
        },
        {
            "entry": "hermes:coder",
            "change": "changed",
            "fields": [{"field": "enabled", "before": False, "after": True}],
        },
    ]
    words = [admin_routing.override_words(item) for item in overrides]
    assert words == [
        "removes agy:gone",
        "adds claude_code:new (enabled, weight 1, pool openai-sub, tiers complex, standard)",
        "codex:gpt-big: tiers complex to complex, standard",
        "codex:gpt-mid: weight 1 to 3, pool openai-sub to spare",
        "hermes:coder: disabled to enabled",
    ]
    delta_lines = admin_routing.delta_words(admin_routing.routing_delta(before, after))
    assert any(line.startswith("overrides removes agy:gone; ") for line in delta_lines)
    assert all(chr(0x2014) not in line for line in delta_lines)  # no em-dash


def test_the_routing_publish_response_names_the_overrides_and_reason() -> None:
    uow = SimpleNamespace(
        routing_policies=_Routing([_record(39)]),
        attempts=SimpleNamespace(routes_with=lambda name, version: False),
        events=_Events(),
    )
    ctx = SimpleNamespace(clock=FakeClock(NOW), admin=None)
    document = _document(40)
    document["models"][0]["weight"] = 5
    principal = Principal(id="p", name="operator", role=Role.ADMIN, created_at=NOW)
    uow.commit = lambda: None

    view = policies_router.upload_routing(
        "default-routing",
        40,
        ctx,  # type: ignore[arg-type]
        uow,
        principal,
        copy.deepcopy(document),
        "supersedes the even weights of version 39",
    )

    assert view.previous_version == 39
    assert view.reason == "supersedes the even weights of version 39"
    assert [item.model_dump() for item in view.overrides] == [
        {
            "entry": "codex:gpt-mid",
            "change": "changed",
            "fields": [{"field": "weight", "before": 1, "after": 5}],
        }
    ]
    (event,) = uow.events.rows
    assert event.kind == EventKind.ROUTING_POLICY_UPLOADED.value
    assert event.payload["overrides"][0]["entry"] == "codex:gpt-mid"
    assert event.payload["previous_version"] == 39


def test_the_routing_page_history_names_the_overrides_with_the_reason() -> None:
    events = _Events()
    events.append(
        Event(
            seq=None,
            ts=NOW,
            kind=EventKind.ROUTING_POLICY_UPLOADED.value,
            principal="operator",
            verified=True,
            payload={
                "routing_policy": {"name": "default-routing", "version": 40},
                "reason": "supersedes the 2026-10-01 decision to keep coder off",
            },
        )
    )
    newer = _record(40)
    newer = replace(
        newer,
        document={
            **newer.document,
            "models": [
                _entry("codex", "gpt-mid", enabled=False, disabled_reason="slow"),
                _entry("codex", "gpt-big", capability="frontier"),
            ],
        },
    )
    uow = SimpleNamespace(routing_policies=_Routing([_record(39), newer]), events=events)

    section = routing_page._versions_section(uow, "default-routing")

    newest = section["rows"][0]
    assert newest[0] == "40" and newest[2] == "operator"
    assert newest[3] == "supersedes the 2026-10-01 decision to keep coder off"
    assert "overrides codex:gpt-mid: enabled to disabled" in newest[4]


# ----- Review: usage and egress follow the routing version in force ---------------------


def _stored_policy(version: int, routing_version: int, *, pinned: bool) -> Any:
    return Policy(
        name="default-software",
        version=version,
        document=_policy(routing_version, pinned=pinned),
        created_at=NOW + timedelta(minutes=version),
    )


def _usage_uow(policies: list[Any]) -> Any:
    limited = _record(40)
    limited = replace(
        limited,
        document={
            **limited.document,
            "pools": {
                "openai-sub": {"window": "5h", "budget_units": "attempts", "soft_limit": 3},
            },
        },
    )
    return SimpleNamespace(
        routing_policies=_Routing([_record(38), limited]),
        policies=SimpleNamespace(
            list_versions=lambda name: [p for p in policies if p.name == name],
            get=lambda name, version: next(
                (p for p in policies if (p.name, p.version) == (name, version)), None
            ),
        ),
        attempt_metrics=SimpleNamespace(list_since=lambda since, model, task_ids: []),
        pool_exhaustions=SimpleNamespace(get=lambda pool: None),
    )


def test_routing_usage_reports_the_version_in_force_for_an_unpinned_policy() -> None:
    uow = _usage_uow([_stored_policy(7, 38, pinned=False)])
    ctx = SimpleNamespace(clock=FakeClock(NOW))

    view = policies_router.routing_usage(ctx, uow, None)  # type: ignore[arg-type]

    assert view.routing_policy == {"name": "default-routing", "version": 40}
    assert [(pool["pool"], pool["soft_limit"]) for pool in view.pools] == [("openai-sub", 3)]


def test_routing_usage_keeps_the_named_version_when_pinned_or_selected() -> None:
    ctx = SimpleNamespace(clock=FakeClock(NOW))
    pinned = _usage_uow([_stored_policy(7, 38, pinned=True)])
    assert policies_router.routing_usage(ctx, pinned, None).routing_policy == {  # type: ignore[arg-type]
        "name": "default-routing",
        "version": 38,
    }
    selected = _usage_uow([_stored_policy(7, 38, pinned=False)])
    view = policies_router.routing_usage(
        ctx,  # type: ignore[arg-type]
        selected,
        None,  # type: ignore[arg-type]
        policy_version=7,
    )
    assert view.routing_policy == {"name": "default-routing", "version": 38}


def _local(url: str) -> dict[str, Any]:
    return _entry("hermes", "coder", endpoint="local", endpoint_url=url, pool="spare")


def test_a_policy_page_publish_that_unpins_routing_sets_the_egress_it_selects() -> None:
    @dataclass(frozen=True)
    class _DockerConfig:
        proxy_allowlist: tuple[str, ...] = ("api.example.test",)

    document = _full_document()
    document["routing"] = {"policy": {"name": "default-routing", "version": 39, "pinned": True}}
    old = replace(
        _record(39), document={**_document(39), "models": [_local("http://gw-a:8000/v1")]}
    )
    new = replace(
        _record(40), document={**_document(40), "models": [_local("http://gw-b:9000/v1")]}
    )
    uow = SimpleNamespace(
        policies=_Policies(
            [Policy(name="default-software", version=4, document=document, created_at=NOW)]
        ),
        routing_policies=_Routing([old, new]),
        repositories=SimpleNamespace(list_all=lambda: []),
        principals=SimpleNamespace(get=lambda principal_id: None),
        events=_Events(),
        commit=lambda: None,
    )
    docker = SimpleNamespace(config=_DockerConfig())
    admin = SimpleNamespace(
        clock=FakeClock(NOW),
        harnesses=SimpleNamespace(names=lambda: [], get=lambda name: None),
        proxy_config_path=None,
        proxy_subnet="10.0.0.0/24",
        proxy_hosts=(),
        providers={"docker": docker},
        uow_factory=None,
    )
    ctx = SimpleNamespace(settings=None, admin=admin, clock=FakeClock(NOW))
    form = {
        policy_editor.FIELD_PREFIX + field["path"]: (
            "true" if field["value"] is True else field["shown"]
        )
        for group in policy_editor.group_fields(document)
        for field in group["fields"]
        if field["value"] is not False and field["path"] != "routing.policy.pinned"
    }
    form.update(name="default-software", publish_reason="follow the newest routing again")

    asyncio.run(
        policies_page._actions(
            None,  # type: ignore[arg-type]
            "policy-publish",
            ctx,  # type: ignore[arg-type]
            uow,
            Principal(id="p", name="operator", role=Role.ADMIN, created_at=NOW),
            "c",
            form,
            None,
        )
    )

    stored = uow.policies.get("default-software", 5)
    assert stored is not None and stored.document["routing"]["policy"]["pinned"] is False
    assert docker.config.proxy_allowlist == ("api.example.test", "gw-b:9000")


def test_a_policy_publish_that_keeps_the_routing_in_force_leaves_the_egress() -> None:
    policy = Policy(
        name="default-software",
        version=4,
        document=_policy(39, pinned=False),
        created_at=NOW,
    )
    uow = SimpleNamespace(
        policies=SimpleNamespace(list_versions=lambda name: [policy]),
        routing_policies=_Routing([_record(39), _record(40)]),
    )
    ctx = SimpleNamespace(providers={}, proxy_config_path=None)

    assert admin_routing.routing_in_force(uow) == ("default-routing", 40)
    assert admin_routing.sync_policy_egress(ctx, uow, before=("default-routing", 40)) is False  # type: ignore[arg-type]
    assert admin_routing.sync_policy_egress(ctx, uow, before=("default-routing", 39)) is True  # type: ignore[arg-type]
