"""The readiness canary proves what a worker needs from its egress rules (crucible#91).

26's canary proved only that the API server is unreachable, so a cluster whose CNI
never matched the DNS and local endpoint rules reported `egress_enforced: true` while
every attempt had no DNS and no model. The canary now runs under the rules a worker
gets and must also resolve a cluster name and, when a local endpoint is configured,
connect to it. Anything it cannot tell is inconclusive, never a pass.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sfake import FakeKubernetesApi, FakeRegistry
from crucible.adapters.execution.kubernetes import (
    KubernetesConfig,
    KubernetesProvider,
    NamespaceProbe,
    _read_probe,
)
from crucible.domain.cluster_egress import ClusterEgress
from crucible.ports.execution import LaunchRefusedError
from tests.unit.kubernetes_fixtures import IMAGE, build, fake_resolver, spec

LITELLM = "http://litellm.litellm.svc.cluster.local:4000/v1"
IN_CLUSTER = ClusterEgress(
    endpoint_namespace="litellm", endpoint_pod_labels=(("app", "litellm"),), endpoint_port=4000
)


def config(**overrides: Any) -> KubernetesConfig:
    return KubernetesConfig(poll_interval_seconds=0, launch_timeout_seconds=5, **overrides)


def canary_policies(api: Any) -> list[dict[str, Any]]:
    return [
        row["body"]
        for row in api.created
        if row["kind"] == "networkpolicies"
        and row["body"]["spec"]["podSelector"]["matchLabels"][k8sspec.LABEL_ROLE]
        == k8sspec.ROLE_CANARY
    ]


async def resolved(provider: KubernetesProvider) -> None:
    """Resolve an attempt's image, which is what the canary runs, without `prepare`:
    `prepare` consults the probe itself (issue 59), and these tests watch it run."""
    await provider._resolve_image(spec())


async def ready(**build_kwargs: Any) -> tuple[Any, Any, NamespaceProbe]:
    api, _registry, provider = build(**build_kwargs)
    await resolved(provider)
    return api, provider, await provider.ensure_ready()


async def test_the_canary_runs_under_its_own_policy_and_removes_it() -> None:
    api, _provider, probe = await ready()
    assert probe.passed and probe.dns_resolves is True
    assert probe.local_endpoint_reachable is None
    [policy] = canary_policies(api)
    selector = policy["spec"]["podSelector"]["matchLabels"]
    canary = next(
        row["body"]
        for row in api.created
        if row["kind"] == "pods" and row["name"].startswith("crucible-canary-rules-")
    )
    # The policy selects the canary Pod and nothing else.
    assert canary["metadata"]["labels"][k8sspec.LABEL_CANARY] == selector[k8sspec.LABEL_CANARY]
    assert selector[k8sspec.LABEL_ROLE] == k8sspec.ROLE_CANARY
    # Cluster DNS and nothing else: no endpoint is configured.
    [rule] = policy["spec"]["egress"]
    assert {p["port"] for p in rule["ports"]} == {53}
    assert ("networkpolicies", policy["metadata"]["name"]) in api.deleted


async def test_a_dns_rule_that_does_not_match_fails_the_probe_by_name() -> None:
    """The canary on a cluster whose CNI never matches the DNS rule (issue 91)."""
    _api, provider, probe = await ready(canary_dns="failed")
    assert probe.passed is False
    assert probe.egress_enforced is True
    assert probe.dns_resolves is False
    assert probe.checked is True
    assert "DNS check failed" in probe.detail
    health = await provider.health()
    assert health.state == "degraded"
    assert health.checks["dns_resolves"] is False
    assert health.checks["namespace_ready"] is False


async def test_a_canary_without_a_resolver_tool_is_inconclusive_never_a_pass() -> None:
    _api, provider, probe = await ready(canary_dns="inconclusive")
    assert probe.passed is False
    assert probe.checked is False
    assert probe.dns_resolves is None
    assert "DNS check inconclusive" in probe.detail
    assert (await provider.health()).state == "degraded"


async def test_a_configured_local_endpoint_is_connected_to_under_its_selector() -> None:
    api, _provider, probe = await ready(
        config=config(egress=IN_CLUSTER, local_endpoint_url=LITELLM)
    )
    assert probe.passed, probe.detail
    assert probe.local_endpoint_reachable is True
    [policy] = canary_policies(api)
    peers = [d for rule in policy["spec"]["egress"] for d in rule["to"] if "ipBlock" not in d]
    assert {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "litellm"}},
        "podSelector": {"matchLabels": {"app": "litellm"}},
    } in peers
    canary = next(
        row["body"]
        for row in api.created
        if row["kind"] == "pods" and row["name"].startswith("crucible-canary-rules-")
    )
    env = {e["name"]: e["value"] for e in canary["spec"]["containers"][0]["env"]}
    assert env["CRUCIBLE_CANARY_ENDPOINT_URL"] == LITELLM


@pytest.mark.parametrize("answer", ["unreachable", "unresolved"])
async def test_an_unreachable_local_endpoint_refuses_only_launches_routed_to_it(
    answer: str,
) -> None:
    """crucible#110, the operator 2026-09-23: "a down provider should only block that
    provider." The endpoint result no longer fails the whole probe; it refuses only the
    launch whose own route (`LaunchSpec.endpoint`) is `local` (today: Hermes to the
    local gateway), and admits a launch routed elsewhere (a Claude Code or Codex
    attempt on a subscription endpoint, which never touches it)."""
    api, _registry, provider = build(
        config=config(egress=IN_CLUSTER, local_endpoint_url=LITELLM), canary_endpoint=answer
    )
    local_launch = spec(endpoint="local", endpoint_url=LITELLM)
    subscription_launch = spec(attempt_id="01ATTEMPT0000000000000000B")
    # Issue 59: the refusal comes at prepare, before any object of the attempt exists.
    with pytest.raises(LaunchRefusedError, match="local endpoint check failed"):
        await provider.prepare(local_launch)
    assert not api.object_names("persistentvolumeclaims")
    probe = await provider.ensure_ready()
    assert probe.passed is True
    assert probe.local_endpoint_reachable is False
    assert "local endpoint check failed" in probe.detail
    subscription_workspace = await provider.prepare(subscription_launch)
    await provider.launch(subscription_workspace, subscription_launch)


async def test_an_inconclusive_local_endpoint_check_refuses_only_launches_routed_to_it() -> None:
    _api, provider, probe = await ready(
        config=config(egress=IN_CLUSTER, local_endpoint_url=LITELLM),
        canary_endpoint="inconclusive",
    )
    assert probe.passed is True and probe.checked is False
    assert probe.local_endpoint_reachable is None
    assert "local endpoint check inconclusive" in probe.detail
    local_launch = spec(endpoint="local", endpoint_url=LITELLM)
    with pytest.raises(LaunchRefusedError, match="local endpoint check inconclusive"):
        await provider.prepare(local_launch)
    subscription_launch = spec(attempt_id="01ATTEMPT0000000000000000B")
    subscription_workspace = await provider.prepare(subscription_launch)
    await provider.launch(subscription_workspace, subscription_launch)


async def test_dns_down_refuses_every_launch_whatever_the_route() -> None:
    """crucible#110, requirement 5: DNS still gates every launch, whatever the route."""
    api, _registry, provider = build(
        config=config(egress=IN_CLUSTER, local_endpoint_url=LITELLM), canary_dns="failed"
    )
    local_launch = spec(endpoint="local", endpoint_url=LITELLM)
    subscription_launch = spec(attempt_id="01ATTEMPT0000000000000000B")
    with pytest.raises(LaunchRefusedError, match="the workers namespace is not ready"):
        await provider.prepare(local_launch)
    with pytest.raises(LaunchRefusedError, match="the workers namespace is not ready"):
        await provider.prepare(subscription_launch)
    probe = await provider.ensure_ready()
    assert probe.passed is False
    assert not api.object_names("persistentvolumeclaims")


