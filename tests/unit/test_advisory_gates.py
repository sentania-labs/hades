"""ADR 0024: each pre-PR gate is blocking or advisory. A blocking failure stops the task;
an advisory one is recorded and carried in front of the reviewer, and a worker's report
that contradicts Crucible's own re-run is named plainly."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from crucible.application.evidence import _claimed_checks, _scanner_findings
from crucible.application.queries import reviewer_items
from crucible.contracts.completion_claim import load_report
from crucible.contracts.evidence import EvidenceKind
from crucible.contracts.policy import parse_policy
from crucible.domain.entities import GateResultRecord
from crucible.domain.gates import (
    ALWAYS_BLOCKING_GATES,
    DEFAULT_ADVISORY_GATES,
    NO_REPORT_DETAIL,
    PRE_PR_GATES,
    EvidenceItem,
    GateClass,
    GateName,
    GateResult,
    PrePrVerdict,
    advisory_gates,
    blocking,
    evaluate_pre_pr,
    for_reviewer,
    gate_class,
    pre_pr_verdict,
)
from crucible.ports.execution import CollectedOutputs
from tests.fixtures import contract_document
from tests.unit.test_gates import _claim_payload, _ev, _gi, _passing_evidence
from tests.unit.test_policy_schema import seeded_policy_v3

DEFAULT = advisory_gates({})


def _replace(evidence: list[EvidenceItem], index: int, item: EvidenceItem) -> list[EvidenceItem]:
    out = list(evidence)
    out[index] = item
    return out


def _without_review(evidence: list[EvidenceItem]) -> list[EvidenceItem]:
    return [e for e in evidence if e.kind != "review_received"]


def _out_of_scope() -> list[EvidenceItem]:
    evidence = _replace(
        _passing_evidence(),
        3,
        _ev("diff_paths", {"paths": ["src/ledger/a.py", "infrastructure/out.txt"]}, ident=4),
    )
    bundle = dict(evidence[2].payload)
    bundle["commit_paths"] = ["src/ledger/a.py", "infrastructure/out.txt"]
    evidence[2] = _ev("bundle_head", bundle, ident=3)
    return evidence


# ----- classification ------------------------------------------------------------


def test_the_default_classification_is_the_operators() -> None:
    """ADR 0024, amended by hades #498: the report gate is advisory too."""
    assert {
        GateName.SCOPE_CONTAINED,
        GateName.CRITERIA_MAPPED,
        GateName.RUN_EVIDENCE_PRESENT,
        GateName.COMMIT_POLICY,
        GateName.REPORT_PRESENT,
    } == DEFAULT
    for gate in (
        GateName.VERIFICATION_RAN,
        GateName.NO_SECRETS,
        GateName.CI_UNCHANGED,
        GateName.DEPENDENCIES_UNCHANGED,
        GateName.COMMITS_PRESENT,
        GateName.NO_INJECTED_FILES,
        GateName.WORKSPACE_CLEAN,
        GateName.EXIT_CLEAN,
        GateName.INTERNAL_REVIEW_RECORDED,
    ):
        assert gate_class(gate, DEFAULT) is GateClass.BLOCKING, gate


def test_a_policy_without_the_field_takes_the_default() -> None:
    """The lab's default-software v8 and hades-self-hosting v1 carry no list."""
    seeded = seeded_policy_v3()
    assert "advisory" not in seeded["gates"]
    always = {GateName.COMMIT_POLICY, GateName.REPORT_PRESENT}
    assert advisory_gates(seeded) == DEFAULT_ADVISORY_GATES | always
    assert advisory_gates({"gates": {"advisory": None}}) == DEFAULT_ADVISORY_GATES | always


def test_a_policy_list_decides_and_the_secret_gate_always_blocks() -> None:
    """hades #498: the report gate is always advisory, as commit_policy is; no_secrets
    always blocks."""
    always = {GateName.COMMIT_POLICY, GateName.REPORT_PRESENT}
    assert advisory_gates({"gates": {"advisory": []}}) == always
    chosen = advisory_gates(
        {"gates": {"advisory": ["ci_unchanged", "no_secrets", "report_present"]}}
    )
    assert chosen == {GateName.CI_UNCHANGED} | always


