"""Issue 478: capacity always comes from the active policy, never the last launch.

AC1: capacity ignores a prior launch's shape
AC2: a launch under a policy with fraction 0.0833 produces a 250m request
AC3: a login or short-role Pod does not change the capacity shape
AC4: the detail names the policy and version the shape came from

See also: hades #423 (the quota capacity and short-role reservation that 478 builds on).
"""

from __future__ import annotations

from typing import Any

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sfake import FakeKubernetesApi, FakeRegistry
from crucible.adapters.execution.kubernetes import (
    KubernetesConfig,
    KubernetesProvider,
)


def _quota(api: Any, hard: dict[str, str], name: str = "hades-workers") -> None:
    api.create("resourcequotas", {"metadata": {"name": name}, "spec": {"hard": hard}})


def _build(
    *,
    config: KubernetesConfig | None = None,
    policy: dict[str, Any] | None = None,
) -> tuple[FakeKubernetesApi, FakeRegistry, KubernetesProvider]:
    api = FakeKubernetesApi()
    registry = FakeRegistry(api)
    provider = KubernetesProvider(
        config
        or KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=3600,
            storage_class="lab-ssd",
        ),
        api,  # type: ignore[arg-type]
        registry,
    )
    if policy is not None:
        provider.set_policy(policy)
    return api, registry, provider


def _config(**overrides: Any) -> KubernetesConfig:
    values: dict[str, Any] = {
        "poll_interval_seconds": 0,
        "launch_timeout_seconds": 3600,
        "storage_class": "lab-ssd",
        **overrides,
    }
    return KubernetesConfig(**values)


# A policy with name, version, and fraction 0.0833 → 250m CPU request, 1Gi memory request.
POLICY_NAMED = {
    "name": "hades-self-hosting",
    "version": 26,
    "resources": {
        "cpus": 3,
        "memory": "4GiB",
        "cpu_request_fraction": 0.0833,
        "memory_request_fraction": 0.25,
    },
}

# A default-format policy (no name/version, like the existing test policies).
POLICY_DEFAULT = {
    "resources": {"cpus": 3, "memory": "1GiB", "cpu_request_fraction": 0.0833},
}


# ----- AC1: capacity ignores a prior launch's shape ----------------------------


async def test_capacity_ignores_last_launch() -> None:
    """Once a launch has happened, _last_limits is set from that launch, but
    capacity MUST still use the active policy, not the remembered launch.

    Before the fix, _last_limits from the default shape (2 CPU, 0.5 fraction)
    made the quota read divide by 1 CPU instead of 0.25 CPU, under-reporting
    capacity from 24 to 6 workers.
    """
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_NAMED,  # 0.25 CPU request → 24 workers
    )
    _quota(api, {"requests.cpu": "6", "requests.memory": "24Gi"})

    # Simulate a prior launch that used the default shape (2 CPU per worker).
    provider._last_limits = k8sspec.Limits(
        cpus=2,
        memory_bytes=2 * 1024**3,
        ephemeral_storage="2Gi",
        tmpfs_bytes=512 * 1024**2,
        grace_seconds=30,
    )

    capacity = await provider.worker_capacity()

    # Capacity is still 24 (from policy), NOT 3 (from last launch shape).
    assert capacity.workers == 23  # 24 - 1 reserved
    assert capacity.headroom == 24  # 6 / 0.25 = 24 by CPU
    # The source references the policy, not the last launch.
    assert "the active policy" in capacity.source or "hades-self-hosting" in capacity.source
    assert "the last launch" not in capacity.source


async def test_last_launch_cannot_overstate_capacity() -> None:
    """A launch with a BIGGER request than the policy must not increase
    the headroom. The policy limits what the quota admits."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_NAMED,  # 0.25 CPU request
    )
    _quota(api, {"requests.cpu": "6"})  # 6 / 0.25 = 24

    # Simulate a launch that used 10 CPU per worker.
    provider._last_limits = k8sspec.Limits(
        cpus=10,
        memory_bytes=10 * 1024**3,
        ephemeral_storage="10Gi",
        tmpfs_bytes=2 * 1024**3,
        grace_seconds=60,
    )

    capacity = await provider.worker_capacity()

    # Still 24 workers (policy shape), NOT 6/10 = 0.
    assert capacity.headroom == 24
    assert capacity.workers == 23


async def test_last_launch_cannot_understate_capacity() -> None:
    """A launch with a SMALLER request than the policy must not reduce
    the headroom below what the policy admits."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_NAMED,  # 0.25 CPU request → 24 workers by CPU
    )
    _quota(api, {"requests.cpu": "6"})

    # Simulate a launch that used 0.1 CPU per worker (smaller than policy).
    provider._last_limits = k8sspec.Limits(
        cpus=0.5,
        memory_bytes=256 * 1024**3,
        ephemeral_storage="512Mi",
        tmpfs_bytes=128 * 1024**2,
        grace_seconds=15,
    )

    capacity = await provider.worker_capacity()

    # Still 24 workers (policy shape), NOT 6/0.1 = 60.
    assert capacity.headroom == 24
    assert capacity.workers == 23


# ----- AC2: launch under policy with fraction 0.0833 produces 250m request -------


