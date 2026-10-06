"""The adapters as one registry (07): the five real harnesses, and the script harness
only when test fixtures are on (crucible#124)."""

from __future__ import annotations

from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.hermes import HermesAdapter
from crucible.adapters.harness.qwen_code import QwenCodeAdapter
from crucible.adapters.harness.script import ScriptHarnessAdapter
from crucible.application.harnesses import HarnessRegistry
from crucible.ports.harness import HarnessAdapter


def default_adapters(*, test_fixtures: bool = False) -> tuple[HarnessAdapter, ...]:
    """The script harness reports completion without doing any work, so it is a test
    fixture: registered only when `test_fixtures` is on (18, crucible#124)."""
    real: tuple[HarnessAdapter, ...] = (
        ClaudeCodeAdapter(),
        CodexAdapter(),
        AgyAdapter(),
        HermesAdapter(),
        QwenCodeAdapter(),
    )
    return (*real, ScriptHarnessAdapter()) if test_fixtures else real


def default_registry(*, test_fixtures: bool = False) -> HarnessRegistry:
    return HarnessRegistry(default_adapters(test_fixtures=test_fixtures))
