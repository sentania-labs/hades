"""Hades #359: a review attempt holds a pool slot on the route its routing entry
verifies, every attempt records the routing version it was launched under, and the
strictest pool cap across routing versions binds."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor, _Pending
from crucible.contracts.policy import RoutingPolicyV1
from crucible.domain.entities import (
    Attempt,
    Execution,
    ExecutionRole,
    RoutingPolicyRecord,
    Task,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from tests.fixtures import FakeClock, contract_document
from tests.unit.test_class_routing import NOW, _model, _routing

POOL = "shared"


def _routing_version(
    version: int, cap: int | None, *, review_harness: str = "claude_code"
) -> RoutingPolicyV1:
    routing = _routing(
        [
            _model("impl-model", harness="codex", pool=POOL),
            _model("review-model", harness=review_harness, pool=POOL),
        ]
    )
    routing.version = version
    routing.pools[POOL].max_concurrency = cap
    return routing


def _record(routing: RoutingPolicyV1, *, retired: bool = False) -> RoutingPolicyRecord:
    return RoutingPolicyRecord(
        name=routing.name,
        version=routing.version,
        document=routing.model_dump(mode="json"),
        created_at=NOW,
        retired_at=NOW if retired else None,
    )


class _World:
    """The rows one supervisor tick reads, behind the MagicMock unit of work the
    supervisor tests use."""

    def __init__(
        self,
        routings: list[RoutingPolicyRecord],
        snapshot_version: int = 1,
        *,
        pinned: bool = False,
    ) -> None:
        self.routings = routings
        self.policy: dict[str, Any] = {
            "routing": {
                "policy": {"name": "test-routing", "version": snapshot_version, "pinned": pinned}
            },
            "concurrency": {"per_harness": {"codex": 5, "agy": 5, "claude_code": 5}},
        }
        self.tasks: dict[str, Task] = {}
        self.executions: dict[str, Execution] = {}
        self.attempts: dict[str, Attempt] = {}
        self.uow: Any = MagicMock()
        uow = self.uow
        uow.tasks.get.side_effect = lambda task_id, **_: self.tasks.get(task_id)
        uow.executions.get.side_effect = lambda execution_id, **_: self.executions.get(execution_id)
        uow.attempts.get.side_effect = lambda attempt_id, **_: self.attempts.get(attempt_id)
        uow.attempts.add.side_effect = lambda attempt: self.attempts.setdefault(attempt.id, attempt)
        uow.attempts.list_in_states.side_effect = lambda states, **_: [
            a for a in self.attempts.values() if a.state in states
        ]
        uow.attempt_metrics.list_since.return_value = []
        uow.attempt_metrics.recent_for_project.return_value = []
        uow.pool_exhaustions.get.return_value = None
        uow.harness_images.get.return_value = None
        uow.provider_settings.get.return_value = None
        uow.events.latest_for_task_kind.return_value = None
        uow.routing_policies.get.side_effect = lambda name, version: next(
            (r for r in self.routings if (r.name, r.version) == (name, version)), None
        )
        uow.routing_policies.list_versions.side_effect = lambda name: sorted(
            (r for r in self.routings if r.name == name), key=lambda r: r.version
        )
        # Every version in these tests is published: some policy references it.
        uow.routing_policies.is_referenced.return_value = True
        uow.contracts.get.return_value = SimpleNamespace(document=contract_document())

    def add(
        self,
        name: str,
        *,
        role: ExecutionRole = ExecutionRole.IMPLEMENT,
        state: AttemptState = AttemptState.PENDING,
        harness: str = "",
        model: str = "",
        selected_pool: str | None = None,
    ) -> _Pending:
        review = role is ExecutionRole.REVIEW
        task = Task(
            f"task-{name}",
            f"FDY-{name}",
            "foundry",
            "p",
            name,
            TaskState.AWAITING_INTERNAL_REVIEW if review else TaskState.SCHEDULED,
            1,
            "policy",
            1,
            "repo",
            NOW,
            NOW,
        )
        execution = Execution(
            f"execution-{name}",
            task.id,
            role,
            1,
            harness,
            model,
            None,
            "fake",
            "crucible-worker:fake-succeed" if review else "",
            self.policy,
            ExecutionState.CREATED,
            1,
            [],
            60,
            NOW,
        )
        attempt = Attempt(
            f"attempt-{name}",
            execution.id,
            task.id,
            1,
            state,
            NOW + timedelta(seconds=len(self.attempts)),
            selected_pool=selected_pool,
        )
        self.tasks[task.id] = task
        self.executions[execution.id] = execution
        self.attempts[attempt.id] = attempt
        contract = contract_document(external_id=task.external_id)
        contract["execution_request"]["image"] = None
        return _Pending(task=task, execution=execution, attempt=attempt, contract=contract)

    def running_implement(self, name: str) -> None:
        self.add(
            name,
            state=AttemptState.RUNNING,
            harness="codex",
            model="impl-model",
            selected_pool=POOL,
        )

    def events(self, kind: EventKind) -> list[Any]:
        return [
            event
            for event in (call.args[0] for call in self.uow.events.append.call_args_list)
            if event.kind == kind.value
        ]


def _supervisor(monkeypatch: pytest.MonkeyPatch, world: _World) -> Supervisor:
    supervisor = Supervisor(
        MagicMock(),
        {"fake": FakeProvider()},
        FakeClock(NOW),
        holder="test",
        artifact_store=MagicMock(),
    )
    monkeypatch.setattr(supervisor, "_fenced", lambda: nullcontext(world.uow))
    monkeypatch.setattr(supervisor, "_uow_factory", lambda: nullcontext(world.uow))
    monkeypatch.setattr(supervisor, "_eligible_harnesses", lambda **_: None)
    monkeypatch.setattr(supervisor, "_logins_in_progress", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(supervisor, "_harness_gate", lambda _: None)
    monkeypatch.setattr(supervisor, "_take_checkout_lease", MagicMock(return_value=True))
    monkeypatch.setattr(supervisor, "_release_attempt_checkout", MagicMock())
    return supervisor


async def test_a_review_attempt_holds_a_pool_slot_and_a_third_attempt_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World([_record(_routing_version(1, cap=2))])
    supervisor = _supervisor(monkeypatch, world)
    world.running_implement("first")
    review = world.add(
        "review", role=ExecutionRole.REVIEW, harness="claude_code", model="review-model"
    )
    third = world.add("third")

    launched = await supervisor._begin_launch(review)

    assert launched is not None
    attempt = world.attempts[review.attempt.id]
    assert attempt.state is AttemptState.PREPARING
    assert launched[0].attempt.selected_pool == POOL
    assert attempt.selected_pool == POOL
    assert attempt.selected_harness == "claude_code"
    assert attempt.selected_model == "review-model"

    assert await supervisor._begin_launch(third) is None
    assert world.attempts[third.attempt.id].state is AttemptState.PENDING
    deferred = world.events(EventKind.HARNESS_LAUNCH_DEFERRED)
    assert [event.payload["attempt_id"] for event in deferred] == [third.attempt.id]
    assert f"2 of 2 {POOL} pool worker(s) already running" in deferred[0].payload["detail"]


async def test_a_review_waits_for_a_full_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World([_record(_routing_version(1, cap=1))])
    supervisor = _supervisor(monkeypatch, world)
    world.running_implement("first")
    review = world.add(
        "review", role=ExecutionRole.REVIEW, harness="claude_code", model="review-model"
    )

    assert await supervisor._begin_launch(review) is None

    attempt = world.attempts[review.attempt.id]
    assert attempt.state is AttemptState.PENDING
    assert attempt.selected_pool is None
    deferred = world.events(EventKind.HARNESS_LAUNCH_DEFERRED)
    assert deferred[0].payload["detail"] == f"1 of 1 {POOL} pool worker(s) already running"


async def test_the_per_harness_cap_still_holds_a_review(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World([_record(_routing_version(1, cap=None))])
    world.policy["concurrency"]["per_harness"]["claude_code"] = 1
    supervisor = _supervisor(monkeypatch, world)
    world.add(
        "first",
        role=ExecutionRole.REVIEW,
        state=AttemptState.RUNNING,
        harness="claude_code",
        model="review-model",
    )
    review = world.add(
        "review", role=ExecutionRole.REVIEW, harness="claude_code", model="review-model"
    )

    assert await supervisor._begin_launch(review) is None

    deferred = world.events(EventKind.HARNESS_LAUNCH_DEFERRED)
    assert deferred[0].payload["detail"] == "1 of 1 claude_code worker(s) already running"


async def test_attempts_record_the_routing_version_they_launched_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World([_record(_routing_version(1, cap=None)), _record(_routing_version(2, cap=None))])
    supervisor = _supervisor(monkeypatch, world)
    implement = world.add("implement")
    review = world.add(
        "review", role=ExecutionRole.REVIEW, harness="claude_code", model="review-model"
    )

    assert await supervisor._begin_launch(implement) is not None
    assert await supervisor._begin_launch(review) is not None

    # The snapshot references version 1 unpinned, so both launch under version 2,
    # the version in force (hades #254), and say so.
    for pending in (implement, review):
        attempt = world.attempts[pending.attempt.id]
        assert attempt.state is AttemptState.PREPARING
        assert attempt.routing_version == 2
    assert world.attempts[implement.attempt.id].selected_pool == POOL
    assert world.attempts[review.attempt.id].selected_pool == POOL


async def test_a_pinned_review_records_its_pinned_routing_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(
        [_record(_routing_version(1, cap=None)), _record(_routing_version(2, cap=None))],
        pinned=True,
    )
    supervisor = _supervisor(monkeypatch, world)
    review = world.add(
        "review", role=ExecutionRole.REVIEW, harness="claude_code", model="review-model"
    )

    assert await supervisor._begin_launch(review) is not None

    assert world.attempts[review.attempt.id].routing_version == 1


async def test_a_review_whose_harness_its_routing_entry_does_not_pair_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routing entry pairs the review model with agy; a review that asks for
    claude_code with it is refused, holds no pool slot and leaves the pool to others."""
    world = _World([_record(_routing_version(1, cap=2, review_harness="agy"))])
    supervisor = _supervisor(monkeypatch, world)
    world.running_implement("first")
    review = world.add(
        "review", role=ExecutionRole.REVIEW, harness="claude_code", model="review-model"
    )
    second = world.add("second")

    assert await supervisor._begin_launch(review) is None

    attempt = world.attempts[review.attempt.id]
    assert attempt.state is AttemptState.FAILED
    assert attempt.selected_pool is None
    refused = world.events(EventKind.HARNESS_REFUSED)
    assert [event.attempt_id for event in refused] == [attempt.id]
    assert refused[0].payload["stage"] == "routing"
    assert refused[0].payload["detail"] == (
        "model review-model is not paired with harness claude_code in routing policy test-routing/1"
    )
    # The refused review took no slot: the second implement attempt launches.
    assert await supervisor._begin_launch(second) is not None
    assert world.attempts[second.attempt.id].state is AttemptState.PREPARING


