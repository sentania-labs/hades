"""hades #370: an attempt that dies before launch records why.

The 2026-10-02 cases ended `environment` with nothing but the exit class: the preparer's
output was gone with its Pod, a quota refusal looked like the attempt's failure, a clone
that made no progress waited out the whole prepare timeout, and a correction that could
not fetch its resume source did not say which source. This module proves the four asks:

- AC1: a preparer that exits non-zero ends environment with its last output lines on the
  attempt (detail, artifact, evidence) and in the wake.
- AC2: a worker or preparer Job the quota refused at admission is a wait, not the
  attempt's failure, with the refusal text recorded as it came.
- AC3: a preparer that logs nothing for the stall bound is ended with a detail naming the
  stall, before `prepare_timeout_seconds`.
- AC4: a correction whose resume source cannot be fetched names that source in the wake.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution import kubernetes as kubernetes_module
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.kubernetes import (
    JOB_API_ERROR,
    JOB_STALLED,
    JOB_TIMED_OUT,
    KubernetesConfig,
    _last_lines,
)
from crucible.adapters.execution.scripts import preparer_script
from crucible.application.wakes import environment_failure_summary
from crucible.cli.wiring import kubernetes_config
from crucible.contracts.evidence import PREPARER_LOG_NAME, PREPARER_LOG_TYPE, ROLE_PREPARER_LOG
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.ports.artifacts import StoredBlob
from crucible.ports.execution import LaunchWaitError, PrepareFailedError, ProviderError
from crucible.settings import Settings
from tests.unit.kubernetes_fixtures import build, spec
from tests.unit.test_issue_423_quota_capacity_waits import _events, _finishing

PREVIOUS = "01PREVIOUS000000000000000A"

# The fake's FailedCreate event, word for word (k8sfake `_start_job`).
FAILED_CREATE = (
    "exceeded quota: hades-workers, requested: limits.memory=4Gi, used: "
    "limits.memory=12Gi, limited: limits.memory=12Gi"
)
# A 403 body the API server itself answers a create with when a count quota is full.
FORBIDDEN = (
    'jobs.batch "prepare-01attempt0000000000000000a" is forbidden: exceeded quota: '
    "hades-workers, requested: count/jobs.batch=1, used: count/jobs.batch=60, "
    "limited: count/jobs.batch=60"
)
CLONE_FAILURE = (
    "Cloning into '/crucible/work/repo'...\n"
    "fatal: unable to access 'https://github.com/acme/example.git/': "
    "Could not resolve host: github.com"
)


def _config(**overrides: Any) -> KubernetesConfig:
    values: dict[str, Any] = {
        "poll_interval_seconds": 0,
        "launch_timeout_seconds": 5,
        "storage_class": "lab-ssd",
        **overrides,
    }
    return KubernetesConfig(**values)


def _correction(**overrides: Any) -> Any:
    launch = spec(**overrides)
    return replace(launch, role="correct")


def _refusing(
    api: Any, kind: str, body_text: str, *, status: int = 403, role: str | None = None
) -> None:
    """Make the fake API server refuse every create of `kind` (of `role`'s objects only,
    when given) with `body_text`."""
    original = api.create

    def refuse(created_kind: str, body: Any) -> Any:
        labels = (body.get("metadata") or {}).get("labels") or {}
        if created_kind == kind and (role is None or labels.get(k8sspec.LABEL_ROLE) == role):
            raise KubernetesApiError(status, body_text, path=f"/namespaces/x/{kind}")
        return original(created_kind, body)

    api.create = refuse


class _MemoryStore:
    """An artifact store that keeps what it was given, so a test reads it back."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, content: bytes) -> StoredBlob:
        digest = sha256(content).hexdigest()
        self.blobs[digest] = content
        return StoredBlob(path=f"blobs/{digest}", sha256=digest, size=len(content))

    def get(self, path: str) -> bytes:
        return self.blobs[path.removeprefix("blobs/")]

    def exists(self, path: str) -> bool:
        return path.removeprefix("blobs/") in self.blobs


