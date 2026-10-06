from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.pages.proposals import batch_section
from crucible.adapters.ui.render import _page, _state_words
from crucible.adapters.ui.session import _require
from crucible.application.admin.board import board_view

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
def board_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    document = board_view(uow, ctx.clock.now())
    return _page(
        request,
        principal,
        csrf,
        active="/ui/board",
        heading="Board",
        intro=(
            "Every task in flight as a card in the column for its stage, then the same "
            "tasks as a list, their routes, usage and recent quality evidence."
        ),
        sections=[
            _kanban_section(document),
            # hades #424: approve proposals in one action, in the order numbered.
            *([batch] if (batch := batch_section(uow, principal)) else []),
            _list_section(document),
            _routing_section(document),
            *_tokens_sections(document),
            *_quality_sections(document),
        ],
    )
