"""Report parsing with malformed inputs and exit classification from recorded tails (07,
18, S5). The tails are the shapes the real CLIs produced in S5 and S6, with no token,
account identifier, or hostname-bearing value in them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.hermes import HermesAdapter
from crucible.adapters.harness.registry import default_adapters
from crucible.adapters.harness.script import ScriptHarnessAdapter
from crucible.domain.exit_class import ExitClass
from crucible.ports.harness import ExitInfo, HarnessAdapter

CLAIM = {
    "schema_version": "1.0",
    "task_external_id": "EX-0001",
    "summary": "Added one line.",
    "self_review": {
        "documentation": ["Updated notes/c5.txt."],
        "acceptance_criteria": [{"id": "AC1", "status": "met", "evidence": "run-evidence.md"}],
        "omissions": [],
    },
    "changed_files": ["notes/c5.txt"],
    "refs": {"branch": "crucible/EX-0001", "head_sha": "a" * 40, "commits": 1},
    "checks": [{"id": "V1", "command": "echo ok", "exit": 0, "log": "V1.log"}],
    "acceptance_mapping": [{"id": "AC1", "status": "met", "evidence": "run-evidence.md"}],
    "run_evidence": ["run-evidence.md"],
    "proposed_pull_request": {"title": "c5: one line", "body": "", "closes": []},
    "limitations": [],
    "risks": [],
    "blockers": [],
    "follow_ups": [],
}


def exit_ok() -> ExitInfo:
    return ExitInfo(exit_code=0, report_present=True)


def write_lines(path: Path, events: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


# ----- report parsing --------------------------------------------------------


@pytest.mark.parametrize("adapter", default_adapters(test_fixtures=True), ids=lambda a: a.name)
def test_a_valid_report_parses_for_every_adapter(tmp_path: Path, adapter: HarnessAdapter) -> None:
    (tmp_path / "report.yaml").write_text(json.dumps(CLAIM), encoding="utf-8")
    parsed = adapter.parse_report(tmp_path, exit_ok())
    assert parsed.report_present and parsed.claim == CLAIM and parsed.errors == []
    assert parsed.blocked_md is None and parsed.transcript_name is None


def test_a_missing_report_is_reported_absent_not_invented(tmp_path: Path) -> None:
    parsed = CodexAdapter().parse_report(tmp_path, ExitInfo(exit_code=0))
    assert not parsed.report_present and parsed.claim is None and parsed.errors == []


def test_a_malformed_report_carries_the_errors(tmp_path: Path) -> None:
    (tmp_path / "report.yaml").write_text("schema_version: '1.0'\nsummary: 3\n", encoding="utf-8")
    parsed = ClaudeCodeAdapter().parse_report(tmp_path, exit_ok())
    assert parsed.report_present and parsed.claim is not None
    # A judgement field the worker must write (hades #215); the facts Crucible fills.
    assert parsed.errors and any("limitations" in ".".join(e["loc"]) for e in parsed.errors)
    assert not any("task_external_id" in ".".join(e["loc"]) for e in parsed.errors)


def test_a_report_that_is_not_yaml_is_an_error_not_a_crash(tmp_path: Path) -> None:
    (tmp_path / "report.yaml").write_text("{unterminated: [", encoding="utf-8")
    parsed = AgyAdapter().parse_report(tmp_path, exit_ok())
    assert parsed.report_present and parsed.claim is None
    assert parsed.errors[0]["type"] == "yaml"


def test_a_report_that_is_a_list_is_not_a_mapping(tmp_path: Path) -> None:
    (tmp_path / "report.yaml").write_text("- a\n- b\n", encoding="utf-8")
    parsed = ScriptHarnessAdapter().parse_report(tmp_path, exit_ok())
    assert parsed.claim is None and parsed.errors[0]["msg"] == "report is not a mapping"


def test_blocked_md_and_progress_lines_are_read(tmp_path: Path) -> None:
    (tmp_path / "blocked.md").write_text("# Blocked\n\nWhich database?\n", encoding="utf-8")
    write_lines(
        tmp_path / "progress.jsonl",
        [{"milestone": "read identity"}, {"milestone": "edited file"}],
    )
    (tmp_path / "progress.jsonl").open("a", encoding="utf-8").write("not json\n")
    parsed = CodexAdapter().parse_report(tmp_path, ExitInfo(exit_code=75, blocked_present=True))
    assert parsed.blocked_md is not None and "Which database?" in parsed.blocked_md
    assert [p["milestone"] for p in parsed.progress] == ["read identity", "edited file"]


# ----- transcript metrics (05b) ---------------------------------------------


def test_claude_code_metrics_come_from_the_result_line(tmp_path: Path) -> None:
    write_lines(
        tmp_path / "transcript.jsonl",
        [
            {"type": "system", "subtype": "init", "model": "claude-haiku-4-5"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
            {
                "type": "result",
                "subtype": "success",
                "usage": {"input_tokens": 1200, "output_tokens": 45},
                "total_cost_usd": 0.0031,
                "modelUsage": {"claude-haiku-4-5": {"inputTokens": 1200}},
            },
        ],
    )
    parsed = ClaudeCodeAdapter().parse_report(tmp_path, ExitInfo(exit_code=0))
    assert parsed.metrics.model == "claude-haiku-4-5"
    assert (parsed.metrics.tokens_in, parsed.metrics.tokens_out) == (1200, 45)
    assert parsed.metrics.cost_usd == pytest.approx(0.0031)
    assert parsed.metrics.source == "harness_transcript"
    assert parsed.transcript_lines == 3 and parsed.transcript_name == "transcript.jsonl"


def test_codex_metrics_sum_over_turns(tmp_path: Path) -> None:
    write_lines(
        tmp_path / "transcript.jsonl",
        [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
            {"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 20}},
        ],
    )
    parsed = CodexAdapter().parse_report(tmp_path, ExitInfo(exit_code=0))
    assert (parsed.metrics.tokens_in, parsed.metrics.tokens_out) == (300, 30)
    assert parsed.metrics.cost_usd is None


def test_agy_metrics_come_from_the_result_line(tmp_path: Path) -> None:
    """The shape the 1.2.4 CLI emitted in the C5 live run: `event` keyed, the body
    nested under the event's name, usage as input and output tokens."""
    write_lines(
        tmp_path / "transcript.jsonl",
        [
            {"event": "init", "init": {"cwd": "/crucible/repo"}},
            {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE"}},
            {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "done",
                    "usage": {"input_tokens": 900, "output_tokens": 30, "thinking_tokens": 5},
                },
            },
        ],
    )
    parsed = AgyAdapter().parse_report(tmp_path, ExitInfo(exit_code=0))
    assert (parsed.metrics.tokens_in, parsed.metrics.tokens_out) == (900, 30)
    assert parsed.transcript_lines == 3


