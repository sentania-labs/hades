"""POST /tasks: validate the contract against the registry, persist task and contract,
record task_submitted. Does not launch (04)."""

from __future__ import annotations

import fnmatch
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from crucible.application.errors import (
    ContractValidationError,
    DuplicateExternalIdError,
    ForbiddenError,
)
from crucible.application.harnesses import HarnessRegistry
from crucible.application.registry import REGISTERED_HARNESSES, REGISTERED_PROVIDERS
from crucible.application.routing import (
    check_quota,
    check_selection,
    image_for_harness,
    load_attempt_routing,
    select_model,
)
from crucible.application.transitions import record_event
from crucible.contracts.common import to_document
from crucible.contracts.task_contract import TaskContractV1, contract_sha256
from crucible.domain.command_timeout import policy_bounds
from crucible.domain.entities import Event, Policy, Principal, Repository, Role, Task, TaskContract
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.domain.verification import task_specific_checks
from crucible.ports.clock import Clock
from crucible.ports.harness import CredentialSource, HarnessGate, HarnessUnavailableError
from crucible.ports.repository import UnitOfWork

Problem = dict[str, Any]


def _problem(path: str, message: str) -> Problem:
    return {"path": path, "message": message}


def parse_contract(body: object) -> TaskContractV1:
    """Shape validation. Errors name the path and the rule, never a secret value."""
    try:
        return TaskContractV1.model_validate(body)
    except ValidationError as exc:
        problems = [
            _problem(".".join(str(p) for p in err["loc"]) or "$", err["msg"])
            for err in exc.errors(include_url=False, include_input=False)
        ]
        raise ContractValidationError("task contract failed validation", errors=problems) from None


def require_operator_for_pin(principal: Principal, contract: TaskContractV1) -> None:
    if contract.execution_request.pinned_model is not None and principal.role is not Role.OPERATOR:
        raise ForbiddenError(
            "only an operator may submit, amend, or correct an operator-pinned task"
        )


def _check_routing(
    uow: UnitOfWork,
    clock: Clock,
    contract: TaskContractV1,
    policy: Policy,
    eligible_harnesses: set[str] | None,
    harnesses: HarnessRegistry | None,
) -> list[Problem]:
    """The class must have a candidate now; an operator pin is validated exactly."""
    routing = load_attempt_routing(uow, policy.document)
    if routing is None:
        ref = policy.document.get("routing", {}).get("policy", {})
        return [
            _problem(
                "policy.routing",
                f"routing policy {ref.get('name')}/{ref.get('version')} is not uploaded",
            )
        ]
    request = contract.execution_request
    pinned_model = request.pinned_model
    pinned_harness = request.pinned_harness
    if pinned_model is not None and pinned_harness is not None:
        problems = check_selection(
            routing,
            tier=request.tier.value,
            harness=pinned_harness.value,
            model_id=pinned_model,
        )
        entry = routing.model(pinned_model, pinned_harness.value)
        if (
            entry is not None
            and (entry.endpoint == "local" or entry.pool in routing.local_pools())
            and not task_specific_checks(to_document(contract), policy.document)
        ):
            problems.append(_problem("execution_request.model", "no task-specific check"))
        quota = check_quota(uow, routing, model_id=pinned_model, now=clock.now())
        if quota is not None:
            problems.append(quota)
        image = image_for_harness(uow, pinned_harness.value, request.provider.value)
        if image is None and request.provider.value != "fake":
            problems.append(
                _problem("execution_request.model", "pinned harness has no default image")
            )
        allowlist = [str(p) for p in policy.document.get("images", {}).get("allowlist", [])]
        if (
            image
            and allowlist
            and not any(fnmatch.fnmatchcase(image, pattern) for pattern in allowlist)
        ):
            problems.append(
                _problem("execution_request.model", "derived image is outside the policy allowlist")
            )
        return problems
    selection = select_model(
        uow,
        routing,
        tier=request.tier.value,
        project=contract.project,
        provider=request.provider.value,
        now=clock.now(),
        contract=to_document(contract),
        policy_document=policy.document,
        eligible_harnesses=eligible_harnesses,
        harnesses=harnesses,
        image_allowlist=[
            str(pattern) for pattern in policy.document.get("images", {}).get("allowlist", [])
        ],
    )
    if selection.selected is None:
        return [
            _problem(
                "execution_request.tier",
                f"tier {request.tier.value!r} has no selectable model: "
                f"{list(selection.candidates)!r}",
            )
        ]
    allowlist = [str(p) for p in policy.document.get("images", {}).get("allowlist", [])]
    if (
        selection.image
        and allowlist
        and not any(fnmatch.fnmatchcase(selection.image, pattern) for pattern in allowlist)
    ):
        return [
            _problem(
                "execution_request.tier", "every candidate image is outside the policy allowlist"
            )
        ]
    return []


