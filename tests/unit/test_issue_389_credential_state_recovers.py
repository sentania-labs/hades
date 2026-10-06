"""hades #389: credential_state recovers after a later successful launch.

The bug was that credential_state compared last_auth_failure_at against
last_validated_at or last_launch_at using the ``or`` operator (which returns
the first truthy value).  Since last_launch_at moves on every launch and
last_validated_at never changes, an auth failure could persist as "invalid"
even after a successful launch.

The fix adds last_successful_launch_at to HarnessState, updates it only on
non-failure outcomes, and makes credential_state take the max of
last_validated_at and last_successful_launch_at (without last_launch_at,
which moves on every launch regardless of outcome) instead of the first
truthy value.

Acceptance criteria:
  AC1: validated at T0, auth failure at T1, successful launch at T2
       → validated or configured, NOT invalid.
  AC2: validated at T0, successful launch at T1, auth failure at T2
       → invalid.
  AC3: failed launch after auth failure does not clear invalid.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from crucible.application.harnesses import credential_state
from crucible.domain.entities import HarnessState
from crucible.ports.harness import AuthFile, MountMode

# ---------- minimal fixture helpers ----------------------------------------

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_state(
    *,
    last_validated_at: datetime | None = None,
    last_launch_at: datetime | None = None,
    last_auth_failure_at: datetime | None = None,
    last_successful_launch_at: datetime | None = None,
) -> HarnessState:
    """Build a minimal HarnessState with the given timestamps."""
    return HarnessState(
        name="codex",
        enabled=True,
        reason="",
        session_compatibility="unverified",
        updated_at=T0,
        updated_by="test",
        last_validated_at=last_validated_at,
        last_launch_at=last_launch_at,
        last_auth_failure_at=last_auth_failure_at,
        last_successful_launch_at=last_successful_launch_at,
    )


class _FakeSpec:
    """A minimal fake CredentialSpec for credential_state tests."""

    harness: str = "codex"
    minimum_mode: MountMode = MountMode.RO
    auth_files: tuple[AuthFile, ...] = (
        type(
            "Auth",
            (),
            {
                "name": "key",
                "required": True,
                "json": False,
            },
        )(),
    )

    @staticmethod
    def source_path(root: str, name: str) -> Any:
        return None


def _spec() -> Any:
    """Return a fake spec."""
    return _FakeSpec()


# ---------- tests ----------------------------------------------------------


class TestAC1RecoveryAfterAuthFailure:
    """AC1: validated at T0, auth failure at T1, successful launch at T2
    reports validated or configured, not invalid."""

    def test_auth_failure_cleared_by_successful_launch(self) -> None:
        state = _make_state(
            last_validated_at=T0,
            last_auth_failure_at=T1,
            last_successful_launch_at=T2,
        )
        result = credential_state(
            _spec(),
            None,
            state,
            stored_sizes={"key": 32},
            stored_in="the Secret store",
        )
        # last_validated_at is set, so it returns "validated";
        # with no last_validated_at it would return "configured".
        # Either way, it must NOT be "invalid".
        assert result.state != "invalid"


class TestAC2FailureAfterSuccessfulLaunch:
    """AC2: validated at T0, successful launch at T1, auth failure at T2
    reports invalid."""

    def test_auth_failure_after_success_is_invalid(self) -> None:
        state = _make_state(
            last_validated_at=T0,
            last_launch_at=T1,
            last_auth_failure_at=T2,
            last_successful_launch_at=T1,
        )
        result = credential_state(
            _spec(),
            None,
            state,
            stored_sizes={"key": 32},
            stored_in="the Secret store",
        )
        assert result.state == "invalid"


class TestAC3FailedLaunchAfterAuthFailure:
    """AC3: a failed launch after the auth failure does not clear invalid."""

    def test_failing_launch_after_auth_failure_stays_invalid(self) -> None:
        """T0: validated. T1: auth failure. T2: failed launch (not successful).
        last_successful_launch_at stays at T0, which is before T1."""
        state = _make_state(
            last_validated_at=T0,
            last_auth_failure_at=T1,
            last_launch_at=T2,
            # last_successful_launch_at stays T0 (never updated after auth failure)
        )
        result = credential_state(
            _spec(),
            None,
            state,
            stored_sizes={"key": 32},
            stored_in="the Secret store",
        )
        assert result.state == "invalid"


class TestEdgeCases:
    """Additional boundary cases for credential_state with the fix."""

    def test_no_failure_returns_validated(self) -> None:
        """When there is no auth failure and last_validated_at is set,
        the state is validated (not configured)."""
        state = _make_state(last_validated_at=T0)
        result = credential_state(
            _spec(),
            None,
            state,
            stored_sizes={"key": 32},
            stored_in="the Secret store",
        )
        assert result.state == "validated"

    def test_no_failure_no_validation(self) -> None:
        """No auth failure, no validation: configured."""
        state = _make_state(last_launch_at=T0)
        result = credential_state(
            _spec(),
            None,
            state,
            stored_sizes={"key": 32},
            stored_in="the Secret store",
        )
        assert result.state == "configured"

    def test_only_validation_then_failure(self) -> None:
        """Validated at T0, auth failure at T1, no successful launch."""
        state = _make_state(
            last_validated_at=T0,
            last_auth_failure_at=T1,
        )
        result = credential_state(
            _spec(),
            None,
            state,
            stored_sizes={"key": 32},
            stored_in="the Secret store",
        )
        assert result.state == "invalid"

    def test_successful_launch_then_auth_failure(self) -> None:
        """Successful launch at T0, auth failure at T1 → invalid."""
        state = _make_state(
            last_successful_launch_at=T0,
            last_auth_failure_at=T1,
        )
        result = credential_state(
            _spec(),
            None,
            state,
            stored_sizes={"key": 32},
            stored_in="the Secret store",
        )
        assert result.state == "invalid"

    def test_validated_cleared_by_successful_launch(self) -> None:
        """Validated at T0, auth failure at T1, successful launch at T2.
        The latest of (validated, successful_launch) is T2, after T1."""
        state = _make_state(
            last_validated_at=T0,
            last_auth_failure_at=T1,
            last_successful_launch_at=T2,
        )
        result = credential_state(
            _spec(),
            None,
            state,
            stored_sizes={"key": 32},
            stored_in="the Secret store",
        )
        # last_validated_at is set, so returns "validated"
        assert result.state in ("configured", "validated")

    def test_only_successful_launch_clears_failure(self) -> None:
        """No validation, only a successful launch clears the failure."""
        state = _make_state(
            last_auth_failure_at=T1,
            last_successful_launch_at=T2,
        )
        result = credential_state(
            _spec(),
            None,
            state,
            stored_sizes={"key": 32},
            stored_in="the Secret store",
        )
        # No last_validated_at → "configured", but NOT "invalid"
        assert result.state != "invalid"

    def test_last_launch_at_does_not_clear_failure(self) -> None:
        """A failed launch at T2 after auth failure at T1 does not clear."""
        state = _make_state(
            last_validated_at=T0,
            last_auth_failure_at=T1,
            last_launch_at=T2,
        )
        result = credential_state(
            _spec(),
            None,
            state,
            stored_sizes={"key": 32},
            stored_in="the Secret store",
        )
        # last_successful_launch_at is None, so max(T0) = T0
        # T1 >= T0 → invalid
        assert result.state == "invalid"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
