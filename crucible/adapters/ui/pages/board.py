from __future__ import annotations

from typing import Any, cast
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.pages.card_thread import card_thread_context
from crucible.adapters.ui.render import _base, _localize, _redirect, _state_words, templates
from crucible.adapters.ui.session import _csrf, _form, _require
from crucible.application.admin.board_card import board_card_view
from crucible.application.admin.board_lanes import board_lanes_view
from crucible.application.board_actions import CorrectionDeps, apply_move, next_phase
from crucible.application.board_resource import board_resource
from crucible.application.errors import ApplicationError, NotFoundError
from crucible.application.proposals import reject_proposal
from crucible.application.queries import task_view
from crucible.application.task_notes import OPERATOR_ROLES, add_note
from crucible.domain.egress_probe import host_words
from crucible.domain.entities import Principal, Role
from crucible.domain.lifecycle import TaskState
from crucible.domain.waivers import ACCEPT_NO_CI, WAIVABLE_STATES, WAIVE_EXTERNAL_REVIEW

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


def _link(href: str, label: str) -> dict[str, str]:
    return {"kind": "link", "href": href, "label": label}


def _issues(items: list[dict[str, str]]) -> list[Any]:
    return [_link(item["url"], item["label"]) if item["url"] else item["label"] for item in items]


