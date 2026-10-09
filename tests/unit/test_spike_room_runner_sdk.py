"""The room runner spike script (hades #208, item 1b) parses its arguments.

The experiments need the worker image's claude binary, a mounted credential and the
Claude Agent SDK supplied by `uv run --with`; this test covers only the argument parsing
and the child environment, which need nothing but the standard library.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "spikes" / "room_runner_sdk.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("room_runner_sdk", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["room_runner_sdk"] = module
    spec.loader.exec_module(module)
    return module


def test_defaults_point_at_the_worker_mounts() -> None:
    module = _load()
    args = module.build_parser().parse_args(["multi-turn"])
    assert args.experiment == "multi-turn"
    assert args.model == module.DEFAULT_MODEL
    assert args.cli_path == "/usr/local/bin/claude"
    assert args.token_file == Path("/home/worker/.claude/oauth-token")
    assert args.out == Path("/tmp/room-runner-sdk-spike")
    assert args.idle_seconds == 3.0
    assert args.stop_after_deltas == 25


def test_every_experiment_is_a_choice_and_overrides_parse(tmp_path: Path) -> None:
    module = _load()
    parser = module.build_parser()
    assert set(module.EXPERIMENTS) == set(module.RUNNERS)
    for name in (*module.EXPERIMENTS, "all"):
        assert parser.parse_args([name]).experiment == name
    args = parser.parse_args(
        [
            "--out",
            str(tmp_path),
            "--model",
            "claude-opus-5-5",
            "--cli-path",
            "/opt/claude",
            "--token-file",
            str(tmp_path / "token"),
            "--idle-seconds",
            "0.5",
            "--stop-after-deltas",
            "7",
            "all",
        ]
    )
    assert args.out == tmp_path
    assert args.model == "claude-opus-5-5"
    assert args.cli_path == "/opt/claude"
    assert args.token_file == tmp_path / "token"
    assert args.idle_seconds == 0.5
    assert args.stop_after_deltas == 7
    assert args.experiment == "all"


def test_unknown_experiment_and_bad_numbers_are_refused() -> None:
    module = _load()
    parser = module.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["room"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--stop-after-deltas", "many", "kill"])
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_child_env_carries_the_token_from_the_file_only(tmp_path: Path) -> None:
    module = _load()
    token = tmp_path / "oauth-token"
    token.write_text("sk-ant-oat01-example\n", encoding="utf-8")
    env = module.child_env(tmp_path / "config", token)
    assert env == {
        "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
        "TERM": "dumb",
        module.TOKEN_ENV: "sk-ant-oat01-example",
    }
    assert module.TOKEN_ENV not in module.child_env(tmp_path, tmp_path / "missing")


def test_parent_harness_variables_are_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-ours")
    monkeypatch.setenv("SPIKE_KEEP", "yes")
    # The scrub also removes variables this test did not set; put them all back.
    saved = dict(os.environ)
    try:
        removed = module.scrub_parent_environment()
        assert {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ANTHROPIC_API_KEY"} <= set(removed)
        assert not any(k.startswith(("CLAUDE", "ANTHROPIC")) for k in os.environ)
        assert os.environ["SPIKE_KEEP"] == "yes"
    finally:
        os.environ.clear()
        os.environ.update(saved)
