"""Hades #254: every attempt routes with the routing version in force when it is routed.

A correction runs as a new attempt inside the task's history, and its execution carries
the policy snapshot recorded when the task was admitted. When that policy's routing
reference is unpinned, the attempt routes with the newest routing version, so a model
removed or disabled since is never chosen; a pinned reference keeps its version. The
version used is recorded on the attempt, and the policy snapshot stays as recorded.

The supervisor routes the correction against the in-memory store the hades #360 tests
use (the unit tier has no Postgres), with a routing repository holding two versions.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.application.errors import ConflictError, ContractValidationError
from crucible.application.policies import put_routing_policy, validate_policy
from crucible.application.queries import _attempt_summary
from crucible.application.routing import current_routing_version, load_attempt_routing
from crucible.application.supervisor import _Pending
from crucible.domain.entities import (
    Attempt,
    AttemptMetrics,
    Execution,
    ExecutionRole,
    Lease,
    Principal,
    Role,
    RoutingPolicyRecord,
)
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.ports.execution import Workspace
from tests.fixtures import FakeClock
from tests.unit.test_issue_360_ready_for_merge_correction import (
    NOW,
    TASK_ID,
    _attach,
    _correction,
    _correction_attempt,
    _Leases,
    _NoHistory,
    _ready_for_merge,
    _routing,
    _Store,
    _supervisor,
)


class _RoutingVersions:
    """The routing policy repository over every stored version of every routing policy.
    Every version is referenced by a policy, as `publish_routing` leaves it, unless it
    is named in `unpublished` (uploaded with PUT /routing/{name}/{version} only)."""

    def __init__(
        self, records: Sequence[RoutingPolicyRecord], unpublished: Sequence[int] = ()
    ) -> None:
        self.records = list(records)
        self.unpublished = set(unpublished)

    def is_referenced(self, name: str, version: int) -> bool:
        return version not in self.unpublished and self.get(name, version) is not None

    def get(self, name: str, version: int) -> RoutingPolicyRecord | None:
        return next(
            (r for r in self.records if (r.name, r.version) == (name, version)),
            None,
        )

    def list_versions(self, name: str) -> list[RoutingPolicyRecord]:
        return sorted((r for r in self.records if r.name == name), key=lambda r: r.version)


def _model(model_id: str, *, enabled: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = copy.deepcopy(_routing().document["models"][0])
    entry["id"] = model_id
    entry["enabled"] = enabled
    return entry


def _version(version: int, models: list[dict[str, Any]], **fields: Any) -> RoutingPolicyRecord:
    document = copy.deepcopy(_routing().document)
    document["version"] = version
    document["models"] = models
    return RoutingPolicyRecord(
        name="default-routing",
        version=version,
        document=document,
        created_at=NOW + timedelta(minutes=version),
        **fields,
    )


def _store(newest: RoutingPolicyRecord, *, pinned: bool | None = False) -> _Store:
    """A task ready for merge whose policy names default-routing version 3, and a newer
    routing version published after the task was admitted."""
    store = _ready_for_merge()
    ref = store.policies.policy.document["routing"]["policy"]
    assert ref == {"name": "default-routing", "version": 3}
    if pinned is not None:
        ref["pinned"] = pinned
    store.routing_policies = _RoutingVersions([_routing(), newest])  # type: ignore[assignment]
    return store


def _route_correction(store: _Store, tmp_path: Path) -> tuple[Execution, Attempt]:
    clock = FakeClock(NOW)
    _attach(store, _correction(), clock)
    supervisor, _provider = _supervisor(store, clock, tmp_path)
    supervisor._materialize_scheduled()
    execution, attempt = _correction_attempt(store)
    assert attempt.state is AttemptState.PENDING
    task = store.tasks.get(attempt.task_id)
    stored = store.contracts.get(attempt.task_id, execution.contract_version)
    assert task is not None and stored is not None
    routed = supervisor._route_pending(_Pending(attempt, execution, task, stored.document))
    assert routed is not None, "the correction was not routed"
    return _correction_attempt(store)


def _routed_payload(store: _Store, attempt: Attempt) -> dict[str, Any]:
    return next(
        e.payload
        for e in store.events.rows
        if e.kind == EventKind.ATTEMPT_ROUTED.value and e.attempt_id == attempt.id
    )


def test_a_correction_routes_with_the_newest_unpinned_routing_version(tmp_path: Path) -> None:
    store = _store(_version(4, [_model("gpt-test", enabled=False), _model("gpt-new")]))

    execution, attempt = _route_correction(store, tmp_path)

    assert attempt.state is AttemptState.PREPARING
    assert attempt.selected_model == "gpt-new"
    assert execution.model == "gpt-new"
    assert attempt.routing_version == 4
    assert _routed_payload(store, attempt)["routing_policy"] == {
        "name": "default-routing",
        "version": 4,
    }
    # The snapshot stays as recorded, for reproducibility.
    assert execution.policy_snapshot["routing"]["policy"]["version"] == 3


def test_an_explicit_unpinned_reference_follows_the_newest_version(tmp_path: Path) -> None:
    store = _store(_version(4, [_model("gpt-new")]), pinned=None)
    store.policies.policy.document["routing"]["policy"]["pinned"] = False

    _execution, attempt = _route_correction(store, tmp_path)

    assert attempt.selected_model == "gpt-new"
    assert attempt.routing_version == 4


def test_a_model_removed_in_the_newest_version_is_not_selected_for_a_correction(
    tmp_path: Path,
) -> None:
    store = _store(_version(4, [_model("gpt-new")]))

    execution, attempt = _route_correction(store, tmp_path)

    assert attempt.selected_model != "gpt-test"
    assert attempt.selected_model == "gpt-new"
    assert all(c["model"] != "gpt-test" for c in attempt.ordered_candidates)
    assert execution.policy_snapshot["routing"]["policy"]["version"] == 3


def test_a_pinned_reference_keeps_its_version(tmp_path: Path) -> None:
    store = _store(_version(4, [_model("gpt-new")]), pinned=True)

    execution, attempt = _route_correction(store, tmp_path)

    assert attempt.selected_model == "gpt-test"
    assert attempt.routing_version == 3
    assert _routed_payload(store, attempt)["routing_policy"]["version"] == 3
    assert execution.policy_snapshot["routing"]["policy"] == {
        "name": "default-routing",
        "version": 3,
        "pinned": True,
    }


def test_a_retired_version_is_not_the_current_one() -> None:
    store = _store(_version(4, [_model("gpt-new")], retired_at=NOW))
    policy = store.policies.policy.document

    assert current_routing_version(store.uow(), policy) == 3
    routing = load_attempt_routing(store.uow(), policy)
    assert routing is not None and routing.version == 3


def test_a_routed_attempt_keeps_the_version_it_was_routed_with() -> None:
    """The launch, the pool reservation and the exit read the version on the attempt,
    not one published after it was routed."""
    store = _store(_version(4, [_model("gpt-new")]))
    policy = store.policies.policy.document

    routing = load_attempt_routing(store.uow(), policy, 3)
    assert routing is not None and routing.version == 3
    assert routing.model("gpt-test", "codex") is not None
    current = load_attempt_routing(store.uow(), policy)
    assert current is not None and current.version == 4


def test_a_version_only_uploaded_is_not_the_current_one(tmp_path: Path) -> None:
    """A version no policy references was never published: publish_routing has not set
    the egress for it, so an unpinned correction keeps routing with the published one."""
    store = _store(_version(4, [_model("gpt-new")]))
    store.routing_policies.unpublished = {4}  # type: ignore[attr-defined]

    assert current_routing_version(store.uow(), store.policies.policy.document) == 3
    _execution, attempt = _route_correction(store, tmp_path)
    assert attempt.selected_model == "gpt-test"
    assert attempt.routing_version == 3


def test_a_retired_reference_never_falls_back_to_an_older_version() -> None:
    """The referenced version retired with nothing newer published: the attempt keeps the
    referenced version, not an older live one."""
    store = _store(_version(4, [_model("gpt-new")]))
    store.routing_policies.records.insert(  # type: ignore[attr-defined]
        0, _version(2, [_model("gpt-old")])
    )
    store.routing_policies.records = [  # type: ignore[attr-defined]
        replace(r, retired_at=NOW) if r.version in (3, 4) else r
        for r in store.routing_policies.records  # type: ignore[attr-defined]
    ]

    assert current_routing_version(store.uow(), store.policies.policy.document) == 3


class _Metrics(_NoHistory):
    """Routing's empty history, keeping the metrics row the reservation writes."""

    def __init__(self, rows: list[AttemptMetrics]) -> None:
        self.rows = rows

    def put(self, metrics: AttemptMetrics) -> None:
        self.rows.append(metrics)


