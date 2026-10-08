"""Issue 542: writable runtime state, environment stops, and tested mount settings."""

from __future__ import annotations

import base64
import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.kubernetes import KubernetesConfig
from crucible.adapters.harness.codex import CONFIG_DIR, CodexAdapter
from crucible.application.admin import credentials, harness_test
from crucible.application.errors import ContractValidationError
from crucible.application.harnesses import harness_state
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.ports.execution import ProbeResult, Workspace
from crucible.ports.harness import CredentialSource, ExitInfo, MountMode
from tests.unit.kubernetes_fixtures import ATTEMPT, build, pod_of, spec
from tests.unit.test_class_routing import NOW
from tests.unit.test_credential_renewer import _jwt
from tests.unit.test_docker_provider import StubClient, workspace_for
from tests.unit.test_docker_provider import provider as docker_provider
from tests.unit.test_docker_provider import spec as docker_spec
from tests.unit.test_issue_147_harness_test_background import Harnesses
from tests.unit.test_issue_353_infrastructure_interruptions import _events, _finish, _running
from tests.unit.test_issue_423_runtime_settings_part_a import _context

LOG = (
    "WARNING: proceeding, even though we could not create PATH aliases: "
    "Read-only file system (os error 30)\n"
    "Error: failed to initialize sqlite state runtime under /home/worker/.codex: "
    "failed to initialize state runtime at /home/worker/.codex"
)
LOGIN = {
    "tokens": {"access_token": _jwt(NOW + timedelta(hours=1)), "account_id": "fixture-account"}
}


@pytest.mark.parametrize("mode", list(MountMode))
async def test_worker_mounts_and_renewer_projection(mode: MountMode) -> None:
    api, registry, provider = build(
        config=KubernetesConfig(poll_interval_seconds=0, credential_modes={"codex": mode})
    )
    image = "crucible-worker:codex-fake-succeed-2"
    registry.register(image, harness="codex", version="0.156.0")
    api.put_harness_secret("crucible-harness-codex", {"auth.json": json.dumps(LOGIN).encode()})
    launch = spec(harness="codex", image=image)
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    pod = pod_of(api, "worker-")
    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
    volumes = {v["name"]: v for v in pod["volumes"]}
    if mode is MountMode.RO:
        assert mounts[CONFIG_DIR] == {"name": "cred", "mountPath": CONFIG_DIR, "readOnly": True}
        assert CONFIG_DIR + "/config.toml" not in mounts
        sources = volumes["cred"]["projected"]["sources"]
        assert sources[0]["secret"]["items"] == [
            {"key": "auth.json", "path": "auth.json", "mode": 0o400}
        ]
        assert sources[1]["configMap"]["items"][0]["path"] == "config.toml"
        return
    assert mounts[CONFIG_DIR] == {
        "name": "ws",
        "mountPath": CONFIG_DIR,
        "readOnly": False,
        "subPath": "credential",
    }
    assert mounts[CONFIG_DIR + "/config.toml"]["readOnly"] is True
    assert mounts[CONFIG_DIR + "/config.toml"]["subPath"] == "harness/config.toml"
    init = next(c for c in pod["initContainers"] if c["name"] == k8sspec.CREDENTIAL_INIT_CONTAINER)
    script = init["command"][-1]
    source = volumes["cred-source"]["secret"]
    if mode is MountMode.RW_NARROW:
        assert 'cat < "$src/$rel" > "$dst/$rel"' in script
        assert "ln -s" not in script
        assert source["items"][0]["path"] == "auth.json"
        assert k8sspec.CREDENTIAL_SOURCE_MOUNT not in mounts
        return
    assert source["items"] == [
        {"key": "access-token.json", "path": "access-token.json", "mode": 0o400}
    ]
    assert mounts[k8sspec.CREDENTIAL_SOURCE_MOUNT]["readOnly"] is True
    assert "subPath" not in mounts[k8sspec.CREDENTIAL_SOURCE_MOUNT]
    assert 'ln -sfn "$src/$rel" "$dst/$rel"' in script
    assert "access-token.json" in script and "cat <" not in script
    refreshed = {
        "access_token": "replacement-fixture",
        "account_id": "fixture-account",
        "expires_at": "later",
    }
    await provider.refresh_credential_projection(refreshed)
    secret = api.get("secrets", k8sspec.object_name("cred", ATTEMPT))
    assert json.loads(base64.b64decode(secret["data"]["access-token.json"])) == refreshed
    assert source["secretName"] == secret["metadata"]["name"]


