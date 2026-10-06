"""The harness registry and its administration (07, 13, 25).

The registry resolves a harness name to an adapter and refuses an unknown or disabled
name with a reason the supervisor turns into a wake. The operator's configuration entry
is a harness's starting value (it ships off for a harness whose dedicated session is
unverified, S1b, with the reason); until an administrator decides, a launch needs it and
the admin's runtime flag both. Once an administrator has enabled or disabled the harness
(`crucible admin harnesses enable|disable`, the Harnesses page, the admin API), that
stored decision alone decides, without a restart (hades #174).

Also here: the image-version check every launch runs (a combination outside the tested
range is a refusal, never a warning), the egress allowlist a worker gets (the union of
the policy's list and the adapter's declared endpoints, S6), and the state services
that record what runs observed about a credential.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crucible.application.transitions import record_event
from crucible.domain.entities import HarnessState
from crucible.domain.events import PRINCIPAL_CRUCIBLE, PRINCIPAL_WORKER, EventKind
from crucible.domain.lifecycle import AttemptState
from crucible.domain.secrets import redact
from crucible.ports.clock import Clock
from crucible.ports.execution import (
    HARNESSES_LABEL,
    LEGACY_HARNESS_LABEL,
    harness_version_label,
    image_harnesses,
)
from crucible.ports.harness import (
    CredentialSource,
    CredentialSpec,
    HarnessAdapter,
    HarnessGate,
    HarnessUnavailableError,
    MountMode,
    SessionCompatibility,
)
from crucible.ports.repository import UnitOfWork

__all__ = [
    "CredentialState",
    "HarnessCheck",
    "HarnessRegistry",
    "HarnessUnavailableError",
    "UnsupportedHarnessVersionError",
    "check_image_version",
    "credential_state",
    "effective_mount_mode",
    "egress_allowlist",
    "record_credential_observation",
    "record_launch_outcome",
    "set_harness_enabled",
]


class UnsupportedHarnessVersionError(Exception):
    """The image's harness version is outside the adapter's tested range (07)."""


class HarnessRegistry:
    """The adapters by name, and the two enable gates a launch has to pass."""

    def __init__(self, adapters: Iterable[HarnessAdapter]) -> None:
        self._adapters: dict[str, HarnessAdapter] = {a.name: a for a in adapters}

    def __iter__(self) -> Iterator[HarnessAdapter]:
        return iter(self._adapters.values())

    def __contains__(self, name: object) -> bool:
        return name in self._adapters

    def names(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def get(self, name: str) -> HarnessAdapter | None:
        return self._adapters.get(name)

    def require(self, name: str) -> HarnessAdapter:
        adapter = self._adapters.get(name)
        if adapter is None:
            raise HarnessUnavailableError(name, "no adapter declares this harness")
        return adapter

    def resolve(
        self,
        name: str,
        *,
        gates: Mapping[str, HarnessGate] | None = None,
        state: HarnessState | None = None,
    ) -> HarnessAdapter:
        """The adapter for a launch, or a refusal with the reason (07, 25).

        `gates` is the operator's configuration; `state` is the admin's runtime row. A
        harness with no row is enabled by default, so a registry test without a
        database behaves like a fresh deployment's seeded defaults for the verified
        harness.

        The configuration is the starting value only (hades #174): once an
        administrator has enabled or disabled the harness through the service, that
        decision alone decides, read from the row on every call, so it takes effect
        with no restart."""
        adapter = self.require(name)
        gate = (gates or {}).get(name)
        decided = state is not None and state.enabled_decided
        if gate is not None and not gate.enabled and not decided:
            raise HarnessUnavailableError(
                name,
                f"off by the configuration default: {gate.reason or 'no reason recorded'}; "
                "an administrator can enable it on Harnesses",
            )
        if state is not None and not state.enabled:
            raise HarnessUnavailableError(
                name, f"disabled by an administrator: {state.reason or 'no reason recorded'}"
            )
        return adapter


@dataclass(frozen=True, slots=True)
class HarnessCheck:
    ok: bool
    detail: str
    installed: str | None = None
    supported: str = ""
    endpoints: tuple[str, ...] = field(default=())


def check_image_version(
    registry: HarnessRegistry, harness: str, labels: Mapping[str, str]
) -> HarnessCheck:
    """Compare the version the image's labels pin for this harness with the adapter's
    tested range (13). One worker image carries every harness (C11), so the check is
    per harness: the label `crucible.harness.<name>.version` of the harness launched."""
    adapter = registry.get(harness)
    if adapter is None:
        return HarnessCheck(False, f"no adapter declares harness {harness!r}")
    supported = adapter.supported_versions.text
    endpoints = adapter.capabilities().endpoints
    carried = image_harnesses(labels)
    if not carried:
        # 13: an image that does not say which harness it carries is not launched with
        # any harness's credential, whatever else its labels say.
        return HarnessCheck(
            False,
            f"the image carries no {HARNESSES_LABEL} (or {LEGACY_HARNESS_LABEL}) label",
            None,
            supported,
            endpoints,
        )
    if harness not in carried:
        return HarnessCheck(
            False,
            f"the image declares harness {', '.join(sorted(carried))}, "
            f"the execution asks for {harness!r}",
            None,
            supported,
            endpoints,
        )
    installed = carried[harness] or None
    if installed is None:
        return HarnessCheck(
            False,
            f"the image carries no {harness_version_label(harness)} label",
            None,
            supported,
            endpoints,
        )
    if not adapter.supported_versions.supports(installed):
        return HarnessCheck(
            False,
            f"harness {harness} {installed} is outside the tested range {supported}",
            installed,
            supported,
            endpoints,
        )
    return HarnessCheck(
        True,
        f"harness {harness} {installed} is inside {supported}",
        installed,
        supported,
        endpoints,
    )


def egress_allowlist(
    registry: HarnessRegistry,
    harness: str,
    policy_hosts: list[str],
    extra: list[str],
    endpoint_url: str | None = None,
) -> tuple[str, ...]:
    """The union of the policy's allowlist and the adapter's declared endpoints (13)."""
    adapter = registry.get(harness)
    hosts = set(policy_hosts) | set(extra)
    if adapter is not None and not endpoint_url:
        hosts |= set(adapter.capabilities().endpoints)
    if endpoint_url:
        parsed = urlsplit(endpoint_url)
        if parsed.hostname:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            hosts.add(f"{parsed.hostname}:{port}")
    return tuple(sorted(h for h in hosts if h))


def effective_mount_mode(spec: CredentialSpec, source: CredentialSource | None) -> MountMode:
    """The stricter of the adapter's declared minimum and what the operator configured
    (25 step 7). A configuration never lowers the adapter's minimum."""
    configured = source.mount_mode if source is not None else None
    if spec.harness == "codex" and configured is None:
        return MountMode.RENEWER
    if configured is MountMode.RENEWER:
        return MountMode.RENEWER
    if spec.minimum_mode is MountMode.RENEWER and configured is not MountMode.RW_NARROW:
        return MountMode.RENEWER
    if spec.minimum_mode is MountMode.RW_NARROW or configured is MountMode.RW_NARROW:
        return MountMode.RW_NARROW
    return MountMode.RO


# ----- credential status, sanitized (25) ---------------------------------


@dataclass(frozen=True, slots=True)
class CredentialState:
    """What `GET /harnesses` says about a credential: never a value (25)."""

    state: str
    mount_mode: str | None
    source_fingerprint: str | None
    files: tuple[dict[str, Any], ...]
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "mount_mode": self.mount_mode,
            "source_fingerprint": self.source_fingerprint,
            "files": [dict(f) for f in self.files],
            "detail": self.detail,
        }


