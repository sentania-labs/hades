"""Issue 133 / FDY-0464: a node-pressure eviction reads as lost, not exit.

Spec 26 says "evicted, or its node is gone" -> `lost`.  A node-pressure eviction
produces a Pod that is Failed/Evicted with a terminated container.  Before the fix
`observe` checked `_terminated_state` first and returned EXITED; the lost-reasons
check was behind it.  This test exercises all three acceptance criteria against the
fake API.

See docs/spec/26-kubernetes-provider.md lines 527-533 (observe table) and issue 133.
"""

from __future__ import annotations

import pytest

from crucible.adapters.execution.k8sspec import CONTAINER_NAME
from crucible.ports.execution import ObservationState
from tests.unit.kubernetes_fixtures import build, spec


@pytest.mark.asyncio
async def test_ac1_evicted_pod_with_terminated_container_observes_as_lost() -> None:
    """AC1: a Pod with phase Failed, reason Evicted and a terminated worker container
    observes as LOST.  This is the core regression for issue 133.

    The fake API's `evict` method now produces a status with a terminated container
    (to mirror a real node-pressure eviction).  Without the fix in `observe`, the
    `_terminated_state` branch fires first and returns EXITED with exit code 1 --
    a silent misclassification.  After the fix the lost-reasons check fires first.
    """
    _api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)

    # The fake API's evict now sets a terminated containerStatus.
    _api.evict(launch.attempt_id)
    observation = await provider.observe(handle)

    assert observation.state is ObservationState.LOST
    assert "Evicted" in (observation.detail or "")


@pytest.mark.asyncio
async def test_ac2_failed_pod_without_lost_reason_observes_as_exit_with_code() -> None:
    """AC2: a Pod with phase Failed and a terminated container with no lost reason
    still observes as EXITED with its exit code.

    Not every Failed Pod is lost: a non-eviction failure with a terminated container
    (e.g. a container crash) should still report EXITED, not LOST.
    """
    _api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    _ = await provider.observe(handle)  # ensure RUNNING -> see pod

    # Manually set a Failed status with a terminated container, but with a
    # reason that is NOT in _LOST_REASONS.  This mimics a container crash
    # (reason="Error") where the worker did actually run.
    pod = await provider._pod_of(handle.ref)
    assert pod is not None
    pod["status"] = {
        "phase": "Failed",
        "reason": "Error",
        "containerStatuses": [
            {
                "name": CONTAINER_NAME,
                "state": {"terminated": {"exitCode": 42, "reason": "Error"}},
            }
        ],
    }

    observation = await provider.observe(handle)
    assert observation.state is ObservationState.EXITED
    assert observation.exit_code == 42


@pytest.mark.asyncio
async def test_ac3_oomkilled_still_observes_as_exit_with_oom_killed() -> None:
    """AC3: OOMKilled still observes as EXITED with oom_killed set.

    OOMKilled is a terminated container reason that must be preserved in EXITED
    with the oom_killed flag.  Moving the lost-reasons check must not affect this.
    """
    _api, _registry, provider = build()
    launch = spec()
    _api.script(launch.attempt_id, "oom", after=1)
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)

    observation = await provider.observe(handle)
    while observation.state is ObservationState.RUNNING:
        observation = await provider.observe(handle)

    assert observation.state is ObservationState.EXITED
    assert observation.exit_code == 137
    assert observation.oom_killed is True