def test_docker_renewer_uses_writable_home_and_read_only_projection(tmp_path: Path) -> None:
    provider = docker_provider(tmp_path, StubClient())
    login = tmp_path / "login"
    login.mkdir()
    (login / "auth.json").write_text(json.dumps(LOGIN))
    provider.config = replace(
        provider.config, credentials={"codex": CredentialSource(str(login), MountMode.RENEWER)}
    )
    launch = replace(docker_spec(), harness="codex", credential_mode="renewer")
    body = provider._worker_body(
        workspace_for(tmp_path, launch.attempt_id), launch, resolved="image", network="none", env={}
    )
    mounts = {m["Target"]: m for m in body["HostConfig"]["Mounts"]}
    assert CONFIG_DIR not in mounts
    assert "uid=1000,gid=1000" in body["HostConfig"]["Tmpfs"][CONFIG_DIR]
    assert body["HostConfig"]["Tmpfs"][CONFIG_DIR].startswith("rw,")
    assert body["HostConfig"]["Tmpfs"]["/home/worker"].startswith("rw,")
    assert mounts["/crucible/credential-source"]["ReadOnly"] is True
    assert mounts[CONFIG_DIR + "/config.toml"]["ReadOnly"] is True
    assert "ln -sfn /crucible/credential-source/access-token.json" in body["Cmd"][2]


@pytest.mark.parametrize("mode", ["renewer", "rw-narrow", "ro"])
@pytest.mark.parametrize(
    "tail",
    [LOG, f"{CONFIG_DIR}/sessions: Permission denied", f"{CONFIG_DIR}/logs: Read-only file system"],
)
def test_codex_early_filesystem_exit_is_environment(mode: str, tail: str) -> None:
    adapter = CodexAdapter()
    info = ExitInfo(1, duration_seconds=3, credential_mode=mode)
    assert adapter.classify_exit(info, "", tail) is ExitClass.ENVIRONMENT
    interruption = adapter.interruption(info, "", tail)
    assert interruption is not None and interruption.environment
    assert CONFIG_DIR in interruption.message and f"mount mode {mode}" in interruption.message


@pytest.mark.parametrize(
    "info,tail",
    [
        (ExitInfo(1, duration_seconds=60), LOG),
        (ExitInfo(1), LOG),
        (ExitInfo(1, duration_seconds=1), "/crucible/repo: Permission denied"),
        (ExitInfo(1, duration_seconds=1, timed_out=True), LOG),
        (ExitInfo(1, duration_seconds=1, killed=True), LOG),
    ],
)
def test_non_startup_failures_do_not_take_credential_environment_path(
    info: ExitInfo, tail: str
) -> None:
    assert CodexAdapter().classify_exit(info, "", tail) is not ExitClass.ENVIRONMENT


def test_environment_detail_and_zero_attempt_budget_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, attempts = _running(monkeypatch)
    pending.execution.max_attempts = 1
    pending.attempt.started_at = supervisor._clock.now() - timedelta(seconds=3)
    supervisor._record_prepared(
        pending.attempt.id,
        Workspace(pending.attempt.id, "repo", "identity", "report"),
        credential_mode="renewer",
    )
    _finish(supervisor, pending.attempt, LOG)
    assert pending.attempt.exit_class is ExitClass.ENVIRONMENT
    assert CONFIG_DIR in pending.attempt.termination_detail
    assert "mount mode renewer" in pending.attempt.termination_detail
    assert len(attempts) == 1  # Stop for repair, rather than looping on a bad mount.
    failure = next(e for e in _events(uow) if e.kind == EventKind.EXECUTION_FAILED.value)
    assert failure.payload["attempts_used"] == 0