def test_a_harness_that_reports_nothing_leaves_null(tmp_path: Path) -> None:
    """05b: null counts make the pool fall back to counting attempts."""
    write_lines(tmp_path / "transcript.jsonl", [{"event": "result", "result": {"status": "OK"}}])
    parsed = AgyAdapter().parse_report(tmp_path, ExitInfo(exit_code=0))
    assert parsed.metrics.tokens_in is None and parsed.metrics.tokens_out is None
    assert ScriptHarnessAdapter().parse_report(tmp_path, exit_ok()).metrics.source == "none"


def test_hermes_usage_is_run_evidence_and_supplies_metrics(tmp_path: Path) -> None:
    (tmp_path / "hermes-usage.json").write_text(
        json.dumps(
            {
                "completed": True,
                "failed": False,
                "model": "gpt-oss:120b",
                "input_tokens": 120,
                "output_tokens": 30,
                "api_calls": 2,
                "duration_ms": 456,
                "tool_calls": 3,
            }
        ),
        encoding="utf-8",
    )
    parsed = HermesAdapter().parse_report(tmp_path, ExitInfo(exit_code=0))
    assert parsed.run_evidence_error is None
    assert (parsed.metrics.tokens_in, parsed.metrics.tokens_out) == (120, 30)
    assert (parsed.metrics.duration_ms, parsed.metrics.tool_calls) == (456, 3)


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"completed": true}'])
def test_hermes_missing_or_bad_usage_is_an_evidence_anomaly(
    tmp_path: Path, content: str | None
) -> None:
    if content is not None:
        (tmp_path / "hermes-usage.json").write_text(content, encoding="utf-8")
    parsed = HermesAdapter().parse_report(tmp_path, ExitInfo(exit_code=0))
    assert parsed.run_evidence_error is not None


# ----- exit classification (S5 table) ----------------------------------------

