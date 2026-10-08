"""Compose the application from settings. Used by both CLIs."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.execution.k8sapi import (
    KubernetesApiError,
    KubernetesClient,
    in_cluster_access,
    kubeconfig_access,
)
from crucible.adapters.execution.k8spublisher import (
    ByWorkspacePublisher,
    KubernetesPublisher,
    KubernetesPublisherConfig,
)
from crucible.adapters.execution.k8sregistry import CraneRegistryClient
from crucible.adapters.execution.kubernetes import (
    KubernetesConfig,
    KubernetesProvider,
    SettingsSource,
    TimeoutsSource,
)
from crucible.adapters.execution.publisher import DockerPublisher, PublisherConfig
from crucible.adapters.first_run import FILE_NAME, FileDelivery, SecretDelivery
from crucible.adapters.github.appauth import AppAuthenticator, AppConfig
from crucible.adapters.github.apps import RestGitHubApps
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.credentials import DirectoryAppCredentials, SecretAppCredentials
from crucible.adapters.github.transport import RestTransport
from crucible.adapters.harness.registry import default_registry
from crucible.adapters.notification.webhook import WebhookWakeDeliverer
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.admin.context import AdminContext, GitHubAppInfo
from crucible.application.admin.credentials import sweep_retired
from crucible.application.admin.routing import local_endpoint_view
from crucible.application.credential_renewer import (
    CodexCredentialRenewer,
    FileCredentialReader,
    FileCredentialStore,
    KubernetesCredentialReader,
    KubernetesCredentialStore,
    ReadOnlyCredentialStore,
)
from crucible.application.delivery_tick import DeliveryConfig
from crucible.application.errors import NotFoundError
from crucible.application.harnesses import HarnessRegistry, effective_mount_mode
from crucible.application.proxy_config import (
    enabled_local_endpoints,
    install_worker_proxy_config,
    worker_proxy_config,
)
from crucible.application.supervisor import Supervisor
from crucible.application.transitions import record_event
from crucible.application.wakes import create_wake
from crucible.contracts.wake import WakeReason
from crucible.domain.cluster_egress import SETTING_NAME, parse_cluster_egress
from crucible.domain.entities import Role
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.domain.role_timeouts import SETTING_NAME as ROLE_TIMEOUTS_SETTING
from crucible.ports.artifacts import ArtifactStore
from crucible.ports.execution import ExecutionProvider
from crucible.ports.first_run import FirstRunDelivery
from crucible.ports.github import GitHubAppCredentials, GitHubClient
from crucible.ports.harness import CredentialSource, HarnessGate, MountMode
from crucible.ports.notification import WakeDeliverer
from crucible.ports.publish import Publisher
from crucible.ports.repository import UnitOfWorkFactory
from crucible.settings import Settings

log = logging.getLogger("crucible.wiring")


@dataclass(slots=True)
class Wiring:
    settings: Settings
    ctx: AppContext
    providers: dict[str, ExecutionProvider]
    artifact_store: ArtifactStore
    wake_deliverer: WakeDeliverer
    github: GitHubClient | None = None
    publisher: Publisher | None = None
    harnesses: HarnessRegistry | None = None
    admin: AdminContext | None = None
    credential_renewer: CodexCredentialRenewer | None = None

    def supervisor(self) -> Supervisor:
        s = self.settings.supervisor
        return Supervisor(
            self.ctx.uow_factory,
            self.providers,
            self.ctx.clock,
            holder=f"{s.holder or socket.gethostname()}:{os.getpid()}:{new_id()[-6:]}",
            artifact_store=self.artifact_store,
            wake_deliverer=self.wake_deliverer,
            github=self.github,
            publisher=self.publisher,
            delivery_config=DeliveryConfig(
                poll_interval_seconds=self.settings.github.poll_interval_seconds,
                reactions_poll_interval_seconds=(
                    self.settings.github.reactions_poll_interval_seconds
                ),
                ci_log_excerpt_bytes=self.settings.github.ci_log_excerpt_bytes,
                publisher_timeout_seconds=self.settings.github.publisher_timeout_seconds,
                publisher_image=self.settings.github.publisher_image,
            ),
            lease_ttl_seconds=s.lease_ttl_seconds,
            attempt_lease_ttl_seconds=s.attempt_lease_ttl_seconds,
            checkout_lease_ttl_seconds=s.checkout_lease_ttl_seconds,
            grace_seconds=s.grace_seconds,
            collection_retry_ticks=s.collection_retry_ticks,
            harnesses=self.harnesses,
            harness_gates=harness_gates(self.settings),
            credential_sources=credential_sources(self.settings),
            credential_sweep=(
                partial(sweep_retired, self.admin) if self.admin is not None else None
            ),
            credential_renewal=(
                self._credential_renewal if self.credential_renewer is not None else None
            ),
            admin_context=self.admin,
        )

    def _credential_renewal(self) -> bool:
        """Both timer renewal and pending-request refresh (339)."""
        if self.credential_renewer is None:
            return False
        try:
            if self.credential_renewer.refresh_on_request():
                return True
            return self.credential_renewer.refresh_if_due()
        except Exception:
            # A transient request remains pending. Maintenance and the rest of the
            # supervisor tick must still run, even when the login is unavailable.
            log.exception("Codex credential renewal failed")
            return False

    def app(self) -> FastAPI:
        return create_app(self.ctx)


def credential_sources(settings: Settings) -> dict[str, CredentialSource]:
    """12: where each harness's credential directory is. Paths, never values."""
    out: dict[str, CredentialSource] = {}
    for name, entry in settings.credentials.items():
        if entry.path or entry.mount_mode:
            out[name] = CredentialSource(
                path=entry.path or "",
                mount_mode=MountMode(entry.mount_mode) if entry.mount_mode else None,
            )
    return out


