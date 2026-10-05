"""Credential administration (12, 25): validate, the bounded probe, rotate, remove.

Nothing here reads a credential value into a record. Validation is a shape check of
the named files (they exist, they parse, they carry the expected keys); the probe is a
run in the hardened image that records exit facts and whether the files changed; rotate
is an atomic rename with the previous directory retained and then shredded; remove
shreds. Every step is an event with the principal and the reason.

Where the credentials live depends on the deployment. With the Docker provider they are
directories under the credential root. On a deployment whose provider is Kubernetes
they are the harness Secrets in the workers namespace, which the service owns (ADR
0015): state, the shape check, the probe, the Hermes key and the login read and write
the Secret, and rotate and remove, which move directories, are not offered there.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
    record_refusal,
)
from crucible.application.admin.routing import gateway_url
from crucible.application.errors import ConflictError, ContractValidationError, NotFoundError
from crucible.application.harnesses import (
    credential_state,
    effective_mount_mode,
    record_credential_observation,
    record_launch_outcome,
    set_harness_enabled,
)
from crucible.application.runtime_settings import RuntimeValue, resolve, save_scalar
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    ExecutionProvider,
    ProbeRequest,
    ProbeResult,
    ProviderError,
)
from crucible.ports.harness import (
    CredentialSource,
    CredentialSpec,
    ExitInfo,
    HarnessAdapter,
    LaunchContext,
    MountMode,
)
from crucible.ports.repository import UnitOfWork

PROBE_PROMPT = "Reply with exactly the word OK and nothing else. Do not read or change any file."
RETIRED_MARK = ".retired-"
INCOMING_MARK = ".incoming-"
SHRED_CHUNK = 1024 * 1024
HERMES = "hermes"


def mount_mode_setting(harness: str) -> str:
    return f"credentials.{harness}.mount_mode"


def mount_mode_value(ctx: AdminContext, uow: UnitOfWork, harness: str) -> RuntimeValue:
    """The mount mode used by every runtime caller, with deployment input only a seed."""
    spec = spec_for(ctx, harness)
    source = ctx.credential_sources.get(harness)
    seed = source.mount_mode.value if source is not None and source.mount_mode is not None else None
    default = MountMode.RENEWER.value if harness == "codex" else spec.minimum_mode.value
    value = resolve(
        uow,
        name=mount_mode_setting(harness),
        field="mount_mode",
        seed=seed,
        seed_source="environment",
        default=default,
        applies="next launch",
    )
    try:
        mode = MountMode(str(value.value))
    except ValueError:
        mode = MountMode(default)
    return RuntimeValue(value.name, mode.value, value.source, value.applies, value.reason)


def effective_source(ctx: AdminContext, uow: UnitOfWork, harness: str) -> CredentialSource:
    original = ctx.credential_sources.get(harness)
    return CredentialSource(
        path=original.path if original is not None else "",
        mount_mode=MountMode(mount_mode_value(ctx, uow, harness).value),
    )


def set_mount_mode(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    mode: str,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx,
        uow,
        reason,
        principal=principal,
        operation="credential mount mode",
        reason_required=True,
    )
    spec = spec_for(ctx, harness)
    try:
        selected = MountMode(mode)
    except ValueError as exc:
        raise ContractValidationError(
            "mount_mode must be ro, rw-narrow, or renewer",
            errors=[{"path": "mount_mode", "message": "must be ro, rw-narrow, or renewer"}],
        ) from exc
    allowed = (
        {MountMode.RENEWER, MountMode.RW_NARROW}
        if spec.minimum_mode is MountMode.RENEWER
        else {MountMode.RO, MountMode.RW_NARROW}
        if spec.minimum_mode is MountMode.RO
        else {MountMode.RW_NARROW}
    )
    if selected not in allowed:
        detail = (
            f"harness {harness!r} declares {spec.minimum_mode.value}; "
            f"it does not support {selected.value}"
        )
        record_refusal(ctx, principal=principal, operation="credential mount mode", detail=detail)
        raise ContractValidationError(
            f"credential mount mode refused: {detail}",
            errors=[{"path": "mount_mode", "message": detail}],
        )
    before = mount_mode_value(ctx, uow, harness)
    save_scalar(
        uow,
        name=mount_mode_setting(harness),
        field="mount_mode",
        value=selected.value,
        principal=principal,
        reason=reason,
        now=ctx.clock.now(),
    )
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_MOUNT_MODE_SET,
        principal=principal,
        reason=reason,
        before={"mode": before.value, "source": before.source},
        after={"mode": selected.value, "source": "saved", "applies": "next launch"},
        harness=harness,
    )
    return mount_mode_view(ctx, uow, harness)


def mount_mode_view(ctx: AdminContext, uow: UnitOfWork, harness: str) -> dict[str, Any]:
    value = mount_mode_value(ctx, uow, harness)
    return {
        "mount_mode": value.value,
        "mount_mode_source": value.source,
        "mount_mode_applies": value.applies,
        "mount_mode_reason": value.reason or None,
    }


class CredentialAdminError(ConflictError):
    slug = "credential-admin"
    title = "Credential operation refused"


@dataclass(frozen=True, slots=True)
class ShapeCheck:
    ok: bool
    files: tuple[dict[str, Any], ...]
    problems: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "files": [dict(f) for f in self.files],
            "problems": list(self.problems),
        }


@dataclass(frozen=True, slots=True)
class ProbeRecord:
    """What the probe records (25): exit class, harness version, image digest, whether
    the auth files changed, and the duration. Nothing else."""

    harness: str
    exit_class: str
    exit_code: int | None
    harness_version: str | None
    image: str
    image_digest: str
    auth_files_changed: bool
    mount_mode: str
    duration_seconds: float
    files: tuple[dict[str, Any], ...] = ()
    detail: str = ""
    # Whether the run says anything about the credential at all. A probe that completed
    # and a probe that saw the provider reject the credential are both conclusive; a
    # probe that timed out, crashed, was blocked, lost, or never reached the provider
    # says nothing about the credential, only about the run.
    conclusive: bool = True
    # Empty when conclusive; otherwise why the run decided nothing.
    cause: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "exit_class": self.exit_class,
            "exit_code": self.exit_code,
            "harness_version": self.harness_version,
            "image": self.image,
            "image_digest": self.image_digest,
            "auth_files_changed": self.auth_files_changed,
            "mount_mode": self.mount_mode,
            "duration_seconds": self.duration_seconds,
            "files": [dict(f) for f in self.files],
            "detail": self.detail,
            "conclusive": self.conclusive,
            "cause": self.cause,
        }


@dataclass(frozen=True, slots=True)
class CredentialReport:
    harness: str
    state: dict[str, Any]
    shape: ShapeCheck | None = None
    probe: ProbeRecord | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"harness": self.harness, "credential": self.state}
        if self.shape is not None:
            out["shape"] = self.shape.as_dict()
        if self.probe is not None:
            out["probe"] = self.probe.as_dict()
        out.update(self.extra)
        return out


# ----- lookups -------------------------------------------------------------


def adapter_for(ctx: AdminContext, harness: str) -> HarnessAdapter:
    adapter = ctx.harnesses.get(harness)
    if adapter is None:
        raise NotFoundError(f"no adapter declares harness {harness!r}")
    return adapter


def spec_for(ctx: AdminContext, harness: str) -> CredentialSpec:
    credential = adapter_for(ctx, harness).credential_spec()
    if credential is None:
        raise CredentialAdminError(f"harness {harness!r} needs no credential")
    return credential


def secret_store(ctx: AdminContext) -> Any | None:
    """The provider that keeps the harness credentials as Secrets, or None when they are
    directories (ADR 0015). That is the Kubernetes provider whenever it is wired and the
    Docker provider is not: a deployment runs one or the other, and the directories are
    the Docker provider's."""
    if "docker" in ctx.providers:
        return None
    provider = ctx.providers.get("kubernetes")
    if provider is None or not callable(getattr(provider, "write_credential_files", None)):
        return None
    return provider


