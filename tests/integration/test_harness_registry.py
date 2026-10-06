"""The lifecycle through the registry on the fake provider (07, 18, 25): a disabled
harness is refused at launch with a wake and never retried; enabling it through the
service lets the same contract run; `GET /harnesses` and `GET /images` report the
sanitized state; per-harness concurrency defers the second launch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.admin.context import AdminContext
from crucible.application.harnesses import set_harness_enabled
from crucible.application.supervisor import Supervisor
from crucible.ports.harness import CredentialSource, HarnessGate
from tests.fixtures import contract_document
from tests.integration.conftest import (
    event_kinds,
    make_supervisor,
    run_to_settled,
    submit_and_start,
)

pytestmark = pytest.mark.integration


def _disable(ctx: AppContext, name: str, reason: str) -> None:
    with ctx.uow_factory() as uow:
        set_harness_enabled(
            uow,
            ctx.clock,
            principal_name="admin-principal",
            name=name,
            enabled=False,
            reason=reason,
        )
        uow.commit()


def _pinned_document(external_id: str, image: str) -> dict[str, Any]:
    document = contract_document(external_id=external_id)
    document["repository"]["work_branch"] = f"crucible/{external_id}"
    document["execution_request"].update(
        {
            "harness": "codex",
            "model": "gpt-5.6-luna",
            "pin_reason": "harness registry integration test",
            "image": image,
        }
    )
    return document


def _submit_pinned(client: TestClient, tokens: dict[str, str], image: str, external_id: str) -> str:
    headers = {"Authorization": f"Bearer {tokens['operator']}"}
    response = client.post("/v1/tasks", json=_pinned_document(external_id, image), headers=headers)
    assert response.status_code == 201, response.text
    task_id = str(response.json()["id"])
    response = client.post(
        f"/v1/tasks/{task_id}/start",
        json={"provider": "fake", "image": image, "policy_version": 2},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return task_id


async def test_a_disabled_harness_is_refused_with_a_wake_and_no_retry(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    tokens: dict[str, str],
) -> None:
    # Disabled after submit: at submit the gate is a contract problem (review I8); a
    # harness disabled while a task is scheduled is what the launch-time refusal is for.
    task_id = _submit_pinned(client, tokens, "crucible-worker:fake-succeed", "EX-DISABLED-LAUNCH")
    _disable(ctx, "codex", "rotating the dedicated credential")
    assert await run_to_settled(supervisor, client, task_id) == "pre_pr_gates_failed"
    kinds = event_kinds(client, task_id)
    assert "harness_refused" in kinds
    assert "task_retry_scheduled" not in kinds, "a refusal is not retried (07)"
    assert kinds.count("attempt_created") == 1
    refused = next(
        e
        for e in client.get(f"/v1/tasks/{task_id}/events").json()["items"]
        if e["kind"] == "harness_refused"
    )
    assert refused["payload"]["harness"] == "codex"
    assert (
        "disabled by an administrator: rotating the dedicated credential"
        in refused["payload"]["detail"]
    )
    wakes = client.get(
        "/v1/wakes", headers={"Authorization": f"Bearer {tokens['operator']}"}
    ).json()["items"]
    unavailable = [w for w in wakes if w["reason"] == "harness_unavailable"]
    assert len(unavailable) == 1 and "codex" in unavailable[0]["summary"]
    view = client.get(f"/v1/tasks/{task_id}").json()
    (attempt,) = [a for e in view["executions"] for a in e["attempts"]]
    assert attempt["exit_class"] == "environment"


async def test_enabling_through_the_service_lets_the_same_harness_run(
    ctx: AppContext, client: TestClient, supervisor: Supervisor
) -> None:
    _disable(ctx, "codex", "paused")
    with ctx.uow_factory() as uow:
        state = set_harness_enabled(
            uow,
            ctx.clock,
            principal_name="admin-principal",
            name="codex",
            enabled=True,
            reason="credential validated",
        )
        uow.commit()
    assert state.enabled and state.reason == "credential validated"
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert await run_to_settled(supervisor, client, task_id) == "publishing"
    kinds = event_kinds(client, task_id)
    assert "harness_refused" not in kinds


async def test_a_reason_is_an_optional_note_on_the_flag(ctx: AppContext) -> None:
    """crucible#117: flipping the flag without a reason records none."""
    with ctx.uow_factory() as uow:
        state = set_harness_enabled(
            uow, ctx.clock, principal_name="admin-principal", name="agy", enabled=False, reason="  "
        )
    assert state.enabled is False and state.reason == ""