# ----- the verdict ----------------------------------------------------------------


def test_an_advisory_failure_leaves_the_task_moving() -> None:
    gi = _gi(_without_review(_out_of_scope()))
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), gi)
    assert outcomes[GateName.SCOPE_CONTAINED].result is GateResult.FAIL
    assert blocking(outcomes, DEFAULT) == []
    assert pre_pr_verdict(outcomes, DEFAULT) is PrePrVerdict.PASSED
    items = for_reviewer(outcomes, DEFAULT)
    assert items == [
        {
            "gate": GateName.SCOPE_CONTAINED,
            "detail": "outside allowed_paths: ['infrastructure/out.txt']",
        }
    ]


def test_an_advisory_failure_with_the_review_recorded_passes_the_gates() -> None:
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(_out_of_scope()))
    assert pre_pr_verdict(outcomes, DEFAULT) is PrePrVerdict.PASSED
    assert [i["gate"] for i in for_reviewer(outcomes, DEFAULT)] == [GateName.SCOPE_CONTAINED]


def test_the_same_failure_blocks_when_the_policy_makes_it_blocking() -> None:
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(_without_review(_out_of_scope())))
    assert blocking(outcomes, frozenset()) == [GateName.SCOPE_CONTAINED]
    assert pre_pr_verdict(outcomes, frozenset()) is PrePrVerdict.FAILED
    assert for_reviewer(outcomes, frozenset()) == []


def test_a_blocking_failure_stops_the_task() -> None:
    evidence = [
        e
        for e in _passing_evidence()
        if not (e.kind == "verification_run" and e.payload.get("id") == "V2")
    ]
    evidence.append(_ev("verification_run", {"id": "V2", "exit_code": 2, "ran": True}, ident=21))
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(_without_review(evidence)))
    assert blocking(outcomes, DEFAULT) == [GateName.VERIFICATION_RAN]
    assert pre_pr_verdict(outcomes, DEFAULT) is PrePrVerdict.FAILED


def test_a_prohibited_path_still_blocks_when_scope_is_advisory() -> None:
    contract = contract_document()
    contract["scope"]["prohibited_paths"] = ["infrastructure/**"]
    outcomes = evaluate_pre_pr(
        sorted(PRE_PR_GATES), _gi(_without_review(_out_of_scope()), contract=contract)
    )
    scope = outcomes[GateName.SCOPE_CONTAINED]
    assert scope.result is GateResult.FAIL and scope.always_blocks
    assert "prohibited_paths" in scope.detail
    assert blocking(outcomes, DEFAULT) == [GateName.SCOPE_CONTAINED]
    assert pre_pr_verdict(outcomes, DEFAULT) is PrePrVerdict.FAILED
    assert for_reviewer(outcomes, DEFAULT) == []


def test_a_path_merely_outside_allowed_paths_does_not_always_block() -> None:
    outcomes = evaluate_pre_pr([GateName.SCOPE_CONTAINED], _gi(_out_of_scope()))
    assert not outcomes[GateName.SCOPE_CONTAINED].always_blocks


def test_a_missing_judgement_field_is_for_the_reviewer() -> None:
    """report_present (hades #498): a report without `risks` did not parse; the gap is
    listed for the reviewer and the task goes on."""
    claim = _claim_payload(
        parsed_ok=False,
        parse_errors=[{"loc": ["risks"], "msg": "Field required", "type": "missing"}],
    )
    evidence = _without_review(_replace(_passing_evidence(), 1, _ev("artifact_present", claim)))
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(evidence))
    assert outcomes[GateName.REPORT_PRESENT].result is GateResult.FAIL
    assert not outcomes[GateName.REPORT_PRESENT].always_blocks
    assert outcomes[GateName.CRITERIA_MAPPED].result is GateResult.FAIL
    assert blocking(outcomes, DEFAULT) == []
    assert pre_pr_verdict(outcomes, DEFAULT) is PrePrVerdict.PASSED
    assert {i["gate"] for i in for_reviewer(outcomes, DEFAULT)} == {
        GateName.CRITERIA_MAPPED,
        GateName.REPORT_PRESENT,
    }


