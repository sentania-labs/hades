"""Issue 478: quota capacity divides by the policy request shape, not the last launch or
a 1 CPU default.

When a namespace quota has requests.cpu 6 and an active policy requests 0.25 CPU per
worker (cpu_request_fraction 0.0833 on a 3 CPU limit), the capacity must be 24 (6 / 0.25),
not 6 (6 / 1.0 default). The provider uses the active policy's Limits to derive the
per-attempt shape when no launch has happened yet; it falls back to the last launch only
after one, and never falls back to an empty-policy default when a policy exists.

AC1: Capacity is computed from the active policies' request shape, not the last launch or
     the empty default.
AC2: The advertised number updates on the next probe after a policy publish or a quota change.
AC3: The Providers page shows the row and the shape that produced the capacity.

See also: hades #423 (the quota capacity and short-role reservation that 478 builds on).
"""

from __future__ import annotations

from typing import Any

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sfake import FakeKubernetesApi, FakeRegistry
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.adapters.ui.pages.routing import _capacity_words
from crucible.application.admin import kubernetes as kubernetes_admin


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


# A policy requesting 0.25 CPU (3 CPU limit * 0.0833 fraction ≈ 0.25) and 1Gi memory.
POLICY_0_25_CPU = {
    "resources": {"cpus": 3, "memory": "1GiB", "cpu_request_fraction": 0.0833},
}

# A policy requesting 1 CPU (2 CPU limit * 0.5 default fraction).
POLICY_1_CPU = {"resources": {"cpus": 2, "memory": "1GiB"}}


# ----- AC1: capacity from policy shape, not default ---------------------------


async def test_0_25_cpu_policy_on_requests_cpu_6_quota_yields_24_workers() -> None:
    """A quota with requests.cpu 6 and a policy requesting 0.25 CPU yields 24 workers
    before the short-role reservation (headroom 24), 23 after. No prior launch:
    _last_limits is None, so _read_quota falls back to the active policy (set via
    set_policy), not the empty-policy default."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_0_25_CPU,
    )
    _quota(api, {"requests.cpu": "6", "requests.memory": "24Gi"})
    capacity = await provider.worker_capacity()

    assert capacity.workers == 23  # 24 - 1 reserved
    assert capacity.headroom == 24  # 6 / 0.25 = 24 workers the quota admits
    assert capacity.reserved_pods == 1
    # The source mentions the active policy, not "default" or an empty policy.
    assert "the active policy" in capacity.source
    assert "requests.cpu" in capacity.source
    # The detail also includes shape info.
    assert "shape from" in capacity.detail


async def test_0_25_cpu_policy_still_computes_memory_correctly() -> None:
    """Memory for 24 workers: 24 GiB is available, each worker requests 1 GiB.
    Both CPU and memory admit the same number here."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_0_25_CPU,
    )
    _quota(api, {"requests.cpu": "6", "requests.memory": "24Gi"})
    capacity = await provider.worker_capacity()

    assert capacity.workers == 23  # 24 - 1 reserved
    assert capacity.headroom == 24
    assert "requests.cpu" in capacity.source or "requests.memory" in capacity.source


async def test_memory_fewer_workers_when_quota_memory_is_tighter() -> None:
    """requests.memory 12Gi with 1Gi requests → 12 workers by memory, fewer than CPU's 24."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_0_25_CPU,
    )
    _quota(api, {"requests.cpu": "6", "requests.memory": "12Gi"})
    capacity = await provider.worker_capacity()

    assert capacity.workers == 11  # 12 - 1 reserved
    assert capacity.headroom == 12  # limited by memory
    assert "requests.memory" in capacity.source


async def test_1_cpu_policy_on_requests_cpu_6_quota_yields_6_workers() -> None:
    """With a 1-CPU-request policy (the shape that 423's tests use), requests.cpu 6
    yields 6 workers — the same as the old default. This proves the math is correct
    when the policy's request matches the old default."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_1_CPU,
    )
    _quota(api, {"requests.cpu": "6", "requests.memory": "24Gi"})
    capacity = await provider.worker_capacity()

    assert capacity.workers == 5  # 6 - 1 reserved
    assert capacity.headroom == 6
    assert "the active policy" in capacity.source


async def test_last_launch_overrides_policy() -> None:
    """Once a launch has happened, _last_limits (set from the launch spec) takes
    precedence over the active policy. A bigger request in the launch than in the
    policy should reduce capacity."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_0_25_CPU,  # 0.25 CPU request → would give 24
    )
    _quota(api, {"requests.cpu": "6", "requests.memory": "24Gi"})
    # Simulate a prior launch that used 2 CPU per worker.
    provider._last_limits = k8sspec.Limits(
        cpus=2,
        memory_bytes=4 * 1024**3,
        ephemeral_storage="2Gi",
        tmpfs_bytes=512 * 1024**2,
        grace_seconds=30,
    )
    capacity = await provider.worker_capacity()

    # Now the capacity is 6 / 2 = 3 workers (by CPU), NOT 24.
    assert capacity.workers == 2  # 3 - 1 reserved
    assert "the last launch" in capacity.source


# ----- AC2: capacity updates on next probe after policy change ----------------


async def test_publishing_a_new_policy_version_changes_capacity_on_next_probe() -> None:
    """After a policy publish, the next probe must reflect the new shape."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_1_CPU,  # 1 CPU request → 6 workers, 5 after reservation
    )
    _quota(api, {"requests.cpu": "6", "requests.memory": "24Gi"})

    capacity = await provider.worker_capacity()
    assert capacity.workers == 5
    assert capacity.headroom == 6
    assert "the active policy" in capacity.source

    # Publish a new policy version with 0.25 CPU request.
    provider.set_policy(POLICY_0_25_CPU)

    capacity2 = await provider.worker_capacity()
    assert capacity2.workers == 23  # 24 - 1 reserved
    assert capacity2.headroom == 24
    assert "the active policy" in capacity2.source