def credential_state(
    spec: CredentialSpec | None,
    source: CredentialSource | None,
    state: HarnessState | None,
    *,
    stored_sizes: Mapping[str, int] | None = None,
    stored_in: str = "",
) -> CredentialState:
    """`absent`, `configured`, `invalid` or `validated` from the files' presence and the
    recorded observations. The fingerprint is a sha256 of names and sizes only.

    `stored_in` names where the files are when that is not a directory (the harness
    Secret, ADR 0015), and `stored_sizes` is what it holds by auth file name, None when
    it does not exist."""
    if spec is None:
        return CredentialState("not_required", None, None, (), "this harness needs no credential")
    if stored_in:
        if stored_sizes is None:
            return CredentialState(
                "absent",
                effective_mount_mode(spec, source).value,
                None,
                (),
                f"{stored_in} does not exist",
            )
    elif source is None or not source.path:
        return CredentialState("absent", None, None, (), "no credential path is configured")
    mode = effective_mount_mode(spec, source).value
    files: list[dict[str, Any]] = []
    missing: list[str] = []
    digest = hashlib.sha256()
    for auth in spec.auth_files:
        try:
            if stored_in:
                if stored_sizes is None or auth.name not in stored_sizes:
                    raise FileNotFoundError(auth.name)
                size = stored_sizes[auth.name]
            else:
                assert source is not None
                size = spec.source_path(source.path, auth.name).stat().st_size
        except OSError:
            files.append({"name": auth.name, "present": False, "required": auth.required})
            if auth.required:
                missing.append(auth.name)
            continue
        files.append({"name": auth.name, "present": True, "required": auth.required, "size": size})
        digest.update(f"{auth.name}:{size}\n".encode())
    if missing:
        return CredentialState("absent", mode, None, tuple(files), f"missing: {', '.join(missing)}")
    fingerprint = digest.hexdigest()
    if state is not None and state.last_auth_failure_at is not None:
        last_ok = max(
            (
                v
                for v in (
                    state.last_validated_at,
                    state.last_successful_launch_at,
                )
                if v is not None
            ),
            default=None,
        )
        if last_ok is None or state.last_auth_failure_at >= last_ok:
            return CredentialState(
                "invalid",
                mode,
                fingerprint,
                tuple(files),
                "the last launch failed authentication; see last_auth_failure_at",
            )
    if state is not None and state.last_validated_at is not None:
        return CredentialState("validated", mode, fingerprint, tuple(files))
    return CredentialState("configured", mode, fingerprint, tuple(files))