def _check_against_registry(
    contract: TaskContractV1, repository: Repository | None, policy: Policy | None
) -> tuple[list[Problem], Policy | None]:
    problems: list[Problem] = []
    if repository is None:
        problems.append(
            _problem(
                "repository.name", f"repository {contract.repository.name!r} is not registered"
            )
        )
    if policy is None:
        problems.append(
            _problem(
                "policy",
                f"policy {contract.policy.name}/{contract.policy.version} does not exist",
            )
        )
        return problems, None
    if policy.retired_at is not None:
        problems.append(_problem("policy", "policy version is retired"))
    doc = policy.document
    limits = doc.get("limits", {})
    max_attempts_cap = int(limits.get("max_attempts", {}).get("max", 1))
    if contract.lifecycle.max_attempts > max_attempts_cap:
        problems.append(
            _problem("lifecycle.max_attempts", f"exceeds the policy cap of {max_attempts_cap}")
        )
    timeout = limits.get("timeout_seconds", {})
    t_min, t_max = int(timeout.get("min", 1)), int(timeout.get("max", 10**9))
    if not t_min <= contract.execution_request.timeout_seconds <= t_max:
        problems.append(
            _problem(
                "execution_request.timeout_seconds",
                f"outside the policy bounds {t_min}..{t_max}",
            )
        )
    command_timeout = contract.execution_request.command_timeout_ms
    if command_timeout is not None:
        bounds = policy_bounds(doc)
        if not bounds["min"] <= command_timeout <= bounds["max"]:
            problems.append(
                _problem(
                    "execution_request.command_timeout_ms",
                    f"outside the policy bounds {bounds['min']}..{bounds['max']}",
                )
            )
    eligible = set(doc.get("retry", {}).get("eligible_classes", []))
    for cls in contract.lifecycle.retry_on:
        if cls.value not in eligible:
            problems.append(
                _problem(
                    "lifecycle.retry_on", f"{cls.value} is not in the policy's eligible classes"
                )
            )
    git = doc.get("git", {})
    branch_pattern = str(git.get("work_branch_pattern", "*"))
    if not fnmatch.fnmatchcase(contract.repository.work_branch, branch_pattern):
        problems.append(
            _problem("repository.work_branch", f"does not match policy pattern {branch_pattern!r}")
        )
    for protected in git.get("protected_branches", []):
        if fnmatch.fnmatchcase(contract.repository.work_branch, str(protected)):
            problems.append(_problem("repository.work_branch", "names a protected branch"))
            break
    required_checks = [str(c) for c in doc.get("repository", {}).get("required_checks", [])]
    present = set(contract.verification_commands)
    for check in required_checks:
        if check not in present:
            problems.append(
                _problem("required_verification", f"missing the policy-required check {check!r}")
            )
    provider = contract.execution_request.provider.value
    supported = REGISTERED_PROVIDERS.get(provider)
    if supported is None:
        problems.append(_problem("execution_request.provider", f"{provider!r} is not registered"))
    pinned = contract.execution_request.pinned_harness
    supplied_image = contract.execution_request.image
    allowlist = [str(pattern) for pattern in doc.get("images", {}).get("allowlist", [])]
    if supplied_image is not None:
        if provider != "fake":
            problems.append(
                _problem(
                    "execution_request.image",
                    "image is derived from the selected harness and must not be supplied",
                )
            )
        elif allowlist and not any(
            fnmatch.fnmatchcase(supplied_image, pattern) for pattern in allowlist
        ):
            problems.append(
                _problem("execution_request.image", "does not match the policy image allowlist")
            )
    if pinned is not None:
        harness = pinned.value
        if harness not in REGISTERED_HARNESSES:
            problems.append(_problem("execution_request.harness", "is not registered"))
        elif supported is not None and harness not in supported:
            problems.append(
                _problem("execution_request.provider", f"{provider!r} does not support {harness!r}")
            )
    if repository is not None:
        issue_prefix = repository.url.rstrip("/").removesuffix(".git") + "/issues/"
        for index, deliverable in enumerate(contract.deliverables):
            for j, ref in enumerate(deliverable.closes):
                if not ref.startswith(issue_prefix):
                    problems.append(
                        _problem(
                            f"deliverables[{index}].closes[{j}]",
                            "is not an issue in the contract's repository",
                        )
                    )
    if contract.lifecycle.retry_on and ExitClass.COMPLETED in contract.lifecycle.retry_on:
        problems.append(_problem("lifecycle.retry_on", "completed is never retried"))
    return problems, policy


