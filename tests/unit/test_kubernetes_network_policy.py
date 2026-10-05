"""The per-attempt NetworkPolicy (26, requirement 3 of C8a).

The allowlist source is the one `make proxy-config` uses: the policy's
`egress_allowlist`, the contract's `egress_extra`, the adapter's declared endpoints
(S6), and a local route's exact `endpoint_url` host and port (05b, S16).

A `networking.k8s.io/v1` policy has no deny verb, so 26's explicit denials are the
`except` of the one broad allow, and a role with no egress gets no policy at all: the
namespace's default deny is already the answer for it.
"""

from __future__ import annotations

import ipaddress
from dataclasses import replace
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sspec import EgressPlan, SpecError
from crucible.adapters.execution.kubernetes import KubernetesConfig
from crucible.domain.cluster_egress import ClusterEgress
from crucible.ports.execution import CleanupPolicy, ProviderError
from tests.unit.kubernetes_fixtures import build, fake_resolver, spec

DENIED_BY_26 = (
    # the cluster's API server and every other service address
    "10.96.0.1",
    # the node network and other namespaces' pod network
    "10.244.3.7",
    "172.17.0.5",
    # link-local, which is where a cloud metadata service lives
    "169.254.169.254",
    # the lab's own private ranges
    "192.168.40.10",
    "10.10.0.1",
)


def rules(policy: dict[str, Any]) -> list[dict[str, Any]]:
    egress: list[dict[str, Any]] = policy["spec"]["egress"]
    return egress


def allows(policy: dict[str, Any], address: str, port: int, protocol: str = "TCP") -> bool:
    """Whether this policy permits one packet. `except` is what makes a denial."""
    wanted = ipaddress.ip_address(address)
    for rule in rules(policy):
        ports = rule.get("ports") or []
        if ports and not any(
            int(p["port"]) == port and str(p.get("protocol", "TCP")) == protocol for p in ports
        ):
            continue
        for destination in rule.get("to") or []:
            block = destination.get("ipBlock")
            if not block:
                continue
            if wanted not in ipaddress.ip_network(block["cidr"]):
                continue
            if any(wanted in ipaddress.ip_network(e) for e in block.get("except") or []):
                continue
            return True
    return False


async def policies(**kwargs: Any) -> dict[str, dict[str, Any]]:
    """Every NetworkPolicy one whole attempt creates, by the role it selects. The
    readiness canary's own policy is not the attempt's and is tested on its own."""
    api, _registry, provider = build(config=kwargs.pop("config", None))
    launch = spec(**kwargs)
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    while (await provider.observe(handle)).state.value == "running":
        pass
    await provider.collect(handle, workspace, launch)
    await provider.cleanup(workspace, CleanupPolicy.DELETE, launch)
    return {
        row["body"]["spec"]["podSelector"]["matchLabels"][k8sspec.LABEL_ROLE]: row["body"]
        for row in api.created
        if row["kind"] == "networkpolicies"
        and row["body"]["spec"]["podSelector"]["matchLabels"][k8sspec.LABEL_ROLE]
        != k8sspec.ROLE_CANARY
    }


@pytest.fixture
async def rendered() -> dict[str, dict[str, Any]]:
    return await policies()


async def test_the_selector_is_this_attempts_pods_of_this_role(
    rendered: dict[str, dict[str, Any]],
) -> None:
    for role, policy in rendered.items():
        assert policy["spec"]["podSelector"]["matchLabels"] == {
            k8sspec.LABEL_ATTEMPT: "01ATTEMPT0000000000000000A",
            k8sspec.LABEL_ROLE: role,
        }
        # No ingress section at all: nothing ever connects to a worker.
        assert policy["spec"]["policyTypes"] == ["Egress"]


async def test_the_collector_and_the_bundle_verifier_get_no_policy_at_all(
    rendered: dict[str, dict[str, Any]],
) -> None:
    """26: no egress for the collector or the bundle verifier. A namespace with a
    default deny needs no object to express that, and an empty policy would be one
    more thing that could be written wrongly."""
    assert k8sspec.ROLE_COLLECTOR not in rendered
    assert k8sspec.ROLE_BUNDLE not in rendered
    assert k8sspec.ROLE_READER not in rendered
    assert k8sspec.ROLE_CLEANER not in rendered


