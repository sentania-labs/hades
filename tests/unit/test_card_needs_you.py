"""hades FDY-0629: the card opens with what the operator must do, in plain words, and the
board's first lane is Needs you.

Three card states (nothing from you, your decision, waiting on you for), rendered from
fixture tasks through the card page; above the fold nothing names a state, a gate, a
hash, a command or an id. The board's four lanes come from the same mapping, and only
a decision or an operator evidence need puts a card in Needs you."""

from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from crucible.adapters.api.deps import app_context, unit_of_work
from crucible.adapters.ui.pages import board as board_page
from crucible.adapters.ui.pages import tasks as tasks_page
from crucible.application.admin.board_card import board_card_view
from crucible.application.admin.board_lanes import board_lanes_view
from crucible.contracts.common import to_document
from crucible.contracts.task_contract import TaskContractV1, contract_sha256, correction_narrows
from crucible.domain.entities import Escalation, EscalationState, Role, TaskContract
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.domain.needs_you import (
    BOARD_LANES,
    DECISION,
    EVIDENCE,
    LANE_DONE,
    LANE_NEEDS_YOU,
    LANE_WAITING,
    LANE_WORKING,
    NOTHING,
    NeedsFacts,
    lane_for,
    needs_you,
    split_options,
)
from crucible.settings import ServiceSettings, Settings
from tests.fixtures import FakeClock, contract_document
from tests.unit.admin_ui_fixtures import request as make_request
from tests.unit.test_board import NOW as BOARD_NOW
from tests.unit.test_board import Repo, row
from tests.unit.test_issue_360_ready_for_merge_correction import NOW as CARD_NOW
from tests.unit.test_issue_489_board_lanes import fixture, task
from tests.unit.test_issue_489_card_actions import (
    OBSERVER,
    OPERATOR,
    TASK_ID,
    CardStore,
    _document,
    render_card_html,
    store_for,
)

_S = TaskState

QUESTION = (
    "Which API shape do you want for the ScarGuard export?\n"
    "1. One endpoint returning the whole bundle.\n"
    "2. One endpoint per record kind.\n"
    "Either works; the second costs a day more."
)
NEEDS = ["the ScarGuard staging credentials", "a decision on the export API shape"]

# What must not appear above the fold: a state name, a gate name, a 40-hex hash, a
# command, or an id (a ULID).
STATE_NAMES = [state.value for state in TaskState if "_" in state.value] + [
    state.value for state in AttemptState if "_" in state.value
]
GATE_NAMES = [
    "report_present",
    "exit_clean",
    "commits_present",
    "scope_contained",
    "no_injected_files",
    "no_secrets",
    "verification_ran",
    "run_evidence_present",
    "criteria_mapped",
    "dependencies_unchanged",
    "ci_unchanged",
    "workspace_clean",
    "internal_review_recorded",
    "editor_leftovers",
    "acceptance_checks",
    "commit_policy",
]
FORBIDDEN = [*STATE_NAMES, *GATE_NAMES, "uv run", "make "]
HASH = re.compile(r"\b[0-9a-f]{40}\b")
ULID = re.compile(r"\b01[0-9A-Z]{24}\b")


# ----- the mapping ------------------------------------------------------------------


def test_a_running_correction_says_what_the_worker_is_fixing_and_what_comes_next() -> None:
    needs = needs_you(NeedsFacts(_S.RUNNING, correction_reason="external_review", findings=3))
    assert needs.kind == NOTHING
    assert needs.label == "Nothing from you."
    assert needs.now == "The worker is fixing 3 review findings."
    assert needs.next == "Next: Codex re-reviews, then merge."
    assert lane_for(_S.RUNNING, needs) == LANE_WORKING


@pytest.mark.parametrize("state", list(TaskState), ids=lambda s: s.value)
def test_every_state_has_plain_now_and_next_sentences_and_one_lane(state: TaskState) -> None:
    needs = needs_you(NeedsFacts(state))
    if state is _S.PROPOSED:
        assert needs.kind == DECISION and needs.proposal
        assert lane_for(state, needs) == LANE_NEEDS_YOU
        return
    assert needs.kind == NOTHING
    assert needs.now.endswith(".") and needs.next.startswith("Next:")
    lowered = f"{needs.now} {needs.next}".lower()
    for token in FORBIDDEN:
        assert token not in lowered, (state, token)
    assert lane_for(state, needs) in {LANE_WORKING, LANE_WAITING, LANE_DONE}


