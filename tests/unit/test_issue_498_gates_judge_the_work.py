"""hades #498: the gates judge the work, not the time sheet.

The operator's rule of 2026-10-06. (1) The report gates are advisory: a missing report,
a parse failure and a missing finding disposition are listed for the reviewer and never
fail the attempt or class it `completed_without_report`. (2) Hades composes the
completion record from the commits, its re-run checks and the diff against each review
finding's path; the reviewer view and the PR body carry it. (3) A budget stop or a
text-only end with commits is a normal end, `ended_by_budget`, collected and gated like
a completed run. (4) The Qwen wrapper writes a minimal report when the run left none,
and its adapter mirrors the Hermes limits. (5) No new required report field."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from crucible.adapters.harness import base
from crucible.adapters.harness.qwen_code import QwenCodeAdapter
from crucible.application.gates import evidence_items
from crucible.application.queries import attempt_report
from crucible.application.supervisor import TERMINATION_TIMEOUT, local_cap_kind
from crucible.contracts.completion_claim import (
    JUDGEMENT_FIELDS,
    CompletionClaimV1,
    load_report,
    parse_claim,
)
from crucible.contracts.policy import parse_policy
from crucible.domain.completion_record import (
    DISPOSITION_ADDRESSED,
    DISPOSITION_NOT_ADDRESSED,
    WORKER_REPORT_ABSENT,
    WORKER_REPORT_PARSED,
    BranchFacts,
    CheckRun,
    ReviewFinding,
    compose_completion_record,
    disposition_notes,
)
from crucible.domain.exit_class import CLEAN_EXIT_CLASSES, ExitClass
from crucible.domain.gates import (
    ALWAYS_ADVISORY_GATES,
    ALWAYS_BLOCKING_GATES,
    NO_REPORT_DETAIL,
    PAPERWORK_GATES,
    PRE_PR_GATES,
    WORK_GATES,
    GateInput,
    GateName,
    GateResult,
    PrePrVerdict,
    advisory_gates,
    blocking,
    evaluate_pre_pr,
    for_reviewer,
    pre_pr_verdict,
)
from crucible.domain.harness_settings import (
    DEFAULT_QWEN_MAX_OUTPUT_TOKENS,
    effective_settings,
    qwen_effective_settings,
)
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.domain.publication import BodyInput, render_body
from crucible.ports.execution import BranchBundle, CollectedOutputs, VerificationRun
from crucible.ports.harness import LaunchContext
from tests.fixtures import contract_document
from tests.unit.test_advisory_gates import _without_review
from tests.unit.test_gates import HEAD, _claim_payload, _ev, _gi, _passing_evidence
from tests.unit.test_issue_353_infrastructure_interruptions import _running
from tests.unit.test_issue_401_codex_findings_correction import _report
from tests.unit.test_policy_schema import seeded_policy_v3
from tests.unit.test_report_check import CRITERIA, judgement_only, load_checker

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "images" / "worker" / "crucible-qwen-code.py"
DEFAULT = advisory_gates({})


def _no_report_evidence() -> list[Any]:
    return _without_review(
        [e for e in _passing_evidence() if e.payload.get("role") != "completion_claim"]
    )


# ----- (1) the report gates are advisory ----------------------------------------------


def test_the_blocking_gates_are_the_ones_about_the_work() -> None:
    """AC1: for an attempt with commits, the gates that stop it are the work gates;
    the paperwork gates are advisory, and the report gate always is."""
    assert {
        GateName.COMMITS_PRESENT,
        GateName.VERIFICATION_RAN,
        GateName.SCOPE_CONTAINED,
        GateName.NO_INJECTED_FILES,
        GateName.NO_SECRETS,
        GateName.DEPENDENCIES_UNCHANGED,
        GateName.CI_UNCHANGED,
        GateName.WORKSPACE_CLEAN,
        GateName.EDITOR_LEFTOVERS,
        GateName.EXIT_CLEAN,
    } == WORK_GATES
    # scope_contained is advisory for a path outside allowed_paths and blocks for a
    # prohibited one (ADR 0024); every other work gate blocks by default.
    assert not (WORK_GATES - {GateName.SCOPE_CONTAINED}) & DEFAULT
    assert PAPERWORK_GATES <= DEFAULT
    assert GateName.REPORT_PRESENT in ALWAYS_ADVISORY_GATES
    assert {GateName.NO_SECRETS} == ALWAYS_BLOCKING_GATES
    assert not WORK_GATES & PAPERWORK_GATES


def test_no_report_with_passing_work_gates_passes_with_the_gap_listed() -> None:
    """AC1: an attempt with commits and passing work gates but no report passes the
    pre-PR gates; the missing report is for the reviewer in plain words."""
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(_no_report_evidence()))
    report = outcomes[GateName.REPORT_PRESENT]
    assert report.result is GateResult.FAIL and not report.always_blocks
    assert report.detail == NO_REPORT_DETAIL
    assert blocking(outcomes, DEFAULT) == []
    assert pre_pr_verdict(outcomes, DEFAULT) is PrePrVerdict.PASSED
    listed = {item["gate"]: item["detail"] for item in for_reviewer(outcomes, DEFAULT)}
    assert listed[GateName.REPORT_PRESENT] == NO_REPORT_DETAIL


def test_a_report_that_does_not_parse_is_for_the_reviewer_not_a_stop() -> None:
    claim = _claim_payload(
        parsed_ok=False,
        parse_errors=[{"loc": ["self_review"], "msg": "Field required", "type": "missing"}],
    )
    evidence = _without_review(_passing_evidence())
    evidence[1] = _ev("artifact_present", claim, ident=2)
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(evidence))
    report = outcomes[GateName.REPORT_PRESENT]
    assert report.result is GateResult.FAIL and not report.always_blocks
    assert "self_review: Field required" in report.detail
    assert "Hades composed the completion record" in report.detail
    assert pre_pr_verdict(outcomes, DEFAULT) is PrePrVerdict.PASSED


def test_a_missing_finding_disposition_is_an_advisory_note_on_a_parsed_report() -> None:
    """AC1: the correction report parsed; a finding it left without a disposition is a
    finding for the reviewer on the passing report gate, never a failure."""
    note = (
        "the report gives no disposition for review finding(s) finding-2; "
        "Hades read the diff for them"
    )
    evidence = _without_review(_passing_evidence())
    evidence[1] = _ev("artifact_present", _claim_payload(advisory=[note]), ident=2)
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(evidence))
    report = outcomes[GateName.REPORT_PRESENT]
    assert report.result is GateResult.PASS and report.findings == (note,)
    assert pre_pr_verdict(outcomes, DEFAULT) is PrePrVerdict.PASSED
    assert {"gate": GateName.REPORT_PRESENT, "detail": note} in for_reviewer(outcomes, DEFAULT)


def test_disposition_notes_name_only_review_comment_ids() -> None:
    notes = disposition_notes(
        {"finding-1", "finding-2"}, {"finding-2": 2, "finding-9": 1}, ["finding-2"]
    )
    assert notes == [
        "the report gives no disposition for review finding(s) finding-1; "
        "Hades read the diff for them",
        "the report dispositions finding-9, which this correction does not address",
        "the report dispositions finding-2 more than once; Hades recorded none of those",
    ]


def test_no_policy_can_make_the_report_gate_block() -> None:
    assert GateName.REPORT_PRESENT in advisory_gates({"gates": {"advisory": []}})
    document = seeded_policy_v3()
    document["gates"]["advisory"] = ["report_present"]
    with pytest.raises(ValidationError, match="always advisory"):
        parse_policy(document)


def test_exit_clean_accepts_a_budget_end_whatever_the_code() -> None:
    assert ExitClass.ENDED_BY_BUDGET in CLEAN_EXIT_CLASSES
    evidence = _without_review(_passing_evidence())
    evidence[0] = _ev("exit_info", {"exit_code": 137, "exit_class": "ended_by_budget"}, ident=1)
    outcomes = evaluate_pre_pr([GateName.EXIT_CLEAN], _gi(evidence))
    assert outcomes[GateName.EXIT_CLEAN].result is GateResult.PASS
    assert "budget" in outcomes[GateName.EXIT_CLEAN].detail
    evidence[0] = _ev("exit_info", {"exit_code": 137, "exit_class": "timeout"}, ident=1)
    assert (
        evaluate_pre_pr([GateName.EXIT_CLEAN], _gi(evidence))[GateName.EXIT_CLEAN].result
        is GateResult.FAIL
    )


# ----- the supervisor: ends with commits are normal ends --------------------------------


def _bundle(*paths: str, commits: int = 1) -> BranchBundle:
    return BranchBundle(
        head_sha=HEAD,
        base_ref="main",
        work_branch="crucible/EX-0001",
        commits=commits,
        verified=True,
        sha256="f" * 64,
        commit_paths=paths,
        commit_messages=("Return 409 on duplicate import",) if commits else (),
    )


def _finished(
    monkeypatch: pytest.MonkeyPatch,
    *,
    harness: str = "codex",
    findings: dict[str, str | None] | None = None,
) -> tuple[Any, Any, Any, dict[str, Any], list[Any]]:
    supervisor, pending, uow, _attempts = _running(monkeypatch, harness=harness)
    claims: dict[str, Any] = {}
    uow.claims.put.side_effect = lambda claim: claims.update({claim.attempt_id: claim})
    uow.claims.get.side_effect = claims.get
    uow.evidence.list_for_task.return_value = []
    dispositions: list[Any] = []
    uow.dispositions.get_by_comment.side_effect = lambda cid, digest: next(
        (d for d in dispositions if d.review_comment_id == cid), None
    )
    uow.dispositions.add.side_effect = dispositions.append
    if findings is not None:
        pending.contract["correction"] = {
            "addresses": [{"kind": "review_comment", "id": name} for name in findings]
        }
        comments = {
            name: SimpleNamespace(id=name, body_sha256="body", github_id=name, path=path)
            for name, path in findings.items()
        }
        uow.review_comments.get.side_effect = comments.get
    return supervisor, pending, uow, claims, dispositions


def _gate_input(uow: Any, pending: Any) -> GateInput:
    return GateInput(
        contract=pending.contract,
        policy={},
        head_sha=HEAD,
        evidence=evidence_items(uow, pending.attempt.id, pending.task.id),
    )


def test_a_clean_exit_with_commits_and_no_report_is_completed_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 and AC3: a text-only end that left commits is `completed`, never
    `completed_without_report`; the attempt succeeds and the task reaches its gates."""
    supervisor, pending, uow, claims, _ = _finished(monkeypatch)
    outputs = CollectedOutputs(
        report=None,
        report_raw=None,
        blocked_md=None,
        diff_paths=("src/ledger/a.py",),
        bundle=_bundle("src/ledger/a.py"),
    )
    supervisor._finish_exited(pending.attempt.id, 0, outputs)
    assert pending.attempt.exit_class is ExitClass.COMPLETED
    assert pending.attempt.state is AttemptState.SUCCEEDED
    assert pending.task.state is TaskState.REPORTED
    record = claims[pending.attempt.id]
    assert record.parsed_ok is False
    assert record.parse_errors == [{"loc": [], "msg": "no report was written", "type": "missing"}]
    assert record.document["composed"]["worker_report"]["status"] == WORKER_REPORT_ABSENT
    assert record.document["composed"]["branch"]["commits"] == 1
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gate_input(uow, pending))
    assert outcomes[GateName.REPORT_PRESENT].detail == NO_REPORT_DETAIL
    assert outcomes[GateName.EXIT_CLEAN].result is GateResult.PASS
    assert GateName.REPORT_PRESENT not in blocking(outcomes, DEFAULT)


