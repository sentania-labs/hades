"""Regression for the kind quota-reroute failure on main at e75b8b8d."""

from __future__ import annotations

from typing import Any

from tests.e2e.reroute import first_reroute_pair


def test_a_poll_that_observes_a_later_attempt_still_checks_the_first_reroute() -> None:
    """The asynchronous kind path may advance again before the poll reads history."""
    attempts: list[dict[str, Any]] = [
        {"id": "quota", "exit_class": "quota_exhausted"},
        {"id": "successor", "resume_from_remote": False},
        {"id": "later"},
    ]

    first, second = first_reroute_pair(attempts)

    assert first["id"] == "quota"
    assert second["id"] == "successor"
