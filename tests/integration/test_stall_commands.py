"""Issue 152 through the supervisor: a silent command longer than the stall limit but
inside its command timeout is not a stall, and a worker with nothing in flight and no
output still stalls out.

The worker is the fake provider's `hang`, scripted with the lines each harness writes
while a command runs: the log is the only live evidence the supervisor has, on Docker
and Kubernetes alike."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import ProviderSetting
from crucible.ports.execution import LogChunk
from tests.fixtures import FakeClock, contract_document
from tests.integration.conftest import event_kinds, make_supervisor

pytestmark = pytest.mark.integration

# The seeded policy's stall limits, and a command running longer than the fail limit
# but well inside the 60-minute default command timeout.
STALL_WARN = 300
STALL_FAIL = 1800
COMMAND_SECONDS = 2400
TICK = 50

CLAUDE_BASH = {
    "type": "assistant",
    "message": {
        "id": "msg_1",
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "make e2e"}}
        ],
    },
    "parent_tool_use_id": None,
}
CLAUDE_RESULT = {
    "type": "user",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1"}]},
}
CODEX_STARTED = {
    "type": "item.started",
    "item": {"id": "item_1", "type": "command_execution", "command": "make e2e"},
}
CODEX_COMPLETED = {
    "type": "item.completed",
    "item": {"id": "item_1", "type": "command_execution", "command": "make e2e"},
}

# What each harness writes when its command starts and when it ends.
SCRIPTS: dict[str, tuple[LogChunk, LogChunk]] = {
    "claude_code": (
        LogChunk("stdout", (json.dumps(CLAUDE_BASH) + "\n").encode()),
        LogChunk("stdout", (json.dumps(CLAUDE_RESULT) + "\n").encode()),
    ),
    "codex": (
        LogChunk("stdout", (json.dumps(CODEX_STARTED) + "\n").encode()),
        LogChunk("stdout", (json.dumps(CODEX_COMPLETED) + "\n").encode()),
    ),
    "hermes": (
        LogChunk("stderr", b"crucible-launch: commands running: 1\n"),
        LogChunk("stderr", b"crucible-launch: commands running: 0\n"),
    ),
}


# One model each harness serves in the seeded routing policy.
MODELS = {
    "claude_code": "claude-sonnet-5",
    "codex": "gpt-5.6-luna",
    "hermes": "gpt-oss:120b",
    "agy": "gemini-3.8-flash-low",
}


@pytest.fixture
def operator(ctx: AppContext, tokens: dict[str, str]) -> Iterator[TestClient]:
    """Pinning a harness is the operator's (05)."""
    with TestClient(
        create_app(ctx), headers={"Authorization": f"Bearer {tokens['operator']}"}
    ) as c:
        yield c


def local_endpoint_policy(client: TestClient, admin: TestClient, version: int = 152) -> int:
    """Hermes serves only a local endpoint, which the seeded routing leaves unset; the
    same setup as the local pool cap test, one version on."""
    routing = client.get("/v1/routing/default-routing/4").json()["document"]
    routing["version"] = version
    hermes = next(model for model in routing["models"] if model["harness"] == "hermes")
    hermes.update(
        {"endpoint_url": "http://192.0.2.41:11434/v1", "enabled": True, "disabled_reason": None}
    )
    assert admin.put(f"/v1/routing/default-routing/{version}", json=routing).status_code == 200
    policy = client.get("/v1/policies/default-software/4").json()["document"]
    policy["version"] = version
    policy["routing"]["policy"]["version"] = version
    assert admin.put(f"/v1/policies/default-software/{version}", json=policy).status_code == 200
    limits = policy["limits"]
    assert (limits["stall_warn_seconds"], limits["stall_fail_seconds"]) == (STALL_WARN, STALL_FAIL)
    return version


