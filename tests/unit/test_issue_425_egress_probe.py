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
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.docker import LAUNCH_WRAPPER, DockerProvider
from crucible.adapters.execution.kubernetes import KubernetesConfig
from crucible.adapters.harness.script import ScriptHarnessAdapter
from crucible.adapters.ui.pages import tasks as tasks_mod
from crucible.application.errors import NotFoundError
from crucible.application.queries import _attempt_summary
from crucible.application.supervisor import Supervisor
from crucible.contracts.api import AttemptSummary, AttemptView
from crucible.domain.egress_probe import (
    MAX_PROBE_BYTES,
    MAX_PROBE_HOSTS,
    PROBE_MARKER,
    REJECTED_NOT_A_PROBE,
    REJECTED_NOT_JSON,
    REJECTED_TOO_DEEP,
    REJECTED_TOO_LONG,
    REJECTED_TOO_MANY_ROWS,
    check_shape,
    find_probe,
    find_probe_line,
    host_words,
    normalise_probe,
    parse_probe_line,
    probe_expected,
    read_probe_line,
    unreachable_hosts,
)
from crucible.domain.entities import Attempt, Execution, ExecutionRole, TaskContract
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.ports.execution import CleanupPolicy, LogChunk
from crucible.ports.harness import ExitInfo, LaunchContext
from tests.unit.kubernetes_fixtures import build, pod_of, spec
from tests.unit.test_credential_copy import StubClient, config
from tests.unit.test_kubernetes_network_policy import allows, policies

GITHUB = "140.82.121.4"
PYPI = "151.101.0.223"
# What the kind tier's isolation probe dials as its host no policy names (example.com),
# and a documentation address: neither is anything an allowlist here resolves to.
UNRELATED = ("23.192.228.80", "203.0.113.9")


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


def _config(*, broad_egress: bool) -> KubernetesConfig:
    return KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        broad_egress=broad_egress,
    )


@pytest.mark.parametrize("broad_egress", [False, True])
async def test_one_allowlisted_host_admits_that_host_and_denies_an_unrelated_one(
    broad_egress: bool,
) -> None:
    """The regression the kind tier's isolation probe caught: with github.com no longer
    dropped, the worker's list was not empty, and under `broad_egress` (the tier's
    setting) a non-empty list rendered "the public internet on 443", so the probe's
    curl to example.com was let through. A worker and a verifier get their resolved
    allowlist whatever `broad_egress` says, so the one host they name is all they reach."""
    rendered = await policies(
        config=_config(broad_egress=broad_egress), policy=_policy("github.com")
    )
    for role in (k8sspec.ROLE_WORKER, k8sspec.ROLE_VERIFIER):
        policy = rendered[role]
        assert allows(policy, GITHUB, 443), role
        for address in UNRELATED:
            assert not allows(policy, address, 443), (role, address)
        assert not allows(policy, PYPI, 443), role
        for rule in policy["spec"]["egress"]:
            for destination in rule.get("to") or []:
                cidr = (destination.get("ipBlock") or {}).get("cidr")
                assert cidr != "0.0.0.0/0", (role, rule)


async def test_broad_egress_still_reaches_the_git_roles() -> None:
    """The opt-out keeps its meaning where the provider, not the policy, fixes the
    destinations: the preparer takes the broad rule, the worker beside it does not."""
    rendered = await policies(config=_config(broad_egress=True), policy=_policy("github.com"))
    assert allows(rendered[k8sspec.ROLE_PREPARER], UNRELATED[1], 443)
    assert not allows(rendered[k8sspec.ROLE_WORKER], UNRELATED[1], 443)


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