def harness_gates(settings: Settings) -> dict[str, HarnessGate]:
    """25: the operator's configuration gate per harness, with its reason."""
    return {
        name: HarnessGate(enabled=entry.enabled, reason=entry.reason)
        for name, entry in settings.harnesses.items()
    }


def docker_config(
    settings: Settings, *, local_endpoint_url: str | None = None, database_value: bool = False
) -> DockerConfig:
    d = settings.docker
    local_endpoints = []
    endpoint = local_endpoint_url if database_value else settings.endpoint_seed
    if endpoint:
        parsed = urlsplit(endpoint)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        local_endpoints.append(f"{parsed.hostname}:{port}")
    return DockerConfig(
        endpoint=d.host,
        artifact_root=settings.service.artifact_root,
        mount_kind=d.mount_kind,
        artifact_volume=d.artifact_volume,
        artifact_host_root=d.artifact_host_root,
        credential_root=d.credential_root,
        credential_host_root=d.credential_host_root,
        credential_volume=d.credential_volume,
        workers_network=d.workers_network,
        egress_proxy=d.egress_proxy,
        proxy_allowlist=tuple([*d.egress_allowlist, *local_endpoints]),
        no_proxy=d.no_proxy,
        api_timeout_seconds=d.api_timeout_seconds,
        collector_timeout_seconds=d.collector_timeout_seconds,
        verifier_timeout_seconds=d.verifier_timeout_seconds,
        report_size_cap_bytes=d.report_size_cap_bytes,
        workspace_dir_mode=d.workspace_dir_mode,
        use_reference_cache=d.use_reference_cache,
        max_concurrency=d.max_concurrency,
        extra_image_allowlist=tuple(d.extra_image_allowlist),
        credentials=credential_sources(settings),
        credential_host=settings.github.credential_host,
    )


def kubernetes_protected_namespaces(settings: Settings) -> tuple[str, ...]:
    """The namespaces no egress selector may name: the workers' own and Crucible's."""
    k = settings.kubernetes
    return tuple(dict.fromkeys(n for n in (k.workers_namespace, k.namespace) if n))