@pytest.mark.parametrize("passes", [False, True])
async def test_mount_mode_is_tested_before_save(
    monkeypatch: pytest.MonkeyPatch, passes: bool
) -> None:
    ctx, uow = _context()
    uow.harnesses = Harnesses()
    uow.harness_images = MagicMock()
    uow.harness_images.get.return_value = SimpleNamespace(reference="image", version="0.156.0")
    uow.commit = MagicMock()
    ctx.uow_factory = lambda: nullcontext(uow)  # type: ignore[assignment]
    ctx.credential_sources = {"codex": CredentialSource("", MountMode.RW_NARROW)}
    provider = MagicMock()
    ctx.providers = {"kubernetes": provider}
    monkeypatch.setattr(credentials, "secret_store", lambda _: provider)
    monkeypatch.setattr(credentials, "read_secrets", AsyncMock(return_value={}))
    monkeypatch.setattr(credentials, "state_view", lambda *_: {"state": "valid"})
    monkeypatch.setattr(credentials, "probe_image", AsyncMock(return_value="image"))
    monkeypatch.setattr(credentials, "probe_route", lambda *_: ("model", "subscription", None))

    async def launch(request: Any) -> ProbeResult:
        assert credentials.mount_mode_value(ctx, uow, "codex").value == "rw-narrow"
        assert request.credential_mode == "renewer"
        assert request.argv[0] == "/usr/local/bin/crucible-codex-host"
        return ProbeResult(
            0 if passes else 1, "digest", "0.156.0", 2, stderr_tail="" if passes else LOG
        )

    provider.probe_credential = AsyncMock(side_effect=launch)
    if passes:
        result = await credentials.set_mount_mode(
            ctx, uow, principal="admin", harness="codex", mode="renewer", reason="parallel"
        )
        assert result["mount_mode"] == "renewer"
        assert uow.events.items[-1].kind == EventKind.CREDENTIAL_MOUNT_MODE_SET.value
    else:
        with pytest.raises(ContractValidationError, match=r"Worker starts.*mount mode renewer"):
            await credentials.set_mount_mode(
                ctx, uow, principal="admin", harness="codex", mode="renewer", reason="parallel"
            )
        assert credentials.mount_mode_value(ctx, uow, "codex").value == "rw-narrow"
        refusal = next(e for e in uow.events.items if e.kind == EventKind.ADMIN_REFUSED.value)
        assert CONFIG_DIR in refusal.payload["detail"]
        assert "Worker starts" in refusal.payload["detail"]
    provider.probe_credential.assert_awaited_once()
    assert uow.harnesses.get("codex").last_test["ok"] is passes


async def test_mount_mode_validation_refuses_to_overlap_a_harness_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, uow = _context()
    uow.harnesses = Harnesses()
    uow.commit = MagicMock()
    ctx.uow_factory = lambda: nullcontext(uow)  # type: ignore[assignment]
    ctx.credential_sources = {"codex": CredentialSource("", MountMode.RW_NARROW)}
    provider = MagicMock()
    ctx.providers = {"kubernetes": provider}
    monkeypatch.setattr(credentials, "secret_store", lambda _: provider)
    state = harness_state(uow, ctx.clock, "codex")
    state.last_test = {
        "harness": "codex",
        "status": harness_test.RUNNING,
        "ok": None,
        "failed_step": None,
        "steps": [],
        "started_at": ctx.clock.now().isoformat(),
        "started_by": "other-admin",
    }
    uow.harnesses.put(state)

    with pytest.raises(ContractValidationError, match="already running"):
        await credentials.set_mount_mode(
            ctx, uow, principal="admin", harness="codex", mode="renewer", reason="parallel"
        )

    assert credentials.mount_mode_value(ctx, uow, "codex").value == "rw-narrow"
    assert not provider.probe_credential.called
    assert uow.harnesses.locked == ["codex"]
    refusal = next(e for e in uow.events.items if e.kind == EventKind.ADMIN_REFUSED.value)
    assert "already running" in refusal.payload["detail"]


def test_other_environment_exit_is_a_model_call_failure_not_a_startup_failure() -> None:
    record = credentials.ProbeRecord(
        harness="codex",
        exit_class=ExitClass.ENVIRONMENT.value,
        exit_code=137,
        harness_version="0.156.0",
        image="image",
        image_digest="digest",
        auth_files_changed=False,
        mount_mode="renewer",
        duration_seconds=12,
        detail="OOMKilled",
        conclusive=False,
        cause=ExitClass.ENVIRONMENT.value,
    )
    steps = harness_test._Steps([])

    assert not harness_test._worker_did_not_start(record)
    steps.passed(harness_test.WORKER, "the worker started")
    steps.failed(harness_test.MODEL, harness_test.EXIT_WORDS[record.exit_class])

    assert steps.items[0]["name"] == harness_test.WORKER and steps.items[0]["ok"] is True
    assert steps.items[1]["name"] == harness_test.MODEL and steps.items[1]["ok"] is False


