"""Private repository checkout (ADR 0019, crucible#157), below the integration tier.

What is asserted: the checkout token is requested for exactly one repository with
`contents: read` and is never cached; its revocation is `DELETE /installation/token`;
the helper answers only for https on the one configured host; the preparer removes the
token on every path out of the script and leaves nothing of it in the checkout; with the
Docker provider the token reaches the preparer container on stdin and a tmpfs, and no
other container; on Kubernetes it is a per-attempt Secret only the refresher and the
preparer mount, deleted before `prepare` returns on every path; and a public repository
is prepared exactly as before, with no credential at all.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec, scripts, workspace
from crucible.adapters.execution.docker import DockerProvider
from crucible.adapters.execution.kubernetes import KubernetesConfig
from crucible.adapters.github.appauth import CHECKOUT_PERMISSIONS, AppAuthenticator, AppConfig
from crucible.ports.execution import IDENTITY_MOUNT, ProviderError
from crucible.ports.github import AppCredential, InstallationToken
from tests.unit import kubernetes_fixtures as kf
from tests.unit.test_docker_provider import StubClient
from tests.unit.test_docker_provider import config as docker_config
from tests.unit.test_docker_provider import spec as docker_spec


def _secret_value() -> str:
    """A token-shaped value built at run time; nothing committed is one."""
    return "ghs_" + "Q" * 36


def _token(repository: str = "octo-lab/secret") -> InstallationToken:
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    return InstallationToken(
        _secret_value(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        repository=repository,
        permissions={"contents": "read", "metadata": "read"},
    )


# ----- the App calls --------------------------------------------------------------


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any, str]] = []

    def request(self, method: str, path: str, *, bearer: str, body: Any = None) -> Any:
        self.calls.append((method, path, body, bearer))
        if method == "DELETE":
            return 204, None, {}
        return (
            201,
            {
                "token": f"{_secret_value()}{len(self.calls)}",
                "expires_at": "2099-01-01T00:00:00Z",
                "permissions": {"contents": "read", "metadata": "read"},
            },
            {},
        )


class StaticCredentials:
    def __init__(self) -> None:
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )

    def describe(self) -> dict[str, Any]:
        return {}

    def read(self) -> AppCredential:
        return AppCredential(4242, self.pem)

    def write(self, **_: Any) -> dict[str, Any]:
        return {}


def test_the_checkout_token_is_one_repository_contents_read_and_never_cached() -> None:
    transport = RecordingTransport()
    auth = AppAuthenticator(
        AppConfig(app_id=0, private_key_path=""), transport, credentials=StaticCredentials()
    )
    first = auth.checkout_token(installation_id=7, repository="octo-lab/secret")
    second = auth.checkout_token(installation_id=7, repository="octo-lab/secret")
    mints = [c for c in transport.calls if c[0] == "POST"]
    assert len(mints) == 2, "a checkout token is minted fresh for every step"
    for _method, path, body, _bearer in mints:
        assert path == "/app/installations/7/access_tokens"
        assert body == {"repositories": ["secret"], "permissions": {"contents": "read"}}
    assert CHECKOUT_PERMISSIONS == {"contents": "read"}
    assert first.reveal() != second.reveal()
    assert first.repository == "octo-lab/secret"
    assert first.reveal() not in repr(first)

    assert auth.revoke(first) is True
    method, path, body, bearer = transport.calls[-1]
    assert (method, path, body) == ("DELETE", "/installation/token", None)
    assert bearer == first.reveal()
    first.discard()
    assert auth.revoke(first) is False, "an emptied token is never sent"


# ----- the helper -----------------------------------------------------------------


def _helper(tmp_path: Path) -> Path:
    body = scripts._CRED_HELPER_SCRIPT.replace("/tmp/cred-helper.sh", str(tmp_path / "helper"))
    subprocess.run(["sh", "-c", body], check=True)
    return tmp_path / "helper"


@pytest.mark.parametrize(
    ("operation", "protocol", "host", "answers"),
    [
        ("get", "https", "github.com", True),
        ("get", "http", "github.com", False),
        ("get", "https", "example.com", False),
        ("get", "https", "github.com:8443", False),
        ("get", "https", "github.com.example.com", False),
        ("store", "https", "github.com", False),
        ("erase", "https", "github.com", False),
    ],
)
def test_the_helper_answers_only_https_on_the_one_host(
    tmp_path: Path, operation: str, protocol: str, host: str, answers: bool
) -> None:
    helper = _helper(tmp_path)
    token_file = tmp_path / "token"
    token_file.write_text(_secret_value(), encoding="utf-8")
    result = subprocess.run(
        ["sh", str(helper), operation],
        input=f"protocol={protocol}\nhost={host}\npath=octo-lab/secret.git\n\n",
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "CRUCIBLE_TOKEN_FILE": str(token_file),
            "CRUCIBLE_CREDENTIAL_HOST": "github.com",
        },
    )
    if answers:
        assert result.stdout == f"username=x-access-token\npassword={_secret_value()}\n"
    else:
        assert result.stdout == ""


# ----- the preparer script's token lifecycle ---------------------------------------


def _origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    (origin / "README").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "b"],
        cwd=origin,
        check=True,
    )
    return origin


def local_preparer(
    tmp_path: Path, url: str, *, base_ref: str = "main", source: str = "stdin"
) -> tuple[str, Path]:
    """The rendered preparer with every absolute path moved under `tmp_path`, so it runs
    on this host exactly as it would in the container."""
    script = scripts.preparer_script(
        url=url,
        base_ref=base_ref,
        work_branch="crucible/test",
        from_remote_branch=False,
        cache_name=None,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
        origin_placeholder=workspace.ORIGIN_PLACEHOLDER,
        claude_md_wins=True,
        shims=workspace.SHIM_NAMES,
        exclude_entries=workspace.EXCLUDE_ENTRIES,
        identity_mount=IDENTITY_MOUNT,
        checkout_token=source,
        credential_host="github.com",
    )
    private_tmp = tmp_path / "container-tmp"
    private_tmp.mkdir()
    token_dir = tmp_path / "token-tmpfs"
    token_dir.mkdir()
    script = (
        script.replace("/tmp/gitconfig", f"{private_tmp}/gitconfig")
        .replace("/tmp/cred-helper.sh", f"{private_tmp}/cred-helper.sh")
        .replace("/crucible/work", str(tmp_path / "work"))
        .replace(scripts.TOKEN_MOUNT, str(token_dir))
    )
    return script, token_dir


def _nothing_holds_the_token(root: Path) -> None:
    value = _secret_value().encode()
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert value not in path.read_bytes(), f"{path} holds the token"


def test_the_preparer_removes_the_token_after_a_clean_prepare(tmp_path: Path) -> None:
    script, token_dir = local_preparer(tmp_path, str(_origin(tmp_path)))
    result = subprocess.run(
        ["sh", "-c", script], input=_secret_value(), capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert not (token_dir / "token").exists()
    assert not (tmp_path / "container-tmp" / "cred-helper.sh").exists()
    gitconfig = (tmp_path / "container-tmp" / "gitconfig").read_text(encoding="utf-8")
    assert "credential" not in gitconfig
    checkout = tmp_path / "work" / "repo"
    assert (checkout / "README").is_file()
    config = (checkout / ".git" / "config").read_text(encoding="utf-8")
    assert workspace.ORIGIN_PLACEHOLDER in config
    _nothing_holds_the_token(tmp_path / "work")
    assert _secret_value() not in result.stdout + result.stderr


def test_the_preparer_removes_the_token_when_the_prepare_fails(tmp_path: Path) -> None:
    script, token_dir = local_preparer(tmp_path, str(_origin(tmp_path)), base_ref="missing")
    result = subprocess.run(
        ["sh", "-c", script], input=_secret_value(), capture_output=True, text=True, check=False
    )
    assert result.returncode == 3 and "base ref missing does not exist" in result.stderr
    assert not (token_dir / "token").exists()
    assert not (tmp_path / "container-tmp" / "cred-helper.sh").exists()
    _nothing_holds_the_token(tmp_path / "work")


def test_the_preparer_removes_the_token_when_the_clone_fails(tmp_path: Path) -> None:
    script, token_dir = local_preparer(tmp_path, str(tmp_path / "no-such-origin"))
    result = subprocess.run(
        ["sh", "-c", script], input=_secret_value(), capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert not (token_dir / "token").exists()
    assert not (tmp_path / "container-tmp" / "cred-helper.sh").exists()


def test_a_private_preparer_with_no_token_stops_before_git(tmp_path: Path) -> None:
    script, _token_dir = local_preparer(tmp_path, str(_origin(tmp_path)))
    result = subprocess.run(
        ["sh", "-c", script], input="", capture_output=True, text=True, check=False
    )
    assert result.returncode == 3
    assert "no checkout token arrived" in result.stderr
    assert not (tmp_path / "work" / "repo").exists()


def test_the_token_is_never_on_a_command_line_or_in_the_script() -> None:
    for source in ("stdin", "file"):
        script = scripts.preparer_script(
            url="https://github.com/octo-lab/secret",
            base_ref="main",
            work_branch="crucible/test",
            from_remote_branch=False,
            cache_name="0123456789abcdef",
            author_name="crucible-worker",
            author_email="crucible-worker@users.noreply.github.com",
            origin_placeholder=workspace.ORIGIN_PLACEHOLDER,
            claude_md_wins=True,
            shims=workspace.SHIM_NAMES,
            exclude_entries=workspace.EXCLUDE_ENTRIES,
            identity_mount=IDENTITY_MOUNT,
            checkout_token=source,
        )
        assert "ghs_" not in script
        assert "unset GIT_TRACE GIT_TRACE_CURL GIT_CURL_VERBOSE" in script
        assert "trap drop_checkout_token EXIT" in script
        # The token is gone before the checkout is positioned and the shims written.
        assert script.index('drop_checkout_token\ncd "$REPO"') < script.index("checkout -B")


def test_a_public_repository_is_prepared_with_no_credential_at_all() -> None:
    script = scripts.preparer_script(
        url="https://github.com/octo-lab/widgets",
        base_ref="main",
        work_branch="crucible/test",
        from_remote_branch=False,
        cache_name="0123456789abcdef",
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
        origin_placeholder=workspace.ORIGIN_PLACEHOLDER,
        claude_md_wins=True,
        shims=workspace.SHIM_NAMES,
        exclude_entries=workspace.EXCLUDE_ENTRIES,
        identity_mount=IDENTITY_MOUNT,
    )
    for absent in ("cred-helper", "CRUCIBLE_TOKEN_FILE", scripts.TOKEN_MOUNT, "[credential]"):
        assert absent not in script
    refresh = scripts.cache_refresh_script(
        url="https://github.com/octo-lab/widgets", cache_name="0123456789abcdef"
    )
    assert "cred-helper" not in refresh and scripts.TOKEN_MOUNT not in refresh


# ----- the Docker provider --------------------------------------------------------


class PreparingClient(StubClient):
    """The stub daemon, plus the preparer's own output, plus what reached stdin."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.stdin: list[tuple[str, bytes]] = []

    def create_container(self, name: str, body: dict[str, Any]) -> str:
        if name.startswith("crucible-preparer-"):
            output = self.root / "workspaces" / name.removeprefix("crucible-preparer-") / "output"
            output.mkdir(parents=True, exist_ok=True)
            (output / "prepared-head.txt").write_text("a" * 40 + "\n", encoding="utf-8")
            (output / "started-from.txt").write_text("main\n", encoding="utf-8")
        return super().create_container(name, body)

    def write_stdin(self, container_id: str, payload: bytes) -> None:
        self.stdin.append((container_id, payload))


