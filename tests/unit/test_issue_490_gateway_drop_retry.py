"""Hades #490: a local gateway connection drop is retried in the wrapper, rerouted on
`provider_error`, and still leaves a bundle.

A local-lane attempt (Qwen Code or Hermes on the lab gateway) whose connection the
gateway dropped ended at once: neither CLI retries the call, the supervisor retried
nothing for `provider_error` (not in `retryable_exit`), and with no commit on the branch
`git bundle create` refused the empty range, so the verifier wrote "no bundle was
produced", no correction could resume and nothing held the tree the worker left.

Now:

- AC1: the wrappers (images/worker/crucible-qwen-code.py, crucible-hermes.py) read how the
  run ended and, for a transport-level API error (the gateway gave no answer: refused,
  reset, timed out, closed mid-answer), start the harness again after a pause, up to
  three times, before giving up with that run's exit status.
- AC2: an attempt that still ends `provider_error` reroutes by the rule hades #373 sets
  for a model-only refusal: the failed route is excluded, the pool stays as ADR 0028
  left it, and the next eligible candidate launches under `reroute_max`, with no
  operator action and no wake of its own.
- AC3: the collector writes `work_branch.bundle` for a failed attempt with no commit too
  (the branch tip alone), so it verifies, the worker's log is collected beside it and a
  correction can resume from the state the worker left.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

from crucible.adapters.execution import scripts
from crucible.adapters.execution.collected import read_outputs
from crucible.adapters.harness.hermes import USAGE_NAME
from crucible.application.supervisor import REROUTED_EXIT_CLASSES, Supervisor, _Pending
from crucible.contracts.policy import RoutingPolicyV1
from crucible.domain.entities import AttemptMetrics, Event
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.ports.execution import OUTPUT_MOUNT, REPO_MOUNT, REPORT_MOUNT, CollectedOutputs
from tests.collector_tools import collector_env
from tests.unit.test_class_routing import NOW, _model, _routing
from tests.unit.test_issue_353_infrastructure_interruptions import (
    _events,
    _finish,
    _running,
    _wakes,
)

ROOT = Path(__file__).resolve().parents[2]
QWEN_WRAPPER = ROOT / "images" / "worker" / "crucible-qwen-code.py"
HERMES_WRAPPER = ROOT / "images" / "worker" / "crucible-hermes.py"
QWEN = "qwen-lane"
HERMES = "hermes-lane"
CODEX_FALLBACK = "gpt-5-codex"
POOL = "lab-local"
GATEWAY = "https://gateway.example/v1"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _qwen() -> ModuleType:
    return _load(QWEN_WRAPPER, "crucible_qwen_code_490")


def _hermes() -> ModuleType:
    return _load(HERMES_WRAPPER, "crucible_hermes_490")


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


# ----- AC1: the wrappers retry a transport-level API error with backoff ----------------


@pytest.mark.parametrize("wrapper", [_qwen, _hermes])
def test_the_run_is_started_again_after_a_transport_error_until_the_gateway_answers(
    wrapper: Any,
) -> None:
    """Two launches end on a dropped connection, the third gets an answer: the result is
    the third run's exit, after the first two backoff pauses."""
    module = wrapper()
    outcomes = iter([(1, "connection reset"), (1, "connection reset"), (0, None)])
    launched: list[int] = []
    slept: list[float] = []

    def launch(number: int) -> tuple[int, str | None]:
        launched.append(number)
        return next(outcomes)

    assert module.run_with_retry(launch, sleep=slept.append) == (0, 2)
    assert launched == [0, 1, 2]
    assert slept == list(module.BACKOFF_SECONDS[:2])
    assert module.MAX_RETRIES == 3
    assert slept[0] < slept[1] < module.BACKOFF_SECONDS[2]