def test_an_operator_question_is_a_decision_with_its_options() -> None:
    needs = needs_you(NeedsFacts(_S.BLOCKED, question=QUESTION, for_operator=True))
    assert needs.kind == DECISION and needs.label == "Your decision:"
    assert needs.question == (
        "Which API shape do you want for the ScarGuard export?\n"
        "Either works; the second costs a day more."
    )
    assert needs.options == (
        "One endpoint returning the whole bundle.",
        "One endpoint per record kind.",
    )
    assert needs.ask == "Which API shape do you want for the ScarGuard export?"
    assert lane_for(_S.BLOCKED, needs) == LANE_NEEDS_YOU
    # A worker's blocker is Foundry's question, not the operator's.
    foundry = needs_you(NeedsFacts(_S.BLOCKED, question=QUESTION, stuck_key="ambiguous_contract"))
    assert foundry.kind == NOTHING
    assert foundry.now == "The worker stopped because the task reads two ways."
    assert split_options("reason: ambiguous_contract\nWhich one?") == ("Which one?", ())


@pytest.mark.parametrize("state", [_S.BLOCKED, _S.SUBMITTED], ids=lambda s: s.value)
def test_operator_needs_hold_a_blocked_or_submitted_task_on_evidence(state: TaskState) -> None:
    needs = needs_you(
        NeedsFacts(state, operator_needs=tuple(NEEDS), question="Q?", for_operator=True)
    )
    assert needs.kind == EVIDENCE and needs.label == "Waiting on you for:"
    assert needs.items == tuple(NEEDS)
    assert needs.ask == (
        "Waiting on you for: the ScarGuard staging credentials and a decision on the "
        "export API shape."
    )
    assert lane_for(state, needs) == LANE_NEEDS_YOU


def test_operator_needs_do_not_hold_a_task_in_another_state() -> None:
    needs = needs_you(NeedsFacts(_S.AWAITING_CI_CERTIFICATION, operator_needs=tuple(NEEDS)))
    assert needs.kind == NOTHING
    assert lane_for(_S.AWAITING_CI_CERTIFICATION, needs) == LANE_WAITING


def test_the_board_lanes_are_needs_you_working_waiting_and_done_today() -> None:
    assert [name for _key, name, _meaning in BOARD_LANES] == [
        "Needs you",
        "Working",
        "Waiting on review or CI",
        "Done today",
    ]


# ----- the contract field -----------------------------------------------------------


def test_the_contract_carries_optional_operator_needs() -> None:
    plain = TaskContractV1.model_validate(contract_document())
    assert plain.operator_needs == []
    assert "operator_needs" in to_document(plain)
    held = TaskContractV1.model_validate(contract_document(operator_needs=NEEDS))
    assert held.operator_needs == NEEDS
    # An amendment that adds or clears the list narrows nothing.
    assert correction_narrows(plain, held) == []
    assert correction_narrows(held, plain) == []
    with pytest.raises(ValueError, match="blank"):
        TaskContractV1.model_validate(contract_document(operator_needs=["ok", "  "]))
    with pytest.raises(ValueError, match="duplicate"):
        TaskContractV1.model_validate(contract_document(operator_needs=["same", "same"]))


# ----- the card page ----------------------------------------------------------------


def _above_fold(html: str) -> str:
    """The visible words from the page body to the first collapsed section."""
    body = html[html.index("<main") : html.index('<details class="card-fold"')]
    body = re.sub(r"<style>.*?</style>", " ", body, flags=re.DOTALL)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _assert_plain(fold: str) -> None:
    lowered = fold.lower()
    for token in FORBIDDEN:
        assert token not in lowered, token
    assert not HASH.search(fold), fold
    assert not ULID.search(fold), fold


def _correcting_store() -> CardStore:
    """A running correction for three Codex findings."""
    store = store_for(_S.RUNNING)
    correction = _document()
    correction["correction"] = {
        "of_version": 1,
        "reason": "external_review",
        "addresses": [
            {"kind": "review_comment", "id": str(number)} for number in (2101, 2102, 2103)
        ],
        "instructions": "Fix the three findings.",
        "resume_from": "remote_branch",
        "request_internal_review": False,
    }
    store.contracts.add(
        TaskContract(
            id="01CONTRACT629000000000002",
            task_id=TASK_ID,
            version=2,
            document=correction,
            sha256=contract_sha256(correction),
            submitted_at=CARD_NOW - timedelta(minutes=30),
        )
    )
    held = store.tasks.get(TASK_ID)
    assert held is not None
    held.contract_version = 2
    return store


def _deciding_store() -> CardStore:
    """A task blocked on a question Foundry addressed to the operator."""
    store = store_for(_S.BLOCKED)
    store.escalations.add(
        Escalation(
            id="01ESC629000000000000000001",
            task_id=TASK_ID,
            attempt_id="01ATTEMPT48900000000000001",
            state=EscalationState.OPEN,
            question=QUESTION,
            opened_at=CARD_NOW - timedelta(minutes=20),
            reason="decision",
        )
    )
    return store


