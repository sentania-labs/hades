"""Hades #513/#512/#514 routing references and operator audit behavior."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from alembic import op
from fastapi import FastAPI, Request
from starlette.responses import HTMLResponse
from starlette.testclient import TestClient

from crucible.adapters.persistence.migrations.versions import _0051_routing_model_references as m51
from crucible.adapters.ui.pages import gateway as page
from crucible.adapters.ui.render import _page, _routing_policy_details, templates
from crucible.application import routing as routes
from crucible.application.admin import credentials, gateway
from crucible.application.admin import routing as admin_routing
from crucible.application.admin.context import require_reason
from crucible.application.errors import ContractValidationError
from crucible.application.policies import validate_routing_policy
from crucible.application.supervisor import _Pending
from crucible.contracts.policy import RoutingPolicyV1
from crucible.domain.entities import Event, PoolExhaustion
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import TaskState
from tests.fixtures import FakeClock
from tests.unit.admin_ui_fixtures import _render_documents
from tests.unit.test_class_routing import _model, _routing, _uow
from tests.unit.test_issue_254_routing_at_launch import (
    _model as _correction_model,
)
from tests.unit.test_issue_254_routing_at_launch import (
    _route_correction,
    _store,
    _version,
)
from tests.unit.test_issue_360_ready_for_merge_correction import NOW, _supervisor
from tests.unit.test_issue_437_routing_publish_delta import _context, _principal

URL = "http://gateway.internal:4000/v1"


def _entry(harness: str, model: str = "coder") -> dict[str, object]:
    return {
        "model": model,
        "harness": harness,
        "endpoint": "local",
        "endpoint_url": URL,
        "capability": "mid",
        "cost": "none",
        "speed": "fast",
        "pool": "lab-local",
        "weight": 1,
        "enabled": True,
    }


def _document() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "name": "route",
        "version": 8,
        "tiers": {"standard": {"allowed_capability": ["mid"], "prefer": ["mid"]}},
        "models": [_entry("hermes"), _entry("qwen_code"), _entry("codex")],
        "pools": {
            "lab-local": {
                "window": "1h",
                "budget_units": "attempts",
                "soft_limit": 0,
                "max_concurrency": 4,
            }
        },
        "rotation": {
            "strategy": "weighted-least-recent",
            "quality_feedback": True,
            "quality_window": 20,
        },
    }


def test_pair_uniqueness_allows_three_harnesses_to_share_coder() -> None:
    routing = RoutingPolicyV1.model_validate(_document())
    assert [(entry.harness, entry.model) for entry in routing.models] == [
        ("hermes", "coder"),
        ("qwen_code", "coder"),
        ("codex", "coder"),
    ]


def test_shipped_migrated_policy_renders_routing_gateway_and_board_pages() -> None:
    """Keep the worker-local tier sensitive to the three compose-smoke page failures."""
    document = m51._current(m51._legacy(_document()))
    principal = _principal()
    app = FastAPI()
    app.state.ctx = SimpleNamespace(settings=None)

    @app.get("/ui/routing")
    def routing_page(request: Request) -> HTMLResponse:
        return _page(
            request,
            principal,
            "csrf",
            active="/ui/routing",
            heading="Routing",
            intro="Routes in force.",
            sections=[_routing_policy_details(document)],
        )

    @app.get("/ui/gateway")
    def gateway_page(request: Request) -> HTMLResponse:
        rows = [
            [entry["model"], entry["harness"], "enabled" if entry["enabled"] else "disabled"]
            for entry in document["models"]
        ]
        return _page(
            request,
            principal,
            "csrf",
            active="/ui/gateway",
            heading="Local gateway",
            intro="Gateway models.",
            sections=[
                {
                    "title": "Models the key can see",
                    "columns": ["Model", "Harness", "State"],
                    "rows": rows,
                }
            ],
        )

    @app.get("/ui/board")
    def board_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="board.html",
            context={
                "request": request,
                "title": "Board",
                "active": "/ui/board",
                "nav": (),
                "principal": principal,
                "csrf": "csrf",
                "message": None,
                "lanes": [],
            },
        )

    with TestClient(app, raise_server_exceptions=False) as client:
        responses = {path: client.get(path) for path in ("/ui/routing", "/ui/gateway", "/ui/board")}
    assert {path: response.status_code for path, response in responses.items()} == {
        "/ui/routing": 200,
        "/ui/gateway": 200,
        "/ui/board": 200,
    }
    assert "qwen-coder" not in responses["/ui/routing"].text
    assert "coder" in responses["/ui/routing"].text


def test_duplicate_pair_is_refused() -> None:
    document = _document()
    models = cast(list[dict[str, object]], _document()["models"])
    document["models"] = [*models, deepcopy(_entry("qwen_code"))]
    with pytest.raises(ValueError, match=r"\(harness, model\)"):
        RoutingPolicyV1.model_validate(document)


def test_unknown_local_model_names_model_and_checked_listing() -> None:
    document = _document()
    document["models"] = [_entry("qwen_code", "unknown")]
    with pytest.raises(ContractValidationError) as refused:
        validate_routing_policy(
            document, name="route", version=8, local_model_listing=["coder", "reasoner"]
        )
    assert "unknown" in refused.value.errors[0]["message"]
    assert "coder" in refused.value.errors[0]["message"]
    assert "reasoner" in refused.value.errors[0]["message"]


def test_migration_rewrites_populated_qwen_route_without_changing_version() -> None:
    populated = _document()
    populated["version"] = 7
    populated["models"] = [{**_entry("qwen_code"), "id": "qwen-coder", "model_name": "coder"}]
    models = cast(list[dict[str, object]], populated["models"])
    models[0].pop("model")

    migrated = m51._current(populated)

    assert migrated["version"] == 7
    assert migrated["models"][0]["harness"] == "qwen_code"
    assert migrated["models"][0]["model"] == "coder"
    assert "id" not in migrated["models"][0]


def test_operator_reason_is_optional_and_typed_words_are_verbatim() -> None:
    assert require_reason(None, required=False) == ""
    assert require_reason("  keep my spacing  ", required=False) == "  keep my spacing  "


@pytest.mark.parametrize("legacy_event", [False, True])
def test_capacity_retry_keeps_other_harnesses_on_the_shared_model(
    tmp_path: Path,
    legacy_event: bool,
) -> None:
    codex = _correction_model("coder")
    hermes = {**codex, "harness": "hermes"}
    store = _store(_version(4, [codex, hermes]))
    execution, attempt = _route_correction(store, tmp_path)
    assert attempt.selected_harness == "codex"
    attempt.exit_class = ExitClass.INFRASTRUCTURE
    task = store.tasks.get(attempt.task_id)
    assert task is not None
    task.state = TaskState.RUNNING
    store.events.append(
        Event(
            seq=None,
            verified=True,
            ts=NOW,
            kind=EventKind.ATTEMPT_EXITED.value,
            principal="crucible",
            task_id=task.id,
            attempt_id=attempt.id,
            payload={"capacity_refused": True},
        )
    )
    supervisor, _ = _supervisor(store, FakeClock(NOW), tmp_path)
    supervisor._retry_interruption(store.uow(), task, execution, attempt)
    event = store.events.latest_for_task_kind(task.id, EventKind.TASK_RETRY_SCHEDULED.value)
    assert event is not None
    assert (event.payload["excluded_harness"], event.payload["excluded_model"]) == (
        "codex",
        "coder",
    )
    if legacy_event:
        event.payload.pop("excluded_harness")
    retry = store.attempts.get(event.payload["next_attempt_id"])
    contract = store.contracts.get(task.id, execution.contract_version)
    assert retry is not None and contract is not None
    selection = supervisor._selection_for(
        store.uow(), _Pending(retry, execution, task, contract.document)
    )
    assert selection.selected.harness == "hermes"
    candidates = {row["harness"]: row for row in selection.candidates}
    assert candidates["codex"]["capacity_refused"] is True
    assert candidates["hermes"]["eligible"] is True


@pytest.mark.parametrize("blocked_harness", ["hermes", "codex"])
def test_launch_reserves_the_selected_harness_pool(
    monkeypatch: pytest.MonkeyPatch, blocked_harness: str
) -> None:
    routing = _routing(
        [
            _model("coder", harness="hermes", pool="local"),
            _model("coder", harness="codex", pool="subscription"),
        ]
    )
    routing.models[0].endpoint = "local"
    routing.models[0].endpoint_url = URL
    blocked_pool = "local" if blocked_harness == "hermes" else "subscription"
    uow = _uow(
        marks=[
            PoolExhaustion(
                pool=blocked_pool,
                exhausted_at=NOW,
                reset_at=NOW + timedelta(hours=1),
                task_id="task",
                attempt_id="attempt",
                reason="quota",
            )
        ]
    )
    monkeypatch.setattr(routes, "load_attempt_routing", lambda *_args: routing)
    for harness, pool, endpoint in (
        ("hermes", "local", "local"),
        ("codex", "subscription", "subscription"),
    ):
        reservation = routes.reserve(uow, {}, harness=harness, model_id="coder", now=NOW)
        assert reservation.pool == pool
        assert reservation.endpoint_kind == endpoint
        assert reservation.ok is (harness != blocked_harness)
    assert routing.model("coder", "qwen_code") is None


def test_new_gateway_model_has_a_control_and_saves_without_resetting_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document()
    entries = cast(list[dict[str, Any]], document["models"])
    for entry in entries:
        entry["chat_template_kwargs"] = {"enable_thinking": entry["harness"] != "codex"}
    policy = SimpleNamespace(name="policy", version=2, document={})
    route = SimpleNamespace(name="route", version=8, document=document)
    monkeypatch.setattr(gateway, "gateway_url", lambda _uow: (URL, "routing"))
    monkeypatch.setattr(gateway, "_local_entries", lambda _uow: (entries, {}))
    monkeypatch.setattr(credentials, "read_api_key", lambda *_args: "key")
    monkeypatch.setattr(gateway, "fetch_models", lambda *_args: ["coder", "new-model"])
    monkeypatch.setattr(gateway, "active_documents", lambda _uow: (policy, route))
    monkeypatch.setattr(gateway, "guard_mutation", lambda *args, **kwargs: "")
    monkeypatch.setattr(gateway, "admin_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "gateway_view", lambda *_args: {})
    published: list[dict[str, Any]] = []

    def publish(*args: Any, **kwargs: Any) -> tuple[int, int]:
        published.append(kwargs["routing_document"])
        return 3, 9

    monkeypatch.setattr(gateway, "publish_routing", publish)
    context = _context()
    uow = SimpleNamespace(commit=lambda: None)
    listing = asyncio.run(gateway.models_view(context, cast(Any, uow)))
    new = next(row for row in listing["models"] if row["model"] == "new-model")
    assert new["in_policy"] is False
    assert new["harnesses"] and new["harnesses"][0]["enabled"] is False
    monkeypatch.setattr(page, "_require", lambda *_args: (_principal(), "csrf"))
    monkeypatch.setattr(credentials, "read_secrets", AsyncMock(return_value={}))
    monkeypatch.setattr(
        gateway,
        "gateway_view",
        lambda *_args: {
            "last_outcome": None,
            "endpoint_url": URL,
            "key_set": True,
            "last_test": "not tested",
            "last_tested_at": None,
        },
    )
    monkeypatch.setattr(admin_routing, "routing_followers", lambda *_args: {})
    monkeypatch.setattr(gateway, "hermes_limits_view", lambda *_args: {})
    monkeypatch.setattr(page, "_hermes_limits_section", lambda *_args: {"title": "Limits"})
    monkeypatch.setattr(
        page,
        "_page",
        lambda *args, **kwargs: HTMLResponse(_render_documents(kwargs["sections"])),
    )
    response = asyncio.run(
        page.gateway_page(
            cast(Any, SimpleNamespace(query_params={"models": "1"})),
            cast(Any, SimpleNamespace(admin=context)),
            cast(Any, uow),
        )
    )
    html = bytes(response.body).decode()
    assert "<th>Model</th>" in html
    for harness in ("codex", "hermes", "qwen_code"):
        assert f'aria-label="use coder with {harness}"' in html
    assert 'aria-label="use new-model with hermes"' in html
    assert 'name="route.3.model" value="new-model"' in html
    form = {"confirm": "true"}
    index = 0
    for row in listing["models"]:
        for control in row["harnesses"]:
            prefix = f"route.{index}"
            form[f"{prefix}.model"] = row["model"]
            form[f"{prefix}.harness"] = control["harness"]
            # Enable the discovered model and disable Qwen's existing route.
            if control["harness"] != "qwen_code":
                form[f"{prefix}.enabled"] = "true"
            index += 1
    asyncio.run(
        page._action_gateway_models(
            cast(Any, None),
            "gateway-models",
            cast(Any, SimpleNamespace(admin=context)),
            cast(Any, uow),
            _principal(),
            "csrf",
            form,
            None,
        )
    )
    saved = {(entry["harness"], entry["model"]): entry for entry in published[0]["models"]}
    assert saved[("hermes", "new-model")]["enabled"] is True
    assert saved[("hermes", "new-model")]["chat_template_kwargs"]["enable_thinking"] is False
    for harness in ("hermes", "qwen_code", "codex"):
        assert saved[(harness, "coder")]["chat_template_kwargs"]["enable_thinking"] is (
            harness != "codex"
        )
    assert saved[("qwen_code", "coder")]["enabled"] is False


def test_downgrade_generates_unique_ids_and_keeps_endpoint_aliases() -> None:
    document = _document()
    entries = cast(list[dict[str, Any]], document["models"])
    entries.extend([_entry("hermes", "hermes:coder"), _entry("hermes", "qwen-coder")])
    entries[0]["vanished_at"] = "2026-10-07T00:00:00Z"
    legacy = m51._legacy(document)
    ids = [entry["id"] for entry in legacy["models"]]
    assert len(ids) == len(set(ids))
    assert [entry.get("model_name", entry["id"]) for entry in legacy["models"]] == [
        entry["model"] for entry in entries
    ]
    # model_name is written only where it differs from the id, as the legacy shape had it.
    assert all(entry.get("model_name") != entry["id"] for entry in legacy["models"])
    assert all("model" not in entry and "vanished_at" not in entry for entry in legacy["models"])
    restored = m51._current(legacy)
    assert [(e["harness"], e["model"]) for e in restored["models"]] == [
        (e["harness"], e["model"]) for e in entries
    ]


def test_migration_reference_renames_preserve_running_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercise the actual SQL against populated tables; this operation is portable.
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE executions (harness TEXT, model TEXT, policy_version INT)")
        )
        connection.execute(
            sa.text(
                "CREATE TABLE attempts (selected_harness TEXT, selected_model TEXT, "
                "routing_version INT)"
            )
        )
        for harness in ("hermes", "qwen_code", "codex"):
            connection.execute(
                sa.text("INSERT INTO executions VALUES (:harness, 'coder', 7)"),
                {"harness": harness},
            )
            connection.execute(
                sa.text("INSERT INTO attempts VALUES (:harness, 'coder', 8)"), {"harness": harness}
            )
        monkeypatch.setattr(op, "get_bind", lambda: connection)
        ids = m51._legacy_ids([_document()])
        m51._rename_references(ids)
        assert connection.execute(
            sa.text(
                "SELECT selected_harness, selected_model, routing_version "
                "FROM attempts ORDER BY selected_harness"
            )
        ).tuples().all() == [
            (harness, ids[(harness, "coder")], 8) for harness in ("codex", "hermes", "qwen_code")
        ]
        m51._rename_references(
            {(harness, legacy): model for (harness, model), legacy in ids.items()}
        )
        assert connection.execute(
            sa.text("SELECT harness, model, policy_version FROM executions ORDER BY harness")
        ).tuples().all() == [(harness, "coder", 7) for harness in ("codex", "hermes", "qwen_code")]
    engine.dispose()
