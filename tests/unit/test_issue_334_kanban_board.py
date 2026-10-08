"""hades #334: the Board as a kanban, a read-only projection over the task and wake tables.

One column per lifecycle stage, one card per task with who holds it and for how long,
nothing dragged: Hades moves cards as states change.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.ui.pages import board as page
from crucible.adapters.ui.render import ROOT, _localize, templates
from crucible.application.admin.board import (
    ATTENTION_MINUTES,
    COLUMN_BY_STATE,
    DONE_HOURS,
    KANBAN_COLUMNS,
    RESERVED_COLUMNS,
    age_words,
    board_view,
    column_entered_at,
    kanban_age,
    kanban_column,
    kanban_holder,
)
from crucible.domain.lifecycle import TASK_TERMINAL, TaskState
from tests.unit.admin_ui_fixtures import base_context, request
from tests.unit.test_board import NOW, Policies, Repo, fake_uow, row

COLUMN_NAMES = [
    "Proposed",
    "Queued",
    "Running",
    "Awaiting Foundry",
    "Awaiting Codex",
    "Awaiting CI",
    "Ready to merge",
    "Blocked or failed",
    "Done in the last 24 hours",
]


def task(
    task_id: str,
    state: TaskState,
    *,
    updated_at: datetime | None = None,
    closed_at: datetime | None = None,
    policy_version: int = 1,
) -> SimpleNamespace:
    return row(
        id=task_id,
        external_id=f"FDY-{task_id}",
        principal_id="p1",
        project="hades",
        title=f"Task {task_id}",
        state=state,
        contract_version=1,
        policy_name="default",
        policy_version=policy_version,
        updated_at=updated_at or NOW - timedelta(minutes=5),
        created_at=NOW - timedelta(hours=2),
        closed_at=closed_at,
    )


def contract(task_id: str, parent: str | None = None) -> SimpleNamespace:
    document: dict[str, Any] = {"parent_external_id": parent} if parent else {}
    return row(task_id=task_id, version=1, document=document)


def event(seq: int, task_id: str, kind: str, ts: datetime) -> SimpleNamespace:
    return row(seq=seq, task_id=task_id, kind=kind, payload={}, ts=ts)


def empty_uow() -> SimpleNamespace:
    uow = fake_uow()
    for name in (
        "tasks",
        "contracts",
        "attempts",
        "executions",
        "pull_requests",
        "events",
        "attempt_metrics",
        "escalations",
        "wakes",
        "review_comments",
        "dispositions",
    ):
        setattr(uow, name, Repo())
    uow.policies = Policies()
    return uow


def columns(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        column["key"]: [card for group in column["parents"] for card in group["tasks"]]
        for column in document["kanban"]["columns"]
    }


def test_columns_run_left_to_right_with_the_proposed_column_first() -> None:
    assert [name for _key, name in KANBAN_COLUMNS] == COLUMN_NAMES
    # hades #424 landed: the first position holds the proposed state and is not reserved.
    assert KANBAN_COLUMNS[0][0] not in RESERVED_COLUMNS
    assert [s for s, column in COLUMN_BY_STATE.items() if column == "proposed"] == [
        TaskState.PROPOSED
    ]
    # Every lifecycle state has a column, so no task can fall off the board.
    assert set(COLUMN_BY_STATE) == set(TaskState)
    assert all(COLUMN_BY_STATE[state] == "done" for state in TASK_TERMINAL | {TaskState.MERGED})


def test_every_open_task_is_on_the_board_exactly_once_in_the_column_for_its_state() -> None:
    uow = empty_uow()
    uow.tasks = Repo(
        [
            task("queued", TaskState.SCHEDULED),
            task("running", TaskState.RUNNING),
            task("review", TaskState.AWAITING_INTERNAL_REVIEW),
            task("codex", TaskState.AWAITING_EXTERNAL_REVIEW),
            task("ci", TaskState.AWAITING_CI_CERTIFICATION),
            task("merge", TaskState.READY_FOR_MERGE),
            task("blocked", TaskState.BLOCKED),
            task("escalated", TaskState.RUNNING),
            task("merged", TaskState.MERGED, updated_at=NOW - timedelta(hours=1)),
            task(
                "cancelled",
                TaskState.CANCELLED,
                updated_at=NOW - timedelta(hours=1),
                closed_at=NOW - timedelta(hours=1),
            ),
            task(
                "old",
                TaskState.CLOSED,
                updated_at=NOW - timedelta(hours=DONE_HOURS + 1),
                closed_at=NOW - timedelta(hours=DONE_HOURS + 1),
            ),
        ]
    )
    uow.contracts = Repo([contract(item.id) for item in uow.tasks.rows])
    uow.attempts = Repo(
        [
            row(
                id="a1",
                task_id="running",
                execution_id="e1",
                state="running",
                created_at=NOW - timedelta(minutes=9),
                started_at=NOW - timedelta(minutes=9),
                ended_at=None,
                selected_harness="claude_code",
                selected_model="claude-fable-5-1",
                selected_pool="default",
                ordered_candidates=[],
            )
        ]
    )
    uow.escalations = Repo([row(task_id="escalated")])

    by_column = columns(board_view(uow, NOW))

    placed = [card["id"] for cards in by_column.values() for card in cards]
    expected = ["queued", "running", "review", "codex", "ci", "merge", "blocked", "escalated"]
    assert sorted(placed) == sorted([*expected, "merged", "cancelled"])
    assert len(placed) == len(set(placed))
    assert [card["id"] for card in by_column["queued"]] == ["queued"]
    assert [card["id"] for card in by_column["running"]] == ["running"]
    assert sorted(card["id"] for card in by_column["foundry"]) == ["escalated", "review"]
    assert [card["id"] for card in by_column["codex"]] == ["codex"]
    assert [card["id"] for card in by_column["ci"]] == ["ci"]
    assert [card["id"] for card in by_column["merge"]] == ["merge"]
    assert [card["id"] for card in by_column["blocked"]] == ["blocked"]
    assert sorted(card["id"] for card in by_column["done"]) == ["cancelled", "merged"]
    assert by_column["proposed"] == []
    # The holder chip names who has the ball, and the age says how long.
    holders = {
        card["id"]: card["holder"]["label"] for cards in by_column.values() for card in cards
    }
    assert holders == {
        "queued": "Hades",
        "running": "Worker: claude_code / claude-fable-5-1",
        "review": "Foundry",
        "escalated": "Foundry",
        "codex": "Codex",
        "ci": "CI",
        "merge": "Hades",
        "blocked": "Operator",
        "merged": "Merged",
        "cancelled": "Cancelled",
    }
    escalated = next(card for card in by_column["foundry"] if card["id"] == "escalated")
    assert escalated["holder"]["detail"] == "escalation open"
    assert all(card["age"]["label"] == "5 min" for card in by_column["queued"])
    assert by_column["done"][0]["age"]["label"] == "1 h 00 min"


def test_card_fields_title_external_id_holder_and_age() -> None:
    document = board_view(fake_uow(), NOW)
    [card] = columns(document)["ci"]
    assert card["title"] == "Board"
    assert card["external_id"] == "FDY-1"
    assert card["holder"] == {
        "kind": "ci",
        "label": "CI",
        "detail": "CI is still running next line",
    }
    assert card["age"] == {
        "entered_at": NOW - timedelta(minutes=5),
        "seconds": 300,
        "label": "5 min",
        "late": False,
        "budget_seconds": 6 * 3600,
    }
    assert card["parent_external_id"] == "EPIC-1"
    assert document["kanban"]["thresholds"] == {
        "attention_minutes": ATTENTION_MINUTES,
        "done_hours": DONE_HOURS,
    }


@pytest.mark.parametrize(
    ("column", "minutes", "late"),
    [
        ("foundry", 29, False),
        ("foundry", 31, True),
        ("codex", 30, False),
        ("codex", 31, True),
        ("ci", 6 * 60 - 1, False),
        ("ci", 6 * 60 + 1, True),
        ("queued", 10_000, False),
        ("running", 10_000, False),
        ("merge", 10_000, False),
        ("blocked", 10_000, False),
        ("done", 10_000, False),
    ],
)
def test_age_colours_after_thirty_minutes_or_the_ci_budget(
    column: str, minutes: int, late: bool
) -> None:
    age = kanban_age(column, NOW - timedelta(minutes=minutes), NOW, ci_budget_seconds=6 * 3600)
    assert age["late"] is late
    assert age["seconds"] == minutes * 60


def test_ci_budget_comes_from_the_task_policy_with_the_observer_default() -> None:
    uow = fake_uow()
    uow.tasks.rows[0].updated_at = NOW - timedelta(hours=2)
    [card] = columns(board_view(uow, NOW))["ci"]
    assert card["age"]["budget_seconds"] == 6 * 3600
    assert card["age"]["late"] is False

    uow.policies = Policies(
        [
            row(
                name="default",
                version=1,
                document={"ci_certification": {"wait_timeout_hours": 1}},
            )
        ]
    )
    [card] = columns(board_view(uow, NOW))["ci"]
    assert card["age"]["budget_seconds"] == 3600
    assert card["age"]["late"] is True


def test_column_entry_time_is_read_from_the_transition_events() -> None:
    entries = [
        (NOW - timedelta(minutes=60), 1, "queued"),
        (NOW - timedelta(minutes=50), 2, "queued"),
        (NOW - timedelta(minutes=40), 3, "running"),
        (NOW - timedelta(minutes=10), 4, "foundry"),
    ]
    fallback = NOW - timedelta(minutes=1)
    # The card entered Awaiting Foundry at the latest transition into it.
    assert column_entered_at(entries, "foundry", fallback) == NOW - timedelta(minutes=10)
    # Submitted then scheduled is one stay in Queued, counted from the first entry.
    assert column_entered_at(entries[:2], "queued", fallback) == NOW - timedelta(minutes=60)
    # Events that do not end in the task's column (no event for the state) fall back.
    assert column_entered_at(entries, "ci", fallback) == fallback
    assert column_entered_at([], "queued", fallback) == fallback


def test_board_view_ages_cards_from_events_and_moves_them_on_the_next_load() -> None:
    uow = empty_uow()
    uow.tasks = Repo([task("t1", TaskState.AWAITING_INTERNAL_REVIEW)])
    uow.contracts = Repo([contract("t1")])
    uow.events = Repo(
        [
            event(1, "t1", "task_submitted", NOW - timedelta(minutes=90)),
            event(2, "t1", "task_scheduled", NOW - timedelta(minutes=80)),
            event(3, "t1", "task_running", NOW - timedelta(minutes=70)),
            event(4, "t1", "task_reported", NOW - timedelta(minutes=45)),
            event(5, "t1", "task_awaiting_internal_review", NOW - timedelta(minutes=40)),
        ]
    )

    [card] = columns(board_view(uow, NOW))["foundry"]
    assert card["age"]["entered_at"] == NOW - timedelta(minutes=40)
    assert card["age"]["label"] == "40 min"
    assert card["age"]["late"] is True

    # Hades moves the card: a state change on the task, with its event, and the next load
    # shows it in the new column with a fresh age. No table is written by the board.
    uow.tasks.rows[0].state = TaskState.AWAITING_CI_CERTIFICATION
    uow.events.rows.append(
        event(6, "t1", "task_awaiting_ci_certification", NOW - timedelta(minutes=3))
    )
    by_column = columns(board_view(uow, NOW))
    assert by_column["foundry"] == []
    [moved] = by_column["ci"]
    assert moved["age"]["label"] == "3 min"
    assert moved["age"]["late"] is False


def test_done_column_shows_the_last_day_only_and_keeps_the_parent() -> None:
    uow = empty_uow()
    uow.tasks = Repo(
        [
            task("parent", TaskState.RUNNING),
            task("fresh", TaskState.MERGED, updated_at=NOW),
            task("stale", TaskState.MERGED, updated_at=NOW),
            task(
                "rejected",
                TaskState.REJECTED,
                updated_at=NOW - timedelta(hours=2),
                closed_at=NOW - timedelta(hours=2),
            ),
        ]
    )
    uow.contracts = Repo(
        [
            contract("parent"),
            contract("fresh", parent="FDY-parent"),
            contract("stale"),
            contract("rejected", parent="FDY-parent"),
        ]
    )
    uow.events = Repo(
        [
            event(1, "fresh", "task_merged", NOW - timedelta(hours=3)),
            event(2, "stale", "task_merged", NOW - timedelta(hours=DONE_HOURS + 2)),
        ]
    )

    document = board_view(uow, NOW)
    done = next(column for column in document["kanban"]["columns"] if column["key"] == "done")

    [group] = done["parents"]
    assert group["parent_external_id"] == "FDY-parent"
    assert group["parent_task_id"] == "parent"
    assert [card["id"] for card in group["tasks"]] == ["fresh", "rejected"]
    assert [card["holder"]["label"] for card in group["tasks"]] == ["Merged", "Rejected"]
    assert group["tasks"][0]["age"]["entered_at"] == NOW - timedelta(hours=3)
    assert group["tasks"][1]["age"]["entered_at"] == NOW - timedelta(hours=2)


def test_holder_chip_words() -> None:
    attempt = row(selected_harness="codex", selected_model="gpt-5", selected_pool="priority")
    wake = row(payload={"summary": "needs a decision"}, reason="blocked")
    assert kanban_holder("running", TaskState.RUNNING, attempt, None) == {
        "kind": "worker",
        "label": "Worker: codex / gpt-5",
        "detail": "pool priority",
    }
    assert kanban_holder("running", TaskState.PUBLISHING, None, None) == {
        "kind": "hades",
        "label": "Hades",
        "detail": "publishing",
    }
    assert kanban_holder("queued", TaskState.AWAITING_QUOTA, None, None)["detail"] == (
        "awaiting quota"
    )
    assert kanban_holder("foundry", TaskState.AWAITING_ACCEPTANCE, None, None) == {
        "kind": "foundry",
        "label": "Foundry",
        "detail": "awaiting acceptance",
    }
    assert kanban_holder("codex", TaskState.AWAITING_EXTERNAL_REVIEW, None, None)["kind"] == (
        "codex"
    )
    assert kanban_holder("merge", TaskState.READY_FOR_MERGE, None, None) == {
        "kind": "hades",
        "label": "Hades",
        "detail": "merge",
    }
    assert kanban_holder("blocked", TaskState.BLOCKED, None, wake) == {
        "kind": "operator",
        "label": "Operator",
        "detail": "wake: needs a decision",
    }
    assert kanban_holder("blocked", TaskState.HEAD_DIVERGED, None, None)["detail"] == (
        "decision: head diverged"
    )
    assert kanban_holder("done", TaskState.CLOSED, None, None)["label"] == "Closed"
    assert kanban_column(TaskState.RUNNING, has_open_escalation=True) == "foundry"
    assert kanban_column(TaskState.MERGED, has_open_escalation=True) == "done"


def test_age_words() -> None:
    assert age_words(0) == "under a minute"
    assert age_words(59) == "under a minute"
    assert age_words(60) == "1 min"
    assert age_words(45 * 60) == "45 min"
    assert age_words(3 * 3600 + 5 * 60) == "3 h 05 min"
    assert age_words(26 * 3600) == "1 d 2 h"


def _render(sections: list[dict[str, Any]]) -> str:
    return templates.get_template("page.html").render(
        **base_context("/ui/board"),
        heading="Board",
        intro="Fixture",
        sections=_localize(sections, "America/Chicago"),
        badge=None,
    )


def test_page_renders_cards_that_link_to_the_task_page_in_sideways_columns() -> None:
    uow = fake_uow()
    uow.tasks.rows[0].updated_at = NOW - timedelta(hours=7)
    uow.tasks.rows.append(task("parent", TaskState.RUNNING))
    uow.tasks.rows[1].external_id = "EPIC-1"
    uow.contracts.rows.append(contract("parent"))
    document = board_view(uow, NOW)

    html = _render([page._kanban_section(document), page._list_section(document)])

    kanban = html[html.index('class="admin-kanban"') :]
    headings = re.findall(r"<h3>([^<]+)</h3>", kanban)
    assert headings[: len(COLUMN_NAMES)] == COLUMN_NAMES
    assert 'data-column="proposed"' in kanban
    assert "Waiting for the operator: approve, send back or reject." in kanban
    # One card per task, the whole card a link to the task page.
    assert kanban.count('<a class="admin-kanban-card') == 2
    assert 'href="/ui/tasks/t1"' in kanban
    assert 'href="/ui/tasks/parent"' in kanban
    assert '<span class="admin-kanban-card-title">Board</span>' in kanban
    assert '<span class="admin-kanban-card-id">FDY-1</span>' in kanban
    # The holder chip and the age, coloured past the CI budget.
    assert 'class="admin-chip admin-chip--ci"' in kanban
    assert 'class="admin-chip admin-chip--hades"' in kanban
    assert 'class="admin-kanban-age admin-kanban-age--late"' in kanban
    assert "admin-kanban-card admin-kanban-card--late" in kanban
    assert "7 h 00 min" in kanban
    # Workers stay nested under their parent inside the column, the parent linked.
    assert '<div class="admin-kanban-parent-head"><a href="/ui/tasks/parent">EPIC-1</a>' in kanban
    # The list view is still reachable under the kanban.
    assert "<summary>Show the task list</summary>" in html
    assert "Awaiting external: CI" in html


def test_stylesheet_scrolls_columns_sideways_and_fits_a_phone() -> None:
    css = (ROOT / "static" / "admin.css").read_text()
    kanban = re.search(r"\.admin-kanban \{([^}]*)\}", css)
    assert kanban is not None
    assert "overflow-x: auto" in kanban.group(1)
    column = re.search(r"\.admin-kanban-column \{([^}]*)\}", css)
    assert column is not None
    # A column is most of a phone screen, and a fixed width on anything wider.
    assert "min(82vw, 290px)" in column.group(1)
    assert ".admin-kanban-age--late" in css
    assert ".admin-chip--operator" in css
    base = (ROOT / "templates" / "base.html").read_text()
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in base


def test_board_page_leads_with_the_kanban_and_keeps_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(page, "_require", lambda *a, **k: (None, "csrf"))
    monkeypatch.setattr(
        page,
        "board_lanes_view",
        lambda *_args: {
            "lanes": [
                {
                    "key": "inbox",
                    "name": "Inbox",
                    "meaning": "Later intake.",
                    "collapsed": False,
                    "count": 0,
                    "cards": [],
                }
            ]
        },
    )
    ctx = SimpleNamespace(clock=SimpleNamespace(now=lambda: NOW))

    req = request("/ui/board")
    req.scope["query_string"] = b""
    response = page.board_page(req, ctx, fake_uow())  # type: ignore[arg-type]
    assert response.status_code == 200
    assert b"Inbox" in response.body
    assert b"Task list" not in response.body
    assert b"Tokens by harness" not in response.body


def test_no_migration_or_table_was_added_for_the_board() -> None:
    """AC2: the kanban is a projection. The Board module reads repositories only, and the
    change adds no migration revision after the ones the schema already has."""
    source = Path("crucible/application/admin/board.py").read_text()
    assert "migration" not in source.lower()
    assert "exec_driver_sql" not in source
    versions = Path("crucible/adapters/persistence/migrations/versions")
    assert not [path for path in versions.glob("*.py") if "board" in path.name.lower()]
    assert not [path for path in versions.glob("*.py") if "kanban" in path.name.lower()]
