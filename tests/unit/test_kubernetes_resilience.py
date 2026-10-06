"""The Kubernetes provider rides out an API server that cannot answer, and a namespace
that is full, without losing an attempt or waiting in silence (lab findings of
2026-09-29)."""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from http.client import BadStatusLine
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec, kubernetes
from crucible.adapters.execution.k8sapi import KubernetesUnavailableError
from crucible.adapters.execution.kubernetes import (
    CollectionUnavailableError,
    HarnessRefusedError,
    KubernetesConfig,
    KubernetesProvider,
)
from crucible.domain.role_timeouts import parse_role_timeouts
from crucible.ports.execution import (
    Handle,
    LaunchWaitError,
    ObservationState,
    ProviderUnavailableError,
)
from tests.unit.kubernetes_fixtures import build, spec

CODEX_IMAGE = "crucible-worker:codex-fake-succeed-2"
CLAIM = "ws-01attempt0000000000000000a"


def _auth(last_refresh: str) -> bytes:
    return json.dumps(
        {"tokens": {"access_token": "not-a-real-value"}, "last_refresh": last_refresh}
    ).encode()


async def _run_to_exit(provider: KubernetesProvider, handle: Handle) -> Any:
    observation = await provider.observe(handle)
    while observation.state is ObservationState.RUNNING:
        observation = await provider.observe(handle)
    return observation


async def _codex(**build_kwargs: Any) -> Any:
    api, registry, provider = build(harness="codex", **build_kwargs)
    registry.register(CODEX_IMAGE, harness="codex", version="0.153.4")
    api.put_harness_secret("crucible-harness-codex", {"auth.json": _auth("2026-09-20T00:00:00Z")})
    launch = spec(harness="codex", image=CODEX_IMAGE)
    workspace = await provider.prepare(launch)
    return api, provider, launch, workspace


# ----- one failed look is not an answer ------------------------------------