async def test_get_harnesses_reports_flags_ranges_and_a_sanitized_credential_state(
    ctx: AppContext, client: TestClient, supervisor: Supervisor
) -> None:
    _disable(ctx, "agy", "unverified: waiting on the Crucible-side refresh (S1b)")
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    items = {h["name"]: h for h in client.get("/v1/harnesses").json()["items"]}
    assert set(items) == {"claude_code", "codex", "agy", "hermes", "qwen_code", "script-harness"}
    agy = items["agy"]
    assert agy["enabled"] is False and agy["enabled_by_administrator"] is False
    assert agy["enabled_by_configuration"] is True
    assert "unverified" in agy["reason"]
    assert agy["supported_versions"] == ">=1.2.0,<1.3.0"
    # No credential path is configured in this tier: absent, and nothing else to say.
    assert agy["credential"]["state"] == "absent"
    assert agy["credential"]["source_fingerprint"] is None
    claude = items["claude_code"]
    assert claude["enabled"] is True
    assert claude["credential"]["last_launch_outcome"] == "completed"
    assert claude["credential"]["last_launch_at"] is not None
    assert claude["capabilities"]["endpoints"] == ["api.anthropic.com"]
    # The fake provider lists no images (08); the endpoint still answers.
    assert client.get("/v1/images").json()["items"] == []
    # Nothing secret-shaped anywhere in the two documents.
    blob = client.get("/v1/harnesses").text
    assert "token" not in blob.lower().replace("oauth-token", "").replace("oauth_token", "")


async def test_per_harness_concurrency_defers_the_second_launch(
    ctx: AppContext,
    client: TestClient,
    provider: FakeProvider,
    tokens: dict[str, str],
) -> None:
    """05b and 12: a policy cap of 1 defers a second credential holder."""
    supervisor = make_supervisor(ctx, provider)
    first = _submit_pinned(client, tokens, "crucible-worker:fake-hang", "EX-0001")
    second = _submit_pinned(client, tokens, "crucible-worker:fake-succeed", "EX-0002")
    await supervisor.tick()
    await supervisor.tick()
    states = {task: client.get(f"/v1/tasks/{task}").json()["state"] for task in (first, second)}
    assert states[first] == "running"
    assert states[second] == "scheduled", states
    assert "harness_launch_deferred" in event_kinds(client, second)
    # Deferred once per attempt, not once per tick.
    await supervisor.tick()
    assert event_kinds(client, second).count("harness_launch_deferred") == 1
    # Cancel the hang; the second launches on a later tick.
    cancel = client.post(
        f"/v1/tasks/{first}/cancel",
        json={"reason": "free the harness", "verbatim": "cancel it", "decided_by": "tests"},
        headers={"Authorization": f"Bearer {tokens['operator']}"},
    )
    assert cancel.status_code == 200, cancel.text
    # The hang ignores the drain; past the grace it is killed, exits, and is collected,
    # and only then does the cap release the second launch (review I2).
    await supervisor.tick()
    ctx.clock.advance(61)  # type: ignore[attr-defined]
    for _ in range(3):
        await supervisor.tick()
    assert await run_to_settled(supervisor, client, second, max_ticks=40) == ("publishing")


@pytest.mark.parametrize("local_harness", ["hermes", "codex"])
async def test_local_pool_cap_is_independent_of_subscription_harness_caps(
    ctx: AppContext,
    client: TestClient,
    provider: FakeProvider,
    tokens: dict[str, str],
    local_harness: str,
) -> None:
    supervisor = make_supervisor(ctx, provider)
    admin = {"Authorization": f"Bearer {tokens['admin']}"}
    routing = client.get("/v1/routing/default-routing/4").json()["document"]
    routing["version"] = 41
    hermes = next(model for model in routing["models"] if model["harness"] == "hermes")
    hermes.update(
        {
            "harness": local_harness,
            "endpoint_url": "http://192.0.2.41:11434/v1",
            "enabled": True,
            "disabled_reason": None,
        }
    )
    assert (
        client.put("/v1/routing/default-routing/41", json=routing, headers=admin).status_code == 200
    )
    policy = client.get("/v1/policies/default-software/4").json()["document"]
    policy["version"] = 41
    policy["routing"]["policy"]["version"] = 41
    assert (
        client.put("/v1/policies/default-software/41", json=policy, headers=admin).status_code
        == 200
    )

    operator = {"Authorization": f"Bearer {tokens['operator']}"}
    task_ids: list[str] = []
    for number in range(5):
        external_id = f"EX-LOCAL-{number}"
        document = contract_document(external_id=external_id)
        document["repository"]["work_branch"] = f"crucible/{external_id}"
        document["policy"] = {"name": "default-software", "version": 41}
        document["required_verification"].append(
            {"id": "V9", "command": "test -f made-by-the-worker", "expect_exit": 0}
        )
        document["execution_request"].update(
            {
                "harness": local_harness,
                "model": "gpt-oss:120b",
                "pin_reason": "exercise the Spark pool cap",
                "image": "crucible-worker:fake-hang",
            }
        )
        response = client.post("/v1/tasks", json=document, headers=operator)
        assert response.status_code == 201, response.text
        task_id = response.json()["id"]
        response = client.post(
            f"/v1/tasks/{task_id}/start",
            json={
                "provider": "fake",
                "image": "crucible-worker:fake-hang",
                "policy_version": 41,
            },
            headers=operator,
        )
        assert response.status_code == 200, response.text
        task_ids.append(task_id)

    for _ in range(3):
        await supervisor.tick()
    states = [client.get(f"/v1/tasks/{task_id}").json()["state"] for task_id in task_ids]
    assert states.count("running") == 4
    assert states.count("scheduled") == 1
    assert "harness_launch_deferred" in event_kinds(client, task_ids[-1])


