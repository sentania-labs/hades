"""hades #425: an allowlisted host is reachable from a worker, and the launch wrapper's
egress probe records what the worker could reach on the attempt.

The lab's attempt 01M44NW18QGA1VE45WNJ1GEVHB ran under a policy that allowlists
github.com, and its curl to github.com timed out while raw.githubusercontent.com (never
allowlisted, but on the same Fastly addresses as the allowlisted
objects.githubusercontent.com) answered. Of the three causes the issue names, the first
applied: the Kubernetes provider subtracted github.com and api.github.com from the
worker's rules on 26's old sentence that a worker never reaches GitHub, so the allowlist
was not enforced as written. hostAliases already pinned the resolved addresses (hades
#191), so the address spread was not it, and the Kubernetes path has no proxy variables
to miss: a worker reaches an allowlisted host directly through its NetworkPolicy.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.docker import LAUNCH_WRAPPER, DockerProvider
from crucible.adapters.ui.pages import tasks as tasks_mod
from crucible.application.errors import NotFoundError
from crucible.application.queries import _attempt_summary
from crucible.application.supervisor import Supervisor
from crucible.contracts.api import AttemptSummary, AttemptView
from crucible.domain.egress_probe import (
    PROBE_MARKER,
    find_probe,
    host_words,
    normalise_probe,
    parse_probe_line,
    unreachable_hosts,
)
from crucible.domain.entities import Attempt
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.ports.execution import CleanupPolicy, LogChunk
from tests.unit.kubernetes_fixtures import build, pod_of, spec
from tests.unit.test_credential_copy import StubClient, config
from tests.unit.test_kubernetes_network_policy import allows

GITHUB = "140.82.121.4"
PYPI = "151.101.0.223"


def _policy(*hosts: str) -> dict[str, Any]:
    return {
        "images": {"allowlist": ["crucible-worker:*"]},
        "network": {"mode": "egress-proxy", "egress_allowlist": list(hosts)},
        "resources": {"cpus": 2, "memory": "4GiB"},
        "limits": {"grace_seconds": 30},
    }


async def _launched(policy: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The worker's NetworkPolicy and Pod spec for one launch under `policy`."""
    api, _registry, provider = build()
    launch = spec(policy=policy)
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    worker_policy = next(
        row["body"]
        for row in api.created
        if row["kind"] == "networkpolicies"
        and row["body"]["spec"]["podSelector"]["matchLabels"][k8sspec.LABEL_ROLE]
        == k8sspec.ROLE_WORKER
    )
    pod = pod_of(api, "worker-")
    await provider.cleanup(workspace, CleanupPolicy.DELETE, launch)
    return worker_policy, pod


def _env(pod: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e["value"] for e in pod["containers"][0]["env"] if "value" in e}


# ----- the cause: the allowlist is now enforced as written ------------------------------


async def test_a_worker_under_a_policy_that_allowlists_github_gets_github() -> None:
    """The worker's rule permits the address the provider resolved github.com to, the
    Pod is pinned to that same address, and the allowlist the wrapper probes names it."""
    policy, pod = await _launched(_policy("github.com", "pypi.org"))
    assert allows(policy, GITHUB, 443)
    assert allows(policy, PYPI, 443)
    assert policy["metadata"]["annotations"][k8sspec.ANNOTATION_EGRESS] == "github.com,pypi.org"
    assert {"ip": GITHUB, "hostnames": ["github.com"]} in pod["hostAliases"]
    assert _env(pod)["CRUCIBLE_EGRESS_ALLOWLIST"] == "github.com,pypi.org"


async def test_a_worker_under_a_policy_that_does_not_allowlist_github_does_not_get_it() -> None:
    """The fix adds nothing of the provider's own: the policy document decides."""
    policy, pod = await _launched(_policy("pypi.org"))
    assert not allows(policy, GITHUB, 443)
    assert allows(policy, PYPI, 443)
    assert _env(pod)["CRUCIBLE_EGRESS_ALLOWLIST"] == "pypi.org"
    assert all("github.com" not in alias["hostnames"] for alias in pod["hostAliases"])


async def test_the_kubernetes_worker_is_wrapped_so_the_probe_runs_before_the_harness() -> None:
    _policy_body, pod = await _launched(_policy("pypi.org"))
    command = pod["containers"][0]["command"]
    assert command[:5] == ["bash", "-o", "pipefail", "-c", LAUNCH_WRAPPER]
    assert command[5] == "crucible-launch"
    assert command[6:] == ["crucible-script-harness"]