def _local(version: int) -> RoutingPolicyRecord:
    """A routing version that moved gpt-new to a local endpoint in a pool of its own."""
    model = _model("gpt-new")
    model.update(endpoint="local", endpoint_url="http://gateway:4000/v1", pool="gateway")
    record = _version(version, [model])
    record.document["pools"]["gateway"] = dict(record.document["pools"]["openai-sub"])
    return record


@pytest.mark.asyncio
async def test_the_launch_reservation_and_exit_read_the_version_routed_with(
    tmp_path: Path,
) -> None:
    """Routed at v4, then v5 moves the model to a local endpoint in another pool: the
    spec, the pool reservation and the local cap check all still use v4."""
    store = _store(_version(4, [_model("gpt-test", enabled=False), _model("gpt-new")]))
    execution, attempt = _route_correction(store, tmp_path)
    assert attempt.routing_version == 4
    store.routing_policies.records.append(_local(5))  # type: ignore[attr-defined]
    assert current_routing_version(store.uow(), execution.policy_snapshot) == 5
    supervisor, _provider = _supervisor(store, FakeClock(NOW), tmp_path)

    requested: list[str] = []

    def setting(name: str) -> None:
        requested.append(name)

    store.provider_settings = SimpleNamespace(get=setting)  # type: ignore[assignment]
    task = store.tasks.get(attempt.task_id)
    stored = store.contracts.get(attempt.task_id, execution.contract_version)
    assert task is not None and stored is not None
    await supervisor._build_spec(attempt, execution, task, stored.document)
    # A local codex endpoint would read the Hermes settings instead.
    assert requested == ["harness.codex"]

    metrics: list[AttemptMetrics] = []
    store.attempt_metrics = _Metrics(metrics)
    root = tmp_path / "ws"
    workspace = Workspace(
        attempt_id=attempt.id,
        checkout_path=str(root / "repo"),
        identity_path=str(root / "identity"),
        report_path=str(root / "report"),
    )
    assert supervisor._mark_launching(attempt.id, workspace)
    reserved = next(
        e.payload
        for e in store.events.rows
        if e.kind == EventKind.QUOTA_RESERVED.value and e.attempt_id == attempt.id
    )
    assert reserved["pool"] == "openai-sub"
    assert [(m.endpoint_kind, m.pool) for m in metrics] == [("subscription", "openai-sub")]

    launched = store.attempts.get(attempt.id)
    assert launched is not None
    assert (
        supervisor._local_cap(
            store.uow(), execution, launched, ExitClass.TIMEOUT, turn_cap_reached=True
        )
        is None
    )
    # The v5 entry would have named the local turn cap.
    assert (
        supervisor._local_cap(
            store.uow(), execution, replace(launched, routing_version=5), ExitClass.TIMEOUT, True
        )
        == "turns"
    )


