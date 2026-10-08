"""Every pre-PR gate against synthetic evidence (18), including the rule that
worker-asserted evidence never satisfies a gate (11)."""

from __future__ import annotations

from typing import Any

import pytest

from crucible.domain.gates import (
    COLLECTOR_MARKER,
    DEFERRED_TO_C3,
    ENFORCED_PRE_PR_GATES,
    OPTIONAL_PRE_PR_GATES,
    PRE_PR_EVALUATORS,
    PRE_PR_GATES,
    EvidenceItem,
    GateInput,
    GateName,
    GateResult,
    blocking,
    evaluate_gate,
    evaluate_pre_pr,
    waiting_for_review,
)
from tests.fixtures import contract_document

HEAD = "a" * 40


def _ev(
    kind: str,
    payload: dict[str, Any],
    *,
    ident: int = 1,
    source: str = "crucible",
    verified: bool = True,
) -> EvidenceItem:
    return EvidenceItem(id=ident, kind=kind, source=source, verified=verified, payload=payload)


def _claim_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": "completion_claim",
        "parsed_ok": True,
        "self_review_checked": True,
        "parse_errors": [],
        "claimed_head_sha": HEAD,
        "mapped_criteria": [{"id": "AC1", "status": "met"}, {"id": "AC2", "status": "met"}],
        "run_evidence": ["report/run-evidence.md"],
        "changed_files": ["src/ledger/fake_change.py"],
    }
    payload.update(overrides)
    return payload


def _passing_evidence() -> list[EvidenceItem]:
    return [
        _ev("exit_info", {"exit_code": 0, "exit_class": "completed"}, ident=1),
        _ev("artifact_present", _claim_payload(), ident=2),
        _ev(
            "bundle_head",
            {
                "head_sha": HEAD,
                "claimed_head_sha": HEAD,
                "commits": 2,
                "bundle_verified": True,
                "commit_paths": ["src/ledger/fake_change.py"],
                "commit_messages": ["fix the import"],
                "commit_policy": {"checked": True, "author_problems": []},
            },
            ident=3,
        ),
        _ev(
            "diff_paths",
            {"paths": ["src/ledger/fake_change.py", "tests/ledger/fake_change.py"]},
            ident=4,
        ),
        _ev(
            "scanner_result",
            {"findings": [], "scanned": ["report", "diff"], "diff_scanned": True},
            ident=5,
        ),
        _ev(
            "artifact_present",
            {"role": "run_evidence", "path": "report/run-evidence.md", "size": 42},
            ident=6,
        ),
        _ev(
            "verification_run",
            {"id": "V1", "command": "make lint", "exit_code": 0, "ran": True},
            ident=8,
        ),
        _ev(
            "verification_run",
            {"id": "V2", "command": "make test", "exit_code": 0, "ran": True},
            ident=9,
        ),
        _ev(
            "verification_run",
            {"id": "V3", "command": "make scan", "exit_code": 0, "ran": True},
            ident=10,
        ),
        _ev("workspace_state", {"checked": True, "leftover": []}, ident=11),
        _ev(
            "review_received",
            {
                "reviewed_head_sha": HEAD,
                "reviewer_kind": "orchestrator",
                "reviewer_is_author": False,
                "verdict": "approve",
            },
            ident=7,
        ),
    ]


def _gi(evidence: list[EvidenceItem], **kw: Any) -> GateInput:
    return GateInput(
        contract=kw.pop("contract", contract_document()),
        policy=kw.pop("policy", {}),
        head_sha=kw.pop("head_sha", HEAD),
        evidence=tuple(evidence),
        internal_review_required=kw.pop("internal_review_required", True),
    )


def test_every_pre_pr_gate_has_an_evaluator() -> None:
    assert set(PRE_PR_EVALUATORS) == (
        set(PRE_PR_GATES) | set(ENFORCED_PRE_PR_GATES) | set(OPTIONAL_PRE_PR_GATES)
    )


