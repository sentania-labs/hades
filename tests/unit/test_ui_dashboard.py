"""Readiness gate, dashboard steps, fifteen sections and version row."""

from __future__ import annotations

import asyncio
import html
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

import crucible
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.persistence.migrations.versions._0001_walking_skeleton import (
    DEFAULT_POLICY,
)
from crucible.adapters.persistence.migrations.versions._0008_harness_adapters import (
    VERIFIED_ROUTING,
)
from crucible.adapters.ui.pages import dashboard
from crucible.adapters.ui.render import (
    _document_section,
    _localize,
    about_rows,
)
from crucible.application.admin import audit as audit_service
from crucible.application.admin import bootstrap as bootstrap_service
from crucible.application.admin import github as github_service
from crucible.application.admin import providers as providers_service
from crucible.application.admin import routing as routing_service
from crucible.application.admin import setup as setup_service
from crucible.application.admin import status as status_service
from crucible.domain.entities import (
    BootstrapImport,
    Event,
    Lease,
    PoolExhaustion,
    RetentionAction,
    SupervisorStatus,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from tests.unit.admin_ui_fixtures import (
    NOW,
    _document_leaves,
    _harness,
    _lease,
    _panel_leaves,
    _readiness,
    _render_documents,
    _status,
    _strict_status,
    _supervisor_document,
)


def test_healthy_supervisor_adds_no_readiness_step(monkeypatch: pytest.MonkeyPatch) -> None:
    document = {
        "supervisor": _supervisor_document(_lease(), _status()),
        "harnesses": [_harness("hermes")],
        "providers": [],
    }

    readiness = _readiness(
        document, monkeypatch, repositories=[object()], enabled_models={"hermes"}
    )

    assert readiness["steps"] == []
    assert readiness["ready"] is True
    assert readiness["ready_harnesses"] == ["hermes"]


@pytest.mark.parametrize(
    ("lease", "supervisor_status", "cause"),
    [
        (None, _status(), "no supervisor lease"),
        (
            _lease(),
            _status(last_success_at=NOW - timedelta(seconds=31)),
            "no successful tick within the lease window",
        ),
        (
            _lease(),
            _status(
                last_success_at=NOW - timedelta(seconds=1),
                last_error_at=NOW,
                last_error="RuntimeError: test failure",
            ),
            "last tick failed: RuntimeError: test failure",
        ),
    ],
)
def test_each_unhealthy_supervisor_shape_adds_one_cause_specific_step(
    lease: Lease | None,
    supervisor_status: SupervisorStatus,
    cause: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "supervisor": _supervisor_document(lease, supervisor_status),
        "harnesses": [_harness("hermes")],
        "providers": [],
    }

    readiness = _readiness(
        document, monkeypatch, repositories=[object()], enabled_models={"hermes"}
    )

    assert [step["code"] for step in readiness["steps"]] == ["supervisor"]
    assert cause in readiness["steps"][0]["text"]
    assert readiness["ready"] is False


def test_hermes_steps_name_the_real_blocker_and_the_page_that_fixes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """crucible#123: the lab's state on 2026-09-25. A key is set but was never verified,
    the gateway URL is not set, no local model is enabled; the script harness is a test
    fixture and never a to-do."""
    document = {
        "supervisor": _supervisor_document(_lease(), _status()),
        "harnesses": [
            _harness("hermes"),
            _harness(
                "script-harness", images=[{"promotion_state": "candidate"}], default_image=None
            ),
            _harness("codex", enabled_by_configuration=False, reason="unverified"),
        ],
        "providers": [],
    }

    readiness = _readiness(
        document, monkeypatch, credential_state="configured", endpoint=None, enabled_models=set()
    )

    names = [h["name"] for h in readiness["harnesses"]]
    assert "script-harness" not in names
    codex = next(h for h in readiness["harnesses"] if h["name"] == "codex")
    assert codex["state"] == "off" and codex["steps"] == []
    hermes = next(h for h in readiness["harnesses"] if h["name"] == "hermes")
    codes = [step["code"] for step in hermes["steps"]]
    assert codes == ["endpoint_not_configured", "credential_not_verified", "no_enabled_model"]
    assert {step["fix"] for step in hermes["steps"]} == {"/ui/gateway"}
    assert "The gateway URL for hermes is not set" in hermes["steps"][0]["text"]
    assert [step["code"] for step in readiness["steps"]] == ["no_repository", "no_ready_harness"]
    assert all("script-harness" not in step["text"] for step in readiness["steps"])


@pytest.mark.parametrize(
    ("api_base", "repository_url"),
    [
        ("https://api.github.com", "https://github.com/o/r"),
        ("https://API.GitHub.COM", "https://GitHub.COM/o/r"),
        ("https://ghe.example.internal/api/v3", "https://ghe.example.internal/o/r"),
        ("https://GHE.example.internal/api/v3", "https://ghe.EXAMPLE.internal/o/r"),
    ],
)
def test_github_repository_without_connected_app_adds_readiness_step(
    monkeypatch: pytest.MonkeyPatch, api_base: str, repository_url: str
) -> None:
    """Issue #141: repositories on the configured GitHub host need a connected App."""
    document: dict[str, Any] = {
        "supervisor": _supervisor_document(_lease(), _status()),
        "harnesses": [_harness("hermes")],
        "providers": [],
        "github": {"configured": False, "api_base": api_base},
    }
    readiness = _readiness(
        document,
        monkeypatch,
        repositories=[SimpleNamespace(url=repository_url)],
        enabled_models={"hermes"},
    )
    assert readiness["steps"] == [
        {
            "code": "github_app_not_connected",
            "text": "A GitHub repository is registered but no GitHub App is connected.",
            "fix": "/ui/github",
        }
    ]
    assert readiness["ready"] is False

    document["github"]["configured"] = True
    readiness = _readiness(
        document,
        monkeypatch,
        repositories=[SimpleNamespace(url=repository_url)],
        enabled_models={"hermes"},
    )
    assert readiness["steps"] == []
    assert readiness["ready"] is True

    document["github"]["configured"] = False
    for unrelated_url in (
        "https://gitlab.example.com/o/r",
        "https://github.com.example.com/o/r",
        "https://example.com/github.com/o/r",
    ):
        readiness = _readiness(
            document,
            monkeypatch,
            repositories=[SimpleNamespace(url=unrelated_url)],
            enabled_models={"hermes"},
        )
        assert readiness["steps"] == []
        assert readiness["ready"] is True

    del document["github"]
    readiness = _readiness(
        document,
        monkeypatch,
        repositories=[SimpleNamespace(url="https://github.com/o/r")],
        enabled_models={"hermes"},
    )
    assert readiness["steps"][0]["code"] == "github_app_not_connected"
    assert readiness["ready"] is False


def test_a_missing_credential_and_image_are_named_per_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "supervisor": _supervisor_document(_lease(), _status()),
        "harnesses": [
            _harness(
                "claude_code",
                enabled_by_administrator=False,
                images=[{"promotion_state": "candidate"}],
                default_image=None,
            )
        ],
        "providers": [
            {"checks": {"local_endpoint_reachable": False, "local_endpoint_detail": "refused"}}
        ],
    }

    readiness = _readiness(document, monkeypatch, credential_state="absent")

    steps = readiness["harnesses"][0]["steps"]
    assert [step["code"] for step in steps] == [
        "disabled",
        "credential_missing",
        "no_enabled_model",
        "no_promoted_image",
    ]
    assert [step["fix"] for step in steps] == [
        "/ui/harnesses",
        "/ui/credentials",
        "/ui/routing",
        "/ui/images",
    ]


