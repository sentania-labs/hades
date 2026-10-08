"""hades #215: the worker writes judgement, Crucible derives facts. A report with only
the judgement fields passes `report_present` and `criteria_mapped` once Crucible has
filled the rest from its own evidence, and a fact the worker wrote differently is noted
in the gate's detail, never failed on."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from crucible.contracts.completion_claim import FACT_FIELDS
from crucible.domain.gates import GateName
from tests.integration.conftest import (
    ARTIFACTS_DELIVERABLE,
    run_to_settled,
    submit_and_start,
)

pytestmark = pytest.mark.integration


def judgement_only() -> dict[str, Any]:
    """What only the worker can write, with the mapping keyed by criterion id, the shape
    HT-0004's worker used."""
    return {
        "schema_version": "1.0",
        "summary": "Returned 409 on a duplicate import id and kept the existing tests.",
        "self_review": {
            "documentation": ["No documentation update needed for this synthetic report."],
            "acceptance_criteria": [
                {"id": "AC1", "status": "met", "evidence": "V2.log: duplicate case passes"},
                {"id": "AC2", "status": "met", "evidence": "V2.log: existing tests pass"},
            ],
            "omissions": [],
        },
        "acceptance_mapping": {
            "AC1": {"status": "met", "evidence": "V2.log: the new duplicate case passes"},
            "AC2": {"status": "met", "evidence": "V2.log: every existing import test passes"},
        },
        "proposed_pull_request": {"title": "Refuse a duplicate import id", "body": "409."},
        "limitations": [],
        "risks": [],
        "blockers": [],
        "follow_ups": [],
    }


def gate_rows(client: TestClient, task_id: str) -> tuple[str, dict[str, dict[str, Any]]]:
    attempt_id = str(client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"])
    body = client.get(f"/v1/attempts/{attempt_id}/gates").json()
    return attempt_id, {row["gate"]: row for row in body["items"]}


async def test_a_report_with_only_the_judgement_fields_passes_once_crucible_fills_the_rest(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    provider.set_report("EX-0001", judgement_only())
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "accepted"
    attempt_id, rows = gate_rows(client, task_id)
    report_present = rows[GateName.REPORT_PRESENT]
    assert report_present["result"] == "pass", report_present["detail"]
    assert (
        f"Crucible filled {', '.join(FACT_FIELDS)} from its own evidence"
        in (report_present["detail"])
    )
    assert "differs" not in report_present["detail"]
    mapped = rows[GateName.CRITERIA_MAPPED]
    assert mapped["result"] == "pass", mapped["detail"]
    assert "AC1=met, AC2=met" in mapped["detail"]

    # The stored report carries Crucible's facts and the mapping in its list form.
    view = client.get(f"/v1/tasks/{task_id}").json()
    report = client.get(f"/v1/attempts/{attempt_id}").json()["report"]
    assert report["parsed_ok"] is True
    document = report["document"]
    assert document["task_external_id"] == "EX-0001"
    assert document["refs"]["head_sha"] == view["head_sha"]
    assert document["refs"]["branch"] == "crucible/EX-0001"
    assert [c["id"] for c in document["checks"]] == ["V1", "V2", "V3"]
    assert document["run_evidence"] == ["report/run-evidence.md"]
    assert document["changed_files"]
    assert [m["id"] for m in document["acceptance_mapping"]] == ["AC1", "AC2"]


async def test_a_fact_the_worker_wrote_differently_is_noted_not_failed(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    report = judgement_only()
    report.update(
        {
            "task_external_id": "EX-0001",
            "changed_files": ["src/never-touched.py"],
            "refs": {"branch": "crucible/EX-0001", "head_sha": "a" * 40, "commits": 3},
            "checks": [
                {"id": "V1", "command": "make lint", "exit": 0, "log": "V1.log"},
                {"id": "V2", "command": "make test", "exit": 1, "log": "V2.log"},
            ],
        }
    )
    provider.set_report("EX-0001", report)
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "accepted"
    _, rows = gate_rows(client, task_id)
    detail = rows[GateName.REPORT_PRESENT]["detail"]
    assert rows[GateName.REPORT_PRESENT]["result"] == "pass", detail
    assert "Crucible filled run_evidence from its own evidence" in detail
    assert "the report differs from Crucible's evidence" in detail
    assert "changed_files (1 path listed that the collected diff does not change" in detail
    assert "head_sha aaaaaaaaaaaa is not the collected head" in detail
    assert "commits 3, collected 1" in detail
    assert "V2 reported exit 1, Crucible's re-run 0" in detail
    assert "V3 not reported" in detail
    # The worker's path is never repeated as free text.
    assert "never-touched" not in detail
    assert rows[GateName.CRITERIA_MAPPED]["result"] == "pass"


async def test_a_report_missing_judgement_fails_report_present_in_its_own_words(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    report = judgement_only()
    del report["summary"], report["limitations"]
    provider.set_report("EX-0001", report)
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    # hades #498: an incomplete report is for the reviewer, never a failed gate.
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"
    attempt_id, rows = gate_rows(client, task_id)
    assert rows[GateName.REPORT_PRESENT]["result"] == "fail"
    assert rows[GateName.REPORT_PRESENT]["classification"] == "advisory"
    errors = client.get(f"/v1/attempts/{attempt_id}").json()["report"]["parse_errors"]
    assert sorted(".".join(e["loc"]) for e in errors) == ["limitations", "summary"]


async def test_a_null_fact_is_left_out_and_filled_not_a_crash(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    """`refs: null` reads as left out: Crucible fills it, and the worker's own head is
    simply absent from commits_present's note (review of hades #215)."""
    report = judgement_only()
    report.update({"refs": None, "checks": None, "changed_files": None})
    provider.set_report("EX-0001", report)
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "accepted"
    _, rows = gate_rows(client, task_id)
    assert rows[GateName.REPORT_PRESENT]["result"] == "pass"
    assert rows[GateName.COMMITS_PRESENT]["result"] == "pass"
    assert "the report named no head_sha" in rows[GateName.COMMITS_PRESENT]["detail"]
