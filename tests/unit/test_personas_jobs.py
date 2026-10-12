"""FDY-0591: persona contracts, Central cron time, catalog checks, and run now."""

import importlib
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

import crucible.application.supervisor as module
from crucible.adapters.api.routers import personas_jobs as api
from crucible.adapters.ui.pages import personas_jobs as pages
from crucible.adapters.ui.pages.personas_jobs import cadence_from_form
from crucible.application.admin.board_lanes import board_lanes_view
from crucible.application.errors import ContractValidationError
from crucible.application.personas_jobs import (
    generate_contract,
    last_run_result,
    next_run,
    run_job,
    validate_catalog_references,
)
from crucible.domain.entities import (
    ExecutionRole,
    Persona,
    Policy,
    Principal,
    Repository,
    Role,
    ScheduledJob,
    Task,
    TaskContract,
)
from crucible.domain.lifecycle import AttemptState, TaskState
from tests.unit.test_board import Repo, row
from tests.unit.test_issue_489_board_lanes import fixture

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
    document = generate_contract(_world(), _job(), _persona(), NOW, provider="docker")
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
        api, "run_job", lambda *_args, **_kwargs: (job, task, stored, {"title": "Weekly audit"})
    )
    uow = SimpleNamespace(commit=lambda: None)
    result = api.run_now(
        "j",
        cast(
            Any,
            SimpleNamespace(
                clock=None,
                providers=[],
                harnesses=None,
                harness_gates={},
                credential_sources={},
                secret_providers=[],
            ),
        ),
        cast(Any, uow),
        cast(Any, SimpleNamespace()),
    )
    assert result.task_id == "t"
    assert result.contract["title"] == "Weekly audit"


@pytest.mark.parametrize(
    ("form", "expected"),
    [
        (
            {"cadence_preset": "daily", "daily_time": "17:35"},
            ("35 17 * * *", "Daily at 5:35 PM Central"),
        ),
        (
            {"cadence_preset": "weekly", "daily_time": "08:15", "weekly_day": "0"},
            ("15 8 * * 0", "Sunday at 8:15 AM Central"),
        ),
        (
            {"cadence_preset": "raw", "cadence": "*/15 * * * *", "cadence_label": "Quarter hour"},
            ("*/15 * * * *", "Quarter hour"),
        ),
    ],
)
def test_cadence_presets(form: dict[str, str], expected: tuple[str, str]) -> None:

    assert cadence_from_form(form) == expected


def test_next_run_crosses_fall_dst_repeated_hour() -> None:
    first = next_run("30 1 * * *", datetime(2026, 11, 1, 6, tzinfo=UTC))
    second = next_run("30 1 * * *", first)
    assert first == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert second == datetime(2026, 11, 1, 7, 30, tzinfo=UTC)


def test_run_job_persists_metadata_through_real_submit(monkeypatch: pytest.MonkeyPatch) -> None:

    submission = importlib.import_module("crucible.application.submit_task")
    # Registry policy/routing checks have their own tests; keep parsing, provider
    # validation, task/contract writes, hashing, and event recording real here.
    monkeypatch.setattr(submission, "validate_against_registry", lambda *_a, **_k: [])
    monkeypatch.setattr(submission, "work_branch_owner", lambda *_a, **_k: None)
    uow = _world()
    principal = Principal(id="01PRINCIPAL", name="op", role=Role.OPERATOR, created_at=NOW)
    tasks: list[Any] = []
    contracts: list[Any] = []
    jobs: list[Any] = []
    events: list[Any] = []

    def add_task(task: Task) -> None:
        assert task.principal_id == principal.id
        tasks.append(task)

    uow.tasks = SimpleNamespace(
        lock_work_branch=lambda *_a: None,
        get_by_external_id=lambda *_a: None,
        add=add_task,
        search=lambda **_kwargs: tasks,
    )
    uow.contracts = SimpleNamespace(add=contracts.append)
    uow.events = SimpleNamespace(append=events.append)
    uow.personas = SimpleNamespace(get=lambda _id: _persona())
    uow.scheduled_jobs = SimpleNamespace(
        get=lambda *_a, **_k: _job(enabled=False), save=jobs.append
    )
    uow.commit = lambda: None
    uow.executions = SimpleNamespace(list_for_task=lambda _id: [])
    uow.attempts = SimpleNamespace(list_for_task=lambda _id: [])
    context = SimpleNamespace(
        clock=SimpleNamespace(now=lambda: NOW),
        providers=[SimpleNamespace(name="docker")],
        harnesses=None,
        harness_gates={},
        credential_sources={},
        secret_providers=set(),
    )
    response = api.run_now("j", cast(Any, context), uow, principal)
    changed, task, stored, document = jobs[0], tasks[0], contracts[0], response.contract
    assert response.task_id == task.id
    assert response.job.last_run is not None
    assert response.job.last_run["task_id"] == task.id
    assert len(tasks) == len(contracts) == len(jobs) == 1
    assert stored.document["scheduled_job"] == document["scheduled_job"]
    assert stored.document["execution_request"]["provider"] == "docker"
    assert stored.document["scheduled_job"]["persona_tools"] == ["codex"]
    assert task.principal_id == principal.id
    assert changed.last_run_at == NOW
    assert changed.next_run_at is None
    with pytest.raises(ContractValidationError, match="no execution provider"):
        run_job(uow, SimpleNamespace(now=lambda: NOW), principal, "j", wired_providers=set())