async def test_the_worker_reaches_what_the_allowlist_names_and_nothing_else(
    rendered: dict[str, dict[str, Any]],
) -> None:
    worker = rendered[k8sspec.ROLE_WORKER]
    hosts = worker["metadata"]["annotations"][k8sspec.ANNOTATION_EGRESS].split(",")
    # The union of the policy's allowlist and the adapter's declared endpoints (13, S6).
    assert "pypi.org" in hosts
    assert allows(worker, "151.101.0.223", 443)
    # Nothing the allowlist did not name, on any port.
    assert not allows(worker, "203.0.113.9", 443)
    assert not allows(worker, "151.101.0.223", 22)


async def test_the_preparer_reaches_github_only(rendered: dict[str, dict[str, Any]]) -> None:
    """26: the preparer and the publisher do the git traffic, and nothing else."""
    preparer = rendered[k8sspec.ROLE_PREPARER]
    assert preparer["metadata"]["annotations"][k8sspec.ANNOTATION_EGRESS] == (
        "api.github.com,github.com"
    )
    assert allows(preparer, "140.82.121.4", 443)
    assert not allows(preparer, "151.101.0.223", 443)


async def test_the_provider_never_adds_github_to_a_worker() -> None:
    """26: GitHub is not reachable from a worker; the preparer and the publisher do the
    git traffic. The provider adds nothing of its own to a worker's destinations, so a
    policy document that does not name GitHub produces a worker that cannot reach it.

    The seeded `default-software` policy does name `github.com` in its
    `egress_allowlist` (05b, "read-only in effect: workers hold no GitHub credential"),
    which is an operator decision in a policy document rather than something this
    provider can or should override. What the provider owes is that the rendered rule
    is exactly the allowlist and nothing wider."""
    rendered = await policies(
        policy={
            "images": {"allowlist": ["crucible-worker:*"]},
            "network": {"mode": "egress-proxy", "egress_allowlist": ["pypi.org"]},
            "resources": {"cpus": 2, "memory": "4GiB"},
            "limits": {"grace_seconds": 30},
        }
    )
    worker = rendered[k8sspec.ROLE_WORKER]
    assert not allows(worker, "140.82.121.4", 443)
    assert not allows(worker, "140.82.121.6", 443)
    assert allows(worker, "151.101.0.223", 443)


async def test_a_host_that_does_not_resolve_refuses_the_launch() -> None:
    """13's rule for the same situation: an attempt whose allowlist the egress path
    cannot actually permit is refused, never run with less network than promised."""
    _api, _registry, provider = build(resolver=lambda host: [])
    launch = spec()
    with pytest.raises(ProviderError, match="do not resolve"):
        await provider.prepare(launch)


@pytest.mark.parametrize("address", DENIED_BY_26)
async def test_every_denial_26_names_is_denied_for_every_role(
    rendered: dict[str, dict[str, Any]], address: str
) -> None:
    for role, policy in rendered.items():
        assert not allows(policy, address, 443), f"{role} reached {address}"
        assert not allows(policy, address, 80), f"{role} reached {address}"


async def test_cluster_dns_is_port_53_on_the_dns_address_and_nothing_else(
    rendered: dict[str, dict[str, Any]],
) -> None:
    for policy in rendered.values():
        assert allows(policy, "10.96.0.10", 53, "UDP")
        assert allows(policy, "10.96.0.10", 53, "TCP")
        # 26: nothing else on that address.
        assert not allows(policy, "10.96.0.10", 443)
        assert not allows(policy, "10.96.0.10", 8080)


async def test_ipv6_is_denied_entirely(rendered: dict[str, dict[str, Any]]) -> None:
    """Every address rule is an IPv4 ipBlock, so a v6 destination matches nothing. The
    only other peer is a pod selector, which names pods and never an address."""
    for policy in rendered.values():
        for rule in rules(policy):
            for destination in rule.get("to") or []:
                if "ipBlock" not in destination:
                    assert set(destination) == {"namespaceSelector", "podSelector"}
                    continue
                assert ipaddress.ip_network(destination["ipBlock"]["cidr"]).version == 4