@pytest.mark.parametrize("wrapper", [_qwen, _hermes])
def test_three_retries_then_the_last_runs_exit_is_kept(
    wrapper: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    module = wrapper()
    launched: list[int] = []
    slept: list[float] = []

    def launch(number: int) -> tuple[int, str | None]:
        launched.append(number)
        return 75, "connection error"

    assert module.run_with_retry(launch, sleep=slept.append) == (75, 3)
    assert launched == [0, 1, 2, 3]
    assert slept == list(module.BACKOFF_SECONDS)
    err = capsys.readouterr().err
    assert "retry 1 of 3 in 5s" in err and "retry 3 of 3 in 45s" in err
    assert "after 3 retries; giving up with exit 75" in err


@pytest.mark.parametrize("wrapper", [_qwen, _hermes])
def test_an_answer_the_gateway_gave_is_not_retried_and_a_signal_stops_retrying(
    wrapper: Any,
) -> None:
    module = wrapper()
    slept: list[float] = []
    # A run that ended on something other than a transport failure is given up at once.
    assert module.run_with_retry(lambda _n: (1, None), sleep=slept.append) == (1, 0)
    assert slept == []
    # A termination signal during the run: nothing is started again.
    assert module.run_with_retry(
        lambda _n: (143, "connection reset"), sleep=slept.append, stopped=lambda: True
    ) == (143, 0)
    assert slept == []


@pytest.mark.parametrize("wrapper", [_qwen, _hermes])
def test_a_signal_during_backoff_does_not_launch_again(wrapper: Any) -> None:
    module = wrapper()
    launched: list[int] = []
    stopped = False

    def launch(number: int) -> tuple[int, str | None]:
        launched.append(number)
        return 75, "connection reset"

    def pause(_delay: float) -> None:
        nonlocal stopped
        stopped = True

    assert module.run_with_retry(launch, sleep=pause, stopped=lambda: stopped) == (75, 0)
    assert launched == [0]


@pytest.mark.parametrize(
    ("text", "transport"),
    [
        ("TypeError: fetch failed", True),
        ("Error: read ECONNRESET", True),
        ("connect ECONNREFUSED 10.0.0.5:8000", True),
        ("socket hang up", True),
        ("Connection error.", True),
        ("Request timed out.", True),
        ("UND_ERR_SOCKET: other side closed", True),
        ("Error code: 503 - {'error': {'message': 'Service Unavailable'}}", False),
        ("400: maximum context length is 131072 tokens", False),
        ("429 Too Many Requests", False),
        ("", False),
    ],
)
def test_qwen_transport_words_are_the_ones_node_fetch_writes(text: str, transport: bool) -> None:
    assert (_qwen().transport_failure(text) is not None) is transport


@pytest.mark.parametrize(
    ("text", "transport"),
    [
        ("openai.APIConnectionError: Connection error.", True),
        ("openai.APITimeoutError: Request timed out.", True),
        ("httpx.RemoteProtocolError: peer closed connection without sending complete", True),
        ("httpx.RemoteProtocolError: Server disconnected without sending a response.", True),
        ("openai.InternalServerError: Error code: 503 - {'error': {...}}", False),
        ("openai.InternalServerError: Error code: 500", False),
        ("openai.BadRequestError: Error code: 400 - context length", False),
        ("", False),
    ],
)
def test_hermes_transport_words_are_the_ones_its_client_writes(text: str, transport: bool) -> None:
    assert (_hermes().transport_failure(text) is not None) is transport


def test_qwen_reads_only_an_error_result_or_an_exit_without_one() -> None:
    """A tool that printed "fetch failed" inside a turn that ended is never a transport
    failure; the result's own error and the CLI's stderr are."""
    module = _qwen()
    log = module.RunLog()
    log.line(
        json.dumps(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "curl: fetch failed"}]},
            }
        )
    )
    log.line(json.dumps({"type": "result", "subtype": "success", "is_error": False}))
    assert log.transport_failure(0, "ECONNRESET in some tool output") is None
    assert log.transport_failure(1, "") is None
    log.line(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error": {"message": "TypeError: fetch failed"},
            }
        )
    )
    assert log.transport_failure(1, "") == "fetch failed"
    assert log.transport_failure(0, "") == "fetch failed"
    log.result = None
    assert log.transport_failure(1, "Error: read ECONNRESET") == "econnreset"
    assert log.transport_failure(0, "Error: read ECONNRESET") is None
    log.line(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error": "400 context window overflow",
            }
        )
    )
    assert log.transport_failure(1, "") is None


def test_hermes_reads_a_failed_usage_record_or_its_provider_exit(tmp_path: Path) -> None:
    module = _hermes()
    usage = tmp_path / USAGE_NAME
    usage.write_text(
        json.dumps({"completed": None, "failed": True, "failure": "Connection error."})
    )
    assert module.transport_failure_of(usage, 1) == "connection error"
    usage.write_text(json.dumps({"completed": None, "failed": True, "error": "Request timed out."}))
    assert module.transport_failure_of(usage, 1) == "request timed out"
    usage.write_text(json.dumps({"completed": None, "failed": True, "failure": "Error code: 503"}))
    assert module.transport_failure_of(usage, 1) is None
    # The gateway's answer, whatever the stderr says beside it, is not a transport failure.
    assert module.transport_failure_of(usage, 1, "Error code: 503 - Service Unavailable") is None
    # A run Hermes calls completed is never retried, whatever a command printed, and
    # neither is one that ended on its turn budget (`completed: false`, not failed).
    usage.write_text(json.dumps({"completed": True, "failed": False}))
    assert module.transport_failure_of(usage, 0, "curl: Connection error") is None
    usage.write_text(json.dumps({"completed": False, "failed": False, "api_calls": 301}))
    assert module.transport_failure_of(usage, 1, "curl: Connection error") is None
    # Hermes's own exit 75 with the client's message on stderr and no readable record.
    usage.unlink()
    assert module.transport_failure_of(usage, 75, "openai.APIConnectionError: Connection error.")
    assert (
        module.transport_failure_of(usage, 1, "openai.APIConnectionError: Connection error.")
        is None
    )