def kubernetes_egress_seed(settings: Settings) -> dict[str, object]:
    """The settings file's `kubernetes.egress` values, as the admin setting's document."""
    k = settings.kubernetes
    return {
        "dns": {"namespace": k.dns_namespace, "pod_labels": dict(k.dns_pod_labels)},
        "local_endpoint": {
            "namespace": k.local_endpoint_namespace,
            "pod_labels": dict(k.local_endpoint_pod_labels),
            "port": k.local_endpoint_port,
        },
    }


def kubernetes_settings_source(factory: UnitOfWorkFactory) -> SettingsSource:
    """What the provider reads back on each refresh: the saved `kubernetes.egress`
    document, if any, and the enabled local endpoint of the routing policy in force."""

    def read() -> tuple[Mapping[str, object] | None, str | None]:
        with factory() as uow:
            row = uow.provider_settings.get(SETTING_NAME)
            endpoint: str | None = None
            try:
                reference = local_endpoint_view(uow)["routing_policy"]
            except NotFoundError:
                reference = None
            if reference is not None:
                record = uow.routing_policies.get(reference["name"], reference["version"])
                endpoint = enabled_database_endpoint(record.document if record else None)
        return (row.document if row is not None else None, endpoint)

    return read


def kubernetes_timeouts_source(factory: UnitOfWorkFactory) -> TimeoutsSource:
    """What the provider reads back on each refresh: the saved `kubernetes.timeouts`
    document, if any."""

    def read() -> Mapping[str, object] | None:
        with factory() as uow:
            row = uow.provider_settings.get(ROLE_TIMEOUTS_SETTING)
        return row.document if row is not None else None

    return read


def kubernetes_config(
    settings: Settings, *, local_endpoint_url: str | None = None
) -> KubernetesConfig:
    k = settings.kubernetes
    return KubernetesConfig(
        namespace=k.workers_namespace,
        service_account=k.service_account,
        storage_class=k.storage_class,
        workspace_size=k.workspace_size,
        image_pull_secret=k.image_pull_secret,
        cache_claim=k.cache_claim,
        launch_timeout_seconds=k.launch_timeout_seconds,
        prepare_timeout_seconds=k.prepare_timeout_seconds,
        preparer_stall_seconds=k.preparer_stall_seconds,
        prepare_pod_deletion_wait_seconds=k.prepare_pod_deletion_wait_seconds,
        collector_timeout_seconds=k.collector_timeout_seconds,
        verifier_timeout_seconds=k.verifier_timeout_seconds,
        role_timeout_seconds=k.role_timeout_seconds,
        report_size_cap_bytes=k.report_size_cap_bytes,
        max_concurrency=k.max_concurrency,
        short_role_pods=k.short_role_pods,
        poll_interval_seconds=k.poll_interval_seconds,
        api_timeout_seconds=k.api_timeout_seconds,
        cluster_dns_ip=k.cluster_dns_ip,
        # A bad seed is refused here (wire() then leaves the provider out) rather than
        # rendering a rule nobody meant; the same check refuses it through the admin
        # surfaces.
        egress=parse_cluster_egress(
            kubernetes_egress_seed(settings),
            protected_namespaces=kubernetes_protected_namespaces(settings),
        ),
        control_namespace=k.namespace,
        local_endpoint_url=local_endpoint_url or "",
        denied_cidrs=tuple(k.denied_cidrs),
        local_endpoint_cidrs=tuple(k.local_endpoint_cidrs),
        broad_egress=k.broad_egress,
        resolve_ttl_seconds=k.resolve_ttl_seconds,
        extra_image_allowlist=tuple(k.extra_image_allowlist),
        credential_secrets=dict(k.credential_secrets),
        # 25 step 7: a configured mount mode may raise the adapter's declared minimum
        # to rw-narrow and never lowers it. The Kubernetes provider reads the same
        # `[credentials.<harness>]` block the Docker provider does; only the source
        # differs, a Secret in the workers namespace rather than a directory (12, 26).
        credential_modes={
            name: MountMode(entry.mount_mode)
            for name, entry in settings.credentials.items()
            if entry.mount_mode
        },
        image_repositories=tuple(k.image_repositories),
        probe_image=k.probe_image,
        use_reference_cache=k.use_reference_cache,
        credential_host=settings.github.credential_host,
        canary_cpu_millicores=k.canary_cpu_millicores,
        canary_memory=k.canary_memory,
        pod_pid_limit_override=k.pod_pid_limit_override,
    )