def test_a_report_that_is_not_yaml_goes_to_the_reviewer_with_its_parse_error() -> None:
    """The correction to FDY-0138: a report file that is there but is not YAML is recorded
    as present and unparsed (only a message and position, no fact fields), and goes to
    the reviewer with the parser's problem."""
    _, errors = load_report("summary: c5: live run\n")
    claim = {"role": "completion_claim", "parsed_ok": False, "parse_errors": errors}
    evidence = _without_review(_replace(_passing_evidence(), 1, _ev("artifact_present", claim)))
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(evidence))
    report = outcomes[GateName.REPORT_PRESENT]
    assert report.result is GateResult.FAIL and not report.always_blocks
    assert "report.yaml is not YAML: mapping values are not allowed here" in report.detail
    assert "at line 1, column 12" in report.detail
    assert pre_pr_verdict(outcomes, DEFAULT) is PrePrVerdict.PASSED
    assert {"gate": GateName.REPORT_PRESENT, "detail": report.detail} in for_reviewer(
        outcomes, DEFAULT
    )


def test_the_parse_problems_shown_are_few_redacted_and_short() -> None:
    token = "ghp_" + "a" * 36
    errors = [{"loc": ["x", 0], "msg": f"bad {token}", "type": "t"}] + [
        {"loc": [], "msg": "m" * 500, "type": "t"}
    ] * 4
    claim = {"role": "completion_claim", "parsed_ok": False, "parse_errors": errors}
    evidence = _replace(_passing_evidence(), 1, _ev("artifact_present", claim))
    detail = evaluate_pre_pr([GateName.REPORT_PRESENT], _gi(evidence))[
        GateName.REPORT_PRESENT
    ].detail
    assert token not in detail and "x.0: bad [redacted:" in detail
    assert "m" * 201 not in detail and "; and 2 more" in detail


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("- a\n- b\n", "report is not a mapping"),
        ("just text\n", "report is not a mapping"),
        ("a: [1, 2\n", "report.yaml is not YAML: expected ',' or ']', but got '<stream end>'"),
    ],
)
def test_load_report_says_why_without_the_reports_text(raw: str, message: str) -> None:
    report, errors = load_report(raw)
    assert report is None and errors[0]["msg"].startswith(message)
    assert load_report("summary: ok\n") == ({"summary": "ok"}, [])


def test_a_secret_in_a_report_that_is_not_yaml_is_still_found() -> None:
    """Its parse error now reaches the reviewer, so its text is scanned like any report."""
    raw = "summary: key: ghp_" + "a" * 36 + "\n"
    outputs = CollectedOutputs(report=None, report_raw=raw, blocked_md=None)
    assert [f["where"] for f in _scanner_findings(outputs, None)] == ["report"]


def test_no_report_at_all_is_for_the_reviewer() -> None:
    """hades #498: the gates judge the work. With the work gates passing, a missing
    report is listed for the reviewer in plain words and never stops the task."""
    evidence = _without_review(
        [e for e in _passing_evidence() if e.payload.get("role") != "completion_claim"]
    )
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(evidence))
    report = outcomes[GateName.REPORT_PRESENT]
    assert report.result is GateResult.FAIL and not report.always_blocks
    assert report.detail == NO_REPORT_DETAIL
    assert blocking(outcomes, DEFAULT) == []
    assert pre_pr_verdict(outcomes, DEFAULT) is PrePrVerdict.PASSED
    assert {"gate": GateName.REPORT_PRESENT, "detail": NO_REPORT_DETAIL} in for_reviewer(
        outcomes, DEFAULT
    )


def test_an_advisory_gate_that_errors_goes_to_the_reviewer() -> None:
    evidence = _passing_evidence()
    bundle = dict(evidence[2].payload)
    bundle["commit_paths"] = None
    evidence[2] = _ev("bundle_head", bundle, ident=3)  # a payload the evaluator cannot iterate
    outcomes = evaluate_pre_pr([GateName.SCOPE_CONTAINED], _gi(evidence))
    assert outcomes[GateName.SCOPE_CONTAINED].result is GateResult.ERROR
    assert blocking(outcomes, DEFAULT) == []
    assert for_reviewer(outcomes, DEFAULT)[0]["gate"] == GateName.SCOPE_CONTAINED


