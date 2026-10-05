from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from crucible.application.admin.board import board_view, ci_summary, eta_bound, quality_totals
from crucible.domain.entities import PullRequestState
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


class Repo:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    def search(self, **kwargs: Any) -> list[Any]:
        after = kwargs["after_id"]
        if after is None:
            return self.rows[: int(kwargs["limit"])]
        start = next(i for i, row in enumerate(self.rows) if row.id == after) + 1
        return self.rows[start : start + int(kwargs["limit"])]

    def get(self, task_id: str, version: int) -> Any | None:
        return next(
            (row for row in self.rows if row.task_id == task_id and row.version == version), None
        )

    def list_in_states(self, states: list[Any]) -> list[Any]:
        return [row for row in self.rows if row.state in states]

    def list_by_state(self, state: Any) -> list[Any]:
        return [row for row in self.rows if row.state is state]

    def list_global(self, **kwargs: Any) -> list[Any]:
        after = int(kwargs["after_seq"])
        return [row for row in self.rows if row.seq > after][: int(kwargs["limit"])]

    def list_since(self, **kwargs: Any) -> list[Any]:
        return self.rows

    def list_open(self) -> list[Any]:
        return self.rows

    def list_for_principal(self, principal_id: str, **kwargs: Any) -> list[Any]:
        return [
            row for row in self.rows if row.principal_id == principal_id and row.acked_at is None
        ][: int(kwargs["limit"])]

    def list_for_task(self, task_id: str) -> list[Any]:
        return [row for row in self.rows if row.task_id == task_id]

    def list_for_pull_request(self, pull_request_id: str) -> list[Any]:
        return [row for row in self.rows if row.pull_request_id == pull_request_id]

    def list_for_comments(self, ids: list[str], hashes: dict[str, str]) -> list[Any]:
        return [row for row in self.rows if row.review_comment_id in ids]


