"""Declared test services (hades #558, #85): the database the integration tier needs.

A worker on either provider has no Docker daemon, so a repository whose checks need a
Postgres server (`make test-integration` reads `CRUCIBLE_TEST_DATABASE_URL` or starts a
container) could not run them before this. A policy, or a task contract under
`execution_request`, declares a `services` list; the one kind so far is `postgres`. The
Kubernetes provider renders each as a native sidecar of the worker Job, the Docker
provider as an extra container sharing the worker's network namespace, and both tell
the worker where it is through the same variable, so one checkout's `make
test-integration` works the same in either. Nothing here does I/O: this module is what
the contracts validate against and what the providers render from.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SERVICE_KIND_POSTGRES = "postgres"
SERVICE_KINDS: tuple[str, ...] = (SERVICE_KIND_POSTGRES,)

# The digest of postgres:16 the CI workflow's integration tier runs
# (tests/integration/postgres.py, the same pin as compose.yaml), so a worker's database
# is the build CI proves against.
POSTGRES_IMAGE = (
    "postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
)
POSTGRES_PORT = 5432
# The role, password and database the sidecar is created with. They are fixed and are
# not a secret: the server listens on the attempt's own loopback and on nothing else.
POSTGRES_USER = "crucible"
POSTGRES_PASSWORD = "crucible"
POSTGRES_DATABASE = "crucible"
# The variable the repository's integration tier reads (tests/integration/postgres.py),
# and the one value both providers put in it.
TEST_DATABASE_ENV = "CRUCIBLE_TEST_DATABASE_URL"
TEST_DATABASE_URL = (
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@127.0.0.1:{POSTGRES_PORT}"
    f"/{POSTGRES_DATABASE}"
)
# The container name prefix a sidecar carries on both providers (`svc-postgres`).
SERVICE_CONTAINER_PREFIX = "svc-"
# The official image initialises its data directory under PGDATA and wants to own it,
# so PGDATA is a leaf of the mounted directory, which uid 1000 creates itself.
POSTGRES_DATA_MOUNT = "/var/lib/postgresql/data"
POSTGRES_DATA_DIR = f"{POSTGRES_DATA_MOUNT}/pgdata"
POSTGRES_RUN_DIR = "/var/run/postgresql"

DEFAULT_SERVICE_CPUS = 1.0
DEFAULT_SERVICE_MEMORY = "1GiB"
DEFAULT_SERVICE_STORAGE = "1Gi"


@dataclass(frozen=True, slots=True)
class DeclaredService:
    """One declared service, resolved from the policy and the contract."""

    kind: str
    image: str
    cpus: float = DEFAULT_SERVICE_CPUS
    memory: str = DEFAULT_SERVICE_MEMORY
    # The size of the data directory (a Kubernetes quantity, `1Gi`).
    storage: str = DEFAULT_SERVICE_STORAGE

    @property
    def name(self) -> str:
        """The sidecar container's name on both providers."""
        return f"{SERVICE_CONTAINER_PREFIX}{self.kind}"

    @property
    def digest(self) -> str:
        """`sha256:...` from a digest-pinned reference, or empty when it is not one."""
        _, separator, digest = self.image.partition("@")
        return digest if separator else ""

    @property
    def port(self) -> int:
        return POSTGRES_PORT

    def worker_env(self) -> dict[str, str]:
        """What the worker container is told: the variable the tier reads."""
        return {TEST_DATABASE_ENV: TEST_DATABASE_URL}

    def container_env(self) -> dict[str, str]:
        """The sidecar's own environment: the role, password and database the URL
        names, and the data directory the image may initialise as uid 1000."""
        return {
            "POSTGRES_USER": POSTGRES_USER,
            "POSTGRES_PASSWORD": POSTGRES_PASSWORD,
            "POSTGRES_DB": POSTGRES_DATABASE,
            "PGDATA": POSTGRES_DATA_DIR,
        }

    def readiness_command(self) -> list[str]:
        """`pg_isready` over TCP on the loopback: the image's bootstrap runs a
        socket-only server first, so a socket check would answer before the server the
        worker connects to exists."""
        return [
            "pg_isready",
            "-h",
            "127.0.0.1",
            "-p",
            str(POSTGRES_PORT),
            "-U",
            POSTGRES_USER,
            "-d",
            POSTGRES_DATABASE,
        ]

    def as_dict(self) -> dict[str, Any]:
        """The evidence record of this service (the attempt's launch evidence)."""
        return {
            "kind": self.kind,
            "container": self.name,
            "image": self.image,
            "image_digest": self.digest,
            "env": TEST_DATABASE_ENV,
            "url": TEST_DATABASE_URL,
            "cpus": self.cpus,
            "memory": self.memory,
            "storage": self.storage,
        }