async def test_a_hostname_local_route_resolves_to_exact_addresses_and_port() -> None:
    """C10: a configured gateway name may resolve into a private range, but the
    generated exception is still only the resolved address and configured port."""

    def resolver(host: str) -> list[str]:
        return ["10.10.0.42/32"] if host == "llm.apps.int.sentania.net" else ["151.101.0.223/32"]

    api, _registry, provider = build(
        resolver=resolver,
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            local_endpoint_cidrs=("10.10.0.0/24",),
        ),
    )
    launch = spec(endpoint="local", endpoint_url="https://llm.apps.int.sentania.net:8443/v1")
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    rendered = {
        row["body"]["spec"]["podSelector"]["matchLabels"][k8sspec.LABEL_ROLE]: row["body"]
        for row in api.created
        if row["kind"] == "networkpolicies"
    }
    worker = rendered[k8sspec.ROLE_WORKER]
    assert allows(worker, "10.10.0.42", 8443)
    assert not allows(worker, "10.10.0.42", 443)
    assert not allows(worker, "10.10.0.43", 8443)
    await provider.cleanup(workspace, CleanupPolicy.DELETE, launch)
    assert handle


async def test_a_direct_private_local_address_is_refused() -> None:
    """C10 keeps names as the trust anchor and refuses a raw cluster or lab address."""
    _api, _registry, provider = build()
    launch = spec(endpoint="local", endpoint_url="http://10.10.0.42:8000/v1")
    workspace = await provider.prepare(launch)
    with pytest.raises((ProviderError, SpecError), match="names or resolves to an address"):
        await provider.launch(workspace, launch)


@pytest.mark.parametrize(
    ("host", "address"),
    [
        ("kubernetes.default.svc", "10.96.0.1/32"),
        ("service.other-namespace.svc", "10.244.3.7/32"),
    ],
)
async def test_a_named_local_route_into_cluster_ranges_is_refused(host: str, address: str) -> None:
    def resolver(candidate: str) -> list[str]:
        return [address] if candidate == host else ["151.101.0.223/32"]

    _api, _registry, provider = build(
        resolver=resolver,
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            local_endpoint_cidrs=("172.16.6.20/32",),
        ),
    )
    launch = spec(endpoint="local", endpoint_url=f"https://{host}:443/v1")
    workspace = await provider.prepare(launch)
    with pytest.raises(ProviderError, match="resolves to an address"):
        await provider.launch(workspace, launch)


async def test_network_none_creates_no_policy_and_therefore_no_egress() -> None:
    rendered = await policies(network="none")
    assert k8sspec.ROLE_WORKER not in rendered


async def test_the_login_role_gets_the_harness_login_endpoints() -> None:
    _api, _registry, provider = build()
    plan = provider._egress_plan(spec(harness="codex"), k8sspec.ROLE_LOGIN)
    assert "auth.openai.com" in plan.hosts


async def test_the_verifier_gets_the_policys_registries_and_not_the_model_endpoints() -> None:
    """The policy's allowlist as written (hades #425), which is what the Docker
    verifier reaches through the proxy too; the harness endpoints are not part of it."""
    _api, _registry, provider = build()
    plan = provider._egress_plan(spec(harness="codex"), k8sspec.ROLE_VERIFIER)
    assert set(plan.hosts) == {"pypi.org", "github.com"}
    assert "api.openai.com" not in plan.hosts


# ----- the denials are real for a resolved allowlist -----------------------


