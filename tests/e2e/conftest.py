"""The end-to-end tier (18): the real Docker provider, real containers, no model.

The stack the tests stand up is the one spec 13 describes: PostgreSQL, the socket
proxy against the target daemon, and the egress proxy on an `internal: true` workers
network. Crucible itself runs in the test process (developer mode, 13) with the Docker
provider pointed at the socket proxy, never at the socket.

The worker is the script-harness image (18): it reads the identity bundle, does what
the repository's `e2e-behavior` file asks, writes a CompletionClaimV1, and exits. No
subscription, no credential, no model.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.harness.registry import default_registry
from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.auth import mint_token
from crucible.application.proxy_config import worker_proxy_config
from crucible.application.repositories import register_repository
from crucible.application.supervisor import Supervisor
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.entities import Role
from tests.e2e import daemon
from tests.e2e.daemon import derive_subnet  # Issue 135: subnet retry
from tests.e2e.policy import e2e_policy_document, e2e_routing_document
from tests.e2e.repo import make_origin
from tests.fixtures import contract_document, promote_for_test

pytestmark = pytest.mark.e2e

POSTGRES_IMAGE = (
    "postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
)
SOCKET_PROXY_IMAGE = (
    "tecnativa/docker-socket-proxy@sha256:"
    "1f5038b54f06c3e18422902cf00ba21803d1c97805aae032e5e6673d532d3459"
)
EGRESS_PROXY_IMAGE = (
    "ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029"
)

RUN_ID = uuid.uuid4().hex[:8]
NET_WORKERS = f"crucible-e2e-workers-{RUN_ID}"
NET_CONTROL = f"crucible-e2e-control-{RUN_ID}"
# Issue 135: derive the subnet from RUN_ID so that `ensure_network` retries on
# overlap by cycling through 10.100..10.199 /24s.  The seed is also the same
# value used by `derive_subnet` in the stack fixture and by the daemon itself.
SEED = RUN_ID
WORKERS_SUBNET = derive_subnet(SEED, 0)
# github.com for the script tier, plus every endpoint the real adapters declare (S6),
# so the live tier's workers reach their model API through the same filtering proxy.
EGRESS_ALLOWLIST = (
    "github.com",
    "api.anthropic.com",
    "api.openai.com",
    "auth.openai.com",
    "chatgpt.com",
    "daily-cloudcode-pa.googleapis.com",
    "oauth2.googleapis.com",
    "www.googleapis.com",
    "lh3.googleusercontent.com",
)

TRUNCATE = (
    "TRUNCATE github_deliveries, ci_decisions, ci_certifications, reactions, "
    "review_comments, external_reviews, external_review_cycles, pull_request_heads, "
    "pull_requests, retention_actions, log_chunks, attempt_metrics, pool_exhaustions, wakes, "
    "review_dispositions, "
    "decisions, escalations, acceptance_results, gate_results, review_reports, evidence, "
    "artifacts, bootstrap_imports, idempotency_keys, supervisor_status, completion_claims, "
    "leases, events, "
    "attempts, executions, task_contracts, tasks, repositories, principals, policies, "
    "routing_policies "
    "RESTART IDENTITY CASCADE"
)


def _squid_conf(directory: Path, subnet: str) -> Path:
    """The same configuration `make proxy-config` writes, for the test's allowlist."""
    endpoint = (
        os.environ.get("CRUCIBLE_LOCAL_ENDPOINT_URL")
        or os.environ.get("CRUCIBLE_SPARK_ENDPOINT_URL", "")
    ).strip()
    routing = {
        "models": [
            {
                "endpoint": "local",
                "endpoint_url": endpoint,
                "enabled": True,
            }
        ]
        if endpoint
        else []
    }
    path = directory / "squid.conf"
    path.write_text(
        worker_proxy_config(subnet, list(EGRESS_ALLOWLIST), [routing]),
        encoding="utf-8",
    )
    path.chmod(0o644)
    return path


class NetworkDaemon(Protocol):
    """The network operations the stack needs before it starts containers."""

    def ensure_network(
        self,
        name: str,
        *,
        internal: bool,
        subnet: str | None = None,
        seed: str | None = None,
        max_attempts: int = 5,
    ) -> str: ...

    def remove_network(self, name: str) -> None: ...


def _create_stack_networks(network_daemon: NetworkDaemon) -> str:
    """Create the stack networks, removing successful creates if a later one fails."""
    created: list[str] = []
    try:
        network_daemon.ensure_network(NET_CONTROL, internal=False)
        created.append(NET_CONTROL)
        return network_daemon.ensure_network(NET_WORKERS, internal=True, seed=RUN_ID)
    except Exception:
        for name in reversed(created):
            network_daemon.remove_network(name)
        raise