async def test_an_endpoint_recovery_admits_hermes_again_with_no_manual_reset() -> None:
    """crucible#110: a probe that only failed on the local endpoint must not be kept
    once it is settled false, or a gateway blip refuses Hermes until a settings change.
    `ensure_ready()` alone, with no cache reset, must pick the recovery up."""
    api, _registry, provider = build(
        config=config(egress=IN_CLUSTER, local_endpoint_url=LITELLM), canary_endpoint="unreachable"
    )
    local_launch = spec(endpoint="local", endpoint_url=LITELLM)
    with pytest.raises(LaunchRefusedError, match="local endpoint check failed"):
        await provider.prepare(local_launch)
    probe = await provider.ensure_ready()
    assert probe.passed is True and probe.local_endpoint_reachable is False

    api.canary_endpoint = "reachable"
    probe = await provider.ensure_ready()
    assert probe.passed is True
    assert probe.local_endpoint_reachable is True
    local_workspace = await provider.prepare(local_launch)
    await provider.launch(local_workspace, local_launch)


async def test_an_endpoint_no_rule_can_permit_refuses_only_launches_routed_to_it() -> None:
    """Out of the cluster, a name that resolves into a denied range with no
    `local_endpoint_cidrs` declaration is refused for a worker; the canary says so. That
    is the endpoint's failure, not the namespace's (crucible#110): the worker rules
    canary still runs without the endpoint, so DNS keeps its own answer, and only a
    launch routed to the local endpoint is refused."""
    gateway = "http://gateway.lab.example:4000/v1"
    api, _registry, provider = build(
        config=config(local_endpoint_url=gateway),
        resolver=lambda host: (
            ["10.20.0.5/32"] if host == "gateway.lab.example" else fake_resolver(host)
        ),
    )
    local_launch = spec(endpoint="local", endpoint_url=gateway)
    subscription_launch = spec(attempt_id="01ATTEMPT0000000000000000B")
    with pytest.raises(LaunchRefusedError, match="no rule can permit it"):
        await provider.prepare(local_launch)
    subscription_workspace = await provider.prepare(subscription_launch)
    probe = await provider.ensure_ready()
    assert probe.passed is True
    assert probe.dns_resolves is True
    assert probe.local_endpoint_reachable is False
    assert "local endpoint check failed: no rule can permit it" in probe.detail
    assert "10.20.0.5" in (probe.local_endpoint_detail or "")
    # The worker rules canary ran, under DNS alone: the endpoint is in no policy of it.
    # An unsettled endpoint answer is asked again on every gate, so there is one per run.
    for policy in canary_policies(api):
        [rule] = policy["spec"]["egress"]
        assert {p["port"] for p in rule["ports"]} == {53}
    assert canary_policies(api)
    await provider.launch(subscription_workspace, subscription_launch)