@pytest.mark.parametrize("failure", ["contract", "database", "owner"])
def test_supervisor_isolates_jobs_and_uses_persisted_owner(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:

    owner = Principal(id="01PERSISTED", name="op", role=Role.ORCHESTRATOR, created_at=NOW)
    jobs = [_job(id="bad"), _job(id="good")]
    transactions: list[Any] = []
    calls: list[str] = []

    @contextmanager
    def fenced() -> Any:
        tx = SimpleNamespace(committed=False, rolled_back=False)
        tx.scheduled_jobs = SimpleNamespace(
            list_due=lambda _now: jobs,
            get=lambda job_id, **_k: next(j for j in jobs if j.id == job_id),
        )
        tx.principals = SimpleNamespace(
            get_by_name=lambda name: (
                None if failure == "owner" and len(transactions) == 2 else owner
            )
        )
        tx.commit = lambda: setattr(tx, "committed", True)
        transactions.append(tx)
        try:
            yield tx
        except Exception:
            tx.rolled_back = True
            raise

    def submit(uow: Any, clock: Any, principal: Any, job_id: str, **kwargs: Any) -> None:
        assert principal is owner
        assert kwargs["wired_providers"] == {"docker"}
        calls.append(job_id)
        if job_id == "bad":
            if failure == "contract":
                raise ContractValidationError("repository not registered")
            raise RuntimeError("database write failed")

    monkeypatch.setattr(module, "run_job", submit)
    supervisor = object.__new__(module.Supervisor)
    supervisor._clock = SimpleNamespace(now=lambda: NOW)
    supervisor._providers = {"docker": cast(Any, SimpleNamespace())}
    supervisor._harnesses = None
    supervisor._harness_gates = {}
    supervisor._credential_sources = {}
    monkeypatch.setattr(supervisor, "_fenced", fenced)
    assert supervisor._scheduled_jobs_step() == 1
    assert transactions[1].rolled_back
    assert not transactions[1].committed
    assert transactions[2].committed
    assert calls[-1] == "good"


def test_inbox_board_uses_persisted_tags_and_completion_findings() -> None:

    uow, _calls = fixture()
    task = next(item for item in uow.tasks.rows if item.state is TaskState.SUBMITTED)
    contract = next(item for item in uow.contracts.rows if item.task_id == task.id)
    contract.document["scheduled_job"] = {
        "id": "j",
        "results_to": "inbox_card",
        "tags": ["scheduled-job", "inbox"],
        "persona_tools": ["codex"],
    }
    uow.executions = Repo([row(id="e", task_id=task.id, role=ExecutionRole.IMPLEMENT)])
    uow.attempts = Repo(
        [
            row(
                id="a",
                execution_id="e",
                task_id=task.id,
                created_at=NOW,
                state=AttemptState.SUCCEEDED,
                selected_harness="codex",
                selected_model="gpt",
            )
        ]
    )
    uow.claims = SimpleNamespace(
        get=lambda _id: row(
            parsed_ok=True, document={"summary": "Found three obsolete dependencies."}
        )
    )
    lanes = board_lanes_view(uow, NOW)["lanes"]
    inbox = next(lane for lane in lanes if lane["key"] == "inbox")
    card = next(card for card in inbox["cards"] if card["id"] == task.id)
    assert card["waiting_on"] == "Found three obsolete dependencies."
    assert all(
        card["id"] != task.id for lane in lanes if lane["key"] != "inbox" for card in lane["cards"]
    )


@pytest.mark.parametrize("destination", ["chat_message", "report_only"])
def test_last_run_exposes_saved_findings(destination: str) -> None:
    job = _job(last_run_at=NOW, results_to=destination)
    uow = _world()
    uow.tasks = SimpleNamespace(search=lambda **_kw: [row(id="t", state=TaskState.REPORTED)])
    uow.executions = SimpleNamespace(
        list_for_task=lambda _id: [row(id="e", role=ExecutionRole.IMPLEMENT)]
    )
    uow.attempts = SimpleNamespace(
        list_for_task=lambda _id: [row(id="a", execution_id="e", created_at=NOW)]
    )
    uow.claims = SimpleNamespace(
        get=lambda _id: row(parsed_ok=True, document={"summary": "Audit findings"})
    )
    result = last_run_result(uow, job)
    assert result is not None
    assert result["findings"] == "Audit findings"
    assert bool(result["delivery_note"]) == (destination == "chat_message")


def test_jobs_page_renders_presets_and_click_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = Principal(id="op", name="op", role=Role.OPERATOR, created_at=NOW)
    monkeypatch.setattr(pages, "_require", lambda *_args: (principal, "csrf"))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/ui/jobs",
            "query_string": b"",
            "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace())),
        }
    )
    uow = SimpleNamespace(
        personas=SimpleNamespace(list_all=lambda: [_persona()]),
        scheduled_jobs=SimpleNamespace(list_all=lambda: [_job()]),
    )
    response = pages.jobs_page(request, cast(Any, SimpleNamespace()), cast(Any, uow))
    html = bytes(response.body).decode()
    for name in ("cadence_preset", "daily_time", "weekly_day", "cadence", "task_text"):
        assert f'name="{name}"' in html
    assert 'method="post" action="/ui/jobs/j/run-now"' in html
    assert 'method="post" action="/ui/jobs/j/toggle"' in html
    assert "Disable" in html
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in html
    assert "neon-table" in html and "lat-table-scroll" not in html
