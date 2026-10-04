"""Hades #401: correction reports carry one disposition per Codex finding."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from crucible.contracts.completion_claim import parse_claim

ROOT = Path(__file__).resolve().parents[2]


def _report(dispositions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "summary": "Corrected the review findings.",
        "acceptance_mapping": [
            {"id": "AC1", "status": "met", "evidence": "covered by the correction"}
        ],
        "proposed_pull_request": {"title": "Apply Codex findings", "body": "Done."},
        "limitations": [],
        "risks": [],
        "blockers": [],
        "follow_ups": [],
        "finding_dispositions": dispositions,
    }


def test_completion_claim_accepts_fixed_and_declined_findings() -> None:
    claim, errors = parse_claim(
        _report(
            [
                {
                    "review_comment_id": "finding-1",
                    "disposition": "fixed",
                    "commit": "a" * 40,
                },
                {
                    "review_comment_id": "finding-2",
                    "disposition": "declined",
                    "reason": "The suggested branch is unreachable by contract.",
                },
            ]
        )
    )
    assert errors == []
    assert claim is not None
    assert [item.disposition for item in claim.finding_dispositions] == ["fixed", "declined"]


def test_completion_claim_requires_disposition_evidence() -> None:
    claim, errors = parse_claim(
        _report([{"review_comment_id": "finding-1", "disposition": "declined"}])
    )
    assert claim is None
    assert any("finding_dispositions" in error["loc"] for error in errors)


def test_worker_report_checker_mirrors_finding_dispositions() -> None:
    script = ROOT / "images/worker/crucible-report.py"
    spec = importlib.util.spec_from_file_location("worker_report_issue_401", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    problems = module.check(
        _report(
            [
                {
                    "review_comment_id": "finding-1",
                    "disposition": "fixed",
                    "commit": "a" * 40,
                }
            ]
        ),
        criteria=["AC1"],
    )
    assert problems == []