def _docker(tmp_path: Path) -> tuple[PreparingClient, DockerProvider]:
    client = PreparingClient(tmp_path)
    provider = DockerProvider(docker_config(tmp_path), client=client)  # type: ignore[arg-type]
    return client, provider


async def test_docker_hands_the_token_to_the_preparer_alone_on_stdin(tmp_path: Path) -> None:
    client, provider = _docker(tmp_path)
    launch = replace(docker_spec(), repository_url="https://github.com/octo-lab/secret")
    token = _token()
    ws = await provider.prepare(launch, checkout_token=token)
    await provider.launch(ws, launch)

    # hades #137: the cache refresher and the preparer are the two containers that talk
    # to the remote, and each gets the token on its own stdin and tmpfs.
    refresher = next(c for c in client.created if c["name"].startswith("crucible-cache-"))
    preparer = next(c for c in client.created if c["name"].startswith("crucible-preparer-"))
    for created in (refresher, preparer):
        body = created["body"]
        tmpfs = body["HostConfig"]["Tmpfs"][scripts.TOKEN_MOUNT]
        assert "noexec" in tmpfs and "mode=0700" in tmpfs and "uid=1000" in tmpfs
        assert body["OpenStdin"] is True and body["StdinOnce"] is True
        assert "CRUCIBLE_TOKEN_FILE" in body["Cmd"][-1]
    assert client.stdin == [
        ("container-1", _secret_value().encode()),
        ("container-2", _secret_value().encode()),
    ]

    for created in client.created:
        text = json.dumps(created["body"])
        assert _secret_value() not in text, f"{created['name']} carries the token"
    worker = next(c for c in client.created if c["name"] == f"crucible-{launch.attempt_id}")
    assert scripts.TOKEN_MOUNT not in worker["body"]["HostConfig"]["Tmpfs"]
    assert not worker["body"].get("OpenStdin")
    _nothing_holds_the_token(tmp_path)


