"""Regression coverage for the four FDY-0213 renewal review findings."""

from __future__ import annotations

import asyncio
import threading
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from crucible.adapters.harness.registry import default_registry
from crucible.adapters.persistence.migrations.versions import _0035_credential_renewer as migration
from crucible.application.admin import credentials
from crucible.application.harnesses import record_credential_observation
from crucible.application.supervisor import Supervisor
from crucible.cli import wiring
from crucible.domain.entities import ExecutionRole
from crucible.domain.events import EventKind
from crucible.ports.execution import ProbeResult
from crucible.ports.harness import CredentialSource, MountMode
from crucible.settings import CredentialSettings, Settings
from tests.unit.test_class_routing import NOW
from tests.unit.test_credential_renewer import FakeClock, _jwt, _login


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [MountMode.RENEWER, MountMode.RW_NARROW])
async def test_supervisor_persists_effective_credential_command(
    mode: MountMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("crucible.application.supervisor.load_attempt_routing", lambda *_: None)
    uow = Mock()
    uow.provider_settings.get.return_value = None
    # hades #489: the spec carries the task's operator notes; this task has none.
    uow.task_notes.list_for_task.return_value = []
    supervisor = object.__new__(Supervisor)
    supervisor._uow_factory = lambda: nullcontext(uow)  # type: ignore[assignment]
    supervisor._harnesses = default_registry()
    supervisor._credential_sources = {"codex": CredentialSource("", mode)}
    attempt: Any = SimpleNamespace(
        id="attempt",
        number=1,
        selected_harness="codex",
        selected_model="model",
        selected_image="image",
        resume_from_remote=False,
        routing_version=None,
    )
    execution: Any = SimpleNamespace(
        role=ExecutionRole.IMPLEMENT,
        harness="codex",
        model="model",
        image="image",
        policy_snapshot={},
        timeout_seconds=60,
        effort=None,
        provider="kubernetes",
    )
    task: Any = SimpleNamespace(id="task", external_id="FDY-0213", principal_id="tests")
    spec = await supervisor._build_spec(attempt, execution, task, {}, credential_mounted=True)
    if mode is MountMode.RENEWER:
        assert spec.command[0] == "/usr/local/bin/crucible-codex-host"
        assert spec.command[spec.command.index("--token-file") + 1].endswith("/access-token.json")
        assert spec.transcript_path is None
    else:
        assert spec.command[1] == "exec"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [MountMode.RENEWER, MountMode.RW_NARROW])