def _fake_qwen(tmp_path: Path, drops: int) -> tuple[Path, Path]:
    """A fake `qwen` whose first `drops` launches end on a dropped connection, the result
    event's error and a stderr line as Node's fetch writes them, and whose next launch
    ends its turn."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@example")
    _git(repo, "config", "user.name", "worker")
    (repo / "README").write_text("base\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "base")
    counter = tmp_path / "launches"
    dropped = json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "error": {"message": "TypeError: fetch failed"},
        }
    )
    answered = json.dumps({"type": "result", "subtype": "success", "is_error": False})
    binary = tmp_path / "qwen"
    binary.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -e",
                f"N=$(cat {counter} 2>/dev/null || echo 0)",
                f"echo $((N + 1)) > {counter}",
                'printf \'%s\\n\' \'{"type": "system", "subtype": "system_start"}\'',
                f'if [ "$N" -lt {drops} ]; then',
                f"  printf '%s\\n' '{dropped}'",
                "  echo 'Error: read ECONNRESET' >&2",
                "  exit 1",
                "fi",
                f"printf '%s\\n' '{answered}'",
                "exit 0",
            ]
        )
        + "\n"
    )
    binary.chmod(0o755)
    return repo, binary


def _run_qwen_wrapper(
    tmp_path: Path, repo: Path, binary: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, list[float], ModuleType]:
    identity = tmp_path / "IDENTITY.md"
    identity.write_text("# Task EX-0001\n\nWrite report.yaml.\n")
    report_dir = tmp_path / "report"
    report_dir.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    for name, value in {
        "HOME": str(home),
        "CRUCIBLE_QWEN_BINARY": str(binary),
        "CRUCIBLE_QWEN_IDENTITY": str(identity),
        "CRUCIBLE_QWEN_CONTEXT_LENGTH": "65536",
        "CRUCIBLE_QWEN_REPORT_DIR": str(report_dir),
    }.items():
        monkeypatch.setenv(name, value)
    module = _qwen()
    slept: list[float] = []
    monkeypatch.setattr(module.time, "sleep", slept.append)
    monkeypatch.setattr(module.sys, "argv", ["wrapper", "--output-format", "stream-json", "go"])
    monkeypatch.chdir(repo)
    return module.main(), slept, module


def test_the_qwen_wrapper_relaunches_the_cli_and_succeeds_when_the_gateway_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    repo, binary = _fake_qwen(tmp_path, drops=2)
    code, slept, module = _run_qwen_wrapper(tmp_path, repo, binary, monkeypatch)
    out, err = capfd.readouterr()
    assert code == 0
    assert (tmp_path / "launches").read_text().strip() == "3"
    assert slept == list(module.BACKOFF_SECONDS[:2])
    # Every launch's stream went through to the transcript, and the last result event,
    # the one the adapter reads, is the run that ended its turn.
    results = [json.loads(line) for line in out.splitlines() if '"result"' in line]
    assert [r["subtype"] for r in results] == [
        "error_during_execution",
        "error_during_execution",
        "success",
    ]
    assert err.count("Error: read ECONNRESET") == 2
    assert "transport-level API error (fetch failed); retry 1 of 3 in 5s" in err
    assert "retry 2 of 3 in 15s" in err
    document = json.loads((tmp_path / "report" / "report.yaml").read_text())
    assert "Qwen Code ended its turn (exit 0) after 2 transport retries" in document["summary"]


def test_the_qwen_wrapper_gives_up_after_three_retries_with_the_last_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    repo, binary = _fake_qwen(tmp_path, drops=10)
    code, slept, module = _run_qwen_wrapper(tmp_path, repo, binary, monkeypatch)
    err = capfd.readouterr().err
    assert code == 1
    assert (tmp_path / "launches").read_text().strip() == "4"
    assert slept == list(module.BACKOFF_SECONDS)
    assert "after 3 retries; giving up with exit 1" in err
    document = json.loads((tmp_path / "report" / "report.yaml").read_text())
    assert "after 3 transport retries: TypeError: fetch failed" in document["summary"]


HERMES_STAND_IN = r"""
import json, os, sys
counter = os.environ["FAKE_HERMES_COUNTER"]
drops = int(os.environ["FAKE_HERMES_DROPS"])
usage = os.environ["CRUCIBLE_HERMES_USAGE"]
try:
    n = int(open(counter).read())
except OSError:
    n = 0
open(counter, "w").write(str(n + 1))
assert sys.argv[1:] == ["-z", "do the task"], sys.argv
if n < drops:
    json.dump({"completed": None, "failed": True, "failure": "Connection error."}, open(usage, "w"))
    print("openai.APIConnectionError: Connection error.", file=sys.stderr)
    sys.exit(75)
