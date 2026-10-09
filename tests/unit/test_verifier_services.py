"""hades #608, #517, #412: the verifier and the gate probe run beside the attempt's
declared services, the probe takes a named new file as proof, and the verification log
keeps its head as well as its tail.

On FDY-0597 the Postgres sidecar ran beside the worker, but the verifier re-ran
`make test-integration` without it and every test errored on the missing database. The
Docker half is read from the stub daemon (tests/unit/test_docker_services.py), the
Kubernetes half from the fake cluster (tests/unit/kubernetes_fixtures.py), and the
probe's rule from the supervisor with the fake provider (tests/unit/test_gate_probe.py).
Nothing here starts a container.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from crucible.adapters.execution import scripts
from crucible.adapters.execution.collected import (
    VERIFICATION_LOG_LIMIT,
    head_and_tail,
    read_verifications,
)
from crucible.adapters.execution.k8sapi import LogFrame
from crucible.domain.entities import Attempt
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.domain.test_services import POSTGRES_IMAGE, TEST_DATABASE_ENV, TEST_DATABASE_URL
from crucible.domain.verification import (
    PROOF_FAILS,
    PROOF_NEW_FILE,
    PROOF_NONE,
    named_paths,
    probe_proof,
)
from crucible.ports.execution import LaunchSpec
from tests.unit import kubernetes_fixtures
from tests.unit.test_docker_provider import provider as docker_provider
from tests.unit.test_docker_provider import spec as docker_spec
from tests.unit.test_docker_services import ServiceStub
from tests.unit.test_gate_probe import _setup

ATTEMPT = "01ATTEMPT0000000000000000A"
NEW_TEST = "tests/unit/test_issue_608_new.py"


def _env_list(body: dict[str, Any]) -> dict[str, str]:
    return dict(entry.split("=", 1) for entry in body["Env"])


def _env_k8s(container: dict[str, Any]) -> dict[str, str]:
    return {str(e["name"]): str(e["value"]) for e in container.get("env") or []}


# ----- Docker: the verifier's companion container -----------------------------


def _docker_launch(services: list[dict[str, Any]] | None) -> LaunchSpec:
    launch = docker_spec()
    if services is None:
        return launch
    return replace(launch, policy={**launch.policy, "services": services})


def _tree(tmp_path: Path) -> None:
    root = tmp_path / "workspaces" / ATTEMPT
    (root / "output" / "tree").mkdir(parents=True)
    (root / "verify").mkdir(parents=True)


async def test_docker_verifier_runs_beside_the_declared_service(tmp_path: Path) -> None:
    client = ServiceStub()
    docker = docker_provider(tmp_path, client)
    _tree(tmp_path)
    await docker._run_verifier(_docker_launch([{"kind": "postgres"}]))
    names = [str(row["name"]) for row in client.created]
    assert names == [f"crucible-verifier-{ATTEMPT}", f"svc-postgres-verifier-{ATTEMPT}"]
    verifier, service = (row["body"] for row in client.created)
    # The same variable and value the worker was told, so `make test-integration`
    # re-runs against a database exactly as it ran in the worker.
    assert _env_list(verifier)[TEST_DATABASE_ENV] == TEST_DATABASE_URL
    assert service["Image"] == POSTGRES_IMAGE
    # The verifier's own loopback: the companion joins its network namespace.
    assert service["HostConfig"]["NetworkMode"] == "container:container-1"
    # The verifier starts first (Docker needs the namespace's owner running), and the
    # script waits on the service's port before the first check runs.
    assert client.started == ["container-1", "container-2"]
    script = verifier["Cmd"][2]
    assert "until service_up 5432" in script
    assert script.index("until service_up 5432") < script.index("ID='V1'")
    # Both are removed once the verifier is done, the companion first.
    assert client.removed == ["container-2", "container-1"]


async def test_docker_verifier_without_a_declared_service_starts_none(tmp_path: Path) -> None:
    client = ServiceStub()
    docker = docker_provider(tmp_path, client)
    _tree(tmp_path)
    await docker._run_verifier(_docker_launch(None))
    assert [str(row["name"]) for row in client.created] == [f"crucible-verifier-{ATTEMPT}"]
    verifier = client.created[0]["body"]
    assert TEST_DATABASE_ENV not in _env_list(verifier)
    assert "service_up" not in verifier["Cmd"][2]


async def test_docker_verifier_honours_a_contract_that_drops_the_service(
    tmp_path: Path,
) -> None:
    client = ServiceStub()
    docker = docker_provider(tmp_path, client)
    _tree(tmp_path)
    launch = _docker_launch([{"kind": "postgres"}])
    contract = {
        **launch.contract,
        "execution_request": {
            **launch.contract["execution_request"],
            "services": [{"kind": "postgres", "enabled": False}],
        },
    }
    await docker._run_verifier(replace(launch, contract=contract))
    assert [str(row["name"]) for row in client.created] == [f"crucible-verifier-{ATTEMPT}"]


async def test_docker_verifier_whose_service_image_is_absent_is_unverified_with_the_reason(
    tmp_path: Path,
) -> None:
    client = ServiceStub(has_postgres=False)
    docker = docker_provider(tmp_path, client)
    _tree(tmp_path)
    runs = await docker._run_verifier(_docker_launch([{"kind": "postgres"}]))
    assert runs and all(not run.ran for run in runs)
    assert all("declared postgres service image" in run.detail for run in runs)
    # The verifier that never started is still removed.
    assert client.started == [] and client.removed == ["container-1"]


# ----- Kubernetes: the native sidecar on the verifier and the probe ------------


def _k8s_launch(contract_services: list[dict[str, Any]] | None = None) -> LaunchSpec:
    launch = kubernetes_fixtures.spec()
    policy = {**launch.policy, "services": [{"kind": "postgres"}]}
    contract = launch.contract
    if contract_services is not None:
        contract = {
            **contract,
            "execution_request": {
                **contract["execution_request"],
                "services": contract_services,
            },
        }
    return replace(launch, policy=policy, contract=contract)


def _sidecars(pod: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        c
        for c in pod.get("initContainers") or []
        if c["name"] == "svc-postgres" and c.get("restartPolicy") == "Always"
    ]


async def test_kubernetes_verifier_job_carries_the_sidecar_and_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = kubernetes_fixtures.build()
    monkeypatch.setattr(provider, "_await_job", AsyncMock(return_value=0))
    launch = _k8s_launch()
    await provider._run_verifier(launch, provider._limits(launch))
    pod = kubernetes_fixtures.pod_of(api, "verifier-")
    assert len(_sidecars(pod)) == 1
    assert _sidecars(pod)[0]["image"] == POSTGRES_IMAGE
    assert _env_k8s(pod["containers"][0])[TEST_DATABASE_ENV] == TEST_DATABASE_URL


async def test_kubernetes_verifier_without_a_declared_service_has_no_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = kubernetes_fixtures.build()
    monkeypatch.setattr(provider, "_await_job", AsyncMock(return_value=0))
    launch = _k8s_launch([{"kind": "postgres", "enabled": False}])
    await provider._run_verifier(launch, provider._limits(launch))
    pod = kubernetes_fixtures.pod_of(api, "verifier-")
    assert not _sidecars(pod)
    assert TEST_DATABASE_ENV not in _env_k8s(pod["containers"][0])


async def test_kubernetes_gate_probe_job_carries_the_sidecar_and_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = kubernetes_fixtures.build()
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    monkeypatch.setattr(provider, "_await_job", AsyncMock(return_value=0))
    await provider.probe_checks(_k8s_launch(), [{"id": "V4", "command": "make test-integration"}])
    pod = kubernetes_fixtures.pod_of(api, "gate-probe")
    # The checkout runs first; the sidecar starts after it and before the checks.
    assert [c["name"] for c in pod["initContainers"]] == ["checkout", "svc-postgres"]
    assert len(_sidecars(pod)) == 1
    assert _env_k8s(pod["containers"][0])[TEST_DATABASE_ENV] == TEST_DATABASE_URL


async def test_kubernetes_probe_keeps_only_the_missing_paths_the_command_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = kubernetes_fixtures.build()
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    monkeypatch.setattr(provider, "_await_job", AsyncMock(return_value=0))
    command = f"uv run pytest {NEW_TEST} -q"
    line = {"id": "V4", "command": command, "exit": 4, "detail": "", "missing": [NEW_TEST, "x/y"]}

    def logs(name: str, **kwargs: Any) -> list[LogFrame]:
        return [LogFrame("stdout", (json.dumps(line) + "\n").encode())]

    monkeypatch.setattr(api, "pod_log", logs)
    rows = await provider.probe_checks(_k8s_launch(), [{"id": "V4", "command": command}])
    assert rows is not None
    assert rows[0].exit_code == 4 and rows[0].missing_paths == (NEW_TEST,)


# ----- the probe's definition of proof (hades #517, #608) -----------------------


def test_named_paths_reads_the_paths_a_command_names() -> None:
    assert named_paths(f"uv run pytest {NEW_TEST} -q") == (NEW_TEST,)
    assert named_paths(f"uv run pytest -q {NEW_TEST}::test_one tests/unit") == (
        NEW_TEST,
        "tests/unit",
    )
    assert named_paths("python -m pytest test_new.py") == ("test_new.py",)
    for command in (
        "make lint",
        "make test-unit",
        "pytest -k 'a or b' --maxfail=1",
        "cat /etc/passwd ../outside/x.py",
        "curl https://example.org/a.py",
        "pytest tests/*.py $HOME/x.py",
        "pytest 'unterminated",
    ):
        assert named_paths(command) == (), command


@pytest.mark.parametrize(
    ("exit_code", "missing", "proof"),
    [
        (4, (NEW_TEST,), PROOF_NEW_FILE),  # pytest: file or directory not found
        (2, (NEW_TEST,), PROOF_NEW_FILE),  # pytest: usage error naming the path
        (0, (NEW_TEST,), PROOF_NEW_FILE),  # absent on the base is failing by definition
        (1, (), PROOF_FAILS),
        (4, (), PROOF_FAILS),
        (0, (), PROOF_NONE),  # passing on the unchanged tree is never proof
    ],
)
def test_probe_proof(exit_code: int, missing: tuple[str, ...], proof: str) -> None:
    assert probe_proof(exit_code, 0, missing) == proof


@pytest.mark.parametrize("exit_code", [4, 2])
async def test_a_named_new_test_file_is_proof_and_never_blocks(
    monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    item.contract["required_verification"][1]["command"] = f"uv run pytest {NEW_TEST} -q"
    provider.gate_probe_exits = {"V4": exit_code}
    provider.gate_probe_missing = {"V4": (NEW_TEST,)}
    assert await supervisor._probe_before_prepare(item, provider, spec)
    assert item.task.state is TaskState.RUNNING
    assert item.attempt.termination_reason is None
    payload = uow.evidence.add.call_args.args[0].payload
    assert payload["proof"] == PROOF_NEW_FILE
    assert payload["missing_paths"] == [NEW_TEST]
    assert payload["detail"] == f"{PROOF_NEW_FILE}: {NEW_TEST}"
    assert payload["exit"] == exit_code


async def test_a_check_that_passes_on_the_unchanged_tree_is_still_not_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    provider.gate_probe_exits = {"V4": 0}
    assert not await supervisor._probe_before_prepare(item, provider, spec)
    assert item.attempt.termination_reason == "gate_proves_nothing"
    assert uow.evidence.add.call_args.args[0].payload["proof"] == PROOF_NONE


async def test_gate_proves_nothing_names_the_checks_and_the_exact_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    item.contract["required_verification"].append({"id": "V2", "command": "test -d src"})
    provider.gate_probe_exits = {"V4": 0, "V2": 0}
    assert not await supervisor._probe_before_prepare(item, provider, spec)
    question = uow.escalations.add.call_args.args[0].question
    assert "V4 passes on the unchanged repo (`test -f made-by-the-worker` exits 0)" in question
    assert "V2 passes on the unchanged repo (`test -d src` exits 0)" in question
    # The fix is a whole check, with an id the contract does not use yet.
    assert "Fix: amend the contract" in question
    assert (
        '{"id": "V5", "kind": "command", "command": '
        '"uv run pytest -q tests/unit/test_issue_<n>_<slug>.py", "expect_exit": 0}'
    ) in question
    assert "reschedule" in question and "spent no attempt" in question
    assert chr(0x2014) not in question  # no em-dash in operator-facing text


async def test_an_amended_check_is_probed_again_and_the_refusal_spent_no_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    provider.gate_probe_exits = {"V4": 0}
    assert not await supervisor._probe_before_prepare(item, provider, spec)
    blocked = replace(item.attempt)
    assert blocked.started_at is None
    # The operator amends the contract with a check naming a new test file and
    # reschedules; the next attempt is probed again before any worker starts.
    item.contract["required_verification"].append(
        {"id": "V5", "command": f"uv run pytest -q {NEW_TEST}"}
    )
    provider.gate_probe_exits = {"V4": 0, "V5": 4}
    provider.gate_probe_missing = {"V5": (NEW_TEST,)}
    item.attempt = Attempt(
        "next", item.execution.id, item.task.id, 2, AttemptState.PREPARING, blocked.created_at
    )
    item.task.state = TaskState.RUNNING
    uow.attempts.get.return_value = item.attempt
    uow.attempts.list_for_execution.return_value = [blocked, item.attempt]
    assert await supervisor._probe_before_prepare(item, provider, replace(spec, attempt_id="next"))
    assert provider.gate_probe_calls == [blocked.id, "next"]
    assert item.attempt.termination_reason is None


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_probe_script_reports_the_named_paths_the_unchanged_tree_lacks(tmp_path: Path) -> None:
    if shutil.which("jq") is None:
        pytest.skip("jq is not on this machine")
    origin = tmp_path / "origin"
    (origin / "tests" / "unit").mkdir(parents=True)
    (origin / "tests" / "unit" / "test_old.py").write_text("")
    _git(origin, "init", "-b", "main")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "base")
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["sh", "-c", scripts.gate_probe_checkout_script(str(origin), "main", str(checkout))],
        check=True,
        capture_output=True,
    )
    checks = [
        # What pytest does with a path it cannot find: exit 4.
        {"id": "V4", "command": f"sh -c 'exit 4' {NEW_TEST}"},
        {"id": "V5", "command": "test -f tests/unit/test_old.py"},
    ]
    result = subprocess.run(
        ["sh", "-c", scripts.gate_probe_script(str(checkout), checks, 5)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(row["id"], row["exit"], row["missing"]) for row in rows] == [
        ("V4", 4, [NEW_TEST]),
        ("V5", 0, []),
    ]


# ----- the verification log keeps its head and its tail (hades #608) -----------


def test_a_long_log_keeps_the_first_error_and_the_summary(tmp_path: Path) -> None:
    log = tmp_path / "V1.log"
    first = "E   sqlalchemy.exc.OperationalError: connection refused\n"
    middle = "".join(
        f"ERROR tests/integration/test_{n}.py::test - same again\n" for n in range(9000)
    )
    summary = "485 errors in 9.01s\n"
    log.write_text(first + middle + summary)
    assert log.stat().st_size > VERIFICATION_LOG_LIMIT
    kept = head_and_tail(log)
    assert kept.startswith(first)
    assert kept.endswith(summary)
    assert "the middle is omitted" in kept
    assert len(kept.encode("utf-8")) <= VERIFICATION_LOG_LIMIT


def test_a_short_log_is_kept_whole(tmp_path: Path) -> None:
    log = tmp_path / "V1.log"
    log.write_text("1 passed\n")
    assert head_and_tail(log) == "1 passed\n"


def test_a_log_of_invalid_bytes_stays_within_the_cap(tmp_path: Path) -> None:
    log = tmp_path / "V1.log"
    log.write_bytes(b"\xff" * (VERIFICATION_LOG_LIMIT * 2))
    assert len(head_and_tail(log).encode("utf-8")) <= VERIFICATION_LOG_LIMIT
    log.write_bytes(b"\xff" * (VERIFICATION_LOG_LIMIT - 10))
    assert len(head_and_tail(log).encode("utf-8")) <= VERIFICATION_LOG_LIMIT


def test_the_verification_run_carries_the_head_and_the_tail(tmp_path: Path) -> None:
    launch = docker_spec()
    verify = tmp_path / "verify"
    verify.mkdir()
    first = "first error\n"
    (verify / "V1.log").write_text(first + "x" * (3 * VERIFICATION_LOG_LIMIT) + "\nlast line\n")
    (verify / "V1.exit").write_text("1\n")
    runs = read_verifications(verify, launch, [("V1", "make lint")])
    assert runs[0].log_tail.startswith(first)
    assert runs[0].log_tail.endswith("last line\n")
    assert len(runs[0].log_tail.encode("utf-8")) <= VERIFICATION_LOG_LIMIT
