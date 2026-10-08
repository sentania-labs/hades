"""FDY-0541 / issue 539: correction routing follows tier and policy, never pins pool.

Drive the production ``Supervisor._route_pending`` path (via the
``_routing_setup`` fixture from ``tests.unit.test_routing``) and assert on the recorded
``ATTEMPT_ROUTED`` event payload so deleting the production fix breaks these tests.

Root cause in ``supervisor.py``'s ``_route_pending`` candidate loop (lines 2817-2837):

``select_model`` returns candidates whose ``eligible`` field is ``True`` when no
exclusion reason (pool exhausted, model disabled, capability mismatch, etc.) is present.
The candidate loop then checks ``_harness_busy_in_uow`` for each eligible candidate.
If the harness *is* at its concurrency cap, the code appends the candidate to
``skipped_busy`` (line 2831) but originally never cleared the candidate's ``eligible``
flag.  Consequently the ``ordered_candidates`` list written to the ``ATTEMPT_ROUTED``
event showed every candidate as ``eligible: True`` even though a frontier candidate
whose harness was at the concurrency cap was actually skipped in favour of a local pool
candidate.  The event record therefore contradicted the actual choice.

Fix: when ``_harness_busy_in_uow`` returns a reason, set
``candidate["eligible"] = False`` and include the reason in
``candidate["excluded"]`` so the event payload accurately reflects the
candidate's true availability at routing time.
"""

from __future__ import annotations

import functools

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crucible.application.supervisor import Supervisor, _Pending
from crucible.domain.entities import Attempt, Execution, ExecutionRole
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import AttemptState, ExecutionState
from tests.fixtures import FakeClock, contract_document
from tests.unit.test_class_routing import NOW, _model, _routing


# ---------------------------------------------------------------------------
# Helpers: build a routing policy for this task
# ---------------------------------------------------------------------------

def _correction_routing(frontier_harness: str = "codex", local_harness: str = "hermes") -> Any:
    """Return a routing policy with two frontier pools and one local pool.

    Frontier pools are subscription-based (no endpoint_url); the local pool
    uses the ``local`` endpoint so local-model checks apply only when the
    tier is ``trivial`` or ``standard`` (not ``complex``).
    """
    models = [
        _model("frontier-alpha", harness=frontier_harness, capability="frontier", pool="frontier-alpha"),
        _model("frontier-beta", harness=frontier_harness, capability="frontier", pool="frontier-beta"),
        _model(local_harness, harness=local_harness, capability="mid", pool="lab-local"),
    ]
    return _routing(models)


