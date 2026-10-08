"""The per-attempt credential copy (12) against a stub daemon, and the launch wrapper
under the host's own bash.

What is asserted: only the named auth files are seeded, owned by the worker's uid and
mode 600; the create request carries a name and a path for the token variable and never
a value; the copy mounts ro or rw-narrow at the harness's path with the template
read-only on top; a missing credential refuses the launch; the sync-back writes back
only a valid file with a newer issued-at, atomically and mode 600; and the wrapper fills
the variable inside the container without the value reaching argv.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution.docker import (
    LAUNCH_WRAPPER,
    DockerConfig,
    DockerProvider,
    _CredentialCopy,
    _seed_tar,
    _sync_one,
)
from crucible.adapters.execution.dockerapi import DockerApiError
from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.domain.secrets import scan_text
from crucible.ports.execution import LaunchRefusedError, LaunchSpec, ProviderError, Workspace
from crucible.ports.harness import CredentialSource, MountMode
from tests.fixtures import contract_document

CODEX_IMAGE = "crucible-worker:codex-0.153.4-abc"
DIGEST = "crucible-worker@sha256:" + "c" * 64


def _token(prefix: str, count: int = 40) -> str:
    """A secret-shaped value built at runtime; nothing committed is itself one."""
    return prefix + "x" * count


class StubClient:
    def __init__(self, harness: str = "codex", version: str = "0.153.4") -> None:
        self.labels = {"crucible.harness": harness, "crucible.harness_version": version}
        self.created: list[dict[str, Any]] = []
        self.started: list[str] = []
        self.removed: list[str] = []
        self.archives: list[tuple[str, str, bytes]] = []

    def inspect_image(self, reference: str) -> dict[str, Any]:
        return {
            "Id": "sha256:" + "a" * 64,
            "RepoDigests": [DIGEST],
            "Config": {"Labels": self.labels},
        }

    def inspect_network(self, name: str) -> dict[str, Any]:
        return {"Id": "net"}

    def create_container(self, name: str, body: dict[str, Any]) -> str:
        self.created.append({"name": name, "body": body})
        return f"container-{len(self.created)}"

    def put_archive(self, container_id: str, path: str, tar: bytes) -> None:
        self.archives.append((container_id, path, tar))

    def start_container(self, container_id: str) -> None:
        self.started.append(container_id)

    def remove_container(self, container_id: str, *, force: bool = True) -> None:
        self.removed.append(container_id)


def config(tmp_path: Path, **credentials: CredentialSource) -> DockerConfig:
    if "codex" in credentials and credentials["codex"].mount_mode is None:
        credentials["codex"] = CredentialSource(credentials["codex"].path, MountMode.RW_NARROW)
    return DockerConfig(
        endpoint="tcp://127.0.0.1:1",
        artifact_root=str(tmp_path),
        mount_kind="bind",
        artifact_host_root=str(tmp_path),
        artifact_volume="",
        credentials=credentials,
    )


def spec(harness: str = "codex", image: str = CODEX_IMAGE, **fields: Any) -> LaunchSpec:
    document = contract_document()
    document["repository"]["work_branch"] = "crucible/EX-0001"
    return LaunchSpec(
        attempt_id="01ATTEMPT0000000000000000A",
        task_id="01TASK00000000000000000000",
        external_id="EX-0001",
        role="implement",
        harness=harness,
        model="model-x",
        image=image,
        timeout_seconds=600,
        contract=document,
        policy={"images": {"allowlist": ["crucible-worker:*"]}},
        repository_url="/repos/example.git",
        **fields,
    )


def workspace(tmp_path: Path, attempt_id: str) -> Workspace:
    root = tmp_path / "workspaces" / attempt_id
    for leaf in ("repo", "identity", "report", "output", "credential"):
        (root / leaf).mkdir(parents=True, exist_ok=True)
    return Workspace(
        attempt_id=attempt_id,
        checkout_path=str(root / "repo"),
        identity_path=str(root / "identity"),
        report_path=str(root / "report"),
        output_path=str(root / "output"),
        work_branch="crucible/EX-0001",
    )


def codex_source(tmp_path: Path, *, last_refresh: str = "2026-09-16T22:00:00Z") -> Path:
    source = tmp_path / "credentials" / "codex"
    source.mkdir(parents=True)
    (source / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": last_refresh,
                "tokens": {"access_token": _token("eyJ"), "refresh_token": _token("rt-")},
            }
        ),
        encoding="utf-8",
    )
    # Files the CLI writes beside its auth file and that must never be seeded (S1).
    (source / "config.toml").write_text("[projects]\n", encoding="utf-8")
    (source / "state_5.sqlite").write_bytes(b"\x00" * 16)
    return source


def members(tar: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r") as archive:
        return {m.name: m for m in archive.getmembers()}


# ----- seeding ------------------------------------------------------------------


async def test_only_the_named_auth_files_are_seeded_as_uid_1000_mode_600(tmp_path: Path) -> None:
    source = codex_source(tmp_path)
    client = StubClient()
    provider = DockerProvider(
        config(tmp_path, codex=CredentialSource(str(source))),
        client=client,  # type: ignore[arg-type]
    )
    launch = spec()
    ws = workspace(tmp_path, launch.attempt_id)
    handle = await provider.launch(ws, launch)
    assert handle.image_digest == DIGEST
    # Seeded after create, before start, into the mount target (12).
    assert [c for c, _, _ in client.archives] == ["container-1"] and client.started == [
        "container-1"
    ]
    _, path, tar = client.archives[0]
    assert path == "/home/worker/.codex"
    seeded = members(tar)
    assert set(seeded) == {"auth.json"}, "the named auth file and nothing else"
    assert seeded["auth.json"].uid == 1000 and seeded["auth.json"].gid == 1000
    assert seeded["auth.json"].mode == 0o600


async def test_the_create_request_names_the_variable_and_the_path_never_the_value(
    tmp_path: Path,
) -> None:
    source = tmp_path / "credentials" / "claude_code"
    source.mkdir(parents=True)
    token = _token("sk-ant-oat01-")
    (source / "oauth-token").write_text(token + "\n", encoding="utf-8")
    (source / ".claude.json").write_text('{"hasCompletedOnboarding": true}\n', encoding="utf-8")
    client = StubClient("claude_code", "2.1.277")
    provider = DockerProvider(
        config(tmp_path, claude_code=CredentialSource(str(source))),
        client=client,  # type: ignore[arg-type]
    )
    adapter = ClaudeCodeAdapter()
    launch_shape = adapter.build_launch(provider._launch_context(spec("claude_code")))
    launch = spec(
        "claude_code",
        image="crucible-worker:claude_code-2.1.277-abc",
        command=launch_shape.argv,
        env=dict(launch_shape.env),
        env_from_files=dict(launch_shape.env_from_files),
        stdin_text=launch_shape.stdin_text,
        transcript_path=launch_shape.transcript_path,
    )
    await provider.launch(workspace(tmp_path, launch.attempt_id), launch)
    body = client.created[0]["body"]
    blob = json.dumps(body)
    assert token not in blob
    assert scan_text(blob) is None
    env = dict(e.split("=", 1) for e in body["Env"])
    assert (
        env["CRUCIBLE_ENV_FROM_FILES"] == "CLAUDE_CODE_OAUTH_TOKEN=/home/worker/.claude/oauth-token"
    )
    assert env["CLAUDE_CONFIG_DIR"] == "/home/worker/.claude"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert body["Cmd"][:5] == ["bash", "-o", "pipefail", "-c", LAUNCH_WRAPPER]
    assert body["Cmd"][6:] == list(launch_shape.argv)
    mounts = {m["Target"]: m for m in body["HostConfig"]["Mounts"]}
    copy = mounts["/home/worker/.claude"]
    assert copy["ReadOnly"] is True and copy["Source"].endswith("/credential")
    template = mounts["/home/worker/.claude/settings.json"]
    assert template["ReadOnly"] is True and template["Source"].endswith(
        "identity/harness/settings.json"
    )


async def test_agy_mounts_rw_narrow_and_a_configured_ro_never_lowers_it(
    tmp_path: Path,
) -> None:
    """12 and 25 step 7: rw-narrow since the live run showed the token rotating; the
    configuration may not lower the adapter's minimum."""
    source = tmp_path / "credentials" / "agy"
    (source / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (source / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text(
        json.dumps({"token": {"access_token": _token("ya29."), "expiry": "2026-09-17T00:00:00Z"}}),
        encoding="utf-8",
    )
    for mode, expected in ((None, False), (MountMode.RO, False), (MountMode.RW_NARROW, False)):
        client = StubClient("agy", "1.2.4")
        provider = DockerProvider(
            config(tmp_path, agy=CredentialSource(str(source), mode)),
            client=client,  # type: ignore[arg-type]
        )
        launch = spec("agy", image="crucible-worker:agy-1.2.4-abc")
        await provider.launch(workspace(tmp_path, launch.attempt_id), launch)
        mounts = {m["Target"]: m for m in client.created[0]["body"]["HostConfig"]["Mounts"]}
        assert mounts["/home/worker/.gemini"]["ReadOnly"] is expected
        seeded = members(client.archives[0][2])
        assert set(seeded) == {"antigravity-cli", "antigravity-cli/antigravity-oauth-token"}
        assert seeded["antigravity-cli"].isdir() and seeded["antigravity-cli"].mode == 0o700


async def test_an_empty_optional_credential_directory_is_not_mounted(tmp_path: Path) -> None:
    """Hermes's key is optional and Compose creates its directory empty. An empty
    directory is no credential: nothing is mounted or seeded, so the launch keeps the
    no-key fallback instead of refusing over a missing `api-key`."""
    source = tmp_path / "credentials" / "hermes"
    source.mkdir(parents=True)
    provider = DockerProvider(config(tmp_path, hermes=CredentialSource(str(source))))
    launch = spec("hermes", image="crucible-worker:hermes-0.19.0-abc")
    assert provider._credential_copy(launch) is None
    assert provider._credential_mounts(launch) == []
    assert not await provider.credential_available("hermes")
    assert provider._launch_context(launch).credential_mounted is False

    (source / "api-key").write_text("placeholder-for-the-test\n", encoding="utf-8")
    assert provider._credential_copy(launch) is not None
    assert await provider.credential_available("hermes")
    assert provider._launch_context(launch).credential_mounted is True


async def test_a_harness_with_no_configured_credential_is_refused(tmp_path: Path) -> None:
    client = StubClient()
    provider = DockerProvider(config(tmp_path), client=client)  # type: ignore[arg-type]
    launch = spec()
    with pytest.raises(LaunchRefusedError, match="no credential directory is configured"):
        await provider.launch(workspace(tmp_path, launch.attempt_id), launch)
    assert client.started == []


async def test_a_missing_required_auth_file_refuses_and_removes_the_container(
    tmp_path: Path,
) -> None:
    source = tmp_path / "credentials" / "codex"
    source.mkdir(parents=True)
    client = StubClient()
    provider = DockerProvider(
        config(tmp_path, codex=CredentialSource(str(source))),
        client=client,  # type: ignore[arg-type]
    )
    launch = spec()
    with pytest.raises(LaunchRefusedError, match=r"missing its auth file 'auth\.json'"):
        await provider.launch(workspace(tmp_path, launch.attempt_id), launch)
    assert client.removed == ["container-1"] and client.started == []


async def test_the_script_harness_gets_no_credential_and_no_wrapper(tmp_path: Path) -> None:
    client = StubClient("script-harness", "1.0.0")
    provider = DockerProvider(config(tmp_path), client=client)  # type: ignore[arg-type]
    launch = spec(
        "script-harness",
        image="crucible-worker:script-harness-1.0.0-abc",
        command=("crucible-script-harness",),
    )
    await provider.launch(workspace(tmp_path, launch.attempt_id), launch)
    body = client.created[0]["body"]
    assert body["Cmd"] == ["crucible-script-harness"] and client.archives == []
    assert all("/home/worker/." not in m["Target"] for m in body["HostConfig"]["Mounts"])


async def test_a_version_outside_the_range_is_a_refusal(tmp_path: Path) -> None:
    client = StubClient("codex", "0.157.0")
    provider = DockerProvider(
        config(tmp_path, codex=CredentialSource(str(codex_source(tmp_path)))),
        client=client,  # type: ignore[arg-type]
    )
    launch = spec()
    with pytest.raises(LaunchRefusedError, match="outside the tested range"):
        await provider.launch(workspace(tmp_path, launch.attempt_id), launch)


# ----- sync-back (12) ---------------------------------------------------------------


def copy_for(source: Path, adapter: Any = None) -> _CredentialCopy:
    adapter = adapter or CodexAdapter()
    credential = adapter.credential_spec()
    return _CredentialCopy(
        spec=credential,
        source=CredentialSource(str(source)),
        mode=MountMode.RW_NARROW,
        seeded={},
    )


def test_an_unchanged_file_is_left_alone(tmp_path: Path) -> None:
    source = codex_source(tmp_path)
    copy = copy_for(source)
    before = (source / "auth.json").read_bytes()
    result = _sync_one(copy, copy.spec.auth_files[0], before)
    assert not result.changed and not result.synced and result.reason == "unchanged"


def test_a_newer_valid_file_is_written_back_atomically_mode_600(tmp_path: Path) -> None:
    source = codex_source(tmp_path, last_refresh="2026-09-16T22:00:00Z")
    copy = copy_for(source)
    rotated = json.dumps(
        {
            "auth_mode": "chatgpt",
            "last_refresh": "2026-09-17T05:01:10Z",
            "tokens": {"access_token": _token("eyJ", 50), "refresh_token": _token("rt2-")},
        }
    ).encode()
    result = _sync_one(copy, copy.spec.auth_files[0], rotated)
    assert result.changed and result.valid and result.synced
    assert (source / "auth.json").read_bytes() == rotated
    assert oct(os.stat(source / "auth.json").st_mode & 0o777) == "0o600"
    # hades #315: the per-attempt temporary name is gone once the replace lands; only
    # the shared lock file (never a credential) may remain.
    assert [p.name for p in source.glob("auth.json.crucible-sync*")] == [
        "auth.json.crucible-sync.lock"
    ]


def test_an_older_file_never_overwrites_a_newer_source(tmp_path: Path) -> None:
    """12: chosen by the newest issued-at, never by exit order."""
    source = codex_source(tmp_path, last_refresh="2026-09-17T06:00:00Z")
    copy = copy_for(source)
    before = (source / "auth.json").read_bytes()
    stale = json.dumps(
        {"auth_mode": "chatgpt", "last_refresh": "2026-09-17T05:00:00Z", "tokens": {}}
    ).encode()
    result = _sync_one(copy, copy.spec.auth_files[0], stale)
    assert result.changed and not result.synced and "not newer" in result.reason
    assert (source / "auth.json").read_bytes() == before


def test_a_file_that_is_not_the_expected_shape_is_never_written(tmp_path: Path) -> None:
    source = codex_source(tmp_path)
    copy = copy_for(source)
    before = (source / "auth.json").read_bytes()
    for junk in (b"not json", b'{"last_refresh": "2027-01-01T00:00:00Z"}', b"[]"):
        result = _sync_one(copy, copy.spec.auth_files[0], junk)
        assert result.changed and not result.valid and not result.synced
    assert (source / "auth.json").read_bytes() == before


def test_a_state_file_is_never_written_back(tmp_path: Path) -> None:
    source = tmp_path / "credentials" / "claude_code"
    source.mkdir(parents=True)
    (source / "oauth-token").write_text(_token("sk-ant-oat01-"), encoding="utf-8")
    (source / ".claude.json").write_text("{}", encoding="utf-8")
    copy = copy_for(source, ClaudeCodeAdapter())
    state = next(f for f in copy.spec.auth_files if f.name == ".claude.json")
    result = _sync_one(copy, state, b'{"projects": {"/crucible/repo": {}}}')
    assert result.changed and not result.synced and "never written back" in result.reason
    assert (source / ".claude.json").read_text(encoding="utf-8") == "{}"


def test_agy_orders_by_the_token_expiry(tmp_path: Path) -> None:
    source = tmp_path / "credentials" / "agy"
    inner = source / ".gemini" / "antigravity-cli"
    inner.mkdir(parents=True)
    token_file = inner / "antigravity-oauth-token"
    token_file.write_text(
        json.dumps({"token": {"access_token": _token("ya29."), "expiry": "2026-09-16T22:19:00Z"}}),
        encoding="utf-8",
    )
    copy = _CredentialCopy(
        spec=AgyAdapter().credential_spec(),
        source=CredentialSource(str(source), MountMode.RW_NARROW),
        mode=MountMode.RW_NARROW,
        seeded={},
    )
    refreshed = json.dumps(
        {"token": {"access_token": _token("ya29.", 60), "expiry": "2026-09-17T06:19:36-05:00"}}
    ).encode()
    result = _sync_one(copy, copy.spec.auth_files[0], refreshed)
    assert result.synced, result.reason
    assert token_file.read_bytes() == refreshed


def test_the_seed_tar_holds_no_other_file_from_the_directory(tmp_path: Path) -> None:
    source = codex_source(tmp_path)
    tar, hashes = _seed_tar(CodexAdapter().credential_spec(), CredentialSource(str(source)))
    assert set(members(tar)) == {"auth.json"}
    assert set(hashes) == {"auth.json"} and len(hashes["auth.json"] or "") == 64


# ----- the launch wrapper, under the host's bash ------------------------------------


def run_wrapper(
    tmp_path: Path, argv: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-o", "pipefail", "-c", LAUNCH_WRAPPER, "crucible-launch", *argv],
        cwd=str(tmp_path),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def fake_harness(tmp_path: Path, exit_code: int = 0) -> Path:
    script = tmp_path / "harness.sh"
    script.write_text(
        "#!/bin/bash\n"
        # What the harness saw: whether the variable is set (never its value), the
        # bytes on stdin, and the argv it was given.
        'if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then echo TOKEN_SET; else echo TOKEN_UNSET; fi\n'
        'echo "STDIN<$(cat)>"\n'
        'echo "ARGS<$*>"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_the_wrapper_fills_the_variable_from_the_file_and_keeps_it_off_argv(tmp_path: Path) -> None:
    token = _token("sk-ant-oat01-")
    (tmp_path / "oauth-token").write_text(token + "\n", encoding="utf-8")
    (tmp_path / "IDENTITY.md").write_text("identity text", encoding="utf-8")
    harness = fake_harness(tmp_path)
    completed = run_wrapper(
        tmp_path,
        [str(harness), "--model", "m"],
        {
            "CRUCIBLE_ENV_FROM_FILES": f"CLAUDE_CODE_OAUTH_TOKEN={tmp_path / 'oauth-token'}",
            "CRUCIBLE_STDIN_FILES": str(tmp_path / "IDENTITY.md"),
            "CRUCIBLE_PROMPT": "Read IDENTITY.md and execute the task.",
            "CRUCIBLE_TRANSCRIPT": str(tmp_path / "transcript.jsonl"),
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert "TOKEN_SET" in completed.stdout
    # `$(cat)` in the fake harness strips the trailing newline the wrapper sends.
    assert "STDIN<identity text\nRead IDENTITY.md and execute the task.>" in completed.stdout
    assert "ARGS<--model m>" in completed.stdout
    assert token not in completed.stdout and token not in completed.stderr
    # The transcript is the harness's stdout, teed.
    assert (tmp_path / "transcript.jsonl").read_text(encoding="utf-8") == completed.stdout


def test_the_wrapper_propagates_the_harness_exit_code_through_the_tee(tmp_path: Path) -> None:
    harness = fake_harness(tmp_path, exit_code=75)
    completed = run_wrapper(
        tmp_path,
        [str(harness)],
        {"CRUCIBLE_PROMPT": "p", "CRUCIBLE_TRANSCRIPT": str(tmp_path / "t.jsonl")},
    )
    assert completed.returncode == 75
    assert "TOKEN_UNSET" in completed.stdout


def test_the_wrapper_with_nothing_on_stdin_closes_it(tmp_path: Path) -> None:
    harness = fake_harness(tmp_path)
    completed = run_wrapper(tmp_path, [str(harness), "a"], {})
    assert completed.returncode == 0
    assert "STDIN<>" in completed.stdout and "ARGS<a>" in completed.stdout


# ----- what a worker can leave at the credential path (review findings) ----------


class ReadBackClient(StubClient):
    """A stub whose archive read-back answers with what the worker left behind."""

    def __init__(self, answer: Any) -> None:
        super().__init__()
        self.answer = answer
        self.logs_calls = 0

    def get_archive(self, container_id: str, path: str, *, limit: int = 1024 * 1024) -> Any:
        return self.answer

    def container_logs(self, container_id: str, **kw: Any) -> list[Any]:
        return []

    def list_containers(self, **kw: Any) -> list[dict[str, Any]]:
        return []

    def wait_container(self, container_id: str, *, timeout: float) -> int:
        return 0


async def _sync_with(tmp_path: Path, answer: Any) -> Any:
    from crucible.ports.execution import Handle  # noqa: PLC0415

    source = codex_source(tmp_path)
    client = ReadBackClient(answer)
    provider = DockerProvider(
        config(tmp_path, codex=CredentialSource(str(source))),
        client=client,  # type: ignore[arg-type]
    )
    launch = spec()
    ws = workspace(tmp_path, launch.attempt_id)
    handle = Handle(provider="docker", ref="worker", attempt_id=launch.attempt_id)
    before = (source / "auth.json").read_bytes()
    sync = await provider._sync_credential(handle, ws, launch)
    assert (source / "auth.json").read_bytes() == before, "the source was never written"
    assert sync is not None
    return sync.files[0]


async def test_an_oversized_read_back_is_never_parsed(tmp_path: Path) -> None:
    from crucible.adapters.execution.dockerapi import ArchiveFile  # noqa: PLC0415

    result = await _sync_with(tmp_path, ArchiveFile(b"", {"mode": 0o600}, truncated=True))
    assert result.changed and not result.valid and not result.synced
    assert "read limit" in result.reason


async def test_a_symlink_at_the_credential_path_is_refused_by_the_stat_header(
    tmp_path: Path,
) -> None:
    from crucible.adapters.execution.dockerapi import ArchiveFile  # noqa: PLC0415

    # A tar whose one member claims to be a regular file, as the daemon would return
    # for a followed link; the stat header says symlink and that is what decides.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        payload = json.dumps({"tokens": {}, "last_refresh": "2027-01-01T00:00:00Z"}).encode()
        info = tarfile.TarInfo("auth.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    symlink_mode = 1 << 27
    result = await _sync_with(
        tmp_path, ArchiveFile(buffer.getvalue(), {"mode": symlink_mode | 0o777}, truncated=False)
    )
    assert result.changed and not result.valid and not result.synced
    assert "not a regular file" in result.reason


def test_the_archive_stat_header_is_decoded_and_a_regular_file_recognized() -> None:
    import base64  # noqa: PLC0415

    from crucible.adapters.execution.dockerapi import ArchiveFile, _path_stat  # noqa: PLC0415

    header = base64.b64encode(json.dumps({"name": "auth.json", "mode": 0o600}).encode()).decode()
    assert ArchiveFile(b"", _path_stat(header)).is_regular
    assert not ArchiveFile(b"", _path_stat(None)).is_regular
    assert not ArchiveFile(b"", {"mode": (1 << 31) | 0o755}).is_regular
    assert _path_stat("not base64!!") == {}


def test_policy_concurrency_declarations_match_every_adapter() -> None:
    from crucible.adapters.harness.registry import default_registry  # noqa: PLC0415
    from crucible.domain.harness_concurrency import HARNESS_CONCURRENCY  # noqa: PLC0415

    for adapter in default_registry(test_fixtures=True):
        declaration = HARNESS_CONCURRENCY[adapter.name]
        credential = adapter.credential_spec()
        if credential is not None:
            assert credential.minimum_mode.value == declaration.minimum_mode
        assert (
            getattr(adapter, "parallel_attempts_safe", None) == declaration.parallel_attempts_safe
        )


# ----- a launch that fails after seeding leaves no copy behind (review I1) --------


class FailingStartClient(ReadBackClient):
    def __init__(self) -> None:
        super().__init__(answer=None)
        self.cleaner_scripts: list[str] = []

    def create_container(self, name: str, body: dict[str, Any]) -> str:
        if body["Labels"].get("crucible.role") == "cleaner":
            self.cleaner_scripts.append(body["Cmd"][-1])
        return super().create_container(name, body)

    def start_container(self, container_id: str) -> None:
        if (
            self.created[int(container_id.split("-")[1]) - 1]["body"]["Labels"].get("crucible.role")
            == "worker"
        ):
            raise DockerApiError(500, "no such image layer")
        super().start_container(container_id)


async def test_a_start_that_fails_after_seeding_removes_the_copy_through_the_daemon(
    tmp_path: Path,
) -> None:
    source = codex_source(tmp_path)
    client = FailingStartClient()
    provider = DockerProvider(
        config(tmp_path, codex=CredentialSource(str(source))),
        client=client,  # type: ignore[arg-type]
    )
    launch = spec()
    ws = workspace(tmp_path, launch.attempt_id)
    # Hades #346: a refused /start is a start failure, still a ProviderError.
    with pytest.raises(ProviderError, match="the worker never started: no such image layer"):
        await provider.launch(ws, launch)
    assert len(client.archives) == 1, "the copy was seeded before the start failed"
    assert client.removed and client.removed[0] == "container-1"
    assert client.cleaner_scripts, "no cleaner ran to remove the seeded copy"
    assert "credential" in client.cleaner_scripts[0]


async def test_discard_removes_only_the_credential_leaf(tmp_path: Path) -> None:
    client = ReadBackClient(answer=None)
    provider = DockerProvider(config(tmp_path), client=client)  # type: ignore[arg-type]
    launch = spec("script-harness", image="crucible-worker:script-harness-1.0.0-abc")
    ws = workspace(tmp_path, launch.attempt_id)
    leaf = Path(ws.checkout_path).parent / "credential"
    (leaf / "auth.json").write_bytes(b"{}")
    (Path(ws.report_path) / "keep.txt").write_bytes(b"x")
    await provider.discard(ws, launch)
    assert not leaf.exists()
    assert (Path(ws.report_path) / "keep.txt").exists()


class TransportFailingClient(ReadBackClient):
    """The archive read-back dies on the transport, not on the API (review C2)."""

    def __init__(self) -> None:
        super().__init__(answer=None)
        self.cleaner_scripts: list[str] = []

    def get_archive(self, container_id: str, path: str, *, limit: int = 1024 * 1024) -> Any:
        raise TimeoutError("timed out")

    def create_container(self, name: str, body: dict[str, Any]) -> str:
        if body["Labels"].get("crucible.role") == "cleaner":
            self.cleaner_scripts.append(body["Cmd"][-1])
        return super().create_container(name, body)


async def test_a_transport_failure_during_read_back_still_removes_the_copy(tmp_path: Path) -> None:
    from crucible.ports.execution import Handle  # noqa: PLC0415

    source = codex_source(tmp_path)
    client = TransportFailingClient()
    provider = DockerProvider(
        config(tmp_path, codex=CredentialSource(str(source))),
        client=client,  # type: ignore[arg-type]
    )
    launch = spec()
    ws = workspace(tmp_path, launch.attempt_id)
    handle = Handle(provider="docker", ref="worker", attempt_id=launch.attempt_id)
    sync = await provider._sync_credential(handle, ws, launch)
    assert sync is not None
    assert [f.reason for f in sync.files] == ["read failed: TimeoutError: timed out"]
    assert client.cleaner_scripts and "credential" in client.cleaner_scripts[0]
