"""Device tokens (hades #576, U9) and attempt usage (hades #604) against Postgres: the
device row, its one exchange and its revocation through the real unit of work, and the
usage a provider read while collecting, on the attempt, the task and routing history."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.admin.context import AdminContext
from crucible.application.supervisor import Supervisor
from crucible.ports.harness import ReportMetrics
from tests.integration.conftest import run_to_settled, submit_and_start

pytestmark = pytest.mark.integration


@pytest.fixture
def admin_client(ctx: AppContext, tokens: dict[str, str]) -> TestClient:
    assert ctx.harnesses is not None
    ctx.admin = AdminContext(
        uow_factory=ctx.uow_factory, clock=ctx.clock, providers={}, harnesses=ctx.harnesses
    )
    return TestClient(create_app(ctx), base_url="https://hades.test")


def _admin(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['admin']}"}


def test_a_device_token_lives_its_whole_life_in_postgres(
    admin_client: TestClient, tokens: dict[str, str], ctx: AppContext
) -> None:
    minted = admin_client.post(
        "/v1/admin/devices", json={"name": "ops phone"}, headers=_admin(tokens)
    )
    assert minted.status_code == 200, minted.text
    device = minted.json()
    token = device["token"]
    assert device["role"] == "operator"
    phone = {"Authorization": f"Bearer {token}", "User-Agent": "Hades iOS app"}

    tasks = admin_client.get("/v1/tasks", headers=phone)
    assert tasks.status_code == 200, tasks.text
    listed = admin_client.get("/v1/admin/devices", headers=_admin(tokens))
    assert token not in listed.text
    (item,) = listed.json()["items"]
    assert item["last_user_agent"] == "Hades iOS app"
    assert item["last_used_at"] is not None

    exchanged = admin_client.post(
        "/ui/device-sign-in", data={"token": token}, follow_redirects=False
    )
    assert exchanged.status_code == 303, exchanged.text
    assert admin_client.get("/ui/board").status_code == 200
    again = admin_client.post("/ui/device-sign-in", data={"token": token}, follow_redirects=False)
    assert again.status_code == 409

    revoked = admin_client.post(
        f"/v1/admin/devices/{device['id']}/revoke",
        json={"reason": "the phone was replaced"},
        headers=_admin(tokens),
    )
    assert revoked.status_code == 200, revoked.text
    assert admin_client.get("/v1/tasks", headers=phone).status_code == 401
    board = admin_client.get("/ui/board", follow_redirects=False)
    assert board.status_code == 303 and board.headers["location"].startswith("/ui/sign-in")

    audit = admin_client.get("/v1/admin/audit", headers=_admin(tokens)).json()["items"]
    kinds = [entry["kind"] for entry in audit]
    assert kinds.count("device_token_minted") == 1
    assert kinds.count("device_token_used") == 2
    assert kinds.count("device_token_revoked") == 1
    assert token not in str(audit)


async def test_usage_reaches_the_attempt_the_task_and_routing_history(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    provider.set_metrics(
        "EX-0001",
        ReportMetrics(
            model="claude-sonnet-5",
            tokens_in=1200,
            tokens_out=45,
            cost_usd=0.0031,
            source="harness_transcript",
            tokens_cache_read=800,
        ),
    )
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)

    (row,) = client.get("/v1/routing/history", params={"model": "claude-sonnet-5"}).json()["items"]
    assert (row["tokens_in"], row["tokens_out"], row["tokens_cache_read"]) == (1200, 45, 800)
    assert row["cost_units"] == pytest.approx(0.0031)
    assert row["cost_source"] == "harness_transcript"

    task = client.get(f"/v1/tasks/{task_id}").json()
    latest = task["latest_attempt"]
    assert latest["usage"]["tokens_in"] == 1200
    assert latest["usage"]["tokens_cache_read"] == 800
    assert task["usage"]["tokens_out"] == 45
    assert task["usage"]["cost_units"] == pytest.approx(0.0031)

    attempt = client.get(f"/v1/attempts/{latest['id']}").json()
    assert attempt["usage"] == {
        "schema_version": attempt["schema_version"],
        "tokens_in": 1200,
        "tokens_out": 45,
        "tokens_cache_read": 800,
        "cost_units": pytest.approx(0.0031),
        "cost_source": "harness_transcript",
        "model_reported": "claude-sonnet-5",
    }
