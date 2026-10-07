"""What every administrative service needs, and the rules they all obey (25): a mutation
is an event with the principal and a before-and-after summary that never carries a
value, and the few that hand the supervisor work need a live supervisor lease. A reason
is an optional audit note, required only for the destructive or hard-to-reverse
operations (the operator's decision of 2026-09-25, crucible#117)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from crucible.application.errors import ContractValidationError, SupervisorNotLiveError
from crucible.application.harnesses import HarnessRegistry
from crucible.application.queries import supervisor_health
from crucible.application.transitions import record_event
from crucible.domain.entities import Event
from crucible.domain.events import EventKind
from crucible.domain.role_timeouts import DEFAULT_ROLE_TIMEOUT_SECONDS
from crucible.domain.secrets import scan_text
from crucible.ports.clock import Clock
from crucible.ports.execution import ExecutionProvider
from crucible.ports.first_run import FirstRunDelivery
from crucible.ports.github import GitHubAppCredentials, GitHubAppDirectory, GitHubClient
from crucible.ports.harness import CredentialSource, HarnessGate
from crucible.ports.repository import UnitOfWork, UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class GitHubAppInfo:
    """The App's public identity and where its files are (12). Paths, never values."""

    app_id: int = 0
    private_key_path: str | None = None
    webhook_secret_path: str | None = None
    webhook_enabled: bool = False
    api_base: str = "https://api.github.com"


@dataclass(slots=True)
class ProviderStatusCache:
    """Snapshot written by the supervisor and shared with API processes through the database."""

    images: list[tuple[str, Any]] = field(default_factory=list)
    providers: list[dict[str, Any]] = field(default_factory=list)
    refreshed_at: float | None = None

    def due(self, ttl_seconds: float) -> bool:
        return self.refreshed_at is None or time.monotonic() - self.refreshed_at >= ttl_seconds


@dataclass(slots=True)
class BackgroundRuns:
    """The background runs of this process, one per key (issue 147: the harness test).

    A run is a daemon thread and the marker it was started with. `running` says whether
    the key's thread is still alive, so a second start while one runs can hand back the
    first run's marker instead of starting a duplicate. The check sees this process only;
    what every api replica sees is what the run stores (the harness row's `last_test`)."""

    guard: threading.Lock = field(default_factory=threading.Lock)
    _threads: dict[str, threading.Thread] = field(default_factory=dict)
    _markers: dict[str, dict[str, Any]] = field(default_factory=dict)

    def running(self, key: str) -> dict[str, Any] | None:
        """The marker of the run in progress for `key`, or None when none is."""
        thread = self._threads.get(key)
        if thread is None or not thread.is_alive():
            return None
        return self._markers.get(key)

    def start(self, key: str, marker: dict[str, Any], target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, daemon=True, name=f"background-{key}")
        self._threads[key] = thread
        self._markers[key] = marker
        thread.start()

    def wait(self, key: str, timeout: float | None = None) -> bool:
        """Wait for the key's run to end; True when it has (or none was running)."""
        thread = self._threads.get(key)
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()


@dataclass(slots=True)
class AdminContext:
    uow_factory: UnitOfWorkFactory
    clock: Clock
    providers: dict[str, ExecutionProvider]
    harnesses: HarnessRegistry
    harness_gates: Mapping[str, HarnessGate] = field(default_factory=dict)
    credential_sources: dict[str, CredentialSource] = field(default_factory=dict)
    github: GitHubClient | None = None
    github_app: GitHubAppInfo = field(default_factory=GitHubAppInfo)
    # The App credential the service owns and what the App can see (ADR 0017,
    # crucible#120). None where there is nowhere to keep one.
    github_credentials: GitHubAppCredentials | None = None
    github_apps: GitHubAppDirectory | None = None
    artifact_root: str = ""
    lease_ttl_seconds: int = 30
    credential_retention_hours: int = 24
    probe_timeout_seconds: int = 120
    login_timeout_seconds: int = 900
    status_cache_ttl_seconds: float = 60.0
    status_cache_shared: bool = False
    status_cache_enabled: bool = False
    status_cache: ProviderStatusCache = field(default_factory=ProviderStatusCache)
    proxy_config_path: str | None = None
    proxy_subnet: str = "10.88.0.0/24"
    proxy_hosts: tuple[str, ...] = ()
    proxy_reload_timeout_seconds: float = 0
    # The command each harness's login runs, overridable for the fake-CLI tests.
    login_commands: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Login flows beyond the three harnesses' (`login.FLOWS`), by harness name: the
    # stand-in login the end-to-end tiers drive. Never set by a deployment.
    login_flows: dict[str, Any] = field(default_factory=dict)
    # The settings file's `kubernetes.egress` values, shown until a save replaces them,
    # and the namespaces no selector may name (crucible#91).
    kubernetes_egress_seed: dict[str, Any] = field(default_factory=dict)
    kubernetes_protected_namespaces: tuple[str, ...] = ()
    # The settings file's `kubernetes.role_timeout_seconds`, shown until a save of the
    # `kubernetes.timeouts` setting replaces it.
    kubernetes_role_timeout_seed: int = DEFAULT_ROLE_TIMEOUT_SECONDS
    # Where the first-run administrator token was delivered; a revoke of that principal
    # removes it (ADR 0016).
    first_run: FirstRunDelivery | None = None
    # The harness tests running in this process (issue 147), one per harness.
    harness_tests: BackgroundRuns = field(default_factory=BackgroundRuns)


