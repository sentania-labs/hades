"""Deterministic class routing, pool state, usage, and history (05b, C6b)."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from crucible.application.harnesses import HarnessRegistry
from crucible.contracts.policy import RoutingModel, RoutingPolicyV1, window_seconds
from crucible.domain.entities import AttemptMetrics
from crucible.domain.lifecycle import AttemptState
from crucible.domain.verification import task_specific_checks
from crucible.ports.repository import UnitOfWork

Problem = dict[str, Any]


# hades #373: a model-only refusal excludes one model, not its pool. The exclusion is a
# row of the pool_exhaustions table keyed by the route (the harness and model pair a
# routing entry is identified by) under this prefix, so it is written, listed, expired
# and cleared exactly as a pool mark is (05b, 25), and no pool can carry the name.
MODEL_MARK_PREFIX = "model:"


def model_mark_key(model_id: str, harness: str) -> str:
    """The pool_exhaustions key of a route's own exclusion mark (hades #373)."""
    return f"{MODEL_MARK_PREFIX}{harness}:{model_id}"


def model_excluded_until(
    uow: UnitOfWork, model_id: str, harness: str, now: datetime
) -> datetime | None:
    """The reset of a live model-only exclusion (hades #373), or None."""
    mark = uow.pool_exhaustions.get(model_mark_key(model_id, harness))
    if mark is not None and mark.cleared_at is None and mark.reset_at > now:
        return mark.reset_at
    return None


@dataclass(frozen=True, slots=True)
class PoolUsage:
    pool: str
    window: str
    budget_units: str
    soft_limit: int
    used: int
    attempts: int
    fallback_to_attempts: bool
    over_soft_limit: bool
    exhausted_until: datetime | None = None
    exhaustion_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "window": self.window,
            "budget_units": self.budget_units,
            "soft_limit": self.soft_limit,
            "used": self.used,
            "attempts": self.attempts,
            "counting": "attempts" if self.fallback_to_attempts else self.budget_units,
            "fallback_to_attempts": self.fallback_to_attempts,
            "over_soft_limit": self.over_soft_limit,
            "exhausted_until": self.exhausted_until.isoformat() if self.exhausted_until else None,
            "exhaustion_reason": self.exhaustion_reason,
        }


def launch_model_name(route: RoutingModel | None, model_id: str) -> str:
    """Hades #354: the name a launch passes to the harness - a routing entry's own
    `harness_model_name` when it carries one, else the lane name routing, pools and
    evidence always use. With no entry (an unrouted execution) the lane name itself."""
    return route.sent_model_name if route is not None else model_id


def routing_ref(policy_document: dict[str, Any]) -> tuple[str, int] | None:
    ref = (policy_document or {}).get("routing", {}).get("policy", {})
    if not ref:
        return None
    return str(ref.get("name")), int(ref.get("version", 0))


@dataclass(frozen=True, slots=True)
class RoutingResolution:
    """Which routing version a delivery policy routes with now, and why (hades #605).

    `named_version` is the version the policy document names; `version` is the one an
    attempt routes with. `why` says it in plain words for the Routing page."""

    name: str
    named_version: int
    version: int
    pinned: bool
    why: str


def resolve_routing(uow: UnitOfWork, policy_document: dict[str, Any]) -> RoutingResolution | None:
    """hades #605: the routing version a policy routes with at task start. Pinned keeps
    the named version. Unpinned (the default) follows the newest version of the named
    routing policy that is not retired, whether it was published from a page or only
    uploaded with PUT /routing/{name}/{version}, so a version the operator uploads is
    the one new attempts use. When every version is retired the named one is kept, and
    the caller refuses it as it refuses any retired version."""
    ref = routing_ref(policy_document)
    if ref is None:
        return None
    name, named = ref
    if (policy_document.get("routing") or {}).get("policy", {}).get("pinned") is True:
        return RoutingResolution(
            name=name,
            named_version=named,
            version=named,
            pinned=True,
            why=f"pinned: the delivery policy names version {named} and keeps it",
        )
    live = [
        record.version
        for record in uow.routing_policies.list_versions(name)
        if record.retired_at is None
    ]
    if not live:
        return RoutingResolution(
            name=name,
            named_version=named,
            version=named,
            pinned=False,
            why=(
                f"unpinned, but every version of {name} is retired, so the named version "
                f"{named} is kept"
            ),
        )
    newest = max(live)
    if newest == named:
        why = f"unpinned: version {named}, which the delivery policy names, is the newest"
    else:
        why = (
            f"unpinned: follows the newest version not retired; the delivery policy "
            f"names version {named}"
        )
    return RoutingResolution(name=name, named_version=named, version=newest, pinned=False, why=why)


