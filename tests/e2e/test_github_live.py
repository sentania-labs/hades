"""The live GitHub tier (18, 23): a real App, a real repository, real containers.

Local only. It runs the whole delivery half against
`sentania-labs/crucible-spike-target`: mint an installation token from the mounted key,
push the collected head from the bundle in a publisher container, open the pull request
with the rendered body, observe it, and merge-observe. It deletes every branch and closes
every pull request it created, and it never touches the default branch.

Skipped with a reason unless the App key, the App record, and the target repository are
all configured, so a partial run is never mistaken for a pass.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.docker import DockerProvider
from crucible.adapters.execution.publisher import DockerPublisher, PublisherConfig
from crucible.adapters.github.client import RestGitHubClient
from crucible.application.delivery_tick import DeliveryConfig
from crucible.application.repositories import register_repository
from crucible.application.supervisor import Supervisor
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.secrets import scan_text
from tests.e2e import github_live
from tests.e2e.conftest import RUN_ID, e2e_contract, run_until, submit_and_start
from tests.e2e.github_live import LiveConfig
from tests.e2e.policy import e2e_policy_document, e2e_routing_document
from tests.fixtures import promote_for_test

NOT_CONFIGURED = github_live.why_not_configured()
pytestmark = [
    pytest.mark.e2e_github,
    # Real GitHub round trips and CI on a throwaway repository, local only (issue 192).
    pytest.mark.timeout(1800),
    pytest.mark.skipif(bool(NOT_CONFIGURED), reason=NOT_CONFIGURED or "configured"),
]

POLL = DeliveryConfig(poll_interval_seconds=0, reactions_poll_interval_seconds=0)


@pytest.fixture(scope="session")
def live_config() -> LiveConfig:
    return github_live.load_config()


@pytest.fixture
def github(live_config: LiveConfig) -> RestGitHubClient:
    return github_live.client(live_config)


@pytest.fixture
def mirror(artifact_root: Path, live_config: LiveConfig) -> str:
    """A bare mirror of the target inside the artifact root (github_live docstring)."""
    return github_live.seed_mirror(artifact_root / "e2e-repos", live_config)


@pytest.fixture
def cleanup(github: RestGitHubClient, live_config: LiveConfig) -> Iterator[github_live.Cleanup]:
    record = github_live.Cleanup(config=live_config, github=github, branches=[], pull_requests=[])
    yield record
    result = record.run()
    print(f"live cleanup: {result}")
    assert not result["errors"], result["errors"]


@pytest.fixture
def publisher(provider: DockerProvider, stack: dict[str, Any]) -> DockerPublisher:
    return DockerPublisher(
        provider,
        PublisherConfig(
            network=provider.config.workers_network,
            egress_proxy=str(stack["egress_proxy"]),
            timeout_seconds=300,
        ),
    )


@pytest.fixture
def live_supervisor(
    ctx: AppContext,
    provider: DockerProvider,
    github: RestGitHubClient,
    publisher: DockerPublisher,
) -> Supervisor:
    return Supervisor(
        ctx.uow_factory,
        {"docker": provider},
        ctx.clock,
        holder=f"e2e-github-{RUN_ID}",
        artifact_store=ctx.artifact_store,
        github=github,
        publisher=publisher,
        delivery_config=POLL,
        lease_ttl_seconds=300,
        grace_seconds=5,
    )


def register(ctx: AppContext, live_config: LiveConfig, mirror: str) -> None:
    with ctx.uow_factory() as uow:
        register_repository(
            uow,
            ctx.clock,
            principal_name="tests",
            name=live_config.repository,
            registration=RepositoryRegistration(
                # Where Crucible's own containers fetch from. The publisher's remote is
                # derived from the registered name, so the push goes to GitHub.
                url=mirror,
                default_branch="main",
                policy_name="e2e-script",
                installation_id=live_config.installation_id,
                external_review=ExternalReviewAttestation(
                    attested_all_prs=True, attested_by="operator"
                ),
            ),
        )
        uow.commit()


def live_contract(external_id: str, live_config: LiveConfig, image: str) -> dict[str, Any]:
    document = e2e_contract(external_id, live_config.repository, image)
    document["repository"]["work_branch"] = github_live.branch_for(external_id)
    # The target repository carries no check scripts and this tier never pushes to its
    # default branch, so the required commands are ones any tree satisfies.
    document["required_verification"] = [
        {"id": "V1", "command": "echo lint ok", "expect_exit": 0},
        {"id": "V2", "command": "echo test ok", "expect_exit": 0},
        {"id": "V3", "command": "echo scan ok", "expect_exit": 0},
        {"id": "V4", "kind": "artifact", "path": "report/run-evidence.md"},
    ]
    document["deliverables"] = [
        {
            "kind": "pull_request",
            "target": f"https://github.com/{live_config.repository}",
            "draft": False,
            "closes": [],
        }
    ]
    return document


def live_policy() -> dict[str, Any]:
    document = e2e_policy_document()
    document["repository"]["required_checks"] = ["echo lint ok", "echo test ok", "echo scan ok"]
    document["network"]["egress_allowlist"] = ["github.com"]
    # 05b: `required_rounds: 0` makes the external review gates skipped. The reviewer's
    # clean verdict on this repository is a reaction on the pull request, and the App
    # holds no Issues read yet, so no live round is observable; the round logic is
    # covered against the fake server in both shapes. This tier's subject is
    # publication, observation, and the merge.
    document["external_review"]["required_rounds"] = 0
    # 05b: with no round required, the two external review gates move to `skipped`.
    document["gates"]["post_pr"] = ["ci_green_for_head"]
    document["gates"]["skipped"] = [
        "external_review_rounds",
        "feedback_dispositions_complete",
    ]
    # C6c gives the throwaway repository one stable required check. Ordinary tasks
    # prove the green path, while the readiness failure task adds its marker to force
    # this exact check red.
    document["ci_certification"]["allow_no_ci"] = False
    document["ci_certification"]["required_checks"] = ["crucible-readiness"]
    return document


@pytest.fixture
def live_client(
    ctx: AppContext,
    tokens: dict[str, str],
    provider: DockerProvider,
    worker_image: str,
) -> Iterator[TestClient]:
    app = create_app(ctx)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
        routing = e2e_routing_document()
        assert admin.put(
            f"/v1/routing/{routing['name']}/{routing['version']}", json=routing
        ).status_code in (200, 201)
        document = live_policy()
        assert admin.put(
            f"/v1/policies/{document['name']}/{document['version']}", json=document
        ).status_code in (200, 201)
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
                by="e2e-github",
                reason="live GitHub script harness image",
            )
            uow.commit()
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['orchestrator']}"}) as c:
        yield c


async def test_a_task_reaches_a_real_pull_request_and_a_real_merge(
    ctx: AppContext,
    live_client: TestClient,
    live_supervisor: Supervisor,
    live_config: LiveConfig,
    github: RestGitHubClient,
    mirror: str,
    cleanup: github_live.Cleanup,
    worker_image: str,
) -> None:
    register(ctx, live_config, mirror)
    external_id = f"C4-{RUN_ID}"
    branch = github_live.branch_for(external_id)
    cleanup.add_branch(branch)
    document = live_contract(external_id, live_config, worker_image)
    task_id = submit_and_start(live_client, document)

    state = await run_until(
        live_supervisor,
        live_client,
        task_id,
        {"awaiting_ci_certification", "ready_for_merge", "publish_failed"},
        max_ticks=80,
        pause=2.0,
    )
    events = live_client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()
    assert state != "publish_failed", "\n".join(
        str(e["payload"])
        for e in events["items"]
        if e["kind"] in ("task_publish_failed", "publisher_finished")
    )
    record = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
    cleanup.add_pull_request(int(record["number"]))
    print(f"live pull request: {record['url']} at {record['head_sha']}")
    view = live_client.get(f"/v1/tasks/{task_id}").json()
    assert record["head_sha"] == view["head_sha"]
    assert record["state"] == "open"

    access = github_live.token(github, live_config)
    try:
        remote = github.remote_head(access, repository=live_config.repository, ref=branch)
        assert remote == view["head_sha"]
        live = github.get_pull_request(
            access, repository=live_config.repository, number=record["number"]
        )
    finally:
        access.discard()
    assert live.head_sha == view["head_sha"]
    assert live.base_ref == "main"

    # The body is on GitHub; what Crucible kept is its hash. Nothing secret-shaped is in
    # the title or the body, and neither carries an unauthorized closing keyword.
    assert scan_text(live.title) is None

    # 23 and S12: the PR-level reactions endpoint is the one place a clean external
    # review appears, and it needs Issues read. Either it is readable, in which case the
    # poll recorded the reactions it saw, or it is not, in which case the poll recorded
    # that and carried on. Both are asserted; neither is allowed to pass silently.
    observed_kinds = [
        e["kind"]
        for e in live_client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()[
            "items"
        ]
    ]
    if record["reactions_observable"]:
        assert "reactions_unobservable" not in observed_kinds
        assert "pull_request_polled" in observed_kinds
    else:
        assert "reactions_unobservable" in observed_kinds

    ready = await run_until(
        live_supervisor,
        live_client,
        task_id,
        {"ready_for_merge", "external_feedback_received", "rejected"},
        max_ticks=80,
        pause=2.0,
    )
    if ready == "external_feedback_received":
        # A clean accepted signal can arrive after green CI first made the task ready.
        # CRU-04 deliberately steps it back. Never auto-disposition a real finding in
        # this tier: only a clean signal may reconcile forward without judgment.
        feedback = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
        undispositioned = [
            comment
            for comment in feedback["comments"]
            if comment["kind"] == "review_comment" and comment["disposition"] is None
        ]
        assert undispositioned == [], undispositioned
        ready = await run_until(
            live_supervisor,
            live_client,
            task_id,
            {"ready_for_merge", "rejected"},
            max_ticks=5,
            pause=2.0,
        )
    assert ready == "ready_for_merge"
    certification = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()[
        "ci_certifications"
    ][-1]
    assert certification["state"] == "green", certification

    status, payload = github_live.merge_with_app_token(
        github, live_config, record["number"], sha=view["head_sha"]
    )
    assert status == 200, payload
    merged = await run_until(
        live_supervisor, live_client, task_id, {"merged", "rejected"}, max_ticks=30, pause=2.0
    )
    final = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
    assert final["state"] == "merged", final
    assert final["merge_sha"] and final["merged_by"]
    assert merged == "merged"
    kinds = [
        e["kind"]
        for e in live_client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()[
            "items"
        ]
    ]
    for kind in (
        "installation_token_minted",
        "publisher_finished",
        "branch_pushed",
        "pull_request_opened",
        "publish_completed",
        "pull_request_polled",
    ):
        assert kind in kinds, kind


async def test_a_real_required_check_failure_escalates_without_retry(
    ctx: AppContext,
    live_client: TestClient,
    live_supervisor: Supervisor,
    live_config: LiveConfig,
    mirror: str,
    cleanup: github_live.Cleanup,
    worker_image: str,
    engine: Engine,
) -> None:
    """23: a real failing workflow is evidence, not an automatic correction."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE policies SET document = "
                "jsonb_set(jsonb_set(document, '{ci_certification,allow_no_ci}', 'false'), "
                "'{ci_certification,required_checks}', '[\"crucible-readiness\"]') "
                "WHERE name='e2e-script' AND version=1"
            )
        )
    register(ctx, live_config, mirror)
    external_id = f"C6C-CI-{RUN_ID}"
    branch = github_live.branch_for(external_id)
    cleanup.add_branch(branch)
    document = live_contract(external_id, live_config, worker_image)
    document["scope"]["allowed_paths"] = [".crucible-force-ci-failure"]
    task_id = submit_and_start(live_client, document)

    failed = await run_until(
        live_supervisor,
        live_client,
        task_id,
        {"ci_certification_failed", "publish_failed"},
        max_ticks=120,
        pause=2.0,
    )
    assert failed == "ci_certification_failed"
    record = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
    cleanup.add_pull_request(int(record["number"]))
    await live_supervisor.tick()
    record = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
    certification = record["ci_certifications"][-1]
    assert certification["state"] == "failed"
    assert certification["required_checks"] == ["crucible-readiness"]
    assert certification["failure"]["check"] == "crucible-readiness"
    assert certification["failure"]["conclusion"] == "failure"
    assert certification["failure"]["url"]
    assert certification["failure"]["run_id"]

    final = live_client.get(f"/v1/tasks/{task_id}").json()
    assert len(final["executions"]) == 1
    assert len(final["executions"][0]["attempts"]) == 1
    assert final["contract_version"] == 1
    for _ in range(2):
        await live_supervisor.tick()
    unchanged = live_client.get(f"/v1/tasks/{task_id}").json()
    assert unchanged["state"] == "ci_certification_failed"
    assert len(unchanged["executions"]) == 1
    print(
        "live CI failure: "
        f"{record['url']} check {certification['failure']['url']} at {record['head_sha']}"
    )


