"""Credential administration on the Kubernetes provider (25, 26, ADR 0015, #92).

The admin API, the UI and the CLI's remote calls land on the same services; here they
run against a real database with the Kubernetes provider on the in-memory API. The
login is a Job and the credential is the harness Secret the service owns: an empty
namespace is logged into, finished, validated and probed through `/v1/admin`, the
Hermes key is set from the Routing page, and a login and an attempt of one harness are
kept apart (12). The kind tier drives the same flow on a real cluster."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from functools import partial
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.execution.k8sfake import FakeKubernetesApi, FakeLogin, FakeRegistry
from crucible.adapters.execution.kubernetes import (
    ANNOTATION_LOCK_EXPIRES,
    ANNOTATION_LOCK_HOLDER,
    KubernetesConfig,
    KubernetesProvider,
)
from crucible.adapters.harness.registry import default_registry
from crucible.application.admin.context import AdminContext
from crucible.application.admin.login import (
    LoginRegistry,
    _accept_login,
    _accept_while_locked,
    start_login,
)
from crucible.application.errors import ConflictError
from crucible.application.supervisor import Supervisor
from crucible.domain.lifecycle import AttemptState
from crucible.domain.secrets import scan_text
from tests.fixtures import promote_for_test
from tests.integration.conftest import put_seeded_policy_in_force
from tests.integration.test_admin import ui_sign_in
from tests.integration.test_harness_registry import _submit_pinned
from tests.wait import wait_until

pytestmark = pytest.mark.integration

WORKER = "ghcr.io/sentania-labs/crucible-worker:20260916-k8s-login"
LABELS = {
    "crucible.harnesses": "agy,claude_code,codex,hermes",
    "crucible.harness.agy.version": "1.2.8",
    "crucible.harness.claude_code.version": "2.1.280",
    "crucible.harness.codex.version": "0.156.0",
    "crucible.harness.hermes.version": "0.19.0",
}
HOSTS = {
    "auth.openai.com": ["162.159.140.246/32"],
    "api.openai.com": ["162.159.140.245/32"],
    "chatgpt.com": ["162.159.140.247/32"],
    "platform.claude.com": ["160.79.104.20/32"],
    "api.anthropic.com": ["160.79.104.10/32"],
}
CODEX_AUTH = json.dumps(
    {"tokens": {"access_token": "not-a-real-value"}, "last_refresh": "2026-09-24T00:00:00Z"}
).encode()


@pytest.fixture
def k8s_api() -> FakeKubernetesApi:
    return FakeKubernetesApi()


@pytest.fixture
def k8s_provider(k8s_api: FakeKubernetesApi) -> KubernetesProvider:
    registry = FakeRegistry(k8s_api)
    registry.register(WORKER, labels=LABELS)
    return KubernetesProvider(
        KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            extra_image_allowlist=("ghcr.io/sentania-labs/crucible-worker:*",),
        ),
        k8s_api,  # type: ignore[arg-type]
        registry,
        harnesses=default_registry(test_fixtures=True),
        resolver=lambda host: list(HOSTS.get(host, ["203.0.113.1/32"])),
    )


@pytest.fixture
def admin_ctx(
    ctx: AppContext, provider: FakeProvider, k8s_provider: KubernetesProvider
) -> AdminContext:
    """A Kubernetes deployment: the fake provider is always wired, Docker is not, so
    the harness credentials are the Secrets the service owns (ADR 0015). No credential
    directory is configured anywhere."""
    assert ctx.harnesses is not None
    admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        providers={"fake": provider, "kubernetes": k8s_provider},
        harnesses=ctx.harnesses,
        lease_ttl_seconds=30,
        login_timeout_seconds=10,
        probe_timeout_seconds=10,
    )
    ctx.admin = admin
    with ctx.uow_factory() as uow:
        promote_for_test(
            uow,
            digest="sha256:" + "d" * 64,
            reference=WORKER,
            harnesses={k.rsplit(".", 2)[-2]: v for k, v in LABELS.items() if "version" in k},
            at=ctx.clock.now(),
            by="tests",
            reason="the worker image the login and the probe run",
        )
        uow.commit()
    return admin


@pytest.fixture
def live(ctx: AppContext, provider: FakeProvider) -> Supervisor:
    supervisor = Supervisor(
        ctx.uow_factory,
        {"fake": provider},
        SystemClock(),
        holder="k8s-credentials",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=300,
        harnesses=ctx.harnesses,
    )
    asyncio.run(supervisor.tick())
    return supervisor


@pytest.fixture
def admin(
    ctx: AppContext, tokens: dict[str, str], admin_ctx: AdminContext, live: Supervisor
) -> Iterator[TestClient]:
    with TestClient(create_app(ctx), headers={"Authorization": f"Bearer {tokens['admin']}"}) as c:
        yield c


def poll(client: TestClient, harness: str, *states: str) -> dict[str, Any]:
    def wanted_state() -> dict[str, Any]:
        state: dict[str, Any] = client.get(f"/v1/admin/credentials/{harness}/login").json()
        if state["state"] in states:
            return state
        return {}

    return wait_until(
        wanted_state,
        timeout=8,
        describe=f"{harness} login to reach one of {states}",
    )


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_a_codex_login_fills_an_empty_namespace_and_the_probe_validates_it(
    admin: TestClient, k8s_api: FakeKubernetesApi
) -> None:
    before = admin.get("/v1/admin/credentials/codex").json()
    assert before["state"] == "absent"
    assert before["source"] == {
        "kind": "secret",
        "name": "crucible-harness-codex",
        "exists": False,
        "service_owned": False,
    }
    k8s_api.login = FakeLogin(files={"/home/worker/.codex/auth.json": CODEX_AUTH}, finish_after=3)
    started = admin.post("/v1/admin/credentials/codex/login", json={"reason": "onboarding"})
    assert started.status_code == 200, started.text
    assert started.json()["secret"] == "crucible-harness-codex"
    assert started.json()["retained_as"] is None
    state = poll(admin, "codex", "finished", "failed")
    assert state["state"] == "finished", state
    assert state["url"] == "https://example.invalid/device" and state["code"] == "C7AA-TEST"
    assert state["credential_written"] is True
    finished = admin.post("/v1/admin/credentials/codex/login/finish", json={"reason": "done"})
    assert finished.status_code == 200, finished.text
    assert finished.json()["shape"]["ok"] is True
    after = admin.get("/v1/admin/credentials/codex").json()
    assert after["state"] == "configured"
    assert after["source"]["exists"] and after["source"]["service_owned"]

    validated = admin.post("/v1/admin/credentials/codex/validate", json={"reason": "probe"})
    assert validated.status_code == 200, validated.text
    document = validated.json()
    assert document["probe"]["exit_class"] == "completed", document
    assert document["probe"]["image"] == WORKER
    assert document["probe"]["harness_version"] == "0.156.0"
    assert document["credential"]["state"] == "validated"
    assert scan_text(json.dumps(document)) is None
    # The probe left nothing but the harness Secret behind.
    for kind in ("jobs", "pods", "networkpolicies", "configmaps", "persistentvolumeclaims"):
        assert k8s_api.object_names(kind) == [], kind
    assert k8s_api.object_names("secrets") == ["crucible-harness-codex"]
    kinds = [
        row["kind"] for row in admin.get("/v1/admin/audit", params={"limit": 50}).json()["items"]
    ]
    for kind in (
        "credential_login_started",
        "credential_login_finished",
        "credential_validated",
        "credential_probed",
    ):
        assert kind in kinds, kind


def test_a_claude_login_takes_the_pasted_code_and_never_shows_the_token(
    admin: TestClient, k8s_api: FakeKubernetesApi
) -> None:
    k8s_api.login = FakeLogin(
        lines=["https://platform.claude.com/oauth/authorize?code=true"],
        prompt="Paste code here if prompted > ",
        after_code=["[pasted code]", "[captured to oauth-token]"],
        files={"/home/worker/.claude/oauth-token": b"not-a-real-value\n"},
    )
    admin.post("/v1/admin/credentials/claude_code/login", json={"reason": "onboarding"})
    waiting = poll(admin, "claude_code", "waiting_for_code", "failed")
    assert waiting["state"] == "waiting_for_code", waiting
    code = admin.post(
        "/v1/admin/credentials/claude_code/login/code",
        json={"code": "the-code#state", "reason": "complete onboarding"},
    )
    assert code.status_code == 200, code.text
    done = poll(admin, "claude_code", "finished", "failed")
    assert done["state"] == "finished", done
    assert done["token_written"] is True and done["credential_written"] is True
    assert "[captured to oauth-token]" in done["output_tail"]
    assert k8s_api.harness_secret("crucible-harness-claude-code") == {
        "oauth-token": b"not-a-real-value\n"
    }
    # A second login does not replace a credential that passes the shape check unless
    # it is told to, and nothing is touched when it refuses.
    again = admin.post("/v1/admin/credentials/claude_code/login", json={"reason": "again"})
    assert again.status_code == 409 and "Pass replace" in again.json()["detail"]
    assert len([r for r in k8s_api.created if r["kind"] == "jobs"]) == 1


def test_rotate_and_remove_say_the_credential_is_a_secret(admin: TestClient) -> None:
    for verb, body in (("rotate", {"new_path": "/tmp/x"}), ("remove", {})):
        response = admin.post(f"/v1/admin/credentials/codex/{verb}", json={"reason": "r", **body})
        assert response.status_code == 409, response.text
        assert "crucible-harness-codex" in response.json()["detail"]


def test_the_credentials_page_offers_only_what_applies_to_a_secret(
    admin: TestClient, ctx: AppContext, tokens: dict[str, str]
) -> None:
    """crucible#125: rotate and remove move and shred directories, which a Secret-held
    credential has none of, so the page does not offer them or their prepared directory;
    and Hermes, which takes a key, has no login page link."""
    with TestClient(create_app(ctx)) as browser:
        ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/credentials").text
    assert "Rotate from prepared server directory" not in page
    assert "Prepared directory" not in page
    assert 'value="remove"' not in page
    # Nothing is stored yet, so each row offers only the way to set its credential up.
    assert 'value="validate"' not in page
    assert "/ui/credentials/hermes/login" not in page
    assert "/ui/credentials/codex/login" in page


def test_the_hermes_key_is_set_from_the_gateway_page_and_never_shown(
    admin: TestClient, ctx: AppContext, tokens: dict[str, str], k8s_api: FakeKubernetesApi
) -> None:
    api_key = "vk_" + "q" * 40
    put_seeded_policy_in_force(ctx)
    assert admin.get("/v1/admin/credentials/hermes").json()["key_set"] is False
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/gateway")
        assert page.status_code == 200
        assert "Set the gateway URL and key" in page.text
        saved = browser.post(
            "/ui/actions/gateway-save",
            data={
                "csrf": csrf,
                "endpoint_url": "http://127.0.0.1:9/v1",
                "api_key": api_key,
                "reason": "hermes key from the gateway page",
                "return_to": "/ui/gateway",
            },
            follow_redirects=False,
        )
        assert saved.status_code in (302, 303), saved.text
        after = browser.get("/ui/gateway").text
        assert api_key not in after
    assert k8s_api.harness_secret("crucible-harness-hermes") == {
        "api-key": api_key.encode() + b"\n"
    }
    labels = k8s_api.objects[("secrets", "crucible-harness-hermes")].body["metadata"]["labels"]
    assert labels[k8sspec.LABEL_MANAGED_BY] == "crucible"
    view = admin.get("/v1/admin/credentials/hermes")
    assert view.json()["key_set"] is True and api_key not in view.text
    audit = admin.get("/v1/admin/audit", params={"limit": 50}).text
    assert "credential_set" in audit and api_key not in audit
    # The API form writes the same Secret and returns no value either.
    replaced = admin.post(
        "/v1/admin/credentials/hermes/set",
        json={"reason": "rotate the key", "api_key": api_key[:-1] + "r"},
    )
    assert replaced.status_code == 200 and api_key[:-1] not in replaced.text


async def test_a_login_refuses_while_an_attempt_holds_the_credential(
    admin: TestClient,
    admin_ctx: AdminContext,
    live: Supervisor,
    tokens: dict[str, str],
    k8s_api: FakeKubernetesApi,
) -> None:
    """12: a login replaces the credential, so it waits for an attempt that holds a
    copy of it; once that attempt has been collected the login may start."""
    task_id = _submit_pinned(admin, tokens, "crucible-worker:fake-hang", "EX-HOLDS")
    await live.tick()
    await live.tick()
    view = admin.get(f"/v1/tasks/{task_id}").json()
    assert view["latest_attempt"]["state"] == "running", view
    refused = admin.post("/v1/admin/credentials/codex/login", json={"reason": "onboarding"})
    assert refused.status_code == 409, refused.text
    assert "holds its credential" in refused.json()["detail"]
    assert view["latest_attempt"]["id"] in refused.json()["detail"]
    # A login already running when an attempt comes to hold the credential declines to
    # write at the end; this is the check it makes at that moment.
    problems = _accept_login(admin_ctx, "codex", {"auth.json": CODEX_AUTH})
    assert any("came to hold the codex credential" in p for p in problems), problems
    assert not k8s_api.secret_exists("crucible-harness-codex")
    with live._fenced() as uow:
        attempt = uow.attempts.get(view["latest_attempt"]["id"], for_update=True)
        assert attempt is not None
        attempt.state = AttemptState.COLLECTED
        uow.attempts.save(attempt)
        uow.commit()
    k8s_api.login = FakeLogin(files={"/home/worker/.codex/auth.json": CODEX_AUTH}, finish_after=5)
    started = admin.post("/v1/admin/credentials/codex/login", json={"reason": "onboarding"})
    assert started.status_code == 200, started.text
    assert poll(admin, "codex", "finished", "failed")["state"] == "finished"


async def test_a_launch_waits_while_a_login_for_its_harness_runs(
    ctx: AppContext,
    client: TestClient,
    provider: FakeProvider,
    k8s_provider: KubernetesProvider,
    k8s_api: FakeKubernetesApi,
    tokens: dict[str, str],
) -> None:
    """12: the login Job is the lock the supervisor sees. A codex launch is deferred,
    not failed, while one exists, and goes ahead once it is gone."""
    supervisor = Supervisor(
        ctx.uow_factory,
        {"fake": provider, "kubernetes": k8s_provider},
        ctx.clock,
        holder="k8s-login-defer",
        artifact_store=ctx.artifact_store,
        harnesses=ctx.harnesses,
        lease_ttl_seconds=30,
    )
    k8s_api.login = FakeLogin(never_exits=True)
    k8s_api.create(
        "jobs",
        {
            "metadata": {
                "name": "login-codex-login0000000000000000000",
                "labels": {
                    k8sspec.LABEL_ROLE: k8sspec.ROLE_LOGIN,
                    k8sspec.LABEL_HARNESS: "codex",
                    k8sspec.LABEL_LOGIN: "login0000000000000000000",
                },
            },
            "spec": {"template": {"metadata": {"labels": {}}, "spec": {}}},
        },
    )
    task_id = _submit_pinned(client, tokens, "crucible-worker:fake-succeed", "EX-WAITS")
    await supervisor.tick()
    await supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "scheduled"
    deferred = [
        e
        for e in client.get(f"/v1/tasks/{task_id}/events").json()["items"]
        if e["kind"] == "harness_launch_deferred"
    ]
    assert deferred and "a login for codex is running" in deferred[0]["payload"]["detail"]
    k8s_api.delete("jobs", "login-codex-login0000000000000000000")
    await supervisor.tick()
    await supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] != "scheduled"


# ----- one login per harness across every api replica (25, 26) -------------------


def lock_names(api: FakeKubernetesApi) -> list[str]:
    return [n for n in api.object_names("configmaps") if n.startswith("login-lock-")]


def test_a_second_replica_is_refused_while_the_lock_is_held_and_starts_after(
    admin: TestClient, ctx: AppContext, admin_ctx: AdminContext, k8s_api: FakeKubernetesApi
) -> None:
    """Two api replicas each keep their own logins in memory; the lock ConfigMap is what
    both see. The second start is refused naming the holder, and no second Job exists."""
    k8s_api.login = FakeLogin(never_exits=True)
    started = admin.post("/v1/admin/credentials/codex/login", json={"reason": "replica one"})
    assert started.status_code == 200, started.text
    poll(admin, "codex", "waiting_for_operator")
    assert lock_names(k8s_api) == ["login-lock-codex"]
    other = LoginRegistry()  # the second replica's memory: it knows of no login
    with ctx.uow_factory() as uow, pytest.raises(ConflictError) as refused:
        start_login(
            admin_ctx,
            uow,
            other,
            principal="second-principal",
            harness="codex",
            reason="replica two",
        )
    assert "already in progress" in str(refused.value.detail)
    assert "admin-principal on " in str(refused.value.detail)
    assert other.get("codex") is None
    assert len([r for r in k8s_api.created if r["kind"] == "jobs"]) == 1

    cancelled = admin.post("/v1/admin/credentials/codex/login/cancel", json={"reason": "stop"})
    assert cancelled.status_code == 200, cancelled.text
    assert poll(admin, "codex", "failed")["error"] == "login cancelled"
    assert lock_names(k8s_api) == []
    k8s_api.login = FakeLogin(files={"/home/worker/.codex/auth.json": CODEX_AUTH})
    with ctx.uow_factory() as uow:
        start_login(
            admin_ctx, uow, other, principal="second-principal", harness="codex", reason="now"
        )
        uow.commit()

    def finished_session() -> Any:
        session = other.get("codex")
        if session is not None and session.state in ("finished", "failed"):
            return session
        return None

    session = wait_until(
        finished_session,
        timeout=8,
        describe="the second process's codex login to finish",
    )
    assert session is not None and session.state == "finished", session
    assert k8s_api.harness_secret("crucible-harness-codex") == {"auth.json": CODEX_AUTH}
    assert lock_names(k8s_api) == []


def test_the_lock_of_an_api_that_died_is_taken_over_once_it_expires(
    admin: TestClient, k8s_api: FakeKubernetesApi, k8s_provider: KubernetesProvider
) -> None:
    """A lock left by an api that died mid-login holds until its expiry, then the next
    login deletes exactly that lock and takes its own."""
    k8s_api.create(
        "configmaps",
        k8sspec.config_map(
            name="login-lock-codex",
            namespace=k8s_api.namespace,
            object_labels={
                k8sspec.LABEL_ROLE: k8sspec.ROLE_LOGIN_LOCK,
                k8sspec.LABEL_HARNESS: "codex",
            },
            data={},
            annotations={
                ANNOTATION_LOCK_HOLDER: "someone on crucible-api-dead",
                ANNOTATION_LOCK_EXPIRES: "2099-01-01T00:00:00Z",
            },
        ),
    )
    held = admin.post("/v1/admin/credentials/codex/login", json={"reason": "onboarding"})
    assert held.status_code == 409, held.text
    assert "held by someone on crucible-api-dead" in held.json()["detail"]
    assert not [r for r in k8s_api.created if r["kind"] == "jobs"]

    stale = k8s_api.objects[("configmaps", "login-lock-codex")].body
    stale["metadata"]["annotations"][ANNOTATION_LOCK_EXPIRES] = "2026-01-01T00:00:00Z"
    stale_uid = stale["metadata"]["uid"]
    k8s_api.login = FakeLogin(never_exits=True)
    started = admin.post("/v1/admin/credentials/codex/login", json={"reason": "onboarding"})
    assert started.status_code == 200, started.text
    poll(admin, "codex", "waiting_for_operator")
    lock = k8s_api.objects[("configmaps", "login-lock-codex")].body["metadata"]
    assert lock["uid"] != stale_uid
    assert lock["annotations"][ANNOTATION_LOCK_HOLDER].startswith("admin-principal on ")
    admin.post("/v1/admin/credentials/codex/login/cancel", json={"reason": "stop"})
    poll(admin, "codex", "failed")
    assert lock_names(k8s_api) == []


@pytest.mark.parametrize("ending", ["success", "failure", "cancel", "timeout"])
def test_the_lock_is_released_however_the_login_ends(
    ending: str, admin: TestClient, admin_ctx: AdminContext, k8s_api: FakeKubernetesApi
) -> None:
    seen: list[list[str]] = []
    if ending == "success":
        k8s_api.login = FakeLogin(
            files={"/home/worker/.codex/auth.json": CODEX_AUTH}, finish_after=5
        )
    elif ending == "failure":
        k8s_api.login = FakeLogin(exit_code=2, finish_after=5)
    else:
        k8s_api.login = FakeLogin(never_exits=True)
    if ending == "timeout":
        admin_ctx.login_timeout_seconds = 1
    started = admin.post("/v1/admin/credentials/codex/login", json={"reason": ending})
    assert started.status_code == 200, started.text
    seen.append(lock_names(k8s_api))
    if ending == "cancel":
        poll(admin, "codex", "waiting_for_operator")
        admin.post("/v1/admin/credentials/codex/login/cancel", json={"reason": "stop"})
    state = poll(admin, "codex", "finished", "failed")
    expected = {
        "success": ("finished", None),
        "failure": ("failed", "the login's auth files were not stored: auth.json: missing"),
        "cancel": ("failed", "login cancelled"),
        "timeout": ("failed", "login timed out"),
    }[ending]
    assert (state["state"], state["error"]) == expected, state
    assert seen == [["login-lock-codex"]]
    assert lock_names(k8s_api) == []
    assert not [n for n in k8s_api.object_names("jobs") if n.startswith("login-")]


def test_a_cancelled_login_reads_failed_only_once_its_lock_is_gone(
    admin: TestClient,
    k8s_api: FakeKubernetesApi,
    k8s_provider: KubernetesProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator who retries the moment a cancel reads `failed` is not refused by
    the cancelled login's own lock. A slow release widens the window that once let the
    session read `failed` while the lock ConfigMap still stood."""
    release = k8s_provider.release_login_lock

    def slow_release(lock: Any) -> None:
        threading.Event().wait(0.5)
        release(lock)

    monkeypatch.setattr(k8s_provider, "release_login_lock", slow_release)
    k8s_api.login = FakeLogin(never_exits=True)
    started = admin.post("/v1/admin/credentials/codex/login", json={"reason": "onboarding"})
    assert started.status_code == 200, started.text
    poll(admin, "codex", "waiting_for_operator")
    admin.post("/v1/admin/credentials/codex/login/cancel", json={"reason": "stop"})
    assert poll(admin, "codex", "finished", "failed")["error"] == "login cancelled"
    assert lock_names(k8s_api) == []
    again = admin.post("/v1/admin/credentials/codex/login", json={"reason": "retry"})
    assert again.status_code == 200, again.text
    admin.post("/v1/admin/credentials/codex/login/cancel", json={"reason": "stop"})
    poll(admin, "codex", "failed")
    assert lock_names(k8s_api) == []


