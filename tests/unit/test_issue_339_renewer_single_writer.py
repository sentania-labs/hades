"""Single-writer request and concurrency regression coverage (339, 405)."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.ui.pages import credentials as credentials_page_module
from crucible.application.credential_renewer import (
    CodexCredentialRenewer,
    KubernetesCredentialStore,
)
from crucible.application.errors import ConflictError
from crucible.cli import wiring
from crucible.domain.entities import Event, SupervisorStatus
from crucible.domain.events import EventKind
from crucible.settings import CredentialSettings, Settings


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


def _jwt(expiry: datetime) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': expiry.timestamp()})}.signature"


def _login_dict(clock: FakeClock) -> dict[str, Any]:
    return {
        "last_refresh": (clock.now() - timedelta(hours=1)).isoformat(),
        "tokens": {
            "access_token": _jwt(clock.now() + timedelta(hours=1)),
            "refresh_token": "fixture-refresh-value",
            "account_id": "account-fixture",
        },
    }


# ---------------------------------------------------------------------------
# AC1: serve --api constructs no grant-capable renewer
# ---------------------------------------------------------------------------


def test_api_status_cannot_mutate(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login_path = tmp_path / "auth.json"
    original_auth = json.dumps(_login_dict(clock))
    login_path.write_text(original_auth)
    settings = Settings(credentials={"codex": CredentialSettings(path=str(tmp_path))})
    ro = wiring._build_readonly_renewer(settings, {})
    assert ro is not None
    assert ro.dead is False
    assert ro.last_refresh == _login_dict(clock)["last_refresh"]
    assert not hasattr(ro, "refresh")
    assert not hasattr(ro, "write")
    assert not hasattr(ro, "mark_dead")
    assert login_path.read_text() == original_auth


# ---------------------------------------------------------------------------
# AC2: a refresh request recorded by the API is performed by the supervisor tick
# ---------------------------------------------------------------------------


class _FakeStore:
    """A minimal CredentialStore implementation for unit tests."""

    def __init__(
        self,
        read_fn: Callable[[], dict[str, Any]],
        is_dead_fn: Callable[[], bool],
        write_fn: Callable[[Mapping[str, Any]], None] | None = None,
        mark_dead_fn: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._read = read_fn
        self._is_dead = is_dead_fn
        self._write = write_fn or (lambda _doc: None)
        self._mark_dead = mark_dead_fn or (lambda _doc: None)

    def read(self) -> dict[str, Any]:
        return self._read()

    def write(self, document: Mapping[str, Any]) -> None:
        self._write(document)

    def is_dead(self) -> bool:
        return self._is_dead()

    def mark_dead(self, document: Mapping[str, Any]) -> None:
        self._mark_dead(document)


def test_supervisor_performs_refresh_on_pending_request() -> None:
    """refresh_on_request checks the checker and calls refresh when pending."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    grants: list[str] = []

    store = _FakeStore(
        read_fn=lambda: _login_dict(clock),
        is_dead_fn=lambda: False,
        write_fn=lambda doc: None,
    )

    def _grant(token: str) -> dict[str, str]:
        grants.append(token)
        return {"access_token": _jwt(clock.now() + timedelta(hours=2))}

    renewer = CodexCredentialRenewer(
        store=store,
        clock=clock,
        grant=_grant,
    )

    # No checker attached: refresh_on_request is a no-op.
    assert renewer.refresh_on_request() is False

    # Attach a checker that says "pending".
    renewer.set_pending_request_checker(lambda: True)
    assert renewer.refresh_on_request() is True
    assert len(grants) == 1


def test_supervisor_skips_refresh_when_no_pending_request() -> None:
    """When the checker returns False, refresh is not called."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    grants: list[str] = []

    store = _FakeStore(
        read_fn=lambda: _login_dict(clock),
        is_dead_fn=lambda: False,
        write_fn=lambda doc: None,
    )

    def _grant(token: str) -> dict[str, str]:
        grants.append(token)
        return {"access_token": _jwt(clock.now() + timedelta(hours=2))}

    renewer = CodexCredentialRenewer(
        store=store,
        clock=clock,
        grant=_grant,
    )

    renewer.set_pending_request_checker(lambda: False)
    assert renewer.refresh_on_request() is False
    assert len(grants) == 0


def test_supervisor_refresh_on_request_calls_grant_once_per_call() -> None:
    """Two pending-checker calls each trigger one grant invocation."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    counter = {"n": 0}

    store = _FakeStore(
        read_fn=lambda: _login_dict(clock),
        is_dead_fn=lambda: False,
        write_fn=lambda doc: None,
    )

    def counting_grant(token: str) -> dict[str, str]:
        counter["n"] += 1
        return {"access_token": _jwt(clock.now() + timedelta(hours=2))}

    renewer = CodexCredentialRenewer(
        store=store,
        clock=clock,
        grant=counting_grant,
    )

    renewer.set_pending_request_checker(lambda: True)

    renewer.refresh_on_request()
    assert counter["n"] == 1

    renewer.refresh_on_request()
    assert counter["n"] == 2


