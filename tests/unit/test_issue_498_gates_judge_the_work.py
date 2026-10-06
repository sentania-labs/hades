"""Issue 498: work gates, composed completion records, budget ends, and Qwen parity."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from crucible.adapters.harness.qwen_code import QwenCodeAdapter
from crucible.application.completion_record import compose_completion_record
from crucible.contracts.completion_claim import parse_claim
from crucible.domain.entities import Task
from crucible.domain.exit_class import ExitClass, classify_exit
from crucible.domain.gates import GateName, GateResult, advisory_gates, report_present
from crucible.ports.execution import BranchBundle, CollectedOutputs, VerificationRun
from crucible.ports.harness import LaunchContext
from tests.fixtures import contract_document
from tests.unit.test_gates import _gi

ROOT = Path(__file__).resolve().parents[2]


def test_report_gaps_are_advisory_and_clean_text_only_exit_is_completed() -> None:
    outcome = report_present(_gi([]))
    assert outcome.result is GateResult.FAIL
    assert not outcome.always_blocks
    assert GateName.REPORT_PRESENT in advisory_gates({"gates": {"advisory": []}})
    assert classify_exit(exit_code=0, report_present=False, blocked_present=False) is (
        ExitClass.COMPLETED
    )


def test_hades_composes_branch_checks_findings_and_worker_report() -> None:
    contract = contract_document()
    contract["correction"] = {
        "addresses": [
            {"kind": "review_comment", "id": "one"},
            {"kind": "review_comment", "id": "two"},
        ]
    }
    uow = MagicMock()
    uow.review_comments.get.side_effect = lambda ident: SimpleNamespace(
        path="src/touched.py" if ident == "one" else "src/untouched.py"
    )
    task = MagicMock(spec=Task, external_id="FDY-0486")
    outputs = CollectedOutputs(
        report=None,
        report_raw=None,
        blocked_md=None,
        diff_paths=("src/touched.py",),
        bundle=BranchBundle("a" * 40, "main", "work", 2, True),
        verifications=(VerificationRun("V1", "make lint", 0, 0, "ok"),),
    )
    record = compose_completion_record(
        uow,
        task=task,
        contract=contract,
        outputs=outputs,
        worker_report={"summary": "Worker context", "risks": ["known"]},
        worker_report_parsed=False,
        worker_report_errors=[{"loc": ["finding_dispositions"], "msg": "missing"}],
    )
    assert record["refs"]["commits"] == 2
    assert record["checks"][0]["exit"] == 0
    assert [item["disposition"] for item in record["finding_dispositions"]] == [
        "addressed",
        "not addressed",
    ]
    assert record["summary"] == "Worker context"
    assert record["worker_report"]["parse_errors"]


def test_qwen_launch_is_bounded_and_wrapper_writes_fallback_report(tmp_path: Path) -> None:
    launch = QwenCodeAdapter().build_launch(
        LaunchContext(
            attempt_id="attempt",
            model="model",
            effort=None,
            timeout_seconds=10,
            identity_mount="/identity",
            report_mount="/report",
            repo_mount="/repo",
            endpoint="local",
            endpoint_url="http://gateway/v1",
        )
    )
    assert "--safe-mode" in launch.argv and "--allowed-tools" in launch.argv
    assert launch.env["CRUCIBLE_QWEN_MAX_OUTPUT_TOKENS"] == "32000"

    spec = importlib.util.spec_from_file_location(
        "issue_498_qwen_wrapper", ROOT / "images/worker/crucible-qwen-code.py"
    )
    assert spec is not None and spec.loader is not None
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    identity = tmp_path / "IDENTITY.md"
    identity.write_text("Acceptance criteria: `AC1`", encoding="utf-8")
    report_dir = tmp_path / "report"
    wrapper.write_minimal_report(report_dir, identity, 0)
    document = json.loads((report_dir / "report.yaml").read_text(encoding="utf-8"))
    claim, errors = parse_claim(document, criteria=["AC1"])
    assert claim is not None and errors == []


def test_today_valid_worker_report_remains_valid() -> None:
    document = {
        "schema_version": "1.0",
        "summary": "Done",
        "self_review": {
            "documentation": ["No documentation change needed."],
            "acceptance_criteria": [{"id": "AC1", "status": "met", "evidence": "test"}],
            "omissions": [],
        },
        "acceptance_mapping": [{"id": "AC1", "status": "met", "evidence": "test"}],
        "proposed_pull_request": {"title": "Done", "body": "Done"},
        "limitations": [],
        "risks": [],
        "blockers": [],
        "follow_ups": [],
    }
    assert parse_claim(document, criteria=["AC1"])[1] == []
