"""Shared setup for the Kubernetes provider's unit tier (26)."""

from __future__ import annotations

from typing import Any

from crucible.adapters.execution.k8sfake import FakeKubernetesApi, FakeRegistry
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.ports.execution import LaunchSpec
from crucible.ports.harness import MountMode
from tests.fixtures import contract_document

ATTEMPT = "01ATTEMPT0000000000000000A"
TASK = "01TASK00000000000000000000"
IMAGE = "crucible-worker:script-harness-fake-succeed-2"


# A deterministic resolver for the unit tier: the allowlist's names become addresses
# without a network, so the rendered NetworkPolicy is a thing a test can read (26).
HOST_ADDRESSES = {
    "github.com": ["140.82.121.4/32"],
    "api.github.com": ["140.82.121.6/32"],
    "pypi.org": ["151.101.0.223/32"],
    "api.anthropic.com": ["160.79.104.10/32"],
    "api.openai.com": ["162.159.140.245/32"],
    "auth.openai.com": ["162.159.140.246/32"],
    "chatgpt.com": ["162.159.140.247/32"],
    "platform.claude.com": ["160.79.104.20/32"],
    "oauth2.googleapis.com": ["142.250.80.10/32"],
    "www.googleapis.com": ["142.250.80.11/32"],
    "lh3.googleusercontent.com": ["142.250.80.12/32"],
    "ghcr.io": ["140.82.113.34/32"],
}


def fake_resolver(host: str) -> list[str]:
    return list(HOST_ADDRESSES.get(host, []))


def build(
    *,
    harness: str = "script-harness",
    version: str = "1.0.0",
    config: KubernetesConfig | None = None,
    resolver: Any = fake_resolver,
    **api_kwargs: Any,
) -> tuple[FakeKubernetesApi, FakeRegistry, KubernetesProvider]:
    api = FakeKubernetesApi(**api_kwargs)
    registry = FakeRegistry(api)
    registry.register(IMAGE, harness=harness, version=version)
    provider = KubernetesProvider(
        config
        or KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            storage_class="lab-ssd",
            image_pull_secret="ghcr-pull",
            credential_modes={"codex": MountMode.RW_NARROW},
        ),
        api,  # type: ignore[arg-type]
        registry,
        resolver=resolver,
    )
    return api, registry, provider


def spec(
    *,
    attempt_id: str = ATTEMPT,
    harness: str = "script-harness",
    image: str = IMAGE,
    network: str = "policy",
    endpoint: str = "subscription",
    endpoint_url: str | None = None,
    model: str = "none",
    policy: dict[str, Any] | None = None,
    **overrides: Any,
) -> LaunchSpec:
    document = contract_document()
    document["repository"]["work_branch"] = "crucible/EX-0001"
    return LaunchSpec(
        attempt_id=attempt_id,
        task_id=TASK,
        external_id="EX-0001",
        role="implement",
        harness=harness,
        model=model,
        image=image,
        timeout_seconds=600,
        contract=document,
        network=network,  # type: ignore[arg-type]
        endpoint=endpoint,  # type: ignore[arg-type]
        endpoint_url=endpoint_url,
        policy=policy
        or {
            "images": {"allowlist": ["crucible-worker:*"]},
            "network": {"mode": "egress-proxy", "egress_allowlist": ["pypi.org", "github.com"]},
            "resources": {"cpus": 2, "memory": "4GiB"},
            "limits": {"grace_seconds": 30},
        },
        repository_url="https://github.com/acme/example.git",
        **overrides,
    )


def created(api: FakeKubernetesApi, kind: str, name_prefix: str = "") -> list[dict[str, Any]]:
    return [
        row["body"]
        for row in api.created
        if row["kind"] == kind and str(row["name"]).startswith(name_prefix)
    ]


def pod_of(api: FakeKubernetesApi, role_prefix: str) -> dict[str, Any]:
    """The Pod spec of the Job whose name starts with `role_prefix`."""
    for row in api.created:
        if row["kind"] == "jobs" and str(row["name"]).startswith(role_prefix):
            spec_body: dict[str, Any] = row["body"]["spec"]["template"]["spec"]
            return spec_body
    for row in api.created:
        if row["kind"] == "pods" and str(row["name"]).startswith(role_prefix):
            bare: dict[str, Any] = row["body"]["spec"]
            return bare
    raise AssertionError(f"no object named {role_prefix}* was created")