def validate_against_registry(
    uow: UnitOfWork,
    clock: Clock,
    contract: TaskContractV1,
    *,
    eligible_harnesses: set[str] | None = None,
    harnesses: HarnessRegistry | None = None,
) -> list[Problem]:
    """Every submit-time rule of 05 that needs the registry: the repository, the policy
    and its caps, the image allowlist, the provider and harness, the routing entry, and
    the quota. A later contract version has to satisfy the same rules the first one did."""
    repository = uow.repositories.get_by_name(contract.repository.name)
    policy = uow.policies.get(contract.policy.name, contract.policy.version)
    problems, checked_policy = _check_against_registry(contract, repository, policy)
    if checked_policy is not None:
        problems.extend(
            _check_routing(uow, clock, contract, checked_policy, eligible_harnesses, harnesses)
        )
    return problems


def eligible_harness_names(
    uow: UnitOfWork,
    contract: TaskContractV1,
    *,
    harnesses: HarnessRegistry | None,
    harness_gates: Mapping[str, HarnessGate] | None,
    credential_sources: Mapping[str, CredentialSource] | None,
    secret_providers: Collection[str] = (),
) -> set[str] | None:
    if harnesses is None:
        return None
    eligible: set[str] = set()
    provider = contract.execution_request.provider.value
    # A provider that keeps the credentials as Secrets it owns (ADR 0015) has no
    # directory to check; its seeding refuses a missing Secret with the reason.
    needs_credential = provider != "fake" and provider not in secret_providers
    for name in harnesses.names():
        adapter = harnesses.get(name)
        if adapter is None:
            continue
        # Local Codex uses the optional read-only gateway key, not auth.json.
        # Keep its eligibility separate so subscription routes still need their source.
        if name == "codex":
            try:
                harnesses.resolve(name, gates=harness_gates, state=uow.harnesses.get(name))
            except HarnessUnavailableError:
                pass
            else:
                eligible.add("codex:local")
        source = (credential_sources or {}).get(name)
        if (
            needs_credential
            and adapter.credential_spec() is not None
            and (source is None or not Path(source.path).is_dir())
        ):
            continue
        try:
            harnesses.resolve(name, gates=harness_gates, state=uow.harnesses.get(name))
        except HarnessUnavailableError:
            continue
        eligible.add(name)
    return eligible


