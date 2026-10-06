"""Render helpers, reason fields, gateway and routing page rendering."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

from crucible.adapters.ui import render as ui_render
from crucible.adapters.ui.pages import gateway as gw_module
from crucible.adapters.ui.pages import routing_models as ui_routing_models
from crucible.adapters.ui.render import (
    _document_section,
    _localize,
    _panel,
    _safe_value,
    templates,
)
from crucible.application.admin import credentials, gateway
from crucible.application.admin import routing as routing_admin
from crucible.application.admin import routing_models as routing_models_service
from crucible.application.admin.credentials import HERMES
from crucible.domain.entities import (
    Principal,
    Role,
)
from crucible.domain.harness_settings import HermesRunLimits, setting_name
from tests.unit.admin_ui_fixtures import (
    _document_leaves,
    _lease,
    _panel_leaves,
    _render_documents,
    _status,
    _supervisor_document,
    base_context,
    request,
)


def test_readable_panel_preserves_every_supervisor_service_leaf() -> None:
    document = _supervisor_document(_lease(), _status())
    panel = _panel(document)
    rendered = _render_documents([_document_section("Supervisor", document)])

    assert len(_panel_leaves(panel)) == len(_document_leaves(document))
    assert "Lease holder" in rendered
    assert "Last successful tick" in rendered
    assert "Fenced token" in rendered
    assert "not displayed" not in rendered
    assert "healthy" in rendered
    assert "2026-09-21 01:30:00 AM CDT" in rendered
    assert "2026-09-21T06:30:00" not in rendered
    assert "<pre" not in rendered
    assert "{&#34;" not in rendered and '{"' not in rendered


def test_all_document_sections_suppress_secret_shaped_values() -> None:
    marker = "LEAK-MARKER-7f394b"
    document = {
        "token": marker,
        "access_token": marker,
        "refresh_token": marker,
        "device_code": marker,
        "password": marker,
        "webhook_secret": marker,
        "private_key": marker,
        "credential_value": marker,
        "authorization": marker,
        "device_url": f"https://example.invalid/device?user_code={marker}",
        "key_present": True,
        "webhook_secret_present": False,
    }
    titles = (
        "Supervisor",
        "Providers",
        "Status task state",
        "Pending wakes",
        "Active policy",
        "Routing policy",
        "Pool exhaustion",
        "App and repository connectivity",
        "Task state",
        "Wakes",
        "Summary",
        "Next cursor",
        "Imports",
        "Manifest",
    )
    rendered = _render_documents([_document_section(title, document) for title in titles])

    assert marker not in rendered
    assert "not displayed" in rendered
    assert "present" in rendered and "absent" in rendered


def test_harness_version_maps_are_shown_whatever_the_harness_is_called() -> None:
    """crucible#126: the audit of an image promotion lists every harness's version, and
    `claude_code` is a harness name, not a login code. Redaction goes by what a field
    holds, so a code, a token or a key under the same document stays hidden."""
    marker = "LEAK-MARKER-126"
    payload = {
        "reason": "",
        "harnesses": {
            "agy": "1.2.8",
            "claude_code": "2.1.280",
            "codex": "0.156.0",
            "hermes": "0.19.0",
            "script-harness": "1.0.0",
        },
        "code": marker,
        "user_code": marker,
        "login_token": marker,
        "error_code": 70,
        "api_key_set": True,
    }

    rendered = _render_documents([_document_section("Audit", payload)])

    for version in ("1.2.8", "2.1.280", "0.156.0", "0.19.0", "1.0.0"):
        assert version in rendered
    assert marker not in rendered
    assert rendered.count("not displayed") == 3
    assert "70" in rendered
    assert _safe_value("harnesses.claude_code", "2.1.280") == "2.1.280"
    assert _safe_value("claudeCode", "2.1.280") == "2.1.280"
    assert _safe_value("device_code", "ABCD-EFGH") == "not displayed"
    for name in ("secret_key", "token_value", "password_hash", "verification_code"):
        assert _safe_value(name, "x") == "not displayed", name
    for name in ("exit_code", "error_code", "tokens_in", "api_key_set", "private_key_path"):
        assert _safe_value(name, "x") == "x", name


def test_nested_lists_stay_readable_and_suppress_secrets_at_any_depth() -> None:
    marker = "ghp_" + "q" * 40
    document = {
        "entries": [
            {
                "description": f"provider returned {marker}",
                "details": [{"access_token": "LEAK-MARKER", "state": "ready"}],
                "checks": {},
                "lease": {"fenced_token": 27, "exit_code": 70},
            }
        ]
    }

    rendered = _render_documents([_document_section("Nested", document)])

    assert marker not in rendered
    assert "LEAK-MARKER" not in rendered
    assert "[redacted:github_token]" in rendered
    assert "not displayed" in rendered
    assert "ready" in rendered
    assert "Checks" in rendered and ">none<" in rendered
    assert "27" in rendered and "70" in rendered
    assert "{'" not in rendered and '{"' not in rendered


def test_coded_urls_and_manual_table_cells_are_sanitized() -> None:
    code = "ABCD-EFGH"
    marker = "ghp_" + "q" * 40
    sections = [
        {
            "title": "Repositories",
            "columns": ["URL", "Description"],
            "rows": [["https://user:password@example.invalid/repo", f"failure {marker}"]],
        }
    ]

    rendered = _render_documents(sections)

    assert "user:password" not in rendered
    assert marker not in rendered
    assert "[redacted:github_token]" in rendered
    assert code not in _safe_value("device_url", f"https://example.invalid/device#user_code={code}")
    assert code not in _safe_value(
        "device_url", f"https://example.invalid/device?user%5Fcode={code}"
    )
    assert _safe_value("repository_url", "https://[") == "invalid URL"


def test_invalid_timestamp_shaped_text_does_not_break_localization() -> None:
    detail = "provider failed near 2026-99-99T99:99:99Z"

    assert _localize(detail, "America/Chicago") == detail


def test_document_pages_have_no_generic_dump_markup() -> None:
    rendered = _render_documents(
        [
            _document_section("Fields", {"lease_holder": "supervisor-a", "healthy": True}),
            _document_section("Table", [{"attempt_id": "attempt-1", "active": False}]),
            _document_section("Empty", []),
        ]
    )

    filename = templates.get_template("page.html").filename
    assert filename is not None
    source = Path(filename).read_text(encoding="utf-8")
    assert "<pre" not in rendered
    assert "tojson" not in source
    assert "section.json" not in source
    assert "supervisor-a" in rendered and "attempt-1" in rendered
    assert "inactive" in rendered and ">none<" in rendered


def test_live_log_tail_is_the_only_page_template_preformatted_text() -> None:
    rendered = _render_documents([{"title": "Tail", "text": "worker output"}])

    assert rendered.count("<pre") == 1
    assert "worker output" in rendered


def test_a_reason_is_asked_for_only_where_the_service_requires_one() -> None:
    """crucible#117: one rule sets every form's reason field. Required on the
    destructive forms, absent on read-only checks and on actions that never
    need a note (crucible#186: image-change never asks for one)."""

    def form(action: str) -> dict[str, Any]:
        return {
            "title": action,
            "form": {
                "action": action,
                "fields": [
                    {"name": "harness", "label": "Harness"},
                    {"name": "reason", "label": "Reason", "required": True},
                ],
            },
        }

    sections = ui_render._reason_fields(
        [
            form("/ui/actions/token-revoke"),
            form("/ui/actions/image-change"),
            form("/ui/actions/github-check"),
        ]
    )
    reasons = [
        [f for f in section["form"]["fields"] if f["name"] == "reason"] for section in sections
    ]
    assert reasons[0] == [{"name": "reason", "label": "Reason", "required": True}]
    # image-change (the unified promote/rollback control) never asks for a reason.
    assert reasons[1] == []
    assert reasons[2] == []


def test_a_row_action_renders_its_reason_as_optional_required_or_absent() -> None:
    """Codex on PR 164 (crucible#117): a row action's own reason mode decides its input."""

    def row(label: str, **mode: Any) -> dict[str, Any]:
        return {"kind": "form", "action": f"/ui/actions/{label}", "label": label, **mode}

    rendered = templates.get_template("page.html").render(
        **base_context("/ui/images"),
        heading="Images",
        intro="Fixture",
        sections=[
            {
                "title": "Rows",
                "columns": ["Actions"],
                "rows": [
                    [
                        {
                            "kind": "actions",
                            "items": [
                                row("note", reason="optional"),
                                row("remove", reason=True, danger=True),
                                row("check"),
                            ],
                        }
                    ]
                ],
            }
        ],
        badge=None,
    )
    forms = dict(re.findall(r'action="/ui/actions/(\w+)">(.*?)</form>', rendered, re.S))
    assert '<input class="lat-input" name="reason" placeholder="Reason (optional)"' in forms["note"]
    assert "required" not in forms["note"]
    assert 'placeholder="Reason (required)" aria-label="Reason" required>' in forms["remove"]
    assert 'name="reason"' not in forms["check"]


def test_gateway_page_lists_models_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """models_view(fetch=False) skips the gateway network call and reports a not-asked note;
    models_view(fetch=True) contacts the gateway once to list models."""

    # Patch the gateway URL
    monkeypatch.setattr(
        gateway, "gateway_url", lambda _uow: ("http://localhost:8000/v1", "setting")
    )
    # Patch local entries
    monkeypatch.setattr(
        gateway,
        "_local_entries",
        lambda _uow: (
            [
                {
                    "id": "local-model",
                    "endpoint": "local",
                    "model_name": "local-model",
                    "enabled": True,
                },
            ],
            {"max_concurrency": 4},
        ),
    )
    # Patch the API key so read_api_key doesn't fail when fetch=True
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args, **_kwargs: "fake-bearer")

    # Count calls to fetch_models
    call_count = {"n": 0}

    def counting_fetch_models(endpoint: str, bearer: str, **kwargs: object) -> list[str]:
        call_count["n"] += 1
        return ["gpt-4o", "claude-sonnet"]

    monkeypatch.setattr(gateway, "fetch_models", counting_fetch_models)

    ctx_admin = SimpleNamespace(name="admin", role=SimpleNamespace(value="admin"))

    # fetch=False: should NOT call fetch_models
    call_count["n"] = 0
    result = asyncio.run(gateway.models_view(ctx_admin, None, fetch=False))  # type: ignore[arg-type]
    assert call_count["n"] == 0, "fetch_models must not be called when fetch=False"
    assert result["error"] == "The gateway's models were not asked for; use the link to list them."
    assert result["reachable"] is False
    assert result["offered_count"] is None

    # fetch=True: should call fetch_models exactly once
    call_count["n"] = 0
    result = asyncio.run(gateway.models_view(ctx_admin, None, fetch=True))  # type: ignore[arg-type]
    assert call_count["n"] == 1, "fetch_models must be called once when fetch=True"
    assert result["error"] is None
    assert result["reachable"] is True
    assert result["offered_count"] == 2


