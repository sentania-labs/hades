"""PostgreSQL in a container (testcontainers) or CRUCIBLE_TEST_DATABASE_URL, one database per
test process (issue 195); migrated once per process, truncated between tests. The supervisor,
API client, and fake provider of one test share that database."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, make_url, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.harness.registry import default_registry
from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.auth import mint_token
from crucible.application.harnesses import set_harness_enabled
from crucible.application.repositories import register_repository
from crucible.application.supervisor import Supervisor
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.entities import Role
from crucible.domain.lifecycle import TaskState
from crucible.ports.notification import DeliveryResult
from tests.fixtures import REPOSITORY_URL, FakeClock, contract_document
from tests.integration.postgres import SERVER_URL

pytestmark = pytest.mark.integration

TRUNCATE = (
    "TRUNCATE github_deliveries, ci_decisions, ci_certifications, reactions, "
    "review_comments, external_reviews, external_review_cycles, pull_request_heads, "
    "pull_requests, attempt_metrics, pool_exhaustions, wakes, review_dispositions, "
    "decisions, escalations, memory_items, decision_ledger, task_notes, "
    "acceptance_results, gate_results, review_reports, evidence, artifacts, "
    "provider_settings, harness_images, github_manifest_states, "
    "bootstrap_imports, idempotency_keys, supervisor_status, completion_claims, leases, events, "
    "heartbeats, log_chunks, retention_actions, "
    "attempts, executions, task_contracts, tasks, repositories, principals, "
    "policies, routing_policies, harnesses RESTART IDENTITY CASCADE"
)

# The rows the migrations seed. Tests add versions and switch harnesses, so each test
# starts from a copy taken once per process right after migrating, not from whatever
# the test before it left (issue 195).
SEEDED_TABLES = ("policies", "routing_policies", "harnesses")
SEED_SCHEMA = "crucible_test_seed"


def snapshot_seeds(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SEED_SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {SEED_SCHEMA}"))
        for table in SEEDED_TABLES:
            conn.execute(text(f"CREATE TABLE {SEED_SCHEMA}.{table} AS TABLE public.{table}"))


def rebuild(url: str) -> None:
    """The schema from nothing, for a database a failed migration test left somewhere
    `upgrade` cannot come back from. The seed copy lives in its own schema and stays."""
    engine = make_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
    migrate.upgrade(url)


def reset(engine: Engine) -> None:
    """Every table empty, then the migrations' seeded rows back as they were."""
    with engine.begin() as conn:
        conn.execute(text(TRUNCATE))
        for table in SEEDED_TABLES:
            columns = ", ".join(
                f'"{c}"'
                for c in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema AND table_name = :table "
                        "ORDER BY ordinal_position"
                    ),
                    {"schema": SEED_SCHEMA, "table": table},
                ).scalars()
            )
            conn.execute(
                text(
                    f"INSERT INTO public.{table} ({columns}) "
                    f"SELECT {columns} FROM {SEED_SCHEMA}.{table}"
                )
            )


def worker_database_name(worker_id: str, run_id: str) -> str:
    """The database one test process owns (issue 195): `crucible_test_gw0_<run>` for an
    xdist worker, `crucible_test_master_<run>` for a serial run. The run's own id keeps
    two runs against one CRUCIBLE_TEST_DATABASE_URL server off each other's databases."""
    return f"crucible_test_{worker_id}_{run_id[:12]}"