async def test_worker_rules_that_cannot_be_written_refuse_every_launch() -> None:
    """A worker egress plan that fails for a reason other than the local endpoint (here
    a DNS selector naming the workers namespace, which a worker may never reach) still
    fails the whole probe (crucible#110): no worker could run under those rules."""
    api, _registry, provider = build(config=config(egress=IN_CLUSTER, local_endpoint_url=LITELLM))
    local_launch = spec(endpoint="local", endpoint_url=LITELLM)
    subscription_launch = spec(attempt_id="01ATTEMPT0000000000000000B")
    local_workspace = await provider.prepare(local_launch)
    subscription_workspace = await provider.prepare(subscription_launch)
    proved = len(canary_policies(api))
    # The admin setting refuses this selector, so it is put in force directly: it stands
    # for any worker rules the canary's policy cannot be built from.
    provider.config = replace(
        provider.config, egress=replace(IN_CLUSTER, dns_namespace=provider.config.namespace)
    )
    provider.probe = None
    probe = await provider.ensure_ready()
    assert probe.passed is False
    assert "the worker egress rules cannot be written" in probe.detail
    with pytest.raises(LaunchRefusedError, match="the workers namespace is not ready"):
        await provider.launch(local_workspace, local_launch)
    with pytest.raises(LaunchRefusedError, match="the workers namespace is not ready"):
        await provider.launch(subscription_workspace, subscription_launch)
    # No canary policy could be written under the new rules.
    assert len(canary_policies(api)) == proved


