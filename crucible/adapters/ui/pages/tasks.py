from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.pages.proposals import (
    action_forms,
    batch_section,
    contract_rows,
    proposal_sections,
    proposed_tasks,
)
from crucible.adapters.ui.render import _page, _redirect, _state_words
from crucible.adapters.ui.session import _admin, _csrf, _form, _require
from crucible.application.admin import (
    bootstrap,
    status,
)
from crucible.application.artifacts import read_artifact
from crucible.application.decisions import record_decision
from crucible.application.errors import (
    ApplicationError,
    ConflictError,
    NotFoundError,
)
from crucible.application.queries import attempt_report, pull_request_view, task_view
from crucible.contracts.api import (
    DecisionRequest,
)
from crucible.contracts.evidence import REVIEW_DIFF_NAME, REVIEW_DIFF_TYPE
from crucible.domain.egress_probe import host_words
from crucible.domain.entities import Artifact, Role
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.lifecycle import TaskState
from crucible.domain.waivers import ACCEPT_NO_CI, WAIVABLE_STATES, WAIVE_EXTERNAL_REVIEW

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


def _gate_steps(gates: list[dict[str, Any]]) -> dict[str, Any]:
    tones = {"pass": "ok", "fail": "bad", "error": "bad", "skipped": "accent"}
    return {
        "kind": "steps",
        "items": [
            {
                "name": f"{g['gate']} ({g['classification']})",
                "result": g["result"],
                "detail": g["detail"] if g["result"] in ("fail", "error") else "",
                "tone": (
                    "warn"
                    if g["classification"] == "advisory" and g["result"] in ("fail", "error")
                    else tones.get(g["result"], "accent")
                ),
            }
            for g in gates
        ],
    }


def _effective_words(settings: dict[str, Any]) -> str:
    """Hades #388: what the harness was told about the model, in words."""
    context = settings.get("context_length")
    return (
        f"Launched with context length {context or 'found by Hermes'}, "
        f"max output tokens {settings.get('max_output_tokens')}, "
        f"thinking {'on' if settings.get('thinking') else 'off'}"
    )


def _egress_rows(view: Any) -> list[list[str]]:
    """hades #425: one row per attempt per probed host, `reachable` or why not; an
    attempt whose probe named no host, or whose probe line was rejected, says so in one
    row."""
    rows: list[list[str]] = []
    for execution in view.executions:
        for attempt in execution.attempts:
            probe = attempt.egress_probe
            if not probe:
                continue
            hosts = probe.get("hosts") or []
            if probe.get("rejected"):
                # The worker's first marker line was not the wrapper's shape: nothing
                # was read from it, and that is what the row says.
                rows.append([attempt.id, "none", f"probe line rejected: {probe['rejected']}"])
                continue
            if not hosts:
                rows.append([attempt.id, "none", "no allowlisted host to probe"])
                continue
            for row in hosts:
                words = host_words(row)
                host = str(row.get("host", ""))
                rows.append([attempt.id, host, words.removeprefix(f"{host} ")])
    return rows


