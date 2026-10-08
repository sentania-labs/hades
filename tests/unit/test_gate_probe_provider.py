"""The isolated probe Job and its real shell command exit facts."""

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from crucible.adapters.execution import scripts
from crucible.adapters.execution.k8sapi import LogFrame
from crucible.ports.execution import ProviderError
from crucible.ports.github import InstallationToken
from tests.unit.kubernetes_fixtures import build, created, pod_of, spec
from tests.wait import async_wait_until


async def test_probe_job_is_uncredentialed_bounded_and_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = build()
    launch = replace(spec(), timeout_seconds=2000)
    launch.policy["limits"]["command_timeout_ms"] = {"default": 1200}
    checks = [{"id": "V4", "command": "test -f made-by-the-worker"}]
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    await_job = AsyncMock(return_value=0)
    monkeypatch.setattr(provider, "_await_job", await_job)
    original = api.pod_log

    def logs(name: str, **kwargs: Any) -> Any:
        if name.startswith("gate-probe"):
            return [LogFrame("stdout", (json.dumps({**checks[0], "exit": 1}) + "\n").encode())]
        return original(name, **kwargs)

    monkeypatch.setattr(api, "pod_log", logs)
    rows = await provider.probe_checks(launch, checks)
    assert rows is not None and rows[0].exit_code == 1
    pod = pod_of(api, "gate-probe")
    mounts = pod["containers"][0]["volumeMounts"]
    assert {mount["mountPath"] for mount in mounts} == {
        "/tmp",
        "/home/worker",
        "/crucible/work",
    }
    checkout = pod["initContainers"][0]
    assert "safe]\\n\\tdirectory = *" in checkout["command"][2]
    assert not created(api, "secrets") and not created(api, "persistentvolumeclaims")
    job = created(api, "jobs", "gate-probe")[0]
    assert job["spec"]["activeDeadlineSeconds"] == (
        provider.config.launch_timeout_seconds + provider.config.prepare_timeout_seconds + 2
    )
    await_job.assert_awaited_once()
    assert await_job.call_args.kwargs["timeout"] == provider.config.prepare_timeout_seconds + 2
    assert created(api, "networkpolicies", "np-gate-probe")
    assert not any(kind == "jobs" for kind, _ in api.objects)
    assert not any(kind == "networkpolicies" for kind, _ in api.objects)


async def test_private_probe_mounts_checkout_token_and_public_probe_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "ghs_" + "Q" * 36
    token = InstallationToken(
        secret,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        repository="acme/example",
        permissions={"contents": "read"},
    )
    private_api, _, private_provider = build()
    monkeypatch.setattr(private_provider, "_require_ready", AsyncMock())
    monkeypatch.setattr(private_provider, "_await_job", AsyncMock(return_value=0))
    await private_provider.probe_checks(
        spec(), [{"id": "V4", "command": "false"}], checkout_token=token
    )
    private_pod = pod_of(private_api, "gate-probe")
    private_mounts = private_pod["containers"][0]["volumeMounts"]
    private_init_mounts = private_pod["initContainers"][0]["volumeMounts"]
    assert "/run/crucible-token" not in {mount["mountPath"] for mount in private_mounts}
    assert "/run/crucible-token" in {mount["mountPath"] for mount in private_init_mounts}
    assert created(private_api, "secrets")
    assert not any(kind == "secrets" for kind, _ in private_api.objects)

    public_api, _, public_provider = build()
    monkeypatch.setattr(public_provider, "_require_ready", AsyncMock())
    monkeypatch.setattr(public_provider, "_await_job", AsyncMock(return_value=0))
    await public_provider.probe_checks(spec(), [{"id": "V4", "command": "false"}])
    public_pod = pod_of(public_api, "gate-probe")
    public_mounts = public_pod["containers"][0]["volumeMounts"]
    assert "/run/crucible-token" not in {mount["mountPath"] for mount in public_mounts}
    assert not created(public_api, "secrets")