async def test_docker_prepares_a_public_repository_with_nothing_on_stdin(tmp_path: Path) -> None:
    client, provider = _docker(tmp_path)
    await provider.prepare(docker_spec())
    assert len(client.created) == 2  # the cache refresher, then the preparer (hades #137)
    for created in client.created:
        body = created["body"]
        assert scripts.TOKEN_MOUNT not in body["HostConfig"]["Tmpfs"]
        assert "OpenStdin" not in body
    assert client.stdin == []


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/octo-lab/secret",
        "https://example.com/octo-lab/secret",
        "/repos/example.git",
        "git@github.com:octo-lab/secret.git",
    ],
)
async def test_docker_refuses_a_private_url_the_helper_would_not_answer(
    tmp_path: Path, url: str
) -> None:
    client, provider = _docker(tmp_path)
    launch = replace(docker_spec(), repository_url=url)
    with pytest.raises(ProviderError, match=r"over https from github\.com"):
        await provider.prepare(launch, checkout_token=_token())
    assert client.created == [] and client.stdin == []


# ----- the Kubernetes provider ----------------------------------------------------


def _k8s_config(**overrides: Any) -> KubernetesConfig:
    return KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        cache_claim="crucible-reference-cache",
        **overrides,
    )


SECRET_NAME = k8sspec.object_name("checkout", kf.ATTEMPT)