@pytest.fixture(scope="session")
def artifact_root() -> Iterator[Path]:
    """The artifact root, shared between this process and the containers it creates.

    Developer mode means two different uids touch it: the test process, and container
    uid 1000, which under the rootless daemon is a subordinate host uid (S9 Test E).
    The directories are group and other writable for exactly that reason; in normal
    mode the service runs as uid 1000 itself and none of this is needed.
    """
    # The daemon resolves bind sources in its own mount namespace, which is the host
    # filesystem, and under the rootless arrangement it runs as a service user that
    # cannot traverse a private home. The root therefore lives somewhere world
    # traversable, which /var/tmp is.
    base = Path(os.environ.get("CRUCIBLE_E2E_ROOT", "/var/tmp"))
    root = base / f"crucible-e2e-{RUN_ID}"
    root.mkdir(parents=True)
    root.chmod(0o777)
    (root / "e2e-repos").mkdir()
    (root / "e2e-repos").chmod(0o777)
    yield root
    # Container-written files belong to a uid this process is not (S9 Test E), so the
    # daemon removes what the daemon made, then the empty shell goes locally.
    if os.environ.get("CRUCIBLE_E2E_KEEP"):
        return
    daemon.run(
        "run",
        "--rm",
        "--user",
        "0:0",
        "-v",
        f"{root}:/x",
        POSTGRES_IMAGE,
        "sh",
        "-c",
        "rm -rf /x/* /x/.[!.]*",
        check=False,
    )
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def stack(artifact_root: Path) -> Iterator[dict[str, Any]]:
    names = {
        "postgres": f"crucible-e2e-postgres-{RUN_ID}",
        "sockproxy": f"crucible-e2e-sockproxy-{RUN_ID}",
        "egress": f"crucible-e2e-egress-{RUN_ID}",
    }
    # Issue 135: retry on subnet overlap; seed=RUN_ID, max 5 attempts.
    # Returns the actual subnet used (may differ from WORKERS_SUBNET if retry).
    actual_workers_subnet = _create_stack_networks(daemon)
    pg_port, proxy_port = daemon.free_port(), daemon.free_port()
    conf = _squid_conf(artifact_root, actual_workers_subnet)
    socket_path = os.environ.get("CRUCIBLE_E2E_DOCKER_SOCKET", "/var/run/docker.sock")
    try:
        daemon.run_detached(
            names["postgres"],
            [
                "--network",
                NET_CONTROL,
                "-e",
                "POSTGRES_USER=crucible",
                "-e",
                "POSTGRES_PASSWORD=e2e-not-a-secret",
                "-e",
                "POSTGRES_DB=crucible",
                "-p",
                f"127.0.0.1:{pg_port}:5432",
                POSTGRES_IMAGE,
            ],
        )
        # 13: only this container ever mounts the socket, and it publishes on loopback
        # only because developer mode needs it (S9 records the exposure).
        daemon.run_detached(
            names["sockproxy"],
            [
                "--network",
                NET_CONTROL,
                "-e",
                "CONTAINERS=1",
                "-e",
                "IMAGES=1",
                "-e",
                "NETWORKS=1",
                "-e",
                "VOLUMES=1",
                "-e",
                "POST=1",
                "-e",
                "EXEC=0",
                "-e",
                "BUILD=0",
                "-e",
                "SWARM=0",
                "-e",
                "SYSTEM=0",
                "-e",
                "PLUGINS=0",
                "-e",
                "SECRETS=0",
                "-e",
                "CONFIGS=0",
                "-e",
                "INFO=0",
                "-v",
                f"{socket_path}:/var/run/docker.sock:ro",
                "-p",
                f"127.0.0.1:{proxy_port}:2375",
                SOCKET_PROXY_IMAGE,
            ],
        )
        daemon.run_detached(
            names["egress"],
            [
                "--network",
                NET_CONTROL,
                "--network-alias",
                "egress-proxy",
                "-v",
                f"{conf}:/etc/squid/squid.conf:ro",
                EGRESS_PROXY_IMAGE,
            ],
        )
        daemon.run("network", "connect", "--alias", "egress-proxy", NET_WORKERS, names["egress"])
        for name in names.values():
            daemon.wait_for_port(name, 0)
        url = f"postgresql+psycopg://crucible:e2e-not-a-secret@127.0.0.1:{pg_port}/crucible"
        _wait_for_postgres(url)
        yield {
            "names": names,
            "database_url": url,
            "docker_host": f"tcp://127.0.0.1:{proxy_port}",
            "egress_proxy": "http://egress-proxy:3128",
        }
    finally:
        for container in daemon.container_ids("crucible.owner"):
            daemon.rm(container)
        daemon.rm(*names.values())
        daemon.remove_network(NET_WORKERS)
        daemon.remove_network(NET_CONTROL)