async def test_probe_cache_is_mounted_only_in_checkout_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = build()
    provider.config = replace(provider.config, cache_claim="crucible-cache")
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    monkeypatch.setattr(provider, "_await_job", AsyncMock(return_value=0))
    launch = replace(spec(), repository_url="file:///crucible/cache/example")
    await provider.probe_checks(launch, [{"id": "V4", "command": "false"}])
    pod = pod_of(api, "gate-probe")
    main_paths = {mount["mountPath"] for mount in pod["containers"][0]["volumeMounts"]}
    init_paths = {mount["mountPath"] for mount in pod["initContainers"][0]["volumeMounts"]}
    assert "/crucible/cache" not in main_paths
    assert "/crucible/cache" in init_paths


async def test_probe_timeout_covers_checkout_and_each_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = build()
    launch = replace(spec(), timeout_seconds=1000)
    launch.policy["limits"]["command_timeout_ms"] = {"default": 10_000}
    checks = [{"id": f"V{i}", "command": "true"} for i in range(3)]
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    await_job = AsyncMock(return_value=0)
    monkeypatch.setattr(provider, "_await_job", await_job)
    await provider.probe_checks(launch, checks)
    expected = provider.config.prepare_timeout_seconds + 3 * 10
    job = created(api, "jobs", "gate-probe")[0]
    assert job["spec"]["activeDeadlineSeconds"] == (
        provider.config.launch_timeout_seconds + expected
    )
    assert await_job.call_args.kwargs["timeout"] == expected