def test_an_old_canary_output_without_the_new_answers_does_not_pass() -> None:
    probe = _read_probe(
        "crucible-canary.api=unreachable\ncrucible-canary.pids=4096\ncrucible-canary.done=1\n"
    )
    assert probe.passed is False
    assert probe.checked is False
    assert "DNS check inconclusive" in probe.detail


# ----- runtime settings (the `kubernetes.egress` admin setting) -----------


async def test_a_settings_change_forgets_the_probe() -> None:
    _api, provider, probe = await ready()
    assert probe.passed
    provider.apply_settings(IN_CLUSTER.as_document(), LITELLM)
    assert provider.probe is None
    assert provider.config.egress == IN_CLUSTER
    assert provider.config.local_endpoint_url == LITELLM
    # The same settings again keep a passed probe: nothing it proved has changed.
    probe = await provider.ensure_ready()
    provider.apply_settings(IN_CLUSTER.as_document(), LITELLM)
    assert provider.probe is probe


async def test_no_document_means_the_settings_files_values() -> None:
    _api, _registry, provider = build(config=config(egress=IN_CLUSTER))
    provider.apply_settings(ClusterEgress().as_document(), None)
    assert provider.config.egress == ClusterEgress()
    provider.apply_settings(None, None)
    assert provider.config.egress == IN_CLUSTER


async def test_a_refused_document_keeps_what_is_in_force() -> None:
    _api, _registry, provider = build(config=config(egress=IN_CLUSTER))
    bad = {"local_endpoint": {"namespace": "hades-workers", "pod_labels": {"a": "b"}}}
    provider.apply_settings(bad, None)
    assert provider.config.egress == IN_CLUSTER


async def test_the_provider_reads_its_settings_back_from_the_source() -> None:
    reads: list[int] = []

    def source() -> tuple[dict[str, Any] | None, str | None]:
        reads.append(1)
        return IN_CLUSTER.as_document(), LITELLM

    api = FakeKubernetesApi()
    registry = FakeRegistry(api)
    registry.register(IMAGE, harness="script-harness", version="1.0.0")
    provider = KubernetesProvider(
        config(settings_refresh_seconds=3600),
        api,  # type: ignore[arg-type]
        registry,
        resolver=fake_resolver,
        settings_source=source,
    )
    await provider.prepare(spec())
    probe = await provider.ensure_ready()
    assert probe.local_endpoint_reachable is True
    await provider.ensure_ready()
    assert len(reads) == 1
    provider.reload_settings()
    await provider.ensure_ready()
    assert len(reads) == 2


async def test_prepare_refreshes_settings_before_rendering_its_policy() -> None:
    """crucible#102 (Codex): the supervisor calls `prepare()` before `launch()`, so a
    stale file seed must not survive into the preparer's own NetworkPolicy. Only
    `launch()`, `health()` and `ensure_ready()` read the runtime settings back before
    this fix; a cluster whose stored DNS selector differs from the seed had every
    attempt fail in `prepare()`, before the fix loaded from `launch()` was ever
    reached."""
    stored = ClusterEgress(dns_namespace="cilium-dns-relay", dns_pod_labels=(("app", "dns-relay"),))

    def source() -> tuple[dict[str, Any] | None, str | None]:
        return stored.as_document(), None

    api = FakeKubernetesApi()
    registry = FakeRegistry(api)
    registry.register(IMAGE, harness="script-harness", version="1.0.0")
    provider = KubernetesProvider(
        config(settings_refresh_seconds=3600),
        api,  # type: ignore[arg-type]
        registry,
        resolver=fake_resolver,
        settings_source=source,
    )
    await provider.prepare(spec())
    [preparer] = [
        row["body"]
        for row in api.created
        if row["kind"] == "networkpolicies"
        and row["body"]["spec"]["podSelector"]["matchLabels"][k8sspec.LABEL_ROLE]
        == k8sspec.ROLE_PREPARER
    ]
    peers = [
        d for rule in preparer["spec"]["egress"] for d in rule.get("to") or [] if "ipBlock" not in d
    ]
    assert {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "cilium-dns-relay"}},
        "podSelector": {"matchLabels": {"app": "dns-relay"}},
    } in peers


