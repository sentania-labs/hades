"""Hades #607 and #576 U6: every stuck reason is one plain sentence with an owner; the
card shows the sentence, the owner and only the clicks that apply, with the raw text
under one collapsed Details; the Stuck lane splits Waiting on me from Waiting on
Foundry and needs_me counts only the first; Workers and All tasks fold to phone width."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

from crucible.adapters.ui.pages import proposals as proposals_page
from crucible.adapters.ui.pages import workers as ui_workers
from crucible.adapters.ui.pages.work import work_page
from crucible.adapters.ui.render import templates
from crucible.application.admin.stuck import (
    failing_jobs,
    is_operator_question,
    stuck_facts,
    task_stuck_reason,
)
from crucible.application.board_actions import apply_move
from crucible.application.board_resource import board_resource
from crucible.application.transitions import record_event
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import Escalation, EscalationState, Principal, Role
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from crucible.domain.stuck_reasons import (
    GATE_PROVES_NOTHING,
    Owner,
    StuckFacts,
    StuckReason,
    stuck_reason,
)
from tests.fixtures import FakeClock
from tests.unit.test_board import NOW as BOARD_NOW
from tests.unit.test_board import Repo, row
from tests.unit.test_issue_360_ready_for_merge_correction import NOW, OLD_HEAD
from tests.unit.test_issue_379_merge_during_publish import (
    _correcting,
    _merged,
    _open,
    _publish,
)
from tests.unit.test_issue_424_proposed_tasks import OPERATOR as PROPOSAL_OPERATOR
from tests.unit.test_issue_424_proposed_tasks import _propose
from tests.unit.test_issue_424_proposed_tasks import _store as proposal_store
from tests.unit.test_issue_489_board_lanes import Events, fixture, task
from tests.unit.test_issue_489_card_actions import (
    OBSERVER,
    OPERATOR,
    TASK_ID,
    render_card_html,
    store_for,
)

_S = TaskState
# The em and en dash, built from their code points so this file carries neither.
DASHES = frozenset({chr(0x2014), chr(0x2013)})
QUESTION = "Which of the two layouts does AC2 mean? The issue shows both."


def _reason(**facts: Any) -> StuckReason:
    found = stuck_reason(StuckFacts(**facts))
    assert found is not None
    assert DASHES.isdisjoint(found.sentence)
    return found


# ----- AC1: one plain sentence and an owner per reason ------------------------------


def test_gate_proves_nothing_says_the_checks_already_pass_and_foundry_acts() -> None:
    found = _reason(state=_S.BLOCKED, blocked_reason="gate_proves_nothing", question="V2 passes")
    assert found.key == "gate_proves_nothing"
    assert found.sentence == (
        "The checks already pass before any change, so a run could not prove anything. "
        "Foundry adds a check that fails first."
    )
    assert found.sentence == GATE_PROVES_NOTHING
    assert found.owner is Owner.FOUNDRY and found.quote is None
    assert found.clicks == ("send_back", "cancel")
    assert found.group == "waiting_on_foundry"


def test_ambiguous_contract_is_foundrys_and_quotes_the_workers_question() -> None:
    found = _reason(
        state=_S.BLOCKED,
        escalation_reason="ambiguous_contract",
        question=f"reason: ambiguous_contract\n{QUESTION}",
    )
    assert found.key == "ambiguous_contract"
    assert found.sentence.startswith("The worker stopped because the contract reads two ways.")
    assert found.owner is Owner.FOUNDRY
    assert found.quote == QUESTION
    assert "answer" not in found.clicks


def test_missing_capability_is_foundrys_and_quotes_the_workers_question() -> None:
    found = _reason(
        state=_S.BLOCKED,
        escalation_reason="missing_capability",
        question="The image has no gitleaks. I tried apt.",
    )
    assert found.key == "missing_capability"
    assert "lacks a program or access it needs" in found.sentence
    assert found.owner is Owner.FOUNDRY
    assert found.quote == "The image has no gitleaks. I tried apt."


@pytest.mark.parametrize(
    "facts",
    [
        {"state": _S.PRE_PR_GATES_FAILED, "harness_refused": True},
        {"state": _S.BLOCKED, "blocked_reason": "too_big_for_local:32768"},
        {
            "state": _S.BLOCKED,
            "escalation_reason": "missing_capability",
            "question": "Routing put me on the wrong harness; this needs Codex.",
        },
    ],
)
def test_a_wrong_harness_is_foundrys_to_route_again(facts: dict[str, Any]) -> None:
    found = _reason(**facts)
    assert found.key == "wrong_harness"
    assert found.sentence == (
        "The task went to a harness that cannot run it. Foundry routes it to another harness."
    )
    assert found.owner is Owner.FOUNDRY


def test_ci_failure_names_the_failing_jobs_in_words_and_foundry_decides() -> None:
    one = _reason(state=_S.CI_CERTIFICATION_FAILED, failing_jobs=("unit",))
    assert one.key == "ci_certification_failed"
    assert one.sentence.startswith("CI failed on the pull request: the unit job failed.")
    assert one.owner is Owner.FOUNDRY
    many = _reason(state=_S.CI_CERTIFICATION_FAILED, failing_jobs=("unit", "lint", "e2e"))
    assert "the unit, lint and e2e jobs failed." in many.sentence
    unknown = _reason(state=_S.CI_CERTIFICATION_FAILED)
    assert "the failing job was not recorded" in unknown.sentence


def test_ci_failure_diagnosed_as_the_codes_fault_is_the_workers_on_a_correction() -> None:
    found = _reason(
        state=_S.CI_CERTIFICATION_FAILED,
        failing_jobs=("unit", "lint"),
        ci_cause="implementation_defect",
    )
    assert found.owner is Owner.WORKER
    assert "the unit and lint jobs failed." in found.sentence
    assert "the worker fixes it on a correction from Foundry" in found.sentence
    assert found.group == "waiting_on_foundry"
    flaky = _reason(state=_S.CI_CERTIFICATION_FAILED, failing_jobs=("e2e",), ci_cause="flaky_test")
    assert flaky.owner is Owner.FOUNDRY


def test_waiting_on_another_tasks_fix_names_the_task() -> None:
    found = _reason(
        state=_S.BLOCKED,
        question="Blocked until FDY-0599 merges its fix to the gate probe.",
        waiting_for="FDY-0599",
    )
    assert found.key == "waiting_on_task"
    assert found.sentence == (
        "This waits for FDY-0599 to land its fix. Foundry starts it again once FDY-0599 merges."
    )
    assert found.owner is Owner.FOUNDRY


def test_awaiting_internal_review_is_foundrys() -> None:
    found = _reason(state=_S.AWAITING_INTERNAL_REVIEW)
    assert found.key == "awaiting_internal_review"
    assert found.sentence == (
        "The work is finished and waits for Foundry's review before it is published."
    )
    assert found.owner is Owner.FOUNDRY


def test_an_escalation_addressed_to_the_operator_is_yours_with_answer() -> None:
    found = _reason(state=_S.BLOCKED, question=QUESTION, for_operator=True)
    assert found.key == "operator_question"
    assert found.owner is Owner.YOU
    assert found.quote == QUESTION
    assert found.clicks == ("answer", "send_back", "cancel")
    assert found.group == "waiting_on_me"
    assert found.as_dict()["owner_line"] == "Waiting on you."


@pytest.mark.parametrize(
    ("facts", "key"),
    [
        ({"state": _S.BLOCKED, "blocked_reason": "check_cannot_run"}, "check_cannot_run"),
        (
            {"state": _S.PRE_PR_GATES_FAILED, "failing_gates": ("no_secrets",)},
            "pre_pr_gates_failed",
        ),
        ({"state": _S.PUBLISH_FAILED}, "publish_failed"),
        ({"state": _S.HEAD_DIVERGED}, "head_diverged"),
        ({"state": _S.BLOCKED, "question": "Decide whether the merge stands."}, "foundry_question"),
        ({"state": _S.BLOCKED}, "blocked"),
    ],
)
def test_every_other_stuck_state_still_has_a_sentence_and_foundry_owns_it(
    facts: dict[str, Any], key: str
) -> None:
    found = _reason(**facts)
    assert found.key == key and found.owner is Owner.FOUNDRY
    assert found.sentence.endswith(".")


def test_work_that_is_not_stuck_has_no_reason() -> None:
    assert stuck_reason(StuckFacts(state=_S.RUNNING)) is None


def test_the_facts_come_from_the_tasks_events_and_escalation() -> None:
    blocked = task(1, _S.BLOCKED)
    events = {
        EventKind.TASK_RETRY_SCHEDULED.value: row(seq=5, payload={}),
        EventKind.HARNESS_REFUSED.value: row(seq=3, payload={}),
        EventKind.TASK_BLOCKED.value: row(seq=6, payload={"reason": "gate_proves_nothing"}),
    }
    escalation = row(question="Waits on FDY-0007's fix.", reason=None)
    facts = stuck_facts(blocked, escalation, events, other_tasks={"FDY-0007", "FDY-0001"})
    assert facts.blocked_reason == "gate_proves_nothing"
    # A refusal older than the last scheduling belongs to an earlier run.
    assert facts.harness_refused is False
    assert facts.waiting_for == "FDY-0007"
    assert failing_jobs({"failure": {"all": [{"check": "unit"}, {"check": "lint"}]}}) == (
        "unit",
        "lint",
    )


# ----- AC3: the Stuck lane splits by owner; needs_me counts only mine ---------------


def _two_stuck_tasks() -> tuple[Any, Any, Any]:
    uow, _calls = fixture(0)
    mine = task(1, _S.BLOCKED)
    foundrys = task(2, _S.BLOCKED)
    uow.tasks.rows = [mine, foundrys]
    uow.contracts = Repo(
        [row(task_id=item.id, version=1, document={}) for item in (mine, foundrys)]
    )
    uow.escalations = Repo(
        [
            Escalation(
                id="e1",
                task_id=mine.id,
                attempt_id=None,
                state=EscalationState.OPEN,
                opened_at=BOARD_NOW - timedelta(minutes=3),
                reason="design_question",
                question=QUESTION,
            ),
            row(
                id="e2",
                task_id=foundrys.id,
                state=EscalationState.OPEN,
                opened_at=BOARD_NOW - timedelta(minutes=4),
                kind=None,
                reason=None,
                question="V2 passes on the unchanged repo (add a check that fails).",
            ),
        ]
    )
    uow.events = Events(
        [
            row(
                seq=1,
                task_id=foundrys.id,
                kind=EventKind.TASK_BLOCKED.value,
                payload={"reason": "gate_proves_nothing"},
                ts=BOARD_NOW - timedelta(minutes=4),
            )
        ]
    )
    return uow, mine, foundrys


def _operator() -> Principal:
    return Principal(id="p-op", name="scott", role=Role.OPERATOR, created_at=BOARD_NOW)


def test_the_stuck_lane_groups_waiting_on_me_and_waiting_on_foundry() -> None:
    uow, mine, foundrys = _two_stuck_tasks()
    document = board_resource(uow, BOARD_NOW, _operator())
    stuck = next(lane for lane in document["lanes"] if lane["key"] == "stuck")
    groups = {group["key"]: group for group in stuck["groups"]}
    assert [group["name"] for group in stuck["groups"]] == ["Waiting on me", "Waiting on Foundry"]
    assert groups["waiting_on_me"]["card_ids"] == [mine.id]
    assert groups["waiting_on_foundry"]["card_ids"] == [foundrys.id]
    assert groups["waiting_on_me"]["count"] == 1 and groups["waiting_on_foundry"]["count"] == 1
    cards = {card["id"]: card for card in stuck["cards"]}
    assert cards[foundrys.id]["stuck"]["sentence"] == GATE_PROVES_NOTHING
    assert cards[foundrys.id]["waiting_on"] == "Foundry"
    assert [a["key"] for a in cards[foundrys.id]["actions"]] == ["send_back", "cancel"]
    assert [a["label"] for a in cards[foundrys.id]["actions"]] == [
        "Send back to Foundry",
        "Cancel",
    ]
    assert [a["key"] for a in cards[mine.id]["actions"]] == ["answer", "send_back"]


def test_needs_me_counts_only_what_waits_on_the_operator() -> None:
    uow, _mine, _foundrys = _two_stuck_tasks()
    document = board_resource(uow, BOARD_NOW, _operator())
    assert document["counts"]["stuck"] == 2
    assert document["needs_me"] == 1
    # The count holds when the Stuck lane's cards are not built.
    uow, _mine, _foundrys = _two_stuck_tasks()
    lazy = board_resource(uow, BOARD_NOW, _operator(), cards_for=frozenset({"wins"}))
    assert lazy["needs_me"] == 1


def test_the_board_page_shows_both_groups_and_glows_only_mine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uow, _mine, _foundrys = _two_stuck_tasks()
    document = board_resource(uow, BOARD_NOW, _operator())
    html = templates.get_template("board.html").render(
        request=Request({"type": "http", "method": "GET", "path": "/ui/board", "headers": []}),
        title="Board",
        active="/ui/board",
        nav=(),
        principal=None,
        csrf="tok",
        lanes=document["lanes"],
        needs_me=document["needs_me"],
    )
    stuck = html[html.index('data-lane="stuck"') : html.index('data-lane="in_progress"')]
    assert stuck.index("Waiting on me") < stuck.index("Waiting on Foundry")
    assert stuck.count("board-card--mine") == 1
    assert GATE_PROVES_NOTHING.split(",")[0] in stuck
    assert "Waiting on Foundry. Nothing for you to do." in stuck
    foundry_part = stuck[stuck.index('data-group="waiting_on_foundry"') :]
    assert "lat-btn--primary" not in foundry_part
    assert "<b>1</b> need me" in html


# ----- AC2: the card shows the sentence, the owner and only the clicks that apply ----


def _gate_card() -> Any:
    store, clock = store_for(_S.BLOCKED), FakeClock(NOW)
    probe = "V2 passes on the unchanged repo (add a check that fails, for example a new test file)"
    record_event(
        store.uow(),
        clock,
        EventKind.TASK_BLOCKED,
        principal="crucible",
        task_id=TASK_ID,
        payload={"reason": "gate_proves_nothing", "detail": probe},
    )
    store.escalations.add(
        Escalation(
            id="01ESC607000000000000000001",
            task_id=TASK_ID,
            attempt_id=None,
            state=EscalationState.OPEN,
            question=probe,
            opened_at=NOW - timedelta(hours=19),
        )
    )
    return store, probe


def test_a_foundry_card_shows_the_sentence_owner_and_only_send_back_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, probe = _gate_card()
    html = render_card_html(store, OPERATOR, monkeypatch)
    assert GATE_PROVES_NOTHING in html
    assert "Whose move: Foundry" in html
    assert "Waiting on Foundry. Nothing for you to do." in html
    assert 'data-action="send_back"' in html and 'data-action="cancel"' in html
    for absent in ("answer", "correct_remote", "correct_last"):
        assert f'data-action="{absent}"' not in html
    assert "Next phase:" not in html
    # The probe's sentence is shown once, under the one collapsed Details line.
    assert html.count("<summary>Details</summary>") == 1
    details_start = html.index("<summary>Details</summary>")
    details = html[details_start : html.index("</details>", details_start)]
    assert probe in details
    before = html[: html.index("<summary>Details</summary>")]
    assert probe not in before[before.index("Where it is and what it is stuck on") :]
    assert 'card-stuck--mine"' not in html


def test_an_operator_card_offers_the_answer_composer(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _probe = _gate_card()
    store.escalations.rows[-1].reason = "design_question"
    store.escalations.rows[-1].question = QUESTION
    html = render_card_html(store, OPERATOR, monkeypatch)
    assert "Whose move: You" in html and 'card-stuck--mine"' in html
    assert f'<blockquote class="stuck-quote">{QUESTION.replace("&", "&amp;")}' in html
    answer = html[html.index("stuck-click--answer") :]
    answer = answer[: answer.index("</form>")]
    assert "required" in answer and "lat-btn--primary" in answer
    assert 'value="answer"' in answer
    observer = render_card_html(store, OBSERVER, monkeypatch)
    assert 'data-action="answer"' not in observer


def test_send_back_wakes_the_orchestrator_with_the_escalation() -> None:
    store, probe = _gate_card()
    clock = FakeClock(NOW)
    result = apply_move(
        store.uow(),
        clock,
        principal=OPERATOR,
        task_id=TASK_ID,
        move="send_back",
        note_text="Please add a failing check.",
    )
    assert result.task.state is _S.BLOCKED
    [wake] = [w for w in store.wakes.rows if w.reason == WakeReason.SENT_BACK.value]
    assert wake.principal_id == result.task.principal_id
    assert wake.payload["escalation_id"] == "01ESC607000000000000000001"
    assert wake.payload["question"] == probe
    assert wake.payload["stuck_reason"] == "gate_proves_nothing"
    assert GATE_PROVES_NOTHING in wake.payload["summary"]
    assert "Please add a failing check." in wake.payload["summary"]


# ----- AC4: Workers and All tasks on the Neon cards and tables, no sideways scroll -----


def _page_request(path: str) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(ctx=None))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "app": app,
        }
    )


def _assert_phone_width(html: str) -> None:
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert "@media (max-width: 700px)" in html
    assert DASHES.isdisjoint(html)
    body = html[html.index("</style>") :]
    # No sideways-scrolling table wrapper; every table cell carries its column's label.
    assert "lat-table-scroll" not in body
    assert body.count("<td") == body.count("<td data-label=")


def test_the_workers_page_is_neon_cards_with_local_times(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "attempt_id": "01ATT607",
            "state": "running",
            "task_id": "t1",
            "external_id": "FDY-0613",
            "harness": "claude_code",
            "model": "claude-opus-5-5",
            "image_digest": "sha256:" + "0" * 64,
            "started_at": "2026-10-09T15:00:00+00:00",
            "last_heartbeat": "2026-10-09T15:05:00+00:00",
        }
    ]
    principal = Principal(id="p", name="scott", role=Role.OPERATOR, created_at=NOW)
    monkeypatch.setattr(ui_workers, "_require", lambda *_args: (principal, "tok"))
    monkeypatch.setattr(ui_workers, "status", SimpleNamespace(workers=lambda _uow: rows))
    uow = SimpleNamespace(
        tasks=SimpleNamespace(get=lambda _id: SimpleNamespace(title="Work pages"))
    )
    response = ui_workers.workers_page(
        _page_request("/ui/workers"), cast(Any, SimpleNamespace()), cast(Any, uow)
    )
    html = bytes(response.body).decode()
    _assert_phone_width(html)
    assert 'class="board-card work-card"' in html and "Work pages" in html
    assert 'href="/ui/workers/01ATT607/logs"' in html
    assert "2026-10-09 10:00:00 AM CDT" in html
    assert "<summary>Details</summary>" in html


def test_the_all_tasks_page_folds_tables_into_labelled_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, clock = proposal_store(), FakeClock(NOW)
    proposed = _propose(store, clock)
    batch = proposals_page.batch_section(store.uow(), PROPOSAL_OPERATOR)
    assert batch is not None
    sections = [
        batch,
        *proposals_page.proposal_sections(store.uow(), PROPOSAL_OPERATOR),
        {
            "title": "Recently updated",
            "columns": ["Task", "State", "Updated"],
            "rows": [
                [
                    {"kind": "link", "href": f"/ui/tasks/{proposed.id}", "label": "EX-0001"},
                    "Proposed",
                    "2026-10-09T15:00:00+00:00",
                ]
            ],
        },
    ]
    principal = Principal(id="p", name="scott", role=Role.OPERATOR, created_at=NOW)
    response = work_page(
        _page_request("/ui/tasks"),
        principal,
        "tok",
        active="/ui/tasks",
        heading="All tasks",
        intro="Every task.",
        sections=sections,
    )
    html = bytes(response.body).decode()
    _assert_phone_width(html)
    assert '<td data-label="Updated">2026-10-09 10:00:00 AM CDT</td>' in html
    assert 'data-label="Order"' in html
    assert "Importing a duplicate ID must fail with 409" in html
    assert html.count("<summary>Details</summary>") <= 1


@pytest.mark.parametrize("race", ["poll", "lookup"])
def test_production_decision_escalations_persist_operator_routing(
    tmp_path: Path, race: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, clock, supervisor, github, publisher = _correcting(tmp_path)
    if race == "poll":
        github.polled = _merged(OLD_HEAD)
        clock.advance(300)

        async def poll_during_push() -> object:
            return await supervisor.delivery.observe()

        publisher.during_push = poll_during_push
    else:
        github.lookups = [_open(OLD_HEAD), _merged(OLD_HEAD)]
    assert _publish(supervisor) == 0
    [escalation] = store.escalations.rows
    assert isinstance(escalation, Escalation)
    assert escalation.reason == "decision"
    assert is_operator_question(escalation)
    # Exercise the persisted discriminator on an active stuck card too.
    card_store = store_for(_S.BLOCKED)
    escalation.task_id = TASK_ID
    card_store.escalations.add(escalation)
    html = render_card_html(card_store, OPERATOR, monkeypatch)
    assert "Whose move: You" in html
    assert 'value="answer"' in html


@pytest.mark.parametrize(
    "schedule_kind", [EventKind.TASK_SCHEDULED, EventKind.TASK_RETRY_SCHEDULED]
)
@pytest.mark.parametrize("state", [_S.PRE_PR_GATES_FAILED, _S.CI_CERTIFICATION_FAILED])
def test_rescheduled_task_ignores_the_previous_blocker(
    schedule_kind: EventKind, state: TaskState
) -> None:
    facts = stuck_facts(
        task(1, state),
        None,
        {
            EventKind.TASK_BLOCKED.value: row(seq=1, payload={"reason": "gate_proves_nothing"}),
            schedule_kind.value: row(seq=2, payload={}),
        },
    )
    found = stuck_reason(facts)
    assert facts.blocked_reason is None
    assert found is not None and found.key == state.value


class _CIDecisions(Repo):
    def list_for_certifications(self, ids: list[str]) -> list[Any]:
        return [item for item in self.rows if item.ci_certification_id in ids]


@pytest.mark.parametrize(
    ("certification_id", "owner"),
    [("current-ci", Owner.WORKER), ("previous-ci", Owner.FOUNDRY), (None, Owner.FOUNDRY)],
)
@pytest.mark.parametrize("unrelated", [False, True])
def test_board_and_detail_use_only_the_current_ci_diagnosis(
    certification_id: str | None, owner: Owner, unrelated: bool
) -> None:
    uow, _calls = fixture(0)
    failed = task(1, _S.CI_CERTIFICATION_FAILED)
    uow.tasks.rows = [failed]
    uow.contracts = Repo([row(task_id=failed.id, version=1, document={})])
    event = row(
        task_id=failed.id,
        seq=8,
        ts=BOARD_NOW,
        kind=EventKind.CI_CERTIFICATION_RECORDED.value,
        payload={"certification_id": "current-ci", "failure": {"check": "unit"}},
    )
    uow.events = Events([event])
    uow.events.latest_for_task_kind = lambda _id, kind: event if kind == event.kind else None
    uow.ci_decisions = _CIDecisions(
        [
            row(
                id="d1",
                task_id=failed.id,
                ci_certification_id=certification_id,
                cause="implementation_defect",
                created_at=BOARD_NOW,
            ),
            # A newer decision for an unrelated certification must not replace the match.
            row(
                id="d2",
                task_id=failed.id,
                ci_certification_id="unrelated-ci",
                cause="flaky_test",
                created_at=BOARD_NOW + timedelta(seconds=1),
            ),
        ]
    )
    if not unrelated:
        uow.ci_decisions.rows.pop()
    detail = task_stuck_reason(uow, failed, None)
    assert detail is not None and detail.owner is owner
    document = board_resource(uow, BOARD_NOW, _operator())
    [card] = next(lane for lane in document["lanes"] if lane["key"] == "stuck")["cards"]
    assert card["stuck"] == detail.as_dict()
    assert "the unit job failed" in card["stuck"]["sentence"]
