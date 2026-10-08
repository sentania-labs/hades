"""hades #205: a long-running worker's allowlist addresses stay valid.

A worker's NetworkPolicy is written with the addresses its allowlisted names resolved
to at launch, and its Pod's `hostAliases` pin the names to them (hades #191). Neither
the Pod nor a name's answer stands still over the hours a worker runs. The provider
therefore looks the names of a running worker up again on the resolve interval and
patches its policy to follow them, with an overlap window before an address that left
the answer is dropped (26, "A running worker's addresses follow its names").

AC1: a running attempt whose name resolves to a new address gets a policy allowing
the old and the new addresses. AC2: after the overlap window the old address is
removed. The addresses the policy was written with are the exception to AC2 and stay
for the Pod's life: `hostAliases` pin the Pod to them and cannot change, so dropping
one would leave the Pod pinned to an address its policy no longer allows.

The rule is pure (`crucible.domain.cluster_egress.refresh_addresses`) and is tested on
its own first; the provider tests then prove it reaches a live policy through the fake
API server, with a short resolve interval and a short window so real time passes."""

from __future__ import annotations

import time
from typing import Any

import pytest
from pydantic import ValidationError

from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.cli.wiring import kubernetes_config
from crucible.domain.cluster_egress import (
    DEFAULT_ADDRESS_OVERLAP_SECONDS,
    AllowedAddresses,
    refresh_addresses,
)
from crucible.ports.execution import Handle, ObservationState
from crucible.settings import Settings
from tests.unit.kubernetes_fixtures import ATTEMPT, HOST_ADDRESSES, build, spec
from tests.wait import async_wait_until

OLD = "140.82.121.4/32"  # what the fixtures' resolver answers for github.com
NEW = "140.82.114.4/32"
NEWER = "140.82.112.3/32"
PYPI = "151.101.0.223/32"

# ----- the rule --------------------------------------------------------------


def _written(*addresses: str) -> AllowedAddresses:
    return AllowedAddresses(written=addresses, current=(("github.com", addresses),))


def test_ac1_a_new_answer_is_allowed_beside_the_old_one() -> None:
    allowed = refresh_addresses(
        _written(OLD), {"github.com": [NEW]}, now=100.0, overlap_seconds=120
    )
    assert allowed.cidrs == (OLD, NEW)
    assert allowed.current == (("github.com", (NEW,)),)
    # The written address is kept because the Pod is pinned to it, not because it is
    # retiring: nothing about it expires.
    assert allowed.retiring == ()


def test_ac2_an_address_that_left_the_answer_is_dropped_after_the_window() -> None:
    allowed = refresh_addresses(
        _written(OLD), {"github.com": [NEW]}, now=100.0, overlap_seconds=120
    )
    allowed = refresh_addresses(allowed, {"github.com": [NEWER]}, now=400.0, overlap_seconds=120)
    # Inside the window: the written address, the current one and the retiring one.
    assert allowed.cidrs == (OLD, NEWER, NEW)
    assert allowed.retiring == ((NEW, 400.0),)
    # Still inside it one second before the window ends, with no new lookup.
    assert refresh_addresses(allowed, {}, now=519.0, overlap_seconds=120).cidrs == (OLD, NEWER, NEW)
    # At the window's end the old address is gone; the written one is not.
    after = refresh_addresses(allowed, {}, now=520.0, overlap_seconds=120)
    assert after.cidrs == (OLD, NEWER)
    assert after.retiring == ()


def test_an_address_that_comes_back_stops_retiring() -> None:
    allowed = refresh_addresses(
        _written(OLD), {"github.com": [NEW]}, now=100.0, overlap_seconds=120
    )
    allowed = refresh_addresses(allowed, {"github.com": [NEWER]}, now=200.0, overlap_seconds=120)
    allowed = refresh_addresses(allowed, {"github.com": [NEW]}, now=300.0, overlap_seconds=120)
    assert allowed.cidrs == (OLD, NEW, NEWER)
    assert allowed.retiring == ((NEWER, 300.0),)