# The recorded shapes, sanitized: the text each CLI printed for a missing or expired
# login, and the quota shapes, with nothing identifying in them.
CLAUDE_NOT_LOGGED_IN = (
    '{"type":"result","subtype":"success","is_error":true,'
    '"result":"Not logged in · Please run /login","duration_ms":812}\n'
)
CODEX_401 = (
    '{"type":"error","message":"Reconnecting... 5/5"}\n'
    '{"type":"turn.failed","error":{"message":"401 Unauthorized: Missing bearer or basic '
    'authentication in header"}}\n'
)
AGY_AUTH_STDOUT = (
    '{"type":"result","conversation_id":"","status":"ERROR","response":"",'
    '"error":"authentication failed or timed out","duration_seconds":0.4}\n'
)
AGY_AUTH_STDERR = "Error: authentication required. Run 'agy' to log in, then retry.\n"


def test_claude_code_auth_failure_is_on_stdout_with_an_empty_stderr() -> None:
    """S5: the `result` text carries it; stderr is empty."""
    cls = ClaudeCodeAdapter().classify_exit(ExitInfo(exit_code=1), CLAUDE_NOT_LOGGED_IN, "")
    assert cls is ExitClass.AUTH_FAILURE


def test_codex_401_is_an_auth_failure() -> None:
    assert (
        CodexAdapter().classify_exit(ExitInfo(exit_code=1), CODEX_401, "") is ExitClass.AUTH_FAILURE
    )


def test_agy_exit_1_with_a_well_formed_result_line_classifies_on_its_text() -> None:
    adapter = AgyAdapter()
    assert (
        adapter.classify_exit(ExitInfo(exit_code=1), AGY_AUTH_STDOUT, AGY_AUTH_STDERR)
        is ExitClass.AUTH_FAILURE
    )
    # Either tail alone is enough (07: both tails are consulted).
    assert adapter.classify_exit(ExitInfo(exit_code=1), "", AGY_AUTH_STDERR) is (
        ExitClass.AUTH_FAILURE
    )


@pytest.mark.parametrize(
    ("adapter", "tail"),
    [
        (
            ClaudeCodeAdapter(),
            '{"type":"result","is_error":true,"result":"You\'ve hit your usage limit"}',
        ),
        (
            # The live sample (C5b): the window used up, then a synthetic result.
            ClaudeCodeAdapter(),
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
            '"rateLimitType":"five_hour","overageStatus":"rejected",'
            '"overageDisabledReason":"out_of_credits","isUsingOverage":false}}\n'
            '{"type":"result","subtype":"success","is_error":false,"duration_api_ms":0,'
            '"total_cost_usd":0,"terminal_reason":"api_error"}',
        ),
        (CodexAdapter(), '{"type":"turn.failed","error":{"code":"usage_limit_reached"}}'),
        (AgyAdapter(), '{"type":"result","status":"ERROR","error":"RESOURCE_EXHAUSTED: quota"}'),
    ],
    ids=["claude_code", "claude_code_live_window", "codex", "agy"],
)
def test_quota_exhaustion_from_the_tails(adapter: HarnessAdapter, tail: str) -> None:
    assert adapter.classify_exit(ExitInfo(exit_code=1), tail, "") is ExitClass.QUOTA_EXHAUSTED


@pytest.mark.parametrize("adapter", default_adapters(test_fixtures=True), ids=lambda a: a.name)
def test_a_pattern_never_turns_a_clean_exit_into_a_failure(adapter: HarnessAdapter) -> None:
    """Claude Code's rate_limit_event lines report utilization on successful runs (S1)."""
    noisy = '{"type":"rate_limit_event","utilization":0.4}\n{"type":"result","subtype":"success"}'
    assert adapter.classify_exit(ExitInfo(exit_code=0, report_present=True), noisy, "") is (
        ExitClass.COMPLETED
    )
    assert adapter.classify_exit(ExitInfo(exit_code=0), CLAUDE_NOT_LOGGED_IN, "") is (
        ExitClass.COMPLETED_WITHOUT_REPORT
    )


def test_the_deterministic_table_holds_before_any_pattern() -> None:
    adapter = CodexAdapter()
    assert adapter.classify_exit(ExitInfo(exit_code=75, blocked_present=True), CODEX_401, "") is (
        ExitClass.BLOCKED
    )
    assert adapter.classify_exit(ExitInfo(exit_code=75), "", "") is ExitClass.CRASHED
    assert adapter.classify_exit(ExitInfo(exit_code=70), CODEX_401, "") is ExitClass.AUTH_FAILURE
    assert adapter.classify_exit(ExitInfo(exit_code=70), "", "") is ExitClass.ENVIRONMENT
    assert adapter.classify_exit(ExitInfo(exit_code=1), "something else", "") is ExitClass.CRASHED
    assert adapter.classify_exit(ExitInfo(exit_code=137, timed_out=True), CODEX_401, "") is (
        ExitClass.TIMEOUT
    )
    assert adapter.classify_exit(ExitInfo(exit_code=137, killed=True), CODEX_401, "") is (
        ExitClass.KILLED
    )
    assert adapter.classify_exit(ExitInfo(exit_code=None, lost=True), "", "") is ExitClass.LOST
    # S5: 137 with OOMKilled and no signal from Crucible is an environment failure.
    assert adapter.classify_exit(ExitInfo(exit_code=137, oom_killed=True), "", "") is (
        ExitClass.ENVIRONMENT
    )