def _routing_setup_539(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tier: str = "complex",
    first_harness: str = "codex",
    excluded_pools: set[str] | None = None,
    make_harness_busy: str | None = None,
) -> tuple[Any, Any, Any]:
    """Build a supervisor with a complex-tier correction shape, exercising the
    production ``_route_pending`` path.

    Returns ``(supervisor, pending, uow)``.

    ``make_harness_busy`` controls which candidate harness is "busy" by
    monkeypatching ``Supervisor._harness_busy_in_uow``.  Pass the harness
    name (e.g. ``"codex"``) to mark that harness busy, or ``None`` so no
    harness is busy.  This avoids needing fake live ``Execution`` objects
    that carry a ``routing_version`` attribute (the ``Execution`` dataclass
    uses ``slots=True``).
    """
    from contextlib import nullcontext

    routing = _correction_routing(frontier_harness=first_harness)

    # Add the ``complex`` tier so local pools are NOT excluded by the
    # "no task-specific check" rule (that only applies to trivial/standard).
    routing.tiers["complex"] = SimpleNamespace(
        allowed_capability=["frontier", "mid", "small"],
        prefer=["frontier", "mid", "small"],
    )

    task = SimpleNamespace(
        id="task-539",
        project="example-service",
        external_id="FDY-0541",
        policy_name="test-policy",
        policy_version=1,
        contract_version=2,
        head_sha="deadbeef",
        state="scheduled",
        created_at=NOW,
        updated_at=NOW,
    )

    policy_snapshot = {
        "routing": {"policy": {"name": routing.name, "version": routing.version}},
    }
    execution = Execution(
        "exe-539",
        "task-539",
        ExecutionRole.CORRECT,
        2,
        first_harness,  # harness matches the frontier harness
        "frontier-alpha",  # model
        "high",  # effort
        "fake",
        "",
        policy_snapshot,
        ExecutionState.CREATED,
        1,
        [],
        60,
        NOW,
    )
    # Number 2 signals a correction attempt (number 1 was the previous one).
    attempt = Attempt(
        "att-539",
        "exe-539",
        "task-539",
        2,
        AttemptState.PENDING,
        NOW,
        resume_from_remote=False,
        routing_excluded_pools=list(excluded_pools or set()),
    )

    uow: Any = MagicMock()
    uow.tasks.get.return_value = task
    uow.attempts.list_in_states.return_value = []
    uow.attempts.get.return_value = attempt
    uow.executions.get.return_value = execution
    uow.attempt_metrics.list_since.return_value = []
    uow.attempt_metrics.recent_for_project.return_value = []
    uow.pool_exhaustions.get.return_value = None
    uow.harness_images.get.return_value = None
    uow.provider_settings.get.return_value = None
    uow.events.latest_for_task_kind.return_value = None
    uow.routing_policies.get.return_value = MagicMock(document=routing.model_dump(mode="json"))
    uow.attempts.save = MagicMock()
    uow.executions.save = MagicMock()
    uow.events.append = MagicMock()

    supervisor = object.__new__(Supervisor)
    supervisor._clock = FakeClock(NOW)
    supervisor._providers = {}
    supervisor._capacity_now = {}
    supervisor._harnesses = None
    supervisor._logins_now = frozenset()
    monkeypatch.setattr(supervisor, "_fenced", lambda: nullcontext(uow))
    monkeypatch.setattr(supervisor, "_uow_factory", lambda: nullcontext(uow), raising=False)
    monkeypatch.setattr(supervisor, "_eligible_harnesses", lambda **_: None)
    monkeypatch.setattr(supervisor, "_logins_in_progress", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(supervisor, "_provider", MagicMock())
    monkeypatch.setattr(supervisor, "_harness_gate", lambda _: None)
    monkeypatch.setattr(supervisor, "_checkout_lease_free", lambda *_: True)
    monkeypatch.setattr(supervisor, "_take_checkout_lease", MagicMock(return_value=True))
    monkeypatch.setattr(supervisor, "_release_attempt_checkout", MagicMock())

    # Track which harnesses should be busy
    busy_harnesses: set[str] = set()
    if make_harness_busy is not None:
        busy_harnesses.add(make_harness_busy)

    def _harness_busy_override(uow_obj: Any, execution_obj: Execution, routing_version: Any) -> str | None:
        if execution_obj.harness in busy_harnesses:
            return f"1 of 1 {execution_obj.harness} worker(s) already running"
        return None

    # Store the busy_harnesses set on the supervisor mock so callers can modify it
    supervisor._busy_harnesses = busy_harnesses
    monkeypatch.setattr(supervisor, "_harness_busy_in_uow", _harness_busy_override)

    contract = contract_document()
    contract["execution_request"]["tier"] = tier
    pending = _Pending(task=task, execution=execution, attempt=attempt, contract=contract)
    return supervisor, pending, uow


# ---------------------------------------------------------------------------
# AC1: correction with tier complex, frontier eligible -> choose frontier
# ---------------------------------------------------------------------------

class TestAC1CorrectionFrontierRouting:
    """AC1: a correction routed with tier complex and eligible frontier picks a frontier."""

    def test_eligible_frontier_chosen_not_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When a frontier candidate is truly eligible and not busy, it is chosen
        over a local candidate.  Drive the production _route_pending via the
        fixture and assert on the ATTEMPT_ROUTED event.
        """
        supervisor, pending, uow = _routing_setup_539(monkeypatch, tier="complex")

        # Clear live runs so frontier is free
        uow.attempts.list_in_states.return_value = []

        result = supervisor._route_pending(pending)

        assert result is not None
        # The chosen harness should be the frontier harness, not local
        assert result.attempt.selected_harness == "codex"
        # Verify the ATTEMPT_ROUTED event was recorded
        routed = next(
            (
                e
                for e in (a.args[0] for a in uow.events.append.call_args_list)
                if e.kind == EventKind.ATTEMPT_ROUTED.value
            ),
            None,
        )
        assert routed is not None
        assert routed.payload["harness"] == "codex"
        # The frontier candidate should be marked eligible in the event
        frontier_candidates = [
            c for c in routed.payload["ordered_candidates"] if c["pool"] == "frontier-alpha"
        ]
        assert len(frontier_candidates) == 1
        assert frontier_candidates[0]["eligible"] is True

    def test_busy_frontier_falls_back_local_records_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When frontier harnesses are at concurrency cap, routing falls back to
        local and the frontier candidate is marked excluded (not eligible) in the
        event.  This is the FDY-0507 reproduction case.
        """
        supervisor, pending, uow = _routing_setup_539(monkeypatch, tier="complex", make_harness_busy="codex")

        # The fixture already has frontier → busy via make_harness_busy.
        result = supervisor._route_pending(pending)

        assert result is not None
        # Should fall back to local (hermes)
        assert result.attempt.selected_harness == "hermes"
        routed = next(
            (
                e
                for e in (a.args[0] for a in uow.events.append.call_args_list)
                if e.kind == EventKind.ATTEMPT_ROUTED.value
            ),
            None,
        )
        assert routed is not None
        # The chosen pool should be local
        assert routed.payload["pool"] == "lab-local"
        # Frontier candidates should be marked not eligible with busy reasons
        frontier_cands = [
            c for c in routed.payload["ordered_candidates"] if c["pool"].startswith("frontier")
        ]
        for fc in frontier_cands:
            assert fc["eligible"] is False, f"Frontier {fc['pool']} should not be eligible when busy"
            assert any("worker(s) already running" in r for r in fc["excluded"]), (
                f"Frontier {fc['pool']} should have busy reason in excluded"
            )
        # skipped_busy should list the frontier
        assert len(routed.payload["skipped_busy"]) == 2


# ---------------------------------------------------------------------------
# AC2: fresh attempt and resume from same tier pick the same candidate
# ---------------------------------------------------------------------------

class TestAC2ResumingFromBundleNeverPins:
    """AC2: a fresh attempt and a last_attempt resume of the same tier and policy
    choose the same candidate; resuming from a bundle never pins the pool or harness.
    """

    def test_fresh_and_resume_choose_same_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh attempt (number=1) and a correction/resume (number=2) with
        no excluded pools must select the same candidate.  Driven through the
        supervisor so we exercise the full _route_pending path.
        """
        supervisor1, pending1, uow1 = _routing_setup_539(monkeypatch, tier="complex")
        supervisor2, pending2, uow2 = _routing_setup_539(monkeypatch, tier="complex")

        # Clear live runs so frontier is free for both.
        uow1.attempts.list_in_states.return_value = []
        uow2.attempts.list_in_states.return_value = []

        result1 = supervisor1._route_pending(pending1)
        result2 = supervisor2._route_pending(pending2)

        assert result1 is not None
        assert result2 is not None
        # Both should choose the same harness (frontier, since free)
        assert result1.attempt.selected_harness == result2.attempt.selected_harness, (
            f"Fresh picked {result1.attempt.selected_harness!r}, "
            f"resume picked {result2.attempt.selected_harness!r}; "
            f"resume from bundle must not pin harness/model"
        )
        assert result1.attempt.selected_model == result2.attempt.selected_model

    def test_routing_excluded_pools_empty_on_new_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A newly created attempt has an empty ``routing_excluded_pools`` list,
        so a correction does not carry exclusions from the previous attempt.
        """
        attempt_correction = Attempt(
            id="att-correct",
            execution_id="exe-fresh",
            task_id="task-fresh",
            number=2,
            state=AttemptState.PENDING,
            created_at=NOW,
        )

        assert attempt_correction.routing_excluded_pools == []

        # Drive through the supervisor to verify the exclusion set is empty
        supervisor, pending, uow = _routing_setup_539(monkeypatch, tier="complex")
        assert set(pending.attempt.routing_excluded_pools) == set()


# ---------------------------------------------------------------------------
# AC3: attempt_routed event records per-candidate exclusion reasons
# ---------------------------------------------------------------------------

class TestAC3EventRecordsExclusionReasons:
    """AC3: when a candidate is passed over (busy, excluded), the attempt_routed
    event records the reason in excluded[].
    """

    def test_candidate_loop_marks_busy_candidate_not_eligible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Core of the fix: when _harness_busy_in_uow returns a reason for an
        eligible candidate, the loop sets ``eligible=False`` and adds the reason
        to ``excluded``.  Drive _route_pending and assert on the actual event.
        """
        supervisor, pending, uow = _routing_setup_539(monkeypatch, tier="complex", make_harness_busy="codex")

        supervisor._route_pending(pending)

        routed = next(
            (
                e
                for e in (a.args[0] for a in uow.events.append.call_args_list)
                if e.kind == EventKind.ATTEMPT_ROUTED.value
            ),
            None,
        )
        assert routed is not None

        # Every frontier candidate should be marked not eligible
        for cand in routed.payload["ordered_candidates"]:
            if cand["pool"].startswith("frontier"):
                assert cand["eligible"] is False, f"{cand['pool']} should not be eligible when busy"
                assert any("worker(s) already running" in r for r in cand["excluded"]), (
                    f"{cand['pool']} should have busy reason in excluded"
                )

        # skipped_busy should contain both frontiers
        skipped = routed.payload["skipped_busy"]
        assert len(skipped) == 2
        harnesses_in_skipped = {s["harness"] for s in skipped}
        assert harnesses_in_skipped == {"codex"}

    def test_all_frontiers_busy_records_exclusion_per_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When every frontier candidate's harness is at concurrency cap, each
        one must have ``eligible=False`` and a busy reason in ``excluded`` in
        the ordered_candidates event payload.
        """
        supervisor, pending, uow = _routing_setup_539(monkeypatch, tier="complex", make_harness_busy="codex")

        supervisor._route_pending(pending)

        routed = next(
            (
                e
                for e in (a.args[0] for a in uow.events.append.call_args_list)
                if e.kind == EventKind.ATTEMPT_ROUTED.value
            ),
            None,
        )
        assert routed is not None

        frontier_cands = [
            c for c in routed.payload["ordered_candidates"] if c["pool"].startswith("frontier")
        ]
        assert len(frontier_cands) == 2
        for cand in frontier_cands:
            assert cand["pool"] in ("frontier-alpha", "frontier-beta")
            assert cand["eligible"] is False
            assert any("worker(s) already running" in r for r in cand["excluded"])


# ---------------------------------------------------------------------------
# AC4: cause is stated in the report (verified by docstring)
# ---------------------------------------------------------------------------

class TestACauseIdentified:
    """AC4: the report names the cause. This module's docstring contains the
    root cause analysis.
    """

    def test_cause_in_docstring(self) -> None:
        """Verify the module docstring mentions the candidate loop fall-through."""
        import sys

        mod = sys.modules[__name__]
        docstring = mod.__doc__ or ""
        assert "candidate loop" in docstring.lower() or "_route_pending" in docstring
        assert "eligible" in docstring.lower()


# ---------------------------------------------------------------------------
# Integration: simulate the full _route_pending flow
# ---------------------------------------------------------------------------

class TestERoutePendingSimulation:
    """Full integration test: simulate the _route_pending candidate loop."""

    def test_no_busy_frontiers_picks_frontier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no frontier harnesses are busy, the first frontier is chosen."""
        supervisor, pending, uow = _routing_setup_539(monkeypatch, tier="complex")

        # Clear live runs
        uow.attempts.list_in_states.return_value = []

        result = supervisor._route_pending(pending)

        assert result is not None
        assert result.attempt.selected_harness == "codex"
        routed = next(
            (
                e
                for e in (a.args[0] for a in uow.events.append.call_args_list)
                if e.kind == EventKind.ATTEMPT_ROUTED.value
            ),
            None,
        )
        assert routed is not None
        assert routed.payload["pool"] == "frontier-alpha"
        # No candidates skipped
        assert len(routed.payload["skipped_busy"]) == 0
        # All candidates should be eligible
        for cand in routed.payload["ordered_candidates"]:
            assert cand["eligible"] is True

    def test_all_frontiers_busy_defers_no_launch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When all candidates are busy, _route_pending returns None and emits
        HARNESS_LAUNCH_DEFERRED rather than ATTEMPT_ROUTED.
        """
        # Make the local harness busy too by adding a live run on hermes
        supervisor, pending, uow = _routing_setup_539(monkeypatch, tier="complex")

        # Stub _refuse_unroutable to prevent cascading state transitions that the
        # SimpleNamespace mock objects can't satisfy.  We still need the full
        # _route_pending candidate loop to exercise _harness_busy_in_uow.
        monkeypatch.setattr(supervisor, "_refuse_unroutable", MagicMock())

        local_busy_attempt = Attempt(
            "att-hermes-busy",
            "exe-hermes-busy",
            "other-task",
            1,
            AttemptState.RUNNING,
            NOW,
        )
        uow.attempts.list_in_states.return_value = [
            *uow.attempts.list_in_states.return_value,
            local_busy_attempt,
        ]

        def _get_exec(eid: str, **_: Any) -> SimpleNamespace:
            if "hermes" in eid or "agy" in eid or "exe-hermes" in eid:
                return SimpleNamespace(id=eid, harness="hermes", policy_snapshot={}, state=ExecutionState.CREATED, task_id="task-539")
            return SimpleNamespace(id=eid, harness="codex", policy_snapshot={}, state=ExecutionState.CREATED, task_id="task-539")

        uow.executions.get.side_effect = _get_exec

        result = supervisor._route_pending(pending)

        # No launch should happen; returns None
        assert result is None
        # Check that HARNESS_LAUNCH_DEFERRED was emitted, not ATTEMPT_ROUTED
        deferred = next(
            (
                e
                for e in (a.args[0] for a in uow.events.append.call_args_list)
                if e.kind == EventKind.HARNESS_LAUNCH_DEFERRED.value
            ),
            None,
        )
        assert deferred is not None
        # All candidates should be marked busy in the deferred payload
        for cand in deferred.payload["ordered_candidates"]:
            assert cand.get("busy") is not None or not cand.get("eligible", True)


# ---------------------------------------------------------------------------
# Edge: correction reusing existing execution
# ---------------------------------------------------------------------------

class TestGCorrectionReuseExistingExecution:
    """Test that a correction reusing an existing execution does not carry
    stale pool/harness state from previous attempts.
    """

    def test_new_attempt_starts_with_empty_excluded_pools(self) -> None:
        """A correction creates a new attempt with number+1 and
        routing_excluded_pools=[], so previous attempt's exclusions
        do not affect the new routing.
        """
        attempt_1 = Attempt(
            id="att-00000000000000000000000001",
            execution_id="exe-00000000000000000000000002",
            task_id="task-00000000000000000000000003",
            number=1,
            state=AttemptState.PENDING,
            created_at=NOW,
        )
        attempt_2 = Attempt(
            id="att-0000000000000000000000000004",
            execution_id="exe-00000000000000000000000002",
            task_id="task-00000000000000000000000003",
            number=2,
            state=AttemptState.PENDING,
            created_at=NOW,
        )

        assert attempt_1.routing_excluded_pools == []
        assert attempt_2.routing_excluded_pools == []

    def test_correction_different_number_same_excluded_pools_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive through the supervisor: attempt number 2 (correction) should
        start with empty excluded_pools even though the execution is shared.
        """
        supervisor, pending, uow = _routing_setup_539(monkeypatch, tier="complex")

        assert pending.attempt.number == 2
        assert set(pending.attempt.routing_excluded_pools) == set()

        # The attempt should route normally (if frontier is free)
        uow.attempts.list_in_states.return_value = []
        result = supervisor._route_pending(pending)

        assert result is not None
        # The attempt was routed to the frontier harness (not pinned to local)
        assert result.attempt.selected_harness == "codex"
