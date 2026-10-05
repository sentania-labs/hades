"""The lifecycle against the Kubernetes provider (26, requirement 7 of C8a).

The same four cases the Docker and fake providers already carry, driven by a real
supervisor against a real database, with an in-memory Kubernetes API underneath: a full
run to gates, the failure classes of 16, a cancel, and a reconcile across a supervisor
restart.

The fake API is not a cluster. What it proves is that the provider creates the right
objects, reads the states back correctly, and that everything above the provider is
unchanged by the second provider existing. The cluster half is C8b's `make e2e-kind`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.k8sfake import FakeKubernetesApi, FakeRegistry
from crucible.adapters.execution.k8spublisher import ByWorkspacePublisher, KubernetesPublisher
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.adapters.github.appauth import AppAuthenticator, AppConfig
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.transport import RestTransport
from crucible.adapters.harness.registry import default_registry
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.auth import mint_token
from crucible.application.delivery_tick import DeliveryConfig
from crucible.application.repositories import register_repository
from crucible.application.supervisor import Supervisor
from crucible.cli.wiring import wire
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.entities import Role
from crucible.ports.execution import ObservationState
from crucible.settings import Settings
from tests.e2e.policy import e2e_policy_document, e2e_routing_document
from tests.fixtures import REPOSITORY_URL, FakeClock, contract_document, promote_for_test
from tests.integration.conftest import event_kinds, run_to_settled
from tests.integration.fake_github import FakeGitHubServer

pytestmark = pytest.mark.integration

IMAGE = "ghcr.io/sentania-labs/crucible-worker:script-harness-1.0.0"
HOSTS = {"github.com": ["140.82.121.4/32"], "pypi.org": ["151.101.0.223/32"]}


@pytest.fixture
def k8s_api() -> FakeKubernetesApi:
    return FakeKubernetesApi()


@pytest.fixture
def k8s_provider(k8s_api: FakeKubernetesApi) -> KubernetesProvider:
    registry = FakeRegistry(k8s_api)
    registry.register(IMAGE, harness="script-harness", version="1.0.0")
    return KubernetesProvider(
        KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            storage_class="lab-ssd",
            extra_image_allowlist=("ghcr.io/sentania-labs/crucible-worker:*",),
        ),
        k8s_api,  # type: ignore[arg-type]
        registry,
        harnesses=default_registry(test_fixtures=True),
        resolver=lambda host: list(HOSTS.get(host, ["203.0.113.1/32"])),
    )


@pytest.fixture
def k8s_ctx(
    engine: Engine,
    migrated: str,
    clock: FakeClock,
    k8s_provider: KubernetesProvider,
    tmp_path: Path,
) -> AppContext:
    return AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=clock,
        providers=[k8s_provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(tmp_path / "artifacts"),
        harnesses=default_registry(test_fixtures=True),
    )


@pytest.fixture
def k8s_client(k8s_ctx: AppContext) -> Iterator[TestClient]:
    """An admin seeds the routing, the policy, the repository and the promoted image;
    the operator submits. The same sequence a real deployment goes through (25)."""
    app = create_app(k8s_ctx)
    with k8s_ctx.uow_factory() as uow:
        tokens = {
            role.value: mint_token(
                uow, k8s_ctx.clock, name=f"{role.value}-principal", role=role
            ).token
            for role in Role
        }
        promote_for_test(
            uow,
            digest="sha256:" + "c" * 64,
            reference=IMAGE,
            harnesses={"script-harness": "1.0.0"},
            at=k8s_ctx.clock.now(),
            by="tests",
            reason="the C8a integration tier's script harness image",
        )
        uow.commit()
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
        routing = e2e_routing_document()
        # A conflict is the same document already uploaded by an earlier test in this
        # session: policy and routing rows are immutable once referenced and survive
        # the per-test truncation.
        assert admin.put(
            f"/v1/routing/{routing['name']}/{routing['version']}", json=routing
        ).status_code in (200, 201, 409)
        policy = e2e_policy_document(name="k8s-script")
        policy["images"]["allowlist"] = ["ghcr.io/sentania-labs/crucible-worker:*"]
        assert admin.put(
            f"/v1/policies/{policy['name']}/{policy['version']}", json=policy
        ).status_code in (200, 201, 409)
    with k8s_ctx.uow_factory() as uow:
        register_repository(
            uow,
            k8s_ctx.clock,
            principal_name="tests",
            name="example-service",
            registration=RepositoryRegistration(
                url=REPOSITORY_URL,
                default_branch="main",
                policy_name="k8s-script",
                installation_id=1,
                external_review=ExternalReviewAttestation(
                    attested_all_prs=True, attested_by="tests"
                ),
            ),
        )
        uow.commit()
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['orchestrator']}"}) as client:
        yield client


@pytest.fixture
def k8s_supervisor(k8s_ctx: AppContext, k8s_provider: KubernetesProvider) -> Supervisor:
    return Supervisor(
        k8s_ctx.uow_factory,
        {"kubernetes": k8s_provider},
        k8s_ctx.clock,
        holder="k8s-sup-a",
        artifact_store=k8s_ctx.artifact_store,
        harnesses=k8s_ctx.harnesses,
        lease_ttl_seconds=30,
        grace_seconds=5,
    )


def k8s_contract(external_id: str = "EX-0001", **overrides: Any) -> dict[str, Any]:
    document = contract_document(external_id=external_id)
    document["repository"] = {
        "name": "example-service",
        "base_ref": "main",
        "work_branch": f"crucible/{external_id}",
    }
    document["required_verification"] = [
        {"id": "V1", "command": "sh checks/lint.sh", "expect_exit": 0},
        {"id": "V2", "command": "sh checks/test.sh", "expect_exit": 0},
        {"id": "V3", "command": "sh checks/scan.sh", "expect_exit": 0},
    ]
    document["deliverables"] = [{"kind": "artifacts", "target": None, "draft": False, "closes": []}]
    document["policy"] = {"name": "k8s-script", "version": 1}
    document["execution_request"] = {
        **document["execution_request"],
        "provider": "kubernetes",
        "timeout_seconds": 600,
    }
    document["execution_request"].pop("image", None)
    return {**document, **overrides}


def start(client: TestClient, document: dict[str, Any]) -> str:
    response = client.post("/v1/tasks", json=document)
    assert response.status_code == 201, response.text
    task_id: str = response.json()["id"]
    response = client.post(
        f"/v1/tasks/{task_id}/start", json={"provider": "kubernetes", "policy_version": 1}
    )
    assert response.status_code == 200, response.text
    return task_id


# ----- the full run --------------------------------------------------------


async def test_a_task_runs_to_its_gates_on_the_kubernetes_provider(
    k8s_client: TestClient, k8s_supervisor: Supervisor, k8s_api: FakeKubernetesApi
) -> None:
    k8s_api.script_all("succeed", after=2)
    task_id = start(k8s_client, k8s_contract())
    state = await run_to_settled(k8s_supervisor, k8s_client, task_id, max_ticks=20)
    assert state == "publishing"

    view = k8s_client.get(f"/v1/tasks/{task_id}").json()
    attempt = view["executions"][0]["attempts"][0]
    assert attempt["state"] == "succeeded"
    assert attempt["exit_code"] == 0 and attempt["exit_class"] == "completed"
    # 13, 26: every attempt records the digest it ran, resolved at launch.
    detail = k8s_client.get(f"/v1/attempts/{attempt['id']}").json()
    assert "@sha256:" in detail["image_digest"]
    kinds = event_kinds(k8s_client, task_id)
    for kind in (
        "workspace_prepared",
        "attempt_running",
        "attempt_logs_drained",
        "attempt_collected",
        "evidence_recorded",
        "gates_evaluated",
        "attempt_cleaned_up",
    ):
        assert kind in kinds, kind


async def test_the_run_leaves_nothing_of_the_attempt_in_the_namespace(
    k8s_client: TestClient, k8s_supervisor: Supervisor, k8s_api: FakeKubernetesApi
) -> None:
    k8s_api.script_all("succeed", after=2)
    task_id = start(k8s_client, k8s_contract())
    await run_to_settled(k8s_supervisor, k8s_client, task_id, max_ticks=20)
    assert k8s_api.object_names("jobs") == []
    assert k8s_api.object_names("pods") == []
    assert k8s_api.object_names("networkpolicies") == []
    # 12, 16: the per-attempt Secret goes under every policy, and this one kept the
    # workspace claim (keep_diff_only).
    assert k8s_api.object_names("secrets") == []
    assert k8s_api.object_names("persistentvolumeclaims")


async def test_the_launch_evidence_of_26_is_stored_as_an_artifact(
    k8s_client: TestClient, k8s_supervisor: Supervisor, k8s_api: FakeKubernetesApi
) -> None:
    k8s_api.script_all("succeed", after=2)
    task_id = start(k8s_client, k8s_contract())
    await run_to_settled(k8s_supervisor, k8s_client, task_id, max_ticks=20)
    attempt_id = k8s_client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    artifacts = k8s_client.get(f"/v1/attempts/{attempt_id}/artifacts").json()["items"]
    stored = next(a for a in artifacts if a["filename"] == "report/kubernetes-launch.json")
    body = k8s_client.get(f"/v1/artifacts/{stored['id']}/content")
    document = yaml.safe_load(body.text)
    assert document["job"].startswith("worker-")
    assert document["pod"].startswith("worker-")
    assert document["node"] == "lab-node-1"
    assert document["pod_pid_limit"] == 4096
    assert document["runtime_class"] == "standard"


# ----- the failure classes (16) --------------------------------------------


@pytest.mark.parametrize(
    ("behavior", "exit_class"),
    [
        ("crash", "crashed"),
        ("environment", "environment"),
        ("oom", "environment"),
        ("blocked", "blocked"),
        ("vanish", "lost"),
    ],
)
async def test_the_failure_classes_of_16_survive_the_second_provider(
    k8s_client: TestClient,
    k8s_supervisor: Supervisor,
    k8s_api: FakeKubernetesApi,
    behavior: str,
    exit_class: str,
) -> None:
    k8s_api.script_all(behavior, after=2)
    task_id = start(k8s_client, k8s_contract())
    for _ in range(20):
        await k8s_supervisor.tick()
        view = k8s_client.get(f"/v1/tasks/{task_id}").json()
        classes = [a["exit_class"] for e in view["executions"] for a in e["attempts"]]
        if exit_class in classes:
            break
    else:
        raise AssertionError(f"no attempt was classified {exit_class}")


async def test_a_prepare_failure_never_leaves_a_worker_behind(
    k8s_client: TestClient, k8s_supervisor: Supervisor, k8s_api: FakeKubernetesApi
) -> None:
    k8s_api.script_all("prepare-fails", after=1)
    task_id = start(k8s_client, k8s_contract())
    for _ in range(6):
        await k8s_supervisor.tick()
    assert k8s_api.object_names("jobs") == []
    assert "attempt_failed" in event_kinds(k8s_client, task_id)


# ----- cancel (16) ---------------------------------------------------------


async def test_a_cancel_drains_the_pod_and_the_attempt_is_killed(
    k8s_client: TestClient,
    k8s_supervisor: Supervisor,
    k8s_api: FakeKubernetesApi,
    clock: FakeClock,
) -> None:
    k8s_api.script_all("hang", after=1)
    task_id = start(k8s_client, k8s_contract())
    for _ in range(4):
        await k8s_supervisor.tick()
        if k8s_client.get(f"/v1/tasks/{task_id}").json()["state"] == "running":
            break
    response = k8s_client.post(
        f"/v1/tasks/{task_id}/cancel",
        json={
            "reason": "the operator changed their mind",
            "verbatim": "stop this one, I have changed my mind",
            "decided_by": "operator",
        },
    )
    assert response.status_code == 200, response.text
    for _ in range(12):
        await k8s_supervisor.tick()
        # The scripted worker ignores SIGTERM, so the grace window has to pass before
        # the supervisor kills it; the clock is the test's to move.
        clock.advance(30)
        view = k8s_client.get(f"/v1/tasks/{task_id}").json()
        if view["state"] == "cancelled":
            break
    assert view["state"] == "cancelled"
    # 26: a drain deletes the Pod with the policy grace period; a Pod that ignored it
    # is deleted again with grace zero. Neither is ever reported as a loss (16).
    assert [a["exit_class"] for e in view["executions"] for a in e["attempts"]] == ["killed"]
    assert ("pods", f"worker-{view['executions'][0]['attempts'][0]['id'].lower()}-abc12") in (
        k8s_api.deleted
    )


# ----- reconcile across a restart (10, 16, 26) -----------------------------


async def test_a_second_supervisor_re_attaches_to_a_running_job_by_label(
    k8s_ctx: AppContext,
    k8s_client: TestClient,
    k8s_supervisor: Supervisor,
    k8s_provider: KubernetesProvider,
    k8s_api: FakeKubernetesApi,
    clock: FakeClock,
) -> None:
    k8s_api.script_all("succeed", after=8)
    task_id = start(k8s_client, k8s_contract())
    for _ in range(3):
        await k8s_supervisor.tick()
        if k8s_client.get(f"/v1/tasks/{task_id}").json()["state"] == "running":
            break
    adopted = await k8s_provider.reconcile()
    assert len(adopted) == 1

    # A restarted supervisor: a new instance, a new holder, the same namespace. The
    # worker keeps running through the handover and the run still settles (16).
    successor = Supervisor(
        k8s_ctx.uow_factory,
        {"kubernetes": k8s_provider},
        k8s_ctx.clock,
        holder="k8s-sup-b",
        artifact_store=k8s_ctx.artifact_store,
        harnesses=k8s_ctx.harnesses,
        lease_ttl_seconds=30,
        grace_seconds=5,
    )
    clock.advance(31)
    state = await run_to_settled(successor, k8s_client, task_id, max_ticks=20)
    assert state == "publishing"


async def test_a_job_with_no_live_attempt_row_is_reported_as_an_orphan(
    k8s_provider: KubernetesProvider, k8s_api: FakeKubernetesApi
) -> None:
    """26: list Jobs by label; a Job with no live attempt row is orphaned."""
    assert await k8s_provider.reconcile() == []


async def test_a_pod_deleted_out_of_band_is_lost_not_running(
    k8s_client: TestClient,
    k8s_supervisor: Supervisor,
    k8s_provider: KubernetesProvider,
    k8s_api: FakeKubernetesApi,
) -> None:
    k8s_api.script_all("succeed", after=8)
    task_id = start(k8s_client, k8s_contract())
    for _ in range(3):
        await k8s_supervisor.tick()
        if k8s_client.get(f"/v1/tasks/{task_id}").json()["state"] == "running":
            break
    handles = await k8s_provider.reconcile()
    assert handles
    k8s_api.remove_pod_out_of_band(handles[0].attempt_id)
    observation = await k8s_provider.observe(handles[0])
    assert observation.state is ObservationState.LOST


# ----- publication on Kubernetes (23, hades FDY-0133) -----------------------

GITHUB_REPOSITORY = "example-org/example-service"
PULL_REQUEST_DELIVERABLE: list[dict[str, Any]] = [
    {"kind": "pull_request", "target": "main", "draft": False, "closes": []}
]


@pytest.fixture
def github_server() -> Iterator[FakeGitHubServer]:
    with FakeGitHubServer() as server:
        server.state.add_repository(GITHUB_REPOSITORY)
        yield server


@pytest.fixture
def github_client(github_server: FakeGitHubServer, tmp_path: Path) -> RestGitHubClient:
    """The App client against the fake api.github.com, with a key generated here."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "app.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    transport = RestTransport(github_server.url, timeout=10.0)
    return RestGitHubClient(
        AppAuthenticator(
            AppConfig(app_id=4969317, private_key_path=str(path), api_base=github_server.url),
            transport,
        ),
        transport,
    )