json.dump({"completed": True, "failed": False, "api_calls": 3}, open(usage, "w"))
sys.exit(0)
"""


def _run_hermes_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drops: int
) -> tuple[int, list[float], ModuleType, Path]:
    module = _hermes()
    home = tmp_path / "home"
    home.mkdir()
    usage = tmp_path / USAGE_NAME
    for name, value in {
        "CRUCIBLE_HERMES_USAGE": str(usage),
        "HERMES_HOME": str(home),
        "FAKE_HERMES_COUNTER": str(tmp_path / "launches"),
        "FAKE_HERMES_DROPS": str(drops),
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "CRUCIBLE_HERMES_MAX_TURNS",
        "CRUCIBLE_HERMES_CONTEXT_LENGTH",
        "CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS",
        "CRUCIBLE_HERMES_IDENTITY",
    ):
        monkeypatch.delenv(name, raising=False)
    # The venv's Python and the patched Hermes are stood in for: the preflight passes
    # and the bootstrap is the fake above, so the launch loop is what runs for real.
    monkeypatch.setattr(module, "HERMES_PYTHON", sys.executable)
    monkeypatch.setattr(module, "PREFLIGHT", "pass")
    monkeypatch.setattr(module, "BOOTSTRAP", HERMES_STAND_IN)
    slept: list[float] = []
    monkeypatch.setattr(module.time, "sleep", slept.append)
    monkeypatch.setattr(module.sys, "argv", ["wrapper", "-z", "do the task"])
    return module.main(), slept, module, usage


def test_the_hermes_wrapper_relaunches_hermes_and_succeeds_when_the_gateway_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    code, slept, module, usage = _run_hermes_wrapper(tmp_path, monkeypatch, drops=2)
    err = capfd.readouterr().err
    assert code == 0
    assert (tmp_path / "launches").read_text() == "3"
    assert slept == list(module.BACKOFF_SECONDS[:2])
    assert err.count("openai.APIConnectionError: Connection error.") == 2
    assert "crucible-hermes: transport-level API error (connection error); retry 1 of 3" in err
    record = json.loads(usage.read_text())
    assert record["completed"] is True and record["transport_retries"] == 2


def test_the_hermes_wrapper_gives_up_after_three_retries_with_hermes_own_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    code, slept, module, usage = _run_hermes_wrapper(tmp_path, monkeypatch, drops=10)
    err = capfd.readouterr().err
    assert code == 75
    assert (tmp_path / "launches").read_text() == "4"
    assert slept == list(module.BACKOFF_SECONDS)
    assert "after 3 retries; giving up with exit 75" in err
    record = json.loads(usage.read_text())
    # The failed record is Hermes's own, with the relaunch count beside it; the adapter
    # still reads `failed: true` and classifies the exit.
    assert record["failed"] is True and record["completed"] is False
    assert record["transport_retries"] == 3


def test_hermes_usage_aggregates_every_relaunched_session(tmp_path: Path) -> None:
    module = _hermes()
    home = tmp_path / "home"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as database:
        database.execute(
            """CREATE TABLE sessions (
                id TEXT PRIMARY KEY, parent_session_id TEXT, started_at REAL,
                ended_at REAL, tool_call_count INTEGER, model TEXT,
                billing_provider TEXT, estimated_cost_usd REAL,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                reasoning_tokens INTEGER
            )"""
        )
        database.executemany(
            "INSERT INTO sessions VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("dropped", 10.0, 12.0, 1, "fast", "local", 0.25, 10, 2, 3, 4, 5),
                ("answered", 20.0, 24.0, 2, "fast", "local", 0.75, 20, 6, 7, 8, 9),
            ],
        )
    usage_path = tmp_path / USAGE_NAME
    usage_path.write_text(
        json.dumps(
            {
                "session_id": "answered",
                "estimated_cost_usd": 0.75,
                "input_tokens": 20,
                "output_tokens": 6,
                "cache_read_tokens": 7,
                "cache_write_tokens": 8,
                "reasoning_tokens": 9,
                "total_tokens": 41,
                "tool_calls": 2,
                "duration_ms": 4000,
            }
        )
    )
    module._enrich_usage(usage_path, home, transport_retries=1)
    record = json.loads(usage_path.read_text())
    assert record["transport_retries"] == 1
    assert record["duration_ms"] == 14000
    assert record["tool_calls"] == 3
    assert record["estimated_cost_usd"] == pytest.approx(1.0)
    assert record["input_tokens"] == 30
    assert record["output_tokens"] == 8
    assert record["cache_read_tokens"] == 10
    assert record["cache_write_tokens"] == 12
    assert record["reasoning_tokens"] == 14
    assert record["total_tokens"] == 60


# ----- AC2: a provider_error exit reroutes to the next eligible candidate --------------


def _local_entry(model: str, harness: str) -> dict[str, Any]:
    return {
        **_model(model, harness=harness, pool=POOL),
        "endpoint": "local",
        "endpoint_url": GATEWAY,
    }


def _qwen_on_the_gateway(
    monkeypatch: pytest.MonkeyPatch, models: list[dict[str, Any]] | None = None
) -> tuple[Any, Any, Any, list[Any], dict[str, Any]]:
    """A running Qwen Code attempt on the lab gateway, with Hermes in the same pool on
    the same gateway and a Codex fallback in another pool."""
    supervisor, pending, uow, attempts = _running(monkeypatch, harness="qwen_code")
    routing = _routing(
        models
        if models is not None
        else [
            _local_entry(QWEN, "qwen_code"),
            _local_entry(HERMES, "hermes"),
            _model(CODEX_FALLBACK, harness="codex", pool="openai-sub"),
        ]
    ).model_dump(mode="json")
    uow.routing_policies.get.return_value = MagicMock(document=routing)
    # A local candidate needs a task-specific check in the contract (routing).
    pending.contract.setdefault("required_verification", []).append(
        {"id": "V490", "kind": "command", "command": "uv run pytest -q tests/unit/t.py"}
    )
    pending.execution.model = QWEN
    pending.attempt.selected_model = QWEN
    pending.attempt.selected_harness = "qwen_code"
    pending.attempt.selected_pool = POOL
    marks: dict[str, Any] = {}

    def put(mark: Any) -> Any:
        marks[mark.pool] = mark
        return mark

    uow.pool_exhaustions.put.side_effect = put
    uow.pool_exhaustions.get.side_effect = marks.get
    uow.pool_exhaustions.list_all.side_effect = lambda: list(marks.values())
    # No earlier attempt on the pool: one blip marks nothing (ADR 0028).
    uow.attempt_metrics.list_since.return_value = []
    return supervisor, pending, uow, attempts, marks


def _dropped_past_the_retries() -> str:
    """Qwen's stream after the wrapper gave up: the last result event names the dropped
    connection, which the adapter classifies `provider_error`."""
    return (
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error": {"message": "Connection error: fetch failed"},
            }
        )
        + "\n"
    )


def _selection(supervisor: Any, uow: Any, pending: Any, attempt: Any) -> Any:
    supervisor._harnesses = None
    return supervisor._selection_for(
        uow, _Pending(attempt, pending.execution, pending.task, pending.contract)
    )


def _candidate(selection: Any, model: str) -> dict[str, Any]:
    candidates = selection if isinstance(selection, list) else selection.candidates
    return next(c for c in candidates if c["model"] == model)


def test_one_local_provider_error_blip_relaunches_without_marking_the_pool_or_waking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, attempts, marks = _qwen_on_the_gateway(monkeypatch)
    _finish(supervisor, pending.attempt, _dropped_past_the_retries(), exit_code=1)
    assert pending.attempt.exit_class is ExitClass.PROVIDER_ERROR
    assert pending.attempt.state is AttemptState.FAILED
    # The task is scheduled again on a new attempt of the same execution, by Crucible.
    assert pending.task.state is TaskState.SCHEDULED
    assert pending.execution.state is not ExecutionState.FAILED
    assert len(attempts) == 2
    nxt = attempts[-1]
    assert nxt.number == 2
    rerouted = next(e for e in _events(uow) if e.kind == EventKind.TASK_REROUTED.value)
    assert rerouted.payload["from_attempt_id"] == pending.attempt.id
    assert rerouted.payload["to_attempt_id"] == nxt.id
    assert rerouted.payload["next_attempt_id"] == nxt.id
    assert rerouted.payload["excluded_model"] == QWEN
    assert rerouted.payload["excluded_harness"] == "qwen_code"
    assert rerouted.payload["why"] == (
        "previous attempt ended provider_error on its route; rerouted to the next "
        "eligible candidate"
    )
    assert "wip_commit_sha" not in rerouted.payload
    assert not any(e.kind == EventKind.QUOTA_WIP_COMMITTED.value for e in _events(uow))
    # One blip marks no pool and raises no wake: the reroute is Crucible's own decision.
    assert marks == {}
    assert _wakes(uow) == 0
    # The next attempt routes as its launch will: away from the route that failed,
    # inside the same pool, which stays open.
    selection = _selection(supervisor, uow, pending, nxt)
    assert selection.selected is not None and selection.selected.id == HERMES
    assert selection.selected.pool == POOL
    failed = _candidate(selection, QWEN)
    assert not failed["eligible"]
    # The `excluded_routes` path's own reason, the one a capacity refusal's retry and a
    # model-only refusal's reroute carry (routing).
    assert failed["excluded"] == ["model refused capacity for this retry"]
    assert _candidate(selection, HERMES)["excluded"] == []


def test_a_hermes_5xx_provider_error_reroutes_the_same_way(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hermes's client answered with a 500 (no transport failure, no gateway status, so
    `provider_error` rather than `infrastructure`): the task moves to the next candidate."""
    supervisor, pending, uow, attempts, _marks = _qwen_on_the_gateway(monkeypatch)
    pending.execution.harness = "hermes"
    pending.execution.model = HERMES
    pending.attempt.selected_model = HERMES
    pending.attempt.selected_harness = "hermes"
    pending.attempt.workspace_path = str(tmp_path)
    report = tmp_path / "output" / "report"
    report.mkdir(parents=True)
    (report / USAGE_NAME).write_text(json.dumps({"completed": None, "failed": True}))
    supervisor._finish_exited(
        pending.attempt.id,
        1,
        CollectedOutputs(
            report=None,
            report_raw=None,
            blocked_md=None,
            stderr_tail="openai.InternalServerError: Error code: 500 - internal server error\n",
        ),
    )
    assert pending.attempt.exit_class is ExitClass.PROVIDER_ERROR
    assert pending.task.state is TaskState.SCHEDULED
    assert len(attempts) == 2
    rerouted = next(e for e in _events(uow) if e.kind == EventKind.TASK_REROUTED.value)
    assert rerouted.payload["excluded_model"] == HERMES
    assert rerouted.payload["excluded_harness"] == "hermes"
    selection = _selection(supervisor, uow, pending, attempts[-1])
    assert selection.selected is not None and selection.selected.id == QWEN


