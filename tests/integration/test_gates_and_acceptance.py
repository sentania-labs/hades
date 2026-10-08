"""Gate acceptance publishes new runs automatically; legacy acceptance API remains covered."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.corrections import PREVIOUS_BUNDLE_GONE
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import RetentionAction
from crucible.domain.gates import DEFERRED_MARKER, DEFERRED_TO_C3, GateName
from crucible.domain.ids import new_id
from tests.integration.conftest import (
    ARTIFACTS_DELIVERABLE,
    correction_document,
    event_kinds,
    legacy_acceptance_state,
    run_to_settled,
    run_until,
    submit_and_start,
)
from tests.integration.test_report_facts import judgement_only

pytestmark = pytest.mark.integration


def gates(client: TestClient, attempt_id: str) -> dict[str, str]:
    body = client.get(f"/v1/attempts/{attempt_id}/gates").json()
    return {row["gate"]: row["result"] for row in body["items"]}


def latest_attempt(client: TestClient, task_id: str) -> str:
    return str(client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"])


async def test_pass_path_reaches_awaiting_acceptance_then_accepted(
    client: TestClient, supervisor: Supervisor
) -> None:
    """The former two-call acceptance path now accepts an artifacts task automatically."""
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "accepted"
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["head_sha"] and len(view["head_sha"]) == 40
    results = gates(client, view["latest_attempt"]["id"])
    assert results[GateName.INTERNAL_REVIEW_RECORDED] == "skipped"
    for gate in (
        GateName.REPORT_PRESENT,
        GateName.EXIT_CLEAN,
        GateName.COMMITS_PRESENT,
        GateName.SCOPE_CONTAINED,
        GateName.NO_SECRETS,
        GateName.RUN_EVIDENCE_PRESENT,
        GateName.CRITERIA_MAPPED,
        GateName.COMMIT_POLICY,
    ):
        assert results[gate] == "pass"
    assert view["acceptance_results"][0]["verdict"] == "accepted"
    kinds = event_kinds(client, task_id)
    for kind in ("gates_evaluated", "task_gates_passed", "acceptance_recorded", "task_accepted"):
        assert kind in kinds
    assert "task_awaiting_internal_review" not in kinds
    assert "task_awaiting_acceptance" not in kinds
    assert view["review_reports"] == []


async def test_deferred_gates_carry_a_marker_and_never_pass(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    rows = client.get(f"/v1/attempts/{latest_attempt(client, task_id)}/gates").json()["items"]
    deferred = [r for r in rows if r["gate"] in DEFERRED_TO_C3]
    assert len(deferred) == len(DEFERRED_TO_C3)
    for row in deferred:
        assert row["result"] == "pending" and DEFERRED_MARKER in row["detail"]


async def test_pull_request_deliverable_enters_publishing(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert await run_to_settled(supervisor, client, task_id) == "publishing"
    body = client.get(f"/v1/tasks/{task_id}").json()
    assert "task_publishing" in event_kinds(client, task_id)
    assert body["acceptance_results"][0]["verdict"] == "accepted"
    assert body["pull_request"] is None


async def test_a_report_that_contradicts_the_rerun_is_named_for_the_reviewer(
    client: TestClient, supervisor: Supervisor
) -> None:
    """ADR 0024: the fake worker reports every check passing and the verifier fails V1,
    so verification_ran blocks and the contradiction is its own named finding."""
    task_id = submit_and_start(
        client, "crucible-worker:fake-verification-fails", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["gate_summary"]["failing"] == [GateName.VERIFICATION_RAN.value]
    finding = "the worker reported V1 passing; Crucible's re-run failed it"
    assert {"gate": "verification_ran", "detail": finding} in view["gate_summary"]["for_reviewer"]
    rows = client.get(f"/v1/attempts/{view['latest_attempt']['id']}/gates").json()["items"]
    verification = next(r for r in rows if r["gate"] == GateName.VERIFICATION_RAN)
    assert verification["classification"] == "blocking"
    assert verification["findings"] == [finding]
    [wake] = [
        w
        for w in client.get("/v1/wakes").json()["items"]
        if w["reason"] == "pre_pr_gates_failed" and w["task_id"] == task_id
    ]
    assert {"gate": "verification_ran", "detail": finding} in wake["payload"]["for_reviewer"]


async def test_fail_path_scope_contained_on_a_prohibited_path(
    client: TestClient, supervisor: Supervisor
) -> None:
    """ADR 0024: scope_contained is advisory, but a prohibited path always blocks."""
    task_id = submit_and_start(client, "crucible-worker:fake-prohibited-path")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    attempt_id = latest_attempt(client, task_id)
    results = gates(client, attempt_id)
    assert results[GateName.SCOPE_CONTAINED] == "fail"
    assert results[GateName.EXIT_CLEAN] == "pass"
    assert results[GateName.REPORT_PRESENT] == "pass"
    rows = client.get(f"/v1/attempts/{attempt_id}/gates").json()["items"]
    scope = next(r for r in rows if r["gate"] == GateName.SCOPE_CONTAINED)
    assert ".github/CODEOWNERS" in scope["detail"] and "prohibited_paths" in scope["detail"]
    # The failure blocks, so this evaluation of the advisory gate is recorded blocking.
    assert scope["classification"] == "blocking"
    summary = client.get(f"/v1/tasks/{task_id}").json()["gate_summary"]
    assert summary["failing"] == [GateName.SCOPE_CONTAINED.value]
    assert summary["for_reviewer"] == []
    wakes = client.get("/v1/wakes").json()["items"]
    assert any(w["reason"] == "pre_pr_gates_failed" for w in wakes)


async def test_advisory_failures_reach_the_review_and_are_listed_for_the_reviewer(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    """Scope and the report are both advisory (ADR 0024, hades #498): each is listed
    for the reviewer with its detail, and the task waits for that review."""
    report = judgement_only()
    del report["risks"]
    provider.set_report("EX-0001", report)
    task_id = submit_and_start(
        client, "crucible-worker:fake-out-of-scope", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"
    view = client.get(f"/v1/tasks/{task_id}").json()
    summary = view["gate_summary"]
    assert summary["failing"] == []
    assert summary["classification"][GateName.SCOPE_CONTAINED] == "advisory"
    assert summary["classification"][GateName.REPORT_PRESENT] == "advisory"
    listed = {item["gate"]: item["detail"] for item in summary["for_reviewer"]}
    assert "infrastructure/outside-the-contract.txt" in listed[GateName.SCOPE_CONTAINED]
    assert "risks" in listed[GateName.REPORT_PRESENT]
    errors = client.get(f"/v1/attempts/{view['latest_attempt']['id']}").json()["report"][
        "parse_errors"
    ]
    assert [".".join(e["loc"]) for e in errors] == ["risks"]
    [wake] = [
        w
        for w in client.get("/v1/wakes").json()["items"]
        if w["reason"] == "internal_review_needed" and w["task_id"] == task_id
    ]
    assert "report_present" in wake["summary"]
    assert "scope_contained" in {i["gate"] for i in wake["payload"]["for_reviewer"]}
    assert view["acceptance_results"] == []


async def test_a_report_that_is_not_yaml_goes_to_the_reviewer_with_its_parse_error(
    client: TestClient, supervisor: Supervisor
) -> None:
    """The report's parse problem is visible to the reviewer and does not stop the
    task (hades #498)."""
    task_id = submit_and_start(
        client, "crucible-worker:fake-malformed-report", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["gate_summary"]["failing"] == []
    rows = client.get(f"/v1/attempts/{view['latest_attempt']['id']}/gates").json()["items"]
    report = next(row for row in rows if row["gate"] == GateName.REPORT_PRESENT)
    assert report["classification"] == "advisory"
    assert "report.yaml is not YAML: mapping values are not allowed here" in report["detail"]
    assert "at line 1, column 12" in report["detail"]
    assert "live run" not in report["detail"]
    assert view["acceptance_results"] == []


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_no_report_at_all_is_for_the_reviewer(
    client: TestClient, supervisor: Supervisor
) -> None:
    """hades #498: with commits on the branch and the work gates passing, a missing
    report is listed for the reviewer in plain words and never stops the task."""
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed-noreport", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"
    summary = client.get(f"/v1/tasks/{task_id}").json()["gate_summary"]
    assert summary["failing"] == []
    listed = {i["gate"]: i["detail"] for i in summary["for_reviewer"]}
    assert "Hades composed the completion record" in listed[GateName.REPORT_PRESENT]


@pytest.mark.parametrize(
    ("image", "gate"),
    [
        ("crucible-worker:fake-injected", GateName.NO_INJECTED_FILES),
        ("crucible-worker:fake-secret-leak", GateName.NO_SECRETS),
        ("crucible-worker:fake-no-commits", GateName.COMMITS_PRESENT),
        ("crucible-worker:fake-crash", GateName.EXIT_CLEAN),
    ],
)
async def test_each_fail_fixture_fails_its_gate(
    client: TestClient, supervisor: Supervisor, image: str, gate: str
) -> None:
    task_id = submit_and_start(client, image, external_id=f"EX-{gate}")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    assert gates(client, latest_attempt(client, task_id))[gate] == "fail"


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_commit_by_another_author_without_the_trailer_reaches_review(
    client: TestClient, supervisor: Supervisor
) -> None:
    """hades FDY-0143 (operator decision, 2026-09-29): commit_policy is always advisory,
    so the author difference fails it and is listed for the reviewer, and the task goes
    on to review; nothing checks the trailer."""
    task_id = submit_and_start(client, "crucible-worker:fake-other-author", external_id="HT-0007")
    assert await run_to_settled(supervisor, client, task_id) == "publishing"
    attempt_id = latest_attempt(client, task_id)
    results = gates(client, attempt_id)
    assert results[GateName.COMMIT_POLICY] == "fail"
    rows = client.get(f"/v1/attempts/{attempt_id}/gates").json()["items"]
    detail = next(r["detail"] for r in rows if r["gate"] == GateName.COMMIT_POLICY)
    view = client.get(f"/v1/tasks/{task_id}").json()
    head = view["head_sha"]
    assert detail == (
        "1 commit(s) not authored as "
        f"crucible-worker@users.noreply.github.com ({head[:12]} by someone-else@example.test)"
    )
    assert "trailer" not in detail
    assert {"gate": "commit_policy", "detail": detail} in view["gate_summary"]["for_reviewer"]
    assert "commit_policy" not in view["gate_summary"]["failing"]
    kinds = event_kinds(client, task_id)
    assert "task_pre_pr_gates_failed" not in kinds


async def test_a_secret_in_the_diff_is_never_stored(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-secret-leak")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = latest_attempt(client, task_id)
    rows = client.get(f"/v1/attempts/{attempt_id}/gates").json()["items"]
    detail = next(r["detail"] for r in rows if r["gate"] == GateName.NO_SECRETS)
    assert "github_token" in detail and "ghp_" not in detail
    for artifact in client.get(f"/v1/attempts/{attempt_id}/artifacts").json()["items"]:
        content = client.get(f"/v1/artifacts/{artifact['id']}/content").text
        assert "ghp_" not in content


async def test_correction_loop_from_pre_pr_gates_failed(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client, "crucible-worker:fake-prohibited-path", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    failed_head = client.get(f"/v1/tasks/{task_id}").json()["head_sha"]

    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "scheduled" and r.json()["contract_version"] == 2

    # Corrections use the same self-review and automatic acceptance path (09).
    assert await run_to_settled(supervisor, client, task_id) == "accepted"
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["head_sha"] != failed_head
    roles = [e["role"] for e in view["executions"]]
    assert roles == ["implement", "correct"]
    assert gates(client, latest_attempt(client, task_id))[GateName.INTERNAL_REVIEW_RECORDED] == (
        "skipped"
    )

    assert "task_correction_attached" in event_kinds(client, task_id)


async def test_a_correction_is_refused_at_attach_time_when_its_bundle_is_gone(
    client: TestClient, supervisor: Supervisor, ctx: AppContext
) -> None:
    task_id = submit_and_start(
        client, "crucible-worker:fake-prohibited-path", deliverables=ARTIFACTS_DELIVERABLE
    )
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    attempt_id = latest_attempt(client, task_id)
    assert supervisor.fenced_token is not None
    with ctx.uow_factory() as uow:
        uow.set_fenced_token(supervisor.fenced_token)
        task = uow.tasks.get(task_id)
        assert task is not None
        uow.retention.record(
            RetentionAction(
                id=new_id(),
                kind="workspace",
                subject=attempt_id,
                policy_name=task.policy_name,
                policy_version=task.policy_version,
                acted_at=ctx.clock.now(),
                detail={"reason": "test"},
            )
        )
        uow.commit()

    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    response = client.post(f"/v1/tasks/{task_id}/corrections", json=document)

    assert response.status_code == 422
    assert any(error["message"] == PREVIOUS_BUNDLE_GONE for error in response.json()["errors"])


async def test_needs_more_work_then_a_correction(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    await run_to_settled(supervisor, client, task_id)
    await legacy_acceptance_state(supervisor, client, task_id)
    body = client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "needs_more_work", "reasoning": "AC2 is not really exercised."},
    ).json()
    assert body["state"] == "awaiting_acceptance"
    assert any(w["reason"] == "needs_more_work" for w in client.get("/v1/wakes").json()["items"])

    document = correction_document(
        client, task_id, image="crucible-worker:fake-succeed", reason="needs_more_work"
    )
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert r.status_code == 200 and r.json()["state"] == "scheduled"
    assert await run_to_settled(supervisor, client, task_id) == "accepted"


async def test_a_correction_that_widens_scope_is_refused(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-prohibited-path")
    await run_to_settled(supervisor, client, task_id)
    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    document["scope"] = {**document["scope"], "allowed_paths": ["**"]}
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert r.status_code == 422
    assert any(e["path"] == "scope.allowed_paths" for e in r.json()["errors"])


async def test_a_correction_is_refused_from_awaiting_acceptance_without_needs_more_work(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await legacy_acceptance_state(supervisor, client, task_id)
    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert r.status_code == 422
    assert any("needs_more_work" in e["message"] for e in r.json()["errors"])


async def test_reject_from_awaiting_acceptance(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await legacy_acceptance_state(supervisor, client, task_id)
    body = client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "rejected", "reasoning": "The approach is wrong."},
    ).json()
    assert body["state"] == "rejected"


async def test_acceptance_is_refused_before_the_gates_pass(
    client: TestClient, supervisor: Supervisor
) -> None:
    """11: Crucible never infers acceptance, and it is only recorded where 09 allows."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    r = client.post(
        f"/v1/tasks/{task_id}/accept", json={"verdict": "accepted", "reasoning": "too early"}
    )
    assert r.status_code == 409
    assert r.json()["type"] == "urn:crucible:problem:transition-not-allowed"


async def test_acceptance_for_another_head_is_refused(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await legacy_acceptance_state(supervisor, client, task_id)
    r = client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "accepted", "reasoning": "x", "head_sha": "d" * 40},
    )
    assert r.status_code == 409


async def test_an_observer_may_not_accept(
    client: TestClient, supervisor: Supervisor, tokens: dict[str, str]
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    await legacy_acceptance_state(supervisor, client, task_id)
    r = client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "accepted", "reasoning": "x"},
        headers={"Authorization": f"Bearer {tokens['observer']}"},
    )
    assert r.status_code == 403


async def test_blocked_opens_an_escalation_that_a_decision_closes(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-blocked")
    assert await run_until(supervisor, client, task_id, {"blocked"}) == "blocked"
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert len(view["open_escalations"]) == 1
    escalation = view["open_escalations"][0]
    assert escalation["state"] == "open" and "needs a decision" in escalation["question"]
    assert any(w["reason"] == "blocked" for w in client.get("/v1/wakes").json()["items"])

    r = client.post(
        f"/v1/tasks/{task_id}/decisions",
        json={
            "kind": "scope_clarified",
            "verbatim": "Yes, treat a duplicate as a 409 and carry on.",
            "resolves": escalation["id"],
            "escalation_id": escalation["id"],
            "reschedule": True,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "scheduled"
    after = client.get(f"/v1/tasks/{task_id}").json()
    assert after["open_escalations"] == []
    assert after["decisions"][0]["verbatim"].startswith("Yes, treat a duplicate")
    kinds = event_kinds(client, task_id)
    assert "escalation_opened" in kinds and "escalation_closed" in kinds


async def test_attempt_metrics_record_the_run(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", deliverables=ARTIFACTS_DELIVERABLE
    )
    await run_to_settled(supervisor, client, task_id)
    await supervisor.tick()
    rows = client.get("/v1/routing/history", params={"model": "claude-sonnet-5"}).json()["items"]
    assert len(rows) == 1
    row = rows[0]
    assert row["harness"] == "claude_code" and row["pool"] == "anthropic-sub"
    assert row["exit_class"] == "completed" and row["wall_ms"] is not None
    assert row["gates_passed"] >= 9 and row["gates_failed"] == 0
    assert row["acceptance_verdict"] == "accepted"
    assert row["tokens_out"] is None and row["cost_source"] == "none"
