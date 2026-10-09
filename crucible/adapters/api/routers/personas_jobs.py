"""Persona and scheduled-job CRUD, plus immediate scheduled runs."""

from crucible.adapters.api.deps import Ctx, Orchestrator, Reader, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.errors import ConflictError, NotFoundError
from crucible.application.personas_jobs import (
    create_job,
    create_persona,
    require_job,
    require_persona,
    run_job,
    update_job,
    update_persona,
)
from crucible.contracts.api import (
    PersonaList,
    PersonaRequest,
    PersonaView,
    ScheduledJobList,
    ScheduledJobRequest,
    ScheduledJobView,
    ScheduledRunView,
)
from crucible.domain.entities import Persona, ScheduledJob

router = ThreadedAPIRouter()


def persona_view(item: Persona) -> PersonaView:
    return PersonaView.model_validate(item, from_attributes=True)


def job_view(item: ScheduledJob) -> ScheduledJobView:
    return ScheduledJobView.model_validate(item, from_attributes=True)


@router.get("/personas", response_model=PersonaList)
def personas(uow: UoW, _principal: Reader) -> PersonaList:
    return PersonaList(items=[persona_view(item) for item in uow.personas.list_all()])


@router.post("/personas", response_model=PersonaView, status_code=201)
def persona_create(
    body: PersonaRequest, ctx: Ctx, uow: UoW, principal: Orchestrator
) -> PersonaView:
    item = create_persona(uow, ctx.clock, principal, body)
    uow.commit()
    return persona_view(item)


@router.get("/personas/{persona_id}", response_model=PersonaView)
def persona_get(persona_id: str, uow: UoW, _principal: Reader) -> PersonaView:
    return persona_view(require_persona(uow, persona_id))


@router.put("/personas/{persona_id}", response_model=PersonaView)
def persona_update(
    persona_id: str, body: PersonaRequest, ctx: Ctx, uow: UoW, _principal: Orchestrator
) -> PersonaView:
    item = update_persona(uow, ctx.clock, persona_id, body)
    uow.commit()
    return persona_view(item)


@router.delete("/personas/{persona_id}", status_code=204)
def persona_delete(persona_id: str, uow: UoW, _principal: Orchestrator) -> None:
    if any(job.persona_id == persona_id for job in uow.scheduled_jobs.list_all()):
        raise ConflictError("persona is referenced by a scheduled job")
    if not uow.personas.delete(persona_id):
        raise NotFoundError("persona was not found")
    uow.commit()


@router.get("/scheduled_jobs", response_model=ScheduledJobList)
def jobs(uow: UoW, _principal: Reader) -> ScheduledJobList:
    return ScheduledJobList(items=[job_view(item) for item in uow.scheduled_jobs.list_all()])


@router.post("/scheduled_jobs", response_model=ScheduledJobView, status_code=201)
def job_create(
    body: ScheduledJobRequest, ctx: Ctx, uow: UoW, principal: Orchestrator
) -> ScheduledJobView:
    item = create_job(uow, ctx.clock, principal, body)
    uow.commit()
    return job_view(item)


@router.get("/scheduled_jobs/{job_id}", response_model=ScheduledJobView)
def job_get(job_id: str, uow: UoW, _principal: Reader) -> ScheduledJobView:
    return job_view(require_job(uow, job_id))


@router.put("/scheduled_jobs/{job_id}", response_model=ScheduledJobView)
def job_update(
    job_id: str, body: ScheduledJobRequest, ctx: Ctx, uow: UoW, _principal: Orchestrator
) -> ScheduledJobView:
    item = update_job(uow, ctx.clock, job_id, body)
    uow.commit()
    return job_view(item)


@router.delete("/scheduled_jobs/{job_id}", status_code=204)
def job_delete(job_id: str, uow: UoW, _principal: Orchestrator) -> None:
    if not uow.scheduled_jobs.delete(job_id):
        raise NotFoundError("scheduled job was not found")
    uow.commit()


@router.post("/scheduled_jobs/{job_id}/run-now", response_model=ScheduledRunView, status_code=201)
def run_now(job_id: str, ctx: Ctx, uow: UoW, principal: Orchestrator) -> ScheduledRunView:
    job, task, _stored, contract = run_job(uow, ctx.clock, principal, job_id)
    uow.commit()
    return ScheduledRunView(job=job_view(job), task_id=task.id, contract=contract)