# The attempt states in which an attempt holds its harness credential: from the seeding
# in `prepare` until collect has synced the copy back and removed it, which is after
# `exited` (12). The per-harness cap counts these, and a login refuses while any exists.
CREDENTIAL_HOLDING_STATES: tuple[AttemptState, ...] = (
    AttemptState.PREPARING,
    AttemptState.LAUNCHING,
    AttemptState.RUNNING,
    AttemptState.TERMINATING,
    AttemptState.EXITED,
)


def credential_holders(uow: UnitOfWork, harness: str) -> list[str]:
    """The attempts that hold `harness`'s credential right now (12), by id."""
    holders: list[str] = []
    for attempt in uow.attempts.list_in_states(list(CREDENTIAL_HOLDING_STATES)):
        execution = uow.executions.get(attempt.execution_id)
        if execution is not None and execution.harness == harness:
            holders.append(attempt.id)
    return sorted(holders)


def fingerprint_directory(root: Path, names: Iterable[str]) -> str | None:
    """sha256 over `name:size` of the named files, in order; None when any is missing."""
    digest = hashlib.sha256()
    for name in names:
        try:
            size = (root / name).stat().st_size
        except OSError:
            return None
        digest.update(f"{name}:{size}\n".encode())
    return digest.hexdigest()


# ----- progress lines (07) ----------------------------------------------------

PROGRESS_MAX_LINES = 200
PROGRESS_MAX_CHARS = 1000


def ingest_progress(
    uow: UnitOfWork,
    clock: Clock,
    *,
    attempt_id: str,
    task_id: str,
    execution_id: str,
    progress: Sequence[Mapping[str, Any]],
) -> int:
    """07: progress lines the worker wrote are events with the worker as source, marked
    unverified. Bounded per attempt and per line, and redacted line by line, because
    the file is the worker's and its content is data. Returns the count recorded."""
    recorded = 0
    for index, entry in enumerate(progress[:PROGRESS_MAX_LINES]):
        line = redact(json.dumps(dict(entry), sort_keys=True, default=str))
        record_event(
            uow,
            clock,
            EventKind.WORKER_PROGRESS,
            principal=PRINCIPAL_WORKER,
            task_id=task_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
            payload={
                "index": index,
                "line": line[:PROGRESS_MAX_CHARS],
                "truncated": len(line) > PROGRESS_MAX_CHARS,
            },
            verified=False,
        )
        recorded += 1
    return recorded


