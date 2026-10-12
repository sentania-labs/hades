# Hades board resource

`GET /v1/board` is the source for the Hades operator board and future clients. It is
available to authenticated orchestrator, operator, admin, and observer principals.
Observers receive the same cards with empty `actions` arrays.

The response has `schema_version`, a Central-time `generated_at`, `needs_me`, `counts`,
and seven `lanes` in this order: Inbox, Waiting on me, Stuck, In progress, Holding pen,
Wins, and Graveyard. Every lane has its total `count`. Cards contain the title, project,
linked issues, tier, harness and model, pull request, wait reason, lane age, and no more
than two applicable actions. An action states its `method`, `path`, request `body`, and
whether the client must confirm it with a second click. The two actions are chosen by
what the card's lane asks of the operator: a Waiting on me card always offers Answer
first, an In progress card awaiting acceptance offers Accept result first.

Pass one or more `lanes` query values to return cards only for those lanes while keeping
all seven lane counts. The web board uses this form to fetch Wins and Graveyard only
when the operator expands them. Unrequested lanes are counted without building their
cards, so the terminal lanes do not slow the live board as they grow.

Action calls use `POST /v1/board/{task_id}/actions/{action}`. The body is
`{"note": null}`; Decline may carry an optional note. Cancel and Decline have
`confirm: true`. Each accepted action records the authenticated principal and an
America/Chicago timestamp in the audit event. No action requires typed reason text.