def test_a_clean_exit_with_no_commits_and_no_report_stays_without_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, _, _, _ = _finished(monkeypatch)
    outputs = CollectedOutputs(
        report=None, report_raw=None, blocked_md=None, bundle=_bundle(commits=0)
    )
    supervisor._finish_exited(pending.attempt.id, 0, outputs)
    assert pending.attempt.exit_class is ExitClass.COMPLETED_WITHOUT_REPORT
    assert pending.attempt.state is AttemptState.FAILED


def test_a_correction_report_without_dispositions_succeeds_and_hades_reads_the_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 and AC2: the twelve attempts of 2026-10-06. The report parsed, the gap is
    advisory, the attempt succeeds, and the record says which finding the diff touched."""
    supervisor, pending, uow, claims, dispositions = _finished(
        monkeypatch, findings={"finding-1": "src/ledger/a.py", "finding-2": "src/ledger/b.py"}
    )
    outputs = CollectedOutputs(
        report=_report([]),
        report_raw=None,
        blocked_md=None,
        diff_paths=("src/ledger/a.py",),
        bundle=_bundle("src/ledger/a.py"),
        verifications=(
            VerificationRun(id="V1", command="make lint", expect_exit=0, exit_code=0, log_tail=""),
            VerificationRun(id="V2", command="make test", expect_exit=0, exit_code=1, log_tail=""),
        ),
    )
    supervisor._finish_exited(pending.attempt.id, 0, outputs)
    assert pending.attempt.exit_class is ExitClass.COMPLETED
    assert pending.attempt.state is AttemptState.SUCCEEDED
    record = claims[pending.attempt.id]
    assert record.parsed_ok and record.parse_errors == []
    composed = record.document["composed"]
    assert composed["by"] == "hades"
    assert composed["worker_report"]["status"] == WORKER_REPORT_PARSED
    assert record.document["summary"] == "Corrected the review findings."
    assert composed["checks_passed"] == "1 of 2"
    assert [c["id"] for c in composed["checks"]] == ["V1", "V2"]
    assert composed["findings"] == [
        {
            "review_comment_id": "finding-1",
            "path": "src/ledger/a.py",
            "disposition": DISPOSITION_ADDRESSED,
            "commit": HEAD,
            "source": "diff",
        },
        {
            "review_comment_id": "finding-2",
            "path": "src/ledger/b.py",
            "disposition": DISPOSITION_NOT_ADDRESSED,
            "commit": None,
            "source": "diff",
        },
    ]
    # Hades's reading of the diff is shown, not recorded as the finding's disposition.
    assert dispositions == []
    claim_row = next(
        row
        for row in uow.evidence.list_for_attempt(pending.attempt.id)
        if row.payload.get("role") == "completion_claim"
    )
    assert claim_row.payload["advisory"] == [
        "the report gives no disposition for review finding(s) finding-1, finding-2; "
        "Hades read the diff for them"
    ]
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gate_input(uow, pending))
    assert outcomes[GateName.REPORT_PRESENT].result is GateResult.PASS
    assert outcomes[GateName.REPORT_PRESENT].findings == tuple(claim_row.payload["advisory"])
    assert GateName.REPORT_PRESENT not in blocking(outcomes, DEFAULT)
    # The reviewer view is the composed record.
    uow.attempts.get.return_value = pending.attempt
    view = attempt_report(uow, pending.attempt.id)
    assert view.document["composed"]["findings"][0]["disposition"] == DISPOSITION_ADDRESSED


def test_a_worker_disposition_is_recorded_and_kept_beside_hades_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, _, claims, dispositions = _finished(
        monkeypatch, findings={"finding-1": "src/ledger/a.py", "finding-2": "src/ledger/b.py"}
    )
    report = _report(
        [{"review_comment_id": "finding-2", "disposition": "declined", "reason": "Unreachable."}]
    )
    outputs = CollectedOutputs(
        report=report, report_raw=None, blocked_md=None, bundle=_bundle("src/ledger/a.py")
    )
    supervisor._finish_exited(pending.attempt.id, 0, outputs)
    assert pending.attempt.state is AttemptState.SUCCEEDED
    assert [d.review_comment_id for d in dispositions] == ["finding-2"]
    findings = claims[pending.attempt.id].document["composed"]["findings"]
    assert findings[1]["worker"] == {
        "disposition": "declined",
        "commit": None,
        "reason": "Unreachable.",
    }
    assert "worker" not in findings[0]


def test_a_time_limit_stop_with_commits_is_ended_by_budget_and_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: the attempt's time limit stopped a run that had committed. The bundle is
    collected, the exit is `ended_by_budget`, the attempt succeeds and reaches its
    gates, and `exit_clean` passes on the class."""
    supervisor, pending, uow, claims, _ = _finished(monkeypatch)
    pending.attempt.termination_reason = TERMINATION_TIMEOUT
    outputs = CollectedOutputs(
        report=None,
        report_raw=None,
        blocked_md=None,
        diff_paths=("src/ledger/a.py",),
        bundle=_bundle("src/ledger/a.py"),
    )
    supervisor._finish_exited(pending.attempt.id, 137, outputs)
    assert pending.attempt.exit_class is ExitClass.ENDED_BY_BUDGET
    assert pending.attempt.state is AttemptState.SUCCEEDED
    assert pending.task.state is TaskState.REPORTED
    ended = claims[pending.attempt.id].document["composed"]["ended"]
    assert ended["exit_class"] == "ended_by_budget"
    assert ended["how"] == "ended on its budget: the attempt's time limit"
    exited = next(e for e in uow.events.append.call_args_list if e.args[0].kind == "attempt_exited")
    assert exited.args[0].payload["exit_class"] == "ended_by_budget"
    assert not any(e.args[0].kind == "attempt_failed" for e in uow.events.append.call_args_list)
    outcomes = evaluate_pre_pr([GateName.EXIT_CLEAN], _gate_input(uow, pending))
    assert outcomes[GateName.EXIT_CLEAN].result is GateResult.PASS