def test_all_pass_on_a_clean_run() -> None:
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(_passing_evidence()))
    assert blocking(outcomes) == []
    assert not waiting_for_review(outcomes)
    for gate, outcome in outcomes.items():
        expected = (
            GateResult.SKIPPED if gate == GateName.INTERNAL_REVIEW_RECORDED else GateResult.PASS
        )
        assert outcome.result is expected, (gate, outcome.detail)


def test_no_pre_pr_gate_is_deferred_any_more() -> None:
    """C3 shipped the verifier container, so `deferred` is gone for these two (11)."""
    assert not DEFERRED_TO_C3
    for gate in (GateName.VERIFICATION_RAN, GateName.WORKSPACE_CLEAN):
        assert evaluate_gate(gate, _gi(_passing_evidence())).result is GateResult.PASS


def test_verification_ran_reads_only_cruciblees_own_rerun() -> None:
    """11: the worker's own check logs never satisfy this gate."""
    evidence = [e for e in _passing_evidence() if e.kind != "verification_run"]
    assert evaluate_gate(GateName.VERIFICATION_RAN, _gi(evidence)).result is GateResult.FAIL
    worker_claimed = [
        *evidence,
        EvidenceItem(
            id=20,
            kind="verification_run",
            source="worker",
            verified=False,
            payload={"id": "V1", "exit_code": 0, "ran": True},
        ),
    ]
    assert evaluate_gate(GateName.VERIFICATION_RAN, _gi(worker_claimed)).result is GateResult.FAIL


def test_verification_ran_fails_on_a_mismatched_exit() -> None:
    evidence = [
        e
        for e in _passing_evidence()
        if not (e.kind == "verification_run" and e.payload.get("id") == "V2")
    ]
    evidence.append(_ev("verification_run", {"id": "V2", "exit_code": 2, "ran": True}, ident=21))
    outcome = evaluate_gate(GateName.VERIFICATION_RAN, _gi(evidence))
    assert outcome.result is GateResult.FAIL
    assert "V2 exited 2" in outcome.detail


def test_workspace_clean_fails_when_a_container_is_left_behind() -> None:
    evidence = [e for e in _passing_evidence() if e.kind != "workspace_state"]
    evidence.append(
        _ev("workspace_state", {"checked": True, "leftover": ["crucible-collector-x"]}, ident=22)
    )
    outcome = evaluate_gate(GateName.WORKSPACE_CLEAN, _gi(evidence))
    assert outcome.result is GateResult.FAIL
    assert "crucible-collector-x" in outcome.detail


def test_worker_asserted_evidence_never_satisfies_a_gate() -> None:
    """11: a worker's own claim is shown to Foundry and consumed by nothing."""
    worker_only = [
        EvidenceItem(id=e.id, kind=e.kind, source="worker", verified=False, payload=e.payload)
        for e in _passing_evidence()
    ]
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(worker_only))
    assert not any(o.result is GateResult.PASS for o in outcomes.values())
    assert GateName.EXIT_CLEAN in blocking(outcomes)
    assert GateName.REPORT_PRESENT in blocking(outcomes)


def test_verified_flag_alone_does_not_admit_a_worker_row() -> None:
    forged = EvidenceItem(
        id=9, kind="exit_info", source="worker", verified=True, payload={"exit_code": 0}
    )
    assert forged.admissible is False
    assert evaluate_gate(GateName.EXIT_CLEAN, _gi([forged])).result is GateResult.FAIL


def test_missing_evidence_fails_rather_than_waits() -> None:
    """09: a failed attempt still reaches `reported`, and its gates then fail."""
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi([]))
    assert GateName.EXIT_CLEAN in blocking(outcomes)
    assert GateName.COMMITS_PRESENT in blocking(outcomes)
    assert outcomes[GateName.INTERNAL_REVIEW_RECORDED].result is GateResult.SKIPPED


