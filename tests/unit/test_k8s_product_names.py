"""Rename step 2 (hades #609): the Kubernetes names default to the product's.

The product layer says hades: the control plane runs in `hades`, the attempts in
`hades-workers`, Hades's own BuildKit in `hades-buildkit`, and every object the
deployment creates is `hades-*`. The kustomize base, the chart's defaults and the
settings defaults agree on that, and a deployment that set the earlier `crucible` names
explicitly keeps them. The execution service layer (the Python package, the CRUCIBLE_*
setting names, the images, the worker paths and the database) is not renamed here.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.kubernetes import (
    BUILDKIT_HOST,
    BUILDKIT_NAMESPACE,
    BUILDKIT_POD_LABELS,
    KubernetesConfig,
    KubernetesProvider,
    buildkit_host,
    buildkit_pod_labels,
)
from crucible.adapters.execution.room_launch import KubernetesRoomLauncher
from crucible.adapters.first_run import SECRET_NAME
from crucible.cli.wiring import kubernetes_config
from crucible.settings import Settings, load_settings
from tests.unit.kubernetes_fixtures import spec

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "deploy" / "kubernetes" / "base"
OVERLAYS = ROOT / "deploy" / "kubernetes" / "overlays"
CHART = ROOT / "charts" / "hades"
NAMESPACES = {"hades", "hades-workers", "hades-buildkit"}
HARNESSES = ("claude_code", "codex", "agy", "hermes")
IMAGE_CHECKS = [
    {"id": "V4", "command": "make images-check", "expect_exit": 0},
    {"id": "V5", "command": "make registry-check", "expect_exit": 0},
]
# What a deployment installed with the earlier defaults sets to keep them.
OLD_NAMES = {
    "CRUCIBLE_KUBERNETES__NAMESPACE": "crucible",
    "CRUCIBLE_KUBERNETES__WORKERS_NAMESPACE": "crucible-workers",
    "CRUCIBLE_KUBERNETES__SERVICE_ACCOUNT": "crucible-worker",
    "CRUCIBLE_KUBERNETES__CACHE_CLAIM": "crucible-reference-cache",
    "CRUCIBLE_KUBERNETES__FIRST_RUN_SECRET_NAME": "crucible-first-run-admin",
    "CRUCIBLE_KUBERNETES__BUILDKIT_NAMESPACE": "crucible-buildkit",
    "CRUCIBLE_KUBERNETES__CREDENTIAL_SECRETS": json.dumps(
        {h: f"crucible-harness-{h.replace('_', '-')}" for h in HARNESSES}
    ),
    "CRUCIBLE_GITHUB__APP__SECRET_NAME": "crucible-github-app",
    "CRUCIBLE_ROOMS__API_NAMESPACE": "crucible",
}


def _documents(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def _resources(directory: Path) -> list[Path]:
    """The files a kustomization lists, its own directories followed, without kustomize."""
    kustomization = yaml.safe_load((directory / "kustomization.yaml").read_text())
    found: list[Path] = []
    for entry in kustomization.get("resources", []):
        target = (directory / entry).resolve()
        if target.is_dir():
            found.extend(_resources(target))
        elif target.suffix == ".yaml":
            found.append(target)
    return found


def _base_objects() -> list[dict[str, Any]]:
    return [doc for path in _resources(BASE) for doc in _documents(path)]


def _base_settings() -> dict[str, str]:
    for doc in _base_objects():
        if doc["kind"] == "ConfigMap":
            data: dict[str, str] = doc["data"]
            return data
    raise AssertionError("the base has no settings ConfigMap")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in list(os.environ):
        if key.startswith("CRUCIBLE_"):
            monkeypatch.delenv(key)
    return monkeypatch


# ----- AC2: the settings defaults --------------------------------------------


def test_the_settings_default_to_the_product_names(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings()
    k = settings.kubernetes
    assert (k.namespace, k.workers_namespace) == ("hades", "hades-workers")
    assert k.service_account == "hades-worker"
    assert k.first_run_secret_name == "hades-first-run-admin"
    assert k.buildkit_namespace == "hades-buildkit"
    # No claim by default, as before: without one the preparer clones from the remote.
    assert k.cache_claim is None
    assert k.credential_secrets == {}
    assert settings.github.app.secret_name == "hades-github-app"
    assert settings.rooms.api_namespace == "hades"


def test_the_provider_config_built_from_the_defaults_names_hades_objects(
    clean_env: pytest.MonkeyPatch,
) -> None:
    config = kubernetes_config(Settings())
    assert (config.namespace, config.control_namespace) == ("hades-workers", "hades")
    assert config.service_account == "hades-worker"
    assert config.buildkit_namespace == "hades-buildkit"
    assert [config.credential_secret_name(h) for h in HARNESSES] == [
        "hades-harness-claude-code",
        "hades-harness-codex",
        "hades-harness-agy",
        "hades-harness-hermes",
    ]


def test_the_module_defaults_name_hades_objects() -> None:
    config = KubernetesConfig()
    assert (config.namespace, config.control_namespace) == ("hades-workers", "hades")
    assert config.service_account == "hades-worker"
    assert config.credential_secret_name("claude_code") == "hades-harness-claude-code"
    assert k8sspec.PodRequest.__dataclass_fields__["service_account"].default == "hades-worker"
    assert SECRET_NAME == "hades-first-run-admin"
    assert BUILDKIT_NAMESPACE == "hades-buildkit"
    assert BUILDKIT_HOST == "tcp://hades-buildkit.hades-buildkit.svc:1234"
    assert dict(BUILDKIT_POD_LABELS) == {"app.kubernetes.io/name": "hades-buildkit"}
    assert KubernetesRoomLauncher.__dataclass_fields__["api_namespace"].default == "hades"


def test_explicitly_set_crucible_names_are_still_read_as_written(
    clean_env: pytest.MonkeyPatch,
) -> None:
    for key, value in OLD_NAMES.items():
        clean_env.setenv(key, value)
    settings = load_settings()
    k = settings.kubernetes
    assert (k.namespace, k.workers_namespace) == ("crucible", "crucible-workers")
    assert k.service_account == "crucible-worker"
    assert k.cache_claim == "crucible-reference-cache"
    assert k.first_run_secret_name == "crucible-first-run-admin"
    assert k.buildkit_namespace == "crucible-buildkit"
    assert settings.github.app.secret_name == "crucible-github-app"
    assert settings.rooms.api_namespace == "crucible"

    config = kubernetes_config(settings)
    assert (config.namespace, config.control_namespace) == ("crucible-workers", "crucible")
    assert config.service_account == "crucible-worker"
    assert config.cache_claim == "crucible-reference-cache"
    assert config.credential_secret_name("claude_code") == "crucible-harness-claude-code"
    assert config.credential_secret_name("hermes") == "crucible-harness-hermes"
    assert config.buildkit_namespace == "crucible-buildkit"


def _provider(config: KubernetesConfig) -> KubernetesProvider:
    provider = object.__new__(KubernetesProvider)
    provider.config = config
    provider.harnesses = type("Harnesses", (), {"get": lambda *_: None})()
    return provider


@pytest.mark.parametrize("namespace", ["hades-buildkit", "crucible-buildkit"])
def test_the_buildkit_an_attempt_reaches_follows_the_setting(namespace: str) -> None:
    provider = _provider(KubernetesConfig(buildkit_namespace=namespace))
    required = spec()
    required.contract["required_verification"] = IMAGE_CHECKS
    plan = provider._egress_plan(required, k8sspec.ROLE_WORKER)
    assert plan.buildkit_selector == k8sspec.PeerSelector.of(
        namespace, {"app.kubernetes.io/name": namespace}
    )
    assert buildkit_host(namespace) == f"tcp://{namespace}.{namespace}.svc:1234"
    assert dict(buildkit_pod_labels(namespace)) == {"app.kubernetes.io/name": namespace}


# ----- AC1: the kustomize base and overlays ------------------------------------


def test_every_base_object_is_in_a_hades_namespace_with_a_hades_name() -> None:
    objects = _base_objects()
    assert objects
    namespaces = {doc["metadata"]["name"] for doc in objects if doc["kind"] == "Namespace"}
    assert namespaces == NAMESPACES
    for doc in objects:
        metadata = doc["metadata"]
        if doc["kind"] != "Namespace":
            assert metadata["namespace"] in NAMESPACES, (doc["kind"], metadata["name"])
        assert not metadata["name"].startswith("crucible"), (doc["kind"], metadata["name"])
    named = {(doc["kind"], doc["metadata"]["name"]) for doc in objects}
    for kind, name in (
        ("Deployment", "hades-api"),
        ("Deployment", "hades-supervisor"),
        ("StatefulSet", "hades-postgres"),
        ("Job", "hades-migrate"),
        ("ConfigMap", "hades-settings"),
        ("ServiceAccount", "hades-supervisor"),
        ("ServiceAccount", "hades-migrate"),
        ("ServiceAccount", "hades-worker"),
        ("Role", "hades-supervisor"),
        ("RoleBinding", "hades-supervisor"),
        ("PersistentVolumeClaim", "hades-artifacts"),
        ("PersistentVolumeClaim", "hades-reference-cache"),
        ("PersistentVolumeClaim", "hades-buildkit-cache"),
        ("NetworkPolicy", "hades-buildkit"),
        ("Deployment", "hades-buildkit"),
    ):
        assert (kind, name) in named


def test_the_base_settings_are_the_settings_defaults(clean_env: pytest.MonkeyPatch) -> None:
    data = _base_settings()
    k = Settings().kubernetes
    assert data["CRUCIBLE_KUBERNETES__NAMESPACE"] == k.namespace
    assert data["CRUCIBLE_KUBERNETES__WORKERS_NAMESPACE"] == k.workers_namespace
    assert data["CRUCIBLE_KUBERNETES__SERVICE_ACCOUNT"] == k.service_account
    assert data["CRUCIBLE_KUBERNETES__FIRST_RUN_SECRET_NAME"] == k.first_run_secret_name
    assert data["CRUCIBLE_KUBERNETES__BUILDKIT_NAMESPACE"] == k.buildkit_namespace
    assert data["CRUCIBLE_KUBERNETES__CACHE_CLAIM"] == "hades-reference-cache"
    assert data["CRUCIBLE_GITHUB__APP__SECRET_NAME"] == "hades-github-app"
    secrets = json.loads(data["CRUCIBLE_KUBERNETES__CREDENTIAL_SECRETS"])
    assert secrets == {h: KubernetesConfig().credential_secret_name(h) for h in HARNESSES}


def test_no_overlay_or_secret_shape_names_a_crucible_namespace_or_object() -> None:
    paths = [
        *OVERLAYS.rglob("*.yaml"),
        *(ROOT / "deploy" / "kubernetes" / "secret-shapes").rglob("*.yaml"),
        ROOT / "deploy" / "kubernetes" / "argocd" / "application.yaml",
        ROOT / "deploy" / "kind" / "workers.yaml",
    ]
    for path in paths:
        for doc in _documents(path):
            metadata = doc.get("metadata", {})
            assert metadata.get("namespace") not in {"crucible", "crucible-workers"}, path
            name = str(metadata.get("name", ""))
            # The kind tier's own fixtures (its peer namespace and hostPath cache) are
            # not product objects and keep their names.
            if not name.startswith("crucible-kind"):
                assert name not in {"crucible"} and not name.startswith("crucible-"), (path, name)


def test_the_kind_tier_creates_the_workers_namespace_and_buildkit_by_their_new_names() -> None:
    docs = _documents(ROOT / "deploy" / "kind" / "workers.yaml")
    namespaces = {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Namespace"}
    assert {"hades-workers", "hades-buildkit"} <= namespaces
    accounts = [doc for doc in docs if doc["kind"] == "ServiceAccount"]
    assert [(a["metadata"]["namespace"], a["metadata"]["name"]) for a in accounts] == [
        ("hades-workers", "hades-worker")
    ]


# ----- AC1: the chart's defaults and its overrides -------------------------------


def test_the_chart_defaults_to_the_product_names_and_keeps_its_overrides() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    for key in ("nameOverride", "namespaceOverride", "workersNamespaceOverride"):
        assert values[key] == ""
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    assert '.Values.nameOverride | default "hades"' in helpers
    assert '.Values.namespaceOverride | default (include "hades.name" .)' in helpers
    assert values["buildkit"]["namespace"] == "hades-buildkit"
    assert values["postgres"]["databaseSecretName"] == "hades-database"
    assert values["secrets"]["githubApp"] == "hades-github-app"
    assert values["secrets"]["firstRunToken"] == "hades-first-run-admin"
    # The database itself is a later step and keeps its role and name.
    assert (values["postgres"]["user"], values["postgres"]["database"]) == ("crucible",) * 2
    settings = (CHART / "templates" / "configmap.yaml").read_text()
    assert (
        "CRUCIBLE_KUBERNETES__BUILDKIT_NAMESPACE: {{ .Values.buildkit.namespace | quote }}"
        in settings
    )
    for template in (CHART / "templates").glob("*"):
        text = template.read_text()
        assert not re.search(r"^\s+(name|namespace): crucible", text, re.M), template.name


def test_the_chart_checks_cover_the_old_names_set_explicitly() -> None:
    check = (ROOT / "tools" / "chart" / "check.sh").read_text()
    assert "--set namespaceOverride=crucible" in check
    assert "--set workersNamespaceOverride=crucible-workers" in check
    assert "--set buildkit.namespace=crucible-buildkit" in check
    assert "--buildkit-namespace crucible-buildkit" in check
    sync = (ROOT / "tools" / "chart" / "sync.sh").read_text()
    assert "--without-namespace hades-buildkit" in sync


# ----- AC3: the kind tier's scripts ---------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tools/kind/e2e-kind.sh",
        "tools/kind/deploy-kind.sh",
        "tools/kind/cilium-egress.sh",
        "tools/kind/cilium_egress.py",
        "tools/smoke/kubernetes_smoke.py",
        "tools/smoke/first_run_smoke.py",
        "tests/e2e/test_kind.py",
        "tests/e2e/test_kind_self_hosting.py",
    ],
)
def test_the_kind_scripts_use_the_new_names(path: str) -> None:
    text = (ROOT / path).read_text()
    for stale in (
        "-n crucible ",
        '"-n", "crucible"',
        "crucible-workers",
        "crucible-buildkit",
        "crucible-harness-",
        "deployment/crucible-api",
        "deployment/crucible-supervisor",
        "job/crucible-migrate",
        "pvc/crucible-reference-cache",
    ):
        assert stale not in text, (path, stale)


# ----- AC4: the documentation, and the execution layer left alone -----------------


def test_the_deployment_guide_says_how_to_upgrade_from_the_crucible_names() -> None:
    guide = (ROOT / "docs" / "deployment.md").read_text()
    heading = "## Upgrading from crucible names"
    assert heading in guide
    section = guide.split(heading, 1)[1].split("\n## ", 1)[0]
    for needle in (
        "CRUCIBLE_KUBERNETES__NAMESPACE",
        "CRUCIBLE_KUBERNETES__WORKERS_NAMESPACE",
        "CRUCIBLE_KUBERNETES__BUILDKIT_NAMESPACE",
        "nameOverride",
        "buildkit.namespace",
    ):
        assert needle in section, needle
    assert chr(0x2014) not in guide


def test_the_execution_layer_keeps_its_names(clean_env: pytest.MonkeyPatch) -> None:
    assert (ROOT / "crucible" / "settings.py").is_file()
    assert Settings.model_config.get("env_prefix") == "CRUCIBLE_"
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["serviceImage"]["repository"] == "ghcr.io/sentania-labs/crucible"
    assert values["workerImage"]["repository"] == "ghcr.io/sentania-labs/crucible-worker"
    data = _base_settings()
    assert data["CRUCIBLE_SERVICE__ARTIFACT_ROOT"] == "/var/lib/crucible/artifacts"
    # The Docker provider's names are not Kubernetes names and are untouched.
    docker = Settings().docker
    assert (docker.workers_network, docker.artifact_volume) == (
        "crucible-workers",
        "crucible-artifacts",
    )