def _review(store: _Store) -> Attempt:
    """A pending internal review of the task: reviews are never routed."""
    task = store.tasks.get(TASK_ID)
    assert task is not None
    task.state = TaskState.AWAITING_INTERNAL_REVIEW
    implement = store.executions.list_for_task(TASK_ID)[0]
    review = replace(
        implement,
        id="01EXEC254REVIEW0000000001",
        role=ExecutionRole.REVIEW,
        state=ExecutionState.CREATED,
    )
    store.executions.add(review)
    attempt = Attempt(
        id="01ATTEMPT254REVIEW0000001",
        execution_id=review.id,
        task_id=TASK_ID,
        number=1,
        state=AttemptState.PENDING,
        created_at=NOW,
    )
    store.attempts.add(attempt)
    return attempt


def test_a_review_records_the_routing_version_it_launches_with(tmp_path: Path) -> None:
    store = _store(_version(4, [_model("gpt-test")]))
    attempt = _review(store)
    supervisor, _provider = _supervisor(store, FakeClock(NOW), tmp_path)

    started, busy, refusal = supervisor._mark_review_preparing(attempt.id)

    assert started is not None and busy is None and refusal is None
    preparing = store.attempts.get(attempt.id)
    assert preparing is not None and preparing.state is AttemptState.PREPARING
    assert preparing.routing_version == 4


