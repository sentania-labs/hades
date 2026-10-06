"""Hades-composed completion records (#498).

The worker report is optional commentary. The branch, verifier runs, and correction
coverage are facts Hades already collected and are therefore the record's foundation.
"""

from __future__ import annotations

from typing import Any

from crucible.domain.entities import Task
from crucible.ports.execution import CollectedOutputs
from crucible.ports.repository import UnitOfWork


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def compose_completion_record(
    uow: UnitOfWork,
    *,
    task: Task,
    contract: dict[str, Any],
    outputs: CollectedOutputs,
    worker_report: dict[str, Any] | None,
    worker_report_parsed: bool,
    worker_report_errors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compose the durable reviewer/publication record from collected evidence."""
    report = worker_report or {}
    bundle = outputs.bundle
    head = bundle.head_sha if bundle is not None else ""
    objective = str(contract.get("objective") or task.external_id)
    mappings = report.get("acceptance_mapping")
    if not isinstance(mappings, list):
        mappings = [
            {
                "id": str(criterion.get("id", "")),
                "status": "not_exercised",
                "evidence": "the worker report did not provide a mapping",
            }
            for criterion in contract.get("acceptance_criteria", [])
        ]

    dispositions: list[dict[str, Any]] = []
    correction = contract.get("correction") or {}
    changed = set(outputs.diff_paths)
    for address in correction.get("addresses", []):
        if not isinstance(address, dict) or address.get("kind") != "review_comment":
            continue
        finding_id = str(address.get("id", ""))
        comment = uow.review_comments.get(finding_id)
        path = str(getattr(comment, "path", "") or address.get("path") or "")
        addressed = bool(path and path in changed and head)
        dispositions.append(
            {
                "review_comment_id": finding_id,
                "path": path,
                "disposition": "addressed" if addressed else "not addressed",
                **({"commit": head} if addressed else {}),
            }
        )

    proposed = report.get("proposed_pull_request")
    if not isinstance(proposed, dict):
        proposed = {}
    title = proposed.get("title")
    if not isinstance(title, str) or not title.strip():
        title = f"{task.external_id}: {objective}"[:120]
    body = proposed.get("body")
    if not isinstance(body, str):
        body = str(report.get("summary") or objective)

    return {
        "schema_version": "1.0",
        "task_external_id": task.external_id,
        "summary": str(report.get("summary") or "Hades collected completed branch work."),
        "changed_files": list(outputs.diff_paths),
        "refs": (
            {
                "branch": bundle.work_branch,
                "head_sha": bundle.head_sha,
                "commits": bundle.commits,
            }
            if bundle is not None
            else None
        ),
        "checks": [
            {
                "id": run.id,
                "command": run.command,
                "exit": run.exit_code,
                "expected_exit": run.expect_exit,
                "ran": run.ran,
                "log": f"verify/{run.id}.log",
            }
            for run in outputs.verifications
        ],
        "acceptance_mapping": mappings,
        "proposed_pull_request": {"title": title, "body": body},
        "limitations": _string_list(report.get("limitations")),
        "risks": _string_list(report.get("risks")),
        "blockers": _string_list(report.get("blockers")),
        "follow_ups": _string_list(report.get("follow_ups")),
        "finding_dispositions": dispositions,
        "worker_report": {
            "present": worker_report is not None,
            "parsed": worker_report_parsed,
            "parse_errors": worker_report_errors,
            **({"document": worker_report} if worker_report is not None else {}),
        },
    }