def test_a_name_that_does_not_answer_keeps_its_addresses() -> None:
    """A resolver that did not answer is no reason to narrow a running attempt."""
    allowed = refresh_addresses(
        _written(OLD), {"github.com": [NEW]}, now=100.0, overlap_seconds=120
    )
    assert refresh_addresses(allowed, {"github.com": []}, now=200.0, overlap_seconds=120) == allowed
    assert refresh_addresses(allowed, {}, now=200.0, overlap_seconds=120) == allowed


def test_the_same_answer_changes_nothing() -> None:
    allowed = _written(OLD)
    assert (
        refresh_addresses(allowed, {"github.com": [OLD]}, now=100.0, overlap_seconds=120) == allowed
    )


def test_a_zero_window_drops_an_address_the_moment_it_leaves() -> None:
    allowed = refresh_addresses(_written(OLD), {"github.com": [NEW]}, now=100.0, overlap_seconds=0)
    allowed = refresh_addresses(allowed, {"github.com": [NEWER]}, now=200.0, overlap_seconds=0)
    assert allowed.cidrs == (OLD, NEWER)


def test_an_address_two_names_share_is_listed_once() -> None:
    allowed = AllowedAddresses(
        written=(OLD, PYPI),
        current=(("github.com", (OLD,)), ("pypi.org", (PYPI,))),
    )
    allowed = refresh_addresses(allowed, {"pypi.org": [OLD]}, now=100.0, overlap_seconds=120)
    # Written addresses lead, each once, whichever name answers them now.
    assert allowed.cidrs == (OLD, PYPI)
    # pypi.org's written address is pinned, so it is not retiring either.
    assert allowed.retiring == ()


# ----- the setting -----------------------------------------------------------


def test_the_window_is_a_deployment_setting_with_a_documented_default() -> None:
    assert DEFAULT_ADDRESS_OVERLAP_SECONDS == 120.0
    assert kubernetes_config(Settings()).address_overlap_window_seconds == 120.0
    config = kubernetes_config(Settings(kubernetes={"address_overlap_window_seconds": 30}))
    assert config.address_overlap_window_seconds == 30.0
    assert kubernetes_config(Settings(kubernetes={"address_overlap_window_seconds": 0}))
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        Settings(kubernetes={"address_overlap_window_seconds": -1})


# ----- the provider ----------------------------------------------------------


class _Answers:
    """A resolver whose answers a test changes while the worker runs."""

    def __init__(self) -> None:
        self.by_host: dict[str, list[str]] = {k: list(v) for k, v in HOST_ADDRESSES.items()}
        self.lookups: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.lookups.append(host)
        return list(self.by_host.get(host, []))


def _config(*, overlap_seconds: float) -> KubernetesConfig:
    return KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        resolve_ttl_seconds=0.01,
        address_overlap_window_seconds=overlap_seconds,
    )


async def _running_worker(
    *, overlap_seconds: float
) -> tuple[FakeKubernetesApi, KubernetesProvider, _Answers, Handle]:
    answers = _Answers()
    api, _registry, provider = build(
        config=_config(overlap_seconds=overlap_seconds), resolver=answers
    )
    # The fake worker ends after this many observations; the tests take fewer.
    api.script(ATTEMPT, "succeed", after=100)
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.RUNNING
    return api, provider, answers, handle


def _https_cidrs(api: FakeKubernetesApi) -> set[str]:
    """The ipBlocks of the worker policy's TCP 443 rule, read from the live object."""
    policy = api.get("networkpolicies", f"np-worker-{ATTEMPT.lower()}")
    cidrs: set[str] = set()
    for rule in policy["spec"]["egress"]:
        if rule.get("ports") != [{"protocol": "TCP", "port": 443}]:
            continue
        for peer in rule["to"]:
            assert set(peer) == {"ipBlock"}, peer
            assert set(peer["ipBlock"]) == {"cidr"}, peer
            cidrs.add(peer["ipBlock"]["cidr"])
    return cidrs