def start(
    client: TestClient,
    harness: str,
    external_id: str,
    policy_version: int | None = None,
    command_timeout_ms: int | None = None,
) -> str:
    doc = contract_document(external_id=external_id)
    doc["required_verification"].append(
        {"id": "V9", "command": "test -f made-by-the-worker", "expect_exit": 0}
    )
    if command_timeout_ms is not None:
        doc["execution_request"]["command_timeout_ms"] = command_timeout_ms
    if policy_version is not None:
        doc["policy"] = {"name": "default-software", "version": policy_version}
    doc["repository"]["work_branch"] = f"crucible/{external_id}"
    doc["execution_request"]["image"] = "crucible-worker:fake-hang"
    # Long enough that only a stall can end the attempt inside the test's window.
    doc["execution_request"]["timeout_seconds"] = 14400
    doc["execution_request"]["harness"] = harness
    doc["execution_request"]["model"] = MODELS[harness]
    doc["execution_request"]["pin_reason"] = "Issue 152 needs this harness's live evidence."
    r = client.post("/v1/tasks", json=doc)
    assert r.status_code == 201, r.text
    task_id: str = r.json()["id"]
    r = client.post(
        f"/v1/tasks/{task_id}/start",
        json={
            "provider": "fake",
            "image": "crucible-worker:fake-hang",
            "policy_version": policy_version or 2,
        },
    )
    assert r.status_code == 200, r.text
    return task_id