@pytest.mark.parametrize(
    "cause",
    [ssl.SSLError("handshake failed"), BadStatusLine("malformed response")],
    ids=["wrapped_ssl_error", "wrapped_http_parsing_error"],
)
async def test_a_wrapped_transport_error_at_prepare_retries_under_the_budget(
    monkeypatch: pytest.MonkeyPatch, cause: Exception
) -> None:
    elapsed = 0.0
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds

    monkeypatch.setattr(time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    api, _registry, provider = build(
        config=KubernetesConfig(poll_interval_seconds=0, api_retry_seconds=4)
    )
    error = KubernetesUnavailableError(0, "the API server connection failed")
    error.__cause__ = cause
    api.fail_next("create", 2, kind="persistentvolumeclaims", error=error)

    workspace = await provider.prepare(spec())

    assert workspace.started_from
    assert api.create_attempts.count("persistentvolumeclaims") == 3
    assert sleeps == [1, 2]


async def test_a_reader_pod_that_the_api_server_hiccups_on_still_comes_up() -> None:
    """`_await_running` asks again after a failed look, as `_await_job` does, where it
    used to give up on the first error and fail the collection."""
    api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    await _run_to_exit(provider, handle)
    api.fail_next("get", 3, kind="pods")
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.report is not None
    assert api.outages[-1][2] == 0, "the reader's looks never met the outage"


async def test_a_canary_the_api_server_could_not_run_does_not_refuse_the_launch() -> None:
    """A readiness canary that failed because the API server could not answer is no
    verdict on the namespace: the launch fails as an environment failure the retry
    rule covers (never a refusal and a wake), and the next launch runs it again."""
    api, _registry, provider = build(
        config=KubernetesConfig(poll_interval_seconds=0, api_retry_seconds=0)
    )
    launch = spec()
    api.fail_next("create", 1, kind="pods")
    with pytest.raises(ProviderUnavailableError) as raised:
        await provider.prepare(launch)
    assert not isinstance(raised.value, HarnessRefusedError)
    assert provider.probe is not None and provider.probe.unavailable
    workspace = await provider.prepare(launch)
    assert provider.probe.passed
    assert workspace.attempt_id == launch.attempt_id


async def test_a_namespace_that_really_is_not_ready_is_still_refused() -> None:
    _api, _registry, provider = build(egress_enforced=False)
    with pytest.raises(HarnessRefusedError, match="not ready"):
        await provider.prepare(spec())


# ----- the credential copy is read back before it is removed ---------------


async def test_a_copy_that_could_not_be_read_back_is_kept_and_collected_again() -> None:
    """12: the copy may hold the only live token the harness rotated into it. A read-back
    the API server broke leaves it where it is and the collection is tried again; the
    second one reads it, writes it back, and only then removes it."""
    api, provider, launch, workspace = await _codex()
    handle = await provider.launch(workspace, launch)
    api.claims[CLAIM]["credential/auth.json"] = _auth("2026-09-21T00:00:00Z")
    await _run_to_exit(provider, handle)
    api.fail_next("create", 1, kind="pods")
    with pytest.raises(CollectionUnavailableError, match="could not be read back"):
        await provider.collect(handle, workspace, launch)
    assert api.claims[CLAIM]["credential/auth.json"] == _auth("2026-09-21T00:00:00Z")
    assert api.secret_exists("cred-01attempt0000000000000000a")

    outputs = await provider.collect(handle, workspace, launch)
    sync = outputs.credential_sync
    assert sync is not None and sync.removed
    assert [(f.name, f.synced) for f in sync.files] == [("auth.json", True)]
    assert api.harness_secret("crucible-harness-codex")["auth.json"] == _auth(
        "2026-09-21T00:00:00Z"
    )
    assert "credential/auth.json" not in api.claims[CLAIM]


async def test_a_collector_the_api_server_could_not_create_is_collected_again() -> None:
    api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    await _run_to_exit(provider, handle)
    api.fail_next("create", 1, kind="jobs")
    with pytest.raises(CollectionUnavailableError, match="503"):
        await provider.collect(handle, workspace, launch)
    assert api.outages[-1][2] == 0
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.report is not None


async def test_a_collector_wait_the_api_server_never_answered_is_collected_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 237 review: when every look at the collector's Pod fails until the wait runs
    out, that is an outage to retry, not a collector that took too long."""
    _api, _registry, provider = build(config=KubernetesConfig(poll_interval_seconds=0))
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    await _run_to_exit(provider, handle)
    looks = provider._pod_of
    real = time.monotonic

    async def unanswered(job_name: str) -> Any:
        if job_name.startswith(kubernetes.OBJECT_PREFIX[k8sspec.ROLE_COLLECTOR]):
            # The outage outlasts the whole wait: the clock jumps past its deadline.
            monkeypatch.setattr(time, "monotonic", lambda: real() + 10**6)
            raise KubernetesUnavailableError(503, "the API server is down")
        return await looks(job_name)

    provider._pod_of = unanswered  # type: ignore[method-assign]
    with pytest.raises(CollectionUnavailableError, match="did not answer"):
        await provider.collect(handle, workspace, launch)


# ----- a full namespace says so at once ------------------------------------


async def test_a_worker_the_quota_refused_is_a_wait_naming_the_quota() -> None:
    """The Job controller retries a quota-refused Pod forever and never fails the Job;
    `launch` reads the `FailedCreate` event at once and, since hades #423, ends as a
    wait with the quota's own words rather than as a running attempt the quota then
    fails: the Job goes, and the supervisor launches the attempt again later."""
    api, _registry, provider = build(
        config=KubernetesConfig(poll_interval_seconds=0, launch_timeout_seconds=3600)
    )
    launch = spec()
    workspace = await provider.prepare(launch)
    api.quota_refused_roles.add(k8sspec.ROLE_WORKER)
    with pytest.raises(LaunchWaitError, match="exceeded quota"):
        await provider.launch(workspace, launch)
    assert not [name for kind, name in api.objects if kind == "jobs" and name.startswith("worker")]


async def test_a_collector_the_quota_refused_is_a_wait_not_a_verdict() -> None:
    api, _registry, provider = build(
        config=KubernetesConfig(poll_interval_seconds=0, launch_timeout_seconds=3600)
    )
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    await _run_to_exit(provider, handle)
    api.quota_refused_roles.add(k8sspec.ROLE_COLLECTOR)
    started = time.monotonic()
    with pytest.raises(CollectionUnavailableError, match="exceeded quota"):
        await provider.collect(handle, workspace, launch)
    assert time.monotonic() - started < 30


def _quota(api: Any, hard: dict[str, str]) -> None:
    api.create("resourcequotas", {"metadata": {"name": "workers"}, "spec": {"hard": hard}})


@pytest.mark.parametrize(
    ("hard", "expected"),
    [
        # The shipped base quota: every limit fits three Pods at the defaults, one of
        # which is kept for Hades's own short-role Pods (hades #423): two workers.
        (
            {
                "count/jobs.batch": "17",
                "requests.cpu": "3",
                "requests.memory": "12Gi",
                "limits.cpu": "6",
                "limits.memory": "12Gi",
            },
            2,
        ),
        # Jobs for five, memory for two Pods: one worker beside the reserved Pod, where
        # the Job count alone said five.
        ({"count/jobs.batch": "25", "limits.memory": "8Gi"}, 1),
        # CPU requests for one (the default requests half of a 2-CPU limit): a lone
        # attempt's Pods never meet, so one worker still runs.
        ({"count/jobs.batch": "25", "requests.cpu": "1500m"}, 1),
    ],
)
async def test_capacity_is_the_fewest_attempts_any_quota_admits(
    hard: dict[str, str], expected: int
) -> None:
    api, _registry, provider = build()
    _quota(api, hard)
    await provider.health()
    assert provider.capabilities().max_concurrency == expected


# ----- the short roles' time starts when their Pod runs --------------------


async def test_a_role_pod_that_never_starts_is_ended_by_the_launch_timeout() -> None:
    """The role's own time counts from Running; a Pod that never gets there is bounded by
    the launch timeout and says why, rather than eating the role's time."""
    api, _registry, provider = build(
        config=KubernetesConfig(poll_interval_seconds=0, launch_timeout_seconds=1)
    )
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    await _run_to_exit(provider, handle)
    api.pending_forever.add(launch.attempt_id)
    code = await provider._run_role_job(
        launch,
        role=k8sspec.ROLE_BUNDLE,
        image=launch.image,
        script="true",
        mounts=[],
        volumes=[provider._claim_volume(launch.attempt_id)],
        limits=provider._limits(launch),
        timeout=provider.config.role_timeout_seconds,
        plan=k8sspec.EgressPlan(),
    )
    assert code == -2
    text, _unavailable = provider.role_errors[(k8sspec.ROLE_BUNDLE, launch.attempt_id)]
    assert "did not start within 1s" in text
    del workspace


async def test_a_role_jobs_deadline_leaves_room_for_the_pull() -> None:
    api, _registry, provider = build()
    provider.apply_timeouts({"role_timeout_seconds": 45})
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    await _run_to_exit(provider, handle)
    await provider.collect(handle, workspace, launch)
    bundle = next(
        row["body"]
        for row in api.created
        if row["kind"] == "jobs" and row["name"].startswith("verify-bundle-")
    )
    deadline = bundle["spec"]["activeDeadlineSeconds"]
    assert deadline == 45 + provider.config.launch_timeout_seconds


def test_the_saved_timeout_wins_and_a_bad_one_is_refused() -> None:
    _api, _registry, provider = build(
        config=KubernetesConfig(poll_interval_seconds=0, role_timeout_seconds=120)
    )
    provider.apply_timeouts({"role_timeout_seconds": 300})
    assert provider.config.role_timeout_seconds == 300
    provider.apply_timeouts({"role_timeout_seconds": "soon"})
    assert provider.config.role_timeout_seconds == 300
    provider.apply_timeouts(None)
    assert provider.config.role_timeout_seconds == 120


@pytest.mark.parametrize(
    "document",
    [
        {"role_timeout_seconds": 5},
        {"role_timeout_seconds": 3601},
        {"role_timeout_seconds": True},
        {"role_timeout_seconds": "120"},
        {"role_timeout_seconds": 120, "launch_seconds": 60},
        {},
    ],
)
def test_the_role_timeout_document_is_checked(document: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_role_timeouts(document)


def test_a_good_role_timeout_document_is_kept() -> None:
    assert parse_role_timeouts({"role_timeout_seconds": 600}) == {"role_timeout_seconds": 600}


# ----- a kept claim can be released ----------------------------------------


async def test_releasing_a_kept_claim_deletes_it_and_what_is_labelled_with_it() -> None:
    api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    await _run_to_exit(provider, handle)
    await provider.collect(handle, workspace, launch)
    from crucible.ports.execution import CleanupPolicy  # noqa: PLC0415

    await provider.cleanup(workspace, CleanupPolicy.KEEP, launch)
    assert CLAIM in api.claims
    await provider.release_workspace(workspace, launch)
    assert CLAIM not in api.claims
    assert not api.object_names("configmaps")
    # Idempotent: a claim already gone is the state asked for.
    await provider.release_workspace(workspace, launch)


async def test_a_role_that_may_wait_for_quota_room_waits_and_names_the_quota() -> None:
    """The publisher's Jobs wait for a slot rather than failing the publication; when
    the wait runs out the error says it was the quota."""
    api, _registry, provider = build(
        config=KubernetesConfig(poll_interval_seconds=0.01, launch_timeout_seconds=1)
    )
    launch = spec()
    workspace = await provider.prepare(launch)
    api.quota_refused_roles.add(k8sspec.ROLE_PUBLISHER)
    started = time.monotonic()
    code = await provider._run_role_job(
        launch,
        role=k8sspec.ROLE_PUBLISHER,
        image=launch.image,
        script="true",
        mounts=[],
        volumes=[provider._claim_volume(launch.attempt_id)],
        limits=provider._limits(launch),
        timeout=1,
        plan=k8sspec.EgressPlan(),
        wait_for_quota=True,
    )
    assert code == -2
    assert time.monotonic() - started >= 1.5
    text, _unavailable = provider.role_errors[(k8sspec.ROLE_PUBLISHER, launch.attempt_id)]
    assert "exceeded quota" in text
    del workspace


async def test_an_exec_the_api_server_broke_keeps_the_copy_too() -> None:
    """Review of 2026-09-29: the reader Pod came up but its exec failed, which the read
    records as unreadable rather than raising. That is still "not read back", so the
    copy stays, and the second collection syncs it once and removes it."""
    api, provider, launch, workspace = await _codex()
    handle = await provider.launch(workspace, launch)
    api.claims[CLAIM]["credential/auth.json"] = _auth("2026-09-21T00:00:00Z")
    await _run_to_exit(provider, handle)
    api.fail_next("pod_exec", 1)
    with pytest.raises(CollectionUnavailableError, match=r"credential/auth\.json"):
        await provider.collect(handle, workspace, launch)
    assert api.claims[CLAIM]["credential/auth.json"] == _auth("2026-09-21T00:00:00Z")
    assert api.harness_secret("crucible-harness-codex")["auth.json"] == _auth(
        "2026-09-20T00:00:00Z"
    )
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.credential_sync is not None and outputs.credential_sync.removed
    assert api.harness_secret("crucible-harness-codex")["auth.json"] == _auth(
        "2026-09-21T00:00:00Z"
    )


async def test_a_collection_run_again_records_the_first_sync_not_an_absent_file() -> None:
    """The copy was read back, written back and removed, then the collector could not be
    created: the retried collection reports the sync that happened."""
    api, provider, launch, workspace = await _codex()
    handle = await provider.launch(workspace, launch)
    api.claims[CLAIM]["credential/auth.json"] = _auth("2026-09-21T00:00:00Z")
    await _run_to_exit(provider, handle)
    api.quota_refused_roles.add(k8sspec.ROLE_COLLECTOR)
    with pytest.raises(CollectionUnavailableError):
        await provider.collect(handle, workspace, launch)
    assert "credential/auth.json" not in api.claims[CLAIM]
    api.quota_refused_roles.clear()
    outputs = await provider.collect(handle, workspace, launch)
    sync = outputs.credential_sync
    assert sync is not None and sync.removed
    assert [(f.name, f.synced, f.reason) for f in sync.files] == [
        ("auth.json", True, "changed; newer issued-at, written back")
    ]


async def test_a_quota_refusal_the_last_try_met_does_not_end_the_next_one() -> None:
    """Role Job names repeat and an event outlives its Job: only this Job's events, by
    uid, count, so a retry after the namespace has room is not refused by the old one."""
    api, _registry, provider = build(
        config=KubernetesConfig(poll_interval_seconds=0, launch_timeout_seconds=3600)
    )
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    await _run_to_exit(provider, handle)
    api.quota_refused_roles.add(k8sspec.ROLE_COLLECTOR)
    with pytest.raises(CollectionUnavailableError, match="exceeded quota"):
        await provider.collect(handle, workspace, launch)
    assert api.events, "the refusal left an event behind"
    api.quota_refused_roles.clear()
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.report is not None


async def test_a_full_local_disk_fails_the_collection_rather_than_escaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crucible.adapters.execution import kubernetes as kubernetes_module  # noqa: PLC0415
    from crucible.adapters.execution.kubernetes import CollectionFailedError  # noqa: PLC0415

    _api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    await _run_to_exit(provider, handle)

    def full(archive: Any, into: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(kubernetes_module, "_extract", full)
    with pytest.raises(CollectionFailedError, match="No space left") as raised:
        await provider.collect(handle, workspace, launch)
    assert not isinstance(raised.value, ProviderUnavailableError)