def kubernetes_provider(
    settings: Settings,
    registry: HarnessRegistry,
    *,
    factory: UnitOfWorkFactory | None = None,
    local_endpoint_url: str | None = None,
) -> KubernetesProvider:
    """The provider of 26. The access is either a kubeconfig path or the in-cluster
    ServiceAccount; neither is a credential value in configuration (12)."""
    k = settings.kubernetes
    access = (
        kubeconfig_access(k.kubeconfig, k.kubeconfig_context)
        if k.kubeconfig
        else in_cluster_access()
    )
    client = KubernetesClient(access, k.workers_namespace, timeout=k.api_timeout_seconds)
    return KubernetesProvider(
        kubernetes_config(settings, local_endpoint_url=local_endpoint_url),
        client,
        CraneRegistryClient(),
        harnesses=registry,
        settings_source=kubernetes_settings_source(factory) if factory is not None else None,
        timeouts_source=kubernetes_timeouts_source(factory) if factory is not None else None,
    )


def first_run_delivery(settings: Settings) -> FirstRunDelivery | None:
    """Where the first-run administrator token goes (ADR 0016, crucible#122).

    On Kubernetes, the Secret in the service namespace; on Docker, a file in the
    credential root. Anywhere else there is no private place Crucible knows of, and
    None means the migration mints no token at all rather than print one."""
    k = settings.kubernetes
    if k.enabled:
        try:
            access = (
                kubeconfig_access(k.kubeconfig, k.kubeconfig_context)
                if k.kubeconfig
                else in_cluster_access()
            )
        except (KubernetesApiError, OSError) as exc:
            log.error("the first-run token Secret is unreachable: %s", exc)
            return None
        return SecretDelivery(KubernetesClient(access, k.namespace, timeout=k.api_timeout_seconds))
    if settings.docker.credential_root:
        return FileDelivery(Path(settings.docker.credential_root) / FILE_NAME)
    return None


def github_credentials(settings: Settings) -> GitHubAppCredentials | None:
    """Where the App credential the service owns lives (ADR 0017): the Secret in the
    service's own namespace on Kubernetes, the files beside `private_key_path` with
    Docker, and nowhere when neither applies. The settings' App id and `enabled` still
    count for a credential a deployment placed there itself."""
    g = settings.github
    k = settings.kubernetes
    if k.enabled and not settings.docker.enabled:
        try:
            access = (
                kubeconfig_access(k.kubeconfig, k.kubeconfig_context)
                if k.kubeconfig
                else in_cluster_access()
            )
        except (KubernetesApiError, OSError, ValueError) as exc:
            log.error("the GitHub App Secret cannot be reached: %s", exc)
            return None
        return SecretAppCredentials(
            KubernetesClient(access, k.namespace, timeout=k.api_timeout_seconds),
            name=g.app.secret_name,
            settings_app_id=g.app.app_id,
            settings_enabled=g.enabled,
        )
    if g.app.private_key_path:
        return DirectoryAppCredentials(
            g.app.private_key_path,
            webhook_secret_path=g.app.webhook_secret_path,
            settings_app_id=g.app.app_id,
            settings_enabled=g.enabled,
        )
    return None