def test_a_review_whose_model_is_disabled_now_is_refused(tmp_path: Path) -> None:
    store = _store(_version(4, [_model("gpt-test", enabled=False), _model("gpt-new")]))
    attempt = _review(store)
    supervisor, _provider = _supervisor(store, FakeClock(NOW), tmp_path)

    _started, _busy, refusal = supervisor._mark_review_preparing(attempt.id)

    assert refusal is not None and "gpt-test is disabled in routing policy" in refusal
    assert "default-routing/4" in refusal


def test_a_version_an_attempt_routed_with_cannot_be_rewritten() -> None:
    record = _version(4, [_model("gpt-new")])
    uow: Any = SimpleNamespace(
        routing_policies=SimpleNamespace(
            get=lambda name, version: record, is_referenced=lambda name, version: False
        ),
        attempts=SimpleNamespace(
            routes_with=lambda name, version: (
                (name, version)
                == (
                    "default-routing",
                    4,
                )
            )
        ),
    )
    principal = Principal(id="op", name="operator", role=Role.OPERATOR, created_at=NOW)

    with pytest.raises(ConflictError, match="routed with by an attempt"):
        put_routing_policy(
            uow,
            FakeClock(NOW),
            principal=principal,
            name="default-routing",
            version=4,
            document=copy.deepcopy(record.document),
        )


def _two_pools(
    version: int, *, fallback_enabled: bool = True, first_enabled: bool = True
) -> RoutingPolicyRecord:
    """gpt-test in openai-sub and gpt-z-fallback in a pool of its own."""
    fallback = _model("gpt-z-fallback", enabled=fallback_enabled)
    fallback["harness"] = "script-harness"
    fallback["pool"] = "fallback-pool"
    first = _model("gpt-test", enabled=first_enabled)
    first["harness"] = "script-harness"
    record = _version(version, [first, fallback])
    record.document["pools"]["fallback-pool"] = dict(record.document["pools"]["openai-sub"])
    return record


class _NoExhaustions(_NoHistory):
    """No pool is marked exhausted."""

    def list_all(self) -> list[Any]:
        return []


def _quota_exit(store: _Store, tmp_path: Path) -> tuple[Any, Execution, Attempt, Attempt]:
    """Route the correction at v4, publish v5 disabling gpt-z-fallback, and let the
    correction's attempt exit on quota. Returns the supervisor, the execution, the
    exhausted attempt and the reroute's successor."""
    execution, attempt = _route_correction(store, tmp_path)
    assert attempt.routing_version == 4
    assert attempt.selected_model == "gpt-test"
    store.routing_policies.records.append(  # type: ignore[attr-defined]
        _two_pools(5, fallback_enabled=False)
    )
    store.pool_exhaustions = _NoExhaustions()
    task = store.tasks.get(attempt.task_id)
    assert task is not None
    supervisor, _provider = _supervisor(store, FakeClock(NOW), tmp_path)
    supervisor._handle_quota_exit(store.uow(), task, execution, attempt, source="worker")
    attempt.state = AttemptState.FAILED
    store.attempts.save(attempt)
    later = [a for a in store.attempts.list_for_task(task.id) if a.number > attempt.number]
    return supervisor, execution, attempt, later[0] if later else attempt


