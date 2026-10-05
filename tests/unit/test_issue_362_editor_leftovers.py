"""Pre-PR gate editor_leftovers: editor and merge leftovers fail the gate (issue 362).

Checks for paths matching editor/merge leftover patterns in ``diff_paths`` evidence:
*.bak, *.orig, *.rej, *~, .*.swp, .#*.

Acceptance criteria:
  AC1  A diff adding a *.bak file fails and names it.
  AC2  A diff with only ordinary paths passes.
  AC3  The failure blocks even advisory scope_contained.
"""

from __future__ import annotations

from typing import Any

from crucible.domain.gates import (
    EDITOR_LEFTOVERS_PATTERN,
    ENFORCED_PRE_PR_GATES,
    PRE_PR_GATES,
    EvidenceItem,
    GateInput,
    GateName,
    GateResult,
    blocking,
    evaluate_gate,
    evaluate_pre_pr,
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


def _gi(evidence: list[EvidenceItem], **kw: Any) -> GateInput:
    return GateInput(
        contract=kw.pop("contract", contract_document()),
        policy=kw.pop("policy", {}),
        head_sha=kw.pop("head_sha", HEAD),
        evidence=tuple(evidence),
        internal_review_required=kw.pop("internal_review_required", True),
    )


# ---------------------------------------------------------------------------
# EDITOR_LEFTOVERS_PATTERN helper
# ---------------------------------------------------------------------------


def _pattern_matches(path: str) -> bool:
    """Return True when *path* matches an editor or merge leftover name."""
    return EDITOR_LEFTOVERS_PATTERN.search(path) is not None


def test_bak_extension_is_detected() -> None:
    assert _pattern_matches("src/app.py.bak")


def test_orig_extension_is_detected() -> None:
    assert _pattern_matches("src/lib.py.orig")


def test_rej_extension_is_detected() -> None:
    assert _pattern_matches("patch.diff.rej")


def test_tilde_trailing_is_detected() -> None:
    assert _pattern_matches("src/file.py~")


def test_swp_in_path_is_detected() -> None:
    assert _pattern_matches(".file.py.swp")
    assert _pattern_matches("src/.file.py.swp")


def test_dot_hash_prefix_is_detected() -> None:
    assert _pattern_matches(".#file.py")
    assert _pattern_matches(".#lock")


def test_normal_path_is_not_detected() -> None:
    assert not _pattern_matches("src/main.py")
    assert not _pattern_matches("docs/readme.md")
    assert not _pattern_matches("tests/test_app.py")


def test_dot_not_at_start_is_not_detected() -> None:
    assert not _pattern_matches("file.lock")
    assert not _pattern_matches(".foo")


# ---------------------------------------------------------------------------
# editor_leftovers gate function
# ---------------------------------------------------------------------------


def test_ac1_bak_file_fails_and_lists_path() -> None:
    """A diff adding a .bak file fails the gate and names it."""
    ev = _ev(
        "diff_paths", {"paths": ["src/ledger/app.py", "crucible/application/acceptance.py.bak"]}
    )
    gi = _gi([ev])
    outcome = evaluate_gate(GateName.EDITOR_LEFTOVERS, gi)
    assert outcome.result is GateResult.FAIL
    assert "crucible/application/acceptance.py.bak" in outcome.detail


def test_ac2_ordinary_paths_pass() -> None:
    """A diff with only normal paths passes."""
    ev = _ev("diff_paths", {"paths": ["src/ledger/app.py", "tests/test_app.py"]})
    gi = _gi([ev])
    outcome = evaluate_gate(GateName.EDITOR_LEFTOVERS, gi)
    assert outcome.result is GateResult.PASS


def test_no_diff_paths_evidence_fails() -> None:
    """Missing diff_paths evidence fails (not waits)."""
    ev_exit = _ev("exit_info", {"exit_code": 0, "exit_class": "completed"}, ident=2)
    gi = _gi([ev_exit])
    outcome = evaluate_gate(GateName.EDITOR_LEFTOVERS, gi)
    assert outcome.result is GateResult.FAIL
    assert "no verified diff_paths evidence" in outcome.detail


def test_empty_paths_passes() -> None:
    """An empty diff_paths list passes."""
    ev = _ev("diff_paths", {"paths": []})
    gi = _gi([ev])
    outcome = evaluate_gate(GateName.EDITOR_LEFTOVERS, gi)
    assert outcome.result is GateResult.PASS


def test_ac3_always_blocks_editor_leftover() -> None:
    """Editor leftovers always block, even advisory scope_contained."""
    ev = _ev(
        "diff_paths",
        {"paths": ["src/ledger/app.py.bak", "tests/test.py"]},
    )
    gi = _gi([ev])
    outcome = evaluate_gate(GateName.EDITOR_LEFTOVERS, gi)
    assert outcome.always_blocks is True


def test_editor_leftover_in_full_gate_run_blocks() -> None:
    """A diff with a .bak file blocks when running all pre-PR gates."""
    ev_diff = _ev("diff_paths", {"paths": ["src/ledger/app.py.bak"]})
    ev_exit = _ev("exit_info", {"exit_code": 0, "exit_class": "completed"}, ident=2)
    ev_report = _ev(
        "artifact_present",
        {
            "role": "completion_claim",
            "parsed_ok": True,
            "self_review_checked": True,
            "parse_errors": [],
            "claimed_head_sha": HEAD,
            "mapped_criteria": [{"id": "AC1", "status": "met"}],
            "run_evidence": ["report/run-evidence.md"],
            "changed_files": ["src/ledger/app.py.bak"],
        },
        ident=3,
    )
    ev_bundle = _ev(
        "bundle_head",
        {
            "head_sha": HEAD,
            "claimed_head_sha": HEAD,
            "commits": 1,
            "bundle_verified": True,
            "commit_paths": ["src/ledger/app.py.bak"],
            "commit_messages": ["add a change"],
            "commit_policy": {"checked": True, "author_problems": []},
        },
        ident=4,
    )
    ev_secrets = _ev(
        "scanner_result",
        {"findings": [], "scanned": ["diff"], "diff_scanned": True},
        ident=5,
    )
    ev_verify = _ev(
        "verification_run",
        {"id": "V1", "command": "make lint", "exit_code": 0, "ran": True},
        ident=6,
    )
    ev_evidence = _ev(
        "artifact_present",
        {"role": "run_evidence", "path": "report/run-evidence.md", "size": 42},
        ident=7,
    )
    evidence = [ev_exit, ev_report, ev_bundle, ev_secrets, ev_verify, ev_evidence, ev_diff]
    gi = _gi(evidence)
    all_gates = sorted(PRE_PR_GATES | ENFORCED_PRE_PR_GATES)
    outcomes = evaluate_pre_pr(all_gates, gi)
    # Verify editor_leftovers gate fails and blocks
    el_outcome = outcomes.get(GateName.EDITOR_LEFTOVERS)
    assert el_outcome is not None
    assert el_outcome.result is GateResult.FAIL
    assert el_outcome.always_blocks is True
    # blocking() should include editor_leftovers
    blocks = blocking(outcomes)
    assert GateName.EDITOR_LEFTOVERS in blocks


def test_multiple_leftovers_all_listed() -> None:
    ev = _ev(
        "diff_paths",
        {
            "paths": [
                "src/ledger/app.py.bak",
                "docs/guide.md~",
                "tests/test.patch.rej",
            ]
        },
    )
    gi = _gi([ev])
    outcome = evaluate_gate(GateName.EDITOR_LEFTOVERS, gi)
    assert outcome.result is GateResult.FAIL
    for path in ["docs/guide.md~", "src/ledger/app.py.bak", "tests/test.patch.rej"]:
        assert path in outcome.detail


def test_swp_file_in_nested_dir() -> None:
    ev = _ev("diff_paths", {"paths": ["src/ledger/.config.py.swp"]})
    gi = _gi([ev])
    outcome = evaluate_gate(GateName.EDITOR_LEFTOVERS, gi)
    assert outcome.result is GateResult.FAIL
    assert ".config.py.swp" in outcome.detail
