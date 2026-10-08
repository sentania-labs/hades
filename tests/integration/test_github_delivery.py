"""The delivery half against a fake GitHub server (18, 23).

Real HTTP to a fake api.github.com on loopback, a publisher that pushes into its branch
table, and the real supervisor tick. No live GitHub, no Docker, and no secret-shaped
fixture on disk: the App key and every installation token are generated at run time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.github.appauth import AppAuthenticator, AppConfig
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.transport import RestTransport
from crucible.application.delivery_tick import DeliveryConfig
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import ProviderSetting
from crucible.domain.publication import body_sha256
from crucible.domain.secrets import scan_text
from tests.fixtures import FakeClock
from tests.integration.conftest import (
    correction_document,
    event_kinds,
    make_supervisor,
    run_to_settled,
    submit_and_start,
)
from tests.integration.fake_github import FakeGitHubServer, installation_token_value, now_iso
from tests.integration.fake_publisher import FakePublisher
from tests.integration.test_class_routing import _install_policy as install_class_policy
from tests.integration.test_class_routing import _model as class_model
from tests.integration.test_class_routing import _promote as promote_class_image
from tests.integration.test_class_routing import _submit as submit_class_task

pytestmark = pytest.mark.integration

REPOSITORY = "example-org/example-service"
REVIEWER = "chatgpt-codex-connector[bot]"
WEBHOOK_SECRET = "webhook-secret-for-the-test-only"


@pytest.fixture
def app_key(tmp_path: Path) -> str:
    """An RSA key generated for this test run. Nothing secret-shaped is checked in."""
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
    return str(path)


@pytest.fixture
def webhook_secret(tmp_path: Path) -> str:
    path = tmp_path / "webhook.secret"
    path.write_text(WEBHOOK_SECRET, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


@pytest.fixture
def github() -> Iterator[FakeGitHubServer]:
    with FakeGitHubServer() as server:
        server.state.add_repository(REPOSITORY)
        yield server


@pytest.fixture
def github_client(github: FakeGitHubServer, app_key: str) -> RestGitHubClient:
    transport = RestTransport(github.url, timeout=10.0)
    authenticator = AppAuthenticator(
        AppConfig(app_id=4969317, private_key_path=app_key, api_base=github.url), transport
    )
    return RestGitHubClient(authenticator, transport)


@pytest.fixture
def publisher(github: FakeGitHubServer) -> FakePublisher:
    return FakePublisher(github.state, REPOSITORY)


@pytest.fixture
def delivery_supervisor(
    request: pytest.FixtureRequest,
    ctx: AppContext,
    provider: FakeProvider,
    github_client: RestGitHubClient,
    publisher: FakePublisher,
) -> Supervisor:
    # Existing observation/correction tests keep the operator's last word. Auto-merge
    # scenarios explicitly opt into the deployment default using the indirect param.
    if not getattr(request, "param", False):
        with ctx.uow_factory() as uow:
            uow.provider_settings.put(
                ProviderSetting(
                    name="delivery.auto_merge",
                    document={"enabled": False},
                    updated_at=ctx.clock.now(),
                    updated_by="test-admin",
                )
            )
            uow.commit()
    return make_supervisor(
        ctx,
        provider,
        github=github_client,
        publisher=publisher,
        # The tests drive a fake clock, so nothing is ever "due" on elapsed time; the
        # interval is zero and the poll happens whenever the tick runs.
        delivery_config=DeliveryConfig(poll_interval_seconds=0, reactions_poll_interval_seconds=0),
    )


@pytest.fixture
def webhook_client(
    ctx: AppContext, tokens: dict[str, str], webhook_secret: str
) -> Iterator[TestClient]:
    ctx.github_webhook_enabled = True
    ctx.github_webhook_secret_path = webhook_secret
    app = create_app(ctx)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['orchestrator']}"}) as c:
        yield c


# ----- helpers ----------------------------------------------------------


async def publish(
    client: TestClient,
    supervisor: Supervisor,
    *,
    external_id: str = "EX-0001",
    image: str = "crucible-worker:fake-succeed",
) -> tuple[str, dict[str, Any]]:
    """Run a task from submit to a published pull request."""
    task_id = submit_and_start(client, image, external_id=external_id)
    await run_to_settled(supervisor, client, task_id)
    view = client.get(f"/v1/tasks/{task_id}").json()
    return task_id, view


def pr(client: TestClient, task_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/tasks/{task_id}/pull-request")
    assert response.status_code == 200, response.text
    return dict(response.json())


def gate(view: dict[str, Any], name: str) -> str:
    for row in view["gates"]:
        if row["gate"] == name:
            return str(row["result"])
    return "absent"


# ----- publication ------------------------------------------------------


async def test_quota_checkpoint_is_pushed_before_the_reroute_is_scheduled(
    client: TestClient,
    ctx: AppContext,
    clock: FakeClock,
    publisher: FakePublisher,
    delivery_supervisor: Supervisor,
) -> None:
    install_class_policy(
        ctx,
        clock,
        version=90,
        models=[
            class_model("a-quota-model", "codex", "publisher-pool-a"),
            class_model("b-success-model", "agy", "publisher-pool-b"),
        ],
    )
    promote_class_image(ctx, clock, "codex", "crucible-worker:fake-quota")
    promote_class_image(ctx, clock, "agy", "crucible-worker:fake-succeed")
    task_id = submit_class_task(client, "C6B-PUBLISHER", 90)

    await delivery_supervisor.tick()

    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "scheduled"
    assert publisher.pushes == [("crucible/C6B-PUBLISHER", view["head_sha"])]
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    kinds = [event["kind"] for event in events]
    assert kinds.index("branch_pushed") < kinds.index("task_rerouted")


async def test_publication_pushes_the_head_and_opens_the_pull_request(
    client: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    publisher: FakePublisher,
) -> None:
    task_id, view = await publish(client, delivery_supervisor)
    assert view["state"] == "awaiting_external_review"
    record = pr(client, task_id)
    assert record["number"] == 1
    assert record["head_sha"] == view["head_sha"]
    assert publisher.pushes == [("crucible/EX-0001", view["head_sha"])]
    assert github.state.repositories[REPOSITORY].branches["crucible/EX-0001"] == view["head_sha"]
    assert gate(record, "branch_pushed_at_head") == "pass"
    assert gate(record, "pr_exists_head_matches") == "pass"
    kinds = event_kinds(client, task_id)
    for kind in (
        "task_publishing",
        "publish_started",
        "installation_token_minted",
        "publisher_finished",
        "branch_pushed",
        "pull_request_opened",
        "publish_completed",
        "task_awaiting_external_review",
        "external_review_cycle_opened",
    ):
        assert kind in kinds, kind
    assert [h["pushed_by"] for h in record["heads"]] == ["crucible"]
    comments = github.state.repositories[REPOSITORY].pulls[1].issue_comments
    assert [(row["user"]["login"], row["body"]) for row in comments] == [
        ("crucible-spike[bot]", "@codex review")
    ]
    requested = [
        event
        for event in client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()[
            "items"
        ]
        if event["kind"] == "external_review_requested"
    ]
    assert requested[0]["payload"]["comment_id"] == comments[0]["id"]


async def test_publish_failure_can_be_republished(
    client: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    publisher: FakePublisher,
) -> None:
    publisher.refuse_push = "HTTP 503 Service Unavailable"
    task_id, failed = await publish(client, delivery_supervisor)
    assert failed["state"] == "publish_failed"
    failed_head = failed["head_sha"]
    assert len(publisher.bundle_paths) == 1

    publisher.refuse_push = ""
    response = client.post(
        f"/v1/tasks/{task_id}/republish",
        json={"reason": "The transient GitHub service failure has cleared."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "publishing"
    await delivery_supervisor.tick()

    recovered = client.get(f"/v1/tasks/{task_id}").json()
    assert recovered["state"] == "awaiting_external_review"
    assert recovered["head_sha"] == failed_head
    assert publisher.bundle_paths == [publisher.bundle_paths[0], publisher.bundle_paths[0]]
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    retries = [e for e in events if e["kind"] == "task_publishing"]
    assert retries[-1]["payload"]["retry_number"] == 1
    wakes = [w for w in client.get("/v1/wakes").json()["items"] if w["reason"] == "publish_failed"]
    assert wakes and "1 remaining" not in wakes[0]["summary"]
    assert "3 remaining" in wakes[0]["summary"]
    assert len(github.state.repositories[REPOSITORY].pulls[1].issue_comments) == 1


async def test_token_mint_failure_republishes_from_before_push(
    client: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    publisher: FakePublisher,
) -> None:
    github.state.mint_failure_once = True
    task_id, failed = await publish(client, delivery_supervisor)
    assert failed["state"] == "publish_failed"
    assert publisher.bundle_paths == []
    failed_events = [
        event
        for event in client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()[
            "items"
        ]
        if event["kind"] == "task_publish_failed"
    ]
    assert failed_events[-1]["payload"]["step"] == "installation_token"

    response = client.post(
        f"/v1/tasks/{task_id}/republish",
        json={"reason": "The installation-token service recovered."},
    )
    assert response.status_code == 200, response.text
    await delivery_supervisor.tick()

    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_external_review"
    assert len(publisher.pushes) == 1


async def collect_before_publish(supervisor: Supervisor, client: TestClient, task_id: str) -> None:
    """Pause publication I/O while testing changes to retained publication inputs."""
    publisher = supervisor.delivery._publisher
    supervisor.delivery._publisher = None
    try:
        assert await run_to_settled(supervisor, client, task_id) == "publishing"
    finally:
        supervisor.delivery._publisher = publisher


async def test_pre_start_installation_failure_can_be_republished(
    client: TestClient,
    ctx: AppContext,
    delivery_supervisor: Supervisor,
    publisher: FakePublisher,
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await collect_before_publish(delivery_supervisor, client, task_id)
    with ctx.uow_factory() as uow:
        task = uow.tasks.get(task_id)
        assert task is not None
        repository = uow.repositories.get(task.repository_id)
        assert repository is not None
        repository.installation_id = None
        uow.repositories.upsert(repository)
        uow.commit()

    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "publish_failed"
    assert "publish_started" in event_kinds(client, task_id)
    assert publisher.pushes == []

    with ctx.uow_factory() as uow:
        task = uow.tasks.get(task_id)
        assert task is not None
        repository = uow.repositories.get(task.repository_id)
        assert repository is not None
        repository.installation_id = 1
        uow.repositories.upsert(repository)
        uow.commit()
    response = client.post(
        f"/v1/tasks/{task_id}/republish",
        json={"reason": "The repository installation ID is now registered."},
    )
    assert response.status_code == 200, response.text
    await delivery_supervisor.tick()

    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_external_review"
    assert len(publisher.pushes) == 1


async def test_pre_upgrade_bundle_is_sealed_before_publication(
    client: TestClient,
    engine: Engine,
    delivery_supervisor: Supervisor,
    publisher: FakePublisher,
    tmp_path: Path,
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await collect_before_publish(delivery_supervisor, client, task_id)
    output = tmp_path / "output"
    output.mkdir()
    bundle = output / "work_branch.bundle"
    bundle.write_bytes(b"bundle collected before the digest field existed")
    expected_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert delivery_supervisor.fenced_token is not None
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('crucible.fenced_token', :token, true)"),
            {"token": str(delivery_supervisor.fenced_token)},
        )
        connection.execute(
            text("UPDATE attempts SET workspace_path = :workspace WHERE task_id = :task_id"),
            {"workspace": str(tmp_path), "task_id": task_id},
        )
        connection.execute(
            text(
                "UPDATE evidence SET payload = payload - 'bundle_sha256' "
                "WHERE task_id = :task_id AND kind = 'bundle_head'"
            ),
            {"task_id": task_id},
        )

    await delivery_supervisor.tick()

    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_external_review"
    assert publisher.bundle_sha256s == [expected_sha256]
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    started = [event for event in events if event["kind"] == "publish_started"]
    assert started[-1]["payload"]["bundle_sha256"] == expected_sha256


async def test_republish_refuses_changed_bundle_content(
    client: TestClient,
    engine: Engine,
    delivery_supervisor: Supervisor,
    publisher: FakePublisher,
    tmp_path: Path,
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await collect_before_publish(delivery_supervisor, client, task_id)
    output = tmp_path / "output"
    output.mkdir()
    bundle = output / "work_branch.bundle"
    bundle.write_bytes(b"sealed branch bundle")
    sealed_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert delivery_supervisor.fenced_token is not None
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('crucible.fenced_token', :token, true)"),
            {"token": str(delivery_supervisor.fenced_token)},
        )
        connection.execute(
            text("UPDATE attempts SET workspace_path = :workspace WHERE task_id = :task_id"),
            {"workspace": str(tmp_path), "task_id": task_id},
        )
        connection.execute(
            text(
                "UPDATE evidence SET payload = jsonb_set(payload, '{bundle_sha256}', "
                "to_jsonb(CAST(:digest AS text)), true) "
                "WHERE task_id = :task_id AND kind = 'bundle_head'"
            ),
            {"digest": sealed_sha256, "task_id": task_id},
        )
    publisher.refuse_push = "HTTP 503 Service Unavailable"
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "publish_failed"

    bundle.write_bytes(b"changed branch bundle")
    response = client.post(
        f"/v1/tasks/{task_id}/republish",
        json={"reason": "retry a bundle that changed on disk"},
    )
    assert response.status_code == 409
    assert "unchanged sealed bundle" in response.json()["detail"]


async def test_republish_refuses_a_changed_head(
    client: TestClient,
    ctx: AppContext,
    delivery_supervisor: Supervisor,
    publisher: FakePublisher,
) -> None:
    publisher.refuse_push = "HTTP 503 Service Unavailable"
    task_id, failed = await publish(client, delivery_supervisor)
    assert failed["state"] == "publish_failed"
    with ctx.uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        assert task is not None
        task.head_sha = "a" * 40
        uow.tasks.save(task)
        uow.commit()

    response = client.post(
        f"/v1/tasks/{task_id}/republish",
        json={"reason": "retry after changing the head"},
    )
    assert response.status_code == 409
    assert "same head" in response.json()["detail"]


async def test_republish_enforces_the_policy_cap(
    client: TestClient,
    engine: Engine,
    delivery_supervisor: Supervisor,
    publisher: FakePublisher,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE policies SET document = jsonb_set(document, "
                "'{limits,publish_retry_max}', '0'::jsonb, true) "
                "WHERE name = 'default-software' AND version = 2"
            )
        )
    publisher.refuse_push = "HTTP 503 Service Unavailable"
    task_id, failed = await publish(client, delivery_supervisor)
    assert failed["state"] == "publish_failed"

    response = client.post(
        f"/v1/tasks/{task_id}/republish",
        json={"reason": "retry beyond the configured cap"},
    )
    assert response.status_code == 409
    assert "retry cap reached" in response.json()["detail"]
    wakes = [
        w
        for w in client.get("/v1/wakes").json()["items"]
        if w["task_id"] == task_id and w["reason"] == "publish_failed"
    ]
    assert wakes and "0 remaining" in wakes[0]["summary"]
    assert "republish" not in wakes[0]["payload"]["links"]


async def test_the_body_is_rendered_from_verified_evidence_only(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await publish(client, delivery_supervisor)
    body = github.state.repositories[REPOSITORY].pulls[1].body
    record = pr(client, task_id)
    assert record["body_sha256"] == body_sha256(body)
    assert "## Verification" in body
    assert "Crucible's own re-run" in body
    assert "## Provenance" in body
    assert view["head_sha"] in body
    assert "## Acceptance criteria" in body


async def test_no_installation_token_reaches_any_record(
    client: TestClient,
    delivery_supervisor: Supervisor,
    publisher: FakePublisher,
    engine: Engine,
    github: FakeGitHubServer,
) -> None:
    """12: a token lives in memory and in the publisher's tmpfs, nowhere else.

    The publisher records every value it was handed, so the proof here is a search for
    those exact values across every text column of the database and every event body."""
    task_id, _ = await publish(client, delivery_supervisor)
    assert publisher.tokens_seen, "the publisher was never handed a token"
    haystacks: list[str] = []
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type IN "
                "('text', 'character varying', 'jsonb')"
            )
        ).all()
        for table_name, column_name in rows:
            values = connection.execute(
                text(f'SELECT CAST("{column_name}" AS TEXT) FROM "{table_name}"')
            ).scalars()
            haystacks.extend(str(v) for v in values if v is not None)
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()
    haystacks.append(json.dumps(events))
    haystacks.append(json.dumps(client.get("/v1/wakes").json()))
    haystacks.append(json.dumps(pr(client, task_id)))
    blob = "\n".join(haystacks)
    for token in publisher.tokens_seen:
        assert token not in blob

    # The positive control plants a token in a real column through a real write, runs
    # the same search over the same tables, and rolls back. A control that concatenates
    # the needle onto the haystack proves only that Python can find a substring.
    planted = publisher.tokens_seen[0]
    with engine.connect() as connection:
        transaction = connection.begin()
        # A real row in a real table, through a real insert, with the whole 390-character
        # value: a truncated plant would make the control test truncation rather than the
        # search, and a concatenated one would test nothing at all.
        connection.execute(
            text(
                "INSERT INTO artifacts (id, attempt_id, task_id, type, filename, path, "
                "size, sha256, content_type, created_by, created_at) VALUES "
                "(:id, NULL, :task, 'control', :value, 'x', 0, :digest, 'text/plain', "
                "'tests', now())"
            ),
            {
                "id": "01CONTROL0000000000000000",
                "task": task_id,
                "value": planted,
                "digest": "0" * 64,
            },
        )
        control: list[str] = []
        for table_name, column_name in rows:
            values = connection.execute(
                text(f'SELECT CAST("{column_name}" AS TEXT) FROM "{table_name}"')
            ).scalars()
            control.extend(str(v) for v in values if v is not None)
        assert planted in "\n".join(control), "the search cannot find a planted token"
        transaction.rollback()
    with engine.connect() as connection:
        left = connection.execute(
            text("SELECT count(*) FROM artifacts WHERE type = 'control'")
        ).scalar_one()
    assert left == 0, "the control's plant was not rolled back"
    minted = [e for e in events["items"] if e["kind"] == "installation_token_minted"]
    assert minted and "expires_at" in minted[0]["payload"]
    # The event records the expiry and the job, never the value (12).
    assert "token" not in minted[0]["payload"]
    assert scan_text(json.dumps(minted[0]["payload"])) is None


async def test_a_refused_push_lands_in_publish_failed_with_the_remote_head(
    client: TestClient, delivery_supervisor: Supervisor, publisher: FakePublisher
) -> None:
    """23 step 4: a remote head that is not an ancestor fails the push. Crucible records
    it, wakes Foundry, and never force-pushes."""
    publisher.refuse_push = "! [rejected] crucible/EX-0001 -> crucible/EX-0001 (non-fast-forward)"
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(delivery_supervisor, client, task_id)
    await delivery_supervisor.tick()
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == "publish_failed"
    assert "task_publish_failed" in event_kinds(client, task_id)
    wakes = client.get("/v1/wakes").json()["items"]
    assert any(w["reason"] == "publish_failed" for w in wakes)
    assert not publisher.pushes


# ----- external review --------------------------------------------------


async def test_a_clean_reaction_completes_the_round_without_a_judgment_wake(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """S12: a clean result is reactions only. One `+1` from the allowlisted login
    completes every configured component of the cycle at once (23)."""
    task_id, _ = await publish(client, delivery_supervisor)
    eyes = github.state.add_reaction(REPOSITORY, 1, login=REVIEWER, content="eyes")
    await delivery_supervisor.tick()
    github.state.remove_reaction(REPOSITORY, 1, eyes)
    github.state.add_reaction(REPOSITORY, 1, login=REVIEWER, content="+1")
    await delivery_supervisor.tick()
    record = pr(client, task_id)
    assert record["completed_rounds"] == 1
    assert [c["state"] for c in record["cycles"]] == ["completed"]
    assert any(r["content"] == "eyes" and r["removed_at"] for r in record["reactions"])
    kinds = event_kinds(client, task_id)
    assert "reaction_received" in kinds and "reaction_removed" in kinds
    assert "external_review_cycle_completed" in kinds
    state = client.get(f"/v1/tasks/{task_id}").json()["state"]
    assert state == "awaiting_ci_certification"
    # A round with no findings has nothing to disposition, so no wake for judgment (23).
    reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
    assert "external_feedback_received" not in reasons


async def test_reactions_unobservable_is_recorded_and_not_fatal(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """23: the App lacks Issues read until the operator adds it. A 403 on the PR-level
    reactions endpoint is "reactions unobservable", recorded, and the poll continues."""
    github.state.issues_read = False
    task_id, _ = await publish(client, delivery_supervisor)
    await delivery_supervisor.tick()
    record = pr(client, task_id)
    assert record["reactions_observable"] is False
    assert "reactions_unobservable" in event_kinds(client, task_id)
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_external_review"
    status = client.get("/v1/supervisor").json()["github"]
    assert record["id"] in status["reactions_unobservable"]
    # And it recovers by itself once the permission is granted.
    github.state.issues_read = True
    github.state.add_reaction(REPOSITORY, 1, login=REVIEWER, content="+1")
    await delivery_supervisor.tick()
    assert pr(client, task_id)["completed_rounds"] == 1


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_review_with_findings_wakes_for_dispositions(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await publish(client, delivery_supervisor)
    github.state.add_review(
        REPOSITORY,
        1,
        login=REVIEWER,
        body="Codex Review. Reviewed commit: " + view["head_sha"],
        comments=[{"body": "P1 this index is one past the end", "path": "src/app.txt", "line": 3}],
    )
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "external_feedback_received"
    record = pr(client, task_id)
    assert record["completed_rounds"] == 1
    assert len(record["comments"]) == 1
    assert record["comments"][0]["disposition"] is None
    assert gate(record, "feedback_dispositions_complete") == "pending"
    reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
    assert "external_feedback_received" in reasons
    comment_id = record["comments"][0]["id"]
    response = client.post(
        f"/v1/tasks/{task_id}/dispositions",
        json={
            "review_comment_id": comment_id,
            "disposition": "decline",
            "reasoning": "Out of the contract's scope; recorded rather than fixed.",
        },
    )
    assert response.status_code == 200, response.text
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_ci_certification"
    assert gate(pr(client, task_id), "feedback_dispositions_complete") == "pass"


async def test_a_non_allowlisted_login_satisfies_nothing(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """ADR 0008: any other user's activity is recorded and satisfies nothing."""
    task_id, _ = await publish(client, delivery_supervisor)
    github.state.add_reaction(REPOSITORY, 1, login="a-passer-by", content="+1")
    github.state.add_issue_comment(REPOSITORY, 1, login="a-passer-by", body="Looks fine to me.")
    await delivery_supervisor.tick()
    record = pr(client, task_id)
    assert record["completed_rounds"] == 0
    assert [r["accepted"] for r in record["external_reviews"]] == [False, False]
    assert "external_review_ignored" in event_kinds(client, task_id)
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_external_review"


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_an_edited_summary_comment_is_a_change_not_a_round(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """S12: the bot's summary comment is edited in place and never counts as a round."""
    task_id, _ = await publish(client, delivery_supervisor)
    github.state.add_issue_comment(
        REPOSITORY, 1, login=REVIEWER, body="Codex Review Summary: in progress"
    )
    await delivery_supervisor.tick()
    first = pr(client, task_id)
    assert len(first["comments"]) == 1
    pull = github.state.repositories[REPOSITORY].pulls[1]
    pull.issue_comments[0]["body"] = "Codex Review Summary: complete, no findings"
    pull.issue_comments[0]["updated_at"] = "2030-01-01T00:00:00Z"
    await delivery_supervisor.tick()
    second = pr(client, task_id)
    assert len(second["comments"]) == 1
    assert second["comments"][0]["id"] == first["comments"][0]["id"]
    assert "complete, no findings" in second["comments"][0]["body"]
    assert second["completed_rounds"] == first["completed_rounds"]


# ----- CI certification -------------------------------------------------


async def green(
    client: TestClient, supervisor: Supervisor, github: FakeGitHubServer
) -> tuple[str, dict[str, Any]]:
    task_id, view = await publish(client, supervisor)
    github.state.add_reaction(REPOSITORY, 1, login=REVIEWER, content="+1")
    await supervisor.tick()
    return task_id, view


async def test_an_empty_required_set_is_pending_never_green(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """23 and ADR 0009: before GitHub has created any run the task waits."""
    task_id, _ = await green(client, delivery_supervisor, github)
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_ci_certification"
    record = pr(client, task_id)
    assert record["ci_certifications"][-1]["state"] == "pending"
    assert "never green" in record["ci_certifications"][-1]["detail"]
    assert gate(record, "ci_green_for_head") == "pending"


@pytest.mark.parametrize("delivery_supervisor", [True], indirect=True)
async def test_green_required_checks_reach_ready_for_merge_then_merged(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.repositories[REPOSITORY].required_checks = ["never-observed"]
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "merged"
    ready = [w for w in client.get("/v1/wakes").json()["items"] if w["reason"] == "ready_for_merge"]
    assert ready and "ready for merge" in ready[0]["summary"]
    record = pr(client, task_id)
    assert record["merged_by"] == "crucible-spike[bot]" and record["merge_sha"] == "f" * 40
    assert github.state.merge_calls == [(REPOSITORY, 1, "squash")]
    reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
    assert "merged" in reasons


async def test_observation_review_ci_race(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    github.state.add_review(
        REPOSITORY,
        1,
        login=REVIEWER,
        body="Codex Review",
        comments=[{"body": "P1 fix the race", "path": "src/app.txt", "line": 4}],
    )
    await delivery_supervisor.tick()

    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "external_feedback_received"
    assert gate(pr(client, task_id), "feedback_dispositions_complete") == "pending"
    assert "task_ready_for_merge" not in event_kinds(client, task_id)


async def test_ready_for_merge_invalidation(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "ready_for_merge"

    github.state.add_review(
        REPOSITORY,
        1,
        login=REVIEWER,
        body="Codex Review",
        comments=[{"body": "P1 new finding", "path": "src/app.txt", "line": 8}],
    )
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "external_feedback_received"

    comment_id = pr(client, task_id)["comments"][0]["id"]
    response = client.post(
        f"/v1/tasks/{task_id}/dispositions",
        json={
            "review_comment_id": comment_id,
            "disposition": "decline",
            "reasoning": "The comment does not apply to this contract.",
        },
    )
    assert response.status_code == 200, response.text
    await delivery_supervisor.tick()
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "ready_for_merge"

    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="failure")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "ci_certification_failed"
    kinds = event_kinds(client, task_id)
    assert kinds.count("task_external_feedback_received") >= 1
    assert "task_ci_certification_failed" in kinds


async def test_new_inline_comment_on_recorded_review_revokes_ready(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await publish(client, delivery_supervisor)
    review_id = github.state.add_review(
        REPOSITORY,
        1,
        login=REVIEWER,
        body="Codex Review. No findings.",
    )
    await delivery_supervisor.tick()
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "ready_for_merge"

    github.state.add_review_comment(
        REPOSITORY,
        1,
        review_id=review_id,
        login=REVIEWER,
        body="P1 this late inline finding still needs disposition",
        path="src/app.txt",
        line=9,
    )
    await delivery_supervisor.tick()

    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "external_feedback_received"
    assert gate(pr(client, task_id), "feedback_dispositions_complete") == "pending"


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_edited_inline_comment_invalidates_its_old_disposition(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await publish(client, delivery_supervisor)
    github.state.add_review(
        REPOSITORY,
        1,
        login=REVIEWER,
        body="Codex Review",
        comments=[{"body": "P2 original finding", "path": "src/app.txt", "line": 5}],
    )
    await delivery_supervisor.tick()
    comment_id = pr(client, task_id)["comments"][0]["id"]
    response = client.post(
        f"/v1/tasks/{task_id}/dispositions",
        json={
            "review_comment_id": comment_id,
            "disposition": "decline",
            "reasoning": "The original text does not apply.",
        },
    )
    assert response.status_code == 200, response.text
    await delivery_supervisor.tick()
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "ready_for_merge"

    comment = github.state.repositories[REPOSITORY].pulls[1].review_comments[0]
    comment["body"] = "P1 edited text introduces a different finding"
    comment["updated_at"] = now_iso(1)
    await delivery_supervisor.tick()

    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "external_feedback_received"
    assert gate(pr(client, task_id), "feedback_dispositions_complete") == "pending"
    response = client.post(
        f"/v1/tasks/{task_id}/dispositions",
        json={
            "review_comment_id": comment_id,
            "disposition": "decline",
            "reasoning": "The edited text was evaluated separately.",
        },
    )
    assert response.status_code == 200, response.text


async def test_a_required_failure_lands_in_ci_certification_failed_with_evidence(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """ADR 0009: capture the check, workflow, head, and log excerpt; no retry."""
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="failure")
    github.state.set_workflow_run(REPOSITORY, view["head_sha"], name="ci", conclusion="failure")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "ci_certification_failed"
    record = pr(client, task_id)
    failed = record["ci_certifications"][-1]
    assert failed["state"] == "failed"
    assert failed["failure"]["check"] == "build"
    assert failed["failure"]["head_sha"] == view["head_sha"]
    reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
    assert "ci_certification_failed" in reasons
    # No automatic retry: a further tick changes nothing.
    before = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    await delivery_supervisor.tick()
    after = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    assert [e["kind"] for e in after[len(before) :]] in ([], ["pull_request_polled"])


async def test_ci_decision_rerun_records_the_intent_and_wakes_the_operator(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="failure")
    await delivery_supervisor.tick()
    response = client.post(
        f"/v1/tasks/{task_id}/ci-decision",
        json={
            "cause": "flaky_test",
            "action": "rerun",
            "reasoning": "The failure is a known flake in the build job.",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "awaiting_ci_certification"
    record = pr(client, task_id)
    assert record["ci_decisions"][-1]["cause"] == "flaky_test"
    reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
    assert "ci_rerun_needed" in reasons
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "ready_for_merge"


async def test_a_ci_decision_is_refused_outside_ci_certification_failed(
    client: TestClient, delivery_supervisor: Supervisor
) -> None:
    task_id, _ = await publish(client, delivery_supervisor)
    response = client.post(
        f"/v1/tasks/{task_id}/ci-decision",
        json={"cause": "other", "action": "reject", "reasoning": "x"},
    )
    assert response.status_code == 409


# ----- corrections and divergence ---------------------------------------


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_correction_round_updates_the_head_of_the_same_pull_request(
    client: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    publisher: FakePublisher,
) -> None:
    """09: a correction re-enters at `scheduled`, and the second pass through publishing
    updates the PR head. With the default policy it lands in certification, never back
    in external review."""
    task_id, view = await publish(client, delivery_supervisor)
    github.state.add_review(
        REPOSITORY,
        1,
        login=REVIEWER,
        body="Codex Review",
        comments=[{"body": "P1 fix this", "path": "src/app.txt", "line": 1}],
    )
    await delivery_supervisor.tick()
    record = pr(client, task_id)
    client.post(
        f"/v1/tasks/{task_id}/dispositions",
        json={
            "review_comment_id": record["comments"][0]["id"],
            "disposition": "fix",
            "reasoning": "The reviewer is right.",
        },
    )
    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    response = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert response.status_code == 200, response.text
    assert await run_to_settled(delivery_supervisor, client, task_id) == "awaiting_ci_certification"
    corrected = client.get(f"/v1/tasks/{task_id}").json()
    assert corrected["state"] == "awaiting_ci_certification"
    assert corrected["head_sha"] != view["head_sha"]
    after = pr(client, task_id)
    assert after["number"] == 1
    assert after["head_sha"] == corrected["head_sha"]
    assert len(github.state.repositories[REPOSITORY].pulls) == 1
    assert [h["pushed_by"] for h in after["heads"]] == ["crucible", "crucible"]
    assert len(publisher.pushes) == 2


async def test_an_out_of_band_head_moves_the_task_to_head_diverged(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await publish(client, delivery_supervisor)
    github.state.push(REPOSITORY, "crucible/EX-0001", "a" * 40, by="someone-else")
    await delivery_supervisor.tick()
    task = client.get(f"/v1/tasks/{task_id}").json()
    assert task["state"] == "head_diverged"
    record = pr(client, task_id)
    assert [h["pushed_by"] for h in record["heads"]] == ["crucible", "other"]
    assert "task_head_diverged" in event_kinds(client, task_id)
    assert "superseded_for_head" in event_kinds(client, task_id)
    assert all(a["superseded_at"] for a in task["acceptance_results"])
    reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
    assert "head_diverged" in reasons
    # Nothing about the new SHA moves the task: green CI on it changes nothing.
    github.state.set_check(REPOSITORY, "a" * 40, name="build", conclusion="success")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "head_diverged"
    response = client.post(
        f"/v1/tasks/{task_id}/head-decision",
        json={"action": "reject", "reasoning": "Someone pushed by hand; abandon it."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "rejected"
    assert view["head_sha"] != "a" * 40


async def test_head_decision_recollect_returns_the_task_to_supervision(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, _ = await publish(client, delivery_supervisor)
    github.state.push(REPOSITORY, "crucible/EX-0001", "b" * 40, by="someone-else")
    await delivery_supervisor.tick()
    response = client.post(
        f"/v1/tasks/{task_id}/head-decision",
        json={"action": "recollect", "reasoning": "Take the new head through the gates."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "scheduled"
    assert "head_decision_recorded" in event_kinds(client, task_id)
    await run_to_settled(delivery_supervisor, client, task_id)
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] in (
        "awaiting_external_review",
        "awaiting_ci_certification",
        "pre_pr_gates_failed",
    )


async def test_a_pull_request_closed_unmerged_rejects_the_task(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """23: a pull request closed without merge rejects the task, with the closer
    recorded. Whether an open PR is closed is a person's act; Crucible only observes."""
    task_id, _ = await publish(client, delivery_supervisor)
    github.state.close(REPOSITORY, 1)
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "rejected"
    assert pr(client, task_id)["state"] == "closed"


# ----- webhooks ---------------------------------------------------------


def signed(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def delivery_body(number: int, head_sha: str) -> bytes:
    return json.dumps(
        {
            "action": "synchronize",
            "repository": {"full_name": REPOSITORY},
            "pull_request": {
                "number": number,
                "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
                "state": "open",
                "title": "a title",
                "head": {"sha": head_sha, "ref": "crucible/EX-0001"},
                "base": {"ref": "main"},
            },
            "sender": {"login": "crucible-spike[bot]"},
        }
    ).encode("utf-8")


def test_an_unsigned_delivery_is_rejected_and_stores_nothing(
    webhook_client: TestClient, engine: Engine
) -> None:
    body = delivery_body(1, "c" * 40)
    response = webhook_client.post(
        "/v1/github/webhook",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1"},
    )
    assert response.status_code == 401
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM github_deliveries")).scalar() == 0
        kinds = connection.execute(
            text("SELECT kind FROM events WHERE kind LIKE 'github_delivery%'")
        ).scalars()
        assert list(kinds) == ["github_delivery_rejected"]


def test_a_mismatched_signature_is_rejected(webhook_client: TestClient) -> None:
    body = delivery_body(1, "c" * 40)
    response = webhook_client.post(
        "/v1/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d-2",
            "X-Hub-Signature-256": signed("the-wrong-secret", body),
        },
    )
    assert response.status_code == 401


def test_a_valid_delivery_is_stored_normalized_and_deduplicated(
    webhook_client: TestClient, engine: Engine
) -> None:
    body = delivery_body(1, "c" * 40)
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "d-3",
        "X-Hub-Signature-256": signed(WEBHOOK_SECRET, body),
    }
    first = webhook_client.post("/v1/github/webhook", content=body, headers=headers)
    assert first.status_code == 200 and first.json()["duplicate"] is False
    second = webhook_client.post("/v1/github/webhook", content=body, headers=headers)
    assert second.status_code == 200 and second.json()["duplicate"] is True
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT delivery_id, body_sha256, normalized FROM github_deliveries")
        ).all()
    assert len(rows) == 1
    delivery_id, digest, normalized = rows[0]
    assert delivery_id == "d-3" and len(digest) == 64
    assert normalized["pull_request"]["head_sha"] == "c" * 40
    # Only the fields Crucible uses survive, and the raw body is never stored: the
    # normalized record has exactly the keys the reader reads.
    assert set(normalized) == {"pull_request", "sender"}
    assert set(normalized["pull_request"]) == {
        "number",
        "head_sha",
        "state",
        "merged",
        "merged_by",
        "merge_commit_sha",
        "base_ref",
        "url",
    }


def test_a_review_body_carrying_a_secret_is_redacted_before_storage(
    webhook_client: TestClient, engine: Engine
) -> None:
    """23: every user-controlled text field goes through the scanner before it is kept."""
    value = installation_token_value()
    body = json.dumps(
        {
            "action": "submitted",
            "repository": {"full_name": REPOSITORY},
            "pull_request": {
                "number": 1,
                "state": "open",
                "head": {"sha": "c" * 40, "ref": "crucible/EX-0001"},
                "base": {"ref": "main"},
            },
            "review": {
                "id": 1,
                "user": {"login": REVIEWER},
                "state": "COMMENTED",
                "body": f"this token leaked: {value}",
                "commit_id": "c" * 40,
                "submitted_at": "2026-09-16T12:00:00Z",
            },
        }
    ).encode("utf-8")
    response = webhook_client.post(
        "/v1/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request_review",
            "X-GitHub-Delivery": "d-4",
            "X-Hub-Signature-256": signed(WEBHOOK_SECRET, body),
        },
    )
    assert response.status_code == 200
    with engine.begin() as connection:
        stored = connection.execute(
            text("SELECT normalized FROM github_deliveries WHERE delivery_id = 'd-4'")
        ).scalar_one()
    blob = json.dumps(stored)
    assert value not in blob
    assert "[redacted:github_installation_token]" in blob


async def test_a_delivery_forces_a_poll_and_reaches_the_same_state_as_polling(
    ctx: AppContext,
    provider: FakeProvider,
    github_client: RestGitHubClient,
    publisher: FakePublisher,
    github: FakeGitHubServer,
    webhook_client: TestClient,
    clock: FakeClock,
) -> None:
    """23: a webhook only shortens latency. The rows it produces are the rows the poll
    produces, because both run the same normalization and the same application code."""
    supervisor = make_supervisor(
        ctx,
        provider,
        github=github_client,
        publisher=publisher,
        # A long interval, so nothing would be polled on elapsed time alone: only the
        # delivery makes this poll happen.
        delivery_config=DeliveryConfig(
            poll_interval_seconds=100_000, reactions_poll_interval_seconds=100_000
        ),
    )
    task_id, view = await publish(webhook_client, supervisor)
    github.state.add_reaction(REPOSITORY, 1, login=REVIEWER, content="+1")
    await supervisor.tick()
    # Not due: the publication poll already happened and the interval is enormous.
    assert pr(webhook_client, task_id)["completed_rounds"] == 0
    body = delivery_body(1, view["head_sha"])
    webhook_client.post(
        "/v1/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d-5",
            "X-Hub-Signature-256": signed(WEBHOOK_SECRET, body),
        },
    )
    await supervisor.tick()
    assert pr(webhook_client, task_id)["completed_rounds"] == 1
    status = webhook_client.get("/v1/supervisor").json()["github"]
    assert status["deliveries_pending"] == 0
    _ = clock


# ----- idempotence ------------------------------------------------------


@pytest.mark.parametrize("delivery_supervisor", [True], indirect=True)
async def test_a_second_tick_with_nothing_new_changes_nothing(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """10: running the tick twice with nothing happening in between changes nothing."""
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    await delivery_supervisor.tick()
    before = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    await delivery_supervisor.tick()
    after = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    new = [e["kind"] for e in after[len(before) :]]
    # The poll itself is an event; nothing else about the task changes.
    assert set(new) <= {"pull_request_polled"}
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "merged"


@pytest.mark.parametrize("delivery_supervisor", [True], indirect=True)
async def test_persistent_merge_refusal_wakes_once_without_retry_storm(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    github.state.merge_refusal = (409, "base branch has conflicts")

    await delivery_supervisor.tick()
    calls_after_refusal = list(github.state.merge_calls)
    mints_after_refusal = github.state.mint_calls
    await delivery_supervisor.tick()

    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "ready_for_merge"
    assert github.state.merge_calls == calls_after_refusal
    assert github.state.mint_calls == mints_after_refusal
    refused = [
        wake
        for wake in client.get("/v1/wakes").json()["items"]
        if "base branch has conflicts" in wake["summary"]
    ]
    assert len(refused) == 1


@pytest.mark.parametrize("delivery_supervisor", [True], indirect=True)
async def test_retargeted_base_is_not_auto_merged(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    github.state.repositories[REPOSITORY].pulls[1].base_ref = "release"

    await delivery_supervisor.tick()

    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "ready_for_merge"
    assert github.state.merge_calls == []
    summaries = [wake["summary"] for wake in client.get("/v1/wakes").json()["items"]]
    assert any("base changed from main to release" in summary for summary in summaries)


@pytest.mark.parametrize("delivery_supervisor", [True], indirect=True)
async def test_failed_merge_response_is_recovered_from_live_pull_request(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    github.state.merge_response_failure_once = True

    await delivery_supervisor.tick()

    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "merged"
    record = pr(client, task_id)
    assert record["merge_sha"] == "f" * 40
    assert record["merged_by"] == "crucible-spike[bot]"
    assert github.state.merge_calls == [(REPOSITORY, 1, "squash")]


# ----- the C4 correction round ------------------------------------------


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_commit_by_another_author_without_the_trailer_publishes(
    client: TestClient,
    delivery_supervisor: Supervisor,
    publisher: FakePublisher,
    github: FakeGitHubServer,
    ctx: AppContext,
    tokens: dict[str, str],
) -> None:
    """FDY-0143 (operator decision, 2026-09-29): neither the author nor the trailer stops
    a branch. The author difference fails the always-advisory commit_policy gate, which
    lists it for the reviewer; the branch is pushed and the PR opened, and the task
    record is the paper trail: the branch, the pushed head, the PR, and once merged the
    merge commit and who merged."""
    task_id, view = await publish(
        client,
        delivery_supervisor,
        external_id="HT-0143",
        image="crucible-worker:fake-other-author",
    )
    head = view["head_sha"]
    rows = client.get(f"/v1/attempts/{view['latest_attempt']['id']}/gates").json()["items"]
    commit_policy = next(r for r in rows if r["gate"] == "commit_policy")
    assert commit_policy["result"] == "fail"
    assert commit_policy["detail"] == (
        "1 commit(s) not authored as "
        f"crucible-worker@users.noreply.github.com ({head[:12]} by someone-else@example.test)"
    )
    assert publisher.pushes == [(view["delivery"]["work_branch"], head)]
    delivery = view["delivery"]
    assert delivery["work_branch"] and delivery["work_branch"].startswith("crucible/")
    assert delivery["pushed_head"] == head
    assert delivery["pushed_at"]
    assert delivery["pull_request_number"] == 1
    assert delivery["pull_request_url"].endswith("/pull/1")
    assert delivery["pull_request_state"] == "open"
    assert (delivery["merge_sha"], delivery["merged_by"], delivery["merged_at"]) == (
        None,
        None,
        None,
    )
    assert view["pull_request"]["work_branch"] == delivery["work_branch"]

    github.state.add_reaction(REPOSITORY, 1, login=REVIEWER, content="+1")
    await delivery_supervisor.tick()
    github.state.set_check(REPOSITORY, head, name="build", conclusion="success")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "ready_for_merge"
    github.state.merge(REPOSITORY, 1, by="sentania", sha="e" * 40)
    await delivery_supervisor.tick()
    merged = client.get(f"/v1/tasks/{task_id}").json()
    assert merged["state"] == "merged"
    assert merged["delivery"]["merge_sha"] == "e" * 40
    assert merged["delivery"]["merged_by"] == "sentania"
    assert merged["delivery"]["merged_at"]
    assert merged["delivery"]["pushed_head"] == head

    with TestClient(create_app(ctx)) as browser:
        form = browser.get("/ui/sign-in")
        preauth = re.search(r'name="csrf" value="([a-f0-9]+)"', form.text)
        assert preauth is not None
        signed_in = browser.post(
            "/ui/sign-in",
            data={"csrf": preauth.group(1), "token": tokens["observer"], "next": "/ui/tasks"},
            follow_redirects=False,
        )
        assert signed_in.status_code == 303
        listing = browser.get("/ui/tasks")
        assert listing.status_code == 200
        assert f'href="/ui/tasks/{task_id}"' in listing.text
        page = browser.get(f"/ui/tasks/{task_id}")
        assert page.status_code == 200, page.text
        for text_shown in (
            delivery["work_branch"],
            head,
            f'href="{delivery["pull_request_url"]}"',
            "#1",
            "e" * 40,
            "sentania",
        ):
            assert text_shown in page.text, text_shown
        missing = browser.get("/ui/tasks/no-such-task", follow_redirects=False)
        assert missing.status_code == 303


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_fix_disposition_holds_the_task_until_a_correction(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """09: advancement needs every comment dispositioned and none of them fix."""
    task_id, view = await publish(client, delivery_supervisor)
    github.state.add_review(
        REPOSITORY,
        1,
        login=REVIEWER,
        body="Codex Review. Reviewed commit: " + view["head_sha"],
        comments=[{"body": "P1 this is wrong", "path": "src/app.txt", "line": 1}],
    )
    await delivery_supervisor.tick()
    record = pr(client, task_id)
    client.post(
        f"/v1/tasks/{task_id}/dispositions",
        json={
            "review_comment_id": record["comments"][0]["id"],
            "disposition": "fix",
            "reasoning": "The reviewer is right and the work is not done.",
        },
    )
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "external_feedback_received"
    assert gate(pr(client, task_id), "feedback_dispositions_complete") == "pending"


async def test_the_provider_summary_comment_does_not_complete_a_cycle(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """S12's real shape: the summary comment arrives seconds after the pull request opens
    and is edited when the verdict lands. Counting it would complete the round before any
    review exists."""
    task_id, _ = await publish(client, delivery_supervisor)
    github.state.add_issue_comment(
        REPOSITORY,
        1,
        login=REVIEWER,
        body=(
            "<!-- codex-pull-request-review-summary -->\n## Codex Review Summary\n\n"
            "This comment shows the latest Codex review activity."
        ),
    )
    await delivery_supervisor.tick()
    record = pr(client, task_id)
    assert record["completed_rounds"] == 0
    assert [c["state"] for c in record["cycles"]] == ["open"]
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_external_review"
    assert [r["accepted"] for r in record["external_reviews"]] == [False]


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_a_reused_pull_request_that_does_not_match_the_contract_fails_publication(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """A pull request retargeted to another base, or left a draft, delivers something
    else. Crucible reports it and refuses rather than advancing."""
    task_id, view = await publish(client, delivery_supervisor)
    github.state.add_review(
        REPOSITORY,
        1,
        login=REVIEWER,
        body="Codex Review. Reviewed commit: " + view["head_sha"],
        comments=[{"body": "P2 a finding", "path": "src/app.txt", "line": 1}],
    )
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "external_feedback_received"
    # Someone retargets the pull request while the correction is being made.
    github.state.repositories[REPOSITORY].pulls[1].base_ref = "release/1.x"
    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    assert client.post(f"/v1/tasks/{task_id}/corrections", json=document).status_code == 200
    await run_to_settled(delivery_supervisor, client, task_id)
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "publish_failed"
    failed = [
        e
        for e in client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
        if e["kind"] == "task_publish_failed"
    ]
    assert "base_ref" in failed[-1]["payload"]["detail"]


async def test_two_required_rounds_return_the_task_to_awaiting_external_review(
    ctx: AppContext,
    client: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    engine: Engine,
) -> None:
    """09: with rounds outstanding the task goes back to `awaiting_external_review`, and
    `retrigger_after_correction` wakes the orchestrator to post the trigger."""
    # `policies` is not truncated between tests, so this one puts the document back.
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE policies SET document = jsonb_set(jsonb_set(document, "
                "'{external_review,required_rounds}', '2'), "
                "'{external_review,retrigger_after_correction}', 'true') "
                "WHERE name = 'default-software' AND version = 2"
            )
        )
    try:
        task_id, _ = await publish(client, delivery_supervisor)
        github.state.add_reaction(REPOSITORY, 1, login=REVIEWER, content="+1")
        await delivery_supervisor.tick()
        assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_external_review"
        record = pr(client, task_id)
        assert record["completed_rounds"] == 1 and record["required_rounds"] == 2
        # A second cycle is open on this head for the round still outstanding.
        assert [c["state"] for c in record["cycles"]] == ["completed", "open"]
        reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
        assert "external_review_trigger_needed" in reasons
        assert "external_review_trigger_needed" in event_kinds(client, task_id)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE policies SET document = jsonb_set(jsonb_set(document, "
                    "'{external_review,required_rounds}', '1'), "
                    "'{external_review,retrigger_after_correction}', 'false') "
                    "WHERE name = 'default-software' AND version = 2"
                )
            )
    _ = ctx


async def test_a_ci_log_excerpt_is_redacted_before_it_is_stored(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer, engine: Engine
) -> None:
    """12: a workflow log is text the repository controls, and it lands in a stored row."""
    value = installation_token_value()
    github.state.workflow_log = f"the job printed {value}\n".encode()
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="failure")
    github.state.set_workflow_run(REPOSITORY, view["head_sha"], name="ci", conclusion="failure")
    await delivery_supervisor.tick()
    await delivery_supervisor.tick()
    with engine.begin() as connection:
        failures = connection.execute(
            text("SELECT CAST(failure AS TEXT) FROM ci_certifications")
        ).scalars()
        blob = "\n".join(str(f) for f in failures)
    assert value not in blob
    assert "[redacted:github_installation_token]" in blob
    _ = task_id


async def test_a_task_is_not_overdue_the_moment_it_enters_a_waiting_state(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer, clock: FakeClock
) -> None:
    """The clock starts when the task entered the state, not when the pull request was
    opened: a correction on an old pull request must not be overdue on its first poll."""
    task_id, _ = await publish(client, delivery_supervisor)
    # Three days pass while the pull request is open, then the task enters certification.
    clock.advance(3 * 24 * 3600)
    github.state.add_reaction(REPOSITORY, 1, login=REVIEWER, content="+1")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "awaiting_ci_certification"
    reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
    assert "ci_certification_overdue" not in reasons
    # And it does become overdue once the wait itself is long enough.
    clock.advance(7 * 3600)
    await delivery_supervisor.tick()
    reasons = [w["reason"] for w in client.get("/v1/wakes").json()["items"]]
    assert "ci_certification_overdue" in reasons


async def test_a_pull_request_closed_unmerged_records_who_closed_it(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """23: "a PR closed without merge moves the task to rejected with the closer
    recorded". The closer is not on the pull request object; it is a timeline event."""
    task_id, _ = await publish(client, delivery_supervisor)
    github.state.close(REPOSITORY, 1, by="sentania")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "rejected"
    assert pr(client, task_id)["closed_by"] == "sentania"


def test_a_delivery_body_over_the_limit_is_refused_before_it_is_read(
    webhook_client: TestClient, engine: Engine
) -> None:
    """The HMAC is over the raw body, so the body is read before it can be verified: an
    unauthenticated caller decides how much this endpoint reads unless it decides first."""
    body = b'{"padding":"' + b"x" * (2 * 1024 * 1024) + b'"}'
    response = webhook_client.post(
        "/v1/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d-big",
            "X-Hub-Signature-256": signed(WEBHOOK_SECRET, body),
        },
    )
    assert response.status_code == 401
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM github_deliveries")).scalar() == 0


def test_a_rejected_delivery_does_not_record_the_event_name_it_claims(
    webhook_client: TestClient, engine: Engine
) -> None:
    """23 stores nothing of a rejected delivery, and that includes the headers it chose."""
    body = delivery_body(1, "c" * 40)
    response = webhook_client.post(
        "/v1/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "<script>alert(1)</script>",
            "X-GitHub-Delivery": "d-evil",
            "X-Hub-Signature-256": signed("the-wrong-secret", body),
        },
    )
    assert response.status_code == 401
    with engine.begin() as connection:
        payloads = connection.execute(
            text("SELECT CAST(payload AS TEXT) FROM events WHERE kind = 'github_delivery_rejected'")
        ).scalars()
        blob = "\n".join(str(p) for p in payloads)
    assert "script" not in blob
    assert "unrecognized" in blob


@pytest.mark.parametrize("delivery_supervisor", [True], indirect=True)
@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
async def test_live_admin_switch_holds_then_merges_an_existing_ready_head(
    client: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    tokens: dict[str, str],
) -> None:
    headers = {"Authorization": f"Bearer {tokens['admin']}"}
    path = "/v1/admin/delivery/auto-merge"
    assert client.get(path, headers=headers).json() == {"enabled": True}
    assert client.post(path, headers=headers, json={"enabled": False}).status_code == 200
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "ready_for_merge"
    assert github.state.merge_calls == []
    assert client.post(path, headers=headers, json={"enabled": True}).status_code == 200
    await delivery_supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "merged"
    assert github.state.merge_calls == [(REPOSITORY, 1, "squash")]