class Policies:
    """hades #334: the kanban reads the CI budget from the task's policy; an unknown
    policy means the default budget."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    def get(self, name: str, version: int) -> Any | None:
        return next((row for row in self.rows if row.name == name and row.version == version), None)


def row(**values: object) -> SimpleNamespace:
    return SimpleNamespace(**values)


def fake_uow() -> SimpleNamespace:
    task = row(
        id="t1",
        external_id="FDY-1",
        principal_id="p1",
        project="hades",
        title="Board",
        state=TaskState.AWAITING_CI_CERTIFICATION,
        contract_version=1,
        policy_name="default",
        policy_version=1,
        updated_at=NOW - timedelta(minutes=5),
        created_at=NOW - timedelta(hours=2),
        closed_at=None,
    )
    contract = row(
        task_id="t1",
        version=1,
        document={
            "parent_external_id": "EPIC-1",
            "repository": {"url": "https://github.com/acme/repo"},
            "deliverables": [{"closes": ["#188"]}],
        },
    )
    attempt = row(
        id="a1",
        task_id="t1",
        execution_id="e1",
        state=AttemptState.RUNNING,
        created_at=NOW - timedelta(hours=1),
        started_at=NOW - timedelta(minutes=10),
        ended_at=None,
        selected_harness="codex",
        selected_model="gpt-5",
        selected_pool="priority",
        ordered_candidates=[
            {"model": "gpt-6", "harness": "codex", "pool": "priority", "busy": True},
            {"model": "gpt-5", "harness": "codex", "pool": "priority"},
        ],
    )
    execution = row(id="e1", state=ExecutionState.ACTIVE, timeout_seconds=3600)
    pr = row(
        id="pr1",
        task_id="t1",
        state=PullRequestState.OPEN,
        opened_at=NOW - timedelta(hours=1),
        merged_at=None,
        cancelled_at=None,
        number=188,
        url="https://github.com/acme/repo/pull/188",
    )
    events = [
        row(
            seq=1,
            task_id="t1",
            kind="task_correction_attached",
            payload={},
            ts=NOW - timedelta(minutes=30),
        ),
        row(
            seq=2,
            task_id="t1",
            kind="gates_evaluated",
            payload={"phase": "pre_pr", "failing": ["unit"]},
            ts=NOW - timedelta(minutes=29),
        ),
        row(
            seq=3,
            task_id="t1",
            kind="ci_certification_recorded",
            payload={"state": "pending"},
            ts=NOW - timedelta(minutes=2),
        ),
    ]
    wake = row(
        task_id="t1",
        principal_id="p1",
        acked_at=None,
        created_at=NOW - timedelta(minutes=1),
        payload={"summary": "CI is still running\nnext line"},
        reason="ci",
    )
    metric = row(
        attempt_id="a1",
        task_id="t1",
        harness="codex",
        model="gpt-5",
        pool="priority",
        tokens_in=100,
        tokens_out=20,
    )
    comment = row(
        id="c1",
        pull_request_id="pr1",
        login="chatgpt-codex-connector",
        body="P1: fix this",
        body_sha256="sha",
    )
    disposition = row(review_comment_id="c1", disposition=row(value="fix"))
    return SimpleNamespace(
        tasks=Repo([task]),
        contracts=Repo([contract]),
        attempts=Repo([attempt]),
        executions=Repo([execution]),
        pull_requests=Repo([pr]),
        events=Repo(events),
        attempt_metrics=Repo([metric]),
        escalations=Repo(),
        wakes=Repo([wake]),
        review_comments=Repo([comment]),
        dispositions=Repo([disposition]),
        policies=Policies(),
    )


def test_board_groups_and_proves_every_in_flight_column_from_fake_uow() -> None:
    document = board_view(fake_uow(), NOW)
    item = document["in_flight"][6]["parents"][0]["tasks"][0]
    assert item["external_id"] == "FDY-1"
    assert item["title"] == "Board"
    assert item["parent_external_id"] == "EPIC-1"
    assert item["issues"] == [{"label": "#188", "url": "https://github.com/acme/repo/issues/188"}]
    assert (item["harness"], item["model"], item["pool"]) == ("codex", "gpt-5", "priority")
    assert item["state"] == "awaiting_ci_certification"
    assert item["state_since"] == NOW - timedelta(minutes=5)
    assert item["waiting_on"] == "CI is still running next line"
    assert item["pull_request"]["ci"] == {"status": "running", "label": "running"}
    assert item["corrections"] == 1
    assert item["eta"] == {"elapsed_seconds": 600, "timeout_seconds": 3600, "label": "600s / 3600s"}
    assert document["routing"][0]["candidates"][0]["reason"] == "busy, skipped"
    assert document["tokens"]["attempts"][0]["tokens_in"] == 100


def test_ci_summary_and_eta_bound() -> None:
    assert ci_summary("success")["status"] == "green"
    assert ci_summary("failure")["status"] == "red"
    assert ci_summary(None)["status"] == "none"
    assert eta_bound(None, 60, NOW)["label"] == "not started"


def test_quality_totals_and_findings_dispositions() -> None:
    quality = board_view(fake_uow(), NOW)["quality"]
    assert quality["tasks"][0]["findings"] == {"p1": {"fix": 1}}
    assert quality["totals"] == [
        {
            "harness": "codex",
            "model": "gpt-5",
            "tasks": 1,
            "failed_gates": 1,
            "findings": {"p1": {"fix": 1}},
            "corrections": 1,
            "merged": 0,
            "cancelled": 0,
            "open": 1,
        }
    ]
    assert quality_totals([]) == []


def test_quality_outcome_uses_cancelled_task_state_after_pr_opened() -> None:
    uow = fake_uow()
    uow.tasks.rows[0].state = TaskState.CANCELLED

    quality = board_view(uow, NOW)["quality"]

    assert quality["tasks"][0]["outcome"] == "cancelled"
    assert quality["totals"][0]["cancelled"] == 1


def test_board_includes_imported_running_attempt() -> None:
    uow = fake_uow()
    imported = uow.attempts.rows[0]
    imported.unsupervised = True
    uow.attempts.list_in_states = lambda _states: []

    document = board_view(uow, NOW)
    item = document["in_flight"][6]["parents"][0]["tasks"][0]

    assert (item["harness"], item["model"], item["pool"]) == (
        "codex",
        "gpt-5",
        "priority",
    )
    assert item["eta"]["label"] == "600s / 3600s"
    assert document["routing"][0]["attempt_id"] == "a1"


def test_board_uses_newest_unacked_wake_beyond_first_two_hundred() -> None:
    uow = fake_uow()
    old_wakes = [
        row(
            task_id="t1",
            principal_id="p1",
            acked_at=None,
            created_at=NOW - timedelta(minutes=300 - index),
            payload={"summary": f"old {index}"},
            reason="old",
        )
        for index in range(200)
    ]
    newest = row(
        task_id="t1",
        principal_id="p1",
        acked_at=None,
        created_at=NOW,
        payload={"summary": "newest wake"},
        reason="newest",
    )
    uow.wakes = Repo([*old_wakes, newest])

    document = board_view(uow, NOW)
    item = document["in_flight"][6]["parents"][0]["tasks"][0]

    assert item["waiting_on"] == "newest wake"


def test_board_empty_database() -> None:
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
    document = board_view(uow, NOW)
    assert all(not group["parents"] for group in document["in_flight"])
    assert document["quality"]["tasks"] == []


def test_board_handles_two_hundred_tasks_with_bounded_aggregate_reads() -> None:
    uow = fake_uow()
    template = uow.tasks.rows[0]
    uow.tasks = Repo(
        [
            row(
                id=f"t{index}",
                external_id=f"FDY-{index}",
                principal_id="p1",
                project=template.project,
                title=template.title,
                state=TaskState.SUBMITTED,
                contract_version=1,
                policy_name="default",
                policy_version=1,
                updated_at=template.updated_at,
                created_at=template.created_at,
                closed_at=None,
            )
            for index in range(200)
        ]
    )
    uow.contracts = Repo([row(task_id=f"t{index}", version=1, document={}) for index in range(200)])
    uow.attempts = Repo()
    uow.executions = Repo()
    uow.pull_requests = Repo()
    uow.events = Repo()
    uow.attempt_metrics = Repo()
    uow.review_comments = Repo()
    uow.dispositions = Repo()
    document = board_view(uow, NOW)
    assert len(document["in_flight"][0]["parents"][0]["tasks"]) == 200