def record_refusal(ctx: AdminContext, *, principal: str, operation: str, detail: str) -> None:
    """25: "the refusal itself is recorded, so an audit shows the attempt". The refusal
    ends the caller's transaction, so it is written through a unit of work of its own and
    committed there. Best effort: a refusal that cannot be recorded never becomes a
    second failure on top of the first."""
    if not operation:
        return
    try:
        with ctx.uow_factory() as uow:
            record_event(
                uow,
                ctx.clock,
                EventKind.ADMIN_REFUSED,
                principal=principal or "unknown",
                payload={"operation": operation, "detail": detail},
            )
            uow.commit()
    except Exception:  # a refusal is never made worse by a failure to record it
        return


def refuse_secret_shaped(value: str, *, field: str) -> None:
    """25: no event payload ever carries a credential value, and the audit serves payloads
    back. A secret-shaped input is refused rather than redacted, so the operator knows the
    value did not land anywhere."""
    hit = scan_text(value)
    if hit is not None:
        raise ContractValidationError(
            f"the {field} looks like a credential and was not recorded",
            errors=[
                {
                    "path": field,
                    "message": (
                        f"matched the {hit} pattern; an administrative record never "
                        "carries a credential value (25)"
                    ),
                }
            ],
        )


def require_reason(
    reason: str | None,
    ctx: AdminContext | None = None,
    *,
    principal: str = "",
    operation: str = "",
    required: bool = True,
) -> str:
    """The reason an operator gave, stripped; "" when none was given and none is
    required. A destructive or hard-to-reverse operation requires one (25). Whatever is
    given is recorded, so it may not be a credential."""
    if reason is None or not reason.strip():
        if not required:
            return ""
        if ctx is not None:
            record_refusal(
                ctx, principal=principal, operation=operation, detail="no reason was given"
            )
        raise ContractValidationError(
            f"a reason is required for {operation}" if operation else "a reason is required",
            errors=[{"path": "reason", "message": "must not be empty"}],
        )
    cleaned = reason.strip()
    try:
        refuse_secret_shaped(cleaned, field="reason")
    except ContractValidationError:
        if ctx is not None:
            record_refusal(
                ctx,
                principal=principal,
                operation=operation,
                detail="the reason was secret-shaped",
            )
        raise
    return cleaned


def require_live_supervisor(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str = "", operation: str = ""
) -> None:
    """25: a mutation is refused when the supervisor lease is not held by a live
    instance. The refusal itself is recorded, so an audit shows the attempt."""
    ok, detail = supervisor_health(uow, ctx.clock.now(), ctx.lease_ttl_seconds)
    if not ok:
        record_refusal(ctx, principal=principal, operation=operation, detail=detail)
        raise SupervisorNotLiveError(f"refused: {detail}")


def guard_mutation(
    ctx: AdminContext,
    uow: UnitOfWork,
    reason: str | None,
    *,
    principal: str,
    operation: str,
    reason_required: bool = False,
    needs_supervisor: bool = False,
) -> str:
    """The rules every mutation obeys (25), in one call so no operation can skip one: a
    reason that is not a credential, required when `reason_required` (bootstrap commit,
    repository remove, token revoke, credential remove), and, when `needs_supervisor`,
    a live supervisor lease. Either refusal is recorded as `admin_refused`.

    Only an operation that hands the supervisor work or takes away something a running
    worker may be using waits for a live supervisor: committing a bootstrap import, and
    rotating or removing a credential. A configuration write (a token, a repository, the
    gateway, the GitHub App, an image promotion, a setting) proceeds while the
    supervisor misses a tick, and the supervisor reads it when it next runs (the
    operator's direction of 2026-09-29)."""
    cleaned = require_reason(
        reason, ctx, principal=principal, operation=operation, required=reason_required
    )
    if needs_supervisor:
        require_live_supervisor(ctx, uow, principal=principal, operation=operation)
    return cleaned


def admin_event(
    uow: UnitOfWork,
    ctx: AdminContext,
    kind: EventKind,
    *,
    principal: str,
    reason: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    **extra: Any,
) -> Event:
    """One event per mutation: principal, reason, before-and-after summary (25). The
    whole assembled payload is scanned before it is written, because `GET /admin/audit`
    serves it back and the event log is append-only."""
    payload: dict[str, Any] = {
        "reason": reason,
        "before": dict(before or {}),
        "after": dict(after or {}),
    }
    payload.update(extra)
    refuse_secret_shaped(json.dumps(payload, default=str), field="payload")
    return record_event(uow, ctx.clock, kind, principal=principal, payload=payload)
