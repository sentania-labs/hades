"""The card page keeps every operator action the earlier task page offered (hades #576,
#424, ADR 0025), each as one click: the board moves, the proposal answers and the
delivery waivers. One row per task state that offers anything."""

from __future__ import annotations

import pytest

from crucible.adapters.ui.pages.board import card_page_actions
from crucible.application.board_actions import moves_for_card
from crucible.domain.entities import Role
from crucible.domain.lifecycle import TaskState
from crucible.domain.waivers import WAIVABLE_STATES
from tests.unit.test_issue_489_card_actions import (
    ADMIN,
    OBSERVER,
    TASK_ID,
    _task,
    render_card_html,
    store_for,
)

WAIVERS = [
    "Waive the remaining external review rounds",
    "Accept that this repository has no CI",
]


def _labels(state: TaskState, role: Role) -> list[str]:
    moves = moves_for_card(_task(state), escalation=None, pull_request=None)
    return [item["label"] for item in card_page_actions(TASK_ID, state, role, moves)]


def _moves(state: TaskState) -> list[dict[str, str]]:
    return moves_for_card(_task(state), escalation=None, pull_request=None)


def _move_labels(state: TaskState) -> list[str]:
    return [m["label"] for m in _moves(state)]


def test_proposed_card_offers_the_board_moves_and_the_proposal_answers() -> None:
    labels = _labels(TaskState.PROPOSED, Role.OPERATOR)
    moves = _move_labels(TaskState.PROPOSED)
    assert labels[: len(moves)] == moves
    assert "Approve with note" in labels
    assert "Send back" in labels
    # Reject comes from the board's own Decline when the board offers it.
    assert ("Reject" in labels) != ("decline" in {m["key"] for m in _moves(TaskState.PROPOSED)})


@pytest.mark.parametrize("state", sorted(WAIVABLE_STATES, key=str))
def test_an_admin_sees_both_waivers_while_a_pull_request_waits(state: TaskState) -> None:
    labels = _labels(state, Role.ADMIN)
    assert labels == [*_move_labels(state), *WAIVERS]
    assert not set(WAIVERS) & set(_labels(state, Role.OPERATOR))


@pytest.mark.parametrize(
    "state",
    [state for state in TaskState if _move_labels(state) or state is TaskState.PROPOSED],
    ids=lambda s: s.value,
)
def test_each_state_with_an_action_lists_it_for_operators_and_none_for_observers(
    state: TaskState,
) -> None:
    labels = _labels(state, Role.OPERATOR)
    assert labels, state
    assert labels[: len(_move_labels(state))] == _move_labels(state)
    assert _labels(state, Role.OBSERVER) == []


def test_every_card_action_is_a_click_with_an_optional_note_and_remove_like_confirms() -> None:
    for state in TaskState:
        moves = moves_for_card(_task(state), escalation=None, pull_request=None)
        for item in card_page_actions(TASK_ID, state, Role.ADMIN, moves):
            assert item["note"] is None or "optional" in item["note"], item
            if item["key"] in {"cancel", "decline", "reject"}:
                assert item["confirm"], item
            assert item["action"].startswith("/ui/"), item


def test_the_waiver_posts_the_same_decision_the_task_page_recorded() -> None:
    state = sorted(WAIVABLE_STATES, key=str)[0]
    items = card_page_actions(TASK_ID, state, Role.ADMIN, [])
    waive = next(i for i in items if i["key"] == "waive_external_review")
    assert waive["action"] == f"/ui/tasks/{TASK_ID}/decisions"
    assert waive["hidden"]["kind"] == "waive_external_review"
    assert waive["note_name"] == "verbatim"


def test_the_rendered_card_page_carries_the_waiver_for_an_admin_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = render_card_html(store_for(TaskState.AWAITING_EXTERNAL_REVIEW), ADMIN, monkeypatch)
    for words in (*WAIVERS, 'value="waive_external_review"', "Operator waivers"):
        assert words in admin, words
    reader = render_card_html(store_for(TaskState.AWAITING_EXTERNAL_REVIEW), OBSERVER, monkeypatch)
    assert not any(words in reader for words in WAIVERS)