def test_a_quota_exit_never_reroutes_to_a_model_disabled_in_the_current_version(
    tmp_path: Path,
) -> None:
    """The reroute routes with the version in force now, with the exhausted pool
    excluded: gpt-z-fallback, disabled in v5, is never selected."""
    store = _store(_two_pools(4))

    supervisor, execution, attempt, nxt = _quota_exit(store, tmp_path)

    assert all(a.selected_model != "gpt-z-fallback" for a in store.attempts.rows.values())
    if nxt.id != attempt.id:
        assert nxt.routing_version is None
        task = store.tasks.get(nxt.task_id)
        stored = store.contracts.get(nxt.task_id, execution.contract_version)
        assert task is not None and stored is not None
        assert supervisor._route_pending(_Pending(nxt, execution, task, stored.document)) is None
        routed = store.attempts.get(nxt.id)
        assert routed is not None and routed.selected_model is None
        assert routed.state is not AttemptState.PREPARING
    assert execution.policy_snapshot["routing"]["policy"]["version"] == 3


def test_a_quota_reroute_uses_the_current_version_when_it_still_allows_the_fallback(
    tmp_path: Path,
) -> None:
    store = _store(_two_pools(4))
    store.routing_policies.records.append(_two_pools(5))  # type: ignore[attr-defined]
    execution, attempt = _route_correction(store, tmp_path)
    assert attempt.routing_version == 5
    task = store.tasks.get(attempt.task_id)
    assert task is not None
    supervisor, _provider = _supervisor(store, FakeClock(NOW), tmp_path)
    supervisor._handle_quota_exit(store.uow(), task, execution, attempt, source="worker")
    attempt.state = AttemptState.FAILED
    store.attempts.save(attempt)
    (nxt,) = [a for a in store.attempts.list_for_task(task.id) if a.number > attempt.number]
    assert nxt.number == attempt.number + 1
    assert nxt.routing_version is None
    assert nxt.routing_excluded_pools == ["openai-sub"]
    stored = store.contracts.get(nxt.task_id, execution.contract_version)
    assert stored is not None
    assert supervisor._route_pending(_Pending(nxt, execution, task, stored.document)) is not None
    routed = store.attempts.get(nxt.id)
    assert routed is not None
    assert routed.selected_model == "gpt-z-fallback"
    assert routed.routing_version == 5


