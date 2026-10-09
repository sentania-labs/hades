"""Usage per attempt (hades #604): each harness's own usage report, parsed into tokens in,
tokens out, prompt-cache reads and cost where it reports one. The transcripts are the
recorded fixtures under tests/fixtures_data; Hermes's usage record is the one its launch
wrapper writes (test_issue_387), and Qwen Code's stream is the adapter tests' events."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.hermes import HermesAdapter
from crucible.adapters.harness.qwen_code import QwenCodeAdapter
from crucible.ports.harness import ExitInfo, ReportMetrics
from tests.unit.test_issue_387_failure_usage import (
    ISSUE_CALLS,
    RAISED_USAGE,
    _enrich,
    _session_db,
)
from tests.unit.test_qwen_code_adapter import events as qwen_events

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures_data"
TRANSCRIPTS = FIXTURES / "transcripts"
INTERRUPTIONS = FIXTURES / "interruptions"


def _parse(adapter: Any, transcript: Path, tmp_path: Path) -> ReportMetrics:
    shutil.copyfile(transcript, tmp_path / "transcript.jsonl")
    metrics: ReportMetrics = adapter.parse_report(tmp_path, ExitInfo(exit_code=0)).metrics
    return metrics


@pytest.mark.parametrize(
    ("fixture", "tokens_in", "tokens_out", "cost"),
    [
        # Two turns: the background task's notification started a second one. The run
        # is the session's running totals on the last result, 2 + 1 in and out.
        ("claude-code-2.1.280-background-completed.jsonl", 3, 3, 0.000036),
        ("claude-code-2.1.280-background-killed.jsonl", 2, 2, 0.000024),
        ("claude-code-2.1.280-background-requested.jsonl", 2, 2, 0.000024),
    ],
)
def test_claude_code_usage_is_the_sessions_running_total(
    tmp_path: Path, fixture: str, tokens_in: int, tokens_out: int, cost: float
) -> None:
    metrics = _parse(ClaudeCodeAdapter(), TRANSCRIPTS / fixture, tmp_path)
    assert (metrics.tokens_in, metrics.tokens_out) == (tokens_in, tokens_out)
    assert metrics.tokens_cache_read == 0
    assert metrics.cost_usd == pytest.approx(cost)
    assert metrics.model == "claude-sonnet-5"
    assert metrics.source == "harness_transcript"


def test_claude_code_without_model_usage_sums_the_turns(tmp_path: Path) -> None:
    """The interruption fixture's result lines carry `usage` and `total_cost_usd` only."""
    metrics = _parse(
        ClaudeCodeAdapter(), INTERRUPTIONS / "claude-code-pytest-failure.jsonl", tmp_path
    )
    assert (metrics.tokens_in, metrics.tokens_out) == (2, 2)
    assert metrics.tokens_cache_read is None
    assert metrics.cost_usd == pytest.approx(0.0009)


def test_claude_code_reads_cache_reads_from_model_usage(tmp_path: Path) -> None:
    lines = [
        {"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
        {
            "type": "result",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 900},
            "total_cost_usd": 0.5,
            "modelUsage": {
                "claude-opus-5-5": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 900,
                    "costUSD": 0.4,
                },
                "claude-haiku-4-5": {
                    "inputTokens": 4,
                    "outputTokens": 2,
                    "cacheReadInputTokens": 100,
                    "costUSD": 0.1,
                },
            },
        },
    ]
    (tmp_path / "transcript.jsonl").write_text("\n".join(map(json.dumps, lines)))
    metrics = ClaudeCodeAdapter().parse_report(tmp_path, ExitInfo(exit_code=0)).metrics
    assert (metrics.tokens_in, metrics.tokens_out, metrics.tokens_cache_read) == (14, 7, 1000)
    assert metrics.cost_usd == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("transcript", "tokens_in", "tokens_out"),
    [
        (TRANSCRIPTS / "codex-0.156.0-session-abandoned.jsonl", 2, 2),
        (TRANSCRIPTS / "codex-0.156.0-session-polled.jsonl", 3, 3),
        (INTERRUPTIONS / "codex-exec-pytest-failure.jsonl", 812, 64),
    ],
)
def test_codex_usage_sums_its_turns(
    tmp_path: Path, transcript: Path, tokens_in: int, tokens_out: int
) -> None:
    metrics = _parse(CodexAdapter(), transcript, tmp_path)
    assert (metrics.tokens_in, metrics.tokens_out) == (tokens_in, tokens_out)
    assert metrics.tokens_cache_read == 0
    # Codex reports no cost.
    assert metrics.cost_usd is None


def test_codex_cache_reads_are_summed(tmp_path: Path) -> None:
    lines = [
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60}},
        {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 9}},
        {"type": "turn.completed", "usage": {"input_tokens": 40, "cached_input_tokens": 30}},
    ]
    (tmp_path / "transcript.jsonl").write_text("\n".join(map(json.dumps, lines)))
    metrics = CodexAdapter().parse_report(tmp_path, ExitInfo(exit_code=0)).metrics
    assert (metrics.tokens_in, metrics.tokens_out, metrics.tokens_cache_read) == (190, 9, 90)


def test_hermes_usage_comes_from_its_usage_record(tmp_path: Path) -> None:
    """The record the launch wrapper enriched from Hermes's session rows (#387)."""
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    _enrich(tmp_path, RAISED_USAGE)
    metrics = HermesAdapter().parse_report(tmp_path, ExitInfo(exit_code=1)).metrics
    assert (metrics.tokens_in, metrics.tokens_out) == (6_206_631, 50_667)
    assert metrics.tokens_cache_read == 1_000
    assert metrics.cost_usd == pytest.approx(1.3125)
    assert metrics.source == "hermes_usage"


def test_hermes_without_a_usage_record_reports_nothing(tmp_path: Path) -> None:
    metrics = HermesAdapter().parse_report(tmp_path, ExitInfo(exit_code=1)).metrics
    assert metrics == ReportMetrics()


def _qwen(tmp_path: Path, lines: list[dict[str, Any]]) -> ReportMetrics:
    (tmp_path / "transcript.jsonl").write_text("\n".join(map(json.dumps, lines)))
    metrics: ReportMetrics = QwenCodeAdapter().parse_report(tmp_path, ExitInfo(exit_code=0)).metrics
    return metrics


def test_qwen_code_usage_comes_from_the_result_event(tmp_path: Path) -> None:
    metrics = _qwen(tmp_path, qwen_events())
    assert (metrics.tokens_in, metrics.tokens_out) == (100, 25)
    assert metrics.tokens_cache_read is None
    assert metrics.cost_usd is None


def test_qwen_code_sums_every_launch_and_reads_its_stats(tmp_path: Path) -> None:
    """A relaunch after a transport error is a new session with its own result event
    (hades #490), so the run is the sum; a result without `usage` has `stats`."""
    first = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "usage": {"input_tokens": 70, "output_tokens": 5, "cache_read_input_tokens": 40},
    }
    second = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "stats": {
            "models": {
                "qwen-lane": {
                    "api": {"totalRequests": 3},
                    "tokens": {"prompt": 300, "candidates": 30, "cached": 200, "total": 330},
                }
            }
        },
    }
    metrics = _qwen(tmp_path, [*qwen_events()[:3], first, second])
    assert (metrics.tokens_in, metrics.tokens_out, metrics.tokens_cache_read) == (370, 35, 240)
    assert metrics.model == "qwen-lane"
