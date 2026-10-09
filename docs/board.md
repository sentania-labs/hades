# Hades board resource

`GET /v1/board` is the source for the Hades operator board and future clients. It is
available to authenticated orchestrator, operator, admin, and observer principals.
Observers receive the same cards with empty `actions` arrays.

The response has `schema_version`, a Central-time `generated_at`, `needs_me`, `counts`,
and seven `lanes` in this order: Inbox, Waiting on me, Stuck, In progress, Holding pen,
Wins, and Graveyard. Every lane has its total `count`. Cards contain the title, project,
linked issues, tier, harness and model, pull request, wait reason, lane age, and no more
than two applicable actions. An action states its `method`, `path`, request `body`, and
whether the client must confirm it with a second click.

Pass one or more `lanes` query values to return cards only for those lanes while keeping
all seven lane counts. The web board uses this form to fetch Wins and Graveyard only
when the operator expands them.

Action calls use `POST /v1/board/{task_id}/actions/{action}`. The body is
`{"note": null}`; Decline may carry an optional note. Cancel and Decline have
`confirm: true`. Each accepted action records the authenticated principal and an
America/Chicago timestamp in the audit event. No action requires typed reason text.

The HTML front door is `/ui/board`. `/ui/tasks/{id}` is the matching card view, including
the objective, acceptance criteria, pull request and CI state, attempts, actions, and the
existing notes under the clearly named Thread section.
