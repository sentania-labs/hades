"""Hades #388: Hermes budgets its context with the response allowance the gateway enforces.

The lab gateway reserves 32000 tokens of response out of a 131072 window on every
request. Hermes was told the window but not the allowance, so it compressed at 98304
input tokens while the gateway refused input above 99072: 768 tokens of headroom for the
next tool result. Now the allowance reaches Hermes's per-run config as `model.max_tokens`,
the compressor takes its trigger from the window less the allowance (74304), the attempt
records the context length, allowance and thinking setting it was launched with, and a
retry with a lower allowance keeps it.

The wrapper is checked against stand-ins with the shape Hermes 0.19.0 has, as in
test_hermes_wrapper.py. Where Hermes 0.19.0 itself is installed (the worker image, or a
workspace that carries it at /opt/hermes), the same checks also run against it.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.harness.hermes import HermesAdapter
from crucible.adapters.harness.registry import default_registry
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import ExecutionRole
from crucible.domain.harness_settings import (
    DEFAULT_HERMES_MAX_OUTPUT_TOKENS,
    hermes_limit_problems,
)
from crucible.ports.execution import Workspace
from crucible.ports.harness import CredentialSource, LaunchContext
from tests.fixtures import FakeClock
from tests.unit.test_class_routing import _model, _routing

WRAPPER = Path(__file__).resolve().parents[2] / "images" / "worker" / "crucible-hermes.py"
HERMES_PYTHON = Path("/opt/hermes/bin/python")
URL = "http://gateway.lab.test:4000/v1"
WINDOW = 131_072
ALLOWANCE = 32_000

needs_hermes = pytest.mark.skipif(
    not HERMES_PYTHON.exists(), reason="Hermes 0.19.0 is not installed here"
)


def _wrapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("crucible_hermes", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _context(**kw: Any) -> LaunchContext:
    return LaunchContext(
        attempt_id="attempt",
        model="coder",
        effort=None,
        timeout_seconds=600,
        identity_mount="/crucible/identity",
        report_mount="/crucible/report",
        repo_mount="/crucible/repo",
        endpoint="local",
        endpoint_url=URL,
        **kw,
    )


# ----- the wrapper's config carries max_tokens --------------------------------------


def test_the_wrapper_config_carries_max_tokens_from_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter's env, as the wrapper reads it, becomes Hermes's own config."""
    launch = HermesAdapter().build_launch(
        _context(harness_settings={"context_length": WINDOW, "max_output_tokens": ALLOWANCE})
    )
    assert launch.env["CRUCIBLE_HERMES_CONTEXT_LENGTH"] == "131072"
    assert launch.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == "32000"
    for name, value in launch.env.items():
        monkeypatch.setenv(name, value)
    wrapper = _wrapper()
    home = tmp_path / "home"
    assert wrapper.settings_from_env(home) == (WINDOW, ALLOWANCE)
    assert (home / "config.yaml").read_text() == (
        "model:\n  context_length: 131072\n  max_tokens: 32000\n"
    )


def test_the_allowance_is_written_even_when_hermes_finds_the_window(tmp_path: Path) -> None:
    wrapper = _wrapper()
    wrapper.write_settings(tmp_path / "home", 0, ALLOWANCE)
    assert (tmp_path / "home" / "config.yaml").read_text() == "model:\n  max_tokens: 32000\n"
    wrapper.write_settings(tmp_path / "none", 0, 0)
    assert not (tmp_path / "none" / "config.yaml").exists()


def test_the_default_allowance_is_the_gateways_and_must_fit_the_window() -> None:
    launch = HermesAdapter().build_launch(_context())
    assert launch.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == str(ALLOWANCE)
    assert DEFAULT_HERMES_MAX_OUTPUT_TOKENS == ALLOWANCE
    assert hermes_limit_problems(300, WINDOW, ALLOWANCE) == []
    assert hermes_limit_problems(300, 0, ALLOWANCE) == []
    assert hermes_limit_problems(300, WINDOW, WINDOW) == [
        "the max output tokens must be less than the context length"
    ]
    assert hermes_limit_problems(300, WINDOW, 10) == [
        "the max output tokens must be between 1024 and 1000000"
    ]


# ----- the compressor's trigger -----------------------------------------------------


