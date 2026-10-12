"""Tasks page: archived import tasks hidden unless asked for (hades #255)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

from crucible.adapters.ui.pages import tasks as tasks_mod
from crucible.domain.lifecycle import TaskState


def test_tasks_of_disabled_discarded_import_principals_are_hidden_unless_archived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hades FDY-0207: tasks owned by disabled discarded-import principals are absent from
    every Tasks page section and from the counts by default, present with ?archived=1, and
    a note says how many are hidden."""

    archived_principal_id = "abc123"
    normal_principal_id = "normal456"

    # A principal that looks like a discarded import
    archived_principal = SimpleNamespace(
        id=archived_principal_id,
        name="discarded-import-aaa",
        role=SimpleNamespace(value="observer"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        disabled_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    # A normal principal
    normal_principal = SimpleNamespace(
        id=normal_principal_id,
        name="normal-principal",
        role=SimpleNamespace(value="user"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        disabled_at=None,
    )

    # Archived tasks
    archived_task = SimpleNamespace(
        id="archived-task-1",
        external_id="ext-1",
        principal_id=archived_principal_id,
        state=TaskState.SUBMITTED,
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    archived_task_2 = SimpleNamespace(
        id="archived-task-2",
        external_id="ext-2",
        principal_id=archived_principal_id,
        state=TaskState.RUNNING,
        updated_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    # Normal task
    normal_task = SimpleNamespace(
        id="normal-task-1",
        external_id="ext-3",
        principal_id=normal_principal_id,
        state=TaskState.SUBMITTED,
        updated_at=datetime(2026, 6, 3, tzinfo=UTC),
    )

    # Fake principals list (returns both archived and normal)
    def fake_list_all() -> list[Any]:
        return [archived_principal, normal_principal]

    # Fake list_by_state
    def fake_list_by_state(state: TaskState) -> list[Any]:
        return [t for t in [archived_task, archived_task_2, normal_task] if t.state == state]

    # Fake recently_updated
    def fake_recently_updated(
        since: datetime, limit: int, exclude_principal_ids: set[str] | None = None
    ) -> list[Any]:
        return [archived_task, normal_task]

    uow = SimpleNamespace(
        principals=SimpleNamespace(list_all=fake_list_all),
        tasks=SimpleNamespace(
            list_by_state=fake_list_by_state,
            recently_updated=fake_recently_updated,
        ),
    )

    ctx = SimpleNamespace(clock=SimpleNamespace(now=lambda: datetime(2026, 6, 5, tzinfo=UTC)))

    principal = SimpleNamespace(name="reader", role=SimpleNamespace(value="observer"))
    sections_captured: list[dict[str, Any]] = []

    # Fake status.tasks to return a minimal document
    fake_doc = {
        "lists": {
            "submitted": [
                {
                    "id": "archived-task-1",
                    "external_id": "ext-1",
                    "updated_at": "2026-06-01T00:00:00Z",
                },
                {
                    "id": "normal-task-1",
                    "external_id": "ext-3",
                    "updated_at": "2026-06-03T00:00:00Z",
                },
            ],
            "running": [
                {
                    "id": "archived-task-2",
                    "external_id": "ext-2",
                    "updated_at": "2026-06-02T00:00:00Z",
                },
            ],
        },
        "counts": {
            "submitted": 2,
            "running": 1,
        },
        "gates": [],
        "publishing_waiting": [],
    }

    def fake_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return fake_doc

    status_ns = SimpleNamespace(tasks=fake_status)

    def fake_page(
        *args: Any,
        sections: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        sections_captured.extend(sections)
        return "fake response"

    monkeypatch.setattr(tasks_mod, "_require", lambda *a, **k: (principal, "fixture-csrf"))
    monkeypatch.setattr(tasks_mod, "status", status_ns)
    monkeypatch.setattr(tasks_mod, "work_page", fake_page)

    def _get_archived_false() -> Any:
        return tasks_mod.tasks_page(
            Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/ui/tasks",
                    "headers": [],
                    "query_string": b"",
                }
            ),
            cast(Any, ctx),
            cast(Any, uow),
        )

    def _get_archived_true() -> Any:
        return tasks_mod.tasks_page(
            Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/ui/tasks",
                    "headers": [],
                    "query_string": b"archived=1",
                }
            ),
            cast(Any, ctx),
            cast(Any, uow),
        )

    _get_archived_false()

    # Default (archived=false): archived tasks must be absent
    for section in sections_captured:
        title = section["title"]
        rows = section.get("rows", [])

        if title == "Needs attention":
            task_ids_in_rows = [
                r[0].get("label", "") if isinstance(r[0], dict) else str(r[0]) for r in rows
            ]
            assert "ext-1" not in task_ids_in_rows, f"archived ext-1 in {title}"
            assert "ext-2" not in task_ids_in_rows, f"archived ext-2 in {title}"
            assert "ext-3" in task_ids_in_rows, f"normal ext-3 missing from {title}"

        elif title == "Tasks by state":
            total_visible = sum(r[1] for r in rows if isinstance(r[1], int))
            assert total_visible == 1, f"expected 1 task in counts, got {total_visible}"

        elif title == "Recently updated":
            task_ids_in_rows = [
                r[0].get("label", "") if isinstance(r[0], dict) else str(r[0]) for r in rows
            ]
            assert "ext-1" not in task_ids_in_rows, f"archived ext-1 in {title}"
            assert "ext-3" in task_ids_in_rows, f"normal ext-3 missing from {title}"

        elif title == "Gates by task":
            pass  # no gates in our test

    # Check the hidden note exists
    recently_updated = next(
        (s for s in sections_captured if s["title"] == "Recently updated"), None
    )
    assert recently_updated is not None, "Recently updated section not found"
    note = recently_updated.get("note", "")
    assert "archived import tasks hidden" in note, f"Hidden note missing from: {note}"
    assert "2 archived import tasks hidden" in note, f"Wrong count in note: {note}"

    # Reset
    sections_captured.clear()

    # With ?archived=1: archived tasks must be present
    _get_archived_true()

    for section in sections_captured:
        title = section["title"]
        rows = section.get("rows", [])

        if title == "Needs attention":
            task_ids_in_rows = [
                r[0].get("label", "") if isinstance(r[0], dict) else str(r[0]) for r in rows
            ]
            assert "ext-1" in task_ids_in_rows, (
                f"ext-1 should be visible with archived=1 in {title}"
            )
            assert "ext-2" in task_ids_in_rows, (
                f"ext-2 should be visible with archived=1 in {title}"
            )
            assert "ext-3" in task_ids_in_rows, f"ext-3 missing from {title}"

        elif title == "Tasks by state":
            total_visible = sum(r[1] for r in rows if isinstance(r[1], int))
            assert total_visible == 3, f"expected 3 tasks with archived=1, got {total_visible}"

        elif title == "Recently updated":
            task_ids_in_rows = [
                r[0].get("label", "") if isinstance(r[0], dict) else str(r[0]) for r in rows
            ]
            assert "ext-1" in task_ids_in_rows, (
                f"ext-1 should be visible with archived=1 in {title}"
            )
            assert "ext-3" in task_ids_in_rows, f"ext-3 missing from {title}"

    # The hidden note should NOT appear with archived=1
    recently_updated = next(
        (s for s in sections_captured if s["title"] == "Recently updated"), None
    )
    assert recently_updated is not None
    note = recently_updated.get("note", "")
    assert "archived import tasks hidden" not in note, f"Hidden note should not appear: {note}"


def test_recently_updated_excludes_archived_before_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """hades FDY-0207: when 50 archived tasks are newer than one real task, the real
    task still appears in the Recently updated section because archived filtering
    happens before the limit."""

    archived_principal_id = "arch001"
    normal_principal_id = "norm001"

    archived_principal = SimpleNamespace(
        id=archived_principal_id,
        name="discarded-import-bbb",
        role=SimpleNamespace(value="observer"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        disabled_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    normal_principal = SimpleNamespace(
        id=normal_principal_id,
        name="normal-principal-2",
        role=SimpleNamespace(value="user"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        disabled_at=None,
    )

    # Create 50 archived tasks (all with newer timestamps than the real task)
    archived_tasks: list[Any] = []
    for i in range(50):
        archived_tasks.append(
            SimpleNamespace(
                id=f"archived-task-{i}",
                external_id=f"ext-arch-{i}",
                principal_id=archived_principal_id,
                state=TaskState.RUNNING,
                updated_at=datetime(2026, 6, 5, tzinfo=UTC),
            )
        )

    # One normal task with an older update time
    normal_task = SimpleNamespace(
        id="normal-task-2",
        external_id="ext-real",
        principal_id=normal_principal_id,
        state=TaskState.SUBMITTED,
        updated_at=datetime(2026, 6, 4, tzinfo=UTC),
    )

    def fake_list_all() -> list[Any]:
        return [archived_principal, normal_principal]

    def fake_list_by_state(state: TaskState) -> list[Any]:
        result: list[Any] = []
        for t in archived_tasks:
            if t.state == state:
                result.append(t)
        if normal_task.state == state:
            result.append(normal_task)
        return result

    # The key: recently_updated should return 50 archived tasks + 1 normal,
    # but only up to limit rows. Without exclude_principal_ids, the 50 archived
    # tasks fill the limit and the real task is never seen.
    call_kwargs: dict[str, Any] = {}

    def fake_recently_updated(
        since: datetime, limit: int, exclude_principal_ids: set[str] | None = None
    ) -> list[Any]:
        call_kwargs["since"] = since
        call_kwargs["limit"] = limit
        call_kwargs["exclude_principal_ids"] = exclude_principal_ids
        if exclude_principal_ids:
            # Return only tasks whose principal is NOT in the exclude set
            return [normal_task]
        else:
            # Without the filter: 50 archived fill the limit
            return list(archived_tasks)

    uow = SimpleNamespace(
        principals=SimpleNamespace(list_all=fake_list_all),
        tasks=SimpleNamespace(
            list_by_state=fake_list_by_state,
            recently_updated=fake_recently_updated,
        ),
    )

    ctx = SimpleNamespace(clock=SimpleNamespace(now=lambda: datetime(2026, 6, 5, tzinfo=UTC)))

    principal = SimpleNamespace(name="reader", role=SimpleNamespace(value="observer"))
    sections_captured: list[dict[str, Any]] = []

    fake_doc = {
        "lists": {
            "submitted": [
                {
                    "id": "normal-task-2",
                    "external_id": "ext-real",
                    "updated_at": "2026-06-04T00:00:00Z",
                },
            ],
            "running": [
                *[
                    {
                        "id": f"archived-task-{i}",
                        "external_id": f"ext-arch-{i}",
                        "updated_at": "2026-06-05T00:00:00Z",
                    }
                    for i in range(5)
                ],
            ],
        },
        "counts": {
            "submitted": 1,
            "running": 51,  # 50 archived + 1 normal
        },
        "gates": [],
        "publishing_waiting": [],
    }

    def fake_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return fake_doc

    def fake_page(
        *args: Any,
        sections: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        sections_captured.extend(sections)
        return "fake response"

    monkeypatch.setattr(tasks_mod, "_require", lambda *a, **k: (principal, "fixture-csrf"))
    monkeypatch.setattr(tasks_mod, "status", SimpleNamespace(tasks=fake_status))
    monkeypatch.setattr(tasks_mod, "work_page", fake_page)

    tasks_mod.tasks_page(
        Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/ui/tasks",
                "headers": [],
                "query_string": b"",
            }
        ),
        cast(Any, ctx),
        cast(Any, uow),
    )

    # Verify the real task still appears in the Recently updated section
    recently_section = next(
        (s for s in sections_captured if s["title"] == "Recently updated"), None
    )
    assert recently_section is not None
    task_ids_in_recent = [
        r[0].get("label", "") if isinstance(r[0], dict) else str(r[0])
        for r in recently_section["rows"]
    ]
    assert "ext-real" in task_ids_in_recent, (
        "The normal task should appear in Recently updated when archived tasks fill the limit"
    )

    # Verify the archive filter is passed to the query
    assert call_kwargs.get("exclude_principal_ids") == {archived_principal_id}, (
        "exclude_principal_ids should be passed to recently_updated"
    )

    # Verify the normal task is absent from the Needs attention section
    attention_section = next(
        (s for s in sections_captured if s["title"] == "Needs attention"), None
    )
    assert attention_section is not None
    attention_task_ids = [
        r[0].get("label", "") if isinstance(r[0], dict) else str(r[0])
        for r in attention_section["rows"]
    ]
    for i in range(5):
        assert f"ext-arch-{i}" not in attention_task_ids, (
            f"Archived ext-arch-{i} should not appear in Needs attention"
        )