async def advance(supervisor: Supervisor, clock: FakeClock, seconds: int) -> None:
    for _ in range(seconds // TICK):
        clock.advance(TICK)
        await supervisor.tick()


@pytest.mark.parametrize(
    "harness",
    [
        "claude_code",
        "codex",
        pytest.param(
            "hermes",
            marks=pytest.mark.xfail(
                strict=False, reason="hades #560: drifted from the product; cleanup pending"
            ),
        ),
    ],
)
async def test_a_silent_command_past_the_stall_limit_is_not_a_stall(
    operator: TestClient,
    supervisor: Supervisor,
    provider: FakeProvider,
    clock: FakeClock,
    ctx: AppContext,
    tokens: dict[str, str],
    harness: str,
) -> None:
    client = operator
    policy_version = None
    if harness == "hermes":
        admin_headers = {"Authorization": f"Bearer {tokens['admin']}"}
        with TestClient(create_app(ctx), headers=admin_headers) as admin:
            policy_version = local_endpoint_policy(client, admin)
    external_id = f"EX-0152-{harness.upper().replace('_', '')}"
    task_id = start(client, harness, external_id, policy_version)
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    worker = provider.worker(attempt_id)
    assert worker is not None
    command_started, command_ended = SCRIPTS[harness]
    worker.logs.append(command_started)

    await advance(supervisor, clock, COMMAND_SECONDS)
    assert worker.drains == 0 and worker.kills == 0
    kinds = event_kinds(client, task_id)
    assert "worker_quiet" not in kinds and "worker_stalled" not in kinds
    attempt = client.get(f"/v1/attempts/{attempt_id}").json()
    assert attempt["state"] == "running"
    assert attempt["heartbeat_summary"]["state"] == "alive"
    with ctx.uow_factory() as uow:
        signals = [h for h in uow.heartbeats.list_for_attempt(attempt_id, limit=10_000)]
    running = [h for h in signals if h.signal == "command_running"]
    # Refreshed about once a minute, not once a tick.
    assert COMMAND_SECONDS // 120 <= len(running) <= COMMAND_SECONDS // 60 + 1
    assert running[0].detail["count"] == 1

    # The command ends and the worker falls silent: the clock runs again from there.
    worker.logs.append(command_ended)
    await advance(supervisor, clock, STALL_WARN + TICK)
    assert event_kinds(client, task_id).count("worker_quiet") == 1
    await advance(supervisor, clock, STALL_FAIL - STALL_WARN)
    assert worker.drains == 1
    assert client.get(f"/v1/attempts/{attempt_id}").json()["termination_reason"] == "stall"
    assert "worker_stalled" in event_kinds(client, task_id)


@pytest.mark.parametrize("harness", ["agy", "claude_code"])
@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_worker_with_nothing_in_flight_and_no_output_still_stalls(
    operator: TestClient,
    supervisor: Supervisor,
    provider: FakeProvider,
    clock: FakeClock,
    harness: str,
) -> None:
    """Claude Code with no command open, and AGY, which gives no live evidence at all:
    both stall at the limit as before (05b)."""
    client = operator
    task_id = start(client, harness, f"EX-0152-IDLE-{harness.upper().replace('_', '')}")
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    worker = provider.worker(attempt_id)
    assert worker is not None
    await advance(supervisor, clock, STALL_FAIL - TICK)
    assert worker.drains == 0
    assert event_kinds(client, task_id).count("worker_quiet") == 1
    await advance(supervisor, clock, TICK)
    assert worker.drains == 1
    assert client.get(f"/v1/attempts/{attempt_id}").json()["termination_reason"] == "stall"


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_command_ending_and_a_new_one_starting_in_one_replay_gets_its_own_age(
    operator: TestClient,
    supervisor: Supervisor,
    provider: FakeProvider,
    clock: FakeClock,
    ctx: AppContext,
    tokens: dict[str, str],
) -> None:
    """Issue 152 correction (review round 2): Hermes tracks its whole registry under one
    fixed key, so a command ending and a new one starting inside the log a restart
    replays in one batch must not let the new command inherit the ended one's age."""
    client = operator
    admin_headers = {"Authorization": f"Bearer {tokens['admin']}"}
    with TestClient(create_app(ctx), headers=admin_headers) as admin:
        policy_version = local_endpoint_policy(client, admin, version=153)
    task_id = start(
        client, "hermes", "EX-0152-HERMES-BATCH", policy_version, command_timeout_ms=120_000
    )
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    worker = provider.worker(attempt_id)
    assert worker is not None
    started, ended = SCRIPTS["hermes"]
    worker.logs.append(started)
    await advance(supervisor, clock, TICK)
    # Nothing observes the log again until the restart: the first command ends and a
    # second starts while the old supervisor's watch is discarded.
    clock.advance(200)
    worker.logs.append(ended)
    worker.logs.append(started)
    restarted = make_supervisor(ctx, provider)
    await advance(restarted, clock, TICK)
    # If the second command inherited the first one's age, it would already be past its
    # command timeout and margin (180 s) here, so the clock would run again from now.
    await advance(restarted, clock, STALL_WARN + TICK)
    assert "worker_quiet" not in event_kinds(client, task_id)
    assert worker.drains == 0


async def test_a_restarted_supervisor_rebuilds_the_command_from_the_stored_log(
    operator: TestClient,
    supervisor: Supervisor,
    provider: FakeProvider,
    clock: FakeClock,
    ctx: AppContext,
) -> None:
    client = operator
    task_id = start(client, "codex", "EX-0152-RESTART")
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    worker = provider.worker(attempt_id)
    assert worker is not None
    worker.logs.append(SCRIPTS["codex"][0])
    await advance(supervisor, clock, STALL_FAIL // 2)
    # A fresh process: nothing in memory, only the stored log.
    restarted = make_supervisor(ctx, provider)
    restarted_at = clock.now()
    await advance(restarted, clock, STALL_FAIL)
    assert worker.drains == 0
    assert "worker_quiet" not in event_kinds(client, task_id)
    with ctx.uow_factory() as uow:
        heartbeats = uow.heartbeats.list_for_attempt(attempt_id, limit=10_000)
    renewed = [h for h in heartbeats if h.signal == "command_running" and h.ts > restarted_at]
    assert len(renewed) >= STALL_FAIL // 120


async def test_a_restart_mid_command_keeps_the_original_age_and_stops_pausing_at_the_original_bound(
    operator: TestClient,
    supervisor: Supervisor,
    provider: FakeProvider,
    clock: FakeClock,
    ctx: AppContext,
) -> None:
    """Issue 152 correction: a restart or a takeover must not reset a running command's
    age, or a command near its command timeout gets another full timeout, and repeated
    takeovers would extend it indefinitely."""
    client = operator
    task_id = start(client, "codex", "EX-0152-RESTART-OVERRUN", command_timeout_ms=600_000)
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    worker = provider.worker(attempt_id)
    assert worker is not None
    worker.logs.append(SCRIPTS["codex"][0])
    elapsed_before_restart = 300
    await advance(supervisor, clock, elapsed_before_restart)
    # A fresh process mid-command: nothing in memory, only the stored log. If the
    # restart reset the command's age to now, the overrun bound below (measured from
    # the command's original start) would never arrive on this schedule.
    restarted = make_supervisor(ctx, provider)
    remaining_to_command_timeout = 600 - elapsed_before_restart
    await advance(restarted, clock, remaining_to_command_timeout + 60 + STALL_FAIL + 2 * TICK)
    assert worker.drains == 1
    assert client.get(f"/v1/attempts/{attempt_id}").json()["termination_reason"] == "stall"


async def test_a_command_reported_past_its_command_timeout_stops_pausing_the_clock(
    operator: TestClient,
    supervisor: Supervisor,
    provider: FakeProvider,
    clock: FakeClock,
) -> None:
    """A Codex session or a Hermes background process is not ended by the harness's own
    timeout; the command timeout still bounds how long it holds the stall clock."""
    client = operator
    task_id = start(client, "codex", "EX-0152-OVERRUN", command_timeout_ms=600_000)
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    worker = provider.worker(attempt_id)
    assert worker is not None
    worker.logs.append(SCRIPTS["codex"][0])
    await advance(supervisor, clock, 600)
    assert "worker_quiet" not in event_kinds(client, task_id)
    # Never completed: past 600 s and the minute's margin, the silence counts again.
    await advance(supervisor, clock, 60 + STALL_FAIL + 2 * TICK)
    assert worker.drains == 1
    assert client.get(f"/v1/attempts/{attempt_id}").json()["termination_reason"] == "stall"


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_the_saved_hermes_run_limits_reach_the_launch(
    operator: TestClient,
    supervisor: Supervisor,
    provider: FakeProvider,
    ctx: AppContext,
    tokens: dict[str, str],
) -> None:
    """FDY-0140: the limits saved on the Local gateway page are read at each launch and
    handed to the Hermes wrapper; nothing puts the Hermes venv on the worker's PATH."""
    client = operator
    admin_headers = {"Authorization": f"Bearer {tokens['admin']}"}
    await supervisor.tick()
    with TestClient(create_app(ctx), headers=admin_headers) as admin:
        policy_version = local_endpoint_policy(client, admin, version=154)
    # What the Local gateway page saves (test_admin drives the page, the API and the CLI).
    with ctx.uow_factory() as uow:
        uow.provider_settings.put(
            ProviderSetting(
                name="harness.hermes",
                document={"max_turns": 420, "context_length": 98304},
                updated_at=ctx.clock.now(),
                updated_by="tests",
            )
        )
        uow.commit()
    task_id = start(client, "hermes", "EX-0140-LIMITS", policy_version)
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    worker = provider.worker(attempt_id)
    assert worker is not None
    env = worker.spec.env
    assert env["CRUCIBLE_HERMES_MAX_TURNS"] == "420"
    assert env["CRUCIBLE_HERMES_CONTEXT_LENGTH"] == "98304"
    assert env["CRUCIBLE_HERMES_IDENTITY"] == "/crucible/identity/IDENTITY.md"
    assert "PATH" not in env
    assert worker.spec.harness_settings == {"max_turns": 420, "context_length": 98304}
