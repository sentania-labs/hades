"""Policy and routing-policy upload and read (04, 05b).

A version is immutable once a task references it. `allow_no_ci`, `allow_branch_only`, and
turning off `release.require_operator_approval` may only be uploaded by an operator or
admin principal, and each is recorded as a decision."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from crucible.application.errors import (
    ConflictError,
    ContractValidationError,
    ForbiddenError,
    NotFoundError,
)
from crucible.application.transitions import record_event
from crucible.contracts.common import to_document
from crucible.contracts.policy import PolicyV1, RoutingPolicyV1
from crucible.domain.entities import Decision, Policy, Principal, Role, RoutingPolicyRecord
from crucible.domain.events import EventKind
from crucible.domain.harness_concurrency import HARNESS_CONCURRENCY, HarnessConcurrency
from crucible.domain.ids import new_id
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork


def _problems(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"path": ".".join(str(p) for p in err["loc"]) or "$", "message": err["msg"]}
        for err in exc.errors(include_url=False, include_input=False)
    ]


def validate_policy(
    document: object,
    *,
    name: str,
    version: int,
    concurrency_modes: dict[str, str] | None = None,
) -> PolicyV1:
    try:
        policy = PolicyV1.model_validate(document)
    except ValidationError as exc:
        raise ContractValidationError("policy failed validation", errors=_problems(exc)) from None
    problems: list[dict[str, Any]] = []
    if policy.name != name:
        problems.append({"path": "name", "message": f"the path says {name!r}"})
    if policy.version != version:
        problems.append({"path": "version", "message": f"the path says {version}"})
    # Preserve the required frontier caps; optional harnesses are checked when present.
    for harness in sorted({"claude_code", "codex", "agy"} | policy.concurrency.per_harness.keys()):
        declaration = HARNESS_CONCURRENCY.get(harness, HarnessConcurrency())
        if harness == "codex":
            mode = (concurrency_modes or {}).get("codex", declaration.minimum_mode)
            declaration = replace(declaration, renewer_held=mode != "rw-narrow")
        limit = policy.concurrency.per_harness.get(harness)
        if limit is None:
            problems.append(
                {
                    "path": f"concurrency.per_harness.{harness}",
                    "message": "every supported harness needs a concurrency cap",
                }
            )
        elif limit > 1 and not declaration.allows_parallel:
            problems.append(
                {
                    "path": f"concurrency.per_harness.{harness}",
                    "message": (
                        f"{harness} has no read-only credential declaration or parallel-attempt "
                        "safety declaration for rw-narrow copies (12), so concurrency must be 1"
                    ),
                }
            )
    if problems:
        raise ContractValidationError("policy failed validation", errors=problems)
    return policy


def put_policy(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    name: str,
    version: int,
    document: object,
    reason: str | None = None,
    concurrency_modes: dict[str, str] | None = None,
) -> Policy:
    policy = validate_policy(
        document, name=name, version=version, concurrency_modes=concurrency_modes
    )
    existing = uow.policies.get(name, version)
    if existing is not None and uow.policies.is_referenced(name, version):
        raise ConflictError(
            f"policy {name}/{version} is referenced by at least one task and is immutable (05b)"
        )
    operator_only = policy.operator_only_settings()
    if operator_only and principal.role not in (Role.OPERATOR, Role.ADMIN):
        raise ForbiddenError(
            "these settings may only be uploaded by an operator or admin principal: "
            + ", ".join(operator_only),
            errors=[{"path": field, "message": "operator-only (05b)"} for field in operator_only],
        )
    routing = uow.routing_policies.get(policy.routing.policy.name, policy.routing.policy.version)
    if routing is None:
        raise ContractValidationError(
            "policy names a routing policy that does not exist",
            errors=[
                {
                    "path": "routing.policy",
                    "message": (
                        f"{policy.routing.policy.name}/{policy.routing.policy.version} "
                        "is not uploaded"
                    ),
                }
            ],
        )
    now = clock.now()
    stored = uow.policies.put(
        Policy(
            name=name,
            version=version,
            document=to_document(policy),
            created_at=existing.created_at if existing else now,
        )
    )
    record_event(
        uow,
        clock,
        EventKind.POLICY_UPLOADED,
        principal=principal.name,
        payload={
            "policy": {"name": name, "version": version},
            "replaced": existing is not None,
            "operator_only_settings": operator_only,
            **(
                {
                    "reason": reason,
                    "before": {"present": existing is not None},
                    "after": {"present": True, "version": version},
                }
                if reason is not None
                else {}
            ),
        },
    )
    # The local admin CLI acts on the host with no principal row; its upload event above
    # names it and the settings, and a Decision row needs a principal to point at.
    recorded = uow.principals.get(principal.id) is not None
    for field in operator_only if recorded else ():
        uow.decisions.add(
            Decision(
                id=new_id(),
                task_id=None,
                escalation_id=None,
                principal_id=principal.id,
                kind="policy_operator_setting",
                verbatim=f"{principal.name} uploaded {name}/{version} with {field} set",
                resolves=field,
                created_at=now,
            )
        )
    return stored


def get_policy(uow: UnitOfWork, *, name: str, version: int) -> Policy:
    policy = uow.policies.get(name, version)
    if policy is None:
        raise NotFoundError(f"policy {name}/{version} does not exist")
    return policy


def validate_routing_policy(
    document: object,
    *,
    name: str,
    version: int,
    local_model_listing: list[str] | None = None,
) -> RoutingPolicyV1:
    try:
        routing = RoutingPolicyV1.model_validate(document)
    except ValidationError as exc:
        raise ContractValidationError(
            "routing policy failed validation", errors=_problems(exc)
        ) from None
    problems: list[dict[str, Any]] = []
    if routing.name != name:
        problems.append({"path": "name", "message": f"the path says {name!r}"})
    if routing.version != version:
        problems.append({"path": "version", "message": f"the path says {version}"})
    if local_model_listing is not None:
        listed = set(local_model_listing)
        for index, entry in enumerate(routing.models):
            if entry.endpoint == "local" and entry.model not in listed:
                problems.append(
                    {
                        "path": f"models.{index}.model",
                        "message": (
                            f"unknown local model {entry.model!r}; the gateway listing "
                            f"checked at publish time was {sorted(listed)!r}"
                        ),
                    }
                )
    if problems:
        raise ContractValidationError("routing policy failed validation", errors=problems)
    return routing


def put_routing_policy(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    name: str,
    version: int,
    document: object,
    reason: str | None = None,
    extra: Mapping[str, Any] | None = None,
    local_model_listing: list[str] | None = None,
) -> RoutingPolicyRecord:
    """`extra` joins the event payload: a publish records its delta there (hades #437)."""
    routing = validate_routing_policy(
        document, name=name, version=version, local_model_listing=local_model_listing
    )
    existing = uow.routing_policies.get(name, version)
    if existing is not None and uow.routing_policies.is_referenced(name, version):
        raise ConflictError(
            f"routing policy {name}/{version} is referenced by a policy and is immutable (05b)"
        )
    if existing is not None and uow.attempts.routes_with(name, version):
        raise ConflictError(
            f"routing policy {name}/{version} was routed with by an attempt and is immutable "
            "(hades #254)"
        )
    now = clock.now()
    stored = uow.routing_policies.put(
        RoutingPolicyRecord(
            name=name,
            version=version,
            document=to_document(routing),
            created_at=existing.created_at if existing else now,
        )
    )
    record_event(
        uow,
        clock,
        EventKind.ROUTING_POLICY_UPLOADED,
        principal=principal.name,
        payload={
            "routing_policy": {"name": name, "version": version},
            "models": len(routing.models),
            "replaced": existing is not None,
            **(
                {
                    "reason": reason,
                    "before": {"present": existing is not None},
                    "after": {"present": True, "version": version},
                }
                if reason is not None
                else {}
            ),
            **(extra or {}),
        },
    )
    return stored


def get_routing_policy(uow: UnitOfWork, *, name: str, version: int) -> RoutingPolicyRecord:
    routing = uow.routing_policies.get(name, version)
    if routing is None:
        raise NotFoundError(f"routing policy {name}/{version} does not exist")
    return routing