def _held_store() -> CardStore:
    """A blocked task whose amended contract names what the operator must supply."""
    store = store_for(_S.BLOCKED)
    amended = to_document(TaskContractV1.model_validate(contract_document(operator_needs=NEEDS)))
    store.contracts.add(
        TaskContract(
            id="01CONTRACT629000000000003",
            task_id=TASK_ID,
            version=2,
            document=amended,
            sha256=contract_sha256(amended),
            submitted_at=CARD_NOW - timedelta(minutes=10),
        )
    )
    held = store.tasks.get(TASK_ID)
    assert held is not None
    held.contract_version = 2
    return store


def test_the_card_opens_with_nothing_from_you_for_a_running_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = render_card_html(_correcting_store(), OPERATOR, monkeypatch)
    fold = _above_fold(html)
    assert fold.index("What needs you") < fold.index("Nothing from you.") < fold.index("EX-0001")
    assert "The worker is fixing 3 review findings." in fold
    assert "Next: Codex re-reviews, then merge." in fold
    assert "Your decision:" not in fold and "Waiting on you for:" not in fold
    _assert_plain(fold)
    assert 'data-needs="nothing"' in html
    # Everything else is below the fold, closed by default, under plain headings.
    folds = re.findall(r'<details class="card-fold" data-fold="(\w+)"', html)
    assert folds == [
        "where",
        "head",
        "cost",
        "contract",
        "attempts",
        "thread",
        "actions",
        "history",
    ]
    assert "<details open" not in html and 'class="card-fold" open' not in html
    for heading in (
        "Where it is",
        "Branch and checks",
        "Cost so far",
        "What was asked",
        "Attempts",
        "Thread and notes",
        "Other actions",
        "History",
    ):
        assert f"<summary>{heading}</summary>" in html, heading


def test_the_card_opens_with_the_decision_and_an_answer_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = render_card_html(_deciding_store(), OPERATOR, monkeypatch)
    fold = _above_fold(html)
    assert fold.index("Your decision:") < fold.index("EX-0001")
    assert "Which API shape do you want for the ScarGuard export?" in fold
    assert "One endpoint returning the whole bundle." in fold
    assert "One endpoint per record kind." in fold
    assert "Either works; the second costs a day more." in fold
    assert "Nothing from you." not in fold
    _assert_plain(fold)
    above = html[: html.index('<details class="card-fold"')]
    assert f'action="/ui/tasks/{TASK_ID}/decisions"' in above
    assert 'name="kind" value="escalation_answer"' in above
    assert above.count('<textarea class="lat-textarea" name="verbatim"') == 1
    assert 'data-action="answer">Send</button>' in above
    reader = render_card_html(_deciding_store(), OBSERVER, monkeypatch)
    assert "Your decision:" in _above_fold(reader)
    assert 'name="verbatim"' not in reader[: reader.index('<details class="card-fold"')]


def test_the_card_opens_with_what_it_waits_on_you_for(monkeypatch: pytest.MonkeyPatch) -> None:
    html = render_card_html(_held_store(), OPERATOR, monkeypatch)
    fold = _above_fold(html)
    assert fold.index("Waiting on you for:") < fold.index("EX-0001")
    for item in NEEDS:
        assert item in fold
    assert "Nothing from you." not in fold and "Your decision:" not in fold
    _assert_plain(fold)
    card = board_card_view(_held_store().uow(), TASK_ID, CARD_NOW)
    assert card["needs"]["kind"] == EVIDENCE
    assert card["needs"]["items"] == NEEDS
    assert card["needs"]["board_lane"] == LANE_NEEDS_YOU


def test_the_answer_form_records_the_escalation_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _deciding_store()
    app = FastAPI()
    app.include_router(tasks_page.router)
    app.include_router(board_page.router)
    ctx = SimpleNamespace(
        clock=FakeClock(CARD_NOW),
        settings=Settings(service=ServiceSettings(render_timezone="America/Chicago")),
        providers=[SimpleNamespace(name="fake")],
        database_url="postgresql+psycopg://lab/crucible",
    )
    app.state.ctx = ctx
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: store
    monkeypatch.setattr(tasks_page, "_require", lambda request, ctx, uow: (OPERATOR, "tok"))
    monkeypatch.setattr(board_page, "_require", lambda request, ctx, uow: (OPERATOR, "tok"))
    with TestClient(app, follow_redirects=False) as client:
        page = client.get(f"/ui/tasks/{TASK_ID}")
        assert page.status_code == 200 and "Your decision:" in page.text
        sent = client.post(
            f"/ui/tasks/{TASK_ID}/decisions",
            data={"csrf": "tok", "kind": "escalation_answer", "verbatim": "The second."},
        )
    assert sent.status_code == 303, sent.text
    assert "Sent." in sent.headers["location"]
    escalation = store.escalations.get("01ESC629000000000000000001")
    assert escalation is not None and escalation.state is not EscalationState.OPEN
    applied = [
        e.payload for e in store.events.rows if e.kind == EventKind.TASK_PHASE_ACTION_APPLIED.value
    ]
    assert applied and applied[0]["move"] == "answer" and applied[0]["verbatim"] == "The second."


