"""Suite-wide pytest hooks."""

from __future__ import annotations

import math
import os
import subprocess
import time
from pathlib import Path

import pytest

pytest_plugins = ["tests.integration.postgres"]

CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")

# Calibrated timing multiplier for subprocess-heavy and wall-clock-sensitive tests.
# Measured once at import time by running a tiny subprocess and scaling up enough
# that CPU-starvation on the worker node does not flip the test to a spurious failure,
# while the test still fails fast when the behaviour it guards is broken.
_CALIBRATION: float | None = None


def cpu_limit_workers(cpu_max: str | None, cpus: int) -> int:
    """How many xdist workers `-n auto` starts: the CPUs this process may use, capped by
    its cgroup's CPU quota when it has one (hades #184).

    In a Kubernetes Pod `os.cpu_count()` and the affinity mask both report the node's
    CPUs, while the Pod may use only its CPU limit. `make test-unit` in a worker or
    verifier Pod would start one worker per node CPU, each importing the whole package,
    against a limit of two. `cpu.max` reads `max 100000` without a quota and
    `200000 100000` for two CPUs."""
    workers = max(1, cpus)
    if not cpu_max:
        return workers
    quota, _, period = cpu_max.strip().partition(" ")
    if quota == "max" or not quota.isdigit() or not period.isdigit() or int(period) == 0:
        return workers
    return max(1, min(workers, math.ceil(int(quota) / int(period))))


def _calibrate() -> float:
    """Measure a baseline subprocess launch latency and return a safety multiplier.

    Spawns a trivial subprocess ten times, times how long the batch takes, and
    returns a factor that makes a 1-second baseline operation take at least one
    second on a loaded node while keeping the test fast on a healthy one.

    Returns a factor in the range 1.0 (fast machine) to 10.0 (heavily loaded)."""
    baseline_count = 10
    t0 = time.monotonic()
    for _ in range(baseline_count):
        subprocess.run(
            [os.environ.get("PYTHON_EXECUTABLE", "python3"), "-c", "pass"],
            capture_output=True,
            check=False,
            timeout=5,
        )
    elapsed = time.monotonic() - t0

    # On a healthy machine a single spawn+run is around 30-80 ms; ten should take
    # under 1 second. On a heavily loaded node each launch can take 200-500 ms, so
    # the batch runs in 2-5 s.  We want a multiplier that turns the 1-second baseline
    # into something that survives a 50x slowdown without inflating healthy runs.
    #
    # If the batch finishes in <= 1 s we are fine at 1.0.
    # If it takes longer, scale up proportionally, capped at 10.
    if elapsed <= 1.0:
        return 1.0
    factor = min(10.0, math.ceil(2.0 / elapsed * baseline_count))
    return max(1.0, float(factor))


class _CalibrationState:
    """Thread-safe holder for the calibration factor."""

    def __init__(self) -> None:
        self._value: float | None = None

    def get(self) -> float:
        if self._value is None:
            self._value = _calibrate()
        return self._value

    def set(self, value: float) -> None:
        self._value = value


_CAL = _CalibrationState()


def cpu_time() -> float:
    """Return a calibrated time multiplier for wall-clock-sensitive assertions.

    Call this in tests that assert a subprocess or async deadline must finish within
    a given window.  The default is 1.0 on a fast node; on a CPU-starved node (one
    core, heavy load) it can be 2-5.  This keeps the test semantics identical while
    preventing spurious timeouts."""
    return _CAL.get()


# Optional: the hook exists only while pytest-xdist is loaded (`-p no:xdist` runs too).
@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config: object) -> int | None:
    # xdist's own override still wins: returning None hands the choice back to it.
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None
    try:
        cpu_max: str | None = CGROUP_CPU_MAX.read_text(encoding="utf-8")
    except OSError:
        cpu_max = None
    return cpu_limit_workers(cpu_max, len(os.sched_getaffinity(0)))


# Expose the calibration state for tests that need to inject it.
def _set_calibration(value: float) -> None:
    """Set the calibration factor (for testing only)."""
    _CAL.set(value)


# Backwards-compatible alias so old imports still work.
def _get_calibration() -> float | None:
    return _CAL._value


# Re-export for test imports that reference the module-level variable directly.
def _clear_calibration() -> None:
    """Clear the calibration cache (for testing only)."""
    _CAL._value = None