async def test_a_kubernetes_worker_with_no_network_stays_plain() -> None:
    api, _registry, provider = build()
    launch = spec(network="none")
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    pod = pod_of(api, "worker-")
    assert pod["containers"][0]["command"] == ["crucible-script-harness"]
    assert (
        "CRUCIBLE_EGRESS_ALLOWLIST" not in _env(pod) or not _env(pod)["CRUCIBLE_EGRESS_ALLOWLIST"]
    )
    await provider.cleanup(workspace, CleanupPolicy.DELETE, launch)


async def test_the_docker_worker_is_wrapped_only_when_a_host_is_allowlisted(
    tmp_path: Path,
) -> None:
    client = StubClient("script-harness", "1.0.0")
    provider = DockerProvider(config(tmp_path), client=client)  # type: ignore[arg-type]
    image = "crucible-worker:script-harness-1.0.0-abc"
    launch = spec(harness="script-harness", image=image, command=("crucible-script-harness",))
    command, env = provider._command(launch)
    assert command[:5] == ["bash", "-o", "pipefail", "-c", LAUNCH_WRAPPER]
    assert command[6:] == ["crucible-script-harness"] and env == {}
    plain = spec(
        harness="script-harness",
        image=image,
        command=("crucible-script-harness",),
        policy={"images": {"allowlist": ["crucible-worker:*"]}},
    )
    assert provider._command(plain) == (["crucible-script-harness"], {})
    # A local endpoint is a `host:port` entry, which the probe leaves alone, so an
    # allowlist of only that one is not a reason to wrap.
    assert provider._probe_hosts(launch) == ("github.com", "pypi.org")


# ----- the wrapper's probe, run for real with a scripted curl ----------------------------


_FAKE_CURL = """#!/bin/sh
# The last argument is the URL; the host decides the outcome, as the test scripted it.
for last; do :; done
host=${last#https://}; host=${host%%/*}
case "$host" in
  pypi.org) exit 0;;
  github.com)
    echo "curl: (28) Failed to connect to github.com port 443: Timeout was reached" >&2
    exit 28;;
  stub.example) echo 'curl: (35) error:0A00010B:SSL routines::wrong version number' >&2; exit 35;;
  *) echo "curl: (6) Could not resolve host: $host" >&2; exit 6;;
esac
"""