# ----- the board --------------------------------------------------------------------


def _board_fixture() -> Any:
    """Tasks across the lanes: a decision, an evidence hold, a Foundry blocker, work in
    flight, review and CI waits, a win today and an old win."""
    uow, _calls = fixture(0)
    tasks = [
        task(1, _S.BLOCKED),
        task(2, _S.BLOCKED),
        task(3, _S.BLOCKED),
        task(4, _S.RUNNING),
        task(5, _S.SUBMITTED),
        task(6, _S.AWAITING_EXTERNAL_REVIEW),
        task(7, _S.AWAITING_CI_CERTIFICATION),
        task(8, _S.MERGED),
        task(9, _S.MERGED),
        task(10, _S.CANCELLED),
    ]
    tasks[8].updated_at = BOARD_NOW - timedelta(days=3)
    uow.tasks.rows = tasks
    uow.contracts = Repo(
        [
            row(
                task_id=item.id,
                version=1,
                document={
                    "repository": {"url": "https://github.com/Foundry-SH/hades"},
                    "deliverables": [{"kind": "pull_request", "closes": ["#629"]}],
                    "execution": {"tier": "standard"},
                    **({"operator_needs": NEEDS} if item.id == "t2" else {}),
                },
            )
            for item in tasks
        ]
    )
    uow.escalations = Repo(
        [
            row(
                id="e1",
                task_id="t1",
                question=QUESTION,
                reason="decision",
                opened_at=BOARD_NOW - timedelta(minutes=9),
            ),
            row(
                id="e3",
                task_id="t3",
                question="reason: ambiguous_contract\nThe contract reads two ways.",
                reason="ambiguous_contract",
                opened_at=BOARD_NOW - timedelta(minutes=7),
            ),
        ]
    )
    return uow


def test_the_board_lanes_come_from_the_same_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    uow = _board_fixture()
    monkeypatch.setattr(board_page, "_require", lambda *_args: (None, "csrf"))
    ctx = SimpleNamespace(clock=SimpleNamespace(now=lambda: BOARD_NOW))
    req = make_request("/ui/board")
    req.scope["query_string"] = b""
    response = board_page.board_page(req, ctx, uow)  # type: ignore[arg-type]
    assert response.status_code == 200
    html = bytes(response.body).decode()
    lanes = re.findall(r'<section class="board-lane" data-lane="(\w+)">', html)
    assert lanes == [LANE_NEEDS_YOU, LANE_WORKING, LANE_WAITING, LANE_DONE]
    assert html.index("Needs you") < html.index("Working") < html.index("Waiting on review or CI")

    def cards(lane: str) -> list[str]:
        start = html.index(f'data-cards="{lane}"')
        end = html.index("</section>", start)
        return re.findall(r'href="/ui/tasks/(t\d+)"', html[start:end])

    # Oldest first: t2 waited longer than t1.
    assert cards(LANE_NEEDS_YOU) == ["t2", "t1"]
    assert cards(LANE_WORKING) == ["t5", "t4", "t3"]
    assert cards(LANE_WAITING) == ["t7", "t6"]
    # Done today newest first, and only what ended today (t9 ended three days ago).
    assert cards(LANE_DONE) == ["t8", "t10"]
    needs_you = html[html.index('data-cards="needs_you"') : html.index('data-cards="working"')]
    assert "Which API shape do you want for the ScarGuard export?" in needs_you
    assert "Your decision:" in needs_you and "Waiting on you for:" in needs_you
    for item in NEEDS:
        assert item in needs_you
    assert "The worker stopped because the task reads two ways." in html
    assert "<b>2</b> need you." in html
    _assert_plain(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", needs_you)))


def test_the_projection_reads_today_finished_cards_only(monkeypatch: pytest.MonkeyPatch) -> None:
    uow = _board_fixture()
    since = board_page.start_of_today(BOARD_NOW, "America/Chicago")
    document = board_lanes_view(uow, BOARD_NOW, finished_since=since)
    wins = {lane["key"]: lane for lane in document["lanes"]}["wins"]
    assert [card["id"] for card in wins["cards"]] == ["t8"]
    assert wins["count"] == 2
    needs = {card["id"]: card["needs"] for lane in document["lanes"] for card in lane["cards"]}
    assert needs["t1"]["kind"] == DECISION and needs["t2"]["kind"] == EVIDENCE
    assert needs["t3"]["kind"] == NOTHING and needs["t3"]["board_lane"] == LANE_WORKING
    assert needs["t8"]["board_lane"] == LANE_DONE
    assert Role.OPERATOR  # the lanes carry no role-specific words
