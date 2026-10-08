"""Tests for issue 205: allowlist address refresh with overlap window.

When a long-running worker Pod's allowlisted hostname resolves to a new
address, the provider re-resolves on the configured TTL interval.  If the
DNS answer changes, the provider's `_refresh_addresses` method patches the
NetworkPolicy to include both the old and new addresses (overlap window,
205).  After the window the old address is dropped.

AC1: The policy permits both old and new addresses during the overlap
window.

AC2: After the window the old address is removed.

Both are proved by unit tests that control the DNS resolution
(`dual_resolve`), launch an attempt with the default spec, and call
`observe()` to exercise the refresh path on a FakeKubernetesApi.

"""

from __future__ import annotations

import time
from typing import Any

from crucible.adapters.execution.kubernetes import KubernetesConfig
from tests.unit.kubernetes_fixtures import build
from tests.unit.test_kubernetes_network_policy import spec as net_spec


# Dual-resolver: first call → 8.8.8.8, all subsequent calls → 8.8.4.4
_call_count: list[int] = [0]


def dual_resolve(host: str) -> list[str]:
    if _call_count[0] == 0:
        _call_count[0] += 1
        return ["8.8.8.8/32"]
    _call_count[0] += 1
    return ["8.8.4.4/32"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _policy_name(attempt_id: str) -> str:
    return f"np-worker-{attempt_id}"


def _get_policy_from_api(api: Any, attempt_id: str) -> dict[str, Any]:
    """Find the NetworkPolicy body from the fake API (read live objects)."""
    np_name = _policy_name(attempt_id)
    obj = api.objects.get(("networkpolicies", np_name))
    assert obj is not None, f"policy {np_name} not found in objects={list(api.objects.keys())}"
    return dict(obj.body)


def _extract_cidrs(policy: dict[str, Any]) -> list[str]:
    """Return every ipBlock.cidr from the policy's egress rules."""
    cidrs: list[str] = []
    for rule in policy.get("spec", {}).get("egress", []):
        for dest in rule.get("to", []):
            block = dest.get("ipBlock")
            if block:
                cidrs.extend(c.strip() for c in block.get("cidr", "").split(",") if c.strip())
    return sorted(cidrs)


# ---------------------------------------------------------------------------
# AC1: old + new addresses allowed during overlap window
# ---------------------------------------------------------------------------


async def test_ac1_old_and_new_addresses_allowed() -> None:
    _call_count[0] = 0  # fresh resolver

    config = KubernetesConfig(
        poll_interval_seconds=0.01,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        credential_modes={"codex": "rw_narrow"},
        resolve_ttl_seconds=0.05,
        address_overlap_window_seconds=60.0,
    )
    api, registry, provider = build(config=config, resolver=dual_resolve)
    launch_spec = net_spec()  # egress_allowlist=["pypi.org", "github.com"]

    # Prepare and launch the worker.  The first resolver call returns 8.8.8.8
    # for github.com and 8.8.4.4 for pypi.org (two resolver calls).
    ws = await provider.prepare(launch_spec)
    handle = await provider.launch(ws, launch_spec)
    launched = provider._launched[launch_spec.attempt_id]

    assert launched.network_policy is not None
    assert launched.egress_hosts is not None

    # Let the fake worker finish (it does; we check policy afterwards).
    while (await provider.observe(handle)).state.value == "running":
        time.sleep(0.01)

    # Check the initial policy — should contain both initial addresses.
    initial_policy = _get_policy_from_api(api, launched.attempt_id)
    cidrs = _extract_cidrs(initial_policy)
    assert "8.8.8.8/32" in cidrs, f"Initial policy should have both addresses, got {cidrs}"
    assert "8.8.4.4/32" in cidrs, f"Initial policy should have both addresses, got {cidrs}"

    # Now advance time past resolve_ttl_seconds so that _refresh_addresses
    # re-resolves.  The resolver returns 8.8.4.4 for both hosts (subsequent
    # calls).  _refresh_addresses should see a change and patch the policy
    # to include both old and new addresses.
    time.sleep(0.15)
    _call_count[0] = 0  # fresh resolver: first call returns 8.8.8.8 again

    # The job has already completed, but the KubernetesProvider still tracks
    # the _Launched record.  We trigger observe() again so it goes through
    # the refresh path.
    await provider.observe(launch_spec.attempt_id)

    policy = _get_policy_from_api(api, launched.attempt_id)
    cidrs = _extract_cidrs(policy)

    # AC1: Both old and new addresses present during overlap window.
    assert "8.8.8.8/32" in cidrs, f"AC1: old address still present during overlap window, got {cidrs}"
    assert "8.8.4.4/32" in cidrs, f"AC1: new address also present, got {cidrs}"


# ---------------------------------------------------------------------------
# AC2: old address removed after overlap window
# ---------------------------------------------------------------------------


async def test_ac2_old_address_removed_after_window() -> None:
    _call_count[0] = 0  # fresh resolver

    config = KubernetesConfig(
        poll_interval_seconds=0.01,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        credential_modes={"codex": "rw_narrow"},
        resolve_ttl_seconds=0.05,
        address_overlap_window_seconds=0.1,  # short window for testing
    )
    api, registry, provider = build(config=config, resolver=dual_resolve)
    launch_spec = net_spec()

    ws = await provider.prepare(launch_spec)
    handle = await provider.launch(ws, launch_spec)
    launched = provider._launched[launch_spec.attempt_id]

    assert launched.network_policy is not None

    while (await provider.observe(handle)).state.value == "running":
        time.sleep(0.01)

    # Initial policy has both initial addresses.
    initial_policy = _get_policy_from_api(api, launched.attempt_id)
    cidrs = _extract_cidrs(initial_policy)
    assert "8.8.8.8/32" in cidrs, f"Initial should have both, got {cidrs}"
    assert "8.8.4.4/32" in cidrs, f"Initial should have both, got {cidrs}"

    # Trigger refresh: advance past TTL so resolver re-resolves.
    time.sleep(0.15)
    _call_count[0] = 0  # fresh resolver: first call returns 8.8.8.8

    # First observe after TTL → addresses changed → overlap window starts.
    await provider.observe(launch_spec.attempt_id)

    policy = _get_policy_from_api(api, launched.attempt_id)
    cidrs = _extract_cidrs(policy)
    # During overlap window: both old and new present.
    assert "8.8.8.8/32" in cidrs, f"Overlap window: old + new, got {cidrs}"
    assert "8.8.4.4/32" in cidrs, f"Overlap window: new address, got {cidrs}"

    # Advance past overlap window.
    time.sleep(0.5)
    _call_count[0] = 0  # fresh resolver again

    # Second observe → overlap expired → old addresses removed.
    await provider.observe(launch_spec.attempt_id)

    policy = _get_policy_from_api(api, launched.attempt_id)
    cidrs = _extract_cidrs(policy)
    # AC2: Only the new address should remain after window.
    assert "8.8.4.4/32" in cidrs, f"AC2: new address still present, got {cidrs}"
    assert "8.8.8.8/32" not in cidrs, f"AC2: old address removed, got {cidrs}"