def _declaration(entry: Mapping[str, Any]) -> DeclaredService | None:
    kind = str(entry.get("kind") or "")
    if kind not in SERVICE_KINDS:
        return None
    resources = entry.get("resources") or {}
    if not isinstance(resources, Mapping):
        resources = {}
    try:
        cpus = float(resources.get("cpus") or DEFAULT_SERVICE_CPUS)
    except (TypeError, ValueError):
        cpus = DEFAULT_SERVICE_CPUS
    return DeclaredService(
        kind=kind,
        image=str(entry.get("image") or POSTGRES_IMAGE),
        cpus=cpus if cpus > 0 else DEFAULT_SERVICE_CPUS,
        memory=str(resources.get("memory") or DEFAULT_SERVICE_MEMORY),
        storage=str(resources.get("storage") or DEFAULT_SERVICE_STORAGE),
    )


def _entries(document: Mapping[str, Any] | None, *path: str) -> list[Mapping[str, Any]]:
    node: Any = document or {}
    for key in path:
        node = node.get(key) if isinstance(node, Mapping) else None
    if not isinstance(node, list):
        return []
    return [entry for entry in node if isinstance(entry, Mapping)]


def declared_services(
    policy: Mapping[str, Any] | None, contract: Mapping[str, Any] | None = None
) -> tuple[DeclaredService, ...]:
    """The services an attempt runs, by kind: the policy's `services`, then the
    contract's `execution_request.services`, which replaces the policy's entry of the
    same kind and drops it with `enabled: false`. Each kind at most once; an entry whose
    kind is unknown is ignored here (the contracts refuse it at validation)."""
    by_kind: dict[str, DeclaredService] = {}
    for entry in [
        *_entries(policy, "services"),
        *_entries(contract, "execution_request", "services"),
    ]:
        kind = str(entry.get("kind") or "")
        if entry.get("enabled") is False:
            by_kind.pop(kind, None)
            continue
        service = _declaration(entry)
        if service is not None:
            by_kind[service.kind] = service
    return tuple(by_kind[kind] for kind in SERVICE_KINDS if kind in by_kind)


def services_env(services: tuple[DeclaredService, ...] | list[DeclaredService]) -> dict[str, str]:
    """The variables every declared service adds to the worker's environment."""
    env: dict[str, str] = {}
    for service in services:
        env.update(service.worker_env())
    return env


__all__ = [
    "DEFAULT_SERVICE_CPUS",
    "DEFAULT_SERVICE_MEMORY",
    "DEFAULT_SERVICE_STORAGE",
    "POSTGRES_DATABASE",
    "POSTGRES_DATA_DIR",
    "POSTGRES_DATA_MOUNT",
    "POSTGRES_IMAGE",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
    "POSTGRES_RUN_DIR",
    "POSTGRES_USER",
    "SERVICE_CONTAINER_PREFIX",
    "SERVICE_KINDS",
    "SERVICE_KIND_POSTGRES",
    "TEST_DATABASE_ENV",
    "TEST_DATABASE_URL",
    "DeclaredService",
    "declared_services",
    "services_env",
]