def test_the_reroute_budget_is_the_rule_the_model_only_refusal_takes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past `reroute_max` reroutes on the contract version, a provider error ends the
    execution and reports the task with the class visible and one wake saying why."""
    supervisor, pending, uow, attempts, _marks = _qwen_on_the_gateway(monkeypatch)
    routing = RoutingPolicyV1.model_validate(uow.routing_policies.get.return_value.document)
    for number in range(routing.reroute.reroute_max):
        uow.events.append(
            Event(
                seq=number,
                ts=NOW,
                kind=EventKind.TASK_REROUTED.value,
                principal="crucible",
                verified=True,
                payload={"contract_version": pending.execution.contract_version},
                task_id=pending.task.id,
                execution_id=pending.execution.id,
                attempt_id=pending.attempt.id,
            )
        )
    _finish(supervisor, pending.attempt, _dropped_past_the_retries(), exit_code=1)
    assert pending.attempt.exit_class is ExitClass.PROVIDER_ERROR
    assert pending.task.state is TaskState.REPORTED
    assert pending.execution.state is ExecutionState.FAILED
    assert len(attempts) == 1
    failed = next(e for e in _events(uow) if e.kind == EventKind.EXECUTION_FAILED.value)
    assert failed.payload["exit_class"] == "provider_error"
    assert failed.payload["reroute_cap"] == routing.reroute.reroute_max
    assert _wakes(uow) == 1
    wake = next(e for e in _events(uow) if e.kind == EventKind.WAKE_CREATED.value)
    assert wake.payload["summary"] == (
        f"attempt 1 ended provider_error with no reroute remaining "
        f"(reroute_max {routing.reroute.reroute_max})"
    )


def test_with_no_other_candidate_the_task_is_reported_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, attempts, _marks = _qwen_on_the_gateway(
        monkeypatch, models=[_local_entry(QWEN, "qwen_code")]
    )
    _finish(supervisor, pending.attempt, _dropped_past_the_retries(), exit_code=1)
    assert pending.attempt.exit_class is ExitClass.PROVIDER_ERROR
    assert pending.task.state is TaskState.REPORTED
    assert pending.execution.state is ExecutionState.FAILED
    assert len(attempts) == 1
    failed = next(e for e in _events(uow) if e.kind == EventKind.EXECUTION_FAILED.value)
    assert failed.payload["exit_class"] == "provider_error"
    assert failed.payload["no_candidate"] is True
    assert not _candidate(failed.payload["ordered_candidates"], QWEN)["eligible"]
    assert _wakes(uow) == 1
    wake = next(e for e in _events(uow) if e.kind == EventKind.WAKE_CREATED.value)
    assert "no other candidate in the tier was eligible to reroute to" in wake.payload["summary"]


def test_two_in_a_row_local_provider_errors_mark_the_pool_and_reroute_past_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0028 is untouched: the second provider error in a row on the pool marks it
    (one wake, the pool's own), and the reroute then leaves the pool for the fallback."""
    supervisor, pending, uow, attempts, marks = _qwen_on_the_gateway(monkeypatch)
    uow.attempt_metrics.list_since.return_value = [
        AttemptMetrics(
            attempt_id="earlier",
            task_id="other",
            model=HERMES,
            harness="hermes",
            endpoint_kind="local",
            pool=POOL,
            exit_class=ExitClass.PROVIDER_ERROR.value,
            created_at=NOW - timedelta(minutes=5),
        )
    ]
    _finish(supervisor, pending.attempt, _dropped_past_the_retries(), exit_code=1)
    assert pending.attempt.exit_class is ExitClass.PROVIDER_ERROR
    assert marks[POOL].reason == "local endpoint failed (provider_error)"
    assert _wakes(uow) == 1
    assert pending.task.state is TaskState.SCHEDULED
    assert len(attempts) == 2
    selection = _selection(supervisor, uow, pending, attempts[-1])
    assert selection.selected is not None and selection.selected.id == CODEX_FALLBACK
    assert any("pool exhausted" in reason for reason in _candidate(selection, HERMES)["excluded"])