async def test_an_empty_hermes_credential_directory_keeps_the_no_key_fallback(
    ctx: AppContext,
    client: TestClient,
    provider: FakeProvider,
    tokens: dict[str, str],
    tmp_path: Path,
    engine: Engine,
) -> None:
    """Compose's credential-init creates the Hermes directory empty and the service
    registers it. Until `api-key` exists the launch must use the no-key fallback, not
    point OPENAI_API_KEY at a file that is not there."""
    credential_dir = tmp_path / "credentials" / "hermes"
    credential_dir.mkdir(parents=True)
    supervisor = make_supervisor(
        ctx, provider, credential_sources={"hermes": CredentialSource(str(credential_dir))}
    )
    admin = {"Authorization": f"Bearer {tokens['admin']}"}
    routing = client.get("/v1/routing/default-routing/4").json()["document"]
    routing["version"] = 42
    hermes = next(model for model in routing["models"] if model["harness"] == "hermes")
    hermes.update(
        {"endpoint_url": "http://192.0.2.41:11434/v1", "enabled": True, "disabled_reason": None}
    )
    assert (
        client.put("/v1/routing/default-routing/42", json=routing, headers=admin).status_code == 200
    )
    policy = client.get("/v1/policies/default-software/4").json()["document"]
    policy["version"] = 42
    policy["routing"]["policy"]["version"] = 42
    assert (
        client.put("/v1/policies/default-software/42", json=policy, headers=admin).status_code
        == 200
    )
    operator = {"Authorization": f"Bearer {tokens['operator']}"}

    async def launch_env(external_id: str) -> tuple[dict[str, str], dict[str, str]]:
        document = contract_document(external_id=external_id)
        document["repository"]["work_branch"] = f"crucible/{external_id}"
        document["policy"] = {"name": "default-software", "version": 42}
        document["required_verification"].append(
            {"id": "V9", "command": "test -f made-by-the-worker", "expect_exit": 0}
        )
        document["execution_request"].update(
            {
                "harness": "hermes",
                "model": hermes["id"],
                "pin_reason": "exercise the Hermes credential fallback",
                "image": "crucible-worker:fake-hang",
            }
        )
        response = client.post("/v1/tasks", json=document, headers=operator)
        assert response.status_code == 201, response.text
        task_id = response.json()["id"]
        response = client.post(
            f"/v1/tasks/{task_id}/start",
            json={
                "provider": "fake",
                "image": "crucible-worker:fake-hang",
                "policy_version": 42,
            },
            headers=operator,
        )
        assert response.status_code == 200, response.text
        before = set(provider._workers)
        for _ in range(3):
            await supervisor.tick()
        (attempt_id,) = set(provider._workers) - before
        spec = provider._workers[attempt_id].spec
        return dict(spec.env), dict(spec.env_from_files)

    try:
        env, from_files = await launch_env("EX-HERMES-EMPTY")
        assert env["OPENAI_API_KEY"] == "local-no-auth"
        assert from_files == {}

        (credential_dir / "api-key").write_text("placeholder-for-the-test\n", encoding="utf-8")
        _, from_files = await launch_env("EX-HERMES-KEYED")
        assert from_files == {"OPENAI_API_KEY": "/home/worker/.hermes-auth/api-key"}
    finally:
        # Policies outlive the per-test truncate; a version 42 left in force would hand
        # later tests an endpoint they did not configure.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE policies SET retired_at = now() "
                    "WHERE name='default-software' AND version=42"
                )
            )


