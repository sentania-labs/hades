"""Prepare, launch, observe, logs, collect, verify, cleanup on real containers (18)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.docker import DockerProvider
from crucible.application.supervisor import Supervisor
from tests.e2e import daemon
from tests.e2e.conftest import (
    OriginFactory,
    e2e_contract,
    event_kinds,
    gate_results,
    register,
    run_until,
    submit_and_start,
)

pytestmark = [
    pytest.mark.e2e,
    # Real containers, and the first case also pays for the session's stack; the waits
    # inside allow up to four minutes (issue 192).
    pytest.mark.timeout(600),
]

SETTLED = {"accepted", "pre_pr_gates_failed"}


async def test_a_script_harness_run_reaches_acceptance_with_every_gate_green(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    provider: DockerProvider,
    origin: OriginFactory,
    engine: Engine,
    worker_image: str,
) -> None:
    url = origin("full-run", "succeed")
    register(ctx, "full-run", url)
    task_id = submit_and_start(client, e2e_contract("E2E-0001", "full-run", worker_image))

    state = await run_until(supervisor, client, task_id, SETTLED)
    assert state == "accepted", gate_results(client, task_id)

    results = gate_results(client, task_id)
    assert results["verification_ran"] == "pass", results
    assert results["workspace_clean"] == "pass", results
    assert results["commits_present"] == "pass", results
    assert results["no_injected_files"] == "pass", results
    assert results["no_secrets"] == "pass", results
    assert "deferred" not in set(results.values())

    view = client.get(f"/v1/tasks/{task_id}").json()
    attempt_id = view["latest_attempt"]["id"]

    # 13: the attempt records the image digest it ran and the identity bundle hash.
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT image_digest, identity_sha256, logs_drained_at, cleaned_up_at "
                "FROM attempts WHERE id = :id"
            ),
            {"id": attempt_id},
        ).one()
    assert row.image_digest and row.image_digest.startswith("sha256:")
    assert row.identity_sha256 and len(row.identity_sha256) == 64
    assert row.logs_drained_at is not None, "cleanup must never run before logs_drained (08)"
    assert row.cleaned_up_at is not None and row.cleaned_up_at >= row.logs_drained_at

    # 10: the log stream is stored, and the worker's own lines are in it.
    with engine.begin() as conn:
        chunks = conn.execute(
            text("SELECT content, gzipped FROM log_chunks WHERE attempt_id = :id ORDER BY id"),
            {"id": attempt_id},
        ).all()
    assert chunks, "the supervisor stored no log chunks"
    text_out = b"".join(c.content for c in chunks if not c.gzipped).decode("utf-8", "replace")
    assert "read identity bundle" in text_out
    assert "wrote report.yaml" in text_out

    # 11: Crucible's own re-run of every required command, from the collected tree.
    evidence = client.get(f"/v1/attempts/{attempt_id}/evidence").json()["items"]
    runs = [e for e in evidence if e["kind"] == "verification_run"]
    assert {r["payload"]["id"] for r in runs} == {"V1", "V2", "V3"}
    assert all(r["payload"]["exit_code"] == 0 and r["payload"]["ran"] for r in runs)
    assert all(r["source"] == "crucible" and r["verified"] for r in runs)

    bundle = next(e for e in evidence if e["kind"] == "bundle_head")
    assert bundle["payload"]["bundle_verified"] is True
    assert bundle["payload"]["commits"] >= 1

    kinds = event_kinds(client, task_id)
    for kind in (
        "workspace_prepared",
        "image_resolved",
        "checkout_lease_taken",
        "attempt_logs_drained",
        "verification_completed",
        "attempt_cleaned_up",
        "checkout_lease_released",
    ):
        assert kind in kinds, f"{kind} missing from {sorted(set(kinds))}"

    # 08: cleanup removes the container only after logs_drained.
    assert daemon.container_ids(f"crucible.attempt={attempt_id}") == []


async def test_the_branch_carries_no_injected_file(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    worker_image: str,
) -> None:
    """18: no injected files in the resulting branch; the shims are excluded (06, 11)."""
    url = origin("no-injected", "succeed")
    register(ctx, "no-injected", url)
    task_id = submit_and_start(client, e2e_contract("E2E-0002", "no-injected", worker_image))
    await run_until(supervisor, client, task_id, SETTLED)

    view = client.get(f"/v1/tasks/{task_id}").json()
    attempt_id = view["latest_attempt"]["id"]
    evidence = client.get(f"/v1/attempts/{attempt_id}/evidence").json()["items"]
    paths = next(e for e in evidence if e["kind"] == "diff_paths")["payload"]["paths"]
    assert paths, "the worker changed nothing"
    assert "CLAUDE.md" not in paths and "AGENTS.md" not in paths
    assert not any(p.startswith(".crucible") for p in paths)
    assert gate_results(client, task_id)["no_injected_files"] == "pass"
