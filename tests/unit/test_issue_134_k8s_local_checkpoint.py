"""Regression tests for local-origin quota checkpoints on Kubernetes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from tests.unit.kubernetes_fixtures import spec as kubernetes_spec
from tests.unit.test_issue_353_infrastructure_interruptions import _events, _running


def _prepare_checkpoint(
    monkeypatch: pytest.MonkeyPatch, repository_url: str
) -> tuple[Any, Any, Any, Mock]:
    supervisor, pending, uow, _attempts = _running(monkeypatch)
    pending.attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
    launch = replace(kubernetes_spec(), repository_url=repository_url)
    monkeypatch.setattr(supervisor, "_quota_checkpoint_safety", lambda _: (True, "safe"))
    monkeypatch.setattr(supervisor, "_spec_for", AsyncMock(return_value=launch))
    monkeypatch.setattr(supervisor, "_task_merged", lambda _: False)
    monkeypatch.setattr(supervisor, "_execution_provider_name", lambda _: "kubernetes")
    monkeypatch.setattr(supervisor, "_provider", lambda _: MagicMock(spec=[]))
    supervisor.delivery = MagicMock()
    supervisor.delivery.push_quota_checkpoint = AsyncMock(return_value=(True, "checkpoint pushed"))
    finish = MagicMock(wraps=supervisor._finish_deferred_quota)
    monkeypatch.setattr(supervisor, "_finish_deferred_quota", finish)
    return supervisor, pending.attempt, uow, finish


def test_kubernetes_local_origin_records_why_no_checkpoint_was_pushed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor, attempt, uow, finish = _prepare_checkpoint(monkeypatch, str(tmp_path / "origin"))

    asyncio.run(supervisor._complete_quota_checkpoint(attempt.id))

    supervisor.delivery.push_quota_checkpoint.assert_not_awaited()
    finish.assert_called_once()
    attempt_id, pushed, detail = finish.call_args.args
    assert attempt_id == attempt.id
    assert pushed is False
    assert "local-origin quota checkpoints are Docker-only" in detail
    assert finish.call_args.kwargs == {"checkpoint_skipped": True}
    reroute = next(event for event in _events(uow) if event.kind == EventKind.TASK_REROUTED.value)
    assert reroute.payload["checkpoint_push"] == "skipped"
    assert "local-origin quota checkpoints are Docker-only" in reroute.payload["checkpoint_detail"]


def test_kubernetes_github_origin_still_uses_delivery_checkpoint_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, attempt, _uow, finish = _prepare_checkpoint(
        monkeypatch, "https://github.com/example/repository.git"
    )
    asyncio.run(supervisor._complete_quota_checkpoint(attempt.id))

    supervisor.delivery.push_quota_checkpoint.assert_awaited_once_with(attempt.id, required=False)
    finish.assert_called_once_with(attempt.id, True, "checkpoint pushed")
