"""Personas, scheduled jobs, cron calculation, and scheduled task contracts."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from crucible.application.catalog import load_catalog
from crucible.application.errors import ContractValidationError, NotFoundError
from crucible.application.harnesses import HarnessRegistry
from crucible.application.memory import recall_memory
from crucible.application.submit_task import submit_task
from crucible.contracts.api import PersonaRequest, ScheduledJobRequest
from crucible.domain.entities import (
    ExecutionRole,
    Persona,
    Principal,
    ScheduledJob,
    Task,
    TaskContract,
)
from crucible.domain.ids import new_id
from crucible.ports.clock import Clock
from crucible.ports.harness import CredentialSource, HarnessGate
from crucible.ports.repository import UnitOfWork

PROTECTED_PATHS = [".github/**", "**/secrets*", "uv.lock"]


def validate_catalog_references(skills: list[str], tools: list[str]) -> None:
    catalog = load_catalog()
    known_skills = {item.name for item in catalog.skills}
    known_tools = {item.name for item in catalog.tools}
    unknown_skills = sorted(set(skills) - known_skills)
    unknown_tools = sorted(set(tools) - known_tools)
    if unknown_skills or unknown_tools:
        parts = []
        if unknown_skills:
            parts.append(f"unknown catalog skills: {unknown_skills}")
        if unknown_tools:
            parts.append(f"unknown catalog tools: {unknown_tools}")
        raise ContractValidationError("; ".join(parts))


def create_persona(
    uow: UnitOfWork, clock: Clock, principal: Principal, body: PersonaRequest
) -> Persona:
    validate_catalog_references(body.skills, body.tools)
    now = clock.now()
    persona = Persona(
        id=new_id(), created_by=principal.name, created_at=now, updated_at=now, **body.model_dump()
    )
    uow.personas.add(persona)
    return persona


def update_persona(uow: UnitOfWork, clock: Clock, persona_id: str, body: PersonaRequest) -> Persona:
    found = require_persona(uow, persona_id)
    validate_catalog_references(body.skills, body.tools)
    changed = replace(found, updated_at=clock.now(), **body.model_dump())
    uow.personas.save(changed)
    return changed


def require_persona(uow: UnitOfWork, persona_id: str) -> Persona:
    found = uow.personas.get(persona_id)
    if found is None:
        raise NotFoundError(f"persona {persona_id!r} was not found")
    return found


def _field_matches(field: str, value: int, *, weekday: bool = False) -> bool:
    for part in field.split(","):
        if part == "*":
            return True
        if part.startswith("*/"):
            try:
                return value % int(part[2:]) == 0
            except (ValueError, ZeroDivisionError):
                return False
        try:
            wanted = int(part)
            if weekday and wanted == 7:
                wanted = 0
            if value == wanted:
                return True
        except ValueError:
            return False
    return False


def cron_matches(expression: str, instant: datetime, timezone: str = "America/Chicago") -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cadence must be a five-field cron expression")
    local = instant.astimezone(ZoneInfo(timezone))
    cron_weekday = (local.weekday() + 1) % 7
    values = (local.minute, local.hour, local.day, local.month, cron_weekday)
    return all(
        _field_matches(field, value, weekday=index == 4)
        for index, (field, value) in enumerate(zip(fields, values, strict=True))
    )


def next_run(expression: str, after: datetime, timezone: str = "America/Chicago") -> datetime:
    cursor = after.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 366 * 5):
        if cron_matches(expression, cursor, timezone):
            return cursor
        cursor += timedelta(minutes=1)
    raise ValueError("cadence has no run time in the next five years")


def create_job(
    uow: UnitOfWork, clock: Clock, principal: Principal, body: ScheduledJobRequest
) -> ScheduledJob:
    require_persona(uow, body.persona_id)
    upcoming = next_run(body.cadence, clock.now(), body.timezone) if body.enabled else None
    job = ScheduledJob(
        id=new_id(),
        last_run_at=None,
        next_run_at=upcoming,
        created_by=principal.name,
        **body.model_dump(),
    )
    uow.scheduled_jobs.add(job)
    return job


def require_job(uow: UnitOfWork, job_id: str, *, lock: bool = False) -> ScheduledJob:
    found = uow.scheduled_jobs.get(job_id, for_update=lock)
    if found is None:
        raise NotFoundError(f"scheduled job {job_id!r} was not found")
    return found


def update_job(
    uow: UnitOfWork, clock: Clock, job_id: str, body: ScheduledJobRequest
) -> ScheduledJob:
    found = require_job(uow, job_id, lock=True)
    require_persona(uow, body.persona_id)
    upcoming = next_run(body.cadence, clock.now(), body.timezone) if body.enabled else None
    changed = replace(found, next_run_at=upcoming, **body.model_dump())
    uow.scheduled_jobs.save(changed)
    return changed


def generate_contract(
    uow: UnitOfWork, job: ScheduledJob, persona: Persona, now: datetime, *, provider: str
) -> dict[str, object]:
    repository = uow.repositories.get_by_name(job.project)
    if repository is None:
        raise ContractValidationError(f"project repository {job.project!r} is not registered")
    versions = uow.policies.list_versions(repository.policy_name)
    if not versions:
        raise ContractValidationError(f"policy {repository.policy_name!r} is not uploaded")
    policy = max(versions, key=lambda item: item.version)
    checks = [
        str(value) for value in policy.document.get("repository", {}).get("required_checks", [])
    ]
    recalled = (
        recall_memory(uow, subject=job.name, tags=[]).items if job.carry_notes_forward else []
    )
    notes = ""
    if recalled:
        notes = "\n\nEarlier findings recalled from memory:\n" + "\n".join(
            f"- {item.text}" for item in recalled
        )
    objective = f"{persona.role_text.strip()}\n\n{job.task_text.strip()}{notes}"
    result_instruction = {
        "inbox_card": (
            "Put the run findings in the Inbox card body. The scheduled-job:inbox tag "
            "keeps this task in the Inbox lane."
        ),
        "chat_message": (
            "Record the findings on this run and post a system turn to the principal "
            "room when the rooms API exists. If it does not exist, record that fact "
            "with the findings."
        ),
        "report_only": "Record the findings on this run only.",
    }[job.results_to]
    external_id = run_external_id(job, now)
    return {
        "schema_version": "1.0",
        "external_id": external_id,
        "title": f"{job.name} {now.astimezone(ZoneInfo(job.timezone)):%Y-%m-%d}",
        "project": job.project,
        "parent_external_id": None,
        "repository": {"name": repository.name, "base_ref": repository.default_branch},
        "scope": {
            "allowed_paths": ["**"],
            "prohibited_paths": PROTECTED_PATHS,
            "may_add_dependencies": False,
            "may_modify_ci": False,
        },
        "objective": objective,
        "context": [],
        "project_instructions": [{"kind": "skill", "ref": name} for name in persona.skills],
        "acceptance_criteria": [{"id": "AC1", "text": result_instruction}],
        "required_verification": [
            {"id": f"V{index}", "kind": "command", "command": command, "expect_exit": 0}
            for index, command in enumerate(checks, 1)
        ]
        or [{"id": "V1", "kind": "command", "command": "git diff --check", "expect_exit": 0}],
        "constraints": {
            "prohibited_actions": [
                "modify protected paths",
                "Catalog tools recorded for a later task, not mounted: "
                + (", ".join(persona.tools) or "none"),
            ],
            "network": "policy",
        },
        "deliverables": [{"kind": "artifacts"}],
        "reporting": {
            "report_schema": "CompletionClaimV1",
            "report_dir": "/crucible/report",
            "progress_events": True,
        },
        "escalation": {
            "conditions": ["the scheduled task is ambiguous"],
            "action": "write report/blocked.md and exit 75",
        },
        "policy": {"name": policy.name, "version": policy.version},
        "execution_request": {
            "tier": persona.default_tier,
            "provider": provider,
            "timeout_seconds": 3600,
            "rationale": (
                f"Scheduled persona {persona.name}; preferred harness/model recorded: "
                f"{persona.default_harness}/{persona.default_model}"
            ),
        },
        "lifecycle": {"max_attempts": 1, "retry_on": [], "cleanup": "policy"},
        "correction": None,
        "scheduled_job": {
            "id": job.id,
            "results_to": job.results_to,
            "tags": ["scheduled-job", "inbox"]
            if job.results_to == "inbox_card"
            else ["scheduled-job"],
            "persona_tools": persona.tools,
        },
    }


def run_job(
    uow: UnitOfWork,
    clock: Clock,
    principal: Principal,
    job_id: str,
    *,
    wired_providers: Collection[str],
    harnesses: HarnessRegistry | None = None,
    harness_gates: Mapping[str, HarnessGate] | None = None,
    credential_sources: Mapping[str, CredentialSource] | None = None,
    secret_providers: Collection[str] = (),
) -> tuple[ScheduledJob, Task, TaskContract, dict[str, object]]:
    job = require_job(uow, job_id, lock=True)
    persona = require_persona(uow, job.persona_id)
    now = clock.now()
    provider = next(
        (
            name
            for name in ("kubernetes", "docker", "hostprocess", "fake")
            if name in wired_providers
        ),
        None,
    )
    if provider is None:
        raise ContractValidationError("no execution provider is configured for scheduled jobs")
    document = generate_contract(uow, job, persona, now, provider=provider)
    task, stored = submit_task(
        uow,
        clock,
        principal=principal,
        body=document,
        wired_providers=wired_providers,
        harnesses=harnesses,
        harness_gates=harness_gates,
        credential_sources=credential_sources,
        secret_providers=secret_providers,
    )
    changed = replace(
        job,
        last_run_at=now,
        next_run_at=next_run(job.cadence, now, job.timezone) if job.enabled else None,
    )
    uow.scheduled_jobs.save(changed)
    return changed, task, stored, stored.document


def run_external_id(job: ScheduledJob, now: datetime) -> str:
    return f"JOB-{job.id}-{now.astimezone(ZoneInfo(job.timezone)):%Y%m%d-%H%M%S-%f%z}"


def inbox_run(document: dict[str, object]) -> bool:
    metadata = document.get("scheduled_job")
    return (
        isinstance(metadata, dict)
        and metadata.get("results_to") == "inbox_card"
        and "inbox" in metadata.get("tags", [])
    )


def run_findings(uow: UnitOfWork, task_id: str) -> str | None:
    work = {
        execution.id
        for execution in uow.executions.list_for_task(task_id)
        if execution.role is not ExecutionRole.REVIEW
    }
    attempts = sorted(
        (
            attempt
            for attempt in uow.attempts.list_for_task(task_id)
            if attempt.execution_id in work
        ),
        key=lambda attempt: (attempt.created_at, attempt.id),
        reverse=True,
    )
    for attempt in attempts:
        claim = uow.claims.get(attempt.id)
        if claim is not None and claim.parsed_ok:
            summary = claim.document.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary
    return None


def last_run_result(uow: UnitOfWork, job: ScheduledJob) -> dict[str, object] | None:
    if job.last_run_at is None:
        return None
    tasks = uow.tasks.search(
        state=None,
        project=None,
        repository_id=None,
        external_id=run_external_id(job, job.last_run_at),
        updated_since=None,
        after_id=None,
        limit=1,
    )
    if not tasks:
        return None
    task = tasks[0]
    return {
        "task_id": task.id,
        "state": task.state.value,
        "findings": run_findings(uow, task.id),
        "delivery_note": "Rooms API is unavailable; findings are recorded on this run."
        if job.results_to == "chat_message"
        else None,
    }