def _busy_fallthrough(attempt: Any) -> str | None:
    if attempt is None or attempt.model is None:
        return None
    skipped = [
        candidate
        for candidate in attempt.ordered_candidates
        if candidate.get("busy") and candidate.get("model") != attempt.model
    ]
    if not skipped:
        return None
    choices = ", ".join(f"{candidate['model']} on {candidate['harness']}" for candidate in skipped)
    verb = "was" if len(skipped) == 1 else "were"
    return (
        f"Ran on {attempt.model} on {attempt.harness}, its next available choice, "
        f"because {choices} {verb} busy."
    )


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    document = status.tasks(uow)
    show_archived = request.query_params.get("archived") == "1"
    if show_archived:
        archived: set[str] = set()
    else:
        archived = {
            p.id
            for p in uow.principals.list_all()
            if p.disabled_at is not None and p.name.startswith(bootstrap.DISCARDED_PRINCIPAL_PREFIX)
        }

    # Counts stay in SQL. Fetch only archived IDs, across every state, so delivery,
    # publishing and gates receive the same filtering as the attention lists.
    if archived and not hasattr(uow.tasks, "count_by_state"):
        archived_tasks = [
            task
            for state in TaskState
            for task in uow.tasks.list_by_state(state)
            if task.principal_id in archived
        ]
        hidden = {task.id for task in archived_tasks}
        hidden_by_state: dict[str, int] = {}
        for task in archived_tasks:
            hidden_by_state[task.state.value] = hidden_by_state.get(task.state.value, 0) + 1
    else:
        hidden = set(uow.tasks.ids_for_principals(tuple(archived))) if archived else set()
        hidden_by_state = (
            {
                state.value: count
                for state, count in uow.tasks.count_by_state(principal_ids=tuple(archived)).items()
            }
            if archived
            else {}
        )

    # Filter attention list (items carry id or task_id)
    attention = [
        [_task_link(item["id"], item["external_id"]), _state_words(state), item["updated_at"]]
        for state, items in document["lists"].items()
        for item in items
        if item["id"] not in hidden
    ] + [
        # hades FDY-0133: a task in publishing whose publication cannot start says why.
        [
            _task_link(item["task_id"], item["external_id"]),
            f"Waiting to publish: {item['reason']}",
            item["waiting_since"],
        ]
        for item in document.get("publishing_waiting", [])
        if item["task_id"] not in hidden
    ]

    # One bounded query, newest first: the page shows RECENT_TASK_ROWS and reads no more.
    # Exclude archived principals from the query so the limit only applies to visible rows.
    recent = list(
        uow.tasks.recently_updated(
            since=ctx.clock.now() - timedelta(days=RECENT_TASK_DAYS),
            limit=RECENT_TASK_ROWS,
            exclude_principal_ids=archived or None,
        )
    )
    # Double-filter: in case of stale state, keep only non-hidden tasks.
    recent = [task for task in recent if task.id not in hidden]

    # hades FDY-0139: every task with a pull request under observation, each linking to
    # its page, where the operator can waive what the task is still waiting for.
    delivering = [
        [
            {"kind": "link", "href": f"/ui/tasks/{task.id}", "label": task.external_id or task.id},
            _state_words(task.state.value),
            task.updated_at.isoformat(),
        ]
        for state in DELIVERY_STATES
        for task in uow.tasks.list_by_state(state)
        if task.id not in hidden
    ]

    # Filter gates: drop rows whose item id is in hidden
    gates = [item for item in document.get("gates", []) if item["id"] not in hidden]

    # Subtract hidden counts from counts section; skip states that reach zero
    counts: dict[str, int] = {}
    for state, count in document["counts"].items():
        adjusted = count - hidden_by_state.get(state, 0)
        if adjusted > 0:
            counts[state] = adjusted

    # Determine if we need the hidden note
    hidden_total = len(hidden)
    hidden_note: str | None = None
    if hidden_total > 0:
        hidden_note = f"{hidden_total} archived import tasks hidden; add ?archived=1 to show them"
    return _page(
        request,
        principal,
        csrf,
        active="/ui/tasks",
        heading="Tasks",
        intro="Proposals waiting for your answer, tasks that need you, then every task by state.",
        sections=[
            # hades #424: what the orchestrator proposed, readable, with the answers.
            {
                "title": "Proposed",
                "note": (
                    "Contracts the orchestrator proposed. None starts until an operator "
                    "approves it. Each is shown in full below."
                ),
                "empty": "No task is waiting for approval.",
                "columns": ["Task", "Title", "Proposed"],
                "rows": [
                    [_task_link(task.id, task.external_id), task.title, task.created_at.isoformat()]
                    for task in proposed_tasks(uow)
                    if task.id not in hidden
                ],
            },
            *([batch] if (batch := batch_section(uow, principal, hidden=hidden)) else []),
            *proposal_sections(uow, principal, hidden=hidden),
            {
                "title": "Needs attention",
                "empty": "No task needs attention.",
                "columns": ["Task", "Why", "Since"],
                "rows": attention,
            },
            {
                "title": "Pull requests in delivery",
                "empty": "No pull request is open for a task.",
                "columns": ["Task", "State", "Since"],
                "rows": delivering,
            },
            {
                # ADR 0024: every pre-PR gate marked blocking or advisory, and what the
                # reviewer is asked to weigh.
                "title": "Gates by task",
                "note": (
                    "A failed blocking gate stops the task. A failed advisory gate does "
                    "not: it is listed for the reviewer, who decides."
                ),
                "empty": "No task is waiting on its gates.",
                "columns": ["Task", "State", "Gates", "For the reviewer"],
                "rows": [
                    [
                        item["external_id"] or item["id"],
                        _state_words(item["state"]),
                        _gate_steps(item["gates"]),
                        {
                            "kind": "note",
                            "value": "; ".join(
                                f"{r['gate']}: {r['detail']}" for r in item["for_reviewer"]
                            )
                            or "nothing",
                        },
                    ]
                    for item in gates
                ],
            },
            {
                "title": "Tasks by state",
                "empty": "No tasks yet.",
                "columns": ["State", "Tasks"],
                "rows": [[_state_words(state), count] for state, count in sorted(counts.items())],
            },
            {
                "title": "Recently updated",
                "note": (
                    f"Tasks updated in the last {RECENT_TASK_DAYS} days, newest first. Open "
                    "one for its branch, pull request and merge."
                    + (f"\n\n{hidden_note}" if hidden_note else "")
                ),
                "empty": f"No task was updated in the last {RECENT_TASK_DAYS} days.",
                "columns": ["Task", "State", "Updated"],
                "rows": [
                    [
                        _task_link(task.id, task.external_id),
                        _state_words(task.state.value),
                        task.updated_at.isoformat(),
                    ]
                    for task in recent
                ],
            },
        ],
    )