def test_gateway_page_unfetched_shows_admin_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Local gateway page renders the URL/key form and test form even when models
    were not asked for, so an operator can still configure the gateway (PR 291 fix)."""

    monkeypatch.setattr(
        gateway, "gateway_url", lambda _uow: ("http://localhost:8000/v1", "setting")
    )
    monkeypatch.setattr(
        gateway,
        "_local_entries",
        lambda _uow: (
            [
                {
                    "id": "local-model",
                    "endpoint": "local",
                    "model_name": "local-model",
                    "enabled": True,
                },
            ],
            {"max_concurrency": 4},
        ),
    )
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args, **_kwargs: "fake-bearer")

    def counting_fetch_models(endpoint: str, bearer: str, **kwargs: object) -> list[str]:
        return ["gpt-4o"]

    monkeypatch.setattr(gateway, "fetch_models", counting_fetch_models)
    monkeypatch.setattr(
        routing_admin,
        "routing_followers",
        lambda _uow: {"unpinned_policies": ["default-software"], "unpinned_projects": []},
    )

    now = datetime(2025, 1, 1, tzinfo=UTC)

    class _FakeSettingsRepo:
        def get(self, name: str) -> SimpleNamespace | None:
            if name == setting_name(HERMES):
                limits = HermesRunLimits()
                return SimpleNamespace(
                    document=limits.as_dict(),
                    updated_at=now,
                    updated_by="admin",
                )
            return None

    class FakeUoW:
        def __enter__(self) -> FakeUoW:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def commit(self) -> None:
            pass

        @property
        def provider_settings(self) -> _FakeSettingsRepo:
            return _FakeSettingsRepo()

        @property
        def harnesses(self) -> None:
            return None

    fake_uow = FakeUoW()

    monkeypatch.setattr(
        gateway,
        "gateway_view",
        lambda *args: {
            "endpoint_url": "http://localhost:8000/v1",
            "url_source": "setting",
            "key_set": True,
            "credential_state": "validated",
            "last_tested_at": "2025-01-01T00:00:00Z",
            "last_test": "the last test passed",
            "last_outcome": "probe:completed",
            "models": [],
            "pool": {"max_concurrency": 4},
        },
    )

    # Patch credentials.read_secrets to avoid needing real credential stores.
    async def fake_read_secrets(*args: object, **kwargs: object) -> dict[str, SimpleNamespace]:
        return {HERMES: SimpleNamespace(value="fake")}

    monkeypatch.setattr(credentials, "read_secrets", fake_read_secrets)

    ctx = SimpleNamespace(
        admin=SimpleNamespace(
            name="admin",
            role=SimpleNamespace(value="admin"),
            clock=SimpleNamespace(now=lambda: now),
            providers={},
        ),
        settings=SimpleNamespace(service=SimpleNamespace(render_timezone="UTC")),
    )

    principal = Principal(
        id="test-admin",
        name="admin",
        role=Role.ADMIN,
        created_at=now,
    )

    # Bypass session check.
    monkeypatch.setattr(
        gw_module, "_require", lambda *_args, **_kwargs: (principal, "fixture-csrf")
    )
    monkeypatch.setattr(gw_module, "_without_migration", lambda x: x)

    captured_sections: list[dict[str, Any]] = []

    def fake_page(*args: Any, sections: list[dict[str, Any]], **kwargs: Any) -> Any:
        captured_sections.clear()
        captured_sections.extend(sections)
        from crucible.adapters.ui.render import templates  # noqa: PLC0415

        class FakeResponse:
            body = templates.get_template("page.html").render(
                request=args[0] if args else {},
                title="Local gateway",
                active="/ui/gateway",
                nav=(),
                heading="Local gateway",
                intro="The gateway Hermes uses.",
                sections=sections,
                badge="tested",
                badge_kind="ok",
                csrf="fixture-csrf",
                hidden=frozenset(),
                principal=principal,
                message=None,
            )

        return FakeResponse()

    monkeypatch.setattr(gw_module, "_page", fake_page)

    # Unfetched page (no ?models=1 query)
    req = Request(
        {"type": "http", "method": "GET", "path": "/ui/gateway", "query_string": b"", "headers": []}
    )
    asyncio.run(gw_module.gateway_page(req, ctx, fake_uow))  # type: ignore[arg-type]
    assert captured_sections, "sections must be populated"

    section_titles = [s["title"] for s in captured_sections]

    # The URL and key save form must appear on the unfetched page.
    assert "Set the gateway URL and key" in section_titles
    form_section = next(s for s in captured_sections if s["title"] == "Set the gateway URL and key")
    assert "form" in form_section
    fields = [f["name"] for f in form_section["form"]["fields"]]
    assert "endpoint_url" in fields
    assert "api_key" in fields
    assert "reason" in fields
    assert form_section["form"]["label"] == "Save and test"

    # The test form must also appear (on the gateway section).
    gateway_section = next(s for s in captured_sections if s["title"] == "Gateway")
    assert "form" in gateway_section
    assert gateway_section["form"]["label"] == "Test the gateway again"

    # The listing section must have the "List the gateway's models" button.
    listing_section = next(s for s in captured_sections if s["title"] == "Models the key can see")
    assert listing_section.get("button", {}).get("label") == "List the gateway's models"
    assert "rows" not in listing_section, "no model rows when not fetched"
    assert "form" not in listing_section, "no model choice form when not fetched"

    # Local run limits section must be present.
    assert "Local run limits" in section_titles

    # Model choice form must NOT be present when not fetched.
    assert all(
        "form" not in s or s["form"]["label"] != "Save model choices" for s in captured_sections
    ), "model choice form should not be present when models are not fetched"

    # Fetched page (with ?models=1 query)
    req_fetched = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/ui/gateway",
            "query_string": b"models=1",
            "headers": [],
        }
    )
    captured_sections.clear()
    asyncio.run(gw_module.gateway_page(req_fetched, ctx, fake_uow))  # type: ignore[arg-type]
    assert captured_sections

    listing_fetched = next(s for s in captured_sections if s["title"] == "Models the key can see")

    # Model choice form must appear when fetched.
    assert "form" in listing_fetched
    assert listing_fetched["form"]["label"] == "Save model choices"

    # The listing rows must be present (gpt-4o from fetch_models).
    grid_field = next(f for f in listing_fetched["form"]["fields"] if f["kind"] == "grid")
    rows_text = str(grid_field["rows"])
    assert "gpt-4o" in rows_text


def test_routing_model_and_tier_controls_are_ordinary_reasoned_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        routing_models_service,
        "routing_controls_view",
        lambda _uow: {
            "pinned": False,
            "pools": ["codex", "claude"],
            "models": [
                {
                    "id": "gpt",
                    "harness": "codex",
                    "pool": "codex",
                    "capability": "frontier",
                    "enabled": True,
                }
            ],
            "tiers": {
                "complex": {
                    "plain_words": (
                        "complex: Codex first, then Claude Code; a busy first choice waits"
                    ),
                    "prefer_pools": ["codex", "claude"],
                    "allowed_capability": ["frontier"],
                }
            },
        },
    )

    sections = ui_routing_models.control_sections(cast(Any, object()), admin=True)

    assert sections[0]["rows"][0][1].endswith("a busy first choice waits")
    assert sections[1]["rows"][0][4]["action"] == "/ui/actions/routing-model"
    tier_form = sections[2]["form"]
    assert tier_form["action"] == "/ui/actions/routing-tier"
    assert [
        field["value"] for field in tier_form["fields"] if field["name"].startswith("pool_")
    ] == ["codex", "claude"]
    assert tier_form["fields"][-1]["name"] == "reason"


def test_routing_tier_action_keeps_numeric_pool_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[str] = []
    monkeypatch.setattr(
        routing_models_service,
        "save_tier",
        lambda *args, **kwargs: saved.extend(kwargs["prefer_pools"]),
    )
    form = {"tier": "complex", **{f"pool_{n}": f"pool-{n}" for n in range(11)}}

    asyncio.run(
        ui_routing_models._actions(
            request("/ui/actions/routing-tier"),
            "routing-tier",
            cast(Any, SimpleNamespace(admin=object())),
            cast(Any, object()),
            cast(Any, object()),
            "csrf",
            form,
            "reorder pools",
        )
    )

    assert saved == [f"pool-{n}" for n in range(11)]