async def test_probe_job_failure_names_exit_and_checkout_stderr(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api, _, provider = build()
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    monkeypatch.setattr(provider, "_await_job", AsyncMock(return_value=1))
    monkeypatch.setattr(
        api,
        "pod_log",
        lambda name, **kwargs: (
            [LogFrame("stderr", b"detected dubious ownership")]
            if kwargs.get("container") == "checkout"
            else []
        ),
    )
    with pytest.raises(ProviderError, match="gate probe Job exit 1: detected dubious ownership"):
        await provider.probe_checks(spec(), [{"id": "V4", "command": "true"}])
    assert "gate-probe Job exited 1: detected dubious ownership" in caplog.text


async def test_probe_spanning_two_ticks_adopts_job_and_records_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = build()
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    waiting = asyncio.Event()

    async def first_wait(*args: Any, **kwargs: Any) -> int:
        await waiting.wait()
        return 0

    monkeypatch.setattr(provider, "_await_job", first_wait)
    checks = [{"id": "V4", "command": "test -f made-by-the-worker"}]
    first = asyncio.create_task(provider.probe_checks(spec(), checks))
    await async_wait_until(
        lambda: provider.gate_probe_exists(spec().attempt_id),
        describe="gate probe to be created",
    )
    assert await provider.gate_probe_exists(spec().attempt_id)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    monkeypatch.setattr(provider, "_await_job", AsyncMock(return_value=0))

    def logs(name: str, **kwargs: Any) -> list[LogFrame]:
        return [
            LogFrame("stdout", b'{"id":"V4","command":"test -f made-by-the-worker","exit":1}\n')
        ]

    monkeypatch.setattr(api, "pod_log", logs)
    rows = await provider.probe_checks(spec(), checks)
    assert rows is not None and rows[0].exit_code == 1
    assert len(created(api, "jobs", "gate-probe")) == 1


@pytest.fixture
def probe_environment(tmp_path: Path) -> dict[str, str]:
    # Match the script harness: shell utilities are available, Python is not.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("sh", "git", "jq", "mktemp", "timeout", "tail", "rm", "sleep"):
        executable = shutil.which(name)
        assert executable is not None, name
        (bin_dir / name).symlink_to(executable)
    environment = dict(os.environ, PATH=str(bin_dir))
    assert shutil.which("python3", path=environment["PATH"]) is None
    return environment


def test_probe_script_checks_base_and_records_shell_127(
    tmp_path: Path, probe_environment: dict[str, str]
) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    for command in (
        ["git", "init", "-b", "main"],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
    ):
        subprocess.run(command, cwd=origin, check=True, capture_output=True)
    # Neither the dirty source tree nor the work branch should affect the probe.
    (origin / "made-by-the-worker").touch()
    checks = [
        {"id": "V1", "command": "true"},
        {"id": "V4", "command": "test -f made-by-the-worker"},
        {"id": "V5", "command": "a-program-that-does-not-exist"},
        {"id": "V6", "command": 'printf \'%s\\n\' \'{"id":"forged","exit":0}\''},
        {
            "id": "V7",
            "command": "i=0; while [ $i -lt 1100 ]; do printf x; i=$((i+1)); done; exit 127",
        },
        {"id": "V8", "command": "exit 124"},
        {"id": "V9", "command": "exit 137"},
    ]
    checkout = tmp_path / "checkout"
    subprocess.run(
        [
            "sh",
            "-c",
            scripts.gate_probe_checkout_script(str(origin), "main", str(checkout)),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=probe_environment,
    )
    result = subprocess.run(
        ["sh", "-c", scripts.gate_probe_script(str(checkout), checks, 5)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env=probe_environment,
    )
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row["exit"] for row in rows] == [0, 1, 127, 0, 127, 124, 137]
    assert "not found" in rows[2]["detail"]
    assert [row["id"] for row in rows] == ["V1", "V4", "V5", "V6", "V7", "V8", "V9"]
    assert [row["command"] for row in rows] == [check["command"] for check in checks]
    assert rows[4]["detail"] == "x" * 1000
    assert all(rows[i]["detail"] == "" for i in (0, 1, 3))


@pytest.mark.parametrize("command", ["sleep 30", "trap '' TERM; sleep 30"])
def test_probe_script_bounds_each_command_without_python(
    tmp_path: Path, probe_environment: dict[str, str], command: str
) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=origin, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        cwd=origin,
        check=True,
        capture_output=True,
    )
    checks = [{"id": "V4", "command": command}, {"id": "V5", "command": "true"}]
    checkout = tmp_path / "checkout"
    subprocess.run(
        [
            "sh",
            "-c",
            scripts.gate_probe_checkout_script(str(origin), "main", str(checkout)),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=probe_environment,
    )
    result = subprocess.run(
        ["sh", "-c", scripts.gate_probe_script(str(checkout), checks, 1)],
        env=probe_environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode in (124, 137)
    assert "V4: command timed out" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("failure", ["clone", "checkout"])
def test_probe_script_reports_checkout_stderr_on_failure(
    tmp_path: Path, probe_environment: dict[str, str], failure: str
) -> None:
    origin = tmp_path / "origin"
    if failure == "checkout":
        origin.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=origin, check=True, capture_output=True)
    result = subprocess.run(
        [
            "sh",
            "-c",
            scripts.gate_probe_checkout_script(
                str(origin), "missing-ref", str(tmp_path / "checkout")
            ),
        ],
        env=probe_environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "gate probe checkout failed" in result.stderr
    assert ("does not exist" if failure == "clone" else "missing-ref") in result.stderr


def test_probe_checkout_failure_detail_keeps_only_last_1000_bytes(
    tmp_path: Path, probe_environment: dict[str, str]
) -> None:
    git = tmp_path / "bin" / "git"
    git.unlink()
    git.write_text(
        "#!/bin/sh\nprintf 'discard this prefix' >&2\n"
        "i=0; while [ $i -lt 1000 ]; do printf x >&2; i=$((i+1)); done\nexit 23\n"
    )
    git.chmod(0o755)
    result = subprocess.run(
        [
            "sh",
            "-c",
            scripts.gate_probe_checkout_script("unused", "main", str(tmp_path / "checkout")),
        ],
        env=probe_environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 23
    assert result.stdout == ""
    assert result.stderr == "gate probe checkout failed (exit 23): " + "x" * 1000