# How long a page waits for one harness Secret before it says the API server did not
# answer. The read runs on a worker thread, so a slow API server delays only that
# harness's row, never the event loop and the requests behind it (Codex review of PR 156).
SECRET_READ_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class SecretRead:
    """One read of a harness Secret: its body (None when absent), or why it failed."""

    body: dict[str, Any] | None
    error: str | None = None


def read_secret(store: Any, harness: str) -> SecretRead:
    """One blocking read of the harness Secret."""
    try:
        return SecretRead(store.read_credential_secret(harness))
    except ProviderError as exc:
        return SecretRead(None, str(exc))
    except OSError as exc:  # refused, reset, TLS: the API server did not answer at all
        return SecretRead(
            None,
            f"the credential Secret {store.credential_secret(harness)!r} could not be read: "
            f"the API server could not be reached ({type(exc).__name__})",
        )


async def read_secrets(
    ctx: AdminContext,
    harnesses: Iterable[str],
    *,
    timeout: float | None = None,
) -> dict[str, SecretRead]:
    """Each named harness's Secret read once, all at the same time on worker threads,
    each bounded by `timeout` (`SECRET_READ_TIMEOUT_SECONDS` unless given). Empty when
    the credentials are directories."""
    wait = SECRET_READ_TIMEOUT_SECONDS if timeout is None else timeout
    store = secret_store(ctx)
    if store is None:
        return {}
    names = [
        name
        for name in harnesses
        if (adapter := ctx.harnesses.get(name)) is not None
        and adapter.credential_spec() is not None
    ]

    async def one(name: str) -> SecretRead:
        try:
            return await asyncio.wait_for(asyncio.to_thread(read_secret, store, name), wait)
        except TimeoutError:
            return SecretRead(
                None,
                f"the credential Secret {store.credential_secret(name)!r} did not answer "
                f"within {wait:g} seconds",
            )

    return dict(zip(names, await asyncio.gather(*(one(n) for n in names)), strict=True))


def stored_files(store: Any, harness: str) -> dict[str, bytes] | None:
    """The declared auth files the harness Secret holds, None when it does not exist.
    A Secret the API server will not return is a refusal, not an absence."""
    try:
        files: dict[str, bytes] | None = store.read_credential_files(harness)
    except ProviderError as exc:
        raise CredentialAdminError(str(exc)) from exc
    return files


def source_for(ctx: AdminContext, harness: str) -> CredentialSource:
    source = ctx.credential_sources.get(harness)
    if source is None or not source.path:
        raise CredentialAdminError(
            f"no credential directory is configured for harness {harness!r} "
            f"(credentials.{harness}.path)"
        )
    return source


def state_view(
    ctx: AdminContext, uow: UnitOfWork, harness: str, secret: SecretRead | None = None
) -> dict[str, Any]:
    """The credential's state. On Kubernetes `secret` is the Secret as `read_secrets`
    already read it off the event loop; without one it is read here, once, blocking."""
    adapter = adapter_for(ctx, harness)
    state = uow.harnesses.get(harness)
    spec = adapter.credential_spec()
    source = effective_source(ctx, uow, harness) if spec is not None else None
    store = secret_store(ctx) if spec is not None else None
    if store is not None:
        view = _secret_state(
            store,
            spec,
            source,
            state,
            harness,
            secret if secret is not None else read_secret(store, harness),
        )
    else:
        view = credential_state(spec, source, state).as_dict()
    if harness == HERMES:
        # The key is never echoed; whether one is set is all any surface says (12).
        view["key_set"] = any(
            f.get("present") and int(f.get("size") or 0) > 1 for f in view.get("files") or []
        )
    view.update(
        {
            **(mount_mode_view(ctx, uow, harness) if spec is not None else {}),
            "session_compatibility": state.session_compatibility if state else "unverified",
            "refresh_requires_rw": state.refresh_requires_rw if state else None,
            "mount_mode_observed": state.mount_mode_observed if state else None,
            "last_validated_at": _iso(state.last_validated_at) if state else None,
            "last_auth_failure_at": _iso(state.last_auth_failure_at) if state else None,
            "last_launch_at": _iso(state.last_launch_at) if state else None,
            "last_launch_outcome": state.last_launch_outcome if state else None,
        }
    )
    return view