async def test_a_disabled_harness_is_a_contract_problem_at_submit(
    ctx: AppContext, client: TestClient, tokens: dict[str, str]
) -> None:
    """25 (review I8): refused with the reason when the contract is submitted, not a
    task later at launch."""
    _disable(ctx, "codex", "rotating the dedicated credential")
    response = client.post(
        "/v1/tasks",
        json=_pinned_document("EX-DISABLED-SUBMIT", "crucible-worker:fake-succeed"),
        headers={"Authorization": f"Bearer {tokens['operator']}"},
    )
    assert response.status_code == 422, response.text
    problems = response.json()["errors"]
    assert any(
        p["path"] == "execution_request.harness" and "rotating" in p["message"] for p in problems
    ), problems


async def test_the_cap_holds_until_the_copy_is_synced_and_removed(
    ctx: AppContext, client: TestClient, provider: FakeProvider
) -> None:
    """12 (review I2): an attempt in terminating or exited still holds its credential
    copy, so a second seeding must wait for collect."""
    from crucible.domain.lifecycle import AttemptState  # noqa: PLC0415

    supervisor = make_supervisor(ctx, provider)
    first = submit_and_start(client, "crucible-worker:fake-hang", external_id="EX-0001")
    await supervisor.tick()
    await supervisor.tick()
    view = client.get(f"/v1/tasks/{first}").json()
    attempt_id = view["latest_attempt"]["id"]
    assert view["latest_attempt"]["state"] == "running"
    with ctx.uow_factory() as uow:
        execution = uow.executions.get(view["executions"][0]["id"])
        assert execution is not None
        assert supervisor._harness_busy(execution) is not None
    for state in (AttemptState.TERMINATING, AttemptState.EXITED):
        with supervisor._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            attempt.state = state
            uow.attempts.save(attempt)
            uow.commit()
        with ctx.uow_factory() as uow:
            assert supervisor._harness_busy(execution) is not None, state
    with supervisor._fenced() as uow:
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert attempt is not None
        attempt.state = AttemptState.COLLECTED
        uow.attempts.save(attempt)
        uow.commit()
    with ctx.uow_factory() as uow:
        assert supervisor._harness_busy(execution) is None


async def test_a_lost_worker_has_its_credential_copy_discarded(
    ctx: AppContext, client: TestClient, provider: FakeProvider, supervisor: Supervisor
) -> None:
    """12 (review I1): a lost worker is never collected, so the provider is told to
    discard its copy."""
    task_id = submit_and_start(client, "crucible-worker:fake-vanish-1")
    await run_to_settled(supervisor, client, task_id)
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["executions"][0]["attempts"][0]["id"]
    assert attempt_id in provider.discarded


async def test_a_token_shaped_worker_log_line_is_stored_redacted(
    ctx: AppContext, client: TestClient, supervisor: Supervisor
) -> None:
    """12 (review I5): provider log capture passes through the redaction filter."""
    from sqlalchemy import text  # noqa: PLC0415

    from crucible.ports.execution import LogChunk  # noqa: PLC0415

    token = "sk-ant-" + "oat01-" + "x" * 40
    line = f"auth: using {token} for the session\n".encode()
    task_id = submit_and_start(client, "crucible-worker:fake-hang")
    await supervisor.tick()
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    stored = supervisor._store_logs(attempt_id, (LogChunk("stdout", line, lines=1),))
    assert stored == 1
    with ctx.engine.begin() as connection:
        rows = (
            connection.execute(
                text("SELECT content FROM log_chunks WHERE attempt_id = :id"), {"id": attempt_id}
            )
            .scalars()
            .all()
        )
    blob = b"".join(bytes(r) for r in rows).decode("utf-8", "replace")
    assert token not in blob
    assert "[redacted:anthropic_oauth_token]" in blob


async def test_progress_lines_become_unverified_worker_events(
    ctx: AppContext, client: TestClient, supervisor: Supervisor
) -> None:
    """07 (review C4): each progress line is an event with the worker as source, never
    verified, bounded and redacted."""
    from crucible.application.harnesses import ingest_progress  # noqa: PLC0415

    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    view = client.get(f"/v1/tasks/{task_id}").json()
    attempt = view["latest_attempt"]
    token = "sk-ant-" + "oat01-" + "x" * 40
    with ctx.uow_factory() as uow:
        count = ingest_progress(
            uow,
            ctx.clock,
            attempt_id=attempt["id"],
            task_id=task_id,
            execution_id=view["executions"][0]["id"],
            progress=[
                {"milestone": "read identity"},
                {"milestone": "edited file", "note": f"using {token}"},
            ],
        )
        uow.commit()
    assert count == 2
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    progress = [e for e in events if e["kind"] == "worker_progress"]
    assert len(progress) == 2
    assert all(e["principal"] == "worker" and e["verified"] is False for e in progress)
    assert "read identity" in progress[0]["payload"]["line"]
    assert token not in progress[1]["payload"]["line"]
    assert "[redacted:anthropic_oauth_token]" in progress[1]["payload"]["line"]


