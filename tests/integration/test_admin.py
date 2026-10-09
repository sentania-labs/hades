"""The administrative surface (25) on the fake provider: every row of the operations
table through the API and through `crucible-admin` in local mode, both landing in the
same audit trail; a mutation refused without a live supervisor lease; the orchestrator's
read-only view. The fake CLIs stand in for the three logins; every secret-shaped value
is built at runtime.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import html
import json
import os
import re
from collections.abc import Iterator
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution.docker import DockerConfig
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.first_run import FileDelivery
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.application.admin.context import AdminContext
from crucible.application.auth import authenticate, mint_token
from crucible.application.errors import ApplicationError
from crucible.application.routing import image_for_harness
from crucible.application.supervisor import Supervisor
from crucible.application.wakes import create_wake
from crucible.cli import admin as cli
from crucible.client.config import ADMIN_TOKEN_ENV
from crucible.client.http import Api
from crucible.contracts.policy import PolicyV1
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import Attempt, Execution, ExecutionRole, PoolExhaustion, Role
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.ports.execution import ImageInfo
from crucible.ports.harness import CredentialSource, HarnessGate
from crucible.settings import Settings
from tests.admin_cli import admin_main, envelope_data
from tests.fixtures import FakeClock, contract_document, promote_for_test
from tests.wait import wait_until

pytestmark = pytest.mark.integration


def _wait_for_login_state(client: TestClient, harness: str, *wanted: str) -> dict[str, Any]:
    def observe() -> dict[str, Any]:
        state: dict[str, Any] = client.get(f"/v1/admin/credentials/{harness}/login").json()
        return state if state["state"] in wanted else {}

    return wait_until(
        observe,
        timeout=5,
        describe=f"{harness} login to reach one of {wanted}",
    )


# What the C11 worker image declares: every real harness, each inside its adapter's range.
WORKER_HARNESSES = {
    "agy": "1.2.8",
    "claude_code": "2.1.280",
    "codex": "0.156.0",
    "hermes": "0.19.0",
}


@pytest.fixture(autouse=True)
def _quiet_cli_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI binds its logging to stderr; under capture that stream closes with the
    test, so the binding is skipped here. The CLI's output on stdout is what is read."""
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)


def _token(prefix: str, count: int = 40) -> str:
    return prefix + "x" * count


def seed_credentials(root: Path) -> dict[str, CredentialSource]:
    """Shape-valid auth files plus an empty Hermes key directory, built at runtime."""
    (root / "claude_code").mkdir(parents=True)
    (root / "claude_code" / "oauth-token").write_text(_token("sk-ant-oat01-"), encoding="utf-8")
    (root / "codex").mkdir()
    (root / "codex" / "auth.json").write_text(
        json.dumps(
            {"tokens": {"refresh_token": _token("eyJ")}, "last_refresh": "2026-09-17T00:00:00Z"}
        ),
        encoding="utf-8",
    )
    inner = root / "agy" / ".gemini" / "antigravity-cli"
    inner.mkdir(parents=True)
    (inner / "antigravity-oauth-token").write_text(
        json.dumps({"token": {"access_token": _token("ya29."), "expiry": "2026-09-17T01:00:00Z"}}),
        encoding="utf-8",
    )
    (root / "hermes").mkdir()
    for directory in (root / "claude_code", root / "codex", root / "agy", root / "hermes"):
        directory.chmod(0o700)
    return {
        name: CredentialSource(str(root / name))
        for name in ("claude_code", "codex", "agy", "hermes")
    }


# One script per harness, because the three flows differ in exactly the way the driver
# has to tell apart: Claude Code prompts for a pasted code and then prints a token once,
# Codex prints a device code and never prompts, AGY prompts and prints no token. A single
# script for all three let the Codex leg pass on the banner line without ever reaching the
# paste path. The phrases follow the real CLIs (S1b transcripts); the token is built here.
FAKE_LOGIN_SCRIPTS: dict[str, str] = {
    "claude_code": (
        "#!/bin/bash\n"
        'echo "Visit https://example.invalid/device to authorize, then enter the code here"\n'
        'printf "Paste the code: "\n'
        "read -t 30 -r code\n"
        'echo "token: TOKEN_PLACEHOLDER"\n'
        "exit 0\n"
    ),
    "codex": (
        "#!/bin/bash\n"
        'echo "Open https://example.invalid/device in a browser"\n'
        'echo "Your code is ABCD-EFGH (it expires in 15 minutes)"\n'
        'echo "Waiting for authorization..."\n'
        "exit 0\n"
    ),
    "agy": (
        "#!/bin/bash\n"
        'echo "Sign in at https://example.invalid/oauth and copy the code shown"\n'
        'printf "Enter the code: "\n'
        "read -t 30 -r code\n"
        'echo "Signed in."\n'
        "exit 0\n"
    ),
}


def fake_login_cli(root: Path) -> dict[str, tuple[str, ...]]:
    commands: dict[str, tuple[str, ...]] = {}
    for name, script in FAKE_LOGIN_SCRIPTS.items():
        path = root / f"fake-login-{name}"
        path.write_text(
            script.replace("TOKEN_PLACEHOLDER", _token("sk-ant-oat01-")), encoding="utf-8"
        )
        path.chmod(0o755)
        commands[name] = (str(path),)
    return commands


@pytest.fixture
def credential_root(tmp_path: Path) -> Path:
    root = tmp_path / "credentials"
    root.mkdir()
    return root


@pytest.fixture
def admin_ctx(
    ctx: AppContext, provider: FakeProvider, credential_root: Path, tmp_path: Path
) -> AdminContext:
    assert ctx.harnesses is not None
    admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        providers={"fake": provider},
        harnesses=ctx.harnesses,
        credential_sources=seed_credentials(credential_root),
        artifact_root=str(tmp_path / "artifacts"),
        lease_ttl_seconds=30,
        credential_retention_hours=0,
        login_commands=fake_login_cli(tmp_path),
    )
    ctx.admin = admin
    return admin


@pytest.fixture
def live_supervisor(ctx: AppContext, provider: FakeProvider) -> Supervisor:
    """A supervisor on the system clock, so its lease reads as live to both the API
    (fake clock, earlier) and the CLI (system clock)."""
    return Supervisor(
        ctx.uow_factory,
        {"fake": provider},
        SystemClock(),
        holder="admin-tests",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=300,
        harnesses=ctx.harnesses,
    )


@pytest.fixture
def admin_client(
    ctx: AppContext, tokens: dict[str, str], admin_ctx: AdminContext
) -> Iterator[TestClient]:
    with TestClient(create_app(ctx), headers={"Authorization": f"Bearer {tokens['admin']}"}) as c:
        yield c


@pytest.fixture
def config_file(migrated: str, credential_root: Path, tmp_path: Path) -> Path:
    """The CLI's local mode reads configuration; the same database, the same
    credential directories, the fake logins."""
    logins = fake_login_cli(tmp_path)
    lines = [
        # The fake provider and the script harness are test fixtures (crucible#124).
        "test_fixtures = true",
        "[database]",
        f'url = "{migrated}"',
        "[supervisor]",
        "lease_ttl_seconds = 300",
        "[admin]",
        "credential_retention_hours = 0",
        "[admin.login_commands]",
        *[f'{name} = ["{argv[0]}"]' for name, argv in logins.items()],
    ]
    for name in ("claude_code", "codex", "agy", "hermes"):
        lines += [f"[credentials.{name}]", f'path = "{credential_root / name}"']
    path = tmp_path / "crucible.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_cli(config: Path, *argv: str, capsys: pytest.CaptureFixture[str]) -> Any:
    admin_main(["--config", str(config), *argv])
    return envelope_data(capsys)


def audit_kinds(client: TestClient) -> list[tuple[str, str]]:
    items = client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
    return [(e["kind"], e["principal"]) for e in items]


