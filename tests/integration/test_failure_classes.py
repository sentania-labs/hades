"""Every failure class the fake provider can produce (16), with the retry rule."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState
from tests.fixtures import FakeClock
from tests.integration.conftest import event_kinds, run_to_settled, run_until, submit_and_start

pytestmark = pytest.mark.integration


def _attempts(client: TestClient, task_id: str) -> list[dict[str, object]]:
    view = client.get(f"/v1/tasks/{task_id}").json()
    return [a for e in view["executions"] for a in e["attempts"]]


async def _finish_at_cap(
    client: TestClient,
    supervisor: Supervisor,
    task_id: str,
    *,
    local: bool,
    exit_class: ExitClass,
    turn_cap_reached: bool = False,
) -> tuple[str, str]:
    assert await run_until(supervisor, client, task_id, {"running"}) == "running"
    view = client.get(f"/v1/tasks/{task_id}").json()
    execution_id = view["executions"][0]["id"]
    attempt_id = view["latest_attempt"]["id"]
    with supervisor._fenced() as uow:
        execution = uow.executions.get(execution_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert execution is not None and attempt is not None and attempt.selected_model is not None
        ref = execution.policy_snapshot["routing"]["policy"]
        routing = uow.routing_policies.get(str(ref["name"]), int(ref["version"]))
        assert routing is not None
        document = copy.deepcopy(routing.document)
        entry = next(item for item in document["models"] if item["model"] == attempt.selected_model)
        entry["endpoint"] = "local" if local else "subscription"
        if local:
            entry["endpoint_url"] = "http://gateway.lab.test:4000/v1"
        else:
            entry.pop("endpoint_url", None)
        routing.document = document
        uow.routing_policies.put(routing)
        # Submission uses the fixture policy's eligible classes. Exercise timeout
        # retry eligibility directly on this execution without changing that policy.
        execution.retry_on = ["timeout"]
        uow.executions.save(execution)
        attempt.state = AttemptState.COLLECTED
        attempt.exit_class = exit_class
        uow.attempts.save(attempt)
        supervisor._classify_and_finish(uow, attempt, None, turn_cap_reached=turn_cap_reached)
        uow.commit()
    return execution_id, attempt_id


async def test_crash_no_retry(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-crash")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    attempts = _attempts(client, task_id)
    assert len(attempts) == 1
    assert attempts[0]["exit_class"] == "crashed" and attempts[0]["state"] == "failed"
    kinds = event_kinds(client, task_id)
    assert "execution_failed" in kinds and "task_retry_scheduled" not in kinds


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_clean_exit_with_commits_and_no_report_is_completed(
    client: TestClient, supervisor: Supervisor
) -> None:
    """hades #498: the worker returned work, so the attempt is `completed` and the
    missing report is for the reviewer, never `completed_without_report`."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed-noreport")
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "completed" and attempt["state"] == "succeeded"
    assert "report_parsed" not in event_kinds(client, task_id)
    summary = client.get(f"/v1/tasks/{task_id}").json()["gate_summary"]
    assert summary["failing"] == []
    assert "report_present" in {item["gate"] for item in summary["for_reviewer"]}
    record = client.get(f"/v1/attempts/{attempt['id']}/report").json()
    assert record["parsed_ok"] is False
    assert record["document"]["composed"]["by"] == "hades"
    assert record["document"]["composed"]["worker_report"]["status"] == "absent"