def report_github_credential(settings: Settings, store: GitHubAppCredentials | None) -> None:
    """crucible#79: a deployment that says GitHub is on but whose credential is missing
    finds out at startup, with the store named, not at the first delivery. A store the
    operator has not filled yet is not an error: Connect GitHub fills it."""
    if store is None or not settings.github.enabled:
        return
    try:
        described = store.describe()
    except Exception as exc:  # a startup report never stops the process
        log.error("the GitHub App credential could not be inspected: %s", exc)
        return
    where = (
        f"the Secret {described.get('name')} in {described.get('namespace')}"
        if described.get("kind") == "secret"
        else f"{settings.github.app.private_key_path}"
    )
    if described.get("exists") is None:
        log.error(
            "github.enabled is true but %s cannot be read: %s", where, described.get("detail")
        )
    elif not described.get("key_present"):
        log.error(
            "github.enabled is true but %s holds no app.pem; connect the App on the GitHub "
            "page or place the key there",
            where,
        )


def github_client(
    settings: Settings, credentials: GitHubAppCredentials | None = None
) -> tuple[RestGitHubClient | None, RestGitHubApps | None]:
    """The GitHub adapter and the App's own view, over one transport. The key is read
    from the store on each signature, in memory; nothing about it is a configuration
    value (12). With no store, the settings' file path is used when `github.enabled`
    names a complete App, as before ADR 0017."""
    g = settings.github
    transport = RestTransport(g.api_base, timeout=g.api_timeout_seconds)
    if credentials is None:
        if not g.enabled or not g.app.app_id or not g.app.private_key_path:
            return None, None
        authenticator = AppAuthenticator(
            AppConfig(
                app_id=g.app.app_id,
                private_key_path=g.app.private_key_path,
                api_base=g.api_base,
            ),
            transport,
        )
    else:
        authenticator = AppAuthenticator(
            AppConfig(app_id=0, private_key_path="", api_base=g.api_base),
            transport,
            credentials=credentials,
        )
    client = RestGitHubClient(authenticator, transport, allow_issue_comments=g.allow_issue_comments)
    return client, RestGitHubApps(authenticator, transport)


def enabled_database_endpoint(routing_document: Mapping[str, object] | None) -> str | None:
    """The one endpoint an enabled local model authorizes, or None.

    A disabled model gets no Squid rule (`proxy_config.enabled_local_endpoints`); the
    Docker provider's own allowlist, wired from this at startup, has to agree, or a
    restart after disabling every local model puts the destination back.
    """
    if routing_document is None:
        return None
    endpoints = enabled_local_endpoints([routing_document])
    return endpoints[0] if len(endpoints) == 1 else None


ProcessRole = Literal["api", "admin", "supervisor"]