class _CheckoutLeases(_Leases):
    """The checkout leases _begin_launch takes and releases, held in memory."""

    def __init__(self) -> None:
        super().__init__()
        self.checkout: dict[str, Lease] = {}

    def acquire_checkout_lease(
        self, key: str, holder: str, fenced_token: int, now: datetime, ttl_seconds: int
    ) -> Lease | None:
        held = self.checkout.get(key)
        if held is not None and held.holder != holder:
            return None
        lease = Lease(
            id=key,
            kind="checkout",
            key=key,
            holder=holder,
            fenced_token=fenced_token,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self.checkout[key] = lease
        return lease

    def get_checkout_lease(self, key: str) -> Lease | None:
        return self.checkout.get(key)

    def list_checkout_leases(self) -> list[Any]:
        return list(self.checkout.values())

    def release_checkout_lease(self, key: str, holder: str) -> bool:
        if (held := self.checkout.get(key)) is None or held.holder != holder:
            return False
        del self.checkout[key]
        return True


def _pending_successor(store: _Store, tmp_path: Path) -> tuple[Any, _Pending]:
    """The correction routed at v4 on gpt-test exited on quota in openai-sub, which is
    still cooling: its pending successor excludes that pool."""
    store.pool_exhaustions = _NoExhaustions()
    store.leases = _CheckoutLeases()
    execution, attempt = _route_correction(store, tmp_path)
    attempt.state = AttemptState.FAILED
    store.attempts.save(attempt)
    supervisor, _provider = _supervisor(store, FakeClock(NOW), tmp_path)
    successor = supervisor._create_attempt(
        store.uow(), execution, number=attempt.number + 1, excluded_pools={"openai-sub"}
    )
    task = store.tasks.get(attempt.task_id)
    stored = store.contracts.get(attempt.task_id, execution.contract_version)
    assert task is not None and stored is not None
    return supervisor, _Pending(successor, execution, task, stored.document)


@pytest.mark.asyncio
async def test_the_launch_preview_and_routing_agree_on_a_cooling_pool(tmp_path: Path) -> None:
    """Through _begin_launch: the preview excludes the cooling pool as routing does, and
    the successor launches on the fallback under the current version."""
    store = _store(_two_pools(4))
    supervisor, pending = _pending_successor(store, tmp_path)
    store.routing_policies.records.append(_two_pools(5))  # type: ignore[attr-defined]

    result = await supervisor._begin_launch(pending)

    assert result is not None
    launched = store.attempts.get(pending.attempt.id)
    assert launched is not None and launched.state is AttemptState.PREPARING
    assert launched.selected_model == "gpt-z-fallback"
    assert launched.routing_version == 5
    assert [lease.holder for lease in store.leases.list_checkout_leases()] == [launched.id]


@pytest.mark.asyncio
async def test_a_pending_attempt_is_never_stranded_when_the_current_version_refuses(
    tmp_path: Path,
) -> None:
    """The successor carries a stale recorded version under which gpt-z-fallback was
    enabled; v5 disables both models and openai-sub is cooling. The preview and routing both use
    v5, so _begin_launch refuses the attempt rather than moving it to preparing with no
    lease and no launch."""
    store = _store(_two_pools(4))
    supervisor, pending = _pending_successor(store, tmp_path)
    pending.attempt.routing_version = 4
    store.attempts.save(pending.attempt)
    store.routing_policies.records.append(  # type: ignore[attr-defined]
        _two_pools(5, fallback_enabled=False, first_enabled=False)
    )

    result = await supervisor._begin_launch(pending)

    assert result is None
    ended = store.attempts.get(pending.attempt.id)
    assert ended is not None
    assert ended.selected_model is None
    assert ended.state not in (AttemptState.PREPARING, AttemptState.PENDING)
    assert store.leases.list_checkout_leases() == []
    assert not any(
        e.kind == EventKind.ATTEMPT_PREPARING.value and e.attempt_id == pending.attempt.id
        for e in store.events.rows
    )


def test_a_quota_wait_resumes_on_the_current_routing_version(tmp_path: Path) -> None:
    """Routed at v4 and then waiting for quota: v5, published during the wait, disables
    gpt-test and adds gpt-new, so the wait resumes on gpt-new under v5."""
    store = _store(_version(4, [_model("gpt-test")]))
    execution, attempt = _route_correction(store, tmp_path)
    assert attempt.routing_version == 4
    store.routing_policies.records.append(  # type: ignore[attr-defined]
        _version(5, [_model("gpt-test", enabled=False), _model("gpt-new")])
    )
    store.pool_exhaustions = _NoExhaustions()
    task = store.tasks.get(attempt.task_id)
    assert task is not None
    task.state = TaskState.AWAITING_QUOTA
    task.quota_wait_started_at = NOW
    task.resume_at = NOW
    store.tasks.save(task)
    execution.state = ExecutionState.ACTIVE
    store.executions.save(execution)
    supervisor, _provider = _supervisor(store, FakeClock(NOW + timedelta(seconds=1)), tmp_path)

    supervisor._resume_quota_waits()

    resumed = next(
        e.payload for e in store.events.rows if e.kind == EventKind.TASK_QUOTA_RESUMED.value
    )
    assert resumed["model"] == "gpt-new"
    task = store.tasks.get(attempt.task_id)
    assert task is not None and task.state is TaskState.SCHEDULED
    assert execution.policy_snapshot["routing"]["policy"]["version"] == 3


@pytest.mark.parametrize("pinned", ["true", 1])
def test_pinned_must_be_a_strict_bool(pinned: object) -> None:
    """A string or number for pinned is refused, not read as unpinned."""
    document = copy.deepcopy(_ready_for_merge().policies.policy.document)
    document["routing"]["policy"]["pinned"] = pinned

    with pytest.raises(ContractValidationError) as raised:
        validate_policy(document, name=document["name"], version=document["version"])
    assert any("pinned" in problem["path"] for problem in raised.value.errors)


def test_the_attempt_view_shows_the_routing_version(tmp_path: Path) -> None:
    store = _store(_version(4, [_model("gpt-new")]))
    _execution, attempt = _route_correction(store, tmp_path)

    summary = _attempt_summary(attempt)

    assert summary.routing_version == 4
    assert summary.model_dump(mode="json")["routing_version"] == 4