@contextmanager
def own_database(server_url: str, name: str) -> Iterator[str]:
    """A fresh database on the server, for this process alone, dropped afterwards."""
    admin = make_engine(server_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        try:
            yield make_url(server_url).set(database=name).render_as_string(hide_password=False)
        finally:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def database_url(worker_id: str, testrun_uid: str, pytestconfig: pytest.Config) -> Iterator[str]:
    # The URL's role needs CREATEDB. Only the server is shared: each worker still
    # creates, migrates and drops its own database (issue 195).
    url = pytestconfig.stash.get(SERVER_URL, None)
    if not url:
        if os.environ.get("CRUCIBLE_ALLOW_NO_DATABASE"):
            pytest.skip(
                "Docker daemon not available and no CRUCIBLE_TEST_DATABASE_URL; "
                "opted out via CRUCIBLE_ALLOW_NO_DATABASE"
            )
        pytest.fail(
            "Integration tier requires a database: Docker for postgres:16 (testcontainers) "
            "or CRUCIBLE_TEST_DATABASE_URL. Set CRUCIBLE_ALLOW_NO_DATABASE=1 to opt out."
        )
    with own_database(url, worker_database_name(worker_id, testrun_uid)) as own:
        yield own


@pytest.fixture(scope="session")
def migrated(database_url: str) -> str:
    migrate.downgrade(database_url, "base")
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    snapshot_seeds(engine)
    engine.dispose()
    return database_url


@pytest.fixture
def engine(migrated: str) -> Iterator[Engine]:
    eng = make_engine(migrated)
    reset(eng)
    yield eng
    # Reset on the way out as well: a downgrade archives task-bound events and the
    # upgrade puts them back by foreign key, so rows the last test left would break the
    # migration tests that follow it in the run (C6).
    reset(eng)
    eng.dispose()


@pytest.fixture
def uow_factory(engine: Engine) -> SqlUnitOfWorkFactory:
    return SqlUnitOfWorkFactory(engine)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def artifact_store(tmp_path: Path) -> DiskArtifactStore:
    return DiskArtifactStore(tmp_path / "artifacts")


class RecordingDeliverer:
    """A wake receiver a test can make fail. Delivery is best effort; poll is durable."""

    def __init__(
        self, *, ok: bool = True, url: str | None = "https://foundry.invalid/wake"
    ) -> None:
        self.ok = ok
        self.url = url
        self.bodies: list[bytes] = []

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def deliver(self, body: bytes) -> DeliveryResult:
        self.bodies.append(body)
        if self.ok:
            return DeliveryResult(True, "HTTP 200")
        return DeliveryResult(False, "HTTP 503")


@pytest.fixture
def ctx(
    engine: Engine,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FakeClock,
    provider: FakeProvider,
    artifact_store: DiskArtifactStore,
    migrated: str,
) -> AppContext:
    # The seeded defaults ship codex and agy disabled (S1b). The fake provider runs no
    # credential, so the tier enables them through the same service the admin surface
    # calls, with the reason recorded as an event.
    with uow_factory() as uow:
        for name in ("codex", "agy"):
            set_harness_enabled(
                uow,
                clock,
                principal_name="tests",
                name=name,
                enabled=True,
                reason="integration tier: the fake provider runs no credential",
            )
        uow.commit()
    return AppContext(
        uow_factory=uow_factory,
        clock=clock,
        providers=[provider],
        database_url=migrated,
        engine=engine,
        artifact_store=artifact_store,
        harnesses=default_registry(test_fixtures=True),
    )


@pytest.fixture
def tokens(ctx: AppContext) -> dict[str, str]:
    out: dict[str, str] = {}
    with ctx.uow_factory() as uow:
        for role in Role:
            out[role.value] = mint_token(
                uow, ctx.clock, name=f"{role.value}-principal", role=role
            ).token
        register_repository(
            uow,
            ctx.clock,
            principal_name="tests",
            name="example-service",
            registration=RepositoryRegistration(
                url=REPOSITORY_URL,
                default_branch="main",
                policy_name="default-software",
                installation_id=1,
                external_review=ExternalReviewAttestation(
                    attested_all_prs=True, attested_by="tests"
                ),
            ),
        )
        uow.commit()
    return out


@pytest.fixture
def client(ctx: AppContext, tokens: dict[str, str]) -> Iterator[TestClient]:
    app = create_app(ctx)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['orchestrator']}"}) as c:
        yield c


def make_supervisor(
    ctx: AppContext, provider: FakeProvider, holder: str = "sup-a", **kw: Any
) -> Supervisor:
    return Supervisor(
        ctx.uow_factory,
        {"fake": provider},
        ctx.clock,
        holder=holder,
        artifact_store=kw.pop("artifact_store", ctx.artifact_store),
        wake_deliverer=kw.pop("wake_deliverer", None),
        lease_ttl_seconds=kw.pop("lease_ttl_seconds", 30),
        grace_seconds=kw.pop("grace_seconds", 60),
        harnesses=kw.pop("harnesses", ctx.harnesses),
        **kw,
    )


@pytest.fixture
def supervisor(ctx: AppContext, provider: FakeProvider) -> Supervisor:
    return make_supervisor(ctx, provider)


def submit_and_start(
    client: TestClient,
    image: str,
    external_id: str = "EX-0001",
    *,
    start: bool = True,
    **overrides: Any,
) -> str:
    doc = contract_document(external_id=external_id, **overrides)
    doc["repository"]["work_branch"] = f"crucible/{external_id}"
    doc["execution_request"]["image"] = image
    r = client.post("/v1/tasks", json=doc)
    assert r.status_code == 201, r.text
    task_id: str = r.json()["id"]
    if not start:
        return task_id
    req = doc["execution_request"]
    r = client.post(
        f"/v1/tasks/{task_id}/start",
        json={
            "provider": req["provider"],
            "image": req["image"],
            "policy_version": 2,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "scheduled"
    return task_id


async def run_until(
    supervisor: Supervisor, client: TestClient, task_id: str, states: set[str], max_ticks: int = 12
) -> str:
    state = ""
    for _ in range(max_ticks):
        await supervisor.tick()
        state = str(client.get(f"/v1/tasks/{task_id}").json()["state"])
        if state in states:
            return state
    raise AssertionError(f"task never reached {states}; last state {state}")


# In C2 the same tick that reports a task also evaluates its pre-PR gates (09, 11), so a
# run settles past `reported` rather than in it.
POST_REPORT_STATES = {
    "reported",
    "publishing",
    "accepted",
    "publish_failed",
    "awaiting_external_review",
    "awaiting_ci_certification",
    "external_feedback_received",
    "ci_certification_failed",
    "ready_for_merge",
    "pre_pr_gates_failed",
    "awaiting_internal_review",
    "gates_passed",
    "awaiting_acceptance",
}


async def run_to_settled(
    supervisor: Supervisor, client: TestClient, task_id: str, max_ticks: int = 12
) -> str:
    return await run_until(supervisor, client, task_id, POST_REPORT_STATES, max_ticks)


def event_kinds(client: TestClient, task_id: str) -> list[str]:
    r = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200})
    assert r.status_code == 200
    return [e["kind"] for e in r.json()["items"]]


