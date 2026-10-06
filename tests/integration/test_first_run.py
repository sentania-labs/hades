"""The first-run setup through the admin API, the CLI and the UI (crucible#119, #120,
#121, #123), against the stand-in gateway and GitHub App of tools/smoke/first_run_stubs.py
over real loopback HTTP.

The gateway half runs with the Docker provider's credential directories; the GitHub half
runs on the Kubernetes provider's shape, with the App credential in a Secret the service
owns (ADR 0017) on the in-memory API server. The kind proof drives the same flow on a
real cluster (docs/implementation-notes/first-run.md)."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import html
import importlib.util
import json
import re
import sys
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import sqlalchemy
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.github.appauth import AppAuthenticator, AppConfig
from crucible.adapters.github.apps import RestGitHubApps
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.credentials import SecretAppCredentials
from crucible.adapters.github.transport import RestTransport
from crucible.application.admin.context import AdminContext
from crucible.application.auth import mint_token
from crucible.application.supervisor import Supervisor
from crucible.cli import admin as cli
from crucible.domain.entities import Role
from tests.admin_cli import admin_main
from tests.integration.conftest import put_seeded_policy_in_force
from tests.integration.test_admin import (
    fake_login_cli,
    run_cli,
    seed_credentials,
    ui_sign_in,
)

pytestmark = pytest.mark.integration

STUBS_PATH = Path(__file__).parents[2] / "tools" / "smoke" / "first_run_stubs.py"
GATEWAY_KEY = "vk_" + "g" * 40
APP_ID = 4242


def _stubs_module() -> Any:
    spec = importlib.util.spec_from_file_location("first_run_stubs", STUBS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["first_run_stubs"] = module
    spec.loader.exec_module(module)
    return module


STUBS = _stubs_module()


def _rsa_pem() -> tuple[str, str]:
    """A throwaway App key made for this run and never written anywhere."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


@pytest.fixture(scope="module")
def app_key() -> tuple[str, str]:
    return _rsa_pem()


@pytest.fixture
def stubs(app_key: tuple[str, str]) -> Iterator[Any]:
    config = {
        "gateway_key": GATEWAY_KEY,
        "models": ["fast", "coder-large"],
        "app_id": APP_ID,
        "app_slug": "crucible-test",
        "public_key_pem": app_key[1],
        "installations": [
            {
                "id": 7,
                "account": "octo-lab",
                "type": "Organization",
                "repositories": [
                    {"full_name": "octo-lab/widgets", "default_branch": "trunk"},
                    {"full_name": "octo-lab/gadgets", "default_branch": "main"},
                    {"full_name": "octo-lab/old", "archived": True},
                    {"full_name": "octo-lab/secret", "private": True},
                ],
            },
            {
                "id": 9,
                "account": "someone",
                "type": "User",
                "repositories": [{"full_name": "someone/notes", "default_branch": "main"}],
            },
        ],
    }
    with STUBS.StubServer(config) as server:
        yield server


@pytest.fixture
def k8s_api() -> FakeKubernetesApi:
    return FakeKubernetesApi()


@pytest.fixture
def admin_ctx(
    ctx: AppContext,
    provider: FakeProvider,
    tmp_path: Path,
    stubs: Any,
    k8s_api: FakeKubernetesApi,
) -> Iterator[AdminContext]:
    """Credential directories for the harnesses (the Docker shape) and the GitHub App
    credential in a Secret on the in-memory API server (the Kubernetes shape), both
    pointed at the stubs."""
    assert ctx.harnesses is not None
    root = tmp_path / "credentials"
    root.mkdir()
    store = SecretAppCredentials(k8s_api, name="crucible-github-app")
    transport = RestTransport(stubs.url, timeout=10)
    authenticator = AppAuthenticator(
        AppConfig(app_id=0, private_key_path="", api_base=stubs.url),
        transport,
        credentials=store,
    )
    admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        providers={"fake": provider},
        harnesses=ctx.harnesses,
        credential_sources=seed_credentials(root),
        artifact_root=str(tmp_path / "artifacts"),
        lease_ttl_seconds=30,
        credential_retention_hours=0,
        login_commands=fake_login_cli(tmp_path),
        probe_timeout_seconds=10,
        github=RestGitHubClient(authenticator, transport),
        github_credentials=store,
        github_apps=RestGitHubApps(authenticator, transport),
    )
    admin.github_app = type(admin.github_app)(api_base=stubs.url)
    ctx.admin = admin
    with ctx.uow_factory() as uow:
        hermes_before = copy.deepcopy(uow.harnesses.get("hermes"))
        if hermes_before is not None:
            # Hermes as a fresh deployment has it: never tested, never refused. An
            # earlier test's refusal, stamped by the system clock, would otherwise
            # outrank this test's fixed clock.
            fresh = copy.deepcopy(hermes_before)
            fresh.last_validated_at = None
            fresh.last_auth_failure_at = None
            fresh.last_launch_at = None
            fresh.last_launch_outcome = None
            uow.harnesses.put(fresh)
            uow.commit()
    put_seeded_policy_in_force(ctx)
    yield admin
    # Policies and harness state outlive the per-test truncation; leave them as a fresh
    # deployment has them for the tests that follow.
    put_seeded_policy_in_force(ctx)
    with ctx.uow_factory() as uow:
        if hermes_before is not None:
            uow.harnesses.put(hermes_before)
            uow.commit()