# ----- a worker's claim against Crucible's re-run ----------------------------------


def _v3_failed_by_crucible(claimed_exit: int) -> list[EvidenceItem]:
    claim = _claim_payload(
        claimed_checks=[{"id": "V1", "exit": 0}, {"id": "V3", "exit": claimed_exit}]
    )
    evidence = [
        e
        for e in _passing_evidence()
        if not (e.kind == "verification_run" and e.payload.get("id") == "V3")
    ]
    evidence[1] = _ev("artifact_present", claim, ident=2)
    evidence.append(_ev("verification_run", {"id": "V3", "exit_code": 1, "ran": True}, ident=22))
    return evidence


def test_a_claim_that_contradicts_the_rerun_is_a_named_finding() -> None:
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(_v3_failed_by_crucible(0)))
    verification = outcomes[GateName.VERIFICATION_RAN]
    assert verification.result is GateResult.FAIL
    assert verification.findings == ("the worker reported V3 passing; Crucible's re-run failed it",)
    # The check itself failing is still blocking.
    assert blocking(outcomes, DEFAULT) == [GateName.VERIFICATION_RAN]
    assert for_reviewer(outcomes, DEFAULT) == [
        {
            "gate": GateName.VERIFICATION_RAN,
            "detail": "the worker reported V3 passing; Crucible's re-run failed it",
        }
    ]


def test_a_claim_that_agrees_with_a_failed_rerun_is_no_finding() -> None:
    outcomes = evaluate_pre_pr([GateName.VERIFICATION_RAN], _gi(_v3_failed_by_crucible(1)))
    assert outcomes[GateName.VERIFICATION_RAN].findings == ()


def test_only_contract_check_ids_are_echoed() -> None:
    claim = _claim_payload(claimed_checks=[{"id": "free text the worker wrote", "exit": 0}])
    evidence = _replace(_passing_evidence(), 1, _ev("artifact_present", claim, ident=2))
    outcomes = evaluate_pre_pr([GateName.VERIFICATION_RAN], _gi(evidence))
    assert outcomes[GateName.VERIFICATION_RAN].findings == ()


def test_claimed_checks_keep_only_an_id_and_an_integer_exit() -> None:
    claim: dict[str, Any] = {
        "checks": [
            {"id": "V1", "command": "make lint", "exit": 0, "log": "x"},
            {"id": "V2", "exit": True},
            {"id": "V3", "exit": "0"},
            "not a check",
        ]
    }
    assert _claimed_checks(claim) == [{"id": "V1", "exit": 0}]
    assert _claimed_checks({}) == []


# ----- what the task view reads back ---------------------------------------------


def _row(gate: str, result: str, *, blocking: bool, findings: list[str] | None = None) -> Any:
    return GateResultRecord(
        id=gate,
        task_id="t",
        attempt_id="a",
        head_sha="h",
        gate=gate,
        phase="pre_pr",
        result=result,
        detail=f"{gate} detail",
        evidence_ids=[],
        evaluated_at=None,  # type: ignore[arg-type]
        blocking=blocking,
        findings=findings or [],
    )


def test_reviewer_items_list_failed_advisory_gates_then_findings() -> None:
    rows = [
        _row("verification_ran", "fail", blocking=True, findings=["the worker reported ..."]),
        _row("scope_contained", "fail", blocking=False),
        _row("report_present", "pass", blocking=False),
        _row("no_secrets", "fail", blocking=True),
    ]
    assert reviewer_items(rows) == [
        {"gate": "scope_contained", "detail": "scope_contained detail"},
        {"gate": "verification_ran", "detail": "the worker reported ..."},
    ]


# ----- the policy field --------------------------------------------------------------


def _with_advisory(value: Any) -> dict[str, Any]:
    document = copy.deepcopy(seeded_policy_v3())
    document["gates"]["advisory"] = value
    return document