def test_a_turn_limit_stop_with_commits_is_ended_by_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3: Qwen stopped at its session turn limit after committing."""
    supervisor, pending, _, claims, _ = _finished(monkeypatch, harness="qwen_code")
    pending.attempt.workspace_path = str(tmp_path)
    report_dir = tmp_path / "output" / "report"
    report_dir.mkdir(parents=True)
    (report_dir / base.TRANSCRIPT_NAME).write_text(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "error": "maximum turns reached",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )
        + "\n"
    )
    outputs = CollectedOutputs(
        report=None, report_raw=None, blocked_md=None, bundle=_bundle("src/ledger/a.py")
    )
    supervisor._finish_exited(pending.attempt.id, 1, outputs)
    assert pending.attempt.exit_class is ExitClass.ENDED_BY_BUDGET
    assert pending.attempt.state is AttemptState.SUCCEEDED
    ended = claims[pending.attempt.id].document["composed"]["ended"]
    assert ended["how"] == "ended on its budget: Qwen reached its session turn limit"


def test_a_budget_stop_without_commits_keeps_its_class(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, _, _, _ = _finished(monkeypatch)
    pending.attempt.termination_reason = TERMINATION_TIMEOUT
    outputs = CollectedOutputs(
        report=None, report_raw=None, blocked_md=None, bundle=_bundle(commits=0)
    )
    supervisor._finish_exited(pending.attempt.id, 137, outputs)
    assert pending.attempt.exit_class is ExitClass.TIMEOUT
    assert pending.attempt.state is AttemptState.FAILED


def test_a_budget_end_is_never_a_local_size_cap() -> None:
    assert local_cap_kind("local", ExitClass.ENDED_BY_BUDGET, True) is None
    assert local_cap_kind("local", ExitClass.TIMEOUT, False) == "time"
    assert local_cap_kind("local", ExitClass.COMPLETED_WITHOUT_REPORT, True) == "turns"


# ----- (2) the composed completion record ----------------------------------------------


def _composed(worker: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "exit_class": ExitClass.COMPLETED,
        "exit_code": 0,
        "branch": BranchFacts(
            head_sha=HEAD,
            commits=2,
            work_branch="crucible/EX-0001",
            commit_messages=("fix the import\n\nlong body", "add the test"),
            commit_paths=("src/ledger/a.py", "tests/ledger/test_a.py"),
        ),
        "diff_paths": ("src/ledger/a.py", "tests/ledger/test_a.py"),
        "checks": (
            CheckRun(id="V1", command="make lint", exit_code=0),
            CheckRun(id="V2", command="make test", exit_code=2),
            CheckRun(
                id="V3", command="make scan", exit_code=0, ran=False, detail="verifier timed out"
            ),
        ),
        "findings": (
            ReviewFinding("finding-1", "src/ledger/a.py"),
            ReviewFinding("finding-2", "src/other.py"),
            ReviewFinding("finding-3", None),
        ),
        "worker_report": worker,
        "worker_report_status": WORKER_REPORT_PARSED if worker else WORKER_REPORT_ABSENT,
    }
    arguments.update(overrides)
    return compose_completion_record(**arguments)


def test_hades_composes_the_record_from_its_own_evidence() -> None:
    """AC2: commits, re-run checks and per-finding diff coverage, with nothing from a
    worker."""
    record = _composed()
    composed = record["composed"]
    assert composed["by"] == "hades"
    assert composed["branch"] == {
        "head_sha": HEAD,
        "work_branch": "crucible/EX-0001",
        "commits": 2,
        "commit_messages": ["fix the import", "add the test"],
        "changed_paths": ["src/ledger/a.py", "tests/ledger/test_a.py"],
    }
    assert [(c["id"], c["exit"], c["passed"]) for c in composed["checks"]] == [
        ("V1", 0, True),
        ("V2", 2, False),
        ("V3", None, False),
    ]
    assert composed["checks"][2]["detail"] == "verifier timed out"
    assert composed["checks_passed"] == "1 of 3"
    assert [
        (f["review_comment_id"], f["disposition"], f["commit"]) for f in composed["findings"]
    ] == [
        ("finding-1", DISPOSITION_ADDRESSED, HEAD),
        ("finding-2", DISPOSITION_NOT_ADDRESSED, None),
        ("finding-3", DISPOSITION_NOT_ADDRESSED, None),
    ]
    assert "names no path" in composed["findings"][2]["note"]
    assert composed["worker_report"] == {"status": WORKER_REPORT_ABSENT, "problems": []}
    assert record["summary"].startswith("Composed by Hades: 2 commit(s) on the branch")
    assert "1 of 3 review finding(s) touched by the diff" in record["summary"]
    assert "self_review" not in record


def test_the_worker_report_merges_in_and_never_replaces_hades_reading() -> None:
    worker = {
        "summary": "The worker's own words.",
        "self_review": {"documentation": ["none"], "acceptance_criteria": [], "omissions": []},
        "limitations": ["one"],
        "finding_dispositions": [
            {"review_comment_id": "finding-2", "disposition": "fixed", "commit": "b" * 40},
            {"review_comment_id": "finding-1", "disposition": "declined", "reason": "No."},
        ],
        "changed_files": ["src/ledger/a.py"],
    }
    record = _composed(worker)
    assert record["summary"] == "The worker's own words."
    assert record["limitations"] == ["one"] and record["self_review"]["documentation"] == ["none"]
    assert "changed_files" not in record
    findings = record["composed"]["findings"]
    assert findings[0]["disposition"] == DISPOSITION_ADDRESSED
    assert findings[0]["worker"] == {"disposition": "declined", "commit": None, "reason": "No."}
    assert findings[1]["disposition"] == DISPOSITION_NOT_ADDRESSED
    assert findings[1]["worker"]["commit"] == "b" * 40
    assert record["composed"]["worker_report"]["status"] == WORKER_REPORT_PARSED


def test_the_pr_body_carries_the_completion_record() -> None:
    """AC2: the body shows how it ended, the branch, the checks and each finding."""
    record = _composed(
        worker_report_status="did not parse",
        worker_report_problems=["self_review: Field required"],
    )
    body = render_body(
        BodyInput(
            external_id="EX-0001",
            objective="Return 409.",
            head_sha=HEAD,
            attempt_id="attempt-1",
            harness="codex",
            harness_version="0.1",
            image_digest="sha256:abc",
            record=record["composed"],
        )
    )
    assert "## Completion record" in body
    assert "- ended: the worker ended its turn (exit 0)" in body
    assert f"- branch: 2 commit(s) at `{HEAD}`" in body
    assert "- required checks passed: 1 of 3" in body
    assert "- worker report: did not parse" in body
    assert "  - self_review: Field required" in body
    assert f"| `finding-1` | `src/ledger/a.py` | addressed | `{HEAD}` | none |" in body
    assert "| `finding-2` | `src/other.py` | not addressed | `none` | none |" in body
    assert "| `finding-3` | `no path` | not addressed | `none` | none |" in body


def test_a_body_without_a_record_has_no_record_section() -> None:
    body = render_body(
        BodyInput(
            external_id="EX-0001",
            objective="Return 409.",
            head_sha=HEAD,
            attempt_id="attempt-1",
            harness="codex",
            harness_version="0.1",
            image_digest="sha256:abc",
        )
    )
    assert "## Completion record" not in body


# ----- (4) the Qwen wrapper and adapter -------------------------------------------------


def _wrapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("crucible_qwen_code", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _events(with_commit: bool) -> list[dict[str, Any]]:
    commit = "git commit -qm 'worker: return 409' --allow-empty" if with_commit else "true"
    return [
        {"type": "system", "subtype": "system_start", "model": "qwen-lane"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "run_shell_command",
                        "input": {"command": "make lint"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-2",
                        "name": "run_shell_command",
                        "input": {"command": commit},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "call-2", "content": "ok"}]
            },
        },
        {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 12},
    ]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _fake_qwen(
    tmp_path: Path, events: list[dict[str, Any]], *, exit_code: int = 0, write: str = ""
) -> tuple[Path, Path]:
    """A fake `qwen` that prints stream-json, commits as the model's shell command would,
    optionally writes a file into the report directory, and exits as told."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@example")
    _git(repo, "config", "user.name", "worker")
    (repo / "README").write_text("base\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "base")
    stream = tmp_path / "stream.jsonl"
    stream.write_text("".join(json.dumps(e) + "\n" for e in events))
    binary = tmp_path / "qwen"
    commit = any(
        "git commit" in str(b.get("input", {}).get("command", ""))
        for e in events
        for b in (e.get("message") or {}).get("content", [])
        if isinstance(b, dict)
    )
    lines = ["#!/usr/bin/env bash", "set -e", f"cat {stream}"]
    if commit:
        lines.append("git commit -qm 'worker: return 409' --allow-empty")
    if write:
        lines.append(f"printf '%s\\n' {json.dumps(write)} > \"$CRUCIBLE_QWEN_REPORT_DIR/{write}\"")
    lines.append(f"exit {exit_code}")
    binary.write_text("\n".join(lines) + "\n")
    binary.chmod(0o755)
    return repo, binary


def _run_wrapper(tmp_path: Path, repo: Path, binary: Path) -> subprocess.CompletedProcess[str]:
    identity = tmp_path / "IDENTITY.md"
    identity.write_text("# Task EX-0001\n\nWrite report.yaml.\n")
    report_dir = tmp_path / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "CRUCIBLE_QWEN_BINARY": str(binary),
        "CRUCIBLE_QWEN_IDENTITY": str(identity),
        "CRUCIBLE_QWEN_CONTEXT_LENGTH": "65536",
        "CRUCIBLE_QWEN_MAX_OUTPUT_TOKENS": "32000",
        "CRUCIBLE_QWEN_THINKING": "false",
        "CRUCIBLE_QWEN_REPORT_DIR": str(report_dir),
    }
    launch = QwenCodeAdapter().build_launch(_context())
    return subprocess.run(
        [sys.executable, str(WRAPPER), *launch.argv[1:]],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _context(**kwargs: Any) -> LaunchContext:
    return LaunchContext(
        attempt_id="attempt",
        model="qwen-lane",
        effort=None,
        timeout_seconds=600,
        identity_mount="/crucible/identity",
        report_mount="/crucible/report",
        repo_mount="/crucible/repo",
        endpoint="local",
        endpoint_url="https://gateway.example/v1",
        **kwargs,
    )


def test_the_wrapper_compiles() -> None:
    subprocess.run([sys.executable, "-m", "py_compile", str(WRAPPER)], check=True)


def test_the_wrapper_writes_a_minimal_report_from_the_run_log(tmp_path: Path) -> None:
    """AC4: a fake qwen runs two commands, commits, and leaves no report; the wrapper
    passes the stream through, keeps the exit code, and writes the minimal report."""
    repo, binary = _fake_qwen(tmp_path, _events(with_commit=True))
    done = _run_wrapper(tmp_path, repo, binary)
    assert done.returncode == 0, done.stderr
    assert [json.loads(line)["type"] for line in done.stdout.splitlines()] == [
        "system",
        "assistant",
        "user",
        "assistant",
        "user",
        "result",
    ]
    assert "wrote a minimal report from the run log" in done.stderr
    raw = (tmp_path / "report" / "report.yaml").read_text()
    document, errors = load_report(raw)
    assert errors == [] and document is not None
    assert document["schema_version"] == "1.0"
    assert "Qwen Code ended its turn (exit 0) after 2 tool call(s)" in document["summary"]
    assert "Commands run (2): make lint; git commit" in document["summary"]
    head = _git(repo, "rev-parse", "--short", "HEAD")
    assert f"Commits on the branch (1): {head} worker: return 409" in document["summary"]
    assert set(document) == {"schema_version", "summary", "limitations"}
    assert "carries no self-review" in document["limitations"][0]
    # Hades reads it as a present report that did not parse: for the reviewer, with the
    # fields it lacks named, and the completion record composed from Hades's own facts.
    claim, problems = parse_claim(document)
    assert claim is None and {e["loc"][0] for e in problems} >= {"self_review"}
    settings = json.loads((tmp_path / "home" / ".qwen" / "settings.json").read_text())
    assert settings["model"]["generationConfig"]["contextWindowSize"] == 65536


def test_the_wrapper_leaves_the_models_report_or_blocked_md_alone(tmp_path: Path) -> None:
    for name in ("report.yaml", "blocked.md"):
        repo, binary = _fake_qwen(
            tmp_path / name.split(".")[0], _events(with_commit=False), write=name
        )
        done = _run_wrapper(tmp_path / name.split(".")[0], repo, binary)
        assert done.returncode == 0, done.stderr
        report_dir = tmp_path / name.split(".")[0] / "report"
        assert sorted(p.name for p in report_dir.iterdir()) == [name]
        assert (report_dir / name).read_text() == f"{name}\n"
        assert "minimal report" not in done.stderr


def test_the_wrapper_keeps_the_exit_code_and_says_how_the_run_ended(tmp_path: Path) -> None:
    events = _events(with_commit=False)
    events[-1] = {"type": "result", "subtype": "error_max_turns", "is_error": True}
    repo, binary = _fake_qwen(tmp_path, events, exit_code=1)
    done = _run_wrapper(tmp_path, repo, binary)
    assert done.returncode == 1
    document, _ = load_report((tmp_path / "report" / "report.yaml").read_text())
    assert document is not None
    assert "Qwen Code stopped at its session turn limit (exit 1)" in document["summary"]
    assert "No commit was made during the run" in document["summary"]


def test_the_run_log_reads_only_shell_commands_and_the_result() -> None:
    module = _wrapper()
    log = module.RunLog()
    for event in _events(with_commit=False):
        log.line(json.dumps(event))
    log.line("not json")
    log.line('{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}')
    assert log.commands == ["make lint", "true"] and log.tool_calls == 2
    assert log.how_it_ended(0) == "Qwen Code ended its turn (exit 0)"
    assert module.RunLog().how_it_ended(3) == "Qwen Code exited 3 without a result event"


def test_the_adapter_restricts_tools_and_sets_thinking_off_and_an_output_cap(
    tmp_path: Path,
) -> None:
    """AC4: the launch and the settings the wrapper writes mirror the Hermes launch."""
    launch = QwenCodeAdapter().build_launch(_context())
    assert launch.argv[1:3] == ("--yolo", "--auth-type")
    assert "--max-session-turns" in launch.argv
    assert launch.env["CRUCIBLE_QWEN_THINKING"] == "false"
    assert launch.env["CRUCIBLE_QWEN_MAX_OUTPUT_TOKENS"] == str(DEFAULT_QWEN_MAX_OUTPUT_TOKENS)
    assert launch.env["CRUCIBLE_QWEN_REPORT_DIR"] == "/crucible/report"
    capped = QwenCodeAdapter().build_launch(_context(harness_settings={"max_output_tokens": 8000}))
    assert capped.env["CRUCIBLE_QWEN_MAX_OUTPUT_TOKENS"] == "8000"

    module = _wrapper()
    settings = module.write_settings(tmp_path, 65536, 32000, False)
    assert settings == json.loads((tmp_path / ".qwen" / "settings.json").read_text())
    tools = settings["tools"]
    assert tools["core"] == module.CORE_TOOLS
    assert {"run_shell_command", "read_file", "write_file", "edit"} <= set(tools["core"])
    for name in ("task", "skill", "save_memory", "web_fetch", "web_search"):
        assert name not in tools["core"] and name in tools["exclude"]
    assert not tools["shell"]["enableInteractiveShell"]
    assert settings["context"] == {
        "fileName": module.NO_RULES_FILE,
        "loadMemoryFromIncludeDirectories": False,
    }
    assert settings["model"]["generationConfig"] == {
        "contextWindowSize": 65536,
        "enable_thinking": False,
        "samplingParams": {"max_tokens": 32000},
    }
    assert settings["model"]["maxToolCallsPerTurn"] == 0


def test_the_qwen_settings_are_recorded_as_the_hermes_ones_are() -> None:
    qwen = qwen_effective_settings({"max_output_tokens": 16000}, context_length=98304)
    assert qwen == {"context_length": 98304, "max_output_tokens": 16000, "thinking": False}
    assert qwen_effective_settings({}, context_length=None) == {
        "context_length": 131072,
        "max_output_tokens": DEFAULT_QWEN_MAX_OUTPUT_TOKENS,
        "thinking": False,
    }
    assert set(qwen) == set(effective_settings({}, thinking=True))


# ----- (5) no new required report field --------------------------------------------------


def test_todays_valid_report_still_passes_and_nothing_new_is_required() -> None:
    """AC5: the worker-written report has no new required field, and crucible-report
    check is no stricter than before."""
    checker = load_checker()
    document = judgement_only()
    assert checker.check(document, CRITERIA) == []
    claim, errors = parse_claim(document, criteria=CRITERIA)
    assert claim is not None and errors == []
    required = {
        name for name, field in CompletionClaimV1.model_fields.items() if field.is_required()
    }
    assert required == {"schema_version", *JUDGEMENT_FIELDS}
    assert not CompletionClaimV1.model_fields["finding_dispositions"].is_required()
    # The contract's own fixture, with every finding left undispositioned, is valid.
    assert checker.check(_report([]), ["AC1", "AC2"]) == []
    assert checker.JUDGEMENT_FIELDS == JUDGEMENT_FIELDS


def test_the_identity_names_no_disposition_field_as_required(tmp_path: Path) -> None:
    from tests.unit.test_identity_bundle import build  # noqa: PLC0415

    identity, _, _ = build(tmp_path)
    assert "finding_dispositions" not in identity
    assert contract_document()["acceptance_criteria"][0]["id"] == "AC1"