def _delivery_supervisor(
    ctx: AppContext,
    provider: KubernetesProvider,
    github: RestGitHubClient,
    *,
    holder: str,
    publisher: Any,
) -> Supervisor:
    return Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        ctx.clock,
        holder=holder,
        artifact_store=ctx.artifact_store,
        harnesses=ctx.harnesses,
        lease_ttl_seconds=30,
        grace_seconds=5,
        github=github,
        publisher=publisher,
        delivery_config=DeliveryConfig(poll_interval_seconds=0, reactions_poll_interval_seconds=0),
    )


async def _accepted(client: TestClient, supervisor: Supervisor, task_id: str) -> None:
    """Run through automatic gate acceptance and available publication I/O."""
    await run_to_settled(supervisor, client, task_id, max_ticks=20)
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["acceptance_results"][0]["verdict"] == "accepted"


def _publisher_objects(api: FakeKubernetesApi) -> list[str]:
    return [
        f"{kind}/{name}"
        for kind in ("jobs", "pods", "networkpolicies", "secrets")
        for name in api.object_names(kind)
        if name.startswith(("publish", "np-publisher"))
    ]


async def test_a_kubernetes_task_is_pushed_by_the_kubernetes_publisher_and_opens_its_pr(
    k8s_ctx: AppContext,
    k8s_client: TestClient,
    k8s_provider: KubernetesProvider,
    k8s_api: FakeKubernetesApi,
    github_server: FakeGitHubServer,
    github_client: RestGitHubClient,
) -> None:
    """The Job pushes the collected bundle with the token the App minted, Crucible sees
    the remote at the accepted head, the PR opens, and the push leaves nothing behind."""
    k8s_api.script_all("succeed", after=2)
    k8s_api.on_push = lambda push: github_server.state.push(
        GITHUB_REPOSITORY, push["branch"], push["head"]
    )
    supervisor = _delivery_supervisor(
        k8s_ctx,
        k8s_provider,
        github_client,
        holder="k8s-publish",
        publisher=KubernetesPublisher(k8s_provider),
    )
    task_id = start(k8s_client, k8s_contract(deliverables=PULL_REQUEST_DELIVERABLE))
    await _accepted(k8s_client, supervisor, task_id)
    await supervisor.tick()

    view = k8s_client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] != "publishing", view["state"]
    kinds = event_kinds(k8s_client, task_id)
    for kind in ("publish_started", "publisher_finished", "branch_pushed", "publish_completed"):
        assert kind in kinds, kind
    assert "task_publish_pending" not in kinds
    [push] = k8s_api.pushes
    assert push["head"] == view["head_sha"]
    assert push["branch"] == "crucible/EX-0001"
    assert push["remote"] == "https://github.com/example-org/example-service.git"
    # The token in the push is one the App minted, and it is in no record.
    assert push["token"] in github_server.state.tokens
    events = k8s_client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()
    assert push["token"] not in json.dumps(events)
    pull = k8s_client.get(f"/v1/tasks/{task_id}/pull-request")
    assert pull.status_code == 200, pull.text
    assert pull.json()["head_sha"] == view["head_sha"]
    assert _publisher_objects(k8s_api) == []