def load_routing(
    uow: UnitOfWork, policy_document: dict[str, Any], *, in_force: bool = False
) -> RoutingPolicyV1 | None:
    """The routing version `policy_document` names, as written, or with `in_force` the
    version it routes with now (`resolve_routing`, hades #605). GET /routing/usage reads
    the version in force for the newest policy version and the named one for an
    explicitly selected policy version. An attempt routes with `load_attempt_routing`."""
    ref = routing_ref(policy_document)
    if ref is None:
        return None
    if in_force:
        resolved = resolve_routing(uow, policy_document)
        if resolved is not None:
            ref = (resolved.name, resolved.version)
    record = uow.routing_policies.get(*ref)
    if record is None:
        return None
    return RoutingPolicyV1.model_validate(record.document)


def current_routing_version(uow: UnitOfWork, policy_document: dict[str, Any]) -> int | None:
    """hades #254, #605: the routing version an attempt routes with now. A pinned
    reference keeps its version; an unpinned one follows the newest version of the
    routing policy it names that is not retired (`resolve_routing`), so a correction or
    retry never routes with a model the routing policy has since removed or disabled.
    The policy snapshot is not changed."""
    resolved = resolve_routing(uow, policy_document)
    return resolved.version if resolved is not None else None


def load_attempt_routing(
    uow: UnitOfWork, policy_document: dict[str, Any], routing_version: int | None = None
) -> RoutingPolicyV1 | None:
    """The routing policy for an attempt: the version recorded on it once it was
    routed, otherwise the version it would route with now (`current_routing_version`)."""
    ref = routing_ref(policy_document)
    if ref is None:
        return None
    version = (
        routing_version
        if routing_version is not None
        else current_routing_version(uow, policy_document)
    )
    record = uow.routing_policies.get(ref[0], version if version is not None else ref[1])
    if record is None:
        return None
    return RoutingPolicyV1.model_validate(record.document)


def check_selection(
    routing: RoutingPolicyV1, *, tier: str, harness: str, model_id: str
) -> list[Problem]:
    """05: the contract's model must be an enabled entry whose harness matches and whose
    capability the tier allows."""
    problems: list[Problem] = []
    tier_rule = routing.tiers.get(tier)
    if tier_rule is None:
        problems.append(
            {
                "path": "execution_request.tier",
                "message": f"tier {tier!r} is not in routing policy {routing.name}",
            }
        )
    entry = routing.model(model_id, harness)
    if entry is None:
        problems.append(
            {
                "path": "execution_request.model",
                "message": f"model {model_id!r} is not in routing policy {routing.name}",
            }
        )
        return problems
    if not entry.enabled:
        problems.append(
            {"path": "execution_request.model", "message": f"model {model_id!r} is disabled"}
        )
    if tier_rule is not None and entry.capability not in tier_rule.allowed_capability:
        problems.append(
            {
                "path": "execution_request.model",
                "message": (
                    f"tier {tier!r} allows {sorted(tier_rule.allowed_capability)}; "
                    f"{model_id!r} is {entry.capability}"
                ),
            }
        )
    return problems


def pool_usage(uow: UnitOfWork, routing: RoutingPolicyV1, pool: str, now: datetime) -> PoolUsage:
    spec = routing.pools[pool]
    since = now - timedelta(seconds=window_seconds(spec.window))
    routes = {(m.harness, m.model) for m in routing.models if m.pool == pool}
    rows = [
        m
        for m in uow.attempt_metrics.list_since(since=since, model=None, task_ids=None)
        if (m.harness, m.model) in routes
    ]
    attempts = len(rows)
    mark = uow.pool_exhaustions.get(pool)
    active = mark if mark and mark.cleared_at is None and mark.reset_at > now else None
    if spec.budget_units == "attempts":
        return PoolUsage(
            pool=pool,
            window=spec.window,
            budget_units=spec.budget_units,
            soft_limit=spec.soft_limit,
            used=attempts,
            attempts=attempts,
            fallback_to_attempts=False,
            over_soft_limit=spec.soft_limit > 0 and attempts >= spec.soft_limit,
            exhausted_until=active.reset_at if active else None,
            exhaustion_reason=active.reason if active else None,
        )
    values = [
        _budget_value(row, spec.budget_units)
        for row in rows
        if _budget_value(row, spec.budget_units) is not None
    ]
    # 05b: a harness that reports no token counts records null, and the pool falls back
    # to counting attempts, which GET /routing/usage states.
    fallback = not values and attempts > 0
    used = attempts if fallback else int(sum(v or 0 for v in values))
    return PoolUsage(
        pool=pool,
        window=spec.window,
        budget_units=spec.budget_units,
        soft_limit=spec.soft_limit,
        used=used,
        attempts=attempts,
        fallback_to_attempts=fallback,
        over_soft_limit=spec.soft_limit > 0 and used >= spec.soft_limit,
        exhausted_until=active.reset_at if active else None,
        exhaustion_reason=active.reason if active else None,
    )


