"""Corrections and amendments (04, 05, 09).

A correction is a new contract version carrying a `correction` section; it may narrow
scope but never widen it, and it re-enters the supervision half at `scheduled` with a
`correct` execution against the existing branch. An amendment is a new version without a
correction section, allowed only where 04 says."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any

from crucible.application.errors import (
    ContractValidationError,
    NotFoundError,
    TransitionNotAllowedError,
)
from crucible.application.harnesses import HarnessRegistry
from crucible.application.review import latest_work_attempt
from crucible.application.submit_task import (
    eligible_harness_names,
    parse_contract,
    require_operator_for_pin,
    unwired_provider_problems,
    validate_against_registry,
)
from crucible.application.task_access import require_task_principal
from crucible.application.transitions import move_task, record_event, require_contract
from crucible.contracts.common import to_document
from crucible.contracts.task_contract import (
    TaskContractV1,
    contract_sha256,
    correction_narrows,
)
from crucible.domain.entities import (
    AcceptanceVerdict,
    Decision,
    EscalationState,
    Principal,
    Task,
    TaskContract,
)
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.harness import CredentialSource, HarnessGate
from crucible.ports.repository import UnitOfWork

CORRECTABLE_STATES = frozenset(
    {
        TaskState.PRE_PR_GATES_FAILED,
        TaskState.AWAITING_ACCEPTANCE,
        TaskState.EXTERNAL_FEEDBACK_RECEIVED,
        TaskState.CI_CERTIFICATION_FAILED,
        TaskState.BLOCKED,
        TaskState.READY_FOR_MERGE,
    }
)
# hades #379: a task ready for merge has passed its external round and CI. A correction
# there answers Foundry's own judgement of the full diff, a needs_more_work or an
# internal review finding; an external review or CI finding has its own state.
READY_FOR_MERGE_REASONS = frozenset({"needs_more_work", "internal_review"})
AMENDABLE_STATES = frozenset(
    {TaskState.SUBMITTED, TaskState.BLOCKED, TaskState.AWAITING_ACCEPTANCE}
)
PREVIOUS_BUNDLE_GONE = "previous_attempt_bundle_gone"
PREVIOUS_BUNDLE_OTHER_PROVIDER = "previous_attempt_bundle_other_provider"


def _unpublished_bundle_problem(
    uow: UnitOfWork, task: Task, provider: str, *, last_attempt: bool = False
) -> dict[str, str] | None:
    """Name why an unpublished correction cannot resume, or return None when it can."""
    if (
        not last_attempt
        and uow.events.latest_for_task_kind(task.id, EventKind.PUBLISH_COMPLETED.value) is not None
    ):
        return None
    work = latest_work_attempt(uow, task)
    if work is None:
        return {"path": "correction", "message": PREVIOUS_BUNDLE_GONE}
    attempt, execution = work
    exited = uow.events.latest_for_task_kind(task.id, EventKind.ATTEMPT_EXITED.value)
    if (
        exited is not None
        and exited.attempt_id == attempt.id
        and (
            exited.payload.get("never_started") is True or exited.payload.get("no_commits") is True
        )
    ):
        # Hades #346: an attempt that never started (or ended before committing) left
        # no bundle of its own, and it does not discard the work before it. The checks
        # below run against the last attempt that did start and seal a bundle.
        sealed = [
            candidate
            for candidate in uow.attempts.list_for_task(task.id)
            if any(
                row.kind == "bundle_head" and row.verified and row.payload.get("bundle_verified")
                for row in uow.evidence.list_for_attempt(candidate.id)
            )
        ]
        if not sealed:
            # Nothing was ever sealed: with no head the correction starts from the base
            # like the first attempt did; a head with no bundle anywhere cannot resume.
            return (
                {"path": "correction", "message": PREVIOUS_BUNDLE_GONE} if task.head_sha else None
            )
        attempt = max(sealed, key=lambda candidate: candidate.id)
        source_execution = uow.executions.get(attempt.execution_id)
        if source_execution is None:
            return {"path": "correction", "message": PREVIOUS_BUNDLE_GONE}
        execution = source_execution
    if execution.provider != provider:
        return {"path": "execution_request.provider", "message": PREVIOUS_BUNDLE_OTHER_PROVIDER}
    evidence = next(
        (
            row
            for row in reversed(uow.evidence.list_for_attempt(attempt.id))
            if row.kind == "bundle_head"
            and row.verified
            and row.payload.get("bundle_verified")
            and row.payload.get("bundle_sha256")
        ),
        None,
    )
    released = any(
        action.kind == "workspace" and action.subject == attempt.id
        for action in uow.retention.list_recent(10_000)
    )
    path = f"{attempt.workspace_path}/output/work_branch.bundle"
    local_missing = "://" not in path and not Path(path).is_file()
    if evidence is None or released or local_missing:
        return {"path": "correction", "message": PREVIOUS_BUNDLE_GONE}
    return None


def _next_version(uow: UnitOfWork, task: Task) -> int:
    return max((v.version for v in uow.contracts.list_for_task(task.id)), default=0) + 1


def _store_version(
    uow: UnitOfWork, clock: Clock, task: Task, contract: TaskContractV1
) -> TaskContract:
    document = to_document(contract)
    stored = TaskContract(
        id=new_id(),
        task_id=task.id,
        version=_next_version(uow, task),
        document=document,
        sha256=contract_sha256(document),
        submitted_at=clock.now(),
    )
    uow.contracts.add(stored)
    return stored


def _previous(uow: UnitOfWork, task: Task, of_version: int) -> TaskContractV1:
    stored = uow.contracts.get(task.id, of_version)
    if stored is None:
        raise ContractValidationError(
            "correction.of_version names a version this task does not have",
            errors=[{"path": "correction.of_version", "message": f"no version {of_version}"}],
        )
    return TaskContractV1.model_validate(stored.document)


def attach_correction(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    body: object,
    harnesses: HarnessRegistry | None = None,
    harness_gates: dict[str, HarnessGate] | None = None,
    credential_sources: dict[str, CredentialSource] | None = None,
    secret_providers: Collection[str] = (),
    wired_providers: Collection[str] | None = None,
) -> Task:
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    if task.state not in CORRECTABLE_STATES:
        raise TransitionNotAllowedError(
            f"a correction is accepted in {sorted(s.value for s in CORRECTABLE_STATES)}; "
            f"task is {task.state.value}"
        )
    contract = parse_contract(body)
    require_operator_for_pin(principal, contract)
    if contract.correction is None:
        raise ContractValidationError(
            "a correction version carries a correction section",
            errors=[{"path": "correction", "message": "must not be null on a correction"}],
        )
    if contract.correction.of_version != task.contract_version:
        raise ContractValidationError(
            "a correction corrects the version the task is on",
            errors=[
                {
                    "path": "correction.of_version",
                    "message": (
                        f"the task is on version {task.contract_version}; "
                        f"the correction names {contract.correction.of_version}"
                    ),
                }
            ],
        )
    previous = _previous(uow, task, contract.correction.of_version)
    # 3: a correction is a contract version, so it satisfies every submit-time rule.
    eligible = eligible_harness_names(
        uow,
        contract,
        harnesses=harnesses,
        harness_gates=harness_gates,
        credential_sources=credential_sources,
        secret_providers=secret_providers,
    )
    problems: list[dict[str, Any]] = validate_against_registry(
        uow, clock, contract, eligible_harnesses=eligible, harnesses=harnesses
    )
    problems.extend(unwired_provider_problems(contract, wired_providers))
    problems.extend(correction_narrows(previous, contract))
    bundle_problem = _unpublished_bundle_problem(
        uow,
        task,
        contract.execution_request.provider,
        last_attempt=contract.correction.resume_from == "last_attempt",
    )
    if bundle_problem is not None:
        problems.append(bundle_problem)
    if (
        task.state is TaskState.READY_FOR_MERGE
        and contract.correction.reason not in READY_FOR_MERGE_REASONS
    ):
        problems.append(
            {
                "path": "correction.reason",
                "message": (
                    "a correction from ready_for_merge gives reason "
                    f"{' or '.join(sorted(READY_FOR_MERGE_REASONS))}; "
                    f"this one gives {contract.correction.reason}"
                ),
            }
        )
    if task.state is TaskState.AWAITING_ACCEPTANCE:
        # The current verdict only. A needs_more_work that a later accept superseded is
        # not a standing request for more work.
        current = [a for a in uow.acceptance.list_for_task(task.id) if a.superseded_at is None]
        verdict = current[-1].verdict if current else None
        if verdict is not AcceptanceVerdict.NEEDS_MORE_WORK:
            problems.append(
                {
                    "path": "$",
                    "message": (
                        "a correction from awaiting_acceptance follows a needs_more_work "
                        f"AcceptanceResult; the current verdict is "
                        f"{verdict.value if verdict else 'none'}"
                    ),
                }
            )
    if problems:
        raise ContractValidationError("correction failed validation", errors=problems)
    stored = _store_version(uow, clock, task, contract)
    task.contract_version = stored.version
    task.head_sha = None
    uow.tasks.save(task)
    record_event(
        uow,
        clock,
        EventKind.TASK_CORRECTION_ATTACHED,
        principal=principal.name,
        task_id=task.id,
        payload={
            "contract_version": stored.version,
            "contract_sha256": stored.sha256,
            "of_version": contract.correction.of_version,
            "reason": contract.correction.reason,
            "addresses": [a.model_dump(mode="json") for a in contract.correction.addresses],
            "request_internal_review": contract.correction.request_internal_review,
        },
    )
    _from_state = task.state
    move_task(
        uow,
        clock,
        task,
        TaskState.SCHEDULED,
        EventKind.TASK_SCHEDULED,
        principal=principal.name,
        payload={
            "role": "correct",
            "contract_version": stored.version,
            "tier": contract.execution_request.tier.value,
            "pin": (
                {
                    "harness": contract.execution_request.pinned_harness.value,
                    "model": contract.execution_request.pinned_model,
                }
                if contract.execution_request.pinned_harness
                else None
            ),
            "provider": contract.execution_request.provider.value,
            "policy": {"name": contract.policy.name, "version": contract.policy.version},
        },
    )
    if _from_state is TaskState.BLOCKED:
        for escalation in uow.escalations.list_for_task(task.id):
            if escalation.state is EscalationState.OPEN:
                decision = Decision(
                    id=new_id(),
                    task_id=task.id,
                    escalation_id=escalation.id,
                    principal_id=principal.id,
                    kind="correction",
                    verbatim=contract.correction.instructions,
                    resolves=escalation.question,
                    created_at=clock.now(),
                )
                uow.decisions.add(decision)
                record_event(
                    uow,
                    clock,
                    EventKind.DECISION_RECORDED,
                    principal=principal.name,
                    task_id=task.id,
                    payload={
                        "decision_id": decision.id,
                        "kind": "correction",
                        "escalation_id": decision.escalation_id,
                        "resolves": decision.resolves,
                        "verbatim": decision.verbatim,
                    },
                )
                for target in (EscalationState.ANSWERED, EscalationState.CLOSED):
                    escalation.state = target
                    if target is EscalationState.CLOSED:
                        escalation.closed_at = clock.now()
                    escalation.decision_id = decision.id
                    uow.escalations.save(escalation)
                    record_event(
                        uow,
                        clock,
                        EventKind.ESCALATION_ANSWERED
                        if target is EscalationState.ANSWERED
                        else EventKind.ESCALATION_CLOSED,
                        principal=principal.name,
                        task_id=task.id,
                        payload={
                            "escalation_id": escalation.id,
                            "decision_id": decision.id,
                        },
                    )
                break

    return task


def amend_task(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    body: object,
    reason: str = "amendment",
    harnesses: HarnessRegistry | None = None,
    harness_gates: dict[str, HarnessGate] | None = None,
    credential_sources: dict[str, CredentialSource] | None = None,
    secret_providers: Collection[str] = (),
    wired_providers: Collection[str] | None = None,
) -> Task:
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    require_task_principal(principal, task)
    if task.state not in AMENDABLE_STATES:
        raise TransitionNotAllowedError(
            f"an amendment is accepted in {sorted(s.value for s in AMENDABLE_STATES)}; "
            f"task is {task.state.value}"
        )
    contract = parse_contract(body)
    require_operator_for_pin(principal, contract)
    if contract.correction is not None:
        raise ContractValidationError(
            "an amendment carries no correction section; use /corrections",
            errors=[{"path": "correction", "message": "must be null on an amendment"}],
        )
    require_contract(uow, task)
    previous = _previous(uow, task, task.contract_version)
    eligible = eligible_harness_names(
        uow,
        contract,
        harnesses=harnesses,
        harness_gates=harness_gates,
        credential_sources=credential_sources,
        secret_providers=secret_providers,
    )
    problems: list[dict[str, Any]] = validate_against_registry(
        uow, clock, contract, eligible_harnesses=eligible, harnesses=harnesses
    )
    problems.extend(unwired_provider_problems(contract, wired_providers))
    if contract.external_identity_fields() != previous.external_identity_fields():
        problems.append(
            {
                "path": "external_id",
                "message": "external_id, repository, and policy are not amendable",
            }
        )
    if task.state is TaskState.AWAITING_ACCEPTANCE:
        # The gate results and the AcceptanceResult name a head that ran under the
        # previous version. An amendment here may narrow, never widen, and may not change
        # what the deliverable is, or a `pull_request` task could be walked to `accepted`
        # as an `artifacts` one without ever being published (09).
        problems.extend(correction_narrows(previous, contract))
        if [d.kind for d in contract.deliverables] != [d.kind for d in previous.deliverables]:
            problems.append(
                {
                    "path": "deliverables",
                    "message": (
                        "the deliverable kind is not amendable once the gates have passed "
                        "on a collected head"
                    ),
                }
            )
        # The gate results are the proof, and they were evaluated against this version's
        # acceptance criteria and required verification. Changing either would leave a
        # `pass` standing for a question that was never asked. A correction re-runs the
        # work and re-evaluates the gates, so that is the path for a proof-affecting
        # change; an amendment here is refused naming the field (09, 11).
        for field, before, after in (
            (
                "acceptance_criteria",
                [(c.id, c.text) for c in previous.acceptance_criteria],
                [(c.id, c.text) for c in contract.acceptance_criteria],
            ),
            (
                "required_verification",
                [v.model_dump(mode="json") for v in previous.required_verification],
                [v.model_dump(mode="json") for v in contract.required_verification],
            ),
        ):
            if before != after:
                problems.append(
                    {
                        "path": field,
                        "message": (
                            f"{field} is the question the recorded gate results answered "
                            "for this head; change it through a correction, which re-runs "
                            "the work and re-evaluates the gates"
                        ),
                    }
                )
    if problems:
        raise ContractValidationError("amendment failed validation", errors=problems)
    stored = _store_version(uow, clock, task, contract)
    task.contract_version = stored.version
    task.updated_at = clock.now()
    uow.tasks.save(task)
    record_event(
        uow,
        clock,
        EventKind.TASK_AMENDED,
        principal=principal.name,
        task_id=task.id,
        payload={
            "contract_version": stored.version,
            "contract_sha256": stored.sha256,
            "reason": reason,
        },
    )
    return task