async def test_blocked_exit_75(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-blocked")
    assert await run_until(supervisor, client, task_id, {"blocked"}) == "blocked"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "blocked" and attempt["state"] == "blocked"
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    blocked = next(e for e in events if e["kind"] == "task_blocked")
    assert "needs a decision" in blocked["payload"]["blocked_md"]
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["executions"][0]["state"] == "active"


async def test_exit_75_without_blocked_md_is_failure(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-blocked-nofile")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "crashed"


async def test_environment_retries_then_reports(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-environment")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    attempts = _attempts(client, task_id)
    assert [a["number"] for a in attempts] == [1, 2]
    assert all(a["exit_class"] == "environment" for a in attempts)
    kinds = event_kinds(client, task_id)
    assert kinds.count("task_retry_scheduled") == 1
    assert kinds.count("attempt_created") == 2
    assert kinds.index("task_retry_scheduled") < kinds.index("execution_failed")


async def test_lost_retries_when_contract_allows(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-vanish-2")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    attempts = _attempts(client, task_id)
    assert len(attempts) == 2 and all(a["exit_class"] == "lost" for a in attempts)
    assert event_kinds(client, task_id).count("attempt_lost") == 2


async def test_lost_no_retry_when_contract_excludes_it(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-vanish-2",
        lifecycle={"max_attempts": 3, "retry_on": ["environment"], "cleanup": "policy"},
    )
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    assert len(_attempts(client, task_id)) == 1


async def test_prepare_failure_is_environment(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-prepare-fails",
        lifecycle={"max_attempts": 1, "retry_on": [], "cleanup": "policy"},
    )
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "environment"
    kinds = event_kinds(client, task_id)
    assert "attempt_preparing" in kinds and "attempt_launching" not in kinds
    assert "task_running" in kinds


async def test_timeout_drains_then_kills(
    client: TestClient, supervisor: Supervisor, clock: FakeClock, provider: FakeProvider
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-hang")
    await supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "running"
    await supervisor.tick()
    (attempt,) = _attempts(client, task_id)
    worker = provider.worker(str(attempt["id"]))
    assert worker is not None and worker.drains == 0
    clock.advance(3600)
    await supervisor.tick()
    assert worker.drains == 1 and worker.kills == 0
    await supervisor.tick()
    assert worker.kills == 0, "still inside the grace window"
    clock.advance(60)
    await supervisor.tick()
    assert worker.kills == 1
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "timeout" and attempt["exit_code"] == 137
    kinds = event_kinds(client, task_id)
    assert (
        kinds.index("attempt_timeout_drain")
        < kinds.index("attempt_timeout_kill")
        < kinds.index("attempt_exited")
    )
    assert "task_retry_scheduled" not in kinds


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_local_turn_cap_blocks_instead_of_retrying(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-hang",
        lifecycle={"max_attempts": 3, "retry_on": ["environment", "lost"], "cleanup": "policy"},
    )
    _, attempt_id = await _finish_at_cap(
        client,
        supervisor,
        task_id,
        local=True,
        exit_class=ExitClass.COMPLETED_WITHOUT_REPORT,
        turn_cap_reached=True,
    )
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "blocked"
    assert [attempt["id"] for attempt in _attempts(client, task_id)] == [attempt_id]
    assert view["open_escalations"][0]["question"] == "too_big_for_local:turns"


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_local_time_cap_blocks_instead_of_retrying(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-hang",
        lifecycle={"max_attempts": 3, "retry_on": ["environment", "lost"], "cleanup": "policy"},
    )
    _, attempt_id = await _finish_at_cap(
        client,
        supervisor,
        task_id,
        local=True,
        exit_class=ExitClass.TIMEOUT,
    )
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "blocked"
    assert [attempt["id"] for attempt in _attempts(client, task_id)] == [attempt_id]
    assert view["open_escalations"][0]["question"] == "too_big_for_local:time"


async def test_a_frontier_time_cap_still_retries(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-hang",
        lifecycle={"max_attempts": 3, "retry_on": ["environment", "lost"], "cleanup": "policy"},
    )
    _, attempt_id = await _finish_at_cap(
        client,
        supervisor,
        task_id,
        local=False,
        exit_class=ExitClass.TIMEOUT,
    )
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert [attempt["number"] for attempt in _attempts(client, task_id)] == [1, 2]
    assert view["latest_attempt"]["id"] != attempt_id


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_the_too_big_wake_names_the_cap(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-hang")
    _, attempt_id = await _finish_at_cap(
        client,
        supervisor,
        task_id,
        local=True,
        exit_class=ExitClass.COMPLETED_WITHOUT_REPORT,
        turn_cap_reached=True,
    )
    wakes = client.get("/v1/wakes").json()["items"]
    (wake,) = [item for item in wakes if item["payload"].get("attempt_id") == attempt_id]
    assert wake["reason"] == "blocked"
    assert wake["payload"]["summary"] == "split the task: the local attempt hit its turn cap"


async def test_report_with_secret_is_redacted(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    from crucible.adapters.execution.fake import default_report  # noqa: PLC0415
    from crucible.ports.execution import LaunchSpec  # noqa: PLC0415
    from tests.fixtures import contract_document  # noqa: PLC0415

    spec = LaunchSpec(
        attempt_id="x",
        task_id="t",
        external_id="EX-0001",
        role="implement",
        harness="codex",
        model="m",
        image="i",
        timeout_seconds=1,
        contract=contract_document(),
    )
    report = default_report(spec)
    report["summary"] = "pushed with ghp_" + "k" * 36
    provider.set_report("EX-0001", report)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    (attempt,) = _attempts(client, task_id)
    stored = client.get(f"/v1/attempts/{attempt['id']}").json()["report"]
    assert stored["parsed_ok"] is False and stored["document"]["redacted"] is True
    assert "summary" not in stored["document"] or "kkkk" not in stored["document"]["summary"]
    assert "kkkk" not in client.get(f"/v1/attempts/{attempt['id']}").text
    # hades #498: the worker returned work; the redacted report is the reviewer's and
    # the secret is the blocking no_secrets failure.
    assert attempt["exit_class"] == "completed"


async def test_an_oom_killed_worker_is_environment_and_retries(
    client: TestClient, supervisor: Supervisor
) -> None:
    """S5 (review C3): exit 137 with the kernel's OOM kill is an environment failure,
    not a crash, so the contract's retry rule applies."""
    task_id = submit_and_start(client, "crucible-worker:fake-oom")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    attempts = _attempts(client, task_id)
    assert [a["number"] for a in attempts] == [1, 2]
    assert all(a["exit_class"] == "environment" and a["exit_code"] == 137 for a in attempts)
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    exited = [e for e in events if e["kind"] == "attempt_exited"]
    assert exited and all(e["payload"]["oom_killed"] is True for e in exited)


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_report_that_does_not_parse_is_a_parse_failure_not_no_report(
    client: TestClient, supervisor: Supervisor
) -> None:
    """07 (found live): the file was there; the record says it did not parse, and only
    a missing file reads as "without report"."""
    task_id = submit_and_start(client, "crucible-worker:fake-bad-report")
    # hades #498: the parse failure is advisory; the work gates pass, so the task waits
    # for the reviewer with the parse problem listed.
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    kinds = [e["kind"] for e in events]
    assert "report_parse_failed" in kinds
    collected = next(e for e in events if e["kind"] == "attempt_collected")
    assert collected["payload"]["report_present"] is True
    assert collected["payload"]["report_parsed"] is False
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "completed"