async def test_the_retention_sweep_never_takes_a_canary_mid_run() -> None:
    """The supervisor's retention sweep deletes whatever carries an attempt id Crucible
    does not track. A canary is never an attempt, so it carries its own label and the
    sweep's listing does not see it or its policy (found on `make deploy-kind`, where
    the supervisor's sweep deleted the API process's canary and the probe failed)."""
    api, _registry, provider = build()
    await resolved(provider)
    real_log = api.pod_log
    swept: list[str] = []
    in_flight: list[str] = []

    def pod_log(name: str, **kwargs: Any) -> Any:
        # The canary has finished and is about to be read and removed: what the sweep
        # would list right now, for every kind it sweeps.
        for kind in ("pods", "networkpolicies"):
            in_flight.extend(
                str(row["metadata"]["name"])
                for row in api.list_objects(kind)
                if "canary" in str(row["metadata"]["name"])
            )
            swept.extend(
                str(row["metadata"]["name"])
                for row in api.list_objects(kind, label_selector=k8sspec.LABEL_ATTEMPT)
            )
        return real_log(name, **kwargs)

    api.pod_log = pod_log  # type: ignore[method-assign]
    assert (await provider.ensure_ready()).passed
    # The namespace canary alone, then the worker-rules canary and its policy.
    assert len(in_flight) == 1 + 2
    assert not [name for name in swept if "canary" in name]


async def test_on_a_translating_cni_the_address_rule_alone_fails_the_dns_check() -> None:
    """Issue 91 in the fake: with the DNS selector turned off, only the ClusterIP rule
    is left, which a translating CNI never matches; with it on, DNS works."""
    _api, _provider, probe = await ready(
        config=config(egress=ClusterEgress(dns_namespace="", dns_pod_labels=())),
        translates_services=True,
    )
    assert probe.passed is False and probe.dns_resolves is False
    _api, _provider, probe = await ready(config=config(), translates_services=True)
    assert probe.passed and probe.dns_resolves is True


async def test_a_probe_proved_under_rules_that_changed_while_it_ran_is_not_kept() -> None:
    """A refresh during a canary run: the answer is about rules no longer in force, so
    the canary runs again under the new ones before anything is stored."""
    api, _registry, provider = build()
    await resolved(provider)
    real_log = api.pod_log
    runs: list[ClusterEgress] = []

    def pod_log(name: str, **kwargs: Any) -> Any:
        runs.append(provider.config.egress)
        if len(runs) == 1:
            provider.apply_settings(IN_CLUSTER.as_document(), None)
        return real_log(name, **kwargs)

    api.pod_log = pod_log  # type: ignore[method-assign]
    probe = await provider.ensure_ready()
    assert probe.passed
    # Two canary Pods per run: the first run was discarded, the second kept.
    assert len(runs) == 4 and runs[2:] == [IN_CLUSTER, IN_CLUSTER]
    assert provider.probe is probe


async def test_a_launch_after_a_settings_change_is_gated_again() -> None:
    """The gate and the worker's rules are the same settings: a change between them
    runs the canary again, and a failure refuses the launch."""
    api, _registry, provider = build(config=config(egress=IN_CLUSTER, local_endpoint_url=LITELLM))
    launch = spec(endpoint="local", endpoint_url=LITELLM)
    workspace = await provider.prepare(launch)
    probe = await provider.ensure_ready()
    assert probe.passed and probe.local_endpoint_reachable is True
    real_resolve = provider._resolve_image
    changed_egress = replace(IN_CLUSTER, dns_namespace="kube-system-alt")

    async def resolve_and_change(launch_spec: Any, *, use_backoff: bool = False) -> str:
        # The endpoint URL is unchanged; what changes is the cluster's own egress
        # settings (a different DNS selector), which forces the re-gate.
        provider.apply_settings(changed_egress.as_document(), LITELLM)
        api.canary_endpoint = "unreachable"
        return await real_resolve(launch_spec, use_backoff=use_backoff)

    provider._resolve_image = resolve_and_change  # type: ignore[method-assign,assignment]
    with pytest.raises(LaunchRefusedError, match="local endpoint check failed"):
        await provider.launch(workspace, launch)


