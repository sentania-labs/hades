"""CI failure dispositions gain `implementation_defect` and `missing_worker_tooling`
(hades #356). Operator directive, 2026-10-01: distinguish implementation defects,
missing worker tooling, and infrastructure failures; do not mechanically launch
another worker with the same instructions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from crucible.adapters.persistence.migrations.versions import (
    _0041_ci_decision_causes as migration,
)
from crucible.adapters.ui.pages import board as board_mod
from crucible.adapters.ui.pages import tasks as tasks_mod
from crucible.application.admin.board import board_view
from crucible.client import next as nx
from crucible.contracts.api import CIDecisionRequest
from crucible.domain.entities import CI_RERUN_CAUSES, CIAction, CICause
from crucible.domain.lifecycle import TaskState
from tests.unit.test_board import NOW, fake_uow, row

# ----- the enum keeps every existing value and gains the two new ones ------------------


def test_every_pre_existing_cause_is_still_a_valid_member() -> None:
    pre_existing = {
        "false_pre_pr_evidence",
        "wrong_sha_checked",
        "correction_without_checks",
        "environment_drift",
        "flaky_test",
        "crucible_verification_defect",
        "ci_infrastructure",
        "other",
    }
    assert pre_existing <= {c.value for c in CICause}


def test_the_two_new_causes_exist() -> None:
    assert CICause.IMPLEMENTATION_DEFECT.value == "implementation_defect"
    assert CICause.MISSING_WORKER_TOOLING.value == "missing_worker_tooling"


def test_ci_infrastructure_and_flaky_test_are_the_rerun_causes() -> None:
    assert {CICause.CI_INFRASTRUCTURE, CICause.FLAKY_TEST} == CI_RERUN_CAUSES


# ----- the request rule: correct needs a non-rerun cause, rerun needs a rerun cause ----


@pytest.mark.parametrize(
    ("cause", "action"),
    [
        (CICause.IMPLEMENTATION_DEFECT, CIAction.CORRECT),
        (CICause.MISSING_WORKER_TOOLING, CIAction.CORRECT),
        (CICause.CRUCIBLE_VERIFICATION_DEFECT, CIAction.CORRECT),
        (CICause.OTHER, CIAction.CORRECT),
        (CICause.CI_INFRASTRUCTURE, CIAction.RERUN),
        (CICause.FLAKY_TEST, CIAction.RERUN),
        # reject and cancel are unconstrained
        (CICause.CI_INFRASTRUCTURE, CIAction.REJECT),
        (CICause.FLAKY_TEST, CIAction.CANCEL),
        (CICause.IMPLEMENTATION_DEFECT, CIAction.REJECT),
    ],
)
def test_allowed_cause_action_combinations(cause: CICause, action: CIAction) -> None:
    request = CIDecisionRequest(cause=cause, action=action, reasoning="because")
    assert request.cause is cause
    assert request.action is action


@pytest.mark.parametrize(
    ("cause", "action"),
    [
        (CICause.CI_INFRASTRUCTURE, CIAction.CORRECT),
        (CICause.FLAKY_TEST, CIAction.CORRECT),
        (CICause.IMPLEMENTATION_DEFECT, CIAction.RERUN),
        (CICause.MISSING_WORKER_TOOLING, CIAction.RERUN),
        (CICause.OTHER, CIAction.RERUN),
    ],
)
def test_refused_cause_action_combinations(cause: CICause, action: CIAction) -> None:
    """hades #356: a correct action needs a cause other than ci_infrastructure and
    flaky_test; a rerun needs one of those two. A FastAPI route taking this model as
    its body turns this ValidationError into a 422."""
    with pytest.raises(ValidationError):
        CIDecisionRequest(cause=cause, action=action, reasoning="because")


# ----- the stored CHECK constraint matches the enum, so stored rows still load ---------


def test_migration_causes_match_the_enum() -> None:
    """hades #356: the latest migration owns the ci_decisions.cause CHECK constraint
    (same pattern as test_schema_hygiene.test_migration_event_kinds_match_enum); it
    must list every current CICause value so a stored row of any value still loads."""
    assert set(migration.NEW_CAUSES) == {c.value for c in CICause}


def test_migration_keeps_the_old_causes_unedited() -> None:
    """A shipped migration is never edited (CONTRIBUTING.md); the new revision only
    adds to what 0007 already shipped."""
    assert migration.OLD_CAUSES == (
        "false_pre_pr_evidence",
        "wrong_sha_checked",
        "correction_without_checks",
        "environment_drift",
        "flaky_test",
        "crucible_verification_defect",
        "ci_infrastructure",
        "other",
    )
    assert migration.down_revision == "0040_merge_auto_merge_settings"


# ----- the client CLI's `next` offers the new causes as choices ------------------------


def _task(state: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "id": "T1",
        "state": state,
        "principal": "foundry",
        "policy": {"name": "default-software", "version": 3},
        "open_escalations": [],
        **extra,
    }


def test_next_offers_the_new_causes_as_choices_for_a_ci_decision() -> None:
    actions = nx.task_actions(_task("ci_certification_failed"), nx.ORCHESTRATOR, ["crucible"])
    entry = next(e for e in actions if e["action"] == "ci-decision")
    choices = set(entry["needs"]["cause"]["choices"])
    assert {"implementation_defect", "missing_worker_tooling"} <= choices
    assert choices == {c.value for c in CICause}


# ----- the Board shows which kind of failure a task hit ---------------------------------


def test_board_view_surfaces_the_latest_ci_decision_cause() -> None:
    uow = fake_uow()
    uow.events.rows.append(
        row(
            seq=4,
            task_id="t1",
            kind="ci_decision_recorded",
            payload={"cause": "missing_worker_tooling"},
            ts=NOW - timedelta(seconds=30),
        )
    )
    document = board_view(uow, NOW)
    item = document["in_flight"][6]["parents"][0]["tasks"][0]
    assert item["pull_request"]["cause"] == "missing_worker_tooling"


def test_board_view_pull_request_cause_is_none_without_a_decision() -> None:
    document = board_view(fake_uow(), NOW)
    item = document["in_flight"][6]["parents"][0]["tasks"][0]
    assert item["pull_request"]["cause"] is None


def test_board_view_shows_no_cause_when_decision_predates_a_rerun_certification() -> None:
    """Codex finding on PR 415: the board selected the prior CI decision by task alone,
    so a fresh (second) certification that fails before a new decision exists was still
    labelled with the first certification's stale cause. A cause may only be shown when
    its certification_id matches the certification the row is currently displaying."""
    uow = fake_uow()
    uow.events.rows.append(
        row(
            seq=4,
            task_id="t1",
            kind="ci_certification_recorded",
            payload={"state": "failed", "certification_id": "cert-1"},
            ts=NOW - timedelta(minutes=10),
        )
    )
    uow.events.rows.append(
        row(
            seq=5,
            task_id="t1",
            kind="ci_decision_recorded",
            payload={"cause": "flaky_test", "certification_id": "cert-1"},
            ts=NOW - timedelta(minutes=9),
        )
    )
    uow.events.rows.append(
        row(
            seq=6,
            task_id="t1",
            kind="ci_certification_recorded",
            payload={"state": "failed", "certification_id": "cert-2"},
            ts=NOW - timedelta(minutes=1),
        )
    )
    document = board_view(uow, NOW)
    item = document["in_flight"][6]["parents"][0]["tasks"][0]
    assert item["pull_request"]["cause"] is None


def test_board_page_renders_the_cause_beside_the_pull_request() -> None:
    document = {
        "in_flight": [
            {
                "name": "Blocked or failed gates",
                "parents": [
                    {
                        "parent_external_id": "EPIC-1",
                        "tasks": [
                            {
                                "id": "t1",
                                "external_id": "FDY-1",
                                "title": "Board",
                                "issues": [],
                                "harness": None,
                                "model": None,
                                "pool": None,
                                "state": "ci_certification_failed",
                                "state_since": NOW,
                                "waiting_on": "a CI decision is needed",
                                "pull_request": {
                                    "number": 188,
                                    "url": "",
                                    "ci": {"status": "red", "label": "red"},
                                    "cause": "implementation_defect",
                                    "merge_queue_position": None,
                                },
                                "corrections": 0,
                                "eta": {
                                    "elapsed_seconds": None,
                                    "timeout_seconds": None,
                                    "label": "not started",
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
    sections = board_mod._in_flight_sections(document)
    pr_cell = sections[0]["rows"][0][7]
    assert pr_cell["value"] == "#188 · CI red · cause: implementation defect"


# ----- the task view shows the cause for every CI decision recorded --------------------


def test_task_page_shows_a_ci_decisions_section_with_the_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_at = datetime(2026, 10, 1, 19, 0, tzinfo=UTC)
    decision = SimpleNamespace(
        id="D1",
        cause="missing_worker_tooling",
        action="correct",
        reasoning="the stub harness name did not match the image",
        principal="foundry",
        created_at=created_at,
    )
    record = SimpleNamespace(
        url="https://github.com/acme/repo/pull/9",
        number=9,
        state="open",
        head_sha="abc123",
        completed_rounds=0,
        required_rounds=1,
        last_polled_at=None,
        gates=[],
        ci_certifications=[],
        ci_decisions=[decision],
    )
    view = SimpleNamespace(
        id="T1",
        external_id="FDY-9",
        title="Fix the stub harness",
        state=TaskState.CI_CERTIFICATION_FAILED,
        repository="acme/repo",
        principal="foundry",
        head_sha="abc123",
        created_at=created_at,
        updated_at=created_at,
        executions=[],
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
    principal = SimpleNamespace(name="reader", role=SimpleNamespace(value="observer"))
    uow = SimpleNamespace(events=SimpleNamespace(latest_for_task_kind=lambda *a, **k: None))
    ctx = SimpleNamespace(clock=SimpleNamespace(now=lambda: created_at))
    sections_captured: list[dict[str, Any]] = []

    def fake_page(*args: Any, sections: list[dict[str, Any]], **kwargs: Any) -> str:
        sections_captured.extend(sections)
        return "fake response"

    monkeypatch.setattr(tasks_mod, "_require", lambda *a, **k: (principal, "fixture-csrf"))
    monkeypatch.setattr(tasks_mod, "task_view", lambda *a, **k: view)
    monkeypatch.setattr(tasks_mod, "pull_request_view", lambda *a, **k: record)
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

    ci_section = next(s for s in sections_captured if s["title"] == "CI decisions")
    assert ci_section["rows"] == [
        ["missing worker tooling", "correct", decision.reasoning, "foundry", created_at.isoformat()]
    ]