def test_the_reservation_lowers_the_compression_trigger_as_hermes_computes_it() -> None:
    """Hermes 0.19.0: 75% (its floor under a 512000 window) of the window less the
    allowance. Without the allowance the trigger sat 768 tokens below the gateway's
    input ceiling; with it there is room for the next tool result."""
    trigger = _wrapper().compression_trigger
    ceiling = WINDOW - ALLOWANCE
    assert ceiling == 99_072
    assert trigger(WINDOW) == 98_304
    assert ceiling - trigger(WINDOW) == 768
    assert trigger(WINDOW, ALLOWANCE) == 74_304
    assert ceiling - trigger(WINDOW, ALLOWANCE) == 24_768
    # Hermes's other branches: a large window keeps 50%, a small budget triggers at 85%.
    assert trigger(1_000_000, ALLOWANCE) == 484_000
    assert trigger(80_000, ALLOWANCE) == 40_800
    assert trigger(WINDOW, WINDOW) == 98_304


@needs_hermes
def test_the_trigger_is_the_one_hermes_0_19_0_computes() -> None:
    wrapper = _wrapper()
    pairs = [(WINDOW, 0), (WINDOW, ALLOWANCE), (1_000_000, ALLOWANCE), (80_000, ALLOWANCE)]
    script = (
        "import json, sys\n"
        "from agent.context_compressor import ContextCompressor as C\n"
        "pairs = json.loads(sys.argv[1])\n"
        "print(json.dumps([C._compute_threshold_tokens("
        "w, C._effective_threshold_percent(w, 0.5), a or None) for w, a in pairs]))\n"
    )
    result = subprocess.run(
        [str(HERMES_PYTHON), "-P", "-c", script, json.dumps(pairs)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [wrapper.compression_trigger(w, a) for w, a in pairs]


# ----- the attempt records the three effective settings -----------------------------


def _route(thinking: bool) -> Any:
    entry = {
        **_model("a-hermes", harness="hermes", pool="lab-local"),
        "endpoint": "local",
        "endpoint_url": URL,
        "model_name": "coder",
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    return _routing([entry])


class _Attempts:
    def __init__(self, attempt: Any) -> None:
        self.attempt = attempt
        self.saved: list[dict[str, Any] | None] = []

    def get(self, attempt_id: str, *, for_update: bool = False) -> Any:
        return self.attempt

    def save(self, attempt: Any) -> None:
        self.saved.append(attempt.effective_settings)

    def list_for_execution(self, execution_id: str) -> list[Any]:
        assert execution_id == self.attempt.execution_id
        return [self.attempt]


class _Uow:
    def __init__(self, saved: dict[str, Any], attempt: Any) -> None:
        self.document = saved
        self.provider_settings = SimpleNamespace(get=self._setting)
        self.attempts = _Attempts(attempt)
        self.events = SimpleNamespace(append=lambda *a, **k: None)

    def _setting(self, name: str) -> Any:
        if name == "credentials.hermes.mount_mode":
            return None
        assert name == "harness.hermes"
        return SimpleNamespace(document=dict(self.document))

    def set_fenced_token(self, token: int) -> None:
        return None

    def commit(self) -> None:
        return None


def _attempt(attempt_id: str = "attempt-1", *, number: int = 1) -> Any:
    return SimpleNamespace(
        id=attempt_id,
        task_id="task",
        execution_id="execution",
        number=number,
        exit_class=None,
        selected_harness="hermes",
        selected_model="a-hermes",
        selected_image="image",
        resume_from_remote=False,
        routing_version=None,
        effective_settings=None,
    )


def _supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uow: _Uow, thinking: bool
) -> Supervisor:
    route = _route(thinking)
    monkeypatch.setattr("crucible.application.supervisor.load_attempt_routing", lambda *_: route)

    @contextmanager
    def factory() -> Iterator[_Uow]:
        yield uow

    supervisor = object.__new__(Supervisor)
    supervisor._uow_factory = factory  # type: ignore[assignment]
    supervisor._harnesses = default_registry()
    supervisor.fenced_token = 1
    supervisor._clock = FakeClock()
    (tmp_path / "api-key").write_text("test-key")
    supervisor._credential_sources = {"hermes": CredentialSource(str(tmp_path))}
    return supervisor


_EXECUTION: Any = SimpleNamespace(
    id="execution",
    role=ExecutionRole.IMPLEMENT,
    harness="hermes",
    model="a-hermes",
    image="image",
    policy_snapshot={},
    timeout_seconds=600,
    effort=None,
    provider="docker",
)
_TASK: Any = SimpleNamespace(id="task", external_id="FDY-0388", principal_id="tests")
_WORKSPACE = Workspace(
    attempt_id="attempt-1",
    checkout_path="/w/repo",
    identity_path="/w/identity",
    report_path="/w/report",
)


@pytest.mark.asyncio
async def test_the_attempt_records_the_three_effective_settings_and_keeps_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempt = _attempt()
    uow = _Uow(
        {"max_turns": 300, "context_length": WINDOW, "max_output_tokens": ALLOWANCE}, attempt
    )
    supervisor = _supervisor(monkeypatch, tmp_path, uow, thinking=True)
    expected = {"context_length": WINDOW, "max_output_tokens": ALLOWANCE, "thinking": True}

    spec = await supervisor._build_spec(attempt, _EXECUTION, _TASK, {})
    assert spec.effective_settings == expected
    assert spec.env["CRUCIBLE_HERMES_CONTEXT_LENGTH"] == "131072"
    assert spec.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == "32000"
    assert spec.env["CRUCIBLE_HERMES_THINKING"] == "true"

    # The launch records them on the attempt, once.
    supervisor._record_prepared(attempt.id, _WORKSPACE, spec.effective_settings)
    assert attempt.effective_settings == expected
    assert uow.attempts.saved == [expected]
    supervisor._record_prepared(attempt.id, _WORKSPACE, {**expected, "max_output_tokens": 8000})
    assert attempt.effective_settings == expected
    assert uow.attempts.saved == [expected]

    # Saved since: a later spec of the same attempt (a collect after a restart) is built
    # with what the worker was started with, not the new values.
    uow.document = {"max_turns": 300, "context_length": 98_304, "max_output_tokens": 16_000}
    again = await supervisor._build_spec(attempt, _EXECUTION, _TASK, {})
    assert again.effective_settings == expected
    assert again.env["CRUCIBLE_HERMES_CONTEXT_LENGTH"] == "131072"
    assert again.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == "32000"
    assert again.env["CRUCIBLE_HERMES_THINKING"] == "true"


# ----- a retry with a lower allowance is honoured ------------------------------------


@pytest.mark.asyncio
async def test_a_retry_attempt_with_a_lower_allowance_gets_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first attempt keeps its 32000; the retry, launched after the allowance was
    lowered, runs with the lower one rather than the default or the first attempt's."""
    first = _attempt("attempt-1")
    first.effective_settings = {
        "context_length": WINDOW,
        "max_output_tokens": ALLOWANCE,
        "thinking": False,
    }
    uow = _Uow({"max_turns": 300, "context_length": WINDOW, "max_output_tokens": 16_000}, first)
    supervisor = _supervisor(monkeypatch, tmp_path, uow, thinking=False)
    retry = _attempt("attempt-2", number=2)
    spec = await supervisor._build_spec(retry, _EXECUTION, _TASK, {})
    assert spec.effective_settings == {
        "context_length": WINDOW,
        "max_output_tokens": 16_000,
        "thinking": False,
    }
    assert spec.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == "16000"
    kept = await supervisor._build_spec(first, _EXECUTION, _TASK, {})
    assert kept.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == "32000"


# A stand-in with the 0.19.0 shape: the per-request cap Hermes sets on a retry, the one
# it sets when it boosts, and the request overrides the thinking setting rides on.
_STAND_IN_AGENT = """
class AIAgent:
    def __init__(self, base_url=None, api_key=None, provider=None, api_mode=None,
                 acp_command=None, acp_args=None, command=None, args=None, model="",
                 max_iterations=90, tool_delay=1.0, max_tokens=None, request_overrides=None):
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.request_overrides = dict(request_overrides or {})
        self._ephemeral_max_output_tokens = None
"""
_STAND_IN_RUN = """
import json
import run_agent

agent = run_agent.AIAgent(model="m", max_tokens=32000)
seen = [agent._ephemeral_max_output_tokens]
agent._ephemeral_max_output_tokens = 19936  # the gateway said the input leaves less
seen.append(agent._ephemeral_max_output_tokens)
agent._ephemeral_max_output_tokens = 32768  # a truncation boost, Hermes's own cap
seen.append(agent._ephemeral_max_output_tokens)
agent._ephemeral_max_output_tokens = None  # consumed by the request
seen.append(agent._ephemeral_max_output_tokens)
print(json.dumps({"seen": seen, "overrides": agent.request_overrides,
                  "turns": agent.max_iterations}))
"""


def _stand_in(tmp_path: Path) -> Path:
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    (hermes / "run_agent.py").write_text(_STAND_IN_AGENT, encoding="utf-8")
    info = hermes / "hermes_agent-0.19.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: hermes-agent\nVersion: 0.19.0\n", encoding="utf-8"
    )
    return hermes


def _run(python: str, script: str, env: dict[str, str], cwd: Path) -> dict[str, Any]:
    result = subprocess.run(
        [python, "-P", "-c", _wrapper().PATCHES + script],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    document: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return document


def test_hermes_retrying_with_a_lower_allowance_keeps_it_and_boosts_stop_at_the_allowance(
    tmp_path: Path,
) -> None:
    hermes = _stand_in(tmp_path)
    env = {
        "PYTHONPATH": str(hermes),
        "CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS": "32000",
        "CRUCIBLE_HERMES_THINKING": "false",
    }
    outcome = _run(sys.executable, _STAND_IN_RUN, env, tmp_path)
    assert outcome["seen"] == [None, 19936, 32000, None]
    # Thinking rides in extra_body only; no max_tokens override that would replace the
    # lower value Hermes retries with.
    assert outcome["overrides"] == {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}
    }
    assert outcome["turns"] == 90
    # Nothing given, nothing changed.
    untouched = _run(sys.executable, _STAND_IN_RUN, {"PYTHONPATH": str(hermes)}, tmp_path)
    assert untouched["seen"] == [None, 19936, 32768, None]
    assert untouched["overrides"] == {}


_HERMES_RUN = """
import json
from run_agent import AIAgent
from agent.chat_completion_helpers import build_api_kwargs

agent = AIAgent(base_url="http://127.0.0.1:9/v1", api_key="x", provider="openai-api",
                model="coder", quiet_mode=True, enabled_toolsets=["file"], platform="cli")
messages = [{"role": "user", "content": "hi"}]
first = build_api_kwargs(agent, messages)
agent._ephemeral_max_output_tokens = 19936
lower = build_api_kwargs(agent, messages)
agent._ephemeral_max_output_tokens = 32768
boosted = build_api_kwargs(agent, messages)
after = build_api_kwargs(agent, messages)
print(json.dumps({
    "trigger": agent.context_compressor.threshold_tokens,
    "requests": [k.get("max_tokens") for k in (first, lower, boosted, after)],
    "extra_body": first.get("extra_body"),
}))
"""


@needs_hermes
def test_against_hermes_0_19_0_requests_carry_the_allowance_and_a_lower_retry(
    tmp_path: Path,
) -> None:
    wrapper = _wrapper()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "home"),
        "CRUCIBLE_HERMES_CONTEXT_LENGTH": str(WINDOW),
        "CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS": str(ALLOWANCE),
        "CRUCIBLE_HERMES_THINKING": "true",
        "CRUCIBLE_HERMES_EXPECTED_TRIGGER": str(wrapper.compression_trigger(WINDOW, ALLOWANCE)),
    }
    preflight = subprocess.run(
        [str(HERMES_PYTHON), "-P", "-c", wrapper.PREFLIGHT],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=env,
    )
    assert preflight.returncode == 0, preflight.stderr
    wrapper.write_settings(tmp_path / "home", WINDOW, ALLOWANCE)
    outcome = _run(str(HERMES_PYTHON), _HERMES_RUN, env, tmp_path)
    assert outcome["trigger"] == 74_304
    assert outcome["requests"] == [32000, 19936, 32000, 32000]
    assert outcome["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
    # The defect: the same window without the allowance compresses at 98304.
    wrapper.write_settings(tmp_path / "home", WINDOW, 0)
    bare = {k: v for k, v in env.items() if not k.startswith("CRUCIBLE_HERMES_")}
    assert _run(str(HERMES_PYTHON), _HERMES_RUN, bare, tmp_path)["trigger"] == 98_304


@needs_hermes
def test_the_preflight_stops_hermes_when_its_trigger_is_not_the_wrappers(
    tmp_path: Path,
) -> None:
    wrapper = _wrapper()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "CRUCIBLE_HERMES_CONTEXT_LENGTH": str(WINDOW),
        "CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS": str(ALLOWANCE),
        "CRUCIBLE_HERMES_EXPECTED_TRIGGER": str(wrapper.compression_trigger(WINDOW)),
    }
    result = subprocess.run(
        [str(HERMES_PYTHON), "-P", "-c", wrapper.PREFLIGHT],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode != 0
    assert "Hermes compresses at 74304 input tokens" in result.stderr


def test_the_routing_entrys_thinking_reaches_the_launch() -> None:
    launch = HermesAdapter().build_launch(
        dataclasses.replace(_context(), harness_settings={"thinking": True})
    )
    assert launch.env["CRUCIBLE_HERMES_THINKING"] == "true"
