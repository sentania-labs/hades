"""Private repository checkout (ADR 0019, crucible#157) against the stand-in GitHub App
of tools/smoke/first_run_stubs.py over real loopback HTTP, and its git stand-in over
real HTTPS.

What is proved here: registration mints the checkout token once, scoped to the one
repository with `contents: read`, and revokes it; registration refuses a private
repository with a plain reason when the App is not connected or the installation cannot
see it, through the admin API, the 04 path and the UI form; the supervisor mints a fresh
token for a private repository's preparation, hands it to the provider, then revokes and
empties it, and mints nothing for a public repository; a prepare the App cannot serve
ends the attempt with the reason; and the rendered preparer script clones from a remote
that demands the token, and cannot once the token is revoked.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution import scripts, workspace
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.github.appauth import AppAuthenticator, AppConfig
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.transport import RestTransport
from crucible.application.admin.context import AdminContext
from crucible.application.repositories import (
    PrivateCheckoutRefusedError,
    register_repository,
)
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.ports.execution import IDENTITY_MOUNT
from tests.fixtures import REPOSITORY_URL
from tests.integration.conftest import make_supervisor, submit_and_start
from tests.integration.test_admin import ui_sign_in
from tests.integration.test_first_run import APP_ID, STUBS, _rsa_pem

pytestmark = pytest.mark.integration

PRIVATE = "octo-lab/secret"
# The tier's own repository, whose issues its task contracts close; private here.
TIER = REPOSITORY_URL.removeprefix("https://github.com/")


@pytest.fixture(scope="module")
def app_key() -> tuple[str, str]:
    return _rsa_pem()


def _config(app_key: tuple[str, str], git_url: str | None = None) -> dict[str, Any]:
    secret: dict[str, Any] = {
        "full_name": PRIVATE,
        "private": True,
        "default_branch": "main",
        "git": {"files": {"README.md": "private plans\n"}},
    }
    if git_url:
        secret["url"] = git_url
    return {
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
                    secret,
                    {"full_name": TIER, "private": True},
                ],
            },
            {
                "id": 9,
                "account": "someone",
                "type": "User",
                "repositories": [{"full_name": "someone/notes"}],
            },
        ],
    }


@pytest.fixture
def stubs(app_key: tuple[str, str]) -> Iterator[Any]:
    with STUBS.StubServer(_config(app_key)) as server:
        yield server


def _github(stubs: Any, app_key: tuple[str, str], tmp_path: Path) -> RestGitHubClient:
    """The App connected the file way: its id and a key file only this test reads."""
    key = tmp_path / "app.pem"
    key.write_text(app_key[0], encoding="utf-8")
    key.chmod(0o600)
    transport = RestTransport(stubs.url, timeout=10)
    auth = AppAuthenticator(
        AppConfig(app_id=APP_ID, private_key_path=str(key), api_base=stubs.url), transport
    )
    return RestGitHubClient(auth, transport)


def _registration(url: str, installation_id: int | None, *, private: bool) -> Any:
    return RepositoryRegistration(
        url=url,
        default_branch="main",
        policy_name="default-software",
        installation_id=installation_id,
        external_review=ExternalReviewAttestation(attested_all_prs=True, attested_by="tests"),
        private=private,
    )


def _make_private(ctx: AppContext, github: RestGitHubClient | None, **kw: Any) -> Any:
    """Re-register the tier's `example-service` as private, at `url` when given."""
    with ctx.uow_factory() as uow:
        repository = register_repository(
            uow,
            ctx.clock,
            principal_name="tests",
            name="example-service",
            registration=_registration(
                kw.get("url", REPOSITORY_URL),
                kw.get("installation_id", 7),
                private=True,
            ),
            github=github,
        )
        uow.commit()
    return repository


# ----- registration ---------------------------------------------------------------


def test_registration_mints_a_scoped_read_only_token_once_and_revokes_it(
    ctx: AppContext, tokens: dict[str, str], stubs: Any, app_key: tuple[str, str], tmp_path: Path
) -> None:
    github = _github(stubs, app_key, tmp_path)
    repository = _make_private(ctx, github, url=f"https://github.com/{PRIVATE}")
    assert repository.private is True
    assert stubs.stubs.mints == [
        {"installation": 7, "repositories": ["secret"], "permissions": {"contents": "read"}}
    ]
    assert stubs.stubs.revoked == 1 and stubs.stubs.tokens == {}

    # A public registration never asks GitHub for anything.
    with ctx.uow_factory() as uow:
        register_repository(
            uow,
            ctx.clock,
            principal_name="tests",
            name="widgets",
            registration=_registration("https://github.com/octo-lab/widgets", 7, private=False),
            github=github,
        )
        uow.commit()
    assert len(stubs.stubs.mints) == 1