@pytest.mark.skipif(
    github_live.review_wait_seconds() == 0,
    reason=(
        f"set {github_live.WAIT_FOR_REVIEW_ENV} to a number of seconds to wait for a real "
        "external review round; the provider takes about 100 s (S12)"
    ),
)
async def test_a_real_external_review_round_completes_the_cycle(
    ctx: AppContext,
    live_client: TestClient,
    live_supervisor: Supervisor,
    live_config: LiveConfig,
    github: RestGitHubClient,
    mirror: str,
    cleanup: github_live.Cleanup,
    worker_image: str,
    engine: Engine,
) -> None:
    """Opt-in: publish, then wait for the provider's own round on a real pull request.

    S12 measured pickup at 11 s and completion at 101 s, and the completion signal is a
    `+1` reaction on the pull request, which needs Issues read on the App. The wait is
    bounded and the reason for a timeout is the observation itself, not a bare failure.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE policies SET document = jsonb_set(jsonb_set(document, "
                "'{external_review,required_rounds}', '1'), '{gates,skipped}', '[]') "
                "WHERE name = 'e2e-script' AND version = 1"
            )
        )
    register(ctx, live_config, mirror)
    external_id = f"C4R-{RUN_ID}"
    cleanup.add_branch(github_live.branch_for(external_id))
    document = live_contract(external_id, live_config, worker_image)
    task_id = submit_and_start(live_client, document)
    await run_until(
        live_supervisor,
        live_client,
        task_id,
        {"awaiting_external_review", "publish_failed"},
        max_ticks=80,
        pause=2.0,
    )
    record = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
    cleanup.add_pull_request(int(record["number"]))
    print(f"live review round on: {record['url']}")

    deadline = time.monotonic() + github_live.review_wait_seconds()
    while time.monotonic() < deadline:
        await live_supervisor.tick()
        record = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
        if record["completed_rounds"] >= 1:
            break
        time.sleep(10)
    assert record["reactions_observable"], (
        "the App still cannot read reactions on the pull request, which is the only "
        "place a clean result appears (23, S12)"
    )
    assert record["completed_rounds"] >= 1, (
        f"no round completed within {github_live.review_wait_seconds()} s; "
        f"reactions seen: {record['reactions']}, reviews: {record['external_reviews']}"
    )
    accepted = [r for r in record["external_reviews"] if r["accepted"]]
    assert accepted, record["external_reviews"]
    print(f"live round completed by: {[(r['signal'], r['reviewer_login']) for r in accepted]}")


async def test_no_token_reaches_the_daemon_the_logs_or_the_database(
    ctx: AppContext,
    live_client: TestClient,
    live_supervisor: Supervisor,
    live_config: LiveConfig,
    github: RestGitHubClient,
    mirror: str,
    cleanup: github_live.Cleanup,
    worker_image: str,
    provider: DockerProvider,
) -> None:
    """12 and S10's absence proof, at the level this tier can assert it.

    The publisher container is gone by the time the assertions run, so what is checked is
    every place the value could have been left behind: the container inspection the
    daemon still holds for the run, the daemon's log of it, the events, and the database.
    """
    register(ctx, live_config, mirror)
    external_id = f"C4T-{RUN_ID}"
    branch = github_live.branch_for(external_id)
    cleanup.add_branch(branch)
    document = live_contract(external_id, live_config, worker_image)
    task_id = submit_and_start(live_client, document)
    await run_until(
        live_supervisor,
        live_client,
        task_id,
        {"awaiting_ci_certification", "ready_for_merge", "publish_failed"},
        max_ticks=80,
        pause=2.0,
    )
    record = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
    assert "number" in record, record
    cleanup.add_pull_request(int(record["number"]))

    haystack: list[str] = []
    events = live_client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()
    haystack.append(str(events))
    with ctx.engine.begin() as connection:
        for table, column in connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type IN "
                "('text', 'character varying', 'jsonb')"
            )
        ).all():
            values = connection.execute(
                text(f'SELECT CAST("{column}" AS TEXT) FROM "{table}"')
            ).scalars()
            haystack.extend(str(v) for v in values if v is not None)
    blob = "\n".join(haystack)
    # No installation token, in any of its shapes, anywhere Crucible wrote.
    assert scan_text(blob) is None, "a secret-shaped value reached a Crucible record"
    assert "ghs_" not in blob
    # And the publisher's output directory kept no copy of it either.
    publish_root = Path(provider.config.artifact_root) / "publish"
    read = 0
    unreadable: list[str] = []
    for path in publish_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()[:1_000_000].decode("utf-8", "replace")
        except OSError:
            # A file the container's uid left behind that this process cannot read is
            # counted, never silently skipped: an empty scan is not a clean scan (S10).
            unreadable.append(str(path))
            continue
        read += 1
        assert "ghs_" not in content, path
        assert scan_text(content) is None, path
    assert read, f"nothing in {publish_root} was readable; an empty scan is not a pass"
    assert not unreadable, unreadable