# ---------------------------------------------------------------------------
# AC3: stale resourceVersion makes the patch fail closed (409)
# ---------------------------------------------------------------------------


def test_k8s_store_rejects_stale_resource_version() -> None:
    """write() passes the read resourceVersion; a mismatching version yields 409."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login = _login_dict(clock)

    client = FakeKubernetesApi(namespace="crucible")
    client.create(
        "secrets",
        k8sspec.secret(
            name="crucible-harness-codex",
            namespace="crucible",
            object_labels={},
            data={"auth.json": json.dumps(login).encode()},
        ),
    )

    store = KubernetesCredentialStore(client)

    # read() caches the resourceVersion.
    store.read()
    cached_version = store._resource_version
    assert cached_version is not None

    # A separate real store installs a new login and bumps the fake's version.
    other = KubernetesCredentialStore(client)
    other_login = other.read()
    other_login["tokens"]["refresh_token"] = "replacement-login"
    other.write(other_login)
    assert other._resource_version != cached_version

    # Now write() should fail because our cached version is stale.
    with pytest.raises(KubernetesApiError) as exc_info:
        store.write({"last_refresh": clock.now().isoformat(), "tokens": {}})

    assert exc_info.value.status == 409


def test_k8s_store_patch_succeeds_when_version_matches() -> None:
    """write() succeeds when the resourceVersion has not changed."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login = _login_dict(clock)

    client = FakeKubernetesApi(namespace="crucible")
    client.create(
        "secrets",
        k8sspec.secret(
            name="crucible-harness-codex",
            namespace="crucible",
            object_labels={},
            data={"auth.json": json.dumps(login).encode()},
        ),
    )

    store = KubernetesCredentialStore(client)

    # Read to cache the version.
    store.read()
    stored_login = store.read()
    assert "last_refresh" in stored_login

    # No concurrent modification, so write should succeed.
    store.write({"last_refresh": clock.now().isoformat(), "tokens": {}})
    # Verify the data was updated.
    updated = store.read()
    assert updated["last_refresh"] == clock.now().isoformat()


# ---------------------------------------------------------------------------
# Corrections on Hades PR 358 (CI fix plus four Codex findings).
# ---------------------------------------------------------------------------


def test_migration_revision_ids_fit_alembic_version_column() -> None:
    """CI FIRST: alembic_version.version_num is varchar(32). A longer revision id
    fails every migration run with StringDataRightTruncation (e2e, e2e-kind,
    compose-smoke)."""
    package = "crucible.adapters.persistence.migrations.versions"
    versions_dir = (
        Path(__file__).resolve().parents[2]
        / "crucible"
        / "adapters"
        / "persistence"
        / "migrations"
        / "versions"
    )
    offenders = []
    for path in sorted(versions_dir.glob("_*.py")):
        if path.stem == "__init__":
            continue
        module = importlib.import_module(f"{package}.{path.stem}")
        if len(module.revision) > 32:
            offenders.append((path.name, module.revision))
    assert offenders == []


class _FakeEvents:
    """A minimal event store backed by a shared dict, keyed on an incrementing seq."""

    def __init__(self, shared: dict[str, Any]) -> None:
        self._shared = shared

    def append(self, event: Event) -> Event:
        self._shared["seq"] += 1
        event.seq = self._shared["seq"]
        self._shared["events"].append(event)
        return event

    def list_global(
        self, *, after_seq: int, kind: str | None, since: Any, limit: int
    ) -> list[Event]:
        rows = [e for e in self._shared["events"] if e.seq is not None and e.seq > after_seq]
        if kind is not None:
            rows = [e for e in rows if e.kind == kind]
        return rows[:limit]


class _FakeSupervisorStatuses:
    def __init__(self, shared: dict[str, Any]) -> None:
        self._shared = shared

    def get(self) -> SupervisorStatus:
        status: SupervisorStatus = self._shared["status"]
        return status

    def write(self, status: SupervisorStatus) -> None:
        self._shared["status"] = status


class _FakeCursorUow:
    """A UoW backed by a dict shared across the fake factory's instances, so a
    fresh renewer (a simulated supervisor restart) sees what the previous one
    persisted instead of starting over in memory."""

    def __init__(self, shared: dict[str, Any]) -> None:
        self.events = _FakeEvents(shared)
        self.supervisor_status = _FakeSupervisorStatuses(shared)

    def __enter__(self) -> _FakeCursorUow:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def commit(self) -> None:
        pass


def _shared_store() -> dict[str, Any]:
    return {
        "events": [],
        "seq": 0,
        "status": SupervisorStatus(holder=None, last_tick_at=None, tick_ms=None, counts={}),
    }