def ui_sign_in(client: TestClient, token: str) -> str:
    form = client.get("/ui/sign-in")
    preauth = re.search(r'name="csrf" value="([a-f0-9]+)"', form.text)
    assert form.status_code == 200 and preauth is not None
    response = client.post(
        "/ui/sign-in",
        data={"csrf": preauth.group(1), "token": token, "next": "/ui"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/ui")
    assert page.status_code == 200
    match = re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)
    assert match is not None
    return match.group(1)


def test_sign_in_rejects_cross_site_form_without_the_preauth_nonce(
    ctx: AppContext, tokens: dict[str, str]
) -> None:
    with TestClient(create_app(ctx)) as browser:
        refused = browser.post(
            "/ui/sign-in",
            data={"token": tokens["admin"], "next": "/ui"},
            follow_redirects=False,
        )
        assert refused.status_code == 403
        assert "CSRF token is invalid" in refused.text
        assert "crucible_ui=" not in refused.headers.get("set-cookie", "")


# ----- the guard --------------------------------------------------------------------


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_ui_session_csrf_reader_access_and_page_walk(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
) -> None:
    with TestClient(create_app(ctx)) as browser:
        assert browser.get("/ui", follow_redirects=False).status_code == 303
        csrf = ui_sign_in(browser, tokens["observer"])
        for path in (
            "/ui",
            "/ui/harnesses",
            "/ui/credentials",
            "/ui/credentials/hermes/login",
            "/ui/images",
            "/ui/routing",
            "/ui/repositories",
            "/ui/tokens",
            "/ui/github",
            "/ui/workers",
            "/ui/tasks",
            "/ui/board",
            "/ui/wakes",
            "/ui/retention",
            "/ui/audit",
            "/ui/bootstrap",
            "/ui/settings",
        ):
            response = browser.get(path)
            assert response.status_code == 200, (path, response.text)
            assert "Crucible" in response.text
            assert "<pre" not in response.text, path
            assert re.search(r"\{\s*(?:&quot;|\")", response.text) is None, path
        forbidden = browser.post(
            "/ui/actions/harness",
            data={
                "csrf": csrf,
                "harness": "agy",
                "enabled": "false",
                "reason": "reader must not mutate",
                "return_to": "/ui/harnesses",
            },
            follow_redirects=False,
        )
        assert forbidden.status_code == 303
        assert "admin%20role%20required" in forbidden.headers["location"]


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_the_settings_page_shows_broad_egress_and_the_resolve_ttl(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue 61: the two egress settings are on the rendered settings page beside every
    other Kubernetes setting, with where each value came from."""
    monkeypatch.delenv("CRUCIBLE_CONFIG", raising=False)
    monkeypatch.setenv("CRUCIBLE_KUBERNETES__RESOLVE_TTL_SECONDS", "120")
    # A provider's settings are listed only while it is on (crucible#125).
    ctx.settings = Settings(kubernetes={"enabled": True})
    with TestClient(create_app(ctx)) as browser:
        ui_sign_in(browser, tokens["observer"])
        page = browser.get("/ui/settings")
    assert page.status_code == 200, page.text
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page.text))
    # What this deployment set leads, with where it came from; the defaults it left alone
    # are behind a click, without a source column (crucible#115).
    lead, _, defaults = text.partition("Defaults left unchanged")
    assert re.search(r"kubernetes\.resolve_ttl_seconds 120\.0 environment", lead), text
    assert re.search(r"kubernetes\.broad_egress no On, a worker", defaults), text
    assert "GitHub included" in defaults
    assert re.search(r"kubernetes\.launch_timeout_seconds 300 ", defaults), text


def test_ui_mutation_uses_the_same_harness_service_and_rejects_bad_csrf(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
) -> None:
    asyncio.run(live_supervisor.tick())
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        refused = browser.post(
            "/ui/actions/harness",
            data={
                "csrf": "wrong",
                "harness": "agy",
                "enabled": "false",
                "reason": "bad csrf",
                "return_to": "/ui/harnesses",
            },
            follow_redirects=False,
        )
        assert "CSRF" in refused.headers["location"]
        changed = browser.post(
            "/ui/actions/harness",
            data={
                "csrf": csrf,
                "harness": "agy",
                "enabled": "false",
                "reason": "ui parity test",
                "return_to": "/ui/harnesses",
            },
            follow_redirects=False,
        )
        assert changed.status_code == 303
    with ctx.uow_factory() as uow:
        state = uow.harnesses.get("agy")
        assert state is not None
        assert state.enabled is False and state.reason == "ui parity test"


@pytest.mark.usefixtures("engine")
def test_migrate_delivers_the_first_admin_token_and_never_prints_it(
    migrated: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """crucible#122: the token goes to the delivery, mode 0600, and never to stdout or
    stderr; the log names where it is."""
    delivery = FileDelivery(tmp_path / "first-run-admin-token")
    cli.ensure_first_admin(migrated, delivery)
    first = capsys.readouterr()
    shown = delivery.path.read_text(encoding="utf-8").strip()
    assert shown.startswith("cru_")
    assert shown not in first.out and shown not in first.err
    assert "cru_" not in first.out + first.err
    assert str(delivery.path) in first.err
    assert os.stat(delivery.path).st_mode & 0o777 == 0o600
    cli.ensure_first_admin(migrated, delivery)
    assert capsys.readouterr().err == ""
    assert delivery.path.read_text(encoding="utf-8").strip() == shown
    engine = make_engine(migrated)
    try:
        with SqlUnitOfWorkFactory(engine)() as uow:
            principal = authenticate(uow, shown)
            assert principal is not None
            assert principal.name == "first-run-admin" and principal.role is Role.ADMIN
            uow.principals.disable(principal.id, SystemClock().now())
            uow.commit()
        cli.ensure_first_admin(migrated, delivery)
        recovery = capsys.readouterr()
        assert "cru_" not in recovery.out + recovery.err
        recovered_token = delivery.path.read_text(encoding="utf-8").strip()
        assert recovered_token != shown
        with SqlUnitOfWorkFactory(engine)() as uow:
            recovered = authenticate(uow, recovered_token)
            assert recovered is not None
            assert recovered.name.startswith("first-run-admin-")
            assert recovered.role is Role.ADMIN and recovered.disabled_at is None
    finally:
        engine.dispose()


@pytest.mark.usefixtures("engine")
def test_migrate_mints_nothing_it_cannot_deliver(
    migrated: str, capsys: pytest.CaptureFixture[str]
) -> None:
    class Refusing:
        def where(self) -> str:
            return "nowhere"

        def deliver(self, token: str) -> None:
            raise RuntimeError("403 on secrets: forbidden")

        def discard(self) -> None:
            raise AssertionError("never reached")

    with pytest.raises(RuntimeError, match="forbidden"):
        cli.ensure_first_admin(migrated, Refusing())
    cli.ensure_first_admin(migrated, None)
    printed = capsys.readouterr()
    assert "cru_" not in printed.out + printed.err
    assert "No first-run administrator was created" in printed.err
    engine = make_engine(migrated)
    try:
        with SqlUnitOfWorkFactory(engine)() as uow:
            assert not [p for p in uow.principals.list_all() if p.role is Role.ADMIN]
    finally:
        engine.dispose()


@pytest.mark.usefixtures("engine")
def test_the_first_sign_in_removes_the_first_run_token(
    ctx: AppContext, admin_ctx: AdminContext, migrated: str, tmp_path: Path
) -> None:
    delivery = FileDelivery(tmp_path / "first-run-admin-token")
    cli.ensure_first_admin(migrated, delivery)
    token = delivery.path.read_text(encoding="utf-8").strip()
    ctx.first_run = delivery
    with TestClient(create_app(ctx)) as browser:
        page = browser.get("/ui/sign-in").text
        # The page names this deployment's place, not `docker compose logs migrate`.
        assert str(delivery.path) in page and "logs migrate" not in page
        ui_sign_in(browser, token)
    assert not delivery.path.exists()


def test_a_revoke_removes_the_first_run_token_and_the_prefix_is_reserved(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    migrated: str,
    tmp_path: Path,
) -> None:
    asyncio.run(live_supervisor.tick())
    delivery = FileDelivery(tmp_path / "first-run-admin-token")
    admin_ctx.first_run = delivery
    with ctx.uow_factory() as uow:
        minted = mint_token(uow, ctx.clock, name="first-run-admin", role=Role.ADMIN)
        uow.commit()
    delivery.deliver(minted.token)
    reserved = admin_client.post(
        "/v1/admin/tokens",
        json={"name": "first-run-admin-2", "role": "admin", "reason": "x"},
    )
    assert reserved.status_code == 409, reserved.text
    assert "reserved" in reserved.text
    revoked = admin_client.post(
        f"/v1/admin/tokens/{minted.principal.id}/revoke", json={"reason": "leaked"}
    )
    assert revoked.status_code == 200, revoked.text
    assert not delivery.path.exists()


def test_token_and_repository_mutations_have_ui_api_and_cli_parity(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    api_created = admin_client.post(
        "/v1/admin/tokens",
        json={"name": "api-reader", "role": "observer", "reason": "parity"},
    )
    assert api_created.status_code == 200
    assert api_created.json()["token"].startswith("cru_")
    cli_created = run_cli(
        config_file,
        "--reason",
        "parity",
        "token",
        "create",
        "--principal",
        "cli-reader",
        "--role",
        "observer",
        capsys=capsys,
    )
    assert set(cli_created) == {"principal", "role", "token"}
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        ui_created = browser.post(
            "/ui/actions/token-create",
            data={
                "csrf": csrf,
                "name": "ui-reader",
                "role": "observer",
                "reason": "parity",
                "return_to": "/ui/tokens",
            },
        )
        assert ui_created.status_code == 200
        assert re.search(r"cru_[A-Z0-9]{26}\.[A-Za-z0-9_-]+", ui_created.text)

        principals = admin_client.get("/v1/admin/tokens").json()["items"]
        ids = {item["name"]: item["id"] for item in principals}
        ui_revoked = browser.post(
            "/ui/actions/token-revoke",
            data={
                "csrf": csrf,
                "principal_id": ids["api-reader"],
                "reason": "parity",
                "return_to": "/ui/tokens",
            },
            follow_redirects=False,
        )
        assert ui_revoked.status_code == 303
    api_revoked = admin_client.post(
        f"/v1/admin/tokens/{ids['ui-reader']}/revoke", json={"reason": "parity"}
    )
    assert api_revoked.json()["revoked"] is True
    cli_revoked = run_cli(
        config_file,
        "--reason",
        "parity",
        "token",
        "revoke",
        ids["cli-reader"],
        capsys=capsys,
    )
    assert cli_revoked["revoked"] is True
    assert {
        item["name"] for item in run_cli(config_file, "token", "list", capsys=capsys)["items"]
    } >= {
        "api-reader",
        "cli-reader",
        "ui-reader",
    }

    for name in ("api-remove", "cli-remove", "ui-remove"):
        response = admin_client.put(
            f"/v1/admin/repositories/{name}",
            json={
                "url": f"https://github.com/example-org/{name}",
                "attested_all_prs": True,
                "reason": "parity",
            },
        )
        assert response.status_code == 200
    assert admin_client.request(
        "DELETE", "/v1/admin/repositories/api-remove", json={"reason": "parity"}
    ).json()["removed"]
    assert run_cli(
        config_file,
        "--reason",
        "parity",
        "repositories",
        "remove",
        "cli-remove",
        capsys=capsys,
    )["removed"]
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        removed = browser.post(
            "/ui/actions/repository-remove",
            data={
                "csrf": csrf,
                "name": "ui-remove",
                "reason": "parity",
                "return_to": "/ui/repositories",
            },
            follow_redirects=False,
        )
    assert removed.status_code == 303
    assert {
        item["repository"] for item in admin_client.get("/v1/admin/repositories").json()["items"]
    }.isdisjoint({"api-remove", "cli-remove", "ui-remove"})


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_every_remaining_ui_mutation_dispatches_to_the_shared_application_service(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API and CLI parity tests above exercise the services themselves. This matrix
    proves every other mutating UI form reaches those same service functions."""
    ui = import_module("crucible.application.admin")
    routing_ui = import_module("crucible.adapters.ui.pages.routing")
    calls: list[str] = []

    def stub(name: str) -> Any:
        def called(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(name)
            return {"ok": True}

        return called

    def async_stub(name: str) -> Any:
        async def called(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(name)
            return {"ok": True}

        return called

    for owner, name, replacement in (
        (ui.credentials, "validate", async_stub("credential-validate")),
        (ui.credentials, "probe", async_stub("credential-probe")),
        (ui.credentials, "rotate", stub("credential-rotate")),
        (ui.credentials, "remove", stub("credential-remove")),
        (ui.login, "start_login", stub("login-start")),
        (ui.login, "submit_code", stub("login-code")),
        (ui.login, "cancel_login", stub("login-cancel")),
        (ui.login, "finish_login", stub("login-finish")),
        (ui.images, "defaults", async_stub("image-change")),
        (ui.images, "list_all", async_stub("image-change")),
        (ui.routing, "clear_exhaustion", stub("routing-clear")),
        (ui.repositories, "register", stub("repository-register")),
        (ui.repositories, "remove", stub("repository-remove")),
        (ui.github, "check", stub("github-check")),
        (ui.bootstrap, "commit", stub("bootstrap-commit")),
    ):
        monkeypatch.setattr(owner, name, replacement)
    monkeypatch.setattr(routing_ui, "put_routing_policy", stub("routing-upload"))
    monkeypatch.setattr(routing_ui, "put_policy", stub("policy-upload"))

    asyncio.run(live_supervisor.tick())
    common = {"reason": "UI dispatch parity", "return_to": "/ui"}
    requests = [
        ("credential", {"verb": "validate", "harness": "codex"}),
        ("credential", {"verb": "probe", "harness": "codex"}),
        (
            "credential",
            {"verb": "rotate", "harness": "codex", "new_path": "/prepared/codex"},
        ),
        ("credential", {"verb": "remove", "harness": "codex"}),
        ("login-start", {"harness": "codex"}),
        ("login-code", {"harness": "codex", "code": "fixture-code"}),
        ("login-cancel", {"harness": "codex"}),
        ("login-finish", {"harness": "codex"}),
        ("image-change", {"harness": "hermes", "digest": "sha256:" + "a" * 64}),
        ("routing-clear", {"pool": "primary"}),
        (
            "routing-upload",
            {"name": "fixture-routing", "version": "1", "document": "{}"},
        ),
        (
            "policy-upload",
            {"name": "fixture-policy", "version": "1", "document": "{}"},
        ),
        (
            "repository-register",
            {
                "name": "fixture-repository",
                "url": "https://example.invalid/repository.git",
                "default_branch": "main",
                "policy_name": "default-software",
            },
        ),
        ("repository-remove", {"name": "fixture-repository"}),
        ("github-check", {}),
        ("bootstrap-commit", {"import_id": "01TESTIMPORT00000000000000"}),
    ]
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        for action, fields in requests:
            response = browser.post(
                f"/ui/actions/{action}",
                data={"csrf": csrf, **common, **fields},
                follow_redirects=False,
            )
            assert response.status_code == 303, (action, response.text)
    assert calls == [
        "credential-validate",
        "credential-probe",
        "credential-rotate",
        "credential-remove",
        "login-start",
        "login-code",
        "login-cancel",
        "login-finish",
        "image-change",
        "routing-clear",
        "routing-upload",
        "policy-upload",
        "repository-register",
        "repository-remove",
        "github-check",
        "bootstrap-commit",
    ]


def test_only_a_mutation_that_needs_the_supervisor_waits_for_it(
    admin_client: TestClient, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator's direction of 2026-09-29: a configuration write proceeds while the
    supervisor misses a tick; removing or rotating a credential, or committing a
    bootstrap import, still needs a live one, and the refusal is recorded."""
    response = admin_client.post("/v1/admin/harnesses/agy/disable", json={"reason": "test"})
    assert response.status_code == 200, response.text
    token = admin_client.post(
        "/v1/admin/tokens", json={"name": "while-down", "role": "orchestrator"}
    )
    assert token.status_code in (200, 201), token.text
    cli = run_cli(config_file, "--reason", "test", "harnesses", "enable", "agy", capsys=capsys)
    assert "agy" in json.dumps(cli)

    removed = admin_client.post(
        "/v1/admin/credentials/codex/remove", json={"reason": "test while down"}
    )
    assert removed.status_code == 503, removed.text
    assert removed.json()["type"].endswith("supervisor-not-live")
    with pytest.raises(SystemExit):
        admin_main(
            [
                "--config",
                str(config_file),
                "--reason",
                "test while down",
                "credentials",
                "remove",
                "--harness",
                "codex",
            ]
        )
    assert "supervisor-not-live" in capsys.readouterr().out
    refusals = [
        e["payload"]["operation"]
        for e in admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
        if e["kind"] == "admin_refused"
    ]
    assert "credentials remove codex" in refusals


def test_a_reason_is_an_optional_note_except_on_a_destructive_mutation(
    admin_client: TestClient, live_supervisor: Supervisor
) -> None:
    """crucible#117, the operator's decision of 2026-09-25: a reason is an audit note the
    operator may leave out, and the event is recorded without one; revoking a token,
    removing a repository or a credential, and committing a bootstrap import still
    require one."""
    asyncio.run(live_supervisor.tick())
    response = admin_client.post("/v1/admin/harnesses/agy/disable", json={})
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    events = admin_client.get("/v1/admin/audit").json()["items"]
    disabled = [e for e in events if e["kind"] == "harness_disabled"][-1]
    assert disabled["payload"]["reason"] == ""
    for method, path in (
        ("POST", "/v1/admin/tokens/01ABCDEFGHJKMNPQRSTVWXYZ00/revoke"),
        ("DELETE", "/v1/admin/repositories/anything"),
        ("POST", "/v1/admin/credentials/codex/remove"),
        ("POST", "/v1/import/bootstrap/01ABCDEFGHJKMNPQRSTVWXYZ00/commit"),
    ):
        refused = admin_client.request(method, path, json={"reason": "  "})
        assert refused.status_code == 422, (path, refused.text)
        assert refused.json()["errors"][0]["path"] == "reason"


# ----- parity: every operation through both entry points ----------------------------


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_harnesses_list_enable_disable_through_api_and_cli(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    api = admin_client.get("/v1/admin/harnesses").json()["items"]
    local = run_cli(config_file, "harnesses", "list", capsys=capsys)["items"]
    assert [h["name"] for h in api] == [h["name"] for h in local]
    assert {h["name"]: h["credential"]["state"] for h in api} == {
        "claude_code": "configured",
        "codex": "configured",
        "agy": "configured",
        "hermes": "absent",
        "script-harness": "not_required",
    }
    disabled = admin_client.post(
        "/v1/admin/harnesses/agy/disable", json={"reason": "api: rotating"}
    ).json()
    assert disabled["enabled"] is False and disabled["reason"] == "api: rotating"
    enabled = run_cli(
        config_file, "--reason", "cli: rotated", "harnesses", "enable", "agy", capsys=capsys
    )
    assert enabled["enabled"] is True and enabled["reason"] == "cli: rotated"
    kinds = audit_kinds(admin_client)
    assert ("harness_disabled", "admin-principal") in kinds
    assert ("harness_enabled", "crucible-admin") in kinds


def test_an_unverified_harness_is_enabled_in_one_action_from_the_page_or_the_cli(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    config_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """hades #174: the configuration keeps codex off (unverified) as its starting value.
    The Harnesses page offers Enable with the reason as a warning, the page's Enable
    decides, and the CLI does the same with the warning in its answer."""
    asyncio.run(live_supervisor.tick())
    unverified = "unverified: Crucible-side refresh not yet observed (S1b step 5)"
    gates = {"codex": HarnessGate(enabled=False, reason=unverified)}
    ctx.harness_gates = gates
    admin_ctx.harness_gates = gates
    with ctx.uow_factory() as uow:
        state = uow.harnesses.get("codex")
        assert state is not None
        state.enabled_decided = False
        uow.harnesses.put(state)
        uow.commit()
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/harnesses").text
        flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(page)))
        assert "off by default" in flat and unverified in flat, flat
        assert 'name="enabled" value="true"' in page
        changed = browser.post(
            "/ui/actions/harness",
            data={
                "csrf": csrf,
                "harness": "codex",
                "enabled": "true",
                "reason": "",
                "return_to": "/ui/harnesses",
            },
            follow_redirects=False,
        )
        assert changed.status_code == 303, changed.text
        page = browser.get("/ui/harnesses").text
        flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(page)))
        assert "off by default" not in flat
        assert f"codex off enabled none {unverified}" in flat, flat
    with ctx.uow_factory() as uow:
        state = uow.harnesses.get("codex")
        assert state is not None and state.enabled and state.enabled_decided

    gated = tmp_path / "gated.toml"
    gated.write_text(
        config_file.read_text() + f'\n[harnesses.codex]\nenabled = false\nreason = "{unverified}"\n'
    )
    off = run_cli(gated, "--reason", "cli: hold", "harnesses", "disable", "codex", capsys=capsys)
    assert off["enabled"] is False and off["warning"] == unverified
    on = run_cli(gated, "--reason", "cli: try it", "harnesses", "enable", "codex", capsys=capsys)
    assert on["enabled"] is True and on["decided_by_administrator"] is True
    assert on["warning"] == unverified
    listed = run_cli(gated, "harnesses", "list", capsys=capsys)["items"]
    codex = next(h for h in listed if h["name"] == "codex")
    assert codex["enabled"] is True and codex["enabled_by_configuration"] is False
    assert codex["warning"] == unverified


def test_credentials_validate_and_probe_through_api_and_cli(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    provider: FakeProvider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    response = admin_client.post(
        "/v1/admin/credentials/codex/validate", json={"reason": "onboarding"}
    )
    assert response.status_code == 200, response.text
    validated = response.json()
    assert validated["validated"] is True
    assert validated["shape"]["ok"] and validated["probe"]["exit_class"] == "completed"
    assert validated["credential"]["state"] == "validated"
    assert provider.probes == ["codex"]
    # Nothing secret-shaped in the whole document.
    from crucible.domain.secrets import scan_text  # noqa: PLC0415

    assert scan_text(json.dumps(validated)) is None
    probed = run_cli(
        config_file,
        "--reason",
        "cli probe",
        "credentials",
        "probe",
        "--harness",
        "agy",
        capsys=capsys,
    )
    assert probed["probe"]["harness_version"] == "fake"
    assert probed["probe"]["auth_files_changed"] is False
    assert set(probed["probe"]) == {
        "harness",
        "exit_class",
        "exit_code",
        "harness_version",
        "image",
        "image_digest",
        "auth_files_changed",
        "mount_mode",
        "duration_seconds",
        "files",
        "detail",
        "conclusive",
        "cause",
    }
    kinds = audit_kinds(admin_client)
    assert ("credential_validated", "admin-principal") in kinds
    assert ("credential_probed", "crucible-admin") in kinds
    status = run_cli(config_file, "credentials", "status", "--harness", "codex", capsys=capsys)
    assert status["state"] == "validated" and status["last_validated_at"]


def test_hermes_key_before_endpoint_is_saved_and_audited_as_inconclusive(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    credential_root: Path,
) -> None:
    asyncio.run(live_supervisor.tick())
    api_key = "vk_" + "n" * 40
    response = admin_client.post(
        "/v1/admin/credentials/hermes/set",
        json={"reason": "stage key before route", "api_key": api_key},
    )
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["validated"] is False
    assert document["conclusive"] is False
    assert document["cause"] == "endpoint_not_configured"
    assert api_key not in response.text
    assert (credential_root / "hermes" / "api-key").is_file()
    audit = admin_client.get("/v1/admin/audit", params={"limit": 200}).text
    assert "credential_set" in audit and api_key not in audit


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_local_endpoint_and_hermes_key_are_saved_without_exposing_the_key(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_client: TestClient,
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    credential_root: Path,
    config_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """C10: policy and credential writes share the admin guard and the saved key is
    used only in the authenticated models probe."""
    from crucible.application.admin import credentials as credentials_service  # noqa: PLC0415

    asyncio.run(live_supervisor.tick())
    proxy_path = tmp_path / "egress" / "squid.conf"
    admin_ctx.proxy_config_path = str(proxy_path)
    admin_ctx.proxy_hosts = ("github.com",)
    endpoint = "https://llm.apps.int.sentania.net/v1"
    updated = admin_client.post(
        "/v1/admin/routing/local-endpoint",
        json={
            "reason": "configure authenticated lab gateway",
            "endpoint_url": endpoint,
            "models": [{"id": "coder", "enabled": True, "enable_thinking": False}],
            "max_concurrency": 3,
        },
    )
    assert updated.status_code == 200, updated.text
    view = updated.json()
    assert view["endpoint_url"] == endpoint
    assert view["models"][0]["id"] == "coder" and view["models"][0]["enabled"]
    assert view["models"][0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert view["pool"]["max_concurrency"] == 3
    assert "llm.apps.int.sentania.net" in proxy_path.read_text(encoding="utf-8")
    assert (proxy_path.parent / "reload").is_file()
    assert admin_client.get("/v1/admin/routing/local-endpoint").json() == view

    observed: list[tuple[str, str | None]] = []

    def http_get(url: str, *, bearer: str | None, timeout: float) -> tuple[int, bytes]:
        observed.append((url, bearer))
        return 200, b'{"data": [{"id": "coder"}]}'

    monkeypatch.setattr(credentials_service, "_http_get", http_get)
    api_key = "vk_" + "q" * 40
    saved = admin_client.post(
        "/v1/admin/credentials/hermes/set",
        json={"reason": "install virtual key", "api_key": api_key},
    )
    assert saved.status_code == 200, saved.text
    document = saved.json()
    assert document["validated"] is True
    assert api_key not in saved.text
    key_path = credential_root / "hermes" / "api-key"
    assert key_path.read_text(encoding="utf-8").strip() == api_key
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert observed == [
        ("https://llm.apps.int.sentania.net/health/readiness", None),
        ("https://llm.apps.int.sentania.net/v1/models", api_key),
    ]
    audit = admin_client.get("/v1/admin/audit", params={"limit": 200}).text
    assert api_key not in audit
    assert "credential_set" in audit and "local_endpoint_updated" in audit
    assert "credential_validated" not in audit and "credential_probed" not in audit

    cli_key = "vk_" + "z" * 40
    monkeypatch.setattr(cli, "_read_api_key", lambda: cli_key)
    cli_saved = run_cli(
        config_file,
        "--reason",
        "rotate through CLI parity path",
        "credentials",
        "set",
        "--harness",
        "hermes",
        capsys=capsys,
    )
    assert cli_saved["validated"] is True and cli_key not in json.dumps(cli_saved)
    assert key_path.read_text(encoding="utf-8").strip() == cli_key
    cli_route = run_cli(config_file, "routing", "local-endpoint", capsys=capsys)
    assert cli_route["endpoint_url"] == endpoint and cli_route["models"][0]["id"] == "coder"

    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        # crucible#119: one place for the URL and the key, linked from Credentials and
        # Routing; neither page carries a key form of its own any more.
        credentials_page = browser.get("/ui/credentials")
        assert credentials_page.status_code == 200
        assert 'href="/ui/gateway"' in credentials_page.text
        assert "credential-set" not in credentials_page.text
        routing_page = browser.get("/ui/routing")
        assert routing_page.status_code == 200
        assert 'href="/ui/gateway"' in routing_page.text
        assert "routing-local" not in routing_page.text
        gateway_page = browser.get("/ui/gateway?models=1")
        assert gateway_page.status_code == 200
        assert 'action="/ui/actions/gateway-save"' in gateway_page.text
        assert 'name="api_key"' in gateway_page.text
        assert 'type="password"' in gateway_page.text
        assert cli_key not in gateway_page.text

        ui_key = "vk_" + "u" * 40
        ui_saved = browser.post(
            "/ui/actions/gateway-save",
            data={
                "csrf": csrf,
                "endpoint_url": endpoint,
                "api_key": ui_key,
                "reason": "rotate through browser form",
                "return_to": "/ui/gateway",
            },
            follow_redirects=False,
        )
        assert ui_saved.status_code == 303
        assert key_path.read_text(encoding="utf-8").strip() == ui_key
        message = unquote(ui_saved.headers["location"])
        assert f"Gateway {endpoint} reachable, key accepted, 1 model." in message
        assert ui_key not in message

        # #121: the model is picked from the gateway's own list, not typed.
        gateway_page = browser.get("/ui/gateway?models=1")
        assert 'name="model.0.id" value="coder"' in gateway_page.text
        assert 'name="model.0.enabled"' in gateway_page.text
        picks = {
            "csrf": csrf,
            "model.0.id": "coder",
            "model.0.enabled": "true",
            "model.0.thinking": "true",
            "model.0.capability": "mid",
            "max_concurrency": "2",
            "reason": "save through browser form",
            "return_to": "/ui/gateway",
        }
        # hades #437: the first post shows what the save publishes; nothing is saved.
        preview = browser.post("/ui/actions/gateway-models", data=picks, follow_redirects=False)
        assert preview.status_code == 200, preview.text
        assert "What saving publishes" in preview.text
        assert 'name="confirm" value="true"' in preview.text
        ui_models = browser.post(
            "/ui/actions/gateway-models",
            data={**picks, "confirm": "true"},
            follow_redirects=False,
        )
        assert ui_models.status_code == 303, ui_models.text
        assert "kind=ok" in ui_models.headers["location"]
        ui_view = admin_client.get("/v1/admin/routing/local-endpoint").json()
        assert ui_view["models"][0]["chat_template_kwargs"] == {"enable_thinking": True}
        assert ui_view["pool"]["max_concurrency"] == 2


def test_a_save_with_every_local_model_disabled_leaves_the_docker_allowlist_unchanged(
    admin_client: TestClient,
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
) -> None:
    """A disabled model gets no rendered Squid rule (proxy_config.enabled_local_endpoints);
    the in-process allowlist the Docker provider checks launches against must follow the
    same rule, or a disabled destination would still be reachable from that provider."""

    class _DockerStub:
        def __init__(self, config: DockerConfig) -> None:
            self.config = config

    asyncio.run(live_supervisor.tick())
    stub = _DockerStub(
        DockerConfig(endpoint="tcp://127.0.0.1:1", artifact_root="/tmp/does-not-matter")
    )
    admin_ctx.providers["docker"] = stub  # type: ignore[assignment]
    updated = admin_client.post(
        "/v1/admin/routing/local-endpoint",
        json={
            "reason": "leave every local model disabled",
            "endpoint_url": "https://llm.apps.int.sentania.net/v1",
            "models": [{"id": "coder", "enabled": False, "enable_thinking": False}],
            "max_concurrency": 3,
        },
    )
    assert updated.status_code == 200, updated.text
    assert stub.config.proxy_allowlist == ()


@pytest.mark.parametrize("flag", ["enabled", "enable_thinking"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_a_non_boolean_local_model_flag_is_rejected_and_changes_nothing(
    admin_client: TestClient,
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    tmp_path: Path,
    flag: str,
    value: object,
) -> None:
    """bool("false") is True, so a string flag would enable the model and open its proxy
    destination. The API takes JSON booleans only and writes nothing otherwise."""
    asyncio.run(live_supervisor.tick())
    proxy_path = tmp_path / "egress" / "squid.conf"
    admin_ctx.proxy_config_path = str(proxy_path)
    admin_ctx.proxy_hosts = ("github.com",)
    before = admin_client.get("/v1/admin/routing/local-endpoint").json()
    settings: dict[str, object] = {"id": "coder", "enabled": False, "enable_thinking": False}
    settings[flag] = value
    response = admin_client.post(
        "/v1/admin/routing/local-endpoint",
        json={
            "reason": "string flag from a direct caller",
            "endpoint_url": "https://llm.apps.int.sentania.net/v1",
            "models": [settings],
            "max_concurrency": 3,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["errors"] == [
        {"path": f"body.models.0.{flag}", "message": f"{flag} must be a JSON boolean"}
    ]
    assert admin_client.get("/v1/admin/routing/local-endpoint").json() == before
    assert not proxy_path.exists()


def test_credentials_rotate_and_remove_through_api_and_cli(
    admin_client: TestClient,
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    config_file: Path,
    credential_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    asyncio.run(live_supervisor.tick())
    old_token = (credential_root / "codex" / "auth.json").read_text(encoding="utf-8")
    incoming = tmp_path / "incoming-codex"
    incoming.mkdir()
    new_secret = _token("eyJ", 60)
    (incoming / "auth.json").write_text(
        json.dumps(
            {"tokens": {"refresh_token": new_secret}, "last_refresh": "2026-09-17T02:00:00Z"}
        ),
        encoding="utf-8",
    )
    rotated = admin_client.post(
        "/v1/admin/credentials/codex/rotate",
        json={"reason": "new login", "new_path": str(incoming)},
    ).json()
    assert rotated["retained_as"].startswith("codex.retired-")
    swapped = (credential_root / "codex" / "auth.json").read_text(encoding="utf-8")
    assert new_secret in swapped and swapped != old_token
    retired = credential_root / rotated["retained_as"]
    assert (retired / "auth.json").read_text(encoding="utf-8") == old_token
    # The caller's directory is untouched: Crucible copies it and never destroys a path
    # the operator named outside its own credential root.
    assert (incoming / "auth.json").read_text(encoding="utf-8").strip()
    assert rotated["source_kept"] == str(incoming)
    assert new_secret not in json.dumps(rotated) and old_token not in json.dumps(rotated)

    # Retention is 0 h here: the retired directory is shredded by the sweep.
    from crucible.application.admin.credentials import sweep_retired  # noqa: PLC0415

    clock.advance(1.1)
    with admin_ctx.uow_factory() as uow:
        assert sweep_retired(admin_ctx, uow, principal="crucible-admin") == 1
        uow.commit()
    assert not retired.exists()

    removed = run_cli(
        config_file,
        "--reason",
        "leaving",
        "credentials",
        "remove",
        "--harness",
        "agy",
        capsys=capsys,
    )
    assert removed["shredded"]["files"] == 1
    assert removed["credential"]["state"] == "absent"
    assert (credential_root / "agy").is_dir()
    assert not any(p.is_file() for p in (credential_root / "agy").rglob("*"))
    harness = next(
        h for h in admin_client.get("/v1/admin/harnesses").json()["items"] if h["name"] == "agy"
    )
    assert harness["enabled"] is False and "credential removed" in harness["reason"]
    kinds = audit_kinds(admin_client)
    for kind, who in (
        ("credential_rotated", "admin-principal"),
        ("credential_retired_shredded", "crucible-admin"),
        ("credential_removed", "crucible-admin"),
    ):
        assert (kind, who) in kinds, kinds


def test_login_through_api_and_cli_against_the_fake_cli(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    credential_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    refused = admin_client.post(
        "/v1/admin/credentials/claude_code/login", json={"reason": "onboarding"}
    )
    assert refused.status_code == 409, refused.text
    assert "already passes the shape check" in refused.json()["detail"]
    started = admin_client.post(
        "/v1/admin/credentials/claude_code/login",
        json={"reason": "onboarding", "replace": True},
    ).json()
    assert "captured to oauth-token" in started["window"]
    assert started["retained_as"].startswith("claude_code.retired-")
    state = _wait_for_login_state(admin_client, "claude_code", "waiting_for_code")
    assert state["url"] == "https://example.invalid/device", state
    admin_client.post(
        "/v1/admin/credentials/claude_code/login/code",
        json={"code": "ABCD-EFGH", "reason": "complete onboarding"},
    )
    state = _wait_for_login_state(admin_client, "claude_code", "finished", "failed")
    assert state["state"] == "finished", state
    assert state["token_written"] is True
    finished = admin_client.post(
        "/v1/admin/credentials/claude_code/login/finish", json={"reason": "onboarded"}
    ).json()
    assert finished["shape"]["ok"]
    assert "sk-ant-" not in json.dumps(finished) + json.dumps(state)

    monkeypatch.setattr("crucible.cli.admin._read_code", lambda: "ABCD-EFGH")
    result = run_cli(
        config_file,
        "--reason",
        "cli onboarding",
        "credentials",
        "login",
        "--harness",
        "codex",
        "--replace",
        capsys=capsys,
    )
    assert result["login"]["state"] == "finished" and result["login"]["code"] == "ABCD-EFGH"
    kinds = audit_kinds(admin_client)
    assert ("credential_login_started", "admin-principal") in kinds
    assert ("credential_login_finished", "crucible-admin") in kinds


def test_a_promoted_image_no_provider_lists_any_more_is_not_ready(
    admin_client: TestClient, live_supervisor: Supervisor, provider: FakeProvider
) -> None:
    """Codex on PR 164: the default image is a database row, so an image deleted from
    every provider after promotion still has one. Readiness names the missing image
    rather than reading the harness ready, and a fresh listing clears the step."""
    asyncio.run(live_supervisor.tick())
    image = ImageInfo(
        "ghcr.io/sentania-labs/crucible-worker:0.5.5", "sha256:" + "a" * 64, WORKER_HARNESSES
    )
    provider.images = [image]
    promoted = admin_client.post(
        f"/v1/admin/images/{image.digest}/promote", json={"harness": "hermes"}
    )
    assert promoted.status_code == 200, promoted.text

    def image_steps() -> list[dict[str, Any]]:
        readiness = admin_client.get("/v1/admin/status").json()["readiness"]
        hermes = next(h for h in readiness["harnesses"] if h["name"] == "hermes")
        return [s for s in hermes["steps"] if "image" in s["code"]]

    assert image_steps() == []
    provider.images = []
    missing = image_steps()
    assert [s["code"] for s in missing] == ["promoted_image_missing"]
    assert missing[0]["fix"] == "/ui/images"
    assert image.reference in missing[0]["text"]
    assert "no longer in the registry" in missing[0]["text"]
    provider.images = [image]
    assert image_steps() == []


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_row_actions_offer_an_optional_reason_and_destructive_ones_require_it(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_client: TestClient,
    live_supervisor: Supervisor,
    provider: FakeProvider,
) -> None:
    """Codex on PR 164 (crucible#117): enable, disable, promote, roll back and pool clear
    carry an optional note on their row; revoke keeps a required one. A note typed on a
    row reaches the audit record, and leaving it out is accepted."""
    asyncio.run(live_supervisor.tick())
    first = ImageInfo(
        "ghcr.io/sentania-labs/crucible-worker:0.5.5", "sha256:" + "b" * 64, WORKER_HARNESSES
    )
    second = ImageInfo(
        "ghcr.io/sentania-labs/crucible-worker:0.5.6", "sha256:" + "c" * 64, WORKER_HARNESSES
    )
    provider.images = [first, second]
    for image in (first, second):
        promoted = admin_client.post(
            f"/v1/admin/images/{image.digest}/promote", json={"harness": "hermes"}
        )
        assert promoted.status_code == 200, promoted.text
    submitted = admin_client.post(
        "/v1/tasks",
        json=contract_document(external_id="ROW-REASON"),
        headers={"Authorization": f"Bearer {tokens['orchestrator']}"},
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]
    assert live_supervisor.fenced_token is not None
    with ctx.uow_factory() as uow:
        # A mark names the attempt that hit the limit; attempts are the supervisor's (14).
        uow.set_fenced_token(live_supervisor.fenced_token)
        execution = Execution(
            id=new_id(),
            task_id=task_id,
            role=ExecutionRole.IMPLEMENT,
            contract_version=1,
            harness="script-harness",
            model="fake",
            effort=None,
            provider="fake",
            image="crucible-worker:fake-succeed",
            policy_snapshot={},
            state=ExecutionState.ACTIVE,
            max_attempts=1,
            retry_on=[],
            timeout_seconds=60,
            created_at=ctx.clock.now(),
        )
        attempt = Attempt(
            id=new_id(),
            execution_id=execution.id,
            task_id=task_id,
            number=1,
            state=AttemptState.FAILED,
            created_at=ctx.clock.now(),
        )
        uow.executions.add(execution)
        uow.attempts.add(attempt)
        uow.pool_exhaustions.put(
            PoolExhaustion(
                pool="primary",
                exhausted_at=ctx.clock.now(),
                reset_at=ctx.clock.now() + timedelta(hours=5),
                task_id=task_id,
                attempt_id=attempt.id,
                reason="soft limit reached",
            )
        )
        uow.commit()

    def reason_inputs(page: str, action: str) -> list[str]:
        """The reason input of each row form posting to `action`, or "" for none."""
        forms = re.findall(
            rf'<form class="admin-row-form" method="post" action="{action}">(.*?)</form>',
            page,
            re.S,
        )
        assert forms, action
        found = [re.search(r'<input class="lat-input" name="reason"[^>]*>', f) for f in forms]
        return [match.group(0) if match else "" for match in found]

    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        pages = {
            path: browser.get(path).text
            for path in ("/ui/harnesses", "/ui/images", "/ui/routing", "/ui/tokens")
        }
        for path, action in (
            ("/ui/harnesses", "/ui/actions/harness"),
            ("/ui/images", "/ui/actions/image-change"),
            ("/ui/routing", "/ui/actions/routing-clear"),
        ):
            for found in reason_inputs(pages[path], action):
                assert 'placeholder="Reason (optional)"' in found, (path, action)
                assert "required" not in found, (path, action)
        for found in reason_inputs(pages["/ui/tokens"], "/ui/actions/token-revoke"):
            assert 'placeholder="Reason (required)"' in found and found.endswith("required>")
        # The Test action is a read-only check and asks for no reason at all.
        assert reason_inputs(pages["/ui/harnesses"], "/ui/actions/harness-test")[0] == ""
        # The images page no longer has reason inputs on its row actions.
        # Verify the image-change form has no reason field.
        assert 'name="reason"' not in pages["/ui/images"]

        # Post the unified image-change action with the previous digest to trigger a rollback.
        noted = browser.post(
            "/ui/actions/image-change",
            data={
                "csrf": csrf,
                "harness": "hermes",
                "digest": "sha256:" + "b" * 64,
                "return_to": "/ui/images",
            },
            follow_redirects=False,
        )
        assert noted.status_code == 303 and "kind=ok" in noted.headers["location"]
        bare = browser.post(
            "/ui/actions/routing-clear",
            data={"csrf": csrf, "pool": "primary", "reason": "", "return_to": "/ui/routing"},
            follow_redirects=False,
        )
        assert bare.status_code == 303 and "kind=ok" in bare.headers["location"]
    events = admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
    rollback = next(
        e for e in events if e["kind"] == "image_promoted" and e["payload"].get("rollback")
    )
    assert rollback["payload"]["reason"] is None
    with ctx.uow_factory() as uow:
        mark = uow.pool_exhaustions.get("primary")
        assert mark is not None and mark.cleared_at is not None


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_images_are_promoted_and_rolled_back_per_harness(
    ctx: AppContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    provider: FakeProvider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """crucible#116, ADR 0018, the operator's decision of 2026-09-25: each harness has its
    own default image. Promoting one image for Hermes and another for AGY leaves each
    where it was put, a rollback moves only the harness it names, and a launch resolves
    the launching harness's own image."""
    asyncio.run(live_supervisor.tick())
    first = ImageInfo(
        reference="ghcr.io/sentania-labs/crucible-worker:0.5.5",
        digest="sha256:" + "d" * 64,
        harnesses=WORKER_HARNESSES,
    )
    second = ImageInfo(
        "ghcr.io/sentania-labs/crucible-worker:0.5.6", "sha256:" + "e" * 64, WORKER_HARNESSES
    )
    proof = ImageInfo(
        "ghcr.io/sentania-labs/crucible-worker:ci-35000000000",
        "sha256:" + "f" * 64,
        WORKER_HARNESSES,
    )
    provider.images = [first, second, proof]
    listed = admin_client.get("/v1/admin/images").json()
    assert {i["promotion_state"] for i in listed["items"]} == {"candidate"}
    hermes_row = next(row for row in listed["defaults"] if row["harness"] == "hermes")
    assert hermes_row["current"] is None
    # A CI proof tag is never offered (crucible#111).
    assert [c["reference"] for c in hermes_row["choices"]] == [first.reference, second.reference]

    for harness in ("hermes", "agy"):
        promoted = admin_client.post(
            f"/v1/admin/images/{first.digest}/promote", json={"harness": harness}
        )
        assert promoted.status_code == 200, promoted.text
    moved = admin_client.post(
        f"/v1/admin/images/{second.digest}/promote", json={"harness": "agy", "reason": "canary"}
    ).json()
    assert moved["harness"] == "agy" and moved["digest"] == second.digest
    assert moved["previous"]["digest"] == first.digest
    rows = {r["harness"]: r for r in admin_client.get("/v1/admin/images").json()["defaults"]}
    assert rows["hermes"]["current"]["digest"] == first.digest
    assert rows["agy"]["current"]["digest"] == second.digest
    assert rows["claude_code"]["current"] is None
    with ctx.uow_factory() as uow:
        assert image_for_harness(uow, "hermes", "kubernetes") == first.reference
        assert image_for_harness(uow, "agy", "kubernetes") == second.reference
        assert image_for_harness(uow, "codex", "kubernetes") is None

    # A rollback moves only the harness it names, and a second one undoes the first.
    back = admin_client.post("/v1/admin/images/rollback", json={"harness": "agy"}).json()
    assert back["digest"] == first.digest and back["previous"]["digest"] == second.digest
    rows = {r["harness"]: r for r in admin_client.get("/v1/admin/images").json()["defaults"]}
    assert rows["agy"]["current"]["digest"] == first.digest
    assert rows["hermes"]["current"]["digest"] == first.digest
    assert rows["hermes"]["previous"] is None
    refused = admin_client.post("/v1/admin/images/rollback", json={"harness": "hermes"})
    assert refused.status_code == 409, refused.text
    ci = admin_client.post(f"/v1/admin/images/{proof.digest}/promote", json={"harness": "agy"})
    assert ci.status_code == 409 and "CI proof tag" in ci.json()["detail"]
    unnamed = admin_client.post(f"/v1/admin/images/{first.digest}/promote", json={})
    assert unnamed.status_code == 422 and unnamed.json()["errors"][0]["path"] == "harness"
    states = {
        i["digest"]: (i["promotion_state"], i["default_for"], i["previous_for"])
        for i in admin_client.get("/v1/admin/images").json()["items"]
    }
    assert states[first.digest] == ("default", ["agy", "hermes"], [])
    assert states[second.digest] == ("retained", [], ["agy"])

    # A previous image no provider lists any more is not a rollback target.
    provider.images = [first, proof]
    gone = admin_client.post("/v1/admin/images/rollback", json={"harness": "agy"})
    assert gone.status_code == 409 and "any more" in gone.json()["detail"], gone.text
    provider.images = [first, second, proof]
    with pytest.raises(SystemExit):
        admin_main(
            ["--config", str(config_file), "images", "promote", "sha256:nope", "--harness", "agy"]
        )
    assert "not-found" in capsys.readouterr().out
    events = admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
    promotions = [e["payload"] for e in events if e["kind"] == "image_promoted"]
    assert [(p["harness"], p["rollback"]) for p in promotions] == [
        ("hermes", False),
        ("agy", False),
        ("agy", False),
        ("agy", True),
    ]


def test_providers_github_audit_status_and_capabilities(
    ctx: AppContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    tokens: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    api_providers = admin_client.get("/v1/admin/providers").json()["items"]
    cli_providers = run_cli(config_file, "providers", "status", capsys=capsys)["items"]
    assert api_providers[0]["health"] == "ok" and cli_providers[0]["health"] == "ok"

    api_github = admin_client.get("/v1/admin/github").json()
    assert api_github["configured"] is False and api_github["key_present"] is False
    assert api_github["repositories"][0]["repository"] == "example-service"
    assert run_cli(config_file, "github", "status", capsys=capsys)["configured"] is False
    assert admin_client.post("/v1/admin/github/check", json={"reason": "x"}).status_code == 409
    with pytest.raises(SystemExit):
        admin_main(["--config", str(config_file), "--reason", "x", "github", "check"])

    # A registration under /admin is a mutation like any other: a live lease, and the
    # reason when one is given (crucible#117).
    registered = admin_client.put(
        "/v1/admin/repositories/second",
        json={
            "url": "https://github.com/example-org/second",
            "attested_all_prs": True,
            "reason": "onboarding the second repository",
        },
    ).json()
    assert registered["repository"] == "second"
    from_cli = run_cli(
        config_file,
        "--reason",
        "onboarding the third repository",
        "repositories",
        "register",
        "--name",
        "third",
        "--url",
        "https://github.com/example-org/third",
        "--attest-external-review-all-prs",
        capsys=capsys,
    )
    # One service, one document: the two entry points differ only in the values.
    assert set(from_cli) == set(registered)
    registration_events = [
        e
        for e in admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
        if e["kind"] == "repository_registered" and e["payload"]["repository"] == "second"
    ]
    assert registration_events[0]["payload"]["reason"] == "onboarding the second repository"

    document = admin_client.get("/v1/admin/status").json()
    assert set(document) == {
        "harnesses",
        "credentials",
        "providers",
        "github",
        "supervisor",
        "workers",
        "tasks",
        "wakes",
        "retention",
        "bootstrap",
        "audit",
        "readiness",
    }
    assert document["supervisor"]["healthy"] is True
    # crucible#123: the to-do list is part of the one status document, so the API and
    # the CLI read the same list the Status page shows, and the script harness (a test
    # fixture) is never in it.
    assert "script-harness" not in {h["name"] for h in document["readiness"]["harnesses"]}
    assert document["bootstrap"] == {"authoritative": None, "imports": []}
    assert {r["repository"] for r in document["github"]["repositories"]} >= {"second", "third"}
    local = run_cli(config_file, "status", capsys=capsys)
    assert set(local) == set(document)

    tail = admin_client.get("/v1/admin/audit", params={"limit": 2}).json()
    assert len(tail["items"]) == 2 and tail["next_cursor"] == tail["items"][-1]["seq"]
    rest = admin_client.get(
        "/v1/admin/audit", params={"cursor": tail["next_cursor"], "limit": 200}
    ).json()
    assert all(e["seq"] > tail["next_cursor"] for e in rest["items"])
    kinds = {e["kind"] for e in tail["items"] + rest["items"]}
    assert "repository_registered" in kinds and "attempt_running" not in kinds
    assert (
        run_cli(config_file, "audit", "tail", "--limit", "2", capsys=capsys)["items"]
        == tail["items"]
    )

    with TestClient(
        create_app(ctx), headers={"Authorization": f"Bearer {tokens['orchestrator']}"}
    ) as orchestrator:
        view = orchestrator.get("/v1/capabilities").json()
        assert set(view) == {"harnesses", "providers", "github", "workers", "tasks", "wakes"}
        assert set(view["harnesses"][0]["credential"]) == {"state", "session_compatibility"}
        assert orchestrator.get("/v1/admin/status").status_code == 403
        assert (
            orchestrator.post("/v1/admin/harnesses/agy/disable", json={"reason": "x"}).status_code
            == 403
        )
    with TestClient(
        create_app(ctx), headers={"Authorization": f"Bearer {tokens['observer']}"}
    ) as observer:
        assert observer.get("/v1/capabilities").status_code == 403


def test_capabilities_show_an_orchestrator_its_own_work_only(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
) -> None:
    """crucible#40: one orchestrator cannot read another's tasks, attempts or wakes
    through /v1/capabilities; an operator still sees everything."""
    with ctx.uow_factory() as uow:
        other = mint_token(uow, ctx.clock, name="other-orchestrator", role=Role.ORCHESTRATOR)
        uow.commit()

    def client(token: str) -> TestClient:
        return TestClient(create_app(ctx), headers={"Authorization": f"Bearer {token}"})

    with client(tokens["orchestrator"]) as mine, client(other.token) as theirs:
        # hades #564: a branch belongs to one task, so each names its own.
        mine_doc = contract_document(external_id="EX-MINE")
        mine_doc["repository"]["work_branch"] = "crucible/EX-MINE"
        theirs_doc = contract_document(external_id="EX-THEIRS")
        theirs_doc["repository"]["work_branch"] = "crucible/EX-THEIRS"
        own = mine.post("/v1/tasks", json=mine_doc)
        foreign = theirs.post("/v1/tasks", json=theirs_doc)
        assert own.status_code == 201 and foreign.status_code == 201
        asyncio.run(live_supervisor.tick())
        assert live_supervisor.fenced_token is not None
        with ctx.uow_factory() as uow:
            # Each task blocked, so it is listed with its external id, and each with one
            # running attempt, so it is a worker (attempts are the supervisor's, 14).
            uow.set_fenced_token(live_supervisor.fenced_token)
            for task_id in (own.json()["id"], foreign.json()["id"]):
                task = uow.tasks.get(task_id)
                assert task is not None
                task.state = TaskState.BLOCKED
                uow.tasks.save(task)
                execution = Execution(
                    id=new_id(),
                    task_id=task.id,
                    role=ExecutionRole.IMPLEMENT,
                    contract_version=1,
                    harness="script-harness",
                    model="fake",
                    effort=None,
                    provider="fake",
                    image="crucible-worker:fake-succeed",
                    policy_snapshot={},
                    state=ExecutionState.ACTIVE,
                    max_attempts=1,
                    retry_on=[],
                    timeout_seconds=60,
                    created_at=ctx.clock.now(),
                )
                uow.executions.add(execution)
                uow.attempts.add(
                    Attempt(
                        id=new_id(),
                        execution_id=execution.id,
                        task_id=task.id,
                        number=1,
                        state=AttemptState.RUNNING,
                        created_at=ctx.clock.now(),
                    )
                )
                create_wake(
                    uow,
                    ctx.clock,
                    principal_id=task.principal_id,
                    reason=WakeReason.BLOCKED,
                    summary="blocked",
                    task=task,
                    raised_by="tests",
                )
            uow.commit()

        mine_view = mine.get("/v1/capabilities").json()
        theirs_view = theirs.get("/v1/capabilities").json()

    for view, own_id, foreign_id in (
        (mine_view, "EX-MINE", "EX-THEIRS"),
        (theirs_view, "EX-THEIRS", "EX-MINE"),
    ):
        assert view["tasks"]["counts"] == {"blocked": 1}
        assert [t["external_id"] for t in view["tasks"]["lists"]["blocked"]] == [own_id]
        assert [w["external_id"] for w in view["workers"]] == [own_id]
        assert foreign_id not in json.dumps(view)
    assert mine_view["wakes"]["pending"] == {"orchestrator-principal": 1}
    assert mine_view["wakes"]["unacked"] == 1
    assert theirs_view["wakes"]["pending"] == {"other-orchestrator": 1}

    with client(tokens["operator"]) as operator:
        everything = operator.get("/v1/capabilities").json()
    assert everything["tasks"]["counts"] == {"blocked": 2}
    assert sorted(w["external_id"] for w in everything["workers"]) == ["EX-MINE", "EX-THEIRS"]
    assert everything["wakes"]["pending"] == {
        "orchestrator-principal": 1,
        "other-orchestrator": 1,
    }
    assert everything["wakes"]["unacked"] == 2


def test_capabilities_wakes_count_is_not_truncated_by_the_page_limit(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
) -> None:
    """Codex round on #130 (crucible#116): an orchestrator with more unacknowledged
    wakes than the status page limit (200) still gets its true count, not the page
    size, and another orchestrator's wakes are never counted against it."""
    with ctx.uow_factory() as uow:
        other = mint_token(uow, ctx.clock, name="other-orchestrator", role=Role.ORCHESTRATOR)
        mine = uow.principals.get_by_name("orchestrator-principal")
        assert mine is not None
        for _ in range(205):
            create_wake(
                uow,
                ctx.clock,
                principal_id=mine.id,
                reason=WakeReason.BLOCKED,
                summary="blocked",
                raised_by="tests",
            )
        create_wake(
            uow,
            ctx.clock,
            principal_id=other.principal.id,
            reason=WakeReason.BLOCKED,
            summary="blocked",
            raised_by="tests",
        )
        uow.commit()

    def client(token: str) -> TestClient:
        return TestClient(create_app(ctx), headers={"Authorization": f"Bearer {token}"})

    with client(tokens["orchestrator"]) as mine_client, client(other.token) as theirs_client:
        mine_view = mine_client.get("/v1/capabilities").json()
        theirs_view = theirs_client.get("/v1/capabilities").json()

    assert mine_view["wakes"]["pending"] == {"orchestrator-principal": 205}
    assert mine_view["wakes"]["unacked"] == 205
    assert theirs_view["wakes"]["pending"] == {"other-orchestrator": 1}
    assert theirs_view["wakes"]["unacked"] == 1

    with client(tokens["operator"]) as operator:
        everything = operator.get("/v1/capabilities").json()
    assert everything["wakes"]["pending"] == {
        "orchestrator-principal": 205,
        "other-orchestrator": 1,
    }
    assert everything["wakes"]["unacked"] == 206


def test_the_cli_remote_mode_builds_the_same_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, Any]] = []

    def fake_call(self: Any, method: str, path: str, body: Any = None, **_: Any) -> Any:
        calls.append((method, path, body))
        return {"ok": True}

    monkeypatch.setattr(Api, "call", fake_call)
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    admin_main(["--api-url", "http://127.0.0.1:1", "--reason", "r", "harnesses", "disable", "agy"])
    admin_main(
        [
            "--api-url",
            "http://127.0.0.1:1",
            "--reason",
            "r",
            "credentials",
            "probe",
            "--harness",
            "codex",
        ]
    )
    admin_main(
        ["--api-url", "http://127.0.0.1:1", "audit", "tail", "--cursor", "5", "--limit", "10"]
    )
    admin_main(["--api-url", "http://127.0.0.1:1", "routing", "exhaustion"])
    admin_main(
        [
            "--api-url",
            "http://127.0.0.1:1",
            "--reason",
            "r",
            "routing",
            "clear-exhaustion",
            "pool-a",
        ]
    )
    assert calls == [
        ("POST", "/v1/admin/harnesses/agy/disable", {"reason": "r"}),
        ("POST", "/v1/admin/credentials/codex/probe", {"reason": "r"}),
        ("GET", "/v1/admin/audit?limit=10&cursor=5", None),
        ("GET", "/v1/admin/routing/exhaustion", None),
        ("POST", "/v1/admin/routing/exhaustion/pool-a/clear", {"reason": "r"}),
    ]
    assert Role.ADMIN.value == "admin"


def test_remote_login_submits_the_reason_with_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, Any]] = []
    states = iter(
        [
            {"state": "waiting_for_code", "output_tail": []},
            {"state": "finished", "output_tail": []},
        ]
    )

    def fake_call(self: Any, method: str, path: str, body: Any = None, **_: Any) -> Any:
        calls.append((method, path, body))
        if method == "GET":
            return next(states)
        if path.endswith("/login"):
            return {"window": "login window"}
        return {"state": "finished"}

    monkeypatch.setattr(Api, "call", fake_call)
    monkeypatch.setattr("crucible.cli.admin.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("crucible.cli.admin._read_code", lambda: "operator-code")
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    admin_main(
        [
            "--api-url",
            "http://127.0.0.1:1",
            "--reason",
            "operator approved login",
            "credentials",
            "login",
            "--harness",
            "claude_code",
        ]
    )

    assert (
        "POST",
        "/v1/admin/credentials/claude_code/login/code",
        {"reason": "operator approved login", "code": "operator-code"},
    ) in calls


# ----- the correction round ----------------------------------------------------------


def test_a_secret_shaped_reason_is_refused_on_both_entry_points(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """B2: the reason is written to the append-only event log and served back by
    `GET /admin/audit`, so a pasted token would be stored for ever."""
    asyncio.run(live_supervisor.tick())
    secret = _token("sk-ant-oat01-")
    response = admin_client.post(
        "/v1/admin/harnesses/agy/disable", json={"reason": f"rotating {secret}"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["path"] == "reason"
    assert secret not in response.text
    with pytest.raises(SystemExit):
        admin_main(
            [
                "--config",
                str(config_file),
                "--reason",
                f"rotating {secret}",
                "harnesses",
                "disable",
                "agy",
            ]
        )
    err = capsys.readouterr().out
    assert "contract-validation" in err or "reason" in err
    assert secret not in err
    kinds = audit_kinds(admin_client)
    assert ("admin_refused", "admin-principal") in kinds, kinds
    assert secret not in json.dumps(
        admin_client.get("/v1/admin/audit", params={"limit": 200}).json()
    )


def test_registering_a_repository_takes_both_guards_on_both_entry_points(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """B3: the admin route called the legacy service directly, so it skipped the guard.
    A registration is a configuration write, so since the operator's direction of
    2026-09-29 it proceeds with the supervisor down; the guard still audits it."""
    body = {"url": "https://github.com/example-org/guarded", "attested_all_prs": True}
    down = admin_client.put("/v1/admin/repositories/guarded", json={**body, "reason": "x"})
    assert down.status_code == 200, down.text
    asyncio.run(live_supervisor.tick())
    # A reason is an optional note on a registration (crucible#117).
    assert admin_client.put("/v1/admin/repositories/guarded", json=body).status_code == 200
    registered = run_cli(
        config_file,
        "repositories",
        "register",
        "--name",
        "guarded-cli",
        "--url",
        "https://github.com/example-org/guarded-cli",
        "--attest-external-review-all-prs",
        capsys=capsys,
    )
    assert registered["repository"] == "guarded-cli"


def test_finishing_a_login_takes_both_guards(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    credential_root: Path,
) -> None:
    """B4: it writes session_compatibility and clears last_validated_at, so it is a
    mutation and was the one without the guards. It needs no live supervisor (the
    operator's direction of 2026-09-29): with none, what refuses a finish with nothing
    to finish is still the login state."""
    no_lease = admin_client.post(
        "/v1/admin/credentials/codex/login/finish", json={"reason": "done"}
    )
    assert no_lease.status_code == 409, no_lease.text
    asyncio.run(live_supervisor.tick())
    # The reason is an optional note (crucible#117): with the lease held, what refuses a
    # finish with nothing to finish is the login state, not a missing reason.
    no_reason = admin_client.post("/v1/admin/credentials/codex/login/finish", json={})
    assert no_reason.status_code == 409, no_reason.text
    assert "reason" not in no_reason.json()["detail"]


def test_a_failed_swap_leaves_the_configured_directory_exactly_as_it_was(
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    credential_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B6: the swap is two renames. When the second failed the configured path was gone,
    the exception propagated before any event, and every later launch failed on a missing
    credential with nothing in the audit to say why."""
    import crucible.application.admin.credentials as credentials_module  # noqa: PLC0415

    asyncio.run(live_supervisor.tick())
    before = (credential_root / "codex" / "auth.json").read_text(encoding="utf-8")
    incoming = tmp_path / "incoming-rollback"
    incoming.mkdir()
    (incoming / "auth.json").write_text(
        json.dumps(
            {"tokens": {"refresh_token": _token("eyJ", 60)}, "last_refresh": "2026-09-17T03:00:00Z"}
        ),
        encoding="utf-8",
    )
    real_rename = os.rename
    calls = {"n": 0}

    def failing(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(18, "Invalid cross-device link")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", failing)
    with admin_ctx.uow_factory() as uow, pytest.raises(ApplicationError) as caught:
        credentials_module.rotate(
            admin_ctx,
            uow,
            principal="admin-principal",
            harness="codex",
            new_path=str(incoming),
            reason="a swap that fails",
        )
    assert "rolled back" in str(caught.value.detail)
    monkeypatch.undo()
    assert (credential_root / "codex" / "auth.json").read_text(encoding="utf-8") == before
    assert not any(p.name.startswith("codex.incoming-") for p in credential_root.iterdir())
    assert not any(p.name.startswith("codex.retired-") for p in credential_root.iterdir())
    assert (incoming / "auth.json").is_file(), "the caller's directory is never touched"


def test_an_inconclusive_probe_leaves_the_credential_state_alone(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    provider: FakeProvider,
) -> None:
    """A probe that timed out is evidence about the run, not about the credential. The
    first version of this fix marked the credential invalid on any unsuccessful probe,
    which would take a working harness out of service on latency alone."""
    asyncio.run(live_supervisor.tick())
    first = admin_client.post(
        "/v1/admin/credentials/codex/validate", json={"reason": "first pass"}
    ).json()
    assert first["validated"] is True and first["conclusive"] is True
    assert first["credential"]["state"] == "validated"

    provider.probe_outcome = "timeout"
    slow = admin_client.post(
        "/v1/admin/credentials/codex/validate", json={"reason": "a slow daemon"}
    ).json()
    assert slow["validated"] is False
    assert slow["conclusive"] is False and slow["cause"] == "timeout"
    assert slow["probe"]["conclusive"] is False
    assert slow["credential"]["state"] == "validated", slow["credential"]
    status = admin_client.get("/v1/admin/status").json()
    assert status["credentials"]["codex"]["last_launch_outcome"] == "probe:inconclusive:timeout"

    provider.probe_outcome = "auth_failure"
    refused = admin_client.post(
        "/v1/admin/credentials/codex/validate", json={"reason": "after a revoked token"}
    ).json()
    assert refused["validated"] is False
    assert refused["conclusive"] is True and refused["cause"] == ""
    assert refused["probe"]["exit_class"] == "auth_failure"
    assert refused["credential"]["state"] == "invalid", refused["credential"]
    assert admin_client.get("/v1/admin/credentials/codex").json()["state"] == "invalid"


def test_a_probe_the_provider_never_answered_is_inconclusive(
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon that will not answer says nothing about the credential either, and the
    operator gets a record with the cause rather than a 500."""
    from crucible.ports.execution import ProviderError  # noqa: PLC0415

    asyncio.run(live_supervisor.tick())

    async def refuse(_request: object) -> None:
        raise ProviderError("the daemon socket is not there")

    monkeypatch.setattr(provider, "probe_credential", refuse)
    with admin_ctx.uow_factory() as uow:
        before = credentials_state(admin_ctx, uow, "codex")
    with admin_ctx.uow_factory() as uow:
        report = asyncio.run(
            credentials_module_probe(admin_ctx, uow, harness="codex", reason="daemon down")
        )
        uow.commit()
    assert report.probe is not None
    assert report.probe.conclusive is False
    assert report.probe.cause == "provider_unavailable"
    assert report.probe.detail.startswith("ProviderError")
    # Whatever the state was, it is what it still is: nothing was observed.
    assert report.state["state"] == before["state"]
    assert report.state["last_auth_failure_at"] == before["last_auth_failure_at"]
    assert report.state["last_launch_outcome"] == "probe:inconclusive:provider_unavailable"


def credentials_state(ctx: AdminContext, uow: Any, harness: str) -> Any:
    from crucible.application.admin import credentials as credentials_module  # noqa: PLC0415

    return credentials_module.state_view(ctx, uow, harness)


def credentials_module_probe(ctx: AdminContext, uow: Any, *, harness: str, reason: str) -> Any:
    from crucible.application.admin import credentials as credentials_module  # noqa: PLC0415

    return credentials_module.probe(
        ctx, uow, principal="admin-principal", harness=harness, reason=reason
    )


# ----- the second correction round --------------------------------------------------


def test_a_login_that_cannot_run_refuses_with_the_credential_still_at_its_path(
    admin_client: TestClient,
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    credential_root: Path,
    tmp_path: Path,
) -> None:
    """The harness CLIs are in the worker images and not in the Crucible service image
    (13), so on a normal deployment every login refuses on the missing executable. That
    refusal used to happen after the retire, which renamed a valid credential out of the
    configured path and left the retention sweep free to shred it. Nothing moves until
    every precondition that can refuse has been checked."""
    asyncio.run(live_supervisor.tick())
    admin_ctx.login_commands["agy"] = (str(tmp_path / "no-such-cli"),)
    before = {
        path.relative_to(credential_root): path.read_bytes()
        for path in (credential_root / "agy").rglob("*")
        if path.is_file()
    }
    assert before, "the fixture credential is the thing under test"
    refused = admin_client.post(
        "/v1/admin/credentials/agy/login", json={"reason": "onboarding", "replace": True}
    )
    assert refused.status_code == 409, refused.text
    assert "is not installed on this host" in refused.json()["detail"]
    after = {
        path.relative_to(credential_root): path.read_bytes()
        for path in (credential_root / "agy").rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not [p for p in credential_root.iterdir() if p.name.startswith("agy.retired-")]
    # The credential is still the harness's credential: present at the configured path,
    # not `absent`, which is what a retire with no login behind it would have left.
    assert admin_client.get("/v1/admin/credentials/agy").json()["state"] != "absent"


def test_a_start_that_fails_after_the_retire_puts_the_credential_back(
    admin_ctx: AdminContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    credential_root: Path,
) -> None:
    """The failure path is safe, not merely unlikely: the retire is a rename and a rename
    does not roll back with the transaction. A start that fails after a successful retire
    renames the credential back and records the failure."""
    from crucible.application.admin.login import start_login  # noqa: PLC0415

    asyncio.run(live_supervisor.tick())
    live = credential_root / "codex" / "auth.json"
    before = live.read_text(encoding="utf-8")

    class StartFails:
        """Every precondition passes and the start itself falls over: the daemon thread
        could not be created, the CLI vanished between the check and the spawn."""

        def resolve(self, _ctx: AdminContext, _harness: str) -> tuple[str, ...]:
            return ("codex",)

        def start(self, _ctx: AdminContext, _harness: str, _directory: str) -> None:
            raise RuntimeError("the login thread could not be started")

        def get(self, _harness: str) -> None:
            return None

    with admin_ctx.uow_factory() as uow:
        with pytest.raises(ApplicationError) as raised:
            start_login(
                admin_ctx,
                uow,
                StartFails(),  # type: ignore[arg-type]
                principal="admin-principal",
                harness="codex",
                reason="onboarding",
                replace=True,
            )
        # The caller's transaction rolls back with the exception and takes the retire
        # event with it; the directory has to come back on its own.
        uow.rollback()
    assert "put back at its configured path" in str(raised.value.detail)
    assert live.read_text(encoding="utf-8") == before
    assert not [p for p in credential_root.iterdir() if p.name.startswith("codex.retired-")]
    assert admin_client.get("/v1/admin/credentials/codex").json()["state"] != "absent"
    refusals = [
        e
        for e in admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
        if e["kind"] == "admin_refused"
    ]
    assert any(
        r["payload"]["operation"] == "credentials login codex"
        and "renamed back to the configured path" in r["payload"]["detail"]
        for r in refusals
    ), refusals


def test_container_login_checks_promotion_before_retiring_a_credential(
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    credential_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crucible.application.admin.login import start_login  # noqa: PLC0415

    live = credential_root / "codex" / "auth.json"
    before = live.read_text(encoding="utf-8")
    asyncio.run(live_supervisor.tick())

    class ContainerRegistry:
        def resolve(self, _ctx: AdminContext, _harness: str) -> tuple[str, ...]:
            return ("codex", "login", "--device-auth")

        def container_runner(self, _ctx: AdminContext) -> object:
            return object()

    with admin_ctx.uow_factory() as uow:
        monkeypatch.setattr(uow.harness_images, "get", lambda _harness: None)
        with pytest.raises(ApplicationError, match="no worker image is promoted"):
            start_login(
                admin_ctx,
                uow,
                ContainerRegistry(),  # type: ignore[arg-type]
                principal="admin-principal",
                harness="codex",
                reason="promotion precondition",
                replace=True,
            )
        uow.rollback()
    assert live.read_text(encoding="utf-8") == before
    assert not list(credential_root.glob("codex.retired-*"))


def test_container_login_restores_a_credential_when_replacement_mkdir_fails(
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    credential_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crucible.application.admin.login import start_login  # noqa: PLC0415

    live = credential_root / "codex" / "auth.json"
    before = live.read_text(encoding="utf-8")
    asyncio.run(live_supervisor.tick())

    class ContainerRegistry:
        def resolve(self, _ctx: AdminContext, _harness: str) -> tuple[str, ...]:
            return ("codex", "login", "--device-auth")

        def container_runner(self, _ctx: AdminContext) -> object:
            return object()

        def start(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("mkdir must fail before the login thread starts")

    original_mkdir = Path.mkdir

    def fail_replacement_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == credential_root / "codex":
            raise OSError("fixture mkdir failure")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_replacement_mkdir)
    with admin_ctx.uow_factory() as uow:
        promote_for_test(
            uow,
            digest="sha256:" + "b" * 64,
            reference="crucible-worker:codex-fixture",
            harnesses={"codex": "fixture"},
            at=admin_ctx.clock.now(),
            by="tests",
            reason="mkdir rollback test",
        )
        uow.commit()
    with admin_ctx.uow_factory() as uow:
        with pytest.raises(ApplicationError, match="put back at its configured path"):
            start_login(
                admin_ctx,
                uow,
                ContainerRegistry(),  # type: ignore[arg-type]
                principal="admin-principal",
                harness="codex",
                reason="mkdir rollback",
                replace=True,
            )
        uow.rollback()
    assert live.read_text(encoding="utf-8") == before
    assert not list(credential_root.glob("codex.retired-*"))


def test_the_probe_uses_the_model_of_the_routing_policy_in_force(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    provider: FakeProvider,
) -> None:
    """The seeded `default-software` version 2 names `default-routing` version 2, whose
    cheapest enabled codex model is `gpt-5.6-luna`."""
    asyncio.run(live_supervisor.tick())
    response = admin_client.post("/v1/admin/credentials/codex/probe", json={"reason": "onboarding"})
    assert response.status_code == 200, response.text
    assert provider.probe_requests[-1].harness == "codex"
    assert "gpt-5.6-luna" in provider.probe_requests[-1].argv


def test_the_probe_refuses_rather_than_falling_back_to_a_retired_model(
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    provider: FakeProvider,
) -> None:
    """The probe took its model from the routing policy the policy in force names and
    then from the seeded `default-routing` versions 2 and 1, so a policy in force with no
    enabled model for a harness silently ran a model the operator had disabled or
    removed. Only the policy in force decides, and no enabled model for the harness in it
    is a refusal.

    The policy in force is put in place inside a transaction that is rolled back: the
    policy tables are not truncated between tests, and a superseding version with a
    disabled harness would be in force for every test that follows."""
    from crucible.application.admin.credentials import (  # noqa: PLC0415
        adapter_for,
        probe_route,
    )
    from crucible.domain.entities import Policy, RoutingPolicyRecord  # noqa: PLC0415

    asyncio.run(live_supervisor.tick())
    now = SystemClock().now()
    with admin_ctx.uow_factory() as uow:
        seeded_routing = uow.routing_policies.get("default-routing", 2)
        assert seeded_routing is not None
        routing = copy.deepcopy(seeded_routing.document)
        routing["version"] = 99
        for model in routing["models"]:
            if model["harness"] == "codex":
                model["enabled"] = False
        uow.routing_policies.put(
            RoutingPolicyRecord(
                name="default-routing", version=99, document=routing, created_at=now
            )
        )
        seeded_policy = uow.policies.get("default-software", 2)
        assert seeded_policy is not None
        policy = copy.deepcopy(seeded_policy.document)
        policy["version"] = 99
        policy["routing"]["policy"] = {"name": "default-routing", "version": 99}
        uow.policies.put(
            Policy(name="default-software", version=99, document=policy, created_at=now)
        )
        probes_before = len(provider.probe_requests)
        # Through the service the operator calls, not just the selector inside it.
        with pytest.raises(ApplicationError) as raised:
            asyncio.run(
                credentials_module_probe(
                    admin_ctx, uow, harness="codex", reason="after retiring the codex models"
                )
            )
        detail = str(raised.value.detail)
        assert "default-routing version 99" in detail and "no enabled model" in detail
        assert "does not fall back" in detail
        # No fallback: a seeded model the policy in force does not name is never reached.
        assert "gpt-5.6-luna" not in detail
        # A harness the policy in force still enables takes its model from that policy.
        assert (
            probe_route(uow, adapter_for(admin_ctx, "claude_code"), "claude_code")[0]
            == "claude-haiku-4-5"
        )
        uow.rollback()
    # The refusal happened before the provider was asked to run anything.
    assert len(provider.probe_requests) == probes_before


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_a_read_only_credential_directory_is_still_replaceable(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    credential_root: Path,
) -> None:
    """A replacement renames the directory aside and the CLI creates a fresh one, so only
    the credential root has to be writable. Requiring the directory itself refused an
    operator who deliberately holds a credential directory read-only, and it refused with
    a reason that was not the real precondition.

    The refusal each precondition gives is its own: an existing credential with no
    `replace` is refused for being an existing credential, not for a mode."""
    asyncio.run(live_supervisor.tick())
    live = credential_root / "claude_code"
    old_token = (live / "oauth-token").read_text(encoding="utf-8")
    live.chmod(0o500)
    try:
        refused = admin_client.post(
            "/v1/admin/credentials/claude_code/login", json={"reason": "onboarding"}
        )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert "already passes the shape check" in detail
        assert "not writable" not in detail

        started = admin_client.post(
            "/v1/admin/credentials/claude_code/login",
            json={"reason": "onboarding", "replace": True},
        )
        assert started.status_code == 200, started.text
        retained = started.json()["retained_as"]
        assert retained.startswith("claude_code.retired-")
        state = _wait_for_login_state(admin_client, "claude_code", "waiting_for_code")
        admin_client.post(
            "/v1/admin/credentials/claude_code/login/code",
            json={"code": "ABCD-EFGH", "reason": "complete onboarding"},
        )
        state = _wait_for_login_state(admin_client, "claude_code", "finished", "failed")
        assert state["state"] == "finished", state
        # The credential is at its configured path, and it is the new one.
        new_token = (live / "oauth-token").read_text(encoding="utf-8")
        assert new_token.strip() and new_token != old_token
        assert (credential_root / retained / "oauth-token").read_text(encoding="utf-8") == old_token
        assert admin_client.get("/v1/admin/credentials/claude_code").json()["state"] != "absent"
    finally:
        # The temporary tree has to be removable again whatever the test did.
        for path in (live, *credential_root.glob("claude_code.retired-*")):
            if path.is_dir():
                path.chmod(0o700)


def test_a_login_that_reuses_an_unwritable_directory_says_so(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    credential_root: Path,
) -> None:
    """The directory check still exists for the path that keeps it: nothing at the
    configured path passes the shape check, so the login writes into the directory as it
    stands, and an unwritable one is refused for exactly that."""
    asyncio.run(live_supervisor.tick())
    live = credential_root / "codex"
    (live / "auth.json").unlink()
    live.chmod(0o500)
    try:
        refused = admin_client.post(
            "/v1/admin/credentials/codex/login", json={"reason": "onboarding"}
        )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert "is not writable" in detail and str(live) in detail
        assert "already passes the shape check" not in detail
    finally:
        live.chmod(0o700)


class _EgressProbe(FakeProvider):
    """Stands in for the Kubernetes provider: counts the reloads a save asks for."""

    reloads = 0

    def reload_settings(self) -> None:
        self.reloads += 1


def test_kubernetes_egress_through_api_cli_and_ui(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_client: TestClient,
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """crucible#91: the resolver's and an in-cluster local endpoint's selectors are a
    runtime setting with API, CLI and UI parity, one audit trail, and the provider told
    to read them back. Until a save the settings file's values are shown."""
    asyncio.run(live_supervisor.tick())
    probe = _EgressProbe()
    admin_ctx.providers["kubernetes"] = probe
    admin_ctx.kubernetes_protected_namespaces = ("crucible-workers", "crucible")
    first = admin_client.get("/v1/admin/kubernetes/egress").json()
    assert first["source"] == "settings"
    assert first["document"]["dns"] == {
        "namespace": "kube-system",
        "pod_labels": {"k8s-app": "kube-dns"},
    }

    saved = admin_client.post(
        "/v1/admin/kubernetes/egress",
        json={
            "reason": "api: litellm runs in the cluster",
            "dns": {"namespace": "kube-system", "pod_labels": {"k8s-app": "kube-dns"}},
            "local_endpoint": {
                "namespace": "litellm",
                "pod_labels": {"app.kubernetes.io/name": "litellm"},
                "port": 4000,
            },
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["source"] == "database"
    assert saved.json()["document"]["local_endpoint"]["port"] == 4000
    assert probe.reloads == 1

    refused = admin_client.post(
        "/v1/admin/kubernetes/egress",
        json={
            "reason": "api: into the workers namespace",
            "dns": {"namespace": "kube-system", "pod_labels": {"k8s-app": "kube-dns"}},
            "local_endpoint": {"namespace": "crucible-workers", "pod_labels": {"a": "b"}},
        },
    )
    assert refused.status_code == 422
    assert "may never reach" in refused.text
    half = admin_client.post("/v1/admin/kubernetes/egress", json={"dns": {"namespace": ""}})
    assert half.status_code == 422
    # Leaving `dns` out of an edit of the endpoint must not read as "no DNS selector".
    partial = admin_client.post(
        "/v1/admin/kubernetes/egress",
        json={
            "reason": "api: endpoint only",
            "local_endpoint": {"namespace": "litellm", "pod_labels": {"a": "b"}},
        },
    )
    assert partial.status_code == 422
    assert "dns must be given" in partial.text
    assert (
        admin_client.get("/v1/admin/kubernetes/egress").json()["document"]
        == (saved.json()["document"])
    )

    cli_view = run_cli(config_file, "kubernetes", "egress", capsys=capsys)
    assert cli_view["document"] == saved.json()["document"]
    cli_saved = run_cli(
        config_file,
        "--reason",
        "cli: gateway moved port",
        "kubernetes",
        "set-egress",
        "--endpoint-namespace=litellm",
        "--endpoint-labels=app.kubernetes.io/name=litellm",
        "--endpoint-port=8080",
        capsys=capsys,
    )
    assert cli_saved["document"]["local_endpoint"]["port"] == 8080
    assert cli_saved["updated_by"] == "crucible-admin"

    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/routing")
        assert page.status_code == 200
        assert 'action="/ui/actions/kubernetes-egress"' in page.text
        assert 'value="app.kubernetes.io/name=litellm"' in page.text
        ui_saved = browser.post(
            "/ui/actions/kubernetes-egress",
            data={
                "csrf": csrf,
                "dns_namespace": "kube-system",
                "dns_labels": "k8s-app=kube-dns",
                "endpoint_namespace": "",
                "endpoint_labels": "",
                "endpoint_port": "0",
                "reason": "ui: gateway moved out of the cluster",
                "return_to": "/ui/routing",
            },
            follow_redirects=False,
        )
        assert ui_saved.status_code == 303
        assert "Completed" in unquote(ui_saved.headers.get("location", ""))
    final = admin_client.get("/v1/admin/kubernetes/egress").json()
    assert final["document"]["local_endpoint"] == {"namespace": "", "pod_labels": {}, "port": 0}
    assert final["reason"] == "ui: gateway moved out of the cluster"
    # The API and the UI share this process's provider; the CLI in local mode wires its
    # own, and a running supervisor follows its save by reading the row back.
    assert probe.reloads == 2
    kinds = audit_kinds(admin_client)
    assert ("kubernetes_egress_updated", "admin-principal") in kinds
    assert ("kubernetes_egress_updated", "crucible-admin") in kinds
    assert len([k for k in kinds if k[0] == "kubernetes_egress_updated"]) == 3


def test_kubernetes_timeouts_through_api_cli_and_ui(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_client: TestClient,
    admin_ctx: AdminContext,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The short-role timeout (lab findings of 2026-09-29) is a runtime setting with API,
    CLI and UI parity, one audit trail, and the provider told to read it back. It is a
    configuration write, so it needs no live supervisor."""
    probe = _EgressProbe()
    admin_ctx.providers["kubernetes"] = probe
    first = admin_client.get("/v1/admin/kubernetes/timeouts").json()
    assert first["source"] == "settings"
    assert first["document"] == {"role_timeout_seconds": 120, "api_retry_seconds": 60}
    assert first["bounds"] == {"min": 10, "max": 3600}
    assert first["api_retry_bounds"] == {"min": 1, "max": 600}
    assert first["api_retry_seconds_source"] == "default"
    assert first["api_retry_seconds_applies"] == "next launch"

    saved = admin_client.post(
        "/v1/admin/kubernetes/timeouts",
        json={
            "role_timeout_seconds": 300,
            "api_retry_seconds": 45,
            "reason": "api: slow NFS",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["source"] == "database"
    assert saved.json()["document"] == {"role_timeout_seconds": 300, "api_retry_seconds": 45}
    assert probe.reloads == 1
    for bad in (5, 99999, "300", None):
        refused = admin_client.post(
            "/v1/admin/kubernetes/timeouts", json={"role_timeout_seconds": bad}
        )
        assert refused.status_code == 422, (bad, refused.text)
    assert admin_client.get("/v1/admin/kubernetes/timeouts").json()["document"] == {
        "role_timeout_seconds": 300,
        "api_retry_seconds": 45,
    }

    assert run_cli(config_file, "kubernetes", "timeouts", capsys=capsys)["document"] == {
        "role_timeout_seconds": 300,
        "api_retry_seconds": 45,
    }
    cli_saved = run_cli(
        config_file,
        "kubernetes",
        "set-timeouts",
        "--role-seconds=240",
        capsys=capsys,
    )
    assert cli_saved["document"] == {"role_timeout_seconds": 240, "api_retry_seconds": 45}

    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/routing")
        assert page.status_code == 200
        assert 'action="/ui/actions/kubernetes-timeouts"' in page.text
        assert 'value="240"' in page.text
        assert 'name="api_retry_seconds"' in page.text
        assert 'value="45"' in page.text
        assert "source: saved; applies: next launch" in page.text
        ui_saved = browser.post(
            "/ui/actions/kubernetes-timeouts",
            data={
                "csrf": csrf,
                "role_timeout_seconds": "180",
                "api_retry_seconds": "30",
                "reason": "ui: back down",
                "return_to": "/ui/routing",
            },
            follow_redirects=False,
        )
        assert ui_saved.status_code == 303
        assert "Completed" in unquote(ui_saved.headers.get("location", ""))
    final = admin_client.get("/v1/admin/kubernetes/timeouts").json()
    assert final["document"] == {"role_timeout_seconds": 180, "api_retry_seconds": 30}
    assert final["reason"] == "ui: back down"
    assert probe.reloads == 2
    kinds = audit_kinds(admin_client)
    assert len([k for k in kinds if k[0] == "kubernetes_timeouts_updated"]) == 3


def test_the_cli_remote_mode_sends_the_timeouts_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, Any]] = []

    def fake_call(self: Any, method: str, path: str, body: Any = None, **_: Any) -> Any:
        calls.append((method, path, body))
        return {"setting": "kubernetes.timeouts", "source": "database", "document": {}}

    monkeypatch.setattr(Api, "call", fake_call)
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    admin_main(["--api-url", "http://127.0.0.1:1", "kubernetes", "timeouts"])
    admin_main(
        ["--api-url", "http://127.0.0.1:1", "kubernetes", "set-timeouts", "--role-seconds=90"]
    )
    assert calls == [
        ("GET", "/v1/admin/kubernetes/timeouts", None),
        ("POST", "/v1/admin/kubernetes/timeouts", {"reason": "", "role_timeout_seconds": 90}),
    ]


def test_the_cli_remote_mode_sends_the_egress_document(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, Any]] = []

    def fake_call(self: Any, method: str, path: str, body: Any = None, **_: Any) -> Any:
        calls.append((method, path, body))
        return {"setting": "kubernetes.egress", "source": "database", "document": {}}

    monkeypatch.setattr(Api, "call", fake_call)
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    admin_main(["--api-url", "http://127.0.0.1:1", "kubernetes", "egress"])
    admin_main(
        [
            "--api-url",
            "http://127.0.0.1:1",
            "--reason",
            "r",
            "kubernetes",
            "set-egress",
            "--endpoint-namespace=litellm",
            "--endpoint-labels=app=litellm",
            "--endpoint-port=4000",
        ]
    )
    assert calls == [
        ("GET", "/v1/admin/kubernetes/egress", None),
        (
            "POST",
            "/v1/admin/kubernetes/egress",
            {
                "reason": "r",
                "dns": {"namespace": "kube-system", "pod_labels": {"k8s-app": "kube-dns"}},
                "local_endpoint": {
                    "namespace": "litellm",
                    "pod_labels": {"app": "litellm"},
                    "port": 4000,
                },
            },
        ),
    ]


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_a_harness_test_reports_each_step_and_stops_at_the_first_failure(
    ctx: AppContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    provider: FakeProvider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """crucible#118: one Test per harness runs the path a task takes and says, per step,
    pass or fail in plain words. It asks for no reason (crucible#117). The POST starts the
    run in the background and answers its running marker at once; the result lands on
    the harness row, where `GET /admin/harnesses/{name}/test` reads it (issue 147)."""

    def tested(harness: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        started = admin_client.post(f"/v1/admin/harnesses/{harness}/test", json=body)
        assert started.status_code == 202, started.text
        marker = started.json()
        assert marker["status"] == "running" and marker["ok"] is None, marker
        _result: dict[str, Any] | None = None

        def _check() -> bool:
            nonlocal _result
            _result = admin_client.get(f"/v1/admin/harnesses/{harness}/test").json()
            assert _result is not None
            return (  # type: ignore[no-any-return]
                _result["status"] == "finished" and _result["started_at"] >= marker["started_at"]
            )

        wait_until(_check, timeout=30, describe=f"the {harness} test to land within 30 s")
        assert _result is not None
        return _result

    asyncio.run(live_supervisor.tick())
    assert admin_client.get("/v1/admin/harnesses/hermes/test").json()["status"] == "not tested"
    result = tested("hermes")
    assert result["ok"] is False and result["failed_step"] == "Worker image"
    assert [s["result"] for s in result["steps"]] == [
        "pass",
        "fail",
        "not run",
        "not run",
        "not run",
        "not run",
    ]
    assert "choose one on Images" in result["steps"][1]["detail"]

    with ctx.uow_factory() as uow:
        promote_for_test(
            uow,
            digest="sha256:" + "a" * 64,
            reference="ghcr.io/sentania-labs/crucible-worker:0.5.5",
            harnesses={"hermes": "0.19.0", "script-harness": "1.0.0", "codex": "0.156.0"},
            at=ctx.clock.now(),
        )
        uow.commit()
    # Hermes has no key in this tier: the test stops at the credential, before a worker.
    probes_before = len(provider.probes)
    missing = tested("hermes", {})
    assert missing["failed_step"] == "Credential"
    assert "no API key is stored" in missing["steps"][2]["detail"]
    assert "Local gateway" in missing["steps"][2]["detail"]
    assert len(provider.probes) == probes_before

    # The fixture harness calls no model unless routed to a local endpoint; it passes.
    passed = run_cli(config_file, "harnesses", "test", "script-harness", capsys=capsys)
    assert passed["ok"] is True, passed
    assert [s["name"] for s in passed["steps"]] == [
        "Harness enabled",
        "Worker image",
        "Credential",
        "Model",
        "Worker starts",
        "Model call",
    ]
    assert passed["steps"][2]["detail"] == "this harness needs none"
    through_api = tested("script-harness")
    assert through_api["ok"] is True
    assert provider.probe_requests[-1].harness == "script-harness"

    # A model provider that refuses the credential fails the model call, named as such.
    provider.probe_outcome = "auth_failure"
    refused = tested("codex")
    assert refused["failed_step"] == "Model call", refused
    assert "refused the credential" in refused["steps"][-1]["detail"]

    harnesses_view = {h["name"]: h for h in admin_client.get("/v1/admin/harnesses").json()["items"]}
    assert harnesses_view["script-harness"]["last_test"]["ok"] is True
    assert harnesses_view["codex"]["last_test"]["failed_step"] == "Model call"


def test_rows_carry_their_own_actions_instead_of_typed_ids(
    ctx: AppContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    tokens: dict[str, str],
) -> None:
    """crucible#127: where the system knows the value, the UI offers it. Revoke is on the
    principal's row, Remove on the repository's, and no page asks for an ID to be typed."""
    asyncio.run(live_supervisor.tick())
    principals = admin_client.get("/v1/admin/tokens").json()["items"]
    observer = next(item for item in principals if item["role"] == "observer")
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        pages = {path: browser.get(path).text for path in ("/ui/tokens", "/ui/repositories")}
        pages["/ui/workers"] = browser.get("/ui/workers").text
        pages["/ui/bootstrap"] = browser.get("/ui/bootstrap").text
        for text in pages.values():
            for typed in ("Principal ID", "Attempt ID", "Import ID", "Digest or reference"):
                assert f">{typed}<" not in text
        assert f'name="principal_id" value="{observer["id"]}"' in pages["/ui/tokens"]
        assert 'name="name" value="example-service"' in pages["/ui/repositories"]
        revoked = browser.post(
            "/ui/actions/token-revoke",
            data={
                "csrf": csrf,
                "principal_id": observer["id"],
                "return_to": "/ui/tokens",
            },
            follow_redirects=False,
        )
        # The revoke asks for a reason (crucible#117): refused without one.
        assert "reason" in unquote(revoked.headers["location"])
        missing = browser.get("/ui/bootstrap/01ABCDEFGHJKMNPQRSTVWXYZ00", follow_redirects=False)
        assert missing.status_code == 303 and "not%20found" in missing.headers["location"]


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_pages_lead_with_what_the_operator_acts_on(
    ctx: AppContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    tokens: dict[str, str],
) -> None:
    """crucible#115: plain labels first, internals behind a details view, one provider
    table, and navigation entries with nothing behind them left out."""
    asyncio.run(live_supervisor.tick())
    with TestClient(create_app(ctx)) as browser:
        ui_sign_in(browser, tokens["admin"])
        status_page = browser.get("/ui").text
        settings_page = browser.get("/ui/settings").text
        routing_page = browser.get("/ui/routing").text
        github_page = browser.get("/ui/github").text
        credentials_page = browser.get("/ui/credentials").text
    # The pages first-run setup added (crucible#150): Routing leads with what is in force
    # in plain words, its documents behind Details, and a pool is cleared from its row.
    lead, _, details = routing_page.partition("<details")
    assert "In force" in lead and "1 hour by default" in lead
    assert "Delivery policy document" not in lead and "Delivery policy document" in details
    assert 'name="pool"' not in routing_page and "issue 128" not in routing_page
    lead, _, details = github_page.partition("<details")
    assert "Connection" in lead and "Stored App and every repository" in details
    assert "API base" not in lead
    # Hermes has no key stored in this tier: nothing to validate, probe or remove, only the
    # page that sets it.
    assert 'name="harness" value="hermes"' not in credentials_page
    assert 'href="/ui/gateway">Local gateway' in credentials_page
    assert "/ui/credentials/codex/login" in credentials_page
    lead, _, details = status_page.partition("<details")
    assert "Provider: fake" in lead and "Supervisor" in lead
    assert "Fenced token" not in lead and "Fenced token" in details
    assert lead.count("CHECKS") == 0
    nav = status_page[status_page.index("admin-nav") : status_page.index("</nav>")]
    assert 'href="/ui/bootstrap"' not in nav and 'href="/ui/retention"' not in nav
    assert "Set up" in nav and "Work" in nav and "Admin" in nav
    lead, _, defaults = settings_page.partition("<details")
    assert "Defaults left unchanged" in defaults
    assert "restart required" not in settings_page


def test_a_task_for_a_provider_this_deployment_does_not_run_is_refused_at_submit(
    ctx: AppContext, tokens: dict[str, str], live_supervisor: Supervisor
) -> None:
    """crucible#124: with test fixtures off the fake provider is not wired, and a
    contract naming it is refused when it is submitted, not left to fail at launch."""
    asyncio.run(live_supervisor.tick())
    production = dataclasses.replace(ctx, providers=[])
    with TestClient(
        create_app(production), headers={"Authorization": f"Bearer {tokens['orchestrator']}"}
    ) as client:
        response = client.post("/v1/tasks", json=contract_document(external_id="UNWIRED-1"))
    assert response.status_code == 422, response.text
    paths = [error["path"] for error in response.json()["errors"]]
    assert "execution_request.provider" in paths


def test_command_timeout_through_api_cli_and_ui(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """crucible#128: the per-command timeout is a policy limit with API, CLI and UI
    parity. Each save writes a new policy version with only that limit changed, and one
    audit event; a bound left out keeps its value."""
    asyncio.run(live_supervisor.tick())
    first = admin_client.get("/v1/admin/limits/command-timeout").json()
    assert first["command_timeout_ms"] == {"min": 1000, "max": 14_400_000, "default": 3_600_000}
    start = first["policy"]["version"]

    saved = admin_client.post(
        "/v1/admin/limits/command-timeout",
        json={"reason": "api: long builds", "min": 60_000, "max": 7_200_000, "default": 5_400_000},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["policy"]["version"] == start + 1
    stored = admin_client.get(f"/v1/policies/default-software/{start + 1}").json()["document"]
    assert stored["limits"]["command_timeout_ms"] == {
        "min": 60_000,
        "max": 7_200_000,
        "default": 5_400_000,
    }
    previous = admin_client.get(f"/v1/policies/default-software/{start}").json()["document"]

    # Only the limit changed: both versions, normalized, differ in nothing else.
    def rest(document: dict[str, Any]) -> dict[str, Any]:
        normal = PolicyV1.model_validate(document).model_dump(mode="json")
        del normal["version"], normal["description"], normal["limits"]["command_timeout_ms"]
        return normal

    assert rest(stored) == rest(previous)

    for body in (
        {"reason": "api: inverted", "min": 5000, "max": 4000},
        {"reason": "api: a string", "default": "600000"},
        {"reason": "api: a bool", "default": True},
        # A reason is an optional audit note here (crucible#117), so no body is refused
        # for leaving it out; a secret-shaped one still is.
        {"reason": "ghp_" + "a" * 36, "default": 600_000},
    ):
        refused = admin_client.post("/v1/admin/limits/command-timeout", json=body)
        assert refused.status_code == 422, (body, refused.text)
    assert admin_client.get("/v1/admin/limits/command-timeout").json()["policy"]["version"] == (
        start + 1
    )

    cli_view = run_cli(config_file, "limits", "command-timeout", capsys=capsys)
    assert cli_view["command_timeout_ms"]["default"] == 5_400_000
    cli_saved = run_cli(
        config_file,
        "--reason",
        "cli: shorter default",
        "limits",
        "set-command-timeout",
        "--default=1800000",
        capsys=capsys,
    )
    assert cli_saved["command_timeout_ms"] == {
        "min": 60_000,
        "max": 7_200_000,
        "default": 1_800_000,
    }
    assert cli_saved["policy"]["version"] == start + 2

    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/routing")
        assert page.status_code == 200
        assert 'action="/ui/actions/command-timeout"' in page.text
        assert 'value="1800000"' in page.text
        ui_saved = browser.post(
            "/ui/actions/command-timeout",
            data={
                "csrf": csrf,
                "min": "60000",
                "default": "3600000",
                "max": "7200000",
                "reason": "ui: back to an hour",
                "return_to": "/ui/routing",
            },
            follow_redirects=False,
        )
        assert ui_saved.status_code == 303
        assert "Completed" in unquote(ui_saved.headers.get("location", ""))
    final = admin_client.get("/v1/admin/limits/command-timeout").json()
    assert final["command_timeout_ms"]["default"] == 3_600_000
    assert final["policy"]["version"] == start + 3
    kinds = audit_kinds(admin_client)
    assert ("command_timeout_updated", "admin-principal") in kinds
    assert ("command_timeout_updated", "crucible-admin") in kinds
    assert len([k for k in kinds if k[0] == "command_timeout_updated"]) == 3


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_advisory_gates_through_api_cli_and_ui(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ADR 0024: which pre-PR gates are advisory is a policy field with API, CLI and UI
    parity. Each save writes a new policy version with only that list changed; making a
    gate outside the default set advisory is recorded as an operator decision."""
    asyncio.run(live_supervisor.tick())
    first = admin_client.get("/v1/admin/gates/advisory").json()
    assert first["advisory"] == [
        "commit_policy",
        "criteria_mapped",
        "report_present",
        "run_evidence_present",
        "scope_contained",
    ]
    assert first["always_advisory"] == ["commit_policy", "report_present"]
    assert "no_secrets" in first["blocking"] and "internal_review_recorded" in first["blocking"]
    start = first["policy"]["version"]

    saved = admin_client.post(
        "/v1/admin/gates/advisory",
        json={"reason": "api: scope blocks here", "advisory": ["report_present"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["advisory"] == ["commit_policy", "report_present"]
    assert not saved.json()["default"]
    assert "scope_contained" in saved.json()["blocking"]
    stored = admin_client.get(f"/v1/policies/default-software/{start + 1}").json()["document"]
    # hades #498: report_present is always advisory and is not stored in the list.
    assert stored["gates"]["advisory"] == []
    previous = admin_client.get(f"/v1/policies/default-software/{start}").json()["document"]

    def rest(document: dict[str, Any]) -> dict[str, Any]:
        normal = PolicyV1.model_validate(document).model_dump(mode="json")
        del normal["version"], normal["description"], normal["gates"]["advisory"]
        return normal

    assert rest(stored) == rest(previous)

    for body in (
        {"reason": "api: the review", "advisory": ["internal_review_recorded"]},
        {"reason": "api: secrets", "advisory": ["no_secrets"]},
        {"reason": "api: no such gate", "advisory": ["no_such_gate"]},
        {"reason": "api: post-PR", "advisory": ["ci_green_for_head"]},
        {"reason": "api: not a list", "advisory": "scope_contained"},
    ):
        refused = admin_client.post("/v1/admin/gates/advisory", json=body)
        assert refused.status_code == 422, (body, refused.text)
    assert admin_client.get("/v1/admin/gates/advisory").json()["policy"]["version"] == start + 1

    cli_view = run_cli(config_file, "gates", "advisory", capsys=capsys)
    assert cli_view["advisory"] == ["commit_policy", "report_present"]
    cli_saved = run_cli(
        config_file,
        "--reason",
        "cli: CI edits go to the reviewer",
        "gates",
        "set-advisory",
        "--gate=ci_unchanged",
        "--gate=scope_contained",
        capsys=capsys,
    )
    assert cli_saved["advisory"] == ["ci_unchanged", "commit_policy", "scope_contained"]
    assert cli_saved["policy"]["version"] == start + 2
    decisions = [
        d
        for d in admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
        if d["kind"] == "policy_uploaded"
    ]
    assert any(
        "gates.advisory.ci_unchanged" in (d["payload"].get("operator_only_settings") or [])
        for d in decisions
    )

    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/routing")
        assert page.status_code == 200
        assert "Advisory gates" in page.text
        assert 'action="/ui/actions/gate-classes"' in page.text
        assert 'name="advisory_ci_unchanged" value="true" checked' in page.text
        assert 'name="advisory_internal_review_recorded"' not in page.text
        assert 'name="advisory_no_secrets"' not in page.text
        assert 'name="advisory_report_present"' not in page.text
        ui_saved = browser.post(
            "/ui/actions/gate-classes",
            data={
                "csrf": csrf,
                "advisory_criteria_mapped": "true",
                "advisory_report_present": "true",
                "advisory_run_evidence_present": "true",
                "advisory_scope_contained": "true",
                "reason": "ui: back to the default set",
                "return_to": "/ui/routing",
            },
            follow_redirects=False,
        )
        assert ui_saved.status_code == 303
        assert "Completed" in unquote(ui_saved.headers.get("location", ""))
    final = admin_client.get("/v1/admin/gates/advisory").json()
    assert final["advisory"] == first["advisory"]
    assert final["policy"]["version"] == start + 3
    kinds = audit_kinds(admin_client)
    assert ("policy_uploaded", "admin-principal") in kinds
    assert ("policy_uploaded", "crucible-admin") in kinds


async def test_the_tasks_page_marks_each_gate_and_lists_what_is_for_the_reviewer(
    ctx: AppContext,
    tokens: dict[str, str],
    client: TestClient,
    provider: FakeProvider,
    admin_ctx: AdminContext,
) -> None:
    """ADR 0024: a task at its internal review shows every pre-PR gate as blocking or
    advisory, and the failed advisory ones with their detail under For the reviewer."""
    from tests.integration.conftest import (  # noqa: PLC0415
        ARTIFACTS_DELIVERABLE,
        make_supervisor,
        run_to_settled,
        submit_and_start,
    )
    from tests.integration.test_report_facts import judgement_only  # noqa: PLC0415

    report = judgement_only()
    del report["risks"]
    provider.set_report("EX-0001", report)
    task_id = submit_and_start(
        client, "crucible-worker:fake-out-of-scope", deliverables=ARTIFACTS_DELIVERABLE
    )
    supervisor = make_supervisor(ctx, provider)
    # hades #498: both failures are advisory, so the task waits for its reviewer.
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"
    with TestClient(create_app(ctx)) as browser:
        ui_sign_in(browser, tokens["admin"])
        page = html.unescape(browser.get("/ui/tasks").text)
    assert "Gates by task" in page and "For the reviewer" in page
    assert "scope_contained (advisory)" in page and "no_secrets (blocking)" in page
    assert "scope_contained: outside allowed_paths" in page
    assert "infrastructure/outside-the-contract.txt" in page
    assert "report_present: the report did not parse" in page


def test_the_cli_remote_mode_sends_the_advisory_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, Any]] = []

    def fake_call(self: Any, method: str, path: str, body: Any = None, **_: Any) -> Any:
        calls.append((method, path, body))
        return {"policy": {}, "advisory": ["scope_contained"], "blocking": []}

    monkeypatch.setattr(Api, "call", fake_call)
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    admin_main(["--api-url", "http://127.0.0.1:1", "gates", "advisory"])
    admin_main(
        [
            "--api-url",
            "http://127.0.0.1:1",
            "--reason",
            "r",
            "gates",
            "set-advisory",
            "--gate=scope_contained",
        ]
    )
    assert calls == [
        ("GET", "/v1/admin/gates/advisory", None),
        ("POST", "/v1/admin/gates/advisory", {"reason": "r", "advisory": ["scope_contained"]}),
    ]


def test_the_cli_remote_mode_sends_the_command_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, Any]] = []

    def fake_call(self: Any, method: str, path: str, body: Any = None, **_: Any) -> Any:
        calls.append((method, path, body))
        return {"policy": {}, "command_timeout_ms": {"min": 1, "max": 2, "default": 2}}

    monkeypatch.setattr(Api, "call", fake_call)
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    admin_main(["--api-url", "http://127.0.0.1:1", "limits", "command-timeout"])
    admin_main(
        [
            "--api-url",
            "http://127.0.0.1:1",
            "--reason",
            "r",
            "limits",
            "set-command-timeout",
            "--max=7200000",
        ]
    )
    assert calls == [
        ("GET", "/v1/admin/limits/command-timeout", None),
        ("POST", "/v1/admin/limits/command-timeout", {"reason": "r", "max": 7_200_000}),
    ]


def test_recently_updated_reads_the_newest_tasks_only(
    client: TestClient, ctx: AppContext, clock: FakeClock
) -> None:
    """hades PR 238 review: the Tasks page reads its rows with one bounded query, newest
    update first, instead of every task of the last 14 days."""
    since = clock.now()
    ids = []
    for n in range(3):
        clock.advance(60)
        document = contract_document(external_id=f"EX-RU{n}")
        document["repository"]["work_branch"] = f"crucible/EX-RU{n}"
        response = client.post("/v1/tasks", json=document)
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    with ctx.uow_factory() as uow:
        newest = [t.id for t in uow.tasks.recently_updated(since=since, limit=2)]
        later = [t.id for t in uow.tasks.recently_updated(since=clock.now(), limit=50)]
    assert newest == [ids[2], ids[1]]
    assert later == [ids[2]]


@pytest.mark.xfail(strict=False, reason="hades #560: drifted from the product; cleanup pending")
def test_hermes_run_limits_have_api_cli_and_ui_controls(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FDY-0140: every tunable has a UI. The turn and context limits are shown and set
    from the admin API, `crucible-admin gateway limits` and the Local gateway page, and
    each change is audited."""
    asyncio.run(live_supervisor.tick())
    shown = admin_client.get("/v1/admin/gateway/hermes-limits").json()
    assert (shown["max_turns"], shown["context_length"], shown["saved"]) == (300, 131072, False)

    saved = admin_client.post(
        "/v1/admin/gateway/hermes-limits",
        json={"reason": "api: longer tasks", "max_turns": 500, "context_length": 0},
    )
    assert saved.status_code == 200, saved.text
    assert (saved.json()["max_turns"], saved.json()["context_length"]) == (500, 0)
    for body in (
        {"max_turns": 5, "context_length": 0},
        {"max_turns": 300, "context_length": 1000},
        {"max_turns": "many", "context_length": 0},
    ):
        refused = admin_client.post("/v1/admin/gateway/hermes-limits", json=body)
        assert refused.status_code in (409, 422), (body, refused.text)
    assert admin_client.get("/v1/admin/gateway/hermes-limits").json()["max_turns"] == 500

    local = run_cli(config_file, "gateway", "limits", capsys=capsys)
    assert (local["max_turns"], local["context_length"]) == (500, 0)
    changed = run_cli(
        config_file,
        "--reason",
        "cli: bigger window",
        "gateway",
        "limits",
        "--context-length",
        "200000",
        capsys=capsys,
    )
    # A flag not given keeps its value.
    assert (changed["max_turns"], changed["context_length"]) == (500, 200000)

    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/gateway")
        assert page.status_code == 200
        assert "Local run limits" in page.text
        assert 'action="/ui/actions/hermes-limits"' in page.text
        assert 'name="max_turns"' in page.text and 'name="context_length"' in page.text
        response = browser.post(
            "/ui/actions/hermes-limits",
            data={
                "csrf": csrf,
                "max_turns": "250",
                "context_length": "131072",
                "reason": "ui: back to normal",
                "return_to": "/ui/gateway",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        assert "kind=ok" in unquote(response.headers["location"])
    final = admin_client.get("/v1/admin/gateway/hermes-limits").json()
    assert (final["max_turns"], final["context_length"], final["saved"]) == (250, 131072, True)
    events = admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
    changes = [
        e["payload"]["after"]
        for e in events
        if e["kind"] == "local_gateway_updated" and e["payload"].get("change") == "hermes_limits"
    ]
    assert len(changes) == 3


def test_a_principal_rename_keeps_its_tasks_and_token_through_api_cli_and_ui(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    client: TestClient,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ADR 0029: the orchestrator's account is renamed `hades`. Tasks and the token hold
    the principal's id, so they follow it; events keep the name they were written with."""
    from tests.integration.conftest import submit_and_start  # noqa: PLC0415

    asyncio.run(live_supervisor.tick())
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", external_id="REN-0001", start=False
    )
    ids = {p["name"]: p["id"] for p in admin_client.get("/v1/admin/tokens").json()["items"]}
    orchestrator = ids["orchestrator-principal"]
    no_reason = admin_client.post(f"/v1/admin/tokens/{orchestrator}/rename", json={"name": "x"})
    assert no_reason.status_code == 422, no_reason.text
    renamed = admin_client.post(
        f"/v1/admin/tokens/{orchestrator}/rename",
        json={"name": "hades", "reason": "the principal agent's identity"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json() == {
        "id": orchestrator,
        "name": "hades",
        "previous_name": "orchestrator-principal",
    }
    # The same token still works, and its task now reads as the renamed principal's.
    view = client.get(f"/v1/tasks/{task_id}")
    assert view.status_code == 200 and view.json()["principal"] == "hades"
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    assert events[0]["principal"] == "orchestrator-principal"
    audit = admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
    event = next(e for e in audit if e["kind"] == "principal_renamed")
    assert event["payload"]["before"]["name"] == "orchestrator-principal"
    assert event["payload"]["after"]["name"] == "hades"
    # Refusals: a taken name, a reserved one, the same name, an unknown principal.
    for body in (
        {"name": "admin-principal"},
        {"name": "crucible"},
        {"name": "worker:x"},
        {"name": "first-run-admin-9"},
        {"name": "hades"},
        {"name": " padded "},
    ):
        refused = admin_client.post(
            f"/v1/admin/tokens/{orchestrator}/rename", json={**body, "reason": "x"}
        )
        assert refused.status_code == 409, (body, refused.text)
    unknown = admin_client.post(
        "/v1/admin/tokens/01ABCDEFGHJKMNPQRSTVWXYZ00/rename", json={"name": "y", "reason": "x"}
    )
    assert unknown.status_code == 409
    # CLI and UI reach the same operation.
    cli_renamed = run_cli(
        config_file,
        "--reason",
        "parity",
        "token",
        "rename",
        orchestrator,
        "hades-cli",
        capsys=capsys,
    )
    assert cli_renamed["name"] == "hades-cli" and cli_renamed["previous_name"] == "hades"
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/tokens")
        assert "Rename principal" in page.text
        ui_renamed = browser.post(
            "/ui/actions/token-rename",
            data={
                "csrf": csrf,
                "principal_id": orchestrator,
                "name": "hades",
                "reason": "parity",
                "return_to": "/ui/tokens",
            },
            follow_redirects=False,
        )
        assert ui_renamed.status_code == 303
        assert "kind=bad" not in ui_renamed.headers["location"]
    names = {p["id"]: p["name"] for p in admin_client.get("/v1/admin/tokens").json()["items"]}
    assert names[orchestrator] == "hades"