RECENT_TASK_DAYS = 14


RECENT_TASK_ROWS = 50


def _task_link(task_id: str, external_id: str | None) -> dict[str, str]:
    return {"kind": "link", "href": f"/ui/tasks/{quote(task_id)}", "label": external_id or task_id}


def _is_review_diff(artifact: Artifact) -> bool:
    """hades #344: only the collector's own diff, never an artifact a principal uploaded,
    whose type, name, and content type are the uploader's to choose."""
    return (
        artifact.created_by == PRINCIPAL_CRUCIBLE
        and artifact.type == REVIEW_DIFF_TYPE
        and artifact.filename == REVIEW_DIFF_NAME
    )


def _attempt_diff_link(uow: UoW, attempt_id: str) -> dict[str, str] | str:
    for artifact in uow.artifacts.list_for_attempt(attempt_id):
        if _is_review_diff(artifact):
            return {
                "kind": "link",
                "href": f"/ui/artifacts/{quote(artifact.id)}/content",
                "label": "diff.patch",
            }
    return "not collected"


# The states a task page offers the operator's waivers from, in the order a PR moves.
DELIVERY_STATES = (
    TaskState.AWAITING_EXTERNAL_REVIEW,
    TaskState.EXTERNAL_FEEDBACK_RECEIVED,
    TaskState.AWAITING_CI_CERTIFICATION,
    TaskState.CI_CERTIFICATION_FAILED,
    TaskState.HEAD_DIVERGED,
    TaskState.READY_FOR_MERGE,
)


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


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(request: Request, task_id: str, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    try:
        view = task_view(uow, task_id)
    except NotFoundError:
        return RedirectResponse(
            f"/ui/tasks?kind=bad&message={quote(f'No task {task_id}.')}", status_code=303
        )
    # hades FDY-0143: the task's paper trail on GitHub, beside what it is waiting for.
    delivery = view.delivery
    review_request = uow.events.latest_for_task_kind(
        task_id, EventKind.EXTERNAL_REVIEW_REQUESTED.value
    )
    review_request_value = not_yet = "not yet"
    if review_request is not None:
        provider = str(review_request.payload.get("provider") or "external reviewer")
        comment_id = str(review_request.payload.get("comment_id") or "unknown")
        review_request_value = f"{provider}, comment {comment_id}"
    delivered_pr: Any = not_yet
    if delivery.pull_request_number is not None:
        label = f"#{delivery.pull_request_number}"
        delivered_pr = (
            {"href": delivery.pull_request_url, "label": label}
            if (delivery.pull_request_url or "").startswith("https://")
            else label
        )
    proposal: list[dict[str, Any]] = []
    task = (
        uow.tasks.get(task_id) if view.state in (TaskState.PROPOSED, TaskState.SENT_BACK) else None
    )
    if task is not None:
        # hades #424: the proposed contract, readable, and the operator's answers to it.
        rows = contract_rows(uow, task)
        if task.state is TaskState.PROPOSED and principal.role in (Role.OPERATOR, Role.ADMIN):
            rows.append(["Answer", action_forms(task)])
        proposal.append({"title": "Proposed contract", "columns": ["Field", "Value"], "rows": rows})
    sections: list[dict[str, Any]] = [
        *proposal,
        {
            "title": "Task",
            "columns": ["Field", "Value"],
            "rows": [
                ["Task", view.external_id],
                ["Title", view.title],
                ["State", _state_words(view.state.value)],
                ["Id", view.id],
                ["Repository", view.repository],
                ["Owner", view.principal],
                ["Accepted head", view.head_sha or "none yet"],
                ["Created", view.created_at.isoformat()],
                ["Updated", view.updated_at.isoformat()],
            ],
        },
        {
            "title": "Delivery",
            "note": "The task's paper trail on GitHub. Each line fills in when it happens.",
            "columns": ["What", "Value"],
            "rows": [
                ["Work branch", delivery.work_branch or not_yet],
                ["Pushed head", delivery.pushed_head or not_yet],
                [
                    "Pushed at",
                    delivery.pushed_at.isoformat()
                    if delivery.pushed_at
                    else ("not recorded" if delivery.pushed_head else not_yet),
                ],
                ["Pull request", delivered_pr],
                ["Pull request state", delivery.pull_request_state or not_yet],
                ["External review requested", review_request_value],
                ["Merge commit", delivery.merge_sha or not_yet],
                ["Merged by", delivery.merged_by or not_yet],
                ["Merged at", delivery.merged_at.isoformat() if delivery.merged_at else not_yet],
            ],
        },
    ]
    fallthroughs = [
        (attempt.id, words)
        for execution in view.executions
        for attempt in execution.attempts
        if (words := _busy_fallthrough(attempt)) is not None
    ]
    # hades #254: the routing version each attempt routed with, which can be newer than
    # the one the task's policy names.
    fallthroughs += [
        (attempt.id, f"Routed with routing version {attempt.routing_version}")
        for execution in view.executions
        for attempt in execution.attempts
        if attempt.routing_version is not None
    ]
    # hades #388: the window, response allowance and thinking the harness was told.
    fallthroughs += [
        (attempt.id, _effective_words(attempt.effective_settings))
        for execution in view.executions
        for attempt in execution.attempts
        if attempt.effective_settings
    ]
    if fallthroughs:
        sections.append(
            {
                "title": "Routing",
                "columns": ["Attempt", "Decision"],
                "rows": fallthroughs,
            }
        )
    # hades #425: what the launch wrapper could reach before the harness started, per
    # allowlisted host, so a failed install reads as egress rather than as the worker.
    egress_rows = _egress_rows(view)
    if egress_rows:
        sections.append(
            {
                "title": "Egress",
                "note": (
                    "Each allowlisted host, tried from inside the worker before the "
                    "harness started. An unreachable host is the egress path, not the worker."
                ),
                "columns": ["Attempt", "Host", "Result"],
                "rows": egress_rows,
            }
        )
    # Report: show what Crucible filled and what differed per attempt.
    report_rows: list[list[str | dict[str, Any]]] = []
    for execution in view.executions:
        for attempt in execution.attempts:
            try:
                rpt = attempt_report(uow, attempt.id)
            except NotFoundError:
                continue
            filled = ", ".join(rpt.filled_by_crucible) if rpt.filled_by_crucible else "nothing"
            if rpt.differences:
                diffs = "; ".join(
                    f"{d.get('field', '')} ({d.get('detail', '')})" for d in rpt.differences
                )
            else:
                diffs = "none"
            report_rows.append([attempt.id, filled, diffs, _attempt_diff_link(uow, attempt.id)])
    if report_rows:
        sections.append(
            {
                "title": "Report",
                "columns": ["Attempt", "Crucible filled", "Differences", "Diff"],
                "rows": report_rows,
            }
        )
    try:
        record = pull_request_view(uow, task_id)
    except NotFoundError:
        record = None
    if record is not None:
        certification = record.ci_certifications[-1] if record.ci_certifications else None
        sections.append(
            {
                "title": "Pull request",
                "columns": ["Field", "Value"],
                "rows": [
                    ["Pull request", {"href": record.url, "label": f"#{record.number}"}],
                    ["State", record.state],
                    ["Head", record.head_sha],
                    [
                        "External review",
                        f"{record.completed_rounds} of {record.required_rounds} round(s)",
                    ],
                    [
                        "CI",
                        f"{certification.state}: {certification.detail}"
                        if certification
                        else "not certified yet",
                    ],
                    [
                        "Last polled",
                        record.last_polled_at.isoformat() if record.last_polled_at else "never",
                    ],
                ],
            }
        )
        sections.append(
            {
                "title": "Gates after the pull request",
                "empty": "No gate has been evaluated yet.",
                "columns": ["Gate", "Result", "Detail"],
                "rows": [
                    [gate.gate, gate.result, gate.detail]
                    for gate in record.gates
                    if gate.head_sha == view.head_sha
                ],
            }
        )
        sections.append(
            {
                # hades #356: the diagnosis Foundry recorded for each CI certification
                # failure, so the operator sees which kind of failure the task hit.
                "title": "CI decisions",
                "empty": "No CI decision is recorded for this task.",
                "columns": ["Cause", "Action", "Reasoning", "By", "Recorded"],
                "rows": [
                    [
                        decision.cause.replace("_", " "),
                        decision.action,
                        decision.reasoning,
                        decision.principal,
                        decision.created_at.isoformat(),
                    ]
                    for decision in record.ci_decisions
                ],
            }
        )
    waivers = [d for d in view.decisions if d.get("kind") in (WAIVE_EXTERNAL_REVIEW, ACCEPT_NO_CI)]
    sections.append(
        {
            "title": "Operator waivers",
            "empty": "No waiver is recorded for this task.",
            "columns": ["Kind", "Reason", "By", "Recorded"],
            "rows": [
                [d.get("kind"), d.get("verbatim"), d.get("principal"), d.get("created_at")]
                for d in waivers
            ],
        }
    )
    if principal.role is Role.ADMIN and view.state in WAIVABLE_STATES:
        for kind, title, note, resolves in WAIVER_FORMS:
            sections.append(
                {
                    "title": title,
                    "note": note,
                    "form": {
                        "action": f"/ui/tasks/{task_id}/decisions",
                        "label": title,
                        "fields": [
                            {"kind": "hidden", "name": "kind", "value": kind},
                            {"kind": "hidden", "name": "resolves", "value": resolves},
                            {
                                # Not `reason`: this is the decision's verbatim record,
                                # always required, not the optional audit note.
                                "name": "verbatim",
                                "label": "Reason (required; recorded on the task)",
                                "required": True,
                            },
                        ],
                    },
                }
            )
    return _page(
        request,
        principal,
        csrf,
        active=f"/ui/tasks/{task_id}",
        heading=f"Task {view.external_id}",
        intro="The task, its pull request, and what it is waiting for.",
        sections=sections,
    )


@router.get("/artifacts/{artifact_id}/content")
def artifact_content(request: Request, artifact_id: str, ctx: Ctx, uow: UoW) -> Response:
    """The collector's review diff (hades #344), as inert text on the UI's origin: any
    other artifact is refused here, and the bytes are never rendered as markup."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    artifact = uow.artifacts.get(artifact_id)
    if artifact is None or not _is_review_diff(artifact):
        return Response("not found\n", status_code=404, media_type="text/plain; charset=utf-8")
    artifact, content = read_artifact(uow, ctx.artifact_store, artifact_id)
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Content-Disposition": 'inline; filename="diff.patch"',
            "X-Crucible-Artifact-Sha256": artifact.sha256,
        },
    )


@router.post("/tasks/{task_id}/decisions")
async def task_decision(request: Request, task_id: str, ctx: Ctx, uow: UoW) -> Response:
    """ADR 0025: the operator's waiver, recorded through the same decision service the
    API's `POST /v1/tasks/{id}/decisions` uses, so it is audited the same way."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    form = await _form(request)
    form["return_to"] = f"/ui/tasks/{task_id}"
    try:
        _csrf(form, csrf)
        _admin(principal)
        kind = form.get("kind", "")
        if kind not in (WAIVE_EXTERNAL_REVIEW, ACCEPT_NO_CI):
            raise ConflictError(f"the task page records only waivers, not {kind!r}")
        reason = (form.get("verbatim") or "").strip()
        if not reason:
            raise ConflictError("a waiver needs a reason")
        record_decision(
            uow,
            ctx.clock,
            principal=principal,
            task_id=task_id,
            request=DecisionRequest(
                kind=kind,
                verbatim=reason,
                resolves=form.get("resolves") or kind,
            ),
        )
        uow.commit()
        return _redirect(form, f"Recorded: {kind}. The next supervisor tick acts on it.")
    except ApplicationError as exc:
        return _redirect(form, exc.detail, kind="bad")