def _run_wrapper(
    tmp_path: Path, allowlist: str | None, *argv: str, curl: str | None = _FAKE_CURL
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if curl is not None:
        path = bin_dir / "curl"
        path.write_text(curl)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }
    if allowlist is not None:
        env["CRUCIBLE_EGRESS_ALLOWLIST"] = allowlist
    return subprocess.run(
        ["bash", "-o", "pipefail", "-c", LAUNCH_WRAPPER, "crucible-launch", *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _probe_lines(stderr: str) -> list[dict[str, Any]]:
    found = [parse_probe_line(line) for line in stderr.splitlines()]
    return [line for line in found if line is not None]


def test_the_wrapper_probes_every_allowlisted_host_then_runs_the_harness(tmp_path: Path) -> None:
    run = _run_wrapper(
        tmp_path,
        "pypi.org,github.com,stub.example,gateway.local:4000,nowhere.invalid",
        "sh",
        "-c",
        "echo harness-ran; exit 3",
    )
    # The harness ran after the probe, and its exit is the wrapper's.
    assert run.stdout == "harness-ran\n"
    assert run.returncode == 3
    (probe,) = _probe_lines(run.stderr)
    by_host = {row["host"]: row for row in probe["hosts"]}
    # Every name was tried; the `host:port` local endpoint was left alone.
    assert list(by_host) == ["pypi.org", "github.com", "stub.example", "nowhere.invalid"]
    assert by_host["pypi.org"]["reachable"] is True and by_host["pypi.org"]["curl_exit"] == 0
    assert by_host["github.com"]["reachable"] is False
    assert by_host["github.com"]["curl_exit"] == 28
    assert "Timeout was reached" in by_host["github.com"]["detail"]
    # A plain-HTTP stub on 443 answers the TLS handshake with garbage: the connection
    # was made, so the egress path let it through.
    assert by_host["stub.example"]["reachable"] is True
    assert by_host["stub.example"]["curl_exit"] == 35
    assert by_host["nowhere.invalid"]["reachable"] is False
    assert by_host["nowhere.invalid"]["curl_exit"] == 6
    assert all(isinstance(row["ms"], int) and row["ms"] >= 0 for row in probe["hosts"])


def test_the_wrapper_probes_nothing_without_an_allowlist(tmp_path: Path) -> None:
    run = _run_wrapper(tmp_path, None, "sh", "-c", "echo plain")
    assert run.returncode == 0 and run.stdout == "plain\n"
    assert PROBE_MARKER not in run.stderr
    run = _run_wrapper(tmp_path, "", "sh", "-c", "echo plain")
    assert PROBE_MARKER not in run.stderr


def test_a_missing_curl_is_reported_and_never_stops_the_harness(tmp_path: Path) -> None:
    """A PATH with everything the wrapper uses except curl: the probe says so per host
    rather than failing the launch, and the harness runs."""
    bin_dir = tmp_path / "nocurl"
    bin_dir.mkdir()
    for tool in ("sh", "mktemp", "tr", "head", "cat", "rm"):
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    bash = shutil.which("bash")
    assert bash
    run = subprocess.run(
        [bash, "-o", "pipefail", "-c", LAUNCH_WRAPPER, "crucible-launch", "sh", "-c", "echo ok"],
        env={
            "PATH": str(bin_dir),
            "CRUCIBLE_EGRESS_ALLOWLIST": "pypi.org",
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert run.returncode == 0 and run.stdout == "ok\n"
    (probe,) = _probe_lines(run.stderr)
    (row,) = probe["hosts"]
    assert row["host"] == "pypi.org" and row["reachable"] is False
    assert row["curl_exit"] == 127 and "curl" in row["detail"]


def test_the_wrapper_text_is_what_the_providers_hand_the_container() -> None:
    """Both providers pass the same wrapper; the probe sits before everything else in it."""
    assert LAUNCH_WRAPPER.index("egress_probe()") < LAUNCH_WRAPPER.index("CRUCIBLE_CODEX_CONFIG")
    assert LAUNCH_WRAPPER.index("\negress_probe\n") < LAUNCH_WRAPPER.index("CRUCIBLE_CODEX_CONFIG")


# ----- the line's shape -----------------------------------------------------------------


def test_parse_probe_line_takes_only_the_wrappers_line() -> None:
    line = (
        'crucible-egress-probe: {"hosts":[{"host":"pypi.org","reachable":true,'
        '"curl_exit":0,"ms":30,"detail":""}]}'
    )
    assert parse_probe_line(line) == {
        "hosts": [{"host": "pypi.org", "reachable": True, "curl_exit": 0, "ms": 30, "detail": ""}]
    }
    assert parse_probe_line("the harness said crucible-egress-probe: {} earlier") is None
    assert parse_probe_line("crucible-egress-probe: not json") is None
    assert parse_probe_line('crucible-egress-probe: {"nope": 1}') is None
    assert parse_probe_line('crucible-egress-probe: ["list"]') is None
    assert parse_probe_line("crucible-launch: commands running: 1") is None


def test_normalise_drops_half_written_rows_and_derives_reachable_from_the_exit() -> None:
    document = {
        "hosts": [
            {"host": "a.example", "curl_exit": 35, "ms": -1, "detail": "x" * 400},
            {"host": "", "reachable": True},
            "not a row",
            {"host": "b.example", "reachable": "yes", "curl_exit": "7"},
        ]
    }
    assert normalise_probe(document) == {
        "hosts": [
            {
                "host": "a.example",
                "reachable": True,
                "curl_exit": 35,
                "ms": None,
                "detail": "x" * 200,
            },
            {"host": "b.example", "reachable": False, "curl_exit": None, "ms": None, "detail": ""},
        ]
    }
    assert normalise_probe({"hosts": []}) == {"hosts": []}
    assert normalise_probe({}) is None


def test_find_probe_takes_the_first_probe_line_in_a_run_of_lines() -> None:
    text = (
        "crucible-launch: commands running: 0\n"
        'crucible-egress-probe: {"hosts":[{"host":"a","reachable":false,"curl_exit":28}]}\n'
        'crucible-egress-probe: {"hosts":[{"host":"b","reachable":true,"curl_exit":0}]}\n'
    )
    found = find_probe(text)
    assert found is not None and [row["host"] for row in found["hosts"]] == ["a"]
    assert unreachable_hosts(found) == ["a"]
    assert find_probe("nothing here\n") is None


def test_host_words_say_reachable_or_why_not() -> None:
    assert host_words({"host": "pypi.org", "reachable": True, "curl_exit": 0}) == (
        "pypi.org reachable"
    )
    assert (
        host_words(
            {"host": "stub.example", "reachable": True, "curl_exit": 35, "detail": "wrong version"}
        )
        == "stub.example reachable (connected; curl 35: wrong version)"
    )
    assert (
        host_words(
            {
                "host": "github.com",
                "reachable": False,
                "curl_exit": 28,
                "detail": "Timeout was reached",
            }
        )
        == "github.com unreachable (curl 28: Timeout was reached)"
    )
    assert host_words({"host": "x", "reachable": False, "curl_exit": 7}) == "x unreachable (curl 7)"
    assert host_words({"host": "x", "reachable": False}) == "x unreachable"


# ----- the supervisor records it on the attempt, once ---------------------------------


class _Logs:
    def __init__(self) -> None:
        self.appended: list[Any] = []

    def last_offset(self, attempt_id: str) -> int:
        return 0

    def append(self, record: Any) -> None:
        self.appended.append(record)


class _Attempts:
    def __init__(self, attempt: Attempt) -> None:
        self.attempt = attempt
        self.saved: list[dict[str, Any] | None] = []

    def get(self, attempt_id: str, *, for_update: bool = False) -> Attempt:
        return self.attempt

    def save(self, attempt: Attempt) -> None:
        self.saved.append(attempt.egress_probe)


class _Uow:
    def __init__(self, attempt: Attempt) -> None:
        self.attempts = _Attempts(attempt)
        self.logs = _Logs()
        self.heartbeats = SimpleNamespace(append=lambda *a, **k: None)
        self.committed = 0

    def set_fenced_token(self, token: int) -> None:
        return None

    def commit(self) -> None:
        self.committed += 1


def _attempt() -> Attempt:
    return Attempt(
        id="01ATTEMPT0000000000000000A",
        execution_id="execution",
        task_id="task",
        number=1,
        state=AttemptState.RUNNING,
        created_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
    )


def _supervisor(uow: _Uow) -> Supervisor:
    @contextmanager
    def factory() -> Any:
        yield uow

    supervisor = object.__new__(Supervisor)
    supervisor._uow_factory = factory  # type: ignore[assignment]
    supervisor.fenced_token = 1
    supervisor._clock = cast(
        Any, SimpleNamespace(now=lambda: datetime(2026, 10, 5, 12, 1, tzinfo=UTC))
    )
    return supervisor


def test_the_supervisor_keeps_the_first_probe_line_on_the_attempt() -> None:
    attempt = _attempt()
    uow = _Uow(attempt)
    supervisor = _supervisor(uow)
    first = (
        'crucible-egress-probe: {"hosts":[{"host":"github.com","reachable":false,'
        '"curl_exit":28,"ms":5007,"detail":"Timeout was reached"},'
        '{"host":"pypi.org","reachable":true,"curl_exit":0,"ms":30,"detail":""}]}\n'
    )
    stored = supervisor._store_logs(
        attempt.id,
        (
            LogChunk("stdout", b"starting\n", line_sha256="a"),
            LogChunk("stderr", first.encode(), line_sha256="b"),
        ),
    )
    assert stored == 2
    assert attempt.egress_probe == {
        "hosts": [
            {
                "host": "github.com",
                "reachable": False,
                "curl_exit": 28,
                "ms": 5007,
                "detail": "Timeout was reached",
            },
            {"host": "pypi.org", "reachable": True, "curl_exit": 0, "ms": 30, "detail": ""},
        ],
        "recorded_at": "2026-10-05T12:01:00+00:00",
    }
    assert uow.attempts.saved[-1] == attempt.egress_probe
    # The harness echoing a probe line later never replaces the wrapper's.
    later = 'crucible-egress-probe: {"hosts":[{"host":"evil","reachable":true,"curl_exit":0}]}\n'
    supervisor._store_logs(attempt.id, (LogChunk("stdout", later.encode(), line_sha256="c"),))
    assert [row["host"] for row in attempt.egress_probe["hosts"]] == ["github.com", "pypi.org"]


def test_a_log_without_the_line_records_nothing() -> None:
    attempt = _attempt()
    supervisor = _supervisor(_Uow(attempt))
    supervisor._store_logs(attempt.id, (LogChunk("stdout", b"just work\n", line_sha256="a"),))
    assert attempt.egress_probe is None


# ----- the record reaches the API and the task page -------------------------------------


_PROBE = {
    "hosts": [
        {
            "host": "github.com",
            "reachable": False,
            "curl_exit": 28,
            "ms": 5007,
            "detail": "Timeout",
        },
        {"host": "pypi.org", "reachable": True, "curl_exit": 0, "ms": 30, "detail": ""},
    ],
    "recorded_at": "2026-10-05T12:01:00+00:00",
}


def test_the_attempt_summary_carries_the_probe() -> None:
    attempt = _attempt()
    attempt.egress_probe = dict(_PROBE)
    summary = _attempt_summary(attempt)
    assert summary.egress_probe == _PROBE
    assert AttemptSummary.model_validate(summary.model_dump()).egress_probe == _PROBE
    assert AttemptView.model_fields["egress_probe"].default is None
    assert "egress_probe" in json.loads(summary.model_dump_json())


def test_the_task_page_shows_an_egress_section_per_attempt_and_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_at = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    probed = SimpleNamespace(
        id="A1",
        model=None,
        ordered_candidates=[],
        routing_version=None,
        effective_settings=None,
        egress_probe=_PROBE,
    )
    unprobed = SimpleNamespace(
        id="A0",
        model=None,
        ordered_candidates=[],
        routing_version=None,
        effective_settings=None,
        egress_probe=None,
    )
    view = SimpleNamespace(
        id="T1",
        external_id="FDY-0327",
        title="egress",
        state=TaskState.RUNNING,
        repository="acme/repo",
        principal="foundry",
        head_sha=None,
        created_at=created_at,
        updated_at=created_at,
        executions=[SimpleNamespace(attempts=[unprobed, probed])],
        decisions=[],
        delivery=SimpleNamespace(
            pull_request_number=None,
            pull_request_url=None,
            pull_request_state=None,
            work_branch=None,
            pushed_head=None,
            pushed_at=None,
            merge_sha=None,
            merged_by=None,
            merged_at=None,
        ),
    )
    record = SimpleNamespace(
        url=None,
        number=None,
        state=None,
        head_sha=None,
        completed_rounds=0,
        required_rounds=0,
        last_polled_at=None,
        gates=[],
        ci_certifications=[],
        ci_decisions=[],
    )
    principal = SimpleNamespace(name="reader", role=SimpleNamespace(value="observer"))
    uow = SimpleNamespace(events=SimpleNamespace(latest_for_task_kind=lambda *a, **k: None))
    ctx = SimpleNamespace(clock=SimpleNamespace(now=lambda: created_at))
    captured: list[dict[str, Any]] = []

    def fake_page(*args: Any, sections: list[dict[str, Any]], **kwargs: Any) -> str:
        captured.extend(sections)
        return "fake response"

    def no_report(*args: Any, **kwargs: Any) -> Any:
        raise NotFoundError("no report")

    monkeypatch.setattr(tasks_mod, "_require", lambda *a, **k: (principal, "fixture-csrf"))
    monkeypatch.setattr(tasks_mod, "task_view", lambda *a, **k: view)
    monkeypatch.setattr(tasks_mod, "pull_request_view", lambda *a, **k: record)
    monkeypatch.setattr(tasks_mod, "attempt_report", no_report)
    monkeypatch.setattr(tasks_mod, "_page", fake_page)
    tasks_mod.task_page(
        Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/ui/tasks/T1",
                "headers": [],
                "query_string": b"",
            }
        ),
        "T1",
        cast(Any, ctx),
        cast(Any, uow),
    )
    section = next(s for s in captured if s["title"] == "Egress")
    assert section["columns"] == ["Attempt", "Host", "Result"]
    assert section["rows"] == [
        ["A1", "github.com", "unreachable (curl 28: Timeout)"],
        ["A1", "pypi.org", "reachable"],
    ]
    assert "before the harness started" in section["note"]


def test_the_section_is_absent_until_a_probe_was_recorded() -> None:
    view = SimpleNamespace(
        executions=[SimpleNamespace(attempts=[SimpleNamespace(id="A0", egress_probe=None)])]
    )
    assert tasks_mod._egress_rows(view) == []
    empty = SimpleNamespace(
        executions=[
            SimpleNamespace(attempts=[SimpleNamespace(id="A1", egress_probe={"hosts": []})])
        ]
    )
    assert tasks_mod._egress_rows(empty) == [["A1", "none", "no allowlisted host to probe"]]


@pytest.mark.skipif(not os.environ.get("CRUCIBLE_EGRESS_ALLOWLIST"), reason="not inside a worker")
def test_inside_a_real_worker_the_probe_matches_the_allowlist_it_was_given(tmp_path: Path) -> None:
    """Only inside a Crucible worker: the wrapper's probe, with the image's real curl,
    names exactly the hosts the provider allowlisted for this attempt."""
    expected = [h for h in os.environ["CRUCIBLE_EGRESS_ALLOWLIST"].split(",") if ":" not in h]
    run = _run_wrapper(tmp_path, os.environ["CRUCIBLE_EGRESS_ALLOWLIST"], "true", curl=None)
    assert run.returncode == 0
    (probe,) = _probe_lines(run.stderr)
    assert [row["host"] for row in probe["hosts"]] == expected