def test_report_present_fails_when_the_claim_did_not_parse() -> None:
    evidence = _passing_evidence()
    evidence[1] = _ev(
        "artifact_present",
        _claim_payload(parsed_ok=False, parse_errors=[{"loc": ["summary"], "msg": "missing"}]),
        ident=2,
    )
    assert evaluate_gate(GateName.REPORT_PRESENT, _gi(evidence)).result is GateResult.FAIL
    assert evaluate_gate(GateName.CRITERIA_MAPPED, _gi(evidence)).result is GateResult.FAIL


def test_exit_clean_fails_on_a_non_zero_code() -> None:
    evidence = _passing_evidence()
    evidence[0] = _ev("exit_info", {"exit_code": 1, "exit_class": "crashed"}, ident=1)
    outcome = evaluate_gate(GateName.EXIT_CLEAN, _gi(evidence))
    assert outcome.result is GateResult.FAIL and "1" in outcome.detail


@pytest.mark.parametrize("exit_class", ["incomplete", None, "unknown"])
def test_exit_clean_fails_on_a_zero_code_without_a_completed_class(
    exit_class: str | None,
) -> None:
    """Issue 128: a harness that exited 0 with work in flight is `incomplete`, and a
    valid report beside it does not let the attempt pass exit_clean."""
    evidence = _passing_evidence()
    evidence[0] = _ev("exit_info", {"exit_code": 0, "exit_class": exit_class}, ident=1)
    assert evaluate_gate(GateName.REPORT_PRESENT, _gi(evidence)).result is GateResult.PASS
    outcome = evaluate_gate(GateName.EXIT_CLEAN, _gi(evidence))
    assert outcome.result is GateResult.FAIL and repr(exit_class) in outcome.detail


def test_exit_clean_passes_a_zero_code_that_completed() -> None:
    outcome = evaluate_gate(GateName.EXIT_CLEAN, _gi(_passing_evidence()))
    assert outcome.result is GateResult.PASS


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"commits": 0}, "no commit"),
        ({"bundle_verified": False}, "bundle verify"),
        ({"head_sha": ""}, "names no head"),
    ],
)
def test_commits_present_failures(patch: dict[str, Any], reason: str) -> None:
    evidence = _passing_evidence()
    payload = dict(evidence[2].payload)
    payload.update(patch)
    evidence[2] = _ev("bundle_head", payload, ident=3)
    outcome = evaluate_gate(GateName.COMMITS_PRESENT, _gi(evidence))
    assert outcome.result is GateResult.FAIL and reason in outcome.detail


def test_scope_contained_fails_outside_allowed_paths() -> None:
    evidence = _passing_evidence()
    evidence[3] = _ev(
        "diff_paths", {"paths": ["src/ledger/a.py", "infrastructure/out.txt"]}, ident=4
    )
    payload = dict(evidence[2].payload)
    payload["commit_paths"] = ["src/ledger/a.py", "infrastructure/out.txt"]
    evidence[2] = _ev("bundle_head", payload, ident=3)
    outcome = evaluate_gate(GateName.SCOPE_CONTAINED, _gi(evidence))
    assert outcome.result is GateResult.FAIL and "infrastructure/out.txt" in outcome.detail


def test_scope_contained_fails_on_a_prohibited_path() -> None:
    contract = contract_document()
    contract["scope"]["allowed_paths"] = ["**"]
    contract["scope"]["prohibited_paths"] = [".github/**"]
    evidence = _passing_evidence()
    evidence[3] = _ev("diff_paths", {"paths": [".github/workflows/ci.yml"]}, ident=4)
    payload = dict(evidence[2].payload)
    payload["commit_paths"] = [".github/workflows/ci.yml"]
    evidence[2] = _ev("bundle_head", payload, ident=3)
    outcome = evaluate_gate(GateName.SCOPE_CONTAINED, _gi(evidence, contract=contract))
    assert outcome.result is GateResult.FAIL and "prohibited_paths" in outcome.detail