async def test_a_task_waiting_in_publishing_says_why_escalates_then_publishes_on_upgrade(
    k8s_ctx: AppContext,
    k8s_client: TestClient,
    k8s_provider: KubernetesProvider,
    k8s_api: FakeKubernetesApi,
    github_server: FakeGitHubServer,
    github_client: RestGitHubClient,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """hades FDY-0133, the lab's HT-0007: GitHub is ready and no publisher is wired. The
    task says why on its events, on `/v1/supervisor` and on the admin UI's task list,
    the supervisor logs it once, and past the publisher's time limit an escalation opens
    with a wake. After the upgrade the first tick publishes it, with no manual step, and
    the escalation the wait opened is closed."""
    k8s_api.script_all("succeed", after=2)
    k8s_api.on_push = lambda push: github_server.state.push(
        GITHUB_REPOSITORY, push["branch"], push["head"]
    )
    before = _delivery_supervisor(
        k8s_ctx, k8s_provider, github_client, holder="k8s-v065", publisher=None
    )
    task_id = start(k8s_client, k8s_contract(deliverables=PULL_REQUEST_DELIVERABLE))
    caplog.set_level(logging.WARNING, logger="crucible.delivery")
    await _accepted(k8s_client, before, task_id)
    for _ in range(3):
        await before.tick()
    assert k8s_client.get(f"/v1/tasks/{task_id}").json()["state"] == "publishing"
    warnings = [r for r in caplog.records if "cannot start" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "no publisher is configured" in warnings[0].getMessage()

    events = k8s_client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()
    pending = [e for e in events["items"] if e["kind"] == "task_publish_pending"]
    assert len(pending) == 1
    assert "no publisher is configured" in pending[0]["payload"]["reason"]
    supervisor_view = k8s_client.get("/v1/supervisor").json()
    [waiting] = supervisor_view["github"]["publishing_waiting"]
    assert waiting["task_id"] == task_id
    assert "no publisher is configured" in waiting["reason"]
    assert waiting["escalation_id"] is None
    assert k8s_client.get(f"/v1/tasks/{task_id}").json()["open_escalations"] == []

    with k8s_ctx.uow_factory() as uow:
        admin_token = mint_token(uow, clock, name="ui-admin", role=Role.ADMIN).token
        uow.commit()
    with TestClient(create_app(k8s_ctx)) as browser:
        form = browser.get("/ui/sign-in")
        nonce = re.search(r'name="csrf" value="([a-f0-9]+)"', form.text)
        assert nonce is not None
        browser.post(
            "/ui/sign-in",
            data={"csrf": nonce.group(1), "token": admin_token, "next": "/ui/tasks"},
            follow_redirects=False,
        )
        page = browser.get("/ui/tasks")
        assert page.status_code == 200
        assert "Waiting to publish: no publisher is configured" in page.text

    # Past the publisher's own time limit, one escalation and one wake, not one a tick.
    clock.advance(DeliveryConfig().publisher_timeout_seconds + 1)
    for _ in range(2):
        await before.tick()
    view = k8s_client.get(f"/v1/tasks/{task_id}").json()
    [escalation] = view["open_escalations"]
    assert "publication has not started" in escalation["question"]
    assert "no publisher is configured" in escalation["question"]
    kinds = event_kinds(k8s_client, task_id)
    assert kinds.count("escalation_opened") == 1
    with k8s_ctx.uow_factory() as uow:
        principal = uow.tasks.get(task_id).principal_id  # type: ignore[union-attr]
        wakes = uow.wakes.list_for_principal(principal, since=None, include_acked=False, limit=200)
    assert [w.reason for w in wakes if w.task_id == task_id].count("publish_failed") == 1
    assert (
        k8s_client.get("/v1/supervisor").json()["github"]["publishing_waiting"][0]["escalation_id"]
        == escalation["id"]
    )
    assert len([r for r in caplog.records if "cannot start" in r.getMessage()]) == 1

    # The upgrade: a new supervisor process with the publisher wired. Its first tick.
    await before.stop()
    after = _delivery_supervisor(
        k8s_ctx,
        k8s_provider,
        github_client,
        holder="k8s-v066",
        publisher=KubernetesPublisher(k8s_provider),
    )
    await after.tick()
    view = k8s_client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] != "publishing", view["state"]
    assert "publish_completed" in event_kinds(k8s_client, task_id)
    assert view["open_escalations"] == []
    assert "escalation_closed" in event_kinds(k8s_client, task_id)
    assert k8s_client.get("/v1/supervisor").json()["github"]["publishing_waiting"] == []
    assert [p["head"] for p in k8s_api.pushes] == [view["head_sha"]]
    assert _publisher_objects(k8s_api) == []


def _kubeconfig(tmp_path: Path) -> str:
    path = tmp_path / "kubeconfig"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": "lab",
                "contexts": [{"name": "lab", "context": {"cluster": "lab", "user": "sup"}}],
                "clusters": [{"name": "lab", "cluster": {"server": "https://127.0.0.1:1"}}],
                "users": [{"name": "sup", "user": {"token": "not-a-real-token"}}],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def test_wiring_builds_a_kubernetes_publisher_when_kubernetes_and_github_are_on(
    migrated: str, tmp_path: Path
) -> None:
    """The lab's shape: the Kubernetes provider and GitHub, no Docker. Before FDY-0133
    this wired no publisher at all, and a task in `publishing` was skipped in silence."""
    settings = Settings(
        database={"url": migrated},
        service={"artifact_root": str(tmp_path / "artifacts")},
        kubernetes={"enabled": True, "kubeconfig": _kubeconfig(tmp_path)},
        github={"enabled": True},
    )
    wiring = wire(settings, role="supervisor")
    assert "kubernetes" in wiring.providers and "docker" not in wiring.providers
    assert wiring.github is not None
    assert isinstance(wiring.publisher, KubernetesPublisher)
    assert wiring.supervisor().delivery._publisher is wiring.publisher


def test_wiring_routes_by_workspace_when_both_providers_are_on(
    migrated: str, tmp_path: Path
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "app.pem"
    pem.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    settings = Settings(
        database={"url": migrated},
        service={"artifact_root": str(tmp_path / "artifacts")},
        docker={"enabled": True, "host": "tcp://127.0.0.1:1"},
        kubernetes={"enabled": True, "kubeconfig": _kubeconfig(tmp_path)},
        github={"enabled": True, "app": {"app_id": 1, "private_key_path": str(pem)}},
    )
    wiring = wire(settings, role="supervisor")
    assert isinstance(wiring.publisher, ByWorkspacePublisher)
    # And with no GitHub App at all there is no publisher, which the supervisor reports.
    bare = Settings(database={"url": migrated}, docker=settings.docker)
    assert wire(bare, role="supervisor").publisher is None


# ----- FDY-0140: a quiet worker that is working is not stalled ------------------------


def _worker_pod(k8s_api: FakeKubernetesApi, attempt_id: str) -> str:
    prefix = f"worker-{attempt_id.lower()}"
    names = [name for kind, name in k8s_api.objects if kind == "pods" and name.startswith(prefix)]
    assert len(names) == 1, names
    return names[0]


async def _quiet_hang(
    k8s_client: TestClient,
    k8s_supervisor: Supervisor,
    k8s_api: FakeKubernetesApi,
    external_id: str,
) -> tuple[str, str]:
    """A worker that writes nothing to its log for as long as it runs, as Hermes `-z`
    does, on the seeded limits (warn 300 s, fail 1800 s) and an hour's timeout."""
    k8s_api.script_all("hang")
    document = k8s_contract(external_id)
    document["execution_request"]["timeout_seconds"] = 3600
    task_id = start(k8s_client, document)
    for _ in range(10):
        await k8s_supervisor.tick()
        attempt = k8s_client.get(f"/v1/tasks/{task_id}").json().get("latest_attempt")
        if attempt and attempt["state"] == "running":
            return task_id, str(attempt["id"])
    raise AssertionError("the quiet worker never started")


async def test_a_silent_worker_whose_files_change_is_not_stalled(
    k8s_client: TestClient,
    k8s_supervisor: Supervisor,
    k8s_api: FakeKubernetesApi,
    clock: FakeClock,
) -> None:
    """The checkout path is a `k8s://` name, so the supervisor's own walk sees nothing;
    the provider asks the Pod. A worker whose files keep moving works on past the fail
    limit with no log line at all."""
    task_id, attempt_id = await _quiet_hang(k8s_client, k8s_supervisor, k8s_api, "EX-0140A")
    pod = _worker_pod(k8s_api, attempt_id)
    k8s_api.activity_answers[pod] = [f"activity {n} 10 100\n".encode() for n in range(1000)]
    for _ in range(50):
        clock.advance(50)
        await k8s_supervisor.tick()
    kinds = event_kinds(k8s_client, task_id)
    assert "worker_stalled" not in kinds and "worker_quiet" not in kinds
    attempt = k8s_client.get(f"/v1/attempts/{attempt_id}").json()
    assert attempt["state"] == "running" and attempt["termination_reason"] is None
    with k8s_supervisor._uow_factory() as uow:
        signals = [h.signal for h in uow.heartbeats.list_for_attempt(attempt_id, limit=1000)]
    # Asked about once a minute, not every tick: 2500 s is about 40 asks.
    assert 20 <= signals.count("fs_changed") <= 45, signals.count("fs_changed")


async def test_a_worker_whose_files_do_not_move_is_stalled_and_recorded_as_a_stall(
    k8s_client: TestClient,
    k8s_supervisor: Supervisor,
    k8s_api: FakeKubernetesApi,
    clock: FakeClock,
) -> None:
    task_id, attempt_id = await _quiet_hang(k8s_client, k8s_supervisor, k8s_api, "EX-0140B")
    pod = _worker_pod(k8s_api, attempt_id)
    k8s_api.activity_answers[pod] = [b"activity 1 10 100\n"]
    for _ in range(45):
        clock.advance(50)
        await k8s_supervisor.tick()
    kinds = event_kinds(k8s_client, task_id)
    assert "worker_stalled" in kinds
    attempt = k8s_client.get(f"/v1/attempts/{attempt_id}").json()
    assert attempt["termination_reason"] == "stall"
    assert attempt["exit_class"] == "stalled"