def _budget_value(metrics: AttemptMetrics, unit: str) -> float | None:
    if unit == "tokens_out":
        return None if metrics.tokens_out is None else float(metrics.tokens_out)
    if unit == "cost_units":
        return metrics.cost_units
    return 1.0


def usage_report(uow: UnitOfWork, routing: RoutingPolicyV1, now: datetime) -> list[dict[str, Any]]:
    return [pool_usage(uow, routing, pool, now).as_dict() for pool in sorted(routing.pools)]


def check_quota(
    uow: UnitOfWork, routing: RoutingPolicyV1, *, model_id: str, harness: str, now: datetime
) -> Problem | None:
    entry = routing.model(model_id, harness)
    if entry is None:
        return None
    usage = pool_usage(uow, routing, entry.pool, now)
    if usage.exhausted_until is not None:
        exhausted_until = usage.exhausted_until.isoformat()
        return {
            "path": "execution_request.tier",
            "message": f"quota pool {entry.pool} is exhausted until {exhausted_until}",
        }
    if usage.over_soft_limit:
        return {
            "path": "execution_request.model",
            "message": (
                f"quota pool {entry.pool} is at {usage.used} of its soft limit "
                f"{usage.soft_limit} for the current {usage.window} window"
            ),
        }
    return None


@dataclass(frozen=True, slots=True)
class Reservation:
    """What the launch-time reservation recorded. The fake provider consumes nothing, so
    the reservation is structural: the row that C3's real launch will decrement."""

    model: str
    harness: str
    endpoint_kind: str
    pool: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Selection:
    selected: RoutingModel | None
    image: str | None
    candidates: tuple[dict[str, Any], ...]


def image_for_harness(uow: UnitOfWork, harness: str, provider: str) -> str | None:
    """The harness's own default worker image (13, ADR 0018). Promotion is per harness,
    so another harness's default never answers for this one."""
    default = uow.harness_images.get(harness)
    if default is not None:
        return default.reference
    # The fake provider is an in-process test double and has no image manifest.
    if provider == "fake":
        return "crucible-worker:fake-succeed"
    return None


def _project_metrics(
    uow: UnitOfWork, project: str, models: list[str], quality_window: int
) -> dict[str, list[AttemptMetrics]]:
    rows = uow.attempt_metrics.recent_for_project(
        project=project, models=models, limit_per_model=quality_window
    )
    grouped: dict[str, list[AttemptMetrics]] = {}
    for row in rows:
        grouped.setdefault(row.model, []).append(row)
    for values in grouped.values():
        values.sort(
            key=lambda item: (item.created_at or datetime.min.replace(tzinfo=UTC), item.attempt_id)
        )
    return grouped


