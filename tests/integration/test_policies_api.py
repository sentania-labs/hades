"""Policy and routing API (04, 05b): immutability once referenced, operator-only
settings, contract validation against the routing policy, usage, and artifacts."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from crucible.adapters.api.deps import AppContext
from crucible.application.errors import ForbiddenError
from crucible.application.policies import put_policy
from crucible.application.supervisor import Supervisor
from crucible.ports.harness import CredentialSource, MountMode
from tests.fixtures import contract_document
from tests.integration.conftest import run_to_settled, submit_and_start
from tests.unit.test_policy_schema import seeded_policy

pytestmark = pytest.mark.integration


def admin(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['admin']}"}


def test_read_the_seeded_policy_and_routing_policy(client: TestClient) -> None:
    body = client.get("/v1/policies/default-software/2").json()
    assert body["name"] == "default-software" and body["referenced"] is False
    assert body["document"]["routing"]["policy"] == {"name": "default-routing", "version": 2}
    routing = client.get("/v1/routing/default-routing/2").json()
    assert any(m["model"] == "gpt-5.6-luna" for m in routing["document"]["models"])


def test_upload_a_new_policy_version(client: TestClient, tokens: dict[str, str]) -> None:
    # `policies` is not truncated between tests and the usage report follows the newest
    # version, so the upload derives from the current seed (version 2, routing version 2).
    document = client.get("/v1/policies/default-software/2").json()["document"]
    document["version"] = 3
    document["limits"]["grace_seconds"] = 45
    r = client.put("/v1/policies/default-software/3", json=document, headers=admin(tokens))
    assert r.status_code == 200, r.text
    assert (
        client.get("/v1/policies/default-software/3").json()["document"]["limits"]["grace_seconds"]
        == 45
    )


def test_upload_requires_admin(client: TestClient) -> None:
    document = seeded_policy()
    document["version"] = 3
    assert client.put("/v1/policies/default-software/3", json=document).status_code == 403


def test_an_invalid_policy_is_refused_with_paths(
    client: TestClient, tokens: dict[str, str]
) -> None:
    document = seeded_policy()
    document["version"] = 4
    document["gates"]["pre_pr"].remove("no_secrets")
    r = client.put("/v1/policies/default-software/4", json=document, headers=admin(tokens))
    assert r.status_code == 422
    assert any("gates" in e["path"] for e in r.json()["errors"])


def test_the_path_and_the_document_must_agree(client: TestClient, tokens: dict[str, str]) -> None:
    document = seeded_policy()
    document["version"] = 9
    r = client.put("/v1/policies/default-software/5", json=document, headers=admin(tokens))
    assert r.status_code == 422
    assert any(e["path"] == "version" for e in r.json()["errors"])


@pytest.mark.parametrize("harness", ["claude_code", "agy"])
def test_frontier_harnesses_accept_parallel_concurrency(
    client: TestClient, tokens: dict[str, str], harness: str
) -> None:
    """05b: read-only credentials or declared parallel safety permit higher caps."""
    document = seeded_policy()
    document["version"] = 6
    document["concurrency"]["per_harness"][harness] = 2
    r = client.put("/v1/policies/default-software/6", json=document, headers=admin(tokens))
    assert r.status_code == 200


def test_codex_copy_mode_stays_serial(
    client: TestClient, tokens: dict[str, str], ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """12: Codex copy mode has no parallel safety; a cap above 1 is refused."""
    monkeypatch.setitem(
        ctx.credential_sources, "codex", CredentialSource("/credential", MountMode.RW_NARROW)
    )
    document = seeded_policy()
    document["version"] = 6
    document["concurrency"]["per_harness"]["codex"] = 2
    r = client.put("/v1/policies/default-software/6", json=document, headers=admin(tokens))
    assert r.status_code == 422
    assert any(e["path"] == "concurrency.per_harness.codex" for e in r.json()["errors"])


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("ci_certification", "allow_no_ci", True),
        ("deliverables", "allow_branch_only", True),
        ("release", "require_operator_approval", False),
    ],
)
def test_an_operator_principal_may_set_an_operator_only_field(
    ctx: AppContext, tokens: dict[str, str], section: str, field: str, value: bool
) -> None:
    """05b: these three may only be uploaded by an operator or admin, and each is
    recorded as a decision. The use case is called directly, because the route's own
    admin check would refuse an operator principal before this rule is reached."""
    document = seeded_policy()
    document["version"] = 7
    document[section][field] = value
    with ctx.uow_factory() as uow:
        operator = uow.principals.get_by_name("operator-principal")
        assert operator is not None
        put_policy(
            uow,
            ctx.clock,
            principal=operator,
            name="default-software",
            version=7,
            document=document,
        )
        uow.commit()
    with ctx.uow_factory() as uow:
        stored = uow.policies.get("default-software", 7)
        assert stored is not None and stored.document[section][field] is value
    with ctx.engine.connect() as conn:
        recorded = conn.execute(
            text("SELECT kind, resolves, verbatim FROM decisions ORDER BY id")
        ).all()
    kinds = {row[0] for row in recorded}
    assert kinds == {"policy_operator_setting"}
    assert {row[1] for row in recorded} == {f"{section}.{field}"}
    assert all("operator-principal" in row[2] for row in recorded)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("ci_certification", "allow_no_ci", True),
        ("deliverables", "allow_branch_only", True),
        ("release", "require_operator_approval", False),
    ],
)
def test_an_orchestrator_principal_may_not_set_an_operator_only_field(
    ctx: AppContext, tokens: dict[str, str], section: str, field: str, value: bool
) -> None:
    document = seeded_policy()
    document["version"] = 9
    document[section][field] = value
    with ctx.uow_factory() as uow:
        orchestrator = uow.principals.get_by_name("orchestrator-principal")
        assert orchestrator is not None
        with pytest.raises(ForbiddenError) as exc:
            put_policy(
                uow,
                ctx.clock,
                principal=orchestrator,
                name="default-software",
                version=9,
                document=document,
            )
    assert [e["path"] for e in exc.value.errors] == [f"{section}.{field}"]
    with ctx.uow_factory() as uow:
        assert uow.policies.get("default-software", 9) is None


def test_the_upload_route_is_admin_only(client: TestClient, tokens: dict[str, str]) -> None:
    document = seeded_policy()
    document["version"] = 10
    for role in ("orchestrator", "operator", "observer"):
        r = client.put(
            "/v1/policies/default-software/10",
            json=document,
            headers={"Authorization": f"Bearer {tokens[role]}"},
        )
        assert r.status_code == 403, role
    assert (
        client.put(
            "/v1/policies/default-software/10", json=document, headers=admin(tokens)
        ).status_code
        == 200
    )


def test_a_referenced_policy_version_is_immutable(
    client: TestClient, tokens: dict[str, str]
) -> None:
    client.post("/v1/tasks", json=contract_document())
    document = client.get("/v1/policies/default-software/2").json()["document"]
    r = client.put("/v1/policies/default-software/2", json=document, headers=admin(tokens))
    assert r.status_code == 409
    assert "immutable" in r.json()["detail"]
    assert client.get("/v1/policies/default-software/2").json()["referenced"] is True


def test_a_referenced_routing_policy_is_immutable(
    client: TestClient, tokens: dict[str, str]
) -> None:
    document = client.get("/v1/routing/default-routing/2").json()["document"]
    r = client.put("/v1/routing/default-routing/2", json=document, headers=admin(tokens))
    assert r.status_code == 409


def test_upload_a_new_routing_policy_version(client: TestClient, tokens: dict[str, str]) -> None:
    # The migrations seed several versions, so the new one is the next free number.
    version = 3
    while client.get(f"/v1/routing/default-routing/{version}").status_code == 200:
        version += 1
    document = copy.deepcopy(client.get("/v1/routing/default-routing/2").json()["document"])
    document["version"] = version
    document["models"][1]["enabled"] = False
    r = client.put(f"/v1/routing/default-routing/{version}", json=document, headers=admin(tokens))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    ("patch", "path"),
    [
        ({"model": "no-such-model"}, "execution_request.model"),
        ({"model": "claude-sonnet-5"}, "execution_request.harness"),
        ({"model": "gpt-5.6-sol", "tier": "trivial"}, "execution_request.model"),
        ({"model": "local-rtx-small", "tier": "trivial"}, "execution_request.model"),
    ],
)
def test_a_contract_is_validated_against_the_routing_policy(
    client: TestClient, tokens: dict[str, str], patch: dict[str, str], path: str
) -> None:
    document = contract_document()
    document["execution_request"] = {
        **document["execution_request"],
        "harness": "codex",
        "model": "gpt-5.6-luna",
        "pin_reason": "routing policy validation test",
        **patch,
    }
    r = client.post(
        "/v1/tasks",
        json=document,
        headers={"Authorization": f"Bearer {tokens['operator']}"},
    )
    assert r.status_code == 422, r.text
    assert any(e["path"] == path for e in r.json()["errors"])


def test_a_complex_tier_may_name_a_frontier_model(
    client: TestClient, tokens: dict[str, str]
) -> None:
    document = contract_document()
    document["execution_request"] = {
        **document["execution_request"],
        "tier": "complex",
        "harness": "codex",
        "model": "gpt-5.6-sol",
        "pin_reason": "routing policy validation test",
    }
    assert (
        client.post(
            "/v1/tasks",
            json=document,
            headers={"Authorization": f"Bearer {tokens['operator']}"},
        ).status_code
        == 201
    )


def test_routing_usage_reports_every_pool(client: TestClient) -> None:
    # Without `policy_version` the report follows the newest policy version; this module
    # uploads several, so the seeded version 2 is named explicitly.
    body = client.get("/v1/routing/usage", params={"policy_version": 2}).json()
    assert body["routing_policy"] == {"name": "default-routing", "version": 2}
    pools = {p["pool"]: p for p in body["pools"]}
    assert set(pools) == {"anthropic-sub", "openai-sub", "google-sub"}
    assert pools["openai-sub"]["soft_limit"] == 0
    assert pools["openai-sub"]["over_soft_limit"] is False


async def test_usage_counts_attempts_when_no_tokens_are_reported(
    client: TestClient, supervisor: Supervisor
) -> None:
    """05b: a harness that reports no token counts falls back to counting attempts."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    usage = client.get("/v1/routing/usage", params={"policy_version": 2}).json()
    pools = {p["pool"]: p for p in usage["pools"]}
    anthropic = pools["anthropic-sub"]
    assert anthropic["attempts"] == 1
    assert anthropic["fallback_to_attempts"] is True and anthropic["counting"] == "attempts"


async def test_routing_history_filters_by_model_and_project(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    assert client.get("/v1/routing/history", params={"model": "gpt-5.6-sol"}).json()["items"] == []
    rows = client.get(
        "/v1/routing/history",
        params={"model": "claude-sonnet-5", "project": "example-service"},
    ).json()["items"]
    assert len(rows) == 1 and rows[0]["task_id"] == task_id
    assert client.get("/v1/routing/history", params={"project": "nothing"}).json()["items"] == []