def _secret_state(
    store: Any,
    spec: CredentialSpec | None,
    source: CredentialSource | None,
    state: Any,
    harness: str,
    secret: SecretRead,
) -> dict[str, Any]:
    """The state of a Secret-held credential: what `credential_state` says of a
    directory, read from the Secret, plus which Secret it is and whether the service
    owns it. Sizes only; never a value."""
    name = store.credential_secret(harness)
    if secret.error is not None:
        return {
            "state": "unreadable",
            "mount_mode": None,
            "fingerprint": None,
            "files": [],
            "detail": secret.error,
            "source": {"kind": "secret", "name": name, "exists": None, "service_owned": None},
        }
    body = secret.body
    files = store.credential_files_in(harness, body)
    view = credential_state(
        spec,
        source,
        state,
        stored_sizes=None if files is None else {k: len(v) for k, v in files.items()},
        stored_in=f"the Secret {name}",
    ).as_dict()
    labels = ((body or {}).get("metadata") or {}).get("labels") or {}
    view["source"] = {
        "kind": "secret",
        "name": name,
        "exists": body is not None,
        "service_owned": labels.get("app.kubernetes.io/managed-by") == "crucible",
    }
    return view


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


# ----- shape check ---------------------------------------------------------


def check_shape(spec: CredentialSpec, path: str) -> ShapeCheck:
    """25 step 4: the named auth files exist, parse, and carry the expected fields.
    Nothing is printed; the record says which file failed and why, never what it held."""
    files: list[dict[str, Any]] = []
    problems: list[str] = []
    for auth in spec.auth_files:
        target = spec.source_path(path, auth.name)
        entry: dict[str, Any] = {"name": auth.name, "required": auth.required}
        try:
            stat = target.stat()
        except OSError:
            entry["present"] = False
            if auth.required:
                problems.append(f"{auth.name}: missing")
            files.append(entry)
            continue
        entry["present"] = True
        entry["size"] = stat.st_size
        entry["mode"] = oct(stat.st_mode & 0o777)
        if stat.st_size == 0:
            problems.append(f"{auth.name}: empty")
        if auth.json:
            try:
                document = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                problems.append(f"{auth.name}: not JSON")
                entry["parses"] = False
                files.append(entry)
                continue
            entry["parses"] = isinstance(document, dict)
            if not isinstance(document, dict):
                problems.append(f"{auth.name}: not a JSON object")
            else:
                missing = [k for k in auth.json_keys if k not in document]
                if missing:
                    problems.append(f"{auth.name}: missing keys {missing}")
        files.append(entry)
    return ShapeCheck(ok=not problems, files=tuple(files), problems=tuple(problems))


def check_shape_files(spec: CredentialSpec, files: Mapping[str, bytes]) -> ShapeCheck:
    """`check_shape` for auth files held in memory: what a login Job wrote, or what a
    harness Secret holds (ADR 0015). The same rules, and the same record: which file
    failed and why, never what it held."""
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    for auth in spec.auth_files:
        entry: dict[str, Any] = {"name": auth.name, "required": auth.required}
        content = files.get(auth.name)
        if content is None:
            entry["present"] = False
            if auth.required:
                problems.append(f"{auth.name}: missing")
            entries.append(entry)
            continue
        entry["present"] = True
        entry["size"] = len(content)
        if not content:
            problems.append(f"{auth.name}: empty")
        if auth.json:
            try:
                document = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                problems.append(f"{auth.name}: not JSON")
                entry["parses"] = False
                entries.append(entry)
                continue
            entry["parses"] = isinstance(document, dict)
            if not isinstance(document, dict):
                problems.append(f"{auth.name}: not a JSON object")
            else:
                missing = [k for k in auth.json_keys if k not in document]
                if missing:
                    problems.append(f"{auth.name}: missing keys {missing}")
        entries.append(entry)
    return ShapeCheck(ok=not problems, files=tuple(entries), problems=tuple(problems))


def current_shape(ctx: AdminContext, spec: CredentialSpec, harness: str) -> ShapeCheck:
    """The shape of the credential as it stands, wherever this deployment keeps it."""
    store = secret_store(ctx)
    if store is not None:
        return check_shape_files(spec, stored_files(store, harness) or {})
    return check_shape(spec, source_for(ctx, harness).path)


async def set_api_key(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    api_key: str,
    reason: str | None,
) -> CredentialReport:
    """Write one opaque API key without ever returning, logging, or auditing its value."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials set {harness}"
    )
    await write_api_key(
        ctx, uow, principal=principal, harness=harness, api_key=api_key, reason=reason
    )
    return await validate(
        ctx,
        uow,
        principal=principal,
        harness=harness,
        reason=reason,
        audit_events=False,
    )


async def write_api_key(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    api_key: str,
    reason: str,
) -> None:
    """The write half of `set_api_key`, for a caller that has already passed the guard
    (the local gateway save, crucible#119). Records `credential_set`; never the value."""
    if harness != HERMES:
        raise CredentialAdminError("the paste credential flow is available only for Hermes")
    value = api_key.strip()
    if not value:
        raise ContractValidationError(
            "an API key is required", errors=[{"path": "api_key", "message": "must not be empty"}]
        )
    spec = spec_for(ctx, harness)
    store = secret_store(ctx)
    if store is not None:
        # The Secret is the service's own (ADR 0015): created when absent, and the key
        # is its only file. The value is in the request body to the API server and
        # nowhere else.
        try:
            written = await asyncio.to_thread(
                store.write_credential_files, harness, {"api-key": value.encode("utf-8") + b"\n"}
            )
        except ProviderError as exc:
            raise CredentialAdminError(str(exc)) from exc
        admin_event(
            uow,
            ctx,
            EventKind.CREDENTIAL_SET,
            principal=principal,
            reason=reason,
            before={"harness": harness},
            after={
                "harness": harness,
                "state": "credential set",
                "secret": written["secret"],
                "created": written["created"],
            },
        )
        return
    source = source_for(ctx, harness)
    directory = Path(source.path)
    try:
        directory_stat = directory.stat()
    except OSError as exc:
        raise CredentialAdminError("the configured Hermes credential directory is absent") from exc
    if not directory.is_dir() or directory_stat.st_mode & 0o077:
        raise CredentialAdminError("the configured Hermes credential directory must be mode 0700")
    target = spec.source_path(source.path, "api-key")
    temporary = target.with_name(f".{target.name}.incoming")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value.encode("utf-8"))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_SET,
        principal=principal,
        reason=reason,
        before={"harness": harness},
        after={"harness": harness, "state": "credential set"},
    )