@pytest.fixture
def live(ctx: AppContext, provider: FakeProvider) -> Supervisor:
    return Supervisor(
        ctx.uow_factory,
        {"fake": provider},
        SystemClock(),
        holder="first-run-tests",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=300,
        harnesses=ctx.harnesses,
    )


@pytest.fixture
def admin(
    ctx: AppContext, tokens: dict[str, str], admin_ctx: AdminContext, live: Supervisor
) -> Iterator[TestClient]:
    with TestClient(create_app(ctx), headers={"Authorization": f"Bearer {tokens['admin']}"}) as c:
        yield c


@pytest.fixture
def config_file(migrated: str, admin_ctx: AdminContext, tmp_path: Path) -> Path:
    """The CLI's local mode: the same database and the same Hermes key directory."""
    source = admin_ctx.credential_sources["hermes"]
    path = tmp_path / "crucible.toml"
    path.write_text(
        "\n".join(
            [
                "[database]",
                f'url = "{migrated}"',
                "[supervisor]",
                "lease_ttl_seconds = 300",
                "[credentials.hermes]",
                f'path = "{source.path}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _readiness(client: TestClient, harness: str) -> dict[str, Any]:
    document = client.get("/v1/admin/status").json()["readiness"]
    return next(h for h in document["harnesses"] if h["name"] == harness)


def test_the_gateway_is_set_tested_and_its_models_picked(
    admin: TestClient,
    live: Supervisor,
    stubs: Any,
    ctx: AppContext,
    tokens: dict[str, str],
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live.tick())
    endpoint = f"{stubs.url}/v1"
    before = admin.get("/v1/admin/gateway").json()
    assert before["endpoint_url"] is None and before["key_set"] is False
    codes = [s["code"] for s in _readiness(admin, "hermes")["steps"]]
    assert codes[:2] == ["credential_missing", "endpoint_not_configured"]

    # #119: the URL and the key in one step, tested at once, reported in plain words.
    saved = admin.post(
        "/v1/admin/gateway",
        json={"reason": "first run", "endpoint_url": endpoint, "api_key": GATEWAY_KEY},
    )
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result["test"]["passed"] is True
    assert result["test"]["summary"] == f"Gateway {endpoint} reachable, key accepted, 2 models."
    assert GATEWAY_KEY not in saved.text
    assert result["gateway"]["endpoint_url"] == endpoint
    assert result["gateway"]["credential_state"] == "validated"
    assert "GET /health/readiness" in stubs.stubs.seen and "GET /v1/models" in stubs.stubs.seen

    # #121: only what the key can see, without the seeded `coder` entry it lacks.
    listing = admin.get("/v1/admin/gateway/models").json()
    rows = {row["id"]: row for row in listing["models"]}
    assert listing["reachable"] is True and listing["offered_count"] == 2
    assert rows["fast"]["offered"] is True and rows["fast"]["in_policy"] is False
    assert set(rows) == {"fast", "coder-large"}

    refused = admin.post(
        "/v1/admin/gateway/models",
        json={"reason": "typo", "models": [{"id": "nope", "enabled": True}]},
    )
    assert refused.status_code == 409 and "does not offer ['nope']" in refused.json()["detail"]

    picked = admin.post(
        "/v1/admin/gateway/models",
        json={
            "reason": "use the fast model",
            "models": [{"id": "fast", "enabled": True, "enable_thinking": True}],
            "max_concurrency": 2,
        },
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["added"] == ["fast"] and picked.json()["enabled"] == ["fast"]
    local = admin.get("/v1/admin/routing/local-endpoint").json()
    fast = next(m for m in local["models"] if m["id"] == "fast")
    assert fast["harness"] == "hermes" and fast["endpoint_url"] == endpoint
    assert fast["chat_template_kwargs"] == {"enable_thinking": True}
    assert local["pool"]["max_concurrency"] == 2
    codes = [s["code"] for s in _readiness(admin, "hermes")["steps"]]
    assert codes == ["no_promoted_image"]

    # The page only shows what the key can still see. Saving that view disables the
    # missing `fast` route with the not-offered reason rather than removing it.
    stubs.stubs.config["models"] = ["coder-large"]
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        # Plain GET: no gateway call, but admin form and link are present.
        plain_page = browser.get("/ui/gateway").text
        assert "Set the gateway URL and key" in plain_page
        assert "List the gateway" in plain_page
        page = browser.get("/ui/gateway?models=1").text
        assert 'value="fast"' not in page
        shown_rows = re.findall(r'name="model\.(\d+)\.id" value="([^"]+)"', page)
        form = {"csrf": csrf, "reason": "saved as shown", "return_to": "/ui/gateway"}
        form["max_concurrency"] = "2"
        for index, model in shown_rows:
            form[f"model.{index}.id"] = model
            if model in ("fast", "coder-large"):
                form[f"model.{index}.enabled"] = "true"
        preview = browser.post("/ui/actions/gateway-models", data=form, follow_redirects=False)
        assert preview.status_code == 200 and "What saving publishes" in preview.text
        saved_as_shown = browser.post(
            "/ui/actions/gateway-models", data={**form, "confirm": "true"}, follow_redirects=False
        )
        assert saved_as_shown.status_code == 303, saved_as_shown.text
        location = unquote(saved_as_shown.headers["location"])
        assert "kind=ok" in location and "Disabled as no longer offered: fast." in location
    local = admin.get("/v1/admin/routing/local-endpoint").json()
    fast = next(m for m in local["models"] if m["id"] == "fast")
    assert fast["enabled"] is False and "no longer offers" in fast["disabled_reason"]
    large = next(m for m in local["models"] if m["id"] == "coder-large")
    assert large["enabled"] is True

    # The CLI reads and writes the same state.
    shown = run_cli(config_file, "gateway", "show", capsys=capsys)
    assert shown["endpoint_url"] == endpoint and shown["key_set"] is True
    cli_models = run_cli(config_file, "gateway", "models", capsys=capsys)
    assert [row["id"] for row in cli_models["models"]][:1] == ["coder-large"]
    cli_pick = run_cli(
        config_file,
        "--reason",
        "thinking on",
        "gateway",
        "pick",
        "--enable",
        "coder-large",
        "--thinking",
        "coder-large",
        capsys=capsys,
    )
    assert cli_pick["enabled"] == ["coder-large"]

    # A wrong key is saved (the operator may fix the gateway next) and said plainly.
    wrong = admin.post(
        "/v1/admin/gateway",
        json={"reason": "wrong key", "endpoint_url": endpoint, "api_key": "vk_" + "w" * 40},
    )
    assert wrong.status_code == 200
    assert wrong.json()["test"]["summary"] == (
        f"Gateway {endpoint} reachable, but it refused the key (HTTP 401)."
    )
    # A new URL whose test is inconclusive does not keep reading as verified (#123).
    admin.post(
        "/v1/admin/gateway",
        json={"reason": "right key", "endpoint_url": endpoint, "api_key": GATEWAY_KEY},
    )
    assert admin.get("/v1/admin/gateway").json()["credential_state"] == "validated"
    down = admin.post(
        "/v1/admin/gateway",
        json={"reason": "moved", "endpoint_url": "http://127.0.0.1:9/v1"},
    )
    assert down.status_code == 200 and down.json()["test"]["conclusive"] is False
    assert down.json()["gateway"]["credential_state"] == "configured"
    codes = [s["code"] for s in _readiness(admin, "hermes")["steps"]]
    assert "credential_not_verified" in codes
    audit = admin.get("/v1/admin/audit", params={"limit": 200}).text
    assert "local_gateway_updated" in audit
    assert GATEWAY_KEY not in audit and "w" * 40 not in audit

    # The UI page shows the URL, the result and the pick form, never the key.
    with TestClient(create_app(ctx)) as browser:
        ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/gateway?models=1")
        assert page.status_code == 200
        assert "http://127.0.0.1:9/v1" in page.text and GATEWAY_KEY not in page.text
        assert "the last test was inconclusive" in page.text
        # With the gateway down it lists nothing, so the page says why and still shows
        # the entries in force.
        assert "could not be reached" in page.text
        assert re.search(r'name="model\.\d\.id" value="coder-large"', page.text)


def test_github_is_connected_installed_and_a_repository_picked(
    admin: TestClient,
    live: Supervisor,
    stubs: Any,
    k8s_api: FakeKubernetesApi,
    app_key: tuple[str, str],
    ctx: AppContext,
    tokens: dict[str, str],
) -> None:
    asyncio.run(live.tick())
    assert admin.get("/v1/admin/github").json()["configured"] is False
    picker = admin.get("/v1/admin/github/installations").json()
    assert picker["connected"] is False and "No GitHub App is connected" in picker["error"]

    # Create GitHub App is the only way to connect an App (the operator, 2026-09-27):
    # pasting an existing App's id and key is gone from the API (the UI and the CLI are
    # checked in the tests below).
    gone = admin.post(
        "/v1/admin/github/app",
        json={"reason": "connect", "app_id": APP_ID, "private_key": app_key[0]},
    )
    assert gone.status_code == 404
    assert ("secrets", "crucible-github-app") not in k8s_api.objects

    # The one-click flow is tested below; here the credential it would store is put in the
    # service's own store, and the picker is what is under test.
    assert ctx.admin is not None and ctx.admin.github_credentials is not None
    ctx.admin.github_credentials.write(
        app_id=APP_ID, private_key=app_key[0].encode(), webhook_secret=None
    )
    body = admin.get("/v1/admin/github").json()
    assert body["configured"] is True and body["app_id"] == APP_ID
    assert body["stored_in"]["kind"] == "secret" and body["stored_in"]["service_owned"] is True
    assert "PRIVATE KEY" not in json.dumps(body)

    # #120: the picker, grouped by account, and a pick registers with GitHub's facts.
    picker = admin.get("/v1/admin/github/installations").json()
    assert picker["install_url"] == "https://github.com/apps/crucible-test/installations/new"
    assert [i["account"] for i in picker["installations"]] == ["octo-lab", "someone"]
    widgets = picker["installations"][0]["repositories"][-1]
    assert widgets["full_name"] == "octo-lab/widgets" and widgets["registered_as"] is None
    added = admin.post(
        "/v1/admin/github/repositories",
        json={
            "reason": "first repository",
            "installation_id": 7,
            "repository": "octo-lab/widgets",
            "attested_all_prs": True,
        },
    )
    assert added.status_code == 200, added.text
    registered = added.json()
    assert registered["repository"] == "widgets"
    assert registered["url"] == "https://github.com/octo-lab/widgets"
    assert registered["default_branch"] == "trunk" and registered["installation_id"] == 7
    picker = admin.get("/v1/admin/github/installations").json()
    widgets = next(
        r for r in picker["installations"][0]["repositories"] if r["full_name"].endswith("widgets")
    )
    assert widgets["registered_as"] == "widgets"
    uncovered = admin.post(
        "/v1/admin/github/repositories",
        json={
            "reason": "wrong installation",
            "installation_id": 9,
            "repository": "octo-lab/gadgets",
        },
    )
    assert uncovered.status_code == 409 and "does not cover" in uncovered.json()["detail"]
    archived = admin.post(
        "/v1/admin/github/repositories",
        json={"reason": "archived", "installation_id": 7, "repository": "octo-lab/old"},
    )
    assert archived.status_code == 409 and "archived" in archived.json()["detail"]

    # ADR 0019: a private repository is registered as private, once the App has shown it
    # can mint the read-only checkout token for it; the token is revoked at once.
    secret = next(
        r for r in picker["installations"][0]["repositories"] if r["full_name"].endswith("secret")
    )
    assert secret["private"] is True and secret["unsupported"] is None
    assert widgets["unsupported"] is None
    old = next(
        r for r in picker["installations"][0]["repositories"] if r["full_name"].endswith("old")
    )
    assert old["unsupported"] == "archived: cannot take a pull request"
    revoked_before = stubs.stubs.revoked
    registered_private = admin.post(
        "/v1/admin/github/repositories",
        json={
            "reason": "private",
            "installation_id": 7,
            "repository": "octo-lab/secret",
            "attested_all_prs": True,
        },
    )
    assert registered_private.status_code == 200, registered_private.text
    assert registered_private.json()["private"] is True
    assert stubs.stubs.mints[-1] == {
        "installation": 7,
        "repositories": ["secret"],
        "permissions": {"contents": "read"},
    }
    assert stubs.stubs.revoked == revoked_before + 1
    items = {r["repository"]: r for r in admin.get("/v1/admin/repositories").json()["items"]}
    assert items["secret"]["private"] is True and items["widgets"]["private"] is False
    steps = admin.get("/v1/admin/status").json()["readiness"]["steps"]
    assert "no_repository" not in [s["code"] for s in steps]

    # The UI: the install link, the picker's select of unregistered repositories.
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/github")
        assert page.status_code == 200
        assert 'href="https://github.com/apps/crucible-test/installations/new"' in page.text
        assert '<option value="octo-lab/gadgets"' in page.text
        assert '<option value="octo-lab/widgets"' not in page.text
        # Registered already, and the archived one is marked and never offered.
        assert '<option value="octo-lab/secret"' not in page.text
        assert '<option value="octo-lab/old"' not in page.text
        assert "archived: cannot take a pull request" in page.text
        assert "not supported yet" not in page.text
        assert "PRIVATE KEY" not in page.text
        posted = browser.post(
            "/ui/actions/github-add-repository",
            data={
                "csrf": csrf,
                "installation_id": "7",
                "repository": "octo-lab/gadgets",
                "name": "",
                "policy_name": "default-software",
                "attested_all_prs": "true",
                "reason": "second repository",
                "return_to": "/ui/github",
            },
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert "Registered gadgets" in unquote(posted.headers["location"])
    names = {r["repository"] for r in admin.get("/v1/admin/repositories").json()["items"]}
    assert {"widgets", "gadgets"} <= names


def test_the_cli_remote_mode_builds_the_first_run_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from crucible.client.config import ADMIN_TOKEN_ENV  # noqa: PLC0415
    from crucible.client.http import Api  # noqa: PLC0415

    calls: list[tuple[str, str, Any]] = []

    def fake_call(self: Any, method: str, path: str, body: Any = None, **_: Any) -> Any:
        calls.append((method, path, body))
        return {"ok": True}

    monkeypatch.setattr(Api, "call", fake_call)
    monkeypatch.setattr(cli, "_read_api_key", lambda: GATEWAY_KEY)
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    base = ["--api-url", "http://127.0.0.1:1", "--reason", "r"]
    admin_main([*base, "gateway", "set", "--endpoint-url", "http://gw/v1", "--key"])
    admin_main([*base, "gateway", "test"])
    admin_main(["--api-url", "http://127.0.0.1:1", "gateway", "models"])
    admin_main([*base, "gateway", "pick", "--enable", "a", "--disable", "b", "--thinking", "a"])
    # `github connect` (an existing App's id and key) is gone; Create GitHub App on the
    # GitHub page is the only way to connect one (the operator, 2026-09-27).
    with pytest.raises(SystemExit) as refused:
        admin_main([*base, "github", "connect", "--app-id", "5", "--private-key-file", "x"])
    assert refused.value.code == 2
    assert "invalid choice: 'connect'" in capsys.readouterr().out
    admin_main(["--api-url", "http://127.0.0.1:1", "github", "installations"])
    admin_main(["--api-url", "http://127.0.0.1:1", "github", "external-url"])
    admin_main([*base, "github", "set-external-url", "--url", "https://hades.example"])
    admin_main([*base, "github", "set-external-url", "--url", ""])
    admin_main([*base, "github", "add-repository", "--installation-id", "7", "--repository", "o/r"])
    expected: list[tuple[str, str, Any]] = [
        (
            "POST",
            "/v1/admin/gateway",
            {"reason": "r", "endpoint_url": "http://gw/v1", "api_key": GATEWAY_KEY},
        ),
        ("POST", "/v1/admin/gateway/test", {"reason": "r"}),
        ("GET", "/v1/admin/gateway/models", None),
        (
            "POST",
            "/v1/admin/gateway/models",
            {
                "reason": "r",
                "models": [
                    {"id": "a", "enabled": True, "enable_thinking": True, "capability": None},
                    {"id": "b", "enabled": False, "enable_thinking": False, "capability": None},
                ],
                "max_concurrency": None,
            },
        ),
        ("GET", "/v1/admin/github/installations", None),
        ("GET", "/v1/admin/github/external-url", None),
        ("POST", "/v1/admin/github/external-url", {"reason": "r", "url": "https://hades.example"}),
        ("POST", "/v1/admin/github/external-url", {"reason": "r", "url": None}),
        (
            "POST",
            "/v1/admin/github/repositories",
            {
                "reason": "r",
                "installation_id": 7,
                "repository": "o/r",
                "name": None,
                "policy_name": "default-software",
                "attested_all_prs": False,
                "attested_by": None,
            },
        ),
    ]
    assert calls == expected
    assert re.search(r"cru_", json.dumps(calls)) is None


# ----- the one-click App: GitHub's manifest flow (crucible#168) -------------------------

HADES = "http://hades.test"
MANIFEST_APP_ID = 5151


def _manifest_form(page: str) -> tuple[str, dict[str, Any]]:
    action = re.search(r'id="github-manifest"[^>]*action="([^"]+)"', page)
    manifest = re.search(r'name="manifest" value="([^"]+)"', page)
    assert action is not None and manifest is not None, page
    return html.unescape(action.group(1)), json.loads(html.unescape(manifest.group(1)))


def _create(browser: TestClient, csrf: str, **fields: str) -> TestClient | Any:
    return browser.post(
        "/ui/actions/github-create-app",
        data={"csrf": csrf, "return_to": "/ui/github", **fields},
        headers={"Origin": HADES},
        follow_redirects=False,
    )


def _on_github(target: str, manifest: dict[str, Any]) -> str:
    """What the operator's browser does on github.com: post the manifest, press Create
    on the confirm page, and follow the redirect back. Returns the path and query it is
    sent back to on Crucible."""
    with httpx.Client(follow_redirects=False, timeout=10) as web:
        confirm = web.post(target, data={"manifest": json.dumps(manifest)})
        assert confirm.status_code == 200, confirm.text
        pending = re.search(r"name='pending' value='([^']+)'", confirm.text)
        assert pending is not None
        back = web.post(
            urllib.parse.urljoin(target, "/_stub/apps/confirm"),
            data={"pending": pending.group(1)},
        )
    assert back.status_code == 302
    location = back.headers["location"]
    assert location.startswith(f"{HADES}/ui/github/callback?"), location
    return location[len(HADES) :]


def _flash(response: Any) -> str:
    assert response.status_code == 303, response.text
    return unquote(response.headers["location"])


def test_github_app_is_created_with_one_click_and_installed(
    admin: TestClient,
    live: Supervisor,
    stubs: Any,
    k8s_api: FakeKubernetesApi,
    ctx: AppContext,
    tokens: dict[str, str],
) -> None:
    asyncio.run(live.tick())
    stubs.stubs.config["installations"] = []
    stubs.stubs.config["manifest"] = {
        "app_id": MANIFEST_APP_ID,
        "owner": "sentania",
        "install": {
            "id": 31,
            "account": "octo-lab",
            "type": "Organization",
            "repositories": [
                {"full_name": "octo-lab/widgets", "default_branch": "trunk"},
                {"full_name": "octo-lab/secret", "private": True},
            ],
        },
    }
    responses: list[str] = []
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/github").text
        # One button, and no way to paste an existing App's id and key (the operator,
        # 2026-09-27): not on the page, not by the old link, not by the old action.
        assert "Create GitHub App" in page and 'value="Hades-' in page
        for text in (page, browser.get("/ui/github?existing=1").text):
            assert 'name="private_key"' not in text and "existing=1" not in text
            assert "github-connect" not in text and "Already have a GitHub App" not in text
        pasted = browser.post(
            "/ui/actions/github-connect",
            data={"csrf": csrf, "return_to": "/ui/github", "app_id": "5", "private_key": "x"},
            follow_redirects=False,
        )
        assert "unknown UI action 'github-connect'" in _flash(pasted)

        # Create: the page the browser posts to GitHub, the manifest filled in.
        started = _create(browser, csrf, app_name="Hades-test", organization="", reason="")
        assert started.status_code == 200 and started.headers["cache-control"] == "no-store"
        binding = started.headers["set-cookie"]
        assert binding.startswith("crucible_github_start_") and "samesite=lax" in binding.lower()
        assert "path=/ui/github" in binding.lower() and "httponly" in binding.lower()
        responses.append(started.text)
        target, manifest = _manifest_form(started.text)
        assert target.startswith(f"{stubs.url}/settings/apps/new?state=")
        assert manifest == {
            "name": "Hades-test",
            "url": HADES,
            "description": "Crucible delivery: opens and follows pull requests.",
            "public": False,
            "redirect_url": f"{HADES}/ui/github/callback",
            "setup_url": f"{HADES}/ui/github/installed",
            "setup_on_update": True,
            "hook_attributes": {"url": f"{HADES}/v1/github/webhook", "active": False},
            "default_permissions": {
                "metadata": "read",
                "contents": "write",
                "pull_requests": "write",
                "checks": "read",
                "actions": "read",
                "issues": "read",
            },
            "default_events": [],
        }
        with ctx.uow_factory() as uow:
            stored = uow.session.execute(  # type: ignore[attr-defined]
                sqlalchemy.text("SELECT state_hash FROM github_manifest_states")
            ).scalars()
            state = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)["state"][0]
            assert list(stored) == [hashlib.sha256(state.encode()).hexdigest()]

        # An organization's page, when one is named; a login that is not one is refused.
        org = _create(browser, csrf, app_name="Hades-org", organization="octo-lab")
        assert _manifest_form(org.text)[0].startswith(
            f"{stubs.url}/organizations/octo-lab/settings/apps/new?state="
        )
        bad_org = _create(browser, csrf, app_name="x", organization="-no-")
        assert "is not a GitHub organization login" in _flash(bad_org)

        callback = _on_github(target, manifest)
        assert stubs.stubs.manifests[0]["manifest"] == manifest

        # GitHub's redirect is cross-site, so the Strict session cookie is not sent: the
        # first arrival reloads itself from Crucible's own site; still no session, and
        # it goes to sign-in and back. Nothing is exchanged on the way.
        with TestClient(create_app(ctx)) as cross_site:
            hop = cross_site.get(callback, follow_redirects=False)
            assert hop.status_code == 200 and 'http-equiv="refresh"' in hop.text
            assert hop.headers["referrer-policy"] == "no-referrer"
            again = re.search(r'url=([^"]+)"', hop.text)
            assert again is not None and "hop=1" in html.unescape(again.group(1))
            signed_out = cross_site.get(html.unescape(again.group(1)), follow_redirects=False)
            assert signed_out.status_code == 303
            assert _flash(signed_out).startswith("/ui/sign-in?next=/ui/github/callback?code=")
        assert stubs.stubs.conversions == []

        # The signed-in browser: the state checks out, the code is exchanged once, and
        # the new App's credential is in the Secret the service owns.
        done = browser.get(callback, follow_redirects=False)
        message = _flash(done)
        responses.append(message)
        assert "kind=ok" in message and f"(App {MANIFEST_APP_ID})" in message
        assert "Created the GitHub App hades-test" in message
        conversions = [s for s in stubs.stubs.seen if s.startswith("POST /app-manifests/")]
        assert len(conversions) == 1 and len(stubs.stubs.conversions) == 1
        secret = k8s_api.objects[("secrets", "crucible-github-app")].body
        assert secret["metadata"]["labels"][k8sspec.LABEL_MANAGED_BY] == "crucible"
        assert set(secret["data"]) == {"app-id", "app.pem", "webhook.secret"}
        assert base64.b64decode(secret["data"]["app-id"]).decode() == str(MANIFEST_APP_ID)
        pem = base64.b64decode(secret["data"]["app.pem"]).decode()
        hook = base64.b64decode(secret["data"]["webhook.secret"]).decode()
        assert "PRIVATE KEY" in pem and hook

        # Used once: the same return is refused without asking GitHub again.
        replay = browser.get(callback, follow_redirects=False)
        assert "kind=bad" in _flash(replay) and "used already" in _flash(replay)
        wrong = browser.get(
            "/ui/github/callback?code=mc_x&state=" + "w" * 43, follow_redirects=False
        )
        assert "matches no start" in _flash(wrong)
        assert len(stubs.stubs.conversions) == 1

        # Expired: a start older than its 15 minutes is refused, and GitHub is not asked.
        late = _create(browser, csrf, app_name="Hades-late")
        late_target, late_manifest = _manifest_form(late.text)
        late_callback = _on_github(late_target, late_manifest)
        with ctx.uow_factory() as uow:
            uow.session.execute(  # type: ignore[attr-defined]
                sqlalchemy.text(
                    "UPDATE github_manifest_states SET expires_at = created_at "
                    "- interval '1 second' WHERE app_name = 'Hades-late'"
                )
            )
            uow.commit()
        expired = browser.get(late_callback, follow_redirects=False)
        assert "expired after 15 minutes" in _flash(expired)
        assert len(stubs.stubs.conversions) == 1

        # Another administrator's start is not this one's to finish.
        mine = _create(browser, csrf, app_name="Hades-mine")
        mine_callback = _on_github(*_manifest_form(mine.text))
        with ctx.uow_factory() as uow:
            other = mint_token(uow, ctx.clock, name="second-admin", role=Role.ADMIN).token
            uow.commit()
        with TestClient(create_app(ctx)) as second:
            ui_sign_in(second, other)
            theirs = second.get(mine_callback, follow_redirects=False)
            assert "belongs to another administrator" in _flash(theirs)
        # The same administrator in a browser that did not press Create: the start is
        # tied to the browser's cookie, so a state read from anywhere else finishes
        # nothing. Neither refusal spends the start.
        with TestClient(create_app(ctx)) as elsewhere:
            ui_sign_in(elsewhere, tokens["admin"])
            other_browser = elsewhere.get(mine_callback, follow_redirects=False)
            assert "made in another browser" in _flash(other_browser)
        with ctx.uow_factory() as uow:
            unspent = uow.session.execute(  # type: ignore[attr-defined]
                sqlalchemy.text(
                    "SELECT consumed_at FROM github_manifest_states WHERE app_name = 'Hades-mine'"
                )
            ).scalar_one()
            assert unspent is None
        assert len(stubs.stubs.conversions) == 1

        # Install: the button opens the App's own page; GitHub sends the browser back to
        # the repository picker, which lists what the installation covers.
        page = browser.get("/ui/github").text
        responses.append(page)
        install = f"{stubs.url}/apps/hades-test/installations/new"
        assert f'class="lat-btn lat-btn--primary" href="{install}"' in page
        assert "Install on GitHub" in page and "connected, App 5151" in page
        # Replace the App offers only a new App.
        assert "Replace the App" in page and "Create a new App instead" in page
        assert 'name="private_key"' not in page and "existing=1" not in page
        with httpx.Client(follow_redirects=False, timeout=10) as web:
            assert web.get(install).status_code == 200
            installed = web.post(install)
        assert installed.status_code == 302
        setup = installed.headers["location"]
        assert setup.startswith(f"{HADES}/ui/github/installed?installation_id=31")
        landed = browser.get(setup[len(HADES) :], follow_redirects=False)
        assert _flash(landed).startswith("/ui/github?kind=ok&message=Installed on GitHub")
        picker = browser.get("/ui/github").text
        responses.append(picker)
        assert "octo-lab (Organization), installation 31" in picker
        assert '<option value="octo-lab/widgets"' in picker

    # The key and the webhook secret are never in an answer, a page or the audit.
    audit_response = admin.get("/v1/admin/audit", params={"limit": 200})
    status_response = admin.get("/v1/admin/github")
    assert audit_response.status_code == 200 and status_response.status_code == 200
    audit, status = audit_response.text, status_response.text
    for text in [*responses, audit, status]:
        assert "PRIVATE KEY" not in text
        assert pem.strip().splitlines()[1] not in text and hook not in text
    events = [e for e in admin.get("/v1/admin/audit", params={"limit": 200}).json()["items"]]
    kinds = [e["kind"] for e in events]
    assert kinds.count("github_app_manifest_started") == 4
    connected = [e for e in events if e["kind"] == "github_app_connected"]
    assert len(connected) == 1
    after = connected[0]["payload"]["after"]
    assert after["via"] == "manifest" and after["app_id"] == MANIFEST_APP_ID
    assert after["key_fingerprint"].startswith("sha256:") and after["webhook_secret_set"] is True
    refusals = [e["payload"]["detail"] for e in events if e["kind"] == "admin_refused"]
    assert any("used already" in d for d in refusals)


def test_the_binding_cookie_is_scoped_to_a_reverse_proxy_path_prefix(
    admin: TestClient,
    live: Supervisor,
    stubs: Any,
    k8s_api: FakeKubernetesApi,
    ctx: AppContext,
    tokens: dict[str, str],
) -> None:
    """Codex correction on crucible#168, 2026-09-28: `github.external_url` with a path
    prefix (a deployment behind a reverse proxy) means GitHub returns the browser to
    `<prefix>/ui/github/callback`. The binding cookie must be scoped under that prefix, or
    the browser withholds it and the callback is refused. A real proxy strips the prefix
    before the request reaches Crucible, so the request that lands here is still
    `/ui/github/callback`; only the cookie the browser carries with it is prefix-scoped."""
    asyncio.run(live.tick())
    stubs.stubs.config["installations"] = []
    app_id = MANIFEST_APP_ID + 1
    stubs.stubs.config["manifest"] = {
        "app_id": app_id,
        "owner": "sentania",
        "install": {"id": 41, "account": "octo-lab", "type": "Organization", "repositories": []},
    }
    prefixed = f"{HADES}/crucible"
    saved = admin.post(
        "/v1/admin/github/external-url", json={"url": prefixed, "reason": "behind the ingress"}
    )
    assert saved.status_code == 200, saved.text
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        started = _create(browser, csrf, app_name="Hades-prefixed", organization="")
        binding = started.headers["set-cookie"]
        assert "path=/crucible/ui/github" in binding.lower()
        cookie_name, cookie_value = binding.split(";", 1)[0].split("=", 1)
        target, manifest = _manifest_form(started.text)
        assert manifest["redirect_url"] == f"{prefixed}/ui/github/callback"

        with httpx.Client(follow_redirects=False, timeout=10) as web:
            confirm = web.post(target, data={"manifest": json.dumps(manifest)})
            assert confirm.status_code == 200, confirm.text
            pending = re.search(r"name='pending' value='([^']+)'", confirm.text)
            assert pending is not None
            back = web.post(
                urllib.parse.urljoin(target, "/_stub/apps/confirm"),
                data={"pending": pending.group(1)},
            )
        assert back.status_code == 302
        location = back.headers["location"]
        assert location.startswith(f"{prefixed}/ui/github/callback?"), location
        # What a reverse proxy hands Crucible once it strips the `/crucible` prefix; the
        # browser's cookie (kept out of the jar above, since its path does not match this
        # unprefixed request) travels with the request regardless of the strip.
        stripped = location[len(prefixed) :]
        browser.cookies.set(cookie_name, cookie_value)
        done = browser.get(stripped, follow_redirects=False)
        message = _flash(done)
        assert "kind=ok" in message and f"(App {app_id})" in message
        cleared = done.headers["set-cookie"]
        assert "path=/crucible/ui/github" in cleared.lower()
    admin.post("/v1/admin/github/external-url", json={"url": None, "reason": "cleanup"})


def test_the_return_address_setting_overrides_the_browsers(
    admin: TestClient,
    live: Supervisor,
    stubs: Any,
    ctx: AppContext,
    tokens: dict[str, str],
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live.tick())
    assert admin.get("/v1/admin/github/external-url").json()["source"] == "browser"
    bad = admin.post("/v1/admin/github/external-url", json={"url": "ftp://x", "reason": "r"})
    assert bad.status_code == 422
    saved = admin.post(
        "/v1/admin/github/external-url",
        json={"url": "https://hades.apps.example.internal/", "reason": "behind the ingress"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["url"] == "https://hades.apps.example.internal"
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/github").text
        assert "https://hades.apps.example.internal (saved)" in page
        _, manifest = _manifest_form(_create(browser, csrf, app_name="Hades-x").text)
        assert manifest["redirect_url"] == "https://hades.apps.example.internal/ui/github/callback"
        cleared = browser.post(
            "/ui/actions/github-external-url",
            data={"csrf": csrf, "return_to": "/ui/github", "external_url": ""},
            follow_redirects=False,
        )
        assert "Cleared" in _flash(cleared)
    assert admin.get("/v1/admin/github/external-url").json()["url"] is None
    # The CLI's local mode, through the same service.
    cli_saved = run_cli(
        config_file,
        "--reason",
        "cli",
        "github",
        "set-external-url",
        "--url",
        "https://cli.example.internal",
        capsys=capsys,
    )
    assert cli_saved["url"] == "https://cli.example.internal"
    assert run_cli(config_file, "github", "external-url", capsys=capsys)["source"] == "database"
    audit = admin.get("/v1/admin/audit", params={"limit": 200}).text
    assert audit.count("github_external_url_updated") == 3


def test_local_codex_models_api_ui_and_cli(
    admin: TestClient,
    live: Supervisor,
    stubs: Any,
    ctx: AppContext,
    tokens: dict[str, str],
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#249: both harnesses can use one gateway alias, with a version per save."""
    asyncio.run(live.tick())
    saved = admin.post(
        "/v1/admin/gateway",
        json={"reason": "local Codex", "endpoint_url": f"{stubs.url}/v1", "api_key": GATEWAY_KEY},
    )
    assert saved.status_code == 200, saved.text
    picked = admin.post(
        "/v1/admin/gateway/models",
        json={
            "reason": "both harnesses",
            "models": [
                {"id": "fast", "enabled": True, "codex_enabled": True, "capability": "small"}
            ],
        },
    )
    assert picked.status_code == 200, picked.text
    version = picked.json()["routing_policy"]["version"]
    assert set(picked.json()["enabled"]) == {"fast", "codex-local:fast"}
    local = admin.get("/v1/admin/routing/local-endpoint").json()
    codex = next(m for m in local["models"] if m["harness"] == "codex")
    assert codex["model_name"] == "fast" and codex["pool"] == "lab-local"
    assert codex["endpoint"] == "local" and codex["capability"] == "small"
    refused = admin.post(
        "/v1/admin/gateway/models",
        json={"reason": "invalid flag", "models": [{"id": "fast", "codex_enabled": "false"}]},
    )
    assert refused.status_code == 422
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/gateway?models=1").text
        assert "use Codex for fast" in page
        rows = re.findall(r'name="model\.(\d+)\.id" value="([^"]+)"', page)
        index = next(i for i, model in rows if model == "fast")
        response = browser.post(
            "/ui/actions/gateway-models",
            data={
                "csrf": csrf,
                "reason": "use Codex alone",
                "return_to": "/ui/gateway",
                "model.0.id": "fast",
                "model.0.codex": "true",
                "max_concurrency": "3",
            },
            follow_redirects=False,
        )
        assert f'name="model.{index}.codex"' in page
        assert response.status_code == 303 and "kind=ok" in response.headers["location"]
    local = admin.get("/v1/admin/routing/local-endpoint").json()
    assert local["routing_policy"]["version"] > version
    assert {m["id"] for m in local["models"] if m["enabled"]} == {"codex-local:fast"}
    cli = run_cli(
        config_file,
        "--reason",
        "disable Codex",
        "gateway",
        "pick",
        "--harness",
        "codex",
        "--disable",
        "fast",
        capsys=capsys,
    )
    assert cli["enabled"] == []
    cli = run_cli(
        config_file,
        "--reason",
        "enable Codex",
        "gateway",
        "pick",
        "--harness",
        "codex",
        "--enable",
        "fast",
        capsys=capsys,
    )
    assert cli["enabled"] == ["codex-local:fast"]

    # A Codex-only alias that disappears still saves as disabled, just like Hermes.
    stubs.stubs.config["models"] = ["coder-large"]
    stale = admin.post(
        "/v1/admin/gateway/models",
        json={"reason": "stale model list", "models": [{"id": "fast", "codex_enabled": True}]},
    )
    assert stale.status_code == 200, stale.text
    assert stale.json()["disabled_not_offered"] == ["codex-local:fast"]
