"""Request-scoped dependencies: settings, unit of work, authenticated principal, roles."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import Engine

from crucible.application.admin.context import AdminContext
from crucible.application.admin.login import LoginRegistry
from crucible.application.auth import authenticate
from crucible.application.credential_renewer import ReadOnlyCredentialStore
from crucible.application.errors import ForbiddenError, UnauthorizedError
from crucible.application.harnesses import HarnessRegistry
from crucible.domain.entities import Principal, Role
from crucible.ports.artifacts import ArtifactStore
from crucible.ports.clock import Clock
from crucible.ports.execution import ExecutionProvider
from crucible.ports.first_run import FirstRunDelivery
from crucible.ports.harness import CredentialSource, HarnessGate
from crucible.ports.repository import UnitOfWork, UnitOfWorkFactory


@dataclass(slots=True)
class AppContext:
    uow_factory: UnitOfWorkFactory
    clock: Clock
    providers: list[ExecutionProvider]
    database_url: str
    engine: Engine
    artifact_store: ArtifactStore
    lease_ttl_seconds: int = 30
    # 23: the webhook receiver is off by default, and its secret is a path Crucible
    # reads, never a configuration value.
    github_webhook_enabled: bool = False
    github_webhook_secret_path: str | None = None
    # 07 and 25: the adapters, the operator's configuration gates, and where each
    # harness's credential directory is. Paths and flags only, never a value.
    harnesses: HarnessRegistry | None = None
    harness_gates: dict[str, HarnessGate] = field(default_factory=dict)
    credential_sources: dict[str, CredentialSource] = field(default_factory=dict)
    # 25: the administrative services and the in-progress logins of this process.
    admin: AdminContext | None = None
    logins: LoginRegistry = field(default_factory=LoginRegistry)
    # Browser sessions are process-local and intentionally expire on restart. The
    # bearer token remains the source of identity and is never copied into state.
    ui_signing_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    settings: object | None = None
    credential_renewer: ReadOnlyCredentialStore | None = None
    # Where the migration left the first-run administrator token, removed from there
    # when that principal first signs in (ADR 0016). None where there is no such place.
    first_run: FirstRunDelivery | None = None

    @property
    def secret_providers(self) -> frozenset[str]:
        """The providers that keep the harness credentials themselves, as Secrets they
        own (ADR 0015), so a harness's credential there is not a configured directory."""
        return frozenset(
            provider.name
            for provider in self.providers
            if callable(getattr(provider, "read_credential_files", None))
        )


def app_context(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def unit_of_work(ctx: Annotated[AppContext, Depends(app_context)]) -> Iterator[UnitOfWork]:
    with ctx.uow_factory() as uow:
        yield uow


def current_principal(
    request: Request,
    uow: Annotated[UnitOfWork, Depends(unit_of_work)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("a bearer token is required")
    principal = authenticate(uow, authorization[7:].strip())
    if principal is None:
        raise UnauthorizedError("token not recognized")
    request.state.principal = principal
    return principal


def require_mutating(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if not principal.role.may_mutate:
        raise ForbiddenError(f"role {principal.role.value} may only read")
    return principal


def require_orchestrator(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    """04: the orchestrator surface, which an operator principal may also use."""
    if principal.role not in (Role.ORCHESTRATOR, Role.OPERATOR):
        raise ForbiddenError("orchestrator or operator role required")
    return principal


def require_operator(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if principal.role not in (Role.OPERATOR, Role.ADMIN):
        raise ForbiddenError("operator or admin role required")
    return principal


def require_admin(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if principal.role is not Role.ADMIN:
        raise ForbiddenError("admin role required")
    return principal


UoW = Annotated[UnitOfWork, Depends(unit_of_work)]
Ctx = Annotated[AppContext, Depends(app_context)]
Reader = Annotated[Principal, Depends(current_principal)]
Mutator = Annotated[Principal, Depends(require_mutating)]
Admin = Annotated[Principal, Depends(require_admin)]
Orchestrator = Annotated[Principal, Depends(require_orchestrator)]
Operator = Annotated[Principal, Depends(require_operator)]
