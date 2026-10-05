"""Hades #401: correction reports carry one disposition per Codex finding."""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from crucible.application.delivery_tick import DeliveryCoordinator
from crucible.contracts.completion_claim import parse_claim
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.ports.execution import CollectedOutputs
from crucible.ports.github import CommentRecord
from tests.unit.test_issue_337_auto_merge import Host
from tests.unit.test_issue_353_infrastructure_interruptions import _running
from tests.unit.test_issue_360_ready_for_merge_correction import NOW, OLD_HEAD, TASK_ID
from tests.unit.test_issue_411_merge_mechanics import _poll, _world

ROOT = Path(__file__).resolve().parents[2]


def _report(dispositions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "self_review": {
            "documentation": ["No documentation change needed for this test fixture."],
            "acceptance_criteria": [
                {"id": "AC1", "status": "met", "evidence": "Test fixture"},
                {"id": "AC2", "status": "met", "evidence": "Test fixture"},
            ],
            "omissions": [],
        },
        "summary": "Corrected the review findings.",
        "acceptance_mapping": [
            {"id": "AC1", "status": "met", "evidence": "covered by the correction"},
            {"id": "AC2", "status": "met", "evidence": "regressions passed"},
        ],
        "proposed_pull_request": {"title": "Apply Codex findings", "body": "Done."},
        "limitations": [],
        "risks": [],
        "blockers": [],
        "follow_ups": [],
        "finding_dispositions": dispositions,
    }


def test_completion_claim_accepts_fixed_and_declined_findings() -> None:
    claim, errors = parse_claim(
        _report(
            [
                {
                    "review_comment_id": "finding-1",
                    "disposition": "fixed",
                    "commit": "a" * 40,
                },
                {
                    "review_comment_id": "finding-2",
                    "disposition": "declined",
                    "reason": "The suggested branch is unreachable by contract.",
                },
            ]
        )
    )
    assert errors == []
    assert claim is not None
    assert [item.disposition for item in claim.finding_dispositions] == ["fixed", "declined"]


def test_completion_claim_requires_disposition_evidence() -> None:
    claim, errors = parse_claim(
        _report([{"review_comment_id": "finding-1", "disposition": "declined"}])
    )
    assert claim is None
    assert any("finding_dispositions" in error["loc"] for error in errors)


