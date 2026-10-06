"""The status document (25): one document, each part also its own resource. Sanitized
throughout: booleans, enumerations, timestamps, hashes of non-secret metadata."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from crucible.application.admin import audit, bootstrap, credentials, github
from crucible.application.admin.context import AdminContext
from crucible.application.admin.gateway import plain_outcome
from crucible.application.admin.harnesses import list_images, read_harnesses
from crucible.application.admin.providers import providers_status
from crucible.application.admin.routing import gateway_url
from crucible.application.publish import publishing_waits
from crucible.application.queries import reviewer_items, supervisor_view
from crucible.domain.entities import Principal, Role
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.ports.repository import UnitOfWork

LISTED_STATES = (
    TaskState.BLOCKED,
    TaskState.PRE_PR_GATES_FAILED,
    TaskState.PUBLISH_FAILED,
    TaskState.CI_CERTIFICATION_FAILED,
    TaskState.HEAD_DIVERGED,
)
# ADR 0024: the states in which a task's pre-PR gates are what a person reads next.
GATE_STATES = (
    TaskState.AWAITING_INTERNAL_REVIEW,
    TaskState.PRE_PR_GATES_FAILED,
    TaskState.AWAITING_ACCEPTANCE,
)
CAPABILITY_PARTS = ("harnesses", "providers", "github", "workers", "tasks", "wakes")


def workers(uow: UnitOfWork, *, owner: str | None = None) -> list[dict[str, Any]]:
    """Active attempts; with `owner`, only those of tasks that principal submitted."""
    out: list[dict[str, Any]] = []
    for attempt in uow.attempts.list_in_states(
        [
            AttemptState.PREPARING,
            AttemptState.LAUNCHING,
            AttemptState.RUNNING,
            AttemptState.TERMINATING,
        ]
    ):
        execution = uow.executions.get(attempt.execution_id)
        task = uow.tasks.get(attempt.task_id)
        if owner is not None and (task is None or task.principal_id != owner):
            continue
        out.append(
            {
                "attempt_id": attempt.id,
                "state": attempt.state.value,
                "task_id": attempt.task_id,
                "external_id": task.external_id if task else None,
                "harness": execution.harness if execution else None,
                "model": execution.model if execution else None,
                "image_digest": attempt.image_digest,
                "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
                "last_heartbeat": attempt.log_resume_ts.isoformat()
                if attempt.log_resume_ts
                else None,
            }
        )
    return out


def tasks(uow: UnitOfWork, *, owner: str | None = None) -> dict[str, Any]:
    """Counts by state and the listed states; with `owner`, only that principal's tasks."""
    counts: dict[str, int] = {}
    lists: dict[str, list[dict[str, Any]]] = {}
    for state in TaskState:
        rows = [
            t for t in uow.tasks.list_by_state(state) if owner is None or t.principal_id == owner
        ]
        if rows:
            counts[state.value] = len(rows)
        if state in LISTED_STATES:
            lists[state.value] = [
                {"id": t.id, "external_id": t.external_id, "updated_at": t.updated_at.isoformat()}
                for t in rows
            ]
    mine = {
        t.id
        for t in uow.tasks.list_by_state(TaskState.PUBLISHING)
        if owner is None or t.principal_id == owner
    }
    waiting = [item for item in publishing_waits(uow) if item["task_id"] in mine]
    gates = [
        _task_gates(uow, t)
        for state in GATE_STATES
        for t in uow.tasks.list_by_state(state)
        if owner is None or t.principal_id == owner
    ]
    return {"counts": counts, "lists": lists, "publishing_waiting": waiting, "gates": gates}


def _task_gates(uow: UnitOfWork, task: Any) -> dict[str, Any]:
    """Each pre-PR gate on the task's current head, marked blocking or advisory, and
    what the reviewer is asked to weigh (ADR 0024)."""
    rows = [
        r
        for r in uow.gate_results.list_for_task(task.id)
        if r.phase == "pre_pr" and r.head_sha == (task.head_sha or "")
    ]
    return {
        "id": task.id,
        "external_id": task.external_id,
        "state": task.state.value,
        "gates": [
            {
                "gate": r.gate,
                "result": r.result,
                "classification": "blocking" if r.blocking else "advisory",
                "detail": r.detail,
            }
            for r in sorted(rows, key=lambda r: r.gate)
        ],
        "for_reviewer": reviewer_items(rows),
    }


