"""FDY-0399: Qwen's launch, evidence and real Kubernetes provider rendering."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from crucible.adapters.execution.kubernetes import KubernetesConfig
from crucible.adapters.harness import base
from crucible.adapters.harness.qwen_code import CommandTracker, QwenCodeAdapter
from crucible.adapters.harness.registry import default_registry
from crucible.adapters.ui.pages.images import _image_rows
from crucible.application.admin import images
from crucible.contracts.policy import RoutingModel
from crucible.contracts.task_contract import HarnessName
from crucible.domain.exit_class import ExitClass
from crucible.ports.execution import ImageInfo
from crucible.ports.harness import ExitInfo, LaunchContext, MountMode
from tests.unit.kubernetes_fixtures import build, pod_of, spec
from tests.unit.test_class_routing import _routing
from tests.unit.test_harness_reports import CLAIM
from tests.unit.test_issue_388_context_budget import (
    _EXECUTION,
    _TASK,
    _attempt,
    _supervisor,
    _Uow,
)

ADAPTER = QwenCodeAdapter()


def context(**kwargs: Any) -> LaunchContext:
    return LaunchContext(
        attempt_id="attempt",
        model="qwen-lane",
        effort=None,
        timeout_seconds=600,
        identity_mount="/crucible/identity",
        report_mount="/crucible/report",
        repo_mount="/crucible/repo",
        endpoint="local",
        endpoint_url="https://gateway.example/v1",
        **kwargs,
    )


def wrapper() -> ModuleType:
    path = Path(__file__).parents[2] / "images/worker/crucible-qwen-code.py"
    module_spec = importlib.util.spec_from_file_location("qwen_wrapper", path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_launch_and_shared_read_only_credential() -> None:
    launch = ADAPTER.build_launch(context(credential_mounted=True))
    assert launch.argv == (
        "/usr/local/bin/crucible-qwen-code",
        "--yolo",
        "--auth-type",
        "openai",
        "--advisor",
        "off",
        "--output-format",
        "stream-json",
        "--max-session-turns",
        "300",
        base.POINTER_PROMPT,
    )
    assert launch.env["OPENAI_BASE_URL"] == "https://gateway.example/v1"
    assert launch.env["OPENAI_MODEL"] == "qwen-lane"
    assert launch.env["CRUCIBLE_QWEN_CONTEXT_LENGTH"] == "131072"
    assert launch.env_from_files == {"OPENAI_API_KEY": "/home/worker/.hermes-auth/api-key"}
    assert launch.workdir == "/crucible/repo"
    assert launch.transcript_path == "/crucible/report/transcript.jsonl"
    credential = ADAPTER.credential_spec()
    assert credential is not None
    assert credential.harness == "hermes"
    assert credential.minimum_mode is MountMode.RO
    assert not credential.auth_files[0].sync_back
    assert ADAPTER.build_launch(context()).env_from_files == {}
    assert default_registry().require(HarnessName.QWEN_CODE).name == "qwen_code"
    with pytest.raises(ValueError, match="local endpoint"):
        ADAPTER.build_launch(replace(context(), endpoint="subscription", endpoint_url=None))


@pytest.mark.parametrize("limit", [65536, 131072, 262144])
def test_context_override(limit: int) -> None:
    launch = ADAPTER.build_launch(context(harness_settings={"context_length": limit}))
    assert launch.env["CRUCIBLE_QWEN_CONTEXT_LENGTH"] == str(limit)


@pytest.mark.parametrize("limit", [0, -1, True, "131072"])
def test_invalid_context_is_refused(limit: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ADAPTER.build_launch(context(harness_settings={"context_length": limit}))


def test_settings_and_identity_are_written_before_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = wrapper()
    identity = tmp_path / "IDENTITY.md"
    identity.write_text("Write report.yaml against CompletionClaimV1. Execute the task.")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CRUCIBLE_QWEN_CONTEXT_LENGTH", "65536")
    monkeypatch.setenv("CRUCIBLE_QWEN_IDENTITY", str(identity))
    monkeypatch.setattr(module.sys, "argv", ["wrapper", *ADAPTER.build_launch(context()).argv[1:]])
    called = []

    def execute(binary: str, argv: list[str]) -> None:
        settings = json.loads((tmp_path / ".qwen/settings.json").read_text())
        assert settings["model"] == {
            "maxToolCallsPerTurn": 0,
            "generationConfig": {"contextWindowSize": 65536},
        }
        assert not settings["tools"]["shell"]["enableInteractiveShell"]
        assert binary == "/usr/local/bin/qwen"
        assert argv[-1].startswith(identity.read_text())
        assert argv[-1].endswith(base.POINTER_PROMPT)
        called.append(binary)

    monkeypatch.setattr(module.os, "execv", execute)
    module.main()
    assert called == ["/usr/local/bin/qwen"]


def events() -> list[dict[str, Any]]:
    return [
        {"type": "system", "subtype": "system_start", "model": "qwen-lane"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "run_shell_command",
                        "input": {"command": "make lint"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "call-1", "content": "passed"},
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1234,
            "usage": {"input_tokens": 100, "output_tokens": 25},
        },
    ]


def test_stream_usage_completion_and_report(tmp_path: Path) -> None:
    (tmp_path / base.TRANSCRIPT_NAME).write_text("\n".join(map(json.dumps, events())))
    (tmp_path / "report.yaml").write_text(json.dumps(CLAIM))
    exit = ExitInfo(exit_code=0, report_present=True)
    report = ADAPTER.parse_report(tmp_path, exit)
    assert report.report_present and not report.errors
    assert report.claim == CLAIM
    assert report.metrics.tokens_in == 100
    assert report.metrics.tokens_out == 25
    assert report.metrics.duration_ms == 1234
    assert report.metrics.tool_calls == 1
    assert report.metrics.model == "qwen-lane"
    assert report.run_evidence_error is None
    assert report.in_flight == ()
    assert ADAPTER.classify_exit(exit, "", "", tmp_path) is ExitClass.COMPLETED


def test_missing_result_is_an_evidence_anomaly(tmp_path: Path) -> None:
    report = ADAPTER.parse_report(tmp_path, ExitInfo(exit_code=0))
    assert report.run_evidence_error == "missing Qwen stream-json result event"


def test_tracker_pairs_tool_calls_across_chunks() -> None:
    tracker = CommandTracker()
    start = json.dumps(events()[1]) + "\n"
    tracker.feed("stdout", start[:20])
    tracker.feed("stdout", start[20:])
    assert tracker.running and "make lint" in tracker.running[0][1]
    tracker.feed("stdout", json.dumps(events()[2]) + "\n")
    assert tracker.running == ()


@pytest.mark.parametrize(
    ("code", "blocked", "text", "expected"),
    [
        (0, False, "", ExitClass.COMPLETED_WITHOUT_REPORT),
        (75, True, "", ExitClass.BLOCKED),
        (75, False, "", ExitClass.CRASHED),
        (1, False, "400: maximum context length is 131072 tokens", ExitClass.PROVIDER_ERROR),
        (1, False, "429 quota exceeded", ExitClass.QUOTA_EXHAUSTED),
        (0, False, "tool output mentions 400", ExitClass.COMPLETED_WITHOUT_REPORT),
    ],
)
def test_exit_classes(code: int, blocked: bool, text: str, expected: ExitClass) -> None:
    assert (
        ADAPTER.classify_exit(ExitInfo(exit_code=code, blocked_present=blocked), "", text)
        is expected
    )


def model_entry(**overrides: Any) -> RoutingModel:
    return RoutingModel.model_validate(
        {
            "id": "qwen-local",
            "harness": "qwen_code",
            "endpoint": "local",
            "endpoint_url": "https://gateway.example/v1",
            "capability": "frontier",
            "cost": "none",
            "speed": "fast",
            "pool": "lab-local",
            "weight": 1,
            "enabled": True,
            **overrides,
        }
    )


def test_routing_entry_context_validation() -> None:
    assert model_entry(context_length=65536).context_length == 65536
    assert model_entry().context_length is None
    with pytest.raises(ValidationError):
        model_entry(context_length=0)


async def test_local_route_launches_on_kubernetes_with_hermes_secret() -> None:
    model = model_entry(context_length=65536)
    api, _, provider = build(
        harness="qwen_code",
        version="0.25.0",
        resolver=lambda host: (
            ["10.10.0.42/32"] if host == "gateway.example" else ["151.101.0.223/32"]
        ),
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            local_endpoint_cidrs=("10.10.0.0/24",),
            credential_secrets={"hermes": "crucible-harness-hermes"},
        ),
    )
    api.put_harness_secret("crucible-harness-hermes", {"api-key": b"placeholder"})
    launch = spec(
        harness=model.harness,
        model=model.id,
        endpoint=model.endpoint,
        endpoint_url=model.endpoint_url,
        harness_settings={"context_length": model.context_length},
    )
    assert await provider.credential_available("qwen_code")
    adapter_launch = ADAPTER.build_launch(provider._launch_context(launch, credential_mounted=True))
    launch = replace(
        launch,
        command=adapter_launch.argv,
        env=dict(adapter_launch.env),
        env_from_files=dict(adapter_launch.env_from_files),
        transcript_path=adapter_launch.transcript_path,
    )
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    pod = pod_of(api, "worker-")
    container = pod["containers"][0]
    assert "/usr/local/bin/crucible-qwen-code" in container["command"]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["OPENAI_MODEL"] == "qwen-local"
    assert env["CRUCIBLE_QWEN_CONTEXT_LENGTH"] == "65536"
    assert "OPENAI_API_KEY=/home/worker/.hermes-auth/api-key" in env["CRUCIBLE_ENV_FROM_FILES"]
    assert any(
        m["mountPath"] == "/home/worker/.hermes-auth" and m["readOnly"]
        for m in container["volumeMounts"]
    )


@pytest.mark.parametrize("limit", [None, 65536])
async def test_supervisor_passes_and_freezes_routing_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    limit: int | None,
) -> None:

    attempt = _attempt()
    attempt.selected_harness = "qwen_code"
    attempt.selected_model = "qwen-lane"
    uow = _Uow({"context_length": 999999}, attempt)
    supervisor = _supervisor(monkeypatch, tmp_path, uow, thinking=False)
    route = _routing([model_entry(context_length=limit, model_name="qwen-lane").model_dump()])
    monkeypatch.setattr("crucible.application.supervisor.load_attempt_routing", lambda *_: route)
    launched = await supervisor._build_spec(attempt, _EXECUTION, _TASK, {})
    assert launched.effective_settings == {"context_length": limit or 131072}
    assert launched.env["CRUCIBLE_QWEN_CONTEXT_LENGTH"] == str(limit or 131072)
    assert launched.env["OPENAI_MODEL"] == "qwen-lane"
    assert launched.env_from_files == {"OPENAI_API_KEY": "/home/worker/.hermes-auth/api-key"}
    attempt.effective_settings = {"context_length": 98304}
    again = await supervisor._build_spec(attempt, _EXECUTION, _TASK, {})
    assert again.env["CRUCIBLE_QWEN_CONTEXT_LENGTH"] == "98304"


@pytest.mark.parametrize(
    ("subtype", "error", "expected"),
    [
        ("error_during_execution", "400 context window overflow", ExitClass.PROVIDER_ERROR),
        ("error_during_execution", "429 quota exceeded", ExitClass.QUOTA_EXHAUSTED),
        ("error_max_turns", "maximum turns reached", ExitClass.INCOMPLETE),
    ],
)
def test_structured_failure_overrides_zero_exit(
    tmp_path: Path,
    subtype: str,
    error: str,
    expected: ExitClass,
) -> None:
    result = {"type": "result", "subtype": subtype, "is_error": True, "error": {"message": error}}
    (tmp_path / base.TRANSCRIPT_NAME).write_text(json.dumps(result))
    assert ADAPTER.classify_exit(ExitInfo(exit_code=0), "", "", tmp_path) is expected
    assert (
        ADAPTER.classify_exit(ExitInfo(exit_code=1, timed_out=True), "", "", tmp_path)
        is ExitClass.TIMEOUT
    )


def test_open_tool_is_incomplete_even_with_report(tmp_path: Path) -> None:
    (tmp_path / base.TRANSCRIPT_NAME).write_text(json.dumps(events()[1]))
    assert (
        ADAPTER.classify_exit(ExitInfo(exit_code=0, report_present=True), "", "", tmp_path)
        is ExitClass.INCOMPLETE
    )


async def test_images_workflow_offers_qwen_its_own_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = ImageInfo(
        reference="crucible-worker:latest",
        digest="sha256:" + "a" * 64,
        harnesses={"hermes": "0.19.0", "qwen_code": "0.25.0"},
    )

    async def listed(_ctx: Any) -> list[tuple[str, ImageInfo]]:
        return [("kubernetes", image)]

    monkeypatch.setattr(images, "provider_images", listed)
    ctx: Any = SimpleNamespace(harnesses=default_registry())
    uow: Any = SimpleNamespace(harness_images=SimpleNamespace(get=lambda _: None))
    rows = await images.defaults(ctx, uow)
    qwen = next(row for row in rows if row["harness"] == "qwen_code")
    assert qwen["choices"] == [
        {
            "reference": image.reference,
            "digest": image.digest,
            "version": "0.25.0",
        }
    ]
    rendered = _image_rows([qwen], admin=True)
    items = rendered[0][-1]["items"]
    assert len(items) == 1
    action = items[0]
    assert action["action"] == "/ui/actions/image-change"
    assert action["hidden"] == {"harness": "qwen_code"}
    assert action["select"]["options"] == [(image.digest, image.reference)]


def test_worker_node_and_qwen_pins_are_checksum_verified() -> None:
    root = Path(__file__).parents[2]
    pins = dict(
        line.split("=", 1)
        for line in (root / "images/pins.env").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    dockerfile = (root / "images/worker/Dockerfile").read_text()
    builder = (root / "images/build.sh").read_text()
    for name in ("NODE_VERSION", "NODE_SHA256"):
        assert f"ARG {name}={pins[name]}" in dockerfile
        assert f'--build-arg "{name}=${name}"' in builder
    assert pins["NODE_VERSION"].startswith("22.")
    assert "ARG HARNESS_QWEN_CODE_VERSION=0.25.0" in dockerfile
    assert "ADD --checksum=sha256:${NODE_SHA256}" in dockerfile
    assert "ADD --checksum=sha256:${HARNESS_QWEN_CODE_SHA256}" in dockerfile
