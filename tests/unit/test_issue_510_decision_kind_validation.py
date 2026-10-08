"""Acceptance tests for Issue #510 (FDY-0509): decision kind validation.

- POST /tasks/{id}/decisions refuses an unknown decision kind with 422,
  listing every accepted kind.
- An escalation_answer with too-short verbatim or resolves is refused with 422.
- `crucible schema` lists the same kinds the validator uses.
- Every known kind is accepted, and existing decisions with other kinds stay readable.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from pydantic import ValidationError

from crucible.application.decisions import record_decision
from crucible.client.schema import document
from crucible.domain.decisions import ACCEPTED_DECISION_KINDS, PUBLIC_DECISION_KINDS

# ---------------------------------------------------------------------------
# AC1: unknown kind → 422, escalation stays open
# ---------------------------------------------------------------------------


class TestUnknownKind:
    """An unknown kind is refused before anything is written."""

    def test_probe_kind_is_refused(self) -> None:
        """AC1: kind '__probe__' is not accepted and is listed in the error."""
        with pytest.raises(ValidationError) as exc:
            from crucible.contracts.api import DecisionRequest  # noqa: PLC0415

            DecisionRequest(
                kind="__probe__",
                verbatim="test probe reason",
                resolves="test resolve",
            )
        errors = exc.value.errors()
        # model_validator errors have loc=() but msg contains the kind.
        assert any(e.get("msg") and "__probe__" in e["msg"] for e in errors)
        # Every public accepted kind must appear in the error body.
        # Internal closure kinds (task_cancelled, task_closed) are excluded
        # from the public list, so they should NOT be in the error (Finding 02).
        for kind in PUBLIC_DECISION_KINDS:
            assert any(kind in (e.get("msg") or "") for e in errors), (
                f"public kind {kind!r} missing from error detail"
            )
        # Internal kinds should NOT appear in the public error listing.
        for kind in ("task_cancelled", "task_closed"):
            assert not any(kind in (e.get("msg") or "") for e in errors), (
                f"internal kind {kind!r} must not appear in public error"
            )

    def test_random_kind_is_refused(self) -> None:
        """A completely arbitrary string kind is rejected."""
        with pytest.raises(ValidationError) as exc:
            from crucible.contracts.api import DecisionRequest  # noqa: PLC0415

            DecisionRequest(
                kind="completely_random_kind_xyz",
                verbatim="test probe reason",
                resolves="test resolve",
            )
        assert any(
            e.get("msg") and "completely_random_kind_xyz" in (e.get("msg") or "")
            for e in exc.value.errors()
        )


# ---------------------------------------------------------------------------
# AC2: too-short verbatim/resolves → 422, escalation stays open
# ---------------------------------------------------------------------------


class TestMinLengthVerbatimResolves:
    """One-character (or zero) verbatim or resolves is refused."""

    def test_empty_verbatim_is_refused(self) -> None:
        """AC2: verbatim='' is rejected."""
        with pytest.raises(ValidationError):
            from crucible.contracts.api import DecisionRequest  # noqa: PLC0415

            DecisionRequest(
                kind="escalation_answer",
                verbatim="",
                resolves="a valid resolve",
            )

    def test_empty_resolves_is_refused(self) -> None:
        """AC2: resolves='' is rejected."""
        with pytest.raises(ValidationError):
            from crucible.contracts.api import DecisionRequest  # noqa: PLC0415

            DecisionRequest(
                kind="escalation_answer",
                verbatim="a valid verbatim",
                resolves="",
            )

    def test_one_char_verbatim_is_refused(self) -> None:
        """AC2: verbatim='x' is rejected (minimum 2)."""
        with pytest.raises(ValidationError):
            from crucible.contracts.api import DecisionRequest  # noqa: PLC0415

            DecisionRequest(
                kind="escalation_answer",
                verbatim="x",
                resolves="a valid resolve",
            )

    def test_one_char_resolves_is_refused(self) -> None:
        """AC2: resolves='x' is rejected (minimum 2)."""
        with pytest.raises(ValidationError):
            from crucible.contracts.api import DecisionRequest  # noqa: PLC0415

            DecisionRequest(
                kind="escalation_answer",
                verbatim="a valid verbatim",
                resolves="x",
            )

    def test_two_char_verbatim_and_resolves_are_accepted(self) -> None:
        """A well-formed escalation_answer with minimum-length fields passes."""
        from crucible.contracts.api import DecisionRequest  # noqa: PLC0415

        req = DecisionRequest(
            kind="escalation_answer",
            verbatim="ab",
            resolves="cd",
        )
        assert req.kind == "escalation_answer"
        assert req.verbatim == "ab"
        assert req.resolves == "cd"


# ---------------------------------------------------------------------------
# AC3: schema lists kinds from the same constant
# ---------------------------------------------------------------------------


class TestSchemaDrift:
    """crucible schema's decision_kinds must match ACCEPTED_DECISION_KINDS."""

    def test_schema_decision_kinds_match_constant(self) -> None:
        """AC3: the schema's decision_kinds list is the same sorted set."""
        doc = document()
        schema_kinds = set(doc["decision_kinds"])
        assert schema_kinds == ACCEPTED_DECISION_KINDS
        # The list must be sorted (discoverable).
        assert doc["decision_kinds"] == sorted(ACCEPTED_DECISION_KINDS)

    def test_schema_includes_decision_kinds_field(self) -> None:
        """The schema has a 'decision_kinds' top-level key."""
        doc = document()
        assert "decision_kinds" in doc
        assert isinstance(doc["decision_kinds"], list)