@pytest.mark.parametrize(
    ("installation_id", "connected", "reason"),
    [
        (9, True, "installation 9 cannot read octo-lab/secret's contents (HTTP 422)"),
        (404, True, "GitHub has no installation 404 for this App (HTTP 404)"),
        (None, True, "names no GitHub App installation"),
        (7, False, "no GitHub App is connected"),
    ],
)
def test_registration_refuses_a_private_repository_the_app_cannot_read(
    ctx: AppContext,
    tokens: dict[str, str],
    stubs: Any,
    app_key: tuple[str, str],
    tmp_path: Path,
    installation_id: int | None,
    connected: bool,
    reason: str,
) -> None:
    github = _github(stubs, app_key, tmp_path) if connected else None
    with pytest.raises(PrivateCheckoutRefusedError) as refused:
        _make_private(
            ctx, github, url=f"https://github.com/{PRIVATE}", installation_id=installation_id
        )
    assert "refusing to register: example-service is private" in str(refused.value)
    assert reason in str(refused.value)
    with ctx.uow_factory() as uow:
        kept = uow.repositories.get_by_name("example-service")
        assert kept is not None and kept.private is False, "a refusal changed the row"
    assert stubs.stubs.tokens == {}, "a token outlived the check"


def _admin_context(ctx: AppContext, github: RestGitHubClient | None, tmp_path: Path) -> None:
    assert ctx.harnesses is not None
    admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        providers={},
        harnesses=ctx.harnesses,
        credential_sources={},
        artifact_root=str(tmp_path / "artifacts"),
        lease_ttl_seconds=300,
        github=github,
        github_apps=None,
    )
    ctx.admin = admin