def _supervisor_prepare_fails(
    monkeypatch: pytest.MonkeyPatch, error: ProviderError
) -> tuple[Any, Any, Any, _MemoryStore]:
    """A preparing attempt whose provider `prepare` raises `error`, with the evidence
    and classification steps real enough to read what they wrote."""
    supervisor, item, uow, _provider, _launch = _finishing(monkeypatch)
    monkeypatch.setattr(supervisor, "_prepare", AsyncMock(side_effect=error))
    store = _MemoryStore()
    supervisor._artifacts = store
    uow.artifacts.find_by_sha256.return_value = None
    return supervisor, item, uow, store


# ----- AC1: the preparer's output is the detail, the evidence and the wake -------------


async def test_a_failed_preparer_raises_with_its_output_and_exit() -> None:
    api, _registry, provider = build(config=_config())
    launch = spec()
    api.script(launch.attempt_id, "prepare-fails")

    with pytest.raises(PrepareFailedError) as raised:
        await provider.prepare(launch)

    error = raised.value
    assert "the preparer Job could not build the checkout (exit 3)" in str(error)
    assert "the fake preparer could not clone" in str(error)
    assert error.exit_code == 3
    assert error.output.strip() == "the fake preparer could not clone"
    assert error.resume_source is None
    # The preparer's words are also what the provider keeps for the role.
    assert "the fake preparer could not clone" in provider.last_error[k8sspec.ROLE_PREPARER]


def test_the_message_keeps_the_last_lines_of_a_long_output() -> None:
    """A clone's log can run to thousands of characters; the detail and the wake are
    capped, so what survives the cap is the end of it, where git says what went wrong."""
    output = "\n".join(f"Receiving objects: {i}% ({i}/100)" for i in range(100))
    output += "\nfatal: the remote end hung up unexpectedly"

    kept = _last_lines(output)

    assert kept.endswith("fatal: the remote end hung up unexpectedly")
    assert "Receiving objects: 0%" not in kept
    assert len(kept) <= 803
    assert _last_lines("one line") == "one line"


async def test_the_supervisor_keeps_the_output_as_evidence_and_puts_the_last_lines_in_the_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = PrepareFailedError(
        f"the preparer Job could not build the checkout (exit 128): {CLONE_FAILURE}",
        output=CLONE_FAILURE,
        exit_code=128,
    )
    supervisor, item, uow, store = _supervisor_prepare_fails(monkeypatch, error)

    assert not await supervisor._finish_launch(item, supervisor._providers.get("fake"))

    attempt = item.attempt
    assert attempt.exit_class is ExitClass.ENVIRONMENT
    assert attempt.state is AttemptState.COLLECTED
    assert attempt.termination_detail is not None
    assert attempt.termination_detail.startswith("prepare: ")
    assert "Could not resolve host: github.com" in attempt.termination_detail
    # The whole output is an artifact of the attempt, and an evidence row points at it.
    (artifact,) = [call.args[0] for call in uow.artifacts.add.call_args_list]
    assert artifact.type == PREPARER_LOG_TYPE
    assert artifact.filename == PREPARER_LOG_NAME
    assert artifact.attempt_id == attempt.id
    assert store.get(artifact.path).decode("utf-8") == CLONE_FAILURE
    rows = [call.args[0] for call in uow.evidence.add.call_args_list]
    (evidence,) = [row for row in rows if row.payload.get("role") == ROLE_PREPARER_LOG]
    assert evidence.kind == "artifact_present"
    assert evidence.verified is True
    assert evidence.artifact_id == artifact.id
    assert evidence.payload["lines"] == 2
    # The wake carries the same last lines.
    finish = supervisor._classify_and_finish
    finish.assert_called_once()
    summary = finish.call_args.kwargs["wake_summary"]
    assert summary.startswith("attempt 1 ended environment at prepare: ")
    assert "Could not resolve host: github.com" in summary
    assert summary == environment_failure_summary(
        1, "prepare", attempt.termination_detail.removeprefix("prepare: ")
    )