def test_hermes_usage_and_provider_failures_precede_report_classification(tmp_path: Path) -> None:
    adapter = HermesAdapter()
    usage = tmp_path / "hermes-usage.json"
    usage.write_text('{"completed":false,"failed":true}', encoding="utf-8")
    assert adapter.classify_exit(ExitInfo(exit_code=0, report_present=True), "", "", tmp_path) is (
        ExitClass.CRASHED
    )
    # Hades #353: Hermes said the provider failed and the gateway did not answer.
    assert (
        adapter.classify_exit(
            ExitInfo(exit_code=0, report_present=True), "", "connection refused", tmp_path
        )
        is ExitClass.INFRASTRUCTURE
    )
    assert (
        adapter.classify_exit(ExitInfo(exit_code=0, report_present=True), "", "HTTP 500", tmp_path)
        is ExitClass.PROVIDER_ERROR
    )
    usage.write_text('{"completed":true,"failed":false}', encoding="utf-8")
    assert adapter.classify_exit(
        ExitInfo(exit_code=75, blocked_present=True), "", "", tmp_path
    ) is (ExitClass.PROVIDER_ERROR)
    assert (
        adapter.classify_exit(
            ExitInfo(exit_code=75, blocked_present=True), "", "quota exceeded", tmp_path
        )
        is ExitClass.QUOTA_EXHAUSTED
    )
    assert adapter.provider_quota_event("quota exceeded", "") is None


def test_hermes_termination_facts_precede_usage_and_provider_text(tmp_path: Path) -> None:
    (tmp_path / "hermes-usage.json").write_text(
        '{"completed":true,"failed":false}', encoding="utf-8"
    )
    adapter = HermesAdapter()
    assert adapter.classify_exit(ExitInfo(exit_code=None, lost=True), "", "", tmp_path) is (
        ExitClass.LOST
    )
    assert adapter.classify_exit(
        ExitInfo(exit_code=137, timed_out=True), "", "quota exceeded", tmp_path
    ) is (ExitClass.TIMEOUT)
    assert adapter.classify_exit(
        ExitInfo(exit_code=137, killed=True), "", "HTTP 503", tmp_path
    ) is (ExitClass.KILLED)
    assert adapter.classify_exit(
        ExitInfo(exit_code=137, oom_killed=True), "", "connection refused", tmp_path
    ) is (ExitClass.ENVIRONMENT)


def test_hermes_completed_usage_then_report_and_missing_usage_order(tmp_path: Path) -> None:
    usage = tmp_path / "hermes-usage.json"
    usage.write_text('{"completed":true,"failed":false}', encoding="utf-8")
    adapter = HermesAdapter()
    assert adapter.classify_exit(ExitInfo(exit_code=0, report_present=True), "", "", tmp_path) is (
        ExitClass.COMPLETED
    )
    assert adapter.classify_exit(ExitInfo(exit_code=0), "", "", tmp_path) is (
        ExitClass.COMPLETED_WITHOUT_REPORT
    )
    usage.unlink()
    assert adapter.classify_exit(ExitInfo(exit_code=0, report_present=True), "", "", tmp_path) is (
        ExitClass.COMPLETED
    )
    parsed = adapter.parse_report(tmp_path, ExitInfo(exit_code=0, report_present=True))
    assert parsed.run_evidence_error == "missing hermes-usage.json"


@pytest.mark.parametrize(
    ("stderr", "exit_class"),
    [
        ("HTTP 500", ExitClass.PROVIDER_ERROR),
        # Hades #353: a gateway that answered 502 or did not answer is infrastructure.
        ("bad gateway", ExitClass.INFRASTRUCTURE),
        ("connection refused", ExitClass.INFRASTRUCTURE),
    ],
)
def test_hermes_local_provider_failures_never_mark_quota(
    tmp_path: Path, stderr: str, exit_class: ExitClass
) -> None:
    (tmp_path / "hermes-usage.json").write_text(
        '{"completed":false,"failed":true}', encoding="utf-8"
    )
    adapter = HermesAdapter()
    assert adapter.classify_exit(ExitInfo(exit_code=75), "", stderr, tmp_path) is exit_class
    assert adapter.provider_quota_event("", stderr) is None
    assert not adapter.provider_quota_exhausted("", stderr)