def test_the_policy_field_is_optional_and_validated() -> None:
    assert parse_policy(seeded_policy_v3()).gates.advisory is None
    assert parse_policy(_with_advisory(["scope_contained"])).gates.advisory == ["scope_contained"]
    for bad, words in (
        (["ci_green_for_head"], "not pre-PR gates"),
        (["no_such_gate"], "not pre-PR gates"),
        (["report_present"], "always advisory"),
        (["no_secrets"], "always block"),
        (["commit_policy"], "always advisory"),
        (["scope_contained", "scope_contained"], "duplicate"),
    ):
        with pytest.raises(ValidationError, match=words):
            parse_policy(_with_advisory(bad))


def test_making_a_safety_gate_advisory_is_an_operator_setting() -> None:
    assert (
        parse_policy(_with_advisory(sorted(DEFAULT_ADVISORY_GATES))).operator_only_settings() == []
    )
    policy = parse_policy(_with_advisory(["verification_ran", "scope_contained"]))
    assert policy.operator_only_settings() == ["gates.advisory.verification_ran"]


def test_no_gate_is_both_always_blocking_and_advisory_by_default() -> None:
    assert not ALWAYS_BLOCKING_GATES & DEFAULT_ADVISORY_GATES
    assert DEFAULT_ADVISORY_GATES <= PRE_PR_GATES


def test_the_commit_policy_gate_is_always_advisory() -> None:
    """FDY-0143 (operator, 2026-09-29): the commit author is for the reviewer and the
    trailer is not checked, so no advisory set, not even an empty one, makes it block."""
    assert GateName.COMMIT_POLICY not in ALWAYS_BLOCKING_GATES
    stored: dict[str, Any]
    for stored in ({}, {"gates": {"advisory": []}}, {"gates": {"advisory": ["ci_unchanged"]}}):
        assert gate_class(GateName.COMMIT_POLICY, advisory_gates(stored)) is GateClass.ADVISORY


def test_a_commit_by_another_author_is_for_the_reviewer_and_never_stops_the_task() -> None:
    evidence = _without_review(_passing_evidence())
    bundle = next(i for i, e in enumerate(evidence) if e.kind == "bundle_head")
    payload = dict(evidence[bundle].payload)
    payload["commit_policy"] = {
        "checked": True,
        "author_problems": [{"sha": "c" * 40, "author": "someone@elsewhere.test"}],
    }
    evidence[bundle] = _ev("bundle_head", payload, ident=evidence[bundle].id)
    gi = _gi(evidence, policy={"git": {"author_email": "worker@example.test"}})
    gates = sorted(PRE_PR_GATES | {GateName.COMMIT_POLICY})
    outcomes = evaluate_pre_pr(gates, gi)
    advisory = advisory_gates({"gates": {"advisory": []}})
    assert outcomes[GateName.COMMIT_POLICY].result is GateResult.FAIL
    assert blocking(outcomes, advisory) == []
    assert pre_pr_verdict(outcomes, advisory) is PrePrVerdict.PASSED
    assert for_reviewer(outcomes, advisory) == [
        {
            "gate": GateName.COMMIT_POLICY,
            "detail": (
                "1 commit(s) not authored as crucible-worker@users.noreply.github.com "
                f"({'c' * 12} by someone@elsewhere.test)"
            ),
        }
    ]


def test_false_claim_evidence_row_is_recorded() -> None:
    """hades #183: a worker claiming exit 0 while the re-run exits nonzero produces a
    FALSE_CLAIM evidence row in the application layer."""

    assert str(EvidenceKind.FALSE_CLAIM) == "false_claim"
    payload = {"check": "V1", "claimed_exit": 0, "rerun_exit": 127, "command": "make lint"}
    kind = EvidenceKind.FALSE_CLAIM
    assert kind.value == "false_claim"
    assert payload["check"] == "V1"
    assert payload["claimed_exit"] == 0
    assert payload["rerun_exit"] == 127


def test_a_boolean_exit_is_not_a_false_claim() -> None:
    """Codex PR 272: a claimed exit that is a bool (False == 0) must not produce a
    FALSE_CLAIM evidence row; only a real integer 0 triggers the guard."""

    assert str(EvidenceKind.FALSE_CLAIM) == "false_claim"
    # Boolean exit False should not be accepted as an integer exit
    claim = {"checks": [{"id": "V1", "exit": False}]}

    checks = _claimed_checks(claim)
    assert checks == []  # bool exit is excluded