def unwired_provider_problems(
    contract: TaskContractV1, wired_providers: Collection[str] | None
) -> list[Problem]:
    """A provider this deployment does not run, the fake one above all when test fixtures
    are off (crucible#124), is refused on every contract version, submitted, amended or
    corrected, rather than left to fail at launch."""
    provider = contract.execution_request.provider.value
    if wired_providers is None or provider in wired_providers:
        return []
    return [
        _problem(
            "execution_request.provider",
            f"{provider!r} is not a provider this deployment runs",
        )
    ]


def submit_task(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    body: object,
    harnesses: HarnessRegistry | None = None,
    harness_gates: Mapping[str, HarnessGate] | None = None,
    credential_sources: Mapping[str, CredentialSource] | None = None,
    secret_providers: Collection[str] = (),
    wired_providers: Collection[str] | None = None,
    proposed: bool = False,
) -> tuple[Task, TaskContract]:
    """`proposed` (hades #424) stores the same validated contract in `proposed` rather
    than `submitted`: nobody has authorized it, so nothing can start it until an operator
    approves it."""
    contract = parse_contract(body)
    require_operator_for_pin(principal, contract)
    repository = uow.repositories.get_by_name(contract.repository.name)
    eligible_harnesses = eligible_harness_names(
        uow,
        contract,
        harnesses=harnesses,
        harness_gates=harness_gates,
        credential_sources=credential_sources,
        secret_providers=secret_providers,
    )
    problems = validate_against_registry(
        uow, clock, contract, eligible_harnesses=eligible_harnesses, harnesses=harnesses
    )
    if harnesses is not None and contract.execution_request.pinned_harness is not None:
        # 25: a disabled harness is a contract problem now, not a refusal a task later.
        name = contract.execution_request.pinned_harness.value
        try:
            harnesses.resolve(name, gates=harness_gates, state=uow.harnesses.get(name))
        except HarnessUnavailableError as exc:
            problems.append(_problem("execution_request.harness", exc.reason))
    problems.extend(unwired_provider_problems(contract, wired_providers))
    if contract.correction is not None:
        problems.append(
            _problem("correction", "must be null on submit; corrections use /corrections")
        )
    if problems:
        # The request transaction rolls back; the API records this event on its own.
        rejection = Event(
            seq=None,
            ts=clock.now(),
            kind=EventKind.CONTRACT_REJECTED.value,
            principal=principal.name,
            verified=True,
            payload={"external_id": contract.external_id, "problems": problems},
        )
        raise ContractValidationError(
            "task contract failed validation", errors=problems, event=rejection
        )
    assert repository is not None
    if uow.tasks.get_by_external_id(principal.id, contract.external_id) is not None:
        raise DuplicateExternalIdError(
            f"external_id {contract.external_id!r} already exists for principal {principal.name}"
        )
    now = clock.now()
    document = to_document(contract)
    task = Task(
        id=new_id(),
        external_id=contract.external_id,
        principal_id=principal.id,
        project=contract.project,
        title=contract.title,
        state=TaskState.PROPOSED if proposed else TaskState.SUBMITTED,
        contract_version=1,
        policy_name=contract.policy.name,
        policy_version=contract.policy.version,
        repository_id=repository.id,
        created_at=now,
        updated_at=now,
    )
    stored = TaskContract(
        id=new_id(),
        task_id=task.id,
        version=1,
        document=document,
        sha256=contract_sha256(document),
        submitted_at=now,
    )
    uow.tasks.add(task)
    uow.contracts.add(stored)
    record_event(
        uow,
        clock,
        EventKind.TASK_PROPOSED if proposed else EventKind.TASK_SUBMITTED,
        principal=principal.name,
        task_id=task.id,
        payload={
            "external_id": task.external_id,
            "contract_version": 1,
            "contract_sha256": stored.sha256,
            "repository": repository.name,
            "policy": {"name": contract.policy.name, "version": contract.policy.version},
        },
    )
    return task, stored
