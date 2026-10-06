from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.pages import routing_models as routing_models_page
from crucible.adapters.ui.render import (
    _document_section,
    _duration_words,
    _page,
    _routing_policy_details,
)
from crucible.adapters.ui.session import _require
from crucible.application.admin import credentials as credentials_admin
from crucible.application.admin import gate_classes as gate_classes_admin
from crucible.application.admin import kubernetes as kubernetes_admin
from crucible.application.admin import limits as limits_admin
from crucible.application.admin import routing, routing_preference
from crucible.application.admin.context import guard_mutation
from crucible.application.errors import ConflictError, ContractValidationError
from crucible.application.policies import put_policy, put_routing_policy
from crucible.domain.cluster_egress import format_labels, parse_labels
from crucible.domain.entities import Principal, Role
from crucible.domain.gates import ALWAYS_BLOCKING_GATES, PRE_PR_GATES
from crucible.domain.secrets import scan_text

router = APIRouter(prefix="/ui", include_in_schema=False)


@router.get("/routing", response_class=HTMLResponse)
async def routing_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    versions = list(uow.policies.list_versions("default-software"))
    # The version in force: the newest one not retired, as the timeout editor reads it.
    live = [item for item in versions if item.retired_at is None]
    policy = max(live, key=lambda item: item.version) if live else None
    routing_ref = ((policy.document.get("routing") or {}).get("policy") or {}) if policy else {}
    routing_record = (
        uow.routing_policies.get(
            str(routing_ref.get("name", "")), int(routing_ref.get("version", 0))
        )
        if routing_ref
        else None
    )
    assert ctx.admin is not None
    exhaustion = routing.list_exhaustions(ctx.admin, uow)
    local = routing.local_endpoint_view(uow)
    egress = kubernetes_admin.egress_view(ctx.admin, uow)
    role_timeouts = kubernetes_admin.timeouts_view(ctx.admin, uow)
    capacity = await kubernetes_admin.capacity_view(ctx.admin)
    role_seconds = int(role_timeouts["document"].get("role_timeout_seconds") or 0)
    gateway_endpoint, _source = routing.gateway_url(uow)
    command_timeout = limits_admin.command_timeout_view(uow)
    preference = routing_preference.preference_view(uow)
    rotation = preference["rotation"]
    classes = gate_classes_admin.gate_classes_view(uow)
    admin = principal.role is Role.ADMIN
    bounds = command_timeout["command_timeout_ms"]
    dns = egress["document"].get("dns") or {}
    endpoint = egress["document"].get("local_endpoint") or {}
    local_models = ", ".join(m["id"] for m in local["models"] if m.get("enabled")) or "none"
    per_harness = (
        (policy.document.get("concurrency", {}).get("per_harness") or {}) if policy else {}
    )
    # crucible#115: what is in force, one line each, in plain words; the documents behind
    # them are under Details, and each is edited from its own form below.
    in_force: list[list[Any]] = [
        [
            "Delivery policy",
            f"{policy.name} version {policy.version}" if policy else "none",
            "",
        ],
        [
            "Routing policy",
            f"{routing_ref.get('name')} version {routing_ref.get('version')}"
            if routing_ref
            else "none",
            "",
        ],
        [
            "Local gateway",
            {
                "kind": "note",
                "value": gateway_endpoint or "not set",
                "hint": f"models in use: {local_models}",
            },
            {"kind": "link", "href": "/ui/gateway", "label": "Set up on Local gateway"},
        ],
        [
            "Routing order",
            {
                "kind": "note",
                "value": "; ".join(
                    f"{name}: {_pool_order_words(rule)}"
                    for name, rule in preference["tiers"].items()
                ),
                "hint": (
                    "other allowed models are fallbacks when these are unavailable or busy; "
                    "the task page says when a task ran on its next choice because one was busy"
                ),
            },
            "",
        ],
        [
            "Frontier workers",
            {
                "kind": "note",
                "value": "; ".join(
                    f"{name}: {per_harness.get(name, 1)} at once"
                    for name in ("claude_code", "codex", "agy")
                ),
                "hint": (
                    "Claude Code uses a read-only token that never refreshes; AGY keeps its "
                    "refresh token; Codex permits parallel workers in renewer mode. "
                    "Rollback to rw-narrow copies requires per_harness.codex 1. "
                    "Watch per-harness auth_failure_count in supervisor logs."
                ),
            },
            "",
        ],
        [
            "Demotion",
            {
                "kind": "note",
                "value": (
                    f"at {rotation['demote_failure_percent']}% blocking-gate failures over "
                    f"at least {rotation['demote_min_sample']} of the last "
                    f"{rotation['quality_window']} attempts in a project"
                    if rotation["quality_feedback"]
                    else "off: failures never move routing"
                ),
                "hint": (
                    f"a demoted model is tried again after {rotation['probe_after_minutes']} "
                    "minutes; one failure never demotes"
                ),
            },
            "",
        ],
        [
            "Per-command timeout",
            {
                "kind": "note",
                "value": f"{_duration_words(bounds['default'])} by default",
                "hint": (
                    f"a task may set {_duration_words(bounds['min'])} "
                    f"to {_duration_words(bounds['max'])}"
                ),
            },
            "",
        ],
        [
            "Advisory gates",
            {
                "kind": "note",
                "value": ", ".join(classes["advisory"]) or "none: every gate blocks",
                "hint": (
                    "a failure goes to the reviewer instead of stopping the task"
                    + ("; the default set" if classes["default"] else "")
                    + ". A prohibited path always blocks."
                ),
            },
            "",
        ],
        [
            "Kubernetes worker egress",
            {
                "kind": "note",
                "value": (
                    f"DNS: {dns.get('namespace') or 'any namespace'}; local endpoint: "
                    f"{endpoint.get('namespace') or 'outside the cluster'}"
                )
                if egress["provider_enabled"]
                else "not in use: the Kubernetes provider is off",
            },
            "",
        ],
        [
            "Kubernetes short-role timeout",
            {
                "kind": "note",
                "value": f"{_duration_words(role_seconds * 1000)} from the Pod running"
                if role_timeouts["provider_enabled"]
                else "not in use: the Kubernetes provider is off",
                "hint": "bundle verifier, cleaner, and the Job that readies a publish",
            },
            "",
        ],
        [
            "Kubernetes worker capacity",
            {
                "kind": "note",
                "value": _capacity_words(capacity),
                "hint": (
                    "the namespace quota's headroom, less the Pods kept for Hades's own "
                    "short-role Pods (gate probe, collector, canary, login, preparer); a "
                    "launch past it waits, scheduled, for a worker to finish"
                ),
            },
            "",
        ],
    ]
    marks = [item for item in exhaustion["items"] if item["active"]]
    sections: list[dict[str, Any]] = [
        {
            "title": "In force",
            "columns": ["Setting", "Value", ""],
            "rows": in_force,
            "details": [
                _document_section("Local endpoint", local),
                _document_section("Routing order and demotion", preference),
                _document_section("Kubernetes egress selectors", egress),
                _document_section("Kubernetes short-role timeout", role_timeouts),
                _document_section("Kubernetes worker capacity", capacity),
                _document_section("Per-command timeout", command_timeout),
                _document_section("Gate classes", classes),
                *(
                    _routing_policy_details(
                        routing_record.document if routing_record else None,
                        policy.document if policy else None,
                    )
                    for _ in [1]
                ),
            ],
        },
        {
            "title": "Exhausted pools",
            "empty": "No pool is marked exhausted.",
            "columns": ["Pool", "Since", "Resets", "Why", ""],
            "rows": [
                [
                    item["pool"],
                    item["exhausted_at"],
                    item["reset_at"],
                    item["reason"],
                    # The row's own action, never a typed pool name (crucible#127).
                    {
                        "kind": "form",
                        "action": "/ui/actions/routing-clear",
                        "label": "Clear",
                        "reason": "optional",
                        "hidden": {"pool": item["pool"]},
                    }
                    if admin
                    else "",
                ]
                for item in marks
            ],
            "details": [_document_section("Every mark, cleared ones too", exhaustion)]
            if exhaustion["items"]
            else [],
        },
    ]
    sections[1:1] = routing_models_page.control_sections(uow, admin=admin)
    if routing_ref:
        sections.insert(1, _versions_section(uow, str(routing_ref.get("name", ""))))
    if admin:
        tier_fields: list[dict[str, Any]] = []
        for name, rule in preference["tiers"].items():
            tier_fields.extend(
                [
                    {
                        "name": f"prefer_{name}",
                        "label": f"{name}: pools first, in order",
                        "value": ", ".join(rule["prefer_pools"]),
                        "placeholder": "no preference",
                    },
                    {
                        "name": f"default_{name}",
                        "label": f"{name}: use the default",
                        "kind": "checkbox",
                        "value": rule["default"],
                    },
                ]
            )
        sections.append(
            {
                "title": "Edit routing order and demotion",
                "note": (
                    "Per tier, the pools routing tries first, in order (pool names, comma "
                    f"separated; the pools are {', '.join(preference['pools'])}). Leave it "
                    "empty for no preference, where the tier's capability preference "
                    "decides. The default is "
                    f"{preference['default_rule']}. A model is demoted in a project when "
                    "the percentage of its judged attempts that failed a blocking gate "
                    "reaches the threshold, over at least the minimum sample; a demoted "
                    "model is tried again once its last attempt is the probe interval old. "
                    "Saving writes a new routing version and a delivery policy version "
                    "naming it."
                ),
                "form": {
                    "action": "/ui/actions/routing-preference",
                    "label": "Save routing order",
                    "collapsed": "Change the routing order or demotion",
                    "fields": [
                        *tier_fields,
                        {
                            "name": "quality_feedback",
                            "label": "Demote models that fail blocking gates",
                            "kind": "checkbox",
                            "value": rotation["quality_feedback"],
                        },
                        {
                            "name": "quality_window",
                            "label": "Attempts judged per model",
                            "kind": "number",
                            "value": rotation["quality_window"],
                            "required": True,
                        },
                        {
                            "name": "demote_failure_percent",
                            "label": "Failure percentage that demotes",
                            "kind": "number",
                            "value": rotation["demote_failure_percent"],
                            "required": True,
                        },
                        {
                            "name": "demote_min_sample",
                            "label": "Minimum judged attempts (2 or more)",
                            "kind": "number",
                            "value": rotation["demote_min_sample"],
                            "required": True,
                        },
                        {
                            "name": "probe_after_minutes",
                            "label": "Probe a demoted model after (minutes)",
                            "kind": "number",
                            "value": rotation["probe_after_minutes"],
                            "required": True,
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
        sections.append(
            {
                "title": "Edit per-command timeout",
                "note": (
                    "The timeout, in milliseconds, every harness runs a shell command under. "
                    "A task may narrow the default within min and max. Saving writes a new "
                    "delivery policy version."
                ),
                "form": {
                    "action": "/ui/actions/command-timeout",
                    "label": "Save command timeout",
                    "collapsed": "Change the per-command timeout",
                    "fields": [
                        {
                            "name": "min",
                            "label": "Minimum (ms)",
                            "kind": "number",
                            "value": bounds["min"],
                            "required": True,
                        },
                        {
                            "name": "default",
                            "label": "Default (ms)",
                            "kind": "number",
                            "value": bounds["default"],
                            "required": True,
                        },
                        {
                            "name": "max",
                            "label": "Maximum (ms)",
                            "kind": "number",
                            "value": bounds["max"],
                            "required": True,
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
        sections.append(
            {
                "title": "Edit advisory gates",
                "note": (
                    "Ticked gates are advisory: a failure is recorded and listed for the "
                    "reviewer, and the task goes on to its internal review. Unticked gates "
                    "block. internal_review_recorded and no_secrets always block, "
                    "commit_policy (who authored the commits) is always advisory, a path "
                    "matching a contract's prohibited_paths stops the task even when "
                    "scope_contained is advisory, and so does a missing report. A task "
                    "whose policy requires no internal review for its head goes to "
                    "acceptance, with the list in the wake. Making a gate outside the "
                    "default set advisory is an operator decision. Saving writes a new "
                    "delivery policy version."
                ),
                "form": {
                    "action": "/ui/actions/gate-classes",
                    "label": "Save advisory gates",
                    "collapsed": "Change which gates are advisory",
                    "fields": [
                        {
                            "name": f"advisory_{gate}",
                            "label": gate,
                            "kind": "checkbox",
                            "value": gate in classes["advisory"],
                        }
                        for gate in sorted(PRE_PR_GATES - ALWAYS_BLOCKING_GATES)
                    ]
                    + [{"name": "reason", "label": "Reason", "required": True}],
                },
            }
        )
        sections.append(
            {
                "title": "Edit Kubernetes egress selectors",
                "note": (
                    "How a Kubernetes worker reaches cluster DNS and an in-cluster local "
                    "endpoint when the CNI translates service addresses before it applies "
                    "policy (Cilium with kube-proxy replacement). Labels are key=value, "
                    "comma separated. Leave the endpoint namespace empty for an endpoint "
                    "outside the cluster. Every process picks a save up within 15 seconds "
                    "and re-runs the namespace readiness canary before a launch uses it."
                ),
                "form": {
                    "action": "/ui/actions/kubernetes-egress",
                    "label": "Save egress selectors",
                    "collapsed": "Change the egress selectors",
                    "fields": [
                        {
                            "name": "dns_namespace",
                            "label": "DNS namespace",
                            "value": dns.get("namespace", ""),
                        },
                        {
                            "name": "dns_labels",
                            "label": "DNS pod labels",
                            "value": format_labels(dns.get("pod_labels") or {}),
                        },
                        {
                            "name": "endpoint_namespace",
                            "label": "Local endpoint namespace",
                            "value": endpoint.get("namespace", ""),
                        },
                        {
                            "name": "endpoint_labels",
                            "label": "Local endpoint pod labels",
                            "value": format_labels(endpoint.get("pod_labels") or {}),
                        },
                        {
                            "name": "endpoint_port",
                            "label": "Local endpoint pod port (0: the URL's port)",
                            "kind": "number",
                            "value": endpoint.get("port", 0),
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
        sections.append(
            {
                "title": "Edit Kubernetes short-role timeout",
                "note": (
                    "How long the short Kubernetes roles may run once their Pod is "
                    "running: the bundle verifier, the cleaner, and the Job that readies a "
                    "workspace for a publish. Pulling the image and waiting for a node "
                    "count against the launch timeout instead. Seconds, "
                    f"{role_timeouts['bounds']['min']} to {role_timeouts['bounds']['max']}. "
                    "Every process picks a save up within 15 seconds."
                ),
                "form": {
                    "action": "/ui/actions/kubernetes-timeouts",
                    "label": "Save role timeout",
                    "collapsed": "Change the short-role timeout",
                    "fields": [
                        {
                            "name": "role_timeout_seconds",
                            "label": "Short-role timeout (seconds)",
                            "kind": "number",
                            "value": role_seconds,
                            "required": True,
                        },
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
        sections.extend(
            [
                {
                    "title": "Upload routing policy version",
                    "note": (
                        "Upload a complete validated JSON document. Referenced versions "
                        "remain immutable."
                    ),
                    "form": {
                        "action": "/ui/actions/routing-upload",
                        "label": "Upload routing",
                        "collapsed": "Upload a routing policy document",
                        "fields": [
                            {
                                "name": "name",
                                "label": "Name",
                                "value": "default-routing",
                                "required": True,
                            },
                            {
                                "name": "version",
                                "label": "Version",
                                "kind": "number",
                                "required": True,
                            },
                            {
                                "name": "document",
                                "label": "Document",
                                "kind": "textarea",
                                "rows": 12,
                                "required": True,
                            },
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                },
                {
                    "title": "Upload delivery policy version",
                    "form": {
                        "action": "/ui/actions/policy-upload",
                        "label": "Upload policy",
                        "collapsed": "Upload a delivery policy document",
                        "fields": [
                            {
                                "name": "name",
                                "label": "Name",
                                "value": "default-software",
                                "required": True,
                            },
                            {
                                "name": "version",
                                "label": "Version",
                                "kind": "number",
                                "required": True,
                            },
                            {
                                "name": "document",
                                "label": "Document",
                                "kind": "textarea",
                                "rows": 12,
                                "required": True,
                            },
                            {
                                "name": "routing_pinned",
                                "label": "Deliberately pin the routing policy version",
                                "kind": "checkbox",
                                "value": False,
                            },
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                },
            ]
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/routing",
        heading="Routing",
        intro="What routes and limits a task: the policies in force, the gateway, and pools.",
        sections=sections,
    )


def _versions_section(uow: UoW, name: str) -> dict[str, Any]:
    """hades #437: each routing version beside what it changed, who published it and
    why, so a publish that flips a model or a pool cap is seen, not found a day later."""
    history = routing.routing_history(uow, name)
    return {
        "title": "Routing versions",
        "note": (
            "Newest first. A version that enables or disables a model or changes a pool "
            "cap names the decision it supersedes in its reason, and wakes the "
            "orchestrator with this change."
        ),
        "empty": "No routing version is recorded.",
        "columns": ["Version", "Published", "By", "Reason", "What changed"],
        "rows": [
            [
                f"{item['version']}" + (" (retired)" if item["retired"] else ""),
                item["created_at"],
                item["published_by"] or "not recorded",
                item["reason"] or "none given",
                "; ".join(routing.delta_words(item["delta"]))
                if item["delta"] is not None
                else "first version",
            ]
            for item in history
        ],
    }


def _capacity_words(capacity: dict[str, Any]) -> str:
    """hades #423, #478: the worker capacity in one line: headroom, reservation, result,
    and the quota row that produced the number."""
    if not capacity.get("provider_enabled"):
        return "not in use: the Kubernetes provider is off"
    if capacity.get("error"):
        return f"could not be read: {capacity['error']}"
    workers = capacity.get("worker_capacity")
    headroom = capacity.get("quota_headroom")
    reserved = capacity.get("short_role_pods_reserved") or 0
    source = capacity.get("capacity_source", "")
    if headroom is None:
        return (
            f"{workers} worker(s) at once from kubernetes.max_concurrency; "
            "no quota in the namespace"
        )
    parts = [f"{workers} worker(s) at once: the quota admits {headroom} Pod(s)"]
    # AC3: show the quota row and the shape source that produced the capacity
    if "the active policy" in source:
        parts.append("shape from the active policy")
    elif "the last launch" in source:
        parts.append("shape from the last launch")
    parts.append(f"{reserved} kept for short-role Pods")
    return ", ".join(parts)


def _milliseconds(form: dict[str, str], name: str) -> int | None:
    raw = form.get(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ContractValidationError(
            f"{name} must be a whole number of milliseconds",
            errors=[{"path": name, "message": "a whole number of milliseconds"}],
        )
    return int(raw)


def _whole_number(form: dict[str, str], name: str) -> int | None:
    raw = form.get(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ContractValidationError(
            f"{name} must be a whole number",
            errors=[{"path": name, "message": "a whole number"}],
        )
    return int(raw)


def _tier_order(form: dict[str, str], tier: str, shown: list[str]) -> list[str] | None:
    """A tier's order from the Routing form. "Use the default" keeps the default only
    while the order field still shows what was on the page: an order typed over it is
    the administrator's, not something to discard under a ticked box."""
    typed = routing_preference.parse_pool_order(form.get(f"prefer_{tier}", ""))
    if form.get(f"default_{tier}") == "true" and (typed is None or typed == shown):
        return None
    return typed


def _pool_order_words(rule: dict[str, Any]) -> str:
    """One tier's pool order as the Routing page says it (ADR 0028)."""
    pools = rule["prefer_pools"]
    words = (
        f"{' then '.join(pools)} first"
        if pools
        else f"no pool preference ({', '.join(rule['prefer'])} first by capability)"
    )
    return f"{words} (default)" if rule["default"] else words


async def _actions(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    assert ctx.admin is not None
    if action == "routing-clear":
        routing.clear_exhaustion(
            ctx.admin, uow, principal=principal.name, pool=form.get("pool", ""), reason=reason
        )
    elif action == "routing-preference":
        tiers = routing_preference.preference_view(uow)["tiers"]
        routing_preference.save_preference(
            ctx.admin,
            uow,
            principal=principal,
            tiers={
                name: _tier_order(form, name, rule["prefer_pools"]) for name, rule in tiers.items()
            },
            rotation={
                "quality_feedback": form.get("quality_feedback") == "true",
                **{
                    key: value
                    for key in (
                        "quality_window",
                        "demote_failure_percent",
                        "demote_min_sample",
                        "probe_after_minutes",
                    )
                    if (value := _whole_number(form, key)) is not None
                },
            },
            reason=reason,
        )
    elif action == "gate-classes":
        gate_classes_admin.save_gate_classes(
            ctx.admin,
            uow,
            principal=principal,
            advisory=[
                key.removeprefix("advisory_")
                for key, value in form.items()
                if key.startswith("advisory_") and value == "true"
            ],
            reason=reason,
        )
    elif action == "command-timeout":
        limits_admin.save_command_timeout(
            ctx.admin,
            uow,
            principal=principal,
            minimum=_milliseconds(form, "min"),
            maximum=_milliseconds(form, "max"),
            default=_milliseconds(form, "default"),
            reason=reason,
        )
    elif action == "kubernetes-egress":
        kubernetes_admin.save_egress(
            ctx.admin,
            uow,
            principal=principal.name,
            document={
                "dns": {
                    "namespace": form.get("dns_namespace", ""),
                    "pod_labels": parse_labels(form.get("dns_labels", "")),
                },
                "local_endpoint": {
                    "namespace": form.get("endpoint_namespace", ""),
                    "pod_labels": parse_labels(form.get("endpoint_labels", "")),
                    "port": int(form.get("endpoint_port") or "0"),
                },
            },
            reason=reason,
        )
    elif action == "kubernetes-timeouts":
        raw_seconds = form.get("role_timeout_seconds", "").strip()
        kubernetes_admin.save_timeouts(
            ctx.admin,
            uow,
            principal=principal.name,
            document={
                "role_timeout_seconds": int(raw_seconds)
                if raw_seconds.lstrip("-").isdigit()
                else raw_seconds
            },
            reason=reason,
        )
    elif action in ("routing-upload", "policy-upload"):
        raw = form.get("document", "")
        if scan_text(raw) is not None:
            raise ConflictError("the policy document looks like it contains a secret")
        document = json.loads(raw)
        audited_reason = guard_mutation(
            ctx.admin,
            uow,
            reason,
            principal=principal.name,
            operation=action,
        )
        if action == "routing-upload":
            put_routing_policy(
                uow,
                ctx.clock,
                principal=principal,
                name=form.get("name", ""),
                version=int(form.get("version", "0")),
                document=document,
                reason=audited_reason,
            )
        else:
            document.setdefault("routing", {}).setdefault("policy", {})["pinned"] = (
                form.get("routing_pinned") == "true"
            )
            put_policy(
                uow,
                ctx.clock,
                principal=principal,
                name=form.get("name", ""),
                version=int(form.get("version", "0")),
                document=document,
                reason=audited_reason,
                concurrency_modes={
                    name: credentials_admin.mount_mode_value(ctx.admin, uow, name).value
                    for name in ctx.admin.harnesses.names()
                    if (adapter := ctx.admin.harnesses.get(name)) is not None
                    and adapter.credential_spec() is not None
                },
            )
    return None


register("routing-clear", _actions)
register("routing-preference", _actions)
register("gate-classes", _actions)
register("command-timeout", _actions)
register("kubernetes-egress", _actions)
register("kubernetes-timeouts", _actions)
register("routing-upload", _actions)
register("policy-upload", _actions)