def _wait_for_postgres(url: str, *, attempts: int = 120) -> None:
    last = ""
    for _ in range(attempts):
        try:
            engine = make_engine(url)
            with engine.begin() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return
        except Exception as exc:  # the container is still starting
            last = str(exc)
            time.sleep(0.5)
    raise RuntimeError(f"postgres never became ready: {last}")


@pytest.fixture(scope="session")
def worker_image() -> str:
    return os.environ.get("CRUCIBLE_E2E_IMAGE") or daemon.image_tag(
        "crucible-worker:script-harness-", harness="script-harness"
    )


@pytest.fixture(scope="session")
def migrated(stack: dict[str, Any]) -> str:
    url = str(stack["database_url"])
    migrate.upgrade(url)
    return url


@pytest.fixture
def engine(migrated: str) -> Iterator[Engine]:
    eng = make_engine(migrated)
    with eng.begin() as conn:
        conn.execute(text(TRUNCATE))
    yield eng
    eng.dispose()


@pytest.fixture
def docker_config(stack: dict[str, Any], artifact_root: Path) -> DockerConfig:
    allowlist = list(EGRESS_ALLOWLIST)
    endpoint = (
        os.environ.get("CRUCIBLE_LOCAL_ENDPOINT_URL")
        or os.environ.get("CRUCIBLE_SPARK_ENDPOINT_URL", "")
    ).strip()
    if endpoint:
        parsed = urlsplit(endpoint)
        allowlist.append(
            f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
        )
    return DockerConfig(
        endpoint=str(stack["docker_host"]),
        artifact_root=str(artifact_root),
        # Developer mode: the artifact root is a host directory the daemon can see.
        mount_kind="bind",
        artifact_host_root=str(artifact_root),
        artifact_volume="",
        workers_network=NET_WORKERS,
        egress_proxy=str(stack["egress_proxy"]),
        proxy_allowlist=tuple(allowlist),
        collector_timeout_seconds=300,
        verifier_timeout_seconds=300,
        workspace_dir_mode=0o777,
        use_reference_cache=False,
        credential_root=str(artifact_root / "credentials"),
        credential_host_root=str(artifact_root / "credentials"),
    )


@pytest.fixture
def provider(docker_config: DockerConfig) -> DockerProvider:
    return DockerProvider(docker_config)


@pytest.fixture
def ctx(engine: Engine, migrated: str, artifact_root: Path, provider: DockerProvider) -> AppContext:
    return AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=SystemClock(),
        providers=[provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(artifact_root / "store"),
        harnesses=default_registry(test_fixtures=True),
    )


