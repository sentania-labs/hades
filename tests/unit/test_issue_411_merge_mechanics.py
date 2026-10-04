"""Hades #411: conflicts, main CI holds, and remote-head adoption."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from crucible.application.delivery_tick import DeliveryCoordinator, MainCIPlan
from crucible.application.release_hold import release_held
from crucible.domain.entities import HeadAction
from tests.fixtures import FakeClock
from tests.unit.test_issue_337_auto_merge import Host

NOW = datetime(2026, 10, 4, tzinfo=UTC)


def test_adopt_is_an_explicit_head_decision() -> None:
    assert HeadAction.ADOPT.value == "adopt"


def test_green_main_clears_the_release_hold() -> None:
    uow = MagicMock()
    held = SimpleNamespace(document={"held": True})
    uow.provider_settings.get.return_value = held
    coordinator = DeliveryCoordinator(Host(uow), FakeClock(NOW))
    plan = MainCIPlan("task", "org/repo", 1, "a" * 40, 411)

    coordinator._record_main_ci(plan, [])

    saved = uow.provider_settings.put.call_args.args[0]
    assert saved.document["held"] is False
    uow.provider_settings.get.return_value = saved
    assert release_held(uow) is False