def wire(settings: Settings, *, role: ProcessRole) -> Wiring:
    engine = make_engine(settings.database.url)
    factory = SqlUnitOfWorkFactory(engine)
    database_endpoint: str | None = None
    routing_document: dict[str, object] | None = None
    database_value = False
    try:
        with factory() as uow:
            local = local_endpoint_view(uow)
            database_value = True
            reference = local["routing_policy"]
            record = uow.routing_policies.get(reference["name"], reference["version"])
            routing_document = record.document if record is not None else None
            database_endpoint = enabled_database_endpoint(routing_document)
    except (SQLAlchemyError, NotFoundError):
        # `migrate` and first-run commands can wire before the policy tables exist.
        database_value = False
    if settings.admin.proxy_config_path and routing_document is not None:
        path = Path(settings.admin.proxy_config_path)
        install_worker_proxy_config(
            path,
            worker_proxy_config(
                settings.admin.proxy_subnet,
                list(settings.docker.egress_allowlist),
                [routing_document],
            ),
        )
    registry = default_registry(test_fixtures=settings.test_fixtures)
    # The fake provider runs nothing, so it is wired only for the test tiers (crucible#124).
    providers: dict[str, ExecutionProvider] = (
        {"fake": FakeProvider()} if settings.test_fixtures else {}
    )
    docker: DockerProvider | None = None
    if settings.docker.enabled:
        docker = DockerProvider(
            docker_config(
                settings,
                local_endpoint_url=database_endpoint,
                database_value=database_value,
            ),
            harnesses=registry,
        )
        providers["docker"] = docker
    if settings.kubernetes.enabled:
        # 26: the Kubernetes provider is reported by `GET /v1/capabilities` and
        # `GET /v1/admin/providers` exactly when it is wired, with its namespace probe
        # in its health checks. A deployment that turned it on with no kubeconfig and
        # no in-cluster ServiceAccount gets the provider left out and a log line, not a
        # service that will not start: the Docker provider and the API are still the
        # operator's way of finding out what is wrong (25).
        try:
            providers["kubernetes"] = kubernetes_provider(
                settings, registry, factory=factory, local_endpoint_url=database_endpoint
            )
        except KubernetesApiError as exc:
            log.error("the kubernetes provider is enabled but unreachable: %s", exc)
        except ValueError as exc:
            # A `kubernetes.egress` seed that names a protected namespace or an empty
            # selector: the provider is left out rather than rendering a rule nobody
            # meant, and the API stays up to say so.
            log.error("the kubernetes provider is enabled but its settings are refused: %s", exc)
    artifact_store = DiskArtifactStore(settings.service.artifact_root)
    wake_deliverer = WebhookWakeDeliverer(
        settings.wake.webhook_url,
        settings.wake.secret,
        timeout_seconds=settings.wake.timeout_seconds,
    )
    github_store = github_credentials(settings)
    github, github_apps = github_client(settings, github_store)
    report_github_credential(settings, github_store)
    first_run = first_run_delivery(settings)
    admin = AdminContext(
        uow_factory=factory,
        clock=SystemClock(),
        providers=providers,
        harnesses=registry,
        harness_gates=harness_gates(settings),
        credential_sources=credential_sources(settings),
        github=github,
        github_app=GitHubAppInfo(
            app_id=settings.github.app.app_id,
            private_key_path=settings.github.app.private_key_path,
            webhook_secret_path=settings.github.app.webhook_secret_path,
            webhook_enabled=settings.github.webhook_enabled,
            api_base=settings.github.api_base,
        ),
        github_credentials=github_store,
        github_apps=github_apps,
        artifact_root=settings.service.artifact_root,
        lease_ttl_seconds=settings.supervisor.lease_ttl_seconds,
        credential_retention_hours=settings.admin.credential_retention_hours,
        probe_timeout_seconds=settings.admin.probe_timeout_seconds,
        login_timeout_seconds=settings.admin.login_timeout_seconds,
        status_cache_ttl_seconds=settings.admin.status_cache_ttl_seconds,
        status_cache_enabled=role != "admin",
        status_cache_shared=role != "admin",
        login_commands={k: tuple(v) for k, v in settings.admin.login_commands.items()},
        proxy_config_path=settings.admin.proxy_config_path,
        proxy_subnet=settings.admin.proxy_subnet,
        proxy_hosts=tuple(settings.docker.egress_allowlist),
        proxy_reload_timeout_seconds=settings.admin.proxy_reload_timeout_seconds,
        kubernetes_egress_seed=kubernetes_egress_seed(settings),
        kubernetes_protected_namespaces=kubernetes_protected_namespaces(settings),
        kubernetes_role_timeout_seed=settings.kubernetes.role_timeout_seconds,
        first_run=first_run,
    )
    renewer = (
        build_credential_renewer(settings, providers, factory, admin)
        if role == "supervisor"
        else None
    )
    api_renewer = _build_readonly_renewer(settings, providers)
    kubernetes = providers.get("kubernetes")
    if api_renewer is not None and isinstance(kubernetes, KubernetesProvider):
        kubernetes.set_credential_dead_check(lambda: api_renewer.dead)

    ctx = AppContext(
        uow_factory=factory,
        clock=SystemClock(),
        providers=list(providers.values()),
        database_url=settings.database.url,
        engine=engine,
        artifact_store=artifact_store,
        lease_ttl_seconds=settings.supervisor.lease_ttl_seconds,
        github_webhook_enabled=settings.github.webhook_enabled,
        github_webhook_secret_path=settings.github.app.webhook_secret_path,
        github_client=github,
        harnesses=registry,
        harness_gates=harness_gates(settings),
        credential_sources=credential_sources(settings),
        admin=admin,
        settings=settings,
        first_run=first_run,
        credential_renewer=api_renewer,
    )
    publisher = build_publisher(settings, docker, providers.get("kubernetes"), github)
    return Wiring(
        settings=settings,
        ctx=ctx,
        providers=providers,
        artifact_store=artifact_store,
        wake_deliverer=wake_deliverer,
        github=github,
        publisher=publisher,
        harnesses=registry,
        admin=admin,
        credential_renewer=renewer,
    )