def test_provider_error_reroutes_keep_every_failed_route_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscription failure after a local failure must not send a later reroute back
    to the failed local route. Every provider-error route in this reroute chain remains
    excluded, while unrelated candidates remain eligible."""
    agy_fallback = "gemini-fallback"
    supervisor, pending, uow, attempts, marks = _qwen_on_the_gateway(
        monkeypatch,
        models=[
            _local_entry(QWEN, "qwen_code"),
            _model(CODEX_FALLBACK, harness="codex", pool="openai-sub"),
            _model(agy_fallback, harness="agy", pool="google-sub"),
        ],
    )
    _finish(supervisor, pending.attempt, _dropped_past_the_retries(), exit_code=1)
    subscription = attempts[-1]
    subscription.selected_model = CODEX_FALLBACK
    subscription.selected_harness = "codex"
    subscription.selected_pool = "openai-sub"
    subscription.state = AttemptState.RUNNING
    subscription.started_at = NOW
    pending.task.state = TaskState.RUNNING
    uow.attempts.get.side_effect = lambda attempt_id, **_kwargs: next(
        attempt for attempt in attempts if attempt.id == attempt_id
    )

    _finish(supervisor, subscription, _dropped_past_the_retries(), exit_code=1)
    third = attempts[-1]
    selection = _selection(supervisor, uow, pending, third)

    assert marks == {}
    assert selection.selected is not None and selection.selected.id == agy_fallback
    assert not _candidate(selection, QWEN)["eligible"]
    assert not _candidate(selection, CODEX_FALLBACK)["eligible"]
    assert _candidate(selection, agy_fallback)["excluded"] == []


def test_a_rerouted_subscription_provider_error_does_not_mark_its_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint health belongs to the route that actually ran, not the execution's
    original local route. A subscription provider error therefore marks no pool."""
    supervisor, pending, uow, attempts, marks = _qwen_on_the_gateway(
        monkeypatch,
        models=[
            _local_entry(QWEN, "qwen_code"),
            _model(CODEX_FALLBACK, harness="codex", pool="openai-sub"),
        ],
    )
    _finish(supervisor, pending.attempt, _dropped_past_the_retries(), exit_code=1)
    rerouted = attempts[-1]
    rerouted.selected_model = CODEX_FALLBACK
    rerouted.selected_harness = "codex"
    rerouted.selected_pool = "openai-sub"
    uow.attempt_metrics.list_since.return_value = [
        AttemptMetrics(
            attempt_id=pending.attempt.id,
            task_id=pending.task.id,
            model=QWEN,
            harness="qwen_code",
            endpoint_kind="local",
            pool=POOL,
            exit_class=ExitClass.PROVIDER_ERROR.value,
            created_at=NOW - timedelta(minutes=1),
        )
    ]
    supervisor._mark_local_endpoint_down(uow, rerouted, pending.execution)
    assert marks == {}