async def test_quota_change_reflected_on_next_probe() -> None:
    """When the quota changes, the next probe picks up the new numbers."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_1_CPU,
    )
    _quota(api, {"requests.cpu": "6", "requests.memory": "24Gi"})

    capacity = await provider.worker_capacity()
    assert capacity.workers == 5
    assert capacity.headroom == 6

    # Change the quota: update the existing quota object's hard values.
    obj = api.objects[("resourcequotas", "hades-workers")]
    obj.body["spec"]["hard"]["requests.cpu"] = "12"
    obj.body["spec"]["hard"]["requests.memory"] = "48Gi"

    capacity2 = await provider.worker_capacity()
    assert capacity2.workers == 11  # 12 - 1 reserved
    assert capacity2.headroom == 12


# ----- AC3: the Providers page shows shape and quota row ----------------------


async def test_capacity_words_shows_shape_from_policy() -> None:
    """The _capacity_words helper (used on the Providers page) includes the shape
    source when it comes from the active policy."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_0_25_CPU,
    )
    _quota(api, {"requests.cpu": "6"})

    capacity = await provider.worker_capacity()
    view = {"provider_enabled": True, **capacity.as_dict()}
    words = _capacity_words(view)

    assert "shape from the active policy" in words
    assert "23 worker" in words  # 24 - 1 reserved
    assert "1 kept for short-role Pods" in words


async def test_capacity_words_shows_shape_from_last_launch() -> None:
    """When capacity comes from the last launch, the Providers page says so."""
    api, _registry, provider = _build(config=_config())
    _quota(api, {"limits.cpu": "32", "limits.memory": "128Gi"})
    provider._last_limits = k8sspec.limits_from_policy({"resources": {"cpus": 3, "memory": "4GiB"}})

    capacity = await provider.worker_capacity()
    view = {"provider_enabled": True, **capacity.as_dict()}
    words = _capacity_words(view)

    assert "shape from the last launch" in words


async def test_admin_capacity_view_exposes_shape_in_detail_and_source() -> None:
    """kubernetes_admin.capacity_view returns the full capacity dict, including the
    source and detail that show the shape source."""
    api, _registry, provider = _build(
        config=_config(),
        policy=POLICY_0_25_CPU,
    )
    _quota(api, {"requests.cpu": "6"})

    ctx = type("Ctx", (), {"providers": {"kubernetes": provider}, "kubernetes_egress_seed": None})()
    view = await kubernetes_admin.capacity_view(ctx)

    assert view["provider_enabled"] is True
    assert view["worker_capacity"] == 23
    assert view["quota_headroom"] == 24
    assert "the active policy" in view["capacity_source"]
    assert "shape from" in view["capacity_detail"]


# ----- fallback: no policy, no quota, no launch -------------------------------


async def test_no_policy_no_quota_uses_config_fallback() -> None:
    """With no policy set, no quota, and no launch, the provider falls back to the
    configured max_concurrency."""
    _api, _registry, provider = _build(config=_config(max_concurrency=5))
    capacity = await provider.worker_capacity()

    assert capacity.workers == 5
    assert capacity.headroom is None  # no quota


async def test_no_policy_with_quota_still_works() -> None:
    """Without a policy but with a quota, the empty-policy default (2 CPU, 0.5
    fraction → 1 CPU request) is used. This is the old behaviour for backwards
    compatibility when no policy is set."""
    api, _registry, provider = _build(config=_config())
    _quota(api, {"requests.cpu": "6"})
    capacity = await provider.worker_capacity()

    # Empty policy: 2 CPU, 0.5 fraction → 1 CPU request → 6 / 1 = 6
    assert capacity.workers == 5  # 6 - 1 reserved
    assert capacity.headroom == 6
    assert "policy" in capacity.source  # still mentions policy, just empty default


async def test_short_role_reservation_still_applies() -> None:
    """Issue 423 B's reservation is preserved: the short-role Pod count is subtracted
    from headroom, leaving the correct worker count."""
    api, _registry, provider = _build(
        config=_config(short_role_pods=2),
        policy=POLICY_0_25_CPU,
    )
    _quota(api, {"requests.cpu": "6", "requests.memory": "24Gi"})
    capacity = await provider.worker_capacity()

    # 24 workers by CPU, but we reserve 2 pods for short roles.
    assert capacity.reserved_pods == 2
    assert capacity.headroom == 24
    assert capacity.workers == 22  # 24 - 2 reserved