def wakes(uow: UnitOfWork, *, owner: str | None = None) -> dict[str, Any]:
    """Pending wakes per principal; with `owner`, that principal's alone, and `unacked`
    counts only its own. Each principal's count comes from a count query, not the
    (page-limited) list, so a principal with more than the page limit of pending
    wakes still reports its true count."""
    per_principal: dict[str, int] = {}
    oldest: str | None = None
    for principal in uow.principals.list_all():
        if owner is not None and principal.id != owner:
            continue
        pending = uow.wakes.list_for_principal(
            principal.id, since=None, include_acked=False, limit=200
        )
        if pending:
            per_principal[principal.name] = uow.wakes.count_unacked_for_principal(principal.id)
            first = min(w.created_at for w in pending).isoformat()
            oldest = first if oldest is None or first < oldest else oldest
    return {
        "pending": per_principal,
        "oldest_pending": oldest,
        "unacked": uow.wakes.count_unacked() if owner is None else sum(per_principal.values()),
    }


def retention(uow: UnitOfWork) -> dict[str, Any]:
    recent = list(uow.retention.list_recent(50))
    last = max((a.acted_at for a in recent), default=None)
    return {
        "last_run": last.isoformat() if last else None,
        "recent_actions": len(recent),
        "kinds": sorted({a.kind for a in recent}),
    }


def is_test_fixture(ctx: AdminContext, harness: str) -> bool:
    """A harness that exists only for the test tiers (the script harness, 18). It is never
    a prerequisite for real work, so the readiness list leaves it out (crucible#123)."""
    return bool(getattr(ctx.harnesses.get(harness), "test_fixture", False))


def _enabled_models(uow: UnitOfWork) -> set[str]:
    document, _where = credentials._routing_document(uow)
    return {
        str(model.get("harness"))
        for model in (document or {}).get("models", [])
        if model.get("enabled") is True
    }


def _step(code: str, text: str, fix: str) -> dict[str, str]:
    return {"code": code, "text": text, "fix": fix}


