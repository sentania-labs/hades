"""The correction round's findings, against real containers (18).

A hostile ref forced past the contract, two verification ids a plain substitution would
collapse, and a verifier container that never finishes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.application.supervisor import Supervisor
from crucible.ports.execution import LaunchSpec, ProviderError
from tests.e2e.conftest import (
    OriginFactory,
    e2e_contract,
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
# No spaces, so git would accept it as a branch name; the only thing that stops the
# payload is that the preparer never lets it reach a command line as text.
PAYLOAD = "crucible/$(touch /crucible/work/output/pwned)"


async def test_a_hostile_ref_forced_past_the_contract_does_not_execute(
    provider: DockerProvider,
    origin: OriginFactory,
    artifact_root: Path,
    worker_image: str,
) -> None:
    """The contract refuses this ref at submit (tests/unit/test_refs.py). If something
    ever put one on a launch spec anyway, the preparer must still treat it as data."""
    url = origin("hostile-ref", "succeed")
    document = e2e_contract("E2E-0020", "hostile-ref", worker_image)
    document["repository"]["work_branch"] = "crucible/E2E-0020"
    spec = LaunchSpec(
        attempt_id="01HOSTILEREF00000000000000",
        task_id="01HOSTILETASK00000000000AA",
        external_id="E2E-0020",
        role="implement",
        harness="script-harness",
        model="none",
        image=worker_image,
        timeout_seconds=600,
        contract=document,
        policy={"images": {"allowlist": ["crucible-worker:*"]}},
        repository_url=url,
    )
    # Forced: the ref goes straight onto the spec, past every validator.
    spec.contract["repository"]["work_branch"] = PAYLOAD

    try:
        workspace = await provider.prepare(spec)
    except ProviderError:
        workspace = None
    root = artifact_root / "workspaces" / spec.attempt_id
    assert not (root / "output" / "pwned").exists(), "the ref executed inside the preparer"
    assert not Path("/tmp/pwned").exists()
    if workspace is not None:
        # git took it as one branch name, which is the whole point.
        assert workspace.work_branch == PAYLOAD
        head = (root / "repo" / ".git" / "HEAD").read_text(encoding="utf-8")
        assert PAYLOAD in head


async def test_two_verification_ids_that_differ_only_in_a_separator_stay_apart(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    worker_image: str,
) -> None:
    """`check/one` and `check_one` are two commands, two logs and two exit codes."""
    url = origin("ids", "succeed")
    register(ctx, "ids", url)
    document = e2e_contract("E2E-0021", "ids", worker_image)
    document["required_verification"] = [
        *document["required_verification"],
        {"id": "check/one", "command": "sh checks/lint.sh", "expect_exit": 0},
        {"id": "check_one", "command": "exit 3", "expect_exit": 3},
    ]
    task_id = submit_and_start(client, document)
    state = await run_until(supervisor, client, task_id, SETTLED)
    assert state == "accepted", gate_results(client, task_id)

    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    evidence = client.get(f"/v1/attempts/{attempt_id}/evidence").json()["items"]
    runs = {e["payload"]["id"]: e["payload"] for e in evidence if e["kind"] == "verification_run"}
    assert {"check/one", "check_one"} <= set(runs)
    assert runs["check/one"]["exit_code"] == 0
    assert runs["check_one"]["exit_code"] == 3
    assert gate_results(client, task_id)["verification_ran"] == "pass"


async def test_a_verifier_that_never_finishes_fails_the_gate(
    ctx: AppContext,
    client: TestClient,
    docker_config: DockerConfig,
    origin: OriginFactory,
    worker_image: str,
) -> None:
    """A genuinely hung throwaway container: the wait outlives the transport, the
    container is killed, and `verification_ran` fails with the reason (11, 16)."""
    url = origin("hung-verifier", "succeed")
    register(ctx, "hung-verifier", url)
    document = e2e_contract("E2E-0022", "hung-verifier", worker_image)
    document["required_verification"] = [
        *document["required_verification"],
        {"id": "V9", "command": "sleep 600", "expect_exit": 0},
    ]
    # The worker runs the same list, so it hangs on that command too; its own timeout
    # is what ends it, and the verifier's timeout is what this test is about.
    document["execution_request"]["timeout_seconds"] = 20
    task_id = submit_and_start(client, document)

    impatient: dict[str, Any] = {"verifier_timeout_seconds": 5, "api_timeout_seconds": 5}
    supervisor = Supervisor(
        ctx.uow_factory,
        {"docker": DockerProvider(replace_config(docker_config, impatient))},
        ctx.clock,
        holder="e2e-impatient",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=120,
        grace_seconds=5,
    )
    state = await run_until(supervisor, client, task_id, SETTLED, max_ticks=60, pause=1.0)
    assert state == "pre_pr_gates_failed", gate_results(client, task_id)

    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    evidence = client.get(f"/v1/attempts/{attempt_id}/evidence").json()["items"]
    runs = {e["payload"]["id"]: e["payload"] for e in evidence if e["kind"] == "verification_run"}
    # The commands the verifier got through before it hung are recorded as run; the one
    # it never finished is not, and that is what fails the gate.
    assert runs["V9"]["ran"] is False
    assert "did not finish within" in runs["V9"]["detail"]
    assert runs["V1"]["ran"] is True and runs["V1"]["exit_code"] == 0
    assert gate_results(client, task_id)["verification_ran"] == "fail"
    # The attempt still finished: nothing escaped and nothing reran it forever.
    assert client.get(f"/v1/attempts/{attempt_id}").json()["state"] in ("failed", "succeeded")


def replace_config(config: DockerConfig, changes: dict[str, Any]) -> DockerConfig:
    from dataclasses import replace  # noqa: PLC0415

    return replace(config, **changes)
