"""FDY-0591: persona contracts, Central cron time, catalog checks, and run now."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from crucible.adapters.api.routers import personas_jobs as api
from crucible.application.errors import ContractValidationError
from crucible.application.personas_jobs import (
    generate_contract,
    next_run,
    validate_catalog_references,
)
from crucible.domain.entities import Persona, Policy, Repository, ScheduledJob, Task, TaskContract
from crucible.domain.lifecycle import TaskState

NOW = datetime(2026, 3, 7, 15, tzinfo=UTC)


class _Memory:
    def recall(self, **_kwargs: Any) -> list[Any]:
        return [
            SimpleNamespace(
                id="m",
                text="Earlier audit found drift",
                source="run",
                observed_at=NOW,
                scope_tags=[],
                promoted_by="hades",
                promoted_at=NOW,
                superseded_by=None,
                superseded_at=None,
                current=True,
            )
        ]


def _world() -> Any:
    repository = Repository(
        id="r",
        name="hades",
        url="x",
        default_branch="main",
        policy_name="default",
        installation_id=None,
        registered_by="x",
        created_at=NOW,
    )
    policy = Policy(
        name="default",
        version=1,
        document={"repository": {"required_checks": ["make lint"]}},
        created_at=NOW,
    )
    return SimpleNamespace(
        repositories=SimpleNamespace(get_by_name=lambda _name: repository),
        policies=SimpleNamespace(list_versions=lambda _name: [policy]),
        memory=_Memory(),
    )


def _persona() -> Persona:
    return Persona(
        id="p",
        name="Auditor",
        role_text="Act as an auditor.",
        skills=["example_analyzer"],
        tools=["codex"],
        default_harness="codex",
        default_model="gpt",
        default_tier="standard",
        budget_usd=2,
        created_by="op",
        created_at=NOW,
        updated_at=NOW,
    )


def _job(**changes: Any) -> ScheduledJob:
    job = ScheduledJob(
        id="j",
        persona_id="p",
        name="Weekly audit",
        task_kind="prompt",
        task_text="Inspect the repository.",
        cadence="0 9 * * *",
        cadence_label="Daily at 9",
        timezone="America/Chicago",
        results_to="inbox_card",
        carry_notes_forward=True,
        project="hades",
        enabled=True,
        last_run_at=None,
        next_run_at=NOW,
        created_by="op",
    )
    return replace(job, **changes)


def test_contract_recalls_notes_and_tags_inbox() -> None:
    document = generate_contract(_world(), _job(), _persona(), NOW)
    assert "Earlier audit found drift" in str(document["objective"])
    assert document["scheduled_job"] == {
        "id": "j",
        "results_to": "inbox_card",
        "tags": ["scheduled-job", "inbox"],
        "persona_tools": ["codex"],
    }
    assert document["required_verification"][0]["command"] == "make lint"  # type: ignore[index]


def test_next_run_crosses_central_dst_spring_change() -> None:
    before = datetime(2026, 3, 7, 15, tzinfo=UTC)
    first = next_run("0 9 * * *", before)
    second = next_run("0 9 * * *", first)
    assert first == datetime(2026, 3, 8, 14, tzinfo=UTC)
    assert second == datetime(2026, 3, 9, 14, tzinfo=UTC)


def test_catalog_references_reject_unknown_names() -> None:
    validate_catalog_references(["example_analyzer"], ["codex"])
    with pytest.raises(ContractValidationError, match="unknown catalog tools"):
        validate_catalog_references([], ["credential-value"])


def test_run_now_files_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job(enabled=False, next_run_at=None)
    task = Task(
        id="t",
        external_id="JOB-j",
        principal_id="p",
        project="hades",
        title="Weekly audit",
        state=TaskState.SUBMITTED,
        contract_version=1,
        policy_name="default",
        policy_version=1,
        repository_id="r",
        created_at=NOW,
        updated_at=NOW,
    )
    stored = TaskContract(
        id="c", task_id="t", version=1, document={}, sha256="0" * 64, submitted_at=NOW
    )
    monkeypatch.setattr(
        api, "run_job", lambda *_args: (job, task, stored, {"title": "Weekly audit"})
    )
    uow = SimpleNamespace(commit=lambda: None)
    result = api.run_now(
        "j",
        cast(Any, SimpleNamespace(clock=None)),
        cast(Any, uow),
        cast(Any, SimpleNamespace()),
    )
    assert result.task_id == "t"
    assert result.contract["title"] == "Weekly audit"