def _harness_steps(
    ctx: AdminContext,
    uow: UnitOfWork,
    item: dict[str, Any],
    *,
    enabled_models: set[str],
    endpoint: str | None,
    unreachable: str | None,
    secret: credentials.SecretRead | None = None,
) -> list[dict[str, str]]:
    """What stands between one harness and its first task, in the order an operator
    fixes it, each naming the page that fixes it. Read from the same state the
    Harnesses, Credentials, Local gateway, Routing and Images pages show."""
    name = str(item["name"])
    hermes = name in {credentials.HERMES, "qwen_code"}
    credential_page = "/ui/gateway" if hermes else "/ui/credentials"
    model_page = "/ui/gateway" if hermes else "/ui/routing"
    steps: list[dict[str, str]] = []
    if not item["enabled_by_administrator"]:
        steps.append(
            _step("disabled", f"{name} is disabled. Enable it on Harnesses.", "/ui/harnesses")
        )
    view = credentials.state_view(ctx, uow, name, secret)
    state = view.get("state")
    if state == "absent":
        steps.append(
            _step(
                "credential_missing",
                f"{name} has no key. Set the gateway URL and key on Local gateway."
                if hermes
                else f"{name} has no credential. Log in on Credentials.",
                credential_page,
            )
        )
    elif state == "unreadable":
        steps.append(
            _step(
                "credential_unreadable",
                f"The credential of {name} cannot be read: {view.get('detail') or 'no detail'}.",
                credential_page,
            )
        )
    elif state == "invalid":
        steps.append(
            _step(
                "credential_invalid",
                f"The credential of {name} was refused at its last test or launch. "
                + ("Set a new key on Local gateway." if hermes else "Log in again on Credentials."),
                credential_page,
            )
        )
    if hermes and endpoint is None:
        steps.append(
            _step(
                "endpoint_not_configured",
                f"The gateway URL for {name} is not set. Set it on Local gateway.",
                "/ui/gateway",
            )
        )
    if state == "configured":
        steps.append(
            _step(
                "credential_not_verified",
                f"The credential of {name} is set but not verified ("
                f"{plain_outcome(view.get('last_launch_outcome'))}). "
                + ("Test it on Local gateway." if hermes else "Validate it on Credentials."),
                credential_page,
            )
        )
    if name not in enabled_models:
        steps.append(
            _step(
                "no_enabled_model",
                f"{name} has no enabled model. Pick one on Local gateway."
                if hermes
                else f"{name} has no enabled model in the routing policy in force. "
                "Enable one on Routing.",
                model_page,
            )
        )
    if hermes and endpoint is not None and unreachable is not None:
        steps.append(
            _step(
                "endpoint_unreachable",
                f"Workers cannot reach the gateway of {name}: {unreachable}. "
                "Check the Kubernetes egress selectors on Routing.",
                "/ui/routing",
            )
        )
    # Promotion is per harness (ADR 0018): the harness's own default, which must also be
    # an image a provider lists now. A row alone can name an image deleted since.
    default = item.get("default_image")
    if not default:
        steps.append(
            _step(
                "no_promoted_image",
                f"{name} has no promoted worker image. Promote one on Images.",
                "/ui/images",
            )
        )
    elif not any(i.get("promotion_state") == "default" for i in item.get("images") or []):
        steps.append(
            _step(
                "promoted_image_missing",
                f"The promoted image for {name} ({default.get('reference') or 'no reference'}) "
                "is no longer in the registry, or the registry did not answer. "
                "Promote another on Images.",
                "/ui/images",
            )
        )
    return steps


def harness_readiness(
    ctx: AdminContext,
    uow: UnitOfWork,
    items: list[dict[str, Any]],
    providers: list[dict[str, Any]],
    secrets: dict[str, credentials.SecretRead] | None = None,
) -> list[dict[str, Any]]:
    """Each real harness `ready`, `not_ready` with the steps that fix it, or `off` when the
    configuration default keeps it off and no administrator has decided (hades #174);
    test fixtures are left out. The Harnesses page reads its
    one word from this too, so it and Status never disagree about a harness."""
    endpoint, _source = gateway_url(uow)
    enabled_models = _enabled_models(uow)
    unreachable: str | None = None
    for provider in providers:
        checks = provider.get("checks") or {}
        if checks.get("local_endpoint_reachable") is False:
            unreachable = str(checks.get("local_endpoint_detail") or "the connection failed")
    harnesses: list[dict[str, Any]] = []
    for item in items:
        name = str(item["name"])
        if is_test_fixture(ctx, name):
            continue
        if not item["enabled_by_configuration"] and not item.get("decided_by_administrator"):
            harnesses.append(
                {
                    "name": name,
                    "state": "off",
                    "note": (
                        "off by the configuration default: "
                        f"{item.get('reason') or 'no reason given'}. Enable it on Harnesses "
                        "to use it"
                    ),
                    "steps": [],
                }
            )
            continue
        harness_steps = _harness_steps(
            ctx,
            uow,
            item,
            enabled_models=enabled_models,
            endpoint=endpoint,
            unreachable=unreachable,
            secret=(secrets or {}).get(name),
        )
        harnesses.append(
            {
                "name": name,
                "state": "not_ready" if harness_steps else "ready",
                "note": "" if harness_steps else "ready for a task",
                "steps": harness_steps,
            }
        )
    return harnesses