def read_api_key(ctx: AdminContext, harness: str = HERMES) -> str | None:
    """The stored Hermes key, for the one outbound call that sends it (the gateway's
    `/models`). None when none is stored. Blocking; never logged or returned."""
    store = secret_store(ctx)
    if store is not None:
        held = stored_files(store, harness)
        value = (held or {}).get("api-key", b"").decode("utf-8", "replace").strip()
        return value or None
    source = ctx.credential_sources.get(harness)
    if source is None or not source.path:
        return None
    key_path = spec_for(ctx, harness).source_path(source.path, "api-key")
    try:
        value = key_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


async def validate(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    reason: str | None,
    audit_events: bool = True,
) -> CredentialReport:
    """25: shape check of the named auth files, then the bounded probe; returns state
    and timestamps only. The shape check alone marks `invalid`; the probe decides
    `validated`."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials validate {harness}"
    )
    spec = spec_for(ctx, harness)
    before = state_view(ctx, uow, harness)
    shape = current_shape(ctx, spec, harness)
    probe: ProbeRecord | None = None
    if shape.ok:
        probe = await _probe_async(
            ctx,
            uow,
            harness=harness,
            principal=principal,
            reason=reason,
            audit_event=audit_events,
        )
    now = ctx.clock.now()
    state = uow.harnesses.get(harness)
    validated = (
        shape.ok
        and probe is not None
        and probe.conclusive
        and probe.exit_class
        in (
            ExitClass.COMPLETED.value,
            ExitClass.COMPLETED_WITHOUT_REPORT.value,
        )
    )
    observed_auth_failure = probe is not None and probe.exit_class == ExitClass.AUTH_FAILURE.value
    inconclusive = probe is not None and not probe.conclusive
    cause = probe.cause if probe is not None else ""
    if state is not None:
        if validated:
            state.last_validated_at = now
        elif not shape.ok or observed_auth_failure:
            # 25: `invalid` means the shape check failed or the probe saw the provider
            # refuse the credential. Those are the two pieces of evidence there are.
            state.last_auth_failure_at = now
        # An inconclusive probe (a timeout, a daemon that would not answer, a crash) says
        # nothing about the credential, only about the run, so the previous state stands
        # exactly as it was. Marking it invalid would take a working harness out of
        # service on latency, which is not a credential problem and is not what an
        # operator reading `invalid` would go and fix.
        state.updated_at = now
        uow.harnesses.put(state)
    after = state_view(ctx, uow, harness)
    if audit_events:
        admin_event(
            uow,
            ctx,
            EventKind.CREDENTIAL_VALIDATED,
            principal=principal,
            reason=reason,
            before=before,
            after=after,
            harness=harness,
            shape=shape.as_dict(),
            validated=validated,
            conclusive=not inconclusive,
            cause=cause,
        )
    return CredentialReport(
        harness,
        after,
        shape=shape,
        probe=probe,
        extra={"validated": validated, "conclusive": not inconclusive, "cause": cause},
    )


# ----- the bounded probe ---------------------------------------------------


def _probe_provider(ctx: AdminContext) -> ExecutionProvider:
    """Where the probe and the harness test run: where the credentials are. Docker when
    it is wired, then Kubernetes (the fake provider, wired only with test fixtures on,
    runs behaviours, not credentials, so it is only ever the answer when nothing else
    is)."""
    for name in ("docker", "kubernetes"):
        provider = ctx.providers.get(name)
        if provider is not None:
            return provider
    if ctx.providers:
        return next(iter(ctx.providers.values()))
    raise CredentialAdminError("no execution provider is configured for the probe")


async def probe_image(
    ctx: AdminContext, uow: UnitOfWork, provider: ExecutionProvider, harness: str
) -> str:
    """The image the probe runs: the harness's own default (ADR 0018), else the one
    labelled image the provider has for it, else a refusal naming the ambiguity (13)."""
    default = uow.harness_images.get(harness)
    if default is not None:
        return default.reference
    if provider.name == "fake":
        # The fake provider runs behaviours, not images (08).
        return "crucible-worker:fake-probe"
    images = [i for i in await provider.list_images() if i.carries(harness)]
    references = sorted({i.reference for i in images})
    if len(references) == 1:
        return references[0]
    if not references:
        raise CredentialAdminError(f"the provider has no image labelled for harness {harness!r}")
    raise CredentialAdminError(
        f"{len(references)} images are labelled for {harness!r} and none is promoted for "
        "it; promote one (images promote --harness) so the probe knows which to run"
    )


def _record_inconclusive(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    harness: str,
    principal: str,
    reason: str,
    image: str,
    mode: MountMode,
    cause: str,
    detail: str,
    duration: float,
) -> ProbeRecord:
    """A probe that decided nothing. It is recorded like any other, with its cause, and
    it moves no credential state: `last_launch_outcome` says `probe:inconclusive:<cause>`
    so the status shows what happened without claiming the credential is bad."""
    record = ProbeRecord(
        harness=harness,
        exit_class=ExitClass.ENVIRONMENT.value,
        exit_code=None,
        harness_version=None,
        image=image,
        image_digest="",
        auth_files_changed=False,
        mount_mode=mode.value,
        duration_seconds=round(duration, 1),
        detail=detail,
        conclusive=False,
        cause=cause,
    )
    record_launch_outcome(
        uow,
        ctx.clock,
        name=harness,
        outcome=f"probe:inconclusive:{cause}",
        at=ctx.clock.now(),
        auth_failure=False,
    )
    record_event_probe(uow, ctx, principal, record, reason)
    return record


async def _probe_async(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    harness: str,
    principal: str,
    reason: str,
    audit_event: bool = True,
    in_worker: bool = False,
) -> ProbeRecord:
    """The bounded probe. `in_worker` runs every harness in a worker, Hermes too, the
    way a task runs it (the harness test, crucible#118); without it Hermes is checked
    from the service with two HTTP requests, the cheap check it has always been."""
    if harness == HERMES and not in_worker:
        return await _hermes_probe_async(
            ctx,
            uow,
            harness=harness,
            principal=principal,
            reason=reason,
            audit_event=audit_event,
        )
    adapter = adapter_for(ctx, harness)
    spec = adapter.credential_spec()
    source = (
        effective_source(ctx, uow, harness)
        if secret_store(ctx) is not None or spec is None
        else source_for(ctx, harness)
    )
    if spec is not None and source is not None:
        source = CredentialSource(
            path=source.path,
            mount_mode=MountMode(mount_mode_value(ctx, uow, harness).value),
        )
    provider = _probe_provider(ctx)
    image = await probe_image(ctx, uow, provider, harness)
    mode = effective_mount_mode(spec, source) if spec is not None else MountMode.RO
    model, endpoint, endpoint_url = probe_route(uow, adapter, harness)
    launch = adapter.build_launch(
        LaunchContext(
            attempt_id="probe",
            model=model,
            effort=None,
            timeout_seconds=ctx.probe_timeout_seconds,
            identity_mount=IDENTITY_MOUNT,
            report_mount=REPORT_MOUNT,
            repo_mount=REPO_MOUNT,
            credential_mounted=spec is not None,
            credential_mode=mode,
            endpoint=endpoint,
            endpoint_url=endpoint_url,
            probe=True,
        )
    )
    request = ProbeRequest(
        harness=harness,
        image=image,
        argv=tuple(launch.argv),
        env=dict(launch.env),
        env_from_files=dict(launch.env_from_files),
        stdin_files=tuple(launch.stdin_files),
        stdin_text=PROBE_PROMPT if launch.stdin_text else "",
        identity_text=f"# Probe\n\n{PROBE_PROMPT}\n",
        timeout_seconds=ctx.probe_timeout_seconds,
        policy={
            "resources": {"cpus": 2, "memory": "3GiB", "pids": 1024, "tmpfs_total": "1GiB"},
            "network": {"mode": "egress-proxy", "egress_allowlist": []},
            "images": {"allowlist": [image]},
        },
        endpoint=endpoint,
        endpoint_url=endpoint_url,
    )
    started = time.monotonic()
    try:
        result: ProbeResult = await provider.probe_credential(request)
    except ProviderError as exc:
        # The run never reached the provider, so it observed nothing about the
        # credential. That is an inconclusive probe with a cause, not a failed one.
        return _record_inconclusive(
            ctx,
            uow,
            harness=harness,
            principal=principal,
            reason=reason,
            image=image,
            mode=mode,
            cause="provider_unavailable",
            detail=f"{type(exc).__name__}: {exc}",
            duration=time.monotonic() - started,
        )
    exit_class = adapter.classify_exit(
        ExitInfo(
            exit_code=result.exit_code,
            report_present=False,
            timed_out=result.timed_out,
            oom_killed=result.oom_killed,
        ),
        result.stdout_tail,
        result.stderr_tail,
    )
    # A probe asks for no report: exit 0 without one is the probe's success.
    if exit_class is ExitClass.COMPLETED_WITHOUT_REPORT:
        exit_class = ExitClass.COMPLETED
    sync = result.credential_sync
    changed = bool(sync and sync.changed)
    # Only two outcomes say anything about the credential itself: it worked, or the
    # provider refused it. Everything else is about the run.
    conclusive = exit_class in (ExitClass.COMPLETED, ExitClass.AUTH_FAILURE)
    record = ProbeRecord(
        harness=harness,
        exit_class=exit_class.value,
        exit_code=result.exit_code,
        harness_version=result.harness_version,
        image=image,
        image_digest=result.image_digest,
        auth_files_changed=changed,
        mount_mode=mode.value,
        duration_seconds=round(result.duration_seconds, 1),
        files=tuple(f.as_dict() for f in sync.files) if sync else (),
        detail=result.detail,
        conclusive=conclusive,
        cause="" if conclusive else exit_class.value,
    )
    now = ctx.clock.now()
    record_launch_outcome(
        uow,
        ctx.clock,
        name=harness,
        outcome=(
            f"probe:{exit_class.value}" if conclusive else f"probe:inconclusive:{record.cause}"
        ),
        at=now,
        auth_failure=exit_class is ExitClass.AUTH_FAILURE,
    )
    record_credential_observation(
        uow, ctx.clock, name=harness, mount_mode=MountMode(mode.value), changed=changed, at=now
    )
    record_event_probe(uow, ctx, principal, record, reason)
    return record


MODELS_BODY_LIMIT = 1024 * 1024


def _http_get(url: str, *, bearer: str | None, timeout: float) -> tuple[int, bytes]:
    """One GET that never follows a redirect (the bearer must not travel to another
    host) and reads at most MODELS_BODY_LIMIT bytes of the answer."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str,
        ) -> None:
            return None

    headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return int(response.status), response.read(MODELS_BODY_LIMIT)
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""