def test_a_promoted_image_no_provider_lists_is_named_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex on PR 164: the harness has a default row, but no listed image is it."""
    document = {
        "supervisor": _supervisor_document(_lease(), _status()),
        "harnesses": [_harness("hermes", images=[{"promotion_state": "candidate"}])],
        "providers": [],
    }

    readiness = _readiness(
        document, monkeypatch, repositories=[object()], enabled_models={"hermes"}
    )

    hermes = readiness["harnesses"][0]
    assert hermes["state"] == "not_ready"
    assert [step["code"] for step in hermes["steps"]] == ["promoted_image_missing"]
    assert hermes["steps"][0]["text"] == (
        "The promoted image for hermes (w:1) is no longer in the registry, or the "
        "registry did not answer. Promote another on Images."
    )
    assert hermes["steps"][0]["fix"] == "/ui/images"
    assert readiness["ready"] is False


async def test_gap_logic_reads_only_keys_from_real_status_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def list_images(_ctx: Any) -> list[Any]:
        return []

    async def provider_status(_ctx: Any) -> dict[str, Any]:
        return {}

    harnesses = [
        _harness("disabled", enabled=False, enabled_by_administrator=False),
        _harness("unpromoted", images=[{"promotion_state": "candidate"}], default_image=None),
    ]
    supervisor = _supervisor_document(None, _status())
    status_module = cast(Any, status_service)
    monkeypatch.setattr(status_service, "list_images", list_images)

    async def read_harnesses(_ctx: Any, _uow: Any, _images: Any) -> tuple[Any, dict[str, Any]]:
        return harnesses, {}

    async def read_stored(_ctx: Any) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(status_service, "read_harnesses", read_harnesses)
    monkeypatch.setattr(status_module.github, "read_stored", read_stored)
    monkeypatch.setattr(status_service, "providers_status", provider_status)
    monkeypatch.setattr(
        status_service,
        "supervisor_view",
        lambda *_args: SimpleNamespace(model_dump=lambda **_kwargs: supervisor),
    )
    monkeypatch.setattr(status_module.github, "status", lambda _ctx, _uow, **_kwargs: {})
    monkeypatch.setattr(status_service, "workers", lambda _uow: [])
    monkeypatch.setattr(status_service, "tasks", lambda _uow: {})
    monkeypatch.setattr(status_service, "wakes", lambda _uow: {})
    monkeypatch.setattr(status_service, "retention", lambda _uow: {})
    monkeypatch.setattr(status_module.bootstrap, "status_part", lambda _uow: {})
    monkeypatch.setattr(status_module.audit, "tail", lambda _uow, **_kwargs: {"next_cursor": None})
    computed: dict[str, Any] = {}

    def readiness(
        ctx: Any, uow: Any, document: dict[str, Any], secrets: Any = None
    ) -> dict[str, Any]:
        result = _readiness(_strict_status(document), monkeypatch)
        computed["readiness"] = result
        return result

    monkeypatch.setattr(status_service, "readiness", readiness)
    document = await status_service.status(
        cast(
            Any,
            SimpleNamespace(
                providers={}, clock=SimpleNamespace(now=lambda: NOW), lease_ttl_seconds=30
            ),
        ),
        cast(Any, SimpleNamespace()),
    )

    steps = [
        step["code"]
        for part in (document["readiness"], *document["readiness"]["harnesses"])
        for step in part["steps"]
    ]
    assert steps == [
        "supervisor",
        "no_repository",
        "no_ready_harness",
        "disabled",
        "no_enabled_model",
        "no_enabled_model",
        "no_promoted_image",
    ]


def test_all_fifteen_sections_preserve_real_service_output_shapes() -> None:
    blocked_task = SimpleNamespace(
        id="01BLOCKEDTASK00000000000000",
        external_id="FDY-READABLE",
        updated_at=NOW,
    )
    tasks_uow = SimpleNamespace(
        tasks=SimpleNamespace(
            list_by_state=lambda state: [blocked_task] if state is TaskState.BLOCKED else []
        )
    )
    wake = SimpleNamespace(created_at=NOW)
    wakes_uow = SimpleNamespace(
        principals=SimpleNamespace(
            list_all=lambda: [SimpleNamespace(id="principal-1", name="operator")]
        ),
        wakes=SimpleNamespace(
            list_for_principal=lambda *_args, **_kwargs: [wake],
            count_unacked=lambda: 1,
            count_unacked_for_principal=lambda _principal_id: 1,
        ),
    )
    retention_action = RetentionAction(
        id="01RETENTION000000000000000",
        kind="logs_removed",
        subject="attempt-1",
        policy_name="default-software",
        policy_version=1,
        acted_at=NOW,
        detail={"bytes": 4096},
    )
    retention_uow = SimpleNamespace(
        retention=SimpleNamespace(list_recent=lambda _limit: [retention_action])
    )
    exhaustion = PoolExhaustion(
        pool="openai-sub",
        exhausted_at=NOW,
        reset_at=NOW + timedelta(hours=5),
        task_id="01BLOCKEDTASK00000000000000",
        attempt_id="01ATTEMPT0000000000000000",
        reason="soft limit reached",
    )
    routing_document = routing_service.list_exhaustions(
        cast(Any, SimpleNamespace(clock=SimpleNamespace(now=lambda: NOW))),
        cast(
            Any,
            SimpleNamespace(pool_exhaustions=SimpleNamespace(list_all=lambda: [exhaustion])),
        ),
    )
    provider_document = asyncio.run(
        providers_service.providers_status(
            cast(Any, SimpleNamespace(providers={"fake": FakeProvider()}))
        )
    )
    github_document = github_service.status(
        cast(
            Any,
            SimpleNamespace(
                github=object(),
                github_app=SimpleNamespace(
                    app_id=1234,
                    api_base="https://api.github.com",
                    private_key_path=None,
                    webhook_secret_path=None,
                    webhook_enabled=True,
                ),
            ),
        ),
        cast(
            Any,
            SimpleNamespace(
                repositories=SimpleNamespace(
                    list_all=lambda: [
                        SimpleNamespace(name="sentania-labs/crucible", installation_id=55)
                    ]
                ),
                events=SimpleNamespace(list_global=lambda **_kwargs: []),
            ),
        ),
    )
    manifest = {
        "import_id": "01IMPORT000000000000000000",
        "state": "verified",
        "counts": {"tasks": 2, "events": 3},
        "principal": "operator",
        "verified_at": NOW.isoformat(),
        "committed_at": None,
        "tasks": [{"external_id": "FDY-1", "state": "closed"}],
    }
    import_record = BootstrapImport(
        id=str(manifest["import_id"]),
        state="verified",
        schema_version="1.0",
        content_sha256="a" * 64,
        source_sha256="b" * 64,
        source={"kind": "foundry-ledger"},
        manifest=manifest,
        principal_id="principal-1",
        imported_by="admin",
        verified_at=NOW,
    )
    bootstrap_uow = cast(
        Any,
        SimpleNamespace(
            bootstrap_imports=SimpleNamespace(
                list_all=lambda: [import_record],
                get=lambda import_id: import_record if import_id == import_record.id else None,
            )
        ),
    )
    imports_document = bootstrap_service.list_imports(bootstrap_uow)
    manifest_document = bootstrap_service.show(bootstrap_uow, import_record.id)
    audit_event = Event(
        seq=27,
        ts=NOW,
        kind=EventKind.HARNESS_ENABLED.value,
        principal="admin",
        verified=True,
        payload={"harness": "codex", "reason": "operator enabled"},
    )

    def audit_rows(*, after_seq: int, **_kwargs: Any) -> list[Event]:
        return [audit_event] if after_seq < (audit_event.seq or 0) else []

    audit_document = audit_service.tail(
        cast(Any, SimpleNamespace(events=SimpleNamespace(list_global=audit_rows))),
        cursor=0,
        limit=100,
    )
    task_document = status_service.tasks(cast(Any, tasks_uow))
    wake_document = status_service.wakes(cast(Any, wakes_uow))
    retention_document = status_service.retention(cast(Any, retention_uow))
    documents: list[tuple[str, Any | None]] = [
        ("Supervisor", _supervisor_document(_lease(), _status())),
        ("Providers", provider_document),
        ("Status task state", task_document),
        ("Pending wakes", wake_document),
        ("Active policy", DEFAULT_POLICY),
        ("Routing policy", VERIFIED_ROUTING),
        ("Pool exhaustion", routing_document),
        ("App and repository connectivity", github_document),
        ("Task state", task_document),
        ("Wakes", wake_document),
        ("Summary", retention_document),
        ("Next cursor", {"next_cursor": audit_document["next_cursor"]}),
        ("Imports", imports_document),
        ("Tail", None),
        ("Manifest", manifest_document),
    ]

    assert len(documents) == 15
    for title, document in documents:
        if document is None:
            rendered = _render_documents([{"title": title, "text": "worker output"}])
            assert "worker output" in rendered
            continue
        section = _document_section(title, document)
        panel = cast(dict[str, Any], _localize(section["panel"], "America/Chicago"))
        rendered = html.unescape(_render_documents([section]))
        assert len(_panel_leaves(panel)) == len(_document_leaves(document)), title
        for leaf in _panel_leaves(panel):
            values = leaf if isinstance(leaf, list) else [leaf]
            for value in values:
                assert str(value) in rendered, (title, value)


def test_the_harnesses_word_is_the_first_readiness_step() -> None:
    """Review of the first-run integration: the Harnesses page and Status agree."""
    from crucible.adapters.ui.pages.harnesses import _harness_status  # noqa: PLC0415

    item = _harness("hermes")
    gap = {"code": "endpoint_not_configured", "text": "The gateway URL is not set.", "fix": "/"}
    assert _harness_status(item, {"steps": [gap]})["value"] == "needs the gateway"
    assert _harness_status(item, {"steps": []})["value"] == "ready"
    assert _harness_status(_harness("script-harness", default_image=None), None)["value"] == (
        "needs an image"
    )


def test_the_about_block_shows_the_version_first() -> None:
    """hades #214: Status folded into the Admin About block, which leads with the running
    version and names the image digest the deployment reports."""
    digest = "sha256:" + "1" * 64
    rows = about_rows(SimpleNamespace(service=SimpleNamespace(image=f"hades:1@{digest}")))
    assert rows[0][0] == "Version"
    assert rows[0][1]["value"] == crucible.__version__
    assert rows[1][0] == "Image digest" and rows[1][1]["value"] == digest
    unreported = about_rows(SimpleNamespace(service=SimpleNamespace(image="")))
    assert unreported[1][1]["value"] == "not reported"


def test_ui_sends_the_operator_to_set_up_or_the_board(monkeypatch: pytest.MonkeyPatch) -> None:
    """hades #576 U5: /ui was Status; it now leads to Set up while a step is undone."""
    principal = SimpleNamespace(name="reader", role=SimpleNamespace(value="observer"))
    monkeypatch.setattr(dashboard, "_require", lambda *args, **kwargs: (principal, "csrf"))
    steps = [{"done": False}]
    monkeypatch.setattr(setup_service, "setup_steps", lambda _ctx, _uow: steps)
    request = Request({"type": "http", "method": "GET", "path": "/ui", "headers": []})
    ctx = cast(Any, SimpleNamespace(admin=object()))
    response = dashboard.dashboard(request, ctx, cast(Any, SimpleNamespace()))
    assert response.headers["location"] == "/ui/setup"
    steps[0]["done"] = True
    response = dashboard.dashboard(request, ctx, cast(Any, SimpleNamespace()))
    assert response.headers["location"] == "/ui/board"