def _policy(api: FakeKubernetesApi) -> dict[str, Any]:
    return api.get("networkpolicies", f"np-worker-{ATTEMPT.lower()}")


async def _wait_for_resolve_interval(provider: KubernetesProvider) -> None:
    deadline = provider._launched[ATTEMPT].egress_refreshed_at + provider.config.resolve_ttl_seconds
    await async_wait_until(
        lambda: time.monotonic() >= deadline, describe="worker resolve interval to expire"
    )


async def _wait_for_overlap_expiry(provider: KubernetesProvider) -> None:
    allowed = provider._launched[ATTEMPT].allowed
    assert allowed is not None and allowed.retiring
    deadline = max(since for _address, since in allowed.retiring)
    deadline += provider.config.address_overlap_window_seconds
    await async_wait_until(
        lambda: time.monotonic() >= deadline,
        describe="retiring addresses' overlap window to expire",
    )


async def _observe_after_the_interval(provider: KubernetesProvider, handle: Handle) -> None:
    await _wait_for_resolve_interval(provider)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.RUNNING


async def test_ac1_a_running_worker_whose_name_moves_is_allowed_the_old_and_the_new_address() -> (
    None
):
    api, provider, answers, handle = await _running_worker(overlap_seconds=60.0)
    assert _https_cidrs(api) == {OLD, PYPI}
    before = _policy(api)

    answers.by_host["github.com"] = [NEW]
    await _observe_after_the_interval(provider, handle)

    assert _https_cidrs(api) == {OLD, NEW, PYPI}
    after = _policy(api)
    # One merge patch of the egress rules: the object's identity is as written.
    assert after["metadata"]["name"] == before["metadata"]["name"]
    assert after["metadata"]["labels"] == before["metadata"]["labels"]
    assert after["metadata"]["annotations"] == before["metadata"]["annotations"]
    assert after["spec"]["podSelector"] == before["spec"]["podSelector"]
    dns_rules = [r for r in after["spec"]["egress"] if r["ports"][0]["port"] == 53]
    assert dns_rules == [r for r in before["spec"]["egress"] if r["ports"][0]["port"] == 53]
    # Every allowlisted name was looked up again, pypi.org included, on the interval.
    assert answers.lookups.count("pypi.org") >= 2


async def test_ac2_after_the_window_the_old_address_is_removed() -> None:
    api, provider, answers, handle = await _running_worker(overlap_seconds=0.5)
    answers.by_host["github.com"] = [NEW]
    await _observe_after_the_interval(provider, handle)
    assert _https_cidrs(api) == {OLD, NEW, PYPI}

    answers.by_host["github.com"] = [NEWER]
    await _observe_after_the_interval(provider, handle)
    # Inside the window the address that left the answer is still allowed.
    assert _https_cidrs(api) == {OLD, NEW, NEWER, PYPI}

    await _wait_for_overlap_expiry(provider)
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.RUNNING
    # After it, the old address is gone and the current one stays. The launch address
    # stays too: the Pod's hostAliases pin github.com to it and cannot change.
    assert _https_cidrs(api) == {OLD, NEWER, PYPI}


async def test_the_addresses_the_pod_is_pinned_to_are_never_dropped() -> None:
    api, provider, answers, handle = await _running_worker(overlap_seconds=0.0)
    job = api.get("jobs", f"worker-{ATTEMPT.lower()}")
    aliases = {a["ip"]: a["hostnames"] for a in job["spec"]["template"]["spec"]["hostAliases"]}
    assert aliases == {"140.82.121.4": ["github.com"], "151.101.0.223": ["pypi.org"]}

    answers.by_host["github.com"] = [NEW]
    await _observe_after_the_interval(provider, handle)
    answers.by_host["github.com"] = [NEWER]
    await _observe_after_the_interval(provider, handle)
    # A zero window drops NEW the moment it leaves the answer; OLD, the pinned address,
    # is not touched by the window at all.
    assert _https_cidrs(api) == {OLD, NEWER, PYPI}
    assert (
        api.get("jobs", f"worker-{ATTEMPT.lower()}")["spec"]["template"]["spec"]["hostAliases"]
        == job["spec"]["template"]["spec"]["hostAliases"]
    )


