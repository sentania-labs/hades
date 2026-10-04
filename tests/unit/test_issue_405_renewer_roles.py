"""Real composition and two-store races for issue 405."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, assert_type
from unittest.mock import Mock

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.application.credential_renewer import (
    CodexCredentialRenewer,
    InvalidGrantError,
    KubernetesCredentialStore,
    ReadOnlyCredentialStore,
)
from crucible.application.errors import NotFoundError
from crucible.cli import admin, main, serve, wiring
from crucible.domain.events import EventKind
from crucible.ports.execution import ProviderError
from crucible.settings import Settings
from tests.unit.kubernetes_fixtures import build
from tests.unit.test_issue_339_renewer_single_writer import (
    FakeClock,
    _FakeCursorUow,
    _jwt,
    _login_dict,
    _record_refresh_request,
    _shared_store,
)

SECRET = "crucible-harness-codex"
NOW = datetime(2026, 10, 4, tzinfo=UTC)


def _secret(client: FakeKubernetesApi) -> None:
    client.create(
        "secrets",
        k8sspec.secret(
            name=SECRET,
            namespace=client.namespace,
            object_labels={},
            data={"auth.json": json.dumps(_login_dict(FakeClock(NOW))).encode()},
        ),
    )


@pytest.fixture(params=["file", "kubernetes"])
def configured(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    settings = Settings(
        database={"url": "sqlite://"},
        service={"artifact_root": str(tmp_path / "artifacts")},
        credentials={"codex": {"mount_mode": "renewer"}},
    )
    if request.param == "file":
        (tmp_path / "auth.json").write_text(json.dumps(_login_dict(FakeClock(NOW))))
        settings.credentials["codex"].path = str(tmp_path)
    else:
        client, _, provider = build(harness="codex")
        _secret(client)
        settings.kubernetes.enabled = True
        monkeypatch.setattr(wiring, "kubernetes_provider", lambda *a, **kw: provider)
        monkeypatch.setattr(wiring, "github_credentials", lambda _: None)
        monkeypatch.setattr(wiring, "first_run_delivery", lambda _: None)
    return settings


@pytest.mark.parametrize("role", ["api", "admin", "supervisor"])
def test_wire_builds_grant_only_for_supervisor(
    configured: Settings, role: wiring.ProcessRole, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = Mock(wraps=wiring.build_credential_renewer)
    monkeypatch.setattr(wiring, "build_credential_renewer", builder)
    composed = wiring.wire(configured, role=role)
    try:
        assert builder.call_count == (1 if role == "supervisor" else 0)
        assert (composed.credential_renewer is not None) == (role == "supervisor")
        status = composed.ctx.credential_renewer
        assert_type(status, ReadOnlyCredentialStore | None)
        assert status is not None
        assert not status.dead
        assert status.last_refresh == _login_dict(FakeClock(NOW))["last_refresh"]
        for method in ("write", "mark_dead", "refresh", "grant", "read"):
            assert not hasattr(status, method)
        for method in ("write", "mark_dead"):
            assert not hasattr(status._reader, method)
    finally:
        composed.ctx.engine.dispose()


def test_readonly_type_rejects_mutation(tmp_path: Path) -> None:
    probe = tmp_path / "readonly_probe.py"
    probe.write_text(
        "from crucible.adapters.api.deps import AppContext\n"
        "def probe(ctx: AppContext) -> None:\n"
        "    status = ctx.credential_renewer\n"
        "    assert status is not None\n"
        "    status.write({})\n"
        "    status.mark_dead({})\n"
        "    status.refresh('api', force=True)\n"
        "    status._reader.write({})\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--follow-imports=silent", str(probe)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.count("[attr-defined]") == 4, result.stdout


@pytest.mark.parametrize("flag", ["--api", "--supervisor", "--all"])
def test_serve_entry_uses_real_wire(
    configured: Settings, flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serve, "load_settings", lambda _: configured)
    # Exit at the migration gate, after real composition, without starting a server.
    monkeypatch.setattr(serve, "is_current", lambda *args: (False, "test gate"))
    builder = Mock(wraps=wiring.build_credential_renewer)
    monkeypatch.setattr(wiring, "build_credential_renewer", builder)
    composed_wire = Mock(wraps=wiring.wire)
    monkeypatch.setattr(serve, "wire", composed_wire)
    with pytest.raises(SystemExit) as exc:
        serve.main(["serve", flag])
    assert exc.value.code == 2
    expected = "api" if flag == "--api" else "supervisor"
    composed_wire.assert_called_once_with(configured, role=expected)
    assert builder.call_count == (0 if flag == "--api" else 1)


def test_admin_entry_uses_real_wire(configured: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin, "load_settings", lambda _: configured)
    builder = Mock(wraps=wiring.build_credential_renewer)
    monkeypatch.setattr(wiring, "build_credential_renewer", builder)
    composed_wire = Mock(wraps=wiring.wire)
    monkeypatch.setattr(admin, "wire", composed_wire)
    local = Mock(return_value={})
    monkeypatch.setattr(admin, "_local", local)
    args = main.build_parser().parse_args(["admin", "status"])
    admin.run(args, root_api_url=None, timezone=None)
    composed_wire.assert_called_once_with(configured, role="admin")
    builder.assert_not_called()
    assert local.call_args.args[1].credential_renewer is None


def test_metadata_conflict_preserves_rotated_tokens_with_two_stores() -> None:
    client = FakeKubernetesApi()
    _secret(client)
    supervisor = KubernetesCredentialStore(client)
    other = KubernetesCredentialStore(client)
    grants: list[str] = []
    events: list[EventKind] = []
    projections: list[Mapping[str, str]] = []
    clock = FakeClock(NOW)
    first_version = client.get("secrets", SECRET)["metadata"]["resourceVersion"]

    def grant(token: str) -> dict[str, str]:
        grants.append(token)
        if len(grants) == 1:
            # Another store writes the same login, then an operator labels the Secret.
            # Both real patches advance the version after the supervisor's read.
            other.write(other.read())
            client.patch("secrets", SECRET, {"metadata": {"labels": {"owner": "operator"}}})
        else:
            assert token == "rotated-once"
        return {
            "access_token": _jwt(clock.now() + timedelta(hours=2)),
            "refresh_token": "rotated-once" if len(grants) == 1 else "rotated-twice",
        }

    renewer = CodexCredentialRenewer(
        store=supervisor,
        clock=clock,
        grant=grant,
        record=lambda kind, _: events.append(kind),
        propagate=projections.append,
    )
    assert renewer.refresh("timer")
    assert len(grants) == 1
    saved = other.read()
    assert saved["tokens"]["refresh_token"] == "rotated-once"
    assert saved["last_refresh"] == NOW.isoformat()
    metadata = client.get("secrets", SECRET)["metadata"]
    assert int(metadata["resourceVersion"]) == int(first_version) + 3
    assert metadata["labels"]["owner"] == "operator"
    assert events == [EventKind.CREDENTIAL_REFRESHED]
    assert len(projections) == 1
    clock.value += timedelta(hours=2)
    assert renewer.refresh_if_due()
    assert other.read()["tokens"]["refresh_token"] == "rotated-twice"
    assert not renewer.dead


def test_new_login_wins_conflict_with_two_stores() -> None:
    client = FakeKubernetesApi()
    _secret(client)
    supervisor = KubernetesCredentialStore(client)
    admin_store = KubernetesCredentialStore(client)
    record, propagate = Mock(), Mock()

    def grant(_: str) -> dict[str, str]:
        new_login = admin_store.read()
        new_login["tokens"]["refresh_token"] = "new-interactive-login"
        admin_store.write(new_login)
        return {"refresh_token": "rotated-old-login"}

    renewer = CodexCredentialRenewer(
        store=supervisor, clock=FakeClock(NOW), grant=grant, record=record, propagate=propagate
    )
    with pytest.raises(KubernetesApiError) as exc:
        renewer.refresh("timer")
    assert exc.value.status == 409
    assert admin_store.read()["tokens"]["refresh_token"] == "new-interactive-login"
    assert not renewer.dead
    record.assert_not_called()
    propagate.assert_not_called()


def test_stale_dead_marker_cannot_poison_new_login() -> None:
    client = FakeKubernetesApi()
    _secret(client)
    supervisor = KubernetesCredentialStore(client)
    admin_store = KubernetesCredentialStore(client)
    supervisor.read()
    login = admin_store.read()
    login["tokens"]["refresh_token"] = "new-interactive-login"
    admin_store.write(login)
    with pytest.raises(KubernetesApiError) as exc:
        supervisor.mark_dead({"dead": True})
    assert exc.value.status == 409
    assert not admin_store.is_dead()


def test_invalid_grant_retries_dead_marker_after_unrelated_edit() -> None:
    client = FakeKubernetesApi()
    _secret(client)
    supervisor = KubernetesCredentialStore(client)
    record, wake = Mock(), Mock()

    def grant(_: str) -> Mapping[str, Any]:
        client.patch("secrets", SECRET, {"metadata": {"labels": {"owner": "operator"}}})
        raise InvalidGrantError("invalid_grant")

    renewer = CodexCredentialRenewer(
        store=supervisor,
        clock=FakeClock(NOW),
        grant=grant,
        record=record,
        wake=wake,
    )
    with pytest.raises(InvalidGrantError, match="invalid_grant"):
        renewer.refresh("timer")
    assert supervisor.is_dead()
    record.assert_called_once_with(
        EventKind.CREDENTIAL_REFRESH_FAILED,
        {
            "harness": "codex",
            "reason": "invalid_grant: the login was revoked or refreshed by another session",
            "result": "credential_dead",
        },
    )
    wake.assert_called_once()


def test_invalid_grant_dead_marker_refuses_new_login() -> None:
    client = FakeKubernetesApi()
    _secret(client)
    supervisor = KubernetesCredentialStore(client)
    admin_store = KubernetesCredentialStore(client)
    record, wake = Mock(), Mock()

    def grant(_: str) -> Mapping[str, Any]:
        login = admin_store.read()
        login["tokens"]["refresh_token"] = "new-interactive-login"
        admin_store.write(login)
        raise InvalidGrantError("invalid_grant")

    renewer = CodexCredentialRenewer(
        store=supervisor,
        clock=FakeClock(NOW),
        grant=grant,
        record=record,
        wake=wake,
    )
    with pytest.raises(KubernetesApiError) as exc:
        renewer.refresh("timer")
    assert exc.value.status == 409
    assert admin_store.read()["tokens"]["refresh_token"] == "new-interactive-login"
    assert not supervisor.is_dead()
    record.assert_not_called()
    wake.assert_not_called()


def test_admin_login_write_rejects_a_concurrent_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, provider = build(harness="codex")
    _secret(client)
    supervisor = KubernetesCredentialStore(client)
    refreshed = supervisor.read()
    refreshed["tokens"]["refresh_token"] = "rotated-during-login-write"
    patch = client.patch

    def concurrent_patch(
        kind: str, name: str, body: Mapping[str, Any], *, resource_version: str | None = None
    ) -> dict[str, Any]:
        if "metadata" in body:
            supervisor.write(refreshed)
        return patch(kind, name, body, resource_version=resource_version)

    monkeypatch.setattr(client, "patch", concurrent_patch)
    with pytest.raises(ProviderError, match="409"):
        provider.write_credential_files(
            "codex", {"auth.json": json.dumps(_login_dict(FakeClock(NOW))).encode()}
        )
    assert supervisor.read()["tokens"]["refresh_token"] == "rotated-during-login-write"


def test_wire_coalesces_requests_and_leaves_arrivals_during_grant_pending(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = _shared_store()
    monkeypatch.setattr(wiring, "SqlUnitOfWorkFactory", lambda _: lambda: _FakeCursorUow(shared))
    monkeypatch.setattr(wiring, "local_endpoint_view", Mock(side_effect=NotFoundError("no policy")))
    clock = FakeClock(NOW)
    for _ in range(1001):
        _record_refresh_request(shared, clock)
    composed = wiring.wire(configured, role="supervisor")
    renewer = composed.credential_renewer
    assert renewer is not None
    grants = 0

    def grant(_: str) -> dict[str, str]:
        nonlocal grants
        grants += 1
        if grants == 1:
            _record_refresh_request(shared, clock)
        return {"access_token": _jwt(renewer.clock.now() + timedelta(hours=2))}

    renewer.grant = grant
    assert composed._credential_renewal()
    assert grants == 1
    assert shared["status"].refresh_request_cursor == 1001
    assert composed._credential_renewal()
    assert grants == 2
    assert shared["status"].refresh_request_cursor == 1002
    assert not composed._credential_renewal()
    assert grants == 2


def test_wire_refresh_failure_does_not_escape_tick_callback(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = _shared_store()
    monkeypatch.setattr(wiring, "SqlUnitOfWorkFactory", lambda _: lambda: _FakeCursorUow(shared))
    monkeypatch.setattr(wiring, "local_endpoint_view", Mock(side_effect=NotFoundError("no policy")))
    _record_refresh_request(shared, FakeClock(NOW))
    composed = wiring.wire(configured, role="supervisor")
    renewer = composed.credential_renewer
    assert renewer is not None
    grant = Mock(side_effect=RuntimeError("temporary grant failure"))
    renewer.grant = grant
    assert not composed._credential_renewal()
    grant.assert_called_once()
    assert shared["status"].refresh_request_cursor is None
    assert not renewer.dead
