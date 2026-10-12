"""Regression tests for FDY-0633: Kubernetes scratch creates no claims."""

from __future__ import annotations

from crucible.adapters.execution import k8sspec, scripts
from crucible.adapters.execution.kubernetes import KubernetesConfig
from crucible.settings import KubernetesSettings
from tests.unit.kubernetes_fixtures import build, spec


def test_every_workspace_volume_is_a_size_limited_empty_dir() -> None:
    assert k8sspec.workspace_volume(size="64Mi") == {
        "name": "ws",
        "emptyDir": {"sizeLimit": "64Mi"},
    }


def test_workspace_size_is_the_only_provider_setting() -> None:
    assert "workspace_ephemeral_class" not in KubernetesConfig.__dataclass_fields__
    assert "workspace_ephemeral_class" not in KubernetesSettings.model_fields
    assert KubernetesConfig().workspace_ephemeral_size == "20Gi"
    assert KubernetesSettings().workspace_ephemeral_size == "20Gi"


async def test_prepare_reads_its_metadata_before_ephemeral_scratch_disappears() -> None:
    api, _registry, provider = build()
    await provider.prepare(spec())
    assert any(
        scripts.PREPARED_HEAD_MARKER in line for lines in api.logs.values() for line in lines
    )
    assert not [
        row for row in api.created if row["kind"] == "pods" and row["name"].startswith("reader-")
    ]
