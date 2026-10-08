"""/events, /executions/{id}, /attempts/{id}, /repositories/{name} (04)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from crucible.adapters.api.deps import Admin, Ctx, Orchestrator, Reader, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.application.artifacts import read_artifact, upload_artifact
from crucible.application.auth import authenticate
from crucible.application.errors import NotFoundError, UnauthorizedError
from crucible.application.queries import (
    artifact_view,
    attempt_artifacts,
    attempt_evidence,
    attempt_gates,
    attempt_report,
    attempt_view,
    execution_view,
    global_events,
)
from crucible.application.repositories import register_repository
from crucible.contracts.api import (
    ArtifactList,
    ArtifactView,
    AttemptView,
    CompletionClaimView,
    EventList,
    EvidenceList,
    ExecutionView,
    GateList,
    RepositoryRegistration,
    RepositoryView,
)
from crucible.domain.entities import LogChunkRecord, Repository

router = ThreadedAPIRouter()


@router.get("/events", response_model=EventList)
def events(
    uow: UoW,
    _principal: Reader,
    cursor: str | None = None,
    kind: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> EventList:
    return global_events(uow, cursor=cursor, kind=kind, since=since, limit=limit)


@router.get("/executions/{execution_id}", response_model=ExecutionView)
def get_execution(execution_id: str, uow: UoW, _principal: Reader) -> ExecutionView:
    return execution_view(uow, execution_id)


@router.get("/attempts/{attempt_id}", response_model=AttemptView)
def get_attempt(attempt_id: str, uow: UoW, _principal: Reader) -> AttemptView:
    return attempt_view(uow, attempt_id)


def _log_body(chunks: list[LogChunkRecord], offset: int) -> tuple[bytes, int]:
    body = bytearray()
    next_offset = offset
    for item in chunks:
        chunk = item
        start = max(offset, chunk.offset_start)
        body.extend(chunk.content[start - chunk.offset_start :])
        next_offset = max(next_offset, chunk.offset_end)
    return bytes(body), next_offset


@router.get("/attempts/{attempt_id}/logs")
async def get_attempt_logs(
    attempt_id: str,
    request: Request,
    ctx: Ctx,
    authorization: Annotated[str | None, Header()] = None,
    stream: Literal["stdout", "stderr"] | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    """Read stored bytes, or follow newly appended chunks with server-sent events (04)."""
    # Streaming responses finalize yield dependencies only when the stream closes.
    # Authenticate and take the initial snapshot in a short local UoW so a live tail
    # never holds a pool connection for its lifetime.
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("a bearer token is required")
    with ctx.uow_factory() as initial:
        principal = authenticate(initial, authorization[7:].strip())
        if principal is None:
            raise UnauthorizedError("token not recognized")
        request.state.principal = principal
        attempt = initial.attempts.get(attempt_id)
        if attempt is None:
            raise NotFoundError(f"attempt {attempt_id} not found")
        chunks = list(initial.logs.list_from_offset(attempt_id, offset=offset, stream=stream))
        drained = attempt.logs_drained_at is not None
    if "text/event-stream" not in request.headers.get("accept", ""):
        body, next_offset = _log_body(chunks, offset)
        return Response(
            content=body,
            media_type="text/plain",
            headers={
                "X-Crucible-Log-Offset": str(next_offset),
                "X-Crucible-Logs-Drained": str(drained).lower(),
            },
        )

    async def events() -> AsyncIterator[str]:
        cursor = offset
        while True:
            with ctx.uow_factory() as fresh:
                current = fresh.attempts.get(attempt_id)
                if current is None:
                    return
                chunks = list(fresh.logs.list_from_offset(attempt_id, offset=cursor, stream=stream))
                drained = current.logs_drained_at is not None
            for chunk in chunks:
                start = max(cursor, chunk.offset_start)
                content = chunk.content[start - chunk.offset_start :].decode("utf-8", "replace")
                cursor = max(cursor, chunk.offset_end)
                data = json.dumps(
                    {
                        "offset_start": start,
                        "offset_end": chunk.offset_end,
                        "content": content,
                    },
                    separators=(",", ":"),
                )
                yield f"id: {cursor}\nevent: {chunk.stream}\ndata: {data}\n\n"
            if drained and not chunks:
                yield f'id: {cursor}\nevent: end\ndata: {{"offset":{cursor}}}\n\n'
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _repo_view(repo: Repository) -> RepositoryView:
    return RepositoryView(
        id=repo.id,
        name=repo.name,
        url=repo.url,
        default_branch=repo.default_branch,
        policy_name=repo.policy_name,
        installation_id=repo.installation_id,
        registered_by=repo.registered_by,
        created_at=repo.created_at,
        external_review_attested=repo.external_review_attested,
        attested_by=repo.attested_by,
        attested_at=repo.attested_at,
        private=repo.private,
    )


@router.get("/repositories/{name}", response_model=RepositoryView)
def get_repository(name: str, uow: UoW, _principal: Reader) -> RepositoryView:
    repo = uow.repositories.get_by_name(name)
    if repo is None:
        raise NotFoundError(f"repository {name!r} is not registered")
    return _repo_view(repo)


@router.put("/repositories/{name}", response_model=RepositoryView)
def put_repository(
    name: str, body: RepositoryRegistration, ctx: Ctx, uow: UoW, principal: Admin
) -> RepositoryView:
    repo = register_repository(
        uow,
        ctx.clock,
        principal_name=principal.name,
        name=name,
        registration=body,
        github=ctx.admin.github if ctx.admin is not None else None,
    )
    uow.commit()
    return _repo_view(repo)


@router.get("/attempts/{attempt_id}/gates", response_model=GateList)
def get_gates(attempt_id: str, uow: UoW, _principal: Reader) -> GateList:
    return attempt_gates(uow, attempt_id)


@router.get("/attempts/{attempt_id}/evidence", response_model=EvidenceList)
def get_evidence(attempt_id: str, uow: UoW, _principal: Reader) -> EvidenceList:
    return attempt_evidence(uow, attempt_id)


@router.get("/attempts/{attempt_id}/report", response_model=CompletionClaimView)
def get_report(attempt_id: str, uow: UoW, _principal: Reader) -> CompletionClaimView:
    return attempt_report(uow, attempt_id)


@router.get("/attempts/{attempt_id}/artifacts", response_model=ArtifactList)
def list_attempt_artifacts(attempt_id: str, uow: UoW, _principal: Reader) -> ArtifactList:
    return attempt_artifacts(uow, attempt_id)


@router.post("/attempts/{attempt_id}/artifacts", response_model=ArtifactView, status_code=201)
async def post_attempt_artifact(
    attempt_id: str,
    request: Request,
    ctx: Ctx,
    uow: UoW,
    principal: Orchestrator,
    type: Annotated[str, Query(min_length=1)],
    filename: Annotated[str, Query(min_length=1)],
) -> ArtifactView:
    """Upload an artifact. 04 describes multipart; this takes the bytes as the request
    body with `type` and `filename` as query parameters, because multipart parsing would
    need a dependency Crucible does not carry. Recorded in docs/implementation-notes/c2.md."""
    content = await request.body()
    artifact = upload_artifact(
        uow,
        ctx.clock,
        ctx.artifact_store,
        principal=principal,
        attempt_id=attempt_id,
        artifact_type=type,
        filename=filename,
        content=content,
        content_type=request.headers.get("content-type", "application/octet-stream"),
    )
    uow.commit()
    return artifact_view(uow, artifact.id)


@router.get("/artifacts/{artifact_id}", response_model=ArtifactView)
def get_artifact(artifact_id: str, uow: UoW, _principal: Reader) -> ArtifactView:
    return artifact_view(uow, artifact_id)


@router.get("/artifacts/{artifact_id}/content")
def get_artifact_content(artifact_id: str, ctx: Ctx, uow: UoW, _principal: Reader) -> Response:
    artifact, content = read_artifact(uow, ctx.artifact_store, artifact_id)
    return Response(
        content=content,
        media_type=artifact.content_type,
        headers={"X-Crucible-Artifact-Sha256": artifact.sha256},
    )