def _pods(api: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in api.created:
        if row["kind"] == "jobs":
            out[str(row["name"])] = row["body"]["spec"]["template"]["spec"]
        elif row["kind"] == "pods":
            out[str(row["name"])] = row["body"]["spec"]
    return out


def _token_volume(pod: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        v
        for v in pod.get("volumes") or []
        if (v.get("secret") or {}).get("secretName") == SECRET_NAME
    ]


async def test_kubernetes_mounts_the_token_secret_into_the_git_pods_only() -> None:
    api, _registry, provider = kf.build(config=_k8s_config())
    launch = kf.spec()
    ws = await provider.prepare(launch, checkout_token=_token())

    secrets_made = kf.created(api, "secrets", "checkout-")
    assert len(secrets_made) == 1
    made = secrets_made[0]
    assert made["metadata"]["name"] == SECRET_NAME
    assert made["metadata"]["labels"][k8sspec.LABEL_ATTEMPT] == kf.ATTEMPT
    assert set(made["data"]) == {"token"}
    assert not api.secret_exists(SECRET_NAME), "the Secret outlived the preparation step"

    pods = _pods(api)
    refresher = next(p for n, p in pods.items() if n.startswith("refresh-cache-"))
    preparer = next(p for n, p in pods.items() if n.startswith("prepare-"))
    for pod in (refresher, preparer):
        (volume,) = _token_volume(pod)
        assert volume["secret"]["defaultMode"] == 0o400
        mount = next(
            m for c in pod["containers"] for m in c["volumeMounts"] if m["name"] == volume["name"]
        )
        assert mount["mountPath"] == scripts.TOKEN_MOUNT and mount["readOnly"] is True
        script = pod["containers"][0]["command"][-1]
        assert "CRUCIBLE_TOKEN_FILE" in script and 'cat > "$CRUCIBLE_TOKEN_FILE"' not in script

    await provider.launch(ws, launch)
    pods = _pods(api)
    for name, pod in pods.items():
        if name.startswith(("refresh-cache-", "prepare-")):
            continue
        assert not _token_volume(pod), f"{name} mounts the checkout token"
    worker = next(p for n, p in pods.items() if n.startswith("worker-"))
    assert scripts.TOKEN_MOUNT not in json.dumps(worker)
    for row in api.created:
        if row["kind"] == "secrets":
            continue
        assert _secret_value() not in json.dumps(row["body"]), f"{row['name']} carries it"


@pytest.mark.parametrize("break_it", ["preparer-fails", "no-head"])
async def test_kubernetes_deletes_the_token_secret_when_the_prepare_fails(break_it: str) -> None:
    api, _registry, provider = kf.build(config=_k8s_config())
    launch = kf.spec()
    if break_it == "preparer-fails":
        api.script(launch.attempt_id, "prepare-fails")
    else:
        api.claims_suppress_head = True
    with pytest.raises(ProviderError):
        await provider.prepare(launch, checkout_token=_token())
    assert kf.created(api, "secrets", "checkout-"), "the Secret was never made"
    assert not api.secret_exists(SECRET_NAME)


async def test_kubernetes_prepares_a_public_repository_with_no_secret() -> None:
    api, _registry, provider = kf.build(config=_k8s_config())
    await provider.prepare(kf.spec())
    assert not kf.created(api, "secrets", "checkout-")
    for pod in _pods(api).values():
        assert not _token_volume(pod)
        assert "CRUCIBLE_TOKEN_FILE" not in pod["containers"][0]["command"][-1]


async def test_kubernetes_lets_the_git_pods_reach_a_configured_credential_host() -> None:
    hosts = {**kf.HOST_ADDRESSES, "git.example.test": ["203.0.113.9/32"]}

    def resolver(host: str) -> list[str]:
        return list(hosts.get(host, []))

    api, _registry, provider = kf.build(
        config=_k8s_config(credential_host="git.example.test:8443"), resolver=resolver
    )
    launch = replace(kf.spec(), repository_url="https://git.example.test:8443/octo-lab/secret")
    await provider.prepare(launch, checkout_token=_token())
    policies = kf.created(api, "networkpolicies")
    preparer = next(p for p in policies if "prepare" in p["metadata"]["name"])
    rendered = json.dumps(preparer)
    assert "203.0.113.9/32" in rendered and "8443" in rendered
    # A public repository's preparer gets no such rule.
    api2, _registry2, provider2 = kf.build(
        config=_k8s_config(credential_host="git.example.test:8443"), resolver=resolver
    )
    await provider2.prepare(kf.spec())
    policies2 = kf.created(api2, "networkpolicies")
    preparer2 = next(p for p in policies2 if "prepare" in p["metadata"]["name"])
    assert "203.0.113.9/32" not in json.dumps(preparer2)


async def test_kubernetes_refuses_a_private_url_off_the_credential_host() -> None:
    api, _registry, provider = kf.build(config=_k8s_config())
    launch = replace(kf.spec(), repository_url="https://example.com/octo-lab/secret")
    with pytest.raises(ProviderError, match=r"over https from github\.com"):
        await provider.prepare(launch, checkout_token=_token())
    assert not kf.created(api, "secrets")


# ----- the failure paths the review asked for ---------------------------------------


class StdinRefusedClient(PreparingClient):
    def write_stdin(self, container_id: str, payload: bytes) -> None:
        raise OSError("the attach connection was reset")


async def test_docker_removes_the_preparer_when_the_token_cannot_be_written(
    tmp_path: Path,
) -> None:
    client = StdinRefusedClient(tmp_path)
    provider = DockerProvider(docker_config(tmp_path), client=client)  # type: ignore[arg-type]
    launch = replace(docker_spec(), repository_url="https://github.com/octo-lab/secret")
    with pytest.raises(ProviderError, match="preparer container could not build"):
        await provider.prepare(launch, checkout_token=_token())
    # hades #137: the refresher's token write failed first, which only costs the
    # refresh; the preparer's is what fails the prepare. Neither container outlives it.
    assert [c["name"].split("-")[1] for c in client.created] == ["cache", "preparer"]
    assert client.removed == ["container-1", "container-2"]
    assert client.killed == ["container-1", "container-2"]


async def test_kubernetes_deletes_the_token_secret_when_prepare_is_cancelled() -> None:
    import asyncio  # noqa: PLC0415

    api, _registry, provider = kf.build(config=_k8s_config())

    async def cancelled(*_: Any, **__: Any) -> Any:
        assert api.secret_exists(SECRET_NAME), "the Secret should exist while the step runs"
        raise asyncio.CancelledError

    provider._run_preparer = cancelled  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await provider.prepare(kf.spec(), checkout_token=_token())
    assert not api.secret_exists(SECRET_NAME)


async def test_kubernetes_launches_no_worker_beside_a_secret_it_could_not_delete() -> None:
    from crucible.adapters.execution.k8sapi import KubernetesApiError  # noqa: PLC0415

    api, _registry, provider = kf.build(config=_k8s_config())
    original = api.delete
    created: list[bool] = []

    def refuse_once_made(kind: str, name: str, **kw: Any) -> None:
        if kind == "secrets" and name == SECRET_NAME and api.secret_exists(name):
            created.append(True)
            raise KubernetesApiError(500, "etcd is unhappy")
        original(kind, name, **kw)

    api.delete = refuse_once_made  # type: ignore[method-assign]
    with pytest.raises(ProviderError, match="no worker is launched beside it"):
        await provider.prepare(kf.spec(), checkout_token=_token())
    assert created, "the deletion was never attempted"
    assert not kf.created(api, "jobs", "worker-")


async def test_kubernetes_discard_and_cleanup_remove_a_leftover_token_secret() -> None:
    from crucible.ports.execution import CleanupPolicy  # noqa: PLC0415

    for finish in ("discard", "cleanup"):
        api, _registry, provider = kf.build(config=_k8s_config())
        launch = kf.spec()
        ws = await provider.prepare(launch)
        api.create(
            "secrets",
            k8sspec.secret(
                name=SECRET_NAME,
                namespace="crucible-workers",
                object_labels=k8sspec.labels(launch, k8sspec.ROLE_PREPARER),
                data={"token": b"revoked"},
            ),
        )
        assert api.secret_exists(SECRET_NAME)
        if finish == "discard":
            await provider.discard(ws, launch)
        else:
            await provider.cleanup(ws, CleanupPolicy.DELETE, launch)
        assert not api.secret_exists(SECRET_NAME), finish


@pytest.mark.parametrize(
    "url",
    ["https://GitHub.com/octo-lab/secret", "https://github.com:443/octo-lab/secret"],
)
def test_the_url_check_matches_what_the_helper_is_given(url: str) -> None:
    """git gives the helper the host as the URL writes it, port included, so the check
    compares exactly: a spelling the helper would not answer is refused up front."""
    with pytest.raises(ProviderError, match="over https from github"):
        workspace.require_checkout_url(url, "github.com")
    workspace.require_checkout_url("https://github.com/octo-lab/secret", "github.com")
    workspace.require_checkout_url("https://x-access-token@github.com/o/r", "github.com")