@pytest.mark.parametrize("prompt", [False, True])
@pytest.mark.parametrize("transcript", [False, True])
@pytest.mark.parametrize("monitor", [False, True])
@pytest.mark.parametrize("cleanup_exit", [0, 23])
def test_wrapped_harness_receives_term_and_finishes_cleanup(
    tmp_path: Path, prompt: bool, transcript: bool, monitor: bool, cleanup_exit: int
) -> None:
    """Signal only PID 1's stand-in, as a Pod drain does, and wait for real cleanup."""
    curl = tmp_path / "curl"
    curl.write_text(_FAKE_CURL)
    curl.chmod(0o755)
    harness = tmp_path / "harness.py"
    harness.write_text(
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        "def terminate(signum, frame):\n"
        "    Path('terminated').touch()\n"
        "    while not Path('release').exists(): time.sleep(0.01)\n"
        "    print('cleanup finished', flush=True)\n"
        "    sys.exit(int(sys.argv[1]))\n"
        "signal.signal(signal.SIGTERM, terminate)\n"
        "print('stdin=' + sys.stdin.read().strip(), flush=True)\n"
        "Path('ready').touch()\n"
        "while True: time.sleep(0.01)\n"
    )
    env = {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "CRUCIBLE_EGRESS_ALLOWLIST": "pypi.org",
    }
    if prompt:
        env["CRUCIBLE_PROMPT"] = "the prompt"
    if transcript:
        env["CRUCIBLE_TRANSCRIPT"] = str(tmp_path / "transcript")
    if monitor:
        env["CRUCIBLE_IN_FLIGHT_FILE"] = str(tmp_path / "commands")
        (tmp_path / "commands").write_text('{"session_id": "active"}')
    process = subprocess.Popen(
        [
            "bash",
            "-o",
            "pipefail",
            "-c",
            LAUNCH_WRAPPER,
            "crucible-launch",
            sys.executable,
            str(harness),
            str(cleanup_exit),
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    def await_file(name: str) -> None:
        deadline = time.monotonic() + 5
        while not (tmp_path / name).exists():
            assert process.poll() is None, "wrapper exited before harness cleanup"
            assert time.monotonic() < deadline, f"harness never wrote {name}"
            time.sleep(0.01)

    try:
        await_file("ready")
        process.terminate()
        await_file("terminated")
        assert process.poll() is None, "wrapper must wait for cooperative cleanup"
        (tmp_path / "release").touch()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == cleanup_exit, stderr
        assert stdout == f"stdin={'the prompt' if prompt else ''}\ncleanup finished\n"
        assert _probe_lines(stderr)[0]["hosts"][0]["reachable"] is True
        if transcript:
            assert (tmp_path / "transcript").read_text() == stdout
    finally:
        # Also clean up the harness if a regression leaves it orphaned.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)


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
    for tool in ("sh", "mktemp", "tr", "head", "cat", "rm", "python3"):
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


def test_the_wrapper_marker_is_ascii_when_curl_writes_non_utf8(tmp_path: Path) -> None:
    curl = r"""#!/bin/sh
printf '\374 Gr\303\274nde' >&2
exit 7
"""
    run = _run_wrapper(tmp_path, "odd.example", "true", curl=curl)
    assert run.returncode == 0
    marker = next(line for line in run.stderr.splitlines() if line.startswith(PROBE_MARKER))
    marker.encode("ascii")
    (probe,) = _probe_lines(run.stderr)
    assert probe["hosts"][0]["detail"] == "? Gr\u00fcnde"


def test_the_wrapped_scripted_quota_harness_still_classifies_as_quota_exhausted(
    tmp_path: Path,
) -> None:
    """The round-two question (PR 431): the wrapped path and the exit class. The script
    adapter's quota harness, run under the wrapper exactly as the Docker provider runs
    it (probe first, then the harness as a direct child), exits 1 with its refusal on
    stderr after the probe line, and the adapter classifies that exit as before. What
    the wrapper changes is only when the exit happens: the probe's round trip comes
    first, so the tick that launched the worker observes it still running."""
    adapter = ScriptHarnessAdapter()
    launch = adapter.build_launch(
        LaunchContext(
            attempt_id="01ATTEMPT0000000000000000A",
            model="a-scripted-quota",
            effort=None,
            timeout_seconds=600,
            identity_mount="/crucible/identity",
            report_mount="/crucible/report",
            repo_mount=str(tmp_path / "repo"),
        )
    )
    (tmp_path / "repo").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(_FAKE_CURL)
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    run = subprocess.run(
        ["bash", "-o", "pipefail", "-c", LAUNCH_WRAPPER, "crucible-launch", *launch.argv],
        cwd=launch.workdir,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "TMPDIR": str(tmp_path),
            "CRUCIBLE_EGRESS_ALLOWLIST": "github.com",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert run.returncode == 1, run.stderr
    lines = run.stderr.splitlines()
    assert lines[0].startswith(PROBE_MARKER) and lines[1] == '{"error":"scripted_quota_exhausted"}'
    exit_info = ExitInfo(exit_code=run.returncode)
    assert adapter.classify_exit(exit_info, run.stdout, run.stderr) is ExitClass.QUOTA_EXHAUSTED
    assert adapter.provider_quota_exhausted(run.stdout, run.stderr) is True
    # The wrapper's own stderr line does not read as the harness's refusal.
    assert adapter.provider_quota_exhausted("", lines[0]) is False
    assert (tmp_path / "repo" / "src" / "quota-checkpoint.txt").read_text() == "quota checkpoint\n"


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


def _no_loads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`json.loads` replaced for the test: what it was handed, which must stay empty."""
    called: list[str] = []

    def fake(text: str, **kwargs: Any) -> Any:
        called.append(text)
        return {}

    monkeypatch.setattr(json, "loads", fake)
    return called


def _row(host: str, detail: str = "") -> str:
    return f'{{"host":"{host}","reachable":false,"curl_exit":28,"ms":5007,"detail":"{detail}"}}'


def test_a_line_over_the_byte_cap_is_rejected_before_it_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P2 on PR 431: one marker line from the worker must never cost the shared
    supervisor more than the caps. The length is checked first, and `json.loads` is
    never reached for a line over it."""
    rows = ",".join(_row(f"h{i}.example", "x" * 190) for i in range(300))
    payload = f'{{"hosts":[{rows}]}}'
    assert len(payload) > MAX_PROBE_BYTES
    loads_called = _no_loads(monkeypatch)
    assert read_probe_line(PROBE_MARKER + payload) == (None, REJECTED_TOO_LONG)
    assert loads_called == []
    # Right at the cap, the line is parsed.
    exact = PROBE_MARKER + " " * (MAX_PROBE_BYTES - len('{"hosts":[]}')) + '{"hosts":[]}'
    assert check_shape(exact[len(PROBE_MARKER) :]) is None


def test_a_line_nested_too_deep_or_with_too_many_rows_is_rejected_before_it_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads_called = _no_loads(monkeypatch)
    deep = '{"hosts":[{"host":"a","nested":{"deeper":1}}]}'
    assert check_shape(deep) == REJECTED_TOO_DEEP
    assert read_probe_line(PROBE_MARKER + deep) == (None, REJECTED_TOO_DEEP)
    many = '{"hosts":[' + ",".join(_row(f"h{i}") for i in range(MAX_PROBE_HOSTS + 1)) + "]}"
    assert len(many) < MAX_PROBE_BYTES
    assert check_shape(many) == REJECTED_TOO_MANY_ROWS
    assert read_probe_line(PROBE_MARKER + many) == (None, REJECTED_TOO_MANY_ROWS)
    # Brackets inside strings are text, not nesting: a detail of "[[[{{{" is fine.
    assert check_shape('{"hosts":[{"host":"a","detail":"[[[{{{\\"}}]}') is None
    assert loads_called == []
    # Exactly the cap fits.
    full = '{"hosts":[' + ",".join(_row(f"h{i}") for i in range(MAX_PROBE_HOSTS)) + "]}"
    assert check_shape(full) is None


def test_scalar_rows_past_the_cap_are_rejected_after_parsing_and_rows_are_bounded() -> None:
    """Rows that open no bracket slip past the scan; the parsed array is bounded too."""
    scalars = '{"hosts":[' + ",".join("1" for _ in range(MAX_PROBE_HOSTS + 1)) + "]}"
    assert check_shape(scalars) is None
    assert read_probe_line(PROBE_MARKER + scalars) == (None, REJECTED_TOO_MANY_ROWS)
    document = {"hosts": [{"host": f"h{i}"} for i in range(MAX_PROBE_HOSTS + 5)]}
    normalised = normalise_probe(document)
    assert normalised is not None and len(normalised["hosts"]) == MAX_PROBE_HOSTS
    assert normalise_probe({"hosts": [{"host": "h" * 400}]}) == {
        "hosts": [
            {"host": "h" * 200, "reachable": False, "curl_exit": None, "ms": None, "detail": ""}
        ]
    }


def test_a_marker_line_that_is_not_a_probe_document_says_why() -> None:
    assert read_probe_line("crucible-egress-probe: not json") == (None, REJECTED_NOT_JSON)
    assert read_probe_line('crucible-egress-probe: ["list"]') == (None, REJECTED_NOT_JSON)
    assert read_probe_line('crucible-egress-probe: {"nope": 1}') == (None, REJECTED_NOT_A_PROBE)
    assert read_probe_line("plain line") == (None, None)
    assert read_probe_line('crucible-egress-probe: {"hosts":[]}') == ({"hosts": []}, None)


def test_find_probe_line_lets_the_first_marker_line_decide() -> None:
    """A rejected first line is the answer; a well-formed line after it is not read,
    because the wrapper's line is always the first one."""
    text = (
        "crucible-egress-probe: not json\n"
        'crucible-egress-probe: {"hosts":[{"host":"b","reachable":true,"curl_exit":0}]}\n'
    )
    assert find_probe_line(text) == (None, REJECTED_NOT_JSON)
    assert find_probe(text) is None
    assert find_probe_line("nothing\n") == (None, None)


def test_probe_expected_follows_the_providers_network_rule() -> None:
    assert probe_expected({"network": {"mode": "egress-proxy"}}, {"constraints": {}}) is True
    assert probe_expected({}, {}) is True
    assert probe_expected(None, None) is True
    assert probe_expected({"network": {"mode": "none"}}, {}) is False
    assert probe_expected({}, {"constraints": {"network": "none"}}) is False
    assert (
        probe_expected(
            {"network": {"mode": "egress-proxy"}}, {"constraints": {"network": "policy"}}
        )
        is True
    )


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


def _execution(policy: dict[str, Any] | None = None) -> Execution:
    return Execution(
        id="execution",
        task_id="task",
        role=ExecutionRole.IMPLEMENT,
        contract_version=1,
        harness="script-harness",
        model="a-scripted-quota",
        effort=None,
        provider="docker",
        image="crucible-worker:test",
        policy_snapshot=policy if policy is not None else {"network": {"mode": "egress-proxy"}},
        state=ExecutionState.ACTIVE,
        max_attempts=3,
        retry_on=[],
        timeout_seconds=600,
        created_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
    )


def _contract(document: dict[str, Any] | None = None) -> TaskContract:
    return TaskContract(
        id="contract",
        task_id="task",
        version=1,
        document=document if document is not None else {"constraints": {}},
        sha256="0" * 64,
        submitted_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
    )


_WITH_NETWORK = _execution()
_PLAIN_CONTRACT = _contract()


class _Uow:
    def __init__(
        self,
        attempt: Attempt,
        *,
        execution: Execution | None = _WITH_NETWORK,
        contract: TaskContract | None = _PLAIN_CONTRACT,
    ) -> None:
        self.attempts = _Attempts(attempt)
        self.logs = _Logs()
        self.heartbeats = SimpleNamespace(append=lambda *a, **k: None)
        self.committed = 0
        self.lookups = 0

        def get_execution(execution_id: str, **kwargs: Any) -> Execution | None:
            self.lookups += 1
            return execution

        self.executions = SimpleNamespace(get=get_execution)
        self.contracts = SimpleNamespace(get=lambda task_id, version: contract)

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


def test_kind_log_shape_records_ascii_probe_and_replaces_invalid_worker_bytes() -> None:
    """The real pods/log stream is timestamp-prefixed and may contain arbitrary worker
    bytes. Its marker still lands on the attempt, and stored logs remain valid UTF-8."""
    attempt = _attempt()
    uow = _Uow(attempt)
    supervisor = _supervisor(uow)
    raw = (
        b"2026-10-05T12:00:01.123456789Z crucible-egress-probe: "
        b'{"hosts":[{"host":"github.com","reachable":true,"curl_exit":0,'
        b'"ms":12,"detail":""}]}\n'
        b"2026-10-05T12:00:02.123456789Z publisher byte: \xfc\n"
    )

    assert supervisor._store_logs(attempt.id, (LogChunk("stdout", raw, line_sha256="kind"),)) == 1
    assert attempt.egress_probe is not None
    assert attempt.egress_probe["hosts"] == [
        {
            "host": "github.com",
            "reachable": True,
            "curl_exit": 0,
            "ms": 12,
            "detail": "",
        }
    ]
    stored = uow.logs.appended[0].content
    assert "publisher byte: \ufffd" in stored.decode("utf-8")


def test_a_log_without_the_line_records_nothing() -> None:
    attempt = _attempt()
    uow = _Uow(attempt)
    supervisor = _supervisor(uow)
    supervisor._store_logs(attempt.id, (LogChunk("stdout", b"just work\n", line_sha256="a"),))
    assert attempt.egress_probe is None
    # No marker, no lookup of what the attempt runs under.
    assert uow.lookups == 0


def _marker(payload: str) -> LogChunk:
    return LogChunk("stderr", (PROBE_MARKER + payload + "\n").encode(), line_sha256="m")


def test_an_oversized_marker_line_is_rejected_recorded_and_the_log_still_advances(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Codex P2 on PR 431: a hostile first line is rejected before `json.loads`, the
    attempt records the rejection, the chunk is stored so the offset commits and the
    pull is not retried, and a well-formed line after it is never read."""
    attempt = _attempt()
    uow = _Uow(attempt)
    supervisor = _supervisor(uow)
    loads_called = _no_loads(monkeypatch)
    huge = '{"hosts":[' + ",".join(_row(f"h{i}", "x" * 190) for i in range(300)) + "]}"
    assert len(huge) > MAX_PROBE_BYTES
    with caplog.at_level("WARNING"):
        stored = supervisor._store_logs(attempt.id, (_marker(huge),))
    assert stored == 1 and len(uow.logs.appended) == 1
    assert uow.logs.appended[0].offset_end == len(uow.logs.appended[0].content)
    assert attempt.egress_probe == {
        "hosts": [],
        "rejected": REJECTED_TOO_LONG,
        "recorded_at": "2026-10-05T12:01:00+00:00",
    }
    assert loads_called == []
    assert any("egress probe line rejected" in r.message for r in caplog.records)
    later = _marker('{"hosts":[{"host":"evil","reachable":true,"curl_exit":0}]}')
    supervisor._store_logs(attempt.id, (later,))
    assert attempt.egress_probe["rejected"] == REJECTED_TOO_LONG
    assert loads_called == []


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ('{"hosts":[{"host":"a","deeper":{"x":[1]}}]}', REJECTED_TOO_DEEP),
        (
            '{"hosts":[' + ",".join(_row(f"h{i}") for i in range(MAX_PROBE_HOSTS + 1)) + "]}",
            REJECTED_TOO_MANY_ROWS,
        ),
        ("{not json", REJECTED_NOT_JSON),
        ('{"other": true}', REJECTED_NOT_A_PROBE),
    ],
)
def test_each_rejection_path_is_recorded_on_the_attempt(payload: str, reason: str) -> None:
    attempt = _attempt()
    supervisor = _supervisor(_Uow(attempt))
    supervisor._store_logs(attempt.id, (_marker(payload),))
    assert attempt.egress_probe is not None
    assert attempt.egress_probe["rejected"] == reason and attempt.egress_probe["hosts"] == []


def test_a_marker_line_from_an_attempt_with_no_network_is_ignored_and_never_parsed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No network, no wrapper probe, so the line can only be the harness's."""
    loads_called = _no_loads(monkeypatch)
    line = _marker('{"hosts":[{"host":"pypi.org","reachable":true,"curl_exit":0}]}')
    for uow in (
        _Uow(_attempt(), execution=_execution({"network": {"mode": "none"}})),
        _Uow(_attempt(), contract=_contract({"constraints": {"network": "none"}})),
        _Uow(_attempt(), execution=None),
        _Uow(_attempt(), contract=None),
    ):
        supervisor = _supervisor(uow)
        with caplog.at_level("WARNING"):
            stored = supervisor._store_logs(uow.attempts.attempt.id, (line,))
        assert stored == 1
        assert uow.attempts.attempt.egress_probe is None
        assert uow.lookups == 1
    assert loads_called == []
    assert any("egress probe line ignored" in r.message for r in caplog.records)


def test_once_a_probe_is_recorded_no_later_marker_line_is_examined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt()
    attempt.egress_probe = {"hosts": [], "recorded_at": "2026-10-05T12:00:30+00:00"}
    uow = _Uow(attempt)
    supervisor = _supervisor(uow)
    loads_called = _no_loads(monkeypatch)
    line = _marker('{"hosts":[{"host":"evil","reachable":true,"curl_exit":0}]}')
    supervisor._store_logs(attempt.id, (line, line))
    assert attempt.egress_probe == {"hosts": [], "recorded_at": "2026-10-05T12:00:30+00:00"}
    assert loads_called == [] and uow.lookups == 0


def test_the_network_rule_is_looked_up_once_per_store_call() -> None:
    attempt = _attempt()
    uow = _Uow(attempt, execution=_execution({"network": {"mode": "none"}}))
    supervisor = _supervisor(uow)
    line = _marker('{"hosts":[]}')
    supervisor._store_logs(attempt.id, (line, line, line))
    assert uow.lookups == 1 and attempt.egress_probe is None


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
    rejected = SimpleNamespace(
        executions=[
            SimpleNamespace(
                attempts=[
                    SimpleNamespace(
                        id="A2", egress_probe={"hosts": [], "rejected": REJECTED_TOO_LONG}
                    )
                ]
            )
        ]
    )
    assert tasks_mod._egress_rows(rejected) == [
        ["A2", "none", f"probe line rejected: {REJECTED_TOO_LONG}"]
    ]


@pytest.mark.skipif(not os.environ.get("CRUCIBLE_EGRESS_ALLOWLIST"), reason="not inside a worker")
def test_inside_a_real_worker_the_probe_matches_the_allowlist_it_was_given(tmp_path: Path) -> None:
    """Only inside a Crucible worker: the wrapper's probe, with the image's real curl,
    names exactly the hosts the provider allowlisted for this attempt."""
    expected = [h for h in os.environ["CRUCIBLE_EGRESS_ALLOWLIST"].split(",") if ":" not in h]
    run = _run_wrapper(tmp_path, os.environ["CRUCIBLE_EGRESS_ALLOWLIST"], "true", curl=None)
    assert run.returncode == 0
    (probe,) = _probe_lines(run.stderr)
    assert [row["host"] for row in probe["hosts"]] == expected