def test_worker_report_checker_mirrors_finding_dispositions() -> None:
    script = ROOT / "images/worker/crucible-report.py"
    spec = importlib.util.spec_from_file_location("worker_report_issue_401", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    problems = module.check(
        _report(
            [
                {
                    "review_comment_id": "finding-1",
                    "disposition": "fixed",
                    "commit": "a" * 40,
                }
            ]
        ),
        criteria=["AC1", "AC2"],
    )
    assert problems == []


# Exercise observation through the delivery coordinator and the existing fake publisher.
def _feedback_world(tmp_path: Path, monkeypatch: Any, provider: str) -> tuple[Any, ...]:

    store, clock, supervisor, client, publisher = _world(
        tmp_path, state=TaskState.AWAITING_EXTERNAL_REVIEW
    )
    client.pull().mergeable_state = "clean"
    client.pull().mergeable = True
    store.policies.policy.document["external_review"]["provider"] = provider
    comments: list[Any] = []
    rows = MagicMock()
    rows.list_for_pull_request.side_effect = lambda *_: list(comments)
    rows.get_by_github.side_effect = lambda pr, kind, github_id: next(
        (c for c in comments if c.kind == kind and c.github_id == github_id), None
    )

    def add_comment(row: Any) -> bool:
        comments.append(row)
        return True

    rows.add.side_effect = add_comment
    store.review_comments = rows
    store.dispositions = MagicMock()
    store.dispositions.get_by_comment.return_value = None
    store.dispositions.list_for_task.return_value = []
    observe = client.observe
    findings = tuple(
        CommentRecord(
            github_id=str(i),
            login="chatgpt-codex-connector[bot]",
            body=f"Finding {i}",
            created_at=NOW,
            updated_at=NOW,
            path="app.py",
            line=i,
            commit_id=OLD_HEAD,
        )
        for i in (1, 2)
    )
    monkeypatch.setattr(
        client, "observe", lambda *a, **kw: replace(observe(*a, **kw), review_comments=findings)
    )
    return store, clock, supervisor, client, publisher


@pytest.mark.parametrize("provider", ["codex", "other", "none"])
def test_only_codex_findings_schedule_automatic_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:

    store, clock, supervisor, _, _ = _feedback_world(tmp_path, monkeypatch, provider)
    assert _poll(store, clock, supervisor) == 1
    task = store.tasks.get(TASK_ID)
    assert task.state is (
        TaskState.SCHEDULED if provider == "codex" else TaskState.EXTERNAL_FEEDBACK_RECEIVED
    )
    assert len(store.wakes.rows) == 1
    assert task.contract_version == (2 if provider == "codex" else 1)
    if provider == "codex":
        correction = store.contracts.get(TASK_ID, 2).document["correction"]
        assert len(correction["addresses"]) == 2
        assert "Path: app.py\nLine: 1\nBody:\nFinding 1" in correction["instructions"]
        supervisor._materialize_scheduled()
        assert len(store.executions.list_for_task(TASK_ID)) == 2


def _report_attempt(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:

    supervisor, pending, uow, attempts = _running(monkeypatch)
    pending.contract["correction"] = {
        "addresses": [{"kind": "review_comment", "id": name} for name in ("finding-1", "finding-2")]
    }
    comments = {
        name: SimpleNamespace(id=name, body_sha256="body", github_id=name)
        for name in ("finding-1", "finding-2")
    }
    dispositions: list[Any] = []
    uow.review_comments.get.side_effect = comments.get
    uow.dispositions.get_by_comment.side_effect = lambda cid, digest: next(
        (d for d in dispositions if d.review_comment_id == cid), None
    )
    uow.dispositions.add.side_effect = dispositions.append
    claims: dict[str, Any] = {}
    uow.claims.put.side_effect = lambda claim: claims.update({claim.attempt_id: claim})
    uow.claims.get.side_effect = claims.get
    return supervisor, pending, uow, attempts, dispositions


def _dispositions() -> list[dict[str, Any]]:
    return [
        {"review_comment_id": "finding-1", "disposition": "fixed", "commit": "a" * 40},
        {"review_comment_id": "finding-2", "disposition": "declined", "reason": "Not reachable."},
    ]


@pytest.mark.parametrize("failure", ["exit", "collection", "local_cap"])
def test_failed_report_does_not_record_dispositions_or_reply_before_retry(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:

    supervisor, pending, uow, attempts, dispositions = _report_attempt(monkeypatch)
    if failure == "local_cap":
        monkeypatch.setattr(supervisor, "_local_cap", lambda *a: "test_cap")
    outputs = CollectedOutputs(report=_report(_dispositions()), report_raw=None, blocked_md=None)
    supervisor._finish_exited(
        pending.attempt.id,
        1 if failure == "exit" else 0,
        outputs,
        collection_error="collection failed" if failure == "collection" else None,
    )
    assert pending.attempt.state is AttemptState.FAILED
    assert uow.claims.get(pending.attempt.id).parsed_ok
    assert dispositions == []
    github = MagicMock()
    coordinator = DeliveryCoordinator(Host(uow), supervisor._clock, github=github)
    uow.pull_requests.get_for_task.return_value = SimpleNamespace(number=1)
    uow.repositories.get.return_value = SimpleNamespace(name="org/repo", installation_id=1)
    asyncio.run(coordinator._post_decline_replies())
    github.reply_to_review_comment.assert_not_called()

    retry = replace(pending.attempt, id="retry", number=2, state=AttemptState.RUNNING)
    attempts.append(retry)
    uow.attempts.get.return_value = retry
    pending.task.state = TaskState.RUNNING
    pending.execution.state = ExecutionState.ACTIVE
    monkeypatch.setattr(supervisor, "_local_cap", lambda *a: None)
    revised = _dispositions()
    revised[1]["reason"] = "The retry verified this branch is unreachable."
    supervisor._finish_exited(
        retry.id, 0, CollectedOutputs(report=_report(revised), report_raw=None, blocked_md=None)
    )
    assert retry.state is AttemptState.SUCCEEDED
    assert len(dispositions) == 2
    assert dispositions[1].reasoning == revised[1]["reason"]
    asyncio.run(coordinator._post_decline_replies())
    github.reply_to_review_comment.assert_called_once()
    assert github.reply_to_review_comment.call_args.kwargs["body"] == revised[1]["reason"]


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_finding_ids_are_rejected_with_ids_named(
    monkeypatch: pytest.MonkeyPatch, conflicting: bool
) -> None:

    supervisor, pending, uow, _, dispositions = _report_attempt(monkeypatch)
    reported = _dispositions()
    reported.append(
        {"review_comment_id": "finding-1", "disposition": "declined", "reason": "Conflict"}
        if conflicting
        else dict(reported[0])
    )
    supervisor._finish_exited(
        pending.attempt.id,
        0,
        CollectedOutputs(report=_report(reported), report_raw=None, blocked_md=None),
    )
    claim = uow.claims.get(pending.attempt.id)
    assert not claim.parsed_ok
    assert any("duplicate ids: finding-1" in error["msg"] for error in claim.parse_errors)
    assert pending.attempt.state is AttemptState.FAILED
    assert dispositions == []


def test_connector_refusal_wakes_without_scheduling_a_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, clock, supervisor, client, _ = _feedback_world(tmp_path, monkeypatch, "codex")
    observe = client.observe
    refusal = CommentRecord(
        github_id="refusal",
        login="chatgpt-codex-connector[bot]",
        body="To use Codex here, create a Codex account and connect to github.",
        created_at=NOW,
        updated_at=NOW,
        kind="issue_comment",
    )
    monkeypatch.setattr(
        client,
        "observe",
        lambda *a, **kw: replace(observe(*a, **kw), review_comments=(), issue_comments=(refusal,)),
    )
    assert _poll(store, clock, supervisor) == 1
    assert len(store.wakes.rows) == 1
    assert "external review failed" in store.wakes.rows[0].payload["summary"]
    assert store.tasks.get(TASK_ID).contract_version == 1
    assert _poll(store, clock, supervisor) == 1
    assert len(store.wakes.rows) == 1