def build_credential_renewer(
    settings: Settings,
    providers: Mapping[str, ExecutionProvider],
    factory: UnitOfWorkFactory,
    admin: AdminContext,
) -> CodexCredentialRenewer | None:
    """Build the one Codex writer from its directory or service-held Secret."""
    source = credential_sources(settings).get("codex")
    spec = default_registry().require("codex").credential_spec()
    assert spec is not None
    if effective_mount_mode(spec, source) is not MountMode.RENEWER:
        return None
    store: FileCredentialStore | KubernetesCredentialStore | None = None
    if source is not None and source.path and (path := Path(source.path) / "auth.json").is_file():
        store = FileCredentialStore(path)
    elif (
        settings.kubernetes.enabled
        and not settings.docker.enabled
        and isinstance((kubernetes := providers.get("kubernetes")), KubernetesProvider)
    ):
        candidate = KubernetesCredentialStore(
            kubernetes.client, kubernetes.credential_secret("codex")
        )
        try:
            candidate.read()
        except KubernetesApiError as exc:
            if exc.status != 404:
                raise
        except ValueError:
            pass
        else:
            store = candidate
    if store is None:
        return None

    try:
        service_loop = asyncio.get_running_loop()
    except RuntimeError:
        service_loop = None  # The local administrative CLI has no service loop.

    def record_refresh(kind: EventKind, payload: Mapping[str, Any]) -> None:
        with factory() as uow:
            record_event(
                uow,
                admin.clock,
                kind,
                principal="credential-renewer",
                payload=dict(payload),
            )
            uow.commit()

    async def propagate(document: Mapping[str, str]) -> None:
        for provider in providers.values():
            update = getattr(provider, "refresh_credential_projection", None)
            if callable(update):
                await update(document)

    def propagate_refresh(document: Mapping[str, str]) -> None:
        if service_loop is None:
            asyncio.run(propagate(document))
        else:
            future = asyncio.run_coroutine_threadsafe(propagate(document), service_loop)
            future.add_done_callback(lambda completed: completed.result())

    def record_on_service_loop(kind: EventKind, payload: Mapping[str, Any]) -> None:
        if service_loop is None:
            record_refresh(kind, payload)
        else:
            service_loop.call_soon_threadsafe(record_refresh, kind, dict(payload))

    def wake(summary: str) -> None:
        with factory() as uow:
            principals = [p for p in uow.principals.list_all() if p.disabled_at is None]
            principal = next(
                (p for p in principals if p.role is Role.ORCHESTRATOR),
                next((p for p in principals if p.role is Role.ADMIN), None),
            )
            if principal is None:
                log.error("cannot create the Codex credential wake: no active principal")
                return
            create_wake(
                uow,
                admin.clock,
                principal_id=principal.id,
                reason=WakeReason.AUTH_FAILURE,
                summary=summary,
                extra_links={"credentials": "/ui/credentials"},
            )
            uow.commit()

    renewer = CodexCredentialRenewer(
        store=store,
        clock=admin.clock,
        record=record_on_service_loop,
        propagate=propagate_refresh,
        wake=wake,
    )

    # 339: the renewer checks for CREDENTIAL_REFRESH_REQUESTED events so the
    # supervisor can honour a forced refresh recorded from the API. The cursor
    # lives on the supervisor_status row, not in process memory, so a supervisor
    # restart does not replay every historical request as pending (0037).
    _pending_seq: list[int | None] = [None]

    def _has_pending_refresh_request() -> bool:
        try:
            with factory() as uow:
                cursor = uow.supervisor_status.get().refresh_request_cursor or 0
                newest = cursor
                while True:
                    rows = uow.events.list_global(
                        after_seq=newest,
                        kind=EventKind.CREDENTIAL_REFRESH_REQUESTED.value,
                        since=datetime.min.replace(tzinfo=UTC),
                        limit=1000,
                    )
                    if not rows:
                        break
                    assert rows[-1].seq is not None
                    newest = rows[-1].seq
                    if len(rows) < 1000:
                        break
                if newest == cursor:
                    return False
                # All requests observed before the grant share one refresh. Requests
                # arriving during it remain beyond this cursor for the next tick.
                _pending_seq[0] = newest
                return True
        except Exception:  # pragma: no cover - safe fallback for test fakes
            return False

    def _ack_pending_refresh_request() -> None:
        seq = _pending_seq[0]
        if seq is None:
            return
        try:
            with factory() as uow:
                status = uow.supervisor_status.get()
                status.refresh_request_cursor = seq
                uow.supervisor_status.write(status)
                uow.commit()
        except Exception:  # pragma: no cover - safe fallback for test fakes
            log.exception("could not persist the credential refresh cursor")
            return
        _pending_seq[0] = None

    renewer.set_pending_request_checker(_has_pending_refresh_request)
    renewer.set_pending_request_ack(_ack_pending_refresh_request)

    return renewer