async def test_nothing_is_patched_while_the_answer_stands() -> None:
    api, provider, _answers, handle = await _running_worker(overlap_seconds=60.0)
    version = _policy(api)["metadata"]["resourceVersion"]
    await _observe_after_the_interval(provider, handle)
    await _observe_after_the_interval(provider, handle)
    assert _policy(api)["metadata"]["resourceVersion"] == version


async def test_the_names_are_looked_up_again_only_on_the_interval() -> None:
    answers = _Answers()
    config = KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        resolve_ttl_seconds=300.0,
    )
    api, _registry, provider = build(config=config, resolver=answers)
    api.script(ATTEMPT, "succeed", after=100)
    launch = spec()
    handle = await provider.launch(await provider.prepare(launch), launch)
    lookups = len(answers.lookups)
    answers.by_host["github.com"] = [NEW]
    for _ in range(3):
        await provider.observe(handle)
    # Within the resolve TTL the cached answer stands and the policy is not touched.
    assert len(answers.lookups) == lookups
    assert _https_cidrs(api) == {OLD, PYPI}


async def test_an_answer_in_a_denied_range_is_never_added() -> None:
    api, provider, answers, handle = await _running_worker(overlap_seconds=60.0)
    answers.by_host["github.com"] = ["10.96.0.1/32"]
    await _observe_after_the_interval(provider, handle)
    assert _https_cidrs(api) == {OLD, PYPI}


async def test_a_failed_patch_is_tried_again_on_the_next_observation() -> None:
    api, provider, answers, handle = await _running_worker(overlap_seconds=60.0)
    answers.by_host["github.com"] = [NEW]
    api.fail_next("patch", kind="networkpolicies")
    await _observe_after_the_interval(provider, handle)
    assert _https_cidrs(api) == {OLD, PYPI}
    await _observe_after_the_interval(provider, handle)
    assert _https_cidrs(api) == {OLD, NEW, PYPI}


async def test_a_lookup_that_fails_leaves_the_policy_as_it_is() -> None:
    api, provider, answers, handle = await _running_worker(overlap_seconds=60.0)

    def failing(host: str) -> list[str]:
        raise OSError("no resolver")

    provider.resolve = failing
    await _observe_after_the_interval(provider, handle)
    assert _https_cidrs(api) == {OLD, PYPI}
    del answers


async def test_an_ended_worker_is_not_refreshed() -> None:
    answers = _Answers()
    api, _registry, provider = build(config=_config(overlap_seconds=60.0), resolver=answers)
    launch = spec()
    handle = await provider.launch(await provider.prepare(launch), launch)
    for _ in range(5):
        observation = await provider.observe(handle)
        if observation.state is ObservationState.EXITED:
            break
    assert observation.state is ObservationState.EXITED
    answers.by_host["github.com"] = [NEW]
    await _wait_for_resolve_interval(provider)
    await provider.observe(handle)
    assert _https_cidrs(api) == {OLD, PYPI}


# ----- an adopted worker (Codex finding: adopted workers were never refreshed) -----


async def _restart(provider: KubernetesProvider) -> None:
    """What a supervisor restart leaves: no memory of the launch, then reconcile."""
    provider._launched.clear()
    provider._resolved.clear()
    adopted = await provider.reconcile()
    assert [h.attempt_id for h in adopted] == [ATTEMPT]


async def test_an_adopted_worker_keeps_following_its_names() -> None:
    api, provider, answers, handle = await _running_worker(overlap_seconds=60.0)
    before = _policy(api)
    await _restart(provider)

    answers.by_host["github.com"] = [NEW]
    observation = await provider.observe(handle)
    assert observation.state is ObservationState.RUNNING
    assert _https_cidrs(api) == {OLD, NEW, PYPI}
    after = _policy(api)
    # Only the allowlist rule moved: every other rule is as the policy was written.
    others = [
        r for r in after["spec"]["egress"] if r["ports"] != [{"protocol": "TCP", "port": 443}]
    ]
    assert others == [
        r for r in before["spec"]["egress"] if r["ports"] != [{"protocol": "TCP", "port": 443}]
    ]
    assert after["metadata"]["annotations"] == before["metadata"]["annotations"]
    assert after["spec"]["podSelector"] == before["spec"]["podSelector"]


