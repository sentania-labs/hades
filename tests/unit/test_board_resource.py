"""FDY-0585: the role-aware board document."""

from __future__ import annotations

from crucible.application.board_resource import board_resource
from crucible.domain.entities import Principal, Role
from tests.unit.test_board import NOW
from tests.unit.test_issue_489_board_lanes import fixture


def principal(role: Role) -> Principal:
    return Principal(id=f"p-{role.value}", name=role.value, role=role, created_at=NOW)


def test_board_document_has_approved_lanes_counts_and_local_times() -> None:
    uow, _calls = fixture(3)
    document = board_resource(uow, NOW, principal(Role.OPERATOR))
    assert [lane["key"] for lane in document["lanes"]] == [
        "inbox",
        "waiting_on_me",
        "stuck",
        "in_progress",
        "holding_pen",
        "wins",
        "graveyard",
    ]
    assert document["needs_me"] == document["counts"]["waiting_on_me"]
    assert not document["generated_at"].endswith(("Z", "+00:00"))


def test_observer_has_no_actions_and_collapsed_cards_load_on_demand() -> None:
    uow, _calls = fixture(3)
    document = board_resource(uow, NOW, principal(Role.OBSERVER), cards_for=frozenset({"wins"}))
    assert all(not lane["cards"] for lane in document["lanes"] if lane["key"] != "wins")
    assert all(not card["actions"] for lane in document["lanes"] for card in lane["cards"])