def _http_status(url: str, *, bearer: str | None, timeout: float) -> int:
    return _http_get(url, bearer=bearer, timeout=timeout)[0]


def model_ids(body: bytes) -> list[str]:
    """The model ids of an OpenAI-compatible `/models` answer, `{"data": [{"id": ...}]}`,
    in the order the gateway lists them. Anything else is no models."""
    try:
        document = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return []
    items = document.get("data") if isinstance(document, dict) else None
    if not isinstance(items, list):
        return []
    ids = [str(item["id"]) for item in items if isinstance(item, dict) and item.get("id")]
    return list(dict.fromkeys(ids))


async def _hermes_probe_async(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    harness: str,
    principal: str,
    reason: str,
    audit_event: bool = True,
) -> ProbeRecord:
    """Probe LiteLLM without running a model or exposing its bearer in process output.

    The detail says in plain words what the probe proved and names the URL it used
    (crucible#119): a `completed` probe is a gateway that answered its readiness check
    and listed its models for the stored key."""
    bearer = await asyncio.to_thread(read_api_key, ctx, harness)
    endpoint, _source = gateway_url(uow)
    started = time.monotonic()
    readiness_status: int | None = None
    models_status: int | None = None
    if not isinstance(endpoint, str) or not endpoint:
        exit_class = ExitClass.ENVIRONMENT
        conclusive = False
        cause = "endpoint_not_configured"
        detail = "The gateway URL is not set, so nothing was tested. Set it on Local gateway."
    elif bearer is None:
        exit_class = ExitClass.ENVIRONMENT
        conclusive = False
        cause = "key_not_set"
        detail = f"No Hermes key is stored, so gateway {endpoint} was not tested."
    else:
        parsed = urlsplit(endpoint)
        readiness = urlunsplit((parsed.scheme, parsed.netloc, "/health/readiness", "", ""))
        models = endpoint.rstrip("/") + "/models"
        try:
            readiness_status, _ = await asyncio.to_thread(
                _http_get, readiness, bearer=None, timeout=float(ctx.probe_timeout_seconds)
            )
            if readiness_status != 200:
                exit_class = ExitClass.ENVIRONMENT
                conclusive = False
                cause = f"readiness_http_{readiness_status}"
                detail = (
                    f"Gateway {endpoint} answered its readiness check with HTTP "
                    f"{readiness_status}; the key was not tested."
                )
            else:
                models_status, body = await asyncio.to_thread(
                    _http_get,
                    models,
                    bearer=bearer,
                    timeout=float(ctx.probe_timeout_seconds),
                )
                exit_class = (
                    ExitClass.COMPLETED
                    if models_status == 200
                    else ExitClass.AUTH_FAILURE
                    if models_status == 401
                    else ExitClass.ENVIRONMENT
                )
                conclusive = models_status in (200, 401)
                cause = "" if conclusive else f"models_http_{models_status}"
                if models_status == 200:
                    count = len(model_ids(body))
                    detail = (
                        f"Gateway {endpoint} reachable, key accepted, "
                        f"{count} model{'' if count == 1 else 's'}."
                    )
                elif models_status == 401:
                    detail = f"Gateway {endpoint} reachable, but it refused the key (HTTP 401)."
                else:
                    detail = (
                        f"Gateway {endpoint} reachable, but listing its models answered "
                        f"HTTP {models_status}."
                    )
        except (OSError, urllib.error.URLError, UnicodeError) as exc:
            exit_class = ExitClass.ENVIRONMENT
            conclusive = False
            cause = "endpoint_unreachable"
            detail = f"Gateway {endpoint} could not be reached ({type(exc).__name__})."
    record = ProbeRecord(
        harness=harness,
        exit_class=exit_class.value,
        exit_code=0 if exit_class is ExitClass.COMPLETED else 1,
        harness_version=None,
        image="direct-http",
        image_digest="",
        auth_files_changed=False,
        mount_mode=MountMode.RO.value,
        duration_seconds=round(time.monotonic() - started, 1),
        detail=detail,
        conclusive=conclusive,
        cause=cause,
    )
    now = ctx.clock.now()
    record_launch_outcome(
        uow,
        ctx.clock,
        name=harness,
        outcome=(f"probe:{exit_class.value}" if conclusive else f"probe:inconclusive:{cause}"),
        at=now,
        auth_failure=exit_class is ExitClass.AUTH_FAILURE,
    )
    record_credential_observation(
        uow, ctx.clock, name=harness, mount_mode=MountMode.RO, changed=False, at=now
    )
    if audit_event:
        record_event_probe(uow, ctx, principal, record, reason)
    return record