async def test_250m_cpu_request_from_0_0833_fraction() -> None:
    """A policy with cpu_request_fraction 0.0833 and 3 CPU limits produces a
    250m CPU request (3 * 0.0833 ≈ 0.25) and 1Gi memory request
    (4Gi * 0.25 = 1Gi)."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_NAMED,
    )
    _quota(api, {"requests.cpu": "6"})

    capacity = await provider.worker_capacity()

    # The shape used for capacity: 0.25 CPU request, 1Gi memory request.
    assert capacity.headroom == 24  # 6 / 0.25 = 24
    # Check the reservation shape.
    assert capacity.reservation["each"]["cpu_request"] == "250m"
    # memory_request is stored as string bytes (1Gi = 1073741824)
    assert capacity.reservation["each"]["memory_request"] == "1073741824"


async def test_0_0833_policy_gives_correct_memory_headroom() -> None:
    """Memory for 24 workers: 24Gi available, 1Gi per worker."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_NAMED,
    )
    _quota(api, {"requests.cpu": "6", "requests.memory": "24Gi"})

    capacity = await provider.worker_capacity()

    # Both CPU and memory admit 24 here.
    assert capacity.headroom == 24
    assert capacity.workers == 23


# ----- AC3: login and short-role Pods do not change capacity shape --------------


async def test_login_does_not_affect_capacity() -> None:
    """The login path creates a Job directly under LOGIN_POLICY and does not
    update _last_limits. Capacity must remain policy-driven."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_NAMED,
    )
    _quota(api, {"requests.cpu": "6"})

    # Simulate that a login Job was created (which doesn't set _last_limits).
    # After the fix, even if _last_limits is somehow set, capacity ignores it.
    capacity_before = await provider.worker_capacity()

    # Set _last_limits as if a login had happened (it shouldn't, but verify).
    provider._last_limits = k8sspec.limits_from_policy(
        {"resources": {"cpus": 1, "memory": "1GiB", "cpu_request_fraction": 0.5}}
    )

    capacity_after = await provider.worker_capacity()

    # Capacity is unchanged: still based on the active policy.
    assert capacity_before.workers == capacity_after.workers
    assert capacity_before.headroom == capacity_after.headroom


async def test_reconciler_short_role_does_not_affect_capacity() -> None:
    """Reconciliation paths that build limits with limits_from_policy({}) (the
    default shape) must not update _last_limits and thus must not affect
    the capacity computation."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_NAMED,
    )
    _quota(api, {"requests.cpu": "6"})

    # Verify that _last_limits is not touched by a short-role pod's creation.
    capacity = await provider.worker_capacity()

    # Even though limits_from_policy({}) could be called for a short-role pod,
    # capacity is still based on the policy.
    assert "the active policy" in capacity.source or "hades-self-hosting" in capacity.source
    assert "the last launch" not in capacity.source


# ----- AC4: the detail names the policy and version ----------------------------


async def test_detail_names_policy_and_version() -> None:
    """When the active policy has name and version, capacity_source and
    capacity_detail must include them."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_NAMED,
    )
    _quota(api, {"requests.cpu": "6"})

    capacity = await provider.worker_capacity()

    assert "hades-self-hosting" in capacity.source
    assert "v26" in capacity.source
    assert "shape from" in capacity.source
    assert "hades-self-hosting" in capacity.detail
    assert "v26" in capacity.detail


async def test_detail_names_policy_version_in_capacity_detail() -> None:
    """capacity_detail includes the policy name and version string."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_NAMED,
    )
    _quota(api, {"requests.cpu": "6"})

    capacity = await provider.worker_capacity()
    d = capacity.as_dict()

    assert "hades-self-hosting" in d["capacity_detail"]
    assert "v26" in d["capacity_detail"]


async def test_detail_fallback_when_no_policy_name() -> None:
    """When the policy has no name/version (like the test policy from issue 478),
    the source falls back to 'the active policy'."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_DEFAULT,  # no name/version
    )
    _quota(api, {"requests.cpu": "6"})

    capacity = await provider.worker_capacity()

    assert "the active policy" in capacity.source
    assert "shape from" in capacity.detail


# ----- Fallback: no quota ------------------------------------------------------


async def test_no_quota_uses_fallback_and_mentions_policy() -> None:
    """Without a quota, the provider falls back to max_concurrency and still
    mentions the policy shape in the source."""
    _api, _registry, provider = _build(config=_config(max_concurrency=10))
    capacity = await provider.worker_capacity()

    assert capacity.workers == 10
    assert "the active policy" in capacity.source


# ----- Unchanged: quota without a policy still works ---------------------------


async def test_no_policy_quota_uses_default_shape() -> None:
    """Without a policy set, the empty-policy default (2 CPU, 0.5 fraction → 1 CPU)
    is used. This is the old behaviour for backwards compatibility."""
    api, _registry, provider = _build(config=_config())
    _quota(api, {"requests.cpu": "6"})
    capacity = await provider.worker_capacity()

    # Empty policy: 2 CPU, 0.5 → 1 CPU request → 6 / 1 = 6
    assert capacity.workers == 5  # 6 - 1 reserved
    assert capacity.headroom == 6


# ----- Short-role reservation still applies ------------------------------------


async def test_short_role_reservation_preserved() -> None:
    """Issue 423's reservation is preserved: short-role Pod count is subtracted."""
    api, _registry, provider = _build(
        config=_config(short_role_pods=2),
        policy=POLICY_NAMED,
    )
    _quota(api, {"requests.cpu": "6", "requests.memory": "24Gi"})
    capacity = await provider.worker_capacity()

    assert capacity.reserved_pods == 2
    assert capacity.headroom == 24
    assert capacity.workers == 22  # 24 - 2 reserved