def test_a_provider_error_consumes_no_retry_and_its_bundle_is_the_resume_source() -> None:
    """The rerouted attempt resumes from this attempt's sealed bundle (the launch's
    `interruption_retry` rule and the retention rule both read this set), and the
    attempt is not an ordinary one against `max_attempts`."""
    assert ExitClass.PROVIDER_ERROR in REROUTED_EXIT_CLASSES
    assert {ExitClass.INFRASTRUCTURE, ExitClass.QUOTA_EXHAUSTED} <= REROUTED_EXIT_CLASSES
    assert ExitClass.PROVIDER_ERROR.value not in Supervisor._FAILURE_WAKE_REASONS


# ----- AC3: a failed attempt with no commits still leaves a bundle ---------------------


def _work_repo(tmp_path: Path, *, root_base: bool = False) -> tuple[Path, Path, Path]:
    """A checkout on the work branch at the base with no commit of its own, as a worker
    that lost its gateway before its first edit leaves it; the collector's output
    directory, with the preparer's base record; and the worker's report directory
    holding the transcript the launch wrapper wrote."""
    repo = tmp_path / "repo"
    repo.mkdir()
    identity = ["-c", "user.name=t", "-c", "user.email=t@example"]
    _git(repo, "init", "-q", "-b", "main")
    if not root_base:
        (repo / "first.txt").write_text("first\n")
        _git(repo, "add", "first.txt")
        _git(repo, *identity, "commit", "-q", "-m", "first")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, *identity, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "crucible/test")
    output = tmp_path / "work" / "output"
    output.mkdir(parents=True)
    (output / "prepared-base.txt").write_text(base + "\n")
    (output / "prepared-head.txt").write_text(base + "\n")
    report = tmp_path / "work" / "report"
    report.mkdir()
    (report / "transcript.jsonl").write_text(_dropped_past_the_retries(), encoding="utf-8")
    return repo, output, report


def _collect(repo: Path, output: Path, report: Path) -> subprocess.CompletedProcess[str]:
    generated = scripts.collector_script(
        base_ref="main",
        work_branch="crucible/test",
        size_cap_bytes=1024 * 1024,
        attempt_id="01ATTEMPT",
        author_name="Policy Author",
        author_email="policy@example.invalid",
        trailer_value="EX-0001",
    )
    # The report mount is replaced first: its path begins with the repo mount's.
    generated = generated.replace(REPORT_MOUNT, str(report))
    generated = generated.replace(REPO_MOUNT, str(repo))
    generated = generated.replace(OUTPUT_MOUNT, str(output))
    return subprocess.run(
        ["sh", "-c", generated],
        capture_output=True,
        text=True,
        check=False,
        env=collector_env(output.parent),
    )