def test_the_form_the_api_and_the_04_path_accept_and_refuse_alike(
    ctx: AppContext, tokens: dict[str, str], stubs: Any, app_key: tuple[str, str], tmp_path: Path
) -> None:
    github = _github(stubs, app_key, tmp_path)
    _admin_context(ctx, github, tmp_path)
    live = make_supervisor(ctx, FakeProvider(), holder="private-checkout", lease_ttl_seconds=300)
    asyncio.run(live.tick())
    body = {
        "url": f"https://github.com/{PRIVATE}",
        "default_branch": "main",
        "policy_name": "default-software",
        "attested_all_prs": True,
    }
    with TestClient(create_app(ctx)) as http:
        admin = {"Authorization": f"Bearer {tokens['admin']}"}
        wrong = http.put(
            "/v1/admin/repositories/secret",
            json={**body, "installation_id": 9, "private": True, "reason": "wrong"},
            headers=admin,
        )
        assert wrong.status_code == 409
        assert "cannot read octo-lab/secret's contents" in wrong.json()["detail"]
        put = http.put(
            "/v1/admin/repositories/secret",
            json={**body, "installation_id": 7, "private": True, "reason": "right"},
            headers=admin,
        )
        assert put.status_code == 200, put.text
        assert put.json()["private"] is True

        # 04's own path: the same check, the same words.
        plain = http.put(
            "/v1/repositories/secret-04",
            json={
                "url": f"https://github.com/{PRIVATE}",
                "default_branch": "main",
                "policy_name": "default-software",
                "installation_id": 9,
                "private": True,
                "external_review": {"attested_all_prs": True},
            },
            headers=admin,
        )
        assert plain.status_code == 409
        assert "cannot read octo-lab/secret's contents" in plain.json()["detail"]
        view = http.get("/v1/repositories/secret", headers=admin).json()
        assert view["private"] is True

        csrf = ui_sign_in(http, tokens["admin"])
        page = http.get("/ui/repositories")
        assert 'name="private"' in page.text and "read-only token" in page.text
        posted = http.post(
            "/ui/actions/repository-register",
            data={
                "csrf": csrf,
                "name": "secret-form",
                "url": f"https://github.com/{PRIVATE}",
                "default_branch": "main",
                "policy_name": "default-software",
                "installation_id": "9",
                "private": "true",
                "attested_all_prs": "true",
                "reason": "through the form",
                "return_to": "/ui/repositories",
            },
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert "cannot read octo-lab/secret's contents" in unquote(posted.headers["location"])
        posted = http.post(
            "/ui/actions/repository-register",
            data={
                "csrf": csrf,
                "name": "secret-form",
                "url": f"https://github.com/{PRIVATE}",
                "default_branch": "main",
                "policy_name": "default-software",
                "installation_id": "7",
                "private": "true",
                "attested_all_prs": "true",
                "reason": "through the form",
                "return_to": "/ui/repositories",
            },
            follow_redirects=False,
        )
        assert posted.status_code == 303 and "kind=ok" in posted.headers["location"]
    with ctx.uow_factory() as uow:
        form = uow.repositories.get_by_name("secret-form")
        assert form is not None and form.private is True
    assert stubs.stubs.tokens == {}


# ----- the supervisor ---------------------------------------------------------------


@pytest.mark.parametrize("provider_name", ["fake", "kubernetes"])
async def test_the_supervisor_hands_a_fresh_token_to_prepare_and_revokes_it(
    ctx: AppContext,
    client: TestClient,
    provider: FakeProvider,
    stubs: Any,
    app_key: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    github = _github(stubs, app_key, tmp_path)
    _make_private(ctx, github)
    minted_at_registration = len(stubs.stubs.mints)
    supervisor = make_supervisor(ctx, provider, github=github)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    monkeypatch.setattr(provider, "name", provider_name)
    original_launch = provider.launch

    async def launch_with_checkout(ws: Any, spec: Any, **kwargs: Any) -> Any:
        token = provider.checkout_tokens[spec.attempt_id].token
        assert bool(token.reveal()) is (provider_name == "kubernetes")
        return await original_launch(ws, spec, **kwargs)

    monkeypatch.setattr(provider, "launch", launch_with_checkout)
    for _ in range(3):
        await supervisor.tick()
    task = client.get(f"/v1/tasks/{task_id}").json()
    attempt_id = task["executions"][0]["attempts"][0]["id"]
    seen = provider.checkout_tokens[attempt_id]
    assert seen.had_value and seen.repository == TIER
    assert seen.permissions == {"contents": "read", "metadata": "read"}
    assert seen.token.reveal() == "", "the token outlived launch in this process"
    assert stubs.stubs.mints[minted_at_registration:] == [
        {
            "installation": 7,
            "repositories": ["example-service"],
            "permissions": {"contents": "read"},
        }
    ]
    assert stubs.stubs.tokens == {}, "the token was not revoked at GitHub"


async def test_a_public_repository_is_prepared_with_no_token(
    ctx: AppContext,
    client: TestClient,
    provider: FakeProvider,
    stubs: Any,
    app_key: tuple[str, str],
    tmp_path: Path,
) -> None:
    supervisor = make_supervisor(ctx, provider, github=_github(stubs, app_key, tmp_path))
    submit_and_start(client, "crucible-worker:fake-succeed")
    for _ in range(3):
        await supervisor.tick()
    assert provider.checkout_tokens == {}
    assert stubs.stubs.mints == []


async def test_a_prepare_the_app_cannot_serve_ends_the_attempt_with_the_reason(
    ctx: AppContext,
    client: TestClient,
    provider: FakeProvider,
    stubs: Any,
    app_key: tuple[str, str],
    tmp_path: Path,
) -> None:
    github = _github(stubs, app_key, tmp_path)
    _make_private(ctx, github)
    # The App was disconnected after registration: this supervisor has no client.
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    for _ in range(3):
        await supervisor.tick()
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    collected = [e for e in events if e["kind"] == "attempt_collected"]
    assert collected, [e["kind"] for e in events]
    detail = collected[0]["payload"]["detail"]
    assert detail == (
        "refusing to prepare: example-service is private and no GitHub App is connected, "
        "so it cannot be cloned; connect the App on the GitHub page"
    )
    assert collected[0]["payload"]["exit_class"] == "environment"
    assert provider.checkout_tokens == {}


# ----- a real clone through the git stand-in -----------------------------------------


def _tls(tmp_path: Path) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "git.crt"
    key_path = tmp_path / "git.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def _prepare_locally(
    tmp_path: Path, url: str, host: str, token: str, ca: str, name: str
) -> subprocess.CompletedProcess[str]:
    """The preparer script the Docker provider runs, token on stdin, on this host."""
    script = scripts.preparer_script(
        url=url,
        base_ref="main",
        work_branch="crucible/private",
        from_remote_branch=False,
        cache_name=None,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
        origin_placeholder=workspace.ORIGIN_PLACEHOLDER,
        claude_md_wins=True,
        shims=workspace.SHIM_NAMES,
        exclude_entries=workspace.EXCLUDE_ENTRIES,
        identity_mount=IDENTITY_MOUNT,
        checkout_token="stdin",
        credential_host=host,
    )
    run = tmp_path / name
    (run / "tmp").mkdir(parents=True)
    (run / "token").mkdir()
    script = (
        script.replace("/tmp/gitconfig", f"{run}/tmp/gitconfig")
        .replace("/tmp/cred-helper.sh", f"{run}/tmp/cred-helper.sh")
        .replace("/crucible/work", f"{run}/work")
        .replace(scripts.TOKEN_MOUNT, f"{run}/token")
    )
    env = {"PATH": os.environ["PATH"], "GIT_SSL_CAINFO": ca, "NO_PROXY": "*"}
    return subprocess.run(
        ["sh", "-c", script], input=token, capture_output=True, text=True, env=env, check=False
    )


def test_the_preparer_clones_a_remote_that_demands_the_token(
    app_key: tuple[str, str], tmp_path: Path
) -> None:
    cert, key = _tls(tmp_path)
    with STUBS.StubServer(_config(app_key)) as api:
        github = _github(api, app_key, tmp_path)
        with STUBS.GitStubServer(
            _config(app_key), root=tmp_path / "git", auth_url=api.url, cert=cert, key=key
        ) as git:
            host = f"127.0.0.1:{git.port}"
            url = f"https://{host}/{PRIVATE}"
            token = github.checkout_token(installation_id=7, repository=PRIVATE)
            value = token.reveal()

            done = _prepare_locally(tmp_path, url, host, value, cert, "ok")
            assert done.returncode == 0, done.stderr
            checkout = tmp_path / "ok" / "work" / "repo"
            assert (checkout / "README.md").read_text() == "private plans\n"
            assert not (tmp_path / "ok" / "token" / "token").exists()
            for path in (tmp_path / "ok").rglob("*"):
                if path.is_file():
                    assert value.encode() not in path.read_bytes(), path
            assert value not in done.stdout + done.stderr
            # git asked without a credential first, was refused, and the helper answered.
            assert any(n.startswith("401 GET") for n in git.notes), git.notes
            assert any(n.startswith("200 GET info/refs") for n in git.notes), git.notes

            # The helper answers for the one host only: named differently, git gets
            # nothing and the remote refuses the clone.
            elsewhere = _prepare_locally(tmp_path, url, "github.com", value, cert, "elsewhere")
            assert elsewhere.returncode != 0
            assert not (tmp_path / "elsewhere" / "token" / "token").exists()

            # A token for another repository is refused by the remote.
            other = github.checkout_token(installation_id=7, repository="octo-lab/widgets")
            wrong = _prepare_locally(tmp_path, url, host, other.reveal(), cert, "wrong")
            assert wrong.returncode != 0
            assert any(n.startswith("403") for n in git.notes), git.notes

            # Revoked is revoked: the same token no longer clones.
            assert github.revoke_token(token) is True
            token.discard()
            revoked = _prepare_locally(tmp_path, url, host, value, cert, "revoked")
            assert revoked.returncode != 0
            assert not (tmp_path / "revoked" / "work" / "repo" / "README.md").exists()


async def test_the_token_is_revoked_and_emptied_when_prepare_fails(
    ctx: AppContext,
    client: TestClient,
    provider: FakeProvider,
    stubs: Any,
    app_key: tuple[str, str],
    tmp_path: Path,
) -> None:
    github = _github(stubs, app_key, tmp_path)
    _make_private(ctx, github)
    supervisor = make_supervisor(ctx, provider, github=github)
    task_id = submit_and_start(client, "crucible-worker:fake-prepare-fails")
    for _ in range(3):
        await supervisor.tick()
    task = client.get(f"/v1/tasks/{task_id}").json()
    attempt_id = task["executions"][0]["attempts"][0]["id"]
    seen = provider.checkout_tokens[attempt_id]
    assert seen.had_value and seen.token.reveal() == ""
    assert stubs.stubs.tokens == {}, "a failed prepare left its token live at GitHub"
