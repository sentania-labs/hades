"""The principal harness spike script (hades #208, work item 1) parses its arguments.

The experiments themselves need the worker image's CLIs and a mounted credential; this
test covers only the argument parsing so the script stays importable and runnable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "spikes" / "principal_harness.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("principal_harness", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The dataclass decorator looks its module up in sys.modules.
    sys.modules["principal_harness"] = module
    spec.loader.exec_module(module)
    return module


def test_defaults_point_at_the_worker_mounts() -> None:
    module = _load()
    args = module.build_parser().parse_args(["claude-resume"])
    assert args.experiment == "claude-resume"
    assert args.model == module.DEFAULT_MODEL
    assert args.token_file == Path("/home/worker/.claude/oauth-token")
    assert args.codex_home == Path("/home/worker/.codex")
    assert args.out == Path("/tmp/principal-harness-spike")


def test_every_experiment_is_a_choice_and_overrides_parse(tmp_path: Path) -> None:
    module = _load()
    parser = module.build_parser()
    for name in module.EXPERIMENTS:
        assert parser.parse_args([name]).experiment == name
    args = parser.parse_args(
        [
            "--out",
            str(tmp_path),
            "--model",
            "claude-opus-5-5",
            "--codex-model",
            "gpt-5-codex",
            "--token-file",
            str(tmp_path / "token"),
            "--codex-home",
            str(tmp_path / "codex"),
            "all",
        ]
    )
    assert args.out == tmp_path
    assert args.model == "claude-opus-5-5"
    assert args.codex_model == "gpt-5-codex"
    assert args.token_file == tmp_path / "token"
    assert args.codex_home == tmp_path / "codex"
    assert args.experiment == "all"


def test_unknown_experiment_is_refused() -> None:
    module = _load()
    with pytest.raises(SystemExit):
        module.build_parser().parse_args(["claude-interactive"])


def test_token_env_is_read_from_the_file_only(tmp_path: Path) -> None:
    module = _load()
    token = tmp_path / "oauth-token"
    token.write_text("sk-ant-oat01-example\n", encoding="utf-8")
    env = module.claude_env(tmp_path / "config", token)
    assert env[module.TOKEN_ENV] == "sk-ant-oat01-example"
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "config")
    assert "CLAUDECODE" not in env
    assert module.TOKEN_ENV not in module.claude_env(tmp_path, tmp_path / "missing")