def test_no_injected_files_sees_the_commit_list_as_well_as_the_diff() -> None:
    evidence = _passing_evidence()
    payload = dict(evidence[2].payload)
    payload["commit_paths"] = [".crucible/identity.md"]
    evidence[2] = _ev("bundle_head", payload, ident=3)
    outcome = evaluate_gate(GateName.NO_INJECTED_FILES, _gi(evidence))
    assert outcome.result is GateResult.FAIL and ".crucible/identity.md" in outcome.detail


def test_no_secrets_reports_the_pattern_not_the_value() -> None:
    evidence = _passing_evidence()
    evidence[4] = _ev(
        "scanner_result",
        {
            "findings": [{"where": "diff", "pattern": "github_token"}],
            "scanned": ["diff"],
            "diff_scanned": True,
        },
        ident=5,
    )
    outcome = evaluate_gate(GateName.NO_SECRETS, _gi(evidence))
    assert outcome.result is GateResult.FAIL
    assert "github_token" in outcome.detail and "ghp_" not in outcome.detail


def test_no_secrets_will_not_pass_without_the_diff_content() -> None:
    """11 wants the scanner over the diff; an empty finding list is not coverage."""
    evidence = _passing_evidence()
    evidence[4] = _ev(
        "scanner_result", {"findings": [], "scanned": ["report"], "diff_scanned": False}, ident=5
    )
    outcome = evaluate_gate(GateName.NO_SECRETS, _gi(evidence))
    assert outcome.result is GateResult.PENDING
    assert COLLECTOR_MARKER in outcome.detail


def test_no_secrets_will_not_pass_on_an_empty_scanned_list() -> None:
    evidence = _passing_evidence()
    evidence[4] = _ev(
        "scanner_result", {"findings": [], "scanned": [], "diff_scanned": True}, ident=5
    )
    assert evaluate_gate(GateName.NO_SECRETS, _gi(evidence)).result is GateResult.PENDING


def test_no_injected_files_needs_the_commit_list() -> None:
    """11 wants the diff and every commit on work_branch, not the diff alone."""
    evidence = [e for e in _passing_evidence() if e.kind != "bundle_head"]
    assert evaluate_gate(GateName.NO_INJECTED_FILES, _gi(evidence)).result is GateResult.FAIL


def test_commits_present_takes_the_head_from_the_bundle_not_the_report() -> None:
    """hades #187: HT-0002 reported a head it did not end on. The collected bundle is the
    head; a report that names another one, or none, is noted and does not fail."""
    evidence = _passing_evidence()
    for claimed, note in (
        (None, "named no head_sha"),
        ("9" * 40, "named 999999999999, which is not the collected head"),
        ("not a hash at all", "named another value, which is not the collected head"),
    ):
        payload = dict(evidence[2].payload)
        payload["claimed_head_sha"] = claimed
        evidence[2] = _ev("bundle_head", payload, ident=3)
        outcome = evaluate_gate(GateName.COMMITS_PRESENT, _gi(evidence))
        assert outcome.result is GateResult.PASS, outcome.detail
        assert HEAD in outcome.detail and note in outcome.detail


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("src/*", "src/a.py", True),
        ("src/*", "src/a/b.py", False),
        ("src/**", "src/a/b/c.py", True),
        ("src/**/*.py", "src/a/b.py", True),
        ("src/ledger/**", "src/ledger/import.py", True),
        ("src/ledger/**", "src/other/import.py", False),
        ("docs/ledger.md", "docs/ledger.md", True),
        ("docs/ledger.md", "docs/ledger.md.bak", False),
    ],
)
def test_a_single_star_does_not_cross_a_separator(pattern: str, path: str, matches: bool) -> None:
    """`src/*` naming everything under src would widen every contract silently."""
    contract = contract_document()
    contract["scope"] = {
        "allowed_paths": [pattern],
        "prohibited_paths": [],
        "may_add_dependencies": False,
        "may_modify_ci": False,
    }
    evidence = _passing_evidence()
    evidence[3] = _ev("diff_paths", {"paths": [path]}, ident=4)
    bundle = dict(evidence[2].payload)
    bundle["commit_paths"] = [path]
    evidence[2] = _ev("bundle_head", bundle, ident=3)
    outcome = evaluate_gate(GateName.SCOPE_CONTAINED, _gi(evidence, contract=contract))
    assert (outcome.result is GateResult.PASS) is matches