def record_event_probe(
    uow: UnitOfWork, ctx: AdminContext, principal: str, record: ProbeRecord, reason: str
) -> None:
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_PROBED,
        principal=principal,
        reason=reason,
        before=None,
        after=None,
        **record.as_dict(),
    )


def _routing_document(uow: UnitOfWork) -> tuple[dict[str, Any] | None, str]:
    """The one routing policy the policy in force names, and where it came from.

    Only that one. The earlier form appended the seeded `default-routing` versions 2 and 1
    after it, so a policy in force whose routing policy had no enabled model for a harness
    fell through to an older seeded policy and the probe ran a model the operator had
    disabled or removed. A routing policy that is not the one in force is not a fallback;
    it is a policy the operator superseded."""
    versions = [p for p in uow.policies.list_versions("default-software") if p.retired_at is None]
    if not versions:
        return None, "no default-software policy is in force"
    newest = max(versions, key=lambda p: p.version)
    routing = newest.document.get("routing") or {}
    ref = routing.get("policy") if isinstance(routing, dict) else None
    if not (isinstance(ref, dict) and ref.get("name") and ref.get("version") is not None):
        return None, f"default-software version {newest.version} names no routing policy"
    name = str(ref["name"])
    try:
        version = int(ref["version"])
    except (TypeError, ValueError):
        return None, (
            f"default-software version {newest.version} names routing policy {name} with a "
            "version that is not a number"
        )
    record = uow.routing_policies.get(name, version)
    if record is None or record.retired_at is not None:
        # A retired routing policy is one the operator took out of service, which is one
        # of the two ways they retire a model; it is not in force either.
        return None, (
            f"routing policy {name} version {version}, which default-software version "
            f"{newest.version} names, is " + ("retired" if record is not None else "not stored")
        )
    return record.document, f"{name} version {version}"


def probe_route(
    uow: UnitOfWork, adapter: HarnessAdapter, harness: str
) -> tuple[str, Literal["subscription", "local"], str | None]:
    """The model a probe or harness test runs, and where it is served: the cheapest
    enabled model the routing policy in force names for the harness, so a probe never
    runs on a frontier model and never on a model the operator retired. A model behind
    a local endpoint brings its URL, so the worker reaches it as a task would.

    A harness without a model flag (the script harness) needs a model only when the
    policy routes it to a local endpoint, which is then the model it calls; otherwise
    none. For every other harness a routing policy without an enabled model for it is a
    refusal: the CLIs reject an unknown model name, so guessing one would only produce
    a crash that says nothing about the credential (AGY did exactly that, C5b live
    run), and reaching past the policy in force to an older one would run a model the
    operator disabled."""
    needs_model = adapter.capabilities().model_flag
    document, where = _routing_document(uow)
    if document is None:
        if not needs_model:
            return "none", "subscription", None
        raise CredentialAdminError(
            f"the probe needs the routing policy the policy in force names, and {where}; "
            "put a policy in force that names a stored routing policy"
        )
    order = {"small": 0, "mid": 1, "frontier": 2}
    # A subscription model before a local one: a local endpoint does not read the
    # harness's own login, so a probe routed there would call a credential validated
    # without using it. Hermes has only local models, which read its key; the script
    # harness has no credential, and a local model is what gives it a model to call.
    local_first = not needs_model
    best: tuple[tuple[int, int], dict[str, Any]] | None = None
    for model in document.get("models", []):
        if model.get("harness") == harness and model.get("enabled"):
            is_local = model.get("endpoint") == "local"
            rank = (
                0 if is_local == local_first else 1,
                order.get(str(model.get("capability")), 3),
            )
            if best is None or rank < best[0]:
                best = (rank, model)
    if best is None:
        if not needs_model:
            return "none", "subscription", None
        raise CredentialAdminError(
            f"routing policy {where}, which the policy in force names, has no enabled "
            f"model for harness {harness!r}; the probe needs one (enable a model for it, "
            "or put a policy in force that names a routing policy with one). The probe "
            "does not fall back to an older routing policy, because that would run a "
            "model the operator disabled or removed"
        )
    model = best[1]
    if model.get("endpoint") == "local" and model.get("endpoint_url"):
        return str(model["id"]), "local", str(model["endpoint_url"])
    if not needs_model:
        return "none", "subscription", None
    return str(model["id"]), "subscription", None


