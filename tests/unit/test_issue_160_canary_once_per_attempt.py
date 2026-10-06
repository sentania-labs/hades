"""Issue 160: readiness canary runs once per attempt while the local endpoint is unsettled.

When the local endpoint is unreachable, _probe_is_settled returns False because
local_endpoint_detail is set. Both prepare (via _require_ready) and launch (also
via _require_ready) call ensure_ready(), which without a cache window would run
the two-Pod canary twice for the same attempt.

This test verifies that prepare and launch of one attempt with an unsettled
endpoint run the canary once, and that after the flag from prepare is cleared
(by a subsequent prepare or launch) the canary runs again so a recovered
endpoint becomes settled without a restart.
"""

from __future__ import annotations

from typing import Any

import pytest

from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.execution.kubernetes import (
    KubernetesConfig,
    KubernetesProvider,
)
from tests.unit.kubernetes_fixtures import build, fake_resolver, spec

IMAGE = "crucible-worker:script-harness-fake-succeed-2"


def _count_canary_pods(api: FakeKubernetesApi) -> int:
    """Count canary Pods created in this api run.

    The fake creates Pods directly (not Jobs), so we look for kind=pods
    with the canary naming prefix.
    """
    return sum(1 for row in api.created if row["kind"] == "pods" and "canary-" in row["name"])


def _build_unsettled_endpoint() -> tuple[FakeKubernetesApi, Any, KubernetesProvider]:
    """Build a provider that produces an unsettled probe.

    We need an unsettled but PASSED probe:
    - egress_enforced=True: canary cannot reach API (expected pass).
    - canary_answer="unreachable": the API check reports unreachable, which
      is the expected pass under egress enforcement.
    - local_endpoint_url configured: the second canary also checks the local
      endpoint.
    - canary_endpoint="unreachable": the endpoint check fails, but per
      _read_probe this is kept out of `problems` (so probe.passed stays True)
      and only sets endpoint_detail (making the probe unsettled).
    """
    resolver_fns = fake_resolver

    def patched_resolver(host: str) -> list[str]:
        if host == "llm.example.local":
            return ["10.10.0.100"]
        return resolver_fns(host)

    return build(
        resolver=patched_resolver,
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            local_endpoint_url="https://llm.example.local/v1",
            local_endpoint_cidrs=("10.10.0.0/24",),
        ),
        canary_endpoint="unreachable",
    )


@pytest.mark.asyncio
async def test_prepare_and_launch_run_canary_once_when_unsettled() -> None:
    """AC1: prepare and launch of one attempt with an unsettled endpoint run the
    canary once.

    Both _require_ready (called from prepare and launch) call ensure_ready().
    When the probe is unsettled (local_endpoint_detail is set), ensure_ready()
    should cache the result for a bounded window so the same attempt's launch
    does not run a second canary.

    The mechanism is a flag (_probe_cached_for_launch) set by a successful
    prepare and cleared by the next prepare. Within that window a
    subsequent ensure_ready() call returns the cached probe without
    re-running the canary.
    """
    api, _registry, provider = _build_unsettled_endpoint()

    # First prepare: runs the canary once (2 pods). The flag is set at the end.
    launch1 = spec()
    workspace = await provider.prepare(launch1)
    assert workspace is not None
    assert _count_canary_pods(api) == 2, (
        f"expected 2 canary pods after first prepare, got {_count_canary_pods(api)}"
    )
    assert provider._probe_cached_for_launch is True
    # The probe is unsettled (local_endpoint_detail set) but passed=True.
    assert provider.probe is not None
    assert provider.probe.passed is True
    assert provider.probe.local_endpoint_detail is not None

    # ensure_ready() within the window (flag is True) should return the cache
    # without running a new canary.
    probe2 = await provider.ensure_ready()
    assert _count_canary_pods(api) == 2, (
        f"ensure_ready within window: expected 2 canary pods, got {_count_canary_pods(api)}"
    )
    assert probe2.local_endpoint_detail is not None

    # Now launch: it calls _require_ready -> ensure_ready(). The flag is still
    # True from prepare. ensure_ready() returns the cache, so no new canary.
    await provider.launch(workspace, launch1)
    assert _count_canary_pods(api) == 2, (
        f"launch should not re-run canary: expected 2 canary pods, got {_count_canary_pods(api)}"
    )


@pytest.mark.asyncio
async def test_after_prepare_clears_flag_canary_runs_again() -> None:
    """AC2: after the flag is cleared (by a subsequent prepare or launch) the
    canary runs again and a recovered endpoint becomes settled.

    The flag is cleared at the start of `prepare` (for isolation from prior
    attempts). A new prepare therefore runs the canary again.
    """
    api, _registry, provider = _build_unsettled_endpoint()

    # First prepare: runs the canary, sets the flag.
    launch1 = spec()
    workspace1 = await provider.prepare(launch1)
    assert workspace1 is not None

    assert _count_canary_pods(api) == 2
    assert provider._probe_cached_for_launch is True

    # Second prepare: clears the flag at the start (running a fresh canary),
    # then sets it again at the end.
    launch2 = spec()
    workspace2 = await provider.prepare(launch2)
    assert workspace2 is not None

    # Second prepare ran its own canary (2 more pods).
    assert _count_canary_pods(api) == 4, (
        f"expected 4 canary pods after second prepare, got {_count_canary_pods(api)}"
    )
    assert provider._probe_cached_for_launch is True

    # Third prepare: clears the flag, runs a third canary.
    launch3 = spec()
    await provider.prepare(launch3)

    assert _count_canary_pods(api) == 6, (
        f"expected 6 canary pods after third prepare, got {_count_canary_pods(api)}"
    )


@pytest.mark.asyncio
async def test_endpoint_recovery_after_flag_cleared() -> None:
    """AC2 (recovery): after the flag is cleared by a second prepare, the canary
    picks up endpoint recovery without a restart.

    The flag is cleared at the start of `prepare`. A new prepare runs the canary
    fresh, which reflects the current canary_endpoint state.
    """
    api, _registry, provider = _build_unsettled_endpoint()

    # First prepare: endpoint is unreachable (unsettled but passed).
    launch1 = spec()
    await provider.prepare(launch1)
    assert _count_canary_pods(api) == 2

    # Change endpoint to reachable, then second prepare (clears flag, runs canary).
    api.canary_endpoint = "reachable"
    launch2 = spec()
    workspace2 = await provider.prepare(launch2)
    assert workspace2 is not None

    # The second canary ran: total 4 pods. The probe should now be settled.
    assert _count_canary_pods(api) == 4
    assert provider.probe is not None
    assert provider.probe.passed is True
    assert provider._probe_is_settled(provider.probe) is True