def test_an_uploaded_artifact_satisfies_run_evidence_present() -> None:
    """An orchestrator upload names the contract's path; the bytes live at a digest.
    The gate compares the name, or an upload could never satisfy it."""
    evidence = [e for e in _passing_evidence() if e.payload.get("role") != "run_evidence"]
    evidence.append(
        _ev(
            "artifact_present",
            {
                "role": "run_evidence",
                "path": "report/run-evidence.md",
                "stored_at": "blobs/ab/cd/" + "e" * 64,
                "size": 48,
                "uploaded_by": "foundry",
            },
            ident=9,
        )
    )
    outcome = evaluate_gate(GateName.RUN_EVIDENCE_PRESENT, _gi(evidence))
    assert outcome.result is GateResult.PASS
    assert outcome.evidence_ids == (9,)


def test_a_stored_path_alone_does_not_satisfy_run_evidence_present() -> None:
    """The content-addressed path is not the name the contract asked for."""
    evidence = [e for e in _passing_evidence() if e.payload.get("role") != "run_evidence"]
    evidence.append(
        _ev(
            "artifact_present",
            {"role": "run_evidence", "path": "blobs/ab/cd/" + "e" * 64, "size": 48},
            ident=9,
        )
    )
    outcome = evaluate_gate(GateName.RUN_EVIDENCE_PRESENT, _gi(evidence))
    assert outcome.result is GateResult.FAIL and "report/run-evidence.md" in outcome.detail


def test_run_evidence_matches_the_path_not_the_basename() -> None:
    evidence = _passing_evidence()
    evidence[5] = _ev(
        "artifact_present",
        {"role": "run_evidence", "path": "somewhere/else/run-evidence.md", "size": 42},
        ident=6,
    )
    outcome = evaluate_gate(GateName.RUN_EVIDENCE_PRESENT, _gi(evidence))
    assert outcome.result is GateResult.FAIL and "report/run-evidence.md" in outcome.detail


def test_run_evidence_present_fails_when_missing_or_empty() -> None:
    evidence = _passing_evidence()
    del evidence[5]
    assert evaluate_gate(GateName.RUN_EVIDENCE_PRESENT, _gi(evidence)).result is GateResult.FAIL
    evidence.append(
        _ev(
            "artifact_present",
            {"role": "run_evidence", "path": "report/run-evidence.md", "size": 0},
            ident=8,
        )
    )
    outcome = evaluate_gate(GateName.RUN_EVIDENCE_PRESENT, _gi(evidence))
    assert outcome.result is GateResult.FAIL and "empty" in outcome.detail


def test_run_evidence_present_is_skipped_without_an_artifact_verification() -> None:
    contract = contract_document()
    contract["required_verification"] = [
        v for v in contract["required_verification"] if "path" not in v
    ]
    outcome = evaluate_gate(
        GateName.RUN_EVIDENCE_PRESENT, _gi(_passing_evidence(), contract=contract)
    )
    assert outcome.result is GateResult.SKIPPED


def test_criteria_mapped_fails_on_an_unmapped_criterion() -> None:
    evidence = _passing_evidence()
    evidence[1] = _ev(
        "artifact_present",
        _claim_payload(mapped_criteria=[{"id": "AC1", "status": "met"}]),
        ident=2,
    )
    outcome = evaluate_gate(GateName.CRITERIA_MAPPED, _gi(evidence))
    assert outcome.result is GateResult.FAIL and "AC2" in outcome.detail