async def test_an_adopted_worker_keeps_its_pinned_addresses_and_retires_the_rest() -> None:
    api, provider, answers, handle = await _running_worker(overlap_seconds=0.5)
    answers.by_host["github.com"] = [NEW]
    await _observe_after_the_interval(provider, handle)
    assert _https_cidrs(api) == {OLD, NEW, PYPI}

    await _restart(provider)
    answers.by_host["github.com"] = [NEWER]
    await provider.observe(handle)
    # NEW was a previous process's refresh: it gets the overlap window, not a cut.
    assert _https_cidrs(api) == {OLD, NEW, NEWER, PYPI}

    await _wait_for_overlap_expiry(provider)
    await provider.observe(handle)
    # OLD is what hostAliases pins the Pod to and stays; NEW is gone after the window.
    assert _https_cidrs(api) == {OLD, NEWER, PYPI}


async def test_an_adopted_worker_whose_policy_cannot_be_read_is_left_as_it_is() -> None:
    api, provider, answers, handle = await _running_worker(overlap_seconds=60.0)
    provider._launched.clear()
    api.fail_next("get", kind="networkpolicies")
    await provider.reconcile()
    assert provider._launched[ATTEMPT].egress_plan is None
    answers.by_host["github.com"] = [NEW]
    await _observe_after_the_interval(provider, handle)
    assert _https_cidrs(api) == {OLD, PYPI}


# ----- the interval clock (Codex finding: it advanced before the patch landed) -----


def _age(provider: KubernetesProvider, seconds: float) -> None:
    """Move the last lookup and the resolution cache `seconds` into the past."""
    provider._launched[ATTEMPT].egress_refreshed_at -= seconds
    for host, (at, addresses) in list(provider._resolved.items()):
        provider._resolved[host] = (at - seconds, addresses)


def _long_interval_worker() -> tuple[FakeKubernetesApi, KubernetesProvider, _Answers]:
    answers = _Answers()
    config = KubernetesConfig(
        poll_interval_seconds=0,
        launch_timeout_seconds=5,
        storage_class="lab-ssd",
        image_pull_secret="ghcr-pull",
        resolve_ttl_seconds=300.0,
    )
    api, _registry, provider = build(config=config, resolver=answers)
    api.script(ATTEMPT, "succeed", after=100)
    return api, provider, answers


async def test_a_failed_patch_is_retried_without_waiting_a_whole_interval() -> None:
    api, provider, answers = _long_interval_worker()
    launch = spec()
    handle = await provider.launch(await provider.prepare(launch), launch)
    _age(provider, 301.0)
    answers.by_host["github.com"] = [NEW]
    api.fail_next("patch", kind="networkpolicies")
    await provider.observe(handle)
    assert _https_cidrs(api) == {OLD, PYPI}
    lookups = len(answers.lookups)
    # The next observation, well inside the 300 second interval, lands the patch from
    # the cached answer without asking the resolver again.
    await provider.observe(handle)
    assert _https_cidrs(api) == {OLD, NEW, PYPI}
    assert len(answers.lookups) == lookups


async def test_a_lookup_that_changes_nothing_restarts_the_interval() -> None:
    api, provider, _answers = _long_interval_worker()
    launch = spec()
    handle = await provider.launch(await provider.prepare(launch), launch)
    _age(provider, 301.0)
    stale = provider._launched[ATTEMPT].egress_refreshed_at
    await provider.observe(handle)
    assert provider._launched[ATTEMPT].egress_refreshed_at > stale + 300.0
    assert _https_cidrs(api) == {OLD, PYPI}