def test_a_bare_number_in_a_crashed_agy_tail_is_not_quota() -> None:
    """A line number or a token count is three digits too; the quota class needs the
    provider's own words."""
    tail = '{"event":"step_update","step_update":{"error":"panic at foo.js:429 (line 1429)"}}\n'
    assert AgyAdapter().classify_exit(ExitInfo(exit_code=1), tail, "") is ExitClass.CRASHED
    assert (
        AgyAdapter().classify_exit(ExitInfo(exit_code=1), '{"error":"429 Too Many Requests"}', "")
        is ExitClass.QUOTA_EXHAUSTED
    )


def test_reset_timestamp_must_be_on_the_quota_event_line() -> None:
    adapter = CodexAdapter()
    unrelated = '{"reset_at":"2026-09-21T12:00:00Z","type":"usage"}'
    assert adapter.quota_reset_at(unrelated, "") is None
    quota = '{"error":"usage_limit_reached","reset_at":"2026-09-21T12:00:00Z"}'
    assert adapter.quota_reset_at(quota, "") == datetime(2026, 9, 21, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("adapter", "event"),
    [
        (
            ClaudeCodeAdapter(),
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
            '"overageDisabledReason":"out_of_credits"}}',
        ),
        (CodexAdapter(), '{"type":"turn.failed","error":{"code":"usage_limit_reached"}}'),
        (
            AgyAdapter(),
            '{"type":"result","status":"ERROR","error":"RESOURCE_EXHAUSTED: quota"}',
        ),
        (ScriptHarnessAdapter(), '{"error":"scripted_quota_exhausted"}'),
    ],
)
def test_only_structured_harness_events_authorize_a_shared_quota_mark(
    adapter: HarnessAdapter, event: str
) -> None:
    assert adapter.provider_quota_exhausted(event, "") is True
    assert adapter.provider_quota_exhausted("agent says usage limit reached", "") is False


def test_provider_reset_comes_only_from_the_authoritative_event() -> None:
    adapter = ClaudeCodeAdapter()
    assistant = (
        '{"type":"assistant","rate_limit_event":{"status":"rejected",'
        '"reason":"out_of_credits"},"metadata":{"reset_at":"2026-09-21T12:00:00Z"}}'
    )
    assert adapter.provider_quota_event(assistant, "") is None
    refusal = (
        '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
        '"overageDisabledReason":"out_of_credits",'
        '"reset_at":"2026-09-21T12:00:00Z"}}'
    )
    event = adapter.provider_quota_event(refusal, "")
    assert event is not None
    assert event.reset_at == datetime(2026, 9, 21, 12, tzinfo=UTC)


def test_hermes_blocked_md_on_a_clean_exit_is_blocked(tmp_path: Path) -> None:
    """FDY-0140: Hermes exits 0 when the model stops; `blocked.md` then is the model's
    escalation, even beside a report or a usage record that says the run failed.
    Hermes's own 75 is still a provider failure (the test above)."""
    usage = tmp_path / "hermes-usage.json"
    adapter = HermesAdapter()
    for document in (
        '{"completed": true, "failed": false}',
        '{"completed": false, "failed": true}',
    ):
        usage.write_text(document, encoding="utf-8")
        exit = ExitInfo(exit_code=0, report_present=True, blocked_present=True)
        assert adapter.classify_exit(exit, "", "", tmp_path) is ExitClass.BLOCKED


def test_hermes_records_a_run_that_reached_its_turn_limit(tmp_path: Path) -> None:
    usage = tmp_path / "hermes-usage.json"
    usage.write_text(
        '{"completed": false, "failed": false, "api_calls": 301, "max_turns": 300,'
        ' "turn_limit_reached": true}',
        encoding="utf-8",
    )
    parsed = HermesAdapter().parse_report(tmp_path, ExitInfo(exit_code=0))
    assert parsed.limit_reached == (
        "Hermes reached its turn limit of 300 after 301 model calls and stopped before finishing"
    )
    usage.write_text('{"completed": true, "failed": false, "api_calls": 12}', encoding="utf-8")
    assert HermesAdapter().parse_report(tmp_path, ExitInfo(exit_code=0)).limit_reached is None