ARTIFACTS_DELIVERABLE: list[dict[str, Any]] = [
    {"kind": "artifacts", "target": None, "draft": False, "closes": []}
]


def upload_review(
    client: TestClient, task_id: str, *, verdict: str = "approve", head_sha: str | None = None
) -> Any:
    """Upload a ReviewReportV1 as the orchestrator's own non-author review (04, 11)."""
    view = client.get(f"/v1/tasks/{task_id}").json()
    head = head_sha or view["head_sha"]
    report = {
        "schema_version": "1.0",
        "task_external_id": view["external_id"],
        "reviewed_head_sha": head,
        "reviewer": {"kind": "orchestrator", "principal": "orchestrator-principal"},
        "verdict": verdict,
        "findings": (
            []
            if verdict == "approve"
            else [{"severity": "major", "path": "src/a.py", "line": 1, "text": "narrow it"}]
        ),
        "summary": f"Reviewed {head}.",
    }
    return client.post(f"/v1/tasks/{task_id}/review", json={"report": report})


def legacy_review_state(supervisor: Supervisor, task_id: str) -> None:
    """Seed an old deployment's waiting task for compatibility tests of the review API.

    Fresh runs never enter this state. These fixtures exercise persisted legacy tasks.
    """
    with supervisor._fenced() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        assert task is not None
        task.state = TaskState.AWAITING_INTERNAL_REVIEW
        uow.tasks.save(task)
        uow.commit()


async def legacy_acceptance_state(
    supervisor: Supervisor, client: TestClient, task_id: str, *, verdict: str = "approve"
) -> str:
    """Seed the pre-upgrade review and acceptance state for legacy API tests only."""
    legacy_review_state(supervisor, task_id)
    response = upload_review(client, task_id, verdict=verdict)
    assert response.status_code == 200, response.text
    with supervisor._fenced() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        assert task is not None
        task.state = TaskState.AWAITING_ACCEPTANCE
        uow.tasks.save(task)
        uow.commit()
    return "awaiting_acceptance"


def correction_document(
    client: TestClient, task_id: str, *, image: str, reason: str = "pre_pr_gates", **overrides: Any
) -> dict[str, Any]:
    """A correction version of the task's current contract (05)."""
    view = client.get(f"/v1/tasks/{task_id}").json()
    document = dict(view["contract"])
    document["execution_request"] = {**document["execution_request"], "image": image}
    document["correction"] = {
        "of_version": view["contract_version"],
        "reason": reason,
        "addresses": [{"kind": "acceptance", "id": "1", "disposition_id": None}],
        "instructions": "Keep every change inside the contract's allowed paths.",
        "resume_from": "remote_branch",
        "request_internal_review": False,
    }
    document.update(overrides)
    return document


def put_seeded_policy_in_force(ctx: AppContext) -> None:
    """Policies and routing policies are not truncated between tests, so the version in
    force is whatever the last test left, and some tests write versions directly, past
    validation. A test that writes routing versions of its own first puts in force a copy
    of the newest `default-software` whose documents validate and whose routing policy
    has the seeded shape of a fresh deployment: a Hermes local entry and no gateway URL
    on any local entry."""
    import copy  # noqa: PLC0415

    from crucible.application.policies import put_policy  # noqa: PLC0415
    from crucible.contracts.policy import parse_policy, parse_routing_policy  # noqa: PLC0415
    from crucible.domain.entities import Principal  # noqa: PLC0415

    def fresh(policy: Any, uow: Any) -> bool:
        ref = (policy.document.get("routing") or {}).get("policy") or {}
        routing = uow.routing_policies.get(str(ref.get("name", "")), int(ref.get("version", 0)))
        if policy.retired_at is not None or routing is None or routing.retired_at is not None:
            return False
        try:
            parse_policy(policy.document)
            parse_routing_policy(routing.document)
        except ValueError:
            return False
        local = [m for m in routing.document["models"] if m.get("endpoint") == "local"]
        return any(m.get("harness") == "hermes" for m in local) and not any(
            m.get("endpoint_url") for m in local
        )

    with ctx.uow_factory() as uow:
        versions = sorted(uow.policies.list_versions("default-software"), key=lambda p: p.version)
        chosen = next(p for p in reversed(versions) if fresh(p, uow))
        version = versions[-1].version + 1
        document = copy.deepcopy(chosen.document)
        document["version"] = version
        put_policy(
            uow,
            ctx.clock,
            principal=Principal(
                id="tests", name="tests", role=Role.ADMIN, created_at=ctx.clock.now()
            ),
            name="default-software",
            version=version,
            document=document,
            reason="tests: a fresh deployment's policy in force",
        )
        uow.commit()
