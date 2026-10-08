"""Hades #394: the workspace claim of an attempt that never launched is cleaned up.

Cleanup of 08 waits for `logs_drained`, which only a launched worker records, so an
attempt that ended at prepare, was killed during launch or ran out of quota at reserve
kept its `ws-<attempt>` claim forever. The supervisor now cleans such an attempt up
under `delete` (no gate consumed the claim), and the retention sweep no longer holds
back the objects of one, so claims leaked before the fix drain on their own. A claim
with the retention label, a running attempt's claim, and a never-launched claim that is
a resume source are left alone."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.execution.kubernetes import KubernetesProvider
from crucible.application.supervisor import Supervisor
from crucible.contracts.evidence import EvidenceKind
from crucible.domain.entities import Attempt, EvidenceRecord
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState
from crucible.ports.execution import CleanupPolicy, LaunchSpec, ProviderError, Workspace
from tests.fixtures import FakeClock
from tests.unit.kubernetes_fixtures import build, spec
from tests.unit.test_class_routing import NOW

PRE_LAUNCH = "01ATTEMPT0000000000000000B"
LEAKED = "01ATTEMPT0000000000000000C"
RUNNING = "01ATTEMPT0000000000000000D"
KEPT = "01ATTEMPT0000000000000000E"
RESUME = "01ATTEMPT0000000000000000F"


def claim(attempt_id: str) -> str:
    return k8sspec.object_name("ws", attempt_id)


def attempt(
    attempt_id: str,
    state: AttemptState,
    exit_class: ExitClass | None = None,
    **fields: Any,
) -> Attempt:
    return Attempt(
        attempt_id,
        "execution",
        "task",
        1,
        state,
        NOW,
        exit_class=exit_class,
        ended_at=fields.pop("ended_at", NOW if state is not AttemptState.RUNNING else None),
        **fields,
    )


def bundle_evidence(attempt_id: str) -> EvidenceRecord:
    return EvidenceRecord(
        id=None,
        attempt_id=attempt_id,
        task_id="task",
        kind=EvidenceKind.BUNDLE_HEAD.value,
        source="crucible",
        observed_at=NOW,
        verified=True,
        payload={"head_sha": "a" * 40, "bundle_verified": True, "bundle_sha256": "b" * 64},
    )


def supervisor_over(
    monkeypatch: pytest.MonkeyPatch,
    provider: KubernetesProvider,
    attempts: list[Attempt],
    evidence: dict[str, list[EvidenceRecord]] | None = None,
) -> tuple[Supervisor, Any]:
    """The real supervisor steps over a mocked unit of work holding `attempts`."""
    supervisor = Supervisor(
        MagicMock(),
        {provider.name: provider},
        FakeClock(NOW),
        holder="test",
        artifact_store=MagicMock(),
    )
    uow: Any = MagicMock()
    uow.attempts.list_in_states.side_effect = lambda states: [
        row for row in attempts if row.state in set(states)
    ]
    uow.attempts.get.side_effect = lambda attempt_id, **_: next(
        (row for row in attempts if row.id == attempt_id), None
    )
    uow.executions.get.return_value = SimpleNamespace(provider=provider.name, contract_version="1")
    uow.tasks.get.return_value = SimpleNamespace(id="task", repository_id="repo")
    uow.contracts.get.return_value = SimpleNamespace(document=spec(attempt_id="task").contract)
    uow.repositories.get.return_value = SimpleNamespace(url="https://github.com/acme/example.git")
    uow.evidence.list_for_attempt.side_effect = lambda attempt_id: (evidence or {}).get(
        attempt_id, []
    )
    monkeypatch.setattr(supervisor, "_uow_factory", lambda: nullcontext(uow))
    monkeypatch.setattr(supervisor, "_fenced", lambda: nullcontext(uow))
    monkeypatch.setattr(supervisor, "_release_checkout_leases", MagicMock())
    return supervisor, uow


def _spec_returning(spec_: LaunchSpec) -> Callable[[Attempt], Awaitable[LaunchSpec]]:
    """Build a monkeypatch that makes ``_spec_for`` always return *spec_*."""

    async def _inner(attempt: Attempt) -> LaunchSpec:
        return spec_

    return _inner


async def prepared(api: FakeKubernetesApi, provider: KubernetesProvider, attempt_id: str) -> None:
    await provider.prepare(spec(attempt_id=attempt_id))
    assert claim(attempt_id) in api.object_names("persistentvolumeclaims")


def cleaned_payloads(uow: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for call in uow.events.append.call_args_list:
        event = call.args[0]
        if event.kind == EventKind.ATTEMPT_CLEANED_UP.value:
            out[str(event.attempt_id)] = event.payload["workspace"]
    return out


# ----- AC1: the supervisor's cleanup path for an attempt with started_at null ----------


async def test_an_attempt_that_ends_environment_at_prepare_has_its_claim_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _registry, provider = build()
    api.script(PRE_LAUNCH, "prepare-fails")
    with pytest.raises(ProviderError):
        await provider.prepare(spec(attempt_id=PRE_LAUNCH))
    # The preparer failed after the claim was made: the leak of the issue.
    assert claim(PRE_LAUNCH) in api.object_names("persistentvolumeclaims")

    row = attempt(PRE_LAUNCH, AttemptState.PREPARING)
    supervisor, uow = supervisor_over(monkeypatch, provider, [row])
    monkeypatch.setattr(supervisor, "_record_bare_evidence", MagicMock())
    monkeypatch.setattr(supervisor, "_classify_and_finish", MagicMock())
    monkeypatch.setattr(supervisor, "_spec_for", _spec_returning(spec(attempt_id=PRE_LAUNCH)))
    supervisor._environment_failure(row.id, "prepare", "the fake preparer could not clone")
    assert row.exit_class is ExitClass.ENVIRONMENT
    assert row.started_at is None and row.logs_drained_at is None
    assert row.workspace_path is None  # set only at launch: no filter may rely on it
    row.state = AttemptState.FAILED  # what _classify_and_finish does with no retry left

    # The cleanup of 08 never sees it: its logs were never drained.
    assert supervisor._list_cleanup_due() == []
    assert await supervisor._pre_launch_cleanup_step() == 1

    assert claim(PRE_LAUNCH) not in api.object_names("persistentvolumeclaims")
    assert ("persistentvolumeclaims", claim(PRE_LAUNCH)) in api.deleted
    assert row.cleaned_up_at == NOW
    assert cleaned_payloads(uow) == {PRE_LAUNCH: "delete"}
    # Recorded cleaned, it is not visited again.
    assert await supervisor._pre_launch_cleanup_step() == 0


@pytest.mark.parametrize(
    ("exit_class", "state"),
    [
        (ExitClass.KILLED, AttemptState.FAILED),  # cancelled during launch
        (ExitClass.QUOTA_EXHAUSTED, AttemptState.FAILED),  # refused at reserve
        (ExitClass.ENVIRONMENT, AttemptState.BLOCKED),
    ],
)
async def test_every_pre_launch_ending_has_its_claim_deleted(
    monkeypatch: pytest.MonkeyPatch, exit_class: ExitClass, state: AttemptState
) -> None:
    api, _registry, provider = build()
    await prepared(api, provider, PRE_LAUNCH)
    supervisor, uow = supervisor_over(
        monkeypatch, provider, [attempt(PRE_LAUNCH, state, exit_class)]
    )
    monkeypatch.setattr(supervisor, "_spec_for", _spec_returning(spec(attempt_id=PRE_LAUNCH)))

    assert await supervisor._pre_launch_cleanup_step() == 1

    assert api.object_names("persistentvolumeclaims") == []
    assert not [name for kind, name in api.objects if kind == "configmaps"]
    assert cleaned_payloads(uow) == {PRE_LAUNCH: "delete"}


async def test_an_interrupted_start_waits_the_attempt_lease_as_cleanup_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _registry, provider = build()
    await prepared(api, provider, PRE_LAUNCH)
    row = attempt(PRE_LAUNCH, AttemptState.FAILED, ExitClass.INFRASTRUCTURE)
    supervisor, _uow = supervisor_over(monkeypatch, provider, [row])
    monkeypatch.setattr(supervisor, "_spec_for", _spec_returning(spec(attempt_id=PRE_LAUNCH)))

    assert await supervisor._pre_launch_cleanup_step() == 0
    assert PRE_LAUNCH in supervisor._live_attempt_ids()
    assert claim(PRE_LAUNCH) in api.object_names("persistentvolumeclaims")

    row.ended_at = NOW - timedelta(seconds=supervisor.attempt_lease_ttl_seconds)
    assert await supervisor._pre_launch_cleanup_step() == 1
    assert api.object_names("persistentvolumeclaims") == []


# ----- AC2: the retention sweep drains a leaked claim and honours the label -------------


async def test_the_sweep_deletes_a_leaked_unlabelled_claim_and_leaves_a_labelled_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _registry, provider = build()
    await prepared(api, provider, LEAKED)
    await prepared(api, provider, KEPT)
    # Both attempts never launched and were never cleaned (they ended before this fix).
    # One claim carries the retention label, as an operator or an old cleanup left it.
    labels = api.objects[("persistentvolumeclaims", claim(KEPT))].body["metadata"]["labels"]
    labels[k8sspec.LABEL_RETAIN] = "keep"
    supervisor, _uow = supervisor_over(
        monkeypatch,
        provider,
        [
            attempt(LEAKED, AttemptState.FAILED, ExitClass.ENVIRONMENT),
            attempt(KEPT, AttemptState.FAILED, ExitClass.ENVIRONMENT),
        ],
    )

    keep = supervisor._live_attempt_ids()
    assert LEAKED not in keep and KEPT not in keep
    removed = await provider.retention(keep)

    assert removed >= 1
    assert ("persistentvolumeclaims", claim(LEAKED)) in api.deleted
    assert api.object_names("persistentvolumeclaims") == [claim(KEPT)]


async def test_a_finished_launched_attempt_not_yet_cleaned_is_still_held_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hades #237 is unchanged: a launched attempt's bundle is read until its cleanup."""
    api, _registry, provider = build()
    await prepared(api, provider, LEAKED)
    row = attempt(LEAKED, AttemptState.FAILED, ExitClass.COMPLETED, started_at=NOW)
    supervisor, _uow = supervisor_over(monkeypatch, provider, [row])

    keep = supervisor._live_attempt_ids()
    assert LEAKED in keep
    await provider.retention(keep)
    assert await supervisor._pre_launch_cleanup_step() == 0
    assert api.object_names("persistentvolumeclaims") == [claim(LEAKED)]