def held_release(
    k8s_provider: KubernetesProvider, monkeypatch: pytest.MonkeyPatch
) -> threading.Event:
    """Hold the login's lock release until the returned event is set, so the window
    between a login's decided outcome and its terminal state stays open for the test."""
    release = k8s_provider.release_login_lock
    go = threading.Event()

    def held(lock: Any) -> None:
        go.wait(10)
        release(lock)

    monkeypatch.setattr(k8s_provider, "release_login_lock", held)
    return go


def audit_count(client: TestClient, kind: str) -> int:
    items = client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
    return sum(1 for row in items if row["kind"] == kind)


def test_a_login_whose_cli_exited_refuses_a_cancel_while_it_cleans_up(
    admin: TestClient,
    k8s_api: FakeKubernetesApi,
    k8s_provider: KubernetesProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the CLI has exited the outcome is decided: a cancel sent while the Job and
    the lock are cleaned up is refused, not audited as accepted and then overwritten,
    and the session reads `finished` only once the lock is gone."""
    go = held_release(k8s_provider, monkeypatch)
    k8s_api.login = FakeLogin(files={"/home/worker/.codex/auth.json": CODEX_AUTH})
    admin.post("/v1/admin/credentials/codex/login", json={"reason": "onboarding"})
    poll(admin, "codex", "finishing")
    cancel = admin.post("/v1/admin/credentials/codex/login/cancel", json={"reason": "stop"})
    assert cancel.status_code == 409, cancel.text
    assert "nothing left to cancel" in cancel.json()["detail"]
    retry = admin.post("/v1/admin/credentials/codex/login", json={"reason": "retry"})
    assert retry.status_code == 409 and "cleaning up" in retry.json()["detail"]
    state = admin.get("/v1/admin/credentials/codex/login").json()
    assert state["state"] == "finishing" and state["cancel_requested"] is False
    assert lock_names(k8s_api) == ["login-lock-codex"]
    go.set()
    assert poll(admin, "codex", "finished", "failed")["state"] == "finished"
    assert lock_names(k8s_api) == []
    assert audit_count(admin, "credential_login_cancelled") == 0


def test_a_cancelled_login_refuses_a_code_and_a_second_cancel_while_it_cleans_up(
    admin: TestClient,
    k8s_api: FakeKubernetesApi,
    k8s_provider: KubernetesProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login cancelled at its code prompt is past taking a code: the code and a second
    cancel are refused while it cleans up, neither is audited as accepted, and it reads
    `failed` only once the lock is gone."""
    go = held_release(k8s_provider, monkeypatch)
    k8s_api.login = FakeLogin(
        lines=["https://platform.claude.com/oauth/authorize?code=true"],
        prompt="Paste code here if prompted > ",
    )
    admin.post("/v1/admin/credentials/claude_code/login", json={"reason": "onboarding"})
    poll(admin, "claude_code", "waiting_for_code")
    first = admin.post("/v1/admin/credentials/claude_code/login/cancel", json={"reason": "stop"})
    assert first.status_code == 200, first.text
    poll(admin, "claude_code", "finishing")
    code = admin.post(
        "/v1/admin/credentials/claude_code/login/code",
        json={"code": "the-code#state", "reason": "complete onboarding"},
    )
    assert code.status_code == 409, code.text
    assert "no longer takes a code" in code.json()["detail"]
    again = admin.post("/v1/admin/credentials/claude_code/login/cancel", json={"reason": "again"})
    assert again.status_code == 409, again.text
    assert admin.get("/v1/admin/credentials/claude_code/login").json()["state"] == "finishing"
    assert lock_names(k8s_api) == ["login-lock-claude-code"]
    go.set()
    done = poll(admin, "claude_code", "finished", "failed")
    assert done["state"] == "failed" and done["error"] == "login cancelled"
    assert lock_names(k8s_api) == []
    assert audit_count(admin, "credential_login_cancelled") == 1
    assert audit_count(admin, "credential_login_code_submitted") == 0


def test_a_login_that_lost_its_lock_does_not_store(
    admin_ctx: AdminContext, k8s_api: FakeKubernetesApi, k8s_provider: KubernetesProvider
) -> None:
    """A login that outlived its lock may have been overtaken by another replica's; the
    store check at the moment of the write refuses it."""
    lock = k8s_provider.acquire_login_lock("codex", holder="replica one", timeout=10)
    accept = partial(
        _accept_while_locked, k8s_provider, lock, partial(_accept_login, admin_ctx, "codex")
    )
    assert accept({"auth.json": CODEX_AUTH}) == []
    k8s_api.delete("configmaps", "login-lock-codex")
    taken = k8s_provider.acquire_login_lock("codex", holder="replica two", timeout=10)
    assert taken.uid != lock.uid
    problems = accept({"auth.json": CODEX_AUTH})
    assert any("lock expired and another login took it over" in p for p in problems), problems
    # Its release leaves the other replica's lock alone.
    k8s_provider.release_login_lock(lock)
    assert lock_names(k8s_api) == ["login-lock-codex"]
    k8s_provider.release_login_lock(taken)
    assert lock_names(k8s_api) == []


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_a_probe_the_job_deadline_ended_is_recorded_as_a_timeout(
    admin: TestClient, k8s_api: FakeKubernetesApi
) -> None:
    """A hanging probe harness is ended by the worker Job's own deadline before the
    provider's longer wait; validation records a timeout, not a crash."""
    k8s_api.put_harness_secret("crucible-harness-codex", {"auth.json": CODEX_AUTH})
    k8s_api.script_all("hang")
    k8s_api.job_deadline_fires = True
    validated = admin.post("/v1/admin/credentials/codex/validate", json={"reason": "probe"})
    assert validated.status_code == 200, validated.text
    probe = validated.json()["probe"]
    assert probe["exit_class"] == "timeout", probe
    assert "the Job's deadline ended it" in probe["detail"]


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_a_slow_secret_read_does_not_hold_the_status_handler(
    ctx: AppContext,
    admin_ctx: AdminContext,
    k8s_provider: KubernetesProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status reads each harness Secret once, on a worker thread, with a bounded wait:
    an API server that does not answer turns into an unreadable row, and the event loop
    keeps serving everything else meanwhile."""
    from crucible.application.admin import credentials as credentials_admin  # noqa: PLC0415
    from crucible.application.admin import status as status_admin  # noqa: PLC0415

    release = threading.Event()
    reads: list[str] = []
    real = k8s_provider.read_credential_secret

    def stalled(harness: str) -> Any:
        reads.append(harness)
        release.wait(30)
        return real(harness)

    monkeypatch.setattr(k8s_provider, "read_credential_secret", stalled)
    monkeypatch.setattr(credentials_admin, "SECRET_READ_TIMEOUT_SECONDS", 0.5)

    async def scenario() -> tuple[dict[str, Any], float, int]:
        ticks = 0

        async def other_requests() -> None:
            nonlocal ticks
            while True:
                await asyncio.to_thread(threading.Event().wait, 0.02)
                ticks += 1

        running = asyncio.create_task(other_requests())
        started = time.monotonic()
        try:
            with ctx.uow_factory() as uow:
                document = await status_admin.status(admin_ctx, uow)
        finally:
            elapsed = time.monotonic() - started
            running.cancel()
            release.set()
        return document, elapsed, ticks

    document, elapsed, ticks = asyncio.run(scenario())

    held = [
        name
        for name in admin_ctx.harnesses.names()
        if (adapter := admin_ctx.harnesses.get(name)) and adapter.credential_spec() is not None
    ]
    assert held and sorted(reads) == sorted(held)  # one read per harness, readiness included
    assert elapsed < 3, elapsed  # the reads waited together, not one after another
    assert ticks >= 10, ticks  # the loop served other work while the reads were stalled
    for item in document["harnesses"]:
        if item["name"] in held:
            assert item["credential"]["state"] == "unreadable"
            assert "did not answer within 0.5 seconds" in item["credential"]["detail"]
    # The to-do list reuses those reads: it names the unreadable credential too.
    listed = [h for h in document["readiness"]["harnesses"] if h["state"] != "off"]
    assert listed
    for item in listed:
        assert "credential_unreadable" in [s["code"] for s in item["steps"]], item