def select_model(
    uow: UnitOfWork,
    routing: RoutingPolicyV1,
    *,
    tier: str,
    project: str,
    provider: str,
    now: datetime,
    contract: dict[str, Any] | None = None,
    policy_document: dict[str, Any] | None = None,
    eligible_harnesses: set[str] | None = None,
    harnesses: HarnessRegistry | None = None,
    image_allowlist: list[str] | None = None,
    excluded_pools: set[str] | None = None,
    excluded_routes: set[tuple[str, str]] | None = None,
    pinned_model: str | None = None,
    pinned_harness: str | None = None,
) -> Selection:
    """Return the same answer for the same rows, including a reason for every exclusion."""
    tier_rule = routing.tiers.get(tier)
    if tier_rule is None:
        return Selection(None, None, tuple())
    metrics = _project_metrics(
        uow, project, [entry.id for entry in routing.models], routing.rotation.quality_window
    )
    has_task_check = bool(task_specific_checks(contract or {}, policy_document or {}))
    local_pools = set(routing.local_pools())
    preferred = routing.preferred_pools(tier)
    probing: set[str] | None = None
    ranked: list[tuple[tuple[Any, ...], RoutingModel, str, list[str], QualityState]] = []
    for entry in routing.models:
        reasons: list[str] = []
        if not has_task_check and (entry.endpoint == "local" or entry.pool in local_pools):
            reasons.append("no task-specific check")
        if pinned_model is not None and entry.id != pinned_model:
            reasons.append("not the operator pin")
        if pinned_harness is not None and entry.harness != pinned_harness:
            reasons.append("harness does not match the operator pin")
        if not entry.enabled:
            reasons.append("model disabled")
        if (
            eligible_harnesses is not None
            and entry.harness not in eligible_harnesses
            and not (
                entry.harness == "codex"
                and entry.endpoint == "local"
                and "codex:local" in eligible_harnesses
            )
        ):
            reasons.append("harness disabled or has no credential")
        if entry.capability not in tier_rule.allowed_capability:
            reasons.append(f"capability {entry.capability} is not allowed for tier {tier}")
        usage = pool_usage(uow, routing, entry.pool, now)
        if usage.over_soft_limit:
            reasons.append("pool is at its soft limit")
        if usage.exhausted_until is not None:
            reasons.append(f"pool exhausted until {usage.exhausted_until.isoformat()}")
        model_until = model_excluded_until(uow, entry.model, entry.harness, now)
        if model_until is not None:
            reasons.append(f"model excluded until {model_until.isoformat()}")
        if excluded_routes and (entry.harness, entry.model) in excluded_routes:
            reasons.append("model refused capacity for this retry")
        if excluded_pools and entry.pool in excluded_pools:
            reasons.append("pool excluded for the current quota reroute")
        image = image_for_harness(uow, entry.harness, provider)
        if image is None:
            reasons.append("selected harness has no default image")
            image = ""
        elif provider != "fake":
            default = uow.harness_images.get(entry.harness)
            if default is None or default.reference != image:
                reasons.append("derived image is unknown or retired")
            elif harnesses is not None:
                adapter = harnesses.get(entry.harness)
                if adapter is None:
                    reasons.append("derived image names an unknown harness")
                elif not adapter.supported_versions.supports(default.version):
                    reasons.append(
                        "derived image harness version is outside the adapter supported range"
                    )
            if image_allowlist and not any(
                fnmatch.fnmatchcase(image, pattern) for pattern in image_allowlist
            ):
                reasons.append("derived image is outside the policy allowlist")
        # ADR 0028: the tier's pool order first, then its capability preference. A
        # model in a preferred pool is the default; the rest are fallbacks, tried when
        # every preferred one is excluded above.
        pool_rank = preferred.index(entry.pool) if entry.pool in preferred else len(preferred)
        cap_rank = (
            tier_rule.prefer.index(entry.capability)
            if entry.capability in tier_rule.prefer
            else len(tier_rule.prefer)
        )
        recent = metrics.get(entry.id, [])[-routing.rotation.quality_window :]
        quality = quality_state(routing, recent, now)
        if quality.probe:
            if probing is None:
                probing = _routed_not_launched(uow, project)
            if entry.id in probing:
                # The probe is taken: an attempt routed to it has no metrics row yet.
                quality = replace(quality, probe=False)
        last = recent[-1].created_at if recent else None
        weight = max(entry.weight, 1)
        age_weight = (now - last).total_seconds() * weight if last is not None else 0.0
        rank = (
            int(quality.demoted and not quality.probe),
            pool_rank,
            0
            if entry.endpoint == "local"
            and entry.harness == "codex"
            and tier in ("trivial", "standard")
            else 1,
            cap_rank,
            0 if last is None else 1,
            -age_weight,
            entry.id,
        )
        ranked.append((rank, entry, image, reasons, quality))
    ranked.sort(key=lambda item: item[0])
    ordered = tuple(
        {
            "model": entry.id,
            "harness": entry.harness,
            "pool": entry.pool,
            "capability": entry.capability,
            "image": image or None,
            "eligible": not reasons,
            "excluded": reasons,
            "capacity_refused": bool(
                excluded_routes and (entry.harness, entry.model) in excluded_routes
            ),
            "preferred_pool": entry.pool in preferred,
            "quality": quality.as_dict(),
        }
        for _, entry, image, reasons, quality in ranked
    )
    chosen = next(((entry, image) for _, entry, image, reasons, _ in ranked if not reasons), None)
    return Selection(chosen[0] if chosen else None, chosen[1] if chosen else None, ordered)


def _routed_not_launched(uow: UnitOfWork, project: str) -> set[str]:
    """Models with an attempt on `project` that routing chose and whose launch has not
    yet written its metrics row (it is written at launch). One such attempt is the
    probe of a demoted model, so a second task in the same tick falls back."""
    out: set[str] = set()
    for attempt in uow.attempts.list_in_states([AttemptState.PREPARING, AttemptState.LAUNCHING]):
        if attempt.selected_model is None or attempt.selected_model in out:
            continue
        task = uow.tasks.get(attempt.task_id)
        if task is not None and task.project == project:
            out.add(attempt.selected_model)
    return out