def _verify_bundle(output: Path) -> subprocess.CompletedProcess[str]:
    """The bundle verifier as the provider runs it, from the collected tree."""
    generated = scripts.BUNDLE_VERIFY_SCRIPT.replace(OUTPUT_MOUNT, str(output))
    return subprocess.run(
        ["sh", "-c", generated],
        capture_output=True,
        text=True,
        check=False,
        env=collector_env(output.parent),
    )


def _spec() -> Any:
    return MagicMock(contract={"repository": {"base_ref": "main", "work_branch": "crucible/test"}})


def _resume_from(bundle: Path, base_repo: Path, tmp_path: Path) -> Path:
    """What a correction's preparer does with the previous attempt's bundle: verify it
    against a fresh clone of the base and fetch the work branch from it."""
    clone = tmp_path / "resume"
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(base_repo), str(clone)], check=True)
    _git(clone, "bundle", "verify", str(bundle))
    _git(clone, "fetch", "-q", str(bundle), "crucible/test:refs/crucible/resume")
    _git(clone, "checkout", "-q", "-B", "crucible/test", "refs/crucible/resume", "--")
    return clone


def test_a_failed_attempt_with_no_commit_still_leaves_a_verified_bundle_and_its_log(
    tmp_path: Path,
) -> None:
    repo, output, report = _work_repo(tmp_path)
    base = _git(repo, "rev-parse", "main")
    done = _collect(repo, output, report)
    assert done.returncode == 0, done.stderr
    bundle = output / "work_branch.bundle"
    assert bundle.is_file() and bundle.stat().st_size > 0
    assert (output / "commits.txt").read_text().strip() == "0"
    verified = _verify_bundle(output)
    assert verified.returncode == 0, verified.stderr
    assert "no bundle was produced" not in verified.stderr
    outputs = read_outputs(
        output,
        tmp_path / "verify",
        spec=_spec(),
        bundle_verified=verified.returncode == 0,
        collector_exit=done.returncode,
        verifications=(),
        tail_bytes=4096,
    )
    assert outputs.bundle is not None
    assert outputs.bundle.verified and outputs.bundle.commits == 0
    assert outputs.bundle.head_sha == base and outputs.bundle.sha256
    # The worker's log is collected beside it, and the tree is there to read.
    assert [a.name for a in outputs.artifacts if a.name.startswith("report/")] == [
        "report/transcript.jsonl"
    ]
    assert "fetch failed" in next(
        a.content.decode() for a in outputs.artifacts if a.name == "report/transcript.jsonl"
    )
    assert (output / "tree" / "tracked.txt").read_text() == "base\n"
    # A correction resumes from it: the branch comes back at the state the worker left.
    clone = _resume_from(bundle, repo, tmp_path)
    assert _git(clone, "rev-parse", "HEAD") == base
    assert (clone / "tracked.txt").read_text() == "base\n"


def test_the_bundle_holds_what_the_worker_left_uncommitted(tmp_path: Path) -> None:
    """The workspace state as the attempt ended: an edit the model made before the
    gateway dropped is committed by the collector and carried by the bundle."""
    repo, output, report = _work_repo(tmp_path)
    base = _git(repo, "rev-parse", "main")
    (repo / "tracked.txt").write_text("edited before the drop\n")
    done = _collect(repo, output, report)
    assert done.returncode == 0, done.stderr
    assert (output / "commits.txt").read_text().strip() == "1"
    assert _verify_bundle(output).returncode == 0
    clone = _resume_from(output / "work_branch.bundle", repo, tmp_path)
    assert _git(clone, "rev-parse", "HEAD") != base
    assert _git(clone, "rev-parse", "HEAD~1") == base
    assert (clone / "tracked.txt").read_text() == "edited before the drop\n"
    assert "left uncommitted" in _git(clone, "log", "-1", "--format=%s")


def test_a_base_that_is_a_root_commit_is_bundled_whole(tmp_path: Path) -> None:
    repo, output, report = _work_repo(tmp_path, root_base=True)
    base = _git(repo, "rev-parse", "main")
    done = _collect(repo, output, report)
    assert done.returncode == 0, done.stderr
    assert (output / "work_branch.bundle").stat().st_size > 0
    assert _verify_bundle(output).returncode == 0
    listed = _git(output / "tree", "bundle", "list-heads", str(output / "work_branch.bundle"))
    assert listed == f"{base} refs/heads/crucible/test"
    clone = _resume_from(output / "work_branch.bundle", repo, tmp_path)
    assert _git(clone, "rev-parse", "HEAD") == base


def test_the_verifier_still_names_a_bundle_the_collector_did_not_write(tmp_path: Path) -> None:
    output = tmp_path / "output"
    (output / "tree").mkdir(parents=True)
    verified = _verify_bundle(output)
    assert verified.returncode == 2
    assert "no bundle was produced" in verified.stderr


def test_the_collector_script_is_shell_that_parses() -> None:
    script = scripts.collector_script(
        base_ref="main", work_branch="crucible/test", size_cap_bytes=1
    )
    assert "NEW_COMMITS=" in script and '"$WORK_BRANCH~1..$WORK_BRANCH"' in script
    done = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
