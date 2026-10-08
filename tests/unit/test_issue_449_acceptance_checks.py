"""hades #449: executable acceptance checks the contract carries, judged before any PR.

An acceptance criterion may carry `check: {command, expect_exit}`. Crucible's verifier
re-runs each one from the collected tree beside the required commands, and the
`acceptance_checks` gate reads those exits: blocking when the attempt ran on a lab-local
pool, advisory elsewhere. The worker sees each check under its criterion in IDENTITY.md.
No review attempt is launched to judge them.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from crucible.adapters.execution import scripts
from crucible.adapters.execution.collected import read_verifications
from crucible.adapters.execution.fake import verification_runs
from crucible.adapters.execution.identity import render_identity_md
from crucible.application import gates as app_gates
from crucible.application.errors import ContractValidationError
from crucible.application.submit_task import parse_contract
from crucible.contracts.task_contract import TaskContractV1
from crucible.domain.acceptance_checks import criterion_checks, verifier_checks
from crucible.domain.gates import (
    ENFORCED_PRE_PR_GATES,
    PRE_PR_GATES,
    EvidenceItem,
    GateInput,
    GateName,
    GateResult,
    PrePrVerdict,
    acceptance_checks,
    advisory_gates,
    blocking,
    evaluate_pre_pr,
    for_reviewer,
    pre_pr_verdict,
)
from crucible.ports.execution import LaunchSpec
from tests.fixtures import FakeClock, contract_document
from tests.unit.test_issue_360_ready_for_merge_correction import NOW
from tests.unit.test_issue_424_proposed_tasks import ORCHESTRATOR, _api, _store

CHECK = "uv run pytest -q tests/unit/test_import.py -k 'duplicate and not slow'"


def _contract(**check: Any) -> dict[str, Any]:
    doc = contract_document()
    doc["acceptance_criteria"] = [
        {"id": "AC1", "text": "Duplicate import fails with a 409.", "check": check or None},
        {"id": "AC2", "text": "The error page reads well."},
    ]
    if not check:
        doc["acceptance_criteria"][0].pop("check")
    return doc


def _run(check_id: str, exit_code: int, *, ran: bool = True, row: int = 7) -> EvidenceItem:
    """A `verification_run` row as record_collection_evidence writes it."""
    return EvidenceItem(
        id=row,
        kind="verification_run",
        source="crucible",
        verified=True,
        payload={"id": check_id, "command": CHECK, "exit_code": exit_code, "ran": ran},
    )


def _gi(contract: dict[str, Any], evidence: list[EvidenceItem], *, lab_local: bool) -> GateInput:
    return GateInput(
        contract=contract,
        policy={},
        head_sha="a" * 40,
        evidence=tuple(evidence),
        internal_review_required=False,
        lab_local=lab_local,
    )


# ----- AC1: the contract carries a check; submit refuses a malformed one, naming it ----


def test_contract_accepts_a_criterion_check() -> None:
    contract = TaskContractV1.model_validate(_contract(command=CHECK, expect_exit=0))
    check = contract.acceptance_criteria[0].check
    assert check is not None and check.command == CHECK and check.expect_exit == 0
    assert contract.acceptance_criteria[1].check is None


def test_expect_exit_defaults_to_zero() -> None:
    contract = TaskContractV1.model_validate(_contract(command="make test"))
    assert contract.acceptance_criteria[0].check is not None
    assert contract.acceptance_criteria[0].check.expect_exit == 0


@pytest.mark.parametrize(
    "check",
    [
        {"expect_exit": 0},
        {"command": ""},
        {"command": "   "},
        {"command": "make test", "expect_exit": "zero"},
        {"command": "make test", "unknown": 1},
        {"command": "kubectl get pods"},
    ],
)
def test_a_malformed_check_is_refused_naming_the_criterion(check: dict[str, Any]) -> None:
    doc = _contract()
    doc["acceptance_criteria"][0]["check"] = check
    with pytest.raises(ValidationError) as exc:
        TaskContractV1.model_validate(doc)
    assert "criterion 'AC1' has a malformed check" in str(exc.value)


def test_submit_answers_422_naming_the_criterion() -> None:
    from tests.fixtures import FakeClock  # noqa: PLC0415

    doc = _contract()
    doc["acceptance_criteria"][0]["check"] = {"command": "", "expect_exit": 0}
    with _api(_store(), FakeClock(NOW), ORCHESTRATOR) as client:
        refused = client.post("/v1/tasks", json=doc)
        doc["acceptance_criteria"][0]["check"] = {"command": CHECK, "expect_exit": 0}
        accepted = client.post("/v1/tasks", json=doc)
    assert refused.status_code == 422
    assert "criterion 'AC1' has a malformed check" in refused.text
    assert accepted.status_code == 201, accepted.text


def test_parse_contract_problem_names_the_criterion() -> None:
    from crucible.application.errors import ContractValidationError  # noqa: PLC0415

    doc = _contract()
    doc["acceptance_criteria"][0]["check"] = {"command": "docker compose up"}
    with pytest.raises(ContractValidationError) as exc:
        parse_contract(doc)
    assert any("'AC1'" in str(p["message"]) for p in exc.value.errors)


# ----- the verifier runs the criterion checks on the collected tree ---------------------


def test_the_verifier_runs_each_criterion_check_beside_the_required_commands() -> None:
    contract = _contract(command=CHECK, expect_exit=3)
    checks = verifier_checks(contract)
    required = [
        v["id"] for v in contract["required_verification"] if v.get("kind", "command") == "command"
    ]
    assert [c["id"] for c in checks] == [*required, "acceptance:AC1"]
    assert checks[-1] == {
        "id": "acceptance:AC1",
        "criterion": "AC1",
        "command": CHECK,
        "expect_exit": 3,
    }
    script = scripts.verifier_script([(c["id"], c["command"]) for c in checks])
    assert scripts._quote(CHECK) in script
    assert scripts._quote("acceptance:AC1") in script


def test_the_verifier_exit_is_read_back_for_a_criterion_check(tmp_path: Path) -> None:
    contract = _contract(command=CHECK, expect_exit=0)
    spec = LaunchSpec(
        attempt_id="A1",
        task_id="T1",
        external_id="EX-0001",
        role="implement",
        harness="script-harness",
        model="test",
        image="fake:succeed",
        timeout_seconds=60,
        contract=contract,
    )
    safe = scripts.encode_check_id("acceptance:AC1")
    (tmp_path / f"{safe}.exit").write_text("1\n")
    (tmp_path / f"{safe}.log").write_text("1 failed\n")
    (run,) = read_verifications(tmp_path, spec, [("acceptance:AC1", CHECK)])
    assert (run.id, run.exit_code, run.expect_exit, run.ran) == ("acceptance:AC1", 1, 0, True)


def test_the_fake_verifier_reruns_criterion_checks() -> None:
    runs = verification_runs(_contract(command=CHECK), "succeed")
    assert ("acceptance:AC1", CHECK, 0) in [(r.id, r.command, r.exit_code) for r in runs]


# ----- AC2: lab-local, a failing check blocks with the criterion id and the exit -------


def test_failing_check_blocks_a_lab_local_attempt() -> None:
    contract = _contract(command=CHECK, expect_exit=0)
    outcome = acceptance_checks(_gi(contract, [_run("acceptance:AC1", 1)], lab_local=True))
    assert outcome.result is GateResult.FAIL
    assert outcome.always_blocks
    assert "criterion AC1" in outcome.detail and "exited 1, expected 0" in outcome.detail
    assert CHECK in outcome.detail
    assert outcome.evidence_ids == (7,)


def test_failing_check_stops_publication_even_if_a_policy_lists_it_advisory() -> None:
    contract = _contract(command=CHECK, expect_exit=0)
    gi = _gi(contract, [_run("acceptance:AC1", 2)], lab_local=True)
    outcomes = evaluate_pre_pr([GateName.ACCEPTANCE_CHECKS], gi)
    advisory = advisory_gates({}) | {GateName.ACCEPTANCE_CHECKS}
    assert blocking(outcomes, advisory) == [GateName.ACCEPTANCE_CHECKS]
    assert pre_pr_verdict(outcomes, advisory) is PrePrVerdict.FAILED


def test_a_check_that_did_not_run_blocks_a_lab_local_attempt() -> None:
    contract = _contract(command=CHECK, expect_exit=0)
    missing = acceptance_checks(_gi(contract, [], lab_local=True))
    timed_out = acceptance_checks(
        _gi(
            contract,
            [
                EvidenceItem(
                    id=9,
                    kind="verification_run",
                    source="crucible",
                    verified=True,
                    payload={"id": "acceptance:AC1", "ran": False, "detail": "verifier timed out"},
                )
            ],
            lab_local=True,
        )
    )
    assert missing.result is GateResult.FAIL and "criterion AC1" in missing.detail
    assert timed_out.result is GateResult.FAIL and "verifier timed out" in timed_out.detail


def test_passing_check_passes_a_lab_local_attempt() -> None:
    contract = _contract(command=CHECK, expect_exit=4)
    outcome = acceptance_checks(_gi(contract, [_run("acceptance:AC1", 4)], lab_local=True))
    assert outcome.result is GateResult.PASS
    # AC2 has no check: listed for the reviewer, as before.
    assert outcome.findings == ("criterion AC2 has no executable check; the reviewer judges it",)


def test_a_worker_asserted_run_never_satisfies_the_gate() -> None:
    contract = _contract(command=CHECK)
    worker = EvidenceItem(
        id=3,
        kind="verification_run",
        source="worker",
        verified=True,
        payload={"id": "acceptance:AC1", "exit_code": 0, "ran": True},
    )
    assert acceptance_checks(_gi(contract, [worker], lab_local=True)).result is GateResult.FAIL


# ----- AC3: elsewhere the same gate is advisory ----------------------------------------


def test_failing_check_is_advisory_on_a_frontier_attempt() -> None:
    contract = _contract(command=CHECK, expect_exit=0)
    gi = _gi(contract, [_run("acceptance:AC1", 1)], lab_local=False)
    outcomes = evaluate_pre_pr([GateName.ACCEPTANCE_CHECKS], gi)
    outcome = outcomes[GateName.ACCEPTANCE_CHECKS]
    assert outcome.result is GateResult.PASS and not outcome.always_blocks
    assert blocking(outcomes, advisory_gates({})) == []
    listed = [row["detail"] for row in for_reviewer(outcomes, advisory_gates({}))]
    assert any("criterion AC1" in d and "exited 1, expected 0" in d for d in listed)
    assert pre_pr_verdict(outcomes, advisory_gates({})) is PrePrVerdict.PASSED


def test_no_criterion_check_skips_the_gate() -> None:
    outcome = acceptance_checks(_gi(_contract(), [], lab_local=True))
    assert outcome.result is GateResult.SKIPPED


def test_the_gate_always_runs_and_no_policy_lists_it() -> None:
    assert GateName.ACCEPTANCE_CHECKS in ENFORCED_PRE_PR_GATES
    assert GateName.ACCEPTANCE_CHECKS not in PRE_PR_GATES
    assert GateName.ACCEPTANCE_CHECKS in app_gates.configured_pre_pr_gates(
        {"gates": {"pre_pr": ["exit_clean"]}}
    )


def _attempt(pool: str | None) -> Any:
    return SimpleNamespace(selected_pool=pool, routing_version=4)


def test_lab_local_is_the_attempts_pool_holding_a_local_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int | None] = []

    def routing(_uow: Any, _policy: Any, version: int | None) -> Any:
        seen.append(version)
        return SimpleNamespace(local_pools=lambda: ["lab-local"])

    monkeypatch.setattr(app_gates, "load_attempt_routing", routing)
    assert app_gates.ran_on_lab_local_pool(None, _attempt("lab-local"), {})  # type: ignore[arg-type]
    assert not app_gates.ran_on_lab_local_pool(None, _attempt("openai-sub"), {})  # type: ignore[arg-type]
    assert not app_gates.ran_on_lab_local_pool(None, _attempt(None), {})  # type: ignore[arg-type]
    assert seen == [4, 4]
    monkeypatch.setattr(app_gates, "load_attempt_routing", lambda *_: None)
    assert not app_gates.ran_on_lab_local_pool(None, _attempt("lab-local"), {})  # type: ignore[arg-type]


# ----- AC4: IDENTITY.md shows each check under its criterion, verbatim ------------------


def test_identity_shows_each_check_under_its_criterion_verbatim() -> None:
    contract = _contract(command=CHECK, expect_exit=2)
    text = render_identity_md(
        contract=contract,
        policy={},
        external_id="EX-0001",
        owner="foundry",
        work_branch="crucible/EX-0001",
        network_mode="policy",
    )
    section = text[text.index("## Acceptance criteria") : text.index("## Checks")]
    lines = section.splitlines()
    first = lines.index("- `AC1`: Duplicate import fails with a 409.")
    assert lines[first + 1] == f"  - check: `{CHECK}` (expect exit 2)"
    assert lines[first + 2] == "- `AC2`: The error page reads well."
    assert "before any pull request" in section
    assert criterion_checks(contract)[0]["command"] == CHECK


def test_identity_without_checks_is_unchanged() -> None:
    text = render_identity_md(
        contract=_contract(),
        policy={},
        external_id="EX-0001",
        owner="foundry",
        work_branch="crucible/EX-0001",
        network_mode="policy",
    )
    section = text[text.index("## Acceptance criteria") : text.index("## Checks")]
    assert "check:" not in section and "before any pull request" not in section


# ----- AC5: no review attempt is launched before publication ----------------------------


def test_judging_the_checks_launches_no_review() -> None:
    """The gate is a pure judgement of the verifier's exits: the task never waits for an
    internal review over it, on either lane, and the self-review stays the review."""
    contract = _contract(command=CHECK)
    for lab_local in (True, False):
        gi = _gi(contract, [_run("acceptance:AC1", 1)], lab_local=lab_local)
        outcomes = evaluate_pre_pr(app_gates.configured_pre_pr_gates({}), gi)
        assert outcomes[GateName.INTERNAL_REVIEW_RECORDED].result is GateResult.SKIPPED
        assert pre_pr_verdict(outcomes, advisory_gates({})) is not PrePrVerdict.REVIEW
    assert not app_gates.internal_review_required({}, contract, SimpleNamespace())  # type: ignore[arg-type]


# ----- Findings: acceptance criteria amendments include check -------------------------


def test_amend_rejects_check_added_on_amendment() -> None:
    """Finding 01M4CS2Q69VXH953KB3X8F48MH: an amendment that adds a check to a criterion
    in AWAITING_ACCEPTANCE is refused because the recorded acceptance_checks pass did not
    run the new command."""
    from crucible.application.corrections import amend_task  # noqa: PLC0415
    from crucible.application.submit_task import submit_task  # noqa: PLC0415
    from crucible.domain.lifecycle import TaskState  # noqa: PLC0415

    store = _store()
    clock = FakeClock(NOW)

    # Submit version 1: AC1 has no check.
    v1_doc = _contract()
    v1_doc["acceptance_criteria"] = [
        {"id": "AC1", "text": "Duplicate import fails with a 409."},
        {"id": "AC2", "text": "The error page reads well."},
    ]
    task, _ = submit_task(
        store.uow(),
        clock,
        principal=ORCHESTRATOR,
        body=v1_doc,
        proposed=True,
    )
    task_id = task.id

    # Manually set the task to AWAITING_ACCEPTANCE so amend validation fires.
    stored_task = store.tasks.rows[task_id]
    stored_task.state = TaskState.AWAITING_ACCEPTANCE

    # Amend: AC1 now has a check.
    v2_doc = _contract()
    check_dict = {"command": CHECK, "expect_exit": 0}
    v2_doc["acceptance_criteria"] = [
        {"id": "AC1", "text": "Duplicate import fails with a 409.", "check": check_dict},
        {"id": "AC2", "text": "The error page reads well."},
    ]
    with pytest.raises(ContractValidationError):
        amend_task(
            store.uow(),
            clock,
            principal=ORCHESTRATOR,
            task_id=task_id,
            body=v2_doc,
            reason="fix",
        )


def test_required_verification_reserved_namespace_collisions() -> None:
    """Finding 01M4CS2Q6CD3D8QECEYKRQW503: a required verification id that starts with
    'acceptance:' collides with the generated id for a criterion check."""
    doc = contract_document()
    doc["required_verification"] = [
        {"id": "acceptance:AC1", "command": "make test", "expect_exit": 0},
    ]
    with pytest.raises(ValidationError) as exc:
        TaskContractV1.model_validate(doc)
    assert "acceptance:" in str(exc.value)


def test_required_verification_ok_without_prefix() -> None:
    """A required verification with a normal id coexists with a criterion check."""
    contract = TaskContractV1.model_validate(_contract(command=CHECK, expect_exit=0))
    assert all(not str(v.id).startswith("acceptance:") for v in contract.required_verification)