# ----- state services (25): the same functions the admin API and CLI call ------


def _summary(state: HarnessState) -> dict[str, Any]:
    return {
        "enabled": state.enabled,
        "enabled_decided": state.enabled_decided,
        "reason": state.reason,
        "session_compatibility": state.session_compatibility,
        "mount_mode_observed": state.mount_mode_observed,
        "refresh_requires_rw": state.refresh_requires_rw,
    }


def _ensure_state(uow: UnitOfWork, clock: Clock, name: str) -> HarnessState:
    state = uow.harnesses.get(name)
    if state is None:
        state = HarnessState(
            name=name,
            enabled=True,
            reason="",
            session_compatibility=SessionCompatibility.UNVERIFIED.value,
            updated_at=clock.now(),
            updated_by=PRINCIPAL_CRUCIBLE,
        )
    return state


def harness_state(uow: UnitOfWork, clock: Clock, name: str) -> HarnessState:
    """The harness's runtime row, or the defaults a fresh deployment's row starts from."""
    return _ensure_state(uow, clock, name)


def set_harness_enabled(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal_name: str,
    name: str,
    enabled: bool,
    reason: str,
    session_compatibility: SessionCompatibility | None = None,
    warning: str = "",
) -> HarnessState:
    """Enable or disable a harness (25): configuration retained, running attempts finish,
    new launches refused with a wake. Every change is an event with the principal, the
    reason when one was given, and a before-and-after summary that carries no value.

    It is an administrator's decision (hades #174): from here the configuration entry
    no longer decides the harness's availability, this row does."""
    state = _ensure_state(uow, clock, name)
    before = _summary(state)
    state.enabled = enabled
    state.enabled_decided = True
    state.reason = reason.strip()
    if session_compatibility is not None:
        state.session_compatibility = session_compatibility.value
    state.updated_at = clock.now()
    state.updated_by = principal_name
    uow.harnesses.put(state)
    record_event(
        uow,
        clock,
        EventKind.HARNESS_ENABLED if enabled else EventKind.HARNESS_DISABLED,
        principal=principal_name,
        payload={
            "harness": name,
            "reason": state.reason,
            "before": before,
            "after": _summary(state),
            # What the configuration said when the administrator decided anyway: the
            # unverified reason, recorded with the decision (hades #174).
            **({"configuration_warning": warning} if warning else {}),
        },
    )
    return state


def record_launch_outcome(
    uow: UnitOfWork,
    clock: Clock,
    *,
    name: str,
    outcome: str,
    at: datetime,
    auth_failure: bool = False,
) -> HarnessState:
    """What the last launch of this harness came to (25 status: last launch outcome)."""
    state = _ensure_state(uow, clock, name)
    state.last_launch_at = at
    state.last_launch_outcome = outcome
    if auth_failure:
        state.last_auth_failure_at = at
    else:
        state.last_successful_launch_at = at
    state.updated_at = clock.now()
    uow.harnesses.put(state)
    return state


def record_credential_observation(
    uow: UnitOfWork,
    clock: Clock,
    *,
    name: str,
    mount_mode: MountMode,
    changed: bool,
    at: datetime,
) -> HarnessState:
    """A run's observation of its credential copy (12, 25 step 6): whether the named
    auth files changed. A change under any mode means refresh needs writable state; a
    run that changes nothing never lowers what was observed before."""
    state = _ensure_state(uow, clock, name)
    state.mount_mode_observed = mount_mode.value
    if changed:
        state.refresh_requires_rw = True
    elif state.refresh_requires_rw is None:
        state.refresh_requires_rw = False
    state.updated_at = clock.now()
    uow.harnesses.put(state)
    return state