async def test_kubernetes_probe_mounts_requested_mode_instead_of_configured_mode() -> None:
    from crucible.ports.execution import ProbeRequest  # noqa: PLC0415
    from tests.unit.kubernetes_fixtures import created  # noqa: PLC0415

    api, registry, provider = build()  # Configured rw-narrow.
    image = "crucible-worker:codex-fake-succeed-2"
    registry.register(image, harness="codex", version="0.156.0")
    api.put_harness_secret("crucible-harness-codex", {"auth.json": json.dumps(LOGIN).encode()})
    result = await provider.probe_credential(
        ProbeRequest(
            harness="codex",
            image=image,
            argv=("true",),
            credential_mode="renewer",
            policy={"images": {"allowlist": [image]}},
        )
    )
    assert result.exit_code == 0
    assert result.credential_sync is not None
    assert result.credential_sync.mount_mode == "renewer"
    worker = created(api, "jobs", "worker-probe")[0]["spec"]["template"]["spec"]
    mounts = {m["mountPath"]: m for m in worker["containers"][0]["volumeMounts"]}
    assert mounts[CONFIG_DIR]["readOnly"] is False
    assert mounts[k8sspec.CREDENTIAL_SOURCE_MOUNT]["readOnly"] is True


def test_init_link_follows_atomic_secret_projection_updates(tmp_path: Path) -> None:
    import subprocess  # noqa: PLC0415

    from crucible.adapters.execution.kubernetes import _seed_script  # noqa: PLC0415
    from crucible.application.credential_renewer import worker_credential_spec  # noqa: PLC0415

    source, runtime = tmp_path / "source", tmp_path / "runtime"
    source.mkdir()
    for version in ("v1", "v2"):
        (source / version).mkdir()
        (source / version / "access-token.json").write_text(version)
    (source / "..data").symlink_to("v1")
    (source / "access-token.json").symlink_to("..data/access-token.json")
    script = _seed_script(worker_credential_spec(CodexAdapter().credential_spec()), renewer=True)
    script = script.replace(k8sspec.CREDENTIAL_SOURCE_MOUNT, str(source)).replace(
        "/crucible/credential", str(runtime)
    )
    subprocess.run(["sh", "-c", script], check=True)
    token = runtime / "access-token.json"
    assert token.is_symlink() and token.read_text() == "v1"
    (runtime / "state.sqlite").write_text("runtime is writable")
    (source / "..data-next").symlink_to("v2")
    (source / "..data-next").replace(source / "..data")
    assert token.read_text() == "v2"


def test_collection_delay_does_not_hide_an_early_environment_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crucible.ports.execution import Observation, ObservationState  # noqa: PLC0415

    supervisor, pending, uow, _ = _running(monkeypatch)
    pending.attempt.started_at = supervisor._clock.now() - timedelta(minutes=5)
    supervisor._record_prepared(
        pending.attempt.id,
        Workspace(pending.attempt.id, "repo", "identity", "report"),
        credential_mode="rw-narrow",
    )
    _finish(
        supervisor,
        pending.attempt,
        LOG,
        final_observation=Observation(ObservationState.EXITED, exit_code=1, duration_seconds=3),
    )
    assert pending.attempt.exit_class is ExitClass.ENVIRONMENT
    assert "mount mode rw-narrow" in pending.attempt.termination_detail
    failure = next(e for e in _events(uow) if e.kind == EventKind.EXECUTION_FAILED.value)
    assert failure.payload["attempts_used"] == 0


async def test_docker_observation_uses_container_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crucible.ports.execution import Handle  # noqa: PLC0415

    client = StubClient()
    monkeypatch.setattr(
        client,
        "inspect_container",
        lambda _: {
            "State": {
                "Status": "exited",
                "ExitCode": 1,
                "StartedAt": "2026-10-08T00:00:00Z",
                "FinishedAt": "2026-10-08T00:00:03Z",
            }
        },
    )
    observation = await docker_provider(tmp_path, client).observe(
        Handle("docker", "container", "attempt")
    )
    assert observation.duration_seconds == 3


async def test_kubernetes_observation_uses_container_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crucible.ports.execution import Handle  # noqa: PLC0415

    _, _, provider = build()
    pod = {
        "metadata": {"name": "worker-time"},
        "status": {
            "phase": "Failed",
            "containerStatuses": [
                {
                    "name": k8sspec.CONTAINER_NAME,
                    "state": {
                        "terminated": {
                            "exitCode": 1,
                            "reason": "Error",
                            "startedAt": "2026-10-08T00:00:00Z",
                            "finishedAt": "2026-10-08T00:00:03Z",
                        }
                    },
                }
            ],
        },
    }
    monkeypatch.setattr(provider, "_pod_of", AsyncMock(return_value=pod))
    observation = await provider.observe(Handle("kubernetes", "worker-time", "attempt"))
    assert observation.duration_seconds == 3