async def test_a_launch_failure_without_output_stores_no_preparer_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a preparer that ran has output. Any other environment end records its detail
    as before and no empty artifact."""
    supervisor, item, uow, _store = _supervisor_prepare_fails(
        monkeypatch, ProviderError("the preparer produced no HEAD")
    )

    assert not await supervisor._finish_launch(item, supervisor._providers.get("fake"))

    assert item.attempt.termination_detail == "prepare: the preparer produced no HEAD"
    uow.artifacts.add.assert_not_called()


# ----- AC2: a quota refusal at admission is a wait, with the refusal recorded ---------


async def test_a_preparer_pod_the_quota_refused_is_a_wait_with_the_event_verbatim() -> None:
    """The Job controller could not create the preparer's Pod: its FailedCreate event
    names the quota. The attempt waits for room, and the event's words are the reason."""
    api, _registry, provider = build(config=_config())
    launch = spec()
    api.quota_refused_roles.add(k8sspec.ROLE_PREPARER)

    with pytest.raises(LaunchWaitError) as raised:
        await provider.prepare(launch)

    assert str(raised.value).startswith("the namespace quota has no room for the preparer: ")
    assert FAILED_CREATE in str(raised.value)
    assert not isinstance(raised.value, PrepareFailedError)
    # Nothing of the preparer stays behind to count against the quota again.
    assert not any(kind == "jobs" for kind, _ in api.objects)


async def test_a_preparer_job_the_api_server_refused_for_the_quota_is_a_wait() -> None:
    """A `count/jobs.batch` quota refuses the Job itself with a 403; the body is kept."""
    api, _registry, provider = build(config=_config())
    _refusing(api, "jobs", FORBIDDEN)

    with pytest.raises(LaunchWaitError) as raised:
        await provider.prepare(spec())

    assert FORBIDDEN in str(raised.value)


@pytest.mark.parametrize(
    ("kind", "role"),
    [("configmaps", None), ("networkpolicies", k8sspec.ROLE_PREPARER)],
    ids=["identity-configmap", "preparer-networkpolicy"],
)
async def test_any_quota_resource_refusing_a_prepare_create_is_a_wait(
    kind: str, role: str | None
) -> None:
    """hades #423 made the claim and the preparer Job wait. Every other object the
    preparation creates counts against some quota resource too, and a 403 naming the
    quota on any of them is the same wait."""
    api, _registry, provider = build(config=_config())
    body = (
        f'{kind} "x" is forbidden: exceeded quota: hades-workers, requested: count/{kind}=1, '
        f"used: count/{kind}=20, limited: count/{kind}=20"
    )
    _refusing(api, kind, body, role=role)

    with pytest.raises(LaunchWaitError) as raised:
        await provider.prepare(spec())

    assert body in str(raised.value)


async def test_a_refusal_that_is_not_the_quota_still_ends_the_attempt() -> None:
    """Only the quota is a wait. A webhook that denied the object is the attempt's
    environment failure with the API server's words, as hades #423 left it."""
    api, _registry, provider = build(config=_config())
    _refusing(api, "configmaps", 'admission webhook "policy.lab" denied the request')

    with pytest.raises(ProviderError, match=r'webhook "policy\.lab" denied') as raised:
        await provider.prepare(spec())

    assert not isinstance(raised.value, LaunchWaitError)


async def test_a_worker_pod_the_quota_refused_is_a_wait_with_the_event_verbatim() -> None:
    api, _registry, provider = build(config=_config())
    launch = spec()
    workspace = await provider.prepare(launch)
    api.quota_refused_roles.add(k8sspec.ROLE_WORKER)

    with pytest.raises(LaunchWaitError) as raised:
        await provider.launch(workspace, launch)

    assert FAILED_CREATE in str(raised.value)
    assert launch.attempt_id not in provider._launched