def test_dependencies_and_ci_gates_respect_the_contract_flags() -> None:
    evidence = _passing_evidence()
    evidence[3] = _ev("diff_paths", {"paths": ["uv.lock", ".github/workflows/ci.yml"]}, ident=4)
    assert evaluate_gate(GateName.DEPENDENCIES_UNCHANGED, _gi(evidence)).result is GateResult.FAIL
    assert evaluate_gate(GateName.CI_UNCHANGED, _gi(evidence)).result is GateResult.FAIL
    permissive = contract_document()
    permissive["scope"]["may_add_dependencies"] = True
    permissive["scope"]["may_modify_ci"] = True
    gi = _gi(evidence, contract=permissive)
    assert evaluate_gate(GateName.DEPENDENCIES_UNCHANGED, gi).result is GateResult.SKIPPED
    assert evaluate_gate(GateName.CI_UNCHANGED, gi).result is GateResult.SKIPPED


def test_internal_review_gate_is_retired() -> None:
    evidence = [e for e in _passing_evidence() if e.kind != "review_received"]
    assert (
        evaluate_gate(GateName.INTERNAL_REVIEW_RECORDED, _gi(evidence)).result is GateResult.SKIPPED
    )
    assert (
        evaluate_gate(
            GateName.INTERNAL_REVIEW_RECORDED, _gi(evidence, internal_review_required=False)
        ).result
        is GateResult.SKIPPED
    )
    assert (
        evaluate_gate(GateName.INTERNAL_REVIEW_RECORDED, _gi(_passing_evidence())).result
        is GateResult.SKIPPED
    )


def test_internal_review_gate_ignores_the_author_and_another_head() -> None:
    base = [e for e in _passing_evidence() if e.kind != "review_received"]
    author = [
        *base,
        _ev(
            "verification_run",
            {"id": "V1", "command": "make lint", "exit_code": 0, "ran": True},
            ident=8,
        ),
        _ev(
            "verification_run",
            {"id": "V2", "command": "make test", "exit_code": 0, "ran": True},
            ident=9,
        ),
        _ev(
            "verification_run",
            {"id": "V3", "command": "make scan", "exit_code": 0, "ran": True},
            ident=10,
        ),
        _ev("workspace_state", {"checked": True, "leftover": []}, ident=11),
        _ev(
            "review_received",
            {"reviewed_head_sha": HEAD, "reviewer_is_author": True, "verdict": "approve"},
            ident=7,
        ),
    ]
    assert (
        evaluate_gate(GateName.INTERNAL_REVIEW_RECORDED, _gi(author)).result is GateResult.SKIPPED
    )
    other_head = [
        *base,
        _ev(
            "verification_run",
            {"id": "V1", "command": "make lint", "exit_code": 0, "ran": True},
            ident=8,
        ),
        _ev(
            "verification_run",
            {"id": "V2", "command": "make test", "exit_code": 0, "ran": True},
            ident=9,
        ),
        _ev(
            "verification_run",
            {"id": "V3", "command": "make scan", "exit_code": 0, "ran": True},
            ident=10,
        ),
        _ev("workspace_state", {"checked": True, "leftover": []}, ident=11),
        _ev(
            "review_received",
            {"reviewed_head_sha": "b" * 40, "reviewer_is_author": False, "verdict": "approve"},
            ident=7,
        ),
    ]
    assert (
        evaluate_gate(GateName.INTERNAL_REVIEW_RECORDED, _gi(other_head)).result
        is GateResult.SKIPPED
    )


def test_an_evaluator_that_raises_is_error_not_an_exception() -> None:
    broken = _gi([_ev("diff_paths", {"paths": [None]}, ident=4)], contract={"scope": None})
    outcome = evaluate_gate(GateName.SCOPE_CONTAINED, broken)
    assert outcome.result is GateResult.ERROR
    assert blocking({GateName.SCOPE_CONTAINED: outcome}) == [GateName.SCOPE_CONTAINED]


def test_unknown_gate_is_error() -> None:
    assert evaluate_gate("no_such_gate", _gi([])).result is GateResult.ERROR