# ---------------------------------------------------------------------------
# AC4: every known kind accepted, old kinds still readable
# ---------------------------------------------------------------------------


class TestKnownKindsAndBackwardsCompatibility:
    """Every spec-named kind is accepted; existing decisions with other kinds stay readable."""

    @pytest.mark.parametrize(
        "kind",
        [
            "accept",
            "accept_no_ci",
            "escalation_answer",
            "recollect",
            "release_authorization",
            "waive_external_review",
        ],
    )
    def test_every_known_kind_is_accepted(self, kind: str) -> None:
        """AC4: each kind the spec names is accepted by the model."""
        from crucible.contracts.api import DecisionRequest  # noqa: PLC0415

        req = DecisionRequest(
            kind=kind,
            verbatim="accepting because it is right",
            resolves="task is ready",
        )
        assert req.kind == kind

    def test_existing_probe_decision_still_readable(self) -> None:
        """Existing decisions with unknown kinds are readable.

        AC4: the validator only applies to *new* requests. A decision with
        kind '__probe__' in the database is still returned by the API,
        because we only validate the request, not the stored data.
        """
        # The model validator only fires on new DecisionRequest instances.
        # This proves that the validation is at the boundary (the API layer)
        # and not on the domain model itself.
        from datetime import datetime  # noqa: PLC0415

        # We can't create one with a probe kind, but the Decision domain
        # entity is not validated — only the request model is.
        from crucible.domain.entities import Decision  # noqa: PLC0415

        old_decision = Decision(
            id="01HB9WZQJQJQJQJQJQJQJQJQJQ",
            task_id="01HB9WZQJQJQJQJQJQJQJQJQJQ",
            escalation_id=None,
            principal_id="01HB9WZQJQJQJQJQJQJQJQJQJQ",
            kind="__probe__",
            verbatim="old probe reason",
            resolves="old probe resolve",
            created_at=datetime.now(UTC),
        )
        # The domain entity can hold any kind; the list is enforced only at
        # the API boundary (DecisionRequest model), so existing decisions
        # with other kinds remain readable.
        assert old_decision.kind == "__probe__"


# ---------------------------------------------------------------------------
# Integration-like: record_decision with a valid request
# ---------------------------------------------------------------------------


class TestRecordDecisionWithValidRequest:
    """record_decision accepts a valid request and records the decision."""

    def test_record_decision_passes_validation(self) -> None:
        """A valid request goes through record_decision without raising."""
        from unittest.mock import MagicMock, patch  # noqa: PLC0415

        from crucible.contracts.api import DecisionRequest  # noqa: PLC0415
        from crucible.domain.entities import Principal, Role  # noqa: PLC0415
        from crucible.domain.lifecycle import TaskState  # noqa: PLC0415
        from tests.fixtures import FakeClock  # noqa: PLC0415

        clock = FakeClock()
        principal = Principal(
            id="principal-1",
            name="operator",
            role=Role.OPERATOR,
            created_at=clock.now(),
        )

        # Build a minimal mock task that record_decision expects.
        task = MagicMock()
        task.id = "task-1"
        task.state = TaskState.BLOCKED
        task.principal_id = "principal-1"

        uow = MagicMock()
        uow.tasks.get.return_value = task
        uow.decisions.add = MagicMock()
        uow.events.append = MagicMock()

        # A valid request with a non-operator kind by an operator should succeed.
        request = DecisionRequest(
            kind="escalation_answer",
            verbatim="operator approved",
            resolves="block resolved",
            reschedule=False,
        )

        with patch(
            "crucible.application.decisions.require_contract",
            return_value=MagicMock(),
        ):
            result = record_decision(
                uow, clock, principal=principal, task_id="task-1", request=request
            )

        assert result is not None
        uow.decisions.add.assert_called_once()
        added_decision = uow.decisions.add.call_args.args[0]
        assert added_decision.kind == "escalation_answer"
        assert added_decision.verbatim == "operator approved"
        assert added_decision.resolves == "block resolved"