async def test_the_supervisor_returns_a_refused_attempt_to_pending_with_the_refusal_uncut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is recorded as it came: a long one is not cut to the detail cap."""
    refusal = FORBIDDEN + "; " + ", ".join(f"pod-{i} holds limits.cpu=3" for i in range(60))
    assert len(refusal) > 1000
    supervisor, item, uow, _provider, _launch = _finishing(monkeypatch)
    monkeypatch.setattr(
        supervisor,
        "_prepare",
        AsyncMock(
            side_effect=LaunchWaitError(
                f"the namespace quota has no room for the preparer: {refusal}"
            )
        ),
    )

    assert not await supervisor._finish_launch(item, supervisor._providers.get("fake"))

    attempt = item.attempt
    assert attempt.state is AttemptState.PENDING
    assert item.task.state is TaskState.SCHEDULED
    assert attempt.exit_class is None
    assert attempt.termination_detail is None
    assert attempt.ended_at is None
    supervisor._classify_and_finish.assert_not_called()
    uow.artifacts.add.assert_not_called()
    (deferred,) = _events(uow, EventKind.HARNESS_LAUNCH_DEFERRED)
    assert deferred.payload["quota_wait"] is True
    assert deferred.payload["stage"] == "prepare"
    assert deferred.payload["detail"].endswith(refusal)


# ----- AC3: a silent preparer is ended at the stall bound -----------------------------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """The provider's monotonic clock, advanced one second per poll, so a wait of
    minutes takes no time and the test reads how long the provider waited."""
    state = {"now": 0.0}

    async def sleep(seconds: float) -> None:
        state["now"] += max(seconds, 1.0)

    monkeypatch.setattr(
        kubernetes_module,
        "time",
        SimpleNamespace(monotonic=lambda: state["now"], time=lambda: 1_760_000_000.0),
    )
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return state


async def test_a_silent_preparer_is_ended_at_the_stall_bound_before_the_prepare_timeout(
    clock: dict[str, float],
) -> None:
    api, _registry, provider = build(
        config=_config(
            launch_timeout_seconds=300, prepare_timeout_seconds=900, preparer_stall_seconds=60
        )
    )
    launch = spec()
    api.script(launch.attempt_id, "prepare-hangs")

    with pytest.raises(PrepareFailedError) as raised:
        await provider.prepare(launch)

    error = raised.value
    assert error.exit_code == JOB_STALLED
    assert str(error).startswith("the preparer Job could not build the checkout (stalled): ")
    assert "wrote no log output for 60s while it ran" in str(error)
    assert "below its 900s timeout" in str(error)
    assert "its last output: Cloning into '/crucible/work/repo'..." in str(error)
    # Ended at the bound, not at the prepare timeout.
    assert 60 <= clock["now"] < 120
    # The stalled Job is gone, as a finished one would be.
    assert not any(kind == "jobs" for kind, _ in api.objects)
    assert "wrote no log output" in provider.last_error[k8sspec.ROLE_PREPARER]


async def test_a_preparer_that_keeps_writing_is_not_a_stall(
    clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Progress is a log that changes. A clone still receiving objects for longer than
    the bound is left alone; the bound counts from its last line."""
    api, _registry, provider = build(
        config=_config(
            launch_timeout_seconds=300, prepare_timeout_seconds=900, preparer_stall_seconds=60
        )
    )
    launch = spec()
    api.script(launch.attempt_id, "prepare-hangs")
    original = api.pod_log
    reads = {"n": 0}

    def progressing(name: str, **kwargs: Any) -> Any:
        if name.startswith("prepare-") and reads["n"] < 150:
            reads["n"] += 1
            api.logs[name] = [
                f"2026-10-02T00:{reads['n'] // 60:02d}:{reads['n'] % 60:02d}.000000000Z "
                f"Receiving objects: {reads['n'] // 2}%"
            ]
        return original(name, **kwargs)

    monkeypatch.setattr(api, "pod_log", progressing)

    with pytest.raises(PrepareFailedError, match=r"could not build the checkout \(stalled\)"):
        await provider.prepare(launch)

    # About 150 polls of progress, then the 60 s bound: well past the bound alone.
    assert clock["now"] >= 200


