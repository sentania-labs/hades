from __future__ import annotations

import base64
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.application.credential_renewer import (
    CodexCredentialRenewer,
    InvalidGrantError,
    KubernetesCredentialStore,
)
from crucible.cli import wiring
from crucible.domain.entities import Principal, Role
from crucible.domain.events import EventKind
from crucible.settings import CredentialSettings, Settings
from tests.unit.kubernetes_fixtures import build


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


def _jwt(expiry: datetime) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': expiry.timestamp()})}.signature"


def _login(path: Path, clock: FakeClock) -> None:
    path.write_text(
        json.dumps(
            {
                "last_refresh": (clock.now() - timedelta(hours=1)).isoformat(),
                "tokens": {
                    "access_token": _jwt(clock.now() + timedelta(hours=1)),
                    "refresh_token": "fixture-refresh-value",
                    "account_id": "account-fixture",
                },
            }
        )
    )


def test_t_auth_3_two_callers_make_one_grant_and_propagate(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login = tmp_path / "auth.json"
    _login(login, clock)
    grants = 0
    projections: list[dict[str, str]] = []
    events: list[EventKind] = []
    barrier = threading.Barrier(2)

    def grant(_refresh: str) -> dict[str, str]:
        nonlocal grants
        grants += 1
        return {"access_token": _jwt(clock.now() + timedelta(hours=2))}

    renewer = CodexCredentialRenewer(
        login,
        grant=grant,
        clock=clock,
        propagate=lambda value: projections.append(dict(value)),
        record=lambda kind, _payload: events.append(kind),
    )

    def call() -> None:
        barrier.wait()
        renewer.refresh("worker rejected token")

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert grants == 1
    assert len(projections) == 2
    assert events == [EventKind.CREDENTIAL_REFRESHED]
    assert login.stat().st_mode & 0o777 == 0o600
    assert set(projections[-1]) == {"access_token", "account_id", "expires_at"}


def test_t_auth_4_invalid_grant_marks_dead_and_wakes_once(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login = tmp_path / "auth.json"
    _login(login, clock)
    grants = 0
    events: list[EventKind] = []
    wakes: list[str] = []

    def grant(_refresh: str) -> dict[str, str]:
        nonlocal grants
        grants += 1
        raise InvalidGrantError("invalid_grant")

    renewer = CodexCredentialRenewer(
        login,
        grant=grant,
        clock=clock,
        record=lambda kind, _payload: events.append(kind),
        wake=wakes.append,
    )
    with pytest.raises(InvalidGrantError):
        renewer.refresh("timer")
    with pytest.raises(InvalidGrantError):
        renewer.refresh("timer")

    assert renewer.dead
    assert grants == 1
    assert events == [EventKind.CREDENTIAL_REFRESH_FAILED]
    assert len(wakes) == 1


def test_kubernetes_store_refreshes_the_service_held_secret() -> None:
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login = {
        "last_refresh": (clock.now() - timedelta(hours=1)).isoformat(),
        "tokens": {
            "access_token": _jwt(clock.now() + timedelta(hours=1)),
            "refresh_token": "fixture-refresh-value",
            "account_id": "account-fixture",
        },
    }
    client = FakeKubernetesApi(namespace="crucible")
    client.create(
        "secrets",
        k8sspec.secret(
            name="hades-harness-codex",
            namespace="crucible",
            object_labels={},
            data={"auth.json": json.dumps(login).encode()},
        ),
    )
    renewer = CodexCredentialRenewer(
        store=KubernetesCredentialStore(client),
        clock=clock,
        grant=lambda _token: {"access_token": _jwt(clock.now() + timedelta(hours=2))},
    )

    assert renewer.refresh("timer") is True
    stored_secret = client.get("secrets", "hades-harness-codex")
    stored = json.loads(base64.b64decode(stored_secret["data"]["auth.json"]))
    assert stored["last_refresh"] == clock.now().isoformat()


@pytest.mark.parametrize("mode", ["renewer", "rw-narrow"])
def test_wiring_uses_service_secret_when_codex_has_no_directory(mode: Any) -> None:
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login = {
        "last_refresh": (clock.now() - timedelta(hours=1)).isoformat(),
        "tokens": {
            "access_token": _jwt(clock.now() + timedelta(hours=1)),
            "refresh_token": "fixture-refresh-value",
            "account_id": "account-fixture",
        },
    }
    client, _registry, provider = build(harness="codex")
    client.create(
        "secrets",
        k8sspec.secret(
            name="hades-harness-codex",
            namespace="hades-workers",
            object_labels={},
            data={"auth.json": json.dumps(login).encode()},
        ),
    )

    settings = Settings(
        docker={"enabled": False},
        kubernetes={"enabled": True},
        credentials={"codex": CredentialSettings(mount_mode=mode)},
    )

    def unused_factory() -> None:
        return None

    renewer = wiring.build_credential_renewer(
        settings,
        {"kubernetes": provider},
        unused_factory,  # type: ignore[arg-type]
        type("Admin", (), {"clock": clock})(),
    )

    if mode == "rw-narrow":
        assert renewer is None
    else:
        assert renewer is not None
        assert isinstance(renewer.store, KubernetesCredentialStore)


def test_production_wiring_records_one_failure_event_and_one_durable_wake(
    tmp_path: Path,
) -> None:
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    credential_dir = tmp_path / "codex"
    credential_dir.mkdir()
    login = credential_dir / "auth.json"
    _login(login, clock)
    events: list[Any] = []
    wakes: list[Any] = []
    principal = Principal("01FOUNDRY000000000000000", "foundry", Role.ORCHESTRATOR, clock.now())

    class Uow:
        principals = type("Principals", (), {"list_all": lambda self: [principal]})()

        class Events:
            def append(self, event: Any) -> Any:
                events.append(event)
                return event

        events = Events()
        wakes = type("Wakes", (), {"add": lambda self, wake: wakes.append(wake)})()

        def __enter__(self) -> Uow:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def commit(self) -> None:
            return None

    settings = Settings(
        credentials={"codex": CredentialSettings(path=str(credential_dir), mount_mode="renewer")}
    )

    def factory() -> Uow:
        return Uow()

    renewer = wiring.build_credential_renewer(
        settings,
        {},
        factory,  # type: ignore[arg-type]
        type("Admin", (), {"clock": clock})(),
    )
    assert renewer is not None
    renewer.grant = lambda _token: (_ for _ in ()).throw(InvalidGrantError("invalid_grant"))

    with pytest.raises(InvalidGrantError):
        renewer.refresh("timer")
    with pytest.raises(InvalidGrantError):
        renewer.refresh("timer")

    assert [event.kind for event in events].count(EventKind.CREDENTIAL_REFRESH_FAILED.value) == 1
    assert len(wakes) == 1
    assert wakes[0].reason == "auth_failure"
    assert "Codex" in wakes[0].payload["summary"]