def _record_refresh_request(shared: dict[str, Any], clock: FakeClock) -> None:
    _FakeEvents(shared).append(
        Event(
            seq=None,
            ts=clock.now(),
            kind=EventKind.CREDENTIAL_REFRESH_REQUESTED.value,
            principal="admin",
            verified=True,
            payload={"harness": "codex", "reason": "administrator requested refresh"},
        )
    )


def test_refresh_request_cursor_persists_across_supervisor_restart(tmp_path: Path) -> None:
    """Codex finding (1): the cursor must survive a supervisor restart, or every
    historical CREDENTIAL_REFRESH_REQUESTED event replays and forces a grant."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login_path = tmp_path / "auth.json"
    login_path.write_text(json.dumps(_login_dict(clock)))
    settings = Settings(credentials={"codex": CredentialSettings(path=str(tmp_path))})
    shared = _shared_store()
    admin: Any = SimpleNamespace(clock=clock)
    factory: Any = lambda: _FakeCursorUow(shared)  # noqa: E731

    _record_refresh_request(shared, clock)

    renewer = wiring.build_credential_renewer(settings, {}, factory, admin)
    assert renewer is not None
    renewer.grant = lambda _token: {"access_token": _jwt(clock.now() + timedelta(hours=2))}
    assert renewer.refresh_on_request() is True
    assert shared["status"].refresh_request_cursor == 1

    # A fresh renewer (new in-memory closures) stands in for a supervisor restart.
    # The persisted cursor must stop it from replaying the handled request.
    restarted = wiring.build_credential_renewer(settings, {}, factory, admin)
    assert restarted is not None
    grants: list[str] = []

    def _record_and_grant(token: str) -> dict[str, str]:
        grants.append(token)
        return {"access_token": _jwt(clock.now() + timedelta(hours=3))}

    restarted.grant = _record_and_grant
    assert restarted.refresh_on_request() is False
    assert grants == []


def test_refresh_request_cursor_not_advanced_on_transient_failure(tmp_path: Path) -> None:
    """Codex finding (2): a transient OAuth/Secret/projection error must not advance
    the cursor, so the same request retries on the next tick."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login_path = tmp_path / "auth.json"
    login_path.write_text(json.dumps(_login_dict(clock)))
    settings = Settings(credentials={"codex": CredentialSettings(path=str(tmp_path))})
    shared = _shared_store()
    admin: Any = SimpleNamespace(clock=clock)
    factory: Any = lambda: _FakeCursorUow(shared)  # noqa: E731

    _record_refresh_request(shared, clock)

    renewer = wiring.build_credential_renewer(settings, {}, factory, admin)
    assert renewer is not None

    def flaky_grant(_token: str) -> dict[str, str]:
        raise RuntimeError("temporary network failure")

    renewer.grant = flaky_grant
    with pytest.raises(RuntimeError):
        renewer.refresh_on_request()

    # Transient: not durably terminal, so the cursor must not move.
    assert shared["status"].refresh_request_cursor is None
    assert renewer.dead is False

    # The next tick retries the same request and this time it succeeds.
    renewer.grant = lambda _token: {"access_token": _jwt(clock.now() + timedelta(hours=2))}
    assert renewer.refresh_on_request() is True
    assert shared["status"].refresh_request_cursor == 1


def test_credentials_refresh_refused_without_scheduled_renewer() -> None:
    """Codex finding (3): when no renewer is scheduled (rw-narrow mount, or
    build_credential_renewer returned None), refuse the refresh request with the
    previous configuration-conflict answer instead of recording an event no one
    will ever consume."""
    principal: Any = SimpleNamespace(name="admin")
    ctx: Any = SimpleNamespace(
        admin=SimpleNamespace(clock=FakeClock(datetime.now(UTC))), credential_renewer=None
    )
    uow = Mock()
    request: Any = None  # unused by the refresh branch

    with pytest.raises(ConflictError):
        asyncio.run(
            credentials_page_module._action_credential(
                request,
                "credential",
                ctx,
                uow,
                principal,
                "csrf",
                {"verb": "refresh"},
                None,
            )
        )
    uow.commit.assert_not_called()


def test_0036_downgrade_disables_append_only_and_fenced_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex finding (4): the downgrade must disable the append-only and fenced
    triggers around the DELETE of request events, as the other event-kind
    migrations do (0021, 0026, 0032, 0034)."""
    migration = importlib.import_module(
        "crucible.adapters.persistence.migrations.versions._0036_cred_refresh_request"
    )
    execute = Mock()
    monkeypatch.setattr("alembic.op.execute", execute)

    migration.downgrade()

    statements = [call.args[0] for call in execute.call_args_list]
    disable_index = statements.index("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    delete_index = next(i for i, s in enumerate(statements) if s.startswith("DELETE FROM events"))
    enable_index = statements.index("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    assert disable_index < delete_index < enable_index
    assert "ALTER TABLE events DISABLE TRIGGER trg_events_fenced" in statements[:delete_index]
    assert "ALTER TABLE events ENABLE TRIGGER trg_events_fenced" in statements[delete_index:]