@pytest.fixture
def tokens(ctx: AppContext, artifact_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with ctx.uow_factory() as uow:
        for role in Role:
            out[role.value] = mint_token(
                uow, ctx.clock, name=f"{role.value}-principal", role=role
            ).token
        uow.commit()
    return out


@pytest.fixture
def client(
    ctx: AppContext,
    tokens: dict[str, str],
    worker_image: str,
    provider: DockerProvider,
) -> Iterator[TestClient]:
    app = create_app(ctx)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
        routing = e2e_routing_document()
        response = admin.put(f"/v1/routing/{routing['name']}/{routing['version']}", json=routing)
        assert response.status_code in (200, 201), response.text
        document = e2e_policy_document()
        response = admin.put(
            f"/v1/policies/{document['name']}/{document['version']}", json=document
        )
        assert response.status_code in (200, 201), response.text
        worker = next(
            item for item in asyncio.run(provider.list_images()) if item.reference == worker_image
        )
        with ctx.uow_factory() as uow:
            promote_for_test(
                uow,
                digest=worker.digest,
                reference=worker.reference,
                harnesses=dict(worker.harnesses) or {"script-harness": "1.0.0"},
                at=ctx.clock.now(),
                by="e2e",
                reason="e2e script harness image",
            )
            uow.commit()
    # The live variants add an explicit model pin, which is an operator-only action.
    # The ordinary tier remains class-routed even though it shares this client.
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as c:
        yield c


@pytest.fixture
def supervisor(ctx: AppContext, provider: DockerProvider) -> Supervisor:
    return Supervisor(
        ctx.uow_factory,
        {"docker": provider},
        ctx.clock,
        holder=f"e2e-{RUN_ID}",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=120,
        grace_seconds=5,
        harnesses=ctx.harnesses,
    )


class OriginFactory(Protocol):
    """Builds a throwaway origin repository inside the artifact root."""

    def __call__(self, name: str, behavior: str = ..., **kw: Any) -> str: ...


@pytest.fixture
def origin(artifact_root: Path) -> OriginFactory:
    def build(name: str, behavior: str = "succeed", **kw: Any) -> str:
        return make_origin(artifact_root / "e2e-repos", name, behavior, **kw)

    return build


def register(ctx: AppContext, name: str, url: str) -> None:
    with ctx.uow_factory() as uow:
        register_repository(
            uow,
            ctx.clock,
            principal_name="tests",
            name=name,
            registration=RepositoryRegistration(
                url=url,
                default_branch="main",
                policy_name="e2e-script",
                external_review=ExternalReviewAttestation(
                    attested_all_prs=True, attested_by="tests"
                ),
            ),
        )
        uow.commit()


def e2e_contract(external_id: str, repository: str, image: str, **overrides: Any) -> dict[str, Any]:
    # The ordinary e2e tier exercises class routing.  The image argument remains in
    # this shared helper's interface for the live tiers, which add an operator pin.
    del image
    doc = contract_document(external_id=external_id)
    doc["repository"] = {
        "name": repository,
        "base_ref": "main",
        "work_branch": f"crucible/{external_id}",
    }
    doc["scope"] = {
        "allowed_paths": ["src/**", "checks/**"],
        "prohibited_paths": [".github/**"],
        "may_add_dependencies": False,
        "may_modify_ci": False,
    }
    doc["required_verification"] = [
        {"id": "V1", "command": "sh checks/lint.sh", "expect_exit": 0},
        {"id": "V2", "command": "sh checks/test.sh", "expect_exit": 0},
        {"id": "V3", "command": "sh checks/scan.sh", "expect_exit": 0},
        {"id": "V4", "kind": "artifact", "path": "report/run-evidence.md"},
    ]
    doc["deliverables"] = [{"kind": "artifacts", "target": None, "draft": False, "closes": []}]
    doc["policy"] = {"name": "e2e-script", "version": 1}
    doc["execution_request"] = {
        **doc["execution_request"],
        "provider": "docker",
        "timeout_seconds": 600,
    }
    doc["execution_request"].pop("image", None)
    doc["project_instructions"] = []
    doc.update(overrides)
    return doc


def submit_and_start(client: TestClient, document: dict[str, Any]) -> str:
    response = client.post("/v1/tasks", json=document)
    assert response.status_code == 201, response.text
    task_id: str = response.json()["id"]
    request = document["execution_request"]
    start = {
        "provider": request["provider"],
        "policy_version": document["policy"]["version"],
    }
    for field in ("harness", "model", "image"):
        if request.get(field) is not None:
            start[field] = request[field]
    response = client.post(
        f"/v1/tasks/{task_id}/start",
        json=start,
    )
    assert response.status_code == 200, response.text
    return task_id


async def run_until(
    supervisor: Supervisor,
    client: TestClient,
    task_id: str,
    states: set[str],
    *,
    max_ticks: int = 40,
    pause: float = 0.5,
    min_seconds: float = 240.0,
) -> str:
    """Tick until the task reaches one of `states`: at least `max_ticks` ticks and at
    least `min_seconds`. A launch runs beside the tick (hades #190), so a tick no longer
    lasts as long as the launch it starts and a count of ticks alone is no budget."""
    state = ""
    ticks = 0
    started = time.monotonic()
    while ticks < max_ticks or time.monotonic() - started < min_seconds:
        await supervisor.tick()
        ticks += 1
        state = str(client.get(f"/v1/tasks/{task_id}").json()["state"])
        if state in states:
            return state
        time.sleep(pause)
    raise AssertionError(f"task never reached {states}; last state {state}")


def upload_review(client: TestClient, task_id: str) -> None:
    view = client.get(f"/v1/tasks/{task_id}").json()
    report = {
        "schema_version": "1.0",
        "task_external_id": view["external_id"],
        "reviewed_head_sha": view["head_sha"],
        "reviewer": {"kind": "orchestrator", "principal": "orchestrator-principal"},
        "verdict": "approve",
        "findings": [],
        "summary": f"Reviewed {view['head_sha']}.",
    }
    response = client.post(f"/v1/tasks/{task_id}/review", json={"report": report})
    assert response.status_code == 200, response.text


def gate_results(client: TestClient, task_id: str) -> dict[str, str]:
    summary = client.get(f"/v1/tasks/{task_id}").json()["gate_summary"]
    return {str(k): str(v) for k, v in summary["results"].items()}


def event_kinds(client: TestClient, task_id: str) -> list[str]:
    response = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200})
    return [event["kind"] for event in response.json()["items"]]


def git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/nonexistent",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "e2e",
            "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
            "GIT_COMMITTER_NAME": "e2e",
            "GIT_COMMITTER_EMAIL": "e2e@example.invalid",
        },
    )
    return completed.stdout