def _build_readonly_renewer(
    settings: Settings, providers: Mapping[str, ExecutionProvider]
) -> ReadOnlyCredentialStore | None:
    """Build status reads directly, without first constructing a writer or grant."""
    source = credential_sources(settings).get("codex")
    spec = default_registry().require("codex").credential_spec()
    assert spec is not None
    if effective_mount_mode(spec, source) is not MountMode.RENEWER:
        return None
    if source is not None and source.path and (path := Path(source.path) / "auth.json").is_file():
        return ReadOnlyCredentialStore(FileCredentialReader(path))
    if (
        settings.kubernetes.enabled
        and not settings.docker.enabled
        and isinstance((kubernetes := providers.get("kubernetes")), KubernetesProvider)
    ):
        reader = KubernetesCredentialReader(
            kubernetes.client, kubernetes.credential_secret("codex")
        )
        try:
            reader.read()
        except KubernetesApiError as exc:
            if exc.status != 404:
                raise
        except ValueError:
            pass
        else:
            return ReadOnlyCredentialStore(reader)
    return None


def build_publisher(
    settings: Settings,
    docker: DockerProvider | None,
    kubernetes: ExecutionProvider | None,
    github: GitHubClient | None,
) -> Publisher | None:
    """The publisher of 23 for each provider that is wired, when GitHub is.

    A Kubernetes attempt's bundle is on its workspace claim and is pushed by a Job in
    the workers namespace; a Docker attempt's is pushed by a container on the daemon.
    With both providers the bundle path says which one holds it. With neither, or with
    no GitHub client, there is no publisher, and the supervisor says so for every task
    that waits in `publishing` (hades FDY-0133) rather than skipping it without a word."""
    if github is None:
        return None
    kube: Publisher | None = None
    if isinstance(kubernetes, KubernetesProvider):
        kube = KubernetesPublisher(
            kubernetes,
            KubernetesPublisherConfig(
                credential_host=settings.github.credential_host,
                timeout_seconds=settings.github.publisher_timeout_seconds,
            ),
        )
    dock: Publisher | None = None
    if docker is not None:
        dock = DockerPublisher(
            docker,
            PublisherConfig(
                # 23 step 3: the publisher's own egress network, not the workers'.
                network=settings.github.publisher_network,
                egress_proxy=(
                    settings.github.publisher_egress_proxy or settings.docker.egress_proxy
                ),
                no_proxy=settings.docker.no_proxy,
                credential_host=settings.github.credential_host,
                timeout_seconds=settings.github.publisher_timeout_seconds,
            ),
        )
    if dock is not None and kube is not None:
        return ByWorkspacePublisher(dock, kube)
    return dock or kube