The HTML front door is `/ui/board`. `/ui/tasks/{id}` is the matching card view, including
the objective, acceptance criteria, pull request and CI state, attempts, actions, and the
existing notes under the clearly named Thread section. When an attempt recorded a worker egress probe
(hades #425), the card also lists each allowlisted host and whether the worker reached it
before the harness started, as the earlier task page did.

The root `/` redirects to `/ui/board`, as does sign-in without an explicit return
destination. Sign-in honors `next=/ui`: that route leads to Set up (`/ui/setup`, the
ordered first-run steps) while a step is undone, and to the Board once every step is
done (hades #169). The Status page it used to be is folded into the Board's service
strip and the Admin About block on Settings (hades #214). A fresh installation can also open
`/ui/board` immediately, with seven empty lanes.

FDY-0585 correction note: compose smoke explicitly posts `next=/ui`, so its first-run
landing check uses the rendered `<h1>Set up</h1>` heading (`<h1>Status</h1>` before
hades #169) and then fetches `/ui/board`
separately to check `<h1>Board</h1>`. The regression test renders both pages through a
first-run session with incomplete setup. Docker is unavailable in the worker; this
route trace and unit test cover the correction locally, and the compose run stays in CI.
No application routing workaround was needed.

The card page's Actions panel keeps every operator action the earlier task page offered,
each as one click: the card's board moves (Cancel and Decline confirm with a second
click), the proposal answers for a proposed task (Approve with an optional note, Send back
with an optional note, Reject when the board offers no Decline), and, for an admin while a
pull request waits, the two ADR 0025 waivers (Waive the remaining external review rounds,
Accept that this repository has no CI). The waivers post to the same
`POST /ui/tasks/{id}/decisions` handler and record the same decision kinds; a blank reason
records what the waiver resolves, with the operator and the time. Recorded waivers are
listed under Operator waivers in the panel.

## The card and the board lead with what needs you (hades FDY-0629)

Every card (`/ui/tasks/{id}`, the same page as `/ui/board/{id}`) opens with a What needs
you section above the title block, in exactly one of three states, decided by one
mapping in `crucible/domain/needs_you.py` (unit tests in
`tests/unit/test_card_needs_you.py`):

| State | Opens with | When |
|---|---|---|
| Nothing from you | one plain sentence of what is happening now and one of what happens next, from a table of phrases keyed by the task's state, its stuck reason and its current correction (a running correction for Codex findings reads "The worker is fixing 3 review findings. Next: Codex re-reviews, then merge.") | no decision or evidence is asked of the operator |
| Your decision: | the open escalation's question as the worker wrote it, the options it listed as bullet or numbered lines, and an answer form (one text field, Send) that posts `kind=escalation_answer` to `POST /ui/tasks/{id}/decisions`, which takes the board's Answer move: a worker's question gets a correction carrying the answer, any other escalation an `escalation_answer` decision | an open escalation addressed to the operator (`decision`, `design`, `design_question`, `decision_question`); a proposal waiting for approval opens the same way with the Approve, Send back and Decline clicks instead of the form |
| Waiting on you for: | the contract's `operator_needs` lines | the list is not empty and the task is blocked or submitted |

No state name, gate name, hash, command or id appears above the fold. Everything the
card showed before (where it is and what it is stuck on, the head and CI, the cost, the
contract, the attempts and corrections, the egress probe, the thread and notes, the other
actions, the history) sits below it in collapsed sections, closed by default, under plain
headings. The supervisor health banner is unchanged (hades #634 is separate).

`/ui/board` has four lanes, in this order, from the same mapping: Needs you (only the
cards in the decision or evidence state, each with its one-line ask and an Answer or
Open link to the card), Working (nothing needed: a worker, Hades or Foundry is on it,
including work stopped on something Foundry owns), Waiting on review or CI (nothing
needed), and Done today (work that reached a terminal state today, newest first; older
finished work is on All tasks). The count at the top is the Needs you lane's. The
projection builds the Wins and Graveyard cards only for tasks updated since local
midnight (`finished_since`), so Done today costs today's tasks and no more. The JSON
resource `GET /v1/board` keeps its seven lanes.

## Stuck cards say who acts (hades #607)

Every reason a task waits maps to one plain sentence and an owner in
`crucible/domain/stuck_reasons.py`, with a unit test per reason in
`tests/unit/test_stuck_reasons.py`. The owner is you (the operator), Foundry, or the
worker on a correction from Foundry:

| Reason | Owner | Sentence |
|---|---|---|
| `gate_proves_nothing` | Foundry | The checks already pass before any change, so a run could not prove anything. Foundry adds a check that fails first. |
| `check_cannot_run` | Foundry | A required check cannot run in the worker, so no run could pass it. |
| `ambiguous_contract`, `missing_capability` | Foundry | The worker stopped; its question is shown as a quote. |
| wrong harness (a refused launch, `too_big_for_local`, a worker naming the wrong harness) | Foundry | The task went to a harness that cannot run it. |
| `ci_certification_failed` | Foundry, or the worker when Foundry diagnosed the code | Names the failing jobs in words. |
| waiting on another task's fix | Foundry | Names the task. |
| `awaiting_internal_review` | Foundry | Waits for Foundry's review. |
| an escalation addressed to the operator (`decision`, `design`, `design_question`, `decision_question`) | You | Quotes the question and offers the Answer composer. |

A card with a reason, on the board and on the card page, shows the sentence, the owner
and only the clicks that apply: Answer when the owner is you, Send back to Foundry, and
Cancel (the board card keeps its first two). Send back to Foundry
(`POST /v1/board/{task_id}/actions/send_back`) wakes the task's orchestrator with a
`sent_back` wake carrying the open escalation, the reason and the note; the task does not
move. The raw gate and escalation text sits under one collapsed Details line on the card
page.

A stuck task stays in the Stuck lane whoever owns it. The lane carries `groups`, Waiting
on me then Waiting on Foundry, each with its `count` and the `card_ids` of the lane's
cards in it; cards carry `stuck` (the reason's key, sentence, owner, quote, group and
clicks). `needs_me` counts the Waiting on me lane plus the Stuck lane's Waiting on me
group, never a card that waits on Foundry. A worker's `ambiguous_contract` is Foundry's to
answer, so it no longer puts a card in Waiting on me; that lane holds the operator's
questions on work that is not stuck.

## Workers and All tasks (hades #576 U6)

`/ui/workers` shows one Neon card per running attempt (task, state, harness, model,
start and heartbeat in Central time, its log, and the identifiers under Details).
`/ui/tasks` (All tasks) renders the same sections as before through `work.html`, whose
tables fold into labelled rows below 700 px, so neither page scrolls sideways on a phone.

Stuck ownership reads the escalation's persisted `reason`, including `decision`,
`design`, `design_question` and `decision_question` for operator questions. Callers
asking the operator to decide a branch or merge conflict record `decision`; a missing
reason and worker blockers remain Foundry's. No ownership is inferred from question
wording. Existing unclassified escalations remain with Foundry because their intended
audience was not recorded.

Both the board and card use only the CI diagnosis whose `ci_certification_id` matches
the latest `ci_certification_recorded` event's `certification_id`. An undiagnosed new
certification waits on Foundry, even if a previous head needed a worker correction.
The board loads these diagnoses in one query. A blocked event older than the latest
schedule or retry belongs to the previous attempt and cannot hide a current failure.
