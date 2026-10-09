"""Gate-failed corrections retain their sealed work even after publication."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from crucible.adapters.execution import scripts
from crucible.application.gates import evaluate_and_advance
from crucible.contracts.task_contract import Correction
from crucible.domain.entities import ExecutionRole
from crucible.domain.events import EventKind
from crucible.domain.gates import GateOutcome, GateResult
from crucible.domain.lifecycle import TaskState
from crucible.ports.execution import WORK_MOUNT
from tests.unit.test_correction_resume import _git, _repository
from tests.unit.test_routing import _routing_setup


def test_correction_resume_default_and_explicit_alternative() -> None:
    values = dict(
        of_version=1,
        reason="pre_pr_gates",
        addresses=[],
        instructions="Fix the test",
        request_internal_review=False,
    )
    assert Correction.model_validate(values).resume_from == "last_attempt"
    assert (
        Correction.model_validate({**values, "resume_from": "remote_branch"}).resume_from
        == "remote_branch"
    )
    assert (
        Correction.model_validate({**values, "reason": "external_review"}).resume_from
        == "remote_branch"
    )


@pytest.mark.parametrize("resume", [None, "last_attempt", "remote_branch"])
@pytest.mark.parametrize("newer_attempt", [False, True])
@pytest.mark.parametrize("failed_gate", ["verification_ran", "no_secrets"])
async def test_published_correction_selects_only_newest_gate_failed_bundle(
    monkeypatch: pytest.MonkeyPatch,
    resume: str | None,
    newer_attempt: bool,
    failed_gate: str,
) -> None:
    supervisor, pending, uow = _routing_setup(monkeypatch, all_busy=False)
    pending.execution.role = ExecutionRole.CORRECT
    previous = replace(pending.attempt, id="000failed", workspace_path="/workspace/failed")
    attempts = [previous, pending.attempt]
    if newer_attempt:
        attempts.append(replace(previous, id="001newer"))
    uow.executions.list_for_task.return_value = [pending.execution]
    uow.attempts.list_for_execution.return_value = attempts
    uow.evidence.list_for_attempt.return_value = [
        SimpleNamespace(
            kind="bundle_head",
            verified=True,
            payload={"bundle_verified": True, "head_sha": "failed-head", "bundle_sha256": "seal"},
        )
    ]
    uow.gate_results.list_for_attempt.return_value = [
        SimpleNamespace(gate=failed_gate, result="fail")
    ]
    events = {
        EventKind.PUBLISH_COMPLETED.value: SimpleNamespace(payload={"head_sha": "pushed-head"}),
        EventKind.TASK_PRE_PR_GATES_FAILED.value: SimpleNamespace(attempt_id=previous.id),
    }
    uow.events.latest_for_task_kind.side_effect = lambda _task, kind: events.get(kind)
    pending.contract["correction"] = {"reason": "pre_pr_gates"}
    if resume:
        pending.contract["correction"]["resume_from"] = resume
    launch = await supervisor._build_spec(
        pending.attempt, pending.execution, pending.task, pending.contract
    )
    if resume == "remote_branch" or newer_attempt or failed_gate == "no_secrets":
        assert launch.resume_bundle_path is None
    else:
        assert launch.resume_bundle_path == "/workspace/failed/output/work_branch.bundle"
        assert launch.resume_bundle_head == "failed-head"
        assert launch.resume_bundle_sha256 == "seal"
        assert launch.resume_bundle_ancestor == "pushed-head"


@pytest.mark.parametrize("mode", ["descendant", "divergent", "tampered", "secret"])
def test_preparer_checks_seal_and_task_ancestry_before_using_bundle(
    tmp_path: Path, mode: str
) -> None:
    origin, seed, published, bundle = _repository(tmp_path)
    _git(seed, "push", "origin", "crucible/FDY-0150")
    if mode == "divergent":
        _git(seed, "reset", "--hard", "main")
    (seed / "file.txt").write_text("failed attempt's work\n")
    if mode == "secret":
        (seed / "credential.txt").write_text("ghs_" + "A" * 30 + "\n")
        _git(seed, "add", "credential.txt")
    _git(seed, "commit", "-am", "gate-failed correction")
    failed = _git(seed, "rev-parse", "HEAD")
    _git(seed, "bundle", "create", str(bundle), "main..crucible/FDY-0150")
    seal = hashlib.sha256(bundle.read_bytes()).hexdigest()
    if mode == "tampered":
        with bundle.open("ab") as output:
            output.write(b"tampering")
    identity = tmp_path / "identity"
    identity.mkdir()
    work = tmp_path / "work"
    script = (
        scripts.preparer_script(
            url=str(origin),
            base_ref="main",
            work_branch="crucible/FDY-0150",
            from_remote_branch=True,
            cache_name=None,
            author_name="Test",
            author_email="test@example.test",
            origin_placeholder="https://invalid.example/repo",
            claude_md_wins=False,
            shims=(),
            exclude_entries=(),
            identity_mount=str(identity),
            resume_bundle=str(bundle),
            resume_bundle_head=failed,
            resume_bundle_sha256=seal,
            resume_bundle_ancestor=published,
        )
        .replace(WORK_MOUNT, str(work))
        .replace("/tmp/gitconfig", str(tmp_path / "gitconfig"))
    )
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    if mode in {"descendant", "secret"}:
        assert result.returncode == 0, result.stderr
        assert _git(work / "repo", "rev-parse", "HEAD") == failed
        if mode == "secret":
            assert "path=credential.txt" in result.stderr
            assert "rule=github_installation_token" in result.stderr
            assert "excerpt=ghs_...AAA (" in result.stderr
    else:
        assert result.returncode == 4
        assert (
            "does not descend from task head" if mode == "divergent" else "does not match its seal"
        ) in result.stderr
        assert not (work / "output" / "prepared-head.txt").exists()
    # Preparing the next attempt never publishes the failed work.
    assert _git(origin, "rev-parse", "refs/heads/crucible/FDY-0150") == published


def test_gate_failure_wake_names_next_starting_head(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow = _routing_setup(monkeypatch, all_busy=False)
    pending.task.state = TaskState.REPORTED
    pending.task.head_sha = "failed-head"
    pending.execution.policy_snapshot = {}
    uow.contracts.get.return_value = SimpleNamespace(document=pending.contract)
    uow.evidence.list_for_attempt.return_value = []
    uow.gate_results.list_for_attempt.return_value = []
    monkeypatch.setattr(
        "crucible.application.gates.evaluate_pre_pr",
        lambda _gates, _input: {
            "verification_ran": GateOutcome(GateResult.FAIL, "verification failed"),
            "no_secrets": GateOutcome(GateResult.PASS, "scanner found nothing"),
        },
    )
    evaluate_and_advance(
        uow,
        supervisor._clock,
        task=pending.task,
        attempt=pending.attempt,
        execution=pending.execution,
    )
    assert pending.task.state is TaskState.PRE_PR_GATES_FAILED
    wake = uow.wakes.add.call_args.args[0]
    assert "defaults to last_attempt at failed-head" in wake.payload["summary"]
    assert "remote_branch is an explicit alternative" in wake.payload["summary"]


def test_no_secrets_failure_wake_names_correctable_match(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow = _routing_setup(monkeypatch, all_busy=False)
    pending.task.state = TaskState.REPORTED
    pending.task.head_sha = "unsafe-head"
    pending.execution.policy_snapshot = {}
    uow.contracts.get.return_value = SimpleNamespace(document=pending.contract)
    uow.evidence.list_for_attempt.return_value = []
    uow.gate_results.list_for_attempt.return_value = []
    uow.events.latest_for_task_kind.return_value = SimpleNamespace(
        payload={"head_sha": "published-head"}
    )
    monkeypatch.setattr(
        "crucible.application.gates.evaluate_pre_pr",
        lambda _gates, _input: {
            "no_secrets": GateOutcome(GateResult.FAIL, "secret pattern matched")
        },
    )
    evaluate_and_advance(
        uow,
        supervisor._clock,
        task=pending.task,
        attempt=pending.attempt,
        execution=pending.execution,
    )
    wake = uow.wakes.add.call_args.args[0]
    assert "secret pattern matched" in wake.payload["summary"]
    assert "defaults to last_attempt at unsafe-head" in wake.payload["summary"]
    assert "where the match can be removed" in wake.payload["summary"]