async def worker_probe(
    ctx: AdminContext, uow: UnitOfWork, *, harness: str, principal: str, reason: str
) -> ProbeRecord:
    """The bounded probe in a worker for every harness, Hermes included: the harness
    test's run (crucible#118). The caller has applied the guard."""
    return await _probe_async(
        ctx, uow, harness=harness, principal=principal, reason=reason, in_worker=True
    )


async def probe(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, harness: str, reason: str | None
) -> CredentialReport:
    """25: the bounded probe on its own, 120 s, records exit class, harness version,
    image digest and whether the auth files changed; removes everything."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials probe {harness}"
    )
    spec_for(ctx, harness)
    if secret_store(ctx) is None:
        source_for(ctx, harness)
    record = await _probe_async(ctx, uow, harness=harness, principal=principal, reason=reason)
    return CredentialReport(harness, state_view(ctx, uow, harness), probe=record)


# ----- rotate, remove, shred -------------------------------------------------


def shred_file(path: Path) -> int:
    """Overwrite a regular file with zeros in place, fsync, unlink. Returns bytes."""
    size = path.stat().st_size
    with open(path, "r+b") as handle:
        remaining = size
        while remaining > 0:
            chunk = min(SHRED_CHUNK, remaining)
            handle.write(b"\0" * chunk)
            remaining -= chunk
        handle.flush()
        os.fsync(handle.fileno())
    path.unlink()
    return size


class ShredIncompleteError(CredentialAdminError):
    """A shred that did not remove everything. It is never a success: the operator asked
    for a credential to be gone and part of it is still on disk."""

    slug = "shred-incomplete"
    title = "The credential was not fully shredded"


def _remove_entry(path: Path) -> int | None:
    """One entry, whatever it is. A symlink, socket, fifo or device node holds no bytes
    of its own, so it is unlinked and not counted as a file shredded; only a regular file
    is overwritten first."""
    if path.is_symlink() or not path.is_file():
        path.unlink()
        return None
    return shred_file(path)


def _shred_pass(root: Path) -> tuple[int, int, list[str]]:
    """One bottom-up pass. Nothing here aborts the walk: an entry that cannot be removed
    is recorded and the rest of the tree is still shredded, because stopping at the first
    failure is what left an auth file in place behind a directory that would not go."""
    files = 0
    total = 0
    failed: list[str] = []
    walker = os.walk(
        root, topdown=False, onerror=lambda exc: failed.append(str(getattr(exc, "filename", root)))
    )
    for dirpath, dirnames, filenames in walker:
        here = Path(dirpath)
        for name in filenames:
            try:
                size = _remove_entry(here / name)
            except OSError:
                failed.append(str(here / name))
                continue
            if size is not None:
                total += size
                files += 1
        for name in dirnames:
            child = here / name
            try:
                if child.is_symlink():
                    child.unlink()
                else:
                    child.rmdir()
            except OSError:
                failed.append(str(child))
    return files, total, failed


def shred_tree(root: Path, *, keep_root: bool) -> dict[str, int]:
    """Shred every file under root and remove every subdirectory; the root stays when
    asked (the configured path is what the next login points at).

    Two passes, because a file the harness CLI writes between the listing and the parent
    directory's removal is a benign race the second pass absorbs. Anything still there
    after that is a refusal, not a rounded-down success: the previous walk was ordered
    only by depth and stopped at the first directory that would not go, which left files
    queued behind it on disk while the caller was told the shred had worked.
    """
    files = 0
    total = 0
    remaining: list[str] = []
    for _ in range(2):
        pass_files, pass_bytes, remaining = _shred_pass(root)
        files += pass_files
        total += pass_bytes
        if not remaining:
            break
    if not remaining and not keep_root:
        try:
            root.rmdir()
        except OSError:
            remaining = [str(root)]
    if remaining:
        raise ShredIncompleteError(
            f"{len(remaining)} entries under {root} could not be removed, so the "
            f"credential is still partly on disk: {sorted(set(remaining))[:5]}"
        )
    return {"files": files, "bytes": total}


def rotate(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    new_path: str,
    reason: str | None,
) -> CredentialReport:
    """25: a new directory is prepared and validated first; the swap is an atomic
    rename; the previous directory is retained beside it for `credential_retention_hours`
    and then shredded; every step an event.

    `new_path` is an operator-typed path that Crucible does not own, so rotate copies it
    and leaves it exactly as it found it. The only thing rotate ever destroys is inside
    the configured credential root, and even that goes on the retention schedule rather
    than now. Disposing of the source is the operator's to do, and the response and the
    event both say it was left."""
    reason = guard_mutation(
        ctx,
        uow,
        reason,
        principal=principal,
        operation=f"credentials rotate {harness}",
        needs_supervisor=True,
    )
    _refuse_for_secret(ctx, harness, "rotate")
    spec = spec_for(ctx, harness)
    source = source_for(ctx, harness)
    incoming = Path(new_path)
    if not incoming.is_dir():
        raise ContractValidationError(
            "the new credential directory does not exist",
            errors=[{"path": "new_path", "message": f"{new_path} is not a directory"}],
        )
    shape = check_shape(spec, str(incoming))
    if not shape.ok:
        raise ContractValidationError(
            "the new credential directory failed the shape check",
            errors=[{"path": "new_path", "message": p} for p in shape.problems],
        )
    current = Path(source.path)
    before = state_view(ctx, uow, harness)
    stamp = ctx.clock.now().strftime("%Y%m%dT%H%M%SZ")
    staged = current.with_name(current.name + INCOMING_MARK + stamp)
    if incoming.resolve() == current.resolve():
        raise CredentialAdminError("the new directory is the configured directory itself")
    # Stage beside the target so the final step is one rename on one filesystem. A copy
    # or a chmod that fails leaves a full copy of the credential at `<path>.incoming-`,
    # which no retention sweep matches, so it is shredded here instead of living on. The
    # staging directory is created exclusively first, so what is shredded is only ever
    # what this call made: the stamp is one second wide, and shredding a name that was
    # already there would destroy another rotate's copy.
    try:
        os.mkdir(staged, 0o700)
    except FileExistsError as exc:
        raise CredentialAdminError(
            f"a staging directory from an earlier rotate is already at {staged.name}; "
            "leave it for the operator to look at rather than overwriting it"
        ) from exc
    try:
        shutil.copytree(incoming, staged, symlinks=False, dirs_exist_ok=True)
        _tighten(staged)
    except Exception:
        with contextlib.suppress(Exception):
            if staged.is_dir():
                shred_tree(staged, keep_root=False)
        raise
    retired = current.with_name(current.name + RETIRED_MARK + stamp)
    _swap(ctx, current, retired, staged, principal=principal, harness=harness)
    state = uow.harnesses.get(harness)
    if state is not None:
        state.last_validated_at = None
        state.last_auth_failure_at = None
        state.updated_at = ctx.clock.now()
        uow.harnesses.put(state)
    after = state_view(ctx, uow, harness)
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_ROTATED,
        principal=principal,
        reason=reason,
        before=before,
        after=after,
        harness=harness,
        retained_as=retired.name if retired.exists() else None,
        retention_hours=ctx.credential_retention_hours,
        source_kept=str(incoming),
        shape=shape.as_dict(),
    )
    return CredentialReport(
        harness,
        after,
        shape=shape,
        extra={
            "retained_as": retired.name if retired.exists() else None,
            # The caller's directory is untouched and still holds the credential: the
            # operator disposes of it, because Crucible does not own that path.
            "source_kept": str(incoming),
        },
    )


def _swap(
    ctx: AdminContext,
    current: Path,
    retired: Path,
    staged: Path,
    *,
    principal: str,
    harness: str,
) -> None:
    """The two renames as one step. If the second fails the configured path would be
    gone, so the first is undone and the staged copy shredded: a failed rotate leaves
    exactly what it found. The failure is recorded through a unit of work of its own,
    because the caller's transaction is about to roll back with the exception."""
    moved = False
    if current.exists():
        os.rename(current, retired)
        moved = True
    try:
        os.rename(staged, current)
    except OSError as exc:
        if moved and not current.exists():
            os.rename(retired, current)
        with contextlib.suppress(OSError):
            if staged.is_dir():
                shred_tree(staged, keep_root=False)
        record_refusal(
            ctx,
            principal=principal,
            operation=f"credentials rotate {harness}",
            detail=f"the swap failed and was rolled back: {type(exc).__name__}",
        )
        raise CredentialAdminError(
            f"the credential swap failed ({type(exc).__name__}) and was rolled back; "
            "the configured directory is unchanged"
        ) from exc