# ----- hades #174: the configuration is the starting value, the admin decides -------------

UNVERIFIED = "unverified: Crucible-side refresh not yet observed (S1b step 5)"


async def test_an_administrators_enable_replaces_the_configuration_default_without_a_restart(
    ctx: AppContext, provider: FakeProvider, tokens: dict[str, str]
) -> None:
    """Codex ships off in configuration. On a deployment upgraded with that default (its
    row undecided), a task naming it is refused; one enable through the admin API, on
    the same running service and supervisor, lets the next task route and run; the
    decision is audited with the configuration's warning; a disable refuses again."""
    gates = {"codex": HarnessGate(enabled=False, reason=UNVERIFIED)}
    ctx.harness_gates = gates
    assert ctx.harnesses is not None
    ctx.admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        providers={"fake": provider},
        harnesses=ctx.harnesses,
        harness_gates=gates,
    )
    with ctx.uow_factory() as uow:
        # What an upgraded deployment has: a row nobody decided (0027).
        state = uow.harnesses.get("codex")
        assert state is not None
        state.enabled_decided = False
        uow.harnesses.put(state)
        uow.commit()
    supervisor = make_supervisor(ctx, provider, harness_gates=gates)
    await supervisor.tick()
    admin_headers = {"Authorization": f"Bearer {tokens['admin']}"}
    operator_headers = {"Authorization": f"Bearer {tokens['operator']}"}
    orchestrator = {"Authorization": f"Bearer {tokens['orchestrator']}"}
    with TestClient(create_app(ctx), headers=orchestrator) as client:
        listed = client.get("/v1/admin/harnesses", headers=admin_headers).json()["items"]
        codex = next(h for h in listed if h["name"] == "codex")
        assert codex["enabled"] is False and codex["decided_by_administrator"] is False
        assert codex["warning"] == UNVERIFIED

        refused = client.post(
            "/v1/tasks",
            json=_pinned_document("EX-174-BEFORE", "crucible-worker:fake-succeed"),
            headers=operator_headers,
        )
        assert refused.status_code == 422, refused.text
        assert "configuration default" in refused.text

        enabled = client.post(
            "/v1/admin/harnesses/codex/enable",
            json={"reason": "operator: try codex anyway"},
            headers=admin_headers,
        )
        assert enabled.status_code == 200, enabled.text
        body = enabled.json()
        assert body["enabled"] is True and body["decided_by_administrator"] is True
        assert body["warning"] == UNVERIFIED

        listed = client.get("/v1/admin/harnesses", headers=admin_headers).json()["items"]
        codex = next(h for h in listed if h["name"] == "codex")
        assert codex["enabled"] is True and codex["enabled_by_configuration"] is False
        assert codex["warning"] == UNVERIFIED

        task_id = _submit_pinned(client, tokens, "crucible-worker:fake-succeed", "EX-174-AFTER")
        assert await run_to_settled(supervisor, client, task_id) == "publishing"
        assert "harness_refused" not in event_kinds(client, task_id)

        audit = client.get("/v1/admin/audit", params={"limit": 200}, headers=admin_headers)
        decision = next(
            e
            for e in audit.json()["items"]
            if e["kind"] == "harness_enabled" and e["principal"] == "admin-principal"
        )
        assert decision["payload"]["harness"] == "codex"
        assert decision["payload"]["reason"] == "operator: try codex anyway"
        assert decision["payload"]["configuration_warning"] == UNVERIFIED
        assert decision["payload"]["before"]["enabled_decided"] is False
        assert decision["payload"]["after"]["enabled_decided"] is True

        disabled = client.post(
            "/v1/admin/harnesses/codex/disable",
            json={"reason": "operator: back off"},
            headers=admin_headers,
        )
        assert disabled.status_code == 200 and disabled.json()["enabled"] is False
        again = client.post(
            "/v1/tasks",
            json=_pinned_document("EX-174-DISABLED", "crucible-worker:fake-succeed"),
            headers=operator_headers,
        )
        assert again.status_code == 422 and "disabled by an administrator" in again.text