def test_configured_pre_pr_gates_reads_the_policy() -> None:
    """05b: the policy names the required set. An explicit empty list is an empty set."""
    from crucible.application.gates import configured_pre_pr_gates  # noqa: PLC0415

    enforced = sorted(ENFORCED_PRE_PR_GATES)
    assert configured_pre_pr_gates({}) == sorted(PRE_PR_GATES) + sorted(
        ENFORCED_PRE_PR_GATES - PRE_PR_GATES
    )
    assert configured_pre_pr_gates({"gates": {}}) == sorted(PRE_PR_GATES) + sorted(
        ENFORCED_PRE_PR_GATES - PRE_PR_GATES
    )
    assert configured_pre_pr_gates({"gates": {"pre_pr": []}}) == enforced
    narrowed = {"gates": {"pre_pr": ["exit_clean", "no_secrets"]}}
    assert configured_pre_pr_gates(narrowed) == ["exit_clean", "no_secrets", *enforced]


def test_a_narrowed_gate_set_only_runs_what_the_policy_asked_for() -> None:
    """A gate a policy moved out of pre_pr is not silently evaluated anyway."""
    outcomes = evaluate_pre_pr(["exit_clean"], _gi(_passing_evidence()))
    assert set(outcomes) == {"exit_clean"}


# ----- commit_policy (hades FDY-0135, FDY-0143) -----------------------------------------


def _with_commit_policy(check: dict[str, Any] | None) -> GateInput:
    evidence = _passing_evidence()
    payload = dict(evidence[2].payload)
    if check is None:
        payload.pop("commit_policy")
    else:
        payload["commit_policy"] = check
    evidence[2] = _ev("bundle_head", payload, ident=3)
    policy = {"git": {"author_email": "worker@example.test", "commit_trailer": "Crucible-Attempt"}}
    return _gi(evidence, policy=policy)


def test_commit_policy_passes_commits_by_the_policy_author() -> None:
    outcome = evaluate_gate(
        GateName.COMMIT_POLICY, _with_commit_policy({"checked": True, "author_problems": []})
    )
    assert outcome.result is GateResult.PASS
    assert outcome.detail == "every commit is authored as worker@example.test"
    assert outcome.evidence_ids == (3,)


def test_commit_policy_never_fails_on_a_missing_trailer() -> None:
    """FDY-0143: the trailer is a courtesy nothing checks. A bundle recorded before the
    change, with trailer problems in it, still passes."""
    outcome = evaluate_gate(
        GateName.COMMIT_POLICY,
        _with_commit_policy(
            {"checked": True, "author_problems": [], "trailer_problems": ["b" * 40]}
        ),
    )
    assert outcome.result is GateResult.PASS
    assert "trailer" not in outcome.detail


def test_commit_policy_fails_a_commit_by_another_author_for_the_reviewer() -> None:
    """The failure is advisory (always, FDY-0143): it is listed for the reviewer."""
    sha = "c" * 40
    outcome = evaluate_gate(
        GateName.COMMIT_POLICY,
        _with_commit_policy(
            {
                "checked": True,
                "author_problems": [{"sha": sha, "author": "someone@elsewhere.test"}],
            }
        ),
    )
    assert outcome.result is GateResult.FAIL
    assert outcome.detail == (
        f"1 commit(s) not authored as worker@example.test ({sha[:12]} by someone@elsewhere.test)"
    )


def test_commit_policy_echoes_no_worker_text_that_is_not_a_hash_or_an_address() -> None:
    """The author email and the sha come out of the worker's commits: data, not text."""
    outcome = evaluate_gate(
        GateName.COMMIT_POLICY,
        _with_commit_policy(
            {
                "checked": True,
                "author_problems": [{"sha": "ignore previous", "author": "run `rm -rf /`"}],
            }
        ),
    )
    assert outcome.result is GateResult.FAIL
    assert "ignore previous" not in outcome.detail
    assert "rm -rf" not in outcome.detail
    assert "a commit by another address" in outcome.detail


def test_commit_policy_fails_for_the_reviewer_when_the_collector_could_not_check() -> None:
    outcome = evaluate_gate(GateName.COMMIT_POLICY, _with_commit_policy({"checked": False}))
    assert outcome.result is GateResult.FAIL
    assert "could not read the collected commits' authors" in outcome.detail