async def test_without_a_stall_bound_the_silent_preparer_waits_out_the_prepare_timeout(
    clock: dict[str, float],
) -> None:
    """The bound is what ends it early: with the setting off, the same preparer runs to
    the prepare timeout and is reported as such."""
    api, _registry, provider = build(
        config=_config(
            launch_timeout_seconds=300, prepare_timeout_seconds=900, preparer_stall_seconds=0
        )
    )
    launch = spec()
    api.script(launch.attempt_id, "prepare-hangs")

    with pytest.raises(PrepareFailedError) as raised:
        await provider.prepare(launch)

    assert raised.value.exit_code == JOB_TIMED_OUT
    assert str(raised.value).startswith(
        "the preparer Job could not build the checkout (timed out): "
    )
    # The Pod ran, so what it wrote is read before the Job is deleted: the timeout
    # reason ends with it, and it is the output the attempt keeps.
    assert "did not finish within 900s of running" in str(raised.value)
    assert "its last output: Cloning into '/crucible/work/repo'..." in str(raised.value)
    assert raised.value.output.strip() == "Cloning into '/crucible/work/repo'..."
    assert clock["now"] >= 900


async def test_a_preparer_still_writing_at_the_prepare_timeout_keeps_its_log(
    clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone that keeps reporting progress is not a stall, but it can still run out
    the prepare timeout. Its log is kept as evidence and its last lines are the detail."""
    api, _registry, provider = build(
        config=_config(
            launch_timeout_seconds=300, prepare_timeout_seconds=900, preparer_stall_seconds=60
        )
    )
    launch = spec()
    api.script(launch.attempt_id, "prepare-hangs")
    original = api.pod_log
    reads = {"n": 0}

    def progressing(name: str, **kwargs: Any) -> Any:
        if name.startswith("prepare-"):
            reads["n"] += 1
            api.logs[name] = [
                f"2026-10-02T{reads['n'] // 3600:02d}:{reads['n'] // 60 % 60:02d}:"
                f"{reads['n'] % 60:02d}.000000000Z Receiving objects: {i}%"
                for i in range(reads["n"])
            ]
        return original(name, **kwargs)

    monkeypatch.setattr(api, "pod_log", progressing)

    with pytest.raises(PrepareFailedError) as raised:
        await provider.prepare(launch)

    error = raised.value
    assert error.exit_code == JOB_TIMED_OUT
    assert str(error).startswith("the preparer Job could not build the checkout (timed out): ")
    assert "Receiving objects: " in str(error)
    # The whole log, from its first line, is the output, not only the tail.
    assert error.output.startswith("Receiving objects: 0%\n")
    assert error.output.count("Receiving objects: ") > 900
    assert not any(kind == "jobs" for kind, _ in api.objects)


async def test_a_failed_preparer_keeps_its_whole_log_beyond_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detail and the wake carry a bounded tail; the evidence is the whole log. A
    clone that wrote more than the tail's 2,000 lines and 4,000 characters loses none of
    it from `output`."""
    api, _registry, provider = build(config=_config())
    launch = spec()
    api.script(launch.attempt_id, "prepare-fails")
    original = api.pod_log
    lines = [f"Receiving objects: {i}/5000" for i in range(5000)] + [
        "fatal: the remote end hung up unexpectedly"
    ]

    def long_log(name: str, **kwargs: Any) -> Any:
        if name.startswith("prepare-"):
            api.logs[name] = list(lines)
        return original(name, **kwargs)

    monkeypatch.setattr(api, "pod_log", long_log)

    with pytest.raises(PrepareFailedError) as raised:
        await provider.prepare(launch)

    error = raised.value
    assert error.output == "\n".join(lines) + "\n"
    assert str(error).endswith("fatal: the remote end hung up unexpectedly")
    assert "Receiving objects: 0/5000" not in str(error)
    # The role's error, read for messages, is still the bounded tail.
    assert len(provider.last_error[k8sspec.ROLE_PREPARER]) <= 4000


def test_the_stall_bound_has_a_default_well_below_the_prepare_timeout() -> None:
    config = KubernetesConfig()
    assert config.preparer_stall_seconds == 300
    assert config.preparer_stall_seconds * 3 <= config.prepare_timeout_seconds
    # The settings file seeds it, and the wiring passes it through.
    assert Settings().kubernetes.preparer_stall_seconds == 300
    wired = kubernetes_config(Settings(kubernetes={"preparer_stall_seconds": 45}))
    assert wired.preparer_stall_seconds == 45


def test_the_clone_reports_its_progress_so_a_large_transfer_is_not_a_stall() -> None:
    script = preparer_script(
        url="https://github.com/acme/example.git",
        base_ref="main",
        work_branch="crucible/EX-0001",
        from_remote_branch=False,
        cache_name=None,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
        origin_placeholder="crucible://origin",
        claude_md_wins=False,
        shims=(),
        exclude_entries=(),
        identity_mount="/crucible/identity",
    )
    assert "clone --progress --no-hardlinks --no-checkout" in script


# ----- Every form keeps the words the message had before hades #370 -------------------

# The kind tier's hades #191 test (tests/e2e/test_kind.py) expects the preparer that
# cannot reach its git host to fail with `ProviderError` matching these words, whether
# its clone exits non-zero or runs to the 45 s prepare timeout the test sets. The first
# head of this change said "the preparer timed out" there and the run failed (CI run
# 37718411730); the cause now lives in the parenthesis after the old words.
KIND_191_PATTERN = "preparer Job could not build"


def test_the_kind_tier_still_matches_these_words() -> None:
    """The pattern this module proves is the one the kind test matches, read from its
    source, so a change to either is a change to both."""
    kind = Path(__file__).parents[2] / "tests" / "e2e" / "test_kind.py"
    assert f'match="{KIND_191_PATTERN}"' in kind.read_text()


@pytest.mark.parametrize(
    ("exit_code", "cause"),
    [
        (3, "exit 3"),
        (128, "exit 128"),
        (JOB_STALLED, "stalled"),
        (JOB_TIMED_OUT, "timed out"),
        (JOB_API_ERROR, "its Pod never ran"),
    ],
)
def test_every_prepare_failure_form_begins_with_the_old_words(exit_code: int, cause: str) -> None:
    _api, _registry, provider = build(config=_config())
    for resume_source in (None, "the remote work branch 'crucible/EX-0001'"):
        error = provider._prepare_failed(exit_code, "fatal: could not connect", resume_source)
        message = str(error)
        assert re.search(KIND_191_PATTERN, message), message
        assert f"the preparer Job could not build the checkout ({cause}): " in message
        assert message.endswith("fatal: could not connect")
        assert error.exit_code == exit_code


async def test_a_preparer_that_runs_out_its_timeout_still_matches_the_kind_test(
    clock: dict[str, float],
) -> None:
    """hades #191 on the kind tier: the preparer's clone of a git host its policy does
    not permit hangs until `prepare_timeout_seconds` (45 there, well below the stall
    bound). Rendered through the new path with the fake, the message the kind test
    matches is the message the attempt records."""
    api, _registry, provider = build(
        config=_config(launch_timeout_seconds=45, prepare_timeout_seconds=45)
    )
    launch = spec()
    api.script(launch.attempt_id, "prepare-hangs")

    with pytest.raises(ProviderError, match=KIND_191_PATTERN) as raised:
        await provider.prepare(launch)

    assert isinstance(raised.value, PrepareFailedError)
    assert raised.value.exit_code == JOB_TIMED_OUT
    assert "did not finish within 45s of running" in str(raised.value)


async def test_a_preparer_that_stalls_still_matches_the_kind_test(
    clock: dict[str, float],
) -> None:
    api, _registry, provider = build(
        config=_config(
            launch_timeout_seconds=45, prepare_timeout_seconds=900, preparer_stall_seconds=30
        )
    )
    launch = spec()
    api.script(launch.attempt_id, "prepare-hangs")

    with pytest.raises(ProviderError, match=KIND_191_PATTERN) as raised:
        await provider.prepare(launch)

    assert isinstance(raised.value, PrepareFailedError)
    assert raised.value.exit_code == JOB_STALLED
    assert "wrote no log output for 30s while it ran" in str(raised.value)


# ----- AC4: a correction names the resume source it could not fetch -------------------


async def test_a_correction_resuming_from_the_remote_branch_names_it() -> None:
    api, _registry, provider = build(config=_config())
    launch = _correction()
    api.script(launch.attempt_id, "prepare-fails")

    with pytest.raises(PrepareFailedError) as raised:
        await provider.prepare(launch)

    assert raised.value.resume_source == "the remote work branch 'crucible/EX-0001'"
    assert str(raised.value).startswith(
        "the correction resumes from the remote work branch 'crucible/EX-0001', and the "
        "preparer Job could not build the checkout (exit 3): "
    )
    assert "the fake preparer could not clone" in str(raised.value)


async def test_a_correction_resuming_from_a_bundle_names_the_bundle() -> None:
    api, _registry, provider = build(config=_config())
    launch = _correction(
        resume_bundle_path="/workspace/previous/output/work_branch.bundle",
        resume_bundle_attempt_id=PREVIOUS,
        resume_bundle_head="a" * 40,
        resume_bundle_sha256="b" * 64,
        resume_bundle_ancestor="c" * 40,
    )
    api.script(launch.attempt_id, "prepare-fails")

    with pytest.raises(PrepareFailedError) as raised:
        await provider.prepare(launch)

    assert raised.value.resume_source == f"the sealed bundle of attempt {PREVIOUS}"
    assert f"the correction resumes from the sealed bundle of attempt {PREVIOUS}" in str(
        raised.value
    )


async def test_an_implementing_attempt_names_no_resume_source() -> None:
    api, _registry, provider = build(config=_config())
    launch = spec()
    api.script(launch.attempt_id, "prepare-fails")

    with pytest.raises(PrepareFailedError) as raised:
        await provider.prepare(launch)

    assert raised.value.resume_source is None
    assert "resumes from" not in str(raised.value)


async def test_the_wake_names_the_resume_source_the_correction_could_not_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "previous attempt bundle is gone"
    error = PrepareFailedError(
        f"the correction resumes from the sealed bundle of attempt {PREVIOUS}, and the "
        f"preparer Job could not build the checkout (exit 4): {output}",
        output=output,
        exit_code=4,
        resume_source=f"the sealed bundle of attempt {PREVIOUS}",
    )
    supervisor, item, _uow, _store = _supervisor_prepare_fails(monkeypatch, error)

    assert not await supervisor._finish_launch(item, supervisor._providers.get("fake"))

    summary = supervisor._classify_and_finish.call_args.kwargs["wake_summary"]
    assert f"resumes from the sealed bundle of attempt {PREVIOUS}" in summary
    assert "previous attempt bundle is gone" in summary
    assert item.attempt.termination_detail is not None
    assert f"sealed bundle of attempt {PREVIOUS}" in item.attempt.termination_detail