async def test_a_host_that_resolves_into_a_denied_range_refuses_the_launch() -> None:
    """26: the denials are the `except` of every allow, so a name that resolves into a
    denied range cannot open one.

    An allowed destination is a `/32`, so asking whether a denied `/8` sits inside it is
    always false; the check that matters is the other direction. Without it, a vendor
    host whose record points at the API server's ClusterIP, at cloud metadata, or into
    the lab's own ranges becomes an allow rule for exactly that address."""
    for address in ("169.254.169.254/32", "10.43.0.1/32", "192.168.40.10/32"):
        _api, _registry, provider = build(resolver=lambda _host, a=address: [a])
        launch = spec()
        with pytest.raises(ProviderError, match="denies"):
            await provider.prepare(launch)


async def test_the_rendered_rules_never_name_a_denied_address() -> None:
    """The property the parametrised denial test above states, asserted against the
    rendered object rather than against a fixture that could not produce one."""
    rendered = await policies()
    for policy in rendered.values():
        for rule in rules(policy):
            for destination in rule.get("to") or []:
                if "ipBlock" not in destination:
                    continue
                block = destination["ipBlock"]
                if block["cidr"] == "0.0.0.0/0":
                    continue
                network = ipaddress.ip_network(block["cidr"])
                for denied in k8sspec.DEFAULT_DENIED_CIDRS:
                    if k8sspec.denied_by(block["cidr"], [denied]) is not None:
                        # Cluster DNS is the one address 26 allows inside a denied
                        # range, on port 53 and nothing else.
                        assert network.version == 4
                        assert block["cidr"] == "10.96.0.10/32", block
                        assert rule["ports"] == [
                            {"protocol": "UDP", "port": 53},
                            {"protocol": "TCP", "port": 53},
                        ]


async def test_a_worker_reaches_the_git_remote_when_the_policy_names_it() -> None:
    """hades #425: the worker's egress is the policy's `egress_allowlist` as written (05b:
    "hostnames the egress proxy permits for workers"). Until #425 the provider subtracted
    github.com from the worker, so a policy that allowlisted it produced a worker whose
    curl to github.com timed out while the task page said it was permitted. The worker
    still holds no GitHub credential, so what it gets is read-only in effect."""
    rendered = await policies(
        policy={
            "images": {"allowlist": ["crucible-worker:*"]},
            "network": {
                "mode": "egress-proxy",
                "egress_allowlist": ["github.com", "api.github.com", "pypi.org"],
            },
            "resources": {"cpus": 2, "memory": "4GiB"},
            "limits": {"grace_seconds": 30},
        }
    )
    worker = rendered[k8sspec.ROLE_WORKER]
    assert allows(worker, "140.82.121.4", 443)
    assert allows(worker, "140.82.121.6", 443)
    assert allows(worker, "151.101.0.223", 443)
    # What was granted is what the annotation records: the list as written.
    hosts = worker["metadata"]["annotations"][k8sspec.ANNOTATION_EGRESS].split(",")
    assert {"github.com", "api.github.com", "pypi.org"} <= set(hosts)
    # The preparer still does the git traffic, whether or not the policy names GitHub.
    assert allows(rendered[k8sspec.ROLE_PREPARER], "140.82.121.4", 443)


async def test_the_verifier_gets_the_git_remote_when_the_policy_names_it() -> None:
    _api, _registry, provider = build()
    plan = provider._egress_plan(
        spec(
            policy={
                "images": {"allowlist": ["crucible-worker:*"]},
                "network": {
                    "mode": "egress-proxy",
                    "egress_allowlist": ["github.com", "pypi.org"],
                },
                "resources": {"cpus": 2, "memory": "4GiB"},
                "limits": {"grace_seconds": 30},
            }
        ),
        k8sspec.ROLE_VERIFIER,
    )
    assert set(plan.hosts) == {"github.com", "pypi.org"}


# ----- selectors that survive a translating CNI (crucible#91) ---------------

IN_CLUSTER = ClusterEgress(
    endpoint_namespace="litellm",
    endpoint_pod_labels=(("app.kubernetes.io/name", "litellm"),),
    endpoint_port=4000,
)
LITELLM_URL = "http://litellm.litellm.svc.cluster.local:80/v1"


