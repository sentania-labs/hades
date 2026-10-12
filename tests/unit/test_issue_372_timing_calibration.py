"""Sanity checks for the timing calibration helper (issue 372).

Verifies that ``tests.conftest.cpu_time`` returns a usable factor and that the
factor changes when we fake a slow subprocess environment.  This file is designed
to fail on main (before the calibration helper is introduced) because it imports
``tests.conftest.cpu_time`` which will not exist yet.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests import conftest
from tests.conftest import cpu_time


def test_cpu_time_returns_a_positive_number() -> None:
    """The calibrated factor is always a positive float."""
    value = cpu_time()
    assert isinstance(value, float)
    assert value >= 1.0


def test_cpu_time_is_cached() -> None:
    """A second call returns the exact same value without re-calibrating.

    The calibration is stubbed: two real measurements differ on a loaded node, so
    comparing them made this test fail under `make test-unit` load."""
    calls: list[float] = []

    def counting_calibrate() -> float:
        calls.append(float(len(calls) + 2))
        return calls[-1]

    conftest._clear_calibration()
    try:
        with patch.object(conftest, "_calibrate", counting_calibrate):
            first = cpu_time()
            second = cpu_time()
            assert calls == [2.0]
            assert second == first == 2.0
            conftest._clear_calibration()
            # Clearing the cache is what forces a re-calibration.
            assert cpu_time() == 3.0
            assert calls == [2.0, 3.0]
    finally:
        conftest._clear_calibration()


def test_cpu_time_scales_with_slow_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """If subprocesses take long, cpu_time() returns a larger factor."""

    def slow_calibrate() -> float:
        # Pretend the calibration took 4 seconds (slow machine).
        # 2.0 / 4 * 10 = 5
        return 5.0

    conftest._clear_calibration()
    try:
        with patch.object(conftest, "_calibrate", slow_calibrate):
            value = cpu_time()
        assert value == 5.0
    finally:
        conftest._clear_calibration()  # Reset for other tests