def _refuse_for_secret(ctx: AdminContext, harness: str, verb: str) -> None:
    """Rotate and remove move and shred directories. Where the credential is a Secret
    the service owns (ADR 0015) there is no directory, and a login with `replace` is
    how it is replaced; this says so rather than asking for a path nobody configured."""
    store = secret_store(ctx)
    if store is None:
        return
    raise CredentialAdminError(
        f"{verb} works on a credential directory, and on this deployment the {harness} "
        f"credential is the Secret {store.credential_secret(harness)} that Crucible owns; "
        "run the login with replace to put a new one in its place"
    )


def _tighten(root: Path) -> None:
    os.chmod(root, 0o700)
    for path in root.rglob("*"):
        if path.is_dir():
            os.chmod(path, 0o700)
        elif path.is_file():
            os.chmod(path, 0o600)


def remove(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, harness: str, reason: str | None
) -> CredentialReport:
    """25: the harness becomes `absent`; the files are shredded; the harness is
    disabled with the reason so a launch is refused cleanly rather than failing auth."""
    reason = guard_mutation(
        ctx,
        uow,
        reason,
        principal=principal,
        operation=f"credentials remove {harness}",
        reason_required=True,
        needs_supervisor=True,
    )
    _refuse_for_secret(ctx, harness, "remove")
    spec_for(ctx, harness)
    source = source_for(ctx, harness)
    before = state_view(ctx, uow, harness)
    root = Path(source.path)
    shredded = shred_tree(root, keep_root=True) if root.is_dir() else {"files": 0, "bytes": 0}
    set_harness_enabled(
        uow,
        ctx.clock,
        principal_name=principal,
        name=harness,
        enabled=False,
        reason=f"credential removed: {reason}",
    )
    state = uow.harnesses.get(harness)
    if state is not None:
        state.last_validated_at = None
        state.updated_at = ctx.clock.now()
        uow.harnesses.put(state)
    after = state_view(ctx, uow, harness)
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_REMOVED,
        principal=principal,
        reason=reason,
        before=before,
        after=after,
        harness=harness,
        shredded=shredded,
    )
    return CredentialReport(harness, after, extra={"shredded": shredded})


def sweep_retired(ctx: AdminContext, uow: UnitOfWork, *, principal: str = "crucible") -> int:
    """Shred every retired directory older than the retention window (25). Called by
    the supervisor's retention step; idempotent."""
    cutoff = ctx.clock.now() - timedelta(hours=ctx.credential_retention_hours)
    shredded = 0
    for harness, source in ctx.credential_sources.items():
        current = Path(source.path)
        parent = current.parent
        if not parent.is_dir():
            continue
        for candidate in parent.iterdir():
            if not candidate.name.startswith(current.name + RETIRED_MARK):
                continue
            stamp = candidate.name[len(current.name) + len(RETIRED_MARK) :]
            try:
                retired_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=cutoff.tzinfo
                )
            except ValueError:
                continue
            if retired_at > cutoff:
                continue
            try:
                result = shred_tree(candidate, keep_root=False)
            except ShredIncompleteError as exc:
                # One directory that will not go never stops the sweep reaching the rest,
                # and it is recorded rather than retried silently on the next tick.
                record_refusal(
                    ctx,
                    principal=principal,
                    operation="credential retention sweep",
                    detail=str(exc.detail),
                )
                continue
            shredded += 1
            admin_event(
                uow,
                ctx,
                EventKind.CREDENTIAL_RETIRED_SHREDDED,
                principal=principal,
                reason=f"retention window of {ctx.credential_retention_hours} h elapsed",
                before={"retained": candidate.name},
                after={"retained": None},
                harness=harness,
                shredded=result,
            )
    return shredded