def count_blocking_failures(gates: Iterable[Any]) -> int:
    """ADR 0028: the gate failures routing judges a model on. A failed advisory gate
    (ADR 0024) is a finding for the reviewer, not a failure; a gate result from before
    that classification is blocking."""
    return sum(
        1 for gate in gates if gate.result in ("fail", "error") and getattr(gate, "blocking", True)
    )


@dataclass(frozen=True, slots=True)
class QualityState:
    """A model's standing in a project (ADR 0028). `demoted` ranks it after every model
    that is not, until `probe` says its turn to prove itself again has come."""

    sample: int
    failures: int
    demoted: bool
    probe: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample": self.sample,
            "blocking_failures": self.failures,
            "demoted": self.demoted,
            "probe": self.probe,
        }


def quality_state(
    routing: RoutingPolicyV1, recent: list[AttemptMetrics], now: datetime
) -> QualityState:
    """Judge a failure rate, not a single failure. The sample is the attempts in the
    window that reached the gates; a failure is one with a failed blocking gate (the
    supervisor counts only those into `gates_failed`, so advisory findings never count).
    Demotion needs the minimum sample, the failure percentage, and two failures at
    least. A demoted model whose last attempt is `probe_after_minutes` old ranks as if
    it were not demoted, once: the probe's launch makes its last attempt new again."""
    rotation = routing.rotation
    judged = [row for row in recent if row.gates_passed + row.gates_failed > 0]
    failures = sum(1 for row in judged if row.gates_failed > 0)
    demoted = (
        rotation.quality_feedback
        and len(judged) >= rotation.demote_min_sample
        and failures >= 2
        and failures * 100 >= rotation.demote_failure_percent * len(judged)
    )
    last = recent[-1].created_at if recent else None
    probe = (
        demoted
        and last is not None
        and now - last >= timedelta(minutes=rotation.probe_after_minutes)
    )
    return QualityState(sample=len(judged), failures=failures, demoted=demoted, probe=probe)


def reserve(
    uow: UnitOfWork,
    policy_document: dict[str, Any],
    *,
    harness: str,
    model_id: str,
    now: datetime,
    routing_version: int | None = None,
) -> Reservation:
    """The authoritative pool check at attempt launch (05b). Called inside the fenced
    transaction that moves the attempt to `launching`, against the routing version the
    attempt was routed with (hades #254)."""
    routing = load_attempt_routing(uow, policy_document, routing_version)
    entry: RoutingModel | None = routing.model(model_id, harness) if routing else None
    if routing is None or entry is None:
        return Reservation(
            model=model_id,
            harness=harness,
            endpoint_kind="unknown",
            pool="unrouted",
            ok=True,
            detail="no routing policy entry; nothing to reserve",
        )
    problem = check_quota(uow, routing, model_id=model_id, harness=harness, now=now)
    if problem is not None:
        return Reservation(
            model=model_id,
            harness=harness,
            endpoint_kind=entry.endpoint,
            pool=entry.pool,
            ok=False,
            detail=str(problem["message"]),
        )
    return Reservation(
        model=model_id,
        harness=harness,
        endpoint_kind=entry.endpoint,
        pool=entry.pool,
        ok=True,
        detail=f"pool {entry.pool} is under its soft limit",
    )


def history(
    uow: UnitOfWork,
    *,
    model: str | None,
    project: str | None,
    since: datetime | None,
) -> list[dict[str, Any]]:
    """Per-model outcomes Foundry reads before selecting (04, 05b)."""
    task_ids: list[str] | None = None
    if project is not None:
        task_ids = [
            t.id
            for t in uow.tasks.search(
                state=None,
                project=project,
                repository_id=None,
                external_id=None,
                updated_since=None,
                after_id=None,
                limit=1000,
            )
        ]
    rows = uow.attempt_metrics.list_since(since=since, model=model, task_ids=task_ids)
    return [
        {
            "attempt_id": m.attempt_id,
            "task_id": m.task_id,
            "model": m.model,
            "model_reported": m.model_reported,
            "harness": m.harness,
            "endpoint_kind": m.endpoint_kind,
            "pool": m.pool,
            "wall_ms": m.wall_ms,
            "tokens_in": m.tokens_in,
            "tokens_out": m.tokens_out,
            "cost_units": m.cost_units,
            "cost_source": m.cost_source,
            "exit_class": m.exit_class,
            "gates_passed": m.gates_passed,
            "gates_failed": m.gates_failed,
            "corrections_after": m.corrections_after,
            "acceptance_verdict": m.acceptance_verdict,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in rows
    ]
