"""Hades #606: the Admin policies page lists, edits and publishes a policy version.

/ui/policies lists every delivery policy and its versions, shows the version in force
grouped (routing, services, gates, network allowlist, limits, concurrency), edits those
groups as plain inputs with one reason box (the publish reason), publishes the edit as
the next version recorded with the operator, and shows a version against the one
before it. Only an administrator sees it; it is registered without a navigation link.
"""

from __future__ import annotations

import asyncio
import copy
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from fastapi import FastAPI
from starlette.testclient import TestClient

from crucible.adapters.api.deps import unit_of_work
from crucible.adapters.ui import router as ui_router
from crucible.adapters.ui.actions import handlers
from crucible.adapters.ui.pages import policies as policies_page
from crucible.application.admin import policy_editor
from crucible.application.errors import ContractValidationError
from crucible.contracts.policy import parse_policy, policy_document
from crucible.domain.entities import Event, Policy, Principal, Role, RoutingPolicyRecord
from crucible.domain.events import EventKind
from tests.fixtures import FakeClock

NOW = datetime(2026, 10, 9, 15, tzinfo=UTC)
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "policies" / "default-software.yaml"


def _document() -> dict[str, Any]:
    """The shipped example, as a stored version holds it: validated and written out whole."""
    return policy_document(parse_policy(yaml.safe_load(EXAMPLE.read_text())))


class _Policies:
    def __init__(self, rows: list[Policy]) -> None:
        self.rows = rows

    def list_names(self) -> list[str]:
        return sorted({row.name for row in self.rows})

    def list_versions(self, name: str) -> list[Policy]:
        return [row for row in self.rows if row.name == name]

    def get(self, name: str, version: int) -> Policy | None:
        return next((r for r in self.rows if (r.name, r.version) == (name, version)), None)

    def put(self, policy: Policy) -> Policy:
        self.rows.append(policy)
        return policy

    def is_referenced(self, name: str, version: int) -> bool:
        return True


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


def _world() -> Any:
    first = _document()
    second = copy.deepcopy(first)
    second["version"] = 4
    second["limits"]["grace_seconds"] = 90
    events = _Events()
    events.append(
        Event(
            seq=None,
            ts=NOW,
            kind=EventKind.POLICY_UPLOADED.value,
            principal="scott",
            verified=True,
            payload={
                "policy": {"name": "default-software", "version": 4},
                "reason": "longer grace",
            },
        )
    )
    routing = RoutingPolicyRecord(
        name="default-routing", version=3, document={}, created_at=NOW, retired_at=None
    )
    return SimpleNamespace(
        policies=_Policies(
            [
                Policy(name="default-software", version=3, document=first, created_at=NOW),
                Policy(
                    name="default-software",
                    version=4,
                    document=second,
                    created_at=NOW + timedelta(hours=1),
                ),
                Policy(name="hades-self-hosting", version=1, document=first, created_at=NOW),
            ]
        ),
        routing_policies=SimpleNamespace(get=lambda name, version: routing),
        events=events,
        principals=SimpleNamespace(get=lambda principal_id: None),
        commit=lambda: None,
    )


def _principal(role: Role = Role.ADMIN) -> Principal:
    return Principal(id="p", name="scott", role=role, created_at=NOW)


def _ctx() -> Any:
    harnesses = SimpleNamespace(names=lambda: [], get=lambda name: None)
    admin = SimpleNamespace(clock=FakeClock(NOW), harnesses=harnesses, uow_factory=None)
    return SimpleNamespace(settings=None, admin=admin, clock=FakeClock(NOW))


def _client(uow: Any, monkeypatch: pytest.MonkeyPatch, role: Role = Role.ADMIN) -> TestClient:
    app = FastAPI()
    app.state.ctx = _ctx()
    app.include_router(policies_page.router)
    app.dependency_overrides[unit_of_work] = lambda: uow
    monkeypatch.setattr(
        policies_page, "_require", lambda request, ctx, uow: (_principal(role), "c")
    )
    return TestClient(app)


def _form_fields(html: str) -> dict[str, str]:
    """name -> value of every input in the publish form (checkboxes: 'true' when ticked)."""
    form = html[html.index('action="/ui/actions/policy-publish"') :]
    form = form[: form.index("</form>")]
    found: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", form):
        name = re.search(r'name="([^"]+)"', tag)
        if name is None:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        if 'type="checkbox"' in tag:
            if "checked" in tag:
                found[name.group(1)] = "true"
            continue
        found[name.group(1)] = value.group(1) if value else ""
    return found


def test_the_page_is_registered_without_a_navigation_link() -> None:
    paths = {getattr(route, "path", "") for route in ui_router.router.routes}
    assert "/ui/policies" in paths
    assert "policy-publish" in handlers
    render = Path(policies_page.__file__).parents[1] / "render.py"
    assert '"/ui/policies"' not in render.read_text()


def test_only_an_administrator_sees_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _client(_world(), monkeypatch, Role.OPERATOR).get("/ui/policies")

    assert response.status_code == 403
    assert "Administrator only" in response.text
    assert "grace" not in response.text


