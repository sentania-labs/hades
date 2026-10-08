"""Regression for hades #205's reconcile-time egress adoption."""

from __future__ import annotations

from typing import Any

from crucible.adapters.execution.kubernetes import KubernetesConfig
from tests.unit.kubernetes_fixtures import ATTEMPT, HOST_ADDRESSES, build, spec


class _Answers:
    def __init__(self) -> None:
        self.by_host = {host: list(addresses) for host, addresses in HOST_ADDRESSES.items()}

    def __call__(self, host: str) -> list[str]:
        return list(self.by_host.get(host, []))


def _clear_egress_state(launched: Any) -> None:
    launched.egress_plan = None
    launched.network_policy = None
    launched.allowed = None


def _https_cidrs(policy: dict[str, object]) -> set[str]:
    egress = policy["spec"]
    assert isinstance(egress, dict)
    rules = egress["egress"]
    assert isinstance(rules, list)
    return {
        peer["ipBlock"]["cidr"]
        for rule in rules
        if rule.get("ports") == [{"protocol": "TCP", "port": 443}]
        for peer in rule["to"]
    }


async def test_an_already_known_job_without_egress_state_adopts_and_refreshes() -> None:
    answers = _Answers()
    config = KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        resolve_ttl_seconds=0,
    )
    api, _registry, provider = build(config=config, resolver=answers)
    api.script(ATTEMPT, "succeed", after=100)
    launch = spec()
    handle = await provider.launch(await provider.prepare(launch), launch)

    launched = provider._launched[ATTEMPT]
    _clear_egress_state(launched)

    [reconciled] = await provider.reconcile()
    assert reconciled.attempt_id == ATTEMPT
    assert launched.egress_plan is not None
    policy_name = launched.network_policy
    assert policy_name == f"np-worker-{ATTEMPT.lower()}"

    answers.by_host["github.com"] = ["140.82.114.4/32"]
    await provider.observe(handle)
    policy = api.get("networkpolicies", policy_name)
    assert _https_cidrs(policy) == {
        "140.82.121.4/32",
        "140.82.114.4/32",
        "151.101.0.223/32",
    }