def _in_flight_sections(document: dict[str, Any]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for group in document["in_flight"]:
        rows = []
        for parent in group["parents"]:
            for item in parent["tasks"]:
                pr = item["pull_request"]
                pr_cell: Any = "none"
                if pr:
                    label = f"#{pr['number']} · CI {pr['ci']['label']}"
                    # hades #356: the operator's diagnosis for a CI failure, beside the
                    # red/green state, in the label so it shows whether or not the pull
                    # request has a URL yet.
                    if pr["cause"]:
                        label = f"{label} · cause: {pr['cause'].replace('_', ' ')}"
                    hint = (
                        f"Merge queue position {pr['merge_queue_position']}"
                        if pr["merge_queue_position"] is not None
                        else "Merge queue position not recorded"
                    )
                    pr_cell = {"kind": "note", "value": label, "hint": hint}
                    if pr["url"]:
                        pr_cell = _link(pr["url"], label)
                rows.append(
                    [
                        parent["parent_external_id"],
                        _link(f"/ui/tasks/{quote(item['id'])}", item["external_id"] or item["id"]),
                        item["title"],
                        _issues(item["issues"]),
                        (
                            f"{item['harness'] or 'none'} / {item['model'] or 'none'} / "
                            f"{item['pool'] or 'none'}"
                        ),
                        {
                            "kind": "note",
                            "value": _state_words(item["state"]),
                            "hint": f"since {item['state_since'].isoformat()}",
                        },
                        item["waiting_on"],
                        pr_cell,
                        item["corrections"],
                        item["eta"]["label"],
                    ]
                )
        sections.append(
            {
                "title": group["name"],
                "empty": "No tasks are waiting here.",
                "columns": [
                    "Parent",
                    "Task",
                    "Title",
                    "Closes",
                    "Harness / model / pool",
                    "State",
                    "Waiting on",
                    "Pull request",
                    "Corrections",
                    "Elapsed / timeout",
                ],
                "rows": rows,
            }
        )
    return sections


def _kanban_columns(document: dict[str, Any]) -> list[dict[str, Any]]:
    """hades #334: one column per stage, one card per task, linked to its task page."""
    columns = []
    for column in document["kanban"]["columns"]:
        parents = []
        count = 0
        for group in column["parents"]:
            cards = [{**card, "href": f"/ui/tasks/{quote(card['id'])}"} for card in group["tasks"]]
            count += len(cards)
            parents.append(
                {
                    "parent_external_id": group["parent_external_id"],
                    "href": (
                        f"/ui/tasks/{quote(group['parent_task_id'])}"
                        if group["parent_task_id"]
                        else None
                    ),
                    "tasks": cards,
                }
            )
        columns.append(
            {
                "key": column["key"],
                "name": column["name"],
                "reserved": column["reserved"],
                "note": column["note"],
                "count": count,
                "empty": "No tasks here.",
                "parents": parents,
            }
        )
    return columns


def _kanban_section(document: dict[str, Any]) -> dict[str, Any]:
    thresholds = document["kanban"]["thresholds"]
    return {
        "title": "In flight",
        "note": (
            "One card per task, in the column for its state, with who holds it and for "
            "how long. Hades moves cards as states change; nothing is dragged. A card "
            f"colours after {thresholds['attention_minutes']} minutes in Awaiting Foundry "
            "or Awaiting Codex, and after the policy's CI budget in Awaiting CI. Columns "
            "scroll sideways."
        ),
        "kanban": _kanban_columns(document),
    }


def _list_section(document: dict[str, Any]) -> dict[str, Any]:
    """The Board's earlier list, every task as a table row, still reachable under the
    kanban (hades #334)."""
    return {
        "title": "Task list",
        "note": "The same tasks as tables, grouped by what they wait on and their parent.",
        "details_label": "Show the task list",
        "details": _in_flight_sections(document),
    }


def _routing_section(document: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for item in document["routing"]:
        candidates = [
            f"{candidate['order']}. {candidate.get('model', 'unknown')} on "
            f"{candidate.get('harness', 'unknown')} / {candidate.get('pool', 'unknown')}: "
            f"{candidate['reason']}"
            for candidate in item["candidates"]
        ]
        rows.append([item["external_id"], candidates or ["No candidates recorded"]])
    return {
        "title": "Routing",
        "empty": "No current attempts.",
        "columns": ["Task", "Ordered candidates and choice"],
        "rows": rows,
    }


def _tokens_sections(document: dict[str, Any]) -> list[dict[str, Any]]:
    tokens = document["tokens"]
    return [
        {
            "title": "Tokens by harness, model and pool",
            "note": (
                "A harness that does not report usage is shown as not recorded. "
                "No usage is estimated."
            ),
            "empty": "No attempt usage recorded.",
            "columns": [
                "Harness",
                "Model",
                "Pool",
                "Input",
                "Output",
                "Recorded attempts",
                "Not recorded",
            ],
            "rows": [
                [
                    r["harness"],
                    r["model"],
                    r["pool"],
                    r["tokens_in"],
                    r["tokens_out"],
                    r["recorded_attempts"],
                    r["unrecorded_attempts"],
                ]
                for r in tokens["totals"]
            ],
        },
        {
            "title": "Tokens by attempt",
            "empty": "No attempts yet.",
            "columns": ["Attempt", "Harness", "Model", "Pool", "Input", "Output", "Recording"],
            "rows": [
                [
                    r["attempt_id"],
                    r["harness"],
                    r["model"],
                    r["pool"],
                    r["tokens_in"] if r["tokens_in"] is not None else "not recorded",
                    r["tokens_out"] if r["tokens_out"] is not None else "not recorded",
                    r["recording"],
                ]
                for r in tokens["attempts"]
            ],
        },
    ]


def _quality_sections(document: dict[str, Any]) -> list[dict[str, Any]]:
    quality = document["quality"]
    totals = quality["totals"]
    tasks = quality["tasks"]
    return [
        {
            "title": "Quality totals by harness and model",
            "note": f"Tasks that reached a pull request in the last {quality['days']} days.",
            "empty": "No pull requests in this period.",
            "columns": [
                "Harness",
                "Model",
                "Tasks",
                "Failed gates",
                "Findings",
                "Corrections",
                "Merged",
                "Cancelled",
                "Open",
            ],
            "rows": [
                [
                    r["harness"],
                    r["model"],
                    r["tasks"],
                    r["failed_gates"],
                    r["findings"],
                    r["corrections"],
                    r["merged"],
                    r["cancelled"],
                    r["open"],
                ]
                for r in totals
            ],
        },
        {
            "title": "Quality log",
            "empty": "No pull requests in this period.",
            "columns": [
                "Task",
                "Harness",
                "Model",
                "Failed pre-PR gates",
                "Codex findings",
                "Corrections",
                "Outcome",
                "Submit to merge",
            ],
            "rows": [
                [
                    _link(f"/ui/tasks/{quote(r['task_id'])}", r["external_id"]),
                    r["harness"],
                    r["model"],
                    r["failed_gates"],
                    r["findings"],
                    r["corrections"],
                    r["outcome"],
                    f"{r['submit_to_merge_seconds']}s"
                    if r["submit_to_merge_seconds"] is not None
                    else "not merged",
                ]
                for r in tasks
            ],
        },
    ]


@router.get("/board", response_class=HTMLResponse)
def board_page(request: Request, ctx: Ctx, uow: UoW, lane: str | None = None) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    if lane is not None:
        return JSONResponse(
            board_resource(uow, ctx.clock.now(), principal, cards_for=frozenset({lane}))
        )
    render_principal = cast("Principal | None", principal)
    if render_principal is None:  # Compatibility for projection-only unit fixtures.
        document = board_lanes_view(uow, ctx.clock.now())
        document["needs_me"] = 0
    else:
        document = board_resource(
            uow,
            ctx.clock.now(),
            render_principal,
            cards_for=frozenset({"inbox", "waiting_on_me", "stuck", "in_progress", "holding_pen"}),
        )
    return templates.TemplateResponse(
        request=request,
        name="board.html",
        context={
            **_base(request, principal, csrf, title="Board", active="/ui/board"),
            "title": "Board",
            "lanes": document["lanes"],
            "needs_me": document["needs_me"],
        },
    )


def _timezone(request: Request) -> str:
    settings = getattr(getattr(request.app.state, "ctx", None), "settings", None)
    return str(settings.service.render_timezone) if settings is not None else "America/Chicago"


def egress_rows(view: Any) -> list[dict[str, Any]]:
    """hades #425: one row per attempt per probed host; each row carries attempt id,
    host, the result words, ms (latency), detail, and recorded_at (probe recording
    time, localized by the template context)."""
    rows: list[dict[str, Any]] = []
    for execution in view.executions:
        for attempt in execution.attempts:
            probe = attempt.egress_probe
            if not probe:
                continue
            hosts = probe.get("hosts") or []
            recorded_at = probe.get("recorded_at")
            if probe.get("rejected"):
                # The worker's first marker line was not the wrapper's shape: nothing
                # was read from it, and that is what the row says.
                rows.append(
                    {
                        "attempt_id": attempt.id,
                        "host": "none",
                        "result": f"probe line rejected: {probe['rejected']}",
                        "ms": None,
                        "detail": "",
                        "recorded_at": recorded_at,
                    }
                )
                continue
            if not hosts:
                rows.append(
                    {
                        "attempt_id": attempt.id,
                        "host": "none",
                        "result": "no allowlisted host to probe",
                        "ms": None,
                        "detail": "",
                        "recorded_at": recorded_at,
                    }
                )
                continue
            for row in hosts:
                host = str(row.get("host", ""))
                ms = row.get("ms")
                detail = str(row.get("detail") or "")
                words = host_words(row)
                rows.append(
                    {
                        "attempt_id": attempt.id,
                        "host": host,
                        "result": words.removeprefix(f"{host} "),
                        "ms": ms,
                        "detail": detail,
                        "recorded_at": recorded_at,
                    }
                )
    return rows


# The operator's waivers (ADR 0025), offered on the card page while a PR waits on them.
WAIVER_FORMS = (
    (
        WAIVE_EXTERNAL_REVIEW,
        "Waive the remaining external review rounds",
        "The task stops waiting for the external reviewer and goes on to CI. Use it when "
        "the reviewer will not review this pull request.",
        "the external reviewer did not review this pull request",
    ),
    (
        ACCEPT_NO_CI,
        "Accept that this repository has no CI",
        "With no check run or workflow run on the head, CI certification is skipped "
        "instead of waiting. A check that does run is still certified.",
        "this repository has no CI for this task",
    ),
)

REMOVE_LIKE_MOVES = frozenset({"cancel", "decline"})

# hades #424: the proposal answers the earlier task page offered, as (key, label, note
# field label, confirm with a second click).
PROPOSAL_ANSWERS = (
    ("approve", "Approve with note", "Note added to the objective (optional)", False),
    ("send_back", "Send back", "Note to the orchestrator (optional)", False),
    ("reject", "Reject", None, True),
)

CARD_CLICK_REASON = "answered with one click on the card page"

# hades #607: the words field beside a stuck card's clicks. Answer is the operator's
# reply and needs words; the others take an optional note.
MOVE_NOTES = {
    "answer": "Your answer (the worker or Foundry reads it as written)",
    "send_back": "Note to Foundry (optional)",
}


def card_page_actions(
    task_id: str,
    state: TaskState,
    role: Role,
    moves: list[dict[str, str]],
    default_move: str | None = None,
) -> list[dict[str, Any]]:
    """Every action the card page offers, each one click. A note field, where there is
    one, is optional: the handler records the operator and the time either way."""
    if role not in OPERATOR_ROLES:
        return []
    quoted = quote(task_id)
    actions: list[dict[str, Any]] = [
        {
            "key": move["key"],
            "label": move["label"],
            "hint": move.get("meaning", ""),
            "action": f"/ui/board/{quoted}/actions",
            "hidden": {"move": move["key"], "apply": "go"},
            "note": MOVE_NOTES.get(
                move["key"], "Note (optional)" if move["key"] in REMOVE_LIKE_MOVES else None
            ),
            "note_name": "note",
            "required": move["key"] == "answer",
            "primary": move["key"] == default_move,
            "confirm": move["key"] in REMOVE_LIKE_MOVES,
        }
        for move in moves
    ]
    keys = {move["key"] for move in moves}
    if state is TaskState.PROPOSED:
        for key, label, note, confirm in PROPOSAL_ANSWERS:
            # The board's own Decline already rejects.
            if key == "reject" and "decline" in keys:
                continue
            actions.append(
                {
                    "key": key,
                    "label": label,
                    "hint": "",
                    "action": f"/ui/tasks/{quoted}/proposal",
                    "hidden": {"proposal_action": key, "reason": CARD_CLICK_REASON},
                    "note": note,
                    "note_name": "note",
                    "primary": key == "approve" and "approve" not in keys,
                    "confirm": confirm,
                }
            )
    if role is Role.ADMIN and state in WAIVABLE_STATES:
        for kind, title, hint, resolves in WAIVER_FORMS:
            actions.append(
                {
                    "key": kind,
                    "label": title,
                    "hint": hint,
                    "action": f"/ui/tasks/{quoted}/decisions",
                    "hidden": {"kind": kind, "resolves": resolves},
                    "note": "Reason (optional; recorded on the task)",
                    "note_name": "verbatim",
                    "primary": False,
                    "confirm": False,
                }
            )
    return actions


@router.get("/board/{task_id}", response_class=HTMLResponse)
def board_card_page(request: Request, task_id: str, ctx: Ctx, uow: UoW) -> Response:
    """hades #489: the opened card. Operators get the note and the phase actions;
    observers read the same card without the forms."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    try:
        document = board_card_view(uow, task_id, ctx.clock.now())
    except NotFoundError:
        return RedirectResponse(
            f"/ui/board?kind=bad&message={quote(f'No task {task_id}.')}", status_code=303
        )
    try:
        window = int(request.query_params.get("window", "50"))
    except ValueError:
        window = 50
    timezone = _timezone(request)
    card = _localize(document, timezone)
    view = task_view(uow, task_id)
    actions = card_page_actions(
        task_id, view.state, principal.role, document["moves"], document["default_move"]
    )
    # hades #607: a card with a stuck reason offers only that reason's clicks, beside
    # its sentence; no other phase move is offered in the panel.
    reason_actions: list[dict[str, Any]] = []
    if document["reason"] is not None:
        move_keys = {move["key"] for move in document["moves"]}
        by_key = {item["key"]: item for item in actions}
        reason_actions = [
            {**by_key[key], "primary": key == "answer"}
            for key in document["reason"]["clicks"]
            if key in by_key
        ]
        actions = [item for item in actions if item["key"] not in move_keys]
    waivers = _localize(
        [d for d in view.decisions if d.get("kind") in (WAIVE_EXTERNAL_REVIEW, ACCEPT_NO_CI)],
        timezone,
    )
    return templates.TemplateResponse(
        request=request,
        name="card.html",
        context={
            **_base(
                request,
                principal,
                csrf,
                title=f"Card {document['external_id']}",
                active="/ui/board",
            ),
            **card_thread_context(
                ctx,
                uow,
                principal,
                card,
                timezone=timezone,
                window=window,
                questions=view.questions,
                handoffs=view.handoffs,
            ),
            "card": card,
            "can_act": principal.role in OPERATOR_ROLES,
            "actions": actions,
            "reason_actions": reason_actions,
            "waivers": waivers,
            "egress": egress_rows(view),
            "return_to": f"/ui/board/{quote(task_id)}",
        },
    )


@router.post("/board/{task_id}/notes")
async def board_card_note(request: Request, task_id: str, ctx: Ctx, uow: UoW) -> Response:
    """An operator's note on the card, stored as typed (hades #489)."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    form["return_to"] = f"/ui/board/{quote(task_id)}"
    try:
        _csrf(form, csrf)
        add_note(uow, ctx.clock, principal=principal, task_id=task_id, text=form.get("note", ""))
        uow.commit()
        return _redirect(form, "Note recorded.")
    except ApplicationError as exc:
        return _redirect(form, exc.detail, kind="bad")


@router.post("/board/{task_id}/actions")
async def board_card_action(request: Request, task_id: str, ctx: Ctx, uow: UoW) -> Response:
    """Go applies the chosen move with the note; Next phase applies the lane's default
    move with the note. Either records the note's words as the decision's verbatim."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    form["return_to"] = f"/ui/board/{quote(task_id)}"
    try:
        _csrf(form, csrf)
        deps = CorrectionDeps(
            harnesses=getattr(ctx, "harnesses", None),
            harness_gates=dict(getattr(ctx, "harness_gates", {}) or {}),
            credential_sources=dict(getattr(ctx, "credential_sources", {}) or {}),
            secret_providers=getattr(ctx, "secret_providers", frozenset()),
            wired_providers=frozenset(
                provider.name for provider in getattr(ctx, "providers", []) or []
            ),
        )
        note = form.get("note", "")
        if not note.strip():
            note = f"{form.get('move') or 'next phase'} by {principal.name}"
        if form.get("move") == "decline":
            task = reject_proposal(
                uow,
                ctx.clock,
                principal=principal,
                task_id=task_id,
                reason=note,
            )
            uow.commit()
            return _redirect(form, f"Declined {task.external_id}.")
        if form.get("apply") == "next":
            result = next_phase(
                uow, ctx.clock, principal=principal, task_id=task_id, note_text=note, deps=deps
            )
        else:
            result = apply_move(
                uow,
                ctx.clock,
                principal=principal,
                task_id=task_id,
                move=form.get("move", ""),
                note_text=note,
                deps=deps,
            )
        uow.commit()
        return _redirect(form, f"{result.move.label}: {result.message}")
    except ApplicationError as exc:
        return _redirect(form, exc.detail, kind="bad")