def test_lists_policies_versions_and_the_grouped_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _client(_world(), monkeypatch).get("/ui/policies?name=default-software")

    assert response.status_code == 200
    html = response.text
    assert "default-software (shown)" in html and "hades-self-hosting" in html
    assert "Versions of default-software" in html
    assert "4 (in force)" in html and "longer grace" in html
    # Local Central time, never UTC, in what the operator reads.
    assert "10:00:00 AM CDT" in html
    for title in (
        "Routing (version 4)",
        "Services (version 4)",
        "Gates (version 4)",
        "Network allowlist (version 4)",
        "Limits (version 4)",
        "Concurrency (version 4)",
    ):
        assert title in html
    # The version in force against the one before it.
    assert "Version 4 against version 3" in html
    assert "limits.grace_seconds" in html
    assert chr(0x2014) not in html  # no em-dash


def test_the_edit_form_has_plain_inputs_and_only_the_publish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _client(_world(), monkeypatch).get("/ui/policies?name=default-software").text

    fields = _form_fields(html)
    assert fields["p.routing.policy.name"] == "default-routing"
    assert fields["p.limits.grace_seconds"] == "90"
    assert fields["p.network.egress_allowlist"].startswith("github.com, ")
    assert fields["p.concurrency.per_harness.codex"] == "1"
    assert "p.services.postgres.declared" not in fields  # not declared: unticked
    assert "p.gates.skipped" in fields
    assert 'name="reason"' not in html
    assert "<textarea" not in html[html.index("policy-publish") :]
    assert 'name="publish_reason"' in html
    # Phone width: the form is the shared auto-fitting grid and every table scrolls in
    # its own wrapper, so nothing is wider than the screen.
    assert 'class="admin-form-grid"' in html
    assert html.count("<table") == html.count('class="admin-table-wrap lat-table-scroll"')
    assert "style=" not in html[html.index('<div class="admin-stack">') :]


def test_publishing_writes_the_next_version_with_the_operator_and_a_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uow = _world()
    client = _client(uow, monkeypatch)
    fields = _form_fields(client.get("/ui/policies?name=default-software").text)
    fields.update(
        {
            "p.limits.grace_seconds": "120",
            "p.network.egress_allowlist": fields["p.network.egress_allowlist"] + ", example.org",
            "p.routing.policy.pinned": "true",
            "p.services.postgres.declared": "true",
            "publish_reason": "pin routing while version 41 is checked",
        }
    )

    response = asyncio.run(
        policies_page._actions(
            None,  # type: ignore[arg-type]
            "policy-publish",
            _ctx(),
            uow,
            _principal(),
            "c",
            fields,
            None,
        )
    )

    stored = uow.policies.get("default-software", 5)
    assert stored is not None
    assert stored.document["limits"]["grace_seconds"] == 120
    assert stored.document["network"]["egress_allowlist"][-1] == "example.org"
    assert stored.document["routing"]["policy"]["pinned"] is True
    assert stored.document["services"][0]["kind"] == "postgres"
    assert (
        stored.document["description"]
        == uow.policies.get("default-software", 4).document["description"]
    )
    event = uow.events.rows[-1]
    assert event.kind == EventKind.POLICY_UPLOADED.value
    assert event.principal == "scott"
    assert event.payload["reason"] == "pin routing while version 41 is checked"
    assert response is not None
    location = urlsplit(response.headers["location"])
    assert location.path == "/ui/policies"
    query = parse_qs(location.query)
    assert query["version"] == ["5"]
    assert query["message"][0].startswith("Published default-software version 5 by scott")

    html = client.get("/ui/policies?name=default-software&version=5").text
    assert "Version 5 against version 4" in html
    assert "limits.grace_seconds" in html and "routing.policy.pinned" in html
    assert "5 (in force)" in html and "pin routing while version 41 is checked" in html


def test_a_publish_needs_a_reason_and_a_change() -> None:
    uow = _world()
    current = uow.policies.get("default-software", 4).document
    unchanged = {
        policy_editor.FIELD_PREFIX + field["path"]: (
            "true" if field["value"] is True else field["shown"]
        )
        for group in policy_editor.group_fields(current)
        for field in group["fields"]
        if field["value"] is not False
    }

    with pytest.raises(ContractValidationError):
        policy_editor.publish_policy(
            _ctx().admin,
            uow,
            principal=_principal(),
            name="default-software",
            form=unchanged,
            reason="",
        )
    with pytest.raises(ContractValidationError, match="nothing was changed"):
        policy_editor.publish_policy(
            _ctx().admin,
            uow,
            principal=_principal(),
            name="default-software",
            form=unchanged,
            reason="no change",
        )
    assert uow.policies.get("default-software", 5) is None


def test_document_diff_lists_each_changed_setting() -> None:
    before = {"version": 1, "limits": {"grace_seconds": 60}, "network": {"egress_allowlist": []}}
    after = {"version": 2, "limits": {"grace_seconds": 90}, "network": {"egress_allowlist": ["a"]}}

    assert policy_editor.document_diff(before, after) == [
        {"path": "limits.grace_seconds", "before": 60, "after": 90},
        {"path": "network.egress_allowlist", "before": [], "after": ["a"]},
    ]