def selector_peers(policy: dict[str, Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Every (selector peer, ports) pair a policy carries."""
    return [
        (destination, rule.get("ports") or [])
        for rule in rules(policy)
        for destination in rule.get("to") or []
        if "ipBlock" not in destination
    ]


def in_cluster_config(**overrides: Any) -> KubernetesConfig:
    return KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        **{"egress": IN_CLUSTER, **overrides},
    )


async def test_dns_is_allowed_by_the_resolvers_pods_as_well_as_its_address(
    rendered: dict[str, dict[str, Any]],
) -> None:
    """A CNI that translates the kube-dns ClusterIP to its pods before it evaluates
    policy (Cilium with kube-proxy replacement) never matches the /32; it matches the
    selector. Both sit in one rule, so both get port 53 and nothing else."""
    for policy in rendered.values():
        dns_rules = [
            rule
            for rule in rules(policy)
            if any("podSelector" in d for d in rule["to"]) and rule["ports"][0]["port"] == 53
        ]
        assert len(dns_rules) == 1
        rule = dns_rules[0]
        assert rule["ports"] == [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]
        assert {"ipBlock": {"cidr": "10.96.0.10/32"}} in rule["to"]
        assert {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        } in rule["to"]


async def test_an_in_cluster_local_endpoint_is_its_pods_on_the_backend_port() -> None:
    """The Service's port (80 here) is not what a translating CNI sees; the pods' port
    (4000) is, so the rule names the backend port and no address at all."""
    rendered = await policies(
        config=in_cluster_config(), endpoint="local", endpoint_url=LITELLM_URL
    )
    worker = rendered[k8sspec.ROLE_WORKER]
    endpoint_rules = [
        (peer, ports)
        for peer, ports in selector_peers(worker)
        if peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "litellm"
    ]
    assert endpoint_rules == [
        (
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "litellm"}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "litellm"}},
            },
            [{"protocol": "TCP", "port": 4000}],
        )
    ]
    # The service name was never resolved into an address rule.
    assert not allows(worker, "10.43.12.7", 80)
    assert not allows(worker, "10.43.12.7", 4000)
    # Only the worker gets the model endpoint.
    for role, policy in rendered.items():
        if role != k8sspec.ROLE_WORKER:
            assert all(
                p["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] != "litellm"
                for p, _ in selector_peers(policy)
            )


async def test_port_zero_means_the_endpoint_urls_own_port() -> None:
    config = in_cluster_config(egress=replace(IN_CLUSTER, endpoint_port=0))
    rendered = await policies(
        config=config, endpoint="local", endpoint_url="http://litellm.litellm.svc:4000/v1"
    )
    ports = [
        ports
        for peer, ports in selector_peers(rendered[k8sspec.ROLE_WORKER])
        if peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "litellm"
    ]
    assert ports == [[{"protocol": "TCP", "port": 4000}]]


async def test_an_out_of_cluster_endpoint_keeps_its_address_rule() -> None:
    """With no endpoint namespace the local route is the resolved /32, as before."""

    def resolver(host: str) -> list[str]:
        return ["10.10.0.42/32"] if host == "llm.apps.int.sentania.net" else fake_resolver(host)

    api, _registry, provider = build(
        resolver=resolver,
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            local_endpoint_cidrs=("10.10.0.0/24",),
        ),
    )
    launch = spec(endpoint="local", endpoint_url="https://llm.apps.int.sentania.net:8443/v1")
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    worker = next(
        row["body"]
        for row in api.created
        if row["kind"] == "networkpolicies" and row["name"].startswith("np-worker")
    )
    assert allows(worker, "10.10.0.42", 8443)
    assert all(
        p["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "kube-system"
        for p, _ in selector_peers(worker)
    )


@pytest.mark.parametrize("address", DENIED_BY_26)
async def test_a_selector_rule_never_opens_a_denied_address(address: str) -> None:
    """26's denials hold with every selector configured: a selector names pods, adds no
    address, and carries only its own port, so every denied address stays denied on
    every port for every role."""
    rendered = await policies(
        config=in_cluster_config(), endpoint="local", endpoint_url=LITELLM_URL
    )
    for role, policy in rendered.items():
        for port in (53, 80, 443, 4000, 6443, 8080):
            for protocol in ("TCP", "UDP"):
                if address == "10.96.0.10" and port == 53:
                    continue
                assert not allows(policy, address, port, protocol), f"{role} {address}:{port}"
        for peer, ports in selector_peers(policy):
            # One namespace by name, and a non-empty pod selector: never a whole
            # namespace, never every namespace.
            assert set(peer) == {"namespaceSelector", "podSelector"}
            assert list(peer["namespaceSelector"]["matchLabels"]) == ["kubernetes.io/metadata.name"]
            assert peer["podSelector"]["matchLabels"]
            assert ports and all(p["port"] in (53, 4000) for p in ports)


@pytest.mark.parametrize("namespace", ["crucible-workers", "crucible"])
async def test_a_selector_into_the_workers_or_crucibles_namespace_is_refused(
    namespace: str,
) -> None:
    """A selector into the workers namespace is a worker reaching another attempt; into
    Crucible's own, a worker reaching its database. The provider refuses to render it
    even if a document got past the admin check."""
    config = in_cluster_config(egress=replace(IN_CLUSTER, endpoint_namespace=namespace))
    _api, _registry, provider = build(config=config)
    launch = spec(endpoint="local", endpoint_url=LITELLM_URL)
    with pytest.raises(SpecError, match="may never reach"):
        provider._policy_body(
            "np",
            {},
            launch.attempt_id,
            k8sspec.ROLE_WORKER,
            provider._egress_plan(launch, k8sspec.ROLE_WORKER),
        )
    config = KubernetesConfig(egress=replace(ClusterEgress(), dns_namespace=namespace))
    _api, _registry, provider = build(config=config)
    with pytest.raises(SpecError, match="may never reach"):
        provider._policy_body("np", {}, launch.attempt_id, k8sspec.ROLE_WORKER, EgressPlan())


def test_an_empty_pod_selector_is_refused_at_render_time() -> None:
    with pytest.raises(SpecError, match="no pod labels"):
        k8sspec.egress_policy(
            name="np",
            namespace="crucible-workers",
            object_labels={},
            attempt_id="A",
            role=k8sspec.ROLE_WORKER,
            plan=EgressPlan(),
            dns_server="",
            dns_selector=k8sspec.PeerSelector("kube-system", ()),
        )


def test_an_empty_dns_namespace_leaves_the_address_rule_alone() -> None:
    body = k8sspec.egress_policy(
        name="np",
        namespace="crucible-workers",
        object_labels={},
        attempt_id="A",
        role=k8sspec.ROLE_WORKER,
        plan=EgressPlan(),
        dns_server="10.96.0.10",
    )
    assert body["spec"]["egress"] == [
        {
            "to": [{"ipBlock": {"cidr": "10.96.0.10/32"}}],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        }
    ]


# ----- hades #191: the git roles reach the git host --------------------------------

GIT_ROLES = frozenset({k8sspec.ROLE_CACHE_REFRESHER, k8sspec.ROLE_PREPARER})


def _selects(policy: dict[str, Any], labels: dict[str, str]) -> bool:
    wanted = policy["spec"]["podSelector"].get("matchLabels", {})
    return all(labels.get(key) == value for key, value in wanted.items())


def _allows(policy: dict[str, Any], cidr: str, port: int) -> bool:
    for rule in policy["spec"]["egress"]:
        ports = {p["port"] for p in rule.get("ports", []) if p.get("protocol") == "TCP"}
        if port not in ports:
            continue
        for peer in rule.get("to", []):
            block = peer.get("ipBlock")
            if block and ipaddress.ip_address(cidr.partition("/")[0]) in ipaddress.ip_network(
                block["cidr"]
            ):
                return True
    return False


@pytest.mark.parametrize("private", [False, True])
async def test_every_git_role_pod_is_selected_by_a_policy_that_allows_the_git_host(
    private: bool,
) -> None:
    """hades #191: the refresher timed out on github.com while the preparer reached it.
    Every Pod of a git role is selected by a policy that permits the git host's
    addresses on 443, and the Pod resolves the host to exactly those addresses."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from crucible.ports.github import InstallationToken  # noqa: PLC0415

    api, _registry, provider = build(
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            storage_class="lab-ssd",
            cache_claim="crucible-reference-cache",
        )
    )
    token = (
        InstallationToken(
            "ghs_" + "Q" * 36,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            repository="acme/example",
            permissions={"contents": "read"},
        )
        if private
        else None
    )
    await provider.prepare(spec(), checkout_token=token)
    policies = [row["body"] for row in api.created if row["kind"] == "networkpolicies"]
    git_addresses = fake_resolver("github.com")
    assert git_addresses
    seen: set[str] = set()
    for row in api.created:
        if row["kind"] != "jobs":
            continue
        template = row["body"]["spec"]["template"]
        labels = template["metadata"]["labels"]
        role = labels[k8sspec.LABEL_ROLE]
        if role not in GIT_ROLES:
            continue
        seen.add(role)
        selecting = [p for p in policies if _selects(p, labels)]
        assert selecting, f"no NetworkPolicy selects the {role} Pod ({labels})"
        for cidr in git_addresses:
            assert any(_allows(p, cidr, 443) for p in selecting), (
                f"no policy selecting the {role} Pod allows {cidr}:443"
            )
        pinned = {
            alias["ip"]
            for alias in template["spec"].get("hostAliases", [])
            if "github.com" in alias["hostnames"]
        }
        assert pinned == {str(ipaddress.ip_network(c).network_address) for c in git_addresses}
    assert seen == GIT_ROLES


async def test_a_pod_resolves_its_hosts_to_the_addresses_its_policy_was_written_with() -> None:
    """hades #191: github.com answers one address with a 60 second TTL, and the provider
    keeps a resolved address for `resolve_ttl_seconds`. The Pod's own lookup could
    return an address the policy never named; `hostAliases` takes that lookup away."""
    answers = {"github.com": ["140.82.112.3/32"], "api.github.com": ["140.82.112.6/32"]}

    def rotating(host: str) -> list[str]:
        return list(answers.get(host, []))

    api, _registry, provider = build(
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            storage_class="lab-ssd",
            cache_claim="crucible-reference-cache",
        ),
        resolver=rotating,
    )
    await provider.prepare(spec())
    answers["github.com"] = ["140.82.114.4/32"]
    # Within the resolve TTL the provider keeps the first answer for the next attempt.
    await provider.prepare(spec(attempt_id="01ATTEMPT0000000000000000B"))
    for row in api.created:
        if row["kind"] != "jobs":
            continue
        pod = row["body"]["spec"]["template"]["spec"]
        aliases = {a["ip"]: a["hostnames"] for a in pod.get("hostAliases", [])}
        assert aliases == {"140.82.112.3": ["github.com"], "140.82.112.6": ["api.github.com"]}