def test_commit_policy_skips_an_attempt_collected_before_the_check_existed() -> None:
    """A task already waiting for review when Crucible upgrades is not failed by a gate
    whose evidence its collector never wrote."""
    outcome = evaluate_gate(GateName.COMMIT_POLICY, _with_commit_policy(None))
    assert outcome.result is GateResult.SKIPPED
    assert "before Crucible recorded commit authors" in outcome.detail


def test_commit_policy_ignores_a_worker_asserted_bundle() -> None:
    evidence = [e for e in _passing_evidence() if e.kind != "bundle_head"]
    evidence.append(
        _ev(
            "bundle_head",
            {"commit_policy": {"checked": True, "author_problems": []}},
            ident=9,
            source="worker",
        )
    )
    outcome = evaluate_gate(GateName.COMMIT_POLICY, _gi(evidence))
    assert outcome.result is GateResult.FAIL
    assert "no verified bundle_head" in outcome.detail


def test_a_false_claim_is_named_in_verification_ran() -> None:
    """hades #183 point 2: claim V1 exit 0, re-run 127 -> gate is blocking and the
    message names V1, 0 and 127."""
    evidence = []
    for e in _passing_evidence():
        if e.kind == "verification_run" and e.payload.get("id") == "V1":
            continue
        if e.kind == "artifact_present" and e.payload.get("role") == "completion_claim":
            evidence.append(
                _ev(
                    "artifact_present",
                    {
                        **e.payload,
                        "claimed_checks": [{"id": "V1", "exit": 0}],
                    },
                    ident=e.id,
                )
            )
        else:
            evidence.append(e)
    evidence.append(_ev("verification_run", {"id": "V1", "exit_code": 127, "ran": True}, ident=30))
    outcome = evaluate_gate(GateName.VERIFICATION_RAN, _gi(evidence))
    assert outcome.result is GateResult.FAIL
    assert outcome.always_blocks is True
    assert "V1" in outcome.detail
    assert "exit 0" in outcome.detail
    assert "127" in outcome.detail


def test_a_boolean_exit_is_not_a_false_claim() -> None:
    """Codex PR 272: a claimed exit that is a bool (False == 0) is not treated as
    an integer exit 0, so it cannot be a false claim. Claim V1 exit False, re-run 127:
    the gate may still fail because V1 exited 127, but not because of a false claim."""
    evidence = []
    for e in _passing_evidence():
        if e.kind == "verification_run" and e.payload.get("id") == "V1":
            continue
        if e.kind == "artifact_present" and e.payload.get("role") == "completion_claim":
            evidence.append(
                _ev(
                    "artifact_present",
                    {
                        **e.payload,
                        "claimed_checks": [{"id": "V1", "exit": False}],
                    },
                    ident=e.id,
                )
            )
        else:
            evidence.append(e)
    evidence.append(_ev("verification_run", {"id": "V1", "exit_code": 127, "ran": True}, ident=31))
    outcome = evaluate_gate(GateName.VERIFICATION_RAN, _gi(evidence))
    assert outcome.result is GateResult.FAIL
    assert outcome.always_blocks is False
    # The gate fails because V1 exited 127, not because of a false claim.
    assert "the worker reported" not in outcome.detail


def test_matching_exits_are_not_a_false_claim() -> None:
    """hades #183 point 2: when the claim matches the rerun exit, there is no false
    claim and the gate passes."""
    evidence = [e for e in _passing_evidence() if e.kind != "verification_run"]
    # Add all verification runs with exit 0
    for run_id, command in [("V1", "make lint"), ("V2", "make test"), ("V3", "make scan")]:
        evidence.append(
            _ev(
                "verification_run",
                {"id": run_id, "command": command, "exit_code": 0, "ran": True},
                ident=id(run_id),
            )
        )
    outcome = evaluate_gate(GateName.VERIFICATION_RAN, _gi(evidence))
    assert outcome.result is GateResult.PASS
