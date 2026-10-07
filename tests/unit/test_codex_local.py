"""FDY-0149: local Codex keeps the gateway's credential and pool boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.execution.docker import LAUNCH_WRAPPER, DockerProvider
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.registry import default_registry
from crucible.application.routing import select_model
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import ExecutionRole
from crucible.domain.lifecycle import AttemptState
from crucible.ports.harness import CredentialSource, LaunchContext, MountMode
from tests.unit.kubernetes_fixtures import build, spec
from tests.unit.test_class_routing import NOW, _model, _routing, _uow
from tests.unit.test_credential_copy import StubClient, config

URL = "http://gateway.lab.test:4000/v1"


def context(**kw: Any) -> LaunchContext:
    return LaunchContext(
        attempt_id="attempt",
        model="coder",
        effort=None,
        timeout_seconds=60,
        identity_mount="/crucible/identity",
        report_mount="/crucible/report",
        repo_mount="/crucible/repo",
        **kw,
    )


def test_local_launch_config_key_and_closed_stdin(tmp_path: Path) -> None:
    launch = CodexAdapter().build_launch(
        context(
            endpoint="local",
            endpoint_url=URL,
            credential_mounted=True,
            harness_settings={"context_length": 96000},
        )
    )
    assert "--ignore-user-config" not in launch.argv
    assert "--dangerously-bypass-approvals-and-sandbox" in launch.argv
    assert not launch.env["CODEX_HOME"].startswith("/tmp")
    assert launch.env_from_files == {"OPENAI_API_KEY": "/home/worker/.hermes-auth/api-key"}
    assert "auth.json" not in repr(launch)
    home = tmp_path / "codex-home"
    identity = tmp_path / "IDENTITY.md"
    identity.write_text("identity instructions")
    # Execute the actual provider wrapper with a stand-in that reads until EOF.
    result = subprocess.run(
        [
            "bash",
            "-o",
            "pipefail",
            "-c",
            LAUNCH_WRAPPER,
            "launch",
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read())",
        ],
        env={
            **os.environ,
            **launch.env,
            "CODEX_HOME": str(home),
            "CRUCIBLE_STDIN_FILES": str(identity),
            "CRUCIBLE_PROMPT": launch.stdin_text,
        },
        input="",
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert "identity instructions" in result.stdout
    assert launch.stdin_text in result.stdout
    document = tomllib.loads((home / "config.toml").read_text())
    assert document["model_context_window"] == 96000
    assert document["model_provider"] == "local_gateway"
    assert document["model_providers"]["local_gateway"] == {
        "name": "Local gateway",
        "base_url": URL,
        "env_key": "OPENAI_API_KEY",
        "wire_api": "responses",
    }


def test_subscription_launch_unchanged() -> None:
    launch = CodexAdapter().build_launch(context(credential_mounted=True))
    assert launch.env == {"CODEX_HOME": "/home/worker/.codex"}
    assert launch.env_from_files == {}
    assert "--dangerously-bypass-approvals-and-sandbox" in launch.argv
    assert launch.stdin_files == ("/crucible/identity/IDENTITY.md",)
    assert CodexAdapter().credential_spec().minimum_mode is MountMode.RENEWER


def test_local_credential_mounts_only_gateway_key(tmp_path: Path) -> None:
    key = tmp_path / "gateway"
    key.mkdir()
    (key / "api-key").write_text("test-key")
    provider = DockerProvider(
        config(tmp_path, hermes=CredentialSource(str(key), MountMode.RW_NARROW)),
        StubClient(),  # type: ignore[arg-type]
    )
    local = spec(harness="codex", endpoint="local", endpoint_url=URL)
    copy = provider._credential_copy(local)
    assert copy is not None and copy.mode is MountMode.RO
    assert [a.name for a in copy.spec.auth_files] == ["api-key"]
    mounts = provider._credential_mounts(local)
    assert len(mounts) == 1 and mounts[0]["ReadOnly"] is True
    assert mounts[0]["Target"] == "/home/worker/.hermes-auth"
    _, _, kubernetes = build()
    kcopy = kubernetes._credential_copy(local)
    assert kcopy is not None and kcopy.mode is MountMode.RO
    assert kcopy.source_secret == kubernetes.config.credential_secret_name("hermes")
    subscription = kubernetes._credential_copy(
        replace(local, endpoint="subscription", endpoint_url=None)
    )
    assert subscription is not None and subscription.mode is MountMode.RW_NARROW
    assert subscription.source_secret == kubernetes.config.credential_secret_name("codex")


def test_local_worker_egress_has_gateway_not_subscription_hosts() -> None:
    _, _, provider = build()
    local = spec(harness="codex", endpoint="local", endpoint_url=URL)
    plan = provider._egress_plan(local, "worker")
    assert "gateway.lab.test:4000" in plan.endpoints
    assert not {"api.openai.com", "auth.openai.com", "chatgpt.com"} & set(plan.hosts)
    subscription = provider._egress_plan(
        replace(local, endpoint="subscription", endpoint_url=None), "worker"
    )
    assert {"api.openai.com", "auth.openai.com", "chatgpt.com"} <= set(subscription.hosts)


def routing() -> Any:
    models = [
        {
            **_model("a-hermes", harness="hermes", pool="lab-local"),
            "endpoint": "local",
            "endpoint_url": URL,
        },
        {**_model("z-codex", pool="lab-local"), "endpoint": "local", "endpoint_url": URL},
        _model("subscription"),
    ]
    route = _routing(models)
    route.tiers["trivial"] = route.tiers["standard"]
    route.pools["lab-local"].max_concurrency = 2
    return route


@pytest.mark.parametrize("tier", ["trivial", "standard"])
def test_codex_first_and_hermes_fallback(tier: str) -> None:
    route = routing()
    for eligible, wanted in [
        ({"codex", "hermes"}, "z-codex"),
        ({"hermes"}, "a-hermes"),
        ({"codex:local", "hermes"}, "z-codex"),
    ]:
        result = select_model(
            _uow(),
            route,
            contract={"required_verification": [{"command": "python3 -m unittest tests.test_x"}]},
            tier=tier,
            project="p",
            provider="fake",
            now=NOW,
            eligible_harnesses=eligible,
        )
        assert result.selected is not None and result.selected.id == wanted


def test_local_codex_uses_pool_limit_subscription_still_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = routing()
    monkeypatch.setattr("crucible.application.supervisor.load_attempt_routing", lambda *_: route)
    supervisor = object.__new__(Supervisor)
    supervisor._harnesses = default_registry()
    supervisor._credential_sources = {}
    supervisor._logins_now = frozenset()
    local: Any = SimpleNamespace(harness="codex", model="z-codex", policy_snapshot={})
    subscription: Any = SimpleNamespace(harness="codex", model="subscription", policy_snapshot={})
    executions = {"local": local, "subscription": subscription}
    live = [
        SimpleNamespace(
            routing_version=None,
            state=AttemptState.RUNNING,
            execution_id="local",
            selected_pool="lab-local",
            selected_model=None,
            selected_harness=None,
        )
    ]
    uow = _uow(attempts=live)
    uow.executions = SimpleNamespace(get=executions.get)
    assert supervisor._harness_busy_in_uow(uow, local) is None
    assert supervisor._harness_busy_in_uow(uow, subscription) is None
    live.append(
        SimpleNamespace(
            routing_version=None,
            state=AttemptState.RUNNING,
            execution_id="local",
            selected_pool="lab-local",
            selected_model=None,
            selected_harness=None,
        )
    )
    assert "2 of 2 lab-local" in str(supervisor._harness_busy_in_uow(uow, local))
    live[:] = [
        SimpleNamespace(
            state=AttemptState.EXITED,
            routing_version=None,
            execution_id="subscription",
            selected_pool="pool-subscription",
            selected_model=None,
            selected_harness=None,
        )
    ]
    assert supervisor._harness_busy_in_uow(uow, local) is None
    assert "1 of 1 codex" in str(supervisor._harness_busy_in_uow(uow, subscription))


@pytest.mark.asyncio
async def test_supervisor_launch_uses_gateway_alias_limits_and_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route = routing()
    route.models[1].model_name = "coder"
    monkeypatch.setattr("crucible.application.supervisor.load_attempt_routing", lambda *_: route)
    requested: list[str] = []

    def setting(name: str) -> Any:
        requested.append(name)
        return SimpleNamespace(document={"context_length": 96000})

    uow: Any = SimpleNamespace(provider_settings=SimpleNamespace(get=setting))
    supervisor = object.__new__(Supervisor)
    supervisor._uow_factory = lambda: nullcontext(uow)  # type: ignore[assignment]
    supervisor._harnesses = default_registry()
    (tmp_path / "api-key").write_text("test-key")
    supervisor._credential_sources = {"hermes": CredentialSource(str(tmp_path))}
    attempt: Any = SimpleNamespace(
        id="attempt",
        number=1,
        selected_harness="codex",
        selected_model="coder",
        selected_image="image",
        resume_from_remote=False,
        routing_version=None,
        effective_settings=None,
    )
    execution: Any = SimpleNamespace(
        role=ExecutionRole.IMPLEMENT,
        harness="codex",
        model="coder",
        image="image",
        policy_snapshot={},
        timeout_seconds=60,
        effort=None,
        provider="docker",
    )
    task: Any = SimpleNamespace(id="task", external_id="FDY-0149", principal_id="tests")
    launch = await supervisor._build_spec(attempt, execution, task, {})
    assert requested == ["harness.hermes"]
    assert launch.model == "coder"
    assert launch.command[launch.command.index("--model") + 1] == "coder"
    assert launch.env_from_files == {"OPENAI_API_KEY": "/home/worker/.hermes-auth/api-key"}
    assert "model_context_window = 96000" in launch.env["CRUCIBLE_CODEX_CONFIG"]