@pytest.mark.parametrize(
    ("selected_harness", "selected_pool", "marked"),
    [("codex", POOL, True), ("claude_code", POOL, False), ("codex", "other", False)],
)
def test_only_the_verified_route_is_marked_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    selected_harness: str,
    selected_pool: str,
    marked: bool,
) -> None:
    world = _World([_record(_routing_version(1, cap=None))])
    supervisor = _supervisor(monkeypatch, world)
    pending = world.add(
        "quota",
        state=AttemptState.RUNNING,
        harness=selected_harness,
        model="impl-model",
        selected_pool=selected_pool,
    )
    attempt = world.attempts[pending.attempt.id]
    attempt.selected_model = "impl-model"
    attempt.selected_harness = selected_harness
    attempt.routing_version = 1

    supervisor._mark_pool_exhausted(world.uow, attempt, pending.execution, None)

    assert world.uow.pool_exhaustions.put.called is marked
    if marked:
        assert world.uow.pool_exhaustions.put.call_args.args[0].pool == POOL


@pytest.mark.parametrize(
    ("snapshot_cap", "newest_cap", "expected"),
    [(3, 1, 1), (1, 3, 1), (None, 1, 1), (1, None, 1)],
)
async def test_the_strictest_pool_cap_across_routing_versions_wins(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_cap: int | None,
    newest_cap: int | None,
    expected: int,
) -> None:
    """A task pinned to version 1 still routes with it (hades #254); the cap that binds
    is the smaller of version 1's and the newest active version's."""
    world = _World(
        [
            _record(_routing_version(1, cap=snapshot_cap)),
            _record(_routing_version(2, cap=newest_cap)),
        ],
        pinned=True,
    )
    supervisor = _supervisor(monkeypatch, world)
    world.running_implement("first")
    waiting = world.add("waiting")

    assert await supervisor._begin_launch(waiting) is None

    assert world.attempts[waiting.attempt.id].state is AttemptState.PENDING
    deferred = world.events(EventKind.HARNESS_LAUNCH_DEFERRED)
    assert f"1 of {expected} {POOL} pool worker(s) already running" in deferred[0].payload["detail"]


async def test_a_retired_routing_version_does_not_lower_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(
        [
            _record(_routing_version(1, cap=3)),
            _record(_routing_version(2, cap=3)),
            _record(_routing_version(3, cap=1), retired=True),
        ]
    )
    supervisor = _supervisor(monkeypatch, world)
    world.running_implement("first")
    launching = world.add("launching")

    assert await supervisor._begin_launch(launching) is not None

    assert world.attempts[launching.attempt.id].state is AttemptState.PREPARING