def readiness(
    ctx: AdminContext,
    uow: UnitOfWork,
    document: dict[str, Any],
    secrets: dict[str, credentials.SecretRead] | None = None,
) -> dict[str, Any]:
    """crucible#123: the "before a task can run" list. The system is ready when the
    supervisor is healthy, a repository is registered, and at least one harness is
    ready."""
    harnesses = harness_readiness(ctx, uow, document["harnesses"], document["providers"], secrets)
    steps: list[dict[str, str]] = []
    supervisor = document["supervisor"]
    repositories = uow.repositories.list_all()
    if not supervisor["healthy"]:
        steps.append(
            _step(
                "supervisor",
                f"The supervisor is not ready: {supervisor['health_detail']}.",
                "/ui",
            )
        )
    if not repositories:
        steps.append(
            _step(
                "no_repository",
                "No repository is registered. Pick one on GitHub, or register it on Repositories.",
                "/ui/github",
            )
        )
    if repositories and document.get("github", {}).get("configured") is not True:
        github_host = urlsplit(
            document.get("github", {}).get("api_base", "https://api.github.com")
        ).hostname
        if github_host == "api.github.com":
            github_host = "github.com"
        if github_host and any(
            urlsplit(getattr(repo, "url", "")).hostname == github_host for repo in repositories
        ):
            steps.append(
                _step(
                    "github_app_not_connected",
                    "A GitHub repository is registered but no GitHub App is connected.",
                    "/ui/github",
                )
            )
    ready_harnesses = [h["name"] for h in harnesses if h["state"] == "ready"]
    if not ready_harnesses:
        steps.append(
            _step(
                "no_ready_harness",
                "No harness is ready yet. Each harness's missing steps are listed below.",
                "/ui",
            )
        )
    return {
        "ready": not steps,
        "ready_harnesses": ready_harnesses,
        "steps": steps,
        "harnesses": harnesses,
    }


async def status(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    images = await list_images(ctx)
    # The GitHub store is read while the harness Secrets are, not after them.
    stored_read = asyncio.ensure_future(github.read_stored(ctx))
    try:
        harnesses, secrets = await read_harnesses(ctx, uow, [i for _, i in images])
    finally:
        stored = await stored_read
    credential_states = {h["name"]: h["credential"] for h in harnesses}
    supervisor = supervisor_view(
        uow, list(ctx.providers.values()), ctx.clock.now(), ctx.lease_ttl_seconds
    ).model_dump(mode="json")
    document = {
        "harnesses": harnesses,
        "credentials": credential_states,
        "providers": await providers_status(ctx),
        "github": github.status(ctx, uow, stored=stored),
        "supervisor": supervisor,
        "workers": workers(uow),
        "tasks": tasks(uow),
        "wakes": wakes(uow),
        "retention": retention(uow),
        # 25: the bootstrap import is CLI-and-API in C6; its state is exposed here.
        "bootstrap": bootstrap.status_part(uow),
        "audit": {"cursor": audit.tail(uow, cursor=None, limit=1)["next_cursor"]},
    }
    document["readiness"] = readiness(ctx, uow, document, secrets)
    return document


async def capabilities(ctx: AdminContext, uow: UnitOfWork, principal: Principal) -> dict[str, Any]:
    """25: the orchestrator's read-only view: harnesses, providers, github health,
    workers, tasks, wakes; nothing it could mutate and no credential detail beyond the
    state enumeration.

    An orchestrator sees its own work only: the workers, tasks and wakes parts are
    filtered to the tasks it submitted and the wakes addressed to it (crucible#40). An
    operator is exempt, as it is from task ownership (04). Harness, provider and GitHub
    health are the service's and not any one principal's, so every caller sees them."""
    document = await status(ctx, uow)
    view = {part: document[part] for part in CAPABILITY_PARTS}
    if principal.role is not Role.OPERATOR:
        view["workers"] = workers(uow, owner=principal.id)
        view["tasks"] = tasks(uow, owner=principal.id)
        view["wakes"] = wakes(uow, owner=principal.id)
    for harness in view["harnesses"]:
        harness["credential"] = {
            "state": harness["credential"]["state"],
            "session_compatibility": harness["credential"]["session_compatibility"],
        }
    view["github"] = {
        "configured": view["github"]["configured"],
        "key_present": view["github"]["key_present"],
        "repositories": [
            {"repository": r["repository"], "installation_covers": r["installation_covers"]}
            for r in view["github"]["repositories"]
        ],
    }
    return view
