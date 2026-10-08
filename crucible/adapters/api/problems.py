"""RFC 9457 problem responses and the exception handlers that produce them."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from crucible.adapters.api.deps import AppContext
from crucible.application.errors import ApplicationError, TransitionNotAllowedError
from crucible.application.transitions import record_rejected_transition
from crucible.contracts.problem import ProblemDetails, problem_type
from crucible.domain.entities import Principal
from crucible.domain.lifecycle import IllegalTransitionError

PROBLEM_MEDIA_TYPE = "application/problem+json"


def problem_response(
    *,
    slug: str,
    title: str,
    status: int,
    detail: str | None = None,
    instance: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ProblemDetails(
        type=problem_type(slug),
        title=title,
        status=status,
        detail=detail,
        instance=instance,
        errors=errors or [],
    )
    return JSONResponse(
        status_code=status,
        content=body.model_dump(mode="json"),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApplicationError)
    async def _application_error(request: Request, exc: ApplicationError) -> JSONResponse:
        if exc.event is not None:
            ctx: AppContext = request.app.state.ctx
            with ctx.uow_factory() as uow:
                uow.events.append(exc.event)
                uow.commit()
        return problem_response(
            slug=exc.slug,
            title=exc.title,
            status=exc.status,
            detail=exc.detail,
            instance=str(request.url.path),
            errors=exc.errors,
        )

    @app.exception_handler(IllegalTransitionError)
    async def _illegal_transition(request: Request, exc: IllegalTransitionError) -> JSONResponse:
        # The attempting transaction rolled back; the rejection is recorded on its own (09).
        ctx: AppContext = request.app.state.ctx
        principal: Principal | None = getattr(request.state, "principal", None)
        with ctx.uow_factory() as uow:
            record_rejected_transition(
                uow, ctx.clock, exc, principal=principal.name if principal else "anonymous"
            )
            uow.commit()
        return problem_response(
            slug=TransitionNotAllowedError.slug,
            title=TransitionNotAllowedError.title,
            status=TransitionNotAllowedError.status,
            detail=str(exc),
            instance=str(request.url.path),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
        ]
        return problem_response(
            slug="request-invalid",
            title="Request failed validation",
            status=422,
            detail="one or more fields are invalid",
            instance=str(request.url.path),
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        slug = {401: "unauthorized", 403: "forbidden", 404: "not-found", 405: "method-not-allowed"}
        return problem_response(
            slug=slug.get(exc.status_code, "http-error"),
            title=str(exc.detail),
            status=exc.status_code,
            instance=str(request.url.path),
        )