async def test_probe_persists_effective_credential_command(
    mode: MountMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = Mock()
    provider.probe_credential = AsyncMock(return_value=ProbeResult(0, "digest", "0.156.0", 1))
    ctx: Any = SimpleNamespace(
        harnesses=default_registry(),
        credential_sources={"codex": CredentialSource("", mode)},
        providers={"kubernetes": provider},
        probe_timeout_seconds=60,
        clock=FakeClock(NOW),
    )
    uow = Mock()
    uow.harnesses.get.return_value = None
    monkeypatch.setattr(credentials, "secret_store", lambda _: provider)
    monkeypatch.setattr(credentials, "probe_image", AsyncMock(return_value="image"))
    monkeypatch.setattr(credentials, "probe_route", lambda *_: ("model", "subscription", None))
    monkeypatch.setattr(credentials, "record_event_probe", Mock())
    record = await credentials._probe_async(
        ctx, uow, harness="codex", principal="admin", reason="test"
    )
    request = provider.probe_credential.call_args.args[0]
    assert record.mount_mode == mode.value
    if mode is MountMode.RENEWER:
        assert request.argv[0] == "/usr/local/bin/crucible-codex-host"
        assert request.argv[request.argv.index("--token-file") + 1].endswith("/access-token.json")
    else:
        assert request.argv[1] == "exec"


def test_record_credential_observation_accepts_renewer() -> None:
    uow = Mock()
    uow.harnesses.get.return_value = None
    state = record_credential_observation(
        uow, FakeClock(NOW), name="codex", mount_mode=MountMode("renewer"), changed=False, at=NOW
    )
    assert state.mount_mode_observed == "renewer"
    uow.harnesses.put.assert_called_once_with(state)


def test_migration_extends_mount_constraint_and_restores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = Mock()
    monkeypatch.setattr("alembic.op.execute", execute)
    migration.upgrade()
    assert "'renewer'" in execute.call_args.args[0]
    assert "ck_harnesses_mount_mode" in execute.call_args.args[0]
    execute.reset_mock()
    migration.downgrade()
    statements = [call.args[0] for call in execute.call_args_list]
    assert statements[0].startswith("UPDATE harnesses SET mount_mode_observed = NULL")
    constraint = next(s for s in statements if "ADD CONSTRAINT ck_harnesses_mount_mode" in s)
    assert "'renewer'" not in constraint


@pytest.mark.parametrize("mode", [None, "renewer", "rw-narrow"])
@pytest.mark.parametrize("directory", [True, False])
def test_wiring_constructs_only_the_effective_renewer(
    tmp_path: Path, mode: Any, directory: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(tmp_path / "auth.json", FakeClock(NOW))
    settings = Settings(
        credentials={
            "codex": CredentialSettings(
                path=str(tmp_path) if directory else None,
                mount_mode=mode,
            )
        }
    )
    # No Secret provider is needed to prove rollback exits before inspecting storage.
    store = Mock()
    monkeypatch.setattr(wiring, "FileCredentialStore", store)
    renewer = wiring.build_credential_renewer(settings, {}, Mock(), Mock(clock=FakeClock(NOW)))
    expected = directory and mode != "rw-narrow"
    assert (renewer is not None) is expected
    assert store.called is expected
    constructor = Mock()
    monkeypatch.setattr(wiring, "Supervisor", constructor)
    service = wiring.Wiring(settings, Mock(), {}, Mock(), Mock(), credential_renewer=renewer)
    service.supervisor()
    scheduled = constructor.call_args.kwargs["credential_renewal"]
    # The renewal callable now covers both pending requests and timer-based
    # renewal (339); it is a bound method that internally calls refresh_on_
    # request and refresh_if_due.  Check it is the wiring method.
    if renewer is not None:
        assert scheduled == service._credential_renewal
    else:
        assert scheduled is None
    if mode == "rw-narrow":
        assert wiring.credential_sources(settings)["codex"].mount_mode is MountMode.RW_NARROW


@pytest.mark.asyncio
async def test_due_renewer_sweep_from_thread_projects_and_records_on_service_loop(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    _login(tmp_path / "auth.json", clock)
    clock.value += timedelta(minutes=40)
    loop_thread = threading.get_ident()
    event_seen = asyncio.Event()
    projection_seen = asyncio.Event()
    uow = Mock()
    events: list[Any] = []

    def append(event: Any) -> None:
        assert threading.get_ident() == loop_thread
        events.append(event)
        event_seen.set()

    uow.events.append.side_effect = append
    provider = Mock()

    async def project(document: Any) -> None:
        assert threading.get_ident() == loop_thread
        assert set(document) == {"access_token", "account_id", "expires_at"}
        projection_seen.set()

    provider.refresh_credential_projection = project
    renewer = wiring.build_credential_renewer(
        Settings(credentials={"codex": CredentialSettings(path=str(tmp_path))}),
        {"fake": provider},
        Mock(side_effect=lambda: nullcontext(uow)),
        Mock(clock=clock),
    )
    assert renewer is not None
    renewer.grant = Mock(return_value={"access_token": _jwt(clock.now() + timedelta(hours=2))})
    supervisor = object.__new__(Supervisor)
    supervisor._clock = clock
    supervisor._credential_sweep = None
    supervisor._credential_renewal = renewer.refresh_if_due
    sweep_uow = Mock()
    sweep_uow.logs.attempts_with_logs_before.return_value = []
    sweep_uow.wakes.list_acked_before.return_value = []
    supervisor._fenced = Mock(side_effect=lambda: nullcontext(sweep_uow))  # type: ignore[method-assign]
    supervisor.fenced_token = 42
    assert await asyncio.to_thread(supervisor._retention_sweep) == 1
    await asyncio.wait_for(event_seen.wait(), 2)
    await asyncio.wait_for(projection_seen.wait(), 2)
    assert [event.kind for event in events] == [EventKind.CREDENTIAL_REFRESHED.value]
    assert await asyncio.to_thread(supervisor._retention_sweep) == 0
    renewer.grant.assert_called_once()