async def test_a_refused_document_still_follows_the_endpoint_url() -> None:
    _api, _registry, provider = build(config=config(egress=IN_CLUSTER))
    bad = {"local_endpoint": {"namespace": "hades-workers", "pod_labels": {"a": "b"}}}
    provider.apply_settings(bad, LITELLM)
    assert provider.config.egress == IN_CLUSTER
    assert provider.config.local_endpoint_url == LITELLM


async def test_a_namespace_without_its_default_deny_fails_even_with_worker_rules() -> None:
    """The canary with a policy of its own would be isolated by it; the namespace-scope
    canary has none, so a namespace that lost its default deny still fails the probe
    (readiness row 12, and the e2e-kind case that deletes the default deny)."""
    api, _registry, provider = build()
    await resolved(provider)
    real_log = api.pod_log

    def pod_log(name: str, **kwargs: Any) -> Any:
        # The API server answers whoever has no policy of their own.
        if "-ns-" in name:
            api.logs[name] = [
                line.replace("api=unreachable", "api=reachable") for line in api.logs[name]
            ]
        return real_log(name, **kwargs)

    api.pod_log = pod_log  # type: ignore[method-assign]
    probe = await provider.ensure_ready()
    assert probe.passed is False and probe.egress_enforced is False
    assert "reached the API server" in probe.detail
    namespace_canary = next(
        row["body"]
        for row in api.created
        if row["kind"] == "pods" and row["name"].startswith("crucible-canary-ns-")
    )
    selected = [
        row
        for row in api.created
        if row["kind"] == "networkpolicies"
        and row["body"]["spec"]["podSelector"]["matchLabels"].get(k8sspec.LABEL_CANARY)
        == namespace_canary["metadata"]["labels"][k8sspec.LABEL_CANARY]
    ]
    assert selected == []


def test_the_worker_rules_canary_must_also_find_the_api_server_unreachable() -> None:
    namespace = (
        "crucible-canary.api=unreachable\ncrucible-canary.pids=4096\ncrucible-canary.done=1\n"
    )
    rules = (
        "crucible-canary.api=reachable\ncrucible-canary.dns=resolved\n"
        "crucible-canary.endpoint=none\ncrucible-canary.done=1\n"
    )
    probe = _read_probe(namespace, rules)
    assert probe.passed is False and probe.egress_enforced is False
    assert "under the worker egress rules" in probe.detail


async def test_both_canaries_take_the_configured_canary_size() -> None:
    """Two canaries at a worker's size (2 CPU, 4 GiB each) filled the kind tier's 8 GiB
    quota until the quota controller caught up, and the worker Pod after them waited.
    Both take `canary_limits()` from settings (93), the defaults here."""
    api, _provider, probe = await ready()
    assert probe.passed
    canaries = [
        row["body"]
        for row in api.created
        if row["kind"] == "pods" and row["name"].startswith("crucible-canary-")
    ]
    assert len(canaries) == 2
    for pod in canaries:
        resources = pod["spec"]["containers"][0]["resources"]
        assert resources["requests"] == {"cpu": "100m", "memory": str(64 * 1024**2)}
        assert resources["limits"]["cpu"] == "100m"


def test_the_failure_detail_quotes_the_worker_rules_canarys_own_curl_exit() -> None:
    namespace = (
        "crucible-canary.api=unreachable\ncrucible-canary.pod_pids=4096\n"
        "crucible-canary.pod_pids_source=cgroup-v2-parent\ncrucible-canary.done=1\n"
    )
    rules = (
        "crucible-canary.api=unreachable\ncrucible-canary.dns=resolved\n"
        "crucible-canary.endpoint=unreachable\ncrucible-canary.endpoint_curl_exit=28\n"
        "crucible-canary.done=1\n"
    )
    probe = _read_probe(namespace, rules)
    # crucible#110: an unreachable local endpoint alone no longer fails the probe.
    assert probe.passed is True
    assert probe.local_endpoint_reachable is False
    assert "curl exit 28" in probe.detail
    assert "curl exit 28" in (probe.local_endpoint_detail or "")