# ----- AC3: a running attempt's claim and a kept claim survive both paths ---------------


async def test_running_and_kept_claims_survive_the_cleanup_and_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _registry, provider = build()
    for attempt_id in (RUNNING, KEPT, RESUME):
        await prepared(api, provider, attempt_id)
    # A kept claim: a launched attempt that cleanup kept under `keep`.
    await provider.cleanup(workspace(KEPT), CleanupPolicy.KEEP, spec(attempt_id=KEPT))
    assert (
        k8sspec.LABEL_RETAIN
        in (api.objects[("persistentvolumeclaims", claim(KEPT))].body["metadata"]["labels"])
    )
    rows = [
        attempt(RUNNING, AttemptState.RUNNING, started_at=NOW),
        attempt(
            KEPT,
            AttemptState.SUCCEEDED,
            ExitClass.COMPLETED,
            started_at=NOW,
            logs_drained_at=NOW,
            cleaned_up_at=NOW,
        ),
        # Never launched, but its claim holds a verified bundle a correction may resume
        # from: kept, not deleted.
        attempt(RESUME, AttemptState.FAILED, ExitClass.QUOTA_EXHAUSTED),
    ]
    supervisor, uow = supervisor_over(
        monkeypatch, provider, rows, evidence={RESUME: [bundle_evidence(RESUME)]}
    )
    monkeypatch.setattr(supervisor, "_spec_for", _spec_returning(spec(attempt_id=RESUME)))

    assert await supervisor._pre_launch_cleanup_step() == 1
    assert cleaned_payloads(uow) == {RESUME: "keep"}
    keep = supervisor._live_attempt_ids()
    assert RUNNING in keep
    await provider.retention(keep)
    # A second pass changes nothing.
    assert await supervisor._pre_launch_cleanup_step() == 0
    await provider.retention(supervisor._live_attempt_ids())

    assert sorted(api.object_names("persistentvolumeclaims")) == sorted(
        claim(attempt_id) for attempt_id in (RUNNING, KEPT, RESUME)
    )
    resume_labels = api.objects[("persistentvolumeclaims", claim(RESUME))].body["metadata"][
        "labels"
    ]
    assert resume_labels[k8sspec.LABEL_RETAIN] == CleanupPolicy.KEEP.value
    assert not [name for kind, name in api.deleted if kind == "persistentvolumeclaims"]


def workspace(attempt_id: str) -> Workspace:
    return Workspace(
        attempt_id=attempt_id,
        checkout_path="k8s://ws/repo",
        identity_path="k8s://ws/identity",
        report_path="k8s://ws/report",
    )