def test_host_aliases_pin_nothing_under_the_broad_rule() -> None:
    plan = EgressPlan(
        hosts=("github.com",),
        broad=True,
        host_addresses=(("github.com", ("140.82.112.3/32",)),),
    )
    assert k8sspec.host_aliases(plan) == []
    assert k8sspec.host_aliases(replace(plan, broad=False)) == [
        {"ip": "140.82.112.3", "hostnames": ["github.com"]}
    ]


def test_host_aliases_carry_only_names_the_api_server_accepts() -> None:
    """A hostAliases hostname must be a lowercase DNS-1123 name, or the Job is refused;
    the policy's allowlist is not held to that, so the alias is normalised or left out."""
    plan = EgressPlan(
        hosts=("Registry.NPMjs.org", "pypi.org.", "bad_name.example"),
        host_addresses=(
            ("Registry.NPMjs.org", ("104.16.1.34/32",)),
            ("pypi.org.", ("151.101.0.223/32",)),
            ("bad_name.example", ("203.0.113.9/32",)),
        ),
    )
    assert k8sspec.host_aliases(plan) == [
        {"ip": "104.16.1.34", "hostnames": ["registry.npmjs.org"]},
        {"ip": "151.101.0.223", "hostnames": ["pypi.org"]},
    ]
