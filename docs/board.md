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
existing notes under the clearly named Thread section.

The root `/` redirects to `/ui/board`, as does sign-in without an explicit return
destination. Sign-in honors `next=/ui`: that route is the Status page and shows the
ordered setup steps while setup is incomplete. A fresh installation can also open
`/ui/board` immediately, with seven empty lanes.

FDY-0585 correction note: compose smoke explicitly posts `next=/ui`, so its first-run
landing check uses the rendered `<h1>Status</h1>` heading and then fetches `/ui/board`
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
